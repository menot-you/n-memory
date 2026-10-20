"""The durable run vocabulary: what a registry row IS.

Port of the row half of `capabilities/nspawn/src/registry.rs` .

`RunPhase` and `Backend` live in `registry.rs` in the oracle, not in `wire.rs`,
and `Marker` and `RunRecord` exist NOWHERE in the member — the first port attempt
imported all four from a `wire` module the oracle does not have, so every type
here is re-derived from the oracle's real row shape.

Two structural choices are TIGHTER than the oracle's and neither changes
behaviour:

- the pid triple is ONE optional `ProcessIdentity` rather than three nullable
  columns, so the schema's all-or-nothing CHECK has no way to be violated in
  memory;
- `sandbox` and `approval_policy` stay strings (the sandbox column admits
  `policy-only`, which is not an `ExecutorSandbox`) but the constructor rejects
  anything outside the column's own CHECK set instead of waiting for the database.
"""

import json
import math
import posixpath
import re
from dataclasses import dataclass
from enum import Enum
from hashlib import sha256

from .limits import (
    MAX_DURABLE_IDENTIFIER_BYTES,
    MAX_DURABLE_OBSERVATION_BYTES,
    MAX_DURABLE_PATH_BYTES,
    MAX_DURABLE_ROW_BYTES,
    MAX_LAST_ITEM_BYTES,
)
from .process_identity import ProcessIdentity

#: The Claude confinement that is neither of the two executor sandboxes.
CLAUDE_CONFINEMENT_POLICY_ONLY = "policy-only"

#: Exactly the values the `sandbox` column's CHECK admits.
SANDBOX_VALUES = ("read-only", "workspace-write", CLAUDE_CONFINEMENT_POLICY_ONLY)

#: Exactly the values the `approval_policy` column's CHECK admits.
APPROVAL_POLICY_VALUES = ("untrusted", "on-request", "never")

#: Exactly the values the `marker` column's CHECK admits.
MARKER_VALUES = ("DONE", "BLOCKED")

#: The durable event classes from the source event journal. The PostgreSQL row
#: folds the source's open `kind` under this closed class vocabulary.
RUN_EVENT_KIND_VALUES = (
    "lifecycle",
    "item",
    "approval",
    "output",
    "file_change",
    "error",
    "warning",
    "usage",
    "recovery",
)

#: SHA-256's lowercase hexadecimal width, derived from the stdlib algorithm
#: rather than restated as a numeric limit.
SHA256_HEX_LENGTH = sha256().digest_size * 2


class RunPhase(Enum):
    """Where a run stands."""

    PENDING = "pending"
    SPAWNING = "spawning"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    IDLE = "idle"
    INTERRUPTED = "interrupted"
    RECOVERING = "recovering"
    ORPHANED = "orphaned"
    COMPLETED = "completed"
    FAILED = "failed"
    CLOSED = "closed"

    def is_terminal(self) -> bool:
        """A terminal run is one the reaper may reclaim a lease from."""
        return self in TERMINAL_PHASES


#: The ONE list of phases a run never leaves. Public because `mark_orphans`
#: fences on it too: the sweep carried its own hand-written copy naming only
#: two of the four, and 501 runs that had already finished were relabelled
#: `orphaned` — a whole verdict reading exactly like a run that never spoke.
TERMINAL_PHASES = frozenset(
    {RunPhase.ORPHANED, RunPhase.COMPLETED, RunPhase.FAILED, RunPhase.CLOSED}
)


class Backend(Enum):
    """An execution backend admitted by the durable historical registry."""

    CODEX = "codex"
    CLAUDE = "claude"
    GROK = "grok"
    AGY = "agy"
    COMMAND = "command"

    def as_str(self) -> str:
        """Canonical lowercase string as stored in the `backend` column."""
        return self.value

    @classmethod
    def parse(cls, name: str) -> Backend | None:
        """The backend a caller named, or None for a word that is not one.

        The exact inverse of `as_str`, and it MUST stay that way: the column and
        the wire spell a backend the same, so a value that round-trips the
        database also round-trips a tool call.
        """
        try:
            return cls(name)
        except ValueError:
            return None


