# M1 — Candidate Coverage Validation

**Question being answered (once):** Does `entity_candidates` (27,592 pending
org pairs) represent *all discoverable candidates* under the blocking strategy,
or only the subset that ingest order happened to generate?

**Why it matters:** candidate generation runs *only when an org is newly created*
(`pipeline.py:153`), comparing the new org's **raw** string against existing
org names. So coverage is a by-product of creation order. If discoverable pairs
are missing, reviewers would be working from an incomplete set — wasted effort.
Answer this **before** the gold set (M2) and before any review UI (M5).

## How to run

Requires a DB with `canonical-v2` materialized (`migrate && reparse &&
canonicalize`, or a restore of `archive-v1` + replay). Then:

```bash
psql "$DATABASE_URL" -f experiments/entity_resolution/coverage_scan.sql
# or: docker exec -i stevie-pg psql -U stevie -d stevie_platform -f - < .../coverage_scan.sql
```

> Not yet executed: at design time the local runtime is cold (no Docker daemon,
> no `.env`, no DB). The scan is read-only and ready to run the moment canonical
> is materialized.

## Method (what the SQL does)

1. **All discoverable pairs** — an all-pairs trigram-blocked scan
   (`name %% name`, unordered, deduped) at the generator's own threshold
   (`similarity_threshold = 0.3`, floor `0.4`). This is the "complete" set the
   generator is meant to approximate.
2. **Generated pairs** — reduce existing `entity_candidates` rows to unordered
   org-pairs by resolving each source mention to its org via `entity_links`.
3. **Diff** both directions.

## The three outcomes (decided in advance)

| `discoverable_NOT_generated` | meaning | action |
|---|---|---|
| **0** | generator is complete under blocking | trust 27,592; proceed to M2 |
| **small** (tens–low hundreds) | minor order-dependent misses | patch generator (one all-pairs pass), then review |
| **large** | generation is structurally incomplete | redesign candidate generation before any review |

`generated_NOT_discoverable > 0` is expected and benign: those pairs were scored
**raw-vs-name** (the generator compares the incoming raw string to existing
cleaned names), which the **name-vs-name** all-pairs view doesn't reproduce. The
sample query confirms they're raw-only artifacts, not a defect.

## Known caveat in the comparison

The generator blocks on **raw-vs-name**; this scan blocks on **name-vs-name**.
They are close but not identical, which is itself a finding: scoring on
normalized cores (Phase E / M4) removes this asymmetry. If the gap is large and
dominated by raw-vs-name differences, that is *evidence for* the M4 scorer
redesign, not just a generator patch — record which.

## Result — executed 2026-06-29 on a full archive-v1 rebuild

Environment: `archive-v1` dump restored into a fresh Postgres 16, then
`migrate → reparse → canonicalize`. Deterministic outputs reproduced
`canonical-v2` exactly: **organizations 27,797**, **recognitions 84,495**.

```
all_discoverable_pairs (trgm sim >= 0.4) : 287,347
generated_pairs (current entity_candidates): 27,590
discoverable_NOT_generated               : 266,436   <- 92.7% of discoverable
generated_NOT_discoverable               :   6,679   (raw-vs-name artifacts, expected)
outcome                                  : REDESIGN candidate generation
```

### Verdict: **REDESIGN** — the generator is materially incomplete

The current `entity_candidates` set captures only **~10%** of the pairs
discoverable under its *own* blocking threshold. This is not a rounding gap; the
generator misses obvious true duplicates.

Distribution of discoverable pairs vs. how many the generator **missed**, by
trigram-similarity band:

| sim band | all discoverable | missed (not generated) | missed % |
|---|---:|---:|---:|
| 0.90–1.00 | 324 | 36 | 11% |
| 0.80–0.90 | 743 | 152 | 20% |
| 0.70–0.80 | 2,302 | 1,176 | 51% |
| 0.60–0.70 | 14,249 | 11,671 | 82% |
| 0.50–0.60 | 67,053 | 61,740 | 92% |
| 0.40–0.50 | 202,676 | 191,661 | 95% |

Even in the **high-confidence zone (sim ≥ 0.70)** — where pairs are very likely
real — **1,364 pairs were never generated**. A sample of the missed top-band
pairs (all the highest-similarity, all absent from the review queue):

```
Online Guru                ~ Guru Online                 1.000   (token reorder)
Red Havas                  ~ Havas Red                    1.000   (token reorder)
EY GDS                     ~ GDS EY                        1.000   (token reorder)
Cramer-Krasselt            ~ Cramer Krasselt              1.000   (punctuation)
DHL - SSA Regional Services~ DHL SSA Regional Services    1.000   (punctuation)
BCW-Global                 ~ BCW Global                    1.000   (punctuation)
Catapult PR-IR             ~ Catapult PR & IR             1.000
Canvas Communication...    ~ Canvas Communications...     0.935   (plural)
GeiserMaclang Marketing... ~ Geiser Maclang Marketing...  0.923   (spacing)
```

### Reason for the missing pairs (the question this scan had to answer)

All three structural causes, confirmed:

1. **`limit 5` per new org** (`ops.py:129`) — a hard recall cap; a mention with
   >5 near-neighbours silently drops the rest.
2. **Creation-order dependence** — candidates are generated only when the *second*
   org of a pair is created, comparing it to *already-existing* orgs. Reordering
   ingest changes the set (directly evidenced: this rebuild produced **27,590**
   vs the manifest's **27,592** from the same deterministic pipeline).
3. **Raw-vs-name scoring** (`org_raw` vs cleaned `name`, `pipeline.py:155`) — the
   6,679 `generated_NOT_discoverable` pairs are this asymmetry (benign), but it
   also means scores are computed on noisy strings, distorting the top-5 cut.

A fourth, separate finding: several top-band misses are **punctuation-only**
variants (`Cramer-Krasselt` vs `Cramer Krasselt`). `norm_key` keeps hyphens
(`_NONWORD = [^\w\s-]`, `normalize.py:17`), so these survive as *distinct orgs*
rather than collapsing deterministically. That's a cheap normalization win, not a
fuzzy problem.

### Implications for the roadmap

- **Do not build a review UI over the current 27,590 set** — it omits easy,
  high-confidence wins. Validating this *before* M5 is exactly what M1 was for.
- **The generator must be rebuilt, not patched.** Fold it into the M4 scorer
  work: replace limit-5-at-creation with a principled all-pairs/blocked scan +
  the composite scorer. **Token-set ratio specifically recovers the reorder
  cases** (`Online Guru`/`Guru Online`, `Red Havas`/`Havas Red`, `EY GDS`/`GDS
  EY`) that pure raw-string trigram missed.
- **287,347 is not a review workload.** The 0.40–0.50 band (202k) is dominated by
  coincidental-token noise. The deliverable of M4 is a *calibrated threshold*
  (set on the M2 gold set) that maximizes recall at acceptable precision — not a
  raw dump of all blocked pairs.
- **Quick deterministic win first:** treat hyphens/punctuation as separators in
  `norm_key` to auto-collapse the punctuation-only duplicates before any fuzzy
  step (re-measure org count after).

### Reproduce

DB left running as container `stevie-pg` (Postgres 16, host port 5432, network
`stevie-net`). Re-run: `docker exec -i stevie-pg psql -U stevie -d
stevie_platform -f - < coverage_scan.sql`.
