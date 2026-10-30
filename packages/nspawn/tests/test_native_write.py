"""Native write mode: admission, the writer's profile, its slice custody and key lookup.

A native write run is one Codex app-server turn that may write only inside its
`cwd`. These infrastructure fixtures use verification scratch writes. The
permission profile and run-specific systemd slice fence the writes. The slice
must be emptied before the worktree claim may go.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import select
import shutil
import stat
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest
from class_dispatch import (
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
    driver,
    native,
    picker,
    supervisor,
    system1,
)
from nspawn.app_server import AppServerTurn
from nspawn.custody import AggregateScope, ControllerRefusal, writer_unit
from nspawn.mcp import McpServer
from nspawn.owner_roster import NativeAccount, Provider, Roster
from nspawn.process_identity import capture_process_identity
from nspawn.redact import SecretNeedles
from nspawn.registry import POISONED_LEASE_UNTIL, Registry, RegistryError
from nspawn.rows import (
    Backend,
    NativeSessionContext,
    RunPhase,
    RunRow,
    SessionProfile,
)
from nspawn.supervisor import InvalidRunRequest, RunCloseRejected, Supervisor

# Custody fixtures are verification writers; implementation cases supply RED explicitly.
DECLARED_CLASS = "verification"

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
OBSERVED_WRITER_SANDBOX = {"type": "dangerFullAccess"}

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
        writer = params.get('sandbox') == 'danger-full-access'
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
        supervisor, "read_roster", lambda *a, **k: pytest.fail("read the roster")
    )
    sv = _sv(registry, tmp_path)
    try:
        with pytest.raises(supervisor.InvalidRunRequest, match="native_write"):
            sv.execute("implement", str(tmp_path))
    finally:
        sv.shutdown()
    assert [p.name for p in (tmp_path / "runs").iterdir()] == []


def test_native_write_with_read_paths_is_refused_at_the_mcp_door(
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
    monkeypatch.setattr(McpServer, "_launch", _forbidden)
    response = _call(
        McpServer(supervisor=sv),
        "nspawn_run_start",
        _write(writer_route, read_paths=[str(tmp_path)]),
    )

    assert "error" in response, response
    assert response["error"]["data"]["type"] == "InvalidRunRequest"
    message = response["error"]["message"]
    assert "drop read_paths" in message
    assert "reads the host" in message
    assert "read_only" in message
    assert registry.list_runs() == ()
    assert registry.read_claim(writer_route.worktree) is None
    assert writer_route.prepared == []
    assert started == []
    assert list((tmp_path / "runs").iterdir()) == []


@pytest.mark.parametrize("read_only", [False, True])
@pytest.mark.parametrize("empty", [False, True])
def test_native_write_with_read_paths_is_refused_before_the_roster(
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    read_only: bool,
    empty: bool,
) -> None:
    sv = _sv(registry, tmp_path)
    monkeypatch.setattr(supervisor, "read_roster", _forbidden)
    monkeypatch.setattr(native, "prepare_native", _forbidden)
    monkeypatch.setattr(child, "spawn_child", _forbidden)
    with pytest.raises(InvalidRunRequest, match="drop read_paths.*reads the host"):
        sv.execute(
            "change it",
            str(tmp_path),
            native_write=True,
            read_only=read_only,
            read_paths=[] if empty else [str(tmp_path)],
        )
    assert registry.list_runs() == ()
    assert _store_rows("SELECT count(*) FROM claims")[0][0] == 0
    assert list((tmp_path / "runs").iterdir()) == []


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
        _write(writer_route, provider="codex", **{"class": "verification"}),
    )

    row = registry.get_run(_payload(answer)["run_id"])
    assert row is not None and row.executed_provider == "codex"
    assert unsure_classifier.classes == ["verification"]
    assert unsure_classifier.asked == []


def test_unsupported_writer_class_refuses_before_strength_ranking(
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
    assert "mechanical_fanout" in message and "unsupported" in message, answer
    assert unsure_classifier.classes == []
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
def test_native_write_thread_keeps_the_catalog_without_a_writer_profile(
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
        },
    )
    status = client.terminal(accepted["run_id"])

    assert status["phase"] == "completed", status
    params = json.loads((cwd / "thread-request.json").read_text())
    assert params["sandbox"] == "danger-full-access"
    assert "permissions" not in params
    settings = params["config"]
    assert "permissions" not in settings and "default_permissions" not in settings
    assert not any(key.startswith("shell_environment_policy") for key in settings)
    servers = settings["mcp_servers"]
    assert servers["nmemory"]["args"] == ["--project", "pynott", "--read-only"]
    assert servers["nmemory"]["enabled_tools"] == ["nmemory_context_show"]
    assert servers["nspawn"]["enabled"] is False
    assert settings["plugins"]["extra@runtime"] == {
        "mcp_servers": {"runtime_extra": {"enabled": False}}
    }
    assert settings["features.apps"] is False
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
        {"type": "readOnly"},
        {"type": "workspaceWrite"},
    ],
    ids=["readonly", "workspace"],
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
        with pytest.MonkeyPatch.context() as patch:
            identity = capture_process_identity(os.getpid())
            patch.setattr(
                "nspawn.registry.capture_process_identity",
                lambda pid: replace(identity, start_ticks=identity.start_ticks + 1),
            )
            dead.open(now=1000)
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
            raise ControllerRefusal(
                f"aggregate systemd operation unavailable: systemctl stop "
                f"{self.unit}: no answer within 5 s"
            )
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
        # The reason names the systemd call that failed, and why.
        assert f"systemctl stop {writer_unit(run_id)}: no answer within 5 s" in (
            status.failure_reason or ""
        ), status.failure_reason
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


class _BeforeTheTurn(Exception):
    """Raised where the turn would start: no model is asked, no quota spent."""


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
# The driver speaks stream-json: the prompt is the first user message, and stdin
# stays open until the child gave its result.
first = json.loads(sys.stdin.readline())
content = first["message"]["content"]
prompt = content if isinstance(content, str) else "".join(
    block.get("text", "") for block in content if block.get("type") == "text")
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
    if len(roots) >= 2:  # the clone, the scratch, and the caches present
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
    elif any(arg.startswith("--resume=") for arg in sys.argv):
        thread = next(arg.split("=", 1)[1] for arg in sys.argv if arg.startswith("--resume="))
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
    if "--resume" in sys.argv:
        session = sys.argv[sys.argv.index("--resume") + 1]
    elif any(arg.startswith("--resume=") for arg in sys.argv):
        session = next(arg.split("=", 1)[1] for arg in sys.argv if arg.startswith("--resume="))
    else:
        session = next(
            arg.split("=", 1)[1] for arg in sys.argv if arg.startswith("--session-id=")
        )
        # The turn is in the native history before the vendor answers, so the
        # next account can continue it.
        (Path(os.environ["CLAUDE_CONFIG_DIR"]) / "peer-history.json").write_text(
            json.dumps({"thread": session, "prompt": prompt}))
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
    # The cage passes `--resume <id>`, the uncaged child `--resume=<id>`.
    resumed = [arg.split("=", 1)[1] for arg in sys.argv if arg.startswith("--resume=")]
    if "--resume" in sys.argv:
        resumed.append(sys.argv[sys.argv.index("--resume") + 1])
    if resumed:
        original = json.loads(native_history.read_text())
        assert [original["thread"]] == resumed
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
print(json.dumps({"type": "result", "is_error": prompt == "fail after history",
                  "terminal_reason": "error" if prompt == "fail after history" else "completed",
                  "result": final, "usage": {"input_tokens": 1, "output_tokens": 1}}), flush=True)
sys.stdin.read()
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
    # Every child runs as an agent: `worker` when its start names none.
    (plugin / "agents").mkdir()
    (plugin / "agents" / "worker.md").write_text("Do the brief.\n")
    # Valid for the uncaged read-only path; the caged runs load no plugin.
    (plugin / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"nmemory": {"command": "nmemory"}}})
    )
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    secrets.chmod(0o700)  # whatever the umask
    (secrets / "writer-a.token").write_text(_CLAUDE_TOKEN + "\n")
    (secrets / "writer-a.token").chmod(0o600)  # whatever the umask
    (secrets / "writer-b.token").write_text(_CLAUDE_TOKEN[::-1] + "\n")
    (secrets / "writer-b.token").chmod(0o600)  # whatever the umask
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
    config = tmp_path / "uncaged-claude"
    config.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
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


def test_claude_implements_a_native_writer_and_a_caged_review() -> None:
    assert supervisor.CAPABILITIES["claude"] == ("custody", "resume", "review", "write")


@pytest.mark.parametrize("endpoint", ["https://proxy.invalid", "http://1.1.1.1:8080"])
def test_a_claude_writer_keeps_its_declared_public_endpoint(
    registry: Registry,
    tmp_path: Path,
    claude_writer: SimpleNamespace,
    paper_slices: list[object],
    endpoint: str,
) -> None:
    claude_writer.roster.write_text(
        claude_writer.roster.read_text().replace(
            'model = "opus"',
            f'env = {{ ANTHROPIC_BASE_URL = {json.dumps(endpoint)} }}\nmodel = "opus"',
            1,
        )
    )
    sv = _sv(registry, tmp_path)
    run_id = _payload(
        _call(
            McpServer(supervisor=sv), "nspawn_run_start", _claude_write(claude_writer)
        )
    )["run_id"]
    status = sv.status(run_id, timeout_ms=20_000)
    assert status.phase is RunPhase.COMPLETED, (status.failure_reason, status.warning)
    assert _seen_by(claude_writer)["env"]["ANTHROPIC_BASE_URL"] == endpoint
    assert sv.close(run_id).claim_released is True


@pytest.mark.needs("systemd_user", "cgroup")
def test_a_claude_writer_runs_uncaged_in_its_own_slice(
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
    assert argv[:1] == ["-p"] and "--restricted" not in argv
    assert "--settings" not in argv and "--plugin-dir" in argv
    env: dict[str, str] = seen["env"]
    assert env[child.CLAUDE_OAUTH_TOKEN_ENV] == _CLAUDE_TOKEN
    assert env[child.RUN_ID_ENV] == run_id
    assert seen["cwd"] == str(claude_writer.checkout)
    assert seen["prompt"] == "change it"
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

    def command(self, argv: list[str]) -> list[str]:
        return list(argv)

    def environment(self, target: dict[str, str] | None) -> dict[str, str]:
        return dict(os.environ if target is None else target)

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


def test_the_claude_writer_call_site_launches_the_native_client_into_its_slice(
    registry: Registry,
    tmp_path: Path,
    claude_writer: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    paper_slices: list[object],
) -> None:
    """The native writer keeps inherited settings and its owning slice."""
    monkeypatch.setenv("CANARY_ENV", "inherited-canary")
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
    assert "--restricted" not in argv and "--settings" not in argv
    assert "--plugin-dir" in argv
    assert seen["env"]["CANARY_ENV"] == "inherited-canary"
    assert seen["cwd"] == str(claude_writer.checkout)
    assert seen["prompt"] == "change it"


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
    assert f"--resume={original.thread_id}" in seen["argv"]
    if role == "review":
        assert seen["argv"][seen["argv"].index("--permission-mode") + 1] == "dontAsk"
    else:
        assert "--restricted" not in seen["argv"]
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
    assert len(_PaperSlice.opened) == 2
    if role == "writer":
        assert all(scope.closed for scope in _PaperSlice.opened)
    assert sv.close(follow_up).claim_released is True


@pytest.mark.parametrize("reference", ["thread", "run"])
@pytest.mark.parametrize("phase", [RunPhase.COMPLETED, RunPhase.FAILED])
@pytest.mark.parametrize("role", ["writer", "review"])
def test_a_terminal_origin_resumes_without_a_manual_close(
    registry: Registry,
    tmp_path: Path,
    claude_writer: SimpleNamespace,
    paper_slices: list[object],
    reference: str,
    phase: RunPhase,
    role: str,
) -> None:
    sv = _sv(registry, tmp_path)
    server = McpServer(supervisor=sv)
    arguments = (
        _claude_write(claude_writer)
        if role == "writer"
        else _claude_review(claude_writer)
    )
    docs = tmp_path / "docs"
    docs.mkdir()
    if role == "review":
        arguments["read_paths"] = [str(docs)]
    origin_prompt = (
        "fail after history" if phase is RunPhase.FAILED else "origin beacon"
    )
    first = _payload(
        _call(server, "nspawn_run_start", {**arguments, "prompt": origin_prompt})
    )["run_id"]
    origin = sv.status(first, timeout_ms=20_000)
    assert origin.phase is phase, origin.failure_reason
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
        request.pop("native_write", None)
        request.pop("read_only", None)
    response = _call(server, "nspawn_run_start", request)
    assert "error" not in response, response
    follow_up = _payload(response)["run_id"]
    terminal = sv.status(follow_up, timeout_ms=20_000)
    assert terminal.phase is RunPhase.COMPLETED, terminal.failure_reason
    assert (
        sv.result(follow_up).final_text == "Remembered original input: " + origin_prompt
    )
    assert registry.get_run(first).phase is RunPhase.CLOSED
    assert sv.result(first) == replace(original_result, phase=RunPhase.CLOSED)
    claim = registry.read_claim(claude_writer.worktree)
    assert (None if claim is None else claim.run_id) == (
        follow_up if role == "writer" else None
    )
    assert len(_PaperSlice.opened) == 2
    if role == "writer":
        assert all(scope.closed for scope in _PaperSlice.opened)
    assert sv.close(follow_up).claim_released is True


@pytest.mark.parametrize("reference", ["thread", "run"])
@pytest.mark.parametrize("door", ["mcp", "supervisor"])
def test_a_writer_continuation_refuses_read_paths_before_it_changes_the_origin(
    registry: Registry,
    tmp_path: Path,
    claude_writer: SimpleNamespace,
    paper_slices: list[object],
    monkeypatch: pytest.MonkeyPatch,
    reference: str,
    door: str,
) -> None:
    sv = _sv(registry, tmp_path)
    server = McpServer(supervisor=sv)
    first = _payload(_call(server, "nspawn_run_start", _claude_write(claude_writer)))[
        "run_id"
    ]
    assert sv.status(first, timeout_ms=20_000).phase is RunPhase.COMPLETED
    original = registry.get_run(first)
    assert original is not None and original.thread_id is not None
    claim = registry.read_claim(claude_writer.worktree)
    request = _claude_write(claude_writer, read_paths=[str(tmp_path)])
    if reference == "thread":
        request["session_id"] = original.thread_id
    else:
        request["original_run"] = first
        del request["native_write"]
    monkeypatch.setattr(supervisor, "read_roster", _forbidden)
    monkeypatch.setattr(child, "spawn_child", _forbidden)
    monkeypatch.setattr(Supervisor, "_close_resume_origin", _forbidden)

    if door == "mcp":
        response = _call(server, "nspawn_run_start", request)
        assert response["error"]["data"]["type"] == "InvalidRunRequest", response
        assert "drop read_paths" in response["error"]["message"]
        assert "read_only" in response["error"]["message"]
    else:
        request["execution_class"] = request.pop("class")
        with pytest.raises(InvalidRunRequest, match="drop read_paths"):
            sv.execute(**request)
    assert registry.get_run(first) == original
    assert registry.read_claim(claude_writer.worktree) == claim
    assert _store_rows("SELECT count(*) FROM runs")[0][0] == 1
    assert len(paper_slices) == 1


def test_a_resume_refusal_names_every_changed_profile_field_without_prompt_text(
    registry: Registry,
    tmp_path: Path,
    claude_writer: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    paper_slices: list[object],
) -> None:
    sv = _sv(registry, tmp_path)
    first = _payload(
        _call(
            McpServer(supervisor=sv), "nspawn_run_start", _claude_write(claude_writer)
        )
    )["run_id"]
    assert sv.status(first, timeout_ms=20_000).phase is RunPhase.COMPLETED
    original = registry.get_run(first)
    profile = registry.session_profile(first)
    assert original is not None and profile is not None
    monkeypatch.setattr(child, "spawn_child", _forbidden)
    requested_home = tmp_path / "other-home"
    monkeypatch.setattr(Path, "home", lambda: requested_home)
    private_prompt = "Private privileged instructions that must not leave the request."
    with pytest.raises(supervisor.InvalidRunRequest) as refused:
        sv.execute(
            "continue",
            str(tmp_path),
            original_run=first,
            provider="writer-b",
            model="other-model",
            execution_class="review",
            timeout_ms=42_000,
            system_prompt=private_prompt,
            read_paths=[str(tmp_path)],
            native_write=False,
            read_only=True,
        )
    message = str(refused.value)
    fields = message.split("changed fields: ", 1)[1].split(".", 1)[0].split(", ")
    assert set(fields) == {
        "cwd",
        "provider",
        "model",
        "execution_class",
        "timeout_ms",
        "read_paths",
        "native_write",
        "read_only",
        "home",
        "system_prompt",
    }
    assert "start a new run" in message
    assert "repeat the recorded profile" in message
    for value in (
        str(claude_writer.checkout),
        str(tmp_path),
        str(requested_home),
        "writer-a",
        "writer-b",
        "other-model",
        private_prompt,
    ):
        assert value not in message
    assert registry.get_run(first) == original
    assert _store_rows("SELECT count(*) FROM runs")[0][0] == 1


@pytest.mark.parametrize("obstruction", ["live", "slice", "phase", "survivor", "claim"])
def test_a_resume_keeps_the_origin_claim_without_close_proof(
    registry: Registry,
    tmp_path: Path,
    claude_writer: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    paper_slices: list[object],
    obstruction: str,
) -> None:
    sv = _sv(registry, tmp_path)
    server = McpServer(supervisor=sv)
    first = _payload(_call(server, "nspawn_run_start", _claude_write(claude_writer)))[
        "run_id"
    ]
    assert sv.status(first, timeout_ms=20_000).phase is RunPhase.COMPLETED
    original = registry.get_run(first)
    assert original is not None and original.process_identity is not None
    claim = registry.read_claim(claude_writer.worktree)
    assert claim is not None and claim.run_id == first
    if obstruction == "live":
        monkeypatch.setattr(
            Registry,
            "inference_origin",
            lambda self, thread: replace(original, phase=RunPhase.RUNNING),
        )
        monkeypatch.setattr(sv, "close", _forbidden)
        refusal = "DuplicateSession"
    elif obstruction == "slice":

        def undrained() -> None:
            raise ControllerRefusal("test slice did not drain")

        monkeypatch.setattr(_PaperSlice, "on_close", undrained)
        refusal = "RunCloseRejected"
    else:
        tree = supervisor.ProcessTreeReadback(
            (original.process_identity,),
            (original.process_identity,) if obstruction == "survivor" else (),
        )
        monkeypatch.setattr(
            sv,
            "_close_locked",
            lambda *args, **kwargs: supervisor.CloseReadback(
                first,
                RunPhase.COMPLETED if obstruction == "phase" else RunPhase.CLOSED,
                tree,
                obstruction != "claim",
            ),
        )
        refusal = "RunCloseRejected"
    monkeypatch.setattr(child, "spawn_child", _forbidden)
    response = _call(
        server,
        "nspawn_run_start",
        _claude_write(claude_writer, original_run=first),
    )
    assert response["error"]["data"]["type"] == refusal, response
    assert registry.get_run(first) == original
    assert registry.read_claim(claude_writer.worktree) == claim
    assert _store_rows("SELECT count(*) FROM runs")[0][0] == 1


@pytest.mark.parametrize("has_identity", [True, False])
def test_an_orphaned_origin_refuses_resume_and_keeps_its_claim(
    registry: Registry,
    tmp_path: Path,
    claude_writer: SimpleNamespace,
    paper_slices: list[object],
    has_identity: bool,
) -> None:
    sv = _sv(registry, tmp_path)
    server = McpServer(supervisor=sv)
    first = _payload(_call(server, "nspawn_run_start", _claude_write(claude_writer)))[
        "run_id"
    ]
    assert sv.status(first, timeout_ms=20_000).phase is RunPhase.COMPLETED
    sql(registry, "UPDATE runs SET phase = 'orphaned' WHERE run_id = ?", (first,))
    if not has_identity:
        sql(
            registry,
            "UPDATE runs SET process_pid = NULL, process_boot_id = NULL, "
            "process_start_ticks = NULL WHERE run_id = ?",
            (first,),
        )
    origin = registry.get_run(first)
    claim = registry.read_claim(claude_writer.worktree)
    assert origin is not None and origin.phase is RunPhase.ORPHANED
    assert (origin.process_identity is not None) is has_identity
    assert claim is not None and claim.run_id == first
    response = _call(
        server, "nspawn_run_start", _claude_write(claude_writer, original_run=first)
    )
    assert response.get("error", {}).get("data", {}).get("type") == "DuplicateSession"
    message = response["error"]["message"]
    assert "orphaned" in message and "nspawn_run_close" in message
    assert registry.get_run(first) == origin
    assert registry.read_claim(claude_writer.worktree) == claim
    assert _store_rows("SELECT count(*) FROM runs")[0][0] == 1


def test_a_foreign_session_refuses_before_disclosing_its_profile(
    registry: Registry,
    tmp_path: Path,
    claude_writer: SimpleNamespace,
    paper_slices: list[object],
) -> None:
    origin_sv = Supervisor(registry, tmp_path / "runs", owner="node:origin")
    first = _payload(
        _call(
            McpServer(supervisor=origin_sv),
            "nspawn_run_start",
            _claude_write(claude_writer, timeout_ms=20_000),
        )
    )["run_id"]
    assert origin_sv.status(first, timeout_ms=20_000).phase is RunPhase.COMPLETED
    origin = registry.get_run(first)
    assert origin is not None and origin.thread_id is not None
    claim = registry.read_claim(claude_writer.worktree)
    caller = Supervisor(registry, tmp_path / "runs", owner="node:caller")
    response = _call(
        McpServer(supervisor=caller),
        "nspawn_run_start",
        _claude_write(
            claude_writer,
            cwd=str(tmp_path),
            provider="writer-b",
            session_id=origin.thread_id,
            timeout_ms=20_000,
        ),
    )
    assert response["error"]["data"]["type"] == "InvalidRunRequest"
    assert response["error"]["message"] == (
        "session_id names no run of this node to resume"
    )
    rendered = json.dumps(response)
    assert str(claude_writer.checkout) not in rendered
    assert origin.executed_account not in rendered
    assert registry.get_run(first) == origin
    assert registry.read_claim(claude_writer.worktree) == claim


def test_an_unregistered_session_secret_reaches_no_refusal_surface(
    registry: Registry, tmp_path: Path
) -> None:
    sv = Supervisor(registry, tmp_path / "runs", owner="node:caller")
    marker = "synthetic-session-id-secret-not-a-real-credential"
    with pytest.raises(InvalidRunRequest) as refused:
        sv.execute(
            "again",
            str(tmp_path),
            session_id=marker,
            read_only=True,
            execution_class=DECLARED_CLASS,
        )
    directory = driver.drivers_dir(tmp_path / "runs")
    before = set(directory.glob("*.ack"))
    response = _call(
        McpServer(supervisor=sv),
        "nspawn_run_start",
        {
            "prompt": "again",
            "cwd": str(tmp_path),
            "session_id": marker,
            "read_only": True,
            "class": DECLARED_CLASS,
        },
    )
    assert response["error"]["data"]["type"] == "InvalidRunRequest"
    (ack_path,) = set(directory.glob("*.ack")) - before
    acknowledgment = ack_path.read_text()
    ack = json.loads(acknowledgment)
    message = str(refused.value)
    assert ack["refused"]["message"] == response["error"]["message"] == message
    leaked = [
        name
        for name, text in (
            ("refusal", message),
            ("acknowledgment", acknowledgment),
            ("MCP result", json.dumps(response)),
        )
        if marker in text
    ]
    assert not leaked
    assert message == "session_id names no run of this node to resume"
    assert _store_rows("SELECT count(*) FROM runs")[0][0] == 0


def test_a_resume_secret_reaches_neither_the_ack_nor_the_mcp_refusal(
    registry: Registry,
    tmp_path: Path,
    claude_writer: SimpleNamespace,
    paper_slices: list[object],
) -> None:
    sv = _sv(registry, tmp_path)
    server = McpServer(supervisor=sv)
    first = _payload(_call(server, "nspawn_run_start", _claude_write(claude_writer)))[
        "run_id"
    ]
    assert sv.status(first, timeout_ms=20_000).phase is RunPhase.COMPLETED
    directory = driver.drivers_dir(tmp_path / "runs")
    before = set(directory.glob("*.ack"))
    synthetic_secret = "synthetic-resume-provider-secret-not-a-real-credential"
    response = _call(
        server,
        "nspawn_run_start",
        _claude_write(claude_writer, original_run=first, provider=synthetic_secret),
    )
    assert response["error"]["data"]["type"] == "InvalidRunRequest"
    (ack_path,) = set(directory.glob("*.ack")) - before
    acknowledgment = ack_path.read_text()
    ack = json.loads(acknowledgment)
    assert ack["refused"]["message"] == response["error"]["message"]
    assert synthetic_secret not in acknowledgment
    assert synthetic_secret not in json.dumps(response)
    message = response["error"]["message"]
    assert "provider" in message
    assert "start a new run" in message
    assert str(claude_writer.checkout) not in acknowledgment
    assert "writer-a" not in acknowledgment


@pytest.fixture
def uncaged_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, claude_models: dict[str, str]
) -> Path:
    """The config dir an uncaged child of `claude_writer` inherits: its
    accounts declare no home. The model catalogue is the fixture's."""
    home = tmp_path / "claude-config"
    home.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    return home


