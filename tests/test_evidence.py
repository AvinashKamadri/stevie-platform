"""Evidence Layer scaffold tests — pure, no DB, no network, no LLM.

Exercises the seams (Discovery/Fetcher/Extractor selection) and subject ranking.
The real search APIs and Claude extractor are not called here.
"""
import asyncio

import pytest

from stevie_platform.acquisition.evidence import (
    NullDiscovery, NullExtractor, StaticDiscovery, get_discovery, get_extractor,
    html_to_text, rank_subjects,
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


def test_html_to_text_strips_boilerplate():
    html = (b"<html><body><script>alert('x')</script>"
            b"<article><p>Hello world, this is the main article content about "
            b"the awards and the company's growth this year.</p></article>"
            b"<nav>site menu</nav></body></html>")
    txt = html_to_text(html)
    assert "Hello world" in txt
    assert "alert" not in txt
