"""Prepare selected native CLI coordinates without starting or modifying a process."""

from __future__ import annotations

import json
import os
import shutil
import stat
import threading
import time
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, ClassVar, Final

import nott_database
from nott_system1 import SCOPES, route_variables

from .child import MEMORY_POLICY_ENV, RUN_ID_ENV, ChildArgvError, validate_read_paths
from .claude_child import (
    ChildMcpConfigError,
    native_binary_allowed,
    read_child_mcp_config,
)
from .errors import NERR_1105, NSpawnError
from .inference import CodexInference, StructuredInference
from .limits import MAX_DURABLE_IDENTIFIER_BYTES
from .owner_roster import (
    NativeAccount,
    Provider,
    Roster,
    credential_source,
    expand_home,
)
from .redact import SecretNeedles
from .rows import NativeSessionContext, validate_system_prompt
from .vendor_home import (
    AUTH_MAX_BYTES,
    CredentialInFlightError,
    VendorHomeError,
    read_owner_config,
    read_owner_credential,
)
from .wire import SANDBOX_TOOL_PATH

# A closed list: neither a new parent integration nor a new secret-shaped name
# acquires child access just because it appeared in the parent environment.
NATIVE_ENV_NAMES = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "LANG",
        "LC_ALL",
        "TERM",
        "TZ",
        "TMPDIR",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "NIX_SSL_CERT_FILE",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "NODE_EXTRA_CA_CERTS",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "XDG_STATE_HOME",
        "XDG_RUNTIME_DIR",
        "DBUS_SESSION_BUS_ADDRESS",
        "GIT_CONFIG_GLOBAL",
        "GH_CONFIG_DIR",
        "UV_CACHE_DIR",
        "NOTT_NMEMORY_CONFIG",
        "NOTT_NSPAWN_CONFIG",
        "NOTT_NSPAWN_ROSTER",
        "NOTT_DATABASE_DIR",
        MEMORY_POLICY_ENV,
    }
)
_HOME_SELECTORS = frozenset(
    {
        "HOME",
        "CODEX_HOME",
        "CODEX_SQLITE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "XDG_STATE_HOME",
        "XDG_RUNTIME_DIR",
    }
)
_MCP_SCOPE_ENV = {
    "nspawn": (
        "NOTT_NSPAWN_CONFIG",
        "NOTT_NSPAWN_ROSTER",
        "NOTT_DATABASE_DIR",
        MEMORY_POLICY_ENV,
    ),
    "nmemory": (
        "NOTT_NMEMORY_CONFIG",
        "NOTT_DATABASE_DIR",
        MEMORY_POLICY_ENV,
        RUN_ID_ENV,
    ),
}
_MCP_SCOPE_NAMES = frozenset(
    name for names in _MCP_SCOPE_ENV.values() for name in names
)
_MCP_OPTIONAL_ENV = {
    "nmemory": (
        *route_variables(),
        *route_variables("TAINT"),
        *route_variables("STAGED"),
        *route_variables("KIND"),
        *route_variables("CURATOR"),
        "OPENAI_API_KEY",
    ),
    "nspawn": (
        *route_variables(),
        *route_variables("EXECUTION_CLASS"),
        "SAKANA_MAX_API_KEY",
        "SAKANA_PRO_API_KEY",
    ),
}
_MCP_ROUTE_SETTINGS = frozenset(
    name for scope in (None, *SCOPES) for name in route_variables(scope)[:2]
)
# An empty route value is unset, as `nott_system1.route` reads it; the base key
# keeps its refusal.
_MCP_ROUTE_OPTIONAL = frozenset(
    name for scope in (None, *SCOPES) for name in route_variables(scope)
) - {route_variables()[2]}
# `env_vars` carries two classes of name that read alike and behave differently.
# A credential above is LOOKED UP: its value leaves in the child's own `env` and
# is retained as a needle. A route here is only a NAME handed to the launcher --
# never read, never retained -- because the codex app-server gives its MCP
# children `HOME LANG PATH SHELL USER` and nothing else it was not told to pass,
# and a child like `jbcontext` cannot reach the session bus without one.
# `_HOME_SELECTORS` is excluded for the same reason the provider `env` block
# below excludes it: those names SELECT where a native account's state lives, and
# this launcher owns that choice. Measured with every selector in the ambient:
# `CODEX_HOME` arrives rewritten to the account home and `CODEX_SQLITE_HOME` is
# dropped, so the exclusion is a boundary over the selection, not a claim that
# each value is rewritten -- `HOME` and the `XDG_*` names do pass through.
# A name in neither class stays refused.
_MCP_FORWARD_ENV = NATIVE_ENV_NAMES - _HOME_SELECTORS


MAX_NATIVE_NEEDLE_BYTES = 16 * AUTH_MAX_BYTES


#: The effort tiers AGY spells inside a model id (`agy models`, 2026-09-20).
AGY_EFFORT_TIERS: Final = frozenset({"low", "medium", "high"})


class NativePreparationError(NSpawnError):
    """A safe refusal: messages carry field names, never credential values."""

    def __init__(self, message: str) -> None:
        self.message = message
        self.entry = NERR_1105
        Exception.__init__(self, message)


#: The private orchestrator MCP as one turn sees it: one server name, the module
#: `nott_harness/orchestrator_mcp.py` is launched as, and the five tools it
#: offers. A tool this table does not name is not a tool of an orchestrator turn.
ORCHESTRATOR_MCP_NAME: Final = "nott_orchestrator"
ORCHESTRATOR_MCP_MODULE: Final = "nott_harness.orchestrator_mcp"
ORCHESTRATOR_MCP_TOOLS: Final = (
    "nott_capsule_show",
    "nott_proposal_create",
    "nott_session_show",
    "nott_workflow_list",
    "nott_workflow_show",
)
#: That server's disposable needs, as leaf names under one root. NOT the store:
#: `--database-dir` states it on the launch, and the server refuses a configured
#: `NOTT_DATABASE_DIR` naming another one -- so planting it here would only turn
#: a bound turn into a refused one.
ORCHESTRATOR_DISPOSABLE: Final = {
    "HOME": "home",
    "TMPDIR": "tmp",
    "XDG_CACHE_HOME": "cache",
}
#: The flags that decide WHICH `nott_harness.orchestrator_mcp` is launched.
#: `python -m` prepends the working directory to `sys.path` before any installed
#: package, and the app-server's working directory IS the snapshot this turn
#: reads -- so a snapshot carrying `nott_harness/orchestrator_mcp.py` would be
#: the private MCP, project code launched as the one server the turn trusts and
#: offering exactly the five names the contract check looks for. `-I` (isolated:
#: no working directory, no user site, no `PYTHON*` variable) is the whole
#: defence, because `--runtime-root` is read by the module import already chose.
ORCHESTRATOR_MCP_ISOLATION: Final = ("-I",)
#: The names the codex app-server hands every MCP child of its own accord
#: (`HOME LANG PATH SHELL USER`, as `_MCP_FORWARD_ENV` above records), named with
#: fixed literals where they are not a path of this turn's own: a per-server
#: `env` is applied OVER those, so a dictionary that leaves one out is not that
#: child's whole environment, it is an overlay on this process's. `USER` is not
#: among them because the launch upstream carries none to forward.
ORCHESTRATOR_MCP_LITERALS: Final = {
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "SHELL": "/bin/sh",
}
#: The vendor child's own disposable state tree, as leaf names under the same
#: run-private root: the account home is read for a credential, never written to
#: or inherited from (`orchestrator_launch_environment`).
ORCHESTRATOR_LAUNCH_DISPOSABLE: Final = {
    "HOME": "vendor-home",
    "TMPDIR": "vendor-tmp",
    "XDG_CONFIG_HOME": "xdg-config",
    "XDG_DATA_HOME": "xdg-data",
    "XDG_CACHE_HOME": "xdg-cache",
    "XDG_STATE_HOME": "xdg-state",
}
#: Fixed operational values, so the host's locale, terminal and shell stay out,
#: and the app-server's own remote control stays closed.
ORCHESTRATOR_LAUNCH_LITERALS: Final = {
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "TERM": "dumb",
    "SHELL": "/bin/sh",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_TERMINAL_PROMPT": "0",
    "CODEX_INTERNAL_APP_SERVER_REMOTE_CONTROL_DISABLED": "1",
}
#: The certificate bundles a turn that reaches its own model provider needs:
#: paths, and the only names carried from the ambient or from a route literal.
ORCHESTRATOR_LAUNCH_CERTIFICATES: Final = (
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "NIX_SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",
)
#: Every hook and plugin an account home would contribute to a turn, closed by
#: the vendor's OWN key names: the three capability features that gate hooks and
#: plugins, and the account's `[plugins]` table emptied, so no plugin is
#: installed to carry a hook in the first place. The launch states these
#: (`orchestrator_startup_arguments`) and the composed thread states them again
#: (`app_server.orchestrator_thread_params`), from here, because the vendor reads
#: configuration at two times and one of them is before its own startup.
#:
#: `projects = {}` AND `mcp_servers = {}` ARE NOT HERE, AND MUST NOT COME BACK.
#: Measured against codex 0.159.2 (M-a, 2026-10-01): an empty inline table is a
#: MERGE NO-OP. With both overrides on the argv the snapshot's project layer still
#: loaded, an `[mcp_servers.X]` it declared still started host-side, and its
#: `[mcp_servers.nott_orchestrator.env]` still merged into the one server the turn
#: trusts; an explicit `trust_level = "untrusted"` for the same tree, at launch or
#: on the thread, did not stop the merge either. The project layer is closed by
#: `orchestrator_neutral_cwd` -- the app-server runs where there is no project to
#: load -- and the server table is DECLARED rather than emptied
#: (`orchestrator_mcp_arguments`), which is also what makes the vendor's own
#: effective view checkable before any thread exists.
ORCHESTRATOR_STARTUP_CLOSURE: Final[dict[str, Any]] = {
    "features.hooks": False,
    "features.plugin_hooks": False,
    "features.plugins": False,
    "plugins": {},
}
#: The fields of a composed MCP server that ONE orchestrator launch declares with
#: `-c`, and the fields `config/read` reports back for comparison (M-e: command,
#: args, env AS MERGED, and enabled). The composed thread's two tool-contract keys
#: are not among them: a key the vendor's own effective view does not represent
#: could not be validated there, and this list is exactly what is validated.
ORCHESTRATOR_LAUNCH_MCP_FIELDS: Final = ("command", "args", "env", "enabled")
#: The leaf of a run's disposable root the app-server RUNS IN, and the markers
#: that disqualify a directory from being it: `.git` anywhere on its path, and
#: `.codex` in the directory itself (see `orchestrator_neutral_refusal`).
ORCHESTRATOR_NEUTRAL_LEAF: Final = "neutral"
ORCHESTRATOR_NEUTRAL_ANCESTOR_MARKER: Final = ".git"
ORCHESTRATOR_NEUTRAL_OWN_MARKER: Final = ".codex"
#: The characters a bare TOML key is made of. A `-c` override is TEXT the vendor
#: parses, and the only key syntax measured against it (M-e) is the bare one, so a
#: server name outside this set refuses the launch rather than reaching the vendor
#: as syntax nobody measured.
_TOML_BARE_KEY: Final = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
)
#: The leaf of a run's directory that holds every disposable need above.
ORCHESTRATOR_DISPOSABLE_DIRECTORY: Final = "disposable"
#: The prefix of an orchestrator turn's permission profile; its run id completes
#: it, so the profile admission RECORDS is the profile the turn starts with, and
#: not a fresh identifier minted later where nothing can compare the two.
ORCHESTRATOR_PROFILE_PREFIX: Final = "nott-nspawn-orchestrator-"

