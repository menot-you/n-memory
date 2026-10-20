"""The `project_prefix` fence is VALIDATED and then APPLIED — never discarded.

Upstream implementation reference points:

- `store.rs:1802 project_matches` — `project_id` and `project_prefix` compose by
  AND, and the prefix matches the EXACT id or the id followed by `/`, never the id
  followed by anything else: `"nott"` covers `"nott"` and `"nott/x"`, never `"nottx"`.
- `server/retrieve.rs:45` — "Every authority, lifecycle, time, project and
  session fence applies to BOTH lanes". A fence proved in one lane and not in
  the other is half a fence.
- `server/visual.rs:50` — the prefix applies ONLY to `view=tiers`; with
  `dag`/`relations`/`sessions` it is REFUSED with an instructional error, never ignored.
- `server/digest.rs` — capsule sections honor the prefix, and `recent_failures`
  is WITHHELD under a prefix: miss ledger rows have no project attribution, so
  printing them under a fence leaks store-global query text.

The defect that these tests reproduce is the WORST form of fencing: `server.py:1418`
validates the prefix FORMAT (refuses empty, refuses ending with `/`) and dispatch
discards it. Validation makes the parameter APPEAR applied, and a fenced call
returns store-global data without any indication.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from typing import Any

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
from nmemory.server import MemoryServer
from nmemory.space import DIMENSION
from nmemory.store import Store

NOW = datetime(2026, 8, 30, 12, 0, 0, tzinfo=UTC)
UNIT_VEC: list[float] = [1.0] + [0.0] * (DIMENSION - 1)

# The lexical marker that ALL capsules share: the term lane matches all four,
# and only the fence separates them.
MARKER = "prefixmarker"

# `treex` exists for a reason: bare `startswith` matches it with the prefix `tree`,
# but the pin does not. It is the only case that distinguishes the correct fence from almost-correct.
PROJECTS = ("tree", "tree/child", "other", "treex")


def cap_in(project: str) -> Capsule:
    content = f"Capsule {MARKER} of project {project}."
    return Capsule(
        content=content,
        provenance=Provenance(
            source="test:scope-fences",
            anchor=f"tests/test_scope_fences_applied.py:{project}",
            source_hash=sha256_hex(content.encode("utf-8")),
        ),
        confidence=0.9,
        freshness=Freshness(valid_from=NOW),
        scope=Scope(project_id=project),
        authority_class=AuthorityClass.USER_STATED,
        instruction_taint=False,
    )


def seeded_store(
    *, embeddings: EmbeddingClient | None = None
) -> tuple[Store, dict[str, str]]:
    """One capsule per project, and the project -> id map for assertions."""
    store = Store.open_disposable_test_store()
    ids: dict[str, str] = {}
    for project in PROJECTS:
        cid, _ = store.append(cap_in(project), NOW)
        ids[project] = cid
        if embeddings is not None:
            put_embedding(
                store.db,
                cid,
                list(UNIT_VEC),
                embeddings.space_fingerprint.as_str(),
                sha256_hex(cid.encode("utf-8")),
            )
    return store, ids


def hooked_client() -> EmbeddingClient:
    return EmbeddingClient(custom_hook=lambda text: list(UNIT_VEC))


def ok(res: dict[str, Any]) -> dict[str, Any]:
    assert "result" in res and res["result"]["isError"] is False, res
    payload: dict[str, Any] = json.loads(res["result"]["content"][0]["text"])
    return payload


def in_subtree(ids: dict[str, str]) -> set[str]:
    return {ids["tree"], ids["tree/child"]}


def outside_subtree(ids: dict[str, str]) -> set[str]:
    return {ids["other"], ids["treex"]}


# --------------------------------------------------------------------------
# nmemory_capsule_retrieve — BOTH lanes, one fence
# --------------------------------------------------------------------------


def test_term_lane_honours_a_prefix_only_call() -> None:
    """Prefix only, without `project_id`: the lexical lane fences to the subtree.

    This is the form that catches the handler's `or "nmemory"`: a manufactured
    `project_id` composes by AND against the prefix and the call returns nothing — or,
    worse, returns the wrong project.
    """
    store, ids = seeded_store()

    res = retrieve(
        store,
        RetrieveParams(query=MARKER, project_prefix="tree"),
        embeddings=None,
        now=NOW,
    )

    returned = {ev["id"] for ev in res.get("evidence", [])}
    assert returned == in_subtree(ids), res
    assert not (returned & outside_subtree(ids))


def test_vector_lane_honours_a_prefix_only_call() -> None:
    """The query matches no term: only the vector lane returns candidates.

    Isolates the other half of the fence. If the prefix only reaches `search_fts_scoped`,
    all four capsules enter here and `other`/`treex` leak.
    """
    client = hooked_client()
    store, ids = seeded_store(embeddings=client)

    res = retrieve(
        store,
        RetrieveParams(query="zzzunmatchedtermxyz", project_prefix="tree"),
        embeddings=client,
        now=NOW,
    )

    returned = {ev["id"] for ev in res.get("evidence", [])}
    assert returned == in_subtree(ids), res
    assert not (returned & outside_subtree(ids))


def test_prefix_and_project_id_compose_by_and() -> None:
    """`project_matches`: the two names fence TOGETHER, never one alone."""
    store, ids = seeded_store()

    res = retrieve(
        store,
        RetrieveParams(query=MARKER, project_id="tree/child", project_prefix="tree"),
        embeddings=None,
        now=NOW,
    )

    assert {ev["id"] for ev in res.get("evidence", [])} == {ids["tree/child"]}


def test_retrieve_over_the_wire_fences_the_prefix() -> None:
    """The fence reaches the real consumer: `call_tool`, not just the engine."""
    store, ids = seeded_store()
    server = MemoryServer(store=store)

    # Retrieve reads the clock unless told; capsules unread for over 30 days are
    # excluded, and the fixture date is in the past.
    payload = ok(
        server.call_tool(
            "nmemory_capsule_retrieve",
            {"terms": [MARKER], "project_prefix": "tree"},
            now=NOW,
        )
    )

    returned = {r["id"] for r in payload.get("results", [])}
    assert returned == in_subtree(ids), payload


# --------------------------------------------------------------------------
# nmemory_digest_show — capsule sections fence, store-global sections do not
# --------------------------------------------------------------------------


def test_digest_fences_its_capsule_sections() -> None:
    store, ids = seeded_store()
    server = MemoryServer(store=store)

    payload = ok(server.call_tool("nmemory_digest_show", {"project_prefix": "tree"}))

    assert payload["total"] == 2, payload
    assert {row["project_id"] for row in payload["by_project"]} == {
        "tree",
        "tree/child",
    }
    assert {row["id"] for row in payload["newest"]} == in_subtree(ids)


def test_digest_withholds_recent_failures_under_a_prefix() -> None:
    """Miss rows have no project: printing them under a fence is a leak."""
    store, _ = seeded_store()
    store.traffic.record_recall_miss("segredodeoutroprojecto", "abstain", NOW)
    server = MemoryServer(store=store)

    unfenced = ok(server.call_tool("nmemory_digest_show", {}))
    assert "recent_failures" in unfenced, unfenced

    fenced = ok(server.call_tool("nmemory_digest_show", {"project_prefix": "tree"}))
    assert "recent_failures" not in fenced, fenced
    assert "segredodeoutroprojecto" not in json.dumps(fenced)


# --------------------------------------------------------------------------
# The fence must be THE SAME in both lanes — literal, never a PATTERN
# --------------------------------------------------------------------------


def test_a_like_metacharacter_never_becomes_a_wildcard() -> None:
    """`%` and `_` are SQL LIKE metacharacters and NOT subtree metacharacters.

    The term lane compares literally (`in_project_subtree`); the vector lane
    compares in SQL. Passing the raw prefix to `LIKE` causes both to disagree
    WITHIN the same call, and a `%` matches EVERYTHING — the fence becomes a sieve.
    The pin has no pattern semantics whatsoever (`store.rs:1802
    project_matches`: equality or `strip_prefix(...).starts_with('/')`).
    """
    client = hooked_client()
    store, _ids = seeded_store(embeddings=client)

    for prefix in ("%", "tre_", "_ree", "tree%"):
        res = retrieve(
            store,
            RetrieveParams(query="zzzunmatchedtermxyz", project_prefix=prefix),
            embeddings=client,
            now=NOW,
        )
        returned = {ev["id"] for ev in res.get("evidence", [])}
        assert returned == set(), (prefix, returned, res)


def test_the_two_lanes_never_disagree_inside_one_call() -> None:
    """What the lexical lane refuses, the vector lane must refuse as well.

    `tre_` is the minimal case: `LIKE 'tre_/%'` matches `tree/child`, while
    `in_project_subtree("tree/child", "tre_")` refuses it. A fence that depends
    on which lane responded is not a fence.
    """
    client = hooked_client()
    store, _ = seeded_store(embeddings=client)

    lexical = retrieve(
        store,
        RetrieveParams(query=MARKER, project_prefix="tre_"),
        embeddings=None,
        now=NOW,
    )
    vectorial = retrieve(
        store,
        RetrieveParams(query="zzzunmatchedtermxyz", project_prefix="tre_"),
        embeddings=client,
        now=NOW,
    )

    assert {ev["id"] for ev in lexical.get("evidence", [])} == set(), lexical
    assert {ev["id"] for ev in vectorial.get("evidence", [])} == set(), vectorial


def test_an_empty_project_id_is_a_fence_not_an_absence() -> None:
    """`""` is a fence that matches NOTHING — never "the caller did not ask".

    `args.get("project_id") or ...` takes the falsy branch on an empty string and
    discards the EXACT half of the fence silently. The pin composes by AND
    (`exact.is_none_or(|v| project_id == v)`), so `""` matches no capsule at all
    and the honest answer is `abstain`. It is a REGRESSION: in the base,
    `or "nmemory"` caused this call to abstain.
    """
    store, _ = seeded_store()
    server = MemoryServer(store=store)

    payload = ok(
        server.call_tool(
            "nmemory_capsule_retrieve",
            {"terms": [MARKER], "project_id": "", "project_prefix": "tree"},
        )
    )

    assert payload["outcome"] == "abstain", payload
    assert not payload.get("results"), payload


# --------------------------------------------------------------------------
# F1 — the fence must not BLIND the index it fences
# --------------------------------------------------------------------------
#
# The tests above prove that the fence APPLIES. This proves the other half, which
# none of them saw: that it applies without clearing the window. `search_cosine` puts
# the fence ABOVE the HNSW index scan, so with `hnsw.iterative_scan` at default
# `off` the index yields `ef_search` GLOBAL candidates and the fence prunes them
# all. Measured on the live store 2026-09-12: recall@10 of 19.6% in `nott/tech`,
# 17 of 25 probes returning ZERO, and the same measurement rising to 95.6% with the
# fix.
#
# THREE things had to be true at the same time for this RED to exist, and
# each cost a failed attempt:
#
# 1. The planner must CHOOSE HNSW. `enable_seqscan=off` alone is not enough —
#    it switches to a bitmap scan over `capsules_project` and sorts exactly,
#    yielding 10 of 10 and stamping the defect green. Only `enable_sort=off` together
#    removes the sorting alternative and forces an ordered index scan.
# 2. GUCs must reach the connection that `search_cosine` opens — and it opens ITS
#    OWN, on each call (`_open_connection`, autocommit). Database level is the
#    only one that reaches, and only in a disposable `_pytest` database.
# 3. Fenced vectors must be REACHABLE in the HNSW graph. Two earlier synthetic
#    geometries — fenced orthogonal to the probe, and fenced in a wide cone
#    — formed an ISOLATED component: with `ef_search=400` over 220 rows the index
#    returned 200, and the 20 fenced never appeared, with or without the
#    fix. An arc of increasing angle keeps the graph connected, and there the defect
#    and the fix separate: 0 rows versus 10.

FAR_PROJECT = "f1outside"
FENCED_PROJECT = "f1fenced"
# 200 against 20: the fence covers 9% of the set, within the measured dead zone
# (5-13%), and 200 > `ef_search` (40) — without this there is no window to prune.
FAR_COUNT = 200
FENCED_COUNT = 20
# The arc step. Small enough for neighbors to connect in the graph,
# large enough for cosine ordering to remain strict.
ARC_STEP = 0.004


def _arc_vector(index: int) -> list[float]:
    """A point on the arc in the (e0, e1) plane, at `index * ARC_STEP` radians.

    The distance to the probe increases with `index`, monotonically: all 200
    outside sit ahead of the 20 fenced. And because they are neighbors of one another,
    HNSW connects them in a chain — which makes the fenced reachable by a scan that
    CONTINUES, and unreachable by one that stops at `ef_search`.
    """
    theta = index * ARC_STEP
    vec = [0.0] * DIMENSION
    vec[0] = math.cos(theta)
    vec[1] = math.sin(theta)
    return vec


def _seed_arc_store(client: EmbeddingClient) -> Store:
    store = Store.open_disposable_test_store()
    space = client.space_fingerprint.as_str()
    # One boundary for the whole seed: one write transaction, not one per row.
    with store.db.tool_boundary():
        _seed_arc(store, space)
    return store


def _seed_arc(store: Store, space: str) -> None:
    for project, count, offset in (
        (FAR_PROJECT, FAR_COUNT, 1),
        (FENCED_PROJECT, FENCED_COUNT, FAR_COUNT + 1),
    ):
        for i in range(count):
            content = f"F1 {project} capsula numero {i}."
            capsule = Capsule(
                content=content,
                provenance=Provenance(
                    source="test:f1-fence-blinds-the-index",
                    anchor=f"tests/test_scope_fences_applied.py:{project}:{i}",
                    source_hash=sha256_hex(content.encode("utf-8")),
                ),
                confidence=0.9,
                freshness=Freshness(valid_from=NOW),
                scope=Scope(project_id=project),
                authority_class=AuthorityClass.USER_STATED,
                instruction_taint=False,
            )
            cid, _ = store.append(capsule, NOW)
            put_embedding(
                store.db,
                cid,
                _arc_vector(offset + i),
                space,
                sha256_hex(cid.encode("utf-8")),
            )


def test_a_minority_fence_does_not_blind_the_vector_index() -> None:
    """A minority fence returns the requested window, not zero.

    The probe points to the start of the arc: the 200 closest neighbors are ALL
    outside the fence. The retired PostgreSQL index stopped at its first
    `ef_search` and left the fence with nothing — the outcome the live store gave
    17 times out of 25. The fence is applied before the ranking, so the window
    fills from inside the fence however far its members sit.
    """
    client = EmbeddingClient(custom_hook=lambda text: _arc_vector(0))
    store = _seed_arc_store(client)

    rows = store.embeddings.search_cosine(
        query_vector=_arc_vector(0),
        limit=10,
        project_id=FENCED_PROJECT,
        space_fingerprint=client.space_fingerprint.as_str(),
    )

    assert len(rows) == 10, (
        f"the minority fence returned {len(rows)} of 10 requested: the index scan"
        " stopped before reaching it and the fence erased the rest"
    )
