# Stevie Platform — `canonical-v2`

**Frozen:** 2026-06-27
**Git commit:** `376b661d1db0b5e83d7e134349bf863a977d3d24` (tag `canonical-v2` on master)
**Regenerable:** yes — from the `archive-v1` raw dump + this commit:
`cli migrate && cli reparse && cli canonicalize`. No re-crawl, no dump needed
(the raw archive is the only non-regenerable asset; it is frozen in archive-v1).

## What changed vs archive-v1 baseline

Deterministic brand-level org normalization promoted into the canonical
pipeline (Phase D, location + corporate-suffix rules). Org identity is now
keyed at the **brand** level; the original string and legal form are preserved.

| | archive-v1 | canonical-v2 |
|---|---|---|
| organizations (created)   | 32,446 | **27,797**  (−4,649, −14.3%) |
| candidate merges generated| 52,781 | **27,592**  (−25,189, −47.7%) |
| recognitions built        | 84,495 | 84,495 |
| canonicalization failures | 0 | 0 |
| data-quality gates        | 8/8 pass | **8/8 pass** |

## Org schema (migration 008)

- `norm_key`     — brand-level dedup key (location + legal-suffix stripped), e.g. `cisco systems`
- `name`         — cleaned display name, e.g. `Cisco Systems`
- `raw_name`     — first-seen original string (100% populated)
- `legal_suffix` — stripped legal form (Inc./Ltd./GmbH/…), 15.7% of orgs
- per-occurrence original names also remain in `recognition_parties.raw_value`

## Normalization rules (deterministic, tested)

- **Location rule:** strip trailing segments matching the record's own
  city/state/country or a US-state/country gazetteer. −11.0% orgs standalone.
- **Suffix rule:** strip legal-entity suffixes; brand-level model. 96
  cross-legal-form merges reviewed (REVIEW_96_legal_forms.md); non-lossy via
  legal_suffix. Combined effect slightly super-additive.
- Evidence: `experiments/org_normalization/` (REPORT.md, REPORT_suffix.md);
  23 unit tests in tests/ + experiments/.

## Next phase

Fuzzy entity resolution over the remaining ~27,592 candidate pairs — the
genuinely ambiguous cases left after deterministic normalization.
