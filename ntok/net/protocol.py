"""Wire protocol for ntok's client/server split.

A tiny length-prefixed framing over a TCP stream. Each frame is::

    [ uint32 length ][ uint8 type ][ payload ... ]

where ``length`` counts the type byte plus the payload. Control messages carry
a JSON payload; AUDIO carries raw little-endian int16 mono PCM; COMMIT carries
UTF-8 text. Everything here is pure (no sockets) so it can be unit-tested with
in-memory byte streams — the socket I/O lives in server.py / client.py.
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass

# Message types.
HELLO = 1      # client->server: {"token","sample_rate", ...} auth + params
HELLO_OK = 2   # server->client: {} accepted
HELLO_ERR = 3  # server->client: {"reason"}
AUDIO = 4      # client->server: raw int16 PCM bytes
COMMIT = 5     # server->client: UTF-8 committed text delta
END = 6        # client->server: end of utterance, flush the tail
CANCEL = 7     # client->server: discard the in-flight utterance
PING = 8
PONG = 9

_HEADER = struct.Struct(">IB")  # length (uint32), type (uint8)
MAX_FRAME = 16 * 1024 * 1024     # 16 MiB guard against a runaway length


@dataclass
class Frame:
    type: int
    payload: bytes

    # -- typed payload helpers ----------------------------------------------
    def json(self) -> dict:
        return json.loads(self.payload.decode("utf-8")) if self.payload else {}

    def text(self) -> str:
        return self.payload.decode("utf-8")


def encode(msg_type: int, payload: bytes = b"") -> bytes:
    return _HEADER.pack(len(payload) + 1, msg_type) + payload


def encode_json(msg_type: int, obj: dict) -> bytes:
    return encode(msg_type, json.dumps(obj).encode("utf-8"))


def encode_text(msg_type: int, text: str) -> bytes:
    return encode(msg_type, text.encode("utf-8"))


class FrameParser:
    """Incremental parser: feed raw bytes, get back complete frames.

    Tolerates messages split across reads or coalesced into one read — the
    realities of a TCP stream.
    """

    def __init__(self, max_frame: int = MAX_FRAME):
        self._buf = bytearray()
        self._max = max_frame

    def feed(self, data: bytes) -> list[Frame]:
        self._buf.extend(data)
        frames: list[Frame] = []
        while len(self._buf) >= _HEADER.size:
            length, mtype = _HEADER.unpack_from(self._buf, 0)
            if length < 1:
                raise ValueError("frame length must include the type byte")
            if length - 1 > self._max:
                raise ValueError(f"frame too large: {length - 1} bytes")
            total = _HEADER.size + length - 1  # header + type-inclusive length
            if len(self._buf) < total:
                break  # wait for the rest
            payload = bytes(self._buf[_HEADER.size:total])
            del self._buf[:total]
            frames.append(Frame(mtype, payload))
        return frames
