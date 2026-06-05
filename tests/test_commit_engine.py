"""Tier 1: the commit engine. Pure logic — no GPU, no model, runs in ms.

This is where the commit-only invariant and the anti-duplication / stability
guards are proven. The ``replay`` simulator drives the engine with a stream of
absolute-timed segments the way a real session would: the buffer advances on
commit, the in-progress last segment grows word-by-word, and we assert the
invariants on the emitted deltas.
"""

from __future__ import annotations

from ntok.commit import CommitEngine, Segment
from ntok.textutil import normalize, tokens


# --- a deterministic streaming simulator -----------------------------------

def replay(abs_segments, total_duration, *, tick_s=0.5, min_silence_s=0.5,
           require_confirmation=True, capitalize_first=False):
    """Replay absolute-timed (start, end, text) segments through the engine.

    Models how a real session feeds the engine: at each tick the visible buffer
    is [buffer_start, audio_now]; segments before buffer_start are gone
    (already committed + dropped); the last in-flight segment is rendered
    partially, with words emerging linearly across its [start, end] span.
    Returns (engine, events) where events is a list of (tick_time, delta).
    """
    eng = CommitEngine(
        min_silence_s=min_silence_s,
        require_confirmation=require_confirmation,
        capitalize_first=capitalize_first,
    )
    events = []
    buffer_start = 0.0
    eps = 1e-9

    def visible(audio_now):
        vis = []
        for start, end, text in abs_segments:
            if start < buffer_start - eps:
                continue  # already committed and dropped
            if start >= audio_now - eps:
                continue  # hasn't begun yet
            rel_start = start - buffer_start
            if end <= audio_now + eps:
                rel_end, shown = end - buffer_start, text
            else:
                # in progress: reveal words proportional to elapsed fraction
                words = text.split()
                frac = (audio_now - start) / max(end - start, eps)
                k = max(1, min(len(words), int(round(frac * len(words)))))
                rel_end, shown = audio_now - buffer_start, " ".join(words[:k])
            vis.append(Segment(rel_start, rel_end, shown))
        return vis

    t = tick_s
    while t < total_duration - eps:
        audio_now = min(t, total_duration)
        res = eng.step(visible(audio_now), audio_now - buffer_start, ended=False)
        for d in res.deltas:
            events.append((t, d))
        buffer_start += res.advance_seconds
        t += tick_s

    # Final flush at end-of-audio.
    res = eng.step(visible(total_duration), total_duration - buffer_start, ended=True)
    for d in res.deltas:
        events.append((total_duration, d))
    return eng, events


# --- the headline invariant tests ------------------------------------------

REFERENCE = "hello there my friend how are you doing today"
SEGS = [
    (0.0, 2.0, "Hello there my friend."),
    (2.5, 5.0, "How are you doing today?"),
]


def test_it_streams_at_least_two_commits_before_end():
    _eng, events = replay(SEGS, total_duration=6.0)
    before_end = [d for (t, d) in events if t < 6.0]
    assert len(before_end) >= 2, f"expected incremental commits, got {events}"


def test_commit_only_is_monotonic_and_matches_concatenation():
    eng, events = replay(SEGS, total_duration=6.0)
    deltas = [d for (_t, d) in events]
    # Concatenation of deltas IS the committed text — nothing rewritten.
    assert "".join(deltas) == eng.committed_text
    # Running prefix only ever grows (no retraction possible by construction,
    # but assert it explicitly as the contract).
    running = ""
    for d in deltas:
        assert eng.committed_text.startswith(running)
        running += d
    assert running == eng.committed_text


def test_accuracy_against_reference():
    eng, _events = replay(SEGS, total_duration=6.0)
    assert normalize(eng.committed_text) == REFERENCE


def test_no_word_duplicated_across_seam():
    eng, events = replay(SEGS, total_duration=6.0)
    toks = tokens(eng.committed_text)
    # Reference has no repeated adjacent words; a seam bug would create one.
    assert toks == REFERENCE.split()


# --- confirmation-delayed commit (agreement-of-1) --------------------------

def test_unstable_segment_is_not_committed_until_confirmed():
    eng = CommitEngine(min_silence_s=0.5)
    # Tick 1: first phrase complete, a second phrase in progress (mis-heard).
    r1 = eng.step([Segment(0.0, 2.0, "the cat sat"),
                   Segment(2.1, 3.0, "on the mat")], buffer_duration=3.0)
    assert r1.deltas == []  # nothing confirmed yet on the very first tick
    # Tick 2: first phrase still agrees -> it commits; second still in-progress.
    r2 = eng.step([Segment(0.0, 2.0, "the cat sat"),
                   Segment(2.1, 3.2, "on the mat")], buffer_duration=3.2)
    assert r2.deltas == ["the cat sat"]


