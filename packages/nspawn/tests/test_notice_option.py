"""A start with `notice: false` owes no session the end of its run.

A side unit's background agent runs in the process of the main session and
shares its nSPAWN door (`main.py`), so each run it started notified the main
session (`notify.py`). `notice: false` drops the caller from the dispatch frame:
admission then writes no `notices` row, so nothing pushes and nothing drains.
The agent waits with `nspawn run wait <run-id>` instead. It reads the run
through the same tool as `nspawn run show` and exits 0 only for a completed run.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest
from mcp_fakes import FakeSupervisor, handle
from registry_reset import sql
from supervisor_support import _drain_worker_threads, _stop_every_reaper, registry

from nspawn import cli, driver, notify
from nspawn.mcp import McpServer
from nspawn.registry import NoticeCaller, Registry
from nspawn.rows import Backend, RunPhase, RunRow
from nspawn.supervisor import DRIVE_CALL, DriveCall, RunAccepted

__all__ = ["_drain_worker_threads", "_stop_every_reaper", "registry"]

SOCKET_ENV = "CLAUDE_CODE_MESSAGING_SOCKET"
NODE_ENV = "NOTT_NSPAWN_NODE_SESSION"
REMOTE_ENV = "NOTT_NSPAWN_REMOTE_SESSION"

#: One start of each verb that takes the option, as a caller sends it.
STARTS: dict[str, dict[str, Any]] = {
    "nspawn_run_start": {"prompt": "p", "class": "design"},
    "nspawn_review_start": {"branch": "lane/x", "base": "main", "prompt": "q"},
}


def _wait_for(predicate: Callable[[], Any], timeout: float, what: str) -> Any:
    deadline = time.monotonic() + timeout
    while not (value := predicate()):
        if time.monotonic() >= deadline:
            pytest.fail(f"timed out after {timeout} s waiting for {what}")
        time.sleep(0.05)
    return value


# ---------------------------------------------------------------------------
# The door: `notice: false` leaves the admission no session to tell.
# ---------------------------------------------------------------------------


@dataclass
class _Owed(FakeSupervisor):
    """Records, for each admission, the caller `admit_run` would write a row for."""

    owed: list[NoticeCaller | None] = field(default_factory=list)

    def _owe(self) -> None:
        call = DRIVE_CALL.get()
        self.owed.append(None if call is None else call.notice())

    def execute(self, *args: Any, **kwargs: Any) -> RunAccepted:
        self._owe()
        return super().execute(*args, **kwargs)

    def review(self, *args: Any, **kwargs: Any) -> RunAccepted:
        self._owe()
        return super().review(*args, **kwargs)


def _door(monkeypatch: pytest.MonkeyPatch, *, node: bool = False) -> _Owed:
    monkeypatch.delenv(REMOTE_ENV, raising=False)
    if node:
        monkeypatch.delenv(SOCKET_ENV, raising=False)
        monkeypatch.setenv(NODE_ENV, str(uuid.uuid4()))
    else:
        monkeypatch.delenv(NODE_ENV, raising=False)
    return _Owed(
        execute_result=RunAccepted(run_id="run-x"),
        review_result=RunAccepted(run_id="run-r"),
    )


def _call(server: McpServer, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return handle(
        server,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        },
    )


def _served(answer: dict[str, Any]) -> dict[str, Any]:
    assert "error" not in answer, answer
    payload: dict[str, Any] = json.loads(answer["result"]["content"][0]["text"])
    return payload


@pytest.mark.parametrize("tool", sorted(STARTS))
def test_the_door_admits_a_start_with_notice_false_for_no_session(
    tool: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = os.getppid()
    addr = f"/run/user/0/cc-socks/{parent}.sock"
    fake = _door(monkeypatch)
    monkeypatch.setenv(SOCKET_ENV, addr)
    server = McpServer(supervisor=fake)
    start = {**STARTS[tool], "cwd": str(tmp_path)}
    _served(_call(server, tool, start))
    _served(_call(server, tool, {**start, "notice": False}))
    told, quiet = fake.owed
    assert told is not None and (told.addr, told.pid) == (addr, parent), (
        "the start without the option owes no session: the caller did not resolve"
    )
    assert quiet is None, "a start with notice false still owes its session the end"
    assert all("notice" not in kwargs for _, kwargs in fake.calls), (
        "the option reached the supervisor as an argument"
    )


@pytest.mark.parametrize("tool", sorted(STARTS))
def test_a_node_door_refuses_notice_false_before_admission(
    tool: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _door(monkeypatch, node=True)
    server = McpServer(supervisor=fake)
    start = {**STARTS[tool], "cwd": str(tmp_path)}
    if tool == "nspawn_run_start":
        start["timeout_ms"] = 60_000
    refused = _call(server, tool, {**start, "notice": False})
    assert "error" in refused, refused
    message = str(refused["error"].get("message"))
    assert "notice" in message and "node door" in message, message
    assert fake.calls == [], "a node door admitted a start with notice false"
    _served(_call(server, tool, start))
    assert len(fake.calls) == 1


# ---------------------------------------------------------------------------
# The outbox: the run ends, and its end reaches no session.
# ---------------------------------------------------------------------------


@dataclass
class _Session:
    """A live socket named `<parent pid>.sock`: `driver.caller_identity` resolves it
    as the socket of the Claude Code session that owns this process. It keeps each
    frame that a push writes into it."""

    addr: str
    frames: list[bytes] = field(default_factory=list)

    def text(self) -> str:
        return b"".join(self.frames).decode("utf-8", errors="replace")


@contextlib.contextmanager
def _session() -> Iterator[_Session]:
    directory = Path(tempfile.mkdtemp(prefix="nso"))
    session = _Session(str(directory / f"{os.getppid()}.sock"))
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stop = threading.Event()

    def accept() -> None:
        while not stop.is_set():
            try:
                peer, _ = server.accept()
            except TimeoutError:
                continue
            with peer:
                peer.settimeout(2)
                chunks: list[bytes] = []
                with contextlib.suppress(OSError):
                    while chunk := peer.recv(65536):
                        chunks.append(chunk)
                session.frames.append(b"".join(chunks))

    listener = threading.Thread(target=accept, daemon=True)
    try:
        server.bind(session.addr)
        server.listen(4)
        server.settimeout(0.1)
        listener.start()
        yield session
    finally:
        stop.set()
        if listener.is_alive():
            listener.join(timeout=5)
        server.close()
        shutil.rmtree(directory, ignore_errors=True)


def _admit(store: Registry, run_id: str, notice: NoticeCaller | None) -> None:
    now = int(time.time() * 1000)
    store.heartbeat_instance(now)
    store.admit_run(
        RunRow(
            run_id=run_id,
            worktree=f"/fixture/{run_id}",
            phase=RunPhase.PENDING,
            sandbox="workspace-write",
            model="m",
            owner_instance=store.instance_id,
            created_at=now,
            updated_at=now,
            backend=Backend.CLAUDE,
        ),
        False,
        now,
        60_000,
        notice=notice,
    )


def _move(store: Registry, run_id: str, *phases: RunPhase) -> None:
    store.heartbeat_instance(int(time.time() * 1000))
    for phase in phases:
        row = store.get_run(run_id)
        assert row is not None
        store.update_run(replace(row, phase=phase, updated_at=row.updated_at + 1))


def _notice_row(store: Registry, run_id: str) -> dict[str, Any] | None:
    row = sql(store, "SELECT * FROM notices WHERE run_id = ?", (run_id,)).fetchone()
    return None if row is None else dict(row)


def test_a_run_started_with_notice_false_ends_with_no_row_and_no_push(
    registry: Registry, tmp_path: Path
) -> None:
    told_run, quiet_run = str(uuid.uuid4()), str(uuid.uuid4())
    with _session() as session:
        environ = {SOCKET_ENV: session.addr}
        for run_id, option in ((told_run, {}), (quiet_run, {"notice": False})):
            frame = driver.make_frame(
                "nspawn_run_start",
                {"prompt": "p", "cwd": str(tmp_path), **option},
                drivers_dir=None,
                environ=environ,
            )
            call = DriveCall(
                launch_uuid=str(frame["launch_uuid"]),
                caller=frame["caller"],
                project=frame["project"],
            )
            _admit(registry, run_id, call.notice())
            _move(registry, run_id, RunPhase.RUNNING, RunPhase.COMPLETED)
        assert _notice_row(registry, quiet_run) is None, (
            "a run started with notice false has a notices row"
        )
        told = _notice_row(registry, told_run)
        assert told is not None, "the run started without the option lost its row"
        assert told["caller_addr"] == session.addr
        assert notify.push_ready(registry, [told_run, quiet_run]) == 1
        _wait_for(lambda: session.frames, 5, "the push of the control run")
        assert told_run in session.text()
        assert quiet_run not in session.text(), "the session heard the quiet run"


# ---------------------------------------------------------------------------
# The wait: `nspawn run wait <run-id>` against a real registry.
# ---------------------------------------------------------------------------


@pytest.fixture
def run_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The run root the command's MCP peer reads: this test's, never the live one."""
    root = tmp_path / "runs"
    root.mkdir(mode=0o700)
    monkeypatch.setenv("NOTT_NSPAWN_RUN_ROOT", str(root))
    return root


