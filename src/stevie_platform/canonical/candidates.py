"""
Merge-candidate generation (Phase F / M4) — high-recall, explainable blocking.

This is the FIRST stage of entity resolution and its only job is RECALL: surface
every pair of organizations that *might* be the same, cheaply. Precision is the
scorer's problem (a later stage); a true pair missed HERE can never be recovered
downstream, so the blockers err toward over-generating.

Shape (see migration 010):

    blocker_a ┐
    blocker_b ┼─► union ─► dedup (merging `reasons`) ─► persist / measure
    blocker_c ┘

Each blocker is independent and emits `RawPair`s tagged with one reason. Adding a
blocker is additive — no existing blocker changes. The dedup/ordering/token logic
is pure (no DB) so it is unit-tested directly; only `generate`/`persist` touch the
connection. Candidate generation runs as its own batch (`cli candidates`), NOT
inside canonicalize — see the CADENCE note in migration 010.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

# A pair as emitted by a blocker, before ordering/dedup.
RawPair = tuple[str, int, str, int, str]  # (key_a, id_a, key_b, id_b, reason)

# Tokens this short are structural noise (of, &, the, ag, co); rare-token
# blocking on them is meaningless and explosive. norm_key has already stripped
# location and legal suffixes, so a >=4-char token is a real content word.
_MIN_TOKEN_LEN = 4

# Stopwords dropped when forming the stopword-skipping initialism variant, so
# "association for computing machinery" -> "acm" (not "afcm"). MUST mirror
# mine_hard_cases.STOPWORDS: the acronym blocker is justified by a yield study
# (acronym_feasibility_2026-07-01.md) run over the population that miner defines,
# so the two notions of "acronym" have to be the same or the estimate doesn't
# transfer.
_ACRONYM_STOPWORDS = frozenset(
    {"for", "the", "of", "and", "a", "an", "to", "in", "at", "on", "by"})
# An acronym key must be this many chars: >=2 excludes single letters (noise),
# <=7 keeps it an acronym rather than a short name that happens to be one token.
_ACRONYM_MIN, _ACRONYM_MAX = 2, 7


@dataclass(frozen=True)
class Pair:
    """A deduped, canonically-ordered candidate pair (left_key < right_key)."""
    left_key: str
    left_id: int
    right_key: str
    right_id: int
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class BlockerStat:
    """Per-blocker accounting for the efficiency table: how many raw pairs this
    blocker emitted (its share of scorer workload) and how long it took. `emitted`
    is pre-union — it overlaps other blockers, which is exactly the point: a
    blocker emitting 500k pairs to add 2 gold matches isn't worth its runtime."""
    name: str
    emitted: int
    runtime_s: float


def order_pair(key_a: str, id_a: int, key_b: str, id_b: int) -> tuple[str, int, str, int]:
    """Canonicalize an unordered pair so the smaller key is on the left. Makes
    (A,B) and (B,A) the same row and matches how gold pairs / merge decisions are
    keyed (by norm_key, not by org id)."""
    if key_a <= key_b:
        return key_a, id_a, key_b, id_b
    return key_b, id_b, key_a, id_a


def merge_pairs(raw: list[RawPair]) -> list[Pair]:
    """Union blocker outputs into deduped pairs, merging reasons.

    A pair surfaced by two blockers becomes one Pair carrying BOTH reasons — that
    union is what lets the recall harness attribute marginal recall per blocker.
    Self-pairs (same key) are dropped: a blocker should never emit them, but a
    normalization collision could. Reasons are sorted for stable output."""
    merged: dict[tuple[str, str], dict] = {}
    for key_a, id_a, key_b, id_b, reason in raw:
        if key_a == key_b:
            continue
        lk, lid, rk, rid = order_pair(key_a, id_a, key_b, id_b)
        slot = merged.get((lk, rk))
        if slot is None:
            merged[(lk, rk)] = {"lid": lid, "rid": rid, "reasons": {reason}}
        else:
            slot["reasons"].add(reason)
    return [
        Pair(lk, v["lid"], rk, v["rid"], tuple(sorted(v["reasons"])))
        for (lk, rk), v in merged.items()
    ]


