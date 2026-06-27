#!/usr/bin/env bash
# Non-invasive fetch telemetry sampler. Snapshots fetch_queue into fetch_samples
# every 60s. Pure observer — does NOT touch the fetch worker. Ctrl-C to stop.
set -euo pipefail
cd "$(dirname "$0")/.."
LOG="${1:-fetch.log}"
PSQL=(docker exec stevie-pg psql -U stevie -d stevie_platform -tAc)

while true; do
  # Best-effort AIMD gap from the worker's log; NULL if not yet printed.
  # `|| true` so a no-match grep can't trip pipefail+set -e and kill the loop.
  gap=$(grep -o 'gap=[0-9.]*' "$LOG" 2>/dev/null | tail -1 | cut -d= -f2 || true)
  [ -z "${gap:-}" ] && gap=NULL
  "${PSQL[@]}" "insert into fetch_samples (fetched, pending, fetching, http403, http429, gap_s)
     select count(*) filter (where status='done'),
            count(*) filter (where status='pending'),
            count(*) filter (where status='fetching'),
            count(*) filter (where last_error like 'HTTP 403'),
            count(*) filter (where last_error like 'HTTP 429'),
            ${gap}
     from fetch_queue;" >/dev/null
  sleep 60
done
