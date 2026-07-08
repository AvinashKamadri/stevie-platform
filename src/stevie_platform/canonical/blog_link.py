"""
Blog LINK — pure mention detection + resolution to canonical entities. No DB,
no network: feed text + a vocab, get graph edges. Trivially testable.

Precision-first v1 (decision 2026-07-08): match EXACT normalized n-grams (via
norm_key, the same key the canonicalizer used) against controlled vocabularies
(programs, category definitions) and MULTI-TOKEN organization names, resolving
each span to an EXISTING canonical entity. Enrich-not-define: a span that does
not resolve is dropped, never minted. Single-token org names are excluded (the
false-positive-prone case). No fuzzy, no LLM — those stay deferred.

Confidence is behavioral, not calibrated (like M7 fact_confidence), and sits
BELOW winner-record trust: a blog mention is weaker evidence than a corroborated
recognition.
"""
from __future__ import annotations

import re

from stevie_platform.canonical.normalize import norm_key

LINK_VERSION = "1.0.0"

CONF = {
    "program": 0.70,
    "program_edition": 0.70,
    "category_definition": 0.65,
    "organization": 0.55,
}
_MAX_N = 8              # cap n-gram length (bounds cost; long category names rarely appear verbatim)
_MIN_CHARS = 6          # specificity guard: drop very short normalized names
_YEAR_RE = re.compile(r"\b(20[0-2]\d)\b")


def build_vocab(programs, categories, organizations) -> dict[str, tuple[str, str, int]]:
    """Rows are dicts with id/slug/name. Returns {norm_key: (entity_type, slug, id)}.
    Controlled vocab (programs, categories) is added first and WINS on a key
    collision — a name that is both a program and an org resolves to the program.
    Organizations are restricted to multi-token, sufficiently-specific names."""
    vocab: dict[str, tuple[str, str, int]] = {}

    def add(rows, etype, *, guard=False):
        for r in rows:
            k = norm_key(r["name"])
            if not k:
                continue
            # Specificity guard: single-token / very short names ("Sales",
            # "Services", "Marketing") collide with ordinary prose -> drop them.
            if guard and (len(k.split()) < 2 or len(k) < _MIN_CHARS):
                continue
            vocab.setdefault(k, (etype, r["slug"], r["id"]))

    add(programs, "program")                                  # 11, distinctive — no guard
    add(categories, "category_definition", guard=True)
    add(organizations, "organization", guard=True)
    return vocab


def detect_years(text: str) -> set[int]:
    return {int(y) for y in _YEAR_RE.findall(text or "") if 2002 <= int(y) <= 2026}


def find_mentions(text: str, vocab: dict, editions: dict | None = None) -> list[dict]:
    """Resolve entity mentions in `text`. `editions` maps (program_id, year) ->
    edition_slug; when a program is matched and a co-mentioned year has an
    edition, an extra program_edition edge (carrying the year) is emitted.

    De-duplicated per (entity_type, entity_slug) within the text."""
    toks = norm_key(text).split()
    years = detect_years(text)
    seen: dict[tuple[str, str], dict] = {}
    n = len(toks)
    for i in range(n):
        for span in range(min(_MAX_N, n - i), 0, -1):     # prefer the longest match at i
            gram = " ".join(toks[i:i + span])
            hit = vocab.get(gram)
            if not hit:
                continue
            etype, slug, eid = hit
            seen.setdefault((etype, slug), {
                "entity_type": etype, "entity_slug": slug, "entity_id": eid,
                "mention_text": gram, "confidence": CONF[etype], "year": None})
            if etype == "program" and editions:
                for y in years:
                    eslug = editions.get((eid, y))
                    if eslug:
                        seen.setdefault(("program_edition", eslug), {
                            "entity_type": "program_edition", "entity_slug": eslug,
                            "entity_id": None, "mention_text": f"{gram} {y}",
                            "confidence": CONF["program_edition"], "year": y})
            break
    return list(seen.values())
