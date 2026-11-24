"""Sort iCloud Photos into albums, without keeping copies on the Mac.

  .venv/bin/python scripts/icloud_albums.py --limit 200   # try the first 200 items
  .venv/bin/python scripts/icloud_albums.py               # the whole library (resumes where it stopped)
  .venv/bin/python scripts/icloud_albums.py --dry-run     # everything except adding to albums

Mac only. Works with "Optimize Mac Storage" on:
  1. Asks Photos for a small batch of items, which downloads them from iCloud into work/icloud/export/.
  2. Makes the small previews the sorter needs, then deletes the downloaded copies.
  3. Runs dupes.py and label_claude.py on the previews, the same as a normal sort.
  4. Finds trips from each item's date and location: home is the place with photos on the most
     different days; a run of photos more than 100 km from home is a trip, named by Claude
     ("Japan, Mar 2025"). Trip items go to an album per trip in Photo sorter > Trips; a trip that
     covers several places becomes a folder with an album per place (Trips > Japan, Mar 2025 > Kyoto).
  5. Adds everything else to an album per folder in categories.json, inside "Photo sorter" in Photos.
     Duplicates go to "Review - duplicates". Albums take no extra iCloud space.
  6. Claude flags blurry, dark and accidental shots; they ALSO go to "Review - maybe delete".
Options: --home LAT,LON if home is guessed wrong, --away-km 100, --no-trips, --no-review,
  --redo to fill the albums again after deleting the "Photo sorter" folder in Photos.
Then ./start.sh shows the result on the dashboard (trips, duplicates and "maybe delete" included).
Nothing in the Photos library is moved, edited or deleted. Run it again after adding new photos:
only the new ones are downloaded, sorted and added, and albums you tidied by hand are left alone.
The first run, macOS asks to let Terminal control Photos: click Allow.
"""
import argparse, hashlib, json, math, os, re, shutil, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from PIL import Image, ImageOps
import imagehash

try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass

sys.path.insert(0, os.path.dirname(__file__))
from common import CATEGORIES, DATA, IMG, LABELS, THUMBS, VID, WORK, categories, load, save

HERE = os.path.dirname(os.path.abspath(__file__))
MINI = os.path.join(DATA, "mini")
ICLOUD = os.path.join(WORK, "icloud")
EXPORT = os.path.join(ICLOUD, "export")
ITEMS = os.path.join(ICLOUD, "items.json")      # Photos ids, in library order; index = inventory id
FAILED = os.path.join(ICLOUD, "failed.json")    # ids Photos could not export, with the reason
ADDED = os.path.join(ICLOUD, "added.json")      # ids already put in an album (later runs only add new ones)
BAD = os.path.join(ICLOUD, "maybe_delete.json") # {"checked": [inventory ids], "bad": {id: reason}, "added": [Photos ids]}
PLACES = os.path.join(ICLOUD, "places.json")    # place names Claude gave trip locations, so names stay the same
INV = os.path.join(DATA, "inventory.json")
FOLDER = "Photo sorter"
TRIPS = "Trips"
DUPES_ALBUM = "Review - duplicates"
BAD_ALBUM = "Review - maybe delete"
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "sonnet")
NOT_TRIP = {"docs", "memes", "screenshots", "vscreen"}  # unlocated screenshots etc. never join a trip
TRIP_GAP = 3 * 86400   # a longer gap between photos away from home starts a new trip
TRIP_MIN = 6           # fewer photos away than this is not a trip
HAS_FFMPEG = shutil.which("ffmpeg") and shutil.which("ffprobe")


def osa(script, args, timeout=7200):
    r = subprocess.run(["osascript", "-", *args], input=script, capture_output=True, text=True, timeout=timeout)
    if r.returncode:
        err = r.stderr.strip()
        if "-1743" in err or "Not authorized" in err:
            sys.exit("macOS blocked access to Photos. Open System Settings > Privacy & Security > Automation, "
                     "turn on Photos under Terminal, then run this again.")
        raise RuntimeError(err[:300])
    return r.stdout


