"""
Human review workflow (Phase 3) — the last stage before a merge becomes
durable:

    candidate -> score -> sort -> HUMAN REVIEW -> approve/reject/related
        -> organization_merge_decision -> canonicalize replay

Two lanes, not one, per the M5 iteration findings (see canonical/scorer.py):
  main     candidates NOT flagged 'acronym', sorted by calibrated probability
           (highest first) — the score is trustworthy here.
  acronym  candidates flagged 'acronym', sorted by acronym-token length (not
           score) — three separate iterations (normalization, interaction
           terms, class weighting) all failed to make the acronym subgroup's
           probability meaningful (see features.py's module docstring for the
           root cause: confirmed acronym merges and confirmed acronym
           distincts can have near-identical feature vectors). Surfacing them
           sorted BY score would silently misrepresent model confidence that
           doesn't exist for this population.

Outcomes:
  merge     -> organization_merge_decision(decision='merge') — a winner/loser
              pick is required; defaults to the side with more recognition
              records, reviewer can swap.
  distinct  -> organization_merge_decision(decision='distinct'). NOTE the
              schema's `unique(loser_key)` is global ("one fate per losing
              key" — see M0_DECISION_STORE_DESIGN.md): if either key was
              already settled by an earlier decision, this insert is skipped
              (logged anyway) rather than erroring the session.
  related   -> organization_review_log ONLY. organization_merge_decision has
              no 'related' value by design — the relationship graph
              (parent/subsidiary/foundation) is separate, later work; this is
              durable seed data for it, not a replay-affecting decision.
  skip      -> not logged; resurfaces next session.

Every action is also written to organization_review_log (migration 015),
which is what makes a pair not resurface on a later run.
"""
from __future__ import annotations

import getpass
from datetime import datetime, timezone

from stevie_platform.canonical.candidates import order_pair

LANES = ("main", "acronym")


# --- pure: eligibility, ordering, winner selection --------------------------

def is_eligible(left_key: str, right_key: str, *, decided_keys: frozenset[str],
                 reviewed_pairs: frozenset[tuple[str, str]]) -> bool:
    """A pair drops out of the queue once either key is durably settled
    (merged away or marked distinct anywhere — organization_merge_decision's
    `unique(loser_key)` is global) or this EXACT pair has already been logged
    (any action, including a prior 'related' or a 'distinct' that hit the
    unique(loser_key) conflict — no point re-asking)."""
    if left_key in decided_keys or right_key in decided_keys:
        return False
    if (left_key, right_key) in reviewed_pairs:
        return False
    return True


def acronym_priority(left_key: str, right_key: str) -> int:
    """Review-priority (order, not score) for the acronym lane: length of the
    SHORTER key (the acronym side) — a longer acronym is less likely a
    coincidental collision ('nasa' vs 'ab'). Mirrors
    mine_hard_cases.py's acronym_strength, which the M4 yield study already
    validated as a sensible ordering signal for this exact population."""
    return min(len(left_key.replace(" ", "")), len(right_key.replace(" ", "")))


def choose_winner(left_key: str, left_recs: int, right_key: str, right_recs: int) -> tuple[str, str]:
    """Default winner/loser for a merge: more recognition records wins (more
    evidence of being the established, canonical entry). Ties default to
    left_key — arbitrary but deterministic; the reviewer can always swap.
    Returns (winner_key, loser_key)."""
    if right_recs > left_recs:
        return right_key, left_key
    return left_key, right_key


def distinct_decision_keys(left_key: str, right_key: str) -> tuple[str, str]:
    """(winner_key, loser_key) for a 'distinct' decision. Neither side
    actually loses anything — winner_key merely 'names the other side of the
    pair' per the original decision-store design — so the assignment just
    needs to be a stable, deterministic rule. Uses order_pair's convention
    (loser_key = the smaller key) purely for consistency with how every other
    table in this system orders a norm_key pair."""
    lk, _, rk, _ = order_pair(left_key, 0, right_key, 0)
    return rk, lk  # winner_key, loser_key


# --- DB-touching: queue assembly + review loop ------------------------------

async def _load_decided_keys(conn) -> frozenset[str]:
    cur = await conn.execute("select loser_key from organization_merge_decision")
    return frozenset(r["loser_key"] for r in await cur.fetchall())


async def _load_reviewed_pairs(conn) -> frozenset[tuple[str, str]]:
    cur = await conn.execute("select left_key, right_key from organization_review_log")
    return frozenset((r["left_key"], r["right_key"]) for r in await cur.fetchall())


async def _load_queue(conn, *, lane: str, model_version: str, limit: int) -> list[dict]:
    decided = await _load_decided_keys(conn)
    reviewed = await _load_reviewed_pairs(conn)

    lane_filter = "'acronym' = any(omc.reasons)" if lane == "acronym" else "not ('acronym' = any(omc.reasons))"
    cur = await conn.execute(
        f"""select omc.left_key, omc.right_key, omc.reasons, mp.probability
              from model_predictions mp
              join organization_merge_candidate omc
                on omc.left_key = mp.left_key and omc.right_key = mp.right_key
             where mp.model_version = %s and {lane_filter}""",
        (model_version,),
    )
    rows = [r for r in await cur.fetchall()
            if is_eligible(r["left_key"], r["right_key"], decided_keys=decided, reviewed_pairs=reviewed)]

    if lane == "acronym":
        rows.sort(key=lambda r: -acronym_priority(r["left_key"], r["right_key"]))
    else:
        rows.sort(key=lambda r: -float(r["probability"]))
    return rows[:limit]


