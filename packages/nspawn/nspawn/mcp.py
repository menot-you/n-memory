"""The MCP surface nSPAWN serves, and the ceilings it refuses at.

The official MCP Python SDK (`mcp`, wired in `main.py`) owns the JSON-RPC
framing and the handshake; this module owns the tool catalogue and the one
call boundary, `McpServer.call_tool`. `main.build` answers one call at a time.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import sys
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, fields, is_dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jsonschema import Draft202012Validator, validators  # type: ignore[import-untyped]
from jsonschema.exceptions import best_match  # type: ignore[import-untyped]

from . import child, config, driver, native, notify, ranks
from .errors import UNKNOWN_CODE, NSpawnError, nerr_of
from .limits import MAX_DURABLE_ROW_BYTES
from .picker import PickerError
from .registry import (
    STORE_ERRORS,
    SWARM_PREFIX,
    Registry,
    RegistryError,
    store_path,
)
from .rows import RunPhase, validate_system_prompt
from .supervisor import (
    HOST_CONTROL_REFUSAL,
    InvalidRunRequest,
    RunResult,
    RunStatus,
    Supervisor,
    agent_system_prompt,
    provider_report,
    review_branch,
)

if TYPE_CHECKING:
    from .supervisor import (
        CloseReadback,
        FleetPage,
        ProviderReport,
        QuotaReport,
        TranscriptPage,
    )

SERVER_NAME = "nspawn"
SERVER_VERSION = "0.1.0"

#: The hard cap on one tool's rendered answer.
MAX_MCP_RESPONSE_BYTES = 1024 * 1024
#: Compact JSON tools/call params ceiling; checked before dispatch.
MAX_MCP_PARAMS_BYTES = 1024 * 1024
#: Maximum serialized result bytes inline before lossless artifact spill.
MAX_RESPONSE_BYTES = 65536

#: Environment inputs for the default live Supervisor. Read lazily, on the first
#: real tool dispatch, so the handshake and ``tools/list`` never require a
#: database: a bare ``McpServer()`` — exactly what ``main.py`` constructs —
#: must answer LD-0 before any of this is touched.

#: JSON-RPC codes this door emits.
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


#: Schema annotation: the arguments of which a call names exactly one.
EXACTLY_ONE_OF = "x-exactly-one-of"


#: The execution-class vocabulary, READ FROM `ranks.py` AND NEVER RETYPED.
#:
#: `ranks.py` says whose it is: "The execution-class vocabulary is owned here as
#: plain strings." So the vocabulary is every module-level plain string that
#: module declares, taken from the module itself — a copied literal rots, and
#: this campaign found five that had.
#:
#: NOT built from `ranks._AXES`: that table is the RANKED subset, and
#: `DETERMINISTIC` is a class precisely BY being absent from it, so an enum
#: derived from it would refuse a class the router accepts. The cost of reading
#: the whole namespace is that an unrelated uppercase string added to `ranks.py`
#: would land here — which is why `test_mcp_l6` pins the exact eight: that drift
#: is RED, never silent.
EXECUTION_CLASSES: tuple[str, ...] = tuple(
    value
    for name, value in vars(ranks).items()
    if name.isupper() and isinstance(value, str)
)

#: The one sentence a caller needs to choose the cage. It names the CONSEQUENCE
#: rather than the flag: a caller picking a dispatch mode is deciding what the
#: child may do to the disk, not which vendor argument carries it.
_READ_ONLY_DESCRIPTION = (
    "Dispatch the child caged: it can read and search inside `cwd` and explicit "
    "`read_paths`, and every write refuses — the operator's `bypassPermissions` "
    "does not reach it. Under the Claude `dontAsk` cage the child runs only the "
    "shell commands the vendor classifies as read-only (grep, find, wc) and "
    "never runs a script, an interpreter, a clone or a mktemp: every other "
    "command is denied with nobody to ask. nSPAWN makes no git contract on any "
    "route; pass history as a file in `read_paths`. (Measured 2026-10-09, a "
    "fact and not a promise: that cage, with `--disallowedTools Bash(git:*)`, "
    "denied every git spelling tried.) Use it for any audit, survey or "
    "investigation that reads. A run that must write is not caged: a writable "
    "dispatch (false, the default) must also set `native_write`, and is "
    "otherwise refused before a child starts. `read_paths` requires this cage "
    "or `native_write`; `nspawn_review_start` and `nspawn_batch_start` are "
    "always caged."
)
_NATIVE_WRITE_DESCRIPTION = (
    "Run one writer turn that can write only inside `cwd`. Use a route that "
    "lists `write` in `nspawn_provider_list`. Today, these routes are claude "
    "and codex. AGY has no write capability. nSPAWN refuses a claude route that "
    "sends its model calls to a different endpoint. `cwd` must be an existing "
    "absolute directory under a `[native_write]` root of the roster. `cwd` must "
    "be a standalone clone: its `.git` is a directory, not a linked worktree. "
    "The run takes the one-writer claim on `cwd`. The child gets private state "
    "and no network. On claude, the writer has the Bash tool only, no MCP tools "
    "and the explicit `dontAsk` permission mode, and cannot read or write `.git`. "
    "On codex, the writer can read `.git` but cannot write it. On codex, HOME, "
    "TMPDIR and the caches are disposable directories under "
    "`cwd/.nott/disposable`, and `nmemory_context_show` is the only MCP tool. "
    "Every process that the writer starts runs in the systemd slice of the run. "
    "Close empties the slice before it releases the claim. The writer does not "
    "commit or push. You commit and push after the run. nSPAWN refuses the run "
    "with `read_only` and on a route that does not list `write`. A `session_id` "
    "resume of a writer run must repeat `native_write=true`. nSPAWN refuses "
    "`native_write` with any other `session_id`. An `original_run` continuation "
    "retains the recorded opt-in."
)

_ORCHESTRATOR_DESCRIPTION = (
    "Bind this read-only codex turn to one durable conversation session and one "
    "memory project, and give it the private orchestrator MCP and nothing else: "
    "{session, project, repository, runtime, database_dir, interpreter}, every "
    "path absolute and canonical. `repository` is the session's REAL repository "
    "root — the tree its proposals are bound to; state it, because nothing stands "
    "in for it and a dispatch that omits it is refused by name. The turn never "
    "reads or writes it: it reads only `cwd` (its clean inspection snapshot, "
    "recorded beside the repository as `snapshot`) and `runtime`, takes no "
    "`read_paths` beside them, no `native_write`, and no route but codex. Its "
    "session, both trees, permission profile and tool contract are recorded with "
    "the admission itself, in one transaction, so a turn that exists is a turn "
    "whose binding is on record. Omit it and nothing changes: the dispatch is the "
    "one this verb always admitted, down to its idempotency key."
)
_CWD_DESCRIPTION = (
    "The child's working directory. A caged child is confined to this `cwd` plus "
    "trees explicitly named by `read_paths`; no other path is granted."
)
_READ_PATHS_DESCRIPTION = (
    "Extra trees the caged child may read, each passed as one literal `--add-dir` "
    "argument. They never grant writes and are refused on a writable execute "
    "other than `native_write`."
)
_RUN_ID_DESCRIPTION = "A run_id from a start tool or `nspawn_run_list`."


#: One sentence per routing argument, shared by every verb that takes it, so a
#: cold caller learns the vocabulary from `tools/list` — which every MCP client
#: already receives — instead of reading the owner's roster off the disk. That
#: read is what actually happened: an agent asked to dispatch "CODEX + AGY"
#: opened `~/.nott/spawn.toml` first, because these four arguments were
#: identical and mute.
_SYSTEM_PROMPT_DESCRIPTION = (
    "System instructions for this dispatch as literal text, not a path. The exact "
    f"text is persisted with the run, limited to {MAX_DURABLE_ROW_BYTES} UTF-8 bytes, "
    "contains no NUL, and is never interpreted by a shell."
)

_AGENT_DESCRIPTION = (
    "Name of an agent file in the NOTT plugin, for example nott:python-reviewer "
    "or python-reviewer. nSPAWN reads agents/<name>.md in the plugin root, removes "
    "the frontmatter and uses the body as system_prompt. Do not give agent and "
    "system_prompt together. If the name is unknown, nSPAWN refuses the call and "
    "gives the known names."
)


_PROVIDER_DESCRIPTION = (
    "Roster name of the route to dispatch to — the owner's own name for a "
    "provider, never a vendor CLI. The live list is `nspawn_provider_list`, which "
    "reads roster routes and answers while the store is down. The "
    "roster declares claude, fugu, agy, codex, zai and qwen — a snapshot of "
    "owner data, not a closed set: a name it declares "
    "later is served here without any change to this schema. When both are "
    "present, explicit provider wins "
    "and `class` does not route. Omit to let `class` route, or to take the "
    "roster's own default."
)
_MODEL_DESCRIPTION = (
    "Model id, caller-pinned and checked against the chosen provider's declared "
    "models. The vocabulary is per-provider and there is no global list: "
    "`nspawn_provider_list` reports each provider's `models` (empty when it trusts the proxy's live catalog) and its "
    "`default_model`. Omit to take that provider's default."
)
_EFFORT_DESCRIPTION = (
    "Effort label, per-provider and not one ladder: `nspawn_provider_list` reports "
    "the efforts each provider actually serves, and they diverge — `xhigh` "
    "exists on some routes and `max` is the whole ladder on others. An effort "
    "the route does not serve is a refused turn, never a slower one; that "
    "includes an effort a roster line declares but the route's transport "
    "rewrites on the wire. The roster's `[classes]` table decides what each "
    "`class` buys and its ceiling: omit this to take the class initial, and a "
    "pin above the ceiling is refused naming both. On `nspawn_review_start` "
    "the class is always `review`."
)
_CLASS_DESCRIPTION = (
    "What kind of work this run is, so routing chooses by the owner's declared "
    "ranks instead of by name: judgment, planning, review, verification and "
    "implementation rank by `strength`, mechanical_fanout by `thrift`, design "
    "by `design`, and deterministic expresses no preference. Closed vocabulary — a value outside "
    "it is refused by name, never silently ignored. Explicit provider wins when "
    "both are supplied; then `class` does not route but still prices the run: "
    "the roster's `[classes]` table sets the effort each class starts at and "
    "the most a caller may pin. Required. The declared class is a ceiling: "
    "when the roster has a `[classes]` table, the first 4000 characters of "
    "`prompt` go to the configured System1 classifier, and the run executes as "
    "the observed class only when it is cheaper on every axis, else as the "
    "declared one (a classifier failure observes `unclassified`); a "
    "disagreement or a failure is reported in `nspawn_run_show`'s `warning`."
)

#: The three judgment verbs share their `state` and `model` arguments, their
#: answer and their refusal. `state` leaves this machine, so its sentence says where.
_JUDGMENT_STATE_DESCRIPTION = (
    "The content every question is asked about. It is sent to the configured "
    "System1 endpoint (NOTT_SYSTEM1_ENDPOINT, else the roster's [system1] "
    "table); there is no default endpoint."
)
_JUDGMENT_MODEL_DESCRIPTION = (
    "System1 model id, not an nSPAWN provider model; omit for the configured "
    "default (laya-nott unless NOTT_SYSTEM1_MODEL or the roster names another)."
)
_JUDGMENT_ANSWER = (
    "Returns {answers: {id: {type, value, probabilities, confidence, legend}}}. "
    "An unconfigured or failing service (no endpoint, no NOTT_SYSTEM1_API_KEY, an "
    "HTTP error, the configured timeout, 120 s by default) is a -32002 error, "
    "never an answer."
)


def _read_paths_property() -> dict[str, Any]:
    return {
        "type": "array",
        "minItems": 1,
        "maxItems": child.MAX_READ_PATHS,
        "items": {"type": "string", "minLength": 1},
        "description": _READ_PATHS_DESCRIPTION,
    }


def _system_prompt_property() -> dict[str, Any]:
    return {
        "type": "string",
        "minLength": 1,
        "maxLength": MAX_DURABLE_ROW_BYTES,
        "description": _SYSTEM_PROMPT_DESCRIPTION,
    }


def _agent_property() -> dict[str, Any]:
    return {"type": "string", "minLength": 1, "description": _AGENT_DESCRIPTION}


_PRODUCER_DESCRIPTION = (
    "The agent that made the work this run judges: route names, account aliases "
    "or model ids, for example the author's own route and model. The run never "
    "executes as one of them. nSPAWN refuses a pinned `provider` that is one of "
    "them. With no `provider`, a review or verification run takes the routes in "
    "the owner's order: the roster's `[preference]` list for the class, then the "
    "reviewer role, then the class rank. nSPAWN skips each route that cannot serve "
    "now and writes the reason in `warning`. When a route refuses the turn, the "
    "work continues as a new run on the next route, and the failed run's "
    "`warning` names it."
)


def _producer_property() -> dict[str, Any]:
    return {
        "type": "array",
        "minItems": 1,
        "maxItems": 8,
        "items": {"type": "string", "minLength": 1},
        "description": _PRODUCER_DESCRIPTION,
    }


#: Code points: at 4 UTF-8 bytes each plus a batch entry's `/index`, a key or a
#: group the schema admits always fits the store's 512-byte identifier.
DEDUP_TEXT_MAX_CHARS = 120


def _dedup_properties() -> dict[str, Any]:
    return {
        "idempotency_key": {
            "type": "string",
            "minLength": 1,
            "maxLength": DEDUP_TEXT_MAX_CHARS,
            "description": (
                "Dedup key, scoped to the calling project. The same key with the "
                "same request reattaches to its run (`deduplicated: true` and the "
                "run's current phase) instead of dispatching again; the same key "
                "with a different request is refused as IdempotencyConflict. "
                "The request is compared as sent: omitting provider, model or "
                "effort and naming its default are different requests. "
                "Holds until the run is closed, then 24 h. No `/`: a batch keys "
                "its entries `key/index`."
            ),
        },
        "group": {
            "type": "string",
            "minLength": 1,
            "maxLength": DEDUP_TEXT_MAX_CHARS,
            "description": (
                "A label the run carries, read back by nspawn_run_show. It "
                "must not start with `swarm:`."
            ),
        },
    }


#: The prefix a swarm adds to its group: `swarm:<name>`.
SWARM_TEXT_MAX_CHARS = DEDUP_TEXT_MAX_CHARS - len(SWARM_PREFIX)


def _swarm_property() -> dict[str, Any]:
    return {
        "type": "string",
        "minLength": 1,
        "maxLength": SWARM_TEXT_MAX_CHARS,
        "description": (
            "The name of a swarm. Give the same name to each run of one "
            "dispatch, for example to the two readers of one review. The "
            "session gets ONE notice when all runs of the swarm are terminal. "
            "That notice gives the run_id, the phase and the executed account "
            "of each run, and the cause of each run that did not complete. "
            "If a read-only run fails, nSPAWN starts the work on the next "
            "entry of its walk. The walk is the preference order, then the "
            "other accounts of the route. For a pinned route, the walk is the "
            "other accounts of that route. A pinned account has no walk, and "
            "a preference entry that names an account adds no accounts. A "
            "route refusal takes the next entry each time. A different failure "
            "takes it one time. A run "
            "with no timeout_ms stops after 2 hours, and nSPAWN does not start "
            "it again. The run shows the group `swarm:<name>`. Do not give "
            "`group` with it. A node door refuses this option."
        ),
    }


def _swarm_group(group: str | None, swarm: str | None) -> str | None:
    """The group a start verb stores: `swarm:<name>` for a swarm.

    The prefix is reserved: a group that starts with it is refused. A node door
    refuses `swarm`: the host takes a node's notices one by one.
    """
    if group is not None and group.startswith(SWARM_PREFIX):
        raise McpError(
            INVALID_PARAMS,
            f"group must not start with {SWARM_PREFIX!r}: use the swarm option",
        )
    if swarm is None:
        return group
    if group is not None:
        raise McpError(INVALID_PARAMS, "give swarm or group, not both")
    if driver.node_owner(os.environ) is not None:
        raise McpError(
            INVALID_PARAMS,
            "a node door does not accept swarm: the host gives one notice per run",
        )
    return SWARM_PREFIX + swarm


@dataclass(frozen=True, slots=True)
class NSpawnToolContract:
    """One future MCP tool declaration; it is not live on the legacy door."""

    name: str
    description: str
    input_schema: dict[str, Any]


#: L6's complete replacement surface. It stays separate from ``TOOLS`` so merely
#: declaring these contracts cannot make the deliberately RED legacy port pass.
NSPAWN_TOOL_CONTRACTS = (
    NSpawnToolContract(
        name="nspawn_run_start",
        description=(
            "Start one run asynchronously on the provider the roster routes to "
            "(any declared backend, not only Claude) and return {run_id, "
            "deduplicated, phase}. The call waits up to 180 s for the run's "
            "driver to acknowledge; past that it fails with AdmissionPending, "
            "whose data names a launch_uuid that `nspawn_run_show` resolves — "
            "dispatching again would start a second run. "
            "The run lives in its own driver process and survives the "
            "caller's session. In a Claude Code session its terminal phase "
            "(clean exit, failure or signal death) arrives as one cross-session "
            "notice naming this run_id and the phase — pushed to the "
            "dispatching session while that session lives, else printed once "
            "at the project's next session start; read the answer with "
            "`nspawn_result_show` when it arrives. A run with `swarm` gives no "
            "notice of its own: the swarm gives one notice for all its runs. "
            "Other clients receive no "
            "notice: block on `nspawn_result_show` with a positive timeout_ms, "
            "or read `nspawn_run_list`."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "prompt": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "The task for the child as literal text; the child "
                        "shares no context with this session."
                    ),
                },
                "cwd": {
                    "type": "string",
                    "minLength": 1,
                    "description": _CWD_DESCRIPTION,
                },
                "provider": {
                    "type": "string",
                    "minLength": 1,
                    "description": _PROVIDER_DESCRIPTION,
                },
                "model": {
                    "type": "string",
                    "minLength": 1,
                    "description": _MODEL_DESCRIPTION,
                },
                "effort": {
                    "type": "string",
                    "minLength": 1,
                    "description": _EFFORT_DESCRIPTION,
                },
                # NO `minLength`: the enum already refuses the empty string, and
                # it refuses it while NAMING the eight, which is the answer the
                # caller needs. A length bound beside a closed set is dead.
                "class": {
                    "type": "string",
                    "enum": list(EXECUTION_CLASSES),
                    "description": _CLASS_DESCRIPTION,
                },
                "session_id": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "The thread_id of an earlier run (as `nspawn_run_show` "
                        "reports it) to resume that vendor session; requires "
                        "read_only true. A codex or agy thread needs a recorded "
                        "origin: the resume runs on that run's provider and "
                        "account, in the same cwd, and cannot widen its read grants."
                    ),
                },
                "original_run": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Continue this verified original run's conversation without changing its phase or grants.",
                },
                "activation": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "provider": {"enum": ["claude", "codex", "qwen", "agy"]},
                        "account_route": {"type": "string", "minLength": 1},
                        "model": {"type": "string", "minLength": 1},
                        "effort": {"type": ["string", "null"]},
                        "profile": {"type": ["string", "null"]},
                        "profile_digest": {"type": ["string", "null"]},
                        "grant_root": {"type": ["string", "null"]},
                    },
                    "required": ["provider", "account_route", "model"],
                    "description": "Prepare this selected native owner session without a prompt or inference. Requires a server with host control.",
                },
                "session_operation": {
                    "type": "object",
                    "description": "One ACP operation for this original run's privately owned native channel. Requires a server with host control.",
                },
                "command": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "executable": {"type": "string", "minLength": 1},
                        "arguments": {"type": "array", "items": {"type": "string"}},
                        "name": {"type": "string", "maxLength": 80},
                        "cols": {"type": "integer", "minimum": 2, "maximum": 500},
                        "rows": {"type": "integer", "minimum": 2, "maximum": 200},
                        "centerId": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 512,
                        },
                        "replacesRun": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 512,
                        },
                    },
                    "required": ["executable", "cols", "rows", "centerId"],
                    "description": "Open an owner command session with PATH identity, process custody, and retained terminal output.",
                },
                "timeout_ms": {
                    "type": "integer",
                    "minimum": 0,
                    "description": (
                        "Deadline for the run, in milliseconds, counted from dispatch. "
                        "Past it the child is signalled and the run lands `failed` with a "
                        "reason naming the deadline — a deliberate death, never a vendor "
                        "error. The dispatch stays asynchronous: this never blocks the "
                        "call. Omit or 0 for no deadline (the default): a bound nobody "
                        "asked for is a killer nobody authorized. A node caller "
                        "(NOTT_NSPAWN_NODE_SESSION set) must pass a positive one."
                    ),
                },
                # NOT on `nspawn_review_start`: a review IS read-only and cannot opt
                # out. That verb called itself read-only for its whole life
                # while nothing enforced it, and an auditing child installed a
                # binary of unknown provenance (F-026).
                "read_only": {
                    "type": "boolean",
                    "description": _READ_ONLY_DESCRIPTION,
                },
                "native_write": {
                    "type": "boolean",
                    "description": _NATIVE_WRITE_DESCRIPTION,
                },
                "orchestrator": {
                    "type": "object",
                    "description": _ORCHESTRATOR_DESCRIPTION,
                },
                "read_paths": _read_paths_property(),
                "system_prompt": _system_prompt_property(),
                "agent": _agent_property(),
                "producer": _producer_property(),
                "swarm": _swarm_property(),
                "inference_profile": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "tools": {"const": "none"},
                        "output_schema": {"type": "object"},
                    },
                    "required": ["tools", "output_schema"],
                    "description": (
                        "Tool-free native JSON Schema inference on a Claude transport. "
                        "Requires read_only, loads no plugin or MCP, and refuses "
                        "read_paths, system_prompt, native_write and orchestrator. "
                        "The profile is recorded at admission and retained on resume."
                    ),
                },
                **_dedup_properties(),
            },
            "required": ["cwd"],
            "allOf": [
                {
                    "if": {
                        "not": {
                            "anyOf": [
                                {"required": ["activation"]},
                                {"required": ["session_operation"]},
                                {"required": ["command"]},
                            ]
                        }
                    },
                    "then": {"required": ["prompt"]},
                },
                {
                    "if": {
                        "not": {
                            "anyOf": [
                                {"required": ["original_run"]},
                                {"required": ["activation"]},
                                {"required": ["command"]},
                                # A pinned, tool-free and read-only inference (the
                                # node host's gist): nothing to route or price.
                                {
                                    "required": [
                                        "provider",
                                        "model",
                                        "effort",
                                        "inference_profile",
                                        "read_only",
                                    ],
                                    "properties": {
                                        "read_only": {"const": True},
                                        "native_write": {"const": False},
                                    },
                                },
                            ]
                        }
                    },
                    "then": {"required": ["class"]},
                },
            ],
        },
    ),
    NSpawnToolContract(
        name="nspawn_review_start",
        description=(
            "Start a read-only review of a branch in a fresh session that shares "
            "no context with this one. It returns {run_id, deduplicated, phase}. "
            "Do not pin `provider` or `model` to choose a reviewer: nSPAWN chooses "
            "it by the owner's preference order, skips a route that cannot serve "
            "now, and never runs as a `producer`. "
            "The child reads and searches. Under the Claude `dontAsk` cage, the "
            "child never runs a script or a gate. A review that must run a "
            "script or a gate is not caged. It needs a `nspawn_run_start` with "
            "`native_write`, on a route that lists `write` (today, claude and codex). nSPAWN reports "
            "its terminal phase as it reports the phase of `nspawn_run_start`."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "branch": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "The branch under review. It must be the branch checked "
                        "out in `cwd`: nSPAWN refuses a worktree on another branch. "
                        "If you do not give it, nSPAWN uses the branch checked out "
                        "in `cwd`. If `cwd` has a detached HEAD, nSPAWN refuses "
                        "the call."
                    ),
                },
                "cwd": {
                    "type": "string",
                    "minLength": 1,
                    "description": _CWD_DESCRIPTION,
                },
                "base": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "Base named in the default review prompt; it does not "
                        "reach the child when `prompt` is given."
                    ),
                },
                # The SAME three arguments as `nspawn_run_start`, so the same
                # three sentences: leaving them mute here would keep the exact
                # lie in the neighbouring verb. `class` is absent on purpose —
                # a review IS the `review` class and L7 pins it.
                "provider": {
                    "type": "string",
                    "minLength": 1,
                    "description": _PROVIDER_DESCRIPTION,
                },
                "model": {
                    "type": "string",
                    "minLength": 1,
                    "description": _MODEL_DESCRIPTION,
                },
                "effort": {
                    "type": "string",
                    "minLength": 1,
                    "description": _EFFORT_DESCRIPTION,
                },
                "prompt": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "The reviewer's whole task, replacing the default "
                        '("Review branch <branch> [against base <base>] '
                        'read-only; do not modify the worktree."). The reviewer '
                        "shares no context with this session: name the artefact "
                        "under judgment, the source to trace it against (grant "
                        "it with `read_paths`) and the one thing to decide."
                    ),
                },
                "read_paths": _read_paths_property(),
                "system_prompt": _system_prompt_property(),
                "agent": _agent_property(),
                "producer": _producer_property(),
                "swarm": _swarm_property(),
                **_dedup_properties(),
            },
            "required": ["cwd"],
        },
    ),
    NSpawnToolContract(
        name="nspawn_batch_start",
        description=(
            "Start one prompt on several routes at once and return {runs: "
            "[{provider, run_id, deduplicated, phase}], refused: [{provider, "
            "reason, type[, launch_uuid]}]} — the fan-out for exploration and "
            "for review, where the "
            "answer you want is the disagreement between routes. Every child "
            "is caged (read-only) by construction, never by argument: the "
            "verb takes one `cwd`, and N writers in one tree is the "
            "one-writer law broken N-1 times. A writable fan-out is N "
            "`nspawn_run_start` calls into N worktrees, each admitted only as "
            "its `read_only` describes. A name the roster does "
            "not declare refuses the whole call before anything is "
            "dispatched; a wall or a missing credential refuses only its own "
            "entry and the siblings still run."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "prompt": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "The task for every child as literal text; no child "
                        "shares context with this session."
                    ),
                },
                "cwd": {
                    "type": "string",
                    "minLength": 1,
                    "description": _CWD_DESCRIPTION,
                },
                # NO `model`/`effort`: both vocabularies are PER-PROVIDER and
                # they diverge, so one batch-wide pin would refuse most of the
                # fan-out it was asked to spread. Each route takes its own
                # declared default, which is what "N different takes" means.
                # NO `session_id`: N runs in one session collide N ways.
                # NO `timeout_ms`: each route announces its own terminal
                # phase to the caller, so there is nothing to block for.
                "providers": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"type": "string", "minLength": 1},
                    "description": (
                        "Roster names of the routes to run the prompt on, one run "
                        "per name (live list: `nspawn_provider_list`). An "
                        "undeclared name refuses the whole call before anything "
                        "is dispatched; a walled route or a missing credential "
                        "refuses only its own entry."
                    ),
                },
                "read_paths": _read_paths_property(),
                "system_prompt": _system_prompt_property(),
                **_dedup_properties(),
            },
            "required": ["prompt", "cwd", "providers"],
        },
    ),
    NSpawnToolContract(
        name="nspawn_run_list",
        description=(
            "List the newest 128 runs in the registry (every project on this "
            "account), newest first, under one clock (`observed_at`): run_id, "
            "lane, phase, executed provider and account, model, tokens, "
            "turn_count, group, created_at and updated_at, plus `notices` — "
            "terminal notices no session has received yet. Older runs are not "
            "listed and there is no cursor; read a known run with "
            "`nspawn_run_show`. It never long-polls."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {},
            "required": [],
        },
    ),
    NSpawnToolContract(
        name="nspawn_run_show",
        description=(
            "Read one run's status: phase, run_dir, failure_reason, tokens, "
            "executed provider and account, warning, marker (the executor's own "
            "DONE/BLOCKED claim, reported as an observation), thread_id, group "
            "and child_addr. Use it for what a run is doing mid-flight; outside "
            "Claude Code it is also how you learn a run ended. `child_addr` is "
            "the live Claude child's own `uds:` session address — present only "
            "while that process is alive, null on a terminal row, for non-Claude "
            "backends, before spawn, or when the dispatch did not come from a "
            "Claude Code session. `effort` is {requested, requested_source, sent, "
            "sent_source, executed, executed_source}: what the caller asked for "
            "and the launcher was handed, as the run's admission recorded them "
            "(source `admission`, a null there meaning none), or `unknown` for a "
            "run admitted before that record existed. `executed` is always null "
            "and `unknown`: the current adapters have no verified turn-scoped effort echo. "
            "`admission` is the observed {group, node, releasedGeneration, nodeGeneration} "
            "binding, or null for an unbound run. `terminal_receipt` is the first observed "
            "terminal {phase, reason, at}, preserved after close; null means unknown "
            "for a run that ended before receipts existed. Both fields are read-only."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "run_id": {
                    "type": "string",
                    "minLength": 1,
                    "description": _RUN_ID_DESCRIPTION,
                },
                "timeout_ms": {
                    "type": "integer",
                    "minimum": 0,
                    "description": (
                        "Milliseconds to block until the run reaches a terminal "
                        "phase; the current status is returned when the wait "
                        "ends, with no error at expiry. 0 or omitted returns at "
                        "once."
                    ),
                },
                "launch_uuid": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "The launch an `AdmissionPending` answer named: resolves "
                        "to its run once the driver has acknowledged it. Never "
                        "dispatch again to find out."
                    ),
                },
                "idempotency_key": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": DEDUP_TEXT_MAX_CHARS,
                    "description": (
                        "The key of a keyed `nspawn_run_start` or "
                        "`nspawn_review_start`, with that call as `request`: "
                        "answers the run's status plus `lookup: found`, "
                        "`{lookup: missing, idempotency_key}` when the key names "
                        "no run, or `{lookup: conflict, run_id}` when it names a "
                        "run of another request. It starts, reattaches and "
                        "notifies nothing, and takes no timeout_ms."
                    ),
                },
                "request": {
                    "type": "object",
                    "description": (
                        "With idempotency_key only: {tool, arguments}, the keyed "
                        "call exactly as sent, group included and "
                        "idempotency_key left out."
                    ),
                },
            },
            "required": [],
            # Exactly one of these. Top-level oneOf is refused by some clients'
            # tool schemas, so the rule is an annotation validate_arguments
            # enforces.
            EXACTLY_ONE_OF: ["run_id", "launch_uuid", "idempotency_key"],
        },
    ),
    NSpawnToolContract(
        name="nspawn_result_show",
        description=(
            "Read the answer of one run without its transcript. The answer has "
            "run_id, phase, final_text and stderr_tail. When nSPAWN recorded a "
            "failure, the answer also has failure_reason: the error class and "
            "domain code, never the raw exception text. failure_reason is the "
            "same as in `nspawn_run_show`, and it stays after close. final_text "
            "is null until the run has a result. phase is the run phase at the "
            "read. When a wait ends before the run ends, phase is not terminal. "
            "When an account or a route could not serve the run, nSPAWN moves "
            "the work to the next account or route. Then the answer is the "
            "answer of the run that served: run_id names that run, and alert "
            "names each route that could not serve, its cause and the route that "
            "served. Otherwise the answer has no alert."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "run_id": {
                    "type": "string",
                    "minLength": 1,
                    "description": _RUN_ID_DESCRIPTION,
                },
                "timeout_ms": {
                    "type": "integer",
                    "minimum": 0,
                    "description": (
                        "Milliseconds to wait until the run reaches a terminal "
                        "phase. Then nSPAWN returns the answer. When the wait "
                        "ends first, nSPAWN returns the current answer and no "
                        "error. 0 or no value returns at once."
                    ),
                },
            },
            "required": ["run_id"],
        },
    ),
    NSpawnToolContract(
        name="nspawn_transcript_show",
        description=(
            "Read the stream-json events one run has persisted, oldest first, with "
            "credentials redacted. Use it to see what a run did (tool calls, text, "
            "the result frame); use `nspawn_result_show` when only the final text is "
            "wanted, and `nspawn_run_show` for phase and tokens without the events."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "run_id": {
                    "type": "string",
                    "minLength": 1,
                    "description": _RUN_ID_DESCRIPTION,
                },
                "since_seq": {
                    "type": "integer",
                    "minimum": 0,
                    "description": (
                        "Position to start reading from; 0 or omitted starts at the "
                        "first event. The page returns next_seq — pass it here to "
                        "read the following page."
                    ),
                },
                "max_bytes": {
                    "type": "integer",
                    "minimum": 1,
                    "description": (
                        "Upper bound on one page's event bytes (default "
                        f"{Supervisor.DEFAULT_TRANSCRIPT_MAX_BYTES}); the page stops "
                        "at the last whole event under it and reports "
                        "truncated=true when events remain. Any nSPAWN answer "
                        f"whose JSON exceeds {MAX_RESPONSE_BYTES} bytes arrives as "
                        "{preview, path, bytes}: preview is the first "
                        f"{MAX_RESPONSE_BYTES} bytes of that JSON (not parseable) "
                        "and path is a file holding all of it. The page's JSON is "
                        "larger than max_bytes because each event line is escaped "
                        "as a JSON string."
                    ),
                },
                "frame_types": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": Supervisor.MAX_TRANSCRIPT_FRAME_TYPES,
                    "items": {"type": "string", "minLength": 1},
                    "description": (
                        "Return only these top-level stream-json frame types. "
                        "By default, every frame except system/thinking_tokens "
                        "telemetry is returned."
                    ),
                },
            },
            "required": ["run_id"],
        },
    ),
    NSpawnToolContract(
        name="nspawn_run_close",
        description=(
            "Close one run: a live child is stopped (SIGSTOP, then SIGKILL on its "
            "process group, after its identity is re-verified); a finished run is "
            "only released. Returns the "
            "terminal process-tree readback. Closing is what frees the run's worktree "
            "claim — a finished run that was never closed keeps fencing its worktree "
            "until the lease expires. A run whose live driver still drives it is "
            "closed by that driver; if it has not answered in 45 s the answer is "
            "ClosePending and the request stands. By idempotency_key it releases "
            "a keyed dispatch's claim under the key's admission lock: the run the "
            "key names is closed (`lookup: found`), `{lookup: conflict, run_id}` "
            "when it names another request's run, and `{lookup: fenced, "
            "idempotency_key}` when it names none — then no later dispatch of the "
            "key is ever admitted (KeyFenced). With only_unadmitted: true, "
            "a found run is returned unchanged and is never closed; only a "
            "missing key is fenced. Guarded answers echo only_unadmitted: true."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "run_id": {
                    "type": "string",
                    "minLength": 1,
                    "description": _RUN_ID_DESCRIPTION,
                },
                "idempotency_key": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": DEDUP_TEXT_MAX_CHARS,
                    "description": (
                        "The key of a keyed `nspawn_run_start` or "
                        "`nspawn_review_start`, with that call as `request`."
                    ),
                },
                "request": {
                    "type": "object",
                    "description": (
                        "With idempotency_key only: {tool, arguments}, the keyed "
                        "call exactly as sent, group included and "
                        "idempotency_key left out."
                    ),
                },
                "only_unadmitted": {
                    "type": "boolean",
                    "description": (
                        "With idempotency_key only: fence a missing key, but "
                        "return an admitted run unchanged without closing it. "
                        "Omitted or false preserves ordinary keyed close."
                    ),
                },
            },
            "required": [],
            EXACTLY_ONE_OF: ["run_id", "idempotency_key"],
        },
    ),
    NSpawnToolContract(
        name="nspawn_route_ping",
        description=(
            "Ping every declared route on its own `model_fast` and answer per "
            "route under D18: `0` it answered, `2` a typed refusal that is not "
            "a failure — no quota, no credential, no declared model, or the "
            "deadline — and `3` only behaviour, a wrong answer or a broken "
            "transport. An empty balance never reddens a route. Unlike "
            "`nspawn_batch_start` this blocks, because the verdict is the product; "
            "the wait is bounded by one deadline for the whole call, not one "
            "per route. Naming no provider pings the whole roster. Each ping is "
            "a real model turn, run read-only in `cwd`, and spends that route's "
            "quota. Returns {deadline_ms, routes: [{provider, code, verdict "
            "(green, refused or red), cause, run_id}]}."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "cwd": {
                    "type": "string",
                    "minLength": 1,
                    "description": _CWD_DESCRIPTION,
                },
                "providers": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"type": "string", "minLength": 1},
                    "description": (
                        "Roster names to ping; omit to ping every declared route. "
                        "An undeclared name refuses the whole call."
                    ),
                },
                # REQUIRED to be positive, but not a required ARGUMENT: the
                # default is what makes the verb one call, and zero is refused
                # rather than read as "no deadline".
                "deadline_ms": {
                    "type": "integer",
                    "minimum": 1,
                    "description": (
                        "One deadline for the whole call in milliseconds "
                        "(default 180000; must be positive). A route still "
                        "running at the deadline is reported refused (code 2), "
                        "not broken."
                    ),
                },
            },
            "required": ["cwd"],
        },
    ),
    NSpawnToolContract(
        name="nspawn_usage_show",
        description="Read recorded tokens for 30 local days by stable node identity. Unknown counters remain partial. Capped reads remain incomplete.",
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "timeZone": {"type": "string", "minLength": 1, "maxLength": 255}
            },
            "required": ["timeZone"],
        },
    ),
    NSpawnToolContract(
        name="nspawn_quota_show",
        description=(
            "Report what is known of every declared provider's quota, or one "
            "named provider's: per provider its models and default model, a "
            "`state` (observed, stale, expired, never_observed or no_source — "
            "never a zero standing in for an unknown), windows with utilization, "
            "resets_at and observed_at, per-account entries, and any active wall "
            "(`cooling_until`). An undeclared provider name is refused."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "provider": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Roster name to report alone; omit for all.",
                },
                "refresh": {
                    "type": "integer",
                    "minimum": 0,
                    "description": (
                        "0 (default) answers from stored observations only; any "
                        "positive value first asks native Codex account metadata; "
                        "the magnitude is ignored."
                    ),
                },
            },
            "required": [],
        },
    ),
    NSpawnToolContract(
        name="nspawn_judgment_estimate",
        description=(
            "Ask yes/no questions where the probability is the product, not a "
            "verdict. Reach for this when a binary answer would throw away the "
            "hesitation you need: whether a message names live work, whether a "
            "document carries an injected instruction, whether a claim is supported "
            "by its evidence. A noul carries no separate confidence — the "
            "probability is the signal, and one near 0.5 is the model saying the "
            "question is ambiguous on this input. " + _JUDGMENT_ANSWER
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "state": {
                    "type": "string",
                    "minLength": 1,
                    "description": _JUDGMENT_STATE_DESCRIPTION,
                },
                "questions": {
                    "type": "object",
                    "minProperties": 1,
                    # A strict tool grammar (vLLM xgrammar) refuses a map that
                    # must hold a key it does not declare unless it says so.
                    "additionalProperties": True,
                    "description": (
                        "Map of question id to either the question text, or "
                        '{"instructions": text, "criteria": {"true": ..., '
                        '"false": ...}} when the line between yes and no needs '
                        "defining. At most 64 questions per call; each is "
                        "answered independently of the others."
                    ),
                },
                "model": {
                    "type": "string",
                    "minLength": 1,
                    "description": _JUDGMENT_MODEL_DESCRIPTION,
                },
            },
            "required": ["state", "questions"],
        },
    ),
    NSpawnToolContract(
        name="nspawn_judgment_choose",
        description=(
            "Pick one option from a set with no order between them, with the "
            "probability of each and a confidence. Reach for this to route a "
            "request, classify a document, or name which of several things a text "
            "actually is. Include an explicit none-of-these option whenever the set "
            "might not cover the input: without it the model must pick a wrong one, "
            "and the distribution lies. " + _JUDGMENT_ANSWER
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "state": {
                    "type": "string",
                    "minLength": 1,
                    "description": _JUDGMENT_STATE_DESCRIPTION,
                },
                "questions": {
                    "type": "object",
                    "minProperties": 1,
                    # A strict tool grammar (vLLM xgrammar) refuses a map that
                    # must hold a key it does not declare unless it says so.
                    "additionalProperties": True,
                    "description": (
                        'Map of question id to {"instructions": text, "criteria": '
                        "{option: description, ...}}; criteria is the complete set "
                        "of options, so include a none-of-these option when the "
                        "set may not cover the input. At most 64 questions per call."
                    ),
                },
                "model": {
                    "type": "string",
                    "minLength": 1,
                    "description": _JUDGMENT_MODEL_DESCRIPTION,
                },
            },
            "required": ["state", "questions"],
        },
    ),
    NSpawnToolContract(
        name="nspawn_judgment_score",
        description=(
            "Place the input on an ordered spectrum whose levels you describe, "
            "with a continuous position between them and the probability of each "
            "level. Reach for this when the judgement has degrees that matter — "
            "severity, how complete a handoff is, how far a number sits from its "
            "floor. Describe every level: an unlabelled ladder is a number the "
            "reader has to guess the meaning of. " + _JUDGMENT_ANSWER
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "state": {
                    "type": "string",
                    "minLength": 1,
                    "description": _JUDGMENT_STATE_DESCRIPTION,
                },
                "questions": {
                    "type": "object",
                    "minProperties": 1,
                    # A strict tool grammar (vLLM xgrammar) refuses a map that
                    # must hold a key it does not declare unless it says so.
                    "additionalProperties": True,
                    "description": (
                        'Map of question id to {"instructions": text, "criteria": '
                        "[level description, ...]} with levels ordered lowest to "
                        "highest. At most 64 questions per call."
                    ),
                },
                "model": {
                    "type": "string",
                    "minLength": 1,
                    "description": _JUDGMENT_MODEL_DESCRIPTION,
                },
            },
            "required": ["state", "questions"],
        },
    ),
    NSpawnToolContract(
        name="nspawn_provider_list",
        description=(
            "List every declared provider — models, default model, accounts, "
            "efforts and routing ranks. Read the roster without opening the store. "
            "Claude family routes include native client model metadata, with no inference call."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            # One optional filter.
            "properties": {
                "provider": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Roster name to list alone; omit for all.",
                },
            },
            "required": [],
        },
    ),
)

#: Fast lookup from wire tool name to its declared contract.
NSPAWN_TOOL_BY_NAME: dict[str, NSpawnToolContract] = {
    contract.name: contract for contract in NSPAWN_TOOL_CONTRACTS
}

#: What `initialize` tells a cold client this door can do.
#:
#: THE TOOL LIST IS DERIVED, NEVER TYPED. The hand-written one announced spawn,
#: status, wait, send, transcript, close and operation_lookup: four of those
#: seven never existed on this surface and `nspawn_review_start` existed without being
#: announced. A cold agent plans its orchestration against that vocabulary and
#: discovers the lie one failed call at a time. A sentence built from the
#: registry cannot drift from it, and `test_mcp_l6` fails on any `nspawn_*`
#: token here that the registry does not carry.
SERVER_INSTRUCTIONS = (
    "nSPAWN controls executors; each dispatch runs in its own driver "
    "process, so a run outlives the session that started it. Triggers — reach "
    "for these by when to use them, not by what they are: "
    "bounded independent work goes to nspawn_run_start (one unit) or "
    "nspawn_batch_start (one prompt, several read-only routes), never to an "
    "improvised runner; a "
    "plan or review that needs a witness the author cannot supply goes to "
    "nspawn_review_start, a fresh read-only session with no shared context "
    "(omit `provider` and name the author in `producer`: nSPAWN then chooses "
    "the reviewer by the owner's preference order and never runs as the "
    "author); read "
    "nspawn_provider_list for the roster and nspawn_quota_show for the "
    "budget before dispatching, not after a refusal; when a run's notice "
    "arrives, read the answer with nspawn_result_show rather than re-dispatching. "
    "Tools: "
    + ", ".join(contract.name for contract in NSPAWN_TOOL_CONTRACTS)
    + ". Model is caller-pinned; writable linked worktrees are "
    "one-writer claimed. "
    "In a Claude Code session every run that reaches a terminal phase — a "
    "clean exit, a failure, or a signal death — owes the dispatching session "
    "one cross-session notice naming the run and its phase: pushed while "
    "that session lives, else printed once at the project's next session "
    "start; one notice per `swarm`. Other clients get "
    "no notice. nspawn_run_list lists the fleet and "
    "never long-polls; "
    "nspawn_run_show with a positive timeout_ms long-polls that one run. "
    "nspawn_provider_list reads roster routes and native model metadata without the store, so it "
    "answers even while the registry is unreachable. An answer over "
    f"{MAX_RESPONSE_BYTES} bytes of JSON arrives as "
    "{preview, path, bytes}: path is a file holding all of it. The "
    "executor's DONE/BLOCKED marker is parsed as an observation, never a "
    "self-witnessed kernel outcome."
)


class McpError(NSpawnError):
    """A refusal this door renders as a JSON-RPC error object."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        self.code = code
        self.rpc_message = message
        self.data = data
        Exception.__init__(self, message)

    def to_wire(self) -> dict[str, Any]:
        wire: dict[str, Any] = {"code": self.code, "message": self.rpc_message}
        if self.data is not None:
            wire["data"] = self.data
        return wire


