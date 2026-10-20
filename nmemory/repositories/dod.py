"""The DoD event log: `dod_events`, and the state each capsule's events fold to."""

from __future__ import annotations

from ..capsule import parse_rfc3339
from ..dod import DodEvent, DodState
from .base import Repository


class DodRepository(Repository):
    """Owns dod_events."""

    def dod_state_of(self, capsule_id: str) -> DodState:
        raw_events = self.list_dod_events(capsule_id)
        events: list[DodEvent] = []
        status = "proposed"
        verdict = "unmet"
        for row in raw_events:
            _seq, _cid, axis, val, actor, reason, at_str = row
            at_dt = parse_rfc3339(at_str)
            event = DodEvent(axis=axis, value=val, actor=actor, reason=reason, at=at_dt)
            events.append(event)
            if axis == "status":
                status = val
            elif axis == "verdict":
                verdict = val
        return DodState(status=status, verdict=verdict, history=events)

    def append_dod_event(
        self, capsule_id: str, axis: str, value: str, actor: str, reason: str, at: str
    ) -> int:
        with self.db.writing() as handle, handle.cursor() as cur:
            row = cur.execute(
                "INSERT INTO dod_events (capsule_id, axis, value, actor, reason, at)"
                " VALUES (?, ?, ?, ?, ?, ?) RETURNING seq",
                (capsule_id, axis, value, actor, reason, at),
            ).fetchone()
        return int(row[0])

    def list_dod_events(
        self, capsule_id: str
    ) -> list[tuple[int, str, str, str, str, str, str]]:
        with self.db.connection() as handle, handle.cursor() as cur:
            rows = cur.execute(
                "SELECT seq, capsule_id, axis, value, actor, reason, at FROM dod_events"
                " WHERE capsule_id = ? ORDER BY seq ASC",
                (capsule_id,),
            ).fetchall()
        return [
            (
                int(r[0]),
                str(r[1]),
                str(r[2]),
                str(r[3]),
                str(r[4]),
                str(r[5]),
                str(r[6]),
            )
            for r in rows
        ]
