"""Tier 1: text utilities. Fast, no GPU, no model."""

from ntok.textutil import dedup_overlap, normalize, tokens, wer


def test_normalize_strips_punct_and_case():
    assert normalize("Hello, World!") == "hello world"
    assert normalize("  The   QUICK  brown.  ") == "the quick brown"
    assert normalize("") == ""


def test_tokens_keeps_intra_word_punct():
    assert tokens("Don't stop, co-op!") == ["don't", "stop", "co-op"]


def test_dedup_overlap_single_word_seam():
    # Classic boundary doubling: committed ends with "world", repeat re-emitted.
    assert dedup_overlap("hello world", "world again") == "again"


def test_dedup_overlap_multiword_seam():
    assert dedup_overlap("the quick brown fox", "brown fox jumps over") == "jumps over"


def test_dedup_overlap_no_false_positive_midsentence():
    # No tail/head overlap -> nothing removed.
    assert dedup_overlap("i went to the store", "and bought milk") == "and bought milk"


def test_dedup_overlap_preserves_casing_and_punct_of_survivor():
    assert dedup_overlap("hello there", "There, friend.") == "friend."


def test_dedup_overlap_empty_addition():
    assert dedup_overlap("anything", "") == ""


def test_wer_perfect():
    assert wer("the quick brown fox", "The quick brown fox!") == 0.0


def test_wer_one_substitution():
    # 4 ref words, 1 wrong -> 0.25
    assert abs(wer("the quick brown fox", "the quick brown cat") - 0.25) < 1e-9


def test_wer_empty_reference():
    assert wer("", "") == 0.0
    assert wer("", "extra") == 1.0
