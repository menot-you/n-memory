"""L7 run-directory and supervisor: admission, lifecycle, and identity-bound close.

Admission is synchronous only up to the durable row (`execute`/`review` return
``RunAccepted`` before the child ever spawns); everything from spawn onward runs
on one worker thread per run — the child-I/O boundary the shared contract admits
threads at. That worker is `_drive_run`, factored out from `execute`/`review` so
it can be driven directly and synchronously in tests, with L2/L3/L5/L8 replaced
by fakes; real thread timing proves only that `execute` schedules it.

Two clocks meet here and nowhere else in this package: the registry's lease and
row timestamps are milliseconds (`admit_run`'s `now` is added directly to
`lease_ms`), while the picker, the wall tables, and terminal classification are
all contracted in epoch *seconds* ("current epoch seconds" — L2, L5). Every call
site below picks the one its callee's contract names; nothing is converted
implicitly.

One SQLite connection backs `self._registry`, and every registry method is a
short write or a read. `self._lock` (one `threading.Condition`) therefore
serializes *every* registry call across every thread this supervisor owns, and
doubles as the wait/notify primitive `status()`'s long-poll blocks on.
"""

from __future__ import annotations

import codecs
import contextlib
import contextvars
import errno
import functools
import json
import math
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Any, Final, cast

from nott_system1 import SCOPES, route_variables

from . import (
    agy,
    agy_restricted,
    app_server,
    child,
    classify,
    claude_catalog,
    claude_child,
    claude_restricted,
    config,
    native,
    notify,
    picker,
    pump,
    quota_sources,
    route,
    session,
    terminal,
    threads,
    worktree,
)
from .admission import AdmissionRefusal, AttemptReceiptV1, SessionAdmissionV1
from .command import Command
from .command import resolve as command_resolve
from .command_terminal import canonical_cwd as command_cwd
from .custody import AggregateScope, ControllerRefusal, ExecutorScope
from .errors import NERR_1104, NERR_1128, ErrorEntry, NSpawnError
from .inference import InferenceHome, StructuredInference
from .limits import MAX_DURABLE_OBSERVATION_BYTES
from .owner_roster import (
    ANTHROPIC_BASE_URL_KEY,
    CAPABILITIES,
    NativeAccount,
    Provider,
    Roster,
    claude_config_dir,
    claude_family,
    credential_source,
    expand_home,
    expand_value_with,
)
from .process_identity import (
    ProcessIdentity,
    ProcessIdentityError,
    capture_process_identity,
    group_member_pids,
    process_identity_matches,
)
from .quota_sources import PULL_TIMEOUT_S, SourceReading
from .redact import SecretNeedles
from .registry import (
    EXHAUSTED_UTILIZATION,
    POISONED_LEASE_UNTIL,
    RUN_KEY_CONSTRAINT,
    STORE_ERRORS,
    SWARM_PREFIX,
    UNKNOWN_EFFORT,
    AdmissionHeldRow,
    AdmissionUnboundRow,
    ClosedRun,
    EffortProfile,
    KeyedRun,
    KeyFencedRow,
    KeyLock,
    NoticeCaller,
    Registry,
    RegistryError,
    RunKey,
    StaleComputeReading,
    UniqueTaken,
    token_counter,
    token_sum,
)
from .rows import (
    CLAUDE_CONFINEMENT_POLICY_ONLY,
    AccountUsageRow,
    AccountWallRow,
    Backend,
    ClassObservation,
    ComputeBalanceRow,
    NativeSessionContext,
    ProviderWallRow,
    RunPhase,
    RunRow,
    SessionActivation,
    SessionProfile,
    SessionReference,
    validate_system_prompt,
)
from .vendor_home import VendorHomeError, read_owner_config

#: `nspawn thread list|show`: the time one home may take, the time the whole verb
#: may take (well within the host's 60 s `nspawn_answer`), and the wait for a
#: killed reader's thread.
THREAD_HOME_S: Final = 10.0
THREAD_VERB_S: Final = 30.0
THREAD_REAP_S: Final = 5.0
#: The newest messages a replay shows, and their JSON bytes.
THREAD_MESSAGES: Final = 2000
THREAD_BYTES: Final = 16 * 1024 * 1024
# shortcut: tokens only within a 64 MiB budget per list, upgrade to a cache keyed by
# path, size and mtime when the owner needs every row.
THREAD_BUDGET_BYTES: Final = 64 * 1024 * 1024
# shortcut: scan 200 recent Claude sessions, add paging when older sessions are needed.
THREAD_SCAN_LIMIT: Final = 200
THREAD_SLOW_REASON: Final = "the transcript took too long to read"
THREAD_TIMEOUT_REASON: Final = "not read: the home did not answer in time"
HOST_CONTROL_REFUSAL: Final = "native session control requires host control"


class ThreadUnread(NSpawnError):
    """One home's sessions could not be read; the reason names why."""

    def __init__(self, reason: str) -> None:
        super().__init__(NERR_1128, reason)


def _codex_homes(roster: Roster) -> list[tuple[Provider, str | None, Path]]:
    """Each Codex home to read: the `codex` provider's accounts, then the default
    `~/.codex` when it exists and no account uses it (alias None)."""
    provider = next(
        (p for p in roster.providers if p.name == "codex" and p.kind == "codex"), None
    )
    if provider is None:
        return []
    homes: list[tuple[Provider, str | None, Path]] = [
        (provider, alias, native.vendor_home(account).expanduser())
        for alias, account in sorted(provider.accounts.items())
    ]
    default = Path.home() / ".codex"
    if default.is_dir() and default.resolve() not in {
        home.resolve() for _, _, home in homes
    }:
        homes.append((provider, None, default))
    return homes


#: How long a close waits for a live driver to answer, above the summed
#: close steps; how often it re-reads the owner; and the claim lease.
CLOSE_WAIT_S: Final = 45.0
CLOSE_POLL_S: Final = 1.0
CLOSE_LEASE_S: Final = 45.0


@dataclass
class DriveCall:
    """Per-call data a driver's `serve` hands to `_admit`, and what it hands back.

    Carried by a contextvar so the public `execute`/`review` signatures, and
    every fake that overrides them, stay unchanged.
    """

    launch_uuid: str
    group: str | None = None
    #: The session a terminal notice is owed to (`{addr, pid, start_ticks}`)
    #: and the project key its drain is scoped by; no caller, no notices row.
    caller: dict[str, Any] | None = None
    project: str | None = None
    #: This admission's `run_keys` row; `_admit` writes it with the run.
    run_key: RunKey | None = None
    run_id: str | None = None
    worker: threading.Thread | None = None

    def notice(self) -> NoticeCaller | None:
        if self.caller is None or self.project is None:
            return None
        return NoticeCaller(
            addr=str(self.caller["addr"]),
            pid=int(self.caller["pid"]),
            start_ticks=int(self.caller["start_ticks"]),
            project=self.project,
        )


DRIVE_CALL: contextvars.ContextVar[DriveCall | None] = contextvars.ContextVar(
    "nspawn_drive_call", default=None
)


def join_workers(call: DriveCall) -> None:
    """Join the call's worker, and each worker it hands the call to.

    A review that fails over admits its successor from inside the failed run's
    worker, and that admission moves `call.worker` (and `call.run_id`) to the
    successor. A driver that joined only the first worker would exit while the
    review still runs, and end that run FAILED at exit.
    """
    joined: threading.Thread | None = None
    while (worker := call.worker) is not None and worker is not joined:
        worker.join()
        joined = worker


#: The env var naming an explicit `claude` binary override, read once per admission
#: so a test or an operator can pin the binary without touching the roster.
CLAUDE_BIN_ENV: Final = "NOTT_NSPAWN_CLAUDE_BIN"

#: The CLOSED allowlist a child inherits from this supervisor's own environment.
#: `build_child_env` starts from `{}`, so before this existed the child received
#: NOTHING: no `PATH` (a real executor cannot find `bash`, `git` or `node`) and
#: no `HOME` (it cannot read its own `~/.claude`). It is an allowlist and NEVER
#: `os.environ`: a child carrying the operator's whole environment hides what the
#: composed one really provides, which is how an empty memory store once reached
#: the owner unseen.
CHILD_ENV_ALLOWLIST: Final = ("PATH", "HOME", "LANG", "LC_ALL")


#: A Claude child reads its whole prompt from stdin before its first request;
#: a minute is a stuck child, not a slow one.
PROMPT_WRITE_SECONDS = 60.0


def preparation_sibling_key(run_id: str) -> str:
    """The custody slot of a preparation child that runs beside the run's leader."""
    return run_id + "/mcp_inventory"


def _allowed_base_environ() -> dict[str, str]:
    """Read the closed allowlist out of this process's own environment."""
    return {
        name: value
        for name in CHILD_ENV_ALLOWLIST
        if (value := os.environ.get(name)) is not None
    }


def _dropped_records(first: bytes) -> int:
    """The records a command's retained output emptied before `first`, its first
    line (`droppedRecords` in its gap record); 0 for every other line."""
    try:
        frame = json.loads(first)
    except ValueError, RecursionError:
        return 0
    if not isinstance(frame, dict) or frame.get("type") != "terminal_output":
        return 0
    dropped = frame.get("droppedRecords")
    return dropped if type(dropped) is int and dropped > 0 else 0


def _log_tick_failure(error: BaseException) -> None:
    """Surface a reaper failure, with traceback, without re-raising.

    A bare `print` would couple the supervisor to the stdio of the host
    process; a logger would couple it to a config the lane does not own.
    Writing to the process's own stderr under `nspawn.reaper` lets the host
    route it through whatever log shipper it already runs, and keeps the
    lane dependency-free. The traceback is included ON PURPOSE: a
    programming defect in the heartbeat or the orphan sweep would otherwise
    repeat silently every tick — a full stack every ten seconds is the
    loudest whisper this design allows. Best effort: a log that cannot be
    written never fails the seam that called it.
    """
    if sys.is_finalizing():
        return
    with contextlib.suppress(Exception):
        print(
            f"nspawn.reaper: tick failed and the loop continues: "
            f"{type(error).__name__}: {error}",
            file=sys.stderr,
            flush=True,
        )
        traceback.print_exception(
            type(error), error, error.__traceback__, file=sys.stderr
        )


def _log_shutdown_failure(operation: str, error: BaseException) -> None:
    """Surface one expected shutdown failure while cleanup continues."""
    print(
        f"nspawn.shutdown: {operation} failed and shutdown continues: "
        f"{type(error).__name__}: {error}",
        file=sys.stderr,
        flush=True,
    )


@dataclass(frozen=True, slots=True)
class BootstrapOutcome:
    """What the attempt to wake a sleeping route did, and whether it worked.

    `live` is the only field that authorises a dispatch. `ran` and
    `returncode` are there so the owner can see on the run row that a dispatch
    started a service — a side effect the run should never hide.
    """

    ran: bool
    live: bool
    argv: tuple[str, ...] = ()
    returncode: int | None = None
    detail: str | None = None


def _route_answers(url: str, timeout: float) -> bool:
    """Whether the endpoint is serving, asked the cheapest honest way.

    Any HTTP answer counts, including 401 and 404: this asks whether the
    process is up, not whether this caller may use it. Only a transport
    failure means asleep.
    """
    request = urllib.request.Request(f"{url.rstrip('/')}/v1/models", method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout):
            return True
    except urllib.error.HTTPError:
        return True
    except urllib.error.URLError, OSError, ValueError:
        return False


def bring_route_up(
    account: NativeAccount,
    *,
    probe_timeout_s: float = 3.0,
    bootstrap_timeout_s: float = 90.0,
    settle_s: float = 2.0,
) -> BootstrapOutcome:
    """Wake the route behind `account` if it is asleep, and prove it woke.

    Runs the declared argv AT MOST ONCE, then reads the endpoint back. A
    command that exits zero is not proof that the service answers — that
    distinction has cost this project a session before — so the second probe,
    not the exit code, decides `live`.

    An account with no endpoint or no declared command is left exactly alone:
    nothing here guesses how to start a service nobody described.
    """
    if account.base_url is None or not account.bootstrap:
        live = (
            False
            if account.base_url is None
            else _route_answers(account.base_url, probe_timeout_s)
        )
        return BootstrapOutcome(ran=False, live=live)
    if _route_answers(account.base_url, probe_timeout_s):
        return BootstrapOutcome(ran=False, live=True)

    argv = tuple(account.bootstrap)
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            timeout=bootstrap_timeout_s,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return BootstrapOutcome(
            ran=True, live=False, argv=argv, detail=f"bootstrap failed: {error}"
        )
    if settle_s > 0:
        time.sleep(settle_s)
    live = _route_answers(account.base_url, probe_timeout_s)
    detail = None if live else f"{account.base_url} did not answer after bootstrap"
    return BootstrapOutcome(
        ran=True,
        live=live,
        argv=argv,
        returncode=completed.returncode,
        detail=detail,
    )


class SupervisorError(NSpawnError):
    """Root of L7's own typed refusals. Dependency refusals propagate as-is."""

    #: A refusal raised after `admit_run` names the run it failed.
    run_id: str | None = None
    #: Typed detail rendered beside the message at the MCP boundary.
    data: dict[str, Any] | None = None

    def __init__(self, message: str) -> None:
        self.message = message
        Exception.__init__(self, message)


class IdempotencyConflict(SupervisorError):
    """The key already names a run admitted for a different request."""


class KeyFenced(SupervisorError):
    """The key's claim was released naming no run: nothing is admitted under it."""


class AdmissionHeld(SupervisorError):
    """A reversible node Stop refused admission; the same request and key remain free."""


class AdmissionUnbound(SupervisorError):
    """The request's group, key or fingerprint does not match its reserved binding."""


class KeyTaken(SupervisorError):
    """The key's unique index refused this admission: another run holds it.

    The backstop behind the key lock; the driver re-reads the key and answers
    the hit or the conflict it is.
    """


class ClosePending(SupervisorError):
    """The run's live driver has not answered its close request in time."""


class InvalidRunRequest(SupervisorError):
    """A caller-supplied field failed a basic shape check before any state changed."""


#: An admission refusal that names the route, not the request: an unpinned
#: review or verification skips that preference and tries the next one.
#: Store, key and node-hold refusals are not here: no other route cures them.
_ROUTE_REFUSALS: Final = (
    picker.PickerError,
    picker.AllProvidersWalled,
    InvalidRunRequest,
    native.NativePreparationError,
    VendorHomeError,
)

#: A failure inside the worker that is the route's refusal to prepare the turn,
#: never the work's: the next preference may serve it.
_PREPARATION_REFUSALS: Final = (
    native.NativePreparationError,
    app_server.PluginSkillsRefused,
    VendorHomeError,
)
_PREPARATION_REFUSAL_NAMES: Final = frozenset(
    refusal.__name__ for refusal in _PREPARATION_REFUSALS
)

#: The witness classes that fail over across the owner's preferences.
_WITNESS_CLASSES: Final = frozenset({"review", "verification"})
#: A dispatch that names no producer excludes nothing.
_NO_PRODUCER: Final = picker.Producer()
#: The refusal of a pick whose model is the producer's. It is known only after
#: the pick, because a route's default model can be the producer's.
PRODUCER_MODEL: Final = "the producer's model"


@dataclass(frozen=True, slots=True)
class _AccountStep(picker.Preference):
    """A walk entry that names one account of one provider (`route`).

    Resolved by provider name with every other account of it kept off, never
    by the alias alone: two providers' secrets directories may share an alias.
    """

    account: str = ""

    def __post_init__(self) -> None:
        if not self.account:
            raise ValueError("an account step names its account")

    def __str__(self) -> str:
        return f"{self.route}/{self.account}"


def _credential(provider: Provider, alias: str | None) -> str:
    """The key of the credential a run on `provider` and account `alias`
    (None: no account) uses: `credential_source(...).key()`, the source the
    spawn itself reads its secret from, and nothing else.

    It never raises: a provider that declares no source is keyed by its
    route and alias, and the reason is logged, so a walk is never lost.
    """
    try:
        return credential_source(provider, alias).key()
    except (ValueError, OSError) as error:
        fallback = f"route:{provider.name}" + ("" if alias is None else f"/{alias}")
        _log_credential(fallback, type(error).__name__)
        return fallback


def _log_credential(key: str, why: str) -> None:
    with contextlib.suppress(Exception):
        print(
            f"nspawn.failover: the credential is keyed as {key}: {why}",
            file=sys.stderr,
            flush=True,
        )


def _named_accounts(
    roster: Roster, preference: picker.Preference
) -> list[tuple[Provider, str]]:
    """The accounts a walk entry names: a step's one account; for a route that
    is an account alias and not a provider name, each provider's account of
    that alias; none for a provider name."""
    if isinstance(preference, _AccountStep):
        return [
            (provider, preference.account)
            for provider in roster.providers
            if provider.name == preference.route
        ]
    if any(provider.name == preference.route for provider in roster.providers):
        return []
    return [
        (provider, preference.route)
        for provider in roster.providers
        if preference.route in provider.discovered_accounts()
    ]


def _without_credentials(
    roster: Roster, refused: frozenset[str], only: _AccountStep | None = None
) -> Roster:
    """A copy of `roster` in which no account whose credential is `refused`
    runs, and, with `only`, its provider runs on that one account alone."""
    if not refused and only is None:
        return roster

    def kept_off(provider: Provider, alias: str) -> bool:
        if only is not None and provider.name == only.route and alias != only.account:
            return True
        return _credential(provider, alias) in refused

    return replace(
        roster,
        providers=[
            replace(
                provider,
                excluded_accounts=[
                    *provider.excluded_accounts,
                    *(
                        alias
                        for alias in provider.discovered_accounts()
                        if alias not in provider.excluded_accounts
                        and kept_off(provider, alias)
                    ),
                ],
            )
            for provider in roster.providers
        ],
    )


@dataclass
class _Failover:
    """What a run needs to try its next preference: every fresh run with no
    `session_id`, no orchestrator binding and no structured inference. A writer takes it only while its tree shows no write.

    `remaining` is the walk. For an unpinned review or verification it is the
    rest of the owner's order. For any other unpinned run it is the other
    routes of its class, then the other accounts of its route, none when the
    entry that served names an account. Pinned, the walk is the other accounts
    of the pinned route, none for a pinned account. `skipped` is each preference
    not served so far, with its reason. `refused` holds the routes of runs with
    no account that refused this work, and `refused_credentials` the credential
    of every run that refused or failed it: the same work is not sent to them
    again.
    """

    execution_class: str
    remaining: list[picker.Preference]
    skipped: list[str]
    producer: picker.Producer
    refused: picker.Producer
    #: The caller's own model and effort, which every preference carries.
    model: str | None
    effort: str | None
    #: The `_admit` keywords every attempt shares: prompt, cwd, the roster, the
    #: cage and the run's shape.
    admit: dict[str, Any]
    #: When the caller's deadline ends (monotonic), None for no deadline.
    deadline_at: float | None
    current: picker.Preference | None = None
    #: The reset of each wall that stopped a preference, for the final refusal.
    resets: list[int] = field(default_factory=list)
    any_failure: bool = False
    #: The credential (`_credential`) of each run that refused or failed this
    #: work, with an account or without one: it never serves this work again,
    #: under any provider.
    refused_credentials: frozenset[str] = frozenset()
    #: The run whose failure (not a refusal) spent the one retry; None while
    #: the retry is free.
    retried_by: str | None = None


def _skipped_note(failover: _Failover) -> str | None:
    """The run's record of each preference skipped before it, or None."""
    if not failover.skipped:
        return None
    return _bounded_text(
        f"{failover.execution_class} preference: skipped "
        + "; skipped ".join(failover.skipped),
        MAX_DURABLE_OBSERVATION_BYTES // 2,
    )


SWARM_TIMEOUT_MS: Final = 2 * 60 * 60 * 1000


def _is_swarm(group: str | None) -> bool:
    return group is not None and group.startswith(SWARM_PREFIX)


#: The HTTP statuses by which a vendor refuses an account: its credential (401,
#: 403), its quota (402) or its rate (429).
_ACCOUNT_STATUSES: Final = frozenset({401, 402, 403, 429})


def _limit_type(classification: terminal.TerminalClassification) -> str:
    """The cause an account wall records for a refusal of that account."""
    if classification.api_error_status == 402:
        return "quota_exhausted"
    if classification.api_error_status == 429:
        return "rate_limited"
    return "credential_rejected"


def _moved_note(
    picked: picker.PickerResult, classification: terminal.TerminalClassification
) -> str:
    """The note of an account a run left for another one inside the same run,
    in the form `notify.substitution_alert` reads."""
    cause = _route_refusal(classification) or "it refused this turn"
    return (
        f"moved off {picked.executed_provider.name}/{picked.executed_account} "
        f"({cause.replace('(', '').replace(')', '').replace(';', ',')})"
    )


def _route_refusal(classification: terminal.TerminalClassification) -> str | None:
    """Why the route refused this turn, or None for any other ending.

    A wall, an HTTP status from the vendor, or a rate-limit or credential
    rejection is the route's answer, and the next preference may serve. A
    failure without one of them is not known to be the route's, so the work
    is not sent again, except a swarm member's one retry (`_retry_reason`).
    """
    if classification.phase is not RunPhase.FAILED:
        return None
    http: str | None = None
    if (status := classification.api_error_status) is not None:
        # The status alone does not say why: a 403 is also an organization that
        # refuses the OAuth account, a 429 is also Z.AI's spent balance (1113).
        code = terminal.rejected_code(classification.final_text, status)
        http = (
            f"HTTP {status} code {code}"
            if code is not None
            else f"HTTP {status} {classification.vendor_error}"
            if classification.vendor_error is not None
            else f"HTTP {status}"
        )
    wall = (
        f"account {account.account_alias} walled: {account.limit_type}"
        if (account := classification.account_wall) is not None
        else "provider walled"
        if classification.provider_wall is not None
        else None
    )
    if wall is not None:
        return wall if http is None else f"{wall}, {http}"
    if http is not None:
        return http
    # codex relays a refused model's status only in its typed failure reason;
    # a preparation refusal `_fail_run` queued for retry is named the same way.
    reason = classification.failure_reason
    if reason is not None and (
        reason.startswith("VendorApiError (HTTP ")
        or reason.partition(" ")[0].partition(":")[0] in _PREPARATION_REFUSAL_NAMES
    ):
        return reason
    failure = (
        None if classification.telemetry is None else classification.telemetry.failure
    )
    if failure in ("quota_exhausted", "credential_rejected", "rate_limited"):
        return failure
    return None


class UnknownRun(SupervisorError):
    """No run by that id is known to this supervisor's registry."""


class RunDirectoryError(SupervisorError):
    """The private run directory could not be created or reserved safely."""


class KeptAnswerError(SupervisorError):
    """A run's `final_text` exists and cannot be served whole."""


class DuplicateSession(SupervisorError):
    """The requested session id already names another run's durable thread."""


class SessionIdCollision(SupervisorError):
    """The vendor refused the minted `--session-id` as already in use, twice.

    One collision on a fresh spawn is operational — the id is re-minted and the
    same run is spawned again. A second one on a fresh id is no
    longer chance, and the run fails naming both ids rather than reading as
    «exited with code 1 without terminal result», which is what it read as for
    a whole run before this existed.
    """


class RunDeadlineExceeded(SupervisorError):
    """The caller named a deadline and the child outlived it.

    A DELIBERATE FAILURE, never a vendor error: the reason names the deadline so
    an operator reading the row can tell «I bounded this» from «the provider
    broke». It reaches the terminal seam through `_drive_run`'s existing
    `except NSpawnError`, so a bounded run is announced like any other death.
    """


class RunCloseRejected(SupervisorError):
    """Death was proven locally but the registry's own fence rejected release."""


class RunClosedBeforeSpawn(SupervisorError):
    """A queued worker lost admission to close before creating its process."""


@dataclass(frozen=True, slots=True)
class RunDirectory:
    root: Path
    stdout: Path
    stderr: Path


@dataclass(frozen=True, slots=True)
class RunAccepted:
    run_id: str
    #: The phase the admission committed; `spawning` once `admit_run` started it.
    phase: RunPhase = RunPhase.PENDING


@dataclass(frozen=True, slots=True)
class RunStatus:
    run_id: str
    phase: RunPhase
    run_dir: str
    failure_reason: str | None
    total_seq: int
    requested_provider: str | None
    executed_provider: str | None
    executed_account: str | None
    warning: str | None
    tokens: int
    marker: str | None
    has_result: bool = False
    stdout_mtime_ns: int | None = None
    stderr_tail: str | None = None
    #: The vendor's HTTP status on the terminal frame, when it carried one.
    #: `failure_reason` is free-form prose; THIS is the field a reader may
    #: branch on. 402 is an empty account, not a broken route, and telling
    #: them apart from the prose alone is a substring guess.
    api_error_status: int | None = None
    #: Quota's OTHER channel: the vendor rejected on its own rate-limit frame,
    #: with no HTTP status to read. Same fact, different wire.
    rate_limited: bool = False
    #: The Claude child's OWN session address, so the caller can subscribe to its end
    #: instead of polling this verb. Null until the child is spawned, and null
    #: for a non-Claude backend or a supervisor outside a Claude session.
    child_addr: str | None = None
    backend: Backend = Backend.CLAUDE
    thread_id: str | None = None
    #: The dispatch's `group`, as the caller named it.
    group: str | None = None
    #: What the caller asked for and the launcher was handed, as the admission
    #: recorded them; unknown for a run admitted before that record existed.
    effort: EffortProfile = UNKNOWN_EFFORT
    inference_profile: dict[str, Any] | None = None
    session: SessionReference | None = None
    transport: dict[str, str] | None = None
    exit_code: int | None = None
    cost_usd: str | None = None
    #: The model the run was admitted on, as its row records it.
    model: str | None = None


@dataclass(frozen=True, slots=True)
class RunResult:
    run_id: str
    final_text: str | None
    stderr_tail: str | None = None
    failure_reason: str | None = None
    structured_output: dict[str, Any] | None = None
    #: The run's phase when the answer was read: a wait that ended first shows
    #: a non-terminal phase here, not a missing answer.
    phase: RunPhase | None = None
    alert: str | None = None


@dataclass(frozen=True, slots=True)
class TranscriptPage:
    run_id: str
    since_seq: int
    next_seq: int
    lines: tuple[str, ...]
    truncated: bool


@dataclass(frozen=True, slots=True)
class ProcessTreeReadback:
    """Exact identities checked after close and any identities still alive."""

    checked: tuple[ProcessIdentity, ...]
    survivors: tuple[ProcessIdentity, ...]


@dataclass(frozen=True, slots=True)
class CloseReadback:
    """What close() did, each field OBSERVED after the act, never intended.

    `claim_released` answers exactly one question: does `nspawn.claims` still
    name this run on this run's worktree? It is read back from the table after
    whatever release the path attempted — it was once a literal `True` on two
    paths that ran no claim DELETE at all, which is the lie this field exists
    to not tell.
    """

    run_id: str
    phase: RunPhase
    tree: ProcessTreeReadback
    claim_released: bool


class QuotaState(Enum):
    """How a quota answer relates to observation. Closed set, five members.

    The four honest ways of not knowing are DISTINCT on purpose. Collapsing any
    two of them into a number — or into each other — is the lie this whole layer
    exists to refuse: an account nobody ever ran is not an account at 0%, and a
    window that already reset is not a current reading of the same figure.
    """

    #: A reading whose window has not reset and whose source answered fresh.
    OBSERVED = "observed"
    #: A real reading, past its source's freshness bound. The NUMBER IS KEPT and
    #: labelled; serving it silently as current is the defect.
    STALE = "stale"
    #: A real reading whose window has already reset. Kept, never zeroed: it is
    #: a fact about the past, and the reader is told which past.
    EXPIRED = "expired"
    #: A real account that has never produced a reading. Not zero.
    NEVER_OBSERVED = "never_observed"
    #: No source can answer for this target at all, for a NAMED reason.
    NO_SOURCE = "no_source"


@dataclass(frozen=True, slots=True)
class WindowQuota:
    """One vendor window, as the report carries it."""

    window: str
    utilization: float
    resets_at: int
    observed_at: int
    state: QuotaState


@dataclass(frozen=True, slots=True)
class AccountQuota:
    """One named account's quota, or the exact reason there is none.

    The `windows`/`absence` pair carries the same iff invariant the source layer
    enforces: windows without a reason, or a reason beside windows, are both
    refused. A caller can therefore never read an empty tuple and wonder whether
    it means "nothing spent" or "we could not ask".
    """

    account_alias: str
    state: QuotaState
    windows: tuple[WindowQuota, ...] = ()
    source: str | None = None
    absence: str | None = None
    fetched_at: int | None = None
    cooling_until: int | None = None
    wall_source: str | None = None
    wall_limit_type: str | None = None

    def __post_init__(self) -> None:
        _refuse_half_an_answer(
            self.account_alias, self.windows, self.absence, "quota windows"
        )


@dataclass(frozen=True, slots=True)
class ProviderQuota:
    """One roster provider: what it serves, and what is known about its quota.

    A provider carries `windows` of its OWN when its quota is not per-account —
    the env-route providers have no account to attribute a reading to — and
    carries `accounts` when it does. Both being empty is legal only beside an
    `absence`, which the constructor enforces.
    """

    provider: str
    models: tuple[str, ...]
    state: QuotaState
    default_model: str | None = None
    accounts: tuple[AccountQuota, ...] = ()
    windows: tuple[WindowQuota, ...] = ()
    source: str | None = None
    absence: str | None = None
    fetched_at: int | None = None
    cooling_until: int | None = None
    wall_source: str | None = None
    #: A compute-unit balance from the provider's `quota_source`, or the reason
    #: it could not be read. Both None: the provider declares no such source.
    compute: quota_sources.ComputeBalance | None = None
    compute_absence: str | None = None
    #: "updated 2 min ago", beside a `compute` reading.
    compute_updated: str | None = None
    #: The reading is older than `COMPUTE_STALE_S`: shown, never as fresh.
    compute_stale: bool = False
    #: The last failed pull: its reason and its epoch second. A failure keeps
    #: the last good `compute`.
    compute_failure: str | None = None
    compute_failed_at: int | None = None

    def __post_init__(self) -> None:
        if not self.accounts:
            _refuse_half_an_answer(
                self.provider, self.windows, self.absence, "quota windows"
            )
        if self.compute is not None and self.compute_absence is not None:
            raise InvalidRunRequest(
                f"{self.provider!r} carries a compute balance beside its absence"
            )
        if self.compute_absence == "" or self.compute_failure == "":
            raise InvalidRunRequest(f"{self.provider!r} carries an empty reason")
        if (self.compute is None) != (self.compute_updated is None) or (
            self.compute is None and self.compute_stale
        ):
            raise InvalidRunRequest(
                f"{self.provider!r} carries an age or a stale mark without a reading"
            )
        if (self.compute_failure is None) != (self.compute_failed_at is None):
            raise InvalidRunRequest(f"{self.provider!r} carries half a failure")


@dataclass(frozen=True, slots=True)
class FleetRun:
    """One run as the fleet listing shows it — never the whole row.

    `lane` is the field the operator actually reads: a fleet of UUIDs cannot be
    reasoned about, which is why the column exists and why leaving it NULL made
    the listing useless before this.
    """

    run_id: str
    lane: str | None
    phase: RunPhase
    executed_provider: str | None
    executed_account: str | None
    model: str
    tokens: int
    turn_count: int
    created_at: int
    updated_at: int
    #: The batch or campaign group the caller named, as `nspawn_run_show` shows it.
    group: str | None = None
    backend: Backend = Backend.CLAUDE


@dataclass(frozen=True, slots=True)
class FleetPage:
    """Every live run under one clock.

    A PAGE and not a bare tuple, for two reasons that both bite. `_to_jsonable`
    renders a dict at the top level of an MCP result and a JSON array there does
    not type-check; and age has to be DERIVED from one `observed_at` rather than
    stamped per row, so a listing carries a single clock instead of N that can
    disagree with each other by the width of the query.
    """

    observed_at: int
    runs: tuple[FleetRun, ...]
    #: Terminal notices armed and not yet delivered to any session.
    notices: tuple[FleetNotice, ...] = ()


@dataclass(frozen=True, slots=True)
class FleetNotice:
    """A terminal notice the outbox holds for a session that has not heard it."""

    run_id: str
    phase: RunPhase
    caller_project: str


@dataclass(frozen=True, slots=True)
class QuotaReport:
    """Every declared provider, its models, and what is known of its quota."""

    observed_at: int
    providers: tuple[ProviderQuota, ...]


@dataclass(frozen=True, slots=True)
class ProviderListing:
    """One declared provider: what it serves, and what a router needs to choose it.

    THE RANKS ARE PART OF THE ANSWER, not decoration. `ranks.py` decides every
    route on exactly these three numbers, so a listing without them says WHAT
    EXISTS and not WHAT TO CHOOSE, and a cold orchestrator would have to read
    the roster it just asked this verb to read for it.

    `accounts`/`absence` carry the same iff `AccountQuota` carries: a provider
    with no discovered account NAMES why, so an empty tuple can never be read
    as "this provider is unreachable" when it is the env route working exactly
    as declared.
    """

    provider: str
    kind: str
    #: What nSPAWN implements for `kind` (`owner_roster.CAPABILITIES`), sorted.
    capabilities: tuple[str, ...]
    models: tuple[str, ...]
    #: The provider's OWN `model`, never `defaults.model`: the roster default is
    #: what a caller gets when a provider declares none, and reporting it here
    #: would state that this provider serves a model it never named.
    default_model: str | None
    efforts: tuple[str, ...]
    accounts: tuple[str, ...]
    absence: str | None
    strength: int | None
    thrift: int | None
    design: int | None
    model_catalog: dict[str, str] | None = None
    model_catalog_error: str | None = None

    def __post_init__(self) -> None:
        _refuse_half_an_answer(self.provider, self.accounts, self.absence, "accounts")


@dataclass(frozen=True, slots=True)
class ProviderReport:
    """Every declared provider, read from the roster and nothing else.

    A PAGE for `FleetPage`'s first reason: `_to_jsonable` renders a dict at the
    top level of an MCP result and a JSON array there does not type-check. It
    carries no clock, unlike the two reports beside it — there is no reading to
    age here, only a file the owner edits, and an `observed_at` no consumer
    reads would be a number this page invites a caller to trust.
    """

    providers: tuple[ProviderListing, ...]
    #: The roster's `[roles]` binding, as declared; an unbound role is absent.
    roles: dict[str, str] = field(default_factory=dict)
    #: The roster's `[role_profile]` tables, as declared; a role without one is absent.
    role_profiles: dict[str, dict[str, str]] = field(default_factory=dict)


#: The injected layer: (provider name, account alias or None) -> one reading.
type QuotaSource = Callable[[str, str | None], SourceReading]

