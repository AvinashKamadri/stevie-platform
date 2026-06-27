"""
Data Quality Report — runs after canonicalization; the platform's health check.

If a Stevie site change breaks category parsing or drops countries, this catches
it immediately (a column goes to zero / a distribution collapses) — long before
any AI feature would notice. Plain text to stdout; every number is one SQL query
so it's cheap to run after every crawl.
"""
from __future__ import annotations

from stevie_platform import db


async def _scalar(conn, sql, params=()):
    cur = await conn.execute(sql, params)
    row = await cur.fetchone()
    return list(row.values())[0] if row else None


async def _rows(conn, sql, params=()):
    cur = await conn.execute(sql, params)
    return await cur.fetchall()


def _bar(label, n, total, width=24):
    filled = int(width * n / total) if total else 0
    return f"  {label:<22} {'█'*filled}{'·'*(width-filled)} {n:>7,}"


async def print_report() -> None:
    p = await db.pool()
    async with p.connection() as conn:
        recognitions   = await _scalar(conn, "select count(*) from recognitions")
        parsed_total   = await _scalar(conn, "select count(*) from parsed_records")
        parsed_ok      = await _scalar(conn, "select count(*) from parsed_records where is_complete")
        parsed_bad     = parsed_total - (parsed_ok or 0)
        orgs           = await _scalar(conn, "select count(*) from organizations")
        org_exact      = await _scalar(conn, "select count(*) from entity_links where entity_type='organization' and match_method='exact'")
        org_new        = await _scalar(conn, "select count(*) from entity_links where entity_type='organization' and match_method='new'")
        pending_cands  = await _scalar(conn, "select count(*) from entity_candidates where accepted is null")
        countries      = await _scalar(conn, "select count(*) from countries")
        categories     = await _scalar(conn, "select count(*) from category_definitions")
        programs       = await _scalar(conn, "select count(*) from programs")

        print("\n" + "=" * 56)
        print(" STEVIE PLATFORM — DATA QUALITY REPORT")
        print("=" * 56)
        print(f"  recognitions ingested      : {recognitions:>8,}")
        rate = (100.0 * (parsed_ok or 0) / parsed_total) if parsed_total else 0
        print(f"  parsed records             : {parsed_total:>8,}  ({rate:.1f}% complete)")
        print(f"  records missing req. fields: {parsed_bad:>8,}")
        print(f"  organizations              : {orgs:>8,}")
        print(f"    exact reuse / newly made : {org_exact or 0:>8,} / {org_new or 0:,}")
        print(f"  candidate merges to review : {pending_cands:>8,}")
        print(f"  countries / categories / programs : {countries} / {categories} / {programs}")

        print("\n-- distribution by result level " + "-" * 24)
        rl = await _rows(conn, "select result_level, count(*) n from recognitions group by result_level order by n desc")
        for r in rl:
            print(_bar(r["result_level"], r["n"], recognitions))

        print("\n-- recognitions by program " + "-" * 29)
        for r in await _rows(conn, "select name, total_recognitions n from program_stats order by n desc"):
            print(_bar(r["name"][:22], r["n"], recognitions))

        print("\n-- recognitions by year (recent) " + "-" * 23)
        yr = await _rows(conn, "select year, count(*) n from recognitions where year is not null group by year order by year desc limit 12")
        ymax = max((r["n"] for r in yr), default=1)
        for r in yr:
            print(_bar(str(r["year"]), r["n"], ymax))

        print("\n-- top 15 countries " + "-" * 36)
        for r in await _rows(conn, "select name, total_recognitions n from country_stats order by n desc limit 15"):
            print(_bar(r["name"][:22], r["n"], recognitions))

        print("\n-- top 15 organizations by recognitions " + "-" * 16)
        for r in await _rows(conn, "select name, total_recognitions, gold, prestige_score from organization_stats order by total_recognitions desc limit 15"):
            print(f"  {r['name'][:34]:<34} {r['total_recognitions']:>4}  gold={r['gold']:<3} prestige={r['prestige_score']}")

        print("\n-- top 15 categories (by lineage) " + "-" * 22)
        for r in await _rows(conn, "select name, total_recognitions n from category_stats order by n desc limit 15"):
            print(f"  {r['name'][:44]:<44} {r['n']:>5}")

        if pending_cands:
            print("\n-- sample candidate merges awaiting review " + "-" * 13)
            for r in await _rows(conn, """
                select ec.raw_value, o.name candidate, round(ec.score,2) score
                from entity_candidates ec join organizations o on o.id = ec.candidate_entity_id
                where ec.accepted is null order by ec.score desc limit 10"""):
                print(f"  {r['raw_value'][:30]:<30} ~= {r['candidate'][:26]:<26} ({r['score']})")
        print("=" * 56 + "\n")
