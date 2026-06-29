#!/usr/bin/env python3
"""
M2 interactive pair labeler — entity resolution gold dataset.

Commands:
  python label.py           -- label next unlabeled pairs (default)
  python label.py status    -- show labeling progress
  python label.py export    -- write labeled pairs to gold/pairs.jsonl

Pairs are drawn from `m2_gold_sample` (created by m2_sample.sql).
Run `python label.py status` to see the gate: need >= 500 labeled pairs.

Keys during labeling:
  m  — merge   (these are the same brand / org)
  d  — distinct (genuinely different entities)
  s  — skip this pair (come back later)
  q  — quit session (progress is saved)
"""
from __future__ import annotations

import getpass
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# ── env setup ─────────────────────────────────────────────────────────────────
_HERE = Path(__file__).parent
_ROOT = _HERE.parent.parent  # stevie-platform root

# Support running from any directory
for _env in (_ROOT / ".env", Path(".env")):
    if _env.exists():
        try:
            from dotenv import load_dotenv
            load_dotenv(_env)
        except ImportError:
            pass
        break

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://stevie:stevie@localhost:5432/stevie_platform",
)
GOLD_DIR = _HERE / "gold"
PAIRS_FILE = GOLD_DIR / "pairs.jsonl"

# ── helpers ───────────────────────────────────────────────────────────────────

def _connect():
    try:
        import psycopg
    except ImportError:
        sys.exit("psycopg not installed — run: pip install 'psycopg[binary]'")
    try:
        return psycopg.connect(DATABASE_URL)
    except Exception as exc:
        sys.exit(f"DB connection failed: {exc}\n  DATABASE_URL={DATABASE_URL}")


def _ensure_table(conn):
    exists = conn.execute("""
        select exists (
            select 1 from pg_tables
            where schemaname = 'public' and tablename = 'm2_gold_sample'
        )
    """).fetchone()[0]
    if not exists:
        sys.exit(
            "m2_gold_sample table not found.\n"
            "  Create it first:\n"
            "    docker exec stevie-pg psql -U stevie -d stevie_platform -f - "
            "< experiments/entity_resolution/m2_sample.sql"
        )


def _fmt_countries(arr) -> str:
    if not arr:
        return "—"
    shown = arr[:4]
    suffix = f" +{len(arr) - 4}" if len(arr) > 4 else ""
    return ", ".join(shown) + suffix


def _fmt_band(band: str) -> str:
    return {"high": "high  (sim ≥ 0.70)", "border": "border (0.55–0.70)", "low": "low   (0.40–0.55)"}[band]


# ── commands ──────────────────────────────────────────────────────────────────

def cmd_status(conn):
    _ensure_table(conn)
    row = conn.execute("""
        select
            count(*)                                    as total,
            count(*) filter (where label is not null)   as labeled,
            count(*) filter (where label = 'merge')     as merges,
            count(*) filter (where label = 'distinct')  as distincts,
            count(*) filter (where label is null)       as remaining
        from m2_gold_sample
    """).fetchone()
    total, labeled, merges, distincts, remaining = row
    pct = 100 * labeled // max(total, 1)
    gate_gap = max(0, 500 - labeled)

    print()
    print(f"  total:     {total}")
    print(f"  labeled:   {labeled} / {total}  ({pct}%)")
    print(f"    merge:     {merges}")
    print(f"    distinct:  {distincts}")
    print(f"  remaining: {remaining}")
    if gate_gap == 0:
        print()
        print("  ✓  GATE CLEARED (>=500 labeled)")
    else:
        print()
        print(f"  gate: {gate_gap} more to clear the >=500 milestone")

    print()
    band_rows = conn.execute("""
        select band,
               count(*) as total,
               count(*) filter (where label is not null) as labeled
        from m2_gold_sample
        group by band
        order by case band when 'high' then 0 when 'border' then 1 else 2 end
    """).fetchall()
    print("  by band:")
    for band, btotal, blabeled in band_rows:
        print(f"    {band:<6}  {blabeled}/{btotal}")
    print()


