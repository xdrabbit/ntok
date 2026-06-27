"""Text utilities shared by the streaming engine and the test suite.

All three helpers work on a common notion of a *normalized token*: lowercased,
stripped of surrounding punctuation, whitespace-collapsed. The streaming engine
uses normalization for two safety-critical jobs — deciding when two consecutive
transcriptions *agree* on a phrase (confirmation-delayed commit) and removing
duplicated words at a commit seam (``dedup_overlap``). The acceptance test uses
``wer`` to score accuracy against a reference transcript.
"""

from __future__ import annotations

# Punctuation we strip from the edges of a token before comparing. We keep
# intra-word characters (e.g. "don't", "co-op") intact.
_EDGE_PUNCT = ".,!?;:\"'`()[]{}…—–-"


def norm_token(word: str) -> str:
    """Normalize a single word for comparison: lowercase, strip edge punctuation."""
    return word.strip(_EDGE_PUNCT).lower()


def normalize(text: str) -> str:
    """Normalize a whole string to space-joined normalized tokens."""
    return " ".join(t for t in (norm_token(w) for w in text.split()) if t)


def tokens(text: str) -> list[str]:
    """Normalized, non-empty tokens of ``text``, in order."""
    return [t for t in (norm_token(w) for w in text.split()) if t]


def dedup_overlap(committed: str, addition: str, max_overlap: int = 8) -> str:
    """Drop a leading run of ``addition`` that repeats the tail of ``committed``.

    This is the belt-and-suspenders guard against boundary duplication: when the
    buffer is cut at a segment edge and re-transcribed, Whisper can re-emit the
    last word(s) we already committed. We compare on *normalized* tokens but
    return the surviving suffix of ``addition`` with its original casing and
    punctuation intact.

    Only the largest overlap up to ``max_overlap`` tokens is considered, so a
    legitimately repeated short word mid-sentence ("that that") isn't eaten when
    it isn't actually a seam artifact.
    """
    add_words = addition.split()
    if not add_words:
        return ""
    ctail = tokens(committed)
    add_norm = [norm_token(w) for w in add_words]
    # Note: add_norm may contain "" for pure-punctuation tokens; those never
    # match a real committed token, so they correctly halt the overlap.
    limit = min(len(ctail), len(add_norm), max_overlap)
    best = 0
    for k in range(limit, 0, -1):
        if ctail[-k:] == add_norm[:k] and all(add_norm[:k]):
            best = k
            break
    return " ".join(add_words[best:])


# --------------------------------------------------------------------------
# Flow + spoken-punctuation dictation mode
# --------------------------------------------------------------------------
#
# In flow mode the engine does NOT auto-capitalize/auto-terminate every short
# commit. Words flow continuously; the *speaker* dictates punctuation. The only
# automatic structure is capitalization at the very start, after a
# sentence-ending mark (. ? !) and after a newline. Whisper's own fragment-edge
# punctuation and fragment-initial capitalization are stripped so that ONLY the
# spoken commands + these auto-cap rules drive layout. Intra-word punctuation
# (don't, co-op) and digits are preserved.

# Spoken commands keyed on *normalized* tokens (see ``norm_token``).
_FLOW_TWO_WORD = {
    ("full", "stop"): ".",
    ("question", "mark"): "?",
    ("exclamation", "point"): "!",
    ("exclamation", "mark"): "!",
    ("new", "line"): "\n",
    ("new", "paragraph"): "\n\n",
    ("open", "paren"): "(",
    ("close", "paren"): ")",
}
_FLOW_ONE_WORD = {
    "period": ".",
    "comma": ",",
    "colon": ":",
    "semicolon": ";",
    "dash": "-",
    "hyphen": "-",
}
# "cap"/"capital" capitalizes the NEXT word (proper nouns the engine can't know).
_FLOW_CAP = {"cap", "capital"}
# First tokens that *might* begin a two-word command (or the cap modifier); when
# one of these arrives as the last buffered token we wait for its partner rather
# than committing it as a plain word — this is what makes a command split across
# a delta boundary ("full" | "stop") still resolve correctly.
_FLOW_PREFIXES = {a for (a, _b) in _FLOW_TWO_WORD} | _FLOW_CAP

# Punctuation that hugs the preceding token (no leading space).
_FLOW_NO_SPACE_BEFORE = {".", ",", ";", ":", "?", "!", ")"}
_FLOW_SENTENCE_END = {".", "?", "!"}
# Whisper-added edge punctuation we peel off a word (kept intra-word: ' and -).
_FLOW_STRIP = ".,!?;:\"'`…—–()[]{}"
# English pronoun "I" stays capitalized even mid-sentence.
_FLOW_I_WORDS = {"i", "i'm", "i'll", "i've", "i'd"}


