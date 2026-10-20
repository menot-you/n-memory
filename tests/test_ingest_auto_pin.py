"""An owner rule is pinned in the same transaction as its ingest.

Only a verified source quote proves that the owner said it: a declared
authority_class alone does not pin.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from storefile import fresh_store_path

from nmemory.capsule import sha256_hex
from nmemory.server import MemoryServer
from nmemory.store import Store

QUOTE = "run the member checks before each push"


def _item(kind: str, authority: str, quote: str | None = QUOTE) -> dict[str, Any]:
    item: dict[str, Any] = {
        "content": f"Rule: {QUOTE}.",
        "source": "session:test",
        "anchor": "NOTES.md:1",
        "kind": kind,
        "authority_class": authority,
        "project_id": "nmemory",
    }
    if quote is not None:
        item["source_quote"] = quote
    return item


def _pinned(store: Store, content: str) -> bool:
    stored = store.capsules.find_by_source_hash(sha256_hex(content))
    assert stored is not None, "the capture was lost"
    return store.pins.get_pin(stored.id)


@pytest.fixture
def server(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[Store, MemoryServer]:
    monkeypatch.setenv("NOTT_NMEMORY_SYSTEM1", "0")
    (tmp_path / "NOTES.md").write_text(f"The owner said: {QUOTE}.\n")
    store = Store.open(fresh_store_path())
    return store, MemoryServer(store=store, anchor_root=str(tmp_path))


@pytest.mark.parametrize("kind", ["decision", "procedure", "constraint"])
def test_a_quoted_user_stated_lasting_ingest_is_pinned(
    server: tuple[Store, MemoryServer], kind: str
) -> None:
    store, mcp = server
    item = _item(kind, "user-stated")
    mcp.call_tool("nmemory_capsule_ingest", item)
    assert _pinned(store, item["content"])


@pytest.mark.parametrize(
    ("kind", "authority", "quote"),
    [
        ("fact", "user-stated", QUOTE),
        ("decision", "agent-inferred", QUOTE),
        ("decision", "user-stated", None),
        ("decision", "user-stated", "words the owner never wrote"),
    ],
)
def test_other_ingests_stay_unpinned(
    server: tuple[Store, MemoryServer], kind: str, authority: str, quote: str | None
) -> None:
    store, mcp = server
    item = _item(kind, authority, quote)
    mcp.call_tool("nmemory_capsule_ingest", item)
    assert not _pinned(store, item["content"])


@pytest.mark.parametrize(
    ("quote", "why"),
    [(None, "skipped: no source quote"), ("never written", "skipped: quote mismatch")],
)
def test_an_unpinned_owner_rule_says_why(
    server: tuple[Store, MemoryServer], quote: str | None, why: str
) -> None:
    _, mcp = server
    result = mcp.call_tool(
        "nmemory_capsule_ingest", _item("decision", "user-stated", quote)
    )
    text = result["result"]["content"][0]["text"]
    assert json.loads(text)["outcomes"][0]["pin"] == why


def test_a_rejected_batch_leaves_no_pin(
    server: tuple[Store, MemoryServer], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pin is in the ingest transaction: a rollback takes it away too."""
    store, mcp = server
    real = store.pins.set_pin

    def failing(*args: Any, **kwargs: Any) -> None:
        real(*args, **kwargs)
        raise ValueError("forced failure after the pin")

    monkeypatch.setattr(store.pins, "set_pin", failing)
    item = _item("decision", "user-stated")
    mcp.call_tool("nmemory_capsule_ingest", item)
    assert store.capsules.find_by_source_hash(sha256_hex(item["content"])) is None
    assert store.pins.pinned_among(["cap-1"]) == set()
