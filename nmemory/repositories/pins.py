"""Pins: the event-sourced flag that keeps a capsule in view."""

from __future__ import annotations

from datetime import datetime

from ..capsule import format_rfc3339
from ..database import writes
from .base import Repository


class PinRepository(Repository):
    """Owns pin_events."""

    @writes
    def set_pin(
        self, capsule_id: str, pinned: bool, reason: str, actor: str, now: datetime
    ) -> None:
        at_str = format_rfc3339(now)
        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM pin_events")
            row = cur.fetchone()
            assert row is not None
            seq = int(row[0])
            cur.execute(
                "INSERT INTO pin_events (seq, capsule_id, pinned, reason, actor, at) VALUES (?, ?, ?, ?, ?, ?)",
                (seq, capsule_id, 1 if pinned else 0, reason, actor, at_str),
            )

    def pinned_among(self, candidate_ids: list[str]) -> set[str]:
        """Which of these capsules are pinned RIGHT NOW, in a single query.

        Batched for the same reason as `last_passing_verification_among`: the
        `context` ranker runs over the full candidate pool (up to 500 candidates), not over
        the <=50 served rows. Per-candidate `get_pin` was 500 roundtrips to the store on
        the path serving 1126 of the 2613 queries.

        The current state is the LATEST event by `seq` -- here `seq` is the correct
        ordering, unlike the verification clock: a pin has no caller-declared
        timestamp that could disagree with append order.
        """
        if not candidate_ids:
            return set()
        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT capsule_id FROM (
                    SELECT capsule_id, pinned
                    FROM pin_events
                    WHERE seq IN (
                        SELECT max(seq) FROM pin_events WHERE capsule_id IN (SELECT value FROM json_each(?))
                        GROUP BY capsule_id)
                ) latest
                WHERE pinned <> 0
                """,
                (candidate_ids,),
            )
            return {str(r[0]) for r in cur.fetchall()}

    def get_pin(self, capsule_id: str, as_of: datetime | None = None) -> bool:
        with self.db.connection() as conn, conn.cursor() as cur:
            if as_of is None:
                cur.execute(
                    "SELECT pinned FROM pin_events WHERE capsule_id = ? ORDER BY seq DESC LIMIT 1",
                    (capsule_id,),
                )
            else:
                cur.execute(
                    "SELECT pinned FROM pin_events WHERE capsule_id = ? AND at <= ? ORDER BY seq DESC LIMIT 1",
                    (capsule_id, format_rfc3339(as_of)),
                )
            row = cur.fetchone()
            return bool(row[0]) if row else False

    def last_change(self) -> str | None:
        """The RFC3339 time of the newest pin event, or None when there is none."""
        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT max(at) FROM pin_events")
            row = cur.fetchone()
            return None if row is None or row[0] is None else str(row[0])
