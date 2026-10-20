"""The registry's durable shape: one SQLite file, created by `nott_database.create`.

`SCHEMA_DDL` is the whole store. It is `TABLES_DDL` then `TRIGGERS_DDL`, kept apart
so the importer can bulk-load history (events of runs already pruned included)
before the triggers exist. `PRAGMA user_version` carries `REGISTRY_SCHEMA_VERSION`.
`Registry.open` gives a version-1 store
the `class_observations` table and its trigger, a version-1 or -2 store the
`key_fences` table and its trigger, a version-1, -2 or -3 store the `owner_stamps`
table, and any older store the run `owner` and the notice `generation` with the
re-arm trigger that counts it. Version 6 adds reversible node holds, workflow
bindings, key reservations and first-terminal receipts. Version 7 extends the
backend constraint for command sessions. It preserves rows and guards
in one transaction. An older build refuses an upgraded store.

Times are integer milliseconds and uuids are text minted in Python; JSON is text
under a `json_valid` CHECK. The one clock inside the database is the notice
trigger's `ready_at`, the moment the run became terminal.
"""

from typing import Final

from .limits import (
    MAX_DURABLE_IDENTIFIER_BYTES,
    MAX_DURABLE_OBSERVATION_BYTES,
    MAX_DURABLE_PATH_BYTES,
    MAX_DURABLE_ROW_BYTES,
    MAX_LAST_ITEM_BYTES,
)
from .rows import (
    APPROVAL_POLICY_VALUES,
    MARKER_VALUES,
    MAX_ACCOUNT_ALIAS_BYTES,
    RUN_EVENT_KIND_VALUES,
    SANDBOX_VALUES,
    SHA256_HEX_LENGTH,
    TERMINAL_PHASES,
    Backend,
    RunPhase,
)

#: The generation this build writes and the only one it opens.
REGISTRY_SCHEMA_VERSION: Final = 7

THREAD_ORIGIN_INDEX_DDL: Final = """CREATE INDEX IF NOT EXISTS run_events_thread_origin
    ON run_events(json_extract(payload, '$.thread_origin'))
    WHERE kind = 'lifecycle' AND json_type(payload, '$.thread_origin') = 'text'"""

#: The file inside `NOTT_DATABASE_DIR` (or the account's live directory).
STORE_FILE: Final = "nspawn.sqlite3"

#: The closed provenance vocabulary both wall tables' CHECK constraints admit,
#: matching `terminal.WallSource`.
WALL_SOURCE_VALUES = ("vendor_reset", "default_1h")

#: How long a keyed closed run stays deduplicable after its close (D6 window).
#: `Registry.prune_closed_runs` keeps it; nothing else deletes runs.
KEYED_RETENTION_MS = 24 * 60 * 60 * 1000

# A verified Claude origin may resume only after its owning close. Unverified
# legacy origins still refuse at the supervisor boundary.
LEGACY_THREAD_INDEX_DDL: Final = """CREATE UNIQUE INDEX runs_legacy_thread_key
    ON runs(thread_id) WHERE backend NOT IN ('codex', 'agy') AND phase != 'closed'"""


def _quoted(values: tuple[str, ...]) -> str:
    return ",".join(f"'{value}'" for value in values)


_PHASES = tuple(phase.value for phase in RunPhase)
_BACKENDS = tuple(backend.as_str() for backend in Backend)
_TERMINAL_SQL = _quoted(tuple(sorted(phase.value for phase in TERMINAL_PHASES)))
_IDENT = MAX_DURABLE_IDENTIFIER_BYTES
_PATH = MAX_DURABLE_PATH_BYTES
_HEX = "NOT GLOB '*[^0-9a-f]*'"
# No C0 control character (NUL aside) and no DEL.
_NO_CNTRL = "NOT GLOB '*[' || char(1) || '-' || char(31) || char(127) || ']*'"

# Orphaned/interrupted still own their session: terminal for a reaper is NOT
# permission for another process to resume a vendor thread.
NATIVE_ACTIVE_SQL = "phase NOT IN ('completed', 'failed', 'closed')"


def _text(name: str, maximum: int, *, nullable: bool = False) -> str:
    bound = f"octet_length({name}) BETWEEN 1 AND {maximum}"
    if nullable:
        return f"{name} TEXT CHECK ({name} IS NULL OR {bound})"
    return f"{name} TEXT NOT NULL CHECK ({bound})"


