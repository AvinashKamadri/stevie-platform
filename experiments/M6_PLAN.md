# M6 Plan — Active Learning → v2 Scorer Retrain

Status: **DESIGN** (opened 2026-07-02, branch `m6-active-learning`).
Owner: avinash@flashbacklabs.com.

This document is the single source of truth for M6. **If the plan changes,
update this file _before_ changing code** (the discipline that carried M5).

---

## 0. The journey so far (initial step → M5)

Documented here so M6 has the full arc in one place. Per-milestone detail lives
in `entity_resolution/ORG_RESOLUTION_v1.0.md`, `M0_DECISION_STORE_DESIGN.md`,
and `PHASE_E_DESIGN.md`.

| Stage | Commit(s) | What shipped |
|---|---|---|
| **Acquisition baseline** | `bf9c74b` | Harvest → fetch → parse pipeline; raw archive-v1. The success criterion: regenerate everything from the raw archive with NO network. |
| **Phase D — deterministic normalization** | `9af5c49`→`da75291` | Org location-clause + corporate-suffix normalization promoted into canonical. Produced **canonical-v2** (brand-level org model, `legal_suffix` metadata). |
| **M0 — decision store design** | (design doc) | Designed `organization_merge_decision` as a durable, never-truncated human input, cleanly separated from all derived/regenerable state. |
| **M2 — gold evaluation set** | `2a7ce48` | 500 labeled pairs sampled from the full discoverable space. The benchmark everything since is measured against. |
| **M3 — merge-decision replay** | `499f67e` | migration 009: `organization_merge_decision` + `organization_alias`. `build_merge_closure()` resolves the decision graph on canonicalize. Verified reversible (delete decision → orgs restored; recognitions invariant at 84,495). |
| **M4 — blocking subsystem** | `96d7c3f` | High-recall candidate generation: `trigram` + `rare_token` + evidence-driven `acronym` blocker. **Achievable recall on gold v2 = 100%.** |
| **M5 — the scorer** | `e59fcf2`→`95ff73e` | Deterministic split (v1, 60/20/20) → feature extraction (v1→v2→v3) → logistic regression → Platt calibration → **one frozen evaluation per model version**. |
| **Phase 3 — production + review** | `72f061c`, `24f0742` | Incremental production scoring (`stevie score`); two-lane human review (`main` by score, `acronym` by priority). `model_predictions` re-keyed by norm-key pair to survive candidate regeneration. |
| **v1.0 record** | `0501ca3` | Release-level architecture + evaluation doc tying M2–M5 together. |

### The frozen M5 baseline (the number M6 must beat)

From `model_registry` (frozen — `cli train`/`calibrate` refuse to touch an
evaluated version), transcribed in `ORG_RESOLUTION_v1.0.md`:

| Model | Features | Recall | Precision | F1 | Acronym recall |
|---|---|---|---|---|---|
| v1 | v1 | 0.548 | 0.895 | 0.680 | 0/17 |
| **v1.1** (best) | v2 | **0.645** | **0.909** | **0.755** | 0/17 |
| v1.2 (production) | v3 | 0.645 | 0.870 | 0.741 | 0/17 |
| v1.3 | v3 + balanced | 0.645 | 0.870 | 0.741 | 0/17 |

- Blocking recall (gold v2): **100%** — candidate generation is *not* the bottleneck.
- Scorer recall stuck at **0.645**: ~35% of true merges are surfaced by blocking but *rejected by the scorer*.
- Acronym recall **0/17**: diagnosed as an **information ceiling**, not a fitting
  problem (a confirmed merge and a confirmed distinct have near-identical feature
  vectors). Interaction terms + class weighting moved it **zero**.

> **Baseline-freeze note (2026-07-02):** these numbers are the *authoritative
> frozen baseline* and are captured here from `model_registry`. A live re-run of
> the test + eval suite is **blocked pending environment setup** — this checkout
> has no `.venv` and no reachable Postgres (Docker not wired into WSL). Before
> the first M6 experiment, run: `make install && make db && make migrate`, then
> `make test` and `stevie evaluate --model-version v1.2` to reproduce the table
> above live. See §7.

---

## 1. Goal (one objective)

**Raise scorer recall above the frozen 0.645 baseline — without sacrificing
precision — by growing the labeled corpus through uncertainty-prioritized human
review, then retraining and freezing a v2 scorer on the expanded corpus.**

