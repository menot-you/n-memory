"""The curator's candidate lists: which capsules the next pass reads."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Final

from ..capsule import format_rfc3339
from ..database import Database
from ..project import glob_subtree_pattern
from ..relation import live_relation_sql
from ..repositories.base import StoreError
from .staged import REVIEW_REASON_EVIDENCE

#: The mechanical anchor verdicts that the curator queue can serve. The
#: order is NOT a preference, it is only the legal set: `judge_anchor` counts
#: lines, and only these two mean "the anchor stopped matching".
CURATOR_VERDICTS: Final = ("dead_anchor", "moved_anchor", "changed_anchor")


def unclassified_capsules(
    db: Database, *, project: str | None, limit: int
) -> list[tuple[str, str]]:
    """U5: `(id, content)` of live capsules WITHOUT a row in `classifications`.

    Same project fence as `curator_candidates` (equality or subtree),
    append order. `limit` is mandatory and positive: triage without a ceiling
    burns the entire budget on the first pass.
    """
    if limit <= 0:
        raise StoreError(
            f"unclassified_capsules: limit={limit} is not positive; "
            'a queue without ceiling reads as "nothing to classify" or spends everything'
        )
    params: list[object] = []
    fence = ""
    if project is not None:
        fence = "AND (c.project_id = ? OR c.project_id GLOB ?)"
        params.extend([project, glob_subtree_pattern(project)])
    params.append(limit)
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT c.id, c.canonical_json
            FROM capsules c
            LEFT JOIN classifications cl ON cl.capsule_id = c.id
            WHERE cl.capsule_id IS NULL
              AND c.canonical_json IS NOT NULL
              AND c.id NOT IN (SELECT capsule_id FROM tombstones)
            {fence}
            ORDER BY c.seq ASC
            LIMIT ?
            """,
            params,
        )
        rows = cur.fetchall()
    return [(str(r[0]), str(json.loads(r[1])["content"])) for r in rows]


def curator_daily_candidates(db: Database, project: str, now: datetime) -> list[str]:
    """Since the last completed pass, plus unresolved ambiguity and failures."""
    day = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT at FROM audit_events "
            "WHERE action = 'curator_daily_pass' AND subject = ? AND ts(at) <= ts(?) "
            "ORDER BY ts(at) DESC LIMIT 1",
            (project, format_rfc3339(now)),
        )
        previous = cur.fetchone()
        since = (
            str(previous[0])
            if previous and previous[0] is not None
            else format_rfc3339(day)
        )
        cur.execute(
            """
            WITH observations AS (
                SELECT capsule_id, question, choice, at
                FROM jev_observations
                WHERE seq IN (
                    SELECT max(seq) FROM jev_observations
                    WHERE ts(at) <= ts(:now)
                    GROUP BY capsule_id, question)
            )
            SELECT c.id FROM capsules c
            WHERE c.canonical_json IS NOT NULL
              AND (c.project_id = :project OR c.project_id GLOB :prefix)
              AND NOT EXISTS (SELECT 1 FROM tombstones t WHERE t.capsule_id = c.id)
              AND NOT EXISTS (
                   SELECT 1 FROM review_events r WHERE r.capsule_id = c.id
                   AND r.reason = :evidence
              )
              AND (
                  (ts(c.created_at) >= ts(:since)
                   AND ts(c.created_at) <= ts(:now)
                   AND NOT EXISTS (
                       SELECT 1 FROM audit_events completed
                       WHERE completed.subject = c.id
                         AND completed.action = 'curator_daily_complete'
                         AND ts(completed.at) >= ts(c.created_at)
                         AND ts(completed.at) <= ts(:now)
                   ))
                  OR EXISTS (
                      SELECT 1 FROM audit_events pending
                      WHERE pending.subject = c.id
                        AND pending.action IN ('curator_daily_failed', 'curator_daily_pending')
                        AND ts(pending.at) <= ts(:now)
                        AND NOT EXISTS (
                            SELECT 1 FROM audit_events done
                            WHERE done.subject = c.id AND done.action = 'curator_daily_complete'
                              AND done.seq > pending.seq
                        )
                  )
                  OR EXISTS (
                      SELECT 1 FROM observations o WHERE o.capsule_id = c.id
                      AND o.question IN ('curator', 'staged')
                      AND o.choice IN ('nenhuma', 'nao_decide', 'moved')
                      AND (o.question <> 'staged' OR (
                          SELECT r.verdict FROM review_events r
                          WHERE r.capsule_id = c.id ORDER BY r.seq DESC LIMIT 1
                      ) = 'proposed')
                      AND NOT EXISTS (
                          SELECT 1 FROM audit_events a
                          WHERE a.subject = c.id
                            AND a.action = 'curator_daily_' || o.question || '_complete'
                            AND ts(a.at) >= ts(o.at)
                      )
                  )
              )
              AND NOT EXISTS (
                  SELECT 1 FROM audit_events a
                  WHERE a.subject = c.id
                    AND a.action IN ('curator_daily_complete', 'curator_daily_pending')
                    AND a.reason = :provenance
              )
            ORDER BY c.seq
            """,
            {
                "project": project,
                "prefix": glob_subtree_pattern(project),
                "since": since,
                "now": format_rfc3339(now),
                "provenance": f"curator-daily:{day.date().isoformat()}",
                "evidence": REVIEW_REASON_EVIDENCE,
            },
        )
        rows = cur.fetchall()
    return [str(row[0]) for row in rows]


