"""Microphone capture via PipeWire/PulseAudio (`parec`).

We record raw signed-16-bit mono at 16 kHz straight from the default source into
an in-memory buffer (no temp files), then hand a float32 numpy array to Whisper.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import threading

import numpy as np


class Recorder:
    """Background mic recorder. start() begins capture; stop() returns audio."""

    def __init__(self, sample_rate: int = 16000, source: str = "", max_seconds: int = 300):
        self.sample_rate = sample_rate
        self.source = source
        self.max_bytes = sample_rate * 2 * max_seconds  # 2 bytes/sample, mono
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._recording = False

    @property
    def recording(self) -> bool:
        return self._recording

    def _build_cmd(self) -> list[str]:
        if shutil.which("parec"):
            cmd = [
                "parec",
                "--format=s16le",
                f"--rate={self.sample_rate}",
                "--channels=1",
                "--latency-msec=30",
            ]
            if self.source:
                cmd.append(f"--device={self.source}")
            return cmd
        # Fallback: ffmpeg from PulseAudio
        if shutil.which("ffmpeg"):
            return [
                "ffmpeg", "-loglevel", "quiet",
                "-f", "pulse", "-i", self.source or "default",
                "-ac", "1", "-ar", str(self.sample_rate),
                "-f", "s16le", "-",
            ]
        raise RuntimeError("Need `parec` (pipewire-pulse) or `ffmpeg` to record audio.")

    def _reader(self) -> None:
        assert self._proc and self._proc.stdout
        while True:
            chunk = self._proc.stdout.read(4096)
            if not chunk:
                break
            with self._lock:
                self._buf.extend(chunk)
                if len(self._buf) >= self.max_bytes:
                    break

    def start(self) -> None:
        if self._recording:
            return
        self._buf = bytearray()
        self._proc = subprocess.Popen(
            self._build_cmd(),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()
        self._recording = True

    def stop(self) -> np.ndarray:
        """Stop capture and return mono float32 audio in [-1, 1]."""
        if not self._recording:
            return np.zeros(0, dtype=np.float32)
        self._recording = False
        if self._proc:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        if self._thread:
            self._thread.join(timeout=2)
        with self._lock:
            raw = bytes(self._buf)
            self._buf = bytearray()
        self._proc = None
        if not raw:
            return np.zeros(0, dtype=np.float32)
        pcm = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        return pcm

    def drain(self) -> np.ndarray:
        """Return new audio captured since the last drain, without stopping.

        This is the continuous source for StreamingSession: the reader thread
        keeps filling ``_buf`` while we periodically pull what's accumulated. A
        dangling odd byte (half a sample) is held back for the next drain.

        If the capture process died while we thought we were recording, attempt
        a transparent restart to keep the dictation reliable.
        """
        if self._recording:
            self._ensure_alive()
        with self._lock:
            n = len(self._buf) - (len(self._buf) % 2)
            if n == 0:
                return np.zeros(0, dtype=np.float32)
            raw = bytes(self._buf[:n])
            del self._buf[:n]
        return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0

    def _ensure_alive(self) -> None:
        """Restart capture if the underlying process has died unexpectedly."""
        if not self._proc or self._proc.poll() is None:
            return  # alive or not started
        # Proc died. Restart.
        try:
            self._proc = subprocess.Popen(
                self._build_cmd(),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            # Reader thread may have exited; start a fresh one.
            if self._thread and self._thread.is_alive():
                # old one will exit on its own; start new reader
                pass
            self._thread = threading.Thread(target=self._reader, daemon=True)
            self._thread.start()
        except Exception:
            # Give up silently; next drain or explicit start will surface.
            self._recording = False

    def stop_capture(self) -> None:
        """Stop the mic process but keep the buffered tail for a final drain()."""
        if not self._recording:
            return
        self._recording = False
        if self._proc:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        if self._thread:
            self._thread.join(timeout=2)
        self._proc = None

    def cancel(self) -> None:
        """Abort recording, discarding audio."""
        self.stop()

    def duration(self) -> float:
        with self._lock:
            return len(self._buf) / (self.sample_rate * 2)

    def recent_rms(self, window_ms: int = 300) -> float:
        """Approximate recent RMS level of buffered (un-drained) audio. 0 if none."""
        win = int((window_ms / 1000.0) * self.sample_rate) * 2
        with self._lock:
            if not self._buf:
                return 0.0
            n = len(self._buf) - (len(self._buf) % 2)
            if n == 0:
                return 0.0
            take = min(n, max(2, win))
            raw = bytes(self._buf[-take:])
        pcm = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        return float(np.sqrt(np.mean(pcm * pcm))) if pcm.size else 0.0


def list_input_sources() -> list[dict]:
    """Return a list of plausible microphone input sources.

    Tries pactl (Pulse/PipeWire) first, falls back to parsing pw-cli output.
    Filters out monitor sinks so the list is mic-focused. Each entry has
    ``name`` (for config [audio].source) and a short ``description``.
    """
    sources: list[dict] = []

    # pactl (most common on PipeWire/Pulse setups)
    if shutil.which("pactl"):
        try:
            out = subprocess.check_output(
                ["pactl", "list", "short", "sources"], text=True, stderr=subprocess.DEVNULL
            )
            for line in out.strip().splitlines():
                # Format: <index> <name> <module> <format> <state>
                parts = line.split("\t", 2)
                if len(parts) >= 2:
                    name = parts[1].strip()
                    desc = name
                    if ".monitor" in name.lower():
                        continue
                    sources.append({"name": name, "description": desc})
        except Exception:
            pass

    if sources:
        return sources

    # pw-cli fallback
    if shutil.which("pw-cli"):
        try:
            out = subprocess.check_output(
                ["pw-cli", "list-objects"], text=True, stderr=subprocess.DEVNULL
            )
            # Look for audio sources
            for block in re.split(r"\n\s*object\.", "\n" + out):
                if "Audio/Source" in block or "alsa_input" in block or "node.name" in block:
                    m = re.search(r'node\.name\s*=\s*"([^"]+)"', block)
                    d = re.search(r'node\.description\s*=\s*"([^"]+)"', block)
                    if m:
                        name = m.group(1)
                        if ".monitor" in name:
                            continue
                        sources.append({
                            "name": name,
                            "description": d.group(1) if d else name,
                        })
        except Exception:
            pass

    # dedup while preserving order
    seen = set()
    uniq = []
    for s in sources:
        if s["name"] not in seen:
            seen.add(s["name"])
            uniq.append(s)
    return uniq
