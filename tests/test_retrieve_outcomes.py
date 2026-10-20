"""The THREE honest outcomes reach the wire — not two and a literal.

`SERVER_INSTRUCTIONS`, first line, and the description of `nmemory_capsule_retrieve` in
`tools_schema.json`: *"recall with three honest outcomes: grounded,
missing_evidence (matched but every match excluded, per-reason counts), or
abstain (zero matches)"*.

The engine KNOWS all three (`retrieve.py` returns `missing_evidence` with `exclusions`
and `total_candidates`), and the handler only branched on `abstain` and returned the
LITERAL `"outcome": "grounded"`. A `missing_evidence` reached the wire as
`grounded` with `results: []` — the outcome that exists precisely to say
"evidence exists and NONE can ground" was indistinguishable from "found
zero useful things".

Upstream implementation (`retrieve.rs:3228 fn missing_evidence`) — `excluded_count`,
the `excluded` map by reason, and the sentence naming the count, reasons and
remaining reach avenue.
"""

from __future__ import annotations

import json
from typing import Any

from storefile import fresh_store_path

from nmemory.server import MemoryServer
from nmemory.store import Store

MARKER = "desfechomarcador"


def fresh_server() -> MemoryServer:
    url = fresh_store_path("outcomes")
    return MemoryServer(store=Store.open(url))


def ok(res: dict[str, Any]) -> dict[str, Any]:
    assert "result" in res and res["result"]["isError"] is False, res
    payload: dict[str, Any] = json.loads(res["result"]["content"][0]["text"])
    return payload


def ingest(server: MemoryServer, content: str, **extra: Any) -> str:
    payload = ok(
        server.call_tool(
            "nmemory_capsule_ingest",
            {
                "items": [
                    {
                        "content": content,
                        "kind": "fact",
                        "source": "test:outcomes",
                        "anchor": "PLAN.md:1",
                        "project_id": "nmemory",
                        **extra,
                    }
                ]
            },
        )
    )
    row = payload["outcomes"][0]
    assert row["status"] == "captured", row
    return str(row["id"])


def only_match_then_superseded(server: MemoryServer) -> str:
    """A capsule that the lexical lane MATCHES and the eligibility fence excludes."""
    cid = ingest(server, f"The only capsule with the term {MARKER} in its body.")
    ingest(server, "The replacement, with an entirely different body.", supersedes=cid)
    return cid


def retrieve(server: MemoryServer, term: str) -> dict[str, Any]:
    return ok(
        server.call_tool(
            "nmemory_capsule_retrieve", {"terms": [term], "project_id": "nmemory"}
        )
    )


def test_a_fully_excluded_match_is_missing_evidence_never_grounded() -> None:
    server = fresh_server()
    only_match_then_superseded(server)

    payload = retrieve(server, MARKER)

    assert payload["outcome"] == "missing_evidence", payload
    assert not payload.get("results"), payload


def test_missing_evidence_carries_its_per_reason_counts() -> None:
    """Without counts by reason the outcome does not explain what happened."""
    server = fresh_server()
    only_match_then_superseded(server)

    payload = retrieve(server, MARKER)

    assert payload["excluded"] == {"superseded": 1}, payload
    assert payload["excluded_count"] == 1, payload


def test_missing_evidence_names_the_reason_and_the_way_out() -> None:
    """The pin sentence: how many, why, and that the capsule remains reachable."""
    server = fresh_server()
    only_match_then_superseded(server)

    reason = retrieve(server, MARKER)["reason"]

    assert "1 capsule(s) matched" in reason, reason
    assert "1 superseded" in reason, reason
    assert "get/list" in reason, reason


def test_zero_matches_still_abstains() -> None:
    """The boundary between the two empty outcomes remains intact."""
    server = fresh_server()
    only_match_then_superseded(server)

    payload = retrieve(server, "termoquenaoexisteemladonenhum")

    assert payload["outcome"] == "abstain", payload


def test_a_live_match_still_grounds() -> None:
    """The regression that matters: the happy path does not move."""
    server = fresh_server()
    ingest(server, f"A live capsule with the term {MARKER}.")

    payload = retrieve(server, MARKER)

    assert payload["outcome"] == "grounded", payload
    assert len(payload["results"]) == 1, payload


def ingest_expired(server: MemoryServer, content: str) -> str:
    """A capsule that the lane matches and freshness fence excludes."""
    return ingest(
        server,
        content,
        valid_from="2020-01-01T00:00:00Z",
        valid_to="2020-01-02T00:00:00Z",
    )


def test_the_reason_order_is_the_pin_s_variant_order_not_alphabetical() -> None:
    """pin `retrieve.rs`: `BTreeMap<ExclusionReason, _>` walks the VARIANT
    order — quarantined, falsified, archived, superseded, expired, ... —
    which is an order of DOMINANCE, not alphabet.

    `sorted()` over the names puts `archived` before `superseded` and `expired`
    before `superseded`: partial coincidence hiding the divergence. The
    pair exposing it is `superseded` versus `expired`: alphabetically `expired`
    comes first, by variant comes later.
    """
    server = fresh_server()
    only_match_then_superseded(server)
    ingest_expired(server, f"Outra capsula com o termo {MARKER}, ja expirada.")

    reason = retrieve(server, MARKER)["reason"]

    assert "1 superseded, 1 expired" in reason, reason


def test_the_excluded_map_is_served_in_that_same_order() -> None:
    """Deterministic bytes: the map appears in the same order as the sentence."""
    server = fresh_server()
    only_match_then_superseded(server)
    ingest_expired(server, f"Outra capsula com o termo {MARKER}, ja expirada.")

    excluded = retrieve(server, MARKER)["excluded"]

    assert list(excluded) == ["superseded", "expired"], excluded


def test_the_order_is_global_not_first_capsule_wins() -> None:
    """Order belongs to the MAP, not to the capsule that triggered each reason first.

    The exclusions loop already tests reasons in pin order, but PER CAPSULE:
    a normal `dict` preserves FIRST insertion order, so a late capsule
    bringing an early reason ends up at the end. Here `expired` is ingested
    BEFORE `superseded`, inverting insertion order relative to pin order —
    and that inversion is what separates sorting from not sorting.
    """
    server = fresh_server()
    ingest_expired(server, f"The expired one with the term {MARKER}, ingested first.")
    only_match_then_superseded(server)

    payload = retrieve(server, MARKER)

    assert list(payload["excluded"]) == ["superseded", "expired"], payload
    assert "1 superseded, 1 expired" in payload["reason"], payload
