"""
Person-name extraction from individual-award nomination titles. Pure, no DB —
feed a nomination_title, get a cleaned person name or None. Trivially testable.

Individual awards (Executive/Professional/Employee/Thought Leader of the Year,
etc.) attribute the recognition to an ORGANIZATION in the canonical model; the
person's name sits unstructured in `recognitions.nomination_title`. This module
recovers it. Precision-first (like the blog linker): the dominant clean formats
("Name", "Name, Title", "Name: <blurb>", "Name - <blurb>") are parsed; noisy
narrative/project/org titles are REJECTED rather than mangled into a fake name.
An LLM pass can lift recall on the rejected tail later (measured in MP.3).
"""
from __future__ import annotations

import re

from stevie_platform.canonical.normalize import norm_key, slugify

EXTRACT_VERSION = "1.0.0"

# Category names that denote an individual (not team/org) award.
INDIVIDUAL_AWARD_RX = (
    r'(executive|professional|employee|manager|entrepreneur|woman|man|director|'
    r'leader|officer|maverick|thought leader) of the year')

# First boundary between a name and a trailing title/blurb.
_DELIM = re.compile(r"\s[-–—]\s|[,:;(|]")

_HONORIFICS = {"mr", "mrs", "ms", "miss", "dr", "prof", "atty", "sir", "madam",
               "mme", "mx", "engr", "hon", "rev", "capt", "col", "sr", "fr"}
# Tokens that betray an org / not a personal name.
_ORG_MARKERS = {"inc", "llc", "ltd", "corp", "corporation", "company", "co",
                "group", "solutions", "solution", "services", "service",
                "technologies", "technology", "systems", "software", "alliance",
                "association", "foundation", "bank", "university", "hospital",
                "institute", "team", "enterprises", "holdings", "partners",
                "consulting", "agency", "gmbh", "pvt", "plc", "sa", "ag",
                "limited", "department", "division", "center", "centre",
                "global", "international", "national", "council", "committee"}
# Lowercase name particles that are legal inside a name.
_PARTICLES = {"de", "del", "della", "van", "von", "der", "den", "la", "le",
              "di", "da", "dos", "das", "bin", "binti", "al", "el", "san",
              "st", "mc", "mac", "ter", "ten", "bint", "abu"}

_INITIAL = re.compile(r"^[A-Za-z]\.?$")


def _clean_token(t: str) -> str:
    return t.strip(".").lower()


def _alpha_core(t: str) -> str:
    return "".join(ch for ch in t if ch.isalpha())


def _is_acronym(t: str) -> bool:
    """ALL-CAPS run of >=2 letters (SPHR, EVP, VS) — a cert/acronym, not a name.
    Single-letter initials ("T.") are NOT acronyms and are preserved."""
    core = _alpha_core(t)
    return len(core) >= 2 and core.isupper()


def _is_strong_name(t: str) -> bool:
    """A real name token: capitalized (Unicode-aware — José, Åke, Díaz), not a
    particle or bare initial."""
    if not t or not t[:1].isupper():
        return False
    if _clean_token(t) in _PARTICLES:
        return False
    if any(ch.isdigit() for ch in t):
        return False
    return t.replace("'", "").replace("’", "").replace(".", "").replace("-", "").isalpha()


def _is_name_token(t: str) -> bool:
    if _INITIAL.match(t):
        return True
    if _clean_token(t) in _PARTICLES:
        return True
    return _is_strong_name(t)


def extract_person(title: str | None) -> str | None:
    """Return a cleaned person name from a nomination title, or None if the
    title does not cleanly name an individual."""
    if not title or not title.strip():
        return None
    seg = _DELIM.split(title.strip(), 1)[0].strip().strip('"“”')
    if not seg:
        return None
    toks = seg.split()
    # strip leading honorifics ("Mr.", "Miss", ...)
    while toks and _clean_token(toks[0]) in _HONORIFICS:
        toks = toks[1:]
    # drop ALL-CAPS acronyms/certs/suffixes (SPHR, GPHR, EVP) but keep initials
    toks = [t for t in toks if not _is_acronym(t)]
    if not (2 <= len(toks) <= 5):
        return None
    if any(_clean_token(t) in _ORG_MARKERS for t in toks):
        return None
    if not all(_is_name_token(t) for t in toks):
        return None
    # need at least two "strong" tokens (not bare particles/initials) so
    # "de la" or "J. R." alone don't qualify.
    strong = [t for t in toks if _is_strong_name(t) and not _INITIAL.match(t)]
    if len(strong) < 2:
        return None
    return " ".join(toks)


def person_key(name: str) -> str:
    """Dedup key for a person name (reuses canonical norm_key)."""
    return norm_key(name)


_TITLE_MAX_WORDS = 10


def parse_title(nomination_title: str | None) -> str | None:
    """Best-effort role/title: the clause after the first comma, cut at the next
    delimiter (e.g. 'Ines Ruiz, CEO and Founder' -> 'CEO and Founder')."""
    if not nomination_title or "," not in nomination_title:
        return None
    tail = _DELIM.split(nomination_title.split(",", 1)[1].strip(), 1)[0].strip()
    if not tail or len(tail.split()) > _TITLE_MAX_WORDS:
        return None
    return tail


def resolve_people(records: list[dict]) -> list[dict]:
    """Pure resolution. `records`: dicts with rec_id, nomination_title, org_id.
    Returns person dicts {norm_key, slug, name, org_id, title, confidence, rec_ids}.

    Identity key = (person_key(name), org_id): same name at the same employer is
    one person (conservative — favors precision over merging job-changers).
    Confidence rises with corroboration (how many recognitions name the person)."""
    groups: dict[tuple, dict] = {}
    for r in records:
        name = extract_person(r.get("nomination_title"))
        if not name:
            continue
        key = (person_key(name), r.get("org_id"))
        g = groups.get(key)
        if g is None:
            g = groups[key] = {"name": name, "org_id": r.get("org_id"),
                               "title": None, "rec_ids": []}
        g["rec_ids"].append(r["rec_id"])
        if g["title"] is None:
            g["title"] = parse_title(r.get("nomination_title"))

    people, used = [], set()
    for (pkey, org_id), g in groups.items():
        base = slugify(g["name"]) or "person"
        slug, i = base, 2
        while slug in used:
            slug, i = f"{base}-{i}", i + 1
        used.add(slug)
        n = len(g["rec_ids"])
        conf = 0.55 if n == 1 else 0.68 if n < 4 else 0.80
        people.append({"norm_key": f"{pkey}@{org_id or 0}", "slug": slug,
                       "name": g["name"], "org_id": org_id, "title": g["title"],
                       "confidence": conf, "rec_ids": g["rec_ids"]})
    return people
