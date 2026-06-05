"""Whisper transcription via faster-whisper (CTranslate2).

The model is loaded once and kept warm in VRAM by the daemon, so each dictation
only pays for inference, not model load.
"""

from __future__ import annotations

import numpy as np


class Transcriber:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._model = None

    def load(self) -> None:
        """Load the model into memory/VRAM. Called once at daemon startup."""
        from faster_whisper import WhisperModel

        m = self.cfg["model"]
        self._model = WhisperModel(
            m["name"],
            device=m["device"],
            compute_type=m["compute_type"],
        )

    @property
    def ready(self) -> bool:
        return self._model is not None

    def transcribe(self, audio: np.ndarray) -> str:
        if self._model is None:
            raise RuntimeError("Model not loaded")
        if audio.size == 0:
            return ""

        t = self.cfg["transcribe"]
        lang = self.cfg["model"]["language"] or None
        segments, _info = self._model.transcribe(
            audio,
            language=lang,
            beam_size=t["beam_size"],
            vad_filter=t["vad_filter"],
            initial_prompt=t["initial_prompt"] or None,
            condition_on_previous_text=False,
        )
        text = "".join(seg.text for seg in segments).strip()
        return text

    def transcribe_segments(
        self,
        audio: np.ndarray,
        initial_prompt: str = "",
        vad_filter: bool | None = None,
    ) -> list[tuple[float, float, str]]:
        """Transcribe and return per-segment (start, end, text) with timestamps.

        Used by the streaming engine, which needs segment boundaries to decide
        what is stable enough to commit. ``initial_prompt`` carries the tail of
        already-committed text so the model keeps lexical context across the
        hard audio cut at each commit seam. ``condition_on_previous_text`` stays
        False to avoid runaway hallucination; the prompt gives the context.

        Times are in seconds, relative to the start of ``audio``.
        """
        if self._model is None:
            raise RuntimeError("Model not loaded")
        if audio.size == 0:
            return []

        t = self.cfg["transcribe"]
        lang = self.cfg["model"]["language"] or None
        vf = t["vad_filter"] if vad_filter is None else vad_filter
        segments, _info = self._model.transcribe(
            audio,
            language=lang,
            beam_size=t["beam_size"],
            vad_filter=vf,
            initial_prompt=(initial_prompt or t["initial_prompt"] or None),
            condition_on_previous_text=False,
        )
        return [(seg.start, seg.end, seg.text) for seg in segments]
