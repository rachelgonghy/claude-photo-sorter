"""Turn the sorted iCloud library into a digital scrapbook.  Mac only, after icloud_albums.py has run.

  .venv/bin/python scripts/scrapbook.py              # build work/scrapbook/site/
  .venv/bin/python scripts/scrapbook.py --preview    # build, then open it in the browser
  .venv/bin/python scripts/scrapbook.py --title "The Tan family scrapbook"
  .venv/bin/python scripts/scrapbook.py --look rich      # finish: natural (default), rich or none

Chapters: one per trip (a page per place), plus one "at home" chapter per year (a page per month).
Claude picks the best photos for each page from the previews (sharp, well composed, people, the
place itself, no near-copies) and writes a short caption for some of them and an intro per chapter.
Only the picked photos are fetched from Photos, resized to 1600px and saved WITHOUT their metadata
(no GPS, no camera details), into work/scrapbook/site/images/, with a gentle editorial finish
(balanced exposure, a little more colour and warmth, light sharpening). Originals are never changed. Videos, screenshots, documents,
duplicates and "maybe delete" photos are left out. Runs again resume: picks and photos are kept.

The site folder is private data: it lives in work/ (never committed). To share it with family,
ask Claude Code on this Mac to "publish my scrapbook" (see CLAUDE.md): it becomes a private
claude.ai page, and you choose who to share it with from its Share menu.
"""
import argparse, hashlib, json, math, os, re, shutil, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass

sys.path.insert(0, os.path.dirname(__file__))
from common import DATA, THUMBS, WORK, load, save
import icloud_albums as ia

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(WORK, "scrapbook")
SITE = os.path.join(OUT, "site")
IMAGES = os.path.join(SITE, "images")
RAW = os.path.join(OUT, "raw")              # web-size copies before the finish, so a new --look needs no re-download
SHEETS = os.path.join(OUT, "sheets")
PICKS = os.path.join(OUT, "picks.json")     # cache: page key -> {id: caption}
INTROS = os.path.join(OUT, "intros.json")   # cache: chapter id -> intro
LEAVE_OUT = {"docs", "memes", "screenshots"}  # never in a scrapbook
MAX_PHOTOS = 450                             # a published page holds about 500 files
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December"]

# Photos' own export of the current version (a JPEG for photos), for pictures already on the Mac.
EXPORT_JPEG = """
on run argv
  set n to (count of argv) div 2
  tell application "Photos"
    with timeout of 1800 seconds
      repeat with i from 1 to n
        try
          export {media item id (item (n + i) of argv)} to ((POSIX file (item i of argv)) as alias)
        end try
      end repeat
    end timeout
  end tell
end run
"""


def dt(t):
    return datetime.fromtimestamp(t, timezone.utc)


def span(ts):
    a, b = dt(min(ts)), dt(max(ts))
    if (a.year, a.month, a.day) == (b.year, b.month, b.day):
        return f"{a.day} {a:%b %Y}"
    if (a.year, a.month) == (b.year, b.month):
        return f"{a.day}–{b.day} {a:%b %Y}"
    if a.year == b.year:
        return f"{a.day} {a:%b} – {b.day} {b:%b %Y}"
    return f"{a.day} {a:%b %Y} – {b.day} {b:%b %Y}"


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:48] or "chapter"


# ---------- choosing ----------

