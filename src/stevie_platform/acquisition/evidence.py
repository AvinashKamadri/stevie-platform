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
        # Retry transient network errors (ConnectTimeout etc.) with backoff — a
        # single flaky search must not abort a multi-hour crawl.
        last_exc = None
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    # Bearer-only auth: sending api_key in the body too makes
                    # Tavily return /goto redirect URLs instead of real URLs.
                    r = await client.post(
                        "https://api.tavily.com/search",
                        headers={"Authorization": f"Bearer {self._key}"},
                        json={"query": query, "max_results": self._max,
                              "search_depth": "basic"})
                    r.raise_for_status()
                    results = r.json().get("results", [])
                return [Hit(url=x["url"], title=x.get("title"), source_type="tavily")
                        for x in results if x.get("url")]
            except (httpx.TransportError, httpx.HTTPStatusError) as e:
                last_exc = e
                if attempt < 2:
                    await asyncio.sleep(2 * (attempt + 1))
        raise last_exc


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
    model = None

    async def extract(self, doc: Document, subject: dict) -> dict:
        return {}


_EXTRACT_PROMPT = (
    "You are extracting structured evidence about a Stevie Awards winner from a "
    "public web page. Subject: {name} ({stype}).\n\nPage text:\n{text}\n\n"
    "Extract only what the page actually supports about this subject's "
    "achievements, growth, recognition, and impact. If the page is not about the "
    "subject, return empty lists and sentiment 'neutral'.")


class EvidenceExtraction(BaseModel):
    """Shared structured-output schema — same fields across every LLM backend so
    winner_evidence.extracted is provider-agnostic."""
    themes: list[str]
    quoted_metrics: list[str]
    quotes: list[str]
    sentiment: str
    summary: str

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
        parsed = resp.choices[0].message.parsed
        # Guard against a refusal/empty parse — return the empty-shape schema.
        return parsed.model_dump() if parsed else {
            "themes": [], "quoted_metrics": [], "quotes": [],
            "sentiment": "neutral", "summary": ""}


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

    # #3 process several subjects concurrently to hide fetch/search latency; the
    # shared _TokenBudget still bounds total extraction to the per-minute cap.
    conc = max(1, int(os.environ.get("STEVIE_EVIDENCE_CONCURRENCY") or 4))
    cap = getattr(getattr(extractor, "_budget", None), "cap", None)
    print(f"[evidence] {len(subjects)} subjects; {len(pending)} pending "
          f"({len(subjects) - len(pending)} already done); discovery={discovery.name} "
          f"fetcher=httpx extractor={extractor.name} conc={conc} tpm={cap}"
          + (f" (detected {detected})" if detected else ""))

    name_gate = os.environ.get("STEVIE_EVIDENCE_NAME_GATE", "on").lower() != "off"
    counts = {"discovered": 0, "stored": 0}
    skipped = {"junk_url": 0, "already_stored": 0, "fetch_fail": 0,
               "low_text": 0, "dup_content": 0, "no_subject_mention": 0,
               "extract_fail": 0, "discover_fail": 0,
               "subject_skip": len(subjects) - len(pending)}
    seen_hashes: set[str] = set()
    sem = asyncio.Semaphore(conc)

    async with HttpxFetcher() as fetcher:
        async def process(s: dict) -> None:
            async with sem:
                try:
                    hits = await discovery.discover(s)
                except Exception as e:        # a flaky search skips the subject, not the crawl
                    skipped["discover_fail"] += 1
                    print(f"[evidence] discover_fail {s['subject_slug']}: "
                          f"{type(e).__name__}: {e}")
                    return
                for hit in hits:
                    counts["discovered"] += 1
                    if is_junk_url(hit.url):                    # nav/listing pages
                        skipped["junk_url"] += 1
                        continue
                    if await db.evidence_exists(s["subject_type"], s["subject_slug"], hit.url):
                        skipped["already_stored"] += 1          # never re-extract stored
                        continue
                    doc = await fetcher.fetch(hit.url)
                    if not doc:
                        skipped["fetch_fail"] += 1
                        continue
                    if len(doc.text) < _MIN_CONTENT_CHARS:      # skip near-empty pages
                        skipped["low_text"] += 1
                        continue
                    digest = hashlib.sha256(doc.text.encode("utf-8")).hexdigest()
                    if digest in seen_hashes:                   # syndicated/mirror dup
                        skipped["dup_content"] += 1
                        continue
                    seen_hashes.add(digest)
                    if name_gate and not subject_mentioned(     # entity-resolution gate
                            doc.text, s.get("name", ""), s["subject_type"], url=doc.url):
                        skipped["no_subject_mention"] += 1       # off-subject/collision page
                        continue
                    raw_id = await db.save_raw_page(
                        url=doc.url, page_type="evidence", html=doc.html,
                        http_status=doc.status, crawl_run_id=crawl_run_id)
                    try:
                        extracted = await extractor.extract(doc, s)  # only survivors hit the LLM
                    except Exception as e:                       # never let one page kill the run
                        skipped["extract_fail"] += 1
                        print(f"[evidence] extract_fail {s['subject_slug']} {hit.url}: "
                              f"{type(e).__name__}: {e}")
                        continue
                    await db.insert_winner_evidence(
                        subject=s, url=doc.url, source_type=hit.source_type,
                        content=doc.text, extracted=extracted,
                        discovery=discovery.name, extraction=extractor.name,
                        extractor_model=getattr(extractor, "model", None),
                        extractor_version=EXTRACT_SCHEMA_VERSION,
                        raw_page_id=raw_id, crawl_run_id=crawl_run_id)
                    counts["stored"] += 1

        results = await asyncio.gather(*(process(s) for s in pending),
                                       return_exceptions=True)
    for s, r in zip(pending, results):
        if isinstance(r, Exception):          # unexpected per-subject failure — log, don't abort
            print(f"[evidence] subject_error {s['subject_slug']}: {type(r).__name__}: {r}")

    print(f"[evidence] discovered={counts['discovered']} extracted={counts['stored']} "
          f"skipped={skipped} (extractor={extractor.name}/{getattr(extractor, 'model', None)})")
    return {"subjects": len(subjects), "pending": len(pending),
            "discovered": counts["discovered"], "extracted": counts["stored"],
            "skipped": skipped}


async def subjects_report(n_org: int = 20, n_person: int = 20) -> None:
    org_rows, person_rows = await db.evidence_subjects(n_org, n_person)
    subs = rank_subjects(org_rows, person_rows)
    print(f"[evidence] {len(subs)} curated subjects ({n_org} orgs + {n_person} people):")
    for s in subs:
        print(f"    {s['recognitions']:4}  {s['subject_type']:12} {s['name']}")
