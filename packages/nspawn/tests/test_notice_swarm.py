"""A swarm: the runs of one dispatch give their caller ONE notice.

Owner order 2026-10-20: the notices of one dispatch arrive all at once. A run
whose group is `swarm:<name>` holds its notice while another run of that swarm,
dispatched by the same caller, is not terminal. The last member to end sends one
notice that names every member. A run with no swarm is notified as before.
"""

from __future__ import annotations

import os
import re
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import nott_database
import pytest
from registry_reset import sql
from supervisor_support import (
    _drain_worker_threads,
    _stop_every_reaper,
    registry,
    roster_home,
)
from test_driver import _wait_for
from test_notice_outbox import (
    PROJECT,
    Session,
    _dead_caller,
    _drain,
    _lapse,
    _notice,
    _rid,
    sessions,
)
from test_review_failover import (
    _broken,
    _completed,
    _fenced,
    _refused,
    _roster,
    _successor,
)

import nspawn.registry as store_module
from nspawn import mcp, native, notify, supervisor, terminal
from nspawn.registry import NoticeCaller, Registry
from nspawn.rows import Backend, RunPhase, RunRow
from nspawn.supervisor import Supervisor

__all__ = [
    "_drain_worker_threads",
    "_stop_every_reaper",
    "registry",
    "roster_home",
    "sessions",
]

A, B = _rid("swarm-a"), _rid("swarm-b")
SWARM = "swarm:review-1"


def _admit(
    store: Registry, run_id: str, caller: NoticeCaller, group: str | None
) -> None:
    store.heartbeat_instance(store_module.now_ms())
    store.admit_run(
        RunRow(
            run_id=run_id,
            worktree=f"/fixture/{run_id}",
            group_name=group,
            phase=RunPhase.PENDING,
            sandbox="read-only",
            model="m",
            owner_instance=store.instance_id,
            created_at=1000,
            updated_at=1000,
            backend=Backend.CLAUDE,
        ),
        False,
        1000,
        60_000,
        notice=caller,
    )


def _bodies(session: Session, count: int) -> list[str]:
    """The bodies the session recorded, once `count` of them have landed."""
    _wait_for(lambda: len(session.frames()) >= count, 5, f"{count} notice(s)")
    return session.bodies()


def _end(store: Registry, run_id: str, phase: RunPhase = RunPhase.COMPLETED) -> None:
    store.heartbeat_instance(store_module.now_ms())
    for step in (RunPhase.RUNNING, phase):
        row = store.get_run(run_id)
        assert row is not None
        store.update_run(
            replace(
                row,
                phase=step,
                updated_at=row.updated_at + 1,
                executed_provider="claude",
                executed_account=f"account-{run_id[:4]}",
            )
        )


@pytest.mark.parametrize("last", [RunPhase.COMPLETED, RunPhase.CLOSED])
def test_a_swarm_of_two_runs_gives_one_notice_that_names_both(
    registry: Registry, sessions: Callable[..., Session], last: RunPhase
) -> None:
    session = sessions()
    _admit(registry, A, session.caller(), SWARM)
    _admit(registry, B, session.caller(), SWARM)
    _end(registry, A)
    assert notify.push_ready(registry, [A]) == 0, "a member ended alone"
    assert session.frames() == []
    _end(registry, B, last)
    assert notify.push_ready(registry, [B]) == 2
    assert notify.push_ready(registry) == 0
    (body,) = _bodies(session, 1)
    assert "swarm review-1" in body and "2 runs" in body
    assert f"{A} is completed" in body and f"{B} is {last.value}" in body
    assert f"account-{A[:4]}" in body and f"account-{B[:4]}" in body
    assert "nspawn_result_show" in body
    for run_id in (A, B):
        notice = _notice(registry, run_id)
        assert notice is not None and notice["delivered_at"] is not None


@pytest.mark.parametrize("group", [None, "review-1"])
def test_two_runs_with_no_swarm_still_give_two_notices(
    registry: Registry, sessions: Callable[..., Session], group: str | None
) -> None:
    session = sessions()
    _admit(registry, A, session.caller(), group)
    _admit(registry, B, session.caller(), group)
    _end(registry, A)
    assert notify.push_ready(registry, [A]) == 1
    _end(registry, B)
    assert notify.push_ready(registry, [B]) == 1
    # The fake session records each connection on its own thread: any order.
    bodies = _bodies(session, 2)
    assert sorted((A in body, B in body) for body in bodies) == [
        (False, True),
        (True, False),
    ]
    assert all(" is completed" in body for body in bodies)


def test_a_swarm_holds_nothing_for_a_member_of_another_caller(
    registry: Registry, sessions: Callable[..., Session]
) -> None:
    """A swarm is one caller's: another session's run in that group never holds
    this session's notice."""
    mine, theirs = sessions(), sessions()
    _admit(registry, A, mine.caller(), SWARM)
    _admit(registry, B, theirs.caller(), SWARM)
    _end(registry, A)
    assert notify.push_ready(registry, [A]) == 1
    (body,) = _bodies(mine, 1)
    assert f"{A} is completed" in body


def test_a_held_member_is_not_drained_while_its_caller_lives(
    registry: Registry,
    sessions: Callable[..., Session],
    capsys: pytest.CaptureFixture[str],
) -> None:
    session = sessions()
    _admit(registry, A, session.caller(), SWARM)
    _admit(registry, B, session.caller(), SWARM)
    _end(registry, A)
    sql(
        registry,
        "UPDATE notices SET ready_at = ? WHERE run_id = ?",
        (store_module.now_ms() - 600_000, A),
    )
    assert _drain(capsys) == [], "another session start took a held member"
    _end(registry, B)
    assert notify.push_ready(registry, [B]) == 2
    (body,) = _bodies(session, 1)
    assert A in body and B in body


def test_a_dead_callers_swarm_is_drained_as_one_notice_once_all_end(
    registry: Registry, capsys: pytest.CaptureFixture[str]
) -> None:
    dead = _dead_caller()
    _admit(registry, A, dead, SWARM)
    _admit(registry, B, dead, SWARM)
    _end(registry, A)
    assert _drain(capsys) == [], "a member was drained while its sibling runs"
    _end(registry, B, RunPhase.FAILED)
    (line, *rest) = _drain(capsys)
    assert line.startswith("nSPAWN swarm review-1 ended")
    body = "\n".join([line, *rest])
    assert f"{A} is completed" in body and f"{B} is failed" in body
    assert f"ALERT: 1 member(s) did not succeed: run {B}" in body
    assert _drain(capsys) == []


