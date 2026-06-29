-- PROPOSED migration 009 — org merge graph (Phase E / M0).
--
-- NOT in migrations/ on purpose: this is a design artifact. Do not apply until
-- M0 is reviewed and promoted. When promoted, move to migrations/009_merge_graph.sql.
--
-- Model: a DIRECTED merge graph, replayed during canonicalize AFTER deterministic
-- normalization. Decisions are a durable INPUT (never truncated); aliases are a
-- derived PROJECTION (rebuilt with canonical). See M0_DECISION_STORE_DESIGN.md.

-- ---------------------------------------------------------------------------
-- INPUT: durable human/deterministic merge decisions. Keyed by norm_key (the
-- only rebuild-stable org identity). Excluded from truncate_canonical.
-- ---------------------------------------------------------------------------
create table if not exists organization_merge_decision (
    id            bigserial primary key,
    loser_key     text not null,          -- norm_key folded away
    winner_key    text not null,          -- surviving brand's norm_key
    decision      text not null check (decision in ('merge','distinct')),
    source        text not null check (source in ('manual','deterministic')),
    confidence    numeric,                -- score at decision time (provenance)
    reason        text,                   -- human / feature note
    loser_witness text,                   -- representative raw_name at decision time
    reviewed_by   text not null,
    reviewed_at   timestamptz not null default now(),
    check (loser_key <> winner_key),
    unique (loser_key)                    -- one fate per losing key
);
create index if not exists omd_winner_idx on organization_merge_decision (winner_key);
create index if not exists omd_decision_idx on organization_merge_decision (decision);

-- ---------------------------------------------------------------------------
-- DERIVED: retired key/slug -> surviving org, so external consumers' stable
-- keys never 404 after a merge. Rebuilt during canonicalize; safe to truncate.
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

-- NOTE for the promotion PR (not part of this DDL):
--   * truncate_canonical must add organization_alias but NOT
--     organization_merge_decision.
--   * a data-quality gate must flag orphaned loser_key/winner_key (rule drift).
