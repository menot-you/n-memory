"""The Codex orchestrator turn: its cage, its one private MCP and its binding.

One orchestrator turn is one Codex app-server turn admitted through nSPAWN that
reads a clean snapshot and a trusted runtime, talks to the private orchestrator
MCP (`nott_harness/orchestrator_mcp.py`) bound to one conversation session and
one memory project, and to nothing else. What fences it is the same
root-deny/minimal-read permission profile a caged reader already carries, plus a
thread configuration that names every inherited capability path closed; what
records it is one lifecycle `run_events` payload written in the SAME transaction
as the run row.

THE NATIVE SANDBOX IS NOT CLAIMED HERE. Every assertion below is on the
COMPOSED configuration — the thread start parameters, the permission profile and
the launch nSPAWN hands the vendor — because a nested sandbox cannot be started
from inside the workflow's own. The one case that asks the real `codex sandbox`
to hold this profile skips with its reason when that sandbox cannot start, and a
skip is never a pass; the installed positive/negative canaries are the trusted
operator's to run.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tomllib
import uuid
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import nott_database
import pytest
from class_dispatch import DECLARED_CLASS, system1_never_reaches_the_network
from supervisor_support import (
    _drain_worker_threads,
    _stop_every_reaper,
    _sv,
    registry,
    roster_home,
)
from test_driver import _call, _payload, _terminal, _wait_for
from test_native_environment import credential
from test_native_execute import _store_rows, _store_write

from nspawn import app_server, child, driver, native, picker, supervisor, wire
from nspawn import registry as registry_module
from nspawn.custody import ControllerRefusal
from nspawn.mcp import INVALID_PARAMS, McpServer
from nspawn.owner_roster import Defaults, NativeAccount, Provider, Roster
from nspawn.redact import SecretNeedles
from nspawn.registry import Registry, RegistryError, UniqueTaken
from nspawn.rows import Backend, NativeSessionContext, RunPhase, RunRow
from nspawn.supervisor import Supervisor
from nspawn.wire import SANDBOX_TOOL_PATH

# Export imported fixtures so pytest discovers them in this module.
__all__ = [
    "_drain_worker_threads",
    "_stop_every_reaper",
    "registry",
    "roster_home",
    "system1_never_reaches_the_network",
]


#: The five tools the private orchestrator MCP offers, spelled here rather than
#: read from the product: a sixth one arriving in `nspawn` turns this RED.
CONTRACT_TOOLS = (
    "nott_capsule_show",
    "nott_proposal_create",
    "nott_session_show",
    "nott_workflow_list",
    "nott_workflow_show",
)

#: What one orchestrator launch closes BEFORE the app-server starts, spelled
#: here rather than read from the product: the two feature gates that decide
#: whether a hook is loaded and fired at all, the plugin gate beside them, and
#: the account's own `[plugins]` table emptied, so no plugin of that home is
#: installed to carry a hook in the first place. Stated by the launch AND by the
#: composed thread.
#:
#: `projects = {}` and `mcp_servers = {}` are NOT here, and a launch that carries
#: either is RED. Measured against codex 0.159.2 (M-a, 2026-10-01): an empty
#: inline table MERGES NOTHING. With both overrides on the argv the snapshot's
#: project layer still loaded, an `[mcp_servers.X]` it declared still started
#: host-side and its `[mcp_servers.nott_orchestrator.env]` still merged into the
#: one server the turn trusts; `trust_level = "untrusted"` for the same tree, at
#: launch or on the thread, did not stop it either. What the project layer is
#: closed by now is the app-server having no project: a NEUTRAL cwd (M-b).
STARTUP_CLOSURE = {
    "features.hooks": False,
    "features.plugin_hooks": False,
    "features.plugins": False,
    "plugins": {},
}

#: The two merge no-ops, spelled so their ABSENCE from every launch is a case.
MERGE_NO_OPS = ("projects={}", "mcp_servers={}")

#: The same closure as the launch arguments it is stated with. A `-c` override is
#: part of the configuration the vendor loads before its own startup; a thread's
#: configuration is merged after it, when the account's plugins are loaded and
#: their hook trust state has been read.
STARTUP_ARGUMENTS = (
    "-c",
    "features.hooks=false",
    "-c",
    "features.plugin_hooks=false",
    "-c",
    "features.plugins=false",
    "-c",
    "plugins={}",
)

#: The fields of the private MCP's launch that an orchestrator LAUNCH declares
#: with `-c`, and the fields `config/read` reports back (M-e). Spelled here.
LAUNCH_MCP_FIELDS = ("command", "args", "env", "enabled")


def _toml(value: Any) -> str:
    """One composed value as the inline TOML a `-c` override carries.

    Rendered HERE rather than read from the product: the vendor is handed TEXT,
    and a renderer measured against itself measures nothing.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml(item) for item in value) + "]"
    assert isinstance(value, dict), value
    body = ", ".join(
        f"{json.dumps(key)} = {_toml(item)}" for key, item in value.items()
    )
    return "{" + body + "}"


def _declaration(servers: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """What ONE orchestrator launch declares with `-c`, from the composition: the
    private MCP's whole launch, and every name the account declared disabled."""
    return {
        name: (
            {key: server[key] for key in LAUNCH_MCP_FIELDS}
            if name == native.ORCHESTRATOR_MCP_NAME
            else {"enabled": False}
        )
        for name, server in sorted(servers.items())
    }


def _declaration_arguments(declaration: dict[str, dict[str, Any]]) -> tuple[str, ...]:
    """That declaration as the startup overrides one launch carries."""
    return tuple(
        argument
        for name, server in declaration.items()
        for argument in ("-c", f"mcp_servers.{name}={_toml(server)}")
    )


#: Everything a codex launch carries after the hook decision: unchanged by this
#: unit, and the reason the assertions below can be on the WHOLE argv.
CODEX_TAIL = (
    "-c",
    'cli_auth_credentials_store="file"',
    "-c",
    "features.memories=false",
    "app-server",
    "--listen",
    "stdio://",
)

#: The idempotency key of an orchestrator turn's dispatch WITHOUT a binding,
#: measured on the base before this unit existed (P0's fixed-key regression):
#: a default request's print must stay byte-identical.
UNBOUND_FINGERPRINT = "600cde01faf32547779b24441c06a04ae2f89800bc776af76ab80611fd087e22"

UNBOUND_REQUEST = {
    "prompt": "orchestrate",
    "cwd": "/snapshot",
    "provider": None,
    "model": None,
    "effort": None,
    "execution_class": "planning",
    "session_id": None,
    "timeout_ms": 0,
    "read_only": True,
    "read_paths": None,
    "system_prompt": None,
}


def _binding_request(root: Path) -> dict[str, str]:
    """The six coordinates a caller states, the REAL repository among them.

    `repository` is the session's own editable tree: where a proposal this turn
    writes belongs. It is never the run's cwd, which is the clean INSPECTION
    SNAPSHOT the turn reads — a fixture that spelled them the same path could not
    tell a binding that names the repository from one that names the snapshot.
    """
    return {
        "session": "conversation-1",
        "project": "nott",
        "repository": str(root / "repository"),
        "runtime": str(root / "runtime"),
        "database_dir": str(root / "store"),
        "interpreter": sys.executable,
    }


def _binding(root: Path, snapshot: Path, **changes: str) -> Any:
    request = {**_binding_request(root), **changes}
    return native.OrchestratorBinding.from_request(request, snapshot=str(snapshot))


# ---------------------------------------------------------------------------
# The binding itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("session", ""),
        ("session", "has\x00nul"),
        ("session", "a" * 4096),
        ("project", ""),
        ("project", "two words\n"),
        ("repository", ""),
        ("repository", "relative/repository"),
        ("repository", "/srv/project/../project"),
        ("repository", "/" + "r" * 512),
        ("runtime", "relative/runtime"),
        ("runtime", ""),
        ("database_dir", "relative/store"),
        ("interpreter", "python3"),
        ("interpreter", ""),
    ],
)
def test_an_orchestrator_coordinate_is_refused_by_its_own_name(
    tmp_path: Path, field: str, value: str
) -> None:
    with pytest.raises(native.NativePreparationError, match=field):
        _binding(tmp_path, tmp_path / "snapshot", **{field: value})


def test_an_unknown_or_missing_orchestrator_coordinate_is_refused_by_name(
    tmp_path: Path,
) -> None:
    request = _binding_request(tmp_path)
    with pytest.raises(native.NativePreparationError, match="marketplace"):
        native.OrchestratorBinding.from_request(
            {**request, "marketplace": "/elsewhere"}, snapshot=str(tmp_path)
        )
    del request["project"]
    with pytest.raises(native.NativePreparationError, match="project"):
        native.OrchestratorBinding.from_request(request, snapshot=str(tmp_path))
    # The repository is a coordinate the caller STATES, so omitting it is refused
    # by its own name rather than filled in from the cwd.
    without = _binding_request(tmp_path)
    del without["repository"]
    with pytest.raises(native.NativePreparationError, match="repository"):
        native.OrchestratorBinding.from_request(without, snapshot=str(tmp_path))


def test_the_recorded_binding_is_the_whole_binding_a_resume_must_restate(
    tmp_path: Path,
) -> None:
    """The record is the COMPLETE binding, because a resume is checked against it.

    A record of the session, the profile and the tool contract alone cannot
    answer whether a resume names the same store or the same interpreter: it was
    the gap that let a bound conversation be continued against another
    `--database-dir`, which is where a proposal is written, with the session and
    the contract matching to the byte.
    """
    binding = _binding(tmp_path, tmp_path / "snapshot")

    payload = binding.payload("nott-nspawn-orchestrator-run-1")

    assert set(payload) == {"orchestrator_binding"}
    recorded = payload["orchestrator_binding"]
    assert set(recorded) == {
        "session",
        "profile",
        "tool_contract",
        "repository",
        "snapshot",
        "runtime",
        "database_dir",
        "interpreter",
    }
    assert set(recorded) == set(registry_module.ORCHESTRATOR_BINDING_KEYS)
    assert recorded["session"] == "conversation-1"
    assert recorded["profile"] == "nott-nspawn-orchestrator-run-1"
    # Every coordinate the private MCP's own argv is built from, by name — and
    # the repository is the REAL one, beside the snapshot that is the run's cwd.
    assert recorded["repository"] == str(tmp_path / "repository")
    assert recorded["snapshot"] == str(tmp_path / "snapshot")
    assert recorded["runtime"] == str(tmp_path / "runtime")
    assert recorded["database_dir"] == str(tmp_path / "store")
    assert recorded["interpreter"] == sys.executable
    # The contract names the one server, the project it is bound to and its tools.
    assert recorded["tool_contract"] == {
        "mcp": native.ORCHESTRATOR_MCP_NAME,
        "project": "nott",
        "tools": list(CONTRACT_TOOLS),
    }
    # One payload, and it survives a JSON round trip as the registry writes it.
    assert json.loads(json.dumps(payload)) == payload
    # And the registry reads back exactly what the binding recorded.
    assert registry_module.validated_orchestrator_binding(recorded) == recorded


def test_the_private_mcp_is_bound_to_the_repository_and_reads_the_snapshot(
    tmp_path: Path,
) -> None:
    """The positive control: repository A, snapshot S, and neither stands in for
    the other.

    `--repository` is the tree the private MCP binds a proposal TO: the session's
    real, editable repository. The run's cwd is the clean INSPECTION SNAPSHOT,
    which is what the turn READS. Binding the proposals to the snapshot bound
    them to a throwaway copy — a path no later session can act on — while the
    tree the session actually edits was named nowhere on record.
    """
    repository = tmp_path / "repository"
    snapshot = tmp_path / "snapshot"

    binding = _binding(tmp_path, snapshot)

    assert binding.repository == str(repository)
    assert binding.snapshot == str(snapshot)
    server = binding.mcp_servers(str(tmp_path / "disposable"))[
        native.ORCHESTRATOR_MCP_NAME
    ]
    arguments = server["args"]
    assert arguments[arguments.index("--repository") + 1] == str(repository)
    # The snapshot is on no argument of the private MCP at all.
    assert str(snapshot) not in arguments
    # Both are recorded, under their own names.
    recorded = binding.payload("nott-nspawn-orchestrator-run-1")["orchestrator_binding"]
    assert (recorded["repository"], recorded["snapshot"]) == (
        str(repository),
        str(snapshot),
    )
    # And the repository is bound, NEVER read: the roots are the snapshot and
    # the runtime, and the editable tree is in neither.
    assert binding.read_roots == tuple(
        sorted((str(snapshot), str(tmp_path / "runtime")))
    )
    assert str(repository) not in binding.read_roots


# ---------------------------------------------------------------------------
# The composed thread start: the cage
# ---------------------------------------------------------------------------


def _composed(tmp_path: Path) -> tuple[Any, dict[str, Any]]:
    snapshot = tmp_path / "snapshot"
    binding = _binding(tmp_path, snapshot)
    params = app_server.orchestrator_thread_params(
        model="fixture",
        cwd=str(snapshot),
        profile="nott-nspawn-orchestrator-run-1",
        read_roots=binding.read_roots,
        mcp_servers=binding.mcp_servers(str(tmp_path / "disposable")),
        tool_environment=native.orchestrator_mcp_environment(
            str(tmp_path / "disposable")
        ),
    )
    return binding, params


def test_the_composed_thread_start_keeps_the_root_deny_minimal_read_cage(
    tmp_path: Path,
) -> None:
    binding, params = _composed(tmp_path)

    assert params["approvalPolicy"] == "never"
    # The profile names what is readable; a `sandbox` beside it would decide twice.
    assert "sandbox" not in params
    assert params["permissions"] == "nott-nspawn-orchestrator-run-1"
    profile = params["config"]["permissions"]["nott-nspawn-orchestrator-run-1"]
    assert profile == {
        "filesystem": {
            ":root": "deny",
            ":minimal": "read",
            str(tmp_path / "runtime"): "read",
            str(tmp_path / "snapshot"): "read",
        },
        "network": {"enabled": False},
    }
    assert (
        profile["filesystem"]
        == app_server.permission_profile(binding.read_roots)["filesystem"]
    )
    # The REAL repository is bound, never readable: it is what the private MCP
    # writes a proposal to, and a turn that could read it is a turn reading the
    # tree its own session is editing.
    assert str(tmp_path / "repository") not in profile["filesystem"]
    # Nothing writable anywhere, and the network off on both paths that carry it.
    assert "write" not in profile["filesystem"].values()
    assert params["config"]["sandbox_workspace_write.network_access"] is False
    assert params["config"]["default_permissions"] == params["permissions"]


@pytest.mark.parametrize(
    "path",
    [
        "browser_use",
        "browser_use_external",
        "in_app_browser",
        "computer_use",
        "code_mode",
        "code_mode_host",
        "code_mode_only",
        "multi_agent",
        "enable_fanout",
        "hooks",
        "plugin_hooks",
        "plugins",
        "apps",
        "enable_mcp_apps",
        "standalone_web_search",
        "search_tool",
        "memories",
    ],
)
def test_the_composed_thread_start_closes_every_inherited_capability(
    tmp_path: Path, path: str
) -> None:
    _, params = _composed(tmp_path)

    assert params["config"][f"features.{path}"] is False


