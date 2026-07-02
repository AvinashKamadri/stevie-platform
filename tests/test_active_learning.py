"""Uncertainty sampling (M6, Slice 1) — pure ranking core."""
from stevie_platform.canonical import active_learning as al


def _s(lk, rk, p):
    return {"left_key": lk, "right_key": rk, "probability": p}


def test_uncertainty_is_distance_from_half():
    assert al.uncertainty(0.5) == 0.0
    assert al.uncertainty(0.99) == 0.49
    assert al.uncertainty(0.01) == 0.49


def test_rank_puts_most_uncertain_first():
    scored = [_s("a", "b", 0.98), _s("c", "d", 0.51), _s("e", "f", 0.20)]
    ranked = al.rank_by_uncertainty(scored)
    assert [r["right_key"] for r in ranked] == ["d", "f", "b"]  # 0.51, 0.20, 0.98


def test_rank_is_deterministic_on_ties():
    # Equal uncertainty (both 0.10 from 0.5) must resolve by key, not input order.
    scored = [_s("z", "z", 0.6), _s("a", "a", 0.4), _s("m", "m", 0.6)]
    ranked = al.rank_by_uncertainty(scored)
    keys = [(r["left_key"], r["right_key"]) for r in ranked]
    assert keys == [("a", "a"), ("m", "m"), ("z", "z")]
    # Re-running on shuffled input gives the identical order.
    assert al.rank_by_uncertainty(list(reversed(scored))) == ranked


def test_rank_excludes_pairs():
    scored = [_s("a", "b", 0.5), _s("c", "d", 0.5)]
    ranked = al.rank_by_uncertainty(scored, exclude=frozenset({("a", "b")}))
    assert [(r["left_key"], r["right_key"]) for r in ranked] == [("c", "d")]


def test_select_queue_respects_limit_and_exclusions():
    scored = [_s(str(i), "x", 0.5 + i * 0.01) for i in range(10)]
    q = al.select_queue(scored, limit=3, exclude=frozenset({("0", "x")}))
    assert len(q) == 3
    assert ("0", "x") not in {(r["left_key"], r["right_key"]) for r in q}
    # All tagged uncertainty when random_fraction=0.
    assert all(r["strategy"] == "uncertainty" for r in q)


def test_select_queue_mixes_random_deterministically():
    scored = [_s(f"k{i:02d}", "x", 0.5 + i * 0.02) for i in range(20)]
    q1 = al.select_queue(scored, limit=10, random_fraction=0.3)
    q2 = al.select_queue(scored, limit=10, random_fraction=0.3)
    assert q1 == q2  # reproducible
    strat = [r["strategy"] for r in q1]
    assert strat.count("uncertainty") == 7 and strat.count("random") == 3
    # No pair appears twice across the two strategies.
    keys = [(r["left_key"], r["right_key"]) for r in q1]
    assert len(keys) == len(set(keys))


def test_select_queue_empty_inputs():
    assert al.select_queue([], limit=5) == []
    assert al.select_queue([_s("a", "b", 0.5)], limit=0) == []
