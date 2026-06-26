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
        self.tick_s = s.get("tick_ms", 300) / 1000.0
        self.min_commit_s = max(s.get("min_silence_ms", 350) / 1000.0, 0.35)
        self.max_buffer_s = s.get("max_buffer_seconds", 28)
        self.vad_filter = s.get("vad_filter", False)
        self.final_vad_filter = s.get("final_vad_filter", True)
        self.silence_rms = s.get("silence_rms", 0.008)
        self.min_silence_s = s.get("min_silence_ms", 350) / 1000.0
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
        txt = self.engine.committed_text
        return self._strip_halluc(txt)

    def cancel(self) -> None:
        """Abort: discard the uncommitted tail, emit nothing further."""
        self._cancelled = True
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=30)

    @property
    def transcript(self) -> str:
        return self.engine.committed_text

    _HALLUC_END = (
        "thank you", "thank you.", "thanks", "thank you for watching", "thanks for watching",
        "bye", "okay", "ok", "i don't know", "i dont know", "you", "the end", "that's it",
        "all right", "alright", "goodbye", "have a good day", "see you", "end of message",
        "i'm sorry", "im sorry", "sorry", "hello", "hi there", "excuse me"
    )

    def _strip_halluc(self, txt: str) -> str:
        """Aggressively remove common hallucinations and junk phrases that models
        emit on silence/low confidence (thank you, sorry, URLs, "for more info", etc).
        Applied to every segment for complete elimination of repetitive bad endings."""
        t = txt.strip()
        if not t:
            return t
        junk_phrases = [
            "for more information", "visit www", "www.", "call now", "subscribe",
            "like and subscribe", "check the description", "thank you for your attention"
        ]
        changed = True
        iterations = 0
        while changed and t and iterations < 5:
            changed = False
            iterations += 1
            low = t.lower()
            for h in junk_phrases:
                if h in low:
                    idx = low.find(h)
                    before = t[:idx].rstrip(" ,.!?")
                    after = t[idx+len(h):].lstrip(" ,.!?")
                    t = (before + " " + after).strip() if before and after else (before or after)
                    changed = True
                    low = t.lower()
            for h in self._HALLUC_END:
                if low.endswith(h):
                    t = t[: -len(h)].rstrip(" ,.!?")
                    changed = True
                    break
        # Drop a *standalone* filler the model emits over a silence gap (a lone
        # "Hello." or "Listen.") — but never eat the first word of a real phrase,
        # so only strip when the filler is the entire segment.
        if t.lower().strip(" .,!?") in {"hello", "hi", "hey", "listen", "well", "so", "um", "uh"}:
            t = ""
        low3 = t.lower().strip(".,!? ")
        fillers = {"i", "a", "the", "um", "uh", "ah", "er", "hmm", "mhm", "im", "i'm", "hello", "hi", "listen"}
        if len(low3) <= 3 or low3 in fillers or not any(c.isalpha() for c in low3):
            return ""
        return t.strip()

    # Back-compat alias
    _clean_final = _strip_halluc

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
        # Use a short fixed window (150ms) for quick end-of-speech detection to
        # minimize delay from last word to commit. The min_silence_s is still
        # used for the "phrase end" logic in engine.
        short_win = int(0.15 * self.sample_rate)
        if short_win <= 0 or self._buffer.size < short_win:
            return False
        tail = self._buffer[-short_win:]
        recent = float(np.sqrt(np.mean(tail * tail)))
        overall = float(np.sqrt(np.mean(self._buffer * self._buffer))) or 1e-9
        # More aggressive threshold (0.4 * overall) + short window for low latency.
        return recent < max(self.silence_rms, 0.4 * overall)

    def _trim_trailing_silence(self, buf: np.ndarray) -> np.ndarray:
        """Drop a silent tail so the final flush doesn't transcribe pure silence
        (Whisper hallucinates phrases like 'Thank you.' over it)."""
        if buf.size == 0:
            return buf
        loud = np.flatnonzero(np.abs(buf) > self.silence_rms)
        if loud.size == 0:
            # Very quiet buffer: drop almost everything (keep tiny safety head)
            keep = int(0.15 * self.sample_rate)
            return buf[:max(0, buf.size - keep)]
        # Trim past the last loud sample + a small grace (less grace than before)
        end = min(buf.size, int(loud[-1]) + int(0.08 * self.sample_rate))
        # Also drop any trailing low-energy tail beyond last "real" energy
        tail_start = int(0.6 * self.sample_rate)
        if buf.size > tail_start:
            tail = buf[-tail_start:]
            tail_loud = np.flatnonzero(np.abs(tail) > self.silence_rms * 0.6)
            if tail_loud.size > 0:
                end = min(end, buf.size - (tail_start - int(tail_loud[-1])))
        return buf[:end]

    def _ingest_and_step(self, ended: bool) -> None:
        new = self.source.drain()
        if new is not None and new.size:
            self._buffer = np.concatenate([self._buffer, new])
        if ended:
            self._buffer = self._trim_trailing_silence(self._buffer)
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

        # Use VAD on flush if configured. Always use a (mild) anti-closing prompt
        # to discourage the model from "finishing" a buffer that ends in silence with
        # stock phrases like "thank you".
        if ended:
            use_vf = self.final_vad_filter
        else:
            use_vf = self.vad_filter
        anti = " Speak naturally. Do not add thank you, thanks, or closing phrases at sentence ends."
        final_prompt = (self.engine.prompt() + anti).strip()

        t = time.monotonic()
        raw = self.transcriber.transcribe_segments(
            self._buffer, initial_prompt=final_prompt,
            vad_filter=use_vf,
        )
        self.metrics.tick_compute_s.append(time.monotonic() - t)

        segs = [Segment(s, e, txt) for (s, e, txt) in raw]
        # Aggressively strip known hallucinations from *all* segments (not just tail)
        # to completely eliminate repetitive "thank you", "sorry", junk URLs, etc.
        for i in range(len(segs)):
            cleaned = self._strip_halluc(segs[i].text)
            if cleaned != segs[i].text:
                segs[i] = Segment(segs[i].start, segs[i].end, cleaned)
        trailing = self._trailing_silence() or ended
        res = self.engine.step(
            segs, dur, ended=ended,
            trailing_silence=trailing,
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
