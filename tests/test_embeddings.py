"""Test battery for embedding write path, space transitions, worker, telemetry, and CLI parity (sub-08)."""

from __future__ import annotations

import json
from array import array
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from storefile import default_store_path

from nmemory.capsule import (
    AuthorityClass,
    Capsule,
    Freshness,
    Provenance,
    Scope,
    sha256_hex,
)
from nmemory.database import Database, now_us, vector_blob
from nmemory.embedding import (
    EmbeddingClient,
    l2_normalize,
)
from nmemory.embedding_index import EmbeddingWorker
from nmemory.ingest import IngestDefaults, IngestRequest, ingest
from nmemory.queries.embedding import embedding_status
from nmemory.repositories.embeddings import EmbeddingJob
from nmemory.retrieve import RetrieveParams, retrieve
from nmemory.space import DIMENSION, SpaceFingerprint
from nmemory.store import Store

TEST_DB = ""


def job_for_capsule(store: Store, capsule_id: str) -> EmbeddingJob | None:
    with store.db.connection() as handle, handle.cursor() as cur:
        row = cur.execute(
            "SELECT capsule_id, index_text, text_fingerprint, target_space"
            " FROM embedding_jobs WHERE capsule_id = ?",
            (capsule_id,),
        ).fetchone()
    return None if row is None else EmbeddingJob(*map(str, row))


@pytest.fixture(autouse=True)
def _store_file() -> None:
    """Each test's own store, at the path a child process resolves by itself."""
    global TEST_DB
    TEST_DB = str(default_store_path())


NOW = datetime(2026, 8, 23, 12, 0, 0, tzinfo=UTC)


def unit_vector(index: int) -> list[float]:
    v = [0.0] * DIMENSION
    v[index % DIMENSION] = 1.0
    return v


def mock_hook(text: str) -> list[float]:
    v = [0.0] * DIMENSION
    for i, ch in enumerate(text.encode("utf-8")[:DIMENSION]):
        v[i] = float(ch)
    return l2_normalize(v)


def test_index_text_labels_the_headline_because_openai_has_no_title_slot() -> None:
    """Index text prefixes headline: <headline> and content: <content>."""
    store = Store.open_disposable_test_store()
    capsule = Capsule(
        content="first line\nbody line",
        provenance=Provenance(
            source="test",
            anchor="private-anchor:99",
            source_hash="hash-private-anchor:99",
        ),
        confidence=0.9,
        freshness=Freshness(valid_from=NOW),
        scope=Scope(project_id="nott"),
        authority_class=AuthorityClass.OBSERVED_FACT,
        instruction_taint=False,
    )
    store.append(capsule, NOW)

    # The index text as the store queues it for the embedding worker at append.
    job = job_for_capsule(store, "cap-1")
    assert job is not None
    cid, index_text, fp = job.capsule_id, job.index_text, job.text_fingerprint
    assert cid == "cap-1"
    assert index_text == "headline: first line…\ncontent: first line\nbody line"
    assert fp == sha256_hex(index_text.encode("utf-8"))
    assert "private-anchor" not in index_text


def test_space_names_model_dimension_and_prompt_and_only_exact_is_current() -> None:
    """SpaceFingerprint names provider, model, dimension, prompt_version."""
    current = SpaceFingerprint.current()
    assert current.as_str() == "gemini:gemini-embedding-2:3072:nmemory-retrieval-v1"
    assert SpaceFingerprint.current().as_str() == (
        "gemini:gemini-embedding-2:3072:nmemory-retrieval-v1"
    )
    # Each axis of the name continues to be a falsifier: model, dimension,
    # prompt_version and provider. The default changed; the requirement for EXACT
    # equality did not.
    assert SpaceFingerprint.current().as_str() != (
        "gemini:gemini-embedding-001:3072:nmemory-retrieval-v1"
    )
    assert SpaceFingerprint.current().as_str() != (
        "gemini:gemini-embedding-2:1536:nmemory-retrieval-v1"
    )
    assert SpaceFingerprint.current().as_str() != (
        "gemini:gemini-embedding-2:3072:nmemory-retrieval-v0"
    )
    assert SpaceFingerprint.current().as_str() != (
        "openai:gemini-embedding-2:3072:nmemory-retrieval-v1"
    )
    assert SpaceFingerprint.current().as_str() != (
        "openai:text-embedding-3-large:3072:nmemory-retrieval-v1"
    )


