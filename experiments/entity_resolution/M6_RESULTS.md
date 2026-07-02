# M6 Results — Active Learning, Round 1

Recorded 2026-07-02. Frozen benchmark: `frozen_benchmark_v1` (112 pairs; 108
binary + 4 related). Both models evaluated on the identical pairs. Model family
held fixed (logistic regression, feature_version v3) — **only the training data
changed.**

## The experiment

Does adding uncertainty-sampled labels improve the scorer? Round 1: the 100
most-uncertain pairs (p≈0.500) were labeled per `LABELING_GUIDE.md`; 89 recorded
(17 merge / 61 distinct / 11 related), 11 held as ambiguous. 56 of the recorded
labels landed in the v2 `train` split.

> **Label provenance caveat:** round-1 labels are **AI-assisted** (drafted per the
> agreed policy, `source=active_learning_ai_assisted`), not independently
> human-verified. See "Before promoting" below.

## A/B on the frozen benchmark

| Model | Recall | Precision | F1 | tp | fp | fn | tn |
|---|---|---|---|---|---|---|---|
| v1.2 (M5 baseline) | 0.645 | 0.870 | 0.741 | 20 | 3 | 11 | 74 |
| v2-ablation (v2 pipeline, **no new labels**) | 0.613 | 0.905 | 0.731 | — | — | — | — |
| **v2 (M6, +round-1 labels)** | **0.677** | **0.913** | **0.778** | 21 | 2 | 10 | 75 |

Pre-registered success criteria (M6_PLAN §3): recall > 0.645 **and** precision
≥ 0.88 → **both met.**

## Honest read of the magnitude

- **The recall gain is one merge.** tp 20 → 21 on 31 benchmark positives
  (0.645 → 0.677). On n=31, ±1 TP is within sampling noise — this is
  *direction-correct and criteria-meeting, not decisive.*
- **The precision gain is the more solid signal.** fp 3 → 2; precision
  0.870 → 0.913 (matching v1.1, the best M5 model). The 61 `distinct` labels —
  mostly same-brand-different-country pairs — taught the model to reject a false
  merge. This is exactly what a hard-negative round should do.
- **Attribution is clean.** v2-ablation (same pipeline, no new labels) scores
  0.613; the new labels moved it to 0.677 (+0.064). Because the model and
  features are identical to v1.2, the improvement is attributable to the data.
- **Acronym recall is unchanged (~0).** Round 1 added no acronym-expansion merge
  labels; the M5 acronym information ceiling stands. Untouched, as designed.

## Verdict

**Positive, small, criteria-met.** The active-learning loop works end to end and
moves the benchmark in the right direction on one round of hard cases. The
milestone question — *does active learning improve the scorer?* — is answered
**yes, with a caveat on effect size.**

## Before promoting v2 to production

v2 is **frozen** as the round-1 record but is **not recommended for production
promotion yet**:

1. **Validate the AI-assisted labels** — spot-check the 17 recorded merges and
   the 61 distincts against `LABELING_GUIDE.md`. A wrong gold label is worse than
   a deferred one.
2. **Resolve the 11 held pairs** (listed in the round-1 handoff) and add them.
3. **Run round 2** — one round's +1 TP is not decisive. A second round should
   show whether the recall trend holds or was noise. If recall climbs again with
   precision held, promote; if it flattens, keep v1.2 and investigate.

Production stays **v1.2** until the above clears. Nothing here mutated production
canonicalization — round-1 labels are experiment gold only (not merge decisions).

## Next-round recommendations

- Sample round 2 against **v2** (`stevie sample --model-version v2`) so it targets
  *v2's* new uncertainty region.
- Consider a merge-enriched sampling variant: recall is capped by missed *merges*,
  and this round was 68% negatives. Uncertainty naturally surfaces many regional
  negatives; a complementary strategy that over-samples likely-positive boundary
  cases could move recall faster.
