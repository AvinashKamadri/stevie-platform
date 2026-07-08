"""Blog discover + extract tests — pure, no network, no DB.

Extractor cases run against real HubSpot HTML saved in tests/fixtures/
(one English post, one German) so the language gate is proven on live markup,
not a hand-mocked shape.
"""
from pathlib import Path

from stevie_platform.acquisition.blog_discover import (
    is_blog_post, parse_sitemap, select_blog_posts,
)
from stevie_platform.parsing.blog_parse import (
    detect_language, is_english, parse_blog_post, slug_from_url,
)
from stevie_platform.canonical.blog_link import build_vocab, find_mentions

FIX = Path(__file__).parent / "fixtures"
BASE = "https://blog.stevieawards.com"

URLSET = f"""<?xml version="1.0"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>{BASE}/blog/future-proofing-your-organization</loc></url>
  <url><loc>{BASE}/blog/die-klimaschutz-kategorien-2026</loc></url>
  <url><loc>{BASE}/blog/topic/marketing-awards</loc></url>
  <url><loc>{BASE}/blog/archive/2025/12</loc></url>
  <url><loc>{BASE}/event-awards</loc></url>
  <url><loc>{BASE}/blog</loc></url>
</urlset>"""

INDEX = """<?xml version="1.0"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://blog.stevieawards.com/sitemap.xml?page=1</loc></sitemap>
  <sitemap><loc>https://blog.stevieawards.com/sitemap.xml?page=2</loc></sitemap>
</sitemapindex>"""


# --- discover ---------------------------------------------------------------
def test_is_blog_post():
    assert is_blog_post(f"{BASE}/blog/some-real-post") is True
    assert is_blog_post(f"{BASE}/blog/topic/marketing") is False   # topic surface
    assert is_blog_post(f"{BASE}/blog/archive/2025/12") is False   # archive surface
    assert is_blog_post(f"{BASE}/blog") is False                   # the index
    assert is_blog_post(f"{BASE}/event-awards") is False           # not a blog path
    assert is_blog_post(f"{BASE}/de/blog/ein-beitrag") is False    # locale-prefixed


def test_parse_sitemap_urlset():
    children, pages = parse_sitemap(URLSET)
    assert children == []
    assert len(pages) == 6


def test_parse_sitemap_index():
    children, pages = parse_sitemap(INDEX)
    assert len(children) == 2
    assert pages == []


def test_select_blog_posts():
    _, pages = parse_sitemap(URLSET)
    posts = select_blog_posts(pages)
    assert posts == [
        f"{BASE}/blog/die-klimaschutz-kategorien-2026",
        f"{BASE}/blog/future-proofing-your-organization",
    ]


def test_slug_from_url():
    assert slug_from_url(f"{BASE}/blog/future-proofing-your-organization") == \
        "future-proofing-your-organization"
    assert slug_from_url(f"{BASE}/blog/some-post/") == "some-post"


# --- language detection -----------------------------------------------------
def test_detect_language_short_circuits():
    assert detect_language("这是一篇关于斯蒂维奖的中文博客文章内容示例。") == "cjk"
    assert detect_language("hi") == "und"        # too little signal


def test_language_gate_on_fixtures():
    en = parse_blog_post((FIX / "blog_en.html").read_text(encoding="utf-8"),
                         f"{BASE}/blog/future-proofing-your-organization")
    de = parse_blog_post((FIX / "blog_de.html").read_text(encoding="utf-8"),
                         f"{BASE}/blog/die-klimaschutz-kategorien-2026")
    assert en["lang"] == "en" and en["is_english"] is True
    assert de["lang"] == "de" and de["is_english"] is False


# --- extraction on real markup ----------------------------------------------
def test_extract_english_post():
    post = parse_blog_post((FIX / "blog_en.html").read_text(encoding="utf-8"),
                           f"{BASE}/blog/future-proofing-your-organization")
    assert "Future-Proofing" in post["title"]
    assert post["author"] == "Amanda Del Signore"
    assert len(post["clean_text"]) > 1000
    assert post["slug"] == "future-proofing-your-organization"


def test_extract_german_post():
    post = parse_blog_post((FIX / "blog_de.html").read_text(encoding="utf-8"),
                           f"{BASE}/blog/die-klimaschutz-kategorien-2026")
    assert post["author"] == "Jana Novatscheck"
    assert "Klimaschutz" in post["title"]
    assert len(post["clean_text"]) > 1000


# --- linker (mention -> canonical entity, precision-first) ------------------
PROGRAMS = [{"id": 1, "slug": "the-american-business-awards",
             "name": "The American Business Awards"}]
CATEGORIES = [{"id": 10, "slug": "customer-service-team-of-the-year",
               "name": "Customer Service Team of the Year"},
              {"id": 11, "slug": "sales", "name": "Sales"}]      # single-word -> dropped
ORGS = [{"id": 100, "slug": "acme-robotics-inc", "name": "Acme Robotics Inc"},
        {"id": 101, "slug": "ibm", "name": "IBM"}]                 # single-token
EDITIONS = {(1, 2013): "the-american-business-awards-2013"}


def _vocab():
    return build_vocab(PROGRAMS, CATEGORIES, ORGS)


def test_build_vocab_excludes_single_token_orgs():
    v = _vocab()
    assert "acme robotics inc" in v                      # multi-token org kept
    assert "ibm" not in v                                # single-token org dropped
    assert "sales" not in v                              # single-word category dropped
    assert "customer service team of the year" in v      # distinctive category kept
    assert v["the american business awards"][0] == "program"


def test_build_vocab_controlled_vocab_wins_collision():
    v = build_vocab([{"id": 1, "slug": "p", "name": "Foo Bar"}], [],
                    [{"id": 2, "slug": "o", "name": "Foo Bar"}])
    assert v["foo bar"] == ("program", "p", 1)


def test_find_mentions_resolves_and_editions():
    text = ("At the 2013 gala, The American Business Awards honored Acme "
            "Robotics Inc for Customer Service Team of the Year. IBM also spoke.")
    edges = {(e["entity_type"], e["entity_slug"]): e
             for e in find_mentions(text, _vocab(), EDITIONS)}
    assert ("program", "the-american-business-awards") in edges
    assert ("organization", "acme-robotics-inc") in edges
    assert ("category_definition", "customer-service-team-of-the-year") in edges
    # program + co-mentioned 2013 -> edition edge carrying the year
    ed = edges[("program_edition", "the-american-business-awards-2013")]
    assert ed["year"] == 2013
    # single-token org must NOT appear (reference-only + org guard)
    assert ("organization", "ibm") not in edges
    # every edge carries a below-winner confidence
    assert all(0 < e["confidence"] <= 0.7 for e in edges.values())


def test_find_mentions_reference_only():
    # a company that isn't a canonical entity produces no edge (never minted)
    edges = find_mentions("Some Unknown Startup LLC won an award.", _vocab(), EDITIONS)
    assert edges == []
