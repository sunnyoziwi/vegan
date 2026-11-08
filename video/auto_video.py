#!/usr/bin/env python3
"""Tự động làm 1 video tin tức: RSS -> bài + ảnh -> Gemini viết lời & chọn ảnh khớp từng câu -> giọng -> MP4.
Không dùng Claude. Cần biến môi trường GEMINI_API_KEY.

    python video/auto_video.py --out run/video            # chạy thật
    python video/auto_video.py --out run/video --mock video/sample/mock_gemini.json   # thử không gọi Gemini

Danh sách bài đã làm lưu ở state/video.json (workflow commit lại để lần sau không trùng).
"""
import argparse, base64, html, json, os, re, sys, time, urllib.parse, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"
BOT_UA = "VeganVideoBot/0.1 (https://github.com/sunnyoziwi/vegan)"
MODELS = os.environ.get("GEMINI_MODELS", "gemini-3.5-flash-lite,gemini-3.1-flash-lite,gemini-3.6-flash").split(",")
FEEDS = [
    "https://sentientmedia.org/feed/", "https://mercyforanimals.org/feed/", "https://www.farmsanctuary.org/feed/",
    "https://animalequality.org/feed/", "https://plantbasednews.org/feed/", "https://www.veganfoodandliving.com/feed/",
]
ON_TOPIC = re.compile(r"vegan|plant-based|animal|farm|slaughter|meat|dairy|egg|fish|wildlife|sanctuary|rescue|cruelty|foie", re.I)
MAX_AGE_DAYS, MAX_CANDIDATE_IMAGES, MIN_SCENES, MAX_SCENES = 7, 8, 5, 8


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def http(url, data=None, headers=None, tries=5, timeout=60):
    h = {"User-Agent": UA, **(headers or {})}
    for i in range(tries):
        try:
            return urllib.request.urlopen(urllib.request.Request(url, data=data, headers=h), timeout=timeout).read()
        except urllib.error.HTTPError as e:
            if e.code in (429, 503) and i < tries - 1:
                time.sleep(8 * (i + 1)); continue
            raise
        except Exception:
            if i == tries - 1: raise
            time.sleep(3)


def strip_html(s):
    s = re.sub(r"<(script|style)[\s\S]*?</\1>", " ", s, flags=re.I)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


# ---------- 1. chọn bài ----------
def pick_article(seen):
    from email.utils import parsedate_to_datetime
    cands = []
    for feed in FEEDS:
        try:
            xml = http(feed).decode("utf8", "ignore")
        except Exception as e:
            log("bỏ feed", feed, e); continue
        for m in re.finditer(r"<item>[\s\S]*?</item>", xml):
            b = m.group(0)
            t = re.search(r"<title>([\s\S]*?)</title>", b); l = re.search(r"<link>([\s\S]*?)</link>", b)
            d = re.search(r"<pubDate>([\s\S]*?)</pubDate>", b)
            if not (t and l): continue
            title = strip_html(re.sub(r"<!\[CDATA\[|\]\]>", "", t.group(1))); link = l.group(1).strip()
            try: age = (time.time() - parsedate_to_datetime(d.group(1).strip()).timestamp()) / 86400
            except Exception: age = 99
            key = re.sub(r"^https?://(www\.)?|[?#].*$|/+$", "", link).lower()
            if age <= MAX_AGE_DAYS and key not in seen and ON_TOPIC.search(title):
                cands.append((age, title, link, key))
    cands.sort()
    return cands


