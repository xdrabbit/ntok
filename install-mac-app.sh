#!/usr/bin/env bash
# Build a code-signed macOS .app that runs the ntok thin client daemon.
#
# Why an .app instead of the LaunchAgent: a launchd-spawned process can never be
# granted Microphone or Accessibility (TCC) permission — launchd has no GUI
# session to show the consent prompt, so macOS silently denies it and feeds
# ffmpeg digital silence while blocking AppleScript keystrokes. A real .app
# bundle, launched via LaunchServices (Finder / Login Items), becomes its own
# "responsible process", so the permissions it's granted stick and propagate to
# the ffmpeg/osascript it spawns. Ad-hoc code signing gives TCC a stable code
# identity to remember the grants against.
set -euo pipefail

PROJ="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$PROJ/.venv"
NTOK_BIN="$VENV/bin/ntok"
APP_DIR="$HOME/Applications"
APP="$APP_DIR/Ntok Dictation.app"
BUNDLE_ID="io.ritualstack.ntok-dictation"

say()  { printf '\033[1;36m> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m! %s\033[0m\n' "$*"; }

[ "$(uname -s)" = "Darwin" ] || { echo "macOS only." >&2; exit 1; }
[ -x "$NTOK_BIN" ] || { echo "ntok venv not found at $NTOK_BIN — run install-mac-client.sh first." >&2; exit 1; }

say "Building $APP"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"

cat > "$APP/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Ntok Dictation</string>
  <key>CFBundleDisplayName</key><string>Ntok Dictation</string>
  <key>CFBundleIdentifier</key><string>$BUNDLE_ID</string>
  <key>CFBundleExecutable</key><string>ntok-dictation</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleInfoDictionaryVersion</key><string>6.0</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
  <key>LSUIElement</key><true/>
  <key>NSMicrophoneUsageDescription</key>
  <string>ntok captures microphone audio for live dictation.</string>
</dict>
</plist>
EOF

# The bundle's main executable. It stays alive as the long-running app process
# (no exec) so the app's signed identity remains the TCC responsible process for
# the ffmpeg capture and osascript keystrokes the daemon spawns.
cat > "$APP/Contents/MacOS/ntok-dictation" <<EOF
#!/bin/bash
exec >> "\$HOME/Library/Logs/ntok-dictation.log" 2>&1
echo "[\$(date)] ntok-dictation app starting"
"$NTOK_BIN" client-daemon
EOF
chmod +x "$APP/Contents/MacOS/ntok-dictation"

if command -v codesign >/dev/null; then
  say "Ad-hoc code signing (stable TCC identity)"
  codesign --force --sign - --identifier "$BUNDLE_ID" "$APP"
  codesign --verify --verbose "$APP" || warn "codesign verify reported issues (usually fine for ad-hoc)."
else
  warn "codesign not found (install Xcode CLT: xcode-select --install). The app will still run, but TCC grants may be less stable across rebuilds."
fi

# Register with LaunchServices so it shows up by name in permission dialogs.
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$APP" 2>/dev/null || true

cat <<EOF

$(say "Built: $APP")

Next steps (one-time):
  1. Launch it once:        open "$APP"
  2. Trigger a capture so the Microphone prompt appears:
        $NTOK_BIN client toggle      # speak, then toggle again
     Click "Allow" on "Ntok Dictation would like to access the Microphone".
  3. Grant Accessibility (needed to type): System Settings -> Privacy & Security
     -> Accessibility -> + -> add "$APP" -> toggle ON.
  4. Run at login: System Settings -> General -> Login Items -> + -> add
     "Ntok Dictation".  (Login Items launch via LaunchServices, which preserves
     the TCC permissions — unlike a launchd LaunchAgent.)

Bind your hotkey to:  $NTOK_BIN client toggle
Logs:                 ~/Library/Logs/ntok-dictation.log
EOF