#: How long a reading counts as current. Past it the NUMBER IS KEPT and
#: labelled STALE — never dropped, and never rewritten to zero.
#: ponytail: ONE bound for every source, because the contracted `SourceReading`
#: carries no freshness of its own. Move it onto the reading the day a source
#: declares one.
QUOTA_FRESHNESS_S: Final = 300

#: How long one refresh keeps STARTING vendor pulls, from its first: past it an
#: account answers with what is stored, so a refresh over many accounts ends
#: within this plus one pull (`PULL_TIMEOUT_S`) instead of one pull per account.
QUOTA_PULL_BUDGET_S: Final = PULL_TIMEOUT_S

#: What a row of `nspawn.account_usage` is: the child's own wire, recorded at
#: terminal classification. Named, so an operator can tell a reading nSPAWN
#: happened to see from one a source was asked for.
RUN_WIRE_SOURCE: Final = "run-wire"

#: Distance from "a current number", worst last. A target's state is the WORST
#: of the states beneath it: summarising three windows as the freshest of them
#: is how a report ends up quieter than its own contents.
_STATE_DISTANCE: Final = {
    QuotaState.OBSERVED: 0,
    QuotaState.STALE: 1,
    QuotaState.EXPIRED: 2,
    QuotaState.NEVER_OBSERVED: 3,
    QuotaState.NO_SOURCE: 4,
}


@dataclass(frozen=True, slots=True)
class _Resolved:
    """What one quota target — an account, or a provider that routes by env —
    turned out to know. The `windows`/`absence` iff holds here already."""

    state: QuotaState
    windows: tuple[WindowQuota, ...] = ()
    source: str | None = None
    fetched_at: int | None = None
    absence: str | None = None


def _refuse_half_an_answer(
    subject: str, present: tuple[object, ...], absence: str | None, noun: str
) -> None:
    """Enforce: an empty `present` IFF a named absence. Both illegal quadrants die here.

    Empty-without-a-reason is the silent zero — indistinguishable from "nothing
    spent" for a window, and from "unreachable" for an account. A-reason-beside-
    values is a fact the report simultaneously disowns. Neither is
    representable, and ONE gate holds the law for both surfaces: `noun` is
    required rather than defaulted so a third caller cannot inherit the wrong
    sentence by saying nothing.
    """
    if not present and absence is None:
        raise InvalidRunRequest(
            f"{subject!r} carries no {noun} and no reason for their absence"
        )
    if present and absence is not None:
        raise InvalidRunRequest(f"{subject!r} carries {noun} beside an absence reason")


def _window_quota(
    window: terminal.WindowUsage, observed_at: int, now: int
) -> WindowQuota:
    """One window's state. EXPIRED outranks STALE on purpose: a window that has
    reset is a fact about the NUMBER (it describes a period that is over),
    while staleness is a fact about how long ago we asked. The stronger claim
    about the number itself is the one the reader needs first."""
    if window.resets_at <= now:
        state = QuotaState.EXPIRED
    elif now - observed_at > QUOTA_FRESHNESS_S:
        state = QuotaState.STALE
    else:
        state = QuotaState.OBSERVED
    return WindowQuota(
        window=window.window,
        utilization=window.utilization,
        resets_at=window.resets_at,
        observed_at=observed_at,
        state=state,
    )


def _worst(states: Iterable[QuotaState]) -> QuotaState:
    """The state furthest from a current number. Never called on nothing."""
    return max(states, key=lambda state: _STATE_DISTANCE[state])


def _resolved_windows(
    windows: tuple[terminal.WindowUsage, ...],
    observed_at: int,
    source: str,
    now: int,
) -> _Resolved:
    quotas = tuple(_window_quota(window, observed_at, now) for window in windows)
    return _Resolved(
        state=_worst(quota.state for quota in quotas),
        windows=quotas,
        source=source,
        fetched_at=observed_at,
    )


def _resolve_quota(
    provider: str,
    account_alias: str | None,
    stored: tuple[AccountUsageRow, ...],
    cooling: bool,
    source: QuotaSource | None,
    refresh: int,
    now: int,
) -> _Resolved:
    """What is known about ONE target, and why, in one pass.

    The order is the whole contract. A live wall means the vendor has already
    refused and named the instant it will answer again: a pull before then is
    not an attempt, it is a wasted one, so `refresh` does NOT reach past it. A
    source that answers with an absence never erases a number we already hold —
    the stored reading is served and labelled instead, and so is it where no
    source is wired to ask. And an account with nothing stored is
    NEVER_OBSERVED, which is not 0.0 and not NO_SOURCE: nobody asked, so
    nobody was refused.
    """
    subject = provider if account_alias is None else account_alias
    pull = refresh > 0 and not cooling
    if pull and source is not None:
        reading = source(provider, account_alias)
        if reading.absence is None:
            return _resolved_windows(
                reading.windows, reading.fetched_at, reading.source, now
            )
        if not stored:
            # The source that could not answer is named: a failed read is not
            # "nothing reads it" (its own words stay in `absence`).
            return _Resolved(
                state=QuotaState.NO_SOURCE,
                source=None if reading.source == "none" else reading.source,
                absence=reading.absence,
            )
    if pull and not stored:
        return _Resolved(
            state=QuotaState.NO_SOURCE,
            absence=f"no quota source is wired for {subject!r}",
        )
    if stored:
        windows = tuple(
            _window_quota(
                terminal.WindowUsage(row.usage_window, row.utilization, row.resets_at),
                row.observed_at,
                now,
            )
            for row in stored
        )
        return _Resolved(
            state=_worst(window.state for window in windows),
            windows=windows,
            source=RUN_WIRE_SOURCE,
            fetched_at=max(row.observed_at for row in stored),
        )
    return _Resolved(
        state=QuotaState.NEVER_OBSERVED,
        absence=f"no quota reading has ever been recorded for {subject!r}",
    )


def _account_quota(
    provider: str,
    alias: str,
    stored: tuple[AccountUsageRow, ...],
    wall: AccountWallRow | None,
    provider_walled: bool,
    source: QuotaSource | None,
    refresh: int,
    now: int,
) -> AccountQuota:
    """One account's answer. A wall on EITHER the account or its provider
    suppresses the pull: the vendor refused, and it refused for the credential,
    not for the caller that asked twice."""
    resolved = _resolve_quota(
        provider,
        alias,
        stored,
        provider_walled or wall is not None,
        source,
        refresh,
        now,
    )
    return AccountQuota(
        account_alias=alias,
        state=resolved.state,
        windows=resolved.windows,
        source=resolved.source,
        absence=resolved.absence,
        fetched_at=resolved.fetched_at,
        cooling_until=None if wall is None else wall.cooling_until,
        wall_source=None if wall is None else wall.source,
        wall_limit_type=None if wall is None else wall.limit_type,
    )


def _with_wall(account: AccountQuota, wall: AccountWallRow | None) -> AccountQuota:
    """`account` reporting `wall` as its cooldown, or none."""
    return replace(
        account,
        cooling_until=None if wall is None else wall.cooling_until,
        wall_source=None if wall is None else wall.source,
        wall_limit_type=None if wall is None else wall.limit_type,
    )


def _provider_quota(
    provider: Provider,
    stored: dict[str, tuple[AccountUsageRow, ...]],
    wall: ProviderWallRow | None,
    account_walls: dict[str, AccountWallRow],
    source: QuotaSource | None,
    refresh: int,
    now: int,
) -> ProviderQuota:
    """One provider's answer: per account where it has accounts, per PROVIDER
    where it routes by env. The env route has no alias to attribute a reading
    to, so it never has a stored one — `account_usage` is keyed by alias, and
    inventing a key for it is the defect `_spawn_and_wait` already refuses."""
    accounts = tuple(
        _account_quota(
            provider.name,
            alias,
            stored.get(alias, ()),
            account_walls.get(alias),
            wall is not None,
            source,
            refresh,
            now,
        )
        for alias in provider.discovered_accounts()
    )
    resolved = (
        _Resolved(state=_worst(account.state for account in accounts))
        if accounts
        else _resolve_quota(
            provider.name, None, (), wall is not None, source, refresh, now
        )
    )
    return ProviderQuota(
        provider=provider.name,
        models=tuple(provider.models),
        state=resolved.state,
        default_model=provider.model,
        accounts=accounts,
        windows=resolved.windows,
        source=resolved.source,
        absence=resolved.absence,
        fetched_at=resolved.fetched_at,
        cooling_until=None if wall is None else wall.cooling_until,
        wall_source=None if wall is None else wall.source,
    )


#: The `nspawn quota record` timer interval (ops/systemd/user/nspawn-quota-record.timer).
COMPUTE_INTERVAL_S: Final = 60
#: Past ten intervals a reading is reported stale: the timer is late or dead.
COMPUTE_STALE_S: Final = 10 * COMPUTE_INTERVAL_S


def _with_compute(
    entry: Provider, quota: ProviderQuota, row: ComputeBalanceRow | None, now: int
) -> ProviderQuota:
    """`quota` with the stored balance of `entry.quota_source`. Reads only."""
    if entry.quota_source is None:
        return quota
    if row is None:
        return replace(
            quota, compute_absence="not read yet: nspawn quota record has not run"
        )
    if row.balance is None:
        return replace(
            quota,
            compute_absence="no reading has succeeded",
            compute_failure=row.failure,
            compute_failed_at=row.failed_at,
        )
    assert row.observed_at is not None
    assert row.rate_per_hour is not None and row.active_assignments is not None
    age = now - row.observed_at
    if age < 0:
        # A reading from the future: the clock moved back. Never shown as fresh.
        return replace(
            quota,
            compute=quota_sources.ComputeBalance(
                row.balance, row.rate_per_hour, row.active_assignments, row.observed_at
            ),
            compute_updated=f"updated {-age} s in the future: the clock moved back",
            compute_stale=True,
            compute_failure=row.failure,
            compute_failed_at=row.failed_at,
        )
    return replace(
        quota,
        compute=quota_sources.ComputeBalance(
            row.balance, row.rate_per_hour, row.active_assignments, row.observed_at
        ),
        compute_updated=_age(age),
        compute_stale=age > COMPUTE_STALE_S,
        compute_failure=row.failure,
        compute_failed_at=row.failed_at,
    )


def pull_compute(registry: Registry, roster: Roster) -> dict[str, str]:
    """Read every declared balance source once and store it: `nspawn quota record`.

    Each provider gets "ok" or the failure. A failure keeps the last good
    reading; the store refuses a reading older than the stored one.
    """
    outcome: dict[str, str] = {}
    for entry in roster.providers:
        if entry.quota_source != "colab":
            continue
        now = int(time.time())
        try:
            reading = quota_sources.read_colab_usage(now)
        except quota_sources.QuotaSourceError as error:
            registry.record_compute_failure(entry.name, error.message, now)
            outcome[entry.name] = error.message
            continue
        try:
            registry.record_compute_reading(
                ComputeBalanceRow(
                    provider=entry.name,
                    balance=reading.balance,
                    rate_per_hour=reading.rate_per_hour,
                    active_assignments=reading.active_assignments,
                    observed_at=reading.fetched_at,
                    failure=None,
                    failed_at=None,
                )
            )
        except StaleComputeReading as error:
            outcome[entry.name] = error.message
            continue
        outcome[entry.name] = "ok"
    return outcome


def _age(seconds: int) -> str:
    if seconds < 120:
        return f"updated {max(seconds, 0)} s ago"
    if seconds < 7200:
        return f"updated {seconds // 60} min ago"
    return f"updated {seconds // 3600} h ago"


def _declared_providers(
    provider: str | None, *, roster: Roster | None = None
) -> list[Provider]:
    """Every provider the roster declares, or the ONE named — refused by name.

    A provider the roster does not declare is REFUSED, never answered with an
    empty listing: "no entry for `nope`" would read as "`nope` serves nothing"
    and "`nope` has no quota", which are different and false statements. One
    gate, because both roster verbs ask the same question of the same file.
    """
    declared = (roster if roster is not None else _read_roster()).providers
    if provider is None:
        return declared
    named = [entry for entry in declared if entry.name == provider]
    if not named:
        # The refusal CARRIES the vocabulary: without this the caller has no
        # alternative but to read `~/.nott/spawn.toml` from disk. `picker.py`
        # already did this for `nspawn_run_start`, and two surfaces of the same
        # roster telling different stories is a defect, not an economy.
        raise InvalidRunRequest(
            f"provider {provider!r} is not declared in the roster; "
            f"it declares [{', '.join(entry.name for entry in declared)}]"
        )
    return named


def _account_absence(provider: Provider) -> str:
    """Why a provider carries no account alias, in its own terms.

    NEITHER BRANCH IS A DEFECT. Eight of the nine live providers authenticate
    from the environment and have no account store to discover at all; the
    second is a declared directory that answered nothing, which is a different
    fact about a different roster line. `discovered_accounts` collapses every
    `OSError` to no entries, so the second sentence claims only that no
    readable `.token` was found — never that the directory was empty.
    """
    directory = provider.secrets_dir()
    if directory is None:
        return (
            f"provider {provider.name!r} declares no secrets directory; "
            "it authenticates from the environment"
        )
    return (
        f"provider {provider.name!r} declares secrets directory {directory}, "
        "where no readable .token file was found"
    )


def _provider_listing(provider: Provider, roster: Roster) -> ProviderListing:
    accounts = tuple(provider.discovered_accounts())
    catalog = None
    catalog_error = None
    if provider.kind == "claude" and any(
        claude_family(model) is not None for model in provider.models
    ):
        try:
            catalog = provider.catalog
        except claude_catalog.ModelCatalogError as error:
            catalog_error = error.detail or str(error)
    return ProviderListing(
        provider=provider.name,
        kind=provider.kind,
        capabilities=tuple(
            capability
            for capability in CAPABILITIES[provider.kind]
            if capability not in CAGED
            or provider.kind != "claude"
            or not _claude_route_elsewhere(
                roster.env_of(provider), provider.accounts.values()
            )
        ),
        models=tuple(provider.models),
        default_model=provider.model,
        # What the route SERVES, not everything the roster line names: a report
        # that advertises an effort the picker refuses sends the caller to read
        # the roster for an answer this verb already had.
        efforts=tuple(picker.served_efforts(provider)),
        accounts=accounts,
        absence=None if accounts else _account_absence(provider),
        strength=provider.strength,
        thrift=provider.thrift,
        design=provider.design,
        model_catalog=catalog,
        model_catalog_error=catalog_error,
    )


def provider_report(provider: str | None = None) -> ProviderReport:
    """List roster routes without opening the registry or reading quota.

    Claude family routes include native client model metadata. This bounded
    control read has no prompt or credential. The answer remains available
    when the registry is down.
    """
    roster = _read_roster()
    return ProviderReport(
        providers=tuple(
            _provider_listing(entry, roster)
            for entry in _declared_providers(provider, roster=roster)
        ),
        roles=dict(roster.roles),
        role_profiles={role: dict(p) for role, p in roster.role_profiles.items()},
    )


def _composed_warning(current: str | None, incoming: str) -> str:
    """Warnings ACCUMULATE by segment: a later writer never erases an earlier.

    Both sides arrive already composed (`"; "`-joined), so comparing whole
    strings would either repeat a segment the row already carries or drop a new
    one that travelled beside an old one.
    """
    segments = [part for part in (current or "").split("; ") if part]
    segments += [part for part in incoming.split("; ") if part and part not in segments]
    return "; ".join(segments)


def _open_regular(directory: int, name: str, flags: int) -> int:
    """`name` inside the run directory `directory`: never through a symlink,
    never blocking on a FIFO, and only a regular file; anything else raises
    OSError. A hard link is a regular file and passes.

    The one opener for `late_writes`, `terminal.json`, `final_text.partial` and
    `final_text` (written, checked, and read back by `run_result`). The notice
    spill creates its files through the run directory's descriptor with
    O_EXCL|O_NOFOLLOW. The streams and prompt files keep their own opens: a
    process of the same uid planting files in this 0700 directory already holds
    the store, the roster and this code, so it is outside the threat boundary."""
    fd = os.open(
        name,
        flags | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
        0o600,
        dir_fd=directory,
    )
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, f"{name} is not a regular file")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _late_write(
    run_id: str, state: str, phase: RunPhase, fields: Mapping[str, object]
) -> str:
    """The diagnostic line for a phase write that was not applied."""
    return (
        f"run {run_id} {state}: a {phase.value} write was not applied; "
        f"reason={fields.get('blocked_reason')!r} warning={fields.get('warning')!r}"
    )


def _driver_alive(pidfile: Path) -> bool:
    """The `drivers/<uuid>.pid` record names a live process; unreadable = no."""
    try:
        record = json.loads(pidfile.read_text(encoding="utf-8"))
        identity = ProcessIdentity(
            record["pid"], record["boot_id"], record["start_ticks"]
        )
    except OSError, ValueError, TypeError, KeyError:
        return False
    return process_identity_matches(identity)


def prune_driver_files(directory: Path, older_than_s: float) -> int:
    """Drop the `drivers/<uuid>.*` files of launches untouched for
    `older_than_s` whose driver is gone; returns the launches pruned.

    `driver.drivers_dir` names this directory. The retention is the closed
    runs' own: a driver's log outliving its run by more is only inodes.
    """
    cutoff = time.time() - older_than_s
    launches: dict[str, list[Path]] = {}
    try:
        for path in directory.iterdir():
            launches.setdefault(path.name.split(".", 1)[0], []).append(path)
    except FileNotFoundError:
        return 0
    pruned = 0
    for launch, paths in launches.items():
        try:
            if max(path.lstat().st_mtime for path in paths) > cutoff:
                continue
        except FileNotFoundError:
            continue
        if _driver_alive(directory / f"{launch}.pid"):
            continue
        for path in paths:
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
        pruned += 1
    return pruned


def create_run_directory(run_root: Path, run_id: str, *, dir_fd: int) -> RunDirectory:
    """Create one private 0700 run directory via the held run-root descriptor.

    ``run_root`` names the returned paths; the bind is ``mkdirat`` on ``dir_fd``.
    """
    if not run_id or "/" in run_id or run_id in (".", "..") or "\x00" in run_id:
        raise InvalidRunRequest(f"run_id {run_id!r} is not a safe directory name")
    root = run_root / run_id
    try:
        os.mkdir(run_id, 0o700, dir_fd=dir_fd)
    except FileExistsError as error:
        raise RunDirectoryError(f"run directory {root} already exists") from error
    except OSError as error:
        raise RunDirectoryError(
            f"run directory {root} could not be created: {error}"
        ) from error
    return RunDirectory(root=root, stdout=root / "stdout", stderr=root / "stderr")


def _cleanup_run_dir(run_dir: RunDirectory) -> None:
    """Best-effort tidy of a directory whose admission never went further.

    Only ever called before anything has written into it, so it is still empty.
    """
    with contextlib.suppress(OSError):
        run_dir.root.rmdir()


def _require_text(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise InvalidRunRequest(f"{name} must be a non-empty string")


def _worktree_head(cwd: str) -> str | None:
    """The HEAD text of the git worktree at cwd; None outside a readable repository."""
    try:
        repo_root = worktree.discover_repository_root(Path(cwd))
    except worktree.RepositoryRootNotFound, OSError:
        return None

    git_path = repo_root / ".git"
    head_path: Path | None = None
    if git_path.is_dir():
        head_path = git_path / "HEAD"
    elif git_path.is_file():
        try:
            content = git_path.read_text(encoding="utf-8").strip()
            if content.startswith("gitdir:"):
                gitdir_raw = content[len("gitdir:") :].strip()
                gitdir = worktree._resolve_gitdir(repo_root, gitdir_raw)
                head_path = gitdir / "HEAD"
        except OSError:
            return None

    if head_path is None or not head_path.is_file():
        return None

    try:
        return head_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def review_branch(cwd: str) -> str:
    """The branch checked out at cwd: the branch of a review that names none."""
    head = _worktree_head(cwd)
    if head is None or not head.startswith("ref:"):
        raise InvalidRunRequest(
            f"give branch: the worktree at {cwd!r} has no branch checked out "
            "(detached HEAD or no readable git repository)"
        )
    branch = head[len("ref:") :].strip().removeprefix("refs/heads/")
    # The name reaches a git argv and a prompt: no option, no line break.
    if (
        not branch
        or branch.startswith("-")
        or any(c.isspace() or not c.isprintable() for c in branch)
    ):
        raise InvalidRunRequest(
            f"give branch: the worktree at {cwd!r} has HEAD on {branch!r}, "
            "which is not a usable branch name"
        )
    return branch


def agent_system_prompt(agent: str, cwd: str) -> str:
    """The body of `agents/<agent>.md` in the roster's plugin root, without frontmatter."""
    roster = _read_roster(cwd)
    if roster.defaults.plugin_root is None:
        raise InvalidRunRequest(
            "agent needs a plugin root: the roster sets no defaults.plugins"
        )
    directory = expand_home(roster.defaults.plugin_root) / "agents"
    if not directory.is_dir():
        raise InvalidRunRequest(
            f"agent {agent!r}: the agents directory {directory} does not exist"
        )
    # Only a name found in the directory is read, so a path in `agent` reads nothing.
    known = sorted(path.stem for path in directory.glob("*.md"))
    name = agent.removeprefix("nott:")
    if name not in known:
        raise InvalidRunRequest(
            f"unknown agent {agent!r} in {directory}. "
            f"Known agents: {', '.join(known) or 'none'}"
        )
    try:
        path = (directory / f"{name}.md").resolve(strict=True)
        if not path.is_relative_to(directory.resolve(strict=True)):
            raise InvalidRunRequest(
                f"agent {agent!r}: the file is a link out of {directory}"
            )
        # The owner-config read: no-follow, regular file only (a FIFO is
        # refused, not waited on), owner-held, and bounded.
        text = read_owner_config(path).decode("utf-8").replace("\r\n", "\n")
    except (OSError, RuntimeError, UnicodeDecodeError, VendorHomeError) as error:
        raise InvalidRunRequest(
            f"agent {agent!r}: cannot read its file in {directory}: "
            f"{type(error).__name__}"
        ) from None
    if text.startswith("---\n"):
        _, separator, text = text[4:].partition("\n---\n")
        if not separator:
            raise InvalidRunRequest(f"agent {agent!r} has frontmatter with no end")
    return text.lstrip("\n")


def _validate_review_worktree_branch(cwd: str, branch: str) -> None:
    """Ensure the worktree at cwd is on the requested branch, if in a git repository."""
    head_content = _worktree_head(cwd)
    if head_content is not None and head_content.startswith("ref:"):
        ref = head_content[len("ref:") :].strip()
        actual_branch = ref.removeprefix("refs/heads/")
        requested_branch = branch.removeprefix("refs/heads/")
        if actual_branch != requested_branch:
            raise InvalidRunRequest(
                f"cannot review branch {branch!r} in worktree at {cwd!r}: "
                f"worktree is on branch {actual_branch!r}"
            )


def _combine_system_prompt_file(
    cwd: str, destination: Path, declared_args: Sequence[str]
) -> list[str]:
    """Keep the roster's global prompt when one dispatch adds its own text."""
    flag = "--append-system-prompt-file"
    args = list(declared_args)
    if any(argument.startswith(f"{flag}=") for argument in args):
        raise SupervisorError(f"roster must declare {flag} and its path separately")
    count = args.count(flag)
    if count == 0:
        return args
    if count > 1:
        raise SupervisorError(f"roster declares {flag} more than once")
    index = args.index(flag)
    if index + 1 == len(args) or args[index + 1].startswith("--"):
        raise SupervisorError(f"roster declares {flag} without a path")
    source = Path(args[index + 1])
    if not source.is_absolute():
        source = Path(cwd) / source
    global_prompt = source.read_bytes()
    suffix = b"\n\n" + global_prompt
    descriptor = os.open(
        destination,
        os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW,
    )
    with os.fdopen(descriptor, "a+b") as prompt_file:
        # _drive_run reuses this file when another account or session id is tried.
        # Read only the expected suffix through the same no-follow descriptor.
        if prompt_file.tell() >= len(suffix):
            prompt_file.seek(-len(suffix), os.SEEK_END)
            if prompt_file.read() == suffix:
                del args[index : index + 2]
                return args
        prompt_file.write(suffix)
    del args[index : index + 2]
    return args


def _bounded_text(text: str, max_bytes: int) -> str:
    """Truncate to at most `max_bytes` UTF-8 bytes without splitting a codepoint."""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


#: The run-directory file holding a final answer the row could not hold whole.
FINAL_TEXT_FILE = "final_text"
#: A run's late-write diagnostics: appended, never on stderr, so no pipe stalls them.
LATE_WRITES_FILE = "late_writes"
#: One bound for keeping and reading an answer whole: no run outputs more.
KEPT_ANSWER_MAX_BYTES: Final = pump.MAX_RUN_OUTPUT_BYTES
#: A terminal the store refused, kept beside the run for the operator.
TERMINAL_FILE = "terminal.json"


#: The phases a run never leaves except by a close; ORPHANED is provisional.
_ENDED = frozenset({RunPhase.COMPLETED, RunPhase.FAILED, RunPhase.CLOSED})
#: The phases an annotation may write again: its own, never another.
_ANNOTATED = frozenset({RunPhase.COMPLETED, RunPhase.FAILED})
#: The `RunRow` columns capped at `MAX_DURABLE_OBSERVATION_BYTES`.
_OBSERVATION_FIELDS = ("final_text", "warning", "blocked_reason")


@dataclass(frozen=True, slots=True)
class _StreamObservation:
    has_result: bool
    has_valid_result: bool
    final_text: str | None
    stdout_mtime_ns: int | None
    stderr_tail: str | None
    #: The vendor's HTTP status when the terminal frame carried one. `terminal`
    #: already separates 402 (quota exhausted) from a broken transport and the
    #: `_TerminalResult` read below carries the integer; keeping only `.text`
    #: left every reader with `failure_reason` to substring-match, which is a
    #: guess dressed as a classification.
    api_error_status: int | None = None
    #: The vendor's TYPED quota channel, which carries no HTTP status at all:
    #: `rate_limit_event` with `status == "rejected"` is what `terminal.py`
    #: turns into an account wall. A reader that watched only
    #: `api_error_status` saw a five-hour ceiling as an ordinary failure.
    #:
    #: ponytail: read from the stdout TAIL, like every other field here. The
    #: `result` frame is last by construction so its window is safe; a
    #: `rate_limit_event` is not, and a rejection followed by enough frames
    #: falls out of the window and reads as an ordinary failure again. Upgrade
    #: path if that is ever observed: `classify_terminal_run` reads the whole
    #: stream and already has the answer — persist it instead of re-observing.
    rate_limited: bool = False
    structured_output: dict[str, Any] | None = None


def _read_tail(path: Path, maximum: int) -> tuple[bytes, int, bool] | None:
    try:
        with path.open("rb") as stream:
            stat = os.fstat(stream.fileno())
            start = max(0, stat.st_size - maximum)
            starts_mid_line = False
            if start:
                stream.seek(start - 1)
                starts_mid_line = stream.read(1) != b"\n"
            stream.seek(start)
            return (
                stream.read(min(stat.st_size, maximum)),
                stat.st_mtime_ns,
                starts_mid_line,
            )
    except OSError:
        return None


def _read_kept_answer(
    run_dir: Path, limit: int | None = None
) -> tuple[str, int] | None:
    """The whole answer `_keep_whole_answer` kept, read through the same opener
    and bound, or None when there is none: the row's answer is then whole, or
    the prefix its warning names as not kept. Answers `(text, file size)`; with
    `limit`, the text is the file's first `limit` bytes at most, a codepoint cut
    at its end held back unless the read reached the file's end. Anything else
    is a `KeptAnswerError`, never a silent fallback to the row's prefix."""
    try:
        directory = os.open(
            run_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        )
        try:
            fd = _open_regular(directory, FINAL_TEXT_FILE, os.O_RDONLY)
        finally:
            os.close(directory)
        with open(fd, "rb") as stream:
            # Sized by the file: `read(n)` reserves n bytes before it reads.
            size = os.fstat(stream.fileno()).st_size
            if limit is None:
                data = stream.read(min(size, KEPT_ANSWER_MAX_BYTES) + 1)
            else:
                data = stream.read(min(size, limit))
        if len(data) > KEPT_ANSWER_MAX_BYTES or size > KEPT_ANSWER_MAX_BYTES:
            raise KeptAnswerError(
                f"{FINAL_TEXT_FILE} of run {run_dir.name} is over the "
                f"{KEPT_ANSWER_MAX_BYTES} bytes a kept answer may hold"
            )
        decoder = codecs.getincrementaldecoder("utf-8")()
        return decoder.decode(data, final=len(data) >= size), size
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as error:
        raise KeptAnswerError(
            f"{FINAL_TEXT_FILE} of run {run_dir.name} cannot be served whole: "
            f"{type(error).__name__}: {error}"
        ) from error


def may_keep_answer(row: RunRow) -> bool:
    """`row`'s answer may be a cut prefix of one kept whole beside it: it
    completed (a close keeps its result) and its answer reaches the cut.
    `_bounded_text` drops at most a 3-byte codepoint tail at the cut."""
    text = row.final_text
    return (
        row.phase in (RunPhase.COMPLETED, RunPhase.CLOSED)
        and text is not None
        and len(text.encode("utf-8")) >= MAX_DURABLE_OBSERVATION_BYTES - 3
    )


def kept_answer(row: RunRow, run_dir: Path) -> str | None:
    """The whole answer kept beside `row` when `may_keep_answer`; None
    otherwise, and nothing in its run directory is read then. Raises
    `KeptAnswerError`."""
    if not may_keep_answer(row):
        return None
    kept = _read_kept_answer(run_dir)
    return None if kept is None else kept[0]


def answer_head(row: RunRow, run_dir: Path | None, limit: int) -> tuple[str, int]:
    """The answer a notice shows, and its whole size in bytes. Every notice
    reads its answer here; only `run_result` reads a kept one whole. A row
    answer that may be a cut prefix is read from the file kept beside it, its
    first `limit` bytes at most; one that cannot be read (no `run_dir`, no
    file, or a `KeptAnswerError`) raises `KeptAnswerError`, whatever the row's
    warning says: an answer at the cap may be a cut prefix. Otherwise the
    row's own answer, under the cap, is the whole."""
    if may_keep_answer(row):
        kept = None if run_dir is None else _read_kept_answer(run_dir, limit)
        if kept is None:
            raise KeptAnswerError(
                f"the answer of run {row.run_id} reaches the row's cap and no "
                "whole answer is kept beside it"
            )
        return kept
    text = row.final_text or ""
    return text, len(text.encode("utf-8"))


def stderr_tail(run_dir: Path) -> str | None:
    """The bounded tail of the run's stderr, or None when it has none."""
    stderr = _read_tail(run_dir / "stderr", MAX_DURABLE_OBSERVATION_BYTES)
    return None if stderr is None else stderr[0].decode("utf-8", errors="ignore")


def run_result(
    row: RunRow,
    run_dir: Path,
    *,
    inference: bool = False,
    profile: StructuredInference | None = None,
) -> RunResult:
    """What `nspawn_result_show` answers for `row`, read from `run_dir`."""
    streams = _observe_streams(run_dir)
    structured_output = streams.structured_output
    if (
        inference
        and row.backend is Backend.CLAUDE
        and row.phase.is_terminal()
        and streams.stdout_mtime_ns is not None
    ):
        result, _, _, _ = terminal._stdout_evidence(
            terminal._read_evidence(
                run_dir / "stdout", "stdout", pump.MAX_RUN_OUTPUT_BYTES
            )
        )
        structured_output = None if result is None else result.structured_output
    final_text = streams.final_text if streams.has_valid_result else row.final_text
    if not streams.has_valid_result:
        # An answer over the row's cap lives whole in the run directory.
        kept = kept_answer(row, run_dir)
        if kept is not None:
            final_text = kept
    if (
        profile is not None
        and row.backend is Backend.CODEX
        and row.phase in {RunPhase.COMPLETED, RunPhase.CLOSED}
        and row.blocked_reason is None
        and final_text is not None
    ):
        try:
            structured_output = profile.parse_output(final_text)
        except ValueError:
            raise KeptAnswerError(
                "the complete native structured answer is invalid"
            ) from None
    return RunResult(
        run_id=row.run_id,
        final_text=final_text,
        stderr_tail=streams.stderr_tail,
        failure_reason=row.blocked_reason,
        structured_output=structured_output,
        phase=row.phase,
    )


def _observe_streams(run_dir: Path) -> _StreamObservation:
    """Read bounded stream evidence without changing the registry record."""
    stdout = _read_tail(run_dir / "stdout", terminal.LIVE_PROGRESS_TAIL_BYTES)
    has_result = False
    has_valid_result = False
    final_text = None
    stdout_mtime_ns = None
    api_error_status: int | None = None
    rate_limited = False
    structured_output: dict[str, Any] | None = None
    if stdout is not None:
        value, stdout_mtime_ns, starts_mid_line = stdout
        lines = value.split(b"\n")
        if starts_mid_line:
            lines = lines[1:]
        for line in lines:
            try:
                loaded: object = json.loads(line)
            except UnicodeDecodeError, ValueError, RecursionError:
                continue
            if not isinstance(loaded, dict):
                continue
            frame = cast(dict[str, object], loaded)
            if frame.get("type") == "rate_limit_event":
                # DECLARED, NEVER DUPLICATED: `terminal._rate_limit` is the
                # one parser for this frame, and the one `classify_terminal_run`
                # itself uses.
                try:
                    limit = terminal._rate_limit(frame)
                except terminal.TerminalClassificationError:
                    # Defense in depth: no measured stream produces this
                    # shape. The three wire corpora (2.1.241/250/251) pass
                    # `_rate_limit` without a refusal. The test drives a
                    # MALFORMED frame (non-object `rate_limit_info`), never a
                    # claimed vendor shape: `RULEBOOK.md` §L5 "Wire authority" forbids
                    # accepting the flattened form as parity
                    # (`test_ping.py::test_a_malformed_rate_limit_frame_does_not_sink_the_observation`).
                    # It stays because removing it would trade visible debt for
                    # invisible risk: a malformed frame would drown the
                    # entire observation, and this is the bounded re-read that cannot
                    # sink — `classify_terminal_run` calls the SAME
                    # parser and refuses the run, which is the right behavior THERE.
                    continue
                rate_limited = isinstance(limit, terminal._RejectedRateLimit)
                continue
            if frame.get("type") != "result":
                continue
            has_result = True
            try:
                result = terminal._terminal_result(frame)
            except terminal.TerminalClassificationError:
                continue
            has_valid_result = True
            final_text = result.text
            api_error_status = result.api_error_status
            structured_output = result.structured_output

    tail = stderr_tail(run_dir)
    return _StreamObservation(
        has_result=has_result,
        has_valid_result=has_valid_result,
        final_text=final_text,
        stdout_mtime_ns=stdout_mtime_ns,
        stderr_tail=tail,
        api_error_status=api_error_status,
        rate_limited=rate_limited,
        structured_output=structured_output,
    )


def _default_review_prompt(branch: str, base: str | None) -> str:
    if base is None:
        return f"Review branch {branch} read-only; do not modify the worktree."
    return (
        f"Review branch {branch} against base {base} read-only; "
        "do not modify the worktree."
    )


def _read_roster(cwd: str | None = None) -> Roster:
    # `cwd` is accepted only so the three call sites (`:641`, `:982`, `:1071`)
    # keep their signature; under L3 (owner ruling 2026-08-29) the roster is
    # `NOTT_NSPAWN_ROSTER` or the config file's `spawn.toml` and nothing
    # else, so the argument is otherwise ignored. A missing or unparseable
    # roster is a typed refusal naming both homes the operator may declare.
    path = Roster.default_path(Path(cwd) if cwd is not None else None)
    if path is None:
        raise NSpawnError(
            NERR_1104,
            f"cannot resolve the provider roster: single config file {config.store_config_path()} "
            "is absent and NOTT_NSPAWN_ROSTER is unset",
        )
    # A named roster that is absent is a REFUSAL, wherever the name came from: the
    # operator pointed to a file and it is not there, and silence would serve
    # a roster they never chose. Under file-wins there is no longer a
    # "derived absence": declaring `spawn.toml` IS naming. What remains
    # as an empty page is the roster that EXISTS and declares no provider,
    # and that is the rule of `Roster.read` itself.
    # The guard asks about the SOURCE THAT WON, not the one that lost. Previously
    # it always asked for `NOTT_NSPAWN_ROSTER`: an absence declared by
    # file passed in silence, and a merely present env invented an
    # NERR_1104 that named the wrong variable and a path it does not carry.
    if not path.is_file():
        fonte = (
            "NOTT_NSPAWN_CONFIG (single config file)"
            if config.resolve_roster_path() is not None
            else "NOTT_NSPAWN_ROSTER"
        )
        raise NSpawnError(
            NERR_1104,
            f"cannot resolve the provider roster: {fonte} points at {path} which does not exist",
        )
    return Roster.read(path)


def _lane_of(cwd: str) -> str | None:
    "The lane a run executes in: the linked worktree containing ``cwd``, by name."
    try:
        root = worktree.discover_repository_root(Path(cwd))
    except worktree.WorktreeError:
        return None
    return root.name if (root / ".git").is_file() else None


def _claude_bin_override() -> Path | None:
    value = os.environ.get(CLAUDE_BIN_ENV)
    return Path(value) if value else None


def _probe_claude_version(path: str) -> claude_child.Version:
    """The `Probe` L8's `inspect_claude_installation` requires.

    No lane has shipped a real one yet ("the runner is on lane/spawn-d2"); L7
    composes L8 and something must supply this callable, so this is the minimal
    subprocess probe until spawn-d2 lands its own.
    """
    try:
        completed = subprocess.run(
            [path, "--version"],
            capture_output=True,
            text=True,
            timeout=claude_child.VERSION_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise SupervisorError(
            f"claude --version probe failed for {path}: {error}"
        ) from error
    version = claude_child.parse_version(completed.stdout)
    if version is None:
        raise SupervisorError(
            f"claude --version at {path} produced unparseable output: "
            f"{completed.stdout!r}"
        )
    return version


def _live(row: RunRow, run_root: Path) -> terminal.LiveProgress:
    if row.phase.is_terminal():
        return terminal.LiveProgress()
    return terminal.observe_live_progress(run_root / row.run_id / "stdout")


def _live_tokens(row: RunRow, run_root: Path) -> int:
    return max(row.tokens, _live(row, run_root).tokens)


def _live_turns(row: RunRow, run_root: Path) -> int:
    return max(row.turn_count, _live(row, run_root).turns)


def _child_addr(row: RunRow) -> str | None:
    """The Claude child's own session address, or None for other backends.

    FENCED BY THE PID TRIPLE, not by the pid. `_update_phase` never clears
    `process_identity` on a natural exit — only an explicit `close()` does — so
    a COMPLETED row keeps naming a pid the kernel is free to hand to somebody
    else, and a caller subscribing to that address would land in a STRANGER'S
    session.

    The refusal is CAUGHT, and that is the whole point of this helper. Reading
    status could not fail on identity before this candidate, and
    `process_identity_matches` RAISES rather than answering False in three
    cases — a non-Linux host, and either /proc read hitting an OSError that is
    not ENOENT. The measured one is the teardown race the sibling caller
    already documents at `_wait_identity_dead`: `/proc/<pid>/stat` opens fine
    and then reads ESRCH. Letting that out would turn the ONE status read the
    new contract tells a caller to make into a typed refusal, at exactly the
    moment the child dies — the case the fence exists to serve. Absent is the
    honest answer, and it is the same answer the sibling reaches.
    """
    if row.backend is not Backend.CLAUDE:
        return None
    identity = row.process_identity
    if identity is None:
        return None
    try:
        alive = process_identity_matches(identity)
    except ProcessIdentityError:
        return None
    return child.child_socket_address(identity.pid) if alive else None


def _to_status(
    row: RunRow, run_root: Path, effort: EffortProfile = UNKNOWN_EFFORT
) -> RunStatus:
    """The row, plus what the live stream says when the row cannot say it yet.

    `tokens` is written ONCE, at terminal classification. Until then the row
    carries zero, and an orchestrator watching a ten-minute run watched a zero
    (F-015). The stream itself has the number the whole time, so a non-terminal
    read takes it from there — an observation of the wire, never a write back
    into the registry, which keeps exactly one author for the record.
    """
    run_dir = run_root / row.run_id
    streams = _observe_streams(run_dir)
    tokens = row.tokens
    total_seq = row.records_seen
    if not row.phase.is_terminal():
        live = terminal.observe_live_progress(run_dir / "stdout")
        tokens = max(tokens, live.tokens)
    if total_seq == 0:
        # NOT A RESET — AN OBSERVATION THAT STOPPED. `records_seen` has exactly
        # one writer, `_spawn_and_wait`, and it writes AFTER a clean `wait()`:
        # a run that failed at event 690 never reached it, and no native run
        # reaches it at all. Gating the stream count on a non-terminal phase
        # therefore swapped the 690 a watcher had been reading for the row's
        # untouched zero at the exact moment the failure made it matter. The
        # file on disk is the evidence either way, and it is only read here
        # while the row itself carries nothing.
        try:
            data = (run_dir / "stdout").read_bytes()
            total_seq = _dropped_records(data.partition(b"\n")[0]) + data.count(b"\n")
        except OSError:
            pass
    return RunStatus(
        run_id=row.run_id,
        phase=row.phase,
        run_dir=str(run_dir),
        failure_reason=row.blocked_reason,
        total_seq=total_seq,
        requested_provider=row.requested_provider,
        executed_provider=row.executed_provider,
        executed_account=row.executed_account,
        warning=row.warning,
        tokens=tokens,
        marker=row.marker,
        has_result=row.final_text is not None or streams.has_result,
        stdout_mtime_ns=streams.stdout_mtime_ns,
        stderr_tail=streams.stderr_tail,
        api_error_status=streams.api_error_status,
        rate_limited=streams.rate_limited,
        # Derived, never stored: the pid the registry already holds IS the
        # address, so there is exactly one author for that fact.
        #
        # FENCED BY THE PID TRIPLE, not by the pid. `_update_phase` never clears
        # `process_identity` on a natural exit — only an explicit `close()`
        # does — so a COMPLETED row keeps naming a pid the kernel is free to
        # hand to somebody else, and the caller's subscription would land in a
        # STRANGER'S session. `process_identity_matches` is the same fence the
        # live judge already uses below; the address exists exactly while the
        # process behind it does.
        child_addr=_child_addr(row),
        backend=row.backend,
        thread_id=row.thread_id,
        group=row.group_name,
        effort=effort,
        model=row.model,
    )


_SESSION_COLLISION_MARKS: Final = (b"Session ID", b"is already in use")


def _session_id_collided(stderr: Path) -> bool:
    """Did THIS attempt die on the vendor's session-id collision refusal?"""
    try:
        text = stderr.read_bytes()
    except FileNotFoundError:
        return False
    return all(mark in text for mark in _SESSION_COLLISION_MARKS)


def _best_effort_killpg(pid: int) -> None:
    """The fallback custody path when no live `SpawnedChild` is held in memory.

    Reachable only across a supervisor restart: a row admitted by a prior
    instance still names a live process group, but this instance never spawned
    it and so never held its `SpawnedChild`.
    """
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, signal.SIGSTOP)
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, signal.SIGKILL)


