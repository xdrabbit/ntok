"""The commit engine — the safety-critical core of streaming dictation.

OS-level injection (ydotool/uinput) cannot un-type. So the one invariant that
matters is **commit-only**: text we emit is final and only ever grows; we never
retract. That makes *when* we commit the whole game. This engine is a pure
function over a stream of transcriptions — no audio, no model, no I/O — so the
dangerous logic can be tested exhaustively in milliseconds.

Three mechanisms keep commits trustworthy:

1. **Don't commit the in-progress tail.** The last segment of a transcription is
   still growing as the speaker talks, so we never commit it *unless* it's
   followed by trailing silence (sentence finished) or audio has ended.

2. **Confirmation-delayed commit (agreement-of-1).** A segment is only committed
   once two consecutive transcriptions agree on its normalized text at the same
   position. This costs one tick of latency and buys stability: we never commit
   a word the next tick would have revised — which, under commit-only, we could
   never take back.

3. **Seam de-duplication.** When the buffer is cut at a committed segment's edge
   and re-transcribed, Whisper can re-emit the last word(s). ``dedup_overlap``
   strips that repeat before it reaches the screen.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .textutil import dedup_overlap, normalize


@dataclass
class Segment:
    """A transcription segment, timed relative to the current buffer start."""

    start: float
    end: float
    text: str


@dataclass
class StepResult:
    """What a single tick decided: text to emit and audio to drop from the buffer."""

    deltas: list[str] = field(default_factory=list)
    advance_seconds: float = 0.0

    @property
    def committed(self) -> bool:
        return bool(self.deltas)


class CommitEngine:
    def __init__(
        self,
        *,
        min_silence_s: float = 0.5,
        require_confirmation: bool = True,
        capitalize_first: bool = False,
        max_overlap_tokens: int = 8,
    ):
        self.min_silence_s = min_silence_s
        self.require_confirmation = require_confirmation
        self.capitalize_first = capitalize_first
        self.max_overlap_tokens = max_overlap_tokens

        self.committed_text = ""        # concatenation of every delta emitted
        self._prev_norm: list[str] = []  # normalized segment texts seen last tick
        self._emitted = False

    # -- public API ---------------------------------------------------------
    def step(
        self,
        segments: list[Segment],
        buffer_duration: float,
        ended: bool = False,
    ) -> StepResult:
        """Decide what to commit from this transcription of the current buffer.

        ``segments`` are timed relative to the buffer start. ``buffer_duration``
        is the seconds of audio currently buffered (used to detect trailing
        silence after the last segment). ``ended`` flushes everything, since no
        further audio is coming.
        """
        if not segments:
            # Nothing transcribed. Keep prior confirmation state on a normal
            # tick; on end-of-audio there is simply nothing left to flush.
            return StepResult()

        cur_norm = [normalize(s.text) for s in segments]

        commit_count = self._eligible_count(segments, buffer_duration, ended)
        if self.require_confirmation and not ended:
            commit_count = self._confirmed_prefix(cur_norm, commit_count)

        if commit_count == 0:
            # Remember what we saw so the next tick can confirm it.
            self._prev_norm = cur_norm
            return StepResult()

        deltas: list[str] = []
        for seg in segments[:commit_count]:
            delta = self._present(seg.text)
            if delta:
                deltas.append(delta)

        advance = min(segments[commit_count - 1].end, buffer_duration)
        # The uncommitted remainder becomes next tick's confirmation baseline,
        # already in the post-advance coordinate frame (positions align with the
        # next transcription's leading segments).
        self._prev_norm = cur_norm[commit_count:]
        return StepResult(deltas=deltas, advance_seconds=advance)

    def prompt(self, max_chars: int = 200) -> str:
        """Tail of committed text, to feed as Whisper ``initial_prompt`` so the
        next transcription has lexical left-context despite the audio cut."""
        return self.committed_text[-max_chars:].strip()

    # -- internals ----------------------------------------------------------
    def _eligible_count(
        self, segments: list[Segment], buffer_duration: float, ended: bool
    ) -> int:
        """How many leading segments are stable enough to consider committing."""
        n = len(segments)
        if ended:
            return n
        # All but the last are complete (more speech follows them). The last is
        # also stable if there's trailing silence after it — a sentence/pause.
        last_gap = buffer_duration - segments[-1].end
        if last_gap >= self.min_silence_s:
            return n
        return n - 1

    def _confirmed_prefix(self, cur_norm: list[str], limit: int) -> int:
        """Longest leading run (up to ``limit``) that matches last tick at the
        same position and is non-empty — the agreement-of-1 gate."""
        k = 0
        while k < limit:
            if k >= len(self._prev_norm):
                break
            if not cur_norm[k] or cur_norm[k] != self._prev_norm[k]:
                break
            k += 1
        return k

    def _present(self, text: str) -> str:
        """Turn a committed segment into a ready-to-type delta and fold it into
        ``committed_text``. Dedups the seam, joins phrases with one space, and
        optionally capitalizes the very first character of the session."""
        phrase = dedup_overlap(self.committed_text, text.strip(), self.max_overlap_tokens)
        phrase = phrase.strip()
        if not phrase:
            return ""
        if not self._emitted:
            if self.capitalize_first:
                phrase = phrase[0].upper() + phrase[1:]
            delta = phrase
        else:
            delta = " " + phrase
        self.committed_text += delta
        self._emitted = True
        return delta