def test_the_composed_thread_start_closes_web_hooks_and_the_vendor_orchestrator(
    tmp_path: Path,
) -> None:
    _, params = _composed(tmp_path)
    config = params["config"]

    assert config["tools.web_search"] is False
    assert config["web_search"] == "disabled"
    # The vendor's OWN orchestrator MCP and skills, which are not this one.
    assert config["orchestrator.mcp.enabled"] is False
    assert config["orchestrator.skills.enabled"] is False
    assert config["plugins"] == {}
    assert config["notify"] == []
    assert config["shell_environment_policy.inherit"] == "none"
    assert params["dynamicTools"] == []
    assert params["selectedCapabilityRoots"] == []
    assert params["environments"] == []
    assert params["allowProviderModelFallback"] is False
    # Every closed feature the app-server knows is named, plus code mode, which
    # `child.app_server_arguments` deliberately ENABLES for the Claude cage.
    assert set(app_server.ORCHESTRATOR_CLOSED_FEATURES) == set(
        wire.DISABLED_CAPABILITY_FEATURES
    ) | {"code_mode", "code_mode_host"}


# ---------------------------------------------------------------------------
# The composed thread start: exactly one MCP
# ---------------------------------------------------------------------------


def test_the_composed_thread_start_lists_exactly_the_private_mcp(
    tmp_path: Path,
) -> None:
    _, params = _composed(tmp_path)

    servers = params["config"]["mcp_servers"]
    assert list(servers) == [native.ORCHESTRATOR_MCP_NAME]
    assert "nmemory" not in servers and "nspawn" not in servers
    server = servers[native.ORCHESTRATOR_MCP_NAME]
    assert server["command"] == sys.executable
    assert server["args"] == [
        "-I",
        "-m",
        "nott_harness.orchestrator_mcp",
        "--session",
        "conversation-1",
        "--project",
        "nott",
        "--repository",
        str(tmp_path / "repository"),
        "--runtime-root",
        str(tmp_path / "runtime"),
        "--database-dir",
        str(tmp_path / "store"),
    ]
    assert server["enabled"] is True
    assert server["enabled_tools"] == list(CONTRACT_TOOLS)
    # Nothing is forwarded from this process by name.
    assert server["env_vars"] == []
    # And a composer that was handed a second server refuses to compose at all.
    binding = _binding(tmp_path, tmp_path / "snapshot")
    with pytest.raises(app_server.AppServerError, match="exactly the private"):
        app_server.orchestrator_thread_params(
            model="fixture",
            cwd=str(tmp_path / "snapshot"),
            profile="nott-nspawn-orchestrator-run-1",
            read_roots=binding.read_roots,
            mcp_servers={
                **binding.mcp_servers(str(tmp_path / "disposable")),
                "nmemory": {"command": "/usr/bin/nmemory", "enabled": True},
            },
            tool_environment=native.orchestrator_mcp_environment(
                str(tmp_path / "disposable")
            ),
        )


def test_the_composed_thread_start_names_every_inherited_server_closed(
    tmp_path: Path,
) -> None:
    """A mapping that merely omits an inherited server is an OVERLAY.

    The vendor merges the thread config over the config file its `CODEX_HOME`
    holds, so a server that account declares and this mapping never mentions
    keeps its own definition and starts — before any live catalog check can
    refuse the turn. Every inherited name is therefore composed, closed, and a
    name left enabled is not composed at all.
    """
    binding = _binding(tmp_path, tmp_path / "snapshot")
    disposable = str(tmp_path / "disposable")

    def compose(**servers: dict[str, Any]) -> dict[str, Any]:
        return app_server.orchestrator_thread_params(
            model="fixture",
            cwd=str(tmp_path / "snapshot"),
            profile="nott-nspawn-orchestrator-run-1",
            read_roots=binding.read_roots,
            mcp_servers={**servers, **binding.mcp_servers(disposable)},
            tool_environment=native.orchestrator_mcp_environment(disposable),
        )

    servers = compose(nmemory={"enabled": False})["config"]["mcp_servers"]

    assert set(servers) == {"nmemory", native.ORCHESTRATOR_MCP_NAME}
    assert servers["nmemory"] == {"enabled": False}
    assert [
        name for name, server in servers.items() if server.get("enabled", True)
    ] == [native.ORCHESTRATOR_MCP_NAME]
    # An inherited name left enabled, or carrying anything but its own closure.
    with pytest.raises(app_server.AppServerError, match="exactly the private"):
        compose(nmemory={"command": "/usr/bin/nmemory", "enabled": True})
    with pytest.raises(app_server.AppServerError, match="exactly the private"):
        compose(nmemory={"enabled": False, "command": "/usr/bin/nmemory"})
    with pytest.raises(app_server.AppServerError, match="exactly the private"):
        compose(nmemory={})


def test_the_private_mcp_runs_an_interpreter_the_snapshot_cannot_shadow(
    tmp_path: Path,
) -> None:
    """The flags before `-m` are the whole defence, so they are MEASURED.

    The app-server runs IN the snapshot, and `python -m` prepends the working
    directory to `sys.path` before any installed package. A snapshot carrying
    `nott_harness/orchestrator_mcp.py` would then BE the private MCP — project
    code, launched as the one server the turn trusts, offering the five names
    the contract check looks for. `--runtime-root` cannot help: it is read by
    the module that import already chose.
    """
    _, params = _composed(tmp_path)
    args = params["config"]["mcp_servers"][native.ORCHESTRATOR_MCP_NAME]["args"]
    flags = args[: args.index("-m")]
    snapshot = tmp_path / "shadow"
    snapshot.mkdir()
    (snapshot / "nott_shadow_probe.py").write_text("print('the snapshot ran')\n")

    def run(*argv: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, *argv, "-m", "nott_shadow_probe"],
            cwd=snapshot,
            env={"PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

    isolated = run(*flags)

    assert isolated.returncode != 0, isolated.stdout
    assert "No module named" in isolated.stderr, isolated.stderr
    # The control: without those flags the very same interpreter runs the plant.
    shadowed = run()
    assert shadowed.returncode == 0, shadowed.stderr
    assert shadowed.stdout.strip() == "the snapshot ran"


def test_the_private_mcp_environment_carries_nothing_but_explicit_paths(
    tmp_path: Path,
) -> None:
    _, params = _composed(tmp_path)
    disposable = tmp_path / "disposable"

    environment = params["config"]["mcp_servers"][native.ORCHESTRATOR_MCP_NAME]["env"]

    assert environment == {
        "PATH": SANDBOX_TOOL_PATH,
        "HOME": str(disposable / "home"),
        "TMPDIR": str(disposable / "tmp"),
        "XDG_CACHE_HOME": str(disposable / "cache"),
        # `env` is applied OVER the five names the codex app-server hands every
        # MCP child of its own accord (`HOME LANG PATH SHELL USER`), so the three
        # it does not already name are named here: an `env` that leaves one out
        # is not that child's whole environment, it is an overlay on this
        # process's. `USER` is not among them because nothing upstream has one
        # to forward (test_the_orchestrator_launch_environment_is_explicit).
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "SHELL": "/bin/sh",
    }
    # An account home, a live store, an owner or controller socket and the
    # session bus each record or reach something this turn does not own.
    for absent in (
        "CODEX_HOME",
        "CODEX_SQLITE_HOME",
        nott_database.DIRECTORY_VARIABLE,
        "DBUS_SESSION_BUS_ADDRESS",
        "XDG_RUNTIME_DIR",
        "CLAUDE_CODE_MESSAGING_SOCKET",
        "NOTT_NMEMORY_SESSION_ID",
        "NOTT_NSPAWN_CONFIG",
        "NOTT_WORKFLOW_ROOT",
        "USER",
        "LOGNAME",
    ):
        assert absent not in environment, absent
    # The tools of the turn itself see the same closed set and nothing else.
    assert params["config"]["shell_environment_policy.include_only"] == [
        "PATH",
        "HOME",
        "TMPDIR",
        "XDG_CACHE_HOME",
        "LANG",
        "LC_ALL",
        "SHELL",
    ]


def _entry(name: str, tools: Any = (), **changes: Any) -> dict[str, Any]:
    return {
        "name": name,
        "tools": {tool: {} for tool in tools},
        "resources": [],
        "resourceTemplates": [],
        **changes,
    }


def _composed_server(tmp_path: Path) -> dict[str, Any]:
    """The private MCP exactly as the composition hands it to the vendor."""
    binding = _binding(tmp_path, tmp_path / "snapshot")
    disposable = str(tmp_path / "disposable")
    return binding.mcp_servers(disposable)[native.ORCHESTRATOR_MCP_NAME]


def _private(tmp_path: Path, **launch: Any) -> dict[str, Any]:
    """The vendor's EFFECTIVE entry for the private MCP: the contract's tools and
    the launch it reports having performed, which a faithful vendor reports as
    the composition gave it, and a merged configuration layer does not."""
    server = _composed_server(tmp_path)
    return _entry(
        native.ORCHESTRATOR_MCP_NAME,
        CONTRACT_TOOLS,
        **{
            "command": server["command"],
            "args": list(server["args"]),
            "env": dict(server["env"]),
            **launch,
        },
    )


def test_the_tool_contract_refuses_a_status_the_binding_does_not_name(
    tmp_path: Path,
) -> None:
    binding = _binding(tmp_path, tmp_path / "snapshot")
    composed = _composed_server(tmp_path)
    bound = [_private(tmp_path)]

    assert (
        app_server.orchestrator_mcp_violation(
            binding, bound, frozenset(), composed=composed
        )
        is None
    )
    # A second server, a tool outside the contract, a resource, or nothing at all.
    assert app_server.orchestrator_mcp_violation(
        binding, [*bound, _entry("nmemory")], frozenset(), composed=composed
    ) == ("native orchestrator MCP catalog carries a server the binding does not name")
    widened = [_private(tmp_path)]
    widened[0]["tools"]["nmemory_add"] = {}
    assert app_server.orchestrator_mcp_violation(
        binding, widened, frozenset(), composed=composed
    ) == ("native orchestrator MCP exposes a tool outside its contract")
    resourced = [{**bound[0], "resources": ["file:///etc"]}]
    assert app_server.orchestrator_mcp_violation(
        binding, resourced, frozenset(), composed=composed
    ) == ("native orchestrator MCP exposes a resource outside its contract")
    assert app_server.orchestrator_mcp_violation(
        binding, [], frozenset(), composed=composed
    ) == ("native orchestrator MCP is not available to the turn")


@pytest.mark.parametrize("offered", [(), ("nott_session_show",), CONTRACT_TOOLS[:4]])
def test_the_tool_contract_refuses_a_catalog_that_lost_a_contract_tool(
    tmp_path: Path, offered: tuple[str, ...]
) -> None:
    """The recorded contract is five tools, so five is what must be there.

    A turn admitted on a contract naming `nott_proposal_create` and then started
    against a server offering four tools, or none, has no proposal to create:
    accepting it would record an orchestration the turn could not perform. So the
    observed set must EQUAL the contract, not merely stay inside it.
    """
    binding = _binding(tmp_path, tmp_path / "snapshot")
    short = _private(tmp_path)
    short["tools"] = {tool: {} for tool in offered}

    violation = app_server.orchestrator_mcp_violation(
        binding, [short], frozenset(), composed=_composed_server(tmp_path)
    )

    assert (
        violation == "native orchestrator MCP is missing a tool its contract requires"
    )


def test_a_server_the_binding_named_closed_may_be_listed_but_not_live(
    tmp_path: Path,
) -> None:
    """The vendor lists a disabled server with no tools, and that is not a breach:
    `_verify_readonly_mcp` already reads such entries on the writer's path. One
    that came up with a tool anyway did start, and ends the turn."""
    binding = _binding(tmp_path, tmp_path / "snapshot")
    composed = _composed_server(tmp_path)
    bound = [_private(tmp_path)]
    closed = frozenset({"nmemory"})

    assert (
        app_server.orchestrator_mcp_violation(
            binding, [*bound, _entry("nmemory")], closed, composed=composed
        )
        is None
    )
    assert app_server.orchestrator_mcp_violation(
        binding,
        [*bound, _entry("nmemory", ("nmemory_capture",))],
        closed,
        composed=composed,
    ) == ("native orchestrator turn reached a server the binding named closed")
    assert app_server.orchestrator_mcp_violation(
        binding,
        [*bound, _entry("nmemory", resources=["file:///etc"])],
        closed,
        composed=composed,
    ) == ("native orchestrator turn reached a server the binding named closed")


#: What a merged project configuration layer did to the private MCP's launch:
#: `[mcp_servers.nott_orchestrator.env]` in the snapshot's own `.codex/config.toml`
#: reached the server's environment, under the name the turn trusts, with the
#: contract's five tools answering as before.
INJECTED = {"INJECTED_BY_PROJECT": "yes"}


def test_the_effective_mcp_launch_is_compared_against_the_composed_one(
    tmp_path: Path,
) -> None:
    """Names and tools are not the boundary; the launch is.

    A server named `nott_orchestrator`, offering exactly the contract's five
    tools, is what the project layer's injection produced: the only thing that
    differed was the environment the vendor actually started it with. So the
    effective list is compared against the composed command, the composed args
    and the composed environment, field by field.
    """
    binding = _binding(tmp_path, tmp_path / "snapshot")
    composed = _composed_server(tmp_path)

    def violation(**launch: Any) -> str | None:
        return app_server.orchestrator_mcp_violation(
            binding, [_private(tmp_path, **launch)], frozenset(), composed=composed
        )

    assert violation() is None
    # The defect itself: one name merged into the environment of the one server.
    assert violation(env={**composed["env"], **INJECTED}) == (
        "native orchestrator MCP was started with an environment the "
        "composition does not name"
    )
    # A preload is the same breach, and so is an environment short of one name.
    assert violation(env={**composed["env"], "LD_PRELOAD": "/snapshot/x.so"}) == (
        "native orchestrator MCP was started with an environment the "
        "composition does not name"
    )
    assert violation(env={}) is not None
    # Another interpreter, or another module, under the same name and tools.
    assert violation(command="/usr/bin/python3") == (
        "native orchestrator MCP was started with a command the composition "
        "does not name"
    )
    assert violation(args=[*composed["args"], "--extra"]) == (
        "native orchestrator MCP was started with a command the composition "
        "does not name"
    )
    assert violation(args=composed["args"][2:]) is not None
    # And an entry that states NO launch is the real vendor's own answer, so it
    # is not a refusal: see
    # test_the_status_list_cannot_stand_for_the_effective_launch.
    bare = _entry(native.ORCHESTRATOR_MCP_NAME, CONTRACT_TOOLS)
    assert (
        app_server.orchestrator_mcp_violation(
            binding, [bare], frozenset(), composed=composed
        )
        is None
    )


def test_the_status_list_cannot_stand_for_the_effective_launch(
    tmp_path: Path,
) -> None:
    """THE BEHAVIOURAL RED OF M-c, at the boundary it was measured on.

    Measured against codex 0.159.2 (M-c, 2026-10-01): an `mcpServerStatus/list`
    entry carries NO command, args or env. Its whole key set is `name`,
    `runtimeStatus`, `pluginId`, `httpOrigin`, `serverInfo`, `serverCapabilities`,
    `toolsError`, `resources`, `resourceTemplates`, `authStatus` and `tools`. So a
    check that reads the effective LAUNCH out of that list answers "states no
    launch for the private MCP" for EVERY orchestrator turn, the clean one
    included, and refuses them all.

    The status list therefore stands for names, tools and resources alone — an
    ADDITIONAL check — and the launch is validated where the vendor does state it:
    `config/read`, before any thread exists
    (test_the_effective_configuration_is_the_launch_boundary).
    """
    binding = _binding(tmp_path, tmp_path / "snapshot")
    composed = _composed_server(tmp_path)
    # The real entry, key for key, as the integrator measured it.
    measured = {
        "name": native.ORCHESTRATOR_MCP_NAME,
        "runtimeStatus": "connected",
        "pluginId": None,
        "httpOrigin": None,
        "serverInfo": {"name": "nott-orchestrator", "version": "0"},
        "serverCapabilities": {"tools": {}},
        "toolsError": None,
        "resources": [],
        "resourceTemplates": [],
        "authStatus": None,
        "tools": {tool: {} for tool in CONTRACT_TOOLS},
    }
    assert not {"command", "args", "env"} & set(measured)

    assert app_server.orchestrator_effective_launch(measured) is None
    assert (
        app_server.orchestrator_mcp_violation(
            binding, [measured], frozenset(), composed=composed
        )
        is None
    )
    # And the contract checks it DOES answer for still answer.
    assert (
        app_server.orchestrator_mcp_violation(
            binding,
            [{**measured, "tools": {}}],
            frozenset(),
            composed=composed,
        )
        == "native orchestrator MCP is missing a tool its contract requires"
    )


def test_the_effective_launch_is_read_wherever_the_entry_states_it(
    tmp_path: Path,
) -> None:
    """The vendor may state the launch at the entry's top level or inside it; a
    `command` is what identifies it, never a field name this repository invented."""
    binding = _binding(tmp_path, tmp_path / "snapshot")
    composed = _composed_server(tmp_path)
    inner = _entry(
        native.ORCHESTRATOR_MCP_NAME,
        CONTRACT_TOOLS,
        config={
            "command": composed["command"],
            "args": list(composed["args"]),
            "env": dict(composed["env"]),
        },
    )

    assert (
        app_server.orchestrator_mcp_violation(
            binding, [inner], frozenset(), composed=composed
        )
        is None
    )
    injected = _entry(
        native.ORCHESTRATOR_MCP_NAME,
        CONTRACT_TOOLS,
        config={
            "command": composed["command"],
            "args": list(composed["args"]),
            "env": {**composed["env"], **INJECTED},
        },
    )
    assert app_server.orchestrator_mcp_violation(
        binding, [injected], frozenset(), composed=composed
    ) == (
        "native orchestrator MCP was started with an environment the "
        "composition does not name"
    )


# ---------------------------------------------------------------------------
# The effective configuration: the boundary, read before any thread exists
# ---------------------------------------------------------------------------


def _declared(tmp_path: Path, **closed: Any) -> dict[str, dict[str, Any]]:
    """What ONE orchestrator launch declares with `-c`, from the composition."""
    binding = _binding(tmp_path, tmp_path / "snapshot")
    return _declaration({**closed, **binding.mcp_servers(str(tmp_path / "disposable"))})


#: The layers a NEUTRAL cwd leaves (M-b/M-e): session flags, the account's user
#: layer and the system one. No project layer, because there is no project.
NEUTRAL_LAYERS = [
    {"name": {"type": "sessionFlags"}, "disabledReason": None},
    {"name": {"type": "user"}, "disabledReason": None},
    {"name": {"type": "system"}, "disabledReason": None},
]


def _read(declared: dict[str, dict[str, Any]], **changes: Any) -> dict[str, Any]:
    """`config/read {cwd, includeLayers: true}` as a faithful vendor answers it:
    `{config, layers, origins}`, the config's `[mcp_servers]` AS MERGED (M-d)."""
    return {
        "config": {"mcp_servers": {name: dict(s) for name, s in declared.items()}},
        "layers": [dict(layer) for layer in NEUTRAL_LAYERS],
        "origins": {},
        **changes,
    }


def test_the_effective_configuration_is_the_launch_boundary(tmp_path: Path) -> None:
    """The vendor's OWN effective view of the launch, validated before a thread.

    `config/read {cwd, includeLayers: true}` is the one place the vendor states
    the launch it will perform, and it states it BEFORE any MCP child exists
    (M-e): the private server's command, its arguments and its environment AS
    MERGED, each layer by type, and every other server's `enabled`. So this is
    where the composition is checked, and the check is an EQUALITY on the
    environment — the defect was one account-layer `[mcp_servers.X.env]` key
    merging into the one server the turn trusts (M-e), with the name, the command
    and the arguments all still right.
    """
    declared = _declared(tmp_path, nmemory={"enabled": False})
    private = native.ORCHESTRATOR_MCP_NAME

    def violation(result: Any) -> str | None:
        return app_server.orchestrator_config_violation(result, declared=declared)

    # The positive control: a clean neutral launch, answered faithfully, PASSES.
    assert violation(_read(declared)) is None

    def merged(**fields: Any) -> dict[str, Any]:
        answer = _read(declared)
        answer["config"]["mcp_servers"][private].update(fields)
        return answer

    # (b) EXACTLY the composed env: one extra merged key refuses, and so does one
    # missing. An account layer's own `[mcp_servers.nott_orchestrator.env]` is
    # what merged here (M-e), and a project layer's was the original defect.
    composed = declared[private]
    assert violation(merged(env={**composed["env"], **INJECTED})) == (
        "native orchestrator effective MCP env is not the composed one"
    )
    assert violation(merged(env={**composed["env"], "LD_PRELOAD": "/x.so"})) == (
        "native orchestrator effective MCP env is not the composed one"
    )
    assert violation(merged(env={})) is not None
    assert violation(merged(command="/usr/bin/python3")) == (
        "native orchestrator effective MCP command is not the composed one"
    )
    assert violation(merged(args=[*composed["args"], "--extra"])) == (
        "native orchestrator effective MCP args are not the composed ones"
    )
    assert violation(merged(enabled=False)) is not None


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("project-layer", "loaded a project configuration layer"),
        ("no-layers", "states no configuration layers"),
        ("layers-not-a-list", "states no configuration layers"),
        ("layer-without-type", "states no type"),
        ("layer-not-an-object", "is not an object"),
        ("no-config", "answered no config"),
        ("no-servers", "states no mcp_servers"),
        ("no-private", "states no private MCP"),
        ("private-not-an-object", "states no private MCP"),
        ("args-not-a-list", "args are not the composed ones"),
        ("env-not-an-object", "env is not the composed one"),
        ("another-enabled", "leaves another server enabled"),
        ("another-undeclared", "does not declare"),
        ("not-an-object", "answered no object"),
        ("none", "answered no object"),
    ],
)
def test_an_effective_configuration_this_reader_cannot_vouch_for_refuses(
    tmp_path: Path, case: str, expected: str
) -> None:
    """FAIL CLOSED. A layer of type `project`, a shape this reader does not
    recognise, a field missing or of another type — each is a refusal naming the
    field, never a pass. An answer nobody could validate proves nothing, and the
    whole defence of this turn is that no project layer and no second launch can
    reach it unobserved."""
    declared = _declared(tmp_path, nmemory={"enabled": False})
    private = native.ORCHESTRATOR_MCP_NAME
    result: Any = _read(declared)
    match case:
        case "project-layer":
            result["layers"].append(
                {"name": {"type": "project"}, "disabledReason": None}
            )
        case "no-layers":
            result["layers"] = []
        case "layers-not-a-list":
            result["layers"] = {"project": {}}
        case "layer-without-type":
            result["layers"][1] = {"name": {}, "disabledReason": None}
        case "layer-not-an-object":
            result["layers"][1] = "user"
        case "no-config":
            del result["config"]
        case "no-servers":
            result["config"] = {}
        case "no-private":
            del result["config"]["mcp_servers"][private]
        case "private-not-an-object":
            result["config"]["mcp_servers"][private] = "yes"
        case "args-not-a-list":
            result["config"]["mcp_servers"][private]["args"] = "-I"
        case "env-not-an-object":
            result["config"]["mcp_servers"][private]["env"] = []
        case "another-enabled":
            result["config"]["mcp_servers"]["nmemory"] = {"enabled": True}
        case "another-undeclared":
            result["config"]["mcp_servers"]["nelsewhere"] = {"enabled": False}
        case "not-an-object":
            result = ["config"]
        case "none":
            result = None

    violation = app_server.orchestrator_config_violation(result, declared=declared)

    assert violation is not None, case
    assert expected in violation, violation
    # A refusal names the field or the server, never an effective value of either.
    for secret in (*declared[private]["env"].values(), declared[private]["command"]):
        assert secret not in violation, violation


