"""Node callers: a non-centre node's nSPAWN owns its runs; the host takes its notices.

The host sets `NOTT_NSPAWN_NODE_SESSION` on every non-centre node's nSPAWN, and
`NOTT_NSPAWN_GRANT=read` on an audit parent's. Every run such a door admits or
reattaches carries `owner = node:<session>`; the door shows, reads, closes,
resumes and lists only those; and their notices are the host's to take, renew,
ack and release, never pushed to a session socket nor printed by a drain.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time
import uuid
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import Any, NoReturn

import nott_database
import pytest
from class_dispatch import system1_never_reaches_the_network
from registry_reset import sql
from supervisor_support import (
    _drain_worker_threads,
    _stop_every_reaper,
    fake_picked,
    fake_provider,
    registry,
    roster_home,
    wired_success,
)
from test_dedup import _frame, _notices, _runs, _serve, parked
from test_driver import Drivers, Reaper, drv, reaper
from test_notice_outbox import PROJECT, _dead_caller, _drain, _lapse, _move, _notice

from nspawn import driver, mcp, notify
from nspawn import registry as store_module
from nspawn.cli import main
from nspawn.process_identity import capture_process_identity
from nspawn.registry import MAX_RUN_PAGE, Registry, RegistryError, RunKey
from nspawn.rows import Backend, RunPhase, RunRow
from nspawn.supervisor import (
    ClosePending,
    DuplicateSession,
    IdempotencyConflict,
    InvalidRunRequest,
    Supervisor,
    UnknownRun,
)

__all__ = [
    "_drain_worker_threads",
    "_stop_every_reaper",
    "drv",
    "fake_picked",
    "fake_provider",
    "parked",
    "reaper",
    "registry",
    "roster_home",
    "system1_never_reaches_the_network",
    "wired_success",
]


def _session(name: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"nspawn-test:{name}"))


def _rid(name: str) -> str:
    """A run id in the minted shape: the drain prints nothing else."""
    return _session(f"run:{name}")


SESSION = _session("node-a")
OWNER = f"node:{SESSION}"
OTHER = f"node:{_session('node-b')}"


def _row(
    registry: Registry,
    run_id: str,
    *,
    owner: str | None = None,
    phase: RunPhase = RunPhase.RUNNING,
    created_at: int = 1,
    **fields: Any,
) -> RunRow:
    return RunRow(
        run_id=run_id,
        worktree=f"/fixture/{run_id}",
        phase=phase,
        sandbox="read-only",
        model="m",
        owner_instance=registry.instance_id,
        created_at=created_at,
        updated_at=created_at,
        backend=Backend.CLAUDE,
        **({} if owner is None else {"owner": owner}),
        **fields,
    )


def _node_caller(owner: str = OWNER) -> store_module.NoticeCaller:
    me = os.getpid()
    return store_module.NoticeCaller(
        addr=owner,
        pid=me,
        start_ticks=capture_process_identity(me).start_ticks,
        project=PROJECT,
    )


def _admit(
    registry: Registry,
    run_id: str,
    owner: str = OWNER,
    *,
    caller: store_module.NoticeCaller | None = None,
    run_key: RunKey | None = None,
    group: str | None = None,
) -> None:
    """A node's run and its notice; with `caller`, an unowned run of that caller."""
    # The drain's orphan pass must not orphan the fixture's runs.
    registry.heartbeat_instance(store_module.now_ms())
    registry.admit_run(
        _row(
            registry,
            run_id,
            owner=None if caller is not None else owner,
            phase=RunPhase.PENDING,
            created_at=1000,
            group_name=group,
        ),
        False,
        1000,
        60_000,
        notice=_node_caller(owner) if caller is None else caller,
        run_key=run_key,
    )


def _generation(registry: Registry, run_id: str) -> int:
    notice = _notice(registry, run_id)
    assert notice is not None
    return int(notice["generation"])


# ---------------------------------------------------------------------------
# Identity: the node session is the caller, ahead of any socket.
# ---------------------------------------------------------------------------


def test_a_node_session_is_the_caller_ahead_of_the_socket_and_the_remote_session() -> (
    None
):
    socket = f"/fixture/session/{os.getppid()}.sock"
    assert driver.caller_identity({driver.CALLER_SOCKET_ENV: socket})["addr"] == socket
    caller = driver.caller_identity(
        {
            driver.NODE_SESSION_ENV: SESSION,
            driver.CALLER_SOCKET_ENV: socket,
            driver.REMOTE_SESSION_ENV: _session("remote"),
        }
    )
    assert caller is not None
    assert caller["addr"] == OWNER
    assert caller["pid"] == os.getpid()


def test_a_malformed_node_session_or_grant_is_refused_by_name() -> None:
    for bad in ("", "not-a-session", SESSION.upper(), f"{{{SESSION}}}"):
        with pytest.raises(driver.DriverUnavailable, match=driver.NODE_SESSION_ENV):
            driver.caller_identity({driver.NODE_SESSION_ENV: bad})
        with pytest.raises(driver.DriverUnavailable, match=driver.NODE_SESSION_ENV):
            driver.node_owner({driver.NODE_SESSION_ENV: bad})
    assert driver.node_owner({}) is None
    assert driver.node_owner({driver.NODE_SESSION_ENV: SESSION}) == OWNER
    assert driver.read_grant({}) is False
    assert driver.read_grant({driver.GRANT_ENV: "read"}) is True
    for bad in ("", "write", "READ", "read "):
        with pytest.raises(driver.DriverUnavailable, match=driver.GRANT_ENV):
            driver.read_grant({driver.GRANT_ENV: bad})


