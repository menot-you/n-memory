"""Embedding index maintenance: the background worker that keeps capsule vectors current.

The worker elects itself, drains the embedding job queue through the HTTP client in
`embedding`, re-syncs the space on every generation and runs the protective
consolidation pass. Every store call and write is here; `embedding` holds only the
client and its wire helpers.
"""

from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING

import nott_database
from nott_database import DatabaseError

from . import NMemoryError
from .capsule import headline_of, sha256_hex
from .consolidate import (
    ConsolidationRecord,
    Tier,
    archive_deadline,
    protective_tier_moves,
)
from .embedding import (
    GEMINI_MAX_BATCH,
    EmbeddingClient,
)
from .queries.embedding import embedding_status
from .queries.sidecars import digest_sidecars, recall_mark
from .space import (
    EmbeddingError,
    EmbeddingQuotaExhausted,
    EmbeddingRateLimited,
    EmbeddingTransportError,
)

if TYPE_CHECKING:
    from .repositories.embeddings import EmbeddingJob
    from .store import Store

logger = logging.getLogger(__name__)

#: How often a standby worker retries the election.
ELECTION_POLL_S = 0.25


OPENAI_JOBS_PER_CYCLE = 192


class _ConsolidationDeferred(NMemoryError):
    """This consolidation gives up its turn; the boundary must ROLL BACK.

    Private and always caught inside `_consolidate`, so it never reaches the
    drain loop — and it derives from `NMemoryError` because that is what the
    drain loop itself catches, so a future escape is absorbed there instead of
    killing the embedding thread. (`tests/test_worker_recovery.py` pins the
    narrower `DatabaseError`, so it would NOT notice that guarantee being
    narrowed away; the guarantee lives in the drain loop's own `except`.)
    """


