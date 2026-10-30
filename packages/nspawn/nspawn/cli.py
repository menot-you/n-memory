"""The one `nspawn` executable: `nspawn <noun> <verb> [arguments]`.

`mcp serve` serves the MCP surface on stdio. Clients reach it through the release
link `tools/mcp.sh` publishes, whose launcher runs `nspawn mcp serve`; a
bare `nspawn` is a usage error. Every other piece of the member is a verb of this
executable, never a second executable (`rails/organization.toml`).

`COMMANDS` is the only list of commands. `tools/organization.py` reads its keys by AST
against the declared grammar, the help is rendered from it, and every handler is a
`cmd_<noun>_<verb>` bound in it exactly once (`tests/test_cli.py`). A spelling that
is not in the table is an unknown command: there are no aliases.
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, NamedTuple

import nott_database
from nott_system1.renamed import RENAMED as SYSTEM1_RENAMED
from nott_system1.renamed import RenamedVariableError as System1RenamedVariableError
from nott_system1.renamed import refuse as system1_refuse

from . import driver, install, notify, route, steer
from .claude_catalog import ModelCatalogError
from .client import NspawnClient, NspawnClientError, NspawnRefused, StdioServer
from .custody import ControllerRefusal
from .errors import NSpawnError
from .owner_roster import Provider, Roster, account_notes
from .picker import AccountUse, AllProvidersWalled, PickerError, _select_account
from .registry import (
    NODE_PREFIX,
    NOTICE_LEASE_S,
    NOTICE_RETENTION_DAYS,
    STORE_ERRORS,
    Registry,
    RegistryError,
    WriterRedCorrupt,
    _chain_valid,
    create_store,
    store_path,
)
from .renamed import RENAMED, RenamedVariableError, refuse
from .rows import RunPhase, RunRow, SessionProfile


def _one_argument(name: str, arguments: list[str]) -> str | None:
    if len(arguments) != 1 or arguments[0].startswith("-"):
        sys.stderr.write(f"nspawn: this verb takes exactly one argument, <{name}>\n")
        return None
    return arguments[0]


def cmd_mcp_serve(arguments: list[str]) -> int:
    # The MCP SDK is loaded only when this verb serves its stdio boundary.
    from . import main as server

    return server.main(arguments)


def cmd_dispatch_run(arguments: list[str]) -> int:
    """The per-call driver the stdio door launches for each dispatch (`driver.py`)."""
    return driver.drive_main(arguments)


def cmd_notice_drain(arguments: list[str]) -> int:
    """The SessionStart drain of the notice outbox (`notify.drain_main`)."""
    return notify.drain_main(arguments)


#: How many notices one `notice take` claims when the host names no `--limit`.
NOTICE_TAKE_LIMIT: Final = 16
#: How often a `notice take --wait-ms` looks again while nothing is free.
#: ponytail: a 200 ms poll; a wake-up from the arming trigger if latency matters.
NOTICE_POLL_S: Final = 0.2


def _usage(noun: str, verb: str) -> int:
    sys.stderr.write(
        f"nspawn: usage: {noun} {verb} {COMMANDS[(noun, verb)].synopsis}\n"
    )
    return 2


def _refused(noun: str, verb: str, error: BaseException) -> int:
    sys.stderr.write(f"nspawn {noun} {verb} refused: {type(error).__name__}: {error}\n")
    return 2


def _options(
    arguments: list[str], required: tuple[str, ...], optional: tuple[str, ...] = ()
) -> dict[str, str] | None:
    """`--name value` pairs: each required name once, each optional one at most
    once, and nothing else; None otherwise."""
    found: dict[str, str] = {}
    rest = arguments
    while rest:
        match rest:
            case [name, value, *rest] if (
                name in required + optional and name not in found
            ):
                found[name] = value
            case _:
                return None
    return found if all(name in found for name in required) else None


def _count(text: str | None, default: int, minimum: int) -> int | None:
    """An integer option at least `minimum`, `default` when absent, else None."""
    if text is None:
        return default
    try:
        value = int(text)
    except ValueError:
        return None
    return value if value >= minimum else None


def _answer(payload: dict[str, Any]) -> int:
    sys.stdout.write(json.dumps(payload) + "\n")
    return 0


def _coordinator_refuse(noun: str, verb: str) -> int | None:
    """Control verbs cannot run inside a node-scoped nSPAWN environment."""
    for name in (driver.NODE_SESSION_ENV, driver.GRANT_ENV):
        if name in os.environ:
            return _refused(noun, verb, RegistryError(f"node environment sets {name}"))
    return None


def cmd_admission_stop(arguments: list[str]) -> int:
    refused = _coordinator_refuse("admission", "stop")
    if refused is not None:
        return refused
    options = _options(arguments, ("--node", "--generation"))
    generation = None if options is None else _count(options["--generation"], 0, 0)
    if options is None or generation is None:
        return _usage("admission", "stop")
    with Registry(store_path(), "admission-stop", allow_live_store=True) as registry:
        registry.upgrade()
        result = registry.admission_stop(options["--node"], generation)
    return _answer(result)


def _admission_options(
    arguments: list[str], verb: str
) -> tuple[dict[str, str], int] | None:
    required: tuple[str, ...] = (
        "--group",
        "--node",
        "--project",
        "--decision",
        "--generation",
    )
    if verb == "bind":
        required += ("--key", "--fingerprint")
    options = _options(arguments, required)
    generation = None if options is None else _count(options["--generation"], 0, 0)
    if options is None or generation is None:
        _usage("admission", verb)
        return None
    return options, generation


def cmd_admission_bind(arguments: list[str]) -> int:
    refused = _coordinator_refuse("admission", "bind")
    if refused is not None:
        return refused
    parsed = _admission_options(arguments, "bind")
    if parsed is None:
        return 2
    options, generation = parsed
    with Registry(store_path(), "admission-bind", allow_live_store=True) as registry:
        registry.upgrade()
        result = registry.admission_bind(
            options["--group"],
            options["--node"],
            options["--project"],
            options["--decision"],
            generation,
            options["--key"],
            options["--fingerprint"],
        )
    return _answer(result)


def cmd_admission_resume(arguments: list[str]) -> int:
    refused = _coordinator_refuse("admission", "resume")
    if refused is not None:
        return refused
    parsed = _admission_options(arguments, "resume")
    if parsed is None:
        return 2
    options, generation = parsed
    with Registry(store_path(), "admission-resume", allow_live_store=True) as registry:
        registry.upgrade()
        result = registry.admission_resume(
            options["--group"],
            options["--node"],
            options["--project"],
            options["--decision"],
            generation,
        )
    return _answer(result)


def cmd_notice_take(arguments: list[str]) -> int:
    """Claim the free notices of the nodes a prefix names, for the host to deliver."""
    options = _options(
        arguments, ("--owner-prefix",), ("--wait-ms", "--lease-ms", "--limit")
    )
    if options is None:
        return _usage("notice", "take")
    wait_ms = _count(options.get("--wait-ms"), 0, 0)
    lease_ms = _count(options.get("--lease-ms"), NOTICE_LEASE_S * 1000, 1)
    limit = _count(options.get("--limit"), NOTICE_TAKE_LIMIT, 1)
    if wait_ms is None or lease_ms is None or limit is None:
        return _usage("notice", "take")
    deadline = time.monotonic() + wait_ms / 1000
    with Registry(store_path(), "notice-take", allow_live_store=True) as registry:
        registry.upgrade()
        prefix = options["--owner-prefix"]
        while not (taken := registry.take_notices(prefix, lease_ms, limit)):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(remaining, NOTICE_POLL_S))
    return _answer(
        {
            "notices": [
                {
                    "runId": notice.run_id,
                    "owner": notice.owner,
                    "generation": notice.generation,
                    "token": notice.token,
                    "phase": notice.phase,
                    "group": notice.group,
                    "key": notice.key,
                    "failureReason": notice.failure_reason,
                }
                for notice in taken
            ]
        }
    )


def _held(
    arguments: list[str], verb: str, *extra: str
) -> tuple[str, int, str, dict[str, str]] | None:
    """A notice claim whose token comes from one file or stdin, never argv."""
    if arguments.count("--stdin") > 1:
        _usage("notice", verb)
        return None
    stdin = "--stdin" in arguments
    options = _options(
        [argument for argument in arguments if argument != "--stdin"],
        ("--run", "--generation", *extra),
        ("--from-file",),
    )
    generation = None if options is None else _count(options["--generation"], 0, 0)
    if options is None or generation is None or stdin == ("--from-file" in options):
        _usage("notice", verb)
        return None
    try:
        if stdin and sys.stdin is None:
            raise ValueError
        with (
            nullcontext(sys.stdin.buffer)
            if stdin
            else os.fdopen(
                os.open(
                    options["--from-file"],
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                ),
                "rb",
            )
        ) as source:
            if not stdin:
                metadata = os.fstat(source.fileno())
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or stat.S_IMODE(metadata.st_mode) != 0o600
                    or metadata.st_uid != os.geteuid()
                ):
                    raise ValueError
            encoded = source.read(route.MAX_PROFILE_BYTES + 1)
        if len(encoded) > route.MAX_PROFILE_BYTES:
            raise ValueError
        token = encoded.decode("utf-8").strip()
    except OSError, ValueError:
        _refused(
            "notice", verb, route.RouteRefused("notice token input could not be read")
        )
        return None
    if not token or "\n" in token:
        _refused(
            "notice", verb, route.RouteRefused("notice token input must hold one token")
        )
        return None
    return options["--run"], generation, token, options


def cmd_notice_renew(arguments: list[str]) -> int:
    """Extend the lease of a notice the host still holds."""
    held = _held(arguments, "renew", "--lease-ms")
    if held is None:
        return 2
    run_id, generation, token, options = held
    lease_ms = _count(options["--lease-ms"], 0, 1)
    if lease_ms is None:
        return _usage("notice", "renew")
    with Registry(store_path(), "notice-renew", allow_live_store=True) as registry:
        registry.upgrade()
        renewed = registry.renew_notice(run_id, generation, token, lease_ms)
    return _answer({"renewed": renewed})


def cmd_notice_ack(arguments: list[str]) -> int:
    """Mark a held notice delivered, once."""
    held = _held(arguments, "ack")
    if held is None:
        return 2
    run_id, generation, token, _ = held
    with Registry(store_path(), "notice-ack", allow_live_store=True) as registry:
        registry.upgrade()
        acked = registry.ack_notice(run_id, generation, token)
    return _answer({"acked": acked})


def cmd_notice_release(arguments: list[str]) -> int:
    """Clear every claim on the undelivered notices of the nodes a prefix names."""
    options = _options(arguments, ("--owner-prefix",))
    if options is None:
        return _usage("notice", "release")
    with Registry(store_path(), "notice-release", allow_live_store=True) as registry:
        registry.upgrade()
        released = registry.release_notices(options["--owner-prefix"])
    return _answer({"released": released})


def cmd_database_create(arguments: list[str]) -> int:
    """Bring this account's registry file into existence; the door never creates one."""
    if arguments:
        sys.stderr.write("nspawn: database create takes no arguments\n")
        return 2
    path = store_path()
    try:
        create_store(path, allow_live_store=True)
    except nott_database.RefusedError as refusal:
        sys.stderr.write(f"nspawn database create refused: {refusal}\n")
        return 2
    except STORE_ERRORS as error:
        sys.stderr.write(f"nspawn database create failed: {error}\n")
        return 1
    sys.stdout.write(f"created {path}\n")
    return 0


