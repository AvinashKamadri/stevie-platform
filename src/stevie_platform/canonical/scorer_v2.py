"""
v2 training pipeline (M6, Slice 2) — active-learning scorer.

Trains on the expanded corpus (v3 = M5 gold + active-learning rounds) and
evaluates on the FROZEN 112-pair benchmark, so an M5-vs-v2 comparison varies
exactly one thing: the training labels. Per the M6 design (M6_SLICE2_DESIGN.md):

  - M5 is immutable. This module imports the pure math from scorer.py /
    calibration.py / scorer_eval.py; it never edits them, and never touches
    v1.2's artifact or registry row.
  - The 112 benchmark pairs are subtracted by set membership (never re-hashed)
    and are EVALUATION ONLY. Everything else is split train/calibration/
    validation by split_v2.
  - The contamination guard runs immediately after dataset assembly, BEFORE the
    fit — a leaked benchmark pair is a hard failure, not a silent bias.
  - Model family is held fixed (logistic regression, FEATURE_VERSION v3 —
    identical to v1.2). Only the data changes.

One orchestrator (`run_fit_v2`) does train -> calibrate -> evaluate in sequence
and freezes the version at the end (metrics_summary write), mirroring M5's
"evaluate freezes once" discipline. Run with persist=False while iterating.
"""
from __future__ import annotations

import json
from collections import Counter

# Pure math reused from the (immutable) M5 modules — no reimplementation.
from stevie_platform.canonical.calibration import (
    apply_platt, brier_score, fit_platt, reliability_bins,
)
from stevie_platform.canonical.scorer import (
    FEATURE_ORDER, coefficient_table, fit_model, to_row, transform,
)
from stevie_platform.canonical.scorer_eval import (
    confusion_counts, false_negatives, precision_recall_f1,
    provenance_breakdown, related_summary, _nan_to_none,
)

MODEL_VERSION_DEFAULT = "v2"
SOURCE_CORPUS = "v3"
ALGORITHM = "logistic_regression"  # held fixed vs v1.2 — only the data changes


# --- dataset assembly (the v2 fork) -----------------------------------------

