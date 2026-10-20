"""Per-capsule sidecars of the digest and the views, one query each."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ..capsule import parse_rfc3339
from ..capsule_ref import compose_handle
from ..classify import CandidateKind
from ..consolidate import Tier
from ..database import Database
from ..project import glob_subtree_pattern

# The ranking recall counters (`usage` via retrieve, `digest_sidecars`) read
# `read_traffic` through an ALLOWLIST, never a denylist. A new
# serving surface that starts leaving rows — `get`, `list`, `digest`, `inbox`,
# and the transcript backfill — stays OUT of recall unless deliberately added
# here. A denylist absorbs each new surface silently, and `archive_deadline`
# (`recall_count == 0`, consolidate.py) stops firing for anything the surface
# ever showed. `retrieve` is absent on purpose: it is already counted once in
# `usage`, and unioning its read_traffic rows would double it.
# `bootstrap` stays out by the same law it always had: orientation never
# increments recall (test_bootstrap.py:143).
RANKING_SURFACES: tuple[str, ...] = ("context",)


_RANKING_IN = ",".join(f"'{s}'" for s in RANKING_SURFACES)

_RANKING_RT_SUBQUERY = f"""
                    SELECT sid.value AS capsule_id, sum(traffic.n) AS n,
                           max(traffic.last_at) AS last_at
                    FROM (
                        SELECT served_ids, count(*) AS n, max(at) AS last_at
                        FROM read_traffic
                        WHERE surface IN ({_RANKING_IN}) AND revoked_at IS NULL
                        GROUP BY served_ids
                    ) traffic CROSS JOIN json_each(traffic.served_ids) AS sid
                    GROUP BY 1
"""


def recall_mark(db: Database) -> int:
    """The newest `read_traffic` row: a count read after it already holds it."""
    with db.connection() as conn, conn.cursor() as cur:
        row = cur.execute("SELECT COALESCE(max(seq), 0) FROM read_traffic").fetchone()
    return int(row[0]) if row is not None else 0


def recalls_after(db: Database, capsule_ids: list[str], mark: int) -> dict[str, int]:
    """Each capsule's `usage` count plus the ranking rows past `mark` that list it.

    Per capsule, never per store: `usage` by its key, and only the rows a
    `recall_mark` read before them did not reach are unpacked.
    """
    if not capsule_ids:
        return {}
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT capsule_id, sum(n) FROM (
                SELECT capsule_id, recall_count AS n FROM usage
                WHERE capsule_id IN (SELECT value FROM json_each(?))
                UNION ALL
                SELECT sid.value, 1
                FROM read_traffic, json_each(read_traffic.served_ids) AS sid
                WHERE read_traffic.seq > ? AND surface IN ({_RANKING_IN})
                  AND revoked_at IS NULL
                  AND sid.value IN (SELECT value FROM json_each(?))
            ) GROUP BY capsule_id
            """,
            (capsule_ids, mark, capsule_ids),
        )
        return {str(r[0]): int(r[1]) for r in cur.fetchall()}


@dataclass(frozen=True)
class CapsuleSidecar:
    """Everything digest queries PER CAPSULE, in a single row."""

    tier: Tier
    kind: CandidateKind | None
    handle: str
    pinned: bool
    recall_count: int
    last_recalled_at: datetime | None


@dataclass(frozen=True)
class ViewSidecar:
    """Everything a VIEW — `nmemory_capsule_export` — queries per capsule.

    Separated from `CapsuleSidecar` by design: digest pays for `pinned` and the
    recall pair, which neither view reads, and export needs review and anchor,
    which digest does not read. A single sidecar would make each caller pay for the
    other's columns.
    """

    tier: Tier
    kind: CandidateKind | None
    handle: str
    review: str | None
    anchor_effective: str | None


