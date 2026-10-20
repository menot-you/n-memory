"""One native Codex turn over the existing bounded child custody boundary."""

from __future__ import annotations

import json
import math
import os
import queue
import re
import subprocess
import time
import uuid
from collections import deque
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import Any, Final, Self

from . import child, wire
from .custody import ExecutorScope
from .errors import NERR_1126, NERR_2003, NSpawnError
from .inference import CodexInference, StructuredInference
from .native import (
    ORCHESTRATOR_LAUNCH_MCP_FIELDS,
    ORCHESTRATOR_MCP_NAME,
    ORCHESTRATOR_STARTUP_CLOSURE,
    NativePreparationError,
    OrchestratorBinding,
    orchestrator_launch_mcp_servers,
)
from .pump import MAX_OUTPUT_LINE_BYTES, PumpLimitExceeded
from .quota_sources import (
    SourceReading,
    parse_codex_quota_response,
    parse_codex_rate_limits,
)
from .redact import SecretNeedles
from .vendor_home import VendorHomeError, read_owner_config

MAX_PENDING_FRAMES = 64
#: Raw bytes of notifications held while a handshake call awaits its response.
#: Raw, not budget bytes: the run budget charges post-redaction lines, and a
#: redacted credential echo shrinks there while its frame does not. Room for
#: the largest line the pump admits, which a capped command can send before the
#: `turn/start` response, plus the chatter around it.
MAX_PENDING_BYTES = MAX_OUTPUT_LINE_BYTES + 4 * 1024 * 1024
#: What codex reports for a thread started with a native write profile (codex
#: 0.157.1, observed 2026-09-28): the profile, not the legacy writable roots,
#: names what is writable, and the network is off.
NATIVE_WRITE_SANDBOX: Final = {
    "type": "workspaceWrite",
    "writableRoots": [],
    "networkAccess": False,
    "excludeTmpdirEnvVar": True,
    "excludeSlashTmp": True,
}
RPC_TIMEOUT_SECONDS = 30.0
MAX_REQUEST_BYTES = MAX_OUTPUT_LINE_BYTES + 16384


class AppServerError(NSpawnError):
    """The native server did not complete the requested protocol exchange."""

    def __init__(self, message: str) -> None:
        super().__init__(NERR_2003, message)


class AppServerDeadlineExceeded(AppServerError):
    """The caller's turn deadline elapsed before completion."""


class AppServerStartupTimeout(AppServerError):
    """A handshake request got no answer within `RPC_TIMEOUT_SECONDS`.

    The run's own deadline had not elapsed: this is the vendor not starting,
    not the turn running out of time."""


class AppServerRefused(AppServerError):
    """The native server answered a request with an error."""


#: The thread sources an external session can come from. nSPAWN's own threads
#: are `appServer` too, so the run registry, not the source, tells them apart.
THREAD_SOURCES: Final = ("cli", "vscode", "exec", "appServer", "unknown")
THREAD_PAGES: Final = 4
#: The page size, not the caller's limit: held threads must not use up the pages.
THREAD_PAGE_SIZE: Final = 200


class _PluginSkillReason(StrEnum):
    SUMMARY = "native admitted plugin summary is malformed"
    DISABLED = "native admitted plugin is not enabled"
    UNINSTALLED = "native admitted plugin is not installed"
    SKILLS_TYPE = "native admitted plugin skills are not a list"
    SKILLS_COUNT = "native admitted plugin skill count is outside its limit"
    ENTRY = "native admitted plugin skill is malformed"
    PATH = "native admitted plugin skill path is invalid"
    VERSION = "native advertised plugin skills differ from the admitted version"
    NO_ENABLED_SKILL = "native admitted plugin has no enabled skill paths"
    NOT_SELECTED = "native admitted skill plugin is not enabled/installed"


class PluginSkillsRefused(NSpawnError):
    """The vendor does not hold the admitted plugin skills: a policy refusal that
    every attempt meets again, never a protocol failure to retry."""

    def __init__(self, message: str) -> None:
        self.reason = message if isinstance(message, _PluginSkillReason) else None
        super().__init__(NERR_1126, message)


def _verify_plugin_skills(
    body: dict[str, Any], root: str, source: str | None = None
) -> None:
    """Native advertisement may verify a persisted grant, never widen it."""
    summary, skills = body.get("summary"), body.get("skills")
    if not isinstance(summary, dict):
        raise PluginSkillsRefused(_PluginSkillReason.SUMMARY)
    if summary.get("enabled") is not True:
        raise PluginSkillsRefused(_PluginSkillReason.DISABLED)
    if summary.get("installed") is not True:
        raise PluginSkillsRefused(_PluginSkillReason.UNINSTALLED)
    if not isinstance(skills, list):
        raise PluginSkillsRefused(_PluginSkillReason.SKILLS_TYPE)
    if not 1 <= len(skills) <= 1024:
        raise PluginSkillsRefused(_PluginSkillReason.SKILLS_COUNT)
    expected = Path(root)
    checked = 0
    for skill in skills:
        if not isinstance(skill, dict) or type(skill.get("enabled")) is not bool:
            raise PluginSkillsRefused(_PluginSkillReason.ENTRY)
        if not skill["enabled"]:
            continue
        path = skill.get("path")
        if not isinstance(path, str) or not path.isprintable():
            raise PluginSkillsRefused(_PluginSkillReason.PATH)
        try:
            actual = Path(path)
            if not actual.is_absolute() or not actual.is_file():
                raise ValueError
            resolved = actual.resolve(strict=True)
            if actual.is_relative_to(expected) and resolved.is_relative_to(expected):
                read_owner_config(actual)
            else:
                if source is None:
                    raise ValueError
                declared = Path(source).resolve(strict=True)
                # The owner's installer stages a versioned tree and repoints a
                # live link at it, so the declared source may BE that link while
                # plugin/read advertises the resolved file. Containment is
                # decided after resolution on both sides; a lexical compare here
                # refused every dispatch whose plugin root was installed that way.
                relative = resolved.relative_to(declared)
                installed = expected / relative
                installed.resolve(strict=True).relative_to(expected)
                # Local plugin/read advertises the registered source, whereas
                # model turns read the installed cache. Verify the same file;
                # the source path never becomes a filesystem grant.
                if read_owner_config(actual) != read_owner_config(installed):
                    raise ValueError
        except OSError, RuntimeError, ValueError, VendorHomeError:
            raise PluginSkillsRefused(_PluginSkillReason.VERSION) from None
        checked += 1
    if not checked:
        raise PluginSkillsRefused(_PluginSkillReason.NO_ENABLED_SKILL)


@dataclass(frozen=True)
class AppServerResult:
    """Native result with usage scoped to this turn, including all model requests."""

    thread_id: str
    turn_id: str
    status: str
    final_text: str | None
    usage: dict[str, int]
    error: str | None = None
    api_error_status: int | None = None
    account_failure: str | None = None
    quota: SourceReading | None = None
    quota_warning: str | None = None
    failure_reason: str | None = None
    structured_output: Any = None

    def __post_init__(self) -> None:
        if self.quota_warning is not None and (
            not isinstance(self.quota_warning, str) or not self.quota_warning
        ):
            raise AppServerError("native quota warning is malformed")
        if self.quota is not None and not isinstance(self.quota, SourceReading):
            raise AppServerError("native quota observation is malformed")
        for name, value in (("thread", self.thread_id), ("turn", self.turn_id)):
            if not isinstance(value, str) or wire.bounded_identifier(value) is None:
                raise AppServerError(f"native result has no valid {name} identity")
        if not wire.TurnCompletionStatus.parse(self.status).is_known():
            raise AppServerError("native result has no terminal status")
        if self.final_text is not None and not isinstance(self.final_text, str):
            raise AppServerError("native result final text is not a string")
        if not isinstance(self.usage, dict) or any(
            not isinstance(key, str)
            or isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            for key, value in self.usage.items()
        ):
            raise AppServerError(
                "native result usage is not nonnegative integer counters"
            )
        if self.error is not None and (
            not isinstance(self.error, str) or self.status == "completed"
        ):
            raise AppServerError(
                "native result carries an invalid or contradictory error"
            )
        if self.api_error_status is not None and (
            type(self.api_error_status) is not int
            or not 0 <= self.api_error_status <= 65535
        ):
            raise AppServerError("native result carries an invalid HTTP status")
        if self.account_failure not in (
            None,
            "credential_rejected",
            "quota_exhausted",
            "rate_limited",
        ):
            raise AppServerError("native result carries an unknown account failure")
        if self.status == "completed" and (
            self.api_error_status is not None or self.account_failure is not None
        ):
            raise AppServerError("native completed result carries failure metadata")
        if (self.failure_reason is None) != (self.status == "completed") or (
            self.failure_reason is not None
            and (not isinstance(self.failure_reason, str) or not self.failure_reason)
        ):
            raise AppServerError("native failure reason contradicts the turn status")


