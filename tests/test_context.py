from __future__ import annotations

import json
import os
import stat
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from storefile import default_store_path

from nmemory.capsule import (
    AuthorityClass,
    Capsule,
    Freshness,
    Provenance,
    Scope,
    sha256_hex,
)
from nmemory.context import (
    ALL_CONTEXT_SOURCES,
    ALL_INDEX_SOURCES,
    GroupedCandidate,
    execute_context,
    prepare_files,
    prepare_git,
    rustdoc_chunks,
    take_grouped_candidate,
)
from nmemory.context_readers import (
    MAX_ADAPTER_OUTPUT,
    ContextPathError,
    ContextProvenance,
    normalize_chunk,
    query_adapter,
    query_adapter_program,
    repository_head,
    run_json_adapter,
)
from nmemory.embedding import EmbeddingClient
from nmemory.ingest import effective_defaults
from nmemory.project import DEFAULT_PROJECT_ID
from nmemory.server import MemoryServer
from nmemory.store import Store
from nmemory.tools import context as context_tool
from nmemory.tools import index as index_tool
from nmemory.tools import ingest as ingest_tool
from nmemory.tools import purge as purge_tool
from nmemory.tools import retrieve as retrieve_tool

NOW = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)


def git(repo: Path, args: list[str]) -> None:
    res = subprocess.run(
        ["git", "-C", str(repo)] + args, capture_output=True, check=False
    )
    assert res.returncode == 0, f"git {args} failed: {res.stderr.decode()}"


def seed_repo(repo: Path) -> None:
    git(repo, ["init", "-q"])
    git(repo, ["config", "user.email", "context@example.invalid"])
    git(repo, ["config", "user.name", "Context Test"])


def fake_adapter(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IRUSR)


def cap_at(content: str, project: str, anchor: str) -> Capsule:
    return Capsule(
        content=content,
        provenance=Provenance(
            source="test",
            anchor=anchor,
            source_hash=sha256_hex(content.encode("utf-8")),
        ),
        confidence=0.9,
        freshness=Freshness(valid_from=NOW),
        scope=Scope(project_id=project),
        authority_class=AuthorityClass.USER_STATED,
        instruction_taint=False,
    )


@pytest.mark.parametrize("provider", ["gemini", "openai", "openai-compatible"])
def test_semantic_status_names_the_client_provider(
    monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    monkeypatch.setenv("NOTT_NMEMORY_EMBED_PROVIDER", provider)
    monkeypatch.setenv("NOTT_NMEMORY_EMBED_MODEL", "test-model")
    monkeypatch.setenv("NOTT_NMEMORY_EMBED_DIMENSION", "768")
    monkeypatch.setenv(
        "NOTT_NMEMORY_EMBED_ENDPOINT", "http://localhost:11434/v1/embeddings"
    )
    client = EmbeddingClient(api_key="test-key")
    monkeypatch.setenv("NOTT_NMEMORY_EMBED_PROVIDER", "openai")
    store = Store.open_disposable_test_store()
    result = execute_context(store, "probe", sources=[], embeddings=client)
    assert result["semantic"] == {"provider": provider, "status": "ready"}


@pytest.mark.parametrize(
    ("provider", "key"), [("gemini", "GEMINI_API_KEY"), ("openai", "OPENAI_API_KEY")]
)
@pytest.mark.parametrize("with_client", [True, False])
def test_semantic_status_names_the_missing_provider_key(
    monkeypatch: pytest.MonkeyPatch, provider: str, key: str, with_client: bool
) -> None:
    monkeypatch.setenv("NOTT_NMEMORY_EMBED_PROVIDER", provider)
    monkeypatch.delenv(key, raising=False)
    client = EmbeddingClient() if with_client else None
    store = Store.open_disposable_test_store()
    result = execute_context(store, "probe", sources=[], embeddings=client)
    semantic = result["semantic"]
    assert semantic["status"] == "unavailable_lexical_only"
    assert semantic["detail"] == (
        f"{provider} embedding failed (OperatorAction): {key} is not configured"
    )


@pytest.mark.parametrize(
    ("provider", "model", "detail"),
    [
        ("bogus", None, "NOTT_NMEMORY_EMBED_PROVIDER='bogus'"),
        ("openai-compatible", None, "NOTT_NMEMORY_EMBED_MODEL"),
        ("openai-compatible", "test-model", "NOTT_NMEMORY_EMBED_DIMENSION"),
    ],
)
@pytest.mark.parametrize("with_client", [False, True])
def test_context_keeps_lexical_results_with_invalid_embedding_config(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    model: str | None,
    detail: str,
    with_client: bool,
) -> None:
    store = Store.open_disposable_test_store()
    cid, _ = store.append(cap_at("Lexical fallback", "nott", "config:1"), NOW)
    monkeypatch.setenv("NOTT_NMEMORY_EMBED_PROVIDER", provider)
    monkeypatch.delenv("NOTT_NMEMORY_EMBED_MODEL", raising=False)
    monkeypatch.delenv("NOTT_NMEMORY_EMBED_DIMENSION", raising=False)
    if model is not None:
        monkeypatch.setenv("NOTT_NMEMORY_EMBED_MODEL", model)
    client = EmbeddingClient() if with_client else None
    if client is not None:
        assert client.space_provider == "unconfigured"
        assert client.config_error is not None
        monkeypatch.setenv("NOTT_NMEMORY_EMBED_PROVIDER", "gemini")
    result = execute_context(
        store, "lexical", sources=["memory"], embeddings=client, now=NOW
    )
    assert [row["id"] for row in result["results"]] == [cid]
    semantic = result["semantic"]
    assert semantic["status"] == "unavailable_lexical_only"
    assert detail in semantic["detail"]
    if client is not None:
        assert semantic["detail"] == client.config_error


def test_normalization_and_rustdoc_extraction_are_exact_and_deterministic() -> None:
    assert normalize_chunk("  one  \r\n\r\n\r\n two\n") == "one\n\n two"
    docs = rustdoc_chunks("/// Alpha\n/// beta\nfn x() {}\n// ordinary\n")
    assert docs == [(1, 2, "Alpha\nbeta")]
    assert len(sha256_hex(normalize_chunk("x").encode("utf-8"))) == 64


def test_adapter_boundary_requires_an_absolute_regular_file_and_bounds_output(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError) as exc:
        run_json_adapter(Path("relative-adapter"), [], Path("/"))
    assert "not absolute" in str(exc.value)

    cat_bin = Path(os.path.realpath("/bin/cat"))
    small = tmp_path / "small.json"
    small.write_text('{"ready":true}')
    res = run_json_adapter(cat_bin, [str(small)], tmp_path)
    assert res == {"ready": True}

    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b"x" * (MAX_ADAPTER_OUTPUT + 1))
    with pytest.raises(ValueError) as exc:
        run_json_adapter(cat_bin, [str(oversized)], tmp_path)
    assert "exceeded 1 MiB" in str(exc.value)

    linked = tmp_path / "linked-adapter"
    os.symlink(str(cat_bin), str(linked))
    with pytest.raises(ValueError) as exc:
        run_json_adapter(linked, [], tmp_path)
    assert "not a regular non-symlink file" in str(exc.value)


