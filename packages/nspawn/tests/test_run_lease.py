"""The run lease: a live nSPAWN run holds a shared flock in the git dir of its tree.

`tools/worktree.sh collect` holds the exclusive flock on the same file from its
check until the removal of the tree ends. Thus a tree with a live run stays,
and a dispatch into a tree under collection refuses before admission.

Each caller of admission gives the supervisor the lease as a callback on its
drive call: `serve` for a dispatch, `ping_routes` for a route ping. Each
admission path calls it with the store directory of its registry immediately
before its run directory. The holder process keeps the lease until it exits.
"""

from __future__ import annotations

import ast
import contextlib
import fcntl
import importlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import Mock

import nott_database
import pytest
from class_dispatch import (
    DECLARED_CLASS,
    system1_never_reaches_the_network,  # noqa: F401
)
from mcp_fakes import FakeSupervisor, handle
from registry_reset import sql, store
from test_native_execute import VENDOR, Client

import nspawn
from nspawn import child, claude_child, mcp, native, picker
from nspawn import supervisor as supervisor_module
from nspawn.mcp import McpServer
from nspawn.owner_roster import NativeAccount, Provider, Roster
from nspawn.redact import SecretNeedles
from nspawn.registry import STORE_FILE, EffortProfile, Registry, create_store
from nspawn.rows import NativeSessionContext, RunPhase
from nspawn.supervisor import DRIVE_CALL, RunAccepted, Supervisor

PACKAGE = Path(nspawn.__file__).resolve().parent


@pytest.fixture
def drv() -> ModuleType:
    """The driver module, imported per test so its absence reds each case."""
    return importlib.import_module("nspawn.driver")


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        env=os.environ
        | {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"},
    )


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A linked worktree of a disposable repository."""
    repo = tmp_path / "repo"
    _git(tmp_path, "init", "-q", "-b", "main", str(repo))
    _git(
        repo,
        "-c",
        "user.name=lease",
        "-c",
        "user.email=lease@test",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "base",
    )
    linked = tmp_path / "tree"
    _git(repo, "worktree", "add", "-q", "-b", "lane/lease", str(linked))
    return linked


def _lease(tmp_path: Path, name: str = "tree") -> Path:
    """The lease file in the git dir of a linked worktree."""
    return tmp_path / "repo" / ".git" / "worktrees" / name / "nott-run.lease"


def _frame(drv: ModuleType, cwd: Path, **arguments: Any) -> dict[str, Any]:
    return dict(
        drv.make_frame(
            "nspawn_run_start",
            {"prompt": "p", "cwd": str(cwd), **arguments},
            drivers_dir=None,
        )
    )


def _exclusive_refused(path: Path) -> bool:
    """True when a holder keeps `LOCK_EX | LOCK_NB` on `path` from this caller."""
    with path.open("a") as collector:
        try:
            fcntl.flock(collector, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
    return False


@dataclass
class LeasingSupervisor(FakeSupervisor):
    """`execute` stands for a real admission that takes the lease first.

    It records `execute`, calls the lease callback of the drive call with its
    `cwd` and `store`, records `admit` and returns its run. A refused callback
    leaves no `admit` record. With `park`, a worker waits until `release` is
    set. With `successor`, that worker first calls the callback of the same call
    again, as a failover successor does.
    """

    store: Path = field(default_factory=nott_database.directory)
    park: bool = False
    successor: bool = False
    release: threading.Event = field(default_factory=threading.Event)
    retaken: threading.Event = field(default_factory=threading.Event)

    def _take(self, call: Any, cwd: str) -> None:
        take = getattr(call, "take_lease", None)
        if take is not None:
            take(cwd, self.store)

    def execute(self, prompt: str, cwd: str, **kwargs: Any) -> RunAccepted:
        self.calls.append(("execute", {"prompt": prompt, "cwd": cwd}))
        call = DRIVE_CALL.get()
        self._take(call, cwd)
        if self.park:
            assert call is not None

            def work() -> None:
                if self.successor:
                    self._take(call, cwd)
                    self.retaken.set()
                self.release.wait(30)

            supervisor_module.start_worker(
                call, threading.Thread(target=work, daemon=True)
            )
        self.calls.append(("admit", {"cwd": cwd}))
        return RunAccepted(run_id="run-lease")


def _counting(drv: ModuleType, monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Count the calls of `nspawn.driver.take_run_lease` for each tree."""
    real = drv.take_run_lease
    counts: dict[str, int] = {}

    def counted(cwd: str, *rest: Any) -> Any:
        counts[cwd] = counts.get(cwd, 0) + 1
        return real(cwd, *rest)

    monkeypatch.setattr(drv, "take_run_lease", counted)
    return counts


def _serve_on_thread(
    drv: ModuleType, frame: dict[str, Any], fake: Supervisor, **kwargs: Any
) -> tuple[threading.Thread, list[dict[str, Any]], threading.Event]:
    acks: list[dict[str, Any]] = []
    acked = threading.Event()

    def write(ack: dict[str, Any]) -> None:
        acks.append(ack)
        acked.set()

    server = threading.Thread(
        target=drv.serve,
        args=(frame, write),
        kwargs={
            "supervisor_factory": lambda: fake,
            "owns_supervisor": False,
            **kwargs,
        },
        daemon=True,
    )
    server.start()
    return server, acks, acked