@pytest.mark.parametrize("reference", ["thread", "run"])
def test_a_read_only_claude_run_that_is_not_a_review_resumes_its_history(
    registry: Registry,
    tmp_path: Path,
    claude_writer: SimpleNamespace,
    uncaged_home: Path,
    reference: str,
) -> None:
    """A read-only planning run is not a review: it runs uncaged, on its
    account's own config dir. Its resume continues that history on the same
    account, cwd and confinement, and widens no grant."""
    sv = _sv(registry, tmp_path)
    server = McpServer(supervisor=sv)
    arguments = {
        "prompt": "origin beacon",
        "cwd": str(claude_writer.checkout),
        "provider": "writer-a",
        "class": DECLARED_CLASS,
        "read_only": True,
    }
    first = _payload(_call(server, "nspawn_run_start", arguments))["run_id"]
    origin = sv.status(first, timeout_ms=20_000)
    assert origin.phase is RunPhase.COMPLETED, (origin.failure_reason, origin.warning)
    original_seen = _seen_by(claude_writer)
    original_config = original_seen["env"]["CLAUDE_CONFIG_DIR"]
    assert registry.read_claim(claude_writer.worktree) is None
    assert sv.close(first).claim_released is True
    original = registry.get_run(first)
    assert original is not None and original.thread_id is not None

    request = {
        **(
            {**arguments, "session_id": original.thread_id}
            if reference == "thread"
            else {"original_run": first, "cwd": str(claude_writer.checkout)}
        ),
        "prompt": "continue this conversation",
    }
    follow_up = _payload(_call(server, "nspawn_run_start", request))["run_id"]
    terminal = sv.status(follow_up, timeout_ms=20_000)
    assert terminal.phase is RunPhase.COMPLETED, (
        terminal.failure_reason,
        terminal.warning,
    )
    assert terminal.session is not None and terminal.session.original_run_id == first
    assert sv.result(follow_up).final_text == "Remembered original input: origin beacon"
    seen = _seen_by(claude_writer)
    assert seen["env"]["CLAUDE_CONFIG_DIR"] == original_config
    assert f"--resume={original.thread_id}" in seen["argv"]
    # The same read-only cage as its origin, never the writer's pinned one.
    for argv in (original_seen["argv"], seen["argv"]):
        assert "--restricted" in argv and "--settings" not in argv
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
    assert registry.read_claim(claude_writer.worktree) is None
    # A resume cannot widen the recorded grants.
    with pytest.raises(
        supervisor.InvalidRunRequest, match="cannot change its verified origin"
    ):
        sv.execute(
            "widen it",
            str(claude_writer.checkout),
            session_id=original.thread_id,
            read_only=True,
            read_paths=[str(tmp_path)],
        )


