"""Epistemics: each capsule's status and `stale_if` condition, and its source quote."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ..capsule import (
    format_rfc3339,
)
from ..database import writes
from .base import Repository


class EpistemicsRepository(Repository):
    """Owns epistemics, source_quotes."""

    def get_epistemic(self, capsule_id: str) -> dict[str, Any] | None:
        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT evidence_state, proof_hint, stale_if FROM epistemics WHERE capsule_id = ?",
                (capsule_id,),
            )
            row = cur.fetchone()
            if row:
                return {
                    "evidence_state": row[0],
                    "proof_hint": row[1],
                    "stale_if": row[2],
                }
            return None

    @writes
    def set_epistemic(
        self,
        capsule_id: str,
        evidence_state: str | None,
        proof_hint: str | None,
        stale_if: str | None,
        now: datetime | None = None,
    ) -> None:
        at_str = format_rfc3339(now or datetime.now(UTC))
        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO epistemics (capsule_id, evidence_state, proof_hint, stale_if, at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (capsule_id) DO UPDATE SET
                    evidence_state = COALESCE(EXCLUDED.evidence_state, epistemics.evidence_state),
                    proof_hint = COALESCE(EXCLUDED.proof_hint, epistemics.proof_hint),
                    stale_if = COALESCE(EXCLUDED.stale_if, epistemics.stale_if),
                    at = EXCLUDED.at
                """,
                (capsule_id, evidence_state, proof_hint, stale_if, at_str),
            )

    def all_stale_if(self) -> list[tuple[str, str]]:
        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT capsule_id, stale_if FROM epistemics
                WHERE stale_if IS NOT NULL AND trim(stale_if) <> ''
                ORDER BY capsule_id ASC
                """
            )
            return [(str(r[0]), str(r[1])) for r in cur.fetchall()]

    @writes
    def set_source_quote(
        self, capsule_id: str, quote: str, verdict: str, now: datetime
    ) -> None:
        at_str = format_rfc3339(now)
        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO source_quotes (capsule_id, quote, verdict, at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (capsule_id) DO UPDATE SET
                    quote = EXCLUDED.quote,
                    verdict = EXCLUDED.verdict,
                    at = EXCLUDED.at
                """,
                (capsule_id, quote, verdict, at_str),
            )

    def get_source_quote(self, capsule_id: str) -> dict[str, Any] | None:
        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT quote, verdict FROM source_quotes WHERE capsule_id = ?",
                (capsule_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return {"quote": str(row[0]), "verdict": str(row[1])}