def _bounded(name: str, value: str, maximum: int, *, minimum: int = 1) -> str:
    size = len(value.encode("utf-8"))
    if size < minimum or size > maximum:
        raise ValueError(
            f"{name} must be between {minimum} and {maximum} bytes, got {size}"
        )
    return value


def _bounded_optional(name: str, value: str | None, maximum: int) -> str | None:
    if value is None:
        return None
    return _bounded(name, value, maximum)


def validate_system_prompt(value: str) -> str:
    """Return one non-blank prompt that fits its durable PostgreSQL row."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("system_prompt must be a non-empty string")
    if "\x00" in value:
        raise ValueError("system_prompt must not contain NUL")
    return _bounded("system_prompt", value, MAX_DURABLE_ROW_BYTES)


#: The closed roster-alias grammar: lowercase ASCII alnum segments joined by a
#: single hyphen — the same shape `owner_roster.discovered_accounts` derives
#: from `<alias>.token` filenames. A path separator, a dot, or any credential
#: byte (token, key file path) falls outside it by construction.
_ACCOUNT_ALIAS_RE = re.compile(r"[a-z0-9]+([_-][a-z0-9]+)*")

#: Same ceiling as `owner_roster._valid_dev_app_slug`'s closed slug grammar.
#: A real roster alias never approaches it (the longest observed is 22 bytes);
#: it exists to keep a lowercase-alnum secret (a hex token, say) from hiding
#: inside a still-grammatical string under the generic identifier ceiling.
MAX_ACCOUNT_ALIAS_BYTES = 63


def _account_alias(name: str, value: str | None, maximum: int) -> str | None:
    """A roster account alias — never credential material.

    Refuses anything shaped like a path, a token, or otherwise outside the
    closed alias grammar. The refused value is NEVER echoed: only the field
    name and the expected shape are named.
    """
    if value is None:
        return None
    size = len(value.encode("utf-8"))
    if size < 1 or size > maximum or _ACCOUNT_ALIAS_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be a roster account alias")
    return value


def _capped_optional(name: str, value: str | None, maximum: int) -> str | None:
    """A column whose CHECK is `<= maximum` with no lower bound."""
    if value is None:
        return None
    size = len(value.encode("utf-8"))
    if size > maximum:
        raise ValueError(f"{name} must be at most {maximum} bytes, got {size}")
    return value


def _non_negative_fraction(name: str, value: float) -> float:
    """A utilization fraction, exactly as the `utilization` column admits it.

    Floor only: `isUsingOverage` makes a value above 1.0 legal, so a ceiling here
    would refuse a real overage reading. NaN is refused because PostgreSQL sorts
    it ABOVE every number — `CHECK (utilization >= 0)` admits it, and a stored NaN
    is a reading nobody can compare.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number")
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite, got {value!r}")
    if value < 0:
        raise ValueError(f"{name} must not be negative, got {value}")
    return value