def cmd_export(conn):
    _ensure_table(conn)
    GOLD_DIR.mkdir(exist_ok=True)
    rows = conn.execute("""
        select key_a, key_b, name_a, name_b, sim::float,
               band, rec_count_a, rec_count_b, countries_a, countries_b,
               label, reason, labeled_by, labeled_at
        from m2_gold_sample
        where label is not null
        order by case band when 'high' then 0 when 'border' then 1 else 2 end, sim desc
    """).fetchall()
    cols = [
        "key_a", "key_b", "name_a", "name_b", "sim", "band",
        "rec_count_a", "rec_count_b", "countries_a", "countries_b",
        "label", "reason", "labeled_by", "labeled_at",
    ]
    with PAIRS_FILE.open("w", encoding="utf-8") as fh:
        for row in rows:
            rec = dict(zip(cols, row))
            if rec["labeled_at"]:
                rec["labeled_at"] = rec["labeled_at"].isoformat()
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"  exported {len(rows)} labeled pairs → {PAIRS_FILE}")


def cmd_label(conn):
    _ensure_table(conn)
    reviewer = getpass.getuser()

    # Count totals for progress display
    total = conn.execute("select count(*) from m2_gold_sample").fetchone()[0]
    done_at_start = conn.execute(
        "select count(*) from m2_gold_sample where label is not null"
    ).fetchone()[0]

    # Fetch unlabeled pairs: high band first (most likely true merges, easier decisions),
    # then border (threshold calibration), then low (precision floor). Within each band
    # order by descending sim — most-obvious pairs first to build labeling rhythm.
    pairs = conn.execute("""
        select key_a, key_b, name_a, name_b, sim::float, band,
               rec_count_a, rec_count_b, countries_a, countries_b
        from m2_gold_sample
        where label is null
        order by
            case band when 'high' then 0 when 'border' then 1 else 2 end,
            sim desc
    """).fetchall()

    if not pairs:
        print("\n  All pairs labeled. Run `python label.py status` to check the gate.")
        return

    session_labeled = 0
    for i, row in enumerate(pairs):
        key_a, key_b, name_a, name_b, sim, band, rec_a, rec_b, ctry_a, ctry_b = row
        n = done_at_start + i + 1

        width = 70
        print(f"\n{'─' * width}")
        print(f"  pair {n}/{total}   band: {_fmt_band(band)}   sim: {sim:.4f}")
        print(f"{'─' * width}")
        # Pad names to width-4 so the rec/country info stays aligned
        maxlen = width - 30
        na = name_a[:maxlen] + ("…" if len(name_a) > maxlen else "")
        nb = name_b[:maxlen] + ("…" if len(name_b) > maxlen else "")
        print(f"  A: {na!r:<{maxlen + 2}}  {rec_a} recs  {_fmt_countries(ctry_a)}")
        print(f"  B: {nb!r:<{maxlen + 2}}  {rec_b} recs  {_fmt_countries(ctry_b)}")
        print(f"{'─' * width}")
        print("  [m] merge   [d] distinct   [s] skip   [q] quit")

        # Read input
        while True:
            try:
                choice = input("  > ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print("\n  (interrupted — progress saved)")
                _print_session_summary(session_labeled)
                return
            if choice in ("m", "d", "s", "q"):
                break
            print("  enter m, d, s, or q")

        if choice == "q":
            break
        if choice == "s":
            continue

        label = "merge" if choice == "m" else "distinct"

        try:
            reason = input("  reason (optional, Enter to skip): ").strip() or None
        except (EOFError, KeyboardInterrupt):
            reason = None

        conn.execute(
            """
            update m2_gold_sample
               set label      = %s,
                   reason     = %s,
                   labeled_by = %s,
                   labeled_at = %s
             where key_a = %s and key_b = %s
            """,
            (label, reason, reviewer, datetime.now(timezone.utc), key_a, key_b),
        )
        conn.commit()
        session_labeled += 1

        mark = "✓ merge" if label == "merge" else "✗ distinct"
        print(f"  {mark}")

    _print_session_summary(session_labeled)
    print("  Run `python label.py status` to check progress toward the 500-pair gate.")


def _print_session_summary(n: int):
    print(f"\n  session done — {n} pair(s) labeled this session.")


# ── entry point ───────────────────────────────────────────────────────────────

def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "label"
    with _connect() as conn:
        if cmd == "status":
            cmd_status(conn)
        elif cmd == "export":
            cmd_export(conn)
        elif cmd == "label":
            cmd_label(conn)
        else:
            print(f"unknown command: {cmd!r}")
            print(__doc__)
            sys.exit(1)


if __name__ == "__main__":
    main()
