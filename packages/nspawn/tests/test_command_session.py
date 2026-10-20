"""Registered PATH commands use real custody, output, and private control sockets."""

# ruff: noqa: F811
from __future__ import annotations

import base64
import errno
import json
import os
import socket
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from mcp_fakes import handle
from nott_socket.door import AcceptError
from supervisor_support import registry  # noqa: F401

from nspawn import command as command_module
from nspawn import command_terminal
from nspawn.mcp import McpServer
from nspawn.registry import Registry
from nspawn.supervisor import Supervisor


def call(server: McpServer, arguments: dict[str, Any]) -> dict[str, Any]:
    answer = handle(
        server,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "nspawn_run_start", "arguments": arguments},
        },
    )
    assert answer is not None
    assert "error" not in answer, "nSPAWN must admit a registered PATH command."
    result: dict[str, Any] = json.loads(answer["result"]["content"][0]["text"])
    return result


def control(
    port: dict[str, str], method: str, params: dict[str, Any]
) -> dict[str, Any]:
    with socket.socket(socket.AF_UNIX) as peer:
        peer.settimeout(5)
        peer.connect(port["control"])
        peer.sendall(json.dumps({"method": method, "params": params}).encode() + b"\n")
        with peer.makefile("rb") as incoming:
            reply: dict[str, Any] = json.loads(incoming.readline())
    assert "error" not in reply, "The command control operation must succeed."
    return reply["result"]


def recorded_output(supervisor: Supervisor, run_id: str) -> bytes:
    return b"".join(
        base64.b64decode(frame["data"])
        for line in supervisor.transcript(run_id).lines
        if (frame := json.loads(line)).get("type") == "terminal_output"
    )


