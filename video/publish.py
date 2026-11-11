#!/usr/bin/env python3
"""Đăng các video đã làm (run/video/<n>/video.mp4 + publish.json) lên Facebook Page hoặc TikTok.

    python video/publish.py --dir run/video --to facebook   # cần FB_PAGE_TOKEN (+ FB_PAGE_ID)
    python video/publish.py --dir run/video --to tiktok     # cần TIKTOK_CLIENT_KEY/SECRET/REFRESH_TOKEN
    python video/publish.py --to tiktok --retry --retry-state state/video-tiktok-retry.json   # gửi bù (cần GH_TOKEN)

Cách đăng giống hai flow ảnh cũ: Facebook hẹn giờ trải đều trong khung giờ của ngày kế tiếp;
TikTok gửi vào hộp nháp (TIKTOK_POST_MODE=inbox) hoặc đăng thẳng (direct).
Lỗi đăng không làm hỏng workflow: video vẫn nằm trong artifact để đăng tay.
TikTok lỗi (vd hộp nháp đầy) + --retry-state: video được ghi lại, lần chạy sau tải artifact về và gửi bù.
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
def tiktok_token():
    ck, cs, rt = (os.environ.get(k, "") for k in ("TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET", "TIKTOK_REFRESH_TOKEN"))
    if not (ck and cs and rt): log("Thiếu TIKTOK_CLIENT_KEY/SECRET/REFRESH_TOKEN: bỏ qua đăng TikTok"); return None
    st, body = call("https://open.tiktokapis.com/v2/oauth/token/", urllib.parse.urlencode(
        {"client_key": ck, "client_secret": cs, "grant_type": "refresh_token", "refresh_token": rt}).encode(),
        {"Content-Type": "application/x-www-form-urlencoded"}, "POST")
    tok = json.loads(body).get("access_token") if st < 300 else None
    if not tok: log("TikTok refresh token lỗi:", body[:300])
    return tok


def tiktok_send(tok, path, meta):
    """Gửi 1 video. Trả về (True, trạng thái) hoặc (False, lỗi)."""
    direct = os.environ.get("TIKTOK_POST_MODE", "inbox").lower() == "direct"
    auth = {"Authorization": "Bearer " + tok}
    size = path.stat().st_size
    chunk = size if size <= 64 * 1024 * 1024 else 32 * 1024 * 1024
    total = -(-size // chunk)
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
        return False, f"khởi tạo HTTP {st} {json.dumps(r.get('error') or r, ensure_ascii=False)[:300]}"
    raw = path.read_bytes()
    for i in range(total):
        a, b = i * chunk, min(size, (i + 1) * chunk) - 1
        st, resp = call(data["upload_url"], raw[a:b + 1], {"Content-Type": "video/mp4", "Content-Length": str(b - a + 1),
                        "Content-Range": f"bytes {a}-{b}/{size}"}, "PUT", 600)
        if st >= 300: return False, f"tải lên HTTP {st} {resp[:200]}"
    status = "PROCESSING_UPLOAD"
    for _ in range(15):
        time.sleep(4)
        st, r = jcall("https://open.tiktokapis.com/v2/post/publish/status/fetch/", {"publish_id": data["publish_id"]}, auth)
        status = (r.get("data") or {}).get("status", status)
        if status in ("SEND_TO_USER_INBOX", "PUBLISH_COMPLETE", "FAILED"): break
    if status in ("SEND_TO_USER_INBOX", "PUBLISH_COMPLETE"):
        return True, status + (" (vào hộp nháp TikTok)" if status == "SEND_TO_USER_INBOX" else "")
    return False, f"{status} {(r.get('data') or {}).get('fail_reason', '')}".strip()


def to_tiktok(vids, retry_file=None, run_id=""):
    """Gửi các video mới. Video lỗi được ghi vào retry_file để lần chạy sau gửi bù (--retry)."""
    tok = tiktok_token()
    ok, failed = 0, []
    for path, meta in vids:
        good, msg = tiktok_send(tok, path, meta) if tok else (False, "không lấy được token TikTok")
        if good: ok += 1; log(f"TikTok: {path.parent.name} — {msg}")
        else: failed.append((path, meta, msg)); log(f"TikTok LỖI {path.parent.name}: {msg}")
    if retry_file and run_id and failed:
        state = load_retry(retry_file)
        for path, meta, msg in failed:
            state["pending"][f"{run_id}/{path.parent.name}"] = {
                "run": run_id, "folder": path.parent.name, "title": (meta.get("title") or "")[:90],
                "since": int(time.time()), "attempts": 1, "last": msg[:300]}
        save_retry(retry_file, state)
        log(f"Đã ghi {len(failed)} video lỗi vào {retry_file}: lần chạy sau gửi bù")
    return ok


# ---------- Gửi bù TikTok ----------
# Video nằm trong artifact "video-tiktok-<run_id>" (giữ 7 ngày), nên chỉ gửi bù trong RETRY_DAYS ngày.
RETRY_DAYS = 6
RETRY_MAX_ATTEMPTS = 6
RETRY_PER_RUN = 2  # hộp nháp TikTok chỉ nhận ~5 bài chờ / 24 giờ: không gửi dồn


def load_retry(f):
    p = Path(f)
    s = json.loads(p.read_text(encoding="utf8")) if p.exists() else {}
    s.setdefault("pending", {}); s.setdefault("done", {})
    return s


def save_retry(f, s):
    cutoff = time.time() - 30 * 86400  # chỉ giữ lịch sử 30 ngày
    s["done"] = {k: v for k, v in s["done"].items() if v.get("at", 0) >= cutoff}
    Path(f).parent.mkdir(parents=True, exist_ok=True)
    Path(f).write_text(json.dumps(s, indent=2, ensure_ascii=False) + "\n", encoding="utf8")


def download_artifact(run_id, dest):
    """Tải artifact video-tiktok-<run_id> bằng gh CLI (có sẵn trên runner, cần GH_TOKEN)."""
    import subprocess
    dest.mkdir(parents=True, exist_ok=True)
    try:
        r = subprocess.run(["gh", "run", "download", str(run_id), "-n", f"video-tiktok-{run_id}", "-D", str(dest)],
                           capture_output=True, text=True)
    except FileNotFoundError:
        log("Không có gh CLI để tải artifact"); return False
    if r.returncode: log(f"Không tải được artifact video-tiktok-{run_id}: {(r.stderr or r.stdout).strip()[:300]}")
    return r.returncode == 0


def set_output(name, value):
    """Ghi output cho bước GitHub Actions (steps.<id>.outputs.<name>)."""
    f = os.environ.get("GITHUB_OUTPUT")
    if f:
        with open(f, "a", encoding="utf8") as fh: fh.write(f"{name}={value}\n")


def retry_tiktok(retry_file, work):
    state = load_retry(retry_file)
    pend = state["pending"]
    if not pend: log("Không có video TikTok nào cần gửi bù"); return
    now = time.time()
    for k, v in list(pend.items()):
        if now - v.get("since", now) > RETRY_DAYS * 86400 or v.get("attempts", 0) >= RETRY_MAX_ATTEMPTS:
            log(f"Bỏ gửi bù {k} sau {v.get('attempts')} lần (artifact sắp hết hạn hoặc lỗi mãi): {v.get('last', '')}")
            state["done"][k] = {**v, "at": int(now), "result": "bỏ: " + v.get("last", "")}
            del pend[k]
    todo = sorted(pend.items(), key=lambda kv: kv[1].get("since", 0))[:RETRY_PER_RUN]
    if todo: set_output("backfill", "true")  # lượt này gửi bù thì không làm video mới
    tok = tiktok_token() if todo else None
    for k, v in todo:
        d = Path(work) / str(v["run"])
        if not (d / v["folder"] / "video.mp4").exists() and not download_artifact(v["run"], d):
            v["attempts"] += 1; v["last"] = "không tải được artifact"; continue
        path, meta_f = d / v["folder"] / "video.mp4", d / v["folder"] / "publish.json"
        if not path.exists() or not meta_f.exists():
            log(f"Artifact {v['run']} không có {v['folder']}/video.mp4: bỏ")
            state["done"][k] = {**v, "at": int(now), "result": "bỏ: artifact không có video"}; del pend[k]; continue
        good, msg = tiktok_send(tok, path, json.loads(meta_f.read_text(encoding="utf8"))) if tok else (False, "không lấy được token TikTok")
        if good:
            log(f"TikTok gửi bù: {k} — {msg}")
            state["done"][k] = {**v, "at": int(time.time()), "result": msg}; del pend[k]
        else:
            v["attempts"] += 1; v["last"] = msg[:300]
            log(f"TikTok gửi bù LỖI {k} (lần {v['attempts']}): {msg}")
    save_retry(retry_file, state)
    if pend: log(f"Còn {len(pend)} video chờ gửi bù ở lần chạy sau")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir"); ap.add_argument("--to", required=True, choices=["facebook", "tiktok"])
    ap.add_argument("--retry-state", help="file JSON ghi video TikTok lỗi để gửi bù (vd state/video-tiktok-retry.json)")
    ap.add_argument("--run-id", default=os.environ.get("GITHUB_RUN_ID", ""))
    ap.add_argument("--retry", action="store_true", help="chỉ gửi bù các video TikTok lỗi ở lần chạy trước")
    a = ap.parse_args()
    if a.retry:
        if a.to != "tiktok" or not a.retry_state: ap.error("--retry cần --to tiktok và --retry-state")
        retry_tiktok(a.retry_state, a.dir or "run/retry"); return
    if not a.dir: ap.error("cần --dir")
    vids = videos(a.dir)
    if not vids: log("Không có video nào để đăng"); return
    n = to_facebook(vids) if a.to == "facebook" else to_tiktok(vids, a.retry_state, a.run_id)
    log(f"Đã đăng {n}/{len(vids)} video lên {a.to}")


if __name__ == "__main__":
    main()
