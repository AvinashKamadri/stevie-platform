"""
Blog acquisition orchestration (Phase 1 close-out) — the DB/network glue over
the pure logic in blog_discover.py (sitemap → URLs) and parsing/blog_parse.py
(HTML → post + language).

Deliberately does NOT touch the frozen winner pipeline: no edits to fetch.py, no
use of the node_id-keyed fetch_queue (which `stevie fetch` scans). State lives in
`meta` (discovered URLs) and `raw_pages` (archived HTML, page_type='blog'), and
we reuse only the genuinely reusable pieces — AdaptiveRate + proxy config +
db.save_raw_page. blog.stevieawards.com is a different host from the Stevie WAF,
so no shared network lock is needed.

Stages (each a `stevie blog <stage>`), decoupled and replayable:
  discover → meta['blog_urls']
  fetch    → raw_pages (page_type='blog'), incremental via already-archived set
  extract  → blog_posts (English-only gate on detected language)
  report   → counts + language distribution
"""
from __future__ import annotations

import asyncio
import random
import uuid

import httpx

from stevie_platform import db
from stevie_platform.acquisition.blog_discover import parse_sitemap, select_blog_posts
from stevie_platform.acquisition.fetch import AdaptiveRate
from stevie_platform.config import (
    DETAIL_CONCURRENCY, HTTP_TIMEOUT_S, PER_PROXY_RPS, PROXIES,
    RETRY_HTTP_STATUSES, USER_AGENTS,
)
from stevie_platform.parsing.blog_parse import parse_blog_post

SITEMAP_URL = "https://blog.stevieawards.com/sitemap.xml"
_META_KEY = "blog_urls"


# --- discover ---------------------------------------------------------------
async def discover() -> int:
    """Fetch the sitemap (expanding one index level), select /blog/<slug> posts,
    and store the URL list in meta. Returns the post count."""
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S, follow_redirects=True,
                                 headers={"User-Agent": random.choice(USER_AGENTS)}) as client:
        r = await client.get(SITEMAP_URL)
        r.raise_for_status()
        children, pages = parse_sitemap(r.text)
        for child in children:                    # flat urlset today; future-proof
            cr = await client.get(child)
            if cr.status_code == 200:
                _, cp = parse_sitemap(cr.text)
                pages.extend(cp)
    posts = select_blog_posts(pages)
    await db.set_meta(_META_KEY, posts)
    print(f"[blog discover] {len(pages)} sitemap URLs -> {len(posts)} blog posts "
          f"(stored in meta['{_META_KEY}'])")
    return len(posts)


# --- fetch ------------------------------------------------------------------
async def _fetch_one(client: httpx.AsyncClient, rate: AdaptiveRate,
                     url: str, crawl_run_id: uuid.UUID) -> str:
    """Fetch + archive one post. Returns 'ok' | 'blocked' | 'err'."""
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    try:
        await rate.wait()
        r = await client.get(url, headers={"User-Agent": random.choice(USER_AGENTS)})
        if r.status_code == 200:
            rate.on_ok()
            await db.save_raw_page(url=url, page_type="blog", html=r.content,
                                   http_status=200, crawl_run_id=crawl_run_id,
                                   node_id=slug)
            return "ok"
        if r.status_code in RETRY_HTTP_STATUSES:
            rate.on_block()
            return "blocked"
        return "err"
    except Exception:  # noqa: BLE001 — one bad post must not kill the run
        rate.on_block()
        return "err"


