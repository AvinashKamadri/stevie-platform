"""
Merge/no-merge scorer (M5.3) — a boringly correct logistic-regression baseline.

Scope is locked to answering ONE question: can the engineered features
(canonical/features.py) produce a useful merge probability? Not: the best
model. Hyperparameter tuning, feature selection, and fancier algorithms are
explicitly out of scope until a baseline exists to measure them against. v1.1
adds one normalization feature (despaced_trigram_similarity, feature_version
v2) in response to the v1 frozen evaluation's false-negative list — same
algorithm, same design otherwise; see features.py's module docstring.

Pipeline (this module owns the train step; split.py partitions, calibration.py
Platt-scales, scorer_eval.py runs the one frozen evaluation):
    labeled gold pair -> split.assign_split -> {train, calibration, evaluation}
    train                -> fit scaler + logistic regression (this module)
    calibration/evaluation -> scored with the fitted model and persisted as a
                               provisional (raw) probability, immediately
                               superseded by calibration.run_calibrate's
                               Platt-scaled value — see model_predictions'
                               mutability note (migration 011). NOT evaluated
                               yet either way; that is M5.5's single frozen run.

`related` is a REPORTED class, never a modeled one (see split.py's measured
starvation: 1/0/4 across partitions). It is excluded from the fit target and
from calibration, but pairs it labels are still scored like anything else in
their partition — a related pair's predicted probability is data for M5.5's
report, not for training.

Artifacts: the fitted (scaler, classifier) pair is pickled to
artifacts/models/<model_version>.joblib (gitignored — a build product,
regenerable from a fixed model_version/feature_version/split_version). The
durable, versioned record is model_registry (migration 012); model outputs land
in model_predictions (migration 011), one row per (candidate, model_version),
mutable until model_registry.metrics_summary is set (M5.5 freezes it).
"""
from __future__ import annotations

import json

from stevie_platform.canonical.candidates import order_pair
from stevie_platform.canonical.features import FEATURE_NAMES

MODEL_VERSION = "v1"
ALGORITHM = "logistic_regression"

# Binary indicators — already 0/1, comparable scale to each other; left raw.
BINARY_FEATURES = (
    "blocked_by_trigram", "blocked_by_rare_token", "blocked_by_acronym",
    "is_acronym_expansion",
)
# Continuous/count features — standardized (fit on TRAIN only, applied
# unchanged elsewhere). NOTE: prefix_overlap and suffix_match are continuous
# ratios in [0,1] (longest-common-affix / longer length), not booleans, despite
# reading like structural yes/no signals — they belong here with the other
# ratio-valued features (trigram_similarity, token_jaccard,
# normalized_token_overlap are Jaccard/overlap coefficients also in [0,1];
# shared_rare_token_count and length_ratio are the exceptions with different
# natural ranges). Standardizing keeps the regularization penalty comparable
# across all of them regardless of each one's natural scale.
SCALE_FEATURES = (
    "trigram_similarity", "token_jaccard", "length_ratio",
    "shared_rare_token_count", "normalized_token_overlap",
    "despaced_trigram_similarity",
    "prefix_overlap", "suffix_match",
    "acronym_x_trigram", "acronym_x_jaccard",
)

# Fixed design-matrix column order: scaled columns first, then binary columns.
# Guarded against drift if features.py's feature set ever changes without
# updating this module.
FEATURE_ORDER = SCALE_FEATURES + BINARY_FEATURES
assert set(FEATURE_ORDER) == set(FEATURE_NAMES), \
    "scorer.FEATURE_ORDER must cover exactly features.FEATURE_NAMES"

N_SCALE = len(SCALE_FEATURES)


# --- pure: vectorization + fitting (no DB/IO; unit-tested directly) ---------

def to_row(features: dict) -> list[float]:
    """One candidate's named feature dict -> a fixed-order numeric row."""
    return [float(features[name]) for name in FEATURE_ORDER]


def fit_model(x_train_raw, y_train, *, random_state: int = 0, class_weight=None):
    """Fit a StandardScaler (on the scale-feature columns only) + a logistic
    regression on the transformed matrix. Deterministic: lbfgs (sklearn's
    default solver) has no internal randomness for this problem size;
    random_state is set anyway so a future solver change stays reproducible.
    Pure given x_train_raw/y_train — no DB, no file IO — so determinism is
    directly unit-testable (same inputs -> bit-identical coefficients).

    class_weight: None (default, v1/v1.1/v1.2) fits every row equally, which
    is exactly what lets ONE global coefficient on a majority-population
    feature dominate a minority subgroup's classification (see the v1.1
    acronym decomposition in features.py's module docstring). 'balanced'
    reweights the loss by inverse class frequency — a pre-authorized fallback
    if interaction features alone don't move acronym recall."""
    import numpy as np
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler

    x_train_raw = np.asarray(x_train_raw, dtype=float)
    scaler = StandardScaler().fit(x_train_raw[:, :N_SCALE])
    x_train = transform(x_train_raw, scaler)
    clf = LogisticRegression(max_iter=1000, random_state=random_state, class_weight=class_weight)
    clf.fit(x_train, y_train)
    return scaler, clf


