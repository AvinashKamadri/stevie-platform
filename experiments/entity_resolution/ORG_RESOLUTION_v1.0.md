# Organization Entity Resolution — v1.0

Status as of 2026-07-01. This is the architecture + evaluation record for the
organization-resolution subsystem (M2–M5). It supersedes nothing in the
per-milestone design docs (`M0_DECISION_STORE_DESIGN.md`, `PHASE_E_DESIGN.md`)
— this is the release-level summary that ties them together.

## What this is (and isn't)

An organization is "resolved" when duplicate names across recognitions
(`"IBM"`, `"International Business Machines"`, `"the IBM Corporation"`) are
recognized as the same entity in the canonical schema. This subsystem finds
candidate duplicates, scores them, and durably records human decisions.

**Not in scope** (separate, later projects):
- Person/award/role extraction (`Recognition -> Organization | Person | Award
  | Role`) — person names currently live unstructured in `nomination_title`.
- The relationship graph — structuring `related` verdicts (parent/subsidiary,
  org/foundation) into real edges. Currently just an audit-logged verdict.

## Architecture

```
organizations (canonical, from parsed_records)
      |
      v
BLOCKING (candidate generation) ---- canonical/candidates.py, migration 010
      |   trigram | rare_token | acronym
      v
organization_merge_candidate  (derived, truncatable, reasons[] provenance)
      |
      v
FEATURES ---- canonical/features.py
      |   normalization (v2) + interaction terms (v3), versioned FEATURE_VERSION
      v
organization_merge_candidate.features (named jsonb, versioned)
      |
      v
SCORER ---- canonical/scorer.py (train) + canonical/calibration.py (Platt)
      |   logistic regression, versioned MODEL_VERSION, versioned SPLIT_VERSION
      v
model_predictions  (left_key, right_key, model_version) -> probability
      |
      v
PRODUCTION SCORING ---- canonical/predict.py (`cli score`)
      |   incremental: only unscored candidates get scored
      v
HUMAN REVIEW ---- canonical/review.py (`cli review --lane main|acronym`)
      |   merge / distinct / related
      v
organization_merge_decision  (durable input, NEVER truncated)
      |
      v
CANONICALIZE REPLAY ---- canonical/pipeline.py, canonical/normalize.py
      |   build_merge_closure() resolves the decision graph
      v
organization_alias (derived) + resolved organizations
```

Every stage left of `organization_merge_decision` is a **pure recomputation**
from data below it — regenerable, truncatable, safe to rebuild. Everything
right of it is a **durable input** a human produced; nothing downstream
touches it destructively.

## Data model

| Table | Kind | Truncated by | Notes |
|---|---|---|---|
| `organization_merge_candidate` | derived | `cli candidates` (DELETE, not TRUNCATE — see below) | keys `collate "C"`, `left_key < right_key` |
| `model_predictions` | derived, mostly stable | never (survives regen) | keyed by `(left_key, right_key, model_version)` — **not** `candidate_id` |
| `model_registry` | durable metadata | never | one row per trained model version; `metrics_summary` set = frozen |
| `organization_merge_decision` | **durable input** | never | `unique(loser_key)` — one fate per losing key, globally |
| `organization_alias` | derived | `canonicalize --fresh` | retired key -> surviving org id |
| `organization_review_log` | **durable audit log** | never | every review action, incl. `related` (no other home for it yet) |

**Why `model_predictions` isn't keyed by `candidate_id`:** `organization_merge_candidate`
is fully regenerated (new ids) on every `cli candidates` run and on every full
canonical rebuild. Postgres's `TRUNCATE ... CASCADE` transitively wipes any
table connected by an FK chain regardless of that FK's own `ON DELETE` clause
— verified empirically. Predictions are keyed by the rebuild-stable norm-key
pair instead, matching every other durable/semi-durable table here.

## Versioning

Three independent version axes, each frozen once evaluated against:

- `SPLIT_VERSION` (`canonical/split.py`) — the train/calibration/evaluation
  partition function (SHA-256 hash of the ordered key pair -> bucket).
  Currently `v1` (60/20/20).
