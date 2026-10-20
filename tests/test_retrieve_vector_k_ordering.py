"""The two defects that independent review found in U0g.

**F1 — the time fence must run BEFORE the vector cap.** When giving effect to
`vector_k` (which #174 accepted and never read), I made the cap prune the RAW
cosine search. This inverts the order that two sources already declared:

    tools_schema.json, description of `vector_k` AT THE TIME (the sentence was removed in B3,
    because the "raw" path it promised ceased to exist):
      "an explicit `time_window` runs before this cap, while an omitted window
      preserves the historical raw cosine top-K path. Omitted → 10."

    PLAN.md:104, U0b — five lines above the U0g I was delivering:
      "`time_window` fences by valid time, pre-ranking, and before `vector_k`"

One sentence, two clauses: I implemented the second and made the first false. With
a declared window, a capsule INSIDE the window but outside the cosine top-10 was
no longer found — `grounded` turned into `missing_evidence`. This is not inherited
debt: it is debt ACTIVATED by this candidate.

**F2 — negative `vector_k` reached PostgreSQL.** The schema declares
`"minimum": 0` and the port did not check it, so `LIMIT -1` raised
`InvalidRowCountInLimitClause`, which the lane's broad `except` caught with a
`logger.debug`: the vector lane vanished silently and the caller was told
that no capsule matched. `weight_blend`, three lines above, already rejected
outside `0.0..=1.0`. And this candidate AGGRAVATED it by starting to accept
`min_confidence`, which the port previously rejected as an unknown key.
"""

from __future__ import annotations

import json
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
from nmemory.repositories.capsules import EventTimeRange
from nmemory.retrieve import RetrieveParams, retrieve
from nmemory.server import MemoryServer
from nmemory.space import DIMENSION
from nmemory.store import Store

NOW = datetime(2026, 9, 2, 12, 0, 0, tzinfo=UTC)
IN_WINDOW = datetime(2024, 6, 1, 12, 0, 0, tzinfo=UTC)

# Vectors with STRICTLY descending cosine, so ranking is
# deterministic and the "top-10" is a known set, never a tie.
SEEDED = 14
NEAR_TOP = 10


def _vec(i: int) -> list[float]:
    v = [0.0] * DIMENSION
    v[0] = 1.0
    v[1] = 0.01 * i
    return v


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


def _seeded() -> tuple[Store, EmbeddingClient]:
    """14 capsules; the 10 closest OUTSIDE the window, the remaining 4 INSIDE.

    This is the exact shape of the defect: what the caller wants is outside the cosine
    top-10, and only the window distinguishes it.
    """
    store = Store.open_disposable_test_store()
    client = EmbeddingClient(custom_hook=lambda text: _vec(0))
    for i in range(SEEDED):
        at = IN_WINDOW if i >= NEAR_TOP else NOW
        cid, _ = store.append_capture(
            _cap(f"Vizinho vetorial {i}.", f"vo:{i}"),
            None,
            EventTimeRange.point(at),
            None,
            NOW,
        )
        store.set_birth_classification(cid, "fact", "project", NOW)
        put_embedding(
            store.db,
            cid,
            _vec(i),
            client.space_fingerprint.as_str(),
            sha256_hex(cid.encode()),
        )
    return store, client


def _window_call(store: Store, client: EmbeddingClient, **kw: Any) -> dict[str, Any]:
    return retrieve(
        store,
        RetrieveParams(
            query="zzztermoqueninguemtem",
            project_id="nmemory",
            lane="vector",
            limit=SEEDED,
            token_budget=100_000,
            time_window={"from": "2024-01-01T00:00:00Z", "to": "2024-12-31T23:59:59Z"},
            **kw,
        ),
        embeddings=client,
        now=NOW,
    )


def test_time_window_runs_before_the_vector_cap() -> None:
    """RED: with the cap pruning the RAW cosine, the 4 in-window are never seen."""
    store, client = _seeded()
    res = _window_call(store, client)
    assert res["outcome"] == "grounded", (
        f"the 4 in-window capsules exist; the fence must run before the cap. "
        f"got {res['outcome']} with exclusions={res.get('exclusions')}"
    )
    assert len(res["evidence"]) == SEEDED - NEAR_TOP


