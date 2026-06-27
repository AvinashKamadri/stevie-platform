-- Stevie Platform — timeline stats + trgm indexes on dimensions.
--
-- timeline_stats answers "trends over time" (country/program/category growth)
-- for both products without scanning recognitions per request.
--
-- The trgm GIN indexes make the possible-duplicate METRIC (a read-only
-- diagnostic self-join) fast, and pre-stage the eventual generalization of
-- candidate generation to every dimension — NOT built yet, just unblocked.

create index if not exists programs_name_trgm   on programs   using gin (name gin_trgm_ops);
create index if not exists categories_name_trgm on categories using gin (name gin_trgm_ops);
create index if not exists industries_name_trgm on industries using gin (name gin_trgm_ops);
create index if not exists people_name_trgm     on people     using gin (name gin_trgm_ops);

drop materialized view if exists timeline_stats cascade;
create materialized view timeline_stats as
select r.year,
       count(*)                                            as total_recognitions,
       count(*) filter (where r.result_level = 'gold')     as gold,
       count(*) filter (where r.result_level = 'silver')   as silver,
       count(*) filter (where r.result_level = 'bronze')   as bronze,
       count(*) filter (where r.result_level = 'finalist') as finalist,
       count(distinct r.recipient_party_id)                as distinct_recipients,
       count(distinct r.country_id)                        as distinct_countries,
       count(distinct r.category_definition_id)            as distinct_categories,
       count(distinct r.program_edition_id)                as distinct_editions
from recognitions r
where r.year is not null
group by r.year;
create unique index timeline_stats_year_uidx on timeline_stats (year);

-- Re-define the refresh helper to include timeline_stats.
create or replace function refresh_derived() returns void language plpgsql as $$
begin
    refresh materialized view organization_stats;
    refresh materialized view category_stats;
    refresh materialized view country_stats;
    refresh materialized view program_stats;
    refresh materialized view timeline_stats;
end;
$$;
