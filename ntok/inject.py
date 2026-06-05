"""Type transcribed text into the focused window.

Primary method is ydotool (kernel uinput) which works on any Wayland compositor,
including COSMIC where the virtual-keyboard protocol may be unavailable. A
clipboard-paste mode is also supported for speed with long text.
"""

from __future__ import annotations

import os
import shutil
import subprocess

# Linux input event keycodes (for ydotool key)
KEY_LEFTCTRL = 29
KEY_LEFTSHIFT = 42
KEY_V = 47


def _env() -> dict:
    env = dict(os.environ)
    # ydotoold default socket; install.sh sets this explicitly too.
    env.setdefault("YDOTOOL_SOCKET", f"/run/user/{os.getuid()}/.ydotool_socket")
    return env


def _have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def type_text(text: str, key_delay_ms: int = 4) -> None:
    if not _have("ydotool"):
        raise RuntimeError("ydotool not found. Run install.sh.")
    subprocess.run(
        ["ydotool", "type", "--key-delay", str(key_delay_ms), "--", text],
        check=True,
        env=_env(),
    )


def _wl_copy(text: str) -> None:
    subprocess.run(["wl-copy", "--", text], check=True)


def _wl_paste() -> str:
    try:
        return subprocess.run(
            ["wl-paste", "--no-newline"], capture_output=True, text=True, timeout=2
        ).stdout
    except Exception:
        return ""


def paste_text(text: str, restore_clipboard: bool = True) -> None:
    if not _have("wl-copy") or not _have("ydotool"):
        # Fall back to typing if clipboard tooling is missing.
        type_text(text)
        return
    saved = _wl_paste() if restore_clipboard else None
    _wl_copy(text)
    # Ctrl+V
    subprocess.run(
        ["ydotool", "key",
         f"{KEY_LEFTCTRL}:1", f"{KEY_V}:1", f"{KEY_V}:0", f"{KEY_LEFTCTRL}:0"],
        check=True, env=_env(),
    )
    if restore_clipboard and saved is not None:
        # Give the target app a moment to read the clipboard before restoring.
        subprocess.run(["sleep", "0.15"])
        _wl_copy(saved)


def append_text(text: str, cfg: dict) -> None:
    """Streaming inject sink: type a committed delta as-is.

    The commit engine already handles inter-phrase spacing and the optional
    leading capitalization, so the delta is typed verbatim. We always use type
    mode — clipboard paste would thrash the user's clipboard on every phrase.
    """
    if not text:
        return
    type_text(text, key_delay_ms=cfg["inject"]["key_delay_ms"])


def inject(text: str, cfg: dict) -> None:
    if not text:
        return
    ic = cfg["inject"]
    if ic["capitalize_first"] and text:
        text = text[0].upper() + text[1:]
    if ic["trailing_space"]:
        text = text + " "
    if ic["method"] == "paste":
        paste_text(text, restore_clipboard=ic["restore_clipboard"])
    else:
        type_text(text, key_delay_ms=ic["key_delay_ms"])
