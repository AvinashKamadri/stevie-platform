-- Migration 020 — person layer (ADDITIVE, non-destructive).
--
-- Individual-award winners' names live unstructured in recognitions.nomination_
-- title; the canonical model attributed those recognitions to the ORG. This layer
-- recovers the people WITHOUT mutating recognitions / parties / recipient
-- attribution: it fills the stubbed `people` table (from 002) and adds a
-- person<->recognition edge table. Fully regenerable (truncate-and-rebuild by
-- `stevie people`), mirroring fact_confidence (M7) and the blog layer.

create table if not exists recognition_people (
    id                bigserial primary key,
    recognition_id    bigint  not null references recognitions (id) on delete cascade,
    person_id         bigint  not null references people (id) on delete cascade,
    role              text    not null default 'honoree',
    extracted_name    text    not null,   -- name as parsed from the nomination
    title             text,               -- role/title parsed alongside (e.g. "CEO")
    confidence        numeric not null check (confidence >= 0 and confidence <= 1),
    extraction_method text    not null default 'heuristic-v1',
    created_at        timestamptz not null default now(),
    unique (recognition_id, person_id)
);
create index if not exists recognition_people_person_idx on recognition_people (person_id);
create index if not exists recognition_people_recog_idx  on recognition_people (recognition_id);

-- person extraction is its own provenance kind.
alter table crawl_runs drop constraint if exists crawl_runs_kind_check;
alter table crawl_runs add  constraint crawl_runs_kind_check
    check (kind in ('harvest', 'fetch', 'parse', 'canonicalize',
                    'blog_fetch', 'blog_extract', 'blog_link', 'people'));

comment on table recognition_people is
    'Additive person<->recognition edges (milestone B). People recovered from '
    'individual-award nomination titles; org attribution on recognitions is '
    'left untouched. Regenerable by `stevie people`.';
