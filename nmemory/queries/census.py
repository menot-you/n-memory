"""The delivery and write/read census: what was written, and who read it."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ..database import Database, from_us, last_read_state
from ..project import glob_subtree_pattern
from ..relation import CLOSING_KIND_VALUES, live_relation_sql

# U3 (nmemory-curator): the two honest halves of ARRIVAL. A query reached the
# capsule (`asked`) or a delivery brought it to the session (`delivered`) — and
# the census tells which, because serving without anyone asking is not the same
# as being read. `retrieve` is here and also in `usage`; the union
# deduplicates by id, never double-counting the same (capsule, surface) pair.
ASKED_SURFACES: tuple[str, ...] = ("context", "retrieve", "get")


DELIVERED_SURFACES: tuple[str, ...] = ("inbox", "bootstrap", "digest", "list")


# Kinds whose consumer is NOT reading: a DoD closes with a verdict and a task
# with a LIVE `witnesses`/`supersedes` edge (`relation.CLOSING_KINDS`; an
# `outcome` is an advisory observation and does not close), and the work census
# reports exactly these two. Measuring them via the delivery census would make
# the memory server answer for work that is not its own. The initial version also
# named `epic` and `plan` and removed them from the knowledge cohort WITHOUT
# reporting them anywhere — 208 capsules in the live store vanishing from both
# counts, caught by independent review when the arithmetic failed to reconcile.
# A `plan` is read as a document; it belongs to knowledge.
WORK_KINDS: tuple[str, ...] = ("task", "dod")


@dataclass(frozen=True)
class DeliveryCounts:
    """The five states of a write cohort, by scope (U3)."""

    asked: int = 0
    delivered: int = 0
    undelivered: int = 0
    no_session: int = 0
    retired_unread: int = 0

    @property
    def written(self) -> int:
        return (
            self.asked
            + self.delivered
            + self.undelivered
            + self.no_session
            + self.retired_unread
        )


@dataclass(frozen=True)
class WorkCounts:
    """The work plane, measured by its own consumer (U3)."""

    dod_total: int = 0
    dod_without_verdict: int = 0
    task_total: int = 0
    task_open: int = 0


@dataclass(frozen=True)
class DeliveryCensus:
    knowledge: dict[str, DeliveryCounts]
    work: WorkCounts

    def total(self) -> DeliveryCounts:
        return DeliveryCounts(
            asked=sum(c.asked for c in self.knowledge.values()),
            delivered=sum(c.delivered for c in self.knowledge.values()),
            undelivered=sum(c.undelivered for c in self.knowledge.values()),
            no_session=sum(c.no_session for c in self.knowledge.values()),
            retired_unread=sum(c.retired_unread for c in self.knowledge.values()),
        )


def delivery_census(
    db: Database, since: datetime, project_prefix: str | None = None
) -> DeliveryCensus:
    """The five states of the write cohort, by scope, plus work.

    `since` fences ONLY knowledge: work (task/dod) is counted from all time,
    because an open task does not cease being open simply by aging.

    "Session of scope" is an EVENT, never a number of days: a row
    `read_traffic surface='context'` STRICTLY subsequent to `created_at`
    whose `project_id` is scope, or whose `project_prefix` is ancestor or
    equal to it. The `>` is load-bearing: a capsule written at the same instant
    as the session could not have been delivered by it, and the boundary test
    constrains both sides of that line.

    Resolution order is total and leaves no capsule unnamed: asked,
    delivered, retired-unread, undelivered, no-session.
    """
    asked = list(ASKED_SURFACES)
    delivered = list(DELIVERED_SURFACES)
    work = list(WORK_KINDS)
    prefix_pattern = (
        glob_subtree_pattern(project_prefix) if project_prefix is not None else None
    )
    scope_args = (project_prefix, project_prefix, prefix_pattern)
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            WITH cohort AS (
                SELECT c.id, c.project_id, ts(c.created_at) AS born,
                       c.canonical_json IS NULL AS forgotten
                FROM capsules c
                LEFT JOIN classifications cl ON cl.capsule_id = c.id
                WHERE ts(c.created_at) >= ts(?)
                  AND (? IS NULL OR c.project_id = ?
                       OR c.project_id GLOB ?)
                  AND coalesce(cl.kind, 'unclassified') NOT IN (SELECT value FROM json_each(?))
            ), traffic AS (
                SELECT served_ids,
                       max(surface IN (SELECT value FROM json_each(?))) AS asked,
                       max(surface IN (SELECT value FROM json_each(?))) AS delivered
                FROM read_traffic
                WHERE revoked_at IS NULL
                GROUP BY served_ids
            ), traffic_ids AS MATERIALIZED (
                SELECT CAST(trim(sid.value) AS TEXT) AS capsule_id,
                       traffic.asked, traffic.delivered
                FROM traffic CROSS JOIN json_each(traffic.served_ids) AS sid
                WHERE traffic.asked OR traffic.delivered
            ), asked_ids AS (
                SELECT capsule_id FROM traffic_ids WHERE asked
                UNION
                SELECT capsule_id FROM usage
            ), delivered_ids AS (
                -- TEXT affinity lets SQLite index this set for the capsule join.
                SELECT DISTINCT capsule_id FROM traffic_ids WHERE delivered
            ), retired_ids AS (
                SELECT capsule_id FROM tiers WHERE tier <> 'active'
                UNION SELECT r.to_id FROM relations r
                      WHERE r.kind IN ('supersedes', 'falsifies') AND """
            + live_relation_sql()
            + """
                UNION SELECT capsule_id FROM tombstones
            ), fences AS (
                -- Whether a session reaches a scope depends only on the
                -- fence it ran, so each fence's last session is taken once
                -- and every instant is converted once. Matching raw rows per
                -- scope instead converted each subtree session again for
                -- every scope below it: 2.35 M `ts()` calls, 8.1 s of the
                -- hook's 5 s budget in the live store (2026-09-26).
                SELECT project_id, project_prefix, max(ts(at)) AS last_at
                FROM read_traffic
                WHERE surface = 'context' AND revoked_at IS NULL
                GROUP BY project_id, project_prefix
            ), sessions AS (
                -- One session per SCOPE, not per capsule: "session exists
                -- after write" is `max(at) > born`. A scope no fence
                -- reaches has no row, and the LEFT JOIN below reads NULL.
                SELECT sc.project_id, max(f.last_at) AS last_session
                FROM (SELECT DISTINCT project_id FROM cohort) sc
                JOIN fences f
                  ON f.project_id = sc.project_id
                     OR (f.project_prefix IS NOT NULL
                         AND (sc.project_id = f.project_prefix
                              OR substr(sc.project_id, 1,
                                        length(f.project_prefix) + 1)
                                 = f.project_prefix || '/'))
                GROUP BY sc.project_id
            ), state AS (
                SELECT co.project_id,
                    a.capsule_id IS NOT NULL AS was_asked,
                    d.capsule_id IS NOT NULL AS was_delivered,
                    (co.forgotten OR r.capsule_id IS NOT NULL) AS retired,
                    (se.last_session IS NOT NULL
                     AND se.last_session > co.born) AS had_session
                FROM cohort co
                LEFT JOIN asked_ids a ON a.capsule_id = co.id
                LEFT JOIN delivered_ids d ON d.capsule_id = co.id
                LEFT JOIN retired_ids r ON r.capsule_id = co.id
                LEFT JOIN sessions se ON se.project_id = co.project_id
            )
            SELECT project_id,
                count(*) FILTER (WHERE was_asked) AS asked,
                count(*) FILTER (WHERE NOT was_asked AND was_delivered)
                    AS delivered,
                count(*) FILTER (WHERE NOT was_asked AND NOT was_delivered
                                   AND retired) AS retired_unread,
                count(*) FILTER (WHERE NOT was_asked AND NOT was_delivered
                                   AND NOT retired AND had_session)
                    AS undelivered,
                count(*) FILTER (WHERE NOT was_asked AND NOT was_delivered
                                   AND NOT retired AND NOT had_session)
                    AS no_session
            FROM state GROUP BY project_id ORDER BY project_id
            """,
            (since, *scope_args, work, asked, delivered),
        )
        knowledge = {
            str(row[0]): DeliveryCounts(
                asked=int(row[1]),
                delivered=int(row[2]),
                retired_unread=int(row[3]),
                undelivered=int(row[4]),
                no_session=int(row[5]),
            )
            for row in cur.fetchall()
        }
        # Work DOES NOT expire: the work cohort is all-time, unlike
        # the knowledge cohort above, which maintains the `since`
        # window. A task opened 30 days ago remains open and remains
        # on the line — the number RISES, and that is honest.
        cur.execute(
            """
            WITH cohort AS (
                SELECT c.id, cl.kind
                FROM capsules c
                JOIN classifications cl ON cl.capsule_id = c.id
                WHERE (? IS NULL OR c.project_id = ?
                       OR c.project_id GLOB ?)
                  AND cl.kind IN ('dod', 'task')
            )
            SELECT
                count(*) FILTER (WHERE kind = 'dod') AS dod_total,
                -- `unmet` is the BIRTH value and is never a legal transition
                -- (`legal_transitions` of verdict: unmet -> met | partial | failed),
                -- so no row in `dod_events` can carry it: the existence of a
                -- verdict event IS the verdict. A `<> 'unmet'` filter here would
                -- be dead code feigning rigor. A verdict is TERMINAL and never
                -- relabeled: an incorrect dod is REPLACED (`supersedes`), and a
                -- replaced one is not "without verdict" — it was retired.
                count(*) FILTER (
                    WHERE kind = 'dod' AND NOT EXISTS (
                        SELECT 1 FROM dod_events e
                        WHERE e.capsule_id = cohort.id
                          AND e.axis = 'verdict')
                      AND NOT EXISTS (SELECT 1 FROM relations r
                                      WHERE r.to_id = cohort.id
                                        AND r.kind = 'supersedes'
                                        AND """
            + live_relation_sql()
            + """)
                ) AS dod_without_verdict,
                count(*) FILTER (WHERE kind = 'task') AS task_total,
                -- The single closing rule (`relation.CLOSING_KINDS`): a LIVE
                -- incoming witnesses|supersedes edge (U1: a revoked edge does
                -- not close). A row in `outcomes` is an advisory observation
                -- and DOES NOT close.
                count(*) FILTER (
                    WHERE kind = 'task'
                      AND NOT EXISTS (SELECT 1 FROM relations r
                                      WHERE r.to_id = cohort.id
                                        AND r.kind IN (SELECT value FROM json_each(?))
                                        AND """
            + live_relation_sql()
            + """)
                ) AS task_open
            FROM cohort
            """,
            (*scope_args, list(CLOSING_KIND_VALUES)),
        )
        row = cur.fetchone()
        assert row is not None
        work_counts = WorkCounts(
            dod_total=int(row[0]),
            dod_without_verdict=int(row[1]),
            task_total=int(row[2]),
            task_open=int(row[3]),
        )
    return DeliveryCensus(knowledge=knowledge, work=work_counts)


