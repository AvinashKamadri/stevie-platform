#!/usr/bin/env python3
"""
M2 interactive pair labeler — entity resolution gold dataset.

Commands:
  python label.py           -- label next unlabeled pairs (default)
  python label.py status    -- show labeling progress
  python label.py export    -- write labeled pairs to the corpus file

Corpus (--corpus, default 'sample'):
  sample      m2_gold_sample      -> gold/pairs.jsonl       (the original v1 set)
  supplement  m2_gold_supplement  -> gold/supplement.jsonl  (mined hard cases, v2)

  python label.py --corpus supplement          -- label hard cases (priority order)
  python label.py --corpus supplement export    -- write gold/supplement.jsonl

Two sampling goals, kept separate (supplement only):
  HARVEST gold_v2   priority order   (default labeling) -> positives/negatives/related
  ESTIMATE yield    uniform random   -> unbiased true-merge rate for a blocker decision

  python label.py --corpus supplement draw --random 80   -- tag 80 random acronyms
  python label.py --corpus supplement --tag random        -- label only that random set
  python label.py --corpus supplement yield               -- yield + 95% CI + projection

Reviewing in priority order ENRICHES the sample, so a rate read off it is biased
high. Estimate the population yield only from the uniform-random subset.
Pairs are drawn from the corpus table; the sample table is created by
m2_sample.sql, the supplement table by mine_hard_cases.py.

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
sys.path.insert(0, str(_ROOT / "src"))  # reuse the package's pure stats helpers

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

# Corpus config: which table to label and where to export it. The 'context'
# column is the per-pair tag shown during labeling (band for the sample, the
# mined pattern for the supplement).
CORPORA = {
    "sample": {
        "table": "m2_gold_sample", "out": GOLD_DIR / "pairs.jsonl",
        "context": "band", "missed": False, "allow_related": False,
        "hint": "m2_sample.sql", "gate": 500,
    },
    "supplement": {
        "table": "m2_gold_supplement", "out": GOLD_DIR / "supplement.jsonl",
        "context": "pattern", "missed": True, "allow_related": True,
        "samplable": True, "hint": "mine_hard_cases.py", "gate": 0,
    },
}

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


def _ensure_table(conn, cfg):
    exists = conn.execute(
        "select exists (select 1 from pg_tables "
        "where schemaname = 'public' and tablename = %s)",
        (cfg["table"],),
    ).fetchone()[0]
    if not exists:
        sys.exit(
            f"{cfg['table']} table not found.\n"
            f"  Create it first by running {cfg['hint']}."
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

def cmd_status(conn, cfg):
    _ensure_table(conn, cfg)
    row = conn.execute(f"""
        select
            count(*)                                    as total,
            count(*) filter (where label is not null)   as labeled,
            count(*) filter (where label = 'merge')     as merges,
            count(*) filter (where label = 'distinct')  as distincts,
            count(*) filter (where label is null)       as remaining
        from {cfg['table']}
    """).fetchone()
    total, labeled, merges, distincts, remaining = row
    pct = 100 * labeled // max(total, 1)
    gate_gap = max(0, cfg["gate"] - labeled)

    print()
    print(f"  total:     {total}")
    print(f"  labeled:   {labeled} / {total}  ({pct}%)")
    print(f"    merge:     {merges}")
    print(f"    distinct:  {distincts}")
    print(f"  remaining: {remaining}")
    if cfg["gate"]:
        if gate_gap == 0:
            print(f"\n  ✓  GATE CLEARED (>={cfg['gate']} labeled)")
        else:
            print(f"\n  gate: {gate_gap} more to clear the >={cfg['gate']} milestone")

    print()
    ctx = cfg["context"]
    order = ("case band when 'high' then 0 when 'border' then 1 else 2 end"
             if ctx == "band" else ctx)
    ctx_rows = conn.execute(f"""
        select {ctx} as grp,
               count(*) as total,
               count(*) filter (where label is not null) as labeled
        from {cfg['table']}
        group by grp
        order by {order}
    """).fetchall()
    print(f"  by {ctx}:")
    for grp, gtotal, glabeled in ctx_rows:
        print(f"    {str(grp):<22}  {glabeled}/{gtotal}")
    print()


def cmd_export(conn, cfg):
    _ensure_table(conn, cfg)
    GOLD_DIR.mkdir(exist_ok=True)
    out = cfg["out"]
    # The sample exports `band`; the supplement exports `pattern` +
    # `missed_by_blocking` so gold_v2 keeps the provenance of each hard case.
    if cfg["context"] == "band":
        cols = ["key_a", "key_b", "name_a", "name_b", "sim", "band",
                "rec_count_a", "rec_count_b", "countries_a", "countries_b",
                "label", "reason", "labeled_by", "labeled_at"]
        order = "case band when 'high' then 0 when 'border' then 1 else 2 end, sim desc"
    else:
        cols = ["key_a", "key_b", "name_a", "name_b", "sim", "pattern",
                "missed_by_blocking", "sample_tag", "rec_count_a", "rec_count_b",
                "countries_a", "countries_b",
                "label", "reason", "labeled_by", "labeled_at"]
        order = "pattern, sim desc"
    select = ", ".join("sim::float" if c == "sim" else c for c in cols)
    rows = conn.execute(
        f"select {select} from {cfg['table']} where label is not null order by {order}"
    ).fetchall()
    with out.open("w", encoding="utf-8") as fh:
        for row in rows:
            rec = dict(zip(cols, row))
            if rec["labeled_at"]:
                rec["labeled_at"] = rec["labeled_at"].isoformat()
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"  exported {len(rows)} labeled pairs → {out}")


def cmd_label(conn, cfg, tag=None):
    _ensure_table(conn, cfg)
    reviewer = getpass.getuser()
    table, ctx = cfg["table"], cfg["context"]
    # Optional restriction to a sample subset (e.g. --tag random) so the uniform
    # estimation set can be labeled as its own focused pass.
    tag_filter = f" and sample_tag = '{tag}'" if tag else ""
    scope = f" [{tag}]" if tag else ""

    # Count totals for progress display
    total = conn.execute(
        f"select count(*) from {table} where true{tag_filter}").fetchone()[0]
    done_at_start = conn.execute(
        f"select count(*) from {table} where label is not null{tag_filter}"
    ).fetchone()[0]
    if tag:
        print(f"  labeling {tag!r} subset: {total} pairs ({done_at_start} done)")

    # Ordering: the sample goes high band -> border -> low (easy decisions first);
    # the supplement goes missed-by-blocking first (the true hard positives) then
    # by pattern. Within a group, descending sim builds labeling rhythm.
    missed_sel = "missed_by_blocking" if cfg["missed"] else "null as missed_by_blocking"
    if ctx == "band":
        order = "case band when 'high' then 0 when 'border' then 1 else 2 end, sim desc"
    else:
        # Review PRIORITY order — see mine_hard_cases.py. Highest-priority hard
        # cases first so an early stop is prioritization, not corpus bias.
        order = "review_priority desc nulls last, missed_by_blocking desc, sim desc"
    pairs = conn.execute(f"""
        select key_a, key_b, name_a, name_b, sim::float, {ctx} as context,
               {missed_sel}, rec_count_a, rec_count_b, countries_a, countries_b
        from {table}
        where label is null{tag_filter}
        order by {order}
    """).fetchall()

    if not pairs:
        print("\n  All pairs labeled. Run `python label.py status` to check the gate.")
        return

    session_labeled = 0
    for i, row in enumerate(pairs):
        (key_a, key_b, name_a, name_b, sim, context, missed,
         rec_a, rec_b, ctry_a, ctry_b) = row
        n = done_at_start + i + 1

        width = 70
        tag = _fmt_band(context) if ctx == "band" else f"pattern: {context}"
        if missed:
            tag += "   ⚠ MISSED by current blocking"
        print(f"\n{'─' * width}")
        print(f"  pair {n}/{total}   {tag}   sim: {sim:.4f}")
        print(f"{'─' * width}")
        # Pad names to width-4 so the rec/country info stays aligned
        maxlen = width - 30
        na = name_a[:maxlen] + ("…" if len(name_a) > maxlen else "")
        nb = name_b[:maxlen] + ("…" if len(name_b) > maxlen else "")
        print(f"  A: {na!r:<{maxlen + 2}}  {rec_a} recs  {_fmt_countries(ctry_a)}")
        print(f"  B: {nb!r:<{maxlen + 2}}  {rec_b} recs  {_fmt_countries(ctry_b)}")
        print(f"{'─' * width}")
        rel = cfg["allow_related"]
        valid = ("m", "d", "s", "q") + (("r",) if rel else ())
        menu = ("  [m] merge   [d] distinct" + ("   [r] related" if rel else "")
                + "   [s] skip   [q] quit")
        print(menu)
        if rel:
            print("  ([r] = related but NOT same entity: parent/subsidiary, org/foundation)")

        # Read input
        while True:
            try:
                choice = input("  > ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print("\n  (interrupted — progress saved)")
                _print_session_summary(session_labeled)
                return
            if choice in valid:
                break
            print(f"  enter {', '.join(valid)}")

        if choice == "q":
            break
        if choice == "s":
            continue

        label = {"m": "merge", "d": "distinct", "r": "related"}[choice]

        try:
            reason = input("  reason (optional, Enter to skip): ").strip() or None
        except (EOFError, KeyboardInterrupt):
            reason = None

        conn.execute(
            f"""
            update {table}
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

        mark = {"merge": "✓ merge", "distinct": "✗ distinct", "related": "~ related"}[label]
        print(f"  {mark}")

    _print_session_summary(session_labeled)
    print("  Run `python label.py status` to check progress.")


