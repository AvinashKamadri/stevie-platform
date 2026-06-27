"""
Pure text normalization — the deterministic foundation of canonicalization.
No DB, no network: feed a string, get a stable key/slug. Trivially testable.

norm_key: the dedup key. Strips accents (Türkiye -> turkiye) but KEEPS non-Latin
scripts (so CJK/Arabic names don't collapse to empty), lowercases, drops ®™©,
removes punctuation, collapses whitespace.

slug: URL-safe page path derived from the key. Uniqueness across distinct keys
is enforced at insert time (see db.unique_slug), not here.
"""
from __future__ import annotations

import re
import unicodedata

_NONWORD = re.compile(r"[^\w\s-]", re.UNICODE)  # \w (unicode) keeps letters/digits
_WS = re.compile(r"\s+")
_DASHES = re.compile(r"-+")
_MARKS = "®™©℠"


def norm_key(text: str | None) -> str:
    if not text:
        return ""
    t = unicodedata.normalize("NFKD", text)
    t = "".join(c for c in t if not unicodedata.combining(c))  # drop accents, keep base
    t = t.lower()
    for m in _MARKS:
        t = t.replace(m, "")
    t = _NONWORD.sub(" ", t)
    return _WS.sub(" ", t).strip()


def slugify(text: str | None) -> str:
    k = norm_key(text)
    s = _DASHES.sub("-", k.replace(" ", "-")).strip("-")
    return s or "n-a"


def edition_slug(program_name: str, year: int | str) -> str:
    return f"{slugify(program_name)}-{year}"


# --- Location-clause normalization (Phase D — the "location rule") -----------
# Org names routinely carry a trailing location ("IBM, Armonk, NY") that the
# parser already captured structurally (city/state/country). Those trailing
# segments are redundant, so dropping them collapses obvious duplicates
# deterministically — never a guess: a segment is removed only if it matches
# THIS record's own city/state/country or a US-state / country gazetteer.
# Evidence: experiments/org_normalization (−11.0% distinct orgs, 0 false merges,
# 9 unit tests). The corporate-suffix rule is intentionally NOT applied here
# (held pending review of cross-legal-form merges).

_US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska",
    "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey",
    "NM": "New Mexico", "NY": "New York", "NC": "North Carolina",
    "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon",
    "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
    "DC": "District of Columbia",
}
_COUNTRY_ALIASES = ["USA", "U.S.A.", "United States", "United States of America",
                    "U.S.", "US", "UK", "U.K.", "United Kingdom", "UAE"]


def build_location_vocab(country_names=()) -> frozenset:
    """norm_key'd set of trailing segments always safe to treat as location:
    US state abbrevs+names, country aliases, and the country names present in
    the data (passed in — keeps this deterministic and DB-state-independent)."""
    vocab = set()
    for ab, full in _US_STATES.items():
        vocab.add(norm_key(ab))
        vocab.add(norm_key(full))
    for c in _COUNTRY_ALIASES:
        vocab.add(norm_key(c))
    for c in country_names:
        k = norm_key(c)
        if k:
            vocab.add(k)
    vocab.discard("")
    return frozenset(vocab)


def strip_location_clause(name: str, *, city=None, state=None, country=None,
                          vocab=frozenset()) -> str:
    """Drop trailing comma-separated segments that are location noise (match
    this record's own city/state/country or the gazetteer). Stops at the first
    non-location segment so a real name is never truncated; keeps >=1 segment."""
    segs = [s.strip() for s in name.split(",")]
    if len(segs) <= 1:
        return name.strip()
    rec_loc = {norm_key(v) for v in (city, state, country) if v}
    rec_loc.discard("")

    def is_location(seg: str) -> bool:
        k = norm_key(seg)
        if not k:
            return True
        return k in rec_loc or k in vocab

    while len(segs) > 1 and is_location(segs[-1]):
        segs.pop()
    return ", ".join(segs).strip()


def _collapse_repeated_tail_tokens(text: str) -> str:
    toks = text.split()
    while len(toks) >= 2 and toks[-1] == toks[-2]:
        toks.pop()
    return " ".join(toks)


def location_dedup_key(name, *, city=None, state=None, country=None,
                       vocab=frozenset()) -> str:
    """Org dedup key with the location rule applied. Falls back to norm_key so a
    real name is never collapsed to empty."""
    if not name:
        return ""
    stripped = strip_location_clause(name, city=city, state=state,
                                     country=country, vocab=vocab)
    key = _collapse_repeated_tail_tokens(norm_key(stripped))
    return key or norm_key(name)


def location_display_name(name, *, city=None, state=None, country=None,
                          vocab=frozenset()) -> str:
    """Cleaned display name = raw minus its redundant trailing location."""
    if not name:
        return name
    return strip_location_clause(name, city=city, state=state,
                                 country=country, vocab=vocab) or name
