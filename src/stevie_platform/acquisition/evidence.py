"""
Evidence Layer (crawler milestone) — pluggable external-evidence acquisition.
Everything above the Fetcher is implementation-agnostic:

    Discovery.discover(subject) -> [Hit]       which pages exist (official search APIs)
    Fetcher.fetch(url) -> Document | None       get one page (httpx today; stable seam)
    Extractor.extract(doc, subject) -> dict     themes/metrics/quotes/sentiment (LLM)

Providers are selected by env config; defaults are no-op stubs so the pipeline
runs today with no keys. The real search APIs (Google CSE / Bing / SerpAPI /
Tavily) and the Claude extractor activate when their keys are present. Uses OSS
tools rather than hand-rolled solutions: `trafilatura` for main-content
extraction, the official `anthropic` SDK for the LLM. Enrich-not-define:
evidence references a canonical subject by stable slug, never defines one.
No SERP/LinkedIn scraping — discovery is official-API only.
"""
from __future__ import annotations

import os
import re
import random
import uuid
from dataclasses import dataclass, field

import httpx

from stevie_platform import db
from stevie_platform.acquisition.fetch import AdaptiveRate
from stevie_platform.config import HTTP_TIMEOUT_S, USER_AGENTS

EXTRACT_SCHEMA_VERSION = "1.0.0"

# Evidence pages are arbitrary public news/company sites that block non-browser
# clients. A realistic browser header set (not the Stevie crawler UA) cuts 403s.
_BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


@dataclass
class Hit:
    url: str
    title: str | None = None
    source_type: str | None = None


@dataclass
class Document:
    url: str
    status: int
    html: bytes
    text: str


# --- content extraction (trafilatura: boilerplate removal / main content) ----
def html_to_text(html: bytes) -> str:
    raw = html.decode("utf-8", "replace")
    try:
        import trafilatura
        text = trafilatura.extract(raw, include_comments=False, include_tables=False,
                                    no_fallback=False)
        if text:
            return re.sub(r"\n{3,}", "\n\n", text).strip()
    except Exception:  # noqa: BLE001 — fall back to a plain text dump
        pass
    from selectolax.parser import HTMLParser
    tree = HTMLParser(raw)
    for sel in ("script", "style", "nav", "footer", "header", "noscript"):
        for n in tree.css(sel):
            n.decompose()
    root = tree.body or tree
    return re.sub(r"\s+", " ", root.text(separator=" ", strip=True)).strip()


# --- Fetcher seam (httpx today; Scrapy/Playwright/Firecrawl later, same API) --
class HttpxFetcher:
    name = "httpx"

    def __init__(self, rate: AdaptiveRate | None = None):
        self._rate = rate or AdaptiveRate()
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self):
        self._client = httpx.AsyncClient(timeout=HTTP_TIMEOUT_S, follow_redirects=True,
                                         headers=_BROWSER_HEADERS)
        return self

    async def __aexit__(self, *exc):
        await self._client.aclose()

    async def fetch(self, url: str) -> Document | None:
        await self._rate.wait()
        try:
            r = await self._client.get(url)
        except Exception:  # noqa: BLE001 — one bad URL must not kill the run
            self._rate.on_block()
            return None
        if r.status_code != 200:
            self._rate.on_block()
            return None
        self._rate.on_ok()
        return Document(url=url, status=200, html=r.content, text=html_to_text(r.content))


# --- Discovery seam (official search APIs plug in here; stubs need no key) -----
class NullDiscovery:
    name = "null"

    async def discover(self, subject: dict) -> list[Hit]:
        return []


class StaticDiscovery:
    """Discovery from user-provided URLs (meta['evidence_urls'][slug]) — no API/key."""
    name = "static"

    def __init__(self, url_map: dict | None = None):
        self.url_map = url_map or {}

    async def discover(self, subject: dict) -> list[Hit]:
        return [Hit(url=u, source_type="user")
                for u in self.url_map.get(subject["subject_slug"], [])]


class TavilyDiscovery:
    """Discovery via the Tavily Search API (official-API, ToS-safe). Reads the
    key from TAVILY_API_KEY or CRAWL_KEY. One query per subject; returns Hits."""
    name = "tavily"

    def __init__(self, api_key: str, max_results: int = 8):
        self._key = api_key
        self._max = max_results

    async def discover(self, subject: dict) -> list[Hit]:
        # Plain keyword query. Boolean OR operators make Tavily return /goto
        # redirect URLs instead of real page URLs.
        query = f'{subject["name"]} Stevie Awards achievements customer success'
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Bearer-only auth: sending api_key in the body too makes Tavily
            # return /goto redirect URLs instead of the real page URLs.
            r = await client.post(
                "https://api.tavily.com/search",
                headers={"Authorization": f"Bearer {self._key}"},
                json={"query": query, "max_results": self._max,
                      "search_depth": "basic"})
            r.raise_for_status()
            results = r.json().get("results", [])
        return [Hit(url=x["url"], title=x.get("title"), source_type="tavily")
                for x in results if x.get("url")]


