"""U6: fourteen inventory groups, each judged against changing real state."""
#: `projetodeteste` and not `nmemory`: since 2026-09-19 `nmemory` is in
#: `PROJECT_ALIASES` (owner decision) and folds to `pynott`. A fixture that
#: seeds RAW (`Store.append`) and reads through the DOOR (which canonicalizes) would cease to
#: find itself — FIXTURE asymmetry, not product: a real session
#: writes via ingest, which folds, and reads through the door, which also folds.

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from storefile import default_store_path, sample_capsule_id
from test_context import git, seed_repo
from test_retrieve_envelope_honesty import UNIT_VEC, _server

import nmemory.context as context_module
from nmemory.capsule import format_rfc3339
from nmemory.capsule_ref import CapsuleRefResolution
from nmemory.context import index_context
from nmemory.embedding import EmbeddingClient
from nmemory.embedding_index import EmbeddingWorker
from nmemory.server import MemoryServer
from nmemory.space import EmbeddingError
from nmemory.store import Store
from nmemory.tools import get as get_tool
from nmemory.tools import retrieve as retrieve_tool

NOW = datetime(2026, 9, 12, tzinfo=UTC)


def ok(response: dict[str, Any]) -> dict[str, Any]:
    assert "result" in response and not response["result"]["isError"], response
    return json.loads(response["result"]["content"][0]["text"])


@pytest.fixture
def db_server() -> MemoryServer:
    return MemoryServer(store=Store.open(default_store_path("engine-literals")))


def add(server: MemoryServer, kind: str, text: str, **extra: Any) -> str:
    result = ok(
        server.call_tool(
            "nmemory_capsule_ingest",
            {
                "items": [
                    {
                        "kind": kind,
                        "content": text,
                        "source": "session:engine-literals-test",
                        "anchor": "test_no_engine_literals.py",
                        "proof_hint": "fixture: the capture gate requires re-obtainable proof",
                        "confidence": 0.9,
                        "project_id": "nott",
                        **extra,
                    }
                ]
            },
            now=NOW,
        )
    )
    return str(result["outcomes"][0]["id"])


def relate(server: MemoryServer, kind: str, src: str, dst: str) -> None:
    ok(
        server.call_tool(
            "nmemory_relation_add", {"kind": kind, "from": src, "to": dst}, now=NOW
        )
    )


def index(root: Path, server: MemoryServer, source: str) -> dict[str, Any]:
    """`index_context` (`nmemory index build`) over `root`, the one source's entry."""
    entry: dict[str, Any] = index_context(server.store, root, None, [source], NOW)[
        "sources"
    ][0]
    return entry


CASES = (
    "outcomes",
    "relevance",
    "bm25",
    "lanes",
    "trims",
    "budget",
    "receipt",
    "purge",
    "digest_miss",
    "classification",
    "mentions",
    "semantic_failure",
    "partial_index",
    "capture_context",
)


def call(server: MemoryServer, tool: str, **args: Any) -> dict[str, Any]:
    return ok(server.call_tool(tool, args, now=NOW))


def recall(server: MemoryServer, **args: Any) -> dict[str, Any]:
    return call(
        server,
        "nmemory_capsule_retrieve",
        terms=["needle"],
        project_id="nott",
        lane="term",
        **args,
    )