def fetch_article(link):
    page = http(link).decode("utf8", "ignore")
    og = re.search(r'og:image"[^>]*content="([^"]+)"', page) or re.search(r'content="([^"]+)"[^>]*og:image', page)
    body = re.search(r"<article[\s\S]*?</article>", page)
    scope = body.group(0) if body else page
    text = strip_html(scope)[:9000]
    imgs, bad = [], re.compile(r"logo|avatar|icon|favicon|gravatar|pixel|1x1|\.gif|\.svg|-\d+x\d+\.", re.I)
    urls = ([og.group(1)] if og else []) + re.findall(r'(?:data-src|src)="(https?://[^"]+\.(?:jpe?g|png|webp)[^"]*)"', scope)
    for u in urls:
        u = html.unescape(u)
        if u not in imgs and not (bad.search(u) and u != (og.group(1) if og else "")): imgs.append(u)
    # caption/alt giúp Gemini biết ảnh nói về gì, và loại ảnh của "bài liên quan"
    alts = {}
    for m in re.finditer(r'<img[^>]+(?:data-src|src)="([^"]+)"[^>]*alt="([^"]*)"', scope):
        alts[html.unescape(m.group(1))] = html.unescape(m.group(2))
    return text, imgs[:MAX_CANDIDATE_IMAGES], alts


def download(url, path):
    path.write_bytes(http(url, headers={"User-Agent": BOT_UA if "wikimedia" in url else UA}))
    head = path.read_bytes()[:12]
    ok = head[:3] == b"\xff\xd8\xff" or head[:4] == b"\x89PNG" or (head[:4] == b"RIFF")
    if not ok: path.unlink(); raise ValueError("không phải ảnh")


def commons_search(query, outdir, name):
    """Tìm 1 ảnh Wikimedia Commons theo từ khoá; trả về (đường dẫn, ghi nguồn) hoặc None."""
    u = "https://commons.wikimedia.org/w/api.php?" + urllib.parse.urlencode({
        "action": "query", "format": "json", "generator": "search", "gsrsearch": query + " filetype:bitmap",
        "gsrnamespace": 6, "gsrlimit": 4, "prop": "imageinfo", "iiprop": "url|extmetadata|size", "iiurlwidth": 1600})
    r = json.loads(http(u, headers={"User-Agent": BOT_UA}))
    for p in sorted(r.get("query", {}).get("pages", {}).values(), key=lambda p: p["index"]):
        ii = p["imageinfo"][0]; lic = ii["extmetadata"].get("LicenseShortName", {}).get("value", "")
        if ii["width"] < 1000 or not re.search(r"CC|public|PD|No restrictions", lic, re.I): continue
        time.sleep(4)
        path = outdir / f"{name}.jpg"
        try: download(ii["thumburl"], path)
        except Exception: continue
        return path, f"{p['title']} — {lic}"
    return None


# ---------- 2. Gemini ----------
def gemini(parts, schema_hint=None):
    key = os.environ["GEMINI_API_KEY"]
    last = None
    for model in MODELS:
        for attempt in range(3):
            body = {"contents": [{"role": "user", "parts": parts}],
                    "generationConfig": {"responseMimeType": "application/json", "temperature": 0.4}}
            try:
                raw = http(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                           data=json.dumps(body).encode(), headers={"Content-Type": "application/json", "x-goog-api-key": key},
                           tries=1, timeout=120)
                txt = json.loads(raw)["candidates"][0]["content"]["parts"][0]["text"]
                return json.loads(re.sub(r"^```(?:json)?|```$", "", txt.strip(), flags=re.M).strip())
            except urllib.error.HTTPError as e:
                msg = e.read().decode()[:300]; last = f"{model}: HTTP {e.code} {msg}"
                if e.code == 402: sys.exit("Gemini hết credit (402): " + msg)
                if e.code in (401, 403): sys.exit("Gemini từ chối key: " + msg)
                if e.code == 429 or e.code >= 500: time.sleep(10 * (attempt + 1)); continue
                break
            except Exception as e:
                last = f"{model}: {e}"; time.sleep(3)
    sys.exit(f"Gemini lỗi: {last}")


def img_part(path):
    mime = "image/png" if path.read_bytes()[:4] == b"\x89PNG" else "image/jpeg"
    return {"inlineData": {"mimeType": mime, "data": base64.b64encode(path.read_bytes()).decode()}}