class ResponseTooLarge(McpError):
    """One tool's answer exceeded the hard cap."""

    def __init__(self, size: int) -> None:
        super().__init__(
            INTERNAL_ERROR,
            f"nspawn response of {size} bytes exceeded the "
            f"{MAX_MCP_RESPONSE_BYTES}-byte hard cap; use pagination or a smaller window",
        )


class SupervisorUnconfigured(McpError):
    """No `Supervisor` was injected and the live store cannot build one.

    Raised only when a REAL tool is dispatched with no supervisor already
    attached; the handshake and ``tools/list`` never reach this path. The most
    common cause is a registry file nobody created yet: the door never creates
    one, `nspawn database create` does. That cure, like any other, arrives in
    `detail` from `nott_database`'s refusal; the door adds no copy of its own.
    """

    def __init__(self, store: str, detail: str) -> None:
        super().__init__(
            INTERNAL_ERROR,
            f"nspawn has no supervisor configured: the registry at {store} "
            f"cannot be opened ({detail})",
            data={"store": store},
        )


def _default_supervisor(
    sidecar_kind: str = "stdio", *, host_control: bool = False
) -> Supervisor:
    """Build the live Supervisor on this account's registry file.

    `stdio` for the session's own door, `driver` inside a per-call driver. A
    node's identity and grant are read first: a malformed one opens no store.
    """
    owner = driver.node_owner(os.environ)
    read_grant = driver.read_grant(os.environ)
    path = store_path()
    run_root = config.resolve_run_root()
    registry = Registry(path, uuid.uuid4().hex, allow_live_store=True)
    # Building the Registry does NOT open it: `connect` opens the file and
    # `open` checks its schema version and registers this instance. Both are
    # idempotent, so a reused process re-enters them safely.
    try:
        registry.connect()
    except RegistryError as error:
        raise SupervisorUnconfigured(str(path), str(error)) from error
    try:
        # MILLISECONDS, like the reaper tick and every peer's mark_orphans.
        registry.open(int(time.time() * 1000))
    except STORE_ERRORS as error:
        registry.close()
        raise RegistryError(
            f"the registry at {path} could not be opened: {type(error).__name__}"
        ) from error
    except BaseException:
        registry.close()
        raise
    # ARMED HERE AND NOWHERE ELSE: this is the supervisor that SERVES a caller,
    # so this is the only one whose terminal phases are that caller's business.
    return Supervisor(
        registry,
        run_root,
        announces_terminal=True,
        sidecar_kind=sidecar_kind,
        owner=owner,
        read_grant=read_grant,
        host_control=host_control,
    )