def write_read_census(
    db: Database,
    since: datetime,
    project_prefix: str | None = None,
    *,
    rewrites: bool | None = None,
) -> dict[str, tuple[int, int]]:
    """Captured and never-served counts by kind within a creation window.

    `rewrites` fences on a live outgoing `supersedes` edge: `True` counts only
    capsules that replace an older one, `False` only the rest, `None` all.
    A bulk rewrite (a translation sweep, measured 2026-09-26) is not unread
    knowledge, and `digest show` reports it apart.

    U1 (nmemory-curator): a never-served capsule has no usage row AND no
    read_traffic row on ANY surface — the census counts ARRIVAL, not
    asking. The ranking counters keep their allowlist (RANKING_SURFACES),
    which is where the #434 law now lives. Keep the creation cohort intact
    across tier changes; this measures writing, not an archive policy's
    denominator.
    """
    ordinary, rewritten = write_read_census_partition(db, since, project_prefix)
    if rewrites is True:
        return rewritten
    if rewrites is False:
        return ordinary
    return {
        kind: (
            ordinary.get(kind, (0, 0))[0] + rewritten.get(kind, (0, 0))[0],
            ordinary.get(kind, (0, 0))[1] + rewritten.get(kind, (0, 0))[1],
        )
        for kind in sorted(ordinary.keys() | rewritten.keys())
    }


