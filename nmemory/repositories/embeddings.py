"""The embedding index: vectors, the job queue that fills them, and the space they live in."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from .. import NMemoryError
from ..capsule import headline_of, sha256_hex
from ..database import now_us, vector_blob
from ..project import glob_subtree_pattern
from ..space import EmbeddingError, SpaceFingerprint
from .base import Repository

#: A job claimed this many times is never claimed again (`claim_embedding`).
MAX_EMBEDDING_ATTEMPTS = 20


class SupersededSpace(NMemoryError):
    """Embeddings exist in the fence, but none under the requested space."""


@dataclass(frozen=True)
class EmbeddingJob:
    capsule_id: str
    index_text: str
    text_fingerprint: str
    target_space: str


class EmbeddingRepository(Repository):
    """Owns capsule_embeddings, embedding_state, embedding_jobs."""

    def search_cosine(
        self,
        query_vector: list[float],
        limit: int = 50,
        project_id: str | None = None,
        project_prefix: str | None = None,
        space_fingerprint: str | None = None,
    ) -> list[tuple[str, float]]:
        """The `limit` nearest capsules by cosine similarity, exact, inside the fence.

        `is not None`, NOT truthiness, for both fences: `""` is a fence that
        matches nothing, never "no fence".
        """
        if limit < 0:
            raise ValueError(f"limit must be >= 0, got {limit}")
        conditions: list[str] = []
        params: list[Any] = []
        if project_id is not None:
            conditions.append("{p} = ?")
            params.append(project_id)
        if project_prefix is not None:
            conditions.append("({p} = ? OR {p} GLOB ?)")
            params.extend([project_prefix, glob_subtree_pattern(project_prefix)])
        fence = " AND ".join(conditions).format(p="project_id") or "1"
        with self.db.connection() as handle, handle.cursor() as cur:
            if space_fingerprint:
                # Distinguish no embeddings from embeddings under another space (F008).
                # `+project_id` keeps the count off the clustered 12 KiB rows: it
                # scans the narrow capsule index instead (48 ms -> 1.4 ms warm).
                check = " AND ".join(conditions).format(p="+project_id") or "1"
                row = cur.execute(
                    f"SELECT count(*), coalesce(sum(space_fingerprint = ?), 0)"
                    f" FROM capsule_embeddings WHERE {check}",
                    (space_fingerprint, *params),
                ).fetchone()
                total, in_space = (int(row[0]), int(row[1])) if row else (0, 0)
                if total > 0 and in_space == 0:
                    raise SupersededSpace(
                        "embeddings exist under superseded space fingerprint, "
                        f"not '{space_fingerprint}'"
                    )
                if total == 0:
                    return []
                fence += " AND space_fingerprint = ?"
                params.append(space_fingerprint)
            rows = cur.execute(
                f"""
                SELECT capsule_id, vec_distance_cosine(embedding, ?) AS d
                FROM capsule_embeddings WHERE {fence}
                ORDER BY d ASC NULLS LAST, capsule_id LIMIT ?
                """,
                (vector_blob(query_vector), *params, limit),
            ).fetchall()
        return [
            (str(r[0]), float("nan") if r[1] is None else 1.0 - float(r[1]))
            for r in rows
        ]

    def queue_embeddings(
        self, inputs: list[tuple[str, str, str]], target_space: str
    ) -> int:
        """Queue `(capsule_id, index_text, text_fingerprint)` in one statement.

        A job already current, or a vector already embedded for that text in
        that space, is left alone; a changed text or space re-arms the job.
        """
        if not inputs:
            return 0
        now = now_us()
        with self.db.writing() as handle, handle.cursor() as cur:
            # Covered capsules also abandon old-space jobs. Delete them under
            # the same lock as queue replacement, before skipping covered inputs.
            cur.execute(
                "DELETE FROM embedding_jobs WHERE target_space <> :space"
                " AND capsule_id IN (SELECT value ->> 0 FROM json_each(:inputs))",
                {
                    "space": target_space,
                    "inputs": json.dumps([list(i) for i in inputs]),
                },
            )
            cur.execute(
                """
                INSERT INTO embedding_jobs
                    (capsule_id, index_text, text_fingerprint, target_space,
                     attempts, available_at, updated_at)
                SELECT j.value ->> 0, j.value ->> 1, j.value ->> 2, :space, 0, :now, :now
                FROM json_each(:inputs) AS j
                WHERE EXISTS (
                    SELECT 1 FROM capsules c WHERE c.id = j.value ->> 0
                      AND c.canonical_json IS NOT NULL)
                  AND NOT EXISTS (
                    SELECT 1 FROM capsule_embeddings e
                    WHERE e.capsule_id = j.value ->> 0
                      AND e.text_fingerprint = j.value ->> 2
                      AND e.space_fingerprint = :space)
                ON CONFLICT (capsule_id) DO UPDATE SET
                    index_text = excluded.index_text,
                    text_fingerprint = excluded.text_fingerprint,
                    target_space = excluded.target_space,
                    attempts = 0,
                    available_at = excluded.available_at,
                    last_error_class = NULL,
                    updated_at = excluded.updated_at
                WHERE embedding_jobs.text_fingerprint <> excluded.text_fingerprint
                   OR embedding_jobs.target_space <> excluded.target_space
                """,
                {
                    "space": target_space,
                    "now": now,
                    "inputs": json.dumps([list(i) for i in inputs]),
                },
            )
            return cur.rowcount

    @staticmethod
    def _job(row: Any) -> EmbeddingJob:
        return EmbeddingJob(
            capsule_id=str(row[0]),
            index_text=str(row[1]),
            text_fingerprint=str(row[2]),
            target_space=str(row[3]),
        )

    def prune_embeddings(self) -> None:
        """Remove orphan jobs and vectors using current capsule rows under the lock."""
        with self.db.writing() as handle, handle.cursor() as cur:
            for table in ("embedding_jobs", "capsule_embeddings"):
                cur.execute(
                    f"DELETE FROM {table} WHERE NOT EXISTS"
                    f" (SELECT 1 FROM capsules c WHERE c.id = {table}.capsule_id"
                    " AND c.canonical_json IS NOT NULL)"
                )

    def claim_embedding(self, space: str | None = None) -> EmbeddingJob | None:
        """The next ready job; with `space`, only a job targeting that space, so a
        producer never takes work it would label with another model's space."""
        now = now_us()
        with self.db.writing() as handle, handle.cursor() as cur:
            row = cur.execute(
                """
                UPDATE embedding_jobs SET attempts = attempts + 1,
                    available_at = :later, updated_at = :now
                WHERE capsule_id = (
                    SELECT capsule_id FROM embedding_jobs
                    WHERE available_at <= :now AND attempts < :cap
                      AND (:space IS NULL OR target_space = :space)
                      AND EXISTS (SELECT 1 FROM capsules c
                          WHERE c.id = embedding_jobs.capsule_id
                            AND c.canonical_json IS NOT NULL)
                    ORDER BY available_at, capsule_id LIMIT 1)
                RETURNING capsule_id, index_text, text_fingerprint, target_space
                """,
                {
                    "now": now,
                    "later": now + 120_000_000,
                    "space": space,
                    "cap": MAX_EMBEDDING_ATTEMPTS,
                },
            ).fetchone()
        return None if row is None else self._job(row)

    def _current(self, job: EmbeddingJob) -> bool:
        with self.db.connection() as handle:
            return (
                handle.execute(
                    "SELECT 1 FROM embedding_jobs j JOIN capsules c ON c.id = j.capsule_id"
                    " WHERE j.capsule_id = ? AND j.index_text = ?"
                    " AND j.text_fingerprint = ? AND j.target_space = ?"
                    " AND c.canonical_json IS NOT NULL"
                    " AND j.target_space = (SELECT coalesce(target_space, active_space)"
                    " FROM embedding_state)",
                    (
                        job.capsule_id,
                        job.index_text,
                        job.text_fingerprint,
                        job.target_space,
                    ),
                ).fetchone()
                is not None
            )

    @contextmanager
    def submission(self, jobs: list[EmbeddingJob]) -> Iterator[None]:
        """Serialize job admission and transmission with purge, before each attempt."""
        with self.db.writing():
            if any(not self._current(job) for job in jobs):
                raise EmbeddingError("embedding job is no longer current")
            yield

    def complete_embedding(
        self, job: EmbeddingJob, vector: list[float], space: str | None = None
    ) -> bool:
        """Store the vector for a job still current; False when it moved on, or
        when `space` (the producer's) is not the job's target."""
        if space is not None and space != job.target_space:
            return False
        with self.db.writing() as handle, handle.cursor() as cur:
            if not self._current(job):
                return False
            cur.execute(
                "DELETE FROM capsule_embeddings WHERE capsule_id = ?", (job.capsule_id,)
            )
            cur.execute(
                """
                INSERT INTO capsule_embeddings
                    (project_id, space_fingerprint, capsule_id, text_fingerprint,
                     embedding, updated_at)
                SELECT project_id, ?, id, ?, ?, ? FROM capsules
                WHERE id = ? AND canonical_json IS NOT NULL
                """,
                (
                    job.target_space,
                    job.text_fingerprint,
                    vector_blob(vector),
                    now_us(),
                    job.capsule_id,
                ),
            )
            # No capsule, no vector: the job is spent either way.
            stored = cur.rowcount == 1
            cur.execute(
                "DELETE FROM embedding_jobs WHERE capsule_id = ?", (job.capsule_id,)
            )
            return stored

    #: Failures of the endpoint, not of the job: they give back the claim's attempt,
    #: so an endpoint down for hours (how a re-embed is paused) strands no job.
    _ENDPOINT_FAILURES = ("failover_eligible", "rate_limited", "quota_exhausted")

    def fail_embedding(
        self, job: EmbeddingJob, error_class: str, *, attempt_charged: bool = False
    ) -> None:
        """Defer the failure; only a caller that claimed the job can refund it.

        Batch submission defers without claiming, so collection cannot refund an
        earlier worker's attempt, even when the job already has a positive count.
        """
        now = now_us()
        refund = int(attempt_charged and error_class in self._ENDPOINT_FAILURES)
        with self.db.writing() as handle, handle.cursor() as cur:
            cur.execute(
                """
                UPDATE embedding_jobs SET last_error_class = ?, available_at = ?,
                    updated_at = ?, attempts = attempts - ?
                WHERE capsule_id = ? AND text_fingerprint = ? AND target_space = ?
                """,
                (
                    error_class,
                    now + 300_000_000,
                    now,
                    refund,
                    job.capsule_id,
                    job.text_fingerprint,
                    job.target_space,
                ),
            )

    #: The vector lane's off phases: `lexical_only` while a provider refuses, and
    #: `stalled` while this host's embedding configuration is refused (status reports
    #: it as `lexical_only` with `last_error = config_error`; no schema change).
    _OFF = ("lexical_only", "stalled")

    def _move_phase(self, from_phases: tuple[str, ...], to: str) -> None:
        """Move the phase when it is in `from_phases`. A read first: a recall or a
        cycle that finds the state already right takes no write lock."""
        with self.db.connection() as handle, handle.cursor() as cur:
            row = cur.execute("SELECT phase FROM embedding_state").fetchone()
        if row is None or str(row[0]) not in from_phases:
            return
        marks = ",".join("?" * len(from_phases))
        with self.db.writing() as handle, handle.cursor() as cur:
            cur.execute(
                f"UPDATE embedding_state SET phase = {to}, updated_at = ?"
                f" WHERE phase IN ({marks})",
                (now_us(), *from_phases),
            )

    def serving_space(self) -> str | None:
        """The space recall may search: the active one once it is whole, never one
        being built (a first build, a new model, or a rollback repairing the old)."""
        with self.db.connection() as handle, handle.cursor() as cur:
            row = cur.execute(
                "SELECT active_space, target_space FROM embedding_state"
            ).fetchone()
        if row is None or row[1] is not None:
            return None
        return str(row[0])

    def set_lexical_only(self, lexical_only: bool) -> None:
        """Report that the provider refuses until an operator acts, or that it
        embeds again: `lexical_only` until a vector lands, then the phase it left."""
        if lexical_only:
            self._move_phase(("ready", "reindexing", "stalled"), "'lexical_only'")
        else:
            self._move_phase(self._OFF, self._RESUMED)

    _RESUMED = "CASE WHEN target_space IS NULL THEN 'ready' ELSE 'reindexing' END"

    def mark_config_error(self) -> None:
        """This host's embedding configuration is refused: the lane is off."""
        self._move_phase(("ready", "reindexing", "lexical_only"), "'stalled'")

    def clear_config_error(self) -> None:
        """The configuration is accepted again: leave `stalled`."""
        self._move_phase(("stalled",), self._RESUMED)

    def begin_current_space_transition_if_needed(self) -> SpaceFingerprint | None:
        expected = SpaceFingerprint.current()
        with self.db.writing() as handle, handle.cursor() as cur:
            row = cur.execute(
                "SELECT coalesce(target_space, active_space) FROM embedding_state"
            ).fetchone()
            if row is None or str(row[0]) == expected.as_str():
                return None
            cur.execute(
                "UPDATE embedding_state SET target_space = ?, phase = 'reindexing',"
                " updated_at = ?",
                (expected.as_str(), now_us()),
            )
            return expected

    def activate_target_if_complete(self) -> bool:
        """Activate only current-text coverage of every live capsule.

        The worker starts a transition before queuing its jobs. An empty target
        queue in that interval is not completion. Hold the write lock across
        coverage and activation so capture, queue and transition writers cannot
        change the evidence between the check and the state update.
        """
        with self.db.writing() as handle, handle.cursor() as cur:
            row = cur.execute("SELECT target_space FROM embedding_state").fetchone()
            if row is None or not row[0]:
                return False
            target = str(row[0])
            pending = cur.execute(
                "SELECT count(*) FROM embedding_jobs WHERE target_space = ?", (target,)
            ).fetchone()
            if pending is not None and int(pending[0]) != 0:
                return False
            cur.execute(
                "SELECT json_extract(c.canonical_json, '$.content'), e.text_fingerprint"
                " FROM capsules c LEFT JOIN capsule_embeddings e"
                " ON e.capsule_id = c.id AND e.project_id = c.project_id"
                " AND e.space_fingerprint = ?"
                " WHERE c.canonical_json IS NOT NULL",
                (target,),
            )
            while (coverage := cur.fetchone()) is not None:
                content, fingerprint = coverage
                if fingerprint is None:
                    return False
                index_text = f"headline: {headline_of(content)}\ncontent: {content}"
                if fingerprint != sha256_hex(index_text.encode("utf-8")):
                    return False
            cur.execute(
                "UPDATE embedding_state SET active_space = ?, target_space = NULL,"
                " phase = 'ready', updated_at = ?",
                (target, now_us()),
            )
            cur.execute(
                "DELETE FROM capsule_embeddings WHERE space_fingerprint <> ?", (target,)
            )
            return True
