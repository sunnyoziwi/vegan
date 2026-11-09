#!/usr/bin/env bash
# Steps of the "Video daily" GitHub Actions workflow. Each step gets only the secret it needs.
# Claude's output never goes to the Actions log, because the log of a public repo is public.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${GITHUB_WORKSPACE:-$(dirname "$HERE")}"
PAST="$ROOT/_past"
LOGS="$ROOT/_logs"
SKILL_ARCHIVE="_automation/vegan-explainer-video.tgz"
MAX_ATTEMPTS=3

first_topic() {
  grep -v -E '^[[:space:]]*(#|$)' "$HERE/topics.txt" 2>/dev/null | head -n 1 || true
}

# Writes the Drive login for this step only and removes it when the step ends.
with_drive() {
  export RCLONE_CONFIG="$RUNNER_TEMP/rclone.conf"
  printf '%s\n' "$RCLONE_CONF" > "$RCLONE_CONFIG"
  trap 'rm -f "$RCLONE_CONFIG"' EXIT
}

prepare() {
  with_drive
  rclone copyto "gdrive:$SKILL_ARCHIVE" "$RUNNER_TEMP/skill.tgz"
  mkdir -p "$HOME/.claude/skills" "$PAST" "$LOGS"
  tar -xzf "$RUNNER_TEMP/skill.tgz" -C "$HOME/.claude/skills"
  rclone copy gdrive: "$PAST" --include '/post[0-9]*/PUBLISH.txt'

  local top n
  top="$(rclone lsf gdrive: --dirs-only | sed -n -E 's#^post([0-9]+)/$#\1#p' | sort -n | tail -n 1)"
  n=$(( ${top:-0} + 1 ))
  echo "POST=post$n" >> "$GITHUB_ENV"
  echo "RUN_DIR=$ROOT/_run/post$n" >> "$GITHUB_ENV"
  echo "Next episode: post$n"
}

# Prints one line per problem. No output means the folder may be published as postN.
check() {
  python - "$1" <<'EOF'
import json, os, subprocess, sys
d = sys.argv[1]
bad = []
if os.path.exists(os.path.join(d, "FAILED.txt")):
    bad.append("Claude wrote FAILED.txt")
for f in ("video_final.mp4", "PUBLISH.txt", "CREDITS.txt", "voiceover.srt", "captions_final.srt", "NOTES.txt"):
    p = os.path.join(d, f)
    if not os.path.exists(p) or os.path.getsize(p) == 0:
        bad.append("missing or empty: " + f)
if not os.path.exists(os.path.join(d, "thumbnails", "thumb_1.png")):
    bad.append("missing: thumbnails/thumb_1.png")
video = os.path.join(d, "video_final.mp4")
if os.path.exists(video) and os.path.getsize(video):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height:format=duration",
                        "-of", "json", video], capture_output=True, text=True)
    try:
        info = json.loads(r.stdout)
        vs = [s for s in info["streams"] if s["codec_type"] == "video"]
        if not vs or vs[0]["width"] != 1920 or vs[0]["height"] != 1080:
            bad.append("video is not 1920x1080")
        if not any(s["codec_type"] == "audio" for s in info["streams"]):
            bad.append("video has no audio track")
        length = float(info["format"]["duration"])
        if not 170 <= length <= 320:
            bad.append("duration out of range: %d s" % length)
    except (ValueError, KeyError):
        bad.append("ffprobe could not read the video")
print("\n".join(bad))
EOF
}

# The script copies the deliverables itself, so a refused cp inside Claude cannot lose a finished video.
assemble() {
  local out="$RUN_DIR/video/out" deliver="$RUN_DIR/deliver"
  rm -rf "$deliver"
  mkdir -p "$deliver/thumbnails"
  for f in video_final.mp4 PUBLISH.txt CREDITS.txt voiceover.srt captions_final.srt; do
    if [ -f "$out/$f" ]; then cp "$out/$f" "$deliver/"; fi
  done
  if [ -d "$out/thumbnails" ]; then
    find "$out/thumbnails" -maxdepth 1 -name 'thumb_*.png' -exec cp {} "$deliver/thumbnails/" \;
  fi
  for f in NOTES.txt FAILED.txt; do
    if [ -f "$RUN_DIR/$f" ]; then cp "$RUN_DIR/$f" "$deliver/"; fi
  done
}

