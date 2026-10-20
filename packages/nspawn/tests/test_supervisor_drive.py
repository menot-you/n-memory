"""Supervisor drive contracts."""

from __future__ import annotations

import json
import os
import sys
import threading
from dataclasses import replace
from pathlib import Path

import nott_database
import pytest
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

from nspawn import claude_child, picker, supervisor, terminal
from nspawn.child import SESSION_ID_ENV
from nspawn.limits import MAX_DURABLE_ROW_BYTES
from nspawn.process_identity import ProcessIdentity
from nspawn.redact import SecretNeedles
from nspawn.registry import Registry, RegistryError
from nspawn.rows import RunPhase
from nspawn.supervisor import (
    InvalidRunRequest,
    Supervisor,
)

# Export imported fixtures so pytest discovers them in this module.
__all__ = [
    "_drain_worker_threads",
    "_stop_every_reaper",
    "fake_picked",
    "fake_provider",
    "registry",
    "roster_home",
]


class _ExitedProcess:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.returncode = 0
        #: The supervisor writes the prompt here; the child already exited.
        self.stdin = open(os.devnull, "wb")  # noqa: SIM115 -- closed by close_stdin

    def wait(self, timeout: float | None = None) -> int:
        return 0


class _JoinGatedPump:
    """Withhold final output until the child's wait path joins this pump."""

    def __init__(self, path: Path, payload: bytes) -> None:
        self._path = path
        self._payload = payload
        self.join_calls: list[float | None] = []

    def join(self, timeout: float | None = None) -> None:
        self.join_calls.append(timeout)
        self._path.write_bytes(self._payload)


def test_drive_run_classifies_only_after_wait_flushes_the_result_pump(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_picked: picker.PickerResult,
) -> None:
    sv = _sv(registry, tmp_path)
    from nspawn.owner_roster import Roster

    roster = Roster.read(Roster.default_path())  # type: ignore[arg-type]
    run_id, thread_id = _admit_pending(sv, registry, tmp_path, worktree=str(tmp_path))
    run_dir = supervisor.RunDirectory(
        root=sv._run_root_path / run_id,
        stdout=sv._run_root_path / run_id / "stdout",
        stderr=sv._run_root_path / run_id / "stderr",
    )
    stdout_pump = _JoinGatedPump(
        run_dir.stdout,
        b'{"type":"assistant"}\n'
        b'{"type":"system","subtype":"thinking_tokens"}\n'
        b'{"type":"result","is_error":false,"terminal_reason":"completed",'
        b'"result":"PONG","usage":{"input_tokens":2,"output_tokens":3}}\n',
    )
    stderr_pump = _JoinGatedPump(run_dir.stderr, b"")
    process = _ExitedProcess(pid=424_242)
    spawned = supervisor.child.SpawnedChild(  # type: ignore[arg-type]
        process,
        ProcessIdentity(pid=process.pid, boot_id="e" * 8, start_ticks=5),
        stdout_pump,
        stderr_pump,
        run_dir.root,
    )
    _wire_l8_and_l3(monkeypatch, lambda *a, **k: spawned)

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
    assert stdout_pump.join_calls == [None]
    assert stderr_pump.join_calls == [None]
    assert status.phase is RunPhase.COMPLETED
    assert status.tokens == 5
    assert sv.result(run_id).final_text == "PONG"
    assert registry.get_run(run_id).records_seen == 3


def test_drive_run_reaches_completed_on_the_happy_path(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_picked: picker.PickerResult,
) -> None:
    _wire_l8_and_l3(monkeypatch, lambda *a, **k: FakeSpawnedChild(_spawn_real("true")))
    monkeypatch.setattr(
        supervisor.terminal, "classify_terminal_run", _classify_by_exit_code
    )
    sv = _sv(registry, tmp_path)
    from nspawn.owner_roster import Roster

    roster = Roster.read(Roster.default_path())  # type: ignore[arg-type]
    run_id, thread_id = _admit_pending(sv, registry, tmp_path, worktree=str(tmp_path))
    run_dir = supervisor.RunDirectory(
        root=sv._run_root_path / run_id,
        stdout=sv._run_root_path / run_id / "stdout",
        stderr=sv._run_root_path / run_id / "stderr",
    )
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
    assert status.phase is RunPhase.COMPLETED
    # An exited child is out of custody: a later shutdown must not try to
    # signal a reaped identity (the driver logged ChildIdentityError on every
    # exit once each run got its own supervisor).
    assert run_id not in sv._children


def test_drive_run_passes_read_paths_to_the_child_composer(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_picked: picker.PickerResult,
) -> None:
    _wire_l8_and_l3(monkeypatch, lambda *a, **k: FakeSpawnedChild(_spawn_real("true")))
    seen: dict[str, object] = {}

    def _capture(*args: object, **kwargs: object) -> list[str]:
        seen.update(kwargs)
        return ["claude", "-p"]

    monkeypatch.setattr(supervisor.child, "compose_child_argv", _capture)
    monkeypatch.setattr(
        supervisor.terminal, "classify_terminal_run", _classify_by_exit_code
    )
    sv = _sv(registry, tmp_path)
    from nspawn.owner_roster import Roster

    roster = Roster.read(Roster.default_path())  # type: ignore[arg-type]
    run_id, thread_id = _admit_pending(sv, registry, tmp_path, worktree=str(tmp_path))
    run_dir = supervisor.RunDirectory(
        root=sv._run_root_path / run_id,
        stdout=sv._run_root_path / run_id / "stdout",
        stderr=sv._run_root_path / run_id / "stderr",
    )
    paths = ("/opt/test/donor-pin", "/opt/test/a path;printf HACKED")

    sv._drive_run(
        run_id,
        str(tmp_path),
        run_dir,
        "hi",
        roster,
        fake_picked,
        thread_id,
        False,
        True,
        paths,
    )

    assert sv.status(run_id).phase is RunPhase.COMPLETED
    assert seen["read_only"] is True
    assert seen["read_paths"] == paths