def test_a_live_run_holds_a_shared_lease_on_its_tree(
    drv: ModuleType, tree: Path, tmp_path: Path
) -> None:
    leases = drv.RunLeases()
    fake = LeasingSupervisor(park=True)
    server, acks, acked = _serve_on_thread(drv, _frame(drv, tree), fake, leases=leases)
    try:
        assert acked.wait(20), "the driver sent no ack"
        assert acks[0].get("run_id") == "run-lease", acks
        with _lease(tmp_path).open("a") as collector:
            with pytest.raises(BlockingIOError):
                fcntl.flock(collector, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Runs in one tree share the lease.
            fcntl.flock(collector, fcntl.LOCK_SH | fcntl.LOCK_NB)
    finally:
        fake.release.set()
        server.join(20)
    try:
        assert not server.is_alive(), "serve did not return after the worker ended"
        # `serve` does not close the holder of its caller: the driver process
        # keeps the lease until it exits.
        assert _exclusive_refused(_lease(tmp_path))
    finally:
        leases.close()
    assert not _exclusive_refused(_lease(tmp_path))


@dataclass
class Admission:
    """A real supervisor on a real registry, with no child that can start."""

    supervisor: Supervisor
    registry: Registry
    runs: Path
    #: Each call that reached `_drive_native_run`.
    driven: list[str]

    def assert_no_run(self) -> None:
        for table in ("runs", "claims", "run_keys"):
            assert sql(self.registry, f"SELECT * FROM {table}") == [], table
        assert list(self.runs.iterdir()) == []
        assert self.driven == []


@contextlib.contextmanager
def _real_admission(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    store_file: Path,
    drive: Callable[..., None] | None = None,
) -> Iterator[Admission]:
    """Only routing and native preparation are doubled; admission stays real."""
    provider = Provider(
        name="fixture",
        kind="codex",
        model="fixture-model",
        model_fast="fixture-fast",
        models=["fixture-model", "fixture-fast"],
    )
    roster = Roster(providers=[provider])
    picked = picker.PickerResult(
        None, provider, "fixture-account", "fixture-model", None, None
    )
    monkeypatch.setattr(supervisor_module, "read_roster", lambda _: roster)
    monkeypatch.setattr(picker, "pick_execution", lambda *args, **kwargs: picked)
    launch = native.NativeLaunch(
        (),
        {},
        SecretNeedles.none(),
        NativeSessionContext(str(tmp_path)),
        NativeAccount(tmp_path),
    )
    monkeypatch.setattr(native, "prepare_native", lambda *args, **kwargs: launch)
    driven: list[str] = []

    def fence(*args: Any, **kwargs: Any) -> None:
        driven.append("child")
        raise AssertionError("a refused admission reached its child")

    monkeypatch.setattr(Supervisor, "_drive_native_run", drive or fence)
    runs = tmp_path / "runs"
    with Registry(store_file, "lease-admitter") as registry:
        registry.open(1)
        supervisor = Supervisor(registry, runs)
        try:
            yield Admission(supervisor, registry, runs, driven)
        finally:
            supervisor.shutdown()


def test_a_tree_under_collection_refuses_the_dispatch_before_admission(
    drv: ModuleType, tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with (
        _real_admission(monkeypatch, tmp_path, store()) as admission,
        _lease(tmp_path).open("a") as collector,
    ):
        fcntl.flock(collector, fcntl.LOCK_EX | fcntl.LOCK_NB)
        ack = drv.serve(
            _frame(drv, tree, read_only=True),
            lambda ack: None,
            supervisor_factory=lambda: admission.supervisor,
            owns_supervisor=False,
        )
        assert "refused" in ack, ack
        assert ack["refused"]["type"] == "RunLeaseUnavailable", ack
        assert ack["refused"]["retryable"] is True, ack
        admission.assert_no_run()


def test_a_lease_unlinked_before_its_lock_refuses(
    drv: ModuleType, tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The collector removes the tree between the open and the flock of the driver.
    real_flock = fcntl.flock

    def unlink_then_lock(fd: Any, operation: int) -> None:
        _lease(tmp_path).unlink(missing_ok=True)
        real_flock(fd, operation)

    monkeypatch.setattr(fcntl, "flock", unlink_then_lock)
    fake = LeasingSupervisor()
    ack = drv.serve(
        _frame(drv, tree),
        lambda ack: None,
        supervisor_factory=lambda: fake,
        owns_supervisor=False,
    )
    assert "refused" in ack, ack
    assert ack["refused"]["type"] == "RunLeaseUnavailable", ack
    assert [name for name, _ in fake.calls] == ["execute"]


def test_a_failover_successor_keeps_the_lease_of_its_call(
    drv: ModuleType, tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    counts = _counting(drv, monkeypatch)
    fake = LeasingSupervisor(park=True, successor=True)
    server, acks, acked = _serve_on_thread(drv, _frame(drv, tree), fake)
    try:
        assert acked.wait(20), "the driver sent no ack"
        assert acks[0].get("run_id") == "run-lease", acks
        assert fake.retaken.wait(20), "the successor took no lease"
        assert _exclusive_refused(_lease(tmp_path))
        # The successor used the lease that its call already holds.
        assert counts == {str(tree): 1}
    finally:
        fake.release.set()
        server.join(20)
    assert not server.is_alive(), "serve did not return after the worker ended"
    # `serve` made this holder, so it closes it as its last act.
    assert not _exclusive_refused(_lease(tmp_path))


def test_a_run_outside_a_git_tree_takes_no_lease(
    drv: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    # git does not look for a repository above tmp_path.
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    fake = LeasingSupervisor()
    ack = drv.serve(
        _frame(drv, plain),
        lambda ack: None,
        supervisor_factory=lambda: fake,
        owns_supervisor=False,
    )
    assert ack.get("run_id") == "run-lease", ack
    assert [name for name, _ in fake.calls] == ["execute", "admit"]
    assert list(tmp_path.rglob("nott-run.lease")) == []


def test_a_tree_made_again_at_its_path_takes_a_new_lease(
    drv: ModuleType, tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    leases = drv.RunLeases()
    counts = _counting(drv, monkeypatch)
    try:
        leases.take(str(tree), tmp_path)
        leases.take(str(tree), tmp_path)
        assert counts == {str(tree): 1}
        repo = tmp_path / "repo"
        _git(repo, "worktree", "remove", "--force", str(tree))
        _git(repo, "worktree", "add", "-q", str(tree), "lane/lease")
        leases.take(str(tree), tmp_path)
        assert counts == {str(tree): 2}
        assert _exclusive_refused(_lease(tmp_path))
    finally:
        leases.close()


def _admission_methods() -> dict[str, ast.FunctionDef]:
    """Each method of `Supervisor` that calls `self._registry.admit_run`."""
    module = ast.parse((PACKAGE / "supervisor.py").read_text())
    (cls,) = (
        node
        for node in module.body
        if isinstance(node, ast.ClassDef) and node.name == "Supervisor"
    )
    methods: dict[str, ast.FunctionDef] = {}
    for method in cls.body:
        if not isinstance(method, ast.FunctionDef):
            continue
        for node in ast.walk(method):
            if (
                isinstance(node, ast.Call)
                and ast.unparse(node.func) == "self._registry.admit_run"
            ):
                methods[method.name] = method
    return methods


def _calls(function: ast.AST, name: str) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(function)
        if isinstance(node, ast.Call) and ast.unparse(node.func) == name
    ]


def test_each_admission_path_takes_the_lease_first() -> None:
    methods = _admission_methods()
    assert sorted(methods) == ["_activate", "_admit_attempt", "_command_start"]
    for name, method in methods.items():
        (lease,) = _calls(method, "self._hold_run_lease")
        (directory,) = _calls(method, "create_run_directory")
        assert (lease.lineno, lease.col_offset) < (
            directory.lineno,
            directory.col_offset,
        ), name


def test_the_discovery_lock_is_in_the_store_of_the_admitting_registry(
    drv: ModuleType, tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = tmp_path / "other-store"
    other.mkdir(mode=0o700)
    assert other != nott_database.directory()
    create_store(other / STORE_FILE)
    with (
        _real_admission(monkeypatch, tmp_path, other / STORE_FILE) as admission,
        (other / "worktree-collect.lock").open("a") as collector,
    ):
        fcntl.flock(collector, fcntl.LOCK_EX | fcntl.LOCK_NB)
        ack = drv.serve(
            _frame(drv, tree, read_only=True),
            lambda ack: None,
            supervisor_factory=lambda: admission.supervisor,
            owns_supervisor=False,
        )
        assert "refused" in ack, ack
        assert ack["refused"]["type"] == "RunLeaseUnavailable", ack
        assert ack["refused"]["retryable"] is True, ack
        admission.assert_no_run()
    assert not (nott_database.directory() / "worktree-collect.lock").exists()


def test_a_route_ping_into_a_tree_under_collection_refuses_before_admission(
    drv: ModuleType, tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mcp, "PING_LEASES", drv.RunLeases())
    try:
        with (
            _real_admission(monkeypatch, tmp_path, store()) as admission,
            _lease(tmp_path).open("a") as collector,
        ):
            fcntl.flock(collector, fcntl.LOCK_EX | fcntl.LOCK_NB)
            answer = McpServer(supervisor=admission.supervisor).nspawn_route_ping(
                str(tree), deadline_ms=5000
            )
            (row,) = answer["routes"]
            assert row["code"] == 2, row
            assert row["run_id"] is None, row
            assert "the worktree collector holds the run lease" in row["cause"], row
            admission.assert_no_run()
    finally:
        mcp.PING_LEASES.close()


def test_a_route_ping_answers_at_its_deadline_and_keeps_the_lease_while_its_stdio_lives(
    drv: ModuleType, tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mcp, "PING_LEASES", drv.RunLeases())
    counts = _counting(drv, monkeypatch)
    release = threading.Event()
    ended = threading.Event()
    tried = threading.Event()
    held: list[bool] = []

    def slow(*args: Any, **kwargs: Any) -> None:
        # A slow bootstrap: the worker outlives the ping deadline.
        held.append(_exclusive_refused(_lease(tmp_path)))
        tried.set()
        release.wait(30)
        ended.set()

    try:
        with _real_admission(monkeypatch, tmp_path, store(), slow) as admission:
            server = McpServer(supervisor=admission.supervisor)
            started = time.monotonic()
            answer = server.nspawn_route_ping(str(tree), deadline_ms=2000)
            elapsed = time.monotonic() - started
            assert not release.is_set()
            assert elapsed < 10, elapsed
            (row,) = answer["routes"]
            assert row["code"] == 2, row
            assert (
                row["cause"] == "still running when the ping's 2000 ms deadline expired"
            )
            assert tried.wait(10)
            assert held == [True]
            assert _exclusive_refused(_lease(tmp_path))
            release.set()
            assert ended.wait(10), "the worker did not end"
            # The stdio keeps the lease after the worker ended, until it exits.
            assert _exclusive_refused(_lease(tmp_path))
            server.nspawn_route_ping(str(tree), deadline_ms=2000)
            assert counts == {str(tree): 1}
    finally:
        release.set()
        mcp.PING_LEASES.close()


def test_a_route_ping_of_a_leased_tree_does_not_wait_for_another_discovery(
    drv: ModuleType, tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mcp, "PING_LEASES", drv.RunLeases())
    other = tmp_path / "tree-b"
    _git(tmp_path / "repo", "worktree", "add", "-q", "-b", "lane/lease-b", str(other))
    counts = _counting(drv, monkeypatch)
    entered = threading.Event()
    release = threading.Event()
    real_run = subprocess.run
    parked = ["git", "-C", str(other), "rev-parse", "--absolute-git-dir"]

    def run(args: Any, *rest: Any, **kwargs: Any) -> Any:
        if list(args) == parked:
            # This discovery parks under the discovery lock.
            entered.set()
            release.wait(30)
        return real_run(args, *rest, **kwargs)

    monkeypatch.setattr(drv.subprocess, "run", run)
    try:
        with _real_admission(
            monkeypatch, tmp_path, store(), lambda *args, **kwargs: None
        ) as admission:
            server = McpServer(supervisor=admission.supervisor)
            store_dir = admission.registry.path.parent
            server.nspawn_route_ping(str(tree), deadline_ms=2000)
            successor = threading.Thread(
                target=mcp.PING_LEASES.take, args=(str(other), store_dir), daemon=True
            )
            successor.start()
            assert entered.wait(10), "the parked discovery did not start"
            answers: list[dict[str, Any]] = []
            ping = threading.Thread(
                target=lambda: answers.append(
                    server.nspawn_route_ping(str(tree), deadline_ms=2000)
                ),
                daemon=True,
            )
            started = time.monotonic()
            ping.start()
            ping.join(5)
            elapsed = time.monotonic() - started
            assert not ping.is_alive(), "the ping waited for another discovery"
            assert successor.is_alive()
            assert not release.is_set()
            assert elapsed < 3, elapsed
            (row,) = answers[0]["routes"]
            assert row["run_id"] is not None, row
            assert counts[str(tree)] == 1
            release.set()
            successor.join(10)
            assert not successor.is_alive()
            assert _exclusive_refused(_lease(tmp_path, "tree-b"))
            assert counts[str(other)] == 1
    finally:
        release.set()
        mcp.PING_LEASES.close()


def _collector_lease(tree: Path) -> Path:
    """The lease file that `tools/worktree.sh` names for `tree` (`lease_take`).

    The collector runs `git -C <tree> rev-parse --absolute-git-dir`. This call
    gives git no repository-local git variable (`git rev-parse --local-env-vars`).
    """
    local = subprocess.run(
        ["git", "rev-parse", "--local-env-vars"],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    ).stdout.split()
    found = subprocess.run(
        ["git", "-C", str(tree), "rev-parse", "--absolute-git-dir"],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
        env={name: value for name, value in os.environ.items() if name not in local},
    )
    return Path(found.stdout.removesuffix("\n"), "nott-run.lease")


def test_the_discovery_clears_each_local_git_variable_of_this_git(
    drv: ModuleType,
) -> None:
    local = subprocess.run(
        ["git", "rev-parse", "--local-env-vars"],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    ).stdout.split()
    assert local
    assert set(local) <= drv.LOCAL_GIT_ENV


def test_a_run_leases_its_own_tree_under_an_inherited_git_dir(
    drv: ModuleType, tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The stdio environment names the git dir of another repository. `-C` does
    # not override GIT_DIR, so the discovery must not use it.
    other = tmp_path / "other"
    _git(tmp_path, "init", "-q", "-b", "main", str(other))
    collector_lease = _collector_lease(tree)
    assert collector_lease == _lease(tmp_path)
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    leases = drv.RunLeases()
    fake = LeasingSupervisor(park=True)
    server, acks, acked = _serve_on_thread(drv, _frame(drv, tree), fake, leases=leases)
    try:
        assert acked.wait(20), "the driver sent no ack"
        assert acks[0].get("run_id") == "run-lease", acks
        # While the run lives, the collector cannot take the lease of its tree.
        assert _exclusive_refused(collector_lease), "the run leased another tree"
        assert not (other / ".git" / "nott-run.lease").exists()
    finally:
        fake.release.set()
        server.join(20)
        leases.close()


#: The time after the ping deadline in which the ping must answer: the kill and
#: the reap of git, and the row of the route.
PING_MARGIN_S = 1.0


def test_a_route_ping_answers_at_its_deadline_while_git_names_the_git_dir(
    drv: ModuleType, tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mcp, "PING_LEASES", drv.RunLeases())
    real_git = shutil.which("git")
    assert real_git is not None
    # A git that sleeps 8 s, longer than the ping budget, only before it names
    # the git dir. `exec` keeps one process, so a kill stops the sleep too.
    sleeper = "import os, sys, time; time.sleep(8); os.execv(sys.argv[1], sys.argv[1:])"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_git = fake_bin / "git"
    fake_git.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = -C ] && [ "$3" = rev-parse ] '
        '&& [ "$4" = --absolute-git-dir ]; then\n'
        f"  exec {shlex.quote(sys.executable)} -c {shlex.quote(sleeper)} "
        f'{shlex.quote(real_git)} "$@"\n'
        "fi\n"
        f'exec {shlex.quote(real_git)} "$@"\n'
    )
    fake_git.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")
    try:
        with _real_admission(
            monkeypatch, tmp_path, store(), lambda *args, **kwargs: None
        ) as admission:
            started = time.monotonic()
            answer = McpServer(supervisor=admission.supervisor).nspawn_route_ping(
                str(tree), deadline_ms=2000
            )
            elapsed = time.monotonic() - started
            runs = [
                tuple(r) for r in sql(admission.registry, "SELECT run_id FROM runs")
            ]
            assert elapsed < 2.0 + PING_MARGIN_S and runs == [], (elapsed, runs)
            (row,) = answer["routes"]
            assert row == {
                "provider": "fixture",
                "code": 2,
                "verdict": "refused",
                "cause": "the ping's 2000 ms deadline expired before this "
                "route's turn to dispatch",
                "run_id": None,
            }, row
            admission.assert_no_run()
    finally:
        mcp.PING_LEASES.close()


def _open_paths() -> set[str]:
    """The path of each descriptor that this process has open now."""
    paths: set[str] = set()
    for fd in os.listdir("/proc/self/fd"):
        with contextlib.suppress(OSError):
            paths.add(os.readlink(f"/proc/self/fd/{fd}"))
    return paths


def test_a_route_ping_whose_lease_ends_after_its_deadline_keeps_no_lease(
    drv: ModuleType, tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mcp, "PING_LEASES", drv.RunLeases())
    real = drv.take_run_lease
    taken: list[int] = []

    def late(cwd: str, store_dir: Path, deadline: float | None = None) -> Any:
        # The git discovery and the flock end before the deadline. Then the
        # thread is late, as a descheduled thread is.
        assert deadline is not None
        lease = real(cwd, store_dir, deadline)
        assert lease is not None and time.monotonic() < deadline
        taken.append(lease)
        time.sleep(deadline - time.monotonic() + 0.05)
        return lease

    monkeypatch.setattr(drv, "take_run_lease", late)
    lease_path = str(_collector_lease(tree))
    try:
        with _real_admission(
            monkeypatch, tmp_path, store(), lambda *args, **kwargs: None
        ) as admission:
            answer = McpServer(supervisor=admission.supervisor).nspawn_route_ping(
                str(tree), deadline_ms=2000
            )
            (row,) = answer["routes"]
            assert row == {
                "provider": "fixture",
                "code": 2,
                "verdict": "refused",
                "cause": "the ping's 2000 ms deadline expired before this "
                "route's turn to dispatch",
                "run_id": None,
            }, row
            admission.assert_no_run()
            assert len(taken) == 1
            # The refused call keeps no lease: the holder has no descriptor
            # and no tree, and this process has the lease file open nowhere.
            assert mcp.PING_LEASES.held == []
            assert mcp.PING_LEASES._trees == {}
            assert lease_path not in _open_paths()
            # The collector (`lease_take`) gets the exclusive lease of the tree.
            assert not _exclusive_refused(Path(lease_path))
    finally:
        mcp.PING_LEASES.close()


#: The receivers of an `execute` that is a SQLite statement, not an admission.
SQLITE_HANDLES = frozenset({"connection", "conn", "cursor"})


def _receiver(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


class _References(ast.NodeVisitor):
    """The innermost function of each `execute` or `review` reference."""

    def __init__(self) -> None:
        self.stack: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
        self.found: dict[str, ast.AST | None] = {}

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.stack.append(node)
        self.generic_visit(node)
        self.stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if (
            node.attr in ("execute", "review")
            and _receiver(node.value) not in SQLITE_HANDLES
        ):
            holder = self.stack[-1] if self.stack else None
            self.found["<module>" if holder is None else holder.name] = holder
        self.generic_visit(node)


def _holders() -> dict[tuple[str, str], ast.AST | None]:
    """Each function outside `supervisor.py` that refers to `execute` or `review`."""
    found: dict[tuple[str, str], ast.AST | None] = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        if path == PACKAGE / "supervisor.py":
            continue
        module = path.relative_to(PACKAGE).with_suffix("").as_posix()
        references = _References()
        references.visit(ast.parse(path.read_text()))
        for name, function in references.found.items():
            found[(module, name)] = function
    return found


def test_each_admission_caller_gives_a_lease_callback() -> None:
    holders = _holders()
    assert sorted(holders) == [("driver", "_dispatch_method"), ("mcp", "ping_routes")]
    ping = holders[("mcp", "ping_routes")]
    assert ping is not None
    (drive,) = _calls(ping, "DriveCall")
    takes = [k for k in drive.keywords if k.arg == "take_lease"]
    assert [ast.unparse(k.value) for k in takes] == [
        "functools.partial(PING_LEASES.take, deadline=deadline)"
    ]
    assert _calls(ping, "DRIVE_CALL.set")
    for node in ast.walk(ping):
        if isinstance(node, ast.Call):
            name = ast.unparse(node.func)
            assert name != "join_workers", name
            assert not name.endswith(".close") and name != "close", name
            assert name not in ("threading.Thread", "Thread"), name
    module = ast.parse((PACKAGE / "driver.py").read_text())
    (drive_main,) = (
        node
        for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name == "drive_main"
    )
    (serve,) = _calls(drive_main, "serve")
    assert "leases" in [keyword.arg for keyword in serve.keywords]
    # The driver closes no lease: the kernel closes each one when it exits.
    # `os.close` here closes the log descriptor after its dup2.
    closes = [
        ast.unparse(node.func)
        for node in ast.walk(drive_main)
        if isinstance(node, ast.Call)
        and ast.unparse(node.func).rsplit(".", 1)[-1] == "close"
    ]
    assert set(closes) <= {"os.close"}, closes


def test_a_dispatch_while_the_collector_has_git_moved_aside_refuses(
    drv: ModuleType, tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The collector holds the discovery lock and moves the `.git` of the tree
    # aside. git then exits non-zero in the tree, as outside a git tree, so the
    # driver cannot find the per-tree lease. The dispatch must refuse.
    lock = tmp_path / "worktree-collect.lock"
    # git does not look for a repository above tmp_path.
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    fake = LeasingSupervisor(store=tmp_path)
    with lock.open("a") as collector:
        fcntl.flock(collector, fcntl.LOCK_EX | fcntl.LOCK_NB)
        (tree / ".git").rename(tmp_path / "gitfile")
        try:
            ack = drv.serve(
                _frame(drv, tree),
                lambda ack: None,
                supervisor_factory=lambda: fake,
                owns_supervisor=False,
            )
        finally:
            (tmp_path / "gitfile").rename(tree / ".git")
    assert "refused" in ack, ack
    assert ack["refused"]["type"] == "RunLeaseUnavailable", ack
    assert ack["refused"]["retryable"] is True, ack
    assert [name for name, _ in fake.calls] == ["execute"]
    # After the collector restores `.git`, the next dispatch finds the lease.
    lease = drv.take_run_lease(str(tree), tmp_path)
    assert lease is not None
    os.close(lease)


def test_a_discovery_lock_that_cannot_be_opened_refuses(
    drv: ModuleType, tree: Path, tmp_path: Path
) -> None:
    # A link in the place of the lock: O_NOFOLLOW refuses to open it.
    lock = tmp_path / "worktree-collect.lock"
    lock.symlink_to(tmp_path / "elsewhere")
    fake = LeasingSupervisor(store=tmp_path)
    ack = drv.serve(
        _frame(drv, tree),
        lambda ack: None,
        supervisor_factory=lambda: fake,
        owns_supervisor=False,
    )
    assert "refused" in ack, ack
    assert ack["refused"]["type"] == "RunLeaseUnavailable", ack
    assert "retryable" not in ack["refused"], ack
    assert [name for name, _ in fake.calls] == ["execute"]
    assert not (tmp_path / "elsewhere").exists()


def test_a_live_run_does_not_hold_the_discovery_lock(
    drv: ModuleType, tree: Path, tmp_path: Path
) -> None:
    # The driver closes the discovery lock once it holds the run lease, so a
    # live run does not keep the collector from other trees.
    lock = tmp_path / "worktree-collect.lock"
    fake = LeasingSupervisor(store=tmp_path, park=True)
    server, _, acked = _serve_on_thread(drv, _frame(drv, tree), fake)
    try:
        assert acked.wait(20), "the driver sent no ack"
        assert [name for name, _ in fake.calls] == ["execute", "admit"]
        with lock.open("a") as collector:
            fcntl.flock(collector, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # The run still holds its lease.
        assert _exclusive_refused(_lease(tmp_path))
    finally:
        fake.release.set()
        server.join(20)
    assert not server.is_alive(), "serve did not return after the worker ended"


def _mock_registry_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> McpServer:
    """The supervisor of `test_result_failure_reason.py`: real admission, a
    `Mock` registry that opens no file, and an environment with HOME and PATH
    only, so `nott_database.directory()` names the live store directory."""
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    secrets.chmod(0o700)
    (secrets / "acme.token").write_text("synthetic-unused")
    (secrets / "acme.token").chmod(0o600)
    source = tmp_path / "roster.toml"
    source.write_text(
        '[defaults]\nmodel = "m1"\n'
        '[bins]\nclaude = "/fixture/launcher"\n'
        '[[provider]]\nname = "a1"\nkind = "claude"\n'
        f'secrets = "{secrets}"\nmodels = ["m1"]\n'
    )
    roster = Roster.read(source)
    monkeypatch.setattr(supervisor_module, "read_roster", lambda cwd: roster)
    monkeypatch.setattr(supervisor_module, "_lane_of", lambda cwd: None)
    monkeypatch.setattr(child.os, "environ", {"HOME": str(tmp_path), "PATH": "/bin"})
    launcher = Mock(side_effect=AssertionError("launcher unexpectedly reached"))
    installation = Mock(side_effect=AssertionError("installation unexpectedly reached"))
    monkeypatch.setattr(child, "spawn_child", launcher)
    monkeypatch.setattr(claude_child, "inspect_claude_installation", installation)
    instance = object.__new__(Supervisor)
    instance._lock = threading.Condition()
    instance._children = {}
    instance._commands = {}
    runs = tmp_path / "runs"
    runs.mkdir()
    instance._run_root = runs
    instance.announces_terminal = False
    instance._writers = {}
    instance._unpublished = {}
    instance._failovers = {}
    instance._agent_launches = {}
    rows: dict[str, Any] = {}
    registry = Mock()
    registry.instance_id = "lease-memory"
    registry.writer_scope.return_value = None
    registry.writer_red.return_value = None
    registry.effort_profile.return_value = EffortProfile()
    registry.read_run_events.return_value = []
    registry.session_reference.return_value = None
    registry.session_transport.return_value = None
    registry.session_exit_code.return_value = None
    registry.admission_show.return_value = None
    registry.terminal_receipt_show.return_value = None
    registry.inference_profile.return_value = None
    registry.get_provider_walls.return_value = {}
    registry.get_account_walls.return_value = {}
    registry.get_run.side_effect = rows.get
    registry.read_claim.return_value = None
    registry.run_custody.return_value = (registry.instance_id, "stdio", True)
    registry.claim_close.return_value = "none"
    registry.implementation_recovery.return_value = None

    def admit(row: Any, claim: bool, now: int, lease: int, **_: Any) -> None:
        rows[row.run_id] = row

    def update(row: Any, **kwargs: Any) -> None:
        rows[row.run_id] = row

    registry.admit_run.side_effect = admit
    registry.update_run.side_effect = update
    registry.close_owned.return_value = True
    instance._registry = registry
    return McpServer(supervisor=instance)


def test_a_registry_without_a_store_directory_takes_no_lease(
    tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded: list[tuple[Any, ...]] = []

    def recorder(*args: Any) -> None:
        recorded.append(args)

    monkeypatch.setattr("nspawn.driver.take_run_lease", recorder, raising=False)
    server = _mock_registry_server(tmp_path, monkeypatch)
    answer = handle(
        server,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "nspawn_run_start",
                "arguments": {
                    "prompt": "reply with the word pong",
                    "cwd": str(tree),
                    "provider": "acme",
                    "class": DECLARED_CLASS,
                    "read_only": True,
                    "timeout_ms": 60000,
                },
            },
        },
    )
    assert "result" in answer, answer
    accepted = json.loads(answer["result"]["content"][0]["text"])
    assert accepted.get("run_id"), accepted
    # The run fails before its child (the launcher is a fence): let it settle.
    handle(
        server,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "nspawn_run_show",
                "arguments": {"run_id": accepted["run_id"], "timeout_ms": 2000},
            },
        },
    )
    assert recorded == []
    assert list(tmp_path.rglob("worktree-collect.lock")) == []
    assert list(tmp_path.rglob("nott-run.lease")) == []


#: Prints the store directory of the registry that `_default_supervisor` opens.
NAME_STORE = "from nspawn import registry\nprint(registry.store_path().parent)\n"


def _native_stdio(tmp_path: Path, run_root: str | None) -> dict[str, str]:
    """The environment of a real stdio with one route of kind `codex`.

    The vendor is the native fixture binary. HOME is this test's own directory,
    and the store directory is the one of this test (`NOTT_DATABASE_DIR`), so no
    live store or lock is touched. With `run_root`, the config file names that
    relative run root. Else `NOTT_NSPAWN_RUN_ROOT` names `<tmp_path>/runs`.
    """
    home = tmp_path / "home"
    home.mkdir(mode=0o700, exist_ok=True)
    env = {
        key: os.environ[key]
        for key in ("PATH", "LANG", "NOTT_DATABASE_DIR")
        if key in os.environ
    }
    env.update(HOME=str(home), PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    binary = tmp_path / "vendor"
    binary.write_text(f"#!{sys.executable}\nKIND='codex'\nRESET_USAGE=False\n" + VENDOR)
    binary.chmod(0o700)
    account = tmp_path / "first"
    account.mkdir(mode=0o700)
    auth = account / "auth.json"
    auth.write_text(
        json.dumps({"tokens": {"access_token": "synthetic-native-access-token"}})
    )
    auth.chmod(0o600)
    config = tmp_path / "spawn.toml"
    config.write_text(
        "\n".join(
            [
                *(["[store]", f"run_root={json.dumps(run_root)}"] if run_root else []),
                "[bins]",
                f"codex={json.dumps(str(binary))}",
                "[[provider]]",
                'name="codex"',
                'kind="codex"',
                'model="test-model"',
                'models=["test-model"]',
                "[provider.accounts.first]",
                f"home={json.dumps(str(account))}",
            ]
        )
        + "\n"
    )
    env.update(
        NOTT_NSPAWN_DRIVER_LAUNCH="direct",
        NOTT_NSPAWN_CONFIG=str(config),
        NOTT_NSPAWN_ROSTER=str(config),
    )
    if run_root is None:
        env["NOTT_NSPAWN_RUN_ROOT"] = str(tmp_path / "runs")
    return env


@contextlib.contextmanager
def _stdio(env: dict[str, str], cwd: Path) -> Iterator[Client]:
    with subprocess.Popen(
        [str(Path(sys.executable).with_name("nspawn")), "mcp", "serve"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        env=env,
    ) as process:
        assert process.stdin is not None
        client = Client(process)
        try:
            assert "result" in client.request(
                "initialize",
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "lease-test", "version": "1"},
                },
            )
            process.stdin.write(
                b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
            )
            process.stdin.flush()
            yield client
        finally:
            client.selector.close()
            process.stdin.close()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def test_drivers_with_different_run_roots_share_the_discovery_lock(
    tmp_path: Path,
) -> None:
    """Two drivers with different run roots and one store directory.

    The first driver has its run root from `NOTT_NSPAWN_RUN_ROOT`. The second is
    a stdio with its own config file, whose relative `[store] run_root` resolves
    against another working directory. Both open their registry in the store
    directory of this test (`NOTT_DATABASE_DIR`), so both name one discovery
    lock. While a holder has that lock exclusive, as the collector has it while
    it moves a `.git` aside, the dispatch of the second driver refuses and is
    retryable. HOME is this test's own directory, so no live lock is touched.
    """
    store()
    env = _native_stdio(tmp_path, "runs")
    first = tmp_path / "first-root"
    first.mkdir()
    named = subprocess.run(
        [sys.executable, "-c", NAME_STORE],
        cwd=first,
        env=env
        | {
            "NOTT_NSPAWN_RUN_ROOT": str(first / "runs"),
            "NOTT_NSPAWN_CONFIG": str(first / "absent.toml"),
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert named.returncode == 0, named.stderr
    directory = Path(named.stdout.removesuffix("\n"))
    assert directory == Path(os.environ["NOTT_DATABASE_DIR"])
    lock = directory / "worktree-collect.lock"
    second = tmp_path / "second-root"
    second.mkdir()
    with lock.open("a") as holder, _stdio(env, second) as client:
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        answer = client.request(
            "tools/call",
            {
                "name": "nspawn_run_start",
                "arguments": {
                    "provider": "first",
                    "cwd": str(tmp_path),
                    "prompt": "Reply PONG",
                    "class": DECLARED_CLASS,
                    "read_only": True,
                    "timeout_ms": 5000,
                },
            },
        )
    assert "error" in answer, answer
    data = answer["error"]["data"]
    assert data["type"] == "RunLeaseUnavailable", answer
    assert data["retryable"] is True, answer
    # The second driver locked no file beside its own run root.
    assert list(second.rglob("worktree-collect.lock")) == []


def _readers(inode: int) -> set[int]:
    """The pids of each granted `FLOCK ADVISORY READ` on `inode` in /proc/locks."""
    pids: set[int] = set()
    for line in Path("/proc/locks").read_text().splitlines():
        fields = line.split()
        # A blocked request has `->` after its number: it holds nothing.
        if len(fields) < 6 or fields[1] == "->":
            continue
        if (
            fields[1:4] == ["FLOCK", "ADVISORY", "READ"]
            and int(fields[5].rsplit(":", 1)[1]) == inode
        ):
            pids.add(int(fields[4]))
    return pids


def _exited(pid: int) -> bool:
    """The state of `pid` is `Z` or `X`, or its stat file does not exist.

    An exited process that no parent reaps keeps `/proc/<pid>` with the state
    `Z`, so the existence of `/proc/<pid>` is not evidence of a live process.
    """
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return True
    return text.rsplit(")", 1)[1].split()[0] in ("Z", "X")


def _phase(client: Client, run_id: str, timeout_ms: int = 0) -> RunPhase:
    arguments: dict[str, Any] = {"run_id": run_id}
    if timeout_ms:
        arguments["timeout_ms"] = timeout_ms
    return RunPhase(client.call("nspawn_run_show", arguments)["phase"])


def test_the_driver_process_holds_the_lease_until_it_exits(
    tree: Path, tmp_path: Path
) -> None:
    store()
    env = _native_stdio(tmp_path, None)
    stdio = tmp_path / "stdio"
    stdio.mkdir()
    with _stdio(env, stdio) as client:
        accepted = client.call(
            "nspawn_run_start",
            {
                "provider": "first",
                "cwd": str(tree),
                "prompt": "wait for deadline",
                "class": DECLARED_CLASS,
                "read_only": True,
                "timeout_ms": 3000,
            },
        )
        run_id = accepted["run_id"]
        (pidfile,) = (tmp_path / "runs" / "drivers").glob("*.pid")
        driver = int(json.loads(pidfile.read_text())["pid"])
        lease = _lease(tmp_path)
        inode = lease.stat().st_ino
        # Live holder: both phases around the observation are not terminal.
        before = _phase(client, run_id)
        readers = _readers(inode)
        refused = _exclusive_refused(lease)
        after = _phase(client, run_id)
        assert not before.is_terminal() and not after.is_terminal(), (before, after)
        assert driver in readers, readers
        assert refused
        bound = time.monotonic() + 60
        while not _phase(client, run_id, 5000).is_terminal():
            assert time.monotonic() < bound, "the run got no terminal phase"
        # Until the exit: the lease stays while the driver lives.
        bound = time.monotonic() + 60
        while not _exited(driver):
            if driver not in _readers(inode):
                limit = time.monotonic() + 1
                while not _exited(driver):
                    assert time.monotonic() < limit, (
                        "the lease closed while the driver lived"
                    )
                    time.sleep(0.01)
                break
            assert time.monotonic() < bound, "the driver did not exit"
            time.sleep(0.01)
        # The last thread of the process can close the descriptors after the
        # leader shows `Z`.
        bound = time.monotonic() + 10
        while _exclusive_refused(lease):
            assert time.monotonic() < bound, "the lease stayed after the driver exited"
            time.sleep(0.01)
