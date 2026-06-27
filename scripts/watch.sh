#!/usr/bin/env bash
# Live acquisition progress. Ctrl-C to exit. Refreshes every 10s.
set -euo pipefail
PSQL=(docker exec stevie-pg psql -U stevie -d stevie_platform -At -F$'\t' -c)

while true; do
  clear
  echo "stevie-platform — acquisition  ($(date '+%H:%M:%S'))"
  echo "----------------------------------------------------------------"

  # --- harvest (discovery) ---
  "${PSQL[@]}" "
    select
      'harvest' as stage,
      count(*) filter (where status='done') || ' / 1409' as done,
      round(100.0*count(*) filter (where status='done')/1409,1) || '%' as pct,
      count(*) filter (where status='failed') || ' failed' as issues
    from harvest_state" | column -t -s$'\t'

  # --- fetch (download) — enriched: %, live rate, ETA, 403s ---
  "${PSQL[@]}" "
    with q as (
      select
        count(*) filter (where status='done')                                   as fetched,
        count(*) filter (where status='pending')                                as pending,
        count(*) filter (where status='failed')                                 as failed,
        count(*) filter (where last_error like 'HTTP 403')                       as blocked,
        count(*) filter (where status='done' and updated_at > now() - interval '2 minutes') as recent
      from fetch_queue
    )
    select
      'fetch' as stage,
      fetched || ' / 84534'                                       as done,
      round(100.0*fetched/84534,1) || '%'                         as pct,
      pending || ' pending'                                       as issues,
      round(recent/120.0, 2) || ' req/s'                          as rate,
      case when recent > 0
           then '~' || round((pending/(recent/120.0))/3600.0, 1) || 'h'
           else 'stalled?' end                                    as eta,
      blocked || ' x403'                                          as blocks,
      failed || ' failed'                                         as dead
    from q" | column -t -s$'\t'

  echo "----------------------------------------------------------------"
  # freshness: how long since the last fetch completion landed
  "${PSQL[@]}" "select 'last fetch write: ' ||
     coalesce(round(extract(epoch from now()-max(updated_at)))::text,'?') || 's ago'
     from fetch_queue where status='done'"
  sleep 10
done
