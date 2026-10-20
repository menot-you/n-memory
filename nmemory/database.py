"""The nMEMORY store: one SQLite file behind `nott_database`.

The store is `<NOTT_DATABASE_DIR>/nmemory.sqlite3` (`store_path`), created by
`create_store` from `SCHEMA`, the whole DDL as one importable constant.

Transactions. A connection is idle with `query_only=ON`. A tool call is ONE
transaction (`tool_boundary`): reads run before any write, the first declared
write (`begin_write`, which `@writes` calls) takes the write lock with
`BEGIN IMMEDIATE`, and the boundary commits once at its end, or rolls back when
the call raised. A statement that writes without declaring it fails under
`query_only` with `RefusedError` — the database refuses, not a list kept by hand.
A canonical write advances the `generation` counter once per call.

Elections. One embedding worker, one curator, one staged curator and one daily
curator per store, each by a non-blocking `flock` on `nmemory.<role>.lock` next
to the store. The kernel releases it when the holder dies.

Type conversions from the PostgreSQL schema this replaces:

- `bigint`, `integer` -> INTEGER; `double precision` -> REAL; `text` -> TEXT.
- `GENERATED ALWAYS AS IDENTITY` -> `INTEGER PRIMARY KEY` (the rowid allocates it).
- `boolean` singleton rows -> `INTEGER CHECK (singleton = 1)`.
- `timestamptz` -> INTEGER microseconds since the epoch (queue and state times,
  computed in Python, never `clock_timestamp()`); the `at` columns stay RFC 3339
  TEXT, compared through the `ts()` function this module registers.
- `halfvec(3072)` -> BLOB of 3072 little-endian float32 (12288 bytes), searched
  with sqlite-vec's `vec_distance_cosine`; the HNSW indexes are gone (exact scan).
- `read_traffic.served_ids` comma-joined text -> a JSON array.
- the `~ '^[0-9a-f]{64}$'` CHECKs -> `length(x) = 64 AND x NOT GLOB '*[^0-9a-f]*'`.
- `schema_meta`, `canonical_generation`, `embedding_events`, `alert_outbox` and
  the corpus embeddings are not carried over.
"""

from __future__ import annotations

import contextlib
import functools
import hashlib
import itertools
import json
import os
import threading
import time
from array import array
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Concatenate, NamedTuple, Protocol, Self, cast

import nott_database
from nott_database import (
    BusyError,
    Connection,
    DatabaseError,
    RefusedError,
    SchemaError,
    begin,
    finish,
    mapped,
)

from . import NMemoryError
from .capsule import format_rfc3339, parse_rfc3339
from .capture import client_key
from .lexical import TOKENIZER, lexical_frequencies, lexical_tokens
from .space import SpaceFingerprint

EMBEDDING_DIMENSION = 3072
STORE_FILE = "nmemory.sqlite3"
SCHEMA_VERSION = 1
MMAP_BYTES = 1 << 30

#: Every failure the store can raise from the database, for the few callers that
#: must catch it (bookkeeping, the protocol edge). A raw `nott_database.Error` that
#: `nott_database` does not name passes through untyped, so both are needed.
DB_ERRORS: tuple[type[Exception], ...] = (DatabaseError, nott_database.Error)

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_HEX64 = "length({c}) = 64 AND {c} NOT GLOB '*[^0-9a-f]*'"

#: Every id a non-revoked `read_traffic` row lists, with how many times such
#: rows list it, kept by triggers in the transaction that writes the row. The
#: inbox asks "served?" of this key; unpacking every row's `served_ids` instead
#: cost 7.7 s per `nmemory_context_show` (1.80 M rows, 31.8 M ids, live store
#: copy, 2026-10-04). A revoke or delete subtracts what its row added, so an
#: id is served while `n > 0`.
SERVED_DDL = """
CREATE TABLE IF NOT EXISTS served (
    capsule_id TEXT PRIMARY KEY, n INTEGER NOT NULL
) WITHOUT ROWID;
CREATE TRIGGER IF NOT EXISTS read_traffic_served_insert
AFTER INSERT ON read_traffic WHEN NEW.revoked_at IS NULL BEGIN
    INSERT INTO served (capsule_id, n)
    SELECT trim(value), 1 FROM json_each(NEW.served_ids) WHERE value IS NOT NULL
    ON CONFLICT (capsule_id) DO UPDATE SET n = n + 1;
END;
CREATE TRIGGER IF NOT EXISTS read_traffic_served_delete
AFTER DELETE ON read_traffic WHEN OLD.revoked_at IS NULL BEGIN
    INSERT INTO served (capsule_id, n)
    SELECT trim(value), -1 FROM json_each(OLD.served_ids) WHERE value IS NOT NULL
    ON CONFLICT (capsule_id) DO UPDATE SET n = n - 1;
END;
CREATE TRIGGER IF NOT EXISTS read_traffic_served_update
AFTER UPDATE OF served_ids, revoked_at ON read_traffic BEGIN
    INSERT INTO served (capsule_id, n)
    SELECT trim(value), -1 FROM json_each(OLD.served_ids)
    WHERE OLD.revoked_at IS NULL AND value IS NOT NULL
    ON CONFLICT (capsule_id) DO UPDATE SET n = n - 1;
    INSERT INTO served (capsule_id, n)
    SELECT trim(value), 1 FROM json_each(NEW.served_ids)
    WHERE NEW.revoked_at IS NULL AND value IS NOT NULL
    ON CONFLICT (capsule_id) DO UPDATE SET n = n + 1;
END;
"""

#: The counts the triggers would have kept, for a store written before them.
#: Identical arrays are grouped first (the live copy's 1.80 M rows hold under
#: 7,000 distinct ones): 2.1 s there, against 13.6 s unpacking every row.
_SERVED_BACKFILL = """
INSERT INTO served (capsule_id, n)
SELECT trim(sid.value), sum(a.k)
FROM (SELECT served_ids, count(*) AS k FROM read_traffic
      WHERE revoked_at IS NULL GROUP BY served_ids) AS a,
     json_each(a.served_ids) AS sid
WHERE sid.value IS NOT NULL
GROUP BY 1;
"""


# SQLite needs one trigger per event. Neither trigger reads traffic or parses time.
_LAST_READ_OBJECTS = {
    "last_read": """CREATE TABLE last_read (
        capsule_id TEXT PRIMARY KEY, at INTEGER NOT NULL
    ) WITHOUT ROWID""",
    "last_read_state": """CREATE TABLE last_read_state (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        covered_seq INTEGER NOT NULL, valid INTEGER NOT NULL, reason TEXT
    )""",
    "read_traffic_last_read_delete": """CREATE TRIGGER read_traffic_last_read_delete
        AFTER DELETE ON read_traffic BEGIN
        UPDATE last_read_state SET valid = 0, reason = coalesce(reason, 'traffic deleted');
    END""",
    "read_traffic_last_read_update": """CREATE TRIGGER read_traffic_last_read_update
        AFTER UPDATE OF served_ids, at, revoked_at ON read_traffic BEGIN
        UPDATE last_read_state SET valid = 0, reason = coalesce(reason, 'traffic updated');
    END""",
}
LAST_READ_DDL = ";\n".join(_LAST_READ_OBJECTS.values()) + ";\n"
LAST_READ_DDL += "INSERT INTO last_read_state VALUES (1, 0, 1, NULL);\n"
LAST_READ_CHUNK_SIZE = 1000
_LAST_READ_UPSERT = """
INSERT INTO last_read (capsule_id, at) VALUES (?, ?)
ON CONFLICT (capsule_id) DO UPDATE SET at = max(at, excluded.at)
"""

