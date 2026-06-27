"""Tier 1: flow + spoken-punctuation transform. Fast, no GPU, no model.

Covers the pure one-shot transform (``format_spoken``), the stateful streaming
wrapper (``FlowFormatter``) at delta boundaries, and the CommitEngine wiring
that gates it behind ``spoken_punctuation`` (default off).
"""

from __future__ import annotations

from ntok.commit import CommitEngine, Segment
from ntok.textutil import FlowFormatter, format_spoken


# --- the pure one-shot transform -------------------------------------------

def test_spec_example():
    spoken = "the build passed period ship it new paragraph next comma the api"
    assert format_spoken(spoken) == "The build passed. Ship it.\n\nNext, the api"


def test_each_one_word_command():
    assert format_spoken("a comma b") == "A, b"
    assert format_spoken("a period b") == "A. B"
    assert format_spoken("a colon b") == "A: b"
    assert format_spoken("a semicolon b") == "A; b"


def test_each_two_word_command():
    assert format_spoken("a full stop b") == "A. B"
    assert format_spoken("really question mark yes") == "Really? Yes"
    assert format_spoken("wow exclamation point ok") == "Wow! Ok"
    assert format_spoken("wow exclamation mark ok") == "Wow! Ok"
    assert format_spoken("one new line two") == "One\nTwo"
    assert format_spoken("one new paragraph two") == "One.\n\nTwo"


def test_parens_spacing():
    assert format_spoken("see open paren note close paren done") == "See (note) done"


def test_dash_is_spaced():
    assert format_spoken("state dash of dash art") == "State - of - art"


def test_no_space_before_closing_punct():
    assert format_spoken("hello comma world period done") == "Hello, world. Done"


def test_auto_cap_at_start_and_after_sentence_end():
    assert format_spoken("hello period world") == "Hello. World"
    assert format_spoken("hello question mark world") == "Hello? World"
    assert format_spoken("hello exclamation point world") == "Hello! World"


def test_auto_cap_after_newline():
    assert format_spoken("hello new line world") == "Hello\nWorld"


def test_no_auto_cap_after_comma_or_colon():
    assert format_spoken("hello comma world") == "Hello, world"
    assert format_spoken("note colon details") == "Note: details"


def test_cap_command_capitalizes_next_word_only():
    assert format_spoken("meet cap alice today") == "Meet Alice today"
    assert format_spoken("the cap api is down") == "The Api is down"
    assert format_spoken("hi cap bob comma welcome") == "Hi Bob, welcome"


def test_capital_alias():
    assert format_spoken("call cap bob") == "Call Bob"


def test_strips_whisper_edge_punct_and_fragment_caps():
    # Whisper-style fragment: capitalized first letter + trailing period.
    assert format_spoken("The build passed.") == "The build passed"
    assert format_spoken("Ship.") == "Ship"


def test_keeps_intra_word_punct_and_digits():
    assert format_spoken("don't ship co-op 3.14 now") == "Don't ship co-op 3.14 now"


def test_pronoun_i_stays_capitalized():
    assert format_spoken("yesterday i went and i'm back") == "Yesterday I went and I'm back"


def test_single_line_break_does_not_terminate_sentence():
    # Soft wrap (lists): no auto-period, unlike a paragraph break.
    assert format_spoken("buy milk new line buy eggs") == "Buy milk\nBuy eggs"


# --- single space after sentence-ending punctuation (Problem 2 regression) --
#
# "Ship it.Next" must render as "Ship it. Next": no space BEFORE . , ; : ? ! ),
# but exactly one space BETWEEN the mark and the following word — including when
# the mark and the next word land in separate deltas. Newline commands instead
# absorb the owed space so there's no stray gap around the break.

def test_in_sentence_period_gets_one_following_space():
    assert format_spoken("ship it period next") == "Ship it. Next"

def test_question_and_exclamation_get_one_following_space():
    assert format_spoken("ready question mark go") == "Ready? Go"
    assert format_spoken("go exclamation point now") == "Go! Now"

def test_sentence_end_space_across_delta_boundary():
    # Period ends one delta; the next word arrives in the following delta.
    f = FlowFormatter()
    out = f.feed("ship it period") + f.feed("next") + f.flush()
    assert out == "Ship it. Next"
    # Same for ? split across deltas (mark completes in delta 1, word in delta 2).
    f = FlowFormatter()
    out = f.feed("ready question mark") + f.feed("go") + f.flush()
    assert out == "Ready? Go"
    # And for ! split across deltas.
    f = FlowFormatter()
    out = f.feed("go exclamation point") + f.feed("now") + f.flush()
    assert out == "Go! Now"

