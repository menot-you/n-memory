"""U0g — `vector_k` limits the vector lane.

The RED that this file carries is from the `nmemory-instrument-hardening` campaign:
PR #174 passed `vector_k` through `TOOL_SPECS`, converted it in
`server.py:2716` and passed it to `RetrieveParams` in `server.py:2901`, and then
NEVER read it. The only ceiling on the vector lane was the literal `limit=50` in
`retrieve.py`, so the caller declared a cutoff that the engine ignored — the
exact defect F1 the campaign existed to fix, reintroduced for one of the
eleven parameters.

The proof is of COUNT and not identity: the capsules share the same unit
vector, so cosine ties and their order comes from pgvector. What
`vector_k` promises is not WHICH return, but HOW MANY feed the ranking.
"""
#: `projetodeteste` and not `nmemory`: these tests seed RAW (`Store.append`,
#: which does not canonicalize) and read through the DOOR (which canonicalizes).
#: With `nmemory` in the fold map — owner decision, 2026-09-19 — reading asked
#: for `pynott` and the seed remained in `nmemory`: 30 red tests due to FIXTURE
#: asymmetry, not product. A real session writes via ingest and reads through
#: the door, so folds on both sides. The name only needs to be OUTSIDE `PROJECT_ALIASES`.

from __future__ import annotations

import json
from datetime import UTC, datetime

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
from nmemory.retrieve import RetrieveParams, retrieve
from nmemory.space import DIMENSION
from nmemory.store import Store

NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)
UNIT_VEC: list[float] = [1.0] + [0.0] * (DIMENSION - 1)

# More than the schema default of 10, so that a `vector_k` smaller than the
# default and a `vector_k` larger than it are both observable.
SEEDED = 14


def _cap(content: str, anchor: str) -> Capsule:
    return Capsule(
        content=content,
        provenance=Provenance(
            source="test",
            anchor=anchor,
            source_hash=sha256_hex(content.encode("utf-8")),
        ),
        confidence=0.9,
        freshness=Freshness(valid_from=NOW),
        scope=Scope(project_id="projetodeteste"),
        authority_class=AuthorityClass.USER_STATED,
        instruction_taint=False,
    )


def _seeded_store() -> tuple[Store, EmbeddingClient]:
    """`SEEDED` capsules, all with embeddings, none reachable by term."""
    store = Store.open_disposable_test_store()
    client = EmbeddingClient(custom_hook=lambda text: list(UNIT_VEC))
    for i in range(SEEDED):
        cid, _ = store.append(_cap(f"Vizinho vetorial numero {i}.", f"v:{i}"), NOW)
        store.set_birth_classification(cid, "fact", "project", NOW)
        put_embedding(
            store.db,
            cid,
            list(UNIT_VEC),
            client.space_fingerprint.as_str(),
            sha256_hex(cid.encode("utf-8")),
        )
    return store, client


def test_vector_k_caps_the_vector_lane() -> None:
    """`vector_k=3` returns THREE, with `limit` relaxed to 10.

    RED before U0g: `params.vector_k` is not read, the lane fetches 50 and the cutoff
    becomes `limit`, so 10 return.
    """
    store, client = _seeded_store()

    res = retrieve(
        store,
        RetrieveParams(
            query="zzztermoqueninguemtem",
            project_id="projetodeteste",
            lane="vector",
            limit=10,
            vector_k=3,
            token_budget=100_000,
        ),
        embeddings=client,
        now=NOW,
    )

    assert res["outcome"] == "grounded", res
    assert len(res["evidence"]) == 3, (
        f"vector_k=3 must fence the vector lane at 3; got {len(res['evidence'])}"
    )


def test_vector_k_omitted_keeps_the_schema_default_of_ten() -> None:
    """Omitted, the ceiling is 10 — what `tools_schema.json` promises to the caller.

    With `SEEDED` = 14 capsules and `limit` at 14, a missing default would
    let all 14 pass through.
    """
    store, client = _seeded_store()

    res = retrieve(
        store,
        RetrieveParams(
            query="zzztermoqueninguemtem",
            project_id="projetodeteste",
            lane="vector",
            limit=SEEDED,
            token_budget=100_000,
        ),
        embeddings=client,
        now=NOW,
    )

    assert res["outcome"] == "grounded", res
    assert len(res["evidence"]) == 10, (
        f"o default de vector_k é 10; vieram {len(res['evidence'])}"
    )


def test_vector_k_above_the_seeded_set_returns_everything() -> None:
    """A ceiling above the set cuts nothing — the fence does not invent a limit."""
    store, client = _seeded_store()

    res = retrieve(
        store,
        RetrieveParams(
            query="zzztermoqueninguemtem",
            project_id="projetodeteste",
            lane="vector",
            limit=SEEDED,
            vector_k=SEEDED + 5,
            token_budget=100_000,
        ),
        embeddings=client,
        now=NOW,
    )

    assert res["outcome"] == "grounded", res
    assert len(res["evidence"]) == SEEDED, (
        f"vector_k={SEEDED + 5} cannot cut {SEEDED} capsules; got "
        f"{len(res['evidence'])}"
    )


def test_vector_k_reaches_the_engine_through_the_mcp_door() -> None:
    """The DOOR mutant: deleting `vector_k=vector_k` from `server.py` must turn red.

    The tests above call `retrieve()` directly and prove the ENGINE. This proves the
    WIRE: without it, removing the assignment on the server lets the default 10 respond
    and no assertion notices. That was exactly the mutant that survived the
    430 tests of PR #174.
    """
    from nmemory.server import MemoryServer

    store, client = _seeded_store()
    server = MemoryServer(
        store=store, embeddings=client, anchor_root=".", project_id="projetodeteste"
    )

    res = server.call_tool(
        "nmemory_capsule_retrieve",
        {
            "terms": ["zzztermoqueninguemtem"],
            "project_id": "projetodeteste",
            "lane": "vector",
            "limit": 10,
            "vector_k": 3,
            "token_budget": 100_000,
        },
        None,
        now=NOW,
    )
    assert "error" not in res, res
    payload = json.loads(res["result"]["content"][0]["text"])
    assert payload["outcome"] == "grounded", payload
    assert len(payload["results"]) == 3, (
        f"vector_k=3 must cross the MCP door; got {len(payload['results'])}"
    )
