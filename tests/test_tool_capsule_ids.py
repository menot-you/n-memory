"""Every tool that stores a capsule id names a stored capsule.

`nmemory_capsule_pin`, `nmemory_capsule_reanchor` and
`nmemory_outcome_record` stored the id they were given without resolving it: a typo
or a stale id was recorded as a pin, an anchor correction or an
outcome about nothing, and an id passed as a handle was stored under the handle,
which every reader (keyed by canonical id) misses. Each id is now resolved inside
the tool's transaction, stored in canonical form, and an id that names no stored
capsule is refused with nothing written.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from storefile import default_store_path, sample_capsule_id

from nmemory.capture import CaptureContext
from nmemory.server import MemoryServer
from nmemory.store import Store
from nmemory.tools.errors import unknown_capsule

T0 = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


def _ctx() -> CaptureContext:
    return CaptureContext(
        harness="pytest", model="none", cwd="/tmp", session_id="s", host="t", owner="t"
    )


def _fresh() -> tuple[Store, MemoryServer]:
    store = Store.open(default_store_path("pinprefids"))
    return store, MemoryServer(
        store=store, project_id="ids", hmac_env_key=b"pin-preference-ids-key"
    )


def _ingest(server: MemoryServer, content: str, *, kind: str = "task") -> str:
    args = {
        "content": content,
        "kind": kind,
        "source": "t",
        "anchor": "ids/x",
        "project_id": "ids",
    }
    if kind == "failure_pattern":
        args["source_quote"] = content
    r = server.call_tool("nmemory_capsule_ingest", args, context=_ctx(), now=T0)
    assert not r["result"]["isError"], r
    outcome = json.loads(r["result"]["content"][0]["text"])["outcomes"][0]
    assert "id" in outcome, outcome
    return str(outcome["id"])


def _rows(store: Store, table: str) -> int:
    with store.db.writing() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {table}")
        row = cur.fetchone()
    return int(row[0]) if row else 0


def _call(server: MemoryServer, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    return server.call_tool(tool, args, context=_ctx(), now=T0)


def _body(res: dict[str, Any]) -> dict[str, Any]:
    assert "result" in res and not res["result"]["isError"], res
    body: dict[str, Any] = json.loads(res["result"]["content"][0]["text"])
    return body


def test_pin_accepts_failure_pattern_short_id_and_full_handle() -> None:
    store, server = _fresh()
    cid = _ingest(server, "a recorded failure pattern", kind="failure_pattern")
    handle = store.capsules.get_handle(cid)
    short = f"failure-pattern-{cid.removeprefix('cap-')}"

    for reference, pinned in ((short, True), (handle, False)):
        body = _body(
            _call(
                server,
                "nmemory_capsule_pin",
                {"id": reference, "pinned": pinned, "reason": "r"},
            )
        )
        assert body["id"] == cid
        assert store.pins.get_pin(cid) is pinned


def test_pin_refuses_a_slug_only_reference_shared_by_two_capsules() -> None:
    store, server = _fresh()
    same_prefix = "a long identical capsule headline with enough words to truncate the handle slug "
    first = _ingest(server, same_prefix + "one")
    second = _ingest(server, same_prefix + "two")
    first_handle = store.capsules.get_handle(first)
    second_handle = store.capsules.get_handle(second)
    first_slug = first_handle.split("-", 2)[2]
    second_slug = second_handle.split("-", 2)[2]
    assert first_slug == second_slug
    alias = f"task-{first_slug}"
    pin_events = _rows(store, "pin_events")

    response = _call(
        server, "nmemory_capsule_pin", {"id": alias, "pinned": True, "reason": "r"}
    )
    assert response["error"]["code"] == -32002, response
    assert _rows(store, "pin_events") == pin_events


def test_pin_resolves_a_unique_legacy_slug_alias() -> None:
    store, server = _fresh()
    cid = _ingest(server, "a unique slug alias for the historical handle")
    handle = store.capsules.get_handle(cid)
    alias = "task-" + handle.split("-", 2)[2]
    assert store.capsules.resolve_id(alias) == cid

    body = _body(
        _call(
            server, "nmemory_capsule_pin", {"id": alias, "pinned": True, "reason": "r"}
        )
    )
    assert body["id"] == cid
    assert store.pins.get_pin(cid) is True


def test_canonical_id_resolves_when_an_old_capsule_has_no_handle() -> None:
    store, server = _fresh()
    cid = _ingest(server, "a capsule created before handles existed")
    with store.db.writing() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM capsule_handles WHERE capsule_id = ?", (cid,))

    assert store.capsules.resolve_id(cid) == cid


def test_pin_refuses_an_id_that_names_no_capsule_and_writes_nothing() -> None:
    store, server = _fresh()
    _ingest(server, "a stored capsule so the store is not empty")
    pins, audits = _rows(store, "pin_events"), _rows(store, "audit_events")

    for pinned in (True, False):
        res = _call(
            server,
            "nmemory_capsule_pin",
            {"id": sample_capsule_id(999999), "pinned": pinned, "reason": "typo"},
        )
        assert res["error"]["code"] == -32002, res
        assert res["error"]["data"] == {
            "kind": "unknown_capsule",
            "id": sample_capsule_id(999999),
        }
        assert "nmemory_capsule_retrieve" in res["error"]["message"]
        assert "nmemory_capsule_list" in res["error"]["message"]

    assert _rows(store, "pin_events") == pins
    assert _rows(store, "audit_events") == audits


def test_unknown_capsule_error_payload_names_both_discovery_tools() -> None:
    error = unknown_capsule("nmemory_capsule_pin", sample_capsule_id(999999))
    assert error["jsonrpc"] == "2.0"
    assert error["error"]["code"] == -32002
    assert error["error"]["data"] == {
        "kind": "unknown_capsule",
        "id": sample_capsule_id(999999),
    }
    assert (
        "(cap-<n>, short id or handle); find it with nmemory_capsule_retrieve "
        "or nmemory_capsule_list"
    ) in error["error"]["message"]


def test_each_unknown_capsule_refusal_names_the_tool_that_refused_it() -> None:
    _store, server = _fresh()
    missing = sample_capsule_id(999999)
    calls = (
        ("nmemory_capsule_pin", {"id": missing, "pinned": True, "reason": "r"}),
        (
            "nmemory_capsule_reanchor",
            {"capsule_id": missing, "anchor": "ids/y", "reason": "r"},
        ),
        (
            "nmemory_outcome_record",
            {"capsule_id": missing, "description": "d", "actor": "owner"},
        ),
        (
            "nmemory_proposal_review",
            {"capsule_id": missing, "verdict": "ratified", "actor": "owner"},
        ),
    )
    for tool, args in calls:
        res = _call(server, tool, args)
        assert res["error"]["code"] == -32002, (tool, res)
        assert res["error"]["message"].startswith(
            f"unknown capsule {missing!r}: {tool} takes the id of a stored capsule"
        ), (tool, res)


def test_pin_by_handle_is_stored_under_the_canonical_id() -> None:
    store, server = _fresh()
    cid = _ingest(server, "the capsule pinned through its handle")
    handle = store.capsules.get_handle(cid)
    assert handle != cid

    body = _body(
        _call(
            server, "nmemory_capsule_pin", {"id": handle, "pinned": True, "reason": "r"}
        )
    )

    assert body["id"] == cid
    assert store.pins.get_pin(cid) is True

    short = "-".join(handle.split("-", 2)[:2])
    body = _body(
        _call(
            server, "nmemory_capsule_pin", {"id": short, "pinned": False, "reason": "r"}
        )
    )
    assert body["id"] == cid
    assert store.pins.get_pin(cid) is False


def test_reanchor_refuses_an_id_that_names_no_capsule_and_writes_nothing() -> None:
    store, server = _fresh()
    _ingest(server, "a stored capsule so the store is not empty")
    rebinds, audits = _rows(store, "anchor_rebind"), _rows(store, "audit_events")

    res = _call(
        server,
        "nmemory_capsule_reanchor",
        {
            "capsule_id": sample_capsule_id(999999),
            "anchor": "ids/y",
            "reason": "typo",
        },
    )
    assert res["error"]["code"] == -32002, res
    assert res["error"]["data"] == {
        "kind": "unknown_capsule",
        "id": sample_capsule_id(999999),
    }
    assert "nmemory_capsule_retrieve" in res["error"]["message"]

    assert _rows(store, "anchor_rebind") == rebinds
    assert _rows(store, "audit_events") == audits


def test_reanchor_by_handle_rebinds_the_canonical_id() -> None:
    store, server = _fresh()
    cid = _ingest(server, "the capsule reanchored through its handle")
    handle = store.capsules.get_handle(cid)
    assert handle != cid

    args = {"capsule_id": handle, "anchor": "ids/moved", "reason": "r"}
    body = _body(_call(server, "nmemory_capsule_reanchor", args))
    assert body["capsule_id"] == cid
    assert store.anchors.get_reanchor(cid) == "ids/moved"

    short = "-".join(handle.split("-", 2)[:2])
    args = {"capsule_id": short, "anchor": "ids/again", "reason": "r"}
    body = _body(_call(server, "nmemory_capsule_reanchor", args))
    assert body["capsule_id"] == cid
    assert store.anchors.get_reanchor(cid) == "ids/again"


def test_outcome_refuses_an_unknown_capsule_id_and_writes_nothing() -> None:
    store, server = _fresh()
    _ingest(server, "a stored capsule so the store is not empty")
    outcomes, audits = _rows(store, "outcomes"), _rows(store, "audit_events")

    res = _call(
        server,
        "nmemory_outcome_record",
        {
            "description": "d",
            "actor": "owner",
            "capsule_id": sample_capsule_id(999999),
        },
    )
    assert res["error"]["code"] == -32002, res
    assert res["error"]["data"] == {
        "kind": "unknown_capsule",
        "id": sample_capsule_id(999999),
    }

    assert _rows(store, "outcomes") == outcomes
    assert _rows(store, "audit_events") == audits


def test_outcome_records_the_canonical_id_and_needs_none() -> None:
    store, server = _fresh()
    cid = _ingest(server, "the capsule an outcome bears on")

    args = {
        "description": "d",
        "actor": "owner",
        "capsule_id": store.capsules.get_handle(cid),
    }
    body = _body(_call(server, "nmemory_outcome_record", args))
    assert body["recorded"]["capsule_id"] == cid
    assert body["advisory"].endswith(
        "(receipt_id + score) is stored with the row and changes no ranking."
    )

    body = _body(
        _call(server, "nmemory_outcome_record", {"description": "d", "actor": "owner"})
    )
    assert "capsule_id" not in body["recorded"]
