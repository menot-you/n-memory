"""The three eras of the live ledger, and the downgrade that must not pass.

The fixed hashes below came from a read-only measurement of the live store made by
another session (2026-08-28): seq 1-10018 match EXCLUSIVELY with the Rust shape,
10019-10301 EXCLUSIVELY with the old port shape, zero ambiguous. They
enter here as INPUT, not design — the design is the monotonic
verifier in `journal.py`.
"""

from __future__ import annotations

from dataclasses import replace

from storefile import sample_capsule_id

from nmemory import audit_actions
from nmemory.capsule import parse_rfc3339
from nmemory.journal import (
    AuditEvent,
    ChainStatus,
    compute_chained_hash,
    verify_chain,
)

LEGACY_HASH = "6732d971f1985bca1a60fce4aef306584ec63c2704ababb56f4339faa9e30880"
CURRENT_HASH = "9cbb845c7f032dd6cae50d33a366d8127b9742ad70252f91dc1c6a5e36b24671"
DOWNGRADE_HASH = "389393b763046a538fd980498ee99738ac6c537c33ceb29347c0581374062f82"


def _event(
    seq: int,
    at: str,
    actor: str,
    subject: str,
    chained_hash: str,
    *,
    action: str = audit_actions.INGEST,
) -> AuditEvent:
    return AuditEvent(
        seq=seq,
        at=parse_rfc3339(at),
        actor=actor,
        action=action,
        subject=subject,
        reason="captured",
        chained_hash=chained_hash,
        stored_at=at,
    )


def _mixed_chain() -> list[AuditEvent]:
    return [
        _event(
            1,
            "2026-07-18T14:43:54.150006683Z",
            "rust-migrator",
            "cap-1",
            LEGACY_HASH,
        ),
        _event(
            2,
            "2026-08-28T12:34:56.123456Z",
            "mcp-caller",
            sample_capsule_id(2),
            CURRENT_HASH,
        ),
    ]


def test_migrated_rust_prefix_transitions_once_to_python_hashes() -> None:
    status = verify_chain(_mixed_chain())
    assert status.is_ok
    assert status.count == 2


def test_tampered_legacy_event_reports_exact_sequence() -> None:
    events = _mixed_chain()
    events[0] = _event(
        1,
        "2026-07-18T14:43:54.150006683Z",
        "rust-migrator",
        "cap-1",
        LEGACY_HASH,
        action=audit_actions.FORGET,
    )
    status = verify_chain(events)
    assert not status.is_ok
    assert status.broken_seq == 1


def test_a_fresh_store_may_start_directly_in_parity() -> None:
    """A new store never had an old era: the first row is already parity."""
    first = _event(1, "2026-08-28T12:00:00.25Z", "mcp-caller", "cap-1", "")
    first = replace(
        first, chained_hash=compute_chained_hash("", first.canonical_line())
    )
    status = verify_chain([first])
    assert status.is_ok, status
    assert status.count == 1


def _boundary(seq: int, at: str, prev: str) -> AuditEvent:
    from nmemory.journal import (
        BOUNDARY_ACTION,
        BOUNDARY_ACTOR,
        BOUNDARY_REASON,
        BOUNDARY_SUBJECT,
    )

    ev = AuditEvent(
        seq=seq,
        at=parse_rfc3339(at),
        actor=BOUNDARY_ACTOR,
        action=BOUNDARY_ACTION,
        subject=BOUNDARY_SUBJECT,
        reason=BOUNDARY_REASON,
        chained_hash="",
        stored_at=at,
    )
    return replace(ev, chained_hash=compute_chained_hash(prev, ev.canonical_line()))


def test_rust_event_with_nanoseconds_after_boundary_verifies() -> None:
    from nmemory.journal import _line_rust_historic

    boundary = _boundary(1, "2026-08-28T12:35:00.5Z", "")
    event = _event(
        2,
        "2026-08-28T12:35:01.123456789Z",
        "rust-writer",
        sample_capsule_id(2),
        "",
    )
    event = replace(
        event,
        chained_hash=compute_chained_hash(
            boundary.chained_hash, _line_rust_historic(event)
        ),
    )

    status = verify_chain([boundary, event])
    assert status.is_ok, status
    assert status.count == 1