def test_the_composed_thread_restates_the_launch_declaration_and_nothing_more(
    tmp_path: Path,
) -> None:
    """Every MCP of this turn is declared at LAUNCH and validated there, so the
    thread may restate that declaration and narrow the private server to its tool
    contract — and may not add a server or change one field of one."""
    declared = _declared(tmp_path, nmemory={"enabled": False})
    binding = _binding(tmp_path, tmp_path / "snapshot")
    thread = {
        "nmemory": {"enabled": False},
        **binding.mcp_servers(str(tmp_path / "disposable")),
    }
    private = native.ORCHESTRATOR_MCP_NAME

    assert app_server.orchestrator_thread_restatement(thread, declared) is None
    # The two keys a thread may add: the tool contract of the private server.
    assert set(thread[private]) - set(declared[private]) == {
        "env_vars",
        "enabled_tools",
    }
    assert (
        app_server.orchestrator_thread_restatement(
            {**thread, "nelsewhere": {"enabled": False}}, declared
        )
        == "a native orchestrator thread names a server the launch does not declare"
    )
    assert (
        app_server.orchestrator_thread_restatement(
            {name: s for name, s in thread.items() if name != "nmemory"}, declared
        )
        is not None
    )
    assert (
        app_server.orchestrator_thread_restatement(
            {**thread, private: {**thread[private], "command": "/usr/bin/python3"}},
            declared,
        )
        == f"a native orchestrator thread changes the MCP server {private!r}"
    )
    assert (
        app_server.orchestrator_thread_restatement(
            {**thread, "nmemory": {"enabled": False, "startup_timeout_sec": 1}},
            declared,
        )
        == "a native orchestrator thread adds a field to the MCP server 'nmemory'"
    )


# ---------------------------------------------------------------------------
# The launch: only the snapshot and the runtime are readable
# ---------------------------------------------------------------------------


@pytest.fixture
def installed_route(tmp_path: Path) -> SimpleNamespace:
    """A codex route with the owner's INSTALLED plugin: a skill tree a caged
    reader may read, and an `.mcp.json` declaring nmemory.

    Both are what an orchestrator turn must NOT inherit, so the fixture carries
    them: a roster with neither could not tell a turn that closed them from one
    that had nothing to close.
    """
    home = tmp_path / "account"
    home.mkdir(mode=0o700)
    credential(home, "ORCHESTRATOR-OAUTH-SECRET-FOR-TESTING")
    (home / "config.toml").write_text('[plugins."fixture@local"]\nenabled=true\n')
    source = tmp_path / "plugin"
    cache = home / "plugins/cache/local/fixture/v1"
    manifest = json.dumps({"name": "fixture", "version": "v1", "skills": "./skills/"})
    for root in (source, cache):
        (root / ".codex-plugin").mkdir(parents=True)
        (root / ".codex-plugin/plugin.json").write_text(manifest)
        (root / "skills/readback").mkdir(parents=True)
        (root / "skills/readback/SKILL.md").write_text("Read the request.\n")
    (source / ".mcp.json").write_text(
        '{"mcpServers": {"nmemory": {"command": "/fixture/nmemory"}}}'
    )
    binary = tmp_path / "codex-cli"
    binary.write_text("#!/bin/sh\nexit 99\n")
    binary.chmod(0o700)
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (tmp_path / "runtime").mkdir()
    provider = Provider(
        name="codex",
        kind="codex",
        bin=binary,
        model="luna",
        models=["luna"],
        accounts={"one": NativeAccount(home)},
    )
    return SimpleNamespace(
        provider=provider,
        roster=Roster(defaults=Defaults(plugin_root=source)),
        source=source,
        snapshot=snapshot,
        runtime=tmp_path / "runtime",
        skills=cache / "skills",
        account=home,
        config=home / "config.toml",
        disposable=tmp_path / "runs/a-run/disposable",
        binding=_binding(tmp_path, snapshot),
    )


#: An ambient a real host has: the owner's own home and user, the session bus,
#: the controller's runtime directory, the live store and the roster's
#: coordinates. An unbound caged reader carries most of it by design, which is
#: what makes the orchestrator turn's own environment measurable.
LOUD_AMBIENT = {
    "PATH": "/usr/sbin:/usr/bin:/bin",
    "HOME": "/home/owner",
    "USER": "owner",
    "LOGNAME": "owner",
    "LANG": "en_GB.UTF-8",
    "TERM": "xterm-256color",
    "TMPDIR": "/home/owner/tmp",
    "XDG_CACHE_HOME": "/home/owner/.cache",
    "XDG_CONFIG_HOME": "/home/owner/.config",
    "XDG_RUNTIME_DIR": "/run/user/1000",
    "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
    "NOTT_DATABASE_DIR": "/home/owner/.nott/store",
    "NOTT_NSPAWN_CONFIG": "/home/owner/.nott/nspawn.toml",
    "NOTT_NSPAWN_ROSTER": "/home/owner/.nott/roster.toml",
    "NOTT_NMEMORY_CONFIG": "/home/owner/.nott/nmemory.toml",
    "SSL_CERT_FILE": "/etc/ssl/cert.pem",
}


def _prepared(route: SimpleNamespace, **changes: Any) -> native.NativeLaunch:
    arguments: dict[str, Any] = {
        "roster": route.roster,
        "cwd": route.snapshot,
        "read_only": True,
        "ambient": {"HOME": "/owner", "PATH": "/usr/bin:/bin"},
        **changes,
    }
    return native.prepare_native(route.provider, "one", **arguments)


def _orchestrated(route: SimpleNamespace, **changes: Any) -> native.NativeLaunch:
    return _prepared(
        route,
        orchestrator=route.binding,
        disposable_root=str(route.disposable),
        **changes,
    )


def test_the_orchestrator_launch_reads_only_the_snapshot_and_the_runtime(
    installed_route: SimpleNamespace,
) -> None:
    # The positive control: an UNBOUND caged reader on this very route reads the
    # installed skill tree and inherits the account's nMEMORY declaration.
    unbound = _prepared(installed_route)
    assert str(installed_route.skills) in (unbound.context.read_roots or ())
    assert list(unbound.mcp_servers) == ["nmemory"]

    launch = _orchestrated(installed_route)

    assert launch.context.read_roots == tuple(
        sorted(
            (
                os.path.realpath(installed_route.snapshot),
                os.path.realpath(installed_route.runtime),
                str(installed_route.provider.bin.resolve()),
            )
        )
    )
    # The vendor's own skill trees stay out, and the plugin's nmemory is not
    # carried: the launch loads no plugin, and closing a name the account's
    # config never declares is a server with no transport (see
    # test_every_server_the_orchestrator_launch_names_keeps_a_transport).
    assert launch.plugin_skill_roots == {}
    assert launch.mcp_servers == {}


def test_the_orchestrator_read_roots_are_the_snapshot_runtime_and_codex_only(
    installed_route: SimpleNamespace, tmp_path: Path
) -> None:
    """EXACTLY three roots, asserted as a set: the snapshot, the runtime and the
    canonical codex executable.

    The REAL repository is bound and never granted. A turn that could read the
    tree its own session is editing would read the working copy it was given a
    clean snapshot of precisely so it could not; a turn that could WRITE it would
    be the editor nothing in this cage is. So the repository exists here, with a
    file in it, and is in neither the read roots nor the permission profile.
    """
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "working-copy.txt").write_text("the editable tree\n")

    launch = _orchestrated(installed_route)

    assert launch.context.read_roots is not None
    assert set(launch.context.read_roots) == {
        os.path.realpath(installed_route.snapshot),
        os.path.realpath(installed_route.runtime),
        str(installed_route.provider.bin.resolve()),
    }
    assert str(repository) not in launch.context.read_roots
    # And the profile composed from the binding grants it nothing either, read
    # or write: `:root` is denied and the repository is named by no entry.
    profile = app_server.permission_profile(installed_route.binding.read_roots)
    assert str(repository) not in profile["filesystem"]
    assert profile["filesystem"][":root"] == "deny"
    assert "write" not in profile["filesystem"].values()


def test_an_orchestrator_launch_refuses_a_read_path_beside_its_binding(
    installed_route: SimpleNamespace, tmp_path: Path
) -> None:
    other = tmp_path / "elsewhere"
    other.mkdir()

    with pytest.raises(native.NativePreparationError, match="read_paths"):
        _orchestrated(installed_route, read_paths=(str(other),))


def test_an_orchestrator_launch_closes_the_accounts_own_declarations(
    installed_route: SimpleNamespace,
) -> None:
    """An account-level `[mcp_servers]` table is what is inherited.

    `_codex_mcp_servers` projects the PLUGIN's `.mcp.json` only, so a server the
    account itself declares was never in the mapping at all — the exact name
    that survives the vendor's own config merge.
    """
    installed_route.config.write_text(
        '[plugins."fixture@local"]\nenabled=true\n'
        '[mcp_servers.helper]\ncommand="/usr/bin/helper"\n'
    )

    launch = _orchestrated(installed_route)

    assert launch.mcp_servers == {"helper": {"enabled": False}}


def test_every_server_the_orchestrator_launch_names_keeps_a_transport(
    installed_route: SimpleNamespace,
) -> None:
    """A closure codex can parse: each `-c mcp_servers.<name>` merged over the
    account's own `config.toml` still names a `command` or a `url`.

    MEASURED 2026-10-02 on codex-cli 0.159.3: `-c 'mcp_servers.x={enabled=false}'`
    over a home that declares no `x` exits at startup with `invalid transport in
    mcp_servers.x` (nERR-2003, run 4b5b20b8). The installed plugin's `.mcp.json`
    is not that home's configuration, and the launch loads no plugin anyway.
    """
    installed_route.config.write_text(
        '[plugins."fixture@local"]\nenabled=true\n'
        '[mcp_servers.helper]\ncommand="/usr/bin/helper"\n'
    )

    launch = _orchestrated(installed_route)

    account = tomllib.loads(installed_route.config.read_text())["mcp_servers"]
    for name, server in _launch_mcp(launch.argv).items():
        merged = {**account.get(name, {}), **server}
        assert {"command", "url"} & set(merged), name


def test_an_account_server_without_a_transport_is_refused_by_name(
    installed_route: SimpleNamespace,
) -> None:
    """Closing it would hand codex a server with no `command` or `url`, which it
    refuses at startup; preparation refuses it first, naming the server."""
    installed_route.config.write_text("[mcp_servers.helper]\nenabled=false\n")

    with pytest.raises(native.NativePreparationError, match="'helper'"):
        _orchestrated(installed_route)


def test_a_plugin_http_declaration_of_the_private_mcp_name_is_refused(
    installed_route: SimpleNamespace,
) -> None:
    """The name is checked on the raw `.mcp.json`, before the http filter that
    keeps a pure http entry out of the projection."""
    (installed_route.source / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "nmemory": {"command": "/fixture/nmemory"},
                    native.ORCHESTRATOR_MCP_NAME: {
                        "type": "http",
                        "url": "http://127.0.0.1:1/mcp",
                    },
                }
            }
        )
    )

    with pytest.raises(
        native.NativePreparationError, match=native.ORCHESTRATOR_MCP_NAME
    ):
        _orchestrated(installed_route)


def test_an_unbound_run_still_projects_a_plugin_server_of_the_private_name(
    installed_route: SimpleNamespace,
) -> None:
    """The name is private only to an orchestrator turn: an ordinary run has no
    private binding for it to collide with, so it projects as before."""
    (installed_route.source / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "nmemory": {"command": "/fixture/nmemory"},
                    native.ORCHESTRATOR_MCP_NAME: {"command": "/fixture/other"},
                }
            }
        )
    )

    launch = _prepared(installed_route)

    assert launch.mcp_servers[native.ORCHESTRATOR_MCP_NAME]["command"] == (
        "/fixture/other"
    )
    with pytest.raises(
        native.NativePreparationError, match=native.ORCHESTRATOR_MCP_NAME
    ):
        _orchestrated(installed_route)


def test_an_account_that_declares_the_private_mcp_name_is_refused(
    installed_route: SimpleNamespace,
) -> None:
    """Closed and composed collide on one name, and the collision is refused.

    Letting the private definition win would make an account able to decide,
    silently, which of two definitions of `nott_orchestrator` the turn talks to.
    """
    installed_route.config.write_text(
        f'[mcp_servers."{native.ORCHESTRATOR_MCP_NAME}"]\ncommand="/usr/bin/x"\n'
    )

    with pytest.raises(
        native.NativePreparationError, match=native.ORCHESTRATOR_MCP_NAME
    ):
        _orchestrated(installed_route)


def _overrides(argv: tuple[str, ...]) -> dict[str, Any]:
    """Every `-c` startup override of a launch, as the TOML the vendor parses.

    Read from the argv rather than from any mapping of the product: the vendor is
    handed text, and text is what a launch must be measured on.
    """
    return tomllib.loads(
        "\n".join(argv[index + 1] for index, token in enumerate(argv) if token == "-c")
    )


def _launch_mcp(argv: tuple[str, ...]) -> dict[str, Any]:
    """The `[mcp_servers]` table ONE orchestrator launch declares with `-c`."""
    table = _overrides(argv).get("mcp_servers")
    assert isinstance(table, dict), argv
    return table


def test_the_orchestrator_launch_closes_inherited_hooks_before_startup(
    installed_route: SimpleNamespace, tmp_path: Path
) -> None:
    """An orchestrator launch asks for no hook and loads no plugin, and it says
    so where the vendor can still hear it.

    `--dangerously-bypass-hook-trust` is what every other codex launch carries,
    because nSPAWN vets the hook source it installs. This turn vets nothing and
    runs nobody's hooks: its argv drops the flag AND closes the hook and plugin
    configuration, because dropping the flag alone leaves standing every hook
    the account has already trusted, and the thread's `plugins = {}` arrives
    after the process has loaded them.
    """
    binary = str(installed_route.provider.bin.resolve())
    # The positive control: an UNBOUND caged reader on this very route carries
    # the bypass flag and the base's whole argv, byte for byte.
    assert _prepared(installed_route).argv == (
        binary,
        "--dangerously-bypass-hook-trust",
        *CODEX_TAIL,
    )

    launch = _orchestrated(installed_route)

    assert launch.argv[1 : len(STARTUP_ARGUMENTS) + 1] == STARTUP_ARGUMENTS
    assert "--dangerously-bypass-hook-trust" not in launch.argv
    # Before the subcommand, or the vendor reads them as the subcommand's own.
    assert launch.argv[-3:] == CODEX_TAIL[-3:]
    assert launch.argv.index("app-server") == len(launch.argv) - 3
    # And each override is the TOML the vendor parses, not shell-shaped text.
    overrides = _overrides(launch.argv)
    assert overrides["features"] == {
        "hooks": False,
        "plugin_hooks": False,
        "plugins": False,
        "memories": False,
    }
    assert overrides["plugins"] == {}
    # The two merge no-ops are gone: an empty inline table merges nothing (M-a).
    assert "projects" not in overrides
    assert not set(launch.argv) & set(MERGE_NO_OPS)
    # The composed thread states the same closure, from the same definition: the
    # vendor reads configuration at two times and neither one answers for both.
    _, params = _composed(tmp_path)
    for key, value in STARTUP_CLOSURE.items():
        assert params["config"].get(key) == value, key
    assert "projects" not in params["config"]


def test_a_hook_the_account_already_trusts_is_closed_the_same_way(
    installed_route: SimpleNamespace,
) -> None:
    """Trust is the account's, and this launch does not consult it.

    A plugin whose hooks the owner has trusted per version runs WITHOUT the
    bypass flag, so an argv that merely dropped the flag would still fire this
    home's `sessionStart` hook. The closure is unconditional: the launch of an
    account carrying a trusted hook is the launch of one carrying none.
    """
    untrusted = _orchestrated(installed_route).argv
    # The installed plugin, with a hook its manifest points at — codex loads a
    # plugin's hooks only where the manifest says — and that hook trusted for
    # this version in the account's own hook state, which is what the bypass
    # flag stands in for when it is missing.
    manifest = json.dumps(
        {
            "name": "fixture",
            "version": "v1",
            "skills": "./skills/",
            "hooks": "./hooks/hooks.json",
        }
    )
    hooks = json.dumps({"sessionStart": [{"command": "/usr/bin/true"}]})
    for root in (installed_route.skills.parent, installed_route.source):
        (root / ".codex-plugin/plugin.json").write_text(manifest)
        (root / "hooks").mkdir()
        (root / "hooks/hooks.json").write_text(hooks)
    (installed_route.account / "hooks.state").write_text(
        json.dumps({"fixture@local": {"v1": "trusted"}})
    )

    launch = _orchestrated(installed_route)

    assert launch.argv == untrusted
    assert "--dangerously-bypass-hook-trust" not in launch.argv
    assert launch.argv[1 : len(STARTUP_ARGUMENTS) + 1] == STARTUP_ARGUMENTS
    # And nothing of that home reaches the turn by another door either.
    assert launch.plugin_skill_roots == {}
    assert launch.mcp_servers == {}


def test_the_orchestrator_launch_leaves_the_project_layer_no_cwd_to_apply_to(
    installed_route: SimpleNamespace, tmp_path: Path
) -> None:
    """The snapshot's own `.codex/config.toml` is a configuration layer, and the
    launch does not argue with it: it runs where no project layer exists.

    Measured against the vendor (codex 0.159.2, M-a, 2026-10-01): `-c projects={}`
    and `-c mcp_servers={}` are MERGE NO-OPS. An empty inline table merges
    nothing — with both on the argv the snapshot's project layer still loaded,
    an `[mcp_servers.X]` it declared still started host-side, and an
    `[mcp_servers.nott_orchestrator.env]` it declared still merged into the one
    server the turn trusts. An explicit `trust_level = "untrusted"` for the same
    tree, at launch or on the thread, did not stop the merge either.

    What DOES stop it (M-b) is giving the app-server no project to read: its
    process cwd and its thread cwd are a NEUTRAL directory under the run's own
    disposable root — inside no git repository, carrying no `.codex` of its own.
    The snapshot stays what it was: the sandbox's one read root beside the
    runtime, and the recorded `snapshot`. It is simply not the directory the
    vendor loads configuration from — and it is not the `--repository` either,
    which is the session's own editable tree.
    """
    layer = installed_route.snapshot / ".codex"
    layer.mkdir()
    (layer / "config.toml").write_text(
        f'[mcp_servers.layer_probe]\ncommand = "{sys.executable}"\n'
        f'args = ["{installed_route.snapshot / "probe.py"}"]\n'
        f"[mcp_servers.{native.ORCHESTRATOR_MCP_NAME}.env]\n"
        'LD_PRELOAD = "/snapshot/evil.so"\n'
    )

    launch = _orchestrated(installed_route)

    # Neither no-op is spelled anywhere on the argv, by key or by whole token.
    assert not set(launch.argv) & set(MERGE_NO_OPS)
    overrides = _overrides(launch.argv)
    assert "projects" not in overrides
    # The neutral cwd is a leaf of the RUN's own disposable root, and it is one of
    # the leaves the drive creates 0700 before any child.
    neutral = native.orchestrator_neutral_cwd(str(installed_route.disposable))
    assert Path(neutral).parent == installed_route.disposable
    assert Path(neutral).name in native.orchestrator_disposable_leaves()
    # The snapshot is still the read root, and the neutral directory is not one.
    assert os.path.realpath(installed_route.snapshot) in (
        launch.context.read_roots or ()
    )
    assert neutral not in (launch.context.read_roots or ())
    private = _launch_mcp(launch.argv)[native.ORCHESTRATOR_MCP_NAME]
    arguments = private["args"]
    assert arguments[arguments.index("--repository") + 1] == str(
        tmp_path / "repository"
    )
    assert str(installed_route.snapshot) not in arguments
    # The composed thread names the one server this turn has; a layer that cannot
    # be loaded has nothing to merge into it.
    _, params = _composed(tmp_path)
    assert list(params["config"]["mcp_servers"]) == [native.ORCHESTRATOR_MCP_NAME]


@pytest.mark.parametrize(
    ("marker", "in_cwd"),
    [(".git", False), (".git", True), (".codex", True)],
    ids=["git-above", "git-in-cwd", "codex-in-cwd"],
)
def test_an_orchestrator_launch_refuses_a_cwd_that_is_not_neutral(
    installed_route: SimpleNamespace, tmp_path: Path, marker: str, in_cwd: bool
) -> None:
    """Each marker where it actually disqualifies a cwd, by its own name and
    without printing a path.

    A `.git` ANYWHERE on the path is a repository the vendor may treat as a
    project at all, so it is refused in the directory and in every ancestor. A
    `.codex` is refused in the NEUTRAL DIRECTORY ITSELF, which is the only one
    that could be loaded as a project layer for this cwd (the vendor reads a
    project `.codex` between the cwd and the project root, and with no repository
    on the path there is no such root above it). A run root planted under either
    is refused instead of launched.
    """
    planted = tmp_path / "planted"
    route = SimpleNamespace(
        **{**vars(installed_route), "disposable": planted / "runs/a-run/disposable"}
    )
    cwd = Path(native.orchestrator_neutral_cwd(str(route.disposable)))
    ((cwd if in_cwd else planted) / marker).mkdir(parents=True)

    with pytest.raises(native.NativePreparationError, match=marker) as refusal:
        _orchestrated(route)

    message = str(refusal.value)
    assert "neutral cwd" in message, message
    # The refusal names the marker and the field, never the path it found it on.
    assert str(planted) not in message, message

    # The positive control: the same route under a parent carrying neither.
    clean = SimpleNamespace(
        **{
            **vars(installed_route),
            "disposable": tmp_path / "clean/runs/a-run/disposable",
        }
    )
    assert (
        native.orchestrator_neutral_refusal(
            native.orchestrator_neutral_cwd(str(clean.disposable))
        )
        is None
    )
    _orchestrated(clean)


def test_an_orchestrator_launch_accepts_a_codex_above_the_neutral_cwd(
    installed_route: SimpleNamespace, tmp_path: Path
) -> None:
    """A `.codex` in an ANCESTOR of the neutral cwd does not refuse the launch.

    The vendor loads a project `.codex` layer only between the cwd and the project
    root, and the neutral cwd is inside no git repository, so the only `.codex`
    that could apply to it is its own. Refusing every ancestor refused every
    launch on a host whose account home sits above the run root -- `/home/<user>/
    .codex` is where the vendor's own home lives, and the default run root is
    below it. What stands for the layer itself is the `config/read` boundary,
    which refuses any layer of type `project` before a thread may start.
    """
    planted = tmp_path / "planted"
    (planted / ".codex").mkdir(parents=True)
    route = SimpleNamespace(
        **{**vars(installed_route), "disposable": planted / "runs/a-run/disposable"}
    )
    neutral = native.orchestrator_neutral_cwd(str(route.disposable))

    assert native.orchestrator_neutral_refusal(neutral) is None
    _orchestrated(route)
    # The same ancestor still carries no `.git`: that marker's reach is unchanged.
    (planted / ".git").mkdir()
    refusal = native.orchestrator_neutral_refusal(neutral)
    assert refusal is not None and ".git" in refusal, refusal


