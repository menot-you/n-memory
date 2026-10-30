"""Synonyms: the alias terms that widen a capsule's lexical reach."""

from __future__ import annotations

from .base import Repository


class AliasRepository(Repository):
    """Owns synonyms."""

    def list_aliases(self) -> list[tuple[str, str, str]]:
        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT term, alias, at FROM synonyms ORDER BY term ASC, alias ASC"
            )
            return [(str(r[0]), str(r[1]), str(r[2])) for r in cur.fetchall()]
