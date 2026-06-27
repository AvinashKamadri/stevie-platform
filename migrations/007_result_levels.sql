-- Stevie Platform — migration 007: recognize the special Stevie award tiers.
--
-- Parser 1.1.0 classifies three legitimate recognition levels that carry no
-- medal keyword: Grand Stevie (top cross-program honor), People's Choice
-- (public vote) and Distinguished Honoree. The original CHECK constraint only
-- allowed gold/silver/bronze/finalist/other, so these records failed to load.
-- Widen the constraint and teach the prestige rollup their weights (leaving
-- them at the previous implicit 0 would understate top winners like Grand
-- Stevie recipients).

-- 1) Widen the result_level domain ------------------------------------------
alter table recognitions drop constraint if exists recognitions_result_level_check;
alter table recognitions add  constraint recognitions_result_level_check
    check (result_level in (
        'gold','silver','bronze','finalist',
        'grand','peoples_choice','distinguished_honoree',
        'other'));

-- 2) Rebuild organization_stats with the new tiers (counts + prestige) -------
-- Prestige weighting (tunable): grand 8 / gold 5 / silver 3 / peoples_choice 2 /
-- bronze 1 / finalist 0.5 / distinguished_honoree 0.5 / other 0.
drop materialized view if exists organization_stats cascade;
create materialized view organization_stats as
select o.id, o.slug, o.name,
       count(*)                                                          as total_recognitions,
       count(*) filter (where r.result_level = 'grand')                  as grand,
       count(*) filter (where r.result_level = 'gold')                   as gold,
       count(*) filter (where r.result_level = 'silver')                 as silver,
       count(*) filter (where r.result_level = 'bronze')                 as bronze,
       count(*) filter (where r.result_level = 'finalist')               as finalist,
       count(*) filter (where r.result_level = 'peoples_choice')         as peoples_choice,
       count(*) filter (where r.result_level = 'distinguished_honoree')  as distinguished_honoree,
       min(r.year)                                           as first_win_year,
       max(r.year)                                           as latest_win_year,
       count(distinct r.year)                                as active_years,
       count(distinct r.category_definition_id)              as category_diversity,
       count(distinct r.country_id)                          as country_spread,
       count(distinct r.program_edition_id)                  as editions_entered,
       round(sum(case r.result_level
                   when 'grand'    then 8   when 'gold'     then 5
                   when 'silver'   then 3   when 'peoples_choice' then 2
                   when 'bronze'   then 1   when 'finalist' then 0.5
                   when 'distinguished_honoree' then 0.5
                   else 0 end), 1)                          as prestige_score,
       round(count(*)::numeric / nullif(count(distinct r.year), 0), 2)
                                                             as avg_wins_per_active_year
from organizations o
join parties p        on p.organization_id = o.id
join recognitions r   on r.recipient_party_id = p.id
group by o.id, o.slug, o.name;
create unique index organization_stats_id_uidx on organization_stats (id);
create index organization_stats_prestige_idx on organization_stats (prestige_score desc);
