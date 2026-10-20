"""The work plane: open tasks and DoDs, and the work-edge shapes a reclassification keeps."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..capsule import Capsule, headline_of
from ..database import Cursor, Database
from ..dod import DodContradiction
from ..project import glob_subtree_pattern
from ..relation import (
    CLOSING_KIND_VALUES,
    RelationKind,
    RelationRecord,
    live_relation_sql,
)
from ..repositories.base import StoreError
from ..repositories.dod import DodRepository
from ..repositories.relations import RelationRepository
from ..wire import decode_cursor, encode_cursor
from .inbox import short_headline

#: How many open work rows `nmemory_context_show` carries. Intentionally small: the
#: field is additive and outside `token_budget`, and what moves the needle is the
#: exact TOTAL next to it, not a long list nobody reads. Measured on the live store
#: on 2026-09-17: 1434 open tasks — dumping them was noise, hiding the total was
#: the defect.
WORK_ROWS: int = 5
WORK_PAGE_MAX: int = 100
# The workflow stdio client (nott_socket.mcp.REPLY_CEILING) accepts 4 MiB.
WORK_REPLY_CEILING: int = 1 << 22
# Leave room for the JSON-RPC/MCP envelope and request correlation id.
_WORK_ENVELOPE_BYTES: int = 1024
#: The cursor-bound option bounds (`tools_schema.json` serves the two lengths).
#: `source` is sized from the real producer labels (`nott.workflow`,
#: `nott.conversation.owner`, `github:<owner>/<repo>` at most 147, bridge labels).
WORK_SCOPE_MAX_BYTES: int = 1024
WORK_PREFIX_MAX: int = 140
WORK_SOURCE_MAX: int = 256


class WorkPageError(StoreError):
    """A page request cannot safely continue the requested work enumeration."""


def _cursor_position(
    cursor: str | None,
    scope: str,
    subtree: bool,
    generation: int,
    headline_prefix: str | None,
    include_content: bool,
    source: str | None,
) -> int:
    if cursor is None:
        return 0
    decoded = decode_cursor(cursor, WORK_CURSOR_MAX)
    if (
        not isinstance(decoded, list)
        or len(decoded) not in (5, 7, 8)
        or decoded[0] != {5: "work-v1", 7: "work-v2", 8: "work-v3"}[len(decoded)]
        or (
            len(decoded) >= 7
            and (
                not (decoded[5] is None or isinstance(decoded[5], str))
                or not isinstance(decoded[6], bool)
                or (len(decoded) == 8 and not isinstance(decoded[7], str))
            )
        )
        or not isinstance(decoded[1], str)
        or not isinstance(decoded[2], bool)
        or type(decoded[3]) is not int
        or not 1 <= decoded[3] <= 2**63 - 1
        or type(decoded[4]) is not int
        or not 1 <= decoded[4] <= 2**63 - 1
    ):
        raise WorkPageError("invalid work cursor")
    options = (decoded[5:] + [None])[:3] if len(decoded) >= 7 else [None, False, None]
    if decoded[1:3] != [scope, subtree] or options != [
        headline_prefix,
        include_content,
        source,
    ]:
        raise WorkPageError("work cursor scope differs from this request")
    if decoded[3] != generation:
        raise WorkPageError(
            "stale work cursor: canonical state changed; restart without cursor"
        )
    return int(decoded[4])


@dataclass(frozen=True)
class WorkRow:
    """An OPEN work unit for the scope, for the mandatory surface.

    The same taint fence as `InboxRow`: for an `instruction_taint` capsule, the BARE
    id is sent, without headline and without slug handle — the slug IS
    `slugify(content)`, meaning content bytes arriving through another field.
    """

    id: str
    handle: str
    kind: str
    headline: str | None
    age_days: int
    instruction_taint: bool
    content: str | None = None

    def to_dict(self) -> dict[str, Any]:
        row: dict[str, Any] = {
            "id": self.id,
            "handle": self.handle,
            "kind": self.kind,
            "age_days": self.age_days,
        }
        if self.headline is not None:
            row["headline"] = self.headline
        if self.instruction_taint:
            row["instruction_taint"] = True
        if self.content is not None:
            row["content"] = self.content
        return row


def _work_cursor(
    scope: str,
    subtree: bool,
    generation: int,
    position: int,
    headline_prefix: str | None,
    include_content: bool,
    source: str | None,
) -> str:
    value: list[Any] = ["work-v1", scope, subtree, generation, position]
    if headline_prefix is not None or include_content or source is not None:
        value[0] = "work-v2"
        value.extend([headline_prefix, include_content])
    if source is not None:
        value[0] = "work-v3"
        value.append(source)
    return encode_cursor(value)


# The longest cursor any accepted request can be handed, computed by the encoder:
# a control character is the worst case per character (six JSON bytes, `\u0001`,
# against at most four UTF-8 bytes for anything else) and per byte of the
# byte-bounded scope; `false` is the longer literal; both counters at their maximum.
WORK_CURSOR_MAX: int = len(
    _work_cursor(
        "\x01" * WORK_SCOPE_MAX_BYTES,
        False,
        2**63 - 1,
        2**63 - 1,
        "\x01" * WORK_PREFIX_MAX,
        False,
        "\x01" * WORK_SOURCE_MAX,
    )
)


def _work_wire_bytes(rows: list[WorkRow], cursor: str | None) -> int:
    # Account for JSON text nested inside MCP's content string, including the
    # stdio door's ASCII fallback. Never trim bytes from a complete content field.
    text = json.dumps(
        {"rows": [row.to_dict() for row in rows], "next_cursor": cursor},
        ensure_ascii=False,
    )
    return (
        len(json.dumps(text, ensure_ascii=True).encode("utf-8")) + _WORK_ENVELOPE_BYTES
    )


@dataclass(frozen=True)
class OpenWork:
    """Open work for the scope: a CAPPED sample and EXACT totals."""

    rows: list[WorkRow]
    task_open: int
    dod_open: int
    dod_without_verdict: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "open": [r.to_dict() for r in self.rows],
            "task_open": self.task_open,
            "dod_open": self.dod_open,
            "dod_without_verdict": self.dod_without_verdict,
        }


def _work_row(
    row: tuple[Any, ...], now: datetime, *, include_content: bool = False
) -> WorkRow:
    cid, canonical, kind, prefix, slug = row
    capsule = Capsule.from_canonical_json(str(canonical))
    tainted = capsule.instruction_taint
    return WorkRow(
        id=str(cid),
        handle=str(cid)
        if tainted
        else (f"{prefix}-{slug}" if prefix and slug else str(cid)),
        kind=str(kind),
        headline=None if tainted else short_headline(headline_of(capsule.content)),
        age_days=max(0, (now - capsule.freshness.valid_from).days),
        instruction_taint=tainted,
        content=capsule.content if include_content and not tainted else None,
    )


def _work_cohort(
    scope: str, subtree: bool
) -> tuple[str, tuple[str, str | None, str | None, list[str]]]:
    pattern = glob_subtree_pattern(scope) if subtree else None
    closing = list(CLOSING_KIND_VALUES)
    cohort_sql = (
        """
        WITH cohort AS (
            -- ONE row per capsule: the `JOIN` with `classifications` is
            -- 1:N, so a capsule classified twice inflated totals
            -- (`count(*)`) and could repeat in list slots
            -- (independent review finding, `fugu-ultra`, 2026-09-17).
            -- Between `dod` and `task`, `dod` remains, alphabetically: it is
            -- arbitrary but DETERMINISTIC, and the verdict axis only
            -- exists for `dod`.
            -- The ranking columns only: the payload is joined back for the
            -- rows a caller returns, never carried through the sort.
            SELECT c.seq, c.id, min(cl.kind) AS kind
            FROM capsules c
            JOIN classifications cl ON cl.capsule_id = c.id
            WHERE c.canonical_json IS NOT NULL
              AND cl.kind IN ('task', 'dod')
              AND (c.project_id = ?
                   OR (? IS NOT NULL
                       AND c.project_id GLOB ?))
              AND NOT EXISTS (SELECT 1 FROM tombstones t
                              WHERE t.capsule_id = c.id)
            GROUP BY c.id
        ), closed AS (
            SELECT cohort.id FROM cohort
            WHERE EXISTS (SELECT 1 FROM relations r
                          WHERE r.to_id = cohort.id
                            AND r.kind IN (SELECT value FROM json_each(?))
                            AND """
        + live_relation_sql()
        + """)
        ), live AS (
            SELECT * FROM cohort WHERE id NOT IN (SELECT id FROM closed)
        )
    """
    )
    args = (scope, pattern, pattern, closing)
    return cohort_sql, args


def check_scope(scope: str, limit: int) -> None:
    """Refuse a project or a page size a work reader cannot bound."""
    if (
        not scope.strip()
        or scope.endswith("/")
        or len(scope.encode("utf-8")) > WORK_SCOPE_MAX_BYTES
    ):
        raise WorkPageError(
            "project_id must name a non-empty project without a trailing slash"
            f" (max {WORK_SCOPE_MAX_BYTES} bytes)"
        )
    if type(limit) is not int or not 1 <= limit <= WORK_PAGE_MAX:
        raise WorkPageError(f"limit must be an integer from 1 to {WORK_PAGE_MAX}")


def work_page(
    db: Database,
    scope: str,
    *,
    subtree: bool,
    limit: int,
    cursor: str | None,
    now: datetime,
    headline_prefix: str | None = None,
    include_content: bool = False,
    source: str | None = None,
) -> tuple[list[WorkRow], str | None]:
    """Keyset page of canonical open work; changed canonical state refuses a cursor.

    `source` selects records whose `provenance.source` equals it exactly, tainted
    ones included: caller metadata, never content, so no prefix or partial match
    opens a channel into a tainted body. `headline_prefix` never matches a tainted
    record, since a prefix test over its content would be a read oracle.

    The generation and rows share one read snapshot. No cursor stores a SQL
    transaction or creates persistent state, and ordinary read traffic cannot
    expire a cursor. Collect every page before writing canonical updates.
    """
    check_scope(scope, limit)
    if headline_prefix is not None and (
        not isinstance(headline_prefix, str)
        or not 1 <= len(headline_prefix) <= WORK_PREFIX_MAX
        or any(ch in headline_prefix for ch in "\n\r\x0b\x0c\x85\u2028\u2029")
    ):
        raise WorkPageError(
            "headline_prefix must be a non-empty single-line string"
            f" (max {WORK_PREFIX_MAX} characters)"
        )
    if type(include_content) is not bool:
        raise WorkPageError("include_content must be a boolean")
    if source is not None and (
        not isinstance(source, str) or not 1 <= len(source) <= WORK_SOURCE_MAX
    ):
        raise WorkPageError(
            f"source must be a non-empty string (max {WORK_SOURCE_MAX} characters)"
        )
    cohort_sql, args = _work_cohort(scope, subtree)
    with db.tool_boundary(), db.connection() as conn, conn.cursor() as cur:
        generation = db.get_generation()
        after = _cursor_position(
            cursor,
            scope,
            subtree,
            generation,
            headline_prefix,
            include_content,
            source,
        )
        cur.execute(
            cohort_sql
            + """
            SELECT p.seq, p.id, c.canonical_json, p.kind, p.prefix, p.slug FROM (
                SELECT seq, id, kind, prefix, slug FROM (
                    SELECT l.seq, l.id, l.kind, h.prefix, h.slug,
                           row_number() OVER (
                               PARTITION BY l.id
                               ORDER BY h.prefix NULLS LAST, h.slug NULLS LAST
                           ) AS rn
                    FROM live l
                    LEFT JOIN capsule_handles h ON h.capsule_id = l.id
                    WHERE l.seq > ?
                      AND (? IS NULL OR EXISTS (
                          SELECT 1 FROM capsules x
                          WHERE x.seq = l.seq
                            AND json_extract(x.canonical_json, '$.instruction_taint') = 0
                            AND substr(json_extract(x.canonical_json, '$.content'), 1, length(?)) = ?
                      ))
                      AND (? IS NULL OR EXISTS (
                          SELECT 1 FROM capsules y
                          WHERE y.seq = l.seq
                            AND json_extract(y.canonical_json, '$.provenance.source') = ?
                      ))
                ) d
                WHERE d.rn = 1
                ORDER BY d.seq ASC
                LIMIT ?
            ) p
            JOIN capsules c ON c.seq = p.seq
            ORDER BY p.seq ASC
            """,
            (
                *args,
                after,
                headline_prefix,
                headline_prefix,
                headline_prefix,
                source,
                source,
                limit + 1,
            ),
        )
        page: list[WorkRow] = []
        next_cursor = None
        for row in cur:
            if len(page) == limit:
                return page, next_cursor
            candidate = _work_row(tuple(row[1:]), now, include_content=include_content)
            candidate_cursor = _work_cursor(
                scope,
                subtree,
                generation,
                int(row[0]),
                headline_prefix,
                include_content,
                source,
            )
            if (
                _work_wire_bytes([*page, candidate], candidate_cursor)
                > WORK_REPLY_CEILING
            ):
                if not page:
                    raise WorkPageError(
                        "work record exceeds the reply ceiling; request without include_content"
                    )
                return page, next_cursor
            page.append(candidate)
            next_cursor = candidate_cursor
    return page, None


def open_work(
    db: Database,
    scope: str,
    *,
    subtree: bool,
    now: datetime,
    cap: int = WORK_ROWS,
) -> OpenWork:
    """The FIFTH site of the single closing rule (C2a), and the only one a session
    reads before any other tool.

    Measured in the live store on 2026-09-17: 1434 open tasks out of 2062 and 407 dod without
    verdict out of 1112 — numbers that only existed in SessionStart and in the census,
    so no one saw them WHILE WORKING. `inbox_candidates` excludes `task`/`dod`
    ON PURPOSE (the work plan has its own consumer); this is that consumer,
    and does not steal slots from knowledge delivery.

    The rule is `CLOSING_KIND_VALUES` with the liveness fence, the SAME as the
    other four sites: an `outcome` is an advisory observation and does not close, a
    revoked edge does not close (U1). Written here in SQL and not derived again:
    the fourth site derived it and diverged on the revocation axis.

    Ordered by the OLDEST: the task that rots is the one from thirty days ago,
    and the one the session just wrote it already knows. The list is capped and
    totals are exact — hiding the total was the defect that made 1434 invisible.

    A pure reader: no writes, no lock, no digest. It is called from inside
    `nmemory_context_show`, which repository policy forbids from paying the canonical
    boundary.
    """
    cohort_sql, args = _work_cohort(scope, subtree)
    with db.connection() as conn, conn.cursor() as cur:
        # The TOTALS, always and independent of cap: with `cap=0` the list returns
        # empty and the total must remain exact — hiding the total was the
        # defect that made 1434 invisible.
        cur.execute(
            cohort_sql
            + """
            SELECT
                count(*) FILTER (WHERE kind = 'task'),
                -- `dod_open` is the total by the SAME rule as the LIST (open =
                -- without a live closing edge). `dod_without_verdict` is another
                -- question -- closes via `dod_events.axis = 'verdict'` -- and
                -- stays, because it is the diagnostic it always was. Serving only the
                -- second alongside the list made the reader compare "N of 407"
                -- with items that the 407 do not count: measured in the live
                -- store, ~705 dods have a verdict and no closing edge.
                count(*) FILTER (WHERE kind = 'dod'),
                count(*) FILTER (
                    WHERE kind = 'dod' AND NOT EXISTS (
                        SELECT 1 FROM dod_events e
                        WHERE e.capsule_id = live.id AND e.axis = 'verdict')
                )
            FROM live
            """,
            args,
        )
        totals = cur.fetchone()
        assert totals is not None
        task_open = int(totals[0])
        dod_open = int(totals[1])
        dod_without_verdict = int(totals[2])
        rows: list[Any] = []
        if cap > 0:
            cur.execute(
                cohort_sql
                + """
                SELECT p.id, c.canonical_json, p.kind, p.prefix, p.slug FROM (
                    SELECT id, seq, kind, prefix, slug FROM (
                        SELECT l.id, l.seq, l.kind, h.prefix, h.slug,
                               row_number() OVER (
                                   PARTITION BY l.id
                                   ORDER BY h.prefix NULLS LAST, h.slug NULLS LAST
                               ) AS rn
                        FROM live l
                        LEFT JOIN capsule_handles h
                               ON h.capsule_id = l.id
                    ) d
                    WHERE d.rn = 1
                    ORDER BY d.seq ASC
                    LIMIT ?
                ) p
                JOIN capsules c ON c.seq = p.seq
                ORDER BY p.seq ASC
                """,
                (*args, cap),
            )
            rows = cur.fetchall()
    out = [_work_row(tuple(row), now) for row in rows]
    return OpenWork(
        rows=out,
        task_open=task_open,
        dod_open=dod_open,
        dod_without_verdict=dod_without_verdict,
    )


def dod_of_session(db: Database, session_id: str) -> list[tuple[str, str, bool, bool]]:
    """The DoDs that this session captured, with the TWO states the repository
    distinguishes: closed (live `witnesses`/`supersedes` edge) and with verdict
    (`dod_events.axis = 'verdict'`).

    A row in `outcomes` is an advisory observation and DOES NOT close (store.py:2522);
    serving only one number beside the other makes the reader compare
    incompatible totals, which is the lesson already recorded in store.py:2168-2174.

    `capture_context.session_id` enters with `coalesce` because most
    capsules have no `capsules.session_id`: it was NULL in 9085 of 12019 rows.

    Measured in the live store on 2026-09-19: open DoDs are not old --- it is
    opening without closing, which only the owner sees if told at session end.
    """
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.id,
                   c.canonical_json ->> '$.content',
                   EXISTS(
                       SELECT 1 FROM relations r
                       WHERE r.to_id = c.id
                         AND r.kind IN (SELECT value FROM json_each(?))
                         AND """
            + live_relation_sql()
            + """
                   ),
                   EXISTS(
                       SELECT 1 FROM dod_events e
                       WHERE e.capsule_id = c.id AND e.axis = 'verdict'
                   )
            FROM capsules c
            JOIN classifications cl ON cl.capsule_id = c.id
            LEFT JOIN capture_context cc ON cc.capsule_id = c.id
            WHERE coalesce(c.session_id, cc.session_id) = ?
              AND cl.kind = 'dod'
              -- `canonical_json IS NOT NULL` suffices and a fence via
              -- `tombstones` would be redundant: `forget_capsule` sets the
              -- column to NULL in BOTH modes (purged and redacted), so
              -- no input distinguishes the two fences and a guard that
              -- no one can break is a guard no one trusts.
              AND c.canonical_json IS NOT NULL
            ORDER BY c.seq
            """,
            (list(CLOSING_KIND_VALUES), session_id),
        )
        return [
            (str(r[0]), str(r[1] or ""), bool(r[2]), bool(r[3])) for r in cur.fetchall()
        ]


