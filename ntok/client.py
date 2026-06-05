"""Thin client that sends a one-shot command to the running daemon."""

from __future__ import annotations

import socket
import sys

from .daemon import socket_path


def send(command: str, timeout: float = 5.0) -> str:
    path = socket_path()
    if not path.exists():
        raise ConnectionError(
            "ntokd not running. Start it with `systemctl --user start ntokd` "
            "or `ntok daemon`."
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


def main(command: str) -> int:
    try:
        print(send(command))
        return 0
    except Exception as e:  # noqa: BLE001
        print(f"ntok: {e}", file=sys.stderr)
        return 1
