"""Recall traffic: usage counters, feedback weights, and the recalls that found nothing."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..capsule import format_rfc3339
from ..capsule_ref import fold_diacritic
from ..database import writes
from .base import Repository, StoreError


class TrafficRepository(Repository):
    """Owns recall_misses, recall_miss_context, usage, feedback_weights."""

    @writes
    def record_recall(self, capsule_id: str, now: datetime) -> None:
        at_str = format_rfc3339(now)
        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO usage (capsule_id, recall_count, last_recalled_at)
                VALUES (?, 1, ?)
                ON CONFLICT (capsule_id) DO UPDATE SET
                    recall_count = usage.recall_count + 1,
                    last_recalled_at = EXCLUDED.last_recalled_at
                """,
                (capsule_id, at_str),
            )

    def get_feedback_weight(self, capsule_id: str) -> float | None:
        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT weight FROM feedback_weights WHERE capsule_id = ?",
                (capsule_id,),
            )
            row = cur.fetchone()
            return float(row[0]) if row else None

    @writes
    def record_recall_miss(
        self,
        term: str,
        outcome: str,
        now: datetime,
        *,
        call_id: str | None = None,
        lane: str | None = None,
        project_id: str | None = None,
        project_prefix: str | None = None,
    ) -> None:
        """Records a term lane failure, and the context that makes it replayable.

        L8: a wave-2 curator does not fabricate evidence that was never captured.
        Until now, the row was `(seq, term, outcome, at)` — no fence, no lane,
        without knowing which terms belonged to the SAME call. Two of the four
        diagnoses that lock 4 wanted ("the fence was wrong", "the capsule exists
        and the index did not reach it") are invisible on that row, and the
        metric "the same query now succeeds" was satisfiable with an INSERT,
        because `nmemory_synonym_add` expands every term during retrieval.

        The context lives in `recall_miss_context`, a SISTER table rather than a
        column: see the DDL (`database.py`).
        The parameters are optional because a legacy caller continues to record the usual
        row — what it does not record is the context, and the absence speaks for itself.
        """
        at_str = format_rfc3339(now)
        term_folded = "".join(fold_diacritic(c) for c in term.lower())
        if not any(c.isalnum() for c in term_folded):
            return
        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM recall_misses")
            row = cur.fetchone()
            assert row is not None
            seq = int(row[0])
            cur.execute(
                "INSERT INTO recall_misses (seq, term, outcome, at) VALUES (?, ?, ?, ?)",
                (seq, term_folded, outcome, at_str),
            )
            if call_id is not None and lane is not None:
                cur.execute(
                    "INSERT INTO recall_miss_context"
                    " (seq, call_id, lane, project_id, project_prefix)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (seq, call_id, lane, project_id, project_prefix),
                )

    def list_recall_misses(self, limit: int = 50) -> list[dict[str, Any]]:
        """The ledger rows, with L8 context when present."""
        if limit < 0:
            raise StoreError(f"list_recall_misses: limit={limit} is negative")
        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT m.seq, m.term, m.outcome, m.at,"
                " c.call_id, c.lane, c.project_id, c.project_prefix"
                " FROM recall_misses m"
                " LEFT JOIN recall_miss_context c ON c.seq = m.seq"
                " ORDER BY m.seq DESC LIMIT ?",
                (limit,),
            )
            rows = cur.fetchall()
            return [
                {
                    "seq": int(r[0]),
                    "term": str(r[1]),
                    "outcome": str(r[2]),
                    "at": str(r[3]),
                    # LEFT JOIN: rows recorded before L8 have no context,
                    # and `None` communicates this. Fabricating a value here would be the same
                    # disease as F2 — a manufactured cause where no measurement occurred.
                    "call_id": str(r[4]) if r[4] is not None else None,
                    "lane": str(r[5]) if r[5] is not None else None,
                    "project_id": str(r[6]) if r[6] is not None else None,
                    "project_prefix": str(r[7]) if r[7] is not None else None,
                }
                for r in rows
            ]

    def recall_miss_terms(self) -> list[tuple[str, int]]:
        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT term, COUNT(*) FROM recall_misses GROUP BY term ORDER BY term ASC"
            )
            rows = cur.fetchall()
            return [(str(r[0]), int(r[1])) for r in rows]
