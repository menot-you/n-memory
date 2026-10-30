"""U2: an nSPAWN child runs with its agent and that agent's skills, never the law."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from class_dispatch import DECLARED_CLASS, system1_never_reaches_the_network
from supervisor_support import (
    FakeSpawnedChild,
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
from test_driver import _call, _payload
from test_native_execute import native_client
from test_native_write import (
    _claude_write,
    _seen_by,
    claude_writer,
    paper_slices,
)

from nspawn import driver, picker, supervisor
from nspawn.child import LAW_SESSION_ENV, LAW_TITLE, READ_ONLY_TOOLS
from nspawn.mcp import McpServer
from nspawn.owner_roster import Provider
from nspawn.registry import Registry
from nspawn.rows import RunPhase

# Export imported fixtures so pytest discovers them in this module.
__all__ = [
    "_drain_worker_threads",
    "_stop_every_reaper",
    "claude_writer",
    "fake_picked",
    "fake_provider",
    "native_client",
    "paper_slices",
    "registry",
    "roster_home",
    "system1_never_reaches_the_network",
]

#: The law as the plugin ships it: a file that begins with its title.
LAW = LAW_TITLE + b" v9\n\nThe law.\n"
WORKER_BODY = "WORKER BODY: do the brief.\n"
EXPLORER_BODY = "EXPLORER BODY.\n"
SKILL_BODY = "EXPLORE PROCEDURE: each fact with its file:line and a confidence.\n"
#: The plugin root of this repository, the one the plugin ships.
REPOSITORY_PLUGIN = Path(__file__).resolve().parents[3] / "plugin"


def _agent_plugin(root: Path) -> Path:
    """A plugin root with `worker`, `explorer` (skills: explore) and the explore skill."""
    (root / "agents").mkdir(parents=True, exist_ok=True)
    (root / "skills" / "explore").mkdir(parents=True, exist_ok=True)
    (root / "agents" / "worker.md").write_text(
        "---\nname: worker\ndescription: The default child.\n---\n\n" + WORKER_BODY,
        encoding="utf-8",
    )
    (root / "agents" / "explorer.md").write_text(
        "---\nname: explorer\ndescription: Finds facts.\ntools: Read, Grep, Glob\n"
        "skills: explore\n---\n\n" + EXPLORER_BODY,
        encoding="utf-8",
    )
    (root / "skills" / "explore" / "SKILL.md").write_text(
        "---\nname: explore\ndescription: How a fact is reported.\n---\n\n"
        + SKILL_BODY,
        encoding="utf-8",
    )
    # The plugin's MCP declarations, which a child's launch reads
    # (claude_child.py:285-297).
    mcp = root / ".mcp.json"
    if not mcp.exists():
        mcp.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "nmemory": {"command": "/fixture/memory"},
                        "nspawn": {"command": "/fixture/spawn"},
                    }
                }
            )
        )
    return root


def _claude_route(
    registry: Registry,
    roster_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_picked: picker.PickerResult,
    *,
    appended: bytes | None = None,
    plugin: Path | None = None,
    routes: str = "",
) -> tuple[supervisor.Supervisor, dict[str, list[Any]]]:
    """An uncaged Claude route whose child is `true`. It records each child's
    argv, prompt-file bytes and env. `routes` adds roster providers."""
    plugin = _agent_plugin(tmp_path / "plugin") if plugin is None else plugin
    roster = f"[defaults]\nplugins = [{json.dumps(str(plugin))}]\n" + routes
    if appended is not None:
        (plugin / "NOTT.md").write_bytes(appended)
        roster += 'args = ["--append-system-prompt-file", "$PLUGIN/NOTT.md"]\n'
    (roster_home / ".nott" / "spawn.toml").write_text(roster, encoding="utf-8")
    seen: dict[str, list[Any]] = {"argv": [], "files": [], "env": []}
    real_compose = supervisor.child.compose_child_argv

    def _compose(*args: Any, **kwargs: Any) -> list[str]:
        prompt_file = kwargs.get("append_system_prompt_file")
        if prompt_file is not None:
            seen["files"].append(Path(prompt_file).read_bytes())
        argv = real_compose(*args, **kwargs)
        seen["argv"].append(argv)
        return argv

    def _spawn(*args: Any, **kwargs: Any) -> FakeSpawnedChild:
        seen["env"].append(dict(args[2]))
        return FakeSpawnedChild(_spawn_real("true"))

    _wire_l8_and_l3(monkeypatch, _spawn)
    monkeypatch.setattr(supervisor.child, "compose_child_argv", _compose)
    monkeypatch.setattr(
        supervisor.terminal, "classify_terminal_run", _classify_by_exit_code
    )
    monkeypatch.setattr(
        supervisor.picker, "pick_execution", lambda *a, **k: fake_picked
    )
    return _sv(registry, tmp_path), seen


def _completed(sv: supervisor.Supervisor, run_id: str, timeout_ms: int = 5000) -> None:
    status = sv.status(run_id, timeout_ms=timeout_ms)
    assert status.phase is RunPhase.COMPLETED, (status.failure_reason, status.warning)


def _recorded_agents(registry: Registry, run_id: str) -> set[str]:
    return {
        event.payload["agent"]
        for event in registry.read_run_events(run_id)
        if event.kind == "lifecycle"
        and isinstance(event.payload, dict)
        and "agent" in event.payload
    }


def _shown(server: McpServer, run_id: str) -> dict[str, Any]:
    shown: dict[str, Any] = _payload(
        _call(server, "nspawn_run_show", {"run_id": run_id})
    )
    return shown


def _prompts_to_the_writer(monkeypatch: pytest.MonkeyPatch) -> list[bytes]:
    """Read each writer prompt before the run unlinks it."""
    prompts: list[bytes] = []
    original = supervisor.child.compose_child_argv

    def _capture(*args: Any, **kwargs: Any) -> Any:
        prompt = kwargs.get("append_system_prompt_file")
        prompts.append(b"" if prompt is None else Path(prompt).read_bytes())
        return original(*args, **kwargs)

    monkeypatch.setattr(supervisor.child, "compose_child_argv", _capture)
    return prompts


def _native_start(cwd: Path, **changes: Any) -> dict[str, Any]:
    return {
        "provider": "first",
        "cwd": str(cwd),
        "prompt": "Reply PONG",
        "class": DECLARED_CLASS,
        "read_only": True,
        "timeout_ms": 5000,
        **changes,
    }


@pytest.mark.parametrize(
    "dispatch",
    [None, "Persona: cite `evidence`.\n"],
    ids=["no-dispatch-prompt", "dispatch-prompt"],
)
def test_a_roster_law_never_reaches_a_claude_child(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_picked: picker.PickerResult,
    dispatch: str | None,
) -> None:
    sv, seen = _claude_route(
        registry, roster_home, tmp_path, monkeypatch, fake_picked, appended=LAW
    )
    accepted = sv.execute("work", str(tmp_path), read_only=True, system_prompt=dispatch)
    _completed(sv, accepted.run_id)

    law = str(tmp_path / "plugin" / "NOTT.md")
    assert seen["argv"], "no Claude child was composed"
    assert all(law not in arg for argv in seen["argv"] for arg in argv)
    assert all(LAW_TITLE not in data for data in seen["files"])
    if dispatch is not None:
        # The caller's text still arrives, so the law check is not vacuous.
        assert any(dispatch.encode() in data for data in seen["files"])


@pytest.mark.parametrize(
    "appended",
    [LAW, b"A persona.\n", None],
    ids=["law-control", "persona", "no-append"],
)
def test_every_claude_child_runs_with_the_law_session_off(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_picked: picker.PickerResult,
    appended: bytes | None,
) -> None:
    sv, seen = _claude_route(
        registry, roster_home, tmp_path, monkeypatch, fake_picked, appended=appended
    )
    accepted = sv.execute("work", str(tmp_path), read_only=True)
    _completed(sv, accepted.run_id)

    assert seen["env"], "no Claude child was spawned"
    assert all(env.get(LAW_SESSION_ENV) == "off" for env in seen["env"])


#: Two Claude-kind routes: the native one, and `gpt`, the Claude client through
#: a local proxy (CLIProxyAPI). The token is a stand-in, never a credential.
_ROUTES = {
    "claude": ("", Provider(name="claude", kind="claude", model="opus")),
    "gpt": (
        (
            '[[provider]]\nname = "gpt"\nkind = "claude"\nmodel = "gpt-6.1-sol"\n'
            "[provider.env]\n"
            'ANTHROPIC_BASE_URL = "http://127.0.0.1:1"\n'
            'ANTHROPIC_AUTH_TOKEN = "stand-in"\n'
        ),
        Provider(
            name="gpt",
            kind="claude",
            model="gpt-6.1-sol",
            env={
                "ANTHROPIC_BASE_URL": "http://127.0.0.1:1",
                "ANTHROPIC_AUTH_TOKEN": "stand-in",
            },
        ),
    ),
}


def _read_only_grant(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    route: str,
    agent: str | None,
) -> tuple[list[str], list[str]]:
    """The built-in tools and the allowed tools of one read-only Claude child
    that `nspawn_run_start` launches on `route` as `agent`."""
    routes, provider = _ROUTES[route]
    picked = picker.PickerResult(
        requested_provider=None,
        executed_provider=provider,
        executed_account=None,
        model=provider.model,
        effort="high",
        warning=None,
    )
    sv, seen = _claude_route(
        registry, roster_home, tmp_path, monkeypatch, picked, routes=routes
    )
    accepted = sv.execute("audit it", str(tmp_path), read_only=True, agent=agent)
    _completed(sv, accepted.run_id)
    (argv,) = seen["argv"]
    allowed = (
        argv[argv.index("--allowedTools") + 1].split(",")
        if "--allowedTools" in argv
        else []
    )
    return argv[argv.index("--tools") + 1].split(","), allowed


@pytest.mark.parametrize("route", sorted(_ROUTES))
def test_a_read_only_agent_without_bash_gets_no_bash_on_any_claude_route(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    route: str,
) -> None:
    """Run 16dc5d14 (2026-10-24): `agent=adversarial`, read-only, on the `gpt`
    route. The agent declares `tools: Read, Grep, Glob`, and the child still had
    Bash under `dontAsk` and ran `python -I -`. The agent's `tools` narrows the
    cage: no Bash, so no interpreter, on a native or a proxy Claude route."""
    tools, allowed = _read_only_grant(
        registry, tmp_path, roster_home, monkeypatch, route, "explorer"
    )

    assert not any(name.startswith("Bash") for name in (*tools, *allowed))
    assert set(tools) == {"Read", "Grep", "Glob"}


@pytest.mark.parametrize("route", sorted(_ROUTES))
def test_an_agent_without_tools_keeps_the_same_cage_on_every_claude_route(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    route: str,
) -> None:
    """The worker file declares no `tools`: the cage is not narrowed, and no
    route gets more than it."""
    tools, _ = _read_only_grant(
        registry, tmp_path, roster_home, monkeypatch, route, None
    )

    assert tools == list(READ_ONLY_TOOLS)


def test_an_agentless_claude_start_composes_and_records_worker(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_picked: picker.PickerResult,
) -> None:
    sv, seen = _claude_route(registry, roster_home, tmp_path, monkeypatch, fake_picked)
    accepted = sv.execute("work", str(tmp_path), read_only=True)
    _completed(sv, accepted.run_id)

    assert any(WORKER_BODY.encode() in data for data in seen["files"])
    assert _recorded_agents(registry, accepted.run_id) == {"worker"}
    assert _shown(McpServer(supervisor=sv), accepted.run_id).get("agent") == "worker"


def test_an_agentless_start_runs_with_the_repository_worker_file(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_picked: picker.PickerResult,
) -> None:
    worker = REPOSITORY_PLUGIN / "agents" / "worker.md"
    assert worker.is_file(), f"the plugin ships no {worker}"
    sv, seen = _claude_route(
        registry,
        roster_home,
        tmp_path,
        monkeypatch,
        fake_picked,
        plugin=REPOSITORY_PLUGIN,
    )
    accepted = sv.execute("work", str(tmp_path), read_only=True)
    _completed(sv, accepted.run_id)

    body = worker.read_text(encoding="utf-8").partition("\n---\n")[2].strip()
    assert body and any(body.encode() in data for data in seen["files"])
    assert _recorded_agents(registry, accepted.run_id) == {"worker"}


@pytest.mark.parametrize("root", ["plugin", "my-plugin"])
def test_a_start_without_the_worker_file_is_refused_with_its_fix(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_picked: picker.PickerResult,
    root: str,
) -> None:
    checkout = tmp_path / "checkout"
    plugin = checkout / root
    (plugin / "agents").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)
    sv, seen = _claude_route(
        registry, roster_home, tmp_path, monkeypatch, fake_picked, plugin=plugin
    )
    answer = _call(
        McpServer(supervisor=sv),
        "nspawn_run_start",
        {
            "prompt": "work",
            "cwd": str(tmp_path),
            "class": DECLARED_CLASS,
            "read_only": True,
        },
    )

    message = answer.get("error", {}).get("message", "")
    assert str(plugin / "agents" / "worker.md") in message, answer
    assert seen["env"] == []
    if root != "plugin":
        # deliver writes only <checkout>/plugin, so no command reaches this root.
        assert "plugin.py deliver" not in message
        assert f"{plugin} is not the plugin/ directory of a checkout" in message
        return
    # deliver takes the top level of the Git checkout (package.py:764-770).
    command = "python3 plugin/scripts/plugin.py deliver <commit> "
    assert command in message, message
    target = message.split(command, 1)[1].split()[0]
    top = subprocess.run(
        ["git", "-C", str(plugin), "rev-parse", "--show-toplevel"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert Path(target).resolve() == Path(top).resolve()


@pytest.mark.parametrize(
    "skills",
    ["skills: explore\n", "skills:\n  - explore\n", "skills:\n- explore\n"],
    ids=["scalar", "indented-list", "unindented-list"],
)
def test_an_agent_skills_list_is_read_in_each_yaml_form(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_picked: picker.PickerResult,
    skills: str,
) -> None:
    plugin = _agent_plugin(tmp_path / "plugin")
    (plugin / "agents" / "explorer.md").write_text(
        "---\nname: explorer\ndescription: Finds facts.\n"
        + skills
        + "tools: Read\n---\n\n"
        + EXPLORER_BODY,
        encoding="utf-8",
    )
    sv, seen = _claude_route(
        registry, roster_home, tmp_path, monkeypatch, fake_picked, plugin=plugin
    )
    accepted = sv.execute("work", str(tmp_path), read_only=True, agent="explorer")
    _completed(sv, accepted.run_id)

    assert any(SKILL_BODY.encode() in data for data in seen["files"])
    shown = _shown(McpServer(supervisor=sv), accepted.run_id)
    assert (shown.get("agent"), shown.get("skills")) == ("explorer", ["explore"])


def test_a_native_claude_writer_gets_no_law_and_its_law_session_is_off(
    registry: Registry,
    tmp_path: Path,
    claude_writer: SimpleNamespace,
    paper_slices: list[object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin = _agent_plugin(tmp_path / "plugin")
    (plugin / "NOTT.md").write_bytes(LAW)
    claude_writer.roster.write_text(
        claude_writer.roster.read_text().replace(
            "[defaults]\n",
            '[defaults]\nargs = ["--append-system-prompt-file", "$PLUGIN/NOTT.md"]\n',
            1,
        )
    )
    prompts = _prompts_to_the_writer(monkeypatch)
    sv = _sv(registry, tmp_path)
    run_id = _payload(
        _call(
            McpServer(supervisor=sv),
            "nspawn_run_start",
            _claude_write(claude_writer, agent="explorer"),
        )
    )["run_id"]
    _completed(sv, run_id, timeout_ms=20_000)

    assert prompts and EXPLORER_BODY.encode() in prompts[-1]
    assert LAW_TITLE not in prompts[-1]
    seen = _seen_by(claude_writer)
    assert all(str(plugin / "NOTT.md") not in arg for arg in seen["argv"])
    assert seen["env"].get(LAW_SESSION_ENV) == "off"


def test_an_explorer_native_writer_composes_its_skill_and_its_worktree(
    registry: Registry,
    tmp_path: Path,
    claude_writer: SimpleNamespace,
    paper_slices: list[object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _agent_plugin(tmp_path / "plugin")
    prompts = _prompts_to_the_writer(monkeypatch)
    sv = _sv(registry, tmp_path)
    server = McpServer(supervisor=sv)
    run_id = _payload(
        _call(
            server, "nspawn_run_start", _claude_write(claude_writer, agent="explorer")
        )
    )["run_id"]
    _completed(sv, run_id, timeout_ms=20_000)

    assert prompts
    text = prompts[-1].decode()
    assert EXPLORER_BODY in text and SKILL_BODY in text
    assert text.index(EXPLORER_BODY) < text.index(SKILL_BODY)
    assert "name: explore" not in text  # both frontmatters are cut
    assert WORKER_BODY not in text  # the named agent replaces worker
    assert text.rstrip("\n").endswith(f"Worktree: {claude_writer.worktree}.")
    shown = _shown(server, run_id)
    assert (shown.get("agent"), shown.get("skills")) == ("explorer", ["explore"])


def test_an_agentless_native_writer_and_its_resume_compose_and_record_worker(
    registry: Registry,
    tmp_path: Path,
    claude_writer: SimpleNamespace,
    paper_slices: list[object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _agent_plugin(tmp_path / "plugin")
    prompts = _prompts_to_the_writer(monkeypatch)
    sv = _sv(registry, tmp_path)
    server = McpServer(supervisor=sv)
    first = _payload(
        _call(
            server,
            "nspawn_run_start",
            {**_claude_write(claude_writer), "prompt": "origin beacon"},
        )
    )["run_id"]
    _completed(sv, first, timeout_ms=20_000)

    assert prompts
    text = prompts[-1].decode()
    assert WORKER_BODY in text
    assert text.rstrip("\n").endswith(f"Worktree: {claude_writer.worktree}.")
    assert _shown(server, first).get("agent") == "worker"

    assert sv.close(first).claim_released is True
    original = registry.get_run(first)
    assert original is not None and original.thread_id is not None
    launched = len(prompts)
    follow_up = _payload(
        _call(
            server,
            "nspawn_run_start",
            {
                **_claude_write(claude_writer),
                "prompt": "continue this conversation",
                "session_id": original.thread_id,
            },
        )
    )["run_id"]
    _completed(sv, follow_up, timeout_ms=20_000)

    assert len(prompts) == launched + 1
    assert WORKER_BODY.encode() in prompts[-1]
    assert _shown(server, follow_up).get("agent") == "worker"


@pytest.mark.parametrize("native_client", ["agy"], indirect=True)
def test_an_agentless_agy_start_is_admitted_and_records_no_agent(
    native_client: tuple[Any, Path],
) -> None:
    client, cwd = native_client
    # Client.call asserts no error: a composed prompt would be refused (native.py:1231-1232).
    accepted = client.call("nspawn_run_start", _native_start(cwd))
    status = client.terminal(accepted["run_id"])
    assert status["phase"] == "completed", status
    # ASSUMPTION: U2 names the field `agent` and gives AGY the string "none"; the
    # show dict passes it through unchanged (`Client.terminal`,
    # test_native_execute.py:392-399; `_payload`, test_driver.py:88-90).
    assert status.get("agent") == "none"


@pytest.mark.parametrize("agent", [None, "explorer"], ids=["agentless", "explorer"])
def test_a_start_keeps_the_fingerprint_it_has_today(
    registry: Registry,
    tmp_path: Path,
    claude_writer: SimpleNamespace,
    paper_slices: list[object],
    agent: str | None,
) -> None:
    """GUARD: the worker default, the skills and the suggestion join after the
    fingerprint; an agent request prints its body, an agentless one no prompt."""
    _agent_plugin(tmp_path / "plugin")
    sv = _sv(registry, tmp_path)
    key = f"guard-{agent or 'agentless'}"
    start = _claude_write(
        claude_writer,
        idempotency_key=key,
        **({} if agent is None else {"agent": agent}),
    )
    run_id = _payload(_call(McpServer(supervisor=sv), "nspawn_run_start", start))[
        "run_id"
    ]
    _completed(sv, run_id, timeout_ms=20_000)

    found = sv.find_key(driver._caller_project({"cwd": start["cwd"]}), key)
    assert found is not None and found.run_id == run_id
    today = {
        "prompt": start["prompt"],
        "cwd": start["cwd"],
        "provider": start["provider"],
        "execution_class": start["class"],
        "native_write": True,
        "system_prompt": None if agent is None else EXPLORER_BODY,
    }
    assert found.fingerprint == driver.request_fingerprint(
        "nspawn_run_start", today, group=None, strict=True
    )
