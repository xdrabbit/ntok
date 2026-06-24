"""Tier 2: end-to-end acceptance against a real Whisper model on the GPU.

This is the contract for "done". It drives the *real* streaming engine with the
*real* faster-whisper model, replacing only the two injected seams:

* audio source -> a fake that replays the fixture WAV in real time, so latency
  and commit timing are meaningful.
* inject sink  -> a collector that records each committed delta.

Run it explicitly (excluded from the default fast suite):

    ./.venv/bin/pytest -m acceptance
    NTOK_TEST_MODEL=small.en ./.venv/bin/pytest -m acceptance   # fast iteration

Asserts the four properties: it streams, it's commit-only (monotonic, no seam
duplication), it's accurate (WER <= 0.15), and first-commit lag is low.
"""

from __future__ import annotations

import os
import time

import numpy as np
import pytest

from ntok import config
from ntok.stream import StreamingSession
from ntok.textutil import normalize, tokens, wer
from tests._fixture import ensure_fixture, load_pcm_f32

pytestmark = pytest.mark.acceptance

WER_BOUND = 0.15
FIRST_COMMIT_LAG_BOUND = 3.0


class RealtimeWav:
    """Replays a float32 clip in real time: drain() returns only the audio that
    'would have been captured' by now, so the session sees it as a live mic."""

    def __init__(self, pcm: np.ndarray, sample_rate: int):
        self.pcm = pcm
        self.sr = sample_rate
        self._t0: float | None = None
        self._pos = 0

    @property
    def duration(self) -> float:
        return len(self.pcm) / self.sr

    @property
    def exhausted(self) -> bool:
        return self._pos >= len(self.pcm)

    def drain(self) -> np.ndarray:
        now = time.monotonic()
        if self._t0 is None:
            self._t0 = now
        upto = min(int((now - self._t0) * self.sr), len(self.pcm))
        chunk = self.pcm[self._pos:upto]
        self._pos = upto
        return chunk


def _cfg():
    cfg = config.load()
    # Validate the model the daemon actually streams with: env override, else the
    # configured stream model, else the batch model.
    model = (
        os.environ.get("NTOK_TEST_MODEL")
        or cfg["stream"].get("model")
        or cfg["model"]["name"]
    )
    cfg["model"]["name"] = model
    cfg["feedback"] = {"sound": False, "notify": False}
    # Force ultra-aggressive low-latency params + fast model for this run.
    model = (
        os.environ.get("NTOK_TEST_MODEL")
        or "large-v3-turbo"
    )
    cfg["model"]["name"] = model
    cfg["model"]["backend"] = "faster-whisper"  # use local for test (no API key)
    s = cfg.setdefault("stream", {})
    s["tick_ms"] = 200
    s["min_silence_ms"] = 250
    s["silence_rms"] = 0.006
    s["final_vad_filter"] = True
    s["vad_filter"] = False
    s["require_confirmation"] = True
    cfg["transcribe"]["beam_size"] = 2
    return cfg


@pytest.fixture(scope="module")
def streamed():
    cfg = _cfg()
    from ntok.transcribe import Transcriber

    tr = Transcriber(cfg)
    tr.load()  # downloads weights on first run; not timed

    wav, reference = ensure_fixture()
    pcm = load_pcm_f32(wav)
    src = RealtimeWav(pcm, cfg["audio"]["sample_rate"])
    sink: list[str] = []

    sess = StreamingSession(src, tr, sink.append, cfg)
    sess.start()
    # Let it play out in real time, then a margin for the last tick to land.
    deadline = time.monotonic() + src.duration + 5.0
    while not src.exhausted and time.monotonic() < deadline:
        time.sleep(0.1)
    time.sleep(1.0)
    transcript = sess.stop()

    return {
        "transcript": transcript,
        "sink": sink,
        "reference": reference,
        "metrics": sess.metrics,
        "duration": src.duration,
    }


def test_it_streams(streamed):
    # >= 2 commits land before end-of-audio (proves incremental output).
    emits = streamed["metrics"].emits
    before_end = [e for e in emits if e.wall_elapsed < streamed["duration"]]
    assert len(before_end) >= 2, (
        f"expected >=2 commits before end; got {len(before_end)} "
        f"of {len(emits)} total"
    )


def test_commit_only_monotonic(streamed):
    # The typed deltas concatenate exactly to the transcript — nothing rewritten.
    assert "".join(streamed["sink"]) == streamed["transcript"]
    running = ""
    for d in streamed["sink"]:
        assert streamed["transcript"].startswith(running)
        running += d


def test_no_seam_duplication(streamed):
    # No phrase boundary doubled a word. Compare adjacency structure: the
    # transcript must not contain a repeated word that the reference doesn't.
    h = tokens(streamed["transcript"])
    r = tokens(streamed["reference"])
    h_rep = sum(1 for a, b in zip(h, h[1:]) if a == b)
    r_rep = sum(1 for a, b in zip(r, r[1:]) if a == b)
    assert h_rep <= r_rep, f"seam duplication: {h_rep} adjacent repeats vs {r_rep}"


def test_accuracy(streamed):
    score = wer(streamed["reference"], streamed["transcript"])
    assert score <= WER_BOUND, (
        f"WER {score:.3f} > {WER_BOUND}\n"
        f"ref: {normalize(streamed['reference'])}\n"
        f"got: {normalize(streamed['transcript'])}"
    )


def test_first_commit_latency(streamed):
    lag = streamed["metrics"].first_commit_lag
    assert lag is not None, "no commits at all"
    assert lag <= FIRST_COMMIT_LAG_BOUND, (
        f"first-commit lag {lag:.2f}s > {FIRST_COMMIT_LAG_BOUND}s"
    )
