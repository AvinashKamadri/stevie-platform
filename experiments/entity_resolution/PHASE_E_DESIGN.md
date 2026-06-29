# Phase E — Fuzzy Entity Resolution (Design)

**Status:** design only. No pipeline code changes proposed here are applied yet.
**Baseline:** `canonical-v2` (commit `376b661`, tag `canonical-v2`).
**Authoritative workload:** **27,592** pending org candidate pairs — the count
frozen in `snapshots/canonical-v2/MANIFEST.md` and reported by
`entity_candidates where accepted is null`. (The earlier "43,849 / 90.3%"
figure was an *experiment-time* estimate against the pre-promotion data; it is
superseded by the 27,592 now in the table.)

This is no longer a parsing or normalization problem. Deterministic rules are
exhausted. What remains is an **entity-linking** problem: deciding, for genuinely
ambiguous pairs, whether two org identities are the same brand — and recording
that decision durably and reversibly.

---

## 1. Architecture audit — how the current candidate system actually works

Answers to the four questions, with code evidence.

### 1.1 What is `entity_candidates`? How is it populated?

`migrations/004_candidates.sql`. It is a **mention → entity** table, *not* a
symmetric entity ↔ entity pair table:

```
parsed_record_id    -- the source record whose org string is being placed
raw_value           -- that source string (e.g. "Cisco Systems Pvt Ltd")
candidate_entity_id -- an EXISTING organization row it might equal
score numeric       -- a single similarity score
algorithm text      -- 'trgm' today
accepted boolean    -- null = unreviewed; t/f = decision
reviewed_by, reviewed_at
```

Population — `src/stevie_platform/canonical/pipeline.py:153-157`: candidates are
generated **only when an org is newly created** (a new `norm_key`). At that
moment `ops.org_candidates` (`ops.py:128-137`) runs:

```sql
select id, similarity(name, $raw) as score
from organizations
where id <> $new_id and name %% $raw
order by score desc limit 5
```

with `pg_trgm.similarity_threshold = 0.3` set on the connection
(`pipeline.py:215`, governs the `%` operator) and a stricter Python `floor = 0.4`,
capped at the top 5 matches.

Two consequences worth designing around:

- **It scores the *raw* incoming string against existing *cleaned* `name`s.**
  `org_candidates` is called with `org_raw` (`pipeline.py:155`), which still
  carries location/suffix noise. So shared location tokens ("New York") inflate
  scores and legal suffixes deflate them — the trigram score is computed on
  mismatched representations.
- **Coverage is creation-order-dependent.** A pair (A,B) is recorded once, when
  the *second* of the two is created and compared against the first. This is
  fine for review (each pair appears once) but means the candidate set is a
  by-product of ingest order, not an exhaustive all-pairs scan.

### 1.2 What does each candidate contain?

Closer to the rich form than the bare `(id1, id2)` form, but **mention-keyed**:
it has `parsed_record_id`, `raw_value`, `candidate_entity_id`, `score`,
`algorithm`, and the review columns. Country / program / recognition_count are
**not stored** but are *derivable* by joining `parsed_record_id → recognitions`.
There is no free-text `reason` beyond the algorithm name, and no symmetric
`org_a / org_b` pairing.

### 1.3 What scoring already exists?

Only `pg_trgm` trigram similarity (the GIN index `organizations_name_trgm`,
`002_canonical.sql:79`). No Levenshtein, no token-set/Jaccard, no embeddings, no
blocking strategy beyond the trgm threshold + `limit 5`. **Improve this; don't
replace the plumbing** — pg_trgm blocking is a good candidate generator.

### 1.4 How are merges represented? (the critical finding)

**They are not.** There is currently no merge model and no executor:

- Nothing consumes `accepted = true`. Flipping the flag has no downstream effect
  — no code merges two `organizations` rows, repoints `parties`, or rewrites
  `recognition_parties`. `entity_links` (`002_canonical.sql:170`) is a *match
  ledger* (audit of where each mention resolved); it does not redirect identity.
- `organizations` has no `merged_into_id` / `canonical_id` column. There is no
  `organization_merge` or alias table.
- **Phase D's −14.3% org reduction did not come from this table at all.** It came
  from changing `norm_key` upstream (`normalize_org` in `normalize.py:206`), so
  duplicates collapse *at creation time* and never become two rows.
  `entity_candidates` was a bystander.

### 1.5 The rebuild hazard (must fix before any human review)

`truncate_canonical` (`ops.py:18-25`) truncates `entity_candidates` along with
the rest of canonical, `restart identity cascade`, on every `cli canonicalize`.
Therefore:

- candidate `id`s are **not stable** across rebuilds;
- any `accepted` / `reviewed_by` decision written into `entity_candidates` is
  **destroyed** by the next full rebuild.

