"""Falsifiable mutation tests verifying core invariants:
1. RRF dual-lane retrieval ranking & grounding.
2. Validating constructors reject illegal state (empty provenance).
3. Fail-closed opening refuses a missing store (no silent creation).
"""
#: `projetodeteste` and not `nmemory`: since 2026-09-19 `nmemory` is in
#: `PROJECT_ALIASES` (owner decision) and folds to `pynott`. A fixture that
#: seeds RAW (`Store.append`) and reads through the DOOR (which canonicalizes)
#: would fail to find itself — fixture asymmetry, not product: a real session
#: writes via ingest, which folds, and reads through the door, which also folds.

import argparse
import json

import pytest
from nott_database import DatabaseError
from storefile import default_store_path, put_embedding

from nmemory.capsule import CapsuleError, Provenance
from nmemory.commands.common import open_store_from_args
from nmemory.database import Database, store_path
from nmemory.embedding_index import EmbeddingWorker
from nmemory.repositories.embeddings import EmbeddingRepository, SupersededSpace
from nmemory.retrieve import RetrieveParams, retrieve
from nmemory.server import MemoryServer
from nmemory.store import Store


def test_rrf_retrieval_grounded() -> None:
    test_url = default_store_path()
    store = Store.open(test_url)
    server = MemoryServer(store=store)
    server.call_tool(
        "nmemory_capsule_ingest",
        {
            "items": [
                {
                    "content": "Postgres upgrade to version sixteen is completed.",
                    "source": "session:test",
                    "anchor": "PLAN.md:10",
                    "confidence": 0.95,
                    "project_id": "projetodeteste",
                    "kind": "fact",
                },
                {
                    "content": "We use Postgres for persistent storage.",
                    "source": "session:test",
                    "anchor": "PLAN.md:15",
                    "confidence": 0.95,
                    "project_id": "projetodeteste",
                    "kind": "fact",
                },
            ]
        },
    )
    res = retrieve(
        store,
        RetrieveParams(query="Postgres upgrade sixteen", project_id="projetodeteste"),
    )
    assert res["outcome"] == "grounded", (
        f"Expected outcome == 'grounded', got {res['outcome']}"
    )
    assert len(res["evidence"]) >= 1
    assert res["evidence"][0]["id"] == "cap-1", (
        f"Expected top rank cap-1, got {res['evidence'][0]['id']}"
    )
    assert "sixteen" in res["evidence"][0]["content"]


def test_validating_constructor_rejects_empty() -> None:
    with pytest.raises(CapsuleError, match="provenance.source is empty"):
        Provenance(source="", anchor="PLAN.md:1", source_hash="abc")


def test_fail_closed_refusal_on_a_missing_store() -> None:
    """No store file: the opener refuses, typed, and never creates one."""
    args = argparse.Namespace()
    with pytest.raises(DatabaseError, match="missing"):
        open_store_from_args(args)
    assert not store_path().exists()


def test_r5_search_cosine_space_fingerprint_isolation() -> None:
    test_url = default_store_path()
    pg = Database(test_url)
    store = Store.open(test_url)
    server = MemoryServer(store=store)

    server.call_tool(
        "nmemory_capsule_ingest",
        {
            "items": [
                {
                    "content": "Space fingerprint vector isolation test capsule.",
                    "kind": "fact",
                    "source": "session:test",
                    "anchor": "PLAN.md:1",
                    "confidence": 0.95,
                    "project_id": "projetodeteste",
                }
            ]
        },
    )
    canned_vec = [0.01] * 3072
    norm = sum(x * x for x in canned_vec) ** 0.5
    canned_vec = [x / norm for x in canned_vec]
    target_space = "openai:text-embedding-3-large:3072:nmemory-retrieval-v1"
    put_embedding(
        pg,
        capsule_id="cap-1",
        vector=canned_vec,
        space=target_space,
        text_fingerprint="a" * 64,
    )

    # Search with matching space fingerprint
    matches = EmbeddingRepository(pg).search_cosine(
        query_vector=canned_vec,
        limit=10,
        project_id="projetodeteste",
        space_fingerprint=target_space,
    )
    assert len(matches) == 1
    assert matches[0][0] == "cap-1"

    # Search with mismatched space fingerprint when embeddings exist under another space (CODEX F008: typed refusal)
    mismatched_space = "openai:text-embedding-3-small:1536:v1"
    with pytest.raises(SupersededSpace, match="superseded space fingerprint"):
        EmbeddingRepository(pg).search_cosine(
            query_vector=canned_vec,
            limit=10,
            project_id="projetodeteste",
            space_fingerprint=mismatched_space,
        )


