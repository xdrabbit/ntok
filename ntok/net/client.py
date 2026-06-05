"""ntok client transport — the network half of a thin seat.

Connects to the blackbird server, authenticates, streams mic PCM up as AUDIO
frames, and delivers committed text deltas to an ``on_commit`` callback as they
arrive. This class is transport-only and hardware-free: the mic capture and the
local keystroke injection are supplied by the client daemon (platform adapters),
so this same code drives a Linux seat and a Mac seat alike.
"""

from __future__ import annotations

import socket
import threading

from . import protocol as p


class Client:
    def __init__(self, cfg: dict, on_commit):
        net = cfg["net"]
        self.host = net["server_host"]
        self.port = net["server_port"]
        self.token = str(net.get("token", ""))
        self.sample_rate = net.get("sample_rate", cfg["audio"]["sample_rate"])
        self.on_commit = on_commit
        self._sock: socket.socket | None = None
        self._parser = p.FrameParser()
        self._reader: threading.Thread | None = None
        self._send_lock = threading.Lock()
        self._done = threading.Event()  # server finished flushing, or closed

    # -- lifecycle ----------------------------------------------------------
    def connect(self, timeout: float = 10.0) -> None:
        self._sock = socket.create_connection((self.host, self.port), timeout=timeout)
        self._send(p.encode_json(p.HELLO, {
            "token": self.token, "sample_rate": self.sample_rate,
        }))
        self._sock.settimeout(timeout)
        authed = False
        while not authed:
            data = self._sock.recv(65536)
            if not data:
                raise ConnectionError("connection closed during handshake")
            for f in self._parser.feed(data):
                if f.type == p.HELLO_OK:
                    authed = True
                elif f.type == p.HELLO_ERR:
                    raise ConnectionError(
                        f"server rejected connection: {f.json().get('reason')}")
        self._sock.settimeout(None)
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        try:
            while True:
                data = self._sock.recv(65536)
                if not data:
                    break
                for f in self._parser.feed(data):
                    if f.type == p.COMMIT:
                        self.on_commit(f.text())
                    elif f.type == p.END:
                        self._done.set()
        except OSError:
            pass
        finally:
            self._done.set()

    # -- audio + control ----------------------------------------------------
    def send_audio(self, pcm_bytes: bytes) -> None:
        self._send(p.encode(p.AUDIO, pcm_bytes))

    def stop(self, timeout: float = 30.0) -> None:
        """End the utterance, wait for the server's final flush, then close."""
        self._send(p.encode(p.END))
        self._done.wait(timeout)
        self.close()

    def cancel(self) -> None:
        try:
            self._send(p.encode(p.CANCEL))
        except OSError:
            pass
        self.close()

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass

    # -- internal -----------------------------------------------------------
    def _send(self, frame: bytes) -> None:
        with self._send_lock:
            self._sock.sendall(frame)