def tool_result(text: str) -> dict[str, Any]:
    """Package one tool's answer the way the oracle packages it."""
    encoded = text.encode("utf-8")
    if len(encoded) > MAX_MCP_RESPONSE_BYTES:
        raise ResponseTooLarge(len(encoded))
    return {"content": [{"type": "text", "text": text}], "isError": False}


def batch_runs(
    supervisor: Any,
    prompt: str,
    cwd: str,
    providers: Sequence[str],
    read_paths: Sequence[str] | None = None,
    system_prompt: str | None = None,
    *,
    dispatch: Callable[[list[dict[str, Any]]], list[dict[str, Any]]] | None = None,
    idempotency_key: str | None = None,
    group: str | None = None,
) -> dict[str, Any]:
    """Dispatch ONE prompt across several routes, caged, and report both sides.

    The two refusals are not the same failure and must not share an answer.

    A name the roster does not declare is the CALLER's mistake, and it fails
    the whole call BEFORE the first dispatch. A batch that half-runs on a typo
    spends real tokens and hands back a fan-out the caller has to reconcile
    against the list they meant to send.

    A wall, a quota or an absent credential is the WORLD, and it fails only its
    own entry. Refusing the batch for one closed route would throw away every
    route that could still answer, which is the entire point of a fan-out.
    """
    granted_read_paths = child.validate_read_paths(read_paths, read_only=True)
    declared, roster_source = supervisor.declared_providers(cwd)
    unknown = [name for name in providers if name not in declared]
    if unknown:
        raise McpError(
            INVALID_PARAMS,
            f"{PickerError.unknown_provider(unknown[0], sorted(declared))}; "
            f"roster source: {roster_source}",
        )
    if dispatch is None:
        # No stdio door around it: each entry is served by the in-process
        # double, exactly as a McpServer built with this supervisor serves it.
        double = driver.InProcessLauncher(lambda: supervisor, owns_supervisor=False)

        def dispatch(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
            return double.dispatch(
                [
                    driver.make_frame("nspawn_run_start", entry, drivers_dir=None)
                    for entry in entries
                ]
            )

    # Every entry is its own driver; they launch together and their acks are
    # collected under one deadline, so a dead launch refuses only its entry.
    entries = [
        {
            "prompt": prompt,
            "cwd": cwd,
            "provider": name,
            "read_only": True,
            "read_paths": list(granted_read_paths),
            "system_prompt": system_prompt,
            "idempotency_key": None
            if idempotency_key is None
            else f"{idempotency_key}/{index}",
            "group": group,
            # Fingerprinted without the entry's provider (the list and the
            # key's index name it): every `key/*` row carries one batch print.
            "batch": list(providers),
        }
        for index, name in enumerate(providers)
    ]
    if idempotency_key is None or not entries:
        acks = dispatch(entries)
    else:
        # Keyed: entry 0 goes first. A refusal of the KEY — a conflict, or a
        # retryable busy lock or unread fingerprint — says nothing of the
        # provider, so the rest are refused with it instead of interleaving
        # with the batch that holds the key; a wall of the world fails entry 0
        # alone. The driver still compares every `key/*` row (the backstop).
        # Ceiling: a keyed batch pays one admission before the others.
        (head,) = dispatch(entries[:1])
        refusal = head.get("refused")
        if isinstance(refusal, dict) and (
            refusal.get("type") == "IdempotencyConflict"
            or refusal.get("retryable") is True
        ):
            acks = [head] * len(entries)
        else:
            acks = [head, *dispatch(entries[1:])] if entries[1:] else [head]
    runs: list[dict[str, Any]] = []
    refused: list[dict[str, Any]] = []
    for name, ack in zip(providers, acks, strict=True):
        refusal = ack.get("refused")
        if isinstance(refusal, dict):
            entry = {
                "provider": name,
                "reason": refusal.get("message"),
                "type": refusal.get("type"),
            }
            if "launch_uuid" in refusal:
                entry["launch_uuid"] = refusal["launch_uuid"]
            refused.append(entry)
            continue
        runs.append(
            {
                "provider": name,
                "run_id": ack["run_id"],
                "deduplicated": ack.get("deduplicated", False),
                "phase": ack.get("phase"),
            }
        )
    return {"runs": runs, "refused": refused}


#: The one word the ping asks for. Matched with `in`, never `==`: a small
#: fast model answers "PONG." or "Sure! PONG", and both mean the route spoke.
PING_TOKEN = "PONG"
PING_PROMPT = f"Reply with exactly one word: {PING_TOKEN}"

#: TWICE the slowest route that actually ANSWERED on its own fast lane.
#:
#: Measured 2026-09-02 over the two ping rounds on this box, reading
#: `duration_ms` off each run's own terminal frame: on `model_fast`, six routes
#: answered between 2.3 s and 16.9 s (codex 2.3 · agy 2.3 · minimax 2.4 · fugu
#: 4.7 · zai 7.8 · servervllm 16.9) and the m1's `gemma4:e4b` took **96.1 s** —
#: it is monoposto (`OLLAMA_MAX_LOADED_MODELS=1`, `OLLAMA_KEEP_ALIVE=24h`) and
#: pays a cold model load the hosted routes never do.
#:
#: The floor is therefore 96.1 s, not an opinion: any deadline under it turns
#: the one route that DID answer into a refusal, which is precisely the lie
#: this verb exists to stop telling.
DEFAULT_PING_DEADLINE_MS = 180_000

#: An empty balance (402) and a rate limit (429) are the SAME fact for a ping:
#: the account cannot answer right now. Neither is a broken route.
_NO_QUOTA_STATUSES = frozenset({402, 429})


def _ping_row(
    provider: str, code: int, cause: str, run_id: str | None
) -> dict[str, Any]:
    """One route's verdict. `code` IS the D18 answer — never prose to parse."""
    verdict = {0: "green", 2: "refused", 3: "red"}[code]
    return {
        "provider": provider,
        "code": code,
        "verdict": verdict,
        "cause": cause,
        "run_id": run_id,
    }


#: The most runs that one ping follows when its work moves to another account.
MAX_PING_CONTINUATIONS: Final = 32


def _ping_verdict(
    supervisor: Any,
    provider: str,
    run_id: str,
    remaining_ms: int,
    deadline_ms: int,
    deadline: float,
) -> dict[str, Any]:
    """Read one dispatched ping to its D18 code.

    The order is the point. A route still running when the deadline passes is
    REFUSED and named as such — the m1 is monoposto
    (`OLLAMA_MAX_LOADED_MODELS=1`, `OLLAMA_KEEP_ALIVE=24h`), so a loaded model
    holds the slot for 24 hours and a request for another one never starts:
    silent, zero tokens, and indistinguishable from dead to anything that does
    not name the deadline.

    Quota is read from `api_error_status`, the INTEGER — `failure_reason` is
    free-form prose and substring-matching it is a guess wearing a
    classification's clothes.
    """
    status = supervisor.status(run_id, timeout_ms=remaining_ms)
    # A route's other account can take the ping's work: read the run that did.
    for _ in range(MAX_PING_CONTINUATIONS):
        successor = (
            notify.continued_in(status.warning)
            if status.phase is RunPhase.FAILED
            else None
        )
        if successor is None:
            break
        run_id = successor
        status = supervisor.status(
            run_id, timeout_ms=max(1, int((deadline - time.monotonic()) * 1000))
        )
    expired = time.monotonic() >= deadline
    if not status.phase.is_terminal():
        return _ping_row(
            provider,
            2,
            f"still running when the ping's {deadline_ms} ms deadline expired",
            run_id,
        )
    if expired and status.phase is RunPhase.FAILED:
        # The run's own `timeout_ms` and the ping's deadline are the SAME
        # instant, so either can win the race: the poll times out (above), or
        # `RunDeadlineExceeded` kills the child first and the phase is FAILED.
        # One fact, and it must read the same both ways. Judged by OUR clock,
        # never by the reason string. A transport that broke at the exact
        # deadline reads as a refusal too — an honest tie, and the safe side of
        # it: a refusal costs a re-ping, a false red costs a route.
        return _ping_row(
            provider,
            2,
            f"the ping's {deadline_ms} ms deadline expired and the run was signalled",
            run_id,
        )
    if status.phase is RunPhase.COMPLETED:
        text = supervisor.result(run_id).final_text or ""
        if PING_TOKEN in text:
            return _ping_row(provider, 0, "answered", run_id)
        return _ping_row(provider, 3, f"answered without {PING_TOKEN!r}", run_id)
    if status.api_error_status in _NO_QUOTA_STATUSES:
        return _ping_row(
            provider,
            2,
            f"no quota to answer with (HTTP {status.api_error_status})",
            run_id,
        )
    if status.rate_limited:
        # Quota's other wire. The native vendor rejects on `rate_limit_event`
        # and sends NO http status, so watching only `api_error_status` read a
        # five-hour ceiling as a broken route.
        return _ping_row(provider, 2, "the vendor rejected on rate limit", run_id)
    return _ping_row(
        provider,
        3,
        status.failure_reason or f"the run ended {status.phase.value}",
        run_id,
    )


def ping_routes(
    supervisor: Any,
    cwd: str,
    providers: Sequence[str] | None = None,
    deadline_ms: int = DEFAULT_PING_DEADLINE_MS,
) -> dict[str, Any]:
    """Ping every declared route on its own `model_fast` and answer PER ROUTE.

    THE VERDICT IS D18 AND IT IS PER ROUTE, never for the call: `0` the route
    answered, `2` a typed refusal that is NOT a failure — no quota, no
    credential, no declared model, or the deadline — and `3` only behaviour,
    a wrong answer or a broken transport. A route with an empty balance must
    not redden the ping; a detector that shouts about the owner's wallet stops
    being read.

    THIS ONE BLOCKS, and `nspawn_batch_start` deliberately does not. Batch is a
    minutes-long fan-out whose terminal phase reaches the caller by notice, so
    there is nothing to wait for. A ping's whole product IS the synchronous
    verdict — "ask me later" is just `nspawn_batch_start` with a different prompt.
    The wait is bounded by ONE deadline taken once: N routes under a 60 s
    deadline are 60 s, not N × 60 s, the same correction `_drive_run` already
    made for a run's own accounts.
    """
    if deadline_ms <= 0:
        raise McpError(
            INVALID_PARAMS,
            "nspawn_route_ping needs a positive deadline_ms: with no deadline a BUSY "
            "route reads exactly like a dead one, which is the confusion this "
            "verb exists to undo",
        )
    fast = supervisor.fast_models(cwd)
    names = list(providers) if providers is not None else sorted(fast)
    unknown = [name for name in names if name not in fast]
    if unknown:
        # Same split as `batch_runs`: a typo is the CALLER's and kills the whole
        # call before anything is dispatched; a closed route is the WORLD's and
        # refuses only its own row.
        raise McpError(
            INVALID_PARAMS,
            f"the roster at {cwd!r} declares no provider named "
            f"{unknown[0]!r}; `nspawn_provider_list` lists the live set",
        )
    deadline = time.monotonic() + deadline_ms / 1000
    rows: list[dict[str, Any]] = []
    in_flight: list[tuple[str, str]] = []
    for name in names:
        remaining_ms = int((deadline - time.monotonic()) * 1000)
        if remaining_ms <= 0:
            # `0` is not "immediately" downstream — `supervisor.py` reads
            # `deadline_ms <= 0` as NO deadline and waits forever. Dispatching
            # here would hand the last route an unbounded run, the exact
            # opposite of the absolute deadline this loop enforces.
            rows.append(
                _ping_row(
                    name,
                    2,
                    f"the ping's {deadline_ms} ms deadline expired before this "
                    "route's turn to dispatch",
                    None,
                )
            )
            continue
        try:
            accepted = supervisor.execute(
                PING_PROMPT,
                cwd,
                provider=name,
                # The route's OWN fast lane. `None` is not a branch here: the
                # picker already owns "this route declares no model", and a
                # second opinion in the ping would be a second answer to one
                # question.
                preferred_model=fast[name],
                read_only=True,
                # The deadline is ABSOLUTE, so a run takes what REMAINS of
                # it — never the full budget over again. Handing every route
                # `deadline_ms` let the last-dispatched child outlive the
                # verdict by however long the earlier dispatches took, burning
                # tokens after the caller already had its answer.
                timeout_ms=remaining_ms,
            )
        except NSpawnError as error:
            # `.entry` is NOT guaranteed: subclasses that define their own
            # `__init__` (`VendorHomeError` among them) never set it. Reading
            # it blind kills the whole ping on the first such refusal and
            # takes its siblings down with it. Same `getattr`
            # this module already uses at the JSON-RPC boundary below.
            entry = getattr(error, "entry", None)
            cause = str(error) if entry is None else f"nERR-{entry.code_str()}: {error}"
            rows.append(_ping_row(name, 2, cause, None))
            continue
        in_flight.append((name, accepted.run_id))
    for name, run_id in in_flight:
        remaining = max(0, int((deadline - time.monotonic()) * 1000))
        rows.append(
            _ping_verdict(supervisor, name, run_id, remaining, deadline_ms, deadline)
        )
    return {"deadline_ms": deadline_ms, "routes": rows}


#: Draft 2020-12, except that an `integer` is a Python int and never a bool or `5.0`.
_SCHEMA_VALIDATOR = validators.extend(
    Draft202012Validator,
    type_checker=Draft202012Validator.TYPE_CHECKER.redefine(
        "integer", lambda _checker, value: type(value) is int
    ),
)


def validate_arguments(schema: dict[str, Any], arguments: dict[str, Any]) -> None:
    """Enforce one declared contract, its shape closed, with `jsonschema`.

    A refusal names the argument and the rule it broke, never the value: a prompt
    or a system prompt is not echoed back into an error.
    """
    closed = {**schema, "additionalProperties": False}
    error = best_match(_SCHEMA_VALIDATOR(closed).iter_errors(arguments))
    if error is not None:
        if error.validator in ("required", "additionalProperties"):
            raise McpError(INVALID_PARAMS, error.message)
        where = "".join(
            f"[{part}]" if isinstance(part, int) else f".{part}"
            for part in error.absolute_path
        ).lstrip(".")
        raise McpError(
            INVALID_PARAMS,
            f"argument {where!r} must satisfy {error.validator}: "
            f"{error.validator_value!r}",
        )
    alternatives = schema.get(EXACTLY_ONE_OF)
    if alternatives is not None:
        given = [name for name in alternatives if name in arguments]
        if len(given) != 1:
            raise McpError(
                INVALID_PARAMS,
                f"exactly one of {' or '.join(alternatives)} is required, got {given}",
            )


def _wire_kwargs(name: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Map validated wire arguments onto one `nspawn_*` method's kwarg names."""
    kwargs = dict(payload)
    if name == "nspawn_run_start" and "class" in kwargs:
        kwargs["execution_class"] = kwargs.pop("class")
    return kwargs


def _to_jsonable(value: Any) -> Any:
    """Render an L7 result — nested dataclasses, enums, tuples — as plain JSON."""
    if is_dataclass(value):
        return {
            field.name: _to_jsonable(getattr(value, field.name))
            for field in fields(value)
            if not (
                isinstance(value, RunResult)
                and field.name
                in {"failure_reason", "structured_output", "phase", "alert"}
                and getattr(value, field.name) is None
                or isinstance(value, RunStatus)
                and field.name == "inference_profile"
                and value.inference_profile is None
            )
        }
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (tuple, list)):
        return [_to_jsonable(item) for item in value]
    return value


def _descriptor_path(fd: int) -> Path:
    """The path the kernel holds for an open directory descriptor."""
    if sys.platform == "darwin":
        raw = fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024))
        return Path(os.fsdecode(raw.split(b"\0", 1)[0]))
    return Path(os.readlink(f"/proc/self/fd/{fd}"))


