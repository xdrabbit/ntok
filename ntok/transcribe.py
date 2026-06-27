"""Whisper transcription.

Supports backends (selected by [model].backend):
- "faster-whisper": local CTranslate2 (warm GPU, private, fast on good hardware)
- "openai": OpenAI Whisper API
- "grok": xAI Grok STT (lowest latency real-time via REST/WebSocket capable; recommended for dictation)

Grok STT provides word-level timestamps and is built for low-latency streaming use cases.
"""

from __future__ import annotations

import io
import os
from typing import Any

import numpy as np


class Transcriber:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._backend = (cfg.get("model", {}).get("backend") or "faster-whisper").lower()
        self._local = None  # faster-whisper WhisperModel
        self._openai_client: Any = None

    def load(self) -> None:
        """Load for the chosen backend. Local warms model in VRAM. API prepares client."""
        m = self.cfg["model"]
        if self._backend == "openai":
            self._load_openai()
            return
        if self._backend == "grok":
            self._load_grok()
            return
        from faster_whisper import WhisperModel
        self._local = WhisperModel(
            m["name"],
            device=m["device"],
            compute_type=m["compute_type"],
        )

    def _load_openai(self) -> None:
        try:
            from openai import OpenAI
        except ImportError as e:
            raise RuntimeError(
                "backend=openai but openai package missing. Install with: pip install openai"
            ) from e
        key = os.environ.get("OPENAI_API_KEY") or self.cfg.get("net", {}).get("openai_key", "")
        if not key:
            pass  # will fail on first use with a clear error from the OpenAI client
        self._openai_client = OpenAI(api_key=key or None)

    def _load_grok(self) -> None:
        try:
            import requests  # used for /v1/stt multipart upload
        except ImportError as e:
            raise RuntimeError(
                "backend=grok but 'requests' package missing. Install with: pip install requests"
            ) from e
        key = os.environ.get("XAI_API_KEY") or self.cfg.get("net", {}).get("xai_key", "") or self.cfg.get("net", {}).get("grok_key", "")
        if not key:
            # Will fail on first use
            pass
        self._grok_key = key
        self._requests = requests  # store module

    @property
    def ready(self) -> bool:
        if self._backend in ("openai", "grok"):
            return True  # lazy; actual calls will validate key
        return self._local is not None

    # helpers
    def _language(self) -> str | None:
        return self.cfg["model"].get("language") or None

    def _prompt(self, extra: str = "") -> str | None:
        t = self.cfg.get("transcribe", {})
        # Merge the configured vocab-bias prompt with any per-call context. Both
        # matter and must coexist: the config biases out-of-vocabulary words like
        # "ntok"; ``extra`` carries the streaming engine's committed left-context
        # (and anti-hallucination priming). The old ``extra or config`` form
        # silently dropped the config prompt whenever streaming supplied context.
        base = t.get("initial_prompt", "") or ""
        p = " ".join(s for s in (base, extra) if s).strip()
        if self._backend in ("openai", "grok"):
            # Prime against repetitive "thank you" / closing hallucinations common in silence tails.
            anti = " Accurate verbatim transcription of spoken words only. Never append 'thank you', 'thanks', 'the end', 'okay', 'you', or any closing statements."
            p = (p + " " + anti).strip()
        return p.strip() or None

    # local
    def _transcribe_local(self, audio: np.ndarray, initial_prompt: str = "", want_segments: bool = False, vf: bool | None = None):
        if self._local is None:
            raise RuntimeError("Local model not loaded")
        if audio.size == 0:
            return [] if want_segments else ""
        t = self.cfg.get("transcribe", {})
        lang = self._language()
        vf = t.get("vad_filter", True) if vf is None else vf
        extra: dict[str, Any] = {}
        if vf:
            # Pad detected speech so Silero VAD doesn't shave word edges (the
            # clipped-first/last-word problem). speech_pad_ms widens each speech
            # region; vad_min_silence_ms is how long a gap must be before VAD
            # treats it as a real pause. Both configurable so they can be nudged
            # without a code change.
            extra["vad_parameters"] = {
                "speech_pad_ms": int(t.get("speech_pad_ms", 400)),
                "min_silence_duration_ms": int(t.get("vad_min_silence_ms", 2000)),
            }
        # When set, raises Whisper's confidence bar for emitting a segment over a
        # near-silent tail — drops phantom trailing words/hallucinations. Left at
        # the library default (0.6) unless overridden in config.
        nst = t.get("no_speech_threshold", None)
        if nst is not None:
            extra["no_speech_threshold"] = float(nst)
        segments, _info = self._local.transcribe(
            audio,
            language=lang,
            beam_size=t.get("beam_size", 5),
            vad_filter=vf,
            initial_prompt=self._prompt(initial_prompt),
            condition_on_previous_text=False,
            **extra,
        )
        if want_segments:
            return [(seg.start, seg.end, seg.text) for seg in segments]
        return "".join(seg.text for seg in segments).strip()

    # openai
    def _pcm_to_wav(self, audio: np.ndarray, sr: int) -> bytes:
        import wave
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sr)
            pcm16 = (np.clip(audio, -1.0, 1.0) * 32767.0).astype(np.int16)
            w.writeframes(pcm16.tobytes())
        return buf.getvalue()

    def _transcribe_openai(self, audio: np.ndarray, initial_prompt: str = "", want_segments: bool = False):
        if self._openai_client is None:
            self._load_openai()
        if audio.size == 0:
            return [] if want_segments else ""
        sr = int(self.cfg.get("audio", {}).get("sample_rate", 16000))
        wav_bytes = self._pcm_to_wav(audio, sr)
        tcfg = self.cfg.get("transcribe", {})
        model = tcfg.get("openai_model", "whisper-1")
        lang = self._language()
        kwargs: dict[str, Any] = {
            "model": model,
            "file": ("speech.wav", wav_bytes, "audio/wav"),
            "prompt": self._prompt(initial_prompt),
            "response_format": "verbose_json" if want_segments else "json",
        }
        if lang:
            kwargs["language"] = lang
        resp = self._openai_client.audio.transcriptions.create(**kwargs)
        if want_segments:
            segs = getattr(resp, "segments", None) or (resp.get("segments") if isinstance(resp, dict) else [])
            return [(float(s.get("start", 0) if isinstance(s, dict) else getattr(s, "start", 0)),
                     float(s.get("end", 0) if isinstance(s, dict) else getattr(s, "end", 0)),
                     (s.get("text", "") if isinstance(s, dict) else getattr(s, "text", "")))
                    for s in segs]
        txt = getattr(resp, "text", None)
        if txt is None and isinstance(resp, dict):
            txt = resp.get("text", "")
        return (txt or "").strip()

    # grok (xAI)
    def _transcribe_grok(self, audio: np.ndarray, initial_prompt: str = "", want_segments: bool = False):
        if not hasattr(self, "_requests") or self._requests is None:
            self._load_grok()
        if audio.size == 0:
            return [] if want_segments else ""
        sr = int(self.cfg.get("audio", {}).get("sample_rate", 16000))
        wav_bytes = self._pcm_to_wav(audio, sr)  # reuse wav maker
        tcfg = self.cfg.get("transcribe", {})
        model = tcfg.get("grok_model", "grok-stt")
        lang = self._language()
        prompt = self._prompt(initial_prompt)
        files = {"file": ("speech.wav", wav_bytes, "audio/wav")}
        data = {"model": model}
        if lang:
            data["language"] = lang
        # Grok STT supports prompt-like via ? but for simplicity pass as param if supported; docs show mainly file.
        # Many STT accept "prompt" for biasing.
        if prompt:
            data["prompt"] = prompt
        resp = self._requests.post(
            "https://api.x.ai/v1/stt",
            headers={"Authorization": f"Bearer {self._grok_key}"},
            files=files,
            data=data,
            timeout=30,
        )
        resp.raise_for_status()
        j = resp.json()
        text = j.get("text", "") or ""
        if not want_segments:
            return text.strip()
        # Use word-level timestamps for high quality segment data (better for commit engine)
        words = j.get("words", []) or []
        if words:
            # Return as fine segments; commit engine will group logically via confirmation
            segs = []
            for w in words:
                segs.append( (float(w.get("start", 0)), float(w.get("end", 0)), w.get("text", "")) )
            return segs
        # Fallback: single segment for whole text (times approximate)
        dur = j.get("duration", len(audio) / sr)
        return [(0.0, dur, text.strip())]

    def transcribe(self, audio: np.ndarray) -> str:
        if self._backend == "openai":
            return self._transcribe_openai(audio)
        if self._backend == "grok":
            return self._transcribe_grok(audio)
        return self._transcribe_local(audio)

    def transcribe_segments(
        self,
        audio: np.ndarray,
        initial_prompt: str = "",
        vad_filter: bool | None = None,
    ) -> list[tuple[float, float, str]]:
        """Transcribe and return per-segment (start, end, text) with timestamps.

        For openai/grok backends, vad_filter is ignored (server-side handling + prompt).
        Used by the streaming engine, which needs segment boundaries.
        Grok returns high-quality word timestamps when available.
        """
        if self._backend == "openai":
            return self._transcribe_openai(audio, initial_prompt, want_segments=True)
        if self._backend == "grok":
            return self._transcribe_grok(audio, initial_prompt, want_segments=True)
        return self._transcribe_local(audio, initial_prompt, want_segments=True, vf=vad_filter)
