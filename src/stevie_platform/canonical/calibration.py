"""
Probability calibration (M5.4) — Platt scaling on the `calibration` split only.

Calibration answers a DIFFERENT question than the base classifier: not "does a
higher score mean more likely to merge" (ranking — already fixed once the base
model is trained) but "does a score of 0.8 actually mean an 80% chance of
merge" (probability quality). It cannot change which pairs rank above which —
if an acronym merge scores below a distinct pair's raw decision score, it will
score below it after calibration too. That is expected and is not this
module's job to fix; see canonical/scorer.py's module docstring and M5.5's
per-provenance evaluation for the real diagnosis of that finding.

Method: classic two-parameter Platt scaling — a 1-feature logistic regression
mapping the base classifier's raw decision-function (log-odds) to a calibrated
probability, fit on `calibration` labels only, then applied unchanged to
`calibration` (diagnostic) and `evaluation` (frozen later, by M5.5) rows. Never
touches `train` or refits the base classifier.

This SUPERSEDES the provisional (raw) probabilities scorer.run_train wrote to
model_predictions for the same model_version — see that module's docstring and
model_predictions' mutability note (migration 011). Refuses to run against a
FROZEN model_version (model_registry.metrics_summary already set) for the same
reason run_train does.
"""
from __future__ import annotations

import json

MODEL_VERSION_DEFAULT = "v1"


# --- pure: fitting + diagnostics (no DB; unit-tested directly) --------------

def fit_platt(scores: list[float], y: list[int], *, random_state: int = 0):
    """Fit sigmoid(a*score + b) to map raw decision-function scores to a
    calibrated probability. A plain 1-feature LogisticRegression IS Platt
    scaling; sklearn's default (mild L2) regularization is fine for a 2-
    parameter fit. Deterministic given fixed inputs (lbfgs, no randomness for
    this problem size; random_state set for future-proofing)."""
    import numpy as np
    from sklearn.linear_model import LogisticRegression

    x = np.asarray(scores, dtype=float).reshape(-1, 1)
    platt = LogisticRegression(random_state=random_state)
    platt.fit(x, y)
    return platt


def apply_platt(platt, scores: list[float]) -> list[float]:
    """Map raw decision-function scores through a FITTED Platt calibrator."""
    import numpy as np
    if not scores:
        return []
    x = np.asarray(scores, dtype=float).reshape(-1, 1)
    return platt.predict_proba(x)[:, 1].tolist()


def brier_score(probs: list[float], y: list[int]) -> float:
    """Mean squared error between predicted probability and the 0/1 outcome —
    lower is better; 0.25 is the score of an uninformative p=0.5 classifier on
    a balanced set. The single-number summary of calibration + discrimination
    combined."""
    if not y:
        return float("nan")
    return sum((p - yi) ** 2 for p, yi in zip(probs, y)) / len(y)


def reliability_bins(probs: list[float], y: list[int], *, n_bins: int = 10) -> list[dict]:
    """Equal-width bins over [0,1]: for each, the average PREDICTED probability
    vs. the EMPIRICAL merge rate among pairs landing in that bin. A
    well-calibrated model has avg_predicted ≈ empirical_rate in every
    populated bin. Bins with no rows report both as None (not 0 — an empty bin
    says nothing about calibration, it isn't evidence of miscalibration)."""
    bins: list[list[tuple[float, int]]] = [[] for _ in range(n_bins)]
    for p, yi in zip(probs, y):
        idx = min(int(p * n_bins), n_bins - 1)
        bins[idx].append((p, yi))
    out = []
    for i, b in enumerate(bins):
        lo, hi = i / n_bins, (i + 1) / n_bins
        if b:
            avg_predicted = sum(p for p, _ in b) / len(b)
            empirical_rate = sum(yi for _, yi in b) / len(b)
        else:
            avg_predicted = empirical_rate = None
        out.append({"lo": round(lo, 2), "hi": round(hi, 2), "n": len(b),
                     "avg_predicted": avg_predicted, "empirical_rate": empirical_rate})
    return out


# --- DB-touching orchestration -----------------------------------------------