def _ceiling(name: str, maximum: int) -> str:
    return f"{name} TEXT CHECK ({name} IS NULL OR octet_length({name}) <= {maximum})"


def _json(name: str, *, nullable: bool) -> str:
    valid = f"json_valid({name}) AND octet_length({name}) <= {MAX_DURABLE_ROW_BYTES}"
    if nullable:
        return f"{name} TEXT CHECK ({name} IS NULL OR ({valid}))"
    return f"{name} TEXT NOT NULL CHECK ({valid})"


#: Added in version 2; `Registry.open` adds it, and its trigger, to a version-1
#: store in place. No foreign key: pruning a closed run keeps its observation,
#: which carries the run's outcome itself.
CLASS_OBSERVATIONS_DDL: Final = f"""
CREATE TABLE class_observations (
    {_text("run_id", _IDENT)} PRIMARY KEY,
    {_text("text", MAX_DURABLE_ROW_BYTES)},
    {_text("declared", _IDENT)},
    {_text("observed", _IDENT, nullable=True)},
    confidence REAL CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1),
    {_text("effective", _IDENT)},
    observed_at INTEGER NOT NULL CHECK (observed_at >= 0),
    outcome TEXT CHECK (outcome IS NULL OR outcome IN ({_TERMINAL_SQL})),
    outcome_at INTEGER CHECK (outcome_at IS NULL OR outcome_at >= 0),
    CHECK ((observed IS NULL) = (confidence IS NULL)),
    CHECK ((outcome IS NULL) = (outcome_at IS NULL))
) STRICT;
"""

#: The outcome is the first terminal phase the run reaches. `orphaned` is
#: provisional, as it is for notices: a later different terminal replaces it.
#: A close after `completed` or `failed` keeps the earlier outcome.
CLASS_OUTCOME_TRIGGER_DDL: Final = f"""
CREATE TRIGGER runs_record_class_outcome AFTER UPDATE OF phase ON runs
WHEN NEW.phase IN ({_TERMINAL_SQL})
BEGIN
    UPDATE class_observations
       SET outcome = NEW.phase, outcome_at = NEW.updated_at
     WHERE run_id = NEW.run_id
       AND (outcome IS NULL OR (outcome = 'orphaned' AND NEW.phase <> 'orphaned'));
END;
"""

#: Added in version 3; `Registry.open` adds it to an older store in place. A key
#: whose claim was released naming no run: written and read under the key lock,
#: so no driver of it admits afterwards (`driver._reattach`).
KEY_FENCES_DDL: Final = f"""
CREATE TABLE key_fences (
    {_text("project", _PATH)},
    {_text("idempotency_key", _IDENT)},
    fenced_at INTEGER NOT NULL CHECK (fenced_at >= 0),
    PRIMARY KEY (project, idempotency_key)
) STRICT;
"""

#: Added in version 4; `Registry.open` adds it to an older store in place. It held
#: the digest of each owner stamp that admitted a run; nSPAWN no longer takes a
#: stamp and writes no row, and the table stays so the store version holds.
#: `IF NOT EXISTS`: a store rolled back to version 3 keeps its rows.
OWNER_STAMPS_DDL: Final = f"""
CREATE TABLE IF NOT EXISTS owner_stamps (
    digest TEXT PRIMARY KEY CHECK (octet_length(digest) = 64 AND digest {_HEX}),
    {_text("run_id", _IDENT)},
    admitted_at INTEGER NOT NULL CHECK (admitted_at >= 0)
) STRICT;
"""

#: A balance source's last good reading and its last failure, one row per
#: provider; a failure never erases the reading. Added at every open without a
#: version change: an older build ignores the table.
COMPUTE_BALANCE_DDL: Final = f"""
CREATE TABLE IF NOT EXISTS compute_balance (
    {_text("provider", _IDENT)} PRIMARY KEY,
    balance REAL CHECK (balance >= 0),
    rate_per_hour REAL CHECK (rate_per_hour >= 0),
    active_assignments INTEGER CHECK (active_assignments >= 0),
    observed_at INTEGER CHECK (observed_at >= 0),
    {_text("failure", 512, nullable=True)},
    failed_at INTEGER CHECK (failed_at >= 0),
    CHECK ((balance IS NULL) = (observed_at IS NULL)
       AND (balance IS NULL) = (rate_per_hour IS NULL)
       AND (balance IS NULL) = (active_assignments IS NULL)
       AND (failure IS NULL) = (failed_at IS NULL))
) STRICT;
"""

