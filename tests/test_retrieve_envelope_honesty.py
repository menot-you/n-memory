"""B2 — the `nmemory_capsule_retrieve` envelope reports the ENGINE, never constants.

#174 made the engine obey the caller: `retrieve.py` began honoring
`token_budget` and routing by `lane`. And it left the MCP envelope serving the
same literals it served before — `"lane": "term"`, `"token_budget": 1500`,
`"trimmed": 0` — which were TRUE while the engine practiced them and became
false the instant it stopped practicing them.

This is the missing half of the honest port: obeying the caller and then lying
about what was done is not obeying. A consumer reading `lane_used` to
know whether the vector index ran, or `token_budget` to decide whether it can request
more, reads a constant written in 2026.

Each test here is the falsifier for ONE field. None of them dies from the same
mutation, and that is why there are five and not one.
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
from nmemory.server import MemoryServer
from nmemory.space import DIMENSION
from nmemory.store import Store

NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)
UNIT_VEC: list[float] = [1.0] + [0.0] * (DIMENSION - 1)

TERM = "sincronizacao"
# Intentionally long content: ~40 tokens per capsule via the engine's
# approx_tokens (chars/4), so that a small budget cuts in an observable manner.
BODY = f"Note on {TERM} of distributed clocks in a ring with measured drift. " * 2


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


def _server(n: int, *, with_vectors: bool) -> MemoryServer:
    store = Store.open_disposable_test_store()
    client = EmbeddingClient(custom_hook=lambda text: list(UNIT_VEC))
    for i in range(n):
        cid, _ = store.append(_cap(BODY + f" #{i}", f"e:{i}"), NOW)
        store.set_birth_classification(cid, "fact", "project", NOW)
        if with_vectors:
            put_embedding(
                store.db,
                cid,
                list(UNIT_VEC),
                client.space_fingerprint.as_str(),
                sha256_hex(cid.encode("utf-8")),
            )
    return MemoryServer(
        store=store,
        embeddings=client if with_vectors else None,
        anchor_root=".",
        project_id="projetodeteste",
    )


def _call(server: MemoryServer, args: dict[str, Any]) -> dict[str, Any]:
    res = server.call_tool("nmemory_capsule_retrieve", args, None, now=NOW)
    assert "error" not in res, res
    payload: dict[str, Any] = json.loads(res["result"]["content"][0]["text"])
    return payload


def test_token_budget_echoes_what_the_caller_declared() -> None:
    """RED: the envelope returns 1500 to a caller that requested 100000."""
    server = _server(4, with_vectors=False)
    p = _call(
        server,
        {"terms": [TERM], "project_id": "projetodeteste", "token_budget": 100_000},
    )
    assert p["outcome"] == "grounded", p
    assert p["token_budget"] == 100_000, (
        f"the envelope must report the EFFECTIVE budget; it reported {p['token_budget']}"
    )


def test_token_budget_echoes_the_default_when_omitted() -> None:
    """Omitted, the effective budget IS 1500 — and there the number is true.

    Companion to the test above: proves that the fix is not "swapping 1500 for
    another constant", it is "reporting what the engine actually practiced".
    """
    server = _server(4, with_vectors=False)
    p = _call(server, {"terms": [TERM], "project_id": "projetodeteste"})
    assert p["token_budget"] == 1500, p


def test_lane_used_names_the_lane_that_actually_ran() -> None:
    """RED: caller requests `vector`, the vector lane produces, the envelope says `term`."""
    server = _server(4, with_vectors=True)
    p = _call(
        server,
        {
            "terms": ["zzztermoqueninguemtem"],
            "project_id": "projetodeteste",
            "lane": "vector",
            "token_budget": 100_000,
        },
    )
    assert p["outcome"] == "grounded", p
    assert p["lane"] == "vector", (
        f"`lane` is what the caller asked for; got {p['lane']}"
    )
    assert p["lane_used"] == "vector", (
        f"the vector lane produced these results; `lane_used` says {p['lane_used']}"
    )


def test_lane_used_stays_term_when_no_vector_index_exists() -> None:
    """Without a vector index, `term` is the truth — the fence does not invent `vector`."""
    server = _server(4, with_vectors=False)
    p = _call(server, {"terms": [TERM], "project_id": "projetodeteste"})
    assert p["lane_used"] == "term", p


def test_matched_counts_before_the_trim_and_trimmed_by_limit_counts_the_cut() -> None:
    """RED: `matched` is `len(results)`, so it never differs from `returned`.

    With 9 eligible and `limit=3`, an honest envelope says matched=9,
    returned=3, trimmed_by_limit=6. #174's envelope says 3, 3, 0.
    """
    server = _server(9, with_vectors=False)
    p = _call(
        server,
        {
            "terms": [TERM],
            "project_id": "projetodeteste",
            "limit": 3,
            "token_budget": 100_000,
        },
    )
    assert p["outcome"] == "grounded", p
    assert p["returned"] == 3, p
    assert p["matched"] == 9, (
        f"9 cápsulas passaram as cercas; `matched` reportou {p['matched']}"
    )
    assert p["trimmed_by_limit"] == 6, p
    assert p["trimmed_by_budget"] == 0, p
    assert p["trimmed"] == 6, p


def test_trimmed_by_budget_counts_what_the_budget_cut() -> None:
    """RED: a budget that truncates reports `trimmed_by_budget: 0`."""
    server = _server(9, with_vectors=False)
    p = _call(
        server,
        {
            "terms": [TERM],
            "project_id": "projetodeteste",
            "limit": 9,
            "token_budget": 40,
        },
    )
    assert p["outcome"] == "grounded", p
    assert p["returned"] < 9, p
    assert p["trimmed_by_budget"] > 0, (
        "o budget cortou resultados e o envelope reportou trimmed_by_budget=0"
    )
    assert p["trimmed"] == p["matched"] - p["returned"], p


def test_missing_evidence_also_names_the_lane_that_ran() -> None:
    """The `missing_evidence` envelope derived `term` as a literal.

    Here the vector lane PRODUCES candidates and all are fenced afterward, so
    the rank maps are not empty and `lane_used` has a meaningful value to report —
    unlike `abstain`, where both are empty by definition.
    """
    store = Store.open_disposable_test_store()
    client = EmbeddingClient(custom_hook=lambda text: list(UNIT_VEC))
    cid, _ = store.append(_cap(BODY, "me:1"), NOW)
    store.set_birth_classification(cid, "fact", "project", NOW)
    put_embedding(
        store.db,
        cid,
        list(UNIT_VEC),
        client.space_fingerprint.as_str(),
        sha256_hex(b"x"),
    )
    # Fenced by confidence: enters as vector lane candidate and is
    # excluded afterward — the path separating missing_evidence from abstain.
    server = MemoryServer(
        store=store, embeddings=client, anchor_root=".", project_id="projetodeteste"
    )
    p = _call(
        server,
        {
            "terms": ["zzztermoqueninguemtem"],
            "project_id": "projetodeteste",
            "lane": "vector",
            "min_confidence": 1.0,
        },
    )
    assert p["outcome"] == "missing_evidence", p
    assert p["lane_used"] == "vector", (
        f"the vector lane brought the candidate that was fenced; "
        f"`lane_used` says {p['lane_used']}"
    )


def test_a_long_item_does_not_close_the_budget_for_the_items_after_it() -> None:
    """RED: the first item that did not fit closed the budget for every later item.

    Measured 2026-10-16: with the default 1500-token block the median recall held
    one capsule, so a rule ranked below one long capsule never reached the
    session. Packing skips an item that does not fit and continues.
    """
    from types import SimpleNamespace
    from typing import cast

    from nmemory.retrieve import Ranked, trim_to_budget

    def item(chars: int) -> Ranked:
        return cast(
            Ranked,
            (SimpleNamespace(capsule=SimpleNamespace(content="x" * chars)), 1.0, 1.0),
        )

    short, long = 16, 400  # 4 and 100 tokens
    items = [item(short), item(long), item(short), item(long), item(8)]
    kept, by_limit, by_budget = trim_to_budget(items, 10, 10)
    assert kept == [items[0], items[2], items[4]]
    assert (by_limit, by_budget) == (0, 2)
    kept, by_limit, by_budget = trim_to_budget(items, 2, 10)
    assert kept == [items[0], items[2]]
    assert (by_limit, by_budget) == (2, 1)
    # A long first item is skipped like any other: costs [100, 4, 2] keep [4, 2].
    first_long = [item(long), item(short), item(8)]
    assert trim_to_budget(first_long, 10, 10) == (first_long[1:], 0, 1)
    # Only when no item fits does the engine serve the first one, so a grounded
    # answer is never empty. A zero budget still serves nothing.
    none_fit = [item(long), item(long)]
    assert trim_to_budget(none_fit, 10, 10) == (none_fit[:1], 0, 1)
    assert trim_to_budget(items, 10, 0) == ([], 0, 5)


def test_the_default_budget_serves_the_match_ranked_after_a_long_capsule() -> None:
    """RED through the tool: at the default 1500 tokens, a long capsule ranked second
    closed the block, so the short match ranked third never came back."""
    store = Store.open_disposable_test_store()
    top, _ = store.append(_cap("Note on alvorada and bravura together.", "e:a"), NOW)
    long, _ = store.append(_cap("alvorada " * 6000, "e:long"), NOW)
    third, _ = store.append(_cap("A short note on alvorada.", "e:c"), NOW)
    for cid in (top, long, third):
        store.set_birth_classification(cid, "fact", "project", NOW)
    server = MemoryServer(store=store, anchor_root=".", project_id="projetodeteste")
    p = _call(
        server, {"terms": ["alvorada", "bravura"], "project_id": "projetodeteste"}
    )
    assert p["outcome"] == "grounded", p
    assert p["token_budget"] == 1500, p
    assert [r["id"] for r in p["results"]] == [top, third], p
    assert (p["trimmed_by_budget"], p["trimmed"]) == (1, 1), p
