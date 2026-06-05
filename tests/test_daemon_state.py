"""Tier 1: daemon control-plane state machine. No GPU, no mic, no model.

This is the path the user actually presses every day. We stub the mic, the
model, injection, and feedback, then assert the toggle/start/stop/cancel state
transitions — especially that cancel discards without injecting anything.
"""

from __future__ import annotations

import time

import numpy as np
import pytest

from ntok import daemon as daemon_mod


class FakeRecorder:
    def __init__(self):
        self.started = False
        self.capture_stopped = False
        self.cancelled = False

    def start(self):
        self.started = True

    def drain(self):
        return np.zeros(0, dtype=np.float32)

    def stop_capture(self):
        self.capture_stopped = True

    def cancel(self):
        self.cancelled = True


class FakeTranscriber:
    ready = True

    def transcribe_segments(self, buffer, initial_prompt="", vad_filter=False):
        return []


@pytest.fixture
def daemon():
    d = daemon_mod.Daemon.__new__(daemon_mod.Daemon)
    from ntok import config
    d.cfg = config.load()
    d.cfg["feedback"] = {"sound": False, "notify": False}
    d.recorder = FakeRecorder()
    d.transcriber = FakeTranscriber()
    import threading
    d.lock = threading.Lock()
    d.session = None
    d.busy = False
    d._running = True
    return d


def test_status_idle_when_nothing_running(daemon):
    assert daemon.cmd_status() == "idle"


def test_start_then_double_start(daemon):
    assert daemon.cmd_start() == "streaming"
    assert daemon.session is not None
    assert daemon.recorder.started
    assert daemon.cmd_start() == "already streaming"
    daemon.session.cancel()  # cleanup the worker thread


def test_status_reports_streaming(daemon):
    daemon.cmd_start()
    assert daemon.cmd_status().startswith("streaming")
    daemon.session.cancel()


def test_stop_when_idle(daemon):
    assert daemon.cmd_stop() == "not streaming"


def test_toggle_starts_then_stops(daemon):
    assert daemon.cmd_toggle() == "streaming"
    assert daemon.session is not None
    assert daemon.cmd_toggle() == "stopping"
    # _finalize runs on a worker thread; wait for it to clear the session.
    deadline = time.monotonic() + 5
    while daemon.session is not None and time.monotonic() < deadline:
        time.sleep(0.02)
    assert daemon.session is None
    assert daemon.recorder.capture_stopped


def test_cancel_discards_and_injects_nothing(daemon):
    injected: list[str] = []
    daemon._sink = injected.append  # capture anything that would be typed

    daemon.cmd_start()
    assert daemon.cmd_cancel() == "cancelled"
    assert daemon.session is None
    assert daemon.recorder.cancelled
    assert injected == []  # nothing was ever typed


def test_cancel_when_idle(daemon):
    assert daemon.cmd_cancel() == "nothing to cancel"
