"""Parity of the audit ledger against the Rust oracle (nmemory 0.6.0, schema v29).

The literals in this file are REAL BYTES from the oracle: they were coined by running
`~/.cache/pynott-oracles/nmemory git-scan` — the only CLI path that writes
to `nmemory.audit_events` (git.rs:1023) — against disposable `*_pytest` databases,
and reading the rows back in PostgreSQL. None touches the owner's live store.

The oracle hashes `sha256(prev_hash || linha_canonica)` WITHOUT a separator byte: the
`Vec::with_capacity(previous.len() + canonical_line.len())` of store.rs:1910-1915
does not reserve space for any. And the canonical line is the compact JSON that
`serde_json` emits for the `Line` struct of store.rs:1882 — fields in order of
declaration, `reason: Option<&str>` without `skip_serializing_if`, so `null` when
absent, and raw non-ASCII.

KNOWN GAP (structural, outside this write-set): the oracle's `at` has
nanosecond precision (9 digits) and `AuditEvent.at` is a `datetime`, which only
stores 6. The port correctly re-derives every event that it recorded itself, but
not an event recorded by Rust whose subsecond exceeds microseconds —
closing this requires raw `at` traversing `store.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime

from nmemory import audit_actions
from nmemory.journal import AuditEvent, compute_chained_hash

# Database nmemory_oracle_ledger_pytest, two consecutive scans: seq=2 chains into seq=1.
ORACLE_LINHA_1 = '{"seq":1,"at":"2026-08-28T12:10:22.298379098Z","actor":"git-scan","action":"corroborate","subject":"git:/tmp/nm-ledger-mint.fAqdiN/repo","reason":"head=a7952baf773ba75b4f42c41a4b20ab7f80e5b184 history_target=a7952baf773ba75b4f42c41a4b20ab7f80e5b184 corroborated=0 drifted=0 missing=0 mentions=0 skipped=0 commits=1 truncated=false cursor_advanced=true next_offset=None"}'
ORACLE_HASH_1 = "5b47a95957817135754189d55ffd393878624ec54b17cd2ff9217a89ad9c8a5c"

ORACLE_LINHA_2 = '{"seq":2,"at":"2026-08-28T12:10:35.430305612Z","actor":"git-scan","action":"corroborate","subject":"git:/tmp/nm-ledger-mint.fAqdiN/repo","reason":"head=6199bedd307043ec610a70289006539f9b0e918d history_target=6199bedd307043ec610a70289006539f9b0e918d corroborated=0 drifted=0 missing=0 mentions=0 skipped=0 commits=1 truncated=false cursor_advanced=true next_offset=None"}'
ORACLE_HASH_2 = "8c71a5df22709d94969b0a79ca66013a6f0b3a5a115a52a9be53855f83ab6a3b"

# Database nmemory_ledger_escape_pytest: repo path contains quote, backslash, and non-ASCII.
ORACLE_LINHA_ESCAPE = '{"seq":1,"at":"2026-08-28T12:12:54.923515273Z","actor":"git-scan","action":"corroborate","subject":"git:/tmp/nm-ledger-mint.fAqdiN/répo \\"aspa\\" \\\\barra ✓","reason":"head=4db5d7349d9175c6df9037e392154f11ed3ebaf5 history_target=4db5d7349d9175c6df9037e392154f11ed3ebaf5 corroborated=0 drifted=0 missing=0 mentions=0 skipped=0 commits=1 truncated=false cursor_advanced=true next_offset=None"}'
ORACLE_HASH_ESCAPE = "157bea6913e3648629770c0aff868ad728cca34a3992272d0d7a090f4570c323"


def test_hash_encadeado_re_deriva_a_cadeia_do_oraculo() -> None:
    """Two chained oracle rows re-derive in port: no separator between prev and line."""
    assert compute_chained_hash("", ORACLE_LINHA_1) == ORACLE_HASH_1
    assert compute_chained_hash(ORACLE_HASH_1, ORACLE_LINHA_2) == ORACLE_HASH_2


def test_hash_encadeado_re_deriva_linha_com_escape_e_nao_ascii() -> None:
    """Quote, backslash and non-ASCII: the port escapes exactly like `serde_json`."""
    assert compute_chained_hash("", ORACLE_LINHA_ESCAPE) == ORACLE_HASH_ESCAPE


def test_canonical_line_emite_o_json_do_oraculo() -> None:
    """`canonical_line` is the JSON of the Rust struct, not a tab-separated line."""
    evento = AuditEvent(
        seq=1,
        at=datetime(2026, 8, 28, 12, 10, 22, 298379, tzinfo=UTC),
        actor="git-scan",
        action="corroborate",
        subject="git:/tmp/nm-ledger-mint.fAqdiN/repo",
        reason="head=a7952baf773ba75b4f42c41a4b20ab7f80e5b184 history_target=a7952baf773ba75b4f42c41a4b20ab7f80e5b184 corroborated=0 drifted=0 missing=0 mentions=0 skipped=0 commits=1 truncated=false cursor_advanced=true next_offset=None",
        chained_hash="",
    )
    # The only difference from ORACLE_LINHA_1 is `at`: a `datetime` does not retain the
    # three nanosecond digits of the Rust clock.
    assert evento.canonical_line() == ORACLE_LINHA_1.replace(".298379098Z", ".298379Z")


def test_reason_ausente_vira_null_e_nao_string_vazia() -> None:
    """`Option<&str>` without `skip_serializing_if` serializes `None` as `null`."""
    evento = AuditEvent(
        seq=7,
        at=datetime(2026, 8, 28, 12, 11, 2, tzinfo=UTC),
        actor="porte",
        action=audit_actions.INGEST,
        subject="cap-1",
        reason=None,
        chained_hash="",
    )
    assert evento.canonical_line() == (
        '{"seq":7,"at":"2026-08-28T12:11:02Z","actor":"porte",'
        f'"action":"{audit_actions.INGEST}","subject":"cap-1","reason":null}}'
    )


def _at_de(micro: int) -> str:
    """The `at` field that `canonical_line` emits for a given microsecond."""
    evento = AuditEvent(
        seq=1,
        at=datetime(2026, 8, 28, 12, 11, 1, micro, tzinfo=UTC),
        actor="porte",
        action=audit_actions.INGEST,
        subject="cap-1",
        reason=None,
        chained_hash="",
    )
    return evento.canonical_line().split('"at":"')[1].split('"')[0]


def test_subsegundo_apara_zeros_a_direita_como_o_crate_time() -> None:
    """The oracle trims subseconds to the last significant digit.

    time-0.3.47 formatting/formattable.rs:312-334 chooses between 1 and 9 digits and
    omits the dot when subseconds are zero; Python's `isoformat()` always emits
    6. Without trimming, ~10% of events recorded by the port — those with
    microseconds ending in zero — would not re-derive in the Rust verifier.
    """
    assert _at_de(298379) == "2026-08-28T12:11:01.298379Z"
    assert _at_de(120000) == "2026-08-28T12:11:01.12Z"
    assert _at_de(100000) == "2026-08-28T12:11:01.1Z"
    assert _at_de(0) == "2026-08-28T12:11:01Z"