def cmd_database_snapshot(arguments: list[str]) -> int:
    """Copy the registry to a new 0600 file, 0640 with `--group-read`."""
    group_read = arguments[1:] == ["--group-read"]
    if (len(arguments) != 1 and not group_read) or arguments[0].startswith("-"):
        sys.stderr.write("nspawn: usage: database snapshot <dest> [--group-read]\n")
        return 2
    try:
        connection = nott_database.connect(
            store_path(), readonly=True, allow_live_store=True
        )
        try:
            nott_database.snapshot(connection, arguments[0], group_read=group_read)
        finally:
            connection.close()
    except nott_database.RefusedError as refusal:
        sys.stderr.write(f"nspawn database snapshot refused: {refusal}\n")
        return 2
    except STORE_ERRORS as error:
        sys.stderr.write(f"nspawn database snapshot failed: {error}\n")
        return 1
    sys.stdout.write(f"snapshot {arguments[0]}\n")
    return 0


def cmd_database_rollback(arguments: list[str]) -> int:
    refused = _coordinator_refuse("database", "rollback")
    if refused is not None:
        return refused
    if arguments:
        return _usage("database", "rollback")
    with Registry(store_path(), "database-rollback", allow_live_store=True) as registry:
        registry.upgrade()
        version = registry.database_rollback()
    return _answer({"version": version})


def cmd_quota_record(arguments: list[str]) -> int:
    """Read every declared balance source once and store it (the timer's verb)."""
    if arguments:
        return _usage("quota", "record")
    from . import supervisor  # the supervisor module is heavy; only this verb needs it

    try:
        roster = supervisor.read_roster()
        with Registry(store_path(), "quota-record", allow_live_store=True) as registry:
            registry.upgrade()
            outcome = supervisor.pull_compute(registry, roster)
    except (nott_database.RefusedError, NSpawnError) as refusal:
        sys.stderr.write(f"nspawn quota record refused: {refusal}\n")
        return 2
    except STORE_ERRORS as error:
        sys.stderr.write(f"nspawn quota record failed: {error}\n")
        return 1
    _answer(outcome)
    return 0 if all(value == "ok" for value in outcome.values()) else 1


def cmd_learning_export(arguments: list[str]) -> int:
    """Print one JSON line per finished run's class observation; writes nothing."""
    if arguments:
        sys.stderr.write("nspawn: learning export takes no arguments\n")
        return 2
    try:
        with Registry(
            store_path(), "learning-export", allow_live_store=True
        ) as registry:
            labels = registry.export_class_observations()
    except nott_database.RefusedError as refusal:
        sys.stderr.write(f"nspawn learning export refused: {refusal}\n")
        return 2
    except STORE_ERRORS as error:
        sys.stderr.write(f"nspawn learning export failed: {error}\n")
        return 1
    for label in labels:
        observation = label.observation
        row = {
            "run_id": label.run_id,
            "observed_at": label.observed_at,
            "phase": label.phase.value,
            "outcome_at": label.outcome_at,
            "text": observation.text,
            "declared": observation.declared,
            "observed": observation.observed,
            "confidence": observation.confidence,
            "label": label.label,
        }
        sys.stdout.write(json.dumps(row, ensure_ascii=False) + "\n")
    return 0


def _read_tool(tool: str, arguments: dict[str, str | bool]) -> int:
    """Read through this runtime's native MCP; request no domain mutation.

    The server retains its normal lifecycle. Its executable and working directory
    come from this installed package, never a caller's project configuration.
    """
    peer = StdioServer(sys.executable, ("-m", "nspawn.cli", "mcp", "serve"))
    try:
        with NspawnClient(peer, Path(__file__).resolve().parent.parent) as client:
            result = client.call(tool, arguments)
    except NspawnRefused as error:
        sys.stderr.write(f"{tool.replace('_', ' ')} refused: {error}\n")
        return 2
    except NspawnClientError as error:
        sys.stderr.write(f"{tool.replace('_', ' ')} failed: {error}\n")
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _run_read(tool: str, arguments: list[str]) -> int:
    run_id = _one_argument("run-id", arguments)
    return 2 if run_id is None else _read_tool(tool, {"run_id": run_id})


def _provider_read(tool: str, arguments: list[str]) -> int:
    if not arguments:
        return _read_tool(tool, {})
    provider = _one_argument("provider", arguments)
    return 2 if provider is None else _read_tool(tool, {"provider": provider})


def _node(value: str) -> bool:
    """`value` names a node as the host sets it: `node:<session uuid>`."""
    try:
        session = {driver.NODE_SESSION_ENV: value.removeprefix(NODE_PREFIX)}
        return driver.node_owner(session) == value
    except driver.DriverUnavailable:
        return False


def _owner(
    arguments: list[str], verb: str, optional: tuple[str, ...] = ()
) -> dict[str, str] | None:
    """`--owner node:<session>` and the `optional` options; a node's own nSPAWN
    names no other node."""
    options = _options(arguments, ("--owner",), optional)
    if options is None or not _node(options["--owner"]):
        _usage("run", verb)
        return None
    owner = options["--owner"]
    try:
        mine = driver.node_owner(os.environ)
    except NSpawnError as error:
        _refused("run", verb, error)
        return None
    if mine is not None and mine != owner:
        sys.stderr.write(f"nspawn run {verb} refused: this nSPAWN serves {mine}\n")
        return None
    return options


def _group_runs(arguments: list[str]) -> int:
    """`--group <group>`: every run of that group from the store, whoever admitted it,
    each with its owner and the sandbox it was admitted under. A node's own nSPAWN
    names no other node's runs, so it lists no group."""
    options = _options(arguments, ("--group",))
    if options is None or not options["--group"].strip():
        return _usage("run", "list")
    try:
        mine = driver.node_owner(os.environ)
    except NSpawnError as error:
        return _refused("run", "list", error)
    if mine is not None:
        sys.stderr.write(f"nspawn run list refused: this nSPAWN serves {mine}\n")
        return 2
    from .config import resolve_run_root
    from .supervisor import observe_run_progress

    run_root = resolve_run_root()
    with Registry(store_path(), "run-list", allow_live_store=True) as registry:
        registry.upgrade()
        grouped = tuple(
            (row, key, registry.session_reference(row))
            for row, key in registry.group_runs(options["--group"])
        )
    return _answer(
        {
            "runs": [
                {
                    "runId": row.run_id,
                    "group": row.group_name,
                    "key": key,
                    "phase": row.phase.value,
                    "provider": row.executed_provider,
                    "model": row.model,
                    "failureReason": row.blocked_reason,
                    "owner": row.owner,
                    "sandbox": row.sandbox,
                    "tokens": row.tokens,
                    "turn_count": row.turn_count,
                    **({"session": asdict(session)} if session is not None else {}),
                }
                for stored, key, session in grouped
                for row in (observe_run_progress(stored, run_root),)
            ]
        }
    )


def _last_output_at(stdout: Path) -> int | None:
    """When the run last wrote its stdout file, in epoch ms: one stat, no read.
    None when the file does not exist: the child did not start, or retention
    removed the run directory."""
    try:
        return stdout.stat().st_mtime_ns // 1_000_000
    except FileNotFoundError:
        return None