#: The term lane's inverted index. `term_documents` holds the token count of
#: each live capsule. `term_postings` holds, for each word, the capsules that
#: contain it and how often. The posting count of a word is its document
#: frequency, so a query reads the postings of its own words and never tests
#: every capsule. `CapsuleRepository.insert` and `forget_capsule` keep the index
#: in their own transaction, and only when the stored `tokenizer` is theirs.
#: The triggers mark the index invalid when another statement changes,
#: deletes or moves an indexed capsule, or inserts one at or below
#: `covered_seq`. An insert above `covered_seq` leaves the index behind the
#: newest capsule. `valid` is 1 for an index of every capsule through
#: `covered_seq`, 2 while a build has written the postings through
#: (`cursor`, `cursor_seq`), and 0 after an invalidation. Recall reads the index
#: only when `valid` is 1, it covers the newest capsule and its `tokenizer` is
#: this build's `TOKENIZER`. In all other cases recall scans every capsule.
_TERM_INDEX_OBJECTS = {
    "term_postings": """CREATE TABLE term_postings (
        word TEXT NOT NULL, seq INTEGER NOT NULL, tf INTEGER NOT NULL,
        PRIMARY KEY (word, seq)
    ) WITHOUT ROWID""",
    "term_documents": """CREATE TABLE term_documents (
        seq INTEGER PRIMARY KEY, length INTEGER NOT NULL
    )""",
    "term_index_state": """CREATE TABLE term_index_state (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        covered_seq INTEGER NOT NULL, valid INTEGER NOT NULL, reason TEXT,
        cursor TEXT, cursor_seq INTEGER, tokenizer TEXT NOT NULL,
        documents INTEGER NOT NULL, tokens INTEGER NOT NULL
    )""",
    "capsules_term_index_insert": """CREATE TRIGGER capsules_term_index_insert
        AFTER INSERT ON capsules
        WHEN NEW.canonical_json IS NOT NULL
          AND NEW.seq <= (SELECT covered_seq FROM term_index_state) BEGIN
        UPDATE term_index_state SET valid = 0,
            reason = coalesce(reason, 'capsule inserted below the covered sequence');
    END""",
    # No column list: SQLite fires a column-list trigger for no update
    # through the rowid aliases (`rowid`, `_rowid_`, `oid`) of `seq`.
    "capsules_term_index_update": """CREATE TRIGGER capsules_term_index_update
        AFTER UPDATE ON capsules
        WHEN (NEW.seq IS NOT OLD.seq OR NEW.canonical_json IS NOT OLD.canonical_json)
          AND (NEW.canonical_json IS NOT NULL
            OR EXISTS (SELECT 1 FROM term_documents WHERE seq = OLD.seq)) BEGIN
        UPDATE term_index_state SET valid = 0,
            reason = coalesce(reason, 'capsule text changed outside the index');
    END""",
    "capsules_term_index_delete": """CREATE TRIGGER capsules_term_index_delete
        AFTER DELETE ON capsules
        WHEN EXISTS (SELECT 1 FROM term_documents WHERE seq = OLD.seq) BEGIN
        UPDATE term_index_state SET valid = 0,
            reason = coalesce(reason, 'capsule deleted outside the index');
    END""",
}
TERM_INDEX_DDL = ";\n".join(_TERM_INDEX_OBJECTS.values()) + ";\n"
_TERM_INDEX_STATE = "INSERT INTO term_index_state VALUES (1, ?, ?, ?, ?, ?, ?, 0, 0)"
#: Rows per build transaction. A build inserts postings in word order, so each
#: transaction appends to the end of `term_postings`.
TERM_INDEX_SLICE = 200_000
#: Capsules per catch-up transaction, for capsules after a build's snapshot.
TERM_INDEX_CHUNK_SIZE = 500

SCHEMA = f"""
CREATE TABLE generation (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    value INTEGER NOT NULL CHECK (value >= 1)
);

CREATE TABLE capsules (
    seq INTEGER PRIMARY KEY, id TEXT NOT NULL UNIQUE, canonical_json TEXT,
    created_at TEXT NOT NULL, source_hash TEXT NOT NULL UNIQUE,
    project_id TEXT NOT NULL, authority_class TEXT NOT NULL,
    valid_from TEXT NOT NULL, session_id TEXT
);
CREATE INDEX capsules_project ON capsules (project_id);
CREATE INDEX capsules_session ON capsules (session_id);

CREATE TABLE relations (
    kind TEXT NOT NULL, from_id TEXT NOT NULL, to_id TEXT NOT NULL,
    at TEXT NOT NULL, origin TEXT NOT NULL,
    PRIMARY KEY (kind, from_id, to_id)
);
CREATE INDEX relations_from ON relations (from_id);
CREATE INDEX relations_to ON relations (to_id);

-- U2: verification events. The standing of a capsule derives from these rows.
CREATE TABLE verification_events (
    seq INTEGER PRIMARY KEY,
    capsule_id TEXT NOT NULL, at TEXT NOT NULL, kind TEXT NOT NULL,
    result TEXT NOT NULL, detail TEXT NOT NULL, actor TEXT NOT NULL
);
CREATE INDEX verification_events_capsule ON verification_events (capsule_id, seq DESC);

-- U5 (system1): what the typed cheap model OBSERVED. A dedicated table, not a new
-- `kind` in `verification_events`: the curator queue takes the latest event of
-- ANY kind per capsule, and `standing_of` classifies a CLOSED set of results.
CREATE TABLE jev_observations (
    seq INTEGER PRIMARY KEY,
    capsule_id TEXT NOT NULL, at TEXT NOT NULL, question TEXT NOT NULL,
    subject TEXT NOT NULL, choice TEXT NOT NULL, p REAL NOT NULL,
    confidence REAL, model TEXT NOT NULL
);
CREATE INDEX jev_observations_question_subject ON jev_observations (question, subject);

CREATE TABLE audit_events (
    seq INTEGER PRIMARY KEY, at TEXT NOT NULL, actor TEXT NOT NULL,
    action TEXT NOT NULL, subject TEXT NOT NULL, reason TEXT,
    chained_hash TEXT NOT NULL
);
CREATE INDEX audit_subject ON audit_events (subject);
CREATE TABLE classifications (
    capsule_id TEXT PRIMARY KEY, kind TEXT NOT NULL, scope TEXT NOT NULL,
    at TEXT NOT NULL
);
CREATE TABLE tiers (capsule_id TEXT PRIMARY KEY, tier TEXT NOT NULL, at TEXT NOT NULL);
CREATE TABLE synonyms (
    term TEXT NOT NULL, alias TEXT NOT NULL, at TEXT NOT NULL,
    PRIMARY KEY (term, alias)
);
CREATE TABLE tombstones (
    capsule_id TEXT PRIMARY KEY, mode TEXT NOT NULL, content_hmac TEXT NOT NULL,
    at TEXT NOT NULL, reason TEXT NOT NULL, provenance_source TEXT,
    provenance_anchor TEXT, source_hash TEXT
);
CREATE TABLE sessions (
    session_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
    summary TEXT
);
CREATE TABLE outcomes (
    seq INTEGER PRIMARY KEY, id TEXT NOT NULL UNIQUE, description TEXT NOT NULL,
    actor TEXT NOT NULL, evidence_ref TEXT, capsule_id TEXT, receipt_id TEXT,
    score REAL, at TEXT NOT NULL
);
CREATE TABLE preferences (
    seq INTEGER PRIMARY KEY, id TEXT NOT NULL UNIQUE, preferred_id TEXT NOT NULL,
    rejected_id TEXT NOT NULL, context TEXT NOT NULL, actor TEXT NOT NULL,
    at TEXT NOT NULL
);
CREATE TABLE usage (
    capsule_id TEXT PRIMARY KEY, recall_count INTEGER NOT NULL,
    last_recalled_at TEXT NOT NULL
);
CREATE TABLE anchor_hashes (capsule_id TEXT PRIMARY KEY, hash TEXT NOT NULL, at TEXT NOT NULL);
CREATE TABLE anchor_rebind (
    capsule_id TEXT PRIMARY KEY, anchor TEXT NOT NULL, reason TEXT NOT NULL,
    at TEXT NOT NULL
);
CREATE TABLE capture_context (
    capsule_id TEXT PRIMARY KEY, harness TEXT NOT NULL, model TEXT NOT NULL,
    cwd TEXT NOT NULL, session_id TEXT NOT NULL, host TEXT NOT NULL,
    owner TEXT NOT NULL
);
CREATE TABLE epistemics (
    capsule_id TEXT PRIMARY KEY, evidence_state TEXT, proof_hint TEXT,
    stale_if TEXT, at TEXT NOT NULL
);
CREATE TABLE source_quotes (
    capsule_id TEXT PRIMARY KEY, quote TEXT NOT NULL, verdict TEXT NOT NULL,
    at TEXT NOT NULL
);
CREATE TABLE recall_misses (
    seq INTEGER PRIMARY KEY, term TEXT NOT NULL, outcome TEXT NOT NULL,
    at TEXT NOT NULL
);
-- L8: the context that makes a `recall_misses` row REPLAYABLE — the fence, the
-- lane that actually ran, and the call the row belongs to. `seq` is the SAME row.
CREATE TABLE recall_miss_context (
    seq INTEGER PRIMARY KEY, call_id TEXT NOT NULL, lane TEXT NOT NULL,
    project_id TEXT, project_prefix TEXT
);
-- Read traffic: bookkeeping ABOUT the store, recorded by every serving surface.
-- `served_ids` is a JSON array of the ids served IN THAT SURFACE'S namespace
-- (`nmemory_context_show` also serves corpus `ctx-*` ids). `client_key` is the session
-- the server derives from its own parent. `origin` is `live` or `transcript:*`
-- (a backfill), and a backfill is undone by `revoked_at`, never by a delete.
CREATE TABLE read_traffic (
    seq INTEGER PRIMARY KEY,
    surface TEXT NOT NULL, session_id TEXT NOT NULL, client_key TEXT,
    project_id TEXT, project_prefix TEXT, terms TEXT NOT NULL,
    served_ids TEXT NOT NULL CHECK (json_valid(served_ids)),
    served_count INTEGER NOT NULL, outcome TEXT, at TEXT NOT NULL,
    origin TEXT NOT NULL DEFAULT 'live', source_ref TEXT, revoked_at TEXT
);
-- Backfill idempotency: a transcript call is recorded once.
CREATE UNIQUE INDEX read_traffic_backfill_once
    ON read_traffic (origin, source_ref) WHERE origin <> 'live';
CREATE TABLE import_blocks (
    source_key TEXT NOT NULL, block_hash TEXT NOT NULL, capsule_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL, at TEXT NOT NULL,
    PRIMARY KEY (source_key, block_hash)
);
CREATE TABLE recall_receipts (
    seq INTEGER PRIMARY KEY, id TEXT NOT NULL UNIQUE, terms TEXT NOT NULL,
    returned_ids TEXT NOT NULL, project_id TEXT, project_prefix TEXT,
    session_id TEXT, at TEXT NOT NULL
);
CREATE TABLE feedback_weights (
    capsule_id TEXT PRIMARY KEY, weight REAL NOT NULL, at TEXT NOT NULL
);
CREATE TABLE lane_overrides (
    seq INTEGER PRIMARY KEY, forced TEXT NOT NULL, auto_pick TEXT NOT NULL,
    at TEXT NOT NULL
);
CREATE TABLE event_time (
    capsule_id TEXT PRIMARY KEY, event_from TEXT NOT NULL, event_to TEXT NOT NULL,
    declared_at TEXT NOT NULL
);
CREATE TABLE review_events (
    seq INTEGER PRIMARY KEY, capsule_id TEXT NOT NULL, verdict TEXT NOT NULL,
    reason TEXT NOT NULL, actor TEXT NOT NULL, at TEXT NOT NULL
);
CREATE INDEX review_events_capsule ON review_events (capsule_id, seq DESC);
CREATE TABLE pin_events (
    seq INTEGER PRIMARY KEY, capsule_id TEXT NOT NULL, pinned INTEGER NOT NULL,
    reason TEXT NOT NULL, actor TEXT NOT NULL, at TEXT NOT NULL
);
CREATE TABLE tier_events (
    seq INTEGER PRIMARY KEY, capsule_id TEXT NOT NULL, tier TEXT NOT NULL,
    at TEXT NOT NULL, reconstructed INTEGER NOT NULL
);
CREATE TABLE relation_revocations (
    seq INTEGER PRIMARY KEY, kind TEXT NOT NULL, from_id TEXT NOT NULL,
    to_id TEXT NOT NULL, asserted_at TEXT NOT NULL, revoked_at TEXT NOT NULL,
    origin TEXT NOT NULL
);
-- U4: the identity fold's undo register, kept as a record of the 2026-09-17 sweep.
CREATE TABLE project_rebind (
    seq INTEGER PRIMARY KEY, capsule_id TEXT NOT NULL, was TEXT NOT NULL,
    became TEXT NOT NULL, reason TEXT, at TEXT NOT NULL
);
CREATE TABLE dod_events (
    seq INTEGER PRIMARY KEY, capsule_id TEXT NOT NULL, axis TEXT NOT NULL,
    value TEXT NOT NULL, actor TEXT NOT NULL, reason TEXT NOT NULL,
    at TEXT NOT NULL
);
CREATE INDEX dod_events_capsule ON dod_events (capsule_id);

CREATE TABLE embedding_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    active_space TEXT NOT NULL,
    target_space TEXT,
    phase TEXT NOT NULL CHECK (phase IN ('ready', 'reindexing', 'lexical_only', 'stalled')),
    updated_at INTEGER NOT NULL
);
CREATE TABLE embedding_jobs (
    capsule_id TEXT PRIMARY KEY,
    index_text TEXT NOT NULL,
    text_fingerprint TEXT NOT NULL CHECK ({_HEX64.format(c="text_fingerprint")}),
    target_space TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    available_at INTEGER NOT NULL,
    last_error_class TEXT,
    updated_at INTEGER NOT NULL
);
CREATE INDEX embedding_jobs_ready ON embedding_jobs (available_at, capsule_id);
-- Clustered by (project, space): a fenced exact scan reads one contiguous run of
-- pages in a snapshot, which `VACUUM INTO` writes compact. `project_id` is a copy
-- of `capsules.project_id`, kept in step by every writer of that column.
CREATE TABLE capsule_embeddings (
    project_id TEXT NOT NULL,
    space_fingerprint TEXT NOT NULL,
    capsule_id TEXT NOT NULL,
    text_fingerprint TEXT NOT NULL CHECK ({_HEX64.format(c="text_fingerprint")}),
    embedding BLOB NOT NULL CHECK (length(embedding) = {EMBEDDING_DIMENSION * 4}),
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (project_id, space_fingerprint, capsule_id)
) WITHOUT ROWID;
CREATE UNIQUE INDEX capsule_embeddings_capsule ON capsule_embeddings (capsule_id);

CREATE TABLE capsule_handles (capsule_id TEXT PRIMARY KEY, prefix TEXT NOT NULL, slug TEXT);

CREATE TABLE corpus_sources (
    source_key TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('markdown', 'code', 'git', 'rustdoc')),
    root TEXT NOT NULL,
    revision TEXT NOT NULL,
    indexed_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ready', 'unavailable')),
    detail TEXT
);
CREATE TABLE corpus_chunks (
    seq INTEGER PRIMARY KEY,
    id TEXT NOT NULL UNIQUE,
    content_hash TEXT NOT NULL UNIQUE,
    content TEXT NOT NULL
);
CREATE TABLE corpus_occurrences (
    occurrence_key TEXT PRIMARY KEY CHECK ({_HEX64.format(c="occurrence_key")}),
    content_hash TEXT NOT NULL,
    source_key TEXT NOT NULL,
    project_id TEXT,
    path TEXT NOT NULL,
    start_line INTEGER,
    end_line INTEGER,
    revision TEXT NOT NULL,
    provenance TEXT NOT NULL
);
CREATE INDEX corpus_occurrences_hash ON corpus_occurrences (content_hash);
CREATE INDEX corpus_occurrences_source ON corpus_occurrences (source_key);
CREATE INDEX corpus_occurrences_project ON corpus_occurrences (project_id);
{SERVED_DDL}
{LAST_READ_DDL}
{TERM_INDEX_DDL}
INSERT INTO term_index_state VALUES (1, 0, 1, NULL, NULL, NULL, '{TOKENIZER}', 0, 0);
PRAGMA user_version = {SCHEMA_VERSION};
"""


