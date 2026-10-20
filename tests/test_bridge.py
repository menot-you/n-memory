from __future__ import annotations

import json
import os
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from pathlib import Path

import pytest

from nmemory.bridge import (
    BridgeCandidate,
    BridgeIoError,
    BridgeNotADirectory,
    BridgeSource,
    BridgeSourceMissing,
    candidates_from,
    heading_level,
    import_block_source_key,
    read_source,
    split_markdown,
)
from nmemory.capsule import AuthorityClass
from nmemory.relation import RelationKind, RelationOrigin
from nmemory.server import MemoryServer
from nmemory.store import Store
from nmemory.tools import import_ as import__tool

NOW = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)


def test_bridge_candidate_cannot_change_after_reading() -> None:
    candidate = BridgeCandidate("content", "note.md:1", "memory-dir")
    with pytest.raises(FrozenInstanceError):
        candidate.content = "replacement"  # type: ignore[misc]  # verify runtime immutability


def test_heading_boundaries_keep_preamble_and_nested_subsections() -> None:
    assert split_markdown("intro\n## Only\nbody") == [
        (1, "intro"),
        (2, "## Only\nbody"),
    ]
    assert split_markdown("intro\n\n## Only\nbody") == [
        (1, "intro"),
        (3, "## Only\nbody"),
    ]
    assert split_markdown("# One\nbody\n## Nested\ninside\n# Two\nend") == [
        (1, "# One\nbody\n## Nested\ninside"),
        (5, "# Two\nend"),
    ]