def test_the_cap_still_applies_inside_the_window() -> None:
    """The correct order does not disable the cap: `vector_k=2` on 4 in-window yields 2."""
    store, client = _seeded()
    res = _window_call(store, client, vector_k=2)
    assert res["outcome"] == "grounded", res
    assert len(res["evidence"]) == 2, (
        f"vector_k=2 must cut inside the window; got {len(res['evidence'])}"
    )


def test_without_a_window_the_cap_still_bites() -> None:
    """Without a window, the cap still bites — but NO LONGER on raw cosine.

    Since B3, the SQL LIMIT is `max(v_limit, 50)` also without a window: the search
    fetches the same wide window and the cap prunes after late fences, as it already
    did with a window. What this test pins is that pruning happens, not where.
    """
    store, client = _seeded()
    res = retrieve(
        store,
        RetrieveParams(
            query="zzztermoqueninguemtem",
            project_id="nmemory",
            lane="vector",
            limit=SEEDED,
            vector_k=3,
            token_budget=100_000,
        ),
        embeddings=client,
        now=NOW,
    )
    assert res["outcome"] == "grounded", res
    assert len(res["evidence"]) == 3, res


def test_vector_k_zero_without_a_window_fences_the_lane_shut() -> None:
    """`vector_k=0` is a cap of ZERO, not "uncapped".

    Prior to B3, `0` reached SQL as `LIMIT 0` and the lane returned empty by
    accident of LIMIT. Now search fetches up to 50 rows and the POST-
    eligibility cap prunes them all — the observable result must be the
    same: zero vector evidence, and a term that matches nothing does not restore it.
    """
    store, client = _seeded()
    res = retrieve(
        store,
        RetrieveParams(
            query="zzztermoqueninguemtem",
            project_id="nmemory",
            lane="vector",
            limit=SEEDED,
            vector_k=0,
            token_budget=100_000,
        ),
        embeddings=client,
        now=NOW,
    )
    assert res["evidence"] == [], (
        f"vector_k=0 must close the lane; got {len(res['evidence'])}"
    )
    assert res["outcome"] != "grounded", res


# --------------------------------------------------------------------------
# F2 — the range declared in the schema is refused at the port
# --------------------------------------------------------------------------


def _door(args: dict[str, Any]) -> dict[str, Any]:
    store, client = _seeded()
    server = MemoryServer(
        store=store, embeddings=client, anchor_root=".", project_id="nmemory"
    )
    return server.call_tool("nmemory_capsule_retrieve", args, None, now=NOW)


def test_negative_vector_k_is_refused_at_the_door() -> None:
    """RED: `LIMIT -1` must not reach the store, where it means no limit at all."""
    res = _door({"terms": ["x"], "project_id": "nmemory", "vector_k": -1})
    assert "error" in res, (
        f"vector_k=-1 must be refused at the door (the schema says minimum 0); got {res}"
    )
    assert res["error"]["code"] == -32602


def test_min_confidence_outside_zero_one_is_refused() -> None:
    """This candidate began ACCEPTING min_confidence; it must fence it as well."""
    for bad in (-0.1, 1.5):
        res = _door({"terms": ["x"], "project_id": "nmemory", "min_confidence": bad})
        assert "error" in res, f"min_confidence={bad} tem de recusar; veio {res}"
        assert res["error"]["code"] == -32602


def test_the_valid_range_still_passes() -> None:
    """Refusal closes the illegal without closing the door to the legal."""
    res = _door(
        {"terms": ["x"], "project_id": "nmemory", "vector_k": 0, "min_confidence": 0.0}
    )
    assert "error" not in res, res
    payload = json.loads(res["result"]["content"][0]["text"])
    assert payload["outcome"] in ("grounded", "abstain", "missing_evidence"), payload
