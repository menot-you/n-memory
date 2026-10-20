"""Native write mode: admission, the writer's profile, its slice custody and key lookup.

A native write run is one Codex app-server turn that may write only inside its
`cwd`. It needs no implementation authority; what fences it is the permission
profile the thread carries and the systemd slice every process of the run lives
in, which is emptied before the worktree claim may go.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import select
import shutil
import socket
import stat
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest
from class_dispatch import (
    DECLARED_CLASS,
    answering,
    system1_never_reaches_the_network,
)
from host_process import process_state
from registry_reset import sql, store
from supervisor_support import (
    _drain_worker_threads,
    _stop_every_reaper,
    _sv,
    registry,
    roster_home,
)
from test_driver import _call, _payload, _wait_for
from test_native_execute import _store_rows, native_client

from nspawn import (
    app_server,
    child,
    claude_restricted,
    driver,
    native,
    picker,
    supervisor,
    system1,
    wire,
)
from nspawn.app_server import AppServerTurn
from nspawn.custody import AggregateScope, ControllerRefusal
from nspawn.mcp import McpServer
from nspawn.owner_roster import NativeAccount, Provider, Roster
from nspawn.process_identity import capture_process_identity
from nspawn.redact import SecretNeedles
from nspawn.registry import POISONED_LEASE_UNTIL, Registry, RegistryError
from nspawn.rows import Backend, NativeSessionContext, RunPhase, RunRow
from nspawn.supervisor import RunCloseRejected, Supervisor
from nspawn.wire import SANDBOX_TOOL_PATH

# Export imported fixtures so pytest discovers them in this module.
__all__ = [
    "_drain_worker_threads",
    "_stop_every_reaper",
    "native_client",
    "registry",
    "roster_home",
    "system1_never_reaches_the_network",
]

#: What codex 0.157.1 reported for a thread started with a native write profile
#: (observed 2026-09-28): the profile, not the legacy writable roots, names what
#: is writable, and the network stays off.
OBSERVED_WRITER_SANDBOX = {
    "type": "workspaceWrite",
    "writableRoots": [],
    "networkAccess": False,
    "excludeTmpdirEnvVar": True,
    "excludeSlashTmp": True,
}

#: A codex app-server double for the supervisor: it records every frame and its
#: own cgroup, echoes the sandbox codex reports, and with the prompt `hold` leaves
#: a `setsid` descendant behind and never finishes its turn. With the file
#: `hold-preparation` in its record, its preparation inspector never answers.
WRITER_SERVER = r"""
import json, os, subprocess, sys, time
from pathlib import Path
record = Path(os.environ['WRITER_RECORD'])
(record / f'cgroup-{os.getpid()}').write_text(Path('/proc/self/cgroup').read_text())
if sys.argv[-3:] == ['mcp', 'list', '--json']:
    print('[]', flush=True)
    raise SystemExit(0)
WRITER = json.loads(os.environ['WRITER_SANDBOX'])
def send(value):
    print(json.dumps(value), flush=True)
for line in sys.stdin:
    with (record / 'frames.ndjson').open('a') as log:
        log.write(line)
    frame = json.loads(line)
    method, params = frame.get('method'), frame.get('params') or {}
    if method == 'initialize':
        send({'id': frame['id'], 'result': {'userAgent': 'writer-fixture'}})
    elif method == 'plugin/installed':
        if (record / 'hold-preparation').exists():
            (record / 'preparing.tmp').write_text(str(os.getpid()))
            os.replace(record / 'preparing.tmp', record / 'preparing.pid')
            time.sleep(60)
        send({'id': frame['id'], 'result': {'marketplaces': [], 'marketplaceLoadErrors': []}})
    elif method == 'thread/start':
        profiles = params.get('config', {}).get('permissions', {})
        profile = profiles.get(params.get('permissions'), {})
        writer = 'write' in profile.get('filesystem', {}).values()
        send({'id': frame['id'], 'result': {'thread': {'id': 'writer-thread'},
              'model': params['model'],
              'sandbox': WRITER if writer else {'type': 'readOnly'},
              'activePermissionProfile': {'id': params['permissions'], 'extends': None}
              if params.get('permissions') else None}})
    elif method == 'mcpServerStatus/list':
        send({'id': frame['id'], 'result': {'data': [], 'nextCursor': None}})
    elif method == 'turn/start':
        send({'id': frame['id'], 'result': {'turn': {'id': 'writer-turn'}}})
        if params['input'][0]['text'] == 'hold':
            escaped = subprocess.Popen(['/usr/bin/sleep', '60'], start_new_session=True)
            (record / 'escaped.tmp').write_text(str(escaped.pid))
            os.replace(record / 'escaped.tmp', record / 'escaped.pid')
            time.sleep(60)
        send({'method': 'item/completed', 'params': {'threadId': 'writer-thread',
              'turnId': 'writer-turn', 'item': {'id': 'answer', 'type': 'agentMessage',
              'phase': 'final_answer', 'text': 'DONE'}}})
        send({'method': 'turn/completed', 'params': {'threadId': 'writer-thread',
              'turn': {'id': 'writer-turn', 'status': 'completed', 'error': None}}})
"""


def _forbidden(*args: object, **kwargs: object) -> Any:
    pytest.fail("a key lookup or a refused dispatch reached a side effect")


@pytest.fixture
def writer_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, roster_home: Path
) -> SimpleNamespace:
    """A codex route whose vendor is `WRITER_SERVER`, with a git checkout to write
    under the one root the roster declares for native writers."""
    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)
    roster = roster_home / ".nott" / "spawn.toml"
    base_roster = roster.read_text()
    _declare_roots(roster, base_roster, [str(tmp_path)])
    record = tmp_path / "record"
    record.mkdir()
    script = tmp_path / "writer_server.py"
    script.write_text(WRITER_SERVER)
    provider = Provider(
        name="fixture", kind="codex", model="fixture", models=["fixture"]
    )
    picked = picker.PickerResult(None, provider, "fixture", "fixture", None, None)
    monkeypatch.setattr(supervisor.picker, "pick_execution", lambda *a, **k: picked)
    monkeypatch.setattr(Roster, "refuse_unservable_spawn", lambda *a: None)
    prepared: list[dict[str, Any]] = []

    def prepare(*args: object, **kwargs: Any) -> native.NativeLaunch:
        prepared.append(kwargs)
        return native.NativeLaunch(
            (sys.executable, "-u", str(script), "app-server", "--listen", "stdio://"),
            {
                "PATH": "/usr/bin:/bin",
                "WRITER_RECORD": str(record),
                "WRITER_SANDBOX": json.dumps(OBSERVED_WRITER_SANDBOX),
            },
            SecretNeedles.none(),
            NativeSessionContext(
                str(tmp_path), read_roots=(os.path.realpath(checkout),)
            ),
            NativeAccount(tmp_path),
        )

    monkeypatch.setattr(native, "prepare_native", prepare)
    return SimpleNamespace(
        checkout=checkout,
        worktree=os.path.realpath(checkout),
        record=record,
        prepared=prepared,
        roster=roster,
        base_roster=base_roster,
    )


def _declare_roots(roster: Path, base: str, roots: list[str] | None) -> None:
    """The roster as `base`, plus a `[native_write]` table unless `roots` is None."""
    table = "" if roots is None else f"\n[native_write]\nroots = {json.dumps(roots)}\n"
    roster.write_text(base + table)


def _roots_table(checkout: Path) -> str:
    """The roster table that declares the directory holding `checkout`."""
    return f"\n[native_write]\nroots = [{json.dumps(str(checkout.parent))}]\n"


def _write(route: SimpleNamespace, **changes: Any) -> dict[str, Any]:
    return {
        "prompt": "change it",
        "cwd": str(route.checkout),
        "class": DECLARED_CLASS,
        "native_write": True,
        **changes,
    }


def _writer_scopes(run_id: str) -> list[dict[str, str]]:
    # BY ITS KEY: an admission records its effort profile under this same kind,
    # so a lifecycle payload is not necessarily a writer scope.
    return [
        payload["writer_scope"]
        for payload in (
            json.loads(row[0])
            for row in _store_rows(
                "SELECT payload FROM run_events WHERE run_id = ? AND "
                "kind = 'lifecycle' ORDER BY seq",
                (run_id,),
            )
        )
        if "writer_scope" in payload
    ]


def _gone(pid: int) -> bool:
    state = process_state(pid)
    return state is None or state.startswith("Z")


# ---------------------------------------------------------------------------
# Admission
# ---------------------------------------------------------------------------


def test_a_writable_dispatch_without_native_write_is_refused_before_the_roster(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The body-accepted implementation mode is retired: a writable dispatch
    names native_write or is refused, before any roster, row or child."""
    monkeypatch.setattr(
        supervisor, "_read_roster", lambda *a, **k: pytest.fail("read the roster")
    )
    sv = _sv(registry, tmp_path)
    try:
        with pytest.raises(supervisor.InvalidRunRequest, match="native_write"):
            sv.execute("implement", str(tmp_path))
    finally:
        sv.shutdown()
    assert [p.name for p in (tmp_path / "runs").iterdir()] == []


def test_native_write_is_admitted_without_implementation_authority(
    registry: Registry,
    tmp_path: Path,
    writer_route: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        Supervisor, "_drive_native_run", lambda self, *args: started.append(args)
    )
    sv = _sv(registry, tmp_path)

    accepted = _payload(
        _call(McpServer(supervisor=sv), "nspawn_run_start", _write(writer_route))
    )

    row = registry.get_run(accepted["run_id"])
    assert row is not None
    assert (row.sandbox, row.backend, row.worktree) == (
        "workspace-write",
        Backend.CODEX,
        writer_route.worktree,
    )
    claim = registry.read_claim(row.worktree)
    assert claim is not None and claim.run_id == row.run_id
    # The writer reads through the same closed roots a reader gets.
    assert [call["read_only"] for call in writer_route.prepared] == [True]
    assert len(started) == 1


@pytest.mark.parametrize(
    "case",
    [
        "read_only",
        "agy_route",
        "missing_cwd",
        "relative_cwd",
        "git_file",
        "git_symlink",
        "outside_the_roots",
        "no_table",
        "no_roots",
        "symlink_out_of_a_root",
        "the_root_itself",
    ],
)
def test_native_write_is_refused_before_any_child(
    registry: Registry,
    tmp_path: Path,
    writer_route: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    arguments = _write(writer_route)
    git = writer_route.checkout / ".git"
    # The only root, when a case declares one that does not hold the checkout:
    # the way the owner's own primary checkout sits outside every clone area.
    areas = tmp_path / "areas"
    areas.mkdir()
    if case == "read_only":
        arguments["read_only"] = True
    elif case == "agy_route":
        agy = Provider(name="fixture", kind="agy", model="fixture", models=["fixture"])
        routed = picker.PickerResult(None, agy, "fixture", "fixture", None, None)
        monkeypatch.setattr(supervisor.picker, "pick_execution", lambda *a, **k: routed)
    elif case == "missing_cwd":
        arguments["cwd"] = str(tmp_path / "absent")
    elif case == "relative_cwd":
        # It names the checkout from here: only its being relative refuses it.
        monkeypatch.chdir(tmp_path)
        arguments["cwd"] = "checkout"
    elif case == "git_file":
        git.rmdir()
        git.write_text("gitdir: /elsewhere/.git/worktrees/checkout\n")
    elif case == "git_symlink":
        git.rmdir()
        elsewhere = tmp_path / "elsewhere.git"
        elsewhere.mkdir()
        git.symlink_to(elsewhere)
    elif case == "outside_the_roots":
        _declare_roots(writer_route.roster, writer_route.base_roster, [str(areas)])
    elif case == "no_table":
        _declare_roots(writer_route.roster, writer_route.base_roster, None)
    elif case == "no_roots":
        _declare_roots(writer_route.roster, writer_route.base_roster, [])
    elif case == "symlink_out_of_a_root":
        _declare_roots(writer_route.roster, writer_route.base_roster, [str(areas)])
        (areas / "clone").symlink_to(writer_route.checkout)
        arguments["cwd"] = str(areas / "clone")
    else:
        # A root holds the clones; it is never one of them.
        _declare_roots(
            writer_route.roster, writer_route.base_roster, [str(writer_route.checkout)]
        )
    monkeypatch.setattr(child, "spawn_child", _forbidden)

    answer = _call(
        McpServer(supervisor=_sv(registry, tmp_path)), "nspawn_run_start", arguments
    )

    assert answer.get("error", {}).get("data", {}).get("type") == "InvalidRunRequest", (
        answer
    )
    if case in (
        "outside_the_roots",
        "no_table",
        "no_roots",
        "symlink_out_of_a_root",
        "the_root_itself",
    ):
        assert "[native_write]" in answer["error"]["message"], answer
    assert _store_rows("SELECT count(*) FROM runs")[0][0] == 0
    assert _store_rows("SELECT count(*) FROM claims")[0][0] == 0
    assert writer_route.prepared == []


def test_native_write_cannot_resume_its_own_recorded_session(
    registry: Registry,
    tmp_path: Path,
    writer_route: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Supervisor, "_drive_native_run", lambda self, *args: None)
    sv = _sv(registry, tmp_path)
    server = McpServer(supervisor=sv)
    first = _payload(_call(server, "nspawn_run_start", _write(writer_route)))["run_id"]
    row = registry.get_run(first)
    assert row is not None
    registry.update_run(replace(row, thread_id="writer-thread", phase=RunPhase.RUNNING))
    assert sv.close(first).claim_released is True
    monkeypatch.setattr(child, "spawn_child", _forbidden)

    # A session this very checkout recorded, so nothing else refuses the resume.
    answer = _call(
        server, "nspawn_run_start", _write(writer_route, session_id="writer-thread")
    )

    assert answer.get("error", {}).get("data", {}).get("type") == "InvalidRunRequest", (
        answer
    )
    assert "native_write cannot resume a session_id" in answer["error"]["message"]
    assert _store_rows("SELECT count(*) FROM runs")[0][0] == 1
    assert _store_rows("SELECT count(*) FROM claims")[0][0] == 0


@pytest.mark.parametrize(
    "planted", [".nott", ".nott/disposable", ".nott/disposable/home"]
)
def test_the_disposable_area_refuses_a_planted_symlink(
    tmp_path: Path, planted: str
) -> None:
    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)
    (checkout / planted).parent.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "owner-config"
    outside.mkdir()
    (checkout / planted).symlink_to(outside)

    with pytest.raises(supervisor.InvalidRunRequest, match="not a directory"):
        supervisor._disposable_environment(os.path.realpath(checkout))

    assert list(outside.iterdir()) == []