- `FEATURE_VERSION` (`canonical/features.py`) — the feature vector. `v1`
  (baseline, 11 features) -> `v2` (+ normalization, `despaced_trigram_similarity`)
  -> `v3` (+ `acronym_x_trigram`/`acronym_x_jaccard` interactions).
- `MODEL_VERSION` (`model_registry.model_version`) — a trained+calibrated
  model. Once `model_registry.metrics_summary` is set (by `cli evaluate`),
  that version is **frozen**: `cli train`/`cli calibrate` refuse to touch it
  again. A new result is always a new `model_version`.

## Model iteration results (frozen, `model_registry`)

| Model | Features | Recall | Precision | F1 | Acronym recall |
|---|---|---|---|---|---|
| v1 | v1 | 0.548 | 0.895 | 0.680 | 0/17 |
| **v1.1** | v2 (normalization) | **0.645** | **0.909** | **0.755** | 0/17 |
| v1.2 | v3 (+ interactions) | 0.645 | 0.870 | 0.741 | 0/17 |
| v1.3 | v3 (+ class_weight='balanced') | 0.645 | 0.870 | 0.741 | 0/17 |

**v1.1 is the best-performing model** (strictly dominates v1.2/v1.3). **v1.2 is
the production model** — the candidate table's `feature_version` has since
moved to `v3` (from the v1.2/v1.3 experiments), and a model can only score
candidates matching its own `feature_version` exactly. v1.2 is functionally
indistinguishable from v1.1 (the interaction terms are inert, not harmful).

**Root-cause finding — do not spend further budget on this within the current
feature family:** a confirmed acronym merge (`ca`/`cessna aircraft`) and a
confirmed acronym distinct (`ab`/`astrazeneca bulgaria`) have near-identical
feature vectors. Neither interaction terms nor class weighting moved acronym
recall at all (0/17 in every version tried) — this is an information ceiling,
not a fitting problem. The review workflow routes acronym-provenance
candidates to a dedicated lane, sorted by review priority (acronym length),
never by score.

## Blocking recall (M4, `stevie recall --corpus v2`)

| Blocker | Gold found | Emitted | Gold/emit |
|---|---|---|---|
| trigram | 130 | 287,347 | 0.0005 |
| rare_token | 56 | 14,506 | 0.0039 |
| acronym | 18 | 1,094 | 0.0165 (best in set) |

Overall/achievable recall on gold v2: 100%. The acronym blocker was added only
after an unbiased yield study (`acronym_feasibility_2026-07-01.md`) projected
161–359 recoverable merges from a CI lower bound, all structurally
unrecoverable by trigram/rare_token (sim ~0.03).

## How to operate it

```bash
stevie candidates                    # regenerate blocking (DELETE + reinsert; ~90s)
stevie features                      # (re)compute the current FEATURE_VERSION for every candidate
stevie score --model-version v1.2    # score every not-yet-scored candidate (incremental by default)
stevie review --lane main            # review high-confidence non-acronym candidates, sorted by score
stevie review --lane acronym         # review acronym candidates, sorted by review priority (not score)
stevie recall --corpus v2            # measure blocking recall against gold
stevie evaluate --model-version v1.2 # print (or re-print, if already frozen) the model's evaluation
```

Training a NEW model version (only after new evidence — see "Model
iteration" above):

```bash
stevie train --model-version v2 --class-weight balanced   # fit on `train`
stevie calibrate --model-version v2                       # Platt-scale on `calibration`
stevie evaluate --model-version v2                         # ONE frozen run on `evaluation` — locks the version
```

## What I would NOT do next

Per the M5 iteration results: don't keep tuning the scorer without a new
hypothesis or new labeled data (v1.2/v1.3 already show diminishing/negative
returns). The two legitimate next steps are (a) accumulate real review
decisions via `stevie review`, and (b) after enough new labels exist,
retrain a v2 scorer on the expanded corpus — a new hypothesis, not a repeat
of the ones already tested.
