"""
The canonicalizer — a metered PIPELINE, not a monolith. Each parsed record flows
through ordered stages; every stage increments counters so the run log reads
like a health check:

    normalized: 84,534 | countries: 84,534 | programs: 84,534
    orgs exact: 81,920 | orgs created: 2,614 | candidates: 612 | failed: 2

Stages (all deterministic — Phases A/B/C):
    validate -> resolve dimensions -> resolve exact org -> generate candidates
    -> build recognition + parties -> (after all) refresh derived views

It does NOT merge organizations. Near-duplicates are written to entity_candidates
for later review (Phase D). Canonical is a pure projection of parsed_records, so
a run truncates and rebuilds — no stale state.
"""
from __future__ import annotations

import uuid
from collections import defaultdict

from stevie_platform import db
from stevie_platform.canonical import ops
from stevie_platform.canonical.normalize import (
    build_location_vocab, build_merge_closure, norm_key, normalize_org,
)
from stevie_platform.parsing.parse import PARSER_VERSION


def _to_int(v) -> int | None:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


class _TxnCache(dict):
    """Cache of resolved entity ids that is rolled back with the connection.

    Each record is committed individually; on error the connection is rolled
    back. Without this, ids created by the failed record (uncommitted INSERTs)
    stay in the cache and every later record sharing that dimension reuses a
    now-nonexistent id, cascading FK violations. `mark`/`discard_new` keep the
    cache consistent with what actually committed.
    """

    def __init__(self) -> None:
        super().__init__()
        self._pending: list = []

    def __setitem__(self, key, value) -> None:
        if key not in self:
            self._pending.append(key)
        super().__setitem__(key, value)

    def mark(self) -> None:
        """Start tracking keys added while processing the current record."""
        self._pending = []

    def discard_new(self) -> None:
        """Record failed + rolled back — drop the (now-invalid) keys it added."""
        for key in self._pending:
            self.pop(key, None)
        self._pending = []


async def _simple(conn, table, raw, entity_type, cache, m, run_id, pid, pv):
    """Resolve a controlled dimension by exact norm_key; cache + log the link.
    Records per-dimension created/exact counts for the metrics block."""
    if not raw:
        return None
    nk = norm_key(raw)
    if not nk:
        return None
    key = (table, nk)
    if key in cache:
        eid, created = cache[key], False
    else:
        eid, created = await ops.get_or_create_simple(conn, table, nk, raw)
        cache[key] = eid
    m[f"{entity_type}:{'created' if created else 'exact'}"] += 1
    await ops.write_link(conn, pid, run_id, entity_type, eid, raw,
                         "new" if created else "exact", pv)
    return eid


async def _process(conn, run_id, pid, node, d, pv, cache, m, vocab, closure) -> None:
    # --- dimensions -------------------------------------------------------
    country_id  = await _simple(conn, "countries", d.get("country"), "country",
                                cache, m, run_id, pid, pv)
    industry_id = await _simple(conn, "industries", d.get("industry"), "industry",
                                cache, m, run_id, pid, pv)
    program_id  = await _simple(conn, "programs", d.get("award_programs"), "program",
                                cache, m, run_id, pid, pv)
    year = _to_int(d.get("year"))

    edition_id = None
    if program_id and year:
        key = ("ed", program_id, year)
        edition_id = cache.get(key)
        if edition_id is None:
            edition_id, created = await ops.get_or_create_edition(conn, program_id, year, d.get("award_programs"))
            cache[key] = edition_id
            m[f"edition:{'created' if created else 'exact'}"] += 1
        else:
            m["edition:exact"] += 1

    group_id = None
    if edition_id and d.get("category_group"):
        nk = norm_key(d["category_group"])
        key = ("grp", edition_id, nk)
        group_id = cache.get(key)
        if group_id is None:
            group_id, _ = await ops.get_or_create_group(conn, edition_id, nk, d["category_group"])
            cache[key] = group_id

    cat_id = catdef_id = None
    if d.get("category"):
        cdnk = norm_key(d["category"])
        ckey = ("category_definitions", cdnk)
        if ckey in cache:
            catdef_id = cache[ckey]
            m["category:exact"] += 1
        else:
            catdef_id, cd_created = await ops.get_or_create_simple(conn, "category_definitions", cdnk, d["category"])
            cache[ckey] = catdef_id
            m[f"category:{'created' if cd_created else 'exact'}"] += 1
        if edition_id:
            key = ("cat", edition_id, cdnk)
            cat_id = cache.get(key)
            if cat_id is None:
                cat_id, _ = await ops.get_or_create_category(conn, edition_id, group_id, catdef_id, cdnk, d["category"])
                cache[key] = cat_id
        await ops.write_link(conn, pid, run_id, "category_definition", catdef_id, d["category"], "exact", pv)

    # --- exact organization (entrant) + candidate generation --------------
    org_raw = d.get("organization_name")
    entrant_party = None
    if org_raw:
        # Brand-level normalization (location + suffix rules). Dedup on nk;
        # store cleaned display name, original raw_name, and legal_suffix.
        # Uses THIS record's structured city/state/country for location.
        nk, disp, legal_suffix = normalize_org(
            org_raw, city=d.get("city"), state=d.get("state_province"),
            country=d.get("country"), vocab=vocab)
        nk = closure.get(nk, nk)  # apply merge decisions
        okey = ("org", nk)
        if okey in cache:
            org_id, created = cache[okey], False
        else:
            org_id, created = await ops.get_or_create_org(
                conn, nk, disp, raw_name=org_raw, legal_suffix=legal_suffix)
            cache[okey] = org_id
        if created:
            m["organization:created"] += 1
            for cand_id, score in await ops.org_candidates(conn, org_raw, org_id):
                await ops.add_candidate(conn, pid, org_raw, cand_id, score, run_id)
                m["organization:candidates"] += 1
        else:
            m["organization:exact"] += 1
        await ops.write_link(conn, pid, run_id, "organization", org_id, org_raw,
                             "new" if created else "exact", pv)
        entrant_party = await ops.party_for_org(conn, org_id)

    # --- submitting agency (also an org, different role) ------------------
    submitter_party = None
    agency = d.get("submitting_agency")
    if agency:
        # The record's city/state/country describe the ENTRANT, not the agency,
        # so only the gazetteer (states/countries) is applied here — no
        # per-record location, to avoid wrongly stripping the agency's name.
        nk, disp, legal_suffix = normalize_org(agency, vocab=vocab)
        nk = closure.get(nk, nk)  # apply merge decisions
        akey = ("org", nk)
        if akey in cache:
            ag_id = cache[akey]
        else:
            ag_id, ag_created = await ops.get_or_create_org(
                conn, nk, disp, raw_name=agency, legal_suffix=legal_suffix)
            cache[akey] = ag_id
            if ag_created:
                m["organization:created"] += 1
            else:
                m["organization:exact"] += 1
        submitter_party = await ops.party_for_org(conn, ag_id)

    recipient_party = entrant_party  # entrant == recipient in ~99% of records

    # --- build recognition + party roles ---------------------------------
    rid = await ops.insert_recognition(
        conn, parsed_record_id=pid, node_id=node, crawl_run_id=run_id,
        fields={
            "program_edition_id": edition_id, "year": year, "category_id": cat_id,
            "category_group_id": group_id, "category_definition_id": catdef_id,
            "country_id": country_id, "industry_id": industry_id,
            "entrant_party_id": entrant_party, "submitter_party_id": submitter_party,
            "recipient_party_id": recipient_party,
            "result_level": d.get("result_level") or "other",
            "award_raw": d.get("award"), "nomination_title": d.get("nomination_title"),
            "city": d.get("city"), "state_province": d.get("state_province"),
            "submitting_agency_raw": agency, "notes": d.get("notes"),
        },
    )
    if entrant_party:
        await ops.add_recognition_party(conn, rid, entrant_party, "entrant", org_raw)
        await ops.add_recognition_party(conn, rid, recipient_party, "recipient", org_raw)
    if submitter_party:
        await ops.add_recognition_party(conn, rid, submitter_party, "submitter", agency)


