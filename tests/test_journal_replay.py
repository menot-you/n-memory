"""The two legs of `verify_replay`, and what it does when it cannot read.

Upstream implementation (`src/journal.rs:123-181`):

- LIVE leg: each live capsule from the snapshot against ALL audited subjects,
  WITHOUT action filter — any event naming the id already covers it;
- TOMBSTONED leg: each tombstone against `FORGET_ACTIONS`, which there is
  `[audit_actions.FORGET]` — `nmemory_capsule_purge` itself audits with
  `journal::FORGET_ACTIONS[0]`, so the recorded action is that one;
- each miss is a LINE naming the id, not a raw id; order: snapshot first,
  tombstones by id second;
- a read error PROPAGATES (`ReplayError::Store`), never becomes an ok chain.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from storefile import fresh_store_path, sample_capsule_id

from nmemory import audit_actions
from nmemory import journal as journal_mod
from nmemory.journal import AuditEvent, compute_chained_hash, verify_replay
from nmemory.queries import journal as journal_queries
from nmemory.repositories.base import StoreError
from nmemory.store import Store

AT = datetime(2026, 8, 28, 12, 0, 0, tzinfo=UTC)


def _chain(*rows: tuple[str, str]) -> list[AuditEvent]:
    """Events with a VALID chain, so only `out_of_band` speaks in the assert."""
    events: list[AuditEvent] = []
    prev = ""
    for idx, (action, subject) in enumerate(rows, start=1):
        event = AuditEvent(
            seq=idx,
            at=AT,
            actor="judge",
            action=action,
            subject=subject,
            reason=None,
            chained_hash="",
        )
        chained = compute_chained_hash(prev, event.canonical_line())
        event = AuditEvent(
            seq=idx,
            at=AT,
            actor="judge",
            action=action,
            subject=subject,
            reason=None,
            chained_hash=chained,
        )
        events.append(event)
        prev = chained
    return events


def test_a_live_capsule_is_covered_by_any_audit_event() -> None:
    """`nmemory_capsule_pin` names the capsule, so it is NOT out-of-band."""
    events = _chain((audit_actions.PIN, "cap-1"))

    report = verify_replay(events, ["cap-1"], [])

    assert report.out_of_band == [], (
        "the oracle's live leg does not filter by action: an id named by "
        f"ANY event is covered, but the port returned {report.out_of_band}"
    )
    assert report.audit_covers_state is True


def test_a_tombstone_needs_its_own_forget_event() -> None:
    """Ingest covers the LIVE capsule; the tombstone requires a `FORGET` event."""
    events = _chain((audit_actions.INGEST, "cap-1"))

    report = verify_replay(events, [], ["cap-1"])

    assert report.out_of_band == [
        f"tombstone cap-1: no forget audit event (recognized actions: {audit_actions.FORGET})"
    ], (
        "the oracle's tombstoned leg only accepts FORGET_ACTIONS, and the port "
        f"merged the two universes: {report.out_of_band}"
    )
    assert report.audit_covers_state is False


def test_a_forgotten_capsule_is_covered_by_its_forget_event() -> None:
    events = _chain((audit_actions.INGEST, "cap-1"), (audit_actions.FORGET, "cap-1"))

    report = verify_replay(events, [], ["cap-1"])

    assert report.out_of_band == [], (
        "`audit_actions.FORGET` is exactly the action the verb records — the port looks for "
        f"`forget:redacted`/`forget:purged`, which never existed: {report.out_of_band}"
    )


def test_misses_name_the_id_and_keep_snapshot_order_before_tombstones() -> None:
    events = _chain((audit_actions.INGEST, sample_capsule_id(2)))

    report = verify_replay(
        events,
        [sample_capsule_id(3), "cap-1"],
        [sample_capsule_id(9), sample_capsule_id(4)],
    )

    assert report.out_of_band == [
        (
            "capsule "
            + sample_capsule_id(3)
            + ": present in canonical snapshot, named by no audit event"
        ),
        "capsule cap-1: present in canonical snapshot, named by no audit event",
        f"tombstone {sample_capsule_id(4)}: no forget audit event (recognized actions: {audit_actions.FORGET})",
        f"tombstone {sample_capsule_id(9)}: no forget audit event (recognized actions: {audit_actions.FORGET})",
    ], report.out_of_band


def _lane_database_url() -> str:
    """A single owner creates the run database and registers the name for cleanup."""
    return fresh_store_path("journalreplay")


def _fresh_store() -> Store:
    lane_url = _lane_database_url()
    return Store.open(lane_url)


def test_verify_replay_propagates_instead_of_reporting_a_healthy_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Read failure is an UNVERIFIED chain, never an ok chain."""
    store = _fresh_store()

    def boom(*_args: object, **_kwargs: object) -> None:
        raise StoreError("audit ledger unreadable")

    monkeypatch.setattr(journal_mod, "verify_replay", boom)

    with pytest.raises(StoreError):
        journal_queries.verify_replay(store.db)