@pytest.mark.parametrize("resume", [False, True], ids=["fresh", "resume"])
def test_drive_run_binds_the_child_env_to_the_session_it_launches(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_picked: picker.PickerResult,
    resume: bool,
) -> None:
    # The Stop hooks leave a child's final text alone only in the session nSPAWN
    # launched: the env names the very id the argv hands the vendor, fresh or resumed.
    spawned_env: dict[str, str] = {}

    def _spawn(*args: object, **kwargs: object) -> FakeSpawnedChild:
        env = args[2]
        assert isinstance(env, dict)
        spawned_env.update(env)
        return FakeSpawnedChild(_spawn_real("true"))

    _wire_l8_and_l3(monkeypatch, _spawn)
    seen: dict[str, object] = {}
    composed: dict[str, object] = {}

    def _capture(
        *args: object, **kwargs: object
    ) -> tuple[dict[str, str], SecretNeedles]:
        seen.update(kwargs)
        return {}, SecretNeedles.none()

    def _compose(*args: object, **kwargs: object) -> list[str]:
        composed.update(kwargs)
        return ["claude", "-p"]

    monkeypatch.setattr(supervisor.child, "build_child_env", _capture)
    monkeypatch.setattr(supervisor.child, "compose_child_argv", _compose)
    monkeypatch.setattr(
        supervisor.terminal, "classify_terminal_run", _classify_by_exit_code
    )
    sv = _sv(registry, tmp_path)
    from nspawn.owner_roster import Roster

    roster = Roster.read(Roster.default_path())  # type: ignore[arg-type]
    run_id, thread_id = _admit_pending(sv, registry, tmp_path, worktree=str(tmp_path))
    run_dir = supervisor.RunDirectory(
        root=sv._run_root_path / run_id,
        stdout=sv._run_root_path / run_id / "stdout",
        stderr=sv._run_root_path / run_id / "stderr",
    )

    sv._drive_run(
        run_id,
        str(tmp_path),
        run_dir,
        "hi",
        roster,
        fake_picked,
        thread_id,
        resume,
        False,
    )

    assert sv.status(run_id).phase is RunPhase.COMPLETED
    assert (composed["session_id"], composed["resume"]) == (thread_id, resume)
    assert spawned_env[SESSION_ID_ENV] == thread_id
    assert seen["run_id"] == run_id


@pytest.mark.parametrize(
    "invalid",
    [
        pytest.param("x" * (MAX_DURABLE_ROW_BYTES + 1), id="byte-limit"),
        pytest.param("\ud800", id="surrogate"),
        pytest.param("\x00", id="postgres-null-byte"),
    ],
)
def test_execute_refuses_invalid_system_prompt_before_creating_run_directory(
    invalid: str,
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_picked: picker.PickerResult,
) -> None:
    monkeypatch.setattr(
        supervisor.picker, "pick_execution", lambda *a, **k: fake_picked
    )
    sv = _sv(registry, tmp_path)

    with pytest.raises(InvalidRunRequest):
        sv.execute("work", str(tmp_path), system_prompt=invalid)

    assert list(sv._run_root_path.iterdir()) == []


def test_row_construction_failure_leaves_no_run_directory(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_picked: picker.PickerResult,
) -> None:
    monkeypatch.setattr(
        supervisor.picker, "pick_execution", lambda *a, **k: fake_picked
    )

    def _reject_row(**_: object) -> object:
        raise ValueError("row rejected")

    monkeypatch.setattr(supervisor, "RunRow", _reject_row)
    sv = _sv(registry, tmp_path)

    with pytest.raises(ValueError, match="row rejected"):
        sv.execute("work", str(tmp_path), system_prompt="valid", read_only=True)

    assert list(sv._run_root_path.iterdir()) == []


def test_execute_persists_and_transports_the_exact_system_prompt(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_picked: picker.PickerResult,
) -> None:
    system_prompt = (
        "Persona: cite `evidence`.\nDo not interpret: $(printf LEAK); --flag\n"
    )
    monkeypatch.setattr(
        supervisor.picker, "pick_execution", lambda *a, **k: fake_picked
    )
    sv = _sv(registry, tmp_path)
    captured: list[object] = []
    worker_finished = threading.Event()

    def _capture(*args: object) -> None:
        captured.extend(args)
        worker_finished.set()

    monkeypatch.setattr(sv, "_drive_run", _capture)

    accepted = sv.execute(
        "work", str(tmp_path), system_prompt=system_prompt, read_only=True
    )

    assert worker_finished.wait(timeout=2)
    row = registry.get_run(accepted.run_id)
    assert row is not None
    assert row.system_prompt == system_prompt
    assert system_prompt in captured


