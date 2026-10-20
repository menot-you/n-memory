from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from nmemory.anchor import (
    anchor_path_present,
    parse_anchor,
    parse_anchor_path,
    probe_anchor_drift,
    probe_anchor_liveness,
    quote_verdict,
)
from nmemory.capsule import sha256_hex
from nmemory.server import MemoryServer
from nmemory.store import Store


def _ok(res: dict[str, Any]) -> dict[str, Any]:
    assert "result" in res and res["result"]["isError"] is False, res
    payload: dict[str, Any] = json.loads(res["result"]["content"][0]["text"])
    return payload


def test_anchor_parsing_handles_colons_and_quotes() -> None:
    # 1. path:line:col
    assert parse_anchor("src/ingest.rs:42:7").path == "src/ingest.rs"
    assert parse_anchor_path("src/ingest.rs:42:7") == "src/ingest.rs"

    # 2. quotes containing colons
    assert parse_anchor("order: execute step 1").path is None
    assert parse_anchor_path("order: execute step 1") is None

    # 3. doc and issue anchors
    assert parse_anchor("doc-1361").path is None
    assert parse_anchor_path("doc-1361") is None


def test_anchor_liveness_and_drift_with_quotes_and_files(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    file_p = root / "file.txt"
    file_p.write_text("hello anchor\n")

    # Real existing file
    assert probe_anchor_liveness("file.txt:1", str(root)) == "live"
    assert probe_anchor_liveness("file.txt:1:5", str(root)) == "live"

    # Real missing file
    assert probe_anchor_liveness("missing.txt:1", str(root)) == "missing"

    # Verbatim quote with colons -> unknown (never marked missing!)
    assert (
        probe_anchor_liveness("order: execute step 1 on file.txt", str(root))
        == "unknown"
    )

    # Path traversal attack -> unknown (caged)
    assert probe_anchor_liveness("../../../etc/passwd:1", str(root)) == "unknown"


def test_quote_verdict_caging_and_matching(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    doc = root / "doc.txt"
    doc.write_text("The quick brown fox jumps over the lazy dog\n")

    # Match
    assert quote_verdict("doc.txt:1", str(root), "brown fox") == "verified"
    # Mismatch
    assert quote_verdict("doc.txt:1", str(root), "white rabbit") == "mismatch"
    # Quote anchor containing colon (not a real path) -> unverifiable
    assert (
        quote_verdict("note: important rule", str(root), "important") == "unverifiable"
    )
    # Path traversal -> unverifiable
    assert quote_verdict("../../etc/passwd:1", str(root), "root") == "unverifiable"


def test_drift_requires_a_readable_regular_file_inside_root(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    doc = root / "note.txt"
    doc.write_bytes(b"original")
    digest = sha256_hex(b"original")
    assert probe_anchor_drift("note.txt:1", digest, str(root)) == "unchanged"
    doc.write_bytes(b"changed")
    assert probe_anchor_drift("note.txt:1", digest, str(root)) == "drifted"
    (root / "link.txt").symlink_to(doc)
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"original")
    (root / "escape.txt").symlink_to(outside)
    assert probe_anchor_liveness("escape.txt:1", str(root)) == "unknown"
    for anchor, stored, directory in [
        ("note.txt:1", digest, ""),
        ("note.txt:1", None, str(root)),
        ("doc-1361", digest, str(root)),
        ("../outside.txt:1", digest, str(root)),
        ("missing.txt:1", digest, str(root)),
        ("link.txt:1", digest, str(root)),
        ("escape.txt:1", digest, str(root)),
    ]:
        assert probe_anchor_drift(anchor, stored, directory) == "unknown"


def test_anchor_io_failure_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def inaccessible(path: str) -> str:
        raise PermissionError(path)

    monkeypatch.setattr("nmemory.anchor.os.path.realpath", inaccessible)
    assert probe_anchor_liveness("note.txt:1", str(tmp_path)) == "unknown"
    assert probe_anchor_drift("note.txt:1", "digest", str(tmp_path)) == "unknown"


def test_ingest_anchor_note_omitted_for_quotes_with_colons(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "real.txt").write_text("content\n")

    store = Store.open_disposable_test_store()
    server = MemoryServer(
        store=store,
        anchor_root=str(root),
        project_id="testproj",
    )

    # 1. Real file -> no anchor_note
    res_real = _ok(
        server.call_tool(
            "nmemory_capsule_ingest",
            {
                "items": [
                    {
                        "content": "real item",
                        "kind": "fact",
                        "source": "test",
                        "anchor": "real.txt:1:10",
                    }
                ]
            },
        )
    )
    assert "anchor_note" not in res_real["outcomes"][0]

    # 2. Quote with colons -> no anchor_note (not misclassified as path)
    res_quote = _ok(
        server.call_tool(
            "nmemory_capsule_ingest",
            {
                "items": [
                    {
                        "content": "quote item",
                        "kind": "fact",
                        "source": "test",
                        "anchor": "quote: do not modify this line",
                    }
                ]
            },
        )
    )
    assert "anchor_note" not in res_quote["outcomes"][0]

    # 3. Non-existent file -> anchor_note present
    res_missing = _ok(
        server.call_tool(
            "nmemory_capsule_ingest",
            {
                "items": [
                    {
                        "content": "missing item",
                        "kind": "fact",
                        "source": "test",
                        "anchor": "nonexistent.txt:1",
                    }
                ]
            },
        )
    )
    assert "anchor_note" in res_missing["outcomes"][0]


def test_anchor_path_present_skips_out_of_root_and_symlinks(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "store.py").write_bytes(b"content")
    (tmp_path / "link.py").symlink_to(tmp_path / "src" / "store.py")

    assert anchor_path_present(tmp_path, "src/store.py")
    assert not anchor_path_present(tmp_path, "src/absent.py")
    assert not anchor_path_present(tmp_path, "/etc/hostname")
    assert not anchor_path_present(tmp_path, "../escape.py")
    assert not anchor_path_present(tmp_path, "link.py")
