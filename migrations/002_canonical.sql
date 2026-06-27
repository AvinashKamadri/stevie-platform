-- Stevie Platform — STATE 3 (canonical) + editorial overrides.
--
-- Source of truth: a PROJECTION over parsed_records, produced by the
-- canonicalizer (entity resolution). Pure structured data — NO embeddings/LLM.
-- Grain = one *recognition* (one nomination), with free-text dimensions
-- resolved to deduplicated entities and every decision logged in entity_links.
--
-- External consumers (CloudCannon, assistant) reference STABLE NATURAL KEYS —
-- recognitions.node_id and each entity's `slug` — never the surrogate id, which
-- is regenerated on a full rebuild. norm_key (lower/unaccent/strip ®™/collapse)
-- is computed in Python (testable); UNIQUE enforces it.

create extension if not exists pg_trgm;
create extension if not exists unaccent;

-- ===========================================================================
-- Controlled dimensions.
-- ===========================================================================
create table if not exists countries (
    id bigserial primary key, norm_key text not null unique,
    slug text not null unique, name text not null);

create table if not exists industries (
    id bigserial primary key, norm_key text not null unique,
    slug text not null unique, name text not null);

-- Programs are umbrellas; EDITIONS are the events. Deadlines, rules, fees,
-- judges and the category list belong to an edition, not the program. (#2)
create table if not exists programs (
    id bigserial primary key, norm_key text not null unique,
    slug text not null unique, name text not null);

create table if not exists program_editions (
    id            bigserial primary key,
    program_id    bigint not null references programs (id),
    year          int    not null,
    slug          text   not null unique,   -- e.g. international-business-awards-2024
    entry_deadline date,                     -- future: rules/fees/judges hang here
    notes         text,
    unique (program_id, year)
);

-- Categories EVOLVE: split, merge, rename. So separate the timeless lineage
-- (category_definitions) from the edition-scoped instance (categories). (#3)
-- "AI Customer Service / 2026" and "Customer Service AI / 2029" can be distinct
-- instances that later point at the same definition once a rename is confirmed.
create table if not exists category_definitions (
    id bigserial primary key, norm_key text not null unique,
    slug text not null unique, name text not null);

create table if not exists category_groups (
    id                bigserial primary key,
    program_edition_id bigint references program_editions (id),
    norm_key          text not null,
    slug              text not null unique,
    name              text not null,
    unique (program_edition_id, norm_key)
);

create table if not exists categories (
    id                 bigserial primary key,
    program_edition_id bigint references program_editions (id),
    category_group_id  bigint references category_groups (id),
    definition_id      bigint references category_definitions (id),  -- lineage
    norm_key           text not null,
    slug               text not null unique,
    name               text not null,
    unique (program_edition_id, norm_key)
);

-- ===========================================================================
-- Entities. Organizations have NO geography — country/industry belong to the
-- recognition (#3 / your answer 3). Exact match on norm_key + trgm fuzzy gen.
-- ===========================================================================
create table if not exists organizations (
    id bigserial primary key, norm_key text not null unique,
    slug text not null unique, name text not null,
    created_at timestamptz not null default now());
create index if not exists organizations_name_trgm on organizations using gin (name gin_trgm_ops);

create table if not exists people (
    id bigserial primary key, norm_key text not null unique,
    slug text not null unique, name text not null,
    org_id bigint references organizations (id), title text);

-- PARTIES — a thin supertype so any role (entrant/recipient/…) can be an org OR
-- a person (entrant can be a University, Person, agency, …). One party per
-- org/person; reused across all its recognitions and roles. (#1)
create table if not exists parties (
    id              bigserial primary key,
    kind            text not null check (kind in ('organization','person')),
    organization_id bigint references organizations (id),
    person_id       bigint references people (id),
    check ((kind='organization' and organization_id is not null and person_id is null)
        or (kind='person'       and person_id is not null and organization_id is null))
);
create unique index if not exists parties_org_uidx    on parties (organization_id) where organization_id is not null;
create unique index if not exists parties_person_uidx on parties (person_id)       where person_id is not null;

-- ===========================================================================
-- RECOGNITIONS — the central fact (1:1 with a parsed detail page).
-- Party roles are SEPARATED (#1): entrant ≠ submitter ≠ recipient. The
-- denormalized *_party_id columns below are a fast-path cache for the 99% case;
-- recognition_parties (below) is the authoritative, multi-party-capable truth.
-- ===========================================================================
create table if not exists recognitions (
    id               bigserial primary key,
    parsed_record_id bigint not null references parsed_records (id),
    node_id          text not null unique,            -- stable external key
    crawl_run_id     uuid references crawl_runs (id), -- provenance (#5)

    program_edition_id     bigint references program_editions (id),
    year                   int,                       -- denormalized from edition
    category_id            bigint references categories (id),
    category_group_id      bigint references category_groups (id),
    category_definition_id bigint references category_definitions (id),  -- cross-year
    country_id             bigint references countries (id),
    industry_id            bigint references industries (id),

    entrant_party_id   bigint references parties (id),  -- who entered
    submitter_party_id bigint references parties (id),  -- agency, if any
    recipient_party_id bigint references parties (id),  -- who won (= entrant 99%)

    result_level   text not null default 'other'
                   check (result_level in ('gold','silver','bronze','finalist','other')),
    award_raw      text,
    nomination_title text,
    city           text,
    state_province text,
    submitting_agency_raw text,
    notes          text,
    created_at     timestamptz not null default now()
);
create index if not exists recognitions_recipient_year_idx on recognitions (recipient_party_id, year);
create index if not exists recognitions_entrant_idx   on recognitions (entrant_party_id);
create index if not exists recognitions_catdef_year_idx on recognitions (category_definition_id, year);
create index if not exists recognitions_edition_idx   on recognitions (program_edition_id);
create index if not exists recognitions_country_idx   on recognitions (country_id);
create index if not exists recognitions_industry_idx  on recognitions (industry_id);
create index if not exists recognitions_year_idx      on recognitions (year);
create index if not exists recognitions_result_idx    on recognitions (result_level);

-- Authoritative party<->recognition link. Handles JOINT nominations,
-- partnerships, sponsors, and "one company, many roles" with NO duplicate
-- entities. The single *_party_id columns above are derived from this. (#1 + Q1)
create table if not exists recognition_parties (
    id             bigserial primary key,
    recognition_id bigint not null references recognitions (id) on delete cascade,
    party_id       bigint not null references parties (id),
    role           text not null
                   check (role in ('entrant','submitter','recipient','sponsor','partner','judge_employer')),
    raw_value      text,
    created_at     timestamptz not null default now(),
    unique (recognition_id, party_id, role)
);
create index if not exists recognition_parties_party_idx on recognition_parties (party_id, role);

-- An org's role set is DERIVED, not a separate base table (Q1: unified orgs,
-- many roles, no duplicates).
create or replace view organization_roles as
select distinct o.id as organization_id, o.slug, rp.role
from organizations o
join parties p on p.organization_id = o.id
join recognition_parties rp on rp.party_id = p.id;

-- ===========================================================================
-- MATCH LEDGER — why each mention resolved where it did. Expanded per (#4) into
-- a permanent audit trail: who/what reviewed it, with which model & parser.
-- ===========================================================================
create table if not exists entity_links (
    id               bigserial primary key,
    parsed_record_id bigint not null references parsed_records (id),
    crawl_run_id     uuid references crawl_runs (id),
    entity_type      text not null,   -- organization|person|program|program_edition|category|category_group|category_definition|country|industry
    entity_id        bigint not null,
    raw_value        text not null,
    match_method     text not null check (match_method in ('exact','fuzzy','manual','new')),
    match_score      numeric,
    model_version    text,            -- if a model proposed the match
    parser_version   text,            -- parser that produced the source record
    reviewed_by      text,            -- human sign-off, when corrected
    reviewed_at      timestamptz,
    created_at       timestamptz not null default now()
);
create index if not exists entity_links_entity_idx on entity_links (entity_type, entity_id);
create index if not exists entity_links_parsed_idx on entity_links (parsed_record_id);

-- ===========================================================================
-- EDITORIAL OVERRIDES — human knowledge as DATA, keyed to a stable slug so a
-- full rebuild stays a pure function of (raw_pages + overrides).
-- ===========================================================================
create table if not exists overrides (
    id          bigserial primary key,
    entity_type text not null,
    entity_slug text not null,
    field       text not null,
    value       jsonb not null,
    author      text,
    note        text,
    created_at  timestamptz not null default now(),
    unique (entity_type, entity_slug, field)
);