@pytest.mark.parametrize("case", CASES)
def test_engine_values_follow_state(
    case: str, db_server: MemoryServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = db_server
    match case:
        case "outcomes":
            assert recall(server)["outcome"] == "abstain"
            cid = add(server, "fact", "needle observed evidence")
            assert recall(server)["outcome"] == "grounded"
            server.store.tiers.set_tier(cid, "archived", NOW)
            missing = recall(server)
            assert missing["outcome"] == "missing_evidence"
            assert missing["excluded"] == {"archived": 1}
        case "relevance":
            first = add(server, "fact", "needle primary evidence", confidence=0.9)
            assert 0 < recall(server)["results"][0]["relevance"] < 1
            second = add(server, "fact", "needle other evidence", confidence=0.4)
            rows = recall(server)["results"]
            scores = {r["id"]: r["relevance"] for r in rows}
            assert 0 < scores[second] < scores[first] < 1
            assert rows[0]["id"] == first
        case "bm25":
            first = add(server, "fact", "needle")
            before_score = recall(server)["results"][0]["bm25"]
            assert before_score > 0
            second = add(server, "fact", "needle " + "padding " * 30)
            expected = {
                c.id: score
                for c, score in server.store.capsules.search_fts_scoped(
                    ["needle"], project_id="nott"
                )
            }
            rows = recall(server)["results"]
            assert {r["id"]: r["bm25"] for r in rows} == expected
            assert expected[first] != expected[second]
            assert expected[first] != before_score
            vector = _server(1, with_vectors=True)
            row = call(
                vector,
                "nmemory_capsule_retrieve",
                terms=["absent"],
                project_id="projetodeteste",
                lane="vector",
            )["results"][0]
            assert row["bm25"] is None
        case "lanes":
            engine = _server(2, with_vectors=True)
            for lane in ("term", "vector", "fused"):
                p = call(
                    engine,
                    "nmemory_capsule_retrieve",
                    terms=["sincronizacao"],
                    project_id="projetodeteste",
                    lane=lane,
                )
                assert p["lane"] == lane and p["lane_used"] == lane
                assert p["lanes_run"] == (
                    ["term", "vector"] if lane == "fused" else [lane]
                )
        case "trims":
            for i in range(3):
                add(server, "fact", f"needle {i} " + "padding " * 20)
            full = recall(server, limit=3, token_budget=10000)
            limited = recall(server, limit=1, token_budget=10000)
            budgeted = recall(server, limit=3, token_budget=1)
            assert (full["matched"], full["returned"], full["trimmed"]) == (3, 3, 0)
            assert (
                limited["trimmed_by_limit"],
                limited["trimmed_by_budget"],
                limited["trimmed"],
            ) == (2, 0, 2)
            assert (
                budgeted["trimmed_by_limit"],
                budgeted["trimmed_by_budget"],
                budgeted["trimmed"],
            ) == (0, 2, 2)
        case "budget":
            add(server, "fact", "needle evidence")
            assert recall(server)["token_budget"] == 1500
            assert recall(server, token_budget=8888)["token_budget"] == 8888
        case "receipt":
            add(server, "fact", "needle evidence")
            first, second = recall(server), recall(server)
            assert first["receipt_id"] != second["receipt_id"]
            for result in (first, second):
                # The persisted receipt, read where tools/nmemory-recall-replay.py
                # reads it: the returned_ids column, comma-joined by record_receipt.
                with server.store.db.connection() as conn, conn.cursor() as cur:
                    cur.execute(
                        "SELECT returned_ids FROM recall_receipts WHERE id = ?",
                        (result["receipt_id"],),
                    )
                    row = cur.fetchone()
                assert row is not None and row[0].split(",") == [
                    result["results"][0]["id"]
                ]
        case "purge":
            server.hmac_env_key = b"test-only-key"
            standalone = add(server, "fact", "standalone record")
            linked = add(server, "fact", "linked record")
            target = add(server, "fact", "relation target")
            relate(server, "about", linked, target)
            live = call(server, "nmemory_capsule_show", id=linked)
            assert json.dumps(live["relations"]) == json.dumps(
                [
                    {
                        "at": format_rfc3339(NOW),
                        "from": linked,
                        "kind": "about",
                        "to": target,
                    }
                ]
            )
            assert list(live["handles"].items()) == [
                (linked, "fact-2-linked-record"),
                (target, "fact-3-relation-target"),
            ]
            empty = call(
                server,
                "nmemory_capsule_purge",
                id=standalone,
                mode="purged",
                reason="test disposal",
            )
            edged = call(
                server,
                "nmemory_capsule_purge",
                id=linked,
                mode="purged",
                reason="test disposal",
            )
            assert empty["relations"] == []
            assert "handles" not in empty
            assert json.dumps(edged["relations"]) == json.dumps(live["relations"])
            assert list(edged["handles"].items()) == [
                (linked, "fact-2"),
                (target, "fact-3-relation-target"),
            ]
            assert len(edged["relations"]) == 1
            assert (
                edged["relations"]
                == call(server, "nmemory_capsule_show", id=linked)["relations"]
            )
            assert set(edged["handles"]) == {linked, target}
        case "digest_miss":
            assert recall(server)["outcome"] == "abstain"
            cid = add(server, "fact", "needle evidence")
            server.store.tiers.set_tier(cid, "archived", NOW)
            assert recall(server)["outcome"] == "missing_evidence"
            misses = call(server, "nmemory_digest_show")["recent_failures"][
                "recall_misses"
            ]
            assert {r["outcome"] for r in misses} == {"abstain", "missing_evidence"}
        case "classification":
            cid = add(server, "fact", "needle classification")
            owner = call(
                server,
                "nmemory_kind_classify",
                capsule_id=cid,
                content="needle classification",
                kind="fact",
                origin="owner-stated",
                scope="global",
            )
            assert owner["authority_class"] == "user-stated"
            assert owner["scope"] == "global" and not owner["instruction_taint"]
            assert (
                call(server, "nmemory_capsule_show", id=cid)["classification"]["scope"]
                == "global"
            )
            external = call(
                server,
                "nmemory_kind_classify",
                content="needle classification",
                kind="fact",
                origin="external-import",
            )
            assert (
                external["authority_class"] == "externally-imported"
                and external["instruction_taint"]
            )
            session = call(
                server,
                "nmemory_kind_classify",
                capsule_id=cid,
                content="needle classification",
                kind="fact",
                origin="session-note",
            )
            assert session["scope"] == "session"
            assert (
                call(server, "nmemory_capsule_show", id=cid)["classification"]["scope"]
                == "session"
            )
            assert call(
                server,
                "nmemory_kind_classify",
                content="needle classification",
                kind="fact",
                taint_hint=True,
            )["instruction_taint"]
            refused = server.call_tool(
                "nmemory_kind_classify", {"content": ""}, now=NOW
            )
            assert refused["error"]["code"] == -32602
            assert "could not derive candidate kind" in refused["error"]["message"]
        case "mentions":
            seed_repo(tmp_path)
            (tmp_path / "refs.md").write_text(
                "Missing " + sample_capsule_id(9000) + "\n"
            )
            git(tmp_path, ["add", "refs.md"])
            server.anchor_root = str(tmp_path)
            add(server, "fact", ("Other capsule mentions " + sample_capsule_id(9000)))
            found = call(server, "nmemory_capsule_show", id=sample_capsule_id(9000))[
                "mentions"
            ]
            assert (
                found["git"]["status"] == "ready" and len(found["git"]["matches"]) == 1
            )
            assert (
                found["store"]["status"] == "ready"
                and len(found["store"]["matches"]) == 1
            )
            server.anchor_root = str(tmp_path / "absent")
            assert (
                call(server, "nmemory_capsule_show", id=sample_capsule_id(9000))[
                    "mentions"
                ]["git"]["status"]
                == "unavailable"
            )

            broken_git = tmp_path / "git-error"
            broken_git.write_text("#!/bin/sh\nexit 2\n")
            broken_git.chmod(0o700)
            monkeypatch.setattr(get_tool.shutil, "which", lambda name: str(broken_git))
            assert (
                call(server, "nmemory_capsule_show", id=sample_capsule_id(9000))[
                    "mentions"
                ]["git"]["status"]
                == "error"
            )

            def timeout(*args: Any, **kwargs: Any) -> Any:
                raise subprocess.TimeoutExpired("git", 2)

            monkeypatch.setattr(get_tool.subprocess, "run", timeout)
            assert (
                call(server, "nmemory_capsule_show", ids=[sample_capsule_id(9000)])[
                    "results"
                ][0]["mentions"]["git"]["status"]
                == "timed_out"
            )
        case "semantic_failure":
            add(server, "fact", "needle evidence")
            client = EmbeddingClient(custom_hook=lambda text: list(UNIT_VEC))
            server.embeddings = client
            # Recall searches a space only once its index is whole: the drain.
            EmbeddingWorker(server.store, client).drain()
            assert (
                call(
                    server,
                    "nmemory_context_show",
                    query="needle",
                    project_id="nott",
                    sources=["memory"],
                )["semantic"]["status"]
                == "ready"
            )

            def broken(text: str) -> list[float]:
                raise EmbeddingError("provider rate limited")

            server.embeddings = EmbeddingClient(custom_hook=broken)
            result = call(
                server,
                "nmemory_context_show",
                query="needle",
                project_id="nott",
                sources=["memory"],
            )
            assert result["semantic"]["status"] == "unavailable_lexical_only"
            assert "rate limited" in result["semantic"]["detail"]
            assert result["results"]
        case "partial_index":
            seed_repo(tmp_path)
            (tmp_path / "a.md").write_text("First block\n\nSecond block\n")
            git(tmp_path, ["add", "a.md"])
            git(tmp_path, ["commit", "-qm", "initial"])
            ready = index(tmp_path, server, "markdown")
            assert ready["status"] == "ready" and ready["chunks"] > 0
            monkeypatch.setattr(context_module, "MAX_CHUNKS", 1)
            (tmp_path / "b.md").write_text("Additional block\n")
            git(tmp_path, ["add", "b.md"])
            partial = index(tmp_path, server, "markdown")
            assert partial["status"] == "unavailable"
            assert partial["chunks"] == 1 and partial["occurrences"] == 1
            assert partial["revision"] != "unknown"
            assert partial["partial"] is True
            assert server.store.corpus.corpus_chunk_count() == ready["chunks"]
            monkeypatch.setattr(context_module, "MAX_FILE_BYTES", 1)
            (tmp_path / "b.md").unlink()
            skipped = index(tmp_path, server, "markdown")
            assert skipped["status"] == "unavailable" and skipped["skipped"] == 1
            assert skipped["chunks"] == 0 and skipped["occurrences"] == 0
            unknown = index(tmp_path / "missing-root", server, "git")
            assert unknown["revision"] is None and unknown["partial"] is False

        case "capture_context":
            cid = add(server, "fact", "needle captured evidence")
            original = call(server, "nmemory_capsule_show", id=cid)
            assert original["capture_context"] is not None
            assert (
                recall(server)["results"][0]["capture_context"]
                == original["capture_context"]
            )
            # A pre-sidecar capsule is legal persisted state; absence must stay absent.
            with server.store.db.tool_boundary():
                server.store.db.begin_write()
                with server.store.db.writing() as conn, conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM capture_context WHERE capsule_id=?",
                        (cid,),
                    )
            missing = call(server, "nmemory_capsule_show", id=cid)
            recalled = recall(server)["results"][0]
            assert (
                missing["capture_context"] is None
                and recalled["capture_context"] is None
            )
            assert recalled["provenance"] == original["capsule"]["provenance"]
        case _:
            raise AssertionError(case)


