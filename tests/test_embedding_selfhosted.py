"""A self-hosted OpenAI-protocol embedder, and a provider outage that fails fast and shows.

RED before product (2026-10-03): the OpenAI URL and model were hardcoded, so no
self-hosted endpoint could be reached; the Gemini spending-cap 429 was retried five
times (~15.5 s per recall); and the worker logged the failure at debug while
`embedding show` kept saying `ready`.
"""

from __future__ import annotations

import http.client
import json
import logging
import math
import selectors
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from child_env import child_env
from stdio_peer import INITIALIZED, complete
from storefile import default_store_path

from nmemory import embedding as embedding_module
from nmemory.capsule import (
    AuthorityClass,
    Capsule,
    Freshness,
    Provenance,
    Scope,
    sha256_hex,
)
from nmemory.context import execute_context
from nmemory.database import Database
from nmemory.database import now_us as database_now_us
from nmemory.embedding import QUERY_TIMEOUT_S, EmbeddingClient
from nmemory.embedding_index import EmbeddingWorker
from nmemory.queries.embedding import embedding_status
from nmemory.retrieve import RetrieveParams, retrieve
from nmemory.server import MemoryServer
from nmemory.space import DIMENSION, EmbeddingError, SpaceFingerprint
from nmemory.store import Store

MODEL = "qwen3-embedding:0.6b"
NOW = datetime(2026, 10, 3, 12, 0, 0, tzinfo=UTC)
NATIVE = 1024
#: The body Gemini returned from 2026-10-01 23:00Z, byte for byte.
SPENDING_CAP = json.dumps(
    {
        "error": {
            "code": 429,
            "message": "Your project has exceeded its monthly spending cap. Please go"
            " to AI Studio at https://ai.studio/spend to manage your project spend"
            " cap.",
            "status": "RESOURCE_EXHAUSTED",
        }
    }
).encode()


class Stub:
    """An OpenAI-protocol embeddings server on loopback that records each request."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, str], dict[str, Any]]] = []
        self.status = 200
        self.body: bytes | None = None
        #: Replies served first, in order, before `status`/`body` take over.
        self.replies: list[tuple[int, bytes]] = []
        self.reason: str | None = None
        #: A Content-Length to advertise in place of the body's own.
        self.advertised: int | None = None
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                payload = json.loads(raw)
                stub.requests.append((self.path, dict(self.headers), payload))
                status, body = stub.status, stub.body
                if stub.replies:
                    status, body = stub.replies.pop(0)
                if body is None:
                    body = json.dumps(
                        {
                            "object": "list",
                            "data": [
                                {"index": i, "embedding": [float(i + 1)] * NATIVE}
                                for i in range(len(payload["input"]))
                            ],
                            "model": payload["model"],
                        }
                    ).encode()
                self.send_response(status, stub.reason)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(stub.advertised or len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: object) -> None:
                return None

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1/embeddings"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def stub() -> Iterator[Stub]:
    s = Stub()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def selfhosted(stub: Stub, monkeypatch: pytest.MonkeyPatch) -> Stub:
    monkeypatch.setenv("NOTT_NMEMORY_EMBED_PROVIDER", "openai-compatible")
    monkeypatch.setenv("NOTT_NMEMORY_EMBED_ENDPOINT", stub.url)
    monkeypatch.setenv("NOTT_NMEMORY_EMBED_MODEL", MODEL)
    monkeypatch.setenv("NOTT_NMEMORY_EMBED_DIMENSION", str(NATIVE))
    monkeypatch.setenv("OPENAI_API_KEY", SECRET)
    return stub


def test_selfhosted_posts_to_the_endpoint_pads_and_names_the_model(
    selfhosted: Stub,
) -> None:
    space = SpaceFingerprint.current()
    assert space.as_str() == f"openai-compatible:{MODEL}:{NATIVE}:nmemory-retrieval-v1"

    client = EmbeddingClient()
    assert client.is_configured()
    vectors = client.embed_documents(["first capsule", "second capsule"])

    [(path, headers, payload)] = selfhosted.requests
    assert path == "/v1/embeddings"
    assert payload == {"model": MODEL, "input": ["first capsule", "second capsule"]}
    # A self-hosted endpoint needs no key, and the OpenAI key never leaves for it.
    assert "Authorization" not in headers
    assert [len(v) for v in vectors] == [DIMENSION, DIMENSION]
    for vec in vectors:
        assert vec[NATIVE:] == [0.0] * (DIMENSION - NATIVE)
        assert math.isclose(sum(x * x for x in vec), 1.0)
        assert math.isclose(vec[0], 1 / math.sqrt(NATIVE))


def test_selfhosted_without_endpoint_or_model_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NOTT_NMEMORY_EMBED_PROVIDER", "openai-compatible")
    monkeypatch.setenv("NOTT_NMEMORY_EMBED_DIMENSION", str(NATIVE))
    monkeypatch.setenv(
        "NOTT_NMEMORY_EMBED_ENDPOINT", "http://127.0.0.1:9/v1/embeddings"
    )
    monkeypatch.delenv("NOTT_NMEMORY_EMBED_MODEL", raising=False)
    with pytest.raises(EmbeddingError, match="NOTT_NMEMORY_EMBED_MODEL"):
        SpaceFingerprint.current()
    monkeypatch.setenv("NOTT_NMEMORY_EMBED_MODEL", MODEL)
    monkeypatch.delenv("NOTT_NMEMORY_EMBED_ENDPOINT")
    # Refused as a vector lane, never as a server: recall goes lexical.
    with pytest.raises(EmbeddingError, match="NOTT_NMEMORY_EMBED_ENDPOINT"):
        EmbeddingClient().embed_query("anything")


def test_spending_cap_429_is_not_retried(
    stub: Stub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NOTT_NMEMORY_EMBED_PROVIDER", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(
        "nmemory.embedding.GEMINI_ENDPOINT", stub.url.removesuffix("/v1/embeddings")
    )
    stub.status, stub.body = 429, SPENDING_CAP

    started = time.monotonic()
    with pytest.raises(EmbeddingError, match="spending cap"):
        EmbeddingClient().embed_query("what broke recall")
    elapsed = time.monotonic() - started

    assert len(stub.requests) == 1
    assert elapsed < 1.0


def _ingest(store: Store, n: int) -> None:
    now = Freshness(valid_from=NOW)
    for i in range(n):
        store.append(
            Capsule(
                content=f"Memory item {i} about the embedder outage",
                provenance=Provenance(
                    source="session",
                    anchor=f"file{i}.py:1",
                    source_hash=sha256_hex(f"content-{i}".encode()),
                ),
                confidence=0.9,
                freshness=now,
                scope=Scope(project_id="nott"),
                authority_class=AuthorityClass.USER_STATED,
                instruction_taint=False,
            ),
            now.valid_from,
        )


def test_fatal_provider_error_shows_in_the_state_and_clears_on_success(
    selfhosted: Stub, caplog: pytest.LogCaptureFixture
) -> None:
    store = Store.open(str(default_store_path()))
    _ingest(store, 3)
    selfhosted.status, selfhosted.body = 429, SPENDING_CAP

    worker = EmbeddingWorker(store, EmbeddingClient())
    with caplog.at_level(logging.WARNING, logger="nmemory.embedding_index"):
        assert worker.cycle() == 0

    status = embedding_status(store.db)
    assert status.phase == "lexical_only"
    assert status.last_error == "quota_exhausted"
    # One call for the whole batch: a fatal error is not retried item by item.
    assert len(selfhosted.requests) == 1
    assert any("spending cap" in r.getMessage() for r in caplog.records)

    selfhosted.status, selfhosted.body = 200, None
    with store.db.writing() as handle, handle.cursor() as cur:
        cur.execute("UPDATE embedding_jobs SET available_at = 0")
    assert worker.drain() == 3
    status = embedding_status(store.db)
    assert status.phase == "ready"
    assert status.last_error is None
    assert status.to_dict()["last_error"] is None


#: Gemini's transient per-minute limit: the same status, but waiting cures it.
PER_MINUTE = json.dumps(
    {
        "error": {
            "code": 429,
            "message": "Resource has been exhausted (e.g. check quota).",
            "status": "RESOURCE_EXHAUSTED",
        }
    }
).encode()


def test_per_minute_429_is_retried_and_then_succeeds(
    stub: Stub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NOTT_NMEMORY_EMBED_PROVIDER", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(
        "nmemory.embedding.GEMINI_ENDPOINT", stub.url.removesuffix("/v1/embeddings")
    )
    monkeypatch.setattr("nmemory.embedding.time.sleep", lambda _s: None)
    stub.replies = [(429, PER_MINUTE)]
    stub.body = json.dumps({"embeddings": [{"values": [0.5] * DIMENSION}]}).encode()

    # Indexing keeps its retries (a recall's query makes one attempt).
    vec = EmbeddingClient().embed_document("what broke recall")

    assert len(stub.requests) == 2
    assert len(vec) == DIMENSION


def test_a_rate_limited_batch_is_not_retried_item_by_item(
    selfhosted: Stub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RED before product (2026-10-07): a batch that stayed rate limited through its
    retries fell back to one request per job, each retried again: 3 jobs cost 20
    calls here, and a full cycle of 192 jobs up to 965."""
    monkeypatch.setattr("nmemory.embedding.time.sleep", lambda _s: None)
    store = Store.open(str(default_store_path()))
    _ingest(store, 3)
    selfhosted.status, selfhosted.body = 429, PER_MINUTE

    assert EmbeddingWorker(store, EmbeddingClient()).cycle() == 0

    # One batch with its own backoff, and not one request for a single job.
    assert len(selfhosted.requests) == embedding_module.RETRY_ATTEMPTS
    assert all(len(p["input"]) == 3 for _, _, p in selfhosted.requests)
    with store.db.connection() as handle, handle.cursor() as cur:
        rows = cur.execute("SELECT last_error_class FROM embedding_jobs").fetchall()
    assert [r[0] for r in rows] == ["rate_limited"] * 3
    # Waiting cures a rate limit: the vector lane is not switched off for it.
    assert embedding_status(store.db).phase != "lexical_only"


@pytest.mark.parametrize(
    "vector",
    [[], [0.5] * 768, [0.0] * NATIVE],
    ids=["empty", "short", "zero"],
)
def test_a_vector_of_the_wrong_size_or_no_direction_is_refused(
    selfhosted: Stub, vector: list[float]
) -> None:
    selfhosted.body = json.dumps(
        {"data": [{"index": 0, "embedding": vector}], "model": MODEL}
    ).encode()
    with pytest.raises(EmbeddingError):
        EmbeddingClient().embed_document("a capsule")


def test_selfhosted_without_a_native_dimension_is_refused(
    selfhosted: Stub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NOTT_NMEMORY_EMBED_DIMENSION")
    with pytest.raises(EmbeddingError, match="NOTT_NMEMORY_EMBED_DIMENSION"):
        SpaceFingerprint.current()


def test_a_schemeless_endpoint_turns_the_vector_lane_off(
    selfhosted: Stub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOTT_NMEMORY_EMBED_ENDPOINT", "100.64.10.7:11434/v1/embeddings")
    # Refused as a vector lane, never as a server: recall goes lexical.
    with pytest.raises(EmbeddingError, match="NOTT_NMEMORY_EMBED_ENDPOINT"):
        EmbeddingClient().embed_query("anything")


def test_recall_goes_lexical_within_the_query_bound_when_the_host_never_answers(
    selfhosted: Stub,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = Store.open(str(default_store_path()))
    _ingest(store, 3)
    EmbeddingWorker(store, EmbeddingClient()).drain()  # whole: recall may ask
    # Accepts the connection (the kernel completes the handshake) and never answers.
    silent = socket.socket()
    silent.bind(("127.0.0.1", 0))
    silent.listen()
    try:
        monkeypatch.setenv(
            "NOTT_NMEMORY_EMBED_ENDPOINT",
            f"http://127.0.0.1:{silent.getsockname()[1]}/v1/embeddings",
        )
        started = time.monotonic()
        with caplog.at_level(logging.WARNING, logger="nmemory.retrieve"):
            res = retrieve(
                store,
                RetrieveParams(query="embedder outage", project_id="nott"),
                embeddings=EmbeddingClient(),
                now=NOW,
            )
        elapsed = time.monotonic() - started
    finally:
        silent.close()

    assert res["outcome"] == "grounded"
    assert res["degraded"] is True
    assert any("vector search lane failed" in r.getMessage() for r in caplog.records)
    assert elapsed < QUERY_TIMEOUT_S + 2


def test_the_reported_error_is_the_current_state_not_an_old_job(
    selfhosted: Stub,
) -> None:
    store = Store.open(str(default_store_path()))
    _ingest(store, 3)
    selfhosted.status, selfhosted.body = 429, SPENDING_CAP
    worker = EmbeddingWorker(store, EmbeddingClient())
    worker.cycle()
    assert embedding_status(store.db).last_error == "quota_exhausted"

    # The provider recovers; one job is retried now, two still wait out their delay.
    selfhosted.status, selfhosted.body = 200, None
    with store.db.writing() as handle, handle.cursor() as cur:
        first = cur.execute("SELECT min(capsule_id) FROM embedding_jobs").fetchone()
        cur.execute(
            "UPDATE embedding_jobs SET available_at = 0 WHERE capsule_id = ?", first
        )
    assert worker.cycle() == 1

    status = embedding_status(store.db)
    assert status.phase != "lexical_only"
    assert status.pending == 2
    assert status.last_error is None


def _vector_spaces(store: Store) -> list[str]:
    with store.db.connection() as handle, handle.cursor() as cur:
        rows = cur.execute(
            "SELECT space_fingerprint FROM capsule_embeddings"
        ).fetchall()
    return [str(r[0]) for r in rows]


def test_a_worker_never_stores_a_vector_into_another_clients_space(
    selfhosted: Stub,
) -> None:
    store = Store.open(str(default_store_path()))
    _ingest(store, 2)
    mine = SpaceFingerprint.current().as_str()
    gemini = SpaceFingerprint.default_gemini()
    other = EmbeddingClient(custom_hook=lambda _t: [1.0], space_fingerprint=gemini)

    # A server still on Gemini during the install claims nothing of the new space.
    assert EmbeddingWorker(store, other).cycle() == 0
    assert _vector_spaces(store) == []
    assert embedding_status(store.db).pending == 2

    # And a job claimed under one space is not completed by another producer.
    job = store.embeddings.claim_embedding(mine)
    assert job is not None
    assert not store.embeddings.complete_embedding(
        job, [1.0] * DIMENSION, gemini.as_str()
    )
    assert _vector_spaces(store) == []

    with store.db.writing() as handle, handle.cursor() as cur:
        cur.execute("UPDATE embedding_jobs SET available_at = 0")
    assert EmbeddingWorker(store, EmbeddingClient()).drain() == 2
    assert _vector_spaces(store) == [mine, mine]


@pytest.mark.parametrize(
    "body",
    [
        b"\xff\xfe not utf-8",
        json.dumps({"data": [{"index": 0, "embedding": ["x"] * NATIVE}]}).encode(),
        json.dumps({"data": [{"index": 0, "embedding": [None] * NATIVE}]}).encode(),
        json.dumps(
            {"data": [{"index": 0, "embedding": [0.5] * NATIVE}], "usage": None}
        ).encode(),
        json.dumps(
            {"data": [{"index": 0, "embedding": [float("nan")] * NATIVE}]}
        ).encode(),
        json.dumps(
            {"data": [{"index": 0, "embedding": [float("inf")] * NATIVE}]}
        ).encode(),
        b"[1, 2, 3]",
    ],
    ids=["utf8", "non-numeric", "null-component", "usage-null", "nan", "inf", "list"],
)
def test_a_malformed_response_is_an_embedding_error(
    selfhosted: Stub, body: bytes
) -> None:
    selfhosted.body = body
    with pytest.raises(EmbeddingError):
        EmbeddingClient().embed_document("a capsule")


def test_a_query_only_outage_shows_in_the_state_and_clears(
    selfhosted: Stub, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The cooldown after a failed query has its own test; here it ends at once.
    monkeypatch.setattr("nmemory.retrieve.QUERY_COOLDOWN_S", 0.0)
    store = Store.open(str(default_store_path()))
    _ingest(store, 2)
    client = EmbeddingClient()
    EmbeddingWorker(store, client).drain()
    assert embedding_status(store.db).phase == "ready"
    assert embedding_status(store.db).pending == 0

    selfhosted.status, selfhosted.body = 400, b"model not loaded"
    res = retrieve(
        store,
        RetrieveParams(query="embedder outage", project_id="nott"),
        embeddings=client,
        now=NOW,
    )
    assert res["degraded"] is True
    status = embedding_status(store.db)
    assert status.phase == "lexical_only"
    assert status.last_error == "query_embedding_failed"

    selfhosted.status, selfhosted.body = 200, None
    retrieve(
        store,
        RetrieveParams(query="the embedder is back", project_id="nott"),
        embeddings=client,
        now=NOW,
    )
    status = embedding_status(store.db)
    assert status.phase == "ready"
    assert status.last_error is None


def test_a_provider_error_never_echoes_a_credential(
    stub: Stub, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured, query_key, bearer = "configured-4242", "qk9137", "bt8264"
    monkeypatch.delenv("NOTT_NMEMORY_EMBED_PROVIDER", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", configured)
    monkeypatch.setattr(
        "nmemory.embedding.GEMINI_ENDPOINT", stub.url.removesuffix("/v1/embeddings")
    )
    stub.status = 400
    stub.body = (
        f"invalid {configured} at ?key={query_key} Authorization: Bearer {bearer} "
        + "x" * 2000
    ).encode()

    with pytest.raises(EmbeddingError) as caught:
        EmbeddingClient().embed_document("a capsule")

    message = str(caught.value)
    for secret in (configured, query_key, bearer):
        assert secret not in message
    assert "[redacted]" in message
    assert len(message) < 700


def test_a_bad_embedding_config_degrades_the_real_server_to_lexical(
    selfhosted: Stub,
) -> None:
    path = default_store_path()
    store = Store.open(str(path))
    _ingest(store, 2)
    env = child_env()
    env["NOTT_DATABASE_DIR"] = str(path.parent)
    env["NOTT_NMEMORY_EMBED_ENDPOINT"] = "127.0.0.1:11434/v1/embeddings"
    call = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "nmemory_capsule_retrieve",
            "arguments": {"terms": ["embedder", "outage"], "project_id": "nott"},
        },
    }
    with subprocess.Popen(
        [sys.executable, "-c", "from nmemory import cli; cli.main()", "mcp", "serve"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    ) as proc:
        try:
            assert proc.stdin is not None and proc.stdout is not None
            proc.stdin.write(
                json.dumps(complete({"id": 0, "method": "initialize"})) + "\n"
            )
            proc.stdin.flush()
            proc.stdout.readline()
            proc.stdin.write(json.dumps(INITIALIZED) + "\n")
            proc.stdin.write(json.dumps(call) + "\n")
            proc.stdin.flush()
            with selectors.DefaultSelector() as selector:
                selector.register(proc.stdout, selectors.EVENT_READ)
                assert selector.select(20), "the server never answered"
            line = proc.stdout.readline()
            assert line, proc.poll()
            response = json.loads(line)
            deadline = time.monotonic() + 10
            status = embedding_status(Database(path))
            while status.last_error != "config_error" and time.monotonic() < deadline:
                time.sleep(0.1)
                status = embedding_status(Database(path))
        finally:
            proc.kill()
            proc.wait(timeout=5)

    assert "error" not in response, response
    assert not response["result"].get("isError"), response
    recall = json.loads(response["result"]["content"][0]["text"])
    assert recall["outcome"] == "grounded"
    assert recall["lanes_run"] == ["term"]
    assert status.phase == "lexical_only"
    assert status.last_error == "config_error"


def test_a_missing_dimension_still_ingests_and_constructs(
    selfhosted: Stub, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = default_store_path()  # the store exists before the bad configuration
    monkeypatch.delenv("NOTT_NMEMORY_EMBED_DIMENSION")
    store = Store.open(str(path))
    _ingest(store, 1)
    client = EmbeddingClient()
    assert not client.is_configured()
    with pytest.raises(EmbeddingError, match="NOTT_NMEMORY_EMBED_DIMENSION"):
        client.embed_query("anything")


def test_a_bracketed_endpoint_is_an_embedding_config_error(
    selfhosted: Stub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOTT_NMEMORY_EMBED_ENDPOINT", "http://[::1/v1/embeddings")
    with pytest.raises(EmbeddingError, match="NOTT_NMEMORY_EMBED_ENDPOINT"):
        EmbeddingClient().embed_query("anything")


@pytest.mark.parametrize("where", ["reason", "component"])
def test_every_client_error_is_redacted(
    stub: Stub, monkeypatch: pytest.MonkeyPatch, where: str
) -> None:
    configured = "configured-4242"
    monkeypatch.delenv("NOTT_NMEMORY_EMBED_PROVIDER", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", configured)
    monkeypatch.setattr(
        "nmemory.embedding.GEMINI_ENDPOINT", stub.url.removesuffix("/v1/embeddings")
    )
    if where == "reason":
        stub.status, stub.reason, stub.body = 400, f"bad {configured}", b"{}"
    else:
        stub.body = json.dumps(
            {"embeddings": [{"values": [configured] * DIMENSION}]}
        ).encode()
    with pytest.raises(EmbeddingError) as caught:
        EmbeddingClient().embed_document("a capsule")
    assert configured not in str(caught.value)


@pytest.mark.parametrize(
    "provider,body",
    [
        ("gemini", b'{"embeddings": []}'),
        (
            "openai-compatible",
            (
                b'{"data": [{"index": 0, "embedding": ['
                + b",".join([b"1" + b"0" * 400] * NATIVE)
                + b"]}]}"
            ),
        ),
    ],
    ids=["gemini-empty", "overflow"],
)
def test_cardinality_and_overflow_are_embedding_errors(
    selfhosted: Stub, monkeypatch: pytest.MonkeyPatch, provider: str, body: bytes
) -> None:
    if provider == "gemini":
        monkeypatch.setenv("NOTT_NMEMORY_EMBED_PROVIDER", "gemini")
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        monkeypatch.setattr(
            "nmemory.embedding.GEMINI_ENDPOINT",
            selfhosted.url.removesuffix("/v1/embeddings"),
        )
    selfhosted.body = body
    with pytest.raises(EmbeddingError):
        EmbeddingClient().embed_document("a capsule")


def test_a_retryable_query_failure_goes_lexical_within_the_deadline(
    selfhosted: Stub,
) -> None:
    store = Store.open(str(default_store_path()))
    _ingest(store, 2)
    EmbeddingWorker(store, EmbeddingClient()).drain()  # whole: recall may ask
    selfhosted.requests.clear()
    selfhosted.status, selfhosted.body = 503, b"busy"
    started = time.monotonic()
    res = retrieve(
        store,
        RetrieveParams(query="embedder outage", project_id="nott"),
        embeddings=EmbeddingClient(),
        now=NOW,
    )
    elapsed = time.monotonic() - started
    assert res["degraded"] is True
    assert len(selfhosted.requests) == 1
    assert elapsed < QUERY_TIMEOUT_S


@pytest.mark.parametrize("many", [False, True], ids=["one", "many"])
def test_a_hook_vector_is_validated_too(many: bool) -> None:
    client = EmbeddingClient(custom_hook=lambda _t: [0.0] * 10)
    with pytest.raises(EmbeddingError):
        if many:
            client.embed_documents(["a", "b"])
        else:
            client.embed_document("a")


SECRET = "fixture-credential-" + str(4242)


def test_a_non_ascii_digit_dimension_is_an_embedding_error(
    selfhosted: Stub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOTT_NMEMORY_EMBED_DIMENSION", "\u00b2")
    with pytest.raises(EmbeddingError, match="NOTT_NMEMORY_EMBED_DIMENSION"):
        SpaceFingerprint.current()


def test_an_overlong_digit_dimension_is_an_embedding_error(
    selfhosted: Stub, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 4301 digits pass isdigit, but int() refuses them with ValueError.
    monkeypatch.setenv("NOTT_NMEMORY_EMBED_DIMENSION", "9" * 4301)
    with pytest.raises(EmbeddingError, match="NOTT_NMEMORY_EMBED_DIMENSION"):
        SpaceFingerprint.current()


def test_a_zero_padded_dimension_is_still_its_value(
    selfhosted: Stub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOTT_NMEMORY_EMBED_DIMENSION", "0" * 4301 + "1024")
    assert SpaceFingerprint.current().dimension == 1024


def test_a_config_error_shows_with_no_pending_job_and_clears_when_fixed(
    selfhosted: Stub, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store.open(str(default_store_path()))
    monkeypatch.setenv("NOTT_NMEMORY_EMBED_ENDPOINT", "ftp://macbook/v1/embeddings")
    EmbeddingWorker(store, EmbeddingClient()).cycle()
    status = embedding_status(store.db)
    assert status.pending == 0
    assert (status.phase, status.last_error) == ("lexical_only", "config_error")

    monkeypatch.setenv("NOTT_NMEMORY_EMBED_ENDPOINT", selfhosted.url)
    EmbeddingWorker(store, EmbeddingClient()).cycle()
    status = embedding_status(store.db)
    assert status.phase != "lexical_only"
    assert status.last_error is None


def test_a_bad_endpoint_port_is_an_embedding_config_error(
    selfhosted: Stub, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store.open(str(default_store_path()))
    _ingest(store, 1)
    monkeypatch.setenv(
        "NOTT_NMEMORY_EMBED_ENDPOINT", "http://127.0.0.1:invalid/v1/embeddings"
    )
    with pytest.raises(EmbeddingError, match="NOTT_NMEMORY_EMBED_ENDPOINT"):
        EmbeddingClient().embed_document("a capsule")
    EmbeddingWorker(store, EmbeddingClient()).cycle()
    status = embedding_status(store.db)
    assert (status.phase, status.last_error) == ("lexical_only", "config_error")


def test_a_key_straddling_the_cut_is_redacted(
    stub: Stub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NOTT_NMEMORY_EMBED_PROVIDER", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", SECRET)
    monkeypatch.setattr(
        "nmemory.embedding.GEMINI_ENDPOINT", stub.url.removesuffix("/v1/embeddings")
    )
    stub.status, stub.body = 400, ("y" * 395 + SECRET).encode()
    with pytest.raises(EmbeddingError) as caught:
        EmbeddingClient().embed_document("a capsule")
    assert SECRET[:4] not in str(caught.value)


@pytest.mark.parametrize("many", [False, True], ids=["one", "many"])
def test_a_hook_conversion_error_is_redacted(
    monkeypatch: pytest.MonkeyPatch, many: bool
) -> None:
    monkeypatch.delenv("NOTT_NMEMORY_EMBED_PROVIDER", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", SECRET)
    client = EmbeddingClient(custom_hook=lambda _t: [SECRET] * DIMENSION)
    with pytest.raises(EmbeddingError) as caught:
        if many:
            client.embed_documents(["a", "b"])
        else:
            client.embed_document("a")
    assert SECRET not in str(caught.value)


def test_the_config_warning_is_redacted(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("NOTT_NMEMORY_EMBED_PROVIDER", "openai-compatible")
    monkeypatch.setenv("NOTT_NMEMORY_EMBED_MODEL", MODEL)
    monkeypatch.setenv("NOTT_NMEMORY_EMBED_DIMENSION", str(NATIVE))
    monkeypatch.setenv("OPENAI_API_KEY", SECRET)
    monkeypatch.setenv("NOTT_NMEMORY_EMBED_ENDPOINT", f"ftp://u:{SECRET}@h/v1")
    with caplog.at_level(logging.WARNING, logger="nmemory.embedding"):
        client = EmbeddingClient()
    assert client.config_error is not None
    assert SECRET not in client.config_error
    assert caplog.records
    assert all(SECRET not in r.getMessage() for r in caplog.records)
    with pytest.raises(EmbeddingError) as caught:
        client.embed_query("anything")
    assert SECRET not in str(caught.value)


def test_recall_goes_lexical_within_the_deadline_when_the_host_trickles(
    selfhosted: Stub,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = Store.open(str(default_store_path()))
    _ingest(store, 2)
    EmbeddingWorker(store, EmbeddingClient()).drain()  # whole: recall may ask
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()

    def trickle() -> None:
        conn, _ = server.accept()
        with conn:
            conn.recv(65536)
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100000\r\n\r\n")
            for _ in range(20):  # 10 s at most, one byte per 0.5 s
                try:
                    conn.sendall(b" ")
                except OSError:
                    return
                time.sleep(0.5)

    threading.Thread(target=trickle, daemon=True).start()
    monkeypatch.setenv(
        "NOTT_NMEMORY_EMBED_ENDPOINT",
        f"http://127.0.0.1:{server.getsockname()[1]}/v1/embeddings",
    )
    try:
        started = time.monotonic()
        res = retrieve(
            store,
            RetrieveParams(query="embedder outage", project_id="nott"),
            embeddings=EmbeddingClient(),
            now=NOW,
        )
        elapsed = time.monotonic() - started
    finally:
        server.close()
    assert res["degraded"] is True
    assert elapsed < QUERY_TIMEOUT_S + 1


def test_recall_goes_lexical_and_redacted_on_an_endpoint_with_a_bad_port(
    selfhosted: Stub, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store.open(str(default_store_path()))
    _ingest(store, 2)
    monkeypatch.setenv("OPENAI_API_KEY", SECRET)
    monkeypatch.setenv(
        "NOTT_NMEMORY_EMBED_ENDPOINT", f"http://127.0.0.1:{SECRET}/v1/embeddings"
    )
    res = retrieve(
        store,
        RetrieveParams(query="embedder outage", project_id="nott"),
        embeddings=EmbeddingClient(),
        now=NOW,
    )
    assert res["outcome"] == "grounded"
    assert res["lanes_run"] == ["term"]
    assert SECRET not in json.dumps(res, default=str)


STATUS_LINE = b"HTTP/1.1 200 OK\r\n"
HEADERS_END = b"Content-Length: 100000\r\n\r\n"
CHUNKED_HEAD = b"Transfer-Encoding: chunked\r\n\r\n"


def _answer(n: int = 1) -> bytes:
    data = [{"index": i, "embedding": [0.5] * NATIVE} for i in range(n)]
    return json.dumps({"object": "list", "data": data, "model": MODEL}).encode()


@pytest.mark.parametrize("call", ["query", "document"])
def test_an_answer_past_the_size_ceiling_is_refused(
    selfhosted: Stub, call: str
) -> None:
    selfhosted.body = _answer() + b" " * 8_000_000  # valid JSON, 8 MB of padding
    client = EmbeddingClient()
    with pytest.raises(EmbeddingError, match="larger than"):
        if call == "query":
            client.embed_query("q")
        else:
            client.embed_document("d")


def test_a_buffered_answer_past_the_deadline_is_refused(
    selfhosted: Stub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole answer sits in the reader's buffer once the headers are read, so
    the body reads never reach the socket; the deadline must still hold."""
    monkeypatch.setattr("nmemory.embedding.QUERY_TIMEOUT_S", 0.6)
    shift = [0.0]
    real_monotonic, real_begin = time.monotonic, http.client.HTTPResponse.begin

    def begin(response: http.client.HTTPResponse) -> None:
        time.sleep(0.1)  # every byte of the answer reaches the socket first
        real_begin(response)
        shift[0] = 0.7  # then the clock passes the deadline

    monkeypatch.setattr(time, "monotonic", lambda: real_monotonic() + shift[0])
    monkeypatch.setattr(http.client.HTTPResponse, "begin", begin)
    with pytest.raises(EmbeddingError, match="within"):
        EmbeddingClient().embed_query("q")


def test_an_error_page_past_the_ceiling_still_names_its_status(
    selfhosted: Stub,
) -> None:
    """An error page is cut, not refused as a flood: its status is the cause."""
    selfhosted.status, selfhosted.body = 400, b"x" * 1_000_000
    with pytest.raises(EmbeddingError, match="HTTP Error 400") as caught:
        EmbeddingClient().embed_document("d")
    assert len(str(caught.value)) < 65536


def _one_reply_host(reply: bytes) -> tuple[str, socket.socket]:
    """A host that answers every request with `reply`, then closes."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()

    def serve() -> None:
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            with conn:
                try:
                    conn.recv(65536)
                    conn.sendall(reply)
                    # Drain before closing: an RST would discard the reply unread.
                    conn.shutdown(socket.SHUT_WR)
                    while conn.recv(65536):
                        pass
                except OSError:
                    pass

    threading.Thread(target=serve, daemon=True).start()
    return f"http://127.0.0.1:{server.getsockname()[1]}/v1/embeddings", server


# One complete chunk and never the final `0` chunk.
UNTERMINATED = (
    STATUS_LINE
    + CHUNKED_HEAD
    + f"{len(_answer()):x}\r\n".encode()
    + _answer()
    + b"\r\n"
)


@pytest.mark.parametrize(
    ("call", "reply", "match"),
    [
        ("query", UNTERMINATED, "truncated"),
        ("document", UNTERMINATED, "truncated"),
        ("document", b"GIBBERISH NOT HTTP\r\n\r\n", "API call failed"),
    ],
    ids=["query-unterminated", "document-unterminated", "document-not-http"],
)
def test_a_broken_answer_is_an_embedding_error(
    selfhosted: Stub,
    monkeypatch: pytest.MonkeyPatch,
    call: str,
    reply: bytes,
    match: str,
) -> None:
    store = Store.open(str(default_store_path()))
    _ingest(store, 1)
    url, server = _one_reply_host(reply)
    monkeypatch.setenv("NOTT_NMEMORY_EMBED_ENDPOINT", url)
    client = EmbeddingClient()
    try:
        with pytest.raises(EmbeddingError, match=match):
            if call == "query":
                client.embed_query("q")
            else:
                client.embed_document("d")
        if call == "document":
            worker = EmbeddingWorker(store, client)
            worker.cycle()
            worker.cycle()  # the worker lives on to the next cycle
    finally:
        server.close()


def test_a_negative_chunk_size_cannot_read_past_the_ceiling(
    selfhosted: Stub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """http.client accepts chunk size -1; the query path still reads off the
    socket no more than about one ceiling of a 4 MB flood."""
    flood = STATUS_LINE + CHUNKED_HEAD + b"-1\r\n" + b"x" * 4_000_000
    url, server = _one_reply_host(flood)
    monkeypatch.setenv("NOTT_NMEMORY_EMBED_ENDPOINT", url)
    received = [0]
    readinto = embedding_module._ReadsWithin.readinto

    def counted(self: Any, buffer: Any, /) -> int:
        n = readinto(self, buffer)
        received[0] += n
        return n

    monkeypatch.setattr(embedding_module._ReadsWithin, "readinto", counted)
    client = EmbeddingClient()
    try:
        with pytest.raises(EmbeddingError, match="larger than"):
            client.embed_query("q")
    finally:
        server.close()
    assert received[0] <= client._answer_ceiling(1) + 2 * 65536, received[0]


@pytest.mark.parametrize("call", ["query", "document"])
def test_a_deeply_nested_answer_is_malformed(selfhosted: Stub, call: str) -> None:
    selfhosted.body = b"[" * 60_000 + b"]" * 60_000  # under the ceiling
    client = EmbeddingClient()
    with pytest.raises(EmbeddingError, match="malformed"):
        if call == "query":
            client.embed_query("q")
        else:
            client.embed_document("d")


def test_a_retried_status_waits_then_a_refused_one_raises(
    selfhosted: Stub, monkeypatch: pytest.MonkeyPatch
) -> None:
    selfhosted.replies = [(503, b"x" * 100_000), (400, b"no")]
    waits: list[float] = []
    monkeypatch.setattr("nmemory.embedding.time.sleep", waits.append)
    with pytest.raises(EmbeddingError, match="HTTP Error 400"):
        EmbeddingClient().embed_document("d")
    assert len(selfhosted.requests) == 2
    assert waits == [1]


def test_an_empty_port_means_the_default_port(
    selfhosted: Stub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RFC 3986: `http://host:/` names the scheme's default port; it is accepted."""
    monkeypatch.setenv("NOTT_NMEMORY_EMBED_ENDPOINT", "http://127.0.0.1:/v1/embeddings")
    assert EmbeddingClient().config_error is None


@pytest.mark.parametrize("call", ["query", "document"])
def test_a_truncated_answer_is_refused(selfhosted: Stub, call: str) -> None:
    selfhosted.body = _answer()
    selfhosted.advertised = 2 * len(selfhosted.body)
    client = EmbeddingClient()
    with pytest.raises(EmbeddingError, match="truncated"):
        if call == "query":
            client.embed_query("q")
        else:
            client.embed_document("d")


def test_a_hook_vector_must_have_the_native_size(selfhosted: Stub) -> None:
    client = EmbeddingClient(custom_hook=lambda _t: [1.0] * (NATIVE - 1))
    with pytest.raises(EmbeddingError, match=f"expected {NATIVE}"):
        client.embed_document("a")


# What the host sends one piece per tick before trickling body bytes: the status
# line a byte at a time, header lines one at a time, or the whole head at once.
TRICKLES = {
    "status": [STATUS_LINE[i : i + 1] for i in range(len(STATUS_LINE))] + [HEADERS_END],
    "headers": [STATUS_LINE] + [b"X-Pad: 1\r\n"] * 10 + [HEADERS_END],
    "body": [STATUS_LINE + HEADERS_END],
    # A chunk-size line that never ends, then trailer lines one at a time.
    "chunk-size": [STATUS_LINE + CHUNKED_HEAD, b"1", b"0"],
    "trailers": [STATUS_LINE + CHUNKED_HEAD + b"1\r\nx\r\n0\r\n"]
    + [b"X-Trail: 1\r\n"] * 10,
}


class Trickler:
    """A host that sends one piece of `head` per `every` seconds, then one body
    byte per tick, one thread per connection; a connection ends when the client
    closes it."""

    def __init__(self, every: float, limit: float, head: list[bytes]) -> None:
        self.every, self.limit, self.head = every, limit, head
        self.live = 0
        self.connections = 0  # every connection ever accepted
        self._lock = threading.Lock()
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen()
        self.url = f"http://127.0.0.1:{self.server.getsockname()[1]}/v1/embeddings"
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            with self._lock:
                self.live += 1
                self.connections += 1
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        try:
            with conn:
                conn.recv(65536)
                pieces = iter(self.head)
                conn.sendall(next(pieces))
                conn.settimeout(self.every)
                ends = time.monotonic() + self.limit
                while time.monotonic() < ends:
                    try:
                        if conn.recv(1) == b"":
                            return  # the client closed its socket
                    except TimeoutError:
                        conn.sendall(next(pieces, b" "))
        except OSError:
            return
        finally:
            with self._lock:
                self.live -= 1

    def close(self) -> None:
        self.server.close()


@pytest.mark.parametrize("phase", sorted(TRICKLES))
def test_expired_queries_leave_no_thread_and_no_socket(
    selfhosted: Stub, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    deadline = 0.6
    monkeypatch.setattr("nmemory.embedding.QUERY_TIMEOUT_S", deadline)
    host = Trickler(every=0.2, limit=20, head=TRICKLES[phase])
    monkeypatch.setenv("NOTT_NMEMORY_EMBED_ENDPOINT", host.url)
    client = EmbeddingClient()
    baseline = threading.active_count()
    try:
        for n in range(4):
            started = time.monotonic()
            with pytest.raises(EmbeddingError):
                client.embed_query(f"query {n}")
            elapsed = time.monotonic() - started
            assert elapsed < deadline + 0.25, f"{phase}: {elapsed:.2f} s"
        # The host notices a closed socket within one tick; its thread ends after.
        settle = time.monotonic() + 2
        while (
            host.live or threading.active_count() != baseline
        ) and time.monotonic() < settle:
            time.sleep(0.05)
        assert host.live == 0
        assert threading.active_count() == baseline
    finally:
        host.close()


@pytest.mark.parametrize("error", [OSError, ValueError, TypeError])
@pytest.mark.parametrize("many", [False, True], ids=["one", "many"])
def test_a_raising_hook_is_an_embedding_error_without_the_key(
    monkeypatch: pytest.MonkeyPatch, error: type[Exception], many: bool
) -> None:
    monkeypatch.delenv("NOTT_NMEMORY_EMBED_PROVIDER", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", SECRET)

    def hook(_text: str) -> list[float]:
        raise error(f"hook broke with {SECRET}")

    client = EmbeddingClient(custom_hook=hook)
    with pytest.raises(EmbeddingError) as caught:
        if many:
            client.embed_documents(["a", "b"])
        else:
            client.embed_document("a")
    assert SECRET not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__


def _recall(store: Store, client: EmbeddingClient, query: str) -> dict[str, Any]:
    return retrieve(
        store,
        RetrieveParams(query=query, project_id="nott"),
        embeddings=client,
        now=NOW,
    )


def test_recall_stays_lexical_while_a_new_space_is_built(
    selfhosted: Stub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("nmemory.embedding_index.OPENAI_JOBS_PER_CYCLE", 2)
    store = Store.open(str(default_store_path()))
    _ingest(store, 6)
    EmbeddingWorker(store, EmbeddingClient()).drain()
    assert embedding_status(store.db).phase == "ready"

    monkeypatch.setenv("NOTT_NMEMORY_EMBED_MODEL", "another-model")
    client = EmbeddingClient()
    assert EmbeddingWorker(store, client).cycle() == 2  # 2 of 6 in the new space
    assert embedding_status(store.db).phase == "reindexing"
    asked = len(selfhosted.requests)
    res = _recall(store, client, "embedder outage")
    assert res["lanes_run"] == ["term"]
    assert "until the embedding index" in res["lane_note"]
    assert "degraded" not in res
    assert len(selfhosted.requests) == asked  # no query embedding was asked


def test_a_failed_query_embedding_pauses_the_vector_lane(
    selfhosted: Stub, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store.open(str(default_store_path()))
    _ingest(store, 2)
    EmbeddingWorker(store, EmbeddingClient()).drain()
    deadline, cooldown = 0.6, 1.5
    monkeypatch.setattr("nmemory.embedding.QUERY_TIMEOUT_S", deadline)
    monkeypatch.setattr("nmemory.retrieve.QUERY_COOLDOWN_S", cooldown)
    host = Trickler(every=0.2, limit=20, head=TRICKLES["body"])
    monkeypatch.setenv("NOTT_NMEMORY_EMBED_ENDPOINT", host.url)
    client = EmbeddingClient()

    def timed(query: str) -> tuple[dict[str, Any], float]:
        started = time.monotonic()
        res = _recall(store, client, query)
        return res, time.monotonic() - started

    try:
        first, spent = timed("embedder outage")
        assert first["degraded"] is True and spent >= deadline
        asked = host.connections
        second, _spent = timed("embedder outage again")  # not in the query cache
        assert second["lanes_run"] == ["term"]
        assert "paused" in second["lane_note"]
        assert host.connections == asked  # no query embedding was asked
        time.sleep(cooldown + 0.1)
        third, spent = timed("embedder outage once more")
        assert "vector" in third["lanes_run"] and spent >= deadline
    finally:
        host.close()


def test_a_stopped_reindex_resumes_without_redoing_vectors(
    selfhosted: Stub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The re-embed is a background job over persisted rows: a worker that dies
    mid-batch and a store reopened later pick up where it stopped, and recall
    answers lexically throughout."""
    monkeypatch.setattr("nmemory.embedding_index.OPENAI_JOBS_PER_CYCLE", 2)
    path = str(default_store_path())
    store = Store.open(path)
    _ingest(store, 6)
    EmbeddingWorker(store, EmbeddingClient()).drain()
    monkeypatch.setenv("NOTT_NMEMORY_EMBED_MODEL", "another-model")

    def embedded() -> list[str]:
        return [
            text
            for _path, _headers, payload in selfhosted.requests
            if payload["model"] == "another-model"
            for text in payload["input"]
        ]

    client = EmbeddingClient()
    assert EmbeddingWorker(store, client).cycle() == 2
    assert _recall(store, client, "embedder outage")["lanes_run"] == ["term"]

    # The worker dies mid-batch: two jobs claimed, nothing sent.
    def die(texts: list[str], **kwargs: Any) -> list[list[float]]:
        raise KeyboardInterrupt

    dying = EmbeddingClient()
    monkeypatch.setattr(dying, "embed_documents", die)
    with pytest.raises(KeyboardInterrupt):
        EmbeddingWorker(store, dying).cycle()
    del store

    # Reopened after the claim's lease, with a fresh worker.
    lease = database_now_us() + 121_000_000
    monkeypatch.setattr("nmemory.repositories.embeddings.now_us", lambda: lease)
    store = Store.open(path)
    client = EmbeddingClient()
    assert _recall(store, client, "embedder outage")["lanes_run"] == ["term"]
    EmbeddingWorker(store, client).drain()

    status = embedding_status(store.db)
    assert status.phase == "ready"
    assert status.active_space.split(":")[1] == "another-model"
    assert status.active_vectors == status.live_capsules == 6
    assert len(embedded()) == 6, embedded()  # no completed vector embedded twice
    assert "vector" in _recall(store, client, "embedder outage")["lanes_run"]


@pytest.mark.parametrize("outage", ["refused", "timeout"])
def test_a_reindex_survives_hours_with_the_endpoint_down(
    selfhosted: Stub, monkeypatch: pytest.MonkeyPatch, outage: str
) -> None:
    """Stopping the endpoint is how a re-embed is paused: an endpoint failure is
    not the job's, so hours of it never use up the job's attempts."""
    store = Store.open(str(default_store_path()))
    _ingest(store, 2)
    clock = [database_now_us()]
    monkeypatch.setattr("nmemory.repositories.embeddings.now_us", lambda: clock[0])
    down = socket.socket()
    down.bind(("127.0.0.1", 0))
    if outage == "timeout":
        down.listen(0)  # accepts the connection, never answers
        monkeypatch.setattr("nmemory.embedding.INDEX_TIMEOUT_S", 0.05)
    port = down.getsockname()[1]
    if outage == "refused":
        down.close()
    monkeypatch.setenv(
        "NOTT_NMEMORY_EMBED_ENDPOINT", f"http://127.0.0.1:{port}/v1/embeddings"
    )
    try:
        worker = EmbeddingWorker(store, EmbeddingClient())
        for _ in range(30):  # 30 rounds, 5 min apart: two and a half hours
            assert worker.cycle() == 0
            clock[0] += 301_000_000
    finally:
        down.close()

    monkeypatch.setenv("NOTT_NMEMORY_EMBED_ENDPOINT", selfhosted.url)
    EmbeddingWorker(store, EmbeddingClient()).drain()
    status = embedding_status(store.db)
    assert (status.pending, status.active_vectors) == (0, 2)
    assert status.phase == "ready"


def test_a_rollback_to_the_active_model_stays_lexical_until_it_is_whole(
    selfhosted: Stub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("nmemory.embedding_index.OPENAI_JOBS_PER_CYCLE", 2)
    store = Store.open(str(default_store_path()))
    _ingest(store, 6)
    EmbeddingWorker(store, EmbeddingClient()).drain()  # A, whole
    monkeypatch.setenv("NOTT_NMEMORY_EMBED_MODEL", "another-model")
    assert EmbeddingWorker(store, EmbeddingClient()).cycle() == 2  # A -> B, 2 of 6
    monkeypatch.setenv("NOTT_NMEMORY_EMBED_MODEL", MODEL)  # back to A, 4 of 6
    client = EmbeddingClient()
    worker = EmbeddingWorker(store, client)
    assert worker.cycle() == 2  # repairing A, not yet whole
    assert _recall(store, client, "embedder outage")["lanes_run"] == ["term"]
    worker.drain()
    assert embedding_status(store.db).phase == "ready"
    assert "vector" in _recall(store, client, "embedder outage")["lanes_run"]


def _context(store: Store, client: EmbeddingClient) -> dict[str, Any]:
    return execute_context(
        store,
        "embedder outage",
        project_id="nott",
        sources=["memory"],
        embeddings=client,
        now=NOW,
    )


def test_context_uses_the_same_vector_gate_as_recall(
    selfhosted: Stub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("nmemory.embedding_index.OPENAI_JOBS_PER_CYCLE", 2)
    store = Store.open(str(default_store_path()))
    _ingest(store, 6)
    EmbeddingWorker(store, EmbeddingClient()).drain()
    monkeypatch.setenv("NOTT_NMEMORY_EMBED_MODEL", "another-model")
    client = EmbeddingClient()
    assert EmbeddingWorker(store, client).cycle() == 2
    asked = len(selfhosted.requests)
    semantic = _context(store, client)["semantic"]
    assert semantic["status"] == "unavailable_lexical_only"
    assert "until the embedding index" in semantic["detail"]
    assert len(selfhosted.requests) == asked  # no query embedding during a reindex


def test_context_respects_the_query_cooldown(selfhosted: Stub) -> None:
    store = Store.open(str(default_store_path()))
    _ingest(store, 2)
    EmbeddingWorker(store, EmbeddingClient()).drain()
    client = EmbeddingClient()
    selfhosted.status, selfhosted.body = 400, b"model not loaded"
    assert _context(store, client)["semantic"]["status"] == "unavailable_lexical_only"
    asked = len(selfhosted.requests)
    semantic = _context(store, client)["semantic"]
    assert "paused" in semantic["detail"]
    assert len(selfhosted.requests) == asked


def test_an_exhausted_quota_does_not_arm_the_cooldown(selfhosted: Stub) -> None:
    store = Store.open(str(default_store_path()))
    _ingest(store, 2)
    EmbeddingWorker(store, EmbeddingClient()).drain()
    client = EmbeddingClient()
    selfhosted.status, selfhosted.body = 429, b"insufficient_quota"
    assert _recall(store, client, "embedder outage")["degraded"] is True
    asked = len(selfhosted.requests)
    res = _recall(store, client, "embedder outage again")
    assert "lane_note" not in res
    assert len(selfhosted.requests) == asked + 1  # asked again, not paused


def test_a_cooldown_in_one_store_does_not_pause_another(selfhosted: Stub) -> None:
    first = Store.open(str(default_store_path()))
    second = Store.open_disposable_test_store()
    for store in (first, second):
        _ingest(store, 2)
        EmbeddingWorker(store, EmbeddingClient()).drain()
    client = EmbeddingClient()  # one client, reused across both stores
    selfhosted.replies = [(400, b"model not loaded")]
    assert _recall(first, client, "embedder outage")["degraded"] is True
    res = _recall(second, client, "embedder outage again")
    assert "vector" in res["lanes_run"], res.get("lane_note")


def test_the_lane_note_reaches_the_agent_through_the_door(
    selfhosted: Stub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("nmemory.embedding_index.OPENAI_JOBS_PER_CYCLE", 2)
    store = Store.open(str(default_store_path()))
    _ingest(store, 6)
    EmbeddingWorker(store, EmbeddingClient()).drain()
    monkeypatch.setenv("NOTT_NMEMORY_EMBED_MODEL", "another-model")
    client = EmbeddingClient()
    assert EmbeddingWorker(store, client).cycle() == 2  # mid re-embed
    server = MemoryServer(store=store, embeddings=client, project_id="nott")
    for terms in (["embedder", "outage"], ["no-such-word-anywhere"]):
        res = server.call_tool("nmemory_capsule_retrieve", {"terms": terms}, now=NOW)
        assert res["result"]["isError"] is False, res
        payload = json.loads(res["result"]["content"][0]["text"])
        assert payload["lanes_run"] == ["term"], payload
        assert "until the embedding index" in payload["lane_note"], payload


@pytest.mark.parametrize(
    ("status", "body", "attempts", "error_class"),
    [
        (400, b"unknown model: rerate-model", 20, "operator_action"),
        (403, b"cannot connect to this model", 20, "operator_action"),
        (400, b"model named insufficient_quota does not exist", 20, "operator_action"),
        (
            200,
            json.dumps(
                {"data": [{"index": 0, "embedding": ["rate"] * NATIVE}]}
            ).encode(),
            20,
            "operator_action",
        ),
        (429, b"busy", 0, "rate_limited"),
        (503, b"busy", 0, "failover_eligible"),
    ],
    ids=[
        "permanent400",
        "permanent403",
        "permanent400-quota-text",
        "invalid-vector",
        "transient429",
        "transient503",
    ],
)
def test_worker_retry_accounting_uses_typed_failure_not_error_text(
    selfhosted: Stub,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    body: bytes,
    attempts: int,
    error_class: str,
) -> None:
    store = Store.open(str(default_store_path()))
    _ingest(store, 1)
    selfhosted.status, selfhosted.body = status, body
    clock = [database_now_us()]
    monkeypatch.setattr("nmemory.repositories.embeddings.now_us", lambda: clock[0])
    monkeypatch.setattr("nmemory.embedding.time.sleep", lambda _s: None)
    worker = EmbeddingWorker(store, EmbeddingClient())
    for _ in range(21):
        assert worker.cycle() == 0
        clock[0] += 301_000_000
    with store.db.connection() as handle, handle.cursor() as cur:
        row = cur.execute(
            "SELECT attempts, last_error_class FROM embedding_jobs"
        ).fetchone()
    assert row is not None
    assert tuple(row) == (attempts, error_class)
    asked = len(selfhosted.requests)
    selfhosted.status, selfhosted.body = 200, None
    done = worker.cycle()
    if attempts == 20:
        assert done == 0 and len(selfhosted.requests) == asked
    else:
        assert done == 1
        worker.cycle()
        assert embedding_status(store.db).phase == "ready"


def test_fallback_quota_stops_remaining_calls_and_preserves_refusal(
    selfhosted: Stub,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = Store.open(str(default_store_path()))
    _ingest(store, 4)
    one_vector = json.dumps(
        {"data": [{"index": 0, "embedding": [1.0] * NATIVE}]}
    ).encode()
    selfhosted.replies = [
        (400, b"bulk unsupported"),
        (200, one_vector),
        (429, SPENDING_CAP),
    ]
    worker = EmbeddingWorker(store, EmbeddingClient())
    assert worker.cycle() == 1
    assert (
        len(selfhosted.requests) == 3
    )  # bulk, first success, then refusal; no further calls
    status = embedding_status(store.db)
    assert status.phase == "lexical_only" and status.last_error == "quota_exhausted"
    assert (status.pending, status.active_vectors) == (3, 1)
    with store.db.connection() as handle, handle.cursor() as cur:
        rows = cur.execute(
            "SELECT attempts, last_error_class FROM embedding_jobs"
        ).fetchall()
    assert [tuple(row) for row in rows] == [(0, "quota_exhausted")] * 3
    assert worker.cycle() == 0
    assert (
        len(selfhosted.requests) == 3
    )  # refunded jobs are deferred, not immediately retried
    retry_at = database_now_us() + 301_000_000
    monkeypatch.setattr("nmemory.repositories.embeddings.now_us", lambda: retry_at)
    assert worker.drain() == 3
    recovered = embedding_status(store.db)
    assert recovered.phase == "ready" and recovered.last_error is None
    assert (recovered.pending, recovered.active_vectors) == (0, 4)


def test_activation_requires_vectors_for_the_current_capsule_text(
    selfhosted: Stub,
) -> None:
    store = Store.open(str(default_store_path()))
    _ingest(store, 2)
    EmbeddingWorker(store, EmbeddingClient()).drain()
    with store.db.writing() as handle, handle.cursor() as cur:
        cur.execute(
            "UPDATE embedding_state SET target_space = active_space, phase = 'reindexing'"
        )
        cur.execute(
            "UPDATE capsule_embeddings SET text_fingerprint = ? WHERE capsule_id = 'cap-1'",
            (sha256_hex(b"obsolete-text"),),
        )
    assert embedding_status(store.db).pending == 0
    assert not store.embeddings.activate_target_if_complete()
    assert store.embeddings.serving_space() is None
    client = EmbeddingClient()
    assert _recall(store, client, "embedder outage")["lanes_run"] == ["term"]
    assert EmbeddingWorker(store, client).drain() == 1
    assert embedding_status(store.db).phase == "ready"


class OpenAIHost:
    """Native loopback HTTP for OpenAI embeddings."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                assert self.path == "/v1/embeddings"
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                data = [
                    {"index": i, "embedding": [1.0] * DIMENSION}
                    for i, _ in enumerate(json.loads(raw)["input"])
                ]
                body = json.dumps({"data": data}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: object) -> None:
                return None

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        port = self.server.server_address[1]
        monkeypatch.setattr(
            "nmemory.embedding.OPENAI_ENDPOINT",
            f"http://127.0.0.1:{port}/v1/embeddings",
        )

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def test_completion_refuses_a_job_from_an_abandoned_space_before_requeue(
    selfhosted: Stub,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = Store.open(str(default_store_path()))
    _ingest(store, 6)
    EmbeddingWorker(store, EmbeddingClient()).drain()
    host = OpenAIHost(monkeypatch)
    try:
        monkeypatch.setenv("NOTT_NMEMORY_EMBED_PROVIDER", "openai")
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        monkeypatch.setattr("nmemory.embedding_index.OPENAI_JOBS_PER_CYCLE", 2)
        assert EmbeddingWorker(store, EmbeddingClient()).cycle() == 2
        space_b = SpaceFingerprint.current().as_str()
        delayed = store.embeddings.claim_embedding(space_b)
        assert delayed is not None
        monkeypatch.setenv("NOTT_NMEMORY_EMBED_PROVIDER", "openai-compatible")
        assert store.embeddings.begin_current_space_transition_if_needed() is not None
        with store.db.connection() as handle, handle.cursor() as cur:
            before = cur.execute(
                "SELECT * FROM capsule_embeddings ORDER BY capsule_id"
            ).fetchall()
        assert not store.embeddings.complete_embedding(
            delayed, [1.0] * DIMENSION, space_b
        )
        with store.db.connection() as handle, handle.cursor() as cur:
            after = cur.execute(
                "SELECT * FROM capsule_embeddings ORDER BY capsule_id"
            ).fetchall()
        assert after == before
    finally:
        host.close()


def _broken_error_host(
    head: bytes, tail_stall: bool
) -> tuple[socket.socket, list[int]]:
    """A host answering each request with `head` and then a body that never completes."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    served: list[int] = []

    def serve() -> None:
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            served.append(1)
            with conn:
                conn.recv(65536)
                conn.sendall(head)
                if tail_stall:
                    time.sleep(3)

    threading.Thread(target=serve, daemon=True).start()
    return server, served


def test_a_truncated_503_body_is_still_a_retryable_status(
    selfhosted: Stub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("nmemory.embedding.time.sleep", lambda _s: None)
    server, served = _broken_error_host(
        b"HTTP/1.1 503 Busy\r\nTransfer-Encoding: chunked\r\n\r\n4\r\nbusy\r\n", False
    )
    monkeypatch.setenv(
        "NOTT_NMEMORY_EMBED_ENDPOINT",
        f"http://127.0.0.1:{server.getsockname()[1]}/v1/embeddings",
    )
    try:
        with pytest.raises(embedding_module.EmbeddingTransportError):
            EmbeddingClient()._request(["x"], timeout=2.0, attempts=3)
    finally:
        server.close()
    assert len(served) == 3


def test_a_stalled_quota_429_body_stays_quota_exhausted(
    selfhosted: Stub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("nmemory.embedding.time.sleep", lambda _s: None)
    server, served = _broken_error_host(
        b"HTTP/1.1 429 Too Many\r\nContent-Length: 100000\r\n\r\n"
        b'{"error":{"code":"insufficient_quota"}}',
        True,
    )
    monkeypatch.setenv(
        "NOTT_NMEMORY_EMBED_ENDPOINT",
        f"http://127.0.0.1:{server.getsockname()[1]}/v1/embeddings",
    )
    try:
        with pytest.raises(embedding_module.EmbeddingQuotaExhausted):
            EmbeddingClient()._request(["x"], timeout=1.0, attempts=3)
    finally:
        server.close()
    assert len(served) == 1