def _caller_runs(arguments: list[str]) -> int:
    """`--caller <socket> --json`: the runs that the Claude Code session at that
    messaging socket started, newest first, from their notice rows. A run leaves
    the list when `prune_notices` removes its notice row or `prune_closed_runs` its
    run row. The nSPAWN of a node or of an audit parent refuses it."""
    refused = _coordinator_refuse("run", "list")
    if refused is not None:
        return refused
    options = _options([a for a in arguments if a != "--json"], ("--caller",))
    if (
        options is None
        or arguments.count("--json") != 1
        or not os.path.isabs(options["--caller"])
    ):
        return _usage("run", "list")
    from .config import resolve_run_root

    with Registry(store_path(), "run-list", allow_live_store=True) as registry:
        registry.upgrade()
        rows = registry.caller_runs(options["--caller"])
    run_root = resolve_run_root()
    # A close moves a failed run to closed. The list keeps `failed`: a close before
    # the host's next read must not hide the failure.
    return _answer(
        {
            "runs": [
                {
                    "runId": row.run_id,
                    "phase": (
                        RunPhase.FAILED.value
                        if row.phase is RunPhase.CLOSED
                        and first == RunPhase.FAILED.value
                        else row.phase.value
                    ),
                    "createdAt": row.created_at,
                    "group": row.group_name,
                    "lastOutputAt": _last_output_at(run_root / row.run_id / "stdout"),
                    "continuedAs": notify.continued_run(row),
                }
                for row, first in rows
            ]
        }
    )


def _scope_empty(registry: Registry, run_id: str) -> bool:
    """Whether the slice the run recorded is gone or holds no process: what nSPAWN's
    close drains. A run that recorded no slice ran nothing in one. A record or a
    group that cannot be read proves nothing."""
    try:
        recorded = registry.writer_scope(run_id)
    except RegistryError:
        return False
    if recorded is None:
        return True
    try:
        return "populated 0" in (Path(recorded[1]) / "cgroup.events").read_text()
    except FileNotFoundError:
        return True  # systemd removes a stopped slice's group
    except OSError:
        return False


def _red_history(registry: Registry, row: RunRow) -> dict[str, object]:
    events = registry.read_run_events(row.run_id)
    profiles = [
        event.payload["session_profile"]
        for event in events
        if event.kind == "lifecycle"
        and isinstance(event.payload, dict)
        and "session_profile" in event.payload
    ]
    result: dict[str, object] = {
        "native_write": False,
        "execution_class": None,
        "red_admission": "unknown",
        "red_admission_reason": "session profile is missing",
        "red_receipt": None,
    }
    if not _chain_valid(events):
        result.update(
            red_admission="invalid",
            red_admission_reason="admission event chain is invalid",
        )
        return result
    if not profiles:
        return result
    try:
        if len(profiles) != 1:
            raise ValueError("multiple session profiles")
        profile = SessionProfile.from_record(profiles[0])
    except TypeError, ValueError:
        result["red_admission_reason"] = "session profile is invalid"
        return result
    result.update(
        native_write=profile.native_write, execution_class=profile.execution_class
    )
    if not profile.native_write or profile.execution_class != "implementation":
        result.update(red_admission="not_required", red_admission_reason=None)
        return result
    try:
        evidence = registry.writer_red(row.run_id)
    except WriterRedCorrupt:
        result.update(
            red_admission="invalid",
            red_admission_reason="writer RED receipt is malformed, duplicate or inconsistent",
        )
        return result
    if evidence is None:
        result.update(
            red_admission="missing",
            red_admission_reason="implementation writer has no admission receipt",
        )
    else:
        receipt, at = evidence
        result.update(
            red_admission="valid",
            red_admission_reason=None,
            red_receipt={
                "record": receipt.record,
                "commit": receipt.commit,
                "unit": receipt.unit,
                "tests": len(receipt.test_ids),
                "admitted_at": at,
            },
        )
    return result


def _live_runs(since: int | None = None) -> int:
    """Read all live runs, or all runs created since the requested epoch ms."""
    try:
        mine = driver.node_owner(os.environ)
        driver.read_grant(os.environ)
    except NSpawnError as error:
        return _refused("run", "list", error)
    from .config import resolve_run_root
    from .supervisor import observe_run_progress

    run_root = resolve_run_root()
    try:
        with Registry(store_path(), "run-list", allow_live_store=True) as registry:
            registry.upgrade()
            rows = (
                registry.live_runs(owner=mine)
                if since is None
                else registry.runs_since(since, owner=mine)
            )
            return _answer(
                {
                    "observed_at": int(time.time()),
                    "runs": [
                        {
                            "run_id": row.run_id,
                            "lane": row.lane,
                            "phase": row.phase.value,
                            "executed_provider": row.executed_provider,
                            "executed_account": row.executed_account,
                            "model": row.model,
                            "tokens": row.tokens,
                            "turn_count": row.turn_count,
                            **(
                                {"failure_reason": row.blocked_reason}
                                if since is not None
                                else {}
                            ),
                            "created_at": row.created_at,
                            "updated_at": row.updated_at,
                            "group": row.group_name,
                            "backend": row.backend.value,
                            **(
                                _red_history(registry, row)
                                if since is not None
                                else {
                                    "native_write": bool(
                                        (
                                            profile := registry.session_profile(
                                                row.run_id
                                            )
                                        )
                                        and profile.native_write
                                    )
                                }
                            ),
                        }
                        for row in (
                            observe_run_progress(stored, run_root) for stored in rows
                        )
                    ],
                }
            )
    except (*STORE_ERRORS, RegistryError, ValueError):
        sys.stderr.write(
            "nspawn run list: RegistryError: complete history or admission evidence read failed; retry the history read\n"
        )
        return 1


def cmd_run_list(arguments: list[str]) -> int:
    """The fleet, through the stdio tool; with `--owner`, a node's runs from the store;
    with `--group`, that group's runs from the store; with `--caller`, one Claude Code
    session's runs from the store. `--include activations` adds the
    node's SDK activations, and names for each run whether it is one and whether the
    slice it recorded is empty or gone."""
    if not arguments:
        return _read_tool("nspawn_run_list", {"include_terminal": True})
    if "--since" in arguments:
        if len(arguments) != 2 or arguments[0] != "--since":
            return _usage("run", "list")
        try:
            instant = datetime.fromisoformat(arguments[1])
            if instant.tzinfo is None:
                instant = instant.replace(tzinfo=UTC)
            since = -(
                (datetime(1970, 1, 1, tzinfo=UTC) - instant)
                // timedelta(milliseconds=1)
            )
        except ValueError, OverflowError:
            sys.stderr.write(
                f"nspawn: --since {arguments[1]!r} needs an ISO date or timestamp\n"
            )
            return 2
        return _live_runs(since)
    if "--live" in arguments:
        return _live_runs() if arguments == ["--live"] else _usage("run", "list")
    if "--group" in arguments:
        return _group_runs(arguments)
    if "--caller" in arguments:
        return _caller_runs(arguments)
    options = _owner(arguments, "list", ("--include",))
    if options is None:
        return 2
    if options.get("--include", "activations") != "activations":
        return _usage("run", "list")
    activations = "--include" in options
    owner = options["--owner"]
    from .config import resolve_run_root
    from .supervisor import observe_run_progress

    run_root = resolve_run_root()
    with Registry(store_path(), "run-list", allow_live_store=True) as registry:
        registry.upgrade()
        owned = []
        for held in registry.owned_runs(owner):
            activation = registry.session_activation(held[0].run_id) is not None
            if activation and not activations:
                continue
            empty = activations and _scope_empty(registry, held[0].run_id)
            owned.append(
                (*held, registry.session_reference(held[0]), activation, empty)
            )
    return _answer(
        {
            "runs": [
                {
                    "runId": row.run_id,
                    "group": row.group_name,
                    "key": key,
                    "phase": row.phase.value,
                    "provider": row.executed_provider,
                    "model": row.model,
                    "failureReason": row.blocked_reason,
                    "generation": generation,
                    "acked": acked,
                    "tokens": row.tokens,
                    "turn_count": row.turn_count,
                    "createdAt": row.created_at,
                    **({"session": asdict(session)} if session is not None else {}),
                    **(
                        {"activation": activation, "scopeEmpty": empty}
                        if activations
                        else {}
                    ),
                }
                for stored, key, generation, acked, session, activation, empty in owned
                for row in (observe_run_progress(stored, run_root),)
            ]
        }
    )