@pytest.mark.parametrize("name", ["owner-command-one", "owner-command-two"])
def test_a_path_command_registers_identity_custody_and_durable_output(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    tools = tmp_path / "bin"
    tools.mkdir()
    executable = tools / name
    executable.write_text("#!/bin/sh\nprintf 'registered-output\\n'\nread answer\n")
    executable.chmod(0o700)
    monkeypatch.setenv("PATH", f"{tools}:{os.defpath}")
    supervisor = Supervisor(registry, tmp_path / "runs")
    server = McpServer(supervisor=supervisor)
    run_id: str | None = None
    try:
        accepted = call(
            server,
            {
                "cwd": str(tmp_path),
                "command": {
                    "executable": name,
                    "arguments": [],
                    "cols": 80,
                    "rows": 24,
                    "centerId": "command-center-test",
                },
            },
        )
        run_id = accepted["run_id"]
        row = registry.get_run(run_id)
        assert row is not None
        assert row.backend.value == "command"
        assert row.worktree == str(tmp_path.resolve())
        assert row.process_identity is not None
        assert registry.writer_scope(run_id) is not None
        events = registry.read_run_events(run_id)
        command = next(
            event.payload["command"] for event in events if "command" in event.payload
        )
        assert command["executable"] == str(executable.resolve())
        assert command["identity"]["inode"] == executable.stat().st_ino
        assert command["centerId"] == "command-center-test"
        shown = supervisor.status(run_id)
        assert shown.transport is not None
        deadline = time.monotonic() + 5
        while True:
            output = control(shown.transport, "terminal/attach", {})
            if b"registered-output" in base64.b64decode(output["data"]):
                break
            assert time.monotonic() < deadline, "The PATH command must produce output."
            time.sleep(0.02)
        replay = control(shown.transport, "terminal/attach", {"since": 0})
        assert replay["seq"] == output["seq"]
        assert replay["info"]["runId"] == run_id
        assert replay["info"]["centerId"] == "command-center-test"
        assert b"registered-output" in recorded_output(supervisor, run_id)
        readback = supervisor.close(run_id)
        assert readback.phase.value == "closed"
        assert not readback.tree.survivors
        assert not Path(f"/proc/{row.process_identity.pid}").exists()
        assert b"registered-output" in recorded_output(supervisor, run_id)
    finally:
        if run_id is not None and registry.get_run(run_id) is not None:
            supervisor.close(run_id)
        server.close()


def test_execution_refuses_a_changed_path_before_the_command_executes(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "owner-path-command"
    executable.write_text("#!/bin/sh\nprintf 'admitted-inode\\n'\nread answer\n")
    executable.chmod(0o700)
    other = tmp_path / "replacement"
    other.write_text("#!/bin/sh\nprintf 'wrong-inode\\n'\nread answer\n")
    other.chmod(0o700)
    admitted = executable.stat().st_ino
    terminal = command_module.Terminal

    def changed(*args: Any, **kwargs: Any) -> Any:
        other.replace(executable)
        return terminal(*args, **kwargs)

    monkeypatch.setattr(command_module, "Terminal", changed)
    supervisor = Supervisor(registry, tmp_path / "runs")
    server = McpServer(supervisor=supervisor)
    run_id = None
    try:
        accepted = call(
            server,
            {
                "cwd": str(tmp_path),
                "command": {
                    "executable": str(executable),
                    "cols": 80,
                    "rows": 24,
                    "centerId": "identity-center",
                },
            },
        )
        run_id = accepted["run_id"]
        deadline = time.monotonic() + 5
        while not recorded_output(supervisor, run_id):
            assert time.monotonic() < deadline
            time.sleep(0.02)
        assert b"The executable changed after command admission." in recorded_output(
            supervisor, run_id
        )
        assert b"wrong-inode" not in recorded_output(supervisor, run_id)
        assert executable.stat().st_ino != admitted
    finally:
        if run_id is not None:
            supervisor.close(run_id)
        server.close()


def test_a_path_script_keeps_its_zero_argument_and_can_read_adjacent_files(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools = tmp_path / "bin"
    tools.mkdir()
    executable = tools / "owner-script"
    executable.write_text(
        '#!/bin/sh\ncat "$(dirname "$0")/neighbor.txt"\nread answer\n'
    )
    executable.chmod(0o700)
    (tools / "neighbor.txt").write_text("normal-script-behavior\n")
    monkeypatch.setenv("PATH", f"{tools}:{os.defpath}")
    supervisor = Supervisor(registry, tmp_path / "runs")
    server = McpServer(supervisor=supervisor)
    run_id = None
    try:
        accepted = call(
            server,
            {
                "cwd": str(tmp_path),
                "command": {
                    "executable": "owner-script",
                    "cols": 80,
                    "rows": 24,
                    "centerId": "script-center",
                },
            },
        )
        run_id = accepted["run_id"]
        deadline = time.monotonic() + 5
        while not recorded_output(supervisor, run_id):
            assert time.monotonic() < deadline
            time.sleep(0.02)
        assert b"normal-script-behavior" in recorded_output(supervisor, run_id)
    finally:
        if run_id is not None:
            supervisor.close(run_id)
        server.close()


@pytest.mark.parametrize("name", ["qwen", "strife"])
@pytest.mark.parametrize("mode", ["direct", "env", "env-split"])
def test_forbidden_provider_executables_never_enter_generic_command_custody(
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    mode: str,
) -> None:
    executable = tmp_path / name
    executable.write_text("#!/bin/sh\nprintf 'forbidden-execution'\n")
    executable.chmod(0o700)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.defpath}")
    supervisor = Supervisor(registry, tmp_path / "runs")
    server = McpServer(supervisor=supervisor)
    command: dict[str, object] = {
        "executable": name if mode == "direct" else "/usr/bin/env",
        "cols": 80,
        "rows": 24,
        "centerId": "refusal-center",
    }
    if mode == "env":
        command["arguments"] = [name]
    elif mode == "env-split":
        command["arguments"] = ["-S", f"timeout 1 {name}"]
    try:
        answer = handle(
            server,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "nspawn_run_start",
                    "arguments": {
                        "cwd": str(tmp_path),
                        "command": command,
                    },
                },
            },
        )
        assert "error" in answer
        assert "provider scope" in answer["error"]["message"]
        assert registry.list_runs() == ()
        accepted = call(
            server,
            {
                "cwd": str(tmp_path),
                "command": {
                    "executable": "printf" if mode == "direct" else "/usr/bin/env",
                    "arguments": (
                        ["%s", name]
                        if mode == "direct"
                        else (
                            ["printf", "%s", name]
                            if mode == "env"
                            else ["-S", f"printf %s {name}"]
                        )
                    ),
                    "cols": 80,
                    "rows": 24,
                    "centerId": "refusal-center",
                },
            },
        )
        supervisor.close(accepted["run_id"])
    finally:
        server.close()


