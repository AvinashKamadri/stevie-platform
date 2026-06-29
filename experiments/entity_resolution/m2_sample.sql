-- M2 — Gold evaluation dataset: stratified sample from the full discoverable space.
--
-- Creates `m2_gold_sample`: 500 pairs drawn from the complete all-pairs blocked scan
-- (287,347 pairs at sim >= 0.40), NOT from entity_candidates. The gold set is built
-- from the broader discoverable space so it remains valid regardless of how M3/M4
-- ultimately generates candidates — a stable benchmark for any future generator/scorer.
--
-- Run once after canonical-v2 is materialized:
--   docker exec stevie-pg psql -U stevie -d stevie_platform -f - < experiments/entity_resolution/m2_sample.sql
-- or:
--   psql "$DATABASE_URL" -f experiments/entity_resolution/m2_sample.sql
--
-- This table is NOT part of the canonical pipeline. `make rebuild` (which calls
-- truncate_canonical) does NOT touch it. It persists across rebuilds.
-- Label columns are populated by label.py.

begin;

-- Match coverage_scan.sql blocking knobs exactly
set pg_trgm.similarity_threshold = 0.3;   -- governs the % operator

-- Reproducible: same seed → same 500 pairs on every run against the same orgs
select setseed(0.42);

drop table if exists m2_gold_sample;

create table m2_gold_sample as
with
-- Full all-pairs name-vs-name blocked scan — mirrors coverage_scan.sql's allpairs view.
-- key_a = lexicographically smaller norm_key; key_b = larger. This ordering is stable
-- across rebuilds (norm_key is a deterministic function of the org name).
allpairs as (
    select
        least(a.norm_key, b.norm_key)                                    as key_a,
        greatest(a.norm_key, b.norm_key)                                 as key_b,
        case when a.norm_key <= b.norm_key then a.name else b.name end   as name_a,
        case when a.norm_key <= b.norm_key then b.name else a.name end   as name_b,
        case when a.norm_key <= b.norm_key then a.id   else b.id   end   as id_a,
        case when a.norm_key <= b.norm_key then b.id   else a.id   end   as id_b,
        similarity(a.name, b.name)                                       as sim
    from organizations a
    join organizations b on a.id < b.id and a.name % b.name
    where similarity(a.name, b.name) >= 0.40
),
-- Assign bands, rank within each band by random order (seeded above)
banded as (
    select *,
        case
            when sim >= 0.70 then 'high'
            when sim >= 0.55 then 'border'
            else                  'low'
        end as band,
        row_number() over (
            partition by
                case
                    when sim >= 0.70 then 'high'
                    when sim >= 0.55 then 'border'
                    else                  'low'
                end
            order by random()
        ) as rn
    from allpairs
),
-- Stratified caps: 200 high / 200 border / 100 low = 500 total
-- Rationale:
--   high   — recall probe: a good generator must find these true merges
--   border — threshold calibration: where precision vs recall tradeoffs live
--   low    — precision probe: mostly noise; verifies we don't over-merge
sample as (
    select key_a, key_b, name_a, name_b, id_a, id_b,
           round(sim::numeric, 4) as sim, band
    from banded
    where (band = 'high'   and rn <= 200)
       or (band = 'border' and rn <= 200)
       or (band = 'low'    and rn <= 100)
)
-- Enrich with recognition context for the labeling UI
select
    s.key_a, s.key_b, s.name_a, s.name_b, s.sim, s.band,
    ctx_a.rec_count   as rec_count_a,
    ctx_b.rec_count   as rec_count_b,
    ctx_a.countries    as countries_a,
    ctx_b.countries    as countries_b,
    -- label columns (null until filled by label.py)
    null::text         as label,
    null::text         as reason,
    null::text         as labeled_by,
    null::timestamptz  as labeled_at
from sample s
join lateral (
    select
        count(distinct rp.recognition_id)::int                            as rec_count,
        array_agg(distinct c.name order by c.name)
            filter (where c.name is not null)                             as countries
    from parties p
    join recognition_parties rp on rp.party_id   = p.id
    join recognitions r          on r.id          = rp.recognition_id
    left join countries c        on c.id          = r.country_id
    where p.organization_id = s.id_a
) ctx_a on true
join lateral (
    select
        count(distinct rp.recognition_id)::int                            as rec_count,
        array_agg(distinct c.name order by c.name)
            filter (where c.name is not null)                             as countries
    from parties p
    join recognition_parties rp on rp.party_id   = p.id
    join recognitions r          on r.id          = rp.recognition_id
    left join countries c        on c.id          = r.country_id
    where p.organization_id = s.id_b
) ctx_b on true;

alter table m2_gold_sample
    add constraint m2_gold_sample_pk    primary key (key_a, key_b),
    add constraint m2_gold_sample_label check (label in ('merge', 'distinct'));

comment on table m2_gold_sample is
    'M2 gold evaluation dataset: 500-pair stratified sample from the full '
    'discoverable candidate space (287k all-pairs at trgm sim >= 0.40). '
    'Keyed by stable norm_key pairs. NOT truncated by make rebuild.';

commit;

-- ── Summary ────────────────────────────────────────────────────────────────────
\echo ''
\echo '--- M2 sample created ---'
select band,
       count(*)                         as pairs,
       round(avg(sim)::numeric, 3)      as avg_sim,
       round(min(sim)::numeric, 3)      as min_sim,
       round(max(sim)::numeric, 3)      as max_sim
from m2_gold_sample
group by band
order by avg_sim desc;

\echo ''
\echo '--- Next step ---'
\echo 'Run label.py to begin hand-labeling:'
\echo '  python experiments/entity_resolution/label.py'
