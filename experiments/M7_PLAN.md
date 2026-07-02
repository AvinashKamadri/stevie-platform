# M7 Plan — Source / Fact Confidence Scoring

Status: **COMPLETE** (built + validated live 2026-07-02, branch
`m7-source-confidence`). Owner: avinash@flashbacklabs.com. Same design-before-code
discipline as M5/M6.

## Results (2026-07-02, live)

All five slices shipped. `stevie confidence` scored **30,361 entities**;
`recognition_confidence` rolls up **84,495 recognitions**.

| Surface | Outcome |
|---|---|
| Entities by band | high 13.3% · medium 40.3% · **low 46.4% (14,089 singletons)** |
| By type (avg) | program 0.95 · category_definition 0.845 · country 0.804 · industry 0.593 · organization 0.569 |
| Recognitions by band | medium 70,828 · low 13,667 (none "high" — the org dimension is the usual weakest link) |
| Explainability | 100% of scores carry ≥1 reason |

**Validation (behavior, not calibration — no labeled ground truth exists):**
ordering holds (controlled > free-text; corroborated > singleton — unit-tested +
observed), the low tail is exactly the singleton/uncorroborated set, and every
score is explainable. **Bonus data-quality finding:** `industry` has 1,145
distinct values with 1,003 singletons → a normalization/dedup opportunity the
confidence report surfaces for free.

**Not promoted to a hard gate anywhere** — `fact_confidence` is derived/additive;
downstream consumption is opt-in. The natural next step ties back to M6: the
low-confidence tail is a ready-made review queue → labels → eventual calibration.

## 0. Reality check that shaped this plan (do not skip)

Before designing, we inspected the live `entity_links` ledger (420,774 rows).
The schema *allows* rich provenance (fuzzy scores, model versions, human review);
**almost none of it is populated:**

| Signal | Reality |
|---|---|
| `match_method` | only `exact` (395,347) and `new` (25,427) — no fuzzy/manual |
| `match_score` | **0 rows** populated |
| `model_version` | **0 rows** |
| `reviewed_by` | **0 rows** |
| `overrides` | **0 rows** |

**Consequence:** the "signal ladder" in `M7_SCOPING.md` (human > exact > fuzzy >
model > new) collapses — those tiers don't exist in the data. Confidence must be
built from the signals that ARE real:

1. **`match_method`** — `exact` (matched an existing canonical entity by norm_key)
   vs `new` (no match; entity created fresh — unverified, often a singleton).
2. **Corroboration** — how many recognitions reference the entity. A singleton
   (count=1) is weaker than one seen 246×; frequency is the strongest *populated*
   trust signal (same `rec_count` we used in M6).
3. **Entity type** — controlled-vocabulary dimensions (country/industry/program/
   category) exact-matched are near-certain; free-text organizations depend on
   dedup quality, so corroboration matters more there.
4. **Recency** (`raw_pages.fetched_at`) — minor tie-breaker.

## 1. Goal

Attach an **explainable confidence ∈ [0,1] + reasons** to each resolved fact
(entity_link grain) and roll it up per recognition, grounded only in signals that
actually exist. Explainability is a first-class requirement: **a score is always
accompanied by the reasons that produced it**, never an opaque number.

## 2. Why now / why this (vs blog linking)

Blog linking has no data (needs acquisition first → M8). Source confidence runs
on existing data, extends M6's foundation, and serves the governance goal of
grounding claims. See `M7_SCOPING.md`.

## 3. What "confidence" means here

Confidence that **this resolution is correct and the fact is trustworthy** — not
a probability of a downstream event. High = exact match to a well-corroborated
canonical entity (esp. controlled vocab). Low = a `new`, uncorroborated singleton
(exactly the facts a grounding/review system should flag first).

## 4. The model (deterministic, explainable — v1)

A transparent function of the real signals, emitting `(score, reasons[])`:

```
base   = 0.90 if match_method == 'exact' else 0.40   # 'new'
type_bonus = +0.05 if entity_type in controlled-vocab dims
corroboration:
    count>=10 -> +0.05 ;  count in 2..9 -> +0.0 ;  count==1 (singleton) -> -0.20
clamp to [0,1]
```
(Exact weights finalized in M7.1 against the observed distribution.) Every term
that fires appends a human-readable reason, e.g. *"exact norm_key match"*,
*"corroborated by 246 recognitions"*, *"controlled-vocabulary dimension"*,
*"singleton — unverified"*. **No ML for v1** — deterministic + explainable first;
a learned model is a later option only if a labeled correctness signal appears.

## 5. Where stored / surfaced

- **Storage:** a NEW **derived** table `fact_confidence` (migration 016), keyed to
  the stable pair (parsed_record + entity_type + entity_id), holding `score`,
  `reasons` jsonb, `computed_at`. Derived = truncatable/regenerable, consistent
  with the architecture (everything left of a durable input is recomputable).
  **Additive** — no change to existing tables, no canonicalize surgery.
- **Compute:** a standalone `stevie confidence` pass over `entity_links` +
  corroboration counts. Does NOT touch the ingestion pipeline (safe, re-runnable).
- **Surface (M7.4):** a `--report` (distributions, low-confidence facts) and a
  recognition-level rollup view for downstream (search/assistant/export).

## 6. Success metrics

- **Monotonicity/ordering:** exact ≥ new; well-corroborated ≥ singleton — the
  score respects the signal ladder (checkable on the full 420k without labels).
- **Distribution sanity:** controlled-vocab exact matches cluster high; the 25k
  `new` links and singletons populate the low tail (they should be the flagged set).
- **Explainability:** 100% of scores carry ≥1 reason.
- **No labeled ground truth exists** (0 reviewed rows) — so this round validates
  *behavior/distribution*, not calibration against correctness. Honest limit,
  stated up front. Closing it is a future active-learning tie-in (review the
  low-confidence tail → labels → calibration).

## 7. Risks

- **Thin signal** — near-binary match_method means confidence is driven mostly by
  corroboration. Mitigate: make corroboration the primary graduation; be explicit
  that v1 is a heuristic prior, not a calibrated probability.
- **No ground truth** — can't prove correctness, only sane behavior. Stated.
- **Grain/volume** — 420k links; keep compute a set-based SQL pass, not per-row Python.

## 8. Rollback

`fact_confidence` is derived and additive: drop/ignore the table and nothing else
changes. No production behavior depends on it until M7.4 surfaces it.

## 9. Slices

| Slice | Deliverable |
|---|---|
| **M7.0 — Planning** | This doc. Define confidence, signals (from real data), storage. |
| **M7.1 — Model** | Pure `score_fact(signals) -> (score, reasons)`; unit-tested; weights tuned to the observed distribution. |
| **M7.2 — Pipeline** | Migration 016 `fact_confidence`; `stevie confidence` computes over entity_links + corroboration, persists score+reasons. Additive, re-runnable. |
| **M7.3 — Evaluation** | Distributions; ordering checks; low-confidence tail = new/singletons; explainability coverage. |
| **M7.4 — Surface** | `stevie confidence --report`; recognition-level rollup view for downstream. |

## 10. Roadmap position

M6 active learning ✅ → **M7 source confidence (how trustworthy is each fact)** →
M8 blog acquisition → M9 blog/entity linking → M10 Nomination Assistant. Each
builds on capabilities that already exist. New evidence sources (M8/M9) later feed
*into* the confidence assessment built here.