def _discover_group_members(leader: ProcessIdentity) -> tuple[ProcessIdentity, ...]:
    """Every other live process sharing `leader`'s process group, by identity.

    ponytail: a process-group scan, not a PPID-chain walk — a descendant that
    called `setsid()` to leave the group is undiscoverable here. Upgrade to a
    PPID tree walk if a vendor child is ever observed doing that.
    """
    members: list[ProcessIdentity] = []
    for pid in group_member_pids(leader.pid):
        if pid == leader.pid:
            continue
        try:
            members.append(capture_process_identity(pid))
        except ProcessIdentityError:
            continue
    return tuple(members)


def _wait_identity_dead(identity: ProcessIdentity, timeout_s: float) -> bool:
    """Poll for death with a bounded deadline.

    A single check right after signalling would see a zombie: the pid is our
    own child, so until reaped its `/proc` entry still matches. The live judge
    itself polls up to 5s (`_identities_dead`); this mirrors it.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            alive = process_identity_matches(identity)
        except ProcessIdentityError:
            # A process actively being torn down can vanish BETWEEN the
            # directory listing and the stat read: `/proc/<pid>/stat` opens
            # fine and then reads ESRCH ("no such process"), which
            # process_identity.py surfaces as a refusal rather than the
            # ENOENT case it already treats as "gone". Reaching this line at
            # all means the earlier identity/boot-id read already succeeded
            # (see `close()`), so a failure THIS run always means the pid
            # went away mid-check, never an unreadable boot id.
            return True
        if not alive:
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.02, remaining))


class Supervisor:
    """Own run admission, lifecycle, transcripts and identity-bound close."""

    #: ponytail: a fixed claim lease, never renewed mid-run. `protected_custody`
    #: (the process triple staying on the row) is what actually keeps the
    #: worktree fenced across a long turn; this lease only matters in the
    #: narrow window before a process identity lands. Add a heartbeat loop if a
    #: turn commonly outlives this and the lease-expiry path is ever observed.
    RUN_CLAIM_LEASE_MS: Final = 30 * 60 * 1000
    CLOSE_POLL_TIMEOUT_S: Final = 5.0
    DEFAULT_TRANSCRIPT_MAX_BYTES: Final = 256 * 1024
    MAX_TRANSCRIPT_FRAME_TYPES: Final = 16
    #: U4 — orphan threshold for `mark_orphans`. A run whose owning instance
    #: has not refreshed `heartbeat_at` within this window is marked
    #: `orphaned` on the next tick. Default 30 s.
    STALE_AFTER_MS: Final = 30 * 1000
    #: U4 — how often the reaper thread wakes. Must be strictly less than
    #: STALE_AFTER_MS so the reaper gets at least one chance per window.
    TICK_INTERVAL_S: Final = 10.0
    #: Retention period for closed runs without custody before pruning. Default 24h.
    RETAIN_CLOSED_MS: Final = 24 * 60 * 60 * 1000
    #: Maximum closed runs retained in history. Default 1000.
    MAX_CLOSED_RUNS: Final = 1000

    #: A supervisor is SILENT unless the LIVE DOOR arms it. The terminal notice
    #: writes into the session that started this process, and a test suite that
    #: drives real runs shares that session: `tests/test_supervisor.py` alone
    #: reaches terminal 32 times, and one gate run put 32 unasked messages in
    #: the operator's conversation before this default existed. Announcing is a
    #: property of SERVING a caller, never of the class — so `_default_supervisor`
    #: turns it on and nothing else does.
    announces_terminal: bool = False
    #: `node:<session>` on a non-centre node's nSPAWN (`driver.node_owner`): every
    #: run it admits carries it, and it shows, reads, closes, resumes and lists
    #: only those. None everywhere else, which sees every run as before.
    owner: str | None = None
    #: An audit parent's grant (`driver.read_grant`): only caged dispatch.
    read_grant: bool = False
    #: Only a trusted host constructor can grant native session control.
    host_control: bool = False

    def __init__(
        self,
        registry: Registry,
        run_root: Path,
        *,
        announces_terminal: bool = False,
        sidecar_kind: str | None = None,
        owner: str | None = None,
        read_grant: bool = False,
        host_control: bool = False,
    ) -> None:
        self.announces_terminal = announces_terminal
        self.owner = owner
        self.read_grant = read_grant
        self.host_control = host_control
        #: `stdio` or `driver` for a v11 process; None for a bare supervisor
        #: (tests, tools) that never enters `sidecar_instances`.
        self._sidecar_kind = sidecar_kind
        if sidecar_kind is not None:
            registry.register_sidecar_instance(sidecar_kind, int(time.time() * 1000))
        #: The slice of each native write run this process drives.
        self._writers: dict[str, AggregateScope] = {}
        #: When each writer's tree was last nSPAWN's alone (`time.time_ns`):
        #: after its disposable home exists, before its first child. A change
        #: at or after it is the writer's (`_writer_stays`).
        self._write_baselines: dict[str, int] = {}
        self._registry = registry
        self._run_root_fd: int | None = None
        run_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._run_root_path = run_root
        self._run_root_fd = os.open(
            run_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
        self._lock = threading.Condition()
        self._children: dict[str, child.SpawnedChild] = {}
        self._sessions: dict[str, session.NativeSession] = {}
        self._commands: dict[str, Command] = {}
        #: Terminal phases the registry refused, retried by `publish_pending`.
        self._unpublished: dict[str, terminal.TerminalClassification] = {}
        #: Each live unpinned witness run, with what its next preference needs
        #: and the call that admitted it. Taken once, by its terminal write.
        self._failovers: dict[str, tuple[_Failover, DriveCall | None]] = {}
        # U4 — reaper wiring. `_tick` refreshes this instance's heartbeat and
        # orphans runs whose owning instance stopped proving it is alive. The
        # loop was the missing caller (registry.py:269, :718, :824 had zero
        # production callers before U4); wiring it here is the single act
        # that turns the dead code into a live signal. `stop_tick` is the
        # graceful shutdown signal; `_tick_thread` is the daemon that wakes
        # every TICK_INTERVAL_S until the supervisor closes. The thread name
        # is read lazily so a test double without `instance_id` can construct
        # a Supervisor without attribute errors.
        self._stop_tick = threading.Event()
        self._tick_thread = threading.Thread(
            target=self._tick_loop,
            name=self._tick_thread_name(registry),
            daemon=True,
        )
        self._tick_thread.start()

    @property
    def _run_root(self) -> int | None:
        fd = getattr(self, "_run_root_fd", None)
        return fd if isinstance(fd, int) else None

    @_run_root.setter
    def _run_root(self, run_root: Path) -> None:
        previous = getattr(self, "_run_root_fd", None)
        self._run_root_path = run_root
        if run_root.exists() and run_root.is_dir() and not run_root.is_symlink():
            self._run_root_fd = os.open(
                run_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            )
        else:
            self._run_root_fd = None
        if previous is not None:
            with contextlib.suppress(OSError):
                os.close(previous)

    @staticmethod
    def _tick_thread_name(registry: Registry) -> str:
        # `instance_id` may not exist on a test double; never let a debug
        # helper fail the daemon.
        instance_id = getattr(registry, "instance_id", "i-unknown")
        return f"nspawn-reaper-{instance_id}"

    # -- admission -----------------------------------------------------------

    def declared_providers(self, cwd: str) -> tuple[frozenset[str], Path | None]:
        """Declared provider names and the source of the same roster read.

        ONE read for a whole batch, and it happens before the first dispatch:
        a fan-out that discovers a typo halfway has already spent tokens on
        the entries it did reach.
        """
        _require_text("cwd", cwd)
        roster = _read_roster(cwd)
        return frozenset(provider.name for provider in roster.providers), roster.path

    def fast_models(self, cwd: str) -> dict[str, str | None]:
        """Every declared route at ``cwd``, mapped to its own fast lane.

        `None` means the route declares no `model_fast`; it is NOT a branch for
        the caller to resolve — the picker already owns "what does this route
        serve", and a second answer here would be a second authority.
        """
        return {
            provider.name: provider.model_fast
            for provider in _read_roster(cwd).providers
        }

    # -- U4 reaper wiring ----------------------------------------------------

    def _tick(self, now: int | None = None) -> tuple[bool, int]:
        """Refresh this instance's heartbeat and orphan stale runs.

        Returns ``(instance_alive, orphans_marked)``. Called by `_tick_loop`
        every ``TICK_INTERVAL_S`; exposed for tests and for any caller that
        wants a one-shot reaper pass (e.g. after admitting a run that just
        took a slot from a freed instance).

        The tick runs under `self._lock` so a heartbeat/refuse transaction
        cannot interleave with an admit or close on the same shared
        connection: two writes stepping on each other would nest one inside
        the other. A held lock is the cheapest fix; the daemon is a
        ten-second tick, contention with the dispatch path is bounded.
        """
        if now is None:
            now = int(time.time() * 1000)
        with self._lock:
            instance_alive = self._registry.heartbeat_instance(now)
            if not instance_alive:
                # A peer swept this row while this supervisor was quiet. The
                # answer was computed and thrown away before, so an UPDATE kept
                # matching zero rows for the life of the process and every run
                # this instance owned — including the ones that finished — was
                # orphaned by the next sweep. Measured 2026-08-31 on the live
                # registry: 59 instances with orphans, not one row left.
                self._registry.register_instance(now)
            if self._sidecar_kind is not None:
                self._registry.register_sidecar_instance(self._sidecar_kind, now)
                self._registry.sweep_sidecar_instances(now, self.STALE_AFTER_MS)
            orphans_marked = self._registry.mark_orphans(now, self.STALE_AFTER_MS)
            if hasattr(self._registry, "prune_closed_runs"):
                self._registry.prune_closed_runs(
                    now, self.RETAIN_CLOSED_MS, self.MAX_CLOSED_RUNS
                )
                self._registry.prune_notices()
        root = getattr(self, "_run_root_path", None)
        if isinstance(root, Path):
            prune_driver_files(root / "drivers", self.RETAIN_CLOSED_MS / 1000)
        # This process committed those transitions, so it pushes what they armed.
        # ponytail: every free ready row, not only the ones just orphaned; the
        # token claim keeps it at one delivery per live caller either way.
        if orphans_marked:
            self._announce()
        return instance_alive, orphans_marked

    def _tick_loop(self) -> None:
        """The reaper daemon: tick on a fixed interval until ``_stop_tick`` is set.

        A naive `time.sleep(self.TICK_INTERVAL_S)` is fine — the loop is
        best-effort, and the worst case is a single missed window, which the
        NEXT tick catches. Specific exceptions are logged and the loop
        carries on; the bare `Exception` catch that used to be here is
        removed because it violated the no-broad-except-in-persistence rule
        and hid permanent breakage (e.g. a closed connection) indefinitely.
        """
        # WAIT FIRST, tick after: `registry.open` just stamped this instance's
        # heartbeat, so the first refresh has a full interval of slack — and a
        # tick on the very first loop made the reaper's wall clock race every
        # test that admits runs on a synthetic one (the claim's lease_until,
        # written in test-milliseconds, reads as long-expired against
        # time.time(); mark_orphans then deletes it mid-test). The interval
        # delay makes that race impossible.
        #
        # `wait` IS the loop condition: it returns True exactly when the stop
        # signal arrived, so one branch decides both the sleep and the exit.
        # An `is_set()` guard on the `while` plus a `return` inside was two
        # conditions doing one job, and the `while`'s exit arm could never be
        # taken — an unreachable branch, which is the only kind coverage is
        # right to refuse.
        while not self._stop_tick.wait(self.TICK_INTERVAL_S):
            try:
                self._tick()
            except Exception as error:  # noqa: BLE001 — a best-effort daemon must never die mid-run; any failure is logged and the next tick retries
                # The store is busy past its wait, a table is gone, or a test
                # double lacks the interface — none of those can take the
                # supervisor down. A failed write rolled itself back.
                # Logged, never silent, never fatal.
                _log_tick_failure(error)

    def shutdown(self) -> None:
        """Stop the reaper and drop the instance row.

        The runs it owned SURVIVE (`owner_instance` carries no foreign key) —
        the supervisor leaves, not the history of what it ran. The existing
        `close(run_id)` method closes a single run; this is the supervisor's
        own shutdown, hence the distinct name.
        """
        with self._lock:
            self._stop_tick.set()
        if self._tick_thread.is_alive():
            self._tick_thread.join(timeout=2 * self.TICK_INTERVAL_S)
        # CODEX round-3 blocker 4: signal any live children to release the
        # registry's locks before we drop the instance row. The children
        # own their own run ids; we just nudge them. A SIGKILL is the
        # only signal that is unambiguous on a misbehaving vendor.
        for child_id, spawned in list(self._children.items()):
            try:
                if hasattr(spawned, "stop_then_kill"):
                    spawned.stop_then_kill(spawned.identity)
                else:
                    _best_effort_killpg(spawned.identity.pid)
            except (child.ChildIdentityError, OSError) as error:
                _log_shutdown_failure(f"child {child_id} custody", error)
        try:
            self._registry.remove_instance()
            if self._sidecar_kind is not None:
                self._registry.remove_sidecar_instance()
        except (*STORE_ERRORS, OSError) as error:
            _log_shutdown_failure("remove_instance", error)
        fd = getattr(self, "_run_root_fd", None)
        self._run_root_fd = None
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)

    def _command_start(self, cwd: str, requested: dict[str, Any]) -> RunAccepted:
        if self.owner is not None or self.read_grant:
            raise InvalidRunRequest("Command sessions require the owner connection.")
        cwd = command_cwd(cwd)
        selected = command_resolve(requested, _read_roster(cwd), cwd=cwd)
        replaces = selected.get("replacesRun")
        if replaces is not None:
            with self._lock:
                original = self._read(replaces)
                origin = next(
                    (
                        event.payload["command"]
                        for event in self._registry.read_run_events(replaces)
                        if isinstance(event.payload, dict)
                        and "command" in event.payload
                    ),
                    None,
                )
            if (
                original.backend is not Backend.COMMAND
                or original.phase is not RunPhase.CLOSED
                or original.worktree != cwd
                or not isinstance(origin, dict)
                or origin.get("centerId") != selected["centerId"]
            ):
                raise InvalidRunRequest(
                    "The replacement must retain its closed command's project and center."
                )
            selected["replaces"] = original.thread_id
        run_id = claude_child.mint_session_id()
        now = int(time.time() * 1000)
        call = DRIVE_CALL.get()
        row = RunRow(
            run_id=run_id,
            worktree=cwd,
            phase=RunPhase.PENDING,
            sandbox="workspace-write",
            model=Path(selected["executable"]).name,
            owner_instance=self._registry.instance_id,
            created_at=now,
            updated_at=now,
            backend=Backend.COMMAND,
            group_name=None if call is None else call.group,
        )
        root_fd = self._run_root
        if root_fd is None:
            raise RunDirectoryError("The command run root is not held.")
        directory = create_run_directory(self._run_root_path, run_id, dir_fd=root_fd)
        with self._lock:
            self._registry.admit_run(
                row,
                False,
                now,
                self.RUN_CLAIM_LEASE_MS,
                run_key=None if call is None else call.run_key,
            )
            self._registry.append_run_event(
                run_id, at=now, kind="lifecycle", payload={"command": selected}
            )
        ready = threading.Event()

        def exited(code: int) -> None:
            ready.wait()
            with self._lock:
                self._registry.append_run_event(
                    run_id,
                    at=int(time.time() * 1000),
                    kind="lifecycle",
                    payload={"session_exit": code},
                )
                self._update_phase(
                    run_id, RunPhase.COMPLETED if code == 0 else RunPhase.FAILED
                )

        managed: Command | None = None
        try:
            managed = Command(
                selected,
                cwd=cwd,
                run_id=run_id,
                run_dir=directory.root,
                scope=self._open_writer(run_id),
                exited=exited,
            )
            with self._lock:
                self._commands[run_id] = managed
                self._registry.append_run_event(
                    run_id,
                    at=now,
                    kind="lifecycle",
                    payload={"session_transport": managed.data()},
                )
                self._update_phase(
                    run_id,
                    RunPhase.RUNNING,
                    thread_id=managed.terminal.ident,
                    process_identity=capture_process_identity(managed.terminal.pid),
                )
        except (NSpawnError, OSError) as error:
            with self._lock:
                self._update_phase(run_id, RunPhase.FAILED, blocked_reason=str(error))
            writer = self._writers.get(run_id)
            if writer is not None:
                writer.close()
            if managed is not None:
                ready.set()
                managed.close()
            raise
        finally:
            ready.set()
        assert managed is not None
        worker = threading.Thread(
            target=self._command_wait,
            args=(run_id, managed),
            daemon=True,
            name="nspawn-command-session",
        )
        if call is not None:
            call.run_id, call.worker = run_id, worker
        worker.start()
        return RunAccepted(run_id, RunPhase.RUNNING)

    def _command_wait(self, run_id: str, managed: Command) -> None:
        # Retention never ends a command (`Command.retain`): only a close does.
        managed.stopped.wait()
        with self._lock:
            # A local close pops the command when it commits: no registry read then.
            while (
                self._commands.get(run_id) is managed
                and self._read(run_id).phase is not RunPhase.CLOSED
            ):
                self._lock.wait(0.1)
            self._commands.pop(run_id, None)

    def _activate(
        self,
        cwd: str,
        request: dict[str, Any],
        original_run: str | None,
    ) -> RunAccepted:
        """Prepare the selected native route without an inference request."""
        _require_text("cwd", cwd)
        try:
            selected = SessionActivation.from_request(request)
        except (ValueError, TypeError) as error:
            raise InvalidRunRequest("native activation is invalid") from error
        if self.read_grant and selected.profile is None:
            raise InvalidRunRequest(
                "an audit grant requires a verified read-only native profile"
            )
        if selected.provider == "agy" and not native.agy_cage_available():
            raise InvalidRunRequest(AGY_LAUNCH_REFUSAL)
        if original_run is not None:
            previous = self._read(original_run)
            with self._lock:
                recorded = self._registry.session_activation(original_run)
            if (
                previous.phase is not RunPhase.CLOSED
                or recorded != selected
                or previous.worktree != os.path.realpath(cwd)
            ):
                raise InvalidRunRequest(
                    "native activation cannot replace its recorded origin"
                )
        stack = contextlib.ExitStack()
        run_dir: RunDirectory | None = None
        run_id = claude_child.mint_session_id()
        managed: session.NativeSession | None = None
        try:
            descriptors: tuple[int, ...] = ()
            if selected.profile is not None:
                assert (
                    selected.profile_digest is not None
                    and selected.grant_root is not None
                )
                prepared = stack.enter_context(
                    route.profile_prepare(
                        selected.provider,
                        selected.account_route.removeprefix(selected.provider + ":"),
                        path=Path(selected.profile),
                        digest=selected.profile_digest,
                        grant_root=Path(selected.grant_root),
                        cwd=cwd,
                    )
                )
                profile = route.profile_read(
                    Path(selected.profile),
                    selected.profile_digest,
                    grant_root=Path(selected.grant_root),
                ).data()
                if self.read_grant and (
                    profile["scope"]["write"]
                    or profile["scope"]["permission_ceiling"] != "read-only"
                ):
                    raise InvalidRunRequest(
                        "an audit activation cannot widen its read-only grant"
                    )
                if (
                    profile["constructor"]["model"] != selected.model
                    or profile["constructor"]["effort"] != selected.effort
                ):
                    raise InvalidRunRequest(
                        "native activation differs from its PRELAUNCH constructor"
                    )
                if prepared.proxy is not None:
                    from .network import egress_start

                    stack.enter_context(
                        egress_start(Path(prepared.proxy), prepared.destinations)
                    )
                route.profile_verify(prepared)
                argv, environment = prepared.argv, prepared.env
                descriptors = prepared.descriptors
            else:
                argv_list, environment = route.route_launch(
                    selected.provider,
                    selected.account_route.removeprefix(selected.provider + ":"),
                    codex_adapter=os.environ.get("NOTT_NODE_CODEX_ADAPTER")
                    if selected.provider == "codex"
                    else None,
                )
                argv = tuple(argv_list)
            home = environment.get("HOME", str(Path.home()))
            context = NativeSessionContext(
                home=home,
                sqlite_home=environment.get("CODEX_HOME")
                if selected.provider in {"codex", "qwen"}
                else None,
            )
            now = int(time.time() * 1000)
            call = DRIVE_CALL.get()
            row = RunRow(
                run_id=run_id,
                worktree=os.path.realpath(cwd),
                phase=RunPhase.PENDING,
                sandbox="read-only"
                if selected.profile is not None
                else "workspace-write",
                model=selected.model,
                owner_instance=self._registry.instance_id,
                created_at=now,
                updated_at=now,
                backend=Backend.CLAUDE
                if selected.provider == "qwen"
                else Backend(selected.provider),
                requested_provider=selected.provider,
                executed_provider=selected.provider,
                executed_account=(
                    route.codex_account(
                        route.read_roster(),
                        selected.account_route.removeprefix("codex:"),
                    )[1]
                    if selected.provider == "codex"
                    and selected.account_route.removeprefix("codex:").startswith("/")
                    else None
                    if selected.account_route.removeprefix(
                        selected.provider + ":"
                    ).startswith("/")
                    else urllib.parse.unquote(
                        selected.account_route.removeprefix(
                            selected.provider + ":"
                        ).partition("@")[0]
                    )
                ),
                owner=self.owner,
                group_name=None if call is None else call.group,
            )
            root_fd = self._run_root
            if root_fd is None:
                raise RunDirectoryError("native session run root is not held")
            run_dir = create_run_directory(self._run_root_path, run_id, dir_fd=root_fd)
            with self._lock:
                self._registry.admit_run(
                    row,
                    False,
                    now,
                    self.RUN_CLAIM_LEASE_MS,
                    native_context=context,
                    activation=selected.data(),
                    effort_profile={
                        "requested": selected.effort,
                        "sent": selected.effort,
                    },
                    run_key=None if call is None else call.run_key,
                )
            needles = SecretNeedles.none()
            for name in (
                *child.CREDENTIAL_ENV_NAMES,
                "NOTT_NODE_BINDING",
                "NOTT_NODE_IPC_TOKEN",
                *(route_variables(scope)[2] for scope in (None, *sorted(SCOPES))),
            ):
                if environment.get(name):
                    needles = needles.merged(
                        SecretNeedles.from_credential(environment[name].encode())
                    )
            managed = session.NativeSession(
                argv=argv,
                environment=environment,
                cwd=Path(cwd),
                run_dir=run_dir.root,
                needles=needles,
                scope=self._open_writer(run_id),
                pass_fds=descriptors,
                observe=self._session_observer(run_id, selected, needles),
            )
            with self._lock:
                self._sessions[run_id] = managed
                self._children[run_id] = managed.spawned
                self._registry.append_run_event(
                    run_id,
                    at=now,
                    kind="lifecycle",
                    payload={"session_transport": managed.data()},
                )
                self._update_phase(
                    run_id, RunPhase.PENDING, process_identity=managed.spawned.identity
                )
                self._lock.notify_all()
            if call is not None:
                call.run_id = run_id
            worker = threading.Thread(
                target=self._guarded_worker,
                args=(run_id, self._session_wait, run_id, managed, stack),
                daemon=True,
            )
            if call is not None:
                call.worker = worker
            worker.start()
            return RunAccepted(run_id, RunPhase.PENDING)
        except BaseException:
            try:
                if managed is not None:
                    managed.close()
                elif run_dir is not None:
                    _cleanup_run_dir(run_dir)
            finally:
                stack.close()
            raise

    def _session_wait(
        self, run_id: str, managed: session.NativeSession, stack: contextlib.ExitStack
    ) -> None:
        try:
            managed.wait()
            code = managed.spawned.poll()
            if code is not None:
                managed.spawned.wait(timeout=10)
                with self._lock:
                    self._registry.append_run_event(
                        run_id,
                        at=int(time.time() * 1000),
                        kind="lifecycle",
                        payload={"session_exit": code},
                    )
            if not self._read(run_id).phase.is_terminal():
                raise SupervisorError("native session ended before its owner turn")
        finally:
            try:
                managed.close()
            finally:
                stack.close()
                with self._lock:
                    self._sessions.pop(run_id, None)
                    self._children.pop(run_id, None)

    def _session_observer(
        self, run_id: str, selected: SessionActivation, needles: SecretNeedles
    ) -> Callable[[str, dict[str, Any]], None]:
        pending: dict[str | int, str] = {}
        chunks: list[str] = []
        loadable = False
        usage_keys: dict[str | int, str] = {}

        def usage_record(
            key: str, result: object, *, subscription: bool = False
        ) -> None:
            usage = result.get("usage") if isinstance(result, dict) else None
            counters = usage if isinstance(usage, dict) else {}
            if selected.provider == "codex":
                input_tokens = output_tokens = None
            elif subscription:
                input_tokens = token_sum(
                    counters.get(name)
                    for name in (
                        "input_tokens",
                        "cache_read_input_tokens",
                        "cache_creation_input_tokens",
                    )
                )
                output_tokens = token_counter(counters.get("output_tokens"))
            else:
                names = ["inputTokens", "cachedReadTokens"]
                if selected.provider == "claude":
                    names.append("cachedWriteTokens")
                input_tokens = token_sum(counters.get(name) for name in names)
                output_tokens = token_counter(counters.get("outputTokens"))
            with self._lock:
                self._registry.append_run_event(
                    run_id,
                    at=int(time.time() * 1000),
                    kind="usage",
                    payload={
                        "source": "managed",
                        "observation_key": key,
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                    },
                )

        def attempt_key(params: dict[str, Any]) -> str:
            key = params.get("key", {})
            return "attempt:" + json.dumps(
                [key.get("activation_id"), key.get("attempt_id")], separators=(",", ":")
            )

        def bind_thread(thread_id: str) -> None:
            try:
                self._registry.update_run(
                    replace(
                        self._read(run_id),
                        updated_at=int(time.time() * 1000),
                        thread_id=thread_id,
                    )
                )
            except STORE_ERRORS as error:
                raise DuplicateSession(
                    f"session {thread_id!r} is already in use by another run"
                ) from error

        def observe(direction: str, frame: dict[str, Any]) -> None:
            nonlocal loadable
            method, ident = frame.get("method"), frame.get("id")
            if direction == "input":
                if isinstance(method, str) and isinstance(ident, (str, int)):
                    if method in {
                        "session/prompt",
                        "_nott/turn/start",
                        "_nott/turn/resume",
                    }:
                        current = self._read(run_id)
                        if current.phase not in {RunPhase.PENDING, RunPhase.IDLE}:
                            raise InvalidRunRequest(
                                "native session already has an active or ended owner turn"
                            )
                        chunks.clear()
                        self._update_phase(
                            run_id, RunPhase.RUNNING, turn_count=current.turn_count + 1
                        )
                        key = (
                            attempt_key(frame.get("params", {}))
                            if selected.profile is not None
                            else f"prompt:{current.turn_count + 1}"
                        )
                        usage_keys[ident] = key
                        with self._lock:
                            self._registry.append_run_event(
                                run_id,
                                at=int(time.time() * 1000),
                                kind="lifecycle",
                                payload={"usage_pending": key, "usage_managed": True},
                            )
                    pending[ident] = method
                return
            if method == "session/update":
                params = frame.get("params", {})
                update = params.get("update", {}) if isinstance(params, dict) else {}
                if update.get("sessionUpdate") == "agent_message_chunk":
                    content = update.get("content", {})
                    if isinstance(content, dict) and isinstance(
                        content.get("text"), str
                    ):
                        chunks.append(needles.redact(content["text"]))
            if method == "_nott/turn/result" and selected.profile is not None:
                params = frame.get("params")
                if isinstance(params, dict) and isinstance(params.get("key"), dict):
                    usage_record(
                        attempt_key(params), params.get("result"), subscription=True
                    )
            if method == "_nott/turn/record":
                params = frame.get("params")
                if not isinstance(params, dict):
                    raise InvalidRunRequest("native attempt receipt is not an object")
                receipt = AttemptReceiptV1(params).data()
                if selected.profile_digest != receipt["profile_digest"]:
                    raise InvalidRunRequest(
                        "native receipt differs from its activation"
                    )
                if receipt.get("state") == "terminal":
                    usage_record(attempt_key(receipt), None, subscription=True)
                with self._lock:
                    if receipt["native_session"] is not None:
                        bind_thread(receipt["native_session"])
                    self._registry.append_run_event(
                        run_id,
                        at=int(time.time() * 1000),
                        kind="lifecycle",
                        payload={"attempt_receipt": receipt},
                    )
            if (
                isinstance(ident, (str, int))
                and ident in pending
                and "method" not in frame
            ):
                answered = pending.pop(ident)
                result = frame.get("result")
                if answered == "initialize" and isinstance(result, dict):
                    capabilities = result.get("agentCapabilities")
                    loadable = (
                        isinstance(capabilities, dict)
                        and capabilities.get("loadSession") is True
                    )
                if (
                    answered in {"session/new", "session/load"}
                    and selected.profile is None
                    and isinstance(result, dict)
                    and isinstance(result.get("sessionId"), str)
                ):
                    with self._lock:
                        bind_thread(result["sessionId"])
                if answered in {
                    "session/prompt",
                    "_nott/turn/start",
                    "_nott/turn/resume",
                }:
                    if selected.profile is None:
                        usage_record(usage_keys.pop(ident), result)
                    if selected.provider == "agy" and isinstance(result, dict):
                        metadata = result.get("_meta", {})
                        nott = (
                            metadata.get("nott", {})
                            if isinstance(metadata, dict)
                            else {}
                        )
                        native_id = (
                            nott.get("nativeSessionId")
                            if isinstance(nott, dict)
                            else None
                        )
                        if isinstance(native_id, str) and native_id:
                            with self._lock:
                                bind_thread(native_id)
                    success = (
                        isinstance(result, dict)
                        and result.get("stopReason") != "error"
                        and result.get("outcome") not in {"limit", "unknown", "failed"}
                    )
                    answer = "".join(chunks)
                    current = self._read(run_id)
                    artifact = f"turn-{current.turn_count}.json"
                    kept_frame = frame
                    if isinstance(result, dict) and isinstance(
                        result.get("usage"), dict
                    ):
                        # Invalid counters must not interrupt native frame forwarding.
                        kept_frame = {
                            **frame,
                            "result": {
                                **result,
                                "usage": {
                                    name: None
                                    if isinstance(value, float)
                                    and not math.isfinite(value)
                                    else value
                                    for name, value in result["usage"].items()
                                },
                            },
                        }
                    data = needles.redact(
                        json.dumps(
                            {"response": kept_frame, "final_text": answer},
                            allow_nan=False,
                        )
                    ).encode()
                    if len(data) > KEPT_ANSWER_MAX_BYTES:
                        raise InvalidRunRequest(
                            "native turn result exceeds its owned artifact limit"
                        )
                    directory = self._open_run_directory(run_id)
                    try:
                        fd = _open_regular(
                            directory, artifact, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                        )
                        with os.fdopen(fd, "wb") as output:
                            output.write(data)
                            output.flush()
                            os.fsync(output.fileno())
                        os.fsync(directory)
                    finally:
                        os.close(directory)
                    with self._lock:
                        self._registry.append_run_event(
                            run_id,
                            at=int(time.time() * 1000),
                            kind="lifecycle",
                            payload={
                                "session_turn": {
                                    "number": current.turn_count,
                                    "request_id": ident,
                                    "phase": "completed" if success else "failed",
                                    "result": {
                                        "stopReason": result.get(
                                            "stopReason", "end_turn"
                                        )
                                        if success and isinstance(result, dict)
                                        else "error"
                                    },
                                    "artifact": artifact,
                                }
                            },
                        )
                    warning = (
                        self._keep_whole_answer(run_id, answer)
                        if loadable
                        and len(answer.encode()) > MAX_DURABLE_OBSERVATION_BYTES
                        else None
                    )
                    self._update_phase(
                        run_id,
                        RunPhase.IDLE,
                        final_text=_bounded_text(answer, MAX_DURABLE_OBSERVATION_BYTES),
                        warning=warning,
                        blocked_reason=None if success else "NativeSessionTurnRefused",
                    )

        return observe

    def execute(
        self,
        prompt: str | None,
        cwd: str,
        *,
        provider: str | None = None,
        model: str | None = None,
        preferred_model: str | None = None,
        effort: str | None = None,
        execution_class: str | None = None,
        session_id: str | None = None,
        timeout_ms: int = 0,
        read_only: bool = False,
        read_paths: Sequence[str] | None = None,
        system_prompt: str | None = None,
        native_write: bool = False,
        orchestrator: Mapping[str, Any] | None = None,
        inference_profile: dict[str, Any] | None = None,
        original_run: str | None = None,
        activation: dict[str, Any] | None = None,
        session_operation: dict[str, Any] | None = None,
        command: dict[str, Any] | None = None,
        producer: Sequence[str] = (),
    ) -> RunAccepted:
        fresh_turn = (
            command is None
            and activation is None
            and session_operation is None
            and session_id is None
            and original_run is None
            and orchestrator is None
            and inference_profile is None
            and not native_write
        )
        if producer and not fresh_turn:
            raise InvalidRunRequest(
                "producer applies to a fresh read-only turn: it takes no session, "
                "original run, orchestrator, inference profile, command or "
                "native_write"
            )
        # Only a witness class keeps the producer's route out (`_witness_admit`).
        if producer and execution_class not in _WITNESS_CLASSES:
            raise InvalidRunRequest(
                "producer applies to a review or verification run: name `class`"
            )
        if command is not None:
            if (
                any(
                    value is not None
                    for value in (
                        prompt,
                        provider,
                        model,
                        preferred_model,
                        effort,
                        execution_class,
                        session_id,
                        system_prompt,
                        orchestrator,
                        inference_profile,
                        original_run,
                        activation,
                        session_operation,
                    )
                )
                or timeout_ms
                or read_only
                or read_paths
                or native_write
            ):
                raise InvalidRunRequest(
                    "A command session takes no inference or additional grant."
                )
            return self._command_start(cwd, command)
        if activation is not None or session_operation is not None:
            if not self.host_control:
                raise InvalidRunRequest(HOST_CONTROL_REFUSAL)
            if (
                any(
                    value is not None
                    for value in (provider, model, preferred_model, effort)
                )
                or timeout_ms
            ):
                raise InvalidRunRequest(
                    "native activation takes no additional constructor"
                )
            if (
                prompt is not None
                or inference_profile is not None
                or orchestrator is not None
                or native_write
                or read_only
                or read_paths
                or system_prompt is not None
                or execution_class is not None
                or session_id is not None
            ):
                raise InvalidRunRequest(
                    "native activation takes no inference or additional grants"
                )
            if activation is not None:
                if session_operation is not None:
                    raise InvalidRunRequest(
                        "activation and session_operation are exclusive"
                    )
                return self._activate(cwd, activation, original_run)
            if original_run is None or session_operation is None:
                raise InvalidRunRequest(
                    "session_operation requires its actual original run"
                )
            row = self._read(original_run)
            if os.path.realpath(cwd) != row.worktree:
                raise InvalidRunRequest("native session operation cannot change cwd")
            with self._lock:
                port = self._registry.session_transport(original_run)
            if port is None or row.phase is RunPhase.CLOSED:
                raise InvalidRunRequest("native session has no live custodian")
            try:
                if session_operation.get("method") == "_nott/session/admit":
                    session_operation = session.operation(session_operation)
                    with self._lock:
                        selected = self._registry.session_activation(original_run)
                    if (
                        selected is None
                        or selected.profile is None
                        or selected.profile_digest is None
                        or selected.grant_root is None
                    ):
                        raise AdmissionRefusal(
                            "native admission has no verified activation"
                        )
                    profile = route.profile_read(
                        Path(selected.profile),
                        selected.profile_digest,
                        grant_root=Path(selected.grant_root),
                    ).data()
                    if (
                        profile["route_ref"]
                        != selected.account_route.removeprefix(selected.provider + ":")
                        or os.path.realpath(profile["cwd"]) != row.worktree
                        or profile["constructor"]["model"] != selected.model
                        or profile["constructor"]["effort"] != selected.effort
                    ):
                        raise AdmissionRefusal(
                            "native admission differs from its selected constructor"
                        )
                    params = session_operation["params"]
                    if "profile" in params and params["profile"] != profile:
                        raise AdmissionRefusal(
                            "native admission supplies a different profile"
                        )
                    admission = SessionAdmissionV1({**params, "profile": profile})
                    session_operation = {
                        **session_operation,
                        "params": admission.data(),
                    }
                session.submit(port, session_operation)
            except (AdmissionRefusal, OSError, ValueError) as error:
                raise InvalidRunRequest(
                    "native custodian could not admit this operation"
                ) from error
            return RunAccepted(original_run, self._read(original_run).phase)
        if prompt is None:
            raise InvalidRunRequest("prompt is required for an inference turn")
        _require_text("prompt", prompt)
        _require_text("cwd", cwd)
        if original_run is not None:
            _require_text("original_run", original_run)
            original = self._read(original_run)
            with self._lock:
                original_profile = self._registry.session_profile(original.run_id)
            if original_profile is None or original.thread_id is None:
                raise InvalidRunRequest(
                    "original run has no verified continuation origin"
                )
            if session_id not in (None, original.thread_id):
                raise InvalidRunRequest(
                    "original run and session_id name different origins"
                )
            session_id = original.thread_id
            if not native_write and not read_only:
                native_write, read_only = (
                    original_profile.native_write,
                    original_profile.read_only,
                )
            if system_prompt is None:
                system_prompt = original.system_prompt
        if inference_profile is not None:
            try:
                StructuredInference.from_request(inference_profile)
            except ValueError as error:
                raise InvalidRunRequest(str(error)) from error
            if (
                not read_only
                or native_write
                or orchestrator is not None
                or read_paths
                or system_prompt is not None
            ):
                raise InvalidRunRequest(
                    "inference_profile requires read_only and no additional grants"
                )
        if self.read_grant and (
            not read_only or native_write or orchestrator is not None
        ):
            raise InvalidRunRequest(
                "this nSPAWN serves an audit parent (NOTT_NSPAWN_GRANT=read): only "
                "a caged, read-only dispatch is admitted, with no native_write and "
                "no orchestrator"
            )
        if orchestrator is not None and (native_write or not read_only):
            raise InvalidRunRequest(
                "an orchestrator turn is read-only: it takes no native_write and "
                "no writable worktree"
            )
        if orchestrator is not None and read_paths:
            raise InvalidRunRequest(
                "an orchestrator turn reads its binding's snapshot and runtime: "
                "read_paths is refused beside an orchestrator binding"
            )
        resume_profile: SessionProfile | None = None
        if session_id is not None:
            with self._lock:
                previous = self._registry.inference_origin(session_id)
                resume_profile = (
                    None
                    if previous is None
                    else self._registry.session_profile(previous.run_id)
                )
                context = (
                    self._registry.resolve_native_session(session_id)
                    if resume_profile is not None
                    else None
                )
            if resume_profile is not None:
                assert previous is not None
                if (
                    context is None
                    or previous.phase is not RunPhase.CLOSED
                    or (
                        native_write != resume_profile.native_write
                        or read_only != resume_profile.read_only
                        or os.path.realpath(cwd) != previous.worktree
                        or provider
                        not in (
                            None,
                            previous.executed_provider,
                            previous.executed_account,
                        )
                        or model not in (None, previous.model)
                        or execution_class not in (None, resume_profile.execution_class)
                        or timeout_ms not in (0, resume_profile.timeout_ms)
                        or system_prompt != previous.system_prompt
                        or read_paths is not None
                        and tuple(read_paths) != resume_profile.read_paths
                        or context[1].home != str(Path.home())
                    )
                ):
                    raise InvalidRunRequest(
                        "native resume cannot change its verified origin or profile"
                    )
                sent = self._registry.effort_profile(previous.run_id).sent
                if effort not in (None, sent):
                    raise InvalidRunRequest(
                        "native resume cannot change its recorded effort"
                    )
                provider, model, effort = (
                    previous.executed_account,
                    previous.model,
                    sent,
                )
                read_paths = resume_profile.read_paths
                execution_class = resume_profile.execution_class
                timeout_ms = resume_profile.timeout_ms
                if self.owner is not None and timeout_ms <= 0:
                    raise InvalidRunRequest(
                        "owned native continuation has no recorded positive deadline"
                    )
        if native_write:
            cwd = _native_write_worktree(
                cwd,
                read_only=read_only,
                session_id=None if resume_profile is not None else session_id,
            )
        if system_prompt is not None:
            try:
                validate_system_prompt(system_prompt)
            except ValueError as error:
                raise InvalidRunRequest(str(error)) from error
        granted_read_paths = child.validate_read_paths(
            read_paths, read_only=read_only or native_write
        )
        # BEFORE THE ROSTER AND BEFORE THE SYSTEM1: a dispatch about to be
        # refused reads no file and pays no vendor call.
        # `test_a_writable_dispatch_without_native_write_is_refused_before_the_roster`.
        if not read_only and not native_write:
            raise InvalidRunRequest(_WRITE_NEEDS_NATIVE_WRITE)
        try:
            # Read ONCE here and handed to `_admit`: the resolver needs the
            # roster for the cost order, and a dispatch must not read the file
            # twice.
            roster = _read_roster(cwd)
            if native_write:
                _refuse_undeclared_writer_root(cwd, roster)
            class_warning: str | None = None
            class_observation: ClassObservation | None = None
            if execution_class is None and inference_profile is not None:
                # The door admits a class-less run only as a pinned, tool-free and
                # read-only inference: no class cap or System1 bounds it.
                picker.refuse_outside_fast_lane(roster, provider, model, effort)
            # Read BEFORE System1 may lower the class: confinement is what the
            # caller asked for, never what a classifier made of the prompt.
            review_requested = execution_class == "review" or (
                resume_profile is not None and resume_profile.read_only
            )
            if (
                execution_class is not None
                and not native_write
                and resume_profile is None
                and not (producer and execution_class in _WITNESS_CLASSES)
            ):
                execution_class, class_warning, class_observation = (
                    classify.resolve_execution_class(
                        execution_class,
                        prompt,
                        roster,
                        pinned=effort is not None or provider is not None,
                    )
                )
            if fresh_turn and (producer or execution_class in _WITNESS_CLASSES):
                return self._witness_admit(
                    provider,
                    model,
                    effort,
                    producer,
                    prompt=prompt,
                    cwd=cwd,
                    preferred_model=preferred_model,
                    execution_class=execution_class,
                    session_id=None,
                    timeout_ms=timeout_ms,
                    claim_worktree=not read_only,
                    read_only=read_only,
                    read_paths=granted_read_paths,
                    system_prompt=system_prompt,
                    roster=roster,
                    class_warning=class_warning,
                    class_observation=class_observation,
                    review_requested=review_requested,
                )
            return self._admit(
                prompt,
                cwd,
                provider=provider,
                model=model,
                preferred_model=preferred_model,
                effort=effort,
                execution_class=execution_class,
                session_id=session_id,
                timeout_ms=timeout_ms,
                claim_worktree=not read_only,
                read_only=read_only,
                read_paths=granted_read_paths,
                system_prompt=system_prompt,
                roster=roster,
                class_warning=class_warning,
                class_observation=class_observation,
                native_write=native_write,
                review_requested=review_requested,
                orchestrator=orchestrator,
                inference_profile=inference_profile,
            )
        except (picker.PickerError, picker.AllProvidersWalled) as refusal:
            if class_warning is None:
                raise
            # The refusal is born before the row, so the class warning has
            # nowhere else to live: the caller reads declared, observed and cap
            # in one answer instead of two round trips. `AllProvidersWalled` is
            # a sister of `PickerError`, not a subclass: catching one alone lost
            # the warning on the walled route.
            message = f"{refusal.message}; {class_warning}"
            if isinstance(refusal, picker.AllProvidersWalled):
                raise picker.AllProvidersWalled(
                    message, refusal.nearest_reset
                ) from refusal
            raise picker.PickerError(message, entry=refusal.entry) from refusal

    def review(
        self,
        branch: str,
        cwd: str,
        *,
        base: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        prompt: str | None = None,
        read_paths: Sequence[str] | None = None,
        system_prompt: str | None = None,
        producer: Sequence[str] = (),
    ) -> RunAccepted:
        _require_text("branch", branch)
        _require_text("cwd", cwd)
        _validate_review_worktree_branch(cwd, branch)
        if system_prompt is not None:
            try:
                validate_system_prompt(system_prompt)
            except ValueError as error:
                raise InvalidRunRequest(str(error)) from error
        granted_read_paths = child.validate_read_paths(read_paths, read_only=True)
        review_prompt = (
            prompt if prompt is not None else _default_review_prompt(branch, base)
        )
        return self._witness_admit(
            provider,
            model,
            effort,
            producer,
            prompt=review_prompt,
            cwd=cwd,
            execution_class="review",
            session_id=None,
            timeout_ms=0,
            claim_worktree=False,
            # NOT a parameter of `review`: the verb has called itself read-only
            # since it existed, and for that whole time nothing enforced it.
            read_only=True,
            read_paths=granted_read_paths,
            system_prompt=system_prompt,
            review_requested=True,
        )

    def _witness_admit(
        self,
        provider: str | None,
        model: str | None,
        effort: str | None,
        producer_names: Sequence[str],
        **admit: Any,
    ) -> RunAccepted:
        """Admit work that witnesses another agent's work, never as that agent.

        Pinned, the route is the caller's: it is refused when it is the
        producer, it runs off the producer's accounts, and it walks only the
        other accounts of that route, which `_admit` builds (none for a pinned
        account). Unpinned, a review or a verification takes the owner's preference order: each preference that cannot serve now is skipped and the
        reason is recorded on the run that serves.
        """
        # The request's own shape first: a preference must never be skipped for
        # what no route could cure.
        _require_text("prompt", admit["prompt"])
        _require_text("cwd", admit["cwd"])
        roster = admit.pop("roster", None)
        if roster is None:
            roster = _read_roster(admit["cwd"])
        producer = picker.producer_of(roster, producer_names)
        kept_off = picker.without_accounts(roster, producer.accounts)
        execution_class = admit["execution_class"]
        order = picker.preferences(roster, execution_class)
        # A roster that declares no route has no preference to try: the picker
        # answers it as it always did.
        if provider is not None or execution_class not in _WITNESS_CLASSES or not order:
            if provider is not None and (
                reason := picker.producer_refusal(roster, producer, provider, model)
            ):
                raise InvalidRunRequest(f"the pinned route {provider} is {reason}")
            return self._admit(
                provider=provider,
                model=model,
                effort=effort,
                roster=kept_off,
                producer=producer,
                **admit,
            )
        timeout_ms = admit["timeout_ms"]
        return self._admit_preferred(
            _Failover(
                execution_class=execution_class,
                remaining=order,
                skipped=[],
                producer=producer,
                refused=_NO_PRODUCER,
                model=model,
                effort=effort,
                admit={**admit, "roster": kept_off},
                deadline_at=None
                if timeout_ms <= 0
                else time.monotonic() + timeout_ms / 1000,
            )
        )

    def _admit_preferred(self, failover: _Failover) -> RunAccepted:
        """Admit on the first remaining preference that can serve now.

        A preference that is the producer, that refused this work earlier, or
        that admission refuses is skipped, and the reason is kept: on the run
        that serves, or in the refusal when no preference can serve. That
        refusal is a wall when a wall stopped a preference, so a caller that
        waits for `nearest_reset` is told when one may serve again.

        Raises:
            picker.AllProvidersWalled: no preference can serve, and a wall that
                lifts stopped at least one of them.
            picker.PickerError: no preference can serve, and no wall stopped any.
        """
        roster: Roster = failover.admit["roster"]
        while failover.remaining:
            preference = failover.remaining.pop(0)
            model = failover.model or preference.model
            step = isinstance(preference, _AccountStep)
            # A step's route is a provider name: only its provider-level
            # refusals apply here, never an account alias that equals it. The
            # credential check below decides the account.
            reason = picker.producer_refusal(
                roster,
                replace(failover.producer, accounts=frozenset())
                if step
                else failover.producer,
                preference.route,
                model,
            )
            # The route-level refusal never matches by account: a refused
            # account is matched by its credential below, so an alias equal to
            # a provider name never refuses that provider.
            if reason is None and picker.producer_refusal(
                roster, failover.refused, preference.route, None
            ):
                reason = "it refused this work earlier"
            if (
                reason is None
                and isinstance(preference, _AccountStep)
                and preference.account in failover.producer.accounts
            ):
                reason = "the producer's account"
            if reason is None and (
                any(
                    _credential(owner, alias) in failover.refused_credentials
                    for owner, alias in _named_accounts(roster, preference)
                )
                or any(
                    provider.name == preference.route
                    and not provider.discovered_accounts()
                    and _credential(provider, None) in failover.refused_credentials
                    for provider in roster.providers
                )
            ):
                reason = "it refused this work earlier"
            if reason is None:
                # The class warning a classifier wrote comes first, then the skips.
                warning = failover.admit.get("class_warning")
                if (note := _skipped_note(failover)) is not None:
                    warning = (
                        note if warning is None else _composed_warning(warning, note)
                    )
                admit = {
                    **failover.admit,
                    "roster": _without_credentials(
                        roster,
                        failover.refused_credentials,
                        preference if isinstance(preference, _AccountStep) else None,
                    ),
                    "class_warning": warning,
                }
                if failover.deadline_at is not None:
                    left_ms = int((failover.deadline_at - time.monotonic()) * 1000)
                    if left_ms <= 0:
                        failover.skipped.append(f"{preference} (the deadline passed)")
                        break
                    admit["timeout_ms"] = left_ms
                try:
                    return self._admit(
                        provider=preference.route,
                        model=model,
                        effort=failover.effort or preference.effort,
                        producer=failover.producer,
                        failover=replace(
                            failover,
                            current=preference,
                            remaining=list(failover.remaining),
                            skipped=list(failover.skipped),
                        ),
                        **admit,
                    )
                except _ROUTE_REFUSALS as refusal:
                    if (
                        isinstance(refusal, picker.AllProvidersWalled)
                        and refusal.nearest_reset is not None
                    ):
                        failover.resets.append(refusal.nearest_reset)
                    reason = (
                        PRODUCER_MODEL
                        if str(refusal) == PRODUCER_MODEL
                        else f"{type(refusal).__name__}: {refusal}"
                    )
            failover.skipped.append(f"{preference} ({reason})")
        message = (
            f"no {failover.execution_class} preference can serve now: "
            + "; ".join(failover.skipped)
        )
        if failover.resets:
            raise picker.AllProvidersWalled(message, min(failover.resets))
        raise picker.PickerError(message)

    @staticmethod
    def _credentials_of(failover: _Failover, row: RunRow) -> frozenset[str]:
        """The credential the failed run used, as `_credential` keys it."""
        if row.executed_provider is None:
            return frozenset()
        roster: Roster = failover.admit["roster"]
        provider = next(
            (p for p in roster.providers if p.name == row.executed_provider), None
        )
        if row.executed_account is None:
            if provider is None:
                return frozenset({f"route:{row.executed_provider}"})
            return frozenset({_credential(provider, None)})
        if provider is None:
            return frozenset({f"route:{row.executed_provider}/{row.executed_account}"})
        return frozenset({_credential(provider, row.executed_account)})

    def _retry_reason(
        self, run_id: str, phase: RunPhase, reason: str | None
    ) -> str | None:
        """Why a swarm member's failure is retried, or None for any other run."""
        with self._lock:
            taken = self._failovers.get(run_id)
        if taken is None or not taken[0].any_failure or phase is not RunPhase.FAILED:
            return None
        return reason or "it failed"

    def _fail_over(
        self, run_id: str, reason: str | None, *, spends: bool = False
    ) -> str | None:
        """Take the run's failover once; hand its work to the next preference
        when its route refused it, or, `spends`, for a swarm member's one retry,
        and say what happened, or None.

        Both terminal seams call it before they write, so the failed run's one
        terminal write names its successor. The successor is admitted for the
        dispatching call, so its notice, the close watcher and the driver's
        join follow the work. It takes no idempotency key: the key names the
        first run.

        Raises:
            The store's error when the run's row cannot be read, or any error
            while it builds the next step. The failover is kept for the
            terminal write's retry (`publish_pending`), which both terminal
            seams queue with the real terminal data.
        """
        with self._lock:
            taken = self._failovers.pop(run_id, None)
            try:
                row = (
                    None
                    if taken is None or reason is None
                    else self._registry.get_run(run_id)
                )
            except STORE_ERRORS:
                if taken is not None:
                    self._failovers[run_id] = taken
                raise
        # A run a close already ended stays ended: the caller stopped the work.
        if taken is None or row is None or row.phase in _ENDED:
            return None
        failover, call = taken
        # A route refusal walks the whole order, with or without a swarm. Only
        # a failure that is not a refusal (`spends`) takes the swarm's one retry.
        if spends and failover.retried_by is not None:
            return _bounded_text(
                f"the swarm retry is spent: the failure of run "
                f"{failover.retried_by} took it, and run {run_id} failed too "
                f"({reason} on {failover.current})",
                MAX_DURABLE_OBSERVATION_BYTES // 2,
            )
        try:
            # An account is refused by its credential alone; a run with no
            # account refuses its route as well.
            refused = (
                failover.refused
                if row.executed_account is not None
                else replace(
                    failover.refused,
                    providers=failover.refused.providers
                    | {row.executed_provider or ""},
                )
            )
            nxt = replace(
                failover,
                skipped=[
                    *failover.skipped,
                    f"{failover.current} ({reason} on run {run_id})",
                ],
                refused=refused,
                refused_credentials=failover.refused_credentials
                | self._credentials_of(failover, row),
                resets=[],
                current=None,
                retried_by=failover.retried_by or (run_id if spends else None),
            )
        except Exception:
            # Never lose the walk: the terminal write's retry takes it again.
            with self._lock:
                self._failovers[run_id] = taken
            raise
        if row.sandbox == "workspace-write" and (
            stay := self._writer_stays(row, reason)
        ):
            return stay
        if call is not None:
            call.run_key = None
        token = DRIVE_CALL.set(call)
        try:
            accepted = self._admit_preferred(nxt)
        except picker.PickerError, picker.AllProvidersWalled:
            return _bounded_text(
                f"no further {failover.execution_class} preference can serve now: "
                + "; ".join(nxt.skipped),
                MAX_DURABLE_OBSERVATION_BYTES // 2,
            )
        except (NSpawnError, OSError, *STORE_ERRORS) as error:
            # The type only, as `_fail_run` records: the text may carry a path.
            return _bounded_text(
                f"the {failover.execution_class} could not continue on its next "
                f"preference: {type(error).__name__}; skipped "
                + "; ".join(nxt.skipped),
                MAX_DURABLE_OBSERVATION_BYTES // 2,
            )
        finally:
            DRIVE_CALL.reset(token)
        try:
            with self._lock:
                successor = self._registry.get_run(accepted.run_id)
        except STORE_ERRORS:
            # The successor runs: only its route is unread.
            successor = None
        route = (
            "an unread route"
            if successor is None
            else "/".join(
                part
                for part in (successor.executed_provider, successor.executed_account)
                if part
            )
        )
        return f"continued as run {accepted.run_id} on {route}"

    def _writer_stays(self, row: RunRow, reason: str | None) -> str | None:
        """Why a failed writer keeps its work, or None once its claim is given up
        for the next route.

        Two writers never touch one tree: the writer moves
        only when no file or directory of its tree changed since its baseline
        (`_write_baselines`, else its admission; `worktree.unwritten_since`),
        only while the run is not closed, and only from its own claim that no
        fence poisoned and that the store gives up. Its slice was emptied
        before the terminal seam called this.
        """
        with self._lock:
            since_ns = self._write_baselines.get(row.run_id, row.created_at * 1_000_000)
        if not worktree.unwritten_since(Path(row.worktree), since_ns=since_ns):
            return _bounded_text(
                f"the writer wrote to its tree before it failed ({reason}), so "
                "nSPAWN did not move its work to another route",
                MAX_DURABLE_OBSERVATION_BYTES // 2,
            )
        try:
            with self._lock:
                # The scan takes time: a close that ended the run meanwhile wins.
                current = self._registry.get_run(row.run_id)
                if current is None or current.phase in _ENDED:
                    return "the run was closed, so nSPAWN did not move its work"
                claim = self._registry.read_claim(row.worktree)
                if (
                    claim is not None
                    and claim.run_id == row.run_id
                    and claim.lease_until != POISONED_LEASE_UNTIL
                    and self._registry.release_claim(row.worktree, row.run_id)
                ):
                    return None
        except (*STORE_ERRORS, RegistryError) as error:
            return _bounded_text(
                "the writer's claim could not be given to the next route: "
                f"{type(error).__name__}",
                MAX_DURABLE_OBSERVATION_BYTES // 2,
            )
        return "the writer's claim is not its own to give, so nSPAWN did not move it"

    def _admit(
        self,
        prompt: str,
        cwd: str,
        *,
        provider: str | None,
        model: str | None,
        effort: str | None,
        execution_class: str | None,
        session_id: str | None,
        timeout_ms: int,
        claim_worktree: bool,
        read_only: bool,
        read_paths: tuple[str, ...] = (),
        system_prompt: str | None = None,
        roster: Roster | None = None,
        class_warning: str | None = None,
        class_observation: ClassObservation | None = None,
        native_write: bool = False,
        review_requested: bool = False,
        preferred_model: str | None = None,
        orchestrator: Mapping[str, Any] | None = None,
        inference_profile: dict[str, Any] | None = None,
        producer: picker.Producer = _NO_PRODUCER,
        failover: _Failover | None = None,
    ) -> RunAccepted:
        _require_text("prompt", prompt)
        _require_text("cwd", cwd)
        if not read_only and not native_write:
            raise InvalidRunRequest(_WRITE_NEEDS_NATIVE_WRITE)
        swarm_call = DRIVE_CALL.get()
        swarm = swarm_call is not None and _is_swarm(swarm_call.group)
        if swarm and timeout_ms <= 0:
            timeout_ms = SWARM_TIMEOUT_MS
        binding = (
            None
            if orchestrator is None
            else native.OrchestratorBinding.from_request(
                orchestrator, snapshot=os.path.realpath(cwd)
            )
        )
        # The declared config decides the roster; cwd
        # never selects a project file; the declared config is authoritative.
        # `review` and the internal re-picks keep the old path and read it here.
        if roster is None:
            roster = _read_roster(cwd)
        if self.owner is not None and session_id is not None:
            # Every run that recorded the session, and at least one, is this node's.
            with self._lock:
                owners = self._registry.thread_owners(session_id)
            if owners != {self.owner}:
                raise InvalidRunRequest(
                    f"session_id {session_id!r} names no run of this node to resume"
                )
        epoch = time.time()
        now_s = int(epoch)
        now_ms = int(epoch * 1000)
        with self._lock:
            provider_walls = self._registry.get_provider_walls(now_s)
            account_walls = self._registry.get_account_walls(now_s)
            # Read beside the walls, under the same lock and the same instant:
            # a load map from a different moment would spread work by a
            # picture of the fleet that never existed.
            account_load = self._registry.get_account_load()
            account_use = picker.AccountUse(
                self._registry.get_account_usage(),
                self._registry.get_account_last_run(),
            )
        origin: tuple[RunRow, NativeSessionContext] | None = None
        inference_home: InferenceHome | None = None
        # A route the owner's preference chose is not one the caller requested.
        requested_provider = None if failover is not None else provider
        if session_id is not None:
            selected = roster.select(provider) if provider is not None else None
            with self._lock:
                origin = self._registry.resolve_native_session(
                    session_id,
                    provider=selected.name if selected is not None else provider,
                    account=provider
                    if selected is not None and provider != selected.name
                    else None,
                )
                previous = self._registry.inference_origin(session_id)
                if origin is not None and origin[0].backend is Backend.CODEX:
                    previous = origin[0]
                retained = (
                    None
                    if previous is None
                    else self._registry.inference_profile(previous.run_id)
                )
            if retained is not None:
                assert previous is not None
                if inference_profile is None:
                    raise InvalidRunRequest(
                        "structured inference resume requires explicit inference_profile"
                    )
                with self._lock:
                    inference_home = self._registry.inference_home(previous.run_id)
                if inference_home is None and previous.backend is Backend.CLAUDE:
                    raise InvalidRunRequest(
                        "structured inference resume requires its recorded native home"
                    )
                if (
                    inference_profile is not None
                    and inference_profile != retained
                    or not read_only
                    or native_write
                    or binding is not None
                    or read_paths
                    or system_prompt is not None
                    or os.path.realpath(cwd) != previous.worktree
                    or provider
                    not in (None, previous.executed_provider, previous.executed_account)
                    or model not in (None, previous.model)
                ):
                    raise InvalidRunRequest(
                        "structured inference resume cannot change its origin or profile"
                    )
                inference_profile = retained
                provider = previous.executed_account or previous.executed_provider
                model = previous.model
                with self._lock:
                    sent = self._registry.effort_profile(previous.run_id).sent
                if effort not in (None, sent):
                    raise InvalidRunRequest(
                        "structured inference resume cannot change its effort"
                    )
                effort = sent
            elif inference_profile is not None:
                raise InvalidRunRequest(
                    "structured inference resume requires its recorded origin"
                )
            elif (
                previous is not None
                and previous.backend is Backend.CLAUDE
                and self._registry.session_profile(previous.run_id) is None
            ):
                raise DuplicateSession(
                    "unverified legacy Claude origin cannot be resumed"
                )
        if session_id is not None and origin is not None:
            provider = origin[0].executed_account
        picked = picker.pick_execution(
            roster,
            now_s,
            requested_provider=provider,
            requested_model=model,
            requested_effort=effort,
            execution_class=execution_class,
            preferred_model=preferred_model,
            account_load=account_load,
            account_use=account_use,
            producer=None,
            provider_walls=provider_walls,
            account_walls=account_walls,
            read_paths=read_paths,
            system_prompt=system_prompt,
        )
        # The model is known only now: a route's default can be the producer's.
        if picker.is_producer_model(picked.executed_provider, picked.model, producer):
            raise InvalidRunRequest(PRODUCER_MODEL)
        if inference_profile is not None and picked.executed_provider.kind not in {
            "claude",
            "codex",
        }:
            raise InvalidRunRequest(
                "native structured inference requires a Claude or Codex transport"
            )
        # By the backend the picker resolved (or the resumed session recorded),
        # never by a provider's name, and before any row, directory or child.
        if not native.agy_cage_available() and (
            picked.executed_provider.kind == "agy"
            or (origin is not None and origin[0].backend is Backend.AGY)
        ):
            raise InvalidRunRequest(AGY_LAUNCH_REFUSAL)
        # U7 — typed refusal at the supervisor boundary. Picker has already
        # validated the candidate against declared `models` and the live
        # catalog on both branches; this is a belt-and-suspenders check that
        # ALSO names `spawn.toml` in the error message — the operator who
        # wrote the roster knows where to fix it.
        roster.refuse_unservable_spawn(picked.executed_provider.name, picked.model)
        if binding is not None and picked.executed_provider.kind != "codex":
            raise InvalidRunRequest(
                "an orchestrator binding requires a codex route; the picker "
                f"resolved a {picked.executed_provider.kind} one"
            )
        if native_write and "write" not in CAPABILITIES[picked.executed_provider.kind]:
            raise InvalidRunRequest(
                f"native_write is not implemented on a {picked.executed_provider.kind} "
                "route"
            )
        if native_write and picked.executed_provider.kind == "claude":
            _refuse_proxied_claude_writer(roster, picked)
        if (
            review_requested
            and read_only
            and inference_profile is None
            and session_id is not None
            and picked.executed_provider.kind == "claude"
            and not _picked_route_elsewhere(roster, picked)
            and (
                origin is None
                or self._registry.session_profile(origin[0].run_id) is None
            )
        ):
            raise InvalidRunRequest(
                "a caged Claude review cannot resume a session_id: the cage starts "
                "a fresh session"
            )
        # The cage composes its own env and argv; every other Claude child
        # receives the roster's, which would replace a caller-pinned value. Only a
        # Claude pick resolves the roster here: a native route never reads it.
        if picked.executed_provider.kind == "claude" and not (
            native_write
            or (
                read_only
                and review_requested
                and inference_profile is None
                and not _picked_route_elsewhere(roster, picked)
            )
        ):
            _refuse_roster_override(roster, picked)
        if origin is not None:
            # Read for EVERY native resume, not only a bound request: whether the
            # origin was admitted with a binding is the store's answer, and a
            # request that omits one cannot be allowed to decide it.
            with self._lock:
                recorded = self._registry.orchestrator_binding(origin[0].run_id)
            if recorded is None and binding is not None:
                raise InvalidRunRequest(
                    "an unbound origin cannot be resumed with an orchestrator "
                    f"binding: run {origin[0].run_id!r} was admitted with none"
                )
            if recorded is not None and binding is None:
                raise InvalidRunRequest(
                    "a bound origin cannot be resumed without its orchestrator "
                    f"binding: run {origin[0].run_id!r} was admitted with one, and "
                    "a resume states the identical binding or is refused"
                )
            if (
                recorded is not None
                and binding is not None
                and (field := binding.resume_refusal(recorded)) is not None
            ):
                raise InvalidRunRequest(
                    f"an orchestrator resume cannot change the {field} its origin "
                    "recorded"
                )
        launch: native.NativeLaunch | None = None
        previous_usage: dict[str, int] = {}
        if origin is not None and (
            origin[0].backend.value != picked.executed_provider.kind
            or origin[0].executed_provider != picked.executed_provider.name
        ):
            raise InvalidRunRequest(
                "native resume cannot change its recorded backend or provider"
            )
        call = DRIVE_CALL.get()
        run_id = claude_child.mint_session_id()
        if picked.executed_provider.kind in ("codex", "agy"):
            if session_id is not None and origin is None:
                raise InvalidRunRequest(
                    "native resume requires a recorded session origin"
                )
            launch = native.prepare_native(
                replace(picked.executed_provider, model=picked.model),
                picked.executed_account,
                roster=roster,
                cwd=Path(cwd),
                read_only=read_only or native_write,
                read_paths=read_paths,
                resume_id=session_id,
                system_prompt=system_prompt,
                effort=picked.effort,
                run_id=run_id,
                orchestrator=binding,
                disposable_root=None
                if binding is None and inference_profile is None
                else native.orchestrator_disposable_root(self._run_root_path / run_id),
                inference_profile=None
                if inference_profile is None
                else StructuredInference.from_request(inference_profile),
            )
            if origin is not None:
                previous_roots = origin[1].read_roots
                current_roots = launch.context.read_roots
                if (
                    replace(launch.context, read_roots=previous_roots) != origin[1]
                    or origin[0].worktree != cwd
                    or origin[0].backend.value != picked.executed_provider.kind
                    or (origin[0].sandbox == "read-only" and not read_only)
                    or previous_roots is not None
                    and (
                        current_roots is None
                        or any(
                            not any(
                                Path(root).is_relative_to(old) for old in previous_roots
                            )
                            for root in current_roots
                        )
                    )
                ):
                    raise InvalidRunRequest(
                        "native resume cannot change origin or widen its grants"
                    )
            if origin is not None and origin[0].backend is Backend.AGY:
                try:
                    snapshot = json.loads(origin[0].last_item or "null")
                    if not isinstance(snapshot, dict):
                        raise TypeError
                    previous = snapshot["native_usage"]
                    if not isinstance(previous, dict) or any(
                        not isinstance(key, str) or type(value) is not int or value < 0
                        for key, value in previous.items()
                    ):
                        raise ValueError
                    if "total_tokens" not in previous:
                        raise ValueError
                    previous_usage = previous
                except ValueError, TypeError, KeyError:
                    raise InvalidRunRequest(
                        f"{origin[0].backend.value.upper()} resume lacks a valid cumulative usage snapshot"
                    ) from None
        if launch is not None:
            launch.env[child.RUN_ID_ENV] = run_id
        thread_id = (
            session_id
            if session_id is not None
            else (None if launch is not None else claude_child.mint_session_id())
        )
        resume = session_id is not None
        caged_review = (
            read_only
            and review_requested
            and inference_profile is None
            and not _picked_route_elsewhere(roster, picked)
        )
        session_profile: SessionProfile | None = None
        session_context: NativeSessionContext | None = None
        if native_write or caged_review:
            with self._lock:
                retained_profile = (
                    None
                    if origin is None
                    else self._registry.session_profile(origin[0].run_id)
                )
            session_profile = SessionProfile(
                original_run_id=run_id
                if retained_profile is None
                else retained_profile.original_run_id,
                native_write=native_write,
                read_only=read_only,
                read_paths=read_paths,
                execution_class=execution_class,
                timeout_ms=timeout_ms,
                config_dir=(
                    str(self._run_root_path / run_id / claude_restricted.CONFIG_DIR)
                    if picked.executed_provider.kind == "claude"
                    and retained_profile is None
                    else None
                    if retained_profile is None
                    else retained_profile.config_dir
                ),
            )
            if launch is None:
                session_context = NativeSessionContext(
                    str(Path.home()),
                    read_roots=tuple(dict.fromkeys((cwd, *read_paths))),
                )
        root_fd = self._run_root
        if root_fd is None:
            raise RunDirectoryError("run-root descriptor is not held")
        run_dir = create_run_directory(self._run_root_path, run_id, dir_fd=root_fd)
        try:
            row = RunRow(
                run_id=run_id,
                thread_id=thread_id,
                worktree=cwd,
                lane=_lane_of(cwd),
                phase=RunPhase.PENDING,
                sandbox=("read-only" if read_only else "workspace-write")
                if launch is not None or native_write
                else CLAUDE_CONFINEMENT_POLICY_ONLY,
                model=picked.model,
                owner_instance=self._registry.instance_id,
                created_at=now_ms,
                updated_at=now_ms,
                backend=Backend(picked.executed_provider.kind)
                if launch is not None
                else Backend.CLAUDE,
                requested_provider=requested_provider,
                executed_provider=picked.executed_provider.name,
                executed_account=picked.executed_account,
                warning=picked.warning
                if class_warning is None
                else _composed_warning(class_warning, picked.warning or ""),
                system_prompt=system_prompt,
                group_name=None if call is None else call.group,
                owner=self.owner,
            )
            notice = None if call is None else call.notice()
            # `picked.effort` is the one value every launcher below is handed.
            effort_profile = {"requested": effort, "sent": picked.effort}
            with self._lock:
                if launch is None:
                    self._registry.admit_run(
                        row,
                        claim_worktree,
                        now_ms,
                        self.RUN_CLAIM_LEASE_MS,
                        notice=notice,
                        run_key=None if call is None else call.run_key,
                        class_observation=class_observation,
                        effort_profile=effort_profile,
                        inference_profile=inference_profile,
                        inference_home=inference_home,
                        native_context=session_context,
                        session_profile=session_profile,
                    )
                else:
                    self._registry.admit_run(
                        row,
                        claim_worktree,
                        now_ms,
                        self.RUN_CLAIM_LEASE_MS,
                        native_context=launch.context,
                        session_profile=session_profile,
                        notice=notice,
                        run_key=None if call is None else call.run_key,
                        class_observation=class_observation,
                        effort_profile=effort_profile,
                        inference_profile=inference_profile,
                        # The binding names the profile THIS run starts with, and
                        # lands in the same transaction as its row.
                        orchestrator_binding=None
                        if binding is None
                        else binding.payload(native.orchestrator_profile_id(run_id)),
                    )
        except AdmissionHeldRow as error:
            _cleanup_run_dir(run_dir)
            refused = AdmissionHeld(str(error))
            refused.data = dict(error.data)
            raise refused from error
        except AdmissionUnboundRow as error:
            _cleanup_run_dir(run_dir)
            raise AdmissionUnbound(str(error)) from error
        except KeyFencedRow as error:
            _cleanup_run_dir(run_dir)
            raise KeyFenced(str(error)) from error
        except UniqueTaken as error:
            _cleanup_run_dir(run_dir)
            if error.constraint == RUN_KEY_CONSTRAINT:
                raise KeyTaken("the idempotency key already names a run") from error
            raise DuplicateSession(
                f"session {thread_id!r} is already in use by another run"
            ) from error
        except BaseException:
            _cleanup_run_dir(run_dir)
            raise
        if call is not None:
            call.run_id = run_id
        if (
            session_id is None
            and binding is None
            and inference_profile is None
            and (failover is None or swarm)
        ):
            chosen = picked.executed_provider
            accounts: list[picker.Preference] = [
                _AccountStep(chosen.name, account=alias)
                for alias in chosen.ordered_accounts()
                if alias != picked.executed_account
                and alias not in chosen.excluded_accounts
            ]
            if failover is None:
                route = picker.Preference(provider or chosen.name)
                walk = (
                    [
                        *(
                            preference
                            for preference in picker.preferences(
                                roster, execution_class
                            )
                            if preference.route != chosen.name
                        ),
                        *accounts,
                    ]
                    if provider is None
                    else accounts
                    if provider == chosen.name
                    else []
                )
                failover = _Failover(
                    execution_class=execution_class or "run",
                    remaining=walk,
                    skipped=[],
                    producer=producer,
                    refused=_NO_PRODUCER,
                    model=model,
                    effort=effort,
                    admit={
                        "prompt": prompt,
                        "cwd": cwd,
                        "execution_class": execution_class,
                        "session_id": None,
                        "timeout_ms": timeout_ms,
                        "claim_worktree": claim_worktree,
                        "read_only": read_only,
                        "native_write": native_write,
                        "read_paths": read_paths,
                        "system_prompt": system_prompt,
                        "roster": roster,
                        "class_warning": class_warning,
                        "review_requested": review_requested,
                        "preferred_model": preferred_model,
                        "orchestrator": orchestrator,
                        "inference_profile": inference_profile,
                    },
                    deadline_at=None,
                    current=route,
                )
            pinned_account = provider is not None and provider != chosen.name
            failover = replace(
                failover,
                any_failure=failover.any_failure or swarm,
                # The walk ends with the other accounts of the route this run
                # took, each once; a pinned account has none.
                remaining=[
                    *failover.remaining,
                    *(
                        account
                        for account in ([] if pinned_account else accounts)
                        if account not in failover.remaining
                    ),
                ],
                # A run past its deadline is not retried: its notice waits for
                # nothing more. A run with no deadline keeps none.
                deadline_at=failover.deadline_at
                or (None if timeout_ms <= 0 else time.monotonic() + timeout_ms / 1000),
            )
            if not failover.remaining and not failover.any_failure:
                # Nowhere to go (a pinned account): the run ends as before, and
                # a store fault at its end is never queued for a retry.
                failover = None
        if failover is not None:
            # Before the worker starts: its terminal write may come at once.
            with self._lock:
                self._failovers[run_id] = (failover, call)
        if launch is not None:
            worker = threading.Thread(
                target=self._guarded_worker,
                args=(
                    run_id,
                    self._drive_native_run,
                    row,
                    run_dir,
                    prompt,
                    picked,
                    launch,
                    timeout_ms,
                    previous_usage,
                    native_write,
                    binding,
                    inference_profile,
                ),
                daemon=True,
            )
            worker.start()
            if call is not None:
                call.worker = worker
            return RunAccepted(run_id=run_id, phase=RunPhase.SPAWNING)
        assert thread_id is not None
        if native_write or caged_review:
            worker = threading.Thread(
                target=self._guarded_worker,
                args=(
                    run_id,
                    self._drive_claude_caged,
                    row,
                    run_dir,
                    prompt,
                    roster,
                    picked,
                    system_prompt,
                    timeout_ms,
                ),
                kwargs={"writable": native_write, "read_paths": read_paths},
                daemon=True,
            )
            worker.start()
            if call is not None:
                call.worker = worker
            return RunAccepted(run_id=run_id, phase=RunPhase.SPAWNING)
        worker = threading.Thread(
            target=self._guarded_worker,
            args=(
                run_id,
                self._drive_run,
                run_id,
                cwd,
                run_dir,
                prompt,
                roster,
                picked,
                thread_id,
                resume,
                read_only,
                read_paths,
                system_prompt,
                timeout_ms,
                execution_class,
            ),
            daemon=True,
        )
        worker.start()
        if call is not None:
            call.worker = worker
        return RunAccepted(run_id=run_id, phase=RunPhase.SPAWNING)

    # -- the worker: one child, driven start to terminal ----------------------

    def _guarded_worker(
        self, run_id: str, drive: Callable[..., None], *args: Any, **kwargs: Any
    ) -> None:
        """Run one worker; an exception none of its clauses expected ends the
        run FAILED with a named reason instead of leaving it live until its
        owner's heartbeat lapses and `mark_orphans` calls it orphaned."""
        try:
            drive(*args, **kwargs)
        except Exception as error:  # noqa: BLE001 - logged with traceback, and the run ends FAILED naming it
            _log_tick_failure(error)
            if run_id in self._unpublished:
                return  # its real terminal waits for the retry; never replace it
            try:
                with self._lock:
                    row = self._registry.get_run(run_id)
                # ORPHANED is provisional: a live worker still ends it.
                live = row is not None and row.phase not in _ENDED
            except STORE_ERRORS:
                live = True  # unread; `_update_phase` re-reads and fences an ended row
            if live:
                # The type only, as `_fail_run` records: the text may carry
                # vendor bytes. Through `_publish_terminal`, a store refusal
                # waits in `_unpublished` for the driver's retry.
                self._publish_terminal(
                    run_id,
                    terminal.TerminalClassification(
                        phase=RunPhase.FAILED,
                        final_text=None,
                        warning=None,
                        marker=None,
                        provider_wall=None,
                        account_wall=None,
                        failure_reason=f"WorkerDied: {type(error).__name__}",
                    ),
                )

    @contextlib.contextmanager
    def _launching(self, run_id: str) -> Iterator[None]:
        """Close cannot release a run between its last check and PID publication."""
        with self._lock:
            current = self._registry.get_run(run_id)
            if current is None or current.phase.is_terminal():
                raise RunClosedBeforeSpawn("run closed before its child started")
            yield

    def _drive_native_run(
        self,
        row: RunRow,
        run_dir: RunDirectory,
        prompt: str,
        picked: picker.PickerResult,
        launch: native.NativeLaunch,
        timeout_ms: int,
        previous_usage: dict[str, int],
        native_write: bool = False,
        orchestrator: native.OrchestratorBinding | None = None,
        inference_profile: dict[str, Any] | None = None,
    ) -> None:
        needles = native.NativeRedactor(launch)
        started_at = time.monotonic()
        timeout = timeout_ms / 1000 if timeout_ms > 0 else None
        api_status = None
        account_failure = None
        failure_reason: str | None = None
        quota: SourceReading | None = None

        def started(spawned: child.SpawnedChild) -> None:
            with self._lock:
                self._children[row.run_id] = spawned
            self._update_phase(
                row.run_id, RunPhase.RUNNING, process_identity=spawned.identity
            )

        def identified(thread_id: str) -> None:
            self._update_phase(
                row.run_id, RunPhase.RUNNING, thread_id=needles.redact(thread_id)
            )

        try:
            if row.sandbox == "workspace-write" and not native_write:
                raise InvalidRunRequest(_WRITE_NEEDS_NATIVE_WRITE)
            match row.backend:
                case Backend.CODEX:
                    with contextlib.ExitStack() as resources:
                        scope: ExecutorScope | None = None
                        tools: dict[str, str] | None = None
                        # A native writer and an orchestrator turn both run under
                        # the run's own aggregate scope. For the orchestrator the
                        # custody is not about writes: the vendor's MCP children
                        # run in their own process group and session (codex
                        # 0.157.1, measured 2026-10-01), so a process-group kill
                        # of the app-server leaves them alive and the only thing
                        # that reaches every descendant is the run's cgroup.
                        if native_write or orchestrator is not None:
                            # Under the launch lock: a close either sees the
                            # slice on record or refuses this launch.
                            with self._launching(row.run_id):
                                if native_write:
                                    tools = {
                                        **_disposable_environment(row.worktree),
                                        child.RUN_ID_ENV: row.run_id,
                                    }
                                    self._write_baselines[row.run_id] = time.time_ns()
                                    if child.MEMORY_POLICY_ENV in launch.env:
                                        tools[child.MEMORY_POLICY_ENV] = launch.env[
                                            child.MEMORY_POLICY_ENV
                                        ]
                                writer = scope = self._open_writer(row.run_id)
                            # First in, so last out: the slice is emptied after
                            # the turn's own close and before any terminal write
                            # may release the claim.
                            resources.callback(self._close_writer, row, writer)
                        mcp_servers = launch.mcp_servers
                        plugin_policies: dict[str, dict[str, Any]] = {}
                        # Every other turn runs in the tree it was dispatched into.
                        turn_cwd = Path(row.worktree)
                        if inference_profile is not None:
                            _orchestrator_disposable(run_dir.root)
                            if launch.inference is None:
                                raise InvalidRunRequest(
                                    "native inference composition is missing"
                                )
                            try:
                                launch.inference.materialize()
                            except ValueError as invalid:
                                raise InvalidRunRequest(str(invalid)) from None
                            turn_cwd = launch.inference.cwd
                            if (
                                native.orchestrator_neutral_refusal(str(turn_cwd))
                                is not None
                            ):
                                raise InvalidRunRequest(
                                    "native inference requires a neutral run cwd"
                                )
                        elif orchestrator is not None:
                            # NO catalog read: what this turn inherits was
                            # enumerated at preparation and is already carried
                            # closed in `launch.mcp_servers`, so there is no
                            # second child to spawn and no deadline to pay for
                            # it. Its one open server is composed here, from the
                            # binding, over its own disposable needs; a name that
                            # collided with it was refused before this.
                            disposable = _orchestrator_disposable(run_dir.root)
                            tools = native.orchestrator_mcp_environment(disposable)
                            mcp_servers = {
                                **launch.mcp_servers,
                                **orchestrator.mcp_servers(disposable),
                            }
                            # THE NEUTRAL CWD, now that it exists: the app-server's
                            # own process cwd and, through it, the thread's. Not the
                            # snapshot -- an empty `projects` or `mcp_servers`
                            # override merges nothing (codex 0.159.2, measured
                            # 2026-10-01), and what closes the project layer is
                            # having no project to load. Asserted here as well as at
                            # preparation, because this is the directory the child
                            # is actually given.
                            turn_cwd = Path(native.orchestrator_neutral_cwd(disposable))
                            if (
                                refusal := native.orchestrator_neutral_refusal(
                                    str(turn_cwd)
                                )
                            ) is not None:
                                raise InvalidRunRequest(refusal)
                        elif row.sandbox == "read-only" or native_write:
                            mcp_servers, plugin_policies = self._native_readonly_mcp(
                                row,
                                run_dir,
                                launch,
                                needles,
                                None if timeout is None else started_at + timeout,
                                scope=scope,
                            )
                            if timeout is not None:
                                timeout -= time.monotonic() - started_at
                                if timeout <= 0:
                                    raise app_server.AppServerDeadlineExceeded(
                                        "native MCP preparation exhausted the turn deadline"
                                    )
                        with self._launching(row.run_id):
                            session = resources.enter_context(
                                app_server.AppServerTurn.start(
                                    launch.argv,
                                    cwd=turn_cwd,
                                    env=launch.env,
                                    run_dir=run_dir.root,
                                    needles=needles,
                                    scope=scope,
                                )
                            )
                            started(session.child)
                        result = session.run(
                            prompt,
                            model=picked.model,
                            read_only=row.sandbox == "read-only",
                            timeout=timeout,
                            thread_id=row.thread_id,
                            developer_instructions=launch.developer_instructions,
                            model_provider=launch.context.model_provider,
                            effort=picked.effort,
                            mcp_servers=mcp_servers,
                            readonly_mcp=inference_profile is None
                            and (row.sandbox == "read-only" or native_write),
                            read_roots=launch.context.read_roots,
                            plugin_mcp_policies=plugin_policies,
                            on_thread=identified,
                            native_write=native_write,
                            tool_environment=tools,
                            orchestrator=orchestrator,
                            orchestrator_profile=None
                            if orchestrator is None
                            else native.orchestrator_profile_id(row.run_id),
                            inference_profile=None
                            if inference_profile is None
                            else StructuredInference.from_request(inference_profile),
                            inference=launch.inference,
                        )
                    if result.quota is not None and picked.executed_account is not None:
                        with self._lock:
                            self._registry.record_account_usage(
                                tuple(
                                    AccountUsageRow(
                                        account_alias=picked.executed_account,
                                        usage_window=window.window,
                                        utilization=window.utilization,
                                        resets_at=window.resets_at,
                                        observed_at=result.quota.fetched_at,
                                    )
                                    for window in result.quota.windows
                                )
                            )
                    usage = result.usage
                    success = result.status == "completed"
                    stop_reason = result.status
                    text, error = result.final_text, result.error
                    if result.quota_warning is not None:
                        note = "native quota unavailable: " + result.quota_warning
                        error = note if error is None else error + "; " + note
                    api_status = result.api_error_status
                    account_failure = result.account_failure
                    quota = result.quota
                    failure_reason = result.failure_reason
                case Backend.AGY:
                    confined = native.agy_confined(
                        launch,
                        cwd=Path(row.worktree),
                        run_dir=run_dir.root,
                        writable=native_write,
                    )
                    with contextlib.ExitStack() as resources:
                        with self._launching(row.run_id):
                            conversation = resources.enter_context(
                                agy.AgyTurn.start(
                                    confined.argv,
                                    cwd=Path(row.worktree),
                                    env=confined.env,
                                    run_dir=run_dir.root,
                                    needles=needles,
                                )
                            )
                            started(conversation.child)
                        agy_result = conversation.run(
                            prompt,
                            timeout=timeout,
                            conversation_id=row.thread_id,
                            on_thread=identified,
                        )
                    usage = agy_result.usage
                    success = agy_result.status == "SUCCESS"
                    stop_reason = agy_result.status
                    text, error = agy_result.final_text, agy_result.error
                    if agy_result.status == "CANCELED" and error is None:
                        error = "AGY turn canceled"
                    failure_reason = (
                        None if success else f"NativeTurnFailed ({agy_result.status})"
                    )
                case _:
                    raise InvalidRunRequest(
                        "native worker received a non-native backend"
                    )
            if account_failure is not None and picked.executed_account is not None:
                self._wall_rejected_credential(
                    picked, limit_type=account_failure, quota=quota
                )
            delta = {
                key: value - previous_usage.get(key, 0)
                for key, value in usage.items()
                # A partial AGY baseline leaves omitted counters unknown.
                if not previous_usage or key in previous_usage
            }
            if any(value < 0 for value in delta.values()):
                raise InvalidRunRequest("native cumulative usage moved backwards")
            if row.backend is Backend.CODEX:
                tokens = delta.get("totalTokens", 0)
                input_tokens = delta.get("inputTokens")
                output_tokens = delta.get("outputTokens")
                cache_read = delta.get("cachedInputTokens")
                cache_write = delta.get("cacheWriteInputTokens")
            else:
                tokens = delta.get("total_tokens", 0)
                cache_read = delta.get("cache_read_tokens")
                raw_input = delta.get("input_tokens")
                input_tokens = (
                    raw_input + cache_read
                    if raw_input is not None and cache_read is not None
                    else None
                )
                output_tokens = delta.get("output_tokens")
                cache_write = None
            with self._lock:
                self._registry.note_record_observed(
                    row.run_id,
                    int(time.time() * 1000),
                    count=run_dir.stdout.read_bytes().count(b"\n"),
                )
            self._update_phase(
                row.run_id,
                RunPhase.RUNNING,
                last_item=json.dumps(
                    {
                        "native_usage": usage,
                        "native_usage_scope": "turn"
                        if row.backend is Backend.CODEX
                        else "conversation",
                    }
                ),
            )
            self._publish_terminal(
                row.run_id,
                terminal.TerminalClassification(
                    phase=RunPhase.COMPLETED if success else RunPhase.FAILED,
                    final_text=needles.redact(text) if text is not None else None,
                    warning=needles.redact(error) if error is not None else None,
                    marker=None,
                    provider_wall=None,
                    account_wall=None,
                    tokens=tokens,
                    api_error_status=api_status,
                    failure_reason=failure_reason,
                    telemetry=terminal.RunTelemetry(
                        cost_usd=None,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        cache_read_input_tokens=cache_read,
                        cache_creation_input_tokens=cache_write,
                        duration_ms=int((time.monotonic() - started_at) * 1000),
                        duration_api_ms=None,
                        num_turns=1,
                        ttft_ms=None,
                        ttft_stream_ms=None,
                        time_to_request_ms=None,
                        stop_reason=stop_reason,
                        api_error_status=api_status,
                        permission_denials=None,
                        subagent_spawned=None,
                        model_usage=(),
                        failure=None
                        if success
                        else account_failure or "native_turn_failed",
                    ),
                ),
            )
        except RunClosedBeforeSpawn:
            return
        except app_server.AppServerDeadlineExceeded, agy.AgyDeadlineExceeded:
            self._fail_run(
                row.run_id, RunDeadlineExceeded("native turn deadline exceeded")
            )
        except (NSpawnError, OSError, *STORE_ERRORS, ControllerRefusal) as error:
            # Protocol/preparation errors carry safe diagnostics. Refresh refusal
            # must not be followed by a write of unredacted vendor text.
            self._fail_run(row.run_id, error, needles=needles)
        finally:
            with self._lock:
                self._children.pop(row.run_id, None)
                self._writers.pop(row.run_id, None)
                self._write_baselines.pop(row.run_id, None)

    def _native_readonly_mcp(
        self,
        row: RunRow,
        run_dir: RunDirectory,
        launch: native.NativeLaunch,
        needles: native.NativeRedactor,
        deadline: float | None,
        *,
        scope: ExecutorScope | None = None,
    ) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
        """Read the CLI catalog under existing custody without persisting credentials."""
        output = bytearray()
        consumed = 0
        capture_lock = threading.Lock()
        now = time.monotonic()
        capped = deadline is None
        if deadline is None:
            deadline = now + self.NATIVE_CATALOG_CAP_S
        elif deadline <= now:
            raise app_server.AppServerDeadlineExceeded(
                "native MCP catalog deadline exceeded"
            )

        def capture(line: bytes, keep: bool) -> bytes:
            nonlocal consumed
            with capture_lock:
                consumed += len(line)
                if consumed > 1024 * 1024:
                    raise native.NativePreparationError(
                        "native MCP catalog exceeds its byte limit"
                    )
                if keep:
                    output.extend(line)
            return b""

        directory = run_dir.root / "mcp_inventory"
        directory.mkdir(mode=0o700)
        binding_dir = run_dir.root / "mcp_binding"
        binding_dir.mkdir(mode=0o700)
        sibling_key = preparation_sibling_key(row.run_id)
        # The CLI catalog and the installed-plugin bindings are independent
        # reads of the same home: both launch before either is awaited, so the
        # turn deadline pays for the slower one, not for their sum. The catalog
        # child holds the sibling custody slot, which close stops too.
        # prepare_native owns the fixed app-server/--listen/stdio:// suffix.
        argv = (*launch.argv[:-3], "mcp", "list", "--json")
        spawned: child.SpawnedChild | None = None
        inspector: app_server.AppServerTurn | None = None
        watcher: threading.Timer | None = None
        expired = threading.Event()
        try:
            with contextlib.ExitStack() as resources:
                with self._launching(row.run_id):
                    catalog_child = spawned = child.spawn_child(
                        argv,
                        Path(row.worktree),
                        launch.env,
                        directory,
                        needles,
                        stdout_transform=lambda line: capture(line, True),
                        stderr_transform=lambda line: capture(line, False),
                        scope=scope,
                    )
                    self._children[sibling_key] = spawned

                # The catalog deadline holds on its own: an inspector slower
                # than the deadline must not let an expired catalog through.
                def expire() -> None:
                    if catalog_child.poll() is None:
                        expired.set()
                        # Already gone (or reaped) between poll and signal.
                        with contextlib.suppress(child.ChildIdentityError):
                            catalog_child.stop_then_kill(catalog_child.identity)

                watcher = threading.Timer(max(0.0, deadline - time.monotonic()), expire)
                watcher.daemon = True
                watcher.start()
                with self._launching(row.run_id):
                    inspector = resources.enter_context(
                        app_server.AppServerTurn.start(
                            launch.argv,
                            cwd=Path(row.worktree),
                            env=launch.env,
                            run_dir=binding_dir,
                            needles=needles,
                            scope=scope,
                        )
                    )
                    self._children[row.run_id] = inspector.child
                policies = inspector.readonly_mcp_policies(
                    timeout=deadline - time.monotonic(),
                    expected_skill_roots=launch.plugin_skill_roots,
                )
            # The watcher bounds the exit itself; the floor only lets the pumps
            # drain a catalog that exited just before its deadline.
            try:
                exit_code = spawned.wait(timeout=max(0.5, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                expired.set()
            if expired.is_set():
                raise app_server.AppServerDeadlineExceeded(
                    "native MCP catalog deadline exceeded"
                )
            if exit_code != 0:
                raise native.NativePreparationError("native MCP catalog command failed")
            try:
                catalog = json.loads(output.decode("utf-8"))
            except ValueError, UnicodeError, RecursionError:
                raise native.NativePreparationError(
                    "native MCP catalog is malformed"
                ) from None
            projected = native.readonly_mcp_servers(
                launch.mcp_servers, catalog, needles
            )
            return projected, policies
        except app_server.AppServerDeadlineExceeded as expired_step:
            if not capped:
                raise
            # The caller named no deadline: the cap is a vendor that did not start.
            # The detail is nSPAWN's own text and names the step that timed out.
            raise app_server.AppServerStartupTimeout(
                "native MCP preparation got no answer within "
                f"{self.NATIVE_CATALOG_CAP_S:g}s: {expired_step.detail}"
            ) from None
        finally:
            if watcher is not None:
                watcher.cancel()
                watcher.join()
            try:
                if spawned is not None:
                    if spawned.poll() is None:
                        spawned.stop_then_kill(spawned.identity)
                    spawned.wait(timeout=2)
            finally:
                output.clear()
                with self._lock:
                    if (
                        spawned is not None
                        and self._children.get(sibling_key) is spawned
                    ):
                        self._children.pop(sibling_key, None)
                    if (
                        inspector is not None
                        and self._children.get(row.run_id) is inspector.child
                    ):
                        self._children.pop(row.run_id, None)

    #: Default preparation deadline in seconds when the caller gives no run deadline.
    #: 2x the slowest of 1491 measured codex preparations (29 s, 2026-10).
    NATIVE_CATALOG_CAP_S: Final = 60.0

    #: ponytail: a native writer's slice takes the aggregate ceiling
    #: `AggregateScope` admits (8 GiB, 4096 tasks); a roster knob when a
    #: measured run needs less or more.
    WRITER_MEMORY_BYTES: Final = 8 * 1024**3
    WRITER_TASKS: Final = 4096

    def _open_writer(self, run_id: str) -> AggregateScope:
        """The writer's own slice, on record before anything is launched into it."""
        writer = AggregateScope(
            memory_bytes=self.WRITER_MEMORY_BYTES, tasks=self.WRITER_TASKS
        )
        try:
            with self._lock:
                self._registry.append_run_event(
                    run_id,
                    at=int(time.time() * 1000),
                    kind="lifecycle",
                    payload={
                        "writer_scope": {
                            "unit": writer.unit,
                            "cgroup": str(writer.path),
                        }
                    },
                )
                self._writers[run_id] = writer
        except BaseException:
            writer.close()
            raise
        return writer

    def _close_writer(self, row: RunRow, writer: AggregateScope) -> None:
        """Empty and stop the writer's slice; one that cannot be seen empty keeps
        the worktree fenced, so the failure a terminal write records next cannot
        release it."""
        try:
            writer.close()
        except ControllerRefusal as refusal:
            with self._lock:
                self._registry.poison_claim(row.worktree, row.run_id)
            raise RunCloseRejected(
                _undrained(row.run_id, writer.unit, refusal)
            ) from refusal

    #: How long an exhausted quota (402) walls its alias: no reset time came
    #: with it, so nSPAWN's default hour stands in for the vendor's.
    CREDENTIAL_WALL_S: Final = 3600
    #: How long a rejected credential (401/403) walls its alias. Not an hour:
    #: a rejection is not always the credential's fault -- on 2026-09-23 the
    #: caller's own ANTHROPIC_BASE_URL reached the child and a valid token was
    #: walled for an hour, twice. The wall only has to outlast a burst of
    #: dispatches, so a dead token is not retried by every run in it; a
    #: re-login is served minutes later. The stored source still reads
    #: `default_1h` -- the closed vocabulary names "nSPAWN's default, not the
    #: vendor's", and widening it is a schema migration this does not need.
    CREDENTIAL_REJECTED_WALL_S: Final = terminal.ACCOUNT_REFUSED_WALL_S
    RATE_LIMIT_WALL_S: Final = 300

    def _drive_claude_caged(
        self,
        row: RunRow,
        run_dir: RunDirectory,
        prompt: str,
        roster: Roster,
        picked: picker.PickerResult,
        system_prompt: str | None,
        deadline_ms: int,
        *,
        writable: bool,
        read_paths: tuple[str, ...] = (),
    ) -> None:
        """One caged Claude turn in its own slice: a writer (`writable`) or a
        read-only review, which writes nothing but its scratch and holds no claim.

        No session-id re-spawn. A read-only turn whose credential the vendor
        refuses on an unpinned account, or a continuation whose
        account reports a subscription limit, continues the same native history
        on the provider's next usable account, in the same emptied slice;
        otherwise a caged turn runs once, on the account it was admitted on. A
        writer never moves inside the run: after its terminal, `_fail_over`
        gives its work to the next route only while its tree shows no write.
        The slice is emptied before the terminal write may release a writer's
        claim.
        """
        run_id = row.run_id
        assert row.thread_id is not None
        system_prompt_file: Path | None = None
        if writable:
            # The cage keeps its scratch and its config outside the tree.
            with self._lock:
                self._write_baselines[run_id] = time.time_ns()
        try:
            if system_prompt is not None:
                system_prompt_file = run_dir.root / "system-prompt"
                with system_prompt_file.open("xb") as handle:
                    handle.write(system_prompt.encode("utf-8"))
            with contextlib.ExitStack() as resources:
                scratch = claude_restricted.make_scratch()
                # First in, so last out: removed only once the slice is empty,
                # never while a descendant may still write under its name.
                resources.callback(shutil.rmtree, scratch, ignore_errors=True)
                with self._launching(run_id):
                    writer = self._open_writer(run_id)
                # First in, so last out: emptied before any terminal write.
                resources.callback(self._close_writer, row, writer)
                profile = self._registry.session_profile(run_id)
                if profile is None:
                    raise InvalidRunRequest("caged run has no verified session profile")
                resume = profile.original_run_id != run_id
                deadline_at = (
                    time.monotonic() + deadline_ms / 1000 if deadline_ms > 0 else None
                )
                tried: set[str] = set()
                # Only the accounts that refused this turn: the re-pick adds to
                # `tried` an account the cage cannot reach, which refused nothing.
                refused: set[str] = set()
                # Of those, the accounts whose credential the vendor refused.
                rejected: set[str] = set()
                refusal: str | None = None
                # The last refused attempt, and the account that refused it.
                refused_turn: (
                    tuple[terminal.TerminalClassification, picker.PickerResult] | None
                ) = None
                # Each account this turn left for another one (`_moved_note`).
                moved: list[str] = []
                while True:
                    if deadline_at is not None and time.monotonic() >= deadline_at:
                        raise RunDeadlineExceeded(
                            f"run exceeded its caller-named deadline of {deadline_ms} "
                            "ms across subscription failover"
                        )
                    classification = self._spawn_and_wait(
                        run_id,
                        row.worktree,
                        run_dir,
                        prompt,
                        roster,
                        picked,
                        row.thread_id,
                        resume,
                        not writable,
                        read_paths,
                        system_prompt_file=system_prompt_file,
                        deadline_ms=deadline_ms
                        if deadline_at is None
                        else max(1, int((deadline_at - time.monotonic()) * 1000)),
                        writer=writer,
                        scratch=scratch,
                    )
                    wall = classification.account_wall
                    if (
                        resume
                        and wall is not None
                        and wall.limit_type in terminal.SUBSCRIPTION_WINDOWS
                    ):
                        # The wall `_spawn_and_wait` just recorded excludes this alias.
                        tried.add(wall.account_alias)
                        refused.add(wall.account_alias)
                    elif not writable and self._should_fail_over(
                        picked, classification
                    ):
                        self._wall_rejected_credential(
                            picked, limit_type=_limit_type(classification)
                        )
                        tried.add(picked.executed_account)  # type: ignore[arg-type]
                        refused.add(picked.executed_account)  # type: ignore[arg-type]
                        rejected.add(picked.executed_account)  # type: ignore[arg-type]
                        refused_turn = (classification, picked)
                    else:
                        break
                    nxt = self._repick_after_credential_rejection(
                        roster,
                        picked,
                        tried,
                        execution_class=profile.execution_class,
                        caged=True,
                    )
                    if not isinstance(nxt, picker.PickerResult):
                        if nxt is not None:
                            refusal = str(nxt)
                            classification = replace(
                                classification,
                                warning=f"{classification.warning}; the next "
                                f"account was refused: {nxt}",
                            )
                        break
                    # shortcut: the retry resends the prompt, so the native history
                    # holds it twice; upgrade when the CLI resumes without a prompt.
                    writer.drain()
                    self._archive_attempt_evidence(run_dir, len(tried))
                    (run_dir.root / claude_restricted.SETTINGS_FILE).unlink()
                    shutil.rmtree(scratch / "work")
                    moved.append(_moved_note(picked, classification))
                    picked = nxt
                    # The refused turn is already in the native history, and the
                    # CLI refuses its session id a second time.
                    resume = True
                    self._update_phase(
                        run_id, RunPhase.RUNNING, executed_account=nxt.executed_account
                    )
            # An account the loop walled keeps that wall and its label.
            if picked.executed_account not in refused and (
                self._is_credential_rejection(picked, classification)
            ):
                self._wall_rejected_credential(
                    picked, limit_type=_limit_type(classification)
                )
            claude_restricted.require_sandbox_started(run_dir.stdout)
            if (
                refused_turn is not None
                and classification.phase is RunPhase.FAILED
                and _route_refusal(classification) is None
            ):
                lost = (
                    f"the turn could not continue on {picked.executed_account}: "
                    f"{classification.warning or 'no vendor answer'}"
                )
                classification, picked = refused_turn
                classification = replace(
                    classification,
                    warning=_composed_warning(classification.warning, lost),
                )
                # Its note would name the account that never refused.
                rejected = set()
                moved = []
                self._update_phase(
                    run_id, RunPhase.RUNNING, executed_account=picked.executed_account
                )
            if refused:
                note = f"refused on {', '.join(sorted(refused))}"
                if picked.executed_account not in refused:
                    note += f"; the turn then ran on {picked.executed_account}"
                if moved:
                    note += "; " + "; ".join(moved)
                classification = replace(
                    classification,
                    warning=_composed_warning(classification.warning, note),
                )
                self._refuse_accounts(run_id, refused)
            self._publish_terminal(run_id, classification)
            if rejected:
                self._note_exhausted_credentials(
                    run_id,
                    picked,
                    classification,
                    rejected,
                    roster,
                    read_paths=read_paths,
                    system_prompt=system_prompt,
                    execution_class=profile.execution_class,
                    refusal=refusal,
                )
        except RunClosedBeforeSpawn:
            return
        except (
            NSpawnError,
            claude_child.ClaudeChildError,
            OSError,
            *STORE_ERRORS,
            ControllerRefusal,
        ) as error:
            self._fail_run(run_id, error)
        finally:
            with self._lock:
                self._children.pop(run_id, None)
                self._writers.pop(run_id, None)
                self._write_baselines.pop(run_id, None)
            if system_prompt_file is not None:
                with contextlib.suppress(FileNotFoundError):
                    system_prompt_file.unlink()

    def _drive_run(
        self,
        run_id: str,
        cwd: str,
        run_dir: RunDirectory,
        prompt: str,
        roster: Roster,
        picked: picker.PickerResult,
        thread_id: str,
        resume: bool,
        read_only: bool,
        read_paths: tuple[str, ...] = (),
        system_prompt: str | None = None,
        deadline_ms: int = 0,
        #: The class travels all the way because the substitute clause needs
        #: it: the class buys effort, and a route that does not serve it is not
        #: a place to send this work.
        execution_class: str | None = None,
    ) -> None:
        system_prompt_file: Path | None = None
        if system_prompt is not None:
            candidate = run_dir.root / "system-prompt"
            try:
                prompt_file = candidate.open("xb")
            except OSError as error:
                self._fail_run(
                    run_id,
                    RunDirectoryError(
                        f"system prompt file {candidate} could not be reserved: {error}"
                    ),
                )
                return
            system_prompt_file = candidate
            with prompt_file:
                prompt_file.write(system_prompt.encode("utf-8"))
        # CREDENTIAL FAILOVER (owner ruling 2026-08-30): when the vendor
        # rejects THIS account's credential (401/403), the supervisor walls
        # that alias and re-spawns the SAME run on the next unwalled account,
        # bounded by one attempt per account. A caller who PINNED the alias by
        # name owns the failure — no retry. An env-route provider (no account
        # at all) has nothing to rotate.
        tried: set[str] = set()
        # ONE INSTANT, computed ONCE. The loop below RE-SPAWNS the same run on the
        # next account when the vendor rejects the credential, and passing
        # the entire `deadline_ms` to each iteration gave the run N times the deadline
        # requested by the caller: with three accounts, a `timeout_ms=60000` lived ~180 s.
        #
        # Caught by independent review on this same candidate, and what
        # exposes it is written in the code itself: the comment in
        # `_spawn_and_wait` says "THE DEADLINE BELONGS TO THE RUN" and the implementation did it per
        # ATTEMPT. A deadline that multiplies with the number of accounts is not a
        # deadline — it is a suggestion.
        deadline_at = time.monotonic() + deadline_ms / 1000 if deadline_ms > 0 else None
        # ONE attempt counter for the two re-spawns (credential and
        # session id collision): each counting independently gave two
        # archived streams the same name `.attempt-1`, and renaming the second ate the
        # evidence of the first.
        attempt = 0
        # The ID that collided, when already re-spawned once for that reason. Once
        # only: the second is a named failure, never a loop.
        collided_id: str | None = None
        # Each account this run left for another one (`_moved_note`).
        moved: list[str] = []

        def noted(
            classification: terminal.TerminalClassification,
        ) -> terminal.TerminalClassification:
            if not moved:
                return classification
            return replace(
                classification,
                warning=_composed_warning(classification.warning, "; ".join(moved)),
            )

        try:
            while True:
                attempt += 1
                attempt_ms = deadline_ms
                if deadline_at is not None:
                    remaining = deadline_at - time.monotonic()
                    if remaining <= 0:
                        # The deadline expired between attempts: no new iteration.
                        self._fail_run(
                            run_id,
                            RunDeadlineExceeded(
                                f"run exceeded its caller-named deadline of "
                                f"{deadline_ms} ms across credential failover"
                            ),
                        )
                        return
                    attempt_ms = max(1, int(remaining * 1000))
                try:
                    classification = self._spawn_and_wait(
                        run_id,
                        cwd,
                        run_dir,
                        prompt,
                        roster,
                        picked,
                        thread_id,
                        resume,
                        read_only,
                        read_paths,
                        system_prompt_file=system_prompt_file,
                        deadline_ms=attempt_ms,
                    )
                except RunClosedBeforeSpawn:
                    return
                except (NSpawnError, claude_child.ClaudeChildError, OSError) as error:
                    self._fail_run(run_id, error)
                    return
                if classification is None:
                    classification = terminal.TerminalClassification(
                        phase=RunPhase.COMPLETED,
                        final_text=None,
                        warning=None,
                        marker=None,
                        provider_wall=None,
                        account_wall=None,
                    )
                if (
                    classification.phase is RunPhase.FAILED
                    and not resume
                    and _session_id_collided(run_dir.stderr)
                ):
                    if collided_id is not None:
                        self._fail_run(
                            run_id,
                            SessionIdCollision(
                                f"session id {thread_id!r} was refused as already "
                                f"in use after {collided_id!r} was too; not "
                                "re-spawning a third time"
                            ),
                        )
                        return
                    collided_id = thread_id
                    thread_id = claude_child.mint_session_id()
                    self._archive_attempt_evidence(run_dir, attempt)
                    # The row carries the ID that will actually run: a later resume
                    # points to the thread that existed, not the refused one.
                    self._update_phase(run_id, RunPhase.RUNNING, thread_id=thread_id)
                    continue
                if collided_id is not None:
                    note = (
                        f"session id {collided_id} was already in use at the vendor; "
                        f"re-spawned once as {thread_id}"
                    )
                    classification = replace(
                        classification,
                        warning=note
                        if classification.warning is None
                        else f"{classification.warning}; {note}",
                    )
                # The wall belongs to the CREDENTIAL and is written before the
                # branch below, so an alias the vendor refused stops being
                # eligible even when there is nothing to rotate to. The re-pick
                # relies on it: it reads `account_walls` to skip this alias.
                if (
                    self._is_credential_rejection(picked, classification)
                    and classification.account_wall is None
                ):
                    self._wall_rejected_credential(
                        picked, limit_type=_limit_type(classification)
                    )
                if not self._should_fail_over(picked, classification):
                    self._publish_terminal(run_id, noted(classification))
                    self._note_exhausted_credentials(
                        run_id,
                        picked,
                        classification,
                        tried,
                        roster,
                        read_paths=read_paths,
                        system_prompt=system_prompt,
                        execution_class=execution_class,
                    )
                    return
                tried.add(picked.executed_account)  # type: ignore[arg-type]
                nxt = self._repick_after_credential_rejection(
                    roster, picked, tried, execution_class=execution_class
                )
                if nxt is None or isinstance(nxt, picker.PickerError):
                    self._publish_terminal(run_id, noted(classification))
                    self._note_exhausted_credentials(
                        run_id,
                        picked,
                        classification,
                        tried,
                        roster,
                        read_paths=read_paths,
                        system_prompt=system_prompt,
                        execution_class=execution_class,
                        refusal=None if nxt is None else str(nxt),
                    )
                    return
                self._archive_attempt_evidence(run_dir, attempt)
                moved.append(_moved_note(picked, classification))
                picked = nxt
                self._update_phase(
                    run_id, RunPhase.RUNNING, executed_account=nxt.executed_account
                )
        finally:
            if system_prompt_file is not None:
                try:
                    system_prompt_file.unlink()
                except OSError as error:
                    self._fail_run(
                        run_id,
                        RunDirectoryError(
                            f"system prompt file {system_prompt_file} "
                            f"could not be removed: {error}"
                        ),
                    )

    def _keep_whole_answer(self, run_id: str, text: str) -> str:
        """Write an answer over the row's cap to the run's `final_text` file,
        which `nspawn_result_show` answers from; return the warning naming it."""
        data = text.encode("utf-8")
        path = self._run_root_path / run_id / FINAL_TEXT_FILE
        whole = (
            f"final answer of {len(data)} bytes exceeds the row's "
            f"{MAX_DURABLE_OBSERVATION_BYTES}; nspawn_result_show answers it whole "
            f"from {path}"
        )

        def cut(reason: str) -> str:
            return (
                f"final answer of {len(data)} bytes cut to "
                f"{MAX_DURABLE_OBSERVATION_BYTES}; the whole was not kept ({reason})"
            )

        if len(data) > KEPT_ANSWER_MAX_BYTES:
            return cut(f"over the {KEPT_ANSWER_MAX_BYTES} bytes a run may output")
        try:
            directory = self._open_run_directory(run_id)
        except OSError as error:
            return cut(type(error).__name__)
        try:
            # Idempotent: a retry finds what an earlier attempt kept, never
            # rewrites it, and still recomputes the directory sync below.
            kept = False
            with contextlib.suppress(OSError):
                fd = _open_regular(directory, FINAL_TEXT_FILE, os.O_RDONLY)
                with open(fd, "rb") as earlier:
                    # Bounded: a planted sparse file cannot raise MemoryError.
                    kept = earlier.read(len(data) + 1) == data
            if not kept:
                # Whole or absent: a partial file would outrank the bounded row.
                partial = f"{FINAL_TEXT_FILE}.partial"
                try:
                    fd = _open_regular(
                        directory, partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC
                    )
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(data)
                        stream.flush()
                        os.fsync(stream.fileno())  # data on disk before the name is
                    os.replace(
                        partial,
                        FINAL_TEXT_FILE,
                        src_dir_fd=directory,
                        dst_dir_fd=directory,
                    )
                except OSError as error:
                    note = cut(type(error).__name__)
                    try:
                        os.unlink(partial, dir_fd=directory)
                    except FileNotFoundError:
                        pass
                    except OSError as cleanup:
                        note += f"; {partial} left behind ({type(cleanup).__name__})"
                    return note
            # Best effort: the file is whole either way; only its name may not last.
            try:
                os.fsync(directory)
            except OSError as error:
                return f"{whole}; its directory was not synced ({type(error).__name__})"
            return whole
        finally:
            os.close(directory)

    def _open_run_directory(self, run_id: str) -> int:
        """The run's own directory as a descriptor, never reached through a link."""
        return os.open(
            self._run_root_path / run_id,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )

    def _publish_terminal(
        self, run_id: str, classification: terminal.TerminalClassification
    ) -> None:
        """The one place a run's terminal phase reaches the registry."""
        try:
            # Inside the guard: a store fault here queues the real terminal data.
            refusal = _route_refusal(classification)
            reason = refusal or self._retry_reason(
                run_id, classification.phase, classification.failure_reason
            )
            if (
                note := self._fail_over(run_id, reason, spends=refusal is None)
            ) is not None:
                classification = replace(
                    classification,
                    warning=note
                    if classification.warning is None
                    else f"{classification.warning}; {note}",
                )
            warning = classification.warning
            if classification.telemetry is not None:
                with self._lock:
                    row = self._registry.get_run(run_id)
                telemetry = replace(
                    classification.telemetry,
                    witnessed_effect=(
                        "unreadable"
                        if row is None
                        else worktree.observed_effect(
                            Path(row.worktree), since_ns=row.created_at * 1_000_000
                        )
                    ),
                )
                usage_payload = telemetry.as_payload()
            else:
                usage_payload = {"input_tokens": None, "output_tokens": None}
            try:
                with self._lock:
                    self._registry.append_run_event(
                        run_id,
                        at=int(time.time() * 1000),
                        kind="usage",
                        payload=usage_payload,
                    )
            except RegistryError as refusal:
                warning = _bounded_text(
                    f"telemetry not recorded: {refusal}"
                    if warning is None
                    else f"{warning}; telemetry not recorded: {refusal}",
                    16 * 1024,
                )
            final_text = classification.final_text
            if (
                final_text is not None
                and len(final_text.encode("utf-8")) > MAX_DURABLE_OBSERVATION_BYTES
            ):
                note = (
                    self._keep_whole_answer(run_id, final_text)
                    if classification.phase is RunPhase.COMPLETED
                    else f"final answer of {len(final_text.encode('utf-8'))} bytes "
                    f"cut to {MAX_DURABLE_OBSERVATION_BYTES}; a {classification.phase.value} "
                    "run's answer is not kept"
                )
                # First, so no cap on the warning can cut it.
                warning = note if warning is None else f"{note}; {warning}"
            # False: a close already won and announced CLOSED — one run, one notice.
            published = self._update_phase(
                run_id,
                classification.phase,
                final_text=final_text,
                warning=warning,
                marker=classification.marker,
                tokens=classification.tokens,
                blocked_reason=classification.failure_reason,
            )
        except (*STORE_ERRORS, OSError) as error:
            # THE NOTICE FOLLOWS THE RUN, NOT THE REGISTRY. The phase occurred; what
            # failed was writing it, so there is no armed row to push yet. The driver
            # retries the write on reconnect before it exits (`publish_pending`);
            # the write arms the row and pushes. If the store stays down past that,
            # the run is orphaned once the driver's heartbeat stops, and the
            # orphan arms the notice instead. Queued first: no file write below
            # can drop it.
            self._unpublished[run_id] = classification
            data = {
                "run_id": run_id,
                "phase": classification.phase.value,
                "final_text": classification.final_text,
                "warning": classification.warning,
                "marker": classification.marker,
                "tokens": classification.tokens,
                "error": f"{type(error).__name__}: {error}",
            }
            with contextlib.suppress(OSError):
                directory = self._open_run_directory(run_id)
                try:
                    fd = _open_regular(
                        directory,
                        TERMINAL_FILE,
                        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                    )
                finally:
                    os.close(directory)
                with open(fd, "w", encoding="utf-8") as stream:
                    stream.write(json.dumps(data, indent=2))
            _log_tick_failure(error)
            return
        # Outside the refusal branch: the terminal is committed, and nothing the
        # push raises may queue it again.
        if published is not False:
            self._announce(run_id)

    #: How often, and how far apart, a driver retries a refused terminal write.
    PUBLISH_ATTEMPTS: Final = 5
    PUBLISH_RETRY_S: Final = 1.0

    def lock_key(self, project: str, key: str, timeout_s: float) -> KeyLock:
        """Hold the store's key lock until `commit`; reads go through this registry."""
        return KeyLock(self._registry, self._lock, project, key, timeout_s)

    def release_key(self, project: str, key: str, timeout_s: float) -> KeyedRun | None:
        """The run `key` names in `project`, read under the key lock, or None with
        the key fenced: a driver of it that has not admitted yet never will."""
        lock = self.lock_key(project, key, timeout_s)
        try:
            with lock.transaction():
                hit = lock.find()
                if hit is None:
                    lock.fence()
            return self._own_key(key, hit)
        finally:
            lock.close()

    def find_key(self, project: str, key: str) -> KeyedRun | None:
        """The run `key` names in `project`, read without taking the key lock."""
        with self._lock:
            hit = self._registry.find_key(project, key)
        return self._own_key(key, hit)

    def _own_key(self, key: str, hit: KeyedRun | None) -> KeyedRun | None:
        """A node reaches by key only a run it admitted; another's is not named."""
        if hit is not None and self.owner is not None and hit.owner != self.owner:
            raise IdempotencyConflict(
                f"idempotency key {key!r} already names a run another caller "
                "admitted; use another key for this one"
            )
        return hit

    def run_of_launch(self, launch_uuid: str) -> str | None:
        with self._lock:
            return self._registry.run_of_launch(launch_uuid)

    def publish_pending(self) -> bool:
        """Retry every registry-refused terminal write on a fresh connection.

        Called by the driver before it exits. True when nothing is left.
        """
        for attempt in range(self.PUBLISH_ATTEMPTS):
            if not self._unpublished:
                return True
            if attempt:
                time.sleep(self.PUBLISH_RETRY_S)
            try:
                with self._lock:
                    self._registry.reconnect()
            except NSpawnError as error:
                _log_tick_failure(error)
                continue
            for run_id, classification in tuple(self._unpublished.items()):
                del self._unpublished[run_id]
                self._publish_terminal(run_id, classification)
        return not self._unpublished

    def fail_unended(self, run_id: str) -> None:
        """At driver exit, end FAILED a run that no terminal write ended.

        Called before `shutdown` drops this instance: else a peer's orphan pass
        names the exit a lapsed heartbeat, and no notice names it. An unread
        row is left to that pass.
        """
        try:
            with self._lock:
                row = self._registry.get_run(run_id)
        except STORE_ERRORS as error:
            _log_tick_failure(error)
            return
        # A queued terminal is the run's own: its retry writes it, failover and all.
        if (
            row is not None
            and row.phase not in _ENDED
            and run_id not in self._unpublished
        ):
            self._publish_terminal(
                run_id,
                terminal.TerminalClassification(
                    phase=RunPhase.FAILED,
                    final_text=None,
                    warning=None,
                    marker=None,
                    provider_wall=None,
                    account_wall=None,
                    failure_reason="DriverExit: the worker ended with no terminal write",
                ),
            )

    @staticmethod
    def _archive_attempt_evidence(run_dir: RunDirectory, attempt: int) -> None:
        """Move a failed attempt's streams aside so the next attempt's pumps
        can create them fresh.

        The pumps open stdout/stderr with O_CREAT|O_EXCL (pump.py
        `_open_private`): a second attempt on the same paths would die with
        FileExistsError before talking to the vendor. Renaming keeps the
        rejected attempt's evidence (stdout.attempt-1) instead of losing it —
        the 401 text is the operator's clue that a re-login is due.
        """
        for stream in (run_dir.stdout, run_dir.stderr):
            if stream.exists():
                stream.rename(stream.with_name(f"{stream.name}.attempt-{attempt}"))

    def _is_credential_rejection(
        self,
        picked: picker.PickerResult,
        classification: terminal.TerminalClassification,
    ) -> bool:
        """Whether the VENDOR rejected this credential, whoever chose it: the
        credential itself, its quota or its rate.

        This is the error CLASS alone, and it is what authorises the WALL. It
        deliberately says nothing about spares or about who named the account:
        a credential the vendor refuses is dead for the next caller too. A wall
        event may accompany any class and never by itself makes a failure a
        credential rejection.
        """
        # Signal termination is an OS process fault, never rotatable.
        if (
            classification.warning is not None
            and classification.warning.startswith("child terminated by signal")
        ) or (
            classification.telemetry is not None
            and classification.telemetry.failure == "child_signal"
        ):
            return False

        # Provider-level wall (e.g. proxy unavailable) affects all accounts of the provider.
        if classification.provider_wall is not None:
            return False

        if classification.api_error_status is not None:
            if classification.api_error_status not in _ACCOUNT_STATUSES:
                return False
        elif classification.account_wall is None:
            # Without an HTTP status and without an account wall, the failure is
            # non-rotatable (transport drop, unparseable output, etc.).
            return False

        return picked.executed_account is not None

    def _should_fail_over(
        self,
        picked: picker.PickerResult,
        classification: terminal.TerminalClassification,
    ) -> bool:
        """A rejected credential with somewhere else to go, on an unpinned account.

        Rotation needs a spare; marking the dead credential does not, and
        conflating the two is what let a refused alias stay eligible — measured
        2026-09-19, run 4a73702c, with a single token in the secrets dir.
        """
        if not self._is_credential_rejection(picked, classification):
            return False
        # `pick_execution` pins the alias when the caller named the ACCOUNT
        # instead of the provider (picker.py: `pinned_alias`); that caller
        # chose this credential on purpose.
        if (
            picked.requested_provider is not None
            and picked.requested_provider != picked.executed_provider.name
        ):
            return False
        accounts = picked.executed_provider.discovered_accounts()
        return len(accounts) > 1

    def _wall_rejected_credential(
        self,
        picked: picker.PickerResult,
        limit_type: str = "credential_rejected",
        quota: SourceReading | None = None,
    ) -> None:
        now_s = int(time.time())
        until = now_s + {
            "credential_rejected": self.CREDENTIAL_REJECTED_WALL_S,
            "rate_limited": self.RATE_LIMIT_WALL_S,
        }.get(limit_type, self.CREDENTIAL_WALL_S)
        source = terminal.WallSource.DEFAULT_1H
        alias: str = picked.executed_account  # type: ignore[assignment]
        with self._lock:
            # An exhausted quota lasts until the vendor's own reset when it named
            # a future one: the LATEST across exhausted windows, so a 5h window
            # and a weekly one both at 100% never re-pick before the weekly
            # reset. A failure that carries no windows reuses the stored ones.
            if limit_type == "quota_exhausted":
                resets = [
                    window.resets_at
                    for window in (() if quota is None else quota.windows)
                    if window.utilization >= EXHAUSTED_UTILIZATION
                    and window.resets_at > now_s
                ]
                stored = self._registry.get_exhausted_walls(now_s).get(alias)
                if stored is not None:
                    resets.append(stored.cooling_until)
                if resets:
                    until, source = max(resets), terminal.WallSource.VENDOR_RESET
            self._registry.record_account_wall(
                alias,
                until,
                source.value,
                limit_type,
                now_s,
            )

    def _spent_provider_wall(
        self,
        provider: Provider,
        picked: picker.PickerResult,
        classification: terminal.TerminalClassification,
        now_s: int,
    ) -> terminal.ProviderWallRecord | None:
        """The wall on a provider that refused this run as a whole, or None.

        Three refusals no account wall can hold. A vendor code the owner
        declared in `exhausted_codes`, read only from the CLI's exact rejection
        line: Z.AI's empty balance is a 429, which the status alone cannot tell
        from a transient limit. And a 402 on an env route, which executes with
        no account to wall. Both last `CREDENTIAL_WALL_S`, as a 402 on an
        account does. Any other 429 on an env route is a rate limit, or a spent
        balance the roster did not declare: it lasts `RATE_LIMIT_WALL_S`.
        """
        if (
            classification.provider_wall is not None
            or classification.account_wall is not None
        ):
            return None
        status = classification.api_error_status
        code = terminal.rejected_code(classification.final_text, status)
        declared = code is not None and code in provider.exhausted_codes
        unaccounted_402 = status == 402 and picked.executed_account is None
        unaccounted_429 = status == 429 and picked.executed_account is None
        if not (declared or unaccounted_402 or unaccounted_429):
            return None
        return terminal.ProviderWallRecord(
            provider=provider.name,
            cooling_until=now_s
            + (
                self.CREDENTIAL_WALL_S
                if declared or unaccounted_402
                else self.RATE_LIMIT_WALL_S
            ),
            source=terminal.WallSource.DEFAULT_1H,
            observed_at=now_s,
        )

    def _repick_after_credential_rejection(
        self,
        roster: Roster,
        picked: picker.PickerResult,
        tried: set[str],
        *,
        execution_class: str | None = None,
        caged: bool = False,
    ) -> picker.PickerResult | picker.PickerError | None:
        """The next unwalled account of the same provider, None when every one is
        walled, or the picker's refusal of this request on the next account. A
        `caged` turn takes only an account the cage can serve: no other endpoint.

        The wall written a moment ago is what excludes the rejected alias —
        the picker already reads `account_walls`, so the re-pick needs no
        exclusion list of its own beyond the tried set (a wall the registry
        may already have expired between runs).
        """
        now_s = int(time.time())
        with self._lock:
            provider_walls = self._registry.get_provider_walls(now_s)
            account_walls = self._registry.get_account_walls(now_s)
            # The same picture the first pick spread by, so the next account is
            # the spread's choice and not the next one the roster declares.
            account_load = self._registry.get_account_load()
            account_use = picker.AccountUse(
                self._registry.get_account_usage(),
                self._registry.get_account_last_run(),
            )
        while True:
            # A caged turn skips what it tried, so an account the cage cannot
            # serve never hides a later one that it can.
            walls = (
                {**account_walls, **dict.fromkeys(tried, now_s + 1)}
                if caged
                else account_walls
            )
            try:
                nxt = picker.pick_execution(
                    roster,
                    now_s,
                    requested_provider=picked.executed_provider.name,
                    # The CALLER's request again, class included: a value the
                    # first pick resolved is not one the caller pinned.
                    requested_model=picked.requested_model,
                    requested_effort=picked.requested_effort,
                    execution_class=execution_class,
                    preferred_model=picked.preferred_model,
                    account_load=account_load,
                    account_use=account_use,
                    provider_walls=provider_walls,
                    account_walls=walls,
                )
            except picker.AllProvidersWalled:
                # Every remaining account is walled — that IS the exhaustion
                # this re-pick was probing for.
                return None
            except picker.PickerError as error:
                # Not exhaustion: the next account was never tried, and why is
                # the cause.
                return error
            alias = nxt.executed_account
            if alias is None or alias in tried:
                return None
            if not caged:
                break
            if not _picked_route_elsewhere(roster, nxt):
                return nxt
            # The cage's closed env carries only the credential (see admission).
            tried.add(alias)
        # An uncaged child receives the roster's env.
        try:
            _refuse_roster_override(roster, nxt)
        except picker.PickerError as error:
            return error
        return nxt

    def _refuse_accounts(self, run_id: str, accounts: set[str]) -> None:
        """Keep every account that refused this run out of its next preference."""
        with self._lock:
            if (taken := self._failovers.get(run_id)) is not None:
                failover, call = taken
                refused = replace(
                    failover.refused, accounts=failover.refused.accounts | accounts
                )
                self._failovers[run_id] = (replace(failover, refused=refused), call)

    def _note_exhausted_credentials(
        self,
        run_id: str,
        picked: picker.PickerResult,
        classification: terminal.TerminalClassification,
        tried: set[str],
        roster: Roster,
        *,
        read_paths: Sequence[str] = (),
        system_prompt: str | None = None,
        execution_class: str | None = None,
        refusal: str | None = None,
    ) -> None:
        """Name every rejected alias on the row, and where the work can go instead.
        A `refusal` of the retry on the next account is named instead of exhaustion.

        The class test is `_is_credential_rejection` itself rather than a second
        copy of its conditions: two spellings of one rule drift, and a
        non-rotatable cause overwritten by account exhaustion is a lie about
        why the run died.

        It fires with an empty `tried` too. A provider holding a single
        credential never rotates, so `tried` stays empty exactly in the case
        that produced the raw vendor string an agent then carried to the owner.
        """
        if not self._is_credential_rejection(picked, classification):
            return
        aliases = sorted(a for a in {*tried, picked.executed_account} if a is not None)
        reason_label = (
            "rate limited"
            if classification.api_error_status == 429
            else "quota exhausted"
            if (
                classification.api_error_status == 402
                or classification.account_wall is not None
            )
            else "credential rejected"
        )
        self._update_phase(
            run_id,
            classification.phase,
            annotate=True,
            warning=(
                (
                    f"{reason_label} on {', '.join(aliases)}; the retry on the next "
                    f"account of {picked.executed_provider.name} was refused: {refusal}"
                    if refusal is not None
                    else f"{reason_label} on every account of "
                    f"{picked.executed_provider.name}: {', '.join(aliases)}"
                )
                + f"; {
                    self._substitute_clause(
                        roster,
                        picked,
                        read_paths=read_paths,
                        system_prompt=system_prompt,
                        execution_class=execution_class,
                    )
                }"
            ),
        )

    def _substitute_clause(
        self,
        roster: Roster,
        picked: picker.PickerResult,
        *,
        read_paths: Sequence[str] = (),
        system_prompt: str | None = None,
        execution_class: str | None = None,
    ) -> str:
        """Where THIS work can go now, or an honest statement that it cannot.

        The run's own shape travels with the question: a caged review carries
        `read_paths` that some launchers refuse, and a class buys an effort
        that not every route serves. Asking without them names routes that
        will refuse the caller a second time.
        """
        now_s = int(time.time())
        with self._lock:
            provider_walls = self._registry.get_provider_walls(now_s)
            account_walls = self._registry.get_account_walls(now_s)
        try:
            routes = picker.serviceable_routes(
                roster,
                now_s,
                exclude=picked.executed_provider.name,
                provider_walls=provider_walls,
                account_walls=account_walls,
                execution_class=execution_class,
                read_paths=read_paths,
                system_prompt=system_prompt,
            )
        except picker.PickerError as error:
            return f"nSPAWN cannot check other routes: {error.message}"
        if not routes:
            return "no other declared route can serve this work now"
        return f"these routes can serve it now: {', '.join(routes)}"

    def _spawn_and_wait(
        self,
        run_id: str,
        cwd: str,
        run_dir: RunDirectory,
        prompt: str,
        roster: Roster,
        picked: picker.PickerResult,
        thread_id: str,
        resume: bool,
        read_only: bool,
        read_paths: tuple[str, ...] = (),
        system_prompt_file: Path | None = None,
        deadline_ms: int = 0,
        writer: AggregateScope | None = None,
        scratch: Path | None = None,
    ) -> terminal.TerminalClassification:
        provider = picked.executed_provider
        with self._lock:
            profile_data = self._registry.inference_profile(run_id)
        inference = (
            None
            if profile_data is None
            else StructuredInference.from_request(profile_data)
        )
        # ROUTE FIRST. A sleeping machine is brought up before anything
        # else: inspecting the vendor binary for an endpoint that is not
        # up is work nobody needs, and a child that started would die with
        # `connection refused`, the error that tells nobody what to
        # do. `bring_route_up` invents nothing for an account without
        # `bootstrap` or without `base_url`, so any native route passes here
        # without touching it.
        account = (
            provider.accounts.get(picked.executed_account)
            if picked.executed_account is not None
            else None
        )
        if account is not None:
            raised = bring_route_up(account)
            if raised.ran and not raised.live:
                raise SupervisorError(
                    f"account {picked.executed_account!r} of provider "
                    f"{provider.name!r} did not come up: ran "
                    f"{' '.join(raised.argv)} (exit {raised.returncode}) and "
                    f"{account.base_url} still does not answer"
                    + (f"; {raised.detail}" if raised.detail else "")
                )
        match provider.kind:
            case "claude":
                resolved = roster.resolve(provider.name)
                if resolved.plugin_root is None and inference is None:
                    raise SupervisorError(
                        f"provider {provider.name!r} reaches no plugin "
                        "directory: declare [defaults] plugins; cannot "
                        "resolve its child MCP config"
                    )
                binary = claude_child.resolve_claude_binary(
                    environment_override=_claude_bin_override(),
                    provider_binary=provider.bin,
                    roster_default=roster.bins.get("claude"),
                )
                installation = claude_child.inspect_claude_installation(
                    binary, _probe_claude_version
                )
                # The caged writer loads no plugin and no MCP server.
                if writer is not None or inference is not None:
                    mcp_config = None
                else:
                    assert resolved.plugin_root is not None
                    mcp_config = claude_child.inspect_child_mcp_config(
                        resolved.plugin_root
                    )
            case kind:
                raise child.ChildArgvError(
                    f"the Claude worker cannot execute provider {provider.name!r} "
                    f"with kind {kind!r}"
                )
        plugin_dir: Path | str | None = (
            None if mcp_config is None else mcp_config.plugin_dir
        )
        env, needles = child.build_child_env(
            provider,
            picked.executed_account,
            defaults_env=resolved.env,
            model=picked.model,
            model_fast=provider.model_fast,
            effort=picked.effort,
            plugin_root=resolved.plugin_root,
            base_environ=_allowed_base_environ(),
            run_id=run_id,
            binary=installation.binary,
        )
        memory_push = env.get(child.MEMORY_POLICY_ENV) == "push"
        if (
            (read_only or memory_push)
            and mcp_config is not None
            and mcp_config.plugin_dir is not None
            and mcp_config.plugin_dir.is_dir()
        ):
            read_only_plugin_dir = run_dir.root / (
                "read_only_plugin" if read_only else "memory_push_plugin"
            )
            read_only_plugin_dir.mkdir(exist_ok=True)
            for entry in mcp_config.plugin_dir.iterdir():
                if entry.name != claude_child.MCP_CONFIG_FILE:
                    dest = read_only_plugin_dir / entry.name
                    if not dest.is_symlink() and not dest.exists():
                        dest.symlink_to(entry)
            orig_mcp_json = mcp_config.plugin_dir / claude_child.MCP_CONFIG_FILE
            if orig_mcp_json.is_file() or memory_push:
                if read_only and memory_push:
                    servers = {}
                elif read_only:
                    cfg = json.loads(orig_mcp_json.read_bytes())
                    srv = cfg.get("mcpServers", {}).get("nmemory", {})
                    if isinstance(srv.get("args"), list):
                        if "--read-only" not in srv["args"]:
                            srv["args"] = [*srv["args"], "--read-only"]
                    else:
                        srv["args"] = ["--read-only"]
                    servers = {"nmemory": srv}
                else:
                    servers = claude_child.read_child_mcp_config(mcp_config.plugin_dir)
                    if servers["nmemory"].get("type", "stdio") != "stdio":
                        raise claude_child.ChildMcpConfigError(
                            orig_mcp_json, "managed memory requires a stdio server"
                        )
                    servers.pop("nmemory-http", None)
                    for name in ("nmemory", "nspawn"):
                        if name in servers:
                            servers[name]["env"] = {
                                **servers[name].get("env", {}),
                                child.MEMORY_POLICY_ENV: "${NOTT_MEMORY_POLICY}",
                            }
                read_only_cfg = {"mcpServers": servers}
                (read_only_plugin_dir / claude_child.MCP_CONFIG_FILE).write_bytes(
                    json.dumps(read_only_cfg).encode()
                )
                plugin_dir = read_only_plugin_dir
        env[child.SESSION_ID_ENV] = thread_id
        declared = [
            expand_value_with(arg, resolved.plugin_root) for arg in resolved.args
        ]
        if memory_push and mcp_config is not None:
            declared = child._plugin_dirs_the_explicit_one_does_not_replace(
                declared, mcp_config.plugin_dir, False
            )
        argv = child.compose_child_argv(
            installation.binary,
            session_id=thread_id,
            resume=resume,
            append_system_prompt_file=system_prompt_file,
            plugin_dir=plugin_dir,
            declared_args=_combine_system_prompt_file(cwd, system_prompt_file, declared)
            if system_prompt_file is not None
            else declared,
            read_only=read_only,
            read_paths=read_paths,
            inference_profile=inference,
        )
        if inference is not None:
            try:
                actual_home = InferenceHome.from_environment(env)
            except ValueError as error:
                raise InvalidRunRequest(str(error)) from error
            with self._lock:
                retained_home = self._registry.inference_home(run_id)
                if retained_home is None:
                    self._registry.append_run_event(
                        run_id,
                        at=int(time.time() * 1000),
                        kind="lifecycle",
                        payload={"inference_home": actual_home.data()},
                    )
                elif actual_home != retained_home:
                    raise InvalidRunRequest(
                        "structured inference resume cannot change its native home"
                    )
            env["NOTT_NMEMORY_CURATOR"] = "0"
            env["CLAUDE_CODE_MAX_RETRIES"] = "2"
        if child.appends_law(declared, cwd):
            env[child.LAW_SESSION_ENV] = "off"
        restricted: claude_restricted.RestrictedLaunch | None = None
        if writer is not None:
            if scratch is None:
                raise claude_restricted.RestrictedLaunchRefused(
                    "a caged run needs its own scratch directory"
                )
            profile = self._registry.session_profile(run_id)
            if profile is None or profile.config_dir is None:
                raise claude_restricted.RestrictedLaunchRefused(
                    "caged run has no retained native config"
                )
            # The caged writer: its own argv, a closed env holding only the host's
            # credential, and settings pinned now and verified again at exec.
            restricted = claude_restricted.compose_restricted_launch(
                binary=Path(installation.binary),
                checkout=Path(cwd),
                run_dir=run_dir.root,
                host_auth={
                    name: env[name]
                    for name in child.CREDENTIAL_ENV_NAMES
                    if name in env
                },
                model=picked.model,
                effort=picked.effort,
                system_prompt_file=system_prompt_file,
                session_id=thread_id,
                resume=resume,
                config_dir=Path(profile.config_dir),
                tmpdir=scratch,
                writable=not read_only,
                read_paths=tuple(Path(path) for path in read_paths),
            )
            argv = list(restricted.argv)
            cwd = str(restricted.cwd)
            if restricted.preface is not None:
                prompt = f"{restricted.preface}\n\n{prompt}"
            env = {
                **restricted.env,
                child.RUN_ID_ENV: run_id,
                child.SESSION_ID_ENV: thread_id,
            }
            if memory_push:
                env[child.MEMORY_POLICY_ENV] = "push"
        with self._launching(run_id):
            if restricted is not None:
                claude_restricted.verify_pinned_settings(
                    restricted.settings_path, restricted.settings_sha256
                )
            # Only a writer names its slice; a read-only child keeps the old call.
            spawned = (
                child.spawn_child(
                    argv, cwd, env, run_dir.root, needles, stdin_pipe=True
                )
                if writer is None
                else child.spawn_child(
                    argv, cwd, env, run_dir.root, needles, stdin_pipe=True, scope=writer
                )
            )
            self._children[run_id] = spawned
            self._update_phase(
                run_id, RunPhase.RUNNING, process_identity=spawned.identity
            )
        # `process_group` is a distinct, legacy column the registry's own
        # close_owned/close_orphaned fence requires to stay NULL
        # (`_close_with_identity`'s WHERE clause pins `process_group IS
        # NULL`); the pid triple in `process_identity` is what proves and
        # closes a Claude run. The OS-level process GROUP a killer signals is
        # `spawned.identity.pid` itself (`start_new_session=True` makes the
        # leader's pid the pgid) — a kernel fact, never persisted here.
        # L3's SpawnedChild.wait() owns joining both L4 pumps before returning
        # the exit code (or raising pump.PumpLimitExceeded, an NSpawnError
        # already caught by _drive_run) — L7 touches only the outer boundary.
        # THE DEADLINE BELONGS TO THE RUN, and it is OPT-IN. `timeout_ms` promised it in the schema
        # forever and did not deliver: `_admit` called `_wait_past_spawning`, which only
        # blocked the CALL until the phase left SPAWNING, and the run itself was a
        # `wait()` without any ceiling. A silent child stayed silent forever and nothing
        # killed it — and an operator planned recovery of a silent run on
        # top of a deadline that did not exist.
        #
        # Zero remains NO deadline: all callers today pass zero, and a
        # default ceiling would be a killer nobody authorized.
        #
        # The prompt goes in on stdin, never argv (see compose_child_argv). A
        # write that fails leaves a child holding a truncated prompt: kill it
        # rather than let it answer the wrong question.
        try:
            try:
                spawned.write_stdin(prompt.encode(), timeout=PROMPT_WRITE_SECONDS)
            except child.ChildPipeError:
                with contextlib.suppress(child.ChildIdentityError, OSError):
                    spawned.stop_then_kill(spawned.identity)
                raise
            spawned.close_stdin()
            if deadline_ms <= 0:
                exit_code = spawned.wait()
            else:
                try:
                    exit_code = spawned.wait(timeout=deadline_ms / 1000)
                except subprocess.TimeoutExpired as expiry:
                    # KILL BEFORE FAILING. A terminal row on a child still
                    # alive is an orphan with the row saying it finished — the class
                    # of lie that `close` itself exists to prevent.
                    with contextlib.suppress(child.ChildIdentityError, OSError):
                        spawned.stop_then_kill(spawned.identity)
                    raise RunDeadlineExceeded(
                        f"run exceeded its caller-named deadline of {deadline_ms} ms "
                        "and was signalled"
                    ) from expiry
        finally:
            # An exited (or signalled) child is out of custody. Left here, the
            # supervisor's shutdown re-verified a reaped identity and logged
            # ChildIdentityError — on EVERY driver exit, once each run got its
            # own supervisor. The native paths already pop in their `finally`.
            with self._lock:
                if self._children.get(run_id) is spawned:
                    self._children.pop(run_id)
        epoch = time.time()
        try:
            records_seen = run_dir.stdout.read_bytes().count(b"\n")
        except FileNotFoundError:
            pass
        else:
            if records_seen:
                with self._lock:
                    self._registry.note_record_observed(
                        run_id, int(epoch * 1000), count=records_seen
                    )
        now_s = int(epoch)
        classification = terminal.classify_terminal_run(
            run_dir.stdout,
            run_dir.stderr,
            exit_code,
            provider=provider.name,
            account_alias=picked.executed_account,
            now=now_s,
        )
        if exit_code < 0:
            classification = replace(
                classification,
                warning=f"child terminated by signal {-exit_code}",
                telemetry=replace(classification.telemetry, failure="child_signal")
                if classification.telemetry is not None
                else None,
                failure_reason=terminal.failure_reason(
                    classification.api_error_status, "child_signal"
                ),
            )
        spent = self._spent_provider_wall(provider, picked, classification, now_s)
        if spent is not None:
            classification = replace(
                classification,
                provider_wall=spent,
                warning=f"{classification.warning}; provider {provider.name!r} "
                f"walled until {spent.cooling_until}",
                telemetry=replace(classification.telemetry, failure=spent.source.value)
                if classification.telemetry is not None
                else None,
            )
        elif (
            classification.api_error_status == 429
            and classification.account_wall is None
            and classification.provider_wall is None
            and picked.executed_account is not None
        ):
            classification = replace(
                classification,
                account_wall=terminal.AccountWallRecord(
                    account_alias=picked.executed_account,
                    cooling_until=now_s + self.RATE_LIMIT_WALL_S,
                    source=terminal.WallSource.DEFAULT_1H,
                    limit_type="rate_limited",
                    observed_at=now_s,
                ),
            )
        with self._lock:
            if classification.provider_wall is not None:
                self._registry.record_provider_wall(
                    classification.provider_wall.provider,
                    classification.provider_wall.cooling_until,
                    classification.provider_wall.source.value,
                    now_s,
                )
            if classification.account_wall is not None:
                self._registry.record_account_wall(
                    classification.account_wall.account_alias,
                    classification.account_wall.cooling_until,
                    classification.account_wall.source.value,
                    classification.account_wall.limit_type,
                    now_s,
                )
            # ONLY under a real alias. The env-route providers execute with no
            # account at all, and a row filed under a fabricated alias would
            # answer a later `quota()` with a number belonging to nobody —
            # strictly worse than the honest NEVER_OBSERVED it replaces.
            if picked.executed_account is not None:
                self._registry.record_account_usage(
                    tuple(
                        AccountUsageRow(
                            account_alias=picked.executed_account,
                            usage_window=window.window,
                            utilization=window.utilization,
                            resets_at=window.resets_at,
                            observed_at=now_s,
                        )
                        for window in classification.usage
                    )
                )
        # The TERMINAL phase is published by the driver, not here: the driver
        # is the only one that knows whether a rejected credential means
        # another account gets to try. Publishing here made a status read in
        # the failover window report FAILED for a run that was about to run
        # again.
        return classification

    def _fail_run(
        self,
        run_id: str,
        error: BaseException,
        *,
        needles: SecretNeedles | None = None,
    ) -> None:
        # Exception detail can contain credentials, paths or vendor output.
        # Record identity only; status and result share this persisted value.
        reason = type(error).__name__
        entry = getattr(error, "entry", None)
        if isinstance(entry, ErrorEntry):
            reason += f" (nERR-{entry.code_str()})"
        if (
            isinstance(
                error,
                (app_server.AppServerError, agy_restricted.RestrictedLaunchRefused),
            )
            and error.detail
        ):
            # These details are nSPAWN's own literals, never vendor bytes or a
            # path; the run's exact secrets are still taken out.
            detail = (needles if needles is not None else SecretNeedles.none()).redact(
                error.detail
            )
            reason = _bounded_text(f"{reason}: {detail}", MAX_DURABLE_OBSERVATION_BYTES)
        if (
            isinstance(error, app_server.PluginSkillsRefused)
            and error.reason is not None
        ):
            # Only the closed host predicate, never arbitrary exception detail.
            reason += f": {error.reason.value}"
        if isinstance(error, (pump.PumpLimitExceeded, RunCloseRejected)):
            # THE ONE EXCEPTION WHOSE TEXT IS OURS. Every PumpLimitExceeded
            # message is assembled here from this package's own constants and
            # measured counts — a stream name, a line ordinal, byte totals —
            # and never from a child byte, a path or a credential. Five distinct
            # ceilings raise this class; recorded as the bare class name, a run
            # that died on a 1 MiB line read identically to one that died on
            # the shared 64 MiB budget, and the only stderr the operator got
            # named the broken pipe DOWNSTREAM of the refusal. A writer slice
            # that was not seen empty is the other: its unit name and the gate's
            # own refusal, no child byte.
            reason += f": {error.message}"
        # A route that could not prepare this turn is the route's refusal: an
        # unpinned witness goes on to its next preference, named here.
        route_refused = isinstance(error, _PREPARATION_REFUSALS)
        refusal = route_refused or (
            self._retry_reason(run_id, RunPhase.FAILED, reason) is not None
        )
        note: str | None = None
        try:
            if refusal:
                note = self._fail_over(run_id, reason, spends=not route_refused)
            else:
                # Every fresh run holds a walk now: one that ends here keeps none.
                with self._lock:
                    self._failovers.pop(run_id, None)
            published = self._update_phase(
                run_id,
                RunPhase.FAILED,
                drop_claim=True,
                blocked_reason=reason,
                warning=note,
            )
        except (*STORE_ERRORS, OSError) as fault:
            # Only a witness failover is queued: the driver that admitted it
            # retries the queue (`publish_pending`). A run with none (a pinned
            # route, a route ping on the MCP server) may see no retry at all.
            if not refusal or (note is None and run_id not in self._failovers):
                raise
            # As `_publish_terminal` does: the real terminal waits for its retry
            # with the failover `_fail_over` kept, or with the note that names
            # the successor it already admitted.
            with contextlib.suppress(Exception):
                print(
                    f"nspawn.failover: run {run_id} waits for its terminal "
                    f"retry: {type(fault).__name__}: {fault}",
                    file=sys.stderr,
                    flush=True,
                )
            self._unpublished[run_id] = terminal.TerminalClassification(
                phase=RunPhase.FAILED,
                final_text=None,
                warning=note,
                marker=None,
                provider_wall=None,
                account_wall=None,
                failure_reason=reason,
            )
            return
        if published is False:
            # A close already won and announced CLOSED, or the run's terminal
            # already landed or waits for retry: one run, one notice, and the
            # late failure is a diagnostic line, never a second terminal.
            return
        # THE SECOND TERMINAL SEAM, and the one a review caught the contract
        # lying about. A spawn that never started — no binary, a pump over
        # budget, a classification that contradicted itself — reaches FAILED
        # HERE and never through `_publish_terminal`, so a caller promised a
        # notice on "a clean exit, a failure, or the signal death" would have
        # waited forever on the failure that never produced a child at all.
        self._announce(run_id)

    def _announce(self, run_id: str | None = None) -> None:
        """Push the outbox rows this process just armed to their live callers.

        Called by whoever commits a terminal transition: the terminal seams and
        a close name their run; an orphan pass names none and pushes every free
        ready row. The body is `notify.notice_body`: the run id, its phase, and
        the answer or failure framed as the executor's data. The push cannot fail any seam: a store that
        refuses, or a socket or run file that fails, is logged, and the row stays
        for the next pusher or the drain. `_publish_terminal` calls it only after
        its commit, outside its refusal branch.
        """
        if not self.announces_terminal:
            return
        try:
            notify.push_ready(
                self._registry,
                None if run_id is None else [run_id],
                lock=self._lock,
                run_root=self._run_root_path,
            )
        except (NSpawnError, OSError, ValueError, *STORE_ERRORS) as error:
            _log_tick_failure(error)

    def _update_phase(
        self,
        run_id: str,
        phase: RunPhase,
        *,
        drop_claim: bool = False,
        annotate: bool = False,
        **fields: object,
    ) -> bool:
        """Write the phase; False when the row is gone, already ended, or its
        terminal write is queued for retry (`_unpublished`).

        A run has ONE terminal write. What arrives after it, or while it waits
        for retry, never alters it: it is a best-effort line appended to the
        run's own `late_writes` file (never stderr, whose pipe can stall while a
        caller holds this reentrant lock), and its own failure is swallowed.

        `annotate` writes only a completed or failed row's own phase again, to
        add what was learned after it ended (a warning); it never moves a row."""
        now_ms = int(time.time() * 1000)
        late: list[str] = []
        try:
            with self._lock:
                current = (
                    None
                    if run_id in self._unpublished
                    else self._registry.get_run(run_id)
                )
                if current is None or (
                    (current.phase is not phase or phase not in _ANNOTATED)
                    if annotate
                    else current.phase in _ENDED and phase is not RunPhase.CLOSED
                ):
                    # The row is gone (never admitted, or pruned), its terminal
                    # is queued, a close already won, or the run already ended:
                    # the first terminal write wins, so a late failure (a cleanup
                    # that raised after COMPLETED) never replaces it.
                    state = (
                        "is queued or gone"
                        if current is None
                        else f"is {current.phase.value}"
                    )
                    late.append(_late_write(run_id, state, phase, fields))
                    self._lock.notify_all()
                    return False
                # A None NEVER erases a value already on the row. Measured on the
                # version warning this module no longer writes: it landed at
                # spawn, then terminal classification passed `warning=None` on
                # every success and wiped it — the operator saw the marker while
                # a run was live and lost it on exactly the rows they read
                # afterwards. The warning that taught this is gone; the rule it
                # bought is not, and the picker, classification and telemetry
                # warnings still ride here.
                fields = {
                    name: value
                    for name, value in fields.items()
                    if value is not None or getattr(current, name, None) is None
                }
                incoming = fields.get("warning")
                if isinstance(incoming, str):
                    composed = _composed_warning(
                        getattr(current, "warning", None), incoming
                    )
                    if len(composed.encode("utf-8")) > MAX_DURABLE_OBSERVATION_BYTES:
                        # Over the cap the newest segments go first: a cut keeps them.
                        composed = _composed_warning(incoming, current.warning or "")
                    fields["warning"] = composed
                # Bounded here, the one phase write: a value over its column cap
                # raises in `RunRow`, and the worker dies with the run still live.
                for name in _OBSERVATION_FIELDS:
                    value = fields.get(name)
                    if isinstance(value, str):
                        fields[name] = _bounded_text(
                            value, MAX_DURABLE_OBSERVATION_BYTES
                        )
                updated = replace(current, phase=phase, updated_at=now_ms, **fields)  # type: ignore[arg-type]
                try:
                    self._registry.update_run(
                        updated, drop_claim=drop_claim, annotate=annotate
                    )
                except ClosedRun as error:
                    # The same race, caught one layer down: close() committed
                    # between the read above and this write. A close wins.
                    state = f"ended at write time ({error})"
                    late.append(_late_write(run_id, state, phase, fields))
                    self._lock.notify_all()
                    return False
                self._lock.notify_all()
                return True
        finally:
            for line in late:
                self._note_late(run_id, line)

    def _note_late(self, run_id: str, line: str) -> None:
        """Best effort: a diagnostic that cannot be written changes nothing.
        The run directory is never followed through a link, and only a regular
        file is written: a FIFO opens non-blocking and fails at once."""
        with contextlib.suppress(Exception):
            directory = self._open_run_directory(run_id)
            try:
                fd = _open_regular(
                    directory, LATE_WRITES_FILE, os.O_WRONLY | os.O_APPEND | os.O_CREAT
                )
            finally:
                os.close(directory)
            with open(fd, "a", encoding="utf-8") as sink:
                sink.write(f"{line}\n")

    # -- reads -----------------------------------------------------------------

    def _foreign(self, row: RunRow) -> bool:
        """A node sees only its own runs: another owner's is no run at all."""
        return self.owner is not None and row.owner != self.owner

    def _read(self, run_id: str) -> RunRow:
        with self._lock:
            row = self._registry.get_run(run_id)
        if row is None or self._foreign(row):
            raise UnknownRun(f"no run named {run_id!r}")
        return row

    def status(self, run_id: str, timeout_ms: int = 0) -> RunStatus:
        with self._lock:
            row = self._registry.get_run(run_id)
            if row is None or self._foreign(row):
                raise UnknownRun(f"no run named {run_id!r}")
            if timeout_ms > 0:
                deadline = time.monotonic() + timeout_ms / 1000
                while not row.phase.is_terminal():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    # A run another process drives notifies nobody here, so the
                    # row is re-read every second as well.
                    # ponytail: 1 s poll; LISTEN/NOTIFY if latency matters
                    self._lock.wait(min(remaining, 1.0))
                    row = self._registry.get_run(run_id)
                    if row is None:
                        raise UnknownRun(f"run {run_id!r} vanished during long-poll")
            effort = self._registry.effort_profile(run_id)
            cost_usd = None
            for event in reversed(self._registry.read_run_events(run_id)):
                if event.kind != "usage":
                    continue
                if not isinstance(event.payload, dict):
                    raise RegistryError("recorded usage is not an object")
                cost_usd = event.payload.get("cost_usd")
                if cost_usd is not None:
                    if not isinstance(cost_usd, str):
                        raise RegistryError("recorded cost_usd is not text or null")
                    try:
                        amount = Decimal(cost_usd)
                    except InvalidOperation as error:
                        raise RegistryError(
                            "recorded cost_usd is not a decimal"
                        ) from error
                    if not amount.is_finite() or amount < 0:
                        raise RegistryError(
                            "recorded cost_usd must be finite and nonnegative"
                        )
                break
            return replace(
                _to_status(row, self._run_root_path, effort),
                inference_profile=self._registry.inference_profile(run_id),
                session=self._registry.session_reference(row),
                transport=self._registry.session_transport(run_id),
                exit_code=self._registry.session_exit_code(run_id),
                cost_usd=cost_usd,
            )

    #: The most runs one answer follows: a walk is bounded by the roster, and a
    #: chain longer than this is not one nSPAWN made.
    MAX_CONTINUATIONS: Final = 32

    def result(self, run_id: str, timeout_ms: int = 0) -> RunResult:
        """The answer of the run, or of the run its work continued as.

        A failed run that nSPAWN moved names its successor
        (`continued as run ...`). The answer follows each successor to the run
        that served, waits up to `timeout_ms` in total for it to end, and
        carries the substitution alert.
        """
        deadline = time.monotonic() + timeout_ms / 1000
        chain: list[RunRow] = []
        current = run_id
        while True:
            if (left := int((deadline - time.monotonic()) * 1000)) > 0:
                self.status(current, timeout_ms=left)
            row = self._read(current)
            chain.append(row)
            successor = notify.continued_run(row)
            if successor is None or len(chain) > self.MAX_CONTINUATIONS:
                break
            current = successor
        recorded = self._registry.inference_profile(current)
        answer = run_result(
            row,
            self._run_root_path / current,
            inference=recorded is not None,
            profile=None
            if recorded is None
            else StructuredInference.from_request(recorded),
        )
        return replace(answer, alert=notify.substitution_alert(chain))

    def transcript(
        self,
        run_id: str,
        since_seq: int = 0,
        max_bytes: int | None = None,
        frame_types: Sequence[str] | None = None,
    ) -> TranscriptPage:
        if since_seq < 0:
            raise InvalidRunRequest("since_seq must not be negative")
        if frame_types is not None:
            if isinstance(frame_types, (str, bytes)) or not frame_types:
                raise InvalidRunRequest("frame_types must be a non-empty sequence")
            if len(frame_types) > self.MAX_TRANSCRIPT_FRAME_TYPES:
                raise InvalidRunRequest(
                    "frame_types must carry at most "
                    f"{self.MAX_TRANSCRIPT_FRAME_TYPES} items"
                )
            for requested_type in frame_types:
                _require_text("frame_types item", requested_type)
        allowed_types = frozenset(frame_types) if frame_types is not None else None
        cap = (
            max_bytes
            if max_bytes is not None and max_bytes > 0
            else self.DEFAULT_TRANSCRIPT_MAX_BYTES
        )
        self._read(run_id)
        path = self._run_root_path / run_id / "stdout"
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            return TranscriptPage(
                run_id=run_id,
                since_seq=since_seq,
                next_seq=since_seq,
                lines=(),
                truncated=False,
            )
        # Only fully `\n`-terminated lines are observations: the pump may be
        # mid-append, and the trailing split()-artifact after the last `\n`
        # (empty when the file ends cleanly, partial otherwise) is dropped
        # uniformly either way.
        complete_lines = data.split(b"\n")[:-1]
        total = len(complete_lines)
        # A command's retained output may have emptied: its line cursor goes on
        # from the records dropped, and a cursor behind them reads the gap first.
        dropped = _dropped_records(complete_lines[0]) if complete_lines else 0
        start = min(max(since_seq - dropped, 0), total)
        selected: list[bytes] = []
        consumed = 0
        index = start
        while index < total:
            line = complete_lines[index]
            try:
                frame = json.loads(line)
            except json.JSONDecodeError, UnicodeDecodeError:
                frame = None
            frame_type = frame.get("type") if isinstance(frame, dict) else None
            if allowed_types is None:
                include = not (
                    frame_type == "system" and frame.get("subtype") == "thinking_tokens"
                )
            else:
                include = isinstance(frame_type, str) and frame_type in allowed_types
            if not include:
                index += 1
                continue
            line_bytes = len(line) + 1
            if selected and consumed + line_bytes > cap:
                break
            selected.append(line)
            consumed += line_bytes
            index += 1
        return TranscriptPage(
            run_id=run_id,
            since_seq=since_seq,
            next_seq=dropped + index,
            lines=tuple(line.decode("utf-8", errors="replace") for line in selected),
            truncated=index < total,
        )

    # -- quota -----------------------------------------------------------------

    def fleet(self) -> FleetPage:
        """Every run the registry holds, under one clock.

        THE READ AN ORCHESTRATOR MAKES EVERY TICK, and until now it had to go
        around the product to make it: the status verb required a `run_id`, so
        an operator watching eight lanes read the `runs` table by hand. A
        product you have to bypass to see is not observable.

        One `observed_at` for the whole page, taken once here: age is derived
        from it per row, so the listing cannot report two runs against clocks
        that drifted apart between them. Newest first — see `list_runs_page`.
        """
        observed_at = int(time.time())
        with self._lock:
            rows = self._registry.list_runs(owner=self.owner)
            notices = self._registry.undelivered_notices(owner=self.owner)
        return FleetPage(
            observed_at=observed_at,
            runs=tuple(
                FleetRun(
                    run_id=row.run_id,
                    lane=row.lane,
                    phase=row.phase,
                    executed_provider=row.executed_provider,
                    executed_account=row.executed_account,
                    model=row.model,
                    tokens=_live_tokens(row, self._run_root_path),
                    turn_count=_live_turns(row, self._run_root_path),
                    created_at=row.created_at,
                    updated_at=row.updated_at,
                    group=row.group_name,
                    backend=row.backend,
                )
                for row in rows
            ),
            notices=tuple(
                FleetNotice(run_id, RunPhase(phase), project)
                for run_id, phase, project in notices
            ),
        )

    def providers(self, provider: str | None = None) -> ProviderReport:
        """Read roster routes and model metadata through `provider_report`.

        A method as well as a free function because the door holds a
        `Supervisor` when one is injected and this verb must answer the same
        way through either hand; nothing here reads `self`, and reading the
        registry is exactly what this verb refuses to do.
        """
        return provider_report(provider)

    def _native_quota(
        self, provider: Provider, alias: str, roster: Roster
    ) -> SourceReading:
        inspector: app_server.AppServerTurn | None = None
        operation: str | None = None
        try:
            launch = native.prepare_native(
                provider,
                alias,
                roster=roster,
                cwd=native.vendor_home(provider.accounts[alias]).expanduser(),
                read_only=True,
            )
            identity = launch.account_identity()
            with tempfile.TemporaryDirectory(
                prefix=".quota-", dir=self._run_root_path
            ) as directory:
                operation = Path(directory).name
                with contextlib.ExitStack() as resources:
                    with self._lock:
                        if self._stop_tick.is_set():
                            raise app_server.AppServerError(
                                "native quota supervisor is shutting down"
                            )
                        inspector = resources.enter_context(
                            app_server.AppServerTurn.start(
                                launch.argv,
                                cwd=native.vendor_home(launch.account),
                                env=launch.env,
                                run_dir=Path(directory),
                                needles=launch.needles,
                            )
                        )
                        self._children[operation] = inspector.child
                    reading = inspector.read_account_quota(
                        native.vendor_home(launch.account),
                        identity,
                        timeout=PULL_TIMEOUT_S,
                    )
                    if launch.account_identity() != identity:
                        raise app_server.AppServerError(
                            "native quota account identity changed"
                        )
                return reading
        except (NSpawnError, OSError) as error:
            detail = (
                error.detail
                if isinstance(error, app_server.AppServerError)
                else type(error).__name__
            )
            return SourceReading(
                (),
                int(time.time()),
                "codex-native",
                "native quota unavailable: " + (detail or type(error).__name__),
            )
        finally:
            if operation is not None and inspector is not None:
                with self._lock:
                    if self._children.get(operation) is inspector.child:
                        self._children.pop(operation, None)

    def threads(
        self,
        limit: int,
        ref: tuple[str, str] | None = None,
        claude_home: Path | None = None,
        exclude: frozenset[str] = frozenset(),
    ) -> dict[str, Any]:
        """Vendor sessions nSPAWN did not start (`nspawn thread list|show`).

        Without `ref`, the newest `limit` sessions of the host's Claude home and
        of each Codex home, with the homes that could not be read. With a Claude
        `ref` and `claude_home`, that session's messages from that home; with a
        `ref` alone, that one session. A session in `exclude` (the caller's own) is
        left out like one nSPAWN started, before the limit. Each vendor child is in `_children`
        while it reads: a home over `THREAD_HOME_S`, or over the verb's
        `THREAD_VERB_S`, is killed and listed as unread.
        """
        deadline = time.monotonic() + THREAD_VERB_S
        roster = _read_roster()
        homes = [claude_config_dir()]
        for provider in roster.providers:
            if provider.name == "claude" and provider.kind == "claude":
                for account in provider.accounts.values():
                    if account.home is not None:
                        home = claude_config_dir(account)
                        if home.resolve() not in {item.resolve() for item in homes}:
                            homes.append(home)
        rows: list[dict[str, Any]] = []
        missed: list[dict[str, Any]] = []

        def known(ids: list[str]) -> set[str]:
            with self._lock:
                return self._registry.known_threads(ids) | (set(ids) & exclude)

        def read(
            provider: str,
            where: Path,
            call: Callable[[str], Any],
            seconds: float = THREAD_HOME_S,
            slow: str = THREAD_TIMEOUT_REASON,
        ) -> Any:
            """`call(operation)` within the time left, or None with the home unlisted."""
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                missed.append(
                    threads.unlisted(provider, str(where), threads.NO_TIME_REASON)
                )
                return None
            try:
                return self._thread_read(call, min(seconds, remaining), slow)
            except route.RouteRefused as error:
                reason = error.message
            except NSpawnError as error:
                reason = getattr(error, "detail", None) or str(error)
            except (OSError, ValueError) as error:
                reason = f"the home could not be read: {type(error).__name__}"
            missed.append(threads.unlisted(provider, str(where), reason))
            return None

        if ref is not None and claude_home is not None:
            answer = read(
                "claude",
                claude_home,
                lambda operation: self._claude_sessions(
                    operation,
                    claude_home,
                    ["messages", ref[1], str(THREAD_MESSAGES), str(THREAD_BYTES)],
                ),
                THREAD_VERB_S,
                THREAD_SLOW_REASON,
            )
            if answer is None:
                return {"messages": None, "reason": missed[-1]["reason"]}
            return {"messages": answer[:-1], "omitted": answer[-1]["omitted"]}
        for home in homes:
            if not (ref is None or ref[0] == "claude") or not home.is_dir():
                continue
            live = threads.claude_live(home)
            verb = ["list", str(THREAD_SCAN_LIMIT), str(THREAD_BUDGET_BYTES)]

            def sessions(
                operation: str, where: Path = home, arguments: list[str] = verb
            ) -> list[dict[str, Any]]:
                return self._claude_sessions(
                    operation, where, arguments if ref is None else ["info", ref[1]]
                )

            listed = read(
                "claude",
                home,
                sessions,
            )
            entries = (
                []
                if listed is None
                else listed[0]["threads"]
                if ref is None
                else [listed[0]["thread"]]
                if listed[0]["thread"]
                else []
            )
            mine = (
                known([entry["sessionId"] for entry in entries])
                if ref is None
                else set()
            )
            rows.extend(
                threads.claude_row(entry, home, live)
                for entry in entries
                if entry["sessionId"] not in mine
            )
        if ref is None or ref[0] == "codex":
            for provider, alias, where in _codex_homes(roster):
                found = read(
                    "codex",
                    where,
                    functools.partial(
                        self._codex_threads,
                        provider=provider,
                        alias=alias,
                        home=where,
                        roster=roster,
                        limit=limit,
                        ids=[] if ref is None else [ref[1]],
                        known=known,
                    ),
                )
                rows.extend(
                    threads.codex_row(
                        thread,
                        where,
                        None if alias is None else route.account_route(alias, where),
                        threads.DEFAULT_CODEX_REASON if alias is None else None,
                    )
                    for thread in found or []
                )
                if ref is not None and rows:
                    break  # one home holds the thread
        if ref is not None:
            if not rows and missed:
                raise ThreadUnread(missed[-1]["reason"])
            if rows and known([rows[0]["sessionId"]]):
                return {"thread": None, "started": True}
            return {"thread": rows[0] if rows else None}
        if any(provider.kind == "agy" for provider in roster.providers):
            missed.append(threads.unlisted("agy", None, threads.AGY_REASON))
        rows.sort(key=lambda row: row.get("updatedAt") or 0, reverse=True)
        return {"threads": rows[:limit], "unlisted": missed}

    def _thread_read(
        self, call: Callable[[str], Any], seconds: float, slow: str
    ) -> Any:
        """`call(operation)` off this thread. Past `seconds`, the child that
        `call` holds under `operation` is killed and the read is refused."""
        operation = f".threads-{os.urandom(8).hex()}"
        outcome: list[Any] = []

        def work() -> None:
            try:
                outcome.append(call(operation))
            except Exception as error:  # noqa: BLE001 - raised on the caller's thread
                outcome.append(error)

        worker = threading.Thread(target=work, name="nspawn-thread-read", daemon=True)
        worker.start()
        worker.join(seconds)
        if worker.is_alive():
            # Still in custody while it dies: a kill that fails leaves it for shutdown().
            with self._lock:
                spawned = self._children.get(operation)
            if spawned is not None:
                try:
                    spawned.stop_then_kill(spawned.identity)
                except child.ChildIdentityError:
                    if spawned.poll() is None:
                        raise
            worker.join(THREAD_REAP_S)
            raise ThreadUnread(slow)
        if isinstance(outcome[0], Exception):
            raise outcome[0]
        return outcome[0]

    def _hold(self, operation: str, spawned: child.SpawnedChild) -> None:
        """Under `self._lock`: keep `spawned` in custody, unless shutting down."""
        if self._stop_tick.is_set():
            spawned.stop_then_kill(spawned.identity)
            raise ThreadUnread("not read: the supervisor is shutting down")
        self._children[operation] = spawned

    def _release(self, operation: str, spawned: child.SpawnedChild) -> None:
        """Out of custody once it exited; a child a kill missed stays for shutdown()."""
        with self._lock:
            if self._children.get(operation) is spawned and spawned.poll() is not None:
                self._children.pop(operation, None)

    def _codex_threads(
        self,
        operation: str,
        provider: Provider,
        alias: str | None,
        home: Path,
        roster: Roster,
        *,
        limit: int,
        ids: list[str],
        known: Callable[[list[str]], set[str]],
    ) -> list[dict[str, Any]]:
        """One Codex home's threads, read by its own app-server.

        A roster account's launch is `prepare_native`'s. The default `~/.codex`,
        which no account uses, gets only PATH, HOME and CODEX_HOME: no
        credential reaches it, and its app-server writes its own state files."""
        if alias is not None:
            launch = native.prepare_native(
                provider, alias, roster=roster, cwd=home, read_only=True
            )
            argv, environment, needles = launch.argv, launch.env, launch.needles
        else:
            if provider.bin is None:
                raise ThreadUnread("the codex binary is not declared")
            argv = (str(provider.bin.expanduser()), *child.app_server_arguments())
            environment = {
                "PATH": os.environ.get("PATH", ""),
                "HOME": str(Path.home()),
                "CODEX_HOME": str(home),
            }
            needles = SecretNeedles()
        inspector: app_server.AppServerTurn | None = None
        try:
            with (
                tempfile.TemporaryDirectory(
                    prefix=".threads-", dir=self._run_root_path
                ) as directory,
                contextlib.ExitStack() as resources,
            ):
                with self._lock:
                    inspector = resources.enter_context(
                        app_server.AppServerTurn.start(
                            argv,
                            cwd=home,
                            env=environment,
                            run_dir=Path(directory),
                            needles=needles,
                        )
                    )
                    self._hold(operation, inspector.child)
                return inspector.read_threads(
                    home, limit=limit, ids=ids, known=known, timeout=THREAD_VERB_S
                )
        finally:
            if inspector is not None:
                self._release(operation, inspector.child)

    def _claude_sessions(
        self, operation: str, home: Path, arguments: list[str]
    ) -> list[dict[str, Any]]:
        """`claude_sessions.mjs` on `home`: the Agent SDK's own session reads, in
        a child whose environment is PATH, HOME and CLAUDE_CONFIG_DIR alone."""
        runtime = route.runtime_read({})
        if runtime is None:
            raise ThreadUnread("the Claude Agent SDK is not installed")
        reader = Path(__file__).with_name("claude_sessions.mjs")
        argv = [
            runtime["runtime"]["node"]["path"],
            str(reader),
            runtime["runtime"]["sdk"]["path"],
            *arguments,
        ]
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(Path.home()),
            "CLAUDE_CONFIG_DIR": str(home),
        }
        lines: list[bytes] = []

        def keep(line: bytes) -> bytes:
            lines.append(line)
            return b""  # transcript text never reaches a run file

        spawned: child.SpawnedChild | None = None
        try:
            with tempfile.TemporaryDirectory(
                prefix=".threads-", dir=self._run_root_path
            ) as directory:
                with self._lock:
                    spawned = child.spawn_child(
                        argv,
                        home,
                        environment,
                        Path(directory),
                        SecretNeedles(),
                        stdout_transform=keep,
                        stderr_transform=lambda line: b"",
                    )
                    self._hold(operation, spawned)
                try:
                    code = spawned.wait(timeout=THREAD_VERB_S)
                except subprocess.TimeoutExpired:
                    spawned.stop_then_kill(spawned.identity)
                    spawned.wait(timeout=THREAD_REAP_S)
                    raise ThreadUnread(THREAD_SLOW_REASON) from None
        finally:
            if spawned is not None:
                self._release(operation, spawned)
        if code != 0:
            raise ThreadUnread("the Claude Agent SDK could not read the sessions")
        return [json.loads(line) for line in lines]

    def usage_show(self, time_zone: str) -> dict[str, Any]:
        with self._lock:
            return self._registry.usage_show(time_zone)

    def quota(
        self,
        provider: str | None = None,
        refresh: int = 0,
        *,
        source: QuotaSource | None = None,
    ) -> QuotaReport:
        """What is known about every declared provider's quota, or one of them.

        THE SOURCES ARE READ OUTSIDE `self._lock`. That lock serializes every
        registry call on this supervisor's one connection, `status()`'s
        long-poll included; holding it across vendor reads with hard timeouts
        would freeze `execute` and `status` for as long as the slowest vendor
        takes. Only the three registry reads below are inside it.

        `refresh=0` answers from what is durable — the rows a run's own wire
        left behind. A positive value asks the sources instead; its magnitude
        is not read, because the only question is pull or do not pull. A
        negative value is refused rather than quietly treated as zero.

        A provider the roster does not declare is refused by name, in
        `_declared_providers`.
        """
        if refresh < 0:
            raise InvalidRunRequest(f"refresh must not be negative, got {refresh}")
        roster = _read_roster()
        declared = _declared_providers(provider, roster=roster)
        by_name = {entry.name: entry for entry in declared}

        def read_source(name: str, alias: str | None) -> SourceReading:
            entry = by_name[name]
            account = entry.accounts.get(alias) if alias is not None else None
            if (
                entry.kind == "codex"
                and account is not None
                and account.auth_env is None
            ):
                assert alias is not None
                return self._native_quota(entry, alias, roster)
            if source is not None:
                return source(name, alias)
            return SourceReading(
                (), int(time.time()), "none", "no quota source configured"
            )

        pulls_until: list[float] = []

        def reconciled(read: QuotaSource) -> QuotaSource:
            """`read`, whose fresh answer for an account becomes the stored usage
            the picker's derived wall reads — whatever source answered. Once the
            refresh has pulled for `QUOTA_PULL_BUDGET_S`, no other pull starts."""

            def pull(name: str, alias: str | None) -> SourceReading:
                if not pulls_until:
                    pulls_until.append(time.monotonic() + QUOTA_PULL_BUDGET_S)
                elif time.monotonic() >= pulls_until[0]:
                    return SourceReading(
                        (),
                        int(time.time()),
                        "refresh-budget",
                        "not pulled: this refresh spent its time on other accounts",
                    )
                reading = read(name, alias)
                if alias is not None and reading.absence is None:
                    with self._lock:
                        self._registry.record_account_usage(
                            tuple(
                                AccountUsageRow(
                                    account_alias=alias,
                                    usage_window=window.window,
                                    utilization=window.utilization,
                                    resets_at=window.resets_at,
                                    observed_at=reading.fetched_at,
                                )
                                for window in reading.windows
                            ),
                        )
                return reading

            return pull

        now = int(time.time())
        with self._lock:
            usage = self._registry.get_account_usage()
            compute = {
                entry.name: self._registry.get_compute_balance(entry.name)
                for entry in declared
                if entry.quota_source is not None
            }
            provider_walls = {
                row.provider: row for row in self._registry.get_provider_wall_rows(now)
            }
            # Only a LIVE wall suppresses the pull, and not a `vendor_reset` one:
            # reading usage past it keeps the walls DERIVED from usage current
            # (the stored row still ends at its own deadline). Credential and
            # default walls still suppress it.
            account_walls = {
                row.account_alias: row
                for row in self._registry.get_account_wall_rows(now)
                if row.source != terminal.WallSource.VENDOR_RESET.value
            }
        stored: dict[str, tuple[AccountUsageRow, ...]] = {}
        for row in usage:
            stored[row.account_alias] = (*stored.get(row.account_alias, ()), row)
        providers = tuple(
            _provider_quota(
                entry,
                stored,
                provider_walls.get(entry.name),
                account_walls,
                reconciled(read_source)
                if entry.kind == "codex"
                and any(account.auth_env is None for account in entry.accounts.values())
                else None
                if source is None
                else reconciled(source),
                refresh,
                now,
            )
            for entry in declared
        )
        providers = tuple(
            _with_compute(entry, quota, compute.get(entry.name), now)
            for entry, quota in zip(declared, providers, strict=True)
        )
        # The report shows the wall the picker enforces, read AFTER the pulls
        # so a usage window a refresh just saw roll over derives no wall.
        with self._lock:
            enforced = {
                row.account_alias: row
                for row in self._registry.get_enforced_account_wall_rows(now)
            }
        return QuotaReport(
            observed_at=now,
            providers=tuple(
                replace(
                    entry,
                    accounts=tuple(
                        _with_wall(account, enforced.get(account.account_alias))
                        for account in entry.accounts
                    ),
                )
                for entry in providers
            ),
        )

    # -- close -----------------------------------------------------------------

    def _claim_is_released(self, worktree: str, run_id: str) -> bool:
        """READ `nspawn.claims`: is this run's claim on `worktree` gone?

        A claim row belonging to a DIFFERENT run answers the same yes — the
        question is whether THIS run still fences that worktree, and a
        successor's claim is that successor's business.
        """
        with self._lock:
            claim = self._registry.read_claim(worktree)
        return claim is None or claim.run_id != run_id

    def _refuse_orphaned_recovery(self, run_id: str) -> None:
        """A run the retired body-accepted mode left mid-export holds work only
        that mode could recover: its close stays refused, and its claim held,
        rather than release a worktree whose export is unaccounted for."""
        if self._registry.implementation_recovery(run_id) is not None:
            raise RunCloseRejected(
                f"run {run_id!r} holds an implementation recovery that only the "
                "retired body-accepted mode could resolve; its claim stays held"
            )

    def close(self, run_id: str) -> CloseReadback:
        """Close through the run's custody owner when it is a live driver.

        A run a live driver drives is closed BY that driver, under its own lock:
        the request goes to `run_control` and this waits for the outcome the
        driver records, re-reading the owner every second and closing locally
        at once when the owner is gone or stale. Any other run — this
        instance's own, a v10 session's, a finished run whose driver exited —
        takes the identity-fenced local close, which works from any instance.
        A pending request is withdrawn by the same compare-and-set before a
        local close, so exactly one side closes and the other reads its record.
        """
        if self.owner is not None:
            self._read(run_id)
        deadline = time.monotonic() + CLOSE_WAIT_S
        awaiting = False
        while True:
            if awaiting:
                with self._lock:
                    outcome = self._registry.close_outcome(run_id)
                if outcome is not None:
                    return _readback_of(run_id, outcome)
            now = int(time.time() * 1000)
            with self._lock:
                custody = self._registry.run_custody(run_id, now, self.STALE_AFTER_MS)
            if custody is None:
                raise UnknownRun(f"no run named {run_id!r}")
            owner, kind, fresh = custody
            if kind == "driver" and fresh and owner != self._registry.instance_id:
                if not awaiting:
                    with self._lock:
                        self._registry.request_close(run_id)
                    awaiting = True
            else:
                with self._lock:
                    state = self._registry.claim_close(
                        run_id, now, self.STALE_AFTER_MS, CLOSE_LEASE_S
                    )
                if state != "held":
                    return self._close_recorded(run_id, record=state == "won")
                awaiting = True
            if time.monotonic() >= deadline:
                raise ClosePending(
                    f"run {run_id!r}: its custody owner has not answered the close "
                    f"within {CLOSE_WAIT_S:.0f} s; the request stands"
                )
            time.sleep(CLOSE_POLL_S)

    def serve_close_request(self, run_id: str) -> bool:
        """The driver's watcher: claim a pending close of `run_id` and serve it."""
        now = int(time.time() * 1000)
        with self._lock:
            state = self._registry.claim_close(
                run_id, now, self.STALE_AFTER_MS, CLOSE_LEASE_S
            )
        if state != "won":
            return False
        with contextlib.suppress(NSpawnError, ControllerRefusal):
            self._close_recorded(run_id, record=True)
        return True

    def _close_recorded(self, run_id: str, *, record: bool) -> CloseReadback:
        """The local close; with `record`, its outcome goes to `run_control`."""
        try:
            readback = self.close_local(run_id)
        except Exception as refusal:
            # Any failure, not only a typed refusal: the requester otherwise
            # waits out its lease reading ClosePending for a close that died.
            if record:
                with self._lock:
                    self._registry.record_close_outcome(
                        run_id,
                        {
                            "refusal": {
                                "type": type(refusal).__name__,
                                "message": str(refusal),
                            }
                        },
                    )
            raise
        if record:
            with self._lock:
                self._registry.record_close_outcome(run_id, _outcome_of(readback))
        return readback

    def close_local(self, run_id: str) -> CloseReadback:
        """Close only after real identity readback proves the tree state."""
        with self._lock:
            command = self._commands.get(run_id)
        if command is not None:
            command.close()
        # Share the launch lock through close so its observation cannot go stale.
        with self._lock:
            writer = self._writers.get(run_id)
            members: tuple[ProcessIdentity, ...] = ()
            if writer is not None:
                # Read the tree BEFORE the aggregate scope kills it: afterwards
                # its members are gone from the group scan, and the readback
                # would name only the leader it never saw die with them.
                spawned = self._children.get(run_id)
                if spawned is not None and process_identity_matches(spawned.identity):
                    members = _discover_group_members(spawned.identity)
            drained = self._drain_writer(run_id, writer)
            self._refuse_orphaned_recovery(run_id)
            readback = self._close_locked(run_id, members, drained=drained)
            closed = readback.phase is RunPhase.CLOSED
            if closed and self._commands.pop(run_id, None) is not None:
                self._lock.notify_all()
        # The close committed the terminal, so this process pushes it — after
        # the lock: a slow caller socket must not stall every registry user.
        if readback.phase is RunPhase.CLOSED:
            self._announce(run_id)
        return readback

    def _drain_writer(self, run_id: str, writer: AggregateScope | None) -> bool:
        """Empty a native write run's slice before its claim may go.

        The live one this process drives, else the one the run recorded, which
        is how a close from any process drains a writer whose driver died. True
        when the run had a slice and it is now empty and stopped.
        """
        if writer is None:
            recorded = self._registry.writer_scope(run_id)
            if recorded is None:
                return False
            unit, cgroup = recorded
            try:
                writer = AggregateScope.recorded(unit)
            except ControllerRefusal as refusal:
                raise RunCloseRejected(_undrained(run_id, unit, refusal)) from refusal
            if str(writer.path) != cgroup:
                raise RunCloseRejected(
                    f"run {run_id!r}: recorded writer cgroup {cgroup} is not the "
                    f"slice {unit}; its claim stays held"
                )
            # systemd removes a stopped slice's group: nothing can run in it.
            if writer.path is None or not writer.path.exists():
                return True
        try:
            writer.close()
        except ControllerRefusal as refusal:
            raise RunCloseRejected(
                _undrained(run_id, writer.unit, refusal)
            ) from refusal
        return True

    def _close_locked(
        self,
        run_id: str,
        members: tuple[ProcessIdentity, ...] = (),
        *,
        drained: bool = False,
    ) -> CloseReadback:
        with self._lock:
            row = self._registry.get_run(run_id)
        if row is None:
            raise UnknownRun(f"no run named {run_id!r}")
        if row.phase is RunPhase.CLOSED:
            # A row can reach CLOSED without `_close_with_identity` — an
            # `update_run` from anywhere does it — and that path deletes no
            # claim. The leftover then fences the worktree until its lease
            # expires: `_claim_in_transaction` refuses on `lease_until > now`
            # whatever the run's phase. So this release is not ceremony on an
            # already-finished run; it is the close finishing, and a poisoned
            # claim needs the same proof here as on any other close.
            if self._poisoned_undrained(run_id, row.worktree, drained=drained):
                raise self._fence_refusal(run_id, row.worktree, drained=drained)
            with self._lock:
                self._registry.release_claim(row.worktree, run_id, drained=drained)
            return CloseReadback(
                run_id,
                RunPhase.CLOSED,
                ProcessTreeReadback((), ()),
                self._claim_is_released(row.worktree, run_id),
            )
        with self._lock:
            spawned = self._children.get(run_id)
            sibling = self._children.get(preparation_sibling_key(run_id))
        # A preparation sibling (the CLI catalog beside the metadata app-server)
        # dies with its run and is read back like the leader's group.
        sibling_checked: tuple[ProcessIdentity, ...] = ()
        if sibling is not None and process_identity_matches(sibling.identity):
            with contextlib.suppress(child.ChildIdentityError):  # exited meanwhile
                sibling.stop_then_kill(sibling.identity)
            sibling_checked = (sibling.identity,)
        sibling_survivors = tuple(
            identity
            for identity in sibling_checked
            if not _wait_identity_dead(identity, self.CLOSE_POLL_TIMEOUT_S)
        )
        expected = spawned.identity if spawned is not None else row.process_identity
        now_ms = int(time.time() * 1000)
        if expected is None:
            if sibling_survivors:
                return CloseReadback(
                    run_id,
                    row.phase,
                    ProcessTreeReadback(sibling_checked, sibling_survivors),
                    self._claim_is_released(row.worktree, run_id),
                )
            # No process was ever on record, and none is tracked here. The
            # registry closes the row only while it still names none — a driver
            # that publishes one meanwhile keeps its run — and moves the claim
            # in the same transaction, refusing a poisoned one unless the
            # run's slice was drained.
            with self._lock:
                closed = (
                    self._registry.close_orphaned(run_id, None, now_ms, drained=drained)
                    if row.phase is RunPhase.ORPHANED
                    else self._registry.close_owned(
                        run_id, None, now_ms, drained=drained
                    )
                )
                if closed:
                    self._children.pop(run_id, None)
                self._lock.notify_all()
            if not closed:
                with self._lock:
                    current = self._registry.get_run(run_id)
                if current is not None and current.phase is RunPhase.CLOSED:
                    # Another process's close landed after the read above: the
                    # run is closed, and what is left is the closed-row path.
                    return self._close_locked(run_id, members, drained=drained)
                raise self._fence_refusal(run_id, row.worktree, drained=drained)
            return CloseReadback(
                run_id,
                RunPhase.CLOSED,
                ProcessTreeReadback(sibling_checked, ()),
                self._claim_is_released(row.worktree, run_id),
            )
        if process_identity_matches(expected):
            checked = (
                expected,
                *dict.fromkeys((*members, *_discover_group_members(expected))),
            )
            if spawned is not None:
                try:
                    spawned.stop_then_kill(expected)
                except child.ChildIdentityError:
                    # `_drain_writer` above kills the aggregate scope the
                    # child runs in, so the child can die between the check and
                    # the signal. Nothing was signalled; a still-matching
                    # identity is a real refusal, a dead one is the close done.
                    if process_identity_matches(expected):
                        raise
            else:
                _best_effort_killpg(expected.pid)
            survivors = tuple(
                identity
                for identity in checked
                if not _wait_identity_dead(identity, self.CLOSE_POLL_TIMEOUT_S)
            )
        else:
            checked = (expected, *members)
            survivors = tuple(
                identity
                for identity in members
                if not _wait_identity_dead(identity, self.CLOSE_POLL_TIMEOUT_S)
            )
        tree = ProcessTreeReadback(
            checked=(*checked, *sibling_checked),
            survivors=(*survivors, *sibling_survivors),
        )
        survivors = tree.survivors
        if survivors:
            # Nothing was released: a live process still holds the worktree.
            # The value is still READ rather than asserted — a run that never
            # claimed one (every `review`) honestly has no claim outstanding.
            return CloseReadback(
                run_id, row.phase, tree, self._claim_is_released(row.worktree, run_id)
            )
        with self._lock:
            # The fence is the identity the ROW names. A child this process
            # tracks may never have been published on it — a native run's
            # preparation inspector runs before its turn does — and then the
            # row names no process: the registry closes it only while that
            # stays true, so an identity published meanwhile still refuses.
            current = self._registry.get_run(run_id)
            fence = (
                None
                if current is not None and current.process_identity is None
                else expected
            )
            # An ORPHANED row is owned by an instance that stopped proving it is
            # alive, so `close_owned`'s `owner_instance = me` fence can never
            # match it: it refused, and the claim the sweep POISONED stayed on
            # the worktree forever. `close_orphaned` fences on the phase instead
            # and moves the row and the claim in the same one transaction.
            closed = (
                self._registry.close_orphaned(run_id, fence, now_ms, drained=drained)
                if row.phase is RunPhase.ORPHANED
                else self._registry.close_owned(run_id, fence, now_ms, drained=drained)
            )
            if closed:
                self._children.pop(run_id, None)
            self._lock.notify_all()
        if not closed:
            raise self._fence_refusal(run_id, row.worktree, drained=drained)
        # `close_owned` deletes the claim in the SAME transaction as the phase
        # move, so this read is the proof that it did, not a second attempt.
        return CloseReadback(
            run_id,
            RunPhase.CLOSED,
            tree,
            self._claim_is_released(row.worktree, run_id),
        )

    def _poisoned_undrained(self, run_id: str, worktree: str, *, drained: bool) -> bool:
        """This run's claim is poisoned and no drained slice proves it stopped."""
        with self._lock:
            claim = self._registry.read_claim(worktree)
        return (
            not drained
            and claim is not None
            and claim.run_id == run_id
            and claim.lease_until == POISONED_LEASE_UNTIL
        )

    def _fence_refusal(
        self, run_id: str, worktree: str, *, drained: bool
    ) -> RunCloseRejected:
        """Why the registry refused a close whose death proof held."""
        if self._poisoned_undrained(run_id, worktree, drained=drained):
            # Only a drained slice proves nothing of the run still writes, and
            # this run recorded none.
            return RunCloseRejected(
                f"run {run_id!r}: its claim on {worktree} is poisoned and no "
                "writer slice was recorded to prove the run stopped; confirm "
                "nothing of it still writes there, then delete its row from the "
                "nSPAWN store's claims table by hand"
            )
        return RunCloseRejected(
            f"run {run_id!r}: death was proven but the registry fence "
            "rejected the transition to closed"
        )