def _bounded_result(
    rendered: dict[str, Any], root_fd: int | None = None
) -> dict[str, Any]:
    """Keep small JSON inline; persist the complete bytes of a large answer."""
    payload = json.dumps(rendered, separators=(",", ":"))
    encoded = payload.encode("utf-8")
    if len(encoded) > MAX_RESPONSE_BYTES:
        if not isinstance(root_fd, int):
            raise McpError(
                INTERNAL_ERROR, "result spill failed: missing run-root descriptor"
            )
        artifact: Path | None = None
        try:
            run_id = rendered.get("run_id")
            child_name: str
            if run_id is None:
                child_name = uuid.uuid4().hex
                os.mkdir(child_name, 0o700, dir_fd=root_fd)
            else:
                if (
                    not isinstance(run_id, str)
                    or not run_id
                    or "/" in run_id
                    or run_id in (".", "..")
                    or "\x00" in run_id
                ):
                    raise McpError(INTERNAL_ERROR, "unsafe spill run_id")
                child_name = run_id
            expected_root_stat = os.fstat(root_fd)
            try:
                directory_fd = os.open(
                    child_name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=root_fd,
                )
            except OSError as error:
                if error.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise McpError(
                        INTERNAL_ERROR, "spill run directory is a symlink"
                    ) from error
                raise
            try:
                expected_dir_stat = os.fstat(directory_fd)
                name = f"mcp-result-{uuid.uuid4().hex}.json"
                try:
                    file_fd = os.open(
                        name,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o600,
                        dir_fd=directory_fd,
                    )
                    with os.fdopen(file_fd, "wb") as stream:
                        created = os.fstat(stream.fileno())
                        stream.write(encoded)
                    artifact = _descriptor_path(directory_fd) / name
                    if (
                        not os.path.samestat(os.fstat(directory_fd), expected_dir_stat)
                        or not os.path.samestat(os.fstat(root_fd), expected_root_stat)
                        or not os.path.samestat(
                            os.fstat(directory_fd),
                            os.stat(
                                child_name,
                                dir_fd=root_fd,
                                follow_symlinks=False,
                            ),
                        )
                        or not os.path.samestat(
                            created,
                            os.stat(name, dir_fd=directory_fd, follow_symlinks=False),
                        )
                    ):
                        raise McpError(
                            INTERNAL_ERROR, "spill directory or file changed"
                        )
                except OSError, McpError:
                    if artifact is not None:
                        try:
                            if os.path.samestat(
                                created,
                                os.stat(
                                    name,
                                    dir_fd=directory_fd,
                                    follow_symlinks=False,
                                ),
                            ):
                                os.unlink(name, dir_fd=directory_fd)
                        except OSError:
                            # Never publish an incomplete or replaced artifact.
                            pass
                    raise
            finally:
                os.close(directory_fd)
            payload = json.dumps(
                {
                    "preview": encoded[:MAX_RESPONSE_BYTES].decode("ascii"),
                    "path": str(artifact),
                    "bytes": len(encoded),
                },
                separators=(",", ":"),
            )
        except McpError:
            raise
        except (OSError, NSpawnError) as error:
            raise McpError(
                INTERNAL_ERROR, f"result spill failed: {type(error).__name__}"
            ) from error
    return {"content": [{"type": "text", "text": payload}], "isError": False}