class StoreNotInitialized(NMemoryError):
    """A singleton row the store is created with is missing."""


def ts(text: str | None) -> int | None:
    """An RFC 3339 instant as integer microseconds since the epoch (SQL `ts()`).

    Registered on every connection, so a query compares instants, not strings:
    the stored `at` text varies in width (`...12Z`, `...12.5Z`, `+00:00`), and
    text order is not time order.
    """
    if text is None:
        return None
    return (parse_rfc3339(text) - _EPOCH) // timedelta(microseconds=1)


def from_us(micros: int) -> datetime:
    """The instant `ts()` returned, as an aware datetime."""
    return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=micros)


def now_us() -> int:
    return time.time_ns() // 1000


def vector_blob(vector: Sequence[float]) -> bytes:
    """A vector as the float32 BLOB sqlite-vec reads."""
    return array("f", vector).tobytes()


def store_path() -> Path:
    """This process's store: `<NOTT_DATABASE_DIR or the live directory>/nmemory.sqlite3`."""
    return nott_database.directory() / STORE_FILE


def create_store(
    path: str | os.PathLike[str], *, allow_live_store: bool = False
) -> None:
    """Bring an empty store into existence at `path` (refused if it exists)."""
    space = SpaceFingerprint.current().as_str()
    now = now_us()
    nott_database.create(
        path,
        SCHEMA
        + "INSERT INTO generation (singleton, value) VALUES (1, 1);\n"
        + "INSERT INTO embedding_state (singleton, active_space, target_space, phase,"
        + f" updated_at) VALUES (1, {_sql_text(space)}, {_sql_text(space)},"
        + f" 'reindexing', {now});\n",
        allow_live_store=allow_live_store,
    )


