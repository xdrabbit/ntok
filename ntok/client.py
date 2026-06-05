"""Thin client that sends a one-shot command to a running ntok daemon.

Targets either the local Phase 1 daemon (ntok.sock) or the Phase 2 thin-client
daemon (ntok-client.sock), depending on the socket path passed in.
"""

from __future__ import annotations

import socket
import sys
from pathlib import Path

from .daemon import socket_path


def send(command: str, timeout: float = 5.0, path: Path | None = None) -> str:
    path = path or socket_path()
    if not path.exists():
        raise ConnectionError(
            f"daemon not running (no socket at {path}). Start it first."
        )
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(str(path))
        s.sendall(command.encode())
        s.shutdown(socket.SHUT_WR)
        chunks = []
        while True:
            b = s.recv(4096)
            if not b:
                break
            chunks.append(b)
        return b"".join(chunks).decode("utf-8", "replace").strip()
    finally:
        s.close()


def main(command: str, path: Path | None = None) -> int:
    try:
        print(send(command, path=path))
        return 0
    except Exception as e:  # noqa: BLE001
        print(f"ntok: {e}", file=sys.stderr)
        return 1
