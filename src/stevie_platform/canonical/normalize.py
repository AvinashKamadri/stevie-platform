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


# --- Corporate-suffix normalization (Phase D — the "suffix rule") ------------
# This platform models brands, not registered legal entities, so the dedup key
# ignores legal-entity suffixes ("Cisco Systems Inc" == "Cisco Systems"). The
# stripped form is NOT discarded — it is preserved as `legal_suffix` metadata
# (and per-occurrence raw names remain in recognition_parties.raw_value), so
# legal-entity reporting stays possible later. Evidence + the finite review of
# cross-legal-form merges: experiments/org_normalization.

_SUFFIX_SINGLE = {
    "inc", "incorporated", "llc", "ltd", "limited", "corp", "corporation",
    "company", "co", "plc", "llp", "lp", "pllc", "gmbh", "pte", "pty", "pvt",
}
_SUFFIX_MULTI = [("l", "l", "c"), ("l", "l", "p")]      # L.L.C. / L.L.P. in key form
_SUFFIX_MULTI_JOINED = {"l l c", "l l p"}                # same, as a single display token


def strip_corporate_suffix(key: str) -> str:
    """Strip trailing legal-entity suffix tokens from a norm_key'd string,
    repeatedly ("foo co ltd" -> "foo"). Never empties (keeps >=1 token)."""
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


def split_legal_suffix(display_name: str) -> tuple[str, str]:
    """Split a (location-stripped) display name into (core, legal_suffix),
    preserving original casing/punctuation. "Samsung Electronics Co., Ltd" ->
    ("Samsung Electronics", "Co. Ltd"). Never returns an empty core."""
    if not display_name:
        return display_name or "", ""
    s = display_name
    peeled: list[str] = []
    while True:
        s = s.rstrip(" ,")
        m = re.search(r"([^\s,]+)$", s)
        if not m:
            break
        tok = m.group(1)
        nk = norm_key(tok)
        prefix = s[:m.start()].rstrip(" ,")
        if (nk in _SUFFIX_SINGLE or nk in _SUFFIX_MULTI_JOINED) and prefix:
            peeled.insert(0, tok)
            s = prefix
        else:
            break
    core = s.rstrip(" ,").strip()
    return (core or display_name.strip()), " ".join(peeled)


def build_merge_closure(decisions: list[tuple[str, str]]) -> dict[str, str]:
    """Resolve (loser_key, winner_key) merge edges into a flat loser->canonical map.

    Follows chains (A->B, B->C => A maps to C). Cycles are broken at the
    repeated key — the graph must be a forest, but this is defensive."""
    direct = dict(decisions)
    closure: dict[str, str] = {}
    for start in direct:
        key, seen = start, set()
        while key in direct and key not in seen:
            seen.add(key)
            key = direct[key]
        closure[start] = key
    return closure


def normalize_org(name, *, city=None, state=None, country=None,
                  vocab=frozenset()) -> tuple[str, str, str | None]:
    """Full brand-level org normalization. Returns
    (norm_key, display_name, legal_suffix):
      norm_key     — dedup key, location + legal-suffix stripped
      display_name — cleaned, original casing, suffix removed
      legal_suffix — the stripped legal form (or None)
    The original raw string is the caller's to preserve (raw_name)."""
    if not name:
        return "", name or "", None
    loc_display = strip_location_clause(name, city=city, state=state,
                                        country=country, vocab=vocab) or name
    core, suffix = split_legal_suffix(loc_display)
    core = core.strip() or loc_display.strip() or name.strip()
    key = _collapse_repeated_tail_tokens(norm_key(core)) or norm_key(name)
    return key, core, (suffix or None)
