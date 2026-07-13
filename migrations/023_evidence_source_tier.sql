-- Migration 023 — winner_evidence source authority tier. Additive.
--
-- Records the source taxonomy tier (A>B>C>D>E) per evidence row so downstream
-- retrieval/drafting can prefer authoritative sources (official/press/analyst)
-- over general/blog ones. Populated by evidence.source_tier(url) at insert time;
-- nullable so pre-v2 rows stay valid. No CHECK constraint (values are app-owned)
-- to keep the migration re-run-safe.

alter table winner_evidence add column if not exists source_tier text;

create index if not exists winner_evidence_source_tier_idx
  on winner_evidence (source_tier);