def _sql_text(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _add_served(conn: Connection) -> None:
    """Give a store written before `served` the table, its triggers and counts.

    Additive, as `ensure_code_source_kind`: the version stays 1, a build that
    predates the table opens the file unchanged and its writes keep the table
    exact through the triggers, so a symlink rollback needs no step. Asked
    again under the write lock, so two openers add it once.
    """

    def missing() -> bool:
        return not nott_database.query(
            conn, "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'served'"
        )

    if missing():
        with nott_database.write(conn):
            if missing():
                conn.executescript(SERVED_DDL + _SERVED_BACKFILL)


def last_read_state(conn: Connection) -> tuple[int, int] | None:
    """Read coverage only if both tables and both invalidation triggers are intact.

    valid is 1 for complete data, 0 after invalidation, or 2 during a rebuild.
    A separate rebuild state lets a concurrent revoke invalidate a partial build.
    """
    objects = dict(
        nott_database.query(
            conn,
            "SELECT name, sql FROM sqlite_master WHERE name IN "
            "('last_read', 'last_read_state', 'read_traffic_last_read_insert', "
            "'read_traffic_last_read_delete', 'read_traffic_last_read_update')",
        )
    )
    expected = {name: " ".join(sql.split()) for name, sql in _LAST_READ_OBJECTS.items()}
    actual = {name: " ".join((sql or "").split()) for name, sql in objects.items()}
    if actual != expected:
        return None
    rows = nott_database.query(
        conn, "SELECT singleton, covered_seq, valid FROM last_read_state"
    )
    if len(rows) != 1 or rows[0][0] != 1:
        return None
    _, covered, valid = rows[0]
    if not isinstance(covered, int) or covered < 0 or valid not in (0, 1, 2):
        return None
    return covered, valid


def _last_read_times(
    conn: Connection, covered: int, maximum: int
) -> tuple[dict[str, int], str | None]:
    """Read only the domain where integer maxima equal the old SQL scan."""
    rows = nott_database.query(
        conn,
        """
        WITH traffic AS MATERIALIZED (
            SELECT served_ids, at, CASE
                WHEN typeof(served_ids) <> 'text' THEN 'served_ids is not text'
                WHEN NOT json_valid(served_ids) THEN 'served_ids is not valid JSON'
                WHEN json_type(served_ids) <> 'array' THEN 'served_ids is not an array'
                WHEN EXISTS (SELECT 1 FROM json_each(served_ids) WHERE type <> 'text')
                    THEN 'served_ids has a non-string element'
                WHEN typeof(at) <> 'text' THEN 'at is not text'
            END AS reason
            FROM read_traffic
            WHERE seq > ? AND seq <= ? AND revoked_at IS NULL
            GROUP BY served_ids, at
        )
        SELECT CAST(at AS BLOB), reason, CAST(sid.value AS BLOB) FROM traffic
        LEFT JOIN json_each(CASE WHEN reason IS NULL THEN served_ids ELSE '[]' END) sid
        """,
        (covered, maximum),
    )
    times: dict[str, int] = {}
    parsed: dict[bytes, int] = {}
    for text, reason, cid in rows:
        if reason is not None:
            return {}, reason
        if text not in parsed:
            try:
                at = ts(text.decode("utf-8"))
                assert at is not None
                from_us(at)
            except ValueError, OverflowError:
                return {}, "at is not a representable UTC time"
            parsed[text] = at
        if cid is not None:
            try:
                cid = cid.decode("utf-8")
            except UnicodeDecodeError:
                return {}, "served_ids has invalid UTF-8 text"
            at = parsed[text]
            times[cid] = max(times.get(cid, at), at)
    return times, None


def _update_last_read(conn: Connection, covered: int, maximum: int) -> bool:
    """Keep the refusal until a writable open checks the full traffic table."""
    times, reason = _last_read_times(conn, covered, maximum)
    if reason is not None:
        conn.execute("UPDATE last_read_state SET valid = 0, reason = ?", (reason,))
        return False
    conn.executemany(_LAST_READ_UPSERT, list(times.items()))
    return True


def _reset_last_read(conn: Connection) -> None:
    for name in (*_LAST_READ_OBJECTS, "read_traffic_last_read_insert"):
        kind = "TABLE" if name in ("last_read", "last_read_state") else "TRIGGER"
        conn.execute(f"DROP {kind} IF EXISTS {name}")
    conn.executescript(LAST_READ_DDL)
    conn.execute("UPDATE last_read_state SET valid = 2")


def _add_last_read(conn: Connection) -> None:
    """Build in short transactions. Readers scan until the last chunk commits."""
    state = last_read_state(conn)
    maximum = nott_database.query(
        conn, "SELECT coalesce(max(seq), 0) FROM read_traffic"
    )[0][0]
    if state == (maximum, 1):
        return
    first = True
    while True:
        with nott_database.write(conn):
            state = last_read_state(conn)
            maximum = nott_database.query(
                conn, "SELECT coalesce(max(seq), 0) FROM read_traffic"
            )[0][0]
            if state == (maximum, 1):
                return
            if (
                state is None
                or state[1] == 0
                or state[0] > maximum
                or (first and state[1] == 2)
            ):
                _reset_last_read(conn)
                covered = 0
            else:
                covered = state[0]
                conn.execute("UPDATE last_read_state SET valid = 2")
            first = False
            rows = nott_database.query(
                conn,
                "SELECT seq FROM read_traffic WHERE seq > ? ORDER BY seq LIMIT ?",
                (covered, LAST_READ_CHUNK_SIZE),
            )
            end = rows[-1][0] if rows else maximum
            if not _update_last_read(conn, covered, end):
                return
            covered = end
            complete = covered == maximum
            conn.execute(
                "UPDATE last_read_state SET covered_seq = ?, valid = ?",
                (covered, 1 if complete else 2),
            )
            if complete:
                return


class _TermIndexState(NamedTuple):
    covered_seq: int
    valid: int
    cursor: str | None
    cursor_seq: int | None
    tokenizer: str
    documents: int
    tokens: int


def _term_index_state(conn: Connection) -> _TermIndexState | None:
    """The index state if every object of the index is intact, else None."""
    objects = dict(
        nott_database.query(
            conn,
            "SELECT name, sql FROM sqlite_master WHERE name IN (SELECT value FROM json_each(?))",
            (json.dumps(list(_TERM_INDEX_OBJECTS)),),
        )
    )
    expected = {
        name: " ".join(sql.split()) for name, sql in _TERM_INDEX_OBJECTS.items()
    }
    actual = {name: " ".join((sql or "").split()) for name, sql in objects.items()}
    if actual != expected:
        return None
    rows = nott_database.query(
        conn,
        "SELECT covered_seq, valid, cursor, cursor_seq, tokenizer, documents, tokens"
        " FROM term_index_state",
    )
    return _TermIndexState(*rows[0]) if len(rows) == 1 else None


def _newest_seq(conn: Connection) -> int:
    return int(
        nott_database.query(conn, "SELECT coalesce(max(seq), 0) FROM capsules")[0][0]
    )


def term_index(conn: Connection) -> tuple[int, int] | None:
    """`(documents, tokens)` of a complete term index, else None (scan instead)."""
    state = _term_index_state(conn)
    if (
        state is None
        or state.valid != 1
        or state.tokenizer != TOKENIZER
        or state.covered_seq < _newest_seq(conn)
    ):
        return None
    return state.documents, state.tokens


def _invalidate_term_index(conn: Connection, reason: str) -> None:
    with mapped():
        conn.execute(
            "UPDATE term_index_state SET valid = 0, reason = coalesce(reason, ?)",
            (reason,),
        )


def _reset_term_index(conn: Connection, covered: int, valid: int) -> None:
    """Drop and create every object of the index, empty, under the write lock."""
    for name in _TERM_INDEX_OBJECTS:
        kind = "TRIGGER" if name.startswith("capsules_") else "TABLE"
        conn.execute(f"DROP {kind} IF EXISTS {name}")
    conn.executescript(TERM_INDEX_DDL)
    cursor = "" if valid == 2 else None
    seq = 0 if valid == 2 else None
    reason = "index not built" if valid == 0 else None
    conn.execute(_TERM_INDEX_STATE, (covered, valid, reason, cursor, seq, TOKENIZER))


def _add_term_index_objects(conn: Connection) -> None:
    """Give every writable open the index objects before any writer needs them.

    A store written before the index gets them empty and invalid. Its writers
    then find the tables, and recall scans until a build completes.
    """
    if _term_index_state(conn) is None:
        with nott_database.write(conn):
            if _term_index_state(conn) is None:
                _reset_term_index(conn, 0, 0)


def index_term_documents(conn: Connection, rows: Sequence[tuple[int, str]]) -> None:
    """Add `(seq, content)` capsules to the index and its totals."""
    postings = sorted(
        (word, seq, tf)
        for seq, content in rows
        for word, tf in lexical_frequencies(content).items()
    )
    with mapped():
        conn.executemany("INSERT INTO term_postings VALUES (?, ?, ?)", postings)
        _add_term_documents(
            conn, [(seq, len(lexical_tokens(content))) for seq, content in rows]
        )


def _add_term_documents(conn: Connection, lengths: Sequence[tuple[int, int]]) -> None:
    conn.executemany("INSERT INTO term_documents VALUES (?, ?)", lengths)
    conn.execute(
        "UPDATE term_index_state SET documents = documents + ?, tokens = tokens + ?",
        (len(lengths), sum(length for _, length in lengths)),
    )


def unindex_term_document(conn: Connection, seq: int) -> None:
    """Remove one capsule from the index and its totals, before its text goes.

    Only an index of this build's tokenizer is edited: the tokens of another
    tokenizer would leave postings behind, so that index is marked invalid.
    """
    state = _term_index_state(conn)
    if state is None or state.valid == 0:
        return
    if state.tokenizer != TOKENIZER:
        _invalidate_term_index(conn, "capsule forgotten by another tokenizer")
        return
    rows = nott_database.query(
        conn,
        "SELECT d.length, c.canonical_json ->> '$.content' FROM term_documents d"
        " JOIN capsules c ON c.seq = d.seq WHERE d.seq = ?",
        (seq,),
    )
    if not rows:
        return
    length, content = rows[0]
    with mapped():
        conn.executemany(
            "DELETE FROM term_postings WHERE word = ? AND seq = ?",
            [(word, seq) for word in lexical_frequencies(content)],
        )
        conn.execute("DELETE FROM term_documents WHERE seq = ?", (seq,))
        conn.execute(
            "UPDATE term_index_state SET documents = documents - 1, tokens = tokens - ?",
            (length,),
        )


def _building(conn: Connection, cursor: tuple[str, int]) -> bool:
    """The build still owns the state: valid 2 at the cursor it last wrote."""
    state = _term_index_state(conn)
    return (
        state is not None
        and state.valid == 2
        and state.tokenizer == TOKENIZER
        and (state.cursor, state.cursor_seq) == cursor
    )


def _build_term_index(conn: Connection) -> None:
    """Build the index of the capsules in one snapshot, in word order.

    The first transaction creates the empty index objects with `valid` 2, so
    the triggers watch every capsule before the tokens are read outside the
    write lock. A text change after that sets `valid` to 0, and every later
    transaction checks `valid` and the cursor before it writes. Each
    transaction writes at most `TERM_INDEX_SLICE` rows: first the document
    lengths, then the postings in word order, which append to the end of
    `term_postings`. A capsule forgotten during the build is skipped by the
    liveness check of each transaction. A build that stops leaves its cursor,
    and the next open continues after it.
    """
    state = _term_index_state(conn)
    if state is not None and state.valid == 1 and state.tokenizer == TOKENIZER:
        return
    if (
        state is not None
        and state.valid == 2
        and state.tokenizer == TOKENIZER
        and state.cursor is not None
        and state.cursor_seq is not None
    ):
        through, cursor = state.covered_seq, (state.cursor, state.cursor_seq)
    else:
        with nott_database.write(conn):
            through = _newest_seq(conn)
            _reset_term_index(conn, through, 2)
        cursor = ("", 0)
    lengths: list[tuple[int, int]] = []
    postings: dict[str, array[int]] = {}
    word_after, seq_after = cursor
    for seq, content in nott_database.query(
        conn,
        "SELECT seq, canonical_json ->> '$.content' FROM capsules"
        " WHERE seq <= ? AND canonical_json IS NOT NULL ORDER BY seq",
        (through,),
    ):
        lengths.append((seq, len(lexical_tokens(content))))
        for word, tf in lexical_frequencies(content).items():
            if word > word_after or (word == word_after and seq > seq_after):
                pairs = postings.get(word)
                if pairs is None:
                    pairs = postings[word] = array("q")
                pairs.extend((seq, tf))

    while True:
        with nott_database.write(conn):
            if not _building(conn, cursor):
                return
            done = nott_database.query(
                conn, "SELECT coalesce(max(seq), 0) FROM term_documents"
            )[0][0]
            batch = [row for row in lengths if row[0] > done][:TERM_INDEX_SLICE]
            if not batch:
                break
            live = {
                seq
                for (seq,) in nott_database.query(
                    conn,
                    "SELECT seq FROM capsules WHERE canonical_json IS NOT NULL"
                    " AND seq IN (SELECT value FROM json_each(?))",
                    (json.dumps([seq for seq, _ in batch]),),
                )
            }
            _add_term_documents(conn, [row for row in batch if row[0] in live])
            # A batch of only forgotten capsules still moves the build on.
            if not live:
                lengths = [row for row in lengths if row[0] > batch[-1][0]]

    def ordered() -> Iterator[tuple[str, int, int]]:
        for word in sorted(postings):
            pairs = postings.pop(word)
            for i in range(0, len(pairs), 2):
                yield word, pairs[i], pairs[i + 1]

    rows = ordered()
    while True:
        batch_rows = list(itertools.islice(rows, TERM_INDEX_SLICE))
        with nott_database.write(conn):
            if not _building(conn, cursor):
                return
            if not batch_rows:
                conn.execute(
                    "UPDATE term_index_state SET valid = 1, cursor = NULL,"
                    " cursor_seq = NULL"
                )
                return
            live = {
                seq
                for (seq,) in nott_database.query(
                    conn, "SELECT seq FROM term_documents"
                )
            }
            conn.executemany(
                "INSERT INTO term_postings VALUES (?, ?, ?)",
                [row for row in batch_rows if row[1] in live],
            )
            cursor = (batch_rows[-1][0], batch_rows[-1][1])
            conn.execute(
                "UPDATE term_index_state SET cursor = ?, cursor_seq = ?", cursor
            )


def _add_term_index(conn: Connection) -> None:
    """Build the index if needed, then index the capsules written after it.

    The catch-up runs in short transactions of `TERM_INDEX_CHUNK_SIZE`
    capsules, so an older build's inserts reach the index on the next open.
    """
    _build_term_index(conn)
    while term_index(conn) is None:
        with nott_database.write(conn):
            state = _term_index_state(conn)
            if state is None or state.valid != 1 or state.tokenizer != TOKENIZER:
                return
            rows = nott_database.query(
                conn,
                "SELECT seq, canonical_json ->> '$.content' FROM capsules"
                " WHERE seq > ? ORDER BY seq LIMIT ?",
                (state.covered_seq, TERM_INDEX_CHUNK_SIZE),
            )
            if not rows:
                return
            # A forgotten capsule has no text and stays out of the index.
            index_term_documents(
                conn, [(seq, content) for seq, content in rows if content is not None]
            )
            conn.execute("UPDATE term_index_state SET covered_seq = ?", (rows[-1][0],))


def _adapt(value: Any) -> Any:
    if isinstance(value, (list, tuple, set, frozenset)):
        return json.dumps(list(value))
    if isinstance(value, datetime):
        return format_rfc3339(value)
    return value


def _adapt_params(params: Any) -> Any:
    if isinstance(params, dict):
        return {k: _adapt(v) for k, v in params.items()}
    return tuple(_adapt(v) for v in params)


class Cursor:
    """A cursor that closes with its block and adapts lists and datetimes.

    A list (or tuple, set) parameter is bound as a JSON array, read in SQL with
    `IN (SELECT value FROM json_each(?))`; a datetime is bound as RFC 3339 text.
    """

    def __init__(self, conn: nott_database.Connection) -> None:
        self._cur = conn.cursor()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self._cur.close()

    def execute(self, sql: str, params: Any = ()) -> Self:
        with mapped():
            self._cur.execute(sql, _adapt_params(params))
        return self

    def executemany(self, sql: str, seq: Sequence[Any]) -> Self:
        with mapped():
            self._cur.executemany(sql, [_adapt_params(p) for p in seq])
        return self

    def fetchone(self) -> Any:
        with mapped():
            return self._cur.fetchone()

    def fetchall(self) -> list[Any]:
        with mapped():
            return self._cur.fetchall()

    def __iter__(self) -> Iterator[Any]:
        return iter(self.fetchall())

    @property
    def rowcount(self) -> int:
        return self._cur.rowcount

    @property
    def description(self) -> Any:
        return self._cur.description


class Handle:
    """A connection for one block: the boundary's own, or a fresh one closed on exit."""

    def __init__(self, conn: Connection, owned: bool) -> None:
        self.conn = conn
        self._owned = owned

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        if self._owned:
            self.conn.close()

    def cursor(self) -> Cursor:
        return Cursor(self.conn)

    def execute(self, sql: str, params: Any = ()) -> Cursor:
        return Cursor(self.conn).execute(sql, params)


class _State(threading.local):
    conn: Connection | None = None
    began: bool = False
    bump: bool = False
    changes: int = 0
    traffic: list[tuple[Any, ...]] | None = None
    deferred: tuple[Connection, list[tuple[Any, ...]]] | None = None
    embedding_worker: bool = False
    curator: tuple[Path, int] | None = None


class Database:
    """The store file, and the transaction discipline every writer goes through."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        read_only: bool = False,
        allow_live_store: bool = False,
    ) -> None:
        self.path = Path(path).absolute()
        self.read_only = read_only
        self.allow_live_store = allow_live_store
        self._state = _State()
        # Opening once validates the file here, at construction, rather than on
        # the first tool call: a file of another schema is refused, never read.
        conn = self._open()
        try:
            version = nott_database.user_version(conn)
            if version != SCHEMA_VERSION:
                raise SchemaError(
                    f"{self.path} has schema v{version}; this build reads v{SCHEMA_VERSION}"
                )
            if not read_only:
                _add_served(conn)
                _add_last_read(conn)
                _add_term_index_objects(conn)
                # One builder per store. Another opener scans until it is done.
                with self._elect("term-index") as owner:
                    if owner:
                        _add_term_index(conn)
                if not nott_database.query(
                    conn,
                    "SELECT 1 FROM sqlite_master WHERE type = 'index' "
                    "AND name = 'review_events_capsule'",
                ):
                    with nott_database.write(conn):
                        conn.execute(
                            "CREATE INDEX IF NOT EXISTS review_events_capsule "
                            "ON review_events (capsule_id, seq DESC)"
                        )
        finally:
            conn.close()

    # -- connections ----------------------------------------------------------

    def _open(self) -> Connection:
        conn = nott_database.connect(
            self.path, readonly=self.read_only, allow_live_store=self.allow_live_store
        )
        try:
            nott_database.load_vec(conn)
            conn.create_function("ts", 1, ts, deterministic=True)
            # The exact vector scan reads every embedding of the fence; mapped, a
            # cold scan faults pages in with readahead instead of one pread each.
            # ponytail: 1 GiB is address space, not memory; raise past a 1 GiB store.
            nott_database.query(conn, f"PRAGMA mmap_size = {MMAP_BYTES}")
        except BaseException:
            conn.close()
            raise
        return conn

    def connection(self) -> Handle:
        """The boundary's connection while one is open, else a fresh one for the block."""
        conn = self._state.conn
        if conn is not None:
            return Handle(conn, owned=False)
        return Handle(self._open(), owned=True)

    # -- the tool boundary ----------------------------------------------------

    @contextlib.contextmanager
    def tool_boundary(self, *, defer_traffic: bool = False) -> Iterator[None]:
        """ONE transaction for one tool call, taken only when someone writes.

        Reentrant: the outermost boundary is the unit, so a tool that calls
        another stays one transaction and one generation. Until the call declares
        a write it reads one snapshot (a deferred read transaction, which in WAL
        never blocks a writer), so a total and the rows it counts agree; the
        write ends that snapshot and takes the write lock. On exit the write (if
        any) commits, or rolls back when the block raised.

        Read traffic recorded inside the call is written afterwards, in its own
        short write, so it is never lost with a rolled-back call. With
        `defer_traffic` it waits for `flush_traffic`, which the stdio server calls
        once the answer is on the wire: the answer never waits on the write lock
        for it.
        """
        state = self._state
        if state.conn is not None:
            yield
            return
        # A deferred row whose caller never flushed goes out before the next call.
        self.flush_traffic()
        conn = self._open()
        state.conn, state.began, state.bump, state.traffic = conn, False, False, []
        ok = False
        try:
            with mapped():
                conn.execute("BEGIN")
            yield
            ok = True
        finally:
            try:
                if not state.began:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                else:
                    # A declared write that changed no row (a sweep that found
                    # nothing, a re-check that lost the race) is not a generation.
                    # ponytail: a canonical call that only moved bookkeeping rows
                    # still bumps; over-counting only costs a reader one resync.
                    if ok and state.bump and conn.total_changes != state.changes:
                        try:
                            with mapped():
                                conn.execute("UPDATE generation SET value = value + 1")
                        except BaseException:
                            finish(conn, False)
                            raise
                    finish(conn, ok)
            finally:
                traffic = state.traffic or []
                state.conn, state.began, state.bump, state.traffic = (
                    None,
                    False,
                    False,
                    None,
                )
                # On this connection, not a second one: opening and closing
                # another cost WAL recovery and a checkpoint on every call.
                if traffic and defer_traffic:
                    state.deferred = (conn, traffic)
                else:
                    try:
                        if traffic:
                            self._write_traffic(traffic, conn)
                    finally:
                        conn.close()

    def flush_traffic(self) -> None:
        """Write the read traffic a `defer_traffic` boundary left, and close it."""
        state = self._state
        deferred, state.deferred = state.deferred, None
        if deferred is None:
            return
        conn, rows = deferred
        try:
            self._write_traffic(rows, conn)
        finally:
            conn.close()

    def begin_write(self, *, canonical: bool = True) -> None:
        """Declare a write: take the write lock once per boundary (`BEGIN IMMEDIATE`).

        `canonical` writes advance the generation at commit, and only when the
        boundary changed a row; bookkeeping (queues, observations, read traffic)
        does not.
        """
        if self.read_only:
            raise RefusedError("the store is open read-only; it cannot begin a write")
        state = self._state
        if state.conn is None:
            raise DatabaseError(
                "write attempted outside a tool boundary: begin_write requires an "
                "open tool_boundary"
            )
        if not state.began:
            # The read snapshot ends where the write lock begins.
            if state.conn.in_transaction:
                state.conn.execute("COMMIT")
            begin(state.conn)
            state.began = True
            state.changes = state.conn.total_changes
        if canonical:
            state.bump = True

    @contextlib.contextmanager
    def writing(self) -> Iterator[Handle]:
        """A bookkeeping write: join the open boundary, or run as its own."""
        with self.tool_boundary():
            self.begin_write(canonical=False)
            with self.connection() as handle:
                yield handle

    @contextlib.contextmanager
    def write_boundary(self) -> Iterator[None]:
        """A canonical write: `tool_boundary()` with the write declared at entry.

        Inside an open boundary it joins it and only declares the write (the
        lock is taken once per boundary); outside one it is its own
        transaction. The write lock is held from entry, so a re-read inside the
        block decides under the lock: the second of two racing writers waits
        for the first to commit, re-reads, and finds its row.
        """
        with self.tool_boundary():
            self.begin_write()
            yield

    def get_generation(self) -> int:
        """How many canonical write transactions the store has committed, plus one."""
        with self.connection() as handle, handle.cursor() as cur:
            row = cur.execute("SELECT value FROM generation").fetchone()
        if row is None:
            raise StoreNotInitialized("the generation row is missing")
        return int(row[0])

    # -- read traffic ---------------------------------------------------------

    def record_traffic(self, row: tuple[Any, ...]) -> None:
        """Queue one `read_traffic` row; best-effort, never fails the read.

        Inside a boundary the row waits for the boundary to end; outside one it
        is written now. Either way the failure of bookkeeping is swallowed: the
        caller already holds a correct answer.
        """
        if self.read_only:
            return
        pending = self._state.traffic
        if pending is not None:
            pending.append(row)
            return
        self._write_traffic([row])

    def record_read_traffic(
        self,
        *,
        surface: str,
        session_id: str | None,
        terms: str,
        served_ids: list[str],
        project_id: str | None,
        project_prefix: str | None,
        outcome: str | None,
        now: datetime,
    ) -> None:
        """Count a read WITHOUT making the read a canonical write.

        NOT decorated with `@writes`: an orientation read (`nmemory_context_show`,
        `nmemory_project_bootstrap`) must never take the write lock for the answer it is
        serving, nor lose that answer to a concurrent writer. The row is handed
        to `record_traffic`, which writes it after the tool boundary
        ends, in its own short write, and swallows a failure: bookkeeping that
        can fail a read is worse than bookkeeping that is occasionally missing.
        `served_ids` is stored as a JSON array.
        """
        self.record_traffic(
            (
                surface,
                session_id or "unknown",
                # Derived from this server's own parent, never asked of the
                # client: `session_id` is `unknown` for most of the fleet
                # because only Claude Code volunteers one.
                client_key(),
                project_id,
                project_prefix,
                terms,
                list(served_ids),
                len(served_ids),
                outcome,
                format_rfc3339(now),
            )
        )

    def _write_traffic(
        self, rows: list[tuple[Any, ...]], conn: Connection | None = None
    ) -> None:
        """The rows in their own transaction: on `conn` (idle) or a fresh boundary."""
        sql = """
            INSERT INTO read_traffic
                (surface, session_id, client_key, project_id,
                 project_prefix, terms, served_ids, served_count,
                 outcome, at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """

        def insert(conn: Connection) -> None:
            state = last_read_state(conn)
            previous = nott_database.query(
                conn, "SELECT coalesce(max(seq), 0) FROM read_traffic"
            )[0][0]
            with Cursor(conn) as cur:
                cur.executemany(sql, rows)
            if (
                state is not None
                and state[1] == 1
                and _update_last_read(
                    conn,
                    previous,
                    nott_database.query(
                        conn, "SELECT coalesce(max(seq), 0) FROM read_traffic"
                    )[0][0],
                )
                and state[0] == previous
            ):
                conn.execute(
                    "UPDATE last_read_state SET covered_seq = "
                    "(SELECT coalesce(max(seq), 0) FROM read_traffic)"
                )

        try:
            if conn is None:
                with self.writing() as handle:
                    insert(handle.conn)
            else:
                with nott_database.write(conn):
                    insert(conn)
        except DB_ERRORS:
            return

    # -- elections ------------------------------------------------------------

    def _lock_path(self, role: str) -> Path:
        return self.path.parent / f"nmemory.{role}.lock"

    @contextlib.contextmanager
    def _elect(self, role: str) -> Iterator[bool]:
        if self.read_only:
            yield False
            return
        with contextlib.ExitStack() as stack:
            try:
                stack.enter_context(
                    nott_database.lock(
                        self._lock_path(role),
                        wait=0,
                        allow_live_store=self.allow_live_store,
                    )
                )
                owner = True
            except BusyError:
                owner = False
            yield owner

    @property
    def owns_embedding_worker(self) -> bool:
        return self._state.embedding_worker

    @contextlib.contextmanager
    def embedding_worker_owner(self) -> Iterator[bool]:
        """Elect one embedding worker per store; the kernel frees it on death."""
        with self._elect("embedding") as owner:
            self._state.embedding_worker = owner
            try:
                yield owner
            finally:
                self._state.embedding_worker = False

    def curator_lock_alive(self) -> bool:
        """The held lock file is still the one at its path.

        A `flock` is not lost while its holder lives, but a lock file deleted
        or replaced under it lets a second curator lock the new file: the
        inode taken at election must still be the path's.
        """
        held = self._state.curator
        if held is None:
            return False
        path, inode = held
        try:
            return os.stat(path).st_ino == inode
        except FileNotFoundError:
            return False

    @contextlib.contextmanager
    def curator_owner(self) -> Iterator[bool]:
        """U4a: one curator per store, on its own lock, never the embedding one."""
        with self._elect("curator") as owner:
            if owner:
                path = self._lock_path("curator")
                self._state.curator = (path, os.stat(path).st_ino)
            try:
                yield owner
            finally:
                self._state.curator = None

    @contextlib.contextmanager
    def staged_curator_owner(self) -> Iterator[bool]:
        """One staged System1 reviewer per store, independent of the curator lock."""
        with self._elect("staged-curator") as owner:
            yield owner

    @contextlib.contextmanager
    def daily_curator_owner(self, project: str) -> Iterator[bool]:
        """One daily curator run per project (the name is hashed into the file)."""
        digest = hashlib.sha256(project.encode()).hexdigest()[:16]
        with self._elect(f"curator-daily-{digest}") as owner:
            yield owner

    # -- embeddings -----------------------------------------------------------

    # -- definition-of-done events ---------------------------------------------

    # -- maintenance ------------------------------------------------------------

    def snapshot(self, dest: str | os.PathLike[str]) -> tuple[int, int]:
        """Copy the store, compacted, into `dest` from a read-only handle, taking no
        writer lock (`nott_database.snapshot`); answer the store's free and total
        pages, `(freelist_count, page_count)`, read just before the copy."""
        # Plain `connect`, not `_open`: the copy needs neither sqlite-vec nor the map.
        conn = nott_database.connect(
            self.path, readonly=True, allow_live_store=self.allow_live_store
        )
        try:
            free = int(nott_database.query(conn, "PRAGMA freelist_count")[0][0])
            pages = int(nott_database.query(conn, "PRAGMA page_count")[0][0])
            nott_database.snapshot(conn, dest)
        finally:
            conn.close()
        return free, pages


class Declares(Protocol):
    """What writes through a `Database`: the Unit of Work and every repository."""

    db: Database


def writes[S: Declares, **P, R](
    method: Callable[Concatenate[S, P], R],
) -> Callable[Concatenate[S, P], R]:
    """Marks a method that writes to canonical tables: funneling it through the gate.

    Inside a tool call, the boundary is already open and this only declares the
    write (`begin_write`, which takes the write lock once) — an ingestion's
    sidecars remain ONE transaction and ONE generation. Outside of it (CLI,
    tests, any direct `Store` use), it opens its own boundary.

    A writer that misses the decorator does not write: the connection is
    `query_only` until a write is declared, so its statement fails with
    `RefusedError`. That refusal is the protection, pinned by
    `test_an_undeclared_write_is_refused_inside_and_outside_a_boundary`
    (tests/test_sqlite_store.py); it fires on every path a test runs, and no
    static marker test exists.
    """

    @functools.wraps(method)
    def wrapper(self: S, *args: P.args, **kwargs: P.kwargs) -> R:
        with self.db.write_boundary():
            return method(self, *args, **kwargs)

    return cast("Callable[Concatenate[S, P], R]", wrapper)