async def load_labeled_dataset_v2(conn, *, corpus: str = SOURCE_CORPUS):
    """Join every labeled pair in `corpus` to its candidate features, tagged
    with (a) its v2 partition and (b) its provenance.

    Partition rule: a pair in the FROZEN benchmark is 'evaluation' (by set
    membership, not by hash); every other pair is bucketed train/calibration/
    validation by split_v2. Provenance: legacy gold rows default to
    source='manual', review_round=0; active-learning rows carry their own."""
    from stevie_platform.canonical.benchmark import frozen_pair_set
    from stevie_platform.canonical.candidates import order_pair
    from stevie_platform.canonical.features import (
        FEATURE_VERSION, compute_rare_tokens, extract_features,
    )
    from stevie_platform.canonical.recall import load_corpus
    from stevie_platform.canonical.split_v2 import assign_split_v2

    frozen = frozen_pair_set()
    gold, _version, _missing = load_corpus(corpus)
    cur = await conn.execute(
        "select id, left_key, right_key, features, feature_version, "
        "coalesce(reasons, '{}') reasons from organization_merge_candidate"
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
            feats, fver, cand_id, reasons = hit["features"], hit["feature_version"], hit["id"], list(hit["reasons"])
        else:
            if rare_tokens is None:
                rare_tokens = await compute_rare_tokens(conn)
            feats, fver, cand_id, reasons = extract_features(ka, kb, (), rare_tokens=rare_tokens), FEATURE_VERSION, None, []
            fallback_n += 1
        partition = "evaluation" if (lk, rk) in frozen else assign_split_v2(ka, kb)
        rows.append({
            "key_a": ka, "key_b": kb, "left_key": lk, "right_key": rk, "label": g["label"],
            "features": feats, "feature_version": fver, "candidate_id": cand_id, "reasons": reasons,
            "partition": partition,
            "source": g.get("source", "manual"), "review_round": g.get("review_round", 0),
        })
    return rows, fallback_n


def _class_counts_v2(rows: list[dict]) -> dict:
    out: dict[str, dict[str, int]] = {}
    for partition in ("train", "calibration", "validation", "evaluation"):
        c = Counter(r["label"] for r in rows if r["partition"] == partition)
        out[partition] = {"merge": c.get("merge", 0), "distinct": c.get("distinct", 0),
                           "related": c.get("related", 0)}
    return out


# --- artifact JSON mirrors (git-diffable A/B) -------------------------------

def export_registry_json(reg_row: dict, *, model_version: str) -> tuple:
    """Write per-version metrics + calibration JSON mirrors of the registry row.
    model_registry stays the durable truth; these files make an A/B a git diff
    and travel outside the DB. Never overwrites another version's files."""
    from stevie_platform.config import BASE_DIR
    metrics_dir = BASE_DIR / "artifacts" / "metrics"
    calib_dir = BASE_DIR / "artifacts" / "calibration"
    metrics_dir.mkdir(parents=True, exist_ok=True)
    calib_dir.mkdir(parents=True, exist_ok=True)
    metrics_path = metrics_dir / f"{model_version}.json"
    calib_path = calib_dir / f"{model_version}.json"
    if reg_row.get("metrics_summary") is not None:
        metrics_path.write_text(json.dumps(reg_row["metrics_summary"], indent=2) + "\n", encoding="utf-8")
    if reg_row.get("calibration") is not None:
        calib_path.write_text(json.dumps(reg_row["calibration"], indent=2) + "\n", encoding="utf-8")
    return metrics_path, calib_path


# --- orchestration ----------------------------------------------------------

async def run_fit_v2(*, model_version: str = MODEL_VERSION_DEFAULT,
                     corpus: str = SOURCE_CORPUS, persist: bool = True) -> dict:
    """Train -> calibrate -> evaluate v2 on the frozen benchmark, then freeze.

    Guard fires immediately after dataset assembly. Fits logistic regression on
    the v2 `train` split, Platt-scales on `calibration`, evaluates on the 112
    frozen benchmark pairs, and prints an A/B against v1.2."""
    import numpy as np
    import joblib
    from stevie_platform import db
    from stevie_platform.canonical.benchmark import assert_no_contamination, frozen_pair_set
    from stevie_platform.canonical.features import FEATURE_VERSION
    from stevie_platform.canonical.split_v2 import SPLIT_V2_VERSION
    from stevie_platform.config import BASE_DIR

    p = await db.pool()
    async with p.connection() as conn:
        if persist:
            existing = await (await conn.execute(
                "select metrics_summary from model_registry where model_version = %s",
                (model_version,))).fetchone()
            if existing and existing["metrics_summary"] is not None:
                raise SystemExit(
                    f"model_version {model_version!r} is FROZEN (has metrics_summary) — "
                    f"train a NEW model_version instead of overwriting it.")

        rows, fallback_n = await load_labeled_dataset_v2(conn, corpus=corpus)
        if not rows:
            raise SystemExit(f"corpus {corpus!r} produced no labeled rows.")

        feature_versions = {r["feature_version"] for r in rows}
        if feature_versions != {FEATURE_VERSION}:
            raise SystemExit(
                f"mixed/unexpected feature_version(s): {feature_versions}; expected "
                f"{{{FEATURE_VERSION!r}}}. Run `stevie features` first.")

        # --- GUARD EARLY: before any fit, prove the training pool is disjoint
        # from the frozen benchmark. ---------------------------------------
        pool_pairs = [(r["left_key"], r["right_key"]) for r in rows if r["partition"] != "evaluation"]
        assert_no_contamination(pool_pairs, frozen=frozen_pair_set())

        train_rows = [r for r in rows if r["partition"] == "train" and r["label"] in ("merge", "distinct")]
        calib_rows = [r for r in rows if r["partition"] == "calibration" and r["label"] in ("merge", "distinct")]
        val_rows = [r for r in rows if r["partition"] == "validation" and r["label"] in ("merge", "distinct")]
        eval_rows = [r for r in rows if r["partition"] == "evaluation"]
        if not train_rows:
            raise SystemExit("no train rows after benchmark subtraction — nothing to fit.")

        # --- fit (logistic regression, fixed feature set) ---
        x_train = np.array([to_row(r["features"]) for r in train_rows])
        y_train = [1 if r["label"] == "merge" else 0 for r in train_rows]
        scaler, clf = fit_model(x_train, y_train)
        coeffs = coefficient_table(clf)

        def decision_scores(rs):
            if not rs:
                return []
            return clf.decision_function(transform(np.array([to_row(r["features"]) for r in rs]), scaler)).tolist()

        # --- calibrate (Platt on the calibration split) ---
        calib_scores = decision_scores(calib_rows)
        y_calib = [1 if r["label"] == "merge" else 0 for r in calib_rows]
        if not calib_rows:
            raise SystemExit("no calibration rows — cannot Platt-scale.")
        platt = fit_platt(calib_scores, y_calib)
        calib_brier = brier_score(apply_platt(platt, calib_scores), y_calib)

        # --- evaluate on the FROZEN benchmark (calibrated probabilities) ---
        def calibrated(rs):
            return apply_platt(platt, decision_scores(rs))

        eval_binary = [r for r in eval_rows if r["label"] in ("merge", "distinct")]
        eval_related = [r for r in eval_rows if r["label"] == "related"]
        eval_probs = calibrated(eval_binary)
        eval_scored = [{
            "key_a": r["left_key"], "key_b": r["right_key"], "label": r["label"],
            "probability": float(prob), "predicted_label": "merge" if prob >= 0.5 else "distinct",
            "reasons": r["reasons"],
        } for r, prob in zip(eval_binary, eval_probs)]
        rel_probs = calibrated(eval_related)
        rel_scored = [{"predicted_label": "merge" if prob >= 0.5 else "distinct"} for prob in rel_probs]

        counts = confusion_counts(eval_scored)
        overall = precision_recall_f1(counts)
        provenance = provenance_breakdown(eval_scored)
        fn_rows = false_negatives(eval_scored)
        probs = [r["probability"] for r in eval_scored]
        ys = [1 if r["label"] == "merge" else 0 for r in eval_scored]
        brier = brier_score(probs, ys)
        reliability = reliability_bins(probs, ys, n_bins=5)

        # validation snapshot (reporting only — never freezes anything)
        val_scored = [{"label": r["label"], "predicted_label": "merge" if prob >= 0.5 else "distinct"}
                      for r, prob in zip(val_rows, calibrated(val_rows))]
        val_metrics = precision_recall_f1(confusion_counts(val_scored)) if val_scored else None

        train_provenance = dict(Counter(f"{r['source']}#r{r['review_round']}" for r in train_rows))
        class_counts = _class_counts_v2(rows)
        metrics_summary = _nan_to_none({
            "model_version": model_version, "evaluation_n": len(eval_scored),
            "confusion": counts, "overall": overall, "provenance": provenance,
            "brier_score": round(brier, 4) if brier == brier else None, "reliability": reliability,
            "related": related_summary(rel_scored), "false_negative_count": len(fn_rows),
            "benchmark_version": "v1", "corpus": corpus, "train_provenance": train_provenance,
            "validation": _nan_to_none(val_metrics),
        })
        calib_meta = {"method": "platt", "a": float(platt.coef_[0][0]), "b": float(platt.intercept_[0]),
                      "brier_score": round(calib_brier, 6), "n_calibration": len(calib_rows)}

        artifact_path = BASE_DIR / "artifacts" / "models" / f"{model_version}.joblib"
        ab = None
        if persist:
            artifact_path.parent.mkdir(parents=True, exist_ok=True)
            joblib.dump({"scaler": scaler, "clf": clf, "platt": platt, "feature_order": FEATURE_ORDER}, artifact_path)
            await conn.execute(
                """insert into model_registry
                     (model_version, feature_version, split_version, algorithm,
                      training_sample_size, class_counts, artifact_path, coefficients,
                      calibration, metrics_summary)
                   values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   on conflict (model_version) do update set
                     feature_version=excluded.feature_version, split_version=excluded.split_version,
                     algorithm=excluded.algorithm, training_sample_size=excluded.training_sample_size,
                     class_counts=excluded.class_counts, artifact_path=excluded.artifact_path,
                     coefficients=excluded.coefficients, calibration=excluded.calibration,
                     metrics_summary=excluded.metrics_summary, training_timestamp=now()""",
                (model_version, FEATURE_VERSION, SPLIT_V2_VERSION, ALGORITHM,
                 len(train_rows), json.dumps(class_counts), str(artifact_path), json.dumps(coeffs),
                 json.dumps(calib_meta), json.dumps(metrics_summary)))
            await conn.commit()
            export_registry_json({"metrics_summary": metrics_summary, "calibration": calib_meta},
                                 model_version=model_version)
            # Back-fill v1.2's JSON mirrors (read-only w.r.t. M5) for the A/B.
            v12 = await (await conn.execute(
                "select metrics_summary, calibration from model_registry where model_version = 'v1.2'")).fetchone()
            if v12:
                export_registry_json(v12, model_version="v1.2")
                ab = v12["metrics_summary"]
        else:
            v12 = await (await conn.execute(
                "select metrics_summary from model_registry where model_version = 'v1.2'")).fetchone()
            ab = v12["metrics_summary"] if v12 else None

    summary = {
        "model_version": model_version, "corpus": corpus, "persisted": persist,
        "train_n": len(train_rows), "calibration_n": len(calib_rows),
        "validation_n": len(val_rows), "evaluation_n": len(eval_scored),
        "fallback_n": fallback_n, "overall": overall, "validation": val_metrics,
        "coefficients": coeffs, "metrics_summary": metrics_summary,
        "train_provenance": train_provenance, "artifact_path": str(artifact_path),
    }
    _print_report(summary, ab, fn_rows)
    return summary


def _print_report(s: dict, ab, fn_rows) -> None:
    def fmt(v):
        return f"{v:.3f}" if isinstance(v, (int, float)) and v == v else "-"
    o = s["overall"]
    print("\n" + "=" * 66)
    print(f" V2 FIT  -  model {s['model_version']}  (logistic_regression, corpus {s['corpus']})"
          + ("" if s["persisted"] else "   [DRY RUN — not persisted]"))
    print("=" * 66)
    print(f"  splits    train={s['train_n']}  calibration={s['calibration_n']}  "
          f"validation={s['validation_n']}  evaluation(frozen)={s['evaluation_n']}")
    print(f"  train provenance   {s['train_provenance']}")
    if s["fallback_n"]:
        print(f"  ! {s['fallback_n']} pairs had no candidate row (features computed fresh)")
    print("-" * 66)
    print("  A/B on the frozen 112-pair benchmark (identical pairs):")
    print(f"    {'model':<10}{'recall':>9}{'precision':>11}{'f1':>9}")
    if ab and ab.get("overall"):
        ao = ab["overall"]
        print(f"    {'v1.2 (M5)':<10}{fmt(ao['recall']):>9}{fmt(ao['precision']):>11}{fmt(ao['f1']):>9}")
    print(f"    {s['model_version'] + ' (M6)':<10}{fmt(o['recall']):>9}{fmt(o['precision']):>11}{fmt(o['f1']):>9}")
    if ab and ab.get("overall"):
        dr = o["recall"] - ab["overall"]["recall"]
        dp = o["precision"] - ab["overall"]["precision"]
        print(f"    {'delta':<10}{dr:>+9.3f}{dp:>+11.3f}")
    print("-" * 66)
    if s["validation"]:
        v = s["validation"]
        print(f"  validation (selection only)  recall={fmt(v['recall'])} precision={fmt(v['precision'])}")
    print(f"  false negatives on benchmark: {len(fn_rows)}")
    for r in fn_rows[:10]:
        print(f"    {r['key_a']!r:<30} {r['key_b']!r:<32} p={r['probability']:.3f}")
    print("=" * 66 + "\n")
