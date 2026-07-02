-- Migration 016 — fact confidence (M7).
--
-- A DERIVED, regenerable trust score per canonical entity, grounded in the
-- signals that are actually populated in entity_links (see experiments/
-- M7_PLAN.md §0): match provenance is near-binary (exact/new) and match_score/
-- model_version/reviewed_by are empty, so confidence is driven by CORROBORATION
-- (how many recognitions reference the entity) plus entity type (controlled
-- vocabulary vs free-text org/person).
--
-- Grain = one row per canonical entity (entity_type, entity_id). Truncatable and
-- fully recomputable by `stevie confidence` from entity_links; nothing upstream
-- depends on it (additive — no ingestion/canonicalize change). Every score
-- carries `reasons` so it is explainable, never an opaque number (M7 principle).

create table if not exists fact_confidence (
    entity_type text        not null,
    entity_id   bigint      not null,
    score       numeric     not null check (score >= 0 and score <= 1),
    reasons     jsonb       not null,   -- ["exact ...","corroborated by N ...",...]
    rec_count   int         not null,   -- corroboration: recognitions referencing this entity
    computed_at timestamptz not null default now(),
    primary key (entity_type, entity_id)
);
create index if not exists fact_confidence_score_idx on fact_confidence (score);

comment on table fact_confidence is
    'Derived per-entity trust score (M7), regenerable from entity_links by '
    '`stevie confidence`. score in [0,1] with human-readable reasons; driven by '
    'corroboration + entity type because richer provenance (fuzzy/model/review) '
    'is unpopulated. See experiments/M7_PLAN.md.';