def test_embedding_jobs_producer_on_ingest_and_forget_cleanup() -> None:
    """Capture queues text. Purge removes the job in the same transaction."""
    store = Store.open(TEST_DB)
    pg = store.db
    assert pg is not None

    req = IngestRequest(
        content="First ingested memory\nBody content.",
        source="session",
        anchor="src/lib.rs:10",
        project_id="nott",
    )
    outcome = ingest(store, req, defaults=IngestDefaults(project_id="nott"), now=NOW)
    assert outcome.id == "cap-1"

    status = embedding_status(pg)
    assert status.pending == 1

    job = job_for_capsule(store, "cap-1")
    assert job is not None
    assert (
        job.index_text
        == "headline: First ingested memory…\ncontent: First ingested memory\nBody content."
    )

    store.capsules.forget_capsule(
        "cap-1",
        mode="purged",
        reason="test cleanup",
        hmac_key=b"test-key-32-bytes-long-for-hmac!!",
        now=NOW,
    )
    assert job_for_capsule(store, "cap-1") is None
    assert embedding_status(pg).pending == 0


def test_status_counts_a_job_past_the_claim_cap_as_exhausted_not_pending() -> None:
    """A job no claim will ever take again is not pending: on 2026-10-07 the
    status said "pending 1148" for hours while all 1148 were at the cap."""
    store = Store.open(TEST_DB)
    pg = store.db
    ingest(
        store,
        IngestRequest(
            content="Capped", source="session", anchor="a.py:1", project_id="nott"
        ),
        defaults=IngestDefaults(project_id="nott"),
        now=NOW,
    )
    with pg.writing() as conn, conn.cursor() as cur:
        cur.execute("UPDATE embedding_jobs SET attempts = 19")
    assert embedding_status(pg).pending == 1

    last = store.embeddings.claim_embedding()
    assert last is not None
    store.embeddings.fail_embedding(last, "transport")
    with pg.writing() as conn, conn.cursor() as cur:
        cur.execute("UPDATE embedding_jobs SET available_at = ?", (now_us(),))
    assert store.embeddings.claim_embedding() is None

    status = embedding_status(pg)
    assert status.pending == 0
    assert status.exhausted == 1
    assert status.to_dict()["exhausted"] == 1


def test_embedding_job_claim_complete_fail_retry_cycle() -> None:
    """Embedding jobs support claim, fail retry delay, attempt limit, and atomic completion."""
    store = Store.open(TEST_DB)
    pg = store.db
    space = SpaceFingerprint.current()
    store.append(
        Capsule(
            content="One",
            provenance=Provenance(
                source="session", anchor="one.py:1", source_hash=sha256_hex(b"one")
            ),
            confidence=0.9,
            freshness=Freshness(valid_from=NOW),
            scope=Scope(project_id="nott"),
            authority_class=AuthorityClass.USER_STATED,
            instruction_taint=False,
        ),
        NOW,
    )

    input_jobs = [
        (
            "cap-1",
            "headline: One\ncontent: One",
            sha256_hex(b"headline: One\ncontent: One"),
        )
    ]
    # `append` already queued exactly this text: re-queueing it changes nothing.
    assert store.embeddings.queue_embeddings(input_jobs, space.as_str()) == 0
    assert embedding_status(pg).pending == 1

    # Claim
    first = store.embeddings.claim_embedding()
    assert first is not None
    assert first.capsule_id == "cap-1"
    assert store.embeddings.claim_embedding() is None

    # Fail delays availability
    store.embeddings.fail_embedding(first, "transport")
    assert store.embeddings.claim_embedding() is None

    # Make available again and claim
    with pg.writing() as conn, conn.cursor() as cur:
        cur.execute("UPDATE embedding_jobs SET available_at = ?", (now_us(),))
    retried = store.embeddings.claim_embedding()
    assert retried is not None

    # Completing with stale text fingerprint returns False

    stale_job = EmbeddingJob(
        capsule_id="cap-1",
        index_text="stale",
        text_fingerprint=sha256_hex(b"stale"),
        target_space=space.as_str(),
    )
    assert not store.embeddings.complete_embedding(stale_job, unit_vector(1))

    # Completing with valid job succeeds and creates vector
    assert store.embeddings.complete_embedding(retried, unit_vector(1))
    assert store.embeddings.claim_embedding() is None
    assert embedding_status(pg).pending == 0
    assert embedding_status(pg).active_vectors == 1

    nearest = store.embeddings.search_cosine(
        unit_vector(1), 10, space_fingerprint=space.as_str()
    )
    assert len(nearest) == 1
    assert nearest[0][0] == "cap-1"


