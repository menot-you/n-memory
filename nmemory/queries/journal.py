"""The journal replay: the audit chain against the live and tombstoned universes."""

from __future__ import annotations

from ..capsule import parse_rfc3339
from ..database import Cursor, Database
from ..journal import AuditEvent, ReplayReport


def replay_report(cur: Cursor) -> ReplayReport:
    """Replay report on the cursor already opened by the caller."""
    cur.execute(
        "SELECT seq, at, actor, action, subject, reason, chained_hash FROM audit_events ORDER BY seq ASC"
    )
    rows = cur.fetchall()
    events = [
        AuditEvent(
            seq=int(r[0]),
            at=parse_rfc3339(r[1]),
            actor=str(r[2]),
            action=str(r[3]),
            subject=str(r[4]),
            reason=str(r[5]) if r[5] is not None else None,
            chained_hash=str(r[6]),
            # The RAW string from the column: the Rust era signed nanoseconds
            # that `parse_rfc3339` trims. Without it, that era is unreadable.
            stored_at=str(r[1]),
        )
        for r in rows
    ]

    # The LIVE universe is identical to `list()`, and both branches MUST be
    # disjoint: `journal.py` merges universes if an id appears in both, whereas
    # the oracle (`journal.rs:143-172`) never merges.
    #
    # `canonical_json IS NULL` alone does not guarantee disjointness — it only
    # describes what `forget_capsule` (:1547) does today. Any other path inserting
    # into `tombstones` without nulling the column returns the id in BOTH
    # branches and double-counts out_of_band (measured by adversarial review:
    # expected 5, candidate 7).
    # Disjointness now comes from the tombstones table itself, which defines the
    # tombstoned universe below.
    # Append order (seq) because the miss list is produced in that order.
    cur.execute(
        "SELECT id FROM capsules "
        "WHERE canonical_json IS NOT NULL "
        "AND id NOT IN (SELECT capsule_id FROM tombstones) "
        "ORDER BY seq ASC"
    )
    live_ids = [str(r[0]) for r in cur.fetchall()]

    cur.execute("SELECT capsule_id FROM tombstones")
    tomb_ids = [str(r[0]) for r in cur.fetchall()]

    from .. import journal as journal_mod

    # Offline: the verifier PROPAGATES. Returning `chain ok` for an unreadable
    # ledger would publish as verified the very thing nobody verified.
    return journal_mod.verify_replay(events, live_ids, tomb_ids)


def verify_replay(db: Database) -> ReplayReport:
    with db.connection() as conn, conn.cursor() as cur:
        return replay_report(cur)
