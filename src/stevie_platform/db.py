"""
Async Postgres access (psycopg 3). Thin, explicit helpers — no ORM.

Every write here targets one of the three human/crawler-owned tables
(raw_pages, fetch_queue, harvest_state) or the regenerable parsed_records.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import uuid
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from stevie_platform.config import DATABASE_URL, NETWORK_LOCK_KEY

_pool: AsyncConnectionPool | None = None


async def pool() -> AsyncConnectionPool:
    global _pool
    if _pool is None:
        _pool = AsyncConnectionPool(DATABASE_URL, open=False, kwargs={"row_factory": dict_row})
        await _pool.open()
    return _pool


async def close() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def checksum(html: bytes) -> str:
    return hashlib.sha256(html).hexdigest()


# --- network stage mutex (sequential pipeline mode) ------------------------
async def try_network_lock():
    """Acquire the session-level advisory lock that serializes network stages.

    Returns an OPEN connection holding the lock (close it to release), or None if
    another network stage already holds it. Held for the whole stage so harvest
    and fetch can't overlap when PIPELINE_MODE='sequential'.
    """
    conn = await psycopg.AsyncConnection.connect(DATABASE_URL)
    cur = await conn.execute("select pg_try_advisory_lock(%s)", (NETWORK_LOCK_KEY,))
    got = (await cur.fetchone())[0]
    if not got:
        await conn.close()
        return None
    return conn


# --- crawl_runs (provenance spine) -----------------------------------------
async def start_crawl_run(kind: str, *, parser_version: str | None = None,
                          git_commit: str | None = None, notes: str | None = None) -> uuid.UUID:
    """Open a run row and return its id. Every raw page / recognition / link
    produced by this invocation carries it, so any row is traceable to the run
    (and code/parser version) that produced it."""
    p = await pool()
    async with p.connection() as conn:
        cur = await conn.execute(
            "insert into crawl_runs (kind, parser_version, git_commit, notes) "
            "values (%s,%s,%s,%s) returning id",
            (kind, parser_version, git_commit, notes),
        )
        return (await cur.fetchone())["id"]


async def last_run_stats(kind: str) -> dict | None:
    """Stats blob from the most recent finished run of a given kind."""
    p = await pool()
    async with p.connection() as conn:
        cur = await conn.execute(
            "select stats from crawl_runs where kind = %s and stats is not null "
            "order by started_at desc limit 1",
            (kind,),
        )
        row = await cur.fetchone()
        return row["stats"] if row else None


async def finish_crawl_run(run_id: uuid.UUID, stats: dict | None = None) -> None:
    p = await pool()
    async with p.connection() as conn:
        await conn.execute(
            "update crawl_runs set finished_at = now(), stats = %s where id = %s",
            (json.dumps(stats or {}), str(run_id)),
        )


# --- meta -------------------------------------------------------------------
async def set_meta(key: str, value: Any) -> None:
    p = await pool()
    async with p.connection() as conn:
        await conn.execute(
            "insert into meta (key, value, updated_at) values (%s, %s, now()) "
            "on conflict (key) do update set value = excluded.value, updated_at = now()",
            (key, json.dumps(value)),
        )


async def get_meta(key: str) -> Any | None:
    p = await pool()
    async with p.connection() as conn:
        cur = await conn.execute("select value from meta where key = %s", (key,))
        row = await cur.fetchone()
        return row["value"] if row else None


async def get_status() -> dict:
    p = await pool()
    async with p.connection() as conn:
        cur = await conn.execute("select * from acquisition_status")
        return await cur.fetchone() or {}


# --- raw_pages (state 1) ----------------------------------------------------
async def save_raw_page(
    *, url: str, page_type: str, html: bytes, http_status: int,
    crawl_run_id: uuid.UUID, node_id: str | None = None,
    listing_page: int | None = None, headers: dict | None = None,
) -> int | None:
    """Insert one immutable raw page. Returns its id, or None if (url, checksum)
    already exists (identical content already archived)."""
    p = await pool()
    cs = checksum(html)
    async with p.connection() as conn:
        cur = await conn.execute(
            """insert into raw_pages
                 (url, page_type, node_id, listing_page, html, http_status,
                  checksum, headers, crawl_run_id)
               values (%s,%s,%s,%s,%s,%s,%s,%s,%s)
               on conflict (url, checksum) do nothing
               returning id""",
            (url, page_type, node_id, listing_page, gzip.compress(html),
             http_status, cs, json.dumps(headers or {}), str(crawl_run_id)),
        )
        row = await cur.fetchone()
        if row:
            return row["id"]
        cur = await conn.execute(
            "select id from raw_pages where url = %s and checksum = %s", (url, cs)
        )
        existing = await cur.fetchone()
        return existing["id"] if existing else None


async def iter_raw_detail_pages(batch: int = 500):
    """Yield (raw_page_id, node_id, decompressed_html) for every detail page —
    the input to a full reparse."""
    p = await pool()
    async with p.connection() as conn:
        async with conn.cursor(name="raw_detail_cursor") as cur:
            await cur.execute(
                "select id, node_id, html from raw_pages where page_type = 'detail'"
            )
            while rows := await cur.fetchmany(batch):
                for r in rows:
                    yield r["id"], r["node_id"], gzip.decompress(r["html"]).decode("utf-8", "replace")


# --- blog (Phase 1 close-out: blog corpus + graph edges) --------------------
async def blog_archived_urls() -> set[str]:
    """URLs already archived as blog pages — lets fetch run incrementally."""
    p = await pool()
    async with p.connection() as conn:
        cur = await conn.execute(
            "select distinct url from raw_pages where page_type = 'blog'")
        return {r["url"] for r in await cur.fetchall()}


async def iter_raw_blog_pages(batch: int = 200):
    """Yield (raw_page_id, url, decompressed_html) for every archived blog page —
    the input to blog extraction."""
    p = await pool()
    async with p.connection() as conn:
        async with conn.cursor(name="raw_blog_cursor") as cur:
            await cur.execute(
                "select id, url, html from raw_pages where page_type = 'blog'")
            while rows := await cur.fetchmany(batch):
                for r in rows:
                    yield r["id"], r["url"], gzip.decompress(r["html"]).decode("utf-8", "replace")


async def upsert_blog_post(*, url: str, slug: str, title: str | None,
                           author: str | None, published_at, lang: str | None,
                           clean_text: str, raw_page_id: int | None,
                           crawl_run_id: uuid.UUID) -> int:
    """Insert/refresh one blog post, keyed on the stable url. Returns its id."""
    p = await pool()
    async with p.connection() as conn:
        cur = await conn.execute(
            """insert into blog_posts
                 (url, slug, title, author, published_at, lang, clean_text,
                  raw_page_id, crawl_run_id, fetched_at)
               values (%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
               on conflict (url) do update set
                 slug=excluded.slug, title=excluded.title, author=excluded.author,
                 published_at=excluded.published_at, lang=excluded.lang,
                 clean_text=excluded.clean_text, raw_page_id=excluded.raw_page_id,
                 crawl_run_id=excluded.crawl_run_id, fetched_at=now()
               returning id""",
            (url, slug, title, author, published_at, lang, clean_text,
             raw_page_id, str(crawl_run_id)))
        return (await cur.fetchone())["id"]


async def blog_language_counts() -> list[dict]:
    """Post count per detected language (for `blog --report`)."""
    p = await pool()
    async with p.connection() as conn:
        cur = await conn.execute(
            "select coalesce(lang,'?') as lang, count(*) as n from blog_posts "
            "group by lang order by n desc")
        return list(await cur.fetchall())


async def blog_link_sources() -> tuple[list, list, list, list]:
    """Canonical entities the linker resolves mentions against + editions."""
    p = await pool()
    async with p.connection() as conn:
        async def rows(q: str) -> list:
            cur = await conn.execute(q)
            return list(await cur.fetchall())
        programs = await rows("select id, slug, name from programs")
        categories = await rows("select id, slug, name from category_definitions")
        organizations = await rows("select id, slug, name from organizations")
        editions = await rows("select program_id, year, slug from program_editions")
    return programs, categories, organizations, editions


async def all_blog_posts() -> list[dict]:
    """Every stored (English) post — small corpus, load in one shot."""
    p = await pool()
    async with p.connection() as conn:
        cur = await conn.execute("select id, title, clean_text from blog_posts")
        return list(await cur.fetchall())


async def clear_blog_entity_links() -> None:
    """Truncate-and-rebuild: the linker is regenerable from blog_posts."""
    p = await pool()
    async with p.connection() as conn:
        await conn.execute("truncate blog_entity_links restart identity")


async def insert_blog_entity_links(blog_id: int, edges: list[dict]) -> int:
    """Write a post's edges. Reference-only edges are already resolved upstream."""
    if not edges:
        return 0
    p = await pool()
    async with p.connection() as conn:
        for e in edges:
            await conn.execute(
                """insert into blog_entity_links
                     (blog_id, entity_type, entity_slug, entity_id, confidence,
                      extraction_method, mention_text, year)
                   values (%s,%s,%s,%s,%s,'exact-alias',%s,%s)
                   on conflict (blog_id, entity_type, entity_slug) do nothing""",
                (blog_id, e["entity_type"], e["entity_slug"], e["entity_id"],
                 e["confidence"], e["mention_text"], e["year"]))
    return len(edges)


async def blog_link_counts() -> list[dict]:
    """Edge count per entity_type (for `blog --report`)."""
    p = await pool()
    async with p.connection() as conn:
        cur = await conn.execute(
            "select entity_type, count(*) n from blog_entity_links "
            "group by entity_type order by n desc")
        return list(await cur.fetchall())


# --- person layer (milestone B) ---------------------------------------------
async def people_source_recognitions() -> list[dict]:
    """Individual-award recognitions + employer org — input to person extraction."""
    from stevie_platform.canonical.people_extract import INDIVIDUAL_AWARD_RX
    p = await pool()
    async with p.connection() as conn:
        cur = await conn.execute(
            """select r.id as rec_id, r.nomination_title, pa.organization_id as org_id
               from recognitions r
               join category_definitions cd on cd.id = r.category_definition_id
               left join parties pa on pa.id = r.recipient_party_id
               where cd.name ~* %s and r.nomination_title is not null
                 and length(trim(r.nomination_title)) > 0""",
            (INDIVIDUAL_AWARD_RX,))
        return list(await cur.fetchall())


async def clear_person_layer() -> None:
    """Regenerable: drop edges then people (people has no other referrers yet)."""
    p = await pool()
    async with p.connection() as conn:
        await conn.execute("delete from recognition_people")
        await conn.execute("delete from people")


async def insert_people(people: list[dict]) -> dict:
    """Bulk-insert people; return {norm_key: id}."""
    if not people:
        return {}
    p = await pool()
    async with p.connection() as conn:
        async with conn.cursor() as cur:
            await cur.executemany(
                "insert into people (norm_key, slug, name, org_id, title) "
                "values (%s,%s,%s,%s,%s)",
                [(pe["norm_key"], pe["slug"], pe["name"], pe["org_id"], pe["title"])
                 for pe in people])
        cur = await conn.execute("select id, norm_key from people")
        return {r["norm_key"]: r["id"] for r in await cur.fetchall()}


async def insert_recognition_people(links: list[tuple]) -> int:
    """links: (recognition_id, person_id, extracted_name, title, confidence)."""
    if not links:
        return 0
    p = await pool()
    async with p.connection() as conn:
        async with conn.cursor() as cur:
            await cur.executemany(
                "insert into recognition_people "
                "(recognition_id, person_id, extracted_name, title, confidence) "
                "values (%s,%s,%s,%s,%s) on conflict (recognition_id, person_id) do nothing",
                links)
    return len(links)


async def person_layer_report() -> dict:
    p = await pool()
    async with p.connection() as conn:
        np = (await (await conn.execute("select count(*) n from people")).fetchone())["n"]
        nl = (await (await conn.execute("select count(*) n from recognition_people")).fetchone())["n"]
        cur = await conn.execute(
            "select p.name, o.name org, count(*) n from recognition_people rp "
            "join people p on p.id = rp.person_id "
            "left join organizations o on o.id = p.org_id "
            "group by p.id, p.name, o.name order by n desc limit 12")
        return {"people": np, "links": nl, "top": list(await cur.fetchall())}


# --- evidence layer (crawler milestone) -------------------------------------
async def evidence_subjects(n_org: int = 20, n_person: int = 20) -> tuple[list, list]:
    """Curated notable subjects: top orgs + top people by recognition count."""
    p = await pool()
    async with p.connection() as conn:
        cur = await conn.execute(
            """select o.id, o.slug, o.name, count(distinct r.id) n
               from organizations o
               join parties pa on pa.organization_id = o.id
               join recognitions r on r.recipient_party_id = pa.id
               group by o.id, o.slug, o.name order by n desc limit %s""", (n_org,))
        orgs = list(await cur.fetchall())
        cur = await conn.execute(
            """select pe.id, pe.slug, pe.name, count(*) n
               from recognition_people rp join people pe on pe.id = rp.person_id
               group by pe.id, pe.slug, pe.name order by n desc limit %s""", (n_person,))
        people = list(await cur.fetchall())
    return orgs, people


async def evidence_exists(subject_type: str, subject_slug: str, url: str) -> bool:
    p = await pool()
    async with p.connection() as conn:
        cur = await conn.execute(
            "select 1 from winner_evidence where subject_type=%s and subject_slug=%s "
            "and source_url=%s limit 1", (subject_type, subject_slug, url))
        return (await cur.fetchone()) is not None


async def insert_winner_evidence(*, subject: dict, url: str, source_type: str | None,
                                 content: str, extracted: dict, discovery: str,
                                 extraction: str, raw_page_id: int | None,
                                 crawl_run_id: uuid.UUID | None,
                                 extractor_model: str | None = None,
                                 extractor_version: str | None = None,
                                 confidence: float = 0.4) -> None:
    """Store one evidence doc. External prior (0.4) below blog/winner trust.
    Records extraction provenance (model/version/extracted_at) for re-extraction."""
    p = await pool()
    async with p.connection() as conn:
        await conn.execute(
            """insert into winner_evidence
                 (subject_type, subject_slug, subject_id, source_url, source_type,
                  content, extracted, confidence, discovery_provider,
                  extraction_method, extractor_model, extractor_version,
                  extracted_at, raw_page_id, crawl_run_id)
               values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now(),%s,%s)
               on conflict (subject_type, subject_slug, source_url) do nothing""",
            (subject["subject_type"], subject["subject_slug"], subject.get("subject_id"),
             url, source_type, content, json.dumps(extracted or {}), confidence,
             discovery, extraction, extractor_model, extractor_version,
             raw_page_id, str(crawl_run_id) if crawl_run_id else None))


async def evidence_report() -> dict:
    p = await pool()
    async with p.connection() as conn:
        n = (await (await conn.execute("select count(*) n from winner_evidence")).fetchone())["n"]
        cur = await conn.execute(
            "select subject_type, count(*) n from winner_evidence "
            "group by subject_type order by n desc")
        return {"docs": n, "by_type": list(await cur.fetchall())}


# --- harvest_state ----------------------------------------------------------
async def get_done_harvest_pages() -> set[int]:
    """Listing pages already fully harvested — skipped on resume."""
    p = await pool()
    async with p.connection() as conn:
        cur = await conn.execute("select listing_page from harvest_state where status = 'done'")
        return {r["listing_page"] for r in await cur.fetchall()}


async def upsert_harvest_page(listing_page: int, status: str, *,
                              ids_found: int | None = None,
                              raw_page_id: int | None = None,
                              error: str | None = None) -> None:
    p = await pool()
    async with p.connection() as conn:
        await conn.execute(
            """insert into harvest_state (listing_page, status, ids_found, raw_page_id, last_error, updated_at)
               values (%s,%s,%s,%s,%s, now())
               on conflict (listing_page) do update set
                 status = excluded.status,
                 ids_found = coalesce(excluded.ids_found, harvest_state.ids_found),
                 raw_page_id = coalesce(excluded.raw_page_id, harvest_state.raw_page_id),
                 attempts = harvest_state.attempts + 1,
                 last_error = excluded.last_error,
                 updated_at = now()""",
            (listing_page, status, ids_found, raw_page_id, error),
        )


# --- fetch_queue ------------------------------------------------------------
async def enqueue_details(rows: list[tuple[str, str, int, int]]) -> None:
    """rows: (node_id, detail_url, discovered_on_page, position). Idempotent."""
    if not rows:
        return
    p = await pool()
    async with p.connection() as conn:
        async with conn.cursor() as cur:
            await cur.executemany(
                """insert into fetch_queue (node_id, detail_url, discovered_on_page, position)
                   values (%s,%s,%s,%s) on conflict (node_id) do nothing""",
                rows,
            )


async def claim_pending(limit: int) -> list[dict]:
    """Atomically claim up to `limit` pending node ids for fetching."""
    p = await pool()
    async with p.connection() as conn:
        cur = await conn.execute(
            """update fetch_queue set status = 'fetching', claimed_at = now(),
                   attempts = attempts + 1, updated_at = now()
               where node_id in (
                   select node_id from fetch_queue
                   where status = 'pending' order by node_id
                   for update skip locked limit %s)
               returning node_id, detail_url""",
            (limit,),
        )
        return await cur.fetchall()


async def mark_fetched(node_id: str, raw_page_id: int) -> None:
    p = await pool()
    async with p.connection() as conn:
        await conn.execute(
            "update fetch_queue set status='done', raw_page_id=%s, last_error=null, updated_at=now() where node_id=%s",
            (raw_page_id, node_id),
        )


async def mark_failed(node_id: str, error: str, max_attempts: int) -> None:
    """Back to 'pending' for retry, or 'failed' once attempts are exhausted."""
    p = await pool()
    async with p.connection() as conn:
        await conn.execute(
            """update fetch_queue
               set status = case when attempts >= %s then 'failed' else 'pending' end,
                   last_error = %s, updated_at = now()
               where node_id = %s""",
            (max_attempts, error, node_id),
        )


async def requeue_stale_fetching() -> int:
    """Return rows stranded in 'fetching' by a killed fetch back to 'pending'.
    Safe under sequential mode: the advisory lock guarantees no other fetch is
    live, so any 'fetching' row at startup is from a dead run."""
    p = await pool()
    async with p.connection() as conn:
        cur = await conn.execute(
            "update fetch_queue set status='pending', claimed_at=null where status='fetching'"
        )
        return cur.rowcount


async def count_pending() -> int:
    p = await pool()
    async with p.connection() as conn:
        cur = await conn.execute("select count(*) as n from fetch_queue where status = 'pending'")
        return (await cur.fetchone())["n"]


# --- parsed_records (state 2) ----------------------------------------------
async def save_parsed(raw_page_id: int, parser_version: str, node_id: str,
                      data: dict, is_complete: bool) -> None:
    p = await pool()
    async with p.connection() as conn:
        await conn.execute(
            """insert into parsed_records (raw_page_id, parser_version, node_id, data, is_complete)
               values (%s,%s,%s,%s,%s)
               on conflict (raw_page_id, parser_version) do update set
                 data = excluded.data, is_complete = excluded.is_complete, parsed_at = now()""",
            (raw_page_id, parser_version, node_id, json.dumps(data), is_complete),
        )


async def truncate_parsed() -> None:
    """Drop all parsed records — they are fully regenerable from raw_pages.
    CASCADE because the canonical layer (recognitions, …) FK-references this
    table; it is regenerable too and gets rebuilt by `canonicalize`."""
    p = await pool()
    async with p.connection() as conn:
        await conn.execute("truncate parsed_records restart identity cascade")
