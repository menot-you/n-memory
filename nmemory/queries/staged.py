"""The staged queue: proposals awaiting judgment, derived on read from `review_events`."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from ..capsule import Capsule, format_rfc3339
from ..database import Database, from_us
from ..project import glob_subtree_pattern
from ..relation import live_relation_sql
from ..repositories.base import StoreError

#: The reason recorded on the review event distinguishes two things that the
#: `proposed` verdict does not distinguish — and the live store demonstrated
#: why. Measured on 2026-09-17: 110 capsules in `proposed`, NONE ever ratified or
#: rejected in seven weeks, of which 90 are curator evidence (fenced by design,
#: never to be ratified) and 20 are proposals awaiting judgement. Treating all
#: 110 as a queue put the curator's proof trail in the crosshairs of expiration.
#: Both names previously lived as string literals in `curator.py` and `server.py`;
#: they live here because the store decides what `review_events` means.
REVIEW_REASON_EVIDENCE: Final = "curator evidence"


REVIEW_REASON_PROPOSAL: Final = "staged ingest"


#: The verdict and reason for expiration. Named here rather than literals because
#: the read fence is PERMISSION-based: any verdict other than `ratified` remains
#: fenced, and that is what this relies on to avoid promoting anything.
REVIEW_VERDICT_EXPIRED: Final = "expired"


REVIEW_REASON_HORIZON: Final = "staged horizon"


#: U6(b): the reason a curator evidence capsule is rejected. The verdict is
#: `rejected` rather than `expired` because this is NOT expiration — nothing aged
#: out. The proposal existed to support a `falsifies` edge, and that edge was
#: revoked: what is rejected is a claim that no longer has foundation, and the
#: reason names the revocation rather than an age threshold.
REVIEW_VERDICT_REJECTED: Final = "rejected"


REVIEW_REASON_REVOKED: Final = "falsifies revoked"


#: Days without judgement after which a staged proposal counts as EXPIRED.
#: Not an owner threshold: tied to the outcome in one sentence — the staged queue
#: cannot grow without bound and nothing can be promoted by omission, and 14 days
#: are two weekly cycles of attention, so a proposal that survived two cycles is
#: proof nobody wants it. Changed here, in one place.
STAGED_HORIZON_DAYS: Final = 14


@dataclass(frozen=True)
class StagedCensus:
    """The state of a scope's staged queue, derived on read."""

    awaiting: int
    expired: int
    evidence: int
    oldest_days: float | None

    def to_dict(self) -> dict[str, object]:
        return {
            "awaiting": self.awaiting,
            "expired": self.expired,
            "evidence": self.evidence,
            "oldest_days": (
                None if self.oldest_days is None else round(self.oldest_days, 1)
            ),
            "horizon_days": STAGED_HORIZON_DAYS,
        }


