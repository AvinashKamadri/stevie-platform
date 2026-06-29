-- Phase E / M1 — candidate coverage validation.
--
-- Question: is the current entity_candidates set COMPLETE under the blocking
-- strategy, or is it only what creation-order happened to generate?
-- (Generation runs only when an org is newly created — pipeline.py:153.)
--
-- Method: compute the full all-pairs blocked candidate set (name % name above
-- the same threshold the generator uses), reduce existing entity_candidates to
-- an unordered org-pair set, and diff. Read-only; safe to run against canonical.
--
-- Run (once a DB with canonical-v2 materialized exists):
--   docker exec -i stevie-pg psql -U stevie -d stevie_platform -f - < coverage_scan.sql
-- or:  psql "$DATABASE_URL" -f experiments/entity_resolution/coverage_scan.sql

-- Match the generator's blocking knobs (pipeline.py:215 + ops.py:129).
set pg_trgm.similarity_threshold = 0.3;   -- governs the % operator
\set floor 0.4                             -- generator's Python floor

-- (A) ALL discoverable org-pairs under the blocking strategy (name-vs-name,
--     unordered, deduped). This is the "complete" set the generator approximates.
create temporary view allpairs as
select least(a.id, b.id) as a, greatest(a.id, b.id) as b,
       similarity(a.name, b.name) as sim
from organizations a
join organizations b on a.id < b.id and a.name % b.name
where similarity(a.name, b.name) >= :floor;

-- (B) The org-pairs the current generator actually produced. Each
--     entity_candidates row links a source mention (parsed_record_id) to an
--     existing candidate org; resolve the mention to its org via entity_links.
create temporary view generated as
select distinct least(el.entity_id, ec.candidate_entity_id) as a,
                greatest(el.entity_id, ec.candidate_entity_id) as b
from entity_candidates ec
join entity_links el
  on el.parsed_record_id = ec.parsed_record_id
 and el.entity_type = 'organization'
where ec.entity_type = 'organization'
  and el.entity_id <> ec.candidate_entity_id;

-- ---- Headline counts -------------------------------------------------------
select 'all_discoverable_pairs' as metric, count(*) from allpairs
union all
select 'generated_pairs', count(*) from generated
union all
select 'discoverable_NOT_generated', count(*)
  from (select a,b from allpairs except select a,b from generated) x
union all
select 'generated_NOT_discoverable', count(*)
  from (select a,b from generated except select a,b from allpairs) y;

-- Interpretation (the three outcomes):
--   discoverable_NOT_generated = 0        -> generator complete; trust 27,592.
--   small (tens/low hundreds)             -> patch the generator, then review.
--   large                                 -> redesign generation before review.
-- generated_NOT_discoverable > 0 is expected/benign: those were scored on the
--   raw string (raw-vs-name), which the name-vs-name all-pairs view does not
--   reproduce. Inspect a sample below to confirm they're raw-only artifacts.

-- ---- Sample of the gap (highest-similarity misses first) -------------------
select oa.name as org_a, ob.name as org_b, round(ap.sim, 3) as sim
from (select a,b from allpairs except select a,b from generated) m
join allpairs ap on ap.a = m.a and ap.b = m.b
join organizations oa on oa.id = m.a
join organizations ob on ob.id = m.b
order by ap.sim desc
limit 40;
