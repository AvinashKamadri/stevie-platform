-- Migration 021 — Evidence Layer scaffold (winner_evidence).
--
-- External PUBLIC evidence about a canonical subject (organization or person),
-- ENRICH-NOT-DEFINE: it references a subject by stable slug, never defines one,
-- and carries a lower trust prior than authoritative Stevie records. Acquired via
-- pluggable discovery (official search APIs — NO SERP/LinkedIn scraping) + a
-- pluggable fetcher (httpx today, behind a stable seam) + pluggable extraction
-- (LLM). Additive & regenerable; touches nothing in the org/person/blog layers.

create table if not exists winner_evidence (
    id             bigserial primary key,
    subject_type   text    not null,         -- organization | person
    subject_slug   text    not null,         -- stable key into organizations/people
    subject_id     bigint,                   -- convenience cache; NOT source of truth
    source_url     text    not null,
    source_type    text,                     -- company_site|press_release|news|interview|case_study|profile|pdf|user
    title          text,
    published_at   date,
    content        text,                     -- extracted clean text
    extracted      jsonb,                    -- {themes:[],metrics:[],quotes:[],sentiment:...} (LLM)
    confidence     numeric check (confidence >= 0 and confidence <= 1),
    discovery_provider text,                 -- which search provider surfaced it
    extraction_method  text,                 -- which extractor produced `extracted`
    raw_page_id    bigint references raw_pages (id),
    crawl_run_id   uuid   references crawl_runs (id),
    fetched_at     timestamptz not null default now(),
    unique (subject_type, subject_slug, source_url)
);
create index if not exists winner_evidence_subject_idx on winner_evidence (subject_type, subject_slug);

-- SINGLE OWNER (with the block below) of the two enumerated constraints that
-- grow over time. This is the latest migration that adds a page_type / kind, so
-- it re-declares each with the COMPLETE list. 018/020 intentionally no longer
-- ALTER them (see notes there); `stevie migrate` re-runs every file, so having
-- one owner as the last file is the only re-run-safe design. A future migration
-- that adds a value must move these blocks to itself with the new list.
alter table raw_pages drop constraint if exists raw_pages_page_type_check;
alter table raw_pages add  constraint raw_pages_page_type_check
    check (page_type in ('listing', 'detail', 'blog', 'evidence'));


alter table crawl_runs drop constraint if exists crawl_runs_kind_check;
alter table crawl_runs add  constraint crawl_runs_kind_check
    check (kind in ('harvest', 'fetch', 'parse', 'canonicalize',
                    'blog_fetch', 'blog_extract', 'blog_link', 'people', 'evidence'));

comment on table winner_evidence is
    'Evidence Layer (crawler milestone): external public evidence about a '
    'canonical org/person subject. Enrich-not-define, lower trust prior. '
    'Discovery/fetch/extraction are pluggable behind stable seams.';
