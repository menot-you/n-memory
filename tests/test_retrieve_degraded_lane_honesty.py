"""Tests for degraded lane reporting and exclusion publication on grounded outcomes.

UNIT A-HONESTY:
Defect 1 — the vector lane fails with an error and the envelope responds `grounded` or `abstain`
without naming the failure (`lane_error`/`degraded`).
Defect 2 — the MCP port (`server.py`) omits `excluded` and `excluded_count` in the
`grounded_dict` dictionary, making partial exclusions unobservable by the real consumer.
"""
#: `projetodeteste` and not `nmemory`: these tests seed RAW (`Store.append`,
#: which does not canonicalize) and read through the PORT (which canonicalizes).
#: With `nmemory` in the alias fold map — owner decision, 2026-09-19 — reading
#: requested `pynott` and the seed remained in `nmemory`: 30 red tests due to FIXTURE
#: asymmetry, not the product. A real session writes through ingest and reads
#: through the port, hence folding on both sides. The name only needs to be
#: OUTSIDE `PROJECT_ALIASES`.

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

import pytest
from storefile import put_embedding

from nmemory.capsule import (
    AuthorityClass,
    Capsule,
    Freshness,
    Provenance,
    Scope,
    sha256_hex,
)
from nmemory.embedding import EmbeddingClient
from nmemory.server import MemoryServer
from nmemory.space import DIMENSION, EmbeddingError
from nmemory.store import Store

NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)
UNIT_VEC: list[float] = [1.0] + [0.0] * (DIMENSION - 1)
TERM = "sincronizacao"


def _cap(content: str, anchor: str, confidence: float = 0.9) -> Capsule:
    return Capsule(
        content=content,
        provenance=Provenance(
            source="test",
            anchor=anchor,
            source_hash=sha256_hex(content.encode("utf-8")),
        ),
        confidence=confidence,
        freshness=Freshness(valid_from=NOW),
        scope=Scope(project_id="projetodeteste"),
        authority_class=AuthorityClass.USER_STATED,
        instruction_taint=False,
    )


def _call(server: MemoryServer, args: dict[str, Any]) -> dict[str, Any]:
    res = server.call_tool("nmemory_capsule_retrieve", args, None, now=NOW)
    assert "error" not in res, res
    payload: dict[str, Any] = json.loads(res["result"]["content"][0]["text"])
    return payload


def test_red_1_vector_lane_failure_reports_degraded_and_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """RED 1: lane='auto' with EmbeddingError currently returns an envelope indistinguishable from legitimate term.

    When the embedding client raises EmbeddingError, the vector lane dies.
    The envelope must maintain outcome='grounded' (term capsules were retrieved),
    but must declare degraded=True, name the failure in lane_error, and log as WARNING.
    """
    store = Store.open_disposable_test_store()

    def failing_hook(_text: str) -> list[float]:
        raise EmbeddingError("429 credit_balance_exhausted")

    client = EmbeddingClient(custom_hook=failing_hook)

    # Ingest 2 capsules with the term
    cid1, _ = store.append(_cap(f"Primary document on {TERM} in a cluster", "e:1"), NOW)
    store.set_birth_classification(cid1, "fact", "project", NOW)
    put_embedding(
        store.db,
        cid1,
        list(UNIT_VEC),
        client.space_fingerprint.as_str(),
        sha256_hex(cid1.encode("utf-8")),
    )

    cid2, _ = store.append(
        _cap(f"Secondary document on distributed {TERM}", "e:2"), NOW
    )
    store.set_birth_classification(cid2, "fact", "project", NOW)
    put_embedding(
        store.db,
        cid2,
        list(UNIT_VEC),
        client.space_fingerprint.as_str(),
        sha256_hex(cid2.encode("utf-8")),
    )

    server = MemoryServer(
        store=store,
        embeddings=client,
        anchor_root=".",
        project_id="projetodeteste",
    )

    with caplog.at_level(logging.WARNING, logger="nmemory.retrieve"):
        p = _call(
            server,
            {
                "terms": [TERM],
                "project_id": "projetodeteste",
                "lane": "auto",
                "token_budget": 100_000,
            },
        )

    # The outcome remains grounded (lexical matches exist)
    assert p["outcome"] == "grounded", p
    assert p["lane"] == "auto", p
    assert p["lane_used"] == "term", p
    assert len(p["results"]) == 2, p

    # RED: Currently degraded and lane_error do not exist in the envelope
    assert p.get("degraded") is True, f"degraded expected True, got {p.get('degraded')}"
    assert p.get("lane_error") is not None, (
        "lane_error must name the vector lane failure"
    )
    assert "EmbeddingError" in p["lane_error"]
    assert "429 credit_balance_exhausted" in p["lane_error"]

    # RED: Currently logs as DEBUG; should log as WARNING
    warning_records = [
        rec
        for rec in caplog.records
        if rec.levelno >= logging.WARNING and "vector" in rec.message
    ]
    assert len(warning_records) > 0, "The vector lane failure must emit a WARNING log"