def reject_invalidated_work_relations(
    cur: Cursor, capsule_id: str, new_kind: str
) -> None:
    cur.execute(
        """
        SELECT kind, from_id, to_id FROM relations
        WHERE (from_id = ? OR to_id = ?) AND kind IN ('blocks', 'part_of', 'dod_of')
        """,
        (capsule_id, capsule_id),
    )
    rels = cur.fetchall()
    for r_kind_str, from_id, to_id in rels:
        cur.execute("SELECT 1 FROM capsules WHERE id = ?", (from_id,))
        if not cur.fetchone():
            continue
        cur.execute("SELECT 1 FROM capsules WHERE id = ?", (to_id,))
        if not cur.fetchone():
            continue
        cur.execute("SELECT 1 FROM tombstones WHERE capsule_id = ?", (from_id,))
        if cur.fetchone():
            continue
        cur.execute("SELECT 1 FROM tombstones WHERE capsule_id = ?", (to_id,))
        if cur.fetchone():
            continue

        cur.execute(
            "SELECT kind FROM classifications WHERE capsule_id = ?",
            (from_id,),
        )
        from_row = cur.fetchone()
        old_from = str(from_row[0]) if from_row else None

        cur.execute(
            "SELECT kind FROM classifications WHERE capsule_id = ?",
            (to_id,),
        )
        to_row = cur.fetchone()
        old_to = str(to_row[0]) if to_row else None

        next_from = new_kind if from_id == capsule_id else old_from
        next_to = new_kind if to_id == capsule_id else old_to

        def _valid_work(k: str, f: str | None, t: str | None) -> bool:
            if k == "blocks":
                return f == "task" and t == "task"
            if k == "part_of":
                return (f == "task" and t in ("task", "plan", "epic")) or (
                    f == "plan" and t == "epic"
                )
            if k == "dod_of":
                return f == "dod" and t in ("task", "epic", "plan", "brainstorm")
            return True

        if _valid_work(r_kind_str, old_from, old_to) and not _valid_work(
            r_kind_str, next_from, next_to
        ):
            rule = {
                "blocks": "blocks records task readiness edges only; valid shape: task -> task",
                "part_of": "part_of records membership INTO a container; valid shapes: task -> task|plan|epic and plan -> epic",
                "dod_of": "dod_of binds a Definition of Done to the work it governs; valid shape: dod -> task|epic|plan|brainstorm",
            }.get(r_kind_str, "invalid work relation")
            f_desc = f'Some("{next_from}")' if next_from else "None"
            t_desc = f'Some("{next_to}")' if next_to else "None"
            raise StoreError(
                f"{rule}; got {from_id} ({f_desc}) -> {to_id} ({t_desc}) — classify both endpoints with nmemory_kind_classify before recording the edge"
            )