def test_the_orchestrator_launch_declares_the_private_mcp_and_every_closure(
    installed_route: SimpleNamespace,
) -> None:
    """The private MCP's WHOLE launch is declared at startup, with `-c`.

    The vendor reads configuration at two times and the EARLIER one is the only
    one it can be asked about before a thread exists: `config/read` reports the
    effective `[mcp_servers]` of the process, which is what the launch overrides
    compose (M-e). So the private server is declared there in full — command,
    arguments, environment, enabled — and every server the account declared is
    declared `enabled = false` beside it. A declaration that is not representable
    as the TOML the vendor parses refuses the launch rather than reaching it as
    something else.
    """
    installed_route.config.write_text(
        '[plugins."fixture@local"]\nenabled=true\n'
        '[mcp_servers.helper]\ncommand="/usr/bin/helper"\n'
    )

    launch = _orchestrated(installed_route)

    composed = installed_route.binding.mcp_servers(str(installed_route.disposable))[
        native.ORCHESTRATOR_MCP_NAME
    ]
    declared = _launch_mcp(launch.argv)
    # Every name: the private server, and the one the account's config declared.
    assert set(declared) == {native.ORCHESTRATOR_MCP_NAME, "helper"}
    assert declared["helper"] == {"enabled": False}
    # The private server's whole launch, as the vendor will parse it back.
    assert declared[native.ORCHESTRATOR_MCP_NAME] == {
        "command": composed["command"],
        "args": composed["args"],
        "env": composed["env"],
        "enabled": True,
    }
    # Not only the parsed value: the TEXT, rendered independently of the product.
    expected = _declaration_arguments(
        _declaration(
            {
                "helper": {"enabled": False},
                **installed_route.binding.mcp_servers(str(installed_route.disposable)),
            }
        )
    )
    assert set(expected) <= set(launch.argv), launch.argv
    # Before the subcommand, where the vendor still reads them.
    assert launch.argv.index("app-server") == len(launch.argv) - 3
    for index, token in enumerate(launch.argv):
        if token == "-c":
            assert index < launch.argv.index("app-server")


def test_the_launch_declaration_names_the_bound_interpreter_verbatim(
    installed_route: SimpleNamespace, tmp_path: Path
) -> None:
    """The interpreter is spelled as the binding spelled it, symlink and all.

    A venv's `bin/python` IS a symlink, and `-I` is the whole defence that keeps a
    snapshot's own `nott_harness/orchestrator_mcp.py` from being the private MCP:
    resolving the link would launch the interpreter the link points at, under a
    `sys.prefix` that is not the venv's, so the staged real private MCP would not
    be importable at all. So the composed command is the bound path verbatim.
    """
    venv = tmp_path / "venv/bin"
    venv.mkdir(parents=True)
    real = tmp_path / "venv/bin/python3.14"
    real.write_text("#!/bin/sh\nexit 0\n")
    real.chmod(0o700)
    interpreter = venv / "python"
    interpreter.symlink_to("python3.14")
    assert os.path.realpath(interpreter) != str(interpreter)
    route = SimpleNamespace(
        **{
            **vars(installed_route),
            "binding": _binding(
                tmp_path, installed_route.snapshot, interpreter=str(interpreter)
            ),
        }
    )

    launch = _orchestrated(route)

    declared = _launch_mcp(launch.argv)[native.ORCHESTRATOR_MCP_NAME]
    assert declared["command"] == str(interpreter)
    assert declared["command"] != os.path.realpath(interpreter)
    assert declared["args"][0] == "-I"
    # And the composed thread runs the same spelling.
    assert route.binding.mcp_servers(str(route.disposable))[
        native.ORCHESTRATOR_MCP_NAME
    ]["command"] == str(interpreter)


def test_the_orchestrator_launch_environment_is_explicit_paths_and_disposable_needs(
    installed_route: SimpleNamespace,
) -> None:
    """The whole environment the vendor child is launched with, spelled out.

    A read-only turn whose one tool is a private MCP has no use for the owner's
    home, the host user's name, the session bus, the controller's runtime
    directory or the live store — and the memory that MCP binds takes the birth
    fields of what it captures from whatever started it, so a name forwarded
    here decides whose a proposal looks like.
    """
    disposable = installed_route.disposable
    # The positive control: an UNBOUND caged reader on this very ambient carries
    # all of it, so what follows measures a difference and not a filter nobody set.
    unbound = _prepared(installed_route, ambient=LOUD_AMBIENT).env
    assert {"USER", "LOGNAME", "DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR"} <= set(
        unbound
    )
    assert unbound["HOME"] == "/home/owner"
    assert unbound["NOTT_DATABASE_DIR"] == "/home/owner/.nott/store"

    environment = _orchestrated(installed_route, ambient=LOUD_AMBIENT).env

    assert environment == {
        "PATH": SANDBOX_TOOL_PATH,
        # Disposable, under the RUN's own directory, never under the snapshot.
        "HOME": str(disposable / "vendor-home"),
        "TMPDIR": str(disposable / "vendor-tmp"),
        "XDG_CONFIG_HOME": str(disposable / "xdg-config"),
        "XDG_DATA_HOME": str(disposable / "xdg-data"),
        "XDG_CACHE_HOME": str(disposable / "xdg-cache"),
        "XDG_STATE_HOME": str(disposable / "xdg-state"),
        # Fixed literals, not the host's locale or terminal.
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TERM": "dumb",
        "SHELL": "/bin/sh",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "CODEX_INTERNAL_APP_SERVER_REMOTE_CONTROL_DISABLED": "1",
        # The certificate bundle: the one ambient path a turn that reaches its
        # own model provider needs, and an absolute path like any other.
        "SSL_CERT_FILE": "/etc/ssl/cert.pem",
        # THE ONE ACCOUNT PATH THAT STAYS. The vendor binary reads its own OAuth
        # credential and its provider binding from `CODEX_HOME` and has no other
        # way to be given either; a private home would have to carry a copy of
        # that credential, which an OAuth refresh then rotates away from the
        # account's own. What the home can CONTRIBUTE is closed instead: every
        # server it declares is named closed above, its plugins and skills are
        # out of the read roots, and `features.plugins = false` loads none.
        "CODEX_HOME": str(installed_route.account),
    }
    # Named by the run, so a reaper can still tell whose child this is.
    with_run = _orchestrated(installed_route, ambient=LOUD_AMBIENT, run_id="a-run").env
    assert set(with_run) - set(environment) == {"NOTT_NSPAWN_RUN_ID"}


def test_an_orchestrator_turn_names_its_disposable_root_or_is_refused(
    installed_route: SimpleNamespace,
) -> None:
    with pytest.raises(native.NativePreparationError, match="disposable"):
        _prepared(installed_route, orchestrator=installed_route.binding)
    with pytest.raises(native.NativePreparationError, match="disposable"):
        _prepared(
            installed_route,
            orchestrator=installed_route.binding,
            disposable_root="runs/a-run/disposable",
        )
    # And a root nobody asked for is refused beside a turn that has no binding.
    with pytest.raises(native.NativePreparationError, match="disposable"):
        _prepared(installed_route, disposable_root=str(installed_route.disposable))


def test_an_orchestrator_turn_refuses_a_route_environment_literal(
    installed_route: SimpleNamespace,
) -> None:
    """A route literal is the owner's, so it is refused rather than dropped.

    `NATIVE_ENV_NAMES` admits `DBUS_SESSION_BUS_ADDRESS` and `XDG_RUNTIME_DIR`
    as provider literals, which is exactly what this turn's environment may not
    carry; silently discarding one would leave a declaration that reads as
    honoured.
    """
    from dataclasses import replace as _replace

    loud = _replace(
        installed_route.provider, env={"DBUS_SESSION_BUS_ADDRESS": "unix:path=/x"}
    )

    with pytest.raises(native.NativePreparationError, match="environment literal"):
        _orchestrated(SimpleNamespace(**{**vars(installed_route), "provider": loud}))

    # A certificate bundle is a path, and a path is admitted.
    certified = _replace(
        installed_route.provider, env={"NIX_SSL_CERT_FILE": "/etc/ssl/nix.pem"}
    )
    launch = _orchestrated(
        SimpleNamespace(**{**vars(installed_route), "provider": certified})
    )
    assert launch.env["NIX_SSL_CERT_FILE"] == "/etc/ssl/nix.pem"


# ---------------------------------------------------------------------------
# The admission transaction
# ---------------------------------------------------------------------------


def _row(run_id: str) -> RunRow:
    return RunRow(
        run_id=run_id,
        thread_id=None,
        worktree="/snapshot",
        phase=RunPhase.PENDING,
        sandbox="read-only",
        model="luna",
        owner_instance="i-l7",
        created_at=1,
        updated_at=1,
        backend=Backend.CODEX,
        executed_provider="codex",
        executed_account="one",
    )


def _lifecycle(run_id: str) -> list[dict[str, Any]]:
    return [
        json.loads(row[0])
        for row in _store_rows(
            "SELECT payload FROM run_events WHERE run_id = ? AND kind = 'lifecycle' "
            "ORDER BY seq",
            (run_id,),
        )
    ]


def _bindings(run_id: str) -> list[dict[str, Any]]:
    """Every recorded binding, in order. A run that holds its own scope custody
    records that slice as a `lifecycle` payload too, which is not a binding."""
    return [
        payload["orchestrator_binding"]
        for payload in _lifecycle(run_id)
        if "orchestrator_binding" in payload
    ]


def test_the_binding_is_appended_in_the_admission_transaction(
    registry: Registry, tmp_path: Path
) -> None:
    binding = _binding(tmp_path, Path("/snapshot"))
    payload = binding.payload("nott-nspawn-orchestrator-run-a")

    registry.admit_run(
        _row("run-a"),
        False,
        1,
        100,
        native_context=NativeSessionContext("/account"),
        orchestrator_binding=payload,
    )

    assert _bindings("run-a") == [payload["orchestrator_binding"]]
    assert registry.verify_run_event_chain("run-a") is True
    assert registry.orchestrator_binding("run-a") == payload["orchestrator_binding"]
    # An unbound admission records no lifecycle event at all.
    registry.admit_run(
        _row("run-b"), False, 1, 100, native_context=NativeSessionContext("/account")
    )
    assert _bindings("run-b") == []
    assert registry.orchestrator_binding("run-b") is None


def test_a_binding_that_cannot_be_appended_rolls_the_admission_back(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_: object) -> str:
        raise RuntimeError("the chain could not be extended")

    monkeypatch.setattr(registry_module, "compute_chained_hash", refuse)
    binding = _binding(tmp_path, Path("/snapshot"))

    with pytest.raises(RuntimeError):
        registry.admit_run(
            _row("run-c"),
            False,
            1,
            100,
            native_context=NativeSessionContext("/account"),
            orchestrator_binding=binding.payload("nott-nspawn-orchestrator-run-c"),
        )

    assert registry.get_run("run-c") is None
    assert _store_rows("SELECT count(*) FROM runs")[0][0] == 0
    assert _bindings("run-c") == []


def test_a_refused_admission_leaves_the_first_binding_alone(
    registry: Registry, tmp_path: Path
) -> None:
    binding = _binding(tmp_path, Path("/snapshot"))
    payload = binding.payload("nott-nspawn-orchestrator-run-d")
    registry.admit_run(
        _row("run-d"),
        False,
        1,
        100,
        native_context=NativeSessionContext("/account"),
        orchestrator_binding=payload,
    )

    with pytest.raises(UniqueTaken):
        registry.admit_run(
            _row("run-d"),
            False,
            1,
            100,
            native_context=NativeSessionContext("/account"),
            orchestrator_binding=binding.payload("nott-nspawn-orchestrator-again"),
        )

    assert _bindings("run-d") == [payload["orchestrator_binding"]]


def _recorded(**changes: Any) -> dict[str, Any]:
    """A complete recorded binding, as `payload` writes it."""
    return {
        "session": "s",
        "profile": "p",
        "tool_contract": {"mcp": "m", "project": "p", "tools": ["t"]},
        "repository": "/repository",
        "snapshot": "/snapshot",
        "runtime": "/runtime",
        "database_dir": "/store",
        "interpreter": "/usr/bin/python3",
        **changes,
    }


@pytest.mark.parametrize(
    "bad",
    [
        {},
        {"session": "s", "profile": "p"},
        # The three keys the first record carried, now short of the binding.
        {
            "session": "s",
            "profile": "p",
            "tool_contract": {"mcp": "m", "project": "p", "tools": ["t"]},
        },
        _recorded(extra=1),
        _recorded(session="", tool_contract={}),
        _recorded(tool_contract={"mcp": "m", "project": "p", "tools": []}),
        # A field stored as a registry identifier, past the registry's own 512.
        _recorded(session="a" * 513),
        _recorded(database_dir="relative/store"),
        _recorded(interpreter=""),
        _recorded(runtime=None),
        _recorded(repository="relative/repository"),
        # A record short of either path: neither can be filled in afterwards, so
        # a run carrying one is a run that cannot be resumed at all.
        {key: value for key, value in _recorded().items() if key != "repository"},
        {key: value for key, value in _recorded().items() if key != "snapshot"},
    ],
)
def test_a_malformed_binding_payload_is_refused_before_any_row(
    registry: Registry, bad: dict[str, Any]
) -> None:
    with pytest.raises(RegistryError, match="orchestrator"):
        registry.admit_run(
            _row("run-e"),
            False,
            1,
            100,
            native_context=NativeSessionContext("/account"),
            orchestrator_binding={"orchestrator_binding": bad},
        )

    assert registry.get_run("run-e") is None


# ---------------------------------------------------------------------------
# The idempotency key
# ---------------------------------------------------------------------------


def test_a_request_without_a_binding_keeps_the_base_idempotency_key() -> None:
    assert (
        driver.request_fingerprint(
            "nspawn_run_start", UNBOUND_REQUEST, group=None, strict=False
        )
        == UNBOUND_FINGERPRINT
    )
    # An explicitly absent binding is the same request as no binding at all.
    assert (
        driver.request_fingerprint(
            "nspawn_run_start",
            {**UNBOUND_REQUEST, "orchestrator": None},
            group=None,
            strict=False,
        )
        == UNBOUND_FINGERPRINT
    )


def test_a_bound_request_takes_a_key_of_its_own(tmp_path: Path) -> None:
    bound = {**UNBOUND_REQUEST, "orchestrator": _binding_request(tmp_path)}

    first = driver.request_fingerprint(
        "nspawn_run_start", bound, group=None, strict=False
    )

    assert first != UNBOUND_FINGERPRINT
    # Every coordinate is part of the print: another session is another request,
    # and so is another repository — the tree a proposal of this turn is bound to.
    for field, value in (
        ("session", "conversation-2"),
        ("repository", str(tmp_path / "another-repository")),
    ):
        other = {
            **bound,
            "orchestrator": {**_binding_request(tmp_path), field: value},
        }
        assert (
            driver.request_fingerprint(
                "nspawn_run_start", other, group=None, strict=False
            )
            != first
        ), field


# ---------------------------------------------------------------------------
# Admission through the door
# ---------------------------------------------------------------------------