#: Added in version 5; `Registry.open` appends both to an older store in place.
#: The node that admitted a run (`node:<session>`), NULL for every other caller;
#: `update_run` never writes it. A notice's `generation` counts its armings.
RUN_OWNER_COLUMN: Final = _text("owner", _IDENT, nullable=True)
NOTICE_GENERATION_COLUMN: Final = (
    "generation INTEGER NOT NULL DEFAULT 0 CHECK (generation >= 0)"
)

#: The abort a fenced key's `run_keys` row meets (`registry.KeyFencedRow`).
KEY_FENCED: Final = "run_keys: the idempotency key is fenced"

#: The store refuses a fenced key's binding itself, so a driver that opened the
#: store before it carried fences, and never reads them, binds no run either.
KEY_FENCE_TRIGGER_DDL: Final = f"""
CREATE TRIGGER run_keys_refuse_fenced BEFORE INSERT ON run_keys
WHEN NEW.idempotency_key IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, '{KEY_FENCED}')
     WHERE EXISTS (SELECT 1 FROM key_fences
                    WHERE project = NEW.project
                      AND idempotency_key = NEW.idempotency_key);
END;
"""

#: Additive version 6. A rollback keeps these rows and re-upgrade creates nothing
#: twice. Receipts have no foreign key: pruning the run must keep its outcome.
ADMISSION_TABLES_DDL: Final = f"""
CREATE TABLE IF NOT EXISTS node_holds (
    {_text("node", _IDENT)} PRIMARY KEY,
    generation INTEGER NOT NULL CHECK (generation >= 0),
    stopped_at INTEGER NOT NULL CHECK (stopped_at >= 0)
) STRICT;

CREATE TABLE IF NOT EXISTS admission_bindings (
    {_text("group_name", _IDENT)} PRIMARY KEY,
    {_text("node", _IDENT)},
    {_text("project", _PATH)},
    {_text("decision", _IDENT)},
    released_generation INTEGER NOT NULL CHECK (released_generation >= 0),
    bound_at INTEGER NOT NULL CHECK (bound_at >= 0)
) STRICT;
CREATE INDEX IF NOT EXISTS admission_bindings_node_idx ON admission_bindings(node);

CREATE TABLE IF NOT EXISTS admission_keys (
    {_text("project", _PATH)},
    {_text("idempotency_key", _IDENT)},
    group_name TEXT NOT NULL REFERENCES admission_bindings(group_name),
    fingerprint TEXT NOT NULL CHECK (octet_length(fingerprint) = 64 AND fingerprint {_HEX}),
    PRIMARY KEY (project, idempotency_key)
) STRICT;

CREATE TABLE IF NOT EXISTS terminal_receipts (
    {_text("run_id", _IDENT)} PRIMARY KEY,
    phase TEXT NOT NULL CHECK (phase IN ({_TERMINAL_SQL})),
    {_ceiling("reason", MAX_DURABLE_OBSERVATION_BYTES)},
    at INTEGER NOT NULL CHECK (at >= 0)
) STRICT;
"""

ADMISSION_HELD: Final = "admission held"
ADMISSION_UNBOUND: Final = "admission outside its binding"

#: The admission's last statement, after its key, claim, notice and events. An
#: abort here rolls every one of those writes back in the enclosing transaction.
ADMISSION_TRIGGER_DDL: Final = f"""
CREATE TRIGGER IF NOT EXISTS runs_check_admission BEFORE UPDATE OF phase ON runs
WHEN OLD.phase = 'pending' AND NEW.phase = 'spawning'
BEGIN
    SELECT RAISE(ABORT, '{ADMISSION_UNBOUND}') WHERE
        EXISTS (SELECT 1 FROM run_keys k JOIN admission_keys a
                    ON a.project = k.project AND a.idempotency_key = k.idempotency_key
                 WHERE k.run_id = NEW.run_id
                   AND (a.group_name IS NOT NEW.group_name OR a.fingerprint <> k.fingerprint))
        OR (EXISTS (SELECT 1 FROM admission_bindings b WHERE b.group_name = NEW.group_name)
            AND NOT EXISTS (SELECT 1 FROM run_keys k JOIN admission_keys a
                               ON a.project = k.project AND a.idempotency_key = k.idempotency_key
                             JOIN admission_bindings b ON b.group_name = a.group_name
                            WHERE k.run_id = NEW.run_id AND a.group_name = NEW.group_name
                              AND a.fingerprint = k.fingerprint AND b.project = k.project));
    SELECT RAISE(ABORT, '{ADMISSION_HELD}') WHERE EXISTS (
        SELECT 1 FROM run_keys k JOIN admission_keys a
            ON a.project = k.project AND a.idempotency_key = k.idempotency_key
        JOIN admission_bindings b ON b.group_name = a.group_name
        JOIN node_holds h ON h.node = b.node
        WHERE k.run_id = NEW.run_id AND h.generation > b.released_generation
    );
END;
"""

#: The first observed terminal phase wins. Only orphaned is provisional, as for
#: the class outcome; close and annotations never replace completed or failed.
TERMINAL_RECEIPT_TRIGGER_DDL: Final = f"""
CREATE TRIGGER IF NOT EXISTS runs_record_terminal_receipt AFTER UPDATE OF phase ON runs
WHEN NEW.phase IN ({_TERMINAL_SQL})
 AND (OLD.phase NOT IN ({_TERMINAL_SQL})
      OR (OLD.phase = 'orphaned' AND NEW.phase <> OLD.phase))
BEGIN
    INSERT INTO terminal_receipts(run_id, phase, reason, at)
    VALUES (NEW.run_id, NEW.phase, NEW.blocked_reason, NEW.updated_at)
    ON CONFLICT (run_id) DO UPDATE SET
        phase = excluded.phase, reason = excluded.reason, at = excluded.at
    WHERE terminal_receipts.phase = 'orphaned' AND excluded.phase <> 'orphaned';
END;
"""

#: Every entry into a terminal phase re-arms the run's notices, from a
#: non-terminal phase or from `orphaned` into a different terminal, clears any
#: claim, so a stale pusher's token-fenced write matches nothing, and counts the
#: arming in `generation`, so a delivery is named by `(run, generation)`.
#: Replaced whole when a version-4 store gains the column (`Registry.open`).
NOTICE_ARM_TRIGGER_DDL: Final = f"""
CREATE TRIGGER runs_arm_notices AFTER UPDATE OF phase ON runs
WHEN NEW.phase IN ({_TERMINAL_SQL})
 AND (OLD.phase NOT IN ({_TERMINAL_SQL})
      OR (OLD.phase = 'orphaned' AND NEW.phase <> OLD.phase))
BEGIN
    UPDATE notices
       SET phase = NEW.phase,
           ready_at = CAST(unixepoch('subsec') * 1000 AS INTEGER),
           claimed_at = NULL, claim_lease_until = NULL, claim_token = NULL,
           delivered_at = NULL, generation = generation + 1
     WHERE run_id = NEW.run_id;
END;
"""

