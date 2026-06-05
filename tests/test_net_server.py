"""Tier 1: client/server over real loopback sockets, no GPU.

A fake transcriber returns a fixed phrase, so this exercises the full network
path — auth handshake, framing, audio upload, commit return, end/cancel — at
millisecond speed without loading a model.
"""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from ntok import config
from ntok.net.client import Client
from ntok.net.server import Server


class FakeTranscriber:
    """Returns one fixed segment spanning the buffer, regardless of content."""

    def transcribe_segments(self, buffer, initial_prompt="", vad_filter=False):
        if buffer.size == 0:
            return []
        return [(0.0, buffer.size / 16000, "hello world")]


def _cfg(token="secret"):
    cfg = config.load()
    cfg["net"]["token"] = token
    cfg["net"]["host"] = "127.0.0.1"
    cfg["net"]["port"] = 0  # ephemeral
    cfg["feedback"] = {"sound": False, "notify": False}
    return cfg


@pytest.fixture
def server():
    cfg = _cfg()
    srv = Server(cfg, transcriber=FakeTranscriber())
    port = srv.bind()
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    time.sleep(0.1)
    yield srv, port
    srv.stop()


def _client_cfg(port, token="secret"):
    cfg = _cfg(token)
    cfg["net"]["server_host"] = "127.0.0.1"
    cfg["net"]["server_port"] = port
    return cfg


def _tone(seconds=1.0, sr=16000, amp=3000):
    n = int(seconds * sr)
    return (np.ones(n, dtype=np.int16) * amp).tobytes()


def test_bad_token_is_rejected(server):
    _srv, port = server
    c = Client(_client_cfg(port, token="wrong"), on_commit=lambda d: None)
    with pytest.raises(ConnectionError):
        c.connect(timeout=5)


def test_handshake_ok(server):
    _srv, port = server
    c = Client(_client_cfg(port), on_commit=lambda d: None)
    c.connect(timeout=5)
    c.cancel()


def test_audio_to_commit_roundtrip(server):
    _srv, port = server
    got: list[str] = []
    c = Client(_client_cfg(port), on_commit=got.append)
    c.connect(timeout=5)
    c.send_audio(_tone(1.0))
    c.stop(timeout=10)
    assert "".join(got).strip() == "hello world"


def test_cancel_is_clean(server):
    _srv, port = server
    got: list[str] = []
    c = Client(_client_cfg(port), on_commit=got.append)
    c.connect(timeout=5)
    c.send_audio(_tone(0.5))
    c.cancel()  # should not raise
