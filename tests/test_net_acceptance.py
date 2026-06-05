"""Tier 2: full client/server end-to-end over loopback with a real model.

Runs the actual server (warm Whisper model) and a real Client over 127.0.0.1,
streaming the fixture clip in real-time-paced chunks as a mic would. Proves the
network path delivers streamed, accurate, commit-only text — the Phase 2
contract — without needing a second machine.

    ./.venv/bin/pytest -m acceptance tests/test_net_acceptance.py
"""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from ntok import config
from ntok.net.client import Client
from ntok.net.server import Server
from ntok.textutil import normalize, wer
from tests._fixture import ensure_fixture, load_pcm_f32

pytestmark = pytest.mark.acceptance

WER_BOUND = 0.15


@pytest.fixture(scope="module")
def running_server():
    from ntok.transcribe import Transcriber

    cfg = config.load()
    cfg["model"]["name"] = cfg["stream"]["model"] or cfg["model"]["name"]
    cfg["net"]["token"] = "test-secret"
    cfg["net"]["host"] = "127.0.0.1"
    cfg["net"]["port"] = 0
    cfg["feedback"] = {"sound": False, "notify": False}

    srv = Server(cfg, transcriber=Transcriber(cfg))
    srv.load()
    port = srv.bind()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.2)
    yield port
    srv.stop()


@pytest.fixture(scope="module")
def streamed(running_server):
    port = running_server
    wav, reference = ensure_fixture()
    pcm16 = (load_pcm_f32(wav) * 32768.0).astype(np.int16)

    ccfg = config.load()
    ccfg["net"].update({"server_host": "127.0.0.1", "server_port": port,
                        "token": "test-secret", "sample_rate": 16000})

    t0 = time.monotonic()
    commits: list[tuple[float, str]] = []
    c = Client(ccfg, on_commit=lambda d: commits.append((time.monotonic() - t0, d)))
    c.connect(timeout=10)

    # Stream in real time, 0.25 s chunks, like a live mic.
    chunk = 16000 // 4
    for i in range(0, len(pcm16), chunk):
        c.send_audio(pcm16[i:i + chunk].tobytes())
        time.sleep(0.25)
    t_send_done = time.monotonic() - t0
    c.stop(timeout=30)

    return {
        "commits": commits,
        "reference": reference,
        "transcript": "".join(d for _t, d in commits),
        "send_done": t_send_done,
    }


def test_streams_over_network(streamed):
    commits = streamed["commits"]
    assert len(commits) >= 2, f"expected streaming commits, got {commits}"
    before_end = [c for c in commits if c[0] < streamed["send_done"]]
    assert len(before_end) >= 1, "no commit arrived before end-of-audio"


def test_commit_only_over_network(streamed):
    # Deltas only append; the transcript is exactly their concatenation.
    assert streamed["transcript"] == "".join(d for _t, d in streamed["commits"])


def test_accuracy_over_network(streamed):
    score = wer(streamed["reference"], streamed["transcript"])
    assert score <= WER_BOUND, (
        f"WER {score:.3f} > {WER_BOUND}\n"
        f"ref: {normalize(streamed['reference'])}\n"
        f"got: {normalize(streamed['transcript'])}"
    )
