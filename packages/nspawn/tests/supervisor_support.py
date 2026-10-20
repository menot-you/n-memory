"""Shared real-registry fixtures and child doubles for supervisor tests."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from registry_reset import reset_registry, store

from nspawn import claude_child, picker, supervisor, terminal
from nspawn.owner_roster import Provider
from nspawn.process_identity import ProcessIdentity, capture_process_identity
from nspawn.redact import SecretNeedles
from nspawn.registry import Registry
from nspawn.rows import RunPhase
from nspawn.supervisor import (
    RunAccepted,
    Supervisor,
    create_run_directory,
)

# ---------------------------------------------------------------------------
# Registry fixture — a real registry file, mirroring tests/test_registry.py.
# ---------------------------------------------------------------------------


def _url() -> str:
    """This test's registry file, created on first use."""
    return str(store())


@pytest.fixture
def registry() -> Iterator[Registry]:
    reg = Registry(_url(), "i-l7").connect()
    reset_registry(reg, now=1000)
    yield reg
    reg.close()


@pytest.fixture(autouse=True)
def _drain_worker_threads(
    registry: Registry, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Join every `threading.Thread` this test starts before the `registry`
    fixture closes its connection, and before `monkeypatch` reverts its own
    patches.

    A `Supervisor._drive_run` worker straggling past its own test's return
    would otherwise write through a closed connection. Depending on `registry` AND
    `monkeypatch` here (rather than being independently autouse) orders this
    fixture's teardown BEFORE both of theirs, since a dependency tears down
    after its dependents — a still-running worker must observe the SAME
    `terminal.classify_terminal_run` fake the test installed, not the reverted
    original, until it is joined.
    """
    started: list[threading.Thread] = []
    original_start = threading.Thread.start

    def _tracking_start(self: threading.Thread) -> None:
        started.append(self)
        original_start(self)

    threading.Thread.start = _tracking_start  # type: ignore[method-assign]
    try:
        yield
    finally:
        threading.Thread.start = original_start  # type: ignore[method-assign]
        for thread in started:
            thread.join(timeout=10)


@pytest.fixture
def roster_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A minimal real roster: only `defaults.plugins` matters to L7 here.

    L3 (owner ruling 2026-08-29) deletes the silent HOME fallback; the
    roster is reached via `NOTT_NSPAWN_ROSTER`.
    The fixture sets `NOTT_NSPAWN_ROSTER` so the test surface is identical
    to what the live door reads.
    """
    home = tmp_path / "home"
    (home / ".nott").mkdir(parents=True)
    (home / ".nott" / "spawn.toml").write_text(
        '[defaults]\nplugins = ["/tmp/plugin-root"]\n', encoding="utf-8"
    )
    monkeypatch.delenv("NOTT_NSPAWN_ROSTER", raising=False)
    monkeypatch.setenv("NOTT_NSPAWN_ROSTER", str(home / ".nott/spawn.toml"))
    # Under file-wins, isolating only by env lets fixture be overridden
    # by operator's ~/.nott/spawn.toml: fixing CONFIG is mandatory.
    monkeypatch.setenv("NOTT_NSPAWN_CONFIG", str(tmp_path / "absent-spawn.toml"))
    return home


@pytest.fixture
def fake_provider() -> Provider:
    return Provider(
        name="acme",
        bin=Path("/usr/bin/claude"),
        model="opus",
        models=["opus"],
    )


@pytest.fixture
def fake_picked(fake_provider: Provider) -> picker.PickerResult:
    return picker.PickerResult(
        requested_provider=None,
        executed_provider=fake_provider,
        executed_account="acct-1",
        model="opus",
        effort="high",
        warning=None,
    )


# ---------------------------------------------------------------------------
# A minimal, contract-shaped SpawnedChild double wrapping a REAL process, so
# identity capture/matching exercises the real process_identity code paths.
# ---------------------------------------------------------------------------


class FakeSpawnedChild:
    def __init__(self, popen: subprocess.Popen[bytes]) -> None:
        self.process = popen
        self._identity = capture_process_identity(popen.pid)
        self.stop_calls: list[ProcessIdentity] = []

    @property
    def identity(self) -> ProcessIdentity:
        return self._identity

    def poll(self) -> int | None:
        return self.process.poll()

    def wait(self, timeout: float | None = None) -> int:
        return self.process.wait(timeout=timeout)

    def write_stdin(self, payload: bytes, timeout: float | None) -> None:
        self.stdin_written = payload

    def close_stdin(self) -> None:
        pass

    def stop_then_kill(self, expected: ProcessIdentity) -> None:
        self.stop_calls.append(expected)
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(expected.pid, signal.SIGSTOP)
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(expected.pid, signal.SIGKILL)


def _spawn_real(*args: str) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        list(args),
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _fake_installation(binary: Path, probe: object) -> claude_child.ClaudeInstallation:
    return claude_child.ClaudeInstallation(
        binary=binary,
        identity=claude_child.BinaryIdentity(device=1, inode=1, size=1, modified_ns=1),
        version=claude_child.Version(2, 1, 220),
    )


def _fake_mcp_config(plugin_dir: Path, **_: object) -> claude_child.ChildMcpConfig:
    return claude_child.ChildMcpConfig(plugin_dir=plugin_dir, server_names=("nmemory",))


def _wire_l8_and_l3(
    monkeypatch: pytest.MonkeyPatch,
    spawned_factory: object,
) -> None:
    monkeypatch.setattr(
        supervisor.claude_child,
        "resolve_claude_binary",
        lambda **k: Path("/usr/bin/claude"),
    )
    monkeypatch.setattr(
        supervisor.claude_child, "inspect_claude_installation", _fake_installation
    )
    monkeypatch.setattr(
        supervisor.claude_child, "inspect_child_mcp_config", _fake_mcp_config
    )
    monkeypatch.setattr(
        supervisor.child,
        "build_child_env",
        lambda *a, **k: ({}, SecretNeedles.none()),
    )
    monkeypatch.setattr(
        supervisor.child, "compose_child_argv", lambda *a, **k: ["claude", "-p"]
    )
    monkeypatch.setattr(supervisor.child, "spawn_child", spawned_factory)


def _classify_by_exit_code(
    stdout_path: object, stderr_path: object, exit_code: int, **kwargs: object
) -> terminal.TerminalClassification:
    phase = RunPhase.COMPLETED if exit_code == 0 else RunPhase.FAILED
    return terminal.TerminalClassification(
        phase=phase,
        final_text="PONG" if phase is RunPhase.COMPLETED else None,
        warning=None,
        marker=None,
        provider_wall=None,
        account_wall=None,
        tokens=7,
    )


@pytest.fixture
def wired_success(
    monkeypatch: pytest.MonkeyPatch, fake_picked: picker.PickerResult
) -> None:
    """The full L2/L3/L5/L8 happy path: `true` exits 0, classified COMPLETED."""
    monkeypatch.setattr(
        supervisor.picker, "pick_execution", lambda *a, **k: fake_picked
    )
    _wire_l8_and_l3(monkeypatch, lambda *a, **k: FakeSpawnedChild(_spawn_real("true")))
    monkeypatch.setattr(
        supervisor.terminal, "classify_terminal_run", _classify_by_exit_code
    )


@pytest.fixture(autouse=True)
def _stop_every_reaper(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No reaper outlives the test that created it.

    `Supervisor.__init__` starts a daemon that only `shutdown()` stops, and
    most of this file's tests build supervisors without calling it. The
    `registry` fixture closes its connection in teardown, and every daemon left
    behind would keep ticking against it, failing in the teardown of tests
    that created no supervisor at all.

    Registering here, instead of at every construction site, is deliberate: there are
    seven sites and the next one to be written would not know this rule.
    """
    live: list[Supervisor] = []
    born = Supervisor.__init__

    def recording(self: Supervisor, *args: object, **kwargs: object) -> None:
        born(self, *args, **kwargs)  # type: ignore[arg-type]
        live.append(self)

    monkeypatch.setattr(Supervisor, "__init__", recording)
    yield
    for supervisor_ in live:
        supervisor_._stop_tick.set()
    for supervisor_ in live:
        supervisor_._tick_thread.join(timeout=2)
    # `shutdown()` is the only production path that closes the run-root
    # descriptor; a supervisor that never reached it still owns one. Release
    # exactly that one, the way `shutdown()` does — a supervisor that did shut
    # down holds None.
    for supervisor_ in live:
        fd = supervisor_._run_root_fd
        supervisor_._run_root_fd = None
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)


def _sv(registry: Registry, tmp_path: Path) -> Supervisor:
    return Supervisor(registry, tmp_path / "runs")


def _await_running(sv: Supervisor, run_id: str, timeout_ms: int = 2000) -> None:
    """Return as soon as `run_id` is RUNNING with its child published.

    `sv.status(run_id, timeout_ms=...)` long-polls for a TERMINAL phase, so the
    tests that used it to "wait until RUNNING" against a child that sleeps
    always spent the whole timeout. Every phase write notifies `sv._lock`, and
    `_launching` publishes the child before writing RUNNING, so this wakes on
    the transition itself."""
    deadline = time.monotonic() + timeout_ms / 1000
    with sv._lock:
        while True:
            row = sv._registry.get_run(run_id)
            if row is not None and row.phase is RunPhase.RUNNING:
                assert run_id in sv._children
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                pytest.fail(f"run {run_id} was not RUNNING within {timeout_ms} ms")
            sv._lock.wait(remaining)


def _admit_claimed(sv: Supervisor, prompt: str, cwd: str) -> RunAccepted:
    """A dispatch that holds its worktree claim, driven through the real worker.

    A writable `execute` requires `native_write` on a writer route (codex, or
    the caged Claude writer in `claude_restricted.py`) in a declared checkout.
    The claim these tests measure is the admission's, so they take it there —
    the form `test_supervisor.py` adopted in b8699f7bc."""
    return sv._admit(
        prompt,
        cwd,
        provider=None,
        model=None,
        effort=None,
        execution_class=None,
        session_id=None,
        timeout_ms=0,
        claim_worktree=True,
        read_only=True,
    )


# ---------------------------------------------------------------------------
# _drive_run / _spawn_and_wait, called directly and synchronously
# ---------------------------------------------------------------------------


def _admit_pending(
    sv: Supervisor,
    registry: Registry,
    tmp_path: Path,
    *,
    worktree: str,
    claim: bool = True,
) -> tuple[str, str]:
    """Create just the durable row + directory an execute() admission would,
    without starting a worker — for tests driving `_drive_run` synchronously.

    `claim=False` is the `review` admission: read-only, so it takes no claim."""
    from nspawn.rows import CLAUDE_CONFINEMENT_POLICY_ONLY, Backend, RunRow

    run_id = claude_child.mint_session_id()
    thread_id = claude_child.mint_session_id()
    create_run_directory(sv._run_root_path, run_id, dir_fd=sv._run_root)
    row = RunRow(
        run_id=run_id,
        thread_id=thread_id,
        worktree=worktree,
        phase=RunPhase.PENDING,
        sandbox=CLAUDE_CONFINEMENT_POLICY_ONLY,
        model="opus",
        owner_instance=registry.instance_id,
        created_at=1,
        updated_at=1,
        backend=Backend.CLAUDE,
    )
    registry.admit_run(row, claim, 1, sv.RUN_CLAIM_LEASE_MS)
    return run_id, thread_id
