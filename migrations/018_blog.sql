-- Migration 018 — blog corpus + knowledge-graph edges (Phase 1 close-out).
--
-- The blog ENRICHES the graph; it never DEFINES it. blog_entity_links only
-- REFERENCE canonical entities (by their STABLE slug / node_id) — a mention
-- resolves to an EXISTING entity or it does not link. No blog row can mint or
-- override a canonical org/person/category/etc; winner facts come solely from
-- recognitions. Blog-derived trust is scored BELOW winner-record trust.
--
-- Scope (2026-07-08): blog.stevieawards.com only (HubSpot), English posts only.
-- Program subdomains (aba/asia/tech/hr/mena/gsa/sascs) are deferred to Phase 3
-- evidence sources. Language is per-post (NOT in the URL — the sitemap has ~1,722
-- /blog/<slug> posts across many languages), so the English gate lives in
-- extraction and the detected language is recorded in blog_posts.lang.
--
-- Additive & re-runnable (stevie migrate re-applies every *.sql in order):
-- `if not exists` on tables/indexes; the page_type CHECK is drop-if-exists+add.

-- NOTE: raw_pages_page_type_check AND crawl_runs_kind_check are each owned by the
-- LATEST migration that adds a value (currently 021), which re-declares the full
-- list. Earlier migrations must NOT also ALTER them: `stevie migrate` re-runs
-- every file in order, so a narrower re-add here runs BEFORE the owner and
-- rejects rows of a later value (a 'blog'/'evidence' page, a 'people'/'evidence'
-- crawl_run). 001 sets the initial inline CHECKs via CREATE TABLE IF NOT EXISTS,
-- which is skipped on re-run; the explicit ALTERs below were the re-run hazard.

-- blog_posts — the FIRST base table not derived from parsed_records. One row per
-- extracted post. url/slug are the STABLE natural keys (external refs never use
-- the surrogate id, which is regenerated on a full rebuild). Provenance via
-- raw_page_id + crawl_run_id, consistent with recognitions/entity_links.
create table if not exists blog_posts (
    id           bigserial   primary key,
    url          text        not null unique,
    slug         text        not null unique,
    title        text,
    author       text,
    published_at date,
    lang         text,                        -- detected at extract; English-only gate
    clean_text   text,                         -- extracted article body
    raw_page_id  bigint      references raw_pages (id),
    crawl_run_id uuid        references crawl_runs (id),
    fetched_at   timestamptz not null default now()
);
create index if not exists blog_posts_lang_idx      on blog_posts (lang);
create index if not exists blog_posts_published_idx on blog_posts (published_at);

-- blog_entity_links — graph edges from a post to canonical entities. Polymorphic
-- on (entity_type, entity_slug), mirroring entity_links/overrides. Keyed on the
-- STABLE slug (or recognitions.node_id when entity_type='recognition') so edges
-- survive a canonical rebuild; entity_id is a NULLABLE convenience cache only,
-- never the source of truth. No FK is possible on the polymorphic pair (same
-- tradeoff entity_links already accepts) — the linker enforces "entity must
-- already exist" in code, which is the mechanism behind enrich-not-define.
create table if not exists blog_entity_links (
    id                bigserial primary key,
    blog_id           bigint  not null references blog_posts (id) on delete cascade,
    entity_type       text    not null,     -- organization|person|program|program_edition|
                                            -- category|category_definition|country|industry|recognition
    entity_slug       text    not null,     -- stable key (slug, or node_id for recognition)
    entity_id         bigint,               -- convenience cache; NOT source of truth
    confidence        numeric not null check (confidence >= 0 and confidence <= 1),
    extraction_method text    not null,     -- exact-alias|fuzzy|llm|manual
    mention_text      text,                 -- surface form found in the article
    year              int,                  -- scalar (year is not an entity)
    created_at        timestamptz not null default now(),
    unique (blog_id, entity_type, entity_slug)
);
create index if not exists blog_entity_links_entity_idx on blog_entity_links (entity_type, entity_slug);
create index if not exists blog_entity_links_blog_idx   on blog_entity_links (blog_id);

comment on table blog_posts is
    'Stevie blog corpus (blog.stevieawards.com, HubSpot), Phase 1. First base '
    'table not derived from parsed_records. English-only via detected lang.';
comment on table blog_entity_links is
    'Graph edges: blog post -> EXISTING canonical entity, keyed on stable '
    'slug/node_id. Enrich-not-define: references only, never creates/overrides. '
    'Blog-derived confidence sits below winner-record confidence (M7).';