def test_the_boundary_seals_the_tab_era_even_when_bytes_collide() -> None:
    """The review blocker, closed by the BOUNDARY.

    For microseconds not ending in zero — ~90% of rows — the historical
    Rust era row and the parity row are byte-for-byte identical. Guessing
    matches era 0, the index locks, and the middle TAB era remains acceptable
    forever: three parity rows followed by ONE in the old formula returned
    `is_ok: True`.
    """
    from nmemory.journal import _hash_newline, _line_port_python

    b = _boundary(1, "2026-08-28T12:35:00.5Z", "")
    prev = b.chained_hash
    events = [b]
    for seq in (2, 3):
        ev = _event(seq, f"2026-08-28T12:35:0{seq}.5Z", "mcp-caller", f"cap-{seq}", "")
        ev = replace(ev, chained_hash=compute_chained_hash(prev, ev.canonical_line()))
        events.append(ev)
        prev = ev.chained_hash

    ok = verify_chain(events)
    assert ok.is_ok, ok
    # `verified` counts LOGICAL events: boundary does not enter.
    assert ok.count == 2, ok

    tab = _event(4, "2026-08-28T12:35:04.5Z", "mcp-caller", sample_capsule_id(4), "")
    tab = replace(tab, chained_hash=_hash_newline(prev, _line_port_python(tab)))
    events.append(tab)
    status = verify_chain(events)
    assert not status.is_ok, "the TAB era came back after the boundary"
    assert status.broken_seq == 4, status


def test_a_second_boundary_is_tampering() -> None:
    """The era is already sealed: a second boundary is tampering."""
    first = _boundary(1, "2026-08-28T12:00:00.25Z", "")
    second = _boundary(2, "2026-08-28T12:00:01.25Z", first.chained_hash)
    status = verify_chain([first, second])
    assert not status.is_ok
    assert status.broken_seq == 2, status


def test_parity_is_unreachable_until_the_boundary_says_so() -> None:
    """Without boundary, parity does not exist: fail-closed by design."""
    events = _mixed_chain()
    parity = _event(3, "2026-08-28T12:35:01.5Z", "mcp-caller", sample_capsule_id(3), "")
    parity = replace(
        parity,
        chained_hash=compute_chained_hash(
            events[1].chained_hash, parity.canonical_line()
        ),
    )
    events.append(parity)
    status = verify_chain(events)
    assert not status.is_ok, "parity without a boundary was accepted"
    assert status.broken_seq == 3, status


def test_the_boundary_action_is_reserved_even_with_the_wrong_fields() -> None:
    """The boundary action is reserved: inexact tuple breaks, never slips through.

    Without this, a row with the marker action but mismatched actor or subject
    would leave `is_parity_boundary` false and fall into inference, where it would match
    as an ordinary historical Rust row — the reserved action would become a
    cheap place to hide a row.
    """
    from nmemory.journal import BOUNDARY_ACTION, BOUNDARY_REASON, BOUNDARY_SUBJECT

    for actor, subject, reason in (
        ("intruso", BOUNDARY_SUBJECT, BOUNDARY_REASON),
        ("nmemory", "outra-coisa", BOUNDARY_REASON),
        ("nmemory", BOUNDARY_SUBJECT, "motivo inventado"),
    ):
        ev = AuditEvent(
            seq=1,
            at=parse_rfc3339("2026-08-28T12:00:00.25Z"),
            actor=actor,
            action=BOUNDARY_ACTION,
            subject=subject,
            reason=reason,
            chained_hash="",
            stored_at="2026-08-28T12:00:00.25Z",
        )
        ev = replace(ev, chained_hash=compute_chained_hash("", ev.canonical_line()))
        status = verify_chain([ev])
        assert not status.is_ok, (actor, subject, reason)
        assert status.broken_seq == 1, status


def test_a_near_marker_with_the_action_changed_is_refused() -> None:
    """A corrupted sentinel field cannot become a normal event.

    The row carries `actor=nmemory`, `subject=audit_events`, `reason=None` and an
    arbitrary `action`, with a valid hash under the parity formula. Reserving only
    `action`, it passed through: inference accepted it as a historical
    Rust row, the era never sealed in parity, `SELECT 1` found no
    boundary and wrote ANOTHER — cutover became a silent no-op.
    """
    at = parse_rfc3339("2026-08-28T22:00:00.5Z")
    near = AuditEvent(
        seq=1,
        at=at,
        actor="nmemory",
        action=audit_actions.INGEST,
        subject="audit_events",
        reason=None,
        chained_hash="",
    )
    near = replace(near, chained_hash=compute_chained_hash("", near.canonical_line()))
    assert verify_chain([near]) == ChainStatus(is_ok=False, broken_seq=1)
