#!/usr/bin/env bash
# One-shot acquisition progress snapshot (no loop). For live view: make watch
set -euo pipefail
docker exec stevie-pg psql -U stevie -d stevie_platform -xc "
select
  (select count(*)||' / 1409  ('||round(100.0*count(*)/1409,1)||'%)' from harvest_state where status='done')            as harvest_pages,
  (select count(*) from harvest_state where status='failed')                                                            as harvest_failed,
  (select count(*) from fetch_queue)                                                                                    as ids_discovered,
  (select count(*) filter (where status='done')||' / 84534  ('||round(100.0*count(*) filter (where status='done')/84534,1)||'%)' from fetch_queue) as detail_fetched,
  (select count(*) from fetch_queue where status='failed')                                                              as fetch_failed,
  (select count(*) from parsed_records)                                                                                 as parsed;"