# ---------------------------------------------------------------------------
# The writer's execution class
# ---------------------------------------------------------------------------

#: The real picker, taken before any fixture replaces it.
_PICK_EXECUTION = picker.pick_execution


def _classed_codex(tmp_path: Path) -> str:
    """The host's shape: `unclassified` caps strength below the codex route, the
    declared `judgment` does not, and the declared class is the dearer one."""
    home = tmp_path / "codex-account"
    home.mkdir(exist_ok=True)
    return (
        '\n[bins]\ncodex = "/usr/bin/false"\n'
        '\n[[provider]]\nname = "codex"\nkind = "codex"\nmodel = "fixture"\n'
        'models = ["fixture"]\nstrength = 9\nefforts = ["low", "medium", "high"]\n'
        f"accounts = [{json.dumps(str(home))}]\n"
        "\n[classes]\n"
        'unclassified = { effort = "low", ceiling = "medium", max_strength = 7 }\n'
        'mechanical_fanout = { effort = "low", ceiling = "medium", max_strength = 7 }\n'
        'judgment = { effort = "high", ceiling = "high" }\n'
    )


@pytest.fixture
def unsure_classifier(
    writer_route: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> SimpleNamespace:
    """The live rehearsal's System1: it names the declared class at 0.3023, below
    the confidence an observation needs. It also records every class the real
    picker is asked for."""
    asked: list[object] = []
    classes: list[object] = []

    def ask(
        state: object,
        questions: dict[str, object],
        model: object = None,
        timeout: float | None = None,
        scope: str | None = None,
    ) -> dict[str, system1.Answer]:
        asked.append(state)
        ident = next(iter(questions))
        return {
            ident: system1.Answer(
                id=ident,
                type="choice",
                value="judgment",
                probabilities={"judgment": 0.3023},
                confidence=0.3023,
                legend=None,
            )
        }

    def picking(*args: Any, **kwargs: Any) -> picker.PickerResult:
        classes.append(kwargs.get("execution_class"))
        return _PICK_EXECUTION(*args, **kwargs)

    monkeypatch.setattr(system1, "ask", ask)
    monkeypatch.setattr(supervisor.picker, "pick_execution", picking)
    _declare_roots(
        writer_route.roster,
        writer_route.base_roster + _classed_codex(tmp_path),
        [str(tmp_path)],
    )
    return SimpleNamespace(asked=asked, classes=classes)


def test_native_write_runs_as_its_declared_class_and_system1_is_never_asked(
    registry: Registry,
    tmp_path: Path,
    writer_route: SimpleNamespace,
    unsure_classifier: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Supervisor, "_drive_native_run", lambda self, *args: None)

    answer = _call(
        McpServer(supervisor=_sv(registry, tmp_path)),
        "nspawn_run_start",
        _write(writer_route, provider="codex", **{"class": "judgment"}),
    )

    row = registry.get_run(_payload(answer)["run_id"])
    assert row is not None and row.executed_provider == "codex"
    assert unsure_classifier.classes == ["judgment"]
    assert unsure_classifier.asked == []


def test_native_write_stays_under_its_declared_class_strength_cap(
    registry: Registry,
    tmp_path: Path,
    writer_route: SimpleNamespace,
    unsure_classifier: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(child, "spawn_child", _forbidden)

    answer = _call(
        McpServer(supervisor=_sv(registry, tmp_path)),
        "nspawn_run_start",
        _write(writer_route, provider="codex", **{"class": "mechanical_fanout"}),
    )

    message = answer.get("error", {}).get("message", "")
    assert 'class "mechanical_fanout" caps provider strength at 7' in message, answer
    assert unsure_classifier.classes == ["mechanical_fanout"]
    assert unsure_classifier.asked == []
    assert _store_rows("SELECT count(*) FROM runs")[0][0] == 0


def test_the_same_dispatch_without_native_write_still_asks_system1(
    registry: Registry,
    tmp_path: Path,
    writer_route: SimpleNamespace,
    unsure_classifier: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """System1 is still asked. Its unsure answer is no evidence against the
    caller's provider pin, so the declared class stands and the row says why."""
    monkeypatch.setattr(Supervisor, "_drive_native_run", lambda self, *args: None)

    answer = _call(
        McpServer(supervisor=_sv(registry, tmp_path)),
        "nspawn_run_start",
        _write(
            writer_route,
            provider="codex",
            native_write=False,
            read_only=True,
            **{"class": "judgment"},
        ),
    )

    row = registry.get_run(_payload(answer)["run_id"])
    assert row is not None and row.warning is not None, answer
    assert "the classifier was 0.3023 confident" in row.warning
    assert 'it runs as "judgment"' in row.warning
    assert unsure_classifier.classes == ["judgment"]
    assert len(unsure_classifier.asked) == 1


# ---------------------------------------------------------------------------
# The writer's thread through the served process
# ---------------------------------------------------------------------------


@pytest.mark.needs("systemd_user", "cgroup")
@pytest.mark.parametrize("native_client", ["codex"], indirect=True)
def test_native_write_thread_carries_the_profile_catalog_and_tool_environment(
    native_client: tuple[Any, Path],
) -> None:
    client, cwd = native_client
    (cwd / ".git").mkdir()
    plugin = cwd / "writer-plugin"
    plugin.mkdir()
    (plugin / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "nmemory": {
                        "command": "/fixture/memory",
                        "args": ["--project", "pynott"],
                    },
                    "nspawn": {"command": "/fixture/spawn"},
                }
            }
        )
    )
    shared = cwd.parent / f"{cwd.name}-shared"
    shared.mkdir()
    with (cwd / "spawn.toml").open("a") as config:
        config.write("\n[defaults]\nplugins=[" + json.dumps(str(plugin)) + "]\n")
        config.write(_roots_table(cwd))

    accepted = client.call(
        "nspawn_run_start",
        {
            "provider": "first",
            "cwd": str(cwd),
            "prompt": "Reply PONG",
            "class": DECLARED_CLASS,
            "native_write": True,
            "read_paths": [str(shared)],
        },
    )
    status = client.terminal(accepted["run_id"])

    assert status["phase"] == "completed", status
    root = os.path.realpath(cwd)
    params = json.loads((cwd / "thread-request.json").read_text())
    assert "sandbox" not in params
    profile_id = params["permissions"]
    assert profile_id.startswith("nott-nspawn-write-")
    settings = params["config"]
    assert settings["permissions"] == {
        profile_id: {
            "filesystem": {
                ":root": "deny",
                ":minimal": "read",
                os.path.realpath(cwd / "vendor"): "read",
                os.path.realpath(shared): "read",
                root: "write",
                root + "/.git": "read",
            },
            "network": {"enabled": False},
        }
    }
    # Codex refuses to reload a config whose profiles name no default.
    assert settings["default_permissions"] == profile_id
    disposable = Path(root, ".nott", "disposable")
    tools = {
        "PATH": SANDBOX_TOOL_PATH,
        "NOTT_NSPAWN_RUN_ID": accepted["run_id"],
        "HOME": str(disposable / "home"),
        "TMPDIR": str(disposable / "tmp"),
        "XDG_CACHE_HOME": str(disposable / "cache"),
        "UV_CACHE_DIR": str(disposable / "uv-cache"),
        "NOTT_DATABASE_DIR": str(disposable / "database"),
    }
    assert settings["shell_environment_policy.inherit"] == "none"
    assert {
        key.removeprefix("shell_environment_policy.set."): value
        for key, value in settings.items()
        if key.startswith("shell_environment_policy.set.")
    } == tools
    # What the account's own `[shell_environment_policy.set]` adds is dropped.
    assert sorted(settings["shell_environment_policy.include_only"]) == sorted(tools)
    for leaf in ("home", "tmp", "cache", "uv-cache", "database"):
        mode = (disposable / leaf).lstat().st_mode
        assert stat.S_ISDIR(mode) and stat.S_IMODE(mode) == 0o700
    servers = settings["mcp_servers"]
    assert servers["nmemory"]["args"] == ["--project", "pynott", "--read-only"]
    assert servers["nmemory"]["enabled_tools"] == ["nmemory_context_show"]
    assert servers["nspawn"]["enabled"] is False
    assert settings["plugins"]["extra@runtime"] == {
        "mcp_servers": {"runtime_extra": {"enabled": False}}
    }
    assert settings["features.apps"] is False
    assert settings["web_search"] == "disabled"
    assert settings["tools.web_search"] is False
    assert (cwd / "mcp-status-checked").exists()
    (row,) = _store_rows(
        "SELECT sandbox, backend FROM runs WHERE run_id = ?", (accepted["run_id"],)
    )
    assert tuple(row) == ("workspace-write", "codex")


@pytest.mark.needs("systemd_user", "cgroup")
@pytest.mark.parametrize("native_client", ["codex"], indirect=True)
@pytest.mark.parametrize(
    "echo",
    [
        {"networkAccess": True},
        {"writableRoots": ["/"]},
        {"excludeSlashTmp": False},
        {"type": "dangerFullAccess"},
    ],
    ids=["network", "writable-roots", "slash-tmp", "type"],
)
def test_native_write_refuses_a_changed_sandbox_before_the_model(
    native_client: tuple[Any, Path], echo: dict[str, Any]
) -> None:
    client, cwd = native_client
    (cwd / ".git").mkdir()
    (cwd / "writer-sandbox.json").write_text(
        json.dumps({**OBSERVED_WRITER_SANDBOX, **echo})
    )
    with (cwd / "spawn.toml").open("a") as config:
        config.write(_roots_table(cwd))
    run = client.call(
        "nspawn_run_start",
        {
            "provider": "first",
            "cwd": str(cwd),
            "prompt": "Reply PONG",
            "class": DECLARED_CLASS,
            "native_write": True,
        },
    )
    assert client.terminal(run["run_id"])["phase"] == "failed"
    assert not (cwd / "model-turn-started").exists()


# ---------------------------------------------------------------------------
# Custody: the writer's slice
# ---------------------------------------------------------------------------