def test_search_cosine_project_prefix_is_literal_subtree() -> None:
    test_url = default_store_path()
    pg = Database(test_url)
    store = Store.open(test_url)
    server = MemoryServer(store=store)
    captured = server.call_tool(
        "nmemory_capsule_ingest",
        {
            "items": [
                {
                    "content": f"prefix vector capsule {project}",
                    "kind": "fact",
                    "source": "session:test",
                    "anchor": f"PLAN.md:{index}",
                    "project_id": project,
                }
                for index, project in enumerate(("tree", "tree/child", "other"), 1)
            ]
        },
    )
    captured_data = captured["result"]["content"][0]["text"]
    ids = [item["id"] for item in json.loads(captured_data)["outcomes"]]
    canned_vector = [0.01] * 3072
    norm = sum(value * value for value in canned_vector) ** 0.5
    canned_vector = [value / norm for value in canned_vector]
    target_space = "openai:text-embedding-3-large:3072:nmemory-retrieval-v1"
    for index, capsule_id in enumerate(ids, 1):
        put_embedding(
            pg,
            capsule_id=capsule_id,
            vector=canned_vector,
            space=target_space,
            text_fingerprint=f"{index:064x}",
        )

    matches = EmbeddingRepository(pg).search_cosine(
        query_vector=canned_vector,
        limit=10,
        project_prefix="tree",
        space_fingerprint=target_space,
    )

    assert {capsule_id for capsule_id, _ in matches} == set(ids[:2])


def test_r6_headline_of_contracts() -> None:
    from nmemory.capsule import headline_of

    # Single-line < 140
    assert headline_of("Short single line") == "Short single line"

    # Single-line == 140
    line_140 = "a" * 140
    assert headline_of(line_140) == line_140
    assert len(headline_of(line_140)) == 140

    # Single-line > 140
    line_150 = "b" * 150
    res_150 = headline_of(line_150)
    assert res_150 == ("b" * 140) + "…"
    assert len(res_150) == 141
    assert res_150.endswith("\u2026")
    assert not res_150.endswith("...")

    # Multi-line < 140 with newline (continuation marker)
    assert headline_of("First line\nSecond line") == "First line…"
    assert headline_of("First line\r\nSecond line") == "First line…"
    assert headline_of("First line\rSecond line") == "First line…"

    # Multi-line with lone trailing newline (no trailing content -> no ellipsis)
    assert headline_of("First line\n") == "First line"

    # Multi-line > 140
    multi_long = ("c" * 150) + "\nSecond line"
    assert headline_of(multi_long) == ("c" * 140) + "…"

    # Unicode characters
    uni_long = "é" * 150
    assert headline_of(uni_long) == ("é" * 140) + "…"

    # No stripping (preserves leading and internal whitespace)
    assert headline_of("   leading spaces   ") == "   leading spaces   "
    assert headline_of("   leading spaces\nsecond") == "   leading spaces…"


def test_r9_embedding_usage_emitted_only_when_vector_ran() -> None:
    from nmemory.embedding import EmbeddingClient

    test_url = default_store_path()
    store = Store.open(test_url)

    # 1. Retrieve with unconfigured embeddings (lexical mode) -> NO embedding_usage key
    unconfigured_client = EmbeddingClient(api_key="")
    res_lexical = retrieve(
        store,
        RetrieveParams(query="Postgres upgrade", project_id="projetodeteste"),
        embeddings=unconfigured_client,
    )
    assert "embedding_usage" not in res_lexical, (
        "Lexical retrieve must not emit embedding_usage"
    )

    # 2. Retrieve with configured embeddings (vector lane active) -> embedding_usage emitted
    def mock_embed(text: str) -> list[float]:
        return [0.01] * 3072

    configured_client = EmbeddingClient(custom_hook=mock_embed)
    # Recall searches a space only once its index is whole: the worker's drain.
    EmbeddingWorker(store, configured_client).drain()
    res_vector = retrieve(
        store,
        RetrieveParams(query="Postgres upgrade", project_id="projetodeteste"),
        embeddings=configured_client,
    )
    assert "embedding_usage" in res_vector, "Vector retrieve must emit embedding_usage"
    assert res_vector["embedding_usage"]["openai_requests"] == 1
    assert res_vector["embedding_usage"]["openai_prompt_tokens"] == 2


if __name__ == "__main__":
    print("Running baseline mutation tests...")
    test_rrf_retrieval_grounded()
    print("✓ test_rrf_retrieval_grounded passed")
    test_validating_constructor_rejects_empty()
    print("✓ test_validating_constructor_rejects_empty passed")
    test_r5_search_cosine_space_fingerprint_isolation()
    print("✓ test_r5_search_cosine_space_fingerprint_isolation passed")
    test_r6_headline_of_contracts()
    print("✓ test_r6_headline_of_contracts passed")
    test_r9_embedding_usage_emitted_only_when_vector_ran()
    print("✓ test_r9_embedding_usage_emitted_only_when_vector_ran passed")
    print("All baseline tests GREEN.")
