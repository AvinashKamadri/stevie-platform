"""
Frozen scorer evaluation (M5.5) — the ONE measurement of a model_version
against the `evaluation` partition, which nothing has touched until now.

This mirrors canonical/recall.py's relationship to blocking: recall.py answers
"did blocking surface the pair"; this module answers "given that it was
surfaced, did the scorer classify it correctly" — and, like recall.py, its
most useful output is not the headline number but the FAILURE LIST (here:
false negatives — confirmed merges the scorer scored below 0.5).

`related` stays a REPORTED class only (see split.py; canonical/scorer.py never
trains on it): counted, never scored against precision/recall, per the
measured starvation (0 in calibration, 4 in evaluation — not enough for any
statistic beyond a count).

Precision/recall/F1 use the CALIBRATED probability's implied predicted_label
(>= 0.5) already stored in model_predictions by calibration.run_calibrate.
Per-blocker-provenance breakdown is the more actionable table: an aggregate
recall number can look fine while one blocker's entire contribution is being
misclassified (see the M5.3 finding on the acronym blocker) — this table is
built specifically to make that visible rather than averaged away.

Calling run_evaluate WRITES model_registry.metrics_summary exactly once — that
write is what FREEZES the model_version (scorer.run_train and
calibration.run_calibrate both then refuse to touch it). Re-running evaluate
after freezing recomputes and prints the same report but does not rewrite the
frozen record.
"""
from __future__ import annotations

import json

from stevie_platform.canonical.calibration import brier_score, reliability_bins

MODEL_VERSION_DEFAULT = "v1"
PROVENANCE_BLOCKERS = ("trigram", "rare_token", "acronym")


# --- pure: metric math (no DB; unit-tested directly) ------------------------

def confusion_counts(rows: list[dict]) -> dict:
    """rows: [{'label': 'merge'|'distinct', 'predicted_label': 'merge'|'distinct'}].
    Caller excludes 'related' rows — this only knows about the binary target."""
    tp = sum(1 for r in rows if r["label"] == "merge" and r["predicted_label"] == "merge")
    fp = sum(1 for r in rows if r["label"] == "distinct" and r["predicted_label"] == "merge")
    fn = sum(1 for r in rows if r["label"] == "merge" and r["predicted_label"] == "distinct")
    tn = sum(1 for r in rows if r["label"] == "distinct" and r["predicted_label"] == "distinct")
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn}


def precision_recall_f1(counts: dict) -> dict:
    tp, fp, fn, tn = counts["tp"], counts["fp"], counts["fn"], counts["tn"]
    precision = tp / (tp + fp) if (tp + fp) else float("nan")
    recall = tp / (tp + fn) if (tp + fn) else float("nan")
    f1 = (2 * precision * recall / (precision + recall)
          if precision == precision and recall == recall and (precision + recall) else float("nan"))
    return {"precision": precision, "recall": recall, "f1": f1,
            "support_merge": tp + fn, "support_distinct": fp + tn}


def provenance_breakdown(rows: list[dict], blockers: tuple[str, ...] = PROVENANCE_BLOCKERS) -> dict:
    """Per-blocker precision/recall/support. A row counts under EVERY blocker
    that surfaced it (overlapping, not exclusive) — mirrors recall.py's
    found_by_blocker convention, so a pair caught by both trigram and
    rare_token contributes to both rows, not an arbitrary "primary" one."""
    out = {}
    for b in blockers:
        sub = [r for r in rows if b in r["reasons"]]
        counts = confusion_counts(sub)
        out[b] = {**precision_recall_f1(counts), "n": len(sub), **counts}
    return out


def false_negatives(rows: list[dict]) -> list[dict]:
    """Confirmed merges the scorer scored below 0.5 — the work queue this
    report exists to produce, mirroring recall.py's blocking_failures.jsonl."""
    return [r for r in rows if r["label"] == "merge" and r["predicted_label"] == "distinct"]


