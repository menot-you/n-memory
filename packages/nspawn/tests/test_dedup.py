"""U3: exact dispatch dedup — one key, one request, one run (D6).

Unit tier, in-process `serve` over real Supervisors on the real store: each
`Drivers()` call is one driver's own Supervisor on its own connection, so two
dispatches of one key race exactly as two driver processes would. A run that
must stay non-terminal is admitted with its worker stubbed out (`_parked`), so
the test, not a child, decides when it ends.
"""

from __future__ import annotations

import contextlib
import os
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import nott_database
import pytest
from class_dispatch import DECLARED_CLASS, system1_never_reaches_the_network
from registry_reset import sql
from supervisor_support import (
    _drain_worker_threads,
    _stop_every_reaper,
    _url,
    fake_picked,
    fake_provider,
    registry,
    roster_home,
    wired_success,
)
from test_driver import (
    Drivers,
    Reaper,
    _call,
    _payload,
    _record_pushes,
    _stdio,
    _terminal,
    _wait_for,
    _write_frame,
    drv,
    reaper,
    write_mode,
)

from nspawn import supervisor
from nspawn.mcp import McpServer
from nspawn.process_identity import capture_process_identity
from nspawn.registry import Registry
from nspawn.supervisor import Supervisor

__all__ = [
    "_drain_worker_threads",
    "_stop_every_reaper",
    "drv",
    "fake_picked",
    "fake_provider",
    "reaper",
    "registry",
    "roster_home",
    "system1_never_reaches_the_network",
    "wired_success",
]

KEY = "u3-key"


def _frame(
    drv: ModuleType, root: Path, key: str | None = KEY, **arguments: Any
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "prompt": "read it",
        "cwd": str(root),
        "execution_class": DECLARED_CLASS,
        "read_only": True,
        "idempotency_key": key,
        **arguments,
    }
    return dict(
        drv.make_frame("nspawn_run_start", entry, drivers_dir=root / "runs" / "drivers")
    )


def _drivers(tmp_path: Path, reaper: Reaper) -> Drivers:
    drivers = Drivers(tmp_path / "runs")
    reaper.drivers.append(drivers)
    return drivers


def _serve(
    drv: ModuleType, frame: dict[str, Any], drivers: Callable[[], Supervisor]
) -> dict[str, Any]:
    ack: dict[str, Any] = drv.serve(
        frame, lambda ack: None, supervisor_factory=drivers, owns_supervisor=False
    )
    return ack


def _runs(registry: Registry) -> list[str]:
    rows = sql(registry, "SELECT run_id FROM runs ORDER BY run_id").fetchall()
    return [str(row["run_id"]) for row in rows]


def _run_dirs(tmp_path: Path) -> list[str]:
    return sorted(
        path.name for path in (tmp_path / "runs").iterdir() if path.name != "drivers"
    )


def _notices(registry: Registry, run_id: str, addr: str) -> list[dict[str, Any]]:
    rows = sql(
        registry,
        "SELECT * FROM notices WHERE run_id = ? AND caller_addr = ?",
        (run_id, addr),
    ).fetchall()
    return [dict(row) for row in rows]


def _addr(name: str) -> str:
    """A caller address its pid names (`<pid>.sock`), one directory per caller."""
    return f"/fixture/{name}/{os.getpid()}.sock"


def _caller(addr: str) -> dict[str, Any]:
    return {
        "addr": addr,
        "pid": os.getpid(),
        "start_ticks": capture_process_identity(os.getpid()).start_ticks,
    }


@pytest.fixture
def parked(monkeypatch: pytest.MonkeyPatch, wired_success: None) -> None:
    """Admission is real; the worker is not: the run stays `spawning`."""
    monkeypatch.setattr(Supervisor, "_drive_run", lambda *args, **kwargs: None)


@contextlib.contextmanager
def _held(statement: str = "", params: tuple[object, ...] = ()) -> Iterator[None]:
    """The registry file's write lock, held by another thread (after `statement`,
    uncommitted) until the block ends; the held write then commits."""
    taken, release = threading.Event(), threading.Event()

    def hold() -> None:
        conn = nott_database.connect(_url())
        try:
            with nott_database.write(conn):
                if statement:
                    conn.execute(statement, params).close()
                taken.set()
                release.wait(60)
        finally:
            conn.close()

    thread = threading.Thread(target=hold, daemon=True)
    thread.start()
    assert taken.wait(10), "the holder never took the write lock"
    try:
        yield
    finally:
        release.set()
        thread.join(10)


# ---------------------------------------------------------------------------
# One key, one run.
# ---------------------------------------------------------------------------


