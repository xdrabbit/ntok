"""User feedback: subtle sounds + desktop notifications."""

from __future__ import annotations

import shutil
import subprocess

_SOUNDS = {
    # freedesktop sound theme names shipped on most distros
    "start": "/usr/share/sounds/freedesktop/stereo/audio-volume-change.oga",
    "stop": "/usr/share/sounds/freedesktop/stereo/complete.oga",
    "error": "/usr/share/sounds/freedesktop/stereo/dialog-error.oga",
}


def play(kind: str) -> None:
    path = _SOUNDS.get(kind)
    if not path or not shutil.which("paplay"):
        return
    try:
        subprocess.Popen(
            ["paplay", path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def notify(summary: str, body: str = "", icon: str = "audio-input-microphone",
           tag: str = "ntok", urgency: str = "low") -> None:
    if not shutil.which("notify-send"):
        return
    try:
        subprocess.Popen(
            ["notify-send", "-a", "ntok", "-i", icon, "-u", urgency,
             "-h", f"string:x-canonical-private-synchronous:{tag}",
             summary, body],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass
