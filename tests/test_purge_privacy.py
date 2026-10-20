"""Purge removes private sidecars and fences delayed embedding work."""

from __future__ import annotations

import io
import json
from typing import Any

import pytest

from nmemory import embedding
from nmemory.embedding import EmbeddingClient
from nmemory.embedding_index import EmbeddingWorker
from nmemory.repositories.embeddings import EmbeddingJob
from nmemory.server import MemoryServer
from nmemory.space import DIMENSION, EmbeddingError
from nmemory.store import Store

CONTENT = "The cobalt memory contains private factual text."
QUOTE = "The original cobalt source quote contains private text."
VECTOR = [1.0] + [0.0] * (DIMENSION - 1)


def _ok(server: MemoryServer, name: str, args: dict[str, Any]) -> dict[str, Any]:
    reply = server.call_tool(name, args)
    assert "result" in reply and not reply["result"].get("isError"), reply
    return json.loads(reply["result"]["content"][0]["text"])


@pytest.fixture
def captured() -> tuple[Store, MemoryServer, str, EmbeddingJob]:
    store = Store.open_disposable_test_store()
    server = MemoryServer(store=store, hmac_env_key=b"fixture" * 8)
    result = _ok(
        server,
        "nmemory_capsule_ingest",
        {
            "content": CONTENT,
            "kind": "fact",
            "source": "fixture",
            "anchor": "fixture.py:1",
            "source_quote": QUOTE,
            "project_id": "purge-fixture",
            "hints": False,
        },
    )
    cid = result["outcomes"][0]["id"]
    with store.db.connection() as conn:
        row = conn.execute(
            "SELECT capsule_id, index_text, text_fingerprint, target_space"
            " FROM embedding_jobs WHERE capsule_id = ?",
            (cid,),
        ).fetchone()
    assert row is not None
    return store, server, cid, EmbeddingJob(*row)


def _purge(server: MemoryServer, cid: str, mode: str = "purged") -> dict[str, Any]:
    return _ok(
        server,
        "nmemory_capsule_purge",
        {"id": cid, "mode": mode, "reason": "Remove this disposable fixture."},
    )


def _job_count(store: Store) -> int:
    with store.db.connection() as conn:
        return int(conn.execute("SELECT count(*) FROM embedding_jobs").fetchone()[0])


def _seed_stale_job(store: Store, job: EmbeddingJob) -> None:
    with store.db.writing() as conn:
        conn.execute(
            "INSERT INTO embedding_jobs"
            " (capsule_id, index_text, text_fingerprint, target_space, available_at, updated_at)"
            " VALUES (?, ?, ?, ?, 0, 0)",
            (job.capsule_id, job.index_text, job.text_fingerprint, job.target_space),
        )


@pytest.mark.parametrize("mode", ["purged", "redacted"])
def test_purge_removes_quote_slug_jobs_and_vectors_atomically(
    captured: tuple[Store, MemoryServer, str, EmbeddingJob],
    mode: str,
) -> None:
    store, server, cid, job = captured
    assert store.embeddings.complete_embedding(job, VECTOR)
    assert (
        store.embeddings.queue_embeddings(
            [(cid, job.index_text, job.text_fingerprint)], "next-space"
        )
        == 1
    )
    result = _purge(server, cid, mode)
    assert store.epistemics.get_source_quote(cid) is None
    assert store.capsules.handle_slug(cid) is None
    assert CONTENT.split()[1].lower() not in result["handle"]
    assert _job_count(store) == 0
    with store.db.connection() as conn:
        assert (
            conn.execute("SELECT count(*) FROM capsule_embeddings").fetchone()[0] == 0
        )
        assert (
            conn.execute(
                "SELECT canonical_json FROM capsules WHERE id = ?", (cid,)
            ).fetchone()[0]
            is None
        )
    shown = _ok(server, "nmemory_capsule_show", {"id": cid})
    assert CONTENT not in json.dumps(shown)
    assert QUOTE not in json.dumps(shown)
    assert store.capsules.get_tombstone(cid) is not None


def test_failed_purge_preserves_the_complete_capture(
    captured: tuple[Store, MemoryServer, str, EmbeddingJob],
) -> None:
    store, server, cid, job = captured
    assert store.embeddings.complete_embedding(job, VECTOR)
    store.embeddings.queue_embeddings(
        [(cid, job.index_text, job.text_fingerprint)], "next-space"
    )
    with store.db.writing() as conn:
        conn.execute(
            "CREATE TRIGGER refuse_fixture_purge BEFORE INSERT ON tombstones"
            " BEGIN SELECT RAISE(ABORT, 'fixture purge failure'); END"
        )
    reply = server.call_tool(
        "nmemory_capsule_purge",
        {"id": cid, "mode": "purged", "reason": "Remove this fixture."},
    )
    assert reply.get("error") or reply.get("result", {}).get("isError"), reply
    assert store.capsules.get(cid).capsule.content == CONTENT
    assert store.epistemics.get_source_quote(cid)["quote"] == QUOTE
    assert store.capsules.handle_slug(cid) is not None
    assert _job_count(store) == 1
    with store.db.connection() as conn:
        assert (
            conn.execute("SELECT count(*) FROM capsule_embeddings").fetchone()[0] == 1
        )
    assert store.capsules.get_tombstone(cid) is None


def test_a_stale_snapshot_cannot_requeue_purged_text(
    captured: tuple[Store, MemoryServer, str, EmbeddingJob],
) -> None:
    store, server, cid, job = captured
    _purge(server, cid)
    assert (
        store.embeddings.queue_embeddings(
            [(cid, job.index_text, job.text_fingerprint)], job.target_space
        )
        == 0
    )
    assert _job_count(store) == 0
    assert not store.embeddings.complete_embedding(job, VECTOR)


@pytest.mark.parametrize("operation", ["claim", "complete", "prune"])
def test_old_purged_jobs_cannot_run_or_restore_vectors(
    captured: tuple[Store, MemoryServer, str, EmbeddingJob],
    operation: str,
) -> None:
    store, server, cid, job = captured
    _purge(server, cid)
    with store.db.writing() as conn:
        conn.execute("DELETE FROM embedding_jobs")
    _seed_stale_job(store, job)
    if operation == "claim":
        assert store.embeddings.claim_embedding() is None
    elif operation == "complete":
        assert not store.embeddings.complete_embedding(job, VECTOR)
    else:
        EmbeddingWorker(store, EmbeddingClient()).cycle()
        assert _job_count(store) == 0
    with store.db.connection() as conn:
        assert (
            conn.execute("SELECT count(*) FROM capsule_embeddings").fetchone()[0] == 0
        )


def test_worker_does_not_send_the_last_purged_capsule(
    captured: tuple[Store, MemoryServer, str, EmbeddingJob],
) -> None:
    store, server, cid, _ = captured
    sent: list[str] = []

    def record(text: str) -> list[float]:
        sent.append(text)
        return VECTOR

    _purge(server, cid)
    assert EmbeddingWorker(store, EmbeddingClient(custom_hook=record)).cycle() == 0
    assert sent == []


def test_a_claimed_job_is_checked_before_provider_transmission(
    captured: tuple[Store, MemoryServer, str, EmbeddingJob],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _, cid, _ = captured
    other = MemoryServer(store=Store.open(store.db.path), hmac_env_key=b"fixture" * 8)
    sent: list[bytes] = []
    purged = False

    class Connection:
        sock = None

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            nonlocal purged
            if not purged:
                purged = True
                _purge(other, cid)

        def connect(self) -> None:
            pass

        def request(self, method: str, path: str, *, body: bytes, headers: Any) -> None:
            sent.append(body)
            raise OSError("fixture response failure")

        def close(self) -> None:
            pass

    monkeypatch.setattr(embedding.http.client, "HTTPSConnection", Connection)
    assert EmbeddingWorker(store, EmbeddingClient(api_key="fixture-key")).cycle() == 0
    assert purged
    assert sent == [], "the transport sent a body after purge completed"


def test_provider_retry_rechecks_purge_before_each_transmission(
    captured: tuple[Store, MemoryServer, str, EmbeddingJob],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _, cid, _ = captured
    other = MemoryServer(store=Store.open(store.db.path), hmac_env_key=b"fixture" * 8)
    sent: list[bytes] = []
    purged = False

    class Socket:
        def settimeout(self, seconds: float) -> None:
            pass

    class Connection:
        sock = Socket()

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def connect(self) -> None:
            pass

        def request(self, method: str, path: str, *, body: bytes, headers: Any) -> None:
            sent.append(body)

        def close(self) -> None:
            pass

    class Response:
        status, reason, length = 503, "fixture unavailable", 0

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.fp: Any = io.BytesIO()

        def begin(self) -> None:
            pass

        def read1(self, size: int) -> bytes:
            return b""

        def close(self) -> None:
            self.fp.close()

    def retry_wait(seconds: float) -> None:
        nonlocal purged
        if not purged:
            purged = True
            _purge(other, cid)

    monkeypatch.setattr(embedding.http.client, "HTTPSConnection", Connection)
    monkeypatch.setattr(embedding.http.client, "HTTPResponse", Response)
    monkeypatch.setattr(embedding.time, "sleep", retry_wait)
    assert EmbeddingWorker(store, EmbeddingClient(api_key="fixture-key")).cycle() == 0
    assert purged
    assert len(sent) == 1, "the transport retried private text after purge completed"
    assert _job_count(store) == 0


def test_failed_batch_cannot_resend_a_purged_item(
    captured: tuple[Store, MemoryServer, str, EmbeddingJob],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _, cid, _ = captured
    other = MemoryServer(store=Store.open(store.db.path), hmac_env_key=b"fixture" * 8)
    sent: list[str] = []

    def record(text: str) -> list[float]:
        sent.append(text)
        return VECTOR

    client = EmbeddingClient(custom_hook=record)

    def fail_batch(*args: Any, **kwargs: Any) -> Any:
        _purge(other, cid)
        raise EmbeddingError("fixture batch failure")

    monkeypatch.setattr(client, "embed_documents", fail_batch)
    assert EmbeddingWorker(store, client).cycle() == 0
    assert sent == []


def test_delayed_completion_cannot_restore_a_purged_vector(
    captured: tuple[Store, MemoryServer, str, EmbeddingJob],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _, cid, _ = captured
    other = MemoryServer(store=Store.open(store.db.path), hmac_env_key=b"fixture" * 8)
    complete = store.embeddings.complete_embedding

    def delayed_complete(
        job: EmbeddingJob, vector: list[float], space: str | None = None
    ) -> bool:
        _purge(other, cid)
        return complete(job, vector, space)

    monkeypatch.setattr(store.embeddings, "complete_embedding", delayed_complete)
    assert (
        EmbeddingWorker(store, EmbeddingClient(custom_hook=lambda text: VECTOR)).cycle()
        == 0
    )
    with store.db.connection() as conn:
        assert (
            conn.execute("SELECT count(*) FROM capsule_embeddings").fetchone()[0] == 0
        )