#: A codex app-server double, faithful to codex 0.159.2 where the integrator
#: measured it. It records every frame and its own process cwd; it parses the `-c`
#: startup overrides off its argv, which is where the private MCP is declared, and
#: answers `config/read {cwd, includeLayers: true}` with `{config, layers,
#: origins}` — the merged `[mcp_servers]` with per-server command/args/env (M-d)
#: and the three layers a neutral cwd leaves (M-b). It answers
#: `mcpServerStatus/list` WITHOUT command, args or env, because the real one does
#: not carry them (M-c). Unfaithful on request: `layer.json` merges into a
#: server's launch the way a configuration layer does, per field, `[env]` name by
#: name; `layers.json` replaces the layer list (a project layer among them);
#: `config_error.json` answers `config/read` with an error; `servers.json`
#: replaces the status listing; `escape.py` is started in its own session at
#: thread start, a descendant no process-group kill of this child can reach.
ORCHESTRATOR_SERVER = r"""
import json, os, subprocess, sys, time, tomllib
from pathlib import Path
record = Path(os.environ['ORCHESTRATOR_RECORD'])
(record / 'cwd.txt').write_text(os.getcwd())
# The launch's own `-c` overrides: where the private MCP is declared, and what
# `config/read` reports back before any thread exists.
overrides = [sys.argv[i + 1] for i, a in enumerate(sys.argv) if a == '-c']
launched = tomllib.loads('\n'.join(overrides)).get('mcp_servers', {})
composed = {}
def send(value):
    print(json.dumps(value), flush=True)
def merged(servers):
    layer = {}
    if (record / 'layer.json').exists():
        layer = json.loads((record / 'layer.json').read_text())
    out = {}
    for name in list(servers) + [n for n in layer if n not in servers]:
        s = dict(servers.get(name, {}))
        for key, value in layer.get(name, {}).items():
            s[key] = {**s.get(key, {}), **value} if key == 'env' else value
        out[name] = s
    return out
def effective():
    if (record / 'servers.json').exists():
        listed = json.loads((record / 'servers.json').read_text())
    else:
        listed = [{'name': name,
                   'tools': {tool: {} for tool in (server.get('enabled_tools') or [])},
                   'resources': [], 'resourceTemplates': []}
                  for name, server in composed.items()
                  if server.get('enabled', True)]
    # No command, args or env: the real entry carries none of them (M-c).
    return [{'runtimeStatus': 'connected', 'pluginId': None, 'httpOrigin': None,
             'serverInfo': {'name': entry['name'], 'version': '0'},
             'serverCapabilities': {'tools': {}}, 'toolsError': None,
             'authStatus': None, **entry} for entry in listed]
for line in sys.stdin:
    with (record / 'frames.ndjson').open('a') as log:
        log.write(line)
    frame = json.loads(line)
    method, params = frame.get('method'), frame.get('params') or {}
    if method == 'initialize':
        send({'id': frame['id'], 'result': {'userAgent': 'orchestrator-fixture'}})
    elif method == 'config/read':
        if (record / 'config_error.json').exists():
            send({'id': frame['id'], 'error': json.loads(
                (record / 'config_error.json').read_text())})
            continue
        if (record / 'layers.json').exists():
            layers = json.loads((record / 'layers.json').read_text())
        else:
            layers = [{'name': {'type': t}, 'disabledReason': None, 'config': {},
                       'version': 1} for t in ('sessionFlags', 'user', 'system')]
        send({'id': frame['id'], 'result': {'config': {'mcp_servers': merged(launched)},
              'layers': layers, 'origins': {}}})
    elif method == 'thread/start':
        composed = params['config']['mcp_servers']
        if (record / 'escape.py').exists():
            subprocess.Popen([sys.executable, str(record / 'escape.py'), str(record)],
                             start_new_session=True)
            while not (record / 'escaped.pid').exists():
                time.sleep(0.01)
        send({'id': frame['id'], 'result': {'thread': {'id': 'orchestrator-thread'},
              'model': params['model'],
              'sandbox': {'type': 'readOnly'},
              'activePermissionProfile': {'id': params['permissions'],
                                          'extends': None}}})
    elif method == 'mcpServerStatus/list':
        send({'id': frame['id'],
              'result': {'data': effective(), 'nextCursor': None}})
    elif method == 'turn/start':
        send({'id': frame['id'], 'result': {'turn': {'id': 'orchestrator-turn'}}})
        send({'method': 'item/completed', 'params': {
              'threadId': 'orchestrator-thread', 'turnId': 'orchestrator-turn',
              'item': {'id': 'answer', 'type': 'agentMessage',
                       'phase': 'final_answer', 'text': 'DONE'}}})
        send({'method': 'turn/completed', 'params': {
              'threadId': 'orchestrator-thread',
              'turn': {'id': 'orchestrator-turn', 'status': 'completed',
                       'error': None}}})
"""

#: One MCP child of the vendor, in its own session: measured against codex
#: 0.157.1 (2026-10-01), an app-server's MCP children run in their own process
#: group and session, so a process-group kill of the app-server leaves them
#: alive and they outlive the turn. Only the run's cgroup custody holds them.
#: Its pid receipt lands whole by rename: the vendor answers as soon as the
#: receipt exists, so one written in place can be read empty.
ESCAPED_DESCENDANT = """
import os, sys, time
from pathlib import Path
receipt = Path(sys.argv[1], f'escaped-{os.getpid()}.tmp')
receipt.write_text(str(os.getpid()))
os.replace(receipt, Path(sys.argv[1], 'escaped.pid'))
time.sleep(120)
"""


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _custody(root: Path, record: Path) -> Any:
    """The scope custody of a run, standing in for systemd alone.

    The workflow's own sandbox has no user manager (`/run/user` is not there), so
    `AggregateScope` cannot be constructed here: every case that needs the REAL
    slice carries `needs("systemd_user", "cgroup")` and SKIPS on this host, and a
    skip is never a pass. What this class keeps is the supervisor's own custody
    path, unmocked — `_open_writer` recording the slice before any child,
    `scope.command` wrapping every launch into it, `_close_writer` and
    `_drain_writer` refusing one that cannot be seen empty. What it stands in for
    is the kernel: `drain` reports the slice still populated while a descendant
    the turn left behind is alive, as `cgroup.events` does.
    """

    class Slice:
        opened: ClassVar[list[Any]] = []
        units: ClassVar[dict[str, Any]] = {}

        def __init__(self, *, memory_bytes: int = 0, tasks: int = 0) -> None:
            self.memory_bytes, self.tasks = memory_bytes, tasks
            self.unit = f"nottcontroller{uuid.uuid4().hex}.slice"
            self.path = root / self.unit
            self.path.mkdir(parents=True)
            self.commands: list[list[str]] = []
            self.on_record: list[list[str]] = []
            self.drains = 0
            self.closed = False
            Slice.opened.append(self)
            Slice.units[self.unit] = self

        @classmethod
        def recorded(cls, unit: str) -> Any:
            """The slice a run recorded, for `drain` and `close` only."""
            return cls.units[unit]

        def verify(self) -> None:
            if self.closed:
                raise ControllerRefusal("aggregate scope is closed or unbound")

        def command(self, argv: list[str]) -> list[str]:
            self.verify()
            self.commands.append(list(argv))
            self.on_record.append(_recorded_units())
            # Manager access belongs to the launcher, never its target.
            return ["/usr/bin/env", "-u", "XDG_RUNTIME_DIR", "--", *argv]

        def environment(self, target: dict[str, str] | None) -> dict[str, str]:
            return {**(target or {}), "XDG_RUNTIME_DIR": str(root)}

        def drain(self) -> None:
            self.drains += 1
            escaped = record / "escaped.pid"
            if escaped.exists() and _alive(int(escaped.read_text())):
                raise ControllerRefusal("aggregate descendants survived cleanup")

        def close(self) -> None:
            if self.closed:
                return
            self.drain()
            shutil.rmtree(self.path, ignore_errors=True)
            self.closed = True

    Slice.opened = []
    Slice.units = {}
    return Slice


def _recorded_units() -> list[str]:
    """Every writer scope on record, across runs, as the store holds it now."""
    return [
        json.loads(row[0])["writer_scope"]["unit"]
        for row in _store_rows(
            "SELECT payload FROM run_events WHERE kind = 'lifecycle' ORDER BY seq"
        )
        if "writer_scope" in json.loads(row[0])
    ]


@pytest.fixture
def orchestrator_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, roster_home: Path
) -> Iterator[SimpleNamespace]:
    """A codex route whose vendor is `ORCHESTRATOR_SERVER`, a clean snapshot to
    read, a trusted runtime beside it, and the run's scope custody.

    The vendor reports the effective MCP list of the thread it was configured
    with, so the composition is measured against what a faithful vendor says it
    started; `record/servers.json` and `record/layer.json` make it unfaithful in
    the two ways the project configuration layer was observed to be.
    """
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    record = tmp_path / "record"
    record.mkdir()
    script = tmp_path / "orchestrator_server.py"
    script.write_text(ORCHESTRATOR_SERVER)
    custody = _custody(tmp_path / "slices", record)
    monkeypatch.setattr(supervisor, "AggregateScope", custody)
    provider = Provider(
        name="fixture", kind="codex", model="fixture", models=["fixture"]
    )
    picked = picker.PickerResult(None, provider, "fixture", "fixture", None, None)
    monkeypatch.setattr(supervisor.picker, "pick_execution", lambda *a, **k: picked)
    monkeypatch.setattr(Roster, "refuse_unservable_spawn", lambda *a: None)
    prepared: list[dict[str, Any]] = []
    launches: list[native.NativeLaunch] = []

    def prepare(*args: object, **kwargs: Any) -> native.NativeLaunch:
        prepared.append(kwargs)
        # The account's own inherited declaration, carried closed: the driven
        # thread must still name it, or the vendor merges its live definition
        # back in.
        inherited: dict[str, dict[str, Any]] = {"nmemory": {"enabled": False}}
        overrides: tuple[str, ...] = ()
        if (bound := kwargs.get("orchestrator")) is not None:
            # The startup overrides a launch carries, rendered by this file and
            # parsed back by the double: the private MCP is declared at launch, so
            # the vendor's effective view is the one this turn composed.
            overrides = (
                *STARTUP_ARGUMENTS,
                *_declaration_arguments(
                    _declaration(
                        {**inherited, **bound.mcp_servers(kwargs["disposable_root"])}
                    )
                ),
            )
        launch = native.NativeLaunch(
            (
                sys.executable,
                "-u",
                str(script),
                *overrides,
                "app-server",
                "--listen",
                "stdio://",
            ),
            {"PATH": "/usr/bin:/bin", "ORCHESTRATOR_RECORD": str(record)},
            SecretNeedles.none(),
            NativeSessionContext(
                str(tmp_path),
                read_roots=tuple(
                    sorted((os.path.realpath(snapshot), os.path.realpath(runtime)))
                ),
            ),
            NativeAccount(tmp_path),
            mcp_servers=inherited,
        )
        launches.append(launch)
        return launch

    monkeypatch.setattr(native, "prepare_native", prepare)
    yield SimpleNamespace(
        snapshot=snapshot,
        runtime=runtime,
        record=record,
        prepared=prepared,
        launches=launches,
        binding=_binding_request(tmp_path),
        custody=custody,
    )
    escaped = record / "escaped.pid"
    if escaped.exists() and _alive(pid := int(escaped.read_text())):
        os.kill(pid, 9)


def _dispatch(route: SimpleNamespace, **changes: Any) -> dict[str, Any]:
    return {
        "prompt": "orchestrate",
        "cwd": str(route.snapshot),
        "class": DECLARED_CLASS,
        "read_only": True,
        "orchestrator": route.binding,
        **changes,
    }


def test_the_orchestrator_turn_is_admitted_with_its_binding_on_record(
    registry: Registry,
    tmp_path: Path,
    orchestrator_route: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        Supervisor, "_drive_native_run", lambda self, *args: started.append(args)
    )
    sv = _sv(registry, tmp_path)

    accepted = _payload(
        _call(
            McpServer(supervisor=sv), "nspawn_run_start", _dispatch(orchestrator_route)
        )
    )

    run_id = accepted["run_id"]
    row = registry.get_run(run_id)
    assert row is not None
    assert (row.sandbox, row.backend) == ("read-only", Backend.CODEX)
    (recorded,) = _bindings(run_id)
    assert recorded["session"] == "conversation-1"
    # The profile the binding records is the run's own, never a fresh id.
    assert recorded["profile"] == f"nott-nspawn-orchestrator-{run_id}"
    assert recorded["tool_contract"]["project"] == "nott"
    # The launch is prepared with the binding and no read path beside it.
    assert [call.get("read_paths") for call in orchestrator_route.prepared] == [()]
    assert [call["orchestrator"].session for call in orchestrator_route.prepared] == [
        "conversation-1"
    ]
    # The turn's disposable needs are named under the RUN's own directory.
    assert [call["disposable_root"] for call in orchestrator_route.prepared] == [
        str(tmp_path / "runs" / run_id / "disposable")
    ]
    assert len(started) == 1