async def _org_context(conn, norm_key: str) -> dict:
    cur = await conn.execute(
        """select o.name, count(distinct rp.recognition_id)::int rec_count,
                  array_agg(distinct c.name) filter (where c.name is not null) countries
             from organizations o
             left join parties p on p.organization_id = o.id
             left join recognition_parties rp on rp.party_id = p.id
             left join recognitions r on r.id = rp.recognition_id
             left join countries c on c.id = r.country_id
            where o.norm_key = %s
            group by o.name""",
        (norm_key,),
    )
    row = await cur.fetchone()
    return row or {"name": norm_key, "rec_count": 0, "countries": []}


async def _log_action(conn, *, left_key: str, right_key: str, action: str, lane: str,
                       model_version: str | None, probability, reviewed_by: str, notes: str | None) -> None:
    await conn.execute(
        """insert into organization_review_log
             (left_key, right_key, action, lane, model_version, probability, reviewed_by, notes)
           values (%s,%s,%s,%s,%s,%s,%s,%s)""",
        (left_key, right_key, action, lane, model_version, probability, reviewed_by, notes),
    )


async def run_review(*, lane: str = "main", model_version: str = "v1.1", limit: int = 50) -> dict:
    """CLI entry: interactive review session for one lane."""
    if lane not in LANES:
        raise SystemExit(f"lane must be one of {LANES}")
    from stevie_platform import db

    p = await db.pool()
    reviewer = getpass.getuser()
    counts = {"merge": 0, "distinct": 0, "related": 0, "skip": 0, "conflict": 0}

    async with p.connection() as conn:
        queue = await _load_queue(conn, lane=lane, model_version=model_version, limit=limit)
        if not queue:
            print(f"\n  queue empty for lane={lane!r} — nothing eligible to review.\n")
            return {"lane": lane, **counts}

        print(f"\n  reviewing lane={lane!r}  ({len(queue)} pairs, model={model_version})")
        for i, row in enumerate(queue):
            lk, rk = row["left_key"], row["right_key"]
            prob = float(row["probability"])
            left_ctx = await _org_context(conn, lk)
            right_ctx = await _org_context(conn, rk)

            width = 70
            print(f"\n{'─' * width}")
            score_line = "score: NOT RELIABLE for acronym pairs — see docs" if lane == "acronym" \
                else f"score: {prob:.3f}"
            print(f"  pair {i + 1}/{len(queue)}   {score_line}   reasons={list(row['reasons'])}")
            print(f"{'─' * width}")
            print(f"  A: {left_ctx['name']!r:<40}  {left_ctx['rec_count']} recs  {left_ctx['countries'] or '—'}")
            print(f"  B: {right_ctx['name']!r:<40}  {right_ctx['rec_count']} recs  {right_ctx['countries'] or '—'}")
            print(f"{'─' * width}")
            print("  [m] merge   [d] distinct   [r] related   [s] skip   [q] quit")

            try:
                choice = input("  > ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print("\n  (interrupted)")
                break
            if choice == "q":
                break
            if choice == "s" or choice not in ("m", "d", "r"):
                counts["skip"] += 1
                continue

            try:
                notes = input("  notes (optional, Enter to skip): ").strip() or None
            except (EOFError, KeyboardInterrupt):
                notes = None

            if choice == "m":
                winner, loser = choose_winner(lk, left_ctx["rec_count"], rk, right_ctx["rec_count"])
                print(f"  proposed winner: {winner!r} (loser folds away: {loser!r})")
                try:
                    swap = input("  [Enter] confirm, [s] swap winner> ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    swap = ""
                if swap == "s":
                    winner, loser = loser, winner
                try:
                    await conn.execute(
                        """insert into organization_merge_decision
                             (loser_key, winner_key, decision, source, confidence, reviewed_by)
                           values (%s,%s,'merge','manual',%s,%s)""",
                        (loser, winner, prob, reviewer),
                    )
                    await _log_action(conn, left_key=lk, right_key=rk, action="merge", lane=lane,
                                       model_version=model_version, probability=prob,
                                       reviewed_by=reviewer, notes=notes)
                    await conn.commit()
                    counts["merge"] += 1
                    print(f"  ✓ merge — {loser!r} folds into {winner!r}")
                except Exception as exc:
                    await conn.rollback()
                    print(f"  ✗ could not record merge ({exc}) — skipped")
                    counts["conflict"] += 1

            elif choice == "d":
                winner, loser = distinct_decision_keys(lk, rk)
                try:
                    await conn.execute(
                        """insert into organization_merge_decision
                             (loser_key, winner_key, decision, source, confidence, reviewed_by)
                           values (%s,%s,'distinct','manual',%s,%s)""",
                        (loser, winner, prob, reviewer),
                    )
                    await conn.commit()
                except Exception:
                    await conn.rollback()
                    print(f"  (note: {loser!r} was already settled by an earlier decision — "
                          f"suppressing this pair without a new decision row)")
                    counts["conflict"] += 1
                await _log_action(conn, left_key=lk, right_key=rk, action="distinct", lane=lane,
                                   model_version=model_version, probability=prob,
                                   reviewed_by=reviewer, notes=notes)
                await conn.commit()
                counts["distinct"] += 1
                print("  ✗ distinct")

            elif choice == "r":
                await _log_action(conn, left_key=lk, right_key=rk, action="related", lane=lane,
                                   model_version=model_version, probability=prob,
                                   reviewed_by=reviewer, notes=notes)
                await conn.commit()
                counts["related"] += 1
                print("  ~ related (logged; not a merge decision)")

    print(f"\n  session done — merge={counts['merge']} distinct={counts['distinct']} "
          f"related={counts['related']} skip={counts['skip']} conflicts={counts['conflict']}\n")
    return {"lane": lane, **counts}
