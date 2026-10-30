"""The one reader of the session journal.

Two consumers call these functions: the CLI verbs `nspawn session list|show`
and the node host, which imports this module in-process (`open_readonly`,
`sessions`, `facts`, `owned`, `related`, `bounds`, `replay_start`,
`rows_after`, `rows_before`, `active_rows`, `data_version`, `media`). No other
code reads the journal tables or the media files.

The store is opened read-only. `show` prints a session's stored rows with
its run-home identity, when present, then a footer with `total` and
`native_activity`: stored import facts or a typed refusal reason. The show
footer is the only export of `native_activity`, so a `list` entry has none and
the list reads no `journal_source` row. CLI `list` entries carry `source_home`,
the `source.home` of the session's one `session` row, or null with
`source_home_refusal` when the session has no such row or more than one, or
when the row names another session. Reads never open a native source.
Stored rows and totals stay unchanged. `list` reads `journal_session` and the
sums over `journal_usage`, never every row of a session. Its `ids` filter
searches the primary key of `journal_session`. Each read of `journal_row`
searches its primary key (vendor, session, seq), or the `journal_session_row`
index on the same columns for the `session` rows. A session whose `session`
row names a format other than `FORMAT` is refused with `JournalFormatRefused`.
"""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Collection, Sequence
from pathlib import Path
from typing import Any, Final

import nott_database

from ..registry import open_store, store_path
from . import FORMAT
from .activity import observation
from .claude import IMAGE_TYPES

