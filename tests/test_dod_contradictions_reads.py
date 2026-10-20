"""`dod_contradictions` reads the edges of the capsule it judges, not the store's.

Every rendered capsule asks it — each `nmemory_capsule_show` and each row of a
`with_record` list page — and it used to decode every live relation of the store each
time: on the live store, one conversation replay rendered 14 records and decoded
110,630 relation rows. These cases pin what it answers and count what it reads.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from itertools import pairwise

import pytest
from storefile import fresh_store_path

from nmemory.capture import CaptureContext
from nmemory.queries.work import dod_contradictions
from nmemory.relation import RelationKind
from nmemory.repositories.relations import RelationRepository
from nmemory.server import MemoryServer
from nmemory.store import Store

T0 = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
CTX = CaptureContext(
    harness="pytest",
    model="none",
    cwd="/tmp",
    session_id="sess-dod",
    host="t",
    owner="t",
)


def _ingest(server: MemoryServer, content: str, kind: str) -> str:
    response = server.call_tool(
        "nmemory_capsule_ingest",
        {
            "content": content,
            "source": "t",
            "anchor": content[:16],
            "proof_hint": "fixture: re-obtainable proof",
            "kind": kind,
        },
        context=CTX,
        now=T0,
    )
    assert not response["result"]["isError"], response
    return str(
        json.loads(response["result"]["content"][0]["text"])["outcomes"][0]["id"]
    )


def _contradicted(
    witnessed: bool = True,
) -> tuple[Store, MemoryServer, str, str, str]:
    """An accepted DoD judged unmet whose task a live edge witnesses (unless
    `witnessed` is false), beside 120 edges of other capsules."""
    store = Store.open(fresh_store_path("dodreads"))
    server = MemoryServer(store=store, project_id="dodreads")
    dod = _ingest(server, "aardvark definition of done", "dod")
    task = _ingest(server, "badger task the dod governs", "task")
    witness = _ingest(server, "civet fact that witnesses the task", "fact")
    store.add_relation(RelationKind.DOD_OF, dod, task, now=T0)
    store.append_dod_event(
        capsule_id=dod,
        axis="status",
        value="accepted",
        actor="owner",
        reason="",
        now=T0,
    )
    if witnessed:
        store.add_relation(RelationKind.WITNESSES, witness, task, now=T0)
    others = [_ingest(server, f"dingo fact number {n}", "fact") for n in range(41)]
    for n, (left, right) in enumerate(pairwise(others)):
        for kind in (
            RelationKind.DERIVED_FROM,
            RelationKind.ABOUT,
            RelationKind.GROUNDED_IN,
        ):
            store.add_relation(kind, left, right, now=T0 + timedelta(seconds=n))
    return store, server, dod, task, witness


def test_both_ends_of_a_standing_contradiction_report_it() -> None:
    store, _, dod, task, _ = _contradicted()
    for capsule in (dod, task):
        [found] = dod_contradictions(store.db, capsule)
        assert (found.dod_id, found.target_id) == (dod, task)


def test_a_superseded_dod_or_an_unwitnessed_target_reports_none() -> None:
    unwitnessed, _, dod, task, _ = _contradicted(witnessed=False)
    assert dod_contradictions(unwitnessed.db, task) == []
    assert dod_contradictions(unwitnessed.db, dod) == []
    store, server, dod, task, _ = _contradicted()
    assert dod_contradictions(store.db, task)
    newer = _ingest(server, "the replacement definition of done", "dod")
    store.add_relation(RelationKind.SUPERSEDES, newer, dod, now=T0 + timedelta(hours=2))
    assert dod_contradictions(store.db, task) == []
    assert dod_contradictions(store.db, dod) == []


def test_a_superseded_target_reports_none_from_either_end() -> None:
    store, server, dod, task, _ = _contradicted()
    newer = _ingest(server, "the task that replaces the governed one", "task")
    store.add_relation(
        RelationKind.SUPERSEDES, newer, task, now=T0 + timedelta(hours=2)
    )
    assert dod_contradictions(store.db, task) == []
    assert dod_contradictions(store.db, dod) == []


def test_a_dod_that_supersedes_an_older_one_still_stands() -> None:
    store, server, dod, task, _ = _contradicted()
    older = _ingest(server, "the definition of done it replaced", "dod")
    store.add_relation(RelationKind.SUPERSEDES, dod, older, now=T0 + timedelta(hours=2))
    for capsule in (dod, task):
        [found] = dod_contradictions(store.db, capsule)
        assert (found.dod_id, found.target_id) == (dod, task)


def test_a_task_that_witnesses_something_else_is_not_witnessed() -> None:
    unwitnessed, server, dod, task, _ = _contradicted(witnessed=False)
    other = _ingest(server, "something the task itself witnesses", "fact")
    unwitnessed.add_relation(RelationKind.WITNESSES, task, other, now=T0)
    assert dod_contradictions(unwitnessed.db, task) == []
    assert dod_contradictions(unwitnessed.db, dod) == []


def test_a_revoked_supersede_stops_counting_until_it_is_asserted_again() -> None:
    store, server, dod, task, _ = _contradicted()
    newer = _ingest(server, "the task that replaces the governed one", "task")
    later = T0 + timedelta(hours=2)
    store.add_relation(RelationKind.SUPERSEDES, newer, task, now=later)
    assert dod_contradictions(store.db, task) == []
    assert store.revoke_relation(
        RelationKind.SUPERSEDES, newer, task, actor="t", reason="undo", now=later
    )
    for capsule in (dod, task):
        [found] = dod_contradictions(store.db, capsule)
        assert (found.dod_id, found.target_id) == (dod, task)
    store.add_relation(RelationKind.SUPERSEDES, newer, task, now=later)
    assert dod_contradictions(store.db, task) == []
    assert dod_contradictions(store.db, dod) == []


def test_a_rendered_capsule_reads_only_the_edges_that_touch_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Counted in calls and rows: the store's whole relation table is never read to
    render one capsule, and what is decoded is the edges around it."""
    _, server, dod, task, witness = _contradicted()
    whole: list[int] = []
    around: list[int] = []
    list_relations = RelationRepository.list_relations
    list_relations_for = RelationRepository.list_relations_for

    def counted_whole(self, **kwargs):  # type: ignore[no-untyped-def]
        rows = list_relations(self, **kwargs)
        whole.append(len(rows))
        return rows

    def counted_around(self, capsule_id, **kwargs):  # type: ignore[no-untyped-def]
        rows = list_relations_for(self, capsule_id, **kwargs)
        around.append(len(rows))
        return rows

    monkeypatch.setattr(RelationRepository, "list_relations", counted_whole)
    monkeypatch.setattr(RelationRepository, "list_relations_for", counted_around)
    for capsule in (dod, task, witness):
        response = server.call_tool("nmemory_capsule_show", {"id": capsule}, now=T0)
        assert not response["result"]["isError"], response
    assert whole == []
    # The task's three edges, the DoD's one and the witness's one, each read a small
    # number of times; 120 unrelated edges are never among them.
    assert sum(around) <= 20, around
