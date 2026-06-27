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