TABLES = ("journal_source", "journal_session", "journal_row", "journal_usage")
#: A session's native links in source order, dropped records included.
LINKS_SQL: Final = (
    "SELECT uuid, parent_uuid, parent_known, logical_parent_uuid, src "
    "FROM journal_link WHERE vendor = ? AND session = ? ORDER BY src"
)
TOTAL_SQL = (
    "SELECT vendor, session, coalesce(sum(input), 0), coalesce(sum(cache_read), 0), "
    "coalesce(sum(cache_write), 0), coalesce(sum(output), 0), sum(reasoning), "
    "count(*) FROM journal_usage"
)
#: A session's rows after one seq, in seq order, at most `limit`.
ROWS_AFTER_SQL: Final = (
    "SELECT row FROM journal_row WHERE vendor = ? AND session = ? AND seq > ? "
    "ORDER BY seq LIMIT ?"
)
#: Rows after one seq, through the captured head, in ascending order.
ROWS_AFTER_THROUGH_SQL: Final = (
    "SELECT row FROM journal_row WHERE vendor = ? AND session = ? "
    "AND seq > ? AND seq <= ? ORDER BY seq LIMIT ?"
)
#: The nearest older rows, in descending order for the key index.
ROWS_BEFORE_SQL: Final = (
    "SELECT row FROM journal_row WHERE vendor = ? AND session = ? AND seq < ? "
    "ORDER BY seq DESC LIMIT ?"
)
#: Read both retained endpoints with one key lookup per endpoint.
BOUNDS_SQL: Final = (
    "SELECT (SELECT seq FROM journal_row WHERE vendor = ? AND session = ? "
    "ORDER BY seq LIMIT 1), "
    "(SELECT seq FROM journal_row WHERE vendor = ? AND session = ? "
    "ORDER BY seq DESC LIMIT 1)"
)
#: The last row before the most recent `limit` rows of a session.
REPLAY_START_SQL: Final = (
    "SELECT seq FROM journal_row WHERE vendor = ? AND session = ? "
    "ORDER BY seq DESC LIMIT 1 OFFSET ?"
)
#: The last omitted seq when the replay stops at the captured head.
REPLAY_START_THROUGH_SQL: Final = (
    "SELECT seq FROM journal_row WHERE vendor = ? AND session = ? AND seq <= ? "
    "ORDER BY seq DESC LIMIT 1 OFFSET ?"
)
#: A session's first owner text.
TITLE_SQL: Final = (
    "SELECT json_extract(row, '$.text') FROM journal_row "
    "WHERE vendor = ? AND session = ? AND type = 'message' "
    "AND json_extract(row, '$.role') = 'owner' "
    "AND json_extract(row, '$.text') IS NOT NULL ORDER BY seq LIMIT 1"
)
#: The journal ids of one vendor session: its copies by `vendor_id`, and a
#: session whose metadata the writer has not written yet by its id. Two
#: searches, as a UNION merges them over every session of the vendor.
VENDOR_ID_SQL: Final = (
    "SELECT id FROM journal_session WHERE vendor = ? AND vendor_id = ?",
    (
        "SELECT id FROM journal_session WHERE vendor = ? AND id = ? "
        "AND vendor_id IS NULL"
    ),
)
#: The format that a session's `session` row names.
FORMAT_SQL: Final = (
    "SELECT json_extract(row, '$.format') FROM journal_row "
    "WHERE vendor = ? AND session = ? AND type = 'session' ORDER BY seq LIMIT 1"
)
#: A session's first two `session` rows, by the `journal_session_row` index.
#: The second row, when present, shows that the first one is not unique.
SESSION_ROWS_SQL: Final = (
    "SELECT row FROM journal_row "
    "WHERE vendor = ? AND session = ? AND type = 'session' ORDER BY seq LIMIT 2"
)
#: The (vendor, id) keys of the sessions that a JSON array of ids names, for
#: one vendor: each id and each id that starts with it and a `.` character.
#: The range searches the primary key with the BINARY collation: `.` is 0x2E
#: and `/` is 0x2F, so the range holds each id that starts with the element.
IDS_SQL: Final = (
    "SELECT s.vendor, s.id FROM json_each(?) AS w CROSS JOIN journal_session AS s "
    "WHERE s.vendor = ? AND s.id >= w.value AND s.id < w.value || '/' "
    "AND (s.id = w.value OR substr(s.id, length(w.value) + 1, 1) = '.')"
)
#: The `source_home_refusal` of a session without exactly one `session` row.
NOT_UNIQUE: Final = "session row is not unique"
#: The `source_home_refusal` of a `session` row that names another session.
IDENTITY_MISMATCH: Final = "session identity does not match"
#: The length of a session's title, in characters.
TITLE_LENGTH: Final = 200
#: The keys of a `sessions` entry that `list` does not print, so its output
#: stays the cut 1 output.
HOST_ONLY: Final = frozenset({"title", "account"})
#: The sessions whose usage totals one query reads.
TOTALS_CHUNK: Final = 500
#: The rows that `show` reads in one query.
PAGE: Final = 1000
#: An image file that the writer keeps: `<sha256>.<ext>` (session-journal.v1).
MEDIA_NAME: Final = re.compile(
    rf"[0-9a-f]{{64}}\.(?:{'|'.join(sorted(IMAGE_TYPES.values()))})"
)


class Missing(Exception):
    """The session is not in the store; the message names the store's path."""


class JournalFormatRefused(Exception):
    """A session's rows have a format that this reader does not know."""


class ChainIncomplete(ValueError):
    """The active chain names a native record that the journal does not hold."""


def open_readonly() -> nott_database.Connection | None:
    """A read-only connection to this process's registry file, or None when
    the file does not exist. A file that is there and does not open raises
    `RegistryError`. A write on the connection fails."""
    path = store_path()
    if not path.exists():
        return None
    return open_store(path, readonly=True)


def data_version(connection: nott_database.Connection) -> int:
    """SQLite's `data_version`: it changes when another connection commits."""
    return int(nott_database.query(connection, "PRAGMA data_version")[0][0])


def _journal(connection: nott_database.Connection) -> bool:
    found = nott_database.query(
        connection,
        f"SELECT count(*) FROM sqlite_schema WHERE type = 'table' AND name IN "
        f"({', '.join('?' * len(TABLES))})",
        TABLES,
    )
    return int(found[0][0]) == len(TABLES)


