"""
Stage completion gates — each network/parse stage refuses to start on an
incomplete predecessor, so the pipeline can run fully unattended without a
partial crawl silently flowing downstream. Every stage operates on a COMPLETE
dataset or not at all.
"""
from __future__ import annotations

import math

from stevie_platform import db
from stevie_platform.config import ITEMS_PER_PAGE


async def _count(conn, sql, params=()):
    cur = await conn.execute(sql, params)
    return list((await cur.fetchone()).values())[0]


async def _reported_total(conn) -> int | None:
    cur = await conn.execute("select (value->>'total')::int t from meta where key='reported_total'")
    row = await cur.fetchone()
    return row["t"] if row else None


async def check_harvest_complete() -> tuple[bool, list]:
    """harvest -> fetch gate."""
    p = await db.pool()
    async with p.connection() as conn:
        total = await _reported_total(conn)
        if not total:
            return False, [("reported_total known", False, "missing — run harvest first")]
        total_pages = math.ceil(total / ITEMS_PER_PAGE)
        done   = await _count(conn, "select count(*) from harvest_state where status='done'")
        failed = await _count(conn, "select count(*) from harvest_state where status='failed'")
        queued = await _count(conn, "select count(*) from fetch_queue")
        checks = [
            ("all listing pages harvested", done == total_pages, f"{done}/{total_pages}"),
            ("zero failed pages",            failed == 0,         f"{failed} failed"),
            ("queue covers reported total",  queued >= total,     f"{queued:,}/{total:,}"),
        ]
    return all(ok for _, ok, _ in checks), checks


async def check_fetch_complete() -> tuple[bool, list]:
    """fetch -> parse gate."""
    p = await db.pool()
    async with p.connection() as conn:
        total   = await _reported_total(conn)
        fetched = await _count(conn, "select count(*) from fetch_queue where status='done'")
        pending = await _count(conn, "select count(*) from fetch_queue where status='pending'")
        failed  = await _count(conn, "select count(*) from fetch_queue where status='failed'")
        fetching = await _count(conn, "select count(*) from fetch_queue where status='fetching'")
        checks = [
            ("all details downloaded", total is not None and fetched >= total, f"{fetched:,}/{(total or 0):,}"),
            ("none pending",  pending == 0,  f"{pending:,} pending"),
            ("none fetching", fetching == 0, f"{fetching:,} in-flight"),
            ("none failed",   failed == 0,   f"{failed:,} failed"),
        ]
    return all(ok for _, ok, _ in checks), checks


def print_gate(title: str, checks: list) -> bool:
    ok = all(c[1] for c in checks)
    print(f"\n-- {title} " + "-" * max(2, 40 - len(title)))
    for name, passed, detail in checks:
        print(f"  [{'✓' if passed else '✗'}] {name:<32} {detail}")
    return ok
