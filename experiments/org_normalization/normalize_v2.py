"""
EXPERIMENTAL org-name normalization (Phase D, isolated — NOT wired into the
canonical pipeline). Pure functions only: deterministic and auditable.

The canonical key today is just `norm_key(name)`, so "IBM" and
"IBM, Armonk, NY" never collapse. This module adds a conservative cascade that
runs BEFORE the existing norm_key:

    raw
     -> whitespace normalize
     -> strip trailing LOCATION clauses
          (only segments that duplicate THIS record's own city/state/country,
           or a US state name/abbrev — never a guess)
     -> collapse immediately-repeated trailing tokens
     -> norm_key (existing: accents, ®™, punctuation, case, whitespace)

Because location is removed only when it is *redundant* with the structured
fields the parser already extracted, the merges are explainable per record.
"""
from __future__ import annotations

import re

from stevie_platform.canonical.normalize import norm_key

_WS = re.compile(r"\s+")

# US states — abbreviations and full names (norm_key'd at build time below).
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

# Country-name variants the parser's `country` field won't always echo verbatim.
_COUNTRY_ALIASES = ["USA", "U.S.A.", "United States", "United States of America",
                    "U.S.", "US", "UK", "U.K.", "United Kingdom", "UAE"]


def base_location_vocab(country_names: list[str]) -> frozenset[str]:
    """norm_key'd set of location tokens that are ALWAYS safe to treat as a
    trailing location clause: US state abbrevs+names, common country aliases,
    and the canonical country list passed in from the DB."""
    vocab: set[str] = set()
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


def _segments(name: str) -> list[str]:
    return [s.strip() for s in name.split(",")]


def strip_location_clause(name: str, *, city: str | None, state: str | None,
                          country: str | None, base_vocab: frozenset[str]) -> str:
    """Drop trailing comma-separated segments that are location noise: either
    they match this record's own city/state/country, or they're in base_vocab
    (US states / country aliases). Stops at the first non-location segment, so
    the company's real name is never truncated. Always keeps >=1 segment."""
    segs = _segments(name)
    if len(segs) <= 1:
        return name.strip()

    rec_loc = {norm_key(v) for v in (city, state, country) if v}
    rec_loc.discard("")

    def is_location(seg: str) -> bool:
        k = norm_key(seg)
        if not k:
            return True  # empty trailing segment from "Foo,," — safe to drop
        return k in rec_loc or k in base_vocab

    while len(segs) > 1 and is_location(segs[-1]):
        segs.pop()
    return ", ".join(segs).strip()


# Legal-entity suffixes (norm_key'd forms). Dotted abbrevs like "L.L.C." become
# "l l c" after norm_key, so multi-token forms are matched explicitly. The very
# ambiguous 2-letter national forms (sa/ag/ab/as/oy/bv/nv/kk) are intentionally
# EXCLUDED from this first measurement to keep the suffix signal clean.
_SUFFIX_SINGLE = {
    "inc", "incorporated", "llc", "ltd", "limited", "corp", "corporation",
    "company", "co", "plc", "llp", "lp", "pllc", "gmbh", "pte", "pty", "pvt",
}
_SUFFIX_MULTI = [("l", "l", "c"), ("l", "l", "p")]  # L.L.C. / L.L.P.


def strip_corporate_suffix(key: str) -> str:
    """Strip trailing legal-entity suffix tokens from a norm_key'd string,
    repeatedly ("foo co ltd" -> "foo"). Never empties: keeps >=1 token."""
    toks = key.split()
    changed = True
    while changed and len(toks) > 1:
        changed = False
        for mt in _SUFFIX_MULTI:
            n = len(mt)
            if len(toks) > n and tuple(toks[-n:]) == mt:
                del toks[-n:]
                changed = True
                break
        if changed:
            continue
        if len(toks) > 1 and toks[-1] in _SUFFIX_SINGLE:
            toks.pop()
            changed = True
    return " ".join(toks)


def _collapse_repeated_tail_tokens(text: str) -> str:
    """Collapse an immediately-repeated trailing token: "nu skin singapore
    singapore" -> "nu skin singapore". Conservative: only the final token, only
    when duplicated back-to-back."""
    toks = text.split()
    while len(toks) >= 2 and toks[-1] == toks[-2]:
        toks.pop()
    return " ".join(toks)


def enhanced_key(name: str | None, *, city: str | None = None,
                 state: str | None = None, country: str | None = None,
                 base_vocab: frozenset[str] = frozenset(),
                 strip_location: bool = True, strip_suffix: bool = False) -> str:
    """The experimental dedup key, composable so each rule can be measured in
    isolation. `strip_location` removes redundant trailing geography;
    `strip_suffix` removes legal-entity suffixes. Falls back to plain norm_key
    so it never produces an empty key for a non-empty name."""
    if not name:
        return ""
    s = name
    if strip_location:
        s = strip_location_clause(s, city=city, state=state,
                                  country=country, base_vocab=base_vocab)
    key = _collapse_repeated_tail_tokens(norm_key(s))
    if strip_suffix:
        key = strip_corporate_suffix(key)
    return key or norm_key(name)  # never collapse a real name to empty