def cmd_run_close(arguments: list[str]) -> int:
    """Close every run a node admitted that is not terminal yet: the host does it
    as it closes that node. With `--before T`, only those admitted at or before T
    (epoch milliseconds): a Stop closes what its node admitted by then, never a run
    admitted after it. Each close is the supervisor's, custody included."""
    group = None
    owner = None
    before = None
    if "--group" in arguments:
        refused = _coordinator_refuse("run", "close")
        if refused is not None:
            return refused
        options = _options(arguments, ("--group",))
        if options is None or not options["--group"].strip():
            return _usage("run", "close")
        group = options["--group"]
    else:
        options = _owner(arguments, "close", ("--before",))
        if options is None:
            return 2
        owner = options["--owner"]
        if "--before" in options:
            before = _count(options["--before"], 0, 0)
            if before is None:
                return _usage("run", "close")
    from .mcp import _default_supervisor  # deferred, like every verb that serves
    from .supervisor import ClosePending

    try:
        supervisor = _default_supervisor()
    except NSpawnError as error:
        return _refused("run", "close", error)

    def outcome(run_id: str) -> tuple[str, str]:
        try:
            readback = supervisor.close(run_id)
        except ClosePending as error:
            return "alive", f"{run_id}: {error}"
        except (NSpawnError, ControllerRefusal) as error:
            return "refused", f"{run_id}: {type(error).__name__}: {error}"
        if readback.phase is RunPhase.CLOSED:
            return "closed", run_id
        return "alive", (
            f"{run_id}: still {readback.phase.value}, "
            f"{len(readback.tree.survivors)} process(es) survive"
        )

    try:
        with supervisor._lock:
            if group is not None:
                held, run_ids = supervisor._registry.admission_close(group)
            else:
                assert owner is not None
                run_ids = supervisor._registry.open_owned_runs(owner, before)
        # Every close is requested at once and each waits out its own bound: one
        # run whose driver never answers holds back none of the others.
        with ThreadPoolExecutor(max_workers=max(1, len(run_ids))) as pool:
            outcomes = list(pool.map(outcome, run_ids))
    finally:
        supervisor.shutdown()
        supervisor._registry.close()
    closed = [line for kind, line in outcomes if kind == "closed"]
    if len(closed) == len(outcomes):
        return _answer(
            {"closed": closed}
            if group is None
            else {
                "group": group,
                "status": "held" if held else "released",
                "closed": closed,
            }
        )
    for kind, line in outcomes:
        sys.stderr.write(f"nspawn run close {kind}: {line}\n")
    return 2


def cmd_slice_reconcile(arguments: list[str]) -> int:
    """Stop each empty writer slice whose run ended, or that no run of the store
    names once it is an hour old, and give back the poisoned claim each held.
    A slice that holds a process, or whose run has not ended, stays. With
    `--dry-run`, name each slice it would stop and stop none."""
    if arguments not in ([], ["--dry-run"]):
        return _usage("slice", "reconcile")
    refused = _coordinator_refuse("slice", "reconcile")
    if refused is not None:
        return refused
    from .mcp import _default_supervisor  # deferred, like every verb that serves

    try:
        supervisor = _default_supervisor()
    except NSpawnError as error:
        return _refused("slice", "reconcile", error)
    try:
        outcomes = supervisor.reconcile_slices(unknown=True, dry_run=bool(arguments))
    except (NSpawnError, ControllerRefusal) as error:
        return _refused("slice", "reconcile", error)
    finally:
        supervisor.shutdown()
        supervisor._registry.close()
    by_action: dict[str, list[dict[str, Any]]] = {}
    for outcome in outcomes:
        by_action.setdefault(outcome.action, []).append(
            {
                "unit": outcome.unit,
                "run_id": outcome.run_id,
                "phase": outcome.phase,
                **({} if outcome.detail is None else {"detail": outcome.detail}),
                **({"claim_released": True} if outcome.claim_released else {}),
            }
        )
    _answer(
        {
            "dry_run": bool(arguments),
            "counts": {action: len(found) for action, found in by_action.items()},
            **by_action,
        }
    )
    return 1 if "refused" in by_action else 0


def cmd_run_show(arguments: list[str]) -> int:
    return _run_read("nspawn_run_show", arguments)


def _run_wait_status(result: dict[str, Any]) -> dict[str, Any]:
    """Read and remove the spill file this wait's own call created."""
    if set(result) != {"preview", "path", "bytes"}:
        return result
    path_text = result["path"]
    if not isinstance(path_text, str) or not path_text:
        raise NspawnClientError("the peer returned an invalid run status spill path")
    path = Path(path_text)
    try:
        try:
            payload = path.read_bytes()
        finally:
            path.unlink(missing_ok=True)
    except (OSError, ValueError) as error:
        raise NspawnClientError(
            f"cannot read or remove spilled run status: {error}"
        ) from error
    size = result["bytes"]
    if type(size) is not int or len(payload) != size:
        raise NspawnClientError("the spilled run status does not match its byte count")
    try:
        status = json.loads(payload)
    except (ValueError, RecursionError) as error:
        raise NspawnClientError("the spilled run status is not valid JSON") from error
    if not isinstance(status, dict):
        raise NspawnClientError("the spilled run status is not a JSON object")
    return status


def _start_tool(noun: str, arguments: list[str]) -> int:
    """Send one stdin object unchanged through this runtime's MCP door."""
    if arguments:
        return _usage(noun, "start")
    try:
        payload = json.load(sys.stdin)
    except (ValueError, OSError, RecursionError) as error:
        return _refused(noun, "start", error)
    if not isinstance(payload, dict):
        return _refused(noun, "start", ValueError("stdin must be one JSON object"))
    peer = StdioServer(sys.executable, ("-P", "-m", "nspawn.cli", "mcp", "serve"))
    try:
        with NspawnClient(
            peer, Path.cwd(), deadline_s=driver.ACK_WAIT_S + 60
        ) as client:
            result = client.call(f"nspawn_{noun}_start", payload)
    except NspawnRefused as error:
        sys.stderr.write(
            f"nspawn {noun} start refused: {error.message}\n"
            f"{json.dumps(error.data, ensure_ascii=False)}\n"
        )
        return 2
    except NspawnClientError as error:
        sys.stderr.write(f"nspawn {noun} start failed: {error}\n")
        return 1
    return _answer(result)


def cmd_run_start(arguments: list[str]) -> int:
    return _start_tool("run", arguments)


def cmd_review_start(arguments: list[str]) -> int:
    return _start_tool("review", arguments)


def cmd_run_wait(arguments: list[str]) -> int:
    """Wait through this runtime's MCP, following the run that serves the work."""
    if len(arguments) != 1 or not arguments[0] or arguments[0].startswith("-"):
        sys.stderr.write(
            f"nspawn run wait refused: expected one <run-id>, got {arguments!r}\n"
        )
        return 2
    from .supervisor import Supervisor

    run_id = arguments[0]
    followed = 0
    peer = StdioServer(sys.executable, ("-m", "nspawn.cli", "mcp", "serve"))
    try:
        with NspawnClient(peer, Path(__file__).resolve().parent.parent) as client:
            while True:
                result = _run_wait_status(
                    client.call(
                        "nspawn_run_show", {"run_id": run_id, "timeout_ms": 20_000}
                    )
                )
                try:
                    phase = RunPhase(result.get("phase"))
                except ValueError as error:
                    raise NspawnClientError(
                        "the peer returned no valid run phase"
                    ) from error
                if not phase.is_terminal():
                    continue
                successor = (
                    notify.continued_in(result.get("warning"))
                    if phase in (RunPhase.FAILED, RunPhase.CLOSED)
                    else None
                )
                if successor is not None and followed < Supervisor.MAX_CONTINUATIONS:
                    run_id = successor
                    followed += 1
                    continue
                break
    except NspawnRefused as error:
        sys.stderr.write(f"nspawn run wait refused for {run_id!r}: {error}\n")
        return 2
    except NspawnClientError as error:
        sys.stderr.write(f"nspawn run wait failed for {run_id!r}: {error}\n")
        return 1
    print(json.dumps({"run_id": run_id, "phase": phase.value}))
    return 0 if phase is RunPhase.COMPLETED else 3


def cmd_result_show(arguments: list[str]) -> int:
    """One run's result, read from the store: its answer whole, which the stdio tool
    would spill past one response into a file its caller must then read."""
    run_id = _one_argument("run-id", arguments)
    if run_id is None:
        return 2
    from .config import resolve_run_root
    from .supervisor import run_result

    try:
        mine = driver.node_owner(os.environ)
        driver.read_grant(os.environ)  # a malformed grant opens no store, as on stdio
    except NSpawnError as error:
        return _refused("result", "show", error)
    with Registry(store_path(), "result-show", allow_live_store=True) as registry:
        registry.upgrade()
        row = registry.get_run(run_id)
    # A node's own nSPAWN sees only its own runs, as the stdio tool does.
    if row is None or (mine is not None and row.owner != mine):
        sys.stderr.write(f"nspawn result show refused: no run named {run_id!r}\n")
        return 2
    result = run_result(row, resolve_run_root() / row.run_id)
    shown = {
        "run_id": result.run_id,
        "final_text": result.final_text,
        "stderr_tail": result.stderr_tail,
    }
    if result.failure_reason is not None:
        shown["failure_reason"] = result.failure_reason
    print(json.dumps(shown, ensure_ascii=False, indent=2))
    return 0


def cmd_transcript_show(arguments: list[str]) -> int:
    return _run_read("nspawn_transcript_show", arguments)


def cmd_quota_show(arguments: list[str]) -> int:
    return _provider_read("nspawn_quota_show", arguments)


def cmd_provider_list(arguments: list[str]) -> int:
    return _provider_read("nspawn_provider_list", arguments)