def _check_format(
    connection: nott_database.Connection, vendor: str, session: str
) -> None:
    found = nott_database.query(connection, FORMAT_SQL, (vendor, session))
    if found and found[0][0] != FORMAT:
        raise JournalFormatRefused(
            f"{vendor} {session} has rows of format {found[0][0]}; "
            f"this reader knows format {FORMAT}"
        )


def _source_home(
    connection: nott_database.Connection, vendor: str, session: str
) -> tuple[str | None, str | None]:
    """Refuse a format other than `FORMAT`, as `_check_format` does, with
    one query. Return the `source.home` of the session's one `session` row
    and None. Return None and `NOT_UNIQUE` when the session has no `session`
    row or more than one, and None and `IDENTITY_MISMATCH` when the row's
    `vendor` or `id` is not the session's. A row without a text home gives
    (None, None)."""
    found = [
        json.loads(str(row[0]))
        for row in nott_database.query(connection, SESSION_ROWS_SQL, (vendor, session))
    ]
    first = found[0] if found and isinstance(found[0], dict) else {}
    if found and first.get("format") != FORMAT:
        raise JournalFormatRefused(
            f"{vendor} {session} has rows of format {first.get('format')}; "
            f"this reader knows format {FORMAT}"
        )
    if len(found) != 1:
        return None, NOT_UNIQUE
    if first.get("vendor") != vendor or first.get("id") != session:
        return None, IDENTITY_MISMATCH
    source = first.get("source")
    home = source.get("home") if isinstance(source, dict) else None
    return (home if isinstance(home, str) else None), None


def replay_start(
    connection: nott_database.Connection,
    vendor: str,
    session: str,
    limit: int,
    *,
    through_seq: int | None = None,
) -> int | None:
    """Return the last omitted seq, or None when all rows fit within `limit`.

    If supplied, `through_seq` excludes rows above the captured head.
    """
    if limit < 1:
        raise ValueError(f"limit must be 1 or more, not {limit}")
    if not _journal(connection):
        return None
    _check_format(connection, vendor, session)
    found = (
        nott_database.query(connection, REPLAY_START_SQL, (vendor, session, limit))
        if through_seq is None
        else nott_database.query(
            connection,
            REPLAY_START_THROUGH_SQL,
            (vendor, session, through_seq, limit),
        )
    )
    return int(found[0][0]) if found else None


def rows_after(
    connection: nott_database.Connection,
    vendor: str,
    session: str,
    seq: int,
    limit: int,
    *,
    through_seq: int | None = None,
) -> list[dict[str, Any]]:
    """The session's rows with a seq above `seq`, in seq order, at most
    `limit`. If supplied, `through_seq` is the inclusive upper bound.
    An unknown session or a store without the journal gives []."""
    if limit < 1:
        raise ValueError(f"limit must be 1 or more, not {limit}")
    if not _journal(connection):
        return []
    _check_format(connection, vendor, session)
    found = (
        nott_database.query(connection, ROWS_AFTER_SQL, (vendor, session, seq, limit))
        if through_seq is None
        else nott_database.query(
            connection,
            ROWS_AFTER_THROUGH_SQL,
            (vendor, session, seq, through_seq, limit),
        )
    )
    return [json.loads(row[0]) for row in found]


def rows_before(
    connection: nott_database.Connection,
    vendor: str,
    session: str,
    seq: int,
    limit: int,
) -> list[dict[str, Any]]:
    """Return the nearest rows below `seq` in ascending order.

    `limit` must be an integer from 1 through 200. An unknown session or a
    store without the journal gives [].
    """
    if isinstance(limit, bool) or not 1 <= limit <= 200:
        raise ValueError("limit must be an integer from 1 through 200")
    if not _journal(connection):
        return []
    _check_format(connection, vendor, session)
    found = nott_database.query(
        connection, ROWS_BEFORE_SQL, (vendor, session, seq, limit)
    )
    return [json.loads(row[0]) for row in reversed(found)]


def bounds(
    connection: nott_database.Connection, vendor: str, session: str
) -> tuple[int, int] | None:
    """Return (oldest retained seq, head seq), or None when no rows exist."""
    if not _journal(connection):
        return None
    _check_format(connection, vendor, session)
    oldest, head = nott_database.query(
        connection, BOUNDS_SQL, (vendor, session, vendor, session)
    )[0]
    return (int(oldest), int(head)) if oldest is not None else None


