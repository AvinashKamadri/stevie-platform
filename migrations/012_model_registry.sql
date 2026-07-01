-- Migration 012 — model registry (M5.3).
--
-- One durable row per trained model version: what it was trained on, how big,
-- where its artifact lives, and (once M5.5 runs) its frozen evaluation metrics.
-- The pickled artifact itself is a build product (regenerable by re-running
-- training against the same versions with a fixed random_state) and is NOT
-- committed to git (see .gitignore); this row is the reproducible record of
-- what produced it.
--
-- model_version is the primary key and is never overwritten once a model has a
-- frozen evaluation: a later change is a NEW model_version, exactly like
-- feature_version and split_version — otherwise "model v2 beat v1" could mean
-- "v1's row quietly changed underneath the comparison".

create table if not exists model_registry (
    model_version         text primary key,
    feature_version       text not null,
    split_version         text not null,
    algorithm             text not null,
    training_timestamp    timestamptz not null default now(),
    training_sample_size  int not null,
    -- {"train": {"merge": n, "distinct": n}, "calibration": {...}, "evaluation": {...}}
    -- (partition x label counts across the WHOLE labeled gold set, not just the
    -- rows actually fit — includes 'related' counts, reported not modeled).
    class_counts          jsonb not null,
    artifact_path         text not null,
    -- {feature_name: coefficient}, sorted by |coefficient| desc at write time —
    -- kept alongside the pickle so coefficients are inspectable without
    -- unpickling or reloading sklearn.
    coefficients          jsonb not null,
    -- Filled in once by M5.5's single frozen evaluation run; null until then.
    -- A non-null value marks this model_version as FROZEN — run_train() refuses
    -- to overwrite a frozen version (retrain as a new model_version instead).
    metrics_summary       jsonb
);

comment on table model_registry is
    'One row per trained model version: training provenance, artifact location, '
    'coefficients, and (once frozen) evaluation metrics. metrics_summary is null '
    'until the M5.5 frozen evaluation run; a non-null value locks the version.';
