"""
Fact / source confidence scoring (M7).

Attaches an explainable trust score in [0,1] — always WITH reasons — to each
canonical entity, then rolls up per recognition. Grounded only in signals that
are actually populated in `entity_links` (see experiments/M7_PLAN.md §0):
match_method is near-binary (exact/new) and match_score/model_version/reviewed_by
are empty, so the graduating signal is CORROBORATION (how many recognitions
reference the entity) plus entity TYPE (controlled vocabulary vs free-text).

Deterministic and explainable by design — no ML for v1. The score is a heuristic
prior on resolution trustworthiness, not a calibrated probability (no labeled
correctness signal exists yet; closing that gap is a future active-learning tie-in).
"""
from __future__ import annotations

import json

# Controlled-vocabulary dimensions: an exact match here is near-certain (small,
# curated value space). Free-text entities (org/person) depend on dedup quality,
# so corroboration carries more weight for them.
CONTROLLED_TYPES = frozenset({
    "country", "industry", "program", "program_edition",
    "category", "category_group", "category_definition",
})


# --- pure core (no DB; unit-tested directly) --------------------------------

def score_entity(entity_type: str, rec_count: int) -> tuple[float, list[str]]:
    """Deterministic confidence + reasons for one canonical entity.

    Base by type (controlled vocab scores higher), graduated by corroboration:
    a singleton (referenced once) is the low-confidence tail a grounding system
    should flag; a well-corroborated entity is high. Reasons mirror every term
    that fires, so the number is always explainable."""
    reasons: list[str] = []

    controlled = entity_type in CONTROLLED_TYPES
    if controlled:
        base = 0.85
        reasons.append(f"controlled-vocabulary dimension ({entity_type})")
    else:
        base = 0.70
        reasons.append(f"free-text entity ({entity_type}) — trust leans on corroboration")

    if rec_count >= 10:
        corr = 0.10
        reasons.append(f"well-corroborated: appears in {rec_count} recognitions")
    elif rec_count >= 3:
        corr = 0.05
        reasons.append(f"corroborated: appears in {rec_count} recognitions")
    elif rec_count == 2:
        corr = 0.0
        reasons.append("appears in 2 recognitions")
    else:
        corr = -0.30
        reasons.append("singleton — appears in only 1 recognition (unverified)")

    score = max(0.0, min(1.0, base + corr))
    return round(score, 4), reasons


def confidence_band(score: float) -> str:
    """Coarse band for reporting/surfacing."""
    if score >= 0.85:
        return "high"
    if score >= 0.65:
        return "medium"
    return "low"


# --- DB orchestration -------------------------------------------------------

async def run_confidence(*, batch_size: int = 5000) -> dict:
    """Recompute `fact_confidence` from entity_links corroboration counts.

    Truncate-and-rebuild (derived table). Corroboration = number of entity_links
    (≈ recognitions) referencing each (entity_type, entity_id)."""
    from stevie_platform import db

    p = await db.pool()
    async with p.connection() as conn:
        cur = await conn.execute(
            "select entity_type, entity_id, count(*)::int rec_count "
            "from entity_links group by entity_type, entity_id")
        entities = await cur.fetchall()

        rows = []
        for e in entities:
            score, reasons = score_entity(e["entity_type"], e["rec_count"])
            rows.append((e["entity_type"], e["entity_id"], score,
                         json.dumps(reasons), e["rec_count"]))

        await conn.execute("truncate fact_confidence")
        written = 0
        for i in range(0, len(rows), batch_size):
            batch = rows[i:i + batch_size]
            async with conn.cursor() as cur2:
                await cur2.executemany(
                    "insert into fact_confidence (entity_type, entity_id, score, reasons, rec_count) "
                    "values (%s,%s,%s,%s,%s)", batch)
            await conn.commit()
            written += len(batch)

    summary = {"entities_scored": written}
    print("\n" + "=" * 56)
    print(" FACT CONFIDENCE  -  computed")
    print("=" * 56)
    print(f"  entities scored   {written:>10,}")
    print("=" * 56 + "\n")
    return summary


async def run_confidence_report() -> dict:
    """Distributions + the low-confidence tail (M7.3 evaluation surface)."""
    from stevie_platform import db

    p = await db.pool()
    async with p.connection() as conn:
        async def q(sql):
            return [dict(r) for r in await (await conn.execute(sql)).fetchall()]

        total = (await q("select count(*) n from fact_confidence"))[0]["n"]
        if total == 0:
            raise SystemExit("fact_confidence is empty — run `stevie confidence` first.")

        by_band = await q(
            "select case when score>=0.85 then 'high' when score>=0.65 then 'medium' else 'low' end band, "
            "count(*) n from fact_confidence group by band order by band")
        by_type = await q(
            "select entity_type, round(avg(score),3) avg_score, count(*) n, "
            "count(*) filter (where score < 0.65) low_n from fact_confidence group by entity_type order by n desc")
        singletons = (await q("select count(*) n from fact_confidence where rec_count = 1"))[0]["n"]
        tail = await q(
            "select entity_type, entity_id, score, rec_count from fact_confidence "
            "order by score asc, rec_count asc limit 8")

    print("\n" + "=" * 60)
    print(" FACT CONFIDENCE  -  report")
    print("=" * 60)
    print(f"  entities            {total:>10,}")
    print(f"  singletons (rec=1)  {singletons:>10,}   (the low-confidence tail)")
    print("-" * 60)
    print("  by band:")
    for b in by_band:
        print(f"    {b['band']:<8} {b['n']:>10,}  ({100*b['n']/total:.1f}%)")
    print("-" * 60)
    print("  by entity_type:")
    print(f"    {'type':<22}{'avg':>7}{'n':>10}{'low':>10}")
    for t in by_type:
        print(f"    {t['entity_type']:<22}{float(t['avg_score']):>7.3f}{t['n']:>10,}{t['low_n']:>10,}")
    print("=" * 60 + "\n")
    return {"total": total, "by_band": by_band, "by_type": by_type, "singletons": singletons}
