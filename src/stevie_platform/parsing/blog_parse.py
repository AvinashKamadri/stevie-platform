"""
Blog EXTRACT — pure HubSpot post parsing + language detection. Side-effect free
(feed it saved HTML, assert the dict), mirroring parse.py. State: raw HTML ->
structured post.

This HubSpot theme is metadata-poor: no JSON-LD, no og:locale, no <time>, and
`<html lang>` is hard-coded "en-us" on EVERY post regardless of content (verified
against a German post). So language is detected from the BODY TEXT, and the
English-only gate is `lang == "en"`. Publish date is not machine-readable in the
post HTML, so published_at is best-effort and may be None (column is nullable;
the sitemap lastmod is the ordering fallback, applied by the discover stage).

Detection is a deterministic, dependency-free stopword-ratio classifier (like
confidence.py: explainable, no ML), sufficient for the binary keep-English gate.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from selectolax.parser import HTMLParser

BLOG_PARSER_VERSION = "1.0.0"

# --- language detection -----------------------------------------------------
# Common function words per Latin-script language. English is the only one we
# keep; the others exist so a non-English post is classified (and dropped) with
# a real code rather than a vague "not-english".
_STOPWORDS = {
    "en": {"the", "and", "of", "to", "in", "a", "is", "for", "with", "that",
           "on", "are", "as", "this", "by", "an", "be", "or", "from", "at",
           "your", "you", "we", "it", "its", "our", "has", "have", "will"},
    "de": {"der", "die", "und", "den", "das", "von", "ist", "mit", "für",
           "auf", "ein", "eine", "im", "dem", "des", "zu", "sich", "nicht",
           "auch", "werden", "wird", "sind", "als", "es", "bei", "aus"},
    "es": {"el", "la", "de", "que", "y", "en", "los", "un", "una", "por",
           "con", "para", "se", "del", "las", "es", "su", "lo", "como", "más"},
    "fr": {"le", "la", "les", "de", "des", "et", "un", "une", "que", "dans",
           "pour", "sur", "est", "au", "du", "en", "qui", "avec", "pas", "sont"},
}
_WORD_RE = re.compile(r"[a-zà-ÿ]+", re.I)
# Non-Latin scripts → immediately non-English, classified by script.
_SCRIPT_RANGES = {
    "cjk": [(0x4E00, 0x9FFF), (0x3040, 0x30FF), (0xAC00, 0xD7AF)],  # Han/Kana/Hangul
    "cyrillic": [(0x0400, 0x04FF)],
    "arabic": [(0x0600, 0x06FF)],
}


def _script_share(text: str) -> tuple[str | None, float]:
    """Dominant non-Latin script and its share of alphabetic chars, if any."""
    counts = {name: 0 for name in _SCRIPT_RANGES}
    alpha = 0
    for ch in text:
        if ch.isalpha():
            alpha += 1
            cp = ord(ch)
            for name, ranges in _SCRIPT_RANGES.items():
                if any(lo <= cp <= hi for lo, hi in ranges):
                    counts[name] += 1
    if not alpha:
        return None, 0.0
    name = max(counts, key=counts.get)
    return name, counts[name] / alpha


def detect_language(text: str) -> str:
    """Best-effort language code ('en','de','es','fr', a script name, or 'und').

    Deterministic: non-Latin scripts win by character share; otherwise the
    Latin language with the highest stopword hit-ratio wins (needs a small
    minimum so a stray page of proper nouns is 'und', not a coin-flip).
    """
    script, share = _script_share(text)
    if script and share >= 0.10:
        return script
    tokens = [t.lower() for t in _WORD_RE.findall(text)]
    if len(tokens) < 15:
        return "und"
    ratios = {lang: sum(t in words for t in tokens) / len(tokens)
              for lang, words in _STOPWORDS.items()}
    best = max(ratios, key=ratios.get)
    return best if ratios[best] >= 0.04 else "und"


def is_english(text: str) -> bool:
    return detect_language(text) == "en"


# --- extraction -------------------------------------------------------------
def slug_from_url(url: str) -> str:
    """/blog/<slug> -> <slug>."""
    return urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]


def _meta(tree: HTMLParser, attr: str, val: str) -> str | None:
    node = tree.css_first(f'meta[{attr}="{val}"]')
    if node is None:
        return None
    content = node.attributes.get("content")
    return content.strip() if content else None


def parse_blog_post(html: str, url: str) -> dict:
    """Extract a structured post from HubSpot HTML. Pure; no network/DB.

    title  : the post-name module (#hs_cos_wrapper_name), falling back to og:title
    author : <meta name="author">
    body   : text of .post-body
    lang   : detected from body (English gate = is_english)
    """
    tree = HTMLParser(html)

    name = tree.css_first("#hs_cos_wrapper_name")
    title = (name.text(strip=True) if name else None) or _meta(tree, "property", "og:title")
    author = _meta(tree, "name", "author")

    body_node = tree.css_first(".post-body")
    clean_text = body_node.text(separator=" ", strip=True) if body_node else ""
    clean_text = re.sub(r"\s+", " ", clean_text).strip()

    links = []
    if body_node:
        for a in body_node.css("a[href]"):
            href = a.attributes.get("href")
            if href and href.startswith(("http://", "https://")):
                links.append(href)

    lang = detect_language(clean_text)
    return {
        "url": url,
        "slug": slug_from_url(url),
        "title": title,
        "author": author,
        "published_at": None,          # not machine-readable in this theme
        "lang": lang,
        "is_english": lang == "en",
        "clean_text": clean_text,
        "links": sorted(set(links)),
        "parser_version": BLOG_PARSER_VERSION,
    }