def test_drive_run_uses_a_private_ephemeral_system_prompt_file(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_picked: picker.PickerResult,
) -> None:
    system_prompt = (
        "Persona: cite `evidence`.\nDo not interpret: $(printf LEAK); --flag\n"
    )
    global_prompt = b"GLOBAL NOTT LAW\n"
    plugin_root = tmp_path / "plugin"
    plugin_root.mkdir()
    (plugin_root / "NOTT.md").write_bytes(global_prompt)
    (roster_home / ".nott" / "spawn.toml").write_text(
        f'[defaults]\nplugins = ["{plugin_root}"]\n'
        'args = ["--append-system-prompt-file", '
        '"$PLUGIN/NOTT.md"]\n',
        encoding="utf-8",
    )
    original_compose = supervisor.child.compose_child_argv
    _wire_l8_and_l3(monkeypatch, lambda *a, **k: FakeSpawnedChild(_spawn_real("true")))
    seen: dict[str, object] = {}

    def _capture(*args: object, **kwargs: object) -> list[str]:
        prompt_file = kwargs["append_system_prompt_file"]
        assert isinstance(prompt_file, Path)
        seen["path"] = prompt_file
        seen["bytes"] = prompt_file.read_bytes()
        argv = original_compose(*args, **kwargs)  # type: ignore[arg-type]
        seen["argv"] = argv
        return argv

    monkeypatch.setattr(supervisor.child, "compose_child_argv", _capture)
    monkeypatch.setattr(
        supervisor.terminal, "classify_terminal_run", _classify_by_exit_code
    )
    sv = _sv(registry, tmp_path)
    from nspawn.owner_roster import Roster

    roster = Roster.read(Roster.default_path())  # type: ignore[arg-type]
    run_id, thread_id = _admit_pending(sv, registry, tmp_path, worktree=str(tmp_path))
    run_dir = supervisor.RunDirectory(
        root=sv._run_root_path / run_id,
        stdout=sv._run_root_path / run_id / "stdout",
        stderr=sv._run_root_path / run_id / "stderr",
    )

    sv._drive_run(
        run_id,
        str(tmp_path),
        run_dir,
        "work",
        roster,
        fake_picked,
        thread_id,
        False,
        False,
        system_prompt=system_prompt,
    )

    status = sv.status(run_id)
    assert status.phase is RunPhase.COMPLETED, status.failure_reason
    assert seen["bytes"] == system_prompt.encode("utf-8") + b"\n\n" + global_prompt
    argv = seen["argv"]
    assert isinstance(argv, list)
    assert system_prompt not in argv
    assert argv.count("--append-system-prompt-file") == 1
    assert isinstance(seen["path"], Path)
    assert argv[argv.index("--append-system-prompt-file") + 1] == str(seen["path"])
    assert not seen["path"].exists()


@pytest.mark.parametrize(
    "system_prompt",
    [
        None,
        "Persona: cite `evidence`.\n",
        "Quote the title:\n# **NOTT** System Prompt v9\nand nothing else.\n",
    ],
    ids=["roster-prompt", "dispatch-prompt", "dispatch-quotes-the-title"],
)
@pytest.mark.parametrize(
    ("global_prompt", "silenced"),
    [
        (b"# **NOTT** System Prompt v9\n\nThe law.\n", True),
        (b"A persona.\n", False),
        (b"A persona that quotes:\n# **NOTT** System Prompt v9\n", False),
        (None, False),
    ],
    ids=["law", "no-law", "roster-quotes-the-title", "no-append"],
)
def test_a_child_that_appends_the_law_silences_the_plugin_copy(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_picked: picker.PickerResult,
    system_prompt: str | None,
    global_prompt: bytes | None,
    silenced: bool,
) -> None:
    """The plugin's law-session.sh prints the whole law at SessionStart; a child whose
    appended system prompt already holds it would read it twice. Off only then: a child
    appending other text keeps the plugin's copy, its only one. The roster's own file
    decides, never the dispatch's text, which is combined IN FRONT of it: a dispatch
    that quotes the law's title must not silence the only copy a child would get."""
    plugin_root = tmp_path / "plugin"
    plugin_root.mkdir()
    roster = f'[defaults]\nplugins = ["{plugin_root}"]\n'
    if global_prompt is not None:
        (plugin_root / "NOTT.md").write_bytes(global_prompt)
        roster += 'args = ["--append-system-prompt-file", "$PLUGIN/NOTT.md"]\n'
    (roster_home / ".nott" / "spawn.toml").write_text(roster, encoding="utf-8")
    monkeypatch.delenv("NOTT_LAW_SESSION", raising=False)
    original_compose = supervisor.child.compose_child_argv
    seen: dict[str, object] = {}

    def _spawn(*args: object, **kwargs: object) -> FakeSpawnedChild:
        seen["env"] = args[2]
        return FakeSpawnedChild(_spawn_real("true"))

    _wire_l8_and_l3(monkeypatch, _spawn)
    monkeypatch.setattr(supervisor.child, "compose_child_argv", original_compose)
    monkeypatch.setattr(
        supervisor.terminal, "classify_terminal_run", _classify_by_exit_code
    )
    sv = _sv(registry, tmp_path)
    from nspawn.owner_roster import Roster

    roster = Roster.read(Roster.default_path())  # type: ignore[arg-type]
    run_id, thread_id = _admit_pending(sv, registry, tmp_path, worktree=str(tmp_path))
    run_dir = supervisor.RunDirectory(
        root=sv._run_root_path / run_id,
        stdout=sv._run_root_path / run_id / "stdout",
        stderr=sv._run_root_path / run_id / "stderr",
    )

    sv._drive_run(
        run_id,
        str(tmp_path),
        run_dir,
        "work",
        roster,
        fake_picked,
        thread_id,
        False,
        False,
        system_prompt=system_prompt,
    )

    env = seen["env"]
    assert isinstance(env, dict)
    assert (env.get("NOTT_LAW_SESSION") == "off") is silenced