@pytest.mark.needs("systemd_user", "cgroup", "bwrap")
def test_two_write_mode_drivers_with_one_key_admit_one_run(
    drv: ModuleType,
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    reaper: Reaper,
) -> None:
    admitting, entered = threading.Event(), threading.Event()
    with write_mode(
        tmp_path, monkeypatch, reaper, admitting=admitting, entered=entered
    ) as fx:
        admitted: list[str] = []
        admission = supervisor._refuse_undeclared_writer_root

        def counting(worktree: str, roster: Any) -> None:
            admitted.append(worktree)
            admission(worktree, roster)

        monkeypatch.setattr(supervisor, "_refuse_undeclared_writer_root", counting)
        acks: list[dict[str, Any]] = []
        frames = []
        for _ in range(2):
            frame = _write_frame(drv, tmp_path)
            frame["arguments"]["idempotency_key"] = KEY
            frames.append(frame)

        def serve(frame: dict[str, Any]) -> None:
            acks.append(
                drv.serve(
                    frame,
                    lambda ack: None,
                    supervisor_factory=fx.drivers,
                    owns_supervisor=True,
                )
            )

        reaper.thread(lambda: serve(frames[0]))
        assert entered.wait(20)
        reaper.thread(lambda: serve(frames[1]))
        time.sleep(0.5)  # the second driver reaches the key lock and waits on it
        admitting.set()
        fx.gate.touch()
        _wait_for(lambda: len(acks) == 2, 40, "both drivers to answer")
        assert len(admitted) == 1
        assert "refused" not in acks[0] and "refused" not in acks[1], acks
        assert {ack["run_id"] for ack in acks} == set(_runs(registry))
        assert len(_runs(registry)) == 1
        assert len(_run_dirs(tmp_path)) == 1
        assert sorted(ack["deduplicated"] for ack in acks) == [False, True]


def test_two_read_only_drivers_with_one_key_admit_one_run(
    drv: ModuleType,
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    reaper: Reaper,
    wired_success: None,
) -> None:
    drivers = _drivers(tmp_path, reaper)
    barrier = threading.Barrier(2)
    acks: list[dict[str, Any]] = []

    def serve() -> None:
        frame = _frame(drv, tmp_path)
        barrier.wait()
        acks.append(_serve(drv, frame, drivers))

    for _ in range(2):
        reaper.thread(serve)
    _wait_for(lambda: len(acks) == 2, 30, "both drivers to answer")
    assert len(_runs(registry)) == 1, acks
    assert len(_run_dirs(tmp_path)) == 1
    assert sorted(ack["deduplicated"] for ack in acks) == [False, True]
    assert acks[0]["run_id"] == acks[1]["run_id"]


@pytest.mark.parametrize(
    "change",
    [
        {"read_only": False},
        {"prompt": "read something else"},
        {"cwd": "other"},
        {"session_id": "3f2b8c3e-0d7a-4b8e-9d65-5f2f5b0d8a11"},
        {"execution_class": "review"},
        {"group": "another-group"},
    ],
    ids=["mode", "prompt", "cwd", "session_id", "class", "group"],
)
def test_a_different_request_under_one_key_is_an_idempotency_conflict(
    drv: ModuleType,
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    reaper: Reaper,
    parked: None,
    change: dict[str, Any],
) -> None:
    drivers = _drivers(tmp_path, reaper)
    first = _serve(drv, _frame(drv, tmp_path, group="g"), drivers)
    assert first["deduplicated"] is False
    arguments: dict[str, Any] = {"group": "g", **change}
    if arguments.get("cwd") == "other":
        (tmp_path / "other").mkdir()
        arguments["cwd"] = str(tmp_path / "other")
    frame = _frame(drv, tmp_path, **arguments)
    ack = _serve(drv, frame, drivers)
    assert ack["refused"]["type"] == "IdempotencyConflict", ack
    assert ack["refused"]["run_id"] == first["run_id"]
    assert "run_id" not in ack, "a conflict names the keyed run, never as failed"
    assert _runs(registry) == [first["run_id"]]


def test_a_retry_through_a_trailing_slash_or_a_symlink_reattaches(
    drv: ModuleType,
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    reaper: Reaper,
    parked: None,
) -> None:
    drivers = _drivers(tmp_path, reaper)
    tree = tmp_path / "tree"
    tree.mkdir()
    link = tmp_path / "link"
    link.symlink_to(tree)
    first = _serve(
        drv, _frame(drv, tmp_path, cwd=str(tree), read_paths=[str(tree)]), drivers
    )
    for cwd in (f"{tree}/", str(link)):
        again = _serve(
            drv, _frame(drv, tmp_path, cwd=cwd, read_paths=[f"{link}/"]), drivers
        )
        assert again == {
            "run_id": first["run_id"],
            "deduplicated": True,
            "phase": "spawning",
        }


def test_a_keyed_dispatch_from_a_non_git_cwd_is_keyed_by_that_cwd(
    drv: ModuleType,
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    reaper: Reaper,
    parked: None,
) -> None:
    drivers = _drivers(tmp_path, reaper)
    monkeypatch.chdir(tmp_path)
    first = _serve(drv, _frame(drv, tmp_path), drivers)
    assert first["deduplicated"] is False
    row = sql(
        registry,
        "SELECT project, idempotency_key FROM run_keys WHERE run_id = ?",
        (first["run_id"],),
    ).fetchone()
    assert dict(row) == {"project": os.path.realpath(tmp_path), "idempotency_key": KEY}
    assert _serve(drv, _frame(drv, tmp_path), drivers)["deduplicated"] is True


