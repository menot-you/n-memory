"""The board: every live task of a project in one column, read in one snapshot.

The columns come from edges only, through the one closing rule
(`CLOSING_KIND_VALUES`, `live_relation_sql`). The first rule that matches wins:

1. `dropped`: a live incoming `supersedes` edge (dead beats done).
2. `done`: a live incoming `witnesses` edge.
3. `doing`: a live `dod_of` edge from a live DoD that no live edge supersedes
   and whose latest status is `accepted`.
4. `blocked`: a live incoming `blocks` edge from an open, live task.
5. `ready`: every other task.

Every field that names a capsule keeps the `_work_row` fence: a tainted capsule
gives its bare id and no headline, slug or outcome text.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from ..capsule import Capsule, headline_of
from ..database import Cursor, Database
from ..relation import CLOSING_KIND_VALUES, live_relation_sql
from .inbox import short_headline
from .staged import REVIEW_REASON_PROPOSAL
from .work import WorkPageError, _work_row, check_scope

COLUMNS: Final = ("ready", "blocked", "doing", "done", "dropped")
#: The `provenance.source` of a change the GitHub sync read back as a proposal.
GITHUB_PULL_SOURCE: Final = "github-pull"


class BoardError(WorkPageError):
    """A board request names a card the board cannot show."""


@dataclass(frozen=True)
class WaitingChange:
    """A staged GitHub change that waits on the owner, and the task it is about."""

    id: str
    task: str
    content: str


def _ratified(capsule: str) -> str:
    return f"""COALESCE((SELECT e.verdict FROM review_events e
                WHERE e.capsule_id = {capsule} ORDER BY e.seq DESC LIMIT 1),
               'ratified') = 'ratified'"""


def _live(capsule: str) -> str:
    return f"""EXISTS (SELECT 1 FROM capsules c WHERE c.id = {capsule}
               AND c.canonical_json IS NOT NULL
               AND NOT EXISTS (SELECT 1 FROM tombstones t WHERE t.capsule_id = c.id))"""


def _closed(capsule: str, kinds: str = "(SELECT value FROM json_each(?))") -> str:
    return f"""EXISTS (SELECT 1 FROM relations x WHERE x.to_id = {capsule}
               AND x.kind IN {kinds} AND {live_relation_sql("x")}
               AND {_live("x.from_id")} AND {_ratified("x.from_id")})"""


def waiting_changes(cur: Cursor, capsule_id: str | None = None) -> list[WaitingChange]:
    """Every staged GitHub change no review has answered, oldest first.

    Waiting means: the latest review event is `proposed` with the staged-ingest
    reason (or `ratified` by the board host while its closing effect is still
    incomplete), the source is the GitHub pull, and a live `about` edge names a task.
    Revoking a completed effect does not return its approval to the queue.
    """
    cur.execute(
        f"""
        SELECT p.id, r.to_id, p.canonical_json FROM capsules p
        JOIN relations r ON r.from_id = p.id AND r.kind = 'about'
        JOIN classifications k ON k.capsule_id = r.to_id AND k.kind = 'task'
        WHERE json_extract(p.canonical_json, '$.provenance.source') = ?
          AND {live_relation_sql("r")}
          AND {_live("p.id")}
          AND (? IS NULL OR p.id = ?)
          AND (
              (SELECT json_array(e.verdict, e.reason) FROM review_events e
               WHERE e.capsule_id = p.id ORDER BY e.seq DESC LIMIT 1)
              = json_array('proposed', ?)
              OR (
                  (SELECT e.verdict FROM review_events e
                   WHERE e.capsule_id = p.id ORDER BY e.seq DESC LIMIT 1) = 'ratified'
                  AND (SELECT e.reason FROM review_events e
                       WHERE e.capsule_id = p.id ORDER BY e.seq DESC LIMIT 1) GLOB 'nott-app:board *'
                  AND (
                      (instr(json_extract(p.canonical_json, '$.content'), ' state closed-') > 0
                       AND NOT EXISTS (
                           SELECT 1 FROM relations c
                           WHERE c.from_id = p.id AND c.to_id = r.to_id
                             AND c.kind IN ('witnesses', 'supersedes')
                       ))
                      OR (instr(json_extract(p.canonical_json, '$.content'), ' body edited') > 0
                          AND NOT EXISTS (
                              SELECT 1 FROM relations d
                              JOIN relations s ON s.from_id = d.from_id AND s.to_id = r.to_id
                              WHERE d.to_id = p.id AND d.kind = 'derived_from'
                                AND s.kind = 'supersedes'
                                AND EXISTS (SELECT 1 FROM review_events e
                                            WHERE e.capsule_id = d.from_id
                                              AND e.verdict = 'ratified')
                          ))
                  )
              )
          )
        ORDER BY p.seq, r.to_id
        """,
        (GITHUB_PULL_SOURCE, capsule_id, capsule_id, REVIEW_REASON_PROPOSAL),
    )
    return [
        WaitingChange(
            id=str(row[0]),
            task=str(row[1]),
            content=Capsule.from_canonical_json(str(row[2])).content,
        )
        for row in cur.fetchall()
    ]


def _headline(canonical: str | None) -> str | None:
    if canonical is None:
        return None
    capsule = Capsule.from_canonical_json(canonical)
    return (
        None
        if capsule.instruction_taint
        else short_headline(headline_of(capsule.content))
    )


def board(
    db: Database,
    project: str,
    *,
    limit: int,
    now: datetime,
    capsule_id: str | None = None,
) -> dict[str, Any]:
    """The project's board, or one card of it when `capsule_id` names a task.

    shortcut: every task of the project is read to place it; page by column when
    a project holds more tasks than one reply can carry.
    """
    check_scope(project, limit)
    closing = json.dumps(list(CLOSING_KIND_VALUES))
    with db.tool_boundary(), db.connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT c.seq, c.id, c.canonical_json, 'task', h.prefix, h.slug,
                   cc.session_id
            FROM capsules c
            JOIN classifications k ON k.capsule_id = c.id AND k.kind = 'task'
            LEFT JOIN capture_context cc ON cc.capsule_id = c.id
            LEFT JOIN (
                SELECT capsule_id, prefix, slug, row_number() OVER (
                    PARTITION BY capsule_id
                    ORDER BY prefix NULLS LAST, slug NULLS LAST) AS rn
                FROM capsule_handles
            ) h ON h.capsule_id = c.id AND h.rn = 1
            WHERE c.project_id = ? AND c.canonical_json IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM tombstones t WHERE t.capsule_id = c.id)
              AND {_ratified("c.id")}
            ORDER BY c.seq
            """,
            (project,),
        )
        tasks = {str(row[1]): row for row in cur.fetchall()}
        if capsule_id is not None and capsule_id not in tasks:
            raise BoardError(f"{capsule_id} is not a task of project {project}")
        cur.execute(
            f"""
            SELECT r.kind, r.from_id, r.to_id, r.at, f.canonical_json, fk.kind,
                   ({_live("r.from_id")} AND {_ratified("r.from_id")}),
                   {_closed("r.from_id")},
                   {_closed("r.from_id", "('supersedes')")},
                   (SELECT json_array(e.value, e.actor, e.at)
                    FROM dod_events e WHERE e.capsule_id = r.from_id
                      AND e.axis = 'status' ORDER BY e.seq DESC LIMIT 1)
            FROM relations r
            JOIN capsules t ON t.id = r.to_id AND t.project_id = ?
            LEFT JOIN capsules f ON f.id = r.from_id
            LEFT JOIN classifications fk ON fk.capsule_id = r.from_id
            WHERE r.kind IN ('supersedes', 'witnesses', 'dod_of', 'blocks', 'part_of')
              AND {live_relation_sql("r")}
            ORDER BY r.at, r.from_id
            """,
            (closing, project),
        )
        edges = cur.fetchall()
        cur.execute(
            """
            SELECT o.capsule_id, o.description, o.actor, o.at FROM outcomes o
            JOIN capsules t ON t.id = o.capsule_id AND t.project_id = ?
            ORDER BY o.seq
            """,
            (project,),
        )
        outcomes = {str(row[0]): row for row in cur.fetchall()}
        waiting = [w for w in waiting_changes(cur) if w.task in tasks]

    closer: dict[str, dict[str, Any]] = {}
    doing: dict[str, dict[str, Any]] = {}
    blockers: dict[str, list[str]] = {}
    children: dict[str, list[str]] = {}
    for kind, src, dst, at, canonical, src_kind, live, shut, dead, status in edges:
        if dst not in tasks:
            continue
        if kind in CLOSING_KIND_VALUES and live:
            previous = closer.get(dst)
            if previous is None or (
                kind == "supersedes" or previous["kind"] != "supersedes"
            ):
                closer[dst] = {
                    "kind": kind,
                    "id": src,
                    "headline": _headline(canonical),
                    "at": at,
                }
        elif kind == "dod_of" and live and not dead and status:
            value, actor, when = json.loads(status)
            if value == "accepted":
                doing[dst] = {"dod": src, "actor": actor, "at": when}
        elif kind == "blocks" and src_kind == "task" and live and not shut:
            blockers.setdefault(dst, []).append(src)
        elif kind == "part_of" and src in tasks:
            units = children.setdefault(dst, [])
            if src not in units:
                units.append(src)

    placed: dict[str, dict[str, Any]] = {}
    columns: dict[str, list[dict[str, Any]]] = {name: [] for name in COLUMNS}
    for cid, row in tasks.items():
        work = _work_row(tuple(row[1:6]), now)
        closed = closer.get(cid)
        column = (
            ("dropped" if closed["kind"] == "supersedes" else "done")
            if closed is not None
            else "doing"
            if cid in doing
            else "blocked"
            if cid in blockers
            else "ready"
        )
        outcome = outcomes.get(cid)
        session = row[6]
        card = {
            "id": cid,
            "handle": work.handle,
            "headline": work.headline,
            "column": column,
            "session": None if session in (None, "unknown") else str(session),
            "blocked_by": sorted(blockers.get(cid, [])),
            "doing": doing.get(cid),
            "closed_by": closed,
            "outcome": None
            if outcome is None or work.instruction_taint
            else {"description": outcome[1], "actor": outcome[2], "at": outcome[3]},
        }
        placed[cid] = card
        columns[column].append(card)
    for name in ("done", "dropped"):
        columns[name].sort(key=lambda card: card["closed_by"]["at"], reverse=True)
    if capsule_id is not None:
        return {"project_id": project, "card": placed[capsule_id]}
    workstreams = [
        {
            "id": cid,
            "headline": card["headline"],
            "column": card["column"],
            "done": sum(1 for u in children[cid] if placed[u]["column"] == "done"),
            "total": sum(1 for u in children[cid] if placed[u]["column"] != "dropped"),
        }
        for cid, card in placed.items()
        if cid in children and card["column"] != "dropped"
    ]
    return {
        "project_id": project,
        "columns": {
            name: {"total": len(cards), "cards": cards[:limit]}
            for name, cards in columns.items()
        },
        "workstreams": workstreams,
        "pending": {
            "total": len(waiting),
            "ids": [{"id": w.id, "task": w.task} for w in waiting[:limit]],
        },
    }