async def run_calibrate(*, model_version: str = MODEL_VERSION_DEFAULT, persist_rows: bool = True) -> dict:
    """CLI entry: load the already-trained artifact for `model_version` (does
    NOT retrain the base classifier), fit Platt scaling on `calibration`, and
    supersede that version's stored probabilities for `calibration` +
    `evaluation` with the calibrated values."""
    import numpy as np
    import joblib
    from stevie_platform import db
    from stevie_platform.canonical.scorer import FEATURE_ORDER, load_labeled_dataset, to_row, transform

    p = await db.pool()
    async with p.connection() as conn:
        reg = await conn.execute(
            "select artifact_path, metrics_summary from model_registry where model_version = %s",
            (model_version,),
        )
        reg_row = await reg.fetchone()
        if reg_row is None:
            raise SystemExit(f"no model_registry row for {model_version!r} — run `cli train` first")
        if persist_rows and reg_row["metrics_summary"] is not None:
            raise SystemExit(
                f"model_version {model_version!r} is FROZEN (has a metrics_summary "
                f"from a completed evaluation) — calibrate a new model_version instead.")

        artifact = joblib.load(reg_row["artifact_path"])
        scaler, clf = artifact["scaler"], artifact["clf"]
        assert tuple(artifact["feature_order"]) == FEATURE_ORDER, \
            "artifact's feature order does not match the current scorer module"

        rows, _fallback_n = await load_labeled_dataset(conn)
        calib_rows = [r for r in rows if r["partition"] == "calibration" and r["label"] in ("merge", "distinct")]
        calib_related_n = sum(1 for r in rows if r["partition"] == "calibration" and r["label"] == "related")
        eval_rows = [r for r in rows if r["partition"] == "evaluation"]  # all labels — scored, not evaluated here

        def decision_scores(rows_: list[dict]) -> list[float]:
            if not rows_:
                return []
            x_raw = np.array([to_row(r["features"]) for r in rows_])
            x = transform(x_raw, scaler)
            return clf.decision_function(x).tolist()

        calib_scores = decision_scores(calib_rows)
        y_calib = [1 if r["label"] == "merge" else 0 for r in calib_rows]
        platt = fit_platt(calib_scores, y_calib)

        calib_calibrated = apply_platt(platt, calib_scores)
        eval_scores = decision_scores(eval_rows)
        eval_calibrated = apply_platt(platt, eval_scores)

        brier = brier_score(calib_calibrated, y_calib)
        reliability = reliability_bins(calib_calibrated, y_calib, n_bins=5)

        pred_rows = []
        for r, prob in list(zip(calib_rows, calib_calibrated)) + list(zip(eval_rows, eval_calibrated)):
            pred_rows.append((
                r["candidate_id"], r["left_key"], r["right_key"], model_version, r["feature_version"],
                round(prob, 6), "merge" if prob >= 0.5 else "distinct", json.dumps(r["features"]),
            ))

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
            artifact["platt"] = platt
            joblib.dump(artifact, reg_row["artifact_path"])
            calib_meta = {
                "method": "platt", "a": float(platt.coef_[0][0]), "b": float(platt.intercept_[0]),
                "brier_score": round(brier, 6), "n_calibration": len(calib_rows),
            }
            await conn.execute(
                "update model_registry set calibration = %s where model_version = %s",
                (json.dumps(calib_meta), model_version),
            )
            await conn.commit()

    summary = {
        "model_version": model_version,
        "calibration_n": len(calib_rows), "calibration_related_excluded": calib_related_n,
        "evaluation_n": len(eval_rows),
        "platt_a": float(platt.coef_[0][0]), "platt_b": float(platt.intercept_[0]),
        "brier_score": round(brier, 4), "reliability": reliability,
        "predictions_written": len(pred_rows) if persist_rows else 0,
        "persisted": persist_rows,
    }
    _print_report(summary)
    return summary


def _print_report(s: dict) -> None:
    print("\n" + "=" * 60)
    print(f" PROBABILITY CALIBRATION  —  model {s['model_version']}  (Platt scaling)")
    print("=" * 60)
    print(f"  calibration pairs   {s['calibration_n']:>6}   "
          f"({s['calibration_related_excluded']} 'related' excluded from the fit)")
    print(f"  evaluation pairs    {s['evaluation_n']:>6}   (scored, not evaluated — that's M5.5)")
    print("-" * 60)
    print(f"  platt: sigmoid({s['platt_a']:+.4f} * raw_score {s['platt_b']:+.4f})")
    print(f"  Brier score (on calibration — diagnostic, expect optimistic)   {s['brier_score']:.4f}")
    print("-" * 60)
    print("  reliability (calibration split):")
    print(f"    {'bin':<12}{'n':>6}{'avg predicted':>16}{'empirical rate':>16}")
    for b in s["reliability"]:
        avg = f"{b['avg_predicted']:.3f}" if b["avg_predicted"] is not None else "—"
        emp = f"{b['empirical_rate']:.3f}" if b["empirical_rate"] is not None else "—"
        print(f"    [{b['lo']:.1f},{b['hi']:.1f})".ljust(12) + f"{b['n']:>6}{avg:>16}{emp:>16}")
    print("-" * 60)
    print(f"  predictions superseded   {s['predictions_written']:>6}   (calibration + evaluation)")
    print("=" * 60 + "\n")
