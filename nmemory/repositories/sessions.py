"""The session brackets `nmemory_session_start` and `nmemory_session_finish` record."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..capsule import (
    format_rfc3339,
    parse_rfc3339,
)
from ..database import writes
from .base import Repository, StoreError


class UnknownSession(StoreError):
    def __init__(self, session_id: str) -> None:
        super().__init__(f"unknown session {session_id}")
        self.session_id = session_id


class SessionFinished(StoreError):
    def __init__(self, session_id: str) -> None:
        super().__init__(f"session {session_id} is already finished")
        self.session_id = session_id


@dataclass(frozen=True)
class SessionRecord:
    session_id: str
    started_at: datetime
    finished_at: datetime | None
    summary: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "started_at": format_rfc3339(self.started_at),
            "finished_at": format_rfc3339(self.finished_at)
            if self.finished_at
            else None,
            "summary": self.summary,
        }


class SessionRepository(Repository):
    """Owns sessions."""

    @writes
    def start_session(self, now: datetime) -> str:
        at_str = format_rfc3339(now)
        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT COALESCE(COUNT(*), 0) + 1 FROM sessions")
            row = cur.fetchone()
            assert row is not None
            next_id = int(row[0])
            sid = f"sess-{next_id}"
            cur.execute(
                "INSERT INTO sessions (session_id, started_at) VALUES (?, ?)",
                (sid, at_str),
            )
            return sid

    @writes
    def finish_session(
        self, session_id: str, summary: str | None, now: datetime
    ) -> None:
        at_str = format_rfc3339(now)
        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT finished_at FROM sessions WHERE session_id = ?",
                (session_id,),
            )
            row = cur.fetchone()
            if not row:
                raise UnknownSession(session_id)
            if row[0] is not None:
                raise SessionFinished(session_id)
            cur.execute(
                "UPDATE sessions SET finished_at = ?, summary = ? WHERE session_id = ?",
                (at_str, summary, session_id),
            )

    def get_session(self, session_id: str) -> SessionRecord | None:
        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT session_id, started_at, finished_at, summary FROM sessions WHERE session_id = ?",
                (session_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return SessionRecord(
                session_id=str(row[0]),
                started_at=parse_rfc3339(row[1]),
                finished_at=parse_rfc3339(row[2]) if row[2] else None,
                summary=str(row[3]) if row[3] else None,
            )

    def list_sessions(self) -> list[SessionRecord]:
        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT session_id, started_at, finished_at, summary FROM sessions ORDER BY started_at ASC"
            )
            rows = cur.fetchall()
            return [
                SessionRecord(
                    session_id=str(r[0]),
                    started_at=parse_rfc3339(r[1]),
                    finished_at=parse_rfc3339(r[2]) if r[2] else None,
                    summary=str(r[3]) if r[3] else None,
                )
                for r in rows
            ]
