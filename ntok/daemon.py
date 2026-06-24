"""ntokd — the warm dictation daemon.

Keeps the Whisper model loaded in VRAM and serves commands over a unix socket:
    toggle | start | stop | cancel | status | ping | shutdown

Bind a keyboard shortcut in your desktop to `ntok toggle` and you're dictating.
"""

from __future__ import annotations

import os
import socket
import sys
import threading
from pathlib import Path

from . import config, feedback
from .audio import Recorder
from .inject import append_text
from .stream import StreamingSession
from .transcribe import Transcriber


def socket_path() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    return Path(runtime) / "ntok.sock"


class _MicSource:
    """Adapts the Recorder to the StreamingSession source interface."""

    def __init__(self, recorder: Recorder):
        self._recorder = recorder

    def drain(self):
        return self._recorder.drain()


class Daemon:
    def __init__(self):
        self.cfg = config.load()
        # Optional streaming model override (e.g. a faster model for low latency).
        # Only for local backend; for openai the name is the api model id (whisper-1).
        if (self.cfg.get("model", {}).get("backend") or "faster-whisper") != "openai":
            stream_model = self.cfg["stream"].get("model")
            if stream_model:
                self.cfg["model"]["name"] = stream_model
        self.recorder = Recorder(
            sample_rate=self.cfg["audio"]["sample_rate"],
            source=self.cfg["audio"]["source"],
            max_seconds=self.cfg["audio"]["max_seconds"],
        )
        self.transcriber = Transcriber(self.cfg)
        self.lock = threading.Lock()
        self.session: StreamingSession | None = None
        self.busy = False  # finalizing a stop (flushing the tail)
        self._running = True

    # ---- command handlers -------------------------------------------------
    def cmd_status(self) -> str:
        if not self.transcriber.ready:
            return "loading"
        if self.session is not None:
            m = self.session.metrics
            words = len(self.session.transcript.split())
            base = f"streaming {m.commit_count} commits / {words} words"
            try:
                rms = self.recorder.recent_rms()
                hearing = " (hearing)" if rms > 0.015 else ""
            except Exception:
                hearing = ""
            return base + hearing
        if self.busy:
            return "finalizing"
        return "idle"

    def _sink(self, delta: str) -> None:
        try:
            append_text(delta, self.cfg)
        except Exception as e:  # noqa: BLE001
            feedback.play("error")
            feedback.notify("Insert failed", str(e), urgency="critical")
            print(f"[ntokd] inject error: {e}", file=sys.stderr, flush=True)

    def cmd_start(self) -> str:
        if self.session is not None:
            return "already streaming"
        self.recorder.start()
        self.session = StreamingSession(
            _MicSource(self.recorder), self.transcriber, self._sink, self.cfg
        )
        self.session.start()
        if self.cfg["feedback"]["sound"]:
            feedback.play("start")
        if self.cfg["feedback"]["notify"]:
            feedback.notify("Listening…", "Speak — text appears as you talk.")
        return "streaming"

    def cmd_stop(self) -> str:
        if self.session is None:
            return "not streaming"
        if self.cfg["feedback"]["sound"]:
            feedback.play("stop")
        # Finalize off the socket thread so status stays responsive while the
        # tail flushes (one last transcription).
        threading.Thread(target=self._finalize, daemon=True).start()
        return "stopping"

    def _finalize(self) -> None:
        self.busy = True
        session = self.session
        cont = bool(self.cfg.get("stream", {}).get("continuous_listen"))
        try:
            self.recorder.stop_capture()  # stop mic, keep the buffered tail
            text = session.stop() if session else ""
            if self.cfg["feedback"]["notify"]:
                n = session.metrics.commit_count if session else 0
                preview = text if len(text) <= 60 else text[:57] + "…"
                feedback.notify("Done", f"“{preview}”  ({n} commits)")
            print(f"[ntokd] streamed -> {text!r}", file=sys.stderr, flush=True)
        finally:
            self.session = None
            self.busy = False
        if cont:
            # Re-arm for continuous hands-free listening
            try:
                self.cmd_start()
            except Exception:
                pass

    def cmd_cancel(self) -> str:
        if self.session is None:
            return "nothing to cancel"
        self.recorder.cancel()
        self.session.cancel()
        self.session = None
        if self.cfg["feedback"]["notify"]:
            feedback.notify("Cancelled", "Dictation discarded.")
        return "cancelled"

    def cmd_toggle(self) -> str:
        with self.lock:
            if self.session is not None:
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

    # ---- server loop ------------------------------------------------------
    def serve(self) -> None:
        path = socket_path()
        if path.exists():
            path.unlink()
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(path))
        os.chmod(path, 0o600)
        srv.listen(8)
        srv.settimeout(0.5)

        print("[ntokd] loading model "
              f"{self.cfg['model']['name']} on {self.cfg['model']['device']}…",
              file=sys.stderr, flush=True)
        self.transcriber.load()
        print("[ntokd] ready. socket:", path, file=sys.stderr, flush=True)
        if self.cfg["feedback"]["notify"]:
            feedback.notify("ntok ready", "Dictation daemon loaded.")

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
        finally:
            srv.close()
            if path.exists():
                path.unlink()
            print("[ntokd] stopped.", file=sys.stderr, flush=True)


def run() -> int:
    config.write_default_if_missing()
    Daemon().serve()
    return 0