def test_a_read_only_run_on_unnormalized_paths_is_admitted_and_resumes(
    registry: Registry,
    tmp_path: Path,
    claude_writer: SimpleNamespace,
    uncaged_home: Path,
) -> None:
    """A cwd or a read path with a trailing slash was admitted before profiles
    were recorded for it, and still is; its resume names the same trees in any
    spelling, a symlink to the cwd included."""
    docs = tmp_path / "docs"
    docs.mkdir()
    (tmp_path / "alias").symlink_to(claude_writer.checkout)
    sv = _sv(registry, tmp_path)
    server = McpServer(supervisor=sv)
    first = _payload(
        _call(
            server,
            "nspawn_run_start",
            {
                "prompt": "origin beacon",
                "cwd": f"{claude_writer.checkout}/",
                "read_paths": [f"{docs}/"],
                "provider": "writer-a",
                "class": DECLARED_CLASS,
                "read_only": True,
            },
        )
    )["run_id"]
    origin = sv.status(first, timeout_ms=20_000)
    assert origin.phase is RunPhase.COMPLETED, (origin.failure_reason, origin.warning)
    assert sv.close(first).claim_released is True
    session = sv.status(first).session
    assert session is not None and session.can_prompt is True
    original = registry.get_run(first)
    assert original is not None and original.thread_id is not None

    # Each resume spells the recorded trees another way than the origin did.
    for cwd, read_path in (
        (str(tmp_path / "alias"), str(docs)),
        (str(claude_writer.checkout), f"{docs}/"),
    ):
        follow_up = _payload(
            _call(
                server,
                "nspawn_run_start",
                {
                    "prompt": "continue this conversation",
                    "cwd": cwd,
                    "read_paths": [read_path],
                    "session_id": original.thread_id,
                    "provider": "writer-a",
                    "class": DECLARED_CLASS,
                    "read_only": True,
                },
            )
        )["run_id"]
        terminal = sv.status(follow_up, timeout_ms=20_000)
        assert terminal.phase is RunPhase.COMPLETED, (
            terminal.failure_reason,
            terminal.warning,
        )
        assert (
            sv.result(follow_up).final_text
            == "Remembered original input: origin beacon"
        )
        resumed = registry.get_run(follow_up)
        # It runs in the tree the origin resolved, under any spelling.
        assert resumed is not None and resumed.worktree == claude_writer.worktree
        assert sv.close(follow_up).claim_released is True


def test_a_resume_never_follows_a_cwd_link_moved_since_its_origin(
    registry: Registry,
    tmp_path: Path,
    claude_writer: SimpleNamespace,
    uncaged_home: Path,
) -> None:
    """The origin recorded the tree its cwd resolved to. A link re-pointed since
    then names another tree, and the resume is refused before any row."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    link = tmp_path / "link"
    link.symlink_to(claude_writer.checkout)
    sv = _sv(registry, tmp_path)
    first = _payload(
        _call(
            McpServer(supervisor=sv),
            "nspawn_run_start",
            {
                "prompt": "origin beacon",
                "cwd": str(link),
                "provider": "writer-a",
                "class": DECLARED_CLASS,
                "read_only": True,
            },
        )
    )["run_id"]
    origin = sv.status(first, timeout_ms=20_000)
    assert origin.phase is RunPhase.COMPLETED, (origin.failure_reason, origin.warning)
    assert sv.close(first).claim_released is True
    link.unlink()
    link.symlink_to(elsewhere)

    with pytest.raises(
        supervisor.InvalidRunRequest, match="cannot change its verified origin"
    ):
        sv.execute(
            "continue this conversation",
            str(link),
            original_run=first,
            read_only=True,
        )
    assert [row.run_id for row in registry.list_runs()] == [first]


@pytest.mark.parametrize(
    ("where", "name"),
    [
        ("cwd", "notas\u00a02026"),
        ("cwd", "notas\u200b2026"),
        ("read_path", "notas\t2026"),
    ],
    ids=["cwd-nbsp", "cwd-zero-width", "read-path-tab"],
)
def test_a_run_whose_paths_a_context_cannot_record_runs_without_continuation(
    registry: Registry,
    tmp_path: Path,
    claude_writer: SimpleNamespace,
    uncaged_home: Path,
    where: str,
    name: str,
) -> None:
    """A path that is no printable text cannot enter a native context. Such a
    run is admitted and completes as it did before profiles, and offers no
    continuation."""
    tree = tmp_path / name
    tree.mkdir()
    sv = _sv(registry, tmp_path)
    run_id = _payload(
        _call(
            McpServer(supervisor=sv),
            "nspawn_run_start",
            {
                "prompt": "plan it",
                "provider": "writer-a",
                "class": DECLARED_CLASS,
                "read_only": True,
                **(
                    {"cwd": str(tree)}
                    if where == "cwd"
                    else {"cwd": str(claude_writer.checkout), "read_paths": [str(tree)]}
                ),
            },
        )
    )["run_id"]
    status = sv.status(run_id, timeout_ms=20_000)
    assert status.phase is RunPhase.COMPLETED, (status.failure_reason, status.warning)
    assert status.session is None
    assert registry.session_profile(run_id) is None


def test_an_uncaged_run_system1_read_as_review_resumes_uncaged(
    registry: Registry,
    tmp_path: Path,
    claude_writer: SimpleNamespace,
    uncaged_home: Path,
    paper_slices: list[object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A declared judgment that System1 reads as a review runs uncaged and
    records the class it read. Its resume follows the confinement it recorded,
    never that class."""
    claude_writer.roster.write_text(claude_writer.roster.read_text() + _CAGE_CLASSES)
    monkeypatch.setattr(system1, "ask", answering("review"))
    sv = _sv(registry, tmp_path)
    server = McpServer(supervisor=sv)
    first = _payload(
        _call(
            server,
            "nspawn_run_start",
            {
                **_claude_review(claude_writer),
                "class": "judgment",
                "prompt": "origin beacon",
            },
        )
    )["run_id"]
    origin = sv.status(first, timeout_ms=20_000)
    assert origin.phase is RunPhase.COMPLETED, (origin.failure_reason, origin.warning)
    assert sv.close(first).claim_released is True

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
    assert len(paper_slices) == 2 and paper_slices == _PaperSlice.opened
    assert "--settings" not in _seen_by(claude_writer)["argv"]


def test_a_continuation_walled_by_its_subscription_continues_on_the_next_account(
    registry: Registry,
    tmp_path: Path,
    claude_writer: SimpleNamespace,
    paper_slices: list[object],
) -> None:
    """The app's run-card follow-up (`original_run` alone) hit the original
    account's weekly limit: the same turn continues the same native history on
    the provider's next account, with that account's own credential."""
    sv = _sv(registry, tmp_path)
    server = McpServer(supervisor=sv)
    arguments = _claude_write(claude_writer)
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
    assert "refused on writer-a; the turn then ran on writer-b" in (
        terminal.warning or ""
    )
    seen = _seen_by(claude_writer)
    assert seen["env"][child.CLAUDE_OAUTH_TOKEN_ENV] == _CLAUDE_TOKEN[::-1]
    assert seen["env"]["CLAUDE_CONFIG_DIR"] == original_config
    assert f"--resume={original.thread_id}" in seen["argv"]
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
    """The organization refuses the first account for Claude Code: the review is walled off that account for
    a while, never for good, and the same run goes on on the route's
    next account."""
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
    assert "moved off writer/writer-a (HTTP 403 oauth_org_not_allowed)" in (
        terminal.warning or ""
    )
    seen = _seen_by(claude_writer)
    assert seen["env"][child.CLAUDE_OAUTH_TOKEN_ENV] == _CLAUDE_TOKEN[::-1]
    now = int(time.time())
    walls = registry.get_account_walls(now)
    assert "writer-a" in walls and "writer-b" not in walls
    assert "writer-a" not in registry.get_account_walls(
        now + supervisor.Supervisor.CREDENTIAL_REJECTED_WALL_S + 1
    )
    assert sv.close(run_id).claim_released is True


def test_a_caged_review_the_vendor_rate_limited_keeps_the_vendors_reset(
    registry: Registry,
    tmp_path: Path,
    claude_writer: SimpleNamespace,
    paper_slices: list[object],
) -> None:
    """The vendor named when writer-a frees again (its weekly limit, `resetsAt`).
    The caged path walling the account with nSPAWN's default rewrote that row's
    source and cause, and `nspawn_quota_show` reads a wall that is not
    `vendor_reset` as a reason to skip the account's quota. The caged path must
    keep the vendor's wall, as the uncaged path does."""
    (claude_writer.stand_in / "walled").write_text(_CLAUDE_TOKEN)
    sv = _sv(registry, tmp_path)
    run_id = _payload(
        _call(
            McpServer(supervisor=sv),
            "nspawn_run_start",
            _claude_review(claude_writer, provider="writer"),
        )
    )["run_id"]
    status = sv.status(run_id, timeout_ms=20_000)
    assert status.phase is RunPhase.COMPLETED, (status.failure_reason, status.warning)
    assert status.executed_account == "writer-b"

    now = int(time.time())
    (wall,) = [
        row
        for row in registry.get_account_wall_rows(now)
        if row.account_alias == "writer-a"
    ]
    assert wall.source == supervisor.terminal.WallSource.VENDOR_RESET.value
    assert wall.limit_type == "seven_day"
    assert wall.cooling_until > now + supervisor.Supervisor.RATE_LIMIT_WALL_S


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
    assert "credential rejected on every account of writer: writer-a, writer-b" in (
        warning
    ), warning
    assert "the turn then ran on" not in warning, warning
    assert len(registry.list_runs()) == 1


def test_a_review_that_moves_records_the_model_that_runs_on_its_new_account(
    registry: Registry,
    tmp_path: Path,
    claude_writer: SimpleNamespace,
    paper_slices: list[object],
) -> None:
    """writer-a runs opus and refuses; the turn moves to writer-b, which runs
    sonnet, and completes there. The row pairs writer-b with sonnet."""
    claude_writer.roster.write_text(
        claude_writer.roster.read_text().replace(
            'models = ["opus"]\n',
            'models = ["opus", "sonnet"]\n'
            "[provider.accounts.writer-a]\n"
            'auth_env = "CLAUDE_CODE_OAUTH_TOKEN"\nmodel = "opus"\n'
            "[provider.accounts.writer-b]\n"
            'auth_env = "CLAUDE_CODE_OAUTH_TOKEN"\nmodel = "sonnet"\n',
        )
    )
    (claude_writer.stand_in / "refused").write_text(_CLAUDE_TOKEN)
    sv = _sv(registry, tmp_path)
    request = _claude_review(claude_writer)
    request.pop("provider")
    run_id = _payload(_call(McpServer(supervisor=sv), "nspawn_run_start", request))[
        "run_id"
    ]
    status = sv.status(run_id, timeout_ms=20_000)
    assert status.phase is RunPhase.COMPLETED, (status.failure_reason, status.warning)
    row = registry.get_run(run_id)
    assert row is not None
    assert (row.executed_account, row.model) == ("writer-b", "sonnet")


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