def _undrained(run_id: str, unit: str, refusal: ControllerRefusal) -> str:
    return (
        f"run {run_id!r}: writer scope {unit} was not observed empty: {refusal}; "
        "its claim stays held"
    )


#: The claude capabilities the caged composition serves, and a route that
#: reaches its model elsewhere therefore lacks.
CAGED: Final = frozenset({"custody", "write"})


def _claude_route_elsewhere(
    env: Mapping[str, str] | None, accounts: Iterable[NativeAccount]
) -> list[str]:
    """The names by which a Claude route (or its accounts) reaches its model
    somewhere other than the vendor's endpoint with a pinned token: the cage's
    closed env carries none of them. A proxy's own credential counts too: in the
    cage the pinned vendor token would go in its place."""
    declared = set(env or {})
    for account in accounts:
        declared |= set(account.env or {})
        if account.base_url is not None:
            declared.add(ANTHROPIC_BASE_URL_KEY)
    return sorted(declared & {*child.ROUTE_ENV_NAMES, *child.CREDENTIAL_ENV_NAMES})


def _refuse_roster_override(roster: Roster, picked: picker.PickerResult) -> None:
    provider = picked.executed_provider
    picker.refuse_roster_override(
        roster,
        provider,
        provider.accounts.get(picked.executed_account or ""),
        picked.requested_model,
        picked.requested_effort,
    )


