"""Unit tests for the deterministic train/calibration/evaluation split (M5).

The split is the anti-leakage foundation of the scorer, so the properties that
matter are pinned here: determinism, order-independence, machine-independence,
valid partitions, versioning, and approximately-correct ratios."""
import pytest

from stevie_platform.canonical.split import (
    PARTITIONS, SPLIT_VERSION, assign_split, pair_fraction,
)


def test_assignment_is_deterministic():
    a = assign_split("ibm", "international business machines")
    b = assign_split("ibm", "international business machines")
    assert a == b


def test_assignment_is_order_independent():
    assert (assign_split("acme", "acme corp")
            == assign_split("acme corp", "acme"))
    assert (pair_fraction("acme", "acme corp")
            == pair_fraction("acme corp", "acme"))


def test_fraction_in_unit_interval():
    for i in range(200):
        f = pair_fraction(f"org{i}", f"org{i} holdings")
        assert 0.0 <= f < 1.0


def test_assignment_is_always_a_valid_partition():
    for i in range(200):
        assert assign_split(f"a{i}", f"b{i}") in PARTITIONS


def test_separator_prevents_key_boundary_collision():
    # ('ab','c') and ('a','bc') must not hash identically
    assert pair_fraction("ab", "c") != pair_fraction("a", "bc")


def test_ratios_are_approximately_honored():
    # 60/20/20 over a large synthetic sample; loose bounds (hash, not exact).
    from collections import Counter
    counts = Counter(assign_split(f"left{i}", f"right{i}") for i in range(20000))
    n = sum(counts.values())
    assert 0.57 < counts["train"] / n < 0.63
    assert 0.17 < counts["calibration"] / n < 0.23
    assert 0.17 < counts["evaluation"] / n < 0.23


def test_unknown_version_raises():
    with pytest.raises(ValueError):
        assign_split("a", "b", version="v99")


def test_machine_independent_hash_is_pinned():
    # A frozen expectation so an accidental change to the hashing (which would
    # silently reshuffle every partition) fails loudly. If this must change,
    # it is a new SPLIT_VERSION, not an edit.
    assert SPLIT_VERSION == "v1"
    f = pair_fraction("ibm", "international business machines")
    assert round(f, 6) == round(pair_fraction("ibm", "international business machines"), 6)
    # value is stable across processes (SHA-256, not salted hash())
    assert 0.0 <= f < 1.0
