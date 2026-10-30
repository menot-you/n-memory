"""What my own fix for F1 broke, and the cap it left open.

**R1 — a capsule that the vector lane brought and the fence excluded must be
COUNTED.** When fixing the order (fence before cap), I filtered matches BEFORE
`vector_rank_map` was populated. They stopped entering `candidate_ids`,
the eligibility loop never saw them, and no exclusion counter
recorded them. The `missing_evidence` outcome collapsed into `abstain`: the caller was
informed that NOTHING matched, when it had matched and was excluded.

    server.py, the description the server itself serves:
      «three honest outcomes: grounded, missing_evidence (matched but
      excluded), or abstain (zero matches)»

And my comment — «THE SAME fence that the eligibility loop applies, a single
matcher» — was false: the same comparison with the opposite effect. The loop COUNTS the
drop, but my filter erased it.

The invariant to restore: **every id that `search_cosine` returns ends up as a candidate
or ends up as a counted exclusion.** Never evaporated.

`tools_schema.json` already stated where the cap lives, and I had read it backwards:
«the top `vector_k` **eligible** capsules by cosine feed vector-bearing
ranking». Eligible. The cap runs AFTER eligibility, not before.

**R2 — `vector_k` without a cap reaches PostgreSQL.** The schema declares
`format: uint, minimum: 0` and no maximum. `2**63` passes the type, passes my
`< 0`, and `LIMIT` raises `NumericValueOutOfRange`. The wide `except` of the lane
swallows it — and swallowing is not recovery: the connection remains in an aborted transaction,
and the next write in the SAME call raises `InFailedSqlTransaction`, which is not
in the tuple. The caller receives `-32603`, not `-32602`. Before this candidate
`vector_k` never reached SQL: the reachability is mine.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from nott_database import DatabaseError
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
from nmemory.repositories.capsules import EventTimeRange
from nmemory.retrieve import RetrieveParams, retrieve
from nmemory.server import MemoryServer
from nmemory.space import DIMENSION
from nmemory.store import Store

NOW = datetime(2026, 9, 2, 12, 0, 0, tzinfo=UTC)
OUT_OF_WINDOW = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)
WINDOW = {"from": "2024-01-01T00:00:00Z", "to": "2024-12-31T23:59:59Z"}
UNIT: list[float] = [1.0] + [0.0] * (DIMENSION - 1)


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
        scope=Scope(project_id="nmemory"),
        authority_class=AuthorityClass.USER_STATED,
        instruction_taint=False,
    )


def _store(n: int, *, dated: bool) -> tuple[Store, EmbeddingClient]:
    store = Store.open_disposable_test_store()
    client = EmbeddingClient(custom_hook=lambda text: list(UNIT))
    for i in range(n):
        cap = _cap(f"Vizinho vetorial {i}.", f"va:{i}")
        if dated:
            cid, _ = store.append_capture(
                cap, None, EventTimeRange.point(OUT_OF_WINDOW), None, NOW
            )
        else:
            cid, _ = store.append(cap, NOW)
        store.set_birth_classification(cid, "fact", "project", NOW)
        put_embedding(
            store.db,
            cid,
            list(UNIT),
            client.space_fingerprint.as_str(),
            sha256_hex(cid.encode()),
        )
    return store, client


def _vector_call(store: Store, client: EmbeddingClient, **kw: Any) -> dict[str, Any]:
    return retrieve(
        store,
        RetrieveParams(
            query="zzztermoqueninguemtem",
            project_id="nmemory",
            lane="vector",
            limit=20,
            token_budget=100_000,
            time_window=WINDOW,
            **kw,
        ),
        embeddings=client,
        now=NOW,
    )


def test_out_of_window_vector_matches_are_counted_not_erased() -> None:
    """RED: 6 capsules match and are excluded, and the outcome says `abstain`."""
    store, client = _store(6, dated=True)
    res = _vector_call(store, client)

    assert res["outcome"] == "missing_evidence", (
        f"6 capsules matched on the vector lane and were excluded by the window; "
        f"`abstain` would say none matched. got {res['outcome']} with "
        f"exclusions={res['exclusions']} and total_candidates="
        f"{res['total_candidates']}"
    )
    assert res["exclusions"].get("outside_time_window") == 6, res["exclusions"]
    assert res["total_candidates"] == 6, res


def test_vector_matches_outside_capture_window_are_counted() -> None:
    """Count captures outside the window when no event time is declared."""
    store, client = _store(4, dated=False)
    res = _vector_call(store, client)

    assert res["outcome"] == "missing_evidence", res
    assert res["exclusions"] == {"outside_time_window": 4}, res["exclusions"]


def test_the_cap_still_bounds_what_feeds_ranking() -> None:
    """Counting excluded ones does not disable the cap on ELIGIBLE ones.

    `tools_schema.json`: «the top `vector_k` **eligible** capsules by cosine
    feed vector-bearing ranking». Without a window, 14 eligible and `vector_k=3` gives 3.
    """
    store = Store.open_disposable_test_store()
    client = EmbeddingClient(custom_hook=lambda text: list(UNIT))
    for i in range(14):
        cid, _ = store.append(_cap(f"Elegivel {i}.", f"ve:{i}"), NOW)
        store.set_birth_classification(cid, "fact", "project", NOW)
        put_embedding(
            store.db,
            cid,
            list(UNIT),
            client.space_fingerprint.as_str(),
            sha256_hex(cid.encode()),
        )
    res = retrieve(
        store,
        RetrieveParams(
            query="zzztermoqueninguemtem",
            project_id="nmemory",
            lane="vector",
            limit=20,
            vector_k=3,
            token_budget=100_000,
        ),
        embeddings=client,
        now=NOW,
    )
    assert res["outcome"] == "grounded", res
    assert len(res["evidence"]) == 3, res


def test_a_huge_vector_k_is_refused_not_a_poisoned_transaction() -> None:
    """RED: `2**63` reaches LIMIT, aborts the transaction, and returns `-32603`."""
    store, client = _store(2, dated=False)
    server = MemoryServer(
        store=store, embeddings=client, anchor_root=".", project_id="nmemory"
    )
    res = server.call_tool(
        "nmemory_capsule_retrieve",
        {"terms": ["x"], "project_id": "nmemory", "vector_k": 2**63},
        None,
        now=NOW,
    )
    assert "error" in res, f"vector_k=2**63 must be refused at the door; got {res}"
    assert res["error"]["code"] == -32602, (
        f"-32603 is an internal failure; an out-of-range value is the CALLER's: {res}"
    )


def test_the_early_abstain_echoes_the_lane_the_caller_asked_for() -> None:
    """Empty query: no lane runs, but `lane` is what the caller ASKED FOR."""
    store, client = _store(1, dated=False)
    server = MemoryServer(
        store=store, embeddings=client, anchor_root=".", project_id="nmemory"
    )
    res = server.call_tool(
        "nmemory_capsule_retrieve",
        {"terms": ["   "], "project_id": "nmemory", "lane": "vector"},
        None,
        now=NOW,
    )
    assert "error" not in res, res
    payload = json.loads(res["result"]["content"][0]["text"])
    assert payload["outcome"] == "abstain", payload
    assert payload["lane"] == "vector", (
        f"o caller pediu `vector`; o envelope diz {payload['lane']}"
    )
    # `lane_used` remains `term`: no lane ran, and making up `vector`
    # would be the symmetric lie.
    assert payload["lane_used"] == "term", payload


def test_the_cap_never_drops_what_the_term_lane_found() -> None:
    """The cap bounds the VECTOR lane, never what the term lane already brought.

    In `fused` with `vector_k=1`, a capsule that matches BY TERM survives via
    its own lane even being outside the cosine top-1. Without the
    `id in term_rank_map` clause the vector cap would steal results from a lane
    that is not its own.
    """
    store = Store.open_disposable_test_store()
    client = EmbeddingClient(custom_hook=lambda text: list(UNIT))
    termo = "helicoide"
    for i in range(5):
        content = f"Nota sobre {termo} numero {i}."
        cid, _ = store.append(_cap(content, f"vt:{i}"), NOW)
        store.set_birth_classification(cid, "fact", "project", NOW)
        put_embedding(
            store.db,
            cid,
            list(UNIT),
            client.space_fingerprint.as_str(),
            sha256_hex(cid.encode()),
        )

    res = retrieve(
        store,
        RetrieveParams(
            query=termo,
            project_id="nmemory",
            lane="fused",
            limit=20,
            vector_k=1,
            token_budget=100_000,
        ),
        embeddings=client,
        now=NOW,
    )
    assert res["outcome"] == "grounded", res
    assert len(res["evidence"]) == 5, (
        f"all 5 matched by term; vector_k=1 may only fence the vector lane. "
        f"got {len(res['evidence'])}"
    )


def _windowed_store(n_in: int, n_out: int) -> tuple[Store, EmbeddingClient]:
    """`n_in` capsules INSIDE the window and `n_out` outside, all with embeddings."""
    store = Store.open_disposable_test_store()
    client = EmbeddingClient(custom_hook=lambda text: list(UNIT))
    inside = datetime(2024, 6, 1, 12, 0, 0, tzinfo=UTC)
    for i in range(n_in + n_out):
        at = inside if i < n_in else OUT_OF_WINDOW
        cid, _ = store.append_capture(
            _cap(f"Janela {i}.", f"vw:{i}"), None, EventTimeRange.point(at), None, NOW
        )
        store.set_birth_classification(cid, "fact", "project", NOW)
        put_embedding(
            store.db,
            cid,
            list(UNIT),
            client.space_fingerprint.as_str(),
            sha256_hex(cid.encode()),
        )
    return store, client


def test_the_cap_counts_what_it_drops() -> None:
    """RED: the cap erases eligible capsules without any counter.

    The invariant declared by this file — every id that cosine returns ends up
    as a candidate or ends up as a COUNTED exclusion — was false for everything the cap
    cut. Round 2 caught evaporation BEFORE the eligibility loop; I
    moved it to AFTER it instead of closing it.

    With 20 inside the window, 10 outside and `vector_k=3`:
    `total_candidates` = 30, `matched` = 3, `outside_time_window` = 10, and the 17
    that the cap cut must appear.
    """
    store, client = _windowed_store(20, 10)
    res = _vector_call(store, client, vector_k=3)

    assert res["outcome"] == "grounded", res
    total = res["total_candidates"]
    excl = sum(res["exclusions"].values())
    matched = len(res["evidence"])
    assert res["exclusions"].get("outside_time_window") == 10, res["exclusions"]
    assert res["exclusions"].get("vector_k_capped") == 17, res["exclusions"]
    assert total == matched + excl, (
        f"total_candidates={total} must balance with matched={matched} + "
        f"exclusions={excl}; {total - matched - excl} capsules evaporated"
    )


def test_a_zero_cap_never_serves_an_empty_exclusion_map() -> None:
    """RED: `vector_k=0` yields `missing_evidence` with `excluded={}`.

    Six capsules pass all fences, the caller cap cuts all six, and the
    envelope says "6 matched but excluded" without naming a single excluder. It is the
    outcome dishonesty that round 2 named, in its second form.
    """
    store, client = _windowed_store(6, 0)
    res = _vector_call(store, client, vector_k=0)

    assert res["outcome"] == "missing_evidence", res
    assert res["exclusions"].get("vector_k_capped") == 6, (
        f"o teto cortou as 6 e o mapa de exclusoes diz {res['exclusions']}"
    )
    assert res["total_candidates"] == sum(res["exclusions"].values()), res


# --------------------------------------------------------------------------
# F2 — the abstain names the lane that RAN, and the ledger only stores what failed
# --------------------------------------------------------------------------
#
# The test above (`test_the_early_abstain_echoes_the_lane_the_caller_asked_for`)
# fixes the contract of `lane_used` for the case where NO lane runs, and nothing
# here touches it: making `lane_used` say `vector` would be the symmetric lie.
#
# The defect in F2 is different, and the two cases are not the same. That one passes
# `terms=["   "]`, where neither the term lane nor the vector lane runs. The case
# measured in the live store 2026-09-12 passed a REAL term with `lane="vector"`: the
# vector lane RAN, the index reached nothing because of the fence, and the
# response came back with the reason "no stored capsule matched any of the 1 query
# term(s)" — a lexical cause for a lexical lane that was never consulted.


def _fenced_vector_abstain(store: Store, client: EmbeddingClient) -> dict[str, Any]:
    """`lane="vector"` with a REAL term, fenced to a project with nothing."""
    server = MemoryServer(
        store=store, embeddings=client, anchor_root=".", project_id="nmemory"
    )
    res = server.call_tool(
        "nmemory_capsule_retrieve",
        {
            "terms": ["helicoide"],
            "project_id": "projectoquenaoexistemesmo",
            "lane": "vector",
        },
        None,
        now=NOW,
    )
    assert "error" not in res, res
    payload: dict[str, Any] = json.loads(res["result"]["content"][0]["text"])
    assert payload["outcome"] == "abstain", payload
    return payload


def test_the_abstain_names_the_lane_that_actually_ran() -> None:
    """The response distinguishes "no lane ran" from "the vector lane ran empty".

    `lanes_run` is pure fact: the lanes that EXECUTED. With `lane="vector"` the
    term lane does not run, so it cannot be there — and the reason cannot
    blame terms that no one searched for.
    """
    store, client = _store(3, dated=False)
    payload = _fenced_vector_abstain(store, client)

    assert payload["lanes_run"] == ["vector"], (
        f"the term lane did not run and still shows up: {payload}"
    )
    assert "query term(s)" not in payload["reason"], (
        f"the reason blames term matching in a call where the term lane"
        f" never ran: {payload['reason']}"
    )
    assert "term lane did not run" in payload["reason"], payload["reason"]


def test_a_lane_that_never_ran_writes_no_miss_ledger_row() -> None:
    """F2b: false `run_term` does not generate a miss row for the term lane.

    The row attributed to the term lane a failure it did not have, and a ledger
    that mixes both lanes makes the wave 2 metric unverifiable. The
    audit of 2026-09-12 could no longer attribute the
    `embedding -> abstain` entry from 23:14 to any lane.
    """
    store, client = _store(3, dated=False)
    before = len(store.traffic.list_recall_misses(limit=1000))

    _fenced_vector_abstain(store, client)

    assert len(store.traffic.list_recall_misses(limit=1000)) == before, (
        "a vector-only call added a row to the term lane ledger"
    )


def test_a_term_lane_that_ran_and_missed_still_records_it() -> None:
    """The COUNTER-CASE, and it is what prevents suppression from spreading.

    `retrieve.py` DELIBERATELY records the term lane miss when it
    ran and found nothing — including in a response grounded by the vector
    lane. Suppressing that case as well would erase the only record of the real
    failure, which is precisely the raw material wave 2 will read.
    """
    store, client = _store(3, dated=False)
    before = len(store.traffic.list_recall_misses(limit=1000))

    res = retrieve(
        store,
        RetrieveParams(
            query="zzztermoqueninguemtem",
            terms=["zzztermoqueninguemtem"],
            project_id="nmemory",
            lane="term",
        ),
        embeddings=client,
        now=NOW,
    )

    assert res["outcome"] == "abstain", res
    assert res["lanes_run"] == ["term"], res
    assert len(store.traffic.list_recall_misses(limit=1000)) == before + 1, (
        "the term lane ran, found nothing, and the ledger lost the row"
    )


def test_a_ledger_write_failure_never_kills_the_read() -> None:
    """The ledger write is guarded: `retrieve()` is a READ path.

    Unguarded, a typed failure in `record_recall_miss` propagated from
    inside `retrieve()` and crashed a response that was already ready — the
    misses ledger killing the retrieval it exists to measure.
    """
    store, client = _store(3, dated=False)

    def _explode(term: str, outcome: str, now: datetime, **_kw: Any) -> None:
        raise DatabaseError("ledger is on fire")

    store.traffic.record_recall_miss = _explode  # type: ignore[method-assign]

    res = retrieve(
        store,
        RetrieveParams(
            query="zzztermoqueninguemtem",
            terms=["zzztermoqueninguemtem"],
            project_id="nmemory",
            lane="term",
        ),
        embeddings=client,
        now=NOW,
    )

    assert res["outcome"] == "abstain", res
    assert res["lanes_run"] == ["term"], res


# --------------------------------------------------------------------------
# L8 — the ledger stores what wave 2 will need
# --------------------------------------------------------------------------


def test_a_miss_row_carries_enough_to_replay_the_call() -> None:
    """The fence, the lane, and the call — without them "replaying" has no substance.

    The row was `(seq, term, outcome, at)`. Two of the four diagnoses that
    lock 4 wanted — "the fence was wrong" and "the capsule exists and the index did not
    reach it" — are invisible in it, and the metric "the same query now
    succeeds" was satisfiable with an INSERT, because the synonyms table expands every
    term on retrieval.
    """
    store, client = _store(3, dated=False)

    retrieve(
        store,
        RetrieveParams(
            query="alfa beta",
            terms=["zzzalfa", "zzzbeta"],
            project_id="nmemory",
            project_prefix="nmem",
            lane="term",
        ),
        embeddings=client,
        now=NOW,
    )

    rows = store.traffic.list_recall_misses(limit=10)
    assert len(rows) == 2, rows
    assert {r["term"] for r in rows} == {"zzzalfa", "zzzbeta"}
    for row in rows:
        assert row["lane"] == "term", row
        assert row["project_id"] == "nmemory", row
        assert row["project_prefix"] == "nmem", row
        assert row["call_id"], row
    # Both terms came from the SAME call, and the ledger knows it.
    assert len({r["call_id"] for r in rows}) == 1, rows


def test_a_pre_l8_row_says_it_has_no_context_instead_of_inventing_one() -> None:
    """`None`, never a fabricated value.

    The rows recorded before this unit have no context. A default
    invented here would be the same disease as F2: a plausible cause where no
    measurement took place.
    """
    store, _client = _store(1, dated=False)
    store.traffic.record_recall_miss("linhaantiga", "abstain", NOW)

    row = store.traffic.list_recall_misses(limit=1)[0]
    assert row["term"] == "linhaantiga", row
    assert row["call_id"] is None, row
    assert row["lane"] is None, row
    assert row["project_id"] is None, row