def _picked_route_elsewhere(roster: Roster, picked: picker.PickerResult) -> list[str]:
    provider = picked.executed_provider
    account = (
        provider.accounts.get(picked.executed_account)
        if picked.executed_account is not None
        else None
    )
    return _claude_route_elsewhere(
        roster.resolve(provider.name).env, () if account is None else (account,)
    )


def _refuse_proxied_claude_writer(roster: Roster, picked: picker.PickerResult) -> None:
    """The cage's closed env carries only the vendor credential, so a Claude route
    that reaches its model through another endpoint cannot write caged."""
    provider = picked.executed_provider
    routed = _picked_route_elsewhere(roster, picked)
    if routed:
        raise InvalidRunRequest(
            f"native_write on claude route {provider.name!r} is refused: it declares "
            f"{', '.join(routed)}, and the caged writer reaches only the vendor's "
            "own endpoint with its pinned account"
        )


def _native_write_worktree(cwd: str, *, read_only: bool, session_id: str | None) -> str:
    """The canonical checkout a native write run may write, or a refusal."""
    if read_only:
        raise InvalidRunRequest("native_write cannot be read_only")
    if session_id is not None:
        raise InvalidRunRequest("native_write cannot resume a session_id")
    if not os.path.isabs(cwd) or not os.path.isdir(cwd):
        raise InvalidRunRequest(
            "native_write needs cwd to be an existing absolute directory"
        )
    worktree = os.path.realpath(cwd)
    try:
        git = os.lstat(os.path.join(worktree, ".git"))
    except OSError:
        git = None
    # lstat: a `.git` symlink points the read-only carve-out somewhere else.
    if git is None or not stat.S_ISDIR(git.st_mode):
        raise InvalidRunRequest("native_write needs cwd/.git to be a directory")
    return worktree