def test_the_door_reads_the_node_and_the_grant_from_its_environment(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOTT_NSPAWN_RUN_ROOT", str(tmp_path / "runs"))
    built: list[Supervisor] = []
    try:
        built.append(mcp._default_supervisor())
        assert (built[-1].owner, built[-1].read_grant) == (None, False)
        monkeypatch.setenv(driver.NODE_SESSION_ENV, SESSION)
        monkeypatch.setenv(driver.GRANT_ENV, "read")
        built.append(mcp._default_supervisor())
        assert (built[-1].owner, built[-1].read_grant) == (OWNER, True)
        monkeypatch.setenv(driver.NODE_SESSION_ENV, "not-a-session")
        with pytest.raises(driver.DriverUnavailable, match=driver.NODE_SESSION_ENV):
            mcp._default_supervisor()
    finally:
        for supervisor_ in built:
            supervisor_.shutdown()
            supervisor_._registry.close()


# ---------------------------------------------------------------------------
# Ownership: recorded at admission and reattachment, and immutable.
# ---------------------------------------------------------------------------


def test_every_run_a_node_admits_carries_its_owner_and_no_other_run_does(
    registry: Registry, tmp_path: Path, roster_home: Path, wired_success: None
) -> None:
    (tmp_path / "wt").mkdir()
    node = Supervisor(registry, tmp_path / "runs", owner=OWNER)
    centre = Supervisor(registry, tmp_path / "runs")
    owned = node.execute("read it", str(tmp_path / "wt"), read_only=True)
    unowned = centre.execute("read it", str(tmp_path / "wt"), read_only=True)
    owned_row, unowned_row = (
        registry.get_run(owned.run_id),
        registry.get_run(unowned.run_id),
    )
    assert owned_row is not None and owned_row.owner == OWNER
    assert unowned_row is not None and unowned_row.owner is None


def test_the_owner_never_moves_after_admission(registry: Registry) -> None:
    registry.insert_run(_row(registry, "owned", owner=OWNER))
    row = registry.get_run("owned")
    assert row is not None
    for moved in (OTHER, None):
        registry.update_run(replace(row, owner=moved, updated_at=row.updated_at + 1))
        after = registry.get_run("owned")
        assert after is not None and after.owner == OWNER


def _owned_by(owner: str) -> Any:
    def wrap(built: Supervisor) -> Supervisor:
        built.owner = owner
        return built

    return wrap


def _node_frame(
    drv: ModuleType, root: Path, owner: str = OWNER, **arguments: Any
) -> dict[str, Any]:
    frame = _frame(drv, root, **arguments)
    me = os.getpid()
    frame["caller"] = {
        "addr": owner,
        "pid": me,
        "start_ticks": capture_process_identity(me).start_ticks,
    }
    return frame


def test_a_node_reattaches_only_a_key_it_owns(
    drv: ModuleType,
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    reaper: Reaper,
    parked: None,
) -> None:
    node, other, centre = (
        Drivers(tmp_path / "runs", wrap=_owned_by(OWNER)),
        Drivers(tmp_path / "runs", wrap=_owned_by(OTHER)),
        Drivers(tmp_path / "runs"),
    )
    reaper.drivers.extend((node, other, centre))
    first = _serve(drv, _node_frame(drv, tmp_path), node)
    assert first["deduplicated"] is False, first
    run_id = first["run_id"]
    row = registry.get_run(run_id)
    assert row is not None and row.owner == OWNER
    (notice,) = _notices(registry, run_id, OWNER)
    assert notice["ready_at"] is None
    assert _serve(drv, _node_frame(drv, tmp_path), node) == {
        "run_id": run_id,
        "deduplicated": True,
        "phase": "spawning",
    }
    for drivers, frame in (
        (other, _node_frame(drv, tmp_path, OTHER)),
        (centre, _frame(drv, tmp_path)),
    ):
        ack = _serve(drv, frame, drivers)
        assert ack["refused"]["type"] == "IdempotencyConflict", ack
        assert run_id not in json.dumps(ack), "a refusal named another owner's run"
        assert _notices(registry, run_id, OTHER) == []
    # A key the centre admitted is no node's to reattach either.
    unowned = _serve(drv, _frame(drv, tmp_path, key="centre-key"), centre)
    assert unowned["deduplicated"] is False, unowned
    ack = _serve(drv, _node_frame(drv, tmp_path, key="centre-key"), node)
    assert ack["refused"]["type"] == "IdempotencyConflict", ack
    assert _notices(registry, unowned["run_id"], OWNER) == []
    assert sorted(_runs(registry)) == sorted([run_id, unowned["run_id"]])


def test_a_node_finds_or_releases_by_key_only_a_run_it_owns(
    registry: Registry, tmp_path: Path
) -> None:
    run_id = _rid("keyed-theirs")
    _admit(
        registry,
        run_id,
        OTHER,
        run_key=RunKey(
            launch_uuid=str(uuid.uuid4()),
            project=PROJECT,
            idempotency_key="k",
            fingerprint="0" * 64,
            prompt_sha256="0" * 64,
        ),
    )
    node = Supervisor(registry, tmp_path / "runs", owner=OWNER)
    for reach in (
        lambda: node.find_key(PROJECT, "k"),
        lambda: node.release_key(PROJECT, "k", 1.0),
    ):
        with pytest.raises(IdempotencyConflict) as refused:
            reach()
        assert run_id not in str(refused.value), "a refusal named another owner's run"
    for owner in (OTHER, None):
        found = Supervisor(registry, tmp_path / "runs", owner=owner).find_key(
            PROJECT, "k"
        )
        assert found is not None and found.run_id == run_id


# ---------------------------------------------------------------------------
# Scope: a node shows, reads, closes, resumes and lists only its own runs.
# ---------------------------------------------------------------------------


def test_a_node_shows_reads_and_closes_only_its_own_runs(
    registry: Registry, tmp_path: Path
) -> None:
    for run_id, owner in (("mine", OWNER), ("theirs", OTHER), ("centre", None)):
        registry.insert_run(_row(registry, run_id, owner=owner))
    node = Supervisor(registry, tmp_path / "runs", owner=OWNER)
    centre = Supervisor(registry, tmp_path / "runs")
    assert node.status("mine").run_id == "mine"
    assert node.result("mine").run_id == "mine"
    assert node.transcript("mine").run_id == "mine"
    for foreign in ("theirs", "centre"):
        for read in (node.status, node.result, node.transcript, node.close):
            with pytest.raises(UnknownRun, match="no run named"):
                read(foreign)
        row = registry.get_run(foreign)
        assert row is not None and row.phase is RunPhase.RUNNING
        assert centre.status(foreign).run_id == foreign
    assert node.close("mine").phase is RunPhase.CLOSED


def test_a_node_lists_only_its_own_runs_filtered_before_the_page_cap(
    registry: Registry, tmp_path: Path
) -> None:
    registry.insert_run(_row(registry, "mine", owner=OWNER, created_at=1))
    for index in range(MAX_RUN_PAGE + 2):
        registry.insert_run(
            _row(registry, f"theirs-{index:03d}", owner=OTHER, created_at=100 + index)
        )
    node = Supervisor(registry, tmp_path / "runs", owner=OWNER)
    centre = Supervisor(registry, tmp_path / "runs")
    assert [run.run_id for run in node.fleet().runs] == ["mine"]
    listed = [run.run_id for run in centre.fleet().runs]
    assert len(listed) == MAX_RUN_PAGE
    assert "mine" not in listed


def test_a_nodes_fleet_shows_only_its_own_notices(
    registry: Registry, tmp_path: Path
) -> None:
    mine, theirs, socket = _rid("fleet-mine"), _rid("fleet-theirs"), _rid("socket")
    _admit(registry, mine)
    _admit(registry, theirs, OTHER)
    _admit(registry, socket, caller=_dead_caller())
    for run_id in (mine, theirs, socket):
        _move(registry, run_id, RunPhase.RUNNING, RunPhase.COMPLETED)
    node = Supervisor(registry, tmp_path / "runs", owner=OWNER)
    centre = Supervisor(registry, tmp_path / "runs")
    assert [notice.run_id for notice in node.fleet().notices] == [mine]
    assert {notice.run_id for notice in centre.fleet().notices} == {
        mine,
        theirs,
        socket,
    }


def test_a_node_resumes_only_a_session_its_own_runs_recorded(
    registry: Registry, tmp_path: Path, roster_home: Path, wired_success: None
) -> None:
    (tmp_path / "wt").mkdir()
    cwd = str(tmp_path / "wt")
    registry.insert_run(_row(registry, "theirs", owner=OTHER, thread_id="t-theirs"))
    registry.insert_run(_row(registry, "mine", owner=OWNER, thread_id="t-mine"))
    node = Supervisor(registry, tmp_path / "runs", owner=OWNER)
    for session in ("t-theirs", "t-nobody"):
        with pytest.raises(InvalidRunRequest, match="names no run"):
            node.execute("again", cwd, session_id=session, read_only=True)
    assert sorted(_runs(registry)) == ["mine", "theirs"]
    # Its own session passes the owner, and meets the session's own rule next.
    with pytest.raises(DuplicateSession):
        node.execute("again", cwd, session_id="t-mine", read_only=True)
    centre = Supervisor(registry, tmp_path / "runs")
    with pytest.raises(DuplicateSession):
        centre.execute("again", cwd, session_id="t-theirs", read_only=True)


def test_the_read_grant_admits_only_caged_dispatch(
    registry: Registry, tmp_path: Path, roster_home: Path, wired_success: None
) -> None:
    (tmp_path / "wt").mkdir()
    cwd = str(tmp_path / "wt")
    audit = Supervisor(registry, tmp_path / "runs", owner=OWNER, read_grant=True)
    refused: list[dict[str, Any]] = [
        {},
        {"read_only": False},
        {"native_write": True},
        {"read_only": True, "native_write": True},
        {"read_only": True, "orchestrator": {"session": "s"}},
    ]
    for arguments in refused:
        with pytest.raises(InvalidRunRequest, match=driver.GRANT_ENV):
            audit.execute("write it", cwd, **arguments)
    assert _runs(registry) == []
    accepted = audit.execute("read it", cwd, read_only=True)
    row = registry.get_run(accepted.run_id)
    assert row is not None and row.owner == OWNER


# ---------------------------------------------------------------------------
# The outbox: a node's notice is the host's, never a session's.
# ---------------------------------------------------------------------------


def test_a_node_notice_is_never_pushed_nor_drained_even_when_stale(
    registry: Registry,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sent: list[str] = []

    def send(addr: str, body: str, **_: object) -> bool:
        sent.append(addr)
        return True

    monkeypatch.setattr(notify, "send_notice", send)
    run_id = _rid("never-pushed")
    _admit(registry, run_id)
    _move(registry, run_id, RunPhase.RUNNING, RunPhase.COMPLETED)
    assert registry.ready_notices() == []
    assert notify.push_ready(registry) == 0
    _lapse(registry, run_id, ready_age_s=10 * store_module.NOTICE_STALE_S)
    registry.heartbeat_instance(store_module.now_ms())
    assert _drain(capsys) == []
    notice = _notice(registry, run_id)
    assert notice is not None and notice["delivered_at"] is None
    assert notice["caller_addr"] == OWNER
    assert sent == []


def test_an_undelivered_node_notice_outlives_the_retention(registry: Registry) -> None:
    pending, ready, socket = _rid("pending"), _rid("ready"), _rid("old-socket")
    held = _rid("held")
    for run_id in (pending, ready, held):
        _admit(registry, run_id)
    _admit(registry, socket, caller=_dead_caller())
    # `held` is armed first and claimed alone: the host holds it, undelivered.
    _move(registry, held, RunPhase.RUNNING, RunPhase.COMPLETED)
    assert [n.run_id for n in registry.take_notices("node:", 60_000, 10)] == [held]
    _move(registry, ready, RunPhase.RUNNING, RunPhase.COMPLETED)
    _move(registry, socket, RunPhase.RUNNING, RunPhase.COMPLETED)
    old = store_module.now_ms() - (store_module.NOTICE_RETENTION_DAYS + 1) * 86_400_000
    sql(registry, "UPDATE notices SET created_at = ?", (old,))
    assert registry.prune_notices() == 1
    assert _notice(registry, socket) is None
    assert _notice(registry, pending) is not None
    assert _notice(registry, ready) is not None
    claimed = _notice(registry, held)
    assert claimed is not None and claimed["claim_token"] is not None
    assert claimed["delivered_at"] is None
    # Delivered, it ages out like any other.
    sql(registry, "UPDATE notices SET delivered_at = ? WHERE run_id = ?", (old, ready))
    assert registry.prune_notices() == 1
    assert _notice(registry, ready) is None


@pytest.mark.parametrize(
    ("retain_ms", "max_closed"),
    [(0, 1_000), (10**12, 0), (0, 0)],
    ids=["by-age", "by-count", "both"],
)
def test_a_closed_run_with_a_pending_node_notice_outlives_run_pruning(
    registry: Registry, retain_ms: int, max_closed: int
) -> None:
    pending, held, socket = (
        _rid("prune-pending"),
        _rid("prune-held"),
        _rid("prune-socket"),
    )
    for run_id in (pending, held):
        _admit(
            registry,
            run_id,
            group="g",
            run_key=RunKey(
                launch_uuid=str(uuid.uuid4()),
                project=PROJECT,
                idempotency_key=run_id,
                fingerprint="0" * 64,
                prompt_sha256="0" * 64,
            ),
        )
    _admit(registry, socket, caller=_dead_caller())
    for run_id in (held, socket):
        _move(registry, run_id, RunPhase.RUNNING, RunPhase.COMPLETED, RunPhase.CLOSED)
    # Only `held` is armed here, so only it is claimed: `pending` stays as a host
    # that is down, or a `notice release` at host start, leaves it.
    assert [n.run_id for n in registry.take_notices("node:", 60_000, 10)] == [held]
    _move(registry, pending, RunPhase.RUNNING, RunPhase.COMPLETED, RunPhase.CLOSED)
    unclaimed = _notice(registry, pending)
    assert unclaimed is not None and unclaimed["claim_token"] is None
    assert unclaimed["delivered_at"] is None
    old = store_module.now_ms() - store_module.KEYED_RETENTION_MS - 86_400_000
    sql(registry, "UPDATE runs SET updated_at = ?", (old,))

    now = store_module.now_ms()
    assert registry.prune_closed_runs(now, retain_ms, max_closed) == 1
    assert registry.get_run(socket) is None
    registry.release_notices("node:")
    taken = {n.run_id: n for n in registry.take_notices("node:", 60_000, 10)}
    assert set(taken) == {pending, held}
    for run_id, notice in taken.items():
        assert registry.get_run(run_id) is not None
        assert (notice.group, notice.key) == ("g", run_id)

    # Acknowledged, the run ages out like any other.
    for run_id, notice in taken.items():
        assert registry.ack_notice(run_id, notice.generation, notice.token)
    assert registry.prune_closed_runs(now, retain_ms, max_closed) == 2
    assert registry.get_run(pending) is None and registry.get_run(held) is None


def test_every_rearm_bumps_the_notice_generation(registry: Registry) -> None:
    run_id = _rid("generations")
    _admit(registry, run_id)
    assert _generation(registry, run_id) == 0
    _move(registry, run_id, RunPhase.RUNNING, RunPhase.ORPHANED)
    assert _generation(registry, run_id) == 1
    _move(registry, run_id, RunPhase.RUNNING, RunPhase.COMPLETED)
    assert _generation(registry, run_id) == 2
    # A close after a final phase arms nothing: the generation stays.
    _move(registry, run_id, RunPhase.CLOSED)
    assert _generation(registry, run_id) == 2


def test_take_claims_each_free_node_notice_once_under_its_lease(
    registry: Registry,
) -> None:
    keyed, theirs, socket = _rid("keyed"), _rid("theirs"), _rid("take-socket")
    _admit(
        registry,
        keyed,
        group="g",
        run_key=RunKey(
            launch_uuid=str(uuid.uuid4()),
            project=PROJECT,
            idempotency_key="k",
            fingerprint="0" * 64,
            prompt_sha256="0" * 64,
        ),
    )
    _admit(registry, theirs, OTHER)
    _admit(registry, socket, caller=_dead_caller())
    _move(registry, keyed, RunPhase.RUNNING)
    row = registry.get_run(keyed)
    assert row is not None
    registry.update_run(
        replace(row, phase=RunPhase.FAILED, blocked_reason="boom", updated_at=9)
    )
    _move(registry, theirs, RunPhase.RUNNING, RunPhase.COMPLETED)
    _move(registry, socket, RunPhase.RUNNING, RunPhase.COMPLETED)

    taken = {
        notice.run_id: notice for notice in registry.take_notices("node:", 60_000, 10)
    }
    assert set(taken) == {keyed, theirs}, "a socket caller's notice was taken"
    first, other = taken[keyed], taken[theirs]
    assert (first.owner, first.generation, first.phase) == (OWNER, 1, "failed")
    assert (first.group, first.key, first.failure_reason) == ("g", "k", "boom")
    assert (other.owner, other.generation, other.phase) == (OTHER, 1, "completed")
    assert (other.group, other.key, other.failure_reason) == (None, None, None)
    assert first.token != other.token
    assert registry.take_notices("node:", 60_000, 10) == [], "a live claim was retaken"

    assert registry.renew_notice(keyed, 1, first.token, 60_000)
    assert not registry.renew_notice(keyed, 2, first.token, 60_000)
    assert not registry.renew_notice(keyed, 1, other.token, 60_000)
    assert not registry.ack_notice(keyed, 2, first.token)
    assert registry.ack_notice(keyed, 1, first.token)
    assert not registry.ack_notice(keyed, 1, first.token), "acked twice"
    acked = _notice(registry, keyed)
    assert acked is not None and acked["delivered_at"] is not None

    # The host's restart releases every claim it held; the old token is dead.
    assert registry.release_notices("node:") == 1
    (again,) = registry.take_notices("node:", 60_000, 10)
    assert (again.run_id, again.generation) == (theirs, 1)
    assert again.token != other.token
    assert not registry.ack_notice(theirs, 1, other.token)
    # A lapsed lease is free again, under another token.
    _lapse(registry, theirs)
    (third,) = registry.take_notices("node:", 60_000, 10)
    assert third.run_id == theirs and third.token != again.token
    assert not registry.renew_notice(theirs, 1, again.token, 60_000)
    assert registry.ack_notice(theirs, 1, third.token)
    assert registry.take_notices("node:", 60_000, 10) == []

    # The verbs never touch a socket caller's row, whatever token they carry.
    addr = _notice(registry, socket)["caller_addr"]  # type: ignore[index]
    claim = registry.claim_notice(socket, addr)
    assert claim is not None
    assert not registry.renew_notice(socket, 1, claim[0], 60_000)
    assert not registry.ack_notice(socket, 1, claim[0])
    for prefix in ("", "/fixture/", "remote:", "node"):
        with pytest.raises(RegistryError, match="node:"):
            registry.take_notices(prefix, 60_000, 10)
        with pytest.raises(RegistryError, match="node:"):
            registry.release_notices(prefix)


def test_a_rearmed_node_notice_is_taken_again_under_its_new_generation(
    registry: Registry,
) -> None:
    run_id = _rid("rearmed")
    _admit(registry, run_id)
    _move(registry, run_id, RunPhase.RUNNING, RunPhase.ORPHANED)
    (first,) = registry.take_notices(OWNER, 60_000, 10)
    assert (first.generation, first.phase) == (1, "orphaned")
    _move(registry, run_id, RunPhase.RUNNING, RunPhase.COMPLETED)
    assert not registry.ack_notice(run_id, 1, first.token), "a stale generation acked"
    (second,) = registry.take_notices(OWNER, 60_000, 10)
    assert (second.generation, second.phase) == (2, "completed")
    assert registry.ack_notice(run_id, 2, second.token)
    assert registry.take_notices(OWNER, 60_000, 10) == []


# ---------------------------------------------------------------------------
# The host's verbs.
# ---------------------------------------------------------------------------


def _json(capsys: pytest.CaptureFixture[str]) -> Any:
    captured = capsys.readouterr()
    assert captured.err == ""
    return json.loads(captured.out)


@pytest.mark.parametrize("source", ["stdin", "file"])
def test_the_notice_verbs_answer_json_and_refuse_by_name(
    registry: Registry,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
) -> None:
    run_id = _rid("cli")
    _admit(registry, run_id)
    _move(registry, run_id, RunPhase.RUNNING, RunPhase.COMPLETED)
    capsys.readouterr()
    take = ["notice", "take", "--owner-prefix", "node:", "--lease-ms", "60000"]
    assert main([*take, "--limit", "5"]) == 0
    (notice,) = _json(capsys)["notices"]
    assert notice == {
        "runId": run_id,
        "owner": OWNER,
        "generation": 1,
        "token": notice["token"],
        "phase": "completed",
        "group": None,
        "key": None,
        "failureReason": None,
    }
    held = ["--run", run_id, "--generation", "1"]
    token_file = tmp_path / "claim-token"
    token_file.write_text(notice["token"] + "\n")
    token_file.chmod(0o600)
    held += ["--stdin"] if source == "stdin" else ["--from-file", str(token_file)]

    def invoke(verb: str, *extra: str) -> int:
        monkeypatch.setattr(
            sys,
            "stdin",
            io.TextIOWrapper(io.BytesIO((notice["token"] + "\n").encode())),
        )
        return main(["notice", verb, *held, *extra])

    assert invoke("renew", "--lease-ms", "60000") == 0
    assert _json(capsys) == {"renewed": True}
    assert invoke("ack") == 0
    assert _json(capsys) == {"acked": True}
    assert invoke("ack") == 0
    assert _json(capsys) == {"acked": False}
    assert main(["notice", "release", "--owner-prefix", "node:"]) == 0
    assert _json(capsys) == {"released": 0}
    started = time.monotonic()
    assert main([*take, "--wait-ms", "300"]) == 0
    assert _json(capsys) == {"notices": []}
    assert time.monotonic() - started >= 0.3
    for bad in (
        ["notice", "take"],
        ["notice", "take", "--owner-prefix", "/fixture/"],
        ["notice", "take", "--owner-prefix", "node:", "--limit", "0"],
        ["notice", "take", "--owner-prefix", "node:", "--wait-ms", "-1"],
        ["notice", "take", "--owner-prefix", "node:", "--lease-ms", "x"],
        ["notice", "renew", *held],
        ["notice", "ack", "--run", run_id, "--generation", "one", "--stdin"],
        ["notice", "ack", *held, "--extra", "1"],
        ["notice", "release"],
        ["notice", "release", "--owner-prefix", "remote:"],
    ):
        assert main(bad) == 2, bad
        captured = capsys.readouterr()
        assert captured.out == "", bad
        assert captured.err, bad


@pytest.mark.parametrize("verb", ["renew", "ack"])
@pytest.mark.parametrize(
    "source",
    [
        "argv",
        "missing",
        "both",
        "duplicate",
        "absent-file",
        "empty",
        "multiline",
        "invalid-utf8",
        "too-large",
        "absent-stdin",
        "closed-stdin",
    ],
)
def test_secret_notice_input_refuses_bad_sources_without_echo(
    registry: Registry,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    verb: str,
    source: str,
) -> None:
    from nspawn import route

    sentinel = "synthetic-notice-secret"
    token_file = tmp_path / "claim-token"
    token_file.write_text(sentinel)
    token_file.chmod(0o600)
    supplied = (sentinel + "\n").encode()
    if source == "empty":
        supplied = b""
    elif source == "multiline":
        supplied = (sentinel + "\nsecond").encode()
    elif source == "invalid-utf8":
        supplied = b"\xff"
    elif source == "too-large":
        supplied = b"x" * (route.MAX_PROFILE_BYTES + 1)
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(supplied)))
    if source == "absent-stdin":
        monkeypatch.setattr(sys, "stdin", None)
    elif source == "closed-stdin":
        sys.stdin.close()
    selectors = {
        "argv": ["--token", sentinel],
        "missing": [],
        "both": ["--stdin", "--from-file", str(token_file)],
        "duplicate": ["--stdin", "--stdin"],
        "absent-file": ["--from-file", str(tmp_path / "missing")],
    }.get(source, ["--stdin"])
    args = ["notice", verb, "--run", "run", "--generation", "1", *selectors]
    if verb == "renew":
        args += ["--lease-ms", "60000"]
    result = main(args)
    assert result == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    leaked = sentinel in captured.err
    assert leaked is False, "secret input appeared in an error"


