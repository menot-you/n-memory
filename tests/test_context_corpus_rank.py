"""Corpus search must find the document named by a natural-language question."""

from __future__ import annotations

from collections import Counter

from nmemory.capsule import sha256_hex
from nmemory.repositories.corpus import (
    CorpusChunkInput,
    CorpusOccurrence,
    CorpusSourceRecord,
    path_matches,
    source_role,
)
from nmemory.store import Store


def test_corpus_search_ranks_a_distinctive_partial_match_above_common_words() -> None:
    store = Store.open_disposable_test_store()
    source_key = "markdown:/repo"
    source = CorpusSourceRecord(
        source_key=source_key,
        kind="markdown",
        root="/repo",
        revision="rev-1",
        indexed_at="2026-09-27T00:00:00Z",
        status="ready",
    )

    def chunk(path: str, content: str) -> CorpusChunkInput:
        return CorpusChunkInput(
            content_hash=sha256_hex(content.encode()),
            content=content,
            occurrence=CorpusOccurrence(
                source_key=source_key,
                project_id="pynott",
                path=path,
                start_line=1,
                end_line=1,
                revision="rev-1",
                provenance="tracked markdown",
            ),
        )

    chunks = [
        chunk(f"docs/generic-{i}.md", f"what can a tool do {i} " + "general " * 80)
        for i in range(8)
    ]
    chunks.append(
        chunk(
            "apps/browser/README.md",
            "nBrowser page snapshot, nBrowser site crawl, nBrowser tab actions",
        )
    )
    store.corpus.replace_corpus_source(source, chunks)

    hits = store.corpus.search_corpus_fts(
        ["what can nBrowser do?"], project_id="pynott", kinds=["markdown"], limit=3
    )

    assert hits, "a whole-question AND drops the only nBrowser document"
    assert hits[0].occurrences[0].path == "apps/browser/README.md"
    assert hits[0].score > 0


def test_repository_readme_wins_workspace_overview() -> None:
    store = Store.open_disposable_test_store()
    source = CorpusSourceRecord(
        "markdown:/repo", "markdown", "/repo", "rev-1", "2026-09-27T00:00:00Z", "ready"
    )
    rows = [
        ("README.md", "pynott Python monorepo overview"),
        ("apps/browser/README.md", "browser app workspace structure and overview"),
        ("apps/harness/measurements.py", "read_measurements validates structure"),
    ]
    store.corpus.replace_corpus_source(
        source,
        [
            CorpusChunkInput(
                sha256_hex(content.encode()),
                content,
                CorpusOccurrence(
                    source.source_key, "pynott", path, 1, 1, "rev-1", "tracked-markdown"
                ),
            )
            for path, content in rows
        ],
    )
    hits = store.corpus.search_corpus_fts(
        ["pynott workspace structure and overview"],
        project_id="pynott",
        kinds=["markdown"],
        limit=3,
    )
    assert hits[0].occurrences[0].path == "README.md"


def test_root_readme_wins_workspace_query_under_subtree_scope() -> None:
    store = Store.open_disposable_test_store()
    source = CorpusSourceRecord(
        "markdown:/repo", "markdown", "/repo", "rev-1", "2026-09-27T00:00:00Z", "ready"
    )
    rows = [
        ("README.md", "Python monorepo workspace structure and overview"),
        ("apps/browser/README.md", "browser app workspace structure and overview"),
    ]
    store.corpus.replace_corpus_source(
        source,
        [
            CorpusChunkInput(
                sha256_hex(content.encode()),
                content,
                CorpusOccurrence(
                    source.source_key, "pynott", path, 1, 1, "rev-1", "tracked-markdown"
                ),
            )
            for path, content in rows
        ],
    )
    for query in (
        "workspace structure and overview",
        "pynott workspace structure and overview",
    ):
        hits = store.corpus.search_corpus_fts(
            [query], project_prefix="pynott", kinds=["markdown"], limit=3
        )
        assert hits[0].occurrences[0].path == "README.md"

    component_hits = store.corpus.search_corpus_fts(
        ["browser app workspace structure"],
        project_prefix="pynott",
        kinds=["markdown"],
        limit=3,
    )
    assert component_hits[0].occurrences[0].path == "apps/browser/README.md"