def _print_session_summary(n: int):
    print(f"\n  session done — {n} pair(s) labeled this session.")


def cmd_draw(conn, cfg, *, n: int):
    """Tag the uniform random sample for yield estimation as the lowest-N rows by
    the stable `random_rank` assigned at staging (see mine_hard_cases.py). Because
    the rank is fixed and predates labeling, growing N is ADDITIVE (the first N
    stay put), reproducible, and independent of outcome — so 'label until the CI
    is narrow enough' needs no caveat about the population being regenerated."""
    if not cfg.get("samplable"):
        sys.exit(f"--corpus {cfg['table']} does not support sampling")
    _ensure_table(conn, cfg)
    pop = conn.execute(
        f"select count(*) from {cfg['table']} where pattern like '%%acronym%%'"
    ).fetchone()[0]
    # Tag ranks 1..N, untag the rest — idempotent; shrinks and grows cleanly.
    conn.execute(f"""
        update {cfg['table']} set sample_tag =
            case when random_rank is not null and random_rank <= %s then 'random' else null end
         where pattern like '%%acronym%%'
    """, (n,))
    conn.commit()
    tagged = conn.execute(
        f"select count(*) from {cfg['table']} where sample_tag = 'random'").fetchone()[0]
    print(f"  tagged {tagged} of {pop} acronym candidates as random (ranks 1..{n})")
    print(f"  label them:  python label.py --corpus supplement --tag random")


