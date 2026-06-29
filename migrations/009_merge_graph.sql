-- Migration 009 — org merge graph (Phase E / M3).
--
-- Two tables:
--   organization_merge_decision  INPUT:  durable human decisions, never truncated.
--   organization_alias           DERIVED: retired key -> canonical org, rebuilt with canonical.
--
-- Design: experiments/entity_resolution/M0_DECISION_STORE_DESIGN.md

-- ---------------------------------------------------------------------------
-- INPUT: durable merge decisions. Keyed by norm_key (rebuild-stable identity).
-- Excluded from truncate_canonical. Treated as an external input like overrides.
-- ---------------------------------------------------------------------------
create table if not exists organization_merge_decision (
    id            bigserial primary key,
    loser_key     text not null,          -- norm_key folded away
    winner_key    text not null,          -- surviving brand's norm_key
    decision      text not null check (decision in ('merge','distinct')),
    source        text not null check (source in ('manual','deterministic')),
    confidence    numeric,                -- score at decision time (provenance)
    reason        text,
    loser_witness text,                   -- representative raw_name at decision time
    reviewed_by   text not null,
    reviewed_at   timestamptz not null default now(),
    check (loser_key <> winner_key),
    unique (loser_key)                    -- one fate per losing key
);
create index if not exists omd_winner_idx   on organization_merge_decision (winner_key);
create index if not exists omd_decision_idx on organization_merge_decision (decision);

comment on table organization_merge_decision is
    'Durable merge decisions (input, never truncated). Replayed during canonicalize '
    'to resolve org identity. Keyed by norm_key — stable across rebuilds.';

-- ---------------------------------------------------------------------------
-- DERIVED: retired alias -> surviving org. Safe to truncate with canonical.
-- Lets external consumers'' stable keys redirect after a merge.
-- ---------------------------------------------------------------------------
create table if not exists organization_alias (
    id              bigserial primary key,
    alias_norm_key  text not null,
    alias_slug      text,
    organization_id bigint not null references organizations (id),
    via             text not null check (via in ('merge_decision','deterministic')),
    unique (alias_norm_key)
);
create index if not exists org_alias_org_idx on organization_alias (organization_id);

comment on table organization_alias is
    'Derived: retired norm_key -> canonical org id. Rebuilt during canonicalize. '
    'Safe to truncate. Source of truth is organization_merge_decision.';