def test_every_admitted_run_has_its_run_keys_row_keyed_or_not(
    drv: ModuleType,
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    reaper: Reaper,
    parked: None,
) -> None:
    drivers = _drivers(tmp_path, reaper)
    frame = _frame(drv, tmp_path, key=None)
    ack = _serve(drv, frame, drivers)
    row = sql(
        registry,
        "SELECT launch_uuid, project, idempotency_key FROM run_keys WHERE run_id = ?",
        (ack["run_id"],),
    ).fetchone()
    assert dict(row) == {
        "launch_uuid": frame["launch_uuid"],
        "project": frame["project"],
        "idempotency_key": None,
    }
    # No key, no dedup: the same request twice is two runs.
    assert (
        _serve(drv, _frame(drv, tmp_path, key=None), drivers)["run_id"] != ack["run_id"]
    )


@pytest.mark.needs("systemd_user", "cgroup", "bwrap")
def test_the_key_lock_timing_out_is_a_typed_retryable_refusal(
    drv: ModuleType,
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    reaper: Reaper,
) -> None:
    admitting, entered = threading.Event(), threading.Event()
    with write_mode(
        tmp_path, monkeypatch, reaper, admitting=admitting, entered=entered
    ) as fx:
        holder = _write_frame(drv, tmp_path)
        holder["arguments"]["idempotency_key"] = KEY
        reaper.thread(
            lambda: drv.serve(
                holder,
                lambda ack: None,
                supervisor_factory=fx.drivers,
                owns_supervisor=True,
            )
        )
        assert entered.wait(20)
        monkeypatch.setattr(drv, "LOCK_TIMEOUT_S", 0.5)
        answers: list[dict[str, Any]] = []
        second = _write_frame(drv, tmp_path)
        second["arguments"]["idempotency_key"] = KEY
        reaper.thread(
            lambda: answers.append(
                drv.serve(
                    second,
                    lambda ack: None,
                    supervisor_factory=fx.drivers,
                    owns_supervisor=True,
                )
            )
        )
        _wait_for(lambda: answers, 10, "the second driver's refusal")
        (ack,) = answers
        assert ack["refused"]["type"] == "IdempotencyBusy", ack
        assert ack["refused"]["retryable"] is True
        admitting.set()
        fx.gate.touch()


def test_a_key_holding_a_slash_is_refused_at_every_dispatching_tool(
    registry: Registry, tmp_path: Path
) -> None:
    server = McpServer(supervisor=_stdio(registry, tmp_path))
    tree = str(tmp_path)
    for tool, arguments in (
        ("nspawn_run_start", {"prompt": "p", "cwd": tree, "class": DECLARED_CLASS}),
        ("nspawn_review_start", {"branch": "lane/x", "cwd": tree}),
        ("nspawn_batch_start", {"prompt": "p", "cwd": tree, "providers": ["x"]}),
    ):
        answer = _call(server, tool, {**arguments, "idempotency_key": "a/b"})
        assert answer["error"]["code"] == -32602, (tool, answer)
        assert "/" in answer["error"]["message"]


# ---------------------------------------------------------------------------
# group, batch, the prune window.
# ---------------------------------------------------------------------------


def test_group_persists_and_reads_back_in_status(
    drv: ModuleType,
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    reaper: Reaper,
    parked: None,
) -> None:
    server = McpServer(
        supervisor=_stdio(registry, tmp_path),
        launcher=drv.InProcessLauncher(
            _drivers(tmp_path, reaper), owns_supervisor=False
        ),
    )
    reaper.launchers.append(server.launcher)
    ack = _payload(
        _call(
            server,
            "nspawn_run_start",
            {
                "prompt": "p",
                "cwd": str(tmp_path),
                "class": DECLARED_CLASS,
                "read_only": True,
                "group": "wave-1",
                "idempotency_key": KEY,
            },
        )
    )
    row = registry.get_run(ack["run_id"])
    assert row is not None and row.group_name == "wave-1"
    status = _payload(_call(server, "nspawn_run_show", {"run_id": ack["run_id"]}))
    assert status["group"] == "wave-1"