@pytest.mark.parametrize("alias", ["declared", "symlink", "hardlink"])
def test_a_native_executable_alias_returns_its_account_route_without_a_command_run(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, alias: str
) -> None:
    executable = tmp_path / "private-native-client"
    executable.write_text("#!/bin/sh\nprintf 'wrong-native-route'\n")
    executable.chmod(0o700)
    selected = executable
    if alias != "declared":
        selected = tmp_path / "owner-client-alias"
        if alias == "symlink":
            selected.symlink_to(executable)
        else:
            selected.hardlink_to(executable)
    roster = tmp_path / "roster.toml"
    roster.write_text(
        f'[bins]\ncodex="{executable}"\n'
        '[[provider]]\nname="codex"\nkind="codex"\nmodel="private-model"\n'
        "accounts = {}\n"
    )
    monkeypatch.setenv("NOTT_NSPAWN_ROSTER", str(roster))
    monkeypatch.setenv("NOTT_NSPAWN_CONFIG", str(tmp_path / "absent.toml"))
    supervisor = Supervisor(registry, tmp_path / "runs")
    server = McpServer(supervisor=supervisor)
    try:
        answer = handle(
            server,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "nspawn_run_start",
                    "arguments": {
                        "cwd": str(tmp_path),
                        "command": {
                            "executable": str(selected),
                            "cols": 80,
                            "rows": 24,
                            "centerId": "native-center",
                        },
                    },
                },
            },
        )
        assert "error" in answer
        assert answer["error"]["data"].get("kind") == "native_route", answer
        assert answer["error"]["data"]["provider"] == "codex"
        assert registry.list_runs() == ()
    finally:
        for run_id in tuple(supervisor._commands):
            supervisor.close(run_id)
        server.close()


def test_relative_path_entries_use_the_recorded_project_and_push_environment(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools = tmp_path / "bin"
    tools.mkdir()
    executable = tools / "owner-env-command"
    executable.write_text(
        "#!/bin/sh\n"
        'test "$NOTT_MEMORY_POLICY" = push && '
        'test -z "${NOTT_PRIVATE_TEST_SENTINEL+x}" && '
        "printf 'closed-environment-ok\\n'\nread answer\n"
    )
    executable.chmod(0o700)
    monkeypatch.setenv("PATH", f"bin:{os.defpath}")
    monkeypatch.setenv("NOTT_PRIVATE_TEST_SENTINEL", "private-test-value")
    supervisor = Supervisor(registry, tmp_path / "runs")
    server = McpServer(supervisor=supervisor)
    run_id = None
    try:
        accepted = call(
            server,
            {
                "cwd": str(tmp_path),
                "command": {
                    "executable": executable.name,
                    "cols": 80,
                    "rows": 24,
                    "centerId": "environment-center",
                },
            },
        )
        run_id = accepted["run_id"]
        deadline = time.monotonic() + 5
        while b"closed-environment-ok" not in recorded_output(supervisor, run_id):
            assert time.monotonic() < deadline
            time.sleep(0.02)
    finally:
        if run_id is not None:
            supervisor.close(run_id)
        server.close()


def test_the_custodian_refuses_a_platform_without_the_linux_pty_boundary(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(command_terminal, "LINUX", False)
    supervisor = Supervisor(registry, tmp_path / "runs")
    server = McpServer(supervisor=supervisor)
    try:
        answer = handle(
            server,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "nspawn_run_start",
                    "arguments": {
                        "cwd": str(tmp_path),
                        "command": {
                            "executable": "/bin/sh",
                            "cols": 80,
                            "rows": 24,
                            "centerId": "platform-center",
                        },
                    },
                },
            },
        )
        assert answer is not None
        assert "error" in answer
        assert "Linux" in answer["error"]["message"]
        assert all(row.process_identity is None for row in registry.list_runs())
    finally:
        server.close()


