# Phase 2 — Search Design (architecture, pre-code)

Status: DESIGN (2026-07-08). Foundation (Phase 1) complete and banked to master
(M6 → M7 → blog). This document is the search architecture agreed in the design
session; implementation follows it.

## Update 2026-07-08 — four-layer architecture (supersedes entity-only scope)

Agreed target architecture:

```
Authoritative Layer   winner pages · categories · programs · orgs        (Phase 1 ✅)
Evidence Layer        Stevie blogs ✅  + external (company blogs, press    (blogs done;
                      releases, news, interviews, video, case studies,     external = next
                      PDFs) — official-API/official-source acquisition,     scoped pilot)
                      enrich-not-define, org_id-linked, lower trust prior
Retrieval Layer       hybrid + semantic + ranking OVER BOTH data layers    (this phase)
Applications          case studies · recommendations · email · blog gen ·  (later)
                      nomination assistant
```

Consequences that change this doc:
- **Retrieval is corpus-agnostic**: `search_documents` carries a doc-type
  discriminator and indexes BOTH entity docs (authoritative) and evidence docs.
  (This supersedes the earlier "entity documents only / article search = Phase 3".)
- **Semantic (pgvector) is coupled to the Evidence Layer**: structured entity
  facts index well with FTS; unstructured external evidence needs embeddings.
  Build order unchanged (FTS v1 over existing data → semantic v2 as evidence
  matures), but semantic is core to the target, not optional.
- **Evidence corpus** = a new `winner_evidence` table (org_id, source_url,
  source_type, published_at, content, themes/metrics/sentiment via LLM extract),
  acquired via official search APIs + official sources (NO SERP/LinkedIn
  scraping). Piloted on a scoped winner slice before any scale.

## Organizing principle: index ENTITIES, not documents

The platform's unit of meaning is the **canonical entity** (organization,
program, program_edition, category_definition, person) — that is what users
query ("IBM's Stevie history", "technology winners in 2022", "Female
Entrepreneur winners"). So the searchable document is **one per entity**,
assembled from the knowledge graph. Blogs and recognitions are *evidence that
enriches an entity's document and its ranking*, never a competing index. This
also yields P2.4 entity pages and P2.5 summaries from the same index, and honors
the rule from Phase 1: search reads resolved entities, never raw blogs.

## Decisions (settled)

- **v1 = Postgres full-text** (`tsvector` + `pg_trgm`, both already installed).
  No new infrastructure. Semantic / NL search (pgvector embeddings + hybrid RRF)
  is **v2**, layered on once FTS proves out.
- **Entity documents only.** Blogs are a ranking signal + a searchable field
  inside entity docs. A separate blog-article collection ("blogs about X") is
  **Phase 3**, added only if users need it.
- **Confidence is a first-class ranking signal.** M7 `fact_confidence` demotes
  low-confidence singletons — the mechanism that keeps "grounded garbage" from
  ranking high.

## Searchable document model

A derived, regenerable `search_documents` table — one row per entity, a
projection over the canonical graph. Truncate-and-rebuild, exactly like
`fact_confidence` (nothing upstream depends on it; additive).

```
search_documents
  entity_type      text        -- organization|program|program_edition|
                                --   category_definition|person
  entity_slug      text        -- stable key (join back to the entity/page)
  entity_id        bigint      -- convenience cache
  display_name     text
  tsv              tsvector    -- weighted (see fields below)
  facets           jsonb       -- years[], programs[], categories[], countries[],
                                --   industries[], result_levels[]  (structured filters)
  signals          jsonb       -- precomputed ranking bundle (see below)
  confidence       numeric     -- from fact_confidence
  updated_at       timestamptz
  primary key (entity_type, entity_slug)
```

### Searchable fields (tsvector weights)

| Weight | Content |
|--------|---------|
| A | entity name + aliases (norm_key variants) |
| B | associated program names, category names |
| C | countries, industries, years, result levels |
| D | nomination titles + blog-mention snippets (enrichment, ranks lowest) |

Fuzzy/typo tolerance via `pg_trgm` on `display_name`, reusing the ER blocking
infra.

## Ranking model

```
score = ts_rank(tsv, query)                      -- text relevance
        × confidence_multiplier(fact_confidence)  -- M7: corroborated > singleton
        × recognition_weight(count, result_level) -- more/higher wins rank up
        + recency_boost(latest_year, blog_recency)
        + buzz(blog_mention_count × link_confidence)
```

Non-text signals (precomputed into `signals`): entity confidence, recognition
count, best result level (gold>silver>bronze>finalist), recency, blog
mention count × link confidence, record completeness.

## Related-entity surfacing (graph, not search)

From an entity, traverse existing edges: shared category_definition / program /
year / country / industry via recognitions, plus co-mentions via
`blog_entity_links`. Ranked by overlap × confidence. This powers "similar
winners" (a Phase 3 nomination-assistant need) without a search query.

## Milestones

| # | Deliverable | Notes |
|---|-------------|-------|
| P2.1 | `search_documents` build (migration + `stevie search build`) | derived/regenerable; the index |
| P2.2 | Query API: FTS + structured facet filters | keyword + "technology, 2022, gold" filters |
| P2.3 | Ranking (confidence + non-text signals) | the formula above |
| P2.4 | Entity pages | rendered from the same doc + facets |
| P2.5 | Search summaries | template-filled from facets ("IBM: N recognitions across M programs, K golds, since YYYY"); grounded, no LLM in v1 |

## Explicitly deferred

- **Semantic / NL search** (pgvector, embeddings, hybrid RRF) → Phase 2 **v2**.
- **Blog-article search collection** → Phase 3 (if needed).
- **LLM-generated summaries** → optional later; v1 summaries are templated.

## Reuse / no new infra for v1

Postgres FTS + `pg_trgm` (installed), `fact_confidence`/`recognition_confidence`
(M7), `normalize.norm_key` (aliases), the canonical graph, and the
truncate-and-rebuild pattern. New: one derived table + a `stevie search`
command family.
