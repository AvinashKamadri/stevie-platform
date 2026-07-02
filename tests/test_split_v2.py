"""Three-way v2 split (M6, Slice 2)."""
import pytest

from stevie_platform.canonical.split_v2 import (
    PARTITIONS, SPLIT_V2_VERSION, _ratios, assign_split_v2,
)


def test_only_three_buckets_no_evaluation():
    assert set(PARTITIONS) == {"train", "calibration", "validation"}
    assert "evaluation" not in PARTITIONS


def test_ratios_sum_to_one():
    total = sum(share for _, share in _ratios(SPLIT_V2_VERSION))
    assert abs(total - 1.0) < 1e-9


def test_assignment_is_one_of_the_buckets():
    for a, b in [("ibm", "international business machines"), ("apple", "apple inc"),
                 ("ca", "cessna aircraft"), ("acme", "acme corp"), ("x", "y")]:
        assert assign_split_v2(a, b) in PARTITIONS


def test_deterministic_and_order_independent():
    # Same pair, either argument order (split reuses order-independent hashing).
    assert assign_split_v2("alpha", "beta") == assign_split_v2("beta", "alpha")
    # Stable across calls.
    assert assign_split_v2("foo corp", "foo corporation") == assign_split_v2("foo corp", "foo corporation")


def test_unknown_version_raises():
    with pytest.raises(ValueError):
        assign_split_v2("a", "b", version="does-not-exist")


def test_distribution_is_roughly_70_15_15():
    # Over many synthetic pairs the empirical split should track the ratios.
    from collections import Counter
    c = Counter(assign_split_v2(f"org{i}", f"company{i}") for i in range(3000))
    frac = {k: v / 3000 for k, v in c.items()}
    assert 0.62 < frac["train"] < 0.78
    assert 0.09 < frac["calibration"] < 0.21
    assert 0.09 < frac["validation"] < 0.21
