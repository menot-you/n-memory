"""Offline differential falsifiers for the vector-lane parity behaviours.

The parity referee runs strictly offline (its embedding lane is deliberately
inert), so two pin behaviours are unreachable from its fixtures. These tests
drive the port's lanes directly — a hooked embedding client plus seeded
pgvector rows, no provider — and pin the port to the semantics:

A. The recall-miss ledger is keyed on the TERM lane's own observation
   (pin retrieve.rs:1793-1803): a response grounded by the vector lane still
   records the term lane's abstain / missing_evidence miss, one folded row
   per RAW caller term (pin store.rs:5036-5066).
B. nmemory_context_show FAILS CLOSED when the semantic lane surfaces a tombstoned
   capsule (pin context.rs:296-312 -> support.rs:192-194): forgetting keeps a
   NULL-content marker row (pin store.rs:4565), get() raises (store.rs:1770),
   and the tool answers -32603 with the exact message instead of
   serving the neighbours.
"""

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
from nmemory.repositories.capsules import Tombstoned
from nmemory.retrieve import RetrieveParams, retrieve
from nmemory.server import MemoryServer
from nmemory.space import DIMENSION
from nmemory.store import Store
from nmemory.tools import context as context_tool
from nmemory.tools import get as get_tool

NOW = datetime(2026, 8, 27, 12, 0, 0, tzinfo=UTC)
UNIT_VEC: list[float] = [1.0] + [0.0] * (DIMENSION - 1)


def cap_at(content: str, project: str, anchor: str) -> Capsule:
    return Capsule(
        content=content,
        provenance=Provenance(
            source="test",
            anchor=anchor,
            source_hash=sha256_hex(content.encode("utf-8")),
        ),
        confidence=0.9,
        freshness=Freshness(valid_from=NOW),
        scope=Scope(project_id=project),
        authority_class=AuthorityClass.USER_STATED,
        instruction_taint=False,
    )


def hooked_client() -> EmbeddingClient:
    return EmbeddingClient(custom_hook=lambda text: list(UNIT_VEC))


def seed_embedding(store: Store, cid: str, client: EmbeddingClient) -> None:
    put_embedding(
        store.db,
        cid,
        list(UNIT_VEC),
        client.space_fingerprint.as_str(),
        sha256_hex(cid.encode("utf-8")),
    )


def test_term_lane_miss_recorded_despite_vector_grounding() -> None:
    store = Store.open_disposable_test_store()
    cid, _ = store.append(
        cap_at("Vector neighbour capsule about routing tables.", "nmemory", "v:1"),
        NOW,
    )
    store.set_birth_classification(cid, "task", "project", NOW)
    client = hooked_client()
    seed_embedding(store, cid, client)

    res = retrieve(
        store,
        # Include tasks explicitly: this regression measures the lane ledger.
        RetrieveParams(
            query="zzzunmatchedterm", project_id="nmemory", exclude_kinds=[]
        ),
        embeddings=client,
        now=NOW,
    )

    # The vector lane grounds the response...
    assert res["outcome"] == "grounded"
    # ...and the term lane's abstain miss is recorded anyway, with the raw
    # caller term as its own folded ledger row.
    assert store.traffic.recall_miss_terms() == [("zzzunmatchedterm", 1)]


def test_term_lane_miss_one_row_per_caller_term() -> None:
    store = Store.open_disposable_test_store()
    cid, _ = store.append(
        cap_at("Second vector neighbour capsule about queues.", "nmemory", "v:2"),
        NOW,
    )
    store.set_birth_classification(cid, "task", "project", NOW)
    client = hooked_client()
    seed_embedding(store, cid, client)

    res = retrieve(
        store,
        # Include tasks explicitly: this regression measures the lane ledger.
        RetrieveParams(query="alpha beta", project_id="nmemory", exclude_kinds=[]),
        embeddings=client,
        now=NOW,
    )

    assert res["outcome"] == "grounded"
    # One row per RAW caller term — never one row for the joined query string.
    assert store.traffic.recall_miss_terms() == [("alpha", 1), ("beta", 1)]


def test_context_fails_closed_on_tombstoned_semantic_candidate() -> None:
    store = Store.open_disposable_test_store()
    store.append(
        cap_at("PostgreSQL connection pooling guidance.", "nmemory", "t:1"), NOW
    )
    store.capsules.forget_capsule("cap-1", "purged", "probe", b"k" * 32, NOW)
    # Stale embedding row the pruning worker has not reached yet (pin leaves
    # capsule_embeddings alone on forget) — the semantic lane surfaces cap-1.
    client = hooked_client()
    seed_embedding(store, "cap-1", client)
    server = MemoryServer(
        store=store, embeddings=client, anchor_root=".", project_id="nmemory"
    )

    res = context_tool.handle(
        server.handler_state(),
        {"query": "PostgreSQL", "sources": ["memory"]},
        None,
        NOW,
        "test",
    )

    assert "error" not in res, res
    body = json.loads(res["content"][0]["text"])
    assert body["label"] == "ADVISORY_NOT_AUTHORITY"
    assert body["excluded"] == {"tombstoned": ["cap-1"]}
    assert body["results"] == []


def test_tombstone_marker_contract_survives_null_row_model() -> None:
    store = Store.open_disposable_test_store()
    store.append(
        cap_at("Marker contract capsule about wal archives.", "nmemory", "m:1"), NOW
    )
    store.capsules.forget_capsule("cap-1", "purged", "probe", b"k" * 32, NOW)
    server = MemoryServer(store=store, anchor_root=".")

    # get() fails closed on the NULL-content marker row with opaque message (M5)
    with pytest.raises(Tombstoned) as exc:
        store.capsules.get("cap-1")
    assert str(exc.value) == (
        "capsule is tombstoned; the content is gone, only the marker remains"
    )
    # ...the list-based surfaces never see the marker...
    assert store.capsules.list() == []
    # ...and nmemory_capsule_show still renders the tombstone marker, not a crash.
    res = get_tool.handle(server.handler_state(), {"id": "cap-1"}, None, NOW, "test")
    body = json.loads(res["content"][0]["text"])
    assert body["id"] == "cap-1"
    assert body["outcome"] == "tombstoned"
    assert body["content_hmac"].startswith("hmac-sha256:")
