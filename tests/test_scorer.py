"""Unit tests for M5.3 scorer logic — vectorization, fitting determinism, and
coefficient reporting. DB-touching load_labeled_dataset/run_train are exercised
by `cli train`; everything here is pure sklearn given an in-memory matrix."""
import numpy as np

from stevie_platform.canonical.features import FEATURE_NAMES
from stevie_platform.canonical.scorer import (
    BINARY_FEATURES, FEATURE_ORDER, SCALE_FEATURES, N_SCALE,
    coefficient_table, fit_model, to_row, transform,
)


def _features(**overrides):
    base = {name: (False if name in BINARY_FEATURES else 0.0) for name in FEATURE_NAMES}
    base.update(overrides)
    return base


# --- to_row: vectorization ---------------------------------------------------
def test_feature_order_covers_every_named_feature():
    assert set(FEATURE_ORDER) == set(FEATURE_NAMES)
    assert set(SCALE_FEATURES) | set(BINARY_FEATURES) == set(FEATURE_NAMES)
    assert not (set(SCALE_FEATURES) & set(BINARY_FEATURES))


def test_to_row_matches_feature_order_and_casts_bools():
    feats = _features(blocked_by_acronym=True, trigram_similarity=0.5)
    row = to_row(feats)
    assert len(row) == len(FEATURE_ORDER)
    idx = FEATURE_ORDER.index("blocked_by_acronym")
    assert row[idx] == 1.0
    idx2 = FEATURE_ORDER.index("trigram_similarity")
    assert row[idx2] == 0.5


# --- fit_model: determinism ---------------------------------------------------
def _toy_dataset(n=40, seed=0):
    rng = np.random.default_rng(seed)
    rows, labels = [], []
    for i in range(n):
        is_merge = i % 2 == 0
        feats = _features(
            trigram_similarity=0.9 if is_merge else 0.05,
            token_jaccard=0.8 if is_merge else 0.0,
            blocked_by_trigram=is_merge,
        )
        rows.append(to_row(feats))
        labels.append(1 if is_merge else 0)
    return np.array(rows, dtype=float), labels


def test_fit_model_is_deterministic():
    x, y = _toy_dataset()
    scaler1, clf1 = fit_model(x, y)
    scaler2, clf2 = fit_model(x, y)
    assert np.array_equal(clf1.coef_, clf2.coef_)
    assert np.array_equal(scaler1.mean_, scaler2.mean_)


def test_fit_model_recovers_the_separating_signal():
    x, y = _toy_dataset()
    scaler, clf = fit_model(x, y)
    x_t = transform(x, scaler)
    preds = clf.predict(x_t)
    assert list(preds) == y  # trivially separable toy data -> perfect fit


def test_transform_only_scales_the_scale_columns():
    x, y = _toy_dataset()
    scaler, _ = fit_model(x, y)
    x_t = transform(x, scaler)
    # binary columns (after the N_SCALE scale columns) are untouched (still 0/1)
    binary_block = x_t[:, N_SCALE:]
    assert set(np.unique(binary_block)) <= {0.0, 1.0}


def test_transform_on_empty_input_is_safe():
    x, y = _toy_dataset()
    scaler, _ = fit_model(x, y)
    empty = np.empty((0, len(FEATURE_ORDER)))
    out = transform(empty, scaler)
    assert out.shape == (0, len(FEATURE_ORDER))


# --- coefficient_table: sorted by |coef| descending --------------------------
def test_coefficient_table_sorted_by_absolute_value_desc():
    x, y = _toy_dataset()
    _, clf = fit_model(x, y)
    table = coefficient_table(clf)
    names = {name for name, _ in table}
    assert names == set(FEATURE_ORDER)
    abs_vals = [abs(c) for _, c in table]
    assert abs_vals == sorted(abs_vals, reverse=True)
    # the two features that actually carry signal in the toy data should rank
    # above the untouched (constant-zero) features
    top_names = {name for name, _ in table[:2]}
    assert top_names & {"trigram_similarity", "token_jaccard", "blocked_by_trigram"}