def test_memory_directory_reads_sorted_markdown_and_ignores_other_entries(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "notes"
    directory.mkdir()
    (directory / "b.md").write_text("second")
    (directory / "a.md").write_text("first")
    (directory / "ignored.txt").write_text("ignored")
    (directory / "nested.md").mkdir()
    (directory / "linked.md").symlink_to(directory / "a.md")
    supplied_paths: list[str | Path] = ["notes", directory]
    for supplied in supplied_paths:
        result = read_source("memory-dir", tmp_path, tmp_path, supplied)
        assert [(c.content, c.anchor, c.source_label) for c in result] == [
            ("first", "notes/a.md:1", "memory-dir"),
            ("second", "notes/b.md:1", "memory-dir"),
        ]


def test_bridge_missing_sources_and_invalid_directory_are_typed(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown bridge source"):
        read_source("invalid", tmp_path, tmp_path)
    with pytest.raises(ValueError, match="requires memory_dir"):
        read_source("memory-dir", tmp_path, tmp_path)
    with pytest.raises(BridgeSourceMissing):
        read_source("user-claude-md", tmp_path, tmp_path)
    with pytest.raises(BridgeSourceMissing):
        read_source("memory-dir", tmp_path, tmp_path, "missing")
    (tmp_path / "file").write_text("not a directory")
    with pytest.raises(BridgeNotADirectory):
        read_source("memory-dir", tmp_path, tmp_path, "file")


@pytest.mark.parametrize("anchor", ["notes/a.md:12", "notes/a.md"])
def test_import_identity_ignores_line_number(anchor: str) -> None:
    assert import_block_source_key("memory-dir", anchor) == "memory-dir\nnotes/a.md"


@pytest.mark.parametrize("operation", ["listdir", "lstat", "open"])
def test_bridge_io_errors_preserve_typed_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    (tmp_path / "a.md").write_text("content")

    def inaccessible(*args: object, **kwargs: object) -> None:
        raise PermissionError("access denied")

    target = (
        "builtins.open" if operation == "open" else f"nmemory.bridge.os.{operation}"
    )
    with monkeypatch.context() as patch:
        patch.setattr(target, inaccessible)
        with pytest.raises(BridgeIoError, match="access denied"):
            read_source("memory-dir", tmp_path, tmp_path, tmp_path)


def test_frontmatter_is_skipped_and_line_numbers_stay_original() -> None:
    doc = "---\ntitle: cfg\n---\n\nFirst block.\n\nSecond block.\n"
    got = split_markdown(doc)
    assert got == [
        (5, "First block."),
        (7, "Second block."),
    ]


def test_unclosed_frontmatter_opener_is_plain_content() -> None:
    doc = "---\nnot frontmatter, never closed"
    got = split_markdown(doc)
    assert got == [(1, "---\nnot frontmatter, never closed")]


def test_in_root_anchor_is_root_relative_out_of_root_stays_absolute() -> None:
    root = Path("/injected-anchor-root")
    in_root = root / "mcps/nmemory/PLAN.md"
    got = candidates_from("only block", in_root, "project-claude-md", root)
    assert len(got) == 1
    assert got[0].anchor == "mcps/nmemory/PLAN.md:1"

    out_of_root = Path("/etc/some-notes.md")
    got = candidates_from("only block", out_of_root, "project-claude-md", root)
    assert got[0].anchor == "/etc/some-notes.md:1"


def test_heading_documents_split_per_top_level_section_with_preamble() -> None:
    doc = "intro before any heading\n\n# One\nbody one\n\n# Two\nbody two\n"
    got = split_markdown(doc)
    assert got == [
        (1, "intro before any heading"),
        (3, "# One\nbody one"),
        (6, "# Two\nbody two"),
    ]


def test_lone_h1_title_splits_at_the_first_repeated_deeper_level() -> None:
    doc = "# Title\n\nintro\n\n## A\na body\n\n## B\nb body\n"
    got = split_markdown(doc)
    assert got == [
        (1, "# Title\n\nintro"),
        (5, "## A\na body"),
        (8, "## B\nb body"),
    ]


def test_single_heading_document_is_one_candidate() -> None:
    doc = "# Memory index\n\n- [a](a.md)\n- [b](b.md)\n"
    got = split_markdown(doc)
    assert got == [(1, "# Memory index\n\n- [a](a.md)\n- [b](b.md)")]


def test_fenced_hash_lines_are_not_headings_and_fenced_blanks_do_not_split() -> None:
    doc = "```bash\n# fenced comment\n\nmake build\n```\ntail line\n"
    got = split_markdown(doc)
    assert got == [(1, "```bash\n# fenced comment\n\nmake build\n```\ntail line")]


def test_fenced_hash_lines_never_open_a_section_in_heading_mode() -> None:
    doc = "# Real\n\n```\n# fake heading\n```\n\n# Also real\nbody\n"
    got = split_markdown(doc)
    assert got == [
        (1, "# Real\n\n```\n# fake heading\n```"),
        (7, "# Also real\nbody"),
    ]


def test_plain_paragraph_blocks_split_on_blank_lines() -> None:
    doc = "first spans\ntwo lines\n\n\nsecond block\n"
    got = split_markdown(doc)
    assert got == [
        (1, "first spans\ntwo lines"),
        (5, "second block"),
    ]


def test_long_heading_section_is_bounded_without_losing_lines() -> None:
    lines = ["# Search", *(f"capability {i:04d} " + "detail " * 18 for i in range(180))]
    chunks = split_markdown("\n".join(lines))
    assert len(chunks) > 1
    assert all(len(body) <= 4096 for _, body in chunks)
    assert [line for _, body in chunks for line in body.splitlines()] == lines
    assert chunks[0][0] == 1
    assert chunks[-1][0] > 1


def test_empty_and_whitespace_only_content_yield_zero_candidates() -> None:
    assert split_markdown("") == []
    assert split_markdown("   \n\n\t\n") == []
    assert split_markdown("---\nx: y\n---\n") == []


def test_bom_is_stripped_before_splitting() -> None:
    doc = "\ufeff# H\nbody\n"
    got = split_markdown(doc)
    assert got == [(1, "# H\nbody")]


def test_heading_detection_requires_column_zero_and_a_space() -> None:
    assert heading_level("# ok") == 1
    assert heading_level("###### deep") == 6
    assert heading_level("#") == 1
    assert heading_level("#nope") is None
    assert heading_level("  # indented") is None
    assert heading_level("####### seven") is None


def test_all_blank_preamble_region_is_dropped() -> None:
    doc = "\n\n# H\nbody\n"
    got = split_markdown(doc)
    assert got == [(3, "# H\nbody")]


def test_fixed_source_that_is_a_directory_is_rejected(tmp_path: Path) -> None:
    claude_dir = tmp_path / "CLAUDE.md"
    claude_dir.mkdir()
    with pytest.raises(BridgeIoError) as exc:
        read_source(BridgeSource.PROJECT_CLAUDE_MD, tmp_path, tmp_path)
    assert "not a regular file" in str(exc.value)


def test_fixed_source_with_invalid_utf8_is_typed_io(tmp_path: Path) -> None:
    agents = tmp_path / "AGENTS.md"
    agents.write_bytes(b"\xff\xfe\xfd")
    with pytest.raises(BridgeIoError) as exc:
        read_source(BridgeSource.PROJECT_AGENTS_MD, tmp_path, tmp_path)
    assert "bridge read failed" in str(exc.value)


def test_import_project_claude_md_births_tainted_capsules(tmp_path: Path) -> None:
    claude = tmp_path / "CLAUDE.md"
    claude.write_text(
        "# Project rules\n\nRule 1: Always verify.\n\n# Next section\n\nRule 2: Never assume.\n"
    )

    store = Store.open_disposable_test_store()
    server = MemoryServer(
        store=store, anchor_root=str(tmp_path), project_dir=str(tmp_path)
    )

    res = import__tool.handle(
        server.handler_state(), {"source": "project-claude-md"}, None, NOW, "test-actor"
    )
    data = json.loads(res["content"][0]["text"])

    assert data["outcome"] == "imported"
    assert data["source"] == "project-claude-md"
    assert data["captured"] == 2
    assert data["rejected"] == 0
    assert data["deduped"] == 0

    first_id = data["outcomes"][0]["id"]
    sc = store.capsules.get(first_id)
    assert sc is not None
    assert sc.capsule.authority_class == AuthorityClass.EXTERNALLY_IMPORTED
    assert sc.capsule.instruction_taint is True
    assert sc.capsule.provenance.source == "project-claude-md"
    assert "CLAUDE.md:" in sc.capsule.provenance.anchor

    # Idempotent re-import dedupes everything
    again = import__tool.handle(
        server.handler_state(), {"source": "project-claude-md"}, None, NOW, "test-actor"
    )
    again_data = json.loads(again["content"][0]["text"])
    assert again_data["captured"] == 0
    assert again_data["deduped"] == 2


def test_import_user_claude_md_without_base_resolves_under_home_dir(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "CLAUDE.md").write_text(
        "# Home rule\nThe home dir carries exactly one memory.\n"
    )

    store = Store.open_disposable_test_store()
    server = MemoryServer(
        store=store,
        anchor_root=str(tmp_path),
        home_dir=str(home),
        project_dir=str(tmp_path / "empty-project"),
    )

    res = import__tool.handle(
        server.handler_state(), {"source": "user-claude-md"}, None, NOW, "test-actor"
    )
    data = json.loads(res["content"][0]["text"])
    assert data["outcome"] == "imported"
    assert data["captured"] == 1


def test_import_symlink_leaf_is_a_typed_rejected_outcome_not_a_protocol_error(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text("secret")
    claude = tmp_path / "CLAUDE.md"
    os.symlink(str(outside), str(claude))

    store = Store.open_disposable_test_store()
    server = MemoryServer(
        store=store, anchor_root=str(tmp_path), project_dir=str(tmp_path)
    )

    res = import__tool.handle(
        server.handler_state(), {"source": "project-claude-md"}, None, NOW, "test-actor"
    )
    data = json.loads(res["content"][0]["text"])
    assert data["outcome"] == "rejected"
    assert "whitelisted leaf is a symlink" in data["reason"]


def test_import_param_errors_for_dir(tmp_path: Path) -> None:
    store = Store.open_disposable_test_store()
    server = MemoryServer(
        store=store, anchor_root=str(tmp_path), project_dir=str(tmp_path)
    )

    res1 = import__tool.handle(
        server.handler_state(),
        {"source": "project-claude-md", "dir": "some/dir"},
        None,
        NOW,
        "test-actor",
    )
    assert res1.get("isError") is True
    assert "dir applies only to source" in res1["content"][0]["text"]

    res2 = import__tool.handle(
        server.handler_state(), {"source": "memory-dir"}, None, NOW, "test-actor"
    )
    assert res2.get("isError") is True
    assert "requires a non-empty dir" in res2["content"][0]["text"]


def test_reimport_auto_supersedes_changed_block_and_revives_reverted_block(
    tmp_path: Path,
) -> None:
    claude = tmp_path / "CLAUDE.md"
    claude.write_text("# A\nRule A version 1.\n")

    store = Store.open_disposable_test_store()
    server = MemoryServer(
        store=store, anchor_root=str(tmp_path), project_dir=str(tmp_path)
    )

    # 1. Initial import
    r1 = json.loads(
        import__tool.handle(
            server.handler_state(),
            {"source": "project-claude-md"},
            None,
            NOW,
            "test-actor",
        )["content"][0]["text"]
    )
    cap_a1_id = r1["outcomes"][0]["id"]
    assert store.capsules.get(cap_a1_id) is not None

    # 2. Edit block -> re-import (should auto-supersede)
    claude.write_text("# A\nRule A version 2.\n")
    r2 = json.loads(
        import__tool.handle(
            server.handler_state(),
            {"source": "project-claude-md"},
            None,
            NOW,
            "test-actor",
        )["content"][0]["text"]
    )
    cap_a2_id = r2["outcomes"][0]["id"]
    assert cap_a2_id != cap_a1_id
    assert store.relations.is_superseded(cap_a1_id) is True

    rels = store.relations.list_relations_for(cap_a1_id)
    assert any(
        r.kind == RelationKind.SUPERSEDES
        and r.from_id == cap_a2_id
        and r.to_id == cap_a1_id
        and r.origin == RelationOrigin.IMPORT
        for r in rels
    )

    # 3. Revert block back to version 1 -> re-import (should revive version 1 and supersede version 2)
    claude.write_text("# A\nRule A version 1.\n")
    r3 = json.loads(
        import__tool.handle(
            server.handler_state(),
            {"source": "project-claude-md"},
            None,
            NOW,
            "test-actor",
        )["content"][0]["text"]
    )
    assert r3["deduped"] == 1
    assert store.relations.is_superseded(cap_a1_id) is False
    assert store.relations.is_superseded(cap_a2_id) is True


def test_reimport_tolerates_a_supersedes_edge_with_revocation_history(
    tmp_path: Path,
) -> None:
    """Review a506eb24 #3: U1 rejects `unsupersede` of an edge with revocation
    history; a re-import that reverts the block cannot become `isError` because
    of that — audits and proceeds."""
    claude = tmp_path / "CLAUDE.md"
    claude.write_text("# A\nRule A version 1.\n")
    store = Store.open_disposable_test_store()
    server = MemoryServer(
        store=store, anchor_root=str(tmp_path), project_dir=str(tmp_path)
    )
    r1 = json.loads(
        import__tool.handle(
            server.handler_state(),
            {"source": "project-claude-md"},
            None,
            NOW,
            "test-actor",
        )["content"][0]["text"]
    )
    a1 = r1["outcomes"][0]["id"]
    claude.write_text("# A\nRule A version 2.\n")
    r2 = json.loads(
        import__tool.handle(
            server.handler_state(),
            {"source": "project-claude-md"},
            None,
            NOW,
            "test-actor",
        )["content"][0]["text"]
    )
    a2 = r2["outcomes"][0]["id"]
    # Revoke and re-add the edge: remains LIVE and with revocation history —
    # the case where `unsupersede` (a DELETE) rejects.
    store.revoke_relation(
        RelationKind.SUPERSEDES, a2, a1, actor="test-actor", reason="undo", now=NOW
    )
    store.add_relation(RelationKind.SUPERSEDES, a2, a1, origin="import", now=NOW)
    assert store.relations.is_superseded(a1) is True
    claude.write_text("# A\nRule A version 1.\n")
    r3 = import__tool.handle(
        server.handler_state(), {"source": "project-claude-md"}, None, NOW, "test-actor"
    )
    assert not r3.get("isError"), r3
    assert store.relations.is_superseded(a1) is True, (
        "history stays; nothing was deleted"
    )
    assert any(
        "unsupersede refused" in str(e.reason)
        for e in store.audit.list_audit(subject=a1)
    )