def write_read_census_partition(
    db: Database, since: datetime, project_prefix: str | None = None
) -> tuple[dict[str, tuple[int, int]], dict[str, tuple[int, int]]]:
    """Organic writes and live rewrites from one arrival-traffic population.

    Digest formerly expanded the identical JSON traffic twice, one full scan
    per partition. Arrival includes every non-revoked serving surface and usage;
    partitioning changes no scope, creation-window or liveness decision.
    """
    with db.tool_boundary(), db.connection() as conn, conn.cursor() as cur:
        state = last_read_state(conn.conn)
        maximum, minimum = cur.execute(
            "SELECT (SELECT coalesce(max(seq), 0) FROM read_traffic),"
            " (SELECT coalesce(min(seq), 1) FROM read_traffic)"
        ).fetchone()
        # Check coverage and read the aggregate in the same snapshot.
        traffic = (
            "SELECT capsule_id FROM last_read"
            if state == (maximum, 1) and minimum >= 1
            else """SELECT sid.value AS capsule_id
                FROM (
                    SELECT DISTINCT served_ids FROM read_traffic
                    WHERE revoked_at IS NULL
                ) traffic, json_each(traffic.served_ids) AS sid"""
        )
        cur.execute(
            """
            WITH served AS (
                SELECT capsule_id FROM usage
                UNION """
            + traffic
            + """
            )
            SELECT coalesce(cl.kind, 'unclassified'),
                   EXISTS (SELECT 1 FROM relations r
                           WHERE r.from_id = c.id AND r.kind = 'supersedes' AND """
            + live_relation_sql()
            + """
                   ) AS is_rewrite,
                   count(*), count(*) FILTER (WHERE s.capsule_id IS NULL)
            FROM capsules c
            LEFT JOIN classifications cl ON cl.capsule_id = c.id
            LEFT JOIN served s ON s.capsule_id = c.id
            WHERE ts(c.created_at) >= ts(?)
              AND (? IS NULL OR c.project_id = ? OR c.project_id GLOB ?)
            GROUP BY cl.kind, is_rewrite
            ORDER BY cl.kind, is_rewrite
            """,
            (
                since,
                project_prefix,
                project_prefix,
                glob_subtree_pattern(project_prefix)
                if project_prefix is not None
                else None,
            ),
        )
        ordinary: dict[str, tuple[int, int]] = {}
        rewritten_counts: dict[str, tuple[int, int]] = {}
        for row in cur.fetchall():
            partition = rewritten_counts if row[1] else ordinary
            partition[str(row[0])] = (int(row[2]), int(row[3]))
    return ordinary, rewritten_counts


