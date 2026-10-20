"""The DOOR of the relevance floor: without it the A1 mechanism is unreachable.

A1 (`7f5fe9f0`) puts `term_floor` and `vector_floor` into `RetrieveParams` and
counts `below_relevance_floor`. What was left out was the MCP door, and a parameter
that the door does not know does not exist for any caller. Measured at the base of this unit:

    nmemory_capsule_retrieve {"terms":["the"],"term_floor":1.0}
    -> isError: true
       "failed to deserialize parameters: unknown field `term_floor`, expected
        one of `terms`, `project_id`, ..., `min_confidence`, `vector_k`, ..."

The FALSIFIER is twofold, and the second half is worse: the daemon continuing to
refuse the field, OR accepting it and IGNORING IT — returning `grounded` on the same
probe that the floor ordered cut, which is lying silently. That is why each arm
has its own measured cutoff: neutralizing `term_floor=` OR `vector_floor=` in the
call to `RetrieveParams` turns red exactly one test in this file.

THE SCALES ARE TWO, and door validation follows each instead of inventing a
common domain:

- `term_floor` reads BM25 (`store.search_fts_scoped`), a sum of positive terms
  (`store.py`, `inv_freq * freq * 2.2 / denominator` with `inv_freq > 0`): the
  domain is `(0, +inf)` and a NEGATIVE floor is scale confusion, never an OFF
  the caller meant to write — `0.0`, the default, is already OFF;
- `vector_floor` reads `1 - (embedding <=> query)` (`postgres.py search_cosine`) and
  pgvector's `<=>` is cosine distance in `[0, 2]`: the domain is `[-1, 1]`,
  negative is LEGAL (and OFF, per `clears_relevance_floor`) and what no score
  reaches is `> 1.0`.

A floor above the achievable maximum silently empties the lane; that is why the
refusal is typed (`-32602`, the VALUE channel of `test_retrieve_trust_boundary`) and
never coercion.

MEASUREMENT fixing values used here: with four capsules that all contain the
word `the`, `search_fts_scoped(terms=["the"])` returns `0.16556652460515572`
for all four (reproduced on this box, 2026-09-03). `TERM_FLOOR_ABOVE = 1.0`
is above; `TERM_FLOOR_BELOW = 0.1` is below. Both sides are precise: a
floor is a THRESHOLD, not a switch.
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
from typing import Any

from storefile import fresh_store_path, put_embedding

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
from nmemory.space import DIMENSION
from nmemory.store import Store

NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)

# The `_abs` probe: a word matching EVERYTHING, thus with the weakest BM25 the
# term lane can produce. No capsule is about it.
STOPWORD = "the"
SEEDED = 4
TERM_FLOOR_ABOVE = 1.0
TERM_FLOOR_BELOW = 0.1

# ORTHOGONAL query and capsule: `1 - (v <=> v)` gives 0.0 exactly, so a floor
# of 0.5 cuts and the default does not.
QUERY_VEC: list[float] = [1.0] + [0.0] * (DIMENSION - 1)
ORTHOGONAL_VEC: list[float] = [0.0, 1.0] + [0.0] * (DIMENSION - 2)
VECTOR_FLOOR_ABOVE = 0.5


def _capsule(content: str, anchor: str) -> Capsule:
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


def _fresh_store(prefix: str) -> Store:
    url = fresh_store_path(prefix)
    return Store.open(url)


def _term_server() -> MemoryServer:
    """`SEEDED` capsules that only the stopword reaches. No embeddings: term lane."""
    store = _fresh_store("rfltd")
    for i in range(SEEDED):
        content = f"The bench note about the meter number {i} in the log."
        cid, _ = store.append(_capsule(content, f"t:{i}"), NOW)
        store.set_birth_classification(cid, "fact", "project", NOW)
    return MemoryServer(
        store=store, embeddings=None, anchor_root=".", project_id="projetodeteste"
    )


def _vector_server() -> MemoryServer:
    """A capsule with embedding ORTHOGONAL to the query: cosine exactly 0.0."""
    store = _fresh_store("rflvd")
    client = EmbeddingClient(custom_hook=lambda text: list(QUERY_VEC))
    content = "Vector neighbour with no word in common with the probe."
    cid, _ = store.append(_capsule(content, "v:0"), NOW)
    store.set_birth_classification(cid, "fact", "project", NOW)
    assert store.db is not None
    put_embedding(
        store.db,
        cid,
        list(ORTHOGONAL_VEC),
        client.space_fingerprint.as_str(),
        sha256_hex(cid.encode("utf-8")),
    )
    return MemoryServer(
        store=store, embeddings=client, anchor_root=".", project_id="projetodeteste"
    )


def _text(res: dict[str, Any]) -> str:
    """The served text, or a failure that NAMES the channel on which the response arrived."""
    assert "result" in res, f"resposta pelo canal JSON-RPC: {json.dumps(res)[:300]}"
    assert res["result"]["isError"] is False, (
        f"isError: {json.dumps(res['result'])[:300]}"
    )
    served: str = res["result"]["content"][0]["text"]
    return served


def _payload(res: dict[str, Any]) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(_text(res))
    return loaded


def _rpc_code(res: dict[str, Any]) -> int:
    assert "error" in res, (
        f"esperada recusa JSON-RPC e veio in-band: {json.dumps(res)[:300]}"
    )
    code: int = res["error"]["code"]
    return code


def test_term_floor_above_the_stopword_score_serves_missing_evidence() -> None:
    """RED at base: `unknown field \\`term_floor\\``, isError true, nothing excluded.

    The `missing_evidence` envelope calls `excluded` what the engine calls
    `exclusions` (`server.py`, `order_exclusions(res["exclusions"])`), and
    serves no `results` — the empty evidence of the DoD is the ABSENCE of the key,
    not an empty list. What is asserted is what the door serves, not what the engine holds.
    """
    server = _term_server()
    res = server.call_tool(
        "nmemory_capsule_retrieve",
        {
            "terms": [STOPWORD],
            "project_id": "projetodeteste",
            "lane": "term",
            "term_floor": TERM_FLOOR_ABOVE,
        },
        None,
        now=NOW,
    )
    payload = _payload(res)
    assert payload["outcome"] == "missing_evidence", payload
    assert payload["excluded"]["below_relevance_floor"] == SEEDED, payload
    assert "results" not in payload, payload


def test_term_floor_below_the_stopword_score_still_grounds() -> None:
    """The floor is a THRESHOLD and not a switch: `0.1 < 0.1655...` retains all four.

    The first assertion is what keeps both halves honest: without it, the two
    floor values would be numbers chosen by eye and one might have fallen
    on the same side of the score. The MEASURED score is framed rather than compared by
    float bytes — what matters is which side of each floor it falls on.
    """
    server = _term_server()
    scores = [
        score
        for _, score in server.store.capsules.search_fts_scoped(
            terms=[STOPWORD],
            project_id="projetodeteste",
            project_prefix=None,
            session_id=None,
            effort_set=None,
            topic_set=None,
            limit=50,
        )
    ]
    assert len(scores) == SEEDED, scores
    assert all(TERM_FLOOR_BELOW < s < TERM_FLOOR_ABOVE for s in scores), scores

    payload = _payload(
        server.call_tool(
            "nmemory_capsule_retrieve",
            {
                "terms": [STOPWORD],
                "project_id": "projetodeteste",
                "lane": "term",
                "term_floor": TERM_FLOOR_BELOW,
            },
            None,
            now=NOW,
        )
    )
    assert payload["outcome"] == "grounded", payload
    assert len(payload["results"]) == SEEDED, payload


def test_vector_floor_above_the_cosine_serves_missing_evidence() -> None:
    """The vector arm has its own cutoff, and that is what catches `IGNORING IT`.

    Without this test, dropping `vector_floor=` in the call to `RetrieveParams` leaves
    the door accepting a field that does nothing — the worse of the two falsifiers,
    because it returns `grounded` without any signal.
    """
    server = _vector_server()
    payload = _payload(
        server.call_tool(
            "nmemory_capsule_retrieve",
            {
                "terms": ["sonda"],
                "project_id": "projetodeteste",
                "lane": "vector",
                "vector_floor": VECTOR_FLOOR_ABOVE,
            },
            None,
            now=NOW,
        )
    )
    assert payload["outcome"] == "missing_evidence", payload
    assert payload["excluded"]["below_relevance_floor"] == 1, payload
    assert "results" not in payload, payload


def test_vector_floor_absent_still_grounds_the_orthogonal_capsule() -> None:
    """The control half of the vector arm: without a floor, cosine 0.0 is served."""
    server = _vector_server()
    payload = _payload(
        server.call_tool(
            "nmemory_capsule_retrieve",
            {"terms": ["sonda"], "project_id": "projetodeteste", "lane": "vector"},
            None,
            now=NOW,
        )
    )
    assert payload["outcome"] == "grounded", payload
    assert len(payload["results"]) == 1, payload


# --------------------------------------------------------------------------
# The second half of the DoD — ABSENT fields serve today's envelope
# --------------------------------------------------------------------------


def test_absent_floors_and_explicit_zero_serve_the_same_bytes() -> None:
    """`0.0` is an EXACT no-op, and the proof is IN-RUN and not a committed fixture.

    A served envelope carries `capture_context` with `host`, `owner`, and `cwd`
    (`server.py`): fixing bytes in a file would tie the test to this machine. What
    is compared are three calls in the SAME run, with fixed `now` for
    deterministic decay.

    The first comparison is the CONTROL, and it ALREADY CAUGHT a non-determinism:
    two calls against the SAME server differ in `"id": "rcpt-1"` versus
    `"rcpt-2"` — `retrieve` records a receipt per call and its id is served.
    That is why each replica has its own fresh store: it is the only way for "the same
    bytes" to be a claim about the floor and not about the receipt counter.
    Without the control, that difference would have been read as the floor's fault.
    """
    base_args: dict[str, Any] = {
        "terms": [STOPWORD],
        "project_id": "projetodeteste",
        "lane": "term",
    }

    def served(args: dict[str, Any]) -> str:
        return _text(
            _term_server().call_tool("nmemory_capsule_retrieve", args, None, now=NOW)
        )

    absent_once = served(base_args)
    absent_twice = served(base_args)
    assert absent_once == absent_twice, "envelope not deterministic without any floor"

    explicit_zero = served({**base_args, "term_floor": 0.0, "vector_floor": 0.0})
    assert explicit_zero == absent_once


# --------------------------------------------------------------------------
# The TYPED refusal — each scale has its own domain
# --------------------------------------------------------------------------


def test_term_floor_below_the_bm25_domain_is_a_typed_refusal() -> None:
    """BM25 is positive by construction: a negative floor is scale confusion."""
    server = _term_server()
    res = server.call_tool(
        "nmemory_capsule_retrieve",
        {"terms": [STOPWORD], "project_id": "projetodeteste", "term_floor": -0.5},
        None,
        now=NOW,
    )
    assert _rpc_code(res) == -32602
    assert "term_floor" in res["error"]["message"], res


def test_vector_floor_above_the_cosine_domain_is_a_typed_refusal() -> None:
    """No cosine exceeds `1.0`: a floor above that silently empties the lane."""
    server = _term_server()
    res = server.call_tool(
        "nmemory_capsule_retrieve",
        {"terms": [STOPWORD], "project_id": "projetodeteste", "vector_floor": 1.5},
        None,
        now=NOW,
    )
    assert _rpc_code(res) == -32602
    assert "vector_floor" in res["error"]["message"], res


def test_vector_floor_negative_is_admitted_because_the_cosine_reaches_it() -> None:
    """Cosine lives in `[-1, 1]`: `-1.0` is a LEGAL floor, and OFF, never refuses.

    This is the test preventing the refusal of negative `term_floor` from being copied
    to the other scale without thinking — the measurement in A1 (an opposite embedding reads
    exactly `-1.0`) is what makes it load-bearing.
    """
    server = _term_server()
    payload = _payload(
        server.call_tool(
            "nmemory_capsule_retrieve",
            {
                "terms": [STOPWORD],
                "project_id": "projetodeteste",
                "lane": "term",
                "vector_floor": -1.0,
            },
            None,
            now=NOW,
        )
    )
    assert payload["outcome"] == "grounded", payload
    assert len(payload["results"]) == SEEDED, payload
