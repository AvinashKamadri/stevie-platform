"""
Parse runner: state 1 (raw_pages) -> state 2 (parsed_records).

Pure, replayable, no network. Iterates every archived detail page, parses it
with the current PARSER_VERSION, and upserts a parsed_records row. Run it any
time the parser improves — no re-crawl.
"""
from __future__ import annotations

from stevie_platform import db
from stevie_platform.parsing.parse import (
    PARSER_VERSION, is_complete_record, parse_detail,
)


async def parse_all(*, fresh: bool = False) -> dict:
    """Parse every raw detail page. If fresh, drop existing parsed rows first."""
    if fresh:
        await db.truncate_parsed()
    await db.set_meta("parser_version", {"version": PARSER_VERSION})

    total = complete = incomplete = 0
    async for raw_id, node_id, html in db.iter_raw_detail_pages():
        rec = parse_detail(html, node_id)
        ok = is_complete_record(rec)
        await db.save_parsed(raw_id, PARSER_VERSION, node_id, rec, ok)
        total += 1
        complete += ok
        incomplete += (not ok)
        if total % 1000 == 0:
            print(f"[parse] {total} parsed ({incomplete} incomplete)")
    summary = {"parser_version": PARSER_VERSION, "total": total,
               "complete": complete, "incomplete": incomplete}
    print(f"[parse] done — {summary}")
    return summary