@dataclass
class McpServer:
    """One MCP session's tool surface."""

    #: Injected for tests; the live door leaves this `None` and lazily builds
    #: one from environment on the first real tool dispatch.
    supervisor: Supervisor | None = None
    #: Who serves a dispatch. FIXED AT CONSTRUCTION and never re-derived from
    #: `supervisor`, which `_resolve_supervisor` fills lazily: built with an
    #: injected supervisor -> the in-process double on that supervisor; built
    #: without -> one scoped driver process per call.
    launcher: driver.Launcher | None = None
    #: Set by the trusted host at construction. Tool arguments cannot grant it.
    host_control: bool = False
    #: A long-poll resolves outside the door's lock (`main.build`).
    _resolving: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if self.launcher is None:
            injected = self.supervisor
            self.launcher = (
                driver.default_launcher(host_control=self.host_control)
                if injected is None
                else driver.InProcessLauncher(lambda: injected, owns_supervisor=False)
            )

    def close(self) -> None:
        """Stop the reaper and drop the instance row for this session.

        `main.serve` calls this in `finally`, so a peer disconnect or a
        tool crash does not leak a heartbeat thread, an open store
        connection, or a phantom instance row. A test double without
        `supervisor` is a no-op; the live door has one and stops it.
        """
        supervisor = self.supervisor
        if supervisor is not None:
            try:
                supervisor.shutdown()
            finally:
                self.supervisor = None

    def _resolve_supervisor(self) -> Supervisor:
        with self._resolving:
            if self.supervisor is None:
                self.supervisor = _default_supervisor(host_control=self.host_control)
            return self.supervisor

    def nspawn_run_start(
        self,
        prompt: str | None = None,
        cwd: str = "",
        *,
        provider: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        execution_class: str | None = None,
        session_id: str | None = None,
        timeout_ms: int = 0,
        read_only: bool = False,
        read_paths: Sequence[str] | None = None,
        system_prompt: str | None = None,
        idempotency_key: str | None = None,
        group: str | None = None,
        swarm: str | None = None,
        native_write: bool = False,
        orchestrator: dict[str, Any] | None = None,
        inference_profile: dict[str, Any] | None = None,
        original_run: str | None = None,
        activation: dict[str, Any] | None = None,
        session_operation: dict[str, Any] | None = None,
        command: dict[str, Any] | None = None,
        agent: str | None = None,
        producer: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """L6 maps wire key ``class`` to ``execution_class`` before dispatch."""
        if (
            activation is not None or session_operation is not None
        ) and not self.host_control:
            raise McpError(INVALID_PARAMS, HOST_CONTROL_REFUSAL)
        system_prompt = _agent_prompt(agent, system_prompt, cwd)
        if read_paths is not None and not (read_only or native_write):
            raise McpError(
                INVALID_PARAMS, "read_paths requires read_only=true or native_write"
            )
        if orchestrator is not None and not read_only:
            raise McpError(INVALID_PARAMS, "orchestrator requires read_only=true")
        if (
            timeout_ms <= 0
            and original_run is None
            and activation is None
            and driver.node_owner(os.environ) is not None
        ):
            # A node's front outlives the turn that sent it: nobody is left to
            # stop a run with no deadline.
            raise McpError(
                INVALID_PARAMS, "a node's nspawn_run_start requires timeout_ms > 0"
            )
        # Invalid input, refused as invalid input: a coordinate that is too long,
        # empty, blank, not printable, not an absolute canonical path or not even
        # a string never becomes a dispatch, so no launch and no store write
        # happens for it. The rule is the binding's own
        # (`native.OrchestratorBinding.request_refusal`), so this door and the
        # admission cannot drift apart.
        if orchestrator is not None and (
            refusal := native.OrchestratorBinding.request_refusal(orchestrator)
        ):
            raise McpError(INVALID_PARAMS, refusal)
        _refuse_slashed(idempotency_key)
        group = _swarm_group(group, swarm)
        arguments: dict[str, Any] = {
            "prompt": prompt,
            "cwd": cwd,
            "provider": provider,
            "model": model,
            "effort": effort,
            "execution_class": execution_class,
            "session_id": session_id,
            "timeout_ms": timeout_ms,
            "read_only": read_only,
            "read_paths": None if read_paths is None else list(read_paths),
            "system_prompt": system_prompt,
            "idempotency_key": idempotency_key,
            "group": group,
        }
        # Only when set, so a dispatch without it reaches `execute` as before.
        if native_write:
            arguments["native_write"] = True
        if producer is not None:
            arguments["producer"] = list(producer)
        if orchestrator is not None:
            arguments["orchestrator"] = orchestrator
        if inference_profile is not None:
            arguments["inference_profile"] = inference_profile
        if original_run is not None:
            arguments["original_run"] = original_run
        if activation is not None:
            arguments["activation"] = activation
        if session_operation is not None:
            arguments["session_operation"] = session_operation
        if command is not None:
            arguments["command"] = command
        return self._dispatched("nspawn_run_start", arguments)

    def _drivers_dir(self, supervisor: Supervisor) -> Path | None:
        run_root = getattr(supervisor, "_run_root_path", None)
        return driver.drivers_dir(run_root) if isinstance(run_root, Path) else None

    def _launch(self, tool: str, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Each entry to its own driver; the answers are the drivers' acks."""
        supervisor = self._resolve_supervisor()
        launcher = self.launcher
        assert launcher is not None  # fixed by __post_init__
        directory = self._drivers_dir(supervisor)
        return launcher.dispatch(
            [
                driver.make_frame(tool, entry, drivers_dir=directory)
                for entry in entries
            ],
            supervisor,
        )

    def _dispatched(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        (ack,) = self._launch(tool, [arguments])
        return _acknowledged(tool, ack)

    def nspawn_batch_start(
        self,
        prompt: str,
        cwd: str,
        providers: Sequence[str],
        read_paths: Sequence[str] | None = None,
        system_prompt: str | None = None,
        idempotency_key: str | None = None,
        group: str | None = None,
    ) -> dict[str, Any]:
        _refuse_slashed(idempotency_key)
        group = _swarm_group(group, None)
        return batch_runs(
            self._resolve_supervisor(),
            prompt,
            cwd,
            providers,
            read_paths=read_paths,
            system_prompt=system_prompt,
            dispatch=lambda entries: self._launch("nspawn_run_start", entries),
            idempotency_key=idempotency_key,
            group=group,
        )

    def nspawn_route_ping(
        self,
        cwd: str,
        providers: Sequence[str] | None = None,
        deadline_ms: int = DEFAULT_PING_DEADLINE_MS,
    ) -> dict[str, Any]:
        return ping_routes(
            self._resolve_supervisor(), cwd, providers, deadline_ms=deadline_ms
        )

    def nspawn_review_start(
        self,
        branch: str | None = None,
        cwd: str = "",
        *,
        base: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        prompt: str | None = None,
        read_paths: Sequence[str] | None = None,
        system_prompt: str | None = None,
        agent: str | None = None,
        idempotency_key: str | None = None,
        group: str | None = None,
        producer: Sequence[str] | None = None,
        swarm: str | None = None,
    ) -> dict[str, Any]:
        _refuse_slashed(idempotency_key)
        group = _swarm_group(group, swarm)
        arguments: dict[str, Any] = {
            "branch": _review_branch(branch, cwd),
            "cwd": cwd,
            "base": base,
            "provider": provider,
            "model": model,
            "effort": effort,
            "prompt": prompt,
            "read_paths": None if read_paths is None else list(read_paths),
            "system_prompt": _agent_prompt(agent, system_prompt, cwd),
            "idempotency_key": idempotency_key,
            "group": group,
        }
        # Only when set, so a review without it reaches `review` as before.
        if producer is not None:
            arguments["producer"] = list(producer)
        return self._dispatched("nspawn_review_start", arguments)

    def nspawn_run_list(self) -> FleetPage:
        """The whole fleet. It takes no timeout: a listing never long-polls."""
        # DECLARED, NOT YET BUILT — see the `FleetPage` import above.
        page: FleetPage = self._resolve_supervisor().fleet()
        return page

    def nspawn_run_show(
        self,
        run_id: str | None = None,
        timeout_ms: int | None = None,
        launch_uuid: str | None = None,
        idempotency_key: str | None = None,
        request: dict[str, Any] | None = None,
    ) -> RunStatus | dict[str, Any]:
        """One run, by its run_id, by the launch_uuid a pending dispatch answered
        with, or by the idempotency key of a keyed dispatch. A positive timeout
        long-polls."""
        supervisor = self._resolve_supervisor()
        if idempotency_key is not None:
            if timeout_ms is not None:
                raise McpError(
                    INVALID_PARAMS, "an idempotency_key lookup takes no timeout_ms"
                )
            return _keyed_lookup(supervisor, idempotency_key, request)
        if request is not None:
            raise McpError(INVALID_PARAMS, "request is read only with idempotency_key")
        if launch_uuid is not None:
            if run_id is not None:
                raise McpError(
                    INVALID_PARAMS,
                    "nspawn_run_show takes run_id or launch_uuid, not both",
                )
            ack = driver.resolve_launch(
                self._drivers_dir(supervisor), launch_uuid, supervisor.run_of_launch
            )
            run_id = str(_acknowledged("nspawn_run_show", ack)["run_id"])
        if run_id is None:
            raise McpError(
                INVALID_PARAMS, "nspawn_run_show needs run_id or launch_uuid"
            )
        return _status_show(supervisor, run_id, timeout_ms=timeout_ms or 0)

    def nspawn_result_show(self, run_id: str, timeout_ms: int = 0) -> RunResult:
        supervisor = self._resolve_supervisor()
        if timeout_ms > 0:
            # The wait follows the work to the run that serves it.
            return supervisor.result(run_id, timeout_ms=timeout_ms)
        return supervisor.result(run_id)

    def nspawn_transcript_show(
        self,
        run_id: str,
        since_seq: int = 0,
        max_bytes: int | None = None,
        frame_types: Sequence[str] | None = None,
    ) -> TranscriptPage:
        return self._resolve_supervisor().transcript(
            run_id,
            since_seq=since_seq,
            max_bytes=max_bytes,
            frame_types=frame_types,
        )

    def nspawn_run_close(
        self,
        run_id: str | None = None,
        idempotency_key: str | None = None,
        request: dict[str, Any] | None = None,
        only_unadmitted: bool | None = None,
    ) -> CloseReadback | dict[str, Any]:
        """One run by its run_id, or the claim of a keyed dispatch released."""
        if only_unadmitted is not None and (
            type(only_unadmitted) is not bool
            or idempotency_key is None
            or run_id is not None
        ):
            raise McpError(
                INVALID_PARAMS, "only_unadmitted is read only with idempotency_key"
            )
        supervisor = self._resolve_supervisor()
        if idempotency_key is not None:
            return _keyed_release(
                supervisor, idempotency_key, request, only_unadmitted is True
            )
        if request is not None or run_id is None:
            raise McpError(INVALID_PARAMS, "request is read only with idempotency_key")
        return supervisor.close(run_id)

    def nspawn_usage_show(self, timeZone: str) -> dict[str, Any]:
        try:
            ZoneInfo(timeZone)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise McpError(
                INVALID_PARAMS, "timeZone must identify an IANA time zone"
            ) from error
        return self._resolve_supervisor().usage_show(timeZone)

    def nspawn_quota_show(
        self, provider: str | None = None, refresh: int = 0
    ) -> QuotaReport:
        """Zero required arguments: the default answer is the whole roster.

        Explicit refresh reads native Codex OAuth account metadata without a
        model turn. Other accounts answer from what their runs' wire recorded.
        Refresh zero uses only durable observations; active walls skip pulls.
        """
        report: QuotaReport = self._resolve_supervisor().quota(
            provider=provider, refresh=refresh
        )
        return report

    def nspawn_provider_list(self, provider: str | None = None) -> ProviderReport:
        """The roster read, and THE ONE VERB THAT NEVER OPENS THE STORE.

        Every other verb resolves a live `Supervisor`, and building one connects
        the registry. Measured 2026-08-28: with the registry
        unreachable, `nspawn_quota_show` answers "the registry ... could
        not be opened". The caller here is asking WHERE IT MAY DISPATCH — often
        precisely because something is broken — so an injected supervisor is
        used when the session holds one and the roster is read directly when it
        does not. Neither arm touches the registry, and the two answer alike.
        """
        if self.supervisor is not None:
            return self.supervisor.providers(provider=provider)
        return provider_report(provider)

    def _typed_question(
        self, kind: str, state: str, questions: Any, model: str | None = None
    ) -> dict[str, Any]:
        """One of the three typed verbs, against System1.

        THE CALLER DOES NOT CHOOSE THE ENDPOINT OR CREDENTIAL. The endpoint comes from
        the process configuration and the key from the environment; an endpoint chosen by the
        caller is exfiltration by another name, and a key passed as an argument ends up
        in the trace of every run.

        Unavailability is OPERATIONAL and bubbles up as a typed refusal. Whoever catches it
        cannot read it as "the answer is no": there was no answer at all.
        """
        from . import system1

        if not isinstance(questions, dict) or not questions:
            raise McpError(
                -32602,
                f"{kind}: `questions` must be an object with at least one question",
            )
        for ident, pergunta in questions.items():
            if isinstance(pergunta, str):
                continue
            if not isinstance(pergunta, dict) or not pergunta.get("instructions"):
                raise McpError(
                    -32602,
                    f"{kind}: question {ident!r} needs non-empty `instructions`",
                )
        try:
            respostas = getattr(system1, kind)(state, questions, model)
        except system1.System1Unavailable as erro:
            raise McpError(-32002, f"{kind}: {erro}") from None
        return {
            "answers": {
                ident: {
                    k: v
                    for k, v in (
                        ("type", r.type),
                        ("value", r.value),
                        ("probabilities", r.probabilities),
                        ("confidence", r.confidence),
                        ("legend", r.legend),
                    )
                    if v is not None
                }
                for ident, r in respostas.items()
            }
        }

    def nspawn_judgment_estimate(
        self, state: str, questions: Any, model: str | None = None
    ) -> dict[str, Any]:
        return self._typed_question("noul", state, questions, model)

    def nspawn_judgment_choose(
        self, state: str, questions: Any, model: str | None = None
    ) -> dict[str, Any]:
        return self._typed_question("choice", state, questions, model)

    def nspawn_judgment_score(
        self, state: str, questions: Any, model: str | None = None
    ) -> dict[str, Any]:
        return self._typed_question("score", state, questions, model)

    def call_tool(self, name: str, arguments: Any) -> dict[str, Any]:
        """Run one of the replacement tools.

        Arguments are validated against the tool's declared schema before the
        tool runs. A typed L7 refusal (`NSpawnError`, but not already an `McpError`) is
        wrapped so it renders as one; an `McpError` — including
        `SupervisorUnconfigured` — passes through unchanged.
        """
        try:
            size = len(
                json.dumps(
                    {"name": name, "arguments": arguments},
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode("utf-8")
            )
        except UnicodeEncodeError as error:
            raise McpError(
                INVALID_PARAMS, "tools/call params must be valid UTF-8"
            ) from error
        if size > MAX_MCP_PARAMS_BYTES:
            raise McpError(
                INVALID_PARAMS,
                f"tools/call params of {size} bytes exceeded the "
                f"{MAX_MCP_PARAMS_BYTES}-byte ceiling",
            )
        contract = NSPAWN_TOOL_BY_NAME.get(name)
        if contract is None:
            raise McpError(INVALID_PARAMS, f"nSPAWN has no tool named {name!r}")
        if arguments is not None and not isinstance(arguments, dict):
            raise McpError(INVALID_PARAMS, "tool arguments must be an object")
        payload = arguments if isinstance(arguments, dict) else {}
        validate_arguments(contract.input_schema, payload)
        system_prompt = payload.get("system_prompt")
        if system_prompt is not None:
            try:
                validate_system_prompt(system_prompt)
            except ValueError as error:
                raise McpError(INVALID_PARAMS, str(error)) from error
        kwargs = _wire_kwargs(name, payload)
        try:
            result = getattr(self, name)(**kwargs)
        except McpError:
            raise
        except NSpawnError as error:
            # CODEX round-2 blocker 7: an NSpawnError here is a TYPED refusal
            # (`-32602` invalid params), not an internal error (`-32603`).
            # Roster/path/model/effort refusals all carry a numeric code
            # and a wire message; the caller needs the code, not a stack
            # trace. Some NERR codes (1104) are operator-side; the boundary
            # is still the caller's input from the wire's perspective.
            #
            # BUT ONLY TYPED ONES. The previous version of this line mapped EVERY
            # NSpawnError to -32602, and the rationale above does not cover
            # that scope: it refers to refusals that "carry a numeric
            # code", and an `UNKNOWN` (9999) is precisely the absence of that —
            # an opaque L7 failure, which in JSON-RPC semantics belongs to the server, not the
            # caller. Measured 2026-08-31 on clean main: five tests in
            # tests/test_mcp_l6.py and tests/test_mcp.py failed with
            # `assert -32602 == -32603`, all constructing NSpawnError(UNKNOWN).
            # The distinction honours the blocker AND the tests; neither was touched.
            wire_code = (
                INVALID_PARAMS
                # `getattr(error, "entry", ...)`, not `getattr(error.entry, ...)`.
                # The second form evaluates `error.entry` BEFORE `getattr`
                # runs, so the default protected `code` on `entry` and never
                # `entry` on `error`. Measured: 37 of the 51 subclasses of
                # `NSpawnError` call `Exception.__init__` directly and never set
                # `.entry` — all raised `AttributeError` INSIDE
                # `except NSpawnError`, which nobody catches higher up (`handle` only
                # catches `McpError`, `serve_one` only `ValueError`, `serve_stream`
                # only `OSError`/`FrameError`), so it escaped to the serving
                # thread and killed the stdio port without any frame. In `main`
                # these paths returned a poorly encoded but VALID -32602 frame:
                # a wrong code turned into a dead port.
                if getattr(getattr(error, "entry", None), "code", UNKNOWN_CODE)
                != UNKNOWN_CODE
                else INTERNAL_ERROR
            )
            # Typed as `_acknowledged` types a driver's refusal: without it a
            # caller cannot tell a forgotten launch from a transient failure.
            nerr = nerr_of(error)
            raise McpError(
                wire_code,
                str(error),
                data={
                    "tool": name,
                    **(getattr(error, "data", None) or {}),
                    "type": type(error).__name__,
                    **({} if nerr is None else {"nerr": nerr}),
                },
            ) from error
        except STORE_ERRORS as error:
            # The store failed under a tool (a busy write lock, an I/O error):
            # the server's failure, not the caller's, and typed like any other.
            # Uncaught, it escaped `handle` and the port answered nothing more.
            raise McpError(
                INTERNAL_ERROR,
                f"the nSPAWN store failed during {name}: {error}",
                data={"tool": name, "type": type(error).__name__},
            ) from error
        return _bounded_result(
            _to_jsonable(result), getattr(self.supervisor, "_run_root", None)
        )


#: The keyed dispatches `nspawn_run_show` finds by key.
_KEYED_TOOLS: Final = ("nspawn_run_start", "nspawn_review_start")


def _status_show(
    supervisor: Supervisor, run_id: str, *, timeout_ms: int = 0
) -> dict[str, Any]:
    """Render C5 from the same registry that supplied this execution's status."""
    status: dict[str, Any] = _to_jsonable(
        supervisor.status(run_id, timeout_ms=timeout_ms)
    )
    registry = getattr(supervisor, "_registry", None)
    # A status-only injected supervisor has no store observation. Production
    # supervisors always carry their registry; store failures propagate.
    if registry is None:
        return {**status, "admission": None, "terminal_receipt": None}
    with supervisor._lock:
        return {
            **status,
            "admission": registry.admission_show(run_id),
            "terminal_receipt": registry.terminal_receipt_show(run_id),
        }


def _keyed_lookup(
    supervisor: Supervisor, key: str, request: dict[str, Any] | None
) -> dict[str, Any]:
    """The run a keyed dispatch admitted, found by recomputing its fingerprint
    in the calling project's key space. It starts, reattaches and notifies
    nothing; an unreadable store is an error, never `missing`."""
    project, fingerprint = _keyed(key, request)
    found = supervisor.find_key(project, key)
    if found is None:
        return {"lookup": "missing", "idempotency_key": key}
    if found.fingerprint != fingerprint:
        return {"lookup": "conflict", "run_id": found.run_id}
    status = _status_show(supervisor, found.run_id)
    return {**status, "lookup": "found"}


def _keyed_release(
    supervisor: Supervisor,
    key: str,
    request: dict[str, Any] | None,
    only_unadmitted: bool = False,
) -> dict[str, Any]:
    """A keyed dispatch's claim released: the key is read under its admission
    lock, and fenced when it names no run, so a driver of it that has not
    admitted yet never will. Unless `only_unadmitted`, a matching run is closed
    and read back; the guarded form returns it without changing custody."""
    project, fingerprint = _keyed(key, request)
    found = supervisor.release_key(project, key, driver.LOCK_TIMEOUT_S)
    guard = {"only_unadmitted": True} if only_unadmitted else {}
    if found is None:
        return {"lookup": "fenced", "idempotency_key": key, **guard}
    if found.fingerprint != fingerprint:
        return {"lookup": "conflict", "run_id": found.run_id, **guard}
    if only_unadmitted:
        status: dict[str, Any] = _to_jsonable(supervisor.status(found.run_id))
        return {**status, "lookup": "found", **guard}
    closed: dict[str, Any] = _to_jsonable(supervisor.close(found.run_id))
    return {**closed, "lookup": "found"}


def _keyed(key: str, request: dict[str, Any] | None) -> tuple[str, str]:
    """The project and fingerprint of the keyed dispatch `request` repeats."""
    _refuse_slashed(key)
    if (
        request is None
        or set(request) != {"tool", "arguments"}
        or request["tool"] not in _KEYED_TOOLS
        or not isinstance(request["arguments"], dict)
    ):
        raise McpError(
            INVALID_PARAMS,
            "request must be {tool: nspawn_run_start or nspawn_review_start, "
            "arguments: {...}}",
        )
    tool, arguments = request["tool"], request["arguments"]
    if "idempotency_key" in arguments:
        raise McpError(
            INVALID_PARAMS,
            "request.arguments leaves idempotency_key out; it is the selector",
        )
    validate_arguments(NSPAWN_TOOL_BY_NAME[tool].input_schema, arguments)
    kwargs = _wire_kwargs(tool, arguments)
    group = _swarm_group(kwargs.pop("group", None), kwargs.pop("swarm", None))
    # The start verbs fingerprint the resolved request, so the lookup does too.
    cwd = str(kwargs.get("cwd", ""))
    kwargs["system_prompt"] = _agent_prompt(
        kwargs.pop("agent", None), kwargs.get("system_prompt"), cwd
    )
    if tool == "nspawn_review_start":
        kwargs["branch"] = _review_branch(kwargs.get("branch"), cwd)
    fingerprint = driver.request_fingerprint(tool, kwargs, group=group, strict=True)
    return driver._caller_project(kwargs), fingerprint


def _agent_prompt(agent: str | None, system_prompt: str | None, cwd: str) -> str | None:
    """The system prompt of a start verb: the text given, or the named agent's body."""
    if agent is None:
        return system_prompt
    if system_prompt is not None:
        raise McpError(INVALID_PARAMS, "give agent or system_prompt, not both")
    try:
        return agent_system_prompt(agent, cwd)
    except InvalidRunRequest as error:
        raise McpError(INVALID_PARAMS, str(error)) from error


def _review_branch(branch: str | None, cwd: str) -> str:
    """The branch a review names, else the branch checked out in cwd."""
    if branch is not None:
        return branch
    try:
        return review_branch(cwd)
    except InvalidRunRequest as error:
        raise McpError(INVALID_PARAMS, str(error)) from error


def _refuse_slashed(idempotency_key: str | None) -> None:
    """A caller key never holds `/`: `key/index` is how a batch keys its entries."""
    if idempotency_key is not None and "/" in idempotency_key:
        raise McpError(
            INVALID_PARAMS,
            "idempotency_key may not contain '/': a batch keys its entries key/index",
        )


def _acknowledged(tool: str, ack: dict[str, Any]) -> dict[str, Any]:
    """A driver's ack as the tool's answer, or its refusal as a typed error."""
    refused = ack.get("refused")
    if not isinstance(refused, dict):
        return ack
    data: dict[str, Any] = {"tool": tool, "type": refused.get("type")}
    data.update(
        (name, value)
        for name, value in refused.items()
        if name not in ("type", "message", "code")
    )
    for name in ("run_id", "phase", "ack_late"):
        if name in ack:
            data[name] = ack[name]
    code = refused.get("code")
    raise McpError(
        code if isinstance(code, int) else INTERNAL_ERROR,
        str(refused.get("message")),
        data=data,
    )
