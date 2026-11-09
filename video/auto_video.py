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
MAX_AGE_DAYS, MAX_CANDIDATE_IMAGES, MIN_SCENES, MAX_SCENES = 7, 8, 4, 8


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
    # Trang có nhiều thẻ <article> (thẻ "bài liên quan" cũng là <article>): lấy khối dài nhất = bài chính.
    for tag in ("nav", "header", "footer", "aside", "form", "noscript"):
        page = re.sub(rf"<{tag}[\s>][\s\S]*?</{tag}>", " ", page, flags=re.I)
    blocks = re.findall(r"<article[\s\S]*?</article>", page)
    scope = max(blocks, key=len) if blocks else page
    if len(strip_html(scope)) < 1500:
        scope = page
    text = strip_html(scope)[:12000]
    imgs, bad = [], re.compile(r"logo|avatar|icon|favicon|gravatar|pixel|1x1|ytimg|\.gif|\.svg|-\d+x\d+\.", re.I)
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


def commons_candidates(query, outdir, name, n=5):
    """Tìm tối đa n ảnh Wikimedia Commons (giấy phép mở) theo từ khoá; trả về [(đường dẫn, ghi nguồn)]."""
    # Commons không trả kết quả cho câu dài: thử từ khoá đầy đủ, rồi rút xuống 3 và 2 từ đầu.
    words = re.sub(r"[^\w\s-]", " ", query).split()
    pages = {}
    for q in dict.fromkeys([" ".join(words), " ".join(words[:3]), " ".join(words[:2])]):
        if not q: continue
        u = "https://commons.wikimedia.org/w/api.php?" + urllib.parse.urlencode({
            "action": "query", "format": "json", "generator": "search", "gsrsearch": q + " filetype:bitmap",
            "gsrnamespace": 6, "gsrlimit": 12, "prop": "imageinfo", "iiprop": "url|extmetadata|size", "iiurlwidth": 1200})
        pages = json.loads(http(u, headers={"User-Agent": BOT_UA})).get("query", {}).get("pages", {})
        if pages: break
        time.sleep(1)
    out = []
    for p in sorted(pages.values(), key=lambda p: p["index"]):
        if len(out) >= n: break
        ii = p["imageinfo"][0]; lic = ii["extmetadata"].get("LicenseShortName", {}).get("value", "")
        if ii["width"] < 1000 or not re.search(r"CC|public|PD|No restrictions", lic, re.I): continue
        if re.search(r"\.(tif|tiff|svg|pdf|djvu)$", p["title"], re.I): continue
        time.sleep(3)
        path = outdir / f"{name}_{len(out)}.jpg"
        try: download(ii["thumburl"], path)
        except Exception: continue
        out.append((path, f"{p['title']} — {lic}"))
    return out



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
        "Chỉ viết những câu có thể minh hoạ bằng địa điểm/toà nhà/vật/con vật THẬT chụp được; ý trừu tượng (số liệu, quy trình, "
        "pháp lý) hãy gắn với một hình đại diện hợp lý (toà nhà chính quyền, hòm phiếu, con vịt, búa quan toà). "
        "Không dùng ảnh chỉ 'có vẻ liên quan'.\n"
        "3. Với mỗi cảnh, nếu một trong các ảnh ứng viên (đánh số IMG0, IMG1...) minh hoạ đúng câu đó thì dùng: \"image\": \"IMGn\". "
        "Ảnh đi kèm bài có thể thuộc 'bài liên quan' và lạc đề: hãy NHÌN ảnh trước khi chọn.\n"
        "4. Nếu không ảnh nào khớp, đặt \"image\": null và \"query\": 2-3 TỪ KHOÁ tiếng Anh (không viết thành câu), đúng tên địa danh/vật/con vật thật, ví dụ 'Denver City County Building', 'mallard duck' "
        "(địa danh, vật thể, con vật) để tìm ảnh Wikimedia Commons. Không tìm ảnh người nổi tiếng hay cảnh đau đớn.\n"
        "5. Một ảnh dùng tối đa 1 lần. Cảnh đầu là câu mở đầu hấp dẫn; cảnh cuối chốt lại thông điệp.\n"
        'Trả về JSON: {"title_vi": str, "caption_vi": str (≤2 câu cho bài đăng), "hashtags": [str], '
        '"scenes": [{"text": str, "image": "IMGn"|null, "query": str|null, "why": "vì sao ảnh khớp câu"}]}\n\n'
        f"TIÊU ĐỀ: {title}\nNỘI DUNG BÀI:\n{text}\n\nCÁC ẢNH ỨNG VIÊN (mô tả alt nếu có):")}]
    for i, (p, u) in enumerate(cands):
        parts += [{"text": f"IMG{i} — alt: {alts.get(u, '(không có)')[:120]}"}, img_part(p)]
    return gemini(parts)


