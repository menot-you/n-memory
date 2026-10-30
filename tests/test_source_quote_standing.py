"""Parity of `source_quote` honesty pair on recall (SourceQuoteWire::standing).

The verbatim quote (`source_quote`) at capture time is verified and recorded.
At recall time (`nmemory_capsule_retrieve`), if the anchor drifted (`anchor_drift` drifted or unknown),
the standing verdict is recalculated live against the current file:
- if anchor did not change (`unchanged`), retains the recorded verdict;
- if anchor drifted and file still contains quote, retains `verified`;
- if anchor drifted and quote vanished from file, becomes `mismatch`;
- if anchor drifted and file was deleted/unreachable, becomes `unverifiable`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from storefile import fresh_store_path

from nmemory.server import MemoryServer
from nmemory.store import Store


def fresh_server(anchor_root: str) -> MemoryServer:
    url = fresh_store_path("sq_standing")
    store = Store.open(url)
    return MemoryServer(store=store, anchor_root=anchor_root)


def ok(res: dict[str, Any]) -> dict[str, Any]:
    assert "result" in res and res["result"]["isError"] is False, res
    payload: dict[str, Any] = json.loads(res["result"]["content"][0]["text"])
    return payload


def test_source_quote_unchanged_anchor_retains_verified(tmp_path: Path) -> None:
    """Unchanged anchor retains verified verdict recorded at capture."""
    src_file = tmp_path / "module.py"
    src_file.write_text("def target_function():\n    return 42\n", encoding="utf-8")

    server = fresh_server(str(tmp_path))
    ingest_res = ok(
        server.call_tool(
            "nmemory_capsule_ingest",
            {
                "items": [
                    {
                        "content": "The function target_function returns 42 in module.py.",
                        "kind": "fact",
                        "source": "test",
                        "anchor": "module.py:1",
                        "source_quote": "return 42",
                        "project_id": "pynott",
                    }
                ]
            },
        )
    )
    assert ingest_res["outcomes"][0]["status"] == "captured"

    ret_res = ok(
        server.call_tool(
            "nmemory_capsule_retrieve",
            {"terms": ["target_function"], "project_id": "pynott"},
        )
    )
    assert ret_res["outcome"] == "grounded"
    result_item = ret_res["results"][0]
    assert result_item["anchor_drift"] == "unchanged"
    assert "source_quote" in result_item
    assert result_item["source_quote"] == {"quote": "return 42", "verdict": "verified"}


def test_source_quote_drifted_anchor_with_quote_present_is_verified(
    tmp_path: Path,
) -> None:
    """Drifted anchor (file modified on another line) but quote still present -> standing verified."""
    src_file = tmp_path / "module.py"
    src_file.write_text("def target_function():\n    return 42\n", encoding="utf-8")

    server = fresh_server(str(tmp_path))
    ok(
        server.call_tool(
            "nmemory_capsule_ingest",
            {
                "items": [
                    {
                        "content": "The function target_function returns 42 in module.py.",
                        "kind": "fact",
                        "source": "test",
                        "anchor": "module.py:1",
                        "source_quote": "return 42",
                        "project_id": "pynott",
                    }
                ]
            },
        )
    )

    # Modifies file on another line (adds comments)
    src_file.write_text(
        "# Novo comentário no topo\ndef target_function():\n    return 42\n",
        encoding="utf-8",
    )

    ret_res = ok(
        server.call_tool(
            "nmemory_capsule_retrieve",
            {"terms": ["target_function"], "project_id": "pynott"},
        )
    )
    assert ret_res["outcome"] == "grounded"
    result_item = ret_res["results"][0]
    assert result_item["anchor_drift"] == "drifted"
    assert "source_quote" in result_item
    assert result_item["source_quote"] == {"quote": "return 42", "verdict": "verified"}


def test_source_quote_drifted_anchor_with_quote_removed_becomes_mismatch(
    tmp_path: Path,
) -> None:
    """Drifted anchor and quote removed from file -> standing degrades to mismatch."""
    src_file = tmp_path / "module.py"
    src_file.write_text("def target_function():\n    return 42\n", encoding="utf-8")

    server = fresh_server(str(tmp_path))
    ok(
        server.call_tool(
            "nmemory_capsule_ingest",
            {
                "items": [
                    {
                        "content": "The function target_function returns 42 in module.py.",
                        "kind": "fact",
                        "source": "test",
                        "anchor": "module.py:1",
                        "source_quote": "return 42",
                        "project_id": "pynott",
                    }
                ]
            },
        )
    )

    # Removes return 42 from file
    src_file.write_text("def target_function():\n    return 100\n", encoding="utf-8")

    ret_res = ok(
        server.call_tool(
            "nmemory_capsule_retrieve",
            {"terms": ["target_function"], "project_id": "pynott"},
        )
    )
    assert ret_res["outcome"] == "grounded"
    result_item = ret_res["results"][0]
    assert result_item["anchor_drift"] == "drifted"
    assert "source_quote" in result_item
    assert result_item["source_quote"] == {"quote": "return 42", "verdict": "mismatch"}


def test_source_quote_drifted_anchor_with_file_deleted_becomes_unverifiable(
    tmp_path: Path,
) -> None:
    """Drifted anchor and file deleted -> standing degrades to unverifiable."""
    src_file = tmp_path / "module.py"
    src_file.write_text("def target_function():\n    return 42\n", encoding="utf-8")

    server = fresh_server(str(tmp_path))
    ok(
        server.call_tool(
            "nmemory_capsule_ingest",
            {
                "items": [
                    {
                        "content": "The function target_function returns 42 in module.py.",
                        "kind": "fact",
                        "source": "test",
                        "anchor": "module.py:1",
                        "source_quote": "return 42",
                        "project_id": "pynott",
                    }
                ]
            },
        )
    )

    # Deletes file
    src_file.unlink()

    ret_res = ok(
        server.call_tool(
            "nmemory_capsule_retrieve",
            {"terms": ["target_function"], "project_id": "pynott"},
        )
    )
    assert ret_res["outcome"] == "grounded"
    result_item = ret_res["results"][0]
    assert result_item["anchor_drift"] == "unknown"
    assert "source_quote" in result_item
    assert result_item["source_quote"] == {
        "quote": "return 42",
        "verdict": "unverifiable",
    }


def test_capsule_without_source_quote_omits_key(tmp_path: Path) -> None:
    """Capsule without source_quote does not emit the key in recall result."""
    server = fresh_server(str(tmp_path))
    ok(
        server.call_tool(
            "nmemory_capsule_ingest",
            {
                "items": [
                    {
                        "content": "Normal capsule without an explicit quote.",
                        "kind": "fact",
                        "source": "test",
                        "anchor": "plain.py:1",
                        "project_id": "pynott",
                    }
                ]
            },
        )
    )

    ret_res = ok(
        server.call_tool(
            "nmemory_capsule_retrieve",
            {"terms": ["normal"], "project_id": "pynott"},
        )
    )
    assert ret_res["outcome"] == "grounded"
    result_item = ret_res["results"][0]
    assert "source_quote" not in result_item


def test_source_quote_quote_verdict_edge_cases(tmp_path: Path) -> None:
    from nmemory.anchor import quote_verdict

    # 1. Empty anchor_root or no path in anchor
    assert quote_verdict("session:123", "", "foo") == "unverifiable"
    assert quote_verdict("session:123", str(tmp_path), "foo") == "unverifiable"
    assert quote_verdict("valid.py:1", "", "foo") == "unverifiable"

    # 2. Path traversal escape
    assert quote_verdict("../../escape.py:1", str(tmp_path), "foo") == "unverifiable"
    assert (
        quote_verdict("subdir/../../escape.py:1", str(tmp_path / "subdir"), "foo")
        == "unverifiable"
    )

    # 3. Directory or symlink
    dir_path = tmp_path / "subdir"
    dir_path.mkdir()
    assert quote_verdict("subdir:1", str(tmp_path), "foo") == "unverifiable"

    target_file = tmp_path / "target.py"
    target_file.write_text("hello world", encoding="utf-8")
    link_path = tmp_path / "link.py"
    link_path.symlink_to(target_file)
    assert quote_verdict("link.py:1", str(tmp_path), "hello") == "unverifiable"
    assert quote_verdict("target.py:1", str(tmp_path), "missing_quote") == "mismatch"

    # 4. Corrupt binary/unicode error handling
    corrupt_file = tmp_path / "corrupt.py"
    corrupt_file.write_bytes(b"\x80\x81\x82\xff\xfe")
    assert quote_verdict("corrupt.py:1", str(tmp_path), "foo") == "unverifiable"


def test_source_quote_reanchor_effective(tmp_path: Path) -> None:
    f1 = tmp_path / "f1.py"
    f1.write_text("original content\n", encoding="utf-8")
    f2 = tmp_path / "f2.py"
    f2.write_text("moved quote here\n", encoding="utf-8")

    server = fresh_server(str(tmp_path))
    ingest_res = ok(
        server.call_tool(
            "nmemory_capsule_ingest",
            {
                "items": [
                    {
                        "content": "Capsule that is going to be reanchored.",
                        "kind": "fact",
                        "source": "test",
                        "anchor": "f1.py:1",
                        "source_quote": "moved quote here",
                        "project_id": "pynott",
                    }
                ]
            },
        )
    )
    cid = ingest_res["outcomes"][0]["id"]

    # Reanchors to f2.py, as the curator's repair path does.
    server.store.anchors.reanchor(cid, "f2.py:1", "moved to f2", datetime.now(UTC))

    ret_res = ok(
        server.call_tool(
            "nmemory_capsule_retrieve",
            {"terms": ["reanchored"], "project_id": "pynott"},
        )
    )
    assert ret_res["outcome"] == "grounded"
    result_item = ret_res["results"][0]
    assert result_item.get("source_quote") == {
        "quote": "moved quote here",
        "verdict": "verified",
    }
