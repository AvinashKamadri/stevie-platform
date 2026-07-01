"""
Stevie Platform acquisition CLI.

  python -m stevie_platform.cli migrate          apply migrations/*.sql
  python -m stevie_platform.cli harvest           Phase 1a: collect node ids (Playwright)
  python -m stevie_platform.cli fetch             Phase 1b: archive detail HTML (httpx)
  python -m stevie_platform.cli parse [--fresh]   state 1 -> state 2 (no network)
  python -m stevie_platform.cli status            completeness report
  python -m stevie_platform.cli reparse           alias for `parse --fresh`

Crawl (harvest + fetch) and parse are decoupled: you can reparse the archive
endlessly without touching the network.
"""
from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
from pathlib import Path

from stevie_platform import db
from stevie_platform.config import BASE_DIR, PIPELINE_MODE


def _git_commit() -> str | None:
    """Best-effort current commit, for run provenance. None if not a git repo."""
    try:
        return subprocess.check_output(
            ["git", "-C", str(BASE_DIR), "rev-parse", "HEAD"],
            stderr=subprocess.DEVNULL, text=True,
        ).strip()
    except Exception:  # noqa: BLE001
        return None


class _NoLock:
    """Stand-in when no mutex is needed (parallel mode), so callers can uniformly
    `await lock.close()`."""
    async def close(self) -> None:  # noqa: D401
        return None


async def _acquire_network_stage(stage: str):
    """In sequential mode, take the shared network lock so harvest/fetch can't
    overlap. Returns a closeable lock handle, or None if blocked (caller aborts)."""
    if PIPELINE_MODE != "sequential":
        return _NoLock()
    lock = await db.try_network_lock()
    if lock is None:
        print(f"[{stage}] ABORT — PIPELINE_MODE=sequential and another network stage "
              f"is already running (it holds the lock). Wait for it to finish, or set "
              f"STEVIE_PIPELINE_MODE=parallel to override.")
        return None
    return lock


async def _migrate() -> None:
    p = await db.pool()
    mig_dir = BASE_DIR / "migrations"
    for sql_file in sorted(mig_dir.glob("*.sql")):
        print(f"[migrate] {sql_file.name}")
        async with p.connection() as conn:
            await conn.execute(Path(sql_file).read_text())
    print("[migrate] done")


async def _status() -> None:
    s = await db.get_status()
    if not s:
        print("no data yet — run migrate first")
        return
    rt = s.get("reported_total")
    print("Stevie Platform — acquisition status")
    print("-" * 40)
    print(f"  reported total : {rt}")
    print(f"  discovered     : {s['discovered']}")
    print(f"  fetched        : {s['fetched']}")
    print(f"  pending        : {s['pending']}")
    print(f"  failed         : {s['failed']}")
    print(f"  parsed         : {s['parsed']}")
    print(f"  parsed (bad)   : {s['parsed_incomplete']}")
    if rt:
        done = s["fetched"] == rt and s["pending"] == 0 and s["failed"] == 0
        print("-" * 40)
        print("  COMPLETE ✅" if done else "  incomplete — keep crawling")


