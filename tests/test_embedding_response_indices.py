"""Provider response identity must survive the HTTP-to-store embedding path."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from nmemory.capsule import AuthorityClass, Capsule, Freshness, Provenance, Scope
from nmemory.embedding import EmbeddingClient
from nmemory.embedding_index import EmbeddingWorker
from nmemory.queries.embedding import embedding_status
from nmemory.space import DIMENSION, EmbeddingError, SpaceFingerprint
from nmemory.store import Store


def _vector(index: int) -> list[float]:
    vector = [0.0] * DIMENSION
    vector[index] = 1.0
    return vector


def _answering(monkeypatch: pytest.MonkeyPatch, body: object) -> None:
    """Every provider call answers `body`, as parsed off the wire."""
    monkeypatch.setattr(EmbeddingClient, "_post_within", lambda *_a: body)


def _openai_client(monkeypatch: pytest.MonkeyPatch, items: object) -> EmbeddingClient:
    monkeypatch.setenv("NOTT_NMEMORY_EMBED_PROVIDER", "openai")
    _answering(monkeypatch, {"data": items})
    return EmbeddingClient(api_key="test-only")


def test_openai_response_permutation_keeps_vectors_on_their_capsules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real queue, completion and vector search observe capsule identity."""
    client = _openai_client(
        monkeypatch,
        [
            {"index": 1, "embedding": _vector(7)},
            {"index": 0, "embedding": _vector(3)},
        ],
    )
    now = datetime.now(UTC)
    store = Store.open_disposable_test_store()
    ids = []
    for content in ("First embedding identity", "Second embedding identity"):
        capsule_id, _ = store.append(
            Capsule(
                content=content,
                provenance=Provenance(
                    source="test", anchor=content, source_hash=content
                ),
                confidence=0.9,
                freshness=Freshness(valid_from=now),
                scope=Scope(project_id="embedding-identity-test"),
                authority_class=AuthorityClass.OBSERVED_FACT,
                instruction_taint=False,
            ),
            now,
        )
        ids.append(capsule_id)
    assert EmbeddingWorker(store, client).cycle() == 2
    assert embedding_status(store.db).active_vectors == 2
    space = SpaceFingerprint.current().as_str()
    for capsule_id, vector_index in zip(ids, (3, 7), strict=True):
        nearest = store.embeddings.search_cosine(
            _vector(vector_index), 2, space_fingerprint=space
        )
        assert nearest[0][0] == capsule_id
        assert nearest[0][1] == pytest.approx(1.0)


@pytest.mark.parametrize(
    "indices",
    [
        [0],
        [0, 0],
        [0, 2],
        [-1, 1],
        [0, "1"],
        [0, 1.0],
        [0, True],
        [0, None],
        [0, "missing"],
        [0, 1, 2],
    ],
    ids=[
        "missing-result",
        "duplicate",
        "out-of-range",
        "negative",
        "string",
        "float",
        "boolean",
        "null",
        "missing-index",
        "extra-result",
    ],
)
def test_openai_rejects_incomplete_or_malformed_response_indices(
    monkeypatch: pytest.MonkeyPatch, indices: list[object]
) -> None:
    items: list[dict[str, Any]] = []
    for index in indices:
        item: dict[str, Any] = {"embedding": _vector(3)}
        if index != "missing":
            item["index"] = index
        items.append(item)
    client = _openai_client(monkeypatch, items)
    with pytest.raises(EmbeddingError, match="index|indices|results"):
        client.embed_documents(["First", "Second"])


@pytest.mark.parametrize("items", [None, {}, [None, {}]])
def test_openai_rejects_malformed_response_data(
    monkeypatch: pytest.MonkeyPatch, items: object
) -> None:
    client = _openai_client(monkeypatch, items)
    with pytest.raises(EmbeddingError, match="results|result"):
        client.embed_documents(["First", "Second"])


def test_gemini_accepts_positional_vectors_without_openai_indices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NOTT_NMEMORY_EMBED_PROVIDER", "gemini")

    _answering(
        monkeypatch, {"embeddings": [{"values": _vector(3)}, {"values": _vector(7)}]}
    )
    assert EmbeddingClient(api_key="test-only").embed_documents(
        ["First", "Second"]
    ) == [_vector(3), _vector(7)]
