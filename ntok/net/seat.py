"""Per-seat platform adapters: mic capture + local keystroke injection.

The networked client is otherwise identical on every OS; only these two pieces
differ. Linux reuses the Phase 1 Recorder (parec) and ydotool injection — both
already exercised on blackbird. macOS uses ffmpeg/avfoundation for the mic and
AppleScript `System Events` keystrokes for injection (app-agnostic: types into
the focused window, web or native).

NOTE: the macOS adapters cannot be tested from this Linux box — verify them on
the Mac (mic device index, and grant the terminal/app Accessibility +
Microphone permissions).
"""

from __future__ import annotations

import json
import platform
import subprocess

from ..audio import Recorder
from ..inject import append_text

IS_MAC = platform.system() == "Darwin"


class _MacRecorder(Recorder):
    """Recorder variant that captures from macOS avfoundation via ffmpeg.

    Set [audio].source to the avfoundation audio device, e.g. ":0". List devices:
        ffmpeg -f avfoundation -list_devices true -i ""
    """

    def _build_cmd(self) -> list[str]:
        device = self.source or ":0"  # ":<audio_index>" (empty video part)
        return [
            "ffmpeg", "-loglevel", "quiet",
            "-f", "avfoundation", "-i", device,
            "-ac", "1", "-ar", str(self.sample_rate),
            "-f", "s16le", "-",
        ]


def make_recorder(cfg: dict) -> Recorder:
    sr = cfg["net"].get("sample_rate", cfg["audio"]["sample_rate"])
    source = cfg["audio"].get("source", "")
    max_seconds = cfg["audio"]["max_seconds"]
    if IS_MAC:
        return _MacRecorder(sample_rate=sr, source=source, max_seconds=max_seconds)
    return Recorder(sample_rate=sr, source=source, max_seconds=max_seconds)


def _mac_inject(delta: str) -> None:
    # AppleScript keystroke; needs Accessibility permission for the host app.
    script = f'tell application "System Events" to keystroke {json.dumps(delta)}'
    subprocess.run(["osascript", "-e", script], check=False)


def make_injector(cfg: dict):
    """Return a callable(delta:str) that types text into the focused window."""
    if IS_MAC:
        return _mac_inject
    return lambda delta: append_text(delta, cfg)
