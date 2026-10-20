"""The hash-chained audit journal."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..capsule import (
    format_rfc3339,
    parse_rfc3339,
)
from ..database import writes
from ..journal import (
    BOUNDARY_ACTION,
    BOUNDARY_ACTOR,
    BOUNDARY_REASON,
    BOUNDARY_SQL,
    BOUNDARY_SQL_PARAMS,
    BOUNDARY_SUBJECT,
    AuditEvent,
    ChainStatus,
    at_canonical,
    compute_chained_hash,
    verify_chain,
)
from .base import Repository


class AuditRepository(Repository):
    """Owns audit_events."""

    @writes
    def append_audit(
        self,
        actor: str,
        action: str,
        subject: str,
        reason: str | None,
        now: datetime,
    ) -> int:
        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1, "
                "(SELECT chained_hash FROM audit_events ORDER BY seq DESC LIMIT 1) "
                "FROM audit_events"
            )
            row = cur.fetchone()
            assert row is not None
            next_seq = int(row[0])
            prev_hash = str(row[1]) if row[1] else ""

            # The EPOCH BOUNDARY, written ONCE, before the first event
            # that this port signs in parity. Runs INSIDE the tool call's
            # transaction, so either both rows enter or neither,
            # and the generation advances only once.
            cur.execute(
                f"SELECT 1 FROM audit_events WHERE {BOUNDARY_SQL} LIMIT 1",
                BOUNDARY_SQL_PARAMS,
            )
            if cur.fetchone() is None:
                boundary = AuditEvent(
                    seq=next_seq,
                    actor=BOUNDARY_ACTOR,
                    action=BOUNDARY_ACTION,
                    subject=BOUNDARY_SUBJECT,
                    reason=BOUNDARY_REASON,
                    at=now,
                    chained_hash="",
                )
                b_hash = compute_chained_hash(prev_hash, boundary.canonical_line())
                # The persisted `at` is the SAME text that entered the hash: this
                # row is the only one whose identity a reader recognizes byte for
                # byte, so here there cannot be the divergence that the rest of
                # the ledger carries.
                cur.execute(
                    """
                    INSERT INTO audit_events
                        (seq, at, actor, action, subject, reason, chained_hash)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        next_seq,
                        at_canonical(now),
                        BOUNDARY_ACTOR,
                        BOUNDARY_ACTION,
                        BOUNDARY_SUBJECT,
                        BOUNDARY_REASON,
                        b_hash,
                    ),
                )
                next_seq += 1
                prev_hash = b_hash

            at_str = format_rfc3339(now)
            event = AuditEvent(
                seq=next_seq,
                actor=actor,
                action=action,
                subject=subject,
                reason=reason,
                at=now,
                chained_hash="",
            )
            line = event.canonical_line()
            chash = compute_chained_hash(prev_hash, line)

            cur.execute(
                """
                INSERT INTO audit_events
                    (seq, at, actor, action, subject, reason, chained_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (next_seq, at_str, actor, action, subject, reason, chash),
            )
            return next_seq

    def list_audit(
        self, limit: int | None = None, subject: str | None = None
    ) -> list[AuditEvent]:
        """LOGICAL events. The epoch boundary does not appear here.

        Same reason as `count_audit_events`: it is cutover metadata, not an
        audited event, and the oracle never writes it. What needs it is
        `verify_chain`, which reads the entire table via another path.
        """
        with self.db.connection() as conn, conn.cursor() as cur:
            query = (
                "SELECT seq, at, actor, action, subject, reason, chained_hash "
                f"FROM audit_events WHERE NOT {BOUNDARY_SQL}"
            )
            params: list[Any] = list(BOUNDARY_SQL_PARAMS)
            if subject:
                query += " AND subject = ?"
                params.append(subject)
            query += " ORDER BY seq DESC"
            if limit:
                query += f" LIMIT {limit}"
            cur.execute(query, tuple(params))
            rows = cur.fetchall()
            return [
                AuditEvent(
                    seq=int(r[0]),
                    at=parse_rfc3339(r[1]),
                    actor=str(r[2]),
                    action=str(r[3]),
                    subject=str(r[4]),
                    reason=str(r[5]) if r[5] is not None else None,
                    chained_hash=str(r[6]),
                    # The RAW column string: the Rust era signed nanoseconds
                    # that `parse_rfc3339` trims. Without it, that era is unreadable.
                    stored_at=str(r[1]),
                )
                for r in rows
            ]

    def count_audit_events(self) -> int:
        """The LOGICAL size of the ledger: the epoch boundary does not count.

        Which parity this preserves, exactly: the DIFFERENTIAL one — oracle and port
        on separate databases, same logical workload. The oracle never writes the
        boundary, so for N tools it has N rows and the port N+1;
        counting the boundary would make every judge step diverge by 1 (measured:
        `audit_events` 13 in the oracle versus 14 in the port).

        What this DOES NOT promise, and which already cost a rollback: on the SAME
        store — oracle reading what the port wrote, which is the cutover case
        — the oracle counts the physical row and answers ONE MORE than this method
        (measured: `audit_events: 3` and `journal.verified: 3` versus 2 from here).
        This is by construction, not a defect: the row exists, is in the chain, and
        enters the digest. Do not "fix" by counting it here — that breaks the
        differential, which is the parity gate.
        """
        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT count(*) FROM audit_events WHERE NOT {BOUNDARY_SQL}",
                BOUNDARY_SQL_PARAMS,
            )
            row = cur.fetchone()
            return int(row[0]) if row else 0

    def audit_watermark(self) -> int:
        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT COALESCE(MAX(seq), -1) FROM audit_events")
            row = cur.fetchone()
            return int(row[0]) if row else -1

    def verify_chain(self) -> ChainStatus:
        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT seq, at, actor, action, subject, reason, chained_hash FROM audit_events ORDER BY seq ASC"
            )
            rows = cur.fetchall()
            events = [
                AuditEvent(
                    seq=int(r[0]),
                    at=parse_rfc3339(r[1]),
                    actor=str(r[2]),
                    action=str(r[3]),
                    subject=str(r[4]),
                    reason=str(r[5]) if r[5] is not None else None,
                    chained_hash=str(r[6]),
                    # The RAW column string: the Rust era signed nanoseconds
                    # that `parse_rfc3339` trims. Without it, that era is unreadable.
                    stored_at=str(r[1]),
                )
                for r in rows
            ]
            return verify_chain(events)