def test_a_bound_admission_records_the_real_repository_beside_its_snapshot(
    registry: Registry,
    tmp_path: Path,
    orchestrator_route: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole path, end to end: A is bound, S is read, and the record says so.

    The run's cwd IS the snapshot, so a repository taken from the cwd would be
    recorded as the binding and handed to the private MCP as `--repository`, and
    every proposal of the conversation would name a directory the run deletes.
    """
    monkeypatch.setattr(Supervisor, "_drive_native_run", lambda self, *args: None)
    server = McpServer(supervisor=_sv(registry, tmp_path))

    accepted = _payload(
        _call(server, "nspawn_run_start", _dispatch(orchestrator_route))
    )

    (recorded,) = _bindings(accepted["run_id"])
    assert recorded["repository"] == str(tmp_path / "repository")
    assert recorded["snapshot"] == os.path.realpath(orchestrator_route.snapshot)
    assert recorded["repository"] != recorded["snapshot"]
    # The binding the launch was prepared with says the same thing, on the argv
    # the private MCP is started with and in the roots the sandbox grants.
    (bound,) = [call["orchestrator"] for call in orchestrator_route.prepared]
    arguments = bound.mcp_servers(str(tmp_path / "disposable"))[
        native.ORCHESTRATOR_MCP_NAME
    ]["args"]
    assert arguments[arguments.index("--repository") + 1] == str(
        tmp_path / "repository"
    )
    assert bound.read_roots == tuple(
        sorted(
            (
                os.path.realpath(orchestrator_route.snapshot),
                str(tmp_path / "runtime"),
            )
        )
    )
    assert str(tmp_path / "repository") not in bound.read_roots


@pytest.mark.parametrize(
    "case",
    ["missing", "empty", "relative", "non-canonical", "over-long", "wrong-type"],
)
def test_the_door_refuses_a_binding_whose_repository_is_not_a_real_root(
    registry: Registry,
    tmp_path: Path,
    orchestrator_route: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    """Refused BY NAME, as invalid params, before any row and before any launch.

    And never filled in: a repository the caller did not state is a coordinate
    the caller believes it set, and the cwd standing in for it is exactly the
    silence this unit closes.
    """
    monkeypatch.setattr(child, "spawn_child", lambda *a, **k: pytest.fail("spawned"))
    monkeypatch.setattr(
        Supervisor, "_drive_native_run", lambda *a: pytest.fail("driven")
    )
    binding: dict[str, Any] = {**orchestrator_route.binding}
    if case == "missing":
        del binding["repository"]
    else:
        binding["repository"] = {
            "empty": "",
            "relative": "repository",
            "non-canonical": str(tmp_path / "repository" / ".." / "repository"),
            "over-long": "/" + "r" * 512,
            "wrong-type": 7,
        }[case]
    server = McpServer(supervisor=_sv(registry, tmp_path))

    answer = _call(
        server,
        "nspawn_run_start",
        _dispatch(orchestrator_route, orchestrator=binding),
    )

    assert answer.get("error", {}).get("code") == INVALID_PARAMS, answer
    assert "repository" in answer["error"]["message"], answer
    assert _store_rows("SELECT count(*) FROM runs")[0][0] == 0
    assert _store_rows("SELECT count(*) FROM run_events")[0][0] == 0
    assert sorted((tmp_path / "runs" / "drivers").glob("*.ack")) == []


@pytest.mark.parametrize(
    "case",
    ["native_write", "writable", "read_paths", "claude_route"],
)
def test_an_orchestrator_binding_outside_a_codex_read_only_turn_is_refused(
    registry: Registry,
    tmp_path: Path,
    orchestrator_route: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    arguments = _dispatch(orchestrator_route)
    if case == "native_write":
        arguments["native_write"] = True
    elif case == "writable":
        arguments["read_only"] = False
    elif case == "read_paths":
        arguments["read_paths"] = [str(tmp_path / "runtime")]
    elif case == "claude_route":
        # AGY is refused for every dispatch by its own launch wall, so the
        # non-codex route this guard can be measured on is the Claude one.
        other = Provider(
            name="fixture", kind="claude", model="fixture", models=["fixture"]
        )
        routed = picker.PickerResult(None, other, "fixture", "fixture", None, None)
        monkeypatch.setattr(supervisor.picker, "pick_execution", lambda *a, **k: routed)
    monkeypatch.setattr(child, "spawn_child", lambda *a, **k: pytest.fail("spawned"))

    answer = _call(
        McpServer(supervisor=_sv(registry, tmp_path)), "nspawn_run_start", arguments
    )

    # A refusal that NAMES the binding, whether the door refuses the argument
    # (-32602) or the supervisor refuses the dispatch it would become.
    assert "result" not in answer, answer
    assert "orchestrator" in answer["error"]["message"], answer
    assert _store_rows("SELECT count(*) FROM runs")[0][0] == 0
    assert _store_rows("SELECT count(*) FROM run_events")[0][0] == 0


def test_an_unbound_origin_cannot_be_resumed_with_a_binding(
    registry: Registry,
    tmp_path: Path,
    orchestrator_route: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Supervisor, "_drive_native_run", lambda self, *args: None)
    sv = _sv(registry, tmp_path)
    server = McpServer(supervisor=sv)
    unbound = _dispatch(orchestrator_route)
    del unbound["orchestrator"]
    first = _payload(_call(server, "nspawn_run_start", unbound))["run_id"]
    # An unbound dispatch is prepared with NO disposable root: that root belongs
    # to an orchestrator turn, and `prepare_native` refuses one beside a turn
    # that has no binding (test_an_orchestrator_turn_names_its_disposable_root).
    assert [call["disposable_root"] for call in orchestrator_route.prepared] == [None]
    row = registry.get_run(first)
    assert row is not None
    from dataclasses import replace

    registry.update_run(
        replace(row, thread_id="origin-thread", phase=RunPhase.COMPLETED)
    )

    answer = _call(
        server,
        "nspawn_run_start",
        _dispatch(orchestrator_route, session_id="origin-thread"),
    )

    assert answer.get("error", {}).get("data", {}).get("type") == "InvalidRunRequest", (
        answer
    )
    assert "unbound" in answer["error"]["message"], answer
    assert _store_rows("SELECT count(*) FROM runs")[0][0] == 1


def test_the_driven_orchestrator_turn_starts_in_the_composed_cage(
    registry: Registry,
    tmp_path: Path,
    orchestrator_route: SimpleNamespace,
) -> None:
    sv = _sv(registry, tmp_path)
    try:
        accepted = _payload(
            _call(
                McpServer(supervisor=sv),
                "nspawn_run_start",
                _dispatch(orchestrator_route),
            )
        )
        run_id = accepted["run_id"]
        row = _terminal(registry, run_id, 20)
    finally:
        sv.shutdown()
    assert row.phase is RunPhase.COMPLETED, row

    frames = [
        json.loads(line)
        for line in (orchestrator_route.record / "frames.ndjson")
        .read_text()
        .splitlines()
    ]
    (thread,) = [f["params"] for f in frames if f.get("method") == "thread/start"]
    assert thread["approvalPolicy"] == "never"
    assert "sandbox" not in thread
    # THE NEUTRAL CWD. The app-server's own process cwd and the thread's cwd are
    # one leaf of the run's disposable root, not the snapshot: an empty `projects`
    # or `mcp_servers` override merges nothing (M-a), and what does close the
    # project layer is having no project to load (M-b).
    neutral = str(tmp_path / "runs" / run_id / "disposable" / "neutral")
    assert (orchestrator_route.record / "cwd.txt").read_text() == neutral
    assert thread["cwd"] == neutral
    assert thread["cwd"] != str(orchestrator_route.snapshot)
    assert native.orchestrator_neutral_refusal(neutral) is None
    # And the REAL binding is untouched by it: the snapshot is still the sandbox's
    # one read root beside the runtime, and the turn's own cwd is not.
    assert (
        os.path.realpath(orchestrator_route.snapshot)
        in thread["config"]["permissions"][thread["permissions"]]["filesystem"]
    )
    (turn,) = [f["params"] for f in frames if f.get("method") == "turn/start"]
    assert turn["cwd"] == neutral
    assert thread["permissions"] == f"nott-nspawn-orchestrator-{run_id}"
    profile = thread["config"]["permissions"][thread["permissions"]]
    assert profile == {
        "filesystem": {
            ":root": "deny",
            ":minimal": "read",
            os.path.realpath(orchestrator_route.runtime): "read",
            os.path.realpath(orchestrator_route.snapshot): "read",
        },
        "network": {"enabled": False},
    }
    servers = thread["config"]["mcp_servers"]
    assert set(servers) == {native.ORCHESTRATOR_MCP_NAME, "nmemory"}
    assert servers["nmemory"] == {"enabled": False}
    server = servers[native.ORCHESTRATOR_MCP_NAME]
    assert server["enabled_tools"] == list(CONTRACT_TOOLS)
    assert server["args"][: server["args"].index("-m")] == ["-I"]
    assert "--session" in server["args"] and "conversation-1" in server["args"]
    assert "--project" in server["args"] and "nott" in server["args"]
    # The composed launch of the private MCP: explicit paths, the literals that
    # close what codex forwards of its own accord, and nothing else.
    assert set(server["env"]) == {
        "PATH",
        "HOME",
        "TMPDIR",
        "XDG_CACHE_HOME",
        "LANG",
        "LC_ALL",
        "SHELL",
    }
    assert stat.S_IMODE(Path(server["env"]["HOME"]).lstat().st_mode) == 0o700
    # Every disposable leaf an orchestrator environment names exists, 0700, under
    # the RUN's own directory: the vendor child's state tree as well as the MCP's,
    # because admission composed an environment that names both.
    root = tmp_path / "runs" / run_id / "disposable"
    for leaf in native.orchestrator_disposable_leaves():
        assert stat.S_IMODE((root / leaf).lstat().st_mode) == 0o700, leaf
    arguments = server["args"]
    assert arguments[arguments.index("--repository") + 1] == str(
        tmp_path / "repository"
    )
    assert str(orchestrator_route.snapshot) not in arguments
    assert neutral not in arguments
    assert thread["config"]["features.browser_use"] is False
    assert thread["config"]["features.code_mode"] is False
    # THE BOUNDARY, in order: `config/read` on the SAME neutral cwd, answered
    # BEFORE any thread/start, and only then the thread. The status list that
    # follows carries no command, args or env at all (M-c), which is exactly why
    # it cannot be the boundary — and the clean turn passes anyway.
    methods = [frame.get("method") for frame in frames if frame.get("method")]
    assert methods.index("config/read") < methods.index("thread/start")
    (read,) = [f["params"] for f in frames if f.get("method") == "config/read"]
    assert read == {"cwd": neutral, "includeLayers": True}
    assert methods.index("mcpServerStatus/list") > methods.index("thread/start")
    # The composed launch IS what the vendor's effective configuration states, so
    # the boundary holds and the turn starts: one turn, and one only.
    assert sum(1 for frame in frames if frame.get("method") == "turn/start") == 1
    # And the effective configuration never reached the recorded transcript: the
    # merged `[mcp_servers]` carries every env value of the private MCP, so the
    # answer is read privately and persisted as nothing at all.
    bodies = b"".join(
        path.read_bytes()
        for path in (tmp_path / "runs" / run_id).rglob("*")
        if path.is_file()
    )
    assert b"origins" not in bodies
    assert b"XDG_CACHE_HOME" not in bodies


#: The whole private MCP, as a live catalog that holds the contract.
_WHOLE = _entry(native.ORCHESTRATOR_MCP_NAME, CONTRACT_TOOLS)


@pytest.mark.parametrize(
    "catalog",
    [
        # A server the binding never named at all.
        [_WHOLE, _entry("nsomething")],
        # The name it DID name closed, come up with a tool of its own: a closed
        # server is listed without one, and this one started.
        [_WHOLE, _entry("nmemory", ("nmemory_capture",))],
        # The private MCP short of the contract its own admission recorded.
        [_entry(native.ORCHESTRATOR_MCP_NAME, CONTRACT_TOOLS[:2])],
        # And the private MCP not there at all.
        [_entry("nmemory")],
    ],
    ids=["unnamed", "closed-but-live", "short-contract", "absent"],
)
def test_a_driven_turn_whose_catalog_breaks_its_contract_fails(
    registry: Registry,
    tmp_path: Path,
    orchestrator_route: SimpleNamespace,
    catalog: list[dict[str, Any]],
) -> None:
    """The contract is checked against the LIVE catalog, not only composed into
    the request, and a name composed CLOSED is no licence to be running."""
    (orchestrator_route.record / "servers.json").write_text(json.dumps(catalog))
    sv = _sv(registry, tmp_path)
    try:
        accepted = _payload(
            _call(
                McpServer(supervisor=sv),
                "nspawn_run_start",
                _dispatch(orchestrator_route),
            )
        )
        row = _terminal(registry, accepted["run_id"], 20)
    finally:
        sv.shutdown()

    assert row.phase is RunPhase.FAILED, row
    # The recorded reason is the class, never the vendor's own bytes; the turn
    # died on the protocol boundary rather than answering.
    assert (row.blocked_reason or "").startswith("AppServerError"), row


# ---------------------------------------------------------------------------
# The effective MCP list of the driven turn
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "layer",
    [
        {"env": INJECTED},
        {"env": {"LD_PRELOAD": "/snapshot/evil.so"}},
        {"command": "/usr/bin/python3"},
        {"args": ["-c", "print()"]},
    ],
    ids=["env-injected", "preload", "command", "args"],
)
def test_a_driven_turn_whose_effective_launch_differs_never_starts_a_thread(
    registry: Registry,
    tmp_path: Path,
    orchestrator_route: SimpleNamespace,
    layer: dict[str, Any],
) -> None:
    """A configuration layer's injection, as the vendor's own effective view
    reports it, and BEFORE the thread that would start the server.

    Each case is a server named `nott_orchestrator` whose command, arguments or
    environment the composition never named — which is exactly what a merged
    `[mcp_servers.nott_orchestrator]` produces, project layer or account layer
    (M-e). The turn is refused on `config/read`, so no thread starts, no MCP child
    is launched at all, and no `turn/start` follows.
    """
    (orchestrator_route.record / "layer.json").write_text(
        json.dumps({native.ORCHESTRATOR_MCP_NAME: layer})
    )
    sv = _sv(registry, tmp_path)
    try:
        accepted = _payload(
            _call(
                McpServer(supervisor=sv),
                "nspawn_run_start",
                _dispatch(orchestrator_route),
            )
        )
        row = _terminal(registry, accepted["run_id"], 20)
    finally:
        sv.shutdown()

    assert row.phase is RunPhase.FAILED, row
    assert (row.blocked_reason or "").startswith("AppServerError"), row
    frames = [
        json.loads(line)
        for line in (orchestrator_route.record / "frames.ndjson")
        .read_text()
        .splitlines()
    ]
    # The effective configuration was read, and the turn died on it: before the
    # thread, so before the vendor started one MCP child of this composition.
    assert any(f.get("method") == "config/read" for f in frames), frames
    assert [f for f in frames if f.get("method") == "thread/start"] == []
    assert [f for f in frames if f.get("method") == "turn/start"] == []
    # And the reason names the field, never an effective value of it.
    reason = row.blocked_reason or ""
    for value in (*INJECTED.values(), "/snapshot/evil.so"):
        assert value not in reason, reason


@pytest.mark.parametrize(
    "case",
    ["project-layer", "unreadable", "second-server"],
    ids=["project-layer", "unreadable", "second-server"],
)
def test_a_driven_turn_refuses_an_effective_configuration_it_cannot_vouch_for(
    registry: Registry,
    tmp_path: Path,
    orchestrator_route: SimpleNamespace,
    case: str,
) -> None:
    """FAIL CLOSED, before any thread: a project layer among the layers, a
    `config/read` that answers an error at all, and a server the launch never
    declared coming back enabled. None of the three is a pass, and none of them
    reaches a thread the vendor would start an MCP child for."""
    record = orchestrator_route.record
    if case == "project-layer":
        record.joinpath("layers.json").write_text(
            json.dumps(
                [
                    {"name": {"type": "sessionFlags"}, "disabledReason": None},
                    {"name": {"type": "project"}, "disabledReason": None},
                    {"name": {"type": "user"}, "disabledReason": None},
                ]
            )
        )
    elif case == "unreadable":
        record.joinpath("config_error.json").write_text(
            json.dumps({"code": -32603, "message": "no"})
        )
    else:
        record.joinpath("layer.json").write_text(
            json.dumps({"nelsewhere": {"command": "/usr/bin/other"}})
        )
    sv = _sv(registry, tmp_path)
    try:
        accepted = _payload(
            _call(
                McpServer(supervisor=sv),
                "nspawn_run_start",
                _dispatch(orchestrator_route),
            )
        )
        row = _terminal(registry, accepted["run_id"], 20)
    finally:
        sv.shutdown()

    assert row.phase is RunPhase.FAILED, row
    assert (row.blocked_reason or "").startswith("AppServerError"), row
    frames = [
        json.loads(line) for line in (record / "frames.ndjson").read_text().splitlines()
    ]
    assert any(f.get("method") == "config/read" for f in frames), frames
    assert [f for f in frames if f.get("method") == "thread/start"] == []


# ---------------------------------------------------------------------------
# Custody: the app-server and every descendant of it
# ---------------------------------------------------------------------------


def test_the_orchestrator_app_server_runs_under_the_runs_scope_custody(
    registry: Registry,
    tmp_path: Path,
    orchestrator_route: SimpleNamespace,
) -> None:
    """One slice, on record before the child it holds.

    The vendor's MCP children run in their own process group and session
    (measured against codex 0.157.1, 2026-10-01), so a process-group kill of the
    app-server leaves them alive. The custody that reaches them is the one a
    native writer already runs under: the run's own systemd scope, recorded
    before anything is launched into it, emptied before any terminal write.
    """
    sv = _sv(registry, tmp_path)
    server = McpServer(supervisor=sv)
    try:
        accepted = _payload(
            _call(server, "nspawn_run_start", _dispatch(orchestrator_route))
        )
        run_id = accepted["run_id"]
        row = _terminal(registry, run_id, 20)
        assert row.phase is RunPhase.COMPLETED, row
        assert orchestrator_route.custody.opened != [], "the turn held no custody"
        (held,) = orchestrator_route.custody.opened
        # The app-server itself, launched into the slice through its own command.
        (launch,) = orchestrator_route.launches
        assert held.commands == [list(launch.argv)]
        # And the slice was on record before that launch, not after it.
        assert held.on_record == [[held.unit]]
        scopes = [p["writer_scope"] for p in _lifecycle(run_id) if "writer_scope" in p]
        assert scopes == [
            {"unit": held.unit, "cgroup": str(tmp_path / "slices" / held.unit)}
        ]
        assert registry.writer_scope(run_id) == (held.unit, scopes[0]["cgroup"])
        # The turn's own close empties the slice; the binding is still on record.
        assert (held.drains, held.closed) == (1, True)
        assert [binding["session"] for binding in _bindings(run_id)] == [
            "conversation-1"
        ]

        closed = _payload(_call(server, "nspawn_run_close", {"run_id": run_id}))

        assert (closed["phase"], closed["claim_released"]) == ("closed", True)
    finally:
        sv.shutdown()


def test_a_descendant_that_outlives_the_orchestrator_turn_refuses_its_close(
    registry: Registry,
    tmp_path: Path,
    orchestrator_route: SimpleNamespace,
) -> None:
    """A survivor is a named failure, never a closed run.

    The vendor leaves one child in its own session, as its MCP children are. The
    turn's own close cannot see the slice empty, so the run lands FAILED rather
    than completed, and every close of it is refused by name while that child
    lives. Once it is gone the same close lands.
    """
    (orchestrator_route.record / "escape.py").write_text(ESCAPED_DESCENDANT)
    sv = _sv(registry, tmp_path)
    server = McpServer(supervisor=sv)
    try:
        accepted = _payload(
            _call(server, "nspawn_run_start", _dispatch(orchestrator_route))
        )
        run_id = accepted["run_id"]
        row = _terminal(registry, run_id, 20)
        escaped = int((orchestrator_route.record / "escaped.pid").read_text())
        assert _alive(escaped)
        assert row.phase is RunPhase.FAILED, row

        refused = _call(server, "nspawn_run_close", {"run_id": run_id})

        assert "result" not in refused, refused
        assert "not observed empty" in refused["error"]["message"], refused
        current = registry.get_run(run_id)
        assert current is not None and current.phase is not RunPhase.CLOSED

        os.kill(escaped, 9)
        _wait_for(lambda: not _alive(escaped), 5, "the escaped descendant to die")

        closed = _payload(_call(server, "nspawn_run_close", {"run_id": run_id}))

        assert closed["phase"] == "closed", closed
        (held,) = orchestrator_route.custody.opened
        assert held.closed is True
    finally:
        sv.shutdown()


# ---------------------------------------------------------------------------
# Resume: the identical recorded binding, or nothing
# ---------------------------------------------------------------------------


def _resumable(
    registry: Registry, server: McpServer, route: SimpleNamespace
) -> tuple[str, dict[str, Any]]:
    """One admitted orchestrator run, its thread on record and its phase
    terminal: the origin a resume names."""
    run_id = _payload(_call(server, "nspawn_run_start", _dispatch(route)))["run_id"]
    row = registry.get_run(run_id)
    assert row is not None
    registry.update_run(
        replace(row, thread_id="origin-thread", phase=RunPhase.COMPLETED)
    )
    (recorded,) = _bindings(run_id)
    return run_id, recorded


@pytest.fixture
def resumable(
    registry: Registry,
    tmp_path: Path,
    orchestrator_route: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[SimpleNamespace]:
    """The origin run, admitted through the door, with no turn driven for it."""
    monkeypatch.setattr(Supervisor, "_drive_native_run", lambda self, *args: None)
    sv = _sv(registry, tmp_path)
    server = McpServer(supervisor=sv)
    run_id, recorded = _resumable(registry, server, orchestrator_route)
    yield SimpleNamespace(server=server, run_id=run_id, recorded=recorded)
    sv.shutdown()


def test_a_bound_origin_cannot_be_resumed_without_its_binding(
    registry: Registry,
    orchestrator_route: SimpleNamespace,
    resumable: SimpleNamespace,
) -> None:
    """A resume that omits the binding is not an unbound turn, it is THIS turn
    continued out of its cage: the same conversation, with no private MCP bound
    to it, no store named and the account's own catalog back."""
    unbound = _dispatch(orchestrator_route, session_id="origin-thread")
    del unbound["orchestrator"]

    answer = _call(resumable.server, "nspawn_run_start", unbound)

    assert "result" not in answer, answer
    assert "orchestrator" in answer["error"]["message"], answer
    assert _store_rows("SELECT count(*) FROM runs")[0][0] == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("session", "conversation-2"),
        ("project", "another-project"),
        ("repository", "/elsewhere/repository"),
        ("runtime", "/elsewhere/runtime"),
        ("database_dir", "/elsewhere/store"),
        ("interpreter", "/usr/bin/python3"),
    ],
)
def test_a_bound_origin_refuses_a_resume_that_changes_one_coordinate(
    registry: Registry,
    orchestrator_route: SimpleNamespace,
    resumable: SimpleNamespace,
    field: str,
    value: str,
) -> None:
    """Every coordinate, by its own name. `database_dir` IS the store a proposal
    is written to and `interpreter` IS the program the private MCP runs, so a
    resume free to change either continues a recorded conversation somewhere its
    admission never bound it to."""
    changed = {**orchestrator_route.binding, field: value}

    answer = _call(
        resumable.server,
        "nspawn_run_start",
        _dispatch(orchestrator_route, session_id="origin-thread", orchestrator=changed),
    )

    assert "result" not in answer, answer
    assert field in answer["error"]["message"], answer
    assert _store_rows("SELECT count(*) FROM runs")[0][0] == 1


