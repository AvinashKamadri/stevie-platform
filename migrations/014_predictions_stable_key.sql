-- Migration 014 — re-key model_predictions by norm_key pair (Phase 3 prep).
--
-- model_predictions was keyed by candidate_id, which is NOT stable:
-- candidates.persist() truncates organization_merge_candidate and reinserts
-- with restart identity every time `cli candidates` runs, and (since migration
-- 011) that truncate CASCADEs, wiping every prediction along with it. That
-- directly conflicts with incremental scoring ("leave existing predictions
-- untouched" — a new scrape must not force rescoring the whole candidate
-- population) and with a human-review workflow (a reviewer's queue would be
-- silently invalidated by any subsequent candidate regeneration).
--
-- Fix: identity moves to (left_key, right_key, model_version) — the same
-- rebuild-stable norm_key-pair identity organization_merge_candidate,
-- organization_merge_decision, and every gold corpus already use, for exactly
-- this reason. candidate_id becomes a nullable convenience pointer (ON DELETE
-- SET NULL, not CASCADE) — useful for joining back to the current
-- generation's reasons/features when it still resolves, but its absence after
-- a regeneration no longer destroys the prediction.

alter table model_predictions add column if not exists left_key text;
alter table model_predictions add column if not exists right_key text;

update model_predictions mp
   set left_key = omc.left_key, right_key = omc.right_key
  from organization_merge_candidate omc
 where omc.id = mp.candidate_id and mp.left_key is null;

alter table model_predictions alter column left_key set not null;
alter table model_predictions alter column right_key set not null;

alter table model_predictions drop constraint if exists model_predictions_candidate_id_model_version_key;
alter table model_predictions drop constraint if exists model_predictions_pair_version_key;
alter table model_predictions add constraint model_predictions_pair_version_key
    unique (left_key, right_key, model_version);

alter table model_predictions drop constraint if exists model_predictions_candidate_id_fkey;
alter table model_predictions alter column candidate_id drop not null;
alter table model_predictions add constraint model_predictions_candidate_id_fkey
    foreign key (candidate_id) references organization_merge_candidate (id) on delete set null;

create index if not exists model_predictions_pair_idx on model_predictions (left_key, right_key);

comment on table model_predictions is
    'Model outputs, one row per (left_key, right_key, model_version) — the '
    'rebuild-stable norm_key-pair identity, NOT candidate_id (which churns on '
    'every candidate regeneration). Mutable while the model_version is unfrozen '
    '(model_registry.metrics_summary null); frozen versions are never touched '
    'again. candidate_id is a nullable convenience pointer into the CURRENT '
    'candidate generation (SET NULL, not CASCADE, on regeneration) — a row '
    'survives even after its candidate_id goes stale.';
