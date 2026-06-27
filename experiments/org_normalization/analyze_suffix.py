"""
READ-ONLY Phase D experiment, step 2: corporate-suffix normalization, measured
in isolation and combined with the location rule. No canonical changes.

Produces:
  - distinct-org reduction under: before / location / suffix-only / combined
  - additivity check (loc + suffix  vs  combined)
  - suffix-attributable merges (combined vs location), audit + safety scan,
    flagging merges that join DIFFERENT legal forms (inc vs llc vs ltd, …)
  - downstream: % of entity_candidates (fuzzy workload) auto-resolved by each rule

Writes experiments/org_normalization/REPORT_suffix.md.
"""
from __future__ import annotations

import asyncio
import os
import sys
from collections import Counter, defaultdict

import psycopg
from psycopg.rows import dict_row

sys.path.insert(0, os.path.dirname(__file__))
from normalize_v2 import (base_location_vocab, enhanced_key,  # noqa: E402
                          strip_corporate_suffix)

from stevie_platform.canonical.normalize import norm_key  # noqa: E402
from stevie_platform.config import DATABASE_URL  # noqa: E402

REPORT = os.path.join(os.path.dirname(__file__), "REPORT_suffix.md")
_SUFFIX_TOK = {"inc", "incorporated", "llc", "ltd", "limited", "corp",
               "corporation", "company", "co", "plc", "llp", "lp", "pllc",
               "gmbh", "pte", "pty", "pvt"}


def _trailing_suffix(loc_key: str) -> str | None:
    toks = loc_key.split()
    return toks[-1] if toks and toks[-1] in _SUFFIX_TOK else None