def transform(x_raw, scaler):
    """Apply a FITTED scaler to the scale-feature columns only, leaving the
    binary columns untouched. Same transform for train/calibration/evaluation —
    the scaler's mean/std come from train alone, never refit."""
    import numpy as np
    x_raw = np.asarray(x_raw, dtype=float)
    x = x_raw.copy()
    if len(x):
        x[:, :N_SCALE] = scaler.transform(x_raw[:, :N_SCALE])
    return x


def coefficient_table(clf) -> list[tuple[str, float]]:
    """(feature_name, coefficient) pairs in FEATURE_ORDER, sorted by |coef|
    descending — a near-zero or wrong-signed coefficient on a feature you
    expected to matter is the fastest sanity check this baseline gives you."""
    coefs = clf.coef_[0].tolist()
    pairs = list(zip(FEATURE_ORDER, coefs))
    return sorted(pairs, key=lambda kv: -abs(kv[1]))


# --- DB-touching: dataset assembly + orchestration --------------------------

async def load_labeled_dataset(conn, *, corpus: str = "v2"):
    """Join every labeled gold pair to its candidate features, tagged with its
    deterministic split partition. Returns (rows, fallback_n).

    Prefers the persisted feature vector on organization_merge_candidate (exact
    parity with what would be scored in production for that pair). Falls back
    to computing features fresh (reasons=(), since the pair was never
    surfaced — a true blocking_gap) only if the pair isn't currently a
    candidate; fallback_n reports how often that happened. As of M4 completion
    (recall --corpus v2: 100% overall/achievable recall), this should be 0."""
    from stevie_platform.canonical.features import (
        FEATURE_VERSION, compute_rare_tokens, extract_features,
    )
    from stevie_platform.canonical.recall import load_corpus
    from stevie_platform.canonical.split import assign_split

    gold, _version, _missing = load_corpus(corpus)
    cur = await conn.execute(
        "select id, left_key, right_key, features, feature_version "
        "from organization_merge_candidate"
    )
    lookup = {(r["left_key"], r["right_key"]): r for r in await cur.fetchall()}

    rare_tokens = None
    rows = []
    fallback_n = 0
    for g in gold:
        ka, kb = g["key_a"], g["key_b"]
        lk, _, rk, _ = order_pair(ka, 0, kb, 0)
        hit = lookup.get((lk, rk))
        if hit is not None and hit["features"] is not None:
            feats, fver, cand_id = hit["features"], hit["feature_version"], hit["id"]
        else:
            if rare_tokens is None:
                rare_tokens = await compute_rare_tokens(conn)
            feats, fver, cand_id = extract_features(ka, kb, (), rare_tokens=rare_tokens), FEATURE_VERSION, None
            fallback_n += 1
        rows.append({
            "key_a": ka, "key_b": kb, "left_key": lk, "right_key": rk, "label": g["label"],
            "features": feats, "feature_version": fver, "candidate_id": cand_id,
            "partition": assign_split(ka, kb),
        })
    return rows, fallback_n


def _class_counts(rows: list[dict]) -> dict:
    from collections import Counter
    out: dict[str, dict[str, int]] = {}
    for partition in ("train", "calibration", "evaluation"):
        c = Counter(r["label"] for r in rows if r["partition"] == partition)
        out[partition] = {"merge": c.get("merge", 0), "distinct": c.get("distinct", 0),
                           "related": c.get("related", 0)}
    return out