def _account_error(info: Any) -> tuple[int | None, str | None]:
    """Read CodexErrorInfo; free-text messages never establish account failure."""
    if info == "unauthorized":
        return None, "credential_rejected"
    if info == "usageLimitExceeded":
        return None, "quota_exhausted"
    if not isinstance(info, dict) or len(info) != 1:
        return None, None
    variant, details = next(iter(info.items()))
    if variant not in (
        "httpConnectionFailed",
        "responseStreamConnectionFailed",
        "responseStreamDisconnected",
        "responseTooManyFailedAttempts",
    ):
        return None, None
    if not isinstance(details, dict):
        raise AppServerError("malformed native HTTP error details")
    status = details.get("httpStatusCode")
    if status is None:
        return None, None
    if type(status) is not int or not 0 <= status <= 65535:
        raise AppServerError("invalid native HTTP error status")
    failure = (
        "credential_rejected"
        if status in (401, 403)
        else "quota_exhausted"
        if status == 402
        else "rate_limited"
        if status == 429
        else None
    )
    return status, failure


#: What a failure reason may quote of the vendor: identifiers, never prose.
_VENDOR_IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")


def _error_body(message: object) -> tuple[int | None, str | None]:
    """The HTTP status and `error.type` of the API error body codex relays as its
    message text, when the message is one."""
    if not isinstance(message, str):
        return None, None
    try:
        body = json.loads(message)
    except ValueError:
        return None, None
    if not isinstance(body, dict):
        return None, None
    status = body.get("status")
    inner = body.get("error")
    kind = inner.get("type") if isinstance(inner, dict) else None
    return (
        status if type(status) is int and 100 <= status <= 599 else None,
        kind if isinstance(kind, str) and _VENDOR_IDENTIFIER.fullmatch(kind) else None,
    )


def _failure_reason(
    turn_status: str, error: dict[str, Any] | None, api_status: int | None
) -> str:
    """A failed turn's typed reason: the HTTP status and the vendor's error type
    when it sent them, else codex's error variant, else the turn status."""
    info = None if error is None else error.get("codexErrorInfo")
    if isinstance(info, dict) and len(info) == 1:
        info = next(iter(info))
    variant = (
        info if isinstance(info, str) and _VENDOR_IDENTIFIER.fullmatch(info) else None
    )
    status, kind = _error_body(None if error is None else error.get("message"))
    status = api_status if api_status is not None else status
    detail = kind or variant
    if status is not None:
        return f"VendorApiError (HTTP {status}" + (f", {detail})" if detail else ")")
    if detail is not None:
        return f"VendorTurnError ({detail})"
    return f"NativeTurnFailed ({turn_status})"


def _token_totals(params: Any) -> dict[str, int]:
    """Read the cumulative counters carried by a vendor usage notification."""
    container = params.get("tokenUsage") if isinstance(params, dict) else None
    total = container.get("total") if isinstance(container, dict) else None
    if not isinstance(total, dict):
        raise AppServerError("malformed native token usage")
    usage = {}
    for key in (
        "totalTokens",
        "inputTokens",
        "outputTokens",
        "cachedInputTokens",
        "cacheWriteInputTokens",
        "reasoningOutputTokens",
    ):
        # The vendor schema gives cacheWriteInputTokens a zero default.
        value = total.get(key, 0) if key == "cacheWriteInputTokens" else total.get(key)
        if type(value) is not int or value < 0:
            raise AppServerError("invalid native token counter")
        usage[key] = value
    return usage


def permission_profile(
    read_roots: Sequence[str], write_root: str | None = None
) -> dict[str, Any]:
    """The codex permission profile of a caged turn: everything denied but the
    minimal system and `read_roots`, `write_root` alone writable with its `.git`
    read-only, and no network."""
    filesystem = {
        ":root": "deny",
        ":minimal": "read",
        **dict.fromkeys(read_roots, "read"),
    }
    if write_root is not None:
        filesystem[write_root] = "write"
        filesystem[f"{write_root}/.git"] = "read"
    return {"filesystem": filesystem, "network": {"enabled": False}}


#: Every capability path an orchestrator turn closes in its own thread config:
#: the app-server's own disabled list, in the app-server's order, plus the two
#: code-mode features `child.app_server_arguments` deliberately ENABLES for the
#: Claude cage. A turn whose only tool is one private MCP needs no browser, no
#: computer use, no code mode, no sub-agent, no web search and no hook.
ORCHESTRATOR_CLOSED_FEATURES: Final = (
    *wire.DISABLED_CAPABILITY_FEATURES,
    "code_mode",
    "code_mode_host",
)


def tool_environment_config(tool_environment: Mapping[str, str]) -> dict[str, Any]:
    """The thread config that gives a turn's tools `tool_environment`, PATH and
    nothing else: `include_only` filters after `set`, so what the account's own
    `[shell_environment_policy.set]` adds never reaches them."""
    return {
        **{
            f"shell_environment_policy.set.{name}": setting
            for name, setting in tool_environment.items()
        },
        "shell_environment_policy.include_only": ["PATH", *tool_environment],
    }


