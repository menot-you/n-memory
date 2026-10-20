"""`hints: false` on an ingest item skips the near-duplicate and sibling passes.

Each pass scans the store's FTS candidates in pure Python; a machine-written
record (a conversation transcript) never reads either answer, and they were most
of the 1.2-1.4 s each item took. The default is unchanged: an item without `hints` runs both
passes and returns the same `dedup_hint` and `siblings` as before.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from nmemory import ingest as core
from nmemory.server import MemoryServer
from nmemory.store import Store

BASE = (
    "the gate stages every pytest run under var tmp because tmp is a tmpfs "
    "with a fixed inode ceiling that per run directories exhaust"
)
NEAR = BASE + " quickly"


def _server() -> MemoryServer:
    return MemoryServer(store=Store.open_disposable_test_store(), project_id="nmemory")


def _ingest(server: MemoryServer, args: dict[str, Any]) -> dict[str, Any]:
    resp = server.call_tool("nmemory_capsule_ingest", args)
    assert "error" not in resp, resp
    body: dict[str, Any] = json.loads(resp["result"]["content"][0]["text"])
    return body


def _item(text: str, **extra: Any) -> dict[str, Any]:
    return {
        "content": text,
        "source": "session:2026-10-03",
        "anchor": "mcps/nmemory/nmemory/ingest.py:576",
        "kind": "fact",
        "project_id": "nmemory",
        **extra,
    }


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    counts = {"near_duplicate_hint": 0, "sibling_hints": 0}
    real_near, real_siblings = core.near_duplicate_hint, core.sibling_hints

    def near(*a: Any, **k: Any) -> Any:
        counts["near_duplicate_hint"] += 1
        return real_near(*a, **k)

    def siblings(*a: Any, **k: Any) -> Any:
        counts["sibling_hints"] += 1
        return real_siblings(*a, **k)

    monkeypatch.setattr(core, "near_duplicate_hint", near)
    monkeypatch.setattr(core, "sibling_hints", siblings)
    return counts


def test_default_still_runs_both_passes_and_returns_both_hints(
    calls: dict[str, int],
) -> None:
    server = _server()
    first = _ingest(server, _item(BASE))["outcomes"][0]
    calls.update(near_duplicate_hint=0, sibling_hints=0)

    row = _ingest(server, {"items": [_item(NEAR)]})["outcomes"][0]

    assert calls == {"near_duplicate_hint": 1, "sibling_hints": 1}
    assert row["status"] == "captured"
    assert row["dedup_hint"] == {"similar_id": first["id"], "score": 0.95}
    assert row["siblings"] == [{"id": first["id"], "score": 0.95}]


def test_hints_true_is_the_default(calls: dict[str, int]) -> None:
    server = _server()
    first = _ingest(server, _item(BASE))["outcomes"][0]
    calls.update(near_duplicate_hint=0, sibling_hints=0)

    row = _ingest(server, _item(NEAR, hints=True))["outcomes"][0]

    assert calls == {"near_duplicate_hint": 1, "sibling_hints": 1}
    assert row["dedup_hint"] == {"similar_id": first["id"], "score": 0.95}
    assert row["siblings"] == [{"id": first["id"], "score": 0.95}]


@pytest.mark.parametrize("batch", [False, True], ids=["single", "batch"])
def test_hints_false_runs_neither_pass_and_omits_both_hints(
    calls: dict[str, int], batch: bool
) -> None:
    server = _server()
    _ingest(server, _item(BASE))
    calls.update(near_duplicate_hint=0, sibling_hints=0)

    item = _item(NEAR, hints=False)
    body = _ingest(server, {"items": [item]} if batch else item)
    row = body["outcomes"][0]

    assert calls == {"near_duplicate_hint": 0, "sibling_hints": 0}
    assert row["status"] == "captured"
    assert row["dedup_hint"] is None
    assert "siblings" not in row
    assert body["captured"] == 1


def test_hints_must_be_a_boolean() -> None:
    body = _ingest(_server(), {"items": [_item(NEAR, hints="no")]})
    assert body["rejected"] == 1
    assert body["outcomes"][0]["error"].startswith("items[0].hints:")