def test_a_claude_review_runs_in_its_own_slice_and_claims_nothing(
    registry: Registry,
    tmp_path: Path,
    claude_writer: SimpleNamespace,
    paper_slices: list[object],
) -> None:
    """Custody of a read-only Claude run: the native client with the tools of its
    agent, in a slice of its own that is emptied at its end, and no claim."""
    sv = _sv(registry, tmp_path)
    run_id = _payload(
        _call(
            McpServer(supervisor=sv), "nspawn_run_start", _claude_review(claude_writer)
        )
    )["run_id"]

    assert sv.status(run_id, timeout_ms=20_000).phase is RunPhase.COMPLETED
    assert len(_PaperSlice.opened) == 1
    assert paper_slices == _PaperSlice.opened
    assert registry.writer_scope(run_id) is not None
    seen = _seen_by(claude_writer)
    argv: list[str] = seen["argv"]
    assert "--restricted" in argv
    assert "--settings" not in argv
    assert seen["cwd"] == str(claude_writer.checkout)
    assert seen["prompt"] == "review it"
    assert registry.read_claim(claude_writer.worktree) is None


def test_a_claude_review_on_a_proxied_route_runs_in_its_own_slice(
    registry: Registry,
    tmp_path: Path,
    claude_writer: SimpleNamespace,
    paper_slices: list[object],
) -> None:
    """A route on a public endpoint runs a review as any other route does."""
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
    # Spawned once, in a slice of its own, and without pinned settings.
    assert len(paper_slices) == 1 and paper_slices == _PaperSlice.opened
    assert "--settings" not in _seen_by(claude_writer)["argv"]


