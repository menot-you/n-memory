"""The embedding provider is chosen by environment, and 429 explains why it failed.

RED before product: provider switching was a commit (`current()` returned
fixed `default_openai()`) and the response body — where
`credit_balance_exhausted` was returned — was thrown away with the rest of HTTPError.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from nmemory.capsule import (
    AuthorityClass,
    Capsule,
    Freshness,
    Provenance,
    Scope,
    sha256_hex,
)
from nmemory.embedding import GEMINI_MAX_BATCH, EmbeddingClient
from nmemory.embedding_index import EmbeddingWorker
from nmemory.space import (
    DIMENSION,
    EmbeddingError,
    EmbeddingQuotaExhausted,
    SpaceFingerprint,
)
from nmemory.store import Store


def test_space_segue_o_provider_do_ambiente(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without environment defaults to Gemini; `openai` reverts; unknown REJECTS."""
    monkeypatch.delenv("NOTT_NMEMORY_EMBED_PROVIDER", raising=False)
    current = SpaceFingerprint.current()
    assert current.provider == "gemini"
    assert current.as_str() == "gemini:gemini-embedding-2:3072:nmemory-retrieval-v1"
    assert current.dimension == DIMENSION

    monkeypatch.setenv("NOTT_NMEMORY_EMBED_PROVIDER", "openai")
    assert (
        SpaceFingerprint.current().as_str()
        == "openai:text-embedding-3-large:3072:nmemory-retrieval-v1"
    )

    monkeypatch.setenv("NOTT_NMEMORY_EMBED_PROVIDER", "cohere")
    with pytest.raises(EmbeddingError, match="is not a known provider"):
        SpaceFingerprint.current()


def test_gemini_le_values_e_nao_data_embedding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gemini response is `embeddings[].values`, not `data[].embedding`."""
    monkeypatch.delenv("NOTT_NMEMORY_EMBED_PROVIDER", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "chave-de-teste")
    enviados: list[str] = []

    def answer(_client: object, url: str, *_a: object) -> object:
        enviados.append(url)
        return {"embeddings": [{"values": [0.5] * DIMENSION}]}

    monkeypatch.setattr(EmbeddingClient, "_post_within", answer)
    client = EmbeddingClient()
    vec = client.embed_text("uma capsula")
    assert len(vec) == DIMENSION
    assert "generativelanguage.googleapis.com" in enviados[0]
    assert "openai.com" not in enviados[0]


def test_erro_http_carrega_o_corpo_da_resposta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 429 for exhausted balance must not be read as rate limit."""
    monkeypatch.setenv("NOTT_NMEMORY_EMBED_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "chave-de-teste")
    corpo = b'{"error":{"code":"credit_balance_exhausted"}}'

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(429, "Too Many Requests")
            self.send_header("Content-Length", str(len(corpo)))
            self.end_headers()
            self.wfile.write(corpo)

        def log_message(self, *args: object) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    monkeypatch.setattr(
        "nmemory.embedding.OPENAI_ENDPOINT", f"http://127.0.0.1:{port}/v1/embeddings"
    )
    try:
        with pytest.raises(EmbeddingQuotaExhausted, match="credit_balance_exhausted"):
            EmbeddingClient().embed_text("uma capsula")
    finally:
        server.shutdown()
        server.server_close()


def test_gemini_batch_embed_contents_caps_requests_at_one_hundred(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gemini batchEmbedContents receives at most 100 items per HTTP call."""
    monkeypatch.delenv("NOTT_NMEMORY_EMBED_PROVIDER", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "chave-de-teste")
    batch_sizes: list[int] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            payload = json.loads(raw)
            requests = payload.get("requests", [])
            batch_sizes.append(len(requests))
            if len(requests) > GEMINI_MAX_BATCH:
                err = json.dumps(
                    {
                        "error": {
                            "code": 400,
                            "message": "At most 100 requests can be in one batch.",
                        }
                    }
                ).encode("utf-8")
                self.send_response(400, "Bad Request")
                self.send_header("Content-Length", str(len(err)))
                self.end_headers()
                self.wfile.write(err)
                return
            body = json.dumps(
                {"embeddings": [{"values": [0.5] * DIMENSION} for _ in requests]}
            ).encode("utf-8")
            self.send_response(200, "OK")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    monkeypatch.setattr(
        "nmemory.embedding.GEMINI_ENDPOINT", f"http://127.0.0.1:{port}/v1beta/models"
    )
    try:
        client = EmbeddingClient()
        vectors = client.embed_documents([f"capsule {i}" for i in range(150)])
        assert len(vectors) == 150
        assert batch_sizes == [100, 50]

        batch_sizes.clear()
        now = datetime(2026, 10, 17, 12, 0, 0, tzinfo=UTC)
        store = Store.open_disposable_test_store()
        for i in range(150):
            store.append(
                Capsule(
                    content=f"Gemini batch capsule {i}",
                    provenance=Provenance(
                        source="session",
                        anchor=f"file{i}.py:1",
                        source_hash=sha256_hex(f"gemini-batch-{i}".encode()),
                    ),
                    confidence=0.9,
                    freshness=Freshness(valid_from=now),
                    scope=Scope(project_id="nott"),
                    authority_class=AuthorityClass.USER_STATED,
                    instruction_taint=False,
                ),
                now,
            )
        worker = EmbeddingWorker(store, EmbeddingClient())
        assert worker.cycle() == 100
        assert worker.cycle() == 50
        assert worker.cycle() == 0
        assert batch_sizes == [100, 50]
    finally:
        server.shutdown()
        server.server_close()