#: A binding field is bounded printable text; a path field is also absolute and
#: canonical, because the profile grants what it says and nothing normalizes it.
#: THE REGISTRY'S OWN IDENTIFIER CEILING, not a number of this module's: every
#: field of a binding is recorded in the admission's `run_events` payload, and a
#: field this module admitted past 512 bytes would be a turn whose own binding
#: the store then refuses to record -- or, worse, to read back on a resume.
_ORCHESTRATOR_FIELD_BYTES: Final = MAX_DURABLE_IDENTIFIER_BYTES


def orchestrator_mcp_environment(disposable_root: str) -> dict[str, str]:
    """The private MCP's WHOLE environment: the sandbox tool PATH and this turn's
    own disposable HOME, TMPDIR and cache under `disposable_root`.

    Nothing else, and that is the point. The server reads its session, project,
    repository, runtime and store off its argv, and the memory it binds takes the
    birth fields of everything it captures from whatever started it -- so an
    account home, a live store, an owner or controller socket or a session bus
    inherited here would decide where a proposal is written and whose it looks
    like, without one argument of one call having claimed either.
    """
    root = Path(disposable_root)
    return {
        "PATH": SANDBOX_TOOL_PATH,
        **{name: str(root / leaf) for name, leaf in ORCHESTRATOR_DISPOSABLE.items()},
        **ORCHESTRATOR_MCP_LITERALS,
    }


def orchestrator_disposable_root(run_dir: Path) -> str:
    """One turn's disposable root, named by the RUN's own directory.

    Admission composes the environment and the drive that follows it creates the
    directories; both name the root through here, so neither can name a root the
    other does not.
    """
    return str(run_dir / ORCHESTRATOR_DISPOSABLE_DIRECTORY)


def orchestrator_disposable_leaves() -> tuple[str, ...]:
    """Every leaf under that root: the vendor child's state tree, the private
    MCP's own, and the NEUTRAL directory the app-server runs in, which the drive
    creates 0700 before either process starts."""
    return tuple(
        sorted(
            {
                *ORCHESTRATOR_DISPOSABLE.values(),
                *ORCHESTRATOR_LAUNCH_DISPOSABLE.values(),
                ORCHESTRATOR_NEUTRAL_LEAF,
            }
        )
    )


def orchestrator_neutral_cwd(disposable_root: str) -> str:
    """The NEUTRAL directory an orchestrator turn's app-server process and its
    thread both run in: one leaf of the RUN's own disposable root.

    NOT THE SNAPSHOT, and this is the whole closure of the project configuration
    layer. Measured against codex 0.159.2 (M-a, 2026-10-01): with the project
    trusted, `-c projects={}` and `-c mcp_servers={}` merge nothing -- the
    snapshot's `.codex/config.toml` still loaded as a project layer, an
    `[mcp_servers.X]` it declared still started host-side, and an
    `[mcp_servers.nott_orchestrator.env]` it declared still merged into the one
    server the turn trusts; `trust_level = "untrusted"` for the same tree did not
    stop it either, at launch or on the thread. What DOES stop it (M-b) is giving
    the vendor no project: a cwd inside no git repository, carrying no `.codex` of
    its own, under the run's own trusted per-run parent. `config/read` from that cwd
    answered `[sessionFlags, user, system]` and the private MCP's environment
    carried nothing of the snapshot.

    The snapshot remains the tree this turn READS and is not weakened by this:
    it is the recorded `snapshot` and the explicit sandbox read root
    (`OrchestratorBinding.read_roots`). It is simply not the directory the vendor
    reads configuration from -- and it is not the private MCP's `--repository`
    either, which is the session's own editable repository, bound and never read.
    """
    return str(Path(disposable_root) / ORCHESTRATOR_NEUTRAL_LEAF)


def orchestrator_neutral_refusal(cwd: str) -> str | None:
    """Why `cwd` is not neutral, as one sentence naming the MARKER, or `None`.

    Two assertions with two different reaches, because the vendor's own reaches
    differ. `.git` is refused in the directory AND in every ancestor up to the
    filesystem root: a repository anywhere above the cwd is a project the vendor
    may treat the tree as having at all. `.codex` is refused in THE DIRECTORY
    ITSELF and nowhere else: the vendor loads a project configuration layer from
    the `.codex` directories between the cwd and the project root, and a cwd with
    no repository on its path has no such root above it, so the only `.codex` that
    could apply to this cwd is its own. Refusing it in every ancestor refused
    every launch on a host whose account home sits above the run root -- the
    vendor's own home is `~/.codex` and the default run root is below `~` -- while
    closing nothing the vendor would have loaded. What stands for the layer itself
    is the `config/read` boundary, which refuses any layer of type `project` on
    this same cwd before a thread may start.

    A launch whose neutral cwd fails either assertion is refused by name rather
    than started. The path is never printed: a refusal carries the field and the
    marker, because a message that quoted the tree would put a run's own layout
    into an operator log for a check that has nothing to do with it.
    """
    if not Path(cwd).is_absolute() or cwd != os.path.normpath(cwd):
        return "an orchestrator turn's neutral cwd must be an absolute canonical path"
    directory = Path(cwd)
    if os.path.lexists(directory / ORCHESTRATOR_NEUTRAL_OWN_MARKER):
        return (
            f"an orchestrator turn's neutral cwd carries "
            f"{ORCHESTRATOR_NEUTRAL_OWN_MARKER} of its own"
        )
    for parent in (directory, *directory.parents):
        if os.path.lexists(parent / ORCHESTRATOR_NEUTRAL_ANCESTOR_MARKER):
            return (
                f"an orchestrator turn's neutral cwd carries "
                f"{ORCHESTRATOR_NEUTRAL_ANCESTOR_MARKER} on its path"
            )
    return None


def orchestrator_launch_environment(
    prepared: Mapping[str, str], *, disposable_root: str, auth_env: str | None = None
) -> dict[str, str]:
    """The WHOLE environment one orchestrator turn's vendor child is launched
    with: not a filter over this process's, a dictionary built here.

    A read-only turn whose one tool is a private MCP has no use for the owner's
    home or user name, the session bus, the controller's runtime directory or the
    live store -- and the memory that MCP binds takes the birth fields of
    everything it captures from whatever started it, so a name forwarded here
    would decide whose a proposal looks like without one argument of one call
    having claimed it.

    Four classes and nothing else: the sandbox tool PATH; a disposable state tree
    under the RUN's own directory, never under the snapshot it only reads; the
    fixed literals above; and the paths the vendor binary itself cannot do
    without -- the certificate bundles, the credential the route declared, and
    `CODEX_HOME`. THAT HOME IS THE ONE ACCOUNT PATH THAT STAYS: the vendor reads
    its OAuth credential and its Responses provider binding from there and has no
    other way to be given either, and a private home would have to hold a copy of
    that credential, which the vendor's own refresh then rotates away from the
    account's. What the home may CONTRIBUTE is closed rather than copied: every
    server it declares is named closed (`orchestrator_inherited_servers`), its
    plugins and skills are not among the read roots, and its hooks and plugins
    are closed at startup by the launch itself
    (`orchestrator_startup_arguments`) as well as by the composed thread.
    """
    root = Path(disposable_root)
    environment = {
        "PATH": SANDBOX_TOOL_PATH,
        **{
            name: str(root / leaf)
            for name, leaf in ORCHESTRATOR_LAUNCH_DISPOSABLE.items()
        },
        **ORCHESTRATOR_LAUNCH_LITERALS,
        "CODEX_HOME": prepared["CODEX_HOME"],
    }
    for name in ORCHESTRATOR_LAUNCH_CERTIFICATES:
        value = prepared.get(name)
        if value is None:
            continue
        if not Path(value).is_absolute():
            raise NativePreparationError(
                f"orchestrator certificate {name} must name an absolute path"
            )
        environment[name] = value
    if auth_env is not None:
        environment[auth_env] = prepared[auth_env]
    if MEMORY_POLICY_ENV in prepared:
        environment[MEMORY_POLICY_ENV] = prepared[MEMORY_POLICY_ENV]
    return environment


