-- Stevie Platform — migration 008: brand-level org normalization metadata.
--
-- Phase D promotes the deterministic location + corporate-suffix rules into the
-- canonical pipeline. The platform models BRANDS, not registered legal
-- entities, so `norm_key` (the dedup key) ignores legal suffixes. To stay
-- non-lossy, the original string and the stripped legal form are preserved:
--
--   norm_key     brand-level dedup key      e.g. "cisco systems"
--   name         cleaned display name       e.g. "Cisco Systems"   (== display_name)
--   raw_name     first-seen original text   e.g. "Cisco Systems, Inc., San Jose, CA"
--   legal_suffix stripped legal form        e.g. "Inc."
--
-- Per-occurrence original names also remain in recognition_parties.raw_value,
-- so every legal/jurisdictional variant is recoverable for future reporting.

alter table organizations add column if not exists raw_name     text;
alter table organizations add column if not exists legal_suffix  text;

comment on column organizations.name         is 'cleaned display name (location + legal-suffix stripped)';
comment on column organizations.raw_name     is 'first-seen original org string, before normalization';
comment on column organizations.legal_suffix is 'stripped legal-entity form (Inc., Ltd., GmbH, …); per-occurrence forms remain in recognition_parties.raw_value';

create index if not exists organizations_legal_suffix_idx on organizations (legal_suffix);