@pytest.mark.parametrize("verb", ["renew", "ack"])
@pytest.mark.parametrize(
    "source", ["world-readable", "group-readable", "symlink", "directory", "fifo"]
)
def test_secret_notice_input_refuses_unsafe_files_without_blocking(
    registry: Registry, tmp_path: Path, verb: str, source: str
) -> None:
    sentinel = "synthetic-notice-secret"
    token_file = tmp_path / "claim-token"
    token_file.write_text(sentinel)
    token_file.chmod(0o600)
    supplied = token_file
    if source == "world-readable":
        token_file.chmod(0o644)
    elif source == "group-readable":
        token_file.chmod(0o640)
    elif source == "symlink":
        supplied = tmp_path / "linked-token"
        supplied.symlink_to(token_file)
    elif source == "directory":
        supplied = tmp_path
    elif source == "fifo":
        supplied = tmp_path / "token-pipe"
        os.mkfifo(supplied, 0o600)
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    database = store_module.store_path().parent
    command = [
        sys.executable,
        "-c",
        "import sys; from nspawn.cli import main; sys.exit(main(sys.argv[1:]))",
        "notice",
        verb,
        "--run",
        _rid("unsafe-file"),
        "--generation",
        "1",
        "--from-file",
        str(supplied),
    ]
    if verb == "renew":
        command += ["--lease-ms", "60000"]
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
            env={
                "PATH": "/usr/bin:/bin",
                "HOME": str(home),
                "TMPDIR": "/var/tmp",
                "NOTT_DATABASE_DIR": str(database),
            },
        )
    except subprocess.TimeoutExpired:
        blocked = True
    else:
        blocked = False
    assert not blocked, "secret file input blocked"
    assert result.returncode == 2
    assert result.stdout == ""
    leaked = sentinel in result.stderr
    assert leaked is False, "secret input appeared in an error"
    assert "Traceback" not in result.stderr
    assert "notice token input" in result.stderr