def started(
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    script: str,
) -> tuple[Supervisor, McpServer, str, dict[str, str]]:
    """A registered PATH command running `script`, and its control port."""
    tools = tmp_path / "bin"
    tools.mkdir()
    executable = tools / "owner-long-command"
    executable.write_text(script)
    executable.chmod(0o700)
    monkeypatch.setenv("PATH", f"{tools}:{os.defpath}")
    supervisor = Supervisor(registry, tmp_path / "runs")
    server = McpServer(supervisor=supervisor)
    accepted = call(
        server,
        {
            "cwd": str(tmp_path),
            "command": {
                "executable": executable.name,
                "cols": 80,
                "rows": 24,
                "centerId": "long-center",
            },
        },
    )
    shown = supervisor.status(accepted["run_id"])
    assert shown.transport is not None
    return supervisor, server, accepted["run_id"], shown.transport


def screen_shows(port: dict[str, str], text: str, timeout: float = 20) -> None:
    deadline = time.monotonic() + timeout
    while not any(text in line for line in control(port, "terminal/read", {})["lines"]):
        assert time.monotonic() < deadline, f"The screen must show {text!r}."
        time.sleep(0.05)


def test_output_past_the_retention_bound_keeps_the_command_alive(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once its retained output passed the run budget, the custodian stopped the
    command's scope: `yes`, a dev server or a verbose build was killed without a
    warning. Retention drops its oldest records and says so; the command lives."""
    monkeypatch.setattr(command_module, "RETAINED_BYTES", 64 * 1024)
    supervisor, server, run_id, port = started(
        registry,
        tmp_path,
        monkeypatch,
        "#!/bin/sh\nhead -c 400000 /dev/zero | tr '\\0' x\n"
        "printf '\\nafter-budget\\n'\nread answer\nprintf 'answered-%s\\n' \"$answer\"\n"
        "read again\n",
    )
    try:
        screen_shows(port, "after-budget")
        control(port, "terminal/input", {"data": base64.b64encode(b"yes\n").decode()})
        screen_shows(port, "answered-yes")
        info = control(port, "terminal/info", {})
        assert info["exited"] is None
        assert info["retention"]["droppedBytes"] > 0
        retained = [json.loads(line) for line in supervisor.transcript(run_id).lines]
        assert retained[0]["data"] == ""
        assert retained[0]["droppedBytes"] == info["retention"]["droppedBytes"]
        assert b"answered-yes" in recorded_output(supervisor, run_id)
        assert sum(len(line) + 1 for line in supervisor.transcript(run_id).lines) <= (
            64 * 1024
        )
        page = control(port, "terminal/output", {"offset": 0})
        assert page["events"][0] == retained[0]
        assert page["offset"] > info["retention"]["droppedBytes"]
        # The transcript's line cursor goes on across the emptied file: a cursor
        # from before it reads the gap record first, and none goes backwards.
        dropped = retained[0]["droppedRecords"]
        assert dropped > 0
        behind = supervisor.transcript(run_id, since_seq=dropped - 1)
        assert json.loads(behind.lines[0]) == retained[0]
        assert behind.next_seq > dropped
        later = supervisor.transcript(run_id, since_seq=behind.next_seq)
        assert later.next_seq >= behind.next_seq
        assert supervisor.status(run_id).total_seq > dropped
    finally:
        supervisor.close(run_id)
        server.close()


WAITING = "#!/bin/sh\nprintf 'door-ready\\n'\nread answer\n"


def test_a_frame_nested_past_the_parser_leaves_the_control_door_serving(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`json.loads` raises `RecursionError`, which no catch named: the control
    thread died and the command lost input, attach, output and resize for good."""
    supervisor, server, run_id, port = started(registry, tmp_path, monkeypatch, WAITING)
    try:
        with socket.socket(socket.AF_UNIX) as peer:
            peer.settimeout(5)
            peer.connect(port["control"])
            peer.sendall(b"[" * 200000 + b"\n")
            with peer.makefile("rb") as incoming:
                assert "error" in json.loads(incoming.readline())
        assert control(port, "terminal/info", {})["runId"] == run_id
    finally:
        supervisor.close(run_id)
        server.close()


def test_a_slow_peer_cannot_hold_the_control_door(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The door serves one connection at a time and waited 1 s per read: a peer
    that sent one byte a little faster than that held it for good."""
    monkeypatch.setattr(command_module, "REQUEST_S", 0.5)
    supervisor, server, run_id, port = started(registry, tmp_path, monkeypatch, WAITING)
    try:
        with socket.socket(socket.AF_UNIX) as slow:
            slow.connect(port["control"])
            stop = time.monotonic() + 4

            def drip() -> None:
                while time.monotonic() < stop:
                    try:
                        slow.send(b" ")
                    except OSError:
                        return  # the door ended this connection
                    time.sleep(0.2)

            dripping = threading.Thread(target=drip, daemon=True)
            dripping.start()
            time.sleep(0.1)
            started_at = time.monotonic()
            assert control(port, "terminal/info", {})["runId"] == run_id
            assert time.monotonic() - started_at < 2
            dripping.join(5)
    finally:
        supervisor.close(run_id)
        server.close()


def test_a_transient_accept_failure_leaves_the_control_door_serving(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An `EMFILE` from accept() was re-raised and ended the control thread."""
    supervisor, server, run_id, port = started(registry, tmp_path, monkeypatch, WAITING)
    try:
        door = supervisor._commands[run_id].door
        accept = door.accept
        failures = [
            AcceptError(door.path, OSError(errno.EMFILE, "Too many open files"))
        ]

        def failing() -> object:
            if failures:
                raise failures.pop()
            return accept()

        monkeypatch.setattr(door, "accept", failing)
        # The loop is blocked in the real accept: this call runs it once more.
        assert control(port, "terminal/info", {})["runId"] == run_id
        assert control(port, "terminal/info", {})["runId"] == run_id
        assert not failures
    finally:
        supervisor.close(run_id)
        server.close()


class FullDisk:
    """The retained file on a disk with no space left."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner

    def write(self, data: bytes) -> int:
        raise OSError(errno.ENOSPC, "No space left on device")

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


def test_a_failed_retention_write_keeps_the_command_alive(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The supervisor ended a command whose retention failed: a full disk killed
    the owner's dev server. Retention stops, says why, and the command lives."""
    supervisor, server, run_id, port = started(
        registry,
        tmp_path,
        monkeypatch,
        "#!/bin/sh\nprintf 'door-ready\\n'\nread answer\n"
        "printf 'after-failure\\n'\nread again\n",
    )
    try:
        screen_shows(port, "door-ready")
        command = supervisor._commands[run_id]
        with command.lock:
            command.output = FullDisk(command.output)
        control(port, "terminal/input", {"data": base64.b64encode(b"x\n").decode()})
        screen_shows(port, "after-failure")
        time.sleep(0.5)
        info = control(port, "terminal/info", {})
        assert info["exited"] is None
        assert "No space left" in info["retention"]["failure"]
        assert supervisor.status(run_id).phase.value == "running"
    finally:
        supervisor.close(run_id)
        server.close()
