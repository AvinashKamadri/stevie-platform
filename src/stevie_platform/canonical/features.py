"""
Scorer feature extraction (M5.2) — turns a candidate pair into a fixed, named
feature vector for the merge/no-merge classifier.

Scope is DELIBERATELY narrow (feature_version "v1", 11 features): provenance +
lexical + structural signal only. Context features (shared country/industry,
recognition counts) are excluded on purpose — those are the same signals used to
ORDER the labeling queue (review_priority in mine_hard_cases.py), and letting
them into the model blurs "the model learned this" with "the reviewer queue
already told it this", per the M4 discipline of keeping review heuristics out of
model features. They can return deliberately, as a versioned v2, once the v1
baseline exists to compare against.

Features are computed once and stored on organization_merge_candidate.features
(named jsonb dict, so a row predating a feature is representable as a missing
key, not a 0) alongside feature_version. Model OUTPUTS live separately in
model_predictions (migration 011) — this module never writes a prediction.
"""
from __future__ import annotations

import json

from stevie_platform.canonical.candidates import content_tokens, is_acronym_expansion

FEATURE_VERSION = "v1"

FEATURE_NAMES = (
    # provenance — which blocker(s) surfaced this pair
    "blocked_by_trigram", "blocked_by_rare_token", "blocked_by_acronym",
    # lexical
    "trigram_similarity", "token_jaccard", "length_ratio",
    "shared_rare_token_count", "normalized_token_overlap",
    # structural
    "is_acronym_expansion", "prefix_overlap", "suffix_match",
)


# --- lexical -----------------------------------------------------------------

def _char_trigrams(s: str) -> set[str]:
    """Padded character trigrams. The leading/trailing padding mirrors pg_trgm's
    convention (so short strings still produce distinguishing edge trigrams).
    This is a pure-Python APPROXIMATION of the trigram_blocker's Postgres
    similarity — it need not bit-match pg_trgm, only correlate with it as a
    continuous feature the boolean blocked_by_trigram lacks."""
    padded = f"  {s} "
    return {padded[i:i + 3] for i in range(len(padded) - 2)}