def _non_negative(name: str, value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{name} must be an integer")
    if value < 0:
        raise ValueError(f"{name} must not be negative, got {value}")
    return value


def _jsonb_canonical_value(value: object) -> object:
    """One JSON value stable across PostgreSQL JSONB's numeric normalization."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, list):
        return [_jsonb_canonical_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _jsonb_canonical_value(item) for key, item in value.items()}
    return value


def _bounded_json_payload(value: object) -> object:
    try:
        encoded = json.dumps(
            _jsonb_canonical_value(value),
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError) as error:
        raise ValueError("payload must be finite JSON data") from error
    if len(encoded) > MAX_DURABLE_ROW_BYTES:
        raise ValueError(f"payload must be at most {MAX_DURABLE_ROW_BYTES} bytes")

    pending = [value]
    while pending:
        item = pending.pop()
        if item is None or isinstance(item, (str, bool, int)):
            continue
        if isinstance(item, float):
            if not math.isfinite(item):
                raise ValueError("payload must be finite JSON data")
            continue
        if isinstance(item, list):
            pending.extend(item)
            continue
        if isinstance(item, dict):
            if not all(isinstance(key, str) for key in item):
                raise ValueError("payload JSON object keys must be strings")
            pending.extend(item.values())
            continue
        raise ValueError("payload must contain only JSON data")
    return value


@dataclass(frozen=True)
class NativeSessionContext:
    """Non-secret account coordinates needed to resume a native vendor thread."""

    home: str
    sqlite_home: str | None = None
    model_provider: str | None = None
    auth_env: str | None = None
    read_roots: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        for name in ("home", "sqlite_home"):
            value = getattr(self, name)
            if value is None and name == "sqlite_home":
                continue
            if (
                not isinstance(value, str)
                or not value.startswith("/")
                or any(ord(char) < 32 or ord(char) == 127 for char in value)
            ):
                raise ValueError(f"{name} must be an absolute path without controls")
            _bounded(name, value, MAX_DURABLE_PATH_BYTES)
        if self.read_roots is not None:
            if (
                not isinstance(self.read_roots, tuple)
                or not 1 <= len(self.read_roots) <= 10
            ):
                raise ValueError("read_roots must be a tuple of one to ten roots")
            for root in self.read_roots:
                if (
                    not isinstance(root, str)
                    or not root.startswith("/")
                    or root.startswith("//")
                    or root != posixpath.normpath(root)
                    or not root.isprintable()
                ):
                    raise ValueError("read_roots must contain canonical absolute paths")
                _bounded("read_roots path", root, MAX_DURABLE_PATH_BYTES)
            if len(set(self.read_roots)) != len(self.read_roots):
                raise ValueError("read_roots must be unique")
        for name, pattern in (
            ("model_provider", r"[A-Za-z0-9][A-Za-z0-9_-]*"),
            ("auth_env", r"[A-Za-z_][A-Za-z0-9_]*"),
        ):
            value = getattr(self, name)
            if value is None:
                continue
            if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
                raise ValueError(f"{name} must be a valid name, never a credential")
            _bounded(name, value, MAX_ACCOUNT_ALIAS_BYTES)


@dataclass(frozen=True, slots=True)
class SessionReference:
    """Public conversation coordinates; account authority stays in the registry."""

    original_run_id: str
    run_ids: tuple[str, ...]
    cwd: str
    model: str
    can_prompt: bool
    owner: str | None
    active: bool


@dataclass(frozen=True, slots=True)
class SessionActivation:
    """Selected native route; preparation is passive and carries no environment."""

    provider: str
    account_route: str
    model: str
    effort: str | None = None
    profile: str | None = None
    profile_digest: str | None = None
    grant_root: str | None = None

    def __post_init__(self) -> None:
        if self.provider not in {"claude", "codex", "qwen", "agy"}:
            raise ValueError("activation requires a supported native provider")
        for name in ("account_route", "model"):
            _bounded(name, getattr(self, name), MAX_DURABLE_IDENTIFIER_BYTES)
        if not self.account_route.startswith(self.provider + ":"):
            raise ValueError("activation route must belong to its selected provider")
        for name in ("effort", "profile_digest"):
            _bounded_optional(name, getattr(self, name), MAX_DURABLE_IDENTIFIER_BYTES)
        for name in ("profile", "grant_root"):
            value = getattr(self, name)
            if value is not None:
                NativeSessionContext(value)
        if any(
            value is not None
            for value in (self.profile, self.profile_digest, self.grant_root)
        ) and (
            self.provider != "claude"
            or any(
                value is None
                for value in (self.profile, self.profile_digest, self.grant_root)
            )
        ):
            raise ValueError(
                "activation requires the complete Claude PRELAUNCH binding"
            )

    def data(self) -> dict[str, str | None]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}

    @classmethod
    def from_request(cls, value: object) -> SessionActivation:
        if not isinstance(value, dict) or set(value) - set(cls.__dataclass_fields__):
            raise ValueError("activation contains unsupported fields")
        if not {"provider", "account_route", "model"}.issubset(value):
            raise ValueError(
                "activation requires its provider, account route and model"
            )
        if any(
            not isinstance(item, str) and item is not None for item in value.values()
        ):
            raise ValueError("activation fields must be text or absent")
        return cls(**value)


@dataclass(frozen=True, slots=True)
class SessionProfile:
    """The immutable grants and retained history of a supervised conversation."""

    original_run_id: str
    native_write: bool
    read_only: bool
    read_paths: tuple[str, ...]
    config_dir: str | None = None
    execution_class: str | None = None
    timeout_ms: int = 0

    def __post_init__(self) -> None:
        if (
            not isinstance(self.original_run_id, str)
            or not self.original_run_id.strip()
        ):
            raise ValueError("session profile requires an original run")
        _bounded("original_run_id", self.original_run_id, MAX_DURABLE_IDENTIFIER_BYTES)
        if type(self.native_write) is not bool or type(self.read_only) is not bool:
            raise ValueError("session profile requires boolean grants")
        if self.native_write == self.read_only:
            raise ValueError("session profile requires exactly one confinement mode")
        if not isinstance(self.read_paths, tuple):
            raise TypeError("session profile requires a read_paths tuple")
        NativeSessionContext("/", read_roots=self.read_paths or None)
        if self.config_dir is not None:
            NativeSessionContext(self.config_dir)
        _bounded_optional(
            "execution_class", self.execution_class, MAX_DURABLE_IDENTIFIER_BYTES
        )
        _non_negative("timeout_ms", self.timeout_ms)

    def data(self) -> dict[str, object]:
        return {
            "original_run_id": self.original_run_id,
            "native_write": self.native_write,
            "read_only": self.read_only,
            "read_paths": list(self.read_paths),
            "config_dir": self.config_dir,
            "execution_class": self.execution_class,
            "timeout_ms": self.timeout_ms,
        }

    @classmethod
    def from_record(cls, record: object) -> SessionProfile:
        if (
            not isinstance(record, dict)
            or set(record)
            != {
                "original_run_id",
                "native_write",
                "read_only",
                "read_paths",
                "config_dir",
                "execution_class",
                "timeout_ms",
            }
            or not isinstance(record["read_paths"], list)
        ):
            raise ValueError("run carries an invalid session profile")
        return cls(
            record["original_run_id"],
            record["native_write"],
            record["read_only"],
            tuple(record["read_paths"]),
            record["config_dir"],
            record["execution_class"],
            record["timeout_ms"],
        )


@dataclass(frozen=True)
class ClassObservation:
    """What System1 read about a dispatch's execution class, and what it said.

    `text` is exactly what System1 was sent. `observed` and `confidence` are None
    together when System1 was unavailable or its answer was not a class it could
    stand behind; `effective` is the class the run was admitted as.
    """

    text: str
    declared: str
    observed: str | None
    confidence: float | None
    effective: str

    def __post_init__(self) -> None:
        _bounded("class_observations.text", self.text, MAX_DURABLE_ROW_BYTES)
        _bounded(
            "class_observations.declared", self.declared, MAX_DURABLE_IDENTIFIER_BYTES
        )
        _bounded(
            "class_observations.effective", self.effective, MAX_DURABLE_IDENTIFIER_BYTES
        )
        _bounded_optional(
            "class_observations.observed", self.observed, MAX_DURABLE_IDENTIFIER_BYTES
        )
        if (self.observed is None) != (self.confidence is None):
            raise ValueError("an observed class and its confidence come together")
        if self.confidence is not None and not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence must be within [0, 1], got {self.confidence}")


@dataclass(frozen=True)
class ClassLabel:
    """One finished run's class observation, labelled with its effective class."""

    run_id: str
    observed_at: int
    phase: RunPhase
    outcome_at: int
    observation: ClassObservation

    @property
    def label(self) -> str:
        return self.observation.effective


@dataclass(frozen=True)
class RunRow:
    """One durable run."""

    run_id: str
    worktree: str
    phase: RunPhase
    sandbox: str
    model: str
    owner_instance: str
    created_at: int
    updated_at: int
    backend: Backend
    thread_id: str | None = None
    group_name: str | None = None
    lane: str | None = None
    approval_policy: str = "never"
    process_group: int | None = None
    #: The pid triple as ONE value: present or absent, never half of it.
    process_identity: ProcessIdentity | None = None
    turn_count: int = 0
    last_item: str | None = None
    final_text: str | None = None
    marker: str | None = None
    blocked_reason: str | None = None
    tokens: int = 0
    pending_approvals: int = 0
    warning: str | None = None
    #: When a record last ARRIVED. READ-ONLY on this row: only the observation
    #: writer moves it, so ordinary progress cannot reset the counter that
    #: testifies about the stretch before it.
    last_record_at: int | None = None
    #: How many records have ARRIVED, across all turns. Read-only, same reason.
    records_seen: int = 0
    #: Which provider the caller requested. None means UNKNOWN, never the
    #: roster default.
    requested_provider: str | None = None
    #: The rules and persona the caller sent. Text, not a path.
    system_prompt: str | None = None
    #: Which provider actually executed the run.
    executed_provider: str | None = None
    #: The roster account alias actually used; never credential material.
    executed_account: str | None = None
    #: The node that admitted the run, `node:<session>`; None for every other
    #: caller. Written at admission only: `IMMUTABLE_COLUMNS` keeps it there.
    owner: str | None = None

    def __post_init__(self) -> None:
        _bounded("run_id", self.run_id, MAX_DURABLE_IDENTIFIER_BYTES)
        _bounded("worktree", self.worktree, MAX_DURABLE_PATH_BYTES)
        _bounded("model", self.model, MAX_DURABLE_IDENTIFIER_BYTES)
        _bounded("owner_instance", self.owner_instance, MAX_DURABLE_IDENTIFIER_BYTES)
        _bounded_optional("thread_id", self.thread_id, MAX_DURABLE_IDENTIFIER_BYTES)
        _bounded_optional("group_name", self.group_name, MAX_DURABLE_IDENTIFIER_BYTES)
        _bounded_optional("lane", self.lane, MAX_DURABLE_IDENTIFIER_BYTES)
        _bounded_optional(
            "requested_provider", self.requested_provider, MAX_DURABLE_IDENTIFIER_BYTES
        )
        if self.system_prompt is not None:
            validate_system_prompt(self.system_prompt)
        _bounded_optional(
            "executed_provider", self.executed_provider, MAX_DURABLE_IDENTIFIER_BYTES
        )
        _account_alias(
            "executed_account", self.executed_account, MAX_ACCOUNT_ALIAS_BYTES
        )
        _bounded_optional("owner", self.owner, MAX_DURABLE_IDENTIFIER_BYTES)
        _capped_optional("last_item", self.last_item, MAX_LAST_ITEM_BYTES)
        _capped_optional("final_text", self.final_text, MAX_DURABLE_OBSERVATION_BYTES)
        _capped_optional(
            "blocked_reason", self.blocked_reason, MAX_DURABLE_OBSERVATION_BYTES
        )
        _capped_optional("warning", self.warning, MAX_DURABLE_OBSERVATION_BYTES)
        if self.sandbox not in SANDBOX_VALUES:
            raise ValueError(
                f"sandbox must be one of {SANDBOX_VALUES}, got {self.sandbox!r}"
            )
        if self.approval_policy not in APPROVAL_POLICY_VALUES:
            raise ValueError(
                f"approval_policy must be one of {APPROVAL_POLICY_VALUES}, "
                f"got {self.approval_policy!r}"
            )
        if self.marker is not None and self.marker not in MARKER_VALUES:
            raise ValueError(
                f"marker must be one of {MARKER_VALUES}, got {self.marker!r}"
            )
        if self.process_group is not None and self.process_group <= 0:
            raise ValueError("process_group must be positive when present")
        for name in (
            "created_at",
            "updated_at",
            "turn_count",
            "tokens",
            "pending_approvals",
        ):
            _non_negative(name, getattr(self, name))
        if self.last_record_at is not None:
            _non_negative("last_record_at", self.last_record_at)
        _non_negative("records_seen", self.records_seen)


#: Every column of the `runs` table, in schema order.
RUN_COLUMNS = (
    "run_id",
    "thread_id",
    "worktree",
    "group_name",
    "lane",
    "phase",
    "sandbox",
    "model",
    "approval_policy",
    "process_group",
    "process_pid",
    "process_boot_id",
    "process_start_ticks",
    "owner_instance",
    "created_at",
    "updated_at",
    "turn_count",
    "last_item",
    "final_text",
    "marker",
    "blocked_reason",
    "tokens",
    "pending_approvals",
    "warning",
    "last_record_at",
    "records_seen",
    "requested_provider",
    "system_prompt",
    "backend",
    "executed_provider",
    "executed_account",
    "owner",
)

#: The observation pair is written ONLY by the observation writer. Naming the
#: exclusion here, beside the row, is what keeps a later caller from adding it to
#: an insert or an update by accident.
OBSERVATION_COLUMNS = ("last_record_at", "records_seen")

#: Columns an insert names. The observation pair is excluded.
INSERT_COLUMNS = tuple(c for c in RUN_COLUMNS if c not in OBSERVATION_COLUMNS)

#: Columns an update may move. The observation pair is excluded for the reason
#: above; `run_id`, `worktree`, `created_at`, `backend` and `owner` are immutable
#: post-insert — a run never switches execution engine, working tree or owner
#: mid-life.
IMMUTABLE_COLUMNS = ("run_id", "worktree", "created_at", "backend", "owner")
UPDATE_COLUMNS = tuple(
    c
    for c in RUN_COLUMNS
    if c not in OBSERVATION_COLUMNS and c not in IMMUTABLE_COLUMNS
)


@dataclass(frozen=True)
class RunEventRow:
    """One immutable event in a run-local cryptographic chain."""

    run_id: str
    seq: int
    at: int
    kind: str
    payload: object
    chained_hash: str

    def __post_init__(self) -> None:
        _bounded("run_id", self.run_id, MAX_DURABLE_IDENTIFIER_BYTES)
        _non_negative("seq", self.seq)
        if self.seq == 0:
            raise ValueError("seq must be positive")
        _non_negative("at", self.at)
        _bounded("kind", self.kind, MAX_DURABLE_IDENTIFIER_BYTES)
        if self.kind not in RUN_EVENT_KIND_VALUES:
            raise ValueError(f"kind must be one of {RUN_EVENT_KIND_VALUES}")
        _bounded_json_payload(self.payload)
        _bounded("chained_hash", self.chained_hash, MAX_DURABLE_IDENTIFIER_BYTES)
        if (
            len(self.chained_hash) != SHA256_HEX_LENGTH
            or re.fullmatch(r"[0-9a-f]+", self.chained_hash) is None
        ):
            raise ValueError("chained_hash must be a lowercase SHA-256 digest")

    def canonical_line(self) -> str:
        """Canonical unhashed bytes represented as one deterministic JSON line."""
        return json.dumps(
            {
                "run_id": self.run_id,
                "seq": self.seq,
                "at": self.at,
                "kind": self.kind,
                "payload": _jsonb_canonical_value(self.payload),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )


#: Every column of `run_events`, in schema and insert order. There is
#: deliberately no update list: an event has no update path.
RUN_EVENT_COLUMNS = ("run_id", "seq", "at", "kind", "payload", "chained_hash")


@dataclass(frozen=True)
class WorktreeClaim:
    """One d17 worktree claim and its lease."""

    worktree: str
    run_id: str
    holder: str
    lease_until: int

    def __post_init__(self) -> None:
        _bounded("worktree", self.worktree, MAX_DURABLE_PATH_BYTES)
        _bounded("run_id", self.run_id, MAX_DURABLE_IDENTIFIER_BYTES)
        _bounded("holder", self.holder, MAX_DURABLE_IDENTIFIER_BYTES)


@dataclass(frozen=True, slots=True)
class ProviderWallRow:
    """One durable row of `nspawn.provider_walls`.

    Field names ARE the column names, so a `dict_row` record constructs this row
    with `ProviderWallRow(**record)` and no mapping layer in between.
    """

    provider: str
    cooling_until: int
    source: str
    observed_at: int

    def __post_init__(self) -> None:
        _bounded("provider", self.provider, MAX_DURABLE_IDENTIFIER_BYTES)
        _bounded("source", self.source, MAX_DURABLE_IDENTIFIER_BYTES)
        _non_negative("cooling_until", self.cooling_until)
        _non_negative("observed_at", self.observed_at)


@dataclass(frozen=True, slots=True)
class AccountWallRow:
    """One durable row of `nspawn.account_walls`. Field names are column names."""

    account_alias: str
    cooling_until: int
    source: str
    limit_type: str
    observed_at: int

    def __post_init__(self) -> None:
        _bounded("account_alias", self.account_alias, MAX_DURABLE_IDENTIFIER_BYTES)
        _bounded("source", self.source, MAX_DURABLE_IDENTIFIER_BYTES)
        _bounded("limit_type", self.limit_type, MAX_DURABLE_IDENTIFIER_BYTES)
        _non_negative("cooling_until", self.cooling_until)
        _non_negative("observed_at", self.observed_at)


@dataclass(frozen=True, slots=True)
class ComputeBalanceRow:
    """One durable row of `nspawn.compute_balance`: the last good reading (four
    fields, all None or all set) and the last failure (two fields, likewise)."""

    provider: str
    balance: float | None
    rate_per_hour: float | None
    active_assignments: int | None
    observed_at: int | None
    failure: str | None
    failed_at: int | None

    def __post_init__(self) -> None:
        reading = (
            self.balance,
            self.rate_per_hour,
            self.active_assignments,
            self.observed_at,
        )
        if len({value is None for value in reading}) != 1:
            raise ValueError("a compute reading is all set or all None")
        if (self.failure is None) != (self.failed_at is None) or self.failure == "":
            raise ValueError("a compute failure has a reason and a time, or neither")
        if any(value is not None and value < 0 for value in reading):
            raise ValueError("a compute reading is never negative")


@dataclass(frozen=True, slots=True)
class AccountUsageRow:
    """One durable row of `nspawn.account_usage`: one account, ONE window.

    The `usage_window`/`utilization`/`resets_at` triple is the durable spelling of
    `terminal.WindowUsage`, which is what the vendor's wire carries; this row adds
    the account it was observed for and when. A window is a ROW, so an account
    that reports two windows is two of these and neither overwrites the other.

    Field names are the column names, so `AccountUsageRow(**record)` constructs
    one straight from a `dict_row`.
    """

    account_alias: str
    usage_window: str
    utilization: float
    resets_at: int
    observed_at: int

    def __post_init__(self) -> None:
        _bounded("account_alias", self.account_alias, MAX_DURABLE_IDENTIFIER_BYTES)
        _bounded("usage_window", self.usage_window, MAX_DURABLE_IDENTIFIER_BYTES)
        _non_negative_fraction("utilization", self.utilization)
        _non_negative("resets_at", self.resets_at)
        _non_negative("observed_at", self.observed_at)


@dataclass(frozen=True)
class RunPage:
    """One page of runs, and where the next page starts."""

    rows: tuple[RunRow, ...]
    next_after_run: str | None = None
    truncated: bool = False