This breaks the project's golden rule (`002_canonical.sql:189`): *"a full rebuild
stays a pure function of (raw_pages + overrides)."* Human review is expensive and
must survive rebuilds. The existing `overrides` table is the established pattern
for "human knowledge as DATA, keyed to a stable slug" — and it is the right home
for merge decisions, keyed by `norm_key`/`slug`, never surrogate id.

### Audit verdict

The candidate **generator** is ~70% there (pg_trgm blocking is sound and reused).
The **scoring** is thin (single trgm on noisy raw strings). The **resolution
half does not exist**: no durable decision store, no merge model, no executor.
Phase E is mostly about building the resolution half *correctly and reversibly*,
plus upgrading scoring — not rewriting the generator.

---

## 2. Design principles (inherited from the project)

1. **Rebuild stays pure.** Review decisions live in a rebuild-stable store keyed
   by `norm_key`, replayed on every `canonicalize` — exactly like `overrides`.
   Never store truth in a table that `truncate_canonical` wipes.
2. **Non-destructive merges.** Never `DELETE`/`UPDATE` away a losing org. Model
   merges as `loser_key → winner_key` aliases; identity is redirected at
   resolution time, and the original is always recoverable (same discipline as
   `raw_name` / `legal_suffix` in Phase D).
3. **Deterministic given the same inputs.** Scoring is reproducible; the only
   non-determinism allowed is the human's accept/reject, which is itself
   recorded data.
4. **Measure before promoting.** A labeled gold set gates every scoring change;
   promotion only happens when precision/recall clear an explicit bar.
5. **Experiment → measure → review → promote → freeze** (the canonical-v2
   workflow).

---

## 3. Proposed architecture

### 3.1 Durable decision store (the missing piece)

A new rebuild-stable table — decisions keyed by stable keys, NOT surrogate ids:

```sql
-- migration 009 (proposed)
create table org_merge_decisions (
    id            bigserial primary key,
    loser_key     text not null,          -- norm_key of the org folded away
    winner_key    text not null,          -- norm_key of the surviving brand
    decision      text not null check (decision in ('merge','distinct')),
    confidence    numeric,                -- score at decision time (provenance)
    reason        text,                   -- human/feature note
    reviewed_by   text not null,
    reviewed_at   timestamptz not null default now(),
    unique (loser_key)                    -- one fate per losing key
);
```

`distinct` decisions are recorded too — a reviewed "these are NOT the same"
suppresses the pair from ever resurfacing. This table is **never truncated** by
`truncate_canonical`.

Resolution becomes a deterministic replay step inside `canonicalize`: when
resolving an org, if its `norm_key` is a `loser_key` with `decision='merge'`,
resolve to the winner's `norm_key` instead. Implemented as a key-rewrite in
`normalize_org`'s output / `get_or_create_org` lookup — same shape as the
existing deterministic rules, so churn is small.

> Alternative considered: store decisions in the existing `overrides` table
> (`entity_type='organization', field='merge_into'`). Pro: zero new tables. Con:
> `overrides` is keyed by `entity_slug` (regenerated on rebuild) and its value is
> opaque jsonb; a dedicated table gives a `unique(loser_key)` guarantee and
> cleaner querying. **Recommendation: dedicated table**, but the override route
> is viable if we want zero schema additions.

### 3.2 Upgraded scoring (improve, don't replace)

Keep pg_trgm as the **blocker** (cheap candidate generation via the GIN index).
Add a composite **scorer** over each blocked pair, computed on *normalized cores*
(not raw strings):

| signal | source | rationale |
|---|---|---|
| trigram similarity | `pg_trgm` on `name` cores | current baseline, keep |
| token-set ratio | Python (sorted token Jaccard) | robust to word order / extra tokens |
| Levenshtein (normalized) | `fuzzystrmatch` or Python | catches typos pg_trgm misses |
| shared country | join via `parsed_record_id` | strong positive signal |
| shared program/category | same | weak positive |
| legal_suffix conflict | `organizations.legal_suffix` | weak negative (already modeled) |

Combine into one score (start with a transparent weighted sum; only consider a
learned model if the gold set says the linear combo plateaus). Store the
component scores in a richer candidate payload (jsonb) so the review UI and the
gold-set analysis can see *why* a pair scored as it did. **No embeddings in
Phase E** unless the gold set proves lexical signals plateau — keep it
deterministic and dependency-light, per the README.

### 3.3 Candidate payload enrichment

Either widen `entity_candidates` (add `features jsonb`, `winner_key`,
`loser_key`) or compute pairs into an experiment table first
(`experiments/entity_resolution/`) and only promote the schema change once the
scorer is validated. Given principle 4, **start in the experiment harness** (no
migration), mirroring how `org_normalization` was proven before migration 008.

---

## 4. Milestones

Reordered per review: **build the merge system before improving the scorer** —
better scores don't move the system forward until accepted decisions have
somewhere durable and executable to live. Each milestone has an explicit gate.

