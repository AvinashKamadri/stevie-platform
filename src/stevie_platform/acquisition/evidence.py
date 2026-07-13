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
import time
import asyncio
import hashlib
import unicodedata
import uuid
from dataclasses import dataclass

import httpx
from pydantic import BaseModel

from stevie_platform import db
from stevie_platform.acquisition.fetch import AdaptiveRate
from stevie_platform.config import HTTP_TIMEOUT_S

EXTRACT_SCHEMA_VERSION = "2.0.0"   # 2.0.0: added categories[] (fixed taxonomy)

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


def _load_proxies() -> list[str]:
    """Proxy URLs from STEVIE_PROXIES (comma/space/newline separated). Each like
    http://user:pass@host:port. Empty list = direct-only."""
    raw = os.environ.get("STEVIE_PROXIES", "")
    return [p.strip() for p in re.split(r"[,\s]+", raw) if p.strip()]


# --- Fetcher seam (httpx today; Scrapy/Playwright/Firecrawl later, same API) --
class HttpxFetcher:
    """Fetches a URL direct first; on failure (403/timeout — ~1 in 5 URLs) retries
    through rotating proxies if STEVIE_PROXIES is set. Proxy retries recover
    bot-blocked pages that a single client IP can't reach — the fetch-layer density
    lever (distinct from the LLM bottleneck)."""
    name = "httpx"

    def __init__(self, rate: AdaptiveRate | None = None):
        self._rate = rate or AdaptiveRate()
        self._proxies = _load_proxies()
        self._clients: list[httpx.AsyncClient] = []   # [direct, proxy1, proxy2, ...]
        # Only try a FEW proxies per failed URL (rotated), on a SHORTER timeout --
        # walking all N proxies at the full timeout made dead URLs ~N x slower and
        # dominated throughput. A live proxy answers fast; 2 attempts recover most.
        self._proxy_attempts = int(os.environ.get("STEVIE_PROXY_ATTEMPTS") or 2)
        self._proxy_timeout = float(os.environ.get("STEVIE_PROXY_TIMEOUT") or 8.0)
        self._rr = 0                                  # round-robin proxy start index
        # benchmark telemetry: how fetches resolved (direct vs proxy-recovered vs lost)
        self.stats = {"direct_ok": 0, "proxy_recovered": 0, "fail": 0}

    async def __aenter__(self):
        opts = dict(follow_redirects=True, headers=_BROWSER_HEADERS)
        self._clients = [httpx.AsyncClient(timeout=HTTP_TIMEOUT_S, **opts)]
        self._clients += [httpx.AsyncClient(proxy=p, timeout=self._proxy_timeout, **opts)
                          for p in self._proxies]
        return self

    async def __aexit__(self, *exc):
        for c in self._clients:
            await c.aclose()

    def _fetch_order(self) -> list[int]:
        """Direct (0) first, then up to _proxy_attempts proxies, rotated per call so
        load/geo spreads across the pool instead of always hitting the first two."""
        order = [0]
        n = len(self._proxies)
        if n:
            start = self._rr % n
            self._rr += 1
            order += [1 + (start + k) % n for k in range(min(self._proxy_attempts, n))]
        return order

    async def fetch(self, url: str) -> Document | None:
        await self._rate.wait()
        for i in self._fetch_order():
            try:
                r = await self._clients[i].get(url)
            except Exception:  # noqa: BLE001 — one bad URL/proxy must not kill the run
                continue
            if r.status_code == 200:
                self._rate.on_ok()
                self.stats["proxy_recovered" if i else "direct_ok"] += 1
                return Document(url=url, status=200, html=r.content,
                                text=html_to_text(r.content))
        self._rate.on_block()                   # direct + sampled proxies all failed
        self.stats["fail"] += 1
        return None


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


# Multiple angled queries per subject widen coverage far more than one query at a
# higher max_results — each angle surfaces a different slice (awards, growth,
# leadership, press). Merged + URL-deduped downstream. Plain keyword strings only:
# boolean OR operators make Tavily return /goto redirect URLs instead of real ones.
_ORG_QUERIES = [
    "{name} awards", "{name} Stevie Awards", "{name} recognition",
    "{name} customer success", "{name} case study", "{name} innovation",
    "{name} leadership", "{name} CEO", "{name} press release",
    "{name} revenue growth", "{name} product launch", "{name} partnerships",
    "{name} acquisitions", "{name} sustainability", "{name} Forbes",
    "{name} Gartner", "{name} AI",
]
_PERSON_QUERIES = [
    "{name} Stevie Awards", "{name} award", "{name} interview",
    "{name} keynote", "{name} Forbes", "{name} article",
    "{name} speaker", "{name} podcast", "{name} LinkedIn",
]