This is the exact path the v1.0 doc names as legitimate: *(a) accumulate real
review decisions, then (b) after enough new labels exist, retrain a v2 scorer —
a new hypothesis, not a repeat of the ones already tested.*

## 2. Why M5 isn't enough

The M5 iteration record proves the recall ceiling is **not** reachable by more
model tuning within the current feature family:

- v1.2 and v1.3 (interaction terms, class weighting) moved recall **not at all**
  vs v1.1, and dented precision. Diminishing/negative returns are confirmed, not
  suspected.
- The doc's explicit "what I would NOT do next": *don't keep tuning the scorer
  without a new hypothesis or new labeled data.*

The one lever untried is **more, better-targeted labels**. M5 trained on the
M2 gold set (500 pairs) alone. The model's own uncertainty region — pairs it
scores near 0.5 — is exactly where new labels carry the most information.

## 3. Success metrics

Measured on a **fixed held-out benchmark** (see §6 — this is the critical
methodological guardrail):

| Metric | Baseline (v1.1) | M6 target | Guardrail |
|---|---|---|---|
| Recall | 0.645 | **≥ 0.72** | must exceed 0.645 to promote |
| Precision | 0.909 | maintain | **must not drop below 0.88** |
| F1 | 0.755 | improve | — |
| Label efficiency | — | report recall gain per 100 new labels | — |

**Explicitly out of scope for M6:** acronym recall (0/17). That is an
information-ceiling problem requiring *new signal* (embeddings / external KB / context
beyond the name string) — a separate future milestone, not this mechanism. If v2
happens to move it, that's a bonus, not a success condition.

## 4. Experiments

1. **Uncertainty sampling.** Rank unlabeled candidates by proximity to the
   decision boundary (`|p − 0.5|`, or entropy). Route the top-N to the review
   lane. Compare label efficiency against a random-sampling control.
2. **Label round(s).** Label a budgeted batch of new pairs (target +150–300)
   via `stevie review`, recorded as durable `organization_merge_decision` rows.
3. **Retrain v2.** Fit + Platt-calibrate a new `MODEL_VERSION` on the expanded
   **train** partition; evaluate **once** on the frozen benchmark (locks v2).
4. **Ablation.** Train v2 on M5-labels-only vs the expanded set → isolate the
   contribution of the new data from any incidental changes.

## 5. Risks

| Risk | Mitigation |
|---|---|
| **Label throughput** — active learning stalls without reviewer time (solo reviewer). | Small budgeted rounds; report label-efficiency so we know when returns flatten. |
| **Benchmark contamination** — new labels hashing into the evaluation bucket would make M5↔M6 non-comparable. | Freeze the M5 evaluation *pair set*; new labels enter train/calibration only. See §6. |
| **Sampling bias** — uncertainty-only sampling skews the training distribution. | Mix in a fraction of random labels each round. |
| **Precision regression** — noisy new labels drag precision down. | Precision guardrail (≥ 0.88); v2 is a new version, not a mutation, so non-promotion is costless. |

## 6. Methodological guardrail — the frozen benchmark

`SPLIT_VERSION` assigns a pair to train/calibration/evaluation by hashing the
key pair. New labeled pairs will hash into **all three** buckets — including
evaluation — which would silently change the evaluation set and break
comparability with M5.

**Decision:** pin the exact M5 evaluation pair-set as a fixed held-out
benchmark. New labels are routed into **train + calibration only**. M5 vs v2
recall is then measured on the *identical* evaluation pairs → a clean A/B.

**Acceptance criterion (regression guard):** if any pair from the frozen
evaluation set appears in the active-learning training pool, the pipeline
**fails with an explicit error** (`BenchmarkContaminationError`) rather than
relying on convention — a stronger guarantee that survives future edits and a
possible `SPLIT_VERSION` bump.

**Delivered in Slice 1** (`canonical/benchmark.py`): the frozen set is
materialized to `gold/frozen_benchmark_v1.jsonl` (**112 pairs**: 31 merge / 77
distinct / 4 related — the `related=4` matches the M5 record exactly, confirming
the pin reproduces the true M5 evaluation partition). The file carries a content
digest so any edit is detected by `stevie benchmark` (verify). New
active-learning labels are excluded from the queue at selection time
(`active_learning._excluded_pairs`) *and* barred at train time by
`assert_no_contamination` — belt and suspenders.

## 7. Rollback criteria

