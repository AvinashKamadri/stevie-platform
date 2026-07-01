-- Migration 013 — calibration metadata on model_registry (M5.4).
--
-- Platt-scaling parameters + the calibration split's own Brier score, kept
-- alongside the pickled calibrator (in the same artifact_path joblib) so the
-- fit is inspectable without unpickling. This is a DIAGNOSTIC number (the
-- calibration split evaluating its own fit is somewhat optimistic); the real
-- calibration quality is measured on `evaluation` and stored in
-- metrics_summary by run_evaluate (M5.5).

alter table model_registry add column if not exists calibration jsonb;

comment on column model_registry.calibration is
    'Platt-scaling fit: {"method","a","b","brier_score","n_calibration"}. Null '
    'until `cli calibrate` runs. The pickled calibrator itself lives inside the '
    'same artifact_path joblib as the base scaler+classifier.';