def test_first_form_of_a_revised_word_never_reaches_output():
    eng = CommitEngine(min_silence_s=0.5)
    # The in-progress tail is mis-transcribed, then corrected before it stabilizes.
    eng.step([Segment(0.0, 1.5, "I scream"),
              Segment(1.6, 2.4, "for nice")], buffer_duration=2.5)
    eng.step([Segment(0.0, 1.5, "I scream"),
              Segment(1.6, 2.8, "for ice cream")], buffer_duration=2.9)
    # Flush.
    eng.step([Segment(0.0, 1.5, "I scream"),
              Segment(1.6, 3.0, "for ice cream")], buffer_duration=4.0, ended=True)
    assert normalize(eng.committed_text) == "i scream for ice cream"
    assert "nice" not in tokens(eng.committed_text)


# --- the silence rule ------------------------------------------------------

def test_lone_segment_commits_only_with_trailing_silence():
    eng = CommitEngine(min_silence_s=0.5)
    # No trailing silence yet (buffer ends right at segment end): hold it.
    eng.step([Segment(0.0, 2.0, "just one phrase")], buffer_duration=2.0)
    r = eng.step([Segment(0.0, 2.0, "just one phrase")], buffer_duration=2.1)
    assert r.deltas == []  # still < min_silence of trailing audio
    # Now 0.6s of trailing silence + confirmed -> commit.
    r = eng.step([Segment(0.0, 2.0, "just one phrase")], buffer_duration=2.6)
    assert r.deltas == ["just one phrase"]
    assert abs(r.advance_seconds - 2.0) < 1e-9


# --- end-of-audio flush ----------------------------------------------------

def test_ended_flushes_without_requiring_confirmation():
    eng = CommitEngine(min_silence_s=0.5)
    r = eng.step([Segment(0.0, 1.0, "final words here")],
                 buffer_duration=1.0, ended=True)
    assert r.deltas == ["final words here"]


# --- seam dedup through the engine -----------------------------------------

def test_engine_dedups_repeated_seam_word():
    eng = CommitEngine(min_silence_s=0.5, require_confirmation=False)
    eng.step([Segment(0.0, 1.0, "hello world"),
              Segment(1.0, 2.0, "x")], buffer_duration=2.0)  # commits "hello world"
    # Next buffer re-emits "world" at the seam; dedup must drop it.
    r = eng.step([Segment(0.0, 1.0, "world again now")],
                 buffer_duration=2.0, ended=True)
    assert r.deltas == [" again now"]
    assert normalize(eng.committed_text) == "hello world again now"


# --- presentation ----------------------------------------------------------

def test_spacing_between_phrases_is_single_space():
    eng, _ = replay(SEGS, total_duration=6.0)
    assert "  " not in eng.committed_text
    assert not eng.committed_text.startswith(" ")


def test_capitalize_first_only_capitalizes_session_start():
    eng = CommitEngine(min_silence_s=0.5, require_confirmation=False,
                       capitalize_first=True)
    eng.step([Segment(0.0, 1.0, "hello"), Segment(1.0, 2.0, "x")],
             buffer_duration=2.0)
    eng.step([Segment(0.0, 1.0, "there"), Segment(1.0, 2.0, "x")],
             buffer_duration=2.0)
    assert eng.committed_text.startswith("Hello")
    assert "Hello there".lower() in eng.committed_text.lower()
    # the second phrase is not capitalized
    assert " there" in eng.committed_text


def test_no_confirmation_mode_commits_immediately():
    eng = CommitEngine(min_silence_s=0.5, require_confirmation=False)
    r = eng.step([Segment(0.0, 1.0, "go now"), Segment(1.0, 2.0, "x")],
                 buffer_duration=2.0)
    assert r.deltas == ["go now"]


def test_prompt_returns_committed_tail():
    eng = CommitEngine(require_confirmation=False)
    eng.step([Segment(0.0, 1.0, "alpha beta gamma"), Segment(1.0, 2.0, "x")],
             buffer_duration=2.0)
    assert eng.prompt(max_chars=5) == "gamma"[-5:]