def orchestrator_startup_arguments() -> tuple[str, ...]:
    """The two closures above as the arguments ONE orchestrator launch carries
    where every other codex launch carries the hook-trust bypass.

    It is not the same statement in another place. The thread's configuration is
    merged after the process has started, and by then the account's plugins are
    loaded, their hook trust state is read, the project layer is loaded and every
    MCP server the merged configuration declares has been started: `plugins = {}`
    on a thread does not unload what the process loaded, dropping the bypass flag
    does not close a hook the account itself already trusts, and naming one server
    on a thread does not unstart another one. A `-c` override belongs to the
    configuration the vendor loads BEFORE all of it, which is where a launch can
    still close them -- so a turn whose only tool is one private MCP loads,
    enables, starts and fires nothing the account home or the snapshot
    contributes, and the private MCP's own environment is the composed one alone.

    `false` and `{}` are one literal in JSON and in TOML, which is why the
    closure above is rendered rather than spelled twice.
    """
    return tuple(
        argument
        for key, value in ORCHESTRATOR_STARTUP_CLOSURE.items()
        for argument in ("-c", f"{key}={json.dumps(value)}")
    )


def _toml_inline(value: object) -> str:
    """One composed configuration value as the inline TOML a `-c` override is.

    Only the four shapes a composed MCP server holds -- boolean, string, list of
    those, table of those -- and anything else refuses. A `-c` override is TEXT
    the vendor parses, so a value this renderer would have to guess at is a launch
    the vendor reads as something other than the one composed, which is the whole
    class of defect the effective-view check exists to catch.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_inline(item) for item in value) + "]"
    if isinstance(value, Mapping):
        body = []
        for key, item in value.items():
            if not isinstance(key, str):
                raise NativePreparationError(
                    "an orchestrator MCP declaration key is not representable"
                )
            body.append(f"{json.dumps(key)} = {_toml_inline(item)}")
        return "{" + ", ".join(body) + "}"
    raise NativePreparationError(
        "an orchestrator MCP declaration field is not representable"
    )


def orchestrator_launch_mcp_servers(
    servers: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Every MCP server ONE orchestrator launch DECLARES, from the same mapping the
    composed thread carries: the private server's whole launch -- command,
    arguments, environment, enabled -- and `enabled = false` for every name the
    account declared.

    THE LAUNCH IS WHERE THIS BELONGS, and not because a thread cannot say it. An
    empty `[mcp_servers]` override merges nothing (M-a), so emptying the table was
    never a closure at all; and the one place the vendor states the launch it will
    perform, before it has performed any of it, is the effective configuration of
    the PROCESS (M-e: `config/read` answered the private server's command, args and
    merged env with no MCP child started yet). Declaring the server here is what
    makes that answer comparable to this composition, field by field.
    """
    if ORCHESTRATOR_MCP_NAME not in servers:
        raise NativePreparationError(
            f"an orchestrator launch declares {ORCHESTRATOR_MCP_NAME!r} itself"
        )
    declaration: dict[str, dict[str, Any]] = {}
    for name, server in sorted(servers.items()):
        if name != ORCHESTRATOR_MCP_NAME:
            if dict(server) != {"enabled": False}:
                raise NativePreparationError(
                    f"an inherited MCP {name!r} carries more than its own closure"
                )
            declaration[name] = {"enabled": False}
            continue
        for required in ORCHESTRATOR_LAUNCH_MCP_FIELDS:
            if required not in server:
                raise NativePreparationError(
                    f"the private orchestrator MCP declares no {required!r}"
                )
        declaration[name] = {
            required: server[required] for required in ORCHESTRATOR_LAUNCH_MCP_FIELDS
        }
    return declaration


def orchestrator_mcp_arguments(
    declaration: Mapping[str, Mapping[str, Any]],
) -> tuple[str, ...]:
    """That declaration as the startup overrides one launch carries.

    Each override is ROUND-TRIPPED through the vendor's own parser before it is
    handed over: an override that does not come back as the value composed refuses
    the launch, naming the server, instead of reaching the app-server as something
    else. The interpreter is spelled exactly as the binding spelled it -- absolute
    and canonical, never symlink-resolved -- because `-I` is the whole defence that
    keeps a snapshot's own `nott_harness/orchestrator_mcp.py` from being the private
    MCP, and resolving a venv's `bin/python` would run an interpreter whose prefix
    cannot import the staged real one.
    """
    arguments: list[str] = []
    for name, server in declaration.items():
        if not name or set(name) - _TOML_BARE_KEY:
            raise NativePreparationError(
                f"MCP server {name!r} is not nameable as a bare startup override"
            )
        override = f"mcp_servers.{name}={_toml_inline(server)}"
        try:
            parsed = tomllib.loads(override)
        except tomllib.TOMLDecodeError, ValueError, RecursionError:
            raise NativePreparationError(
                f"the orchestrator MCP declaration of {name!r} is not representable"
            ) from None
        if parsed != {"mcp_servers": {name: dict(server)}}:
            raise NativePreparationError(
                f"the orchestrator MCP declaration of {name!r} is not representable"
            )
        arguments += ["-c", override]
    return tuple(arguments)


def orchestrator_inherited_servers(account: NativeAccount) -> dict[str, dict[str, Any]]:
    """Every MCP an orchestrator turn INHERITS, each named closed.

    Composing only the private server would be an OVERLAY: the vendor merges a
    thread's configuration over the file its `CODEX_HOME` holds, so an inherited
    name this mapping never mentions keeps its own definition and starts -- and
    it starts before any live catalog check can refuse the turn. So each one is
    carried, closed, and a name that collides with the private server's is
    refused rather than quietly won by one of two definitions.

    Only the account's own names are carried. A name the installed plugin alone
    declares has no definition in that file, and the launch loads no plugin
    (`features.plugins = false` in `ORCHESTRATOR_STARTUP_CLOSURE`; its empty
    `plugins` table merges nothing): closing it would be a server with no
    transport, which codex 0.159.3 refuses at startup (`invalid transport in
    mcp_servers.<name>`, measured 2026-10-02). The plugin's own claim on the
    private name is refused where its raw declarations are read
    (`_codex_mcp_servers`).
    """
    account_names = _account_declared_servers(account)
    if ORCHESTRATOR_MCP_NAME in account_names:
        raise NativePreparationError(
            f"an inherited MCP declares the private orchestrator name "
            f"{ORCHESTRATOR_MCP_NAME!r}"
        )
    return {name: {"enabled": False} for name in sorted(account_names)}


def _account_declared_servers(account: NativeAccount) -> set[str]:
    """The names the account's OWN `config.toml` declares under `[mcp_servers]`.

    `_codex_mcp_servers` projects the installed plugin's `.mcp.json` alone, so a
    server the account declares for itself was never in any mapping nSPAWN
    composed: it is exactly the name that survives the vendor's own merge. Each
    one names a `command` or a `url`, or the closure composed over it is a
    server with no transport, and the turn is refused here, naming it.
    """
    path = vendor_home(account) / "config.toml"
    if not path.exists():
        return set()
    names = set()
    try:
        config = tomllib.loads(read_owner_config(path).decode("utf-8"))
        servers = config.get("mcp_servers", {})
        if not isinstance(servers, dict):
            raise TypeError
        for name, server in servers.items():
            if (
                not isinstance(name, str)
                or not name
                or not name.isprintable()
                or len(name.encode("utf-8")) > 512
            ):
                raise ValueError
            if not isinstance(server, dict) or not {"command", "url"} & set(server):
                raise NativePreparationError(
                    f"the account's MCP server {name!r} declares no command or url"
                )
            names.add(name)
    except (
        OSError,
        ValueError,
        TypeError,
        AttributeError,
        RecursionError,
        UnicodeDecodeError,
        VendorHomeError,
    ):
        raise NativePreparationError(
            "native orchestrator inherited MCP declarations could not be read"
        ) from None
    return names


#: The snapshot `request_refusal` probes with: absolute, canonical, and never a
#: field the caller states, so a refusal it returns is always the caller's own.
_ORCHESTRATOR_PROBE_SNAPSHOT: Final = "/"


def orchestrator_profile_id(run_id: str) -> str:
    """The permission profile of one orchestrator turn, named by its own run."""
    return ORCHESTRATOR_PROFILE_PREFIX + run_id


@dataclass(frozen=True)
class OrchestratorBinding:
    """One orchestrator turn's conversation session, memory project and paths.

    TWO TREES, and they are not the same one. The `repository` is the session's
    REAL repository root, which the caller states: it is the private MCP's
    `--repository`, so it is where a proposal this turn makes is bound, and the
    turn never reads it and never runs in it. The `snapshot` is the admission's
    own cwd -- the clean INSPECTION COPY the turn reads -- and is never a request
    field, because a caller free to name its own read root would name a tree the
    admission never caged.

    A binding that took the repository from the cwd bound every proposal of the
    conversation to that inspection copy: a directory the run deletes, which no
    later session can act on, while the tree the session actually edits was named
    nowhere on record.
    """

    session: str
    project: str
    repository: str
    snapshot: str
    runtime: str
    database_dir: str
    interpreter: str

    #: Exactly the keys a run start request may carry under `orchestrator`.
    REQUEST_FIELDS: ClassVar[tuple[str, ...]] = (
        "session",
        "project",
        "repository",
        "runtime",
        "database_dir",
        "interpreter",
    )
    #: The fields the permission profile and the argv spell as filesystem paths.
    PATH_FIELDS: ClassVar[tuple[str, ...]] = (
        "repository",
        "snapshot",
        "runtime",
        "database_dir",
        "interpreter",
    )

    def __post_init__(self) -> None:
        for name in ("session", "project", *self.PATH_FIELDS):
            value = getattr(self, name)
            if (
                not isinstance(value, str)
                or not value
                or not value.isprintable()
                or len(value.encode("utf-8")) > _ORCHESTRATOR_FIELD_BYTES
            ):
                raise NativePreparationError(
                    f"orchestrator {name} must be bounded printable text"
                )
        for name in self.PATH_FIELDS:
            value = getattr(self, name)
            if not Path(value).is_absolute() or value != os.path.normpath(value):
                raise NativePreparationError(
                    f"orchestrator {name} must be an absolute canonical path"
                )
        for name in ("session", "project"):
            value = getattr(self, name)
            if value.strip() != value or any(c in value for c in " \t"):
                raise NativePreparationError(
                    f"orchestrator {name} carries no whitespace"
                )

    @classmethod
    def from_request(
        cls, request: Mapping[str, Any], *, snapshot: str
    ) -> OrchestratorBinding:
        """The caller's six coordinates, refused one field name at a time.

        A key beside the six is refused BY ITS NAME rather than ignored, and one
        of the six that is missing is refused by its name too: an argument a
        binding does not declare is a coordinate the caller believes it set, and
        silence there is the whole class of defect this unit closes. In
        particular NOTHING stands in for `repository` -- not this `snapshot`, not
        the cwd it came from. A repository nobody stated is a refusal.
        """
        if not isinstance(request, Mapping):
            raise NativePreparationError("orchestrator binding must be an object")
        undeclared = sorted(set(request) - set(cls.REQUEST_FIELDS))
        if undeclared:
            raise NativePreparationError(
                f"orchestrator binding declares no field {undeclared[0]!r}"
            )
        for name in cls.REQUEST_FIELDS:
            if name not in request:
                raise NativePreparationError(f"orchestrator binding requires {name!r}")
        return cls(
            snapshot=snapshot,
            **{name: request[name] for name in cls.REQUEST_FIELDS},
        )

    @classmethod
    def request_refusal(cls, request: object) -> str | None:
        """What is wrong with a caller's `orchestrator` argument, as one sentence
        naming the field, or `None` for an argument this admission can bind.

        The MCP door answers with this BEFORE it dispatches anything, so a
        coordinate that is too long, empty, blank, not printable, not a path or
        not even a string is invalid input refused as invalid input -- not a run
        that reaches a store write or a launch and fails there. The snapshot is
        not a request field (it is the admission's own cwd), so this probes with
        one that cannot itself be the reason: the six fields the caller states,
        `repository` among them, are the only ones it can refuse.
        """
        if not isinstance(request, Mapping):
            return "orchestrator binding must be an object"
        try:
            cls.from_request(request, snapshot=_ORCHESTRATOR_PROBE_SNAPSHOT)
        except NativePreparationError as refusal:
            return refusal.message
        return None

    def resume_refusal(self, recorded: Mapping[str, Any]) -> str | None:
        """The name of the first field in which this binding differs from the one
        an origin RECORDED, or `None` where the two are identical.

        A resume continues a conversation the store already names. It may
        therefore restate the recorded binding and nothing else: `repository` IS
        the tree a proposal is bound to, `database_dir` IS the store it is written
        to and `interpreter` IS the program the private MCP runs, so a resume free
        to change any of them continues a recorded orchestration somewhere its
        admission never bound it to, with the session and the tool contract
        matching to the byte. The `snapshot` is compared for the same reason, even
        though it is the admission's own cwd: it is the one tree the turn reads.
        The profile is not compared: it names the RUN
        (`orchestrator_profile_id`), and a resume is another run.
        """
        for name in ("session", *self.PATH_FIELDS):
            if recorded.get(name) != getattr(self, name):
                return name
        # The project is a field of the caller's own request, so it answers by its
        # own name; the contract around it answers for everything else in it.
        contract = recorded.get("tool_contract")
        if isinstance(contract, Mapping) and contract.get("project") != self.project:
            return "project"
        if contract != self.tool_contract():
            return "tool_contract"
        return None

    @property
    def read_roots(self) -> tuple[str, ...]:
        """The two trees this turn reads by name: its snapshot and its runtime.

        THE REPOSITORY IS NOT ONE OF THEM, read or write. It is the tree the
        private MCP binds a proposal to, and the whole reason this turn is given
        a clean snapshot is that it must not read the working copy its own
        session is editing.
        """
        return tuple(sorted({self.snapshot, self.runtime}))

    def tool_contract(self) -> dict[str, Any]:
        """The one MCP this turn may call, the project it is bound to, its tools."""
        return {
            "mcp": ORCHESTRATOR_MCP_NAME,
            "project": self.project,
            "tools": list(ORCHESTRATOR_MCP_TOOLS),
        }

    def payload(self, profile: str) -> dict[str, Any]:
        """The lifecycle run event payload admission writes beside the run row:
        THE WHOLE BINDING, the profile and the tool contract.

        Every coordinate, because a resume is checked against this record and
        nothing else (`resume_refusal`): a record of the session and the contract
        alone cannot answer whether a resume names the same repository, the same
        store or the same interpreter, and that silence is what let a bound
        conversation be continued against another `--database-dir`. Both trees are
        written down under their own names -- the `repository` that is bound and
        the `snapshot` that is read -- so the record says which was which.
        """
        return {
            "orchestrator_binding": {
                "session": self.session,
                "profile": profile,
                "tool_contract": self.tool_contract(),
                **{name: getattr(self, name) for name in self.PATH_FIELDS},
            }
        }

    def mcp_servers(self, disposable_root: str) -> dict[str, dict[str, Any]]:
        """Exactly the private orchestrator MCP, bound to this session and project.

        Its argv states the session, the project, the REAL repository, the runtime
        and the store, because that server reads its own argv and no environment
        variable. `--repository` is the session's own editable tree and never the
        inspection snapshot this turn reads: it is where a proposal lands, so a
        snapshot spelled here bound every proposal to a copy that is deleted.
        `env_vars` is empty: not one name of this process is forwarded to it by
        the launcher.
        """
        return {
            ORCHESTRATOR_MCP_NAME: {
                "command": self.interpreter,
                "args": [
                    *ORCHESTRATOR_MCP_ISOLATION,
                    "-m",
                    ORCHESTRATOR_MCP_MODULE,
                    "--session",
                    self.session,
                    "--project",
                    self.project,
                    "--repository",
                    self.repository,
                    "--runtime-root",
                    self.runtime,
                    "--database-dir",
                    self.database_dir,
                ],
                "env": orchestrator_mcp_environment(disposable_root),
                "env_vars": [],
                "enabled": True,
                "enabled_tools": list(ORCHESTRATOR_MCP_TOOLS),
            }
        }


@dataclass(frozen=True)
class NativeLaunch:
    argv: tuple[str, ...]
    env: dict[str, str] = field(repr=False)
    needles: SecretNeedles
    context: NativeSessionContext
    account: NativeAccount
    _credential_path: Path | None = field(default=None, repr=False)
    mcp_servers: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False)
    developer_instructions: str | None = field(default=None, repr=False)
    plugin_skill_roots: dict[str, tuple[str, str]] = field(
        default_factory=dict, repr=False
    )
    inference: CodexInference | None = field(default=None, repr=False)

    def account_identity(self) -> str | None:
        """Read only fenced OAuth metadata; never expose tokens in diagnostics."""
        if self._credential_path is None:
            raise NativePreparationError("native quota requires OAuth credentials")
        try:
            body = json.loads(_credential(self._credential_path))
            tokens = body.get("tokens") if isinstance(body, dict) else None
            identity = tokens.get("account_id") if isinstance(tokens, dict) else None
            if identity is not None and (
                not isinstance(identity, str)
                or not identity
                or len(identity) > 512
                or not identity.isprintable()
            ):
                raise NativePreparationError("native account identity is invalid")
            return identity
        except ValueError, UnicodeError:
            raise NativePreparationError(
                "native account identity is unreadable"
            ) from None

    def refreshed_needles(self, previous: SecretNeedles | None = None) -> SecretNeedles:
        """Retain launch-time and refreshed secrets before writing output.

        Consumers pass their preceding result to retain intermediate rotations.
        The 1 MiB ceiling refuses further persistence instead of forgetting a
        secret; there is no mutable cache or unbounded credential history.
        """
        try:
            retained = (
                self.needles
                if previous is None
                else self.needles.merged(
                    previous, maximum_bytes=MAX_NATIVE_NEEDLE_BYTES
                )
            )
            if self._credential_path is None:
                return retained
            return retained.merged(
                _oauth_needles(self._credential_path),
                maximum_bytes=MAX_NATIVE_NEEDLE_BYTES,
            )
        except ValueError:
            raise NativePreparationError(
                "native credential retention budget exceeded"
            ) from None


def agy_cage_available() -> bool:
    """Whether the trusted runtime has the executable needed for enforcement."""
    from .claude_restricted import RUNTIME_PATH

    selected = shutil.which("bwrap", path=RUNTIME_PATH)
    if selected is None:
        return False
    try:
        binary = Path(selected).resolve(strict=True)
        metadata = binary.stat()
        return (
            stat.S_ISREG(metadata.st_mode)
            and metadata.st_uid in (0, os.geteuid())
            and not metadata.st_mode & 0o022
            and os.access(binary, os.X_OK)
        )
    except OSError, RuntimeError:
        return False


