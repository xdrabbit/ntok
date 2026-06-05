"""Tier 1: wire protocol framing. Pure, no sockets."""

import pytest

from ntok.net import protocol as p


def test_roundtrip_text():
    data = p.encode_text(p.COMMIT, "hello world")
    frames = p.FrameParser().feed(data)
    assert len(frames) == 1
    assert frames[0].type == p.COMMIT
    assert frames[0].text() == "hello world"


def test_roundtrip_json():
    data = p.encode_json(p.HELLO, {"token": "abc", "sample_rate": 16000})
    frame = p.FrameParser().feed(data)[0]
    assert frame.type == p.HELLO
    assert frame.json() == {"token": "abc", "sample_rate": 16000}


def test_roundtrip_audio_bytes():
    pcm = bytes(range(256)) * 4
    frame = p.FrameParser().feed(p.encode(p.AUDIO, pcm))[0]
    assert frame.type == p.AUDIO
    assert frame.payload == pcm


def test_empty_payload():
    frame = p.FrameParser().feed(p.encode(p.END))[0]
    assert frame.type == p.END
    assert frame.payload == b""
    assert frame.json() == {}


def test_frame_split_across_reads():
    data = p.encode_text(p.COMMIT, "split me")
    parser = p.FrameParser()
    # Feed one byte at a time; only the last byte completes the frame.
    out = []
    for i in range(len(data)):
        out += parser.feed(data[i:i + 1])
    assert len(out) == 1 and out[0].text() == "split me"


def test_multiple_frames_coalesced():
    blob = (p.encode_text(p.COMMIT, "one")
            + p.encode_text(p.COMMIT, "two")
            + p.encode(p.END))
    frames = p.FrameParser().feed(blob)
    assert [f.type for f in frames] == [p.COMMIT, p.COMMIT, p.END]
    assert frames[0].text() == "one" and frames[1].text() == "two"


def test_partial_then_rest():
    blob = p.encode_json(p.HELLO_OK, {"ok": True})
    parser = p.FrameParser()
    assert parser.feed(blob[:3]) == []
    frames = parser.feed(blob[3:])
    assert len(frames) == 1 and frames[0].json() == {"ok": True}


def test_oversize_frame_rejected():
    parser = p.FrameParser(max_frame=8)
    with pytest.raises(ValueError):
        parser.feed(p.encode(p.AUDIO, b"x" * 9))
