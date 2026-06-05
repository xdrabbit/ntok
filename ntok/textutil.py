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
