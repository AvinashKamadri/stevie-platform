"""
Blocking recall harness (Phase F / M4) — measures the RECALL CEILING.

Candidate generation sets the ceiling on everything downstream: a gold merge pair
that no blocker surfaces can never be recovered by any scorer or reviewer. So we
measure blocking *in isolation* against the gold set BEFORE a scorer exists.

The primary output is NOT the headline percentage — it is the FAILURE LIST: the
gold merge pairs that no blocker generated, bucketed by likely cause. That list is
the work queue for the next blocker. Failures split into two kinds:

  missing_org   — a gold key is absent from organizations.norm_key, so NO blocker
                  could pair it (normalization drift, or the key was merged away).
                  This is a data/normalization finding, not a blocking gap.
  blocking_gap  — both keys exist as orgs but no blocker connected them. This is
                  the real blocker work queue (heavy abbreviation, word-order,
                  cross-script, missing geo signal, …).

We also report each blocker's MARGINAL recall (gold pairs it ALONE catches) so a
blocker that only duplicates others' coverage can be retired, and the candidate
NOISE on the 'distinct' gold pairs (how many non-matches blocking lets through —
a preview of the scorer's rejection load).
"""
from __future__ import annotations

import json
from pathlib import Path

from stevie_platform.canonical.candidates import order_pair, generate
from stevie_platform.config import BASE_DIR

GOLD_DIR = BASE_DIR / "experiments" / "entity_resolution" / "gold"
GOLD_PATH = GOLD_DIR / "pairs.jsonl"
MANIFEST = GOLD_DIR / "CORPUS.json"
FAILURES_OUT = GOLD_DIR / "blocking_failures.jsonl"


