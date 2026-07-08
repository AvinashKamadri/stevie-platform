"""
Blog DISCOVER — pure sitemap → blog-post-URL selection. Side-effect free (no
network, no DB), so it is trivially testable: feed it sitemap XML, assert the
URL list. The thin async wrapper that fetches the sitemap and enqueues into
fetch_queue lives with the other DB-touching acquisition code and reuses this.

Scope: blog.stevieawards.com only (HubSpot). The sitemap is a flat urlset of
~2,726 URLs; ~1,722 are /blog/<slug> posts. Language is NOT in the URL (every
locale lives under /blog/<slug>), so we do NOT filter by language here — that
gate is applied per-post at extraction. See migrations/018_blog.sql.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET

_SM = "{http://www.sitemaps.org/schemas/sitemap/0.9}"

# Non-post paths under /blog/: listing/navigation surfaces, not articles.
_NON_POST_SEGMENTS = {"topic", "archive", "tag", "tags", "author", "page", "category"}

# 2-letter locale prefixes. This HubSpot blog does not use them, but other
# Stevie sites do; excluding them keeps the selector reusable and harmless here.
_LOCALES = {
    "ru", "et", "ro", "sk", "th", "uk", "zh", "hu", "es", "el", "fr", "sl",
    "nb", "nl", "ko", "de", "sv", "da", "it", "pl", "tr", "ar", "ja", "pt",
    "he", "id", "ms", "vi",
}


def parse_sitemap(xml_text: str) -> tuple[list[str], list[str]]:
    """Return (child_sitemaps, page_urls) from a <sitemapindex> or <urlset>.

    A sitemap index yields child sitemap URLs (recurse); a urlset yields page
    URLs. Robust to the default sitemap namespace.
    """
    root = ET.fromstring(xml_text)
    locs = [e.text.strip() for e in root.iter(f"{_SM}loc") if e.text and e.text.strip()]
    if root.tag.lower().endswith("sitemapindex"):
        return locs, []
    return [], locs


def _path(url: str) -> str:
    return re.sub(r"^https?://[^/]+", "", url).split("?")[0].split("#")[0]


def is_blog_post(url: str) -> bool:
    """True iff url is a real blog article: /blog/<slug> (one slug segment),
    English-or-any-language, excluding listing/topic/archive/tag surfaces and
    locale-prefixed paths."""
    segs = [s for s in _path(url).split("/") if s]
    if not segs or segs[0].lower() in _LOCALES:
        return False
    if segs[0].lower() != "blog":
        return False
    if len(segs) < 2:                      # /blog itself = the index, not a post
        return False
    if segs[1].lower() in _NON_POST_SEGMENTS:
        return False
    return True


def select_blog_posts(page_urls: list[str]) -> list[str]:
    """Filter to blog-post URLs, de-duplicated and sorted (stable ordering)."""
    return sorted({u for u in page_urls if is_blog_post(u)})
