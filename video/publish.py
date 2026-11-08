#!/usr/bin/env python3
"""Đăng các video đã làm (run/video/<n>/video.mp4 + publish.json) lên Facebook Page hoặc TikTok.

    python video/publish.py --dir run/video --to facebook   # cần FB_PAGE_TOKEN (+ FB_PAGE_ID)
    python video/publish.py --dir run/video --to tiktok     # cần TIKTOK_CLIENT_KEY/SECRET/REFRESH_TOKEN

Cách đăng giống hai flow ảnh cũ: Facebook hẹn giờ trải đều trong khung giờ của ngày kế tiếp;
TikTok gửi vào hộp nháp (TIKTOK_POST_MODE=inbox) hoặc đăng thẳng (direct).
Lỗi đăng không làm hỏng workflow: video vẫn nằm trong artifact để đăng tay.
"""
import argparse, json, os, sys, time, urllib.error, urllib.parse, urllib.request, uuid
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def call(url, data=None, headers=None, method=None, timeout=300):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        body = r.read()
    except urllib.error.HTTPError as e:
        body = e.read()
        return e.code, body
    return r.status, body


def jcall(url, payload, headers):
    st, body = call(url, json.dumps(payload).encode(), {"Content-Type": "application/json; charset=UTF-8", **headers})
    try: return st, json.loads(body)
    except Exception: return st, {"raw": body[:300].decode("utf8", "ignore")}


def videos(d):
    out = []
    for p in sorted(Path(d).glob("*/video.mp4")):
        meta = p.parent / "publish.json"
        if meta.exists(): out.append((p, json.loads(meta.read_text(encoding="utf8"))))
    return out


def message(meta):
    tags = " ".join("#" + h.lstrip("#") for h in meta.get("hashtags", []))
    return "\n\n".join(x for x in [meta.get("caption", "").strip(), tags, "Nguồn: " + meta.get("source", "")] if x)