def staged_census(
    db: Database, scope: str | None, *, subtree: bool, now: datetime
) -> StagedCensus:
    """How many staged proposals await judgment, and how many have already expired.

    Derived on READ, never stored — the precedent is `standing` and
    `dod_contradictions`: the state is already in `review_events` and a second
    copy only adds modes of divergence. The verdict that counts is the LATEST
    by `seq`, so a proposal ratified afterward stops counting on its own:
    the census measures STATE, not writes.

    `evidence` counts separately and never enters `awaiting`/`expired`. It is not
    a preference: it is what prevents an expiration from erasing the curator's
    proof trail, which is born `proposed` on purpose so as NOT to be
    served as truth and was never meant to be ratified.

    `scope=None` is the ENTIRE store, and not the same as an empty scope: `digest`
    without `--project-prefix` means "all projects", and passing `""`
    in that position measured the literal scope `""`, which reaches no
    project at all — the row disappeared precisely in the default invocation.
    """
    pattern = glob_subtree_pattern(scope) if subtree and scope is not None else None
    horizon = format_rfc3339(now - timedelta(days=STAGED_HORIZON_DAYS))
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            WITH latest AS (
                SELECT capsule_id, verdict, reason, at FROM review_events
                WHERE seq IN (SELECT max(seq) FROM review_events GROUP BY capsule_id)
            )
            SELECT
                count(*) FILTER (WHERE l.reason <> :evidence) AS awaiting,
                count(*) FILTER (
                    WHERE l.reason <> :evidence
                      AND ts(l.at) <= ts(:horizon)
                ) AS expired,
                count(*) FILTER (WHERE l.reason = :evidence) AS evidence,
                min(ts(l.at)) FILTER (
                    WHERE l.reason <> :evidence
                ) AS oldest
            FROM latest l
            JOIN capsules c ON c.id = l.capsule_id
            WHERE l.verdict = 'proposed'
              -- A forgotten capsule is not queue: `forget_capsule` clears
              -- `canonical_json` and KEEPS the row, and deletes nothing from
              -- `review_events`, so its proposal remained counted forever
              -- in a number that promises a CEILING -- and undrainable, because
              -- `store.list()` filters tombstones and reading raises
              -- `Tombstoned`. The fence is the TABLE and not just the column, for
              -- the reason that :838-849 already notes: the column only describes
              -- what `forget_capsule` does today.
              AND c.canonical_json IS NOT NULL
              AND c.id NOT IN (SELECT capsule_id FROM tombstones)
              AND (
                  :scope IS NULL
                  OR c.project_id = :scope
                  OR (
                      :pattern IS NOT NULL
                      AND c.project_id GLOB :pattern
                  )
              )
            """,
            {
                "evidence": REVIEW_REASON_EVIDENCE,
                "horizon": horizon,
                "pattern": pattern,
                "scope": scope,
            },
        )
        row = cur.fetchone()
    awaiting, expired, evidence, oldest = (
        (int(row[0]), int(row[1]), int(row[2]), row[3]) if row else (0, 0, 0, None)
    )
    oldest_days = None
    if oldest is not None:
        oldest_days = (now - from_us(int(oldest))).total_seconds() / 86400.0
    return StagedCensus(
        awaiting=awaiting,
        expired=expired,
        evidence=evidence,
        oldest_days=oldest_days,
    )


def staged_proposals(
    db: Database, scope: str | None, *, subtree: bool, limit: int | None
) -> list[tuple[str, Capsule]]:
    """Staged proposals awaiting judgment, from oldest to newest.

    The SAME fence as `staged_census` — latest verdict `proposed`, reason
    different from curator evidence, live capsule — because a batch that
    counted things the census does not count would triage evidence as queue.
    `limit=None` walks the entire queue for the resident judge.
    """
    if limit is not None and limit <= 0:
        raise StoreError("staged_proposals: limit must be positive")
    pattern = glob_subtree_pattern(scope) if subtree and scope is not None else None
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            WITH latest AS (
                SELECT capsule_id, verdict, reason, at FROM review_events
                WHERE seq IN (SELECT max(seq) FROM review_events GROUP BY capsule_id)
            )
            SELECT c.id, c.canonical_json
            FROM latest l
            JOIN capsules c ON c.id = l.capsule_id
            WHERE l.verdict = 'proposed'
              AND l.reason <> :evidence
              AND c.canonical_json IS NOT NULL
              AND c.id NOT IN (SELECT capsule_id FROM tombstones)
              AND (
                  :scope IS NULL
                  OR c.project_id = :scope
                  OR (
                      :pattern IS NOT NULL
                      AND c.project_id GLOB :pattern
                  )
              )
            ORDER BY l.at ASC, c.seq ASC
            LIMIT coalesce(:limit, -1)
            """,
            {
                "evidence": REVIEW_REASON_EVIDENCE,
                "pattern": pattern,
                "scope": scope,
                "limit": limit,
            },
        )
        rows = cur.fetchall()
    return [(str(r[0]), Capsule.from_canonical_json(str(r[1]))) for r in rows]


