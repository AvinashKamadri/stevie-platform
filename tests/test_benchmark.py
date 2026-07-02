"""
Frozen-benchmark mechanism + regression guard (M6, Slice 1).

The guard test is the Slice 1 acceptance criterion: if any frozen-evaluation
pair leaks into the active-learning training pool, the pipeline MUST fail
explicitly rather than silently comparing against a moving target.
"""
import pytest

from stevie_platform.canonical import benchmark as b
from stevie_platform.canonical.split import assign_split


def _gold(*pairs):
    """Minimal gold rows: (key_a, key_b, label)."""
    return [{"key_a": a, "key_b": bb, "label": lab} for a, bb, lab in pairs]


def test_build_frozen_pairs_selects_only_evaluation_partition():
    # A spread of pairs; the frozen set must be exactly those hashing to 'evaluation'.
    gold = _gold(
        ("ibm", "international business machines", "merge"),
        ("apple", "apple inc", "merge"),
        ("acme", "acme corp", "distinct"),
        ("ca", "cessna aircraft", "merge"),
        ("ab", "astrazeneca bulgaria", "distinct"),
    )
    frozen = b.build_frozen_pairs(gold)
    for r in frozen:
        assert assign_split(r["left_key"], r["right_key"], version="v1") == "evaluation"
    # And nothing hashing elsewhere sneaks in.
    expected = {b.ordered_pair(g["key_a"], g["key_b"]) for g in gold
                if assign_split(*b.ordered_pair(g["key_a"], g["key_b"]), version="v1") == "evaluation"}
    assert {(r["left_key"], r["right_key"]) for r in frozen} == expected


def test_build_frozen_pairs_is_deterministic_and_ordered():
    gold = _gold(("z", "a", "merge"), ("m", "b", "distinct"), ("q", "c", "merge"))
    out1 = b.build_frozen_pairs(gold)
    out2 = b.build_frozen_pairs(list(reversed(gold)))
    assert out1 == out2  # input order must not matter
    keys = [(r["left_key"], r["right_key"]) for r in out1]
    assert keys == sorted(keys)  # byte-stable ordering


def test_ordered_pair_is_symmetric():
    assert b.ordered_pair("b", "a") == b.ordered_pair("a", "b") == ("a", "b")


def test_digest_is_order_independent():
    a = [{"left_key": "a", "right_key": "b", "label": "merge"},
         {"left_key": "c", "right_key": "d", "label": "distinct"}]
    assert b._digest(a) == b._digest(list(reversed(a)))


def test_digest_changes_on_relabel():
    a = [{"left_key": "a", "right_key": "b", "label": "merge"}]
    c = [{"left_key": "a", "right_key": "b", "label": "distinct"}]
    assert b._digest(a) != b._digest(c)


# --- the regression guard: the Slice 1 acceptance criterion -----------------

def test_guard_passes_when_training_pool_is_disjoint():
    frozen = frozenset({("a", "b"), ("c", "d")})
    # No overlap -> must not raise.
    b.assert_no_contamination([("e", "f"), ("g", "h")], frozen=frozen)


def test_guard_raises_when_a_benchmark_pair_leaks_into_training():
    frozen = frozenset({("a", "b"), ("c", "d")})
    with pytest.raises(b.BenchmarkContaminationError) as exc:
        b.assert_no_contamination([("e", "f"), ("c", "d")], frozen=frozen)
    # Error names the offending pair — explicit, not silent.
    assert "c" in str(exc.value) and "d" in str(exc.value)


def test_find_contamination_returns_sorted_offenders():
    frozen = frozenset({("a", "b"), ("c", "d"), ("e", "f")})
    training = [("e", "f"), ("a", "b"), ("x", "y")]
    assert b.find_contamination(training, frozen) == [("a", "b"), ("e", "f")]


# --- the materialized frozen file (committed artifact) ----------------------

def test_frozen_file_exists_and_verifies():
    """The committed benchmark must load and pass its own integrity check."""
    v = b.verify()
    assert v["digest_ok"], "frozen benchmark content digest mismatch — file was edited"
    assert not v["relabeled"], f"benchmark pairs changed meaning: {v['relabeled']}"
    assert not v["dropped_from_corpus"], f"benchmark pairs vanished from corpus: {v['dropped_from_corpus']}"
    assert v["ok"]


def test_frozen_set_matches_recompute_from_current_corpus_component():
    """Sanity: every frozen pair really does hash to 'evaluation' under v1."""
    for pk in b.frozen_pair_set():
        assert assign_split(pk[0], pk[1], version="v1") == "evaluation"
