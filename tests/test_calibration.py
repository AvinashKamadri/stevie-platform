"""Unit tests for M5.4 Platt calibration — pure functions only (DB-touching
run_calibrate is exercised by `cli calibrate`)."""
import math

from stevie_platform.canonical.calibration import (
    apply_platt, brier_score, fit_platt, reliability_bins,
)


def _separable_scores(n=40):
    # scores well above 0 -> merge (1), well below 0 -> distinct (0)
    scores = [3.0 + i * 0.1 for i in range(n // 2)] + [-3.0 - i * 0.1 for i in range(n // 2)]
    y = [1] * (n // 2) + [0] * (n // 2)
    return scores, y


def test_fit_platt_is_deterministic():
    scores, y = _separable_scores()
    p1 = fit_platt(scores, y)
    p2 = fit_platt(scores, y)
    assert p1.coef_[0][0] == p2.coef_[0][0]
    assert p1.intercept_[0] == p2.intercept_[0]


def test_platt_preserves_ranking_not_just_direction():
    # Platt scaling is monotonic in the raw score — it cannot reorder pairs,
    # only rescale their probabilities. This is the property the user's
    # message specifically called out: calibration != re-ranking.
    scores, y = _separable_scores()
    platt = fit_platt(scores, y)
    calibrated = apply_platt(platt, scores)
    order_before = sorted(range(len(scores)), key=lambda i: scores[i])
    order_after = sorted(range(len(scores)), key=lambda i: calibrated[i])
    assert order_before == order_after


def test_platt_high_score_calibrates_above_low_score():
    platt = fit_platt(*_separable_scores())
    lo, hi = apply_platt(platt, [-5.0, 5.0])
    assert lo < hi


def test_apply_platt_on_empty_is_safe():
    platt = fit_platt(*_separable_scores())
    assert apply_platt(platt, []) == []


def test_brier_score_perfect_predictions_is_zero():
    assert brier_score([1.0, 0.0, 1.0], [1, 0, 1]) == 0.0


def test_brier_score_worst_case_is_one():
    assert brier_score([0.0, 1.0], [1, 0]) == 1.0


def test_brier_score_uninformative_half_on_balanced_set():
    assert math.isclose(brier_score([0.5, 0.5], [1, 0]), 0.25)


def test_brier_score_empty_is_nan():
    assert math.isnan(brier_score([], []))


def test_reliability_bins_counts_sum_to_total():
    probs = [0.05, 0.15, 0.55, 0.95, 0.92]
    y = [0, 0, 1, 1, 0]
    bins = reliability_bins(probs, y, n_bins=10)
    assert sum(b["n"] for b in bins) == len(probs)
    assert len(bins) == 10


def test_reliability_bins_empty_bin_reports_none_not_zero():
    bins = reliability_bins([0.05], [0], n_bins=10)
    populated = [b for b in bins if b["n"] > 0]
    empty = [b for b in bins if b["n"] == 0]
    assert len(populated) == 1
    assert all(b["avg_predicted"] is None and b["empirical_rate"] is None for b in empty)


def test_reliability_bins_perfect_calibration_matches_in_bin():
    # everyone at p=0.95 who actually merges -> avg_predicted ~ empirical_rate
    probs = [0.95, 0.95, 0.95, 0.95]
    y = [1, 1, 1, 0]
    bins = reliability_bins(probs, y, n_bins=10)
    b = next(b for b in bins if b["n"] == 4)
    assert math.isclose(b["avg_predicted"], 0.95)
    assert math.isclose(b["empirical_rate"], 0.75)
