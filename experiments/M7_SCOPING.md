# M7 Scoping — PROPOSAL (objective pending user confirmation)

Drafted 2026-07-02 while defining the post-ML-foundation milestone. **No code
written, branch not renamed, nothing committed as final.** This teed-up decision
mirrors the M6 kickoff: pick ONE objective before designing.

## The finding that drives the choice

Two Phase-1 roadmap items remain: **blog linking** and **source-confidence
scoring**. A codebase check shows they are *not* equally ready:

- **Blog linking has no data.** No blog/article tables in any migration, no blog
  crawler in `acquisition/` (only the awards crawler). "Linking" presupposes
  content to link — so this milestone would actually begin with a whole
  **acquisition phase** (new crawler + parser for the Stevie blog), *then*
  linking. Large, multi-slice, and starts from zero data.

- **Source-confidence scoring runs on data we already have.** The schema is
  practically purpose-built for it (see below), and it maps directly onto the
  governance goal of grounding every claim in a verified record + source URL.

**Recommendation: M7 = source-confidence scoring.** Tractable now, no new
acquisition, high governance value. Blog acquisition+linking is a strong M8 once
we're ready to invest in a new data source. (If confirmed, rename the branch
`m7-blog-linking` → `m7-source-confidence`.)

## Why the data is ready (substrate for a confidence model)

Every fact already carries how it came to be:

- **`entity_links`** (the match ledger) — per resolved dimension: `match_method`
  (`exact` | `fuzzy` | `manual` | `new`), `match_score`, `model_version`,
  `parser_version`, `reviewed_by` / `reviewed_at` (human sign-off).
- **`recognitions` → `parsed_records` → `raw_pages`** — `url`, `fetched_at`,
  `checksum`; plus `crawl_run_id` provenance.
- **`overrides`** — human editorial corrections as durable data.

A confidence score is largely a principled combination of signals already
present — not new data collection.

## Sketch of the objective (to refine into a full design once confirmed)

**Goal:** attach a calibrated confidence ∈ [0,1] (and a human-readable basis) to
each resolved fact / recognition, grounded in its provenance.

**Signal ladder (illustrative):**

| Basis | Confidence |
|---|---|
| Human-reviewed / editorial override (`reviewed_by` set, `manual`) | highest (≈1.0) |
| `exact` norm_key match | high |
| `fuzzy` match | scaled by `match_score` |
| model-proposed | the model's calibrated probability (we already calibrate!) |
| `new` / unresolved | low |

Modifiers: source recency (`fetched_at`), official awards page (all current data)
vs. future lower-trust sources (blog, PDF) — which is where blog linking later
plugs in cleanly.

**Proposed slices (M5/M6 discipline):**
1. **Benchmark/infra** — define what "confidence" grades against; a small
   labeled/heuristic ground truth; a frozen evaluation like M6's.
2. **Baseline model** — deterministic confidence from the signal ladder.
3. **Evaluation** — calibration/reliability (reuse M5's Brier/reliability tools).
4. **Refinement** — surface low-confidence facts for review (ties back to the
   active-learning loop).
5. **Docs** — a confidence field consumable by downstream (search, assistant).

## Open decision (for you)

Confirm **M7 = source-confidence scoring** (recommended), or redirect to blog
acquisition+linking / another roadmap item. On confirmation I'll rename the
branch and write the full `M7_PLAN.md` (goal, success metrics, experiments,
risks, rollback) before any code — same discipline as M6.