def _route_arguments(
    arguments: list[str], verb: str
) -> tuple[str, dict[str, str], bool] | None:
    """`<provider> [--route <route>] [--codex-adapter <path>]`, and for start
    `[--cwd <dir>] [--check]`."""
    if not arguments or arguments[0].startswith("-"):
        sys.stderr.write(
            f"nspawn: usage: route {verb} {COMMANDS[('route', verb)].synopsis}\n"
        )
        return None
    options: dict[str, str] = {}
    check = False
    rest = arguments[1:]
    while rest:
        match rest:
            case ["--check", *rest] if verb == "start":
                check = True
            case [
                "--route"
                | "--codex-adapter"
                | "--cwd"
                | "--profile"
                | "--profile-digest"
                | "--grant-root"
                | "--consumer"
                | "--account"
                | "--move" as flag,
                value,
                *rest,
            ] if flag not in options and (
                flag in {"--route", "--codex-adapter"}
                or flag in {"--consumer", "--account", "--move"}
                and verb == "show"
                or flag != "--consumer"
                and verb == "start"
            ):
                options[flag] = value
            case _:
                sys.stderr.write(
                    f"nspawn: usage: route {verb} {COMMANDS[('route', verb)].synopsis}\n"
                )
                return None
    return arguments[0], options, check


class WallsUnreadable(Exception):
    """The registry did not answer the walls and load a new node's account is
    picked on: no account is picked, never the roster's first."""

    def __init__(self, code: int) -> None:
        self.code = code
        super().__init__("account walls cannot be read; no account is picked")


def _picked(account: str | None) -> route.Pick:
    """nSPAWN's own account step (`picker._select_account`) on the registry's walls,
    live runs, quota windows and last runs, for a new node; `account` pins it."""

    def pick(provider: Provider) -> str | None:
        try:
            with Registry(store_path(), "route-show", allow_live_store=True) as opened:
                opened.upgrade()
                now = int(time.time())
                provider_walls = opened.get_provider_walls(now)
                account_walls = opened.get_account_walls(now)
                load = opened.get_account_load()
                use = AccountUse(
                    opened.get_account_usage(), opened.get_account_last_run()
                )
        except (nott_database.RefusedError, RegistryError) as error:
            raise WallsUnreadable(2) from error
        except STORE_ERRORS as error:
            raise WallsUnreadable(1) from error
        if account is not None and (refusal := route.key_refusal(provider, account)):
            raise PickerError(refusal)
        return _select_account(
            route.keyless(provider),
            account,
            provider_walls,
            account_walls,
            now,
            load,
            use,
        )

    return pick


def _moved(
    roster: Roster, walled: str, allowed: set[str], codex_adapter: str | None
) -> tuple[str, route.Route, str | None, list[str]]:
    """The first provider of `allowed` other than `walled`, in the roster's declared
    order, whose picker gives a new node an account: its name, its route, the model
    the route runs (None: the vendor's default) and why each one before it could not.
    Raises `_NoProvider` with every reason when none can."""
    names = dict.fromkeys(
        "agy" if p.kind == "agy" else p.name
        for p in roster.providers
        if p.kind == "agy" or p.name in route.NODE_PROVIDERS
    )
    picker = _picked(None)
    chosen: list[tuple[Provider, str | None]] = []
    skipped: list[str] = []

    def pick(provider: Provider) -> str | None:
        # The provider `route_start` resolved: an unnamed node never runs one the
        # roster keeps for named dispatch.
        if provider.named_only:
            raise PickerError(f'provider "{provider.name}" is named_only')
        alias = picker(provider)
        chosen.append((provider, alias))
        return alias

    for name in (n for n in names if n in allowed and n != walled):
        chosen.clear()
        try:
            found, environment, arguments = route.route_start(
                roster, name, None, codex_adapter=codex_adapter, pick=pick
            )
        except (AllProvidersWalled, PickerError, route.RouteRefused) as error:
            skipped.append(f"{name}: {error.message}")
            continue
        if not chosen and any(p.named_only for p in roster.providers if p.name == name):
            # A Claude roster with no accounts runs the home's login and picks nothing.
            skipped.append(f'{name}: provider "{name}" is named_only')
            continue
        # The model the route itself sets: qwen and codex an account's or the
        # provider's, agy the provider's, Claude none (the node's own default).
        resolved, alias = chosen[0] if chosen else (None, None)
        account = resolved.accounts.get(alias or "") if resolved else None
        default = resolved.model if resolved else None
        model = {
            "qwen": environment.get("ANTHROPIC_MODEL"),
            "codex": (account.model if account else None) or default,
            "agy": default,
        }.get(name)
        return name, (found, environment, arguments), model, skipped
    raise _NoProvider(skipped)


class _NoProvider(Exception):
    """No other provider can serve: why each one could not."""

    def __init__(self, skipped: list[str]) -> None:
        self.skipped = skipped
        super().__init__("; ".join(skipped))


def _reset(nearest: int | None) -> str:
    """When the first account of a walled pool resets, as a refusal says it."""
    if nearest is None:
        return ""
    return "; the first account resets at " + time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(nearest)
    )


def cmd_route_show(arguments: list[str]) -> int:
    """Print the route a node records, the models a qwen node's aliases name, and
    the Claude home a route sets, where its adapter keeps its transcripts.

    Never a token or an environment: the node reads what it shows, nothing else.
    """
    request = _route_arguments(arguments, "show")
    if request is None:
        return 2
    provider, options, _ = request
    allowed = set(filter(None, options.get("--move", "").split(",")))
    if (
        (
            "--consumer" in options
            and (provider != "claude" or options["--consumer"] != "sdk")
        )
        or ("--account" in options and "--route" in options)
        or not allowed <= set(route.NODE_PROVIDERS)
    ):
        return _usage("route", "show")
    alert: str | None = None
    wanted = provider
    try:
        roster = route.node_roster()
        try:
            found, environment, _ = route.route_start(
                roster,
                provider,
                options.get("--route"),
                codex_adapter=options.get("--codex-adapter"),
                pick=None
                if "--route" in options
                else _picked(options.get("--account")),
            )
        except AllProvidersWalled as refused:
            if not allowed or "--route" in options or "--account" in options:
                raise
            try:
                provider, (found, environment, _), model, skipped = _moved(
                    roster, provider, allowed, options.get("--codex-adapter")
                )
            except _NoProvider as none:
                if not none.skipped:
                    raise refused from None
                raise AllProvidersWalled(
                    f"{refused.message}; no other provider serves: {none}",
                    refused.nearest_reset,
                ) from refused
            alert = (
                f"{wanted}: {refused.message}{_reset(refused.nearest_reset)}; this"
                f" node runs on {provider}, "
                + (f"model {model}" if model else "on its default model")
                + "".join(f"; {why}" for why in skipped)
            )
    except route.RouteRefused as refusal:
        sys.stderr.write(f"{refusal.message}\n")
        return 2
    except AllProvidersWalled as walled:
        sys.stderr.write(f"{walled.message}{_reset(walled.nearest_reset)}\n")
        return 2
    except WallsUnreadable as failure:
        sys.stderr.write(f"{failure}\n")
        return failure.code
    except PickerError as failure:
        sys.stderr.write(f"{failure.message}\n")
        return 2
    except route.RosterUnreadable as failure:
        sys.stderr.write(f"{failure.message}\n")
        return 1
    shown: dict[str, Any] = {
        "route": found,
        "model": environment.get("ANTHROPIC_MODEL"),
        "model_fast": environment.get("ANTHROPIC_DEFAULT_HAIKU_MODEL"),
        "config_dir": environment.get("CLAUDE_CONFIG_DIR"),
    }
    if alert is not None:
        shown.update(provider=provider, alert=alert)
    # What the owner fixes in the roster, shown where the node host reads the route.
    selected = roster.select(provider)
    notes = account_notes(selected) if selected is not None else []
    if notes:
        shown["warning"] = "; ".join(notes)
    if (
        options.get("--consumer") == "sdk"
        and environment.get("CLAUDE_CODE_OAUTH_TOKEN")
        and not environment.get("ANTHROPIC_BASE_URL")
    ):
        try:
            runtime = route.runtime_read(environment)
        except route.RouteRefused as error:
            return _refused("route", "show", error)
        if runtime is not None:
            shown["sdk_consumer"] = runtime
    sys.stdout.write(json.dumps(shown) + "\n")
    return 0


def cmd_route_list(arguments: list[str]) -> int:
    refused = _coordinator_refuse("route", "list")
    if refused is not None:
        return refused
    options = _options(arguments[1:], ("--model", "--effort"), ("--exclude",))
    if not arguments or arguments[0] != "claude" or options is None:
        return _usage("route", "list")
    try:
        excluded = json.loads(options.get("--exclude", "[]"))
    except json.JSONDecodeError:
        return _usage("route", "list")
    if not isinstance(excluded, list) or any(
        not isinstance(item, str) for item in excluded
    ):
        return _usage("route", "list")
    try:
        roster = route.node_roster()
        with Registry(store_path(), "route-list", allow_live_store=True) as registry:
            registry.upgrade()
            shown = route.route_list(
                roster,
                registry,
                model=options["--model"],
                effort=options["--effort"],
                now=int(time.time()),
                excluded=tuple(excluded),
            )
    except route.RouteRefused as error:
        return _refused("route", "list", error)
    except ModelCatalogError as error:
        sys.stderr.write(f"nspawn route list failed: ModelCatalogError: {error}\n")
        return 1
    except route.RosterUnreadable as error:
        sys.stderr.write(f"{error.message}\n")
        return 1
    return _answer(shown)