# ---------- Facebook ----------
def multipart(fields, file_field, path):
    b = uuid.uuid4().hex
    parts = []
    for k, v in fields.items():
        parts.append(f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    parts.append(f'--{b}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{path.name}"\r\nContent-Type: video/mp4\r\n\r\n'.encode())
    parts += [path.read_bytes(), f"\r\n--{b}--\r\n".encode()]
    return b"".join(parts), f"multipart/form-data; boundary={b}"


def hm(s, d):
    try: h, m = (s or d).split(":"); return int(h), int(m)
    except Exception: return hm(d, d)


def fb_times(n):
    """Giờ đăng cho n video: trải đều từ FB_START đến FB_END của ngày kế tiếp còn kịp (như flow ảnh)."""
    tz = ZoneInfo(os.environ.get("FB_TIMEZONE") or "Asia/Ho_Chi_Minh")
    now = datetime.now(tz)
    sh, sm = hm(os.environ.get("FB_START"), "10:00"); eh, em = hm(os.environ.get("FB_END"), "21:00")
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if day.replace(hour=sh, minute=sm) < now + timedelta(minutes=30): day += timedelta(days=1)
    start, end = day.replace(hour=sh, minute=sm), day.replace(hour=eh, minute=em)
    step = (end - start) / (n - 1) if n > 1 else timedelta(0)
    return [start + step * i for i in range(n)]


def to_facebook(vids):
    token, page = os.environ.get("FB_PAGE_TOKEN", ""), os.environ.get("FB_PAGE_ID", "604213449441581")
    if not token: log("Thiếu FB_PAGE_TOKEN: bỏ qua đăng Facebook"); return 0
    ver = os.environ.get("FB_GRAPH_VERSION", "v24.0")
    ok = 0
    for (path, meta), when in zip(vids, fb_times(len(vids))):
        fields = {"access_token": token, "description": message(meta), "title": meta.get("title", "")[:100],
                  "published": "false", "scheduled_publish_time": int(when.timestamp())}
        body, ctype = multipart(fields, "source", path)
        st, resp = call(f"https://graph-video.facebook.com/{ver}/{page}/videos", body, {"Content-Type": ctype}, "POST", 1800)
        try: r = json.loads(resp)
        except Exception: r = {}
        if st < 300 and r.get("id"):
            ok += 1; log(f"Facebook: hẹn {when:%d/%m %H:%M} ({when.tzname()}) — {path.parent.name} — id {r['id']}")
        else:
            log(f"Facebook LỖI {path.parent.name}: HTTP {st} {str(r or resp[:300])[:300]}")
    return ok


# ---------- TikTok ----------
def to_tiktok(vids):
    ck, cs, rt = (os.environ.get(k, "") for k in ("TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET", "TIKTOK_REFRESH_TOKEN"))
    if not (ck and cs and rt): log("Thiếu TIKTOK_CLIENT_KEY/SECRET/REFRESH_TOKEN: bỏ qua đăng TikTok"); return 0
    st, body = call("https://open.tiktokapis.com/v2/oauth/token/", urllib.parse.urlencode(
        {"client_key": ck, "client_secret": cs, "grant_type": "refresh_token", "refresh_token": rt}).encode(),
        {"Content-Type": "application/x-www-form-urlencoded"}, "POST")
    tok = json.loads(body).get("access_token") if st < 300 else None
    if not tok: log("TikTok refresh token lỗi:", body[:300]); return 0
    direct = os.environ.get("TIKTOK_POST_MODE", "inbox").lower() == "direct"
    auth = {"Authorization": "Bearer " + tok}
    ok = 0
    for path, meta in vids:
        size = path.stat().st_size
        chunk = size if size <= 64 * 1024 * 1024 else 32 * 1024 * 1024
        total = -(-size // chunk)
        title = (meta.get("title") or "")[:90]
        desc = message(meta)[:2200]
        if direct:
            ep = "/v2/post/publish/video/init/"
            info = {"post_info": {"title": desc, "privacy_level": os.environ.get("TIKTOK_PRIVACY", "SELF_ONLY"),
                                  "disable_comment": False}}
        else:
            ep = "/v2/post/publish/inbox/video/init/"
            info = {}
        init = {**info, "source_info": {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk, "total_chunk_count": total}}
        st, r = jcall("https://open.tiktokapis.com" + ep, init, auth)
        data = r.get("data") or {}
        if st >= 300 or not data.get("upload_url"):
            log(f"TikTok LỖI khởi tạo {path.parent.name}: HTTP {st} {json.dumps(r.get('error') or r, ensure_ascii=False)[:300]}"); continue
        raw = path.read_bytes(); good = True
        for i in range(total):
            a, b = i * chunk, min(size, (i + 1) * chunk) - 1
            st, resp = call(data["upload_url"], raw[a:b + 1], {"Content-Type": "video/mp4", "Content-Length": str(b - a + 1),
                            "Content-Range": f"bytes {a}-{b}/{size}"}, "PUT", 600)
            if st >= 300: log(f"TikTok LỖI tải lên {path.parent.name}: HTTP {st} {resp[:200]}"); good = False; break
        if not good: continue
        status = "PROCESSING_UPLOAD"
        for _ in range(15):
            time.sleep(4)
            st, r = jcall("https://open.tiktokapis.com/v2/post/publish/status/fetch/", {"publish_id": data["publish_id"]}, auth)
            status = (r.get("data") or {}).get("status", status)
            if status in ("SEND_TO_USER_INBOX", "PUBLISH_COMPLETE", "FAILED"): break
        if status in ("SEND_TO_USER_INBOX", "PUBLISH_COMPLETE"):
            ok += 1; log(f"TikTok: {path.parent.name} — {status}" + (" (vào hộp nháp TikTok)" if status == "SEND_TO_USER_INBOX" else ""))
        else:
            log(f"TikTok LỖI {path.parent.name}: {status} {(r.get('data') or {}).get('fail_reason', '')}")
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True); ap.add_argument("--to", required=True, choices=["facebook", "tiktok"])
    a = ap.parse_args()
    vids = videos(a.dir)
    if not vids: log("Không có video nào để đăng"); return
    n = (to_facebook if a.to == "facebook" else to_tiktok)(vids)
    log(f"Đã đăng {n}/{len(vids)} video lên {a.to}")


if __name__ == "__main__":
    main()