def _insert(
    store: Registry,
    root: Path,
    phase: RunPhase,
    *,
    warning: str | None = None,
) -> str:
    run_id = str(uuid.uuid4())
    (root / run_id).mkdir(mode=0o700)
    now = int(time.time() * 1000)
    store.heartbeat_instance(now)
    store.insert_run(
        RunRow(
            run_id=run_id,
            worktree=str(root),
            phase=phase,
            sandbox="read-only",
            model="m",
            owner_instance=store.instance_id,
            created_at=now,
            updated_at=now,
            backend=Backend.CLAUDE,
            warning=warning,
        )
    )
    return run_id


def _waited(capsys: pytest.CaptureFixture[str], run_id: str) -> tuple[int, str, str]:
    capsys.readouterr()
    code = cli.main(["run", "wait", run_id])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_run_wait_exits_0_for_a_completed_run(
    registry: Registry, run_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_id = _insert(registry, run_root, RunPhase.COMPLETED)
    code, out, err = _waited(capsys, run_id)
    assert code == 0, err
    assert json.loads(out) == {"run_id": run_id, "phase": "completed"}


def test_run_wait_exits_3_for_a_failed_run(
    registry: Registry, run_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_id = _insert(registry, run_root, RunPhase.FAILED)
    code, out, err = _waited(capsys, run_id)
    assert code == 3, err
    assert json.loads(out) == {"run_id": run_id, "phase": "failed"}


def test_run_wait_follows_the_run_that_continued_the_work(
    registry: Registry, run_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    successor = _insert(registry, run_root, RunPhase.COMPLETED)
    moved = _insert(
        registry,
        run_root,
        RunPhase.FAILED,
        warning=f"route refused the turn; continued as run {successor}",
    )
    code, out, err = _waited(capsys, moved)
    assert code == 0, err
    assert json.loads(out) == {"run_id": successor, "phase": "completed"}


def test_run_wait_blocks_until_the_run_ends(registry: Registry, run_root: Path) -> None:
    run_id = _insert(registry, run_root, RunPhase.RUNNING)
    # The `with` closes both pipes on every exit; the `finally` first stops a
    # waiter that still runs, so that the close does not wait for it forever.
    with subprocess.Popen(
        [sys.executable, "-m", "nspawn.cli", "run", "wait", run_id],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as waiter:
        try:
            time.sleep(3)
            assert waiter.poll() is None, (
                f"run wait exited {waiter.returncode} while the run was still running"
            )
            _move(registry, run_id, RunPhase.COMPLETED)
            out, err = waiter.communicate(timeout=60)
        finally:
            if waiter.poll() is None:
                waiter.kill()
                waiter.wait(timeout=5)
    assert waiter.returncode == 0, err
    assert json.loads(out) == {"run_id": run_id, "phase": "completed"}


def test_run_wait_refuses_a_run_the_store_does_not_hold(
    registry: Registry, run_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = str(uuid.uuid4())
    code, out, err = _waited(capsys, missing)
    assert code == 2
    assert out == ""
    assert "refused" in err and missing in err


@pytest.mark.parametrize(
    "arguments", [["run", "wait"], ["run", "wait", "a", "b"], ["run", "wait", "--help"]]
)
def test_run_wait_refuses_its_usage_before_starting_a_peer(
    arguments: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("a usage refusal must come before the MCP peer starts")

    monkeypatch.setattr(cli, "NspawnClient", forbidden, raising=False)
    assert cli.main(arguments) == 2


def test_run_wait_reaps_a_run_whose_driver_owner_is_gone(
    registry: Registry, run_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_id = _insert(registry, run_root, RunPhase.RUNNING)
    registry.remove_instance()
    code, out, err = _waited(capsys, run_id)
    assert code == 3, err
    assert json.loads(out) == {"run_id": run_id, "phase": "orphaned"}
    row = registry.get_run(run_id)
    assert row is not None and row.phase is RunPhase.ORPHANED


def test_run_wait_reads_a_status_that_the_door_spills(
    registry: Registry, run_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Escaped as JSON, 16 000 control characters are 96 000 bytes: the door
    # answers `nspawn_run_show` with {preview, path, bytes}, not the status.
    run_id = _insert(registry, run_root, RunPhase.RUNNING)
    row = registry.get_run(run_id)
    assert row is not None
    large = "\x01" * 16_000
    registry.update_run(
        replace(
            row, final_text=large, blocked_reason=large, updated_at=row.updated_at + 1
        )
    )
    _move(registry, run_id, RunPhase.COMPLETED)
    code, out, err = _waited(capsys, run_id)
    assert code == 0, err
    assert json.loads(out) == {"run_id": run_id, "phase": "completed"}
    # Each answer that the door spilled for the wait is the wait's to delete.
    assert sorted((run_root / run_id).glob("mcp-result-*.json")) == []
