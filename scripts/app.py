"""Photo Scrapbook: the Mac app.  .venv/bin/python scripts/app.py   (make_app.sh turns this into an .app)

One window with the three steps, so nobody needs Terminal:
  1. Sort new photos     runs icloud_albums.py (only what's new since last time)
  2. Scrapbook           runs scrapbook.py, then opens it (remove photos there and get replacements)
  3. Share               publishes the scrapbook to a private claude.ai link (via Claude Code), shows the
                         link and a QR code to open it on a phone and add it to the home screen
Settings: the scrapbook's title, the photo finish, and using Claude's lighter model to save plan usage.

A small server on 127.0.0.1 (this Mac only) serves the window; it opens in a native window when pywebview is
installed, otherwise in the browser. Jobs run one at a time and stream their progress to the window.
"""
import collections, json, os, re, shutil, signal, subprocess, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(__file__))
from common import DATA, ROOT, WORK, load, save

PORT = int(os.environ.get("APP_PORT", 8795))
HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
SETTINGS = os.path.join(WORK, "app_settings.json")
BOOK = os.path.join(WORK, "scrapbook", "site", "data.json")
URL = os.path.join(WORK, "scrapbook", "url.txt")
DEFAULTS = {"title": "Our Scrapbook", "look": "natural", "light": False}
NAMES = {"sort": "Sorting new photos", "build": "Making the scrapbook", "publish": "Publishing the scrapbook"}

job = {"task": None, "running": False, "code": None, "done": 0, "total": 0, "log": collections.deque(maxlen=60),
       "started": None, "ended": None, "proc": None}
lock = threading.Lock()
side = {}  # the scrapbook preview and the dashboard, started on demand


def settings():
    return {**DEFAULTS, **load(SETTINGS, {})}


def env():
    e = dict(os.environ)
    if settings()["light"]:
        e.update(CLAUDE_MODEL="haiku", CLAUDE_WORKERS="2")
    return e


def commands(task):
    s = settings()
    if task == "sort":
        return [[PY, "-u", os.path.join(HERE, "icloud_albums.py")]]
    if task == "build":
        return [[PY, "-u", os.path.join(HERE, "scrapbook.py"), "--title", s["title"], "--look", s["look"]]]
    if task == "publish":
        claude = shutil.which("claude")
        if not claude:
            raise ValueError("Claude Code isn't installed on this Mac, so it can't publish. Install it from claude.com/claude-code.")
        prompt = ("Publish my scrapbook, following the scrapbook section of CLAUDE.md: the page work/scrapbook/site/index.html "
                  "with data.json, images/ and videos/, updating the artifact in work/scrapbook/url.txt if that file exists. "
                  "Save the link in work/scrapbook/url.txt and reply with only the link.")
        return [[claude, "-p", prompt, "--allowedTools", "Artifact", "Read", "Write", "Glob", "Bash(ls:*)", "Bash(du:*)",
                 "Bash(cat:*)", "--output-format", "text"]]
    raise ValueError("Unknown task")


def run(task):
    with lock:
        if job["running"]:
            raise ValueError("Something is already running. Wait for it to finish, or press Stop.")
        cmds = commands(task)
        job.update(task=task, running=True, code=None, done=0, total=0, started=time.time(), ended=None)
        job["log"].clear()

    def body():
        code = 0
        for cmd in cmds:
            try:
                p = subprocess.Popen(cmd, cwd=ROOT, env=env(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL, text=True, bufsize=1, start_new_session=True)
            except OSError as e:
                job["log"].append(str(e))
                code = 1
                break
            job["proc"] = p
            for line in p.stdout:
                line = line.rstrip()
                m = re.match(r"PROGRESS (\d+) (\d+)", line)
                if m:
                    job["done"], job["total"] = int(m.group(1)), int(m.group(2))
                elif line:
                    job["log"].append(line)
            code = p.wait()
            if code:
                break
        if task == "publish" and code == 0:
            link = re.findall(r"https://claude\.ai/\S+", "\n".join(job["log"]))
            if link and not os.path.exists(URL):
                with open(URL, "w") as f:
                    f.write(link[-1].rstrip(".)"))
        job.update(running=False, code=code, ended=time.time(), proc=None)

    threading.Thread(target=body, daemon=True).start()


def stop():
    p = job.get("proc")
    if p and p.poll() is None:
        os.killpg(p.pid, signal.SIGTERM)
        job["log"].append("Stopped. Everything finished so far is kept; run it again to continue.")


def start_side(name):
    """The scrapbook preview (with remove + replace) or the sorter's dashboard, on their own ports."""
    if name in side and side[name].poll() is None:
        return
    if name == "book":
        cmd = [PY, os.path.join(HERE, "scrapbook.py"), "--preview", "--no-open", "--title", settings()["title"],
               "--look", settings()["look"]]
    else:
        cmd = [PY, os.path.join(HERE, "server.py")]
    side[name] = subprocess.Popen(cmd, cwd=ROOT, env={**env(), "PORT": "8765"}, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, start_new_session=True)
    port = 8790 if name == "book" else 8765
    for _ in range(120):  # the preview may rebuild first
        try:
            import urllib.request
            urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1)
            return
        except Exception:
            time.sleep(.5)