- If v2 recall on the frozen benchmark **≤ 0.645**, or precision **< 0.88**:
  **do not promote.** Production stays on v1.2.
- Rollback is costless by construction: v2 is a new `MODEL_VERSION`; promotion is
  just moving the production pointer. Everything left of
  `organization_merge_decision` is regenerable/truncatable, and the decisions
  themselves are additive durable input — nothing to undo.

## 8. Slices (M5's disciplined pattern)

| Slice | Deliverable | Status |
|---|---|---|
| **1 — Infrastructure** | Frozen-benchmark mechanism + contamination guard (`canonical/benchmark.py`); uncertainty-sampling queue (`canonical/active_learning.py`); CLI `benchmark` + `sample`; pure-core tests. | **built** (pure cores verified offline; pytest run pending venv) |
| **2 — Baseline impl** | Wire benchmark + guard into a v2 training path; label round 1 (budgeted); retrain + calibrate v2 on expanded corpus. | **pipeline built + validated live** ([M6_SLICE2_DESIGN.md](M6_SLICE2_DESIGN.md)); awaits label round 1 |
| **3 — Evaluation** | One frozen eval of v2 on the pinned benchmark; label-efficiency + ablation numbers. | todo |
| **4 — Refinement** | Iterate sampling / additional label rounds while budget and returns justify it. | todo |
| **5 — Documentation** | Record v2 in `model_registry`; update `ORG_RESOLUTION` to v2.0; M6 close-out (observed vs. targeted metrics). | todo |

### Slice 2 — what shipped (pipeline; awaits labels)

- `canonical/split_v2.py` — three-way train/calibration/validation split
  (70/15/15), **independently hashed** from v1 (salted; see design §4 for why).
- `canonical/scorer_v2.py` — v2 dataset assembly (benchmark subtracted by set
  membership; provenance defaults) → **early `assert_no_contamination`** →
  fit (logistic regression, feature v3, reusing M5's pure math) → Platt →
  evaluate on the 112 frozen pairs → A/B vs v1.2 + JSON mirrors.
- CLI `stevie fit-v2 [--no-persist] [--model-version …] [--corpus …]`.
- Corpus `v3` registered; `artifacts/metrics/` + `artifacts/calibration/` JSON
  mirrors now git-tracked (joblibs stay ignored).
- Tests: `tests/test_split_v2.py`; full suite **130 passed** on the live env.

**Live validation + ablation baseline (2026-07-02):** M5 v1.2 reproduces at
recall 0.645 exactly. With the pipeline built but **no active-learning labels
yet** (corpus v3 == v2), the control model `v2-ablation` scores **recall 0.613 /
precision 0.905** on the frozen benchmark — *below* v1.2's 0.645 recall, because
the new validation split removes 76 pairs from training. This is the honest
starting line: **active learning must first recover ~0.03 recall before any gain
is real.** The guard fired correctly (pool disjoint); re-persist refused (frozen).

### Slice 1 — what shipped

- `canonical/benchmark.py` — pure `build_frozen_pairs`, `freeze` (once,
  overwrite-guarded), `verify` (digest + label cross-check; corpus growth never
  fails it), and `assert_no_contamination` (the regression guard).
- `canonical/active_learning.py` — deterministic `rank_by_uncertainty` (|p−0.5|,
  key tie-break) + `select_queue` (optional deterministic random-mix to counter
  sampling bias); `run_sample` reads `model_predictions`, excludes
  benchmark/labeled/decided pairs, writes a queue JSONL. Read-only; writes
  nothing to the DB.
- CLI: `stevie benchmark [--freeze]`, `stevie sample --model-version v1.2 --limit N`.
- Tests: `tests/test_benchmark.py`, `tests/test_active_learning.py` (14 pure-core
  checks green under a stdlib harness; run under pytest once the venv exists).
- Artifact: `gold/frozen_benchmark_v1.jsonl` + `.manifest.json` (committed).

---

## Kickoff checklist

- [x] M6 branch created (`m6-active-learning`)
- [x] Repo hygiene: normalized whole-repo CRLF churn → LF, added `.gitattributes`, removed stray `27`
- [x] Objective chosen and single (active learning → v2 retrain)
- [x] Baseline metrics captured from frozen `model_registry`
- [ ] **Live baseline reproduction** — blocked on env: `make install && make db && make migrate`, then `make test` + `stevie evaluate --model-version v1.2`
- [ ] Slice 1 begins only after live baseline reproduces the §0 table
