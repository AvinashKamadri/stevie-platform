-- Stevie Platform — DERIVED INTELLIGENCE LAYER (between canonical and consumers).
--
-- NOT AI — derived. Pre-computed rollups so CloudCannon page generation and the
-- nomination assistant both READ these instead of re-aggregating 84k
-- recognitions per request. Materialized; refreshed by refresh_derived() after
-- each canonicalize run (the only time the inputs change).
--
-- Each has a UNIQUE index on its key so it can be REFRESHED CONCURRENTLY
-- (no read-lock on the published site during a refresh).

-- Prestige weighting (tunable): gold 5 / silver 3 / bronze 1 / finalist 0.5.
-- Lives inline here; promote to a config table if it needs to vary by program.

-- --- Organizations -----------------------------------------------------------
drop materialized view if exists organization_stats cascade;
create materialized view organization_stats as
select o.id, o.slug, o.name,
       count(*)                                              as total_recognitions,
       count(*) filter (where r.result_level = 'gold')       as gold,
       count(*) filter (where r.result_level = 'silver')     as silver,
       count(*) filter (where r.result_level = 'bronze')     as bronze,
       count(*) filter (where r.result_level = 'finalist')   as finalist,
       min(r.year)                                           as first_win_year,
       max(r.year)                                           as latest_win_year,
       count(distinct r.year)                                as active_years,
       count(distinct r.category_definition_id)              as category_diversity,
       count(distinct r.country_id)                          as country_spread,
       count(distinct r.program_edition_id)                  as editions_entered,
       round(sum(case r.result_level when 'gold' then 5 when 'silver' then 3
                   when 'bronze' then 1 when 'finalist' then 0.5 else 0 end), 1)
                                                             as prestige_score,
       round(count(*)::numeric / nullif(count(distinct r.year), 0), 2)
                                                             as avg_wins_per_active_year
from organizations o
join parties p        on p.organization_id = o.id
join recognitions r   on r.recipient_party_id = p.id
group by o.id, o.slug, o.name;
create unique index organization_stats_id_uidx on organization_stats (id);
create index organization_stats_prestige_idx on organization_stats (prestige_score desc);

-- --- Categories (by lineage/definition, so they survive renames) -------------
drop materialized view if exists category_stats cascade;
create materialized view category_stats as
select cd.id, cd.slug, cd.name,
       count(*)                                 as total_recognitions,
       count(distinct r.recipient_party_id)     as distinct_recipients,
       count(distinct r.year)                   as active_years,
       min(r.year) as first_year, max(r.year)   as latest_year
from category_definitions cd
join recognitions r on r.category_definition_id = cd.id
group by cd.id, cd.slug, cd.name;
create unique index category_stats_id_uidx on category_stats (id);

-- --- Countries ---------------------------------------------------------------
drop materialized view if exists country_stats cascade;
create materialized view country_stats as
select c.id, c.slug, c.name,
       count(*)                              as total_recognitions,
       count(distinct r.recipient_party_id)  as distinct_recipients,
       count(distinct r.category_definition_id) as category_diversity,
       min(r.year) as first_year, max(r.year) as latest_year
from countries c
join recognitions r on r.country_id = c.id
group by c.id, c.slug, c.name;
create unique index country_stats_id_uidx on country_stats (id);

-- --- Programs (rolled up across their editions) ------------------------------
drop materialized view if exists program_stats cascade;
create materialized view program_stats as
select pr.id, pr.slug, pr.name,
       count(*)                              as total_recognitions,
       count(distinct pe.id)                 as editions,
       count(distinct r.recipient_party_id)  as distinct_recipients,
       min(r.year) as first_year, max(r.year) as latest_year
from programs pr
join program_editions pe on pe.program_id = pr.id
join recognitions r      on r.program_edition_id = pe.id
group by pr.id, pr.slug, pr.name;
create unique index program_stats_id_uidx on program_stats (id);

-- One call to rebuild the whole layer after canonicalize. Plain (non-concurrent)
-- REFRESH so it is callable from inside a function/transaction; the offline
-- rebuild has no live readers to protect. Live page-gen can issue
-- `refresh materialized view concurrently <view>` directly (the unique indexes
-- above exist precisely to allow that).
create or replace function refresh_derived() returns void language plpgsql as $$
begin
    refresh materialized view organization_stats;
    refresh materialized view category_stats;
    refresh materialized view country_stats;
    refresh materialized view program_stats;
end;
$$;