def test_a_keyed_batch_of_three_is_three_runs_and_repeated_the_same_three(
    drv: ModuleType,
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    reaper: Reaper,
    parked: None,
) -> None:
    monkeypatch.setattr(
        Supervisor,
        "declared_providers",
        lambda self, cwd: (frozenset({"a", "b", "c"}), Path("/fixture/roster.toml")),
    )
    server = McpServer(
        supervisor=_stdio(registry, tmp_path),
        launcher=drv.InProcessLauncher(
            _drivers(tmp_path, reaper), owns_supervisor=False
        ),
    )
    reaper.launchers.append(server.launcher)
    arguments = {
        "prompt": "p",
        "cwd": str(tmp_path),
        "providers": ["a", "b", "c"],
        "idempotency_key": KEY,
    }
    first = _payload(_call(server, "nspawn_batch_start", arguments))
    assert first["refused"] == []
    assert [run["deduplicated"] for run in first["runs"]] == [False] * 3
    assert all(run["phase"] == "spawning" for run in first["runs"])
    assert len(_runs(registry)) == 3
    keys = sql(
        registry, "SELECT idempotency_key FROM run_keys ORDER BY idempotency_key"
    ).fetchall()
    assert [row["idempotency_key"] for row in keys] == [f"{KEY}/{n}" for n in range(3)]
    again = _payload(_call(server, "nspawn_batch_start", arguments))
    assert [run["run_id"] for run in again["runs"]] == [
        run["run_id"] for run in first["runs"]
    ]
    assert [run["deduplicated"] for run in again["runs"]] == [True] * 3
    assert [run["phase"] for run in again["runs"]] == ["spawning"] * 3
    assert len(_runs(registry)) == 3


def test_the_count_prune_keeps_a_keyed_closed_run_inside_24h(
    drv: ModuleType,
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    reaper: Reaper,
    wired_success: None,
) -> None:
    drivers = _drivers(tmp_path, reaper)
    keyed = _serve(drv, _frame(drv, tmp_path), drivers)["run_id"]
    loose = _serve(drv, _frame(drv, tmp_path, key=None), drivers)["run_id"]
    owners = {built._registry.instance_id: built for built in drivers.built}
    for run_id in (keyed, loose):
        # Closed by its own driver: a live driver owner is never closed locally.
        owners[_terminal(registry, run_id, 20).owner_instance].close(run_id)
    now = int(time.time() * 1000)
    registry.prune_closed_runs(now, 24 * 60 * 60 * 1000, 0)
    assert _runs(registry) == [keyed]
    assert _serve(drv, _frame(drv, tmp_path), drivers) == {
        "run_id": keyed,
        "deduplicated": True,
        "phase": "closed",
    }


# ---------------------------------------------------------------------------
# Reattach: the notice is the ack, or one armed row — never none, never two.
# ---------------------------------------------------------------------------


def test_a_reattach_to_a_terminal_run_writes_no_row_and_its_ack_carries_the_phase(
    drv: ModuleType,
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    reaper: Reaper,
    wired_success: None,
) -> None:
    drivers = _drivers(tmp_path, reaper)
    first = _serve(drv, _frame(drv, tmp_path), drivers)
    _terminal(registry, first["run_id"], 20)
    for _ in range(2):  # the retry of a lost answer returns the phase again
        frame = _frame(drv, tmp_path)
        frame["caller"] = _caller(_addr("late"))
        ack = _serve(drv, frame, drivers)
        assert ack == {
            "run_id": first["run_id"],
            "deduplicated": True,
            "phase": "completed",
        }
        assert _notices(registry, first["run_id"], _addr("late")) == []
        persisted = tmp_path / "runs" / "drivers" / f"{frame['launch_uuid']}.ack"
        assert persisted.read_text() and ack["phase"] in persisted.read_text()


def test_a_reattach_to_a_live_run_is_armed_once_by_its_terminal(
    drv: ModuleType,
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    reaper: Reaper,
    parked: None,
) -> None:
    drivers = _drivers(tmp_path, reaper)
    first = _serve(drv, _frame(drv, tmp_path), drivers)
    frame = _frame(drv, tmp_path)
    frame["caller"] = _caller(_addr("joiner"))
    ack = _serve(drv, frame, drivers)
    assert ack == {"run_id": first["run_id"], "deduplicated": True, "phase": "spawning"}
    (row,) = _notices(registry, first["run_id"], _addr("joiner"))
    assert row["ready_at"] is None
    # The same caller reattaching again keeps its one row.
    _serve(drv, frame | {"launch_uuid": _frame(drv, tmp_path)["launch_uuid"]}, drivers)
    sql(
        registry,
        "UPDATE runs SET phase = 'failed' WHERE run_id = ?",
        (first["run_id"],),
    )
    (row,) = _notices(registry, first["run_id"], _addr("joiner"))
    assert row["ready_at"] is not None and row["phase"] == "failed"