def dod_contradictions(db: Database, capsule_id: str) -> list[DodContradiction]:
    """Accepted DoDs judged `unmet` whose target a live edge witnesses.

    Reads two owners (the relations and the DoD events), so it is a query.

    Every rendered capsule asks this, so it reads the live edges around the capsule
    and around the ends of its `dod_of` edges, and never the whole relation table: it
    used to decode every live edge of the store per capsule (110,630 relation rows to
    render the 14 records of one conversation replay on the live store).
    """
    dod = DodRepository(db)
    repository = RelationRepository(db)
    around: dict[str, list[RelationRecord]] = {}

    def edges(capsule: str) -> list[RelationRecord]:
        if capsule not in around:
            around[capsule] = repository.list_relations_for(capsule)
        return around[capsule]

    def ended_by(capsule: str, kind: RelationKind) -> bool:
        return any(r.kind == kind and r.to_id == capsule for r in edges(capsule))

    standing: list[DodContradiction] = []
    for r in edges(capsule_id):
        if (
            r.kind == RelationKind.DOD_OF
            and not ended_by(r.from_id, RelationKind.SUPERSEDES)
            and not ended_by(r.to_id, RelationKind.SUPERSEDES)
        ):
            st = dod.dod_state_of(r.from_id)
            if (
                st.status == "accepted"
                and st.verdict == "unmet"
                and ended_by(r.to_id, RelationKind.WITNESSES)
            ):
                standing.append(DodContradiction(dod_id=r.from_id, target_id=r.to_id))
    standing.sort(key=lambda c: (c.dod_id, c.target_id))
    deduped: list[DodContradiction] = []
    for c in standing:
        if (
            not deduped
            or deduped[-1].dod_id != c.dod_id
            or deduped[-1].target_id != c.target_id
        ):
            deduped.append(c)
    return deduped
