#!/usr/bin/env bash
# ntok LAN seat installer - sets up the thin client daemon on a non-GPU seat.
# Re-runnable. This does not enable the local transcription daemon.
set -euo pipefail

PROJ="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$PROJ/.venv"
USER_UNIT_DIR="$HOME/.config/systemd/user"

say()  { printf '\033[1;36m> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m! %s\033[0m\n' "$*"; }

SERVER_HOST="${1:-blackbird.local}"
SERVER_PORT="${2:-6161}"

say "1/6  Python venv + ntok package"
python3 -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -e "$PROJ"

say "2/6  Linux seat packages (sudo): ydotool, wl-clipboard, libnotify"
if ! command -v ydotool >/dev/null || ! command -v wl-copy >/dev/null; then
  sudo apt-get update -qq
  sudo apt-get install -y ydotool wl-clipboard libnotify-bin
else
  echo "    already installed."
fi

say "3/6  uinput permissions (sudo) so ydotool can synthesize keystrokes"
echo 'uinput' | sudo tee /etc/modules-load.d/uinput.conf >/dev/null
sudo modprobe uinput || true
echo 'KERNEL=="uinput", SUBSYSTEM=="misc", GROUP="input", MODE="0660", OPTIONS+="static_node=uinput"' \
  | sudo tee /etc/udev/rules.d/99-uinput-ntok.rules >/dev/null
sudo udevadm control --reload-rules
sudo udevadm trigger /dev/uinput || true

RELOGIN=0
if ! id -nG "$USER" | tr ' ' '\n' | grep -qx input; then
  say "    adding $USER to the 'input' group"
  sudo usermod -aG input "$USER"
  RELOGIN=1
fi

say "4/6  Installing systemd user services"
mkdir -p "$USER_UNIT_DIR"
sed "s|@VENV@|$VENV|g" "$PROJ/systemd/ntok-client.service" > "$USER_UNIT_DIR/ntok-client.service"
cp "$PROJ/systemd/ydotoold.service" "$USER_UNIT_DIR/ydotoold.service"
systemctl --user daemon-reload
systemctl --user import-environment WAYLAND_DISPLAY XDG_RUNTIME_DIR DISPLAY DBUS_SESSION_BUS_ADDRESS XDG_CURRENT_DESKTOP 2>/dev/null || true
systemctl --user enable ydotoold.service ntok-client.service

say "5/6  Writing default config if missing"
"$VENV/bin/ntok" --help >/dev/null 2>&1 || true
"$VENV/bin/python" -c "from ntok import config; config.write_default_if_missing()"

say "6/6  Set this seat to use $SERVER_HOST:$SERVER_PORT"
python3 - "$HOME/.config/ntok/config.toml" "$SERVER_HOST" "$SERVER_PORT" <<'PY'
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

echo
if [ "$RELOGIN" = "1" ]; then
  warn "You were added to the 'input' group - LOG OUT and back in (or reboot)"
  warn "before starting, so ydotoold can access /dev/uinput."
  echo "After re-login:  systemctl --user start ydotoold ntok-client"
else
  systemctl --user restart ydotoold.service ntok-client.service || true
  echo "Client services started. Server target: $SERVER_HOST:$SERVER_PORT"
fi

cat <<EOF
Bind a hotkey on this seat:
  Command:  $VENV/bin/ntok client toggle

Useful:
  $VENV/bin/ntok client status
  $VENV/bin/ntok client cancel
  journalctl --user -u ntok-client -f
  edit ~/.config/ntok/config.toml then: systemctl --user restart ntok-client
EOF