@pytest.mark.needs("systemd_user", "cgroup")
def test_writer_scope_is_recorded_before_any_child_and_holds_every_child(
    registry: Registry,
    tmp_path: Path,
    writer_route: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawned: list[tuple[str | None, list[dict[str, str]]]] = []
    original = child.spawn_child

    def recording(*args: Any, scope: Any = None, **kwargs: Any) -> Any:
        recorded = [
            payload["writer_scope"]
            for payload in (
                json.loads(row[0])
                for row in _store_rows(
                    "SELECT payload FROM run_events WHERE kind = 'lifecycle'"
                )
            )
            if "writer_scope" in payload
        ]
        spawned.append((getattr(scope, "unit", None), recorded))
        return original(*args, scope=scope, **kwargs)

    monkeypatch.setattr(child, "spawn_child", recording)
    sv = _sv(registry, tmp_path)

    run_id = _payload(
        _call(McpServer(supervisor=sv), "nspawn_run_start", _write(writer_route))
    )["run_id"]

    assert sv.status(run_id, timeout_ms=20_000).phase is RunPhase.COMPLETED
    (scope,) = _writer_scopes(run_id)
    # The CLI catalog, the plugin inspector and the turn: each launched into
    # the slice, and each only after the slice was on record.
    assert spawned == [(scope["unit"], [scope])] * 3
    group = "0::/" + str(Path(scope["cgroup"]).relative_to("/sys/fs/cgroup")) + "/"
    cgroups = [path.read_text() for path in writer_route.record.glob("cgroup-*")]
    assert len(cgroups) == 3 and all(text.startswith(group) for text in cgroups)
    assert not Path(scope["cgroup"]).exists()
    claim = registry.read_claim(writer_route.worktree)
    assert claim is not None and claim.run_id == run_id
    assert sv.close(run_id).claim_released is True


@pytest.mark.needs("systemd_user", "cgroup")
def test_close_of_a_live_writer_empties_its_slice_before_releasing_the_claim(
    registry: Registry,
    tmp_path: Path,
    writer_route: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[tuple[bool, list[str]]] = []
    drain = AggregateScope.drain

    def observing(self: AggregateScope) -> None:
        drain(self)
        assert self.path is not None
        events = (self.path / "cgroup.events").read_text()
        claims = [str(row[0]) for row in _store_rows("SELECT run_id FROM claims")]
        observed.append(("populated 0" in events, claims))

    monkeypatch.setattr(AggregateScope, "drain", observing)
    sv = _sv(registry, tmp_path)
    server = McpServer(supervisor=sv)
    run_id = _payload(
        _call(server, "nspawn_run_start", _write(writer_route, prompt="hold"))
    )["run_id"]
    try:
        receipt = writer_route.record / "escaped.pid"
        _wait_for(receipt.exists, 20, "the writer's setsid descendant")
        escaped = int(receipt.read_text())
        (scope,) = _writer_scopes(run_id)

        closed = _payload(_call(server, "nspawn_run_close", {"run_id": run_id}))

        assert (closed["phase"], closed["claim_released"]) == ("closed", True)
        assert observed == [(True, [run_id])]
        assert not Path(scope["cgroup"]).exists()
        _wait_for(lambda: _gone(escaped), 5, "the escaped descendant to die")
        # The driver's worker still reaches its own end, and CLOSED stands.
        _wait_for(lambda: run_id not in sv._writers, 10, "the worker to finish")
        row = registry.get_run(run_id)
        assert row is not None and row.phase is RunPhase.CLOSED
    finally:
        # A failure above must not leave the held writer running in its slice.
        if (writer := sv._writers.get(run_id)) is not None:
            with contextlib.suppress(ControllerRefusal):
                writer.close()


@pytest.mark.needs("systemd_user", "cgroup")
def test_close_during_preparation_closes_the_run_its_inspector_held(
    registry: Registry,
    tmp_path: Path,
    writer_route: SimpleNamespace,
) -> None:
    """The run is still `spawning`: its preparation inspector is the live child,
    and the row names no process yet. The close drains the slice, the driver
    fails its preparation, and the close still lands on the first call."""
    (writer_route.record / "hold-preparation").touch()
    sv = _sv(registry, tmp_path)
    server = McpServer(supervisor=sv)
    run_id = _payload(_call(server, "nspawn_run_start", _write(writer_route)))["run_id"]
    try:
        receipt = writer_route.record / "preparing.pid"
        _wait_for(receipt.exists, 20, "the preparation inspector")
        row = registry.get_run(run_id)
        assert row is not None and row.phase is RunPhase.SPAWNING
        assert row.process_identity is None
        (scope,) = _writer_scopes(run_id)

        closed = _payload(_call(server, "nspawn_run_close", {"run_id": run_id}))

        assert (closed["phase"], closed["claim_released"]) == ("closed", True)
        assert registry.read_claim(writer_route.worktree) is None
        assert not Path(scope["cgroup"]).exists()
        _wait_for(lambda: _gone(int(receipt.read_text())), 5, "the inspector to die")
        _wait_for(lambda: run_id not in sv._writers, 10, "the worker to finish")
        row = registry.get_run(run_id)
        assert row is not None and row.phase is RunPhase.CLOSED
    finally:
        if (writer := sv._writers.get(run_id)) is not None:
            with contextlib.suppress(ControllerRefusal):
                writer.close()


#: Every pid receipt is published by `os.replace`: a waiter that sees the
#: file exists must never read it half written.
LEADER = r"""
import os, subprocess, sys, time
from pathlib import Path
escaped = subprocess.Popen(['/usr/bin/sleep', '60'], start_new_session=True)
Path(sys.argv[1] + '.tmp').write_text(str(escaped.pid))
os.replace(sys.argv[1] + '.tmp', sys.argv[1])
time.sleep(60)
"""


@contextlib.contextmanager
def _writer_after_driver_death(
    tmp_path: Path, *, swept: bool, cgroup: str | None = None
) -> Iterator[tuple[str, str, AggregateScope, int]]:
    """A native write run whose driver died with the writer still running.

    The row, its claim and its recorded slice are the dead driver's; inside the
    slice run the writer and a `setsid` descendant of it. With `swept`, a peer's
    orphan pass has already poisoned the claim, as it does within a minute.
    """
    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)
    worktree = os.path.realpath(checkout)
    dead = Registry(store(), "i-dead-driver").connect()
    scope = AggregateScope(memory_bytes=256 * 1024**2, tasks=64)
    try:
        dead.open(now=1000)  # its heartbeat stopped long ago
        now = int(time.time() * 1000)
        row = RunRow(
            run_id="writer-after-driver-death",
            worktree=worktree,
            phase=RunPhase.PENDING,
            sandbox="workspace-write",
            model="fixture",
            backend=Backend.CODEX,
            owner_instance=dead.instance_id,
            created_at=now,
            updated_at=now,
            executed_provider="fixture",
            executed_account="fixture",
        )
        dead.admit_run(
            row,
            True,
            now,
            Supervisor.RUN_CLAIM_LEASE_MS,
            native_context=NativeSessionContext(str(tmp_path), read_roots=(worktree,)),
        )
        dead.append_run_event(
            row.run_id,
            at=now,
            kind="lifecycle",
            payload={
                "writer_scope": {
                    "unit": scope.unit,
                    "cgroup": cgroup or str(scope.path),
                }
            },
        )
        receipt = tmp_path / "escaped.pid"
        leader = subprocess.Popen(
            scope.command([sys.executable, "-c", LEADER, str(receipt)]),
            env=scope.environment({"PATH": "/usr/bin:/bin"}),
            start_new_session=True,
        )
        # The dead driver's children are reaped by whoever inherits them.
        threading.Thread(target=leader.wait, daemon=True).start()
        _wait_for(receipt.exists, 10, "the writer's setsid descendant")
        dead.update_run(
            replace(
                row,
                phase=RunPhase.RUNNING,
                process_identity=capture_process_identity(leader.pid),
            )
        )
        if swept:
            with Registry(store(), "i-peer").connect() as peer:
                peer.open(now=now)
                assert peer.mark_orphans(now, Supervisor.STALE_AFTER_MS) == 1
            claim = dead.read_claim(worktree)
            assert claim is not None and claim.lease_until == POISONED_LEASE_UNTIL
        yield row.run_id, worktree, scope, int(receipt.read_text())
    finally:
        try:
            if scope.path is not None and scope.path.exists():
                scope.close()
        finally:
            dead.close()


@pytest.mark.needs("systemd_user", "cgroup")
@pytest.mark.parametrize(
    ("swept", "stopped"),
    [(False, False), (True, False), (True, True)],
    ids=["unswept", "swept", "swept-and-already-stopped"],
)
def test_close_after_driver_death_drains_the_recorded_slice_before_the_claim(
    registry: Registry, tmp_path: Path, swept: bool, stopped: bool
) -> None:
    with _writer_after_driver_death(tmp_path, swept=swept) as (
        run_id,
        worktree,
        scope,
        escaped,
    ):
        if stopped:
            # The dying driver got as far as emptying and stopping its slice.
            scope.close()
        readback = _sv(registry, tmp_path).close(run_id)

        assert (readback.phase, readback.claim_released) == (RunPhase.CLOSED, True)
        assert registry.read_claim(worktree) is None
        assert scope.path is not None and not scope.path.exists()
        _wait_for(lambda: _gone(escaped), 5, "the escaped descendant to die")


@pytest.mark.needs("systemd_user", "cgroup")
def test_close_refuses_and_keeps_the_claim_when_the_slice_cannot_be_emptied(
    registry: Registry, tmp_path: Path
) -> None:
    with _writer_after_driver_death(tmp_path, swept=False) as (
        run_id,
        worktree,
        scope,
        escaped,
    ):
        assert scope.path is not None
        kill = scope.path / "cgroup.kill"
        mode = stat.S_IMODE(kill.stat().st_mode)
        kill.chmod(0)
        try:
            with pytest.raises(RunCloseRejected, match="was not observed empty"):
                _sv(registry, tmp_path).close(run_id)
        finally:
            kill.chmod(mode)

        claim = registry.read_claim(worktree)
        assert claim is not None and claim.run_id == run_id
        assert not _gone(escaped)
        row = registry.get_run(run_id)
        assert row is not None and row.phase is not RunPhase.CLOSED


@pytest.mark.needs("systemd_user", "cgroup")
def test_close_refuses_a_recorded_slice_whose_cgroup_is_not_its_unit(
    registry: Registry, tmp_path: Path
) -> None:
    with _writer_after_driver_death(
        tmp_path, swept=False, cgroup="/sys/fs/cgroup/not-the-writer.slice"
    ) as (run_id, worktree, _scope, escaped):
        with pytest.raises(RunCloseRejected, match="is not the slice"):
            _sv(registry, tmp_path).close(run_id)

        claim = registry.read_claim(worktree)
        assert claim is not None and claim.run_id == run_id
        assert not _gone(escaped)


@pytest.mark.parametrize(
    "payloads",
    [
        [{"writer_scope": {"unit": "writer.slice"}}],
        [{"writer_scope": "writer.slice"}],
        [{"writer_scope": {"unit": "writer.slice", "cgroup": "/sys/fs/cgroup/w"}}] * 2,
    ],
    ids=["no-cgroup", "not-a-table", "twice"],
)
def test_a_malformed_writer_scope_record_is_an_error_never_absent(
    registry: Registry, tmp_path: Path, payloads: list[dict[str, Any]]
) -> None:
    now = int(time.time() * 1000)
    row = RunRow(
        run_id="writer-malformed-scope",
        worktree=str(tmp_path),
        phase=RunPhase.PENDING,
        sandbox="workspace-write",
        model="fixture",
        backend=Backend.CODEX,
        owner_instance=registry.instance_id,
        created_at=now,
        updated_at=now,
        executed_provider="fixture",
        executed_account="fixture",
    )
    registry.admit_run(
        row,
        True,
        now,
        Supervisor.RUN_CLAIM_LEASE_MS,
        native_context=NativeSessionContext(str(tmp_path), read_roots=(str(tmp_path),)),
    )
    for payload in payloads:
        registry.append_run_event(row.run_id, at=now, kind="lifecycle", payload=payload)

    # Absent would let a close release the claim without draining anything.
    with pytest.raises(RegistryError, match="malformed writer scope"):
        registry.writer_scope(row.run_id)


def test_a_writer_turn_without_its_own_slice_is_refused_before_any_frame(
    tmp_path: Path,
) -> None:
    checkout = Path(os.path.realpath(tmp_path)) / "checkout"
    (checkout / ".git").mkdir(parents=True)
    record = tmp_path / "record"
    record.mkdir()
    script = tmp_path / "writer_server.py"
    script.write_text(WRITER_SERVER)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    with (
        AppServerTurn.start(
            [sys.executable, "-u", str(script), "app-server"],
            cwd=checkout,
            env={
                "PATH": "/usr/bin:/bin",
                "WRITER_RECORD": str(record),
                "WRITER_SANDBOX": json.dumps(OBSERVED_WRITER_SANDBOX),
            },
            run_dir=run_dir,
            needles=SecretNeedles.none(),
        ) as session,
        pytest.raises(app_server.AppServerError, match="aggregate custody"),
    ):
        session.run(
            "ping",
            model="fixture",
            read_only=False,
            timeout=10,
            readonly_mcp=True,
            read_roots=(str(checkout),),
            native_write=True,
            tool_environment={"HOME": str(checkout / ".nott/disposable/home")},
        )
    assert not (record / "frames.ndjson").exists()


@pytest.mark.needs("systemd_user", "cgroup")
@pytest.mark.parametrize("stage", ["after_its_turn", "before_any_turn"])
def test_a_writer_whose_slice_is_not_emptied_fails_and_keeps_a_poisoned_claim(
    registry: Registry,
    tmp_path: Path,
    writer_route: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    close = AggregateScope.close
    refusing = [True]

    def closing(self: AggregateScope) -> None:
        if refusing[0]:
            raise ControllerRefusal("simulated: descendants survived cleanup")
        close(self)

    def unreadable(*args: object, **kwargs: object) -> Any:
        raise app_server.AppServerError("simulated: the catalog could not be read")

    monkeypatch.setattr(AggregateScope, "close", closing)
    if stage == "before_any_turn":
        # The slice is on record, no turn ever ran: the row has no process.
        monkeypatch.setattr(Supervisor, "_native_readonly_mcp", unreadable)
    sv = _sv(registry, tmp_path)
    run_id = _payload(
        _call(McpServer(supervisor=sv), "nspawn_run_start", _write(writer_route))
    )["run_id"]
    try:
        status = sv.status(run_id, timeout_ms=20_000)

        assert status.phase is RunPhase.FAILED
        assert (status.failure_reason or "").startswith("RunCloseRejected:"), status
        assert "was not observed empty" in (status.failure_reason or "")
        claim = registry.read_claim(writer_route.worktree)
        assert claim is not None and claim.run_id == run_id
        assert claim.lease_until == POISONED_LEASE_UNTIL

        # Once the slice can be emptied, a close drains it and the poison goes;
        # from another process, as every close of a driven run comes.
        refusing[0] = False
        with Registry(store(), "i-peer").connect() as peer:
            peer.open(now=int(time.time() * 1000))
            readback = _sv(peer, tmp_path).close(run_id)

        assert (readback.phase, readback.claim_released) == (RunPhase.CLOSED, True)
        assert registry.read_claim(writer_route.worktree) is None
        (scope,) = _writer_scopes(run_id)
        assert not Path(scope["cgroup"]).exists()
    finally:
        refusing[0] = False
        for recorded in _writer_scopes(run_id):
            if Path(recorded["cgroup"]).exists():
                with contextlib.suppress(ControllerRefusal):
                    AggregateScope.recorded(recorded["unit"]).close()


@pytest.mark.needs("systemd_user", "cgroup")
@pytest.mark.parametrize("driver", ["dead", "alive"])
def test_close_before_the_turn_started_drains_the_slice_and_releases_the_claim(
    registry: Registry, tmp_path: Path, driver: str
) -> None:
    """The slice is on record and no turn identity yet: the row carries no
    process, and only the slice says what may still run. A close always comes
    from another process than the driver's, which may be dead or still alive."""
    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)
    worktree = os.path.realpath(checkout)
    owner = Registry(store(), f"i-{driver}-driver").connect()
    scope = AggregateScope(memory_bytes=256 * 1024**2, tasks=64)
    try:
        now = int(time.time() * 1000)
        owner.open(now=1000 if driver == "dead" else now)
        row = RunRow(
            run_id="writer-before-its-turn",
            worktree=worktree,
            phase=RunPhase.PENDING,
            sandbox="workspace-write",
            model="fixture",
            backend=Backend.CODEX,
            owner_instance=owner.instance_id,
            created_at=now,
            updated_at=now,
            executed_provider="fixture",
            executed_account="fixture",
        )
        owner.admit_run(
            row,
            True,
            now,
            Supervisor.RUN_CLAIM_LEASE_MS,
            native_context=NativeSessionContext(str(tmp_path), read_roots=(worktree,)),
        )
        owner.append_run_event(
            row.run_id,
            at=now,
            kind="lifecycle",
            payload={"writer_scope": {"unit": scope.unit, "cgroup": str(scope.path)}},
        )
        receipt = tmp_path / "escaped.pid"
        leader = subprocess.Popen(
            scope.command([sys.executable, "-c", LEADER, str(receipt)]),
            env=scope.environment({"PATH": "/usr/bin:/bin"}),
            start_new_session=True,
        )
        threading.Thread(target=leader.wait, daemon=True).start()
        _wait_for(receipt.exists, 10, "the writer's setsid descendant")
        escaped = int(receipt.read_text())

        readback = _sv(registry, tmp_path).close(row.run_id)

        assert (readback.phase, readback.claim_released) == (RunPhase.CLOSED, True)
        assert registry.read_claim(worktree) is None
        assert scope.path is not None and not scope.path.exists()
        _wait_for(lambda: _gone(escaped), 5, "the escaped descendant to die")
    finally:
        try:
            if scope.path is not None and scope.path.exists():
                scope.close()
        finally:
            owner.close()


# ---------------------------------------------------------------------------
# nspawn_run_show by idempotency key
# ---------------------------------------------------------------------------


def _read_only(route: SimpleNamespace) -> dict[str, Any]:
    return {
        "prompt": "read it",
        "cwd": str(route.checkout),
        "class": DECLARED_CLASS,
        "read_only": True,
        "group": "readers",
    }


def test_key_lookup_answers_found_missing_and_conflict_without_side_effects(
    registry: Registry,
    tmp_path: Path,
    writer_route: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Supervisor, "_drive_native_run", lambda self, *args: None)
    server = McpServer(supervisor=_sv(registry, tmp_path))
    arguments = _read_only(writer_route)
    run_id = _payload(
        _call(server, "nspawn_run_start", {**arguments, "idempotency_key": "lookup"})
    )["run_id"]
    tables = ("runs", "run_keys", "run_events", "notices", "claims")
    before = {t: _store_rows(f"SELECT count(*) FROM {t}")[0][0] for t in tables}
    monkeypatch.setattr(Supervisor, "_admit", _forbidden)
    monkeypatch.setattr(child, "spawn_child", _forbidden)
    server.launcher = SimpleNamespace(dispatch=_forbidden, threads=[])  # type: ignore[assignment]

    def show(key: str, **changes: Any) -> Any:
        request = {"tool": "nspawn_run_start", "arguments": {**arguments, **changes}}
        return _payload(
            _call(
                server, "nspawn_run_show", {"idempotency_key": key, "request": request}
            )
        )

    found = show("lookup")
    assert (found["lookup"], found["run_id"]) == ("found", run_id)
    assert found["group"] == "readers"
    assert show("elsewhere") == {"lookup": "missing", "idempotency_key": "elsewhere"}
    assert show("lookup", prompt="read that") == {
        "lookup": "conflict",
        "run_id": run_id,
    }
    assert show("lookup", group="writers") == {"lookup": "conflict", "run_id": run_id}
    assert {t: _store_rows(f"SELECT count(*) FROM {t}")[0][0] for t in tables} == (
        before
    )


_REQUEST = {
    "tool": "nspawn_run_start",
    "arguments": {"prompt": "read it", "cwd": "/x", "class": DECLARED_CLASS},
}


@pytest.mark.parametrize(
    "arguments",
    [
        {"idempotency_key": "k", "request": _REQUEST, "timeout_ms": 10},
        {"idempotency_key": "k", "request": _REQUEST, "timeout_ms": 0},
        {"idempotency_key": "k"},
        {"idempotency_key": "k", "request": {"tool": "nspawn_run_start"}},
        {"idempotency_key": "k", "request": {**_REQUEST, "extra": 1}},
        {"idempotency_key": "k", "request": {**_REQUEST, "tool": "nspawn_batch_start"}},
        {
            "idempotency_key": "k",
            "request": {
                **_REQUEST,
                "arguments": {**_REQUEST["arguments"], "idempotency_key": "k"},
            },
        },
        {
            "idempotency_key": "k",
            "request": {
                **_REQUEST,
                "arguments": {"cwd": "/x", "class": DECLARED_CLASS},
            },
        },
        {"idempotency_key": "k/0", "request": _REQUEST},
        {"idempotency_key": "k", "request": "nspawn_run_start"},
        {"run_id": "r", "request": _REQUEST},
        {"run_id": "r", "idempotency_key": "k", "request": _REQUEST},
    ],
    ids=[
        "timeout",
        "zero-timeout",
        "no-request",
        "no-arguments",
        "extra-field",
        "other-tool",
        "nested-key",
        "invalid-arguments",
        "slashed-key",
        "not-an-object",
        "request-without-key",
        "two-selectors",
    ],
)
def test_key_lookup_refuses_a_malformed_selector(
    registry: Registry, tmp_path: Path, arguments: dict[str, Any]
) -> None:
    answer = _call(
        McpServer(supervisor=_sv(registry, tmp_path)), "nspawn_run_show", arguments
    )
    assert answer.get("error", {}).get("code") == -32602, answer


def test_key_lookup_on_an_unreadable_store_is_an_error_never_missing(
    registry: Registry, tmp_path: Path, writer_route: SimpleNamespace
) -> None:
    server = McpServer(supervisor=_sv(registry, tmp_path))
    sql(registry, "DROP TABLE run_keys")
    answer = _call(
        server,
        "nspawn_run_show",
        {
            "idempotency_key": "lookup",
            "request": {
                "tool": "nspawn_run_start",
                "arguments": _read_only(writer_route),
            },
        },
    )
    assert answer.get("error", {}).get("code") == -32603, answer
    assert "could not be read" in answer["error"]["message"]


@pytest.mark.parametrize("guard", [None, False])
def test_a_key_released_by_close_admits_no_later_dispatch(
    registry: Registry,
    tmp_path: Path,
    writer_route: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    guard: bool | None,
) -> None:
    """`nspawn_run_close` by key releases a claim. Under the key lock, a key that names
    this request's run has that run closed, one that names another request's is left
    alone, and one that names none is FENCED: a driver of it still on its way when
    the claim was released is refused at admission, so no run ever follows."""
    monkeypatch.setattr(Supervisor, "_drive_native_run", lambda self, *args: None)
    server = McpServer(supervisor=_sv(registry, tmp_path))
    arguments = _read_only(writer_route)

    def release(key: str, **changes: Any) -> dict[str, Any]:
        request = {"tool": "nspawn_run_start", "arguments": {**arguments, **changes}}
        return _call(
            server,
            "nspawn_run_close",
            {
                "idempotency_key": key,
                "request": request,
                **({"only_unadmitted": guard} if guard is not None else {}),
            },
        )

    run_id = _payload(
        _call(server, "nspawn_run_start", {**arguments, "idempotency_key": "kept"})
    )["run_id"]
    assert _payload(release("kept", prompt="read that")) == {
        "lookup": "conflict",
        "run_id": run_id,
    }
    closed = _payload(release("kept"))
    assert (closed["lookup"], closed["run_id"], closed["phase"]) == (
        "found",
        run_id,
        "closed",
    )
    assert _payload(release("released")) == {
        "lookup": "fenced",
        "idempotency_key": "released",
    }
    late = _call(
        server, "nspawn_run_start", {**arguments, "idempotency_key": "released"}
    )
    assert late.get("error", {}).get("data", {}).get("type") == "KeyFenced", late
    assert _store_rows("SELECT count(*) FROM runs")[0][0] == 1


# ---------------------------------------------------------------------------
# Fingerprint
# ---------------------------------------------------------------------------


def test_missing_only_key_close_preserves_an_admitted_writer_and_its_claim(
    registry: Registry,
    tmp_path: Path,
    writer_route: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Supervisor, "_drive_native_run", lambda self, *args: None)
    sv = _sv(registry, tmp_path)
    server = McpServer(supervisor=sv)
    arguments = _write(writer_route)
    run_id = _payload(
        _call(server, "nspawn_run_start", {**arguments, "idempotency_key": "kept"})
    )["run_id"]
    before = registry.get_run(run_id)
    claim = registry.read_claim(writer_route.worktree)
    assert claim is not None and claim.run_id == run_id
    monkeypatch.setattr(sv, "close", _forbidden)
    request = {"tool": "nspawn_run_start", "arguments": arguments}
    guarded = _payload(
        _call(
            server,
            "nspawn_run_close",
            {"idempotency_key": "kept", "request": request, "only_unadmitted": True},
        )
    )
    assert (guarded["lookup"], guarded["run_id"], guarded["only_unadmitted"]) == (
        "found",
        run_id,
        True,
    )
    assert registry.get_run(run_id) == before
    assert registry.read_claim(writer_route.worktree) == claim
    conflicting = _payload(
        _call(
            server,
            "nspawn_run_close",
            {
                "idempotency_key": "kept",
                "request": {**request, "arguments": {**arguments, "prompt": "other"}},
                "only_unadmitted": True,
            },
        )
    )
    assert conflicting == {
        "lookup": "conflict",
        "run_id": run_id,
        "only_unadmitted": True,
    }
    assert registry.get_run(run_id) == before
    assert registry.read_claim(writer_route.worktree) == claim


def test_missing_only_key_close_repeats_a_fence_and_blocks_late_admission(
    registry: Registry,
    tmp_path: Path,
    writer_route: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Supervisor, "_drive_native_run", _forbidden)
    server = McpServer(supervisor=_sv(registry, tmp_path))
    arguments = _read_only(writer_route)
    request = {"tool": "nspawn_run_start", "arguments": arguments}
    for _ in range(2):
        assert _payload(
            _call(
                server,
                "nspawn_run_close",
                {
                    "idempotency_key": "retired",
                    "request": request,
                    "only_unadmitted": True,
                },
            )
        ) == {
            "lookup": "fenced",
            "idempotency_key": "retired",
            "only_unadmitted": True,
        }
    late = _call(
        server, "nspawn_run_start", {**arguments, "idempotency_key": "retired"}
    )
    assert late.get("error", {}).get("data", {}).get("type") == "KeyFenced", late
    assert _store_rows("SELECT count(*) FROM runs")[0][0] == 0


def test_missing_only_fence_uses_the_native_servers_repository_key_space(
    registry: Registry,
    tmp_path: Path,
    writer_route: SimpleNamespace,
) -> None:
    """Two live stdio servers share a store, not their process-cwd key space."""
    first, second = tmp_path / "first", tmp_path / "second"
    for repository in (first, second):
        (repository / ".git").mkdir(parents=True)
    bootstrap = tmp_path / "namespace_stdio.py"
    bootstrap.write_text(
        """
import os, sys, time
from pathlib import Path
from nspawn import native, picker, system1
from nspawn.main import serve
from nspawn.mcp import McpServer
from nspawn.owner_roster import NativeAccount, Provider, Roster
from nspawn.redact import SecretNeedles
from nspawn.registry import Registry
from nspawn.rows import NativeSessionContext
from nspawn.supervisor import Supervisor
sys.path.insert(0, sys.argv[3])
from class_dispatch import answering
system1.ask = answering()
provider = Provider(name='fixture', kind='codex', model='fixture', models=['fixture'])
picker.pick_execution = lambda *a, **k: picker.PickerResult(
    None, provider, 'fixture', 'fixture', None, None
)
Roster.refuse_unservable_spawn = lambda *a: None
test_root = Path(sys.argv[2]).parent.parent
native.prepare_native = lambda *a, **k: native.NativeLaunch(
    ('/usr/bin/false',), {}, SecretNeedles.none(),
    NativeSessionContext(str(test_root), read_roots=(str(test_root / 'checkout'),)),
    NativeAccount(test_root),
)
# Only the vendor turn is inert: stdio, key lock, admission and registry are real.
Supervisor._drive_native_run = lambda self, *a: None
Supervisor.TICK_INTERVAL_S = 3600
with Registry(sys.argv[1], 'namespace-' + str(os.getpid())).connect() as registry:
    registry.open(now=int(time.time() * 1000))
    server = McpServer(supervisor=Supervisor(registry, Path(sys.argv[2])))
    serve(server)
"""
    )
    arguments = _read_only(writer_route)
    guarded = {
        "idempotency_key": "scoped",
        "request": {"tool": "nspawn_run_start", "arguments": arguments},
        "only_unadmitted": True,
    }
    with contextlib.ExitStack() as cleanup:
        children = []
        for repository in (first, second):
            child_process = subprocess.Popen(
                [
                    sys.executable,
                    "-u",
                    str(bootstrap),
                    str(store()),
                    str(repository / "runs"),
                    str(Path(__file__).parent),
                ],
                cwd=repository,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            def finish(process: subprocess.Popen[str]) -> None:
                try:
                    _, errors = process.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate(timeout=10)
                    pytest.fail("the namespace stdio server did not stop")
                assert process.returncode == 0, errors

            cleanup.callback(finish, child_process)
            children.append(child_process)
            assert child_process.stdin is not None
            assert child_process.stdout is not None
            for frame in (
                {
                    "jsonrpc": "2.0",
                    "id": 0,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1"},
                    },
                },
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
            ):
                child_process.stdin.write(json.dumps(frame) + "\n")
                child_process.stdin.flush()
            assert select.select([child_process.stdout], [], [], 10)[0]
            assert json.loads(child_process.stdout.readline())["id"] == 0
        assert children[0].pid != children[1].pid

        def call(index: int, tool: str, params: dict[str, Any]) -> dict[str, Any]:
            process = children[index]
            assert process.stdin is not None and process.stdout is not None
            process.stdin.write(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {"name": tool, "arguments": params},
                    }
                )
                + "\n"
            )
            process.stdin.flush()
            assert select.select([process.stdout], [], [], 10)[0], tool
            answer = json.loads(process.stdout.readline())
            assert answer["id"] == 1, answer
            return answer

        start = {**arguments, "idempotency_key": "scoped"}
        run_id = _payload(call(0, "nspawn_run_start", start))["run_id"]
        before = registry.get_run(run_id)
        assert _payload(call(1, "nspawn_run_close", guarded)) == {
            "lookup": "fenced",
            "idempotency_key": "scoped",
            "only_unadmitted": True,
        }
        late = call(1, "nspawn_run_start", start)
        assert late["error"]["data"]["type"] == "KeyFenced", late
        found = _payload(call(0, "nspawn_run_close", guarded))
        assert found["lookup"] == "found" and found["run_id"] == run_id
        assert found["only_unadmitted"] is True
        reattached = _payload(call(0, "nspawn_run_start", start))
        assert reattached["run_id"] == run_id
        assert reattached["deduplicated"] is True
        assert registry.get_run(run_id) == before


