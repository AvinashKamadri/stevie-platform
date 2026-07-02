"""Fact confidence scorer (M7) — pure, explainable core."""
from stevie_platform.canonical import confidence as c


def test_controlled_type_scores_higher_than_free_text_at_same_corroboration():
    ctrl, _ = c.score_entity("country", 5)
    free, _ = c.score_entity("organization", 5)
    assert ctrl > free


def test_singleton_is_penalized():
    single, rs = c.score_entity("organization", 1)
    many, _ = c.score_entity("organization", 50)
    assert single < many
    assert any("singleton" in r for r in rs)


def test_monotonic_in_corroboration():
    # More corroboration never lowers the score.
    scores = [c.score_entity("organization", n)[0] for n in (1, 2, 3, 10, 100)]
    assert scores == sorted(scores)


def test_score_in_range_and_has_reasons():
    for t in ("country", "organization", "person", "category_definition"):
        for n in (1, 2, 5, 999):
            s, reasons = c.score_entity(t, n)
            assert 0.0 <= s <= 1.0
            assert reasons, "every score must carry >=1 reason (explainability)"


def test_reasons_mention_corroboration_count():
    _, reasons = c.score_entity("industry", 246)
    assert any("246" in r for r in reasons)


def test_bands():
    assert c.confidence_band(0.90) == "high"
    assert c.confidence_band(0.70) == "medium"
    assert c.confidence_band(0.40) == "low"


def test_controlled_singleton_lands_low():
    # A controlled-vocab value seen once = likely a normalization miss -> flagged.
    s, _ = c.score_entity("category_definition", 1)
    assert c.confidence_band(s) == "low"
