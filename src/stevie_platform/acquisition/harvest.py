"""
Phase 1 — HARVEST (Playwright).

The listing is a Drupal Views exposed form gated by a math question, so a bare
GET returns the captcha page. We drive a headless browser: solve `a + b =`,
apply the form, then page through ~1,378 listing pages collecting every node id.

Each listing page is archived to raw_pages (page_type='listing') and its node
ids are enqueued in fetch_queue. Fully resumable: harvest_state records which
pages are done, so a restart skips them.

This is the ONLY phase that needs a browser. Detail fetching (fetch.py) is
plain HTTP.
"""
from __future__ import annotations

import asyncio
import math
import random
import uuid

from playwright.async_api import async_playwright

from stevie_platform import db
from stevie_platform.config import (
    ACTION_TIMEOUT_MS, BLOCK_RESOURCE_TYPES, DELAY_BETWEEN_LISTING_PAGES,
    DETAIL_URL, HEADLESS, ITEMS_PER_PAGE, LISTING_QUERY, MAX_PAGE_ATTEMPTS,
    NAV_TIMEOUT_MS, RETRY_BACKOFF_BASE, SELECTORS, TARGET_URL, USER_AGENTS,
    VIEWPORT,
)
from stevie_platform.parsing.parse import parse_listing_ids, parse_total, solve_math


def _listing_url(page: int) -> str:
    from urllib.parse import urlencode

    q = dict(LISTING_QUERY)
    q["page"] = str(page)  # Drupal page is 0-indexed
    return f"{TARGET_URL}?{urlencode(q)}"


async def _solve_and_apply(page) -> None:
    """Read the math question text, solve it, fill the input, submit."""
    container = await page.query_selector(SELECTORS["captcha_container"])
    if container is None:
        return  # no captcha on this load
    text = await container.inner_text()
    answer = solve_math(text)
    if answer is None:
        raise RuntimeError(f"could not parse math question: {text!r}")
    await page.fill(SELECTORS["captcha_input"], str(answer))
    await page.select_option(SELECTORS["items_per_page"], str(ITEMS_PER_PAGE))
    await page.click(SELECTORS["apply_button"])
    await page.wait_for_selector(SELECTORS["result_rows"], timeout=ACTION_TIMEOUT_MS)


async def _load_listing(page, page_num: int) -> list[str]:
    """Navigate to a listing page, solve the captcha if shown, return node ids.

    Waits for the results table so we never read a half-rendered page. Raises if
    the page won't yield results — the caller retries.
    """
    await page.goto(_listing_url(page_num), wait_until="domcontentloaded")
    await _solve_and_apply(page)
    # Server-rendered, but a throttled/slow response may lack the table — make
    # its absence an explicit, retryable failure rather than a silent 0 ids.
    await page.wait_for_selector(SELECTORS["result_rows"], timeout=ACTION_TIMEOUT_MS)
    return parse_listing_ids(await page.content())


async def harvest(crawl_run_id: uuid.UUID, start_page: int = 0,
                  max_pages: int | None = None) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=HEADLESS)
        ctx = await browser.new_context(
            user_agent=random.choice(USER_AGENTS), viewport=VIEWPORT
        )
        await ctx.route(
            "**/*",
            lambda route: asyncio.ensure_future(
                route.abort() if route.request.resource_type in BLOCK_RESOURCE_TYPES
                else route.continue_()
            ),
        )
        page = await ctx.new_page()
        page.set_default_navigation_timeout(NAV_TIMEOUT_MS)

        # Establish the authoritative page count. On a resume we already know it
        # (stored in meta from the first run), so SKIP the fragile page-0 reload
        # entirely — the site throttles hard right after a full crawl and that
        # load was crashing the whole retry pass. Only do the live establish on a
        # genuine cold start, and retry it like any other page.
        cached = await db.get_meta("reported_total")
        total = (cached or {}).get("total") if cached else None
        if total is None:
            for attempt in range(1, MAX_PAGE_ATTEMPTS + 1):
                try:
                    await page.goto(_listing_url(start_page), wait_until="domcontentloaded")
                    await _solve_and_apply(page)
                    total = parse_total(await page.content())
                    if total:
                        await db.set_meta("reported_total", {"total": total})
                    break
                except Exception as e:  # noqa: BLE001
                    if attempt == MAX_PAGE_ATTEMPTS:
                        raise
                    backoff = RETRY_BACKOFF_BASE * attempt
                    print(f"[harvest] establish-total attempt {attempt} failed "
                          f"({str(e)[:80]}) — retrying in {backoff:.0f}s")
                    await asyncio.sleep(backoff)
        total_pages = math.ceil(total / ITEMS_PER_PAGE) if total else None
        last = min(start_page + max_pages, total_pages or 10**9) if max_pages \
            else (total_pages or 10**9)
        done = await db.get_done_harvest_pages()  # resume: skip finished pages
        print(f"[harvest] reported_total={total} total_pages={total_pages} "
              f"target {start_page}..{last} ({len(done)} already done)")

        failed: list[int] = []
        for page_num in range(start_page, last):
            if page_num in done:
                continue
            # Last page may legitimately have <60 rows; only it may be empty.
            is_last = total_pages is not None and page_num == total_pages - 1
            for attempt in range(1, MAX_PAGE_ATTEMPTS + 1):
                try:
                    ids = await _load_listing(page, page_num)
                    if not ids and not is_last:
                        raise RuntimeError("0 ids on a non-final page (throttled?)")
                    raw_id = await db.save_raw_page(
                        url=_listing_url(page_num), page_type="listing",
                        html=(await page.content()).encode(), http_status=200,
                        crawl_run_id=crawl_run_id, listing_page=page_num,
                    )
                    await db.enqueue_details([
                        (nid, DETAIL_URL.format(id=nid), page_num, pos)
                        for pos, nid in enumerate(ids)
                    ])
                    await db.upsert_harvest_page(page_num, "done", ids_found=len(ids),
                                                 raw_page_id=raw_id)
                    print(f"[harvest] page {page_num}: +{len(ids)} ids")
                    break
                except Exception as e:  # noqa: BLE001
                    if attempt < MAX_PAGE_ATTEMPTS:
                        backoff = RETRY_BACKOFF_BASE * attempt
                        print(f"[harvest] page {page_num} attempt {attempt} failed "
                              f"({str(e)[:80]}) — retrying in {backoff:.0f}s")
                        await asyncio.sleep(backoff)
                    else:
                        await db.upsert_harvest_page(page_num, "failed", error=str(e)[:500])
                        failed.append(page_num)
                        print(f"[harvest] page {page_num} FAILED after {attempt} attempts — "
                              f"left for a later re-run, continuing")
            await asyncio.sleep(random.uniform(*DELAY_BETWEEN_LISTING_PAGES))

        await browser.close()
    if failed:
        print(f"[harvest] done with {len(failed)} failed pages: {failed[:20]}"
              f"{'...' if len(failed) > 20 else ''} — re-run `harvest` to retry them")
    else:
        print("[harvest] done — all targeted pages harvested")