def content_tokens(norm_key: str, min_len: int = _MIN_TOKEN_LEN) -> list[str]:
    """Content tokens of a norm_key, dropping structural short tokens. Pure mirror
    of the SQL the rare-token blocker runs server-side (kept in sync for tests)."""
    return [t for t in norm_key.split() if len(t) >= min_len]


def _initials(tokens: list[str], *, skip_stop: bool) -> str:
    """Initialism of a tokenized name. skip_stop drops stopwords first, so an
    expansion yields two candidate acronyms (plain + stopword-skipping). Mirror
    of mine_hard_cases._initials."""
    src = [t for t in tokens if not (skip_stop and t in _ACRONYM_STOPWORDS)]
    return "".join(t[0] for t in src if t)


def acronym_pairs(orgs: list[tuple[str, int]]) -> list[RawPair]:
    """Pair a single-token acronym org with any multi-token org whose initials
    spell it — e.g. ('ibm') <-> ('international business machines'). Both the
    plain and stopword-skipping initialisms of every expansion are indexed, so
    'acm' matches 'association for computing machinery' too.

    Pure (no DB) so it is unit-tested directly; `acronym_blocker` is the thin DB
    wrapper. orgs is (norm_key, id) rows. Emit volume is bounded by the number of
    real acronym orgs (~1k here), and every pair it finds has trigram similarity
    ~0.03 — invisible to the other blockers, so this is net-new recall, not
    overlap. Mirrors mine_hard_cases's acronym rule (same population as the yield
    study that justified this blocker)."""
    # Index expansions by both initialism forms.
    initials_idx: dict[str, list[tuple[str, int]]] = {}
    for key, oid in orgs:
        toks = key.split()
        if len(toks) < 2:
            continue
        for ini in {_initials(toks, skip_stop=False), _initials(toks, skip_stop=True)}:
            if _ACRONYM_MIN <= len(ini) <= _ACRONYM_MAX:
                initials_idx.setdefault(ini, []).append((key, oid))
    # Match each single-token alpha org against expansions spelling it.
    out: list[RawPair] = []
    for key, oid in orgs:
        toks = key.split()
        if len(toks) == 1 and key.isalpha() and _ACRONYM_MIN <= len(key) <= _ACRONYM_MAX:
            for exp_key, exp_id in initials_idx.get(key, []):
                out.append((key, oid, exp_key, exp_id, "acronym"))
    return out


# --- blockers ---------------------------------------------------------------
# Each returns RawPairs tagged with its reason. They are deliberately cheap and
# over-generous; the union is deduped by merge_pairs.

async def trigram_blocker(conn, *, threshold: float = 0.4) -> list[RawPair]:
    """Pairs whose display names are trigram-similar. Uses the existing
    organizations_name_trgm GIN index via the `%` operator; set_limit() sets the
    session similarity threshold (recall/cost knob) — `SET` can't take a bind
    param, set_limit() can. `a.id < b.id` yields each unordered pair once at the
    SQL level (re-ordered by key in merge_pairs).

    NOTE: the join query is sent WITH NO params, so psycopg passes it verbatim —
    the trgm operator is a single `%` here (no `%%` escaping, which only applies
    when params are interpolated)."""
    await conn.execute("select set_limit(%s::real)", (threshold,))  # set_limit takes real, not float8
    cur = await conn.execute(
        """select a.norm_key lk, a.id lid, b.norm_key rk, b.id rid
             from organizations a
             join organizations b
               on a.id < b.id and a.name % b.name"""
    )
    return [(r["lk"], r["lid"], r["rk"], r["rid"], "trigram") for r in await cur.fetchall()]


async def rare_token_blocker(conn, *, max_doc_freq: int = 5,
                             min_token_len: int = _MIN_TOKEN_LEN) -> list[RawPair]:
    """Pairs of orgs that share a RARE content token (one appearing in <=
    max_doc_freq orgs). A distinctive shared word ("grazitti", "mslgroup") is
    strong evidence even when trigram similarity is low (word order, prefixes).
    Bounded by construction: a token with frequency k yields <= k*(k-1)/2 pairs,
    and k <= max_doc_freq, so the blow-up per token is tiny."""
    cur = await conn.execute(
        """with toks as (
               select id, norm_key,
                      unnest(string_to_array(norm_key, ' ')) tok
                 from organizations),
             content as (
               select id, norm_key, tok from toks where length(tok) >= %s),
             rare as (
               select tok from content group by tok having count(*) <= %s)
           select a.norm_key lk, a.id lid, b.norm_key rk, b.id rid
             from content a
             join rare r on r.tok = a.tok
             join content b on b.tok = a.tok and a.id < b.id""",
        (min_token_len, max_doc_freq),
    )
    return [(r["lk"], r["lid"], r["rk"], r["rid"], "rare_token") for r in await cur.fetchall()]


