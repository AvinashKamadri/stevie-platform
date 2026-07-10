"""
Person layer build (milestone B) — orchestration over the pure resolver.
Additive & regenerable: fills `people` + `recognition_people` from individual-
award nomination titles; touches nothing in the org model. `stevie people`.
"""
from __future__ import annotations

import uuid

from stevie_platform import db
from stevie_platform.canonical.people_extract import resolve_people


async def build(crawl_run_id: uuid.UUID) -> dict:
    records = await db.people_source_recognitions()
    people = resolve_people([dict(r) for r in records])

    await db.clear_person_layer()
    ids = await db.insert_people(people)

    links = [
        (rec_id, ids[pe["norm_key"]], pe["name"], pe["title"], pe["confidence"])
        for pe in people for rec_id in pe["rec_ids"]
    ]
    await db.insert_recognition_people(links)

    print(f"[people] {len(records)} individual-award recognitions -> "
          f"{len(people)} resolved people, {len(links)} links")
    return {"records": len(records), "people": len(people), "links": len(links)}
