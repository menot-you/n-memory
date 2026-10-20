"""The durable run registry and its d17 worktree claims.

sub-unit: opening the registry, the instance row, and the claim lifecycle.

WHAT A CLAIM PROTECTS. One worktree has ONE writer. A claim is refused while the
existing lease is still live, and ALSO while the run holding it still has
protected custody — a non-closed run that is orphaned, or that still carries any
part of its process triple. An expired lease is NOT enough to take a worktree
from a process that may still be running in it.

The store is one SQLite file opened through `nott_database`. Every write is a
`write()` block (`BEGIN IMMEDIATE`, a SAVEPOINT when nested), so two claimers
serialize on the store's write lock instead of racing; SQLite's own
`busy_timeout` is the only wait.
"""

import contextlib
import hashlib
import json
import os
import re
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from types import TracebackType
from typing import Any, Final, Self
from zoneinfo import ZoneInfo

import nott_database
from nott_database import BusyError, DatabaseError, write

from .errors import NSpawnError
from .inference import InferenceHome, StructuredInference
from .limits import MAX_DURABLE_IDENTIFIER_BYTES, MAX_DURABLE_PATH_BYTES
from .process_identity import (
    ProcessIdentity,
    of_earlier_boot,
    process_identity_matches,
)
from .rows import (
    INSERT_COLUMNS,
    RUN_COLUMNS,
    RUN_EVENT_COLUMNS,
    SHA256_HEX_LENGTH,
    TERMINAL_PHASES,
    UPDATE_COLUMNS,
    AccountUsageRow,
    AccountWallRow,
    Backend,
    ClassLabel,
    ClassObservation,
    ComputeBalanceRow,
    NativeSessionContext,
    ProviderWallRow,
    RunEventRow,
    RunPage,
    RunPhase,
    RunRow,
    SessionActivation,
    SessionProfile,
    SessionReference,
    WorktreeClaim,
)
from .schema import (
    ADMISSION_HELD,
    ADMISSION_TABLES_DDL,
    ADMISSION_TRIGGER_DDL,
    ADMISSION_UNBOUND,
    CLASS_OBSERVATIONS_DDL,
    CLASS_OUTCOME_TRIGGER_DDL,
    COMPUTE_BALANCE_DDL,
    KEY_FENCE_TRIGGER_DDL,
    KEY_FENCED,
    KEY_FENCES_DDL,
    KEYED_RETENTION_MS,
    LEGACY_THREAD_INDEX_DDL,
    NOTICE_ARM_TRIGGER_DDL,
    NOTICE_GENERATION_COLUMN,
    OWNER_STAMPS_DDL,
    REGISTRY_SCHEMA_VERSION,
    RUN_OWNER_COLUMN,
    SCHEMA_DDL,
    STORE_FILE,
    TERMINAL_RECEIPT_TRIGGER_DDL,
    THREAD_ORIGIN_INDEX_DDL,
    WALL_SOURCE_VALUES,
)

#: Every store failure a caller may see below the typed refusals: the library's
#: typed errors and the constraint errors SQLite raises as they are.
STORE_ERRORS: Final = (DatabaseError, nott_database.Error)


SAFE_TOKEN_INTEGER = 2**53 - 1
USAGE_EVENT_CAP = 20_000
# Keep 30-day series below the MCP inline response limit.
USAGE_AGENT_CAP = 8


def token_counter(value: object) -> int | None:
    """Accept exact nonnegative counters within the JSON integer limit."""
    return value if type(value) is int and 0 <= value <= SAFE_TOKEN_INTEGER else None


def token_sum(values: Iterable[object]) -> int | None:
    counters = [token_counter(value) for value in values]
    if any(value is None for value in counters):
        return None
    total = sum(value for value in counters if value is not None)
    return total if total <= SAFE_TOKEN_INTEGER else None


class RegistryError(NSpawnError):
    """A refusal from the registry."""

    def __init__(self, message: str) -> None:
        self.message = message
        Exception.__init__(self, message)


class StaleComputeReading(RegistryError):
    """A compute reading older than the stored one; the store refused it."""


class UnrecognizedSchema(RegistryError):
    """The file carries a schema version this build does not open."""


class KeyFencedRow(RegistryError):
    """An admission's `run_keys` row named a fenced key; the store refused it."""


class AdmissionHeldRow(RegistryError):
    """The node's reversible Stop fence refused admission, without consuming a key."""

    def __init__(self, node: str, generation: int) -> None:
        super().__init__(f"{ADMISSION_HELD}: {node} at generation {generation}")
        self.data = {"node": node, "generation": generation}


class AdmissionUnboundRow(RegistryError):
    """A reserved key or bound group was used outside its immutable binding."""


class UniqueTaken(RegistryError):
    """An admission hit a unique index; `constraint` names the one it was."""

    def __init__(self, message: str, constraint: str | None) -> None:
        super().__init__(message)
        self.constraint = constraint


def now_ms() -> int:
    """The wall clock in milliseconds, the unit of every time column."""
    return int(time.time() * 1000)


def store_path() -> Path:
    """This process's registry file: `NOTT_DATABASE_DIR` (tests), else the live one."""
    return nott_database.directory() / STORE_FILE


def create_store(
    path: str | os.PathLike[str], *, allow_live_store: bool = False
) -> None:
    """Bring a registry file into existence with the current schema."""
    nott_database.create(path, SCHEMA_DDL, allow_live_store=allow_live_store)


#: How many runs one page answers, and the ceiling any caller can ask for.
#: The oracle keeps both beside the registry rather than in `limits.rs`.
MAX_RUN_PAGE = 128

#: The phases `mark_orphans` REFUSES to overwrite, spelled for SQL from the one
#: definition in `rows.TERMINAL_PHASES`. It used to be the literal
#: `('closed', 'orphaned')` — two of the four — so a run that reached
#: `completed` or `failed` was relabelled `orphaned` by the next peer sweep.
#: Measured 2026-08-31 on the live registry: 501 of 665 orphans carried
#: `final_text`. Custody is lost mid-flight; a run that already finished had
#: none left to lose.
TERMINAL_PHASE_SQL = ", ".join(
    f"'{phase.value}'" for phase in sorted(TERMINAL_PHASES, key=lambda p: p.value)
)

#: The lease a poisoned claim carries: the largest signed 64-bit instant, so no
#: `now` can ever exceed it and the worktree stays fenced until a human acts.
POISONED_LEASE_UNTIL = 9223372036854775807

#: The ONE definition of "the run behind this claim still has protected custody":
#: a non-closed run that is orphaned, or that still carries any part of its
#: process triple. It correlates on `claims.run_id`, so every statement that
#: embeds it names `claims` as its own target or in its FROM. Taking a worktree
#: and releasing one are the same question asked from two sides, and a second
#: copy of this predicate is how the two sides drift apart.
PROTECTED_CUSTODY_SQL = """SELECT 1 FROM runs
                        WHERE runs.run_id = claims.run_id
                          AND runs.phase != 'closed'
                          AND (
                              runs.phase = 'orphaned'
                              OR runs.process_group IS NOT NULL
                              OR runs.process_pid IS NOT NULL
                              OR runs.process_boot_id IS NOT NULL
                              OR runs.process_start_ticks IS NOT NULL
                          )"""


@dataclass(frozen=True, slots=True)
class CloseOutcome:
    """What a close DID: one field per statement whose effect was observed.

    `run_closed` is the rowcount of the UPDATE that closed the run.
    `claim_released` is the rowcount of the DELETE that removed its claim, and of
    NO other statement. They are separate fields because they fail separately: a
    run the fence rejected never reaches the delete at all, and a run that closed
    may have held no claim to begin with. Both of those are
    `claim_released=False`, and neither of them means "another writer still holds
    the worktree" — whoever reports this to a human MUST NOT fold the three into
    one sentence.
    """

    run_closed: bool
    claim_released: bool


#: Where an effort profile's value comes from: the run's own admission record,
#: or nothing on record. The words are the consumer contract (RULEBOOK).
EFFORT_FROM_ADMISSION: Final = "admission"
EFFORT_UNKNOWN: Final = "unknown"


@dataclass(frozen=True, slots=True)
class EffortProfile:
    """The effort a run was asked for, handed to its launcher, and ran at.

    A null value beside `admission` is a recorded absence: the caller asked for
    nothing, or the launcher was handed nothing. A null beside `unknown` is a fact
    nobody wrote: a run admitted before the record existed, and `executed` for
    every run, because the current adapters have no verified turn-scoped effort echo.
    """

    requested: str | None = None
    requested_source: str = EFFORT_UNKNOWN
    sent: str | None = None
    sent_source: str = EFFORT_UNKNOWN
    executed: str | None = None
    executed_source: str = EFFORT_UNKNOWN


#: The profile of a run with nothing on record.
UNKNOWN_EFFORT: Final = EffortProfile()


class WorktreeHeld(NSpawnError):
    """Another run holds this worktree — by a live lease or by live custody."""

    def __init__(self, holder: str, run_id: str, lease_until: int) -> None:
        self.holder = holder
        self.run_id = run_id
        self.lease_until = lease_until
        Exception.__init__(
            self,
            f"worktree is held by run {run_id!r} under holder {holder!r} "
            f"until {lease_until}. If that run ended, close it with "
            "nspawn_run_close to release its claim",
        )


class DriverCeiling(RegistryError):
    """Another driver launch would pass the host's live-driver ceiling."""


#: How long a pusher's claim on a notice holds before the drain may take it.
NOTICE_LEASE_S: Final = 30
#: A notice ready this long with no delivery goes to the next session of its
#: project, whether or not its caller still lives.
NOTICE_STALE_S: Final = 120
#: Notices are pruned on their own clock, with no foreign key to `runs`.
NOTICE_RETENTION_DAYS: Final = 7
#: A non-centre node's caller address and run owner: `node:<session>`. Its notices
#: are the host's (`take_notices`): never pushed, never drained, and kept past
#: NOTICE_RETENTION_DAYS until delivered.
NODE_PREFIX: Final = "node:"
_NODE_NOTICE_SQL: Final = f"notices.caller_addr GLOB '{NODE_PREFIX}*'"


@dataclass(frozen=True, slots=True)
class NoticeCaller:
    """The Claude Code session a run's terminal notice is owed to.

    The process that owns the messaging socket, never the stdio process that
    Claude Code can restart while the session lives; `project` scopes the drain.
    """

    addr: str
    pid: int
    start_ticks: int
    project: str

    def __post_init__(self) -> None:
        _validate_required("notices.caller_addr", self.addr, MAX_DURABLE_PATH_BYTES)
        _validate_required(
            "notices.caller_project", self.project, MAX_DURABLE_PATH_BYTES
        )
        if type(self.pid) is not int or self.pid <= 0:
            raise InvalidField(f"notices.caller_pid must be positive, got {self.pid!r}")
        if type(self.start_ticks) is not int or self.start_ticks < 0:
            raise InvalidField(
                f"notices.caller_start_ticks must be >= 0, got {self.start_ticks!r}"
            )


@dataclass(frozen=True, slots=True)
class ReadyNotice:
    """A ready, undelivered notice with no live claim, as a pusher sees it."""

    run_id: str
    addr: str
    pid: int
    start_ticks: int


@dataclass(frozen=True, slots=True)
class TakenNotice:
    """A node's notice the host claimed: delivered once it acks `(run_id,
    generation, token)`."""

    run_id: str
    owner: str
    generation: int
    token: str
    phase: str
    group: str | None
    key: str | None
    failure_reason: str | None


#: The notice rows of the nodes `:prefix` names.
_NOTICE_PREFIX_SQL: Final = "substr(notices.caller_addr, 1, length(:prefix)) = :prefix"


def _node_prefix(prefix: str) -> None:
    """The host's verbs reach node notices only, never a session's."""
    if not prefix.startswith(NODE_PREFIX):
        raise RegistryError(
            f"a notice owner prefix starts with {NODE_PREFIX!r}; got {prefix!r}"
        )


#: The partial unique index that makes one (project, key) one run.
RUN_KEY_CONSTRAINT: Final = "run_keys_project_key"


class IdempotencyBusy(RegistryError):
    """The key's lock stayed held past its timeout. Retryable: the same call again."""

    #: Rendered beside the message at the MCP boundary: `{"retryable": True}`.
    data: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class RunKey:
    """A run's dedup identity, written with its admission."""

    launch_uuid: str
    project: str
    idempotency_key: str | None
    fingerprint: str
    prompt_sha256: str

    def __post_init__(self) -> None:
        _validate_required("run_keys.project", self.project, MAX_DURABLE_PATH_BYTES)
        if self.idempotency_key is not None:
            _validate_required(
                "run_keys.idempotency_key",
                self.idempotency_key,
                MAX_DURABLE_IDENTIFIER_BYTES,
            )
        for name in ("fingerprint", "prompt_sha256"):
            value = getattr(self, name)
            if not re.fullmatch(r"[0-9a-f]{64}", value):
                raise InvalidField(f"run_keys.{name} must be a sha256 hex digest")


@dataclass(frozen=True, slots=True)
class KeyedRun:
    """The run a key already names, its phase read under the run row's lock."""

    run_id: str
    fingerprint: str
    phase: RunPhase
    #: The node that admitted it (`RunRow.owner`); a reattach must be the same.
    owner: str | None = None


def _insert_notice(
    connection: nott_database.Connection,
    run_id: str,
    notice: NoticeCaller,
    *,
    conflict: str,
) -> None:
    connection.execute(
        "INSERT INTO notices(run_id, caller_addr, caller_pid, caller_start_ticks, "
        f"caller_project, created_at) VALUES (?, ?, ?, ?, ?, ?){conflict}",
        (run_id, notice.addr, notice.pid, notice.start_ticks, notice.project, now_ms()),
    ).close()


#: The one lock file per store behind every keyed admission.
KEY_LOCK_FILE: Final = "nspawn.keylock"

#: The run a key names in its project, and that run's phase.
_KEYED_RUN_SQL: Final = (
    "SELECT k.run_id, k.fingerprint, r.phase, r.owner FROM run_keys k "
    "JOIN runs r ON r.run_id = k.run_id "
    "WHERE k.project = ? AND k.idempotency_key = ?"
)


def _keyed_run(rows: list[nott_database.Row]) -> KeyedRun | None:
    if not rows:
        return None
    row = rows[0]
    return KeyedRun(
        str(row["run_id"]),
        str(row["fingerprint"]),
        RunPhase(row["phase"]),
        None if row["owner"] is None else str(row["owner"]),
    )


class KeyLock:
    """The store's key lock: an `flock` held from before a keyed dispatch's first
    side effect until its admission (or its reattach) has committed, so a second
    driver of the key waits and then reads the first one's `run_keys` row.

    ponytail: one file per store, so admissions of different keys wait on each
    other briefly; per-key files only if a measured wait says so. The unique
    index `run_keys_project_key` and the KeyTaken re-read stay as the backstop.
    The reads and the reattach go through the caller's own registry connection,
    under `guard` (the supervisor's lock), inside one short `transaction()`.
    """

    def __init__(
        self,
        registry: Registry,
        guard: contextlib.AbstractContextManager[Any],
        project: str,
        key: str,
        timeout_s: float,
    ) -> None:
        self.project = project
        self.key = key
        #: A batch entry's `K/i` compares its batch's `K/*` rows, which the
        #: store-wide lock keeps still under the comparison.
        self.batch_key = key.rpartition("/")[0] or None
        self._registry = registry
        self._guard = guard
        self._timeout_s = timeout_s
        self._held = contextlib.ExitStack()
        with self._mapped():
            self._held.enter_context(
                nott_database.lock(
                    registry.path.parent / KEY_LOCK_FILE,
                    wait=timeout_s,
                    allow_live_store=registry.allow_live_store,
                )
            )

    @contextlib.contextmanager
    def _mapped(self) -> Iterator[None]:
        """Held past the wait is the retryable `IdempotencyBusy`; any other store
        failure is a `RegistryError`, never a raw store one."""
        try:
            yield
        except BusyError as error:
            busy = IdempotencyBusy(
                f"idempotency key {self.key!r} is being admitted by another "
                f"dispatch and the lock stayed held past {self._timeout_s:.0f} s; "
                "retry the same call"
            )
            busy.data = {"retryable": True}
            raise busy from error
        except STORE_ERRORS as error:
            raise RegistryError(
                f"idempotency key {self.key!r} could not be read: {error}"
            ) from error

    @contextlib.contextmanager
    def transaction(self) -> Iterator[None]:
        """One short write for `batch_conflict`, `find` and `reattach`."""
        with self._mapped(), self._guard, self._registry._transaction():
            yield

    def batch_conflict(self, fingerprint: str) -> str | None:
        """A run of this key's batch admitted for another batch, else None.

        Every `K/*` row of one batch carries the same print (the request
        without its entry's provider), so any other print is another batch.
        """
        if self.batch_key is None:
            return None
        prefix = f"{self.batch_key}/"
        rows = nott_database.query(
            self._registry.connection,
            "SELECT run_id FROM run_keys WHERE project = ? "
            "AND substr(idempotency_key, 1, length(?)) = ? AND fingerprint <> ? "
            "LIMIT 1",
            (self.project, prefix, prefix, fingerprint),
        )
        return str(rows[0]["run_id"]) if rows else None

    def find(self) -> KeyedRun | None:
        """The key's run and its phase, read inside the write transaction, so a
        terminal commit in flight has either landed or not begun."""
        return _keyed_run(
            nott_database.query(
                self._registry.connection, _KEYED_RUN_SQL, (self.project, self.key)
            )
        )

    def fenced(self) -> bool:
        """Whether this key's claim was released naming no run (`fence`)."""
        return bool(
            nott_database.query(
                self._registry.connection,
                "SELECT 1 FROM key_fences WHERE project = ? AND idempotency_key = ?",
                (self.project, self.key),
            )
        )

    def fence(self) -> None:
        """Refuse every later admission of this key: its claim was released."""
        self._registry.connection.execute(
            "INSERT INTO key_fences(project, idempotency_key, fenced_at) "
            "VALUES (?, ?, ?) ON CONFLICT DO NOTHING",
            (self.project, self.key, now_ms()),
        ).close()

    def reattach(
        self, run_id: str, notice: NoticeCaller, *, orphaned: bool = False
    ) -> None:
        """The caller's row; an existing one for it is already right.

        Pending for a live run: the trigger arms it. For an `orphaned` run it is
        written armed AND delivered as `orphaned` — the ack delivered that phase —
        so a run that never moves again leaves nothing undelivered, while one
        that `update_run` revives is re-armed by the trigger on its next terminal.
        """
        connection = self._registry.connection
        if not orphaned:
            _insert_notice(
                connection,
                run_id,
                notice,
                conflict=" ON CONFLICT (run_id, caller_addr) DO NOTHING",
            )
            return
        now = now_ms()
        connection.execute(
            "INSERT INTO notices(run_id, caller_addr, caller_pid, caller_start_ticks, "
            "caller_project, phase, created_at, ready_at, delivered_at) "
            "VALUES (?, ?, ?, ?, ?, 'orphaned', ?, ?, ?) "
            "ON CONFLICT (run_id, caller_addr) DO NOTHING",
            (
                run_id,
                notice.addr,
                notice.pid,
                notice.start_ticks,
                notice.project,
                now,
                now,
                now,
            ),
        ).close()

    def commit(self) -> None:
        """Release the lock: the admission or the reattach has committed."""
        self._held.close()

    def close(self) -> None:
        self._held.close()


#: A notice's phase at delivery: the run's current one, else the armed one
#: (the run row may already be pruned). Correlates on `n`.
_NOTICE_PHASE_SQL = (
    "COALESCE((SELECT r.phase FROM runs r WHERE r.run_id = notices.run_id), "
    "notices.phase)"
)
#: Ready, undelivered, and no live claim on it at `:now`.
_NOTICE_FREE_SQL = (
    "notices.ready_at IS NOT NULL AND notices.delivered_at IS NULL "
    "AND (notices.claim_lease_until IS NULL OR notices.claim_lease_until < :now)"
)
#: The group that makes a run a swarm member (owner, 2026-10-20).
SWARM_PREFIX: Final = "swarm:"
#: The swarm members of `notices`' run: the runs of its `swarm:` group with a
#: notice for the same caller, the run itself included. A swarm is one caller's.
_SWARM_MEMBERS_SQL = (
    "SELECT sn.run_id, sn.ready_at, sn.delivered_at, sn.claim_lease_until "
    "FROM runs mine "
    "JOIN runs sib ON sib.group_name = mine.group_name "
    "JOIN notices sn ON sn.run_id = sib.run_id "
    "AND sn.caller_addr = notices.caller_addr "
    f"WHERE mine.run_id = notices.run_id AND substr(mine.group_name, 1, "
    f"{len(SWARM_PREFIX)}) = '{SWARM_PREFIX}'"
)
#: A swarm member's notice waits while a member's notice is not armed yet, or
#: is under a live claim at `:now`: the swarm leaves as one unit.
_SWARM_HELD_SQL = (
    f"EXISTS (SELECT 1 FROM ({_SWARM_MEMBERS_SQL}) WHERE ready_at IS NULL "
    "OR (delivered_at IS NULL AND claim_lease_until >= :now))"
)
#: When a notice became ready; a swarm member's is its last member's.
_SWARM_READY_SQL = (
    f"COALESCE((SELECT max(ready_at) FROM ({_SWARM_MEMBERS_SQL})), notices.ready_at)"
)


def _validate_required(name: str, value: str, maximum: int) -> None:
    size = len(value.encode("utf-8"))
    if size < 1 or size > maximum:
        raise RegistryError(f"{name} must be between 1 and {maximum} bytes, got {size}")