def test_embedding_worker_drains_jobs_and_enables_vector_search() -> None:
    """EmbeddingWorker drains queued jobs and retrieve() finds results via vector lane with usage stats."""
    store = Store.open(TEST_DB)
    pg = store.db
    assert pg is not None

    client = EmbeddingClient(custom_hook=mock_hook)

    # Ingest 3 capsules
    for i in range(1, 4):
        store.append(
            Capsule(
                content=f"Memory item {i} about architecture and algorithms",
                provenance=Provenance(
                    source="session",
                    anchor=f"file{i}.py:1",
                    source_hash=sha256_hex(f"content-{i}".encode()),
                ),
                confidence=0.9,
                freshness=Freshness(valid_from=NOW),
                scope=Scope(project_id="nott"),
                authority_class=AuthorityClass.USER_STATED,
                instruction_taint=False,
            ),
            NOW,
        )

    assert embedding_status(pg).pending == 3

    worker = EmbeddingWorker(store, client)
    drained = worker.drain()
    assert drained == 3
    assert embedding_status(pg).pending == 0
    assert embedding_status(pg).active_vectors == 3

    # Recall with vector search lane
    res = retrieve(
        store,
        RetrieveParams(query="Memory item 2 about architecture", project_id="nott"),
        embeddings=client,
        now=NOW,
    )
    assert res["outcome"] == "grounded"
    assert len(res["evidence"]) > 0
    assert "embedding_usage" in res
    usage = res["embedding_usage"]
    assert usage["openai_requests"] >= 1
    assert usage["openai_prompt_tokens"] > 0


def test_embedding_worker_keyless_leaves_jobs_for_configured_replica() -> None:
    """A keyless worker leaves shared jobs ready for a configured replica to complete."""
    store = Store.open(TEST_DB)
    pg = store.db
    assert pg is not None

    client = EmbeddingClient(api_key="")
    assert not client.is_configured()

    # Ingest 1 capsule to queue an embedding job
    store.append(
        Capsule(
            content="Keyless worker test content about memory substrate",
            provenance=Provenance(
                source="session",
                anchor="keyless.py:1",
                source_hash=sha256_hex(b"keyless-content"),
            ),
            confidence=0.9,
            freshness=Freshness(valid_from=NOW),
            scope=Scope(project_id="nott"),
            authority_class=AuthorityClass.USER_STATED,
            instruction_taint=False,
        ),
        NOW,
    )

    assert embedding_status(pg).pending == 1

    worker = EmbeddingWorker(store, client)
    drained = worker.drain()
    assert drained == 0

    with pg.writing() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT attempts, last_error_class, available_at <= ? "
            "FROM embedding_jobs WHERE capsule_id = 'cap-1'",
            (now_us(),),
        )
        assert [tuple(r) for r in cur.fetchall()] == [(0, None, 1)]

    replica = Store.open(TEST_DB)
    configured = EmbeddingWorker(
        replica, EmbeddingClient(custom_hook=lambda _text: unit_vector(1))
    )
    with replica.db.embedding_worker_owner() as owner:
        assert owner
        assert configured.drain() == 1
    assert embedding_status(replica.db).pending == 0
    with replica.db.writing() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT embedding FROM capsule_embeddings WHERE capsule_id = 'cap-1'"
        )
        row = cur.fetchone()
        assert row is not None
        assert list(array("f", row[0])) == unit_vector(1)