def write_story(title, text, cands, alts):
    """Gemini nhìn các ảnh ứng viên + đọc bài, viết lời từng cảnh và chọn ảnh khớp từng câu."""
    parts = [{"text": (
        "Bạn là biên tập viên một kênh tin về ăn chay và quyền động vật. Biến bài báo thành video ngắn dọc, kể chuyện nhẹ nhàng, nhân ái.\n"
        f"Viết {MIN_SCENES}-{MAX_SCENES} cảnh. Mỗi cảnh = 1 câu thoại TIẾNG VIỆT (tối đa 28 từ, đọc thành tiếng được, có dấu đầy đủ) + 1 ảnh.\n"
        "QUY TẮC CỨNG:\n"
        "1. Chỉ dùng sự thật có trong bài. Không bịa số liệu, tên, trích dẫn. Không mô tả cảnh máu me.\n"
        "2. ẢNH PHẢI MINH HOẠ ĐÚNG CÂU THOẠI. Lời nói về Washington thì ảnh phải là Washington; nói về chữ ký thì ảnh phải có chữ ký. "
        "Không dùng ảnh chỉ 'có vẻ liên quan'.\n"
        "3. Với mỗi cảnh, nếu một trong các ảnh ứng viên (đánh số IMG0, IMG1...) minh hoạ đúng câu đó thì dùng: \"image\": \"IMGn\". "
        "Ảnh đi kèm bài có thể thuộc 'bài liên quan' và lạc đề: hãy NHÌN ảnh trước khi chọn.\n"
        "4. Nếu không ảnh nào khớp, đặt \"image\": null và \"query\": từ khoá tiếng Anh ngắn, cụ thể, tả cảnh vật thật "
        "(địa danh, vật thể, con vật) để tìm ảnh Wikimedia Commons. Không tìm ảnh người nổi tiếng hay cảnh đau đớn.\n"
        "5. Một ảnh dùng tối đa 1 lần. Cảnh đầu là câu mở đầu hấp dẫn; cảnh cuối chốt lại thông điệp.\n"
        'Trả về JSON: {"title_vi": str, "caption_vi": str (≤2 câu cho bài đăng), "hashtags": [str], '
        '"scenes": [{"text": str, "image": "IMGn"|null, "query": str|null, "why": "vì sao ảnh khớp câu"}]}\n\n'
        f"TIÊU ĐỀ: {title}\nNỘI DUNG BÀI:\n{text}\n\nCÁC ẢNH ỨNG VIÊN (mô tả alt nếu có):")}]
    for i, (p, u) in enumerate(cands):
        parts += [{"text": f"IMG{i} — alt: {alts.get(u, '(không có)')[:120]}"}, img_part(p)]
    return gemini(parts)


def verify(scenes, paths):
    """Lượt 2: Gemini rà từng cặp (câu thoại, ảnh); trả về chỉ số các cảnh bị lệch."""
    parts = [{"text": (
        "Kiểm tra từng cặp LỜI THOẠI + ẢNH. Ảnh có minh hoạ ĐÚNG ý của lời thoại không (đúng địa điểm/vật thể/con vật/sự việc được nhắc)? "
        "Ảnh chỉ 'cùng chủ đề chung' thì trả false. Ảnh có chữ chính trị, người nổi tiếng không liên quan, hoặc cảnh đau đớn/máu me thì false.\n"
        'Trả JSON: {"results": [{"i": số cảnh, "match": true|false, "reason": str}]}')}]
    for i, (s, p) in enumerate(zip(scenes, paths)):
        parts += [{"text": f"CẢNH {i}: «{s['text']}»"}, img_part(p)]
    r = gemini(parts)
    return [x["i"] for x in r["results"] if not x.get("match")]


