-- Stevie Platform — Phase 1 schema (data acquisition).
--
-- Four states, but Phase 1 only materializes the first two plus the crawl
-- bookkeeping. Canonical entities (state 3) and published pages (state 4) land
-- in later migrations, on top of this — they never require a re-crawl.
--
--   raw_pages      (state 1) — immutable archive. Written once, never updated.
--   parsed_records (state 2) — f(raw_pages, parser_version). Fully regenerable.
--   fetch_queue / harvest_state — resumable crawl bookkeeping.
--
-- Invariant that makes "rebuild from scratch" true: the ONLY tables a human or
-- crawler writes to are raw_pages, fetch_queue, harvest_state. parsed_records
-- is always derivable from raw_pages — truncate and replay any time.

create extension if not exists "uuid-ossp";

-- ---------------------------------------------------------------------------
-- HISTORY — every harvest/fetch/parse/canonicalize run. The provenance spine:
-- raw_pages, recognitions and entity_links all carry a crawl_run_id, so six
-- months from now you can point at the exact run that produced any row — and
-- which parser/code version it used. Invaluable when the Stevie site changes.
-- ---------------------------------------------------------------------------
create table if not exists crawl_runs (
    id             uuid primary key default uuid_generate_v4(),
    kind           text not null check (kind in ('harvest','fetch','parse','canonicalize')),
    started_at     timestamptz not null default now(),
    finished_at    timestamptz,
    git_commit     text,
    parser_version text,
    notes          text,
    stats          jsonb
);

-- ---------------------------------------------------------------------------
-- STATE 1 — raw archive. Immutable. Compressed HTML + provenance.
-- ---------------------------------------------------------------------------
create table if not exists raw_pages (
    id           bigserial primary key,
    url          text        not null,
    page_type    text        not null check (page_type in ('listing', 'detail')),
    node_id      text,                    -- detail pages only
    listing_page int,                     -- listing pages only (Drupal 0-indexed)
    html         bytea       not null,    -- gzip-compressed response body
    http_status  int         not null,
    checksum     text        not null,    -- sha256(raw html) — dedup + change detection
    headers      jsonb,
    crawl_run_id uuid        not null,
    fetched_at   timestamptz not null default now()
);
-- Same URL + identical content == same row. A changed page gets a new row,
-- preserving history (compare parser/site versions without re-crawling).
create unique index if not exists raw_pages_url_checksum_uidx on raw_pages (url, checksum);
create index if not exists raw_pages_node_id_idx   on raw_pages (node_id);
create index if not exists raw_pages_page_type_idx  on raw_pages (page_type);

-- ---------------------------------------------------------------------------
-- Crawl bookkeeping — resumable discovery + fetch queue.
-- ---------------------------------------------------------------------------

-- Listing harvest: one row per Drupal listing page (0-indexed).
create table if not exists harvest_state (
    listing_page int  primary key,
    status       text not null default 'pending'
                 check (status in ('pending', 'harvesting', 'done', 'failed')),
    ids_found    int,
    raw_page_id  bigint references raw_pages (id),
    attempts     int  not null default 0,
    last_error   text,
    updated_at   timestamptz not null default now()
);

-- Detail fetch queue: one row per discovered nomination (node id).
create table if not exists fetch_queue (
    node_id            text primary key,
    detail_url         text not null,
    discovered_on_page int,
    position           int,
    status             text not null default 'pending'
                       check (status in ('pending', 'fetching', 'done', 'failed')),
    attempts           int  not null default 0,
    last_error         text,
    raw_page_id        bigint references raw_pages (id),
    claimed_at         timestamptz,
    updated_at         timestamptz not null default now()
);
create index if not exists fetch_queue_status_idx on fetch_queue (status);

-- ---------------------------------------------------------------------------
-- STATE 2 — parsed records. Regenerable. 1:1 with a raw detail page + parser.
-- ---------------------------------------------------------------------------
create table if not exists parsed_records (
    id             bigserial primary key,
    raw_page_id    bigint not null references raw_pages (id),
    parser_version text   not null,
    node_id        text   not null,
    data           jsonb  not null,        -- the structured 13-field record
    is_complete    boolean not null,       -- passed REQUIRED_FIELDS validation
    parsed_at      timestamptz not null default now(),
    -- reparse with a new parser_version = new row; old kept for diffing.
    unique (raw_page_id, parser_version)
);
create index if not exists parsed_records_node_id_idx on parsed_records (node_id);
create index if not exists parsed_records_complete_idx on parsed_records (is_complete);

-- ---------------------------------------------------------------------------
-- Run metadata + completeness reporting.
-- ---------------------------------------------------------------------------
create table if not exists meta (
    key        text primary key,
    value      jsonb not null,
    updated_at timestamptz not null default now()
);

-- Single source of truth for the dashboard / verification gate.
create or replace view acquisition_status as
select
    (select (value->>'total')::int from meta where key = 'reported_total')        as reported_total,
    (select count(*) from fetch_queue)                                            as discovered,
    (select count(*) from fetch_queue where status = 'done')                      as fetched,
    (select count(*) from fetch_queue where status = 'failed')                    as failed,
    (select count(*) from fetch_queue where status = 'pending')                   as pending,
    (select count(*) from parsed_records where parser_version =
        (select value->>'version' from meta where key = 'parser_version'))        as parsed,
    (select count(*) from parsed_records where is_complete = false and parser_version =
        (select value->>'version' from meta where key = 'parser_version'))        as parsed_incomplete;