### M0 — Durable decision store + merge-execution model
Design the rebuild-stable decision store and the directed merge graph; confirm
`truncate_canonical` won't wipe it. No review work begins until decisions have a
safe home. **Detailed in [M0_DECISION_STORE_DESIGN.md](M0_DECISION_STORE_DESIGN.md)**
(+ proposed DDL `proposed_migration_009_merge_graph.sql`).
**Gate:** decision-store + replay design reviewed; migration sketch accepted.

### M1 — Candidate coverage validation ✅ DONE → outcome: **REDESIGN**
All-pairs blocked scan executed on a full archive-v1 rebuild. The current
generator captures only ~10% of pairs discoverable at its own 0.4 floor and
misses 1,364 high-confidence (sim ≥ 0.70) pairs, including sim-1.0 token-reorder
duplicates. **Full results in [COVERAGE_VALIDATION.md](COVERAGE_VALIDATION.md)**
(+ runnable `coverage_scan.sql`).
**Consequence:** candidate generation must be **rebuilt** (folded into M4), not
patched; do not build the review UI (M5) over the current 27,590 set. One cheap
deterministic win surfaced: treat hyphens as separators in `norm_key`.

### M2 — Gold evaluation dataset 🔄 IN PROGRESS
Sample candidate pairs **stratified by score band** (high / borderline / low) so
the set measures precision *and* recall, not just the easy tail. Hand-label each
`merge` / `distinct` with a reason. Store under `gold/`.
**Key decision:** sample from the full discoverable space (287,347 all-pairs at
sim ≥ 0.40), NOT from `entity_candidates` — makes the gold set valid regardless
of how M4 generates candidates. **Detailed in [gold/MANIFEST.md](gold/MANIFEST.md)**
(+ `m2_sample.sql`, `label.py`).
**Gate:** ≥500 labeled pairs; each band ≥50; spot-check agreement ≥90%.

### M3 — Merge-execution model (replay during canonicalize)
Promote migration 009; wire the decision-graph closure into `canonicalize`
(deterministic normalization → merge closure → org resolution, §4 of M0). Prove
reversibility (delete decision → rebuild → split restored) and recognition
invariance on a handful of hand-entered decisions, **before** any scorer work.
**Gate:** replay works end-to-end; the four new gates (§5 of M0) green;
recognitions unchanged.

### M4 — Rebuild candidate generation + composite scorer (experiment harness)
Per the M1 redesign verdict: **replace limit-5-at-creation generation with a
principled all-pairs/blocked scan**, then score each blocked pair with the
composite scorer (§3.2) on normalized cores + structured signals. Token-set ratio
is mandatory — it recovers the sim-1.0 token-reorder duplicates the current
generator misses. Tune weights/thresholds against the M2 gold set; report P/R/F1
per threshold, an auto-merge threshold (precision ≥ ~0.99), and a review band.
The output is a *calibrated* candidate set (high recall at acceptable precision),
not the raw 287k blocked pairs.
**Gate:** documented P/R curve beating the single-trgm baseline; REPORT.md
written (mirrors `org_normalization/REPORT.md`). No embeddings unless the curve
plateaus.

### M5 — Review workflow
A minimal queue over the borderline band: show pair + features + recognition
counts/countries; capture `merge`/`distinct` + reason into the decision store
(now durable, via M3). Highest-impact-first ordering (by combined
recognition_count). **Gate:** a reviewer clears N pairs and decisions persist
across a rebuild.

### M6 — Promote and freeze `canonical-v3`
Apply above-threshold auto-merges + reviewed decisions; run the **8 data-quality
gates** (`gates.py`) — must stay green; recognitions invariant (84,495). Snapshot
manifest under `snapshots/canonical-v3/`, tag the commit, record before/after org
counts + a merge-cluster sample, verify the restore path.
**Gate:** 8/8 gates pass; manifest written; tag pushed; regenerable from
`archive-v1` + decision store.

---

## 5. Risks & mitigations

- **Over-merging distinct brands** (e.g. "Cisco Systems" vs "Cisco Systems
  India" — already distinct keys in `REPORT_suffix.md`). → conservative
  auto-merge threshold (high precision), everything else human-reviewed;
  `distinct` decisions are sticky.
- **Review decisions lost on rebuild** → M0 is a hard prerequisite; nothing is
  reviewed until the durable store exists.
- **Scope creep into embeddings/ML** → explicitly deferred; lexical + structured
  signals first, ML only if the gold set proves a plateau.
- **Candidate set incompleteness** (creation-order coverage, §1.1) → before M2,
  run a one-off all-pairs blocked scan in the experiment harness to confirm the
  27,592 isn't missing obvious pairs; reconcile if it is.

---

## 6. What is explicitly NOT in Phase E

Search, authority pages, embeddings, the nomination assistant, CloudCannon.
Phase E ends at a frozen `canonical-v3` with org identity resolved and every
decision durable and reversible. The intelligence/product layers come after.
