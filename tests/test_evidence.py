"""Evidence Layer scaffold tests — pure, no DB, no network, no LLM.

Exercises the seams (Discovery/Fetcher/Extractor selection) and subject ranking.
The real search APIs and Claude extractor are not called here.
"""
import asyncio

import pytest

from stevie_platform.acquisition.evidence import (
    NullDiscovery, NullExtractor, StaticDiscovery, get_discovery, get_extractor,
    html_to_text, is_junk_url, rank_subjects, subject_mentioned,
)


def test_rank_subjects_merges_orgs_and_people():
    orgs = [{"id": 1, "slug": "ibm", "name": "IBM", "n": 784}]
    people = [{"id": 2, "slug": "jane-doe", "name": "Jane Doe", "n": 5}]
    subs = rank_subjects(orgs, people)
    assert subs[0]["subject_type"] == "organization" and subs[0]["subject_slug"] == "ibm"
    assert subs[1]["subject_type"] == "person" and subs[1]["recognitions"] == 5


def test_static_discovery_from_url_map():
    d = StaticDiscovery({"ibm": ["https://a.com", "https://b.com"]})
    hits = asyncio.run(d.discover({"subject_slug": "ibm"}))
    assert [h.url for h in hits] == ["https://a.com", "https://b.com"]
    assert asyncio.run(d.discover({"subject_slug": "unknown"})) == []


def test_discovery_defaults_to_null(monkeypatch):
    monkeypatch.delenv("STEVIE_EVIDENCE_DISCOVERY", raising=False)
    assert isinstance(get_discovery(), NullDiscovery)


def test_discovery_unknown_provider_raises(monkeypatch):
    monkeypatch.setenv("STEVIE_EVIDENCE_DISCOVERY", "google_cse")
    with pytest.raises(NotImplementedError):
        get_discovery()


def test_extractor_defaults_to_none(monkeypatch):
    monkeypatch.delenv("STEVIE_EVIDENCE_EXTRACTOR", raising=False)
    assert isinstance(get_extractor(), NullExtractor)


def test_extractor_claude_requires_key(monkeypatch):
    monkeypatch.setenv("STEVIE_EVIDENCE_EXTRACTOR", "claude")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        get_extractor()


def test_is_junk_url():
    # listing / nav / auth pages are not evidence
    assert is_junk_url("https://blog.stevieawards.com/blog/topic/marketing")
    assert is_junk_url("https://site.com/category/news")
    assert is_junk_url("https://site.com/blog/archive/2025/12")
    assert is_junk_url("https://site.com/login")
    assert is_junk_url("https://site.com/author/jane-doe")
    # real article pages pass
    assert not is_junk_url("https://en.wikipedia.org/wiki/IBM")
    assert not is_junk_url("https://www.ibm.com/new/announcements/some-real-story")
    assert not is_junk_url("https://mobile.stevieawards.com/sales/ibm-customer-service-success")


def test_subject_mentioned_org():
    # full name and branded-token hits both pass
    assert subject_mentioned("Today IBM announced record results.", "IBM", "organization")
    assert subject_mentioned("A post on the Cisco blog about routers.",
                             "Cisco Systems", "organization")
    assert subject_mentioned("USANA reported strong sales this quarter.",
                             "USANA Health Sciences", "organization")
    # a page about someone else entirely is gated out
    assert not subject_mentioned("Microsoft and Google dominate the cloud.",
                                 "USANA Health Sciences", "organization")
    # legal/generic tokens alone must NOT admit an off-subject page
    assert not subject_mentioned("Our company group holdings had a strong year.",
                                 "Acme Company Group", "organization")


def test_subject_mentioned_person():
    assert subject_mentioned("An interview with Patty Arvielo of New American Funding.",
                             "Patty Arvielo", "person")
    # surname anchor is enough (honorific-tolerant)
    assert subject_mentioned("Dr. Kaplan received a lifetime achievement award.",
                             "Ann Kaplan", "person")
    # diacritics normalize
    assert subject_mentioned("Burcu Ozdemir led the campaign.",
                             "Burcu Özdemir Kayimtu", "person")
    # common-name guard: bare pronoun 'he' must not match 'Emma He'
    assert not subject_mentioned("He went to the store and he came back.",
                                 "Emma He", "person")
    assert subject_mentioned("Emma He accepted the Gold Stevie.", "Emma He", "person")
    # a page that never names the subject is gated out
    assert not subject_mentioned("The winner spoke about snowy woods.",
                                 "Robert Frost", "person")
    # empty/degenerate name fails open (never blocks)
    assert subject_mentioned("anything at all", "", "person")


def test_subject_mentioned_url_provenance():
    # first-person post: body never names the subject, but the URL does
    body = "I'm still in shock — thank you all for this incredible honor!"
    assert not subject_mentioned(body, "Holly Budge", "person")
    assert subject_mentioned(body, "Holly Budge", "person",
                             url="https://ug.linkedin.com/posts/hollybudge_stevies")
    # org own-domain / hyphenated slug
    assert subject_mentioned("We received 14 Stevie Awards.", "Bank of America",
                             "organization",
                             url="https://www.linkedin.com/posts/bank-of-america_wins")
    assert subject_mentioned("Our latest client work.", "Weber Shandwick",
                             "organization", url="https://webershandwick.com/work")
    # a URL for a different subject must NOT admit the page
    assert not subject_mentioned("Some unrelated text.", "Holly Budge", "person",
                                 url="https://example.com/microsoft-news")


def test_html_to_text_strips_boilerplate():
    html = (b"<html><body><script>alert('x')</script>"
            b"<article><p>Hello world, this is the main article content about "
            b"the awards and the company's growth this year.</p></article>"
            b"<nav>site menu</nav></body></html>")
    txt = html_to_text(html)
    assert "Hello world" in txt
    assert "alert" not in txt