def agy_confined(
    launch: NativeLaunch,
    *,
    cwd: Path,
    run_dir: Path,
    writable: bool,
    customization: Path | None = None,
    read_paths: tuple[Path, ...] = (),
    sockets: tuple[Path, ...] = (),
) -> NativeLaunch:
    """Enforce the selected AGY child's boundary at its existing owning launch.

    The account home stays read-only. Only this run's scratch and the original
    checkout grant can be writable; no vendor state or credential is copied.
    A session's `customization` overlays the home's `.gemini` read-only, and
    its `read_paths` and `sockets` are shown read-only at their own paths.
    """
    from .agy_restricted import compose_restricted_launch

    scratch = run_dir / "scratch"
    scratch.mkdir(mode=0o700, exist_ok=True)
    runtime = [Path(launch.argv[0]).resolve(strict=True)]
    # /etc/ssl/certs entries can link into /etc/ca-certificates (Arch).
    for name in (
        "/etc/hosts",
        "/etc/nsswitch.conf",
        "/etc/ssl/certs",
        "/etc/ca-certificates",
    ):
        path = Path(name)
        if path.exists():
            runtime.append(path.resolve(strict=True))
    for name in (
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "NIX_SSL_CERT_FILE",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "NODE_EXTRA_CA_CERTS",
    ):
        if value := launch.env.get(name):
            runtime.append(Path(value).resolve(strict=True))
    resolver = Path("/etc/resolv.conf")
    aliases: tuple[tuple[Path, Path], ...] = ()
    if resolver.is_symlink():
        aliases = ((resolver.resolve(strict=True), resolver),)
    elif resolver.is_file():
        runtime.append(resolver)
    composed = compose_restricted_launch(
        argv=launch.argv,
        env={**launch.env, "TMPDIR": str(scratch)},
        checkout=cwd,
        home=Path(launch.context.home),
        run_dir=run_dir,
        scratch=scratch,
        writable=writable,
        read_paths=(
            *(Path(path) for path in launch.context.read_roots or ()),
            *read_paths,
        ),
        runtime_paths=tuple(dict.fromkeys(runtime)),
        runtime_aliases=aliases,
        customization=customization,
        sockets=sockets,
    )
    return replace(launch, argv=composed.argv, env=composed.env)


class NativeRedactor(SecretNeedles):
    """Refresh the bounded exact-secret set before each output persistence."""

    def __init__(self, launch: NativeLaunch) -> None:
        super().__init__()
        self._launch = launch
        self._retained = launch.needles
        self._lock = threading.Lock()

    def _snapshot(self) -> SecretNeedles:
        with self._lock:
            self._retained = self._launch.refreshed_needles(self._retained)
            return self._retained

    def is_empty(self) -> bool:
        return self._snapshot().is_empty()

    def redact(self, text: str) -> str:
        return self._snapshot().redact(text)

    def redact_value(self, value: Any) -> Any:
        return self._snapshot().redact_value(value)


#: Pauses between reads of one credential before its fence refusal stands. A
#: vendor CLI rewrites its token in place (truncate, then write), so a read can
#: land on the empty file or trip the double-stat fence mid-write. Back-to-back
#: retries finish inside that window and refuse a rotation that was about to be
#: whole; doubling pauses (310 ms in all) outlast a descheduled writer. Only
#: that in-flight signature is read again: an unsafe owner, mode, link or
#: symlink refuses on the first read, and every retry passes the same fence.
CREDENTIAL_RETRY_PAUSES: Final = (0.01, 0.02, 0.04, 0.08, 0.16)


def _credential(path: Path) -> bytes:
    for pause in (*CREDENTIAL_RETRY_PAUSES, None):
        try:
            return read_owner_credential(path)
        except CredentialInFlightError:
            if pause is None:
                break
            time.sleep(pause)
        except VendorHomeError:
            break
    raise NativePreparationError(
        "native credential source failed its owner-file fence"
    ) from None


def _oauth_needles(path: Path) -> SecretNeedles:
    data = _credential(path)
    try:
        document = json.loads(data)
        token = document["tokens" if path.name == "auth.json" else "token"]
        if not isinstance(token, dict):
            raise TypeError
        _secret(token.get("access_token"))
    except ValueError, UnicodeDecodeError, TypeError, KeyError, RecursionError:
        raise NativePreparationError(
            "native OAuth credential structure is invalid"
        ) from None
    # Known token fields remain secret even below the generic fragment floor.
    exact = tuple(
        value
        for key, value in token.items()
        if key in ("access_token", "refresh_token", "id_token")
        and isinstance(value, str)
        and value
    )
    return SecretNeedles.from_credential(data).merged(SecretNeedles(exact))


def _path(path: Path, *, directory: bool, private: bool = True) -> Path:
    try:
        expanded = expand_home(path)
        if not expanded.is_absolute():
            raise NativePreparationError(
                "native paths must be absolute or owner ~/ paths"
            )
        result = expanded.resolve(strict=True)
        metadata = result.stat()
    except OSError, RuntimeError:
        raise NativePreparationError("native path is unavailable") from None
    if directory:
        if not stat.S_ISDIR(metadata.st_mode):
            raise NativePreparationError(
                "native directory must be an existing directory"
            )
        if private and (metadata.st_uid != os.geteuid() or metadata.st_mode & 0o022):
            raise NativePreparationError(
                "native account directory must be owner-controlled"
            )
        if not private and metadata.st_mode & 0o002:
            raise NativePreparationError(
                "native plugin directory must not be world-writable"
            )
    elif not native_binary_allowed(result, metadata):
        raise NativePreparationError("native binary must be an executable regular file")
    return result


def _secret(value: str | None) -> str:
    try:
        if (
            not isinstance(value, str)
            or not value.strip()
            or len(value.encode("utf-8")) > AUTH_MAX_BYTES
            or any(c in value for c in "\x00\r\n")
        ):
            raise ValueError
    except ValueError:
        raise NativePreparationError(
            "selected credential variable is absent or invalid"
        ) from None
    return value


def vendor_home(account: NativeAccount) -> Path:
    """The account's vendor directory, which a NATIVE route always has.

    `NativeAccount.home` became optional when claude-kind accounts arrived:
    those route by `base_url` and own no directory. Nothing in this module
    serves them — it prepares codex and agy, whose home IS `CODEX_HOME` or the
    agy `HOME` — so the absence is a roster contradiction, refused once here
    rather than narrowed at each of nine uses.
    """
    if account.home is None:
        raise NativePreparationError(
            "a native account must declare its home; only a claude-kind "
            "account routes by base_url instead"
        )
    return account.home


def _api_profile(account: NativeAccount, kind: str) -> None:
    if (
        kind != "codex"
        or account.model_provider is None
        or account.model_provider in ("openai", "ollama", "lmstudio")
    ):
        raise NativePreparationError(
            "API auth requires an explicit Codex Responses provider binding"
        )
    try:
        config = tomllib.loads(
            _credential(vendor_home(account) / "config.toml").decode("utf-8")
        )
        profile = config.get("model_providers", {}).get(account.model_provider)
    except ValueError, UnicodeDecodeError, AttributeError:
        raise NativePreparationError(
            "native provider configuration is malformed"
        ) from None
    if (
        not isinstance(profile, dict)
        or profile.get("env_key") != account.auth_env
        or profile.get("wire_api") != "responses"
        or profile.get("requires_openai_auth", False) is not False
        or "auth" in profile
        or "experimental_bearer_token" in profile
    ):
        raise NativePreparationError(
            "selected native profile does not bind the declared Responses credential"
        )


def request_refusal(
    provider: Provider,
    *,
    read_paths: Sequence[str] = (),
    system_prompt: str | None = None,
) -> str | None:
    """Unimplemented request capabilities, checked before routing or launching."""
    if provider.kind == "agy" and read_paths:
        return "native read_paths confinement is not yet proven"
    if provider.kind == "agy" and system_prompt is not None:
        return "AGY does not support a per-run system_prompt"
    return None


def _codex_skill_roots(
    roster: Roster, account: NativeAccount
) -> dict[str, tuple[str, str]]:
    """Admit only the sanctioned installed version's skills, never its home/cache.

    The native catalog subsequently verifies these exact persisted roots before
    starting a model. A cache entry alone is not proof of native selection.
    """
    if roster.defaults.plugin_root is None:
        return {}
    root = expand_home(roster.defaults.plugin_root)
    # ONE BUNDLE, ONE SKILL TREE PER CLIENT. The owner's bundle ships
    # `clients/{agy,claude,codex}`, each with its own `.codex-plugin` manifest
    # whose `./skills/` means ITS directory, and the marketplace installs the
    # codex plugin FROM `clients/codex`. Reading the bundle root's manifest
    # named the claude tree instead, so every path `plugin/read` advertised
    # failed containment and nERR-2003 refused every codex review (measured
    # 2026-09-20). The root stays the fallback for a bundle with no subtree.
    client = root / "clients/codex"
    if (client / ".codex-plugin/plugin.json").exists():
        root = client
    manifest = root / ".codex-plugin/plugin.json"
    config_path = vendor_home(account) / "config.toml"
    if not manifest.exists() or not config_path.exists():
        return {}
    try:
        raw = read_owner_config(manifest)
        declared = json.loads(raw)
        if not isinstance(declared, dict):
            raise TypeError
        relative = declared.get("skills")
        if relative is None:
            return {}
        name, version = declared["name"], declared["version"]
        config = tomllib.loads(read_owner_config(config_path).decode("utf-8"))
        plugins = config.get("plugins", {})
        if not isinstance(plugins, dict):
            raise TypeError
        matches = [key for key in plugins if key.partition("@")[0] == name]
        if not matches:
            return {}
        if len(matches) != 1:
            raise ValueError
        identity = matches[0]
        parts = identity.split("@")
        policy = plugins[identity]
        if len(parts) != 2 or not isinstance(policy, dict):
            raise ValueError
        enabled = policy.get("enabled", True)
        if type(enabled) is not bool:
            raise ValueError
        if not enabled:
            return {}
        for component in (name, version, parts[1]):
            if (
                not isinstance(component, str)
                or not component
                or len(component) > 512
                or not component.isprintable()
                or component in (".", "..")
                or any(char in component for char in "/\\@")
            ):
                raise ValueError
        if not isinstance(relative, str):
            raise TypeError
        if not relative.isprintable() or len(relative) > 512:
            raise ValueError
        skill_path = Path(relative)
        if skill_path.is_absolute() or not skill_path.parts or ".." in skill_path.parts:
            raise ValueError
        cache = vendor_home(account) / "plugins/cache" / parts[1] / name / version
        skills = cache / skill_path
        # Refuse aliases at every cache ancestor: canonicalization must not turn
        # a named content tree into credentials or unrelated owner data.
        current = account.home
        for component in skills.relative_to(account.home).parts:
            current = current / component
            metadata = current.lstat()
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or metadata.st_mode & 0o022
            ):
                raise ValueError
        if read_owner_config(cache / ".codex-plugin/plugin.json") != raw:
            raise ValueError
        return {identity: (str(skills.resolve(strict=True)), str(root / skill_path))}
    except OSError, RuntimeError, ValueError, TypeError, KeyError, VendorHomeError:
        raise NativePreparationError(
            "native installed plugin skills failed their source/owner fence"
        ) from None


