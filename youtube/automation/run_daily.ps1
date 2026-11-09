# Daily vegan video runner. Started by Task Scheduler ("Vegan Daily Video").
# Each successful run produces exactly one new post folder: vegan\postN
# Keep this file ASCII: Windows PowerShell 5.1 misreads BOM-less UTF-8.
param([int]$MaxAttempts = 3, [int]$RetryWaitMinutes = 15)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$Auto = $PSScriptRoot
$Logs = Join-Path $Auto 'logs'
$Work = Join-Path $Root 'work'
New-Item -ItemType Directory -Force -Path $Logs, $Work | Out-Null

$Stamp = Get-Date -Format 'yyyy-MM-dd_HHmmss'
$RunLog = Join-Path $Logs "run_$Stamp.log"
function Log([string]$m) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $m"
    [IO.File]::AppendAllText($RunLog, "$line`r`n", [Text.UTF8Encoding]::new($false))
    Write-Output $line
}
function Set-Status([string]$m) {
    [IO.File]::WriteAllText((Join-Path $Auto 'LAST_STATUS.txt'), "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $m`r`nLog: $RunLog`r`n", [Text.UTF8Encoding]::new($false))
}

# Never run two videos at once, even if started by hand during a scheduled run.
$Mutex = New-Object Threading.Mutex($false, 'Global\VeganDailyVideo')
if (-not $Mutex.WaitOne(0)) { Log 'Another run is still active. Exiting.'; exit 0 }

function Get-NextPostNumber {
    $n = 0
    Get-ChildItem -Path $Root -Directory -Filter 'post*' | ForEach-Object {
        if ($_.Name -match '^post(\d+)$' -and [int]$Matches[1] -gt $n) { $n = [int]$Matches[1] }
    }
    return $n + 1
}

function Test-Deliverables([string]$Dir) {
    # Returns a list of problems; an empty list means the video may be published to postN.
    $bad = @()
    if (Test-Path (Join-Path $Dir 'FAILED.txt')) { $bad += 'Claude wrote FAILED.txt' }
    foreach ($f in 'video_final.mp4', 'PUBLISH.txt', 'CREDITS.txt', 'voiceover.srt', 'captions_final.srt', 'NOTES.txt') {
        $p = Join-Path $Dir $f
        if (-not (Test-Path $p) -or (Get-Item $p).Length -eq 0) { $bad += "missing or empty: $f" }
    }
    if (-not (Test-Path (Join-Path $Dir 'thumbnails\thumb_1.png'))) { $bad += 'missing: thumbnails\thumb_1.png' }
    $video = Join-Path $Dir 'video_final.mp4'
    if (Test-Path $video) {
        $probe = & ffprobe -v error -show_entries 'stream=codec_type,width,height:format=duration' -of json $video | Out-String | ConvertFrom-Json
        $v = @($probe.streams | Where-Object { $_.codec_type -eq 'video' })[0]
        $hasAudio = @($probe.streams | Where-Object { $_.codec_type -eq 'audio' }).Count -gt 0
        $dur = [double]$probe.format.duration
        if (-not $v -or $v.width -ne 1920 -or $v.height -ne 1080) { $bad += "video is not 1920x1080" }
        if (-not $hasAudio) { $bad += 'video has no audio track' }
        if ($dur -lt 170 -or $dur -gt 320) { $bad += "duration out of range: $([math]::Round($dur)) s" }
    }
    return , $bad
}