def choose_image(sentence, cands):
    """Gemini nhìn các ảnh ứng viên và chọn ảnh khớp nhất với câu thoại; pick=-1 nếu không ảnh nào khớp."""
    parts = [{"text": (
        "Chọn ảnh minh hoạ cho MỘT câu thoại trong video tin tức về động vật/ăn chay.\n"
        "Ảnh KHỚP khi nó thể hiện đúng ĐỐI TƯỢNG, ĐỊA ĐIỂM hoặc VẬT được nhắc tới (đúng thành phố/toà nhà/con vật/đồ vật), "
        "hoặc là hình đại diện hợp lý cho ý đó. KHÔNG cần minh hoạ hành động khó chụp (ví dụ quy trình ép ăn): ảnh con vật được nhắc là đủ. "
        "Ảnh sai địa điểm/sai vật (ví dụ Copenhagen khi nói Washington, sách cổ khi nói ngày tháng), có chữ chính trị, "
        "người nổi tiếng không liên quan, cảnh đau đớn/máu me, hoặc ẢNH MÓN ĂN LÀM TỪ THỊT/GAN/ĐỘNG VẬT (kênh này ủng hộ động vật) thì KHÔNG khớp.\n"
        'Trả JSON: {"pick": chỉ số ảnh khớp nhất hoặc -1, "reason": str, "better_query": từ khoá tiếng Anh cụ thể hơn nếu pick=-1, nếu không thì null}\n'
        f"CÂU THOẠI: «{sentence}»")}]
    for i, (p, c) in enumerate(cands):
        parts += [{"text": f"ẢNH {i}:"}, img_part(p)]
    time.sleep(2)
    try:
        r = gemini(parts)
        r["pick"] = int(r.get("pick", -1))
        if r["pick"] >= len(cands): r["pick"] = -1
        return r
    except SystemExit:
        raise
    except Exception:
        return {"pick": -1, "reason": "lỗi đọc trả lời"}


# ---------- main ----------
class Skip(Exception):
    """Bài này không làm được (thiếu ảnh khớp...): bỏ, thử bài khác."""