def test_n_prefix_only_applies_to_real_product_names() -> None:
    assert path_matches("docs/ovos.md", Counter({"novos": 1})) == 0
    assert path_matches("apps/browser/README.md", Counter({"nbrowser": 1})) == 1


def test_audit_and_plan_documents_are_not_penalized_by_directory() -> None:
    assert source_role("docs/audits/incident.md") == source_role("docs/incident.md")
    assert source_role("docs/plans/active.md") == source_role("docs/active.md")


def test_precise_release_instruction_beats_generic_readme() -> None:
    store = Store.open_disposable_test_store()
    source = CorpusSourceRecord(
        "code:/repo", "code", "/repo", "rev-1", "2026-09-27T00:00:00Z", "ready"
    )
    generic = CorpusSourceRecord(
        "markdown:/repo", "markdown", "/repo", "rev-1", "2026-09-27T00:00:00Z", "ready"
    )

    def row(record: CorpusSourceRecord, path: str, content: str) -> CorpusChunkInput:
        return CorpusChunkInput(
            sha256_hex(content.encode()),
            content,
            CorpusOccurrence(
                record.source_key, "pynott", path, 1, 1, "rev-1", "tracked-code"
            ),
        )

    store.corpus.replace_corpus_source(
        generic, [row(generic, "README.md", "Python workspace local tools")]
    )
    store.corpus.replace_corpus_source(
        source,
        [
            row(
                source,
                "tools/mcp.sh",
                "Python local MCP release builds a versioned directory and swaps one symlink atomically",
            )
        ],
    )
    hits = store.corpus.search_corpus_fts(
        ["Python solution for local release"],
        project_id="pynott",
        kinds=["code", "markdown"],
        limit=2,
    )
    assert hits[0].occurrences[0].path == "tools/mcp.sh"


def test_corpus_search_uses_persistent_term_index() -> None:
    store = Store.open_disposable_test_store()
    source = CorpusSourceRecord(
        "markdown:/repo", "markdown", "/repo", "rev-1", "2026-09-27T00:00:00Z", "ready"
    )
    content = "quasar documentation"
    store.corpus.replace_corpus_source(
        source,
        [
            CorpusChunkInput(
                sha256_hex(content.encode()),
                content,
                CorpusOccurrence(
                    source.source_key,
                    "pynott",
                    "README.md",
                    1,
                    1,
                    "rev-1",
                    "tracked-markdown",
                ),
            )
        ],
    )
    with store.db.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT rowid FROM corpus_chunks_fts WHERE corpus_chunks_fts MATCH ?",
            ('"quasar"',),
        )
        assert len(cur.fetchall()) == 1


def test_portuguese_database_question_finds_english_database_docs() -> None:
    store = Store.open_disposable_test_store()
    source = CorpusSourceRecord(
        "markdown:/repo", "markdown", "/repo", "rev-1", "2026-09-27T00:00:00Z", "ready"
    )
    rows = [
        (
            "libs/database/README.md",
            "SQLite database writes wait for the lock and honor the busy timeout",
        ),
        ("apps/browser/README.md", "Page snapshots have a timeout"),
    ]
    store.corpus.replace_corpus_source(
        source,
        [
            CorpusChunkInput(
                sha256_hex(content.encode()),
                content,
                CorpusOccurrence(
                    source.source_key, "pynott", path, 1, 1, "rev-1", "tracked-markdown"
                ),
            )
            for path, content in rows
        ],
    )
    hits = store.corpus.search_corpus_fts(
        ["como configurar o timeout e bloqueio de escrita do banco de dados?"],
        project_id="pynott",
        kinds=["markdown"],
        limit=2,
    )
    assert hits[0].occurrences[0].path == "libs/database/README.md"


