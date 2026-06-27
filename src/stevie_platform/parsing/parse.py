"""
Pure HTML parsing — side-effect free, so it is trivially testable: feed it
saved HTML, assert the dict. No network, no DB. This is state 1 -> state 2.

Bump PARSER_VERSION whenever extraction logic changes; reparsing then writes a
NEW parsed_records row per raw page rather than overwriting, so parser versions
can be compared without re-crawling.
"""
from __future__ import annotations

import re

from stevie_platform.config import MODAL_LABEL_MAP

PARSER_VERSION = "1.1.0"  # 1.1.0: recognize grand / people's-choice / distinguished-honoree levels

# A detail page that parses without these is a structure miss (or a changed
# page) — flag it incomplete, never silently accept a half-empty record.
REQUIRED_FIELDS = ("organization_name", "year", "award")

_TOTAL_RE = re.compile(r"of\s+([\d,]+)", re.I)
_MATH_RE = re.compile(r"(-?\d+)\s*([+\-x×*])\s*(-?\d+)")


def parse_total(html: str) -> int | None:
    """Pull 82654 out of 'Displaying 1 - 60 of 82654'."""
    m = _TOTAL_RE.search(html)
    return int(m.group(1).replace(",", "")) if m else None


def solve_math(text: str) -> int | None:
    """Solve the listing form's 'a + b =' math question."""
    m = _MATH_RE.search(text or "")
    if not m:
        return None
    a, op, b = int(m.group(1)), m.group(2), int(m.group(3))
    if op == "+":
        return a + b
    if op == "-":
        return a - b
    return a * b  # x, ×, *


def listing_has_captcha(html: str) -> bool:
    return 'name="captcha_response"' in html


def derive_result_level(award: str) -> str:
    """Map the free-text `Award` field to a structured level.

    The `Award` field is the LEVEL, not the program — the program lives in the
    separate `Award Programs` field. Besides the medal tiers, Stevie has three
    special recognition levels that carry no medal keyword: Grand Stevie (the
    top cross-program honor), People's Choice (public vote), and Distinguished
    Honoree. Those are matched first; their text never contains a medal word.
    Returns: grand | peoples_choice | distinguished_honoree | gold | silver |
    bronze | finalist | other.
    """
    a = (award or "").lower()
    if "grand" in a:
        return "grand"
    if "people" in a:            # "People's Choice" / "Peoples Choice"
        return "peoples_choice"
    if "distinguished" in a:     # "Distinguished Honoree"
        return "distinguished_honoree"
    for level in ("gold", "silver", "bronze", "finalist"):
        if level in a:
            return level
    return "other"


def is_complete_record(rec: dict) -> bool:
    """True only if every REQUIRED_FIELDS value is present and non-empty."""
    return all((rec.get(f) or "").strip() for f in REQUIRED_FIELDS)


def parse_listing_ids(html: str) -> list[str]:
    """Node ids (`rel`) of every nomination on a listing page."""
    from selectolax.parser import HTMLParser

    tree = HTMLParser(html)
    ids: list[str] = []
    for a in tree.css("a.a-view-past-winner-details"):
        rel = a.attributes.get("rel")
        if rel and rel.isdigit():
            ids.append(rel)
    return ids


def parse_detail(html: str, node_id: str) -> dict:
    """Turn a /view-details/{id} response into a structured record.

    Title lives in an <h5>; the fields are a 2-column label/value table.
    """
    from selectolax.parser import HTMLParser

    tree = HTMLParser(html)
    rec: dict = {"node_id": str(node_id)}

    title_el = tree.css_first("h5") or tree.css_first("h4") or tree.css_first("h3")
    if title_el:
        rec["nomination_title"] = title_el.text(strip=True)

    for tr in tree.css("table tr"):
        # NOTE: tr.css("td, th") groups matches by selector (all td, then all
        # th), NOT document order — so a <th>label</th><td>value</td> row would
        # come back reversed. Iterate children to keep label-then-value order.
        cells = [n for n in tr.iter(include_text=False) if n.tag in ("td", "th")]
        if len(cells) < 2:
            continue
        label = cells[0].text(strip=True).lower().rstrip(":").strip()
        value = cells[1].text(strip=True)
        col = MODAL_LABEL_MAP.get(label)
        if col:
            rec[col] = value

    rec["result_level"] = derive_result_level(rec.get("award", ""))
    return rec
