"""`Supervisor._spawn_and_wait`, measured by what the child and the store received.

The child is a stand-in `claude`: it answers the version probe, keeps the argv,
cwd, environment, pid and stdin it was handed beside itself, prints the
stream-json it was given and ends as told. Binary resolution, installation and
MCP inspection, the environment and argv builders, the spawn and its pumps, the
classifier and the registry are the real ones, except in the tests that drive
the prompt write and the deadline through a recording child.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
from fixture_data import synthetic_token
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
)

from nspawn import child, claude_child, picker, supervisor, terminal
from nspawn.owner_roster import Defaults, NativeAccount, Provider, Roster
from nspawn.process_identity import ProcessIdentity
from nspawn.redact import REDACTION_MARKER
from nspawn.registry import Registry
from nspawn.rows import RunPhase
from nspawn.supervisor import RunDirectory, Supervisor

# Export imported fixtures so pytest discovers them in this module.
__all__ = [
    "_drain_worker_threads",
    "_stop_every_reaper",
    "fake_picked",
    "fake_provider",
    "registry",
]

_STAND_IN = """\
import json, os, sys
from pathlib import Path

here = Path(sys.argv[0]).parent
if sys.argv[1:] == ["--version"]:
    print("2.1.300 (Claude Code)")
    raise SystemExit(0)
if "--no-session-persistence" in sys.argv:
    request = json.loads(sys.stdin.readline())
    assert request["type"] == "control_request"
    assert request["request"] == {"subtype": "initialize"}
    assert sys.stdin.read() == ""
    models = [{"value": tier, "resolvedModel": "claude-" + tier + "-5-5"}
              for tier in ("haiku", "sonnet", "opus", "fable")]
    print(json.dumps({"type": "control_response", "response": {
        "request_id": request["request_id"], "response": {"models": models}}}))
    raise SystemExit(0)
seen = {
    "argv": sys.argv[1:],
    "cwd": os.getcwd(),
    "env": dict(os.environ),
    "pid": os.getpid(),
    "stdin": sys.stdin.read(),
}
(here / "seen.json").write_text(json.dumps(seen))
sys.stdout.write((here / "replay").read_text())
sys.stdout.flush()
ending = (here / "ending").read_text().split()
if ending[0] == "signal":
    os.kill(os.getpid(), int(ending[1]))