def active_chain(
    rows: Sequence[dict[str, Any]], links: Sequence[dict[str, Any]] = ()
) -> list[dict[str, Any]]:
    """The rows of the conversation's active branch, in stored order.

    Start at the latest native leaf and follow `parentUuid`, or at a compaction
    `logicalParentUuid`, through rows and `links` (the stored links of records
    that became no row). A row without a `parentUuid` field is legacy data and
    stays; a session without native links keeps every row in file order.
    """
    native: dict[str, dict[str, Any]] = {}
    for row in sorted([*rows, *links], key=lambda row: row.get("src", -1)):
        uuid = row.get("uuid")
        if isinstance(uuid, str) and "parentUuid" in row:
            native[uuid] = row
    if not native:
        return list(rows)
    leaf: Any = next(
        (
            row["metadata"]["leaf_uuid"]
            for row in rows
            if row.get("type") == "session"
            and isinstance(row.get("metadata"), dict)
            and row["metadata"].get("leaf_uuid")
        ),
        None,
    )
    leaf = leaf or max(native, key=lambda uuid: native[uuid].get("src", -1))
    selected: set[str] = set()
    while isinstance(leaf, str) and leaf not in selected:
        selected.add(leaf)
        record = native.get(leaf)
        if record is None:
            raise ChainIncomplete(
                f"The journal history requires the stored link {leaf}."
            )
        leaf = record.get("parentUuid") or record.get("logicalParentUuid")
    return [
        row for row in rows if "parentUuid" not in row or row.get("uuid") in selected
    ]


def active_rows(
    connection: nott_database.Connection, vendor: str, session: str
) -> list[dict[str, Any]]:
    """Every row of the session's active conversation (`active_chain`). The
    stored rows keep file order; an abandoned branch is only not returned."""
    rows: list[dict[str, Any]] = []
    while page := rows_after(
        connection, vendor, session, rows[-1]["seq"] if rows else -1, PAGE
    ):
        rows.extend(page)
    links = [
        {
            "uuid": row[0],
            **({"parentUuid": row[1]} if row[2] else {}),
            "logicalParentUuid": row[3],
            "src": row[4],
        }
        for row in (
            nott_database.query(connection, LINKS_SQL, (vendor, session))
            if rows
            else []
        )
    ]
    return active_chain(rows, links)


def _totals(
    connection: nott_database.Connection, where: str, params: Sequence[Any]
) -> dict[tuple[str, str], dict[str, Any]]:
    totals: dict[tuple[str, str], dict[str, Any]] = {}
    for row in nott_database.query(
        connection, f"{TOTAL_SQL} {where} GROUP BY vendor, session", params
    ):
        totals[(row[0], row[1])] = {
            "input": row[2],
            "cache_read": row[3],
            "cache_write": row[4],
            "output": row[5],
            "reasoning": row[6],
            "calls": row[7],
            "models": [],
        }
    for row in nott_database.query(
        connection,
        f"SELECT vendor, session, model FROM journal_usage {where}"
        f"{' AND' if where else ' WHERE'} model IS NOT NULL "
        "GROUP BY vendor, session, model ORDER BY model",
        params,
    ):
        totals[(row[0], row[1])]["models"].append(row[2])
    return totals


def _session_totals(
    connection: nott_database.Connection, keys: Sequence[tuple[str, str]]
) -> dict[tuple[str, str], dict[str, Any]]:
    """The totals of the sessions `keys` alone: each query searches the usage
    primary key for at most `TOTALS_CHUNK` sessions of one vendor."""
    by_vendor: dict[str, list[str]] = {}
    for vendor, ident in keys:
        by_vendor.setdefault(vendor, []).append(ident)
    totals: dict[tuple[str, str], dict[str, Any]] = {}
    for vendor, idents in by_vendor.items():
        for start in range(0, len(idents), TOTALS_CHUNK):
            part = idents[start : start + TOTALS_CHUNK]
            totals.update(
                _totals(
                    connection,
                    f"WHERE vendor = ? AND session IN ({', '.join('?' * len(part))})",
                    (vendor, *part),
                )
            )
    return totals