def _codex_mcp_policies(
    root: Path, account: NativeAccount, names: set[str]
) -> dict[str, dict[str, Any]]:
    """Carry the selected plugin's existing policy; never invent approval."""
    manifest = root / ".codex-plugin/plugin.json"
    if not (vendor_home(account) / "config.toml").exists():
        return {}
    try:
        config = tomllib.loads(
            read_owner_config(vendor_home(account) / "config.toml").decode("utf-8")
        )
        direct = config.get("mcp_servers", {})
        if not isinstance(direct, dict):
            raise TypeError
        if names.intersection(direct):
            raise NativePreparationError(
                "native plugin MCP conflicts with an account-level server definition"
            )
        if not manifest.exists():
            return {}
        if not stat.S_ISREG(manifest.lstat().st_mode):
            raise ValueError
        with manifest.open("rb") as handle:
            data = handle.read(AUTH_MAX_BYTES + 1)
        if len(data) > AUTH_MAX_BYTES:
            raise ValueError
        name = json.loads(data)["name"]
        if not isinstance(name, str) or not name:
            raise ValueError
        plugins = config.get("plugins", {})
        matches = [
            value for key, value in plugins.items() if key.partition("@")[0] == name
        ]
        if len(matches) > 1:
            raise ValueError
        if not matches:
            return {}
        plugin = matches[0]
        if not isinstance(plugin, dict) or (
            "enabled" in plugin and type(plugin["enabled"]) is not bool
        ):
            raise ValueError
        if plugin.get("enabled") is False:
            return {server: {"enabled": False} for server in names}
        policies = plugin.get("mcp_servers", {})
        if not isinstance(policies, dict):
            raise TypeError
        selected = {}
        for server in names.intersection(policies):
            policy = policies[server]
            if not isinstance(policy, dict) or set(policy) - {
                "enabled",
                "enabled_tools",
                "disabled_tools",
                "default_tools_approval_mode",
                "tools",
            }:
                raise ValueError
            for key, value in policy.items():
                if key == "enabled" and type(value) is not bool:
                    raise ValueError
                if key in ("enabled_tools", "disabled_tools") and (
                    not isinstance(value, list)
                    or any(not isinstance(tool, str) for tool in value)
                ):
                    raise ValueError
                if key == "default_tools_approval_mode" and value not in (
                    "auto",
                    "prompt",
                    "writes",
                    "approve",
                ):
                    raise ValueError
                if key == "tools":
                    if not isinstance(value, dict):
                        raise ValueError
                    for rule in value.values():
                        if not isinstance(rule, dict) or set(rule) - {
                            "approval_mode",
                            "output_token_limit",
                        }:
                            raise ValueError
                        if "approval_mode" in rule and rule["approval_mode"] not in (
                            "auto",
                            "prompt",
                            "writes",
                            "approve",
                        ):
                            raise ValueError
                        if "output_token_limit" in rule and (
                            type(rule["output_token_limit"]) is not int
                            or rule["output_token_limit"] <= 0
                        ):
                            raise ValueError
            selected[server] = policy
        return selected
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        RecursionError,
        VendorHomeError,
    ):
        raise NativePreparationError(
            "native plugin policy could not be resolved unambiguously"
        ) from None


def _codex_mcp_servers(
    roster: Roster,
    account: NativeAccount,
    needles: SecretNeedles,
    source: Mapping[str, str],
    *,
    orchestrated: bool = False,
    read_only: bool = False,
) -> tuple[dict[str, dict[str, Any]], SecretNeedles]:
    """Bind optional credentials only in per-server RPC config, never argv/env."""
    if roster.defaults.plugin_root is None:
        return {}, needles
    try:
        root = expand_home(roster.defaults.plugin_root)
        servers = read_child_mcp_config(root)
    except ChildMcpConfigError:
        # The reader may quote declaration fields before credentials are bound.
        raise NativePreparationError(
            "native MCP declarations could not be read safely"
        ) from None
    # Before the http filter below, which would hide a pure http entry from any
    # check made on the projection. Only an orchestrator turn has a private
    # server for the name to collide with.
    if orchestrated and ORCHESTRATOR_MCP_NAME in servers:
        raise NativePreparationError(
            f"an installed plugin declares the private orchestrator name "
            f"{ORCHESTRATOR_MCP_NAME!r}"
        )
    # The plugin ships nmemory over http for remote hosts as well; a native child
    # runs on the stdio host, so a pure http entry is not projected. An http entry
    # that also names a command is malformed and still refused below.
    servers = {
        name: declaration
        for name, declaration in servers.items()
        if (declaration.get("type") != "http" or {"command", "args"} & set(declaration))
        and not (
            read_only and source.get(MEMORY_POLICY_ENV) == "push" and name == "nmemory"
        )
    }
    projected: dict[str, dict[str, Any]] = {}
    policies = _codex_mcp_policies(root, account, set(servers))
    environments: dict[str, dict[str, str]] = {}
    forwarded: dict[str, list[str]] = {}
    for name, declaration in servers.items():
        variables = declaration.get("env_vars", [])
        credentials = _MCP_OPTIONAL_ENV.get(name, ())
        if (
            not isinstance(variables, list)
            or any(
                not isinstance(variable, str)
                or (variable not in credentials and variable not in _MCP_FORWARD_ENV)
                for variable in variables
            )
            or len(set(variables)) != len(variables)
        ):
            raise NativePreparationError(
                "native MCP env_vars must name unique variables that are either "
                "a credential admitted for this server or an admitted route"
            )
        environments[name] = {}
        forwarded[name] = [
            variable for variable in variables if variable not in credentials
        ]
        for variable in variables:
            if variable not in credentials or variable not in source:
                continue
            value = source[variable]
            if (
                variable in _MCP_ROUTE_OPTIONAL
                and isinstance(value, str)
                and not value.strip()
            ):
                continue
            secret = _secret(value)
            environments[name][variable] = secret
            if variable not in _MCP_ROUTE_SETTINGS:
                try:
                    needles = needles.merged(
                        SecretNeedles.from_credential(secret.encode("utf-8")),
                        maximum_bytes=MAX_NATIVE_NEEDLE_BYTES,
                    )
                except ValueError:
                    raise NativePreparationError(
                        "native credential retention budget exceeded"
                    ) from None
    for name, declaration in servers.items():
        command = declaration.get("command")
        arguments = declaration.get("args", [])
        if (
            declaration.get("type", "stdio") != "stdio"
            or set(declaration) - {"type", "command", "args", "env", "env_vars"}
            or not isinstance(command, str)
            or not command.strip()
            or not isinstance(arguments, list)
            or any(not isinstance(value, str) for value in arguments)
        ):
            raise NativePreparationError(
                "native MCP requires a declared stdio command and string arguments"
            )
        for value in (name, command, *arguments):
            try:
                value.encode("utf-8")
            except UnicodeError:
                raise NativePreparationError(
                    "native MCP argv must be valid UTF-8"
                ) from None
            if any(c in value for c in "$\x00\r\n") or needles.redact(value) != value:
                raise NativePreparationError(
                    "native MCP argv must be literal and contain no credentials"
                )
        names = list(_MCP_SCOPE_ENV.get(name, ())) + forwarded[name]
        for variable, reference in declaration.get("env", {}).items():
            if variable not in NATIVE_ENV_NAMES or reference != "${" + variable + "}":
                raise NativePreparationError(
                    "native MCP env must reference an admitted variable by the same name"
                )
            names.append(variable)
        projected[name] = {
            "command": command,
            "args": arguments,
            "env_vars": list(dict.fromkeys(names)),
            **policies.get(name, {}),
        }
        if environments[name]:
            # https://learn.chatgpt.com/docs/extend/mcp#stdio-servers
            projected[name]["env"] = environments[name]
    return projected, needles


def readonly_mcp_servers(
    declared: dict[str, dict[str, Any]], catalog: object, needles: SecretNeedles
) -> dict[str, dict[str, Any]]:
    """Veto every inherited name; preserve only declared read-only memory access."""
    if not isinstance(catalog, list) or len(catalog) > 64:
        raise NativePreparationError("native MCP catalog must be a bounded list")
    servers: dict[str, dict[str, Any]] = {}
    for entry in catalog:
        if not isinstance(entry, dict):
            raise NativePreparationError("native MCP catalog entry must be an object")
        name, transport = entry.get("name"), entry.get("transport")
        if (
            not isinstance(name, str)
            or not name
            or not name.isprintable()
            or len(name.encode("utf-8")) > 512
            or name in servers
            or not isinstance(transport, dict)
        ):
            raise NativePreparationError("native MCP catalog identity is invalid")
        selectors = [key for key in ("command", "url") if key in transport]
        if len(selectors) != 1:
            raise NativePreparationError("native MCP catalog transport is ambiguous")
        key = selectors[0]
        value = transport[key]
        if (
            not isinstance(value, str)
            or not value
            or not value.isprintable()
            or len(value.encode("utf-8")) > AUTH_MAX_BYTES
            or needles.redact(value) != value
            or needles.redact(name) != name
        ):
            raise NativePreparationError(
                "native MCP catalog transport is not public text"
            )
        if key == "url":
            from urllib.parse import urlsplit

            try:
                parsed = urlsplit(value)
            except ValueError:
                raise NativePreparationError(
                    "native MCP catalog URL is malformed"
                ) from None
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise NativePreparationError(
                    "native MCP catalog URL carries private parameters"
                )
        servers[name] = {key: value, "enabled": False}
    servers.update({name: dict(value) for name, value in declared.items()})
    for name, server in servers.items():
        if name != "nmemory" or name not in declared:
            server["enabled"] = False
            continue
        arguments = server.get("args", [])
        server["args"] = (
            arguments if "--read-only" in arguments else [*arguments, "--read-only"]
        )
        allowed = server.get("enabled_tools")
        server["enabled_tools"] = (
            ["nmemory_context_show"]
            if allowed is None or "nmemory_context_show" in allowed
            else []
        )
    return servers