#: The one answer to a writable dispatch that is not a native write: the
#: body-accepted implementation mode that used to take it is retired.
#: AGY's native model child must enter the enforcing outer cage. Reads of an
#: existing run launch nothing; a missing runtime refuses before admission.
AGY_LAUNCH_REFUSAL: str = (
    "AGY launches are refused because the enforcing outer cage is unavailable; "
    "no run was created and nothing was re-routed to another backend"
)

_WRITE_NEEDS_NATIVE_WRITE: Final = (
    "a writable run needs native_write; the body-accepted implementation mode "
    "is retired"
)


def _refuse_undeclared_writer_root(worktree: str, roster: Roster) -> None:
    """A native writer's canonical `worktree` lies strictly under a root the host
    declared, so one mistyped `cwd` never hands it the owner's own checkout."""
    source = roster.path or "the roster"
    if not roster.native_write_roots:
        raise InvalidRunRequest(
            f"native_write is refused: {source} declares no [native_write] roots"
        )
    parents = Path(worktree).parents
    if not any(
        Path(os.path.realpath(root)) in parents for root in roster.native_write_roots
    ):
        raise InvalidRunRequest(
            f"native_write is refused: {worktree} is under no [native_write] root "
            f"of {source}"
        )


def _orchestrator_disposable(run_dir: Path) -> str:
    """The root of the turn's disposable needs: 0700 directories under the RUN's
    own directory, never under the clean snapshot the turn only reads.

    Every leaf of both trees -- the vendor child's state and the private MCP's --
    because admission already composed an environment that names them, and the
    NEUTRAL directory the app-server itself runs in, which is a leaf of the same
    trusted per-run root for the same reason: nothing of it is the snapshot's.
    """
    root = Path(native.orchestrator_disposable_root(run_dir))
    for directory in (
        root,
        *(root / leaf for leaf in native.orchestrator_disposable_leaves()),
    ):
        with contextlib.suppress(FileExistsError):
            directory.mkdir(mode=0o700)
        # Nothing follows a symlink an earlier run could have left here.
        if not stat.S_ISDIR(directory.lstat().st_mode):
            raise InvalidRunRequest(
                f"orchestrator directory {directory} is not a directory"
            )
    return str(root)


