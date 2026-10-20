from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

from nmemory.capsule import sha256_hex
from nmemory.context import execute_context, index_context
from nmemory.repositories.corpus import (
    CorpusChunkInput,
    CorpusOccurrence,
    CorpusSourceRecord,
)
from nmemory.store import Store


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def seed(repo: Path, body: str) -> None:
    repo.mkdir()
    (repo / "README.md").write_text(body)
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "context@example.invalid")
    git(repo, "config", "user.name", "Context Test")
    git(repo, "add", "README.md")
    git(repo, "commit", "-qm", "seed")


def test_new_index_retires_only_the_named_old_root_after_full_success(
    tmp_path: Path,
) -> None:
    old, new, other = (tmp_path / name for name in ("old", "new", "other"))
    for repo in (old, new, other):
        seed(repo, "shared quasar architecture\n")
    store = Store.open_disposable_test_store()
    now = datetime(2026, 9, 27, tzinfo=UTC)
    for repo in (old, other):
        index_context(store, repo, "pynott", None, now)

    index_context(store, new, "pynott", None, now, retire_root=old)
    roots = {source.root for source in store.corpus.list_corpus_sources()}
    assert str(old) not in roots
    assert str(new) in roots and str(other) in roots
    content_hash = sha256_hex(b"shared quasar architecture")
    with store.db.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT source_key FROM corpus_occurrences WHERE content_hash = ?",
            (content_hash,),
        )
        source_keys = {str(row[0]) for row in cur.fetchall()}
    assert source_keys == {
        f"markdown:{new}",
        f"markdown:{other}",
    }


def test_tracked_python_and_shell_are_searchable_without_untracked_bytes(
    tmp_path: Path,
) -> None:
    (tmp_path / "engine.py").write_text(
        '"""Decay starts at the last passing verification."""\n'
        "def decay_factor():\n    return 1\n"
    )
    (tmp_path / "install.sh").write_text(
        "#!/bin/sh\n# Atomic release swaps one symlink.\n"
    )
    (tmp_path / "secret.py").write_text("UNTRACKED_SECRET_SENTINEL\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_engine.py").write_text(
        '"""TEST_ONLY_DECOY is not product documentation."""\n'
    )
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "context@example.invalid")
    git(tmp_path, "config", "user.name", "Context Test")
    git(tmp_path, "add", "engine.py", "install.sh", "tests/test_engine.py")
    git(tmp_path, "commit", "-qm", "seed")

    store = Store.open_disposable_test_store()
    now = datetime(2026, 9, 27, tzinfo=UTC)
    indexed = index_context(store, tmp_path, "pynott", ["markdown", "code"], now)
    assert all(source["status"] == "ready" for source in indexed["sources"])
    decoy = execute_context(
        store,
        "TEST_ONLY_DECOY",
        project_id="pynott",
        sources=["code"],
        anchor_root=tmp_path,
        now=now,
    )
    assert not decoy["results"]
    for query, path in [
        ("last passing verification", "engine.py"),
        ("atomic release symlink", "install.sh"),
    ]:
        result = execute_context(
            store,
            query,
            project_id="pynott",
            sources=["code"],
            anchor_root=tmp_path,
            now=now,
        )
        assert any(
            provenance["address"] == path
            for hit in result["results"]
            for provenance in hit["provenance"]
        )
        assert "UNTRACKED_SECRET_SENTINEL" not in str(result)
        markdown_only = execute_context(
            store,
            query,
            project_id="pynott",
            sources=["markdown"],
            anchor_root=tmp_path,
            now=now,
        )
        assert not markdown_only["results"]


def test_invalid_python_does_not_disable_markdown_or_rustdoc(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# Canonical workspace overview\n")
    (tmp_path / "broken.py").write_text("def broken(:\n")
    (tmp_path / "lib.rs").write_text("/// Stable Rustdoc note\n")
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "context@example.invalid")
    git(tmp_path, "config", "user.name", "Context Test")
    git(tmp_path, "add", "README.md", "broken.py", "lib.rs")
    git(tmp_path, "commit", "-qm", "seed")

    store = Store.open_disposable_test_store()
    indexed = index_context(
        store,
        tmp_path,
        "pynott",
        ["markdown", "code", "rustdoc"],
        datetime(2026, 9, 27, tzinfo=UTC),
    )
    statuses = {row["source"]: row["status"] for row in indexed["sources"]}
    assert statuses == {"markdown": "ready", "code": "unavailable", "rustdoc": "ready"}
    for query, kind, path in (
        ("canonical workspace overview", "markdown", "README.md"),
        ("stable Rustdoc note", "rustdoc", "lib.rs"),
    ):
        hits = store.corpus.search_corpus_fts(
            [query], project_id="pynott", kinds=[kind], limit=1
        )
        assert hits and hits[0].occurrences[0].path == path


def test_existing_v1_store_accepts_code_source_without_losing_old_sources() -> None:
    store = Store.open_disposable_test_store()
    old = CorpusSourceRecord(
        "markdown:/old", "markdown", "/old", "old", "2026-09-27T00:00:00Z", "ready"
    )
    store.corpus.replace_corpus_source(old, [])
    with store.db.write_boundary(), store.db.connection() as conn, conn.cursor() as cur:
        cur.execute("ALTER TABLE corpus_sources RENAME TO corpus_sources_new")
        cur.execute(
            "CREATE TABLE corpus_sources (source_key TEXT PRIMARY KEY, "
            "kind TEXT NOT NULL CHECK (kind IN ('markdown', 'git', 'rustdoc')), "
            "root TEXT NOT NULL, revision TEXT NOT NULL, indexed_at TEXT NOT NULL, "
            "status TEXT NOT NULL CHECK (status IN ('ready', 'unavailable')), detail TEXT)"
        )
        cur.execute("INSERT INTO corpus_sources SELECT * FROM corpus_sources_new")
        cur.execute("DROP TABLE corpus_sources_new")
    content = "code source survives expansion"
    code = CorpusSourceRecord(
        "code:/new", "code", "/new", "new", "2026-09-27T00:00:00Z", "ready"
    )
    store.corpus.replace_corpus_source(
        code,
        [
            CorpusChunkInput(
                sha256_hex(content.encode()),
                content,
                CorpusOccurrence(
                    code.source_key, "pynott", "engine.py", 1, 1, "new", "tracked-code"
                ),
            )
        ],
    )
    assert {record.source_key for record in store.corpus.list_corpus_sources()} == {
        old.source_key,
        code.source_key,
    }
    reopened = Store.open(store.db.path)
    assert reopened.corpus.search_corpus_fts(
        ["expansion"], project_id="pynott", kinds=["code"], limit=1
    )
