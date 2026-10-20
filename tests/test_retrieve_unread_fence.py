"""The SECOND decay clock: the last READ, and not the last verification."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from storefile import fresh_store_path, sample_capsule_id

from nmemory.capsule import format_rfc3339
from nmemory.queries.census import last_read_among
from nmemory.retrieve import RetrieveParams, retrieve
from nmemory.server import MemoryServer
from nmemory.store import Store


def _ingest(server: MemoryServer, content: str, anchor: str, kind: str) -> None:
    response = server.call_tool(
        "nmemory_capsule_ingest",
        {
            "content": content,
            "source": "test_retrieve_unread_fence",
            "anchor": anchor,
            "proof_hint": "fixture: a porta de captura exige prova re-obtenível",
            "kind": kind,
        },
    )
    assert not response["result"]["isError"], response


def _served(store: Store, **kwargs: object) -> list[str]:
    result = retrieve(store, RetrieveParams(query="unreadprobe", lane="term", **kwargs))  # type: ignore[arg-type]
    return sorted(e["id"] for e in result["evidence"])


def test_unread_capsules_stop_competing() -> None:
    url = fresh_store_path("unreadfence")
    store = Store.open(url)
    server = MemoryServer(store=store)
    _ingest(server, "unreadprobe adormecida", "sleep", "fact")
    _ingest(server, "unreadprobe lida ontem", "read", "fact")
    _ingest(server, "unreadprobe pinada", "pin", "fact")
    _ingest(server, "unreadprobe trabalho", "work", "task")
    now = datetime.now(UTC)
    with store.db.writing() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE capsules SET created_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '-90 days')"
        )
    # `get` IS NOT in RANKING_SURFACES: the fence uses the same union without surface
    # filtering as `write_read_census` — a read is a read, regardless of what counts for ranking.
    store.db.record_read_traffic(
        surface="get",
        session_id="sess-test",
        terms="unreadprobe",
        served_ids=[sample_capsule_id(2)],
        project_id=None,
        project_prefix=None,
        outcome="grounded",
        now=now - timedelta(days=1),
    )
    store.pins.set_pin(
        sample_capsule_id(3), True, "the pin is a claim by the owner", "owner", now
    )

    assert _served(store) == [sample_capsule_id(2), sample_capsule_id(3)]
    fenced = retrieve(store, RetrieveParams(query="unreadprobe", lane="term"))
    # The fence covers ALL kinds: `task` escapes the standing fence because it has
    # no standing at all, and it is exactly what this clock reaches.
    assert fenced["exclusions"]["unread"] == 2

    # Reading wakes: state derived from reading, never a separate write.
    store.traffic.record_recall("cap-1", now)
    assert _served(store) == ["cap-1", sample_capsule_id(2), sample_capsule_id(3)]

    assert _served(store, unread_days=None) == [
        "cap-1",
        sample_capsule_id(2),
        sample_capsule_id(3),
        sample_capsule_id(4),
    ]


def test_unread_threshold_is_a_parameter() -> None:
    url = fresh_store_path("unreadlimiar")
    store = Store.open(url)
    server = MemoryServer(store=store)
    _ingest(server, "unreadprobe vinte dias", "twenty", "fact")
    with store.db.writing() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE capsules SET created_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '-20 days')"
        )
    # The tight threshold FIRST: serving records `record_recall`, and a read
    # wakes the capsule for the subsequent call.
    assert _served(store, unread_days=14) == []
    assert _served(store) == ["cap-1"], "20 days is inside the default of 30"


def test_a_revoked_read_does_not_keep_a_capsule_awake() -> None:
    """`revoked_at IS NULL` in the union: a revoked transcript read also unwakes.

    The retired transcript backfill MARKED its undone rows instead of deleting
    them (LOCK 2: autonomous means reversible), and the store still holds them,
    so without this fence a capsule whose sole read was revoked would remain
    awake forever — exactly the class of defect that the fence exists to catch.
    """
    url = fresh_store_path("unreadrevog")
    store = Store.open(url)
    server = MemoryServer(store=store)
    _ingest(server, "unreadprobe revogada", "revoked", "fact")
    now = datetime.now(UTC)
    with store.db.writing() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE capsules SET created_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '-90 days')"
        )
    store.db.record_read_traffic(
        surface="context",
        session_id="sess-test",
        terms="unreadprobe",
        served_ids=["cap-1"],
        project_id=None,
        project_prefix=None,
        outcome="grounded",
        now=now - timedelta(days=1),
    )
    with store.db.writing() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE read_traffic SET origin = ?, source_ref = ?, revoked_at = ?",
            ("transcript:teste", "toolu_x", now.isoformat()),
        )
        assert cur.rowcount == 1

    fenced = retrieve(store, RetrieveParams(query="unreadprobe", lane="term"))
    assert fenced["evidence"] == []
    assert fenced["exclusions"]["unread"] == 1
    with store.db.writing() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM read_traffic")
        assert int(cur.fetchone()[0]) == 1, "a linha continua lá, marcada"


def test_idle_exactly_at_the_threshold_still_competes() -> None:
    """`>` and not `>=`: N days idle is the limit, not already the other side of it.

    The clock is injected because the boundary is only observable to the second: with
    `now` read inside the engine, the time elapsed between preparing the fixture and
    measuring always pushes the age past the threshold and both forms agree.
    """
    url = fresh_store_path("unreadborda")
    store = Store.open(url)
    server = MemoryServer(store=store)
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    response = server.call_tool(
        "nmemory_capsule_ingest",
        {
            "content": "unreadprobe na borda",
            "source": "test_retrieve_unread_fence",
            "anchor": "edge",
            "proof_hint": "fixture: a porta de captura exige prova re-obtenível",
            "kind": "fact",
            # `valid_from` at `t0` because the injected clock is BEFORE the
            # actual ingestion instant: without this, it is excluded by `future_dated`
            # and the threshold edge is never measured.
            "valid_from": format_rfc3339(t0),
        },
    )
    assert not response["result"]["isError"], response
    with store.db.writing() as conn, conn.cursor() as cur:
        cur.execute("UPDATE capsules SET created_at = ?", (format_rfc3339(t0),))
    params = RetrieveParams(query="unreadprobe", lane="term", unread_days=20)

    # One second AFTER the threshold: out. First, because it serves nothing and
    # records no recall.
    over = retrieve(store, params, now=t0 + timedelta(days=20, seconds=1))
    assert over["exclusions"]["unread"] == 1

    # EXACTLY at the threshold: in.
    edge = retrieve(store, params, now=t0 + timedelta(days=20))
    assert [e["id"] for e in edge["evidence"]] == ["cap-1"]


def test_the_fence_and_its_two_escapes_exist_at_the_mcp_door() -> None:
    """Through the DOOR, not via `RetrieveParams`: that is where the MCP caller lives.

    Without this, the fence was delivered capability left unplugged — the same defect that
    `include_staged`, `min_confidence`, and the two relevance floors already suffered
    in this schema file.
    """
    url = fresh_store_path("unreadporta")
    store = Store.open(url)
    server = MemoryServer(store=store)
    _ingest(server, "unreadprobe pela porta", "door", "fact")
    with store.db.writing() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE capsules SET created_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '-90 days')"
        )

    def _call(**extra: object) -> dict[str, Any]:
        response = server.call_tool(
            "nmemory_capsule_retrieve",
            {"terms": ["unreadprobe"], "lane": "term", **extra},
        )
        assert not response["result"]["isError"], response
        loaded: dict[str, Any] = json.loads(response["result"]["content"][0]["text"])
        return loaded

    fenced = _call()
    # With nothing surviving, the envelope is `missing_evidence` and carries no `results`
    # — and carries the NAMED fence, which is what distinguishes this from an `abstain`.
    assert fenced["outcome"] == "missing_evidence"
    assert fenced["excluded"]["unread"] == 1

    # The ESCAPE is boolean and not an `unread_days: null`: after `args.get` a
    # `null` on the wire is indistinguishable from "sent nothing".
    assert [r["id"] for r in _call(include_unread=True)["results"]] == ["cap-1"]

    # And the TUNER is another field. `120` serves the 90-day capsule; the previous call
    # woke it, so this one measures the threshold against a read from now.
    assert [r["id"] for r in _call(unread_days=120)["results"]] == ["cap-1"]

    refused = server.call_tool(
        "nmemory_capsule_retrieve", {"terms": ["unreadprobe"], "unread_days": -1}
    )
    assert refused["error"]["code"] == -32602, refused


def test_uma_leitura_REVOGADA_nao_acorda_a_capsula() -> None:
    """Revoked traffic is not a read, and therefore does not disable the fence.

    The M14 mutant (the union ignoring `revoked_at`) SURVIVED the suite: the
    product already filtered `rt.revoked_at IS NULL` in `last_read_among`, but
    no falsifier contained the condition, so the fence passed on both
    sides. The condition is not hypothetical: traffic backfill revokes its
    rows in `--undo` and restores them (`DO UPDATE SET revoked_at = NULL`) when
    re-run, precisely so that undo is not a one-way street.

    Without this filter, a capsule whose ONLY read was later revoked remained
    awake forever and the U2 fence never touched it again.
    """
    url = fresh_store_path("unreadrevoked")
    store = Store.open(url)
    server = MemoryServer(store=store)
    _ingest(server, "unreadprobe read only by a revoked line", "revoked", "fact")
    now = datetime.now(UTC)
    with store.db.writing() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE capsules SET created_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '-90 days')"
        )
    store.db.record_read_traffic(
        surface="get",
        session_id="sess-test",
        terms="unreadprobe",
        served_ids=["cap-1"],
        project_id=None,
        project_prefix=None,
        outcome="grounded",
        now=now - timedelta(days=1),
    )

    # DO NOT serve before revoking. Retrieve records `record_recall` on each id
    # it returns, and that row goes to `nmemory.usage`, which DOES NOT have
    # `revoked_at`: a probe read here would wake the capsule via the other
    # branch of the union and kill the discriminator below. The pair that makes this a
    # discriminator is `last_read_among` below, which measures the union without serving it.
    assert last_read_among(store.db, ["cap-1"]) != {}

    with store.db.writing() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE read_traffic SET revoked_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
        )

    assert last_read_among(store.db, ["cap-1"]) == {}

    assert _served(store) == []
    fenced = retrieve(store, RetrieveParams(query="unreadprobe", lane="term"))
    assert fenced["exclusions"]["unread"] == 1
    # And remains reachable with the fence disabled (`include_unread` at the MCP
    # door translates to this `unread_days=None`), like the other fences.
    assert _served(store, unread_days=None) == ["cap-1"]