def test_digest_reports_the_out_of_band_count_it_computed() -> None:
    """The digest wire returned literal `0` while the journal already counted."""
    from nmemory.server import MemoryServer

    store = _fresh_store()
    server = MemoryServer(store=store)
    items = [
        {
            "content": f"Audit coverage capsule {i}.",
            "kind": "task",
            "source": "session:test-journal-replay",
            "anchor": f"PLAN.md:{i}",
            "confidence": 0.9,
            "project_id": "nmemory",
        }
        for i in (1, 2)
    ]
    for item in items:
        res = server.call_tool("nmemory_capsule_ingest", item)
        assert "result" in res and res["result"]["isError"] is False, res

    # TAIL cut in the ledger: the capsule remains live and no event names it
    # anymore. The chain does not see a tail cut, so only `out_of_band` changes —
    # one variable at a time.
    with store.db.writing() as conn:
        conn.execute(
            "DELETE FROM audit_events WHERE subject = '" + sample_capsule_id(2) + "'"
        )

    res = server.call_tool("nmemory_digest_show", {})
    assert "result" in res and res["result"]["isError"] is False, res
    digest = json.loads(res["result"]["content"][0]["text"])

    assert digest["journal"]["out_of_band"] == 1, digest["journal"]
    assert digest["journal"]["chain"] == "ok", digest["journal"]
    assert "broken_seq" not in digest["journal"], digest["journal"]


def test_verify_replay_store_keeps_the_two_legs_disjoint() -> None:
    """The LIVE leg cannot contain an id that the TOMBSTONED leg also counts.

    The plumbing of `Store.verify_replay` derived the live universe from
    `canonical_json IS NOT NULL`, which only describes what `forget_capsule` does
    today — not an invariant enforced by anyone. Any path that inserts into
    `nmemory.tombstones` without nulling the column (partial migration,
    maintenance script, a future write) returned the id in BOTH legs, exactly
    the "merging the universes" that `journal.py:147-149` forbids and that upstream
    (`journal.rs:143-172`) never does.

    Adversarial review blocking finding, 2026-08-28: 3 live + 2
    tombstoned without audit yielded 7 misses where upstream yields 5.
    """
    store = _fresh_store()
    with store.db.writing() as conn, conn.cursor() as cur:
        for idx in (1, 2, 3, 4, 5):
            cur.execute(
                "INSERT INTO capsules "
                "(id, seq, canonical_json, created_at, source_hash, project_id, "
                " authority_class, valid_from) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    f"cap-{idx}",
                    idx,
                    json.dumps({"id": f"cap-{idx}"}),
                    AT.isoformat(),
                    f"hash-{idx}",
                    "nmemory",
                    "observed",
                    AT.isoformat(),
                ),
            )
        # The last two become tombstones WITHOUT anyone nulling `canonical_json`:
        # it is the path that the previous invariant assumed impossible.
        for idx in (4, 5):
            cur.execute(
                "INSERT INTO tombstones "
                "(capsule_id, mode, content_hmac, at, reason) "
                "VALUES (?, ?, ?, ?, ?)",
                (f"cap-{idx}", "purge", f"hmac-{idx}", AT.isoformat(), "probe"),
            )

    report = journal_queries.verify_replay(store.db)
    counted = [line.split(":", 1)[0].split()[-1] for line in report.out_of_band]
    assert len(counted) == len(set(counted)), (
        f"an id was charged on both legs: {report.out_of_band}"
    )
    assert len(report.out_of_band) == 5, report.out_of_band