def curator_candidates(
    db: Database,
    *,
    project: str | None,
    now: datetime,
    verdicts: Sequence[str] | None = None,
) -> list[tuple[str, str]]:
    """U4a: capsules whose LATEST event is a mechanical anchor judgment
    still without verdict — the `curator` event written later by the worker
    becomes the latest, and the queue empties itself until the anchor changes again.

    One query: the latest ANCHOR event and latest CURATOR event per capsule,
    with the project fence in SQL (no `get()` per candidate). A verdict
    already given for the SAME dead anchor (curator detail begins
    with anchor detail) does not return to the queue: `audit_all` rewrites
    `dead_anchor` on each sweep (review 42eae928). An `error` (timeout,
    credential, transport) only fences the queue for ONE day: afterward the capsule returns
    — one call/day per stuck capsule, never terminal (review 0936f878).

    `verdicts` narrows the queue to a subset of `CURATOR_VERDICTS`. It exists
    because curator error is NOT uniform: measured in U5 (2026-09-17), for
    dead anchors it got 7 of 7 fences right and zero wrong, and for moved anchors it got
    4 of 8 wrong even after excerpt healing. Draining the debt at once is
    safe on the first set and not on the second, and without this fence draining
    fences truthful capsules. A verdict outside the set is a REFUSAL, never an
    empty queue: an empty queue reads as "nothing to judge", and that false zero
    misled me once in this campaign.
    """
    chosen = tuple(verdicts) if verdicts is not None else CURATOR_VERDICTS
    if not chosen:
        raise StoreError(
            "curator_candidates: empty `verdicts` fences nothing, returns nothing, "
            'and reads as "nothing to judge" — pass None for the entire queue'
        )
    unknown = [v for v in chosen if v not in CURATOR_VERDICTS]
    if unknown:
        raise StoreError(
            f"curator_candidates: {unknown} is not a mechanical anchor verdict; "
            f"the queue serves {list(CURATOR_VERDICTS)}"
        )
    cutoff = format_rfc3339(now - timedelta(days=1))
    params: list[object] = [cutoff, cutoff, list(chosen)]
    fence = ""
    if project is not None:
        fence = "AND (c.project_id = ? OR c.project_id GLOB ?)"
        params.extend([project, glob_subtree_pattern(project)])
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            WITH latest AS (
                SELECT capsule_id, seq, kind, result, detail
                FROM verification_events
                WHERE seq IN (
                    SELECT max(seq) FROM verification_events WHERE NOT (kind = 'curator' AND result = 'error' AND at <= ?)
                    GROUP BY capsule_id)
            ), judged AS (
                SELECT capsule_id, result, detail
                FROM verification_events
                WHERE seq IN (
                    SELECT max(seq) FROM verification_events WHERE kind = 'curator' AND (result <> 'error' OR at > ?)
                    GROUP BY capsule_id)
            )
            SELECT v.capsule_id, v.result, v.detail, j.result, j.detail
            FROM latest v
            JOIN capsules c ON c.id = v.capsule_id
            LEFT JOIN judged j ON j.capsule_id = v.capsule_id
            WHERE v.kind = 'anchor' AND v.result IN (SELECT value FROM json_each(?))
            {fence}
            ORDER BY v.seq ASC
            """,
            params,
        )
        rows = cur.fetchall()
    if not rows:
        return []
    dead = sibling_excluded_among(db, [str(r[0]) for r in rows])
    out: list[tuple[str, str]] = []
    for r in rows:
        cid, judgement = str(r[0]), f"{r[1]} at {r[2]}"
        if cid in dead:
            continue
        if r[3] is not None and str(r[4]).split("|", 1)[0] == judgement:
            continue
        out.append((cid, judgement))
    return out


def sibling_excluded_among(db: Database, candidate_ids: list[str]) -> set[str]:
    if not candidate_ids:
        return set()
    with db.connection() as conn, conn.cursor() as cur:
        excluded: set[str] = set()
        cur.execute(
            "SELECT capsule_id FROM tiers WHERE tier IN ('quarantined', 'archived') AND capsule_id IN (SELECT value FROM json_each(?))",
            (candidate_ids,),
        )
        excluded.update(str(r[0]) for r in cur.fetchall())

        cur.execute(
            "SELECT r.to_id FROM relations r WHERE r.kind IN ('falsifies', 'supersedes') "
            "AND r.to_id IN (SELECT value FROM json_each(?)) AND "
            + live_relation_sql(),
            (candidate_ids,),
        )
        excluded.update(str(r[0]) for r in cur.fetchall())

        cur.execute(
            "SELECT capsule_id FROM tombstones WHERE capsule_id IN (SELECT value FROM json_each(?))",
            (candidate_ids,),
        )
        excluded.update(str(r[0]) for r in cur.fetchall())
        return excluded
