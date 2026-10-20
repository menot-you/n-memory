"""Count work-state exclusions and scope drops on real vector candidates."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from storefile import fresh_store_path, put_embedding, sample_capsule_id

from nmemory.embedding import EmbeddingClient
from nmemory.relation import RelationKind
from nmemory.retrieve import InvalidExcludedKinds, RetrieveParams, retrieve
from nmemory.server import MemoryServer
from nmemory.space import DIMENSION
from nmemory.store import Store


@pytest.mark.parametrize("bad", ["task", [1], [[]], ["not-a-kind"]])
def test_invalid_kind_filters_refuse_at_construction(bad: Any) -> None:
    with pytest.raises(InvalidExcludedKinds, match="exclude_kinds"):
        RetrieveParams(query="probe", exclude_kinds=bad)


def test_default_kind_fence_and_per_call_escape() -> None:
    url = fresh_store_path("kindfence")
    store = Store.open(url)
    server = MemoryServer(store=store)
    for kind in ("task", "dod"):
        response = server.call_tool(
            "nmemory_capsule_ingest",
            {
                "content": f"workstateprobe {kind}",
                "source": "test_kind_fence",
                "anchor": kind,
                "proof_hint": "fixture: a porta de captura exige prova re-obtenível",
                "kind": kind,
            },
        )
        assert not response["result"]["isError"], response
    response = server.call_tool(
        "nmemory_capsule_retrieve", {"terms": ["workstateprobe"], "lane": "term"}
    )
    fenced = json.loads(response["result"]["content"][0]["text"])
    # `task` left the fence (measured: the most-served kind in the live store,
    # 43% of its read capsules read across days), so the task survives and only
    # the dod is excluded. The outcome is `grounded` precisely BECAUSE something
    # survived — under the old two-kind fence both were cut and the honest
    # answer degraded to `missing_evidence`.
    assert fenced["outcome"] == "grounded"
    assert fenced["excluded"]["kind_excluded"] == 1
    assert [r["id"] for r in fenced["results"]] == ["cap-1"], "the task, not the dod"
    escaped = retrieve(
        store, RetrieveParams(query="workstateprobe", lane="term", exclude_kinds=[])
    )
    assert len(escaped["evidence"]) == 2
    wire = server.call_tool(
        "nmemory_capsule_retrieve",
        {"terms": ["workstateprobe"], "lane": "term", "exclude_kinds": []},
    )
    assert not wire["result"]["isError"], wire
    assert len(json.loads(wire["result"]["content"][0]["text"])["results"]) == 2
    invalid = server.call_tool(
        "nmemory_capsule_retrieve",
        {"terms": ["workstateprobe"], "exclude_kinds": ["not-a-kind"]},
    )
    assert invalid["error"]["code"] == -32602
    topic = retrieve(
        store, RetrieveParams(query="workstateprobe", lane="term", topic_set=["cap-1"])
    )
    assert [row["id"] for row in topic["evidence"]] == ["cap-1"]
    got = server.call_tool("nmemory_capsule_show", {"id": "cap-1"})
    assert not got["result"]["isError"], got
    assert store.classifications.kind_ids_among(
        ["task"], ["cap-1", sample_capsule_id(2)]
    ) == {"cap-1"}
    assert (
        store.classifications.kind_ids_among(["task"], [sample_capsule_id(2)]) == set()
    )
    assert store.classifications.kind_ids_among([], ["cap-1"]) == set()
    assert store.classifications.kind_ids_among(["task"], []) == set()


@pytest.mark.parametrize(
    ("fence", "reason"),
    [
        ({"session_id": "different"}, "out_of_session"),
        ({"effort_set": []}, "outside_effort"),
        ({"topic_set": []}, "outside_topic"),
    ],
)
def test_vector_scope_exclusions_are_counted(
    fence: dict[str, Any], reason: str
) -> None:
    url = fresh_store_path("scopefence")
    store = Store.open(url)
    response = MemoryServer(store=store).call_tool(
        "nmemory_capsule_ingest",
        {
            "content": "vector scope candidate",
            "source": "test_kind_fence",
            "anchor": "scope",
            "proof_hint": "fixture: a porta de captura exige prova re-obtenível",
            "kind": "fact",
        },
    )
    assert not response["result"]["isError"], response
    unit = [1.0] + [0.0] * (DIMENSION - 1)
    client = EmbeddingClient(custom_hook=lambda text: unit)
    put_embedding(store.db, "cap-1", unit, client.space_fingerprint.as_str(), "a" * 64)
    result = retrieve(
        store, RetrieveParams(query="scope", lane="vector", **fence), embeddings=client
    )
    assert result["outcome"] == "missing_evidence"
    assert result["total_candidates"] == 1
    assert result["exclusions"] == {reason: 1}


def test_kind_fence_precedes_the_situation_fences() -> None:
    """Position is the product here, and exactly one state can see it.

    The PLAN pins the kind fence as the FIRST gate of the loop, before
    `store.get_tier`. Nothing distinguished that from any later placement: a
    mutant that drops the three lines below the tier and superseded fences
    survives every other test in this file. The two orders disagree on ONE
    capsule — one that is both a fenced KIND and already SUPERSEDED — and no
    test ingested one. Kind is a claim about the PLANE a capsule lives in,
    upstream of its situation, so `kind_excluded` is the honest reason and
    `superseded` is the mutant's.
    """
    url = fresh_store_path("fenceorder")
    store = Store.open(url)
    server = MemoryServer(store=store)
    for anchor in ("old", "new"):
        response = server.call_tool(
            "nmemory_capsule_ingest",
            {
                "content": f"fenceorderprobe {anchor}",
                "source": "test_kind_fence",
                "anchor": anchor,
                "proof_hint": "fixture: a porta de captura exige prova re-obtenível",
                # `dod`, not `task`: this test proves the fence ORDER, and the
                # kind is only its vehicle. `task` left the fence, so using it
                # here would leave the test asserting nothing.
                "kind": "dod",
            },
        )
        assert not response["result"]["isError"], response
    store.add_relation(
        RelationKind.SUPERSEDES,
        from_id=sample_capsule_id(2),
        to_id="cap-1",
        now=datetime.now(UTC),
    )
    assert store.relations.superseded_among(["cap-1", sample_capsule_id(2)]) == {
        "cap-1"
    }

    result = retrieve(store, RetrieveParams(query="fenceorderprobe", lane="term"))
    assert result["outcome"] == "missing_evidence"
    # Both by KIND. With the fence moved below, cap-1 would answer `superseded`.
    assert result["exclusions"] == {"kind_excluded": 2}