raise SystemExit(int(ending[0]))
"""

_TOKEN = synthetic_token("test_spawn_and_wait:1")
_OTHER_TOKEN = synthetic_token("test_spawn_and_wait:2")
_RESET = 4_102_444_800
_ALLOWED = {
    "type": "rate_limit_event",
    "rate_limit_info": {
        "status": "allowed",
        "unifiedWindows": {"five_hour": {"utilization": 0.25, "resetsAt": _RESET}},
    },
}
_DONE = {
    "type": "result",
    "is_error": False,
    "terminal_reason": "completed",
    "result": f"PONG {_TOKEN}",
    "usage": {"input_tokens": 3, "output_tokens": 4},
}


def _frames(*frames: Mapping[str, object]) -> str:
    return "".join(json.dumps(frame) + "\n" for frame in frames)


def _claude(directory: Path, replay: str, ending: str = "0") -> Path:
    """A stand-in `claude` in its own directory, which also keeps what it saw."""
    directory.mkdir(parents=True)
    binary = directory / "claude"
    binary.write_text(f"#!{sys.executable}\n{_STAND_IN}", encoding="utf-8")
    binary.chmod(0o755)
    (directory / "replay").write_text(replay, encoding="utf-8")
    (directory / "ending").write_text(ending, encoding="utf-8")
    return binary


def _seen(binary: Path) -> dict[str, Any]:
    seen: dict[str, Any] = json.loads((binary.parent / "seen.json").read_text())
    return seen


def _after(argv: list[str], flag: str) -> list[str]:
    """Every value `argv` carries right after `flag`."""
    return [argv[index + 1] for index, argument in enumerate(argv) if argument == flag]


def _plugin(root: Path, nmemory: dict[str, object]) -> Path:
    """A plugin directory whose config declares nmemory beside another server."""
    (root / "skills").mkdir(parents=True)
    servers = {"nmemory": nmemory, "other": {"command": "other"}}
    (root / claude_child.MCP_CONFIG_FILE).write_text(
        json.dumps({"mcpServers": servers}), encoding="utf-8"
    )
    return root


def _acme(
    tmp_path: Path,
    binary: Path | None,
    *,
    args: tuple[str, ...] = ("--from-acme", "$PLUGIN/extra"),
    nmemory: dict[str, object] | None = None,
) -> tuple[Roster, picker.PickerResult, Path]:
    """Provider `acme` on its SECOND account, listed after a decoy provider.

    Everything the child should get differs from what a wrong lookup would give:
    the decoy has its own args and env, the first account its own token, and
    the roster default binary does not exist.
    """
    plugin = _plugin(
        tmp_path / "plugin",
        {"command": "nmemory", "args": ["serve"]} if nmemory is None else nmemory,
    )
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "acct-a.token").write_text(_OTHER_TOKEN + "\n", encoding="utf-8")
    (secrets / "acct-b.token").write_text(_TOKEN + "\n", encoding="utf-8")
    acme = Provider(
        name="acme",
        secrets=secrets,
        bin=binary,
        env={"STAND_IN_PROVIDER": "acme"},
        args=list(args),
        model="opus",
        model_fast="opus-fast",
        models=["opus"],
    )
    decoy = Provider(
        name="decoy",
        env={"STAND_IN_PROVIDER": "decoy"},
        args=["--decoy"],
        model="m",
        models=["m"],
    )
    roster = Roster(
        defaults=Defaults(
            env={"STAND_IN_DECLARED": "$PLUGIN/declared"}, plugin_root=plugin
        ),
        providers=[decoy, acme],
        bins={"claude": tmp_path / "absent" / "claude"},
    )
    picked = picker.PickerResult(
        requested_provider=None,
        executed_provider=acme,
        executed_account="acct-b",
        model="opus",
        effort="high",
    )
    return roster, picked, plugin


@pytest.fixture
def sv(registry: Registry, tmp_path: Path) -> Iterator[Supervisor]:
    instance = _sv(registry, tmp_path)
    try:
        yield instance
    finally:
        instance.shutdown()


@pytest.fixture(autouse=True)
def _no_binary_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """The operator's own override would outrank every binary these tests declare."""
    monkeypatch.delenv(supervisor.CLAUDE_BIN_ENV, raising=False)


def _admitted(
    sv: Supervisor, registry: Registry, tmp_path: Path
) -> tuple[str, str, RunDirectory, Path]:
    cwd = tmp_path / "work"
    cwd.mkdir(exist_ok=True)
    run_id, thread_id = _admit_pending(sv, registry, tmp_path, worktree=str(cwd))
    root = sv._run_root_path / run_id
    run_dir = RunDirectory(root=root, stdout=root / "stdout", stderr=root / "stderr")
    return run_id, thread_id, run_dir, cwd


# --- the real path ------------------------------------------------------------