class FlowFormatter:
    """Stateful flow + spoken-punctuation transform for the streaming commit path.

    ``feed(delta)`` is called once per committed delta and returns the text to
    append to the output (possibly ""). State persists across calls so the
    transform composes correctly at delta boundaries: pending capitalization
    (did the prior delta end a sentence / newline?), owed inter-word spacing,
    and a small buffer holding a token that may be the first half of a two-word
    command. ``flush()`` drains that buffer at end-of-audio.

    The output is append-only — feed() never rewrites previously returned text —
    so the engine's commit-only / monotonic guarantee is preserved.
    """

    def __init__(self) -> None:
        self._cap_next = True        # capitalize the next word (start of output)
        self._need_space = False     # a space is owed before the next word/group
        self._sentence_open = False  # a word has been emitted since last terminator
        self._pending: list[str] = []  # buffered tokens awaiting a command decision
        self.text = ""               # full formatted output emitted so far

    # -- public API ---------------------------------------------------------
    def feed(self, delta: str) -> str:
        self._pending.extend(delta.split())
        return self._drain(final=False)

    def flush(self) -> str:
        return self._drain(final=True)

    # -- internals ----------------------------------------------------------
    def _drain(self, final: bool) -> str:
        out: list[str] = []
        while self._pending:
            t0 = norm_token(self._pending[0])
            if t0 in _FLOW_PREFIXES:
                if len(self._pending) < 2:
                    if not final:
                        break  # wait — its partner may arrive next delta
                    # End of audio with a dangling prefix: treat it as a word.
                    out.append(self._word(self._pending.pop(0)))
                    continue
                if t0 in _FLOW_CAP:
                    # Capitalize whatever real word comes next.
                    self._cap_next = True
                    self._pending.pop(0)
                    continue
                key = (t0, norm_token(self._pending[1]))
                if key in _FLOW_TWO_WORD:
                    out.append(self._punct(_FLOW_TWO_WORD[key]))
                    del self._pending[:2]
                    continue
                # A prefix word not followed by its partner ("new york") is plain.
                out.append(self._word(self._pending.pop(0)))
                continue
            if t0 in _FLOW_ONE_WORD:
                out.append(self._punct(_FLOW_ONE_WORD[t0]))
                self._pending.pop(0)
                continue
            out.append(self._word(self._pending.pop(0)))
        emitted = "".join(out)
        self.text += emitted
        return emitted

    def _word(self, original: str) -> str:
        w = original.strip(_FLOW_STRIP)
        if not w:
            return ""
        # Neutralize Whisper's fragment-initial capitalization; auto-cap / the
        # `cap` command re-introduce capitals deliberately.
        w = w[:1].lower() + w[1:]
        cap = self._cap_next or w.lower() in _FLOW_I_WORDS
        prefix = " " if self._need_space else ""
        if cap:
            w = w[:1].upper() + w[1:]
        self._cap_next = False
        self._need_space = True
        self._sentence_open = True
        return prefix + w

    def _punct(self, mark: str) -> str:
        if mark in ("\n", "\n\n"):
            # A paragraph break ends the current sentence (terminate with a
            # period if one is open); a single line break is a soft wrap and
            # leaves the sentence open (lists). Newlines absorb spaces and the
            # next word capitalizes.
            lead = ""
            if mark == "\n\n" and self._sentence_open:
                lead = "."
                self._sentence_open = False
            self._need_space = False
            self._cap_next = True
            return lead + mark
        if mark == "(":
            prefix = " " if self._need_space else ""
            self._need_space = False  # no space after "("
            return prefix + "("
        if mark in _FLOW_NO_SPACE_BEFORE:
            self._need_space = True
            if mark in _FLOW_SENTENCE_END:
                self._cap_next = True
                self._sentence_open = False
            return mark
        # Dash / hyphen as a spoken token: render spaced (em-dash style).
        prefix = " " if self._need_space else ""
        self._need_space = True
        return prefix + mark


def format_spoken(text: str) -> str:
    """One-shot flow + spoken-punctuation transform over a whole string.

    Equivalent to feeding ``text`` to a fresh :class:`FlowFormatter` and
    flushing — convenient for tests and non-streaming callers.
    """
    f = FlowFormatter()
    return f.feed(text) + f.flush()


def wer(reference: str, hypothesis: str) -> float:
    """Word error rate of ``hypothesis`` vs ``reference`` over normalized tokens.

    Standard Levenshtein distance at the word level divided by reference length.
    Returns 0.0 when the reference is empty and the hypothesis is too.
    """
    r = tokens(reference)
    h = tokens(hypothesis)
    if not r:
        return 0.0 if not h else 1.0
    # DP edit distance over words.
    prev = list(range(len(h) + 1))
    for i, rw in enumerate(r, start=1):
        cur = [i] + [0] * len(h)
        for j, hw in enumerate(h, start=1):
            cost = 0 if rw == hw else 1
            cur[j] = min(
                prev[j] + 1,      # deletion
                cur[j - 1] + 1,   # insertion
                prev[j - 1] + cost,  # substitution / match
            )
        prev = cur
    return prev[len(h)] / len(r)
