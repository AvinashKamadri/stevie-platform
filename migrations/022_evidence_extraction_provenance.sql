-- Migration 022 — winner_evidence extraction provenance (versioning). Additive.
--
-- Records which model/version produced each `extracted` blob, so prompts or
-- models can improve later and rows be SELECTIVELY re-extracted (rather than
-- guessing which config produced which row). Also the data basis for a future
-- recrawl policy (refresh news ~30d, company pages ~90-180d, Wikipedia ~monthly,
-- press releases never) — `fetched_at`/`extracted_at` + source_type drive it.
-- (extraction_method already stores the provider name, e.g. 'claude'/'none'.)

alter table winner_evidence add column if not exists extractor_model   text;
alter table winner_evidence add column if not exists extractor_version text;
alter table winner_evidence add column if not exists extracted_at       timestamptz;
