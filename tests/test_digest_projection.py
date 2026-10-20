"""What `nmemory_digest_show` PROJECTS: census, DAG, and archival candidates.

Upstream implementation: `src/server/digest.rs` + `src/server/projection.rs` +
`src/relation.rs`.

- census: a `staged` proposal is INTENT, not truth — removed from `total`,
  `by_project`, `newest`, `most_recalled`, and `pinned` (`digest.rs:91-101`); and
  `by_project.count` counts live + superseded while `live` is only the subset
  still standing (`digest.rs:123-142`);
- `dag`: readiness projection over `blocks` edges, with tombstoned and
  superseded dead to it, `witnesses` closing as DONE, and FAILING CLOSED
  on a live cycle;
- `archive_candidates`: how many records the consolidation planner would propose
  archiving, always alongside `archive_scanned`.
"""
#: `projetodeteste` and not `nmemory`: since 2026-09-19 `nmemory` is in
#: `PROJECT_ALIASES` (owner decision) and folds to `pynott`. A fixture that
#: seeds RAW (`Store.append`) and reads through the DOOR (which canonicalizes)
#: would fail to find itself — fixture asymmetry, not product: a real session
#: writes via ingest, which folds, and reads through the door, which also folds.

from __future__ import annotations

import json
from typing import Any

from storefile import fresh_store_path, sample_capsule_id

from nmemory.server import MemoryServer
from nmemory.store import Store


def _lane_database_url() -> str:
    """A single owner creates the run database and registers the name for cleanup."""
    return fresh_store_path("digestproj")


def _fresh_server() -> MemoryServer:
    lane_url = _lane_database_url()
    return MemoryServer(store=Store.open(lane_url))


def _ok(res: dict[str, Any]) -> dict[str, Any]:
    assert "result" in res and res["result"]["isError"] is False, res
    payload: dict[str, Any] = json.loads(res["result"]["content"][0]["text"])
    return payload


def _item(n: int, **extra: Any) -> dict[str, Any]:
    return {
        "content": f"Digest projection capsule number {n}.",
        "kind": "fact",
        "source": "session:test-digest-projection",
        "anchor": f"PLAN.md:{n}",
        "confidence": 0.9,
        "project_id": "projetodeteste",
        **extra,
    }


def _ingest(server: MemoryServer, *items: dict[str, Any]) -> dict[str, Any]:
    return _ok(server.call_tool("nmemory_capsule_ingest", {"items": list(items)}))


def _digest(server: MemoryServer, **args: Any) -> dict[str, Any]:
    return _ok(server.call_tool("nmemory_digest_show", args))


def test_a_staged_proposal_leaves_the_census() -> None:
    server = _fresh_server()
    _ingest(server, _item(1), _item(2), _item(3, staged=True))

    digest = _digest(server)

    assert digest["total"] == 2, (
        "a staged proposal is intent, not truth: the oracle carries it only in the "
        f"`staged` section and takes it out of the total — got {digest['total']}"
    )
    assert digest["by_project"] == [
        {"project_id": "projetodeteste", "count": 2, "live": 2}
    ], digest["by_project"]
    assert [row["id"] for row in digest["newest"]] == [
        sample_capsule_id(2),
        "cap-1",
    ], digest["newest"]
    # `tiers` does NOT take the fence: in upstream it is store-global (`list_by_tier`),
    # so the proposal continues to be counted there.
    assert digest["tiers"] == {"active": 3, "archived": 0, "quarantined": 0}, digest[
        "tiers"
    ]


def test_by_project_live_is_the_still_standing_subset() -> None:
    server = _fresh_server()
    _ingest(server, _item(1))
    _ingest(server, _item(2, supersedes="cap-1"))

    digest = _digest(server)

    assert digest["by_project"] == [
        {"project_id": "projetodeteste", "count": 2, "live": 1}
    ], (
        "`count` keeps parity with the export (live + superseded) and `live` "
        f"names the standing subset: {digest['by_project']}"
    )
    assert digest["total"] == 2, digest["total"]


def _relate(server: MemoryServer, kind: str, src: str, dst: str) -> None:
    res = server.call_tool(
        "nmemory_relation_add", {"kind": kind, "from": src, "to": dst}
    )
    assert "result" in res and res["result"]["isError"] is False, res


def test_dag_names_ready_blocked_and_done() -> None:
    server = _fresh_server()
    _ingest(
        server,
        _item(1, kind="task"),
        _item(2, kind="task"),
        _item(3, kind="task"),
        _item(4, kind="task"),
        _item(5, kind="task"),
        _item(6),
    )
    _relate(server, "blocks", "cap-1", sample_capsule_id(2))
    _relate(server, "blocks", sample_capsule_id(3), sample_capsule_id(4))
    _relate(server, "witnesses", sample_capsule_id(6), sample_capsule_id(3))

    dag = _digest(server)["dag"]

    assert dag["status"] == "ok", dag
    assert dag["ready"] == ["cap-1", sample_capsule_id(4), sample_capsule_id(5)], dag
    assert dag["ready_total"] == 3, dag
    assert dag["blocked"] == [sample_capsule_id(2)], dag
    assert dag["blocked_total"] == 1, dag
    assert dag["done"] == [sample_capsule_id(3)], dag
    assert dag["done_total"] == 1, dag
    assert "legacy_blocks_total" not in dag, dag


def test_dag_id_lists_are_capped_and_totals_stay_exact() -> None:
    server = _fresh_server()
    _ingest(server, *[_item(i, kind="task") for i in range(1, 6)])

    dag = _digest(server, headlines=2)["dag"]

    assert dag["ready"] == ["cap-1", sample_capsule_id(2)], dag
    assert dag["ready_total"] == 5, dag


