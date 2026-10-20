"""The embedding index's coverage, read across the capsules and the index."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..database import Database, StoreNotInitialized
from ..repositories.embeddings import MAX_EMBEDDING_ATTEMPTS


@dataclass(frozen=True)
class EmbeddingStatus:
    active_space: str
    target_space: str | None
    phase: str
    #: Jobs a claim can still take.
    pending: int
    #: Jobs at the claim cap: never claimed again, so never drained.
    exhausted: int
    active_vectors: int
    live_capsules: int
    #: While the lane is `lexical_only`: `config_error` when this host's embedding
    #: configuration is refused, else the newest failed job's error class, or
    #: `query_embedding_failed` when only a recall's query embedding failed.
    last_error: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "active_space": self.active_space,
            "target_space": self.target_space,
            "phase": self.phase,
            "pending": self.pending,
            "exhausted": self.exhausted,
            "active_vectors": self.active_vectors,
            "live_capsules": self.live_capsules,
            "last_error": self.last_error,
        }


def embedding_status(db: Database) -> EmbeddingStatus:
    with db.connection() as handle, handle.cursor() as cur:
        row = cur.execute(
            """
            SELECT s.active_space, s.target_space,
                CASE s.phase WHEN 'stalled' THEN 'lexical_only' ELSE s.phase END,
                (SELECT count(*) FROM embedding_jobs WHERE attempts < :cap),
                (SELECT count(*) FROM embedding_jobs WHERE attempts >= :cap),
                (SELECT count(*) FROM capsule_embeddings e
                 WHERE e.space_fingerprint = s.active_space),
                (SELECT count(*) FROM capsules WHERE canonical_json IS NOT NULL),
                CASE WHEN s.phase = 'stalled' THEN 'config_error'
                WHEN s.phase = 'lexical_only' THEN coalesce(
                (SELECT last_error_class FROM embedding_jobs
                 WHERE last_error_class IS NOT NULL
                 ORDER BY updated_at DESC LIMIT 1), 'query_embedding_failed') END
            FROM embedding_state s
            """,
            {"cap": MAX_EMBEDDING_ATTEMPTS},
        ).fetchone()
    if row is None:
        raise StoreNotInitialized("embedding_state singleton is not initialized")
    return EmbeddingStatus(
        active_space=str(row[0]),
        target_space=None if row[1] is None else str(row[1]),
        phase=str(row[2]),
        pending=int(row[3]),
        exhausted=int(row[4]),
        active_vectors=int(row[5]),
        live_capsules=int(row[6]),
        last_error=None if row[7] is None else str(row[7]),
    )
