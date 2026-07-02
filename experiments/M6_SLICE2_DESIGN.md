# M6 Slice 2 — v2 Training Pipeline (DESIGN, not yet implemented)

Status: **DESIGN — decision-complete, review before code.** Opened 2026-07-02 on
branch `m6-active-learning`. All open questions resolved to recommended defaults
(§12; override anytime — no code exists yet). Implementation is deliberately
deferred until the environment is up (`make install && make db && make migrate`)
so the new training path can be verified against a **live M5 reproduction** —
proving the baseline didn't move.

This document is reviewed *before* the code exists on purpose: Slice 2 touches
the training pipeline, and a design is far cheaper to change than a refactor.

---

## 1. Objective (narrow)

Create a **clean, separate v2 training pipeline** that trains on the expanded
(active-learning) corpus and is evaluated on the **frozen 112-pair benchmark**
from Slice 1 — without altering, and without risking, the M5 (v1.2) path.

Slice 2 builds the *pipeline*. It does **not** itself run active-learning rounds
or claim a recall improvement — that's Slice 3+. Slice 2 is done when we can
train a v2 model that is provably (a) evaluated on the identical benchmark and
(b) never contaminated by it.

## 2. Non-negotiables (the review checklist)

| # | Rule | How the design honors it |
|---|---|---|
| 1 | **M5 is immutable** | `scorer.py` / `calibration.py` / `scorer_eval.py` are **not edited**. v2 lives in a new module and *imports* their pure functions. v1.2's artifact, registry row, and metrics are never overwritten. |
| 2 | **Explicit dataset split** | Four disjoint sets, built by one function, with no code path that can merge them (§4). |
| 3 | **Guard early** | `assert_no_contamination` runs **immediately after dataset assembly, before the split and fit** (§5) — not after training. |
| 4 | **Version every artifact** | Per-version filenames for model, metrics, calibration; no overwrite (§6). |
| 5 | **A/B on one benchmark** | Both models scored on the same 112 frozen pairs; nothing else varies (§7). |
| 6 | **Record provenance** | Every label carries `source` + `review_round` from the first round (§8). |

## 3. Module layout — fork orchestration, reuse the math

```
canonical/
  scorer.py          (M5 — UNTOUCHED)  fit_model, transform, to_row, coefficient_table   ← reused
  calibration.py     (M5 — UNTOUCHED)  fit_platt, apply_platt, brier_score, reliability_bins ← reused
  scorer_eval.py     (M5 — UNTOUCHED)  confusion_counts, precision_recall_f1, provenance_breakdown, false_negatives ← reused
  benchmark.py       (Slice 1)         frozen_pair_set, frozen_labels, assert_no_contamination ← reused
  split_v2.py        (NEW)             three-way {train, calibration, validation} hash split
  scorer_v2.py       (NEW)             v2 dataset assembly + orchestration (train / calibrate / evaluate)
```

**Why a new module, not a flag on `scorer.py`:** the M5 path must stay
byte-reproducible forever. A shared function with a `v2=True` branch is one edit
away from changing v1.2's behavior. A separate module that *calls* the pure
math (which is version-agnostic — it's just `StandardScaler` + `LogisticRegression`
+ Platt) gets reuse without that risk. **No model math is duplicated; only
dataset assembly and orchestration are new.**

**Recommended v2 model = same family as M5** (logistic regression,
`FEATURE_VERSION` v3, same as v1.2). The entire M6 thesis is *"better data, not
architecture."* Holding the model fixed makes any recall change **attributable
to the new labels alone** — the clean experiment the whole milestone is built to
run. (Changing the model *and* the data at once would make the A/B
uninterpretable.) See open question Q2.

## 4. The four-way split

```
ALL labeled pairs (corpus v3 = v2 gold + active_round_*)
        │
        ├───────────────────────────────► FROZEN BENCHMARK  (112 pairs)
        │                                    • NEVER train
        │        (exact set from             • NEVER calibrate
        │         benchmark.py)              • NEVER validate
        │                                    • EVALUATION ONLY
        │
        └── remaining labeled pairs  ──►  split_v2 (pure hash of the pair)
                                             ├── train        (~70%)  fit coefficients
                                             ├── calibration  (~15%)  fit Platt
                                             └── validation   (~15%)  model/threshold selection
```

Key points:

- **The benchmark is subtracted first**, by set membership against
  `benchmark.frozen_pair_set()` — *not* by re-hashing. So it stays exactly 112
  pairs no matter how much the corpus grows.
- **`split_v2` has no `evaluation` bucket.** Evaluation lives in the frozen file,
  external to the hash. `split_v2` only partitions the *non-benchmark* pool into
  train / calibration / **validation**.