def sheet(ids, path):
    """A contact sheet of up to 30 previews, each tagged with its id (like sheets.py)."""
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 22)
    except OSError:
        font = ImageFont.load_default()
    C = 230
    im = Image.new("RGB", (6 * C, 5 * C), "white")
    d = ImageDraw.Draw(im)
    for k, i in enumerate(ids):
        x, y = (k % 6) * C, (k // 6) * C
        im.paste(Image.open(os.path.join(THUMBS, f"{i}.jpg")).convert("RGB").resize((C - 8, C - 8)), (x + 4, y + 4))
        d.rectangle((x + 4, y + 4, x + 14 + 13 * len(str(i)), y + 32), fill="black")
        d.text((x + 8, y + 6), str(i), fill="yellow", font=font)
    im.save(path, quality=82)


def pick(key, ids, k, what):
    """Claude picks the k best of these ids for a page; returns {id: caption}. Cached by key and ids."""
    cache = load(PICKS, {})
    ck = key + ":" + hashlib.sha1(",".join(map(str, ids)).encode()).hexdigest()[:10] + f":{k}"
    if ck in cache:
        return {int(i): c for i, c in cache[ck].items()}
    if len(ids) <= k:
        out = {i: "" for i in ids}
    else:
        os.makedirs(SHEETS, exist_ok=True)
        paths = []
        for s in range(0, len(ids), 30):
            p = os.path.join(SHEETS, f"{hashlib.sha1((ck + str(s)).encode()).hexdigest()[:12]}.jpg")
            sheet(ids[s:s + 30], p)
            paths.append(p)
        ans = ia.ask_claude(
            "You are making a family scrapbook. Use the Read tool to look at each of these contact sheets:\n"
            + "\n".join(paths) + "\n\n"
            f"They show the photos from {what}, each tagged in yellow with an id. Pick the {k} photos that "
            "belong in a beautiful scrapbook: sharp and well composed, people's faces and happy moments, the "
            "place itself (views, streets, food that looks good), and a variety of scenes. Never pick two "
            "near-identical shots, blurry or accidental ones, or screenshots.\n\n"
            "For about half of your picks, write a caption the way a good travel and family magazine would: "
            "2 to 7 words, warm and evocative, about what is visible (\"Lanterns at dusk\", \"Slow breakfast by "
            "the pool\", \"First swim of the trip\"). Don't invent names, places or events; use an empty caption "
            "when unsure.\n\n"
            f"Reply with only a JSON object mapping each picked id to its caption, exactly {k} entries, like "
            "{\"12\": \"Lanterns at dusk\", \"40\": \"\"}.") or {}
        valid = set(ids)
        out = {}
        for i, c in ans.items():
            try:
                i = int(i)
            except ValueError:
                continue
            if i in valid:
                out[i] = str(c).strip().rstrip(".")[:60]
        if not out:  # Claude didn't answer (limit, not signed in): don't cache, try again next run
            return None
    cache[ck] = {str(i): c for i, c in out.items()}
    save(PICKS, cache)
    return out


def intro(ch):
    """The chapter's opening line, plus one highlight line per place for trips. Cached."""
    cache = load(INTROS, {})
    if ch["id"] in cache and isinstance(cache[ch["id"]], dict):
        return cache[ch["id"]]
    lines = "\n".join(f"- {pg['heading']}: " + ("; ".join(p["caption"] for p in pg["photos"] if p["caption"]) or "no captions")
                      for pg in ch["pages"])
    trip = ch["kind"] == "trip"
    ans = ia.ask_claude(
        "You write for a family travel scrapbook, in the voice of a thoughtful travel and family magazine: warm, "
        "calm, specific, never gushing. Chapter: "
        f"{ch['title']}, {ch['dates']}. Its {'places' if trip else 'months'}, with the captions of the photos in each:\n{lines}\n\n"
        "Write \"intro\": one or two sentences, at most 30 words, that open the chapter."
        + (" Also write one highlight line per place, at most 12 words, saying what that place was about in these "
           "photos (\"Temple gates, bamboo paths and slow mornings\")." if trip and len(ch["pages"]) > 1 else "")
        + " Use only what the captions and place names tell you: no names, no invented events, no exclamation "
        "marks.\n\nReply with only a JSON object like {\"intro\": \"...\""
        + (", \"" + ch["pages"][0]["heading"] + "\": \"...\"" if trip and len(ch["pages"]) > 1 else "")
        + "}. Do not use any tools.", timeout=300) or {}
    out = {"intro": str(ans.get("intro", "")).strip()}
    for pg in ch["pages"]:
        if trip and len(ch["pages"]) > 1 and ans.get(pg["heading"]):
            out[pg["heading"]] = str(ans[pg["heading"]]).strip().rstrip(".")
    if out["intro"]:
        cache[ch["id"]] = out
        save(INTROS, cache)
    return out


# ---------- the finish ----------

LOOKS = {"none": None,
         "natural": dict(levels=.5, contrast=1.04, color=1.06, warm=1.02, sharpen=50),
         "rich": dict(levels=.8, contrast=1.08, color=1.12, warm=1.035, sharpen=70)}


def finish(im, look):
    """An editorial grade, the same for every photo so the book feels like one set. Never changes content."""
    L = LOOKS[look]
    if not L:
        return im
    auto = ImageOps.autocontrast(im, cutoff=(.4, .6))          # balance exposure without clipping
    im = Image.blend(im, auto, L["levels"])
    im = ImageEnhance.Contrast(im).enhance(L["contrast"])
    im = ImageEnhance.Color(im).enhance(L["color"])
    r, g, b = im.split()                                         # a touch of warm light
    w = L["warm"]
    im = Image.merge("RGB", (r.point(lambda v: min(255, int(v * w))), g, b.point(lambda v: int(v / w))))
    return im.filter(ImageFilter.UnsharpMask(radius=1.2, percent=L["sharpen"], threshold=3))


def finish_all(ids, look):
    os.makedirs(IMAGES, exist_ok=True)
    mark = os.path.join(OUT, "look.txt")
    redo = (open(mark).read().strip() if os.path.exists(mark) else "") != look
    for i in ids:
        src, dst = os.path.join(RAW, f"{i}.jpg"), os.path.join(IMAGES, f"{i}.jpg")
        if os.path.exists(src) and (redo or not os.path.exists(dst)):
            with Image.open(src) as im:
                finish(im.convert("RGB"), look).save(dst, "JPEG", quality=84, optimize=True, progressive=True)
    with open(mark, "w") as f:
        f.write(look)


# ---------- fetching the picked photos ----------

def fetch(picks, byid):
    """Web-size JPEGs without metadata in site/images/<id>.jpg. Photos first, PhotoKit for iCloud-only ones."""
    os.makedirs(RAW, exist_ok=True)
    todo = [i for i in picks if not os.path.exists(os.path.join(RAW, f"{i}.jpg"))]
    if not todo:
        return
    print(f"Getting {len(todo)} photos from Photos (web size, without location data)...", flush=True)
    tmp = os.path.join(OUT, "dl")
    for s in range(0, len(todo), 20):
        chunk = todo[s:s + 20]
        shutil.rmtree(tmp, ignore_errors=True)
        dirs = [os.path.join(tmp, str(i)) for i in chunk]
        for d in dirs:
            os.makedirs(d)
        pids = [byid[i]["photos_id"] for i in chunk]
        try:
            ia.osa(EXPORT_JPEG, dirs + pids)
        except Exception:
            pass
        empty = [(byid[i]["photos_id"], d) for i, d in zip(chunk, dirs) if not ia.pick_file(d)]
        if empty:
            ia.wait_for_files([d for _, d in empty], 10)
            empty = [(p, d) for p, d in empty if not ia.pick_file(d)]
        if empty:
            ia.download_icloud(empty, {})
        for i, d in zip(chunk, dirs):
            f = ia.pick_file(d)
            if not f:
                print(f"  could not get photo {i}; it is left out", flush=True)
                continue
            with Image.open(f) as im:
                im = ImageOps.exif_transpose(im).convert("RGB")
                im.thumbnail((1600, 1600))
                im.save(os.path.join(RAW, f"{i}.jpg"), "JPEG", quality=90)  # no exif passed: location data is dropped
        shutil.rmtree(tmp, ignore_errors=True)
        print(f"PROGRESS {min(s + 20, len(todo))} {len(todo)}", flush=True)


# ---------- building ----------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--title", default="Our Scrapbook")
    ap.add_argument("--preview", action="store_true", help="open the finished scrapbook in the browser")
    ap.add_argument("--look", choices=list(LOOKS), default="natural", help="photo finish (default natural)")
    args = ap.parse_args()
    if sys.platform != "darwin":
        sys.exit("This needs the Photos app, so it only runs on a Mac.")

    inv = load(os.path.join(DATA, "inventory.json"), [])
    ic = load(os.path.join(DATA, "icloud.json"))
    labels = load(os.path.join(DATA, "labels.json"), {})
    if not inv or ic is None:
        sys.exit("Sort the library first:  .venv/bin/python scripts/icloud_albums.py")
    dupes = {d for g in load(os.path.join(DATA, "dupes.json"), []) for d in g["dupes"]}
    bad, gone, trips = {int(k) for k in ic.get("bad", {})}, set(ic.get("gone", [])), ic.get("trips", {})

    print("Reading dates from Photos...", flush=True)
    meta = ia.read_meta()
    byid = {r["id"]: r for r in inv}
    when = {r["id"]: meta[r["photos_id"]][0] for r in inv if r["photos_id"] in meta}
    ok = [r["id"] for r in inv if r["kind"] == "photo" and r["id"] in when and "error" not in r
          and r["id"] not in dupes | bad | gone and labels.get(str(r["id"])) not in LEAVE_OUT
          and r.get("ext") != ".png"]
    print(f"{len(ok)} photos can go in the scrapbook.", flush=True)

    # chapters: trips (pages = places) and years at home (pages = months)
    chapters = {}
    for i in ok:
        t = trips.get(str(i))
        if t:
            name, _, stop = t.partition("/")
            title, _, w = name.rpartition(", ")
            ch = chapters.setdefault("t:" + name, {"kind": "trip", "title": title or name, "when": w, "pages": {}})
            ch["pages"].setdefault(stop or title or name, []).append(i)
        else:
            d = dt(when[i])
            ch = chapters.setdefault(f"y:{d.year}", {"kind": "year", "title": f"{d.year} at home", "when": str(d.year), "pages": {}})
            ch["pages"].setdefault(MONTHS[d.month - 1], []).append(i)
    for ch in chapters.values():
        n = sum(len(v) for v in ch["pages"].values())
        ch["n"] = n
        ch["target"] = min(36, max(6, round(n ** 0.5 * 2.2))) if ch["kind"] == "trip" else min(48, max(8, round(n ** 0.5 * 2)))
    total = sum(ch["target"] for ch in chapters.values())
    scale = min(1, MAX_PHOTOS / total) if total else 1

    out_ch, waiting = [], 0
    order = sorted(chapters.items(), key=lambda kv: min(when[i] for v in kv[1]["pages"].values() for i in v))
    print(f"Claude is choosing photos for {len(order)} chapters...", flush=True)
    jobs = []
    for key, ch in order:
        target = max(4, round(ch["target"] * scale))
        for heading, ids in ch["pages"].items():
            ids = sorted(ids, key=lambda i: when[i])
            want = max(1, round(target * len(ids) / ch["n"]))
            for s in range(0, len(ids), 90):  # at most 90 previews per question
                g = ids[s:s + 90]
                jobs.append((key, heading, g, max(1, round(want * len(g) / len(ids))),
                             f"{'a trip to ' + ch['title'] if ch['kind'] == 'trip' else 'everyday life at home in ' + ch['when']}"
                             f", {heading}"))
    with ThreadPoolExecutor(int(os.environ.get("CLAUDE_WORKERS", 3))) as ex:
        results = list(ex.map(lambda j: pick(f"{j[0]}|{j[1]}", j[2], j[3], j[4]), jobs))
    chosen = {}
    for (key, heading, g, k, _), res in zip(jobs, results):
        if res is None:
            waiting += 1
            continue
        chosen.setdefault(key, {}).setdefault(heading, {}).update(res)
    if waiting:
        print(f"  {waiting} pages got no answer from Claude (usage limit or not signed in); run again later "
              "to add them.", flush=True)

    all_ids = [i for pages in chosen.values() for pg in pages.values() for i in pg]
    fetch(all_ids, byid)
    finish_all(all_ids, args.look)
    have = lambda i: os.path.exists(os.path.join(IMAGES, f"{i}.jpg"))

    for key, ch in order:
        pages = []
        for heading, ids in ch["pages"].items():
            got = sorted((i for i in chosen.get(key, {}).get(heading, {}) if have(i)), key=lambda i: when[i])
            if not got:
                continue
            caps = chosen[key][heading]
            photos = []
            for i in got:
                with Image.open(os.path.join(IMAGES, f"{i}.jpg")) as im:
                    w, h = im.size
                photos.append({"src": f"images/{i}.jpg", "w": w, "h": h, "caption": caps.get(i, ""),
                               "date": f"{dt(when[i]).day} {dt(when[i]):%b}"})
            pts = [when[i] for i in got]
            sub = span(pts) if ch["kind"] == "trip" else f"{len(got)} photos"
            pages.append({"heading": heading, "sub": sub, "photos": photos, "first": min(pts)})
        if not pages:
            continue
        pages.sort(key=lambda p: p["first"])
        for p in pages:
            del p["first"]
        ts = [when[i] for ids in ch["pages"].values() for i in ids]
        flat = [p for pg in pages for p in pg["photos"]]
        cover = max(flat, key=lambda p: (p["w"] >= p["h"], bool(p["caption"])))["src"]
        c = {"id": slug(ch["title"] + "-" + ch["when"]), "kind": ch["kind"], "title": ch["title"], "when": ch["when"],
             "dates": span(ts) if ch["kind"] == "trip" else ch["when"], "places": [p["heading"] for p in pages] if ch["kind"] == "trip" else [],
             "cover": cover, "pages": pages}
        words = intro(c)
        c["intro"] = words.get("intro", "")
        for pg in pages:  # the place's line, and its standout photo for the chapter's highlights
            pg["blurb"] = words.get(pg["heading"], "")
            pg["lead"] = max(range(len(pg["photos"])), key=lambda j: (pg["photos"][j]["w"] >= pg["photos"][j]["h"], bool(pg["photos"][j]["caption"]), -j))
        out_ch.append(c)

    years = sorted({dt(when[i]).year for i in ok})
    book = {"title": args.title, "span": f"{years[0]}–{years[-1]}" if len(years) > 1 else str(years[0]) if years else "",
            "subtitle": f"{sum(c['kind'] == 'trip' for c in out_ch)} trips and life at home, chosen from {len(ok):,} photos",
            "note": f"Made {datetime.now():%d %b %Y} from our iCloud Photos.", "chapters": out_ch}
    save(os.path.join(SITE, "data.json"), book)
    shutil.copy(os.path.join(HERE, "scrapbook_page.html"), os.path.join(SITE, "index.html"))
    used = {os.path.basename(p["src"]) for c in out_ch for pg in c["pages"] for p in pg["photos"]}
    for f in os.listdir(IMAGES):  # photos dropped from the book since the last run
        if f not in used:
            os.remove(os.path.join(IMAGES, f))
    n = len(used)
    mb = sum(os.path.getsize(os.path.join(IMAGES, f)) for f in used) / 1e6
    print(f"Scrapbook ready: {len(out_ch)} chapters, {n} photos ({mb:.0f} MB) in {os.path.relpath(SITE)}")
    print("To share it: open Claude Code in this folder and ask it to \"publish my scrapbook\".")
    if args.preview:
        port = 8790
        subprocess.Popen([sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1", "--directory", SITE],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1)
        subprocess.run(["open", f"http://127.0.0.1:{port}/"])
        print(f"Preview at http://127.0.0.1:{port}/ (press Control-C to stop)")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
