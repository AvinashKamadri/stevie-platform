"""
Production scoring (Phase 3/4) — apply an already-trained, CALIBRATED model to
candidate pairs outside the gold benchmark. This is what feeds the human-review
queue, and its default mode ("score only what isn't already scored") is
exactly Phase 4's incremental-scoring requirement: a new scrape's candidates
get scored without touching or re-doing existing predictions.

Never retrains or recalibrates — loads the artifact exactly as `cli calibrate`
left it. Scoring a FROZEN model_version is expected and unrestricted; freezing
only protects a version's recorded evaluation from being invalidated by a
later retrain/recalibrate (canonical/scorer.py, canonical/calibration.py) — it
says nothing about using that finished model in production.
"""
from __future__ import annotations

import json


async def run_score(*, model_version: str, rescore: bool = False, batch_size: int = 5000) -> dict:
    """CLI entry: score candidates with `model_version`.

    rescore=False (default): only candidates with NO existing prediction for
    this model_version (the incremental case). rescore=True: recompute for
    every candidate whose feature_version matches the model's (e.g. after
    fixing a bug in the artifact, though that should really be a new
    model_version — this exists for deliberate re-scoring, not casual use)."""
    import numpy as np
    import joblib
    from stevie_platform import db
    from stevie_platform.canonical.scorer import to_row, transform

    p = await db.pool()
    async with p.connection() as conn:
        reg = await conn.execute(
            "select artifact_path, feature_version from model_registry where model_version = %s",
            (model_version,),
        )
        reg_row = await reg.fetchone()
        if reg_row is None:
            raise SystemExit(f"no model_registry row for {model_version!r} — run `cli train` first")

        artifact = joblib.load(reg_row["artifact_path"])
        scaler, clf, platt = artifact["scaler"], artifact["clf"], artifact.get("platt")
        if platt is None:
            raise SystemExit(
                f"model_version {model_version!r} has not been calibrated — run `cli calibrate` first "
                f"(scoring with an uncalibrated model would produce raw decision scores, not probabilities).")

        skipped_version = (await (await conn.execute(
            "select count(*) n from organization_merge_candidate where feature_version is distinct from %s",
            (reg_row["feature_version"],))).fetchone())["n"]

        if rescore:
            cur = await conn.execute(
                "select id, left_key, right_key, features from organization_merge_candidate "
                "where feature_version = %s",
                (reg_row["feature_version"],),
            )
        else:
            cur = await conn.execute(
                """select omc.id, omc.left_key, omc.right_key, omc.features
                     from organization_merge_candidate omc
                    where omc.feature_version = %s
                      and not exists (
                          select 1 from model_predictions mp
                           where mp.left_key = omc.left_key and mp.right_key = omc.right_key
                             and mp.model_version = %s)""",
                (reg_row["feature_version"], model_version),
            )
        rows = await cur.fetchall()

        scored = 0
        for i in range(0, len(rows), batch_size):
            batch = rows[i:i + batch_size]
            x_raw = np.array([to_row(r["features"]) for r in batch])
            x = transform(x_raw, scaler)
            raw_scores = clf.decision_function(x)
            proba = platt.predict_proba(raw_scores.reshape(-1, 1))[:, 1]
            pred_rows = [
                (r["id"], r["left_key"], r["right_key"], model_version, reg_row["feature_version"],
                 round(float(prob), 6), "merge" if prob >= 0.5 else "distinct", json.dumps(r["features"]))
                for r, prob in zip(batch, proba)
            ]
            async with conn.cursor() as cur2:
                await cur2.executemany(
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
            await conn.commit()
            scored += len(batch)
            print(f"[score] {scored}/{len(rows)}")

    summary = {
        "model_version": model_version, "rescore": rescore,
        "candidates_scored": scored, "skipped_feature_version_mismatch": skipped_version,
    }
    print("\n" + "=" * 52)
    print(" PRODUCTION SCORING")
    print("=" * 52)
    print(f"  model_version          {model_version:>10}")
    print(f"  mode                   {'full rescore' if rescore else 'incremental':>10}")
    print(f"  candidates scored      {scored:>10,}")
    if skipped_version:
        print(f"  skipped (stale features) {skipped_version:>8,}   <- run `cli features` first")
    print("=" * 52 + "\n")
    return summary