LIST = """
tell application "Photos"
  with timeout of 1800 seconds
    set ids to id of every media item
  end timeout
end tell
set AppleScript's text item delimiters to linefeed
return ids as text
"""

# argv: one folder per item, then the item ids in the same order. Prints "FAIL<tab>id<tab>reason" lines.
EXPORT_SCRIPT = """
on run argv
  set n to (count of argv) div 2
  set out to ""
  tell application "Photos"
    with timeout of 7200 seconds
      repeat with i from 1 to n
        set pid to item (n + i) of argv
        try
          set dest to (POSIX file (item i of argv)) as alias
          export {media item id pid} to dest with using originals
        on error msg
          set out to out & "FAIL" & tab & pid & tab & msg & linefeed
        end try
      end repeat
    end timeout
  end tell
  return out
end run
"""

# One line per item: id, days and seconds since 1970 (Mac's local time), latitude, longitude (blank if none).
# Days and seconds are separate because AppleScript prints big numbers as 1.7E+9.
META_SCRIPT = """
tell application "Photos"
  with timeout of 1800 seconds
    set ids to id of every media item
    set ds to date of every media item
    set ls to location of every media item
  end timeout
end tell
set e to current date
set day of e to 1
set year of e to 1970
set month of e to January
set time of e to 0
set out to {}
repeat with i from 1 to count of ids
  set dd to ""
  set ss to ""
  try
    set s to (item i of ds) - e
    set dd to (s div 86400) as text
    set ss to ((s mod 86400) as integer) as text
  end try
  set la to ""
  set lo to ""
  try
    set loc to item i of ls
    if (item 1 of loc) is not missing value then
      set la to (item 1 of loc) as text
      set lo to (item 2 of loc) as text
    end if
  end try
  set end of out to (item i of ids) & tab & dd & tab & ss & tab & la & tab & lo
end repeat
set AppleScript's text item delimiters to linefeed
return out as text
"""

# argv: folders inside "Photo sorter" as "Trips/Japan, Mar 2025" ("" for none), album name, then item ids.
ALBUM_SCRIPT = """
on run argv
  set sub to item 1 of argv
  set albumName to item 2 of argv
  set parts to {}
  if sub is not "" then
    set AppleScript's text item delimiters to "/"
    set parts to text items of sub
    set AppleScript's text item delimiters to ""
  end if
  tell application "Photos"
   with timeout of 1800 seconds
    if not (exists folder "%s") then make new folder named "%s"
    set f to folder "%s"
    repeat with p in parts
      set p to p as text
      if not (exists folder p of f) then make new folder named p at f
      set f to folder p of f
    end repeat
    if not (exists album albumName of f) then make new album named albumName at f
    set a to album albumName of f
    set itms to {}
    repeat with i from 3 to count of argv
      try
        set end of itms to media item id (item i of argv)
      end try
    end repeat
    if (count of itms) > 0 then add itms to a
    return count of itms
   end timeout
  end tell
end run
""" % ((FOLDER,) * 3)


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def square(im, size=256):
    im = im.convert("RGB")
    s = min(im.size)
    left, top = (im.width - s) // 2, (im.height - s) // 2
    return im.crop((left, top, left + s, top + s)).resize((size, size))


def video_info(path, thumb):
    if not HAS_FFMPEG:
        return 0, 0, 0.0
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=width,height:stream_tags=rotate:format=duration", "-of", "json", path],
                       capture_output=True, text=True, stdin=subprocess.DEVNULL)
    j = json.loads(r.stdout or "{}")
    s = (j.get("streams") or [{}])[0]
    w, h = s.get("width", 0), s.get("height", 0)
    if str(s.get("tags", {}).get("rotate", "0")) in ("90", "-90", "270"):
        w, h = h, w
    dur = float(j.get("format", {}).get("duration", 0) or 0)
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", "1" if dur > 2 else "0", "-i", path, "-frames:v", "1",
                    "-vf", "scale=256:256:force_original_aspect_ratio=increase,crop=256:256", thumb],
                   stdin=subprocess.DEVNULL)
    return w, h, dur


