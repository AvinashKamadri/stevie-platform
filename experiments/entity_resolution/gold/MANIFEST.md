# M2 Gold Evaluation Dataset — Manifest

**Purpose:** Ground truth for evaluating any entity-resolution candidate generator and
scorer. Valid regardless of how M4 ultimately generates candidates, because it is built
from the full discoverable search space, not from the current `entity_candidates` table.

**Why not from entity_candidates:** M1 proved that table captures ~10% of pairs
discoverable under its own blocking threshold (27,590 generated vs 287,347 discoverable).
A gold set built from it would only measure recall within a flawed candidate set, making
benchmarks worthless for any future generator. Sampling from the full discoverable space
makes this gold set generator-agnostic and permanently valid.

---

## Sampling parameters

| parameter            | value                                                   |
|----------------------|---------------------------------------------------------|
| source               | `organizations` all-pairs name-vs-name blocked scan    |
| blocking operator    | `pg_trgm`, `similarity_threshold = 0.3`, floor ≥ 0.40  |
| total discoverable   | ~287,347 pairs (per M1 coverage scan)                   |
| random seed          | `setseed(0.42)` — reproducible                          |
| high band (≥ 0.70)   | 200 sampled from ~3,345 discoverable                    |
| border (0.55–0.70)   | 200 sampled from ~81,000 discoverable                   |
| low (0.40–0.55)      | 100 sampled from ~202,000 discoverable                  |
| **total sample**     | **500 pairs**                                           |

**Band rationale:**
- `high` — recall probe: a good generator must surface all true merges here
- `border` — threshold calibration: where precision/recall tradeoffs are sharpest
- `low` — precision floor probe: mostly noise; verifies we don't over-merge

---

## Files

| file                  | description                                              |
|-----------------------|----------------------------------------------------------|
| `../m2_sample.sql`    | one-time DDL — creates `m2_gold_sample` in the DB        |
| `../label.py`         | interactive labeling CLI (reads DB, saves back, exports) |
| `pairs.jsonl`         | archived labeled pairs — committed after gate is cleared |

---

## Pair identity (stable across rebuilds)

Pairs are keyed by `(key_a, key_b)`:
- `key_a = least(norm_key_a, norm_key_b)` — lexicographically smaller
- `key_b = greatest(norm_key_a, norm_key_b)`

`norm_key` is a deterministic pure function of the org name (frozen at canonical-v2).
Both keys survive full rebuilds — the pair identity is rebuild-stable.

---

## pairs.jsonl schema

One JSON object per line:

```
key_a        string   norm_key of org A (lexicographically smaller)
key_b        string   norm_key of org B
name_a       string   display name of org A at sample time
name_b       string   display name of org B at sample time
sim          float    pg_trgm name-vs-name similarity (0.40–1.00)
band         string   "high" | "border" | "low"
rec_count_a  int      recognitions org A appears in (any role)
rec_count_b  int      recognitions org B appears in (any role)
countries_a  array    distinct country names for org A's recognitions
countries_b  array    distinct country names for org B's recognitions
label        string   "merge" | "distinct"
reason       string?  optional free-text rationale
labeled_by   string   reviewer username
labeled_at   string   ISO 8601 timestamp
```

---

## M2 gate (milestone complete when)

- [ ] >= 500 pairs labeled (tracked by `python label.py status`)
- [ ] Each band has >= 50 labeled pairs
- [ ] Spot-check: 20 random pairs re-reviewed independently; agreement >= 90%
- [ ] `pairs.jsonl` committed to this directory
- [ ] `PHASE_E_DESIGN.md` M2 entry updated to `✅ DONE`

---

## How to label

```bash
# Step 1: materialize the sample (one time)
docker exec stevie-pg psql -U stevie -d stevie_platform \
    -f - < experiments/entity_resolution/m2_sample.sql

# Step 2: label pairs interactively
python experiments/entity_resolution/label.py

# Check progress
python experiments/entity_resolution/label.py status

# Archive labeled pairs to this directory
python experiments/entity_resolution/label.py export
```

During labeling, choose:
- **m** (merge): these two org names refer to the same brand / legal entity
- **d** (distinct): genuinely different organizations (subsidiary ≠ parent,
  regional office ≠ global brand, etc.)
- **s** (skip): uncertain; come back later
- **q** (quit): save progress and exit (session is always resumable)

---

## Labeling heuristics

**Lean merge when:**
- Same name, different punctuation/spacing/casing only (`Cramer-Krasselt` / `Cramer Krasselt`)
- Token reorder only (`Red Havas` / `Havas Red`, `EY GDS` / `GDS EY`)
- Obvious abbreviation of the same brand (`IBM` / `International Business Machines`)
- One is clearly a historical alias of the other

**Lean distinct when:**
- Geographic specificity differs and that specificity is meaningful (`Cisco Systems` / `Cisco Systems India`)
- One is a subsidiary / holding / regional arm of the other
- Different legal suffixes pointing to genuinely different entities (`Corp` vs `Ltd` in different countries)
- Clearly different industries or recognition contexts

**When uncertain:** use `s` (skip) or label as `distinct` with reason `uncertain`.
A conservative gold set (more `distinct`) is preferable to over-merging.