TABLES_DDL: Final = f"""
CREATE TABLE instances (
    {_text("instance_id", _IDENT)} PRIMARY KEY,
    started_at INTEGER NOT NULL CHECK (started_at >= 0),
    heartbeat_at INTEGER NOT NULL CHECK (heartbeat_at >= 0)
) STRICT;

CREATE TABLE runs (
    {_text("run_id", _IDENT)} PRIMARY KEY,
    {_text("thread_id", _IDENT, nullable=True)},
    {_text("worktree", _PATH)},
    {_text("group_name", _IDENT, nullable=True)},
    {_text("lane", _IDENT, nullable=True)},
    phase TEXT NOT NULL CHECK (phase IN ({_quoted(_PHASES)})),
    sandbox TEXT NOT NULL CHECK (sandbox IN ({_quoted(SANDBOX_VALUES)})),
    {_text("model", _IDENT)},
    approval_policy TEXT NOT NULL DEFAULT 'never'
        CHECK (approval_policy IN ({_quoted(APPROVAL_POLICY_VALUES)})),
    process_group INTEGER CHECK (process_group IS NULL OR process_group > 0),
    process_pid INTEGER CHECK (process_pid IS NULL OR process_pid > 0),
    {_text("process_boot_id", _IDENT, nullable=True)},
    process_start_ticks INTEGER
        CHECK (process_start_ticks IS NULL OR process_start_ticks >= 0),
    {_text("owner_instance", _IDENT)},
    created_at INTEGER NOT NULL CHECK (created_at >= 0),
    updated_at INTEGER NOT NULL CHECK (updated_at >= 0),
    turn_count INTEGER NOT NULL DEFAULT 0 CHECK (turn_count >= 0),
    {_ceiling("last_item", MAX_LAST_ITEM_BYTES)},
    {_ceiling("final_text", MAX_DURABLE_OBSERVATION_BYTES)},
    marker TEXT CHECK (marker IS NULL OR marker IN ({_quoted(MARKER_VALUES)})),
    {_ceiling("blocked_reason", MAX_DURABLE_OBSERVATION_BYTES)},
    tokens INTEGER NOT NULL DEFAULT 0 CHECK (tokens >= 0),
    pending_approvals INTEGER NOT NULL DEFAULT 0 CHECK (pending_approvals >= 0),
    {_ceiling("warning", MAX_DURABLE_OBSERVATION_BYTES)},
    last_record_at INTEGER CHECK (last_record_at IS NULL OR last_record_at >= 0),
    records_seen INTEGER NOT NULL DEFAULT 0 CHECK (records_seen >= 0),
    {_text("requested_provider", _IDENT, nullable=True)},
    {_text("system_prompt", MAX_DURABLE_ROW_BYTES, nullable=True)},
    backend TEXT NOT NULL CHECK (backend IN ({_quoted(_BACKENDS)})),
    {_text("executed_provider", _IDENT, nullable=True)},
    {_text("executed_account", _IDENT, nullable=True)},
    {RUN_OWNER_COLUMN},
    CHECK (
        (process_pid IS NULL AND process_boot_id IS NULL AND process_start_ticks IS NULL)
        OR
        (process_pid IS NOT NULL AND process_boot_id IS NOT NULL
         AND process_start_ticks IS NOT NULL)
    ),
    -- A native thread's origin is never NULL, so the partial unique index below
    -- needs no NULLS NOT DISTINCT.
    CHECK (
        backend NOT IN ('codex', 'agy') OR thread_id IS NULL
        OR (executed_provider IS NOT NULL AND executed_account IS NOT NULL)
    )
) STRICT;

CREATE INDEX runs_phase_idx ON runs(phase);
CREATE INDEX runs_group_idx ON runs(group_name);
CREATE INDEX runs_retention_idx ON runs(phase, updated_at, run_id);
CREATE INDEX runs_page_idx ON runs(created_at, run_id);
{LEGACY_THREAD_INDEX_DDL};
CREATE UNIQUE INDEX runs_native_thread_active_key
    ON runs(backend, executed_provider, executed_account, thread_id)
    WHERE backend IN ('codex', 'agy') AND thread_id IS NOT NULL AND {NATIVE_ACTIVE_SQL};
CREATE INDEX runs_native_thread_history_idx
    ON runs(thread_id, backend, executed_provider, executed_account,
            created_at DESC, run_id DESC)
    WHERE backend IN ('codex', 'agy');

CREATE TABLE claims (
    {_text("worktree", _PATH)} PRIMARY KEY,
    {_text("run_id", _IDENT)},
    {_text("holder", _IDENT)},
    lease_until INTEGER NOT NULL
) STRICT;
CREATE INDEX claims_run_idx ON claims(run_id);

CREATE TABLE provider_walls (
    {_text("provider", _IDENT)} PRIMARY KEY,
    cooling_until INTEGER NOT NULL CHECK (cooling_until >= 0),
    source TEXT NOT NULL CHECK (source IN ({_quoted(WALL_SOURCE_VALUES)})),
    observed_at INTEGER NOT NULL CHECK (observed_at >= 0)
) STRICT;

CREATE TABLE account_walls (
    {_text("account_alias", _IDENT)} PRIMARY KEY,
    cooling_until INTEGER NOT NULL CHECK (cooling_until >= 0),
    source TEXT NOT NULL CHECK (source IN ({_quoted(WALL_SOURCE_VALUES)})),
    {_text("limit_type", _IDENT)},
    observed_at INTEGER NOT NULL CHECK (observed_at >= 0)
) STRICT;

-- A window is a row, never a column: the vendor's window set changes. No
-- ceiling on utilization (overage is legal); the floor stays.
CREATE TABLE account_usage (
    {_text("account_alias", _IDENT)},
    {_text("usage_window", _IDENT)},
    utilization REAL NOT NULL CHECK (utilization >= 0),
    resets_at INTEGER NOT NULL CHECK (resets_at >= 0),
    observed_at INTEGER NOT NULL CHECK (observed_at >= 0),
    PRIMARY KEY (account_alias, usage_window)
) STRICT;

-- The run-local immutable event ledger. No foreign key: run retention drops
-- the mutable index row without touching its history.
CREATE TABLE run_events (
    {_text("run_id", _IDENT)},
    seq INTEGER NOT NULL CHECK (seq > 0),
    at INTEGER NOT NULL CHECK (at >= 0),
    kind TEXT NOT NULL CHECK (kind IN ({_quoted(RUN_EVENT_KIND_VALUES)})),
    {_json("payload", nullable=False)},
    chained_hash TEXT NOT NULL CHECK (
        octet_length(chained_hash) = {SHA256_HEX_LENGTH} AND chained_hash {_HEX}
    ),
    PRIMARY KEY (run_id, seq)
) STRICT;

{THREAD_ORIGIN_INDEX_DDL};

-- Controls and shapes are also refused at construction (rows.NativeSessionContext).
CREATE TABLE native_contexts (
    run_id TEXT PRIMARY KEY REFERENCES runs(run_id) ON DELETE CASCADE,
    home TEXT NOT NULL CHECK (
        octet_length(home) BETWEEN 1 AND {_PATH} AND substr(home, 1, 1) = '/'
        AND home {_NO_CNTRL}
    ),
    sqlite_home TEXT CHECK (
        sqlite_home IS NULL OR (
            octet_length(sqlite_home) BETWEEN 1 AND {_PATH}
            AND substr(sqlite_home, 1, 1) = '/'
            AND sqlite_home {_NO_CNTRL}
        )
    ),
    model_provider TEXT CHECK (
        model_provider IS NULL OR (
            octet_length(model_provider) BETWEEN 1 AND {MAX_ACCOUNT_ALIAS_BYTES}
            AND model_provider GLOB '[A-Za-z0-9]*'
            AND model_provider NOT GLOB '*[^A-Za-z0-9_-]*'
        )
    ),
    auth_env TEXT CHECK (
        auth_env IS NULL OR (
            octet_length(auth_env) BETWEEN 1 AND {MAX_ACCOUNT_ALIAS_BYTES}
            AND auth_env GLOB '[A-Za-z_]*' AND auth_env NOT GLOB '*[^A-Za-z0-9_]*'
        )
    ),
    read_roots TEXT CHECK (
        read_roots IS NULL OR (
            json_valid(read_roots) AND json_type(read_roots) = 'array'
            AND json_array_length(read_roots) BETWEEN 1 AND 10
        )
    )
) STRICT;

CREATE TABLE notices (
    {_text("run_id", _IDENT)},
    {_text("caller_addr", _PATH)},
    caller_pid INTEGER NOT NULL CHECK (caller_pid > 0),
    caller_start_ticks INTEGER NOT NULL CHECK (caller_start_ticks >= 0),
    {_text("caller_project", _PATH)},
    phase TEXT CHECK (phase IS NULL OR phase IN ({_TERMINAL_SQL})),
    created_at INTEGER NOT NULL,
    ready_at INTEGER,
    claimed_at INTEGER,
    claim_lease_until INTEGER,
    claim_token TEXT,
    delivered_at INTEGER,
    {NOTICE_GENERATION_COLUMN},
    PRIMARY KEY (run_id, caller_addr)
) STRICT;

CREATE TABLE run_keys (
    run_id TEXT PRIMARY KEY REFERENCES runs(run_id) ON DELETE CASCADE,
    launch_uuid TEXT NOT NULL UNIQUE,
    {_text("project", _PATH)},
    {_text("idempotency_key", _IDENT, nullable=True)},
    fingerprint TEXT NOT NULL CHECK (octet_length(fingerprint) = 64 AND fingerprint {_HEX}),
    prompt_sha256 TEXT NOT NULL
        CHECK (octet_length(prompt_sha256) = 64 AND prompt_sha256 {_HEX})
) STRICT;
CREATE UNIQUE INDEX run_keys_project_key ON run_keys(project, idempotency_key)
    WHERE idempotency_key IS NOT NULL;
{KEY_FENCES_DDL}{OWNER_STAMPS_DDL}{COMPUTE_BALANCE_DDL}{ADMISSION_TABLES_DDL}
CREATE TABLE run_control (
    run_id TEXT PRIMARY KEY REFERENCES runs(run_id) ON DELETE CASCADE,
    close_requested_at INTEGER,
    claimed_at INTEGER,
    {_text("claimed_by", _IDENT, nullable=True)},
    {_json("outcome", nullable=True)},
    outcome_at INTEGER
) STRICT;

-- An instance's kind (the session's stdio door or a per-call driver).
CREATE TABLE sidecar_instances (
    {_text("instance_id", _IDENT)} PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('stdio', 'driver')),
    heartbeat_at INTEGER NOT NULL CHECK (heartbeat_at >= 0)
) STRICT;

CREATE TABLE launch_reservations (
    launch_uuid TEXT PRIMARY KEY,
    reserved_at INTEGER NOT NULL
) STRICT;

-- What System1 read and answered about a run's execution class, written in the
-- admission's transaction. `observed` and `confidence` are NULL together when
-- System1 could not classify.
{CLASS_OBSERVATIONS_DDL}
-- The exporter's per-run position: re-exporting is the only cost of losing it.
CREATE TABLE export_cursors (
    run_id TEXT PRIMARY KEY,
    seq INTEGER NOT NULL CHECK (seq > 0),
    advanced_at INTEGER NOT NULL
) STRICT;
"""

