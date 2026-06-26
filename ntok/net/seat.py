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

import os
import platform
import shutil
import subprocess

from ..audio import Recorder
from ..inject import append_text

IS_MAC = platform.system() == "Darwin"

# launchd gives a LaunchAgent a minimal PATH (/usr/bin:/bin:/usr/sbin:/sbin) that
# excludes Homebrew, so a bare "ffmpeg" isn't found even after `brew install`.
_FFMPEG_FALLBACKS = (
    "/opt/homebrew/bin/ffmpeg",  # Apple Silicon Homebrew
    "/usr/local/bin/ffmpeg",     # Intel Homebrew
    "/opt/local/bin/ffmpeg",     # MacPorts
)


def _find_ffmpeg(cfg: dict | None = None) -> str:
    """Resolve ffmpeg to an absolute path. Honors [audio].ffmpeg / $NTOK_FFMPEG,
    then PATH, then known Homebrew/MacPorts locations (PATH is bare under launchd).
    """
    override = (cfg or {}).get("audio", {}).get("ffmpeg") or os.environ.get("NTOK_FFMPEG")
    if override:
        return override
    found = shutil.which("ffmpeg")
    if found:
        return found
    for p in _FFMPEG_FALLBACKS:
        if os.path.exists(p):
            return p
    raise FileNotFoundError(
        "ffmpeg not found. Install it (brew install ffmpeg) or set [audio].ffmpeg "
        "in ~/.config/ntok/config.toml to its absolute path "
        "(e.g. /opt/homebrew/bin/ffmpeg)."
    )


class _MacRecorder(Recorder):
    """Recorder variant that captures from macOS avfoundation via ffmpeg.

    Set [audio].source to the avfoundation audio device, e.g. ":0". List devices:
        ffmpeg -f avfoundation -list_devices true -i ""
    """

    def _build_cmd(self) -> list[str]:
        device = self.source or ":0"  # ":<audio_index>" (empty video part)
        cmd = [
            getattr(self, "ffmpeg", "ffmpeg"), "-loglevel", "quiet",
            "-f", "avfoundation", "-i", device,
            "-ac", "1", "-ar", str(self.sample_rate),
        ]
        gain_db = getattr(self, "gain_db", 0.0)
        if gain_db:
            # Fixed gain to lift a quiet interface, plus a brick-wall limiter so the
            # boost can't clip on loud transients. alimiter is causal (~5 ms attack),
            # so this stays safe for low-latency streaming.
            cmd += ["-af", f"volume={gain_db}dB,alimiter=limit=0.95"]
        cmd += ["-f", "s16le", "-"]
        return cmd


def make_recorder(cfg: dict) -> Recorder:
    sr = cfg["net"].get("sample_rate", cfg["audio"]["sample_rate"])
    source = cfg["audio"].get("source", "")
    max_seconds = cfg["audio"]["max_seconds"]
    if IS_MAC:
        rec = _MacRecorder(sample_rate=sr, source=source, max_seconds=max_seconds)
        rec.ffmpeg = _find_ffmpeg(cfg)  # absolute path so launchd's bare PATH is moot
        rec.gain_db = float(cfg["audio"].get("gain_db", 0.0))
        return rec
    return Recorder(sample_rate=sr, source=source, max_seconds=max_seconds)


_MAC_KEYSTROKE_SCRIPT = (
    "on run argv\n"
    "  tell application \"System Events\" to keystroke (item 1 of argv)\n"
    "end run"
)


def _mac_inject(delta: str) -> None:
    # AppleScript keystroke; needs Accessibility permission for the host app.
    # Pass the text as an argv item rather than interpolating it into the script
    # source: that sidesteps AppleScript's string-literal parser, which rejects
    # the \uXXXX escapes json.dumps emits for non-ASCII (smart quotes, em dashes,
    # etc.) and was dropping those words with a -2741 syntax error.
    subprocess.run(["osascript", "-e", _MAC_KEYSTROKE_SCRIPT, delta], check=False)


def make_injector(cfg: dict):
    """Return a callable(delta:str) that types text into the focused window."""
    if IS_MAC:
        return _mac_inject
    return lambda delta: append_text(delta, cfg)
