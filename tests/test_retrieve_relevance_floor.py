"""The relevance floor PER LANE: what matches weakly becomes a COUNTED exclusion.

The defect these tests close: `abstain` is STRUCTURALLY unreachable in the
path the product serves. `retrieve.py` only abstains when
`term_rank_map | vector_rank_map` is empty, and the default lane (`auto`) turns on
the vector lane, which returns up to `vector_k` ids for ANY non-empty query.
A probe whose only matches are stopwords returned `grounded`.

The fix is NOT to widen `abstain` — the published contract defines `abstain` as
ZERO matches, and a matching stopword IS a match. What was missing was
making the third outcome reachable: what matches below the floor becomes
`below_relevance_floor`, a COUNTED exclusion, and the outcome is `missing_evidence`.

**Two floors, one per lane, and never a single floor over the union**: BM25 is positive
without a ceiling (`store.py` `search_fts_scoped`), while cosine lives on another scale. A
single floor downstream of the union would compare the two.

**The ORDER, published because two counted exclusions on the same population
make counts order-dependent:** the floor is the LAST fence in the eligibility
loop — after `min_confidence`, before RRF — and therefore BEFORE the
`vector_k_capped` cap, which runs over `eligible`. Two consequences measured
here: a capsule pruned by the floor is never counted by the cap
(`test_the_floor_runs_before_the_vector_cap`), and a capsule that a stronger
fence already excluded retains the NAME of that fence
(`test_a_stronger_fence_keeps_its_own_name`).

**The default `0.0` is a PROVED no-op, not a promise** — `test_the_default_is_the_envelope_of_today`
pins the envelope captured on base `b1f4ef92`, with the serializer named
(`json.dumps(..., sort_keys=True)`), because float bytes are not unique.

**Measurement that corrected PLAN.md:** `postgres.py` `search_cosine` returns
`1 - (embedding <=> query)` and pgvector's `<=>` is cosine distance in
`[0, 2]`, so `sim` lives in `[-1, 1]` and NOT in `[0, 1]` as §8 of the PLAN claims.
Measured on this box: an embedding opposite to the query reads `sim == -1.0`. With the raw
retention check `score >= floor`, default `0.0` would EXCLUDE that capsule and
cease to be a no-op — which is the property the PLAN itself treats as
load-bearing. Therefore a NON-POSITIVE floor fences nothing: `0.0` is OFF.
"""

from __future__ import annotations

import itertools
import json
import math
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from storefile import put_embedding, sample_capsule_id

from nmemory.capsule import (
    AuthorityClass,
    Capsule,
    Freshness,
    Provenance,
    Scope,
    sha256_hex,
)
from nmemory.embedding import EmbeddingClient
from nmemory.repositories.capsules import EventTimeRange, StoredCapsule
from nmemory.retrieve import RetrieveParams, retrieve
from nmemory.space import DIMENSION
from nmemory.store import Store

NOW = datetime(2026, 9, 2, 12, 0, 0, tzinfo=UTC)
#: The window ceased to be the only way for the lane to fetch MORE than `vector_k`:
#: since B3 (`retrieve.py:548`) the SQL LIMIT is `max(v_limit, 50)` WITH and WITHOUT
#: window, so the cap cuts after eligibility in both cases. The window
#: continues to fix WHICH capsules enter, which is what this file measures.
IN_WINDOW = datetime(2024, 6, 1, 12, 0, 0, tzinfo=UTC)
WINDOW = {"from": "2024-01-01T00:00:00Z", "to": "2024-12-31T23:59:59Z"}
PROJECT = "nmemory"

#: Query vector (hook always returns this one) and the three neighbors fixing the
#: measured domain of `sim`: identical (1.0), oblique (~0.25), opposite (-1.0).
UNIT: list[float] = [1.0] + [0.0] * (DIMENSION - 1)
OBLIQUE: list[float] = [0.25, math.sqrt(1.0 - 0.0625)] + [0.0] * (DIMENSION - 2)
OPPOSITE: list[float] = [-1.0] + [0.0] * (DIMENSION - 1)


def _capsule(content: str, anchor: str, project_id: str = PROJECT) -> Capsule:
    return Capsule(
        content=content,
        provenance=Provenance(
            source="test",
            anchor=anchor,
            source_hash=sha256_hex(content.encode("utf-8")),
        ),
        confidence=0.9,
        freshness=Freshness(valid_from=NOW),
        scope=Scope(project_id=project_id),
        authority_class=AuthorityClass.USER_STATED,
        instruction_taint=False,
    )


_SHARED: list[Store] = []