_FINGERPRINT_CWD = "/nonexistent/e1-fingerprint/checkout"
_READ_IT = {
    "prompt": "read it",
    "cwd": _FINGERPRINT_CWD,
    "execution_class": "research",
    "read_only": True,
}
_SHAPES: dict[str, tuple[str, dict[str, Any], str | None, list[str] | None]] = {
    "minimal_read_only": ("nspawn_run_start", _READ_IT, None, None),
    "writable": (
        "nspawn_run_start",
        {
            "prompt": "change it",
            "cwd": _FINGERPRINT_CWD,
            "execution_class": "implementation",
        },
        None,
        None,
    ),
    "full": (
        "nspawn_run_start",
        {
            **_READ_IT,
            "provider": "codex-main",
            "model": "gpt-6-astra",
            "effort": "high",
            "timeout_ms": 600000,
            "session_id": "3f2b8c3e-0d7a-4b8e-9d65-5f2f5b0d8a11",
            "system_prompt": "be brief",
            "read_paths": [
                "/nonexistent/e1-fingerprint/b",
                "/nonexistent/e1-fingerprint/a",
            ],
        },
        "a-group",
        None,
    ),
    "review": (
        "nspawn_review_start",
        {"branch": "feature", "cwd": _FINGERPRINT_CWD, "execution_class": "review"},
        None,
        None,
    ),
    "batch_entry": ("nspawn_run_start", _READ_IT, "g", ["codex-main", "claude"]),
}
#: Each shape's digest at base ff2092217: a key admitted before native write
#: existed must keep matching the same request after it.
_BASE_DIGESTS = {
    "minimal_read_only": "307e6675215618303054dc86329eaf40757e7e17f6e90f8a08f0190223cc0333",
    "writable": "dde485c3f3e839085884d9bdf42407237ac8464a5c2dc8a92fb098f512b28835",
    "full": "d43e1e41675bb44e3c78cb9414b2f560f1703a2a835d51ba607e0426bdb830e6",
    "review": "8cea60a013464eed4f7471ec7948ad4f2cccd1c7522883dd0fa1e4a0c7c51283",
    "batch_entry": "e220b01cd1a9b811c38a1138e8824e48a6cd0255200cca5028bbc22dd81ad361",
}


