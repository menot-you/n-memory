"""ONE "closed task" rule, consumed across the four sites that decide it (C2a).

Closed = there exists an INCOMING `witnesses` or `supersedes` edge. A
`nmemory_outcome_record` DOES NOT close, by its own contract: the server returns "ADVISORY
observation record — NOT a witnessed close" and accepts `score: 0.0`.

Before this campaign three rules coexisted: `_open_task_ids` (server) already
used `supersedes ∪ witnesses`; `delivery_census` (store) and the curator census
(the curator census script, since removed) counted a row in `outcomes` as
a close — a task closes "by observation" in one place and remains open in another, and the
number the owner reads in SessionStart is not the same that DAG projects.

Each site has ITS OWN test on the SAME four-task fixture: reverting the
conversion of an isolated site turns its own test red and only its own. A helper test
with a broken call site stays green — a law measured in this repository.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from storefile import fresh_store_path

from nmemory.database import Database
from nmemory.queries.census import delivery_census
from nmemory.queries.work import open_work
from nmemory.relation import Dag, RelationKind, closed_task_ids
from nmemory.server import MemoryServer
from nmemory.store import Store
from nmemory.tools.common import open_task_ids

T0 = datetime(2026, 9, 15, 10, 0, tzinfo=UTC)
NOW = T0 + timedelta(hours=1)
SINCE_7D = NOW - timedelta(days=7)


def _fresh(prefix: str) -> tuple[Database, Store, MemoryServer]:
    url = fresh_store_path(prefix)
    pg = Database(url)
    store = Store.open(url)
    return pg, store, MemoryServer(store=store)


def _ingest(server: MemoryServer, content: str, kind: str, now: datetime) -> str:
    response = server.call_tool(
        "nmemory_capsule_ingest",
        {
            "content": content,
            "source": "test_work_closing_rule",
            "anchor": content[:20],
            "proof_hint": "fixture: a porta de captura exige prova re-obtenível",
            "project_id": "curator",
            "kind": kind,
        },
        now=now,
    )
    assert not response["result"]["isError"], response
    return str(
        json.loads(response["result"]["content"][0]["text"])["outcomes"][0]["id"]
    )


class FourTasks:
    """One with `outcome` only, one with `witnesses`, one with `supersedes`, one with nothing."""

    def __init__(self) -> None:
        self.pg, self.store, self.server = _fresh("work_closing_four")
        s, srv = self.store, self.server
        self.outcome_only = _ingest(srv, "closing task with outcome only", "task", T0)
        self.witnessed = _ingest(srv, "closing task witnessed", "task", T0)
        self.superseded = _ingest(srv, "closing task superseded", "task", T0)
        self.bare = _ingest(srv, "closing task bare", "task", T0)
        # The sink only exists for the DAG: the universe of `Dag` is that of `blocks`
        # edges, and a task outside it is not projected — neither closed nor open.
        self.sink = _ingest(srv, "closing task sink", "task", T0)
        # The fifth: a REVOKED `supersedes` (U1 only revokes falsifies/supersedes).
        # The two in-memory sites
        # receive only live edges from `list_relations`; the two in SQL must
        # write the `live_relation_sql()` fence or they diverge on the revocation
        # axis — the divergence that this campaign came to close (b3074d87).
        self.revoked = _ingest(
            srv, "closing task with a revoked supersedes", "task", T0
        )
        witness = _ingest(srv, "closing proof that witnessed delivered", "fact", T0)
        replacement = _ingest(srv, "closing task replacement", "task", T0)

        # Observation goes on the THREE that have something, not just the first: with it
        # only on `outcome_only`, the wrong rule (outcomes ∪ supersedes) and the
        # right one (witnesses ∪ supersedes) closed TWO each — equal counts,
        # vacuous assertion, measured in the first RED of this campaign. Thus, each
        # subset of {outcomes, witnesses, supersedes} closes a different
        # number and only the correct rule closes exactly two.
        for task in (self.outcome_only, self.witnessed, self.superseded):
            s.substrate.record_outcome(
                "observed: delivered", "judge", None, task, None, 0.0, T0
            )
        s.add_relation(RelationKind.WITNESSES, witness, self.witnessed, now=T0)
        revoked_replacement = _ingest(srv, "closing task wrong replacement", "task", T0)
        s.add_relation(
            RelationKind.SUPERSEDES,
            from_id=revoked_replacement,
            to_id=self.revoked,
            now=T0,
        )
        s.revoke_relation(
            RelationKind.SUPERSEDES,
            revoked_replacement,
            self.revoked,
            actor="judge",
            reason="a substituta era de outra task",
            now=T0,
        )
        s.add_relation(
            RelationKind.SUPERSEDES, from_id=replacement, to_id=self.superseded, now=T0
        )
        for task in self.four:
            s.add_relation(RelationKind.BLOCKS, task, self.sink, now=T0)

    @property
    def four(self) -> set[str]:
        return {
            self.outcome_only,
            self.witnessed,
            self.superseded,
            self.bare,
            self.revoked,
        }

    @property
    def expected_closed(self) -> set[str]:
        return {self.witnessed, self.superseded}


@pytest.fixture(scope="module")
def four(tmp_path_factory: pytest.TempPathFactory) -> Iterator[FourTasks]:
    # Module scope runs before the per-test `NOTT_DATABASE_DIR`: name one here.
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv(
            "NOTT_DATABASE_DIR", str(tmp_path_factory.mktemp("work-closing") / "db")
        )
        yield FourTasks()


def test_the_rule_itself_closes_by_witnesses_or_supersedes_only(
    four: FourTasks,
) -> None:
    closed = closed_task_ids(four.store.relations.list_relations())
    assert closed & four.four == four.expected_closed


def test_site_open_task_ids_closes_the_same_set(four: FourTasks) -> None:
    """PIN, not conversion: `open_task_ids` already decided by
    `witnesses`/`supersedes`, and reverting to the previous literal is a semantic
    no-op. What this test guarantees is that it continues to decide this way — and,
    with the fifth task, that a revoked edge does not close (`list_relations`
    returns only live ones, #492)."""
    live_by_id = {c.id: c for c in four.store.capsules.list()}
    open_ids = open_task_ids(
        four.four, live_by_id, four.store.relations.list_relations(), set(), NOW
    )
    assert four.four - open_ids == four.expected_closed


def test_site_dag_closes_the_same_set(four: FourTasks) -> None:
    """PIN, like the server's: `Dag` already read `WITNESSES`/`SUPERSEDES` and
    `supersedes ⊂ dead`, so the conversion is a semantic no-op. The real
    falsifier is in the two SQL sites, which counted `outcomes`."""
    dag = Dag.project_excluding(four.store.relations.list_relations(), set())
    closed = {t for t in four.four if dag.is_done(t) or not dag.is_live(t)}
    assert closed == four.expected_closed
    assert dag.is_done(four.witnessed) and not dag.is_live(four.superseded), (
        "dead beats done: the superseded one is never announced as done"
    )


def test_site_delivery_census_closes_the_same_set(four: FourTasks) -> None:
    work = delivery_census(four.store.db, SINCE_7D).work
    assert work.task_total == 8, "five, the sink and the two replacements"
    assert work.task_total - work.task_open == len(four.expected_closed), (
        f"the task with only an outcome ({four.outcome_only}) counts as open: "
        "an outcome is an advisory observation, not a witnessed closure"
    )


def test_work_never_expires_from_the_census_cohort() -> None:
    """A 30-day task remains on the work line; knowledge keeps 7 d."""
    _pg, store, server = _fresh("work_closing_cohort")
    old = _ingest(server, "closing old open task", "task", NOW - timedelta(days=30))
    old_fact = _ingest(server, "closing old fact", "fact", NOW - timedelta(days=30))
    census = delivery_census(store.db, SINCE_7D)
    assert census.work.task_total == 1 and census.work.task_open == 1, old
    assert census.knowledge == {}, (
        f"{old_fact} is outside the 7-day knowledge window and must stay out"
    )


def test_a_superseded_dod_is_not_without_verdict() -> None:
    """The machine forbids relabeling a verdict: a wrong DoD is REPLACED.

    The total continues to count them.
    """
    _pg, store, server = _fresh("work_closing_dod")
    wrong = _ingest(server, "closing dod that was wrong", "dod", T0)
    right = _ingest(server, "closing dod that replaced it", "dod", T0)
    store.add_relation(RelationKind.SUPERSEDES, from_id=right, to_id=wrong, now=T0)
    work = delivery_census(store.db, SINCE_7D).work
    assert work.dod_total == 2
    assert work.dod_without_verdict == 1, f"{wrong} was replaced, not left open"


def test_o_quinto_sitio_e_a_superficie_obrigatoria(tmp_path: Path) -> None:
    """C2b: open work reaches `nmemory_context_show`, which is the surface that
    a session reads BEFORE any other tool.

    Measured 2026-09-17 in the live store: 1434 open tasks out of 2062 and 407 dod without
    verdict out of 1112 — numbers that only existed in SessionStart and the census, so
    no one saw them while working. `inbox_candidates` excludes `task`/`dod` ON
    PURPOSE ("the work plan has its own consumer"); this is that
    consumer, and does not steal slots from the knowledge inbox.

    The rule is THE SAME as the other four sites: an `outcome` does not close, a
    live `witnesses`/`supersedes` edge closes, a revoked one does not close. Reverting
    the conversion ONLY here turns only this test red."""
    f = FourTasks()
    res = f.server.call_tool(
        "nmemory_context_show",
        {"query": "closing task", "project_id": "curator"},
        now=NOW,
    )
    assert not res["result"]["isError"], res
    body = json.loads(res["result"]["content"][0]["text"])
    work = body["work"]

    abertos = {row["id"] for row in work["open"]}
    assert f.outcome_only in abertos, (
        "an `outcome` is an advisory observation, it does not close"
    )
    assert f.revoked in abertos, "a REVOKED `supersedes` does not close (U1)"
    assert f.witnessed not in abertos, "a live witness closes"
    assert f.superseded not in abertos, "a live `supersedes` closes"
    # The total is EXACT even with the capped list: hiding the total was the defect
    # that made 1434 invisible.
    assert work["task_open"] >= 4
    assert work["dod_without_verdict"] == 0
    assert len(work["open"]) <= work["task_open"]


def test_a_lista_e_o_total_respondem_a_MESMA_pergunta() -> None:
    """Finding from independent review (`fugu-ultra`, 2026-09-17).

    The payload had TWO notions of closing side by side: the `open` list closes by the
    single rule (`witnesses` ∪ `supersedes`, with the liveness fence) and the
    total next to it was `dod_without_verdict`, which closes by `dod_events.axis =
    'verdict'`. A `dod` WITH a verdict and WITHOUT a live edge appeared in the list as
    open and disappeared from the total as closed — and since the list returns the oldest
    first, the 5 slots of the mandatory surface could all come from items that
    the total does not count. Measured in the live store: ~705 dod in that state, vs 407 in
    the total.

    The fix does not erase the metric: `dod_without_verdict` remains the
    diagnostic it was. What
    enters is `dod_open` — the total by the SAME rule as the list — so that whoever reads
    "5 of N" is comparing the same thing.
    """
    now = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
    _pg, store, server = _fresh("worksame")
    com_veredito = _ingest(server, "a dod already judged without a witness", "dod", now)
    sem_nada = _ingest(server, "a dod with no verdict at all", "dod", now)
    # The verdict fence requires the observation justifying it, and is correct:
    # classified with `evidence_state=observed` and a `proof_hint` beforehand.
    store.epistemics.set_epistemic(
        com_veredito,
        "observed",
        "pytest -q tests/test_work_closing_rule.py",
        None,
        now=now,
    )
    store.append_dod_event(
        com_veredito, "verdict", "failed", "owner", reason="nao passou", now=now
    )

    work = open_work(store.db, "curator", subtree=True, now=now, cap=10)
    ids = {row.id for row in work.rows}
    assert {com_veredito, sem_nada} <= ids, (
        "both are open under the single rule: neither has a closing edge"
    )
    assert work.dod_without_verdict == 1, "the diagnosis is still what it was"
    assert work.dod_open == 2, (
        "the total under the LIST rule must count both, otherwise the payload "
        "compares 2 items with a total of 1"
    )
    assert work.to_dict()["dod_open"] == 2