def _shared_store() -> Store:
    """One store per TEST for property tests, shared by its examples: opening one per
    example would pay a new store file for each."""
    if not _SHARED:
        _SHARED.append(Store.open_disposable_test_store())
    return _SHARED[0]


@pytest.fixture(autouse=True)
def _forget_the_shared_store() -> Iterator[None]:
    """The store lives in the test's own `NOTT_DATABASE_DIR`, removed when the test ends.

    A module is imported once per process, and mutmut runs several pytest sessions in
    one: a store kept past its test sent the next session to a removed directory.
    """
    yield
    _SHARED.clear()


def _client() -> EmbeddingClient:
    return EmbeddingClient(custom_hook=lambda text: list(UNIT))


def _add(
    store: Store,
    client: EmbeddingClient,
    content: str,
    anchor: str,
    vec: list[float],
    *,
    dated: bool = False,
    project_id: str = PROJECT,
) -> str:
    cap = _capsule(content, anchor, project_id=project_id)
    if dated:
        cid, _ = store.append_capture(
            cap, None, EventTimeRange.point(IN_WINDOW), None, NOW
        )
    else:
        cid, _ = store.append(cap, NOW)
    store.set_birth_classification(cid, "fact", "project", NOW)
    put_embedding(
        store.db, cid, vec, client.space_fingerprint.as_str(), sha256_hex(cid.encode())
    )
    return cid


def _call(store: Store, client: EmbeddingClient, **kw: Any) -> dict[str, Any]:
    """The DEFAULT lane (`auto`), which is where the defect lives."""
    params = {
        "project_id": PROJECT,
        "limit": 20,
        "token_budget": 100_000,
        **kw,
    }
    return retrieve(
        store,
        RetrieveParams(**params),
        embeddings=client,
        now=NOW,
        record_receipt=False,
    )


def _stopword_corpus(n: int = 6) -> tuple[Store, EmbeddingClient]:
    """`n` capsules whose only common token with the probe is the stopword `the`.

    All embedded in OBLIQUE: they match in BOTH lanes, and weakly in both.
    """
    store = Store.open_disposable_test_store()
    client = _client()
    for i in range(n):
        _add(store, client, f"the sessao numero {i} do registo.", f"s:{i}", OBLIQUE)
    return store, client


def _bm25_of(store: Store, term: str) -> float:
    matches: list[tuple[StoredCapsule, float]] = store.capsules.search_fts_scoped(
        terms=[term],
        project_id=PROJECT,
        project_prefix=None,
        session_id=None,
        effort_set=None,
        topic_set=None,
        limit=50,
    )
    assert matches, (
        f"the probe `{term}` must match for the floor to have something to cut"
    )
    return min(score for _sc, score in matches)


# ---------------------------------------------------------------- the DoD, half 1


def test_a_stopword_only_probe_is_missing_evidence_not_grounded() -> None:
    """RED: currently this reads `grounded` with 6 sessions that matched only via `the`."""
    store, client = _stopword_corpus()
    floor = _bm25_of(store, "the") + 0.05

    res = _call(store, client, query="the", term_floor=floor, vector_floor=0.5)

    assert res["outcome"] == "missing_evidence", (
        "6 capsules matched only on the stopword and all fell below the floor of "
        f"BOTH lanes; got {res['outcome']} with exclusions={res['exclusions']}"
    )
    assert res["outcome"] != "abstain", (
        "`abstain` is ZERO matches, and a stopword that matches IS a match"
    )
    assert res["exclusions"].get("below_relevance_floor") == 6, res["exclusions"]
    assert res["evidence"] == [], res
    assert res["total_candidates"] == 6, res


def test_the_control_probe_grounds_and_still_counts_the_floor() -> None:
    """The third outcome is not relabeled `abstain`: partial exclusion grounds."""
    store, client = _stopword_corpus()
    _add(store, client, "the sesamoraro do registo.", "s:raro", UNIT)
    floor = _bm25_of(store, "the") + 0.05

    res = _call(
        store,
        client,
        query="the sesamoraro",
        term_floor=floor,
        vector_floor=0.5,
    )

    assert res["outcome"] == "grounded", res
    assert res["exclusions"].get("below_relevance_floor") == 6, res["exclusions"]
    assert len(res["evidence"]) == 1, res


def test_stopword_only_matches_for_a_content_query_are_below_relevance_floor() -> None:
    """When a query has content words and candidates match only stop words,
    term-only recall reports missing_evidence with below_relevance_floor,
    while zero matches still report abstain."""
    store, client = _stopword_corpus()

    res = _call(store, client, query="the sesamoraro", lane="term")

    assert res["outcome"] == "missing_evidence", res
    assert res["evidence"] == [], res
    assert res["exclusions"].get("below_relevance_floor") == 6, res["exclusions"]

    zero = _call(store, client, query="zzznadacasa", lane="term")
    assert zero["outcome"] == "abstain", zero
    assert zero["exclusions"] == {}, zero


