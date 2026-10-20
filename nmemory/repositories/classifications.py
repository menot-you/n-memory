"""Classifications: each capsule's kind and scope, and the work-edge shapes kinds allow."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..capsule import (
    format_rfc3339,
    parse_rfc3339,
)
from ..classify import CandidateKind, ClassificationScope
from ..database import writes
from ..relation import RelationKind, live_relation_sql
from .base import Repository, StoreError


@dataclass(frozen=True)
class ClassificationRecord:
    capsule_id: str
    kind: CandidateKind
    scope: ClassificationScope
    at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "capsule_id": self.capsule_id,
            "kind": self.kind.value,
            "scope": self.scope.value,
            "at": format_rfc3339(self.at),
        }


class ClassificationRepository(Repository):
    """Owns classifications."""

    def get_classification(self, capsule_id: str) -> ClassificationRecord | None:
        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT capsule_id, kind, scope, at FROM classifications WHERE capsule_id = ?",
                (capsule_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return ClassificationRecord(
                capsule_id=str(row[0]),
                kind=CandidateKind.from_str(row[1]),
                scope=ClassificationScope.from_str(row[2]),
                at=parse_rfc3339(row[3]),
            )

    def kind_ids_among(
        self, kinds: Sequence[str], candidate_ids: Sequence[str]
    ) -> set[str]:
        """One bounded classification lookup over the caller's existing candidates."""
        if not kinds or not candidate_ids:
            return set()
        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT capsule_id FROM classifications "
                "WHERE kind IN (SELECT value FROM json_each(?)) AND capsule_id IN (SELECT value FROM json_each(?))",
                (list(kinds), list(candidate_ids)),
            )
            return {str(row[0]) for row in cur.fetchall()}

    def kinds_of(self, capsule_ids: Sequence[str]) -> dict[str, str]:
        """The persisted kind of each classified capsule, read in one query."""
        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT capsule_id, kind FROM classifications "
                "WHERE capsule_id IN (SELECT value FROM json_each(?))",
                (list(capsule_ids),),
            )
            return {str(row[0]): str(row[1]) for row in cur.fetchall()}

    def list_ids_of_kind(self, kind: str) -> list[str]:
        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT capsule_id FROM classifications WHERE kind = ? ORDER BY capsule_id ASC",
                (kind,),
            )
            return [str(r[0]) for r in cur.fetchall()]

    def validate_work_relation(
        self,
        kind: RelationKind | str,
        from_id: str,
        to_id: str,
    ) -> None:
        k = RelationKind.from_str(
            kind.value if isinstance(kind, RelationKind) else kind
        )
        if k not in (RelationKind.BLOCKS, RelationKind.PART_OF, RelationKind.DOD_OF):
            return
        from_cls = self.get_classification(from_id)
        to_cls = self.get_classification(to_id)
        from_kind = from_cls.kind.value if from_cls else None
        to_kind = to_cls.kind.value if to_cls else None

        valid = False
        if k == RelationKind.BLOCKS:
            valid = from_kind == "task" and to_kind == "task"
        elif k == RelationKind.PART_OF:
            valid = (from_kind == "task" and to_kind in ("task", "plan", "epic")) or (
                from_kind == "plan" and to_kind == "epic"
            )
        elif k == RelationKind.DOD_OF:
            valid = from_kind == "dod" and to_kind in (
                "task",
                "epic",
                "plan",
                "brainstorm",
            )

        if not valid:
            from_desc = f'Some("{from_kind}")' if from_kind else "None"
            to_desc = f'Some("{to_kind}")' if to_kind else "None"
            if k == RelationKind.DOD_OF:
                raise StoreError(
                    f"dod_of binds a Definition of Done to the work it governs; valid shape: dod -> task|epic|plan|brainstorm; got {from_id} ({from_desc}) -> {to_id} ({to_desc}) — classify both endpoints with nmemory_kind_classify before recording the edge"
                )
            elif k == RelationKind.BLOCKS:
                raise StoreError(
                    f"blocks relation requires task -> task; got {from_id} ({from_desc}) -> {to_id} ({to_desc})"
                )
            elif k == RelationKind.PART_OF:
                raise StoreError(
                    f"part_of relation requires task -> task|plan|epic or plan -> epic; got {from_id} ({from_desc}) -> {to_id} ({to_desc})"
                )
            raise StoreError(
                f"invalid work relation {k.value}: from {from_id} ({from_kind}) to {to_id} ({to_kind})"
            )

        if k is RelationKind.PART_OF:
            with self.db.connection() as conn, conn.cursor() as cur:
                cur.execute(
                    f"""
                    WITH RECURSIVE parents(id) AS (
                        SELECT ?
                        UNION
                        SELECT r.to_id FROM relations r JOIN parents p ON r.from_id = p.id
                        WHERE r.kind = 'part_of' AND {live_relation_sql("r")}
                    )
                    SELECT 1 FROM parents WHERE id = ? LIMIT 1
                    """,
                    (to_id, from_id),
                )
                if cur.fetchone() is not None:
                    raise StoreError("The part_of relation would create a cycle.")

    @writes
    def upsert(self, capsule_id: str, kind: str, scope: str, at: str) -> None:
        """Set the capsule's kind and scope, replacing any earlier row."""
        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO classifications (capsule_id, kind, scope, at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (capsule_id) DO UPDATE SET
                    kind = EXCLUDED.kind,
                    scope = EXCLUDED.scope,
                    at = EXCLUDED.at
                """,
                (capsule_id, kind, scope, at),
            )