def test_a_turn_reaches_the_child_and_the_store_as_the_route_declares(
    sv: Supervisor, registry: Registry, tmp_path: Path
) -> None:
    """One writable turn under the default deadline, which is none."""
    binary = _claude(tmp_path / "bin", _frames(_ALLOWED, _DONE))
    roster, picked, plugin = _acme(tmp_path, binary)
    run_id, thread_id, run_dir, cwd = _admitted(sv, registry, tmp_path)

    before = time.time()
    verdict = sv._spawn_and_wait(
        run_id, str(cwd), run_dir, "ping ✓", roster, picked, thread_id, False, False
    )
    after = time.time()

    seen = _seen(binary)
    assert seen["stdin"] == "ping ✓"
    assert Path(seen["cwd"]) == cwd.resolve()
    argv: list[str] = seen["argv"]
    assert f"--session-id={thread_id}" in argv
    assert not [argument for argument in argv if argument.startswith("--resume")]
    assert _after(argv, "--plugin-dir") == [str(plugin)]
    assert _after(argv, "--from-acme") == [f"{plugin}/extra"]
    assert "--decoy" not in argv
    assert "--strict-mcp-config" not in argv
    env: dict[str, str] = seen["env"]
    assert env["ANTHROPIC_MODEL"] == "claude-opus-5-5"
    assert env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "claude-haiku-5-5"
    assert env["CLAUDE_CODE_EFFORT_LEVEL"] == "high"
    assert env[child.CLAUDE_OAUTH_TOKEN_ENV] == _TOKEN
    assert env["STAND_IN_DECLARED"] == f"{plugin}/declared"
    assert env["STAND_IN_PROVIDER"] == "acme"
    assert env[child.RUN_ID_ENV] == run_id

    assert verdict.phase is RunPhase.COMPLETED
    assert verdict.final_text == f"PONG {REDACTION_MARKER}"
    assert verdict.warning is None
    assert verdict.telemetry is not None
    assert verdict.telemetry.failure is None
    assert _TOKEN not in run_dir.stdout.read_text()

    row = registry.get_run(run_id)
    assert row is not None
    assert row.phase is RunPhase.RUNNING
    assert row.process_identity is not None
    assert row.process_identity.pid == seen["pid"]
    assert run_id not in sv._children
    observed = registry.get_run(run_id)
    assert observed is not None
    assert observed.records_seen == 2
    assert observed.last_record_at is not None
    assert int(before * 1000) <= observed.last_record_at <= int(after * 1000)
    (usage,) = registry.get_account_usage()
    assert (usage.account_alias, usage.usage_window) == ("acct-b", "five_hour")
    assert (usage.utilization, usage.resets_at) == (0.25, _RESET)
    assert int(before) <= usage.observed_at <= int(after)
    assert registry.get_provider_wall_rows(0) == ()
    assert registry.get_account_wall_rows(0) == ()