def trigram_similarity(a: str, b: str) -> float:
    """Jaccard similarity of character trigrams: |A∩B| / |A∪B|."""
    ta, tb = _char_trigrams(a), _char_trigrams(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def token_jaccard(a: str, b: str) -> float:
    """Jaccard similarity of whitespace tokens."""
    ta, tb = set(a.split()), set(b.split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def length_ratio(a: str, b: str) -> float:
    """min/max character length — near 1 for same-length names, near 0 for an
    acronym next to a long expansion (which is itself informative)."""
    la, lb = len(a), len(b)
    if la == 0 or lb == 0:
        return 0.0
    return min(la, lb) / max(la, lb)


def shared_rare_token_count(a: str, b: str, rare_tokens: frozenset[str]) -> int:
    """Count of content tokens (>=4 chars) shared by both keys that are globally
    RARE (doc frequency <= max_doc_freq across all organizations — see
    compute_rare_tokens, kept in sync with rare_token_blocker's threshold). A
    distinctive shared word is strong evidence independent of trigram overlap."""
    ta, tb = set(content_tokens(a)), set(content_tokens(b))
    return len((ta & tb) & rare_tokens)


def normalized_token_overlap(a: str, b: str) -> float:
    """Overlap coefficient |A∩B| / min(|A|,|B|) — unlike token_jaccard (divided
    by the union), this stays high when one name's tokens are a strict subset of
    the other's, e.g. 'acme' vs 'acme global holdings'."""
    ta, tb = set(a.split()), set(b.split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


# --- structural ----------------------------------------------------------

def prefix_overlap(a: str, b: str) -> float:
    """Longest common character prefix, normalized by the longer string's
    length — catches shared-root variants ('grazitti' / 'grazitti interactive')
    that token-level features miss when one side is a single token."""
    n = 0
    for ca, cb in zip(a, b):
        if ca != cb:
            break
        n += 1
    longest = max(len(a), len(b))
    return n / longest if longest else 0.0


def suffix_match(a: str, b: str) -> float:
    """Longest common character suffix, normalized by the longer string's
    length. norm_key already strips legal suffixes, so this catches shared
    trailing content words rather than corporate form."""
    n = 0
    for ca, cb in zip(reversed(a), reversed(b)):
        if ca != cb:
            break
        n += 1
    longest = max(len(a), len(b))
    return n / longest if longest else 0.0


# --- assembly ------------------------------------------------------------

def extract_features(key_a: str, key_b: str, reasons: tuple[str, ...], *,
                      rare_tokens: frozenset[str] = frozenset()) -> dict:
    """Pure: build the full named feature vector for one candidate pair. Order
    of key_a/key_b does not matter for any feature here (all are symmetric)."""
    reasons_set = set(reasons)
    return {
        "blocked_by_trigram": "trigram" in reasons_set,
        "blocked_by_rare_token": "rare_token" in reasons_set,
        "blocked_by_acronym": "acronym" in reasons_set,
        "trigram_similarity": round(trigram_similarity(key_a, key_b), 6),
        "token_jaccard": round(token_jaccard(key_a, key_b), 6),
        "length_ratio": round(length_ratio(key_a, key_b), 6),
        "shared_rare_token_count": shared_rare_token_count(key_a, key_b, rare_tokens),
        "normalized_token_overlap": round(normalized_token_overlap(key_a, key_b), 6),
        "is_acronym_expansion": is_acronym_expansion(key_a, key_b),
        "prefix_overlap": round(prefix_overlap(key_a, key_b), 6),
        "suffix_match": round(suffix_match(key_a, key_b), 6),
    }


# --- DB-touching batch (thin; logic above is pure and unit-tested) -----------

async def compute_rare_tokens(conn, *, max_doc_freq: int = 5, min_token_len: int = 4) -> frozenset[str]:
    """The set of content tokens with global document frequency <= max_doc_freq.
    Defaults MIRROR rare_token_blocker's (candidates.py) — keeps the feature's
    notion of "rare" identical to the blocker's, since blocked_by_rare_token and
    shared_rare_token_count should agree on what counts as rare."""
    cur = await conn.execute(
        """with toks as (
               select unnest(string_to_array(norm_key, ' ')) tok from organizations),
             content as (select tok from toks where length(tok) >= %s)
           select tok from content group by tok having count(*) <= %s""",
        (min_token_len, max_doc_freq),
    )
    return frozenset(r["tok"] for r in await cur.fetchall())


async def persist_features(conn, rows: list[tuple[int, dict]]) -> int:
    """Bulk-write computed features back onto organization_merge_candidate."""
    if not rows:
        return 0
    async with conn.cursor() as cur:
        await cur.executemany(
            "update organization_merge_candidate set features = %s, feature_version = %s where id = %s",
            [(json.dumps(feats), FEATURE_VERSION, cid) for cid, feats in rows],
        )
    return len(rows)


async def run_features(*, persist_rows: bool = True) -> dict:
    """CLI entry: compute feature_version v1 features for every candidate row."""
    from stevie_platform import db
    p = await db.pool()
    async with p.connection() as conn:
        rare = await compute_rare_tokens(conn)
        cur = await conn.execute(
            "select id, left_key, right_key, reasons from organization_merge_candidate")
        rows = await cur.fetchall()
        computed = [
            (r["id"], extract_features(r["left_key"], r["right_key"], tuple(r["reasons"]),
                                        rare_tokens=rare))
            for r in rows
        ]
        written = 0
        if persist_rows:
            written = await persist_features(conn, computed)
            await conn.commit()
    summary = {
        "candidates": len(rows),
        "rare_tokens": len(rare),
        "features_written": written,
        "feature_version": FEATURE_VERSION,
        "persisted": persist_rows,
    }
    print("\n" + "=" * 52)
    print(" FEATURE EXTRACTION")
    print("=" * 52)
    print(f"  candidate pairs        {len(rows):>10,}")
    print(f"  rare tokens (doc<=5)   {len(rare):>10,}")
    print(f"  feature_version        {FEATURE_VERSION:>10}")
    print(f"  written                {written:>10,}")
    print("=" * 52 + "\n")
    return summary
