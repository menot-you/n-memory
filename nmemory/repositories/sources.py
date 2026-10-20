"""The imported blocks each import source already wrote."""

from __future__ import annotations

from dataclasses import dataclass

from ..database import writes
from .base import Repository


@dataclass(frozen=True)
class ImportBlockRow:
    block_hash: str
    capsule_id: str
    ordinal: int


class SourceRepository(Repository):
    """Owns import_blocks."""

    def import_blocks_for(self, source_key: str) -> list[ImportBlockRow]:
        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT block_hash, capsule_id, ordinal
                FROM import_blocks
                WHERE source_key = ?
                ORDER BY ordinal ASC, block_hash ASC
                """,
                (source_key,),
            )
            return [
                ImportBlockRow(
                    block_hash=str(r[0]),
                    capsule_id=str(r[1]),
                    ordinal=int(r[2]),
                )
                for r in cur.fetchall()
            ]

    @writes
    def forget_import_block(self, source_key: str, block_hash: str) -> None:
        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM import_blocks WHERE source_key = ? AND block_hash = ?",
                (source_key, block_hash),
            )

    def import_block_owners(self, capsule_id: str) -> list[str]:
        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT source_key
                FROM import_blocks
                WHERE capsule_id = ?
                ORDER BY source_key ASC
                """,
                (capsule_id,),
            )
            return [str(r[0]) for r in cur.fetchall()]

    @writes
    def insert_import_block(
        self, source_key: str, block_hash: str, capsule_id: str, ordinal: int, at: str
    ) -> bool:
        """Record that `source_key`'s block `block_hash` became `capsule_id`;
        `False` when the block was already recorded."""
        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO import_blocks (source_key, block_hash, capsule_id, ordinal, at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (source_key, block_hash) DO NOTHING
                """,
                (source_key, block_hash, capsule_id, ordinal, at),
            )
            return cur.rowcount > 0
