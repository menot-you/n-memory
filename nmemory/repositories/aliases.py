"""Synonyms: the alias terms that widen a capsule's lexical reach."""

from __future__ import annotations

from datetime import datetime

from ..capsule import format_rfc3339
from ..database import writes
from .base import EmptyField, Repository, StoreError


class SelfAlias(StoreError):
    pass


class AliasRepository(Repository):
    """Owns synonyms."""

    @writes
    def add_alias(self, term: str, alias: str, now: datetime) -> bool:
        t = term.strip().lower()
        a = alias.strip().lower()
        if not t or not a:
            raise EmptyField("alias pair fields cannot be empty")
        if t == a:
            raise SelfAlias(f"self-alias not allowed: {t}")

        at_str = format_rfc3339(now)
        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1 FROM synonyms WHERE term = ? AND alias = ?", (t, a))
            if cur.fetchone() is not None:
                return False

            cur.execute(
                "INSERT INTO synonyms (term, alias, at) VALUES (?, ?, ?) ON CONFLICT DO NOTHING",
                (t, a, at_str),
            )
            return True

    def aliases_for(self, term: str) -> list[str]:
        t = term.strip().lower()
        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT alias FROM synonyms WHERE term = ? ORDER BY alias ASC",
                (t,),
            )
            return [str(r[0]) for r in cur.fetchall()]

    def list_aliases(self) -> list[tuple[str, str, str]]:
        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT term, alias, at FROM synonyms ORDER BY term ASC, alias ASC"
            )
            return [(str(r[0]), str(r[1]), str(r[2])) for r in cur.fetchall()]
