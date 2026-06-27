"""
READ-ONLY Phase D experiment: measure what the experimental org normalization
(normalize_v2.enhanced_key) would collapse, WITHOUT touching canonical tables.

Output: before/after org counts, reduction, the largest new merge clusters, a
random audit sample, and a safety scan for suspiciously generic keys. Also
writes experiments/org_normalization/REPORT.md.

    .venv/bin/python experiments/org_normalization/analyze.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from collections import defaultdict

import psycopg
from psycopg.rows import dict_row

sys.path.insert(0, os.path.dirname(__file__))
from normalize_v2 import base_location_vocab, enhanced_key  # noqa: E402

from stevie_platform.canonical.normalize import norm_key  # noqa: E402
from stevie_platform.config import DATABASE_URL  # noqa: E402

REPORT = os.path.join(os.path.dirname(__file__), "REPORT.md")


async def main() -> None:
    async with await psycopg.AsyncConnection.connect(
        DATABASE_URL, connect_timeout=10, row_factory=dict_row
    ) as conn:
        cur = await conn.execute("select name from countries")
        vocab = base_location_vocab([r["name"] for r in await cur.fetchall()])

        cur = await conn.execute(
            "select data->>'organization_name' org, data->>'city' city, "
            "data->>'state_province' st, data->>'country' country "
            "from parsed_records "
            "where is_complete and coalesce(data->>'organization_name','') <> ''"
        )
        rows = await cur.fetchall()

    # after_key -> {before_key -> (representative_raw, record_count)}
    clusters: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(lambda: [None, 0]))
    before_keys: set[str] = set()
    after_keys: set[str] = set()
    records = 0
    changed_records = 0

    for r in rows:
        raw = r["org"]
        bkey = norm_key(raw)
        akey = enhanced_key(raw, city=r["city"], state=r["st"],
                            country=r["country"], base_vocab=vocab)
        if not bkey:
            continue
        records += 1
        before_keys.add(bkey)
        after_keys.add(akey)
        if akey != bkey:
            changed_records += 1
        slot = clusters[akey][bkey]
        if slot[0] is None:
            slot[0] = raw
        slot[1] += 1

    # New merges = after-keys that absorb >=2 distinct before-keys.
    merges = {ak: bks for ak, bks in clusters.items() if len(bks) >= 2}
    merged_before_variants = sum(len(bks) for bks in merges.values())

    b, a = len(before_keys), len(after_keys)
    lines: list[str] = []
    def out(s: str = "") -> None:
        lines.append(s)
        print(s)

    out("# Phase D — org normalization experiment (read-only)\n")
    out(f"- records considered      : {records:,}")
    out(f"- records whose key changed: {changed_records:,} "
        f"({changed_records / records:.1%})")
    out(f"- distinct orgs BEFORE     : {b:,}")
    out(f"- distinct orgs AFTER      : {a:,}")
    out(f"- reduction                : {b - a:,}  ({(b - a) / b:.1%})")
    out(f"- new merge clusters       : {len(merges):,} "
        f"(absorbing {merged_before_variants:,} prior distinct keys)")
    out("")

    # Largest merge clusters (most distinct variants collapsed).
    out("## Top 25 merge clusters (most variants collapsed)\n")
    top = sorted(merges.items(), key=lambda kv: len(kv[1]), reverse=True)[:25]
    for ak, bks in top:
        total = sum(c for _, c in bks.values())
        out(f"### `{ak}`  — {len(bks)} variants, {total:,} records")
        for raw, cnt in sorted(((v[0], v[1]) for v in bks.values()),
                               key=lambda x: x[1], reverse=True)[:8]:
            out(f"    - {cnt:>5}  {raw}")
        out("")

    # Random-ish audit sample (deterministic: every Nth merge cluster).
    out("## Audit sample (every 50th merge cluster)\n")
    sample = sorted(merges.items())[::50][:25]
    for ak, bks in sample:
        variants = [v[0] for v in bks.values()]
        out(f"- `{ak}`  <=  {variants[:5]}")
    out("")

    # SAFETY: flag suspiciously generic keys that absorbed many variants.
    out("## ⚠ Safety scan — short/generic keys absorbing many variants\n")
    suspicious = [(ak, len(bks)) for ak, bks in merges.items()
                  if (len(ak) <= 4 or len(ak.split()) == 1) and len(bks) >= 3]
    if suspicious:
        for ak, n in sorted(suspicious, key=lambda x: x[1], reverse=True)[:20]:
            variants = [v[0] for v in clusters[ak].values()][:6]
            out(f"- `{ak}` ({n} variants): {variants}")
    else:
        out("  none — no short/single-token key absorbed >=3 variants ✓")
    out("")

    with open(REPORT, "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"\n[written] {REPORT}")


if __name__ == "__main__":
    asyncio.run(main())
