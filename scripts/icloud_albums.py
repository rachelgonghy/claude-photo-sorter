"""Sort iCloud Photos into albums, without keeping copies on the Mac.

  .venv/bin/python scripts/icloud_albums.py --limit 200   # try the first 200 items
  .venv/bin/python scripts/icloud_albums.py               # the whole library (resumes where it stopped)
  .venv/bin/python scripts/icloud_albums.py --dry-run     # everything except adding to albums

Mac only. Works with "Optimize Mac Storage" on:
  1. Asks Photos for a small batch of items, which downloads them from iCloud into work/icloud/export/.
  2. Makes the small previews the sorter needs, then deletes the downloaded copies.
  3. Runs dupes.py and label_claude.py on the previews, the same as a normal sort.
  4. Adds every item to an album inside a "Photo sorter" folder in Photos. Duplicates go to
     "Review - duplicates". Albums take no extra iCloud space.
Nothing in the Photos library is moved, edited or deleted. Run it again after adding new photos:
only the new ones are downloaded, sorted and added, and albums you tidied by hand are left alone.
The first run, macOS asks to let Terminal control Photos: click Allow.
"""
import argparse, hashlib, json, os, shutil, subprocess, sys, time

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
INV = os.path.join(DATA, "inventory.json")
FOLDER = "Photo sorter"
DUPES_ALBUM = "Review - duplicates"
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
  set ids to id of every media item
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

# argv: album name, then item ids.
ALBUM_SCRIPT = """
on run argv
  set albumName to item 1 of argv
  tell application "Photos"
    if not (exists folder "%s") then make new folder named "%s"
    set f to folder "%s"
    if not (exists album albumName of f) then make new album named albumName at f
    set a to album albumName of f
    set itms to {}
    repeat with i from 2 to count of argv
      try
        set end of itms to media item id (item i of argv)
      end try
    end repeat
    if (count of itms) > 0 then add itms to a
    return count of itms
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="only the first N items in the library (to try it out)")
    ap.add_argument("--batch", type=int, default=40, help="items downloaded at a time (default 40)")
    ap.add_argument("--dry-run", action="store_true", help="sort, but do not add anything to albums")
    args = ap.parse_args()
    if sys.platform != "darwin":
        sys.exit("This needs the Photos app, so it only runs on a Mac.")

    inv, live = download_and_preview(args.limit, args.batch)
    if not inv:
        sys.exit("Nothing was downloaded from Photos, so there is nothing to sort.")

    py = sys.executable
    subprocess.run([py, os.path.join(HERE, "dupes.py")], check=True)
    shutil.rmtree(os.path.join(WORK, "sheets"), ignore_errors=True)  # rebuild sheets for the current inventory
    r = subprocess.run([py, os.path.join(HERE, "label_claude.py")], stdin=subprocess.DEVNULL)

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
    save(os.path.join(DATA, "labels.json"), labels)
    shutil.copy(CATEGORIES, os.path.join(DATA, "categories.json"))

    # Only items not placed before: albums you tidied by hand are left alone on later runs.
    placed = set(load(ADDED, []))
    albums = {}
    for r in inv:
        if r["photos_id"] in placed or r["photos_id"] not in live:
            continue
        if str(r["id"]) in unlabelled and r["id"] not in dupe_ids:
            continue  # wait for a real label on the next run instead of a catch-all album
        name = DUPES_ALBUM if r["id"] in dupe_ids else cats[labels[str(r["id"])]]["label"]
        albums.setdefault(name, []).append(r["photos_id"])
    if not albums:
        print("Nothing new to add to albums.")
        return

    print(("Would add" if args.dry_run else "Adding") + f" items to albums in the \"{FOLDER}\" folder in Photos:", flush=True)
    for name, pids in sorted(albums.items(), key=lambda kv: kv[0] == DUPES_ALBUM):
        added = 0
        if not args.dry_run:
            for s in range(0, len(pids), 200):
                added += int(osa(ALBUM_SCRIPT, [name] + pids[s:s + 200]).strip() or 0)
                placed.update(pids[s:s + 200])
                save(ADDED, sorted(placed))
        print(f"  {name:28} {len(pids) if args.dry_run else added}", flush=True)
    if not args.dry_run:
        print(f"Done. Open Photos and look under Albums > {FOLDER}. Nothing was deleted; "
              f"check \"{DUPES_ALBUM}\" and delete copies yourself if you want to.")


if __name__ == "__main__":
    main()
