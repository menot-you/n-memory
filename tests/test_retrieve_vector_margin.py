"""The vector lane's relative floor: a vector-only candidate far below the query's best cosine is cut.

The defect: the vector lane had no floor, so every query's 50 nearest capsules
entered the ranking, and the vector-only ones filled the limit with unrelated
capsules (PrecisionMemBench axis A, lane auto: precision 0.066, against 0.157
for the term lane). The cut is counted under `below_vector_margin`, and a
capsule the term lane also matched survives by its own lane.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from storefile import put_embedding

from nmemory import retrieve as retrieve_module
from nmemory.capsule import (
    AuthorityClass,
    Capsule,
    Freshness,
    Provenance,
    Scope,
    sha256_hex,
)
from nmemory.embedding import EmbeddingClient
from nmemory.retrieve import InvalidVectorMargin, RetrieveParams, retrieve
from nmemory.space import DIMENSION
from nmemory.store import Store

NOW = datetime(2026, 9, 2, 12, 0, 0, tzinfo=UTC)
PROJECT = "nmemory"
UNIT: list[float] = [1.0] + [0.0] * (DIMENSION - 1)
TOP = 0.90
#: The measured margin (`retrieve.VECTOR_MARGIN`), pinned here by value.
VECTOR_MARGIN = 0.083


def _at(cosine: float) -> list[float]:
    """A unit vector whose cosine with UNIT is exactly `cosine`."""
    return [cosine, math.sqrt(1.0 - cosine * cosine)] + [0.0] * (DIMENSION - 2)


def _add(store: Store, client: EmbeddingClient, content: str, cosine: float) -> str:
    cap = Capsule(
        content=content,
        provenance=Provenance(
            source="test",
            anchor=content,
            source_hash=sha256_hex(content.encode("utf-8")),
        ),
        confidence=0.9,
        freshness=Freshness(valid_from=NOW),
        scope=Scope(project_id=PROJECT),
        authority_class=AuthorityClass.USER_STATED,
        instruction_taint=False,
    )
    cid, _ = store.append(cap, NOW)
    store.set_birth_classification(cid, "fact", "project", NOW)
    put_embedding(
        store.db,
        cid,
        _at(cosine),
        client.space_fingerprint.as_str(),
        sha256_hex(cid.encode()),
    )
    return cid


def _corpus() -> tuple[Store, EmbeddingClient, str, str]:
    """The best vector match, one inside the margin and six far below it.

    The six would fill `limit=5` on their own, which is the measured failure.
    """
    store = Store.open_disposable_test_store()
    client = EmbeddingClient(custom_hook=lambda text: list(UNIT))
    best = _add(store, client, "Alpha nearest neighbour.", TOP)
    near = _add(store, client, "Bravo close neighbour.", TOP - VECTOR_MARGIN / 2)
    for i in range(6):
        _add(store, client, f"Charlie far neighbour {i}.", TOP - 3 * VECTOR_MARGIN)
    return store, client, best, near


def _call(store: Store, client: EmbeddingClient, **kw: Any) -> dict[str, Any]:
    params: dict[str, Any] = {
        "query": "zuluquery",
        "project_id": PROJECT,
        "limit": 5,
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


def test_vector_only_candidates_below_the_margin_do_not_fill_the_limit() -> None:
    store, client, best, near = _corpus()

    res = _call(store, client)

    assert res["outcome"] == "grounded", res
    assert {e["id"] for e in res["evidence"]} == {best, near}, res["evidence"]
    assert res["exclusions"].get("below_vector_margin") == 6, res["exclusions"]


def test_a_term_match_survives_the_vector_margin() -> None:
    store, client, best, near = _corpus()
    lexical = _add(store, client, "Delta zuluquery far in vector space.", 0.1)

    res = _call(store, client, terms=["zuluquery"])

    assert {e["id"] for e in res["evidence"]} == {best, near, lexical}, res
    assert res["exclusions"].get("below_vector_margin") == 6, res["exclusions"]


def test_a_term_match_below_the_term_floor_still_survives_the_vector_margin() -> None:
    store, client, _best, _near = _corpus()
    lexical = _add(store, client, "Delta zuluquery far in vector space.", 0.1)

    res = _call(store, client, terms=["zuluquery"], term_floor=1e6)

    assert lexical in {e["id"] for e in res["evidence"]}, res


def test_a_stopword_only_term_match_does_not_bypass_the_vector_margin() -> None:
    """A candidate that matches only stop words when the query has content words
    must still clear the vector margin unless it clears a positive term floor,
    and when cut its exclusion is recorded as below_relevance_floor."""
    store, client, best, near = _corpus()
    _add(store, client, "Delta the is in far vector space.", 0.1)

    res = _call(store, client, query="the zuluquery", terms=["the", "zuluquery"])

    assert {e["id"] for e in res["evidence"]} == {best, near}, res
    assert res["exclusions"].get("below_vector_margin") == 6, res["exclusions"]
    assert res["exclusions"].get("below_relevance_floor") == 1, res["exclusions"]


def test_contraction_fragments_do_not_bypass_the_vector_margin() -> None:
    """Contraction tokens like `I'm` (`i`, `m`) are stop words and must not
    count as content-word matches that bypass VECTOR_MARGIN."""
    store, client, best, near = _corpus()
    _add(store, client, "I'm here today in far vector space.", 0.1)

    res = _call(store, client, query="I'm sesamoraro", terms=["I'm", "sesamoraro"])

    assert {e["id"] for e in res["evidence"]} == {best, near}, res
    assert res["exclusions"].get("below_vector_margin") == 6, res["exclusions"]
    assert res["exclusions"].get("below_relevance_floor") == 1, res["exclusions"]


def test_a_stopword_only_term_match_clearing_a_positive_term_floor_survives() -> None:
    """When the caller sets a positive term_floor and the stop-word match clears
    it, the candidate survives."""
    store, client, best, near = _corpus()
    stop_hit = _add(store, client, "Delta the is in far vector space.", 0.1)

    res = _call(
        store,
        client,
        query="the zuluquery",
        terms=["the", "zuluquery"],
        term_floor=0.01,
    )

    assert {e["id"] for e in res["evidence"]} == {best, near, stop_hit}, res


@pytest.mark.parametrize("margin", [-0.1, math.nan, math.inf])
def test_a_margin_that_is_not_a_finite_non_negative_number_is_refused(
    margin: float,
) -> None:
    with pytest.raises(InvalidVectorMargin):
        RetrieveParams(query="q", vector_margin=margin)


def test_no_margin_is_the_old_ranking() -> None:
    store, client, _best, _near = _corpus()

    res = _call(store, client, vector_margin=None)

    assert len(res["evidence"]) == 5, res
    assert "below_vector_margin" not in res["exclusions"], res["exclusions"]


def test_the_measured_margin_is_the_one_the_tool_states() -> None:
    schema = json.loads(
        (Path(retrieve_module.__file__).parent / "tools_schema.json").read_text()
    )
    text = json.dumps(schema)
    assert retrieve_module.VECTOR_MARGIN == VECTOR_MARGIN
    assert f"within {VECTOR_MARGIN} of the best eligible cosine" in text