async def main() -> None:
    out_lines: list[str] = []
    def out(s: str = "") -> None:
        out_lines.append(s); print(s)

    async with await psycopg.AsyncConnection.connect(
        DATABASE_URL, connect_timeout=10, row_factory=dict_row
    ) as conn:
        cur = await conn.execute("select name from countries")
        vocab = base_location_vocab([r["name"] for r in await cur.fetchall()])

        cur = await conn.execute(
            "select data->>'organization_name' org, data->>'city' city, "
            "data->>'state_province' st, data->>'country' country "
            "from parsed_records "
            "where is_complete and coalesce(data->>'organization_name','') <> ''")
        records = await cur.fetchall()

        # Org-level new keys (mode over each org's recognitions) for downstream.
        cur = await conn.execute(
            "select o.id org_id, rp.raw_value raw, r.city, r.state_province st, "
            "       c.name country "
            "from recognitions r "
            "join recognition_parties rp on rp.recognition_id = r.id and rp.role='recipient' "
            "join parties p on p.id = rp.party_id "
            "join organizations o on o.id = p.organization_id "
            "left join countries c on c.id = r.country_id")
        org_rows = await cur.fetchall()

        cur = await conn.execute(
            "select ec.raw_value, ec.candidate_entity_id cand, "
            "       pr.data->>'city' city, pr.data->>'state_province' st, "
            "       pr.data->>'country' country "
            "from entity_candidates ec "
            "join parsed_records pr on pr.id = ec.parsed_record_id "
            "where ec.entity_type = 'organization'")
        cand_rows = await cur.fetchall()

    def keys(name, city, st, country):
        return (
            norm_key(name),
            enhanced_key(name, city=city, state=st, country=country,
                         base_vocab=vocab, strip_location=True, strip_suffix=False),
            enhanced_key(name, base_vocab=vocab, strip_location=False, strip_suffix=True),
            enhanced_key(name, city=city, state=st, country=country,
                         base_vocab=vocab, strip_location=True, strip_suffix=True),
        )

    # --- 1. distinct-org reduction ------------------------------------------
    s_before, s_loc, s_suf, s_comb = set(), set(), set(), set()
    # combined-key -> set of distinct location-keys it absorbs (suffix effect)
    comb_to_loc: dict[str, set[str]] = defaultdict(set)
    loc_to_raw: dict[str, str] = {}
    for r in records:
        kb, kl, ks, kc = keys(r["org"], r["city"], r["st"], r["country"])
        if not kb:
            continue
        s_before.add(kb); s_loc.add(kl); s_suf.add(ks); s_comb.add(kc)
        comb_to_loc[kc].add(kl)
        loc_to_raw.setdefault(kl, r["org"])

    b, L, S, C = len(s_before), len(s_loc), len(s_suf), len(s_comb)
    out("# Phase D step 2 — corporate-suffix normalization (read-only)\n")
    out("## Distinct-org reduction\n")
    out(f"- before (norm_key only) : {b:,}")
    out(f"- location rule only     : {L:,}   (−{b-L:,}, {(b-L)/b:.1%})")
    out(f"- suffix rule only       : {S:,}   (−{b-S:,}, {(b-S)/b:.1%})")
    out(f"- combined (loc+suffix)  : {C:,}   (−{b-C:,}, {(b-C)/b:.1%})")
    out("")
    out("## Additivity check\n")
    out(f"- location reduction      : {b-L:,}")
    out(f"- suffix reduction        : {b-S:,}")
    out(f"- sum if independent      : {(b-L)+(b-S):,}")
    out(f"- actual combined         : {b-C:,}")
    overlap = (b-L)+(b-S) - (b-C)
    out(f"- overlap (double-counted): {overlap:,}  "
        f"-> {'≈ additive' if abs(overlap) <= 0.15*(b-C) else 'NOT additive (rules overlap)'}")
    out("")

    # --- 2. suffix-attributable merges (combined merges loc keys) ------------
    suffix_merges = {kc: locs for kc, locs in comb_to_loc.items() if len(locs) >= 2}
    diff_form = {kc: locs for kc, locs in suffix_merges.items()
                 if len({_trailing_suffix(l) for l in locs if _trailing_suffix(l)}) >= 2}
    out("## Suffix-attributable merges\n")
    out(f"- combined-keys that absorb >=2 distinct location-keys: {len(suffix_merges):,}")
    out(f"- of those, merging DIFFERENT legal forms (inc vs llc vs …): {len(diff_form):,}")
    out("")
    out("### Top 20 suffix merges (by # location-variants absorbed)\n")
    for kc, locs in sorted(suffix_merges.items(), key=lambda kv: len(kv[1]), reverse=True)[:20]:
        examples = [loc_to_raw.get(l, l) for l in sorted(locs)][:6]
        flag = "  ⚠ different legal forms" if kc in diff_form else ""
        out(f"- `{kc}`  <= {examples}{flag}")
    out("")
    out("### ⚠ Safety — every merge that joins different legal forms (first 25)\n")
    for kc, locs in sorted(diff_form.items())[:25]:
        examples = [loc_to_raw.get(l, l) for l in sorted(locs)][:6]
        out(f"- `{kc}`  <= {examples}")
    if not diff_form:
        out("  none ✓")
    out("")

    # --- 3. downstream: entity_candidates auto-resolved ---------------------
    org_loc: dict[int, Counter] = defaultdict(Counter)
    org_comb: dict[int, Counter] = defaultdict(Counter)
    for r in org_rows:
        _, kl, _, kc = keys(r["raw"], r["city"], r["st"], r["country"])
        org_loc[r["org_id"]][kl] += 1
        org_comb[r["org_id"]][kc] += 1
    okey_loc = {oid: c.most_common(1)[0][0] for oid, c in org_loc.items()}
    okey_comb = {oid: c.most_common(1)[0][0] for oid, c in org_comb.items()}

    total = res_loc = res_comb = 0
    for r in cand_rows:
        cand = r["cand"]
        if cand not in okey_loc:
            continue
        total += 1
        _, kl, _, kc = keys(r["raw_value"], r["city"], r["st"], r["country"])
        if kl == okey_loc[cand]:
            res_loc += 1
        if kc == okey_comb[cand]:
            res_comb += 1
    out("## Downstream — fuzzy-comparison workload (entity_candidates)\n")
    out(f"- entity_candidates (org), measurable : {total:,}")
    out(f"- auto-resolved by location rule       : {res_loc:,}  ({res_loc/total:.1%})")
    out(f"- auto-resolved by combined rule       : {res_comb:,}  ({res_comb/total:.1%})")
    out(f"- remaining fuzzy workload (combined)  : {total-res_comb:,}  "
        f"({(total-res_comb)/total:.1%})")
    out("")

    with open(REPORT, "w") as f:
        f.write("\n".join(out_lines) + "\n")
    print(f"\n[written] {REPORT}")


if __name__ == "__main__":
    asyncio.run(main())