def test_a_swarm_notice_past_the_ceiling_still_arrives_as_one(
    registry: Registry,
    sessions: Callable[..., Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The grouped notice never fails: past the envelope's ceiling it counts
    the members it cannot list, and still arrives once."""
    monkeypatch.setattr(notify, "MAX_BODY_CHARS", 300)
    session = sessions()
    _admit(registry, A, session.caller(), SWARM)
    _admit(registry, B, session.caller(), SWARM)
    _end(registry, A)
    _end(registry, B)
    assert notify.push_ready(registry, [B]) == 2
    (body,) = _bodies(session, 1)
    assert "1 more runs are not listed" in body
    assert len(body) < 300 + 200  # the envelope's own text included


def test_a_drain_takes_a_swarm_whole_by_its_last_members_age(
    registry: Registry,
    sessions: Callable[..., Session],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A live caller nobody can push to: the swarm is stale only when its LAST
    member is, and then it drains as one notice."""
    session = sessions()
    unreachable = session.caller(addr=str(session.record) + ".nobody.sock")
    _admit(registry, A, unreachable, SWARM)
    _admit(registry, B, unreachable, SWARM)
    _end(registry, A)
    _end(registry, B)
    sql(
        registry,
        "UPDATE notices SET ready_at = ? WHERE run_id = ?",
        (store_module.now_ms() - 600_000, A),
    )
    assert _drain(capsys) == [], "the drain split a swarm by its first member's age"
    sql(
        registry,
        "UPDATE notices SET ready_at = ? WHERE run_id = ?",
        (store_module.now_ms() - 121_000, B),
    )
    body = "\n".join(_drain(capsys))
    assert body.startswith("nSPAWN swarm review-1 ended: its 2 runs")
    assert A in body and B in body
    assert _drain(capsys) == []


def test_a_drain_keeps_two_callers_swarms_of_one_name_apart(
    registry: Registry, capsys: pytest.CaptureFixture[str]
) -> None:
    first, second = _dead_caller(), _dead_caller()
    C, D = _rid("swarm-c"), _rid("swarm-d")
    for run_id, caller in ((A, first), (B, first), (C, second), (D, second)):
        _admit(registry, run_id, caller, SWARM)
        _end(registry, run_id)
    lines = _drain(capsys)
    heads = [line for line in lines if line.startswith("nSPAWN swarm")]
    assert len(heads) == 2, lines
    assert all("its 2 runs" in head for head in heads)


def test_a_refused_mark_never_sends_a_part_of_a_swarm_again(
    registry: Registry,
    sessions: Callable[..., Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The members are marked in one write: when the store refuses the mark of
    the second member, the first is not marked either, and the next push sends
    the whole swarm again, never a part of it."""
    session = sessions()
    _admit(registry, A, session.caller(), SWARM)
    _admit(registry, B, session.caller(), SWARM)
    _end(registry, A)
    _end(registry, B)
    real = Registry.mark_notice_delivered
    calls: list[str] = []

    def refuse_second(self: Registry, run_id: str, addr: str, token: str) -> bool:
        calls.append(run_id)
        if len(calls) == 2:
            raise nott_database.OperationalError("database is locked")
        return real(self, run_id, addr, token)

    monkeypatch.setattr(Registry, "mark_notice_delivered", refuse_second)
    with pytest.raises(nott_database.OperationalError):
        notify.push_ready(registry, [B])
    monkeypatch.setattr(Registry, "mark_notice_delivered", real)
    for run_id in (A, B):
        notice = _notice(registry, run_id)
        assert notice is not None and notice["delivered_at"] is None, run_id
    _lapse(registry, A)
    _lapse(registry, B)
    assert notify.push_ready(registry) == 2
    first, second = _bodies(session, 2)
    assert first == second, "the second send was not the whole swarm"


def test_the_swarm_option_is_the_group_swarm_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[dict[str, Any]] = []

    def dispatched(
        self: mcp.McpServer, tool: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        sent.append(arguments)
        return {}

    monkeypatch.setattr(mcp.McpServer, "_dispatched", dispatched)
    monkeypatch.delenv("NOTT_NSPAWN_NODE_SESSION", raising=False)
    server = mcp.McpServer(launcher=object())  # type: ignore[arg-type]
    server.nspawn_run_start("p", "/fixture", swarm="review-1")
    server.nspawn_review_start("lane/x", "/fixture", swarm="review-1")
    assert [arguments["group"] for arguments in sent] == [SWARM, SWARM]
    for start in (server.nspawn_run_start, server.nspawn_review_start):
        with pytest.raises(mcp.McpError, match="swarm or group"):
            start("p", "/fixture", swarm="review-1", group="g")
        with pytest.raises(mcp.McpError, match="must not start with 'swarm:'"):
            start("p", "/fixture", group=SWARM)
    with pytest.raises(mcp.McpError, match="must not start with 'swarm:'"):
        server.nspawn_batch_start("p", "/fixture", ["claude"], group=SWARM)
    for tool in ("nspawn_run_start", "nspawn_review_start"):
        schema = mcp.NSPAWN_TOOL_BY_NAME[tool].input_schema["properties"]
        assert (
            schema["swarm"]["maxLength"] + len("swarm:")
            <= (schema["group"]["maxLength"])
        )


def test_a_node_door_refuses_swarm(monkeypatch: pytest.MonkeyPatch) -> None:
    """The host takes a node's notices one by one: the option is refused, not
    accepted and ignored."""
    monkeypatch.setenv("NOTT_NSPAWN_NODE_SESSION", str(uuid.uuid4()))
    server = mcp.McpServer(launcher=object())  # type: ignore[arg-type]
    with pytest.raises(mcp.McpError, match="a node door does not accept swarm"):
        server.nspawn_review_start("lane/x", "/fixture", swarm="review-1")
    with pytest.raises(mcp.McpError, match="a node door does not accept swarm"):
        server.nspawn_run_start("p", "/fixture", swarm="review-1", timeout_ms=1)


# ---------------------------------------------------------------------------
# The production push timing: a supervisor that announces, members started
# before any of them ends. A member that fails is retried once; a member that
# hangs ends at its deadline. Either way the swarm gives one notice.
# ---------------------------------------------------------------------------

PREFERENCE = '[preference]\nreview = ["oauth", "key"]\n'


@dataclass
class Plan:
    """What each driven run ends with, decided once every member is admitted.

    `first` maps a member's admission index to its verdict, COMPLETED when it
    names none; each run admitted later (a retry) takes the next of `retries`,
    COMPLETED when none is left. `hang_s` holds the first member that long.
    """

    first: dict[int, Callable[[], terminal.TerminalClassification]]
    retries: list[Callable[[], terminal.TerminalClassification]] = field(
        default_factory=list
    )
    hang_s: float = 0.0
    gate: threading.Event = field(default_factory=threading.Event)
    #: A member index whose drive raises this through `_fail_run` instead.
    raises: dict[int, Exception] = field(default_factory=dict)
    members: list[str] = field(default_factory=list)
    timeouts: dict[str, int] = field(default_factory=dict)


@pytest.fixture
def plan(monkeypatch: pytest.MonkeyPatch) -> Plan:
    monkeypatch.setattr(native, "prepare_native", _fenced)
    held = Plan(first={})

    def end(self: Supervisor, run_id: str, timeout_ms: int) -> None:
        held.timeouts[run_id] = timeout_ms
        assert held.gate.wait(10), "the test never released its members"
        if run_id in held.members:
            index = held.members.index(run_id)
            if index in held.raises:
                self._fail_run(run_id, held.raises[index])
                return
            verdict = held.first.get(index, _completed)()
        else:
            verdict = held.retries.pop(0)() if held.retries else _completed()
            index = -1
        if index == 0 and held.hang_s:
            time.sleep(held.hang_s)  # the child hangs past its deadline
            verdict = replace(_broken(), failure_reason="RunDeadlineExceeded")
        self._publish_terminal(run_id, verdict)

    def caged(
        self: Supervisor,
        row: Any,
        run_dir: Any,
        prompt: Any,
        roster: Any,
        picked: Any,
        system_prompt: Any,
        timeout_ms: int,
        **_: object,
    ) -> None:
        end(self, row.run_id, timeout_ms)

    def run(self: Supervisor, run_id: str, *args: Any, **kwargs: Any) -> None:
        end(self, run_id, int(kwargs.get("deadline_ms", 0)))

    monkeypatch.setattr(Supervisor, "_drive_claude_caged", caged)
    monkeypatch.setattr(Supervisor, "_drive_run", run)
    return held


def _swarm(
    registry: Registry,
    tmp_path: Path,
    session: Session,
    plan: Plan,
    *,
    provider: str | None = None,
    start: str = "review",
    before_shutdown: Callable[[Supervisor], None] | None = None,
) -> list[str]:
    """Two runs of one swarm, both admitted before either may end, on a
    supervisor that pushes as the MCP server's does. Answers every run id.
    `start` is `review` (nspawn_review_start) or `execute` (nspawn_run_start)."""
    sv = Supervisor(registry, tmp_path / "runs", announces_terminal=True)
    calls = []
    try:
        for index in range(2):
            call = supervisor.DriveCall(
                launch_uuid=f"launch-{index}",
                group=SWARM,
                caller=session.frame_caller(),
                project=PROJECT,
            )
            token = supervisor.DRIVE_CALL.set(call)
            try:
                accepted = (
                    sv.review("feature-x", str(tmp_path), provider=provider)
                    if start == "review"
                    else sv.execute(
                        "read it", str(tmp_path), provider=provider, read_only=True
                    )
                )
                plan.members.append(accepted.run_id)
            finally:
                supervisor.DRIVE_CALL.reset(token)
            calls.append(call)
        plan.gate.set()
        for call in calls:
            supervisor.join_workers(call)
        if before_shutdown is not None:
            before_shutdown(sv)
            for call in calls:
                supervisor.join_workers(call)
    finally:
        sv.shutdown()
    runs = [row.run_id for row in registry.list_runs()]
    assert all(registry.get_run(run_id).phase.is_terminal() for run_id in runs)  # type: ignore[union-attr]
    return runs


def _one_notice(registry: Registry, session: Session) -> str:
    (body,) = _bodies(session, 1)
    assert notify.push_ready(registry) == 0, "a notice was left behind"
    time.sleep(0.2)
    assert len(session.frames()) == 1, "the swarm gave more than one notice"
    return body


def test_an_unpinned_member_that_fails_succeeds_on_the_next_preference(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    plan: Plan,
    sessions: Callable[..., Session],
) -> None:
    _roster(roster_home, tmp_path, PREFERENCE)
    plan.first = {0: _broken}
    session = sessions()
    runs = _swarm(registry, tmp_path, session, plan)
    first, other = plan.members
    failed = registry.get_run(first)
    assert failed is not None and failed.phase is RunPhase.FAILED
    retry = registry.get_run(_successor(failed.warning))
    assert retry is not None and retry.phase is RunPhase.COMPLETED
    assert (failed.executed_account, retry.executed_account) == ("oauth", "key")
    assert retry.group_name == SWARM and len(runs) == 3
    assert "None" not in (retry.warning or ""), retry.warning
    body = _one_notice(registry, session)
    assert "3 runs are terminal" in body and "ALERT" not in body
    assert f"{retry.run_id} is completed" in body
    assert f"It continues the work of run {first}" in body
    assert f"{other} is completed" in body


def test_a_pinned_member_that_dies_on_quota_succeeds_on_another_account(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    plan: Plan,
    sessions: Callable[..., Session],
) -> None:
    _roster(roster_home, tmp_path)
    plan.first = {0: lambda: _refused(402)}
    session = sessions()
    _swarm(registry, tmp_path, session, plan, provider="claude")
    failed = registry.get_run(plan.members[0])
    assert failed is not None and failed.phase is RunPhase.FAILED
    retry = registry.get_run(_successor(failed.warning))
    assert retry is not None and retry.phase is RunPhase.COMPLETED
    assert retry.executed_account != failed.executed_account
    assert "skipped claude (HTTP 402" in (retry.warning or ""), retry.warning
    body = _one_notice(registry, session)
    assert "3 runs are terminal" in body and "ALERT" not in body


def test_a_member_that_fails_twice_is_named_in_the_one_notice(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    plan: Plan,
    sessions: Callable[..., Session],
) -> None:
    _roster(roster_home, tmp_path, PREFERENCE)
    plan.first = {0: _broken}
    plan.retries = [_broken]
    session = sessions()
    runs = _swarm(registry, tmp_path, session, plan)
    failed = registry.get_run(plan.members[0])
    assert failed is not None
    retry = registry.get_run(_successor(failed.warning))
    assert retry is not None and retry.phase is RunPhase.FAILED
    assert (
        f"the swarm retry is spent: the failure of run {failed.run_id} took it"
    ) in (retry.warning or "")
    assert len(runs) == 3, "a member is retried once, not twice"
    body = _one_notice(registry, session)
    assert (
        f"ALERT: 1 member(s) did not succeed: run {retry.run_id} "
        "(failed: ClaudeRunFailed (terminal_incomplete))"
    ) in body
    assert f"{plan.members[1]} is completed" in body


def test_a_member_that_hangs_ends_at_its_deadline_in_the_one_notice(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    plan: Plan,
    sessions: Callable[..., Session],
) -> None:
    """A swarm member with no timeout gets SWARM_TIMEOUT_MS; past it the run
    ends failed, is not retried, and the one notice names it with its cause."""
    monkeypatch.setattr(supervisor, "SWARM_TIMEOUT_MS", 200)
    _roster(roster_home, tmp_path, PREFERENCE)
    plan.hang_s = 0.4
    session = sessions()
    runs = _swarm(registry, tmp_path, session, plan)
    hung, other = plan.members
    assert plan.timeouts == {hung: 200, other: 200}
    row = registry.get_run(hung)
    assert row is not None and row.phase is RunPhase.FAILED
    assert "the deadline passed" in (row.warning or ""), row.warning
    assert len(runs) == 2, "a member past its deadline is not retried"
    body = _one_notice(registry, session)
    assert f"run {hung} (failed: RunDeadlineExceeded)" in body
    assert f"{other} is completed" in body


def _accounts_roster(home: Path, tmp_path: Path, text: str) -> None:
    """A roster of claude routes; `text` declares the providers by `{name}`
    account homes, which this makes."""
    names = set(re.findall(r"\{(\w+)\}", text))
    homes = {name: tmp_path / name for name in names}
    for directory in homes.values():
        directory.mkdir(exist_ok=True)
    (home / ".nott" / "spawn.toml").write_text(
        '[bins]\nclaude = "/usr/bin/true"\ncodex = "/usr/bin/true"\n'
        + text.format(**homes),
        encoding="utf-8",
    )


def test_a_route_refusal_never_spends_the_swarms_one_retry(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    plan: Plan,
    sessions: Callable[..., Session],
) -> None:
    """oauth answers 402, key 429, third completes: a swarm reader walks the
    whole order, as a review with no swarm does."""
    _accounts_roster(
        roster_home,
        tmp_path,
        '[[provider]]\nname = "claude"\nkind = "claude"\nstrength = 8\n'
        'model = "m"\nmodels = ["m"]\n'
        'accounts = {{ oauth = {{ home = "{oauth}" }}, key = {{ home = "{key}" }}, '
        'third = {{ home = "{third}" }} }}\n'
        '[preference]\nreview = ["oauth", "key", "third"]\n',
    )
    plan.first = {0: lambda: _refused(402)}
    plan.retries = [lambda: _refused(429)]
    session = sessions()
    runs = _swarm(registry, tmp_path, session, plan)
    first = registry.get_run(plan.members[0])
    assert first is not None
    second = registry.get_run(_successor(first.warning))
    assert second is not None and second.executed_account == "key"
    third = registry.get_run(_successor(second.warning))
    assert third is not None and third.phase is RunPhase.COMPLETED
    assert third.executed_account == "third"
    assert len(runs) == 4
    body = _one_notice(registry, session)
    assert "4 runs are terminal" in body and "ALERT" not in body


def test_a_member_moved_by_a_rate_limit_shows_the_substitution_on_its_line(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    plan: Plan,
    sessions: Callable[..., Session],
) -> None:
    """The owner reads where the work ran instead: on the line of
    the member that served, and in the answer the caller reads."""
    _roster(roster_home, tmp_path, PREFERENCE)
    plan.first = {0: lambda: _refused(429)}
    session = sessions()
    _swarm(registry, tmp_path, session, plan)
    failed = registry.get_run(plan.members[0])
    assert failed is not None
    retry = _successor(failed.warning)
    body = _one_notice(registry, session)
    (line,) = [
        text for text in body.splitlines() if text.startswith(f"- nSPAWN run {retry}")
    ]
    assert "Substitution alert:" in line, line
    assert "claude/oauth" in line and "HTTP 429" in line and "claude/key" in line
    others = [text for text in body.splitlines() if "Substitution alert" in text]
    assert others == [line]
    answer = Supervisor(registry, tmp_path / "runs").result(failed.run_id)
    assert answer.run_id == retry and answer.phase is RunPhase.COMPLETED
    assert answer.alert is not None and answer.alert in line


def test_an_unpinned_run_start_member_retries_on_the_next_route(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    plan: Plan,
    sessions: Callable[..., Session],
) -> None:
    """An nspawn_run_start member whose route has one account fails: its one
    retry goes to the next route of the order, never back to that route."""
    _accounts_roster(
        roster_home,
        tmp_path,
        '[[provider]]\nname = "alpha"\nkind = "claude"\nstrength = 9\n'
        'model = "m"\nmodels = ["m"]\naccounts = {{ a1 = {{ home = "{a1}" }} }}\n'
        '[[provider]]\nname = "beta"\nkind = "claude"\nstrength = 8\n'
        'model = "m"\nmodels = ["m"]\naccounts = {{ b1 = {{ home = "{b1}" }} }}\n',
    )
    plan.first = {0: _broken}
    session = sessions()
    runs = _swarm(registry, tmp_path, session, plan, start="execute")
    failed = registry.get_run(plan.members[0])
    assert failed is not None and failed.phase is RunPhase.FAILED
    retry = registry.get_run(_successor(failed.warning))
    assert retry is not None and retry.phase is RunPhase.COMPLETED
    assert {failed.executed_provider, retry.executed_provider} == {"alpha", "beta"}
    assert len(runs) == 3
    body = _one_notice(registry, session)
    assert "3 runs are terminal" in body and "ALERT" not in body


def test_a_swarm_with_one_dead_member_caller_drains_whole(
    registry: Registry,
    sessions: Callable[..., Session],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The caller's stdio restarted between the dispatches: one member names a
    dead pid, the other a live one, on one address. The swarm drains whole."""
    session = sessions()
    dead = _dead_caller()
    addr = f"remote:{uuid.uuid4()}"
    _admit(registry, A, replace(dead, addr=addr), SWARM)
    _admit(registry, B, session.caller(addr=addr), SWARM)
    _end(registry, A)
    _end(registry, B)
    body = "\n".join(_drain(capsys))
    assert body.startswith("nSPAWN swarm review-1 ended: its 2 runs"), body
    assert _drain(capsys) == []


THREE_ACCOUNTS = (
    '[[provider]]\nname = "claude"\nkind = "claude"\nstrength = 8\n'
    'model = "m"\nmodels = ["m"]\n'
    'accounts = {{ oauth = {{ home = "{oauth}" }}, key = {{ home = "{key}" }}, '
    'third = {{ home = "{third}" }} }}\n'
)


def _chain(registry: Registry, run_id: str) -> list[RunRow]:
    """The run and each run its work continued as, in order."""
    rows = []
    while True:
        row = registry.get_run(run_id)
        assert row is not None
        rows.append(row)
        if (
            found := re.search(r"continued as run (\S+) on ", row.warning or "")
        ) is None:
            return rows
        run_id = found[1]


@pytest.mark.parametrize(
    "second", [lambda: _refused(429), _broken], ids=["429", "terminal_incomplete"]
)
def test_a_pinned_member_walks_every_account_of_its_route(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    plan: Plan,
    sessions: Callable[..., Session],
    second: Callable[[], terminal.TerminalClassification],
) -> None:
    """Pinned to `claude`: a 402, then a 429 or a failure that is not a
    refusal, and the third account completes. A refusal never spends the one
    retry, and the retry takes the next account of the walk."""
    _accounts_roster(roster_home, tmp_path, THREE_ACCOUNTS)
    plan.first = {0: lambda: _refused(402)}
    plan.retries = [second]
    session = sessions()
    _swarm(registry, tmp_path, session, plan, provider="claude")
    chain = _chain(registry, plan.members[0])
    assert [row.phase for row in chain] == [
        RunPhase.FAILED,
        RunPhase.FAILED,
        RunPhase.COMPLETED,
    ]
    assert {row.executed_account for row in chain} == {"oauth", "key", "third"}
    body = _one_notice(registry, session)
    assert "4 runs are terminal" in body and "ALERT" not in body


@pytest.mark.parametrize("start", ["review", "execute"])
def test_an_unpinned_member_on_one_route_retries_on_its_other_account(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    plan: Plan,
    sessions: Callable[..., Session],
    start: str,
) -> None:
    """One provider with two accounts: no next route remains, so the one retry
    for a failure that is not a refusal takes the route's other account."""
    _accounts_roster(
        roster_home,
        tmp_path,
        '[[provider]]\nname = "claude"\nkind = "claude"\nstrength = 8\n'
        'model = "m"\nmodels = ["m"]\n'
        'accounts = {{ oauth = {{ home = "{oauth}" }}, key = {{ home = "{key}" }} }}\n',
    )
    plan.first = {0: _broken}
    session = sessions()
    _swarm(registry, tmp_path, session, plan, start=start)
    chain = _chain(registry, plan.members[0])
    assert [row.phase for row in chain] == [RunPhase.FAILED, RunPhase.COMPLETED]
    assert {row.executed_account for row in chain} == {"oauth", "key"}
    body = _one_notice(registry, session)
    assert "3 runs are terminal" in body and "ALERT" not in body


def test_a_walk_entry_is_resolved_by_provider_and_account(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    plan: Plan,
    sessions: Callable[..., Session],
) -> None:
    """Two providers' secrets directories share the aliases `a` and `b`: a walk
    entry resolved by the alias alone is ambiguous and would end the walk. It
    is resolved as provider plus account, so the work goes on to `b`."""
    for name in ("claude", "sonnet"):
        directory = tmp_path / "secrets" / name
        directory.mkdir(parents=True)
        for alias in ("a", "b"):
            (directory / f"{alias}.token").write_text("x", encoding="utf-8")
    _accounts_roster(
        roster_home,
        tmp_path,
        "".join(
            f'[[provider]]\nname = "{name}"\nkind = "claude"\nstrength = 8\n'
            f'model = "m"\nmodels = ["m"]\nsecrets = "{tmp_path / "secrets" / name}"\n'
            for name in ("claude", "sonnet")
        ),
    )
    plan.first = {0: lambda: _refused(402)}
    session = sessions()
    _swarm(registry, tmp_path, session, plan, provider="claude")
    chain = _chain(registry, plan.members[0])
    assert [row.phase for row in chain] == [RunPhase.FAILED, RunPhase.COMPLETED]
    assert {row.executed_account for row in chain} == {"a", "b"}
    assert {row.executed_provider for row in chain} == {"claude"}


def test_the_account_walk_takes_the_declared_order(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    plan: Plan,
    sessions: Callable[..., Session],
) -> None:
    """Accounts declared mid, zeta, alpha: the walk follows the order the owner
    wrote, as the picker does, never the alphabet."""
    _accounts_roster(
        roster_home,
        tmp_path,
        '[[provider]]\nname = "claude"\nkind = "claude"\nstrength = 8\n'
        'model = "m"\nmodels = ["m"]\n'
        'accounts = {{ mid = {{ home = "{mid}" }}, zeta = {{ home = "{zeta}" }}, '
        'alpha = {{ home = "{alpha}" }} }}\n',
    )
    plan.first = {0: lambda: _refused(402)}
    plan.retries = [lambda: _refused(402)]
    session = sessions()
    _swarm(registry, tmp_path, session, plan, provider="claude")
    walked = [row.executed_account for row in _chain(registry, plan.members[0])]
    declared = ["mid", "zeta", "alpha"]
    assert walked[0] != "alpha", "both orders agree after alpha: nothing measured"
    assert walked == [walked[0], *(a for a in declared if a != walked[0])]


def test_an_account_alias_equal_to_a_provider_name_never_skips_a_step(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    plan: Plan,
    sessions: Callable[..., Session],
) -> None:
    """Provider `claude` holds `claude.token` and `first.token`: a 402 on
    claude/claude refuses that account, never the step claude/first."""
    directory = tmp_path / "secrets" / "claude"
    directory.mkdir(parents=True)
    for alias in ("claude", "first"):
        (directory / f"{alias}.token").write_text("x", encoding="utf-8")
    _accounts_roster(
        roster_home,
        tmp_path,
        '[[provider]]\nname = "claude"\nkind = "claude"\nstrength = 8\n'
        f'model = "m"\nmodels = ["m"]\nsecrets = "{directory}"\n',
    )
    plan.first = {0: lambda: _refused(402)}
    session = sessions()
    _swarm(registry, tmp_path, session, plan, provider="claude")
    chain = _chain(registry, plan.members[0])
    assert chain[0].executed_account == "claude", "the test needs claude first"
    assert [row.phase for row in chain] == [RunPhase.FAILED, RunPhase.COMPLETED]
    assert chain[1].executed_account == "first"


def test_a_refusal_on_one_provider_never_keeps_off_the_same_alias_of_another(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    plan: Plan,
    sessions: Callable[..., Session],
) -> None:
    """`sonnet` and `claude` both hold `a` and `b`. 402s on sonnet/a and on
    claude/b refuse those two steps only: sonnet/b still serves."""
    for name in ("claude", "sonnet"):
        directory = tmp_path / "secrets" / name
        directory.mkdir(parents=True)
        for alias in ("a", "b"):
            (directory / f"{alias}.token").write_text("x", encoding="utf-8")
    _accounts_roster(
        roster_home,
        tmp_path,
        "".join(
            f'[[provider]]\nname = "{name}"\nkind = "claude"\nstrength = 8\n'
            f'model = "m"\nmodels = ["m"]\nsecrets = "{tmp_path / "secrets" / name}"\n'
            for name in ("claude", "sonnet")
        )
        + '[preference]\nreview = ["sonnet", "claude"]\n',
    )
    plan.first = {0: lambda: _refused(402)}
    plan.retries = [lambda: _refused(402)]
    session = sessions()
    _swarm(registry, tmp_path, session, plan)
    chain = _chain(registry, plan.members[0])
    assert chain[-1].phase is RunPhase.COMPLETED, [
        (row.executed_provider, row.executed_account, row.warning) for row in chain
    ]
    served = [(row.executed_provider, row.executed_account) for row in chain]
    assert len(set(served)) == len(served), "a refused step ran again"


def test_a_token_two_providers_share_never_serves_again_after_it_refused(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    plan: Plan,
    sessions: Callable[..., Session],
) -> None:
    """`sonnet` and `claude` read ONE secrets directory: `a` and `b` are one
    token each, whatever provider runs them. Each token that refused is kept
    off under both providers, so the walk ends instead of trying it again."""
    shared = tmp_path / "secrets" / "shared"
    shared.mkdir(parents=True)
    for alias in ("a", "b"):
        (shared / f"{alias}.token").write_text("x", encoding="utf-8")
    _accounts_roster(
        roster_home,
        tmp_path,
        "".join(
            f'[[provider]]\nname = "{name}"\nkind = "claude"\nstrength = 8\n'
            f'model = "m"\nmodels = ["m"]\nsecrets = "{shared}"\n'
            for name in ("claude", "sonnet")
        )
        + '[preference]\nreview = ["sonnet", "claude"]\n',
    )
    plan.first = {0: lambda: _refused(402)}
    plan.retries = [lambda: _refused(402), lambda: _refused(402)]
    session = sessions()
    _swarm(registry, tmp_path, session, plan)
    chain = _chain(registry, plan.members[0])
    tokens = [row.executed_account for row in chain]
    assert sorted(tokens) == ["a", "b"], [
        (row.executed_provider, row.executed_account) for row in chain
    ]
    assert chain[-1].phase is RunPhase.FAILED
    assert "no further review preference can serve now" in (chain[-1].warning or "")


def _secrets_roster(
    home: Path, tmp_path: Path, tokens: dict[str, tuple[str, ...]], extra: str = ""
) -> None:
    """Claude providers that each read their own secrets directory of `tokens`."""
    text = ""
    for name, aliases in tokens.items():
        directory = tmp_path / "secrets" / name
        directory.mkdir(parents=True)
        for alias in aliases:
            (directory / f"{alias}.token").write_text("x", encoding="utf-8")
        text += (
            f'[[provider]]\nname = "{name}"\nkind = "claude"\nstrength = 8\n'
            f'model = "m"\nmodels = ["m"]\nsecrets = "{directory}"\n'
        )
    _accounts_roster(home, tmp_path, text + extra)


def test_an_unpinned_walk_reaches_every_account_of_two_directories(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    plan: Plan,
    sessions: Callable[..., Session],
) -> None:
    """claude and sonnet each hold their own `a` and `b`. Three 402s never keep
    off an account that did not refuse: claude/b and sonnet/a still run."""
    _secrets_roster(roster_home, tmp_path, {"claude": ("a", "b"), "sonnet": ("a", "b")})
    plan.first = {0: lambda: _refused(402)}
    plan.retries = [lambda: _refused(402), lambda: _refused(402)]
    session = sessions()
    _swarm(registry, tmp_path, session, plan)
    chain = _chain(registry, plan.members[0])
    served = {(row.executed_provider, row.executed_account) for row in chain}
    assert served == {
        ("claude", "a"),
        ("claude", "b"),
        ("sonnet", "a"),
        ("sonnet", "b"),
    }, [(row.executed_provider, row.executed_account, row.warning) for row in chain]
    assert chain[-1].phase is RunPhase.COMPLETED


def test_an_alias_named_like_another_provider_never_skips_that_provider(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    plan: Plan,
    sessions: Callable[..., Session],
) -> None:
    """anthropic holds `claude.token`: its 402 refuses that token, never the
    provider named claude, which still serves."""
    _secrets_roster(
        roster_home, tmp_path, {"anthropic": ("claude", "work"), "claude": ("x",)}
    )
    plan.first = {0: lambda: _refused(402)}
    session = sessions()
    _swarm(registry, tmp_path, session, plan)
    chain = _chain(registry, plan.members[0])
    assert (chain[0].executed_provider, chain[0].executed_account) == (
        "anthropic",
        "claude",
    ), "the test needs anthropic/claude first"
    assert chain[1].executed_provider == "claude", chain[1].warning
    assert chain[1].phase is RunPhase.COMPLETED


def test_a_tilde_home_walks_with_the_cwd_removed(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    plan: Plan,
    sessions: Callable[..., Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Accounts declared on `~/` homes, and the stdio's cwd is gone (a lane
    worktree removed after its merge): an unpinned review still starts, and a
    swarm member still walks to the other account."""
    monkeypatch.setenv("HOME", str(roster_home))
    for name in ("one", "two"):
        (roster_home / name).mkdir()
    _accounts_roster(
        roster_home,
        tmp_path,
        '[[provider]]\nname = "claude"\nkind = "claude"\nstrength = 8\n'
        'model = "m"\nmodels = ["m"]\n'
        'accounts = {{ one = {{ home = "~/one" }}, two = {{ home = "~/two" }} }}\n',
    )
    gone = tmp_path / "gone"
    gone.mkdir()
    monkeypatch.chdir(gone)
    gone.rmdir()
    plan.first = {0: lambda: _refused(402)}
    session = sessions()
    _swarm(registry, tmp_path, session, plan)
    chain = _chain(registry, plan.members[0])
    assert [row.phase for row in chain] == [RunPhase.FAILED, RunPhase.COMPLETED]
    assert {row.executed_account for row in chain} == {"one", "two"}


def test_one_home_spelled_two_ways_has_one_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nspawn.owner_roster import NativeAccount, Provider

    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "x").mkdir()
    tilde = Provider(
        name="p", kind="codex", accounts={"a": NativeAccount(home=Path("~/x"))}
    )
    whole = Provider(
        name="q", kind="codex", accounts={"b": NativeAccount(home=tmp_path / "x")}
    )
    assert supervisor._credential(tilde, "a") == supervisor._credential(whole, "b")
    assert supervisor._credential(tilde, "a") == (
        f"home:{os.path.realpath(tmp_path)}/x/auth.json"
    )


def test_a_credential_that_cannot_be_read_never_loses_the_walk(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    plan: Plan,
    sessions: Callable[..., Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refused credential raises once while `_fail_over` builds the next
    step: the failover goes back, the terminal write waits for its retry, and
    the retry walks on to the other account."""
    _accounts_roster(roster_home, tmp_path, THREE_ACCOUNTS)
    plan.first = {0: lambda: _refused(402)}
    real = Supervisor._credentials_of
    raised: list[bool] = []

    def once(failover: Any, row: RunRow) -> frozenset[str]:
        if not raised:
            raised.append(True)
            raise OSError("the token directory is gone")
        return real(failover, row)

    monkeypatch.setattr(Supervisor, "_credentials_of", staticmethod(once))
    session = sessions()
    _swarm(
        registry,
        tmp_path,
        session,
        plan,
        provider="claude",
        before_shutdown=lambda sv: sv.publish_pending(),
    )
    assert raised == [True]
    chain = _chain(registry, plan.members[0])
    assert [row.phase for row in chain] == [RunPhase.FAILED, RunPhase.COMPLETED]


def test_a_provider_with_no_source_is_keyed_by_its_alias(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No token file, no auth_env: `credential_source` refuses, and the key
    falls back to route and alias, with the reason logged."""
    from nspawn.owner_roster import NativeAccount, Provider

    monkeypatch.delenv("HOME", raising=False)
    provider = Provider(name="p", accounts={"a": NativeAccount(home=Path("~/x"))})
    assert supervisor._credential(provider, "a") == "route:p/a"
    assert "the credential is keyed as route:p/a" in capsys.readouterr().err


def test_one_codex_home_keeps_its_oauth_and_api_accounts_apart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """codex `main` (OAuth) and `fugu` (API key) share one home. `main` is
    keyed by the OAuth file in that home, `fugu` by its token file, so a 429 on
    main keeps fugu in the walk and a 402 on fugu keeps main."""
    from nspawn.owner_roster import NativeAccount, Provider, Roster

    monkeypatch.setenv("HOME", str(tmp_path))
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "fugu.token").write_text("x", encoding="utf-8")
    codex = Provider(
        name="codex",
        kind="codex",
        secrets=secrets,
        accounts={
            "main": NativeAccount(home=Path("~/.codex")),
            "fugu": NativeAccount(
                home=Path("~/.codex"), auth_env="FUGU_API_KEY", model_provider="fugu"
            ),
        },
    )
    main_key = supervisor._credential(codex, "main")
    fugu_key = supervisor._credential(codex, "fugu")
    assert main_key == f"home:{tmp_path}/.codex/auth.json"
    assert fugu_key == f"token:{secrets}/fugu.token"
    roster = Roster(providers=[codex])
    for refused, other in (("main", "fugu"), ("fugu", "main")):
        kept = supervisor._without_credentials(
            roster, frozenset({supervisor._credential(codex, refused)})
        )
        assert kept.providers[0].excluded_accounts == [refused], other


def test_a_claude_account_with_a_home_is_keyed_by_its_token_file(
    tmp_path: Path,
) -> None:
    """A Claude child authenticates with the token file and uses the home only
    as CLAUDE_CONFIG_DIR: the key is the token file."""
    from nspawn.owner_roster import NativeAccount, Provider

    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "a.token").write_text("x", encoding="utf-8")
    provider = Provider(
        name="claude",
        secrets=secrets,
        accounts={"a": NativeAccount(home=tmp_path / "config")},
    )
    assert supervisor._credential(provider, "a") == f"token:{secrets}/a.token"


def test_the_spawn_and_the_refusal_key_read_one_function(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`child`, `native` and the failover all use `owner_roster.credential_source`:
    a source it names is the file the Claude spawn reads and the key a
    refusal records."""
    from nspawn import child, owner_roster
    from nspawn.owner_roster import Provider

    assert child.credential_source is owner_roster.credential_source
    assert native.credential_source is owner_roster.credential_source
    assert supervisor.credential_source is owner_roster.credential_source
    secret = tmp_path / "chosen.token"
    secret.write_bytes(b"from-the-source")
    chosen = owner_roster.CredentialSource("token", str(secret))
    monkeypatch.setattr(child, "credential_source", lambda *_: chosen)
    monkeypatch.setattr(supervisor, "credential_source", lambda *_: chosen)
    provider = Provider(name="claude")
    assert child._read_credential_bytes(provider, "a", {}) == b"from-the-source"
    assert supervisor._credential(provider, "a") == f"token:{secret}"


def test_an_oserror_through_fail_run_keeps_the_walk(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    plan: Plan,
    sessions: Callable[..., Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A preparation refusal ends the member through `_fail_run`, and the
    credential read raises OSError once there: the failover goes back, the
    terminal waits for its retry, and the retry walks on."""
    _accounts_roster(roster_home, tmp_path, THREE_ACCOUNTS)
    plan.raises = {0: native.NativePreparationError("fenced for this test")}
    real = Supervisor._credentials_of
    raised: list[bool] = []

    def once(failover: Any, row: RunRow) -> frozenset[str]:
        if not raised:
            raised.append(True)
            raise OSError("the token directory is gone")
        return real(failover, row)

    monkeypatch.setattr(Supervisor, "_credentials_of", staticmethod(once))
    session = sessions()
    _swarm(
        registry,
        tmp_path,
        session,
        plan,
        provider="claude",
        before_shutdown=lambda sv: sv.publish_pending(),
    )
    assert raised == [True]
    chain = _chain(registry, plan.members[0])
    assert [row.phase for row in chain] == [RunPhase.FAILED, RunPhase.COMPLETED]
    # The member ends on its own refusal, never as a worker death.
    assert (chain[0].blocked_reason or "").startswith("NativePreparationError")


def test_a_credential_key_follows_the_secret_the_spawn_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The key is the token file `resolve_token_file` gives, and only without
    one the `auth_env` variable: the source `child._read_credential_bytes`
    reads."""
    from nspawn.owner_roster import Provider

    monkeypatch.setenv("HOME", str(tmp_path))
    key = supervisor._credential
    # Only auth_env: two providers on one variable share one key.
    assert key(Provider(name="zai", auth_env="ZAI_API_KEY"), None) == (
        "env:ZAI_API_KEY"
    )
    assert key(Provider(name="zai", auth_env="ZAI_API_KEY"), None) == key(
        Provider(name="zai-air", auth_env="ZAI_API_KEY"), None
    )
    # Both declared: the token file is the secret, so zai and kimi on one
    # variable name keep two keys.
    zai = Provider(
        name="zai",
        auth_env="ANTHROPIC_AUTH_TOKEN",
        oauth_token_file=Path("~/.nott/secrets/zai.token"),
    )
    kimi = Provider(
        name="kimi",
        auth_env="ANTHROPIC_AUTH_TOKEN",
        oauth_token_file=Path("~/.nott/secrets/kimi.token"),
    )
    assert key(zai, None) == f"token:{tmp_path}/.nott/secrets/zai.token"
    assert key(zai, None) != key(kimi, None)
    # The reverse: one token file under two variable names is one key, and
    # `~` and the absolute spelling agree.
    first = Provider(
        name="a", auth_env="FIRST_KEY", oauth_token_file=Path("~/shared.token")
    )
    second = Provider(
        name="b", auth_env="SECOND_KEY", oauth_token_file=tmp_path / "shared.token"
    )
    assert key(first, None) == key(second, None)
    assert key(Provider(name="bare"), None) == "route:bare"


def test_one_secret_behind_a_link_has_one_key(tmp_path: Path) -> None:
    """A secrets directory or a home reached through a symlink names the same
    file the spawn's fence reads: one key, so a refused token stays off."""
    from nspawn.owner_roster import NativeAccount, Provider

    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "a.token").write_text("x", encoding="utf-8")
    (tmp_path / "link").symlink_to(shared)
    direct = Provider(name="claude", secrets=shared)
    linked = Provider(name="sonnet", secrets=tmp_path / "link")
    assert supervisor._credential(direct, "a") == supervisor._credential(linked, "a")
    home = tmp_path / "codex-a"
    home.mkdir()
    (tmp_path / "codex-b").symlink_to(home)
    first = Provider(name="cx", kind="codex", accounts={"a": NativeAccount(home=home)})
    second = Provider(
        name="cy",
        kind="codex",
        accounts={"b": NativeAccount(home=tmp_path / "codex-b")},
    )
    assert supervisor._credential(first, "a") == supervisor._credential(second, "b")


def test_a_claude_child_refuses_a_native_home_source() -> None:
    """Exhaustive: a source the Claude child cannot read is refused by name,
    never read as a token file."""
    from nspawn import child
    from nspawn.owner_roster import NativeAccount, Provider

    provider = Provider(
        name="cx", kind="codex", accounts={"main": NativeAccount(home=Path("/h"))}
    )
    with pytest.raises(child.ChildEnvironmentError, match="not a Claude child's"):
        child._read_credential_bytes(provider, "main", {})


def test_native_prepare_finds_its_secret_through_credential_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The native spawn calls `credential_source` for its OAuth secret: a spy
    on the name native resolves at call time sees the selected account."""
    from test_native_environment import credential, prepare

    from nspawn.owner_roster import NativeAccount, Provider

    binary = tmp_path / "native-cli"
    binary.write_text("#!/bin/sh\nexit 99\n")
    binary.chmod(0o700)
    home = tmp_path / "conta-um"
    credential(home, "FIRST-OAUTH-SECRET-FOR-TESTING")
    provider = Provider(
        name="codex",
        kind="codex",
        bin=binary,
        model="luna",
        accounts={"one": NativeAccount(home)},
    )
    calls: list[tuple[str, str | None]] = []
    real = native.credential_source

    def spy(provider: Provider, alias: str | None) -> Any:
        calls.append((provider.name, alias))
        return real(provider, alias)

    monkeypatch.setattr(native, "credential_source", spy)
    prepare(provider, tmp_path)
    assert calls == [("codex", "one")]
