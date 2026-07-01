-- Migration 011 — model predictions (M5).
--
-- Model outputs live SEPARATELY from the (immutable) feature store, so a
-- retrain, recalibration, or model-version comparison never touches
-- organization_merge_candidate.
--
-- Mutability: a row is free to be UPDATED (upsert on candidate_id+model_version)
-- while its model_version is still unfrozen — training then calibrating the
-- same version is expected to supersede the row with a better probability (raw
-- -> Platt-calibrated). Once model_registry.metrics_summary is set for that
-- version (the M5.5 frozen evaluation has run), scorer.run_train and
-- calibration.run_calibrate both refuse to touch it again — a genuinely new
-- result is a NEW model_version, so a past evaluation stays reproducible.
--
-- candidate_id FKs into organization_merge_candidate(id), which is NOT stable
-- across candidate regeneration (candidates.persist() truncates + restarts
-- identity). A prediction is only meaningful against the generation that
-- produced its candidate row, so this table cascades on that truncate (see
-- candidates.persist()) — regenerating candidates correctly invalidates
-- predictions made against the old generation.
--
-- feature_snapshot duplicates (rather than references) the feature vector used
-- for this prediction, so "why did model v1 score this pair 0.992?" stays
-- answerable even after organization_merge_candidate.features has moved on to a
-- later feature_version — a record of what the model actually saw, not a live
-- pointer that can drift out from under it.

create table if not exists model_predictions (
    id               bigserial primary key,
    candidate_id     bigint not null references organization_merge_candidate (id) on delete cascade,
    model_version    text not null,
    feature_version  text not null,
    probability      numeric not null check (probability >= 0 and probability <= 1),
    -- v1 is a binary merge/no-merge classifier — 'related' is a gold-labeled
    -- REPORTING class (see canonical/split.py), not something the model predicts.
    predicted_label  text not null check (predicted_label in ('merge', 'distinct')),
    feature_snapshot jsonb not null,
    created_at       timestamptz not null default now(),
    -- One prediction per (candidate, model_version) — retraining is a new
    -- model_version, not an update to an existing row.
    unique (candidate_id, model_version)
);
create index if not exists model_predictions_candidate_idx on model_predictions (candidate_id);
create index if not exists model_predictions_version_idx   on model_predictions (model_version);

comment on table model_predictions is
    'Model outputs, one row per (candidate, model_version). Mutable while the '
    'model_version is unfrozen (model_registry.metrics_summary null) — training '
    'then calibrating the same version supersedes the row. Frozen (metrics_summary '
    'set) versions are never touched again; a new result is a new model_version. '
    'Cascades on organization_merge_candidate regeneration (candidate_id is only '
    'valid within one generation).';
comment on column model_predictions.feature_snapshot is
    'The exact feature vector used for this prediction, frozen at prediction '
    'time — answers "why did this model think X?" even after feature_version advances.';