def test_drive_run_refuses_a_preexisting_system_prompt_symlink(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_picked: picker.PickerResult,
) -> None:
    sv = _sv(registry, tmp_path)
    from nspawn.owner_roster import Roster

    roster = Roster.read(Roster.default_path())  # type: ignore[arg-type]
    run_id, thread_id = _admit_pending(sv, registry, tmp_path, worktree=str(tmp_path))
    run_dir = supervisor.RunDirectory(
        root=sv._run_root_path / run_id,
        stdout=sv._run_root_path / run_id / "stdout",
        stderr=sv._run_root_path / run_id / "stderr",
    )
    victim = tmp_path / "victim"
    victim.write_bytes(b"ORIGINAL")
    prompt_path = run_dir.root / "system-prompt"
    prompt_path.symlink_to(victim)
    spawned = False

    def _record_spawn(*args: object, **kwargs: object) -> None:
        nonlocal spawned
        spawned = True

    monkeypatch.setattr(sv, "_spawn_and_wait", _record_spawn)

    sv._drive_run(
        run_id,
        str(tmp_path),
        run_dir,
        "work",
        roster,
        fake_picked,
        thread_id,
        False,
        False,
        system_prompt="DISPATCH SECRET",
    )

    assert victim.read_bytes() == b"ORIGINAL"
    assert prompt_path.is_symlink()
    assert not spawned
    assert sv.status(run_id).phase is RunPhase.FAILED


def test_drive_run_records_system_prompt_cleanup_failure(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_picked: picker.PickerResult,
) -> None:
    sv = _sv(registry, tmp_path)
    from nspawn.owner_roster import Roster

    roster = Roster.read(Roster.default_path())  # type: ignore[arg-type]
    run_id, thread_id = _admit_pending(sv, registry, tmp_path, worktree=str(tmp_path))
    run_dir = supervisor.RunDirectory(
        root=sv._run_root_path / run_id,
        stdout=sv._run_root_path / run_id / "stdout",
        stderr=sv._run_root_path / run_id / "stderr",
    )
    prompt_path = run_dir.root / "system-prompt"

    def _complete(*args: object, **kwargs: object) -> None:
        sv._update_phase(run_id, RunPhase.COMPLETED)

    def _refuse_unlink(path: Path, *, missing_ok: bool = False) -> None:
        assert path == prompt_path
        raise PermissionError("cleanup blocked")

    monkeypatch.setattr(sv, "_spawn_and_wait", _complete)
    monkeypatch.setattr(Path, "unlink", _refuse_unlink)

    sv._drive_run(
        run_id,
        str(tmp_path),
        run_dir,
        "work",
        roster,
        fake_picked,
        thread_id,
        False,
        False,
        system_prompt="DISPATCH SECRET",
    )

    # Review round 2: a cleanup that fails after COMPLETED never replaces it;
    # the leftover is a best-effort diagnostic line, never a second terminal.
    status = sv.status(run_id)
    assert status.phase is RunPhase.COMPLETED
    assert status.failure_reason is None
    assert "RunDirectoryError" not in (status.warning or "")
    late = (run_dir.root / "late_writes").read_text(encoding="utf-8")
    assert "a failed write was not applied; reason='RunDirectoryError'" in late
    assert prompt_path.exists()


def test_drive_run_reaches_failed_on_nonzero_exit(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_picked: picker.PickerResult,
) -> None:
    _wire_l8_and_l3(monkeypatch, lambda *a, **k: FakeSpawnedChild(_spawn_real("false")))
    monkeypatch.setattr(
        supervisor.terminal, "classify_terminal_run", _classify_by_exit_code
    )
    sv = _sv(registry, tmp_path)
    from nspawn.owner_roster import Roster

    roster = Roster.read(Roster.default_path())  # type: ignore[arg-type]
    run_id, thread_id = _admit_pending(sv, registry, tmp_path, worktree=str(tmp_path))
    run_dir = supervisor.RunDirectory(
        root=sv._run_root_path / run_id,
        stdout=sv._run_root_path / run_id / "stdout",
        stderr=sv._run_root_path / run_id / "stderr",
    )
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
    assert sv.status(run_id).phase is RunPhase.FAILED


@pytest.mark.parametrize(
    "break_point",
    [
        "plugin_root",
        "resolve_binary",
        "install",
        "mcp_config",
        "env",
        "argv",
        "spawn",
        "classify",
    ],
)
def test_drive_run_marks_failed_for_every_dependency_refusal(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_picked: picker.PickerResult,
    break_point: str,
) -> None:
    from nspawn.owner_roster import Roster

    if break_point == "plugin_root":
        (roster_home / ".nott" / "spawn.toml").write_text("", encoding="utf-8")

    def _explode(*a: object, **k: object) -> None:
        raise supervisor.SupervisorError(f"boom at {break_point}")

    _wire_l8_and_l3(monkeypatch, lambda *a, **k: FakeSpawnedChild(_spawn_real("true")))
    monkeypatch.setattr(
        supervisor.terminal, "classify_terminal_run", _classify_by_exit_code
    )
    if break_point == "resolve_binary":
        monkeypatch.setattr(supervisor.claude_child, "resolve_claude_binary", _explode)
    elif break_point == "install":
        monkeypatch.setattr(
            supervisor.claude_child, "inspect_claude_installation", _explode
        )
    elif break_point == "mcp_config":
        monkeypatch.setattr(
            supervisor.claude_child, "inspect_child_mcp_config", _explode
        )
    elif break_point == "env":
        monkeypatch.setattr(supervisor.child, "build_child_env", _explode)
    elif break_point == "argv":
        monkeypatch.setattr(supervisor.child, "compose_child_argv", _explode)
    elif break_point == "spawn":
        monkeypatch.setattr(supervisor.child, "spawn_child", _explode)
    elif break_point == "classify":
        monkeypatch.setattr(supervisor.terminal, "classify_terminal_run", _explode)

    sv = _sv(registry, tmp_path)
    roster = Roster.read(Roster.default_path())  # type: ignore[arg-type]
    run_id, thread_id = _admit_pending(sv, registry, tmp_path, worktree=str(tmp_path))
    run_dir = supervisor.RunDirectory(
        root=sv._run_root_path / run_id,
        stdout=sv._run_root_path / run_id / "stdout",
        stderr=sv._run_root_path / run_id / "stderr",
    )
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
    assert status.phase is RunPhase.FAILED
    assert status.marker is None


