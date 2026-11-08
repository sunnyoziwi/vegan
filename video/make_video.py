#!/usr/bin/env python3
"""Dựng video tin tức từ story.json: mỗi cảnh = 1 câu thoại + 1 ảnh minh hoạ đúng câu đó.

    python video/make_video.py video/sample/story.json out.mp4

story.json:
  {"lang": "vi", "width": 1080, "height": 1920,
   "scenes": [{"text": "Câu thoại.", "image": "img/a.jpg"}, ...]}

Giọng: chỉ VoiceStudio (VOICESTUDIO_*; mặc định giọng nam). Thời lượng mỗi cảnh = độ dài thật của
câu được đọc, nên ảnh luôn đổi đúng lúc câu thoại chuyển.
"""
import json, os, shutil, subprocess, sys, tempfile, urllib.request
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

TAIL = 0.35          # nghỉ sau mỗi câu (giây)
ZOOM = 0.06          # ảnh phóng nhẹ 6% trong mỗi cảnh


def find_bin(name):
    p = os.environ.get(name.upper()) or shutil.which(name)
    if p:
        return p
    root = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/WinGet/Packages"
    for c in root.glob(f"Gyan.FFmpeg*/ffmpeg-*/bin/{name}.exe"):
        return str(c)
    sys.exit(f"Không tìm thấy {name}. Cài FFmpeg hoặc đặt biến môi trường {name.upper()}.")


FFMPEG, FFPROBE = find_bin("ffmpeg"), find_bin("ffprobe")


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf8")
    if r.returncode:
        sys.exit("Lỗi lệnh:\n" + " ".join(map(str, cmd)) + "\n" + r.stderr[-1500:])
    return r.stdout


def duration(path):
    return float(run([FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path]).strip())


def voicestudio_up(base):
    try:
        urllib.request.urlopen(base.rsplit("/v1", 1)[0] + "/.well-known/voicestudio-speech", timeout=5)
        return True
    except Exception:
        return False


def tts(text, out):
    """Giọng chỉ lấy từ VoiceStudio (API OpenAI-compatible), không có giọng dự phòng.
    VOICESTUDIO_VOICE  = tên/id giọng đã clone trong app (bỏ trống = giọng thiết kế theo VOICESTUDIO_INSTRUCT)
    VOICESTUDIO_INSTRUCT = thuộc tính thiết kế giọng, mặc định giọng nam trầm trung niên
    VOICESTUDIO_URL / VOICESTUDIO_MODEL / VOICESTUDIO_API_KEY: chỉ đổi khi cần."""
    base = os.environ.get("VOICESTUDIO_URL", "http://localhost:3900/v1")
    if not voicestudio_up(base):
        sys.exit("VoiceStudio chưa chạy ở " + base + ". Mở VoiceStudio (hoặc bật container) rồi chạy lại.")
    body = {"model": os.environ.get("VOICESTUDIO_MODEL") or "tts-1", "input": text,
            "voice": os.environ.get("VOICESTUDIO_VOICE") or "default",
            "instructions": os.environ.get("VOICESTUDIO_INSTRUCT") or "male, middle-aged, low pitch",
            "response_format": "mp3"}
    headers = {"Content-Type": "application/json"}
    if os.environ.get("VOICESTUDIO_API_KEY"):
        headers["Authorization"] = "Bearer " + os.environ["VOICESTUDIO_API_KEY"]
    err = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(base + "/audio/speech", data=json.dumps(body).encode(), headers=headers)
            Path(out).write_bytes(urllib.request.urlopen(req, timeout=600).read())
            if os.path.getsize(out) > 1000:
                return
            err = "file âm thanh rỗng"
        except Exception as e:
            err = e
    sys.exit(f"VoiceStudio đọc lỗi: {err}")


def font(size):
    for f in ("C:/Windows/Fonts/arialbd.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf"):
        if os.path.exists(f):
            return ImageFont.truetype(f, size)
    return ImageFont.load_default()


def chunks(text, max_words=6):
    """Chia câu thoại thành cụm ngắn để phụ đề dễ đọc; ngắt ở dấu câu trước."""
    words, out, cur = text.split(), [], []
    for w in words:
        cur.append(w)
        if len(cur) >= max_words or (w[-1] in ",;:.?!" and len(cur) >= 3):
            out.append(" ".join(cur)); cur = []
    if cur:
        if out and len(cur) < 3:
            out[-1] += " " + " ".join(cur)
        else:
            out.append(" ".join(cur))
    return out