def test_the_review_verb_runs_a_claude_review_in_its_own_slice(
    registry: Registry,
    tmp_path: Path,
    claude_writer: SimpleNamespace,
    paper_slices: list[object],
) -> None:
    """`nspawn_review_start` is the verb the slice is for, not only a
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
    assert len(_PaperSlice.opened) == 1
    assert "--settings" not in _seen_by(claude_writer)["argv"]
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
    ("declared", "observed"),
    [("review", "mechanical_fanout"), ("judgment", "review")],
    ids=["declared-review-lowered", "declared-judgment-read-as-review"],
)
def test_the_confinement_follows_the_request_never_the_class_system1_read(
    registry: Registry,
    tmp_path: Path,
    claude_writer: SimpleNamespace,
    paper_slices: list[object],
    monkeypatch: pytest.MonkeyPatch,
    declared: str,
    observed: str,
) -> None:
    """System1 may lower a run's class, never its confinement: a read-only run
    keeps its tools and its own slice whatever class System1 read."""
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
    assert len(paper_slices) == 1 and paper_slices == _PaperSlice.opened
    argv = _seen_by(claude_writer)["argv"]
    assert "--restricted" in argv and "--settings" not in argv


def test_a_review_of_the_earlier_reader_cage_refuses_to_resume_by_name(
    registry: Registry,
    tmp_path: Path,
    claude_writer: SimpleNamespace,
    paper_slices: list[object],
) -> None:
    """The earlier reader cage kept its session in a config directory of its own,
    which this nSPAWN cannot reach: the resume is refused by name before a child
    starts, never quietly run as a new conversation."""
    sv = _sv(registry, tmp_path)
    thread = str(uuid.uuid4())
    old = RunRow(
        run_id=str(uuid.uuid4()),
        thread_id=thread,
        worktree=claude_writer.worktree,
        phase=RunPhase.PENDING,
        sandbox="read-only",
        model="opus",
        owner_instance=registry.instance_id,
        created_at=1,
        updated_at=1,
        backend=Backend.CLAUDE,
        executed_provider="writer",
        executed_account="writer-a",
    )
    config = tmp_path / "old-cage-config"
    config.mkdir()
    registry.admit_run(
        old,
        False,
        1,
        sv.RUN_CLAIM_LEASE_MS,
        native_context=NativeSessionContext(
            str(tmp_path), read_roots=(claude_writer.worktree,)
        ),
        session_profile=SessionProfile(
            old.run_id,
            False,
            True,
            (),
            config_dir=str(config),
            execution_class="review",
        ),
    )
    registry.update_run(replace(old, phase=RunPhase.COMPLETED))

    answer = _call(
        McpServer(supervisor=sv),
        "nspawn_run_start",
        {**_claude_review(claude_writer), "session_id": thread},
    )

    assert answer.get("error", {}).get("data", {}).get("type") == "InvalidRunRequest", (
        answer
    )
    assert "was a caged review" in answer["error"]["message"]
    assert paper_slices == [] and _PaperSlice.opened == []


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


#: The local route's key and endpoint. Nothing answers on port 9, and the turn
#: never asks: the stand-in client answers it.
_LOCAL_TOKEN = "sk" + "-local-qwen-stand-in-0123456789"
_LOCAL_ENDPOINT = "http://127.0.0.1:9"


def _walled_fleet(route: SimpleNamespace, tmp_path: Path, registry: Registry) -> None:
    """The fleet of 2026-10-21: every vendor account is walled, and a public proxy
    (`fugu`) and a route that cannot write (`agy`) rank above the local route.
    The local route declares its own settings, as the owner's qwen does, and the
    unranked `mixed` route declares a public account before its local one."""
    local = tmp_path / "local-secrets"
    local.mkdir()
    local.chmod(0o700)  # whatever the umask
    for alias in ("server", "near"):
        token = local / f"{alias}.token"
        token.write_text(_LOCAL_TOKEN + "\n")
        token.chmod(0o600)  # whatever the umask
    route.roster.write_text(
        f"[defaults]\nplugins = [{json.dumps(str(tmp_path / 'plugin'))}]\n"
        '[defaults.env]\nAPI_TIMEOUT_MS = "600000"\n'
        'HTTPS_PROXY = "http://proxy.invalid:3128"\n'
        f"[bins]\nclaude = {json.dumps(str(route.stand_in / 'claude'))}\n"
        'codex = "/usr/bin/true"\nagy = "/usr/bin/true"\n'
        '[[provider]]\nname = "writer"\nkind = "claude"\nstrength = 9\n'
        f"secrets = {json.dumps(str(tmp_path / 'secrets'))}\n"
        'model = "opus"\nmodels = ["opus"]\n'
        '[[provider]]\nname = "codex"\nkind = "codex"\nstrength = 9\nmodel = "gpt"\n'
        f"accounts = {{ codex-main = {{ home = {json.dumps(str(tmp_path / 'codex'))} }} }}\n"
        '[[provider]]\nname = "fugu"\nkind = "claude"\nstrength = 8\nmodel = "fugu"\n'
        '[provider.env]\nANTHROPIC_BASE_URL = "https://proxy.invalid"\n'
        '[[provider]]\nname = "agy"\nkind = "agy"\nstrength = 6\nmodel = "gemini"\n'
        f"accounts = {{ agy1 = {{ home = {json.dumps(str(tmp_path / 'agy'))} }} }}\n"
        '[[provider]]\nname = "qwen"\nkind = "claude"\nstrength = 5\nthrift = 9\n'
        f'secrets = {json.dumps(str(local))}\nmodel = "qwen-local"\n'
        '[provider.env]\nCLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS = "1"\n'
        'CLAUDE_CODE_MAX_OUTPUT_TOKENS = "8192"\n'
        f'[provider.accounts.server]\nbase_url = "{_LOCAL_ENDPOINT}"\n'
        'auth_env = "ANTHROPIC_AUTH_TOKEN"\n'
        'env = { CLAUDE_CODE_AUTO_COMPACT_WINDOW = "65536" }\n'
        '[[provider]]\nname = "mixed"\nkind = "claude"\n'
        f'secrets = {json.dumps(str(local))}\nmodel = "qwen-local"\n'
        '[provider.accounts.far]\nbase_url = "https://proxy.invalid"\n'
        'auth_env = "ANTHROPIC_AUTH_TOKEN"\n'
        'env = { MAX_THINKING_TOKENS = "2048" }\n'
        f'[provider.accounts.near]\nbase_url = "{_LOCAL_ENDPOINT}"\n'
        'auth_env = "ANTHROPIC_AUTH_TOKEN"\n'
        'env = { CLAUDE_CODE_MAX_OUTPUT_TOKENS = "4096" }\n'
        f"[native_write]\nroots = [{json.dumps(str(tmp_path))}]\n"
    )
    now = int(time.time())
    for alias in ("writer-a", "writer-b"):
        registry.record_account_wall(
            alias, now + 3600, "vendor_reset", "seven_day", now
        )
    registry.record_provider_wall("codex", now + 3600, "default_1h", now)


def test_writer_capabilities_do_not_depend_on_a_cage_endpoint(
    registry: Registry, tmp_path: Path, claude_writer: SimpleNamespace
) -> None:
    _walled_fleet(claude_writer, tmp_path, registry)
    listed = {
        entry.provider: entry.capabilities
        for entry in supervisor.provider_report().providers
    }
    assert listed["qwen"] == ("custody", "resume", "review", "write")
    assert listed["fugu"] == ("custody", "resume", "review", "write")


def test_writer_routes_keep_public_and_private_accounts(
    tmp_path: Path,
) -> None:
    """Account by account: an account with a public endpoint is kept off its
    route, and a route with no other account is left out."""
    roster = tmp_path / "spawn.toml"
    roster.write_text(
        '[[provider]]\nname = "mixed"\nkind = "claude"\nmodel = "m"\n'
        "[provider.accounts.vendor]\n"
        '[provider.accounts.proxy]\nbase_url = "https://proxy.invalid"\n'
        '[provider.accounts.local]\nbase_url = "http://127.0.0.1:9"\n'
        '[[provider]]\nname = "remote"\nkind = "claude"\nmodel = "m"\n'
        '[provider.accounts.far]\nbase_url = "http://8.8.8.8:80"\n'
    )
    writable = supervisor._writable_routes(Roster.read(roster))
    assert [(p.name, p.excluded_accounts) for p in writable.providers] == [
        ("mixed", []),
        ("remote", []),
    ]


def _c1lock_git(root, *arguments):
    home = root.parent / "c1lock-git-home"
    home.mkdir(exist_ok=True)
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    env.update(
        HOME=str(home),
        XDG_CONFIG_HOME=str(home / "config"),
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL="/dev/null",
        GIT_TERMINAL_PROMPT="0",
    )
    return subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=C1 fixture",
            "-c",
            "user.email=c1@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.hooksPath=/dev/null",
            *arguments,
        ],
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    ).stdout.strip()


def _c1lock_repo(root):
    metadata = root / ".git"
    if metadata.is_dir() and not any(metadata.iterdir()):
        metadata.rmdir()
    root.mkdir(parents=True, exist_ok=True)
    _c1lock_git(root, "init", "-q", "-b", "main")
    (root / ".gitignore").write_text(".nott/\nC1LOCK_COMMAND_RAN\n")
    (root / "base.txt").write_text("A\n")
    _c1lock_git(root, "add", ".gitignore", "base.txt")
    _c1lock_git(root, "commit", "-qm", "A")
    ancestor = _c1lock_git(root, "rev-parse", "HEAD")
    (root / "base.txt").write_text("B\n")
    _c1lock_git(root, "add", "base.txt")
    _c1lock_git(root, "commit", "-qm", "B")
    head = _c1lock_git(root, "rev-parse", "HEAD")
    origin = root.parent / (root.name + "-c1lock-origin.git")
    _c1lock_git(root, "init", "-q", "--bare", str(origin))
    _c1lock_git(root, "remote", "add", "origin", str(origin))
    _c1lock_git(root, "push", "-q", "origin", "main")
    return head, ancestor


def _c1lock_bundle(root, head, unit="u1"):
    import hashlib

    directory = root / ".nott/plans/c1lock"
    tests = directory / "tests" / (unit + ".md")
    record = directory / "red" / (unit + ".json")
    tests.parent.mkdir(parents=True, exist_ok=True)
    record.parent.mkdir(parents=True, exist_ok=True)
    entries = []
    witnesses = {}
    file = "mcps/nspawn/tests/test_c1lock_probe.py"
    for suffix in ("a", "b"):
        ident = "c1lock-probe-" + suffix
        name = "test_c1lock_probe_" + suffix
        entries.append(
            f"## Test {ident}\n"
            f"- `file`: {file}\n"
            f"- `name`: {name}\n"
            "- `covers`: fixture failure\n"
            f"- `fails today`: fixture assertion, {file}:1\n"
        )
        witnesses[ident] = {
            "file": file,
            "name": name,
            "command": "/usr/bin/touch " + str(root / "C1LOCK_COMMAND_RAN"),
            "exit": 1,
            "assertion": "C1LOCK_PRIVATE_ASSERTION",
            "base": head,
        }
    code = ""
    for suffix in ("a", "b"):
        code += (
            f"def test_c1lock_probe_{suffix}():\n"
            "    from pathlib import Path\n"
            "    Path('C1LOCK_COMMAND_RAN').touch()\n"
            "    assert False, 'C1LOCK_PRIVATE_ASSERTION'\n\n"
        )
    raw = (
        "## Failure space\n"
        "- input fixture -> c1lock-probe-a, c1lock-probe-b\n"
        "- failure fixture -> c1lock-probe-a, c1lock-probe-b\n"
        "- touch none: synthetic admission fixture\n"
        + "\n".join(entries)
        + f"\n## Code {file}\n```python\n{code}```\n"
    ).encode()
    value = {
        "schema": "nott.plan.red.v1",
        "tests": "tests/" + unit + ".md",
        "tests_sha256": hashlib.sha256(raw).hexdigest(),
        "witnesses": witnesses,
        "refused": {},
    }
    tests.write_bytes(raw)
    record.write_text(json.dumps(value, sort_keys=True))
    return SimpleNamespace(
        tests=tests,
        record=record,
        raw=raw,
        value=value,
        locator=record.relative_to(root).as_posix(),
    )


def _c1lock_atoms(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from _c1lock_atoms(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _c1lock_atoms(item)
    else:
        yield value


def _c1lock_replace_atom(value, before, after):
    if isinstance(value, dict):
        return {
            key: _c1lock_replace_atom(item, before, after)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_c1lock_replace_atom(item, before, after) for item in value]
    return after if value == before else value


def _c1lock_receipts(registry, run_id):
    return [
        event
        for event in registry.read_run_events(run_id)
        if isinstance(event.payload, dict) and "writer_red" in event.payload
    ]


def _c1lock_refusal(reply):
    error = reply.get("error")
    assert isinstance(error, dict), (
        "implementation admission must refuse instead of accepting a writer",
        reply,
    )
    assert error.get("code") == -32602, error
    assert error.get("data", {}).get("type") == "InvalidRunRequest", (
        "the writer guard must diagnose this, not unknown-field validation",
        error,
    )
    message = error["message"]
    for private in ("C1LOCK_PRIVATE_ASSERTION", "C1LOCK_COMMAND_RAN"):
        assert private not in message
    assert "\x1b" not in message
    assert len(message) <= 8192
    return message


def _c1lock_project(run_id, capsys):
    from nspawn import cli

    capsys.readouterr()
    assert cli.main(["run", "list", "--since", "1970-01-01T00:00:00Z"]) == 0
    output = capsys.readouterr()
    assert output.err == ""
    projected = next(
        item for item in json.loads(output.out)["runs"] if item["run_id"] == run_id
    )
    assert set(projected) == {
        "run_id",
        "lane",
        "phase",
        "executed_provider",
        "executed_account",
        "model",
        "tokens",
        "turn_count",
        "failure_reason",
        "created_at",
        "updated_at",
        "group",
        "backend",
        "native_write",
        "execution_class",
        "red_admission",
        "red_admission_reason",
        "red_receipt",
    }
    return projected


@pytest.fixture
def _c1lock_writer(registry, tmp_path, writer_route, monkeypatch):
    head, ancestor = _c1lock_repo(writer_route.checkout)
    started = []
    monkeypatch.setattr(
        Supervisor,
        "_drive_native_run",
        lambda self, *args, **kwargs: started.append(args),
    )
    sv = _sv(registry, tmp_path)
    case = SimpleNamespace(
        registry=registry,
        sv=sv,
        server=McpServer(supervisor=sv),
        route=writer_route,
        root=writer_route.checkout,
        head=head,
        ancestor=ancestor,
        started=started,
    )
    try:
        yield case
    finally:
        sv.shutdown()


def _c1lock_start(case, **changes):
    return _call(
        case.server,
        "nspawn_run_start",
        _write(case.route, **{"class": "implementation", **changes}),
    )


def _c1lock_no_admission(case):
    assert case.registry.list_runs() == ()
    assert case.registry.read_claim(case.route.worktree) is None
    assert case.route.prepared == []
    assert case.started == []


def test_c1_missing_record_refuses_before_launch(_c1lock_writer):
    case = _c1lock_writer
    message = _c1lock_refusal(_c1lock_start(case))
    assert "nspawn_run_start" in message
    assert case.head in message
    assert ".nott/plans/writer-red/tests/unit.md" in message
    assert ".nott/plans/writer-red/red/unit.json" in message
    assert "nott plan test" in message and "--branch main" in message
    assert "red_record" in message
    _c1lock_no_admission(case)


@pytest.mark.parametrize(
    "execution_class",
    ["judgment", "planning", "review", "mechanical_fanout", "design", "deterministic"],
)
@pytest.mark.parametrize("with_record", [False, True])
def test_c1_class_only_relabel_cannot_bypass_writer_admission(
    _c1lock_writer, execution_class, with_record
):
    case = _c1lock_writer
    options = {"class": execution_class}
    if with_record:
        options["red_record"] = _c1lock_bundle(case.root, case.head).locator
    message = _c1lock_refusal(_c1lock_start(case, **options))
    assert execution_class in message
    assert "implementation" in message and "red_record" in message
    assert not re.search(r"(?:use|retry|set).{0,40}class.{0,10}verification", message)
    _c1lock_no_admission(case)

    read = _c1lock_start(
        case,
        **{
            "class": execution_class,
            "native_write": False,
            "read_only": True,
        },
    )
    assert "error" not in read, (
        "the writer-role binding must not prohibit read-only routing classes",
        read,
    )


@pytest.mark.parametrize("launcher", ["native", "claude"])
def test_c1_valid_record_is_a_writer_request_input(
    registry, tmp_path, monkeypatch, request, capsys, launcher
):
    import hashlib

    from nspawn.schema import REGISTRY_SCHEMA_VERSION

    route = request.getfixturevalue(
        "writer_route" if launcher == "native" else "claude_writer"
    )
    head, _ = _c1lock_repo(route.checkout)
    bundle = _c1lock_bundle(route.checkout, head)
    started = []
    for method in ("_drive_native_run", "_drive_claude_caged"):
        monkeypatch.setattr(
            Supervisor,
            method,
            lambda self, *args, **kwargs: started.append(args),
        )
    sv = _sv(registry, tmp_path)
    try:
        arguments = _write(route) if launcher == "native" else _claude_write(route)
        arguments.update({"class": "implementation", "red_record": bundle.locator})
        reply = _call(McpServer(supervisor=sv), "nspawn_run_start", arguments)
        assert "error" not in reply, (
            "the public JSON door must admit an exact-HEAD bundle",
            reply,
        )
        run_id = _payload(reply)["run_id"]
        _wait_for(lambda: started, 2, "the admitted fixture worker")
        events = _c1lock_receipts(registry, run_id)
        assert len(events) == 1
        atoms = list(_c1lock_atoms(events[0].payload["writer_red"]))
        assert head in atoms
        assert bundle.locator in atoms
        assert bundle.tests.relative_to(route.checkout).as_posix() in atoms
        assert hashlib.sha256(bundle.record.read_bytes()).hexdigest() in atoms
        assert hashlib.sha256(bundle.raw).hexdigest() in atoms
        assert "c1lock-probe-a" in atoms and "c1lock-probe-b" in atoms
        encoded = json.dumps(events[0].payload)
        assert "C1LOCK_PRIVATE_ASSERTION" not in encoded
        assert "C1LOCK_COMMAND_RAN" not in encoded
        assert not (route.checkout / "C1LOCK_COMMAND_RAN").exists()
        assert REGISTRY_SCHEMA_VERSION == 7
        assert set(registry.session_profile(run_id).data()) == {
            "original_run_id",
            "native_write",
            "read_only",
            "read_paths",
            "config_dir",
            "execution_class",
            "timeout_ms",
        }

        row = registry.get_run(run_id)
        registry.update_run(replace(row, phase=RunPhase.COMPLETED), drop_claim=True)
        _c1lock_git(route.checkout, "commit", "--allow-empty", "-qm", "Later HEAD")
        route.checkout.rename(route.checkout.with_name("removed-checkout"))
        projected = _c1lock_project(run_id, capsys)
        assert projected["execution_class"] == "implementation"
        assert projected["red_admission"] == "valid"
        assert projected["red_admission_reason"] is None
        assert projected["red_receipt"] == {
            "record": bundle.locator,
            "commit": head,
            "unit": "u1",
            "tests": 2,
            "admitted_at": events[0].at,
        }
    finally:
        sv.shutdown()


@pytest.mark.parametrize("cause", ["missing", "digest", "ancestor"])
def test_c1_bad_record_refusal_names_the_producer_remedy(_c1lock_writer, cause):
    case = _c1lock_writer
    bundle = _c1lock_bundle(case.root, case.head)
    if cause == "missing":
        bundle.record.unlink()
    elif cause == "digest":
        bundle.tests.write_bytes(bundle.raw + b"\nChanged after production.\n")
    else:
        for witness in bundle.value["witnesses"].values():
            witness["base"] = case.ancestor
        bundle.record.write_text(json.dumps(bundle.value))
    message = _c1lock_refusal(_c1lock_start(case, red_record=bundle.locator))
    for fragment in (
        "nspawn_run_start",
        case.head,
        bundle.locator,
        bundle.tests.relative_to(case.root).as_posix(),
        "nott plan test",
        "--base " + case.head,
        "--branch main",
        "red_record",
        "implementation",
    ):
        assert fragment in message
    if cause == "missing":
        assert any(
            word in message.lower()
            for word in ("missing", "not found", "read", "unavailable")
        )
    elif cause == "digest":
        assert "sha256" in message.lower() or "digest" in message.lower()
    else:
        assert case.ancestor in message and "base" in message.lower()
    assert not re.search(r"<(?:base|commit|branch|path|head)>", message)
    _c1lock_no_admission(case)


def _c1lock_historical_implementation(registry, run_id):
    events = registry.read_run_events(run_id)
    assert any("session_profile" in event.payload for event in events)
    trigger = registry._one(
        "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = ?",
        ("run_events_append_only_delete",),
    )["sql"]
    with registry._transaction():
        registry._run("DROP TRIGGER run_events_append_only_delete")
        registry._run("DELETE FROM run_events WHERE run_id = ?", (run_id,))
        registry._run(trigger)
        for event in events:
            payload = json.loads(json.dumps(event.payload))
            if "session_profile" in payload:
                payload["session_profile"]["execution_class"] = "implementation"
            registry._append_run_event(
                run_id, at=event.at, kind=event.kind, payload=payload
            )
    assert registry.session_profile(run_id).execution_class == "implementation"


def _c1lock_origin(registry, tmp_path, route, phase):
    _c1lock_repo(route.checkout)
    sv = _sv(registry, tmp_path)
    server = McpServer(supervisor=sv)
    reply = _call(
        server,
        "nspawn_run_start",
        _claude_write(
            route,
            **{
                "class": "verification",
                "prompt": "fail after history"
                if phase == "failed"
                else "origin beacon",
            },
        ),
    )
    assert "error" not in reply, ("verification must remain usable", reply)
    first = _payload(reply)["run_id"]
    expected = RunPhase.FAILED if phase == "failed" else RunPhase.COMPLETED
    assert sv.status(first, timeout_ms=20_000).phase is expected
    _c1lock_historical_implementation(registry, first)
    return sv, server, first


@pytest.mark.parametrize("reference", ["run", "thread"])
def test_c1_inherited_implementation_resume_needs_a_record(
    registry, tmp_path, claude_writer, paper_slices, reference
):
    sv, server, first = _c1lock_origin(registry, tmp_path, claude_writer, "completed")
    try:
        assert sv.close(first).claim_released
        original = registry.get_run(first)
        arguments = {
            "cwd": str(claude_writer.checkout),
            "prompt": "continue this conversation",
            **(
                {"original_run": first}
                if reference == "run"
                else {"session_id": original.thread_id, "native_write": True}
            ),
        }
        message = _c1lock_refusal(_call(server, "nspawn_run_start", arguments))
        assert "red_record" in message and "implementation" in message
        assert len(registry.list_runs()) == 1
        assert registry.read_claim(claude_writer.worktree) is None
    finally:
        sv.shutdown()


@pytest.mark.parametrize("reference", ["run", "thread"])
@pytest.mark.parametrize("phase", ["completed", "failed"])
def test_c1_refused_resume_keeps_its_origin(
    registry, tmp_path, claude_writer, paper_slices, reference, phase
):
    sv, server, first = _c1lock_origin(registry, tmp_path, claude_writer, phase)
    try:
        original = registry.get_run(first)
        result = sv.result(first)
        claim = registry.read_claim(claude_writer.worktree)
        arguments = {
            "cwd": str(claude_writer.checkout),
            "prompt": "continue this conversation",
            **(
                {"original_run": first}
                if reference == "run"
                else {"session_id": original.thread_id, "native_write": True}
            ),
        }
        reply = _call(server, "nspawn_run_start", arguments)
        assert registry.get_run(first) == original, (
            "witness preflight must precede automatic origin close",
            reply,
        )
        assert sv.result(first) == result
        assert registry.read_claim(claude_writer.worktree) == claim
        _c1lock_refusal(reply)
        assert len(registry.list_runs()) == 1
    finally:
        sv.shutdown()


@pytest.mark.parametrize("execution_class", ["implementation", None, "future_writer"])
def test_c1_final_admission_has_no_witnessless_failover_path(
    _c1lock_writer, execution_class
):
    case = _c1lock_writer
    error = None
    try:
        case.sv._admit(
            "change it",
            str(case.root),
            provider=None,
            model=None,
            effort=None,
            execution_class=execution_class,
            session_id=None,
            timeout_ms=0,
            claim_worktree=True,
            read_only=False,
            native_write=True,
        )
    except Exception as caught:  # noqa: BLE001 - assert the exact boundary refusal below
        error = caught
    assert isinstance(error, InvalidRunRequest), (
        "the shared final guard must refuse independently of execute",
        error,
    )
    assert "implementation" in str(error)
    if execution_class == "implementation":
        assert "red_record" in str(error)
    _c1lock_no_admission(case)


@pytest.mark.parametrize(
    "mutation",
    [
        "empty",
        "json",
        "schema",
        "duplicate-root",
        "duplicate-id",
        "unknown-root",
        "unknown-witness",
        "no-witnesses",
        "missing-id",
        "additional-id",
        "substituted-id",
        "file",
        "node",
        "exit-zero",
        "exit-two",
        "exit-bool",
        "blank-assertion",
        "multiline-assertion",
        "blank-command",
        "multiline-command",
        "refused",
        "digest",
        "mixed-base",
        "ancestor",
        "short-base",
        "uppercase-base",
        "grammar",
    ],
)
def test_c1_bad_bundles_never_become_launches(_c1lock_writer, mutation):
    import hashlib

    case = _c1lock_writer
    bundle = _c1lock_bundle(case.root, case.head)
    value = bundle.value
    witnesses = value["witnesses"]
    first = witnesses["c1lock-probe-a"]
    if mutation == "empty":
        bundle.record.write_bytes(b"")
    elif mutation == "json":
        bundle.record.write_bytes(b'{"schema":')
    elif mutation == "duplicate-root":
        raw = bundle.record.read_text()
        bundle.record.write_text('{"schema":"nott.plan.red.v1",' + raw[1:])
    elif mutation == "duplicate-id":
        raw = json.dumps(value)
        key = '"c1lock-probe-a":'
        entry = json.dumps(first)
        bundle.record.write_text(
            raw.replace(key, key + entry + ', "c1lock-probe-a":', 1)
        )
    else:
        if mutation == "schema":
            value["schema"] = "nott.plan.red.v0"
        elif mutation == "unknown-root":
            value["extra"] = "C1LOCK_PRIVATE_ASSERTION"
        elif mutation == "unknown-witness":
            first["extra"] = "C1LOCK_PRIVATE_ASSERTION"
        elif mutation == "no-witnesses":
            value["witnesses"] = {}
        elif mutation == "missing-id":
            del witnesses["c1lock-probe-b"]
        elif mutation == "additional-id":
            witnesses["c1lock-extra"] = dict(first)
        elif mutation == "substituted-id":
            witnesses["c1lock-substitute"] = witnesses.pop("c1lock-probe-b")
        elif mutation == "file":
            first["file"] = "mcps/nspawn/tests/test_other.py"
        elif mutation == "node":
            first["name"] = "TestOther::test_substitute"
        elif mutation.startswith("exit-"):
            first["exit"] = {
                "exit-zero": 0,
                "exit-two": 2,
                "exit-bool": True,
            }[mutation]
        elif mutation == "blank-assertion":
            first["assertion"] = " "
        elif mutation == "multiline-assertion":
            first["assertion"] = "first\nC1LOCK_PRIVATE_ASSERTION"
        elif mutation == "blank-command":
            first["command"] = "\t"
        elif mutation == "multiline-command":
            first["command"] = "pytest\nC1LOCK_PRIVATE_ASSERTION"
        elif mutation == "refused":
            value["refused"] = {"c1lock-probe-b": "C1LOCK_PRIVATE_ASSERTION"}
        elif mutation == "digest":
            value["tests_sha256"] = "0" * 64
        elif mutation == "mixed-base":
            first["base"] = case.ancestor
        elif mutation == "ancestor":
            for witness in witnesses.values():
                witness["base"] = case.ancestor
        elif mutation == "short-base":
            first["base"] = case.head[:12]
        elif mutation == "uppercase-base":
            first["base"] = case.head.upper()
        elif mutation == "grammar":
            raw = b"This is not a judged test set.\n"
            bundle.tests.write_bytes(raw)
            value["tests_sha256"] = hashlib.sha256(raw).hexdigest()
        bundle.record.write_text(json.dumps(value))
    message = _c1lock_refusal(_c1lock_start(case, red_record=bundle.locator))
    assert case.head in message
    assert bundle.locator in message
    assert "RED" in message or "witness" in message.lower()
    _c1lock_no_admission(case)


@pytest.mark.parametrize(
    "fault",
    [
        "absolute",
        "traversal",
        "unnormalized",
        "git-metadata",
        "wrong-layout",
        "wrong-stem",
        "record-link",
        "record-parent-link",
        "plan-parent-link",
        "set-link",
        "set-parent-link",
        "record-directory",
        "set-directory",
        "record-fifo",
        "set-fifo",
        "record-large",
        "set-large",
    ],
)
def test_c1_bundle_paths_are_bounded_regular_and_confined(_c1lock_writer, fault):
    import hashlib

    case = _c1lock_writer
    bundle = _c1lock_bundle(case.root, case.head)
    locator = bundle.locator
    if fault == "absolute":
        locator = str(bundle.record)
    elif fault == "traversal":
        locator = ".nott/plans/c1lock/../c1lock/red/u1.json"
    elif fault == "unnormalized":
        locator = ".nott//plans/c1lock/red/u1.json"
    elif fault == "git-metadata":
        locator = ".git/red/u1.json"
    elif fault == "wrong-layout":
        target = case.root / ".nott/plans/c1lock/u1.json"
        target.write_bytes(bundle.record.read_bytes())
        locator = target.relative_to(case.root).as_posix()
    elif fault == "wrong-stem":
        target = bundle.record.with_name("other.json")
        target.write_bytes(bundle.record.read_bytes())
        locator = target.relative_to(case.root).as_posix()
    elif fault.endswith("-link"):
        target = {
            "record-link": bundle.record,
            "set-link": bundle.tests,
            "record-parent-link": bundle.record.parent,
            "set-parent-link": bundle.tests.parent,
            "plan-parent-link": bundle.record.parent.parent,
        }[fault]
        saved = case.root.parent / ("saved-" + target.name)
        target.rename(saved)
        target.symlink_to(saved, target_is_directory=saved.is_dir())
    elif fault.endswith("-directory"):
        target = bundle.record if fault.startswith("record") else bundle.tests
        target.unlink()
        target.mkdir()
    elif fault.endswith("-fifo"):
        target = bundle.record if fault.startswith("record") else bundle.tests
        target.unlink()
        os.mkfifo(target)
    elif fault == "record-large":
        bundle.record.write_bytes(b" " * ((256 << 10) + 1))
    else:
        raw = bundle.raw + b"\n" + b"x" * (256 << 10)
        bundle.tests.write_bytes(raw)
        bundle.value["tests_sha256"] = hashlib.sha256(raw).hexdigest()
        bundle.record.write_text(json.dumps(bundle.value))

    replies = []
    failures = []

    def call():
        try:
            replies.append(_c1lock_start(case, red_record=locator))
        except Exception as error:  # noqa: BLE001 - assert the exact boundary refusal below
            failures.append(error)

    thread = threading.Thread(target=call, daemon=True)
    thread.start()
    thread.join(timeout=3)
    assert not thread.is_alive(), "unsafe files must not block admission"
    assert failures == [], failures
    assert len(replies) == 1
    message = _c1lock_refusal(replies[0])
    assert case.head in message and "red_record" in message
    assert "regular" in message.lower() or "path" in message.lower()
    _c1lock_no_admission(case)


def test_c1_in_place_change_during_read_is_refused(_c1lock_writer, monkeypatch):
    from nspawn import red_witness

    case = _c1lock_writer
    bundle = _c1lock_bundle(case.root, case.head)
    before = bundle.record.stat()
    original = bundle.record.read_bytes()
    changed = original.replace(b"C1LOCK_PRIVATE_ASSERTION", b"C1LOCK_CHANGED_ASSERTION")
    assert changed != original and len(changed) == len(original)
    read = os.read
    rewritten = []

    def rewrite_then_read(fd, size):
        observed = os.fstat(fd)
        if not rewritten and (observed.st_dev, observed.st_ino) == (
            before.st_dev,
            before.st_ino,
        ):
            # The reader has taken its first stat but has not read any bytes.
            with bundle.record.open("r+b") as stream:
                stream.write(changed)
            os.utime(
                bundle.record,
                ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000),
            )
            rewritten.append(bundle.record.stat())
        return read(fd, size)

    monkeypatch.setattr(red_witness.os, "read", rewrite_then_read)
    reply = _c1lock_start(case, red_record=bundle.locator)
    assert len(rewritten) == 1 and rewritten[0].st_ino == before.st_ino
    assert bundle.record.read_bytes() == changed
    message = _c1lock_refusal(reply)
    assert "regular files" in message and case.head in message
    _c1lock_no_admission(case)


def test_c1_admission_never_combines_replaced_bundle_versions(_c1lock_writer):
    import hashlib

    case = _c1lock_writer
    bundle = _c1lock_bundle(case.root, case.head)
    initial = _c1lock_start(case, red_record=bundle.locator)
    assert "error" not in initial, ("valid bundle must reach admission", initial)
    first_id = _payload(initial)["run_id"]
    row = case.registry.get_run(first_id)
    case.registry.update_run(replace(row, phase=RunPhase.COMPLETED), drop_claim=True)
    record_a = bundle.record.read_bytes()
    set_a = bundle.tests.read_bytes()
    set_b = b"Different ignored preamble.\n\n" + set_a
    value_b = json.loads(record_a)
    value_b["tests_sha256"] = hashlib.sha256(set_b).hexdigest()
    record_b = json.dumps(value_b, sort_keys=True).encode()
    pairs = [
        (hashlib.sha256(record_a).hexdigest(), hashlib.sha256(set_a).hexdigest()),
        (hashlib.sha256(record_b).hexdigest(), hashlib.sha256(set_b).hexdigest()),
    ]
    stop = threading.Event()

    def replace_versions():
        for index in range(200):
            if stop.is_set():
                return
            record_raw, set_raw = (record_a, set_a) if index % 2 else (record_b, set_b)
            for target, raw in ((bundle.tests, set_raw), (bundle.record, record_raw)):
                temporary = target.with_name(target.name + ".replace")
                temporary.write_bytes(raw)
                os.replace(temporary, target)

    writer = threading.Thread(target=replace_versions, daemon=True)
    writer.start()
    try:
        for index in range(12):
            reply = _c1lock_start(
                case,
                red_record=bundle.locator,
                idempotency_key=f"c1lock-swap-{index}",
            )
            if "error" in reply:
                _c1lock_refusal(reply)
                continue
            run_id = _payload(reply)["run_id"]
            events = _c1lock_receipts(case.registry, run_id)
            assert len(events) == 1
            atoms = list(_c1lock_atoms(events[0].payload["writer_red"]))
            assert any(a in atoms and b in atoms for a, b in pairs), (
                "receipt must identify one matching snapshot",
                atoms,
            )
            row = case.registry.get_run(run_id)
            case.registry.update_run(
                replace(row, phase=RunPhase.COMPLETED), drop_claim=True
            )
    finally:
        stop.set()
        writer.join(timeout=3)
    assert not writer.is_alive()


def test_c1_clone_head_ignores_inherited_git_redirection(_c1lock_writer, monkeypatch):
    case = _c1lock_writer
    bundle = _c1lock_bundle(case.root, case.head)
    other = case.root.parent / "redirected-repository"
    _c1lock_repo(other)
    _c1lock_git(other, "commit", "--allow-empty", "-qm", "Different redirected HEAD")
    assert _c1lock_git(other, "rev-parse", "HEAD") != case.head
    before = _c1lock_git(case.root, "for-each-ref")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.worktree")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(other))
    reply = _c1lock_start(case, red_record=bundle.locator)
    assert "error" not in reply, (
        "witness binding must observe cwd, not inherited Git redirection",
        reply,
    )
    run_id = _payload(reply)["run_id"]
    events = _c1lock_receipts(case.registry, run_id)
    assert len(events) == 1
    assert case.head in list(_c1lock_atoms(events[0].payload["writer_red"]))
    assert _c1lock_git(case.root, "rev-parse", "HEAD") == case.head
    assert _c1lock_git(case.root, "for-each-ref") == before


@pytest.mark.parametrize("fault", ["drift", "git_failure", "timeout", "ownership"])
def test_c1_head_recheck_rolls_back_admission_and_reuses_the_key(
    _c1lock_writer, monkeypatch, tmp_path, fault
):
    from nott_database import BusyError

    case = _c1lock_writer
    bundle = _c1lock_bundle(case.root, case.head)
    observe = supervisor.red_witness.clone_head
    admit_run = case.registry.admit_run
    admissions = []
    admitting = []
    rechecks = []
    cleanup_writes = []
    moved = []

    def admit(row, *args, **kwargs):
        admissions.append(row.run_id)
        admitting.append(row.run_id)
        try:
            return admit_run(row, *args, **kwargs)
        finally:
            admitting.pop()

    def unavailable(argv, **kwargs):
        assert argv[0] == "git"
        assert "--no-optional-locks" in argv
        assert kwargs["timeout"] == 10
        if fault == "timeout":
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        raise OSError("fixture Git observation failed")

    def head(root):
        if admitting:
            # Admission observes HEAD outside every write, before it writes
            # the run, the receipt or the claim: no provisional claim exists.
            rechecks.append((admitting[-1], case.registry.connection.in_transaction))
            assert case.registry.read_claim(case.route.worktree) is None
            assert case.registry.get_run(admitting[-1]) is None
            assert _c1lock_receipts(case.registry, admitting[-1]) == []
            if len(rechecks) == 1:
                if fault == "ownership":
                    # Another run takes the worktree and releases it during Git.
                    worktree = case.route.worktree
                    case.registry.claim_worktree(
                        worktree, "competing-run", int(time.time() * 1000), 100_000
                    )
                    assert case.registry.release_claim(worktree, "competing-run")
                elif fault == "drift":
                    _c1lock_git(
                        root, "commit", "--allow-empty", "-qm", "Concurrent HEAD"
                    )
                    moved.append(_c1lock_git(root, "rev-parse", "HEAD"))
                else:
                    with monkeypatch.context() as failure:
                        failure.setattr(
                            supervisor.red_witness.subprocess, "run", unavailable
                        )
                        return observe(root)
        return observe(root)

    def busy_cleanup(row, **kwargs):
        cleanup_writes.append(row.run_id)
        raise BusyError("fixture refuses every compensating write")

    monkeypatch.setattr(case.registry, "admit_run", admit)
    monkeypatch.setattr(case.registry, "update_run", busy_cleanup)
    monkeypatch.setattr(supervisor.red_witness, "clone_head", head)
    arguments = _write(
        case.route,
        red_record=bundle.locator,
        idempotency_key="c1lock-transaction-head-recheck",
    )
    del arguments["class"]
    arguments["execution_class"] = "implementation"
    frame = {
        "tool": "nspawn_run_start",
        "launch_uuid": str(uuid.uuid4()),
        "arguments": arguments,
        "project": driver.project_key(str(case.root)),
    }
    if fault == "ownership":
        # The real MCP door, its in-process driver and the supervisor.
        answer = _call(
            case.server,
            "nspawn_run_start",
            _write(
                case.route,
                red_record=bundle.locator,
                idempotency_key="c1lock-transaction-head-recheck",
                **{"class": "implementation"},
            ),
        )
    else:
        answer = driver.serve(
            frame,
            lambda ack: None,
            supervisor_factory=lambda: case.sv,
            owns_supervisor=False,
        )

    assert admissions and rechecks, (
        "the fixture must reach the admission HEAD observation",
        answer,
    )
    assert case.registry.list_runs() == (), "a refused admission must not leave a run"
    assert rechecks == [(admissions[0], False)]
    assert case.registry.read_claim(case.route.worktree) is None
    for table in ("run_events", "native_contexts", "claims", "run_keys", "notices"):
        assert (
            sql(case.registry, f"SELECT count(*) AS n FROM {table}").fetchone()["n"]
            == 0
        )
    assert not (tmp_path / "runs" / admissions[0]).exists()
    assert case.started == []
    assert cleanup_writes == []
    assert case.sv._unpublished == {}
    if fault == "ownership":
        # The client gets the type, the fixed text and the retryable flag.
        error = answer["error"]
        assert error["message"] == (
            "The worktree claim changed while admission read the clone HEAD. "
            "No run was admitted. Retry the same call."
        ), answer
        assert error["data"]["type"] == "WorktreeOwnershipChanged"
        assert error["data"]["retryable"] is True
    else:
        assert answer["refused"]["type"] == "InvalidRunRequest", answer
        message = answer["refused"]["message"]
        assert case.head in message
        assert "HEAD" in message and "red_record" in message
        assert "nott plan test" in message
    if fault == "drift":
        assert moved[0] != case.head
        assert moved[0] in message
    elif fault != "ownership":
        assert "observation failed" in message

    # A competing close has no admission to change before publication.
    with pytest.raises(supervisor.UnknownRun):
        case.sv.close(admissions[0])
    assert case.sv.publish_pending()
    assert case.registry.list_runs() == ()

    if fault == "drift":
        renewed = _c1lock_bundle(case.root, moved[0])
        assert renewed.locator == bundle.locator
    retry = driver.serve(
        {**frame, "launch_uuid": str(uuid.uuid4())},
        lambda ack: None,
        supervisor_factory=lambda: case.sv,
        owns_supervisor=False,
    )
    assert "refused" not in retry and not retry.get("deduplicated"), retry
    assert len(admissions) == 2
    assert rechecks == [(admissions[0], False), (admissions[1], False)]
    assert retry["run_id"] == admissions[1] != admissions[0]
    assert len(case.registry.list_runs()) == 1
    assert len(_c1lock_receipts(case.registry, retry["run_id"])) == 1
    assert case.registry.read_claim(case.route.worktree).run_id == retry["run_id"]
    assert sql(case.registry, "SELECT count(*) AS n FROM run_keys").fetchone()["n"] == 1
    assert len(case.started) == 1

    # A keyed reattachment keeps the receipt that its admission committed.
    receipt = _c1lock_receipts(case.registry, retry["run_id"])
    again = driver.serve(
        {**frame, "launch_uuid": str(uuid.uuid4())},
        lambda ack: None,
        supervisor_factory=lambda: case.sv,
        owns_supervisor=False,
    )
    assert again.get("deduplicated") is True, again
    assert again["run_id"] == retry["run_id"]
    assert len(admissions) == 2
    assert _c1lock_receipts(case.registry, retry["run_id"]) == receipt


def test_c1_record_locator_participates_in_the_request_fingerprint(tmp_path):
    legacy = driver.request_fingerprint(
        "nspawn_run_start",
        {
            "prompt": "bounded fixture work",
            "cwd": "/var/tmp/m6-identity",
            "read_only": True,
        },
        group=None,
    )
    assert legacy == "a8d21bd4173c3a22169e062941472f2681d472511a3c12391dd7b9c176a56922"
    arguments = {
        "prompt": "change it",
        "cwd": str(tmp_path),
        "execution_class": "implementation",
        "native_write": True,
    }
    absent = driver.request_fingerprint("nspawn_run_start", arguments, group=None)
    first = driver.request_fingerprint(
        "nspawn_run_start",
        {**arguments, "red_record": ".nott/plans/p/red/u1.json"},
        group=None,
    )
    second = driver.request_fingerprint(
        "nspawn_run_start",
        {**arguments, "red_record": ".nott/plans/p/red/u1-cure1.json"},
        group=None,
    )
    assert len({absent, first, second}) == 3, (
        "the locator is identity even when neither file exists"
    )


def test_c1_keyed_reattachment_keeps_the_original_receipt(_c1lock_writer):
    case = _c1lock_writer
    bundle = _c1lock_bundle(case.root, case.head)
    arguments = {"red_record": bundle.locator, "idempotency_key": "c1lock-key"}
    first = _c1lock_start(case, **arguments)
    assert "error" not in first, ("exact bundle must admit", first)
    run_id = _payload(first)["run_id"]
    before = _c1lock_receipts(case.registry, run_id)
    assert len(before) == 1
    prepared = len(case.route.prepared)
    bundle.record.unlink()
    bundle.tests.unlink()
    retry = _c1lock_start(case, **arguments)
    assert "error" not in retry, ("reattachment must not reopen files", retry)
    assert _payload(retry)["run_id"] == run_id
    assert _c1lock_receipts(case.registry, run_id) == before
    assert len(case.route.prepared) == prepared
    assert len(case.registry.list_runs()) == 1

    other = _c1lock_bundle(case.root, case.head, "u1-cure1")
    conflict = _c1lock_start(
        case, red_record=other.locator, idempotency_key="c1lock-key"
    )
    assert conflict.get("error", {}).get("data", {}).get("type") == (
        "IdempotencyConflict"
    ), conflict
    refreshed = _c1lock_bundle(case.root, case.head)
    refreshed.record.write_bytes(b"{}")
    same_path = _c1lock_start(case, **arguments)
    assert "error" not in same_path
    assert _payload(same_path)["run_id"] == run_id
    assert _c1lock_receipts(case.registry, run_id) == before
    row = case.registry.get_run(run_id)
    case.registry.update_run(replace(row, phase=RunPhase.COMPLETED), drop_claim=True)
    _c1lock_refusal(
        _c1lock_start(case, red_record=bundle.locator, idempotency_key="c1lock-new-key")
    )
    assert len(case.registry.list_runs()) == 1


@pytest.mark.parametrize("fault", ["missing", "ancestor"])
def test_c1_retained_failover_revalidates_its_record(
    registry, tmp_path, claude_writer, monkeypatch, fault
):
    head, ancestor = _c1lock_repo(claude_writer.checkout)
    bundle = _c1lock_bundle(claude_writer.checkout, head)
    started = []
    monkeypatch.setattr(
        Supervisor,
        "_drive_claude_caged",
        lambda self, *args, **kwargs: started.append(args),
    )
    sv = _sv(registry, tmp_path)
    try:
        reply = _call(
            McpServer(supervisor=sv),
            "nspawn_run_start",
            _claude_write(
                claude_writer,
                **{
                    "provider": "writer",
                    "class": "implementation",
                    "red_record": bundle.locator,
                },
            ),
        )
        assert "error" not in reply, ("initial exact-base admission must work", reply)
        first = _payload(reply)["run_id"]
        failover, _ = sv._failovers[first]
        assert failover.admit.get("red_record") == bundle.locator
        assert failover.remaining
        row = registry.get_run(first)
        registry.update_run(replace(row, phase=RunPhase.FAILED), drop_claim=True)
        if fault == "missing":
            bundle.record.unlink()
        else:
            for witness in bundle.value["witnesses"].values():
                witness["base"] = ancestor
            bundle.record.write_text(json.dumps(bundle.value))
        prepared = len(started)
        error = None
        try:
            sv._admit_preferred(replace(failover, current=None))
        except Exception as caught:  # noqa: BLE001 - assert the exact boundary refusal below
            error = caught
        assert isinstance(error, InvalidRunRequest), error
        assert "red_record" in str(error) or "RED" in str(error)
        assert len(registry.list_runs()) == 1
        assert registry.read_claim(claude_writer.worktree) is None
        assert len(started) == prepared
    finally:
        sv.shutdown()


def test_c1_failed_receipt_write_rolls_back_the_whole_admission(
    _c1lock_writer, monkeypatch
):
    case = _c1lock_writer
    bundle = _c1lock_bundle(case.root, case.head)
    append = case.registry._append_run_event
    attempted = []

    def fail_receipt(run_id, *, at, kind, payload):
        if isinstance(payload, dict) and "writer_red" in payload and not attempted:
            attempted.append(run_id)
            raise RegistryError("fixture receipt write failed")
        return append(run_id, at=at, kind=kind, payload=payload)

    monkeypatch.setattr(case.registry, "_append_run_event", fail_receipt)
    arguments = {
        "red_record": bundle.locator,
        "idempotency_key": "c1lock-rollback",
    }
    failed = _c1lock_start(case, **arguments)
    assert attempted, (
        "receipt persistence must occur in the real admission transaction",
        failed,
    )
    assert "error" in failed
    assert case.registry.list_runs() == ()
    assert case.registry.read_claim(case.route.worktree) is None
    for table in ("run_events", "native_contexts", "claims", "run_keys"):
        assert (
            sql(case.registry, f"SELECT count(*) AS n FROM {table}").fetchone()["n"]
            == 0
        )
    assert case.started == []
    retry = _c1lock_start(case, **arguments)
    assert "error" not in retry, ("the rolled-back key must be reusable", retry)
    run_id = _payload(retry)["run_id"]
    assert len(case.registry.list_runs()) == 1
    assert len(_c1lock_receipts(case.registry, run_id)) == 1
    assert case.registry.read_claim(case.route.worktree).run_id == run_id


@pytest.mark.parametrize("corruption", ["duplicate", "unit"])
def test_c1_since_rejects_duplicate_or_inconsistent_authentic_receipts(
    _c1lock_writer, capsys, corruption
):
    case = _c1lock_writer
    bundle = _c1lock_bundle(case.root, case.head)
    reply = _c1lock_start(case, red_record=bundle.locator)
    assert "error" not in reply, ("a valid receipt must first be admitted", reply)
    run_id = _payload(reply)["run_id"]
    receipts = _c1lock_receipts(case.registry, run_id)
    assert len(receipts) == 1
    assert "u1" in list(_c1lock_atoms(receipts[0].payload["writer_red"]))
    row = case.registry.get_run(run_id)
    case.registry.update_run(replace(row, phase=RunPhase.COMPLETED), drop_claim=True)

    if corruption == "duplicate":
        event = receipts[0]
        case.registry.append_run_event(
            run_id, at=event.at, kind=event.kind, payload=event.payload
        )
    else:
        events = case.registry.read_run_events(run_id)
        trigger = case.registry._one(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = ?",
            ("run_events_append_only_delete",),
        )["sql"]
        with case.registry._transaction():
            case.registry._run("DROP TRIGGER run_events_append_only_delete")
            case.registry._run("DELETE FROM run_events WHERE run_id = ?", (run_id,))
            case.registry._run(trigger)
            for event in events:
                payload = json.loads(json.dumps(event.payload))
                if "writer_red" in payload:
                    payload["writer_red"] = _c1lock_replace_atom(
                        payload["writer_red"], "u1", "another-unit"
                    )
                    assert payload != event.payload
                case.registry._append_run_event(
                    run_id, at=event.at, kind=event.kind, payload=payload
                )
    before = case.registry.read_run_events(run_id)
    projected = _c1lock_project(run_id, capsys)
    assert projected["red_admission"] == "invalid"
    assert projected["red_receipt"] is None
    reason = projected["red_admission_reason"]
    assert isinstance(reason, str) and 0 < len(reason) <= 512
    assert "C1LOCK_PRIVATE_ASSERTION" not in json.dumps(projected)
    assert case.registry.read_run_events(run_id) == before


def _admission_case(tmp_path, run_id, *, receipt_head=None):
    """One RED-receipt admission of a writer on a real clone.

    The receipt names the clone HEAD, or `receipt_head` when it is given.
    """
    from nspawn.rows import SessionProfile, WriterRedReceipt

    root = tmp_path / "checkout"
    head, _ = _c1lock_repo(root)
    at = 1_792_800_000_000
    receipt = WriterRedReceipt(
        ".nott/plans/admit/red/u1.json",
        "a" * 64,
        ".nott/plans/admit/tests/u1.md",
        "b" * 64,
        "u1",
        head if receipt_head is None else receipt_head,
        ("admit-case",),
    )
    profile = SessionProfile(run_id, True, False, (), execution_class="implementation")
    row = RunRow(
        run_id=run_id,
        worktree=str(root),
        phase=RunPhase.PENDING,
        sandbox="workspace-write",
        model="fixture-model",
        owner_instance="driver",
        created_at=at,
        updated_at=at,
        backend=Backend.CLAUDE,
    )
    return SimpleNamespace(
        root=root, head=head, at=at, receipt=receipt, profile=profile, row=row
    )


def _admit(registry, case, *, run_key=None):
    registry.admit_run(
        case.row,
        True,
        case.at,
        100_000,
        session_profile=case.profile,
        writer_red=case.receipt,
        run_key=run_key,
    )


def _no_admission_left(registry, case):
    """A refused admission leaves no run, receipt, claim, key or context."""
    assert registry.get_run(case.row.run_id) is None
    assert registry.read_claim(str(case.root)) is None
    for table in ("runs", "run_events", "claims", "run_keys", "native_contexts"):
        count = sql(registry, f"SELECT count(*) AS n FROM {table}").fetchone()["n"]
        assert count == 0, table


@contextlib.contextmanager
def _counted_writes(registry, monkeypatch):
    """Count the top-level writes that `registry` opens in the block."""
    transaction = registry._transaction
    depth = 0
    writes: list[int] = []

    @contextlib.contextmanager
    def counted() -> Iterator[None]:
        nonlocal depth
        if depth == 0:
            writes.append(1)
        depth += 1
        try:
            with transaction():
                yield
        finally:
            depth -= 1

    with monkeypatch.context() as patch:
        patch.setattr(registry, "_transaction", counted)
        yield writes


def test_admission_git_calls_hold_no_write_lock(tmp_path, monkeypatch):
    """A slow Git observation of the RED receipt never blocks another writer.

    The fake `git` sleeps 2 s for each call. The peer waits 500 ms for the lock.
    If the admission held the write lock across its Git calls, the peer write
    would refuse with a busy-lock error. A peer heartbeat and a peer claim of
    another worktree during Git do not change the ownership of this worktree,
    so the admission must not refuse for them.
    """
    import nott_database

    case = _admission_case(tmp_path, "admit-git-outside-write")
    real_git = shutil.which("git")
    assert real_git is not None
    started = tmp_path / "git-started"
    shim = tmp_path / "slow-bin"
    shim.mkdir()
    (shim / "git").write_text(
        f"#!/bin/sh\n: > '{started}'\nsleep 2\nexec '{real_git}' \"$@\"\n"
    )
    (shim / "git").chmod(0o755)
    monkeypatch.setattr(nott_database, "BUSY_TIMEOUT_MS", 500)
    with (
        Registry(store(), "driver") as admitter,
        Registry(store(), "peer") as peer,
        ThreadPoolExecutor(max_workers=1) as worker,
    ):
        admitter.open(case.at)
        peer.open(case.at)
        monkeypatch.setenv("PATH", f"{shim}{os.pathsep}{os.environ['PATH']}")
        admission = worker.submit(_admit, admitter, case)
        deadline = time.monotonic() + 10
        while not started.exists():
            assert time.monotonic() < deadline, "the fake git did not start"
            time.sleep(0.01)
        try:
            assert peer.heartbeat_instance(case.at + 1)
            peer.claim_worktree(str(tmp_path / "other"), "run-other", case.at, 1)
        except nott_database.BusyError as error:
            pytest.fail(f"a peer write refused while admission ran git: {error}")
        admission.result(timeout=30)
        admitted = admitter.get_run(case.row.run_id)
        assert admitted is not None and admitted.phase is RunPhase.SPAWNING
        assert admitter.writer_red(case.row.run_id) == (case.receipt, case.at)
        claim = admitter.read_claim(str(case.root))
        assert claim is not None and claim.run_id == case.row.run_id


@pytest.mark.parametrize("change", ["handoff", "absent_aba"])
def test_admission_refuses_a_claim_change_during_git(tmp_path, monkeypatch, change):
    """Ownership that changed while admission read HEAD is a retryable refusal.

    The worktree has no claim before Git and no claim after it. During Git,
    run B takes the worktree, and releases it. With "handoff", B also commits
    H2 first, so the HEAD that admission read (H1) is stale. With "absent_aba",
    the HEAD does not change: only the claim generation shows that B held the
    worktree. The admission must refuse with a retryable ownership refusal and
    leave nothing. A retry with the same key observes HEAD again.
    """
    from nspawn import red_witness
    from nspawn import registry as registry_module
    from nspawn.registry import RunKey, WriterRedRefused

    case = _admission_case(tmp_path, f"admit-{change.replace('_', '-')}")
    key = RunKey("launch-change", str(case.root), "change-key", "c" * 64, "d" * 64)
    observe = red_witness.clone_head
    moved: list[str] = []
    observed: list[str] = []
    with Registry(store(), "driver") as admitter, Registry(store(), "peer") as peer:
        admitter.open(case.at)
        peer.open(case.at)

        def during_git(path):
            head = observe(path)
            observed.append(head)
            if len(observed) == 1:
                peer.claim_worktree(str(case.root), "run-b", case.at, 100_000)
                if change == "handoff":
                    _c1lock_git(case.root, "commit", "--allow-empty", "-qm", "H2")
                    moved.append(_c1lock_git(case.root, "rev-parse", "HEAD"))
                assert peer.release_claim(str(case.root), "run-b")
            return head

        monkeypatch.setattr(red_witness, "clone_head", during_git)
        with pytest.raises(registry_module.RegistryError) as refused:
            _admit(admitter, case, run_key=key)
        assert type(refused.value).__name__ == "WorktreeOwnershipChanged"
        assert refused.value.data == {"retryable": True}
        assert str(refused.value) == (
            "The worktree claim changed while admission read the clone HEAD. "
            "No run was admitted. Retry the same call."
        )
        assert observed == [case.head]
        _no_admission_left(admitter, case)

        if change == "handoff":
            # The retry reads the HEAD of the clone again, and refuses the
            # stale receipt: the H1 observation is never admitted.
            with pytest.raises(WriterRedRefused) as stale:
                _admit(admitter, case, run_key=key)
            assert stale.value.head == moved[0] != case.head
            _no_admission_left(admitter, case)
        else:
            _admit(admitter, case, run_key=key)
            assert admitter.writer_red(case.row.run_id) == (case.receipt, case.at)
            claim = admitter.read_claim(str(case.root))
            assert claim is not None and claim.run_id == case.row.run_id
        assert len(observed) == 2


@pytest.mark.parametrize("held", ["live_lease", "expired_custody"])
def test_a_held_worktree_refuses_before_git_and_any_write(tmp_path, monkeypatch, held):
    """A worktree that another run holds is refused by one read.

    The refusal comes before Git and opens no write. "expired_custody" is a
    claim whose lease ended while its run still has protected custody. After
    run B moves HEAD and its custody ends, a new admission reads HEAD again:
    no HEAD from B's interval is kept and admitted later.
    """
    from nspawn import red_witness
    from nspawn.process_identity import ProcessIdentity
    from nspawn.registry import WorktreeHeld, WriterRedRefused

    case = _admission_case(tmp_path, f"admit-held-{held.replace('_', '-')}")
    identity = ProcessIdentity(pid=7, boot_id="boot-1", start_ticks=99)
    observe = red_witness.clone_head
    observed: list[str] = []

    def counted_git(path):
        observed.append(str(path))
        return observe(path)

    with Registry(store(), "driver") as admitter, Registry(store(), "peer") as peer:
        admitter.open(case.at)
        peer.open(case.at)
        if held == "live_lease":
            peer.claim_worktree(str(case.root), "run-b", case.at, 100_000)
        else:
            peer.insert_run(
                replace(
                    case.row,
                    run_id="run-b",
                    phase=RunPhase.RUNNING,
                    owner_instance="peer",
                    process_identity=identity,
                )
            )
            peer.claim_worktree(str(case.root), "run-b", case.at - 10_000, 1)
        monkeypatch.setattr(red_witness, "clone_head", counted_git)
        with (
            _counted_writes(admitter, monkeypatch) as writes,
            pytest.raises(WorktreeHeld) as refused,
        ):
            _admit(admitter, case)
        assert writes == [], "a held worktree opened a write"
        assert observed == [], "a held worktree ran git"
        assert refused.value.run_id == "run-b"
        assert admitter.get_run(case.row.run_id) is None

        _c1lock_git(case.root, "commit", "--allow-empty", "-qm", "H2 by run B")
        moved = _c1lock_git(case.root, "rev-parse", "HEAD")
        if held == "live_lease":
            assert peer.release_claim(str(case.root), "run-b")
        else:
            assert admitter.close_owned("run-b", identity, now=case.at)
        with pytest.raises(WriterRedRefused) as stale:
            _admit(admitter, case)
        assert stale.value.head == moved != case.head
        assert observed == [str(case.root)]
        assert admitter.get_run(case.row.run_id) is None
        assert admitter.read_claim(str(case.root)) is None


def test_a_red_admission_inside_an_open_write_refuses_before_git(tmp_path, monkeypatch):
    """Git must never run while this registry holds the write lock."""
    from nspawn import red_witness

    case = _admission_case(tmp_path, "admit-inside-write")
    observed: list[str] = []
    monkeypatch.setattr(red_witness, "clone_head", lambda path: observed.append(path))
    with Registry(store(), "driver") as admitter:
        admitter.open(case.at)
        with (
            pytest.raises(RegistryError, match="open write"),
            admitter._transaction(),
        ):
            _admit(admitter, case)
        assert observed == []
        assert admitter.get_run(case.row.run_id) is None


def test_a_busy_admission_write_leaves_nothing_and_writes_once(tmp_path, monkeypatch):
    """A peer holds the store lock past the busy wait of the admission write.

    No provisional claim exists during Git, and the busy refusal leaves no run,
    receipt, claim or key, without one more cleanup write.
    """
    import nott_database
    from nott_database import BusyError

    from nspawn import red_witness
    from nspawn.registry import RunKey

    monkeypatch.setattr(nott_database, "BUSY_TIMEOUT_MS", 300)
    case = _admission_case(tmp_path, "admit-busy")
    key = RunKey("launch-busy", str(case.root), "busy-key", "c" * 64, "d" * 64)
    held = threading.Event()
    release = threading.Event()
    observe = red_witness.clone_head
    with (
        Registry(store(), "driver") as admitter,
        Registry(store(), "peer") as peer,
        ThreadPoolExecutor(max_workers=1) as worker,
    ):
        admitter.open(case.at)
        peer.open(case.at)

        def hold() -> None:
            with peer._transaction():
                held.set()
                assert release.wait(timeout=30)

        holding: list[Any] = []

        def during_git(path):
            assert admitter.read_claim(str(case.root)) is None
            holding.append(worker.submit(hold))
            assert held.wait(timeout=10), "the peer did not take the lock"
            return observe(path)

        monkeypatch.setattr(red_witness, "clone_head", during_git)
        try:
            with (
                _counted_writes(admitter, monkeypatch) as writes,
                pytest.raises(BusyError),
            ):
                _admit(admitter, case, run_key=key)
        finally:
            release.set()
            for job in holding:
                job.result(timeout=30)
        assert writes == [1]
        _no_admission_left(admitter, case)