def test_dag_fails_closed_on_a_live_blocks_cycle() -> None:
    server = _fresh_server()
    _ingest(server, _item(1, kind="task"), _item(2, kind="task"))
    _relate(server, "blocks", "cap-1", sample_capsule_id(2))
    _relate(server, "blocks", sample_capsule_id(2), "cap-1")

    dag = _digest(server)["dag"]

    assert dag["status"] == "cycle", (
        f"a live cycle cannot turn into an invented ready/blocked answer: {dag}"
    )
    assert dag["cycle"] == ["cap-1", sample_capsule_id(2)], dag
    assert dag["entangled_total"] == 2, dag
    assert "ready" not in dag, dag


def test_a_pre_contract_blocks_row_is_inert_and_counted() -> None:
    """A `blocks` edge with an endpoint outside `task` stays in history, inert."""
    server = _fresh_server()
    _ingest(server, _item(1, kind="task"), _item(2))
    with server.store.db.writing() as conn:
        conn.execute(
            "INSERT INTO relations (kind, from_id, to_id, at, origin) VALUES ('blocks', '"
            + sample_capsule_id(1)
            + "', '"
            + sample_capsule_id(2)
            + "', '2026-08-28T00:00:00Z', 'manual')"
        )

    dag = _digest(server)["dag"]

    assert dag["legacy_blocks_total"] == 1, dag
    assert dag["blocked"] == [], dag
    assert dag["ready"] == ["cap-1"], dag


def test_archive_candidates_counts_what_the_planner_would_archive() -> None:
    server = _fresh_server()
    _ingest(
        server,
        _item(1, valid_from="2020-01-01T00:00:00Z", valid_to="2020-06-01T00:00:00Z"),
    )
    _ingest(server, _item(2, supersedes="cap-1"))

    digest = _digest(server)

    assert digest["archive_candidates"] == 1, (
        "cap-1 is superseded, expired and active — exactly the protective "
        f"move the planner would propose: {digest['archive_candidates']}"
    )
    assert digest["archive_scanned"] == 2, digest["archive_scanned"]


def _fenced_store() -> MemoryServer:
    """4 capsules in `fenced`, 3 in `fenced/sub`, 5 in `outside`, and one miss."""
    server = _fresh_server()
    for project, count in (("fenced", 4), ("fenced/sub", 3), ("outside", 5)):
        _ingest(
            server,
            *[
                _item(i, project_id=project, content=f"Capsula {i} de {project}.")
                for i in range(count)
            ],
        )
    # A recall-miss so that `recent_failures` exists in the unfenced digest —
    # without it the retention test would be vacuous on both sides.
    _ok(server.call_tool("nmemory_capsule_retrieve", {"terms": ["zzzznaoexisteaqui"]}))
    return server


def test_project_prefix_fences_the_capsule_sections_and_nothing_else() -> None:
    """The fence is subtree-based and only applies to what DESCRIBES capsules.

    `digest.rs:85` passes the prefix to the `ListFilter` of `all`, and `global_snapshot`
    remains outside it: tiers, dag and archive read the entire store.
    """
    server = _fenced_store()
    pinned_outside = next(
        c
        for c in server.store.capsules.list()
        if c.capsule.scope.project_id == "outside"
    )
    _ok(
        server.call_tool(
            "nmemory_capsule_pin",
            {"id": pinned_outside.id, "pinned": True, "reason": "fora da cerca"},
        )
    )

    digest = _digest(server, project_prefix="fenced")

    assert digest["total"] == 7, (
        f"the fence takes `fenced` AND `fenced/sub`, never `outside`: {digest['total']}"
    )
    assert {row["project_id"] for row in digest["by_project"]} == {
        "fenced",
        "fenced/sub",
    }, digest["by_project"]
    for entry in digest["newest"]:
        assert entry["project_id"].startswith("fenced"), entry
    assert "pinned" not in digest, (
        "the only pinned capsule is in `outside`: the `pinned` section is fenced "
        f"like the other four, and should vanish — {digest.get('pinned')}"
    )

    # The store-global sections do NOT move with the fence.
    tiers = digest["tiers"]
    assert tiers["active"] == 12, f"tiers e store-global, nao cercado: {tiers}"
    assert digest["archive_scanned"] == 12, digest["archive_scanned"]

    unfenced = _digest(server)
    assert unfenced["total"] == 12, unfenced["total"]
    assert unfenced["tiers"] == tiers, "a cerca mexeu numa secao global"


def test_project_prefix_withholds_the_miss_detail_but_not_its_count() -> None:
    """A miss line does not carry a project: under the fence it would leak.

    Upstream WITHHOLDS `recent_failures` under prefix (`digest.rs:326`) and keeps
    the `recall_misses` count, which carries no query text.
    """
    server = _fenced_store()

    unfenced = _digest(server)
    assert unfenced["recall_misses"] == 1, unfenced["recall_misses"]
    assert unfenced["recent_failures"]["recall_misses"], (
        "without a fence the miss detail is served, otherwise the test is vacuous"
    )

    digest = _digest(server, project_prefix="fenced")
    assert digest["recall_misses"] == 1, (
        f"the COUNT is store-global and carries no text: {digest['recall_misses']}"
    )
    assert "recent_failures" not in digest, (
        "the miss DETAIL carries the queried term and has no project: under the "
        f"fence it is withheld — {digest.get('recent_failures')}"
    )