def staged_triage(
    db: Database, scope: str | None, *, subtree: bool, limit: int
) -> list[tuple[str, str, float, float | None]]:
    """`(capsule_id, choice, p, confidence)` of the latest `staged` observation
    for each proposal still awaiting — "nao_decide" first, then from most
    confident to least. This is what digest shows; none of this signs.
    """
    pattern = glob_subtree_pattern(scope) if subtree and scope is not None else None
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            WITH latest AS (
                SELECT capsule_id, verdict, reason
                FROM review_events
                WHERE seq IN (SELECT max(seq) FROM review_events GROUP BY capsule_id)
            ), obs AS (
                SELECT capsule_id, choice, p, confidence
                FROM jev_observations
                WHERE seq IN (
                    SELECT max(seq) FROM jev_observations WHERE question = 'staged'
                    GROUP BY capsule_id)
            )
            SELECT o.capsule_id, o.choice, o.p, o.confidence
            FROM obs o
            JOIN latest l ON l.capsule_id = o.capsule_id
            JOIN capsules c ON c.id = o.capsule_id
            WHERE l.verdict = 'proposed'
              AND l.reason <> :evidence
              AND c.canonical_json IS NOT NULL
              AND c.id NOT IN (SELECT capsule_id FROM tombstones)
              AND (
                  :scope IS NULL
                  OR c.project_id = :scope
                  OR (
                      :pattern IS NOT NULL
                      AND c.project_id GLOB :pattern
                  )
              )
            ORDER BY (o.choice = 'nao_decide') DESC,
                     o.confidence DESC NULLS LAST, o.p DESC, o.capsule_id ASC
            LIMIT coalesce(:limit, -1)
            """,
            {
                "evidence": REVIEW_REASON_EVIDENCE,
                "pattern": pattern,
                "scope": scope,
                "limit": limit,
            },
        )
        return [
            (
                str(r[0]),
                str(r[1]),
                float(r[2]),
                None if r[3] is None else float(r[3]),
            )
            for r in cur.fetchall()
        ]


def staged_feedback(
    db: Database, since: datetime, until: datetime
) -> list[dict[str, Any]]:
    """Each `staged` observation in `[since, until)` with a LATER human verdict.

    The label is the latest `ratified`/`rejected` review event after the
    observation whose actor is not an automatic writer. A forgotten capsule
    (`canonical_json` NULL) is excluded: a purge also removes it from training.
    """
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT o.capsule_id, c.canonical_json, o.choice, o.p, o.at,
                   r.verdict, r.at
            FROM jev_observations o
            JOIN capsules c ON c.id = o.capsule_id
            JOIN review_events r ON r.seq = (
                SELECT max(h.seq) FROM review_events h
                WHERE h.capsule_id = o.capsule_id
                  AND h.verdict IN ('ratified', 'rejected')
                  AND h.actor NOT IN ('jev', 'system1', 'curator')
                  AND julianday(h.at) > julianday(o.at)
            )
            WHERE o.question = 'staged'
              AND c.canonical_json IS NOT NULL
              AND julianday(o.at) >= julianday(:since)
              AND julianday(o.at) < julianday(:until)
            ORDER BY o.seq
            """,
            {"since": format_rfc3339(since), "until": format_rfc3339(until)},
        )
        return [
            {
                "capsule_id": str(r[0]),
                "text": Capsule.from_canonical_json(r[1]).content,
                "choice": str(r[2]),
                "p": float(r[3]),
                "verdict": str(r[5]),
                "observed_at": str(r[4]),
                "reviewed_at": str(r[6]),
            }
            for r in cur.fetchall()
        ]


