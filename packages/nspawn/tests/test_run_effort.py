"""The effort a run was asked for and launched with, read back as recorded.

nSPAWN resolves an effort at admission and hands it to the launcher, and until now
nothing wrote it down: the runs table has no effort column, and the resolved value
lived only in `picker.PickerResult` and the child's environment, so no consumer
could read back at what effort a run was requested or launched. What records it is
ONE lifecycle `run_events` payload written in the SAME transaction as the run row
— no new table, no new column, no new event kind — and what answers it is
`nspawn_run_show`'s own `effort` profile, read from that record and never
recomputed from the live roster.

`executed` is the field a vendor would have to prove, and today no vendor does.
Claude echoes no effort. Codex 0.159.3's app-server schema has no turn-scoped echo
either: `turn/started` carries `{threadId, turn}` and its `Turn` has no effort, and
there is no `turnContext` notification. So every run reads `executed` UNKNOWN, and
it is never filled in from the model's prose, the roster, what was sent, or a None
read as "max". A null beside `admission` is a recorded absence (the caller asked
for nothing, or nothing was sent); a null beside `unknown` is a fact nobody wrote.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
from pathlib import Path
from typing import Any

import nott_database
import pytest
from registry_reset import sql
from supervisor_support import (
    FakeSpawnedChild,
    _classify_by_exit_code,
    _drain_worker_threads,
    _spawn_real,
    _stop_every_reaper,
    _sv,
    _wire_l8_and_l3,
    registry,
    roster_home,
)

from nspawn import app_server, driver, native, picker, supervisor
from nspawn.mcp import NSPAWN_TOOL_CONTRACTS, _to_jsonable
from nspawn.owner_roster import NativeAccount, Provider
from nspawn.redact import SecretNeedles
from nspawn.registry import Registry, RegistryError, WorktreeHeld
from nspawn.rows import (
    CLAUDE_CONFINEMENT_POLICY_ONLY,
    Backend,
    NativeSessionContext,
    RunPhase,
    RunRow,
)
from nspawn.supervisor import RunStatus, Supervisor, create_run_directory

# Export imported fixtures so pytest discovers them in this module.
__all__ = [
    "_drain_worker_threads",
    "_stop_every_reaper",
    "registry",
    "roster_home",
]

#: The provenance vocabulary, spelled here rather than read from the product: it
#: IS the consumer contract, and a rename or a new value turns this file RED
#: instead of changing an answer a consumer already branches on.
RECORDED = "admission"
UNKNOWN = "unknown"

#: Every field `nspawn_run_show` answered before the effort profile existed, in
#: order. The change is ADDITIVE: this tuple plus `effort` is the whole answer.
RUN_SHOW_FIELDS = (
    "run_id",
    "phase",
    "run_dir",
    "failure_reason",
    "total_seq",
    "requested_provider",
    "executed_provider",
    "executed_account",
    "warning",
    "tokens",
    "marker",
    "has_result",
    "stdout_mtime_ns",
    "stderr_tail",
    "api_error_status",
    "rate_limited",
    "child_addr",
    "backend",
    "thread_id",
    "group",
)

#: The print of one keyed dispatch, measured on the unmodified tree (2026-10-01).
#: The effort record must not move it: the same request keys the same run before
#: and after, with an effort pinned and with none.
PINNED_PRINTS = {
    "effort": "fb8a2aae1f520bb6863d4adbb839e6503950e37a38908022a17c61e6f9899ce9",
    "no-effort": "6899d1fd0cadc18f6dd1663bf9987b7f58a321448d0981254a3e6f7787c0f99c",
}

UNKNOWN_PROFILE = {
    "requested": None,
    "requested_source": UNKNOWN,
    "sent": None,
    "sent_source": UNKNOWN,
    "executed": None,
    "executed_source": UNKNOWN,
}


def _recorded(requested: str | None, sent: str | None) -> dict[str, Any]:
    """The whole profile of a run whose admission wrote its record."""
    return {
        "requested": requested,
        "requested_source": RECORDED,
        "sent": sent,
        "sent_source": RECORDED,
        "executed": None,
        "executed_source": UNKNOWN,
    }


def _plain(profile: object) -> dict[str, Any]:
    """A profile as the plain mapping a consumer reads, whatever carries it."""
    if dataclasses.is_dataclass(profile) and not isinstance(profile, type):
        return dataclasses.asdict(profile)
    assert isinstance(profile, dict), profile
    return dict(profile)


def _claude_row(run_id: str, registry: Registry, worktree: str) -> RunRow:
    """The row an ordinary Claude admission inserts."""
    return RunRow(
        run_id=run_id,
        thread_id=run_id,
        worktree=worktree,
        phase=RunPhase.PENDING,
        sandbox=CLAUDE_CONFINEMENT_POLICY_ONLY,
        model="opus",
        owner_instance=registry.instance_id,
        created_at=1,
        updated_at=1,
        backend=Backend.CLAUDE,
    )


def _codex_row(run_id: str, registry: Registry, worktree: str) -> RunRow:
    """The row a read-only Codex admission inserts, before its thread exists."""
    return RunRow(
        run_id=run_id,
        worktree=worktree,
        phase=RunPhase.PENDING,
        sandbox="read-only",
        model="m",
        backend=Backend.CODEX,
        owner_instance=registry.instance_id,
        created_at=1,
        updated_at=1,
        executed_provider="codex",
        executed_account="one",
    )


def _picked(requested: str | None, sent: str | None) -> picker.PickerResult:
    """One pick whose request and resolution DIFFER, so a profile that reported
    either twice could not pass for the other."""
    return picker.PickerResult(
        requested_provider=None,
        executed_provider=Provider(
            name="acme", bin=Path("/usr/bin/claude"), model="opus", models=["opus"]
        ),
        executed_account="acct-1",
        model="opus",
        effort=sent,
        warning=None,
        requested_effort=requested,
    )


def _wired(monkeypatch: pytest.MonkeyPatch, picked: picker.PickerResult) -> None:
    """The Claude happy path on one pick: `true` exits 0, classified COMPLETED."""
    monkeypatch.setattr(supervisor.picker, "pick_execution", lambda *a, **k: picked)
    _wire_l8_and_l3(monkeypatch, lambda *a, **k: FakeSpawnedChild(_spawn_real("true")))
    monkeypatch.setattr(
        supervisor.terminal, "classify_terminal_run", _classify_by_exit_code
    )


def _payloads(registry: Registry, run_id: str) -> list[dict[str, Any]]:
    return [
        event.payload
        for event in registry.read_run_events(run_id)
        if isinstance(event.payload, dict)
    ]


def _effort_records(registry: Registry, run_id: str) -> list[object]:
    """Every effort record this run carries, whatever event it rides on."""
    return [
        payload["effort_profile"]
        for payload in _payloads(registry, run_id)
        if "effort_profile" in payload
    ]


# ---------------------------------------------------------------------------
# The record: requested and sent, in the admission's own transaction
# ---------------------------------------------------------------------------


def test_the_admission_records_requested_and_sent_in_its_own_transaction(
    registry: Registry, tmp_path: Path
) -> None:
    worktree = str(tmp_path)
    registry.admit_run(
        _claude_row("run-recorded", registry, worktree),
        True,
        1000,
        60_000,
        effort_profile={"requested": "ultra", "sent": "high"},
    )
    events = registry.read_run_events("run-recorded")
    # Admission records usage coverage before the separate effort record.
    assert [(event.seq, event.kind) for event in events] == [
        (1, "lifecycle"),
        (2, "lifecycle"),
    ]
    assert events[0].payload == {
        "usage_pending": "exec",
        "usage_managed": False,
        "usage_identity": None,
    }
    assert events[1].payload == {
        "effort_profile": {"requested": "ultra", "sent": "high"}
    }
    assert registry.verify_run_event_chain("run-recorded") is True
    assert _plain(registry.effort_profile("run-recorded")) == _recorded("ultra", "high")
    # ONE transaction: this second admission is refused the worktree, and
    # neither the row nor the record survives the refusal.
    with pytest.raises(WorktreeHeld):
        registry.admit_run(
            _claude_row("run-refused", registry, worktree),
            True,
            1000,
            60_000,
            effort_profile={"requested": None, "sent": "low"},
        )
    assert registry.get_run("run-refused") is None
    assert registry.read_run_events("run-refused") == []


@pytest.mark.parametrize("refused", ["the record", "the start"])
def test_a_run_and_its_record_commit_together_or_not_at_all(
    registry: Registry, tmp_path: Path, refused: str
) -> None:
    """Whichever statement of the admission fails, nothing of it survives.

    The fence is a TEMP trigger on this test's own connection to its disposable
    store, so it moves no schema and falls on exactly one statement: the record's
    own insert, or the `spawning` update that closes the admission. A record
    written in a transaction of its own — before or after the row's — leaves a
    row without its record, or a record without its run, in one of the two.
    """
    run_id = "run-all-or-nothing"
    worktree = str(tmp_path)
    fence = (
        "BEFORE INSERT ON run_events WHEN NEW.run_id = 'run-all-or-nothing' "
        "AND json_type(NEW.payload, '$.effort_profile') IS NOT NULL"
        if refused == "the record"
        else "BEFORE UPDATE OF phase ON runs WHEN NEW.run_id = 'run-all-or-nothing'"
        " AND NEW.phase = 'spawning'"
    )
    sql(
        registry,
        f"CREATE TEMP TRIGGER effort_fixture_fence {fence} "
        "BEGIN SELECT RAISE(ABORT, 'effort fixture refuses this admission'); END",
    )
    try:
        with pytest.raises((RegistryError, nott_database.IntegrityError)):
            registry.admit_run(
                _claude_row(run_id, registry, worktree),
                True,
                1000,
                60_000,
                effort_profile={"requested": "high", "sent": "high"},
            )
    finally:
        sql(registry, "DROP TRIGGER IF EXISTS temp.effort_fixture_fence")
    assert registry.get_run(run_id) is None
    assert registry.read_run_events(run_id) == []
    assert registry.read_claim(worktree) is None


@pytest.mark.parametrize(
    "record",
    [
        "high",
        {"requested": "high"},
        {"sent": "high"},
        {"requested": "high", "sent": 5},
        {"requested": "", "sent": "high"},
        {"requested": "high", "sent": "high", "executed": "max"},
    ],
    ids=["not-a-mapping", "no-sent", "no-requested", "not-text", "empty", "forged"],
)
def test_a_malformed_record_is_refused_with_its_admission(
    registry: Registry, tmp_path: Path, record: object
) -> None:
    """Including the record that would claim an `executed` effort: admission
    knows what was asked and what was sent, never what a vendor ran."""
    with pytest.raises(RegistryError):
        registry.admit_run(
            _claude_row("run-malformed", registry, str(tmp_path)),
            True,
            1000,
            60_000,
            effort_profile=record,  # type: ignore[arg-type]
        )
    assert registry.get_run("run-malformed") is None
    assert registry.read_run_events("run-malformed") == []
    assert registry.read_claim(str(tmp_path)) is None


@pytest.mark.parametrize(
    "stored",
    [
        ["high"],
        [{"requested": "high"}],
        [{"requested": "high", "sent": 5}],
        [{"requested": "high", "sent": "high", "executed": "max"}],
        [{"requested": "high", "sent": "high"}, {"requested": "high", "sent": "high"}],
        [{"requested": "high", "sent": "high"}, {"requested": "low", "sent": "low"}],
    ],
    ids=["not-a-mapping", "no-sent", "not-text", "forged", "repeated", "contradicted"],
)
def test_a_stored_record_that_is_malformed_or_duplicated_is_refused_on_read(
    registry: Registry, tmp_path: Path, stored: list[object]
) -> None:
    """The reader picks no record and repairs none: a store holding a malformed or
    a second effort record is answered with a typed refusal, through the same
    door every consumer reads."""
    sv = _sv(registry, tmp_path)
    run_id = "run-damaged-record"
    create_run_directory(sv._run_root_path, run_id, dir_fd=sv._run_root)
    registry.admit_run(
        _claude_row(run_id, registry, str(tmp_path)), False, 1000, 60_000
    )
    for record in stored:
        registry.append_run_event(
            run_id, at=1000, kind="lifecycle", payload={"effort_profile": record}
        )
    with pytest.raises(RegistryError):
        registry.effort_profile(run_id)
    with pytest.raises(RegistryError):
        sv.status(run_id)


@pytest.mark.parametrize(
    "damage", ["wrong-kind", "outer-key", "broken-first", "broken-trailing"]
)
def test_invalid_record_placement_or_chain_refuses_reader_and_status_without_repair(
    registry: Registry, tmp_path: Path, damage: str
) -> None:
    sv = _sv(registry, tmp_path)
    run_id = "run-invalid-effort-journal"
    create_run_directory(sv._run_root_path, run_id, dir_fd=sv._run_root)
    registry.admit_run(
        _claude_row(run_id, registry, str(tmp_path)), False, 1000, 60_000
    )
    payload = {"effort_profile": {"requested": "max", "sent": "high"}}
    if damage == "wrong-kind":
        registry.append_run_event(run_id, at=1000, kind="usage", payload=payload)
    elif damage == "outer-key":
        registry.append_run_event(
            run_id, at=1000, kind="lifecycle", payload={**payload, "extra": True}
        )
    else:
        if damage == "broken-trailing":
            registry.append_run_event(
                run_id, at=1000, kind="lifecycle", payload=payload
            )
        # Plant a structurally valid but unverified append on this test's store.
        # All append-only triggers remain installed; no row is edited or deleted.
        sql(
            registry,
            "INSERT INTO run_events(run_id, seq, at, kind, payload, chained_hash) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                run_id,
                registry.read_run_events(run_id)[-1].seq + 1,
                1001,
                "usage" if damage == "broken-trailing" else "lifecycle",
                json.dumps({} if damage == "broken-trailing" else payload),
                "0" * 64,
            ),
        )
    original = registry.read_run_events(run_id)
    assert registry.verify_run_event_chain(run_id) is (
        damage in ("wrong-kind", "outer-key")
    )
    with pytest.raises(RegistryError, match="event chain|malformed effort"):
        registry.effort_profile(run_id)
    with pytest.raises(RegistryError, match="event chain|malformed effort"):
        sv.status(run_id)
    assert registry.get_run(run_id) is not None
    assert registry.read_run_events(run_id) == original


def test_run_show_answers_what_the_caller_asked_and_what_the_launcher_was_sent(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The caller pinned `ultra`; the picker resolved `high` and THAT is the value
    every launcher is handed (`child.build_child_env`, `claude_restricted`,
    `native.prepare_native`). Both are answered, each as itself."""
    _wired(monkeypatch, _picked("ultra", "high"))
    sv = _sv(registry, tmp_path)
    accepted = sv.execute("hi", str(tmp_path), read_only=True, effort="ultra")
    status = sv.status(accepted.run_id, timeout_ms=5000)
    assert _plain(status.effort) == _recorded("ultra", "high")
    assert _effort_records(registry, accepted.run_id) == [
        {"requested": "ultra", "sent": "high"}
    ]