async def run_train(*, model_version: str = MODEL_VERSION, persist_rows: bool = True,
                     class_weight=None) -> dict:
    """CLI entry: train on `train`, score (not evaluate) `calibration` +
    `evaluation`, persist the artifact + registry row + predictions."""
    import numpy as np
    import joblib
    from stevie_platform import db
    from stevie_platform.canonical.features import FEATURE_VERSION
    from stevie_platform.canonical.split import SPLIT_VERSION
    from stevie_platform.config import BASE_DIR

    p = await db.pool()
    async with p.connection() as conn:
        if persist_rows:
            existing = await conn.execute(
                "select metrics_summary from model_registry where model_version = %s",
                (model_version,),
            )
            row = await existing.fetchone()
            if row and row["metrics_summary"] is not None:
                raise SystemExit(
                    f"model_version {model_version!r} is FROZEN (has a metrics_summary "
                    f"from a completed evaluation) — retrain as a new model_version, "
                    f"don't overwrite it.")

        rows, fallback_n = await load_labeled_dataset(conn)
        feature_versions = {r["feature_version"] for r in rows}
        if feature_versions != {FEATURE_VERSION}:
            raise SystemExit(
                f"mixed/unexpected feature_version(s) in the training set: "
                f"{feature_versions}; expected exactly {{{FEATURE_VERSION!r}}}. "
                f"Re-run `cli features` before training.")

        train_rows = [r for r in rows if r["partition"] == "train" and r["label"] in ("merge", "distinct")]
        calib_rows = [r for r in rows if r["partition"] == "calibration"]
        eval_rows = [r for r in rows if r["partition"] == "evaluation"]

        x_train_raw = np.array([to_row(r["features"]) for r in train_rows])
        y_train = [1 if r["label"] == "merge" else 0 for r in train_rows]
        scaler, clf = fit_model(x_train_raw, y_train, class_weight=class_weight)
        coeffs = coefficient_table(clf)

        def score(rows_: list[dict]):
            if not rows_:
                return []
            x_raw = np.array([to_row(r["features"]) for r in rows_])
            x = transform(x_raw, scaler)
            return clf.predict_proba(x)[:, 1].tolist()

        calib_proba = score(calib_rows)
        eval_proba = score(eval_rows)

        pred_rows = []
        for r, prob in list(zip(calib_rows, calib_proba)) + list(zip(eval_rows, eval_proba)):
            pred_rows.append((
                r["candidate_id"], r["left_key"], r["right_key"], model_version, r["feature_version"],
                round(prob, 6), "merge" if prob >= 0.5 else "distinct", json.dumps(r["features"]),
            ))

        artifact_dir = BASE_DIR / "artifacts" / "models"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        artifact_path = artifact_dir / f"{model_version}.joblib"
        joblib.dump({"scaler": scaler, "clf": clf, "feature_order": FEATURE_ORDER}, artifact_path)

        class_counts = _class_counts(rows)

        if persist_rows:
            if pred_rows:
                async with conn.cursor() as cur:
                    await cur.executemany(
                        """insert into model_predictions
                             (candidate_id, left_key, right_key, model_version, feature_version,
                              probability, predicted_label, feature_snapshot)
                           values (%s,%s,%s,%s,%s,%s,%s,%s)
                           on conflict (left_key, right_key, model_version) do update set
                             candidate_id = excluded.candidate_id,
                             probability = excluded.probability,
                             predicted_label = excluded.predicted_label,
                             feature_snapshot = excluded.feature_snapshot,
                             feature_version = excluded.feature_version,
                             created_at = now()""",
                        pred_rows,
                    )
            await conn.execute(
                """insert into model_registry
                     (model_version, feature_version, split_version, algorithm,
                      training_sample_size, class_counts, artifact_path, coefficients)
                   values (%s,%s,%s,%s,%s,%s,%s,%s)
                   on conflict (model_version) do update set
                     feature_version = excluded.feature_version,
                     split_version = excluded.split_version,
                     algorithm = excluded.algorithm,
                     training_sample_size = excluded.training_sample_size,
                     class_counts = excluded.class_counts,
                     artifact_path = excluded.artifact_path,
                     coefficients = excluded.coefficients,
                     training_timestamp = now()""",
                (model_version, FEATURE_VERSION, SPLIT_VERSION,
                 f"{ALGORITHM}(class_weight={class_weight!r})" if class_weight else ALGORITHM,
                 len(train_rows), json.dumps(class_counts), str(artifact_path),
                 json.dumps(coeffs)),
            )
            await conn.commit()

    summary = {
        "model_version": model_version, "feature_version": FEATURE_VERSION,
        "split_version": SPLIT_VERSION,
        "algorithm": f"{ALGORITHM}(class_weight={class_weight!r})" if class_weight else ALGORITHM,
        "train_n": len(train_rows), "calibration_n": len(calib_rows), "evaluation_n": len(eval_rows),
        "fallback_n": fallback_n, "class_counts": class_counts,
        "predictions_written": len(pred_rows) if persist_rows else 0,
        "artifact_path": str(artifact_path), "persisted": persist_rows,
        "coefficients": coeffs,
    }
    _print_report(summary)
    return summary


def _print_report(s: dict) -> None:
    print("\n" + "=" * 60)
    print(f" SCORER TRAINING  —  model {s['model_version']}  ({s['algorithm']})")
    print("=" * 60)
    print(f"  feature_version   {s['feature_version']}")
    print(f"  split_version     {s['split_version']}")
    print(f"  train pairs       {s['train_n']:>6}   (merge/distinct only; 'related' excluded from fit)")
    print(f"  calibration pairs {s['calibration_n']:>6}   (scored, not yet used for calibration — M5.4)")
    print(f"  evaluation pairs  {s['evaluation_n']:>6}   (scored, not yet evaluated — M5.5)")
    if s["fallback_n"]:
        print(f"  ⚠ {s['fallback_n']} gold pairs had no candidate row — features computed fresh (blocking_gap)")
    print("-" * 60)
    print("  class counts by partition:")
    for part, counts in s["class_counts"].items():
        print(f"    {part:<12} merge={counts['merge']:<4} distinct={counts['distinct']:<4} related={counts['related']}")
    print("-" * 60)
    print("  coefficients (sorted by |coefficient|):")
    for name, coef in s["coefficients"]:
        print(f"    {name:<26} {coef:+.4f}")
    print("-" * 60)
    print(f"  predictions written   {s['predictions_written']:>6}")
    print(f"  artifact              {s['artifact_path']}")
    print("=" * 60 + "\n")