def _fingerprint(shape: str, **changes: Any) -> str:
    tool, arguments, group, batch = _SHAPES[shape]
    return driver.request_fingerprint(
        tool, {**arguments, **changes}, group=group, batch=batch, strict=True
    )


@pytest.mark.parametrize("shape", sorted(_SHAPES))
def test_baseline_request_shapes_hash_as_at_base(shape: str) -> None:
    assert _fingerprint(shape) == _BASE_DIGESTS[shape]


def test_native_write_is_part_of_the_request_only_when_true() -> None:
    assert _fingerprint("writable", native_write=False) == _BASE_DIGESTS["writable"]
    assert _fingerprint("writable", native_write=True) != _BASE_DIGESTS["writable"]


def test_the_same_key_with_and_without_native_write_conflicts(
    registry: Registry,
    tmp_path: Path,
    writer_route: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Supervisor, "_drive_native_run", lambda self, *args: None)
    server = McpServer(supervisor=_sv(registry, tmp_path))
    keyed = _write(writer_route, idempotency_key="writer-key")
    run_id = _payload(_call(server, "nspawn_run_start", keyed))["run_id"]
    del keyed["native_write"]

    refused = _call(server, "nspawn_run_start", keyed)

    data = refused.get("error", {}).get("data", {})
    assert (data.get("type"), data.get("run_id")) == ("IdempotencyConflict", run_id)


# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------


def _network_grants(value: Any) -> list[str]:
    """Every key in a frame that turns the network on."""
    if isinstance(value, list):
        return [grant for item in value for grant in _network_grants(item)]
    if not isinstance(value, dict):
        return []
    grants = [
        key
        for key, item in value.items()
        if "network" in key.lower()
        and (item is True or isinstance(item, dict) and item.get("enabled") is True)
    ]
    return grants + [
        grant for item in value.values() for grant in _network_grants(item)
    ]


@pytest.mark.parametrize(
    "mode",
    [
        "read-only",
        "read-profile",
        pytest.param("writer", marks=pytest.mark.needs("systemd_user", "cgroup")),
    ],
)
def test_no_turn_asks_for_the_network(tmp_path: Path, mode: str) -> None:
    checkout = Path(os.path.realpath(tmp_path)) / "checkout"
    (checkout / ".git").mkdir(parents=True)
    record = tmp_path / "record"
    record.mkdir()
    script = tmp_path / "writer_server.py"
    script.write_text(WRITER_SERVER)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    writer = mode == "writer"
    with contextlib.ExitStack() as stack:
        scope = (
            stack.enter_context(AggregateScope(memory_bytes=256 * 1024**2, tasks=64))
            if writer
            else None
        )
        session = stack.enter_context(
            AppServerTurn.start(
                [sys.executable, "-u", str(script), "app-server"],
                cwd=checkout,
                env={
                    "PATH": "/usr/bin:/bin",
                    "WRITER_RECORD": str(record),
                    "WRITER_SANDBOX": json.dumps(OBSERVED_WRITER_SANDBOX),
                },
                run_dir=run_dir,
                needles=SecretNeedles.none(),
                scope=scope,
            )
        )
        extra: dict[str, Any] = (
            {
                "native_write": True,
                "readonly_mcp": True,
                "tool_environment": {"HOME": str(checkout / ".nott/disposable/home")},
            }
            if writer
            else {}
        )
        result = session.run(
            "ping",
            model="fixture",
            read_only=not writer,
            timeout=10,
            read_roots=None if mode == "read-only" else (str(checkout),),
            **extra,
        )
    assert result.status == "completed"
    frames = [
        json.loads(line) for line in (record / "frames.ndjson").read_text().splitlines()
    ]
    (thread,) = [f["params"] for f in frames if f.get("method") == "thread/start"]
    assert _network_grants(frames) == []
    if writer:
        profile = thread["config"]["permissions"][thread["permissions"]]
        assert profile["filesystem"] == {
            ":root": "deny",
            ":minimal": "read",
            str(checkout): "write",
            f"{checkout}/.git": "read",
        }


# ---------------------------------------------------------------------------
# The profile under the real codex sandbox
# ---------------------------------------------------------------------------

SANDBOX_CHECKS = r"""
import json, os, socket, sys
checkout, outside, port, tmp_name = sys.argv[1:]
def attempt(action):
    try:
        action()
        return 'ok'
    except OSError as error:
        return type(error).__name__
def write(path):
    with open(path, 'w') as handle:
        handle.write('x')
def bus():
    with socket.socket(socket.AF_UNIX) as client:
        client.connect(f'/run/user/{os.getuid()}/bus')
def tcp():
    socket.create_connection(('127.0.0.1', int(port)), timeout=3).close()
print(json.dumps({
    'create': attempt(lambda: write(os.path.join(checkout, 'created.txt'))),
    'edit': attempt(lambda: write(os.path.join(checkout, 'tracked.txt'))),
    'delete': attempt(lambda: os.remove(os.path.join(checkout, 'doomed.txt'))),
    'mkdir': attempt(lambda: os.makedirs(os.path.join(checkout, '.nott', 'disposable', 'tmp'))),
    'git': attempt(lambda: write(os.path.join(checkout, '.git', 'probe'))),
    'outside': attempt(lambda: write(os.path.join(outside, 'escaped.txt'))),
    'tmp': attempt(lambda: write(os.path.join('/tmp', tmp_name))),
    'tcp': attempt(tcp),
    'bus': attempt(bus),
}))
"""


def _codex() -> str | None:
    found = shutil.which("codex") or str(Path.home() / ".local/bin/codex")
    return os.path.realpath(found) if os.access(found, os.X_OK) else None


def _inline_table(table: dict[str, Any]) -> str:
    """A TOML inline table of string keys and string or boolean values."""
    return (
        "{"
        + ", ".join(
            f"{json.dumps(key)}={str(value).lower() if isinstance(value, bool) else json.dumps(value)}"
            for key, value in table.items()
        )
        + "}"
    )


@pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="codex sandbox runs bwrap on Linux"
)
def test_the_writer_profile_holds_under_the_real_codex_sandbox(tmp_path: Path) -> None:
    codex = _codex()
    if codex is None:
        pytest.skip("no codex binary on this host; the profile is proven where one is")
    checkout = Path(os.path.realpath(tmp_path)) / "checkout"
    (checkout / ".git").mkdir(parents=True)
    (checkout / "tracked.txt").write_text("base\n")
    (checkout / "doomed.txt").write_text("doomed\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    tmp_name = f"nspawn-writer-{uuid.uuid4().hex}"
    # Codex refuses a CODEX_HOME under a temp directory for its own helpers.
    home = Path.home() / ".cache" / f"nspawn-writer-{uuid.uuid4().hex}"
    name = "nott-nspawn-write-test"
    profile = app_server.permission_profile((str(checkout), codex), str(checkout))
    try:
        (home / "codex-home").mkdir(parents=True)
        with socket.create_server(("127.0.0.1", 0)) as listener:
            completed = subprocess.run(
                [
                    codex,
                    "sandbox",
                    "-C",
                    str(checkout),
                    "-P",
                    name,
                    "-c",
                    f"permissions.{name}.filesystem={_inline_table(profile['filesystem'])}",
                    "-c",
                    f"permissions.{name}.network={_inline_table(profile['network'])}",
                    "--",
                    "/usr/bin/python3",
                    "-c",
                    SANDBOX_CHECKS,
                    str(checkout),
                    str(outside),
                    str(listener.getsockname()[1]),
                    tmp_name,
                ],
                env={
                    "PATH": "/usr/bin:/bin",
                    "HOME": str(home),
                    "CODEX_HOME": str(home / "codex-home"),
                },
                cwd=checkout,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
    finally:
        shutil.rmtree(home, ignore_errors=True)
        with contextlib.suppress(FileNotFoundError):
            os.remove(os.path.join("/tmp", tmp_name))
    assert completed.returncode == 0, completed.stderr
    results = json.loads(completed.stdout.strip().splitlines()[-1])
    granted = {check for check, outcome in results.items() if outcome == "ok"}
    assert granted == {"create", "edit", "delete", "mkdir"}, results
    assert not (checkout / ".git" / "probe").exists()
    assert list(outside.iterdir()) == []


@pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="codex sandbox runs bwrap on Linux"
)
def test_the_writer_tool_environment_is_closed_under_the_real_codex_sandbox(
    tmp_path: Path,
) -> None:
    """Every owner account carries its own `[shell_environment_policy.set]`; none
    of it may reach a writer's tools, whatever a later edit puts there."""
    codex = _codex()
    if codex is None:
        pytest.skip(
            "no codex binary on this host; the environment is proven where one is"
        )
    checkout = Path(os.path.realpath(tmp_path)) / "checkout"
    (checkout / ".git").mkdir(parents=True)
    tools = {
        **supervisor._disposable_environment(str(checkout)),
        child.RUN_ID_ENV: "run-live",
    }
    # The writer thread's own policy: the base a caged thread carries, then its tools.
    base = wire.thread_start_params(
        "fixture",
        str(checkout),
        wire.ApprovalPolicy.NEVER,
        wire.ExecutorSandbox.READ_ONLY,
    )["config"]
    policy = {
        key: value
        for key, value in base.items()
        if key.startswith("shell_environment_policy.")
    }
    policy.update(app_server.tool_environment_config(tools))
    home = Path.home() / ".cache" / f"nspawn-writer-{uuid.uuid4().hex}"
    name = "nott-nspawn-write-test"
    profile = app_server.permission_profile((codex,), str(checkout))
    try:
        (home / "codex-home").mkdir(parents=True)
        (home / "codex-home" / "config.toml").write_text(
            '[shell_environment_policy.set]\nFOREIGN_ACCOUNT_SETTING = "leaks"\n'
        )
        completed = subprocess.run(
            [
                codex,
                "sandbox",
                "-C",
                str(checkout),
                "-P",
                name,
                "-c",
                f"permissions.{name}.filesystem={_inline_table(profile['filesystem'])}",
                "-c",
                f"permissions.{name}.network={_inline_table(profile['network'])}",
                *(
                    argument
                    for key, value in policy.items()
                    for argument in ("-c", f"{key}={json.dumps(value)}")
                ),
                "--",
                "/usr/bin/env",
                "-0",
            ],
            env={
                "PATH": "/usr/bin:/bin",
                "HOME": str(home),
                "CODEX_HOME": str(home / "codex-home"),
            },
            cwd=checkout,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    finally:
        shutil.rmtree(home, ignore_errors=True)
    assert completed.returncode == 0, completed.stderr
    seen = dict(entry.split("=", 1) for entry in completed.stdout.split("\0") if entry)
    # Codex's sandbox adds these two itself, after the policy; joined, since
    # GitGuardian reads the pair as a password.
    vendor = {"CODEX_SANDBOX_NETWORK_DISABLED", "P" + "WD"}
    assert {k: v for k, v in seen.items() if k not in vendor} == {
        "PATH": SANDBOX_TOOL_PATH,
        **tools,
    }


class _BeforeTheTurn(Exception):
    """Raised where the turn would start: no model is asked, no quota spent."""


@pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="the writer's slice is systemd's"
)
@pytest.mark.parametrize(
    "mode",
    [
        pytest.param("writer", marks=pytest.mark.needs("systemd_user", "cgroup")),
        "reader",
    ],
)
def test_a_profiled_thread_reaches_its_turn_on_the_real_codex(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """thread/start and the MCP status pages against the installed codex, stopped
    just before turn/start: the vendor, not a double, judges the profile config."""
    codex = _codex()
    if codex is None:
        pytest.skip("no codex binary on this host; the thread is proven where one is")

    def before_the_turn(*args: object, **kwargs: object) -> dict[str, Any]:
        raise _BeforeTheTurn

    monkeypatch.setattr(app_server.wire, "turn_start_params", before_the_turn)
    checkout = Path(os.path.realpath(tmp_path)) / "checkout"
    (checkout / ".git").mkdir(parents=True)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    home = Path.home() / ".cache" / f"nspawn-writer-{uuid.uuid4().hex}"
    writer = mode == "writer"
    with contextlib.ExitStack() as stack:
        stack.callback(shutil.rmtree, home, ignore_errors=True)
        (home / "codex-home").mkdir(parents=True)
        scope = (
            stack.enter_context(AggregateScope(memory_bytes=1024**3, tasks=256))
            if writer
            else None
        )
        session = stack.enter_context(
            AppServerTurn.start(
                (
                    codex,
                    "-c",
                    'cli_auth_credentials_store="file"',
                    "-c",
                    "features.memories=false",
                    "app-server",
                    "--listen",
                    "stdio://",
                ),
                cwd=checkout,
                env={
                    "PATH": "/usr/bin:/bin",
                    "HOME": str(home),
                    "CODEX_HOME": str(home / "codex-home"),
                },
                run_dir=run_dir,
                needles=SecretNeedles.none(),
                scope=scope,
            )
        )
        with pytest.raises(_BeforeTheTurn):
            session.run(
                "never sent",
                model="gpt-5.5",
                read_only=not writer,
                timeout=60,
                mcp_servers={},
                readonly_mcp=True,
                read_roots=(str(checkout),),
                native_write=writer,
                tool_environment=(
                    {
                        **supervisor._disposable_environment(str(checkout)),
                        child.RUN_ID_ENV: "run-live",
                    }
                    if writer
                    else None
                ),
            )


# ---------------------------------------------------------------------------
# The Claude writer: the client's own Bash sandbox, in the writer's slice
# ---------------------------------------------------------------------------

#: A stand-in `claude`: answers the version probe, keeps its argv, env and
#: cgroup beside itself, and writes the checkout. With the prompt `hold` it
#: leaves a `setsid` descendant behind and never finishes; with `reject` it
#: reports a rejected credential.
CLAUDE_WRITER = r"""
import json, os, subprocess, sys, time
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
prompt = sys.stdin.read()
(here / f"cgroup-{os.getpid()}").write_text(Path("/proc/self/cgroup").read_text())
(here / "seen.json").write_text(json.dumps(
    {"argv": sys.argv[1:], "env": dict(os.environ), "cwd": os.getcwd(), "prompt": prompt}))
target = Path.cwd()
if "--restricted" in sys.argv and "--settings" in sys.argv:
    Path(".claude/.cc-writes").mkdir(parents=True, mode=0o700)
    settings = json.loads(Path(sys.argv[sys.argv.index("--settings") + 1]).read_text())
    roots = settings["sandbox"]["filesystem"]["allowWrite"]
    preface, separator, payload = prompt.partition("\n\n")
    assert separator
    if len(roots) == 2:
        target = Path(roots[0])
        assert preface.startswith(f"The writable project checkout is the exact directory {target}. ")
        assert "all project work" in preface
    else:
        assert len(roots) == 1 and roots[0] == os.environ["TMPDIR"]
        assert "read-only directory" in preface
    prompt = payload
if prompt == "hold":
    escaped = subprocess.Popen(["/usr/bin/sleep", "60"], start_new_session=True)
    (here / "escaped.tmp").write_text(str(escaped.pid))
    os.replace(here / "escaped.tmp", here / "escaped.pid")
    time.sleep(60)
if prompt == "sandbox-down":
    print(json.dumps({"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "is_error": True, "content":
         "Sandbox is required but failed to initialize: Failed to create bridge "
         "sockets after 5 attempts. Restart to retry."}]}}), flush=True)
if prompt == "quote-it":
    print(json.dumps({"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "is_error": False, "content":
         'SANDBOX_DOWN: Final = b"Sandbox is required but failed to initialize"'}]}}),
        flush=True)
if prompt == "reject":
    print(json.dumps({"type": "result", "is_error": True, "terminal_reason": "error",
                      "result": "Invalid bearer token", "api_error_status": 401}), flush=True)
    raise SystemExit(1)
refused = here / "refused"
if refused.exists() and os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") in refused.read_text().split():
    # The organization refuses this OAuth account, as recorded from Claude Code
    # 2.1.257. The CLI keeps the turn in its native history before it asks.
    if "--resume" in sys.argv:
        thread = sys.argv[sys.argv.index("--resume") + 1]
    else:
        thread = next(arg.split("=", 1)[1] for arg in sys.argv if arg.startswith("--session-id="))
        # A CLI that keeps no history of a refused turn.
        if not (here / "forgetful").exists():
            (Path(os.environ["CLAUDE_CONFIG_DIR"]) / "peer-history.json").write_text(
                json.dumps({"thread": thread, "prompt": prompt}))
    text = ("Your organization has disabled Claude subscription access for Claude Code"
            " \u00b7 Use an Anthropic API key instead, or ask your admin to enable access")
    for frame in (
        {"type": "assistant", "error": "oauth_org_not_allowed", "is_api_error_message": True,
         "session_id": thread, "parent_tool_use_id": None, "message": {"role": "assistant",
         "model": "<synthetic>", "content": [{"type": "text", "text": text}]}},
        {"type": "result", "subtype": "success", "is_error": True,
         "terminal_reason": "api_error", "api_error_status": 403, "result": text,
         "session_id": thread, "modelUsage": {}},
    ):
        print(json.dumps(frame), flush=True)
    raise SystemExit(1)
walled = here / "walled"
if walled.exists() and walled.read_text() == os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
    # The vendor's own weekly-limit reply, as captured from Claude Code 2.1.296.
    session = sys.argv[sys.argv.index("--resume") + 1]
    for frame in (
        {"type": "rate_limit_event", "session_id": session, "rate_limit_info": {
            "status": "rejected", "resetsAt": int(time.time()) + 3600,
            "rateLimitType": "seven_day", "isUsingOverage": False}},
        {"type": "assistant", "error": "rate_limit", "session_id": session,
         "parent_tool_use_id": None, "message": {"role": "assistant", "model": "<synthetic>",
         "content": [{"type": "text", "text": "You've hit your weekly limit"}]}},
        {"type": "result", "subtype": "success", "is_error": True,
         "terminal_reason": "api_error", "api_error_status": 429,
         "result": "You've hit your weekly limit", "session_id": session,
         "modelUsage": {"claude-opus-5": {"inputTokens": 8, "outputTokens": 0, "costUSD": 0}}},
    ):
        print(json.dumps(frame), flush=True)
    raise SystemExit(1)
if "CLAUDE_CONFIG_DIR" in os.environ:
    native_history = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "peer-history.json"
    if "--resume" in sys.argv:
        original = json.loads(native_history.read_text())
        assert original["thread"] == sys.argv[sys.argv.index("--resume") + 1]
        final = "Remembered original input: " + original["prompt"]
    else:
        thread = next(arg.split("=", 1)[1] for arg in sys.argv if arg.startswith("--session-id="))
        if native_history.exists() and json.loads(native_history.read_text())["thread"] == thread:
            print(f"Error: Session ID {thread} is already in use.", file=sys.stderr)
            raise SystemExit(1)
        native_history.write_text(json.dumps({"thread": thread, "prompt": prompt}))
        final = "DONE"
else:
    final = "DONE"
(target / "written.txt").write_text("written\n")
print(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
    {"type": "text", "text": final}]}}), flush=True)
print(json.dumps({"type": "result", "is_error": False, "terminal_reason": "completed",
                  "result": final, "usage": {"input_tokens": 1, "output_tokens": 1}}), flush=True)
"""

#: A credential-shaped stand-in, assembled here so no key shape sits in the source.
_CLAUDE_TOKEN = "sk" + "-ant-oat01-writer-stand-in-0123456789"


@pytest.fixture
def claude_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, roster_home: Path
) -> SimpleNamespace:
    """A claude-kind route `writer` with two accounts whose binary is
    `CLAUDE_WRITER`, and a standalone checkout under the declared writer root."""
    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)
    stand_in = tmp_path / "claude-bin"
    stand_in.mkdir()
    binary = stand_in / "claude"
    binary.write_text(f"#!{sys.executable}\n{CLAUDE_WRITER}")
    binary.chmod(0o755)
    plugin = tmp_path / "plugin"
    (plugin / "skills").mkdir(parents=True)
    # Valid for the uncaged read-only path; the caged runs load no plugin.
    (plugin / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"nmemory": {"command": "nmemory"}}})
    )
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "writer-a.token").write_text(_CLAUDE_TOKEN + "\n")
    (secrets / "writer-b.token").write_text(_CLAUDE_TOKEN[::-1] + "\n")
    roster = roster_home / ".nott" / "spawn.toml"
    roster.write_text(
        f"[defaults]\nplugins = [{json.dumps(str(plugin))}]\n"
        f"[bins]\nclaude = {json.dumps(str(binary))}\n"
        '[[provider]]\nname = "writer"\nkind = "claude"\n'
        f"secrets = {json.dumps(str(secrets))}\n"
        'model = "opus"\nmodels = ["opus"]\n'
        f"[native_write]\nroots = [{json.dumps(str(tmp_path))}]\n"
    )
    monkeypatch.delenv(supervisor.CLAUDE_BIN_ENV, raising=False)
    return SimpleNamespace(
        checkout=checkout,
        worktree=os.path.realpath(checkout),
        stand_in=stand_in,
        roster=roster,
    )


def _claude_write(route: SimpleNamespace, **changes: Any) -> dict[str, Any]:
    return {
        "prompt": "change it",
        "cwd": str(route.checkout),
        "provider": "writer-a",
        "class": DECLARED_CLASS,
        "native_write": True,
        **changes,
    }


def _seen_by(route: SimpleNamespace) -> dict[str, Any]:
    seen: dict[str, Any] = json.loads((route.stand_in / "seen.json").read_text())
    return seen


def test_claude_implements_a_caged_writer_and_a_caged_review() -> None:
    assert supervisor.CAPABILITIES["claude"] == ("custody", "resume", "review", "write")


@pytest.mark.needs("systemd_user", "cgroup")
def test_a_claude_writer_runs_caged_in_its_own_slice(
    registry: Registry, tmp_path: Path, claude_writer: SimpleNamespace
) -> None:
    sv = _sv(registry, tmp_path)
    run_id = _payload(
        _call(
            McpServer(supervisor=sv), "nspawn_run_start", _claude_write(claude_writer)
        )
    )["run_id"]

    status = sv.status(run_id, timeout_ms=20_000)
    assert status.phase is RunPhase.COMPLETED, (status.failure_reason, status.warning)
    assert (claude_writer.checkout / "written.txt").read_text() == "written\n"
    seen = _seen_by(claude_writer)
    argv: list[str] = seen["argv"]
    assert argv[:1] == ["-p"] and "--restricted" in argv
    assert argv[argv.index("--tools") + 1] == "Bash"
    assert "--plugin-dir" not in argv
    settings = Path(argv[argv.index("--settings") + 1])
    assert stat.S_IMODE(settings.stat().st_mode) == 0o600
    cage = json.loads(settings.read_text())["sandbox"]
    assert cage["filesystem"]["denyRead"][0] == "/"
    assert cage["allowUnsandboxedCommands"] is False
    env: dict[str, str] = seen["env"]
    assert env[child.CLAUDE_OAUTH_TOKEN_ENV] == _CLAUDE_TOKEN
    assert "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB" not in env
    assert env[child.RUN_ID_ENV] == run_id
    assert seen["cwd"] == str(Path(env["TMPDIR"]) / "work")
    assert not (claude_writer.checkout / ".claude").exists()
    assert seen["prompt"].startswith(
        f"The writable project checkout is the exact directory {claude_writer.checkout}. "
    )
    assert seen["prompt"].endswith("\n\nchange it")
    row = registry.get_run(run_id)
    assert row is not None
    assert (row.backend, row.sandbox) == (Backend.CLAUDE, "workspace-write")
    assert row.executed_account == "writer-a"
    (scope,) = _writer_scopes(run_id)
    group = "0::/" + str(Path(scope["cgroup"]).relative_to("/sys/fs/cgroup")) + "/"
    cgroups = [p.read_text() for p in claude_writer.stand_in.glob("cgroup-*")]
    assert len(cgroups) == 1 and cgroups[0].startswith(group)
    assert not Path(scope["cgroup"]).exists()
    claim = registry.read_claim(claude_writer.worktree)
    assert claim is not None and claim.run_id == run_id
    assert sv.close(run_id).claim_released is True