def test_a_reattach_racing_the_terminal_commit_is_one_notice_for_its_caller(
    drv: ModuleType,
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    reaper: Reaper,
    parked: None,
) -> None:
    """The terminal transaction holds the run row when the reattach reads it:
    the reattach waits for that commit and answers the terminal phase, with no
    row. A read that did not wait would see `spawning`, insert a row after the
    trigger already fired, and the caller would never hear."""
    drivers = _drivers(tmp_path, reaper)
    first = _serve(drv, _frame(drv, tmp_path), drivers)
    answers: list[dict[str, Any]] = []
    frame = _frame(drv, tmp_path)
    frame["caller"] = _caller(_addr("racer"))
    with _held("UPDATE runs SET phase = 'failed' WHERE run_id = ?", (first["run_id"],)):
        reaper.thread(lambda: answers.append(_serve(drv, frame, drivers)))
        time.sleep(0.5)  # the reattach reaches its write and waits on the holder
    _wait_for(lambda: answers, 10, "the reattach's answer")
    (ack,) = answers
    assert ack == {"run_id": first["run_id"], "deduplicated": True, "phase": "failed"}
    assert _notices(registry, first["run_id"], _addr("racer")) == []


@pytest.mark.needs("systemd_user", "cgroup", "bwrap")
def test_a_repeated_keyed_dispatch_pushes_its_terminal_once_to_each_caller(
    drv: ModuleType,
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    reaper: Reaper,
) -> None:
    sent = _record_pushes(monkeypatch)
    with write_mode(tmp_path, monkeypatch, reaper) as fx:
        fx.drivers.announces = True
        frames = []
        for addr in (_addr("one"), _addr("two")):
            frame = _write_frame(drv, tmp_path)
            frame["arguments"]["idempotency_key"] = KEY
            frame["caller"] = _caller(addr)
            frames.append(frame)
        acks: list[dict[str, Any]] = []
        reaper.thread(
            lambda: drv.serve(
                frames[0],
                acks.append,
                supervisor_factory=fx.drivers,
                owns_supervisor=True,
            )
        )
        _wait_for(lambda: acks, 20, "the first driver's ack")
        second = drv.serve(
            frames[1],
            lambda ack: None,
            supervisor_factory=fx.drivers,
            owns_supervisor=True,
        )
        assert second["run_id"] == acks[0]["run_id"] and second["deduplicated"]
        fx.gate.touch()
        run_id = acks[0]["run_id"]
        _terminal(registry, run_id, 30)
        _wait_for(lambda: len(sent) >= 2, 10, "a push to each caller")
        time.sleep(0.5)
        assert len(sent) == 2 and all(f"{run_id} is completed" in b for b in sent)


# ---------------------------------------------------------------------------
# launch_uuid resolution, and the backstop.
# ---------------------------------------------------------------------------


def test_a_launch_with_no_ack_file_resolves_through_run_keys(
    drv: ModuleType,
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    reaper: Reaper,
    parked: None,
) -> None:
    monkeypatch.setattr(drv, "_persist_ack", lambda frame, ack: None)
    drivers = _drivers(tmp_path, reaper)
    frame = _frame(drv, tmp_path)
    ack = _serve(drv, frame, drivers)
    server = McpServer(supervisor=_stdio(registry, tmp_path))
    status = _payload(
        _call(server, "nspawn_run_show", {"launch_uuid": frame["launch_uuid"]})
    )
    assert status["run_id"] == ack["run_id"]


def test_a_unique_violation_on_the_key_is_answered_as_the_hit_it_is(
    drv: ModuleType,
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    reaper: Reaper,
    parked: None,
) -> None:
    """The backstop: a lookup that missed (it cannot under the lock, so the test
    blinds it once) meets the partial unique index, which is re-read as a hit."""
    drivers = _drivers(tmp_path, reaper)
    first = _serve(drv, _frame(drv, tmp_path), drivers)
    from nspawn import registry as store

    seen = store.KeyLock.find
    blinded: list[bool] = []

    def find(self: Any) -> Any:
        if not blinded:
            blinded.append(True)
            return None
        return seen(self)

    monkeypatch.setattr(store.KeyLock, "find", find)
    again = _serve(drv, _frame(drv, tmp_path), drivers)
    assert blinded
    assert again == {
        "run_id": first["run_id"],
        "deduplicated": True,
        "phase": "spawning",
    }
    assert _runs(registry) == [first["run_id"]]
    assert _run_dirs(tmp_path) == [first["run_id"]]


def test_a_fenced_key_is_refused_by_the_store_past_a_driver_that_never_reads_it(
    drv: ModuleType,
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    reaper: Reaper,
    parked: None,
) -> None:
    """A driver that opened the store before it carried fences never reads them.
    The store itself refuses the key's `run_keys` row, so no binary binds a run
    to a fenced key, and this driver answers that refusal as `KeyFenced`."""
    from nspawn import registry as store

    drivers = _drivers(tmp_path, reaper)
    frame = _frame(drv, tmp_path)
    sql(registry, "INSERT INTO key_fences VALUES (?, ?, 1)", (frame["project"], KEY))
    monkeypatch.setattr(store.KeyLock, "fenced", lambda self: False)
    ack = _serve(drv, frame, drivers)
    assert ack.get("refused", {}).get("type") == "KeyFenced", ack
    assert _runs(registry) == []
    assert _run_dirs(tmp_path) == []