async def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="stevie_platform.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate")
    h = sub.add_parser("harvest")
    h.add_argument("--start-page", type=int, default=0)
    h.add_argument("--max-pages", type=int, default=None)
    h.add_argument("--resume", action="store_true", help="re-run to retry failed/pending pages (default behavior)")
    f = sub.add_parser("fetch")
    f.add_argument("--force", action="store_true", help="skip the harvest-complete gate")
    pp = sub.add_parser("parse")
    pp.add_argument("--fresh", action="store_true")
    pp.add_argument("--force", action="store_true", help="skip the fetch-complete gate")
    sub.add_parser("reparse")
    cz = sub.add_parser("canonicalize")
    cz.add_argument("--keep", action="store_true", help="don't truncate canonical first")
    cn = sub.add_parser("candidates")
    cn.add_argument("--no-persist", action="store_true", help="generate + report only, don't write the table")
    rc = sub.add_parser("recall")
    rc.add_argument("--corpus", default=None, help="gold corpus version (e.g. v1, v2; default from CORPUS.json)")
    sub.add_parser("report")
    sub.add_parser("metrics")
    sub.add_parser("gates")
    sub.add_parser("status")
    args = parser.parse_args(argv)

    rc = 0
    try:
        if args.cmd == "migrate":
            await _migrate()
        elif args.cmd == "status":
            await _status()
        elif args.cmd == "harvest":
            from stevie_platform.acquisition.harvest import harvest
            lock = await _acquire_network_stage("harvest")
            if lock is None:
                return 1
            try:
                run_id = await db.start_crawl_run("harvest", git_commit=_git_commit())
                await harvest(run_id, start_page=args.start_page, max_pages=args.max_pages)
                await db.finish_crawl_run(run_id)
            finally:
                await lock.close()
        elif args.cmd == "fetch":
            from stevie_platform.acquisition.fetch import fetch_all
            from stevie_platform.acquisition.preflight import check_harvest_complete, print_gate
            if not args.force:
                ok, checks = await check_harvest_complete()
                print_gate("harvest-complete gate", checks)
                if not ok:
                    print("[fetch] ABORT — harvest is incomplete. Run `harvest --resume` "
                          "to finish/retry, or `fetch --force` to override.")
                    return 1
            lock = await _acquire_network_stage("fetch")
            if lock is None:
                return 1
            try:
                run_id = await db.start_crawl_run("fetch", git_commit=_git_commit())
                await fetch_all(run_id)
                await db.finish_crawl_run(run_id)
            finally:
                await lock.close()
        elif args.cmd in ("parse", "reparse"):
            from stevie_platform.parsing.run import parse_all
            from stevie_platform.parsing.parse import PARSER_VERSION
            if args.cmd == "parse" and not getattr(args, "force", False):
                from stevie_platform.acquisition.preflight import check_fetch_complete, print_gate
                ok, checks = await check_fetch_complete()
                print_gate("fetch-complete gate", checks)
                if not ok:
                    print("[parse] ABORT — fetch is incomplete. Run `fetch` to finish, "
                          "or `parse --force` to parse the partial archive.")
                    return 1
            run_id = await db.start_crawl_run("parse", parser_version=PARSER_VERSION,
                                              git_commit=_git_commit())
            summary = await parse_all(fresh=args.cmd == "reparse" or getattr(args, "fresh", False))
            await db.finish_crawl_run(run_id, summary)
        elif args.cmd == "canonicalize":
            from stevie_platform.canonical.pipeline import canonicalize
            from stevie_platform.parsing.parse import PARSER_VERSION
            run_id = await db.start_crawl_run("canonicalize", parser_version=PARSER_VERSION,
                                              git_commit=_git_commit())
            summary = await canonicalize(run_id, fresh=not args.keep)
            await db.finish_crawl_run(run_id, summary)
        elif args.cmd == "candidates":
            from stevie_platform.canonical.candidates import run_candidates
            await run_candidates(persist_rows=not args.no_persist)
        elif args.cmd == "recall":
            from stevie_platform.canonical.recall import run_recall
            await run_recall(corpus=args.corpus)
        elif args.cmd == "report":
            from stevie_platform.canonical.report import print_report
            await print_report()
        elif args.cmd == "metrics":
            from stevie_platform.canonical.metrics import print_canonicalization_metrics
            run = await db.last_run_stats("canonicalize")
            await print_canonicalization_metrics(run or {})
        elif args.cmd == "gates":
            from stevie_platform.canonical.gates import run_gates
            ok = await run_gates()
            rc = 0 if ok else 1
    finally:
        await db.close()
    return rc


def main() -> None:
    if sys.platform == "win32":
        # psycopg's async pool can't use Windows' default ProactorEventLoop;
        # the SelectorEventLoop is required. No-op on Linux/macOS.
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    raise SystemExit(asyncio.run(_main(sys.argv[1:])))


if __name__ == "__main__":
    main()
