#!/bin/bash
# Makes "Photo Scrapbook.app" in ~/Applications: double-click it to sort photos, make the scrapbook and share it,
# without Terminal. Run once (and again after moving this folder):  ./make_app.sh
set -e
cd "$(dirname "$0")"
REPO="$(pwd)"
APP="$HOME/Applications/Photo Scrapbook.app"
[ "$(uname)" = "Darwin" ] || { echo "This makes a Mac app, so run it on the Mac."; exit 1; }
[ -x .venv/bin/python ] || ./setup.sh

echo "Installing the app window and QR codes (a minute or two the first time)..."
.venv/bin/pip install -q pywebview qrcode || echo "  (couldn't install the window; the app will open in your browser instead)"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

# the app runs scripts/app.py from this folder, with the same PATH as Terminal (Homebrew, Claude Code, ffmpeg)
cat > "$APP/Contents/MacOS/PhotoScrapbook" <<EOF
#!/bin/bash
LOGIN_PATH="\$(/bin/zsh -lc 'printf %s "\$PATH"' 2>/dev/null)"
export PATH="\$LOGIN_PATH:/opt/homebrew/bin:/usr/local/bin:\$HOME/.local/bin:\$HOME/.claude/local:\$PATH"
cd "$REPO" || exit 1
exec "$REPO/.venv/bin/python" "$REPO/scripts/app.py"
EOF
chmod +x "$APP/Contents/MacOS/PhotoScrapbook"

cat > "$APP/Contents/Info.plist" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Photo Scrapbook</string>
  <key>CFBundleDisplayName</key><string>Photo Scrapbook</string>
  <key>CFBundleIdentifier</key><string>local.claude-photo-sorter.scrapbook</string>
  <key>CFBundleExecutable</key><string>PhotoScrapbook</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSAppleEventsUsageDescription</key><string>Photo Scrapbook asks Photos for your pictures so it can sort them into albums and make your scrapbook.</string>
  <key>NSPhotoLibraryUsageDescription</key><string>Photo Scrapbook downloads the photos it needs from iCloud to sort them and make your scrapbook. Nothing is changed or deleted.</string>
</dict></plist>
EOF

# the icon: a print with a horizon and a low sun, on the scrapbook's near-black
.venv/bin/python - "$APP/Contents/Resources" <<'EOF'
import os, subprocess, sys, tempfile
from PIL import Image, ImageDraw
out = sys.argv[1]
S = 1024
im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(im)
d.rounded_rectangle([40, 40, S - 40, S - 40], 220, fill=(11, 11, 13))
d.rounded_rectangle([210, 250, 814, 774], 36, fill=(246, 246, 247))          # the print
d.rounded_rectangle([250, 290, 774, 694], 18, fill=(205, 214, 220))           # its sky
d.ellipse([560, 390, 680, 510], fill=(244, 196, 120))                         # low sun
d.polygon([(250, 600), (380, 520), (470, 575), (600, 490), (774, 590), (774, 694), (250, 694)], fill=(74, 92, 86))
d.rectangle([250, 640, 774, 694], fill=(52, 66, 62))
tmp = tempfile.mkdtemp(suffix=".iconset")
for s in (16, 32, 128, 256, 512):
    im.resize((s, s), Image.LANCZOS).save(os.path.join(tmp, f"icon_{s}x{s}.png"))
    im.resize((s * 2, s * 2), Image.LANCZOS).save(os.path.join(tmp, f"icon_{s}x{s}@2x.png"))
subprocess.run(["iconutil", "-c", "icns", tmp, "-o", os.path.join(out, "AppIcon.icns")], check=True)
EOF

touch "$APP"
echo "Done: Photo Scrapbook is in your Applications folder (in your home folder)."
open -R "$APP"
