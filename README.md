# Stevie Platform

A trustworthy historical awards data platform. Phase 1 is **pure data
acquisition**: mirror all ~82,654 Stevie winners/finalists into our own
Postgres, with raw HTML preserved so the dataset can be regenerated without ever
re-crawling. Search, entity resolution, authority pages and the nomination
assistant are later layers built *on top* of this — not part of it.

> Written fresh. The existing `StevieIntel`, `stevieNABackend` and
> `Stevie-Awards-CloudCannon` repos are **reference only** (they confirmed the
> endpoints, selectors and safe crawl rate) — no code is inherited.

## Architecture — four states, one rule

```
Stevie site → Acquisition → Raw archive → Parser → Canonical → Published
                              (state 1)   (state 2)  (state 3)   (state 4)
```

Phase 1 materializes **states 1 and 2**. The rule that makes everything
rebuildable:

> The only tables a human or crawler ever writes to are `raw_pages`,
> `fetch_queue`, `harvest_state`. `parsed_records` is always derivable from
> `raw_pages` — truncate and replay any time.

So a better parser never means a re-crawl: bump `PARSER_VERSION`, `make reparse`.

## How acquisition works (two phases, decoupled)

The listing is a Drupal Views form gated by a **math question** (`17 + 3 =`,
not a real CAPTCHA). The detail pages are wide open.

| Phase | Engine | Does | Volume |
|-------|--------|------|--------|
| **harvest** | Playwright | solve the math question, page the listing, collect every node id → `fetch_queue` | ~1,378 pages |
| **fetch** | httpx (no browser) | `GET /view-details/{id}` per node id → archive raw HTML | ~82,654 |

`fetch` never parses — it only archives. Parsing is a separate replayable step.
Both phases are **resumable**: re-running picks up the pending work.

## Quick start

```bash
make install          # deps + chromium
cp .env.example .env  # point DATABASE_URL at your Postgres
make db               # optional: local Postgres via Docker
make migrate          # create the schema
make harvest          # Phase 1a — collect node ids  (~1.5h)
make fetch            # Phase 1b — archive detail HTML (~18h, polite ~1.3 req/s, resumable)
make parse            # state 1 → state 2
make status           # completeness report
make test             # parser unit tests
```

## Done means done

`make status` is the verification gate. Phase 1 is complete only when:

```
reported total == fetched,  pending == 0,  failed == 0,  parsed (bad) == 0
```

Never silently continue past a structure miss: a detail page that parses
without `organization_name / year / award` is flagged `is_complete = false`.

## Layout

```
migrations/001_init.sql        the four-state schema (states 1–2 + crawl bookkeeping)
src/stevie_platform/
  config.py                    confirmed endpoints, selectors, pacing
  db.py                        async psycopg helpers
  acquisition/harvest.py       Playwright: math question + listing → node ids
  acquisition/fetch.py         httpx: AIMD-paced detail archiver
  parsing/parse.py             pure HTML → record (PARSER_VERSION lives here)
  parsing/run.py               raw_pages → parsed_records (replayable)
  cli.py                       migrate | harvest | fetch | parse | status
tests/test_parse.py            parser fixtures
```

## Not in Phase 1

No entity resolution, no search, no embeddings, no RAG, no assistant, no
CloudCannon. Those are states 3–4 and the consumer products, built later on the
intelligence layer as a **library first**, a service only when the assistant
needs it.
```
