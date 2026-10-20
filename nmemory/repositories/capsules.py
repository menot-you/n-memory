"""Capsules: the rows, their handles, capture context, event time, tombstones and the fold log."""

from __future__ import annotations

import builtins
import hashlib
import hmac
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..capsule import (
    Capsule,
    CapsuleError,
    format_rfc3339,
    parse_rfc3339,
)
from ..capsule_ref import (
    CapsuleRefResolution,
    compose_handle,
    contains_exact_ref,
    prefix_for_kind,
    resolve_capsule_ref,
    slugify,
)
from ..capture import CaptureContext
from ..classify import CandidateKind
from ..database import (
    Database,
    index_term_documents,
    term_index,
    unindex_term_document,
    writes,
)
from ..lexical import bm25_postings_scores, bm25_scores, lexical_words, term_matches
from ..project import (
    expands_to_subtree_by_default,
    glob_subtree_pattern,
    in_project_subtree,
)
from .base import Repository, StoreError
from .reviews import ReviewRepository


class CapsuleNotFound(StoreError):
    pass


class UnknownCapsule(CapsuleNotFound):
    pass


class Tombstoned(StoreError):
    def __init__(self, id: str) -> None:
        super().__init__(
            "capsule is tombstoned; the content is gone, only the marker remains"
        )
        self.id = id


class EventTimeRangeError(ValueError):
    def __init__(self, event_from: datetime, event_to: datetime) -> None:
        super().__init__(
            f"event_to {event_to} lies before event_from {event_from} — an event range runs forward"
        )
        self.event_from = event_from
        self.event_to = event_to


@dataclass(frozen=True)
class EventTimeRange:
    _event_from: datetime
    _event_to: datetime

    def __init__(self, event_from: datetime, event_to: datetime) -> None:
        if event_to < event_from:
            raise EventTimeRangeError(event_from, event_to)
        object.__setattr__(self, "_event_from", event_from)
        object.__setattr__(self, "_event_to", event_to)

    @classmethod
    def point(cls, at: datetime) -> EventTimeRange:
        return cls(at, at)

    @classmethod
    def new(cls, event_from: datetime, event_to: datetime) -> EventTimeRange:
        return cls(event_from, event_to)

    @property
    def event_from(self) -> datetime:
        return self._event_from

    @property
    def event_to(self) -> datetime:
        return self._event_to


@dataclass(frozen=True)
class EventTimeRecord:
    range: EventTimeRange
    declared_at: datetime

    def event_from(self) -> datetime:
        return self.range.event_from

    def event_to(self) -> datetime:
        return self.range.event_to


@dataclass(frozen=True)
class StoredCapsule:
    id: str
    seq: int
    capsule: Capsule
    created_at: datetime
    session_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "seq": self.seq,
            "capsule": self.capsule.to_dict(),
            "created_at": format_rfc3339(self.created_at),
            "session_id": self.session_id,
        }


def _stored_capsule(row: Sequence[Any]) -> StoredCapsule:
    """A `(id, seq, canonical_json, created_at, session_id, ...)` capsules row."""
    return StoredCapsule(
        id=str(row[0]),
        seq=int(row[1]),
        capsule=Capsule.from_canonical_json(row[2]),
        created_at=parse_rfc3339(row[3]),
        session_id=str(row[4]) if row[4] else None,
    )


@dataclass(frozen=True)
class TombstoneRecord:
    capsule_id: str
    mode: str
    content_hmac: str
    at: datetime
    reason: str
    provenance_source: str | None
    provenance_anchor: str | None
    source_hash: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "capsule_id": self.capsule_id,
            "mode": self.mode,
            "content_hmac": self.content_hmac,
            "at": format_rfc3339(self.at),
            "reason": self.reason,
            "provenance_source": self.provenance_source,
            "provenance_anchor": self.provenance_anchor,
            "source_hash": self.source_hash,
        }


@dataclass(frozen=True)
class ListFilter:
    project_id: str | None = None
    limit: int | None = None
    project_prefix: str | None = None
    session_id: str | None = None
    #: The persisted classification kind, selected in SQL by the whole-range `list`.
    #: A forward page refuses it: there a kind is a sidecar filter applied after the
    #: page is cut, which is what its continuation is bound to.
    kind: str | None = None


