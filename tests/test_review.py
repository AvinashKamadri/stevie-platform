"""Unit tests for the M5/Phase-3 human review workflow — pure functions only
(DB-touching run_review/_load_queue are exercised interactively via `cli review`)."""
from stevie_platform.canonical.review import (
    acronym_priority, choose_winner, distinct_decision_keys, is_eligible,
)


# --- is_eligible -------------------------------------------------------------
def test_eligible_when_nothing_decided_or_reviewed():
    assert is_eligible("a", "b", decided_keys=frozenset(), reviewed_pairs=frozenset())


def test_ineligible_when_left_key_already_decided():
    assert not is_eligible("a", "b", decided_keys=frozenset({"a"}), reviewed_pairs=frozenset())


def test_ineligible_when_right_key_already_decided():
    assert not is_eligible("a", "b", decided_keys=frozenset({"b"}), reviewed_pairs=frozenset())


def test_ineligible_when_exact_pair_already_reviewed():
    assert not is_eligible("a", "b", decided_keys=frozenset(), reviewed_pairs=frozenset({("a", "b")}))


def test_eligible_when_a_DIFFERENT_pair_was_reviewed():
    # reviewing 'a'/'c' as related must not suppress 'a'/'b' — a key can be
    # related to more than one other org.
    assert is_eligible("a", "b", decided_keys=frozenset(), reviewed_pairs=frozenset({("a", "c")}))


# --- acronym_priority --------------------------------------------------------
def test_acronym_priority_is_the_shorter_keys_length():
    assert acronym_priority("ibm", "international business machines") == 3
    assert acronym_priority("nasa", "national aeronautics and space administration") == 4


def test_acronym_priority_ignores_spaces_in_the_shorter_side():
    # a two-word "acronym" side (rare, but defensive) still measures letters only
    assert acronym_priority("a b", "some long expansion") == 2


def test_longer_acronym_sorts_ahead_of_shorter_one():
    # 'nasa' (4) should out-rank 'ab' (2) when sorting by -acronym_priority
    pairs = [("ab", "astrazeneca bulgaria"), ("nasa", "national aeronautics space admin")]
    ranked = sorted(pairs, key=lambda p: -acronym_priority(*p))
    assert ranked[0][0] == "nasa"


# --- choose_winner ------------------------------------------------------------
def test_choose_winner_more_recognitions_wins():
    assert choose_winner("acme", 5, "acme corp", 20) == ("acme corp", "acme")
    assert choose_winner("acme", 20, "acme corp", 5) == ("acme", "acme corp")


def test_choose_winner_ties_default_to_left():
    assert choose_winner("acme", 10, "acme corp", 10) == ("acme", "acme corp")


# --- distinct_decision_keys ---------------------------------------------------
def test_distinct_decision_keys_is_order_independent():
    a = distinct_decision_keys("b", "a")
    b = distinct_decision_keys("a", "b")
    assert a == b == ("b", "a")  # winner_key="b" (larger), loser_key="a" (smaller)


def test_distinct_decision_keys_never_equal():
    winner, loser = distinct_decision_keys("acme", "acme corp")
    assert winner != loser