def _disposable_environment(worktree: str) -> dict[str, str]:
    """The tool environment of a native write run: its HOME, TMPDIR, caches and
    store are 0700 directories under `<worktree>/.nott/disposable`."""
    root = Path(worktree, ".nott", "disposable")
    names = {
        "HOME": root / "home",
        "TMPDIR": root / "tmp",
        "XDG_CACHE_HOME": root / "cache",
        "UV_CACHE_DIR": root / "uv-cache",
        "NOTT_DATABASE_DIR": root / "database",
    }
    for directory in (root.parent, root, *names.values()):
        with contextlib.suppress(FileExistsError):
            directory.mkdir(mode=0o700)
        # An earlier writer could leave a symlink here; nothing follows it.
        if not stat.S_ISDIR(directory.lstat().st_mode):
            raise InvalidRunRequest(
                f"native write directory {directory} is not a directory"
            )
    return {name: str(directory) for name, directory in names.items()}


def _identity_record(identity: ProcessIdentity) -> dict[str, Any]:
    return {
        "pid": identity.pid,
        "boot_id": identity.boot_id,
        "start_ticks": identity.start_ticks,
    }


def _outcome_of(readback: CloseReadback) -> dict[str, Any]:
    """A close readback as the `run_control.outcome` record."""
    return {
        "phase": readback.phase.value,
        "claim_released": readback.claim_released,
        "checked": [_identity_record(i) for i in readback.tree.checked],
        "survivors": [_identity_record(i) for i in readback.tree.survivors],
    }


def _readback_of(run_id: str, outcome: dict[str, Any]) -> CloseReadback:
    """The close record another process wrote, as this process's answer."""
    refusal = outcome.get("refusal")
    if isinstance(refusal, dict):
        error = RunCloseRejected(
            f"run {run_id!r}: its driver's close refused: "
            f"{refusal.get('type')}: {refusal.get('message')}"
        )
        error.data = {"refusal": refusal}
        raise error
    return CloseReadback(
        run_id,
        RunPhase(outcome["phase"]),
        ProcessTreeReadback(
            tuple(ProcessIdentity(**record) for record in outcome["checked"]),
            tuple(ProcessIdentity(**record) for record in outcome["survivors"]),
        ),
        bool(outcome["claim_released"]),
    )