#: The largest headline-only FORWARD page `nmemory_capsule_list` serves in one
#: answer. A row is a headline-capped entry (`headline_of` caps at 140 chars) plus
#: its id, handle, project, flags and standing, so the page is bounded in BYTES by
#: being bounded in rows: measured on a saturated page of 500 rows with 200-char
#: contents, 207.889 B at 416 B/row — ~20x under the ~4 MiB response frame a client
#: holds. The full-record opt-in uses `LIST_RECORD_PAGE_MAX_BYTES` instead.
#: `nmemory_work_list` keeps its own, smaller bound: two readers over two cohorts,
#: not one knob.
LIST_PAGE_MAX: int = 500

#: The largest complete JSON-RPC stdio reply for a `with_record` list page,
#: including the tool answer envelope and the server's MCP wrapper. The shared
#: client refuses replies above 4 MiB; record pages must leave every byte intact
#: and continue from the last included row instead of exceeding that frame.
LIST_RECORD_PAGE_MAX_BYTES: int = 4 * 1024 * 1024

#: The columns `_stored_capsule` parses, named once for the whole-range read and
#: the forward page.
_LIST_COLUMNS = "id, seq, canonical_json, created_at, session_id"

# Start with pin events. A session start must not scan the capsule store.
_PINNED_FROM_SQL = """
FROM pin_events p JOIN capsules c ON c.id = p.capsule_id
WHERE p.seq IN (SELECT max(seq) FROM pin_events GROUP BY capsule_id)
  AND p.pinned <> 0 AND c.canonical_json IS NOT NULL
  AND (? IS NULL OR c.project_id = ? OR c.project_id GLOB ?)
"""
_PINNED_LIST_SQL = (
    "SELECT c.id, c.seq, CAST(c.canonical_json AS BLOB), c.created_at, c.session_id "
    + _PINNED_FROM_SQL
    + " AND (? IS NULL OR c.seq < ?) ORDER BY c.seq DESC LIMIT ?"
)


@dataclass(frozen=True)
class ListPage:
    """One forward page of a fenced range, and where its scan stopped.

    `rows` are the rows the page SCANNED, before the sidecar filters the tool
    applies on top of them. `last_scanned_seq` is the position a continuation
    records — the last row scanned, never the last one returned, so a page whose
    every row a sidecar filter drops still advances and the consumer never
    re-reads it. `exhausted` is true only when the scan saw the END of the fenced
    range, which is the one condition under which a reader may stop.
    """

    rows: builtins.list[StoredCapsule]
    last_scanned_seq: int | None
    exhausted: bool


def _list_fence(filt: ListFilter | None) -> tuple[str, builtins.list[Any]]:
    """The live-row fence of a list read: its `WHERE` clause and its parameters.

    ONE source for the three fences, read by both the whole-range `list` and the
    forward `list_page`: a page that fenced differently from `list` would serve
    another project's or another session's capsule to a paged reader.

    `kind` is selected here too, for the whole-range read: filtered after the fetch,
    a project-wide read of one kind decoded every capsule of the project to keep a
    handful (13,409 rows decoded to return 1, measured on a conversation replay).
    """
    params: builtins.list[Any] = []
    conditions = ["canonical_json IS NOT NULL"]

    if filt:
        if filt.project_id:
            conditions.append("project_id = ?")
            params.append(filt.project_id)
        if filt.project_prefix:
            conditions.append("(project_id = ? OR project_id GLOB ?)")
            params.append(filt.project_prefix)
            params.append(glob_subtree_pattern(filt.project_prefix))
        if filt.session_id is not None:
            conditions.append("session_id = ?")
            params.append(filt.session_id)
        if filt.kind is not None:
            conditions.append(
                "id IN (SELECT capsule_id FROM classifications WHERE kind = ?)"
            )
            params.append(filt.kind)

    return " WHERE " + " AND ".join(conditions), params