class TavilyDiscovery:
    """Discovery via the Tavily Search API (official-API, ToS-safe). Reads the key
    from TAVILY_API_KEY / CRAWL_KEY. Runs several angled queries per subject
    concurrently and merges the URL-deduped results — the primary density lever.
    Tunables: STEVIE_EVIDENCE_MAX_RESULTS (per query), STEVIE_EVIDENCE_QUERIES
    (how many angles), STEVIE_EVIDENCE_SEARCH_DEPTH (basic|advanced)."""
    name = "tavily"

    def __init__(self, api_keys, max_results: int | None = None,
                 n_queries: int | None = None, depth: str | None = None):
        # Accept one key or several; rotating across keys multiplies both the quota
        # and the rate-limit headroom (each key has its own plan allowance).
        self._keys = [api_keys] if isinstance(api_keys, str) else [k for k in api_keys if k]
        self._key_rr = 0
        self._max = max_results or int(os.environ.get("STEVIE_EVIDENCE_MAX_RESULTS") or 10)
        self._n = n_queries or int(os.environ.get("STEVIE_EVIDENCE_QUERIES") or 12)
        self._depth = depth or os.environ.get("STEVIE_EVIDENCE_SEARCH_DEPTH") or "basic"
        # GLOBAL cap on concurrent Tavily calls across ALL subjects+queries. Without
        # it, disc_conc x n_queries (e.g. 8x12) burst ~100 requests at once -> 429.
        # Scales with key count (~4 concurrent per key).
        default_conc = 4 * max(1, len(self._keys))
        self._sem = asyncio.Semaphore(int(os.environ.get("STEVIE_TAVILY_CONCURRENCY") or default_conc))

    async def _search(self, query: str) -> list[dict]:
        """One Tavily query, globally throttled, rotating across API keys, with
        429/5xx backoff that honors Retry-After. Tavily rate-limits hard, so pacing
        + key rotation beats bursting; a 429/432 on one key retries on the next."""
        last_exc = None
        self._key_rr += 1
        nk = len(self._keys)
        for attempt in range(5):
            key = self._keys[(self._key_rr + attempt) % nk]      # rotate key per attempt
            try:
                async with self._sem, httpx.AsyncClient(timeout=30.0) as client:
                    # Bearer-only auth: api_key in the body too makes Tavily return
                    # /goto redirect URLs instead of the real page URLs.
                    r = await client.post(
                        "https://api.tavily.com/search",
                        headers={"Authorization": f"Bearer {key}"},
                        json={"query": query, "max_results": self._max,
                              "search_depth": self._depth})
                r.raise_for_status()
                return r.json().get("results", [])
            except httpx.HTTPStatusError as e:
                last_exc = e
                code = e.response.status_code
                if code in (429, 432, 500, 502, 503, 504) and attempt < 4:
                    ra = float(e.response.headers.get("retry-after") or 0)
                    await asyncio.sleep(max(ra, 3 * (attempt + 1)))
                    continue
                raise
            except httpx.TransportError as e:
                last_exc = e
                if attempt < 4:
                    await asyncio.sleep(2 * (attempt + 1))
                    continue
                raise
        raise last_exc

    async def discover(self, subject: dict) -> list[Hit]:
        templates = (_PERSON_QUERIES if subject.get("subject_type") == "person"
                     else _ORG_QUERIES)
        queries = [t.format(name=subject["name"]) for t in templates[:self._n]]
        results = await asyncio.gather(*(self._search(q) for q in queries),
                                       return_exceptions=True)
        # If every angle failed, surface it so build() logs discover_fail; a partial
        # failure just contributes fewer URLs.
        if results and all(isinstance(r, Exception) for r in results):
            raise next(r for r in results if isinstance(r, Exception))
        seen: set[str] = set()
        hits: list[Hit] = []
        for r in results:
            if isinstance(r, Exception):
                continue
            for x in r:
                u = x.get("url")
                if u and u not in seen:
                    seen.add(u)
                    hits.append(Hit(url=u, title=x.get("title"), source_type="tavily"))
        return hits


def get_discovery() -> object:
    prov = os.environ.get("STEVIE_EVIDENCE_DISCOVERY", "null").lower()
    if prov == "null":
        return NullDiscovery()
    if prov == "static":
        return StaticDiscovery()  # caller injects url_map
    if prov == "tavily":
        # All configured Tavily keys, rotated to pool their quota + rate limits.
        keys = [k for k in (os.environ.get("TAVILY_API_KEY"), os.environ.get("CRAWL_KEY"),
                            os.environ.get("CRAWL_KEY_TWO"), os.environ.get("CRAWL_KEY_THREE"))
                if k]
        if not keys:
            raise RuntimeError("STEVIE_EVIDENCE_DISCOVERY=tavily but no "
                               "TAVILY_API_KEY / CRAWL_KEY(_TWO/_THREE) in the environment")
        return TavilyDiscovery(keys)
    raise NotImplementedError(
        f"discovery provider '{prov}' needs a search-API key + adapter "
        "(google_cse / bing / serpapi); not wired yet")


# --- Extractor seam (Claude via the official SDK; Null needs no key) ----------
class NullExtractor:
    name = "none"
    model = None

    async def extract(self, doc: Document, subject: dict) -> dict:
        return {}


# Fixed taxonomy so the corpus is queryable by category downstream (drafting,
# strength scoring, timelines) instead of free-form themes only.
_EVIDENCE_CATEGORIES = [
    "Leadership", "Awards", "Financial", "Innovation", "Customer Success",
    "Partnership", "Expansion", "Product Launch", "ESG", "Research", "Patents",
]

_EXTRACT_PROMPT = (
    "You are extracting structured evidence about a Stevie Awards winner from a "
    "public web page. Subject: {name} ({stype}).\n\nPage text:\n{text}\n\n"
    "Extract only what the page actually supports about this subject's "
    "achievements, growth, recognition, and impact. If the page is not about the "
    "subject, return empty lists and sentiment 'neutral'.\n"
    "Tag the evidence with any that apply from this fixed category list "
    "(use these exact labels, omit any that don't apply): " + ", ".join(_EVIDENCE_CATEGORIES) + ".")


class EvidenceExtraction(BaseModel):
    """Shared structured-output schema — same fields across every LLM backend so
    winner_evidence.extracted is provider-agnostic."""
    themes: list[str]
    categories: list[str]          # subset of _EVIDENCE_CATEGORIES that the page supports
    quoted_metrics: list[str]
    quotes: list[str]
    sentiment: str
    summary: str


# --- v3 grok_search: one item per cited source Grok found+used itself -----------
class GrokEvidenceItem(BaseModel):
    """One evidence item Grok produced from a source it searched/browsed itself.
    Same fields as EvidenceExtraction plus the source it cited."""
    source_url: str
    source_title: str
    themes: list[str]
    categories: list[str]
    quoted_metrics: list[str]
    quotes: list[str]
    sentiment: str
    summary: str


class GrokEvidenceBundle(BaseModel):
    items: list[GrokEvidenceItem]

_MAX_EXTRACT_CHARS = 16000
# Input-token/minute cap. STEVIE_EVIDENCE_TPM pins it explicitly; otherwise this is
# just a conservative floor that detect_rate_limit() raises to the account's real
# limit (read from response headers) at the start of a run. Stay under the ceiling
# so we self-throttle instead of bouncing off 429s.
_TPM_ENV = os.environ.get("STEVIE_EVIDENCE_TPM")
_TOKENS_PER_MIN = int(_TPM_ENV) if _TPM_ENV else 9000