def test_contraction_fragments_only_match_is_below_relevance_floor() -> None:
    """Contractions split into 1-char and suffix fragments (`i`, `m`, `re`, `s`, `ve`, `ll`, `t`, `d`)
    are stop words, never content words."""
    store = Store.open_disposable_test_store()
    client = _client()
    _add(store, client, "I'm here today and you're there.", "c:1", OBLIQUE)

    res = _call(
        store,
        client,
        query="I'm sesamoraro",
        terms=["I'm", "sesamoraro"],
        lane="term",
    )

    assert res["outcome"] == "missing_evidence", res
    assert res["evidence"] == [], res
    assert res["exclusions"].get("below_relevance_floor") == 1, res["exclusions"]


def test_fused_stopword_only_match_failing_vector_margin_is_below_relevance_floor() -> (
    None
):
    """In fused/auto mode, a candidate that matched only stop words in the term
    lane and failed VECTOR_MARGIN is counted under below_relevance_floor."""
    store = Store.open_disposable_test_store()
    client = _client()
    best = _add(store, client, "Alpha nearest neighbour.", "f:best", UNIT)
    _add(store, client, "the only match in far vector space.", "f:stop", OBLIQUE)

    res = _call(
        store,
        client,
        query="the zuluquery",
        terms=["the", "zuluquery"],
        lane="auto",
    )

    assert res["outcome"] == "grounded", res
    assert [e["id"] for e in res["evidence"]] == [best], res
    assert res["exclusions"].get("below_relevance_floor") == 1, res["exclusions"]
    assert "below_vector_margin" not in res["exclusions"], res["exclusions"]


# ---------------------------------------------------------------- the DoD, half 2


def _golden_result() -> dict[str, Any]:
    """The baseline envelope. Called by golden capture AND by the test.

    Intentionally passes no floor: it is exactly the call that base
    `b1f4ef92` knows how to answer, so the same code produces the literal and compares it.
    """
    store = Store.open_disposable_test_store()
    client = _client()
    _add(store, client, "Ancora lexical alvodourado presente.", "g:1", UNIT)
    _add(store, client, "Vizinho vetorial obliquo.", "g:2", OBLIQUE)
    _add(store, client, "Vizinho vetorial oposto.", "g:3", OPPOSITE)
    # The vector margin (`test_retrieve_vector_margin.py`) would cut g:2 and g:3;
    # this golden measures the two floors alone.
    return _call(store, client, query="alvodourado", vector_margin=None)