@pytest.mark.parametrize("verb", ["renew", "ack"])
def test_secret_notice_input_refuses_a_file_owned_by_another_uid(
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    verb: str,
) -> None:
    token_file = tmp_path / "claim-token"
    token_file.write_text("synthetic-notice-secret")
    token_file.chmod(0o600)
    fstat = os.fstat
    identity = token_file.stat()

    def other_owner(descriptor: int) -> os.stat_result:
        observed = fstat(descriptor)
        if (observed.st_dev, observed.st_ino) != (identity.st_dev, identity.st_ino):
            return observed
        fields = list(observed)
        fields[4] += 1
        return os.stat_result(fields)

    monkeypatch.setattr(os, "fstat", other_owner)
    args = [
        "notice",
        verb,
        "--run",
        _rid("wrong-owner"),
        "--generation",
        "1",
        "--from-file",
        str(token_file),
    ]
    if verb == "renew":
        args += ["--lease-ms", "60000"]
    assert main(args) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "notice token input" in captured.err
    leaked = "synthetic-notice-secret" in captured.err
    assert leaked is False, "secret input appeared in an error"


def test_a_store_failure_fails_each_store_verb_and_prints_no_answer(
    registry: Registry,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def busy(*_: object, **__: object) -> NoReturn:
        raise nott_database.BusyError("the store stayed locked")

    held = ["--run", "r", "--generation", "1", "--stdin"]
    for method, argv in (
        ("take_notices", ["notice", "take", "--owner-prefix", "node:"]),
        ("renew_notice", ["notice", "renew", *held, "--lease-ms", "5"]),
        ("ack_notice", ["notice", "ack", *held]),
        ("release_notices", ["notice", "release", "--owner-prefix", "node:"]),
        ("owned_runs", ["run", "list", "--owner", OWNER]),
        ("group_runs", ["run", "list", "--group", "g"]),
    ):
        monkeypatch.setattr(Registry, method, busy)
        monkeypatch.setattr(
            sys, "stdin", io.TextIOWrapper(io.BytesIO(b"fixture-token"))
        )
        assert main(argv) == 1, argv
        captured = capsys.readouterr()
        assert captured.out == "", argv
        assert "failed: the store stayed locked" in captured.err, argv


def test_run_list_and_close_by_owner_touch_only_that_owners_runs(
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("NOTT_NSPAWN_RUN_ROOT", str(tmp_path / "runs"))
    registry.insert_run(
        _row(registry, "live", owner=OWNER, created_at=10, group_name="g", tokens=7)
    )
    registry.insert_run(
        _row(registry, "done", owner=OWNER, phase=RunPhase.COMPLETED, created_at=20)
    )
    registry.insert_run(_row(registry, "theirs", owner=OTHER, created_at=30))
    registry.insert_run(_row(registry, "centre", created_at=40))
    capsys.readouterr()
    assert main(["run", "list", "--owner", OWNER]) == 0
    runs = _json(capsys)["runs"]
    assert [run["runId"] for run in runs] == ["done", "live"]
    assert runs[1] == {
        "runId": "live",
        "group": "g",
        "key": None,
        "phase": "running",
        "provider": None,
        "model": "m",
        "failureReason": None,
        "generation": None,
        "acked": True,
        # What the run spent as nSPAWN counted it: a Stop's row says its fronts' spend.
        "tokens": 7,
        # When nSPAWN admitted it: a Stop tells each front by the press that preceded it.
        "createdAt": 10,
    }
    assert main(["run", "close", "--owner", OWNER]) == 0
    assert _json(capsys) == {"closed": ["live"]}
    phases = {
        run_id: row.phase
        for run_id in ("live", "done", "theirs", "centre")
        if (row := registry.get_run(run_id)) is not None
    }
    assert phases == {
        "live": RunPhase.CLOSED,
        "done": RunPhase.COMPLETED,
        "theirs": RunPhase.RUNNING,
        "centre": RunPhase.RUNNING,
    }
    assert main(["run", "close", "--owner", OWNER]) == 0
    assert _json(capsys) == {"closed": []}
    # A node's own nSPAWN names no other node's runs.
    monkeypatch.setenv(driver.NODE_SESSION_ENV, _session("node-b"))
    for verb in ("list", "close"):
        assert main(["run", verb, "--owner", OWNER]) == 2
        assert capsys.readouterr().out == ""
    monkeypatch.delenv(driver.NODE_SESSION_ENV)
    for bad in (
        ["run", "list", "--owner", "node:not-a-session"],
        ["run", "list", "--owner", SESSION],
        ["run", "close"],
        ["run", "close", "--owner"],
        ["run", "close", "--owner", OWNER, "--owner", OWNER],
    ):
        assert main(bad) == 2, bad
        assert capsys.readouterr().out == "", bad


def test_run_list_by_owner_answers_every_run_not_a_page(
    registry: Registry, capsys: pytest.CaptureFixture[str]
) -> None:
    # The answer has no cursor: a cap would drop the oldest runs, open ones too.
    for index in range(MAX_RUN_PAGE + 2):
        registry.insert_run(_row(registry, f"r{index}", owner=OWNER, created_at=index))
    capsys.readouterr()
    assert main(["run", "list", "--owner", OWNER]) == 0
    assert len(_json(capsys)["runs"]) == MAX_RUN_PAGE + 2


def test_run_list_by_owner_names_each_runs_notice_generation_and_its_ack(
    registry: Registry, capsys: pytest.CaptureFixture[str]
) -> None:
    # The host counts a run's result pending until nSPAWN took the ack of the
    # notice's current generation; null before the notice was ever armed.
    run_id = _rid("listed-generation")
    _admit(registry, run_id)

    def listed() -> Any:
        capsys.readouterr()
        assert main(["run", "list", "--owner", OWNER]) == 0
        (run,) = _json(capsys)["runs"]
        return run["generation"], run["acked"]

    def ack() -> None:
        (notice,) = registry.take_notices(OWNER, 60_000, 10)
        assert registry.ack_notice(run_id, notice.generation, notice.token)

    assert listed() == (None, False)
    _move(registry, run_id, RunPhase.RUNNING, RunPhase.ORPHANED)
    assert listed() == (1, False)
    ack()
    assert listed() == (1, True)
    # A re-arm is a new result, not acked yet.
    _move(registry, run_id, RunPhase.RUNNING, RunPhase.COMPLETED)
    assert listed() == (2, False)
    ack()
    # A close after a final phase arms nothing: the ack still holds.
    _move(registry, run_id, RunPhase.CLOSED)
    assert listed() == (2, True)
    # A delivered notice the retention pruned leaves nothing to deliver or ack.
    registry._write("UPDATE notices SET created_at = 0 WHERE run_id = ?", (run_id,))
    assert registry.prune_notices() == 1
    assert listed() == (None, True)


def test_run_list_by_group_answers_every_run_of_the_group_with_owner_and_sandbox(
    registry: Registry,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A bounded change dispatches its writer and its reviewer under its workflow id as
    # their group, with no node owner: a failed attempt and its retry are both its runs.
    registry.admit_run(
        replace(
            _row(
                registry,
                "writer-0",
                phase=RunPhase.PENDING,
                created_at=10,
                group_name="wf",
                executed_provider="codex",
                blocked_reason="RunDeadlineExceeded",
            ),
            sandbox="workspace-write",
        ),
        False,
        10,
        60_000,
        run_key=RunKey(
            launch_uuid=str(uuid.uuid4()),
            project=PROJECT,
            idempotency_key="writer-key",
            fingerprint="0" * 64,
            prompt_sha256="0" * 64,
        ),
    )
    _move(registry, "writer-0", RunPhase.RUNNING, RunPhase.FAILED)
    for run_id, created_at, sandbox, fields in (
        ("writer-1", 20, "workspace-write", {"executed_provider": "codex"}),
        ("review-0", 30, "read-only", {"executed_provider": "codex"}),
        ("mine", 40, "policy-only", {"owner": OWNER}),
        ("elsewhere", 50, "read-only", {"group_name": "wf-2"}),
        ("ungrouped", 60, "read-only", {"group_name": None}),
    ):
        row = _row(
            registry, run_id, created_at=created_at, **({"group_name": "wf"} | fields)
        )
        registry.insert_run(replace(row, sandbox=sandbox))
    capsys.readouterr()
    assert main(["run", "list", "--group", "wf"]) == 0
    runs = _json(capsys)["runs"]
    # Every run of the group, newest first, whoever admitted it.
    assert [run["runId"] for run in runs] == [
        "mine",
        "review-0",
        "writer-1",
        "writer-0",
    ]
    assert runs[3] == {
        "runId": "writer-0",
        "group": "wf",
        "key": "writer-key",
        "phase": "failed",
        "provider": "codex",
        "model": "m",
        "failureReason": "RunDeadlineExceeded",
        "owner": None,
        "sandbox": "workspace-write",
    }
    assert [(run["owner"], run["sandbox"]) for run in runs[:3]] == [
        (OWNER, "policy-only"),
        (None, "read-only"),
        (None, "workspace-write"),
    ]
    assert main(["run", "list", "--group", "no-such-group"]) == 0
    assert _json(capsys) == {"runs": []}
    # A node's own nSPAWN names no other node's runs, so it lists no group.
    monkeypatch.setenv(driver.NODE_SESSION_ENV, SESSION)
    assert main(["run", "list", "--group", "wf"]) == 2
    assert capsys.readouterr().out == ""
    monkeypatch.delenv(driver.NODE_SESSION_ENV)
    for bad in (
        ["run", "list", "--group"],
        ["run", "list", "--group", " "],
        ["run", "list", "--group", "wf", "--group", "wf"],
        ["run", "list", "--group", "wf", "--owner", OWNER],
    ):
        assert main(bad) == 2, bad
        assert capsys.readouterr().out == "", bad


def test_run_close_by_owner_before_a_time_closes_only_runs_admitted_by_then(
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A Stop owes a close to every run its node admitted up to the Stop, and to no
    # run admitted after it: `--before` is compared with each run's admission.
    monkeypatch.setenv("NOTT_NSPAWN_RUN_ROOT", str(tmp_path / "runs"))
    for run_id, at in (("early", 10), ("edge", 20), ("late", 21)):
        registry.insert_run(_row(registry, run_id, owner=OWNER, created_at=at))
    registry.insert_run(_row(registry, "theirs", owner=OTHER, created_at=5))
    capsys.readouterr()
    assert main(["run", "close", "--owner", OWNER, "--before", "20"]) == 0
    assert _json(capsys) == {"closed": ["early", "edge"]}
    # Nothing is left before the cutoff: a repeated close closes nothing.
    assert main(["run", "close", "--owner", OWNER, "--before", "20"]) == 0
    assert _json(capsys) == {"closed": []}
    phases = {
        run_id: row.phase
        for run_id in ("early", "edge", "late", "theirs")
        if (row := registry.get_run(run_id)) is not None
    }
    assert phases == {
        "early": RunPhase.CLOSED,
        "edge": RunPhase.CLOSED,
        "late": RunPhase.RUNNING,
        "theirs": RunPhase.RUNNING,
    }
    for bad in (
        ["run", "close", "--owner", OWNER, "--before"],
        ["run", "close", "--owner", OWNER, "--before", "soon"],
        ["run", "close", "--owner", OWNER, "--before", "-1"],
        ["run", "close", "--owner", OWNER, "--before", "1", "--before", "2"],
        ["run", "close", "--owner", OWNER, "--run", "early"],
    ):
        assert main(bad) == 2, bad
        assert capsys.readouterr().out == "", bad


def test_run_close_by_owner_waits_on_every_close_at_once_so_a_stuck_run_blocks_none(
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Two runs whose live drivers never answer their close, admitted before a
    # healthy one: each waits out its own bound, together, and the healthy one
    # closes. Serially the command outlasts the host's 60 s for any two of them.
    monkeypatch.setenv("NOTT_NSPAWN_RUN_ROOT", str(tmp_path / "runs"))
    for run_id, at in (("stuck-1", 1), ("stuck-2", 2), ("healthy", 3)):
        registry.insert_run(_row(registry, run_id, owner=OWNER, created_at=at))
    close = Supervisor.close
    wait = 1.0

    def unanswered(self: Supervisor, run_id: str) -> Any:
        if run_id.startswith("stuck"):
            time.sleep(wait)
            raise ClosePending(f"run {run_id!r}: no answer; the request stands")
        return close(self, run_id)

    monkeypatch.setattr(Supervisor, "close", unanswered)
    capsys.readouterr()
    began = time.monotonic()
    assert main(["run", "close", "--owner", OWNER, "--before", "3"]) == 2
    elapsed = time.monotonic() - began
    assert elapsed < 1.8 * wait, elapsed
    # A refusal prints nothing on stdout; stderr names each run's outcome.
    answered = capsys.readouterr()
    assert answered.out == ""
    lines = answered.err.splitlines()
    for run_id in ("stuck-1", "stuck-2"):
        assert any(
            line.startswith(f"nspawn run close alive: {run_id}:") for line in lines
        )
    assert "nspawn run close closed: healthy" in lines
    healthy = registry.get_run("healthy")
    assert healthy is not None and healthy.phase is RunPhase.CLOSED


def test_a_node_dispatch_without_a_deadline_is_refused_by_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Audit 2026-10-09: the node parent sent replacement fronts with no
    deadline. A node's run_start must carry timeout_ms; a session's need not."""
    from mcp_fakes import FakeSupervisor

    from nspawn.mcp import McpError, McpServer
    from nspawn.supervisor import RunAccepted

    server = McpServer(supervisor=FakeSupervisor(execute_result=RunAccepted("run-1")))
    monkeypatch.delenv(driver.NODE_SESSION_ENV, raising=False)
    server.nspawn_run_start("p", "/", read_only=True)  # a session: no deadline asked
    monkeypatch.setenv(driver.NODE_SESSION_ENV, SESSION)
    for missing in ({}, {"timeout_ms": 0}):
        with pytest.raises(McpError, match="timeout_ms"):
            server.nspawn_run_start("p", "/", read_only=True, **missing)
    server.nspawn_run_start("p", "/", read_only=True, timeout_ms=600_000)