def test_space_transition_full_reindex_and_stale_vector_cleanup() -> None:
    """A changed space fingerprint triggers reindexing and deletes old space vectors upon completion."""
    store = Store.open(TEST_DB)
    pg = store.db
    assert pg is not None

    current = SpaceFingerprint.current()
    stale_space = "openai:text-embedding-3-large:3072:nmemory-retrieval-v0"
    store.append(
        Capsule(
            content="content",
            provenance=Provenance(
                source="session", anchor="c.py:1", source_hash=sha256_hex(b"content")
            ),
            confidence=0.9,
            freshness=Freshness(valid_from=NOW),
            scope=Scope(project_id="nott"),
            authority_class=AuthorityClass.USER_STATED,
            instruction_taint=False,
        ),
        NOW,
    )

    # Seed store in stale space
    with pg.writing() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE embedding_state SET active_space = ?, target_space = NULL, phase = 'ready' WHERE singleton",
            (stale_space,),
        )
        cur.execute(
            """
            INSERT INTO capsule_embeddings
                (project_id, capsule_id, embedding, space_fingerprint,
                 text_fingerprint, updated_at)
            VALUES ('nott', 'cap-superseded', ?, ?, ?, ?)
            """,
            (
                vector_blob(unit_vector(3)),
                stale_space,
                sha256_hex(b"fp-superseded"),
                now_us(),
            ),
        )

    # Transition started
    targeted = store.embeddings.begin_current_space_transition_if_needed()
    assert targeted is not None
    assert targeted.as_str() == current.as_str()

    status = embedding_status(pg)
    assert status.phase == "reindexing"
    assert status.target_space == current.as_str()

    # Second call is a no-op
    assert store.embeddings.begin_current_space_transition_if_needed() is None

    # Re-queue and complete cap-1 in new space
    index_text = "headline: content\ncontent: content"
    input_jobs = [("cap-1", index_text, sha256_hex(index_text.encode()))]
    store.embeddings.queue_embeddings(input_jobs, current.as_str())
    assert not store.embeddings.activate_target_if_complete()

    job = store.embeddings.claim_embedding()
    assert job is not None
    assert store.embeddings.complete_embedding(job, unit_vector(1))

    # Activate target
    assert store.embeddings.activate_target_if_complete()

    # Verify old vectors pruned and active space updated
    final_status = embedding_status(pg)
    assert final_status.phase == "ready"
    assert final_status.active_space == current.as_str()
    assert final_status.target_space is None

    with pg.writing() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM capsule_embeddings WHERE space_fingerprint <> ?",
            (current.as_str(),),
        )
        row = cur.fetchone()
        assert row is not None and int(row[0]) == 0