class _TokenBudget:
    """Sliding-1-minute input-token limiter. acquire(n) blocks until admitting n
    tokens keeps the trailing-60s sum under the cap — keeps us under the org's
    per-minute input-token rate limit without relying on 429 backoff alone."""

    def __init__(self, cap: int):
        self.cap = cap
        self._events: list[tuple[float, int]] = []   # (monotonic_ts, tokens)

    async def acquire(self, tokens: int) -> None:
        while True:
            now = time.monotonic()
            self._events = [(t, n) for (t, n) in self._events if now - t < 60]
            used = sum(n for _, n in self._events)
            if used + tokens <= self.cap or not self._events:
                self._events.append((now, tokens))
                return
            oldest = min(t for t, _ in self._events)
            await asyncio.sleep(max(1.0, 60.0 - (now - oldest)))


class ClaudeExtractor:
    """LLM extraction via the official anthropic SDK. Reads ANTHROPIC_API_KEY from
    the environment (never from code). Structured output via messages.parse."""
    name = "claude"

    def __init__(self, model: str | None = None):
        import anthropic
        # max_retries: SDK honors Retry-After on 429; bump above the default 2 so a
        # transient burst rides out the per-minute window instead of dying. timeout:
        # an EXPLICIT per-request cap — without it the SDK default is 600s, so one
        # stalled connection can wedge the sequential crawl for 10min+ (and retry).
        # 90s bounds a stuck extraction; on exhaustion build() logs extract_fail and
        # moves on rather than freezing the whole run.
        self._client = anthropic.AsyncAnthropic(max_retries=5, timeout=90.0)  # env-resolved creds
        # Configurable via STEVIE_EVIDENCE_MODEL (default opus). Extraction is a
        # structured-parse task — Sonnet/Haiku cut cost ~3-5x at scale.
        self.model = model or os.environ.get("STEVIE_EVIDENCE_MODEL") or "claude-opus-4-8"
        self._budget = _TokenBudget(_TOKENS_PER_MIN)
        self.usage = {"in": 0, "out": 0}          # benchmark: accumulated token usage

    async def detect_rate_limit(self) -> int | None:
        """Probe the account's real input-tokens/min limit from the response
        headers and raise the token budget to match — so a higher tier is used to
        the full instead of throttling to the conservative default. Skipped when
        STEVIE_EVIDENCE_TPM pins the cap. Returns the detected limit or None.
        Doubles as a credit/liveness check: a dead key surfaces here, not 400 rows
        deep into the crawl."""
        if _TPM_ENV:
            return None                       # user pinned TPM; respect the override
        try:
            raw = await self._client.messages.with_raw_response.create(
                model=self.model, max_tokens=4,
                messages=[{"role": "user", "content": "hi"}])
            lim = raw.headers.get("anthropic-ratelimit-input-tokens-limit")
            if lim and int(lim) > 0:
                self._budget.cap = int(int(lim) * 0.9)   # 90% margin below the ceiling
                return int(lim)
        except Exception as e:                # noqa: BLE001 — degrade to the default cap
            print(f"[evidence] rate-limit probe failed ({type(e).__name__}: {e}); "
                  f"using TPM={self._budget.cap}")
        return None

    async def extract(self, doc: Document, subject: dict) -> dict:
        prompt = _EXTRACT_PROMPT.format(
            name=subject["name"], stype=subject["subject_type"],
            text=doc.text[:_MAX_EXTRACT_CHARS])
        # Limit is on INPUT tokens/min; ~4 chars/token. Small margin for the
        # fixed prompt scaffold + system overhead beyond the page text.
        await self._budget.acquire(len(prompt) // 4 + 200)
        resp = await self._client.messages.parse(
            model=self.model, max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
            output_format=EvidenceExtraction,
        )
        u = getattr(resp, "usage", None)
        if u:
            self.usage["in"] += getattr(u, "input_tokens", 0) or 0
            self.usage["out"] += getattr(u, "output_tokens", 0) or 0
        return resp.parsed_output.model_dump()


class GrokExtractor:
    """LLM extraction via xAI's Grok (OpenAI-compatible API). Structured output via
    the openai SDK's beta.chat.completions.parse with the shared Pydantic schema.
    Key from XAI_API_KEY / GROK_API_KEY / API_KEY (never from code)."""
    name = "grok"
    _BASE_URL = "https://api.x.ai/v1"

    def __init__(self, model: str | None = None):
        from openai import AsyncOpenAI
        key = (os.environ.get("XAI_API_KEY") or os.environ.get("GROK_API_KEY")
               or os.environ.get("API_KEY"))
        # timeout bounds a stalled call; SDK honors 429 Retry-After up to max_retries.
        self._client = AsyncOpenAI(api_key=key, base_url=self._BASE_URL,
                                   timeout=90.0, max_retries=5)
        self.model = model or os.environ.get("STEVIE_EVIDENCE_MODEL") or "grok-4"
        # xAI limits are huge (tens of millions of tokens); throttling is a non-issue.
        # detect_rate_limit() raises the cap to the account's real ceiling.
        self._budget = _TokenBudget(_TOKENS_PER_MIN)
        self.usage = {"in": 0, "out": 0}          # benchmark: accumulated token usage

    async def detect_rate_limit(self) -> int | None:
        """Read xAI's token ceiling (x-ratelimit-limit-tokens) and raise the budget
        to match. Skipped when STEVIE_EVIDENCE_TPM pins it. Also a key/credit
        liveness check — a bad key surfaces here, not deep into the crawl."""
        if _TPM_ENV:
            return None
        try:
            raw = await self._client.chat.completions.with_raw_response.create(
                model=self.model, max_tokens=4,
                messages=[{"role": "user", "content": "hi"}])
            lim = raw.headers.get("x-ratelimit-limit-tokens")
            if lim and int(lim) > 0:
                self._budget.cap = int(int(lim) * 0.9)
                return int(lim)
        except Exception as e:                # noqa: BLE001 — degrade to the default cap
            print(f"[evidence] rate-limit probe failed ({type(e).__name__}: {e}); "
                  f"using TPM={self._budget.cap}")
        return None

    async def extract(self, doc: Document, subject: dict) -> dict:
        prompt = _EXTRACT_PROMPT.format(
            name=subject["name"], stype=subject["subject_type"],
            text=doc.text[:_MAX_EXTRACT_CHARS])
        await self._budget.acquire(len(prompt) // 4 + 200)
        resp = await self._client.beta.chat.completions.parse(
            model=self.model, max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
            response_format=EvidenceExtraction,
        )
        u = getattr(resp, "usage", None)
        if u:
            self.usage["in"] += getattr(u, "prompt_tokens", 0) or 0
            self.usage["out"] += getattr(u, "completion_tokens", 0) or 0
        parsed = resp.choices[0].message.parsed
        # Guard against a refusal/empty parse — return the empty-shape schema.
        return parsed.model_dump() if parsed else {
            "themes": [], "categories": [], "quoted_metrics": [], "quotes": [],
            "sentiment": "neutral", "summary": ""}


_RESEARCH_PROMPT = (
    "You are researching {name} ({stype}), a Stevie Awards winner. Search the web "
    "for evidence of this subject's achievements, growth, recognition, awards, "
    "leadership, innovation, partnerships, financials, and impact. Return one "
    "evidence item per distinct credible source you actually used. For each: the "
    "real source URL, a short title, themes, categories (ONLY from this list: {cats}), "
    "quoted_metrics (verbatim numbers), quotes (verbatim short quotes), sentiment "
    "(positive/neutral/negative), and a 1-2 sentence summary. Only include facts the "
    "source genuinely supports about THIS subject; skip pages that are not about it. "
    "Aim for {target} items from distinct reputable sources.")


class GrokResearcher:
    """v3 discovery+extraction in one call: Grok searches & browses the web itself
    (xAI Responses API + built-in web_search tool) and returns cited, structured
    evidence items. Bypasses Tavily and the fetch layer entirely — no search-quota
    or 403 bottleneck. Key from XAI_API_KEY / GROK_API_KEY / API_KEY."""
    name = "grok_search"

    def __init__(self, model: str | None = None):
        from openai import AsyncOpenAI
        key = (os.environ.get("XAI_API_KEY") or os.environ.get("GROK_API_KEY")
               or os.environ.get("API_KEY"))
        self._client = AsyncOpenAI(api_key=key, base_url="https://api.x.ai/v1",
                                   timeout=240.0, max_retries=3)
        self.model = model or os.environ.get("STEVIE_EVIDENCE_MODEL") or "grok-4"
        self._target = int(os.environ.get("STEVIE_EVIDENCE_TARGET_ITEMS") or 15)
        self.usage = {"in": 0, "out": 0}

    async def research(self, subject: dict) -> list[dict]:
        prompt = _RESEARCH_PROMPT.format(
            name=subject["name"], stype=subject["subject_type"],
            cats=", ".join(_EVIDENCE_CATEGORIES), target=self._target)
        resp = await self._client.responses.parse(
            model=self.model, input=prompt,
            tools=[{"type": "web_search"}],
            text_format=GrokEvidenceBundle,
        )
        u = getattr(resp, "usage", None)
        if u:
            self.usage["in"] += getattr(u, "input_tokens", 0) or 0
            self.usage["out"] += getattr(u, "output_tokens", 0) or 0
        bundle = resp.output_parsed
        return [it.model_dump() for it in bundle.items] if bundle else []


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
    if prov == "grok":
        if not (os.environ.get("XAI_API_KEY") or os.environ.get("GROK_API_KEY")
                or os.environ.get("API_KEY")):
            raise RuntimeError(
                "STEVIE_EVIDENCE_EXTRACTOR=grok but no XAI_API_KEY / GROK_API_KEY / "
                "API_KEY in the environment (export it; never paste keys into code)")
        return GrokExtractor()
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


# --- pre-extraction filtering (don't pay the LLM for junk pages) --------------
_MIN_CONTENT_CHARS = 500
_JUNK_URL_RE = re.compile(
    r"/(tag|tags|category|categories|topic|topics|archive|archives|search|login|"
    r"sign-?in|register|subscribe|cart|account|author|page)(/|$|\?|=)", re.I)


def is_junk_url(url: str) -> bool:
    """Cheap pre-fetch filter: listing/nav/auth pages aren't evidence."""
    return bool(_JUNK_URL_RE.search(url))


# --- source taxonomy (spend extraction budget where authority is highest) ------
# Domain -> tier. A=authoritative/official, B=major press/analyst, C=professional
# /general (default for unknowns), D=blogs/local, E=ignore (social/forums/UGC).
_TIER_B = {"forbes.com", "gartner.com", "idc.com", "fortune.com", "reuters.com",
    "bloomberg.com", "wsj.com", "ft.com", "cnbc.com", "businesswire.com",
    "prnewswire.com", "globenewswire.com", "techcrunch.com", "hbr.org",
    "economist.com", "nytimes.com", "theguardian.com", "inc.com",
    "fastcompany.com", "venturebeat.com", "zdnet.com"}
_TIER_D = {"medium.com", "wordpress.com", "blogspot.com", "substack.com",
    "prlog.org", "openpr.com", "issuu.com"}
_TIER_E = {"reddit.com", "quora.com", "pinterest.com", "facebook.com",
    "twitter.com", "x.com", "tiktok.com", "instagram.com", "glassdoor.com",
    "indeed.com", "scribd.com", "slideshare.net", "yelp.com"}


def _registered_domain(url: str) -> str:
    host = re.sub(r"^https?://", "", url or "").split("/")[0].split(":")[0].lower()
    host = host[4:] if host.startswith("www.") else host
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def source_tier(url: str) -> str:
    """Authority tier A>B>C>D>E for a URL, by registered domain."""
    host = re.sub(r"^https?://", "", url or "").split("/")[0].lower()
    dom = _registered_domain(url)
    if host.endswith(".gov") or host.endswith(".edu") or "stevieawards.com" in host \
            or "sec.gov" in host:
        return "A"
    if dom in _TIER_B:
        return "B"
    if dom in _TIER_E:
        return "E"
    if dom in _TIER_D:
        return "D"
    return "C"


# --- name-presence gate (make entity resolution explicit, not implicit) -------
# Discovery binds a page to a subject by *retrieval* (Tavily searched the name);
# nothing verifies the page actually names the subject. This gate makes that
# check explicit BEFORE the LLM: skip pages that don't mention the subject, so a
# name-collision hit (a different "Robert Frost") never gets attributed — and we
# don't pay to extract it. Diacritic- and variant-tolerant to avoid false skips.
_ORG_NAME_STOP = {"the", "of", "and", "for", "a", "an", "inc", "llc", "ltd", "co",
    "corp", "corporation", "company", "group", "holdings", "plc", "sa", "gmbh",
    "nv", "ag", "pt", "tbk", "limited", "incorporated"}
_PERSON_NAME_STOP = {"dr", "mr", "mrs", "ms", "prof", "jr", "sr", "the"}


def _norm_text(s: str) -> str:
    """Casefold, strip accents (José→jose, Özdemir→ozdemir), reduce to alnum tokens."""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", s.casefold()).strip()


def _name_in_text(text: str, name: str, subject_type: str) -> bool:
    """Token-aware name match against page body text."""
    nn = _norm_text(name or "")
    if not nn:
        return True
    tn = _norm_text(text or "")
    if f" {nn} " in f" {tn} ":                       # exact full name, token-bounded
        return True
    text_tokens = set(tn.split())
    name_tokens = [t for t in nn.split() if len(t) >= 2]
    if not name_tokens:
        return True
    if subject_type == "person":
        distinctive = [t for t in name_tokens if t not in _PERSON_NAME_STOP]
        surname = distinctive[-1] if distinctive else ""
        hits = sum(1 for t in distinctive if t in text_tokens)
        return (len(surname) >= 3 and surname in text_tokens) or hits >= 2
    # organization: keep all non-legal tokens; a single branded hit is enough
    distinctive = [t for t in name_tokens if t not in _ORG_NAME_STOP and len(t) >= 3]
    distinctive = distinctive or name_tokens
    return any(t in text_tokens for t in distinctive)


def _name_in_url(url: str, name: str, subject_type: str) -> bool:
    """URL provenance: the source URL itself names the subject. Catches legit
    first-person / own-domain posts whose body never repeats the name — e.g.
    linkedin.com/posts/bank-of-america, ug.linkedin.com/posts/hollybudge,
    webershandwick.com. Uses spaceless substring match to survive concatenated
    slugs ('hollybudge') that don't tokenize."""
    nn = _norm_text(name or "")
    if not nn:
        return False
    uf = _norm_text(url or "").replace(" ", "")      # flatten host+path to one string
    if not uf:
        return False
    if nn.replace(" ", "") in uf:                    # full name as a slug
        return True
    if subject_type == "person":
        distinctive = [t for t in nn.split() if t not in _PERSON_NAME_STOP]
        surname = distinctive[-1] if distinctive else ""
        return len(surname) >= 4 and surname in uf
    toks = [t for t in nn.split() if len(t) >= 4 and t not in _ORG_NAME_STOP]
    return any(t in uf for t in toks)


def subject_mentioned(text: str, name: str, subject_type: str = "organization",
                      url: str | None = None) -> bool:
    """Does this page belong to the subject? True if the subject is named in the
    body text OR the source URL. Full-name phrase match, else a type-aware token
    rule. Orgs: any distinctive branded token (names are short/branded). People:
    the surname anchor, or >=2 name tokens (guards common names like 'Emma He'
    from matching on the pronoun alone). Fails open on an unusable name."""
    if _name_in_text(text, name, subject_type):
        return True
    return bool(url) and _name_in_url(url, name, subject_type)


# --- orchestration ------------------------------------------------------------
async def build(crawl_run_id: uuid.UUID, n_org: int = 20, n_person: int = 20,
                url_map: dict | None = None) -> dict:
    # v3: Grok-native research (search+extract in one call) — no Tavily/fetch.
    if os.environ.get("STEVIE_EVIDENCE_DISCOVERY", "").lower() == "grok_search":
        return await research_build(crawl_run_id, n_org, n_person)
    t0 = time.monotonic()
    discovery = get_discovery()
    if isinstance(discovery, StaticDiscovery):
        discovery.url_map = url_map or await db.get_meta("evidence_urls") or {}
    extractor = get_extractor()

    org_rows, person_rows = await db.evidence_subjects(n_org, n_person)
    subjects = rank_subjects(org_rows, person_rows)

    # #2 resume skip: drop subjects that already have evidence so a restart doesn't
    # re-search them (turns the checkpoint from ~40s/done-subject into an instant
    # skip). Set STEVIE_EVIDENCE_SKIP_DONE=off to force a full re-crawl.
    skip_done = os.environ.get("STEVIE_EVIDENCE_SKIP_DONE", "on").lower() != "off"
    done = await db.evidence_done_subjects() if skip_done else set()
    pending = [s for s in subjects
               if (s["subject_type"], s["subject_slug"]) not in done]

    # #1 tune the token budget to the account's real input-tokens/min limit (also a
    # fail-fast credit check — a dead key surfaces here, not deep into the crawl).
    detected = None
    if hasattr(extractor, "detect_rate_limit"):
        detected = await extractor.detect_rate_limit()

    # #3 PIPELINED URL-level concurrency. Each subject's URLs are processed as soon
    # as THAT subject's discovery returns -- no global barrier. Rows (and the
    # dashboard) start moving immediately, one stuck search never blocks the whole
    # run, and a live ETA is possible. A global URL semaphore bounds fetch+extract
    # across all subjects; a discovery semaphore bounds concurrent searches.
    conc = max(1, int(os.environ.get("STEVIE_EVIDENCE_CONCURRENCY") or 8))
    disc_conc = max(1, int(os.environ.get("STEVIE_EVIDENCE_DISCOVERY_CONCURRENCY") or 8))
    cap = getattr(getattr(extractor, "_budget", None), "cap", None)
    print(f"[evidence] {len(subjects)} subjects; {len(pending)} pending "
          f"({len(subjects) - len(pending)} done); pipelined discovery={discovery.name} "
          f"extractor={extractor.name} conc={conc} disc={disc_conc} tpm={cap}"
          + (f" (detected {detected})" if detected else ""))

    name_gate = os.environ.get("STEVIE_EVIDENCE_NAME_GATE", "on").lower() != "off"
    counts = {"discovered": 0, "stored": 0, "subjects_done": 0}
    skipped = {"junk_url": 0, "tier_e": 0, "already_stored": 0, "fetch_fail": 0,
               "low_text": 0, "dup_content": 0, "no_subject_mention": 0,
               "extract_fail": 0, "discover_fail": 0,
               "subject_skip": len(subjects) - len(pending)}
    seen_hashes: set[str] = set()
    sem = asyncio.Semaphore(conc)             # URL-level fetch+extract
    disc_sem = asyncio.Semaphore(disc_conc)   # per-subject discovery

    async with HttpxFetcher() as fetcher:
        async def handle(s: dict, hit: Hit) -> None:
            async with sem:
                if is_junk_url(hit.url):                        # nav/listing pages
                    skipped["junk_url"] += 1
                    return
                tier = source_tier(hit.url)
                if tier == "E":                                # social/forums/UGC
                    skipped["tier_e"] += 1
                    return
                if await db.evidence_exists(s["subject_type"], s["subject_slug"], hit.url):
                    skipped["already_stored"] += 1             # never re-extract stored
                    return
                doc = await fetcher.fetch(hit.url)
                if not doc:
                    skipped["fetch_fail"] += 1
                    return
                if len(doc.text) < _MIN_CONTENT_CHARS:          # near-empty pages
                    skipped["low_text"] += 1
                    return
                digest = hashlib.sha256(doc.text.encode("utf-8")).hexdigest()
                if digest in seen_hashes:                       # syndicated/mirror dup
                    skipped["dup_content"] += 1
                    return
                seen_hashes.add(digest)
                if name_gate and not subject_mentioned(         # entity-resolution gate
                        doc.text, s.get("name", ""), s["subject_type"], url=doc.url):
                    skipped["no_subject_mention"] += 1          # off-subject/collision page
                    return
                raw_id = await db.save_raw_page(
                    url=doc.url, page_type="evidence", html=doc.html,
                    http_status=doc.status, crawl_run_id=crawl_run_id)
                try:
                    extracted = await extractor.extract(doc, s)  # only survivors hit the LLM
                except Exception as e:                           # never let one page kill the run
                    skipped["extract_fail"] += 1
                    print(f"[evidence] extract_fail {s['subject_slug']} {hit.url}: "
                          f"{type(e).__name__}: {e}")
                    return
                await db.insert_winner_evidence(
                    subject=s, url=doc.url, source_type=hit.source_type,
                    content=doc.text, extracted=extracted,
                    discovery=discovery.name, extraction=extractor.name,
                    extractor_model=getattr(extractor, "model", None),
                    extractor_version=EXTRACT_SCHEMA_VERSION, source_tier=tier,
                    raw_page_id=raw_id, crawl_run_id=crawl_run_id)
                counts["stored"] += 1

        async def process_subject(s: dict) -> None:
            async with disc_sem:                     # bound concurrent searches
                try:
                    hits = await discovery.discover(s)
                except Exception as e:               # a flaky search skips the subject
                    skipped["discover_fail"] += 1
                    print(f"[evidence] discover_fail {s['subject_slug']}: "
                          f"{type(e).__name__}: {e}")
                    hits = []
            counts["discovered"] += len(hits)
            if hits:                                 # process this subject's URLs now
                await asyncio.gather(*(handle(s, hit) for hit in hits),
                                     return_exceptions=True)
            counts["subjects_done"] += 1

        results = await asyncio.gather(*(process_subject(s) for s in pending),
                                       return_exceptions=True)
    for s, r in zip(pending, results):
        if isinstance(r, Exception):          # unexpected per-subject failure -- log, don't abort
            print(f"[evidence] subject_error {s['subject_slug']}: "
                  f"{type(r).__name__}: {r}")

    elapsed = round(time.monotonic() - t0, 1)
    usage = getattr(extractor, "usage", {"in": 0, "out": 0})
    fstats = fetcher.stats
    print(f"[evidence] discovered={counts['discovered']} extracted={counts['stored']} "
          f"skipped={skipped} fetch={fstats} tokens={usage} elapsed={elapsed}s "
          f"(extractor={extractor.name}/{getattr(extractor, 'model', None)})")
    return {"subjects": len(subjects), "pending": len(pending),
            "discovered": counts["discovered"], "extracted": counts["stored"],
            "skipped": skipped, "fetch": fstats, "tokens": usage,
            "elapsed_sec": elapsed, "model": getattr(extractor, "model", None)}


async def research_build(crawl_run_id: uuid.UUID, n_org: int = 20,
                         n_person: int = 20) -> dict:
    """v3 build: one Grok web-search call per subject returns cited, structured
    evidence items — stored directly. No Tavily, no fetch, no proxy. Subjects run
    concurrently (bounded); each item deduped on source_url and tiered."""
    t0 = time.monotonic()
    researcher = GrokResearcher()
    org_rows, person_rows = await db.evidence_subjects(n_org, n_person)
    subjects = rank_subjects(org_rows, person_rows)

    skip_done = os.environ.get("STEVIE_EVIDENCE_SKIP_DONE", "on").lower() != "off"
    done = await db.evidence_done_subjects() if skip_done else set()
    pending = [s for s in subjects
               if (s["subject_type"], s["subject_slug"]) not in done]

    conc = max(1, int(os.environ.get("STEVIE_EVIDENCE_CONCURRENCY") or 6))
    name_gate = os.environ.get("STEVIE_EVIDENCE_NAME_GATE", "on").lower() != "off"
    counts = {"items": 0, "stored": 0, "subjects_done": 0}
    skipped = {"no_url": 0, "already_stored": 0, "no_subject_mention": 0,
               "research_fail": 0, "subject_skip": len(subjects) - len(pending)}
    print(f"[research] {len(subjects)} subjects; {len(pending)} pending; "
          f"model={researcher.model} conc={conc} (grok web_search, no Tavily/fetch)")
    sem = asyncio.Semaphore(conc)

    async def process(s: dict) -> None:
        async with sem:
            try:
                items = await researcher.research(s)
            except Exception as e:            # one subject's failure must not kill the run
                skipped["research_fail"] += 1
                print(f"[research] research_fail {s['subject_slug']}: "
                      f"{type(e).__name__}: {str(e)[:150]}")
                return
        for it in items:
            counts["items"] += 1
            url = (it.get("source_url") or "").strip()
            if not url or not url.startswith("http"):
                skipped["no_url"] += 1
                continue
            if name_gate and not subject_mentioned(
                    it.get("summary", "") + " " + " ".join(it.get("quotes", [])),
                    s.get("name", ""), s["subject_type"], url=url):
                skipped["no_subject_mention"] += 1
                continue
            if await db.evidence_exists(s["subject_type"], s["subject_slug"], url):
                skipped["already_stored"] += 1
                continue
            content = (it.get("summary", "") + "\n" + "\n".join(it.get("quotes", []))).strip()
            extracted = {k: it.get(k) for k in
                         ("themes", "categories", "quoted_metrics", "quotes", "sentiment", "summary")}
            await db.insert_winner_evidence(
                subject=s, url=url, source_type="grok_search",
                content=content, extracted=extracted,
                discovery="grok_search", extraction=researcher.name,
                extractor_model=researcher.model,
                extractor_version=EXTRACT_SCHEMA_VERSION,
                source_tier=source_tier(url),
                raw_page_id=None, crawl_run_id=crawl_run_id)
            counts["stored"] += 1
        counts["subjects_done"] += 1

    results = await asyncio.gather(*(process(s) for s in pending), return_exceptions=True)
    for s, r in zip(pending, results):
        if isinstance(r, Exception):
            print(f"[research] subject_error {s['subject_slug']}: {type(r).__name__}: {r}")

    elapsed = round(time.monotonic() - t0, 1)
    usage = researcher.usage
    print(f"[research] items={counts['items']} stored={counts['stored']} "
          f"subjects_done={counts['subjects_done']} skipped={skipped} tokens={usage} "
          f"elapsed={elapsed}s (model={researcher.model})")
    return {"subjects": len(subjects), "pending": len(pending),
            "discovered": counts["items"], "extracted": counts["stored"],
            "skipped": skipped, "tokens": usage, "elapsed_sec": elapsed,
            "model": researcher.model}


async def subjects_report(n_org: int = 20, n_person: int = 20) -> None:
    org_rows, person_rows = await db.evidence_subjects(n_org, n_person)
    subs = rank_subjects(org_rows, person_rows)
    print(f"[evidence] {len(subs)} curated subjects ({n_org} orgs + {n_person} people):")
    for s in subs:
        print(f"    {s['recognitions']:4}  {s['subject_type']:12} {s['name']}")


# Density targets by subject type + prominence (recognition-count proxy). Not a
# flat 100 — reflects how much public evidence realistically exists per subject.
def _density_target(subject_type: str, recognitions: int) -> tuple[int, int, str]:
    """Returns (good, stretch, label) for a subject."""
    if subject_type == "organization":
        if recognitions >= 100:
            return (50, 100, "major org")
        return (20, 40, "mid org")
    if recognitions >= 10:
        return (30, 60, "known exec")
    return (15, 30, "executive")


def _distbar(dist: dict, total: int, width: int = 22) -> None:
    """Print a name / bar / percent distribution, largest first."""
    for k, n in sorted(dist.items(), key=lambda kv: -kv[1]):
        pct = 100 * n / total if total else 0
        bar = "#" * round(pct / 100 * width)
        print(f"    {str(k)[:18]:18} {bar:<{width}} {pct:4.0f}%  ({n})")


async def summary_report() -> None:
    """Benchmark report for the corpus — the number to compare against as the
    crawler evolves. Distributions + the most-recent run's acceptance funnel."""
    s = await db.evidence_summary()
    total = s["total"]
    covered = s["subjects"] or 1
    print("=== Evidence Summary ===")
    print(f"Subjects with evidence : {s['subjects']}")
    print(f"Accepted evidence rows : {total}")
    print(f"Avg evidence / subject : {total / covered:.1f}")
    for t, v in s["by_type"].items():
        print(f"    {t:12} {v['rows']} rows across {v['subjects']} subjects "
              f"({v['rows'] / (v['subjects'] or 1):.1f}/subj)")

    run = await db.last_run_stats("evidence")
    if run and run.get("discovered"):
        disc, kept = run["discovered"], run.get("extracted", 0)
        print(f"\nLast run funnel        : {disc} discovered -> {kept} stored "
              f"({100 * kept / disc:.1f}% acceptance)")
        for reason, n in sorted(run.get("skipped", {}).items(), key=lambda kv: -kv[1]):
            if n:
                print(f"    - {reason:20} {n}")

    tier_total = sum(s["by_tier"].values())
    print(f"\nSource tier distribution (of {tier_total} tiered rows):")
    _distbar(s["by_tier"], tier_total)
    cat_total = sum(s["by_category"].values())
    print(f"\nCategory distribution (of {cat_total} category tags):")
    _distbar(s["by_category"], cat_total)
    print("\nSentiment:")
    _distbar(s["by_sentiment"], total)
    print("\nBy extractor model:")
    _distbar(s["by_model"], total)

    print(f"\nNotes: avg confidence = {s['avg_confidence']} (flat external prior - "
          "per-row quality scoring not yet built). Dedup is content-hash at fetch "
          "time; claim-level canonicalization across sources is a future step. Tier/"
          "category coverage applies to v2 rows (extractor_version 2.0.0).")


def _median(xs: list[int]) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    m = len(s) // 2
    return float(s[m]) if len(s) % 2 else (s[m - 1] + s[m]) / 2


async def benchmark_report(n_org: int = 100, n_person: int = 0) -> None:
    """Canonical benchmark for the corpus + the most-recent crawl run — the fixed
    baseline to compare future crawler versions against. Metrics that need run-time
    telemetry (proxy recovery, tokens, duration) come from the last run's stats;
    coverage/median/domains/distributions are computed live from the corpus.
    Grok pricing is an ESTIMATE (override STEVIE_GROK_PRICE_IN/OUT $/1M tokens)."""
    s = await db.evidence_summary()
    counts = await db.evidence_counts_by_subject()
    run = await db.last_run_stats("evidence") or {}
    org_rows, person_rows = await db.evidence_subjects(n_org, n_person)
    subs = rank_subjects(org_rows, person_rows)

    # per-subject density for the graded scope (default: orgs)
    per = []
    met = 0
    for x in subs:
        n = counts.get((x["subject_type"], x["subject_slug"]), {"n": 0})["n"]
        good, _stretch, _lbl = _density_target(x["subject_type"], x["recognitions"])
        per.append(n)
        met += n >= good
    processed = len(subs)
    covered = sum(1 for n in per if n > 0)
    avg = sum(per) / covered if covered else 0
    med = _median([n for n in per if n > 0])
    coverage_pct = 100 * met / processed if processed else 0

    extracted = run.get("extracted", 0)
    after_total = s["total"]
    before_total = after_total - extracted            # extracted = net new inserts this run
    sk = run.get("skipped", {})
    fetch = run.get("fetch", {})
    fetched_ok = fetch.get("direct_ok", 0) + fetch.get("proxy_recovered", 0)
    fetch_attempts = fetched_ok + fetch.get("fail", 0)
    fetch_rate = 100 * fetched_ok / fetch_attempts if fetch_attempts else 0
    dup = sk.get("dup_content", 0)
    dup_rate = 100 * dup / (dup + fetched_ok) if (dup + fetched_ok) else 0
    tok = run.get("tokens", {"in": 0, "out": 0})
    price_in = float(os.environ.get("STEVIE_GROK_PRICE_IN") or 3.0)    # $/1M tok (est.)
    price_out = float(os.environ.get("STEVIE_GROK_PRICE_OUT") or 15.0)
    cost = tok.get("in", 0) / 1e6 * price_in + tok.get("out", 0) / 1e6 * price_out
    cost_per = cost / extracted if extracted else 0
    dur = run.get("elapsed_sec", 0)

    tier_total = sum(s["by_tier"].values()) or 1
    cat_total = sum(s["by_category"].values()) or 1

    print("=== Evidence Crawler Benchmark ===")
    print(f"Model / scope          : {run.get('model', '?')}  |  {processed} subjects graded")
    print(f"Evidence before -> after: {before_total} -> {after_total}  (+{extracted})")
    print(f"Avg evidence / subject : {avg:.1f}   (median {med:.0f}, covered {covered}/{processed})")
    print(f"Coverage vs target     : {coverage_pct:.0f}%  ({met}/{processed} at target)")
    print(f"Unique source domains  : {s['unique_domains']}")
    print(f"Fetch success rate     : {fetch_rate:.0f}%  "
          f"(direct {fetch.get('direct_ok', 0)}, proxy-recovered {fetch.get('proxy_recovered', 0)}, "
          f"lost {fetch.get('fail', 0)})")
    print(f"Proxy recovery         : {fetch.get('proxy_recovered', 0)} URLs")
    print(f"Duplicate collapse     : {dup_rate:.0f}%  ({dup} of {dup + fetched_ok} fetched)")
    print(f"Avg confidence         : {s['avg_confidence']} (flat prior - not yet scored)")
    print(f"Crawl duration         : {dur/60:.1f} min")
    print(f"Tokens (in/out)        : {tok.get('in', 0):,} / {tok.get('out', 0):,}")
    print(f"Est. cost              : ${cost:.2f}  (~${cost_per:.4f}/accepted evidence)")
    print(f"  pricing assumption   : ${price_in}/M in, ${price_out}/M out (override "
          "STEVIE_GROK_PRICE_IN/OUT; verify vs xAI billing)")
    print(f"\nSource tier distribution (of {tier_total} tiered rows):")
    _distbar(s["by_tier"], tier_total)
    print(f"\nCategory distribution (of {cat_total} tags):")
    _distbar(s["by_category"], cat_total)
    if sk:
        print("\nLast-run funnel skips:")
        for reason, n in sorted(sk.items(), key=lambda kv: -kv[1]):
            if n:
                print(f"    {reason:20} {n}")


async def coverage_report(n_org: int = 100, n_person: int = 100) -> None:
    """Per-subject evidence density vs type-based targets — pinpoints which subjects
    are sparse and where authority (tier A/B) is thin, instead of guessing."""
    org_rows, person_rows = await db.evidence_subjects(n_org, n_person)
    subs = rank_subjects(org_rows, person_rows)
    counts = await db.evidence_counts_by_subject()

    rows = []
    for s in subs:
        c = counts.get((s["subject_type"], s["subject_slug"]), {"n": 0, "ab": 0})
        good, stretch, label = _density_target(s["subject_type"], s["recognitions"])
        status = ("met" if c["n"] >= good else "partial" if c["n"] >= good / 2
                  else "sparse" if c["n"] > 0 else "none")
        rows.append({**s, "n": c["n"], "ab": c["ab"], "good": good,
                     "label": label, "status": status})

    met = sum(1 for r in rows if r["status"] == "met")
    none = sum(1 for r in rows if r["status"] == "none")
    total_rows = sum(r["n"] for r in rows)
    total_ab = sum(r["ab"] for r in rows)
    covered = [r for r in rows if r["n"] > 0]
    avg = total_rows / len(covered) if covered else 0

    print(f"[coverage] {len(rows)} subjects | {total_rows} evidence rows | "
          f"avg {avg:.1f}/covered-subject")
    print(f"[coverage] met target: {met}  |  below target: {len(rows)-met-none}  |  "
          f"no evidence: {none}  |  tier A/B share: "
          f"{(100*total_ab/total_rows if total_rows else 0):.0f}% (of tiered rows)")
    print(f"[coverage] {'STATUS':8} {'TYPE':6} {'N':>4}/{'TGT':<4} {'A/B':>4}  {'LABEL':11} SUBJECT")
    # worst first: largest gap to target, then lowest count
    for r in sorted(rows, key=lambda r: (r["n"] - r["good"], r["n"]))[:60]:
        print(f"           {r['status']:8} {r['subject_type'][:6]:6} "
              f"{r['n']:>4}/{r['good']:<4} {r['ab']:>4}  {r['label']:11} {r['name'][:40]}")