def cmd_yield(conn, cfg, archive: bool = False):
    """Estimate the acronym pattern's true-merge yield from the labeled random
    sample, with a Wilson 95% CI, and project it onto the full candidate pool.
    This is the statistically meaningful basis for an acronym-blocker decision.
    --archive writes a dated feasibility-study record (stats auto-filled, decision
    left for the human) so the rationale survives as a data-based artifact."""
    if not cfg.get("samplable"):
        sys.exit(f"--corpus {cfg['table']} does not support yield estimation")
    _ensure_table(conn, cfg)
    from stevie_platform.canonical.recall import wilson_interval
    pop = conn.execute(
        f"select count(*) from {cfg['table']} where pattern like '%%acronym%%'"
    ).fetchone()[0]
    n, merges, related, distinct = conn.execute(f"""
        select count(*) filter (where label is not null),
               count(*) filter (where label = 'merge'),
               count(*) filter (where label = 'related'),
               count(*) filter (where label = 'distinct')
        from {cfg['table']} where sample_tag = 'random'
    """).fetchone()
    print("\n" + "=" * 60)
    print(" ACRONYM BLOCKER YIELD  (uniform random sample)")
    print("=" * 60)
    print(f"  population (acronym candidates)   {pop:>8}")
    print(f"  random sample labeled             {n:>8}")
    print(f"    merge / related / distinct      {merges} / {related} / {distinct}")
    if not n:
        print("  no labeled random sample yet — run `draw --random N` then `--tag random`.")
        print("=" * 60 + "\n"); return
    rate = merges / n
    lo, hi = wilson_interval(merges, n)
    print(f"  true-merge yield                  {rate*100:>7.1f}%   "
          f"95% CI [{lo*100:.1f}%, {hi*100:.1f}%]  (Wilson)")
    print(f"  projected merges in population    {round(rate*pop):>8}   "
          f"95% CI [{round(lo*pop)}, {round(hi*pop)}]")
    print("-" * 60)
    print(f"  blocker gold/emit (point est.)    {rate:>8.4f}   "
          f"(ESTIMATED — vs trigram/rare_token OBSERVED)")
    print("=" * 60 + "\n")
    if archive:
        _archive_feasibility(pop, n, merges, related, distinct, rate, lo, hi)