def wilson_interval(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion k/n — the right CI for a
    yield estimated from a small random sample (unlike the normal approximation,
    it stays inside [0,1] and behaves at extreme rates). Returns (lo, hi) as
    fractions. n==0 -> (0.0, 1.0): no information.

    Used to put a confidence band on a blocker pattern's true-merge yield
    measured on a UNIFORM RANDOM sample, so the projected recall contribution
    carries its uncertainty instead of masquerading as a point estimate."""
    if n <= 0:
        return (0.0, 1.0)
    import math
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def _pair_key(key_a: str, key_b: str) -> tuple[str, str]:
    """Order a gold pair the same way candidates are ordered (ids are irrelevant
    here, so pass 0)."""
    lk, _, rk, _ = order_pair(key_a, 0, key_b, 0)
    return lk, rk


def _load_jsonl(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def load_gold(path: Path = GOLD_PATH) -> list[dict]:
    return _load_jsonl(path)


def load_corpus(version: str | None = None) -> tuple[list[dict], str, list[str]]:
    """Resolve a corpus version from the manifest into a deduped gold list.

    Returns (gold, resolved_version, missing_files). Files listed in the manifest
    but not yet on disk (e.g. supplement.jsonl before labeling) are skipped and
    reported, so `v2` gracefully degrades to whatever exists. Dedup is by ordered
    (key_a,key_b) pair — a later file's pair does not duplicate an earlier one."""
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    version = version or manifest.get("default", "v1")
    if version not in manifest["versions"]:
        raise SystemExit(f"unknown corpus version {version!r}; "
                         f"have {list(manifest['versions'])}")
    gold: list[dict] = []
    seen: set[tuple[str, str]] = set()
    missing: list[str] = []
    for comp in manifest["versions"][version]["components"]:
        fname = comp["file"]
        fpath = GOLD_DIR / fname
        if not fpath.exists():
            missing.append(fname)
            continue
        for g in _load_jsonl(fpath):
            pk = _pair_key(g["key_a"], g["key_b"])
            if pk not in seen:
                seen.add(pk)
                g["_component"] = fname          # provenance for the breakdown
                gold.append(g)
    return gold, version, missing


async def _org_keys(conn) -> set[str]:
    cur = await conn.execute("select norm_key from organizations")
    return {r["norm_key"] for r in await cur.fetchall()}


def evaluate(gold: list[dict], candidate_reasons: dict[tuple[str, str], tuple[str, ...]],
             org_keys: set[str]) -> dict:
    """Pure evaluation core (no DB) — easy to unit-test.

    candidate_reasons: ordered-pair -> reasons tuple (the blocking output).
    org_keys: norm_keys that currently exist as organizations.
    Returns recall, marginal-per-blocker, the bucketed failure list, and a
    per-component breakdown (by gold file) so partial labeling is informative.

    Labels: 'merge' (same entity — the recall target), 'distinct' (different
    entities), 'related' (related but NOT the same — parent/subsidiary,
    org/foundation; future relationship-graph seed). Only 'merge' counts toward
    blocking recall; 'related' is tracked separately, never folded into either."""
    merges = [g for g in gold if g["label"] == "merge"]
    distincts = [g for g in gold if g["label"] == "distinct"]
    related = [g for g in gold if g["label"] == "related"]

    found, failures = [], []
    marginal: dict[str, int] = {}        # gold pairs caught by EXACTLY this blocker
    found_by: dict[str, int] = {}        # gold pairs this blocker caught (may overlap)
    for g in merges:
        pk = _pair_key(g["key_a"], g["key_b"])
        reasons = candidate_reasons.get(pk)
        if reasons is not None:
            found.append(g)
            for blocker in reasons:
                found_by[blocker] = found_by.get(blocker, 0) + 1
            if len(reasons) == 1:  # caught by exactly one blocker -> its marginal
                marginal[reasons[0]] = marginal.get(reasons[0], 0) + 1
        else:
            missing = [k for k in (g["key_a"], g["key_b"]) if k not in org_keys]
            failures.append({
                "key_a": g["key_a"], "key_b": g["key_b"],
                "name_a": g.get("name_a"), "name_b": g.get("name_b"),
                "sim": g.get("sim"), "band": g.get("band"),
                "bucket": "missing_org" if missing else "blocking_gap",
                "missing_keys": missing,
            })

    # Noise the scorer must reject: 'distinct' (and 'related') gold pairs that
    # blocking nonetheless surfaces — a specificity preview.
    distinct_surfaced = sum(1 for g in distincts
                            if _pair_key(g["key_a"], g["key_b"]) in candidate_reasons)
    related_surfaced = sum(1 for g in related
                           if _pair_key(g["key_a"], g["key_b"]) in candidate_reasons)

    # Per-component breakdown (by source file) so a partially-labeled supplement
    # already tells you "+N supplemental merges, M of them blocking gaps".
    components: dict[str, dict] = {}
    for g in gold:
        comp = components.setdefault(g.get("_component", "?"),
                                     {"merge": 0, "distinct": 0, "related": 0, "found": 0})
        lbl = g["label"] if g["label"] in comp else None
        if lbl:
            comp[lbl] += 1
        if g["label"] == "merge" and _pair_key(g["key_a"], g["key_b"]) in candidate_reasons:
            comp["found"] += 1

    n_merge = len(merges)
    blocking_gaps = [f for f in failures if f["bucket"] == "blocking_gap"]
    missing = [f for f in failures if f["bucket"] == "missing_org"]
    # Achievable recall excludes missing_org pairs (no blocker COULD catch them).
    achievable = n_merge - len(missing)
    return {
        "gold_merge_pairs": n_merge,
        "found": len(found),
        "recall_overall": round(100.0 * len(found) / n_merge, 1) if n_merge else 0.0,
        "recall_achievable": round(100.0 * len(found) / achievable, 1) if achievable else 0.0,
        "marginal_recall": marginal,
        "found_by_blocker": found_by,
        "failures_blocking_gap": len(blocking_gaps),
        "failures_missing_org": len(missing),
        "distinct_pairs": len(distincts),
        "distinct_surfaced": distinct_surfaced,
        "related_pairs": len(related),
        "related_surfaced": related_surfaced,
        "components": components,
        "failure_list": failures,
    }


def _print_report(r: dict, *, n_orgs: int = 0, stats: list | None = None) -> None:
    """M4 completion-criteria report. Once these numbers are stable, the recall
    ceiling for the current blocking strategy is 'locked' — any future blocker
    must justify itself by raising achievable recall enough to offset the
    candidate volume it adds (visible in the efficiency table below)."""
    stats = stats or []
    found_by = r.get("found_by_blocker", {})
    print("\n" + "=" * 64)
    print(" M4 — BLOCKING RECALL CEILING")
    print("=" * 64)
    print(f"  gold corpus                 {r.get('corpus', '?'):>10}   "
          f"({r['gold_merge_pairs']} merge / {r['distinct_pairs']} distinct"
          f"{f' / {r['related_pairs']} related' if r.get('related_pairs') else ''})")
    comps = r.get("components", {})
    if len(comps) > 1:
        for fname, c in comps.items():
            print(f"    {fname:<24} {c['merge']:>3} merge ({c['found']} found) / "
                  f"{c['distinct']} distinct / {c['related']} related")
    if n_orgs:
        print(f"  organizations               {n_orgs:>10,}")
    print(f"  candidate pairs (union)     {r.get('candidate_pairs', 0):>10,}")
    print("-" * 64)
    print("  Gold recall:")
    print(f"    achievable*               {r['recall_achievable']:>9}%   "
          f"(*excludes missing_org)")
    print(f"    overall                   {r['recall_overall']:>9}%")
    print(f"    missing_org               {r['failures_missing_org']:>10}   <- normalization/indexing")
    print(f"    blocking_gap              {r['failures_blocking_gap']:>10}   <- blocker work queue")
    print("-" * 64)
    print("  Marginal recall (gold pairs a blocker ALONE catches):")
    if r["marginal_recall"]:
        for blocker, n in sorted(r["marginal_recall"].items(), key=lambda x: -x[1]):
            print(f"    {blocker:<22} +{n}")
    else:
        print("    (none uniquely attributable)")
    if stats:
        print("-" * 64)
        print("  Blocker efficiency:")
        print(f"    {'blocker':<14}{'gold found':>11}{'emitted':>12}{'gold/emit':>12}{'runtime':>10}")
        for s in stats:
            gf = found_by.get(s.name, 0)
            proxy = (gf / s.emitted) if s.emitted else 0.0
            print(f"    {s.name:<14}{gf:>11}{s.emitted:>12,}{proxy:>12.4f}{s.runtime_s:>9.1f}s")
        total_emitted = sum(s.emitted for s in stats)
        total_rt = sum(s.runtime_s for s in stats)
        print(f"    {'union':<14}{r['found']:>11}{r.get('candidate_pairs', 0):>12,}"
              f"{'':>12}{total_rt:>9.1f}s")
        print(f"    (emitted is pre-union: {total_emitted:,} raw across blockers)")
    print("-" * 64)
    print(f"  distinct gold pairs         {r['distinct_pairs']:>10}")
    print(f"  ...surfaced as candidates   {r['distinct_surfaced']:>10}   <- scorer must reject these")
    if r.get("related_pairs"):
        print(f"  related gold pairs          {r['related_pairs']:>10}   <- relationship-graph seed, not merges")
        print(f"  ...surfaced as candidates   {r['related_surfaced']:>10}   <- scorer must NOT merge these")
    print("=" * 64)
    gaps = [f for f in r["failure_list"] if f["bucket"] == "blocking_gap"]
    if gaps:
        print("\n  Blocking gaps (first 15) — these need a new/better blocker:")
        for f in gaps[:15]:
            print(f"    [{f['band']:>4} sim={f['sim']}] {f['name_a']!r}  <->  {f['name_b']!r}")
        if len(gaps) > 15:
            print(f"    ... and {len(gaps) - 15} more (full list in {FAILURES_OUT.name})")
    print()


async def run_recall(*, corpus: str | None = None, write_failures: bool = True) -> dict:
    """CLI entry: generate candidates (blockers only), score recall against the
    selected gold corpus, print the report, and write the failure list to disk."""
    from stevie_platform import db
    from stevie_platform.canonical.candidates import org_count
    gold, version, missing = load_corpus(corpus)
    if missing:
        print(f"[recall] corpus {version}: skipping absent file(s) {missing} "
              f"(not labeled yet)")
    p = await db.pool()
    async with p.connection() as conn:
        n_orgs = await org_count(conn)
        org_keys = await _org_keys(conn)
        pairs, stats = await generate(conn)
    candidate_reasons = {(pr.left_key, pr.right_key): pr.reasons for pr in pairs}
    r = evaluate(gold, candidate_reasons, org_keys)
    r["candidate_pairs"] = len(pairs)
    r["corpus"] = version
    _print_report(r, n_orgs=n_orgs, stats=stats)
    if write_failures:
        with open(FAILURES_OUT, "w", encoding="utf-8") as f:
            for item in r["failure_list"]:
                f.write(json.dumps(item) + "\n")
        print(f"  failure list -> {FAILURES_OUT}\n")
    return r