def _nan_to_none(obj):
    """Recursively replace float('nan') with None. Python's json.dumps emits
    the literal (invalid-JSON) token NaN by default, which Postgres's jsonb
    parser rejects outright — undefined precision/recall (0 denominator) must
    round-trip as JSON null, not crash the write."""
    if isinstance(obj, float) and obj != obj:
        return None
    if isinstance(obj, dict):
        return {k: _nan_to_none(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_nan_to_none(v) for v in obj]
    return obj


def related_summary(related_rows: list[dict]) -> dict:
    """Counts only — n is too small (4 in the current evaluation split) for any
    rate or interval to mean anything."""
    n = len(related_rows)
    pred_merge = sum(1 for r in related_rows if r["predicted_label"] == "merge")
    return {"n": n, "predicted_merge": pred_merge, "predicted_distinct": n - pred_merge}


# --- DB-touching orchestration -----------------------------------------------

async def run_evaluate(*, model_version: str = MODEL_VERSION_DEFAULT, corpus: str = "v2") -> dict:
    from stevie_platform import db
    from stevie_platform.canonical.candidates import order_pair
    from stevie_platform.canonical.recall import GOLD_DIR, load_corpus
    from stevie_platform.canonical.split import assign_split

    p = await db.pool()
    async with p.connection() as conn:
        reg = await conn.execute(
            "select feature_version, split_version, metrics_summary "
            "from model_registry where model_version = %s",
            (model_version,),
        )
        reg_row = await reg.fetchone()
        if reg_row is None:
            raise SystemExit(
                f"no model_registry row for {model_version!r} — run `cli train` and `cli calibrate` first")
        already_frozen = reg_row["metrics_summary"] is not None

        cur = await conn.execute(
            """select mp.left_key, mp.right_key, coalesce(omc.reasons, '{}') reasons,
                      mp.probability, mp.predicted_label
                 from model_predictions mp
                 left join organization_merge_candidate omc
                   on omc.left_key = mp.left_key and omc.right_key = mp.right_key
                where mp.model_version = %s
                order by mp.left_key, mp.right_key""",
            (model_version,),
        )
        pred_rows = await cur.fetchall()

    gold, _version, _missing = load_corpus(corpus)
    gold_by_pair = {}
    for g in gold:
        lk, _, rk, _ = order_pair(g["key_a"], 0, g["key_b"], 0)
        gold_by_pair[(lk, rk)] = g["label"]

    eval_rows = []
    for r in pred_rows:
        key = (r["left_key"], r["right_key"])
        label = gold_by_pair.get(key)
        # every prediction currently comes from a labeled gold pair (see
        # scorer.load_labeled_dataset); a missing label would mean the gold
        # corpus changed after predictions were made — skip rather than crash,
        # since the frozen report speaks only to what's evaluable today.
        if label is None:
            continue
        # model_predictions holds BOTH calibration and evaluation rows (see
        # calibration.py); only `evaluation` counts for the frozen report.
        if assign_split(r["left_key"], r["right_key"]) != "evaluation":
            continue
        eval_rows.append({
            "key_a": r["left_key"], "key_b": r["right_key"], "label": label,
            "predicted_label": r["predicted_label"], "probability": float(r["probability"]),
            "reasons": list(r["reasons"]),
        })

    binary_rows = [r for r in eval_rows if r["label"] in ("merge", "distinct")]
    related_rows = [r for r in eval_rows if r["label"] == "related"]

    counts = confusion_counts(binary_rows)
    overall = precision_recall_f1(counts)
    provenance = provenance_breakdown(binary_rows)
    fn_rows = false_negatives(binary_rows)
    rel_summary = related_summary(related_rows)

    probs = [r["probability"] for r in binary_rows]
    ys = [1 if r["label"] == "merge" else 0 for r in binary_rows]
    brier = brier_score(probs, ys)
    reliability = reliability_bins(probs, ys, n_bins=5)

    summary = {
        "model_version": model_version, "evaluation_n": len(binary_rows),
        "confusion": counts, "overall": overall, "provenance": provenance,
        "brier_score": round(brier, 4) if brier == brier else None, "reliability": reliability,
        "related": rel_summary, "false_negative_count": len(fn_rows),
    }
    summary = _nan_to_none(summary)

    fn_path = GOLD_DIR / f"scorer_false_negatives_{model_version}.jsonl"
    with fn_path.open("w", encoding="utf-8") as f:
        for r in fn_rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    if not already_frozen:
        p2 = await db.pool()
        async with p2.connection() as conn2:
            await conn2.execute(
                "update model_registry set metrics_summary = %s where model_version = %s",
                (json.dumps(summary), model_version),
            )
            await conn2.commit()

    _print_report(summary, fn_rows, fn_path, already_frozen)
    return summary


def _print_report(s: dict, fn_rows: list[dict], fn_path, already_frozen: bool) -> None:
    print("\n" + "=" * 66)
    label = "ALREADY FROZEN — recomputed for inspection" if already_frozen else "FREEZING NOW"
    print(f" FROZEN EVALUATION  —  model {s['model_version']}   [{label}]")
    print("=" * 66)
    print(f"  evaluation pairs (merge/distinct)   {s['evaluation_n']:>6}")
    c = s["confusion"]
    print(f"  confusion:  tp={c['tp']}  fp={c['fp']}  fn={c['fn']}  tn={c['tn']}")
    def fmt(v):
        return f"{v:.3f}" if v is not None else "—"

    o = s["overall"]
    print(f"  overall     precision={fmt(o['precision'])}  recall={fmt(o['recall'])}  "
          f"f1={fmt(o['f1'])}  (support: merge={o['support_merge']}, distinct={o['support_distinct']})")
    print("-" * 66)
    print("  by blocker provenance (a pair counts under every blocker that found it):")
    print(f"    {'blocker':<14}{'recall':>9}{'precision':>11}{'n':>6}")
    for b, m in s["provenance"].items():
        print(f"    {b:<14}{fmt(m['recall']):>9}{fmt(m['precision']):>11}{m['n']:>6}")
    print("-" * 66)
    br = s["brier_score"]
    print(f"  Brier score (evaluation, the real test)   {br:.4f}" if br is not None else "  Brier score: n/a")
    print("  reliability:")
    print(f"    {'bin':<12}{'n':>6}{'avg predicted':>16}{'empirical rate':>16}")
    for b in s["reliability"]:
        avg = f"{b['avg_predicted']:.3f}" if b["avg_predicted"] is not None else "—"
        emp = f"{b['empirical_rate']:.3f}" if b["empirical_rate"] is not None else "—"
        print(f"    [{b['lo']:.1f},{b['hi']:.1f})".ljust(12) + f"{b['n']:>6}{avg:>16}{emp:>16}")
    print("-" * 66)
    r = s["related"]
    print(f"  related (n={r['n']}, COUNT ONLY — never scored against merge/distinct):  "
          f"predicted merge={r['predicted_merge']}  predicted distinct={r['predicted_distinct']}")
    print("-" * 66)
    print(f"  false negatives (confirmed merges scored < 0.5): {len(fn_rows)}")
    for r in fn_rows[:15]:
        print(f"    {r['key_a']!r:<32} {r['key_b']!r:<36} p={r['probability']:.3f}  reasons={r['reasons']}")
    if len(fn_rows) > 15:
        print(f"    ... and {len(fn_rows) - 15} more (full list -> {fn_path})")
    elif fn_rows:
        print(f"    (full list -> {fn_path})")
    print("=" * 66 + "\n")
