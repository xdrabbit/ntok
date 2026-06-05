"""Microphone capture via PipeWire/PulseAudio (`parec`).

We record raw signed-16-bit mono at 16 kHz straight from the default source into
an in-memory buffer (no temp files), then hand a float32 numpy array to Whisper.
"""

from __future__ import annotations

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
        """
        with self._lock:
            n = len(self._buf) - (len(self._buf) % 2)
            if n == 0:
                return np.zeros(0, dtype=np.float32)
            raw = bytes(self._buf[:n])
            del self._buf[:n]
        return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0

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