async def acronym_blocker(conn) -> list[RawPair]:
    """Acronym <-> expansion pairs (ibm <-> international business machines). The
    initials logic — especially the stopword-skipping variant — is awkward in SQL
    and the org table is small, so we pull (norm_key, id) and match in Python via
    the pure `acronym_pairs`. This closes the one real blocking gap M4 measured:
    a uniform-random yield study projected 161-359 recoverable merges here, none
    of which the trigram/rare_token blockers can reach (sim ~0.03)."""
    cur = await conn.execute("select norm_key, id from organizations")
    orgs = [(r["norm_key"], r["id"]) for r in await cur.fetchall()]
    return acronym_pairs(orgs)


# The active blocker set. Append here to add a strategy — union/dedup is automatic.
BLOCKERS = [
    ("trigram", trigram_blocker),
    ("rare_token", rare_token_blocker),
    ("acronym", acronym_blocker),
]


async def generate(conn) -> tuple[list[Pair], list[BlockerStat]]:
    """Run every blocker, union + dedup, return (canonical pairs, per-blocker
    stats). No writes. Stats carry the emitted volume + runtime each blocker
    costs, the denominator of the efficiency table the recall harness prints."""
    raw: list[RawPair] = []
    stats: list[BlockerStat] = []
    for name, blocker in BLOCKERS:
        t0 = time.perf_counter()
        pairs = await blocker(conn)
        dt = time.perf_counter() - t0
        stats.append(BlockerStat(name, len(pairs), round(dt, 2)))
        print(f"[candidates] {name}: {len(pairs)} raw pairs in {dt:.1f}s")
        raw.extend(pairs)
    merged = merge_pairs(raw)
    print(f"[candidates] {len(merged)} unique pairs after dedup")
    return merged, stats


async def persist(conn, pairs: list[Pair]) -> int:
    """Full recompute: truncate the derived table and bulk-insert. Truncating is
    safe — this table is regenerable (see migration 010)."""
    await conn.execute("truncate organization_merge_candidate restart identity")
    if not pairs:
        return 0
    async with conn.cursor() as cur:
        await cur.executemany(
            """insert into organization_merge_candidate
                 (left_key, left_org_id, right_key, right_org_id, reasons)
               values (%s,%s,%s,%s,%s)""",
            [(p.left_key, p.left_id, p.right_key, p.right_id, list(p.reasons))
             for p in pairs],
        )
    return len(pairs)


async def org_count(conn) -> int:
    cur = await conn.execute("select count(*) n from organizations")
    return (await cur.fetchone())["n"]


async def run_candidates(*, persist_rows: bool = True) -> dict:
    """CLI entry: generate candidates and (optionally) persist them. Prints the
    blocking metrics that tell us whether a change improved recall or just shifted
    work to the scorer: total pairs, avg candidates per org."""
    from stevie_platform import db
    p = await db.pool()
    async with p.connection() as conn:
        n_orgs = await org_count(conn)
        pairs, _stats = await generate(conn)
        if persist_rows:
            await persist(conn, pairs)
            await conn.commit()
    avg = (2 * len(pairs) / n_orgs) if n_orgs else 0.0  # each pair touches 2 orgs
    summary = {
        "organizations": n_orgs,
        "candidate_pairs": len(pairs),
        "avg_candidates_per_org": round(avg, 2),
        "persisted": persist_rows,
    }
    print("\n" + "=" * 52)
    print(" CANDIDATE GENERATION")
    print("=" * 52)
    print(f"  organizations         {n_orgs:>10,}")
    print(f"  candidate pairs       {len(pairs):>10,}")
    print(f"  avg candidates / org  {avg:>10.2f}")
    print(f"  persisted             {str(persist_rows):>10}")
    print("=" * 52 + "\n")
    return summary