TRIGGERS_DDL: Final = f"""
{CLASS_OUTCOME_TRIGGER_DDL}
{KEY_FENCE_TRIGGER_DDL}
{ADMISSION_TRIGGER_DDL}
{TERMINAL_RECEIPT_TRIGGER_DDL}
CREATE TRIGGER run_events_monotonic_seq BEFORE INSERT ON run_events
BEGIN
    SELECT RAISE(ABORT, 'run_events requires an existing run')
     WHERE NOT EXISTS (SELECT 1 FROM runs WHERE run_id = NEW.run_id);
    SELECT RAISE(ABORT, 'run_events seq must increase for its run')
     WHERE NEW.seq <= (SELECT coalesce(max(seq), 0) FROM run_events
                        WHERE run_id = NEW.run_id);
END;

CREATE TRIGGER run_events_append_only_update BEFORE UPDATE ON run_events
BEGIN
    SELECT RAISE(ABORT, 'run_events is append-only');
END;

CREATE TRIGGER run_events_append_only_delete BEFORE DELETE ON run_events
BEGIN
    SELECT RAISE(ABORT, 'run_events is append-only');
END;

-- A CHECK cannot read json_each, so the element type of read_roots is held here:
-- every root is a string, never a null, a number or a nested value.
CREATE TRIGGER native_contexts_read_roots_insert BEFORE INSERT ON native_contexts
WHEN NEW.read_roots IS NOT NULL AND json_valid(NEW.read_roots)
BEGIN
    SELECT RAISE(ABORT, 'native_contexts.read_roots holds only strings')
     WHERE EXISTS (SELECT 1 FROM json_each(NEW.read_roots) WHERE type <> 'text');
END;

CREATE TRIGGER native_contexts_read_roots_update
BEFORE UPDATE OF read_roots ON native_contexts
WHEN NEW.read_roots IS NOT NULL AND json_valid(NEW.read_roots)
BEGIN
    SELECT RAISE(ABORT, 'native_contexts.read_roots holds only strings')
     WHERE EXISTS (SELECT 1 FROM json_each(NEW.read_roots) WHERE type <> 'text');
END;

{NOTICE_ARM_TRIGGER_DDL}"""

SCHEMA_DDL: Final = (
    TABLES_DDL + TRIGGERS_DDL + f"\nPRAGMA user_version = {REGISTRY_SCHEMA_VERSION};\n"
)
