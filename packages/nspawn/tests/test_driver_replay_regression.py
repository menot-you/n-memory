from __future__ import annotations

import json
import os
import stat
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from class_dispatch import DECLARED_CLASS, system1_never_reaches_the_network
from registry_reset import reset_registry, sql, store
from supervisor_support import (
    _drain_worker_threads,
    _stop_every_reaper,
    fake_picked,
    fake_provider,
    roster_home,
    wired_success,
)
from test_driver import Drivers, Reaper, drv, reaper

from nspawn import config
from nspawn.cli import main
from nspawn.process_identity import capture_process_identity
from nspawn.registry import Registry, store_path
from nspawn.supervisor import Supervisor

__all__ = [
    "_drain_worker_threads",
    "_stop_every_reaper",
    "drv",
    "fake_picked",
    "fake_provider",
    "reaper",
    "roster_home",
    "system1_never_reaches_the_network",
    "wired_success",
]

KEY = "replay-key"
FREE_KEY = "replay-free-key"
UNSUPPORTED = {"orchestrator": {"session": "s"}}


@pytest.fixture
def private_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """This test's own store directory: named here, mode 0700, never the live one."""
    database = tmp_path / "database"
    database.mkdir(mode=0o700)
    monkeypatch.setenv("NOTT_DATABASE_DIR", str(database))
    assert stat.S_IMODE(database.stat().st_mode) == 0o700, "the store is not private"
    assert store_path().parent == database, "the registry is not in this directory"
    return database


@pytest.fixture
def registry(private_store: Path) -> Iterator[Registry]:
    """The real registry on that store, opened as supervisor_support opens it."""
    reg = Registry(str(store()), "i-replay").connect()
    reset_registry(reg, now=1000)
    yield reg
    reg.close()


@pytest.fixture
def run_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, roster_home: Path
) -> Path:
    """The run root that a stdin driver resolves: named here, mode 0700."""
    root = tmp_path / "runs"
    root.mkdir(mode=0o700)
    monkeypatch.setenv("NOTT_NSPAWN_RUN_ROOT", str(root))
    assert config.resolve_run_root() == root, "the driver resolves another run root"
    return root


@pytest.fixture
def started_workers(monkeypatch: pytest.MonkeyPatch, wired_success: None) -> list[str]:
    """Admission is real. The worker that launches the vendor only records its run."""
    started: list[str] = []
    monkeypatch.setattr(
        Supervisor,
        "_drive_run",
        lambda self, run_id, *args, **kwargs: started.append(run_id),
    )
    return started


def _address(caller: str) -> str:
    return f"/fixture/{caller}/{os.getpid()}.sock"


def _frame(
    drv: ModuleType, run_root: Path, caller: str, key: str, **extra: Any
) -> dict[str, Any]:
    """One keyed nspawn_run_start frame, as the stdio door writes it for a driver."""
    arguments: dict[str, Any] = {
        "prompt": "read it",
        "cwd": str(run_root.parent),
        "execution_class": DECLARED_CLASS,
        "read_only": True,
        "idempotency_key": key,
        **extra,
    }
    frame: dict[str, Any] = dict(
        drv.make_frame("nspawn_run_start", arguments, drivers_dir=run_root / "drivers")
    )
    frame["caller"] = {
        "addr": _address(caller),
        "pid": os.getpid(),
        "start_ticks": capture_process_identity(os.getpid()).start_ticks,
    }
    return frame


def _dispatch_run(run_root: Path, frame: dict[str, Any]) -> dict[str, Any]:
    """`nspawn dispatch run` in this process: the frame on stdin, the ack it persists.

    The driver moves 0, 1 and 2 to its log. They come back after it returns.
    """
    reader, writer = os.pipe()
    os.write(writer, json.dumps(frame).encode("utf-8"))
    os.close(writer)
    saved = [os.dup(number) for number in (0, 1, 2)]
    try:
        os.dup2(reader, 0)
        os.close(reader)
        code = main(["dispatch", "run"])
    finally:
        for number, copy in enumerate(saved):
            os.dup2(copy, number)
            os.close(copy)
    persisted = run_root / "drivers" / f"{frame['launch_uuid']}.ack"
    assert code == 0 and persisted.is_file(), (
        f"nspawn dispatch run exited {code} with no ack; "
        f"read {persisted.with_suffix('.log')}"
    )
    answer: dict[str, Any] = json.loads(persisted.read_text(encoding="utf-8"))
    return answer


def _notices(registry: Registry, run_id: str, caller: str) -> int:
    row = sql(
        registry,
        "SELECT count(*) AS n FROM notices WHERE run_id = ? AND caller_addr = ?",
        (run_id, _address(caller)),
    ).fetchone()
    return int(row["n"])


def _runs(registry: Registry) -> list[str]:
    rows = sql(registry, "SELECT run_id FROM runs ORDER BY run_id").fetchall()
    return [str(row["run_id"]) for row in rows]


def test_a_keyed_stdin_replay_with_the_removed_orchestrator_is_refused_before_reattach(
    drv: ModuleType,
    registry: Registry,
    run_root: Path,
    reaper: Reaper,
    started_workers: list[str],
) -> None:
    drivers = Drivers(run_root)
    reaper.drivers.append(drivers)
    # The ordinary keyed request. Its driver lives on, so the run stays live.
    first = drv.serve(
        _frame(drv, run_root, "first", KEY),
        lambda ack: None,
        supervisor_factory=drivers,
        owns_supervisor=False,
    )
    assert first.get("deduplicated") is False, first
    run_id = first["run_id"]

    free = _dispatch_run(
        run_root, _frame(drv, run_root, "free", FREE_KEY, **UNSUPPORTED)
    )
    replay = _dispatch_run(
        run_root, _frame(drv, run_root, "replay", KEY, **UNSUPPORTED)
    )
    control = _dispatch_run(run_root, _frame(drv, run_root, "control", KEY))

    assert set(free) == {"refused"} and "orchestrator" in free["refused"]["message"], (
        f"the frame under a free key must be refused by the field name: {free!r}"
    )
    assert control == {"run_id": run_id, "deduplicated": True, "phase": "spawning"}, (
        f"the identical keyed replay must reattach to the admitted run: {control!r}"
    )
    assert replay == free, (
        "a keyed replay that adds the removed orchestrator argument must get the "
        f"refusal that the same frame gets under a free key; it answered {replay!r}"
    )
    assert _notices(registry, run_id, "replay") == 0, "the refused replay reattached"
    assert _notices(registry, run_id, "control") == 1, "the control did not reattach"
    assert _runs(registry) == [run_id], "a replay admitted a run"
    made = sorted(path.name for path in run_root.iterdir() if path.name != "drivers")
    assert made == [run_id], f"a replay made a run directory: {made!r}"
    assert started_workers == [run_id], (
        f"a replay started a worker: {started_workers!r}"
    )
