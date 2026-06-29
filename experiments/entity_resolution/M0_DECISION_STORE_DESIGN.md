# M0 — Durable Decision Store & Merge-Execution Model (Design)

**Status:** design only. Proposed DDL lives in
`proposed_migration_009_merge_graph.sql` in *this* directory — deliberately NOT
in `migrations/`, so `make migrate` will not apply it until we choose to promote.
**Goal:** make human (and deterministic) merge decisions a durable, replayable
*input* to canonicalization — never state that lives inside generated tables.

> Reproducibility model being preserved:
> **raw archive + deterministic rules + durable human decisions → canonical DB**
> Human decisions are treated exactly like normalization rules: external inputs
> replayed on every rebuild.

---

## 1. The problem this fixes

`truncate_canonical` (`canonical/ops.py:18`) wipes `entity_candidates`
`restart identity cascade` on every `cli canonicalize`. So today:

- candidate `id`s are not stable across rebuilds, and
- any `accepted` / `reviewed_by` decision stored there is destroyed on the next
  rebuild.

That violates the golden rule (`migrations/002_canonical.sql:189`): *a full
rebuild stays a pure function of (raw_pages + overrides)*. Merge decisions must
move out of the generated tables into a durable input store, keyed by a stable
identity.

---

## 2. The identity-key choice (chosen carefully, per the refinement)

A merge decision must reference org identity by a key that **survives a full
rebuild**. The candidates:

| key | stable across rebuild? | verdict |
|---|---|---|
| surrogate `organizations.id` | ❌ regenerated (`restart identity`) | reject |
| `slug` | ❌ regenerated; collisions disambiguated by insert order (`_unique_slug`) | reject |
| `raw_name` / first-seen string | ✅ (archive data) but **N:1** to identity | not an identity key |
| **`norm_key`** | ✅ deterministic output of `normalize_org` | **chosen** |

**Decision: key merge decisions by `norm_key`.** It is the granularity at which
org rows actually exist (`organizations.norm_key` is `unique`), and it is a pure
deterministic function of the raw string + the frozen normalization rules.

**The one caveat, and its mitigation.** `norm_key` is stable *only while the
normalization rules are fixed*. If a future phase changes `normalize_org`, some
keys shift, and a decision could reference a `loser_key` that no longer maps to
any org. Mitigations baked into the schema:

1. Store, alongside each decision, a **witness**: a representative `raw_name` and
   the recognition-count/context at decision time. This lets a decision be
   re-validated or migrated if rules change — the decision is not opaque.
2. On replay, a `loser_key` (or `winner_key`) that matches **no** current org is
   an **orphaned decision** — surfaced by a data-quality gate (§5), never
   silently dropped.
3. Normalization-rule changes are themselves versioned and frozen per canonical
   snapshot, so key drift is a deliberate, reviewed event — not an accident.

---

## 3. The directed merge graph (three objects)

Per the implementation suggestion — a directed graph, not row overwrites.

```
organizations                 (existing)   the entity rows; identity = norm_key
organization_merge_decision   (NEW, input) durable edges: loser_key -> winner_key
organization_alias            (NEW, derived) materialized retired_key -> canonical org
```

### 3.1 `organization_merge_decision` — the durable INPUT

The external, never-truncated record of every accept/reject. One row per
decision; directed (`loser_key → winner_key`).

```sql
create table organization_merge_decision (
    id            bigserial primary key,
    loser_key     text not null,          -- norm_key folded away
    winner_key    text not null,          -- surviving brand's norm_key
    decision      text not null check (decision in ('merge','distinct')),
    source        text not null check (source in ('manual','deterministic')),
    confidence    numeric,                -- score at decision time (provenance)
    reason        text,                   -- human / feature note
    loser_witness text,                   -- representative raw_name at decision time
    reviewed_by   text not null,
    reviewed_at   timestamptz not null default now(),
    check (loser_key <> winner_key),
    unique (loser_key)                    -- one fate per losing key
);
```

- `decision='distinct'` is recorded too: a reviewed "these are NOT the same"
  permanently suppresses the pair from resurfacing in review. (`winner_key` for a
  `distinct` row names the other side of the pair; it does not redirect identity.)
- `source` separates human judgments from any future deterministic auto-merges
  so each can be audited / rolled back independently.
- **This table is excluded from `truncate_canonical`.** It is an input, like
  `overrides`.

### 3.2 `organization_alias` — the derived PROJECTION