#: DELIBERATELY EXPANDED on 2026-09-12 by the `nmemory-alcance` campaign (U2a):
#: the envelope gained `lanes_run` — the lanes that EXECUTED. The golden is a
#: SHAPE contract, and expanding it is a conscious act, not an accident to
#: accommodate: before this, an `abstain` from a `lane="vector"` request returned
#: with reason "no stored capsule matched any of the N query term(s)" with the term
#: lane disabled, and the F1 blindness read as honest absence of data.
#: What the test PROVES did not change — that default floor `0.0` is a no-op — because
#: both halves are recomputed by the same code.
#: Captured on base `b1f4ef92` with `json.dumps(_golden_result(), sort_keys=True)`.
#: The serializer is NAMED because `f64` -> JSON is not unique (`1e-5` outputs
#: `"0.00001"` in one serialization and `1e-05` in another).
#: Capsule `g:3` (measured sim `-1.0`) is what gives strength to this golden: with the raw
#: retention check it would fall below default `0.0` and turn the golden red.
# U6: additive BM25 values measured; every pre-existing envelope value is unchanged.
# 2026-10-16: confidence left the order (see `decay_factor`), so each
# `relevance_score` is the bare RRF (0.9 confidence no longer scales it).
GOLDEN_BASE_ENVELOPE = (
    '{"embedding_usage": {"openai_prompt_tokens": 1, "openai_requests": 1}, "evidence": [{"anchor_drift": "unknown", "anchor_effective": null, "anchor_live": "unknown", "authority_class": "user-stated", "bm25": 0.9066488893385706, "confidence": 0.9, "content": "Ancora lexical alvodourado presente.", "created_at": "2026-09-02T12:00:00Z", "decayed_weight": 0.9, "handle": "fact-1-ancora-lexical-alvodourado-presente", "headline": "Ancora lexical alvodourado presente.", "id": "'
    + sample_capsule_id(1)
    + '", "instruction_taint": false, "matched_terms": ["alvodourado"], "relevance_score": 0.0328, "tier": "active", "valid_from": "2026-09-02T12:00:00Z", "valid_to": null}, {"anchor_drift": "unknown", "anchor_effective": null, "anchor_live": "unknown", "authority_class": "user-stated", "bm25": null, "confidence": 0.9, "content": "Vizinho vetorial obliquo.", "created_at": "2026-09-02T12:00:00Z", "decayed_weight": 0.9, "handle": "fact-2-vizinho-vetorial-obliquo", "headline": "Vizinho vetorial obliquo.", "id": "'
    + sample_capsule_id(2)
    + '", "instruction_taint": false, "matched_terms": [], "relevance_score": 0.0171, "tier": "active", "valid_from": "2026-09-02T12:00:00Z", "valid_to": null}, {"anchor_drift": "unknown", "anchor_effective": null, "anchor_live": "unknown", "authority_class": "user-stated", "bm25": null, "confidence": 0.9, "content": "Vizinho vetorial oposto.", "created_at": "2026-09-02T12:00:00Z", "decayed_weight": 0.9, "handle": "fact-3-vizinho-vetorial-oposto", "headline": "Vizinho vetorial oposto.", "id": "'
    + sample_capsule_id(3)
    + '", "instruction_taint": false, "matched_terms": [], "relevance_score": 0.0168, "tier": "active", "valid_from": "2026-09-02T12:00:00Z", "valid_to": null}], "exclusions": {}, "framing": "DATA", "label": "ADVISORY_NOT_AUTHORITY", "lane": "auto", "lane_used": "fused", "lanes_run": ["term", "vector"], "matched": 3, "outcome": "grounded", "receipt_id": null, "returned": 3, "token_budget": 100000, "total_candidates": 3, "trimmed": 0, "trimmed_by_budget": 0, "trimmed_by_limit": 0}'
)


def test_the_default_is_the_envelope_of_today() -> None:
    """Default `0.0` is a PROVED no-op against the baseline, not against itself."""
    assert json.dumps(_golden_result(), sort_keys=True) == GOLDEN_BASE_ENVELOPE, (
        "o default mudou o envelope da base"
    )


def test_floors_written_as_zero_are_the_same_envelope() -> None:
    """The explicit half: hand-written `0.0` reads identically to the omitted default."""
    store = Store.open_disposable_test_store()
    client = _client()
    _add(store, client, "Ancora lexical alvodourado presente.", "g:1", UNIT)
    _add(store, client, "Vizinho vetorial obliquo.", "g:2", OBLIQUE)
    _add(store, client, "Vizinho vetorial oposto.", "g:3", OPPOSITE)
    explicit = _call(
        store,
        client,
        query="alvodourado",
        term_floor=0.0,
        vector_floor=0.0,
        vector_margin=None,
    )
    assert json.dumps(explicit, sort_keys=True) == GOLDEN_BASE_ENVELOPE, explicit


# ---------------------------------------------------------------- the mechanism


def test_a_score_exactly_on_the_floor_is_retained() -> None:
    """`>=`, never `>`: with `>` a floor equal to the score would prune its own point."""
    store = Store.open_disposable_test_store()
    client = _client()
    _add(store, client, "Neighbour identical to the query vector.", "e:1", UNIT)

    res = _call(store, client, query="zzznadacasa", vector_floor=1.0)

    assert res["outcome"] == "grounded", (
        f"measured sim is exactly 1.0 and the floor is 1.0: `>=` keeps it. {res}"
    )
    assert res["exclusions"].get("below_relevance_floor") is None, res["exclusions"]


def test_clearing_either_lane_is_enough() -> None:
    """The floor is PER LANE: whatever passes the floor of its own lane survives.

    Both capsules match on term with BM25 below `term_floor`. Only one
    has cosine above `vector_floor`, and it is the only one that survives — with
    `and` in place of `or` both would survive, with inverted `or` neither.
    """
    store = Store.open_disposable_test_store()
    client = _client()
    _add(store, client, "the alvo comum um.", "b:1", UNIT)
    _add(store, client, "the alvo comum dois.", "b:2", OPPOSITE)
    floor = _bm25_of(store, "the") + 0.05

    res = _call(store, client, query="the", term_floor=floor, vector_floor=0.5)

    assert res["outcome"] == "grounded", res
    assert len(res["evidence"]) == 1, res
    assert res["evidence"][0]["content"] == "the alvo comum um.", res
    assert res["exclusions"].get("below_relevance_floor") == 1, res["exclusions"]


