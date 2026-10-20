"""Forward paging of `nmemory_capsule_list`: the opt-in, the binding and the walk.

`limit` keeps the NEWEST tail of the whole fenced range, so a consumer that has to
read a WHOLE session or project could never reach past one response: the older
rows were addressable by no limit at all. Paging is the forward reader for that
consumer, and it is OPT-IN — a request without `page_size` is answered exactly as
before, which `test_an_unpaged_answer_is_what_head_answered` pins to the bytes
recorded from the tree BEFORE paging existed.
"""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from storefile import fresh_store_path, sample_capsule_id

from nmemory.server import MemoryServer
from nmemory.store import Store

#: The declared page maximum, written out so the test states the contract a
#: consumer reads instead of whatever the code happens to allow (the code is held
#: to it by `test_the_page_maximum_is_the_one_the_schema_declares`).
PAGE_SIZE_MAX = 500

#: One instant for every call: `created_at` and `standing` become fixtures too,
#: which is what lets the golden below be BYTES and not a shape.
FIXED_NOW = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)

MALFORMED = "invalid capsule-list cursor"
REBOUND = "capsule-list cursor was issued for a different fence or filter"

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "nmemory" / "tools_schema.json"


def _server() -> MemoryServer:
    return MemoryServer(store=Store.open(fresh_store_path()))


def _session(server: MemoryServer) -> str:
    """A store-minted session id (`sess-<n>`): ingest refuses one never opened."""
    answer = server.call_tool("nmemory_session_start", {}, now=FIXED_NOW)
    assert "result" in answer, answer
    return str(json.loads(answer["result"]["content"][0]["text"])["session_id"])


def _item(
    content: str,
    *,
    project_id: str,
    kind: str = "fact",
    session_id: str | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "content": content,
        "source": "session:paging",
        "anchor": "test_capsule_list_paging.py:1",
        "project_id": project_id,
        "kind": kind,
    }
    if session_id is not None:
        item["session_id"] = session_id
    return item


def _ingest(server: MemoryServer, items: list[dict[str, Any]]) -> list[str]:
    answer = server.call_tool("nmemory_capsule_ingest", {"items": items}, now=FIXED_NOW)
    assert "result" in answer, answer
    payload = json.loads(answer["result"]["content"][0]["text"])
    assert payload["captured"] == len(items), payload
    return [outcome["id"] for outcome in payload["outcomes"]]


def _list(server: MemoryServer, args: dict[str, Any] | None = None) -> dict[str, Any]:
    return server.call_tool("nmemory_capsule_list", args or {}, now=FIXED_NOW)


def _text(answer: dict[str, Any]) -> str:
    assert "result" in answer, answer
    assert answer["result"]["isError"] is False, answer
    return str(answer["result"]["content"][0]["text"])