def test_the_recorded_effort_survives_a_roster_that_now_resolves_another(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _wired(monkeypatch, _picked("ultra", "high"))
    sv = _sv(registry, tmp_path)
    accepted = sv.execute("hi", str(tmp_path), read_only=True, effort="ultra")
    assert sv.status(accepted.run_id, timeout_ms=5000).phase.is_terminal()
    # The roster now defaults to another effort and the picker resolves it: an
    # answer recomputed from the live roster would report `low` for a run that
    # was launched at `high`.
    (roster_home / ".nott" / "spawn.toml").write_text(
        '[defaults]\nplugins = ["/tmp/plugin-root"]\neffort = "low"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        supervisor.picker, "pick_execution", lambda *a, **k: _picked("low", "low")
    )
    assert _plain(sv.status(accepted.run_id).effort) == _recorded("ultra", "high")


@pytest.mark.parametrize(
    "requested,sent", [(None, "high"), (None, None)], ids=["default", "nothing"]
)
def test_a_claude_run_never_claims_an_executed_effort(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    requested: str | None,
    sent: str | None,
) -> None:
    _wired(monkeypatch, _picked(requested, sent))
    sv = _sv(registry, tmp_path)
    accepted = sv.execute("hi", str(tmp_path), read_only=True)
    status = sv.status(accepted.run_id, timeout_ms=5000)
    # A caller that asked for nothing is RECORDED as having asked for nothing,
    # which is not the same answer as unknown; a launcher handed no effort is
    # recorded as handed none, never as a `max` read into a None. Claude echoes
    # no effort: `executed` is unknown, never the value it was sent.
    assert _plain(status.effort) == _recorded(requested, sent)
    rendered = _to_jsonable(status)["effort"]
    assert json.loads(json.dumps(rendered)) == _recorded(requested, sent)
    assert _effort_records(registry, accepted.run_id) == [
        {"requested": requested, "sent": sent}
    ]


def test_a_run_admitted_before_the_record_existed_reads_unknown(
    registry: Registry, tmp_path: Path
) -> None:
    sv = _sv(registry, tmp_path)
    run_id = "run-before-the-record"
    create_run_directory(sv._run_root_path, run_id, dir_fd=sv._run_root)
    registry.admit_run(
        _claude_row(run_id, registry, str(tmp_path)), False, 1000, 60_000
    )
    # Usage coverage carries no effort. The reader adds no effort record.
    original = registry.read_run_events(run_id)
    assert _effort_records(registry, run_id) == []
    assert _plain(registry.effort_profile(run_id)) == UNKNOWN_PROFILE
    assert _plain(sv.status(run_id).effort) == UNKNOWN_PROFILE
    assert registry.read_run_events(run_id) == original


def test_a_resumed_native_run_answers_its_own_record_and_not_its_origin(
    registry: Registry, tmp_path: Path
) -> None:
    """A resume is a NEW run on the SAME thread. Each answers what ITS admission
    recorded: the origin's profile is not inherited by the resume, and the
    resume's does not overwrite the origin's."""
    sv = _sv(registry, tmp_path)
    context = NativeSessionContext(str(tmp_path))
    origin = _codex_row("run-origin", registry, str(tmp_path))
    registry.admit_run(
        origin,
        False,
        1,
        sv.RUN_CLAIM_LEASE_MS,
        native_context=context,
        effort_profile={"requested": "high", "sent": "high"},
    )
    create_run_directory(sv._run_root_path, origin.run_id, dir_fd=sv._run_root)
    registry.update_run(
        dataclasses.replace(
            origin, phase=RunPhase.COMPLETED, thread_id="thread-shared", updated_at=2
        )
    )
    resumed = dataclasses.replace(
        _codex_row("run-resumed", registry, str(tmp_path)),
        thread_id="thread-shared",
        created_at=3,
        updated_at=3,
    )
    registry.admit_run(
        resumed,
        False,
        3,
        sv.RUN_CLAIM_LEASE_MS,
        native_context=context,
        effort_profile={"requested": None, "sent": "low"},
    )
    create_run_directory(sv._run_root_path, resumed.run_id, dir_fd=sv._run_root)
    assert _plain(sv.status("run-origin").effort) == _recorded("high", "high")
    assert _plain(sv.status("run-resumed").effort) == _recorded(None, "low")
    assert _effort_records(registry, "run-origin") == [
        {"requested": "high", "sent": "high"}
    ]
    assert _effort_records(registry, "run-resumed") == [
        {"requested": None, "sent": "low"}
    ]


# ---------------------------------------------------------------------------
# `executed`: no vendor proves it today, Codex included
# ---------------------------------------------------------------------------


class _DoubleSession:
    """An app-server turn that answers one prepared result."""

    def __init__(
        self, child: FakeSpawnedChild, result: app_server.AppServerResult
    ) -> None:
        self.child = child
        self._result = result

    def run(self, *args: object, **kwargs: object) -> app_server.AppServerResult:
        return self._result


def test_a_completed_codex_turn_leaves_executed_unknown(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The installed Codex echoes no effort for the turn, so a completed native
    run keeps the profile its admission recorded and nothing more: the drive
    appends no effort record, and `executed` is not the `high` it was sent."""
    sv = _sv(registry, tmp_path)
    run_id = "run-native"
    context = NativeSessionContext(str(tmp_path))
    row = _codex_row(run_id, registry, str(tmp_path))
    registry.admit_run(
        row,
        False,
        1,
        sv.RUN_CLAIM_LEASE_MS,
        native_context=context,
        effort_profile={"requested": None, "sent": "high"},
    )
    run_dir = create_run_directory(sv._run_root_path, run_id, dir_fd=sv._run_root)
    # What the vendor's pump would have written: a completed turn is never an
    # empty stream, and the drive refuses to record zero records seen.
    run_dir.stdout.write_bytes(
        b'{"method":"turn/completed","params":{"threadId":"thread-native",'
        b'"turn":{"id":"turn-native","status":"completed"}}}\n'
    )
    run_dir.stderr.write_bytes(b"")
    launch = native.NativeLaunch(
        ("/never-start/vendor",),
        {},
        SecretNeedles.none(),
        context,
        NativeAccount(tmp_path),
    )
    picked = picker.PickerResult(
        "codex", Provider(name="codex", kind="codex", model="m"), "one", "m", "high"
    )
    session = _DoubleSession(
        FakeSpawnedChild(_spawn_real("true")),
        app_server.AppServerResult(
            "thread-native", "turn-native", "completed", "PONG", {"totalTokens": 9}
        ),
    )
    monkeypatch.setattr(Supervisor, "_native_readonly_mcp", lambda *a, **k: ({}, {}))
    monkeypatch.setattr(
        app_server.AppServerTurn,
        "start",
        classmethod(lambda cls, *a, **k: contextlib.nullcontext(session)),
    )
    sv._drive_native_run(row, run_dir, "ping", picked, launch, 0, {})

    status = sv.status(run_id)
    assert status.phase is RunPhase.COMPLETED
    assert _plain(status.effort) == _recorded(None, "high")
    assert registry.verify_run_event_chain(run_id) is True
    assert _effort_records(registry, run_id) == [{"requested": None, "sent": "high"}]


# ---------------------------------------------------------------------------
# Additive: every other answer is byte for byte what it was
# ---------------------------------------------------------------------------


def test_run_show_keeps_every_field_it_had_and_every_print_it_made() -> None:
    assert tuple(field.name for field in dataclasses.fields(RunStatus)) == (
        *RUN_SHOW_FIELDS,
        "effort",
        "inference_profile",
        "session",
        "transport",
        "exit_code",
        "cost_usd",
        "model",
    )
    rendered = _to_jsonable(
        RunStatus(
            run_id="r1",
            phase=RunPhase.COMPLETED,
            run_dir="/runs/r1",
            failure_reason=None,
            total_seq=3,
            requested_provider="claude",
            executed_provider="claude",
            executed_account=None,
            warning=None,
            tokens=42,
            marker="DONE",
        )
    )
    assert tuple(rendered) == (
        *RUN_SHOW_FIELDS,
        "effort",
        "session",
        "transport",
        "exit_code",
        "cost_usd",
        "model",
    )
    assert rendered["model"] is None
    assert rendered["session"] is None
    assert rendered["transport"] is None
    assert rendered["exit_code"] is None
    assert rendered["cost_usd"] is None
    # A run_show built without a record answers the unknown profile, as plain
    # JSON a consumer can branch on.
    assert rendered["effort"] == UNKNOWN_PROFILE
    assert json.loads(json.dumps(rendered))["effort"] == UNKNOWN_PROFILE
    # The tool says the field exists.
    contract = next(
        tool for tool in NSPAWN_TOOL_CONTRACTS if tool.name == "nspawn_run_show"
    )
    assert "effort" in contract.description
    # The idempotency print is untouched: the same request keys the same run.
    assert PINNED_PRINTS == {
        "effort": driver.request_fingerprint(
            "nspawn_run_start",
            {"prompt": "hi", "cwd": "/", "effort": "high", "read_only": True},
            group=None,
            strict=False,
        ),
        "no-effort": driver.request_fingerprint(
            "nspawn_run_start",
            {"prompt": "hi", "cwd": "/", "read_only": True},
            group=None,
            strict=False,
        ),
    }