def last_read_among(db: Database, candidate_ids: list[str]) -> dict[str, datetime]:
    """The latest read time for each candidate capsule.

    The SECOND ranking clock, orthogonal to that of
    `last_passing_verification_among`: that one measures proof, this one measures demand.
    `decay_factor` is neutral for items never verified — and measured, only
    669 out of 12411 capsules (5%) were —, meaning decay was inert
    for 95% of the store. There is no assertion here that the capsule became
    FALSE; there is the much weaker assertion, sufficient for RANKING,
    that nobody touches it.

    The SAME union as `write_read_census`, and without surface filtering for the
    same reason: there the census measures ARRIVAL, here DEMAND is measured, and in both
    cases every surface that serves writes a row. The allowlist
    `RANKING_SURFACES` exists for the recall COUNTERS — `retrieve` lives
    in `usage` and unioning it would duplicate counts —, but here the
    aggregator is `max`, and a maximum does not duplicate: the same read counted twice
    yields the identical timestamp.
    """
    if not candidate_ids:
        return {}
    with db.tool_boundary(), db.connection() as conn, conn.cursor() as cur:
        state = last_read_state(conn.conn)
        # The aggregate covers seq 1..max only, so a row below 1 keeps the old scan.
        # Two scalar subqueries: SQLite reads a lone max() or min() from the index,
        # but scans the table when one SELECT holds both.
        maximum, minimum = cur.execute(
            "SELECT (SELECT coalesce(max(seq), 0) FROM read_traffic),"
            " (SELECT coalesce(min(seq), 1) FROM read_traffic)"
        ).fetchone()
        if state == (maximum, 1) and minimum >= 1:
            cur.execute(
                """
                SELECT capsule_id, max(at) FROM (
                    SELECT capsule_id, ts(last_recalled_at) AS at FROM usage
                    WHERE capsule_id IN (SELECT value FROM json_each(?))
                    UNION ALL
                    SELECT capsule_id, at FROM last_read
                    WHERE capsule_id IN (SELECT value FROM json_each(?))
                ) GROUP BY capsule_id
                """,
                (candidate_ids, candidate_ids),
            )
            return {str(r[0]): from_us(int(r[1])) for r in cur.fetchall()}
        # Missing, invalid, or incomplete aggregates must use the old scan.
        cur.execute(
            """
            SELECT capsule_id, max(ts(at)) FROM (
                SELECT capsule_id, last_recalled_at AS at
                FROM usage WHERE capsule_id IN (SELECT value FROM json_each(?))
                UNION ALL
                SELECT sid.value AS capsule_id, rt.at
                FROM read_traffic rt, json_each(rt.served_ids) AS sid
                WHERE rt.revoked_at IS NULL
                  AND sid.value IN (SELECT value FROM json_each(?))
            ) leituras
            GROUP BY capsule_id
            """,
            (candidate_ids, candidate_ids),
        )
        return {str(r[0]): from_us(int(r[1])) for r in cur.fetchall()}