async def canonicalize(run_id: uuid.UUID, *, fresh: bool = True) -> dict:
    pv = PARSER_VERSION
    cache = _TxnCache()
    m: dict = defaultdict(int)
    p = await db.pool()
    async with p.connection() as conn:
        await conn.execute("set pg_trgm.similarity_threshold = 0.3")
        if fresh:
            await ops.truncate_canonical(conn)
            await conn.commit()

        # Load merge decisions and build the closure map once, before touching
        # any records. The closure is a pure in-memory dict (loser_key ->
        # canonical_key) applied in the org-resolution path so that every record
        # mentioning a losing key resolves to the winning org row — deterministic
        # and order-independent.
        decisions_cur = await conn.execute(
            "select loser_key, winner_key "
            "from organization_merge_decision where decision = 'merge'"
        )
        closure = build_merge_closure(
            [(r["loser_key"], r["winner_key"]) for r in await decisions_cur.fetchall()]
        )
        if closure:
            print(f"[canonicalize] {len(closure)} merge decision(s) loaded")

        cur = await conn.execute(
            "select id, node_id, data, is_complete from parsed_records where parser_version = %s",
            (pv,),
        )
        rows = await cur.fetchall()
        await conn.commit()

        # Location-rule gazetteer: built from the country names actually present
        # in the data, so it's deterministic and independent of ingest order.
        vocab = build_location_vocab(
            {r["data"].get("country") for r in rows if r["data"].get("country")}
        )

        for row in rows:
            m["normalized"] += 1
            if not row["is_complete"]:
                m["missing_required"] += 1
                continue
            cache.mark()
            try:
                await _process(conn, run_id, row["id"], row["node_id"], row["data"],
                               pv, cache, m, vocab, closure)
                await conn.commit()
                m["recognitions_built"] += 1
            except Exception as e:  # noqa: BLE001 — isolate one bad record, keep going
                await conn.rollback()
                cache.discard_new()
                m["failed"] += 1
                print(f"[canonicalize] node {row['node_id']} FAILED: {str(e)[:160]}")
            if m["normalized"] % 5000 == 0:
                print(f"[canonicalize] {m['normalized']} processed, "
                      f"{m['recognitions_built']} built, "
                      f"{m['organization:created']} orgs, "
                      f"{m['organization:candidates']} candidates")

        # Emit organization_alias rows for every retired (loser) key so that
        # external consumers' stable keys redirect after a merge. Must happen
        # after all org rows exist so winner IDs are available.
        for loser_key, canonical_key in closure.items():
            cur = await conn.execute(
                "select id from organizations where norm_key = %s", (canonical_key,)
            )
            if winner_row := await cur.fetchone():
                await ops.upsert_alias(conn, loser_key, winner_row["id"], "merge_decision")
                m["aliases_written"] += 1
            else:
                m["aliases_orphaned"] += 1
                print(f"[canonicalize] orphaned merge winner: {canonical_key!r} "
                      f"(loser: {loser_key!r}) — normalization drift?")
        if closure:
            await conn.commit()

        await conn.execute("select refresh_derived()")
        await conn.commit()

    summary = dict(m)
    from stevie_platform.canonical.metrics import print_canonicalization_metrics
    await print_canonicalization_metrics(summary)
    return summary
