# Organization-Resolution Labeling Guide

Authoritative rules for labeling gold pairs (merge / distinct / related). Written
2026-07-02 before recording M6 active-learning round 1, so decisions stay
consistent across rounds and future milestones (M7+). If a case isn't covered
here, add the rule here *first*, then label.

## Decision table

| Situation | Label |
|---|---|
| Same legal entity (formatting / typo / legal suffix / acronym expansion differences) | **merge** |
| Same company, different office / location (same legal entity) | **merge** |
| Parent ↔ subsidiary | **related** |
| Brand ↔ company | **related** |
| Product ↔ company | **related** |
| Same brand, **different country** (separate regional legal entities) | **distinct** |
| Competitors / unrelated orgs sharing a token | **distinct** |
| Joint / co-submission entry vs a solo org (or another joint entry) | **distinct** |

## The two rules that dominated round 1

- **P1 — Same brand, different country → `distinct`**, unless both records clearly
  refer to the *same legal entity* despite different location metadata. Country is
  a strong indicator of a separate legal entity; merging at the brand level would
  destroy the ability to distinguish regional subsidiaries — undesirable for an
  organization-resolution system.
  *Distinct:* DHL Express Germany ↔ DHL Express India · TCS UK ↔ TCS India ·
  MetLife Japan ↔ MetLife US.

- **P2 — Parent/subsidiary or brand/product → `related`, not merged.** Merge only
  when the records identify the *same legal organization*.

- **Same legal entity, different office → `merge`.** e.g. NCR Dayton ↔ NCR Duluth
  are one company at two addresses.

## Notes

- `related` is **reported, never trained as a positive** (see split.py / scorer).
  It exists to seed the future relationship graph, not to inflate merge recall.
- When genuinely undecidable from the available evidence (names + countries +
  record counts), **hold the pair for human review** rather than guessing — a
  wrong gold label is worse than a deferred one.
- Provenance is recorded on every label (`source`, `review_round`) so a round's
  contribution — and any later correction — is auditable.