def _codex_developer_instructions(
    roster: Roster, account: NativeAccount, cwd: Path, system_prompt: str | None
) -> str | None:
    """Append owner and run instructions without overriding vendor base instructions.

    Each source is bounded independently; the composed text belongs to the
    thread RPC, not the durable row containing only the per-run prompt.
    """
    try:
        config_path = vendor_home(account) / "config.toml"
        try:
            config_path.lstat()
        except FileNotFoundError:
            configured = None
        else:
            configured = tomllib.loads(
                read_owner_config(config_path).decode("utf-8")
            ).get("developer_instructions")
        parts = []
        if configured is not None:
            # Codex permits an empty account instruction string.
            if not isinstance(configured, str):
                raise ValueError
            if configured:
                parts.append(validate_system_prompt(configured))
        if roster.defaults.system_prompt is not None:
            path = expand_home(Path(roster.defaults.system_prompt))
            if not path.is_absolute():
                path = cwd / path
            parts.append(
                validate_system_prompt(read_owner_config(path).decode("utf-8"))
            )
        if system_prompt is not None:
            parts.append(validate_system_prompt(system_prompt))
        return "\n\n".join(parts) if parts else None
    except OSError, ValueError, VendorHomeError:
        raise NativePreparationError(
            "native developer instructions require valid bounded owner sources"
        ) from None


