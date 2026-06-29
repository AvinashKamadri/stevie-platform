"""
Data Quality Gates — the report's numbers turned into pass/fail assertions.
Run after canonicalize; exits non-zero on any failure so it can gate CI. If a
Stevie site change silently breaks parsing, a coverage gate trips instead of bad
data flowing downstream.

Thresholds are deliberately conservative; tighten once the full archive sets a
baseline.
"""
from __future__ import annotations

from stevie_platform import db

# (name, sql -> single numeric/bool, predicate, human threshold)
GATES = [
    ("no duplicate node_ids",
     "select count(*) - count(distinct node_id) from recognitions",
     lambda v: v == 0, "== 0"),
    ("no recognitions without an entrant",
     "select count(*) from recognitions where entrant_party_id is null",
     lambda v: v == 0, "== 0"),
    ("no orphan recognition_parties",
     "select count(*) from recognition_parties rp left join recognitions r on r.id=rp.recognition_id where r.id is null",
     lambda v: v == 0, "== 0"),
    ("program coverage > 99%",
     "select coalesce(100.0*count(*) filter (where program_edition_id is not null)/nullif(count(*),0),0) from recognitions",
     lambda v: v > 99, "> 99%"),
    ("organization coverage > 99%",
     "select coalesce(100.0*count(*) filter (where entrant_party_id is not null)/nullif(count(*),0),0) from recognitions",
     lambda v: v > 99, "> 99%"),
    ("country coverage > 98%",
     "select coalesce(100.0*count(*) filter (where country_id is not null)/nullif(count(*),0),0) from recognitions",
     lambda v: v > 98, "> 98%"),
    ("result_level known > 95%",
     "select coalesce(100.0*count(*) filter (where result_level <> 'other')/nullif(count(*),0),0) from recognitions",
     lambda v: v > 95, "> 95%"),
    ("parse completeness > 99%",
     "select coalesce(100.0*count(*) filter (where is_complete)/nullif(count(*),0),0) from parsed_records",
     lambda v: v > 99, "> 99%"),
    # --- M3 merge-graph gates -----------------------------------------------
    # Both pass vacuously (== 0) when organization_merge_decision is empty.
    ("no orphaned merge winners",
     """select count(*) from organization_merge_decision omd
        where decision = 'merge'
          and not exists (
              select 1 from organizations o where o.norm_key = omd.winner_key)""",
     lambda v: v == 0, "== 0"),
    ("alias covers all merge losers",
     """select count(*) from organization_merge_decision omd
        where decision = 'merge'
          and not exists (
              select 1 from organization_alias oa
               where oa.alias_norm_key = omd.loser_key)""",
     lambda v: v == 0, "== 0"),
]


async def run_gates() -> bool:
    """Print each gate's result; return True iff all pass."""
    p = await db.pool()
    all_ok = True
    print("\n" + "=" * 52)
    print(" DATA QUALITY GATES")
    print("=" * 52)
    async with p.connection() as conn:
        for name, sql, predicate, threshold in GATES:
            cur = await conn.execute(sql)
            val = list((await cur.fetchone()).values())[0]
            val = float(val) if val is not None else 0.0
            ok = predicate(val)
            all_ok = all_ok and ok
            shown = f"{val:.1f}%" if "%" in threshold else f"{val:.0f}"
            print(f"  [{'PASS' if ok else 'FAIL'}] {name:<38} {shown:>8} ({threshold})")
    print("=" * 52)
    print(("  ALL GATES PASS ✅" if all_ok else "  GATES FAILED ❌") + "\n")
    return all_ok