def cmd_route_switch(arguments: list[str]) -> int:
    """Consume one causal event on stdin; credentials never cross this port."""
    refused = _coordinator_refuse("route", "switch")
    if refused is not None:
        return refused
    options = _options(arguments[1:], ("--route", "--model", "--effort"))
    if not arguments or arguments[0] != "claude" or options is None:
        return _usage("route", "switch")
    encoded = sys.stdin.buffer.read(route.MAX_PROFILE_BYTES + 1)
    if len(encoded) > route.MAX_PROFILE_BYTES:
        return _refused(
            "route",
            "switch",
            route.RouteRefused(
                "subscription event exceeds the private transport bound"
            ),
        )

    def reject_constant(value: str) -> None:
        raise ValueError("invalid JSON number")

    try:
        body = json.loads(encoded, parse_constant=reject_constant)
    except json.JSONDecodeError, UnicodeDecodeError, ValueError:
        return _refused(
            "route", "switch", route.RouteRefused("subscription event is malformed")
        )
    if not isinstance(body, dict):
        return _refused(
            "route", "switch", route.RouteRefused("subscription event is malformed")
        )
    try:
        roster = route.node_roster()
        with Registry(store_path(), "route-switch", allow_live_store=True) as registry:
            registry.upgrade()
            shown = route.route_switch(
                roster,
                registry,
                options["--route"],
                body,
                now=int(time.time()),
                model=options["--model"],
                effort=options["--effort"],
            )
    except route.RouteRefused as error:
        return _refused("route", "switch", error)
    except ModelCatalogError as error:
        sys.stderr.write(f"nspawn route switch failed: ModelCatalogError: {error}\n")
        return 1
    except route.RosterUnreadable as error:
        sys.stderr.write(f"{error.message}\n")
        return 1
    return _answer(shown)


def cmd_route_wall(arguments: list[str]) -> int:
    """Wall a node's account for its failed prompt, read from stdin as
    `{observed_at, error}`, and read the wall back."""
    refused = _coordinator_refuse("route", "wall")
    if refused is not None:
        return refused
    options = _options(arguments[1:], ("--route",))
    if (
        not arguments
        or arguments[0] not in {"claude", "codex", "agy"}
        or options is None
    ):
        return _usage("route", "wall")
    encoded = sys.stdin.buffer.read(route.MAX_PROFILE_BYTES + 1)

    def reject_constant(value: str) -> None:
        raise ValueError("invalid JSON number")

    try:
        if len(encoded) > route.MAX_PROFILE_BYTES:
            raise ValueError("node failure exceeds the private transport bound")
        body = json.loads(encoded, parse_constant=reject_constant)
    except UnicodeDecodeError, ValueError:
        return _refused(
            "route", "wall", route.RouteRefused("node failure is malformed")
        )
    if not isinstance(body, dict):
        return _refused(
            "route", "wall", route.RouteRefused("node failure is malformed")
        )
    try:
        roster = route.node_roster()
        with Registry(store_path(), "route-wall", allow_live_store=True) as registry:
            registry.upgrade()
            shown = route.route_wall(
                roster,
                registry,
                arguments[0],
                options["--route"],
                body,
                now=int(time.time()),
            )
    except route.RouteRefused as error:
        return _refused("route", "wall", error)
    except route.RosterUnreadable as error:
        sys.stderr.write(f"{error.message}\n")
        return 1
    return _answer(shown)


# Only fixed PRELAUNCH reasons cross this boundary. Unknown text stays private.
_PRELAUNCH_REASONS = frozenset(
    {
        "JSON integer is not exactly portable",
        "JSON object keys must be ASCII strings",
        "JSON text is not valid Unicode",
        "JSON value is outside the canonical vocabulary",
        "PRELAUNCH requires a selected Claude account",
        "PRELAUNCH requires native subscription authentication",
        "a grant shows a socket's directory",
        "admission activation differs",
        "admission conversation differs",
        "admission profile digest differs",
        "configuration document identity differs",
        "configuration snapshot captured bytes differ",
        "configuration snapshot differs from selected inputs",
        "configuration snapshot file identity differs",
        "configuration snapshot identity differs",
        "configuration snapshot topology differs",
        "configuration snapshots require sealed inputs",
        "constructor baseline differs from selected options",
        "constructor environment exceeds its admitted vocabulary",
        "consumer runtime differs from PRELAUNCH",
        "duplicate JSON object key",
        "duplicate pending control reference",
        "duplicate selected launch input",
        "invalid JSON document",
        "launch command has no admitted executable identity",
        "launch runtime identity differs",
        "native account authentication is unavailable",
        "native state and scratch must have admitted destinations",
        "native tool boundary differs from its admitted scope",
        "native tool boundary exposes administrative inputs",
        "native tool boundary has no scratch destination",
        "network bootstrap producers were not admitted",
        "new submission requires its prepared input",
        "node administrative grants expose platform writes",
        "node bootstrap confinement is unavailable",
        "node filesystem destination is absent or noncanonical",
        "noninteger JSON number is not admitted",
        "policy generation differs from admitted managed inputs",
        "private contract bytes are not canonical",
        "private contract must be a JSON object",
        "private egress endpoint was not admitted",
        "private egress port exceeds its admitted boundary",
        "private profile descriptor could not be sealed",
        "private profile descriptor write made no progress",
        "private profile input is unavailable",
        "producer authentication differs from admitted channels",
        "producer caller or project differs",
        "producer destinations exceed the node",
        "producer filesystem grants exceed the node",
        "profile and grant root must be absolute",
        "profile changed while being verified",
        "profile exceeds the private transport bound",
        "profile grant directory is not private and owned",
        "profile grant root is linked or noncanonical",
        "profile is outside the owning activation grant",
        "profile must be a bounded private owned regular file",
        "profile path is not a closed grant-relative path",
        "replacement must not resubmit owner input",
        "runtime content identity differs",
        "runtime entry is absent from its file manifest",
        "runtime file has incompatible identity bindings",
        "runtime input has incompatible identities",
        "runtime input is unavailable",
        "runtime inventory changed or is unavailable",
        "runtime inventory contains a nonregular input",
        "runtime inventory contains missing or extra inputs",
        "runtime inventory exceeds its bound",
        "runtime inventory has an external or unknown link",
        "runtime inventory is unavailable",
        "runtime inventory topology differs",
        "runtime root overlaps or lacks a canonical input",
        "runtime roots exceed the native argument bound",
        "runtime stage already has an uncertain owner",
        "runtime stage directory ownership differs",
        "runtime stage has an admitted filesystem alias",
        "runtime stage is not private and owned",
        "sealed private profile descriptors require Linux",
        "selected file content identity differs",
        "selected file exceeds its launch bound",
        "selected file has incompatible identity bindings",
        "selected file identity is linked or noncanonical",
        "selected file is not a bounded regular input",
        "selected file pin made no progress",
        "selected launch input is unavailable",
        "selected native runtime differs from its account route",
        "selected route or working directory differs",
        "staged profile digest differs",
        "the sealed profile descriptor is not private",
        "the selected consumer is not an ECMAScript module",
        "version must be an integer",
        "write destinations exceed the permission ceiling",
    }
)


def cmd_route_start(arguments: list[str]) -> int:
    """Check the selected native recipe without launching a session.

    Actual session and inference launches enter nspawn_run_start and its
    durable admission, process scope and control boundary.
    """
    request = _route_arguments(arguments, "start")
    if request is None:
        return 2
    provider, options, check = request
    if not check:
        sys.stderr.write(
            "native session launches require nspawn_run_start; route start only accepts --check\n"
        )
        return 2
    private_flags = {"--profile", "--profile-digest", "--grant-root"}
    supplied = private_flags & options.keys()
    if supplied:
        if (
            supplied != private_flags
            or provider != "claude"
            or "--route" not in options
            or "--codex-adapter" in options
        ):
            sys.stderr.write(
                "PRELAUNCH requires a Claude route, profile, digest and grant root\n"
            )
            return 2
        from .admission import AdmissionRefusal

        try:
            with route.profile_prepare(
                provider,
                options["--route"],
                path=Path(options["--profile"]),
                digest=options["--profile-digest"],
                grant_root=Path(options["--grant-root"]),
                cwd=options.get("--cwd"),
            ) as prepared:
                sys.stdout.buffer.write(prepared.receipt.encoded + b"\n")
                return 0
        except (AdmissionRefusal, route.RouteRefused) as error:
            if isinstance(error, route.RouteRefused):
                discriminator = f"RouteRefused: nERR-{error.entry.code_str()}"
            else:
                reason = str(error)
                if reason not in _PRELAUNCH_REASONS:
                    reason = "private admission refused"
                discriminator = f"AdmissionRefusal: {reason}"
            sys.stderr.write(
                "Claude PRELAUNCH was refused before consumer execution "
                f"({discriminator})\n"
            )
            return 2
        except route.RosterUnreadable:
            sys.stderr.write("the provider roster could not be read\n")
            return 1
        except OSError as error:
            sys.stderr.write(f"Claude consumer could not start: {error.strerror}\n")
            return 1
    try:
        route.route_launch(
            provider,
            options.get("--route"),
            codex_adapter=options.get("--codex-adapter"),
        )
    except route.RouteRefused as refusal:
        sys.stderr.write(f"{refusal.message}\n")
        return 2
    except route.RosterUnreadable as failure:
        sys.stderr.write(f"{failure.message}\n")
        return 1
    try:
        os.chdir(options.get("--cwd", "."))
    except OSError as error:
        sys.stderr.write(
            f"{provider} node directory is not reachable: {error.strerror}\n"
        )
        return 1
    return 0


