#!/usr/bin/env python3
"""
Mine HARD candidate pairs for a gold supplement (gold_v2).

The current gold set is saturated: it was sampled from the trigram-discoverable
space (m2_sample.sql), so every blocker scores ~100% recall on it and it can no
longer tell us whether blocking is actually complete. To make `blocking_gap` a
meaningful signal again we need hard positives that live OUTSIDE that
distribution — acronym/expansion, abbreviation, reordered names, &/and.

These CANNOT be hand-authored: a pair only tests blocking if BOTH sides exist as
distinct rows in `organizations` (otherwise the harness drops it into
missing_org). So we mine real instances of each pattern from the live org table.

For each mined pair we check membership in the persisted
organization_merge_candidate table — if current blocking ALREADY surfaces it, it
is not a hard case. The pairs blocking MISSES are the valuable supplement
candidates. We write them out (label=null) for human labeling; we do NOT assign
labels — that judgment is the reviewer's, or the benchmark is circular.

    python mine_hard_cases.py            # mine, report, write jsonl + stage table
    python mine_hard_cases.py --no-load  # mine + report + jsonl only (no DB write)

Output: gold/hard_candidates.jsonl (inspection) AND a staged DB table
`m2_gold_supplement` (mirrors m2_gold_sample, enriched with sim + recognition
context) that `label.py --corpus supplement` labels. label=null throughout —
labels are the reviewer's, or the benchmark is circular.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_HERE = Path(__file__).parent
_ROOT = _HERE.parent.parent
OUT = _HERE / "gold" / "hard_candidates.jsonl"

for _env in (_ROOT / ".env", Path(".env")):
    if _env.exists():
        try:
            from dotenv import load_dotenv
            load_dotenv(_env)
        except ImportError:
            pass
        break

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://stevie:stevie@127.0.0.1:5432/stevie_platform")

# Stopwords ignored when comparing token SETS (reorder detection) and when
# forming acronyms — "Foundation for the Arts" reorders to "Arts Foundation",
# and "Association for Computing Machinery" acronyms to ACM.
STOPWORDS = {"for", "the", "of", "and", "a", "an", "to", "in", "at", "on", "by"}

# Seed for the stable random ranking of acronym candidates (see _stage_table).
# Fixed so the yield sample is reproducible across re-stagings of the same orgs.
RANDOM_SEED = 0.42

# Abbreviation -> possible expansions. Ambiguous abbrevs list several; each is
# tested. Precise + controllable, unlike a generic prefix heuristic.
ABBREV = {
    "assoc": ["association"], "asso": ["association"],
    "univ": ["university"], "dept": ["department"],
    "intl": ["international"], "natl": ["national"],
    "comm": ["communications", "committee", "commission"],
    "mfg": ["manufacturing"], "tech": ["technology", "technologies"],
    "svcs": ["services"], "svc": ["service"], "corp": ["corporation"],
    "inst": ["institute"], "mgmt": ["management"], "dev": ["development"],
    "fdn": ["foundation"], "found": ["foundation"], "sys": ["systems"],
    "natnl": ["national"], "soc": ["society"], "grp": ["group"],
}


def _connect():
    try:
        import psycopg
    except ImportError:
        sys.exit("psycopg not installed — run: pip install 'psycopg[binary]'")
    return psycopg.connect(DATABASE_URL)


def _content_sig(toks: list[str]) -> tuple[str, ...]:
    return tuple(sorted(t for t in toks if t not in STOPWORDS))


def _initials(toks: list[str], *, skip_stop: bool) -> str:
    src = [t for t in toks if not (skip_stop and t in STOPWORDS)]
    return "".join(t[0] for t in src if t)


def _order(ka: str, kb: str) -> tuple[str, str]:
    """Code-point ordering — matches organization_merge_candidate's collate "C"."""
    return (ka, kb) if ka <= kb else (kb, ka)


