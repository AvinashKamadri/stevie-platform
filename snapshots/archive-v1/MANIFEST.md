# Stevie Platform — `archive-v1`

**Frozen:** 2026-06-27
**What this is:** an immutable snapshot of the *non-regenerable* acquisition
state — the raw HTML archive plus crawl provenance. Everything downstream
(parsed_records, canonical, derived) is replayable from this with no network
access, so it is intentionally **not** included.

## Contents (`stevie_raw_archive-v1.dump`, pg_dump custom/compressed format)

| table          | role                                                        |
|----------------|-------------------------------------------------------------|
| `raw_pages`    | immutable archive — 84,534 detail + 1,409 listing pages (HTML stored gzip-compressed in `html` bytea) |
| `harvest_state`| per listing-page harvest status (1,409 pages, all done)     |
| `fetch_queue`  | per node_id fetch status (84,534 done, 0 pending, 0 failed) |
| `crawl_runs`   | provenance: every harvest/fetch/parse/canonicalize run      |
| `meta`         | key/value run metadata (reported_total, parser_version, …)  |

- File: `stevie_raw_archive-v1.dump` (126 MB)
- SHA-256: see `SHA256SUMS`

## Acquisition completeness (verified at freeze)

- raw detail pages: **84,534 / 84,534** (100%), all distinct node_ids
- fetch_queue: 84,534 done, **0 pending, 0 failed**
- 26 node_ids are genuinely *empty records on the source* (blank org / year 0) —
  a real property of the site, not a fetch defect.

## Baseline after replay (parser 1.1.0)

- parsed_records: 84,534 (100% complete); 39 missing required fields
- recognitions built: **84,495**, 0 canonicalization failures
- result level: bronze 27,182 · gold 21,989 · silver 19,492 · finalist 9,792 ·
  distinguished_honoree 1,285 · peoples_choice 1,029 · grand 612 · other 3,114
- organizations 32,446 · countries 331 · categories 4,534 · programs 11
- **All 8 data-quality gates pass.**

## Dedup backlog (diagnostic — deferred to Phase D, nothing merged yet)

- possible org duplicates: 51,043 (driver: city/state suffix, e.g. "IBM" vs
  "IBM, Armonk, NY") · category_definitions: 15,769 · industries: 77 · people: 0
- entity_candidates generated: 52,781

## Restore

```bash
# into a fresh DB (replays everything else from here)
docker exec -i stevie-pg pg_restore -U stevie -d stevie_platform --clean --if-exists \
  < stevie_raw_archive-v1.dump
# then: cli migrate && cli reparse && cli canonicalize
```

## Provenance

- Parser version at freeze: **1.1.0**
- Acquisition method: Playwright harvest (math-captcha listing) + httpx detail
  fast-path through 10 Webshare proxy lanes (~9.5 req/s, 0 blocks).
- Not a git repo at freeze time — code version is pinned only by this manifest.
  Recommended follow-up: `git init` + tag the tree that produced this archive.