def _archive_feasibility(pop, n, merges, related, distinct, rate, lo, hi):
    """Write a dated acronym-blocker feasibility record. Stats are auto-filled
    verbatim from the estimator; Decision/Rationale are left for the human so the
    'why (not) an acronym blocker' answer is documented from data, not memory."""
    day = datetime.now(timezone.utc).date().isoformat()
    out = GOLD_DIR.parent / f"acronym_feasibility_{day}.md"
    out.write_text(f"""# Acronym blocker feasibility study — {day}

Auto-generated by `label.py --corpus supplement yield --archive`. Stats are
verbatim from the uniform random sample; fill in the decision below.

```
Population (acronym candidates):  {pop}
Random sample labeled:            {n}
  merge / related / distinct:     {merges} / {related} / {distinct}
Estimated true-merge yield:       {rate*100:.1f}%   95% CI [{lo*100:.1f}%, {hi*100:.1f}%]  (Wilson)
Projected merges in population:   {round(rate*pop)}   95% CI [{round(lo*pop)}, {round(hi*pop)}]
Blocker candidate volume (emit):  {pop}
```

Decision: <continue sampling | build blocker | reject blocker>
Decision rationale: <judge the CI LOWER bound ({round(lo*pop)}) against the
  engineering/operational complexity of another blocker — not the point estimate>
""", encoding="utf-8")
    print(f"  archived feasibility study -> {out}\n")


# ── entry point ───────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="entity-resolution gold pair labeler")
    parser.add_argument("cmd", nargs="?", default="label",
                        choices=["label", "status", "export", "draw", "yield"])
    parser.add_argument("--corpus", default="sample", choices=list(CORPORA),
                        help="which corpus table to label (default: sample)")
    parser.add_argument("--tag", default=None, choices=["random"],
                        help="label only this sample subset (supplement only)")
    parser.add_argument("--random", type=int, default=None, metavar="N",
                        help="draw command: size of the uniform random sample (ranks 1..N)")
    parser.add_argument("--archive", action="store_true",
                        help="yield command: also write a dated feasibility-study record")
    args = parser.parse_args()
    cfg = CORPORA[args.corpus]
    with _connect() as conn:
        if args.cmd == "status":
            cmd_status(conn, cfg)
        elif args.cmd == "export":
            cmd_export(conn, cfg)
        elif args.cmd == "draw":
            if args.random is None:
                sys.exit("draw requires --random N")
            cmd_draw(conn, cfg, n=args.random)
        elif args.cmd == "yield":
            cmd_yield(conn, cfg, archive=args.archive)
        else:
            cmd_label(conn, cfg, tag=args.tag)


if __name__ == "__main__":
    main()