def test_current_document_beats_historical_commit_metadata() -> None:
    store = Store.open_disposable_test_store()
    for kind, path, content in (
        ("git", "git:abc", "nBrowser " * 30 + "what can it do"),
        ("markdown", "apps/browser/README.md", "nBrowser can crawl pages"),
    ):
        source = CorpusSourceRecord(
            source_key=f"{kind}:/repo",
            kind=kind,
            root="/repo",
            revision="rev-1",
            indexed_at="2026-09-27T00:00:00Z",
            status="ready",
        )
        item = CorpusChunkInput(
            content_hash=sha256_hex(content.encode()),
            content=content,
            occurrence=CorpusOccurrence(
                source_key=source.source_key,
                project_id="pynott",
                path=path,
                start_line=1,
                end_line=1,
                revision="rev-1",
                provenance=kind,
            ),
        )
        store.corpus.replace_corpus_source(source, [item])

    hits = store.corpus.search_corpus_fts(
        ["what can nBrowser do?"],
        project_id="pynott",
        kinds=["markdown", "git"],
        limit=2,
    )
    assert hits[0].occurrences[0].path == "apps/browser/README.md"


def test_question_filler_does_not_beat_the_named_product() -> None:
    store = Store.open_disposable_test_store()
    source = CorpusSourceRecord(
        source_key="markdown:/repo",
        kind="markdown",
        root="/repo",
        revision="rev-1",
        indexed_at="2026-09-27T00:00:00Z",
        status="ready",
    )

    def item(path: str, content: str) -> CorpusChunkInput:
        return CorpusChunkInput(
            content_hash=sha256_hex(content.encode()),
            content=content,
            occurrence=CorpusOccurrence(
                source_key=source.source_key,
                project_id="pynott",
                path=path,
                start_line=1,
                end_line=1,
                revision="rev-1",
                provenance="markdown",
            ),
        )

    store.corpus.replace_corpus_source(
        source,
        [
            item("notes/generic.md", "o que pode fazer? " * 30),
            item("apps/browser/README.md", "nBrowser captures pages and crawls sites"),
        ],
    )
    hits = store.corpus.search_corpus_fts(
        ["o que o nbrowser pode fazer?"],
        project_id="pynott",
        kinds=["markdown"],
        limit=2,
    )
    assert hits[0].occurrences[0].path == "apps/browser/README.md"


def test_current_readme_precedes_a_historical_changelog_entry() -> None:
    store = Store.open_disposable_test_store()
    source = CorpusSourceRecord(
        source_key="markdown:/repo",
        kind="markdown",
        root="/repo",
        revision="rev-1",
        indexed_at="2026-09-27T00:00:00Z",
        status="ready",
    )

    def item(path: str, content: str) -> CorpusChunkInput:
        return CorpusChunkInput(
            content_hash=sha256_hex(content.encode()),
            content=content,
            occurrence=CorpusOccurrence(
                source_key=source.source_key,
                project_id="pynott",
                path=path,
                start_line=1,
                end_line=1,
                revision="rev-1",
                provenance="tracked-markdown",
            ),
        )

    store.corpus.replace_corpus_source(
        source,
        [
            item("changelog.d/browser.md", "nBrowser " * 30 + "crawls sites"),
            item("apps/browser/README.md", "nBrowser crawls sites"),
        ],
    )
    hits = store.corpus.search_corpus_fts(
        ["nBrowser capabilities"],
        project_id="pynott",
        kinds=["markdown"],
        limit=2,
    )
    assert hits[0].occurrences[0].path == "apps/browser/README.md"


def test_portuguese_domain_terms_reach_english_code_explanations() -> None:
    store = Store.open_disposable_test_store()
    source = CorpusSourceRecord(
        source_key="markdown:/repo",
        kind="markdown",
        root="/repo",
        revision="rev-1",
        indexed_at="2026-09-27T00:00:00Z",
        status="ready",
    )
    content = (
        "decay uses the last verification clock; archive hides superseded capsules"
    )
    store.corpus.replace_corpus_source(
        source,
        [
            CorpusChunkInput(
                content_hash=sha256_hex(content.encode()),
                content=content,
                occurrence=CorpusOccurrence(
                    source_key=source.source_key,
                    project_id="pynott",
                    path="nmemory/retrieve.py",
                    start_line=1,
                    end_line=1,
                    revision="rev-1",
                    provenance="tracked-code",
                ),
            )
        ],
    )
    hits = store.corpus.search_corpus_fts(
        ["em que momento elas decaem e somem?"],
        project_id="pynott",
        kinds=["markdown"],
        limit=3,
    )
    assert hits and hits[0].occurrences[0].path == "nmemory/retrieve.py"


