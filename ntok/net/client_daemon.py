"""ntok-client — the thin per-seat daemon.

Mirrors the Phase 1 daemon's control plane (a unix socket taking
toggle/start/stop/cancel/status), but instead of transcribing locally it streams
mic audio to the blackbird server and types back the committed text it returns.
Bind your OS hotkey to `ntok client toggle`.

Collaborators are injected (recorder, injector, client factory) so the control
logic is unit-tested without a mic, a network, or a GPU.
"""

from __future__ import annotations

import os
import signal
import socket
import sys
import threading
import time
from pathlib import Path

import numpy as np

from .. import config, feedback
from . import seat
from .client import Client


def client_socket_path() -> Path:
    """Control-socket path for the thin client, resolved per-platform.

    Linux uses the systemd ``XDG_RUNTIME_DIR`` (``/run/user/<uid>``). macOS has
    no such dir, so use a fixed per-user dir under ``$HOME``; ``NTOK_RUNTIME_DIR``
    overrides either. The daemon and every ``ntok client`` subcommand call this,
    so the bind path and the status/toggle path always agree.

    NOTE (macOS): do NOT key off ``$TMPDIR`` — a hotkey bound via a sandboxed
    launcher (Shortcuts/Automator) runs with a per-app ``$TMPDIR``
    (``.../T/com.apple.shortcuts.../``) that differs from the daemon's, so it
    would compute a different socket path and never find the running daemon.
    ``$HOME`` is stable across those contexts.
    """
    override = os.environ.get("NTOK_RUNTIME_DIR")
    if override:
        base = Path(override)
    elif sys.platform == "darwin":
        base = Path.home() / ".ntok"
    else:
        xdg = os.environ.get("XDG_RUNTIME_DIR")
        base = Path(xdg) if xdg else Path(f"/run/user/{os.getuid()}")
    return base / "ntok-client.sock"


def _pcm_bytes(pcm: np.ndarray) -> bytes:
    return (np.clip(pcm, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()


class ClientDaemon:
    def __init__(self, cfg=None, recorder=None, injector=None, client_factory=None):
        self.cfg = cfg or config.load()
        self.recorder = recorder or seat.make_recorder(self.cfg)
        self.injector = injector or seat.make_injector(self.cfg)
        self.client_factory = client_factory or (lambda c, on_commit: Client(c, on_commit))
        self.lock = threading.Lock()
        self.client: Client | None = None
        self._streaming = False
        self._pump: threading.Thread | None = None
        self._running = True

    # -- handlers -----------------------------------------------------------
    def cmd_status(self) -> str:
        return "streaming" if self.client is not None else "idle"

    def cmd_start(self) -> str:
        if self.client is not None:
            return "already streaming"
        try:
            c = self.client_factory(self.cfg, self.injector)
            c.connect()
        except Exception as e:  # noqa: BLE001
            if self.cfg["feedback"]["notify"]:
                feedback.notify("ntok: can't reach server", str(e), urgency="critical")
            return f"error: {e}"
        self.client = c
        self.recorder.start()
        self._streaming = True
        self._pump = threading.Thread(target=self._pump_loop, daemon=True)
        self._pump.start()
        if self.cfg["feedback"]["sound"]:
            feedback.play("start")
        if self.cfg["feedback"]["notify"]:
            feedback.notify("Listening…", "Speak — text appears as you talk.")
        return "streaming"

    def _pump_loop(self) -> None:
        while self._streaming:
            pcm = self.recorder.drain()
            if pcm.size:
                self.client.send_audio(_pcm_bytes(pcm))
            time.sleep(0.1)

    def cmd_stop(self) -> str:
        if self.client is None:
            return "not streaming"
        self._streaming = False
        if self._pump:
            self._pump.join(timeout=2)
        self.recorder.stop_capture()
        tail = self.recorder.drain()  # whatever the mic buffered last
        if tail.size:
            self.client.send_audio(_pcm_bytes(tail))
        if self.cfg["feedback"]["sound"]:
            feedback.play("stop")
        self.client.stop()  # waits for server's final flush (deltas injected)
        self.client = None
        return "stopped"

    def cmd_cancel(self) -> str:
        if self.client is None:
            return "nothing to cancel"
        self._streaming = False
        if self._pump:
            self._pump.join(timeout=2)
        self.recorder.cancel()
        self.client.cancel()
        self.client = None
        if self.cfg["feedback"]["notify"]:
            feedback.notify("Cancelled", "Dictation discarded.")
        return "cancelled"

    def cmd_toggle(self) -> str:
        with self.lock:
            if self.client is not None:
                return self.cmd_stop()
            return self.cmd_start()

    def dispatch(self, line: str) -> str:
        cmd = line.strip().lower()
        handlers = {
            "toggle": self.cmd_toggle,
            "start": self.cmd_start,
            "stop": self.cmd_stop,
            "cancel": self.cmd_cancel,
            "status": self.cmd_status,
            "ping": lambda: "pong",
        }
        if cmd == "shutdown":
            self._running = False
            return "bye"
        h = handlers.get(cmd)
        return h() if h else f"unknown command: {cmd}"

    # -- server loop --------------------------------------------------------
    def serve(self) -> None:
        path = client_socket_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            path.unlink()
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(path))
        os.chmod(path, 0o600)
        srv.listen(8)
        srv.settimeout(0.5)
        # SIGINT (Ctrl-C) and SIGTERM (launchd/systemd stop) both just flip the
        # flag; the 0.5s accept timeout lets the loop notice and exit cleanly,
        # so neither dumps a traceback nor leaves the socket behind.
        self._install_signal_handlers()
        net = self.cfg["net"]
        print(f"[ntok-client] ready. server={net['server_host']}:{net['server_port']} "
              f"socket={path}", file=sys.stderr, flush=True)
        try:
            while self._running:
                try:
                    conn, _ = srv.accept()
                except socket.timeout:
                    continue
                with conn:
                    data = conn.recv(65536).decode("utf-8", "replace")
                    if not data:
                        continue
                    try:
                        resp = self.dispatch(data)
                    except Exception as e:  # noqa: BLE001
                        resp = f"error: {e}"
                    conn.sendall((resp + "\n").encode())
        except KeyboardInterrupt:
            pass  # safety net if a signal slips in before the handler is set
        finally:
            srv.close()
            if path.exists():
                path.unlink()
            print("[ntok-client] shut down.", file=sys.stderr, flush=True)

    def _install_signal_handlers(self) -> None:
        """Route SIGINT/SIGTERM to the stop flag. No-op off the main thread
        (e.g. under pytest), where signal.signal would raise."""
        def _stop(*_args):
            self._running = False
        try:
            signal.signal(signal.SIGINT, _stop)
            signal.signal(signal.SIGTERM, _stop)
        except ValueError:
            pass


def run() -> int:
    config.write_default_if_missing()
    ClientDaemon().serve()
    return 0