def _empty() -> dict[str, Any]:
    return {
        "input": 0,
        "cache_read": 0,
        "cache_write": 0,
        "output": 0,
        "reasoning": None,
        "calls": 0,
        "models": [],
    }


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _run(home: str | None, vendor: str, path: str) -> str | None:
    """The run owning a top-level session, using the writer's home resolution."""
    source = Path(path).parts
    if home is None or (
        vendor == "claude" and (len(source) != 3 or source[0] != "projects")
    ):
        return None
    try:
        root = (Path.home() / ".nott/state/nspawn/runs").resolve()
        parts = Path(home).resolve().relative_to(root).parts
    except OSError, RuntimeError, ValueError:
        return None
    if len(parts) == 2 and parts[0] != ".." and parts[1] == vendor:
        return parts[0]
    return None


def sessions(
    connection: nott_database.Connection,
    statuses: Collection[str] | None,
    vendor_id: tuple[str, str] | None = None,
    *,
    since: str | None = None,
    vendor: str | None = None,
    ids: Collection[str] | None = None,
    include_source_home: bool = False,
) -> list[dict[str, Any]]:
    """One entry per session with a row, newest change first: the `list` row
    (vendor, id, cwd, model, status, time, total), `title`, the first owner
    text cut to 200 characters or None, `parent`, the session id of a
    subagent's parent or None, and `account`, the roster account alias of its
    home or None. A top-level session whose resolved home is under
    `~/.nott/state/nspawn/runs/<run>/<vendor>` adds `run`, the owning run id.
    Subagents and unresolved homes have no `run`. `statuses` keeps only those statuses; None
    keeps all.
    `vendor_id`, a (vendor, native id) pair, keeps the copies of one vendor
    session, found by index. `since` and `vendor` filter before totals.
    `ids` keeps each session whose id is an element or starts with an element
    and a `.` character, by the primary key (`IDS_SQL`). It needs `vendor`, and
    a call with `ids` and no `vendor` raises `ValueError`. The filters compose
    by AND. `include_source_home` adds `source_home` and `source_home_refusal`
    (`_source_home`), from the query that checks the format.
    A store without the journal gives []."""
    if ids is not None and vendor is None:
        raise ValueError("an ids filter needs a vendor")
    if not _journal(connection):
        return []
    where = ""
    params: tuple[str, ...] = ()
    if statuses is not None:
        if not statuses:
            return []
        where = f"AND status IN ({', '.join('?' * len(statuses))}) "
        params = tuple(statuses)
    if vendor_id is not None:
        copies = sorted(
            {
                row[0]
                for sql in VENDOR_ID_SQL
                for row in nott_database.query(connection, sql, vendor_id)
            }
        )
        if not copies:
            return []
        where += f"AND vendor = ? AND id IN ({', '.join('?' * len(copies))}) "
        params += (vendor_id[0], *copies)
    if since is not None:
        where += "AND time >= ? "
        params += (since,)
    if vendor is not None:
        where += "AND vendor = ? "
        params += (vendor,)
        if ids is not None:
            where += f"AND (vendor, id) IN ({IDS_SQL}) "
            params += (json.dumps(sorted(set(ids))), vendor)
    found = nott_database.query(
        connection,
        "SELECT vendor, id, cwd, model, status, time, parent, account, home, path "
        "FROM journal_session "
        f"WHERE seq >= 0 {where}ORDER BY changed DESC, vendor, id",
        params,
    )
    totals = _session_totals(connection, [(row[0], row[1]) for row in found])
    listed: list[dict[str, Any]] = []
    for row in found:
        vendor, ident = row[0], row[1]
        source_home = (
            _source_home(connection, vendor, ident) if include_source_home else None
        )
        if source_home is None:
            _check_format(connection, vendor, ident)
        title = nott_database.query(connection, TITLE_SQL, (vendor, ident))
        listed.append(
            {
                "vendor": vendor,
                "id": ident,
                "cwd": row[2],
                "model": row[3],
                "status": row[4],
                "time": row[5],
                "total": totals.get((vendor, ident), _empty()),
                "title": str(title[0][0])[:TITLE_LENGTH] if title else None,
                "parent": row[6],
                "account": row[7],
            }
        )
        if source_home is not None:
            listed[-1]["source_home"], listed[-1]["source_home_refusal"] = source_home
        if run := _run(row[8], vendor, row[9]):
            listed[-1]["run"] = run
    return listed


