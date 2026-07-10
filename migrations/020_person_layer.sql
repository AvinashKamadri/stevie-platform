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

-- NOTE: the 'people' crawl_runs kind is declared by the single owner of
-- crawl_runs_kind_check -- the latest kind-adding migration (021, full list).
-- 020 must NOT ALTER the constraint itself (see the note in 018): migrate
-- re-runs every file, and a people-only re-add here would run before 021 and
-- reject an existing 'evidence' crawl_run.

comment on table recognition_people is
    'Additive person<->recognition edges (milestone B). People recovered from '
    'individual-award nomination titles; org attribution on recognitions is '
    'left untouched. Regenerable by `stevie people`.';
