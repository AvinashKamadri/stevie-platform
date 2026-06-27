"""
Phase 1 — FETCH (httpx, no browser).

The /view-details/{id} endpoint is captcha-free, so detail fetching is plain
concurrent HTTP. We DO NOT parse here — we only archive raw HTML to raw_pages
(page_type='detail'). Parsing is a separate, replayable step.

Pacing uses AIMD (additive-increase / multiplicative-decrease): ramp toward the
proven-safe ~1.3 req/s while we get 200s, back off hard on 403/429. This
auto-finds the IP throttle ceiling instead of guessing.

Resumable: only 'pending' rows are claimed. Re-running picks up where it left
off; failures bounce back to 'pending' until attempts are exhausted.
"""
from __future__ import annotations

import asyncio
import random
import uuid

import httpx

from stevie_platform import db
from stevie_platform.config import (
    DETAIL_CONCURRENCY, HTTP_TIMEOUT_S, MAX_DETAIL_ATTEMPTS, PER_PROXY_RPS,
    PROXIES, RATE_BACKOFF, RATE_GAP_STEP, RATE_MAX_GAP, RATE_MIN_GAP,
    RATE_SPEEDUP_AFTER, RATE_START_GAP, RETRY_HTTP_STATUSES, USER_AGENTS,
)


class AdaptiveRate:
    """Shared pacing gate across concurrent fetch workers (AIMD)."""

    def __init__(self, start_gap: float | None = None, min_gap: float | None = None) -> None:
        self.gap = RATE_START_GAP if start_gap is None else start_gap
        self._min_gap = RATE_MIN_GAP if min_gap is None else min_gap
        self._clean_streak = 0
        self._lock = asyncio.Lock()
        self._next_at = 0.0

    async def wait(self) -> None:
        async with self._lock:
            loop = asyncio.get_event_loop()
            now = loop.time()
            start = max(now, self._next_at)
            self._next_at = start + self.gap
            delay = start - now
        if delay > 0:
            await asyncio.sleep(delay)

    def on_ok(self) -> None:
        self._clean_streak += 1
        if self._clean_streak >= RATE_SPEEDUP_AFTER:
            self.gap = max(self._min_gap, self.gap - RATE_GAP_STEP)
            self._clean_streak = 0

    def on_block(self) -> None:
        self._clean_streak = 0
        self.gap = min(RATE_MAX_GAP, self.gap * RATE_BACKOFF)


async def _worker(client: httpx.AsyncClient, rate: AdaptiveRate,
                  crawl_run_id: uuid.UUID, queue: asyncio.Queue) -> None:
    while True:
        item = await queue.get()
        if item is None:
            queue.task_done()
            return
        node_id, url = item["node_id"], item["detail_url"]
        try:
            await rate.wait()
            r = await client.get(url, headers={"User-Agent": random.choice(USER_AGENTS)})
            if r.status_code in RETRY_HTTP_STATUSES:
                rate.on_block()
                await db.mark_failed(node_id, f"HTTP {r.status_code}", MAX_DETAIL_ATTEMPTS)
            elif r.status_code == 200:
                rate.on_ok()
                raw_id = await db.save_raw_page(
                    url=url, page_type="detail", html=r.content,
                    http_status=200, crawl_run_id=crawl_run_id, node_id=node_id,
                )
                await db.mark_fetched(node_id, raw_id)
            else:
                await db.mark_failed(node_id, f"HTTP {r.status_code}", MAX_DETAIL_ATTEMPTS)
        except Exception as e:  # noqa: BLE001
            rate.on_block()
            await db.mark_failed(node_id, str(e)[:500], MAX_DETAIL_ATTEMPTS)
        finally:
            queue.task_done()