#: The `journal_session` columns that `facts` gives, by their own names.
FACTS: Final = (
    "home",
    "vendor_id",
    "owner_node",
    "effort",
    "requested_effort",
    "sent_effort",
    "parent_vendor",
    "parent_evidence",
    "import_state",
)
#: The copies of one vendor session in one node's home, by the vendor id index.
OWNED_SQL: Final = (
    "SELECT id, vendor_id, import_state FROM journal_session "
    "WHERE vendor = ? AND vendor_id = ? AND owner_node = ? AND seq >= 0"
)


def facts(
    connection: nott_database.Connection, statuses: Collection[str] | None
) -> dict[tuple[str, str], dict[str, Any]]:
    """The cut 3 metadata of each session with a row, by (vendor, id), in one
    query: its source `home`, `vendor_id`, `owner_node`, the `effort` its source
    names, `requested_effort` and `sent_effort`, `parent_vendor`,
    `parent_evidence` and `import_state`. A session whose metadata the writer has
    not written yet has None for each. `statuses` keeps only those statuses;
    None keeps all. A store without the journal gives {}."""
    if not _journal(connection) or statuses is not None and not statuses:
        return {}
    where = ""
    params: tuple[str, ...] = ()
    if statuses is not None:
        where = f"AND status IN ({', '.join('?' * len(statuses))})"
        params = tuple(statuses)
    found = nott_database.query(
        connection,
        f"SELECT vendor, id, {', '.join(FACTS)} FROM journal_session "
        f"WHERE seq >= 0 {where}",
        params,
    )
    return {(row[0], row[1]): dict(zip(FACTS, row[2:], strict=True)) for row in found}


#: The `journal_session` columns that `related` gives, by their own names.
RELATED: Final = (
    "vendor",
    "id",
    "vendor_id",
    "cwd",
    "parent",
    "parent_vendor",
    "parent_evidence",
    "owner_node",
)


def related(
    connection: nott_database.Connection, keys: Collection[tuple[str, str]]
) -> list[dict[str, Any]]:
    """The sessions that `keys` name, whatever their status: each key is a
    (vendor, journal id) or a (vendor, vendor id), as a session's `parent` names
    it. Each gives its `RELATED` columns. Each query searches the primary key or
    the vendor id index for at most `TOTALS_CHUNK` keys of one vendor. A store
    without the journal gives []."""
    if not keys or not _journal(connection):
        return []
    by_vendor: dict[str, list[str]] = {}
    for vendor, ident in sorted(set(keys)):
        by_vendor.setdefault(vendor, []).append(ident)
    found: dict[tuple[str, str], dict[str, Any]] = {}
    for vendor, idents in by_vendor.items():
        for start in range(0, len(idents), TOTALS_CHUNK):
            part = idents[start : start + TOTALS_CHUNK]
            marks = ", ".join("?" * len(part))
            for column in ("id", "vendor_id"):
                for row in nott_database.query(
                    connection,
                    f"SELECT {', '.join(RELATED)} FROM journal_session "
                    f"WHERE vendor = ? AND {column} IN ({marks}) AND seq >= 0",
                    (vendor, *part),
                ):
                    found[(row[0], row[1])] = dict(zip(RELATED, row, strict=True))
    return [found[key] for key in sorted(found)]


