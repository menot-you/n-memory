"""The substrate records: outcomes and recall receipts."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..capsule import (
    format_rfc3339,
    parse_rfc3339,
)
from ..database import writes
from ..project import glob_subtree_pattern
from ..substrate import OutcomeRecord, RecallReceipt
from .base import Repository


def _outcome(r: Any) -> OutcomeRecord:
    return OutcomeRecord(
        id=str(r[0]),
        description=str(r[1]),
        actor=str(r[2]),
        evidence_ref=str(r[3]) if r[3] else None,
        capsule_id=str(r[4]) if r[4] else None,
        receipt_id=str(r[5]) if r[5] else None,
        score=float(r[6]) if r[6] is not None else None,
        at=parse_rfc3339(r[7]),
    )


class SubstrateRepository(Repository):
    """Owns outcomes, recall_receipts."""

    @writes
    def record_outcome(
        self,
        description: str,
        actor: str,
        evidence_ref: str | None,
        capsule_id: str | None,
        receipt_id: str | None,
        score: float | None,
        now: datetime,
    ) -> OutcomeRecord:
        at_str = format_rfc3339(now)
        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM outcomes")
            row = cur.fetchone()
            assert row is not None
            seq = int(row[0])
            out_id = f"out-{seq}"

            cur.execute(
                """
                INSERT INTO outcomes (seq, id, description, actor, evidence_ref, capsule_id, receipt_id, score, at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    seq,
                    out_id,
                    description,
                    actor,
                    evidence_ref,
                    capsule_id,
                    receipt_id,
                    score,
                    at_str,
                ),
            )

            return OutcomeRecord(
                id=out_id,
                description=description,
                actor=actor,
                evidence_ref=evidence_ref,
                capsule_id=capsule_id,
                receipt_id=receipt_id,
                score=score,
                at=now,
            )

    def list_outcomes(self) -> list[OutcomeRecord]:
        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, description, actor, evidence_ref, capsule_id, receipt_id, score, at "
                "FROM outcomes ORDER BY seq ASC"
            )
            return [_outcome(r) for r in cur.fetchall()]

    def recent_outcomes(
        self, limit: int, project_prefix: str | None
    ) -> list[OutcomeRecord]:
        """The newest `limit` outcomes, newest first.

        With `project_prefix`, only outcomes whose capsule is in that project or
        its subtree: an outcome bound to no capsule has no project and is left out.
        """
        select = (
            "SELECT o.id, o.description, o.actor, o.evidence_ref, o.capsule_id, "
            "o.receipt_id, o.score, o.at FROM outcomes o"
        )
        params: list[object] = []
        if project_prefix is not None:
            select += (
                " JOIN capsules c ON c.id = o.capsule_id"
                " WHERE (c.project_id = ? OR c.project_id GLOB ?)"
            )
            params += [project_prefix, glob_subtree_pattern(project_prefix)]
        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute(f"{select} ORDER BY o.seq DESC LIMIT ?", (*params, limit))
            return [_outcome(r) for r in cur.fetchall()]

    @writes
    def record_receipt(
        self,
        terms: str,
        returned_ids: list[str],
        project_id: str | None,
        now: datetime,
        *,
        session_id: str | None = None,
        project_prefix: str | None = None,
    ) -> RecallReceipt:
        """Record the receipt, with the two columns the INSERT used to omit.

        `session_id` and `project_prefix` have existed since the table did while
        this INSERT named neither: 1545 of 1549 live rows carry a NULL session. A
        column nobody writes is indistinguishable from a column nobody needs, and
        that is how it survived.

        This stays `@writes` and stays canonical — `recall_receipts` is a native
        table and `nmemory_capsule_retrieve` is an opt-in tool that already paid the write
        boundary. Counting the ORIENTATION surfaces does NOT belong here; it goes
        to `record_read_traffic`, outside the boundary, for the reason measured
        in that method's docstring.
        """
        at_str = format_rfc3339(now)
        ids_str = ",".join(returned_ids)

        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM recall_receipts")
            row = cur.fetchone()
            assert row is not None
            seq = int(row[0])
            rcpt_id = f"rcpt-{seq}"

            cur.execute(
                """
                INSERT INTO recall_receipts
                    (seq, id, terms, returned_ids, project_id, project_prefix,
                     session_id, at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    seq,
                    rcpt_id,
                    terms,
                    ids_str,
                    project_id,
                    project_prefix,
                    session_id or "unknown",
                    at_str,
                ),
            )

            return RecallReceipt(
                id=rcpt_id,
                query=terms,
                returned_ids=returned_ids,
                project_id=project_id,
                at=now,
            )
