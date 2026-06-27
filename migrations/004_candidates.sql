-- Stevie Platform — entity_candidates: POSSIBLE matches (vs entity_links =
-- RESOLVED). Phase C of the canonicalizer writes here and stops — it never
-- merges. Resolution (Phase D) happens later, against the real dataset, by
-- flipping `accepted`. Keeping candidates OUT of canonical means we can improve
-- algorithms, compare versions, and review by hand without ever touching truth.
create table if not exists entity_candidates (
    id                  bigserial primary key,
    parsed_record_id    bigint references parsed_records (id),
    entity_type         text not null,        -- 'organization' (extensible)
    raw_value           text not null,        -- the source string being placed
    candidate_entity_id bigint not null,      -- an existing entity it might equal
    score               numeric not null,
    algorithm           text not null,        -- 'trgm', 'embedding', …
    crawl_run_id        uuid references crawl_runs (id),
    accepted            boolean,              -- null = unreviewed; t/f = decision
    reviewed_by         text,
    reviewed_at         timestamptz,
    created_at          timestamptz not null default now()
);
create index if not exists entity_candidates_type_idx    on entity_candidates (entity_type);
create index if not exists entity_candidates_pending_idx on entity_candidates (accepted) where accepted is null;
create index if not exists entity_candidates_cand_idx    on entity_candidates (candidate_entity_id);