async def _proxy_lane(proxy_url: str, label: str, crawl_run_id: uuid.UUID) -> dict:
    """One exit IP = one lane: claims from the shared queue and fetches through
    its proxy at ~PER_PROXY_RPS, with its OWN AIMD backoff so a flaky proxy only
    slows its own lane. N lanes ≈ N × PER_PROXY_RPS total throughput."""
    gap = 1.0 / PER_PROXY_RPS
    rate = AdaptiveRate(start_gap=gap, min_gap=gap)
    done = blocked = 0
    try:
        async with httpx.AsyncClient(proxy=proxy_url, timeout=HTTP_TIMEOUT_S,
                                     follow_redirects=True) as client:
            while True:
                batch = await db.claim_pending(8)
                if not batch:
                    break
                for item in batch:
                    node_id, url = item["node_id"], item["detail_url"]
                    try:
                        await rate.wait()
                        r = await client.get(url, headers={"User-Agent": random.choice(USER_AGENTS)})
                        if r.status_code == 200:
                            rate.on_ok()
                            raw_id = await db.save_raw_page(
                                url=url, page_type="detail", html=r.content,
                                http_status=200, crawl_run_id=crawl_run_id, node_id=node_id)
                            await db.mark_fetched(node_id, raw_id)
                            done += 1
                        elif r.status_code in RETRY_HTTP_STATUSES:
                            rate.on_block()
                            await db.mark_failed(node_id, f"HTTP {r.status_code}", MAX_DETAIL_ATTEMPTS)
                            blocked += 1
                        else:
                            await db.mark_failed(node_id, f"HTTP {r.status_code}", MAX_DETAIL_ATTEMPTS)
                    except Exception as e:  # noqa: BLE001
                        rate.on_block()
                        await db.mark_failed(node_id, str(e)[:500], MAX_DETAIL_ATTEMPTS)
                        blocked += 1
    except Exception as e:  # noqa: BLE001 — lane-level failure (bad proxy); don't kill the run
        print(f"[fetch] lane {label} ({proxy_url.split('@')[-1]}) died: {str(e)[:80]}")
    return {"lane": label, "done": done, "blocked": blocked}


async def fetch_all(crawl_run_id: uuid.UUID) -> None:
    requeued = await db.requeue_stale_fetching()
    if requeued:
        print(f"[fetch] resumed — requeued {requeued} stale in-flight claims from a prior run")

    # Proxy mode: one rate-limited lane per exit IP (dodges the per-IP throttle).
    if PROXIES:
        print(f"[fetch] proxy mode — {len(PROXIES)} lanes @ {PER_PROXY_RPS} req/s each "
              f"(~{len(PROXIES) * PER_PROXY_RPS:.0f} req/s target)")
        results = await asyncio.gather(
            *[_proxy_lane(p, f"p{i}", crawl_run_id) for i, p in enumerate(PROXIES)],
            return_exceptions=True)
        for r in results:
            if isinstance(r, dict):
                print(f"[fetch]   {r['lane']}: {r['done']} done, {r['blocked']} blocked")
        print(f"[fetch] done — {await db.count_pending()} still pending")
        return

    rate = AdaptiveRate()
    queue: asyncio.Queue = asyncio.Queue(maxsize=DETAIL_CONCURRENCY * 4)
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S, follow_redirects=True) as client:
        workers = [
            asyncio.create_task(_worker(client, rate, crawl_run_id, queue))
            for _ in range(DETAIL_CONCURRENCY)
        ]
        fetched = 0
        while True:
            batch = await db.claim_pending(DETAIL_CONCURRENCY * 4)
            if not batch:
                break
            for row in batch:
                await queue.put(row)
                fetched += 1
            if fetched % 500 < len(batch):
                print(f"[fetch] claimed ~{fetched}, gap={rate.gap:.2f}s "
                      f"(~{1/rate.gap:.2f} req/s)")
        await queue.join()
        for _ in workers:
            await queue.put(None)
        await asyncio.gather(*workers)
    print(f"[fetch] done — {await db.count_pending()} still pending")