def test_cli_embeddings_status(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI embeddings status executes and formats properly."""
    monkeypatch.setenv("NOTT_DATABASE_DIR", str(Path(TEST_DB).parent))
    from nmemory.cli import main

    # Status plain text
    monkeypatch.setattr("sys.argv", ["nmemory", "embedding", "show"])
    main()
    out = capsys.readouterr().out
    assert "active vectors" in out
    assert "pending" in out

    # Status JSON
    monkeypatch.setattr("sys.argv", ["nmemory", "embedding", "show", "--json"])
    main()
    out_json = capsys.readouterr().out
    data = json.loads(out_json)
    assert data["active_space"] == SpaceFingerprint.current().as_str()
    assert data["phase"] == "reindexing" or data["phase"] == "ready"


def _vector_ids(pg: Database) -> set[str]:
    with pg.writing() as conn, conn.cursor() as cur:
        cur.execute("SELECT capsule_id FROM capsule_embeddings")
        return {str(r[0]) for r in cur.fetchall()}


def test_worker_cycle_prunes_embeddings_of_forgotten_capsules() -> None:
    """Purge and the next worker cycle preserve the surviving capsule's vector."""
    store = Store.open(TEST_DB)
    pg = store.db
    assert pg is not None

    defaults = IngestDefaults(project_id="nott")
    keep = ingest(
        store,
        IngestRequest(
            content="Capsule that stays alive\nBody.",
            source="session",
            anchor="src/lib.rs:10",
            project_id="nott",
        ),
        defaults=defaults,
        now=NOW,
    )
    drop = ingest(
        store,
        IngestRequest(
            content="Capsule that is going to be forgotten\nBody.",
            source="session",
            anchor="src/lib.rs:20",
            project_id="nott",
        ),
        defaults=defaults,
        now=NOW,
    )

    for index in (1, 2):
        job = store.embeddings.claim_embedding()
        assert job is not None
        assert store.embeddings.complete_embedding(job, unit_vector(index))
    assert _vector_ids(pg) == {keep.id, drop.id}

    store.capsules.forget_capsule(
        drop.id,
        mode="purged",
        reason="ordem do owner: purga de contexto",
        hmac_key=b"test-key-32-bytes-long-for-hmac!!",
        now=NOW,
    )
    assert drop.id not in _vector_ids(pg)

    EmbeddingWorker(store, EmbeddingClient()).cycle()

    survivors = _vector_ids(pg)
    assert drop.id not in survivors, (
        "the worker cycle left the forgotten capsule's vector standing"
    )
    assert keep.id in survivors, (
        "the prune ate the vector of a LIVE capsule — it is not fenced to live_ids"
    )


def test_worker_cycle_skips_the_live_sync_when_the_generation_is_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = Store.open(TEST_DB)
    pg = store.db
    assert pg is not None

    calls = 0
    orig_list = store.capsules.list

    def spy_list(filt: Any = None, *args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        if filt is None and not kwargs:
            calls += 1
        return orig_list(filt, *args, **kwargs)

    monkeypatch.setattr(store.capsules, "list", spy_list)

    worker = EmbeddingWorker(store, EmbeddingClient())
    worker.cycle()
    worker.cycle()
    assert calls == 1

    defaults = IngestDefaults(project_id="nott")
    ingest(
        store,
        IngestRequest(
            content="Capsula que move a geracao\nCorpo.",
            source="session",
            anchor="src/lib.rs:10",
            project_id="nott",
        ),
        defaults=defaults,
        now=NOW,
    )
    worker.cycle()
    assert calls == 2


def test_worker_cycle_refuses_to_prune_on_an_empty_live_read() -> None:
    """A partial capsule list cannot remove vectors of live database rows."""
    store = Store.open(TEST_DB)
    pg = store.db
    assert pg is not None

    ingest(
        store,
        IngestRequest(
            content="Capsula com vetor\nCorpo.",
            source="session",
            anchor="src/lib.rs:10",
            project_id="nott",
        ),
        defaults=IngestDefaults(project_id="nott"),
        now=NOW,
    )
    job = store.embeddings.claim_embedding()
    assert job is not None
    assert store.embeddings.complete_embedding(job, unit_vector(1))
    assert _vector_ids(pg) == {"cap-1"}

    # The read lies: returns empty with the store full. It is the exact form of
    # a paginated, fenced, or broken `list()`.
    store.capsules.list = lambda *a, **k: []  # type: ignore[method-assign]
    EmbeddingWorker(store, EmbeddingClient()).cycle()

    assert _vector_ids(pg) == {"cap-1"}, (
        "an empty read deleted the whole index — the cycle guard fell"
    )