def pick_file(folder):
    """A Live Photo exports as a photo plus a short .mov: keep the photo."""
    fs = sorted(f for f in os.listdir(folder) if not f.startswith(".") and os.path.splitext(f)[1].lower() in IMG | VID)
    photos = [f for f in fs if os.path.splitext(f)[1].lower() in IMG]
    return os.path.join(folder, (photos or fs)[0]) if fs else None


def preview(i, pid, path):
    """Same record and previews as scan.py, plus the Photos id. The downloaded file is deleted afterwards."""
    ext = os.path.splitext(path)[1].lower()
    kind = "video" if ext in VID else "photo"
    rec = {"id": i, "photos_id": pid, "path": path, "name": os.path.basename(path), "ext": ext, "kind": kind,
           "bytes": os.path.getsize(path), "sha256": sha(path)}
    thumb, mini = os.path.join(THUMBS, f"{i}.jpg"), os.path.join(MINI, f"{i}.jpg")
    try:
        if kind == "video":
            rec["w"], rec["h"], rec["duration"] = video_info(path, thumb)
            if not os.path.exists(thumb):
                Image.new("RGB", (256, 256), (60, 60, 66)).save(thumb)
        else:
            with Image.open(path) as im:
                rec["w"], rec["h"] = im.size
                im.draft("RGB", (512, 512))
                square(ImageOps.exif_transpose(im)).save(thumb, quality=80)
        with Image.open(thumb) as t:
            rec["phash"] = str(imagehash.phash(t))
            t.convert("RGB").resize((72, 72)).save(mini, quality=72)
    except Exception as e:
        rec["error"] = str(e)[:200]
        Image.new("RGB", (256, 256), (230, 228, 222)).save(thumb)
        Image.new("RGB", (72, 72), (230, 228, 222)).save(mini)
    return rec


def download_and_preview(limit, batch):
    print("Asking Photos for the list of items...", flush=True)
    current = [x for x in osa(LIST, []).splitlines() if x.strip()]
    live = set(current)
    old = load(ITEMS, [])
    known = set(old)
    ids = old + [x for x in current if x not in known]  # new items go at the end, so earlier ids never change
    save(ITEMS, ids)
    print(f"Photos has {len(current)} items" + (f" ({len(ids) - len(old)} new since last time)" if old else "") + ".",
          flush=True)
    want = [x for x in (ids[:limit] if limit else ids) if x in live]  # skip items deleted from Photos

    inv = load(INV, []) if load(os.path.join(DATA, "source.json"), {}).get("source") == "iCloud Photos" else []
    if not inv:  # fresh start: clear previews from any earlier folder sort
        for d in (THUMBS, MINI, LABELS, os.path.join(WORK, "sheets")):
            shutil.rmtree(d, ignore_errors=True)
        for f in ("labels.json", "dupes.json"):
            if os.path.exists(os.path.join(DATA, f)):
                os.remove(os.path.join(DATA, f))
    for d in (THUMBS, MINI):
        os.makedirs(d, exist_ok=True)
    save(os.path.join(DATA, "source.json"), {"source": "iCloud Photos", "limit": limit})

    have = {r["photos_id"] for r in inv}
    failed = load(FAILED, {})
    index = {pid: i for i, pid in enumerate(ids)}
    todo = [(index[pid], pid) for pid in want if pid not in have]  # failed ones are retried
    if not todo:
        print("All previews already made.", flush=True)
        return inv, live
    print(f"Downloading and previewing {len(todo)} items, {batch} at a time. "
          "Each batch's downloads are deleted as soon as its previews are made.", flush=True)
    t0 = time.time()
    for s in range(0, len(todo), batch):
        chunk = todo[s:s + batch]
        shutil.rmtree(EXPORT, ignore_errors=True)
        dirs = [os.path.join(EXPORT, str(i)) for i, _ in chunk]
        for d in dirs:
            os.makedirs(d)
        try:
            out = osa(EXPORT_SCRIPT, dirs + [pid for _, pid in chunk])
        except subprocess.TimeoutExpired:
            out = ""
        for line in out.splitlines():
            parts = line.split("\t")
            if parts[0] == "FAIL" and len(parts) >= 3:
                failed[parts[1]] = parts[2][:200]
        for (i, pid), d in zip(chunk, dirs):
            path = pick_file(d)
            if path:
                failed.pop(pid, None)
                inv.append(preview(i, pid, path))
            elif pid not in failed:
                failed[pid] = "Photos exported nothing (it may still be downloading from iCloud)"
        shutil.rmtree(EXPORT, ignore_errors=True)
        inv.sort(key=lambda r: r["id"])
        save(INV, inv)  # saved every batch, so stopping and running again resumes here
        save(FAILED, failed)
        done = min(s + batch, len(todo))
        rate = (time.time() - t0) / done
        print(f"PROGRESS {done} {len(todo)}", flush=True)
        print(f"  {done}/{len(todo)} done, about {rate * (len(todo) - done) / 60:.0f} min left", flush=True)
    if failed:
        print(f"  {len(failed)} items could not be downloaded (listed in {os.path.relpath(FAILED)}). "
              "Run again later to retry them.", flush=True)
    return inv, live


