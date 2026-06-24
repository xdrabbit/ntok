"""ntok transcription server — the central GPU seat on blackbird.

Each client connection gets its own StreamingSession, all sharing one warm
Whisper model. GPU access is serialized by a lock so concurrent seats queue
rather than thrash the single device. Audio arrives as AUDIO frames and is fed
into a per-connection NetworkAudioSource; committed text is streamed back as
COMMIT frames as soon as the engine commits it.

The session seams from Phase 1 (injected source + sink) are exactly what make
this drop-in: the server supplies a network-fed source and a network-returning
sink, and reuses the identical CommitEngine.
"""

from __future__ import annotations

import copy
import hmac
import socket
import sys
import threading

import numpy as np

from ..stream import StreamingSession
from ..transcribe import Transcriber
from . import protocol as p


class NetworkAudioSource:
    """StreamingSession source fed by incoming AUDIO frames (int16 LE PCM)."""

    def __init__(self):
        self._buf = bytearray()
        self._lock = threading.Lock()

    def feed(self, data: bytes) -> None:
        with self._lock:
            self._buf.extend(data)

    def drain(self) -> np.ndarray:
        with self._lock:
            n = len(self._buf) - (len(self._buf) % 2)  # whole samples only
            if n == 0:
                return np.zeros(0, dtype=np.float32)
            raw = bytes(self._buf[:n])
            del self._buf[:n]
        return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


class _LockingTranscriber:
    """Serialize GPU calls so concurrent client sessions don't collide."""

    def __init__(self, transcriber, lock: threading.Lock):
        self._t = transcriber
        self._lock = lock

    def transcribe_segments(self, *args, **kwargs):
        with self._lock:
            return self._t.transcribe_segments(*args, **kwargs)


class Server:
    def __init__(self, cfg: dict, transcriber: Transcriber | None = None):
        self.cfg = cfg
        net = cfg["net"]
        self.host = net["host"]
        self.port = net["port"]
        self.token = str(net.get("token", ""))
        self.transcriber = transcriber or Transcriber(cfg)
        self._gpu_lock = threading.Lock()
        self._srv: socket.socket | None = None
        self._running = False

    def load(self) -> None:
        self.transcriber.load()

    # -- lifecycle ----------------------------------------------------------
    def bind(self) -> int:
        """Bind + listen; returns the actual port (useful when port=0)."""
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.host, self.port))
        srv.listen(16)
        self._srv = srv
        self.port = srv.getsockname()[1]
        return self.port

    def serve_forever(self) -> None:
        if self._srv is None:
            self.bind()
        if not self.token:
            print("[ntok-server] refusing to serve with an empty token "
                  "(set [net].token)", file=sys.stderr, flush=True)
            return
        self._running = True
        print(f"[ntok-server] listening on {self.host}:{self.port}",
              file=sys.stderr, flush=True)
        self._srv.settimeout(0.5)
        try:
            while self._running:
                try:
                    conn, addr = self._srv.accept()
                except socket.timeout:
                    continue
                threading.Thread(target=self._handle, args=(conn, addr),
                                 daemon=True).start()
        finally:
            self._srv.close()

    def stop(self) -> None:
        self._running = False

    # -- per-connection -----------------------------------------------------
    def _handle(self, conn: socket.socket, addr) -> None:
        parser = p.FrameParser()
        send_lock = threading.Lock()
        session: StreamingSession | None = None
        source: NetworkAudioSource | None = None
        authed = False

        def send(frame: bytes) -> None:
            with send_lock:
                try:
                    conn.sendall(frame)
                except OSError:
                    pass

        try:
            while True:
                data = conn.recv(65536)
                if not data:
                    break
                for f in parser.feed(data):
                    if not authed:
                        if f.type != p.HELLO:
                            send(p.encode_json(p.HELLO_ERR, {"reason": "expected HELLO"}))
                            return
                        msg = f.json()
                        if not hmac.compare_digest(str(msg.get("token", "")), self.token):
                            send(p.encode_json(p.HELLO_ERR, {"reason": "bad token"}))
                            return
                        sr = int(msg.get("sample_rate", 16000))
                        cfg = copy.deepcopy(self.cfg)
                        cfg["audio"]["sample_rate"] = sr
                        source = NetworkAudioSource()
                        session = StreamingSession(
                            source,
                            _LockingTranscriber(self.transcriber, self._gpu_lock),
                            lambda delta: send(p.encode_text(p.COMMIT, delta)),
                            cfg,
                        )
                        session.start()
                        authed = True
                        send(p.encode_json(p.HELLO_OK, {}))
                    elif f.type == p.AUDIO:
                        source.feed(f.payload)
                    elif f.type == p.END:
                        session.stop()       # flush tail; sink sends remaining
                        send(p.encode(p.END))  # signal "complete, you have it all"
                        session = None
                        return
                    elif f.type == p.CANCEL:
                        session.cancel()
                        session = None
                        return
                    elif f.type == p.PING:
                        send(p.encode(p.PONG))
        finally:
            if session is not None:
                session.cancel()  # client dropped mid-stream
            conn.close()


def run() -> int:
    from .. import config

    cfg = config.load()
    if not str(cfg["net"].get("token", "")):
        print("[ntok-server] set [net].token in ~/.config/ntok/config.toml first "
              "(shared secret); refusing to serve without one.", file=sys.stderr)
        return 1
    # Use the low-VRAM, fast streaming model by default (same as the daemon).
    if (cfg.get("model", {}).get("backend") or "faster-whisper") != "openai":
        if cfg["stream"].get("model"):
            cfg["model"]["name"] = cfg["stream"]["model"]
    srv = Server(cfg)
    print(f"[ntok-server] loading model {cfg['model']['name']}…",
          file=sys.stderr, flush=True)
    srv.load()
    srv.serve_forever()
    return 0
