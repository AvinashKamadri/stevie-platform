"""Unit tests for M5.5 frozen-evaluation metrics — pure functions only
(DB-touching run_evaluate is exercised by `cli evaluate`)."""
import math

from stevie_platform.canonical.scorer_eval import (
    confusion_counts, false_negatives, precision_recall_f1,
    provenance_breakdown, related_summary,
)


def _row(label, predicted, reasons=()):
    return {"label": label, "predicted_label": predicted, "reasons": list(reasons)}


def test_confusion_counts_basic():
    rows = [
        _row("merge", "merge"),      # tp
        _row("merge", "distinct"),   # fn
        _row("distinct", "merge"),   # fp
        _row("distinct", "distinct"),  # tn
    ]
    assert confusion_counts(rows) == {"tp": 1, "fp": 1, "fn": 1, "tn": 1}


def test_precision_recall_f1_values():
    counts = {"tp": 3, "fp": 1, "fn": 1, "tn": 5}
    m = precision_recall_f1(counts)
    assert math.isclose(m["precision"], 0.75)
    assert math.isclose(m["recall"], 0.75)
    assert math.isclose(m["f1"], 0.75)
    assert m["support_merge"] == 4 and m["support_distinct"] == 6


def test_precision_recall_f1_handles_zero_denominator():
    # no predicted merges at all -> precision undefined (nan), not a crash
    counts = {"tp": 0, "fp": 0, "fn": 5, "tn": 10}
    m = precision_recall_f1(counts)
    assert math.isnan(m["precision"])
    assert m["recall"] == 0.0


def test_provenance_breakdown_counts_overlapping_reasons():
    rows = [
        _row("merge", "merge", ["acronym"]),
        _row("merge", "distinct", ["acronym"]),          # the M5.3 finding, reproduced
        _row("merge", "merge", ["trigram", "rare_token"]),  # counts under BOTH
        _row("distinct", "distinct", ["trigram"]),
    ]
    pb = provenance_breakdown(rows)
    assert pb["acronym"]["n"] == 2
    assert math.isclose(pb["acronym"]["recall"], 0.5)  # 1 of 2 acronym merges caught
    assert pb["trigram"]["n"] == 2  # the both-blocker row AND the distinct row
    assert pb["rare_token"]["n"] == 1


def test_false_negatives_only_missed_merges():
    rows = [
        _row("merge", "distinct"),      # false negative
        _row("merge", "merge"),         # true positive, not a FN
        _row("distinct", "merge"),      # false positive, not a FN
    ]
    fn = false_negatives(rows)
    assert len(fn) == 1 and fn[0]["label"] == "merge" and fn[0]["predicted_label"] == "distinct"


def test_related_summary_counts_only_no_rates():
    rows = [_row("related", "merge"), _row("related", "distinct"), _row("related", "distinct")]
    s = related_summary(rows)
    assert s == {"n": 3, "predicted_merge": 1, "predicted_distinct": 2}
    assert "recall" not in s and "precision" not in s  # never scored as a rate
