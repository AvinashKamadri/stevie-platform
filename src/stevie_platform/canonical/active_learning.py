"""
Active-learning candidate selection (M6, Slice 1) — build the ranked review
queue that grows the training corpus where it will teach the model the most.

The M5 record is explicit: scorer recall (0.645) is capped by *data*, not by
model tuning. The highest-information labels are the pairs the current model is
LEAST sure about — those nearest the 0.5 decision boundary. Labeling there
sharpens the boundary; labeling pairs the model already scores 0.99 or 0.01
teaches it almost nothing.

This module ranks unlabeled candidates by uncertainty and emits a deterministic
queue for the human-review lane. Three exclusions are non-negotiable:

  1. Frozen-benchmark pairs — never surface them. They are already labeled and
     must stay in the evaluation set (see canonical/benchmark.py). Excluding them
     here is the FIRST line of defense; benchmark.assert_no_contamination is the
     backstop at train time.
  2. Already-labeled gold pairs — no point relabeling.
  3. Already-decided pairs (organization_merge_decision) — a human already ruled.

Determinism: ranking is a total order — primary key |p - 0.5| ascending, ties
broken by (left_key, right_key). Same predictions in → same queue out, on any
machine, so an active-learning round is reproducible and auditable.
"""
from __future__ import annotations

import json

from stevie_platform.canonical.split import pair_fraction


# --- pure core (no DB; unit-tested directly) --------------------------------

def uncertainty(probability: float) -> float:
    """Distance from the decision boundary, negated-for-nothing: smaller means
    MORE uncertain. |p - 0.5| == 0 is maximal uncertainty (p == 0.5)."""
    return abs(probability - 0.5)


def rank_by_uncertainty(scored: list[dict], *,
                        exclude: frozenset[tuple[str, str]] = frozenset()) -> list[dict]:
    """Deterministic uncertainty ranking, most-uncertain first.

    `scored`: dicts with at least `left_key`, `right_key`, `probability`. Pairs
    in `exclude` (ordered tuples) are dropped. Total order: (|p-0.5|, left_key,
    right_key) — so equal-uncertainty pairs resolve stably by key, never by
    input order or dict/hash iteration."""
    kept = [s for s in scored if (s["left_key"], s["right_key"]) not in exclude]
    return sorted(kept, key=lambda s: (uncertainty(float(s["probability"])),
                                       s["left_key"], s["right_key"]))


def select_queue(scored: list[dict], *, limit: int,
                 exclude: frozenset[tuple[str, str]] = frozenset(),
                 random_fraction: float = 0.0) -> list[dict]:
    """Top-`limit` review queue. `random_fraction` in [0,1) reserves that share
    of slots for a deterministic random sample (pair_fraction as a stable PRNG),
    mixed in to counter the sampling bias of pure uncertainty selection — a
    known active-learning failure mode where the training set drifts toward one
    region of feature space. Each selected pair is tagged with its `strategy`
    ('uncertainty' | 'random') for later label-efficiency analysis."""
    ranked = rank_by_uncertainty(scored, exclude=exclude)
    if limit <= 0 or not ranked:
        return []

    n_random = int(limit * random_fraction)
    n_uncertain = limit - n_random

    picked: list[dict] = []
    picked_keys: set[tuple[str, str]] = set()
    for s in ranked[:n_uncertain]:
        picked.append({**s, "strategy": "uncertainty"})
        picked_keys.add((s["left_key"], s["right_key"]))

    if n_random > 0:
        # Deterministic pseudo-random over the *remaining* pool: order by the
        # pair's stable hash fraction. Reproducible (no RNG state) yet unrelated
        # to uncertainty, so it samples the space the uncertainty head ignores.
        remaining = [s for s in ranked[n_uncertain:]
                     if (s["left_key"], s["right_key"]) not in picked_keys]
        remaining.sort(key=lambda s: pair_fraction(s["left_key"], s["right_key"]))
        for s in remaining[:n_random]:
            picked.append({**s, "strategy": "random"})

    return picked