def make_one(title, link, out, mock):
    """Làm 1 video từ 1 bài, ghi vào thư mục out. Ném Skip nếu bài không đủ cảnh có ảnh khớp."""
    img = out / "img"; img.mkdir(parents=True, exist_ok=True)
    log("Bài:", title, link)
    text, urls, alts = fetch_article(link)
    cand = []
    for i, u in enumerate(urls):
        p = img / f"cand{i}.jpg"
        try: download(u, p); cand.append((p, u))
        except Exception as e: log("bỏ ảnh", u, e)
    log(f"{len(cand)} ảnh ứng viên từ bài")

    story = json.loads(Path(mock).read_text(encoding="utf8")) if mock else write_story(title, text, cand, alts)
    if not mock and len(story.get("scenes", [])) < MIN_SCENES + 1:  # Gemini đôi khi viết thiếu cảnh: cho viết lại 1 lần
        log(f"Gemini mới viết {len(story.get('scenes', []))} cảnh, thử lại")
        again = write_story(title, text, cand, alts)
        if len(again.get("scenes", [])) > len(story.get("scenes", [])): story = again
    scenes, paths, credits = story["scenes"], [], []
    final_scenes = []
    for i, s in enumerate(scenes):
        sel, query = None, s.get("query")
        m = re.fullmatch(r"IMG(\d+)", s.get("image") or "")
        if m and int(m.group(1)) < len(cand):
            c = [(cand[int(m.group(1))][0], "ảnh trong bài")]
            r = {"pick": 0} if mock else choose_image(s["text"], c)
            if r.get("pick", -1) == 0:
                sel = c[0]
            else:
                log(f"  cảnh {i}: ảnh trong bài không khớp — {r.get('reason', '')[:120]}")
                query = query or r.get("better_query")
        for attempt in range(2):
            if sel or not query: break
            cs = commons_candidates(query, img, f"w{i}_{attempt}")
            if not cs: log(f"  cảnh {i}: không có ảnh cho «{query}»"); break
            r = {"pick": 0} if mock else choose_image(s["text"], cs)
            if r.get("pick", -1) >= 0:
                sel = cs[r["pick"]]; log(f"  cảnh {i}: «{query}» -> {sel[1]}"); break
            log(f"  cảnh {i}: «{query}» không có ảnh khớp — {r.get('reason', '')[:120]}")
            query = r.get("better_query")
        if sel:
            final_scenes.append(s); paths.append(sel[0]); credits.append(f"cảnh {len(paths) - 1}: {sel[1]}")
        else:
            log(f"  cảnh {i}: BỎ — «{s['text'][:60]}»")
    scenes = final_scenes
    if len(scenes) < MIN_SCENES: raise Skip(f"Chỉ {len(scenes)} cảnh có ảnh khớp (cần ≥{MIN_SCENES}); bỏ bài này.")
    final = {"lang": "vi", "width": 1080, "height": 1920, "source": link,
             "scenes": [{"text": s["text"], "image": str(p.relative_to(out)).replace("\\", "/")} for s, p in zip(scenes, paths)]}
    (out / "story.json").write_text(json.dumps(final, ensure_ascii=False, indent=2), encoding="utf8")
    (out / "PUBLISH.txt").write_text(
        f"{story.get('title_vi', title)}\n\n{story.get('caption_vi', '')}\n\n{' '.join('#' + h.lstrip('#') for h in story.get('hashtags', []))}\n\n"
        f"Nguồn: {link}\n\nẢnh:\n" + "\n".join(credits), encoding="utf8")
    (out / "publish.json").write_text(json.dumps(
        {"title": story.get("title_vi", title), "caption": story.get("caption_vi", ""), "hashtags": story.get("hashtags", []), "source": link},
        ensure_ascii=False, indent=2), encoding="utf8")
    sys.path.insert(0, str(Path(__file__).parent))
    import make_video
    make_video.main(str(out / "story.json"), str(out / "video.mp4"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True); ap.add_argument("--mock"); ap.add_argument("--link")
    ap.add_argument("--count", type=int, default=1, help="số video làm trong 1 lần chạy (mỗi video 1 bài khác nhau)")
    ap.add_argument("--state", default="video", help="tên file trạng thái trong state/ (mỗi luồng 1 file riêng)")
    a = ap.parse_args()
    out = Path(a.out)
    if not a.mock and not os.environ.get("GEMINI_API_KEY"): sys.exit("Thiếu GEMINI_API_KEY")
    if a.link:  # thử 1 bài cụ thể, không ghi trạng thái
        make_one(a.link, a.link, out, a.mock); return
    state_f = ROOT / "state" / f"{a.state}.json"
    state = json.loads(state_f.read_text(encoding="utf8")) if state_f.exists() else {"seen": {}}
    done = 0
    for _, title, link, key in pick_article(state["seen"]):
        if done >= a.count: break
        log(f"=== Video {done + 1}/{a.count}: {title}")
        try:
            make_one(title, link, out / str(done + 1), a.mock)
            done += 1
        except (Skip, Exception) as e:  # bài lỗi/thiếu ảnh: bỏ, thử bài kế tiếp (SystemExit như hết quota thì dừng hẳn)
            log("Bỏ bài:", e)
        state["seen"][key] = int(time.time() * 1000)  # kể cả bài bị bỏ, để lần sau không thử lại mãi
        state_f.parent.mkdir(exist_ok=True); state_f.write_text(json.dumps(state, indent=2), encoding="utf8")
    log(f"Xong {done}/{a.count} video")
    if done == 0: sys.exit("Không làm được video nào.")


if __name__ == "__main__":
    main()
