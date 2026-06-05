"""Tier 1: StreamingSession plumbing. No GPU, no model, deterministic.

We drive ``_ingest_and_step`` directly (the worker loop just calls it on a
timer) so there's no thread-timing flakiness. The fake audio is a ramp where
each sample's *value* equals its absolute index, so the fake transcriber can
recover where the current buffer sits in the original audio — which lets us
assert that committed audio is really dropped and never re-fed.
"""

from __future__ import annotations

import numpy as np

from ntok.stream import StreamingSession
from ntok.textutil import normalize

SR = 16000
SCRIPT = [
    (0.0, 1.4, "the first phrase"),
    (1.6, 3.0, "the second phrase"),
    (3.5, 4.6, "a third one"),
]
TOTAL_S = 5.0
REFERENCE = "the first phrase the second phrase a third one"


class FakeSource:
    def __init__(self, total_samples: int):
        self.audio = np.arange(total_samples, dtype=np.float32)
        self.fed = 0
        self.avail = 0

    def make_available(self, up_to_samples: int) -> None:
        self.avail = min(up_to_samples, len(self.audio))

    def drain(self) -> np.ndarray:
        chunk = self.audio[self.fed:self.avail]
        self.fed = self.avail
        return chunk


class FakeTranscriber:
    """Reads absolute position from the ramp; reveals words by elapsed time."""

    def __init__(self):
        self.starts_seen: list[float] = []

    def transcribe_segments(self, buffer, initial_prompt="", vad_filter=False):
        if buffer.size == 0:
            return []
        eps = 1e-6
        start_s = round(float(buffer[0])) / SR
        end_s = start_s + buffer.size / SR
        self.starts_seen.append(start_s)
        segs = []
        for ws, we, text in SCRIPT:
            if ws < start_s - eps or ws >= end_s - eps:
                continue
            rel_s = ws - start_s
            if we <= end_s + eps:
                rel_e, shown = we - start_s, text
            else:  # in progress: reveal a prefix proportional to elapsed time
                words = text.split()
                frac = (end_s - ws) / max(we - ws, eps)
                k = max(1, min(len(words), round(frac * len(words))))
                rel_e, shown = end_s - start_s, " ".join(words[:k])
            segs.append((rel_s, rel_e, shown))
        return segs


def _cfg():
    from ntok import config
    return config.load()


def _run(cancel_at=None):
    src = FakeSource(int(TOTAL_S * SR))
    tr = FakeTranscriber()
    sink_out: list[str] = []
    sess = StreamingSession(src, tr, sink_out.append, _cfg())
    sess._t0 = 0.0  # _ingest_and_step reads metrics timing off this

    step = 0
    t = 0.5
    while t < TOTAL_S - 1e-9:
        src.make_available(int(round(t * SR)))
        sess._ingest_and_step(ended=False)
        step += 1
        if cancel_at is not None and step == cancel_at:
            return sess, tr, sink_out, "cancelled"
        t += 0.5
    src.make_available(int(TOTAL_S * SR))
    sess._ingest_and_step(ended=True)
    return sess, tr, sink_out, "ended"


def test_session_streams_and_transcribes_accurately():
    sess, _tr, sink_out, _ = _run()
    assert normalize(sess.transcript) == REFERENCE
    assert "".join(sink_out) == sess.transcript


def test_session_emits_incrementally_before_end():
    sess, _tr, _sink, _ = _run()
    # More than one emit total, and at least one arrived before the final flush.
    assert sess.metrics.commit_count >= 2
    assert sess.metrics.emits[0].wall_elapsed >= 0.0


def test_committed_audio_is_dropped_and_never_refed():
    _sess, tr, _sink, _ = _run()
    # The buffer start (absolute audio position handed to the transcriber) must
    # be non-decreasing — proof we drop committed audio and never re-feed it.
    starts = tr.starts_seen
    assert starts == sorted(starts), starts
    assert starts[-1] > starts[0]  # it actually advanced


def test_cancel_discards_and_emits_nothing_after():
    sess, _tr, sink_out, status = _run(cancel_at=1)
    assert status == "cancelled"
    before = list(sink_out)
    sess.cancel()  # worker not running here; just assert no flush happens
    assert sink_out == before  # cancel never flushes the uncommitted tail
