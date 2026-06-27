"""
Canonicalization metrics — "where is the work?" Per-dimension created/exact/
candidate counts from the run, plus a read-only POSSIBLE-DUPLICATE diagnostic
(trgm self-join per dimension). The diagnostic only COUNTS likely dupes; it does
not merge or store candidates — entity resolution stays unbuilt until the full
archive lands.
"""
from __future__ import annotations

from stevie_platform import db

# Dimensions worth a near-dup scan. Countries are a controlled list (skip).
_DUP_TABLES = [
    ("organizations", 0.55),
    ("programs", 0.45),
    ("category_definitions", 0.60),
    ("industries", 0.55),
    ("people", 0.60),
]


async def possible_duplicate_counts(conn, threshold_floor: float = 0.45) -> dict:
    """For each dimension, count pairs whose names are trgm-similar above the
    per-table floor. Pure diagnostic — surfaces e.g. the 'The Asia-Pacific…' vs
    'Asia-Pacific…' program split as a number, without acting on it."""
    out: dict[str, int] = {}
    for table, floor in _DUP_TABLES:
        cur = await conn.execute(
            f"select count(*) n from {table} a join {table} b "
            f"on a.id < b.id and a.name %% b.name and similarity(a.name, b.name) >= %s",
            (floor,),
        )
        out[table] = (await cur.fetchone())["n"]
    return out


async def print_canonicalization_metrics(summary: dict) -> None:
    dims = ["country", "industry", "program", "edition", "category", "organization"]
    print("\n" + "=" * 52)
    print(" CANONICALIZATION METRICS")
    print("=" * 52)
    print(f"  normalized        : {summary.get('normalized', 0):>8,}")
    print(f"  recognitions built: {summary.get('recognitions_built', 0):>8,}")
    print(f"  missing req fields: {summary.get('missing_required', 0):>8,}")
    print(f"  failed            : {summary.get('failed', 0):>8,}")
    print(f"  {'dimension':<14}{'created':>10}{'exact':>10}{'candidates':>12}")
    print("  " + "-" * 46)
    for dim in dims:
        created = summary.get(f"{dim}:created", 0)
        exact = summary.get(f"{dim}:exact", 0)
        cands = summary.get(f"{dim}:candidates", 0)
        print(f"  {dim:<14}{created:>10,}{exact:>10,}{cands:>12,}")

    p = await db.pool()
    async with p.connection() as conn:
        dups = await possible_duplicate_counts(conn)
    print("\n  possible duplicates (diagnostic — not merged):")
    for table, n in dups.items():
        flag = "  <-- review at Phase D" if n else ""
        print(f"    {table:<22}{n:>6,}{flag}")
    print("=" * 52 + "\n")