@pytest.mark.parametrize(
    "suffix,symlink", [(".md", True), (".rs", True), (".rs", False)]
)
def test_index_reports_skipped_files_by_source(
    db_server: MemoryServer,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    suffix: str,
    symlink: bool,
) -> None:
    seed_repo(tmp_path)
    document = tmp_path / f"document{suffix}"
    document.write_text(
        "Small prose block\n" if suffix == ".md" else "/// Small docs\nfn good() {}\n"
    )
    git(tmp_path, ["add", "."])
    git(tmp_path, ["commit", "-qm", "valid source"])
    source = "markdown" if suffix == ".md" else "rustdoc"
    before = index(tmp_path, db_server, source)
    assert before["status"] == "ready" and before["chunks"] == 1
    assert before["skipped"] == 0
    if symlink:
        target = tmp_path / "target.txt"
        target.write_text(document.read_text())
        document.unlink()
        document.symlink_to(target.name)
        git(tmp_path, ["add", document.name])
    else:
        monkeypatch.setattr(context_module, "MAX_FILE_BYTES", 1)
    after = index(tmp_path, db_server, source)
    assert after["status"] == "ready" and after["chunks"] == 0
    assert after["skipped"] == 1


def test_get_refuses_an_unknown_resolver_status(
    db_server: MemoryServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cid = add(db_server, "fact", "needle reference control")
    assert call(db_server, "nmemory_capsule_show", id=cid)["id"] == cid
    monkeypatch.setattr(
        db_server.store.capsules,
        "resolve_capsule_ref",
        lambda ref: CapsuleRefResolution("unexpected"),
    )
    result = db_server.call_tool("nmemory_capsule_show", {"id": cid}, now=NOW)
    assert result["result"]["isError"] is True
    assert (
        "unexpected capsule reference status" in result["result"]["content"][0]["text"]
    )


def test_recall_refuses_evidence_that_disappears_before_rendering(
    db_server: MemoryServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cid = add(db_server, "fact", "needle recall control")
    assert recall(db_server)["results"][0]["id"] == cid
    original = retrieve_tool.retrieve

    def disappears(*args: Any, **kwargs: Any) -> dict[str, Any]:
        result = original(*args, **kwargs)
        assert result["evidence"], "the fault must follow actual evidence selection"
        monkeypatch.setattr(db_server.store.capsules, "get", lambda ref: None)
        return result

    monkeypatch.setattr(retrieve_tool, "retrieve", disappears)
    result = db_server.call_tool(
        "nmemory_capsule_retrieve",
        {"terms": ["needle"], "project_id": "nott", "lane": "term"},
        now=NOW,
    )
    assert result["result"]["isError"] is True
    assert "disappeared during recall" in result["result"]["content"][0]["text"]