def test_drive_run_records_provider_and_account_walls(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_picked: picker.PickerResult,
) -> None:
    recorded_provider: list[tuple[object, ...]] = []
    recorded_account: list[tuple[object, ...]] = []

    def _fake_classify(*a: object, **k: object) -> terminal.TerminalClassification:
        return terminal.TerminalClassification(
            phase=RunPhase.FAILED,
            final_text=None,
            warning="rate limited",
            marker=None,
            provider_wall=terminal.ProviderWallRecord(
                provider="acme",
                cooling_until=999,
                source=terminal.WallSource.VENDOR_RESET,
                observed_at=1,
            ),
            account_wall=terminal.AccountWallRecord(
                account_alias="acct-1",
                cooling_until=999,
                source=terminal.WallSource.VENDOR_RESET,
                limit_type="5h",
                observed_at=1,
            ),
            tokens=0,
        )

    monkeypatch.setattr(
        Registry,
        "record_provider_wall",
        lambda self, *a: recorded_provider.append(a),
    )
    monkeypatch.setattr(
        Registry,
        "record_account_wall",
        lambda self, *a: recorded_account.append(a),
    )
    _wire_l8_and_l3(monkeypatch, lambda *a, **k: FakeSpawnedChild(_spawn_real("true")))
    monkeypatch.setattr(supervisor.terminal, "classify_terminal_run", _fake_classify)
    sv = _sv(registry, tmp_path)
    from nspawn.owner_roster import Roster

    roster = Roster.read(Roster.default_path())  # type: ignore[arg-type]
    run_id, thread_id = _admit_pending(sv, registry, tmp_path, worktree=str(tmp_path))
    run_dir = supervisor.RunDirectory(
        root=sv._run_root_path / run_id,
        stdout=sv._run_root_path / run_id / "stdout",
        stderr=sv._run_root_path / run_id / "stderr",
    )
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

    assert recorded_provider and recorded_provider[0][0] == "acme"
    assert recorded_account and recorded_account[0][0] == "acct-1"


def _classify_with_usage(
    *windows: terminal.WindowUsage,
) -> object:
    def _classify(*a: object, **k: object) -> terminal.TerminalClassification:
        return terminal.TerminalClassification(
            phase=RunPhase.COMPLETED,
            final_text="PONG",
            warning=None,
            marker=None,
            provider_wall=None,
            account_wall=None,
            tokens=3,
            usage=windows,
        )

    return _classify


def _drive_once(
    sv: Supervisor,
    registry: Registry,
    tmp_path: Path,
    picked: picker.PickerResult,
) -> str:
    """Admit a row and drive its worker synchronously; return the run id."""
    from nspawn.owner_roster import Roster

    roster = Roster.read(Roster.default_path())  # type: ignore[arg-type]
    run_id, thread_id = _admit_pending(sv, registry, tmp_path, worktree=str(tmp_path))
    run_dir = supervisor.RunDirectory(
        root=sv._run_root_path / run_id,
        stdout=sv._run_root_path / run_id / "stdout",
        stderr=sv._run_root_path / run_id / "stderr",
    )
    sv._drive_run(
        run_id,
        str(tmp_path),
        run_dir,
        "hi",
        roster,
        picked,
        thread_id,
        False,
        False,
    )
    return run_id


def test_drive_run_persists_every_observed_window_under_the_executing_account(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_picked: picker.PickerResult,
) -> None:
    """The wire's `unifiedWindows` reach the durable table, keyed by the alias
    that really executed — read back from PostgreSQL, never from a recorder."""
    _wire_l8_and_l3(monkeypatch, lambda *a, **k: FakeSpawnedChild(_spawn_real("true")))
    monkeypatch.setattr(
        supervisor.terminal,
        "classify_terminal_run",
        _classify_with_usage(
            terminal.WindowUsage(window="5h", utilization=0.42, resets_at=9_000),
            terminal.WindowUsage(window="7d", utilization=0.07, resets_at=99_000),
        ),
    )
    sv = _sv(registry, tmp_path)

    _drive_once(sv, registry, tmp_path, fake_picked)

    stored = registry.get_account_usage()
    assert [
        (row.account_alias, row.usage_window, row.utilization) for row in stored
    ] == [
        ("acct-1", "5h", 0.42),
        ("acct-1", "7d", 0.07),
    ]
    assert stored[0].resets_at == 9_000
    assert stored[1].resets_at == 99_000
    assert stored[0].observed_at > 0


def test_drive_run_stores_no_usage_when_no_account_executed(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_picked: picker.PickerResult,
) -> None:
    """The env-route quadrant: windows were observed, but no alias owns them, so
    the table stays EMPTY rather than gaining a row under an invented name."""
    _wire_l8_and_l3(monkeypatch, lambda *a, **k: FakeSpawnedChild(_spawn_real("true")))
    monkeypatch.setattr(
        supervisor.terminal,
        "classify_terminal_run",
        _classify_with_usage(
            terminal.WindowUsage(window="5h", utilization=0.42, resets_at=9_000)
        ),
    )
    sv = _sv(registry, tmp_path)

    _drive_once(sv, registry, tmp_path, replace(fake_picked, executed_account=None))

    assert registry.get_account_usage() == ()