def sanitize_reason(reason: str, max_chars: int = 512) -> str:
    cleaned = "".join(
        c if (c.isprintable() and c not in ("\x00", "\x1b")) else " " for c in reason
    )
    collapsed = " ".join(cleaned.split())
    if not collapsed:
        raise ValueError("forget reason must be non-empty after sanitization")
    if len(collapsed) > max_chars:
        collapsed = collapsed[:max_chars].rstrip()
    return collapsed


class CapsuleRepository(Repository):
    """Owns capsules, capsule_handles, capture_context, event_time, tombstones."""

    def __init__(self, db: Database) -> None:
        super().__init__(db)
        # Capsules written by THIS process. One nMEMORY server per CLIENT process
        # (MCP stdio; measured fleet-wide: three processes, three interactive
        # sessions), thus requiring no identity claims from anyone — making the
        # self-delivery fence effective for Codex, which writes and reads as
        # `unknown` in 116 of 121 `context` calls, where the `session_id` fence was
        # inert across ~96% of traffic (independent review, 2nd pass). What the
        # fence covers is the PROCESS, not the conversation thread: in nSPAWN
        # executors, an app-server is spawned per TURN
        # (`mcps/nspawn/nspawn/claude_child.py`), so what turn N wrote can return
        # to turn N+1 of the same agent — erring on the safe side (echo, never
        # extra `served`), while the session fence covers the rest when the
        # harness supplies identity. A background worker sharing this `Store`
        # (embeddings, vault — currently neither writes capsules) would join this
        # set and disappear from the inbox of the session hosting it: if any starts
        # writing, it needs its own `Store`. Neither fence invents identity that
        # does not exist.
        self.appended_in_process: set[str] = set()

    @writes
    def insert(
        self,
        capsule: Capsule,
        now: datetime,
        session_id: str | None,
        kind: CandidateKind | str | None,
    ) -> tuple[str, bool]:
        """The capsule row and its handle; `(id, False)` when its source_hash exists."""
        shash = capsule.provenance.source_hash
        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, seq, canonical_json FROM capsules WHERE source_hash = ?",
                (shash,),
            )
            row = cur.fetchone()
            if row is not None:
                if row[2] is None:
                    raise Tombstoned(str(row[0]))
                return (str(row[0]), False)

            cur.execute(
                """
                SELECT COALESCE(MAX(s), 0) + 1 FROM (
                    SELECT seq AS s FROM capsules
                    UNION ALL
                    SELECT CAST(substr(capsule_id, 5) AS INTEGER) AS s FROM capsule_handles
                ) t
                """
            )
            row = cur.fetchone()
            assert row is not None
            next_seq = int(row[0])
            cid = f"cap-{next_seq}"

            # The index takes this capsule only if it covers every older one.
            indexed = term_index(conn.conn) is not None

            created_at = format_rfc3339(now)
            cjson = capsule.to_canonical_json()
            vf = format_rfc3339(capsule.freshness.valid_from)

            cur.execute(
                """
                INSERT INTO capsules (seq, id, canonical_json, created_at, source_hash,
                                              project_id, authority_class, valid_from, session_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    next_seq,
                    cid,
                    cjson,
                    created_at,
                    shash,
                    capsule.scope.project_id,
                    capsule.authority_class.value,
                    vf,
                    session_id,
                ),
            )

            if indexed:
                # The text SQLite reads back, as the backfill and the scan read it.
                content = cur.execute(
                    "SELECT canonical_json ->> '$.content' FROM capsules WHERE seq = ?",
                    (next_seq,),
                ).fetchone()[0]
                index_term_documents(conn.conn, [(next_seq, content)])
                cur.execute("UPDATE term_index_state SET covered_seq = ?", (next_seq,))

            pfx = prefix_for_kind(kind) or "cap"
            slug = slugify(capsule.content)
            cur.execute(
                "INSERT INTO capsule_handles (capsule_id, prefix, slug) VALUES (?, ?, ?) "
                "ON CONFLICT (capsule_id) DO NOTHING",
                (cid, pfx, slug),
            )
            return (cid, True)

    @writes
    def set_capture_context(self, cid: str, ctx: CaptureContext) -> None:
        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO capture_context (capsule_id, harness, model, cwd, session_id, host, owner)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (capsule_id) DO UPDATE SET
                    harness = EXCLUDED.harness,
                    model = EXCLUDED.model,
                    cwd = EXCLUDED.cwd,
                    session_id = EXCLUDED.session_id,
                    host = EXCLUDED.host,
                    owner = EXCLUDED.owner
                """,
                (
                    cid,
                    ctx.harness,
                    ctx.model,
                    ctx.cwd,
                    ctx.session_id,
                    ctx.host,
                    ctx.owner,
                ),
            )

    @writes
    def set_event_time(
        self, cid: str, event_time: EventTimeRange, now: datetime
    ) -> None:
        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO event_time (capsule_id, event_from, event_to, declared_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (capsule_id) DO UPDATE SET
                    event_from = EXCLUDED.event_from,
                    event_to = EXCLUDED.event_to,
                    declared_at = EXCLUDED.declared_at
                """,
                (
                    cid,
                    format_rfc3339(event_time.event_from),
                    format_rfc3339(event_time.event_to),
                    format_rfc3339(now),
                ),
            )

    @writes
    def set_handle_prefix(self, capsule_id: str, typed_prefix: str) -> None:
        """Retype a `cap` handle; a handle already typed keeps its prefix."""
        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE capsule_handles
                SET prefix = ?
                WHERE capsule_id = ? AND prefix = 'cap'
                """,
                (typed_prefix, capsule_id),
            )

    def id_by_provenance_source(self, source: str) -> str | None:
        """The live capsule whose provenance source is `source`, or None."""
        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM capsules"
                " WHERE canonical_json IS NOT NULL"
                "   AND canonical_json ->> '$.provenance.source' = ?"
                " LIMIT 1",
                (source,),
            )
            row = cur.fetchone()
        return None if row is None else str(row[0])

    def latest_revision(self, project_id: str, source: str, anchor: str) -> str | None:
        """Return the latest ID for this exact provenance."""
        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT c.id FROM capsules AS c"
                " LEFT JOIN tombstones AS t ON t.capsule_id = c.id"
                " WHERE c.project_id = ?"
                " AND coalesce(c.canonical_json ->> '$.provenance.source',"
                " t.provenance_source) = ?"
                " AND coalesce(c.canonical_json ->> '$.provenance.anchor',"
                " t.provenance_anchor) = ?"
                " ORDER BY c.seq DESC LIMIT 1",
                (project_id, source, anchor),
            )
            row = cur.fetchone()
        return None if row is None else str(row[0])

    def note_own_write(self, capsule_id: str) -> None:
        """Marks a capsule as written BY THIS PROCESS (inbox fence).

        `append` calls this automatically on new capsules. This method exists for
        the DEDUPLICATION path, which bypasses `append`: `ingest_capture` resolves
        the `source_hash` and returns early (`ingest.py`), so placing it in
        `append`'s dedup return claimed coverage that the `nmemory_capsule_ingest` gate
        never reached — caught by the 3rd pass of independent review. A session
        re-asserting existing content already holds the bytes; delivering them back
        to it burned the capsule's eligibility for those who do not have them.
        """
        self.appended_in_process.add(capsule_id)

    def get(self, capsule_id: str) -> StoredCapsule | None:
        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, seq, canonical_json, created_at, session_id FROM capsules WHERE id = ?",
                (capsule_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            if row[2] is None:
                # Pin store.rs:1770-1773 — a row with NULL canonical_json is a
                # tombstone marker; reading it fails closed instead of pretending
                # the capsule never existed.
                raise Tombstoned(capsule_id)
            cap = Capsule.from_canonical_json(row[2])
            return StoredCapsule(
                id=str(row[0]),
                seq=int(row[1]),
                capsule=cap,
                created_at=parse_rfc3339(row[3]),
                session_id=str(row[4]) if row[4] else None,
            )

    def resolve_id(self, id_or_handle: str) -> str | None:
        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT id FROM capsules WHERE id = ?", (id_or_handle,))
            row = cur.fetchone()
            if row:
                return str(row[0])
            cur.execute(
                "SELECT capsule_id FROM capsule_handles WHERE (prefix || '-' || slug) = ? OR capsule_id = ? LIMIT 2",
                (id_or_handle, id_or_handle),
            )
            rows = cur.fetchall()
            if len(rows) == 1:
                return str(rows[0][0])
            if rows:
                return None
        # Typed short ids carry their sequence outside the handle lookup above.
        resolved = self.resolve_capsule_ref(id_or_handle)
        return resolved.id if resolved.status == "resolved" else None

    def get_handle(self, capsule_id: str) -> str:
        h = self.handle_of(capsule_id)
        return h if h is not None else capsule_id

    def handle_of(self, capsule_id: str) -> str | None:
        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT prefix, slug FROM capsule_handles WHERE capsule_id = ?",
                (capsule_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            slug = str(row[1]) if row[1] is not None else None
            return compose_handle(capsule_id, str(row[0]), slug)

    def handle_slug(self, capsule_id: str) -> str | None:
        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT slug FROM capsule_handles WHERE capsule_id = ?",
                (capsule_id,),
            )
            row = cur.fetchone()
            if not row or row[0] is None:
                return None
            return str(row[0])

    def resolve_capsule_ref(self, reference: str) -> CapsuleRefResolution:
        return resolve_capsule_ref(self, reference)

    def capsule_ref_mentions(
        self, references: builtins.list[str], per_reference: int = 8
    ) -> dict[str, tuple[builtins.list[dict[str, Any]], bool]]:
        unique = list(dict.fromkeys(references))
        mentions: dict[str, tuple[builtins.list[dict[str, Any]], bool]] = {
            r: ([], False) for r in unique
        }
        for stored in self.list(ListFilter()):
            content = stored.capsule.content
            h = self.get_handle(stored.id) or stored.id
            for r in unique:
                rows, _ = mentions[r]
                if contains_exact_ref(content, r):
                    if len(rows) < per_reference:
                        rows.append(
                            {
                                "id": stored.id,
                                "handle": h,
                                "at": format_rfc3339(stored.created_at),
                            }
                        )
                    else:
                        mentions[r] = (rows, True)
        return mentions

    def list(self, filt: ListFilter | None = None) -> builtins.list[StoredCapsule]:
        """The whole fenced range in `seq` order; `limit` keeps its NEWEST tail."""
        with self.db.connection() as conn, conn.cursor() as cur:
            where, params = _list_fence(filt)
            query = f"SELECT {_LIST_COLUMNS} FROM capsules{where} ORDER BY seq ASC"
            cur.execute(query, tuple(params))
            rows = cur.fetchall()
            if filt and filt.limit is not None and len(rows) > filt.limit:
                rows = rows[-filt.limit :]

            return [_stored_capsule(r) for r in rows]

    def list_pinned(
        self,
        project_id: str | None,
        *,
        eligible: Callable[[StoredCapsule, dict[str, str]], bool],
        limit: int | None = 60,
    ) -> tuple[builtins.list[StoredCapsule], int]:
        """Return up to `limit` eligible pins and count omitted or unreadable rows.

        With `limit=None` every eligible pin returns, so the count holds only
        the unreadable rows.
        """
        scope = (
            project_id,
            project_id,
            glob_subtree_pattern(project_id)
            if expands_to_subtree_by_default(project_id) and project_id is not None
            else None,
        )
        kept: builtins.list[StoredCapsule] = []
        omitted = 0
        before: int | None = None
        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) " + _PINNED_FROM_SQL, scope)
            remaining = int(cur.fetchone()[0])
            while remaining and (limit is None or len(kept) < limit):
                # Each page scans pin_events again, so an unlimited read takes one page.
                page = remaining if limit is None else limit
                cur.execute(_PINNED_LIST_SQL, (*scope, before, before, page))
                rows = cur.fetchall()
                if not rows:
                    break
                verdicts = ReviewRepository(self.db).review_verdicts(
                    [str(row[0]) for row in rows]
                )
                for row in rows:
                    remaining -= 1
                    before = int(row[1])
                    try:
                        stored = _stored_capsule(row)
                    except (
                        CapsuleError,
                        ValueError,
                        TypeError,
                        KeyError,
                        AttributeError,
                    ):
                        omitted += 1
                        continue
                    if eligible(stored, verdicts):
                        kept.append(stored)
                        if len(kept) == limit:
                            break
        return kept, omitted + remaining

    def list_page(
        self, filt: ListFilter, *, after_seq: int | None, page_size: int
    ) -> ListPage:
        """The next `page_size` rows of the fenced range after `after_seq`, oldest first.

        The FORWARD reader of the same range `list` reads whole: `list`'s `limit`
        keeps the newest tail, which no sequence of requests can walk backwards
        from. Keyset by `seq` (the store order, and a primary key), so a capsule
        written between two pages lands on a later page and skips none of the
        older ones — the reason the position is a sequence and never an offset.

        Bounded on purpose and in one place: a page size outside the declared
        bound is refused here too, not only at the tool boundary, and `limit`
        cannot ride along because the newest tail and this walk are two reads.
        One row beyond the page is PEEKED, never scanned and never returned: it
        is the only honest answer to "did the scan reach the end of the range".
        """
        if type(page_size) is not int or not 1 <= page_size <= LIST_PAGE_MAX:
            raise ValueError(f"page_size must be an integer from 1 to {LIST_PAGE_MAX}")
        if after_seq is not None and after_seq < 1:
            raise ValueError("after_seq is a store sequence; the first one is 1")
        if filt.limit is not None:
            raise ValueError(
                "a forward page carries no limit: limit keeps the newest tail of the "
                "whole range, a page walks that range from the oldest row"
            )
        if filt.kind is not None:
            raise ValueError(
                "a forward page selects no kind: there a kind is a sidecar filter "
                "applied after the page is cut"
            )

        where, params = _list_fence(filt)
        query = (
            f"SELECT {_LIST_COLUMNS} FROM capsules{where} AND seq > ?"
            " ORDER BY seq ASC LIMIT ?"
        )
        params.append(0 if after_seq is None else after_seq)
        params.append(page_size + 1)
        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute(query, tuple(params))
            rows = cur.fetchall()

        scanned = rows[:page_size]
        return ListPage(
            rows=[_stored_capsule(r) for r in scanned],
            last_scanned_seq=int(scanned[-1][1]) if scanned else None,
            exhausted=len(rows) <= page_size,
        )

    def _lexical_rows(self) -> builtins.list[Any]:
        """Every live capsule as `(id, seq, canonical_json, created_at, session_id,
        content, project_id)`, in `seq` order, without parsing a capsule.

        Every live capsule feeds the corpus statistics, but only the ones a search
        returns are parsed: SQLite extracts the content and the project, and the
        row keeps its JSON for the few that match.
        """
        with self.db.connection() as conn, conn.cursor() as cur:
            return cur.execute(
                "SELECT id, seq, canonical_json, created_at, session_id,"
                " canonical_json ->> '$.content',"
                " coalesce(canonical_json ->> '$.scope.project_id', '')"
                " FROM capsules WHERE canonical_json IS NOT NULL ORDER BY seq ASC"
            ).fetchall()

    def search_fts_scoped(
        self,
        terms: builtins.list[str],
        project_id: str | None = None,
        project_prefix: str | None = None,
        limit: int | None = None,
        time_window: Any | None = None,
        session_id: str | None = None,
        effort_set: builtins.list[str] | set[str] | None = None,
        topic_set: builtins.list[str] | set[str] | None = None,
    ) -> builtins.list[tuple[StoredCapsule, float]]:
        """Lexical search over capsules mirroring store.rs at the contract."""
        queries = [lexical_words(t) for t in terms if lexical_words(t)]
        if not queries:
            return []

        def keep(cid: str, project: str, session: str | None) -> bool:
            return (
                (effort_set is None or cid in effort_set)
                and (topic_set is None or cid in topic_set)
                and (project_id is None or project == project_id)
                and (
                    project_prefix is None
                    or in_project_subtree(project, project_prefix)
                )
                and (session_id is None or session == session_id)
            )

        indexed = self._indexed_search(queries, keep, limit)
        if indexed is not None:
            return indexed

        rows = self._lexical_rows()
        eligible = [r for r in rows if keep(str(r[0]), r[6], r[4] or None)]
        scores = bm25_scores(
            {str(r[0]): r[5] for r in rows},
            queries,
            [str(r[0]) for r in eligible],
        )
        hits = [(r, scores[str(r[0])]) for r in eligible if str(r[0]) in scores]

        hits.sort(key=lambda item: (-item[1], item[0][1]))
        if limit is not None:
            hits = hits[:limit]
        return [(_stored_capsule(r), score) for r, score in hits]

    def _indexed_search(
        self,
        queries: builtins.list[set[str]],
        keep: Callable[[str, str, str | None], bool],
        limit: int | None,
    ) -> builtins.list[tuple[StoredCapsule, float]] | None:
        """The search over the term index, or None while the index is incomplete.

        It reads the postings of the query words, the candidates that match,
        and the rows of the capsules it returns, all in one snapshot.
        """
        with self.db.connection() as handle:
            conn = handle.conn
            own = not conn.in_transaction
            if own:
                conn.execute("BEGIN")
            try:
                index = term_index(conn)
                if index is None:
                    return None
                postings: dict[str, dict[int, int]] = {}
                for word, seq, tf in handle.execute(
                    "SELECT word, seq, tf FROM term_postings"
                    " WHERE word IN (SELECT value FROM json_each(?))",
                    (sorted(set().union(*queries)),),
                ):
                    postings.setdefault(word, {})[seq] = tf
                lengths = {
                    seq: length
                    for seq, cid, project, session, length in handle.execute(
                        "SELECT c.seq, c.id, c.project_id, c.session_id, d.length"
                        " FROM capsules c JOIN term_documents d ON d.seq = c.seq"
                        " WHERE c.seq IN (SELECT value FROM json_each(?))",
                        (sorted(term_matches(postings, queries)),),
                    )
                    if keep(str(cid), str(project), session or None)
                }
                scores = bm25_postings_scores(postings, queries, lengths, *index)
                ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
                if limit is not None:
                    ranked = ranked[:limit]
                rows = {
                    int(row[1]): row
                    for row in handle.execute(
                        f"SELECT {_LIST_COLUMNS} FROM capsules"
                        " WHERE seq IN (SELECT value FROM json_each(?))",
                        ([seq for seq, _ in ranked],),
                    )
                }
            finally:
                if own:
                    conn.execute("ROLLBACK")
        return [(_stored_capsule(rows[seq]), score) for seq, score in ranked]

    @writes
    def forget_capsule(
        self,
        capsule_id: str,
        mode: str,
        reason: str,
        hmac_key: bytes,
        now: datetime,
    ) -> TombstoneRecord:
        clean_reason = sanitize_reason(reason)
        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT seq, canonical_json, source_hash FROM capsules WHERE id = ?",
                (capsule_id,),
            )
            row = cur.fetchone()
            if not row:
                raise CapsuleNotFound(f"capsule {capsule_id} not found")

            _seq, cjson, shash = row[0], row[1], row[2]
            if cjson is None:
                # Pin store.rs:4543 — forgetting a tombstone marker is refused.
                raise Tombstoned(capsule_id)
            cap = Capsule.from_canonical_json(cjson)

            h = hmac.new(hmac_key, b"", hashlib.sha256)
            h.update(b"nmemory-tombstone-hmac-v1")
            h.update(capsule_id.encode("utf-8"))
            h.update(b"\x00")
            h.update(cap.content.encode("utf-8"))
            content_hmac = f"hmac-sha256:{h.hexdigest()}"
            at_str = format_rfc3339(now)

            prov_source = cap.provenance.source if mode == "redacted" else None
            prov_anchor = cap.provenance.anchor if mode == "redacted" else None

            # The index drops the text before it goes: the trigger on
            # `capsules` marks the index invalid if it still holds this capsule.
            unindex_term_document(conn.conn, int(_seq))
            # Keep the identity marker, but remove private text in this transaction.
            cur.execute(
                "UPDATE capsules SET canonical_json = NULL WHERE id = ?",
                (capsule_id,),
            )
            for table in ("source_quotes", "embedding_jobs", "capsule_embeddings"):
                cur.execute(f"DELETE FROM {table} WHERE capsule_id = ?", (capsule_id,))
            cur.execute(
                "UPDATE capsule_handles SET slug = NULL WHERE capsule_id = ?",
                (capsule_id,),
            )
            if mode == "purged":
                cur.execute(
                    "DELETE FROM capture_context WHERE capsule_id = ?",
                    (capsule_id,),
                )

            cur.execute(
                """
                INSERT INTO tombstones (capsule_id, mode, content_hmac, at, reason,
                                                provenance_source, provenance_anchor, source_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    capsule_id,
                    mode,
                    content_hmac,
                    at_str,
                    clean_reason,
                    prov_source,
                    prov_anchor,
                    shash,
                ),
            )

            return TombstoneRecord(
                capsule_id=capsule_id,
                mode=mode,
                content_hmac=content_hmac,
                at=now,
                reason=clean_reason,
                provenance_source=prov_source,
                provenance_anchor=prov_anchor,
                source_hash=shash,
            )

    def get_tombstone(self, capsule_id: str) -> TombstoneRecord | None:
        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT capsule_id, mode, content_hmac, at, reason, provenance_source, provenance_anchor, source_hash "
                "FROM tombstones WHERE capsule_id = ?",
                (capsule_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return TombstoneRecord(
                capsule_id=str(row[0]),
                mode=str(row[1]),
                content_hmac=str(row[2]),
                at=parse_rfc3339(row[3]),
                reason=str(row[4]),
                provenance_source=str(row[5]) if row[5] else None,
                provenance_anchor=str(row[6]) if row[6] else None,
                source_hash=str(row[7]) if row[7] else None,
            )

    def list_tombstoned_ids(self) -> builtins.list[str]:
        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT capsule_id FROM tombstones ORDER BY capsule_id ASC")
            return [str(r[0]) for r in cur.fetchall()]

    def list_tombstones(self) -> builtins.list[TombstoneRecord]:
        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT capsule_id, mode, content_hmac, at, reason, provenance_source, provenance_anchor, source_hash "
                "FROM tombstones ORDER BY capsule_id ASC"
            )
            rows = cur.fetchall()
            return [
                TombstoneRecord(
                    capsule_id=str(r[0]),
                    mode=str(r[1]),
                    content_hmac=str(r[2]),
                    at=parse_rfc3339(r[3]),
                    reason=str(r[4]),
                    provenance_source=str(r[5]) if r[5] else None,
                    provenance_anchor=str(r[6]) if r[6] else None,
                    source_hash=str(r[7]) if r[7] else None,
                )
                for r in rows
            ]

    def get_capture_context(self, capsule_id: str) -> CaptureContext | None:
        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT harness, model, cwd, session_id, host, owner FROM capture_context WHERE capsule_id = ?",
                (capsule_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return CaptureContext(
                harness=str(row[0]),
                model=str(row[1]),
                cwd=str(row[2]),
                session_id=str(row[3]),
                host=str(row[4]),
                owner=str(row[5]),
            )

    def find_by_source_hash(self, source_hash: str) -> StoredCapsule | None:
        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT capsule_id FROM tombstones WHERE source_hash = ?",
                (source_hash,),
            )
            row = cur.fetchone()
            if row is not None:
                raise Tombstoned(str(row[0]))

            cur.execute(
                "SELECT id FROM capsules WHERE source_hash = ?",
                (source_hash,),
            )
            row = cur.fetchone()
            if row is not None:
                return self.get(str(row[0]))
            return None

    def event_time_of(self, capsule_id: str) -> EventTimeRecord | None:
        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT event_from, event_to, declared_at FROM event_time WHERE capsule_id = ?",
                (capsule_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            from_dt = parse_rfc3339(row[0])
            to_dt = parse_rfc3339(row[1])
            dec_dt = parse_rfc3339(row[2])
            return EventTimeRecord(
                EventTimeRange.new(from_dt, to_dt), declared_at=dec_dt
            )