def test_a_resume_that_changes_or_omits_the_repository_is_refused(
    registry: Registry,
    tmp_path: Path,
    orchestrator_route: SimpleNamespace,
    resumable: SimpleNamespace,
) -> None:
    """A resume names repository B for a conversation admitted against A.

    `repository` IS the tree a proposal is bound to, so a resume free to change
    it continues a recorded conversation against another working copy, with the
    session, the store and the contract matching to the byte.
    """
    elsewhere = {
        **orchestrator_route.binding,
        "repository": str(tmp_path / "another-repository"),
    }

    changed = _call(
        resumable.server,
        "nspawn_run_start",
        _dispatch(
            orchestrator_route, session_id="origin-thread", orchestrator=elsewhere
        ),
    )

    assert "result" not in changed, changed
    assert "repository" in changed["error"]["message"], changed
    # And a resume that simply leaves it out is refused by the same name rather
    # than taking the cwd for it.
    omitted = {
        key: value
        for key, value in orchestrator_route.binding.items()
        if key != "repository"
    }

    absent = _call(
        resumable.server,
        "nspawn_run_start",
        _dispatch(orchestrator_route, session_id="origin-thread", orchestrator=omitted),
    )

    assert absent.get("error", {}).get("code") == INVALID_PARAMS, absent
    assert "repository" in absent["error"]["message"], absent
    assert _store_rows("SELECT count(*) FROM runs")[0][0] == 1


def test_a_run_recorded_without_a_repository_cannot_be_resumed(
    registry: Registry,
    tmp_path: Path,
    orchestrator_route: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail CLOSED: a record short of the repository answers no resume at all.

    The record is planted on the store directly, because admission itself refuses
    to write one (test_a_malformed_binding_payload_is_refused_before_any_row):
    what is measured here is the READ. A binding is read back through the same
    validation it was written through, so a run whose record cannot say which
    tree its proposals were bound to is refused rather than resumed against the
    half of it that was written down — and never against the cwd.
    """
    monkeypatch.setattr(Supervisor, "_drive_native_run", lambda self, *args: None)
    server = McpServer(supervisor=_sv(registry, tmp_path))
    unbound = _dispatch(orchestrator_route)
    del unbound["orchestrator"]
    run_id = _payload(_call(server, "nspawn_run_start", unbound))["run_id"]
    row = registry.get_run(run_id)
    assert row is not None
    registry.update_run(
        replace(row, thread_id="origin-thread", phase=RunPhase.COMPLETED)
    )
    short = {
        key: value
        for key, value in _recorded(
            profile=f"nott-nspawn-orchestrator-{run_id}"
        ).items()
        if key != "repository"
    }
    seq = _store_rows(
        "SELECT coalesce(max(seq), 0) FROM run_events WHERE run_id = ?", (run_id,)
    )[0][0]
    _store_write(
        "INSERT INTO run_events (run_id, seq, at, kind, payload, chained_hash) "
        "VALUES (?, ?, 1, 'lifecycle', ?, ?)",
        (run_id, seq + 1, json.dumps({"orchestrator_binding": short}), "0" * 64),
    )
    assert _bindings(run_id) == [short]

    answer = _call(
        server,
        "nspawn_run_start",
        _dispatch(orchestrator_route, session_id="origin-thread"),
    )

    assert "result" not in answer, answer
    assert "orchestrator binding" in answer["error"]["message"], answer
    assert _store_rows("SELECT count(*) FROM runs")[0][0] == 1
    # The reader every resume goes through is the one that refuses it.
    with pytest.raises(RegistryError, match="orchestrator binding names exactly"):
        registry_module.validated_orchestrator_binding(short)


def test_a_bound_origin_resumes_on_the_identical_recorded_binding(
    registry: Registry,
    orchestrator_route: SimpleNamespace,
    resumable: SimpleNamespace,
) -> None:
    """The positive control: the complete binding, restated field for field."""
    answer = _call(
        resumable.server,
        "nspawn_run_start",
        _dispatch(orchestrator_route, session_id="origin-thread"),
    )

    accepted = _payload(answer)
    assert accepted["run_id"] != resumable.run_id
    (resumed,) = _bindings(accepted["run_id"])
    # The same binding under the resumed run's OWN profile.
    assert {key: value for key, value in resumed.items() if key != "profile"} == {
        key: value for key, value in resumable.recorded.items() if key != "profile"
    }
    assert resumed["profile"] == f"nott-nspawn-orchestrator-{accepted['run_id']}"


# ---------------------------------------------------------------------------
# The door: an invalid coordinate never reaches a dispatch
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("case", "changes"),
    [
        ("too-long", {"session": "a" * 513}),
        ("empty", {"session": ""}),
        ("non-printable", {"session": "has\x00nul"}),
        ("wrong-type", {"session": 7}),
        ("whitespace", {"project": "two words"}),
        ("relative-path", {"interpreter": "python3"}),
        ("undeclared", {"marketplace": "/elsewhere"}),
    ],
)
def test_the_door_refuses_an_invalid_orchestrator_coordinate(
    registry: Registry,
    tmp_path: Path,
    orchestrator_route: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    changes: dict[str, Any],
) -> None:
    """Invalid input, refused as invalid input: at the door, before the dispatch
    that would launch anything or write a row.

    A session of 513 bytes is the measured case: the registry stores it as an
    identifier and refuses past 512, so a turn admitted on a longer one is a
    turn whose own binding cannot be recorded.
    """
    monkeypatch.setattr(child, "spawn_child", lambda *a, **k: pytest.fail("spawned"))
    server = McpServer(supervisor=_sv(registry, tmp_path))

    answer = _call(
        server,
        "nspawn_run_start",
        _dispatch(
            orchestrator_route,
            orchestrator={**orchestrator_route.binding, **changes},
        ),
    )

    assert answer.get("error", {}).get("code") == INVALID_PARAMS, answer
    assert "orchestrator" in answer["error"]["message"], answer
    assert _store_rows("SELECT count(*) FROM runs")[0][0] == 0
    assert _store_rows("SELECT count(*) FROM run_events")[0][0] == 0
    # Not even a driver ack: the argument never became a dispatch.
    assert sorted((tmp_path / "runs" / "drivers").glob("*.ack")) == []


def test_the_door_refuses_an_orchestrator_that_is_not_an_object(
    registry: Registry,
    tmp_path: Path,
    orchestrator_route: SimpleNamespace,
) -> None:
    server = McpServer(supervisor=_sv(registry, tmp_path))

    answer = _call(
        server, "nspawn_run_start", _dispatch(orchestrator_route, orchestrator=["s"])
    )

    assert answer.get("error", {}).get("code") == INVALID_PARAMS, answer
    assert "orchestrator" in answer["error"]["message"], answer
    assert _store_rows("SELECT count(*) FROM runs")[0][0] == 0


def test_a_session_past_the_registrys_identifier_is_refused_by_the_binding(
    tmp_path: Path,
) -> None:
    """512 is the registry's own identifier ceiling, and the binding's."""
    assert _binding(tmp_path, tmp_path / "snapshot", session="a" * 512).session == (
        "a" * 512
    )
    with pytest.raises(native.NativePreparationError, match="session"):
        _binding(tmp_path, tmp_path / "snapshot", session="a" * 513)
    with pytest.raises(native.NativePreparationError, match="project"):
        _binding(tmp_path, tmp_path / "snapshot", project="p" * 513)


# ---------------------------------------------------------------------------
# The native canary, which this unit does not claim
# ---------------------------------------------------------------------------

_PROBE = "import sys; sys.stdout.write('started')"


def _codex() -> str | None:
    found = shutil.which("codex") or str(Path.home() / ".local/bin/codex")
    return os.path.realpath(found) if os.access(found, os.X_OK) else None


def _inline_table(table: dict[str, Any]) -> str:
    return (
        "{"
        + ", ".join(
            f"{json.dumps(key)}="
            f"{str(value).lower() if isinstance(value, bool) else json.dumps(value)}"
            for key, value in table.items()
        )
        + "}"
    )


@pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="codex sandbox runs bwrap on Linux"
)
def test_the_orchestrator_profile_holds_under_the_real_codex_sandbox(
    tmp_path: Path,
) -> None:
    """The profile under the vendor's own sandbox.

    A nested sandbox cannot start inside the workflow's own, and this case is
    then SKIPPED with the reason the vendor gave: it is never reported as a pass,
    and the installed positive/negative canaries stay the operator's to run.
    """
    codex = _codex()
    if codex is None:
        pytest.skip("no codex binary on this host; the profile is proven where one is")
    snapshot = Path(os.path.realpath(tmp_path)) / "snapshot"
    snapshot.mkdir()
    (snapshot / "read.txt").write_text("readable\n")
    outside = Path(os.path.realpath(tmp_path)) / "outside"
    outside.mkdir()
    name = "nott-nspawn-orchestrator-test"
    profile = app_server.permission_profile((str(snapshot), codex))
    home = Path.home() / ".cache" / f"nspawn-orchestrator-{uuid.uuid4().hex}"

    def caged(*command: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                codex,
                "sandbox",
                "-C",
                str(snapshot),
                "-P",
                name,
                "-c",
                f"permissions.{name}.filesystem={_inline_table(profile['filesystem'])}",
                "-c",
                f"permissions.{name}.network={_inline_table(profile['network'])}",
                "--",
                *command,
            ],
            env={
                "PATH": "/usr/bin:/bin",
                "HOME": str(home),
                "CODEX_HOME": str(home / "codex-home"),
            },
            cwd=snapshot,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    try:
        (home / "codex-home").mkdir(parents=True)
        started = caged("/usr/bin/python3", "-c", _PROBE)
        if started.returncode != 0 or started.stdout.strip() != "started":
            pytest.skip(
                "the nested codex sandbox could not start here: "
                f"{started.stderr.strip()[:200]!r}"
            )
        with socket.create_server(("127.0.0.1", 0)) as listener:
            observed = caged(
                "/usr/bin/python3",
                "-c",
                _CHECKS,
                str(snapshot),
                str(outside),
                str(listener.getsockname()[1]),
            )
    finally:
        shutil.rmtree(home, ignore_errors=True)
    assert observed.returncode == 0, observed.stderr
    results = json.loads(observed.stdout.strip().splitlines()[-1])
    assert {check for check, outcome in results.items() if outcome == "ok"} == {
        "read"
    }, results
    assert list(outside.iterdir()) == []


_CHECKS = r"""
import json, os, socket, sys
snapshot, outside, port = sys.argv[1:]
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
print(json.dumps({
    'read': attempt(lambda: open(os.path.join(snapshot, 'read.txt')).read()),
    'write': attempt(lambda: write(os.path.join(snapshot, 'created.txt'))),
    'outside': attempt(lambda: write(os.path.join(outside, 'escaped.txt'))),
    'tcp': attempt(lambda: socket.create_connection(
        ('127.0.0.1', int(port)), timeout=3).close()),
    'bus': attempt(bus),
}))
"""