class EmbeddingWorker:
    """Automatic background worker for embedding queue drain and space transitions."""

    def __init__(
        self,
        store: Store,
        client: EmbeddingClient | None = None,
    ) -> None:
        self.store = store
        self.client = client or EmbeddingClient()
        self._stop_event = threading.Event()
        self._synced_generation: int | None = None
        self._consolidated_state: tuple[int, int] | None = None
        self._consolidation_due: datetime | None = None

    def stop(self) -> None:
        self._stop_event.set()

    def is_stopped(self) -> bool:
        return self._stop_event.is_set()

    def _consolidate(self) -> None:
        pg = self.store.db
        if not pg.owns_embedding_worker:
            return
        try:
            now = datetime.now(UTC)
            state = (self.store.audit.audit_watermark(), pg.get_generation())
            if state == self._consolidated_state and (
                self._consolidation_due is None or now <= self._consolidation_due
            ):
                return
            # ONE rule, so nobody has to re-derive which side of `begin_write`
            # they are on: a deferral leaves the boundary by RAISING. A bare
            # `return` is a NORMAL exit, so `tool_boundary` takes the COMMIT arm
            # — and once `begin_write` has taken the write lock that arm bumps
            # `generation` with zero rows changed.
            with pg.tool_boundary():
                mark = recall_mark(self.store.db)
                sidecars = digest_sidecars(self.store.db)
                superseded = self.store.relations.superseded_among(list(sidecars))
                supersession_archives = self.store.tiers.supersession_archives(
                    [cid for cid, sc in sidecars.items() if sc.tier is Tier.ARCHIVED]
                )
                records = []
                for stored in self.store.capsules.list():
                    sidecar = sidecars.get(stored.id)
                    if sidecar is None:
                        # A concurrent capture crossed the read; retry next cycle.
                        raise _ConsolidationDeferred
                    records.append(
                        ConsolidationRecord(
                            id=stored.id,
                            seq=stored.seq,
                            source_hash=stored.capsule.provenance.source_hash,
                            content=stored.capsule.content,
                            instruction_taint=stored.capsule.instruction_taint,
                            authority_class=stored.capsule.authority_class,
                            valid_to=stored.capsule.freshness.valid_to,
                            created_at=stored.created_at,
                            recall_count=sidecar.recall_count,
                            is_superseded=stored.id in superseded,
                            tier=sidecar.tier,
                            pinned=sidecar.pinned,
                            supersession_archive=stored.id in supersession_archives,
                        )
                    )
                moves = protective_tier_moves(records, now)
                if moves:
                    pg.begin_write()
                # Settle after collecting AND after acquiring the write lock.
                # Direct Store writers can advance generation without an audit.
                if state != (self.store.audit.audit_watermark(), pg.get_generation()):
                    raise _ConsolidationDeferred
                applied = (
                    self.store.apply_tier_moves(
                        records, moves, mark, "embedding-worker", now
                    )
                    if moves
                    else []
                )
                settled = self.store.audit.audit_watermark()
            # A mark read after commit could swallow another writer's change.
            self._consolidated_state = (settled, state[1] + bool(applied))
            moved = {move.id for move in applied}
            self._consolidation_due = min(
                (
                    deadline
                    for record in records
                    if record.id not in moved
                    and (deadline := archive_deadline(record)) is not None
                ),
                default=None,
            )
        except _ConsolidationDeferred:
            # Rolled back by `tool_boundary`. `_consolidated_state` stays put on
            # purpose: the next cycle must re-read, not believe this one.
            return

    def cycle(self) -> int:
        """Run one worker cycle. Returns number of embeddings completed."""
        pg = self.store.db
        # 1. Check space transition
        # A bad configuration has no space to move to; the sync below still runs.
        began = (
            None
            if self.client.config_error is not None
            else self.store.embeddings.begin_current_space_transition_if_needed()
        )
        generation = pg.get_generation()

        # 2. Sync queue from live capsules
        if began is not None or generation != self._synced_generation:
            live = self.store.capsules.list()
            inputs = []
            for sc in live:
                content = sc.capsule.content
                idx_text = f"headline: {headline_of(content)}\ncontent: {content}"
                fp = sha256_hex(idx_text.encode("utf-8"))
                inputs.append((sc.id, idx_text, fp))

            status = embedding_status(pg)
            target = status.target_space or status.active_space
            self.store.embeddings.prune_embeddings()
            self.store.embeddings.queue_embeddings(inputs, target)
            self._synced_generation = generation

        # Protection needs neither an embedding key nor the quadratic merge planner.
        self._consolidate()

        # 3. Claim and process jobs
        if self.client.config_error is not None:
            self.store.embeddings.mark_config_error()
        else:
            self.store.embeddings.clear_config_error()
        if not self.client.is_configured():
            return 0

        # Claim and complete only in this client's own space (two servers can run
        # different models while an install moves the store from one to the other).
        space = self.client.space_fingerprint.as_str()
        batch_limit = (
            min(OPENAI_JOBS_PER_CYCLE, GEMINI_MAX_BATCH)
            if self.client.space_provider == "gemini"
            else OPENAI_JOBS_PER_CYCLE
        )
        claimed_jobs: list[EmbeddingJob] = []
        for _ in range(batch_limit):
            job = self.store.embeddings.claim_embedding(space)
            if not job:
                break
            claimed_jobs.append(job)

        completed = 0
        if claimed_jobs:
            # Embed documents
            texts = [j.index_text for j in claimed_jobs]
            try:
                vectors = self.client.embed_documents(
                    texts, guard=partial(self.store.embeddings.submission, claimed_jobs)
                )
                for job, vec in zip(claimed_jobs, vectors, strict=True):
                    if self.store.embeddings.complete_embedding(job, vec, space):
                        completed += 1
            except (EmbeddingQuotaExhausted, EmbeddingRateLimited) as e:
                # Item by item would hit the same refusal once per job.
                exhausted = isinstance(e, EmbeddingQuotaExhausted)
                logger.warning("embedding provider refused: %s", e)
                for job in claimed_jobs:
                    self.store.embeddings.fail_embedding(
                        job,
                        "quota_exhausted" if exhausted else "rate_limited",
                        attempt_charged=True,
                    )
                if exhausted:
                    self.store.embeddings.set_lexical_only(True)
                return 0
            except (EmbeddingError, OSError, ValueError, TypeError) as e:
                logger.debug("batch embedding failed, falling back to per-item: %s", e)
                for index, job in enumerate(claimed_jobs):
                    try:
                        vec = self.client.embed_document(
                            job.index_text,
                            guard=partial(self.store.embeddings.submission, [job]),
                        )
                        if self.store.embeddings.complete_embedding(job, vec, space):
                            completed += 1
                    except EmbeddingQuotaExhausted as err:
                        logger.warning(
                            "embedding provider refused; recall is lexical: %s", err
                        )
                        for remaining in claimed_jobs[index:]:
                            self.store.embeddings.fail_embedding(
                                remaining, "quota_exhausted", attempt_charged=True
                            )
                        self.store.embeddings.set_lexical_only(True)
                        return completed
                    except (EmbeddingError, OSError, ValueError, TypeError) as err:
                        error_class = "operator_action"
                        if isinstance(err, EmbeddingRateLimited):
                            error_class = "rate_limited"
                        elif isinstance(err, EmbeddingTransportError):
                            error_class = "failover_eligible"
                        self.store.embeddings.fail_embedding(
                            job, error_class, attempt_charged=True
                        )
            if completed:
                self.store.embeddings.set_lexical_only(False)
        else:
            self.store.embeddings.activate_target_if_complete()

        return completed

    def drain(self, max_cycles: int = 100) -> int:
        total = 0
        for _ in range(max_cycles):
            done = self.cycle()
            total += done
            if done == 0:
                break
        return total


def spawn_embedding_worker(
    store: Store, client: EmbeddingClient | None = None
) -> tuple[EmbeddingWorker, threading.Thread]:

    worker = EmbeddingWorker(store, client)

    def _run() -> None:
        while not worker.is_stopped():
            try:
                with store.db.embedding_worker_owner() as owner:
                    while owner and not worker.is_stopped():
                        worker.cycle()
                        if not worker.client.is_configured():
                            break
                        worker._stop_event.wait(1.0)
            except (
                NMemoryError,
                DatabaseError,
                nott_database.Error,
                OSError,
                ValueError,
                TypeError,
            ) as e:
                logger.debug("embedding worker error: %s", e)
            # A standby polls the lock four times a second, so it takes over
            # within a second of the owner's death (the kernel frees the flock).
            worker._stop_event.wait(ELECTION_POLL_S)

    t = threading.Thread(target=_run, daemon=True, name="nmemory-embedding-worker")
    t.start()
    return worker, t