def orchestrator_thread_params(
    *,
    model: str,
    cwd: str,
    profile: str,
    read_roots: Sequence[str],
    mcp_servers: Mapping[str, Mapping[str, Any]],
    tool_environment: Mapping[str, str],
    developer_instructions: str | None = None,
) -> dict[str, Any]:
    """The whole thread ONE orchestrator turn starts.

    `wire.thread_start_params` is the base — the same closed shape a native
    writer's thread already carries — so approval stays `never`, the network
    stays off, the shell inherits nothing, and the vendor's own orchestrator MCP
    and skills stay disabled. On top of it: the caged reader's root-deny /
    minimal-read permission profile over `read_roots`, exactly the private
    orchestrator MCP beside the inherited names it closes, no plugin at all, and
    every inherited capability path named closed rather than left to a default.
    The `sandbox` the base names is REPLACED by the profile: two answers to "what
    may this turn read" is one answer too many, and the profile is the one the
    vendor enforces.

    An inherited name is carried as `{"enabled": False}` and nothing else. Naming
    it is not a second MCP: the vendor merges this configuration over the file
    its `CODEX_HOME` holds, so omitting a name the account declares leaves that
    declaration standing, and it starts before any catalog check can refuse the
    turn. A name carrying anything beyond its own closure is refused.
    """
    if not read_roots or any(
        not isinstance(root, str) or not Path(root).is_absolute() for root in read_roots
    ):
        raise AppServerError("native orchestrator read roots must be absolute paths")
    if ORCHESTRATOR_MCP_NAME not in mcp_servers or any(
        dict(server) != {"enabled": False}
        for name, server in mcp_servers.items()
        if name != ORCHESTRATOR_MCP_NAME
    ):
        raise AppServerError(
            "a native orchestrator turn lists exactly the private orchestrator "
            "MCP, beside inherited names carrying nothing but their own closure"
        )
    params = wire.thread_start_params(
        model, cwd, wire.ApprovalPolicy.NEVER, wire.ExecutorSandbox.READ_ONLY
    )
    params.pop("sandbox")
    params["permissions"] = profile
    config = params["config"]
    config["permissions"] = {profile: permission_profile(read_roots)}
    # Codex 0.157.1 refuses to reload a config whose profiles name no default.
    config["default_permissions"] = profile
    # The same closure the LAUNCH states with `-c` before this process started
    # (`native.orchestrator_startup_arguments`), from the one mapping that
    # defines it: a thread cannot unload a plugin the process already loaded, nor
    # untrust a project whose configuration layer it already read, so stating it
    # here alone was the gap, and stating it in two places from two lists would
    # let one of them drift.
    config.update(
        {
            key: dict(value) if isinstance(value, dict) else value
            for key, value in ORCHESTRATOR_STARTUP_CLOSURE.items()
        }
    )
    # AFTER that closure, never before it. The LAUNCH already declared this whole
    # table (`native.orchestrator_mcp_arguments`) and the vendor's own effective
    # view of it was validated before this thread was composed
    # (`orchestrator_config_violation`), so what stands here RESTATES that
    # declaration and narrows the private server to its tool contract --
    # `orchestrator_thread_restatement` is what holds it to that. A key of the
    # shared closure landing on top of it would delete the only server the turn
    # has.
    assert "mcp_servers" not in ORCHESTRATOR_STARTUP_CLOSURE
    config["mcp_servers"] = {name: dict(server) for name, server in mcp_servers.items()}
    config.update({f"features.{name}": False for name in ORCHESTRATOR_CLOSED_FEATURES})
    config.update(tool_environment_config(tool_environment))
    # The base already names PATH, and `tool_environment` names it again because
    # the private MCP's own launch needs it: the filter is a set, not a list.
    config["shell_environment_policy.include_only"] = list(
        dict.fromkeys(config["shell_environment_policy.include_only"])
    )
    if developer_instructions is not None:
        params["developerInstructions"] = developer_instructions
    return params


#: The layer type an orchestrator turn must never see among the vendor's own
#: configuration layers. Measured against codex 0.159.2 (M-a, 2026-10-01): an
#: empty `[projects]` or `[mcp_servers]` override does NOT stop that layer loading
#: for a trusted project, and neither does `trust_level = "untrusted"`. What does
#: (M-b) is a NEUTRAL cwd, after which `config/read` answered
#: `[sessionFlags, user, system]` and nothing else. So its absence is ASSERTED
#: here, on the vendor's own answer, rather than assumed from an override.
ORCHESTRATOR_PROJECT_LAYER: Final = "project"
#: What the composed THREAD may carry beyond the launch declaration: the private
#: server's tool contract, which narrows it and adds no launch field.
ORCHESTRATOR_THREAD_MCP_KEYS: Final = (
    *ORCHESTRATOR_LAUNCH_MCP_FIELDS,
    "env_vars",
    "enabled_tools",
)


def orchestrator_config_violation(
    result: object, *, declared: Mapping[str, Mapping[str, Any]]
) -> str | None:
    """What the vendor's OWN effective configuration does that this launch does not
    allow, as one sentence naming the field or the server; `None` is held.

    THE BOUNDARY OF AN ORCHESTRATOR TURN, read from
    `config/read {cwd: <the neutral cwd>, includeLayers: true}` on the one
    app-server this turn started, BEFORE any `thread/start` -- so before the vendor
    has launched one MCP child of this composition. Measured (M-d, M-e): the answer
    is `{config, layers, origins}`; `config.mcp_servers` carries each server's
    command, args and env AS MERGED, and `layers` carries each layer's own type.
    That is the one place the vendor states the launch it will perform.

    `mcpServerStatus/list` CANNOT STAND HERE. Its entries carry no command, no args
    and no env at all (M-c), so a launch check built on it answers "states no
    launch" for every turn, the clean one included, and refuses them all. It stays
    as an additional check on names, tools and resources, after the thread.

    The environment is an EQUALITY, not a containment: the defect was ONE merged
    `[mcp_servers.nott_orchestrator.env]` key reaching the one server the turn
    trusts -- from a project layer, and from an account layer too (M-e) -- with the
    name, the command and the arguments all still right.

    FAIL CLOSED. A layer of type `project`, a shape this reader does not recognise,
    a field missing or of another type: each is a refusal. An answer nobody could
    validate proves nothing. And no refusal names a VALUE of the configuration --
    only the field or the server -- because these values are an environment.
    """
    if not isinstance(result, Mapping):
        return "native orchestrator config/read answered no object"
    layers = result.get("layers")
    if not isinstance(layers, list) or not layers:
        return "native orchestrator config/read states no configuration layers"
    for layer in layers:
        if not isinstance(layer, Mapping):
            return "native orchestrator configuration layer is not an object"
        name = layer.get("name")
        if not isinstance(name, Mapping) or not isinstance(name.get("type"), str):
            return "native orchestrator configuration layer states no type"
        if name["type"] == ORCHESTRATOR_PROJECT_LAYER:
            return "native orchestrator turn loaded a project configuration layer"
    config = result.get("config")
    if not isinstance(config, Mapping):
        return "native orchestrator config/read answered no config"
    servers = config.get("mcp_servers")
    if not isinstance(servers, Mapping):
        return "native orchestrator effective configuration states no mcp_servers"
    composed = declared.get(ORCHESTRATOR_MCP_NAME)
    if composed is None:
        return "native orchestrator launch declared no private MCP to compare"
    private = servers.get(ORCHESTRATOR_MCP_NAME)
    if not isinstance(private, Mapping):
        return "native orchestrator effective configuration states no private MCP"
    if private.get("command") != composed["command"]:
        return "native orchestrator effective MCP command is not the composed one"
    arguments = private.get("args")
    if not isinstance(arguments, list) or arguments != list(composed["args"]):
        return "native orchestrator effective MCP args are not the composed ones"
    environment = private.get("env")
    if not isinstance(environment, Mapping) or dict(environment) != dict(
        composed["env"]
    ):
        return "native orchestrator effective MCP env is not the composed one"
    if private.get("enabled") is not True:
        return "native orchestrator effective MCP is not enabled for the turn"
    for name, server in servers.items():
        if name == ORCHESTRATOR_MCP_NAME:
            continue
        if name not in declared:
            return (
                "native orchestrator effective configuration carries a server the "
                "launch does not declare"
            )
        if not isinstance(server, Mapping) or server.get("enabled") is not False:
            return (
                "native orchestrator effective configuration leaves another server "
                "enabled"
            )
    return None


def orchestrator_thread_restatement(
    servers: Mapping[str, Mapping[str, Any]],
    declared: Mapping[str, Mapping[str, Any]],
) -> str | None:
    """Whether the composed THREAD restates the launch declaration and nothing
    more, as one sentence naming the server, or `None`.

    Every MCP of this turn is declared at launch and the vendor's own view of that
    declaration has just been validated. A thread that added a server, or changed
    one field of the one it has, would be a launch nobody checked -- so it may
    restate the same closure and narrow the private server to its tool contract,
    and it may do nothing else.
    """
    if set(servers) != set(declared):
        return "a native orchestrator thread names a server the launch does not declare"
    for name, server in servers.items():
        if any(key not in ORCHESTRATOR_THREAD_MCP_KEYS for key in server):
            return (
                f"a native orchestrator thread adds a field to the MCP server {name!r}"
            )
        for field_name, value in declared[name].items():
            if server.get(field_name) != value:
                return f"a native orchestrator thread changes the MCP server {name!r}"
    return None


