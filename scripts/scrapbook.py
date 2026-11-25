"""Turn the sorted iCloud library into a digital scrapbook.  Mac only, after icloud_albums.py has run.

  .venv/bin/python scripts/scrapbook.py              # build work/scrapbook/site/
  .venv/bin/python scripts/scrapbook.py --preview    # build, then open it in the browser
  .venv/bin/python scripts/scrapbook.py --title "The Tan family scrapbook"
  .venv/bin/python scripts/scrapbook.py --look rich      # finish: natural (default), rich or none
  .venv/bin/python scripts/scrapbook.py --restore        # put back everything removed by hand

In the preview (--preview), open a photo or clip and press "Remove from scrapbook": it is taken out (never
from Photos) and Claude picks a replacement from the same place or month, which appears in its place.

Chapters: one per trip (a page per place), plus one "at home" chapter per year (a page per month).
Claude picks the best photos for each page from the previews (sharp, well composed, people, the
place itself, no near-copies) and writes a short caption for some of them and an intro per chapter.
Claude also picks a video or two per place; each becomes a short clip (up to 12 seconds, 1280px,
without metadata) that plays silently in the page and with sound when opened.
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
VIDEOS = os.path.join(SITE, "videos")
MAX_VIDEOS = 24                              # about 3-5 MB each; keeps the page quick and within upload limits
CLIP = 12                                    # seconds per clip
RAW = os.path.join(OUT, "raw")              # web-size copies before the finish, so a new --look needs no re-download
SHEETS = os.path.join(OUT, "sheets")
PICKS = os.path.join(OUT, "picks.json")     # cache: page key -> {id: caption}
INTROS = os.path.join(OUT, "intros.json")   # cache: chapter id -> intro
REMOVED = os.path.join(OUT, "removed.json") # ids taken out of the book by hand; Claude picks replacements
LEAVE_OUT = {"docs", "memes", "screenshots", "vscreen"}  # never in a scrapbook (vscreen: screen recordings)
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

def sheet(ids, path, tags=None):
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
        tag = str(i) + ((" " + tags[i]) if tags and i in tags else "")
        d.rectangle((x + 4, y + 4, x + 14 + 13 * len(tag), y + 32), fill="black")
        d.text((x + 8, y + 6), tag, fill="yellow", font=font)
    im.save(path, quality=82)


def pick(key, ids, k, what, tags=None):
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
            sheet(ids[s:s + 30], p, tags)
            paths.append(p)
        video = bool(tags)
        noun = "videos" if video else "photos"
        brief = (
            "These tiles are opening frames of videos (tagged VIDEO with their length). Pick the clips most worth "
            "watching: real moments and movement (waves, a first swim, kids running, a parade, a view from a "
            "boat), people looking happy, the place at its best. Skip shaky, dark, accidental, pocket or "
            "screen-recording clips, and two clips of the same moment." if video else
            "Choose like the photo editor of a fine travel and family magazine building the story of this place:\n"
            "- one strong establishing image of the place itself: a wide view, landmark or landscape, ideally in "
            "beautiful light (golden hour, soft daylight, night lights);\n"
            "- genuine family moments: candid laughter, faces you can see, eyes open, everyone looking good;\n"
            "- texture that tells the story: food, details, interiors, street life;\n"
            "- variety in distance (wide, medium, close), scene and time of day, so the set reads like a story;\n"
            "- craft: sharp, well exposed, level horizons, clean backgrounds, strong composition.\n"
            "Never pick two near-identical shots, or blurry, dark, cluttered, unflattering, accidental, receipt, "
            "document or screenshot images.")
        ans = ia.ask_claude(
            "You are curating a family scrapbook. Use the Read tool to look at each of these contact sheets:\n"
            + "\n".join(paths) + "\n\n"
            f"They show the {noun} from {what}, each tagged in yellow with an id. Pick the {k} {noun} that belong "
            f"in a beautiful scrapbook. {brief}\n\n"
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
        "Write \"intro\": one short sentence, at most 16 words, that opens the chapter; it is set large over "
        "the chapter's opening photo."
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


def fetch_videos(vids, byid):
    """Short web clips in site/videos/<id>.mp4 (no metadata) and their first frames in raw/<id>.jpg."""
    ff = shutil.which("ffmpeg")
    if not ff:
        print("  ffmpeg isn't installed, so videos are left out (brew install ffmpeg, then run again).", flush=True)
        return
    os.makedirs(VIDEOS, exist_ok=True)
    os.makedirs(RAW, exist_ok=True)
    todo = [i for i in vids if not os.path.exists(os.path.join(VIDEOS, f"{i}.mp4"))]
    if not todo:
        return
    print(f"Making {len(todo)} short video clips...", flush=True)
    tmp = os.path.join(OUT, "dlv")
    for n, i in enumerate(todo, 1):
        shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(tmp)
        pid = byid[i]["photos_id"]
        try:
            ia.osa(EXPORT_JPEG, [tmp, pid])
        except Exception:
            pass
        if not ia.pick_file(tmp):
            ia.wait_for_files([tmp], 10)
        if not ia.pick_file(tmp):
            ia.download_icloud([(pid, tmp)], {})
        src = ia.pick_file(tmp)
        if not src:
            print(f"  could not get video {i}; it is left out", flush=True)
            continue
        dur = float(byid[i].get("duration") or 0)
        start = 0 if dur <= CLIP + 2 else min(dur * .15, dur - CLIP)   # skip the fumbling first seconds of long clips
        out = os.path.join(VIDEOS, f"{i}.mp4")
        for crf in (26, 31):
            r = subprocess.run([ff, "-nostdin", "-v", "error", "-y", "-ss", f"{start:.2f}", "-i", src, "-t", str(CLIP),
                                "-map", "0:v:0", "-map", "0:a:0?", "-map_metadata", "-1", "-map_chapters", "-1",
                                "-vf", "scale='if(gt(iw,ih),min(1280,iw),-2)':'if(gt(iw,ih),-2,min(1280,ih))'",
                                "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf), "-pix_fmt", "yuv420p",
                                "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", out],
                               capture_output=True, text=True, stdin=subprocess.DEVNULL)
            if r.returncode == 0 and os.path.getsize(out) < 14e6:
                break
        if r.returncode or not os.path.exists(out):
            print(f"  could not make a clip of video {i}: {r.stderr.strip()[-160:]}", flush=True)
            if os.path.exists(out):
                os.remove(out)
            continue
        subprocess.run([ff, "-nostdin", "-v", "error", "-y", "-ss", "0.3", "-i", out, "-frames:v", "1", "-q:v", "3",
                        os.path.join(RAW, f"{i}.jpg")], capture_output=True, stdin=subprocess.DEVNULL)
        print(f"PROGRESS {n} {len(todo)}", flush=True)
    shutil.rmtree(tmp, ignore_errors=True)


# ---------- building ----------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--title", default="Our Scrapbook")
    ap.add_argument("--preview", action="store_true", help="open the finished scrapbook in the browser")
    ap.add_argument("--look", choices=list(LOOKS), default="natural", help="photo finish (default natural)")
    ap.add_argument("--restore", action="store_true", help="put back the photos and clips removed by hand")
    ap.add_argument("--no-open", action="store_true", help="with --preview: don't open a browser (the Mac app shows it)")
    args = ap.parse_args()
    if args.restore and os.path.exists(REMOVED):
        os.remove(REMOVED)
        print("Everything removed by hand is back in the running.", flush=True)
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
    okv = [r["id"] for r in inv if r["kind"] == "video" and r["id"] in when and "error" not in r
           and r["id"] not in dupes | bad | gone and labels.get(str(r["id"])) not in LEAVE_OUT
           and float(r.get("duration") or 0) >= 2]
    print(f"{len(ok)} photos and {len(okv)} videos can go in the scrapbook.", flush=True)

    # chapters: trips (pages = places) and years at home (pages = months)
    chapters = {}
    for kind, pool in (("pages", ok), ("vpages", okv)):
        for i in pool:
            t = trips.get(str(i))
            if t:
                name, _, stop = t.partition("/")
                title, _, w = name.rpartition(", ")
                key, heading = "t:" + name, stop or title or name
                ch = chapters.setdefault(key, {"kind": "trip", "title": title or name, "when": w, "pages": {}, "vpages": {}})
            else:
                d = dt(when[i])
                key, heading = f"y:{d.year}", MONTHS[d.month - 1]
                ch = chapters.setdefault(key, {"kind": "year", "title": f"{d.year} at home", "when": str(d.year), "pages": {}, "vpages": {}})
            if kind == "vpages" and key not in chapters:
                continue
            ch[kind].setdefault(heading, []).append(i)
    chapters = {k: ch for k, ch in chapters.items() if ch["pages"]}  # a chapter needs photos; videos join them
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
                             f", {heading}", None))
        for heading, ids in ch["vpages"].items():  # a video or two per place or month
            if heading not in ch["pages"]:
                continue
            ids = sorted(ids, key=lambda i: when[i])[:90]
            k = 1 if len(ids) < 8 or ch["kind"] == "year" else 2
            tags = {i: f"VIDEO {float(byid[i].get('duration') or 0):.0f}s" for i in ids}
            jobs.append((key, heading, ids, k, f"{'a trip to ' + ch['title'] if ch['kind'] == 'trip' else 'everyday life at home in ' + ch['when']}"
                         f", {heading}", tags))
    with ThreadPoolExecutor(int(os.environ.get("CLAUDE_WORKERS", 3))) as ex:
        results = list(ex.map(lambda j: pick(f"{j[0]}|{j[1]}" + ("|v" if j[5] else ""), j[2], j[3], j[4], j[5]), jobs))
    chosen, vchosen = {}, {}
    for (key, heading, g, k, _, tags), res in zip(jobs, results):
        if res is None:
            waiting += 1
            continue
        (vchosen if tags else chosen).setdefault(key, {}).setdefault(heading, {}).update(res)
    # at most 4 clips per chapter and MAX_VIDEOS in all, trips first
    budget = MAX_VIDEOS
    for key, _ in sorted(order, key=lambda kv: kv[1]["kind"] != "trip"):
        n = 0
        for heading in list(vchosen.get(key, {})):
            for i in list(vchosen[key][heading]):
                if n >= 4 or budget <= 0:
                    del vchosen[key][heading][i]
                else:
                    n += 1
                    budget -= 1
    if waiting:
        print(f"  {waiting} pages got no answer from Claude (usage limit or not signed in); run again later "
              "to add them.", flush=True)

    what_of = lambda ch, heading: (f"{'a trip to ' + ch['title'] if ch['kind'] == 'trip' else 'everyday life at home in ' + ch['when']}"
                                   f", {heading}")

    def top_up():
        """Photos and clips removed by hand leave the book; Claude picks the best of the rest of that page."""
        removed = set(load(REMOVED, []))
        for sel, field, video in ((chosen, "pages", False), (vchosen, "vpages", True)):
            for key, pages in sel.items():
                ch = chapters.get(key)
                if not ch:
                    continue
                for heading, picks in pages.items():
                    out = [i for i in picks if i in removed]
                    if not out:
                        continue
                    for i in out:
                        del picks[i]
                    pool = [i for i in sorted(ch[field].get(heading, []), key=lambda i: when[i])
                            if i not in picks and i not in removed][:90]
                    if not pool:
                        continue
                    tags = {i: f"VIDEO {float(byid[i].get('duration') or 0):.0f}s" for i in pool} if video else None
                    res = pick(f"{key}|{heading}|swap" + ("|v" if video else ""), pool, min(len(out), len(pool)),
                               what_of(ch, heading) + (" (replacing ones the family took out)"), tags)
                    if res:
                        picks.update(res)

    def assemble():
        top_up()
        out_ch = []  # fresh each time: the preview rebuilds after every removal
        all_ids = [i for pages in chosen.values() for pg in pages.values() for i in pg]
        all_vids = [i for pages in vchosen.values() for pg in pages.values() for i in pg]
        fetch(all_ids, byid)
        fetch_videos(all_vids, byid)
        finish_all(all_ids + all_vids, args.look)
        have = lambda i: os.path.exists(os.path.join(IMAGES, f"{i}.jpg"))
        have_v = lambda i: have(i) and os.path.exists(os.path.join(VIDEOS, f"{i}.mp4"))

        for key, ch in order:
            pages = []
            for heading, ids in ch["pages"].items():
                caps = dict(chosen.get(key, {}).get(heading, {}))
                vsel = vchosen.get(key, {}).get(heading, {})
                caps.update(vsel)
                got = sorted([i for i in chosen.get(key, {}).get(heading, {}) if have(i)]
                             + [i for i in vsel if have_v(i)], key=lambda i: when[i])
                if not got:
                    continue
                photos = []
                for i in got:
                    with Image.open(os.path.join(IMAGES, f"{i}.jpg")) as im:
                        w, h = im.size
                    item = {"src": f"images/{i}.jpg", "w": w, "h": h, "caption": caps.get(i, ""),
                            "date": f"{dt(when[i]).day} {dt(when[i]):%b}"}
                    if i in vsel:
                        item.update(type="video", src=f"videos/{i}.mp4", poster=f"images/{i}.jpg")
                    photos.append(item)
                pts = [when[i] for i in got]
                sub = span(pts) if ch["kind"] == "trip" else f"{len(got)} photos" + (" and videos" if any(i in vsel for i in got) else "")
                pages.append({"heading": heading, "sub": sub, "photos": photos, "first": min(pts)})
            if not pages:
                continue
            pages.sort(key=lambda p: p["first"])
            for p in pages:
                del p["first"]
            ts = [when[i] for ids in ch["pages"].values() for i in ids]
            flat = [p for pg in pages for p in pg["photos"] if p.get("type") != "video"] or [p for pg in pages for p in pg["photos"]]
            best = max(flat, key=lambda p: (p["w"] >= p["h"], bool(p["caption"])))
            cover = best.get("poster", best["src"])
            c = {"id": slug(ch["title"] + "-" + ch["when"]), "kind": ch["kind"], "title": ch["title"], "when": ch["when"],
                 "dates": span(ts) if ch["kind"] == "trip" else ch["when"], "places": [p["heading"] for p in pages] if ch["kind"] == "trip" else [],
                 "cover": cover, "pages": pages}
            words = intro(c)
            c["intro"] = words.get("intro", "")
            for pg in pages:  # the place's line, and its standout photo for the chapter's highlights
                pg["blurb"] = words.get(pg["heading"], "")
                pg["lead"] = max(range(len(pg["photos"])), key=lambda j: (pg["photos"][j].get("type") != "video", pg["photos"][j]["w"] >= pg["photos"][j]["h"], bool(pg["photos"][j]["caption"]), -j))
            out_ch.append(c)

        years = sorted({dt(when[i]).year for i in ok})
        nt, ny = sum(c["kind"] == "trip" for c in out_ch), sum(c["kind"] == "year" for c in out_ch)
        words = ["", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten"]
        num = lambda n: words[n] if n < len(words) else str(n)
        book = {"title": args.title, "span": f"{years[0]}–{years[-1]}" if len(years) > 1 else str(years[0]) if years else "",
                "subtitle": "The trips we took and the days in between" if nt else "The days that make a year",
                "lead": f"{num(nt)} {'trip' if nt == 1 else 'trips'}, {num(ny).lower()} {'year' if ny == 1 else 'years'} at home,",
                "rest": f"chosen from {len(ok):,} of our photos and kept in one place.",
                "cover": next((c["cover"] for c in out_ch if c["kind"] == "trip"), out_ch[0]["cover"] if out_ch else ""),
                "note": f"Made {datetime.now():%d %b %Y} from our iCloud Photos.", "chapters": out_ch}
        save(os.path.join(SITE, "data.json"), book)
        shutil.copy(os.path.join(HERE, "scrapbook_page.html"), os.path.join(SITE, "index.html"))
        items = [p for c in out_ch for pg in c["pages"] for p in pg["photos"]]
        used = {os.path.basename(p.get("poster", p["src"])) for p in items}
        usedv = {os.path.basename(p["src"]) for p in items if p.get("type") == "video"}
        for folder, keep in ((IMAGES, used), (VIDEOS, usedv)):  # dropped from the book since the last run
            for f in os.listdir(folder) if os.path.isdir(folder) else []:
                if f not in keep:
                    os.remove(os.path.join(folder, f))
        mb = (sum(os.path.getsize(os.path.join(IMAGES, f)) for f in used)
              + sum(os.path.getsize(os.path.join(VIDEOS, f)) for f in usedv)) / 1e6
        print(f"Scrapbook ready: {len(out_ch)} chapters, {len(used) - len(usedv)} photos and {len(usedv)} video clips "
              f"({mb:.0f} MB) in {os.path.relpath(SITE)}")
        print("To share it: open Claude Code in this folder and ask it to \"publish my scrapbook\".")
    assemble()
    if args.preview:
        preview(assemble, open_it=not args.no_open)


def preview(assemble, port=8790, open_it=True):
    """Serve the book on this Mac only. POST api/remove takes a photo or clip out and fills its place."""
    import threading
    from functools import partial
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
    lock = threading.Lock()

    class H(SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send_json(self, obj, code=200):
            b = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            if self.path.split("?")[0] == "/api/editable":
                return self.send_json({"ok": True})
            return super().do_GET()

        def do_POST(self):
            if self.headers.get("Origin") not in (None, f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
                return self.send_json({"error": "forbidden"}, 403)
            if self.path != "/api/remove":
                return self.send_json({"error": "not found"}, 404)
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0) or 0)) or b"{}")
            m = re.search(r"(\d+)\.(jpg|mp4)$", str(body.get("src", "")))
            if not m:
                return self.send_json({"error": "Which photo?"}, 400)
            with lock:
                removed = set(load(REMOVED, []))
                removed.add(int(m.group(1)))
                save(REMOVED, sorted(removed))
                print(f"Removed {m.group(0)}; Claude is choosing a replacement...", flush=True)
                assemble()
            return self.send_json({"ok": True})

    srv = ThreadingHTTPServer(("127.0.0.1", port), partial(H, directory=SITE))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    if open_it:
        subprocess.run(["open", f"http://127.0.0.1:{port}/"])
    print(f"Preview at http://127.0.0.1:{port}/ (press Control-C to stop)")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()