def test_drive_run_stores_nothing_when_the_wire_carried_no_windows(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_picked: picker.PickerResult,
) -> None:
    """An alias with an empty window tuple writes no row — the older claude
    builds carry no `unifiedWindows` at all, and absence is not a reading."""
    _wire_l8_and_l3(monkeypatch, lambda *a, **k: FakeSpawnedChild(_spawn_real("true")))
    monkeypatch.setattr(
        supervisor.terminal, "classify_terminal_run", _classify_with_usage()
    )
    sv = _sv(registry, tmp_path)

    _drive_once(sv, registry, tmp_path, fake_picked)

    assert registry.get_account_usage() == ()


def test_fail_run_and_update_phase_are_a_no_op_for_a_vanished_row(
    registry: Registry, tmp_path: Path
) -> None:
    sv = _sv(registry, tmp_path)
    sv._update_phase("no-such-run", RunPhase.FAILED, blocked_reason="x")  # no raise
    sv._fail_run("no-such-run", RuntimeError("x"))  # no raise


def test_update_phase_is_a_no_op_when_the_row_already_reads_closed(
    registry: Registry, tmp_path: Path
) -> None:
    sv = _sv(registry, tmp_path)
    run_id, _ = _admit_pending(sv, registry, tmp_path, worktree=str(tmp_path))
    sv.close(run_id)
    sv._update_phase(run_id, RunPhase.FAILED, blocked_reason="late write")  # no raise
    assert registry.get_run(run_id).phase is RunPhase.CLOSED  # type: ignore[union-attr]