def test_package_readme_answers_capability_question_before_internal_docstring() -> None:
    store = Store.open_disposable_test_store()
    source = CorpusSourceRecord(
        source_key="markdown:/repo",
        kind="markdown",
        root="/repo",
        revision="rev-1",
        indexed_at="2026-09-27T00:00:00Z",
        status="ready",
    )

    def item(path: str, content: str) -> CorpusChunkInput:
        return CorpusChunkInput(
            content_hash=sha256_hex(content.encode()),
            content=content,
            occurrence=CorpusOccurrence(
                source_key=source.source_key,
                project_id="pynott",
                path=path,
                start_line=1,
                end_line=1,
                revision="rev-1",
                provenance="tracked-code",
            ),
        )

    store.corpus.replace_corpus_source(
        source,
        [
            item("apps/spawn/nspawn/claude_child.py", "nSPAWN AGY Claude " * 30),
            item("apps/spawn/README.md", "nSPAWN executes AGY with its native runtime"),
        ],
    )
    hits = store.corpus.search_corpus_fts(
        ["como o nspawn execute o AGY via claude"],
        project_id="pynott",
        kinds=["markdown"],
        limit=2,
    )
    assert hits[0].occurrences[0].path == "apps/spawn/README.md"


def test_repeated_topic_term_outweighs_single_scenario_mention() -> None:
    store = Store.open_disposable_test_store()
    source = CorpusSourceRecord(
        source_key="markdown:/repo",
        kind="markdown",
        root="/repo",
        revision="rev-1",
        indexed_at="2026-09-27T00:00:00Z",
        status="ready",
    )

    def item(path: str, content: str) -> CorpusChunkInput:
        return CorpusChunkInput(
            content_hash=sha256_hex(content.encode()),
            content=content,
            occurrence=CorpusOccurrence(
                source_key=source.source_key,
                project_id="pynott",
                path=path,
                start_line=1,
                end_line=1,
                revision="rev-1",
                provenance="tracked-code",
            ),
        )

    store.corpus.replace_corpus_source(
        source,
        [
            item("tools/scenario.py", "scenario suite " * 10),
            item("tools/mutation.py", "mutation tests select one test per mutant"),
        ],
    )
    hits = store.corpus.search_corpus_fts(
        ["por que o mutante roda a suite toda? um mutante por cenário"],
        project_id="pynott",
        kinds=["markdown"],
        limit=2,
    )
    assert hits[0].occurrences[0].path == "tools/mutation.py"


def test_named_product_readme_beats_generic_mcp_module() -> None:
    store = Store.open_disposable_test_store()
    source = CorpusSourceRecord(
        source_key="markdown:/repo",
        kind="markdown",
        root="/repo",
        revision="rev-1",
        indexed_at="2026-09-27T00:00:00Z",
        status="ready",
    )

    def item(path: str, content: str) -> CorpusChunkInput:
        return CorpusChunkInput(
            content_hash=sha256_hex(content.encode()),
            content=content,
            occurrence=CorpusOccurrence(
                source_key=source.source_key,
                project_id="pynott",
                path=path,
                start_line=1,
                end_line=1,
                revision="rev-1",
                provenance="tracked-code",
            ),
        )

    store.corpus.replace_corpus_source(
        source,
        [
            item("apps/harness/body/nott_harness_body/mcp.py", "nHARNESS MCP " * 20),
            item("apps/harness/body/README.md", "nHARNESS exposes an MCP server"),
        ],
    )
    hits = store.corpus.search_corpus_fts(
        ["nHARNESS não é um MCP, expõe um MCP server?"],
        project_id="pynott",
        kinds=["markdown"],
        limit=2,
    )
    assert hits[0].occurrences[0].path == "apps/harness/body/README.md"