@pytest.mark.parametrize("busy", [False, True])
def test_missing_only_fence_waits_for_admission_or_refuses_busy_without_fencing(
    drv: ModuleType,
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    reaper: Reaper,
    parked: None,
    busy: bool,
) -> None:
    entered, released, fencing = threading.Event(), threading.Event(), threading.Event()
    execute = Supervisor.execute
    release_key = Supervisor.release_key

    def delayed(self: Supervisor, *args: Any, **kwargs: Any) -> Any:
        entered.set()  # The real driver already owns the admission key lock.
        assert released.wait(10)
        return execute(self, *args, **kwargs)

    def fence(self: Supervisor, project: str, key: str, timeout_s: float) -> Any:
        fencing.set()
        return release_key(self, project, key, 0.05 if busy else timeout_s)

    monkeypatch.setattr(Supervisor, "execute", delayed)
    monkeypatch.setattr(Supervisor, "release_key", fence)
    drivers = _drivers(tmp_path, reaper)
    frame = _frame(drv, tmp_path)
    admissions: list[dict[str, Any]] = []
    answers: list[dict[str, Any]] = []
    request = {
        "tool": "nspawn_run_start",
        "arguments": {
            "prompt": "read it",
            "cwd": str(tmp_path),
            "class": DECLARED_CLASS,
            "read_only": True,
        },
    }
    server = McpServer(supervisor=_stdio(registry, tmp_path))
    reaper.thread(lambda: admissions.append(_serve(drv, frame, drivers)))
    try:
        assert entered.wait(10)
        reaper.thread(
            lambda: answers.append(
                _call(
                    server,
                    "nspawn_run_close",
                    {
                        "idempotency_key": KEY,
                        "request": request,
                        "only_unadmitted": True,
                    },
                )
            )
        )
        assert fencing.wait(10)
        if busy:
            _wait_for(lambda: answers, 10, "the guarded busy refusal")
            assert answers[0]["error"]["data"]["type"] == "IdempotencyBusy"
        else:
            assert not answers  # It cannot answer missing while admission owns custody.
        assert (
            sql(registry, "SELECT count(*) AS n FROM key_fences").fetchone()["n"] == 0
        )
    finally:
        released.set()
    _wait_for(lambda: admissions and answers, 10, "the admission and guarded answer")
    run_id = admissions[0]["run_id"]
    if not busy:
        found = _payload(answers[0])
        assert (found["lookup"], found["run_id"], found["only_unadmitted"]) == (
            "found",
            run_id,
            True,
        )
    same = _serve(drv, _frame(drv, tmp_path), drivers)
    assert same["run_id"] == run_id and same["deduplicated"] is True
    assert registry.get_run(run_id).phase.value == "spawning"
    assert sql(registry, "SELECT count(*) AS n FROM key_fences").fetchone()["n"] == 0


def test_missing_only_fence_precedes_and_refuses_an_old_driver(
    drv: ModuleType,
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    reaper: Reaper,
    parked: None,
) -> None:
    from nspawn import registry as store

    frame = _frame(drv, tmp_path)
    server = McpServer(supervisor=_stdio(registry, tmp_path))
    arguments = {
        "prompt": "read it",
        "cwd": str(tmp_path),
        "class": DECLARED_CLASS,
        "read_only": True,
    }
    fenced = _payload(
        _call(
            server,
            "nspawn_run_close",
            {
                "idempotency_key": KEY,
                "request": {"tool": "nspawn_run_start", "arguments": arguments},
                "only_unadmitted": True,
            },
        )
    )
    assert fenced == {
        "lookup": "fenced",
        "idempotency_key": KEY,
        "only_unadmitted": True,
    }
    # The native registry insertion backstop also fences an old driver that
    # never performs the advisory read introduced by a newer version.
    monkeypatch.setattr(store.KeyLock, "fenced", lambda self: False)
    ack = _serve(drv, frame, _drivers(tmp_path, reaper))
    assert ack.get("refused", {}).get("type") == "KeyFenced", ack
    assert _runs(registry) == [] and _run_dirs(tmp_path) == []


def test_a_review_fingerprints_its_base_name(drv: ModuleType, tmp_path: Path) -> None:
    def printed(base: str) -> str:
        fingerprint: str = drv.request_fingerprint(
            "nspawn_review_start",
            {"branch": "lane/x", "cwd": str(tmp_path), "base": base},
            group=None,
        )
        return fingerprint

    assert printed("main") == printed("main")
    assert printed("main") != printed("origin/main")


