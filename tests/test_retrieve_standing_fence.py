"""The `caducou` (lapsed) verdict of `standing_of` is a retrieve fence, not just a report."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from storefile import fresh_store_path, sample_capsule_id

from nmemory.capsule import format_rfc3339
from nmemory.queries.standing import standing_of
from nmemory.retrieve import RetrieveParams, retrieve
from nmemory.server import MemoryServer
from nmemory.store import Store


def _ingest(server: MemoryServer, content: str, anchor: str, kind: str) -> None:
    response = server.call_tool(
        "nmemory_capsule_ingest",
        {
            "content": content,
            "source": "test_retrieve_standing_fence",
            "anchor": anchor,
            "proof_hint": "fixture: a porta de captura exige prova re-obtenível",
            "kind": kind,
        },
    )
    assert not response["result"]["isError"], response


def test_lapsed_standing_is_fenced_by_default() -> None:
    url = fresh_store_path("standingfence")
    store = Store.open(url)
    server = MemoryServer(store=store)
    _ingest(server, "standingprobe caducada", "stale", "fact")
    _ingest(server, "standingprobe viva", "fresh", "fact")
    now = datetime.now(UTC)
    store.anchors.record_verification(
        "cap-1",
        kind="staleness",
        result="stale_condition_met",
        detail="the condition the capsule itself declared has fired",
        actor="varredura",
        now=now,
    )
    store.anchors.record_verification(
        sample_capsule_id(2),
        kind="anchor",
        result="pass",
        detail="a linha ancorada diz o mesmo",
        actor="varredura",
        now=now,
    )
    standing = standing_of(store.db, ["cap-1", sample_capsule_id(2)], now=now)
    assert standing["cap-1"]["state"] == "caducou", standing
    assert standing[sample_capsule_id(2)]["state"] == "vale", standing

    fenced = retrieve(store, RetrieveParams(query="standingprobe", lane="term"))
    assert [e["id"] for e in fenced["evidence"]] == [sample_capsule_id(2)]
    assert fenced["exclusions"]["lapsed"] == 1

    # Hidden, not deleted: explicit filter returns it.
    opted = retrieve(
        store, RetrieveParams(query="standingprobe", lane="term", lapsed=True)
    )
    assert sorted(e["id"] for e in opted["evidence"]) == [
        "cap-1",
        sample_capsule_id(2),
    ]
    assert "lapsed" not in opted["exclusions"]

    # And direct addressing was never a fence.
    got = server.call_tool("nmemory_capsule_show", {"id": "cap-1"})
    assert not got["result"]["isError"], got

    # THROUGH THE PUBLIC DOOR, which is where the caller lives: a test that only passes
    # `RetrieveParams` measures the engine and not the door, and the door is precisely where
    # `include_staged` proved that a fence without escape remains unreachable.
    wire = server.call_tool(
        "nmemory_capsule_retrieve", {"terms": ["standingprobe"], "lane": "term"}
    )
    assert not wire["result"]["isError"], wire
    served = json.loads(wire["result"]["content"][0]["text"])
    assert [r["id"] for r in served["results"]] == [sample_capsule_id(2)]
    assert served["excluded"]["lapsed"] == 1

    escape = server.call_tool(
        "nmemory_capsule_retrieve",
        {"terms": ["standingprobe"], "lane": "term", "include_lapsed": True},
    )
    assert not escape["result"]["isError"], escape
    opened = json.loads(escape["result"]["content"][0]["text"])
    assert sorted(r["id"] for r in opened["results"]) == [
        "cap-1",
        sample_capsule_id(2),
    ]


def test_the_standing_fence_reads_the_belief_clock() -> None:
    """`known_at`: standing is what it was at the requested belief, not today.

    The discriminator is a capsule whose `valid_to` has passed NOW but had not
    yet passed at the belief instant. The neighboring fence (`expired`) measures it by
    the same `ref_freshness` and lets it pass; if this queried standing with the
    wall clock, the same capsule would leave immediately after as `lapsed` — two
    fences of the same loop disagreeing on what time it is.
    """
    url = fresh_store_path("standingrelogio")
    store = Store.open(url)
    server = MemoryServer(store=store)
    now = datetime.now(UTC)
    response = server.call_tool(
        "nmemory_capsule_ingest",
        {
            "content": "standingrelogio observation with a validity",
            "source": "test_retrieve_standing_fence",
            "anchor": "clock",
            "proof_hint": "fixture: a porta de captura exige prova re-obtenível",
            "kind": "fact",
            "valid_from": format_rfc3339(now - timedelta(days=5)),
            "valid_to": format_rfc3339(now - timedelta(days=1)),
        },
    )
    assert not response["result"]["isError"], response
    # The capsule must EXIST at the belief instant, otherwise what excludes it is the
    # `future_dated` fence and this test would cease to measure standing's clock.
    with store.db.writing() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE capsules SET created_at = ?",
            (format_rfc3339(now - timedelta(days=5)),),
        )
    crenca = format_rfc3339(now - timedelta(days=3))
    assert standing_of(store.db, ["cap-1"], now=now)["cap-1"] == {
        "state": "caducou",
        "reason": "valid_to",
    }, "today it IS expired — that is what makes the clock observable"

    served = retrieve(
        store,
        RetrieveParams(query="standingrelogio", lane="term", known_at=crenca),
    )
    assert [e["id"] for e in served["evidence"]] == ["cap-1"]
    assert "lapsed" not in served["exclusions"]


def test_work_kinds_have_no_standing_to_lapse() -> None:
    """`STANDING_EXEMPT_KINDS`: a `task` has no standing, so does not lapse here."""
    url = fresh_store_path("standingexempt")
    store = Store.open(url)
    server = MemoryServer(store=store)
    _ingest(server, "standingexemptprobe trabalho", "work", "task")
    now = datetime.now(UTC)
    store.anchors.record_verification(
        "cap-1",
        kind="staleness",
        result="stale_condition_met",
        detail="the same event that expires a claim about the world",
        actor="varredura",
        now=now,
    )
    assert standing_of(store.db, ["cap-1"], now=now) == {}

    served = retrieve(store, RetrieveParams(query="standingexemptprobe", lane="term"))
    assert [e["id"] for e in served["evidence"]] == ["cap-1"]
    assert "lapsed" not in served["exclusions"]
