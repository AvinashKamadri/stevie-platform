-- Operational telemetry for the fetch controller. NON-canonical: this is an
-- observability table, not part of the deterministic raw→parsed→canonical
-- spine, so `rebuild` never touches it. Sampled once a minute by a separate
-- observer process (scripts/sampler.sh) — the fetch worker itself is untouched.
--
-- After an overnight crawl this holds thousands of rows: graph effective_rps,
-- 403 frequency and backoff (gap) against time to find the real sweet spot
-- instead of guessing on the next crawl.
create table if not exists fetch_samples (
    id        bigserial primary key,
    ts        timestamptz not null default now(),
    fetched   int,        -- cumulative done
    pending   int,
    fetching  int,        -- in-flight claims
    http403   int,        -- rows currently carrying a 403 last_error (throttle proxy)
    http429   int,
    gap_s     numeric     -- AIMD gap parsed from fetch.log at sample time (NULL if unknown)
);

-- Derived per-interval rate — the actual tuning signal.
create or replace view fetch_rate as
select
    ts,
    fetched,
    pending,
    http403,
    gap_s,
    round(
        (fetched - lag(fetched) over (order by ts))::numeric
        / nullif(extract(epoch from ts - lag(ts) over (order by ts)), 0),
        2) as effective_rps
from fetch_samples
order by ts;