- **`split_v2` hashes INDEPENDENTLY of v1** (salted digest `split_v2\x00lk\x00rk`).
  This is not cosmetic: the benchmark *is* v1's top-20% fraction band, so the
  non-benchmark pool spans only the bottom 80% of v1's hash. Reusing v1's
  fraction would strand any v2 bucket above 0.8 — validation (`[0.85,1.0)`) would
  always be empty. Caught by the Slice 2 dry run (`validation=0`); fixed by
  salting. Lesson: a "reuse the hash" shortcut silently correlated the split with
  the benchmark removal.
- **Validation is new in v2** (M5 had only train/calibration/evaluation). Its
  job: any model or threshold selection happens here, so we never tune against
  the benchmark. The benchmark is still touched exactly once, at the very end.
- **Repartitioning old gold is fine.** Under `split_v2`, old gold pairs (minus
  the benchmark) get fresh train/cal/val buckets that need not match their M5
  v1 buckets. The *only* set that must be identical across M5 and v2 for a valid
  A/B is the evaluation set — and it is, by construction.
- **No labels are wasted.** Slice 1 noted ~20% of new labels would hash to v1
  "evaluation" and be reserved. Under this scheme the only excluded pairs are the
  112 benchmark pairs; every other label (old or new) is usable for train/cal/val.

## 5. Guard placement — fail before you fit

```
load corpus v3 (labels + provenance)
        │
        ▼
join features  ──►  build dataset rows (pair, label, features, source, round)
        │
        ▼
subtract frozen benchmark  ──►  non-benchmark training pool
        │
        ▼
assert_no_contamination(pool_pairs)      ◄── FAILS HERE if any benchmark pair leaked
        │                                     (BenchmarkContaminationError)
        ▼
split_v2  ──►  train / calibration / validation
        │
        ▼
fit scaler + LR (train)  →  fit Platt (calibration)  →  select (validation)
```

The guard is a **backstop that proves the subtraction is correct**: since we
explicitly removed benchmark pairs, it should always pass — and if a future
`split_v2` change or a provenance bug ever reintroduces one, the run dies
immediately with the offending pairs named, before a single coefficient is fit.

## 6. Versioned artifacts (no overwrite)

M5 already versions the model artifact (`artifacts/models/<version>.joblib`) and
the durable metrics record (`model_registry`, frozen once). Slice 2 keeps those
as the source of truth and adds **git-diffable JSON mirrors** so an A/B is a file
diff, not a DB query:

```
artifacts/
  models/
    v1.2.joblib          (M5 — untouched; scaler + clf + platt)
    v2.joblib            (NEW)
  metrics/
    v1.2.json            (mirror of model_registry.metrics_summary for v1.2)
    v2.json              (NEW — written at freeze)
  calibration/
    v1.2.json            (Platt coeffs + calibration Brier/reliability)
    v2.json              (NEW)
```

- `model_registry` stays the durable, freeze-protected record (unchanged
  mechanism). The JSON files are portable mirrors, one per version, never
  overwritten.
- Writing `v1.2.json`/`v1.2 calibration.json` is a one-time back-fill from the
  existing frozen registry row — read-only w.r.t. M5.

## 7. A/B evaluation

```
        M5  (v1.2)                         M6  (v2)
          │                                  │
          └──────────►  112 FROZEN BENCHMARK  ◄──────────┘
                         (identical pairs)

              precision / recall / F1 / Brier
              + per-blocker provenance breakdown
              + false-negative list per model
```

- `scorer_v2.run_evaluate_v2` scores the 112 benchmark pairs with the v2 model,
  computes metrics using the **reused** pure functions from `scorer_eval.py`,
  writes `model_registry` (freezing v2) + `artifacts/metrics/v2.json`, and prints
  a side-by-side vs v1.2.
- **v1.2 needs no re-run.** The frozen benchmark *is* v1.2's evaluation set (v1
  "evaluation" partition of corpus v2), so v1.2's existing `metrics_summary` is
  already the correct left-hand column. (Verified in Slice 1: `related=4` matches.)
- Promotion gate (from `M6_PLAN.md` §7): v2 recall on the benchmark **> 0.645**
  and precision **≥ 0.88**, else v2 is not promoted and production stays v1.2.

## 8. Label provenance (from round 1)

**Where labels come from:** `stevie review` already writes
`organization_merge_decision` (which *already* has a `source` column) and logs
every action to `organization_review_log`. Training, however, reads **gold
corpus files**, not the decision table — so reviewed decisions must be exported
into a gold component with provenance.

**Schema (extends the existing gold row; back-compatible):**

```jsonc
{ "key_a": "...", "key_b": "...", "label": "merge|distinct|related",
  "labeled_by": "avinash", "labeled_at": "…",   // already present in gold rows
  "source": "manual | active_learning",         // NEW
  "review_round": 0 }                            // NEW (0 = original gold; 1..N = AL rounds)
```