def get_discovery() -> object:
    prov = os.environ.get("STEVIE_EVIDENCE_DISCOVERY", "null").lower()
    if prov == "null":
        return NullDiscovery()
    if prov == "static":
        return StaticDiscovery()  # caller injects url_map
    if prov == "tavily":
        key = os.environ.get("TAVILY_API_KEY") or os.environ.get("CRAWL_KEY")
        if not key:
            raise RuntimeError("STEVIE_EVIDENCE_DISCOVERY=tavily but no "
                               "TAVILY_API_KEY / CRAWL_KEY in the environment")
        return TavilyDiscovery(key)
    raise NotImplementedError(
        f"discovery provider '{prov}' needs a search-API key + adapter "
        "(google_cse / bing / serpapi); not wired yet")


# --- Extractor seam (Claude via the official SDK; Null needs no key) ----------
class NullExtractor:
    name = "none"

    async def extract(self, doc: Document, subject: dict) -> dict:
        return {}


_EXTRACT_PROMPT = (
    "You are extracting structured evidence about a Stevie Awards winner from a "
    "public web page. Subject: {name} ({stype}).\n\nPage text:\n{text}\n\n"
    "Extract only what the page actually supports about this subject's "
    "achievements, growth, recognition, and impact. If the page is not about the "
    "subject, return empty lists and sentiment 'neutral'.")

_MAX_EXTRACT_CHARS = 16000


class ClaudeExtractor:
    """LLM extraction via the official anthropic SDK. Reads ANTHROPIC_API_KEY from
    the environment (never from code). Structured output via messages.parse."""
    name = "claude"

    def __init__(self, model: str = "claude-opus-4-8"):
        import anthropic
        self._client = anthropic.AsyncAnthropic()   # env-resolved credentials
        self._model = model

    async def extract(self, doc: Document, subject: dict) -> dict:
        from pydantic import BaseModel

        class EvidenceExtraction(BaseModel):
            themes: list[str]
            quoted_metrics: list[str]
            quotes: list[str]
            sentiment: str
            summary: str

        prompt = _EXTRACT_PROMPT.format(
            name=subject["name"], stype=subject["subject_type"],
            text=doc.text[:_MAX_EXTRACT_CHARS])
        resp = await self._client.messages.parse(
            model=self._model, max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
            output_format=EvidenceExtraction,
        )
        return resp.parsed_output.model_dump()


def get_extractor() -> object:
    prov = os.environ.get("STEVIE_EVIDENCE_EXTRACTOR", "none").lower()
    if prov == "none":
        return NullExtractor()
    if prov == "claude":
        if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("API_KEY")):
            raise RuntimeError(
                "STEVIE_EVIDENCE_EXTRACTOR=claude but no ANTHROPIC_API_KEY in the "
                "environment (do not paste keys into code/chat — export it)")
        if os.environ.get("API_KEY") and not os.environ.get("ANTHROPIC_API_KEY"):
            os.environ["ANTHROPIC_API_KEY"] = os.environ["API_KEY"]
        return ClaudeExtractor()
    raise NotImplementedError(f"extractor '{prov}' not wired")


# --- subject selection --------------------------------------------------------
def rank_subjects(org_rows: list[dict], person_rows: list[dict]) -> list[dict]:
    """Curated notable subjects: top orgs + top people by recognition count."""
    subs = [{"subject_type": "organization", "subject_slug": r["slug"],
             "subject_id": r["id"], "name": r["name"], "recognitions": r["n"]}
            for r in org_rows]
    subs += [{"subject_type": "person", "subject_slug": r["slug"],
              "subject_id": r["id"], "name": r["name"], "recognitions": r["n"]}
             for r in person_rows]
    return subs


# --- orchestration ------------------------------------------------------------
async def build(crawl_run_id: uuid.UUID, n_org: int = 20, n_person: int = 20,
                url_map: dict | None = None) -> dict:
    discovery = get_discovery()
    if isinstance(discovery, StaticDiscovery):
        discovery.url_map = url_map or await db.get_meta("evidence_urls") or {}
    extractor = get_extractor()

    org_rows, person_rows = await db.evidence_subjects(n_org, n_person)
    subjects = rank_subjects(org_rows, person_rows)
    print(f"[evidence] {len(subjects)} curated subjects; discovery={discovery.name} "
          f"fetcher=httpx extractor={extractor.name}")

    discovered = stored = 0
    async with HttpxFetcher() as fetcher:
        for s in subjects:
            for hit in await discovery.discover(s):
                discovered += 1
                if await db.evidence_exists(s["subject_type"], s["subject_slug"], hit.url):
                    continue
                doc = await fetcher.fetch(hit.url)
                if not doc:
                    continue
                raw_id = await db.save_raw_page(
                    url=doc.url, page_type="evidence", html=doc.html,
                    http_status=doc.status, crawl_run_id=crawl_run_id)
                extracted = await extractor.extract(doc, s)
                await db.insert_winner_evidence(
                    subject=s, url=doc.url, source_type=hit.source_type,
                    content=doc.text, extracted=extracted,
                    discovery=discovery.name, extraction=extractor.name,
                    raw_page_id=raw_id, crawl_run_id=crawl_run_id)
                stored += 1
    print(f"[evidence] discovered={discovered} stored={stored}")
    return {"subjects": len(subjects), "discovered": discovered, "stored": stored}


async def subjects_report(n_org: int = 20, n_person: int = 20) -> None:
    org_rows, person_rows = await db.evidence_subjects(n_org, n_person)
    subs = rank_subjects(org_rows, person_rows)
    print(f"[evidence] {len(subs)} curated subjects ({n_org} orgs + {n_person} people):")
    for s in subs:
        print(f"    {s['recognitions']:4}  {s['subject_type']:12} {s['name']}")