def test_the_floor_runs_before_the_vector_cap() -> None:
    """The ORDER, and the two counts that fix it.

    6 vector-only candidates: 4 below floor, 2 above, `vector_k=1`. With the
    floor BEFORE the cap: 4 in `below_relevance_floor`, 2 eligible, 1 pruned
    by the cap. With inverted order the cap would see 6 and prune 5.
    """
    store = Store.open_disposable_test_store()
    client = _client()
    for i in range(4):
        _add(store, client, f"Obliquo {i}.", f"o:{i}", OBLIQUE, dated=True)
    for i in range(2):
        _add(store, client, f"Identico {i}.", f"u:{i}", UNIT, dated=True)

    res = _call(
        store,
        client,
        query="zzznadacasa",
        vector_floor=0.5,
        vector_k=1,
        time_window=WINDOW,
    )

    assert res["exclusions"].get("below_relevance_floor") == 4, res["exclusions"]
    assert res["exclusions"].get("vector_k_capped") == 1, res["exclusions"]
    assert res["total_candidates"] == 6, res


def test_a_stronger_fence_keeps_its_own_name() -> None:
    """The floor is the LAST fence: whatever already dropped by confidence does not become floor.

    `min_confidence` runs immediately before. A capsule below BOTH
    bars counts ONCE, and counts under the stronger fence.
    """
    store = Store.open_disposable_test_store()
    client = _client()
    _add(store, client, "the sessao unica do registo.", "f:1", OBLIQUE)
    floor = _bm25_of(store, "the") + 0.05

    res = _call(
        store,
        client,
        query="the",
        term_floor=floor,
        vector_floor=0.5,
        min_confidence=0.99,
    )

    assert res["exclusions"].get("min_confidence") == 1, res["exclusions"]
    assert res["exclusions"].get("below_relevance_floor") is None, res["exclusions"]


# ---------------------------------------------------------------- the partition


#: One `project_id` per example isolates the shared store; opening a new store per
#: example would pay a new store file for each.
_EXAMPLE = itertools.count()

#: DISJOINT grids on purpose: the stored float32 rounds the embedding, so a
#: `sim` generated exactly on the floor would decide partition by rounding
#: noise rather than by the rule. The margin is 0.05, far above that error.
_SIMS = [round(0.1 * i, 2) for i in range(1, 10)]
_FLOORS = [round(0.1 * i + 0.05, 2) for i in range(1, 9)]


@settings(max_examples=25, deadline=None)
@given(
    sims=st.lists(st.sampled_from(_SIMS), min_size=1, max_size=8),
    floor=st.sampled_from(_FLOORS),
)
def test_every_lane_id_is_a_candidate_or_a_counted_exclusion(
    sims: list[float], floor: float
) -> None:
    """The universal invariant, hence a property rather than examples.

    No id returned by the lane evaporates: each is in evidence or in a
    COUNTED exclusion. It is the direct falsifier of the DoD — an id that becomes neither
    candidate NOR counted exclusion. The vector arm is chosen because
    `sim` is controllable by construction (`insert_embedding`), whereas BM25 is
    a function of the ENTIRE corpus and cannot be generated point-by-point.
    """
    store = _shared_store()
    client = _client()
    project = f"nmemory/prop/{next(_EXAMPLE)}"
    # One boundary for the seed: one write transaction per example, not per row.
    with store.db.tool_boundary():
        for i, sim in enumerate(sims):
            _add(
                store,
                client,
                # Content carries `project` because the store is addressed by
                # CONTENT: two examples with the same text would deduplicate to
                # the same capsule, and the second example would run over zero ids.
                f"Vizinho gerado {project} {i}.",
                f"p:{project}:{i}",
                [sim, math.sqrt(1.0 - sim * sim)] + [0.0] * (DIMENSION - 2),
                project_id=project,
            )

    res = retrieve(
        store,
        RetrieveParams(
            query="zzznadacasa",
            project_id=project,
            limit=100,
            token_budget=1_000_000,
            vector_k=50,
            vector_floor=floor,
        ),
        embeddings=client,
        now=NOW,
        record_receipt=False,
    )

    assert res["total_candidates"] == len(sims), res
    excluded = sum(res["exclusions"].values())
    assert len(res["evidence"]) + excluded == len(sims), (
        f"{len(sims)} ids left the vector lane; "
        f"{len(res['evidence'])} in evidence and {excluded} counted in "
        f"{res['exclusions']} — the rest evaporated"
    )
    assert res["exclusions"].get("below_relevance_floor", 0) == sum(
        1 for s in sims if s < floor
    ), res["exclusions"]
