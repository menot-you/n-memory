"""A viewer search writes no embedding state: a failed or a served query embedding
moves the store's embedding phase for an agent's search only (Codex review, round 1)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from storefile import put_embedding

from nmemory.capsule import (
    AuthorityClass,
    Capsule,
    Freshness,
    Provenance,
    Scope,
    sha256_hex,
)
from nmemory.embedding import EmbeddingClient
from nmemory.queries.embedding import embedding_status
from nmemory.server import MemoryServer
from nmemory.space import DIMENSION, EmbeddingError
from nmemory.store import Store

NOW = datetime(2026, 10, 13, 12, 0, 0, tzinfo=UTC)
TERM = "viewerprobe"


def _phase_after_a_failed_embedding(viewer: bool) -> tuple[str, str]:
    store = Store.open_disposable_test_store()

    def failing(_text: str) -> list[float]:
        raise EmbeddingError("503 embedding service down")

    client = EmbeddingClient(custom_hook=failing)
    content = f"A capsule about {TERM}"
    cid, _ = store.append(
        Capsule(
            content=content,
            provenance=Provenance(
                source="test", anchor="v:1", source_hash=sha256_hex(content.encode())
            ),
            confidence=0.9,
            freshness=Freshness(valid_from=NOW),
            scope=Scope(project_id="viewerproject"),
            authority_class=AuthorityClass.USER_STATED,
            instruction_taint=False,
        ),
        NOW,
    )
    store.set_birth_classification(cid, "fact", "project", NOW)
    put_embedding(
        store.db,
        cid,
        [1.0] + [0.0] * (DIMENSION - 1),
        client.space_fingerprint.as_str(),
        sha256_hex(cid.encode()),
    )
    before = embedding_status(store.db).phase
    server = MemoryServer(
        store=store,
        embeddings=client,
        anchor_root=".",
        project_id="viewerproject",
        viewer=viewer,
    )
    reply = server.call_tool(
        "nmemory_capsule_retrieve",
        {"terms": [TERM], "project_id": "viewerproject", "lane": "auto"},
        None,
        now=NOW,
    )
    payload = json.loads(reply["result"]["content"][0]["text"])
    assert payload["degraded"] is True, payload
    return before, embedding_status(store.db).phase


@pytest.mark.parametrize("viewer", [False, True])
def test_only_an_agents_failed_embedding_moves_the_phase(viewer: bool) -> None:
    before, after = _phase_after_a_failed_embedding(viewer)
    if viewer:
        assert after == before, "a viewer search wrote the embedding phase"
    else:
        # The control: the same search through a normal server moves it.
        assert (before, after) == ("ready", "lexical_only")