def test_update_phase_absorbs_a_close_that_wins_the_race(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The row closes strictly BETWEEN this call's read and its write: the read
    sees a still-open snapshot, but the real fence underneath has already
    moved to closed — `update_run` itself raises `ClosedRun`, which
    `_update_phase` must absorb rather than let escape the worker thread."""
    sv = _sv(registry, tmp_path)
    run_id, _ = _admit_pending(sv, registry, tmp_path, worktree=str(tmp_path))
    stale_open_row = registry.get_run(run_id)
    assert stale_open_row is not None
    sv.close(run_id)  # the row is now genuinely CLOSED in the database

    real_get_run = Registry.get_run
    calls = {"n": 0}

    def _stale_then_real(self: Registry, target_run_id: str) -> object:
        calls["n"] += 1
        if calls["n"] == 1:
            return stale_open_row  # what _update_phase's own read observes
        return real_get_run(self, target_run_id)

    monkeypatch.setattr(Registry, "get_run", _stale_then_real)
    sv._update_phase(run_id, RunPhase.FAILED, blocked_reason="race")  # no raise
    assert registry.get_run(run_id).phase is RunPhase.CLOSED  # type: ignore[union-attr]


def _classify_with_telemetry(
    stdout_path: object, stderr_path: object, exit_code: int, **kwargs: object
) -> terminal.TerminalClassification:
    return terminal.TerminalClassification(
        phase=RunPhase.COMPLETED if exit_code == 0 else RunPhase.FAILED,
        final_text="PONG" if exit_code == 0 else None,
        warning=None,
        marker=None,
        provider_wall=None,
        account_wall=None,
        tokens=7,
        telemetry=terminal.RunTelemetry(
            cost_usd="0.05",
            input_tokens=100,
            output_tokens=20,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
            duration_ms=500,
            duration_api_ms=450,
            num_turns=1,
            ttft_ms=None,
            ttft_stream_ms=None,
            time_to_request_ms=None,
            stop_reason="end_turn",
            api_error_status=None,
            permission_denials=0,
            subagent_spawned=0,
            model_usage=(),
        ),
    )


def test_a_completed_run_appends_a_usage_event_to_the_journal(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_picked: picker.PickerResult,
) -> None:
    _wire_l8_and_l3(monkeypatch, lambda *a, **k: FakeSpawnedChild(_spawn_real("true")))
    monkeypatch.setattr(
        supervisor.terminal, "classify_terminal_run", _classify_with_telemetry
    )
    sv = _sv(registry, tmp_path)
    run_id = _drive_once(sv, registry, tmp_path, fake_picked)

    events = registry.read_run_events(run_id)
    assert [(event.seq, event.kind) for event in events] == [
        (1, "lifecycle"),
        (2, "usage"),
    ]
    assert events[0].payload == {
        "usage_pending": "exec",
        "usage_managed": False,
        "usage_identity": None,
    }
    assert isinstance(events[1].payload, dict)
    assert events[1].payload["cost_usd"] == "0.05"
    assert registry.verify_run_event_chain(run_id) is True


def test_a_telemetry_refusal_never_kills_the_run(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_picked: picker.PickerResult,
) -> None:
    _wire_l8_and_l3(monkeypatch, lambda *a, **k: FakeSpawnedChild(_spawn_real("true")))
    monkeypatch.setattr(
        supervisor.terminal, "classify_terminal_run", _classify_with_telemetry
    )
    sv = _sv(registry, tmp_path)

    def _refuse(*args: object, **kwargs: object) -> None:
        raise RegistryError("the run event could not be appended: planted")

    monkeypatch.setattr(registry, "append_run_event", _refuse)
    run_id = _drive_once(sv, registry, tmp_path, fake_picked)

    assert sv.status(run_id).phase is RunPhase.COMPLETED


def test_a_telemetry_refusal_is_named_in_the_runs_own_warning(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_picked: picker.PickerResult,
) -> None:
    _wire_l8_and_l3(monkeypatch, lambda *a, **k: FakeSpawnedChild(_spawn_real("true")))
    monkeypatch.setattr(
        supervisor.terminal, "classify_terminal_run", _classify_with_telemetry
    )
    sv = _sv(registry, tmp_path)

    def _refuse(*args: object, **kwargs: object) -> None:
        raise RegistryError("planted refusal")

    monkeypatch.setattr(registry, "append_run_event", _refuse)
    run_id = _drive_once(sv, registry, tmp_path, fake_picked)

    row = registry.get_run(run_id)
    assert row is not None
    assert row.warning is not None
    assert "telemetry not recorded" in row.warning
    assert "planted refusal" in row.warning


def test_publish_terminal_handles_missing_row_and_preexisting_warning(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sv = _sv(registry, tmp_path)
    # Missing row in registry
    classification = _classify_with_telemetry(None, None, 0)
    classification = replace(classification, warning="preexisting warning")

    def _refuse(*args: object, **kwargs: object) -> None:
        raise RegistryError("planted refusal")

    monkeypatch.setattr(registry, "append_run_event", _refuse)
    # When run_id does not exist, _publish_terminal should not raise and handles warning
    sv._publish_terminal("nonexistent-run-id", classification)


def test_publish_terminal_store_error_fallback(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sv = _sv(registry, tmp_path)
    run_id, _ = _admit_pending(sv, registry, tmp_path, worktree=str(tmp_path))
    classification = terminal.TerminalClassification(
        phase=RunPhase.COMPLETED,
        final_text="ALL_DONE",
        warning=None,
        marker="DONE",
        provider_wall=None,
        account_wall=None,
        tokens=100,
    )

    def _broken_update_run(row: object, **_: object) -> None:
        raise nott_database.OperationalError("server connection closed")

    monkeypatch.setattr(registry, "update_run", _broken_update_run)
    # Should not raise exception
    sv._publish_terminal(run_id, classification)

    # Check fallback terminal file in run_dir
    terminal_file = sv._run_root_path / run_id / "terminal.json"
    assert terminal_file.is_file()
    data = json.loads(terminal_file.read_text(encoding="utf-8"))
    assert data["run_id"] == run_id
    assert data["final_text"] == "ALL_DONE"
    assert data["phase"] == "completed"


def test_publish_terminal_fallback_handles_oserror(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sv = _sv(registry, tmp_path)
    run_id, _ = _admit_pending(sv, registry, tmp_path, worktree=str(tmp_path))
    classification = terminal.TerminalClassification(
        phase=RunPhase.COMPLETED,
        final_text="ALL_DONE",
        warning=None,
        marker="DONE",
        provider_wall=None,
        account_wall=None,
        tokens=100,
    )

    def _broken_update(row: object, **_: object) -> None:
        raise nott_database.OperationalError("db error")

    def _broken_write(*a: object, **k: object) -> int:
        raise OSError("disk error")

    monkeypatch.setattr(registry, "update_run", _broken_update)
    monkeypatch.setattr(supervisor, "_open_regular", _broken_write)
    sv._publish_terminal(run_id, classification)
    assert run_id in sv._unpublished


def _drive_into_binary_refusal(
    sv: Supervisor,
    run_id: str,
    thread_id: str,
    tmp_path: Path,
    fake_picked: picker.PickerResult,
) -> None:

    from nspawn.owner_roster import Roster

    roster = Roster.read(Roster.default_path())  # type: ignore[arg-type]
    run_dir = supervisor.RunDirectory(
        root=sv._run_root_path / run_id,
        stdout=sv._run_root_path / run_id / "stdout",
        stderr=sv._run_root_path / run_id / "stderr",
    )
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


@pytest.fixture
def refuses_the_binary(monkeypatch: pytest.MonkeyPatch) -> None:

    def _refuse(binary: Path, probe: object) -> claude_child.ClaudeInstallation:
        raise claude_child.BinaryIdentityError(str(binary), "no such file")

    monkeypatch.setattr(
        supervisor.claude_child,
        "resolve_claude_binary",
        lambda **k: Path("/nonexistent/claude"),
    )
    monkeypatch.setattr(supervisor.claude_child, "inspect_claude_installation", _refuse)


def test_c3_a_run_that_fails_at_admission_releases_its_worktree_claim(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    refuses_the_binary: None,
    fake_picked: picker.PickerResult,
) -> None:
    """The refusal is typed and the row ends FAILED — both already true. What
    was not: the claim went with the phase move, so the cwd is free again."""
    sv = _sv(registry, tmp_path)
    run_id, thread_id = _admit_pending(sv, registry, tmp_path, worktree=str(tmp_path))
    assert registry.read_claim(str(tmp_path)) is not None  # admission took it

    _drive_into_binary_refusal(sv, run_id, thread_id, tmp_path, fake_picked)

    status = sv.status(run_id)
    assert status.phase is RunPhase.FAILED
    assert status.failure_reason is not None
    assert "BinaryIdentityError" in status.failure_reason
    assert registry.read_claim(str(tmp_path)) is None


def test_c3_a_failed_review_never_releases_the_claim_of_another_run(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    refuses_the_binary: None,
    fake_picked: picker.PickerResult,
) -> None:
    """A `review` admission is read-only and claims nothing. Its failure must
    stay its own: the delete is fenced on the RUN, so the writer holding the
    same cwd keeps its lease."""
    sv = _sv(registry, tmp_path)
    writer_id, _ = _admit_pending(sv, registry, tmp_path, worktree=str(tmp_path))
    review_id, review_thread = _admit_pending(
        sv, registry, tmp_path, worktree=str(tmp_path), claim=False
    )

    _drive_into_binary_refusal(sv, review_id, review_thread, tmp_path, fake_picked)

    assert sv.status(review_id).phase is RunPhase.FAILED
    held = registry.read_claim(str(tmp_path))
    assert held is not None
    assert held.run_id == writer_id


_READS_STDIN = """
import json, sys
seen = sys.stdin.read()
print(json.dumps({"type": "assistant"}))
print(json.dumps({"type": "result", "is_error": False, "terminal_reason": "completed",
                  "result": str(len(seen)), "usage": {"input_tokens": 1, "output_tokens": 1}}))
"""


def test_a_prompt_larger_than_one_argv_string_reaches_the_child(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_picked: picker.PickerResult,
) -> None:
    #: Measured 2026-09-23: a 137 669-byte prompt failed with "the run wrote
    #: no assistant text" and 114 769 bytes passed. Linux caps one argv string
    #: at 131 072 bytes, and the prompt rode on argv.
    compose = supervisor.child.compose_child_argv
    spawn = supervisor.child.spawn_child
    _wire_l8_and_l3(monkeypatch, spawn)
    script = tmp_path / "claude.py"
    script.write_text(_READS_STDIN)

    def _real_argv(*args: object, **kwargs: object) -> list[str]:
        argv = compose(*args, **kwargs)  # type: ignore[arg-type]
        return [sys.executable, str(script), *argv[1:]]

    monkeypatch.setattr(supervisor.child, "compose_child_argv", _real_argv)
    sv = _sv(registry, tmp_path)
    from nspawn.owner_roster import Roster

    roster = Roster.read(Roster.default_path())  # type: ignore[arg-type]
    run_id, thread_id = _admit_pending(sv, registry, tmp_path, worktree=str(tmp_path))
    run_dir = supervisor.RunDirectory(
        root=sv._run_root_path / run_id,
        stdout=sv._run_root_path / run_id / "stdout",
        stderr=sv._run_root_path / run_id / "stderr",
    )
    prompt = "keep the exporter RFC 4180 compliant. " * 6_000
    assert len(prompt.encode()) > 131_072
    sv._drive_run(
        run_id,
        str(tmp_path),
        run_dir,
        prompt,
        roster,
        fake_picked,
        thread_id,
        False,
        False,
    )
    assert sv.status(run_id).phase is RunPhase.COMPLETED
    assert sv.result(run_id).final_text == str(len(prompt))


def test_a_cleanup_failure_after_a_refused_completion_keeps_the_completion(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_picked: picker.PickerResult,
) -> None:
    """Review round 3: the real COMPLETED publish was refused and queued; the
    system prompt cleanup then failed and `_fail_run` wrote FAILED over the
    still-RUNNING row, and the retry met an ended row and dropped the
    completion and its tokens. The late failure is only a diagnostic line."""
    from nspawn import terminal
    from nspawn.owner_roster import Roster
    from nspawn.registry import STORE_ERRORS

    sv = _sv(registry, tmp_path)
    roster = Roster.read(Roster.default_path())  # type: ignore[arg-type]
    run_id, thread_id = _admit_pending(sv, registry, tmp_path, worktree=str(tmp_path))
    run_dir = supervisor.RunDirectory(
        root=sv._run_root_path / run_id,
        stdout=sv._run_root_path / run_id / "stdout",
        stderr=sv._run_root_path / run_id / "stderr",
    )
    prompt_path = run_dir.root / "system-prompt"

    def _complete(*args: object, **kwargs: object) -> terminal.TerminalClassification:
        sv._update_phase(run_id, RunPhase.RUNNING)
        return terminal.TerminalClassification(
            phase=RunPhase.COMPLETED,
            final_text="done",
            warning=None,
            marker=None,
            provider_wall=None,
            account_wall=None,
            tokens=5,
        )

    def _refuse_unlink(path: Path, *, missing_ok: bool = False) -> None:
        assert path == prompt_path
        raise PermissionError("cleanup blocked")

    real_update = registry.update_run
    refusals = [STORE_ERRORS[0]("database is locked")]

    def refused_once(row: object, **kwargs: object) -> None:
        if getattr(row, "phase", None) is RunPhase.COMPLETED and refusals:
            raise refusals.pop()
        real_update(row, **kwargs)  # type: ignore[arg-type]

    announced: list[str] = []
    monkeypatch.setattr(sv, "_announce", announced.append)
    monkeypatch.setattr(registry, "update_run", refused_once)
    monkeypatch.setattr(sv, "_spawn_and_wait", _complete)
    monkeypatch.setattr(Path, "unlink", _refuse_unlink)
    sv._drive_run(
        run_id,
        str(tmp_path),
        run_dir,
        "work",
        roster,
        fake_picked,
        thread_id,
        False,
        False,
        system_prompt="DISPATCH SECRET",
    )
    monkeypatch.undo()
    monkeypatch.setattr(sv, "_announce", announced.append)
    monkeypatch.setattr(registry, "reconnect", lambda: None)
    assert sv.publish_pending() is True
    row = registry.get_run(run_id)
    assert row is not None
    assert row.phase is RunPhase.COMPLETED
    assert row.blocked_reason is None
    assert row.tokens == 5
    assert row.final_text == "done"
    assert announced == [run_id]
    assert "RunDirectoryError" not in (row.warning or "")
    late = (run_dir.root / "late_writes").read_text(encoding="utf-8")
    assert "a failed write was not applied; reason='RunDirectoryError'" in late