def open_url(url):
    if WINDOW.get("webview"):
        WINDOW["webview"].create_window("Photo Scrapbook", url, width=1280, height=860)
    else:
        subprocess.run(["open", url])


def qr_svg(text):
    try:
        import qrcode
        import qrcode.image.svg
    except ImportError:
        return ""
    img = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathFillImage, box_size=10, border=2)
    return img.to_string(encoding="unicode")


def state():
    inv = load(os.path.join(DATA, "inventory.json"), [])
    ic = load(os.path.join(DATA, "icloud.json"), {}) or {}
    added = os.path.join(WORK, "icloud", "added.json")
    trips = {v.split("/")[0] for v in (ic.get("trips") or {}).values()}
    book = load(BOOK)
    items = [p for c in (book or {}).get("chapters", []) for pg in c["pages"] for p in pg["photos"]]
    url = open(URL).read().strip() if os.path.exists(URL) else ""
    when = lambda p: os.path.getmtime(p) if os.path.exists(p) else None
    j = {k: (list(v) if k == "log" else v) for k, v in job.items() if k != "proc"}
    return {
        "library": {"items": len(inv), "trips": len(trips), "sorted_at": when(added), "icloud": bool(ic)},
        "book": {"chapters": len((book or {}).get("chapters", [])), "photos": sum(p.get("type") != "video" for p in items),
                 "videos": sum(p.get("type") == "video" for p in items), "built_at": when(BOOK),
                 "title": (book or {}).get("title")},
        "share": {"url": url, "qr": qr_svg(url) if url else "", "published_at": when(URL)},
        "job": {**j, "name": NAMES.get(job["task"], ""), "now": time.time()},
        "settings": settings(), "claude": bool(shutil.which("claude")),
    }


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, body, ctype="application/json", code=200):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path == "/":
            return self.send(open(os.path.join(HERE, "app_page.html"), "rb").read(), "text/html; charset=utf-8")
        if self.path == "/api/state":
            return self.send(json.dumps(state()))
        return self.send("{}", code=404)

    def do_POST(self):
        if self.headers.get("Origin") not in (None, f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"):
            return self.send('{"error":"forbidden"}', code=403)
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0) or 0)) or b"{}")
        try:
            if self.path == "/api/run":
                run(body.get("task"))
            elif self.path == "/api/stop":
                stop()
            elif self.path == "/api/settings":
                s = settings()
                s["title"] = (str(body.get("title", s["title"])).strip() or DEFAULTS["title"])[:80]
                s["look"] = body.get("look") if body.get("look") in ("natural", "rich", "none") else s["look"]
                s["light"] = bool(body.get("light", s["light"]))
                save(SETTINGS, s)
            elif self.path == "/api/open":
                what = body.get("what")
                if what == "book":
                    if not os.path.exists(BOOK):
                        raise ValueError("Make the scrapbook first.")
                    start_side("book")
                    open_url("http://127.0.0.1:8790/")
                elif what == "dashboard":
                    start_side("dashboard")
                    open_url("http://127.0.0.1:8765/")
                elif what == "link" and os.path.exists(URL):
                    subprocess.run(["open", open(URL).read().strip()])
                elif what == "photos":
                    subprocess.run(["open", "-a", "Photos"])
            else:
                return self.send('{"error":"not found"}', code=404)
        except ValueError as e:
            return self.send(json.dumps({"error": str(e)}), code=400)
        return self.send(json.dumps(state()))


WINDOW = {}


def main():
    url = f"http://127.0.0.1:{PORT}/"
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    except OSError:  # already running: just show it
        subprocess.run(["open", url])
        return
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        import webview  # pywebview: a real Mac window instead of a browser tab
        WINDOW["webview"] = webview
        webview.create_window("Photo Scrapbook", url, width=1120, height=820, min_size=(760, 600))
        webview.start()
    except ImportError:
        subprocess.run(["open", url])
        print(f"Photo Scrapbook is open at {url} (press Control-C to quit)")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
    finally:
        for p in side.values():
            if p.poll() is None:
                os.killpg(p.pid, signal.SIGTERM)
        stop()


if __name__ == "__main__":
    main()