def mine(orgs: list[dict]) -> dict[tuple[str, str], dict]:
    """Return {ordered_pair: {pattern, ...}} of mined hard candidates.

    orgs: rows of {id, norm_key, name}. Pure (no DB) so it is unit-testable."""
    by_key = {o["norm_key"]: o for o in orgs}
    toks = {o["norm_key"]: o["norm_key"].split() for o in orgs}
    found: dict[tuple[str, str], set[str]] = {}

    def add(ka: str, kb: str, pattern: str) -> None:
        if ka == kb:
            return
        found.setdefault(_order(ka, kb), set()).add(pattern)

    # 1. Reordered names: identical CONTENT token set, different key.
    sigs: dict[tuple[str, ...], list[str]] = {}
    for k, t in toks.items():
        content = [x for x in t if x not in STOPWORDS]
        if len(content) >= 2:
            sigs.setdefault(_content_sig(t), []).append(k)
    for keys in sigs.values():
        if len(keys) > 1:
            for i in range(len(keys)):
                for j in range(i + 1, len(keys)):
                    add(keys[i], keys[j], "reordered")

    # 2. Acronym <-> expansion: a short alpha key equals another key's initials.
    initials_idx: dict[str, list[str]] = {}
    for k, t in toks.items():
        if len(t) >= 2:
            for ini in {_initials(t, skip_stop=False), _initials(t, skip_stop=True)}:
                if 2 <= len(ini) <= 7:
                    initials_idx.setdefault(ini, []).append(k)
    for k, t in toks.items():
        if len(t) == 1 and k.isalpha() and 2 <= len(k) <= 7:
            for expansion in initials_idx.get(k, []):
                add(k, expansion, "acronym")

    # 3. Abbreviation <-> full: expand a known abbrev token; does the result exist?
    for k, t in toks.items():
        for i, tok in enumerate(t):
            for full in ABBREV.get(tok, []):
                cand = " ".join(t[:i] + [full] + t[i + 1:])
                if cand in by_key:
                    add(k, cand, "abbreviation")

    # 4. &/and (and dropped joiner): keys equal after removing 'and' tokens.
    no_and: dict[str, list[str]] = {}
    for k, t in toks.items():
        stripped = " ".join(x for x in t if x != "and")
        if stripped != k:                      # only keys that actually contain 'and'
            no_and.setdefault(stripped, []).append(k)
        else:
            no_and.setdefault(stripped, []).append(k)
    for stripped, keys in no_and.items():
        uniq = sorted(set(keys))
        if len(uniq) > 1:
            for i in range(len(uniq)):
                for j in range(i + 1, len(uniq)):
                    add(uniq[i], uniq[j], "and_variant")

    return {pair: {"patterns": sorted(p)} for pair, p in found.items()}


def _stage_table(conn, rows: list[dict]) -> None:
    """Create + populate m2_gold_supplement, enriched (sim + recognition context)
    exactly like m2_sample.sql does for m2_gold_sample, so label.py labels it with
    the same UI. label columns stay null. Rebuilt fresh on each run."""
    conn.execute("drop table if exists m2_gold_supplement")
    conn.execute("""
        create table m2_gold_supplement (
            key_a text collate "C" not null,
            key_b text collate "C" not null,
            name_a text, name_b text,
            sim numeric, pattern text,
            missed_by_blocking boolean,
            rec_count_a int, rec_count_b int,
            countries_a text[], countries_b text[],
            industries_a text[], industries_b text[],
            review_priority numeric,
            -- Stable uniform-random ordering of the acronym population, assigned
            -- ONCE at staging (see below). `draw --random N` tags ranks 1..N, so
            -- the yield sample grows additively and reproducibly, and selection
            -- is independent of labels (ranks predate labeling) — unbiased.
            random_rank int,
            -- 'random' marks rows drawn into the UNIFORM sample used to estimate
            -- the pattern's true-merge yield (label.py draw --random N). null =
            -- harvested via the priority queue. Selection method, not a label.
            sample_tag text,
            label text, reason text, labeled_by text, labeled_at timestamptz,
            primary key (key_a, key_b),
            -- 'related' = related but NOT the same entity (parent/subsidiary,
            -- org/foundation) — a relationship-graph seed, distinct from a merge.
            constraint m2_gold_supplement_label check (label in ('merge','distinct','related')))
    """)
    with conn.cursor() as cur:
        cur.executemany(
            "insert into m2_gold_supplement "
            "(key_a, key_b, name_a, name_b, pattern, missed_by_blocking) "
            "values (%s,%s,%s,%s,%s,%s) on conflict do nothing",
            [(r["key_a"], r["key_b"], r["name_a"], r["name_b"],
              r["pattern"], r["missed_by_blocking"]) for r in rows])
    # Enrich sim + per-side recognition context (countries + industries), joining
    # orgs back by norm_key.
    ctx = """(select count(distinct rp.recognition_id)::int rec_count,
                     array_agg(distinct c.name order by c.name)
                         filter (where c.name is not null) countries,
                     array_agg(distinct ind.name order by ind.name)
                         filter (where ind.name is not null) industries
                from parties p
                join recognition_parties rp on rp.party_id = p.id
                join recognitions r on r.id = rp.recognition_id
                left join countries c on c.id = r.country_id
                left join industries ind on ind.id = r.industry_id
               where p.organization_id = %s.id)"""
    conn.execute(f"""
        update m2_gold_supplement g set
            sim = round(similarity(g.name_a, g.name_b)::numeric, 4),
            rec_count_a = ca.rec_count, countries_a = ca.countries, industries_a = ca.industries,
            rec_count_b = cb.rec_count, countries_b = cb.countries, industries_b = cb.industries
        from organizations oa
        join lateral {ctx % 'oa'} ca on true,
             organizations ob
        join lateral {ctx % 'ob'} cb on true
        where oa.norm_key = g.key_a and ob.norm_key = g.key_b
    """)
    # Review PRIORITY (order, not eligibility — every candidate stays in the
    # corpus; reviewers just see the most promising first, so stopping early
    # prioritizes rather than biases). Formula, all additive:
    #   acronym_strength  length of the acronym side (longer = less coincidental;
    #                     'BP' is weak, 'CBRE' strong) — only for acronym pattern
    #   geo_bonus    +3 if the two orgs share >=1 country
    #   industry_bonus +3 if they share >=1 industry
    #   reach_bonus  0..2 nudge by the busier side's recognition count
    conn.execute("""
        update m2_gold_supplement set review_priority =
              (case when pattern like '%%acronym%%'
                    then least(char_length(key_a), char_length(key_b)) - 1 else 0 end)
            + (case when (countries_a && countries_b) is true then 3 else 0 end)
            + (case when (industries_a && industries_b) is true then 3 else 0 end)
            + least(greatest(coalesce(rec_count_a,0), coalesce(rec_count_b,0)), 20) / 10.0
    """)
    # Stable random rank over the acronym population (seeded → reproducible;
    # assigned now, before any labeling → independent of outcome). `draw` tags
    # ranks 1..N, making the yield sample additive and unbiased.
    conn.execute("select setseed(%s)", (RANDOM_SEED,))
    conn.execute("""
        with ranked as (
            select key_a, key_b, row_number() over (order by random()) rn
              from m2_gold_supplement where pattern like '%%acronym%%')
        update m2_gold_supplement g set random_rank = ranked.rn
          from ranked where g.key_a = ranked.key_a and g.key_b = ranked.key_b
    """)
    conn.commit()


