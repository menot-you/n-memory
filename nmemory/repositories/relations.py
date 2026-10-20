"""The edges between capsules and their revocation history."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..capsule import (
    format_rfc3339,
    parse_rfc3339,
)
from ..database import writes
from ..relation import (
    RelationKind,
    RelationOrigin,
    RelationRecord,
    live_relation_sql,
)
from .base import Repository, StoreError

REVOCABLE_KINDS = frozenset({"falsifies", "supersedes"})


class RelationRepository(Repository):
    """Owns relations, relation_revocations."""

    @writes
    def assert_edge(
        self, kind: str, from_id: str, to_id: str, at: str, origin: str
    ) -> bool:
        """Insert the edge, or revive it when its last event is a revocation.

        False when the edge already stands; `add_relation` is the composite
        that validates the work shape before it.
        """
        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT at FROM relations WHERE kind = ? AND from_id = ? AND to_id = ?",
                (kind, from_id, to_id),
            )
            row = cur.fetchone()
            if row is not None:
                # U1: the row exists. If the triple's last event is a revocation,
                # reasserting REVIVES — previously it returned False and every caller (nmemory_relation_add,
                # ingest's supersedes, the import, curator) stayed silent with the edge
                # still revoked. `at` remains intact; the event records that it returned.
                if not self._relation_is_revoked(cur, kind, from_id, to_id):
                    return False
                # Inline, under this writer's own declaration: the boundary is already open.
                cur.execute(
                    "SELECT COALESCE(MAX(seq), 0) + 1 FROM relation_revocations"
                )
                next_seq = cur.fetchone()
                seq = int(next_seq[0]) if next_seq else 1
                cur.execute(
                    "INSERT INTO relation_revocations "
                    "(seq, kind, from_id, to_id, asserted_at, revoked_at, origin) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        seq,
                        kind,
                        from_id,
                        to_id,
                        str(row[0]),
                        at,
                        f"reassert:{origin}",
                    ),
                )
                return True

            cur.execute(
                "INSERT INTO relations (kind, from_id, to_id, at, origin) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT DO NOTHING",
                (kind, from_id, to_id, at, origin),
            )
            return True

    @writes
    def revoke(
        self, kind: str, from_id: str, to_id: str, revoked_at: str, origin: str
    ) -> bool:
        """Append a revocation event; False when the edge is absent or already revoked."""
        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT at FROM relations WHERE kind = ? AND from_id = ? AND to_id = ?",
                (kind, from_id, to_id),
            )
            row = cur.fetchone()
            if row is None or self._relation_is_revoked(cur, kind, from_id, to_id):
                return False
            cur.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM relation_revocations")
            next_seq = cur.fetchone()
            seq = int(next_seq[0]) if next_seq else 1
            cur.execute(
                "INSERT INTO relation_revocations "
                "(seq, kind, from_id, to_id, asserted_at, revoked_at, origin) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    seq,
                    kind,
                    from_id,
                    to_id,
                    str(row[0]),
                    revoked_at,
                    origin,
                ),
            )
        return True

    @staticmethod
    def _relation_is_revoked(cur: Any, kind: str, from_id: str, to_id: str) -> bool:
        cur.execute(
            "SELECT origin FROM relation_revocations "
            "WHERE kind = ? AND from_id = ? AND to_id = ? ORDER BY seq DESC LIMIT 1",
            (kind, from_id, to_id),
        )
        last = cur.fetchone()
        return last is not None and str(last[0]).split(":", 1)[0] == "revoke"

    @writes
    def unsupersede(self, capsule_id: str, edge_from: str, now: datetime) -> bool:
        with self.db.connection() as conn, conn.cursor() as cur:
            # U1: an edge with revocation events has history; deleting it (which
            # this DELETE does) would destroy the record. Revoke, do not delete.
            cur.execute(
                "SELECT 1 FROM relation_revocations WHERE kind = 'supersedes' "
                "AND from_id = ? AND to_id = ? LIMIT 1",
                (edge_from, capsule_id),
            )
            if cur.fetchone() is not None:
                raise StoreError(
                    f"unsupersede refused: {edge_from}->{capsule_id} carries revocation "
                    "events; revoke it instead of deleting its history"
                )
            cur.execute(
                """
                DELETE FROM relations
                WHERE kind = 'supersedes' AND from_id = ? AND to_id = ? AND origin = 'import'
                """,
                (edge_from, capsule_id),
            )
            return cur.rowcount > 0

    def effort_members(self, effort_id: str) -> list[str]:
        relations = self.list_relations()
        pending = [effort_id]
        members: set[str] = set()
        while pending:
            parent = pending.pop()
            for r in relations:
                if (
                    r.kind in (RelationKind.PART_OF, RelationKind.GROUNDED_IN)
                    and r.to_id == parent
                    and r.from_id not in members
                ):
                    members.add(r.from_id)
                    pending.append(r.from_id)
        return sorted(members)

    def list_relations(self, *, include_revoked: bool = False) -> list[RelationRecord]:
        """LIVE edges by default (U1): twelve in-memory readers inherit the rule
        from here. `include_revoked=True` is the history, for audit and tests."""
        fence = "" if include_revoked else " WHERE " + live_relation_sql()
        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT kind, from_id, to_id, NULL as reason, at, origin FROM relations r"
                + fence
                + " ORDER BY at ASC"
            )
            rows = cur.fetchall()
            return [
                RelationRecord(
                    kind=RelationKind.from_str(r[0]),
                    from_id=str(r[1]),
                    to_id=str(r[2]),
                    reason=r[3],
                    at=parse_rfc3339(r[4]),
                    origin=RelationOrigin.from_str(r[5])
                    if r[5]
                    else RelationOrigin.MANUAL,
                )
                for r in rows
            ]

    def list_relations_for(
        self, capsule_id: str, *, include_revoked: bool = False
    ) -> list[RelationRecord]:
        fence = "" if include_revoked else " AND " + live_relation_sql()
        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT kind, from_id, to_id, NULL as reason, at, origin FROM relations r "
                "WHERE (from_id = ? OR to_id = ?)" + fence + " ORDER BY at ASC",
                (capsule_id, capsule_id),
            )
            rows = cur.fetchall()
            return [
                RelationRecord(
                    kind=RelationKind.from_str(r[0]),
                    from_id=str(r[1]),
                    to_id=str(r[2]),
                    reason=r[3],
                    at=parse_rfc3339(r[4]),
                    origin=RelationOrigin.from_str(r[5])
                    if r[5]
                    else RelationOrigin.MANUAL,
                )
                for r in rows
            ]

    def is_superseded(self, capsule_id: str, as_of: datetime | None = None) -> bool:
        with self.db.connection() as conn, conn.cursor() as cur:
            if as_of is None:
                cur.execute(
                    "SELECT 1 FROM relations r INDEXED BY relations_to WHERE r.kind = 'supersedes' AND r.to_id = ? AND "
                    + live_relation_sql(),
                    (capsule_id,),
                )
            else:
                stamp = format_rfc3339(as_of)
                cur.execute(
                    "SELECT 1 FROM relations r INDEXED BY relations_to WHERE r.kind = 'supersedes' AND r.to_id = ? "
                    "AND r.at <= ? AND " + live_relation_sql(as_of=True),
                    (capsule_id, stamp, stamp, stamp),
                )
            return cur.fetchone() is not None

    def is_falsified(self, capsule_id: str, as_of: datetime | None = None) -> bool:
        with self.db.connection() as conn, conn.cursor() as cur:
            if as_of is None:
                cur.execute(
                    "SELECT 1 FROM relations r INDEXED BY relations_to WHERE r.kind = 'falsifies' AND r.to_id = ? AND "
                    + live_relation_sql(),
                    (capsule_id,),
                )
            else:
                stamp = format_rfc3339(as_of)
                cur.execute(
                    "SELECT 1 FROM relations r INDEXED BY relations_to WHERE r.kind = 'falsifies' AND r.to_id = ? "
                    "AND r.at <= ? AND " + live_relation_sql(as_of=True),
                    (capsule_id, stamp, stamp, stamp),
                )
            return cur.fetchone() is not None

    def superseded_among(self, candidate_ids: list[str]) -> set[str]:
        if not candidate_ids:
            return set()
        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT r.to_id FROM relations r WHERE r.kind = 'supersedes' "
                "AND r.to_id IN (SELECT value FROM json_each(?)) AND "
                + live_relation_sql(),
                (candidate_ids,),
            )
            return {str(r[0]) for r in cur.fetchall()}