Generated during `canonicalize` (and safe to truncate/rebuild with the rest of
canonical). It exists so **external consumers' stable keys never 404**: if
`norm_key='ibm armonk ny'` was a real org with a `slug`, and it merges into
`ibm`, the old slug must still resolve.

```sql
create table organization_alias (
    id              bigserial primary key,
    alias_norm_key  text not null,        -- a retired key
    alias_slug      text,                  -- the retired slug (consumer redirects)
    organization_id bigint not null references organizations (id),  -- surviving row
    via             text not null check (via in ('merge_decision','deterministic')),
    unique (alias_norm_key)
);
```

`organization_alias` is the cache; `organization_merge_decision` is the source of
truth. The alias table is fully regenerable from decisions + the org set.

---

## 4. How `canonicalize` replays decisions (composition of two functions)

The merge graph is a second deterministic function on the `norm_key` space,
composed *after* deterministic normalization:

```
raw org string
   │  normalize_org()                 ← deterministic rules (Phase D, frozen)
   ▼
norm_key            (the row-level key)
   │  merge closure  ← replay organization_merge_decision (decision='merge')
   ▼
canonical norm_key  (the resolved brand)
```

### 4.1 Build the closure once per run

At the start of `canonicalize`, after loading decisions:

1. Load all `decision='merge'` edges `loser_key → winner_key`.
2. Build connected components via **union-find**; the canonical representative of
   a component is the root reached by following winner edges (chains
   `A→B, B→C` collapse to `C`).
3. **Cycles are forbidden at insert time** (app-level guard / trigger): adding an
   edge that would close a cycle is rejected, keeping the graph a forest. The
   replay therefore always terminates with a unique representative per component.
4. Produce an in-memory map `resolve_key: norm_key → canonical norm_key`
   (identity for keys not in any merge).

### 4.2 Apply at org resolution

In the org-resolution path (today `pipeline.py:143-151` → `ops.get_or_create_org`),
insert one deterministic step:

```
nk, disp, suffix = normalize_org(...)        # unchanged
nk = resolve_key(nk)                          # NEW: apply merge closure
org_id, created = get_or_create_org(conn, nk, ...)
```

Because every record that maps to any key in a merge component now resolves to
the **same** canonical `norm_key`, they collapse into one org row — no row is
ever deleted or updated-away. The losing keys become `organization_alias` rows
(emitted in the same pass) so old slugs redirect.

### 4.3 Properties this preserves

- **Reproducible:** output is a pure function of (raw_pages + frozen rules +
  decisions). Same inputs → same canonical graph, regardless of ingest order.
- **Reversible:** delete a decision row, rebuild — the merge is undone, the
  separate orgs return. Nothing was destroyed.
- **Non-destructive:** merges change *identity resolution*, never recognition
  count. Recognitions stay invariant (the Phase D discipline: 84,495 constant).
- **Order-independent:** the closure is computed before processing records, so
  the result does not depend on which org is seen first.

---

## 5. Data-quality gates to add (M5 will enforce)

- **Orphaned decision:** every `loser_key`/`winner_key` in
  `organization_merge_decision` must match a known pre-merge `norm_key` (else the
  rules drifted — surface it).
- **No cycles / no chains-to-missing-root:** every merge component resolves to
  exactly one representative.
- **Recognition invariance:** total `recognitions` unchanged before/after the
  merge replay.
- **Alias coverage:** every retired `norm_key` has an `organization_alias` row
  (no consumer key silently disappears).

---

## 6. Scope of pipeline change when this is promoted (estimate)

Small and localized:

- `migrations/009_merge_graph.sql` — the two tables (promote the proposed file).
- `canonical/ops.py` — exclude the decision table from `truncate_canonical`;
  add alias upserts.
- `canonical/pipeline.py` — load decisions + build closure once; one
  `resolve_key(nk)` call in the org path; emit aliases.
- `canonical/normalize.py` — optional: a pure `merge_closure()` helper (testable
  without a DB, mirroring the existing pure-function discipline).
- `canonical/gates.py` — the four gates in §5.

No change to the parser, the archive, or any controlled-dimension logic.

---

## 7. Open questions for the migration sketch review

1. Cycle prevention: app-level guard vs a DB trigger? (Leaning app-level + a
   `distinct`-aware uniqueness, to keep the schema portable.)
2. Should `organization_alias` also carry the retired `raw_name`s for richer
   consumer redirects, or is `alias_slug` enough?
3. Representative-selection rule when reviewers disagree on direction over time —
   latest-decision-wins (by `reviewed_at`) vs explicit supersede. (Leaning:
   `unique(loser_key)` + explicit re-decision overwrites.)