def test_a_claude_writer_account_with_its_own_endpoint_is_refused_before_any_child(
    registry: Registry,
    tmp_path: Path,
    claude_writer: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The account is more specific than its route, so its env is judged too."""
    with claude_writer.roster.open("a") as roster:
        roster.write(
            "[provider.accounts.writer-c]\n"
            f"home = {json.dumps(str(tmp_path / 'writer-c'))}\n"
            'auth_env = "WRITER_C_TOKEN"\n'
            "[provider.accounts.writer-c.env]\n"
            'ANTHROPIC_BASE_URL = "https://proxy.invalid"\n'
        )
    monkeypatch.setattr(child, "spawn_child", _forbidden)
    answer = _call(
        McpServer(supervisor=_sv(registry, tmp_path)),
        "nspawn_run_start",
        _claude_write(claude_writer, provider="writer-c"),
    )
    assert answer is not None
    assert "ANTHROPIC_BASE_URL" in answer["error"]["message"]
    assert _store_rows("SELECT run_id FROM runs") == []


class _PaperSlice:
    """A slice with no cgroup behind it: the call site, not the kernel, is judged."""

    opened: ClassVar[list[_PaperSlice]] = []
    #: What the test wants observed at the moment the slice is emptied.
    on_close: ClassVar[Callable[[], None] | None] = None

    def __init__(self, **_: object) -> None:
        self.unit = "paper.slice"
        self.path = Path("/sys/fs/cgroup/paper.slice")
        self.closed = False
        self.drained = 0
        _PaperSlice.opened.append(self)

    def drain(self) -> None:
        self.drained += 1

    def close(self) -> None:
        if _PaperSlice.on_close is not None:
            _PaperSlice.on_close()
        self.closed = True

    @classmethod
    def recorded(cls, unit: str) -> _PaperSlice:
        return next(scope for scope in cls.opened if scope.unit == unit)


@pytest.fixture
def paper_slices(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Writer slices without systemd: the scope each spawn was handed, in order."""
    _PaperSlice.opened = []
    monkeypatch.setattr(_PaperSlice, "on_close", None)
    monkeypatch.setattr(supervisor, "AggregateScope", _PaperSlice)
    scopes: list[object] = []
    spawn = child.spawn_child

    def launched(*args: Any, scope: object = None, **kwargs: Any) -> Any:
        scopes.append(scope)
        return spawn(*args, **kwargs)

    monkeypatch.setattr(child, "spawn_child", launched)
    return scopes


def test_the_claude_writer_call_site_launches_the_cage_into_its_slice(
    registry: Registry,
    tmp_path: Path,
    claude_writer: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    paper_slices: list[object],
) -> None:
    """Without systemd: the supervisor hands the child the composed cage, the
    closed env and the writer's own slice, and closes that slice."""
    monkeypatch.setenv("CANARY_ENV", "SECRET-ENV")
    alive_at_close: list[bool] = []
    monkeypatch.setattr(
        _PaperSlice,
        "on_close",
        lambda: alive_at_close.append(
            Path(_seen_by(claude_writer)["env"]["TMPDIR"]).is_dir()
        ),
    )
    sv = _sv(registry, tmp_path)
    run_id = _payload(
        _call(
            McpServer(supervisor=sv), "nspawn_run_start", _claude_write(claude_writer)
        )
    )["run_id"]

    assert sv.status(run_id, timeout_ms=20_000).phase is RunPhase.COMPLETED
    (paper,) = _PaperSlice.opened
    assert paper_slices == [paper] and paper.closed
    seen = _seen_by(claude_writer)
    argv: list[str] = seen["argv"]
    assert "--restricted" in argv
    mode_flags = [a for a in argv if a.partition("=")[0] == "--permission-mode"]
    assert mode_flags == ["--permission-mode"]
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert argv[argv.index("--tools") + 1] == "Bash"
    env: dict[str, str] = seen["env"]
    assert "CANARY_ENV" not in env
    assert env[child.CLAUDE_OAUTH_TOKEN_ENV] == _CLAUDE_TOKEN
    assert seen["cwd"] == str(Path(env["TMPDIR"]) / "work")
    assert not (claude_writer.checkout / ".claude").exists()
    assert seen["prompt"].startswith(
        f"The writable project checkout is the exact directory {claude_writer.checkout}. "
    )
    assert seen["prompt"].endswith("\n\nchange it")
    run_root = Path(argv[argv.index("--settings") + 1]).parent
    assert Path(env["CLAUDE_CONFIG_DIR"]).parent == run_root
    # Short and outside the run directory, whatever its depth (the client's
    # bridge sockets live under it); kept until the slice is empty, then gone.
    scratch = Path(env["TMPDIR"])
    assert scratch.parent == Path("/var/tmp") and len(str(scratch)) < 40
    assert alive_at_close == [True] and not scratch.exists()


@pytest.mark.parametrize("reference", ["thread", "run"])
@pytest.mark.parametrize("role", ["writer", "review"])
def test_a_verified_claude_writer_continues_its_original_native_history(
    registry: Registry,
    tmp_path: Path,
    claude_writer: SimpleNamespace,
    paper_slices: list[object],
    reference: str,
    role: str,
) -> None:
    """Real private registry/MCP/stdio peer; vendor and kernel cage are fixtures.

    The second turn must consume the retained native history, rather than replay
    the first prompt or invent a new conversation. The original terminal row
    and result remain intact, and the new writer reacquires its own claim.
    """
    sv = _sv(registry, tmp_path)
    server = McpServer(supervisor=sv)
    arguments = (
        _claude_write(claude_writer)
        if role == "writer"
        else _claude_review(claude_writer)
    )
    first = _payload(
        _call(
            server,
            "nspawn_run_start",
            {**arguments, "prompt": "origin beacon"},
        )
    )["run_id"]
    assert sv.status(first, timeout_ms=20_000).phase is RunPhase.COMPLETED
    original_config = _seen_by(claude_writer)["env"]["CLAUDE_CONFIG_DIR"]
    assert (registry.read_claim(claude_writer.worktree) is not None) is (
        role == "writer"
    )
    assert sv.close(first).claim_released is True
    original = registry.get_run(first)
    assert original is not None and original.thread_id is not None
    original_result = sv.result(first)

    request = {
        **arguments,
        "prompt": "continue this conversation",
        **(
            {"session_id": original.thread_id}
            if reference == "thread"
            else {"original_run": first}
        ),
    }
    if reference == "run":
        request.pop("class")  # The server retains the original class and deadline.
        request.pop(
            "native_write", None
        )  # The server retains the first request's opt-in.
    follow_up = _payload(_call(server, "nspawn_run_start", request))["run_id"]
    terminal = sv.status(follow_up, timeout_ms=20_000)
    assert terminal.phase is RunPhase.COMPLETED, terminal
    assert terminal.session is not None and terminal.session.original_run_id == first
    assert sv.result(follow_up).final_text == "Remembered original input: origin beacon"
    seen = _seen_by(claude_writer)
    assert seen["env"]["CLAUDE_CONFIG_DIR"] == original_config
    mode_flags = [a for a in seen["argv"] if a.partition("=")[0] == "--permission-mode"]
    assert mode_flags == ["--permission-mode"]
    assert seen["argv"][seen["argv"].index("--permission-mode") + 1] == "dontAsk"
    assert seen["argv"][seen["argv"].index("--resume") + 1] == original.thread_id
    resumed = registry.get_run(follow_up)
    assert resumed is not None
    assert (
        resumed.thread_id,
        resumed.executed_account,
        resumed.worktree,
        resumed.sandbox,
    ) == (
        original.thread_id,
        original.executed_account,
        original.worktree,
        original.sandbox,
    )
    assert registry.get_run(first) == original and sv.result(first) == original_result
    assert _store_rows("SELECT count(*) FROM runs")[0][0] == 2
    claim = registry.read_claim(claude_writer.worktree)
    assert (claim.run_id if claim is not None else None) == (
        follow_up if role == "writer" else None
    )
    assert len(paper_slices) == 2 and all(scope.closed for scope in paper_slices)
    assert sv.close(follow_up).claim_released is True


@pytest.mark.parametrize("proxied", [False, True])
@pytest.mark.parametrize("role", ["writer", "review"])
def test_a_continuation_walled_by_its_subscription_continues_on_the_next_account(
    registry: Registry,
    tmp_path: Path,
    claude_writer: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    paper_slices: list[object],
    role: str,
    proxied: bool,
) -> None:
    """The app's run-card follow-up (`original_run` alone) hit the original
    account's weekly limit: the same turn continues the same native history on
    the provider's next account, with that account's own credential. An account
    the cage cannot serve (its own endpoint) is passed over, never the end."""
    if proxied:
        # A third account, reached through its own endpoint, comes first.
        (tmp_path / "secrets" / "writer-aa.token").write_text(_CLAUDE_TOKEN + "\n")
        routed = supervisor._picked_route_elsewhere
        monkeypatch.setattr(
            supervisor,
            "_picked_route_elsewhere",
            lambda roster, picked: (
                ["ANTHROPIC_BASE_URL"]
                if picked.executed_account == "writer-aa"
                else routed(roster, picked)
            ),
        )
    sv = _sv(registry, tmp_path)
    server = McpServer(supervisor=sv)
    arguments = (
        _claude_write(claude_writer)
        if role == "writer"
        else _claude_review(claude_writer)
    )
    first = _payload(
        _call(server, "nspawn_run_start", {**arguments, "prompt": "origin beacon"})
    )["run_id"]
    origin = sv.status(first, timeout_ms=20_000)
    assert origin.phase is RunPhase.COMPLETED, (origin.failure_reason, origin.warning)
    original_config = _seen_by(claude_writer)["env"]["CLAUDE_CONFIG_DIR"]
    assert sv.close(first).claim_released is True
    original = registry.get_run(first)
    assert original is not None and original.executed_account == "writer-a"
    (claude_writer.stand_in / "walled").write_text(_CLAUDE_TOKEN)

    follow_up = _payload(
        _call(
            server,
            "nspawn_run_start",
            {
                "original_run": first,
                "cwd": str(claude_writer.checkout),
                "prompt": "continue this conversation",
            },
        )
    )["run_id"]

    terminal = sv.status(follow_up, timeout_ms=20_000)
    assert terminal.phase is RunPhase.COMPLETED, (
        terminal.failure_reason,
        terminal.warning,
    )
    assert sv.result(follow_up).final_text == "Remembered original input: origin beacon"
    # Only the account that refused is named: the cage passed over writer-aa.
    assert "refused on writer-a; the turn then ran on writer-b" in (
        terminal.warning or ""
    )
    seen = _seen_by(claude_writer)
    assert seen["env"][child.CLAUDE_OAUTH_TOKEN_ENV] == _CLAUDE_TOKEN[::-1]
    assert seen["env"]["CLAUDE_CONFIG_DIR"] == original_config
    assert seen["argv"][seen["argv"].index("--resume") + 1] == original.thread_id
    resumed = registry.get_run(follow_up)
    assert resumed is not None and resumed.executed_account == "writer-b"
    assert "writer-a" in registry.get_account_walls(int(time.time()))
    # One slice for the turn, emptied between its two attempts.
    assert paper_slices[1:] == [_PaperSlice.opened[-1]] * 2
    assert _PaperSlice.opened[-1].drained == 1
    assert sv.close(follow_up).claim_released is True
    # The conversation stays one: its attempts span both accounts, and the next
    # follow-up resumes it on the account that last served it.
    session = sv.status(first).session
    assert session is not None and session.run_ids == (first, follow_up)
    assert session.can_prompt is True
    third = _payload(
        _call(
            server,
            "nspawn_run_start",
            {
                "original_run": follow_up,
                "cwd": str(claude_writer.checkout),
                "prompt": "and once more",
            },
        )
    )["run_id"]
    done = sv.status(third, timeout_ms=20_000)
    assert done.phase is RunPhase.COMPLETED, (done.failure_reason, done.warning)
    resumed = registry.get_run(third)
    assert resumed is not None and resumed.executed_account == "writer-b"
    assert sv.close(third).claim_released is True


def test_a_review_its_organization_refuses_continues_on_the_next_account(
    registry: Registry,
    tmp_path: Path,
    claude_writer: SimpleNamespace,
    paper_slices: list[object],
) -> None:
    """The organization refuses the first account for Claude Code: the caged review is walled off that
    account for a while, never for good, and the same turn goes on
    in the same native history on the route's next account."""
    (claude_writer.stand_in / "refused").write_text(_CLAUDE_TOKEN)
    sv = _sv(registry, tmp_path)
    run_id = _payload(
        _call(
            McpServer(supervisor=sv),
            "nspawn_run_start",
            _claude_review(claude_writer, provider="writer"),
        )
    )["run_id"]
    terminal = sv.status(run_id, timeout_ms=20_000)
    assert terminal.phase is RunPhase.COMPLETED, (
        terminal.failure_reason,
        terminal.warning,
    )
    assert terminal.executed_account == "writer-b"
    assert "refused on writer-a; the turn then ran on writer-b" in (
        terminal.warning or ""
    )
    assert sv.result(run_id).final_text == "Remembered original input: review it"
    seen = _seen_by(claude_writer)
    assert seen["env"][child.CLAUDE_OAUTH_TOKEN_ENV] == _CLAUDE_TOKEN[::-1]
    row = registry.get_run(run_id)
    assert row is not None
    assert seen["argv"][seen["argv"].index("--resume") + 1] == row.thread_id
    now = int(time.time())
    walls = registry.get_account_walls(now)
    assert "writer-a" in walls and "writer-b" not in walls
    assert "writer-a" not in registry.get_account_walls(
        now + supervisor.Supervisor.CREDENTIAL_REJECTED_WALL_S + 1
    )
    assert sv.close(run_id).claim_released is True


def test_a_review_every_account_refuses_names_them_all_and_never_returns(
    registry: Registry,
    tmp_path: Path,
    claude_writer: SimpleNamespace,
    paper_slices: list[object],
) -> None:
    """Both accounts are refused: the run names both, and the review's next
    preference never sends the same work back to an account that refused it."""
    (claude_writer.stand_in / "refused").write_text(
        f"{_CLAUDE_TOKEN}\n{_CLAUDE_TOKEN[::-1]}\n"
    )
    with claude_writer.roster.open("a") as roster:
        roster.write('[preference]\nreview = ["writer", "writer-a"]\n')
    sv = _sv(registry, tmp_path)
    request = _claude_review(claude_writer)
    request.pop("provider")
    run_id = _payload(_call(McpServer(supervisor=sv), "nspawn_run_start", request))[
        "run_id"
    ]
    terminal = sv.status(run_id, timeout_ms=20_000)
    assert terminal.phase is RunPhase.FAILED
    warning = terminal.warning or ""
    assert "refused on writer-a, writer-b" in warning, warning
    assert "the turn then ran on" not in warning, warning
    assert "writer-a (it refused this work earlier)" in warning, warning
    assert len(registry.list_runs()) == 1


def test_a_review_whose_move_finds_no_history_goes_to_its_next_preference(
    registry: Registry,
    tmp_path: Path,
    claude_writer: SimpleNamespace,
    paper_slices: list[object],
) -> None:
    """The CLI kept no history of the refused turn, so the resume on writer-b
    fails with no vendor answer. The run keeps writer-a's 403 as its answer,
    and the review goes on to its next preference, as it did before the move."""
    (claude_writer.stand_in / "refused").write_text(_CLAUDE_TOKEN)
    (claude_writer.stand_in / "forgetful").write_text("")
    with claude_writer.roster.open("a") as roster:
        roster.write('[preference]\nreview = ["writer", "writer-b"]\n')
    sv = _sv(registry, tmp_path)
    request = _claude_review(claude_writer)
    request.pop("provider")
    first_id = _payload(_call(McpServer(supervisor=sv), "nspawn_run_start", request))[
        "run_id"
    ]
    first = sv.status(first_id, timeout_ms=20_000)
    assert first.phase is RunPhase.FAILED
    assert first.executed_account == "writer-a"
    assert first.failure_reason == "VendorApiError (HTTP 403)", first.failure_reason
    warning = first.warning or ""
    assert "the turn could not continue on writer-b" in warning, warning
    successor = re.search(r"continued as run (\S+) on", warning)
    assert successor is not None, warning
    second = sv.status(successor.group(1), timeout_ms=20_000)
    assert second.phase is RunPhase.COMPLETED, (second.failure_reason, second.warning)
    assert second.executed_account == "writer-b"


def test_an_unpinned_writer_its_organization_refuses_is_never_moved(
    registry: Registry,
    tmp_path: Path,
    claude_writer: SimpleNamespace,
    paper_slices: list[object],
) -> None:
    """A writer stays failed on the refused account even when the caller did
    not pin it: a second account would send its prompt to the checkout twice."""
    (claude_writer.stand_in / "refused").write_text(_CLAUDE_TOKEN)
    sv = _sv(registry, tmp_path)
    run_id = _payload(
        _call(
            McpServer(supervisor=sv),
            "nspawn_run_start",
            _claude_write(claude_writer, provider="writer"),
        )
    )["run_id"]
    terminal = sv.status(run_id, timeout_ms=20_000)
    assert terminal.phase is RunPhase.FAILED
    assert terminal.executed_account == "writer-a"
    assert len(list(claude_writer.stand_in.glob("cgroup-*"))) == 1
    assert not (claude_writer.checkout / "written.txt").exists()


@pytest.mark.parametrize(
    "change",
    ["account", "cwd", "read_paths", "system_prompt", "read_only", "class", "deadline"],
)
def test_a_verified_claude_writer_refuses_changed_origin_before_a_child(
    registry: Registry,
    tmp_path: Path,
    claude_writer: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    paper_slices: list[object],
    change: str,
) -> None:
    sv = _sv(registry, tmp_path)
    server = McpServer(supervisor=sv)
    first = _payload(_call(server, "nspawn_run_start", _claude_write(claude_writer)))[
        "run_id"
    ]
    assert sv.status(first, timeout_ms=20_000).phase is RunPhase.COMPLETED
    assert sv.close(first).claim_released is True
    original = registry.get_run(first)
    assert original is not None and original.thread_id is not None
    changes: dict[str, Any] = {"session_id": original.thread_id}
    if change == "account":
        changes["provider"] = "writer-b"
    elif change == "cwd":
        other = tmp_path / "other"
        (other / ".git").mkdir(parents=True)
        changes["cwd"] = str(other)
    elif change == "read_paths":
        changes["read_paths"] = [str(tmp_path)]
    elif change == "system_prompt":
        changes["system_prompt"] = "Different privileged instructions."
    elif change == "class":
        changes["class"] = "review"
    elif change == "deadline":
        changes["timeout_ms"] = 42_000
    else:
        changes.update(native_write=False, read_only=True)
    monkeypatch.setattr(child, "spawn_child", _forbidden)
    response = _call(
        server, "nspawn_run_start", _claude_write(claude_writer, **changes)
    )
    assert response["error"]["data"]["type"] == "InvalidRunRequest", response
    assert registry.get_run(first) == original
    assert _store_rows("SELECT count(*) FROM runs")[0][0] == 1
    assert _store_rows("SELECT count(*) FROM claims")[0][0] == 0


def test_a_claude_writer_that_never_spawns_leaves_no_scratch(
    registry: Registry,
    tmp_path: Path,
    claude_writer: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    paper_slices: list[object],
) -> None:
    made: list[Path] = []
    make = claude_restricted.make_scratch

    def recorded() -> Path:
        made.append(make())
        return made[-1]

    def refused(*_: Any, **__: Any) -> Any:
        raise OSError("no spawn")

    monkeypatch.setattr(claude_restricted, "make_scratch", recorded)
    monkeypatch.setattr(child, "spawn_child", refused)
    sv = _sv(registry, tmp_path)
    run_id = _payload(
        _call(
            McpServer(supervisor=sv), "nspawn_run_start", _claude_write(claude_writer)
        )
    )["run_id"]
    assert sv.status(run_id, timeout_ms=20_000).phase is RunPhase.FAILED
    assert len(made) == 1 and not made[0].exists()


@pytest.mark.needs("systemd_user", "cgroup")
def test_close_of_a_live_claude_writer_kills_its_setsid_descendant(
    registry: Registry, tmp_path: Path, claude_writer: SimpleNamespace
) -> None:
    sv = _sv(registry, tmp_path)
    server = McpServer(supervisor=sv)
    run_id = _payload(
        _call(server, "nspawn_run_start", _claude_write(claude_writer, prompt="hold"))
    )["run_id"]
    try:
        receipt = claude_writer.stand_in / "escaped.pid"
        _wait_for(receipt.exists, 20, "the writer's setsid descendant")
        escaped = int(receipt.read_text())
        (scope,) = _writer_scopes(run_id)

        closed = _payload(_call(server, "nspawn_run_close", {"run_id": run_id}))

        assert (closed["phase"], closed["claim_released"]) == ("closed", True)
        assert not Path(scope["cgroup"]).exists()
        _wait_for(lambda: _gone(escaped), 5, "the escaped descendant to die")
        _wait_for(lambda: run_id not in sv._writers, 10, "the worker to finish")
    finally:
        if (writer := sv._writers.get(run_id)) is not None:
            with contextlib.suppress(ControllerRefusal):
                writer.close()


@pytest.mark.needs("systemd_user", "cgroup")
def test_a_rejected_claude_writer_fails_on_its_account_and_is_never_moved(
    registry: Registry, tmp_path: Path, claude_writer: SimpleNamespace
) -> None:
    """No credential failover for a writer: it fails where it was pinned."""
    sv = _sv(registry, tmp_path)
    run_id = _payload(
        _call(
            McpServer(supervisor=sv),
            "nspawn_run_start",
            _claude_write(claude_writer, prompt="reject"),
        )
    )["run_id"]
    assert sv.status(run_id, timeout_ms=20_000).phase is RunPhase.FAILED
    assert len(list(claude_writer.stand_in.glob("cgroup-*"))) == 1
    row = registry.get_run(run_id)
    assert row is not None and row.executed_account == "writer-a"


def _claude_review(route: SimpleNamespace, **changes: Any) -> dict[str, Any]:
    return {
        "prompt": "review it",
        "cwd": str(route.checkout),
        "provider": "writer-a",
        "class": "review",
        "read_only": True,
        **changes,
    }


def test_a_claude_review_runs_caged_in_its_own_slice_and_claims_nothing(
    registry: Registry,
    tmp_path: Path,
    claude_writer: SimpleNamespace,
    paper_slices: list[object],
) -> None:
    """Custody of a read-only Claude run: the same cage, writing only its own
    scratch, in a slice of its own that is emptied at its end."""
    sv = _sv(registry, tmp_path)
    run_id = _payload(
        _call(
            McpServer(supervisor=sv), "nspawn_run_start", _claude_review(claude_writer)
        )
    )["run_id"]

    assert sv.status(run_id, timeout_ms=20_000).phase is RunPhase.COMPLETED
    (paper,) = _PaperSlice.opened
    assert paper_slices == [paper] and paper.closed
    seen = _seen_by(claude_writer)
    argv: list[str] = seen["argv"]
    assert "--restricted" in argv
    assert argv[argv.index("--tools") + 1] == "Bash"
    cage = json.loads(Path(argv[argv.index("--settings") + 1]).read_text())
    assert cage["sandbox"]["filesystem"]["allowWrite"] == [seen["env"]["TMPDIR"]]
    # It works in its scratch and is told, first, where the candidate is.
    assert seen["cwd"] == str(Path(seen["env"]["TMPDIR"]) / "work")
    assert str(claude_writer.checkout) in seen["prompt"].split("\n\n")[0]
    assert seen["prompt"].endswith("review it")
    assert registry.read_claim(claude_writer.worktree) is None


def test_a_claude_review_on_a_proxied_route_keeps_its_uncaged_path(
    registry: Registry,
    tmp_path: Path,
    claude_writer: SimpleNamespace,
    paper_slices: list[object],
) -> None:
    """A proxied route cannot be caged, and says so: it lists no custody."""
    claude_writer.roster.write_text(
        claude_writer.roster.read_text().replace(
            'models = ["opus"]\n',
            'models = ["opus"]\n[provider.env]\n'
            'ANTHROPIC_BASE_URL = "https://proxy.invalid"\n',
        )
    )
    sv = _sv(registry, tmp_path)
    run_id = _payload(
        _call(
            McpServer(supervisor=sv), "nspawn_run_start", _claude_review(claude_writer)
        )
    )["run_id"]

    status = sv.status(run_id, timeout_ms=20_000)
    assert status.phase is RunPhase.COMPLETED, (status.failure_reason, status.warning)
    # Spawned once, into no slice, and without the pinned cage.
    assert paper_slices == [None] and _PaperSlice.opened == []
    assert "--settings" not in _seen_by(claude_writer)["argv"]


def _caged(route: SimpleNamespace, paper_slices: list[object]) -> bool:
    """Whether the one spawn ran in its own slice with the pinned cage."""
    (slice_,) = paper_slices
    return slice_ is not None and "--settings" in _seen_by(route)["argv"]


def test_the_review_verb_runs_a_claude_review_caged(
    registry: Registry,
    tmp_path: Path,
    claude_writer: SimpleNamespace,
    paper_slices: list[object],
) -> None:
    """`nspawn_review_start` is the verb the custody is for, not only a
    read-only `nspawn_run_start` that happens to declare `review`."""
    sv = _sv(registry, tmp_path)
    run_id = _payload(
        _call(
            McpServer(supervisor=sv),
            "nspawn_review_start",
            {
                "branch": "lane",
                "cwd": str(claude_writer.checkout),
                "provider": "writer-a",
                "prompt": "review it",
            },
        )
    )["run_id"]

    assert sv.status(run_id, timeout_ms=20_000).phase is RunPhase.COMPLETED
    assert _caged(claude_writer, paper_slices)
    assert registry.read_claim(claude_writer.worktree) is None


#: Classes where `mechanical_fanout` is cheaper than both others on every axis,
#: and `review` cheaper than `judgment` (`verification` never lowers: it drops
#: the producer from the pool, which is not a price either way). A failed
#: observation does not lower these pinned dispatches, so the lowering class
#: is one System1 names.
_CAGE_CLASSES = (
    "[classes]\n"
    'judgment = { effort = "high", ceiling = "max" }\n'
    'review = { effort = "medium", ceiling = "high" }\n'
    'unclassified = { effort = "low", ceiling = "medium" }\n'
    'mechanical_fanout = { effort = "low", ceiling = "medium" }\n'
)


@pytest.mark.parametrize(
    ("declared", "observed", "caged"),
    [("review", "mechanical_fanout", True), ("judgment", "review", False)],
    ids=["declared-review-lowered", "declared-judgment-read-as-review"],
)
def test_the_cage_follows_the_class_the_caller_declared(
    registry: Registry,
    tmp_path: Path,
    claude_writer: SimpleNamespace,
    paper_slices: list[object],
    monkeypatch: pytest.MonkeyPatch,
    declared: str,
    observed: str,
    caged: bool,
) -> None:
    """System1 may lower a run's class, never its confinement: a declared review
    stays caged when read as something cheaper, and a run that did not ask for
    a review is not caged because System1 read it as one."""
    claude_writer.roster.write_text(claude_writer.roster.read_text() + _CAGE_CLASSES)
    monkeypatch.setattr(system1, "ask", answering(observed))
    sv = _sv(registry, tmp_path)
    run_id = _payload(
        _call(
            McpServer(supervisor=sv),
            "nspawn_run_start",
            {**_claude_review(claude_writer), "class": declared},
        )
    )["run_id"]

    status = sv.status(run_id, timeout_ms=20_000)
    assert status.phase is RunPhase.COMPLETED, (status.failure_reason, status.warning)
    (row,) = registry.export_class_observations()
    assert row.observation.effective == observed
    assert _caged(claude_writer, paper_slices) is caged


def test_a_caged_claude_review_refuses_to_resume_before_it_spawns(
    registry: Registry,
    tmp_path: Path,
    claude_writer: SimpleNamespace,
    paper_slices: list[object],
) -> None:
    """The cage starts a fresh session: a review asked to resume one is refused
    by name, never quietly run as a new conversation."""
    answer = _call(
        McpServer(supervisor=_sv(registry, tmp_path)),
        "nspawn_run_start",
        {**_claude_review(claude_writer), "session_id": str(uuid.uuid4())},
    )
    assert answer.get("error", {}).get("data", {}).get("type") == "InvalidRunRequest", (
        answer
    )
    assert "caged Claude review cannot resume" in answer["error"]["message"]
    assert paper_slices == []


def test_a_claude_writer_whose_sandbox_did_not_start_fails_naming_it(
    registry: Registry,
    tmp_path: Path,
    claude_writer: SimpleNamespace,
    paper_slices: list[object],
) -> None:
    """The client refuses every command when its sandbox cannot start, and the
    turn still ends "completed": a writer that could write nothing must fail."""
    sv = _sv(registry, tmp_path)
    run_id = _payload(
        _call(
            McpServer(supervisor=sv),
            "nspawn_run_start",
            _claude_write(claude_writer, prompt="sandbox-down"),
        )
    )["run_id"]
    status = sv.status(run_id, timeout_ms=20_000)
    assert status.phase is RunPhase.FAILED
    assert "RestrictedLaunchRefused" in (status.failure_reason or "")
    (paper,) = _PaperSlice.opened
    assert paper_slices == [paper] and paper.closed


def test_a_claude_writer_that_only_reads_the_message_completes(
    registry: Registry,
    tmp_path: Path,
    claude_writer: SimpleNamespace,
    paper_slices: list[object],
) -> None:
    """Only the refused tool call counts, never a file that quotes it."""
    sv = _sv(registry, tmp_path)
    run_id = _payload(
        _call(
            McpServer(supervisor=sv),
            "nspawn_run_start",
            _claude_write(claude_writer, prompt="quote-it"),
        )
    )["run_id"]
    assert sv.status(run_id, timeout_ms=20_000).phase is RunPhase.COMPLETED


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("ANTHROPIC_BASE_URL", "https://proxy.invalid"),
        # A proxy's own credential, even without its URL, would reach the vendor.
        ("ANTHROPIC_AUTH_TOKEN", "proxy-credential"),
        ("CLAUDE_CODE_USE_BEDROCK", "1"),
    ],
)
def test_a_claude_writer_on_a_proxied_route_is_refused_before_any_child(
    registry: Registry,
    tmp_path: Path,
    claude_writer: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
) -> None:
    """The cage's closed env carries no endpoint: a route that needs one refuses."""
    claude_writer.roster.write_text(
        claude_writer.roster.read_text().replace(
            'models = ["opus"]\n',
            f'models = ["opus"]\n[provider.env]\n{name} = "{value}"\n',
        )
    )
    monkeypatch.setattr(child, "spawn_child", _forbidden)
    sv = _sv(registry, tmp_path)
    answer = _call(
        McpServer(supervisor=sv), "nspawn_run_start", _claude_write(claude_writer)
    )
    assert answer is not None
    assert name in answer["error"]["message"]
    assert answer["error"]["data"]["type"] == "InvalidRunRequest"
    assert _store_rows("SELECT run_id FROM runs") == []
