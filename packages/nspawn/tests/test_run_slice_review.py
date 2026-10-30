from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import replace
from types import SimpleNamespace

from class_dispatch import system1_never_reaches_the_network
from supervisor_support import (
    FakeSpawnedChild,
    _admit_pending,
    _classify_by_exit_code,
    _drain_worker_threads,
    _spawn_real,
    _stop_every_reaper,
    _sv,
    _wire_l8_and_l3,
    fake_picked,
    fake_provider,
    registry,
    roster_home,
)
from test_run_slice import run_slice

from nspawn import app_server, claude_child, custody, mcp, native, picker, supervisor
from nspawn.owner_roster import NativeAccount, Provider, Roster
from nspawn.process_identity import (
    ProcessIdentity,
    capture_process_identity,
    group_is_only_zombies,
    process_identity_matches,
)
from nspawn.redact import SecretNeedles
from nspawn.rows import NativeSessionContext, RunPhase

__all__ = [
    "_drain_worker_threads",
    "_stop_every_reaper",
    "fake_picked",
    "fake_provider",
    "registry",
    "roster_home",
    "run_slice",
    "system1_never_reaches_the_network",
]

# Restore the real inspector after the shared child wiring replaces it.
_inspect_claude_installation = claude_child.inspect_claude_installation