def _threads(verb: str, read: Callable[[Any], dict[str, Any]]) -> dict[str, Any] | int:
    """`read(supervisor)` on this account's supervisor, shut down after it the way
    `cmd_run_close` shuts it down: every vendor child it held is gone."""
    refused = _coordinator_refuse("thread", verb)
    if refused is not None:
        return refused
    from .mcp import _default_supervisor  # deferred, like every verb that serves

    try:
        supervisor = _default_supervisor()
    except NSpawnError as error:
        return _refused("thread", verb, error)
    try:
        return read(supervisor)
    except (NSpawnError, route.RosterUnreadable) as error:
        return _refused("thread", verb, error)
    finally:
        supervisor.shutdown()
        supervisor._registry.close()


def cmd_thread_list(arguments: list[str]) -> int:
    """Claude and Codex sessions nSPAWN did not start, newest first, and the homes
    it could not read. No title reaches stderr."""
    options = _options(arguments, (), ("--limit", "--exclude"))
    if options is None or options.get("--exclude", "-") != "-":
        return _usage("thread", "list")
    limit = _count(options.get("--limit"), 50, 1)
    if limit is None or limit > 200:
        return _usage("thread", "list")
    exclude: frozenset[str] = frozenset()
    if "--exclude" in options:
        # The caller's own sessions (the node host's nodes), a JSON list on stdin.
        try:
            read = json.loads(sys.stdin.read(64 * 1024 * 1024))
        except ValueError:
            read = None
        if not isinstance(read, list) or not all(isinstance(i, str) for i in read):
            return _usage("thread", "list")
        exclude = frozenset(read)
    shown = _threads(
        "list", lambda supervisor: supervisor.threads(limit, exclude=exclude)
    )
    return shown if isinstance(shown, int) else _answer(shown)


def cmd_thread_show(arguments: list[str]) -> int:
    """One session nSPAWN did not start; with `--messages`, a Claude session's newest
    messages, read from `--claude-home` (a 0700 directory of this uid)."""
    reference, *rest = arguments or [""]
    provider, _, ident = reference.partition(":")
    messages = "--messages" in rest
    if messages:
        rest.remove("--messages")
    options = _options(rest, (), ("--claude-home",))
    if (
        provider not in {"claude", "codex"}
        or not THREAD_ID.fullmatch(ident)
        or options is None
        or (messages and provider != "claude")
        or ("--claude-home" in options) != messages
    ):
        return _usage("thread", "show")
    home = None
    if messages:
        home = Path(options["--claude-home"])
        try:
            info = home.lstat()
        except OSError:
            return _usage("thread", "show")
        if (
            not home.is_absolute()
            or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            sys.stderr.write(
                "nspawn thread show: --claude-home must be a 0700 directory of this user\n"
            )
            return 2
    shown = _threads(
        "show", lambda supervisor: supervisor.threads(1, (provider, ident), home)
    )
    if isinstance(shown, int):
        return shown
    if "thread" in shown and shown["thread"] is None:
        started = "was started by nSPAWN" if shown.get("started") else "not found"
        sys.stderr.write(f"nspawn thread show: {provider}:{ident} {started}\n")
        return 3
    return _answer(shown)


#: A vendor session id: a Claude session UUID or a Codex thread id.
THREAD_ID: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,299}")


def _vendors() -> set[str]:
    from .journal import TRANSLATORS

    return {translator.vendor for translator in TRANSLATORS}


def cmd_session_serve(arguments: list[str]) -> int:
    """The session journal's writer, in the foreground until SIGTERM."""
    if arguments:
        return _usage("session", "serve")
    from .journal import serve

    return serve.main()


def cmd_session_list(arguments: list[str]) -> int:
    """One JSON line per journal session, newest change first, with its totals.
    `--id` keeps one session family and needs a vendor. `--count` ends the reply
    with `{"count":N}`, where N is the number of session lines before it."""
    vendor: str | None = None
    rest = arguments
    if arguments and arguments[0] in _vendors():
        vendor, rest = arguments[0], arguments[1:]
    ids: list[str] = []
    since: str | None = None
    count = False
    while rest:
        match rest:
            case ["--id", ident, *rest] if vendor and THREAD_ID.fullmatch(ident):
                ids.append(ident)
            case ["--since", bound, *rest] if since is None and _iso(bound):
                since = bound
            case ["--count", *rest] if not count:
                count = True
            case _:
                return _usage("session", "list")
    from .journal import show

    try:
        lines = show.list_sessions(store_path(), vendor, since=since, ids=ids or None)
    except show.JournalFormatRefused as error:
        sys.stderr.write(f"nspawn session list: {error}\n")
        return 2
    if count:
        lines.append(json.dumps({"count": len(lines)}, separators=(",", ":")))
    sys.stdout.write("".join(line + "\n" for line in lines))
    return 0


def _iso(value: str) -> bool:
    """Whether `value` is an ISO time that `datetime.fromisoformat` reads."""
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


def cmd_session_show(arguments: list[str]) -> int:
    """A journal session's rows as stored, then its `total` line."""
    match arguments:
        case [vendor, ident] if vendor in _vendors() and THREAD_ID.fullmatch(ident):
            pass
        case _:
            return _usage("session", "show")
    from .journal import show

    try:
        lines = show.show(store_path(), vendor, ident)
    except (show.Missing, show.JournalFormatRefused) as error:
        sys.stderr.write(f"nspawn session show: {error}\n")
        return 2
    sys.stdout.write("".join(line + "\n" for line in lines))
    return 0


def _install(noun: str, verb: str, call: Callable[[], dict[str, Any]]) -> int:
    """An install verb's JSON; a refusal exits 2 and a failure after a call 1."""
    try:
        return _answer(call())
    except install.InstallRefused as error:
        sys.stderr.write(f"{error}\n")
        return 2
    except install.InstallFailed as error:
        sys.stderr.write(f"nspawn {noun} {verb} failed: {error}\n")
        return 1


def install_flags(rest: list[str]) -> tuple[bool, list[str]] | None:
    """`[--check] [--skill <name>]...`; None when `rest` holds anything else."""
    check, names = False, []
    while rest:
        match rest:
            case ["--check", *rest] if not check:
                check = True
            case ["--skill", name, *rest]:
                names.append(name)
            case _:
                return None
    return check, names


def cmd_skill_install(arguments: list[str]) -> int:
    match arguments:
        case [provider, source, *rest] if (flags := install_flags(rest)) is not None:
            check, names = flags
        case _:
            return _usage("skill", "install")
    return _install(
        "skill",
        "install",
        lambda: install.skill_install(provider, source, check, names),
    )


def cmd_plugin_install(arguments: list[str]) -> int:
    match arguments:
        case [provider, source, plugin]:
            check = False
        case [provider, source, plugin, "--check"]:
            check = True
        case _:
            return _usage("plugin", "install")
    return _install(
        "plugin",
        "install",
        lambda: install.plugin_install(provider, source, plugin, check),
    )


def cmd_review_admit(arguments: list[str]) -> int:
    """Count the two public brief lines without starting a model run."""
    if arguments != ["--stdin"]:
        return _usage("review", "admit")
    from . import review_cap

    try:
        subject, candidate, revision = review_cap.brief(sys.stdin.read())
        with Registry(store_path(), "review-admit", allow_live_store=True) as registry:
            registry.upgrade()
            answer = registry.admit_review_head(subject, candidate, revision)
    except review_cap.InvalidCandidate as error:
        sys.stderr.write(f"ReviewCandidateInvalid: {error}\n")
        return 2
    return _answer(answer)


def cmd_turn_send(arguments: list[str]) -> int:
    """Queue one user message for a live run. A run that is not live gives exit 2."""
    match arguments:
        case [run_id, text] if run_id and not run_id.startswith("-"):
            pass
        case _:
            return _usage("turn", "send")
    from .config import resolve_run_root

    with Registry(store_path(), "turn-send", allow_live_store=True) as registry:
        row = registry.get_run(run_id)
    if row is None:
        sys.stderr.write(f"nspawn turn send refused: no run {run_id!r}\n")
        return 2
    if row.phase.is_terminal():
        sys.stderr.write(
            f"nspawn turn send refused: run {run_id} is {row.phase.value}, not live\n"
        )
        return 2
    try:
        steer.send(resolve_run_root(), run_id, text)
    except steer.SteerRefused as error:
        return _refused("turn", "send", error)
    return _answer({"run_id": run_id, "queued": True})


# --- the table ---------------------------------------------------------------------


class Command(NamedTuple):
    synopsis: str
    summary: str
    handler: Callable[[list[str]], int]


