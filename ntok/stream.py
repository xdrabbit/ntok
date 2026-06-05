"""StreamingSession — drives audio source -> commit engine -> inject sink.

Transport-agnostic by design. The three collaborators are injected:

* ``source``      — ``.drain() -> np.ndarray`` returns new float32 audio since
                    the last call without stopping capture. Locally this wraps
                    the mic Recorder; in Phase 2 it can be a network stream.
* ``transcriber`` — ``.transcribe_segments(audio, initial_prompt) -> [(s,e,text)]``
* ``sink``        — ``callable(delta: str)`` that types/sends committed text.

The session owns a rolling buffer of *un-committed* audio. Each tick it drains
new audio, transcribes the buffer, asks the CommitEngine what's stable, emits
those deltas, and drops the committed audio from the front of the buffer (so we
never re-feed committed speech and never approach Whisper's 30 s window).

Latency is measured honestly: the source plays in real time, so when the first
delta lands we record both wall-elapsed and how much audio-time we've committed.
``commit_lag = wall_elapsed - committed_audio_s`` is how far behind the speaker
we are — it isolates compute + confirmation delay from the unavoidable wait for
the speaker to actually finish a phrase.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

import numpy as np

from .commit import CommitEngine, Segment


@dataclass
class Emit:
    wall_elapsed: float      # seconds since session start
    committed_audio_s: float  # cumulative audio-time committed at this point
    delta: str

    @property
    def commit_lag(self) -> float:
        return self.wall_elapsed - self.committed_audio_s


@dataclass
class Metrics:
    emits: list[Emit] = field(default_factory=list)
    tick_compute_s: list[float] = field(default_factory=list)

    @property
    def first_commit_lag(self) -> float | None:
        return self.emits[0].commit_lag if self.emits else None

    @property
    def commit_count(self) -> int:
        return len(self.emits)


class StreamingSession:
    def __init__(self, source, transcriber, sink, cfg: dict):
        self.source = source
        self.transcriber = transcriber
        self.sink = sink
        self.cfg = cfg
        s = cfg.get("stream", {})
        self.sample_rate = cfg["audio"]["sample_rate"]
        self.tick_s = s.get("tick_ms", 500) / 1000.0
        self.min_commit_s = max(s.get("min_silence_ms", 500) / 1000.0, 0.6)
        self.max_buffer_s = s.get("max_buffer_seconds", 28)
        self.vad_filter = s.get("vad_filter", False)
        self.silence_rms = s.get("silence_rms", 0.01)
        self.min_silence_s = s.get("min_silence_ms", 500) / 1000.0
        self.engine = CommitEngine(
            min_silence_s=s.get("min_silence_ms", 500) / 1000.0,
            require_confirmation=s.get("require_confirmation", True),
            capitalize_first=cfg["inject"].get("capitalize_first", False),
        )

        self._buffer = np.zeros(0, dtype=np.float32)
        self._committed_audio_s = 0.0
        self._t0 = 0.0
        self._stop = threading.Event()
        self._cancelled = False
        self._thread: threading.Thread | None = None
        self.metrics = Metrics()

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        self._t0 = time.monotonic()
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    def stop(self) -> str:
        """Signal end-of-audio, flush the tail, and return the full transcript."""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=30)
        return self.engine.committed_text

    def cancel(self) -> None:
        """Abort: discard the uncommitted tail, emit nothing further."""
        self._cancelled = True
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=30)

    @property
    def transcript(self) -> str:
        return self.engine.committed_text

    # -- worker -------------------------------------------------------------
    def _worker(self) -> None:
        next_tick = time.monotonic()
        while not self._stop.is_set():
            now = time.monotonic()
            if now < next_tick:
                time.sleep(min(0.02, next_tick - now))
                continue
            next_tick += self.tick_s
            self._ingest_and_step(ended=False)
        # End of audio: final drain + flush (unless cancelled).
        if not self._cancelled:
            self._ingest_and_step(ended=True)

    def _trailing_silence(self) -> bool:
        """Is the tail of the buffer acoustically silent? Drives phrase commit
        independently of Whisper's silence-stretched segment timestamps."""
        win = int(self.min_silence_s * self.sample_rate)
        if win <= 0 or self._buffer.size < win:
            return False
        tail = self._buffer[-win:]
        recent = float(np.sqrt(np.mean(tail * tail)))
        overall = float(np.sqrt(np.mean(self._buffer * self._buffer))) or 1e-9
        # Silent if the tail is quiet in absolute terms or far below the
        # utterance's own level (so it adapts to a noisy mic floor).
        return recent < max(self.silence_rms, 0.2 * overall)

    def _ingest_and_step(self, ended: bool) -> None:
        new = self.source.drain()
        if new is not None and new.size:
            self._buffer = np.concatenate([self._buffer, new])
        dur = self._buffer.size / self.sample_rate
        if self._buffer.size == 0:
            return
        if not ended and dur < self.min_commit_s:
            return

        # Safety net: if the speaker never pauses and the buffer approaches
        # Whisper's window, commit leading segments without waiting for
        # confirmation this tick so the buffer can't run away.
        relax = dur > self.max_buffer_s
        prev_conf = self.engine.require_confirmation
        if relax:
            self.engine.require_confirmation = False

        t = time.monotonic()
        raw = self.transcriber.transcribe_segments(
            self._buffer, initial_prompt=self.engine.prompt(),
            vad_filter=self.vad_filter,
        )
        self.metrics.tick_compute_s.append(time.monotonic() - t)

        segs = [Segment(s, e, txt) for (s, e, txt) in raw]
        res = self.engine.step(
            segs, dur, ended=ended,
            trailing_silence=self._trailing_silence(),
        )

        if relax:
            self.engine.require_confirmation = prev_conf

        # All deltas in a step collectively cover audio up to the advance point.
        covered = self._committed_audio_s + res.advance_seconds
        for delta in res.deltas:
            self.metrics.emits.append(
                Emit(time.monotonic() - self._t0, covered, delta)
            )
            self.sink(delta)
        if res.advance_seconds > 0:
            self._committed_audio_s += res.advance_seconds
            drop = int(round(res.advance_seconds * self.sample_rate))
            self._buffer = self._buffer[drop:]