- Existing 580 gold rows have neither field → **default `source="manual"`,
  `review_round=0` at load time.** No rewrite of existing files.
- Each active-learning round exports `gold/active_round_<N>.jsonl` with
  `source="active_learning"`, `review_round=N`.
- **New corpus version `v3`** in `CORPUS.json` = v2 components + `active_round_*`.
  `load_corpus("v3")` assembles and dedupes them (existing loader already
  dedupes by ordered pair, so a re-labeled pair in a later round supersedes
  cleanly if we order components newest-last — see open question Q3).

**Why capture it from the start:** provenance is what later answers *"how much
did round 1 help? was round 3 worth it? which labels moved recall most?"* — the
per-round ablation in Slice 3. Impossible to reconstruct if not recorded now.

## 9. CLI surface (additive; M5 commands unchanged)

```bash
stevie export-labels --round N [--source active_learning]   # decisions -> gold/active_round_N.jsonl
stevie train-v2      --model-version v2                     # dataset build + guard + fit (corpus v3)
stevie calibrate-v2  --model-version v2                     # Platt on the v2 calibration split
stevie evaluate-v2   --model-version v2                     # ONE frozen eval on the 112 benchmark; prints A/B vs v1.2
```

Kept separate (not a `--v2` flag on `train`) for the same immutability reason as
the module split, and kept as three commands (not one) to preserve M5's
"evaluate freezes exactly once" discipline. A convenience `stevie fit-v2` that
chains all three (stopping if the guard fires) is optional.

## 10. How M5 reproducibility is protected (summary)

- No edits to `scorer.py`, `calibration.py`, `scorer_eval.py`.
- No writes to `artifacts/models/v1.2.joblib` or v1.2's registry row.
- `split_v2.py` is a new file; `split.py` (v1) is untouched, so v1.2 re-trains
  identically.
- First action once the env is up: `stevie evaluate --model-version v1.2` must
  reproduce recall 0.645 / precision 0.909-family **before** any v2 code runs.

## 11. Test plan (pure cores unit-tested offline; wiring tested on the DB)

- `split_v2`: three buckets, ratios sum to 1, deterministic, order-independent,
  no `evaluation` bucket.
- Dataset assembly: benchmark pairs are absent from the train/cal/val pool;
  provenance defaults applied to legacy rows; corpus v3 dedup correct.
- **Guard integration test:** deliberately inject a benchmark pair into the pool
  and assert `BenchmarkContaminationError` is raised *before* any fit.
- A/B: v2 evaluation set == `frozen_pair_set()` exactly (size 112).
- Reuse contracts: assert `scorer_v2` calls `scorer.fit_model` /
  `calibration.fit_platt` (no reimplementation drift).

## 12. Decisions (provisional defaults — override any before implementation)

These were flagged as your calls; each is set to the recommended default so the
design is implementation-ready. All are cheap to reverse — none has been coded.
Say the word to change any.

- **Q1 — `split_v2` ratios → 70/15/15 (train/calibration/validation).** DECIDED.
  Standard proportions; validation large enough to be informative for model/
  threshold selection without starving train. Revisit if train support proves
  too thin after the benchmark is subtracted.
- **Q2 — v2 model family → logistic regression + feature_version v3, identical
  to v1.2.** DECIDED (the load-bearing one). Only the training *data* changes, so
  any recall movement is attributable to the labels, not the model. Changing the
  model is a *separate* later experiment, never bundled with a data change.
- **Q3 — re-label precedence → newest round wins.** DECIDED. `active_round_*`
  components are ordered last in the `v3` manifest; the existing dedup-by-ordered-
  pair loader keeps the last occurrence, so a re-labeled pair supersedes its
  original gold label cleanly.
- **Q4 — artifact JSON mirrors → adopt `artifacts/metrics/` +
  `artifacts/calibration/`.** DECIDED. `model_registry` stays the durable truth;
  the per-version JSON files make the A/B a git diff and travel outside the DB.

> These defaults are recorded so implementation isn't blocked on a round-trip.
> They are provisional: overriding any is a one-line change to this section, not
> a code refactor, because no Slice 2 code exists yet.

---

## Slice 2 acceptance criteria

1. `scorer.py` / `calibration.py` / `scorer_eval.py` diff-clean (M5 untouched).
2. A v2 model trains on corpus v3, is evaluated on exactly the 112 frozen pairs,
   and its metrics land in `model_registry` + `artifacts/metrics/v2.json`.
3. Injecting a benchmark pair into the training pool raises
   `BenchmarkContaminationError` before any fit.
4. Every training label resolves to a `(source, review_round)` provenance pair.
5. `stevie evaluate --model-version v1.2` still reproduces the M5 baseline.
