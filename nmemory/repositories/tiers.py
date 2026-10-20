"""Tiers: each capsule's consolidation tier, event-sourced in `tier_events`."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from .. import audit_actions
from ..capsule import format_rfc3339
from ..consolidate import SUPERSESSION_ARCHIVE, Tier
from ..database import writes
from .base import Repository


class TierRepository(Repository):
    """Owns tiers, tier_events."""

    @writes
    def set_tier(self, capsule_id: str, tier: Tier | str, now: datetime) -> None:
        t_str = tier.value if isinstance(tier, Tier) else tier
        at_str = format_rfc3339(now)
        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO tiers (capsule_id, tier, at)
                VALUES (?, ?, ?)
                ON CONFLICT (capsule_id) DO UPDATE SET
                    tier = EXCLUDED.tier,
                    at = EXCLUDED.at
                """,
                (capsule_id, t_str, at_str),
            )
            # U5 (nmemory-curator): movement is an EVENT, not an overwrite.
            # The `tiers` row remains the fast present; history lives in
            # `tier_events`, which existed in the schema without any writer since
            # the port — `get_tier(as_of=before)` read the OVERWRITTEN row and the
            # "recorded undo" of LOCK 2 (reach) existed nowhere.
            cur.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM tier_events")
            seq_row = cur.fetchone()
            assert seq_row is not None
            seq = int(seq_row[0])
            cur.execute(
                "INSERT INTO tier_events "
                "(seq, capsule_id, tier, at, reconstructed) "
                "VALUES (?, ?, ?, ?, ?)",
                (seq, capsule_id, t_str, at_str, 0),
            )

    def get_tier(self, capsule_id: str, as_of: datetime | None = None) -> Tier:
        with self.db.connection() as conn, conn.cursor() as cur:
            if as_of is None:
                # Present reads the single row — it is the hot path of all
                # retrieve, and events are history, not inventory.
                cur.execute(
                    "SELECT tier FROM tiers WHERE capsule_id = ?",
                    (capsule_id,),
                )
            else:
                at_str = format_rfc3339(as_of)
                # U5 (nmemory-curator): history reads EVENTS first. The
                # `tiers` row is a fallback only for pre-U5 stores (the 482
                # archives from 2026-09-15 gain a `reconstructed` event on
                # migrate); before the first event, the default is ACTIVE.
                cur.execute(
                    "SELECT tier FROM tier_events "
                    "WHERE capsule_id = ? AND at <= ? ORDER BY seq DESC LIMIT 1",
                    (capsule_id, at_str),
                )
                row = cur.fetchone()
                if row:
                    return Tier.from_str(row[0])
                cur.execute(
                    "SELECT tier FROM tiers WHERE capsule_id = ? AND at <= ?",
                    (capsule_id, at_str),
                )
            row = cur.fetchone()
            if row:
                return Tier.from_str(row[0])
            return Tier.ACTIVE

    def supersession_archives(self, candidate_ids: list[str]) -> set[str]:
        """The candidates whose present archive consolidation made for supersession.

        Consolidation journals every move it applies as `tier=<tier> <reason>`,
        stamped with the instant `set_tier` gives the tier event, and nothing
        else links the two: so the reason binds only where that instant is
        unambiguous. The capsule's latest tier event must be the only one at its
        instant, with exactly one consolidation event journaled for the capsule
        at that instant, opening with `SUPERSESSION_ARCHIVE` (so that event is
        the archive). An archive with no journal, an older one, or one sharing
        its instant keeps its tier, and so does one whose latest event the U5
        migration reconstructed (`reconstructed = 1`): `set_tier` overwrote one
        row before events existed, so a second archive at that instant left no
        trace to tell it apart.
        """
        if not candidate_ids:
            return set()
        history: defaultdict[str, list[tuple[str, bool]]] = defaultdict(list)
        journal: defaultdict[tuple[str, str], list[str | None]] = defaultdict(list)
        with self.db.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT capsule_id, at, reconstructed FROM tier_events "
                "WHERE capsule_id IN (SELECT value FROM json_each(?)) ORDER BY seq",
                (candidate_ids,),
            )
            for cid, at, reconstructed in cur.fetchall():
                history[str(cid)].append((str(at), bool(reconstructed)))
            cur.execute(
                "SELECT subject, at, reason FROM audit_events "
                "WHERE action = ? AND subject IN (SELECT value FROM json_each(?))",
                (audit_actions.CONSOLIDATE, candidate_ids),
            )
            for subject, at, reason in cur.fetchall():
                journal[(str(subject), str(at))].append(
                    None if reason is None else str(reason)
                )
        bound: set[str] = set()
        for cid, events in history.items():
            at, reconstructed = events[-1]
            reasons = journal.get((cid, at), [])
            if (
                not reconstructed
                and [instant for instant, _ in events].count(at) == 1
                and len(reasons) == 1
                and (reasons[0] or "").startswith(SUPERSESSION_ARCHIVE)
            ):
                bound.add(cid)
        return bound