def test_git_corpus_includes_body_and_paths_but_never_diff_bytes(
    tmp_path: Path,
) -> None:
    repo = tmp_path
    (repo / "src").mkdir(parents=True, exist_ok=True)
    (repo / "src" / "lib.rs").write_text("DIFF_BYTES_MUST_NEVER_ENTER_CONTEXT\n")
    seed_repo(repo)
    git(repo, ["add", "src/lib.rs"])
    git(repo, ["commit", "-qm", "subject sentinel", "-m", "body sentinel"])
    git(repo, ["checkout", "-qb", "topic"])
    (repo / "topic.txt").write_text("topic diff bytes\n")
    git(repo, ["add", "topic.txt"])
    git(repo, ["commit", "-qm", "topic sentinel"])
    git(repo, ["checkout", "-q", "-"])
    (repo / "base.txt").write_text("base diff bytes\n")
    git(repo, ["add", "base.txt"])
    git(repo, ["commit", "-qm", "base sentinel"])
    git(repo, ["merge", "-q", "--no-ff", "-m", "merge sentinel", "topic"])

    source = prepare_git(repo, None, NOW)
    assert len(source.chunks) == 4
    content = next(
        c.content for c in source.chunks if "subject subject sentinel" in c.content
    )
    assert "subject subject sentinel" in content
    assert "body body sentinel" in content
    assert "path src/lib.rs" in content

    merge = next(
        c.content for c in source.chunks if "subject merge sentinel" in c.content
    )
    assert "path topic.txt" in merge
    assert all(
        "DIFF_BYTES_MUST_NEVER_ENTER_CONTEXT" not in c.content for c in source.chunks
    )


