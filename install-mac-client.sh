#!/usr/bin/env bash
# ntok macOS LAN seat installer. Installs only the thin client path:
# mic capture on the Mac, transcription on blackbird:6161, local text injection.
set -euo pipefail

PROJ="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$PROJ/.venv"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
PLIST="$LAUNCH_AGENTS/io.ritualstack.ntok-client.plist"

SERVER_HOST="${1:-blackbird.local}"
SERVER_PORT="${2:-6161}"

say() { printf '\033[1;36m> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m! %s\033[0m\n' "$*"; }

if [ "$(uname -s)" != "Darwin" ]; then
  echo "install-mac-client.sh is for macOS seats." >&2
  exit 1
fi

say "1/5  Checking ffmpeg for avfoundation mic capture"
if ! command -v ffmpeg >/dev/null; then
  if command -v brew >/dev/null; then
    brew install ffmpeg
  else
    echo "Install Homebrew or ffmpeg first: brew install ffmpeg" >&2
    exit 1
  fi
fi

say "2/5  Python venv + ntok thin client package"
python3 -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -e "$PROJ"

say "3/5  Writing default config if missing"
"$VENV/bin/ntok" --help >/dev/null 2>&1 || true
"$VENV/bin/python" -c "from ntok import config; config.write_default_if_missing()"

say "4/5  Set this Mac to use $SERVER_HOST:$SERVER_PORT"
"$VENV/bin/python" - "$HOME/.config/ntok/config.toml" "$SERVER_HOST" "$SERVER_PORT" <<'PY'
from pathlib import Path
import re
import sys

path = Path(sys.argv[1])
host = sys.argv[2]
port = sys.argv[3]
text = path.read_text()
text = re.sub(r'(?m)^server_host = ".*"$', f'server_host = "{host}"', text)
text = re.sub(r'(?m)^server_port = \d+$', f'server_port = {port}', text)
path.write_text(text)
PY

say "5/5  Installing LaunchAgent"
mkdir -p "$LAUNCH_AGENTS"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>io.ritualstack.ntok-client</string>
  <key>ProgramArguments</key>
  <array>
    <string>$VENV/bin/ntok</string>
    <string>client-daemon</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>$HOME/Library/Logs/ntok-client.out.log</string>
  <key>StandardErrorPath</key>
  <string>$HOME/Library/Logs/ntok-client.err.log</string>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)" "$PLIST" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl kickstart -k "gui/$(id -u)/io.ritualstack.ntok-client"

echo
warn "Grant Microphone and Accessibility permission to the app that runs the hotkey command."
warn "Set [audio].source if ffmpeg's default avfoundation mic is not right."
cat <<EOF
Useful:
  ffmpeg -f avfoundation -list_devices true -i ""
  $VENV/bin/ntok client status
  $VENV/bin/ntok client toggle
  tail -f "$HOME/Library/Logs/ntok-client.err.log"

Bind your Mac hotkey to:
  $VENV/bin/ntok client toggle
EOF