async def fetch(crawl_run_id: uuid.UUID, limit: int | None = None) -> dict:
    """Archive discovered posts not yet in raw_pages. Incremental + resumable.
    `limit` caps this run (politeness / smoke tests)."""
    urls = await db.get_meta(_META_KEY)
    if not urls:
        print("[blog fetch] no discovered URLs — run `blog discover` first")
        return {"ok": 0, "blocked": 0, "err": 0, "skipped": 0}
    archived = await db.blog_archived_urls()
    todo = [u for u in urls if u not in archived]
    skipped = len(urls) - len(todo)
    if limit is not None:
        todo = todo[:limit]
    print(f"[blog fetch] {len(urls)} known, {skipped} already archived, "
          f"fetching {len(todo)}"
          + (f" (proxy mode: {len(PROXIES)} lanes @ {PER_PROXY_RPS} req/s)" if PROXIES else ""))

    queue: asyncio.Queue = asyncio.Queue()
    for u in todo:
        queue.put_nowait(u)
    tally = {"ok": 0, "blocked": 0, "err": 0, "skipped": skipped}

    async def worker(client: httpx.AsyncClient, rate: AdaptiveRate) -> None:
        while True:
            try:
                url = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            tally[await _fetch_one(client, rate, url, crawl_run_id)] += 1
            queue.task_done()

    if PROXIES:
        gap = 1.0 / PER_PROXY_RPS
        async def lane(proxy_url: str) -> None:
            rate = AdaptiveRate(start_gap=gap, min_gap=gap)
            async with httpx.AsyncClient(proxy=proxy_url, timeout=HTTP_TIMEOUT_S,
                                         follow_redirects=True) as client:
                await worker(client, rate)
        await asyncio.gather(*[lane(p) for p in PROXIES], return_exceptions=True)
    else:
        rate = AdaptiveRate()
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S, follow_redirects=True) as client:
            await asyncio.gather(*[worker(client, rate) for _ in range(DETAIL_CONCURRENCY)])

    print(f"[blog fetch] done — ok={tally['ok']} blocked={tally['blocked']} err={tally['err']}")
    return tally


# --- extract ----------------------------------------------------------------
async def extract(crawl_run_id: uuid.UUID) -> dict:
    """Parse archived blog HTML into blog_posts, applying the English-only gate.
    Non-English posts are counted and skipped (not stored)."""
    tally = {"kept": 0, "skipped_non_english": 0, "skipped_empty": 0, "by_lang": {}}
    async for raw_page_id, url, html in db.iter_raw_blog_pages():
        post = parse_blog_post(html, url)
        lang = post["lang"]
        tally["by_lang"][lang] = tally["by_lang"].get(lang, 0) + 1
        if not post["clean_text"]:
            tally["skipped_empty"] += 1
            continue
        if not post["is_english"]:
            tally["skipped_non_english"] += 1
            continue
        await db.upsert_blog_post(
            url=post["url"], slug=post["slug"], title=post["title"],
            author=post["author"], published_at=post["published_at"],
            lang=lang, clean_text=post["clean_text"],
            raw_page_id=raw_page_id, crawl_run_id=crawl_run_id)
        tally["kept"] += 1
    print(f"[blog extract] kept={tally['kept']} "
          f"skipped_non_english={tally['skipped_non_english']} "
          f"skipped_empty={tally['skipped_empty']}  langs={tally['by_lang']}")
    return tally


# --- link -------------------------------------------------------------------
async def link(crawl_run_id: uuid.UUID) -> dict:
    """Resolve entity mentions in every stored post into blog_entity_links.
    Truncate-and-rebuild (regenerable). Reference-only: unresolved spans drop."""
    from stevie_platform.canonical.blog_link import build_vocab, find_mentions

    programs, categories, orgs, editions = await db.blog_link_sources()
    vocab = build_vocab(programs, categories, orgs)
    edmap = {(e["program_id"], e["year"]): e["slug"] for e in editions}
    print(f"[blog link] vocab: {len(vocab)} keys "
          f"({len(programs)} programs, {len(categories)} categories, "
          f"multi-token orgs), {len(edmap)} editions")

    await db.clear_blog_entity_links()
    posts = edges = 0
    for row in await db.all_blog_posts():
        text = f"{row['title'] or ''} {row['clean_text'] or ''}"
        mentions = find_mentions(text, vocab, edmap)
        edges += await db.insert_blog_entity_links(row["id"], mentions)
        posts += 1
    print(f"[blog link] {posts} posts -> {edges} edges")
    return {"posts": posts, "edges": edges}


# --- report -----------------------------------------------------------------
async def report() -> None:
    langs = await db.blog_language_counts()
    total = sum(c["n"] for c in langs)
    print(f"[blog report] {total} posts stored")
    for c in langs:
        print(f"    lang {c['lang']:6} {c['n']}")
    links = await db.blog_link_counts()
    print(f"[blog report] {sum(c['n'] for c in links)} entity links")
    for c in links:
        print(f"    {c['entity_type']:20} {c['n']}")