def caption_png(text, W, H, path):
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    f = font(int(W * 0.050))
    maxw = int(W * 0.86)
    lines, cur = [], ""
    for w in text.split():
        t = (cur + " " + w).strip()
        if d.textlength(t, font=f) > maxw and cur:
            lines.append(cur); cur = w
        else:
            cur = t
    lines.append(cur)
    lh = int(f.size * 1.35)
    box_h = lh * len(lines) + 44
    y0 = int(H * 0.70)
    d.rounded_rectangle([int(W * 0.05), y0, int(W * 0.95), y0 + box_h], radius=28, fill=(0, 0, 0, 170))
    for i, ln in enumerate(lines):
        x = (W - d.textlength(ln, font=f)) / 2
        d.text((x, y0 + 22 + i * lh), ln, font=f, fill=(255, 255, 255, 255))
    im.save(path)


def render_scene(img, audio, dur, caps, W, H, out, tmp, idx):
    """Nền = chính ảnh làm mờ; ảnh nằm giữa, phóng nhẹ; phụ đề theo cụm; âm thanh = giọng đọc."""
    inputs = ["-loop", "1", "-framerate", "30", "-t", f"{dur:.3f}", "-i", str(img), "-i", audio]
    total = sum(len(c) for c in caps) or 1
    t = 0.0
    parts, last = [], "[v1]"
    for k, c in enumerate(caps):
        seg = dur * len(c) / total
        p = Path(tmp) / f"cap_{idx}_{k}.png"
        caption_png(c, W, H, p)
        inputs += ["-loop", "1", "-t", f"{dur:.3f}", "-i", str(p)]
        a, b = t, t + seg
        nxt = f"[c{k}]"
        parts.append(f"{last}[{k + 2}:v]overlay=0:0:enable='between(t,{a:.3f},{b:.3f})'{nxt}")
        last, t = nxt, b
    # Kích thước ảnh rõ (số chẵn). Zoom bằng zoompan trên ảnh đã phóng 4x để sai số làm tròn
    # nhỏ hơn 1/4 điểm ảnh — scale theo từng khung hình bị làm tròn nguyên điểm ảnh nên giật.
    iw, ih = Image.open(img).size
    k = min(W / iw, H * 0.68 / ih)
    fw, fh = int(iw * k) // 2 * 2, int(ih * k) // 2 * 2
    n = max(1, round(dur * 30))
    fc = (
        f"[0:v]split[a][b];"
        f"[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=40:8,eq=brightness=-0.12[bg];"
        f"[b]scale={fw * 4}:{fh * 4}:flags=lanczos,"
        f"zoompan=z='1+{ZOOM}*on/{n}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={fw}x{fh}:fps=30[fg0];"
        f"[bg][fg0]overlay=x={(W - fw) // 2}:y={(H - fh) // 2 - 80}:format=auto[v1];"
        + ";".join(parts)
    )
    fc = fc.replace(f"[{last[1:-1]}]", "[vout]") if parts else fc.replace("[v1]", "[vout]")
    run([FFMPEG, "-y", "-loglevel", "error", *inputs, "-filter_complex", fc, "-map", "[vout]", "-map", "1:a",
         "-af", f"apad=pad_dur={TAIL}", "-t", f"{dur:.3f}", "-r", "30", "-c:v", "libx264", "-pix_fmt", "yuv420p",
         "-crf", "20", "-c:a", "aac", "-b:a", "160k", "-ar", "44100", str(out)])


def main(story_path, out_path):
    story = json.loads(Path(story_path).read_text(encoding="utf8"))
    base = Path(story_path).parent
    W, H = story.get("width", 1080), story.get("height", 1920)
    with tempfile.TemporaryDirectory() as tmp:
        segs = []
        for i, sc in enumerate(story["scenes"]):
            img = base / sc["image"]
            if not img.exists():
                sys.exit(f"Thiếu ảnh cảnh {i + 1}: {img}")
            mp3 = str(Path(tmp) / f"v{i}.mp3")
            tts(sc["text"], mp3)
            dur = duration(mp3) + TAIL
            seg = Path(tmp) / f"seg{i}.mp4"
            render_scene(img, mp3, dur, chunks(sc["text"]), W, H, seg, tmp, i)
            segs.append(seg)
            print(f"cảnh {i + 1}: {dur:.1f}s  {sc['image']}  «{sc['text'][:50]}…»")
        lst = Path(tmp) / "list.txt"
        lst.write_text("\n".join(f"file '{s.as_posix()}'" for s in segs), encoding="utf8")
        run([FFMPEG, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy",
             "-movflags", "+faststart", str(out_path)])
    print("xong:", out_path, f"{duration(str(out_path)):.1f}s")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
