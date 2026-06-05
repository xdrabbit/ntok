"""Builds the acceptance-test audio fixture (downloaded, not vendored).

Primary source is three short LibriSpeech dev-clean utterances (public domain,
read speech with clear sentence boundaries) concatenated with silence gaps —
representative of dictation, so first-commit latency is meaningful. We read the
raw flac bytes and decode with soundfile to avoid the heavy torchcodec decoder
that `datasets` now pulls in.

If LibriSpeech can't be fetched, we fall back to a doubled JFK clip (a single
reliably-downloadable public-domain WAV). Either way the built clip and its
reference transcript are cached as ``clip.wav`` + ``clip.txt`` so the fixture is
deterministic once created.

Uses only stdlib ``wave`` + numpy to *write* the fixture; building needs no
extra runtime dependencies.
"""

from __future__ import annotations

import io
import urllib.request
import wave
from pathlib import Path

import numpy as np

FIX_DIR = Path(__file__).parent / "fixtures"
JFK_WAV = FIX_DIR / "jfk.wav"
CLIP_WAV = FIX_DIR / "clip.wav"
CLIP_TXT = FIX_DIR / "clip.txt"
JFK_URL = "https://github.com/ggerganov/whisper.cpp/raw/master/samples/jfk.wav"

SR = 16000
GAP_S = 0.6
# Short utterances first so a sentence completes early — a fair latency test.
LIBRISPEECH_INDICES = [1, 0, 5]
JFK_TEXT = (
    "And so my fellow Americans ask not what your country can do for you "
    "ask what you can do for your country"
)


def _read_wav_int16(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        assert w.getframerate() == SR, f"expected {SR} Hz, got {w.getframerate()}"
        assert w.getnchannels() == 1, "expected mono"
        frames = w.readframes(w.getnframes())
    return np.frombuffer(frames, dtype=np.int16)


def _write_wav_int16(path: Path, pcm: np.ndarray) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.astype(np.int16).tobytes())


def _f32_to_i16(arr: np.ndarray) -> np.ndarray:
    if arr.ndim > 1:
        arr = arr.mean(axis=1)
    return np.clip(arr * 32767.0, -32768, 32767).astype(np.int16)


def _build_from_librispeech() -> tuple[np.ndarray, str]:
    import soundfile as sf
    from datasets import Audio, load_dataset

    ds = load_dataset(
        "hf-internal-testing/librispeech_asr_dummy", "clean", split="validation"
    ).cast_column("audio", Audio(decode=False))

    gap = np.zeros(int(GAP_S * SR), dtype=np.int16)
    pieces: list[np.ndarray] = []
    texts: list[str] = []
    for i in LIBRISPEECH_INDICES:
        arr, sr = sf.read(io.BytesIO(ds[i]["audio"]["bytes"]))
        assert sr == SR, f"sample {i} is {sr} Hz"
        if pieces:
            pieces.append(gap)
        pieces.append(_f32_to_i16(arr))
        texts.append(" ".join(ds[i]["text"].split()))
    return np.concatenate(pieces), " ".join(texts)


def _build_from_jfk() -> tuple[np.ndarray, str]:
    if not JFK_WAV.exists():
        urllib.request.urlretrieve(JFK_URL, JFK_WAV)
    jfk = _read_wav_int16(JFK_WAV)
    gap = np.zeros(int(GAP_S * SR), dtype=np.int16)
    return np.concatenate([jfk, gap, jfk]), JFK_TEXT + " " + JFK_TEXT


def ensure_fixture() -> tuple[Path, str]:
    """Return (clip.wav path, reference transcript), building/caching as needed."""
    FIX_DIR.mkdir(parents=True, exist_ok=True)
    if CLIP_WAV.exists() and CLIP_TXT.exists():
        return CLIP_WAV, CLIP_TXT.read_text().strip()

    try:
        clip, reference = _build_from_librispeech()
    except Exception as e:  # noqa: BLE001 — fall back to the always-available clip
        print(f"[fixture] LibriSpeech unavailable ({e}); using JFK fallback")
        clip, reference = _build_from_jfk()

    _write_wav_int16(CLIP_WAV, clip)
    CLIP_TXT.write_text(reference + "\n")
    return CLIP_WAV, reference


def load_pcm_f32(path: Path) -> np.ndarray:
    """Load a 16 kHz mono WAV as float32 in [-1, 1]."""
    return _read_wav_int16(path).astype(np.float32) / 32768.0