def test_a_reattach_to_an_orphaned_run_hears_the_terminal_it_moves_on_to(
    drv: ModuleType,
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    reaper: Reaper,
    parked: None,
) -> None:
    """`orphaned` is terminal yet not final: `update_run` moves it again and the
    trigger re-arms orphaned -> another terminal. The late caller's row is
    written armed and delivered as `orphaned` (the ack delivered that), so a
    truly dead run leaves nothing undelivered, and a revived one re-arms it."""
    drivers = _drivers(tmp_path, reaper)
    run_id = _serve(drv, _frame(drv, tmp_path), drivers)["run_id"]

    def move(phase: str) -> None:
        sql(
            registry,
            "UPDATE runs SET phase = ? WHERE run_id = ?",
            (phase, run_id),
        )

    move("orphaned")
    frame = _frame(drv, tmp_path)
    frame["caller"] = _caller(_addr("late"))
    ack = _serve(drv, frame, drivers)
    assert ack == {"run_id": run_id, "deduplicated": True, "phase": "orphaned"}
    (row,) = _notices(registry, run_id, _addr("late"))
    assert row["phase"] == "orphaned" and row["delivered_at"] is not None, (
        "a dead run's notice must not stay pending"
    )
    move("running")
    move("completed")
    (row,) = _notices(registry, run_id, _addr("late"))
    assert row["phase"] == "completed", "the late caller never hears completed"
    assert row["ready_at"] is not None and row["delivered_at"] is None


def test_a_run_row_held_past_the_lock_timeout_is_the_retryable_busy_refusal(
    drv: ModuleType,
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    reaper: Reaper,
    parked: None,
) -> None:
    """The reattach's write waits on the store's busy timeout: a write lock held
    past it is the same retryable refusal as the key lock, not a raw store
    error that leaves the caller with no ack."""
    # The wait is the store's own busy timeout (10 s); what it proves holds at 0.5 s.
    monkeypatch.setattr(nott_database, "BUSY_TIMEOUT_MS", 500)
    drivers = _drivers(tmp_path, reaper)
    _serve(drv, _frame(drv, tmp_path), drivers)
    held = contextlib.ExitStack()

    def hold_once_built(built: Supervisor) -> Supervisor:
        # Taken after the driver's supervisor exists, so its reattach is the
        # write that waits.
        held.enter_context(_held())
        return built

    drivers.wrap = hold_once_built
    with held:
        ack = _serve(drv, _frame(drv, tmp_path), drivers)
    assert ack["refused"]["type"] == "IdempotencyBusy", ack
    assert ack["refused"]["retryable"] is True


# ---------------------------------------------------------------------------
# Fingerprint edges.
# ---------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> str:
    import subprocess

    done = subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        },
    )
    return done.stdout.strip()


def test_a_review_whose_base_moved_is_another_request(
    drv: ModuleType, tmp_path: Path
) -> None:
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "commit", "-q", "--allow-empty", "-m", "one")
    _git(tmp_path, "branch", "lane/x")

    def printed(**base: str) -> str:
        fingerprint: str = drv.request_fingerprint(
            "nspawn_review_start",
            {"branch": "lane/x", "cwd": str(tmp_path), **base},
            group=None,
        )
        return fingerprint

    named, omitted = printed(base="main"), printed()
    assert printed(base="main") == named and printed() == omitted
    _git(tmp_path, "commit", "-q", "--allow-empty", "-m", "the base moves")
    assert printed(base="main") != named, "a moved named base was deduplicated"
    assert printed() != omitted, "a moved default base was deduplicated"


