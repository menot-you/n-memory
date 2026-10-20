"""Anchor bookkeeping: the anchored file's hash, the re-anchor overrides and the verification events."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ..capsule import (
    format_rfc3339,
    parse_rfc3339,
)
from ..database import writes
from .base import Repository

#: The verification results that reset the decay clock: the statement still holds.
VERIFICATION_PASSING = frozenset({"pass", "holds", "reanchored"})


@dataclass(frozen=True)
class VerificationRecord:
    """A row of `verification_events` (U2)."""

    seq: int
    capsule_id: str
    at: datetime
    kind: str
    result: str
    detail: str
    actor: str


class AnchorRepository(Repository):
    """Owns anchor_hashes, anchor_rebind, verification_events."""

    def get_anchor_hash(self, capsule_id: str) -> str | None:
        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT hash FROM anchor_hashes WHERE capsule_id = ?",
                (capsule_id,),
            )
            row = cur.fetchone()
            return str(row[0]) if row else None

    @writes
    def set_anchor_hash(self, capsule_id: str, hash_val: str, now: datetime) -> None:
        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO anchor_hashes (capsule_id, hash, at)
                VALUES (?, ?, ?)
                ON CONFLICT (capsule_id) DO UPDATE SET
                    hash = EXCLUDED.hash,
                    at = EXCLUDED.at
                """,
                (capsule_id, hash_val, format_rfc3339(now)),
            )

    @writes
    def reanchor(
        self, capsule_id: str, anchor: str, reason: str, now: datetime
    ) -> None:
        at_str = format_rfc3339(now)
        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO anchor_rebind (capsule_id, anchor, reason, at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (capsule_id) DO UPDATE SET
                    anchor = EXCLUDED.anchor,
                    reason = EXCLUDED.reason,
                    at = EXCLUDED.at
                """,
                (capsule_id, anchor, reason, at_str),
            )

    def reanchors_among(self, ids: list[str]) -> dict[str, str]:
        """`get_reanchor` for a batch: anchor draining requested one connection per
        live capsule in scope (~12 k in the whole store) before evaluating anything."""
        if not ids:
            return {}
        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT capsule_id, anchor FROM anchor_rebind WHERE capsule_id IN (SELECT value FROM json_each(?))",
                (ids,),
            )
            return {str(r[0]): str(r[1]) for r in cur.fetchall()}

    def get_reanchor(self, capsule_id: str) -> str | None:
        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT anchor FROM anchor_rebind WHERE capsule_id = ?",
                (capsule_id,),
            )
            row = cur.fetchone()
            return str(row[0]) if row else None

    def record_verification(
        self,
        capsule_id: str,
        *,
        kind: str,
        result: str,
        detail: str,
        actor: str,
        now: datetime,
    ) -> None:
        """U2: a verification event per judgment, append-only.

        `verification_events` is bookkeeping (precedent `read_traffic`): a
        write that does not advance the canonical generation (`db.writing()`,
        not `@writes`). Standing (U3) derives from here on read.
        """
        with self.db.writing() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO verification_events "
                "(capsule_id, at, kind, result, detail, actor) VALUES (?, ?, ?, ?, ?, ?)",
                (capsule_id, format_rfc3339(now), kind, result, detail, actor),
            )

    def list_verifications(self, capsule_id: str) -> list[VerificationRecord]:
        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT seq, capsule_id, at, kind, result, detail, actor "
                "FROM verification_events WHERE capsule_id = ? ORDER BY seq ASC",
                (capsule_id,),
            )
            rows = cur.fetchall()
        return [
            VerificationRecord(
                seq=int(r[0]),
                capsule_id=str(r[1]),
                at=parse_rfc3339(r[2]),
                kind=str(r[3]),
                result=str(r[4]),
                detail=str(r[5]),
                actor=str(r[6]),
            )
            for r in rows
        ]

    def last_passing_verification_among(
        self, candidate_ids: list[str]
    ) -> dict[str, datetime]:
        """The decay clock for the two read surfaces: timestamp of the
        LATEST verification that PASSED, per capsule, in a single query.

        Batched rather than per-capsule because the `context` ranker runs over the
        entire pool (up to 500 candidates, `context.py:1083`), not over the <=50
        served rows — this is where the cost materialized that the docstring of
        `standing_of` expected.

        Only `VERIFICATION_PASSING` resets the clock: a `dead_anchor` is news
        about the anchor, not proof that the statement remains true, and
        letting it reset rewarded precisely the broken capsule.
        """
        if not candidate_ids:
            return {}
        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT capsule_id, at FROM (
                    SELECT capsule_id, at, row_number() OVER (
                        PARTITION BY capsule_id
                        -- By `at` and not by `seq`: `seq` is APPEND order and
                        -- `at` is the caller-declared timestamp. A replay or
                        -- backfill that records an OLD verification after a
                        -- recent one has a larger `seq` and smaller `at`, which
                        -- ran the clock BACKWARDS (independent review finding,
                        -- `fugu-ultra`, 2026-09-17). `seq` is a stable tiebreaker.
                        ORDER BY ts(at) DESC, seq DESC
                    ) AS rn
                    FROM verification_events
                    WHERE capsule_id IN (SELECT value FROM json_each(?))
                      AND result IN (SELECT value FROM json_each(?))
                ) WHERE rn = 1
                """,
                (candidate_ids, list(VERIFICATION_PASSING)),
            )
            return {str(r[0]): parse_rfc3339(str(r[1])) for r in cur.fetchall()}

    def last_anchor_digests_among(self, candidate_ids: list[str]) -> dict[str, str]:
        """What each anchored line SAID in the preceding scan, in a single query.

        The digest travels in event `detail` (`path:line#hash`), and the most
        recent event is already how the curator queue and `standing_of` read the
        past — a new column would be a second past diverging from the
        first. An event without `#` (scans prior to this detector, and every
        anchor without a line) simply has no digest: absent, not empty.
        """
        if not candidate_ids:
            return {}
        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT capsule_id, detail
                FROM verification_events
                WHERE seq IN (
                    SELECT max(seq) FROM verification_events WHERE capsule_id IN (SELECT value FROM json_each(?)) AND kind = 'anchor'
                    GROUP BY capsule_id)
                """,
                (candidate_ids,),
            )
            out: dict[str, str] = {}
            for row in cur.fetchall():
                head, sep, digest = str(row[1]).rpartition("#")
                if sep and head and digest:
                    out[str(row[0])] = digest
            return out