def standing_proposal(
    db: Database, capsule_id: str, *, allow_automatic_correction: bool = False
) -> str | None:
    """`None` when `capsule_id` is a LIVE proposal that `nmemory_proposal_review` can
    transition; otherwise the named reason: `tombstoned`, `forgotten`, `curator
    evidence` (curator evidence is born `proposed` and is never ratified — see
    `staged_census`), or the current state (`ratified`, `rejected`, `none`).
    """
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.canonical_json IS NULL,
                   EXISTS (SELECT 1 FROM tombstones t WHERE t.capsule_id = c.id),
                   l.verdict, l.reason, l.actor
            FROM capsules c
            LEFT JOIN review_events l ON l.seq = (
                SELECT max(r.seq) FROM review_events r WHERE r.capsule_id = c.id
            )
            WHERE c.id = ?
            """,
            (capsule_id,),
        )
        row = cur.fetchone()
    if row is None:
        return "unknown"
    forgotten, tombstoned, verdict, reason, actor = row
    if tombstoned:
        return "tombstoned"
    if forgotten:
        return "forgotten"
    if (
        allow_automatic_correction
        and verdict == REVIEW_VERDICT_EXPIRED
        and reason == REVIEW_REASON_HORIZON
    ):
        return None
    if (
        allow_automatic_correction
        and verdict in ("ratified", "rejected")
        and actor in ("jev", "system1", "curator")
        and reason != REVIEW_REASON_EVIDENCE
    ):
        return None
    if verdict != "proposed":
        #: U6(b): MECHANICAL rejection is the only one the owner overrides through the door.
        #:
        #: Without this line, the reversibility promise of this unit was FALSE,
        #: and independent review caught it: once written as `rejected`,
        #: `standing_proposal` returned `"rejected"` and `nmemory_proposal_review`
        #: (`nmemory.tools.review`) rejected it — there is no CLI command that writes
        #: `ratified` (`cli.py:634` routes to the same door), so the rejection
        #: could only be undone manually against the database. An automated judge whose verdict
        #: the owner cannot override is not a judge, but a sentence.
        #:
        #: The opening is intentionally NARROW: only the verdict that THIS judge
        #: writes (`rejected` + `falsifies revoked`). Curator evidence awaiting
        #: judgment remains fenced by the `REVIEW_REASON_EVIDENCE` branch below,
        #: which is the intended design, and no other rejection becomes reversible
        #: by accident.
        if verdict == REVIEW_VERDICT_REJECTED and reason == REVIEW_REASON_REVOKED:
            return None
        return str(verdict) if verdict else "none"
    if reason == REVIEW_REASON_EVIDENCE:
        return REVIEW_REASON_EVIDENCE
    return None


def expirable_proposals(
    db: Database, scope: str | None, *, subtree: bool, now: datetime
) -> list[str]:
    """The staged proposals past `STAGED_HORIZON_DAYS` that no one judged.

    The read half of `Store.expire_staged_proposals`, which calls it after
    declaring the write, so the choice and the expiry share one transaction.
    Never curator evidence (`REVIEW_REASON_EVIDENCE`), never a tombstone.
    """
    pattern = glob_subtree_pattern(scope) if subtree and scope is not None else None
    horizon = format_rfc3339(now - timedelta(days=STAGED_HORIZON_DAYS))
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            WITH latest AS (
                SELECT capsule_id, verdict, reason, at
                FROM review_events
                WHERE seq IN (SELECT max(seq) FROM review_events GROUP BY capsule_id)
            )
            SELECT l.capsule_id
            FROM latest l
            JOIN capsules c ON c.id = l.capsule_id
            WHERE l.verdict = 'proposed'
              AND l.reason <> :evidence
              AND ts(l.at) <= ts(:horizon)
              AND c.canonical_json IS NOT NULL
              AND c.id NOT IN (SELECT capsule_id FROM tombstones)
              AND (
                  :scope IS NULL
                  OR c.project_id = :scope
                  OR (
                      :pattern IS NOT NULL
                      AND c.project_id GLOB :pattern
                  )
              )
            ORDER BY l.capsule_id ASC
            """,
            {
                "evidence": REVIEW_REASON_EVIDENCE,
                "horizon": horizon,
                "pattern": pattern,
                "scope": scope,
            },
        )
        return [str(r[0]) for r in cur.fetchall()]


def unsupported_evidence(
    db: Database, scope: str | None, *, subtree: bool
) -> list[str]:
    """Curator evidence whose every `falsifies` edge was revoked.

    The read half of `Store.reject_unsupported_evidence`, which calls it after
    declaring the write. Evidence that never had an edge is not returned: it had
    nothing revoked.
    """
    pattern = glob_subtree_pattern(scope) if subtree and scope is not None else None
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            WITH latest AS (
                SELECT capsule_id, verdict, reason
                FROM review_events
                WHERE seq IN (SELECT max(seq) FROM review_events GROUP BY capsule_id)
            )
            SELECT l.capsule_id
            FROM latest l
            JOIN capsules c ON c.id = l.capsule_id
            WHERE l.verdict = 'proposed'
              AND l.reason = :evidence
              AND c.canonical_json IS NOT NULL
              AND c.id NOT IN (SELECT capsule_id FROM tombstones)
              AND (
                  :scope IS NULL
                  OR c.project_id = :scope
                  OR (
                      :pattern IS NOT NULL
                      AND c.project_id GLOB :pattern
                  )
              )
              AND EXISTS (
                  SELECT 1 FROM relations r
                  WHERE r.from_id = c.id AND r.kind = 'falsifies'
              )
              AND NOT EXISTS (
                  SELECT 1 FROM relations r
                  WHERE r.from_id = c.id AND r.kind = 'falsifies'
                    AND {live_relation_sql("r")}
              )
            ORDER BY l.capsule_id ASC
            """,
            {
                "evidence": REVIEW_REASON_EVIDENCE,
                "pattern": pattern,
                "scope": scope,
            },
        )
        return [str(r[0]) for r in cur.fetchall()]
