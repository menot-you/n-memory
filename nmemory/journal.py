"""Audit journal hash-chaining and integrity verification (v27).

Every mutation produces an audited append in the append-only journal.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from . import audit_actions
from .capsule import format_rfc3339, sha256_hex

# `nmemory_capsule_purge` audits with this action (`tools/purge.py`), and the oracle reads the SAME
# constant in the verb and the coverage leg (`journal.rs:69` + `forget.rs:67`),
# ensuring the vocabulary never drifts. The pair `forget:redacted`/`forget:purged`
# that was previously here was never recorded by anyone.
FORGET_ACTIONS = (audit_actions.FORGET,)


def at_canonical(at: datetime) -> str:
    """The canonical line's `at`, as rendered by the `time` crate (Rfc3339).

    `format_rfc3339` always emits 6 subsecond digits (`.120000`); the oracle
    trims trailing zeros to the last significant digit (`.12`) and omits the dot
    when the subsecond is zero (time-0.3.47 formatting/formattable.rs:312-334). Without
    trimming, every event whose microsecond ends in zero — roughly 1 in 10 —
    would record a hash that the Rust verifier rejects.
    """
    base = format_rfc3339(at.replace(microsecond=0))
    if at.microsecond == 0:
        return base
    return f"{base[:-1]}.{at.microsecond:06d}".rstrip("0") + "Z"


@dataclass(frozen=True)
class AuditEvent:
    seq: int
    at: datetime
    actor: str
    action: str
    subject: str
    reason: str | None
    chained_hash: str
    stored_at: str | None = None
    """The EXACT timestamp string as stored in the column when fetched from the database.

    The historical Rust era signed what Rust wrote: nanoseconds, nine
    digits (`.150006683Z`). `parse_rfc3339` trims them to microseconds, and
    re-rendering from `datetime` produces different bytes. Without preserving
    the raw string, no line from that era can be re-derived — and it represents
    the majority of the owner's live ledger.
    """

    def canonical_line(self) -> str:
        """The oracle's canonical line: the compact JSON emitted by `serde_json`.

        Fields in declaration order of struct `Line` (store.rs:1882).
        `reason: Option<&str>` does not have `skip_serializing_if`, so `None`
        serializes as `null` — never as an empty string. `ensure_ascii=False`
        leaves non-ASCII raw, which matches `serde_json` behavior.
        """
        return json.dumps(
            {
                "seq": self.seq,
                "at": at_canonical(self.at),
                "actor": self.actor,
                "action": self.action,
                "subject": self.subject,
                "reason": self.reason,
            },
            separators=(",", ":"),
            ensure_ascii=False,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "at": format_rfc3339(self.at),
            "actor": self.actor,
            "action": self.action,
            "subject": self.subject,
            "reason": self.reason,
            "chained_hash": self.chained_hash,
        }


def compute_chained_hash(prev_hash: str, canonical_line: str) -> str:
    """SHA-256 over prev_hash concatenated with the canonical line, WITHOUT separator.

    store.rs:1910-1915 concatenates both `as_bytes()` into a
    `Vec::with_capacity(previous.len() + canonical_line.len())` — the reserved
    capacity proves that no byte is inserted in between.
    """
    return sha256_hex((prev_hash + canonical_line).encode())


@dataclass(frozen=True)
class ChainStatus:
    is_ok: bool
    count: int = 0
    broken_seq: int | None = None

    def to_dict(self) -> dict[str, Any]:
        if self.is_ok:
            return {"chain": "ok", "verified": self.count}
        return {"chain": "broken", "broken_seq": self.broken_seq}


@dataclass(frozen=True)
class ReplayReport:
    chain: ChainStatus
    audit_covers_state: bool
    out_of_band: list[str]

    def to_dict(self) -> dict[str, Any]:
        res: dict[str, Any] = {
            "chain": "ok" if self.chain.is_ok else "broken",
            "audit_covers_state": self.audit_covers_state,
            "out_of_band": self.out_of_band,
        }
        if self.chain.is_ok:
            res["verified"] = self.chain.count
        else:
            res["broken_seq"] = self.chain.broken_seq
        return res


# ---------------------------------------------------------------------------
# The three ledger eras
#
# The owner's live ledger does NOT have a single formula, it has three, and they are
# historical fact, not a choice. Measured on 2026-08-28 across the 10,301 live rows:
# seq 1-10018 match EXCLUSIVELY the Rust format; 10019-10301
# EXCLUSIVELY the format used by the port; zero ambiguous rows, zero
# rows that fail to match either. A single-formula verifier cannot
# honestly pass green on this — it would break at seq 1 or at 10019, and
# a `chain: ok` over a ledger it cannot read would be the worst outcome of
# the three.
#
# The order is MONOTONIC and fail-closed: once the chain enters an era,
# it never goes back. A row in an older format after a transition is a downgrade, and
# a downgrade is an attack — precisely the approach an adversary would use to
# rewrite history with a formula the verifier still accepts.
# ---------------------------------------------------------------------------


def _line_rust_historic(event: AuditEvent) -> str:
    """The line that Rust signed: JSON with the EXACT column timestamp."""
    return json.dumps(
        {
            "seq": event.seq,
            "at": event.stored_at
            if event.stored_at is not None
            else at_canonical(event.at),
            "actor": event.actor,
            "action": event.action,
            "subject": event.subject,
            "reason": event.reason,
        },
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _line_port_python(event: AuditEvent) -> str:
    """The line signed by the port up to 2026-08-28: TAB-separated fields.

    `reason=None` became an EMPTY string here, never `null` — which is why
    reconstruction requires explicit `is not None` rather than `or ""`, which
    would also collapse a legitimately empty `reason`.
    """
    r_str = event.reason if event.reason is not None else ""
    return (
        f"{event.seq}\t{format_rfc3339(event.at)}\t{event.actor}\t"
        f"{event.action}\t{event.subject}\t{r_str}"
    )


def _hash_concat(prev_hash: str, line: str) -> str:
    """The Rust form: both `as_bytes()` concatenated, with no separator."""
    return sha256_hex((prev_hash + line).encode())


def _hash_newline(prev_hash: str, line: str) -> str:
    """The old port form: a `\n` between the previous hash and the line."""
    return sha256_hex(f"{prev_hash}\n{line}".encode())


# Era order: index only increases.
_ERAS: tuple[tuple[str, Any, Any], ...] = (
    ("rust_historic", _line_rust_historic, _hash_concat),
    ("port_python", _line_port_python, _hash_newline),
    ("rust_parity", lambda e: e.canonical_line(), _hash_concat),
)


def _matches_era(index: int, prev_hash: str, event: AuditEvent) -> bool:
    """Matches an era; parity preserves raw Rust timestamp as secondary JSON."""
    _name, line_of, hash_of = _ERAS[index]
    line = line_of(event)
    if hash_of(prev_hash, line) == event.chained_hash:
        return True
    if index != 2:
        return False
    # `_ERAS` is typed `Any` in both function columns, so the comparison
    # below returns `Any` without `bool()` — `mypy --strict` refuses it.
    raw_line = _line_rust_historic(event)
    return bool(raw_line != line and hash_of(prev_hash, raw_line) == event.chained_hash)


# THE ERA BOUNDARY, recorded as a ROW rather than a column.
#
# A ROW is just another audit row, covered by the hash chain like any other, and it
# stays readable by every verifier of the ledger without a schema change.
#
# The action was verified absent in the live ledger before being chosen.
BOUNDARY_ACTOR = "nmemory"
BOUNDARY_ACTION = "journal:rust_parity"
BOUNDARY_SUBJECT = "audit_events"
BOUNDARY_REASON = None


def is_parity_boundary(event: AuditEvent) -> bool:
    """Is the row the era boundary? All four fields, exact."""
    return (
        event.actor == BOUNDARY_ACTOR
        and event.action == BOUNDARY_ACTION
        and event.subject == BOUNDARY_SUBJECT
        and event.reason == BOUNDARY_REASON
    )


def wears_the_boundary_shape(event: AuditEvent) -> bool:
    """The row wears the boundary shape without being it — the action OR remaining fields.

    This is not about an attacker: whoever forges hashes can forge any row. This is the
    protocol's fail-closed guard against corruption of ONE field of the sentinel. A bug
    changing `action` leaves a row with `nmemory`/`audit_events`/`None`
    that inference accepts as a normal event — the era never seals into parity,
    `SELECT 1` finds no boundary and writes ANOTHER, and the cutover becomes a
    silent no-op that reopens downgrade to the older writer.

    No legitimate event wears this shape: an audit row's `subject` is a
    capsule id, not the table name.
    """
    return event.action == BOUNDARY_ACTION or (
        event.actor == BOUNDARY_ACTOR
        and event.subject == BOUNDARY_SUBJECT
        and event.reason == BOUNDARY_REASON
    )


# `is_parity_boundary`, in SQL, for callers querying the table instead of the list.
#
# Filtering only by action hid from public surfaces any row that
# used the reserved action with another actor/subject/reason — rows that
# `verify_chain` above REFUSES. Both readings must agree on what constitutes a
# boundary, or public counts silently drop while the chain breaks.
# `IS` (SQLite's null-safe equality) to avoid hardcoding `NULL` in SQL: the value
# comes from the constant, just as with the other three.
BOUNDARY_SQL = "(action = ? AND actor = ? AND subject = ? AND reason IS ?)"
BOUNDARY_SQL_PARAMS: tuple[str, str, str, str | None] = (
    BOUNDARY_ACTION,
    BOUNDARY_ACTOR,
    BOUNDARY_SUBJECT,
    BOUNDARY_REASON,
)


def verify_chain(events: list[AuditEvent]) -> ChainStatus:
    """Verifies the chain across the three eras, never moving backward.

    A row is accepted by the FIRST era, from the current one forward, whose form
    reproduces the recorded `chained_hash`. No previous era is reconsidered:
    once the chain has entered `port_python`, a `rust_historic` row is
    rejected even if the hash matches.
    """
    prev_hash = ""
    era = 0
    boundary_seen = 0
    for idx, event in enumerate(events):
        expected_seq = idx + 1
        if event.seq != expected_seq:
            return ChainStatus(is_ok=False, broken_seq=event.seq)

        # Unmarked: row prior to this column. Inference is ambiguous and
        # known to be ambiguous — for ~90% of microseconds the era 0 row and
        # era 2 row are the SAME bytes. It only exists to read the ledger that is
        # already written; every new row marks it, and the first mark seals it.
        if wears_the_boundary_shape(event) and not is_parity_boundary(event):
            # The SHAPE is RESERVED, not just the action. A row wearing the
            # boundary shape without being it — reserved action with other fields, or
            # all three fields with another action — must not slip into
            # inference, where it would match as just another historical Rust row.
            return ChainStatus(is_ok=False, broken_seq=event.seq)

        if is_parity_boundary(event):
            # THE BOUNDARY: authority, never guesswork. Signed by the
            # parity formula itself, and after it ONLY parity passes. A
            # second boundary is tampering — the era was already sealed.
            if era >= 2:
                return ChainStatus(is_ok=False, broken_seq=event.seq)
            if not _matches_era(2, prev_hash, event):
                return ChainStatus(is_ok=False, broken_seq=event.seq)
            era = 2
            boundary_seen += 1
            prev_hash = event.chained_hash
            continue

        # Before the boundary, inference is ambiguous and known to be ambiguous: for
        # ~90% of microseconds the era 0 row and era 2 row are the SAME
        # bytes, so guessing never seals the middle era. The boundary seals it.
        limit = 2 if era < 2 else len(_ERAS)
        matched: int | None = None
        for candidate in range(era, limit):
            if _matches_era(candidate, prev_hash, event):
                matched = candidate
                break
        if matched is None:
            return ChainStatus(is_ok=False, broken_seq=event.seq)
        era = matched
        prev_hash = event.chained_hash
    # `verified` counts LOGICAL events. The boundary is cutover metadata, not
    # an audited event: the oracle never writes it, and including it would cause
    # the public count to diverge from its count by a row describing nothing that
    # happened to a capsule.
    return ChainStatus(is_ok=True, count=len(events) - boundary_seen)


def verify_replay(
    events: list[AuditEvent],
    live_capsule_ids: list[str],
    tombstoned_ids: list[str],
) -> ReplayReport:
    """Verify audit ledger integrity and state coverage."""
    chain_stat = verify_chain(events)

    # Two SEPARATE legs, never a merged universe. A live capsule is covered by
    # ANY event naming it — auditing a pin already proves the id passed through the
    # ledger. A tombstone is a different question: only the forget event itself explains
    # why the content disappeared, so an old ingest does not cover it.
    audited_subjects = {e.subject for e in events}
    out_of_band = [
        f"capsule {cid}: present in canonical snapshot, named by no audit event"
        for cid in live_capsule_ids
        if cid not in audited_subjects
    ]

    forgotten = {e.subject for e in events if e.action in FORGET_ACTIONS}
    recognized = ", ".join(FORGET_ACTIONS)
    out_of_band.extend(
        f"tombstone {cid}: no forget audit event (recognized actions: {recognized})"
        for cid in sorted(tombstoned_ids)
        if cid not in forgotten
    )
    audit_covers = len(out_of_band) == 0

    return ReplayReport(
        chain=chain_stat,
        audit_covers_state=audit_covers,
        out_of_band=out_of_band,
    )
