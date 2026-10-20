"""Write→read with honest names: arrival, delivery, and what is missing (U3).

The old census said "N writes, M never recalled" and concealed three different
things under "never": what no one asked for but was delivered, what no one
asked for and no one delivered ALTHOUGH the audience opened a session, and what
no one could have read because no session for the scope existed. Measured in the
live store on 2026-09-15: the first read arrives with p50 of 4 days and p95 of 18,
so a 7-day window measures mostly "not yet".

The five states, per LIVE capsule (tier active, not superseded, not
falsified, without tombstone), in the order they resolve:

1. `asked`      — a QUESTION reached it (usage, or read_traffic in
                  ASKED_SURFACES: context/retrieve/get).
2. `delivered`  — arrived via DELIVERY (DELIVERED_SURFACES: inbox/bootstrap/
                  digest/list). Arrival is not a question, and the census reports which one it was.
3. `undelivered`— there was a session in the scope AFTER the write and it did not reach it.
                  This is the campaign's only target: `undelivered = 0`.
4. `no_session` — no one opened the scope since it was written. It is not
                  waste: it is absence of audience, and has another name.
5. `retired_unread` — left the stage (superseded/archived/tombstone) without ever
                  having been served. It is the waste of writing, visible.

"Scope session" is an event, never a number of days: a line
`read_traffic surface='context'` after `created_at` whose `project_id` is the
scope, or whose `project_prefix` is ancestor-or-equal to it — which is why
`nmemory_context_show` starts recording the prefix when the fence that ran was
subtree (U4).

The work plan (task/dod/epic/plan) is REPORTED, without a target: a DoD closes
when the work closes, not by an act of the memory server.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from storefile import fresh_store_path

from nmemory.database import Database
from nmemory.queries.census import delivery_census
from nmemory.relation import RelationKind
from nmemory.server import MemoryServer
from nmemory.store import Store

T0 = datetime(2026, 9, 15, 10, 0, tzinfo=UTC)
T_SESSION = T0 + timedelta(hours=1)
T_LATE = T0 + timedelta(hours=2)
SINCE = T0 - timedelta(days=7)


def _fresh() -> tuple[Database, Store, MemoryServer]:
    url = fresh_store_path("delivery_census")
    pg = Database(url)
    store = Store.open(url)
    return pg, store, MemoryServer(store=store)


def _ingest(
    server: MemoryServer,
    content: str,
    project: str,
    kind: str,
    now: datetime,
) -> str:
    response = server.call_tool(
        "nmemory_capsule_ingest",
        {
            "content": content,
            "source": "test_delivery_census",
            "anchor": content[:20],
            "proof_hint": "fixture: a porta de captura exige prova re-obtenível",
            "project_id": project,
            "kind": kind,
        },
        now=now,
    )
    assert not response["result"]["isError"], response
    import json

    return str(
        json.loads(response["result"]["content"][0]["text"])["outcomes"][0]["id"]
    )


def _serve(
    store: Store, surface: str, ids: list[str], scope: str, now: datetime
) -> None:
    store.db.record_read_traffic(
        surface=surface,
        session_id="sess-delivery",
        terms="probe",
        served_ids=ids,
        project_id=scope,
        project_prefix=None,
        outcome=None,
        now=now,
    )


def test_the_five_states_are_named_and_counted_apart() -> None:
    _pg, store, server = _fresh()
    asked = _ingest(server, "delivery asked observation", "curator", "fact", T0)
    delivered = _ingest(server, "delivery delivered observation", "curator", "fact", T0)
    undelivered = _ingest(server, "delivery pending observation", "curator", "fact", T0)
    retired = _ingest(server, "delivery retired observation", "curator", "fact", T0)

    # The question that reached `asked` is also the scope SESSION that makes
    # `undelivered` a delivery failure rather than an absence of audience.
    _serve(store, "context", [asked], "curator", T_SESSION)
    _serve(store, "digest", [delivered], "curator", T_SESSION)
    # The replacement is born BEFORE the session: remains live, unserved, with a scope
    # session afterward — so it is itself `undelivered`. One born AT the
    # instant of the session would be `no_session`, and the boundary has its own test.
    replacement = _ingest(server, "delivery replacement", "curator", "fact", T0)
    store.add_relation(
        RelationKind.SUPERSEDES, from_id=replacement, to_id=retired, now=T_SESSION
    )

    # Written AFTER the last session of the scope: no one could have read it.
    no_session = _ingest(server, "delivery late observation", "curator", "fact", T_LATE)

    census = delivery_census(store.db, SINCE)
    counts = census.knowledge["curator"]
    assert counts.asked == 1, asked
    assert counts.delivered == 1, delivered
    assert counts.undelivered == 2, (
        f"{undelivered} and the replacement were written before the session "
        "and nothing served them"
    )
    assert counts.no_session == 1, no_session
    assert counts.retired_unread == 1, retired


def test_the_session_boundary_has_a_line_on_each_side() -> None:
    """A capsule written AT the instant of the session could not have been delivered.

    The lesson cost two rounds in the previous campaign (`nmemory-escrita` DEBT
    line 6): a boundary clamp needs a line on each side of it,
    otherwise the `>=` mutant survives and `>` passes by accident.
    """
    _pg, store, server = _fresh()
    before = _ingest(
        server,
        "boundary just before the session",
        "curator",
        "fact",
        T_SESSION - timedelta(microseconds=1),
    )
    at = _ingest(server, "boundary at the session", "curator", "fact", T_SESSION)
    _serve(store, "context", [], "curator", T_SESSION)

    counts = delivery_census(store.db, SINCE).knowledge["curator"]
    assert counts.undelivered == 1, f"{before} was writable before the session"
    assert counts.no_session == 1, (
        f"{at} was born at the same instant — the session never had it"
    )


def test_a_subtree_session_counts_for_the_child_scope() -> None:
    """The session is an audience EVENT, and the audience is the tree (U4)."""
    _pg, store, server = _fresh()
    child = _ingest(server, "subtree child pending", "curator/sub", "fact", T0)

    store.db.record_read_traffic(
        surface="context",
        session_id="sess-delivery",
        terms="probe",
        served_ids=[],
        project_id="curator",
        project_prefix="curator",
        outcome=None,
        now=T_SESSION,
    )

    counts = delivery_census(store.db, SINCE).knowledge["curator/sub"]
    assert counts.undelivered == 1, child
    assert counts.no_session == 0, (
        "a subtree session on the parent IS a session for the child scope"
    )


def test_an_exact_session_on_the_parent_is_not_a_session_for_the_child() -> None:
    _pg, store, server = _fresh()
    _ingest(server, "exact parent pending", "curator/sub", "fact", T0)
    _serve(store, "context", [], "curator", T_SESSION)

    counts = delivery_census(store.db, SINCE).knowledge["curator/sub"]
    assert counts.no_session == 1
    assert counts.undelivered == 0, (
        "an EXACT fence on the parent never reached the child — the caller "
        "chose that fence, and the census says so"
    )


def test_the_work_plane_is_measured_by_its_own_consumer() -> None:
    _pg, store, server = _fresh()
    open_dod = _ingest(server, "work open dod", "curator", "dod", T0)
    closed_dod = _ingest(server, "work closed dod", "curator", "dod", T0)
    open_task = _ingest(server, "work open task", "curator", "task", T0)
    closed_task = _ingest(server, "work closed task", "curator", "task", T0)

    # `unmet` is the birth value and is not a legal transition — so the
    # existence of a verdict event IS the verdict, and the open DoD is
    # exactly the one that has none.
    # A verdict requires the observation justifying it (the store refuses without it) —
    # it is the same law that prevents the census from counting a closure by decree.
    store.epistemics.set_epistemic(
        closed_dod, "observed", "uv run --group dev pytest -q", None, T_SESSION
    )
    store.append_dod_event(closed_dod, "verdict", "met", "judge", "proved", T_SESSION)
    # A task closes via a `witnesses` (or `supersedes`) edge — the single rule
    # in `relation.closed_task_ids`. The outcome is recorded AND does not close: it is
    # an advisory observation by contract of `nmemory_outcome_record`.
    store.substrate.record_outcome(
        "the task delivered", "judge", None, closed_task, None, None, T_SESSION
    )
    store.add_relation(RelationKind.WITNESSES, closed_dod, closed_task, now=T_SESSION)

    work = delivery_census(store.db, SINCE).work
    assert work.dod_total == 2
    assert work.dod_without_verdict == 1, (
        f"{open_dod} never got a verdict event; its birth value is not one"
    )
    assert work.task_total == 2
    assert work.task_open == 1, open_task


def test_the_context_row_records_the_subtree_fence_it_ran() -> None:
    """Without this, the census does not distinguish a subtree session from an exact one."""
    pg, store, _server = _fresh()
    server_scoped = MemoryServer(store=store, project_id="curator")
    response = server_scoped.call_tool(
        "nmemory_context_show", {"query": "anything at all"}
    )
    assert not response["result"]["isError"], response

    with pg.writing() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT project_id, project_prefix FROM read_traffic "
            "WHERE surface = 'context' ORDER BY seq DESC LIMIT 1"
        )
        row = cur.fetchone()
    assert row == ("curator", "curator"), (
        "the default fence is a subtree fence, and the row says which one ran"
    )


def test_the_subtree_default_keeps_the_corpus_lane_fenced() -> None:
    """The audience fence must reach ALL lanes, not just two.

    The U4 default passes `project_id=None` to `execute_context` and the prefix
    on the side; the lexical and vector lanes received it and the CORPUS lane
    was left behind, without any fence — every default call served occurrences
    from all projects. No test caught it because all use
    `sources=["memory"]`. Caught by independent review.
    """
    from nmemory.capsule import sha256_hex
    from nmemory.repositories.corpus import (
        CorpusChunkInput,
        CorpusOccurrence,
        CorpusSourceRecord,
    )

    _pg, store, _server = _fresh()
    scoped = MemoryServer(store=store, project_id="curator")
    for project, text in (
        ("curator", "corpusfence own project paragraph"),
        ("outro", "corpusfence foreign project paragraph"),
    ):
        content = f"{text}\n"
        store.corpus.replace_corpus_source(
            CorpusSourceRecord(
                source_key=f"markdown:{project}",
                kind="markdown",
                root=f"/tmp/{project}",
                revision=f"rev-{project}",
                indexed_at="2026-09-15T10:00:00+00:00",
                status="ready",
            ),
            [
                CorpusChunkInput(
                    content_hash=sha256_hex(content.encode("utf-8")),
                    content=content,
                    occurrence=CorpusOccurrence(
                        source_key=f"markdown:{project}",
                        project_id=project,
                        path=f"{project}.md",
                        start_line=1,
                        end_line=1,
                        revision=f"rev-{project}",
                        provenance="test",
                    ),
                )
            ],
        )

    response = scoped.call_tool(
        "nmemory_context_show", {"query": "corpusfence", "sources": ["markdown"]}
    )
    assert not response["result"]["isError"], response
    body = response["result"]["content"][0]["text"]
    assert "own project" in body, "the audience's own corpus still arrives"
    assert "foreign project" not in body, (
        "the subtree default must fence the corpus lane too, or the audience "
        "fence leaks every other project's files"
    )


def test_a_session_instant_is_converted_once_whatever_the_number_of_scopes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One more session costs one `ts()` call, never one per scope.

    The hook's `digest show` runs under `timeout 5`. Measured on 2026-09-26 in the
    live store, the census spent 8.1 of 8.4 s in 2.35 M calls of the Python `ts()`:
    the per-scope subquery converted every subtree session's instant again for each
    of the scopes below it, so the hook's memory block never printed.
    """
    from nmemory import database

    calls = 0
    real_ts = database.ts

    def counting_ts(text: str | None) -> int | None:
        nonlocal calls
        calls += 1
        return real_ts(text)

    monkeypatch.setattr(database, "ts", counting_ts)
    _pg, store, server = _fresh()
    scopes = [f"fleet/p{i:02d}" for i in range(12)]
    for scope in scopes:
        _ingest(server, f"pending in {scope}", scope, "fact", T0)

    def subtree_sessions(count: int, start: datetime) -> None:
        for i in range(count):
            store.db.record_read_traffic(
                surface="context",
                session_id=f"sess-{start.isoformat()}-{i}",
                terms="probe",
                served_ids=[],
                project_id="fleet",
                project_prefix="fleet",
                outcome=None,
                now=start + timedelta(minutes=i),
            )

    def census_calls() -> int:
        before = calls
        knowledge = delivery_census(store.db, SINCE).knowledge
        assert all(knowledge[s].undelivered == 1 for s in scopes), knowledge
        return calls - before

    sessions = 10
    subtree_sessions(sessions, T_SESSION)
    first = census_calls()
    subtree_sessions(sessions, T_LATE)
    added = census_calls() - first

    assert added == sessions, (
        f"{sessions} more subtree sessions cost {added} conversions over "
        f"{len(scopes)} scopes: each session's instant must be converted once"
    )