make() {
  mkdir -p "$RUN_DIR" "$LOGS"
  cp -r "$HOME/.claude/skills/vegan-explainer-video/assets/template" "$RUN_DIR/video"

  local topic topic_text
  topic="$(first_topic)"
  if [ -n "$topic" ]; then topic_text="The topic for this episode is: $topic"
  else topic_text="No topic was queued: choose one yourself from the skill's references/topics.md."; fi
  echo "Topic: ${topic:-(Claude chooses)}"

  TOPIC_TEXT="$topic_text" PAST="$PAST" python - "$HERE/task_template.md" "$RUN_DIR/TASK.md" <<'EOF'
import os, sys
text = open(sys.argv[1], encoding="utf-8").read()
for key, value in (("{{POST}}", os.environ["POST"]), ("{{RUN_DIR}}", os.environ["RUN_DIR"]),
                   ("{{PAST}}", os.environ["PAST"]), ("{{TOPIC}}", os.environ["TOPIC_TEXT"])):
    text = text.replace(key, value)
open(sys.argv[2], "w", encoding="utf-8").write(text)
EOF

  export BASH_DEFAULT_TIMEOUT_MS=600000 BASH_MAX_TIMEOUT_MS=14400000 PYTHONUTF8=1
  unset ANTHROPIC_API_KEY || true

  local session problems prompt code a
  session="$(python -c 'import uuid; print(uuid.uuid4())')"
  problems="not run"
  cd "$RUN_DIR"
  for ((a = 1; a <= MAX_ATTEMPTS; a++)); do
    echo "Attempt $a of $MAX_ATTEMPTS"
    if [ "$a" -eq 1 ]; then
      prompt="Read TASK.md in the current directory and carry it out completely."
      args=(--session-id "$session")
    else
      prompt="The run stopped before finishing. Continue from where it stopped and complete TASK.md. These checks failed: $(printf '%s' "$problems" | tr '\n' ';')"
      args=(--resume "$session")
    fi
    code=0
    claude -p "$prompt" "${args[@]}" --permission-mode dontAsk --settings "$HERE/settings.json" \
      --add-dir "$PAST" --output-format stream-json --verbose \
      > "$LOGS/claude_a$a.jsonl" 2> "$LOGS/claude_a$a.err.txt" || code=$?
    echo "Claude exit code: $code"
    assemble
    problems="$(check "$RUN_DIR/deliver")"
    if [ -z "$problems" ]; then
      echo "All checks passed."
      return 0
    fi
    echo "Checks failed:"
    printf '%s\n' "$problems"
  done
  echo "No video after $MAX_ATTEMPTS attempts. Full log: _logs/ on Drive."
  return 1
}

publish() {
  with_drive
  local dest="$POST"
  if [ "${TEST_RUN:-false}" = "true" ]; then dest="_test/$POST"; fi
  rclone copy "$RUN_DIR/deliver" "gdrive:$dest"
  echo "Uploaded to Drive: $dest"
}

logs() {
  with_drive
  if [ -d "$LOGS" ]; then rclone copy "$LOGS" "gdrive:_logs/${GITHUB_RUN_ID:-local}"; fi
}

finish_topic() {
  local topic
  topic="$(first_topic)"
  if [ -z "$topic" ]; then return 0; fi
  TOPIC="$topic" python - "$HERE/topics.txt" <<'EOF'
import os, sys
path, topic = sys.argv[1], os.environ["TOPIC"]
lines = open(path, encoding="utf-8").read().splitlines()
lines.remove(topic)
open(path, "w", encoding="utf-8").write("\n".join(lines) + "\n")
EOF
  git config user.name "github-actions[bot]"
  git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
  git add video/topics.txt
  git commit -m "Remove finished video topic"
  local url="https://x-access-token:${GH_TOKEN}@github.com/${GITHUB_REPOSITORY}.git"
  git pull --rebase "$url" main
  git push "$url" HEAD:main
}

case "${1:-}" in
  prepare|make|publish|logs|finish_topic) "$1" ;;
  check) check "$2" ;;
  *) echo "usage: run_video.sh prepare|make|publish|logs|finish_topic|check <dir>"; exit 2 ;;
esac
