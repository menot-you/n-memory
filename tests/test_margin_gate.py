"""The margin gate: FLAT evidence does not ground.

Measured in pra2: the top1−top2 BM25 margin separates unanswerable
probes from answerable ones (AUC 0.908; in the single shared stratum, 0.983). The
ABSOLUTE 1-D floor separates nothing — what separates is the DISTANCE
between the first and second matches of the query itself.

`min_term_margin` is the gate: when the top1−top2 margin falls BELOW τ,
evidence is flat and NO candidate from the term lane grounds — all are
counted as `below_relevance_floor` and the honest outcome is
`missing_evidence` (there was a match; none deserves to ground). `None` (default)
is OFF and preserves today's behavior byte-for-byte.

The SHAPE is refusal, not `max(term_floor, top1 − τ)`: a floor derived from
top-1 itself is always cleared by top-1 — a TIE (margin 0) would pass the
entire gate, which is exactly the case the gate exists to catch.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from nmemory.capsule import (
    AuthorityClass,
    Capsule,
    Freshness,
    Provenance,
    Scope,
    sha256_hex,
)
from nmemory.retrieve import RetrieveParams, retrieve
from nmemory.server import MemoryServer
from nmemory.store import Store

NOW = datetime.now(UTC)
MARKER = "margemmarcadorax"
TAU = 0.5


def cap_at(content: str) -> Capsule:
    return Capsule(
        content=content,
        provenance=Provenance(
            source="test",
            anchor="test_margin_gate.py:1",
            source_hash=sha256_hex(content.encode("utf-8")),
        ),
        confidence=0.9,
        freshness=Freshness(valid_from=NOW),
        scope=Scope(project_id="nott"),
        authority_class=AuthorityClass.USER_STATED,
        instruction_taint=False,
    )


def filler(i: int) -> str:
    # Without the marker: exists only to lower the marker's IDF to near zero.
    return f"enchimento {i} de rotina sem valor discriminante"


def flat_store() -> Store:
    """Two NEAR-TIE matches: margin ≈0, the case the gate exists to catch.

    Ingest is idempotent by content-hash — two BYTE-identical capsules
    collapse into one and the lane is left with a single match. The second weak match differs by one
    token to yield TWO capsules with near-equal scores (margin ≪ TAU).
    """
    store = Store.open_disposable_test_store()
    store.append(cap_at(" ".join(["enchimento"] * 30) + f" {MARKER}"), NOW)
    store.append(cap_at(" ".join(["enchimento"] * 30) + f" variadade {MARKER}"), NOW)
    for i in range(12):
        store.append(cap_at(filler(i)), NOW)
    return store


def dominant_store() -> Store:
    """A DOMINANT match: margin ~2.0 over the two weak ones, above TAU."""
    store = Store.open_disposable_test_store()
    store.append(cap_at(f"{MARKER} {MARKER} {MARKER}"), NOW)
    weak = " ".join(["enchimento"] * 30) + f" {MARKER}"
    store.append(cap_at(weak), NOW)
    store.append(cap_at(weak), NOW)
    for i in range(12):
        store.append(cap_at(filler(i)), NOW)
    return store


def run(store: Store, **params: Any) -> dict[str, Any]:
    return retrieve(store, RetrieveParams(query=MARKER, terms=[MARKER], **params))


def test_flat_match_refuses_to_ground_under_the_margin_gate() -> None:
    res = run(flat_store(), min_term_margin=TAU)
    assert res["outcome"] == "missing_evidence", res
    assert res["exclusions"].get("below_relevance_floor", 0) >= 1, res


def test_dominant_match_still_grounds_under_the_same_gate() -> None:
    res = run(dominant_store(), min_term_margin=TAU)
    assert res["outcome"] == "grounded", res
    assert res["evidence"], res


def test_default_off_preserves_todays_behavior_on_flat_evidence() -> None:
    res = run(flat_store())
    assert res["outcome"] == "grounded", res


def door(server: MemoryServer, args: dict[str, Any]) -> dict[str, Any]:
    # `call_tool` is the wire: this is where the allowlist rejects `unknown field` — the
    # internal handler swallows unknown args, and an internal test would prove
    # acceptance where the door refuses (precedent: test_retrieve_vector_k_ordering).
    return server.call_tool(
        "nmemory_capsule_retrieve", {"project_id": "nott", **args}, None, now=NOW
    )


def payload(res: dict[str, Any]) -> dict[str, Any]:
    assert "error" not in res, res
    assert res["result"]["isError"] is False, res
    return json.loads(res["result"]["content"][0]["text"])


def test_the_wire_accepts_min_term_margin_and_drives_the_gate() -> None:
    res = door(
        MemoryServer(store=dominant_store()),
        {"terms": [MARKER], "min_term_margin": TAU},
    )
    assert payload(res)["outcome"] == "grounded", res

    res = door(
        MemoryServer(store=flat_store()), {"terms": [MARKER], "min_term_margin": TAU}
    )
    assert payload(res)["outcome"] == "missing_evidence", res


def test_the_wire_refuses_a_negative_margin_with_a_typed_error() -> None:
    res = door(
        MemoryServer(store=flat_store()), {"terms": [MARKER], "min_term_margin": -1.0}
    )
    assert res["error"]["code"] == -32602, res
    assert "min_term_margin must be >= 0.0" in res["error"]["message"], res