def _public_readonly_start(run_slice, picked, tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    monkeypatch.setattr(picker, "pick_execution", lambda *a, **k: picked)
    monkeypatch.setattr(Roster, "refuse_unservable_spawn", lambda *a: None)
    server = mcp.McpServer(supervisor=run_slice.supervisor)
    accepted = server.nspawn_run_start(
        prompt="Return PONG.",
        cwd=str(tmp_path),
        execution_class="judgment",
        read_only=True,
        notice=False,
    )
    run_id = accepted["run_id"]
    status = run_slice.supervisor.status(run_id, timeout_ms=5_000)
    assert status.phase is RunPhase.COMPLETED, (status.failure_reason, status.warning)
    return server, run_id


def test_probe_a_admitted_uncaged_readonly_has_slice(
    run_slice, fake_picked, registry, tmp_path, monkeypatch
):
    picked = replace(fake_picked, executed_account=None)
    _, run_id = _public_readonly_start(run_slice, picked, tmp_path, monkeypatch)

    assert registry.writer_scope(run_id) is not None, run_id


def test_probe_b_readonly_codex_show_has_slice(run_slice, tmp_path, monkeypatch):
    provider = Provider(
        name="fixture", kind="codex", model="fixture", models=["fixture"]
    )
    picked = picker.PickerResult(None, provider, "fixture", "fixture", None, None)
    launch = native.NativeLaunch(
        ("fixture-codex", "app-server", "--listen", "stdio://"),
        {},
        SecretNeedles.none(),
        NativeSessionContext(str(tmp_path), read_roots=(str(tmp_path),)),
        NativeAccount(tmp_path),
    )
    monkeypatch.setattr(native, "prepare_native", lambda *a, **k: launch)
    monkeypatch.setattr(
        run_slice.supervisor, "_native_readonly_mcp", lambda *a, **k: ({}, {})
    )

    class Turn:
        child = SimpleNamespace(
            identity=ProcessIdentity(424_243, "run-slice-fixture", 2),
            poll=lambda: 0,
            wait=lambda timeout=None: 0,
        )

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def run(self, prompt, **kwargs):
            kwargs["on_thread"](str(uuid.uuid4()))
            return SimpleNamespace(
                quota=None,
                usage={"totalTokens": 7},
                status="completed",
                final_text="PONG",
                error=None,
                quota_warning=None,
                api_error_status=None,
                account_failure=None,
                failure_reason=None,
            )

    def start(argv, *, run_dir, **kwargs):
        (run_dir / "stdout").write_text("{}\n", encoding="utf-8")
        (run_dir / "stderr").write_text("", encoding="utf-8")
        return Turn()

    monkeypatch.setattr(app_server.AppServerTurn, "start", start)
    server, run_id = _public_readonly_start(run_slice, picked, tmp_path, monkeypatch)
    shown = server.nspawn_run_show(run_id)

    assert shown["observation"]["writer_scope"] is not None, shown["observation"]


def _helper_alive(identity):
    return process_identity_matches(identity) and not group_is_only_zombies(
        identity.pid
    )


def _stop_helper(identity):
    if not _helper_alive(identity):
        return
    with contextlib.suppress(ProcessLookupError):
        os.kill(identity.pid, signal.SIGKILL)
    deadline = time.monotonic() + 2
    while _helper_alive(identity) and time.monotonic() < deadline:
        time.sleep(0.01)
    if _helper_alive(identity):
        raise RuntimeError("the identity-checked version helper did not stop")


def test_probe_c_close_stops_detached_version_helper(run_slice, tmp_path, monkeypatch):
    binary = tmp_path / "standin-claude"
    pid_file = tmp_path / "version-helper.pid"
    binary.write_text(
        f"#!{sys.executable}\n"
        "import subprocess\n"
        "from pathlib import Path\n"
        "helper = subprocess.Popen(\n"
        "    ['/usr/bin/sleep', '30'], start_new_session=True,\n"
        "    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,\n"
        "    stderr=subprocess.DEVNULL,\n"
        ")\n"
        f"Path({str(pid_file)!r}).write_text(str(helper.pid))\n"
        "print('2.1.220 (Claude Code)')\n",
        encoding="utf-8",
    )
    binary.chmod(0o700)
    monkeypatch.setattr(claude_child, "resolve_claude_binary", lambda **kwargs: binary)
    monkeypatch.setattr(
        claude_child, "inspect_claude_installation", _inspect_claude_installation
    )
    real_run = subprocess.run
    manager_call = custody.AggregateScope._call
    helpers = []

    def version_run(argv, **kwargs):
        # Emulate only systemd enrollment, never invoke a real systemd launcher.
        # The stand-in and its detached sleep really execute; the manager is fake.
        if str(binary) not in argv or argv[-1] != "--version":
            raise RuntimeError(f"unexpected subprocess in version probe: {argv}")
        unit = next(
            (
                arg.removeprefix("--slice=")
                for arg in argv
                if arg.startswith("--slice=")
            ),
            None,
        )
        completed = real_run([str(binary), "--version"], **kwargs)
        identity = capture_process_identity(int(pid_file.read_text()))
        helpers.append(SimpleNamespace(identity=identity, unit=unit))
        if unit is not None:
            group = custody._manager_group() / custody.aggregate_group(unit)
            (group / "cgroup.events").write_text("populated 1\nfrozen 0\n")
        return completed

    def manager_with_helper(scope, argv):
        if argv[0] == "/usr/bin/systemctl" and argv[2] == "kill":
            for helper in helpers:
                if helper.unit == scope.unit:
                    _stop_helper(helper.identity)
        return manager_call(scope, argv)

    monkeypatch.setattr(subprocess, "run", version_run)
    monkeypatch.setattr(custody.AggregateScope, "_call", manager_with_helper)
    try:
        launch = run_slice.start()
        assert len(helpers) == 1
        helper = helpers[0].identity
        assert _helper_alive(helper)

        closed = mcp.McpServer(supervisor=run_slice.supervisor).nspawn_run_close(
            run_id=launch.run_id
        )

        assert closed.phase is RunPhase.CLOSED
        assert not _helper_alive(helper), "the --version helper survived run close"
    finally:
        for helper in helpers:
            _stop_helper(helper.identity)


def test_probe_e_close_recovers_accepted_slice_after_manager_outage(
    run_slice, registry, tmp_path, fake_picked, monkeypatch
):
    sv = run_slice.supervisor
    sv._stop_tick.set()
    sv._tick_thread.join(timeout=2)
    run_id, thread_id = _admit_pending(
        sv, registry, tmp_path, worktree=str(tmp_path), claim=False
    )
    root = sv._run_root_path / run_id
    run_dir = supervisor.RunDirectory(root, root / "stdout", root / "stderr")
    manager_call = custody.AggregateScope._call

    def unavailable_systemctl(scope, argv):
        if argv[0] == "/usr/bin/systemctl":
            raise custody.ControllerRefusal("fixture manager observation/stop outage")
        return manager_call(scope, argv)

    monkeypatch.setattr(custody.AggregateScope, "_call", unavailable_systemctl)
    try:
        sv._drive_run(
            run_id,
            str(tmp_path),
            run_dir,
            "Return PONG.",
            Roster.read(Roster.default_path()),
            fake_picked,
            thread_id,
            False,
            False,
        )
    finally:
        monkeypatch.setattr(custody.AggregateScope, "_call", manager_call)

    unit = custody.writer_unit(run_id)
    assert unit in run_slice.starts
    assert sv.status(run_id).phase is RunPhase.FAILED
    closed = mcp.McpServer(supervisor=sv).nspawn_run_close(run_id=run_id)
    assert closed.phase is RunPhase.CLOSED

    assert unit in run_slice.stopped, "close lost the accepted slice's cleanup handle"


def test_probe_d_mocked_worker_never_reaches_the_user_manager(
    registry, tmp_path, roster_home, monkeypatch, fake_picked
):
    def refuse():
        raise custody.ControllerRefusal("fixture: no user manager bus")

    monkeypatch.setattr(custody, "user_manager_environment", refuse)
    _wire_l8_and_l3(monkeypatch, lambda *a, **k: FakeSpawnedChild(_spawn_real("true")))
    monkeypatch.setattr(
        supervisor.terminal, "classify_terminal_run", _classify_by_exit_code
    )
    sv = _sv(registry, tmp_path)
    roster = Roster.read(Roster.default_path())
    run_id, thread_id = _admit_pending(sv, registry, tmp_path, worktree=str(tmp_path))
    root = sv._run_root_path / run_id
    run_dir = supervisor.RunDirectory(root, root / "stdout", root / "stderr")
    sv._drive_run(
        run_id,
        str(tmp_path),
        run_dir,
        "hi",
        roster,
        fake_picked,
        thread_id,
        False,
        False,
    )

    status = sv.status(run_id)
    assert status.phase is RunPhase.COMPLETED, (status.phase, status.failure_reason)