def test_red_2_grounded_outcome_publishes_exclusions_through_mcp_door() -> None:
    """RED 2: grounded with candidate exclusion by floor does not publish count through the door.

    When the core excludes candidates (e.g. via below_relevance_floor or min_confidence),
    but still returns evidence, the envelope through the MCP port must publish
    `excluded` and `excluded_count`, exactly as the `missing_evidence` branch already does.
    """
    store = Store.open_disposable_test_store()
    client = EmbeddingClient(custom_hook=lambda _text: list(UNIT_VEC))

    # Ingest 2 capsules: one with confidence 0.9 and another with 0.3
    cid_high, _ = store.append(
        _cap(f"Registro forte com {TERM}", "e:high", confidence=0.9), NOW
    )
    store.set_birth_classification(cid_high, "fact", "project", NOW)

    cid_low, _ = store.append(
        _cap(f"Registro fraco com {TERM}", "e:low", confidence=0.3), NOW
    )
    store.set_birth_classification(cid_low, "fact", "project", NOW)

    server = MemoryServer(
        store=store,
        embeddings=client,
        anchor_root=".",
        project_id="projetodeteste",
    )

    # Request with min_confidence=0.8 -> cid_high passes, cid_low is excluded by min_confidence
    p = _call(
        server,
        {
            "terms": [TERM],
            "project_id": "projetodeteste",
            "min_confidence": 0.8,
            "lane": "term",
        },
    )

    assert p["outcome"] == "grounded", p
    assert len(p["results"]) == 1, p
    assert p["results"][0]["id"] == cid_high

    # RED: Currently server.py:grounded_dict does not include excluded or excluded_count
    assert "excluded" in p, f"excluded ausente do envelope grounded: {p}"
    assert "excluded_count" in p, f"excluded_count ausente do envelope grounded: {p}"
    assert p["excluded_count"] == 1, p
    assert p["excluded"].get("min_confidence") == 1, p


def test_clean_auto_lane_is_not_degraded() -> None:
    """A clean 'auto' lane (healthy vector) is not degraded and fuses rankings."""
    store = Store.open_disposable_test_store()
    client = EmbeddingClient(custom_hook=lambda _text: list(UNIT_VEC))

    cid, _ = store.append(_cap(f"Registro limpo {TERM}", "e:clean"), NOW)
    store.set_birth_classification(cid, "fact", "project", NOW)
    put_embedding(
        store.db,
        cid,
        list(UNIT_VEC),
        client.space_fingerprint.as_str(),
        sha256_hex(cid.encode("utf-8")),
    )

    server = MemoryServer(
        store=store,
        embeddings=client,
        anchor_root=".",
        project_id="projetodeteste",
    )

    p = _call(
        server,
        {
            "terms": [TERM],
            "project_id": "projetodeteste",
            "lane": "auto",
        },
    )

    assert p["outcome"] == "grounded", p
    assert p["lane_used"] == "fused", p
    assert p.get("degraded") is not True, p
    assert p.get("lane_error") is None, p
    assert p.get("excluded") == {}, p
    assert p.get("excluded_count") == 0, p