def final_labels(inv):
    """Same rules as apply.py: Claude's labels, kind must match, hand moves in the dashboard win."""
    cats = {c["key"]: c for c in categories()}
    claude = {}
    for f in sorted(os.listdir(LABELS)) if os.path.isdir(LABELS) else []:
        if f.endswith(".json"):
            claude.update({str(k): v for k, v in load(os.path.join(LABELS, f)).items()})
    labels, unlabelled = {}, set()
    for r in inv:
        pick = claude.get(str(r["id"]))
        if pick is None:
            unlabelled.add(str(r["id"]))
        if pick not in cats or cats[pick]["kind"] != r["kind"]:
            pick = [c for c in cats.values() if c["kind"] == r["kind"]][-1]["key"]
        labels[str(r["id"])] = pick
    for k, v in load(os.path.join(WORK, "overrides.json"), {}).items():
        if k in labels and v in cats:
            labels[k] = v
            unlabelled.discard(k)
    return cats, labels, unlabelled


def ask_claude(prompt, timeout=900):
    """One headless Claude Code call (your Claude plan). Returns the first {...} in the answer, or None."""
    claude = shutil.which("claude")
    if not claude:
        return None
    for attempt in range(3):
        if attempt:
            time.sleep(8 * attempt)
        try:
            r = subprocess.run([claude, "-p", prompt, "--model", CLAUDE_MODEL, "--allowedTools", "Read",
                                "--strict-mcp-config", "--setting-sources", "project", "--no-session-persistence",
                                "--output-format", "json"], cwd=WORK, capture_output=True, text=True,
                               timeout=timeout, stdin=subprocess.DEVNULL)
            text = str(json.loads(r.stdout).get("result", ""))
            m = re.search(r"\{[^{}]*\}", text, re.S)
            if m:
                return json.loads(m.group(0))
        except (subprocess.TimeoutExpired, ValueError, AttributeError):
            pass
    return None


# ---------- trips ----------

def read_meta():
    """{photos_id: (time, lat, lon)} for the whole library; time is the Mac's local time in seconds."""
    meta = {}
    for line in osa(META_SCRIPT, []).splitlines():
        parts = line.split("\t")
        if len(parts) != 5 or not parts[1]:
            continue
        try:
            t = int(parts[1]) * 86400 + int(float(parts[2].replace(",", ".")))
        except ValueError:
            continue
        try:
            la, lo = float(parts[3].replace(",", ".")), float(parts[4].replace(",", "."))
            if abs(la) > 90 or abs(lo) > 180 or (la == 0 and lo == 0):
                raise ValueError
        except ValueError:
            la = lo = None
        meta[parts[0]] = (t, la, lo)
    return meta