# --- DB-touching orchestration ----------------------------------------------

async def _excluded_pairs(conn) -> frozenset[tuple[str, str]]:
    """Every pair that must NOT enter the review queue: frozen-benchmark pairs,
    already-labeled gold pairs, and pairs a human has already decided."""
    from stevie_platform.canonical.benchmark import frozen_pair_set
    from stevie_platform.canonical.candidates import order_pair
    from stevie_platform.canonical.recall import load_corpus

    excluded = set(frozen_pair_set())

    # Already-labeled gold (any component of the widest corpus we know, v2).
    gold, _v, _m = load_corpus("v2")
    for g in gold:
        lk, _, rk, _ = order_pair(g["key_a"], 0, g["key_b"], 0)
        excluded.add((lk, rk))

    # Already-decided pairs. Decisions are keyed by loser_key (unique), with the
    # winner recorded alongside; reconstruct the ordered pair from both.
    cur = await conn.execute(
        "select winner_key, loser_key from organization_merge_decision")
    for r in await cur.fetchall():
        lk, _, rk, _ = order_pair(r["winner_key"], 0, r["loser_key"], 0)
        excluded.add((lk, rk))

    return frozenset(excluded)


async def run_sample(*, model_version: str, limit: int = 100,
                     random_fraction: float = 0.0, out_path: str | None = None) -> dict:
    """CLI entry: emit the next active-learning review queue for `model_version`.

    Reads model_predictions, excludes benchmark/labeled/decided pairs, ranks by
    uncertainty (with an optional random-mix), and writes a queue JSONL the
    review lane consumes. Writes nothing to the DB — selection is a read-only,
    reproducible projection over existing predictions."""
    from stevie_platform import db
    from stevie_platform.canonical.recall import GOLD_DIR

    p = await db.pool()
    async with p.connection() as conn:
        excluded = await _excluded_pairs(conn)
        cur = await conn.execute(
            """select mp.left_key, mp.right_key, mp.probability,
                      coalesce(omc.reasons, '{}') reasons
                 from model_predictions mp
                 left join organization_merge_candidate omc
                   on omc.left_key = mp.left_key and omc.right_key = mp.right_key
                where mp.model_version = %s
                order by mp.left_key, mp.right_key""",
            (model_version,),
        )
        scored = [{"left_key": r["left_key"], "right_key": r["right_key"],
                   "probability": float(r["probability"]), "reasons": list(r["reasons"])}
                  for r in await cur.fetchall()]

    queue = select_queue(scored, limit=limit, exclude=excluded,
                         random_fraction=random_fraction)

    out = GOLD_DIR / (out_path or f"active_queue_{model_version}.jsonl")
    with out.open("w", encoding="utf-8") as f:
        for r in queue:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    from collections import Counter
    strat = dict(Counter(r["strategy"] for r in queue))
    summary = {
        "model_version": model_version, "scored_pool": len(scored),
        "excluded": len(excluded), "queue_size": len(queue),
        "by_strategy": strat, "out_path": str(out),
    }
    print("\n" + "=" * 60)
    print(f" ACTIVE-LEARNING QUEUE  -  model {model_version}")
    print("=" * 60)
    print(f"  scored predictions      {summary['scored_pool']:>8,}")
    print(f"  excluded (bench/labeled/decided) {summary['excluded']:>8,}")
    print(f"  queue size              {summary['queue_size']:>8,}   {strat}")
    if queue:
        print("-" * 60)
        print("  most uncertain (top 10):")
        for r in queue[:10]:
            print(f"    p={r['probability']:.3f}  {r['left_key']!r:<30} {r['right_key']!r:<32} [{r['strategy']}]")
    print(f"  queue -> {out}")
    print("=" * 60 + "\n")
    return summary