def test_tracked_file_under_a_symlinked_parent_cannot_escape_the_root(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    repo.mkdir()
    outside.mkdir()
    (repo / "docs").mkdir()
    (repo / "docs" / "context.md").write_text("inside")
    seed_repo(repo)
    git(repo, ["add", "docs/context.md"])
    git(repo, ["commit", "-qm", "seed context"])

    import shutil

    shutil.rmtree(repo / "docs")
    (outside / "context.md").write_text("outside secret")
    os.symlink(str(outside), str(repo / "docs"))

    with pytest.raises(ContextPathError):
        prepare_files(repo, None, NOW)


def test_tracked_file_cannot_escape_to_a_sibling_with_the_same_path_prefix(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    sibling = tmp_path / "repo-private"
    repo.mkdir()
    sibling.mkdir()
    (repo / "docs").mkdir()
    (repo / "docs" / "context.md").write_text("tracked")
    seed_repo(repo)
    git(repo, ["add", "docs/context.md"])
    git(repo, ["commit", "-qm", "seed context"])
    import shutil

    shutil.rmtree(repo / "docs")
    (sibling / "context.md").write_text("private sibling")
    os.symlink(str(sibling), str(repo / "docs"))
    with pytest.raises(ContextPathError):
        prepare_files(repo, None, NOW)


def test_exact_grouping_combines_providers_before_limit() -> None:
    grouped: dict[str, GroupedCandidate] = {}
    c1 = grouped.setdefault("same", GroupedCandidate())
    c1.add(
        "memory-1",
        "task-1-same-bytes",
        "same bytes",
        "memory",
        [],
        False,
        "memory_lexical",
        1,
    )

    c2 = grouped.setdefault("same", GroupedCandidate())
    c2.add("ctx-1", None, "same bytes", "rustdoc", [], True, "corpus_lexical", 1)

    assert len(grouped) == 1
    candidate = grouped["same"]
    assert len(candidate.providers) == 2
    assert candidate.id == "memory-1"
    assert candidate.handle == "task-1-same-bytes"
    assert candidate.instruction_taint is True


def test_location_grouping_requires_same_revision_path_and_overlapping_span() -> None:
    def provenance(start: int, end: int, rev: str) -> ContextProvenance:
        return ContextProvenance(
            provider="codegraph",
            address="src/lib.rs",
            start_line=start,
            end_line=end,
            revision=rev,
        )

    grouped: dict[str, GroupedCandidate] = {}
    first = [provenance(10, 20, "rev-a")]
    key, cand = take_grouped_candidate(grouped, "hash-a", first)
    cand.add(
        "codegraph-1", None, "first excerpt", "codegraph", first, True, "codegraph", 1
    )
    grouped[key] = cand

    overlapping = [
        ContextProvenance(
            provider="jbcontext",
            address="src/lib.rs",
            start_line=20,
            end_line=30,
            revision="rev-a",
        )
    ]
    key, cand = take_grouped_candidate(grouped, "hash-b", overlapping)
    cand.add(
        "jbcontext-1",
        None,
        "different overlapping excerpt",
        "jbcontext",
        overlapping,
        True,
        "jbcontext",
        1,
    )
    grouped[key] = cand
    assert len(grouped) == 1
    assert len(next(iter(grouped.values())).provenance) == 2

    for h, span in [
        ("hash-c", provenance(20, 30, "rev-b")),
        ("hash-d", provenance(31, 40, "rev-a")),
    ]:
        rows = [span]
        k, c = take_grouped_candidate(grouped, h, rows)
        c.add(
            h,
            None,
            "similar words do not merge",
            "codegraph",
            rows,
            True,
            "codegraph",
            2,
        )
        grouped[k] = c

    assert len(grouped) == 3


def test_omitted_source_lists_select_the_complete_closed_sets() -> None:
    assert len(ALL_CONTEXT_SOURCES) == 7
    assert len(ALL_INDEX_SOURCES) == 4


def test_index_then_context_is_idempotent_grouped_read_only_and_staleness_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    repo = tmp_path
    (repo / "src").mkdir(parents=True, exist_ok=True)
    (repo / "README.md").write_text(
        "Shared quasar evidence\n\nMarkdown-only quasar note\n"
    )
    (repo / "COPY.md").write_text("Shared quasar evidence\n")
    (repo / "src" / "lib.rs").write_text(
        "/// Shared quasar evidence\npub fn quasar() {}\n"
    )
    seed_repo(repo)
    git(repo, ["add", "COPY.md", "README.md", "src/lib.rs"])
    git(repo, ["commit", "-qm", "git-only quasar metadata"])

    store = Store.open_disposable_test_store()
    content = "Memory-only quasar reminder"
    cid, _ = store.append(cap_at(content, "projetodeteste", "memory:1"), NOW)
    store.set_birth_classification(cid, "task", "project", NOW)

    server = MemoryServer(store=store, anchor_root=str(repo))

    # 1. Index
    res = index_tool.handle(
        server.handler_state(),
        {"root": str(repo), "project_id": "projetodeteste"},
        None,
        NOW,
        "test",
    )
    index = json.loads(res["content"][0]["text"])
    assert len(index["sources"]) == 4
    assert all(
        row["status"] == "ready" and row["changed"] is True for row in index["sources"]
    )
    assert index["unique_chunks"] == 3

    md_row = next(r for r in index["sources"] if r["source"] == "markdown")
    assert md_row["chunks"] == 2
    assert md_row["occurrences"] == 3

    # 2. Idempotent re-index
    res_noop = index_tool.handle(
        server.handler_state(),
        {"root": str(repo), "project_id": "projetodeteste"},
        None,
        NOW,
        "test",
    )
    noop = json.loads(res_noop["content"][0]["text"])
    assert all(row["changed"] is False for row in noop["sources"])

    # 3. Context query
    res_ctx = context_tool.handle(
        server.handler_state(),
        {
            "query": "quasar",
            "project_id": "projetodeteste",
            "sources": ["memory", "markdown", "git", "rustdoc"],
            "limit": 10,
            "token_budget": 2000,
        },
        None,
        NOW,
        "test",
    )
    context = json.loads(res_ctx["content"][0]["text"])
    results = context["results"]

    memory = next(r for r in results if r["excerpt"] == content)
    assert memory["id"] == "cap-1"
    assert memory["handle"] == "task-1-memory-only-quasar-reminder"
    assert memory["kind"] == "task"
    assert "memory_lexical" in memory["matched_by"]
    assert memory["rank"] >= 1

    shared = next(r for r in results if r["excerpt"] == "Shared quasar evidence")
    assert "markdown" in shared["providers"]
    assert "rustdoc" in shared["providers"]
    assert "corpus_lexical" in shared["matched_by"]
    assert len(shared["provenance"]) == 3
    assert len(shared["also_seen_in"]) == 2
    assert context["exact_duplicates_grouped"] >= 1
    assert context["semantic"]["status"] == "unavailable_lexical_only"
    assert all(s["status"] == "ready" for s in context["sources"])

    # 4. Stale detection
    (repo / "README.md").write_text(
        "Shared quasar evidence\n\nChanged after indexing\n"
    )
    git(repo, ["add", "README.md"])
    git(repo, ["commit", "-qm", "advance context revision"])

    res_stale = context_tool.handle(
        server.handler_state(),
        {
            "query": "quasar",
            "project_id": "projetodeteste",
            "sources": ["markdown"],
        },
        None,
        NOW,
        "test",
    )
    stale = json.loads(res_stale["content"][0]["text"])
    assert stale["sources"][0]["status"] == "stale"
    assert stale["sources"][0]["revision"] != stale["sources"][0]["current_revision"]


def test_context_applies_the_exact_effective_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("NOTT_NMEMORY_PROJECT", raising=False)
    store = Store.open_disposable_test_store()
    exact_id, _ = store.append(
        cap_at("Scoped quasar exact evidence", "nott", "scope:1"), NOW
    )
    store.append(cap_at("Scoped quasar child evidence", "nott/child", "scope:2"), NOW)
    server = MemoryServer(store=store)

    exact_res = context_tool.handle(
        server.handler_state(),
        {
            "query": "quasar",
            "project_id": "nott",
            "sources": ["memory"],
        },
        None,
        NOW,
        "test",
    )
    exact = json.loads(exact_res["content"][0]["text"])
    assert [row["id"] for row in exact["results"]] == [exact_id]
    assert exact["scope"] == {
        "project_id": "nott",
        "match": "exact",
        "source": "caller",
    }

    default_res = context_tool.handle(
        server.handler_state(),
        {"query": "quasar", "sources": ["memory"]},
        None,
        NOW,
        "test",
    )
    default = json.loads(default_res["content"][0]["text"])
    # U4 (nmemory-curator) changed the default POLICY: the server's default
    # project is AUDIENCE and fences by SUBTREE — `pynott` reaches `pynott/x`.
    # Measured 2026-09-15: no reader passed prefix and the 88 capsules of
    # `pynott/nmemory-escrita` were 100% unread. Explicit remains
    # EXACT — the falsifier below binds both halves.
    assert default["results"] == []
    assert default["scope"] == {
        "project_id": DEFAULT_PROJECT_ID,
        "match": "subtree",
        "source": "literal_default",
    }

    # The new falsifier: a child of the default is served by the default and NOT
    # by explicit — the two fences of the new policy in the same store.
    child_id, _ = store.append(
        cap_at(
            "Scoped quasar child under default subtree",
            f"{DEFAULT_PROJECT_ID}/child",
            "scope:3",
        ),
        NOW,
    )
    subtree_res = context_tool.handle(
        server.handler_state(),
        {"query": "quasar child", "sources": ["memory"]},
        None,
        NOW,
        "test",
    )
    subtree = json.loads(subtree_res["content"][0]["text"])
    assert [row["id"] for row in subtree["results"]] == [child_id]

    exact_child_res = context_tool.handle(
        server.handler_state(),
        {
            "query": "quasar child",
            "project_id": DEFAULT_PROJECT_ID,
            "sources": ["memory"],
        },
        None,
        NOW,
        "test",
    )
    exact_child = json.loads(exact_child_res["content"][0]["text"])
    assert [row["id"] for row in exact_child["results"]] == [], (
        "an explicit project_id keeps the exact fence — the caller chose it"
    )

    empty_res = context_tool.handle(
        server.handler_state(),
        {"query": "no-such-context", "sources": ["memory"]},
        None,
        NOW,
        "test",
    )
    empty = json.loads(empty_res["content"][0]["text"])
    assert empty["results"] == []
    assert empty["scope"] == default["scope"]


def test_retrieve_declares_scope_when_grounded_and_when_abstaining(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Default scope depends on the environment: with `NOTT_NMEMORY_PROJECT` in the env
    # (the repo-wide battery exports it), default ceases to be literal and the test
    # would read red due to ENVIRONMENT, not product — the first false red
    # on the CLAUDE.md list.
    monkeypatch.delenv("NOTT_NMEMORY_PROJECT", raising=False)
    store = Store.open_disposable_test_store()
    cid, _ = store.append(
        cap_at("Scoped nebula evidence", "nott/child", "scope:2"), NOW
    )
    server = MemoryServer(store=store)

    grounded_res = retrieve_tool.handle(
        server.handler_state(),
        {"terms": ["nebula"], "project_id": "nott/child"},
        None,
        NOW,
        "test",
    )
    grounded = json.loads(grounded_res["content"][0]["text"])
    assert grounded["outcome"] == "grounded"
    assert [row["id"] for row in grounded["results"]] == [cid]
    assert grounded["scope"] == {
        "project_id": "nott/child",
        "match": "exact",
        "source": "caller",
    }

    abstain_res = retrieve_tool.handle(
        server.handler_state(), {"terms": ["no-such-memory"]}, None, NOW, "test"
    )
    abstain = json.loads(abstain_res["content"][0]["text"])
    assert abstain["outcome"] == "abstain"
    # U2a: what this test protects is that the scope is DECLARED in both
    # outcomes, with the resolved project and its source — and still protects.
    # `match` was `exact` because the retrieve default fenced the node; since
    # it moved to AUDIENCE (`server.py:2920`), as `context` already did,
    # the default is the tree. The exact fence didn't disappear: it is the branch
    # above, explicit `project_id`, which remains `exact`/`caller`.
    assert abstain["scope"] == {
        "project_id": DEFAULT_PROJECT_ID,
        "match": "prefix",
        "source": "literal_default",
    }


def test_omitted_ingest_and_recall_share_the_client_project() -> None:
    store = Store.open_disposable_test_store()
    server = MemoryServer(store=store)
    server.client_name = "Nott Client"  # what the SDK handshake records

    ingest_res = ingest_tool.handle(
        server.handler_state(),
        {
            "content": "Sessionboundquasar effective scope evidence",
            "kind": "task",
            "source": "test",
            "anchor": "scope:3",
        },
        None,
        NOW,
        "test",
    )
    ingested = json.loads(ingest_res["content"][0]["text"])
    cid = ingested["outcomes"][0]["id"]

    context_res = context_tool.handle(
        server.handler_state(),
        {"query": "sessionboundquasar", "sources": ["memory"]},
        None,
        NOW,
        "test",
    )
    context = json.loads(context_res["content"][0]["text"])
    assert [row["id"] for row in context["results"]] == [cid]
    # U4: the default fences by SUBTREE; the capsule is at the root, and the child
    # would belong to the same audience.
    #
    # U2a SUPERSEDES the second half of this sentence. It said "retrieve, which
    # lacks an audience default, remains exact — both fences report what
    # they are", and it was DIVERGENCE described as if it were design: the same
    # omission gave two audiences in two sibling tools. Retrieve received
    # the same default (`server.py:2920`). What this test protects —
    # that ingest and recall share the SAME project, in the assertion below —
    # did not change; only the two tools stopped disagreeing on its reach.
    # The echo wording remains distinct (`subtree` here, `prefix` in
    # retrieve): it is vocabulary of each tool, not a different fence.
    assert context["scope"] == {
        "project_id": "nott-client",
        "match": "subtree",
        "source": "client_info",
    }

    retrieve_res = retrieve_tool.handle(
        server.handler_state(), {"terms": ["sessionboundquasar"]}, None, NOW, "test"
    )
    retrieved = json.loads(retrieve_res["content"][0]["text"])
    assert [row["id"] for row in retrieved["results"]] == [cid]
    assert retrieved["scope"]["project_id"] == context["scope"]["project_id"]
    assert retrieved["scope"]["match"] == "prefix"


def test_effective_project_source_and_classify_default_are_canonical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NOTT_NMEMORY_PROJECT", "environment-project")
    defaults = effective_defaults(None, None, None)
    assert defaults.project_id == "environment-project"
    assert defaults.source == "env"

    classify_source = (
        Path(__file__).resolve().parent.parent / "nmemory" / "classify.py"
    ).read_text()
    assert "from .project import DEFAULT_PROJECT_ID" in classify_source
    assert "\nDEFAULT_PROJECT_ID =" not in classify_source


def test_context_cli_uses_subtree_default_and_semantic_query_lane(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    from nmemory.commands import context as context_command

    store = Store.open_disposable_test_store()
    seen: dict[str, Any] = {}
    project_root = tmp_path / "pynott"
    project_root.mkdir()
    monkeypatch.chdir(project_root)
    monkeypatch.setenv("NOTT_NMEMORY_PROJECT", "different-environment-default")
    monkeypatch.setattr(context_command, "open_store_from_args", lambda _args: store)

    def capture(**kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs)
        return {"results": []}

    monkeypatch.setattr(context_command, "execute_context", capture)
    context_command.cmd_context_search(["where is the design"])
    assert json.loads(capsys.readouterr().out) == {"results": []}
    assert seen["project_id"] is None
    assert seen["project_prefix"] == "pynott"
    assert seen["embeddings"] is not None


def test_stdio_project_reaches_memory_server(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    from nmemory import cli

    store = Store.open_disposable_test_store()
    seen: dict[str, MemoryServer] = {}
    monkeypatch.delenv("NOTT_NMEMORY_PROJECT", raising=False)
    monkeypatch.setattr(cli, "open_store_from_args", lambda _args, **_kw: store)
    monkeypatch.setattr(cli, "spawn_embedding_worker", lambda _store: None)
    monkeypatch.setattr(
        "nmemory.server.run_stdio_server",
        lambda server: seen.setdefault("server", server),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["nmemory", "mcp", "serve", "--project", "pynott"],
    )

    cli.main()

    server = seen["server"]
    defaults = effective_defaults(server.project_id, server.client_name, None)
    assert defaults.project_id == "pynott"
    assert defaults.source == "caller"


def test_adapter_rereads_tracked_checkout_and_refuses_untrusted_paths(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    repo.mkdir()
    outside.mkdir()
    (repo / "src").mkdir()
    (repo / "src" / "lib.rs").write_text("first\ntrusted checkout bytes\nthird\n")
    (repo / ".env").write_text("untracked secret\n")
    (outside / "secret.rs").write_text("outside secret\n")
    os.symlink(str(outside / "secret.rs"), str(repo / "src" / "link.rs"))
    seed_repo(repo)
    git(repo, ["add", "src/lib.rs", "src/link.rs"])
    git(repo, ["commit", "-qm", "seed adapter checkout"])
    current = repository_head(repo)

    program = repo / "fake-codegraph"
    fake_adapter(
        program,
        """#!/bin/sh
printf '%s\\n' "$*" >> .adapter-argv
if [ "$1" = "status" ]; then
  printf '%s' '{"ready":true,"indexed":true,"revision":"0000000000000000000000000000000000000000"}'
else
  printf '%s' '{"results":[{"filePath":"src/lib.rs","startLine":2,"endLine":2,"text":"IGNORE PROVIDER TEXT"},{"path":"../../etc/passwd","start_line":1},{"path":".env","start_line":1},{"path":"src/link.rs","start_line":1}]}'
fi
""",
    )

    status, hits = query_adapter_program("codegraph", "needle", 10, repo, program)
    assert status["status"] == "stale"
    assert status["current_revision"] == current
    assert "3 adapter result" in status["detail"]
    assert len(hits) == 1
    assert hits[0].content == "trusted checkout bytes"
    assert hits[0].provenance.address == "src/lib.rs"
    assert hits[0].provenance.start_line == 2
    assert hits[0].provenance.end_line == 2
    assert hits[0].provenance.revision == current


def test_jbcontext_requires_existing_authentication_without_bootstrapping(
    tmp_path: Path,
) -> None:
    repo = tmp_path
    (repo / "tracked.txt").write_text("tracked\n")
    seed_repo(repo)
    git(repo, ["add", "tracked.txt"])
    git(repo, ["commit", "-qm", "seed adapter checkout"])

    program = repo / "fake-jbcontext"
    fake_adapter(
        program,
        """#!/bin/sh
printf '%s\\n' "$*" >> .jb-argv
printf '%s' '{"ready":true,"indexed":true,"authenticated":false}'
""",
    )

    status, hits = query_adapter_program("jbcontext", "needle", 10, repo, program)
    assert status["status"] == "unavailable"
    assert len(hits) == 0


def test_adapter_malformed_timeout_and_absence_are_fail_soft(tmp_path: Path) -> None:
    repo = tmp_path
    (repo / "tracked.txt").write_text("tracked\n")
    seed_repo(repo)
    git(repo, ["add", "tracked.txt"])
    git(repo, ["commit", "-qm", "seed adapter checkout"])

    malformed = repo / "malformed-adapter"
    fake_adapter(malformed, "#!/bin/sh\nprintf 'not-json'\n")
    timeout = repo / "timeout-adapter"
    fake_adapter(timeout, "#!/bin/sh\nsleep 4\n")
    missing = repo / "missing-adapter"

    for prog, expected in [
        (malformed, "not JSON"),
        (timeout, "timed out"),
        (missing, "No such file"),
    ]:
        status, hits = query_adapter_program("codegraph", "needle", 10, repo, prog)
        assert status["status"] == "unavailable"
        assert expected in status.get("detail", "")
        assert len(hits) == 0


def test_context_and_index_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path
    (repo / "README.md").write_text("Unique CLI context testing note\n")
    seed_repo(repo)
    git(repo, ["add", "README.md"])
    git(repo, ["commit", "-qm", "initial cli context"])

    test_url = default_store_path("ctxcli")
    monkeypatch.setenv("NOTT_DATABASE_DIR", str(Path(test_url).parent))

    import sys

    from nmemory.cli import main

    # 1. Test index CLI
    monkeypatch.setattr(
        sys,
        "argv",
        ["nmemory", "index", "build", "--root", str(repo), "--project", "testproj"],
    )
    main()
    captured_index = capsys.readouterr()
    index_data = json.loads(captured_index.out)
    assert index_data["label"] == "ADVISORY_NOT_AUTHORITY"
    assert any(
        s["source"] == "markdown" and s["status"] == "ready"
        for s in index_data["sources"]
    )

    # 2. Test context CLI
    monkeypatch.setattr(
        sys,
        "argv",
        ["nmemory", "context", "search", "testing", "--project", "testproj"],
    )
    main()
    captured_ctx = capsys.readouterr()
    ctx_data = json.loads(captured_ctx.out)
    assert ctx_data["label"] == "ADVISORY_NOT_AUTHORITY"
    assert any(
        "Unique CLI context testing note" in r["excerpt"] for r in ctx_data["results"]
    )


@pytest.mark.parametrize(
    ("options", "environment", "named"),
    [
        (
            [],
            {"NOTT_NMEMORY_EMBED_PROVIDER": "cohere"},
            "NOTT_NMEMORY_EMBED_PROVIDER='cohere' is not a known provider",
        ),
        (["--project", ""], {}, "project_id must be non-empty when supplied"),
    ],
    ids=["unknown-embedding-provider", "empty-project"],
)
def test_context_search_keeps_the_refusals_of_the_server_it_no_longer_builds(
    options: list[str], environment: dict[str, str], named: str
) -> None:
    """The context command built a `MemoryServer` only to read its defaults, and
    that constructor refused two things: an empty `--project` (its own check) and an
    embedding provider outside `PROVIDERS` (`SpaceFingerprint.current()`, reached
    through `EmbeddingClient`). Both exited 1. `context search` keeps both refusals
    and their exit code, against a real store, and names them on one line.
    """
    import sys

    from child_env import child_env

    default_store_path()
    proc = subprocess.run(
        [sys.executable, "-m", "nmemory", "context", "search", "q", *options],
        env=child_env({**os.environ, **environment}),
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert proc.returncode == 1, f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    assert proc.stdout == ""
    lines = proc.stderr.splitlines()
    assert len(lines) == 1, proc.stderr
    assert lines[0].startswith("nmemory: context search: "), lines[0]
    assert named in lines[0], lines[0]


def test_context_parses_only_its_lexical_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    """The memory lane reads `matches[:pool]`; it no longer asks for every match.

    Measured on the imported 13.6k-capsule store: a two-word query matched
    ~2.8k capsules, all parsed from JSON, to keep the first 120.
    """
    store = Store.open_disposable_test_store()
    for n in range(20):
        store.append(cap_at(f"pooled probe capsule {n}", "nott", f"pool:{n}"), NOW)
    returned: list[int] = []
    real = store.capsules.search_fts_scoped

    def spy(*args: Any, **kwargs: Any) -> Any:
        out = real(*args, **kwargs)
        returned.append(len(out))
        return out

    monkeypatch.setattr(store.capsules, "search_fts_scoped", spy)
    result = execute_context(
        store, "pooled probe", sources=["memory"], limit=1, now=NOW
    )
    assert result["results"], "the lane found nothing: the test measures nothing"
    assert returned == [12]  # pool = max(limit * 10, DEFAULT_LIMIT)


def test_context_tombstoned_in_excluded(monkeypatch: pytest.MonkeyPatch) -> None:
    store = Store.open_disposable_test_store()
    # Keyless servers refuse nmemory_capsule_purge, and the gate env has no
    # NOTT_NMEMORY_HMAC_KEY (see tests/test_forget.py for the measurement).
    server = MemoryServer(store=store, hmac_env_key=b"k" * 32)
    cid, _ = store.append(cap_at("Tombstone residual content", "nott", "tomb:1"), NOW)
    purge_tool.handle(
        server.handler_state(),
        {"id": cid, "mode": "purged", "reason": "tombstone test reason"},
        None,
        NOW,
        "test",
    )

    class FakeFingerprint:
        def as_str(self) -> str:
            return "fp-test"

    class FakeEmbeddings:
        space_provider = "gemini"

        def is_configured(self) -> bool:
            return True

        def embed_query(self, query: str) -> list[float]:
            return [0.1] * 1536

        @property
        def space_fingerprint(self) -> Any:
            return FakeFingerprint()

    monkeypatch.setattr(
        store.embeddings, "search_cosine", lambda **kwargs: [(cid, 0.95)]
    )
    monkeypatch.setattr(store.embeddings, "serving_space", lambda: "fp-test")
    server.embeddings = FakeEmbeddings()  # type: ignore[assignment]

    res = context_tool.handle(
        server.handler_state(),
        {"query": "residual", "sources": ["memory"]},
        None,
        NOW,
        "test",
    )
    assert "error" not in res, f"Expected 200 OK without JSON-RPC error, got {res}"
    payload = json.loads(res["content"][0]["text"])
    assert payload["label"] == "ADVISORY_NOT_AUTHORITY"
    assert payload["results"] == []
    assert "excluded" in payload
    assert payload["excluded"] == {"tombstoned": [cid]}


def test_codegraph_adapter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path
    (repo / "src").mkdir()
    (repo / "src" / "main.py").write_text("def run():\n    pass\n")
    seed_repo(repo)
    git(repo, ["add", "src/main.py"])
    git(repo, ["commit", "-qm", "seed repo"])

    program = repo / "fake-codegraph-native"
    fake_adapter(
        program,
        """#!/bin/sh
if [ "$1" = "status" ]; then
  printf '%s' '{"initialized":true,"version":"1.5.0","index":{"state":"complete"}}'
else
  printf '%s' '[{"node":{"name":"run","filePath":"src/main.py","startLine":1,"endLine":2}}]'
fi
""",
    )

    # 1. Direct query_adapter_program with native codegraph json structure
    status, hits = query_adapter_program("codegraph", "run", 10, repo, program)
    assert status["status"] == "unknown_staleness" or status["status"] == "ready"
    assert len(hits) == 1
    assert hits[0].provenance.address == "src/main.py"
    assert hits[0].provenance.start_line == 1
    assert hits[0].provenance.end_line == 2
    assert "def run():" in hits[0].content

    # 2. query_adapter runs when NOTT_NMEMORY_CODEGRAPH_BIN is configured
    monkeypatch.setenv("NOTT_NMEMORY_CODEGRAPH_BIN", str(program))
    _status2, hits2 = query_adapter("codegraph", "run", 10, repo)
    assert len(hits2) == 1
    assert hits2[0].provenance.address == "src/main.py"


def test_a_registered_source_with_zero_chunks_reads_as_an_empty_index(
    tmp_path: Path,
) -> None:
    """F5: absence cannot be read as presence.

    The live store carried ONE registered source — `markdown:/tmp/qa-nmemory/idx`,
    a QA directory from 2026-09-05 already deleted — and `corpus_chunks = 0`.
    `nmemory_context_show` replied `status: "unknown_staleness"` WITH an
    `indexed_at` alongside, which reads as "index exists, unknown
    freshness". No index existed, and absence spent a week being
    read as presence.

    Here the source is registered pointing to a path that no longer exists, just
    like there, and the indexed root has no markdown at all: zero occurrences.
    """
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "lib.rs").write_text("pub fn nada() {}\n")
    seed_repo(repo)
    git(repo, ["add", "src/lib.rs"])
    git(repo, ["commit", "-qm", "sem markdown nenhum"])

    store = Store.open_disposable_test_store()
    server = MemoryServer(store=store, anchor_root=str(repo))
    index_tool.handle(
        server.handler_state(),
        {"root": str(repo), "project_id": "projetodeteste"},
        None,
        NOW,
        "t",
    )

    res = context_tool.handle(
        server.handler_state(),
        {"query": "nada", "sources": ["markdown"], "limit": 5, "token_budget": 500},
        None,
        NOW,
        "t",
    )
    payload = json.loads(res["content"][0]["text"])
    markdown = next(s for s in payload["sources"] if s["source"] == "markdown")

    assert markdown["chunks"] == 0, markdown
    assert markdown["status"] == "empty_index", (
        f"a registered source with zero occurrences says {markdown['status']!r}, which"
        " a reader takes for an index that exists"
    )


def test_a_source_that_actually_holds_chunks_is_not_called_empty(
    tmp_path: Path,
) -> None:
    """The counter-case: `empty_index` cannot swallow a populated source.

    Without it, replacing the branch with an unconditional `True` would go unnoticed —
    and the response would call everything empty, which is the opposite falsehood.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("# Title\n\nBody with a whole sentence.\n")
    seed_repo(repo)
    git(repo, ["add", "README.md"])
    git(repo, ["commit", "-qm", "com markdown"])

    store = Store.open_disposable_test_store()
    server = MemoryServer(store=store, anchor_root=str(repo))
    index_tool.handle(
        server.handler_state(),
        {"root": str(repo), "project_id": "projetodeteste"},
        None,
        NOW,
        "t",
    )

    res = context_tool.handle(
        server.handler_state(),
        {"query": "frase", "sources": ["markdown"], "limit": 5, "token_budget": 500},
        None,
        NOW,
        "t",
    )
    payload = json.loads(res["content"][0]["text"])
    markdown = next(s for s in payload["sources"] if s["source"] == "markdown")

    assert markdown["chunks"] > 0, markdown
    assert markdown["status"] != "empty_index", markdown


def test_a_dead_source_does_not_shadow_the_live_one_of_the_same_kind(
    tmp_path: Path,
) -> None:
    """F5, the half that only REAL indexing revealed.

    The reproduction is literal: the LIVE repo is called `aaa` and loses in the alphabet, and
    the DEAD one has no markdown at all — exactly like the QA directory.
    """
    dead = tmp_path / "zzz-morto"
    (dead / "src").mkdir(parents=True)
    (dead / "src" / "lib.rs").write_text("pub fn nada() {}\n")
    seed_repo(dead)
    git(dead, ["add", "src/lib.rs"])
    git(dead, ["commit", "-qm", "sem markdown nenhum"])

    live = tmp_path / "aaa-vivo"
    live.mkdir()
    (live / "NOVO.md").write_text("# Novo\n\nConteudo do repositorio vivo.\n")
    seed_repo(live)
    git(live, ["add", "NOVO.md"])
    git(live, ["commit", "-qm", "repositorio vivo"])

    store = Store.open_disposable_test_store()
    dead_server = MemoryServer(store=store, anchor_root=str(dead))
    index_tool.handle(
        dead_server.handler_state(),
        {"root": str(dead), "project_id": "projetodeteste"},
        None,
        NOW,
        "t",
    )

    server = MemoryServer(store=store, anchor_root=str(live))
    index_tool.handle(
        server.handler_state(),
        {"root": str(live), "project_id": "projetodeteste"},
        None,
        NOW,
        "t",
    )

    res = context_tool.handle(
        server.handler_state(),
        {"query": "conteudo", "sources": ["markdown"], "limit": 5, "token_budget": 500},
        None,
        NOW,
        "t",
    )
    payload = json.loads(res["content"][0]["text"])
    markdown = next(s for s in payload["sources"] if s["source"] == "markdown")

    assert markdown["chunks"] > 0, (
        "the source the response shows has zero chunks: the dead record covered the"
        f" live one, and the anchor was {live}. {markdown}"
    )
    assert markdown["status"] != "empty_index", markdown


def cap_conf(
    content: str, project: str, anchor: str, conf: float, born: datetime = NOW
) -> Capsule:
    return Capsule(
        content=content,
        provenance=Provenance(
            source="test",
            anchor=anchor,
            source_hash=sha256_hex(content.encode("utf-8")),
        ),
        confidence=conf,
        freshness=Freshness(valid_from=born),
        scope=Scope(project_id=project),
        authority_class=AuthorityClass.USER_STATED,
        instruction_taint=False,
    )


def _served(store: Store, query: str, now: datetime) -> dict[str, dict[str, Any]]:
    from nmemory.context import execute_context

    ctx = execute_context(
        store, query, project_id="ranker", sources=["memory"], limit=10, now=now
    )
    return {r["id"]: r for r in ctx["results"]}


def _rrf_of(row: dict[str, Any]) -> float:
    """The RRF sum that lanes published for this capsule, reconstructed from
    `score_explanation` — so the falsifier does not depend on which rank FTS
    gave each one, only on the FACTOR that the ranker applies on top."""
    from nmemory.retrieve import RRF_K

    total = 0.0
    for entry in row["score_explanation"]:
        if "rank=" in entry:
            total += 1.0 / (RRF_K + float(entry.split("rank=")[1]))
    return total


def test_o_context_usa_o_MESMO_ranker_que_o_retrieve() -> None:
    """D7, measured 2026-09-17: of the 2613 queries in `read_traffic`, `context` is
    1126 (9793 capsules served) and `retrieve` is 15. The ranker that matters is
    `context`'s — and it was PURE RRF: `GroupedCandidate.add` summed
    `1/(RRF_K+rank)` and nothing else, while `retrieve()` multiplied by
    confidence and decay (`retrieve.py:709,756`). A capsule that the store marks
    as weak reached bootstrap tied with a strong one.

    No claim is made about which rank FTS gives each: the claim is that the published score
    is lane RRF TIMES decay, which is exactly what `retrieve` does. Confidence left
    the order on 2026-10-16 (measurement in `decay_factor`): the 0.1 capsule and the
    0.9 capsule, both never verified, keep their bare RRF."""
    url = default_store_path("ranker")
    store = Store.open(url)

    id_forte, _ = store.append(
        cap_conf("quasar alinhamento medido com cuidado", "ranker", "a.py:1", 0.9), NOW
    )
    id_fraco, _ = store.append(
        cap_conf("quasar alinhamento medido devagar", "ranker", "b.py:1", 0.1), NOW
    )

    rows = _served(store, "quasar alinhamento", NOW)
    assert id_forte in rows and id_fraco in rows, rows.keys()
    for cid in (id_forte, id_fraco):
        esperado = _rrf_of(rows[cid])  # never verified: decay is 1.0
        assert rows[cid]["score"] == pytest.approx(esperado, rel=1e-9), (
            f"{cid}: score {rows[cid]['score']} != RRF x decay {esperado}"
        )


def test_o_decaimento_do_context_conta_do_relogio_da_verificacao() -> None:
    """The decay ruler was `valid_from` — "recent write beats old".
    The honest claim is "recently verified beats long-ago verified":
    an old capsule that the anchor loop (U2) confirmed TODAY is better
    evidence than an old one nobody has verified since creation.

    Three capsules born on the SAME day, same confidence; the only difference is
    WHEN verification passed. Under the `valid_from` clock all three decay
    equally and the factor is the same — that is what this test overturns.

    The third is what makes the falsifier honest, and replaces the previous
    assertion: it asserted that a NEVER-verified capsule decays from write time,
    which is literally the old ruler ("recent write beats old")
    applied to what nobody inspected. Absence of proof is not proof of
    obsolescence: without verification the factor is neutral, and what the new ruler weighs is
    recently-verified against long-ago-verified."""
    url = default_store_path("rankerdecay")
    store = Store.open(url)

    nascimento = datetime(2025, 1, 1, tzinfo=UTC)
    agora = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
    id_verif, _ = store.append(
        cap_conf("pulsar cadencia medida", "ranker", "a.py:1", 0.8, nascimento),
        nascimento,
    )
    id_crua, _ = store.append(
        cap_conf("pulsar cadencia estimada", "ranker", "b.py:1", 0.8, nascimento),
        nascimento,
    )
    store.anchors.record_verification(
        id_verif,
        kind="anchor",
        result="pass",
        detail="a.py:1",
        actor="anchor-check",
        now=agora,
    )

    id_velha, _ = store.append(
        cap_conf("pulsar cadencia antiga", "ranker", "c.py:1", 0.8, nascimento),
        nascimento,
    )
    store.anchors.record_verification(
        id_velha,
        kind="anchor",
        result="pass",
        detail="c.py:1",
        actor="anchor-check",
        now=agora - timedelta(days=540),
    )

    rows = _served(store, "pulsar cadencia", agora)
    fator_verif = rows[id_verif]["score"] / _rrf_of(rows[id_verif])
    fator_cru = rows[id_crua]["score"] / _rrf_of(rows[id_crua])
    fator_velha = rows[id_velha]["score"] / _rrf_of(rows[id_velha])
    # Verified today: age-since-verification 0, so the factor is 1.0
    # (confidence does not enter the order; see `decay_factor`).
    assert fator_verif == pytest.approx(1.0, rel=1e-6), fator_verif
    # Verified 540 days ago, half-life 180: three half-lives, 1/8.
    assert fator_velha == pytest.approx(0.125, rel=1e-3), fator_velha
    # AND THE RULER: recently verified beats long-ago verified, by age of
    # VERIFICATION and not of write -- all three were born on the same day.
    assert fator_verif > fator_velha * 7, (fator_verif, fator_velha)
    # Never verified: neutral, not penalized for nobody having inspected it.
    assert fator_cru == pytest.approx(1.0, rel=1e-6), fator_cru


def test_uma_capsula_pinada_nao_decai_no_context() -> None:
    """Finding from independent review (`fugu-ultra`, 2026-09-17), and the most
    serious: `decay_factor` covers the pin axis (`pinned → 1.0`) and `retrieve`
    passes `store.get_pin`, but the `context` call passed NO `pinned`
    at all. The owner pins a capsule to say "this one does not decay", and the surface
    serving 1126 of 2613 queries was decaying it.

    In batch and not one query per candidate, for the same reason as the verification
    clock: the pool reaches up to 500 candidates.
    """
    url = default_store_path("pinctx")
    store = Store.open(url)
    velha = NOW - timedelta(days=720)
    pinada, _ = store.append(
        cap_conf("quasar pinado do owner", "ranker", "src/p.py:1", 1.0, velha), velha
    )
    solta, _ = store.append(
        cap_conf("quasar solto sem pin", "ranker", "src/s.py:1", 1.0, velha), velha
    )
    store.pins.set_pin(str(pinada), True, "o owner afirmou relevância", "owner", NOW)

    servidas = _served(store, "quasar", NOW)
    assert str(pinada) in servidas and str(solta) in servidas
    fator_pin = servidas[str(pinada)]["score"] / _rrf_of(servidas[str(pinada)])
    fator_solta = servidas[str(solta)]["score"] / _rrf_of(servidas[str(solta)])
    assert fator_pin == pytest.approx(1.0, abs=1e-6), (
        "the pinned capsule decayed on the mandatory surface: the pin was discarded"
    )
    assert fator_solta == pytest.approx(1.0, abs=1e-6), (
        "with no verification at all the factor is NEUTRAL, not decay since the write"
    )


def test_um_grupo_nao_mistura_a_confianca_de_uma_capsula_com_o_id_de_outra() -> None:
    """Third finding from the same review, and the falsifier is about the CLASS and not
    about the store on purpose: along the route, which of the two capsules with identical
    content arrives first depends on the order FTS returned, so a test on the store
    would pass by luck half the time and falsifies nothing.

    The group is indexed by normalized content sha256. `self.id` is
    first-wins (the first `memory` stays) but `confidence`/`born` were
    last-wins: the group emerged with the id of one capsule and the weight of another — and the
    verification clock, which is read BY `cand.id`, was that of the first.
    """
    from nmemory.context import GroupedCandidate

    grupo = GroupedCandidate()
    nascimento_a = datetime(2026, 1, 1, tzinfo=UTC)
    nascimento_b = datetime(2026, 6, 1, tzinfo=UTC)
    comum: dict[str, Any] = {
        "handle": None,
        "content": "nebulosa identica em duas capsulas",
        "provider": "memory",
        "provenance": [],
        "instruction_taint": False,
    }
    grupo.add(
        "cap-forte",
        lane="memory_lexical",
        rank=3,
        confidence=0.9,
        born=nascimento_a,
        **comum,
    )
    grupo.add(
        "cap-fraca",
        lane="memory_semantic",
        rank=7,
        confidence=0.1,
        born=nascimento_b,
        **comum,
    )
    assert grupo.id == "cap-forte", "the id remains that of the first `memory`"
    assert grupo.confidence == 0.9, (
        f"the group kept the id of `cap-forte` and the confidence {grupo.confidence} "
        "of `cap-fraca`: the served weight is decided by FTS order"
    )
    assert grupo.born == nascimento_a, (
        "`born` must come from the SAME capsule as the id, otherwise the verification "
        "clock is read from one id and the age from another"
    )

    # `absorb` has the SAME class: it took the other's id and retained its own
    # weight, and is the path through which two groups merge.
    outro = GroupedCandidate()
    outro.add(
        "cap-outra",
        lane="memory_lexical",
        rank=1,
        confidence=0.4,
        born=nascimento_b,
        **{**comum, "provider": "corpus"},
    )
    outro.absorb(grupo)
    if outro.id == "cap-forte":
        assert outro.confidence == 0.9 and outro.born == nascimento_a, (
            f"absorbed the id `cap-forte` with the confidence {outro.confidence}"
        )


def test_o_relogio_da_verificacao_nunca_anda_para_tras() -> None:
    """Second finding of the review. `DISTINCT ON … ORDER BY seq DESC` selects the
    latest APPEND, and `at` is the instant the caller declares — a replay or
    backfill that records an OLD verification after a recent one has higher
    `seq` and lower `at`. The factor dropped because proof of passing was
    ADDED: measured at 11×, from ~1.0 to ~0.09.
    """
    url = default_store_path("rankerclock")
    store = Store.open(url)
    at_noon = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
    cid, _ = store.append(
        cap_conf("clock quasar", "ranker", "r.py:1", 1.0, at_noon), at_noon
    )
    store.anchors.record_verification(
        cid, kind="anchor", result="pass", detail="r.py:1", actor="a", now=at_noon
    )
    # The backfill: same verdict, `at` from 600 days earlier, higher `seq`.
    store.anchors.record_verification(
        cid,
        kind="anchor",
        result="pass",
        detail="r.py:1",
        actor="replay",
        now=at_noon - timedelta(days=600),
    )
    relogio = store.anchors.last_passing_verification_among([str(cid)])
    assert relogio[str(cid)] == at_noon, (
        "backfilling an old verification pulled the clock backwards: "
        f"{relogio[str(cid)]}"
    )


def test_o_lexical_do_context_nao_exige_TODAS_as_palavras() -> None:
    """MEASURED 2026-09-21: `nmemory_context_show` returned ZERO in 80-92% of calls
    since Sep 18, and the cause is neither the ranker nor the corpus — it is the call site.

    `search_fts_scoped(terms)` treats each element of `terms` as an
    ALTERNATIVE (OR across groups) and requires all words WITHIN a group
    (`q.issubset(words)`, store.py:2744). `retrieve` passes `expanded_terms` and
    `search_fts_limited` passes `query_tokens` — lists of words, hence OR.
    `context` passed `[query]`: the whole query as ONE group, meaning AND
    over each of its words.

    Measured on the live origin: `assento` returned 60 candidates and
    `async-clover 35e2c final review battery current checkpoint secret proof
    installer public symlink` returned 0 — each of those words exists in
    thousands of capsules, but no single capsule has ALL of them. With the semantic
    lane up this remained hidden; when it went down, only AND remained.

    The BM25 that runs right after exists precisely to rank a
    disjunction: a capsule with all words scores far above one with
    only one. AND at the gate makes that scoring useless.
    """
    url = default_store_path("lexor")
    store = Store.open(url)

    ambas, _ = store.append(cap_conf("pulsar cadencia", "ranker", "a.py:1", 0.8), NOW)
    so_uma, _ = store.append(cap_conf("pulsar sozinho", "ranker", "b.py:1", 0.8), NOW)
    so_outra, _ = store.append(
        cap_conf("cadencia sozinha", "ranker", "c.py:1", 0.8), NOW
    )
    nenhuma, _ = store.append(cap_conf("assunto alheio", "ranker", "d.py:1", 0.8), NOW)

    rows = _served(store, "pulsar cadencia", NOW)

    # The one with both words continues to be served, and on top.
    assert ambas in rows, "the capsule with every word must always come"
    # And those with ONE of the words cease to be invisible.
    assert so_uma in rows, "AND at the door: a capsule with 'pulsar' was dropped"
    assert so_outra in rows, "AND at the door: a capsule with 'cadencia' was dropped"
    # Whichever has neither remains excluded: it is a disjunction, not the entire store.
    assert nenhuma not in rows, "with none of the words you do not get in"
    # BM25 ranks: two words are worth more than one.
    assert rows[ambas]["score"] > rows[so_uma]["score"], (
        rows[ambas]["score"],
        rows[so_uma]["score"],
    )
