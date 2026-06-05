#!/usr/bin/env bash
# ntok installer — sets up the venv, system deps, device permissions, and
# systemd user services. Re-runnable (idempotent).
set -euo pipefail

PROJ="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$PROJ/.venv"
USER_UNIT_DIR="$HOME/.config/systemd/user"

say()  { printf '\033[1;36m▸ %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m! %s\033[0m\n' "$*"; }

# ----------------------------------------------------------------------------
say "1/6  Python venv + dependencies (this downloads CUDA libs, ~2 GB once)"
python3 -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -e "$PROJ"

# ----------------------------------------------------------------------------
say "2/6  System packages (sudo): ydotool, wl-clipboard, libnotify"
if ! command -v ydotool >/dev/null || ! command -v wl-copy >/dev/null; then
  sudo apt-get update -qq
  sudo apt-get install -y ydotool wl-clipboard libnotify-bin
else
  echo "    already installed."
fi

# ----------------------------------------------------------------------------
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

# ----------------------------------------------------------------------------
say "4/6  Installing systemd user services"
mkdir -p "$USER_UNIT_DIR"
sed "s|@VENV@|$VENV|g" "$PROJ/systemd/ntokd.service" > "$USER_UNIT_DIR/ntokd.service"
cp "$PROJ/systemd/ydotoold.service" "$USER_UNIT_DIR/ydotoold.service"
systemctl --user daemon-reload
# Make sure the user services can see the Wayland session
systemctl --user import-environment WAYLAND_DISPLAY XDG_RUNTIME_DIR DISPLAY DBUS_SESSION_BUS_ADDRESS XDG_CURRENT_DESKTOP 2>/dev/null || true
systemctl --user enable ydotoold.service ntokd.service

# ----------------------------------------------------------------------------
say "5/6  Writing default config (~/.config/ntok/config.toml)"
"$VENV/bin/ntok" --help >/dev/null 2>&1 || true
"$VENV/bin/python" -c "from ntok import config; config.write_default_if_missing()"

# ----------------------------------------------------------------------------
say "6/6  Done."
echo
if [ "$RELOGIN" = "1" ]; then
  warn "You were added to the 'input' group — LOG OUT and back in (or reboot)"
  warn "before starting, so ydotoold can access /dev/uinput."
  echo "After re-login:  systemctl --user start ydotoold ntokd"
else
  systemctl --user restart ydotoold.service ntokd.service || true
  echo "Services started. First model load takes a few seconds."
fi
echo
cat <<EOF
─────────────────────────────────────────────────────────────────────────
Bind a hotkey to dictate:
  COSMIC Settings → Keyboard → Keyboard shortcuts → Custom shortcuts → +
    Command:  $VENV/bin/ntok toggle
    Shortcut: press your chosen key (e.g. Super+D)

Then: press the key, talk, press it again → text appears where your cursor is.

Useful:
  ntok status          # idle | recording | transcribing | loading
  ntok cancel          # discard the current recording
  journalctl --user -u ntokd -f   # watch the daemon log
  edit ~/.config/ntok/config.toml then: systemctl --user restart ntokd
─────────────────────────────────────────────────────────────────────────
EOF