def prepare_native(
    provider: Provider,
    account_name: str | None,
    *,
    roster: Roster,
    cwd: Path,
    read_only: bool,
    read_paths: Sequence[str] = (),
    resume_id: str | None = None,
    system_prompt: str | None = None,
    effort: str | None = None,
    ambient: Mapping[str, str] | None = None,
    run_id: str | None = None,
    orchestrator: OrchestratorBinding | None = None,
    disposable_root: str | None = None,
    inference_profile: StructuredInference | None = None,
) -> NativeLaunch:
    """Resolve one account into a closed argv/environment and non-secret context.

    With `run_id`, the launch carries the run marker (`NOTT_NSPAWN_RUN_ID`):
    in its env, and for codex also into its shells, whose environment policy
    inherits nothing — so a hook inside the run knows it is one.

    With an `orchestrator` binding the launch is that turn's: it reads its clean
    snapshot and its trusted runtime and nothing else, its environment is the
    closed one `orchestrator_launch_environment` builds over `disposable_root`,
    and every MCP it would otherwise inherit is carried CLOSED beside the private
    server the binding composes.
    """
    if provider.kind not in ("codex", "agy"):
        raise NativePreparationError("native preparation requires kind codex or agy")
    if inference_profile is not None:
        if (
            provider.kind != "codex"
            or not read_only
            or orchestrator is not None
            or read_paths
            or system_prompt is not None
            or disposable_root is None
        ):
            raise NativePreparationError(
                "native inference requires a closed read-only codex run"
            )
        refusal = orchestrator_neutral_refusal(
            orchestrator_neutral_cwd(disposable_root)
        )
        if refusal is not None:
            raise NativePreparationError("native inference requires a neutral run cwd")
        if set(provider.env) - set(ORCHESTRATOR_LAUNCH_CERTIFICATES):
            raise NativePreparationError(
                "native inference carries no ambient route environment"
            )
    if orchestrator is not None:
        if provider.kind != "codex" or not read_only:
            raise NativePreparationError(
                "an orchestrator binding requires a read-only codex turn"
            )
        if read_paths:
            raise NativePreparationError(
                "an orchestrator turn grants no read_paths beside its binding"
            )
        if disposable_root is None:
            raise NativePreparationError(
                "an orchestrator turn names the disposable root of its own run"
            )
        if not Path(
            disposable_root
        ).is_absolute() or disposable_root != os.path.normpath(disposable_root):
            raise NativePreparationError(
                "an orchestrator turn's disposable root must be an absolute "
                "canonical path"
            )
        # The app-server runs in a NEUTRAL directory of this run, not in the
        # snapshot: refused here by the marker's own name, so a run root planted
        # under a `.git`, or one whose neutral directory carries a `.codex` of its
        # own, is a refusal and never a launch. The drive asserts it again once
        # the directory exists.
        if (
            refusal := orchestrator_neutral_refusal(
                orchestrator_neutral_cwd(disposable_root)
            )
        ) is not None:
            raise NativePreparationError(refusal)
        # A route literal is the OWNER's, so one this environment may not carry
        # is refused rather than dropped: `NATIVE_ENV_NAMES` admits
        # `DBUS_SESSION_BUS_ADDRESS` and `XDG_RUNTIME_DIR` among others, and
        # discarding one would leave a declaration that reads as honoured.
        beside = sorted(set(provider.env) - set(ORCHESTRATOR_LAUNCH_CERTIFICATES))
        if beside:
            raise NativePreparationError(
                f"an orchestrator turn carries no route environment literal "
                f"{beside[0]!r} beside its certificates"
            )
    elif disposable_root is not None and inference_profile is None:
        raise NativePreparationError(
            "a disposable root belongs to an orchestrator turn and to no other"
        )
    if refusal := request_refusal(
        provider, read_paths=read_paths, system_prompt=system_prompt
    ):
        raise NativePreparationError(refusal)
    try:
        granted_paths = validate_read_paths(read_paths, read_only=read_only)
    except ChildArgvError as error:
        raise NativePreparationError(str(error)) from error
    if provider.args:
        raise NativePreparationError("arbitrary native provider args are not supported")
    if (
        provider.auth_env is not None
        or provider.token_file is not None
        or provider.oauth_token_file is not None
        # A shared `secrets` directory is legitimate ALONGSIDE native accounts:
        # `prepare` reads `<secrets>/<alias>.token` for the SELECTED account
        # (test_native_default_token_matches_resolved_account). Without a single
        # declared account there is no alias to bind it to, so the directory is a
        # provider-level legacy credential and reaching a native child with it is
        # exactly what this guard exists to refuse.
        #
        # IT TAKES NO CAPABILITY AWAY, and that is measured, not asserted. The
        # shape `owner_roster.py:585-600` admits — DIRECT_KIND, `bin`, `model`,
        # a populated `secrets` directory, ZERO declared accounts — was already
        # refused here before this line existed, because `resolve_native_account`
        # finds no declared alias to bind the discovered token to. Both runs, on
        # one fixture roster carrying a readable `solo.token`:
        #   a6809e29: NativePreparationError: selected native account
        #             coordinates are invalid
        #   here:     NativePreparationError: native credentials must belong to
        #             the selected account
        # Same refusal, and the message now names the credential rather than
        # blaming coordinates the operator never wrote.
        or (provider.secrets is not None and not provider.accounts)
    ):
        raise NativePreparationError(
            "native credentials must belong to the selected account"
        )
    if provider.bin is None:
        raise NativePreparationError("native provider must declare its binary")
    if not isinstance(cwd, Path) or not cwd.is_absolute() or not cwd.is_dir():
        raise NativePreparationError(
            "native cwd must name an existing absolute directory"
        )
    try:
        if account_name is None or account_name == provider.name:
            account_name = next(iter(sorted(provider.accounts)), None)
        resolved = provider.resolve_native_account(account_name)
        if provider.kind == "codex" and resolved.auth_env is None:
            if resolved.model_provider not in (None, "openai"):
                raise NativePreparationError(
                    "custom native provider requires explicit API auth"
                )
            resolved = replace(resolved, model_provider="openai")
        account = replace(
            resolved,
            home=_path(vendor_home(resolved), directory=True),
            sqlite_home=_path(resolved.sqlite_home, directory=True)
            if resolved.sqlite_home is not None
            else None,
        )
        context = NativeSessionContext(
            str(account.home),
            str(account.sqlite_home) if account.sqlite_home is not None else None,
            account.model_provider,
            account.auth_env,
        )
    except ValueError:
        raise NativePreparationError(
            "selected native account coordinates are invalid"
        ) from None
    binary = _path(provider.bin, directory=False)
    plugin_skill_roots = {}
    if provider.kind == "codex" and read_only and inference_profile is None:
        # An orchestrator turn reads the two trees its binding names. The vendor's
        # own skill trees are not among them: a turn whose tools are one private
        # MCP has nothing to read them with, and a root nobody uses is a root
        # granted for nothing.
        plugin_skill_roots = (
            {} if orchestrator is not None else _codex_skill_roots(roster, account)
        )
        try:
            roots = {str(cwd.resolve(strict=True)), str(binary)}
            if orchestrator is not None:
                roots.update(
                    str(Path(root).resolve(strict=True))
                    for root in orchestrator.read_roots
                )
            roots.update(str(Path(path).resolve(strict=True)) for path in granted_paths)
            roots.update(
                installed for installed, _source in plugin_skill_roots.values()
            )
        except OSError, RuntimeError:
            raise NativePreparationError("native read roots are unavailable") from None
        if len(roots) > 10:
            raise NativePreparationError("native read roots exceed the ten-root limit")
        context = replace(context, read_roots=tuple(sorted(roots)))
    source = os.environ if ambient is None else ambient
    if MEMORY_POLICY_ENV in source and source[MEMORY_POLICY_ENV] != "push":
        raise NativePreparationError("NOTT_MEMORY_POLICY must be push when set")
    env = {key: value for key, value in source.items() if key in NATIVE_ENV_NAMES}
    for value in env.values():
        if (
            not isinstance(value, str)
            or len(value.encode("utf-8")) > AUTH_MAX_BYTES
            or "\x00" in value
        ):
            raise NativePreparationError(
                "native operational environment value is invalid"
            )
    for key, value in provider.env.items():
        if key == MEMORY_POLICY_ENV:
            continue
        if (
            key not in NATIVE_ENV_NAMES
            or key in _HOME_SELECTORS
            or key in _MCP_SCOPE_NAMES
        ):
            raise NativePreparationError(
                "native provider env contains a forbidden override"
            )
        if (
            not isinstance(value, str)
            or "$" in value
            or any(c in value for c in "\x00\r\n")
        ):
            raise NativePreparationError(
                "native provider env requires literal operational values"
            )
        env[key] = value
    credential_path = None
    if account.auth_env is not None:
        if (
            account.auth_env in NATIVE_ENV_NAMES | _HOME_SELECTORS
            or account.auth_env in ("PYTHONPATH", "NODE_OPTIONS", "BASH_ENV", "ENV")
            or account.auth_env.startswith(
                (
                    "CLAUDE_",
                    "ANTHROPIC_",
                    "CODEX_",
                    "LD_",
                    "DYLD_",
                    "GIT_",
                )
            )
        ):
            raise NativePreparationError(
                "native auth_env conflicts with process configuration"
            )
        _api_profile(account, provider.kind)
        # The same per-account token resolver as Claude; auth_env names only
        # the variable the selected Codex profile consumes, never a source.
        try:
            token_path = Path(credential_source(provider, account_name).location)
            secret = _secret(_credential(token_path).decode("utf-8").strip())
        except ValueError:
            raise NativePreparationError(
                "selected native account token file is missing or invalid"
            ) from None
        env[account.auth_env] = secret
        needles = SecretNeedles.from_credential(secret.encode("utf-8"))
        # A custom profile can share a Codex home with an OAuth subscription.
        # Those on-disk tokens remain visible to the CLI even when it uses API auth.
        visible_oauth = vendor_home(account) / "auth.json"
        try:
            visible_oauth.lstat()
        except FileNotFoundError:
            pass
        except OSError:
            raise NativePreparationError(
                "native OAuth source cannot be inspected"
            ) from None
        else:
            credential_path = visible_oauth
            needles = needles.merged(_oauth_needles(visible_oauth))
    else:
        # `vendor_home` validated the home above, so the source resolves.
        oauth = credential_source(provider, account_name)
        # The source names the file inside the account home; the home itself is
        # the one `_path` validated above.
        credential_path = vendor_home(account) / Path(oauth.location).relative_to(
            vendor_home(resolved)
        )
        needles = _oauth_needles(credential_path)
    # The child's HOME is rewritten to the account home, so the store directory is
    # named explicitly: the one this process was given, else this account's live one.
    store_dir = env.get(nott_database.DIRECTORY_VARIABLE) or str(
        nott_database.live_directory()
    )
    if not Path(store_dir).is_absolute():
        raise NativePreparationError("native store directory must be absolute")
    env[nott_database.DIRECTORY_VARIABLE] = store_dir
    if provider.kind == "codex":
        if roster.defaults.plugin_root is not None:
            env["NOTT_NSPAWN_PLUGIN_ROOT"] = str(
                _path(roster.defaults.plugin_root, directory=True, private=False)
            )
        env["CODEX_HOME"] = str(account.home)
        if account.sqlite_home is not None:
            env["CODEX_SQLITE_HOME"] = str(account.sqlite_home)
        if orchestrator is not None or inference_profile is not None:
            assert disposable_root is not None
            env = orchestrator_launch_environment(
                env, disposable_root=disposable_root, auth_env=account.auth_env
            )
            if inference_profile is not None and account.sqlite_home is not None:
                env["CODEX_SQLITE_HOME"] = str(account.sqlite_home)
        # https://learn.chatgpt.com/docs/config-file/config-reference
        # Codex runs a plugin's hooks only once TRUSTED per version (hooks.state) or
        # under this flag; untrusted hooks are skipped without a word. Measured
        # 2026-09-14 in a live `codex exec`: without it `git push origin fix/x:main`
        # landed with every NOTT guard "installed". nSPAWN is the automation that
        # vets the hook source (the bundle it installs), which is what the flag is for.
        # AN ORCHESTRATOR TURN IS THE ONE LAUNCH THAT ASKS FOR NEITHER: it states
        # the startup closure instead, where the flag would stand.
        argv: tuple[str, ...] = (
            str(binary),
            *(
                orchestrator_startup_arguments()
                if orchestrator is not None or inference_profile is not None
                else ("--dangerously-bypass-hook-trust",)
            ),
            "-c",
            'cli_auth_credentials_store="file"',
            "-c",
            "features.memories=false",
            "app-server",
            "--listen",
            "stdio://",
        )
    else:
        if (
            provider.model is None
            or not provider.model.strip()
            or any(c in provider.model for c in "\x00\r\n")
        ):
            raise NativePreparationError(
                "native AGY provider must declare a valid model"
            )
        env["HOME"] = str(account.home)
        # Vendor opt-out: https://antigravity.google/docs/cli/troubleshooting/
        env["AGY_CLI_DISABLE_AUTO_UPDATE"] = "true"
        # THE TIER SUFFIX IS THE EFFORT AXIS, NOT A SECOND KNOB BESIDE IT.
        # Measured 2026-09-20 against the live CLI: `--model=...-low` with
        # `--effort=low` is accepted and the same model with `medium` or
        # `high` dies as «invalid model selection». So an effort does not
        # accompany the model, it SELECTS it: asking for `medium` on a route
        # whose default id ends in `-high` is a request for the medium id, and
        # answering it is what the route actually serves. Refusing instead
        # denied six of the roster's eight classes -- including `design`, the
        # class this route is ranked highest for. A tier the roster never
        # declared is still refused: substituting one would invent a route.
        model = provider.model
        head, _, tier = model.rpartition("-")
        if tier in AGY_EFFORT_TIERS:
            if effort is not None and effort != tier:
                model = f"{head}-{effort}"
                if provider.models and model not in provider.models:
                    raise NativePreparationError(
                        f"native AGY effort {effort} needs model {model}, "
                        f"which provider {provider.name} does not serve"
                    )
            effort = None
        argv = (
            str(binary),
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--model=" + model,
            "--mode=" + ("plan" if read_only else "accept-edits"),
            "--sandbox",
        )
        for flag, argument in (("--effort=", effort), ("--conversation=", resume_id)):
            if argument is not None:
                if (
                    not argument.strip()
                    or len(argument.encode("utf-8")) > 512
                    or any(c in argument for c in "\x00\r\n")
                ):
                    raise NativePreparationError(
                        "native AGY effort/conversation is invalid"
                    )
                argv += (flag + argument,)
    if run_id is not None:
        env[RUN_ID_ENV] = run_id
        if provider.kind == "codex":
            # Before the subcommand: `argv[:-3]` is how the MCP catalog call
            # swaps `app-server --listen stdio://` for its own.
            argv = (
                *argv[:-3],
                "-c",
                f'shell_environment_policy.set.{RUN_ID_ENV}="{run_id}"',
                *argv[-3:],
            )
    mcp_servers: dict[str, dict[str, Any]] = {}
    inference = None
    if inference_profile is not None:
        assert (
            disposable_root is not None
            and provider.model is not None
            and account.home is not None
        )
        try:
            inference = CodexInference.prepare(
                account.home,
                Path(disposable_root),
                provider.model,
                tuple(_account_declared_servers(account)),
            )
        except ValueError as error:
            raise NativePreparationError(str(error)) from None
        config = inference.config()
        argv = (
            *argv[:-3],
            *(
                argument
                for key, value in config.items()
                if key != "mcp_servers"
                for argument in ("-c", f"{key}={json.dumps(value)}")
            ),
            *orchestrator_mcp_arguments(config["mcp_servers"]),
            *argv[-3:],
        )
    elif provider.kind == "codex":
        declared, needles = _codex_mcp_servers(
            roster,
            account,
            needles,
            source,
            orchestrated=orchestrator is not None,
            read_only=read_only,
        )
        mcp_servers = (
            declared
            if orchestrator is None
            else orchestrator_inherited_servers(account)
        )
    if orchestrator is not None:
        assert disposable_root is not None
        # The whole MCP table of this turn, DECLARED at startup: the private
        # server in full and every inherited name disabled. Before the subcommand
        # -- `argv[:-3]` is `app-server --listen stdio://` -- because a `-c`
        # override belongs to the configuration the vendor loads before its own
        # startup, which is the configuration `config/read` then reports and
        # `app_server.orchestrator_config_violation` validates.
        argv = (
            *argv[:-3],
            *orchestrator_mcp_arguments(
                orchestrator_launch_mcp_servers(
                    {**mcp_servers, **orchestrator.mcp_servers(disposable_root)}
                )
            ),
            *argv[-3:],
        )
    return NativeLaunch(
        argv,
        env,
        needles,
        context,
        account,
        credential_path,
        mcp_servers,
        _codex_developer_instructions(roster, account, cwd, system_prompt)
        if provider.kind == "codex" and inference_profile is None
        else None,
        plugin_skill_roots,
        inference,
    )