def test_a_git_timeout_reading_a_review_head_is_retryable_not_a_conflict(
    drv: ModuleType,
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    reaper: Reaper,
    parked: None,
) -> None:
    import subprocess

    def timed_out(*args: Any, **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(args[0], 10)

    monkeypatch.setattr(drv.subprocess, "run", timed_out)
    frame = dict(
        drv.make_frame(
            "nspawn_review_start",
            {"branch": "lane/x", "cwd": str(tmp_path), "idempotency_key": KEY},
            drivers_dir=tmp_path / "runs" / "drivers",
        )
    )
    ack = _serve(drv, frame, _drivers(tmp_path, reaper))
    assert ack["refused"]["retryable"] is True, ack
    assert ack["refused"]["type"] != "IdempotencyConflict"
    assert _runs(registry) == []


@pytest.mark.parametrize("field", ["cwd", "read_paths"])
def test_a_nul_byte_in_a_fingerprinted_path_is_a_typed_refusal(
    drv: ModuleType,
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    reaper: Reaper,
    parked: None,
    field: str,
) -> None:
    bad = f"{tmp_path}/a\x00b"
    frame = _frame(drv, tmp_path, **{field: bad if field == "cwd" else [bad]})
    ack = _serve(drv, frame, _drivers(tmp_path, reaper))
    assert ack["refused"]["type"] == "InvalidRunRequest", ack
    assert _runs(registry) == []


def test_a_key_and_a_group_the_schema_admits_fit_the_store_in_bytes() -> None:
    from nspawn.limits import MAX_DURABLE_IDENTIFIER_BYTES
    from nspawn.mcp import NSPAWN_TOOL_BY_NAME

    for tool in ("nspawn_run_start", "nspawn_review_start", "nspawn_batch_start"):
        properties = NSPAWN_TOOL_BY_NAME[tool].input_schema["properties"]
        for name in ("idempotency_key", "group"):
            # A code point is at most 4 UTF-8 bytes; a batch entry adds `/NN`.
            worst = properties[name]["maxLength"] * 4 + len("/99")
            assert worst <= MAX_DURABLE_IDENTIFIER_BYTES, (tool, name, worst)


def test_a_repeated_keyed_batch_with_more_providers_is_a_conflict(
    drv: ModuleType,
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    reaper: Reaper,
    parked: None,
) -> None:
    monkeypatch.setattr(
        Supervisor,
        "declared_providers",
        lambda self, cwd: (frozenset({"a", "b", "c"}), Path("/fixture/roster.toml")),
    )
    server = McpServer(
        supervisor=_stdio(registry, tmp_path),
        launcher=drv.InProcessLauncher(
            _drivers(tmp_path, reaper), owns_supervisor=False
        ),
    )
    reaper.launchers.append(server.launcher)
    arguments: dict[str, Any] = {
        "prompt": "p",
        "cwd": str(tmp_path),
        "providers": ["a", "b"],
        "idempotency_key": KEY,
    }
    first = _payload(_call(server, "nspawn_batch_start", arguments))
    assert len(first["runs"]) == 2
    more = _payload(
        _call(server, "nspawn_batch_start", {**arguments, "providers": ["a", "b", "c"]})
    )
    assert more["runs"] == [], more
    assert {entry["type"] for entry in more["refused"]} == {"IdempotencyConflict"}
    assert len(_runs(registry)) == 2


def test_a_half_admitted_keyed_batch_refuses_another_batch_under_its_key(
    drv: ModuleType,
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    reaper: Reaper,
    parked: None,
) -> None:
    """[a,b] with `a` walled admits only K/1; [a,c] under K must not admit `a`
    as K/0 beside it: K/0 and K/1 would answer two different requests."""
    from nspawn.errors import NERR_1112, NSpawnError

    monkeypatch.setattr(
        Supervisor,
        "declared_providers",
        lambda self, cwd: (frozenset({"a", "b", "c"}), Path("/fixture/roster.toml")),
    )
    execute = Supervisor.execute
    walled = {"a"}

    def maybe_walled(self: Supervisor, prompt: str, cwd: str, **kwargs: Any) -> Any:
        if kwargs.get("provider") in walled:
            raise NSpawnError(NERR_1112, "account wall is up")
        return execute(self, prompt, cwd, **kwargs)

    monkeypatch.setattr(Supervisor, "execute", maybe_walled)
    server = McpServer(
        supervisor=_stdio(registry, tmp_path),
        launcher=drv.InProcessLauncher(
            _drivers(tmp_path, reaper), owns_supervisor=False
        ),
    )
    reaper.launchers.append(server.launcher)
    arguments: dict[str, Any] = {
        "prompt": "p",
        "cwd": str(tmp_path),
        "providers": ["a", "b"],
        "idempotency_key": KEY,
    }
    first = _payload(_call(server, "nspawn_batch_start", arguments))
    assert [run["provider"] for run in first["runs"]] == ["b"], first
    walled.clear()
    other = _payload(
        _call(server, "nspawn_batch_start", {**arguments, "providers": ["a", "c"]})
    )
    assert other["runs"] == [], other
    assert {entry["type"] for entry in other["refused"]} == {"IdempotencyConflict"}
    assert len(_runs(registry)) == 1
    # The same batch again completes itself: `a` joins K/1 as K/0.
    again = _payload(_call(server, "nspawn_batch_start", arguments))
    assert [run["deduplicated"] for run in again["runs"]] == [False, True], again
    assert len(_runs(registry)) == 2


@pytest.mark.parametrize(
    ("tool", "field"),
    [
        ("nspawn_review_start", "branch"),
        ("nspawn_review_start", "base"),
        ("nspawn_run_start", "group"),
        ("nspawn_run_start", "idempotency_key"),
    ],
)
def test_a_nul_byte_in_a_dispatch_string_is_a_typed_refusal(
    drv: ModuleType,
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    reaper: Reaper,
    parked: None,
    tool: str,
    field: str,
) -> None:
    arguments: dict[str, Any] = {
        "cwd": str(tmp_path),
        "idempotency_key": KEY,
        field: "a\x00b",
    }
    if tool == "nspawn_review_start":
        arguments.setdefault("branch", "lane/x")
    else:
        arguments.update(prompt="p", read_only=True, execution_class=DECLARED_CLASS)
    frame = dict(
        drv.make_frame(tool, arguments, drivers_dir=tmp_path / "runs" / "drivers")
    )
    ack = _serve(drv, frame, _drivers(tmp_path, reaper))
    assert ack["refused"]["type"] == "InvalidRunRequest", ack
    assert "\x00" not in ack["refused"]["message"]
    assert _runs(registry) == []