def km(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 12742 * math.asin(math.sqrt(h))


def median(xs):
    xs = sorted(xs)
    return xs[len(xs) // 2]


def guess_home(points):
    """The ~50 km square with photos on the most different days."""
    days = {}
    for t, la, lo in points:
        days.setdefault((round(la * 2), round(lo * 2)), set()).add(t // 86400)
    best = max(days, key=lambda c: len(days[c]))
    near = [(la, lo) for t, la, lo in points if (round(la * 2), round(lo * 2)) == best]
    return median([p[0] for p in near]), median([p[1] for p in near])


def find_trips(meta, home, away_km):
    located = sorted((t, la, lo, pid) for pid, (t, la, lo) in meta.items() if la is not None)
    trips, cur = [], None
    for t, la, lo, pid in located:
        if km(home, (la, lo)) > away_km:
            if cur and t - cur["end"] <= TRIP_GAP:
                cur["end"] = t
                cur["shots"].append((t, la, lo, pid))
            else:
                if cur:
                    trips.append(cur)
                cur = {"start": t, "end": t, "shots": [(t, la, lo, pid)]}
        elif cur:  # back home
            trips.append(cur)
            cur = None
    if cur:
        trips.append(cur)
    trips = [tr for tr in trips if len(tr["shots"]) >= TRIP_MIN]
    for tr in trips:
        tr["points"] = [(la, lo) for _, la, lo, _ in tr["shots"]]
        tr["pids"] = {pid for *_, pid in tr["shots"]}
        tr["stops"] = find_stops(tr["shots"])
    return trips


def find_stops(shots):
    """Places within a trip: ~50 km squares with at least 3 photos. Returns [(center, {pid})], or [] if only one."""
    cells = {}
    for t, la, lo, pid in shots:
        cells.setdefault((round(la * 2), round(lo * 2)), []).append((la, lo, pid))
    big = [v for v in cells.values() if len(v) >= 3]
    if len(big) < 2:
        return []
    stops = [((median([x[0] for x in v]), median([x[1] for x in v])), set()) for v in big]
    for t, la, lo, pid in shots:  # every photo goes to the nearest place, including those in small squares
        min(stops, key=lambda st: km(st[0], (la, lo)))[1].add(pid)
    return stops


def when(start, end):
    a, b = (datetime.fromtimestamp(x, timezone.utc) for x in (start, end))
    if (a.year, a.month) == (b.year, b.month):
        return a.strftime("%b %Y")
    if a.year == b.year:
        return f"{a.strftime('%b')}-{b.strftime('%b %Y')}"
    return f"{a.strftime('%b %Y')}-{b.strftime('%b %Y')}"


def name_places(spots):
    """spots: {key: [(lat, lon), ...]} -> {key: "Kyoto"}. Cached, so a trip keeps its name on later runs."""
    places = load(PLACES, {})
    todo = [k for k in spots if k not in places]
    if todo:
        lines = []
        for n, k in enumerate(todo, 1):
            pts = spots[k]
            sample = pts[:: max(1, len(pts) // 6)][:6]
            lines.append(f"{n}: " + "; ".join(f"{la:.3f},{lo:.3f}" for la, lo in sample))
        ans = ask_claude(
            "Each numbered line lists GPS points (latitude,longitude) where someone took photos on one trip, "
            "or at home. Name each place the way a person would title a photo album: the city if the points "
            "are in or near one city, otherwise the region or country (for example \"Kyoto\", \"Bali\", "
            "\"Scottish Highlands\", \"Japan\"). One to three words, in English.\n\n" + "\n".join(lines) +
            "\n\nReply with only a JSON object mapping the line number to the name, like {\"1\": \"Kyoto\"}. "
            "Do not use any tools.", timeout=300) or {}
        for n, k in enumerate(todo, 1):
            name = str(ans.get(str(n), "")).strip().replace("/", "-")[:40]
            if name:
                places[k] = name
        save(PLACES, places)
    return places


def trip_albums(inv, labels, args):
    """{inventory id: trip album name} for items taken on a trip, plus a printable summary."""
    try:
        meta = read_meta()
    except RuntimeError as e:
        print(f"  Could not read dates and places from Photos ({e}); skipping trips.", flush=True)
        return {}
    points = [(t, la, lo) for t, la, lo in meta.values() if la is not None]
    if len(points) < 20:
        print("  Too few photos have a location to find trips; skipping trips.", flush=True)
        return {}
    if args.home:
        home = tuple(float(x) for x in args.home.split(","))
    else:
        home = guess_home(points)
    trips = find_trips(meta, home, args.away_km)

    key = lambda la, lo: f"{la:.1f},{lo:.1f}"  # places inside a trip, and home
    trip_key = lambda tr: "trip " + key(median([p[0] for p in tr["points"]]), median([p[1] for p in tr["points"]])) \
        + f" {len(tr['stops'])}"  # a whole trip can be named for a country, so it never shares a name with a place
    home_key = key(*home)
    spots = {home_key: [home]}
    for tr in trips:
        tr["key"] = trip_key(tr)
        spots.setdefault(tr["key"], tr["points"])
        for center, pids in tr["stops"]:
            spots.setdefault(key(*center), [(la, lo) for _, la, lo, pid in tr["shots"] if pid in pids])
    places = name_places(spots)
    print(f"  Home looks like {places.get(home_key, 'somewhere')} ({home[0]:.2f}, {home[1]:.2f}). If that's wrong, "
          "run again with --home LAT,LON (right-click your home in Apple Maps to copy them).", flush=True)

    names, used = [], {}
    for tr in sorted(trips, key=lambda tr: tr["start"]):
        base = f"{places.get(tr['key'], 'Trip')}, {when(tr['start'], tr['end'])}"
        used[base] = used.get(base, 0) + 1
        names.append((tr, base if used[base] == 1 else f"{base} ({used[base]})"))
        # Photos ids -> place name inside the trip ("Kyoto"), when the trip covers more than one place
        tr["stop_of"] = {pid: places.get(key(*center), "Other places") for center, pids in tr["stops"] for pid in pids}

    out = {}
    for r in inv:
        m = meta.get(r["photos_id"])
        if not m:
            continue
        t, la, lo = m
        for tr, name in names:
            if r["photos_id"] in tr["pids"]:
                pid = r["photos_id"]
            elif la is None and tr["start"] - 3 * 3600 <= t <= tr["end"] + 3 * 3600 \
                    and labels.get(str(r["id"])) not in NOT_TRIP and r["ext"] != ".png":
                # no location (e.g. some videos), but taken during the trip: where were you at that time?
                pid = min(tr["shots"], key=lambda sh: abs(sh[0] - t))[3]
            else:
                continue
            stop = tr["stop_of"].get(pid)
            out[r["id"]] = f"{name}/{stop}" if stop else name
            break
    multi = sum(1 for tr, _ in names if tr["stops"])
    print(f"  Found {len(trips)} trips in the library" + (f" ({multi} with more than one place)" if multi else "")
          + (f"; {len(out)} of the sorted items were taken on one." if trips else "."), flush=True)
    return out


# ---------- maybe delete ----------

def review_bad(inv):
    """Claude looks at the contact sheets and flags shots worth deleting. Returns {inventory id: reason}."""
    state = load(BAD, {"checked": [], "bad": {}, "added": []})
    checked = set(state["checked"])
    sheet_dir = os.path.join(WORK, "sheets")
    sheets = sorted(f for f in os.listdir(sheet_dir) if f.startswith("sheet_")) if os.path.isdir(sheet_dir) else []
    batches = []
    for s in range(0, len(sheets), 3):
        group = sheets[s:s + 3]
        first, last = int(group[0][6:-4]) * 30, (int(group[-1][6:-4]) + 1) * 30
        ids = [r["id"] for r in inv[first:last]]
        if not set(ids) <= checked:
            batches.append(([os.path.join(sheet_dir, g) for g in group], ids))
    if batches:
        print(f"Claude is looking for photos you may want to delete ({len(batches)} batches)...", flush=True)

    def run(batch):
        paths, ids = batch
        ans = ask_claude(
            "Use the Read tool to look at each of these contact sheet images:\n" + "\n".join(paths) + "\n\n"
            "Each sheet shows up to 30 square previews of someone's camera roll, each tagged in yellow with a "
            "number (its id). VIDEO tags are videos with their length. List only the ones the owner would "
            "almost certainly want to delete: badly blurred or out of focus, accidental shots (inside a pocket, "
            "the floor or ceiling, a finger over the lens), almost completely black or white, or videos under "
            "2 seconds that show nothing. Do not flag a photo for being ordinary, a screenshot, a document, "
            "dark on purpose (night sky, concert) or artistic. When unsure, leave it out.\n\n"
            "Reply with only a JSON object mapping id to a short reason, like {\"12\": \"blurry\"}, "
            "or {} if none.")
        return ids, ans

    done = 0
    with ThreadPoolExecutor(4) as ex:
        for ids, ans in ex.map(run, batches):
            done += 1
            if ans is None:
                print(f"  batch {done}/{len(batches)}: no answer, will retry next run", flush=True)
                continue
            valid = {str(i) for i in ids}
            state["bad"].update({k: str(v)[:60] for k, v in ans.items() if k in valid})
            checked.update(ids)
            state["checked"] = sorted(checked)
            save(BAD, state)
            print(f"PROGRESS {done} {len(batches)}", flush=True)
    return {int(k): v for k, v in state["bad"].items()}


# ---------- dashboard ----------

def write_dashboard(inv, cats, labels, dupe_ids, trip_of, bad, live=None):
    """What ./start.sh shows: the same cards as a folder sort, plus trips and "Maybe delete"."""
    save(os.path.join(DATA, "labels.json"), labels)
    shutil.copy(CATEGORIES, os.path.join(DATA, "categories.json"))
    save(os.path.join(DATA, "icloud.json"), {"trips": {str(k): v for k, v in trip_of.items()},
                                             "bad": {str(k): v for k, v in bad.items()},
                                             # deleted in Photos since they were sorted: hidden on the dashboard
                                             "gone": [r["id"] for r in inv if live is not None and r["photos_id"] not in live]})
    if not os.path.exists(os.path.join(DATA, "to_delete.json")):  # filled by "Send to Photos to delete" on the dashboard
        save(os.path.join(DATA, "to_delete.json"), [])
    src = load(os.path.join(DATA, "source.json"), {})
    src["sorted"] = "iCloud Photos"  # tells the dashboard the sort is finished
    save(os.path.join(DATA, "source.json"), src)


# ---------- albums ----------

def add_to_album(sub, name, pids):
    """Adds Photos ids to one album, 50 at a time. If Photos is too busy to answer, waits and tries again."""
    n = 0
    for s in range(0, len(pids), 50):
        for attempt in range(4):
            try:
                n += int(osa(ALBUM_SCRIPT, [sub, name] + pids[s:s + 50]).strip() or 0)
                break
            except (RuntimeError, subprocess.TimeoutExpired) as e:
                if attempt == 3 or not ("-1712" in str(e) or "timed out" in str(e) or isinstance(e, subprocess.TimeoutExpired)):
                    raise
                print(f"    Photos is busy, waiting a minute and trying again ({attempt + 1}/3)...", flush=True)
                time.sleep(60)
        yield pids[s:s + 50], n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="only the first N items in the library (to try it out)")
    ap.add_argument("--batch", type=int, default=40, help="items downloaded at a time (default 40)")
    ap.add_argument("--dry-run", action="store_true", help="sort, but do not add anything to albums")
    ap.add_argument("--home", help="home as LAT,LON if the guess is wrong, e.g. 25.03,121.56")
    ap.add_argument("--away-km", type=float, default=100, help="how far from home counts as a trip (default 100)")
    ap.add_argument("--no-trips", action="store_true", help="no trip albums")
    ap.add_argument("--no-review", action="store_true", help="don't look for photos to delete")
    ap.add_argument("--redo", action="store_true",
                    help="put everything in albums again (delete the Photo sorter folder in Photos first)")
    args = ap.parse_args()
    if sys.platform != "darwin":
        sys.exit("This needs the Photos app, so it only runs on a Mac.")

    if args.redo:  # previews and Claude's answers are kept, only the "already in an album" lists are forgotten
        if os.path.exists(ADDED):
            os.remove(ADDED)
        state = load(BAD, None)
        if state:
            state["added"] = []
            save(BAD, state)
    inv, live = download_and_preview(args.limit, args.batch)
    if not inv:
        sys.exit("Nothing was downloaded from Photos, so there is nothing to sort.")

    py = sys.executable
    subprocess.run([py, os.path.join(HERE, "dupes.py")], check=True)
    shutil.rmtree(os.path.join(WORK, "sheets"), ignore_errors=True)  # rebuild sheets for the current inventory
    subprocess.run([py, os.path.join(HERE, "label_claude.py")], stdin=subprocess.DEVNULL)

    cats, labels, unlabelled = final_labels(inv)
    if unlabelled:
        print(f"Claude did not choose an album for {len(unlabelled)} items. They are not added to any album; "
              "run this again to retry them (finished batches are kept).")
        print("  If it said \"Not logged in\": type  claude  in Terminal, then  /login , sign in, type  /exit , "
              "and run this again.", flush=True)
        if len(unlabelled) == len(inv):
            sys.exit(1)
    dupes = load(os.path.join(DATA, "dupes.json"), [])
    dupe_ids = {d for g in dupes for d in g["dupes"]}
    trip_of = {} if args.no_trips else trip_albums(inv, labels, args)
    bad = {} if args.no_review else review_bad(inv)
    write_dashboard(inv, cats, labels, dupe_ids, trip_of, bad, live)

    # Only items not placed before: albums you tidied by hand are left alone on later runs.
    placed = set(load(ADDED, []))
    albums = {}
    for r in inv:
        if r["photos_id"] in placed or r["photos_id"] not in live:
            continue
        if str(r["id"]) in unlabelled and r["id"] not in dupe_ids and r["id"] not in trip_of:
            continue  # wait for a real label on the next run instead of a catch-all album
        if r["id"] in dupe_ids:
            where = ("", DUPES_ALBUM)
        elif r["id"] in trip_of:  # "Japan, Mar 2025/Kyoto" -> folder Trips/Japan, Mar 2025, album Kyoto
            trip, _, stop = trip_of[r["id"]].partition("/")
            where = (f"{TRIPS}/{trip}", stop) if stop else (TRIPS, trip)
        else:
            where = ("", cats[labels[str(r["id"])]]["label"])
        albums.setdefault(where, []).append(r["photos_id"])
    state = load(BAD, {"checked": [], "bad": {}, "added": []})
    review = [r["photos_id"] for r in inv if r["id"] in bad and r["photos_id"] in live
              and r["photos_id"] not in set(state["added"])]
    if review:
        albums[("", BAD_ALBUM)] = review
    if not albums:
        print("Nothing new to add to albums.")
        return

    verb = "Would add" if args.dry_run else "Adding"
    print(f"{verb} items to albums in the \"{FOLDER}\" folder in Photos:", flush=True)
    order = sorted(albums, key=lambda k: (k[1].startswith("Review"), k[0] != "", k[1]))
    for k in order:
        n = len(albums[k])
        if not args.dry_run:
            for done, n in add_to_album(k[0], k[1], albums[k]):  # saved after every 50, so a stop loses nothing
                if k == ("", BAD_ALBUM):
                    state["added"] = sorted(set(state["added"]) | set(done))
                    save(BAD, state)
                else:
                    placed.update(done)
                    save(ADDED, sorted(placed))
        print(f"  {(k[0].replace('/', ' > ') + ' > ' if k[0] else '') + k[1]:48} {n}", flush=True)
    if not args.dry_run:
        print(f"Done. Open Photos and look under Albums > {FOLDER}. Nothing was deleted; check "
              f"\"{DUPES_ALBUM}\" and \"{BAD_ALBUM}\" and delete what you don't want yourself.")
        print("See it all on the dashboard:  ./start.sh", flush=True)


if __name__ == "__main__":
    main()