# ---------- main ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True); ap.add_argument("--mock"); ap.add_argument("--link")
    a = ap.parse_args()
    out = Path(a.out); img = out / "img"; img.mkdir(parents=True, exist_ok=True)
    state_f = ROOT / "state" / "video.json"
    state = json.loads(state_f.read_text(encoding="utf8")) if state_f.exists() else {"seen": {}}
    if not a.mock and not os.environ.get("GEMINI_API_KEY"): sys.exit("Thiếu GEMINI_API_KEY")

    if a.link: title, link, key = a.link, a.link, re.sub(r"^https?://(www\.)?|[?#].*$|/+$", "", a.link).lower()
    else:
        cands = pick_article(state["seen"])
        if not cands: sys.exit("Không có bài mới phù hợp.")
        _, title, link, key = cands[0]
    log("Bài:", title, link)
    text, urls, alts = fetch_article(link)
    cand = []
    for i, u in enumerate(urls):
        p = img / f"cand{i}.jpg"
        try: download(u, p); cand.append((p, u))
        except Exception as e: log("bỏ ảnh", u, e)
    log(f"{len(cand)} ảnh ứng viên từ bài")

    story = json.loads(Path(a.mock).read_text(encoding="utf8")) if a.mock else write_story(title, text, cand, alts)
    scenes, paths, credits = story["scenes"], [], []
    for n in range(2):  # tối đa 1 lần sửa ảnh lệch
        paths, credits = [], []
        for i, s in enumerate(scenes):
            m = re.fullmatch(r"IMG(\d+)", s.get("image") or "")
            if m and int(m.group(1)) < len(cand) and s.get("image") not in [x.get("image") for x in scenes[:i]]:
                paths.append(cand[int(m.group(1))][0]); credits.append(f"cảnh {i}: ảnh trong bài")
            elif s.get("query"):
                got = commons_search(s["query"], img, f"web{i}")
                if not got: log(f"cảnh {i}: không tìm được ảnh cho '{s['query']}'"); paths.append(None); credits.append(""); continue
                paths.append(got[0]); credits.append(f"cảnh {i}: {got[1]}")
            else: paths.append(None); credits.append("")
        good = [(s, p, c) for s, p, c in zip(scenes, paths, credits) if p]
        if len(good) < MIN_SCENES: sys.exit("Không đủ cảnh có ảnh khớp; bỏ bài này.")
        scenes, paths, credits = [g[0] for g in good], [g[1] for g in good], [g[2] for g in good]
        if a.mock: break
        bad = verify(scenes, paths)
        if not bad: break
        log("Cảnh lệch ảnh:", bad)
        if n == 1:  # sau lần sửa vẫn lệch -> bỏ cảnh đó
            keep = [i for i in range(len(scenes)) if i not in bad]
            scenes, paths, credits = [scenes[i] for i in keep], [paths[i] for i in keep], [credits[i] for i in keep]
            if len(scenes) < MIN_SCENES: sys.exit("Quá nhiều cảnh lệch ảnh; bỏ bài này.")
        else:
            for i in bad:  # ép tìm ảnh web cho cảnh lệch
                scenes[i]["image"] = None
                scenes[i]["query"] = scenes[i].get("query") or " ".join(re.findall(r"[A-Za-z]{4,}", scenes[i]["text"]))[:60] or title
    final = {"lang": "vi", "voice": "vi-VN-HoaiMyNeural", "width": 1080, "height": 1920, "source": link,
             "scenes": [{"text": s["text"], "image": str(p.relative_to(out)).replace("\\", "/")} for s, p in zip(scenes, paths)]}
    (out / "story.json").write_text(json.dumps(final, ensure_ascii=False, indent=2), encoding="utf8")
    (out / "PUBLISH.txt").write_text(
        f"{story.get('title_vi', title)}\n\n{story.get('caption_vi', '')}\n\n{' '.join('#' + h.lstrip('#') for h in story.get('hashtags', []))}\n\n"
        f"Nguồn: {link}\n\nẢnh:\n" + "\n".join(credits), encoding="utf8")
    sys.path.insert(0, str(Path(__file__).parent))
    import make_video
    make_video.main(str(out / "story.json"), str(out / "video.mp4"))
    if not a.mock and not a.link:
        state["seen"][key] = int(time.time() * 1000)
        state_f.parent.mkdir(exist_ok=True); state_f.write_text(json.dumps(state, indent=2), encoding="utf8")


if __name__ == "__main__":
    main()