COMMANDS: Final[dict[tuple[str, str], Command]] = {
    ("review", "admit"): Command(
        "--stdin", "count one review head from its brief lines", cmd_review_admit
    ),
    ("mcp", "serve"): Command(
        "[--host-control]", "serve the MCP surface on stdio", cmd_mcp_serve
    ),
    ("dispatch", "run"): Command(
        "[--check | --host-control]",
        "drive one dispatch whose frame arrives on stdin (the stdio door launches it)",
        cmd_dispatch_run,
    ),
    ("notice", "drain"): Command(
        "--project <directory>",
        "print the run notices no live caller of this project heard",
        cmd_notice_drain,
    ),
    ("notice", "take"): Command(
        "--owner-prefix node: [--wait-ms N] [--lease-ms L] [--limit K]",
        "claim, as JSON, the free notices of the nodes the prefix names",
        cmd_notice_take,
    ),
    ("notice", "renew"): Command(
        "--run <run-id> --generation G (--from-file F | --stdin) --lease-ms L",
        "extend the lease of a notice the host holds",
        cmd_notice_renew,
    ),
    ("notice", "ack"): Command(
        "--run <run-id> --generation G (--from-file F | --stdin)",
        "mark a held notice delivered, once",
        cmd_notice_ack,
    ),
    ("notice", "release"): Command(
        "--owner-prefix node:",
        "clear the claims on the undelivered notices of the nodes the prefix names",
        cmd_notice_release,
    ),
    ("database", "create"): Command(
        "", "create this account's registry file", cmd_database_create
    ),
    ("database", "snapshot"): Command(
        "<dest> [--group-read]",
        "copy the registry to a new 0600 file (0640 with --group-read)",
        cmd_database_snapshot,
    ),
    ("database", "rollback"): Command(
        "",
        "set schema 6 back to 5, retaining controls and refusing live reserved runs",
        cmd_database_rollback,
    ),
    ("admission", "stop"): Command(
        "--node node:<session> --generation N",
        "fence later admissions for every workflow of this node",
        cmd_admission_stop,
    ),
    ("admission", "resume"): Command(
        "--group W --node node:<session> --project <path> --decision D --generation N",
        "release only this workflow at the node's exact current Stop generation",
        cmd_admission_resume,
    ),
    ("admission", "bind"): Command(
        "--group W --node node:<session> --project <path> --decision D --generation N --key K --fingerprint <sha256>",
        "reserve one native request's key under its immutable workflow binding",
        cmd_admission_bind,
    ),
    ("learning", "export"): Command(
        "",
        "print each finished run's class observation as a JSON line, labelled "
        "with its effective class",
        cmd_learning_export,
    ),
    ("run", "start"): Command(
        "< <arguments.json>",
        "start a run with the tool's arguments as one JSON object on stdin",
        cmd_run_start,
    ),
    ("review", "start"): Command(
        "< <arguments.json>",
        "start a review with the tool's arguments as one JSON object on stdin",
        cmd_review_start,
    ),
    ("run", "list"): Command(
        "[--live | --since <ISO> | --owner node:<session> [--include activations] | --group <group>"
        " | --caller <socket> --json]",
        "read the fleet as JSON; with --live, every nonterminal run from the store "
        "without a page limit; with --since, every run created at or after that "
        "ISO time (UTC when no offset is given); with --owner, that node's runs (with its SDK "
        "activations and whether each slice is empty, on --include activations); "
        "with --group, every run of that group with its owner and sandbox; "
        "with --caller, the runs that the Claude Code session at that socket "
        f"started, while their notice rows last ({NOTICE_RETENTION_DAYS} days). "
        "A closed run can leave sooner, when nSPAWN prunes its run row",
        cmd_run_list,
    ),
    ("run", "close"): Command(
        "--owner node:<session> [--before <epoch-ms>] | --group W",
        "close every run that node admitted that is not terminal yet, or only those "
        "admitted by --before; with --group, only reserved non-terminal runs while held",
        cmd_run_close,
    ),
    ("run", "show"): Command("<run-id>", "read one run as JSON", cmd_run_show),
    ("run", "wait"): Command(
        "<run-id>",
        "wait for terminal work and print its run and phase as JSON",
        cmd_run_wait,
    ),
    ("slice", "reconcile"): Command(
        "[--dry-run]",
        "stop each empty writer slice whose run ended, or that no run names once "
        "it is an hour old, and release the poisoned claim it held; --dry-run "
        "names them and stops none",
        cmd_slice_reconcile,
    ),
    ("turn", "send"): Command(
        "<run-id> <text>",
        "queue one user message for a live Claude run and exit 0; a run that is "
        "not live gives exit 2",
        cmd_turn_send,
    ),
    ("result", "show"): Command(
        "<run-id>", "read one run's result as JSON", cmd_result_show
    ),
    ("transcript", "show"): Command(
        "<run-id>", "read one bounded transcript page as JSON", cmd_transcript_show
    ),
    ("quota", "show"): Command(
        "[provider]", "read stored fleet quota as JSON", cmd_quota_show
    ),
    ("quota", "record"): Command(
        "", "read each balance source once and store it", cmd_quota_record
    ),
    ("provider", "list"): Command(
        "[provider]", "read the declared provider roster as JSON", cmd_provider_list
    ),
    ("route", "show"): Command(
        "<provider> [--route <route> | --account <alias> | --move <providers>]"
        " [--codex-adapter <path>] [--consumer sdk]",
        "print, as JSON, the route a node on <provider> records and its model; a new"
        " node's account is the one the picker spreads work to, or --account; with"
        " --move, a walled <provider> gives the first of <providers> (comma"
        " separated) that serves, with `provider` and `alert`",
        cmd_route_show,
    ),
    ("route", "start"): Command(
        "<provider> [--route <route>] [--codex-adapter <path>] [--cwd <dir>] [--profile <path> --profile-digest <digest> --grant-root <dir>] [--check]",
        "check the selected native recipe; launch sessions through nspawn_run_start",
        cmd_route_start,
    ),
    ("route", "list"): Command(
        "claude --model <model> --effort <effort> [--exclude <JSON array>]",
        "read configured subscription eligibility and enforced account walls",
        cmd_route_list,
    ),
    ("route", "switch"): Command(
        "claude --route <route> --model <model> --effort <effort>",
        "record a causal subscription event read from stdin and read its wall back",
        cmd_route_switch,
    ),
    ("route", "wall"): Command(
        "<claude|codex|agy> --route <route>",
        "wall a node's account for the failed prompt read from stdin and read the"
        " wall back, with the earliest reset when no account is left",
        cmd_route_wall,
    ),
    ("thread", "list"): Command(
        "[--limit N] [--exclude -]",
        "read, as JSON, the Claude and Codex sessions nSPAWN did not start",
        cmd_thread_list,
    ),
    ("thread", "show"): Command(
        "<provider>:<id> [--messages --claude-home DIR]",
        "read one of those sessions as JSON; with --messages, a Claude session's "
        "newest messages",
        cmd_thread_show,
    ),
    ("session", "serve"): Command(
        "",
        "translate each live Claude session into the session journal, in the "
        "foreground until SIGTERM",
        cmd_session_serve,
    ),
    ("session", "list"): Command(
        "[<vendor>] [--id <id>]... [--since <ISO>] [--count]",
        "print one JSON line per journal session, newest change first, with its "
        "token totals; --id keeps that session, its copies and its subagents; "
        "--count ends the reply with its row count",
        cmd_session_list,
    ),
    ("session", "show"): Command(
        "<vendor> <id>",
        "print a journal session's rows as JSON lines, then its token totals",
        cmd_session_show,
    ),
    ("skill", "install"): Command(
        "<claude|codex> <source> [--check] [--skill <name>]...",
        "install skills with npx skills; --check stages them and shows them as JSON",
        cmd_skill_install,
    ),
    ("plugin", "install"): Command(
        "claude <marketplace-source> <plugin> [--check]",
        "install a Claude plugin; --check stages it and shows it as JSON",
        cmd_plugin_install,
    ),
}

USAGE: Final = (
    "usage: nspawn <noun> <verb> [arguments]   (nspawn --help lists every command)"
)


def render_help() -> str:
    """The help, generated from COMMANDS in table order."""
    lines = [USAGE, ""]
    for (noun, verb), command in COMMANDS.items():
        head = f"nspawn {noun} {verb} {command.synopsis}".rstrip()
        lines.append(f"  {head}\n      {command.summary}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Dispatch `<noun> <verb>` to its handler."""
    arguments = sys.argv[1:] if argv is None else argv
    match arguments:
        case ["--help" | "-h" | "help"]:
            print(render_help())
            return 0
        case [noun, verb, *rest] if (noun, verb) in COMMANDS:
            try:
                refuse(frozenset(os.environ), RENAMED)
                system1_refuse(frozenset(os.environ), SYSTEM1_RENAMED)
            except (RenamedVariableError, System1RenamedVariableError) as error:
                sys.stderr.write(f"nspawn: refused: {error}\n")
                return 2
            try:
                return COMMANDS[(noun, verb)].handler(rest)
            except (nott_database.RefusedError, RegistryError) as error:
                sys.stderr.write(f"nspawn {noun} {verb} refused: {error}\n")
                return 2
            except (*STORE_ERRORS, ModelCatalogError) as error:
                sys.stderr.write(f"nspawn {noun} {verb} failed: {error}\n")
                return 1
        case []:
            sys.stderr.write(f"nspawn: {USAGE}\n")
            return 2
        case _:
            sys.stderr.write(
                f"unknown command: nspawn {' '.join(arguments[:2])}\n{USAGE}\n"
            )
            return 2


if __name__ == "__main__":
    raise SystemExit(main())