def test_newline_commands_absorb_surrounding_spaces():
    # No stray space before/after a soft line break or paragraph break, even
    # though a space was owed from the preceding word.
    assert format_spoken("buy milk new line buy eggs") == "Buy milk\nBuy eggs"
    assert format_spoken("done new paragraph next") == "Done.\n\nNext"
    # Newline command split across a delta boundary still absorbs the space.
    f = FlowFormatter()
    out = f.feed("buy milk new") + f.feed("line buy eggs") + f.flush()
    assert out == "Buy milk\nBuy eggs"


# --- streaming / delta-boundary behavior -----------------------------------

def test_streaming_period_then_word_in_next_delta():
    f = FlowFormatter()
    out = f.feed("the build passed period")
    out += f.feed("ship it")
    out += f.flush()
    assert out == "The build passed. Ship it"


def test_streaming_two_word_command_split_across_deltas():
    f = FlowFormatter()
    out = f.feed("really question")  # "question" is held — may be "question mark"
    assert "question" not in out      # not yet emitted as a word
    out += f.feed("mark yes")
    out += f.flush()
    assert out == "Really? Yes"


def test_streaming_cap_command_split_across_deltas():
    f = FlowFormatter()
    out = f.feed("meet cap")          # "cap" held until its word arrives
    out += f.feed("alice")
    out += f.flush()
    assert out == "Meet Alice"


def test_streaming_dangling_prefix_flushed_as_word():
    f = FlowFormatter()
    out = f.feed("see you new")        # ends on a bare prefix
    out += f.flush()                   # end of audio: "new" becomes a plain word
    assert out == "See you new"


def test_streaming_spacing_persists_across_word_only_deltas():
    f = FlowFormatter()
    out = f.feed("the quick")
    out += f.feed("brown fox")
    out += f.flush()
    assert out == "The quick brown fox"


def test_streaming_output_is_append_only():
    # Each feed() returns exactly the suffix appended to the running text.
    f = FlowFormatter()
    acc = ""
    for delta in ["the build passed period", "ship it", "new paragraph", "next comma the api"]:
        acc += f.feed(delta)
        assert f.text == acc        # running text only ever grows by the return
    acc += f.flush()
    assert acc == f.text == "The build passed. Ship it.\n\nNext, the api"


# --- CommitEngine wiring ---------------------------------------------------

def _commit_all(engine: CommitEngine, phrases: list[str]) -> str:
    """Drive each phrase through the engine as its own confirmed, silence-ended
    segment, collecting every delta — a stand-in for the streaming loop."""
    out = ""
    for ph in phrases:
        engine.require_confirmation = False
        res = engine.step([Segment(0.0, 1.0, ph)], 1.0, trailing_silence=True)
        out += "".join(res.deltas)
    res = engine.step([], 0.0, ended=True)
    out += "".join(res.deltas)
    return out


def test_engine_flow_off_is_legacy_behavior():
    eng = CommitEngine(require_confirmation=False, capitalize_first=True)
    out = _commit_all(eng, ["the build passed", "ship it"])
    # Legacy: verbatim phrases joined with a single space, first char capitalized.
    assert out == "The build passed ship it"


def test_engine_flow_on_applies_spoken_punctuation():
    eng = CommitEngine(require_confirmation=False, spoken_punctuation=True)
    out = _commit_all(eng, ["the build passed period", "ship it"])
    assert out == "The build passed. Ship it"
    assert eng.output_text == "The build passed. Ship it"


def test_engine_flow_command_split_across_commits():
    # The period is the last token of one commit; the next word is its own commit.
    eng = CommitEngine(require_confirmation=False, spoken_punctuation=True)
    out = _commit_all(eng, ["the question period", "quick brown fox"])
    assert out == "The question. Quick brown fox"


def test_engine_flow_flushes_dangling_prefix_on_end():
    eng = CommitEngine(require_confirmation=False, spoken_punctuation=True)
    out = _commit_all(eng, ["hello there new"])
    assert out == "Hello there new"


def test_engine_flow_keeps_raw_committed_text_for_dedup_context():
    # committed_text stays raw (command words intact) so dedup/prompt still work.
    eng = CommitEngine(require_confirmation=False, spoken_punctuation=True)
    _commit_all(eng, ["the build passed period"])
    assert "period" in eng.committed_text
    assert eng.output_text == "The build passed."