def _recovery_reference(value: object) -> dict[str, str]:
    """Validate durable reference syntax before it can fence a worktree."""
    if not isinstance(value, dict) or set(value) != {"path", "sha256"}:
        raise RegistryError("implementation recovery artifact is malformed")
    path, digest = value["path"], value["sha256"]
    if (
        not isinstance(path, str)
        or "\x00" in path
        or not PurePosixPath(path).is_absolute()
        or not isinstance(digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
    ):
        raise RegistryError("implementation recovery artifact is malformed")
    _validate_required(
        "implementation recovery artifact path", path, MAX_DURABLE_PATH_BYTES
    )
    return {"path": path, "sha256": digest}


def _effort_record(value: object) -> dict[str, str | None]:
    """The admission's effort record, exactly `{requested, sent}`, or a refusal.

    The same check guards the write and the read: admission knows what was asked
    and what was sent, so a record naming anything else (an `executed` above all)
    is forged, not extended.
    """
    if not isinstance(value, dict) or set(value) != {"requested", "sent"}:
        raise RegistryError("the effort record must be exactly {requested, sent}")
    for name in ("requested", "sent"):
        effort = value[name]
        if effort is not None:
            if not isinstance(effort, str):
                raise RegistryError(f"the effort record's {name} must be text or null")
            _validate_required(
                f"the effort record's {name}", effort, MAX_DURABLE_IDENTIFIER_BYTES
            )
    return {"requested": value["requested"], "sent": value["sent"]}


def _validate_wall_source(source: str) -> None:
    if source not in WALL_SOURCE_VALUES:
        raise RegistryError(f"walls.source must be one of {WALL_SOURCE_VALUES}")


def _validate_wall_time(name: str, value: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise RegistryError(f"{name} must be an integer")
    if value < 0:
        raise RegistryError(f"{name} must not be negative")


def _admission_validate(
    group: str, node: str, project: str, decision: str, generation: int
) -> None:
    """Validate coordinator identity before writing any binding or reservation."""
    for name, value in (("group", group), ("node", node), ("decision", decision)):
        _validate_required(f"admission {name}", value, MAX_DURABLE_IDENTIFIER_BYTES)
        if not value.strip() or re.search(r"[\x00-\x1f\x7f]", value):
            raise RegistryError(
                f"admission {name} must be nonblank text without controls"
            )
    try:
        canonical = node == NODE_PREFIX + str(uuid.UUID(node.removeprefix(NODE_PREFIX)))
    except ValueError:
        canonical = False
    if not canonical:
        raise RegistryError("admission node must be node:<canonical session uuid>")
    _validate_required("admission project", project, MAX_DURABLE_PATH_BYTES)
    if not PurePosixPath(project).is_absolute() or re.search(
        r"[\x00-\x1f\x7f]", project
    ):
        raise RegistryError(
            "admission project must be an absolute path without controls"
        )
    _validate_wall_time("admission generation", generation)
    if generation > 9223372036854775807:
        raise RegistryError("admission generation must fit a SQLite INTEGER")


#: The initial link of every chain. A run without a previous event chains over this
#: empty string, so the first row is verifiable without a special case.
GENESIS_HASH: Final = ""


def compute_chained_hash(previous_hash: str, canonical_line: str) -> str:
    """SHA-256 over `previous_hash + "\\n" + canonical_line`."""
    return hashlib.sha256(f"{previous_hash}\n{canonical_line}".encode()).hexdigest()


#: Exactly the keys one orchestrator binding records, and their order in the
#: event payload. The store keeps no column for any of them: the binding is a
#: `lifecycle` payload like the writer's slice, and a key beside these would be
#: one more thing admission claims to have bound.
#:
#: THE WHOLE BINDING, not the session and the contract alone: a resume is checked
#: against this record (`native.OrchestratorBinding.resume_refusal`), so a record
#: short of the repository, the store or the interpreter cannot answer whether a
#: resume names the same ones -- and a record that cannot answer is a resume that
#: may change them.
#:
#: BOTH TREES, under their own names: the `repository` the turn's proposals are
#: bound to (the session's real, editable root) and the `snapshot` it reads (the
#: admission's own cwd). A record carrying one path for both could not say which
#: tree a resume is restating, and a run recorded without the `repository` cannot
#: be resumed at all -- `validated_orchestrator_binding` reads this key set on the
#: way in and on the way out, so a short record is a refusal, never a default.
ORCHESTRATOR_BINDING_IDENTIFIERS: Final = ("session", "profile")
ORCHESTRATOR_BINDING_PATHS: Final = (
    "repository",
    "snapshot",
    "runtime",
    "database_dir",
    "interpreter",
)
ORCHESTRATOR_BINDING_KEYS: Final = (
    *ORCHESTRATOR_BINDING_IDENTIFIERS,
    "tool_contract",
    *ORCHESTRATOR_BINDING_PATHS,
)


def validated_orchestrator_binding(binding: object) -> dict[str, Any]:
    """One complete recorded binding, or a refusal naming the field.

    Read on the way IN and on the way back OUT, so a payload another writer left
    in this run's chain can neither be admitted nor answered as a binding -- and
    so a run recorded with an incomplete binding is refused a resume rather than
    resumed against the half of it that was written down.
    """
    if not isinstance(binding, dict) or set(binding) != set(ORCHESTRATOR_BINDING_KEYS):
        raise RegistryError(
            "an orchestrator binding names exactly "
            f"{', '.join(ORCHESTRATOR_BINDING_KEYS)}"
        )
    for name in ORCHESTRATOR_BINDING_IDENTIFIERS + ORCHESTRATOR_BINDING_PATHS:
        value = binding[name]
        if (
            not isinstance(value, str)
            or not value
            or not value.isprintable()
            or len(value.encode("utf-8")) > MAX_DURABLE_IDENTIFIER_BYTES
        ):
            raise RegistryError(
                f"an orchestrator binding's {name} must be a bounded identifier"
            )
    for name in ORCHESTRATOR_BINDING_PATHS:
        value = binding[name]
        if not PurePosixPath(value).is_absolute() or value != os.path.normpath(value):
            raise RegistryError(
                f"an orchestrator binding's {name} must be an absolute canonical path"
            )
    contract = binding["tool_contract"]
    if (
        not isinstance(contract, dict)
        or set(contract) != {"mcp", "project", "tools"}
        or not isinstance(contract["mcp"], str)
        or not contract["mcp"]
        or not isinstance(contract["project"], str)
        or not contract["project"]
        or not isinstance(contract["tools"], list)
        or not contract["tools"]
        or any(not isinstance(tool, str) or not tool for tool in contract["tools"])
    ):
        raise RegistryError(
            "an orchestrator binding's tool_contract names one MCP, one project "
            "and its tools"
        )
    return {name: binding[name] for name in ORCHESTRATOR_BINDING_KEYS}


def _orchestrator_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """The lifecycle payload an admission appends, validated before any row."""
    if not isinstance(payload, Mapping) or set(payload) != {"orchestrator_binding"}:
        raise RegistryError(
            "an orchestrator payload carries exactly `orchestrator_binding`"
        )
    return {
        "orchestrator_binding": validated_orchestrator_binding(
            payload["orchestrator_binding"]
        )
    }


def _unique_constraint(error: nott_database.IntegrityError) -> str | None:
    """The unique index an IntegrityError names, when it is one this store keys.

    SQLite names the columns, never the index: `run_keys.project,
    run_keys.idempotency_key` is `run_keys_project_key`.
    """
    message = str(error)
    if not message.startswith("UNIQUE constraint failed: "):
        return None
    if message.endswith("run_keys.project, run_keys.idempotency_key"):
        return RUN_KEY_CONSTRAINT
    return message.removeprefix("UNIQUE constraint failed: ")


#: A stored window walls its account only at or past 100%: the known exhausted
#: window. A 95% threshold would wall an
#: account at 96% of a weekly window for the week on no failure at all.
EXHAUSTED_UTILIZATION: Final = 1.0


def _window_kind(name: str) -> str:
    """A window's bucket across sources. The unprefixed codex slots (`secondary`,
    the usage API's `secondary_window`) are the main `codex`
    limit's, so they are `codex.secondary` read another way, and a slot qualified
    by the length it declared (`codex.primary.seven_day`) is that slot. Every
    other name (`review.primary`, `seven_day`) is its own bucket."""
    slots = ("primary", "secondary", "tertiary")
    base = name.removesuffix("_window")
    head, _, length = base.rpartition(".")
    if length in ("five_hour", "seven_day") and head.rpartition(".")[2] in slots:
        base = head
    return "codex." + base if base in slots else base


class Registry:
    """One instance's handle on the durable registry file.

    The connection is shared between threads (the supervisor's workers, its
    reaper, a driver's close watcher), and a sqlite3 connection does not
    serialize itself: one thread's COMMIT would end another's transaction.
    Every statement and every transaction therefore runs under `_serial`, a
    reentrant lock taken after any caller lock and never held across a call
    out of the registry.
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        instance_id: str,
        *,
        allow_live_store: bool = False,
    ) -> None:
        _validate_required(
            "instances.instance_id", instance_id, MAX_DURABLE_IDENTIFIER_BYTES
        )
        self.path = Path(path)
        self.allow_live_store = allow_live_store
        self.instance_id = instance_id
        self._connection: nott_database.Connection | None = None
        self._serial = threading.RLock()

    # -- one statement ---------------------------------------------------------

    @contextlib.contextmanager
    def _transaction(self) -> Iterator[None]:
        """One `write()` on this registry's connection, serialized."""
        with self._serial, write(self.connection):
            yield

    def _rows(self, sql: str, params: Any = ()) -> list[nott_database.Row]:
        with self._serial:
            return nott_database.query(self.connection, sql, params)

    def _one(self, sql: str, params: Any = ()) -> nott_database.Row | None:
        rows = self._rows(sql, params)
        return rows[0] if rows else None

    def _run(self, sql: str, params: Any = ()) -> int:
        """Execute one statement inside the caller's write; its rowcount."""
        with self._serial:
            cursor = self.connection.execute(sql, params)
            try:
                return cursor.rowcount
            finally:
                cursor.close()

    def _write(self, sql: str, params: Any = ()) -> int:
        """One statement in its own short write; its rowcount."""
        with self._transaction():
            return self._run(sql, params)

    # -- append-only events -----------------------------------------------

    def append_run_event(
        self, run_id: str, *, at: int, kind: str, payload: object
    ) -> RunEventRow:
        """Appends an event and reads it back before committing."""
        try:
            with self._transaction():
                return self._append_run_event(run_id, at=at, kind=kind, payload=payload)
        except STORE_ERRORS as exc:
            raise RegistryError(f"the run event could not be appended: {exc}") from exc
        except ValueError as exc:
            raise RegistryError(f"the run event is not a valid row: {exc}") from exc

    def _append_run_event(
        self, run_id: str, *, at: int, kind: str, payload: object
    ) -> RunEventRow:
        """One event inside the CALLER's write, read back before it commits.

        Separated from `append_run_event` so an admission can carry an event in
        its OWN transaction: the row and the event then land together or not at
        all, and nothing here opens a second write to nest inside the first.
        """
        if kind == "usage" and isinstance(payload, dict):
            key = payload.get("observation_key")
            if isinstance(key, str):
                duplicate = self._one(
                    f"SELECT {', '.join(RUN_EVENT_COLUMNS)} FROM run_events WHERE run_id = ? AND kind = 'usage' "
                    "AND json_extract(payload, '$.observation_key') = ? LIMIT 1",
                    (run_id, key),
                )
                if duplicate is not None:
                    return RunEventRow(
                        run_id=str(duplicate["run_id"]),
                        seq=int(duplicate["seq"]),
                        at=int(duplicate["at"]),
                        kind=str(duplicate["kind"]),
                        payload=json.loads(duplicate["payload"]),
                        chained_hash=str(duplicate["chained_hash"]),
                    )
            node = self._usage_node(self.get_run(run_id))
            if node is not None:
                payload = {**payload, "node_id": node}
        if (
            kind == "lifecycle"
            and isinstance(payload, dict)
            and "usage_pending" in payload
        ):
            payload = {
                **payload,
                "usage_identity": self._usage_node(self.get_run(run_id)),
            }
        previous = self._one(
            "SELECT seq, chained_hash FROM run_events "
            "WHERE run_id = ? ORDER BY seq DESC LIMIT 1",
            (run_id,),
        )
        if previous is None:
            seq, previous_hash = 1, GENESIS_HASH
        else:
            seq = int(previous["seq"]) + 1
            previous_hash = str(previous["chained_hash"])
        unhashed = RunEventRow(
            run_id=run_id,
            seq=seq,
            at=at,
            kind=kind,
            payload=payload,
            chained_hash="0" * SHA256_HEX_LENGTH,
        )
        row = replace(
            unhashed,
            chained_hash=compute_chained_hash(previous_hash, unhashed.canonical_line()),
        )
        columns = ", ".join(RUN_EVENT_COLUMNS)
        placeholders = ", ".join(f":{c}" for c in RUN_EVENT_COLUMNS)
        self._run(
            f"INSERT INTO run_events({columns}) VALUES ({placeholders})",
            {
                "run_id": row.run_id,
                "seq": row.seq,
                "at": row.at,
                "kind": row.kind,
                "payload": json.dumps(row.payload, ensure_ascii=False, allow_nan=False),
                "chained_hash": row.chained_hash,
            },
        )
        readback = self._one(
            "SELECT chained_hash FROM run_events WHERE run_id = ? AND seq = ?",
            (row.run_id, row.seq),
        )
        if readback is None or readback["chained_hash"] != row.chained_hash:
            raise RegistryError("the appended run event did not read back identically")
        return row

    def read_run_events(self, run_id: str) -> list[RunEventRow]:
        """Every run event, in `seq` order."""
        return [
            RunEventRow(
                run_id=str(r["run_id"]),
                seq=int(r["seq"]),
                at=int(r["at"]),
                kind=str(r["kind"]),
                payload=json.loads(r["payload"]),
                chained_hash=str(r["chained_hash"]),
            )
            for r in self._rows(
                f"SELECT {', '.join(RUN_EVENT_COLUMNS)} FROM run_events "
                "WHERE run_id = ? ORDER BY seq",
                (run_id,),
            )
        ]

    def _usage_node(self, row: RunRow | None) -> str | None:
        if row is None:
            return None
        if row.owner is not None and row.owner.startswith(NODE_PREFIX):
            node = row.owner.removeprefix(NODE_PREFIX)
            if node and not re.search(r"[\x00-\x1f\x7f]", node):
                return node
        if row.group_name is not None:
            binding = self._one(
                "SELECT node FROM admission_bindings WHERE group_name = ? AND project = ?",
                (row.group_name, row.worktree),
            )
            if binding is not None:
                return str(binding["node"]).removeprefix(NODE_PREFIX)
        return None

    def usage_show(self, time_zone: str, *, now: int | None = None) -> dict[str, Any]:
        """Read retained counters for 30 local calendar days."""
        zone = ZoneInfo(time_zone)
        observed = now_ms() if now is None else now
        today = datetime.fromtimestamp(observed / 1000, zone).date()
        dates = [
            (today - timedelta(days=29 - index)).isoformat() for index in range(30)
        ]
        start = int(
            (
                datetime.fromtimestamp(observed / 1000, zone).replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
                - timedelta(days=29)
            ).timestamp()
            * 1000
        )
        rows = self._rows(
            "WITH history AS NOT MATERIALIZED ("
            "SELECT run_id, seq, at, kind, payload, "
            "COALESCE(json_extract(payload, '$.usage_identity'), "
            "json_extract(payload, '$.node_id')) AS node FROM run_events WHERE at <= ? "
            "AND (kind = 'usage' OR (kind = 'lifecycle' AND "
            "(json_type(payload, '$.usage_identity') = 'text' OR "
            "json_type(payload, '$.usage_pending') = 'text')))), "
            "coverage AS (SELECT node, MIN(at) AS at, MAX(at) AS latest "
            "FROM history GROUP BY node ORDER BY node IS NOT NULL, latest DESC, node LIMIT ?), "
            "recent AS (SELECT h.run_id, h.at, h.kind, h.payload, EXISTS("
            "SELECT 1 FROM run_events r WHERE r.run_id=h.run_id AND r.kind='lifecycle' "
            "AND json_extract(r.payload, '$.usage_managed')=1) AS managed "
            "FROM history h WHERE h.at >= ? AND (h.kind='usage' OR "
            "json_type(h.payload, '$.usage_pending')='text') "
            "ORDER BY h.at DESC, h.run_id, h.seq DESC LIMIT ?) "
            "SELECT '' AS run_id, at, 'coverage' AS kind, "
            "json_object('node_id', node) AS payload, 0 AS managed FROM coverage "
            "UNION ALL SELECT run_id, at, kind, payload, managed FROM recent",
            (observed, USAGE_AGENT_CAP + 2, start, USAGE_EVENT_CAP + 1),
        )
        coverage_rows = [row for row in rows if row["kind"] == "coverage"]
        recent_rows = [row for row in rows if row["kind"] != "coverage"]
        incomplete = len(recent_rows) > USAGE_EVENT_CAP
        rows = coverage_rows + recent_rows[:USAGE_EVENT_CAP]
        events = [
            (
                str(row["run_id"]),
                int(row["at"]),
                str(row["kind"]),
                json.loads(row["payload"]),
            )
            for row in rows
        ]
        identities: dict[str, str] = {}
        managed = {str(row["run_id"]) for row in rows if row["managed"]}
        coverage: dict[str | None, str] = {}
        global_start: str | None = None
        for run_id, at, kind, payload in events:
            if not isinstance(payload, dict):
                continue
            day = datetime.fromtimestamp(at / 1000, zone).date().isoformat()
            node = (
                payload.get("usage_identity")
                if kind == "lifecycle"
                else payload.get("node_id")
            )
            if isinstance(node, str):
                if kind != "coverage":
                    identities[run_id] = node
                coverage[node] = min(coverage.get(node, day), day)
            elif kind in {"usage", "coverage"} or "usage_pending" in payload:
                coverage[None] = min(coverage.get(None, day), day)
            if (
                payload.get("usage_managed") is True
                or payload.get("source") == "managed"
            ):
                managed.add(run_id)
            global_start = day if global_start is None else min(global_start, day)
        nodes = [node for node in coverage if node is not None]
        if len(nodes) > USAGE_AGENT_CAP:
            incomplete = True
        nodes = nodes[:USAGE_AGENT_CAP]
        included = set(nodes)
        amounts: dict[str | None, list[int]] = {
            node: [0] * 30 for node in [*nodes, None]
        }
        unknown: dict[str | None, set[int]] = {node: set() for node in amounts}
        completed = {
            (run_id, payload.get("observation_key", "exec"))
            for run_id, _, kind, payload in events
            if kind == "usage"
            and isinstance(payload, dict)
            and (
                isinstance(payload.get("observation_key"), str)
                or payload.get("source") != "managed"
            )
        }
        for run_id, at, kind, payload in events:
            if (
                kind != "lifecycle"
                or not isinstance(payload, dict)
                or "usage_pending" not in payload
            ):
                continue
            node = identities.get(run_id)
            day = datetime.fromtimestamp(at / 1000, zone).date().isoformat()
            if (
                node in amounts
                and day in dates
                and (run_id, payload["usage_pending"]) not in completed
            ):
                unknown[node].add(dates.index(day))
        total = 0
        seen: set[tuple[str, str]] = set()
        for run_id, at, kind, payload in events:
            if kind != "usage" or not isinstance(payload, dict):
                continue
            source = payload.get("source")
            if run_id in managed and source != "managed":
                continue
            node = identities.get(run_id)
            if node is not None and node not in included:
                continue
            day = datetime.fromtimestamp(at / 1000, zone).date().isoformat()
            if day not in dates:
                continue
            index = dates.index(day)
            amount = token_sum(
                (payload.get("input_tokens"), payload.get("output_tokens"))
            )
            if source != "managed" and amount == 0:
                amount = None
            key = payload.get("observation_key")
            if not isinstance(key, str):
                key = json.dumps(
                    [payload.get("input_tokens"), payload.get("output_tokens")],
                    sort_keys=True,
                )
            identity = (run_id, key)
            if identity in seen:
                continue
            seen.add(identity)
            if amount is None or total + amount > SAFE_TOKEN_INTEGER:
                unknown[node].add(index)
                continue
            total += amount
            amounts[node][index] += amount

        def days_for(node: str | None) -> list[dict[str, Any]]:
            start = coverage.get(node)
            return [
                {
                    "day": day,
                    "recordedTokens": amounts[node][index]
                    if start is not None and day >= start
                    else None,
                    "state": "not_recorded"
                    if start is None or day < start
                    else "partial"
                    if incomplete or index in unknown[node]
                    else "recorded",
                }
                for index, day in enumerate(dates)
            ]

        def series(node: str | None) -> dict[str, Any]:
            days = days_for(node)
            covered = [day for day in days if day["recordedTokens"] is not None]
            return {
                "days": days,
                "recordedTokens": sum(day["recordedTokens"] for day in covered)
                if covered
                else None,
                "state": "not_recorded"
                if not covered
                else "partial"
                if any(day["state"] == "partial" for day in covered)
                else "recorded",
            }

        agents = [{"nodeId": node, **series(node)} for node in sorted(nodes)]
        unassigned = series(None)
        all_series = [*agents, unassigned]
        days = [
            {
                "day": day,
                "recordedTokens": sum(
                    item["days"][index]["recordedTokens"] or 0 for item in all_series
                )
                if global_start is not None and day >= global_start
                else None,
                "state": "not_recorded"
                if global_start is None or day < global_start
                else "partial"
                if incomplete
                or any(item["days"][index]["state"] == "partial" for item in all_series)
                else "recorded",
            }
            for index, day in enumerate(dates)
        ]
        return {
            "version": 1,
            "observedAt": observed,
            "timeZone": time_zone,
            "incomplete": incomplete,
            "days": days,
            "agents": agents,
            "unassigned": unassigned,
        }

    def verify_run_event_chain(self, run_id: str) -> bool:
        """Recomputes the entire run chain and checks if it closes."""
        previous_hash = GENESIS_HASH
        for expected_seq, event in enumerate(self.read_run_events(run_id), 1):
            if event.seq != expected_seq:
                return False
            if compute_chained_hash(previous_hash, event.canonical_line()) != (
                event.chained_hash
            ):
                return False
            previous_hash = event.chained_hash
        return True

    def writer_scope(self, run_id: str) -> tuple[str, str] | None:
        """The (unit, cgroup) a native write run recorded before its launch."""
        try:
            scopes = [
                event.payload["writer_scope"]
                for event in self.read_run_events(run_id)
                if event.kind == "lifecycle"
                and isinstance(event.payload, dict)
                and "writer_scope" in event.payload
            ]
        except (*STORE_ERRORS, ValueError) as error:
            raise RegistryError(
                f"run {run_id!r} events could not be read: {error}"
            ) from error
        if not scopes:
            return None
        if (
            len(scopes) != 1
            or not isinstance(scopes[0], dict)
            or not isinstance(unit := scopes[0].get("unit"), str)
            or not isinstance(cgroup := scopes[0].get("cgroup"), str)
        ):
            raise RegistryError(f"run {run_id!r} carries a malformed writer scope")
        return unit, cgroup

    def effort_profile(self, run_id: str) -> EffortProfile:
        """The effort profile its admission recorded, read from the journal alone.

        Never recomputed from a roster and never repaired: no record is the
        unknown profile, and a malformed, misplaced or second record is refused
        rather than picked from.
        """
        try:
            if not self.verify_run_event_chain(run_id):
                raise RegistryError(f"run {run_id!r} event chain is invalid")
            records = [
                (event.kind, event.payload)
                for event in self.read_run_events(run_id)
                if isinstance(event.payload, dict) and "effort_profile" in event.payload
            ]
        except (*STORE_ERRORS, ValueError) as error:
            raise RegistryError(
                f"run {run_id!r} events could not be read: {error}"
            ) from error
        if not records:
            return UNKNOWN_EFFORT
        kind, payload = records[0]
        if (
            len(records) != 1
            or kind != "lifecycle"
            or set(payload) != {"effort_profile"}
        ):
            raise RegistryError(f"run {run_id!r} carries a malformed effort record")
        try:
            record = _effort_record(payload["effort_profile"])
        except RegistryError as error:
            raise RegistryError(
                f"run {run_id!r} carries a malformed effort record: {error}"
            ) from error
        return EffortProfile(
            requested=record["requested"],
            requested_source=EFFORT_FROM_ADMISSION,
            sent=record["sent"],
            sent_source=EFFORT_FROM_ADMISSION,
        )

    def orchestrator_binding(self, run_id: str) -> dict[str, Any] | None:
        """The complete binding this run was admitted with, or None for an unbound
        run — which every run but an orchestrator turn is. Read on every native
        resume: whether an origin was bound is this answer, never the request's."""
        try:
            bindings = [
                event.payload["orchestrator_binding"]
                for event in self.read_run_events(run_id)
                if event.kind == "lifecycle"
                and isinstance(event.payload, dict)
                and "orchestrator_binding" in event.payload
            ]
        except (*STORE_ERRORS, ValueError) as error:
            raise RegistryError(
                f"run {run_id!r} events could not be read: {error}"
            ) from error
        if not bindings:
            return None
        if len(bindings) != 1:
            raise RegistryError(
                f"run {run_id!r} carries more than one orchestrator binding"
            )
        return validated_orchestrator_binding(bindings[0])

    def inference_profile(self, run_id: str) -> dict[str, Any] | None:
        """The immutable zero-tool schema, recorded atomically with admission."""
        profiles = [
            event.payload["inference_profile"]
            for event in self.read_run_events(run_id)
            if event.kind == "lifecycle"
            and isinstance(event.payload, dict)
            and "inference_profile" in event.payload
        ]
        if not profiles:
            return None
        if len(profiles) != 1 or not isinstance(profiles[0], dict):
            raise RegistryError("run carries an invalid inference profile record")
        try:
            return StructuredInference.from_request(profiles[0]).data()
        except ValueError as error:
            raise RegistryError("run carries an invalid inference profile") from error

    def session_profile(self, run_id: str) -> SessionProfile | None:
        """One verified original conversation profile, never a legacy guess."""
        records = [
            event.payload["session_profile"]
            for event in self.read_run_events(run_id)
            if event.kind == "lifecycle"
            and isinstance(event.payload, dict)
            and "session_profile" in event.payload
        ]
        if not records:
            return None
        if len(records) != 1:
            raise RegistryError("run carries multiple session profiles")
        try:
            return SessionProfile.from_record(records[0])
        except (TypeError, ValueError) as error:
            raise RegistryError("run carries an invalid session profile") from error

    def session_activation(self, run_id: str) -> SessionActivation | None:
        records = [
            event.payload["activation"]
            for event in self.read_run_events(run_id)
            if isinstance(event.payload, dict) and "activation" in event.payload
        ]
        if not records:
            return None
        if len(records) != 1:
            raise RegistryError("native session activation is ambiguous")
        return SessionActivation.from_request(records[0])

    def session_transport(self, run_id: str) -> dict[str, str] | None:
        records = [
            event.payload["session_transport"]
            for event in self.read_run_events(run_id)
            if isinstance(event.payload, dict) and "session_transport" in event.payload
        ]
        if not records:
            return None
        if (
            len(records) != 1
            or not isinstance(records[0], dict)
            or set(records[0]) != {"path", "control"}
            or any(not isinstance(value, str) for value in records[0].values())
        ):
            raise RegistryError("native session transport is ambiguous")
        return records[0]

    def session_exit_code(self, run_id: str) -> int | None:
        records = [
            event.payload["session_exit"]
            for event in self.read_run_events(run_id)
            if isinstance(event.payload, dict) and "session_exit" in event.payload
        ]
        if not records:
            return None
        if (
            len(records) != 1
            or not isinstance(records[0], int)
            or isinstance(records[0], bool)
        ):
            raise RegistryError("native session exit is ambiguous")
        return records[0]

    def session_runs(self, run_id: str, *, limit: int = 100) -> tuple[RunRow, ...]:
        """A bounded original conversation's attempts, newest first."""
        row = self.get_run(run_id)
        if row is None:
            raise UnknownRun("no original run exists")
        profile = self.session_profile(run_id)
        if profile is None or row.thread_id is None:
            return (row,)
        rows = self._rows(
            # A Claude conversation's attempts may span the accounts a
            # subscription wall moved it across; codex/agy stay account-bound.
            f"SELECT {', '.join(RUN_COLUMNS)} FROM runs WHERE thread_id = ? "
            "AND backend = ? AND executed_provider IS ? "
            "AND (backend NOT IN ('codex', 'agy') OR executed_account IS ?) "
            "AND owner IS ? ORDER BY created_at DESC, run_id DESC LIMIT ?",
            (
                row.thread_id,
                row.backend.value,
                row.executed_provider,
                row.executed_account,
                row.owner,
                limit,
            ),
        )
        return tuple(
            candidate
            for record in rows
            if (candidate := _row_from_record(record))
            and (retained := self.session_profile(candidate.run_id)) is not None
            and retained.original_run_id == profile.original_run_id
        )

    def session_reference(self, row: RunRow) -> SessionReference | None:
        """The bounded actual attempts of one recorded origin, without launching."""
        profile = self.session_profile(row.run_id)
        if profile is None:
            if (
                row.backend is not Backend.COMMAND
                and self.session_activation(row.run_id) is None
            ):
                return None
            # ACP continuation uses the custodian's framed operation port.
            return SessionReference(
                original_run_id=row.run_id,
                run_ids=(row.run_id,),
                cwd=row.worktree,
                model=row.model,
                can_prompt=False,
                owner=row.owner,
                active=not row.phase.is_terminal(),
            )
        attempts = self.session_runs(row.run_id)
        latest = attempts[0] if attempts else row
        return SessionReference(
            original_run_id=profile.original_run_id,
            run_ids=tuple(attempt.run_id for attempt in reversed(attempts)),
            cwd=row.worktree,
            model=row.model,
            can_prompt=latest.phase is RunPhase.CLOSED
            and row.thread_id is not None
            and (row.owner is None or profile.timeout_ms > 0)
            and (profile.config_dir is None or Path(profile.config_dir).is_dir()),
            owner=row.owner,
            active=not latest.phase.is_terminal(),
        )

    def inference_origin(self, thread_id: str) -> RunRow | None:
        """A recorded Claude session, used to retain its profile and account."""
        _validate_required("thread_id", thread_id, MAX_DURABLE_IDENTIFIER_BYTES)
        found = self._one(
            f"SELECT {', '.join(RUN_COLUMNS)} FROM runs "
            "WHERE thread_id = ? AND backend = 'claude' "
            "ORDER BY created_at DESC, run_id DESC LIMIT 1",
            (thread_id,),
        )
        return None if found is None else _row_from_record(found)

    def inference_home(self, run_id: str) -> InferenceHome | None:
        """The actual launch's original native home, immutable across resume."""
        records = [
            event.payload["inference_home"]
            for event in self.read_run_events(run_id)
            if event.kind == "lifecycle"
            and isinstance(event.payload, dict)
            and "inference_home" in event.payload
        ]
        if not records:
            return None
        if len(records) != 1:
            raise RegistryError("run carries multiple native inference homes")
        try:
            return InferenceHome.from_record(records[0])
        except ValueError as error:
            raise RegistryError(
                "run carries an invalid native inference home"
            ) from error

    # -- lifecycle ---------------------------------------------------------

    def connect(self) -> Self:
        """Open the existing registry file, or refuse. Nothing is created here:
        `create_store` (`nspawn database create`) is the only way a file
        comes into existence."""
        if self._connection is None:
            try:
                connection = nott_database.connect(
                    self.path,
                    allow_live_store=self.allow_live_store,
                    check_same_thread=False,
                )
            except STORE_ERRORS as exc:
                raise RegistryError(
                    f"the registry at {self.path} could not be opened: {exc}"
                ) from exc
            connection.row_factory = nott_database.Row
            self._connection = connection
        return self

    def reconnect(self) -> None:
        """Drop the connection and open a new one."""
        self.close()
        self.connect()

    def close(self) -> None:
        with self._serial:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def __enter__(self) -> Self:
        return self.connect()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    @property
    def connection(self) -> nott_database.Connection:
        if self._connection is None:
            raise RegistryError("the registry is not connected; call connect() first")
        return self._connection

    def open(self, now: int) -> int:
        """Refuse a file of another schema version, and register this instance."""
        version = self.upgrade()
        self.register_instance(now)
        return version

    def upgrade(self) -> int:
        """Bring an older version to this one in place, or refuse any other."""
        with self._serial:
            version = nott_database.user_version(self.connection)
        if version in (1, 2, 3, 4, 5):
            # Additive steps: version 2 adds `class_observations`, 3 `key_fences`,
            # 4 `owner_stamps`, 5 the run owner and notice generation, 6 admission
            # controls and first-terminal receipts. Read
            # again under the write lock, so two openers upgrade once.
            with self._transaction():
                version = nott_database.user_version(self.connection)
                if version == 1:
                    self._run(CLASS_OBSERVATIONS_DDL)
                    self._run(CLASS_OUTCOME_TRIGGER_DDL)
                if version in (1, 2):
                    self._run(KEY_FENCES_DDL)
                    self._run(KEY_FENCE_TRIGGER_DDL)
                if version in (1, 2, 3):
                    self._run(OWNER_STAMPS_DDL)
                if version in (1, 2, 3, 4):
                    self._add_column("runs", RUN_OWNER_COLUMN)
                    self._add_column("notices", NOTICE_GENERATION_COLUMN)
                    self._run("DROP TRIGGER IF EXISTS runs_arm_notices")
                    self._run(NOTICE_ARM_TRIGGER_DDL)
                if version in (1, 2, 3, 4, 5):
                    # Table/index DDL only: no executescript (which commits the
                    # caller's transaction before executing its statements).
                    for statement in ADMISSION_TABLES_DDL.split(";"):
                        if statement.strip():
                            self._run(statement)
                    self._run(ADMISSION_TRIGGER_DDL)
                    self._run(TERMINAL_RECEIPT_TRIGGER_DDL)
                    self._run("PRAGMA user_version = 6")
                version = nott_database.user_version(self.connection)
        if version == 6:
            version = self._backend_upgrade()
        if version != REGISTRY_SCHEMA_VERSION:
            raise UnrecognizedSchema(
                f"the registry at {self.path} carries schema version {version}; "
                f"this build opens only {REGISTRY_SCHEMA_VERSION}"
            )
        with self._transaction():
            index = self._one(
                "SELECT sql FROM sqlite_schema WHERE name = 'runs_legacy_thread_key'"
            )
            if (
                index is None
                or str(index["sql"]).split() != LEGACY_THREAD_INDEX_DDL.split()
            ):
                self._run("DROP INDEX IF EXISTS runs_legacy_thread_key")
                self._run(LEGACY_THREAD_INDEX_DDL)
            self._run(THREAD_ORIGIN_INDEX_DDL)
            self._run(COMPUTE_BALANCE_DDL)
        return version

    def _backend_upgrade(self) -> int:
        """Extend the backend CHECK in one transaction. Preserve rows and their guards."""
        with self._serial:
            self._run("PRAGMA foreign_keys = OFF")
            try:
                with self._transaction():
                    version = nott_database.user_version(self.connection)
                    if version != 6:
                        return version
                    table = self._one(
                        "SELECT sql FROM sqlite_schema WHERE name = 'runs'"
                    )
                    if table is None:
                        raise RegistryError("The run table is missing during upgrade.")
                    backends = ",".join(f"'{backend.value}'" for backend in Backend)
                    definition, count = re.subn(
                        r"backend TEXT NOT NULL CHECK \(backend IN \([^)]*\)\)",
                        f"backend TEXT NOT NULL CHECK (backend IN ({backends}))",
                        str(table["sql"]),
                    )
                    if count != 1:
                        raise RegistryError(
                            "The run backend constraint is not recognized."
                        )
                    guards = self._rows(
                        "SELECT name, type, sql FROM sqlite_schema WHERE sql IS NOT NULL "
                        "AND (type = 'trigger' OR (type = 'index' AND tbl_name = 'runs'))"
                    )
                    for guard in guards:
                        if guard["type"] == "trigger":
                            name = str(guard["name"]).replace('"', '""')
                            self._run(f'DROP TRIGGER "{name}"')
                    replacement, renamed = re.subn(
                        r'^CREATE TABLE (?:"runs"|runs)(?=\s|\()',
                        "CREATE TABLE runs_next",
                        definition,
                    )
                    if renamed != 1:
                        raise RegistryError(
                            "The run table definition is not recognized."
                        )
                    self._run(replacement)
                    self._run("INSERT INTO runs_next SELECT * FROM runs")
                    self._run("DROP TABLE runs")
                    self._run("ALTER TABLE runs_next RENAME TO runs")
                    for guard in guards:
                        self._run(str(guard["sql"]))
                    if self._rows("PRAGMA foreign_key_check"):
                        raise RegistryError(
                            "The run upgrade failed its reference check."
                        )
                    self._run(f"PRAGMA user_version = {REGISTRY_SCHEMA_VERSION}")
                    return REGISTRY_SCHEMA_VERSION
            finally:
                self._run("PRAGMA foreign_keys = ON")

    # -- coordinator admission controls -----------------------------------

    def admission_stop(self, node: str, generation: int) -> dict[str, Any]:
        """Commit a monotonic node fence and read its still-held groups back."""
        _admission_validate("stop", node, "/", "stop", generation)
        with self._transaction():
            self._run(
                "INSERT INTO node_holds(node, generation, stopped_at) VALUES (?, ?, ?) "
                "ON CONFLICT (node) DO UPDATE SET generation = excluded.generation, "
                "stopped_at = excluded.stopped_at WHERE excluded.generation > node_holds.generation",
                (node, generation, now_ms()),
            )
            current = self._generation_show(node)
            groups = self._rows(
                "SELECT group_name FROM admission_bindings "
                "WHERE node = ? AND released_generation < ? ORDER BY group_name",
                (node, current),
            )
            return {
                "node": node,
                "generation": current,
                "groups": [r["group_name"] for r in groups],
            }

    def _generation_show(self, node: str) -> int:
        found = self._one("SELECT generation FROM node_holds WHERE node = ?", (node,))
        return 0 if found is None else int(found["generation"])

    def _binding_require(
        self, group: str, node: str, project: str, decision: str, generation: int
    ) -> None:
        """Reuse the exact identity, or create it before any run joins the group.

        The caller holds the write transaction. A repeated bind never releases a
        hold, even when it carries a newer approval generation.
        """
        found = self._one(
            "SELECT node, project, decision FROM admission_bindings WHERE group_name = ?",
            (group,),
        )
        if found is not None:
            if (found["node"], found["project"], found["decision"]) != (
                node,
                project,
                decision,
            ):
                raise RegistryError(
                    "admission binding identity differs from the stored node, project or decision"
                )
            return
        if (
            self._one("SELECT 1 FROM runs WHERE group_name = ? LIMIT 1", (group,))
            is not None
        ):
            raise RegistryError(
                "first admission bind refused: the group already holds runs"
            )
        self._run(
            "INSERT INTO admission_bindings(group_name, node, project, decision, "
            "released_generation, bound_at) VALUES (?, ?, ?, ?, ?, ?)",
            (group, node, project, decision, generation, now_ms()),
        )

    def admission_bind(
        self,
        group: str,
        node: str,
        project: str,
        decision: str,
        generation: int,
        key: str,
        fingerprint: str,
    ) -> dict[str, Any]:
        """Reserve this request's key atomically with its immutable workflow binding."""
        _admission_validate(group, node, project, decision, generation)
        _validate_required("admission key", key, MAX_DURABLE_IDENTIFIER_BYTES)
        if not key.strip() or re.search(r"[\x00-\x1f\x7f]", key):
            raise RegistryError("admission key must be nonblank text without controls")
        if re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None:
            raise RegistryError("admission fingerprint must be a sha256 hex digest")
        with self._transaction():
            if generation > self._generation_show(node):
                raise RegistryError(
                    "approval generation exceeds the current Stop generation"
                )
            self._binding_require(group, node, project, decision, generation)
            reserved = self._one(
                "SELECT group_name, fingerprint FROM admission_keys WHERE project = ? AND idempotency_key = ?",
                (project, key),
            )
            if reserved is not None:
                if (reserved["group_name"], reserved["fingerprint"]) != (
                    group,
                    fingerprint,
                ):
                    raise RegistryError(
                        "admission key is already reserved for another group or fingerprint"
                    )
            else:
                self._run(
                    "INSERT INTO admission_keys(project, idempotency_key, group_name, fingerprint) VALUES (?, ?, ?, ?)",
                    (project, key, group, fingerprint),
                )
            return self._binding_show(group)

    def admission_resume(
        self, group: str, node: str, project: str, decision: str, generation: int
    ) -> dict[str, Any]:
        """Release only this binding at the node's exact current Stop generation."""
        _admission_validate(group, node, project, decision, generation)
        with self._transaction():
            if generation != self._generation_show(node):
                raise RegistryError(
                    "resume requires the node's current Stop generation"
                )
            self._binding_require(group, node, project, decision, generation)
            self._run(
                "UPDATE admission_bindings SET released_generation = ? WHERE group_name = ?",
                (generation, group),
            )
            return self._binding_show(group)

    def _binding_show(self, group: str) -> dict[str, Any]:
        found = self._one(
            "SELECT b.*, coalesce(h.generation, 0) AS node_generation "
            "FROM admission_bindings b LEFT JOIN node_holds h ON h.node = b.node WHERE b.group_name = ?",
            (group,),
        )
        if found is None:
            raise RegistryError(f"admission group {group!r} is unbound")
        return {
            "group": found["group_name"],
            "node": found["node"],
            "project": found["project"],
            "decision": found["decision"],
            "releasedGeneration": found["released_generation"],
            "nodeGeneration": found["node_generation"],
            "held": found["node_generation"] > found["released_generation"],
        }

    def admission_close(self, group: str) -> tuple[bool, list[str]]:
        """In one read, select reserved non-terminal runs only while the group is held.

        True means held, False released. An unbound group refuses. A completion
        after selection is still the first terminal transition if it beats close.
        """
        _validate_required("admission group", group, MAX_DURABLE_IDENTIFIER_BYTES)
        found = self._rows(
            "SELECT coalesce(h.generation, 0) > b.released_generation AS held, r.run_id "
            "FROM admission_bindings b LEFT JOIN node_holds h ON h.node = b.node "
            "LEFT JOIN admission_keys a ON a.group_name = b.group_name AND a.project = b.project "
            "LEFT JOIN run_keys k ON k.project = a.project AND k.idempotency_key = a.idempotency_key "
            "AND k.fingerprint = a.fingerprint "
            "LEFT JOIN runs r ON r.run_id = k.run_id AND r.group_name = b.group_name "
            f"AND r.phase NOT IN ({TERMINAL_PHASE_SQL}) "
            "AND coalesce(h.generation, 0) > b.released_generation "
            "WHERE b.group_name = ? ORDER BY r.created_at, r.run_id",
            (group,),
        )
        if not found:
            raise RegistryError(f"admission group {group!r} is unbound")
        return bool(found[0]["held"]), [
            r["run_id"] for r in found if r["run_id"] is not None
        ]

    def admission_show(self, run_id: str) -> dict[str, Any] | None:
        """Observed binding, only when this run's actual key and fingerprint match."""
        found = self._one(
            "SELECT b.group_name, b.node, b.released_generation, "
            "coalesce(h.generation, 0) AS node_generation FROM runs r "
            "JOIN run_keys k ON k.run_id = r.run_id "
            "JOIN admission_keys a ON a.project = k.project AND a.idempotency_key = k.idempotency_key "
            "AND a.fingerprint = k.fingerprint AND a.group_name = r.group_name "
            "JOIN admission_bindings b ON b.group_name = a.group_name AND b.project = k.project "
            "LEFT JOIN node_holds h ON h.node = b.node WHERE r.run_id = ?",
            (run_id,),
        )
        return (
            None
            if found is None
            else {
                "group": found["group_name"],
                "node": found["node"],
                "releasedGeneration": found["released_generation"],
                "nodeGeneration": found["node_generation"],
            }
        )

    def terminal_receipt_show(self, run_id: str) -> dict[str, Any] | None:
        """The first observed terminal transition, retained even after run pruning."""
        found = self._one(
            "SELECT phase, reason, at FROM terminal_receipts WHERE run_id = ?",
            (run_id,),
        )
        return None if found is None else dict(found)

    def database_rollback(self) -> int:
        """Restore version 5 only when its reader can read every retained run."""
        with self._transaction():
            version = nott_database.user_version(self.connection)
            if version not in (5, 6, 7):
                raise UnrecognizedSchema(
                    f"Database rollback requires schema 5, 6, or 7. Got {version}."
                )
            newer = self._one(
                "SELECT backend FROM runs WHERE backend = 'command' LIMIT 1"
            )
            if newer is not None:
                raise RegistryError(
                    f"Database rollback refused. A retained run uses {newer['backend']}."
                )
            live = self._one(
                "SELECT r.run_id FROM runs r JOIN run_keys k ON k.run_id = r.run_id "
                "JOIN admission_keys a ON a.project = k.project AND a.idempotency_key = k.idempotency_key "
                f"WHERE r.phase NOT IN ({TERMINAL_PHASE_SQL}) LIMIT 1"
            )
            if live is not None:
                raise RegistryError(
                    f"database rollback refused: reserved run {live['run_id']!r} is non-terminal"
                )
            self._run("PRAGMA user_version = 5")
            return nott_database.user_version(self.connection)

    def _add_column(self, table: str, column: str) -> None:
        """Append `column` to `table`, unless a store set back to an older
        version for an older release still carries it."""
        name = column.split(maxsplit=1)[0]
        present = {
            str(row["name"]) for row in self._rows(f"PRAGMA table_info({table})")
        }
        if name not in present:
            self._run(f"ALTER TABLE {table} ADD COLUMN {column}")

    def register_instance(self, now: int) -> None:
        """Write this instance's row, whether or not one is already there.

        Called by `open` at boot AND by the reaper when `heartbeat_instance`
        answers that the row is gone: a peer sweep DELETES a stale instance,
        and an UPDATE against a deleted row heals nothing. Without this, one
        missed 30 s window deregistered a supervisor permanently and every run
        it opened afterwards was orphaned within a tick.
        """
        self._write(
            "INSERT INTO instances(instance_id, started_at, heartbeat_at) "
            "VALUES (?, ?, ?) ON CONFLICT (instance_id) DO UPDATE SET "
            "started_at = excluded.started_at, heartbeat_at = excluded.heartbeat_at",
            (self.instance_id, now, now),
        )

    # -- the instance row --------------------------------------------------

    def heartbeat_instance(self, now: int) -> bool:
        """Refresh this instance's heartbeat. False when the row is gone."""
        return (
            self._write(
                "UPDATE instances SET heartbeat_at = ? WHERE instance_id = ?",
                (now, self.instance_id),
            )
            == 1
        )

    def remove_instance(self) -> None:
        """Drop this instance's row.

        The runs it owned SURVIVE: `owner_instance` carries no foreign key, so a
        supervisor leaving does not delete the history of what it ran.
        """
        self._write("DELETE FROM instances WHERE instance_id = ?", (self.instance_id,))

    # -- instance kinds, launch reservations, close control -----------------

    def register_sidecar_instance(self, kind: str, now: int) -> None:
        """Upsert this instance's kind (`stdio` or `driver`) and heartbeat."""
        self._write(
            "INSERT INTO sidecar_instances(instance_id, kind, heartbeat_at) "
            "VALUES (?, ?, ?) ON CONFLICT (instance_id) DO UPDATE SET "
            "kind = excluded.kind, heartbeat_at = excluded.heartbeat_at",
            (self.instance_id, kind, now),
        )

    def remove_sidecar_instance(self) -> None:
        self._write(
            "DELETE FROM sidecar_instances WHERE instance_id = ?", (self.instance_id,)
        )

    def sweep_sidecar_instances(self, now: int, stale_after_ms: int) -> int:
        """Drop kind rows whose heartbeat stopped, as the instances sweep does."""
        return self._write(
            "DELETE FROM sidecar_instances WHERE heartbeat_at < ?",
            (now - stale_after_ms,),
        )

    def reserve_launches(
        self,
        launch_uuids: list[str],
        now: int,
        *,
        ceiling: int,
        stale_after_ms: int,
        lapse_ms: int,
    ) -> None:
        """Reserve one driver slot per launch, as a group, or refuse them all.

        Counted inside one write transaction: live reservations (a reservation
        lapses after the ack deadline) plus driver instances with a fresh
        heartbeat.
        """
        reserved_at = now_ms()
        with self._transaction():
            self._run(
                "DELETE FROM launch_reservations WHERE reserved_at < ?",
                (reserved_at - lapse_ms,),
            )
            row = self._one(
                "SELECT (SELECT count(*) FROM launch_reservations) "
                "+ (SELECT count(*) FROM sidecar_instances "
                "   WHERE kind = 'driver' AND heartbeat_at > ?) AS live",
                (now - stale_after_ms,),
            )
            live = 0 if row is None else int(row["live"])
            if live + len(launch_uuids) > ceiling:
                raise DriverCeiling(
                    f"{live} drivers are live or reserved; {len(launch_uuids)} more "
                    f"would pass the ceiling of {ceiling}"
                )
            for launch_uuid in launch_uuids:
                self._run(
                    "INSERT INTO launch_reservations(launch_uuid, reserved_at) "
                    "VALUES (?, ?)",
                    (launch_uuid, reserved_at),
                )

    def release_launch(self, launch_uuid: str) -> None:
        self._write(
            "DELETE FROM launch_reservations WHERE launch_uuid = ?", (launch_uuid,)
        )

    def run_custody(
        self, run_id: str, now: int, stale_after_ms: int
    ) -> tuple[str, str | None, bool] | None:
        """(owner instance, its kind, whether it heartbeats), or None."""
        row = self._one(
            "SELECT r.owner_instance, s.kind, i.heartbeat_at FROM runs r "
            "LEFT JOIN sidecar_instances s ON s.instance_id = r.owner_instance "
            "LEFT JOIN instances i ON i.instance_id = r.owner_instance "
            "WHERE r.run_id = ?",
            (run_id,),
        )
        if row is None:
            return None
        heartbeat = row["heartbeat_at"]
        fresh = heartbeat is not None and int(heartbeat) > now - stale_after_ms
        return str(row["owner_instance"]), row["kind"], fresh

    def request_close(self, run_id: str) -> None:
        """Ask the run's custody owner to close it; a pending request is joined."""
        self._write(
            "INSERT INTO run_control(run_id, close_requested_at) VALUES (:run, :now) "
            "ON CONFLICT (run_id) DO UPDATE SET "
            "close_requested_at = :now, claimed_at = NULL, claimed_by = NULL, "
            "outcome = NULL, outcome_at = NULL "
            "WHERE run_control.outcome IS NOT NULL "
            "OR run_control.close_requested_at IS NULL",
            {"run": run_id, "now": now_ms()},
        )

    def claim_close(
        self, run_id: str, now: int, stale_after_ms: int, lease_s: float
    ) -> str:
        """Take a pending close request: `won`, `held` by a live claimer, or `none`.

        A compare-and-set: the claim is free, older than its lease, or held by
        an instance that stopped heartbeating. The loser reads the outcome.
        Every driver polls this each second, so it reads first and writes only
        when a request is pending.
        """
        pending = self._one(
            "SELECT 1 FROM run_control WHERE run_id = ? "
            "AND close_requested_at IS NOT NULL AND outcome IS NULL",
            (run_id,),
        )
        if pending is None:
            return "none"
        clock = now_ms()
        with self._transaction():
            won = self._rows(
                "UPDATE run_control SET claimed_at = :clock, claimed_by = :me "
                "WHERE run_id = :run AND close_requested_at IS NOT NULL "
                "AND outcome IS NULL AND (claimed_at IS NULL "
                "  OR claimed_at < :lapsed "
                "  OR NOT EXISTS (SELECT 1 FROM instances i "
                "     WHERE i.instance_id = run_control.claimed_by "
                "       AND i.heartbeat_at > :fresh)) "
                "RETURNING run_id",
                {
                    "clock": clock,
                    "me": self.instance_id,
                    "run": run_id,
                    "lapsed": clock - int(lease_s * 1000),
                    "fresh": now - stale_after_ms,
                },
            )
            if won:
                return "won"
            still = self._one(
                "SELECT 1 FROM run_control WHERE run_id = ? "
                "AND close_requested_at IS NOT NULL AND outcome IS NULL",
                (run_id,),
            )
        return "held" if still is not None else "none"

    def record_close_outcome(self, run_id: str, outcome: dict[str, Any]) -> bool:
        return (
            self._write(
                "UPDATE run_control SET outcome = ?, outcome_at = ? "
                "WHERE run_id = ? AND claimed_by = ? AND outcome IS NULL",
                (json.dumps(outcome), now_ms(), run_id, self.instance_id),
            )
            == 1
        )

    def close_outcome(self, run_id: str) -> dict[str, Any] | None:
        row = self._one("SELECT outcome FROM run_control WHERE run_id = ?", (run_id,))
        if row is None or row["outcome"] is None:
            return None
        outcome: dict[str, Any] = json.loads(row["outcome"])
        return outcome

    # -- claims ------------------------------------------------------------

    def claim_worktree(
        self, worktree: str, run_id: str, now: int, lease_ms: int
    ) -> WorktreeClaim:
        """Take the one-writer claim on `worktree`, or refuse.

        Refused while the existing lease is live, and refused while the holding
        run still has protected custody even if its lease expired.
        """
        _validate_required("claims.worktree", worktree, MAX_DURABLE_PATH_BYTES)
        _validate_required("claims.run_id", run_id, MAX_DURABLE_IDENTIFIER_BYTES)
        _validate_required(
            "claims.holder", self.instance_id, MAX_DURABLE_IDENTIFIER_BYTES
        )
        with self._transaction():
            return self._claim_in_transaction(worktree, run_id, now, lease_ms)

    def _claim_in_transaction(
        self, worktree: str, run_id: str, now: int, lease_ms: int
    ) -> WorktreeClaim:
        """The claim itself, inside the caller's write: `admit_run` shares it."""
        existing = self._one(
            f"""SELECT claims.run_id, claims.holder, claims.lease_until,
                    EXISTS({PROTECTED_CUSTODY_SQL}) AS protected_custody
                FROM claims WHERE worktree = ?""",
            (worktree,),
        )
        if existing is not None and (
            existing["lease_until"] > now or existing["protected_custody"]
        ):
            raise WorktreeHeld(
                holder=existing["holder"],
                run_id=existing["run_id"],
                lease_until=existing["lease_until"],
            )
        lease_until = min(now + lease_ms, POISONED_LEASE_UNTIL)
        self._run(
            "INSERT INTO claims(worktree, run_id, holder, lease_until) "
            "VALUES (?, ?, ?, ?) ON CONFLICT (worktree) DO UPDATE SET "
            "run_id = excluded.run_id, holder = excluded.holder, "
            "lease_until = excluded.lease_until",
            (worktree, run_id, self.instance_id, lease_until),
        )
        return WorktreeClaim(
            worktree=worktree,
            run_id=run_id,
            holder=self.instance_id,
            lease_until=lease_until,
        )

    def release_claim(
        self, worktree: str, run_id: str, *, drained: bool = False
    ) -> bool:
        """Give up a claim, and report whether the ROW ACTUALLY WENT.

        TWO ARMS. This instance's own claim is released unconditionally — that is
        the holder clause, and it is what protects a worktree from being taken
        out from under a LIVE rival. A claim whose holder is no longer in
        `instances` is dead custody: any instance may release it, because
        otherwise a supervisor that crashed fences its worktree until a human
        acts, and that fence belongs to `poison_claim`, not to an ordinary death.

        The dead-holder arm is deliberately NARROWER than the owner's. It refuses
        while the run behind the claim still has protected custody — the same
        predicate `claim_worktree` refuses on, named once — and it NEVER touches a
        poisoned lease. A poisoned claim says a human must look; reading the
        holder's absence as permission would undo the exact fence somebody set.

        False means NO row was deleted, and it covers four different facts: a live
        foreign holder, dead custody whose run still holds its process, a poisoned
        lease, and no claim at all. It is the rowcount of THIS delete and of no
        other statement — the whole point of returning it.

        `drained` is the caller's proof that the run's writer slice was observed
        empty and stopped: nothing of the run can still write, so its claim goes
        whoever holds it and whatever poisoned it.
        """
        _validate_required("claims.worktree", worktree, MAX_DURABLE_PATH_BYTES)
        _validate_required("claims.run_id", run_id, MAX_DURABLE_IDENTIFIER_BYTES)
        with self._transaction():
            if self.implementation_recovery(run_id) is not None:
                raise RegistryError(
                    "pending implementation export requires reconciliation"
                )
            deleted = self._run(
                f"""DELETE FROM claims
                    WHERE worktree = :worktree AND run_id = :run_id
                      AND (
                          :drained
                          OR holder = :instance
                          OR (
                              NOT EXISTS (
                                  SELECT 1 FROM instances
                                  WHERE instances.instance_id = claims.holder
                              )
                              AND lease_until <> :poisoned
                              AND NOT EXISTS ({PROTECTED_CUSTODY_SQL})
                          )
                      )""",
                {
                    "worktree": worktree,
                    "run_id": run_id,
                    "instance": self.instance_id,
                    "poisoned": POISONED_LEASE_UNTIL,
                    "drained": drained,
                },
            )
        return deleted == 1

    def poison_claim(self, worktree: str, run_id: str) -> None:
        """Fence a worktree FOREVER, whoever holds it.

        Used when custody could not be established: the lease is pushed to the
        largest instant, so no `now` releases it and no other run can take the
        worktree until a human intervenes. Unlike release, this is deliberately
        NOT scoped to this holder.
        """
        _validate_required("claims.worktree", worktree, MAX_DURABLE_PATH_BYTES)
        _validate_required("claims.run_id", run_id, MAX_DURABLE_IDENTIFIER_BYTES)
        self._write(
            "UPDATE claims SET lease_until = ? WHERE worktree = ? AND run_id = ?",
            (POISONED_LEASE_UNTIL, worktree, run_id),
        )

    # -- runs --------------------------------------------------------------

    def _insert_row(self, row: RunRow) -> None:
        """Write every insertable column. The observation pair is NOT among them."""
        pid, boot_id, start_ticks = _identity_columns(row)
        values: dict[str, Any] = {
            "phase": row.phase.value,
            "backend": row.backend.as_str(),
            "process_pid": pid,
            "process_boot_id": boot_id,
            "process_start_ticks": start_ticks,
        }
        for column in INSERT_COLUMNS:
            if column not in values:
                values[column] = getattr(row, column)
        columns = ", ".join(INSERT_COLUMNS)
        placeholders = ", ".join(f":{column}" for column in INSERT_COLUMNS)
        self._run(f"INSERT INTO runs({columns}) VALUES ({placeholders})", values)

    def insert_run(self, row: RunRow) -> None:
        """Record a run. The observation pair is never written here."""
        with self._transaction():
            self._insert_row(row)

    def admit_run(
        self,
        row: RunRow,
        claim_worktree: bool,
        now: int,
        lease_ms: int,
        *,
        native_context: NativeSessionContext | None = None,
        notice: NoticeCaller | None = None,
        run_key: RunKey | None = None,
        class_observation: ClassObservation | None = None,
        effort_profile: dict[str, str | None] | None = None,
        orchestrator_binding: Mapping[str, Any] | None = None,
        inference_profile: dict[str, Any] | None = None,
        inference_home: InferenceHome | None = None,
        session_profile: SessionProfile | None = None,
        activation: Mapping[str, Any] | None = None,
    ) -> None:
        """Insert a PENDING run, optionally claim its worktree, and start it.

        All of it is ONE transaction: a run that could not take its worktree
        never reaches `spawning`, and a claim never survives a failed insert.
        With a `notice` caller its pending notices row is written in the same
        transaction, so no run exists whose caller the outbox does not know;
        its `run_key` row too, so a key never names a run that does not exist.
        With an `orchestrator_binding` its one lifecycle event is appended there
        too — no new kind, table or column — so a bound turn is never admitted
        without the session, profile and tool contract it was admitted FOR, and a
        binding never names a run that does not exist.
        A second run for one (project, key) is refused by `run_keys_project_key`,
        and any unique index is refused as `UniqueTaken` naming it.

        An `effort_profile` (`{requested, sent}`) is checked before anything is
        written and appended as the run's lifecycle event in the same
        transaction: no run exists without the record its admission was given,
        and no record without its run.
        """
        record = None if effort_profile is None else _effort_record(effort_profile)
        selected = (
            None
            if activation is None
            else SessionActivation.from_request(dict(activation)).data()
        )
        inference = (
            None
            if inference_profile is None
            else StructuredInference.from_request(inference_profile).data()
        )
        payload = (
            None
            if orchestrator_binding is None
            else _orchestrator_payload(orchestrator_binding)
        )
        if row.phase is not RunPhase.PENDING:
            raise InvalidField(
                f"runs.phase must be 'pending' to admit a run, got {row.phase.value!r}"
            )
        if row.backend in (Backend.CODEX, Backend.AGY):
            if native_context is None:
                raise RegistryError("native admission requires session context")
            if row.executed_provider is None or row.executed_account is None:
                raise RegistryError("native admission requires provider and account")
        elif (
            native_context is not None
            and session_profile is None
            and activation is None
        ):
            raise RegistryError("session context requires a native backend")
        connection = self.connection
        try:
            with self._transaction():
                self._insert_row(row)
                node = self._usage_node(row)
                if node is not None:
                    self._append_run_event(
                        row.run_id,
                        at=now,
                        kind="lifecycle",
                        payload={
                            "usage_identity": node,
                            "usage_managed": selected is not None,
                        },
                    )
                if selected is not None:
                    self._append_run_event(
                        row.run_id,
                        at=now,
                        kind="lifecycle",
                        payload={"activation": selected},
                    )
                else:
                    self._append_run_event(
                        row.run_id,
                        at=now,
                        kind="lifecycle",
                        payload={"usage_pending": "exec", "usage_managed": False},
                    )
                if session_profile is not None:
                    self._append_run_event(
                        row.run_id,
                        at=now,
                        kind="lifecycle",
                        payload={"session_profile": session_profile.data()},
                    )
                if inference is not None:
                    self._append_run_event(
                        row.run_id,
                        at=now,
                        kind="lifecycle",
                        payload={"inference_profile": inference},
                    )
                if inference_home is not None:
                    self._append_run_event(
                        row.run_id,
                        at=now,
                        kind="lifecycle",
                        payload={"inference_home": inference_home.data()},
                    )
                if record is not None:
                    self._append_run_event(
                        row.run_id,
                        at=now,
                        kind="lifecycle",
                        payload={"effort_profile": record},
                    )
                if native_context is not None:
                    self._run(
                        "INSERT INTO native_contexts "
                        "(run_id, home, sqlite_home, model_provider, auth_env, "
                        "read_roots) VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            row.run_id,
                            native_context.home,
                            native_context.sqlite_home,
                            native_context.model_provider,
                            native_context.auth_env,
                            json.dumps(list(native_context.read_roots))
                            if native_context.read_roots is not None
                            else None,
                        ),
                    )
                if payload is not None:
                    self._append_run_event(
                        row.run_id, at=now, kind="lifecycle", payload=payload
                    )
                if claim_worktree:
                    self._claim_in_transaction(row.worktree, row.run_id, now, lease_ms)
                if notice is not None:
                    _insert_notice(connection, row.run_id, notice, conflict="")
                if run_key is not None:
                    self._run(
                        "INSERT INTO run_keys(run_id, launch_uuid, project, "
                        "idempotency_key, fingerprint, prompt_sha256) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            row.run_id,
                            run_key.launch_uuid,
                            run_key.project,
                            run_key.idempotency_key,
                            run_key.fingerprint,
                            run_key.prompt_sha256,
                        ),
                    )
                if class_observation is not None:
                    self._run(
                        "INSERT INTO class_observations(run_id, text, declared, "
                        "observed, confidence, effective, observed_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            row.run_id,
                            class_observation.text,
                            class_observation.declared,
                            class_observation.observed,
                            class_observation.confidence,
                            class_observation.effective,
                            row.created_at,
                        ),
                    )
                started = self._run(
                    "UPDATE runs SET phase = ?, updated_at = ? "
                    "WHERE run_id = ? AND owner_instance = ? AND phase = 'pending'",
                    (
                        "pending" if activation is not None else "spawning",
                        now,
                        row.run_id,
                        self.instance_id,
                    ),
                )
                if started != 1:
                    raise UnknownRun(f"run {row.run_id!r} could not be started")
        except nott_database.IntegrityError as error:
            if ADMISSION_HELD in str(error):
                found = self._one(
                    "SELECT b.node, h.generation FROM admission_bindings b "
                    "JOIN node_holds h ON h.node = b.node WHERE b.group_name = ?",
                    (row.group_name,),
                )
                if found is None:
                    raise RegistryError(
                        "admission held but its fence cannot be read"
                    ) from error
                raise AdmissionHeldRow(found["node"], found["generation"]) from error
            if ADMISSION_UNBOUND in str(error):
                raise AdmissionUnboundRow(ADMISSION_UNBOUND) from error
            if KEY_FENCED in str(error):
                raise KeyFencedRow(
                    f"run {row.run_id!r} was refused: its idempotency key is fenced"
                ) from error
            constraint = _unique_constraint(error)
            if constraint is None:
                raise
            raise UniqueTaken(
                f"run {row.run_id!r} was refused by the unique index on {constraint}",
                constraint,
            ) from error

    def export_class_observations(self) -> tuple[ClassLabel, ...]:
        """Every kept class observation whose run has reached a terminal phase.

        Read-only. The outcome lives on the observation, so a run pruned since
        still exports; a run still in progress yields nothing yet.
        """
        found = self._rows(
            "SELECT run_id, text, declared, observed, confidence, effective, "
            "observed_at, outcome, outcome_at FROM class_observations "
            "WHERE outcome IS NOT NULL ORDER BY observed_at, run_id"
        )
        return tuple(
            ClassLabel(
                run_id=record["run_id"],
                observed_at=record["observed_at"],
                phase=RunPhase(record["outcome"]),
                outcome_at=record["outcome_at"],
                observation=ClassObservation(
                    text=record["text"],
                    declared=record["declared"],
                    observed=record["observed"],
                    confidence=record["confidence"],
                    effective=record["effective"],
                ),
            )
            for record in found
        )

    def update_run(
        self, row: RunRow, *, drop_claim: bool = False, annotate: bool = False
    ) -> None:
        """Move a run's mutable columns.

        A run that ENDED (completed, failed or closed) is never rewritten: the
        write is fenced unless the new phase is `closed`, so the first terminal
        phase wins and a late failure cannot replace a completion. `annotate`
        admits a completed or failed row's own phase again, to add what was
        learned after it ended. ORPHANED is provisional and stays writable. The observation pair and the immutable columns are
        not in the statement at all.

        FAILED and CLOSED moves delete this run's ordinary worktree claim;
        `drop_claim` also requests this for other phases. Poison stays fenced.
        The delete is in the SAME transaction as the phase move, so a crash
        between them cannot leave the leftover this argument exists to end. The fence is the RUN, as in `_close_with_identity`: a run that
        took no claim deletes nothing, and never another run's row.
        """
        pid, boot_id, start_ticks = _identity_columns(row)
        values: dict[str, Any] = {
            "run_id": row.run_id,
            "annotate": annotate,
            "phase": row.phase.value,
            "process_pid": pid,
            "process_boot_id": boot_id,
            "process_start_ticks": start_ticks,
        }
        for column in UPDATE_COLUMNS:
            if column not in values:
                values[column] = getattr(row, column)
        assignments = ", ".join(f"{column} = :{column}" for column in UPDATE_COLUMNS)
        with self._transaction():
            # Inspect the stored backend; a caller's replacement row cannot turn
            # off native fences by merely claiming to be Claude.
            stored = self._one(
                "SELECT backend, executed_provider, executed_account "
                "FROM runs WHERE run_id = ?",
                (row.run_id,),
            )
            if (
                stored is not None
                and stored["backend"] in ("codex", "agy")
                and (stored["executed_provider"], stored["executed_account"])
                != (row.executed_provider, row.executed_account)
            ):
                raise RegistryError("native session origin is immutable")
            if row.thread_id is not None and not row.phase.is_terminal():
                finished = self._rows(
                    f"SELECT {', '.join(RUN_COLUMNS)} FROM runs "
                    "WHERE thread_id = ? AND run_id <> ? "
                    "AND backend NOT IN ('codex', 'agy') "
                    "AND phase IN ('completed', 'failed') "
                    "AND NOT EXISTS (SELECT 1 FROM claims WHERE claims.run_id = runs.run_id)",
                    (row.thread_id, row.run_id),
                )
                for record in finished:
                    prior = _row_from_record(record)
                    if (
                        prior.process_identity is None
                        or not process_identity_matches(prior.process_identity)
                    ) and (
                        prior.process_group is None
                        or (
                            prior.process_identity is not None
                            and of_earlier_boot(prior.process_identity)
                        )
                    ):
                        self._run(
                            "UPDATE runs SET phase = 'closed', "
                            "process_group = NULL, process_pid = NULL, "
                            "process_boot_id = NULL, process_start_ticks = NULL "
                            "WHERE run_id = ? AND phase IN ('completed', 'failed')",
                            (prior.run_id,),
                        )
            changed = self._run(
                f"UPDATE runs SET {assignments} WHERE run_id = :run_id AND CASE "
                "WHEN :annotate THEN phase = :phase AND phase IN ('completed', 'failed') "
                "ELSE phase NOT IN ('completed', 'failed', 'closed') "
                "OR :phase = 'closed' END",
                values,
            )
            terminal_released = row.phase in (RunPhase.FAILED, RunPhase.CLOSED) and (
                row.process_identity is None
                or not process_identity_matches(row.process_identity)
            )
            if (drop_claim or terminal_released) and changed == 1:
                self._run(
                    "DELETE FROM claims WHERE run_id = ? AND lease_until <> ?",
                    (row.run_id, POISONED_LEASE_UNTIL),
                )
        if changed == 0:
            found = self._one("SELECT phase FROM runs WHERE run_id = ?", (row.run_id,))
            if found is not None and annotate:
                raise ClosedRun(
                    f"run {row.run_id!r} is {found['phase']}: an annotation of "
                    f"{row.phase.value} never moves it"
                )
            if found is not None and found["phase"] in (
                "completed",
                "failed",
                "closed",
            ):
                raise ClosedRun(
                    f"run {row.run_id!r} is {found['phase']} and is not reopened"
                )
            raise UnknownRun(f"run {row.run_id!r} is not in this registry")

    def note_record_observed(self, run_id: str, at_ms: int, *, count: int = 1) -> None:
        """Record that ``count`` records arrived, rendered or not.

        This is the half `update_run` cannot carry: every other durable field
        moves when nSPAWN RENDERED something, so a record the fold drops moves
        none of them and afterwards "the child said nothing" and "we dropped what
        it said" are the same row. They are different facts.

        A late record does NOT resurrect a closed run.
        """
        _validate_wall_time("runs.records_seen count", count)
        if count == 0:
            raise RegistryError("runs.records_seen count must be positive")
        self._write(
            "UPDATE runs SET last_record_at = ?, records_seen = records_seen + ? "
            "WHERE run_id = ? AND phase != 'closed'",
            (at_ms, count, run_id),
        )

    def get_run(self, run_id: str) -> RunRow | None:
        """One run, or None."""
        _validate_required("runs.run_id", run_id, MAX_DURABLE_IDENTIFIER_BYTES)
        found = self._one(
            f"SELECT {', '.join(RUN_COLUMNS)} FROM runs WHERE run_id = ?", (run_id,)
        )
        return None if found is None else _row_from_record(found)

    def resolve_native_session(
        self, thread_id: str, *, provider: str | None = None, account: str | None = None
    ) -> tuple[RunRow, NativeSessionContext] | None:
        """Resolve one origin, or refuse ambiguity/mismatch rather than fall back.

        LIMIT 2 bounds the ambiguity witness, independent of history size. The
        chosen origin then reads its latest run through the same history index.
        """
        _validate_required("thread_id", thread_id, MAX_DURABLE_IDENTIFIER_BYTES)
        for name, value in (("provider", provider), ("account", account)):
            if value is not None:
                _validate_required(name, value, MAX_DURABLE_IDENTIFIER_BYTES)
        # A Claude origin is its provider alone: its history lives in the session's
        # own config dir, and a subscription wall may move it to another account.
        origins = self._rows(
            "SELECT DISTINCT backend, executed_provider, CASE WHEN backend IN "
            "('codex', 'agy') THEN executed_account END AS account_key FROM runs "
            "WHERE thread_id = :thread AND (backend IN ('codex', 'agy') "
            "OR EXISTS (SELECT 1 FROM native_contexts c WHERE c.run_id = runs.run_id)) "
            "AND (:provider IS NULL OR executed_provider = :provider) "
            "AND (:account IS NULL OR executed_account = :account) LIMIT 2",
            {"thread": thread_id, "provider": provider, "account": account},
        )
        if not origins:
            existing = self._one(
                "SELECT 1 FROM runs WHERE thread_id = ? "
                "AND (backend IN ('codex', 'agy') OR EXISTS "
                "(SELECT 1 FROM native_contexts c WHERE c.run_id = runs.run_id)) LIMIT 1",
                (thread_id,),
            )
            if existing is not None:
                raise RegistryError("native session provider/account mismatch")
            return None
        if len(origins) != 1:
            raise RegistryError(
                "native session origin is ambiguous; name provider/account"
            )
        origin = origins[0]
        found = self._one(
            f"SELECT {', '.join('r.' + c for c in RUN_COLUMNS)}, "
            "c.home, c.sqlite_home, c.model_provider, c.auth_env, c.read_roots "
            "FROM runs r LEFT JOIN native_contexts c "
            "ON c.run_id = r.run_id WHERE r.thread_id = :thread AND r.backend = :backend "
            "AND r.executed_provider IS :provider "
            "AND (:account IS NULL OR r.executed_account = :account) "
            "ORDER BY r.created_at DESC, r.run_id DESC LIMIT 1",
            {
                "thread": thread_id,
                "backend": origin["backend"],
                "provider": origin["executed_provider"],
                "account": origin["account_key"],
            },
        )
        if (
            found is None
            or found["home"] is None
            or found["executed_provider"] is None
            or found["executed_account"] is None
        ):
            raise RegistryError("native session history lacks account context")
        try:
            roots = (
                None if found["read_roots"] is None else json.loads(found["read_roots"])
            )
            if roots is not None and not isinstance(roots, list):
                raise ValueError("native read_roots is not an array")
            context = NativeSessionContext(
                home=found["home"],
                sqlite_home=found["sqlite_home"],
                model_provider=found["model_provider"],
                auth_env=found["auth_env"],
                read_roots=tuple(roots) if roots is not None else None,
            )
        except (TypeError, ValueError) as error:
            raise RegistryError("native session history has invalid context") from error
        return _row_from_record({c: found[c] for c in RUN_COLUMNS}), context

    # -- bulk reads and the reaper ------------------------------------------

    def list_runs_page(
        self,
        run_id: str | None = None,
        after_run: str | None = None,
        limit: int = MAX_RUN_PAGE,
        *,
        owner: str | None = None,
    ) -> RunPage:
        """One page of runs, newest first: `created_at DESC, run_id DESC`.

        Creation time and not `run_id`: a uuid order made page one an arbitrary
        slice, and with 1565 runs a dispatch from a minute ago was on no page an
        operator reads (2026-09-24). `created_at` is immutable after insert, so
        the order cannot shift under a cursor; `run_id` breaks the ties.

        `after_run` names the last run of the previous page and the next page
        starts strictly after its `(created_at, run_id)`. A cursor whose run is
        gone (the reaper pruned it) has no position and is REFUSED rather than
        answered with an empty page that reads as the end of the fleet.

        Naming a `run_id` answers that run alone and NEVER reports truncation —
        one row is the whole answer. Otherwise the page is bounded, and the
        boundary is detected by fetching ONE row past the limit rather than by a
        second count that could disagree with the page it describes.

        An `owner` keeps only the runs that node admitted, in the statement itself:
        the page cap counts its runs, never the fleet's.
        ponytail: no owner index; the filter walks the page index, and closed runs
        are pruned past `Supervisor.MAX_CLOSED_RUNS`. Index it if a scan shows.
        """
        if run_id is not None:
            _validate_required("runs.run_id", run_id, MAX_DURABLE_IDENTIFIER_BYTES)
        if after_run is not None:
            _validate_required(
                "runs.after_run", after_run, MAX_DURABLE_IDENTIFIER_BYTES
            )
        page_limit = min(max(limit, 1), MAX_RUN_PAGE)
        columns = ", ".join(RUN_COLUMNS)
        mine = "" if owner is None else " AND owner = :owner"
        if run_id is not None:
            found = self._rows(
                f"SELECT {columns} FROM runs WHERE run_id = :run{mine} LIMIT 1",
                {"run": run_id, "owner": owner},
            )
        elif after_run is not None:
            anchor = self._one(
                "SELECT created_at FROM runs WHERE run_id = ?", (after_run,)
            )
            if anchor is None:
                raise RegistryError(
                    f"runs.after_run names no run: {after_run!r} "
                    "(pruned since the previous page); start from the first page"
                )
            found = self._rows(
                f"SELECT {columns} FROM runs "
                f"WHERE (created_at, run_id) < (:at, :after){mine} "
                "ORDER BY created_at DESC, run_id DESC LIMIT :limit",
                {
                    "at": anchor["created_at"],
                    "after": after_run,
                    "owner": owner,
                    "limit": page_limit + 1,
                },
            )
        else:
            found = self._rows(
                f"SELECT {columns} FROM runs WHERE TRUE{mine} "
                "ORDER BY created_at DESC, run_id DESC LIMIT :limit",
                {"owner": owner, "limit": page_limit + 1},
            )
        rows = [_row_from_record(record) for record in found]
        truncated = run_id is None and len(rows) > page_limit
        if truncated:
            rows = rows[:page_limit]
        return RunPage(
            rows=tuple(rows),
            next_after_run=rows[-1].run_id if truncated and rows else None,
            truncated=truncated,
        )

    def list_runs(
        self, run_id: str | None = None, *, owner: str | None = None
    ) -> tuple[RunRow, ...]:
        """The first page of runs (of `owner`). Bounded, like every read here."""
        return self.list_runs_page(run_id, owner=owner).rows

    def owned_runs(
        self, owner: str
    ) -> list[tuple[RunRow, str | None, int | None, bool]]:
        """Every run of `owner`, newest first, each with its idempotency key, its
        notice's current generation (None before it was ever armed, or once pruned)
        and whether that generation's delivery was acked. A run with no notice row
        reads acked: nothing is left to deliver, and an undelivered node notice is
        never pruned (`prune_notices`).

        Unpaged: `nspawn run list --owner` has no cursor, and a cap would drop
        the oldest runs, open ones included. The store's retention bounds it."""
        found = self._rows(
            f"SELECT {', '.join('r.' + c for c in RUN_COLUMNS)}, k.idempotency_key, "
            "NULLIF(n.generation, 0) AS notice_generation, "
            "(n.run_id IS NULL OR n.delivered_at IS NOT NULL) AS notice_acked "
            "FROM runs r LEFT JOIN run_keys k ON k.run_id = r.run_id "
            "LEFT JOIN notices n ON n.run_id = r.run_id AND n.caller_addr = r.owner "
            "WHERE r.owner = ? ORDER BY r.created_at DESC, r.run_id DESC",
            (owner,),
        )
        return [
            (
                _row_from_record({c: record[c] for c in RUN_COLUMNS}),
                record["idempotency_key"],
                record["notice_generation"],
                bool(record["notice_acked"]),
            )
            for record in found
        ]

    def group_runs(self, group: str) -> list[tuple[RunRow, str | None]]:
        """Every run of `group`, whoever admitted it, newest first, each with its
        idempotency key. Unpaged, as `owned_runs`: the store's retention bounds it."""
        found = self._rows(
            f"SELECT {', '.join('r.' + c for c in RUN_COLUMNS)}, k.idempotency_key "
            "FROM runs r LEFT JOIN run_keys k ON k.run_id = r.run_id "
            "WHERE r.group_name = ? ORDER BY r.created_at DESC, r.run_id DESC",
            (group,),
        )
        return [
            (
                _row_from_record({c: record[c] for c in RUN_COLUMNS}),
                record["idempotency_key"],
            )
            for record in found
        ]

    def open_owned_runs(self, owner: str, before: int | None = None) -> list[str]:
        """Open delegated runs of `owner`, oldest first, through `before` if set.

        The host closes native activation runs by their own run ID.
        The driver ceiling bounds the number of live runs.
        """
        admitted = "" if before is None else "AND created_at <= ? "
        return [
            str(row["run_id"])
            for row in self._rows(
                "SELECT run_id FROM runs WHERE owner = ? "
                f"AND phase NOT IN ({TERMINAL_PHASE_SQL}) {admitted}"
                "ORDER BY created_at, run_id",
                (owner,) if before is None else (owner, before),
            )
            if self.session_activation(str(row["run_id"])) is None
        ]

    def known_threads(self, ids: Iterable[str]) -> set[str]:
        """Recorded vendor thread IDs, including those whose runs were pruned."""
        wanted = list(dict.fromkeys(ids))
        found: set[str] = set()
        for start in range(0, len(wanted), 500):
            chunk = wanted[start : start + 500]
            found.update(
                row["thread_id"]
                for row in self._rows(
                    "SELECT DISTINCT thread_id FROM runs WHERE thread_id IN "
                    f"({','.join('?' * len(chunk))}) UNION "
                    "SELECT json_extract(payload, '$.thread_origin') FROM run_events "
                    "WHERE kind = 'lifecycle' AND json_type(payload, '$.thread_origin') = 'text' "
                    f"AND json_extract(payload, '$.thread_origin') IN ({','.join('?' * len(chunk))})",
                    tuple(chunk) * 2,
                )
            )
        return found

    def thread_owners(self, thread_id: str) -> set[str | None]:
        """The owners of the runs that recorded `thread_id`; two at most."""
        return {
            row["owner"]
            for row in self._rows(
                "SELECT DISTINCT owner FROM runs WHERE thread_id = ? LIMIT 2",
                (thread_id,),
            )
        }

    def prune_closed_runs(self, now: int, retain_for_ms: int, max_closed: int) -> int:
        """Drop closed runs past their retention, then past their cardinality.

        A run is prunable only when it is closed, holds NO part of a process
        triple, and no claim references it — history is dropped, never custody.
        A keyed run stays for `KEYED_RETENTION_MS` after its close, so its key
        still deduplicates inside that window. A run whose node notice the host
        has not acknowledged stays with its key, so `notice take` still names it.
        """
        prunable = (
            "phase = 'closed' AND process_group IS NULL AND process_pid IS NULL "
            "AND process_boot_id IS NULL AND process_start_ticks IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM claims WHERE claims.run_id = runs.run_id) "
            "AND NOT EXISTS (SELECT 1 FROM notices WHERE notices.run_id = runs.run_id "
            f"  AND {_NODE_NOTICE_SQL} AND notices.delivered_at IS NULL) "
            "AND NOT (updated_at > :keyed AND EXISTS (SELECT 1 FROM run_keys k "
            "  WHERE k.run_id = runs.run_id AND k.idempotency_key IS NOT NULL))"
        )
        keyed = now_ms() - KEYED_RETENTION_MS
        with self._transaction():
            for row in self._rows(
                f"SELECT run_id, thread_id FROM runs WHERE {prunable} "
                "AND thread_id IS NOT NULL AND NOT EXISTS ("
                "SELECT 1 FROM run_events WHERE run_events.run_id = runs.run_id "
                "AND json_type(payload, '$.thread_origin') = 'text')",
                {"keyed": keyed},
            ):
                self._append_run_event(
                    row["run_id"],
                    at=now,
                    kind="lifecycle",
                    payload={"thread_origin": row["thread_id"]},
                )
            by_age = self._run(
                f"DELETE FROM runs WHERE {prunable} AND updated_at < :cutoff",
                {"keyed": keyed, "cutoff": now - max(retain_for_ms, 0)},
            )
            by_count = self._run(
                "DELETE FROM runs WHERE run_id IN ("
                f"SELECT run_id FROM runs WHERE {prunable} "
                "ORDER BY updated_at DESC, run_id DESC LIMIT -1 OFFSET :keep)",
                {"keyed": keyed, "keep": max(max_closed, 0)},
            )
        return by_age + by_count

    def mark_orphans(self, now: int, stale_after_ms: int) -> int:
        """Orphan every run whose owning instance stopped proving it is alive.

        The instance running THIS registry is protected: a supervisor never
        orphans its own runs by its own reaper pass.
        """
        _validate_required(
            "instances.instance_id", self.instance_id, MAX_DURABLE_IDENTIFIER_BYTES
        )
        stale_before = now - max(stale_after_ms, 0)
        with self._transaction():
            changed = self._run(
                f"""UPDATE runs
                    SET phase = 'orphaned',
                        updated_at = :now,
                        blocked_reason = CASE
                            WHEN phase = 'waiting_approval' OR pending_approvals > 0
                            THEN 'owner instance heartbeat expired; parked approvals were lost; confirmed stale close is required'
                            ELSE COALESCE(
                                blocked_reason,
                                'owner instance heartbeat expired; run requires doctor/recovery'
                            )
                        END,
                        pending_approvals = 0
                    WHERE owner_instance != :protected
                      AND phase NOT IN ({TERMINAL_PHASE_SQL})
                      AND NOT EXISTS (
                          SELECT 1 FROM instances
                          WHERE instances.instance_id = runs.owner_instance
                            AND instances.heartbeat_at > :stale_before
                      )""",
                {
                    "now": now,
                    "protected": self.instance_id,
                    "stale_before": stale_before,
                },
            )
            # An orphan that still holds custody keeps its worktree fenced forever.
            self._run(
                "UPDATE claims SET lease_until = ? "
                "WHERE lease_until <> ? AND run_id IN ("
                "SELECT run_id FROM runs WHERE phase = 'orphaned' "
                "AND process_pid IS NOT NULL AND process_boot_id IS NOT NULL "
                "AND process_start_ticks IS NOT NULL)",
                (POISONED_LEASE_UNTIL, POISONED_LEASE_UNTIL),
            )
            # An expired claim is released only when its run holds no custody.
            self._run(
                "DELETE FROM claims WHERE lease_until <= ? "
                "AND run_id NOT IN (SELECT run_id FROM runs "
                "WHERE phase != 'closed' AND (phase = 'orphaned' "
                "OR process_group IS NOT NULL OR process_pid IS NOT NULL "
                "OR process_boot_id IS NOT NULL OR process_start_ticks IS NOT NULL))",
                (now,),
            )
            self._run(
                "DELETE FROM instances WHERE instance_id != ? AND heartbeat_at <= ?",
                (self.instance_id, stale_before),
            )
        return changed

    def find_key(self, project: str, key: str) -> KeyedRun | None:
        """The run `key` names in `project`, read without the key lock."""
        try:
            return _keyed_run(self._rows(_KEYED_RUN_SQL, (project, key)))
        except (*STORE_ERRORS, ValueError) as error:
            raise RegistryError(
                f"idempotency key {key!r} could not be read: {error}"
            ) from error

    def run_of_launch(self, launch_uuid: str) -> str | None:
        """The run a driver launch admitted, once its admission committed."""
        row = self._one(
            "SELECT run_id FROM run_keys WHERE launch_uuid = ?", (launch_uuid,)
        )
        return None if row is None else str(row["run_id"])

    # -- the notice outbox ----------------------------------------------------

    def ready_notices(
        self, run_ids: Sequence[str] | None = None, *, project: str | None = None
    ) -> list[ReadyNotice]:
        """Ready, undelivered notices with no live claim, of `run_ids` or of
        `project` when either is named. Never a node's: those are the host's.
        Never a swarm member's while another member runs."""
        rows = self._rows(
            "SELECT run_id, caller_addr, caller_pid, caller_start_ticks "
            f"FROM notices WHERE {_NOTICE_FREE_SQL} AND NOT {_NODE_NOTICE_SQL} "
            f"AND NOT {_SWARM_HELD_SQL} "
            "AND (:any_run OR run_id IN (SELECT value FROM json_each(:ids))) "
            "AND (:project IS NULL OR caller_project = :project) "
            "ORDER BY ready_at",
            {
                "now": now_ms(),
                "any_run": run_ids is None,
                "ids": json.dumps(list(run_ids or ())),
                "project": project,
            },
        )
        return [
            ReadyNotice(
                str(r["run_id"]),
                str(r["caller_addr"]),
                int(r["caller_pid"]),
                int(r["caller_start_ticks"]),
            )
            for r in rows
        ]

    def claim_notice(self, run_id: str, addr: str) -> tuple[str, str] | None:
        """Take a free notice under a fresh token: `(token, phase)`, or None.

        The token is new per claim, and the readiness trigger clears it on a
        re-arm, so only the claim that is still current can mark the delivery.
        """
        now = now_ms()
        with self._transaction():
            rows = self._rows(
                "UPDATE notices SET claimed_at = :now, claim_token = :token, "
                "claim_lease_until = :lease "
                f"WHERE run_id = :run AND caller_addr = :addr AND {_NOTICE_FREE_SQL} "
                f"RETURNING claim_token AS token, {_NOTICE_PHASE_SQL} AS phase",
                {
                    "now": now,
                    "token": str(uuid.uuid4()),
                    "lease": now + NOTICE_LEASE_S * 1000,
                    "run": run_id,
                    "addr": addr,
                },
            )
        return (str(rows[0]["token"]), str(rows[0]["phase"])) if rows else None

    def mark_notice_delivered(self, run_id: str, addr: str, token: str) -> bool:
        """Mark the delivery of THIS claim; False when a re-arm or a drain won."""
        return (
            self._write(
                "UPDATE notices SET delivered_at = ? "
                "WHERE run_id = ? AND caller_addr = ? AND claim_token = ? "
                "AND delivered_at IS NULL",
                (now_ms(), run_id, addr, token),
            )
            == 1
        )

    def claim_notice_row(
        self, run_id: str, addr: str
    ) -> tuple[str, str, RunRow | None] | None:
        """`claim_notice` and the run's row, read in the claim's write: the
        phase and the row are one state of the run, never a phase from before
        a write and a row from after it."""
        with self._transaction():
            claim = self.claim_notice(run_id, addr)
            return None if claim is None else (*claim, self.get_run(run_id))

    def claim_notice_rows(
        self, run_id: str, addr: str
    ) -> list[tuple[str, str, str, RunRow | None]]:
        """`claim_notice_row` of this notice and, when its run is a swarm
        member, of every other free member notice of the same caller, all in
        one write: `(run_id, token, phase, row)` each. Empty when this notice
        is not free or its swarm still waits on a member."""
        with self._transaction():
            members = self._rows(
                f"SELECT ({_SWARM_HELD_SQL}) AS held, "
                f"(SELECT json_group_array(run_id) FROM ({_SWARM_MEMBERS_SQL})) "
                "AS members FROM notices WHERE run_id = :run AND caller_addr = :addr",
                {"run": run_id, "addr": addr, "now": now_ms()},
            )
            if not members or members[0]["held"]:
                return []
            others = sorted(set(json.loads(members[0]["members"])) - {run_id})
            claims = []
            for member in (run_id, *others):
                claim = self.claim_notice_row(member, addr)
                if claim is not None:
                    claims.append((member, *claim))
                elif member == run_id:
                    return []
            return sorted(claims)

    def mark_notices_delivered(
        self, addr: str, claims: Sequence[tuple[str, str]]
    ) -> int:
        """`mark_notice_delivered` of every `(run_id, token)` in ONE write: a
        swarm's notice is marked whole or not at all, so a store refusal never
        leaves a part of it to be sent again alone."""
        with self._transaction():
            return sum(
                self.mark_notice_delivered(run_id, addr, token)
                for run_id, token in claims
            )

    def drain_notices(
        self,
        project: str,
        dead: Sequence[tuple[str, str]],
        render: Callable[[list[tuple[str, str, RunRow | None, str]]], list[str]],
    ) -> list[str]:
        """Claim and deliver in ONE write what no live caller will hear, and
        `render` every `(run_id, phase, row, caller_addr)` it delivers in that
        same write.

        Free notices of `project` whose caller is in `dead` (as `(run_id, addr)`)
        or that have been ready longer than NOTICE_STALE_S; a swarm member's
        readiness is its last member's, and a swarm is dead when one member's
        caller is, so a swarm drains whole. At most once: the
        rows are delivered before anyone prints them, and no writer lands
        between a run's row and its mark, so every body is the state it was
        delivered in. `render` must stay bounded (the write holds the store's
        lock) and answers the bodies to print. A raise rolls the delivery back. A
        node's notice is never drained into a session: the host takes it.
        """
        now = now_ms()
        with self._transaction():
            rows = self._rows(
                "UPDATE notices SET claimed_at = :now, delivered_at = :now "
                f"WHERE caller_project = :project AND {_NOTICE_FREE_SQL} "
                f"AND NOT {_NODE_NOTICE_SQL} AND NOT {_SWARM_HELD_SQL} "
                f"AND ({_SWARM_READY_SQL} < :stale OR (run_id, caller_addr) IN ("
                "  SELECT json_extract(value, '$[0]'), json_extract(value, '$[1]') "
                "  FROM json_each(:dead)) "
                # A swarm is dead when any member's caller is: it drains whole.
                f"OR EXISTS (SELECT 1 FROM ({_SWARM_MEMBERS_SQL}) member "
                "  JOIN json_each(:dead) dead "
                "  ON json_extract(dead.value, '$[0]') = member.run_id "
                "  AND json_extract(dead.value, '$[1]') = notices.caller_addr)) "
                f"RETURNING run_id, caller_addr, {_NOTICE_PHASE_SQL} AS phase",
                {
                    "now": now,
                    "project": project,
                    "stale": now - NOTICE_STALE_S * 1000,
                    "dead": json.dumps([list(pair) for pair in dead]),
                },
            )
            drained = sorted(
                (str(r["run_id"]), str(r["phase"]), str(r["caller_addr"])) for r in rows
            )
            return render(
                [
                    (run_id, phase, self.get_run(run_id), addr)
                    for run_id, phase, addr in drained
                ]
            )

    def undelivered_notices(
        self, owner: str | None = None
    ) -> list[tuple[str, str, str]]:
        """Every ready, undelivered notice (of `owner`'s runs):
        `(run_id, phase, caller_project)`."""
        rows = self._rows(
            f"SELECT run_id, {_NOTICE_PHASE_SQL} AS phase, caller_project "
            "FROM notices WHERE ready_at IS NOT NULL AND delivered_at IS NULL "
            "AND (:owner IS NULL OR caller_addr = :owner) "
            "ORDER BY ready_at, run_id",
            {"owner": owner},
        )
        return [
            (str(r["run_id"]), str(r["phase"]), str(r["caller_project"])) for r in rows
        ]

    def prune_notices(self) -> int:
        """Drop notices older than NOTICE_RETENTION_DAYS, delivered or not; a
        node's stays until the host delivers it."""
        return self._write(
            "DELETE FROM notices WHERE created_at < ? "
            f"AND NOT ({_NODE_NOTICE_SQL} AND delivered_at IS NULL)",
            (now_ms() - NOTICE_RETENTION_DAYS * 24 * 60 * 60 * 1000,),
        )

    # -- a node's notices: the host's to take ----------------------------------

    def take_notices(self, prefix: str, lease_ms: int, limit: int) -> list[TakenNotice]:
        """Claim up to `limit` free notices of the nodes `prefix` names, each
        under a fresh token for `lease_ms`, oldest ready first.

        `phase` is the one this generation was armed with, so a retake after a
        lapsed lease names the same delivery the same way.
        """
        _node_prefix(prefix)
        now = now_ms()
        with self._transaction():
            rows = self._rows(
                "UPDATE notices SET claimed_at = :now, claim_lease_until = :lease, "
                "claim_token = lower(hex(randomblob(16))) "
                "WHERE (run_id, caller_addr) IN ("
                "  SELECT run_id, caller_addr FROM notices "
                f"  WHERE {_NOTICE_PREFIX_SQL} AND {_NOTICE_FREE_SQL} "
                "  ORDER BY ready_at, run_id LIMIT :limit) "
                "RETURNING run_id, caller_addr, generation, claim_token, phase, "
                "(SELECT group_name FROM runs r WHERE r.run_id = notices.run_id) "
                "  AS group_name, "
                "(SELECT idempotency_key FROM run_keys k "
                "  WHERE k.run_id = notices.run_id) AS idempotency_key, "
                "(SELECT blocked_reason FROM runs r WHERE r.run_id = notices.run_id) "
                "  AS blocked_reason",
                {"now": now, "lease": now + lease_ms, "prefix": prefix, "limit": limit},
            )
        return sorted(
            (
                TakenNotice(
                    run_id=str(row["run_id"]),
                    owner=str(row["caller_addr"]),
                    generation=int(row["generation"]),
                    token=str(row["claim_token"]),
                    phase=str(row["phase"]),
                    group=row["group_name"],
                    key=row["idempotency_key"],
                    failure_reason=row["blocked_reason"],
                )
                for row in rows
            ),
            key=lambda notice: (notice.run_id, notice.owner),
        )

    def renew_notice(
        self, run_id: str, generation: int, token: str, lease_ms: int
    ) -> bool:
        """Extend THIS claim's lease; False once a re-arm, a release, another
        taker or its own ack has replaced it."""
        return (
            self._write(
                "UPDATE notices SET claim_lease_until = ? WHERE run_id = ? "
                "AND generation = ? AND claim_token = ? AND delivered_at IS NULL "
                f"AND {_NODE_NOTICE_SQL}",
                (now_ms() + lease_ms, run_id, generation, token),
            )
            == 1
        )

    def ack_notice(self, run_id: str, generation: int, token: str) -> bool:
        """Mark THIS claim's delivery, once; False when it is no longer current."""
        return (
            self._write(
                "UPDATE notices SET delivered_at = ? WHERE run_id = ? "
                "AND generation = ? AND claim_token = ? AND delivered_at IS NULL "
                f"AND {_NODE_NOTICE_SQL}",
                (now_ms(), run_id, generation, token),
            )
            == 1
        )

    def release_notices(self, prefix: str) -> int:
        """Clear every claim on the undelivered notices of the nodes `prefix`
        names: the host that held them has restarted."""
        _node_prefix(prefix)
        return self._write(
            "UPDATE notices SET claimed_at = NULL, claim_lease_until = NULL, "
            f"claim_token = NULL WHERE {_NOTICE_PREFIX_SQL} "
            "AND delivered_at IS NULL AND claim_token IS NOT NULL",
            {"prefix": prefix},
        )

    def _close_with_identity(
        self,
        run_id: str,
        expected: ProcessIdentity | None,
        now: int,
        owned: bool,
        drained: bool = False,
    ) -> CloseOutcome:
        # Death is checked here, not trusted from a caller or a lease. A dead
        # child remains recoverable before a sweep relabels its run ORPHANED.
        # `None` fences on a row that names no process: the caller proved dead
        # a child the row never published, and an identity published meanwhile
        # makes this update match nothing.
        if expected is not None and process_identity_matches(expected):
            return CloseOutcome(run_closed=False, claim_released=False)
        fence = "AND phase != 'closed'" if owned else "AND phase = 'orphaned'"
        # No process of an earlier boot can live: the legacy group column, which
        # nSPAWN no longer writes, does not fence the close of its run.
        group = (
            ""
            if expected is not None and of_earlier_boot(expected)
            else "AND process_group IS NULL"
        )
        identity = (
            "AND process_pid IS NULL AND process_boot_id IS NULL "
            "AND process_start_ticks IS NULL"
            if expected is None
            else "AND process_pid = :pid AND process_boot_id = :boot_id "
            "AND process_start_ticks = :start_ticks"
        )
        with self._transaction():
            changed = self._run(
                f"""UPDATE runs
                    SET phase = 'closed', updated_at = :now, pending_approvals = 0,
                        -- A close that interrupted a run names what it stopped;
                        -- a finished run keeps the reason, or the null, it had.
                        blocked_reason = CASE
                            WHEN phase IN ('completed', 'failed') THEN blocked_reason
                            ELSE COALESCE(blocked_reason, 'closed while ' || phase)
                        END,
                        process_group = NULL, process_pid = NULL,
                        process_boot_id = NULL, process_start_ticks = NULL
                    WHERE run_id = :run_id {fence}
                      {group}
                      {identity}
                      AND (:drained OR NOT EXISTS (
                          SELECT 1 FROM claims
                          WHERE claims.run_id = runs.run_id
                            AND claims.lease_until = :poisoned
                      ))""",
                {
                    "run_id": run_id,
                    "now": now,
                    "drained": drained,
                    "poisoned": POISONED_LEASE_UNTIL,
                    **(
                        {}
                        if expected is None
                        else {
                            "pid": expected.pid,
                            "boot_id": expected.boot_id,
                            "start_ticks": expected.start_ticks,
                        }
                    ),
                },
            )
            released = 0
            if changed == 1:
                released = self._run(
                    "DELETE FROM claims WHERE run_id = ? AND (? OR lease_until <> ?)",
                    (run_id, drained, POISONED_LEASE_UNTIL),
                )
        return CloseOutcome(run_closed=changed == 1, claim_released=released > 0)

    def close_orphaned(
        self,
        run_id: str,
        expected: ProcessIdentity | None,
        now: int,
        *,
        drained: bool = False,
    ) -> bool:
        """Close an ORPHANED run whose process is the one named. False otherwise.

        The identity must match exactly: closing on a pid alone would close a run
        whose pid the kernel has already handed to somebody else.
        """
        _validate_required("runs.run_id", run_id, MAX_DURABLE_IDENTIFIER_BYTES)
        return self._close_with_identity(
            run_id, expected, now, owned=False, drained=drained
        ).run_closed

    def close_owned_outcome(
        self,
        run_id: str,
        expected: ProcessIdentity | None,
        now: int,
        *,
        drained: bool = False,
    ) -> CloseOutcome:
        """Close a proven-dead run from any instance and report BOTH halves.

        The historical name does not require the original supervisor to survive.
        The stored triple must still match, its process must be dead, and a
        poisoned claim refuses the transition regardless of holder or lease age —
        unless the run's writer slice was `drained`: observed empty and stopped,
        which is the custody the poison says nobody could establish.

        The run half and the claim half are two statements and two rowcounts.
        Answering the second with the first is how eight closes reported a
        released claim while eight rows stayed in `claims` carrying the same
        lease: the product acted and then described a DIFFERENT act's effect.
        """
        _validate_required("runs.run_id", run_id, MAX_DURABLE_IDENTIFIER_BYTES)
        return self._close_with_identity(
            run_id, expected, now, owned=True, drained=drained
        )

    def close_owned(
        self,
        run_id: str,
        expected: ProcessIdentity | None,
        now: int,
        *,
        drained: bool = False,
    ) -> bool:
        """The RUN half of `close_owned_outcome`, for a caller that asks only that.

        A caller that REPORTS the close to a human wants the outcome instead:
        this bool says nothing about the claim, and reading it as if it did is
        the defect that method exists to end.
        """
        return self.close_owned_outcome(
            run_id, expected, now, drained=drained
        ).run_closed

    def with_owned_claim[T](
        self,
        run_id: str,
        worktree: str,
        now: int,
        callback: Callable[[], T],
        *,
        recovery: bool = False,
        holder: str | None = None,
    ) -> T:
        """Run `callback` while the exact live run owns its worktree claim.

        No transaction spans the callback (it would hold the file's one write
        lock for an export's whole length): ownership is read before AND after
        it, so a claim taken meanwhile fails the call instead of letting it
        succeed. Every write the callback makes (`record_implementation_recovery`,
        `resolve_implementation_recovery`) is its own short write. The caller
        serializes this Registry connection.
        """
        phases = (
            (
                "pending",
                "spawning",
                "running",
                "idle",
                "failed",
                "completed",
                "orphaned",
            )
            if recovery
            else ("pending", "spawning", "running", "idle")
        )
        owner = self.instance_id if holder is None else holder

        def require_ownership() -> None:
            found = self._one(
                "SELECT c.run_id FROM claims c JOIN runs r ON r.run_id = c.run_id "
                "WHERE c.worktree = ? AND c.run_id = ? AND c.holder = ? "
                "AND c.lease_until > ? AND r.worktree = c.worktree "
                "AND r.owner_instance = c.holder "
                "AND r.phase IN (SELECT value FROM json_each(?))",
                (worktree, run_id, owner, now, json.dumps(phases)),
            )
            if found is None:
                raise RegistryError(
                    "controller export no longer owns its live worktree claim"
                )

        require_ownership()
        result = callback()
        # A recovery's callback ends by resolving its own fence, which deletes the
        # claim it owned (`resolve_implementation_recovery`): an absent claim with the
        # recovery resolved is that, not a claim taken meanwhile. Anything else is
        # read as ownership again.
        if not (
            recovery
            and self.read_claim(worktree) is None
            and self.implementation_recovery(run_id) is None
        ):
            require_ownership()
        return result

    def implementation_recovery(self, run_id: str) -> dict[str, str] | None:
        if not self.verify_run_event_chain(run_id):
            raise RegistryError("implementation recovery event chain is invalid")
        result = None
        for event in self.read_run_events(run_id):
            payload = event.payload
            if (
                not isinstance(payload, dict)
                or payload.get("schema") != "nott.implementation.custody.v1"
            ):
                continue
            if payload.get("state") == "resolved" and set(payload) == {
                "schema",
                "state",
            }:
                result = None
            elif payload.get("state") == "pending" and set(payload) == {
                "schema",
                "state",
                "artifact",
            }:
                result = _recovery_reference(payload["artifact"])
            else:
                raise RegistryError("implementation recovery state is malformed")
        return result

    def record_implementation_recovery(
        self,
        run_id: str,
        worktree: str,
        now: int,
        lease_ms: int,
        reference: dict[str, str] | None,
    ) -> None:
        """Checkpoint an implementation export in one short write: pending poisons
        the claim, resolved gives it an ordinary lease again."""
        reference = _recovery_reference(reference) if reference is not None else None

        def record() -> None:
            payload: dict[str, Any] = {
                "schema": "nott.implementation.custody.v1",
                "state": "resolved" if reference is None else "pending",
            }
            if reference is not None:
                payload["artifact"] = reference
            with self._transaction():
                self.append_run_event(run_id, at=now, kind="lifecycle", payload=payload)
                self._run(
                    "UPDATE claims SET lease_until = ? WHERE run_id = ? AND worktree = ?",
                    (
                        now + lease_ms if reference is None else POISONED_LEASE_UNTIL,
                        run_id,
                        worktree,
                    ),
                )

        self.with_owned_claim(run_id, worktree, now, record, recovery=True)

    def resolve_implementation_recovery(
        self, run_id: str, worktree: str, holder: str, now: int
    ) -> None:
        """After body reconciliation, resolve the checkpoint and its exact poisoned claim."""
        with self._transaction():
            self.append_run_event(
                run_id,
                at=now,
                kind="lifecycle",
                payload={
                    "schema": "nott.implementation.custody.v1",
                    "state": "resolved",
                },
            )
            self._run(
                "DELETE FROM claims WHERE run_id = ? AND worktree = ? AND holder = ?",
                (run_id, worktree, holder),
            )

    def read_claim(self, worktree: str) -> WorktreeClaim | None:
        """The claim on `worktree`, or None."""
        found = self._one(
            "SELECT worktree, run_id, holder, lease_until FROM claims WHERE worktree = ?",
            (worktree,),
        )
        if found is None:
            return None
        return WorktreeClaim(**found)

    def get_provider_walls(self, now: int) -> dict[str, int]:
        """Return live provider cooldowns keyed by provider name.

        "Live" means still cooling: `cooling_until > now`, the same exclusive
        boundary a lease uses — a wall expires AT its own instant, not after it.
        An expired wall is left in place rather than deleted here; the next
        `record_provider_wall` for that provider overwrites it. A live wall is
        only ever lengthened: a shorter one written later leaves it whole.
        """
        return {
            row.provider: row.cooling_until for row in self.get_provider_wall_rows(now)
        }

    def get_provider_wall_rows(self, now: int) -> tuple[ProviderWallRow, ...]:
        """The same live provider walls, carrying every persisted column.

        `source` and `observed_at` have been written since the walls were born
        and read by nobody: the dict getter above answers the picker, which only
        ever asks "is it cooling". A report that has to TELL an operator why a
        provider is cooling needs the rest, so the SQL lives here once and the
        dict is derived from it rather than duplicated beside it.
        """
        found = self._rows(
            "SELECT provider, cooling_until, source, observed_at "
            "FROM provider_walls WHERE cooling_until > ?",
            (now,),
        )
        return tuple(ProviderWallRow(**record) for record in found)

    def get_account_walls(self, now: int) -> dict[str, int]:
        """Return live account cooldowns keyed by roster alias. See above."""
        return {
            row.account_alias: row.cooling_until
            for row in self.get_enforced_account_wall_rows(now)
        }

    def get_enforced_account_wall_rows(self, now: int) -> tuple[AccountWallRow, ...]:
        """The walls the picker enforces: per account, the latest of its live
        causes and its exhausted stored windows.

        A stored exhausted window walls its account until its own reset, however
        stale: the vendor named the instant it answers again, and an account the
        picker never runs is never re-read. Measured 2026-10-03: codex-me at 1.0
        until 10-25 was re-picked each hour its default wall lifted, and the
        quota report showed that hour. Both read this one merge.
        """
        walls: dict[str, AccountWallRow] = {}
        for wall in (
            *self.get_account_wall_rows(now),
            *self.get_exhausted_walls(now).values(),
        ):
            held = walls.get(wall.account_alias)
            if held is None or wall.cooling_until > held.cooling_until:
                walls[wall.account_alias] = wall
        return tuple(walls.values())

    def get_account_wall_rows(self, now: int) -> tuple[AccountWallRow, ...]:
        """The same live account walls, carrying every persisted column."""
        found = self._rows(
            "SELECT account_alias, cooling_until, source, limit_type, observed_at "
            "FROM account_walls WHERE cooling_until > ?",
            (now,),
        )
        return tuple(AccountWallRow(**record) for record in found)

    def get_exhausted_walls(self, now: int) -> dict[str, AccountWallRow]:
        """Each account's wall from its exhausted stored windows: the latest
        future reset, observed when its newest such window was.

        Exhausted is `EXHAUSTED_UTILIZATION`. A reset at or before `now` walls
        nothing: the window rolled over, whatever it said.
        """
        found = self._rows(
            "SELECT account_alias, max(resets_at) AS cooling_until, "
            "max(observed_at) AS observed_at FROM account_usage "
            "WHERE utilization >= ? AND resets_at > ? GROUP BY account_alias",
            (EXHAUSTED_UTILIZATION, now),
        )
        return {
            record["account_alias"]: AccountWallRow(
                source="vendor_reset", limit_type="quota_exhausted", **record
            )
            for record in found
        }

    def get_account_load(self) -> dict[str, int]:
        """How many of nSPAWN's OWN runs are alive on each `executed_account`.

        The one load signal every route shares: not a vendor quota, not a wall,
        just this fleet's non-terminal runs counted per account. "Non-terminal"
        fences on `TERMINAL_PHASE_SQL`, the SAME spelling of
        `rows.TERMINAL_PHASES` that `mark_orphans` uses — a second hand-written
        list here would be a second truth about when a run stops occupying an
        account, and the first one already relabelled 501 finished runs.

        There is deliberately no `now`: a phase is not a time window. The wall
        readers take one because a cooldown EXPIRES; a run stops being load by
        changing phase, which the row already says.

        An account with no live run is ABSENT, not zero — the caller reads a
        missing key as zero, so no `COALESCE` or roster outer join belongs here.
        Rows whose `executed_account` is NULL are dropped: a run that never
        reached a vendor is load on no account, and admitting it under a `None`
        or `""` key would invent an account the picker would then route to.
        """
        found = self._rows(
            "SELECT executed_account, count(*) AS live FROM runs "
            "WHERE executed_account IS NOT NULL "
            f"AND phase NOT IN ({TERMINAL_PHASE_SQL}) "
            "GROUP BY executed_account"
        )
        return {record["executed_account"]: record["live"] for record in found}

    def get_account_last_run(self) -> dict[str, int]:
        """When the newest run on each `executed_account` started (epoch ms), live or
        ended: the picker gives a tie to the account used longest ago. An account
        with no run is absent."""
        found = self._rows(
            "SELECT executed_account, max(created_at) AS started FROM runs "
            "WHERE executed_account IS NOT NULL GROUP BY executed_account"
        )
        return {record["executed_account"]: record["started"] for record in found}

    # -- account usage -----------------------------------------------------

    def record_account_usage(self, rows: tuple[AccountUsageRow, ...]) -> None:
        """Persist observed windows, all or none, under ONE rule for every writer
        (a run's completion, a quota refresh).

        ONE commit on purpose: an account reports its windows together, and a
        half-written turn would pair this turn's five-hour reading with last
        turn's seven-day one — two instants presented as one observation.

        Each window REPLACES the stored windows of the same bucket, whatever
        the source called it (`codex.secondary`, `secondary`,
        `secondary_window`), unless one of them was observed LATER: a turn that
        completes after a refresh never overwrites the refresh's newer reading,
        and a cached quota-source answer never overwrites a run's. Observations of
        the SAME second (every reader stamps `int(time.time())`) keep the higher
        utilization, the one that errs toward walling. A bucket not in
        `rows` is kept. Only usage changes here, so the walls DERIVED from it
        follow the newest reading; a stored wall row ends only at its deadline.
        """
        if not rows:
            return
        with self._transaction():
            for alias in sorted({row.account_alias for row in rows}):
                stored = self._rows(
                    "SELECT usage_window, utilization, observed_at "
                    "FROM account_usage WHERE account_alias = ?",
                    (alias,),
                )
                # Per bucket: the newest observed_at, and the highest
                # utilization observed at it.
                newest: dict[str, tuple[int, float]] = {}
                for record in stored:
                    kind = _window_kind(record["usage_window"])
                    newest[kind] = max(
                        newest.get(kind, (0, 0.0)),
                        (record["observed_at"], record["utilization"]),
                    )
                fresh = tuple(
                    row
                    for row in rows
                    if row.account_alias == alias
                    and (row.observed_at, row.utilization)
                    >= newest.get(_window_kind(row.usage_window), (0, 0.0))
                )
                kinds = {_window_kind(row.usage_window) for row in fresh}
                self.connection.executemany(
                    "DELETE FROM account_usage "
                    "WHERE account_alias = ? AND usage_window = ?",
                    [
                        (alias, record["usage_window"])
                        for record in stored
                        if _window_kind(record["usage_window"]) in kinds
                    ],
                ).close()
                self._upsert_account_usage(fresh)

    def _upsert_account_usage(self, rows: tuple[AccountUsageRow, ...]) -> None:
        self.connection.executemany(
            "INSERT INTO account_usage"
            "(account_alias, usage_window, utilization, resets_at, observed_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (account_alias, usage_window) DO UPDATE SET "
            "utilization = excluded.utilization, "
            "resets_at = excluded.resets_at, "
            "observed_at = excluded.observed_at",
            [
                (
                    row.account_alias,
                    row.usage_window,
                    row.utilization,
                    row.resets_at,
                    row.observed_at,
                )
                for row in rows
            ],
        ).close()

    def record_compute_reading(self, row: ComputeBalanceRow) -> None:
        """Store a good reading and clear the last failure.

        Raises:
            StaleComputeReading: a newer reading is stored; nothing is written.
        """
        if row.balance is None or row.observed_at is None:
            raise RegistryError("a compute reading to store carries no balance")
        with self._transaction():
            self._run(
                "INSERT OR IGNORE INTO compute_balance(provider) VALUES (?)",
                (row.provider,),
            )
            written = self._run(
                "UPDATE compute_balance SET balance = ?, rate_per_hour = ?, "
                "active_assignments = ?, observed_at = ?, failure = NULL, "
                "failed_at = NULL WHERE provider = ? "
                "AND (observed_at IS NULL OR observed_at <= ?)",
                (
                    row.balance,
                    row.rate_per_hour,
                    row.active_assignments,
                    row.observed_at,
                    row.provider,
                    row.observed_at,
                ),
            )
        if written != 1:
            raise StaleComputeReading(
                f"{row.provider!r}: a reading newer than {row.observed_at} is stored"
            )

    def record_compute_failure(self, provider: str, reason: str, at: int) -> None:
        """Store the last failure. Never touches the last good reading."""
        with self._transaction():
            self._run(
                "INSERT INTO compute_balance(provider, failure, failed_at) "
                "VALUES (?, ?, ?) ON CONFLICT (provider) DO UPDATE SET "
                "failure = excluded.failure, failed_at = excluded.failed_at "
                "WHERE failed_at IS NULL OR failed_at <= excluded.failed_at",
                (provider, reason[:512], at),
            )

    def get_compute_balance(self, provider: str) -> ComputeBalanceRow | None:
        """One balance source's stored row, or None when it was never pulled."""
        found = self._one(
            "SELECT provider, balance, rate_per_hour, active_assignments, "
            "observed_at, failure, failed_at FROM compute_balance "
            "WHERE provider = ?",
            (provider,),
        )
        return None if found is None else ComputeBalanceRow(**dict(found))

    def get_account_usage(self) -> tuple[AccountUsageRow, ...]:
        """Every stored usage row, filtered by nothing.

        There is deliberately no `now` parameter. Whether a window is still
        current is the READER's judgement, and a row dropped here would reach
        that reader as "never observed" — a different claim, and a false one.
        An expired reading is a fact about the past; absence is a fact about
        the store.
        """
        found = self._rows(
            "SELECT account_alias, usage_window, utilization, resets_at, observed_at "
            "FROM account_usage ORDER BY account_alias, usage_window"
        )
        return tuple(AccountUsageRow(**record) for record in found)

    def record_provider_wall(
        self,
        provider: str,
        cooling_until: int,
        source: str,
        now: int,
    ) -> None:
        """Persist one provider wall with ``now`` as ``observed_at``.

        Idempotent: a later observation for the same provider replaces the
        earlier one rather than accumulating history the table has no room for
        — `provider` is the primary key.
        """
        _validate_required(
            "provider_walls.provider", provider, MAX_DURABLE_IDENTIFIER_BYTES
        )
        _validate_wall_source(source)
        _validate_wall_time("provider_walls.cooling_until", cooling_until)
        _validate_wall_time("provider_walls.observed_at", now)
        self._write(
            "INSERT INTO provider_walls(provider, cooling_until, source, observed_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT (provider) DO UPDATE SET "
            "cooling_until = excluded.cooling_until, source = excluded.source, "
            "observed_at = excluded.observed_at "
            "WHERE excluded.cooling_until >= provider_walls.cooling_until",
            (provider, cooling_until, source, now),
        )

    def record_account_wall(
        self,
        account_alias: str,
        cooling_until: int,
        source: str,
        limit_type: str,
        now: int,
    ) -> None:
        """Persist one account-alias wall without credential material.

        One row per account, merged atomically. The deadline is the LATER of
        the two, so an in-flight failure that computed a shorter wall never
        shortens one another failure already wrote. The cause is `vendor_reset`
        only while every live cause merged into the row is: an active credential
        or default wall takes precedence, so a refresh is never read past it,
        and a live `credential_rejected` cause keeps its label whichever wall
        arrives first. That errs toward walling, by up to the quota's reset.

        `account_alias` is a roster alias, never a token path or token bytes —
        the caller's obligation, not something this method can verify beyond the
        same bounds every other identifier here carries.
        """
        _validate_required(
            "account_walls.account_alias", account_alias, MAX_DURABLE_IDENTIFIER_BYTES
        )
        _validate_required(
            "account_walls.limit_type", limit_type, MAX_DURABLE_IDENTIFIER_BYTES
        )
        _validate_wall_source(source)
        _validate_wall_time("account_walls.cooling_until", cooling_until)
        _validate_wall_time("account_walls.observed_at", now)
        self._write(
            "INSERT INTO account_walls"
            "(account_alias, cooling_until, source, limit_type, observed_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT (account_alias) DO UPDATE SET "
            "cooling_until = max(account_walls.cooling_until, excluded.cooling_until), "
            # The held row still counts only while it is live.
            "source = CASE WHEN excluded.source = 'vendor_reset' "
            "AND (account_walls.source = 'vendor_reset' "
            "OR account_walls.cooling_until <= excluded.observed_at) "
            "THEN 'vendor_reset' ELSE 'default_1h' END, "
            "limit_type = CASE WHEN account_walls.limit_type = 'credential_rejected' "
            "AND account_walls.cooling_until > excluded.observed_at "
            "THEN account_walls.limit_type "
            "WHEN excluded.limit_type = 'credential_rejected' "
            "THEN excluded.limit_type "
            "WHEN excluded.source <> 'vendor_reset' "
            "THEN excluded.limit_type "
            "WHEN account_walls.source <> 'vendor_reset' "
            "AND account_walls.cooling_until > excluded.observed_at "
            "THEN account_walls.limit_type "
            "WHEN excluded.cooling_until >= account_walls.cooling_until "
            "THEN excluded.limit_type ELSE account_walls.limit_type END, "
            "observed_at = max(account_walls.observed_at, excluded.observed_at)",
            (account_alias, cooling_until, source, limit_type, now),
        )


class UnknownRun(RegistryError):
    """No run by that id in this registry, or not this instance's to move."""


class ClosedRun(RegistryError):
    """The run is closed. A closed run is never reopened."""


class InvalidField(RegistryError):
    """A field carried a value this operation does not admit."""


def _identity_columns(row: RunRow) -> tuple[int | None, str | None, int | None]:
    """The pid triple as its three columns: all three, or all three NULL."""
    identity = row.process_identity
    if identity is None:
        return (None, None, None)
    return (identity.pid, identity.boot_id, identity.start_ticks)


def _row_from_record(record: Any) -> RunRow:
    """Rebuild a RunRow from a database row, folding the triple back together."""
    data = dict(record)
    pid = data.pop("process_pid")
    boot_id = data.pop("process_boot_id")
    start_ticks = data.pop("process_start_ticks")
    identity = (
        ProcessIdentity(pid=pid, boot_id=boot_id, start_ticks=start_ticks)
        if pid is not None
        else None
    )
    return RunRow(
        phase=RunPhase(data.pop("phase")),
        backend=Backend(data.pop("backend")),
        process_identity=identity,
        **data,
    )
