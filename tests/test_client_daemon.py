"""Tier 1: thin-client daemon state machine. No mic, no network, no GPU."""

from __future__ import annotations

import numpy as np
import pytest

from ntok import config
from ntok.net.client_daemon import ClientDaemon


class FakeRecorder:
    sample_rate = 16000

    def __init__(self):
        self.started = self.capture_stopped = self.cancelled = False

    def start(self):
        self.started = True

    def drain(self):
        return np.full(160, 0.1, dtype=np.float32)

    def stop_capture(self):
        self.capture_stopped = True

    def cancel(self):
        self.cancelled = True


class FakeClient:
    def __init__(self, on_commit, fail=False):
        self.on_commit = on_commit
        self.fail = fail
        self.audio = []
        self.stopped = self.cancelled = False

    def connect(self, timeout=10):
        if self.fail:
            raise ConnectionError("no server")

    def send_audio(self, b):
        self.audio.append(b)

    def stop(self, timeout=30):
        self.on_commit("hello world")  # simulate the server's final flush
        self.stopped = True

    def cancel(self):
        self.cancelled = True


def _daemon(fail=False):
    cfg = config.load()
    cfg["feedback"] = {"sound": False, "notify": False}
    injected: list[str] = []
    rec = FakeRecorder()
    d = ClientDaemon(
        cfg=cfg,
        recorder=rec,
        injector=injected.append,
        client_factory=lambda c, on_commit: FakeClient(on_commit, fail=fail),
    )
    return d, rec, injected


def test_idle():
    d, _r, _i = _daemon()
    assert d.cmd_status() == "idle"


def test_start_streams_and_double_start():
    d, rec, _i = _daemon()
    assert d.cmd_start() == "streaming"
    assert rec.started and d.client is not None
    assert d.cmd_start() == "already streaming"
    d.cmd_cancel()


def test_stop_injects_committed_text():
    d, rec, injected = _daemon()
    d.cmd_start()
    assert d.cmd_stop() == "stopped"
    assert injected == ["hello world"]   # server flush was typed locally
    assert rec.capture_stopped and d.client is None


def test_cancel_injects_nothing():
    d, rec, injected = _daemon()
    d.cmd_start()
    assert d.cmd_cancel() == "cancelled"
    assert injected == [] and rec.cancelled and d.client is None


def test_connect_failure_is_reported():
    d, _r, _i = _daemon(fail=True)
    assert d.cmd_start().startswith("error")
    assert d.client is None


def test_toggle_round_trip():
    d, _r, injected = _daemon()
    assert d.cmd_toggle() == "streaming"
    assert d.cmd_toggle() == "stopped"
    assert injected == ["hello world"]