def test_vector_lane_failure_on_vector_only_reports_degraded_and_error() -> None:
    """When caller requests exclusive lane='vector' and the vector lane dies:

    The outcome falls back to 'abstain', but does not lie: declares degraded=True and names lane_error.
    """
    store = Store.open_disposable_test_store()

    def failing_hook(_text: str) -> list[float]:
        raise EmbeddingError("503 service unavailable")

    client = EmbeddingClient(custom_hook=failing_hook)

    cid, _ = store.append(_cap(f"Registro com {TERM}", "e:vec"), NOW)
    store.set_birth_classification(cid, "fact", "project", NOW)
    put_embedding(
        store.db,
        cid,
        list(UNIT_VEC),
        client.space_fingerprint.as_str(),
        sha256_hex(cid.encode("utf-8")),
    )

    server = MemoryServer(
        store=store,
        embeddings=client,
        anchor_root=".",
        project_id="projetodeteste",
    )

    p = _call(
        server,
        {
            "terms": [TERM],
            "project_id": "projetodeteste",
            "lane": "vector",
        },
    )

    assert p["outcome"] == "abstain", p
    assert p["lane"] == "vector", p
    assert p["lane_used"] == "term", p
    assert p.get("degraded") is True, p
    assert p.get("lane_error") is not None, p
    assert "EmbeddingError" in p["lane_error"]
    assert "503 service unavailable" in p["lane_error"]


def test_grounded_publishes_below_relevance_floor_exclusion() -> None:
    """Grounded with relevance floor exclusion publishes below_relevance_floor through the MCP port.

    Makes assertion (d) of A2 directly observable by the port consumer.
    """
    store = Store.open_disposable_test_store()
    client = EmbeddingClient(custom_hook=lambda _text: list(UNIT_VEC))

    # Insert two capsules with repetitions of the term for different BM25
    # sc1: term appears 5 times -> high BM25
    cid1, _ = store.append(
        _cap(f"Alta relevância: {TERM} {TERM} {TERM} {TERM} {TERM}", "e:1"), NOW
    )
    store.set_birth_classification(cid1, "fact", "project", NOW)

    # sc2: long text with the term only 1 time -> low BM25
    long_noise = "other text unrelated to the query to lower the bm25 score " * 20
    cid2, _ = store.append(_cap(f"{long_noise} {TERM}", "e:2"), NOW)
    store.set_birth_classification(cid2, "fact", "project", NOW)

    server = MemoryServer(
        store=store,
        embeddings=client,
        anchor_root=".",
        project_id="projetodeteste",
    )

    # Choose an intermediate term_floor that admits cid1 and excludes cid2
    # First we read scores without a floor (term_floor=0.0)
    p0 = _call(
        server,
        {
            "terms": [TERM],
            "project_id": "projetodeteste",
            "lane": "term",
            "term_floor": 0.0,
        },
    )
    assert p0["outcome"] == "grounded"
    assert len(p0["results"]) == 2

    # Obtain approximate BM25 score using core
    res_core = store.capsules.search_fts_scoped([TERM], project_id="projetodeteste")
    scores = sorted([bm for _, bm in res_core])
    mid_floor = (scores[0] + scores[1]) / 2.0

    # Call with mid_floor through the MCP port
    p = _call(
        server,
        {
            "terms": [TERM],
            "project_id": "projetodeteste",
            "lane": "term",
            "term_floor": mid_floor,
        },
    )

    assert p["outcome"] == "grounded", p
    assert len(p["results"]) == 1, p
    assert p["results"][0]["id"] == cid1
    assert "excluded" in p, p
    assert p["excluded"].get("below_relevance_floor") == 1, p
    assert p["excluded_count"] == 1, p