def digest_sidecars(
    db: Database, *, project_prefix: str | None = None
) -> dict[str, CapsuleSidecar]:
    """The sidecar of EVERY live capsule in a single query.

    The digest queried tier, usage, pin, classification, and handle one capsule
    at a time, and each query opens a NEW connection (`db.connection`):
    in a real store, that is one connection per capsule times five. Here it is a
    `LEFT JOIN`, and pin — which is event-sourced — enters as the most recent
    event per capsule instead of a query by id.
    """
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.id, t.tier, cl.kind, h.prefix, h.slug,
                   coalesce(u.recall_count, 0) + coalesce(rt.n, 0),
                   coalesce(max(u.last_recalled_at, rt.last_at),
                            u.last_recalled_at, rt.last_at), p.pinned
            FROM capsules c
            LEFT JOIN tiers t ON t.capsule_id = c.id
            LEFT JOIN classifications cl ON cl.capsule_id = c.id
            LEFT JOIN capsule_handles h ON h.capsule_id = c.id
            LEFT JOIN usage u ON u.capsule_id = c.id
            LEFT JOIN (
"""
            + _RANKING_RT_SUBQUERY
            + """
                -- `retrieve` is already in `usage`; guidance (`bootstrap`) and the
                -- service surfaces (`get`, `list`, `digest`, `inbox`) are left
                -- OUT: only who ASKED ranks. The fence is the allowlist
                -- RANKING_SURFACES — a denylist would absorb each new
                -- surface silently (U1 nmemory-curator).
            ) rt ON rt.capsule_id = c.id
            LEFT JOIN (
                SELECT capsule_id, pinned FROM pin_events
                WHERE seq IN (SELECT max(seq) FROM pin_events GROUP BY capsule_id)
            ) p ON p.capsule_id = c.id
            WHERE c.canonical_json IS NOT NULL
              AND (? IS NULL OR c.project_id = ?
                   OR c.project_id GLOB ?)
            """,
            (
                project_prefix,
                project_prefix,
                glob_subtree_pattern(project_prefix)
                if project_prefix is not None
                else None,
            ),
        )
        out: dict[str, CapsuleSidecar] = {}
        for r in cur.fetchall():
            cid = str(r[0])
            out[cid] = CapsuleSidecar(
                tier=Tier.from_str(r[1]) if r[1] is not None else Tier.ACTIVE,
                kind=CandidateKind.from_str(r[2]) if r[2] is not None else None,
                handle=compose_handle(
                    cid,
                    str(r[3]),
                    str(r[4]) if r[4] is not None else None,
                )
                if r[3] is not None
                else cid,
                pinned=bool(r[7]),
                recall_count=int(r[5]) if r[5] is not None else 0,
                last_recalled_at=parse_rfc3339(r[6]) if r[6] is not None else None,
            )
        return out


def view_sidecars(db: Database) -> dict[str, ViewSidecar]:
    """The sidecar of EVERY capsule — live or tombstoned — in a single query.

    `nmemory_capsule_export` queried classification, tier, verdict, anchor, and handle
    one capsule at a time, plus one handle per tombstone; the removed `nmemory_document_render`,
    handle and tier. Each query calls `db.connection`, so the cost was
    5N+M and 2N connections — the same N+1 that `digest_sidecars` had already
    removed from digest.

    The `capsules` row SURVIVES forget with `canonical_json`
    NULL (`forget_capsule`), and nothing deletes from `capsule_handles`: which is
    why there is NO `canonical_json IS NOT NULL` filter from `digest_sidecars` here — a
    single query covers live ones and the tombstoned ones that export also renders.

    Absence reads as the singular getter responded: tier ACTIVE, handle the
    id itself (`get_handle`), the rest `None`.
    """
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.id, t.tier, cl.kind, h.prefix, h.slug, rv.verdict, ar.anchor
            FROM capsules c
            LEFT JOIN tiers t ON t.capsule_id = c.id
            LEFT JOIN classifications cl ON cl.capsule_id = c.id
            LEFT JOIN capsule_handles h ON h.capsule_id = c.id
            LEFT JOIN anchor_rebind ar ON ar.capsule_id = c.id
            LEFT JOIN (
                SELECT capsule_id, verdict
                FROM review_events
                WHERE seq IN (SELECT max(seq) FROM review_events GROUP BY capsule_id)
            ) rv ON rv.capsule_id = c.id
            """
        )
        out: dict[str, ViewSidecar] = {}
        for r in cur.fetchall():
            cid = str(r[0])
            out[cid] = ViewSidecar(
                tier=Tier.from_str(r[1]) if r[1] is not None else Tier.ACTIVE,
                kind=CandidateKind.from_str(r[2]) if r[2] is not None else None,
                handle=compose_handle(
                    cid,
                    str(r[3]),
                    str(r[4]) if r[4] is not None else None,
                )
                if r[3] is not None
                else cid,
                review=str(r[5]) if r[5] is not None else None,
                anchor_effective=str(r[6]) if r[6] is not None else None,
            )
        return out