def owned(
    connection: nott_database.Connection, vendor: str, vendor_id: str, node: str
) -> list[dict[str, Any]]:
    """The journal sessions of vendor session `vendor_id` whose source home is
    node `node`'s own home (`owner_node`), each its `id`, `vendor_id` and
    `import_state`. The writer names both in one metadata write, so a session
    without metadata is not here. A store without the journal gives []."""
    if not _journal(connection):
        return []
    return [
        {"id": row[0], "vendor_id": row[1], "import_state": row[2]}
        for row in nott_database.query(connection, OWNED_SQL, (vendor, vendor_id, node))
    ]


def _activity(
    connection: nott_database.Connection, vendor: str, home: str, path: str
) -> dict[str, Any]:
    """Read one bounded source row. Never reopen a native transcript."""
    columns = {
        row[1]
        for row in nott_database.query(connection, "PRAGMA table_info(journal_source)")
    }
    if "activity" not in columns:
        return {"reason": "native transcript activity not imported"}
    found = nott_database.query(
        connection,
        "SELECT s.status, s.reason, s.activity, s.consumed, j.status "
        "FROM journal_source s LEFT JOIN journal_session j "
        "ON j.vendor = s.vendor AND j.id = s.session "
        "WHERE s.vendor = ? AND s.home = ? AND s.path = ?",
        (vendor, home, path),
    )
    if not found:
        return {"reason": "native transcript source unavailable"}
    status, reason, saved, consumed, session_status = found[0]
    return observation(
        vendor,
        status,
        reason,
        saved,
        consumed,
        session_status,
        Path(home),
        Path(home) / path,
    )


def show(store: Path, vendor: str, session: str) -> list[str]:
    """Stored rows with run identity, then totals and imported native activity."""
    connection = open_store(store, readonly=True)
    try:
        known = _journal(connection) and nott_database.query(
            connection,
            "SELECT home, path FROM journal_session WHERE vendor = ? AND id = ?",
            (vendor, session),
        )
        if not known:
            raise Missing(
                f"{vendor} {session} is not in the session journal at {store}"
            )
        activity = _activity(connection, vendor, known[0][0], known[0][1])
        run = _run(known[0][0], vendor, known[0][1])
        lines: list[str] = []
        seq = -1
        while page := rows_after(connection, vendor, session, seq, PAGE):
            if run:
                for row in page:
                    row["run"] = run
            lines.extend(_dumps(row) for row in page)
            seq = page[-1]["seq"]
        if not lines:
            raise Missing(
                f"{vendor} {session} has no row in the session journal at {store}"
            )
        total = _totals(
            connection, "WHERE vendor = ? AND session = ?", (vendor, session)
        ).get((vendor, session), _empty())
    finally:
        connection.close()
    return [*lines, _dumps({"total": total, "native_activity": activity})]


def list_sessions(
    store: Path,
    vendor: str | None,
    *,
    since: str | None = None,
    ids: Collection[str] | None = None,
) -> list[str]:
    """One line per session, newest change first, with its token totals and
    no native activity. `ids` keeps the sessions that `sessions` keeps."""
    connection = open_store(store, readonly=True)
    try:
        listed = sessions(
            connection,
            None,
            since=since,
            vendor=vendor,
            ids=ids,
            include_source_home=True,
        )
    finally:
        connection.close()
    return [
        _dumps({key: value for key, value in row.items() if key not in HOST_ONLY})
        for row in listed
        if vendor is None or row["vendor"] == vendor
    ]


def media(file: str) -> bytes | None:
    """The bytes of one image file under `~/.nott/sessions/media/`, or None
    for a name outside the media form, a symlink, a file that is not a regular
    file, or a missing file. No path component is followed as a symlink."""
    if not MEDIA_NAME.fullmatch(file):
        return None
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        folder = os.open(
            Path.home() / ".nott" / "sessions" / "media", flags | os.O_DIRECTORY
        )
    except OSError:
        return None
    try:
        descriptor = os.open(file, flags | os.O_NONBLOCK, dir_fd=folder)
    except OSError:
        return None
    finally:
        os.close(folder)
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        return None
    with os.fdopen(descriptor, "rb") as stream:
        return stream.read()
