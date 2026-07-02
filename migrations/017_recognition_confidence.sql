-- Migration 017 — recognition-level confidence rollup (M7.4 surface).
--
-- A recognition's trustworthiness is bounded by its WEAKEST resolved dimension
-- (a gold-award row whose organization is a low-confidence singleton is only as
-- grounded as that org). This view rolls fact_confidence up per recognition for
-- downstream consumers (search, the assistant, exports) — min_score is the
-- honest floor, avg_score the overall texture. Pure view over derived data;
-- recomputes for free whenever fact_confidence is rebuilt.

create or replace view recognition_confidence as
select r.id                       as recognition_id,
       r.node_id,
       round(min(fc.score), 4)    as min_score,   -- weakest link = the floor
       round(avg(fc.score), 4)    as avg_score,
       count(*)                   as n_dimensions,
       case when min(fc.score) >= 0.85 then 'high'
            when min(fc.score) >= 0.65 then 'medium'
            else 'low' end        as band
from recognitions r
join entity_links el     on el.parsed_record_id = r.parsed_record_id
join fact_confidence fc  on fc.entity_type = el.entity_type
                        and fc.entity_id   = el.entity_id
group by r.id, r.node_id;

comment on view recognition_confidence is
    'M7.4: per-recognition confidence rolled up from fact_confidence. min_score '
    '(weakest resolved dimension) is the honest floor for grounding a claim.';