def orchestrator_effective_launch(
    entry: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    """The launch a vendor status entry states for the server it started.

    WHEREVER it states it: at the entry's top level, or one level inside it. A
    `command` is what identifies a launch, so no field name invented here decides
    whether the vendor's answer was understood; an entry that states no command
    anywhere states no launch, and `None` says exactly that.
    """
    candidates = (
        entry,
        *(value for value in entry.values() if isinstance(value, Mapping)),
    )
    for candidate in candidates:
        if isinstance(candidate.get("command"), str):
            return candidate
    return None


def orchestrator_mcp_violation(
    binding: OrchestratorBinding,
    entries: Sequence[Mapping[str, Any]],
    closed: Collection[str],
    *,
    composed: Mapping[str, Any],
) -> str | None:
    """What a live MCP catalog does that this binding's tool contract and this
    turn's composition do not allow, as one sentence; `None` is both held.

    AN ADDITIONAL CHECK, NOT THE BOUNDARY. The boundary is the vendor's own
    effective configuration, validated before any thread exists
    (`orchestrator_config_violation`), because an `mcpServerStatus/list` entry
    carries no command, no args and no env at all: measured against codex 0.159.2
    (M-c, 2026-10-01), its whole key set is `name`, `runtimeStatus`, `pluginId`,
    `httpOrigin`, `serverInfo`, `serverCapabilities`, `toolsError`, `resources`,
    `resourceTemplates`, `authStatus` and `tools`. A check that read the launch out
    of this list therefore answered "states no launch" for EVERY orchestrator turn,
    the clean one included, and refused them all. So the launch is compared here
    only WHERE the vendor states one, and an entry that states none is not a
    refusal; what this list answers for is the names, the tools and the resources.

    The observed tool set must EQUAL the contract, not merely stay inside it: a
    turn admitted on a contract naming `nott_proposal_create` and then started
    against a server offering four tools, or none, has no proposal to create, and
    admitting it would record an orchestration the turn could not perform.

    A name the binding closed MAY be listed -- the vendor lists a disabled server
    with no capability of its own, as `_verify_readonly_mcp` already reads on the
    writer's path -- but one that came up with a tool or a resource anyway did
    start, and that ends the turn. Each failure is said apart, because a turn that
    silently lost its one tool reads exactly like a turn that gained another one,
    and a turn whose one server was started by somebody else's configuration
    reads, by name and by tool, exactly like a turn that was not.
    """
    contract = set(binding.tool_contract()["tools"])
    present = False
    for entry in entries:
        if entry.get("name") != ORCHESTRATOR_MCP_NAME:
            if entry.get("name") not in closed:
                return (
                    "native orchestrator MCP catalog carries a server the binding "
                    "does not name"
                )
            if (
                entry.get("tools")
                or entry.get("resources")
                or entry.get("resourceTemplates")
            ):
                return (
                    "native orchestrator turn reached a server the binding named closed"
                )
            continue
        present = True
        observed = set(entry.get("tools") or ())
        if observed - contract:
            return "native orchestrator MCP exposes a tool outside its contract"
        if contract - observed:
            return "native orchestrator MCP is missing a tool its contract requires"
        if entry.get("resources") or entry.get("resourceTemplates"):
            return "native orchestrator MCP exposes a resource outside its contract"
        effective = orchestrator_effective_launch(entry)
        if effective is None:
            # The real vendor states none (M-c). The launch was already validated
            # against the vendor's own effective configuration, before this thread.
            continue
        if effective.get("command") != composed.get("command") or list(
            effective.get("args") or ()
        ) != list(composed.get("args") or ()):
            return (
                "native orchestrator MCP was started with a command the "
                "composition does not name"
            )
        if dict(effective.get("env") or {}) != dict(composed.get("env") or {}):
            return (
                "native orchestrator MCP was started with an environment the "
                "composition does not name"
            )
    if not present:
        return "native orchestrator MCP is not available to the turn"
    return None


def _threads(items: Sequence[object]) -> list[dict[str, Any]]:
    """The items that are threads: objects with a nonempty string id."""
    return [
        item
        for item in items
        if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"]
    ]


class AppServerTurn:
    """A single stdin writer and one observing stdout pump, never rival readers."""

    #: How long `close` waits for the server to exit on EOF, and again after a stop.
    EXIT_GRACE_S = 3.0

    def __init__(
        self,
        argv: Sequence[str],
        cwd: Path,
        env: dict[str, str],
        run_dir: Path,
        needles: SecretNeedles,
        scope: ExecutorScope | None = None,
    ) -> None:
        self._needles = needles
        self._cwd = str(cwd)
        self._frames: queue.Queue[tuple[wire.RpcFrame, int]] = queue.Queue(
            MAX_PENDING_FRAMES
        )
        self._pending: deque[wire.RpcFrame] = deque()
        self._pending_bytes = 0
        self._next_id = 0
        self._private_responses: set[int] = set()
        self._readonly_active_servers: set[str] | None = None
        self._tool_free = False
        self._deadline: float | None = None
        self._used = False
        self._finished = False
        self._scope = scope
        self._closed = False
        self._consumer_gone = False
        self._reported_pump_error: NSpawnError | OSError | None = None
        self.terminated_by_driver = False
        self.child = child.spawn_child(
            argv,
            cwd,
            env,
            run_dir,
            needles,
            stdin_pipe=True,
            stdout_transform=self._observe,
            scope=scope,
        )

    @classmethod
    def start(
        cls,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: dict[str, str],
        run_dir: Path,
        needles: SecretNeedles,
        scope: ExecutorScope | None = None,
    ) -> Self:
        return cls(argv, cwd, env, run_dir, needles, scope)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            self.close()
        except (NSpawnError, OSError, subprocess.TimeoutExpired) as error:
            if exc is None:
                raise
            exc.add_note(f"native cleanup also failed: {type(error).__name__}")

    def _observe(self, line: bytes) -> bytes:
        """Consume raw protocol in memory, redact decoded values before persistence."""
        try:
            raw = json.loads(line)
            frame = wire.RpcFrame.from_wire(raw)
            if frame.method is not None and (
                not isinstance(frame.method, str)
                or not frame.method
                or len(frame.method.encode()) > wire.MAX_METHOD_BYTES
            ):
                raise AppServerError("invalid native method")
            if frame.id is not None and (
                isinstance(frame.id, bool) or not isinstance(frame.id, (str, int))
            ):
                raise AppServerError("invalid native request id")
            if frame.error is not None and frame.has_result:
                raise AppServerError("native response carries both result and error")
            if (
                self._readonly_active_servers is not None
                and frame.method == "mcpServer/startupStatus/updated"
                and isinstance(frame.params, dict)
                and frame.params.get("name") not in self._readonly_active_servers
            ):
                raise AppServerError(
                    "native read-only started an unadmitted MCP server"
                )
            persisted = (
                b""
                if frame.method is None and frame.id in self._private_responses
                else (
                    json.dumps(
                        self._needles.redact_value(raw),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode()
                    + b"\n"
                )
            )
        except (ValueError, UnicodeError, RecursionError, wire.WireError) as error:
            raise AppServerError("malformed native JSON-RPC frame") from error
        # Backpressure, not refusal: a burst outpaces the consumer only because the
        # GIL schedules it less often, so the reader waits for room. Once `close`
        # declares the consumer gone the frame is dropped; its line still persists.
        while not self._consumer_gone:
            try:
                self._frames.put((frame, len(line)), timeout=0.05)
                break
            except queue.Full:
                continue
        return persisted

    def _remaining(self, deadline: float | None) -> float:
        if deadline is None:
            return RPC_TIMEOUT_SECONDS
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AppServerDeadlineExceeded("native turn deadline exceeded")
        return remaining

    def _send(
        self, frame: wire.RpcFrame, deadline: float | None, *, null_params: bool = False
    ) -> None:
        body = frame.to_wire()
        if null_params:
            body["params"] = None
        payload = json.dumps(body, ensure_ascii=False).encode() + b"\n"
        if len(payload) > MAX_REQUEST_BYTES:
            raise AppServerError("native request exceeds its byte limit")
        self.child.write_stdin(payload, self._remaining(deadline))

    def _receive(self, deadline: float | None) -> tuple[wire.RpcFrame, int]:
        """The next frame and the raw byte length of the line it came from."""
        while True:
            remaining = self._remaining(deadline)
            try:
                self.child.stdout_pump.join(timeout=0)
                self.child.stderr_pump.join(timeout=0)
            except (NSpawnError, OSError) as error:
                self._reported_pump_error = error
                raise
            try:
                return self._frames.get(timeout=min(0.05, remaining))
            except queue.Empty:
                if self.child.stdout_pump.finished:
                    try:
                        self.child.stdout_pump.join(timeout=0)
                    except (NSpawnError, OSError) as error:
                        self._reported_pump_error = error
                        raise
                    raise AppServerError(
                        "native stdout closed before matching completion"
                    )

    def _deny_request(self, frame: wire.RpcFrame, deadline: float | None) -> bool:
        if frame.id is None or frame.method is None:
            return False
        self._send(
            wire.error_response(
                frame.id,
                wire.RpcErrorObject(
                    -32601, f"unsupported server request: {frame.method}"
                ),
            ),
            deadline,
        )
        return True

    def _call(
        self,
        method: str,
        params: dict[str, Any] | None,
        *,
        private: bool = False,
        on_run_deadline: bool = False,
    ) -> Any:
        """One request and its answer. `on_run_deadline` waits as long as the
        run's own deadline allows (thread and turn open, slow at max effort);
        otherwise the fixed cap also bounds it, as a startup timeout."""
        try:
            return self._request(method, params, private, on_run_deadline)
        except AppServerDeadlineExceeded:
            if self._deadline is not None and time.monotonic() >= self._deadline:
                # Name the request: a preparation timeout reports its step.
                raise AppServerDeadlineExceeded(
                    f"native {method} got no answer before the deadline"
                ) from None
            raise AppServerStartupTimeout(
                f"native {method} got no answer within {RPC_TIMEOUT_SECONDS:g}s"
            ) from None

    def _request(
        self,
        method: str,
        params: dict[str, Any] | None,
        private: bool,
        on_run_deadline: bool,
    ) -> Any:
        self._next_id += 1
        request_id = self._next_id
        if private:
            self._private_responses.add(request_id)
        deadline: float | None
        if on_run_deadline:
            # A run without a deadline waits until the child answers or exits.
            deadline = self._deadline
        else:
            deadline = time.monotonic() + RPC_TIMEOUT_SECONDS
            if self._deadline is not None:
                deadline = min(deadline, self._deadline)
        self._send(
            wire.request(request_id, method, params),
            deadline,
            null_params=params is None,
        )
        while True:
            frame, size = self._receive(deadline)
            if self._deny_request(frame, deadline):
                if self._tool_free:
                    raise AppServerError("native inference requested client authority")
                continue
            if frame.method is not None:
                # A byte cap and no backpressure: the stash spans every handshake
                # call, and the awaited response is queued behind these frames, so
                # waiting would deadlock. The run budget cannot bound it, because
                # it charges redacted lines; the raw bytes held here are capped.
                if self._pending_bytes + size > MAX_PENDING_BYTES:
                    raise PumpLimitExceeded(
                        "native notifications before response exceed the "
                        f"{MAX_PENDING_BYTES} byte stash ceiling: "
                        f"{self._pending_bytes + size} bytes"
                    )
                self._pending_bytes += size
                self._pending.append(frame)
                continue
            if frame.id != request_id:
                raise AppServerError("native response id does not match the request")
            if frame.error is not None:
                # The detail becomes the stored failure reason: only an integer
                # code is ours to format, never a vendor's text in its place.
                code = frame.error.code
                raise AppServerRefused(
                    f"native {method} request refused "
                    f"({code if type(code) is int else 'non-integer code'})"
                )
            if not frame.has_result:
                raise AppServerError("native response has neither result nor error")
            return frame.result

    def read_account_quota(
        self, home: Path, identity: str | None, *, timeout: float
    ) -> SourceReading:
        """Read account metadata without starting a thread or model turn."""
        if self._used or self._closed:
            raise AppServerError("app-server child already used")
        if not math.isfinite(timeout) or timeout <= 0:
            raise AppServerError("native timeout must be positive and finite")
        self._used = True
        self._deadline = time.monotonic() + timeout
        initialized = self._call("initialize", wire.initialize_params(), private=True)
        if not isinstance(initialized, dict) or initialized.get("codexHome") != str(
            home
        ):
            raise AppServerError("native quota home identity mismatch")
        self._send(wire.initialized_notification(), self._deadline)
        response = self._call("account/rateLimits/read", None, private=True)
        if not isinstance(response, dict):
            raise AppServerError("native quota response is not an object")
        response_identity = response.get("accountId")
        if response_identity is not None and response_identity != identity:
            raise AppServerError("native quota account identity mismatch")
        reading = parse_codex_quota_response(response, int(time.time()))
        self._finished = True
        return reading

    def read_threads(
        self,
        home: Path,
        *,
        limit: int,
        ids: Sequence[str],
        known: Callable[[list[str]], set[str]],
        timeout: float,
    ) -> list[dict[str, Any]]:
        """The home's threads that `known` does not name, newest first, or the
        threads `ids` names that the home holds. It starts no thread and no turn."""
        if self._used or self._closed:
            raise AppServerError("app-server child already used")
        self._used = True
        self._deadline = time.monotonic() + timeout
        initialized = self._call("initialize", wire.initialize_params(), private=True)
        if not isinstance(initialized, dict) or initialized.get("codexHome") != str(
            home
        ):
            raise AppServerError("native thread home identity mismatch")
        self._send(wire.initialized_notification(), self._deadline)
        found: list[dict[str, Any]] = []
        for ident in ids:
            try:
                read = self._call(
                    "thread/read",
                    {"threadId": ident, "includeTurns": False},
                    private=True,
                )
            except AppServerRefused:
                continue  # this home does not hold that thread
            found.extend(
                _threads([read.get("thread") if isinstance(read, dict) else None])
            )
        cursor: str | None = None
        for _ in range(THREAD_PAGES if not ids else 0):
            page = self._call(
                "thread/list",
                {
                    "limit": THREAD_PAGE_SIZE,
                    "cursor": cursor,
                    "sortKey": "updated_at",
                    "useStateDbOnly": True,
                    "archived": False,
                    "sourceKinds": list(THREAD_SOURCES),
                },
                private=True,
            )
            if not isinstance(page, dict) or not isinstance(page.get("data"), list):
                raise AppServerError("native thread list is not a page")
            threads = _threads(page["data"])
            mine = known([thread["id"] for thread in threads])
            found.extend(thread for thread in threads if thread["id"] not in mine)
            cursor = page.get("nextCursor")
            if len(found) >= limit or not isinstance(cursor, str):
                break
        self._finished = True
        return found[:limit] if not ids else found

    def run(
        self,
        prompt: str,
        *,
        model: str,
        read_only: bool,
        timeout: float | None = None,
        thread_id: str | None = None,
        developer_instructions: str | None = None,
        model_provider: str | None = None,
        effort: str | None = None,
        mcp_servers: dict[str, dict[str, Any]] | None = None,
        readonly_mcp: bool = False,
        read_roots: tuple[str, ...] | None = None,
        plugin_mcp_policies: dict[str, dict[str, Any]] | None = None,
        on_thread: Callable[[str], None] | None = None,
        native_write: bool = False,
        tool_environment: Mapping[str, str] | None = None,
        orchestrator: OrchestratorBinding | None = None,
        orchestrator_profile: str | None = None,
        inference_profile: StructuredInference | None = None,
        inference: CodexInference | None = None,
    ) -> AppServerResult:
        if self._used or self._closed:
            raise AppServerError("one app-server child owns exactly one turn")
        if timeout is not None and (not math.isfinite(timeout) or timeout <= 0):
            raise AppServerError("native timeout must be positive and finite")
        if not prompt or not model:
            raise AppServerError("native prompt and model must be nonempty")
        if inference_profile is not None and (
            not read_only
            or native_write
            or orchestrator is not None
            or mcp_servers
            or read_roots
            or tool_environment
            or inference is None
            or str(inference.cwd) != self._cwd
            or inference.model != model
            or developer_instructions is not None
        ):
            raise AppServerError(
                "structured inference grants no additional tools or paths"
            )
        if not read_only and not native_write:
            raise AppServerError("a writable turn needs native write")
        if native_write and (
            read_only
            or self._scope is None
            or not readonly_mcp
            or not read_roots
            or tool_environment is None
            or os.path.realpath(self._cwd) != self._cwd
        ):
            raise AppServerError(
                "native write needs its own aggregate custody, a closed MCP "
                "catalog, read roots, a tool environment and a canonical cwd"
            )
        if orchestrator is not None and not (
            read_only
            and not native_write
            and readonly_mcp
            and read_roots
            and tool_environment is not None
            and orchestrator_profile is not None
            # The vendor's MCP children run in their own process group and
            # session, so a process-group kill of this child does not reach them:
            # the custody that holds every descendant of an orchestrator turn is
            # the run's own aggregate scope, as it is for a native writer.
            and self._scope is not None
        ):
            raise AppServerError(
                "a native orchestrator turn is read-only and carries its own "
                "profile, read roots, closed MCP catalog, tool environment and "
                "aggregate custody"
            )
        if inference_profile is not None:
            self._readonly_active_servers = set()
        elif orchestrator is not None:
            # The one server that may ever start: not nmemory, and not a name a
            # catalog of this account happens to hold.
            self._readonly_active_servers = {ORCHESTRATOR_MCP_NAME}
        elif readonly_mcp:
            self._readonly_active_servers = {
                name
                for name, server in (mcp_servers or {}).items()
                if name == "nmemory" and server.get("enabled", True)
            }
        self._used = True
        self._tool_free = inference_profile is not None
        self._deadline = None if timeout is None else time.monotonic() + timeout
        self._call("initialize", wire.initialize_params())
        self._send(wire.initialized_notification(), self._deadline)
        profile_id = None
        if inference_profile is not None:
            assert inference is not None
            self._readonly_active_servers = set()
            effective = self._call(
                "config/read",
                {"cwd": self._cwd, "includeLayers": True},
                private=True,
            )
            refusal = inference.config_refusal(effective)
            if refusal is not None:
                raise AppServerError(refusal)
            params: dict[str, Any] = {
                "model": model,
                "cwd": self._cwd,
                "approvalPolicy": "never",
                "sandbox": "read-only",
                "config": inference.config(),
                "developerInstructions": "",
                "dynamicTools": [],
                "selectedCapabilityRoots": [],
            }
        elif orchestrator is not None:
            assert read_roots is not None and tool_environment is not None
            assert orchestrator_profile is not None
            # BEFORE THE THREAD, AND BEFORE ANY MCP CHILD. The launch declared this
            # turn's whole MCP table with `-c`, and `config/read` on the SAME
            # neutral cwd is where the vendor states the launch it will perform
            # (M-e: it answered the private server's command, args and merged env
            # with no child started yet). So the vendor's own effective view is
            # validated here -- no project layer, the private server's command,
            # args and environment EXACTLY as composed, no other server enabled --
            # and only then may a thread start.
            try:
                launched = orchestrator_launch_mcp_servers(mcp_servers or {})
            except NativePreparationError as refusal:
                raise AppServerError(refusal.message) from None
            try:
                # Privately: a merged `[mcp_servers]` carries every environment
                # value of the private MCP, and none of it belongs in a transcript.
                effective = self._call(
                    "config/read",
                    {"cwd": self._cwd, "includeLayers": True},
                    private=True,
                )
            except AppServerDeadlineExceeded:
                raise
            except AppServerError as refused:
                # FAIL CLOSED: an answer nobody could read proves nothing.
                raise AppServerError(
                    "native orchestrator effective configuration could not be read"
                ) from refused
            violation = orchestrator_config_violation(effective, declared=launched)
            if violation is not None:
                raise AppServerError(violation)
            # One composer owns this turn's whole thread: nothing below may add a
            # plugin, a second MCP or an inherited capability back into it.
            params = orchestrator_thread_params(
                model=model,
                cwd=self._cwd,
                profile=orchestrator_profile,
                read_roots=read_roots,
                mcp_servers=mcp_servers or {},
                tool_environment=tool_environment,
                developer_instructions=developer_instructions,
            )
            # And it restates the validated declaration: a thread free to add a
            # server, or to change one field of the one it has, would be a launch
            # the check above never saw.
            restated = orchestrator_thread_restatement(
                params["config"]["mcp_servers"], launched
            )
            if restated is not None:
                raise AppServerError(restated)
            profile_id = orchestrator_profile
        else:
            params = {
                "model": model,
                "cwd": self._cwd,
                "approvalPolicy": "never",
                "sandbox": "read-only",
                "config": {},
            }
            if native_write:
                params = wire.thread_start_params(
                    model,
                    self._cwd,
                    wire.ApprovalPolicy.NEVER,
                    wire.ExecutorSandbox.READ_ONLY,
                )
            if read_roots is not None:
                if (
                    not (read_only or native_write)
                    or not read_roots
                    or any(
                        not isinstance(root, str) or not Path(root).is_absolute()
                        for root in read_roots
                    )
                ):
                    raise AppServerError(
                        "native read roots require readonly absolute paths"
                    )
                profile_id = (
                    "nott-nspawn-write-" if native_write else "nott-nspawn-read-"
                ) + uuid.uuid4().hex
                params.pop("sandbox")
                params["permissions"] = profile_id
                params["config"]["permissions"] = {
                    profile_id: permission_profile(
                        read_roots, self._cwd if native_write else None
                    )
                }
                # Codex 0.157.1 refuses to reload, at the first
                # mcpServerStatus/list, a config whose profiles name no default
                # (observed 2026-09-28).
                params["config"]["default_permissions"] = profile_id
            if tool_environment is not None:
                params["config"].update(tool_environment_config(tool_environment))
            if developer_instructions is not None:
                params["developerInstructions"] = developer_instructions
            if mcp_servers:
                params["config"]["mcp_servers"] = mcp_servers
            if readonly_mcp:
                params["config"]["plugins"] = plugin_mcp_policies or {}
                params["config"]["features.apps"] = False
        if model_provider is not None:
            params["modelProvider"] = model_provider
        if thread_id is not None:
            params["threadId"] = thread_id
        started = self._call(
            "thread/resume" if thread_id is not None else "thread/start",
            params,
            on_run_deadline=True,
        )
        native_thread = wire.thread_id(started)
        if native_thread is None or (
            thread_id is not None and native_thread != thread_id
        ):
            raise AppServerError(
                "native thread identity is missing or changed on resume"
            )
        if not isinstance(started, dict) or started.get("model") != model:
            raise AppServerError("native server changed the requested model")
        if profile_id is not None:
            active = started.get("activePermissionProfile")
            if (
                not isinstance(active, dict)
                or active.get("id") != profile_id
                or active.get("extends") is not None
            ):
                raise AppServerError("native server changed the requested read profile")
        reported_sandbox = started.get("sandbox")
        expected_sandbox = "readOnly" if read_only else "workspaceWrite"
        if (
            not isinstance(reported_sandbox, dict)
            or reported_sandbox.get("type") != expected_sandbox
            or native_write
            and reported_sandbox != NATIVE_WRITE_SANDBOX
        ):
            raise AppServerError("native server did not preserve the requested sandbox")
        if (
            model_provider is not None
            and started.get("modelProvider") != model_provider
        ):
            raise AppServerError("native server changed the requested model provider")
        history_path = None
        if inference_profile is not None:
            assert isinstance(started, dict)
            assert inference is not None
            if (
                started.get("instructionSources") != []
                or started.get("cwd") != self._cwd
                or reported_sandbox.get("networkAccess") is not False
            ):
                raise AppServerError(
                    "native inference has ambient instructions or extra grants"
                )
            history_path = started["thread"].get("path")
            for entry in self._mcp_status(native_thread):
                if (
                    entry.get("tools")
                    or entry.get("resources")
                    or entry.get("resourceTemplates")
                    or entry.get("name") not in inference.closed_servers
                ):
                    raise AppServerError(
                        "native inference has an active MCP capability"
                    )
        if on_thread is not None:
            on_thread(native_thread)
        if orchestrator is not None:
            # Before `turn/start`, against the live list of THIS thread, and
            # ADDITIONAL to the effective-configuration boundary above: the names,
            # the tools and the resources that actually came up. A server the
            # binding never named, or one it named closed that came up with a tool
            # anyway, ends the turn instead of prompting it.
            composed = (mcp_servers or {}).get(ORCHESTRATOR_MCP_NAME)
            if composed is None:
                raise AppServerError(
                    "a native orchestrator turn composes the private MCP itself"
                )
            violation = orchestrator_mcp_violation(
                orchestrator,
                list(self._mcp_status(native_thread)),
                {name for name in (mcp_servers or {}) if name != ORCHESTRATOR_MCP_NAME},
                composed=composed,
            )
            if violation is not None:
                raise AppServerError(violation)
        elif readonly_mcp:
            expected = dict(mcp_servers or {})
            for policy in (plugin_mcp_policies or {}).values():
                for name in policy["mcp_servers"]:
                    expected.setdefault(name, {"enabled": False})
            self._verify_readonly_mcp(native_thread, expected)
        turn_params = wire.turn_start_params(native_thread, prompt, self._cwd, model)
        if effort is not None:
            turn_params["effort"] = effort
        if inference_profile is not None:
            turn_params["outputSchema"] = inference_profile.data()["output_schema"]
            turn_params["collaborationMode"] = {
                "mode": "default",
                "settings": {
                    "model": model,
                    "reasoning_effort": effort,
                    "developer_instructions": None,
                },
            }
        native_turn = wire.turn_id(
            self._call("turn/start", turn_params, on_run_deadline=True)
        )
        if native_turn is None:
            raise AppServerError("native turn response carries no id")
        texts: dict[str, str] = {}
        usage: dict[str, int] = {}
        baseline: dict[str, int] = {}
        began_turn = False
        quota = None
        quota_warning = None
        while True:
            if self._pending:
                frame = self._pending.popleft()
                if not self._pending:
                    self._pending_bytes = 0
            else:
                frame, _ = self._receive(self._deadline)
            if self._deny_request(frame, self._deadline):
                if inference_profile is not None:
                    raise AppServerError("native inference requested client authority")
                continue
            if frame.method is None:
                raise AppServerError("unexpected native response after turn start")
            if (
                frame.method == "account/rateLimits/updated"
                and model_provider == "openai"
            ):
                reading = parse_codex_rate_limits(
                    self._needles.redact_value(frame.params), int(time.time())
                )
                quota_warning = reading.absence
                if (
                    reading.absence is None
                    or quota is None
                    or quota.absence is not None
                ):
                    quota = reading
                continue
            event = wire.parse_notification(frame.method, frame.params)
            if event.thread_id != native_thread:
                continue
            if event.turn_id != native_turn:
                # A cold resume reports any restored ledger immediately after its
                # response, before the new turn. Absence means this process starts
                # its counters at zero; another process's snapshot is not its base.
                if (
                    thread_id is not None
                    and not began_turn
                    and event.kind is wire.NotificationKind.TOKEN_USAGE
                ):
                    baseline = _token_totals(frame.params)
                continue
            began_turn = True
            if event.kind is wire.NotificationKind.ITEM_COMPLETED:
                item = event.item
                if isinstance(item, dict) and item.get("type") == "agentMessage":
                    raw_id = item.get("id")
                    key = (
                        wire.bounded_identifier(raw_id)
                        if isinstance(raw_id, str)
                        else None
                    )
                    value = item.get("text")
                    if key is None or not isinstance(value, str):
                        raise AppServerError("malformed native agent message")
                    if item.get("phase") != "commentary":
                        texts[key] = self._needles.redact(value)
            elif event.kind is wire.NotificationKind.TOKEN_USAGE:
                total = _token_totals(frame.params)
                measured = {
                    key: value - baseline.get(key, 0) for key, value in total.items()
                }
                if any(value < usage.get(key, 0) for key, value in measured.items()):
                    raise AppServerError(
                        "native cumulative usage moved backwards within a turn"
                    )
                usage = measured
            elif event.kind is wire.NotificationKind.TURN_COMPLETED:
                if event.status is None or not event.status.is_known():
                    raise AppServerError(
                        "native completion status is missing or unknown"
                    )
                turn = (
                    frame.params.get("turn") if isinstance(frame.params, dict) else None
                )
                if not isinstance(turn, dict):
                    raise AppServerError("malformed native completed turn")
                error = turn.get("error")
                if error is not None and not isinstance(error, dict):
                    raise AppServerError("malformed native terminal error")
                if error is not None and event.status.as_str() == "completed":
                    raise AppServerError("native completed turn carries an error")
                message = error.get("message") if isinstance(error, dict) else None
                if message is not None and not isinstance(message, str):
                    raise AppServerError("malformed native terminal error")
                api_status, account_failure = _account_error(
                    error.get("codexErrorInfo") if isinstance(error, dict) else None
                )
                completed = event.status.as_str() == "completed"
                final_text = "\n".join(texts.values()) if texts else None
                structured_output = None
                if completed and inference_profile is not None:
                    if final_text is None:
                        raise AppServerError("native structured output is missing")
                    assert inference is not None
                    # The native history can contain a function call omitted by
                    # normalized client events, even after its native refusal.
                    # Read it complete only after the exact child has quiesced.
                    self._finished = True
                    self.close()
                    try:
                        inference.verify_history(
                            history_path, native_thread, native_turn, effort, final_text
                        )
                        structured_output = inference_profile.parse_output(final_text)
                    except (ValueError, TypeError) as refusal:
                        raise AppServerError(str(refusal)) from None
                result = AppServerResult(
                    native_thread,
                    native_turn,
                    event.status.as_str(),
                    final_text,
                    usage,
                    self._needles.redact(message) if message is not None else None,
                    api_error_status=api_status,
                    account_failure=account_failure,
                    quota=quota,
                    quota_warning=quota_warning,
                    failure_reason=None
                    if completed
                    else _failure_reason(event.status.as_str(), error, api_status),
                    structured_output=structured_output,
                )
                self._finished = True
                self.close()
                return result

    def readonly_mcp_policies(
        self,
        *,
        timeout: float,
        expected_skill_roots: dict[str, tuple[str, str]] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Read installed plugin MCP bindings without starting a model or thread."""
        if self._used or self._closed:
            raise AppServerError("one app-server child owns exactly one operation")
        if not math.isfinite(timeout) or timeout <= 0:
            raise AppServerDeadlineExceeded("native MCP binding deadline exceeded")
        self._used = True
        self._deadline = time.monotonic() + timeout
        self._call("initialize", wire.initialize_params(), on_run_deadline=True)
        self._send(wire.initialized_notification(), self._deadline)
        catalog = self._call(
            "plugin/installed",
            {"cwds": [self._cwd]},
            private=True,
            on_run_deadline=True,
        )
        if not isinstance(catalog, dict) or catalog.get("marketplaceLoadErrors"):
            raise AppServerError(
                "native read-only plugin catalog could not be resolved"
            )
        marketplaces = catalog.get("marketplaces")
        if not isinstance(marketplaces, list) or len(marketplaces) > 64:
            raise AppServerError("native read-only plugin marketplaces are invalid")
        policies: dict[str, dict[str, Any]] = {}
        for marketplace in marketplaces:
            if not isinstance(marketplace, dict) or not isinstance(
                marketplace.get("plugins"), list
            ):
                raise AppServerError("native read-only plugin catalog is malformed")
            for plugin in marketplace["plugins"]:
                if not isinstance(plugin, dict):
                    raise AppServerError("native read-only plugin entry is malformed")
                if plugin.get("enabled") is False:
                    continue
                identity = plugin.get("id")
                name = plugin.get("remotePluginId") or plugin.get("name")
                if (
                    not isinstance(identity, str)
                    or not identity
                    or not identity.isprintable()
                    or len(identity.encode()) > 512
                    or not isinstance(name, str)
                    or not name
                    or not name.isprintable()
                    or len(name.encode()) > 512
                    or len(policies) >= 64
                    or identity in policies
                ):
                    raise AppServerError("native read-only plugin identity is invalid")
                detail = self._call(
                    "plugin/read",
                    {
                        "pluginName": name,
                        "marketplacePath": marketplace.get("path"),
                        "remoteMarketplaceName": marketplace.get("name")
                        if marketplace.get("path") is None
                        else None,
                    },
                    private=True,
                    on_run_deadline=True,
                )
                body = detail.get("plugin") if isinstance(detail, dict) else None
                if not isinstance(body, dict):
                    raise AppServerError("native read-only plugin detail is malformed")
                summary = body.get("summary")
                if not isinstance(summary, dict) or summary.get("id") != identity:
                    raise AppServerError("native read-only plugin identity changed")
                if identity in (expected_skill_roots or {}):
                    assert expected_skill_roots is not None
                    _verify_plugin_skills(body, *expected_skill_roots[identity])
                names = body.get("mcpServers")
                if (
                    not isinstance(names, list)
                    or len(names) > 64
                    or any(
                        not isinstance(value, str)
                        or not value
                        or not value.isprintable()
                        or len(value.encode()) > 512
                        for value in names
                    )
                    or len(set(names)) != len(names)
                ):
                    raise AppServerError(
                        "native read-only plugin MCP names are invalid"
                    )
                policies[identity] = {
                    "mcp_servers": {value: {"enabled": False} for value in names}
                }
        if (expected_skill_roots or {}).keys() - policies.keys():
            raise PluginSkillsRefused(_PluginSkillReason.NOT_SELECTED)
        self._finished = True
        return policies

    def _mcp_status(self, thread_id: str) -> Iterator[dict[str, Any]]:
        # Live pagination has repeated names across pages. Discard that pass;
        # require two complete identity snapshots before exposing any rows.
        previous: tuple[tuple[str, str | None], ...] | None = None
        for _ in range(4):
            cursor = None
            seen: set[str] = set()
            entries: list[dict[str, Any]] = []
            moved = False
            for _page in range(64):
                result = self._call(
                    "mcpServerStatus/list",
                    {
                        "threadId": thread_id,
                        "limit": 1,
                        "cursor": cursor,
                    },
                )
                if not isinstance(result, dict) or not isinstance(
                    result.get("data"), list
                ):
                    raise AppServerError("native read-only MCP status is malformed")
                page_names: set[str] = set()
                for entry in result["data"]:
                    if not isinstance(entry, dict):
                        raise AppServerError(
                            "native read-only MCP status entry is malformed"
                        )
                    name, plugin = entry.get("name"), entry.get("pluginId")
                    if (
                        not isinstance(name, str)
                        or not name
                        or not name.isprintable()
                        or len(name.encode()) > 512
                        or name in page_names
                        or (
                            plugin is not None
                            and (
                                not isinstance(plugin, str) or not plugin.isprintable()
                            )
                        )
                        or not isinstance(entry.get("tools"), dict)
                        or not isinstance(entry.get("resources"), list)
                        or not isinstance(entry.get("resourceTemplates"), list)
                    ):
                        raise AppServerError(
                            "native read-only MCP status identity is malformed"
                        )
                    if name in seen:
                        moved = True
                        break
                    if len(seen) >= 64:
                        raise AppServerError(
                            "native read-only MCP catalog exceeds its server limit"
                        )
                    page_names.add(name)
                    seen.add(name)
                    entries.append(entry)
                if moved:
                    break
                cursor = result.get("nextCursor")
                if cursor is None:
                    break
                if not isinstance(cursor, str) or not cursor:
                    raise AppServerError("native read-only MCP cursor is malformed")
            else:
                raise AppServerError(
                    "native read-only MCP catalog exceeds its page limit"
                )
            if moved:
                previous = None
                continue
            identity = tuple(
                (entry["name"], entry.get("pluginId")) for entry in entries
            )
            if identity == previous:
                yield from entries
                return
            previous = identity
        raise AppServerError("native read-only MCP catalog did not stabilize")

    def _verify_readonly_mcp(
        self, thread_id: str, servers: dict[str, dict[str, Any]]
    ) -> None:
        memory = servers.get("nmemory", {})
        required_context = (
            memory.get("enabled", True)
            and "nmemory_context_show" in memory.get("enabled_tools", [])
            and "nmemory_context_show" not in memory.get("disabled_tools", [])
        )
        observed_context = False
        for entry in self._mcp_status(thread_id):
            name = entry["name"]
            if name not in servers:
                raise AppServerError(
                    "native read-only MCP catalog changed before the turn"
                )
            allowed = (
                {"nmemory_context_show"}
                if name == "nmemory" and required_context
                else set()
            )
            if (
                set(entry["tools"]) - allowed
                or entry["resources"]
                or entry["resourceTemplates"]
            ):
                raise AppServerError(
                    "native read-only MCP exposes an unadmitted capability"
                )
            observed_context |= (
                name == "nmemory" and "nmemory_context_show" in entry["tools"]
            )
        if required_context and not observed_context:
            raise AppServerError("native read-only nmemory_context_show is unavailable")

    def _stop(self) -> None:
        try:
            self.child.stop_then_kill(self.child.identity)
        except child.ChildIdentityError:
            if self.child.poll() is None:
                raise
        else:
            self.terminated_by_driver = True

    def close(self) -> None:
        if self._closed:
            return
        # Nobody reads frames past this point: release a reader held for room.
        self._consumer_gone = True
        try:
            if not self._finished and self.child.poll() is None:
                self._stop()
            self.child.close_stdin()
            try:
                exit_code = self.child.wait(timeout=self.EXIT_GRACE_S)
            except subprocess.TimeoutExpired:
                self._stop()
                exit_code = self.child.wait(timeout=self.EXIT_GRACE_S)
            except (NSpawnError, OSError) as error:
                if error is not self._reported_pump_error:
                    raise
                return
            if (
                not self.child.stdout_pump.finished
                or not self.child.stderr_pump.finished
            ):
                raise AppServerError("native process left descendant output pipes open")
            if exit_code != 0 and not self.terminated_by_driver:
                raise AppServerError(
                    f"native process exited unsuccessfully ({exit_code})"
                )
        finally:
            self.child.close_stdin()
            self._closed = (
                self.child.poll() is not None
                and self.child.stdout_pump.finished
                and self.child.stderr_pump.finished
            )