try {
    $Exit = 1
    $N = Get-NextPostNumber
    $Post = "post$N"
    $Dest = Join-Path $Root $Post
    $RunDir = Join-Path $Work $Post
    if (Test-Path $RunDir) {
        # Left over from a failed run: keep it for inspection, reuse the number so posts stay gap-free.
        Rename-Item $RunDir "$($Post)_failed_$Stamp"
    }
    New-Item -ItemType Directory -Force -Path (Join-Path $RunDir 'deliver') | Out-Null
    # Copy the project template here: under dontAsk, Claude's cp from the skill folder is refused.
    Copy-Item (Join-Path $env:USERPROFILE '.claude\skills\vegan-explainer-video\assets\template') (Join-Path $RunDir 'video') -Recurse
    Log "Starting $Post in $RunDir"
    Set-Status "RUNNING $Post"

    # Topic: first non-empty, non-comment line of topics.txt; otherwise Claude chooses.
    $TopicsFile = Join-Path $Auto 'topics.txt'
    $Topic = $null
    if (Test-Path $TopicsFile) {
        $Topic = Get-Content $TopicsFile -Encoding UTF8 | Where-Object { $_.Trim() -and -not $_.TrimStart().StartsWith('#') } | Select-Object -First 1
    }
    if ($Topic) { $TopicText = "The topic for this episode is: $Topic" }
    else { $TopicText = 'No topic was queued: choose one yourself from the skill''s references/topics.md.' }
    Log "Topic: $(if ($Topic) { $Topic } else { '(Claude chooses)' })"

    $Task = (Get-Content (Join-Path $Auto 'task_template.md') -Raw -Encoding UTF8).
        Replace('{{POST}}', $Post).Replace('{{RUN_DIR}}', $RunDir).Replace('{{ROOT}}', $Root).Replace('{{TOPIC}}', $TopicText)
    $TaskFile = Join-Path $RunDir 'TASK.md'
    [IO.File]::WriteAllText($TaskFile, $Task, [Text.UTF8Encoding]::new($false))

    # Environment for Claude and the tools it starts.
    $env:Path = "$(Join-Path $Auto 'bin');$env:Path;$env:LOCALAPPDATA\Microsoft\WinGet\Links"
    $env:PYTHONUTF8 = '1'
    $env:PYTHONIOENCODING = 'utf-8'
    $env:BASH_DEFAULT_TIMEOUT_MS = '600000'
    $env:BASH_MAX_TIMEOUT_MS = '14400000'   # render and voice steps run in the foreground for a long time
    # A stray API key would bill the API instead of the subscription.
    Remove-Item Env:ANTHROPIC_API_KEY -ErrorAction SilentlyContinue

    $Settings = Join-Path $Auto 'settings.json'
    $SessionId = [guid]::NewGuid().ToString()
    $Deliver = Join-Path $RunDir 'deliver'
    $Problems = @('not run')

    Set-Location $RunDir
    for ($a = 1; $a -le $MaxAttempts; $a++) {
        $Out = Join-Path $Logs "claude_$($Stamp)_a$a.jsonl"
        $Err = Join-Path $Logs "claude_$($Stamp)_a$a.err.txt"
        if ($a -eq 1) {
            $Session = "--session-id $SessionId"
            $Prompt = 'Read TASK.md in the current directory and carry it out completely.'
        } else {
            $Session = "--resume $SessionId"
            $Prompt = 'The run stopped before finishing. Continue from where it stopped and complete TASK.md. These checks failed: ' + (($Problems -join '; ') -replace '["%&|<>^]', '')
        }
        Log "Attempt $a of $MaxAttempts"
        $cmd = "claude -p `"$Prompt`" $Session --permission-mode dontAsk --settings `"$Settings`" --add-dir `"$Root`" --output-format stream-json --verbose > `"$Out`" 2> `"$Err`""
        $ErrorActionPreference = 'Continue'
        & cmd.exe /d /c $cmd
        $code = $LASTEXITCODE
        $ErrorActionPreference = 'Stop'
        Log "Claude exit code: $code"

        $Problems = Test-Deliverables $Deliver
        if ($Problems.Count -eq 0) { break }
        Log "Checks failed: $($Problems -join '; ')"
        if ($a -lt $MaxAttempts) { Start-Sleep -Seconds ($RetryWaitMinutes * 60) }
    }
    Set-Location $Auto

    if ($Problems.Count -gt 0) {
        Log "FAILED: no post created. Work kept in $RunDir"
        Set-Status "FAILED $Post : $($Problems -join '; ')"
    } else {
        # Publish only now, so an unfinished video can never appear as a postN folder.
        Move-Item $Deliver $Dest
        Log "Created $Dest"

        if ($Topic) {
            $remaining = New-Object Collections.ArrayList
            $removed = $false
            foreach ($l in (Get-Content $TopicsFile -Encoding UTF8)) {
                if (-not $removed -and $l -eq $Topic) { $removed = $true; continue }
                [void]$remaining.Add($l)
            }
            [IO.File]::WriteAllLines($TopicsFile, [string[]]$remaining, [Text.UTF8Encoding]::new($false))
        }

        # Free disk space; storyboard, src, public and voice stay so the video can be edited later.
        foreach ($d in 'video\node_modules', 'video\out', 'video\candidates') {
            $p = Join-Path $RunDir $d
            if (Test-Path $p) { Remove-Item $p -Recurse -Force -ErrorAction SilentlyContinue }
        }

        # Upload to Google Drive when rclone is set up. Copy only: never deletes anything on Drive.
        $DriveNote = 'Drive: skipped (rclone remote gdrive not configured)'
        if (Get-Command rclone -ErrorAction SilentlyContinue) {
            $ErrorActionPreference = 'Continue'
            $remotes = & rclone listremotes 2>$null
            if ($remotes -contains 'gdrive:') {
                # All postN folders, so anything a past run failed to upload is caught up. Unchanged files are skipped.
                # The gdrive remote is rooted at the user's chosen Drive folder (root_folder_id), so postN goes directly inside it.
                & rclone copy $Root 'gdrive:' --include '/post[0-9]*/**' --log-file (Join-Path $Logs "rclone_$Stamp.log") --log-level INFO
                if ($LASTEXITCODE -eq 0) { $DriveNote = "Drive: uploaded $Post to the shared Drive folder" }
                else { $DriveNote = "Drive: UPLOAD FAILED (exit $LASTEXITCODE), the post is still on this machine" }
            }
            $ErrorActionPreference = 'Stop'
        }
        Log $DriveNote
        Set-Status "OK $Post created. $DriveNote"
        $Exit = 0
    }
} catch {
    Log "ERROR: $($_.Exception.Message)"
    Set-Status "FAILED: $($_.Exception.Message)"
    $Exit = 1
} finally {
    $Mutex.ReleaseMutex()
}
exit $Exit