def main() -> None:
    load = "--no-load" not in sys.argv
    with _connect() as conn:
        orgs = [dict(zip(("id", "norm_key", "name"), r)) for r in
                conn.execute("select id, norm_key, name from organizations").fetchall()]
        # The persisted blocking output, as a fast membership oracle.
        blocked = {(r[0], r[1]) for r in conn.execute(
            "select left_key, right_key from organization_merge_candidate").fetchall()}
        names = {o["norm_key"]: o["name"] for o in orgs}

    mined = mine(orgs)
    rows = []
    for (ka, kb), meta in mined.items():
        missed = (ka, kb) not in blocked
        rows.append({
            "key_a": ka, "key_b": kb,
            "name_a": names.get(ka), "name_b": names.get(kb),
            "pattern": meta["patterns"][0] if len(meta["patterns"]) == 1
                       else "+".join(meta["patterns"]),
            "missed_by_blocking": missed,
            "label": None, "reason": None,           # reviewer fills these in
            "source": "mined_supplement",
        })

    # Report: per-pattern yield and — the number that matters — how many each
    # pattern produces that current blocking MISSES (i.e. true hard positives).
    from collections import Counter
    total = Counter(); missed = Counter()
    for r in rows:
        total[r["pattern"]] += 1
        if r["missed_by_blocking"]:
            missed[r["pattern"]] += 1
    print("\n" + "=" * 60)
    print(" MINED HARD CANDIDATES  (pattern: missed / total)")
    print("=" * 60)
    for pat in sorted(total, key=lambda p: -missed[p]):
        print(f"  {pat:<22} {missed[pat]:>5} / {total[pat]:<6}  "
              f"<- {'BLOCKING GAP' if missed[pat] else 'covered'}")
    print("-" * 60)
    print(f"  total candidates       {sum(total.values()):>5}")
    print(f"  missed by blocking     {sum(missed.values()):>5}   <- label these first")
    print("=" * 60)

    # Write ALL mined pairs (missed first) for labeling; missed ones are the
    # hard positives, covered ones are still useful as supplement context.
    rows.sort(key=lambda r: (not r["missed_by_blocking"], r["pattern"]))
    OUT.parent.mkdir(exist_ok=True)
    with OUT.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"\n  wrote {len(rows)} candidates -> {OUT}")

    if load:
        with _connect() as conn:
            _stage_table(conn, rows)
        print(f"  staged {len(rows)} rows -> m2_gold_supplement (enriched)")
        print("  label with:  python experiments/entity_resolution/label.py --corpus supplement")
    print("  NOTE: label=null — these need human labels before joining gold_v2.\n")


if __name__ == "__main__":
    main()