def _payload(answer: dict[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = json.loads(_text(answer))
    return body


def _refusal(answer: dict[str, Any]) -> str:
    """The message of a -32602 refusal that returned NOTHING."""
    assert "result" not in answer, answer
    assert answer["error"]["code"] == -32602, answer
    return str(answer["error"]["message"])


def _ids(payload: dict[str, Any]) -> list[str]:
    return [str(entry["id"]) for entry in payload["entries"]]


def _wire_list(
    server: MemoryServer,
    args: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[dict[str, Any], int]:
    """Run the real SDK door and measure the exact JSON-RPC line it writes."""
    from stdio_peer import wire

    from nmemory.server import MemoryServer as Server

    def fixed_clock_call(
        name: str,
        arguments: dict[str, Any],
        *positional: Any,
        **keywords: Any,
    ) -> dict[str, Any]:
        now = keywords.pop("now", FIXED_NOW)
        return Server.call_tool(
            server, name, arguments, *positional, now=now, **keywords
        )

    monkeypatch.setattr(server, "call_tool", fixed_clock_call)
    server.read_only = True
    frame = {
        "id": 1,
        "method": "tools/call",
        "params": {"name": "nmemory_capsule_list", "arguments": args},
    }
    (written,) = wire(server, [frame])
    written += "\n"
    return json.loads(written), len(written.encode("utf-8"))


def _wire_payload(wire: dict[str, Any]) -> dict[str, Any]:
    assert "result" in wire, wire
    text = wire["result"]["content"][0]["text"]
    return json.loads(text)


def _record_page_budget(monkeypatch: pytest.MonkeyPatch) -> int:
    """Use the real 4 MiB ceiling before the product constant exists, else shrink it."""
    import nmemory.tools.list_ as list_tool

    name = "LIST_RECORD_PAGE_MAX_BYTES"
    if hasattr(list_tool, name):
        budget = 16 * 1024
        monkeypatch.setattr(list_tool, name, budget)
        return budget
    return 4 * 1024 * 1024


def _walk(
    server: MemoryServer, request: dict[str, Any]
) -> tuple[list[str], list[dict[str, Any]]]:
    """Every page from the start, exactly as the consumer loop runs it."""
    collected: list[str] = []
    pages: list[dict[str, Any]] = []
    args = dict(request)
    while True:
        payload = _payload(_list(server, args))
        pages.append(payload)
        collected.extend(_ids(payload))
        assert len(pages) <= 64, "a bounded store must not page forever"
        if payload["next_cursor"] is None:
            return collected, pages
        args = dict(request)
        args["cursor"] = payload["next_cursor"]


def _encode(position: list[Any]) -> str:
    """A cursor the server never issued, in the wire form it would issue."""
    raw = json.dumps(position, ensure_ascii=False, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


# ---------------------------------------------------------------- the fixed store


def _fixed_store(server: MemoryServer) -> tuple[list[str], str, str]:
    """The store the golden was recorded over: two projects, two sessions, two kinds."""
    first, second = _session(server), _session(server)
    ids = _ingest(
        server,
        [
            _item("Paging fact one.", project_id="nott/pg", session_id=first),
            _item(
                "Paging task two.", project_id="nott/pg", kind="task", session_id=first
            ),
            _item("Paging fact three.", project_id="nott/other", session_id=second),
            _item("Paging fact four.", project_id="nott/pg", session_id=second),
        ],
    )
    assert (first, second) == ("sess-1", "sess-2"), (first, second)
    return ids, first, second


def _walk_store(server: MemoryServer, count: int = 7) -> list[str]:
    """`count` rows in one project, enough for several pages of two."""
    return _ingest(
        server,
        [_item(f"Walk row {n}.", project_id="nott/pg") for n in range(count)],
    )


# ------------------------------------------------- 1. the unpaged answer is the old one

#: The five unpaged shapes recorded below, in the order the golden holds them.
UNPAGED_SHAPES: tuple[dict[str, Any], ...] = (
    {},
    {"project_id": "nott/pg"},
    {"session_id": "sess-1"},
    {"limit": 2},
    {"kind": "fact", "limit": 1},
)

GOLDEN: tuple[dict[str, Any], ...] = (
    {
        "label": "ADVISORY_NOT_AUTHORITY",
        "framing": "DATA",
        "returned": 4,
        "entries": [
            {
                "id": "cap-1",
                "handle": "fact-1-paging-fact-one",
                "project_id": "nott/pg",
                "instruction_taint": False,
                "created_at": "2026-10-01T12:00:00Z",
                "headline": "Paging fact one.",
                "kind": "fact",
                "standing": {"state": "por_verificar", "age_days": 0},
            },
            {
                "id": sample_capsule_id(2),
                "handle": "task-2-paging-task-two",
                "project_id": "nott/pg",
                "instruction_taint": False,
                "created_at": "2026-10-01T12:00:00Z",
                "headline": "Paging task two.",
                "kind": "task",
            },
            {
                "id": sample_capsule_id(3),
                "handle": "fact-3-paging-fact-three",
                "project_id": "nott/other",
                "instruction_taint": False,
                "created_at": "2026-10-01T12:00:00Z",
                "headline": "Paging fact three.",
                "kind": "fact",
                "standing": {"state": "por_verificar", "age_days": 0},
            },
            {
                "id": sample_capsule_id(4),
                "handle": "fact-4-paging-fact-four",
                "project_id": "nott/pg",
                "instruction_taint": False,
                "created_at": "2026-10-01T12:00:00Z",
                "headline": "Paging fact four.",
                "kind": "fact",
                "standing": {"state": "por_verificar", "age_days": 0},
            },
        ],
    },
    {
        "label": "ADVISORY_NOT_AUTHORITY",
        "framing": "DATA",
        "returned": 3,
        "entries": [
            {
                "id": "cap-1",
                "handle": "fact-1-paging-fact-one",
                "project_id": "nott/pg",
                "instruction_taint": False,
                "created_at": "2026-10-01T12:00:00Z",
                "headline": "Paging fact one.",
                "kind": "fact",
                "standing": {"state": "por_verificar", "age_days": 0},
            },
            {
                "id": sample_capsule_id(2),
                "handle": "task-2-paging-task-two",
                "project_id": "nott/pg",
                "instruction_taint": False,
                "created_at": "2026-10-01T12:00:00Z",
                "headline": "Paging task two.",
                "kind": "task",
            },
            {
                "id": sample_capsule_id(4),
                "handle": "fact-4-paging-fact-four",
                "project_id": "nott/pg",
                "instruction_taint": False,
                "created_at": "2026-10-01T12:00:00Z",
                "headline": "Paging fact four.",
                "kind": "fact",
                "standing": {"state": "por_verificar", "age_days": 0},
            },
        ],
    },
    {
        "label": "ADVISORY_NOT_AUTHORITY",
        "framing": "DATA",
        "returned": 2,
        "entries": [
            {
                "id": "cap-1",
                "handle": "fact-1-paging-fact-one",
                "project_id": "nott/pg",
                "instruction_taint": False,
                "created_at": "2026-10-01T12:00:00Z",
                "headline": "Paging fact one.",
                "kind": "fact",
                "session_id": "sess-1",
                "standing": {"state": "por_verificar", "age_days": 0},
            },
            {
                "id": sample_capsule_id(2),
                "handle": "task-2-paging-task-two",
                "project_id": "nott/pg",
                "instruction_taint": False,
                "created_at": "2026-10-01T12:00:00Z",
                "headline": "Paging task two.",
                "kind": "task",
                "session_id": "sess-1",
            },
        ],
    },
    {
        "label": "ADVISORY_NOT_AUTHORITY",
        "framing": "DATA",
        "returned": 2,
        "entries": [
            {
                "id": sample_capsule_id(3),
                "handle": "fact-3-paging-fact-three",
                "project_id": "nott/other",
                "instruction_taint": False,
                "created_at": "2026-10-01T12:00:00Z",
                "headline": "Paging fact three.",
                "kind": "fact",
                "standing": {"state": "por_verificar", "age_days": 0},
            },
            {
                "id": sample_capsule_id(4),
                "handle": "fact-4-paging-fact-four",
                "project_id": "nott/pg",
                "instruction_taint": False,
                "created_at": "2026-10-01T12:00:00Z",
                "headline": "Paging fact four.",
                "kind": "fact",
                "standing": {"state": "por_verificar", "age_days": 0},
            },
        ],
    },
    {
        "label": "ADVISORY_NOT_AUTHORITY",
        "framing": "DATA",
        "returned": 1,
        "entries": [
            {
                "id": sample_capsule_id(4),
                "handle": "fact-4-paging-fact-four",
                "project_id": "nott/pg",
                "instruction_taint": False,
                "created_at": "2026-10-01T12:00:00Z",
                "headline": "Paging fact four.",
                "kind": "fact",
                "standing": {"state": "por_verificar", "age_days": 0},
            }
        ],
    },
)
GOLDEN_SHA256 = "0bfaa633856937f1fe6af6b5ccc795a3d874a3fb8b12edd45665d0827a141ae8"


def test_an_unpaged_answer_is_what_head_answered() -> None:
    """A request that does not opt in is answered byte-for-byte as before."""
    server = _server()
    _fixed_store(server)

    texts: list[str] = []
    for shape, expected in zip(UNPAGED_SHAPES, GOLDEN, strict=True):
        answer = _text(_list(server, shape))
        assert answer == json.dumps(expected, ensure_ascii=False), shape
        texts.append(answer)

    assert hashlib.sha256("\n".join(texts).encode("utf-8")).hexdigest() == GOLDEN_SHA256


def test_an_unpaged_answer_carries_no_cursor_field() -> None:
    """Absent `page_size`, the response keeps its exact old key set."""
    server = _server()
    _walk_store(server)
    for shape in ({}, {"limit": 2}, {"kind": "fact"}):
        payload = _payload(_list(server, shape))
        assert list(payload) == ["label", "framing", "returned", "entries"], shape


# ------------------------------------------------------------- 2. the page is bounded


def test_the_page_maximum_is_the_one_the_schema_declares() -> None:
    from nmemory.repositories.capsules import LIST_PAGE_MAX

    tools: list[dict[str, Any]] = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    schema = next(t for t in tools if t["name"] == "nmemory_capsule_list")[
        "inputSchema"
    ]
    page_size = schema["properties"]["page_size"]
    assert page_size["maximum"] == PAGE_SIZE_MAX
    assert page_size["minimum"] == 1
    assert LIST_PAGE_MAX == PAGE_SIZE_MAX
    assert "cursor" in schema["properties"]


@pytest.mark.parametrize("page_size", [0, -1, PAGE_SIZE_MAX + 1, 10**9])
def test_a_page_size_outside_the_declared_bounds_is_refused_by_name(
    page_size: int,
) -> None:
    server = _server()
    _walk_store(server)
    message = _refusal(_list(server, {"page_size": page_size}))
    assert message == f"page_size must be an integer from 1 to {PAGE_SIZE_MAX}"


def test_the_declared_maximum_itself_is_served() -> None:
    server = _server()
    ids = _walk_store(server)
    payload = _payload(_list(server, {"page_size": PAGE_SIZE_MAX}))
    assert _ids(payload) == ids
    assert payload["next_cursor"] is None


def test_a_cursor_without_page_size_is_refused_by_name() -> None:
    """Paging is OPT-IN: a cursor alone is never read as an unpaged request."""
    server = _server()
    _walk_store(server)
    first = _payload(_list(server, {"page_size": 2}))
    message = _refusal(_list(server, {"cursor": first["next_cursor"]}))
    assert message.startswith("cursor needs page_size")


def test_with_record_without_page_size_is_refused_by_name() -> None:
    server = _server()
    _walk_store(server)
    message = _refusal(_list(server, {"with_record": True}))
    assert message.startswith("with_record needs page_size")


def test_a_paged_record_matches_show_for_the_listed_capsule() -> None:
    server = _server()
    ids = _ingest(
        server,
        [_item("Record batch proof.", project_id="nott/pg")],
    )

    payload = _payload(_list(server, {"page_size": 2, "with_record": True}))
    assert _ids(payload) == ids
    entry = payload["entries"][0]
    record = entry["record"]
    # show reads the clock unless told; the fixture date is in the past.
    shown = _payload(
        server.call_tool("nmemory_capsule_show", {"id": entry["id"]}, now=FIXED_NOW)
    )
    assert record == shown
    for field in ("content", "scope", "provenance", "source_quote"):
        assert record["capsule"].get(field) == shown["capsule"].get(field)


def test_cursor_without_record_is_refused_with_record() -> None:
    server = _server()
    _walk_store(server, count=5)
    first = _payload(_list(server, {"page_size": 2}))

    message = _refusal(
        _list(
            server,
            {
                "page_size": 2,
                "with_record": True,
                "cursor": first["next_cursor"],
            },
        )
    )
    assert REBOUND in message


def test_record_pages_fit_wire_budget_and_walk_every_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _server()
    budget = _record_page_budget(monkeypatch)
    count = 120
    payload_size = 512 if budget < 100_000 else 40_000
    text = "A measured record carries its full content and verbatim words. "
    contents = [
        f"record {i:03d}: " + text * (payload_size // len(text)) for i in range(count)
    ]
    ids: list[str] = []
    for offset in range(0, count, 20):
        ids.extend(
            _ingest(
                server,
                [
                    _item(content, project_id="nott/record-page")
                    for content in contents[offset : offset + 20]
                ],
            )
        )

    args: dict[str, Any] = {
        "project_id": "nott/record-page",
        "page_size": 500,
        "with_record": True,
    }
    seen: list[str] = []
    sizes: list[int] = []
    returned: list[dict[str, Any]] = []
    while True:
        wire, size = _wire_list(server, args, monkeypatch)
        payload = _wire_payload(wire)
        sizes.append(size)
        rows = payload["entries"]
        returned.extend(rows)
        seen.extend(_ids(payload))
        if payload["next_cursor"] is None:
            break
        args = {
            "project_id": "nott/record-page",
            "page_size": 500,
            "with_record": True,
            "cursor": payload["next_cursor"],
        }
    assert len(sizes) > 1
    assert all(size <= budget for size in sizes)
    assert seen == ids
    assert len(seen) == len(set(seen))
    for row in returned:
        shown = _payload(
            server.call_tool("nmemory_capsule_show", {"id": row["id"]}, now=FIXED_NOW)
        )
        assert row["record"] == shown


def test_record_page_byte_cut_keeps_a_continuation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import nmemory.tools.list_ as list_tool

    if not hasattr(list_tool, "LIST_RECORD_PAGE_MAX_BYTES"):
        pytest.skip("HEAD has no byte budget constant to force a short page")
    server = _server()
    budget = _record_page_budget(monkeypatch)
    ids = _ingest(
        server,
        [
            _item(
                f"Record {index}. " + "complete words. " * 200,
                project_id="nott/short-page",
            )
            for index in range(8)
        ],
    )
    wire, size = _wire_list(
        server,
        {"project_id": "nott/short-page", "page_size": 500, "with_record": True},
        monkeypatch,
    )
    first = _wire_payload(wire)
    assert size <= budget
    assert 0 < len(first["entries"]) < len(ids)
    assert first["next_cursor"] is not None


def test_record_that_exceeds_page_budget_is_refused_by_capsule_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _server()
    budget = _record_page_budget(monkeypatch)
    content_size = budget + 1024 if budget < 100_000 else 4 * 1024 * 1024 + 1024
    item = _item(
        "Oversize capsule record. "
        + "The complete stored content stays intact. " * (content_size // 37),
        project_id="nott/oversize-record",
    )
    cid = _ingest(server, [item])[0]
    wire, _ = _wire_list(
        server,
        {"project_id": "nott/oversize-record", "page_size": 500, "with_record": True},
        monkeypatch,
    )
    assert "result" not in wire
    assert wire["error"]["code"] == -32602
    assert cid in wire["error"]["message"]
    assert str(budget) in wire["error"]["message"]


def test_without_with_record_keeps_the_same_answer() -> None:
    server = _server()
    _ingest(
        server,
        [
            _item(f"Compact row {index} remains compact.", project_id="nott/compact")
            for index in range(3)
        ],
    )
    args = {"project_id": "nott/compact", "page_size": 1}
    plain = _list(server, args)
    explicit_false = _list(server, {**args, "with_record": False})
    assert plain == explicit_false


def test_limit_and_page_size_together_are_refused_by_name() -> None:
    """The newest tail and the forward walk are two different reads."""
    server = _server()
    _walk_store(server)
    message = _refusal(_list(server, {"limit": 2, "page_size": 2}))
    assert message.startswith("limit and page_size select different reads")


# -------------------------------------------------------- 3. the cursor is opaque bytes


FORGERIES: tuple[tuple[str, str], ...] = (
    ("empty", ""),
    ("not-base64", "###"),
    ("base64-not-json", base64.urlsafe_b64encode(b"\xff\xfe").decode().rstrip("=")),
    ("not-a-list", _encode({"position": 3})),
    ("another-tool", _encode(["work-v1", "0" * 64, 3])),
    ("short-arity", _encode(["cap-list-v1", "0" * 64])),
    ("long-arity", _encode(["cap-list-v1", "0" * 64, 3, "extra"])),
    ("binding-not-text", _encode(["cap-list-v1", 3, 3])),
    ("binding-wrong-size", _encode(["cap-list-v1", "abc", 3])),
    ("position-not-int", _encode(["cap-list-v1", "0" * 64, "3"])),
    ("position-boolean", _encode(["cap-list-v1", "0" * 64, True])),
    ("position-below-one", _encode(["cap-list-v1", "0" * 64, 0])),
    ("position-negative", _encode(["cap-list-v1", "0" * 64, -5])),
    ("oversized", "A" * 5000),
)


@pytest.mark.parametrize("label,cursor", FORGERIES, ids=[f[0] for f in FORGERIES])
def test_a_malformed_cursor_is_refused_by_name_and_returns_nothing(
    label: str, cursor: str
) -> None:
    del label
    server = _server()
    _walk_store(server)
    assert _refusal(_list(server, {"page_size": 2, "cursor": cursor})) == MALFORMED


def test_a_cursor_with_a_forged_binding_is_refused_by_name() -> None:
    """Well-formed, never issued: its binding matches no request."""
    server = _server()
    _walk_store(server)
    forged = _encode(["cap-list-v1", "0" * 64, 2])
    assert REBOUND in _refusal(_list(server, {"page_size": 2, "cursor": forged}))


REBINDINGS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("project_id", {"project_id": "nott/pg"}),
    ("project_prefix", {"project_prefix": "nott"}),
    ("session_id", {"session_id": "sess-1"}),
    ("kind", {"kind": "fact"}),
    ("tier", {"tier": "active"}),
    ("expired", {"expired": True}),
    ("review_state", {"review_state": "ratified"}),
)


@pytest.mark.parametrize("field,added", REBINDINGS, ids=[r[0] for r in REBINDINGS])
def test_a_cursor_replayed_under_another_fence_or_filter_is_refused(
    field: str, added: dict[str, Any]
) -> None:
    del field
    server = _server()
    _walk_store(server)
    first = _payload(_list(server, {"page_size": 2}))
    replay = {"page_size": 2, "cursor": first["next_cursor"], **added}
    assert REBOUND in _refusal(_list(server, replay))


def test_a_cursor_replayed_with_a_fence_dropped_is_refused() -> None:
    """The binding holds in both directions: removing a fence is a different read."""
    server = _server()
    _walk_store(server)
    fenced = _payload(_list(server, {"page_size": 2, "project_prefix": "nott"}))
    replay = {"page_size": 2, "cursor": fenced["next_cursor"]}
    assert REBOUND in _refusal(_list(server, replay))


def test_the_request_that_issued_the_cursor_continues_with_it() -> None:
    server = _server()
    ids = _walk_store(server)
    first = _payload(_list(server, {"page_size": 2}))
    assert _ids(first) == ids[:2]
    second = _payload(_list(server, {"page_size": 2, "cursor": first["next_cursor"]}))
    assert _ids(second) == ids[2:4]


def test_the_page_size_may_change_between_pages() -> None:
    """`page_size` bounds ONE page and is not part of the binding."""
    server = _server()
    ids = _walk_store(server)
    first = _payload(_list(server, {"page_size": 2}))
    wider = _payload(_list(server, {"page_size": 3, "cursor": first["next_cursor"]}))
    assert _ids(first) + _ids(wider) == ids[:5]


# ------------------------------------------------------------- 4. and 5. the whole walk


def test_every_page_from_the_start_is_the_unpaged_set_in_order() -> None:
    """No omission, no duplicate: the walk equals the unpaged answer with no limit."""
    server = _server()
    ids = _walk_store(server)
    whole = _ids(_payload(_list(server)))
    assert whole == ids

    for page_size in (1, 2, 3, 7, PAGE_SIZE_MAX):
        collected, _pages = _walk(server, {"page_size": page_size})
        assert collected == whole, page_size
        assert len(set(collected)) == len(collected), page_size


def test_only_the_end_of_the_fenced_range_marks_the_cursor_null() -> None:
    server = _server()
    _walk_store(server)
    _collected, pages = _walk(server, {"page_size": 2})
    assert [page["next_cursor"] is None for page in pages] == [
        False,
        False,
        False,
        True,
    ]
    for page in pages[:-1]:
        assert isinstance(page["next_cursor"], str) and page["next_cursor"]


def test_a_page_filtered_empty_by_a_sidecar_still_carries_the_cursor() -> None:
    """Every scanned row failed `kind`: the page is empty and the consumer goes on."""
    server = _server()
    facts = _ingest(
        server, [_item(f"Fact {n}.", project_id="nott/pg") for n in range(3)]
    )
    tasks = _ingest(
        server,
        [_item(f"Task {n}.", project_id="nott/pg", kind="task") for n in range(3)],
    )
    assert len(facts) == 3

    collected, pages = _walk(server, {"page_size": 3, "kind": "task"})
    assert len(pages) == 2, pages
    assert pages[0]["returned"] == 0
    assert pages[0]["entries"] == []
    assert isinstance(pages[0]["next_cursor"], str)
    assert _ids(pages[1]) == tasks
    assert pages[1]["next_cursor"] is None
    assert collected == tasks


def test_a_filtered_empty_page_in_the_middle_advances_the_position() -> None:
    """The position is the last row SCANNED: an empty page mid-walk still moves it.

    A position taken from the last row RETURNED would not move here — the page
    returned nothing — and the consumer would hand the same cursor back forever.
    """
    server = _server()
    rows = _ingest(
        server,
        [_item(f"Head task {n}.", project_id="nott/pg", kind="task") for n in range(2)]
        + [_item(f"Middle fact {n}.", project_id="nott/pg") for n in range(2)]
        + [
            _item(f"Tail task {n}.", project_id="nott/pg", kind="task")
            for n in range(2)
        ],
    )
    tasks = rows[:2] + rows[4:]

    collected, pages = _walk(server, {"page_size": 2, "kind": "task"})
    assert collected == tasks
    assert [page["returned"] for page in pages] == [2, 0, 2]
    assert isinstance(pages[1]["next_cursor"], str)


def test_a_sidecar_filter_never_loses_or_repeats_a_row_across_pages() -> None:
    """Interleaved matches: the position is the last row SCANNED, not the last returned."""
    server = _server()
    rows = _ingest(
        server,
        [
            _item(
                f"Mixed {n}.",
                project_id="nott/pg",
                kind="fact" if n % 2 == 0 else "task",
            )
            for n in range(6)
        ],
    )
    tasks = [row for n, row in enumerate(rows) if n % 2 == 1]

    collected, _pages = _walk(server, {"page_size": 2, "kind": "task"})
    assert collected == tasks


def test_capsules_ingested_between_pages_appear_later_and_none_is_skipped() -> None:
    server = _server()
    before = _walk_store(server, count=5)

    page = _payload(_list(server, {"page_size": 2}))
    collected = _ids(page)
    added = _ingest(
        server, [_item(f"Late row {n}.", project_id="nott/pg") for n in range(2)]
    )
    while page["next_cursor"] is not None:
        page = _payload(_list(server, {"page_size": 2, "cursor": page["next_cursor"]}))
        collected.extend(_ids(page))

    assert collected == before + added
    assert len(set(collected)) == len(collected)


# ------------------------------------------------------------- 6. the fences hold


def test_a_session_fenced_walk_returns_no_other_session_or_project() -> None:
    """Two writers interleaved in store order; each walk sees only its own rows."""
    server = _server()
    mine, theirs = _session(server), _session(server)
    items: list[dict[str, Any]] = []
    for n in range(6):
        items.append(_item(f"Mine {n}.", project_id="nott/pg", session_id=mine))
        items.append(_item(f"Theirs {n}.", project_id="other/pg", session_id=theirs))
    written = _ingest(server, items)
    ours = [row for n, row in enumerate(written) if n % 2 == 0]
    yours = [row for n, row in enumerate(written) if n % 2 == 1]

    collected, pages = _walk(server, {"page_size": 2, "session_id": mine})
    assert collected == ours
    assert not set(collected) & set(yours)
    for page in pages:
        for entry in page["entries"]:
            assert entry["session_id"] == mine
            assert entry["project_id"] == "nott/pg"

    fenced, _pages = _walk(server, {"page_size": 2, "project_id": "other/pg"})
    assert fenced == yours


# ----------------------------------------------------- 7. nothing is stored for a page


def _schema_objects(server: MemoryServer) -> set[str]:
    with server.store.db.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT type || ':' || name FROM sqlite_master")
        return {str(row[0]) for row in cur.fetchall()}


def test_a_walk_creates_no_table_and_no_cursor_row() -> None:
    """The continuation lives in the caller's hand; the store gains nothing for it."""
    server = _server()
    _walk_store(server)
    before = _schema_objects(server)
    _walk(server, {"page_size": 2})
    _walk(server, {"page_size": 3, "kind": "fact"})
    assert _schema_objects(server) == before


def test_a_whole_range_read_of_one_kind_decodes_only_that_kind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The kind is selected in SQL, so the rows decoded are the rows returned.

    A conversation finds its session by reading one kind across the project; filtered
    after the fetch, that read decoded every capsule of the project to return one
    (13,409 rows for 1 on a replay at the owner's scale). Counted in rows, not time.
    """
    from nmemory.repositories import capsules

    server = _server()
    _ingest(server, [_item(f"fact {n}", project_id="p-kind") for n in range(30)])
    [doc] = _ingest(server, [_item("the index", project_id="p-kind", kind="doc")])
    decoded: list[str] = []
    stored_capsule = capsules._stored_capsule

    def counted(row: Any) -> Any:
        decoded.append(str(row[0]))
        return stored_capsule(row)

    monkeypatch.setattr(capsules, "_stored_capsule", counted)
    payload = _payload(_list(server, {"project_id": "p-kind", "kind": "doc"}))
    assert _ids(payload) == [doc]
    assert decoded == [doc]
    # A forward page keeps the kind a sidecar filter applied after the cut.
    with pytest.raises(ValueError, match="selects no kind"):
        server.store.capsules.list_page(
            capsules.ListFilter(kind="doc"), after_seq=None, page_size=3
        )