def test_the_child_environment_is_rebased_on_the_supervisor_allowlist(
    sv: Supervisor,
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The builder copies the ambient environment first, so only an allowlist
    that differs from it shows the supervisor handing its own down."""
    home = tmp_path / "allowlisted-home"
    monkeypatch.setattr(
        supervisor, "_allowed_base_environ", lambda: {"HOME": str(home)}
    )
    binary = _claude(tmp_path / "bin", _frames(_DONE))
    roster, picked, _ = _acme(tmp_path, binary)
    run_id, thread_id, run_dir, cwd = _admitted(sv, registry, tmp_path)

    sv._spawn_and_wait(
        run_id, str(cwd), run_dir, "ping", roster, picked, thread_id, False, False
    )

    assert _seen(binary)["env"]["HOME"] == str(home)


@pytest.mark.parametrize(
    ("override", "provider_binary", "ran"),
    [
        pytest.param("override", "provider", "override", id="override-first"),
        pytest.param(None, "provider", "provider", id="provider-before-roster"),
        pytest.param(None, None, "roster", id="roster-default-last"),
    ],
)
def test_the_child_is_the_binary_the_declarations_rank_first(
    sv: Supervisor,
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    override: str | None,
    provider_binary: str | None,
    ran: str,
) -> None:
    binaries = {
        name: _claude(tmp_path / name, _frames(_DONE))
        for name in ("override", "provider", "roster")
    }
    if override is not None:
        monkeypatch.setenv(supervisor.CLAUDE_BIN_ENV, str(binaries[override]))
    roster, picked, _ = _acme(
        tmp_path, None if provider_binary is None else binaries[provider_binary]
    )
    roster.bins = {"claude": binaries["roster"]}
    run_id, thread_id, run_dir, cwd = _admitted(sv, registry, tmp_path)

    sv._spawn_and_wait(
        run_id, str(cwd), run_dir, "ping", roster, picked, thread_id, False, False
    )

    assert [
        name
        for name, binary in binaries.items()
        if (binary.parent / "seen.json").exists()
    ] == [ran]


def test_a_dispatch_prompt_file_carries_the_roster_prompt_after_it(
    sv: Supervisor, registry: Registry, tmp_path: Path
) -> None:
    """The roster's own prompt file, relative so read from the child's cwd, is
    appended to the dispatch's, and the child is handed that one file once."""
    binary = _claude(tmp_path / "bin", _frames(_DONE))
    roster, picked, plugin = _acme(
        tmp_path,
        binary,
        args=(
            "--append-system-prompt-file",
            "global.md",
            "--from-acme",
            "$PLUGIN/extra",
        ),
    )
    run_id, thread_id, run_dir, cwd = _admitted(sv, registry, tmp_path)
    (cwd / "global.md").write_bytes(b"global prompt")
    prompt_file = run_dir.root / "system-prompt"
    prompt_file.write_bytes(b"dispatch prompt")

    sv._spawn_and_wait(
        run_id,
        str(cwd),
        run_dir,
        "ping",
        roster,
        picked,
        thread_id,
        False,
        False,
        system_prompt_file=prompt_file,
    )

    argv: list[str] = _seen(binary)["argv"]
    assert _after(argv, "--append-system-prompt-file") == [str(prompt_file)]
    assert _after(argv, "--from-acme") == [f"{plugin}/extra"]
    assert prompt_file.read_bytes() == b"dispatch prompt\n\nglobal prompt"


# --- the read-only cage ----------------------------------------------------------


@pytest.mark.parametrize(
    ("declared", "caged"),
    [
        pytest.param(
            {"command": "nmemory", "args": ["serve"]},
            {"command": "nmemory", "args": ["serve", "--read-only"]},
            id="appended",
        ),
        pytest.param(
            {"command": "nmemory", "args": ["serve", "--read-only"]},
            {"command": "nmemory", "args": ["serve", "--read-only"]},
            id="kept-once",
        ),
        pytest.param(
            {"command": "nmemory"},
            {"command": "nmemory", "args": ["--read-only"]},
            id="no-args",
        ),
    ],
)
def test_a_caged_resume_runs_on_a_projection_serving_only_nmemory_read_only(
    sv: Supervisor,
    registry: Registry,
    tmp_path: Path,
    declared: dict[str, object],
    caged: dict[str, object],
) -> None:
    binary = _claude(tmp_path / "bin", _frames(_DONE))
    roster, picked, plugin = _acme(tmp_path, binary, nmemory=declared)
    original = (plugin / claude_child.MCP_CONFIG_FILE).read_bytes()
    tree = tmp_path / "tree"
    tree.mkdir()
    run_id, thread_id, run_dir, cwd = _admitted(sv, registry, tmp_path)

    verdict = sv._spawn_and_wait(
        run_id,
        str(cwd),
        run_dir,
        "review",
        roster,
        picked,
        thread_id,
        True,
        True,
        (str(tree),),
    )

    assert verdict.phase is RunPhase.COMPLETED
    projection = run_dir.root / "read_only_plugin"
    argv: list[str] = _seen(binary)["argv"]
    assert _after(argv, "--plugin-dir") == [str(projection)]
    assert str(tree) in _after(argv, "--add-dir")
    assert "--restricted" in argv
    assert f"--resume={thread_id}" in argv
    assert not [argument for argument in argv if argument.startswith("--session-id")]
    assert os.readlink(projection / "skills") == str(plugin / "skills")
    config = projection / claude_child.MCP_CONFIG_FILE
    assert not config.is_symlink()
    assert json.loads(config.read_text()) == {"mcpServers": {"nmemory": caged}}
    assert (plugin / claude_child.MCP_CONFIG_FILE).read_bytes() == original


@pytest.mark.parametrize(
    "rewritten",
    [
        pytest.param({}, id="no-servers"),
        pytest.param({"mcpServers": {"other": {"command": "other"}}}, id="no-nmemory"),
    ],
)
def test_a_config_rewritten_after_inspection_still_cages_nmemory_alone(
    sv: Supervisor,
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rewritten: dict[str, object],
) -> None:
    """The projection reads the plugin's config again after the inspection
    passed it. A file rewritten in between still yields a cage that declares
    nmemory alone, read-only, and none of what the rewrite declares."""
    inspect = claude_child.inspect_child_mcp_config

    def inspect_then_rewrite(
        plugin_dir: Path, **kwargs: tuple[str, ...]
    ) -> claude_child.ChildMcpConfig:
        inspected = inspect(plugin_dir, **kwargs)
        (plugin_dir / claude_child.MCP_CONFIG_FILE).write_text(json.dumps(rewritten))
        return inspected

    monkeypatch.setattr(claude_child, "inspect_child_mcp_config", inspect_then_rewrite)
    binary = _claude(tmp_path / "bin", _frames(_DONE))
    roster, picked, _ = _acme(tmp_path, binary)
    run_id, thread_id, run_dir, cwd = _admitted(sv, registry, tmp_path)

    sv._spawn_and_wait(
        run_id, str(cwd), run_dir, "review", roster, picked, thread_id, False, True
    )

    projected = run_dir.root / "read_only_plugin" / claude_child.MCP_CONFIG_FILE
    assert json.loads(projected.read_text()) == {
        "mcpServers": {"nmemory": {"args": ["--read-only"]}}
    }


def test_a_second_attempt_runs_on_the_projection_the_first_one_left(
    sv: Supervisor, registry: Registry, tmp_path: Path
) -> None:
    """Failover spawns the same run again in the same run directory, once
    `_drive_run` has moved the streams aside. The projection is already there,
    down to a link to a plugin entry that itself points nowhere."""
    binary = _claude(tmp_path / "bin", _frames(_DONE))
    roster, picked, plugin = _acme(tmp_path, binary)
    (plugin / "unbuilt").symlink_to(tmp_path / "never-built")
    run_id, thread_id, run_dir, cwd = _admitted(sv, registry, tmp_path)

    verdicts: list[terminal.TerminalClassification] = []
    for attempt in (1, 2):
        verdicts.append(
            sv._spawn_and_wait(
                run_id,
                str(cwd),
                run_dir,
                "review",
                roster,
                picked,
                thread_id,
                False,
                True,
            )
        )
        sv._archive_attempt_evidence(run_dir, attempt)

    assert [verdict.phase for verdict in verdicts] == [RunPhase.COMPLETED] * 2
    link = run_dir.root / "read_only_plugin" / "unbuilt"
    assert os.readlink(link) == str(plugin / "unbuilt")


# --- how the run ends --------------------------------------------------------------


def test_a_signal_death_after_a_result_is_reported_as_the_signal(
    sv: Supervisor, registry: Registry, tmp_path: Path
) -> None:
    """Killed after it printed a failed result: the classifier alone would name
    the result; the run names the signal, in its warning and its telemetry."""
    failed = {
        "type": "result",
        "is_error": True,
        "terminal_reason": "error",
        "result": "partial",
    }
    binary = _claude(
        tmp_path / "bin", _frames(failed), ending=f"signal {int(signal.SIGKILL)}"
    )
    roster, picked, _ = _acme(tmp_path, binary)
    run_id, thread_id, run_dir, cwd = _admitted(sv, registry, tmp_path)

    verdict = sv._spawn_and_wait(
        run_id, str(cwd), run_dir, "ping", roster, picked, thread_id, False, False
    )

    assert verdict.phase is RunPhase.FAILED
    assert verdict.warning == f"child terminated by signal {int(signal.SIGKILL)}"
    assert verdict.telemetry is not None
    assert verdict.telemetry.failure == "child_signal"


def test_a_402_on_a_route_without_accounts_walls_its_provider_for_the_hour(
    sv: Supervisor, registry: Registry, tmp_path: Path
) -> None:
    spent = {
        "type": "result",
        "is_error": True,
        "terminal_reason": "error",
        "api_error_status": 402,
        "result": "Payment Required",
    }
    binary = _claude(tmp_path / "bin", _frames(spent), ending="1")
    token = tmp_path / "route.token"
    token.write_text(_TOKEN, encoding="utf-8")
    route = Provider(
        name="route", bin=binary, token_file=token, model="m", models=["m"]
    )
    roster = Roster(
        defaults=Defaults(plugin_root=_plugin(tmp_path / "plugin", {"command": "m"})),
        providers=[route],
    )
    picked = picker.PickerResult(
        requested_provider=None,
        executed_provider=route,
        executed_account=None,
        model="m",
        effort=None,
    )
    run_id, thread_id, run_dir, cwd = _admitted(sv, registry, tmp_path)

    before = int(time.time())
    verdict = sv._spawn_and_wait(
        run_id, str(cwd), run_dir, "ping", roster, picked, thread_id, False, False
    )
    after = int(time.time())

    (wall,) = registry.get_provider_wall_rows(0)
    hour = terminal.WallSource.DEFAULT_1H
    assert (wall.provider, wall.source) == ("route", hour.value)
    assert before <= wall.observed_at <= after
    assert wall.cooling_until == wall.observed_at + Supervisor.CREDENTIAL_WALL_S
    assert verdict.provider_wall == terminal.ProviderWallRecord(
        provider="route",
        cooling_until=wall.cooling_until,
        source=hour,
        observed_at=wall.observed_at,
    )
    assert verdict.warning == (
        "provider quota exhausted (402: ProviderExhausted/QuotaLimit); "
        f"provider 'route' walled until {wall.cooling_until}"
    )
    assert verdict.telemetry is not None
    assert verdict.telemetry.failure == hour.value
    assert registry.get_account_wall_rows(0) == ()


def test_a_rejected_rate_limit_walls_the_account_until_the_vendor_reset(
    sv: Supervisor, registry: Registry, tmp_path: Path
) -> None:
    rejected = {
        "type": "rate_limit_event",
        "rate_limit_info": {
            "status": "rejected",
            "rateLimitType": "five_hour",
            "resetsAt": _RESET,
            "unifiedWindows": {"five_hour": {"utilization": 1.0, "resetsAt": _RESET}},
        },
    }
    limited = {
        "type": "result",
        "is_error": True,
        "terminal_reason": "error",
        "result": "limited",
    }
    binary = _claude(tmp_path / "bin", _frames(rejected, limited), ending="1")
    roster, picked, _ = _acme(tmp_path, binary)
    run_id, thread_id, run_dir, cwd = _admitted(sv, registry, tmp_path)

    before = int(time.time())
    sv._spawn_and_wait(
        run_id, str(cwd), run_dir, "ping", roster, picked, thread_id, False, False
    )
    after = int(time.time())

    (wall,) = registry.get_account_wall_rows(0)
    assert (wall.account_alias, wall.cooling_until) == ("acct-b", _RESET)
    assert (wall.source, wall.limit_type) == (
        terminal.WallSource.VENDOR_RESET.value,
        "five_hour",
    )
    assert before <= wall.observed_at <= after
    assert registry.get_provider_wall_rows(0) == ()
    (usage,) = registry.get_account_usage()
    assert (usage.account_alias, usage.utilization) == ("acct-b", 1.0)


# --- refusals before any child ------------------------------------------------------


def test_a_route_that_stays_asleep_is_refused_naming_its_command_and_endpoint(
    sv: Supervisor,
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wake = ("/usr/bin/wake", "--now")
    monkeypatch.setattr(
        supervisor,
        "bring_route_up",
        lambda account, **kwargs: supervisor.BootstrapOutcome(
            ran=True, live=False, argv=wake, returncode=3
        ),
    )
    server = NativeAccount(home=None, base_url="http://127.0.0.1:59999", bootstrap=wake)
    qwen = Provider(name="qwen", model="q", models=["q"], accounts={"server": server})
    roster = Roster(defaults=Defaults(plugin_root=tmp_path), providers=[qwen])
    picked = picker.PickerResult(
        requested_provider=None,
        executed_provider=qwen,
        executed_account="server",
        model="q",
        effort=None,
    )
    run_id, thread_id, run_dir, cwd = _admitted(sv, registry, tmp_path)

    with pytest.raises(supervisor.SupervisorError) as refused:
        sv._spawn_and_wait(
            run_id, str(cwd), run_dir, "ping", roster, picked, thread_id, False, False
        )

    assert str(refused.value) == (
        "account 'server' of provider 'qwen' did not come up: ran /usr/bin/wake "
        "--now (exit 3) and http://127.0.0.1:59999 still does not answer"
    )


def test_a_provider_reaching_no_plugin_directory_is_refused_by_name(
    sv: Supervisor,
    registry: Registry,
    tmp_path: Path,
    fake_provider: Provider,
    fake_picked: picker.PickerResult,
) -> None:
    roster = Roster(providers=[fake_provider])
    run_id, thread_id, run_dir, cwd = _admitted(sv, registry, tmp_path)

    with pytest.raises(supervisor.SupervisorError) as refused:
        sv._spawn_and_wait(
            run_id,
            str(cwd),
            run_dir,
            "ping",
            roster,
            fake_picked,
            thread_id,
            False,
            False,
        )

    assert str(refused.value) == (
        "provider 'acme' reaches no plugin directory: declare [defaults] plugins; "
        "cannot resolve its child MCP config"
    )


def test_a_provider_of_another_kind_is_refused_by_name_and_kind(
    sv: Supervisor, registry: Registry, tmp_path: Path
) -> None:
    codex = Provider(name="gpt", kind="codex", model="g", models=["g"])
    roster = Roster(defaults=Defaults(plugin_root=tmp_path), providers=[codex])
    picked = picker.PickerResult(
        requested_provider=None,
        executed_provider=codex,
        executed_account=None,
        model="g",
        effort=None,
    )
    run_id, thread_id, run_dir, cwd = _admitted(sv, registry, tmp_path)

    with pytest.raises(child.ChildArgvError) as refused:
        sv._spawn_and_wait(
            run_id, str(cwd), run_dir, "ping", roster, picked, thread_id, False, False
        )

    assert refused.value.detail == (
        "the Claude worker cannot execute provider 'gpt' with kind 'codex'"
    )


# --- the prompt write and the deadline, through a recording child ------------------


class _Child(FakeSpawnedChild):
    """A real process in the supervisor's custody, recording how it was driven."""

    def __init__(
        self,
        popen: subprocess.Popen[bytes],
        *,
        stdin_error: Exception | None,
        kill_error: Exception | None,
    ) -> None:
        super().__init__(popen)
        self.writes: list[tuple[bytes, float | None]] = []
        self.waits: list[float | None] = []
        self._stdin_error = stdin_error
        self._kill_error = kill_error

    def write_stdin(self, payload: bytes, timeout: float | None) -> None:
        self.writes.append((payload, timeout))
        if self._stdin_error is not None:
            raise self._stdin_error

    def wait(self, timeout: float | None = None) -> int:
        self.waits.append(timeout)
        return super().wait(timeout)

    def stop_then_kill(self, expected: ProcessIdentity) -> None:
        super().stop_then_kill(expected)
        if self._kill_error is not None:
            raise self._kill_error


class _Spawner:
    """`spawn_child` for one command, keeping every `_Child` it started."""

    def __init__(
        self,
        *command: str,
        stdin_error: Exception | None = None,
        kill_error: Exception | None = None,
    ) -> None:
        self._command = command
        self._stdin_error = stdin_error
        self._kill_error = kill_error
        self.children: list[_Child] = []

    def __call__(self, *args: object, **kwargs: object) -> _Child:
        spawned = _Child(
            _spawn_real(*self._command),
            stdin_error=self._stdin_error,
            kill_error=self._kill_error,
        )
        self.children.append(spawned)
        return spawned


def _recorded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, spawner: _Spawner
) -> Roster:
    _wire_l8_and_l3(monkeypatch, spawner)
    monkeypatch.setattr(terminal, "classify_terminal_run", _classify_by_exit_code)
    return Roster(defaults=Defaults(plugin_root=tmp_path))


_KILL_ERRORS = [
    pytest.param(None, id="killed"),
    pytest.param(child.ChildIdentityError("gone"), id="identity-gone"),
    pytest.param(OSError("denied"), id="signal-denied"),
]


def test_the_prompt_is_written_within_its_deadline_and_the_wait_is_unbounded(
    sv: Supervisor,
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_picked: picker.PickerResult,
) -> None:
    """A child that wrote no stream at all is also no observation."""
    spawner = _Spawner("true")
    roster = _recorded(monkeypatch, tmp_path, spawner)
    run_id, thread_id, run_dir, cwd = _admitted(sv, registry, tmp_path)

    sv._spawn_and_wait(
        run_id,
        str(cwd),
        run_dir,
        "ping ✓",
        roster,
        fake_picked,
        thread_id,
        False,
        False,
    )

    (driven,) = spawner.children
    assert driven.writes == [("ping ✓".encode(), supervisor.PROMPT_WRITE_SECONDS)]
    assert driven.waits == [None]
    observed = registry.get_run(run_id)
    assert (observed.last_record_at, observed.records_seen) == (None, 0)


@pytest.mark.parametrize("kill_error", _KILL_ERRORS)
def test_a_prompt_the_child_cannot_take_kills_the_child(
    sv: Supervisor,
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_picked: picker.PickerResult,
    kill_error: Exception | None,
) -> None:
    """A truncated prompt is the wrong question: the child dies, and the
    refusal is the pipe's even when the kill itself fails."""
    spawner = _Spawner(
        "sleep", "5", stdin_error=child.ChildPipeError("closed"), kill_error=kill_error
    )
    roster = _recorded(monkeypatch, tmp_path, spawner)
    run_id, thread_id, run_dir, cwd = _admitted(sv, registry, tmp_path)

    with pytest.raises(child.ChildPipeError):
        sv._spawn_and_wait(
            run_id,
            str(cwd),
            run_dir,
            "ping",
            roster,
            fake_picked,
            thread_id,
            False,
            False,
        )

    (driven,) = spawner.children
    assert driven.stop_calls == [driven.identity]
    assert driven.process.wait(timeout=5) == -signal.SIGKILL
    assert run_id not in sv._children


@pytest.mark.parametrize("kill_error", _KILL_ERRORS)
@pytest.mark.parametrize(("deadline_ms", "waited"), [(1, 0.001), (250, 0.25)])
def test_a_child_that_outlives_its_deadline_is_signalled_and_named(
    sv: Supervisor,
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_picked: picker.PickerResult,
    deadline_ms: int,
    waited: float,
    kill_error: Exception | None,
) -> None:
    spawner = _Spawner("sleep", "5", kill_error=kill_error)
    roster = _recorded(monkeypatch, tmp_path, spawner)
    run_id, thread_id, run_dir, cwd = _admitted(sv, registry, tmp_path)

    with pytest.raises(supervisor.RunDeadlineExceeded) as refused:
        sv._spawn_and_wait(
            run_id,
            str(cwd),
            run_dir,
            "ping",
            roster,
            fake_picked,
            thread_id,
            False,
            False,
            deadline_ms=deadline_ms,
        )

    assert str(refused.value) == (
        f"run exceeded its caller-named deadline of {deadline_ms} ms and was signalled"
    )
    (driven,) = spawner.children
    assert driven.waits == [waited]
    assert driven.stop_calls == [driven.identity]
    assert driven.process.wait(timeout=5) == -signal.SIGKILL
    assert run_id not in sv._children


def test_a_child_that_meets_its_deadline_is_classified(
    sv: Supervisor,
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_picked: picker.PickerResult,
) -> None:
    spawner = _Spawner("true")
    roster = _recorded(monkeypatch, tmp_path, spawner)
    run_id, thread_id, run_dir, cwd = _admitted(sv, registry, tmp_path)

    verdict = sv._spawn_and_wait(
        run_id,
        str(cwd),
        run_dir,
        "ping",
        roster,
        fake_picked,
        thread_id,
        False,
        False,
        deadline_ms=60_000,
    )

    assert verdict.phase is RunPhase.COMPLETED
    (driven,) = spawner.children
    assert driven.waits == [60.0]
