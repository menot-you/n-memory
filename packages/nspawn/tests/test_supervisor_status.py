"""Supervisor status contracts."""

from __future__ import annotations

import dataclasses
import json
import os
import threading
import time
from pathlib import Path

import pytest
from registry_reset import sql
from supervisor_support import (
    FakeSpawnedChild,
    _admit_pending,
    _classify_by_exit_code,
    _drain_worker_threads,
    _spawn_real,
    _stop_every_reaper,
    _sv,
    _wire_l8_and_l3,
    fake_picked,
    fake_provider,
    registry,
    roster_home,
    wired_success,
)

from nspawn import picker, supervisor
from nspawn.mcp import McpServer
from nspawn.registry import Registry, RegistryError
from nspawn.rows import RunPhase
from nspawn.supervisor import (
    InvalidRunRequest,
    Supervisor,
    UnknownRun,
)

# Export imported fixtures so pytest discovers them in this module.
__all__ = [
    "_drain_worker_threads",
    "_stop_every_reaper",
    "fake_picked",
    "fake_provider",
    "registry",
    "roster_home",
    "wired_success",
]


def _park_before_launch(
    monkeypatch: pytest.MonkeyPatch, release: threading.Event
) -> threading.Event:
    """Hold the run's worker short of its spawn, outside the supervisor lock.

    `compose_child_argv` runs in `_spawn_and_wait` before `_launching` takes
    `sv._lock`; the returned event is set once the worker is parked there."""
    parked = threading.Event()

    def _parked_argv(*a: object, **k: object) -> list[str]:
        parked.set()
        assert release.wait(5), "the test never released the parked worker"
        return ["claude", "-p"]

    monkeypatch.setattr(supervisor.child, "compose_child_argv", _parked_argv)
    return parked


# ---------------------------------------------------------------------------
# status()
# ---------------------------------------------------------------------------


def test_status_refuses_unknown_run(registry: Registry, tmp_path: Path) -> None:
    sv = _sv(registry, tmp_path)
    with pytest.raises(UnknownRun):
        sv.status("no-such-run")


@pytest.mark.parametrize(
    "usage, expected",
    [
        ([], None),
        ([{"cost_usd": None}], None),
        ([{"cost_usd": "0.0"}], "0.0"),
        ([{"cost_usd": "0.125"}], "0.125"),
        ([{"cost_usd": "0.125"}, {"cost_usd": "0.25"}], "0.25"),
        ([{"cost_usd": "0.125"}, {"cost_usd": None}], None),
        ([{"cost_usd": "0.125"}, {}], None),
    ],
    ids=["absent", "unknown", "zero", "cost", "latest", "latest-unknown", "legacy"],
)
def test_run_show_reads_latest_recorded_cost_without_summing(
    registry: Registry,
    tmp_path: Path,
    usage: list[dict[str, object]],
    expected: str | None,
) -> None:
    sv = _sv(registry, tmp_path)
    run_id, _ = _admit_pending(
        sv, registry, tmp_path, worktree=str(tmp_path), claim=False
    )
    row = registry.get_run(run_id)
    assert row is not None
    registry.update_run(dataclasses.replace(row, phase=RunPhase.COMPLETED))
    for payload in usage:
        registry.append_run_event(run_id, at=2, kind="usage", payload=payload)
    registry.append_run_event(run_id, at=3, kind="lifecycle", payload={"closed": True})
    before = registry.read_run_events(run_id)

    shown = McpServer(supervisor=sv).nspawn_run_show(run_id=run_id)

    assert shown["cost_usd"] == expected
    assert registry.read_run_events(run_id) == before


@pytest.mark.parametrize("cost", [False, 1, "bad", "NaN", "Infinity", "-0.01"])
def test_run_show_refuses_invalid_recorded_cost(
    registry: Registry, tmp_path: Path, cost: object
) -> None:
    sv = _sv(registry, tmp_path)
    run_id, _ = _admit_pending(
        sv, registry, tmp_path, worktree=str(tmp_path), claim=False
    )
    registry.append_run_event(run_id, at=2, kind="usage", payload={"cost_usd": cost})

    with pytest.raises(RegistryError, match="cost_usd"):
        McpServer(supervisor=sv).nspawn_run_show(run_id=run_id)


def test_status_immediate_read_does_not_block(
    registry: Registry, tmp_path: Path, roster_home: Path, wired_success: None
) -> None:
    sv = _sv(registry, tmp_path)
    accepted = sv.execute("hi", str(tmp_path), read_only=True)
    status = sv.status(accepted.run_id, timeout_ms=0)
    assert status.run_id == accepted.run_id


def test_status_long_poll_returns_as_soon_as_terminal(
    registry: Registry, tmp_path: Path, roster_home: Path, wired_success: None
) -> None:
    sv = _sv(registry, tmp_path)
    accepted = sv.execute("hi", str(tmp_path), read_only=True)
    started = time.monotonic()
    status = sv.status(accepted.run_id, timeout_ms=5000)
    elapsed = time.monotonic() - started
    assert status.phase.is_terminal()
    assert elapsed < 4.5  # woke on notify, not the 5s ceiling


def test_status_long_poll_returns_current_state_on_timeout(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_picked: picker.PickerResult,
) -> None:
    def _blocking_spawn(*a: object, **k: object) -> FakeSpawnedChild:
        return FakeSpawnedChild(_spawn_real("sleep", "5"))

    monkeypatch.setattr(
        supervisor.picker, "pick_execution", lambda *a, **k: fake_picked
    )
    _wire_l8_and_l3(monkeypatch, _blocking_spawn)
    monkeypatch.setattr(
        supervisor.terminal, "classify_terminal_run", _classify_by_exit_code
    )
    sv = _sv(registry, tmp_path)
    accepted = sv.execute("hi", str(tmp_path), read_only=True)
    status = sv.status(accepted.run_id, timeout_ms=100)
    assert status.phase is RunPhase.RUNNING
    sv.close(accepted.run_id)


def test_result_wait_that_expires_names_the_running_phase(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_picked: picker.PickerResult,
) -> None:
    """A result wait that ends first returns no answer; the phase says why."""

    def _blocking_spawn(*a: object, **k: object) -> FakeSpawnedChild:
        return FakeSpawnedChild(_spawn_real("sleep", "5"))

    monkeypatch.setattr(
        supervisor.picker, "pick_execution", lambda *a, **k: fake_picked
    )
    _wire_l8_and_l3(monkeypatch, _blocking_spawn)
    monkeypatch.setattr(
        supervisor.terminal, "classify_terminal_run", _classify_by_exit_code
    )
    sv = _sv(registry, tmp_path)
    accepted = sv.execute("hi", str(tmp_path), read_only=True)
    answer = McpServer(supervisor=sv).call_tool(
        "nspawn_result_show", {"run_id": accepted.run_id, "timeout_ms": 100}
    )
    result = json.loads(answer["content"][0]["text"])
    assert result["phase"] == "running"
    assert result["final_text"] is None
    sv.close(accepted.run_id)


def test_status_long_poll_refuses_when_the_run_vanishes_mid_wait(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_picked: picker.PickerResult,
) -> None:
    release = threading.Event()
    monkeypatch.setattr(
        supervisor.picker, "pick_execution", lambda *a, **k: fake_picked
    )
    _wire_l8_and_l3(monkeypatch, lambda *a, **k: FakeSpawnedChild(_spawn_real("true")))
    # The worker parks BEFORE `_launching` takes the supervisor lock: parking
    # inside `spawn_child` held that lock, so the deleter below could only run
    # once the park timed out — five seconds of every run spent waiting.
    _park_before_launch(monkeypatch, release)
    monkeypatch.setattr(
        supervisor.terminal, "classify_terminal_run", _classify_by_exit_code
    )
    sv = _sv(registry, tmp_path)
    accepted = sv.execute("hi", str(tmp_path), read_only=True)

    def _delete_and_notify() -> None:
        time.sleep(0.05)
        with sv._lock:
            sql(
                sv._registry,
                "DELETE FROM claims WHERE run_id = ?",
                (accepted.run_id,),
            )
            sql(sv._registry, "DELETE FROM runs WHERE run_id = ?", (accepted.run_id,))
            sv._lock.notify_all()

    deleter = threading.Thread(target=_delete_and_notify)
    deleter.start()
    try:
        with pytest.raises(UnknownRun, match="vanished"):
            sv.status(accepted.run_id, timeout_ms=5000)
    finally:
        deleter.join()
        release.set()


def test_status_observes_result_and_stdout_mtime_without_rewriting_the_row(
    registry: Registry, tmp_path: Path
) -> None:
    sv = _sv(registry, tmp_path)
    run_id, _ = _admit_pending(sv, registry, tmp_path, worktree=str(tmp_path))
    stdout = sv._run_root_path / run_id / "stdout"
    stdout.write_bytes(b'{"type":"assistant"}\n')
    first_mtime_ns = 1_787_900_001_000_000_000
    os.utime(stdout, ns=(first_mtime_ns, first_mtime_ns))
    before = registry.get_run(run_id)

    unfinished = sv.status(run_id)

    assert unfinished.has_result is False
    assert unfinished.stdout_mtime_ns == first_mtime_ns
    assert registry.get_run(run_id) == before

    with stdout.open("ab") as stream:
        stream.write(
            b'{"type":"result","is_error":false,'
            b'"terminal_reason":"completed","result":"PONG"}\n'
        )
    second_mtime_ns = first_mtime_ns + 2_000_000_000
    os.utime(stdout, ns=(second_mtime_ns, second_mtime_ns))

    finished = sv.status(run_id)

    assert finished.has_result is True
    assert finished.stdout_mtime_ns == second_mtime_ns
    assert registry.get_run(run_id) == before


def test_result_falls_back_to_the_last_result_frame_in_stdout(
    registry: Registry, tmp_path: Path
) -> None:
    sv = _sv(registry, tmp_path)
    run_id, _ = _admit_pending(sv, registry, tmp_path, worktree=str(tmp_path))
    (sv._run_root_path / run_id / "stdout").write_bytes(
        b'{"type":"result","is_error":false,'
        b'"terminal_reason":"completed","result":"first"}\n'
        b'{"type":"assistant"}\n'
        b'{"type":"result","is_error":false,'
        b'"terminal_reason":"completed","result":"last"}\n'
    )

    assert sv.result(run_id).final_text == "last"


def test_status_and_result_carry_a_bounded_stderr_tail(
    registry: Registry, tmp_path: Path
) -> None:
    from nspawn.limits import MAX_DURABLE_OBSERVATION_BYTES

    sv = _sv(registry, tmp_path)
    run_id, _ = _admit_pending(sv, registry, tmp_path, worktree=str(tmp_path))
    stderr = sv._run_root_path / run_id / "stderr"
    suffix = b"[claude-code:unrecognized_model]\n"
    payload = b"discarded\n" * 2_000 + suffix
    stderr.write_bytes(payload)
    expected = payload[-MAX_DURABLE_OBSERVATION_BYTES:].decode("utf-8")

    assert sv.status(run_id).stderr_tail == expected
    assert sv.result(run_id).stderr_tail == expected
    assert expected.endswith(suffix.decode("utf-8"))

    missing_id, _ = _admit_pending(
        sv, registry, tmp_path, worktree=str(tmp_path / "missing")
    )
    assert sv.status(missing_id).stderr_tail is None
    empty = sv._run_root_path / missing_id / "stderr"
    empty.write_bytes(b"")
    assert sv.status(missing_id).stderr_tail == ""


# ---------------------------------------------------------------------------
# transcript()
# ---------------------------------------------------------------------------


def test_transcript_refuses_unknown_run(registry: Registry, tmp_path: Path) -> None:
    sv = _sv(registry, tmp_path)
    with pytest.raises(UnknownRun):
        sv.transcript("no-such-run")


def test_transcript_refuses_negative_since_seq(
    registry: Registry, tmp_path: Path, roster_home: Path, wired_success: None
) -> None:
    sv = _sv(registry, tmp_path)
    accepted = sv.execute("hi", str(tmp_path), read_only=True)
    with pytest.raises(InvalidRunRequest):
        sv.transcript(accepted.run_id, since_seq=-1)


def test_transcript_is_empty_before_anything_was_written(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_picked: picker.PickerResult,
) -> None:
    release = threading.Event()
    monkeypatch.setattr(
        supervisor.picker, "pick_execution", lambda *a, **k: fake_picked
    )
    _wire_l8_and_l3(monkeypatch, lambda *a, **k: FakeSpawnedChild(_spawn_real("true")))
    parked = _park_before_launch(monkeypatch, release)
    monkeypatch.setattr(
        supervisor.terminal, "classify_terminal_run", _classify_by_exit_code
    )
    sv = _sv(registry, tmp_path)
    accepted = sv.execute("hi", str(tmp_path), read_only=True)
    try:
        # The worker is provably short of its spawn, so nothing can have been
        # written yet; parking inside `spawn_child` held the supervisor lock
        # and made this read wait out the park instead.
        assert parked.wait(5)
        page = sv.transcript(accepted.run_id)
        assert page.lines == ()
        assert page.truncated is False
    finally:
        release.set()


def test_transcript_returns_only_newline_terminated_lines_and_paginates(
    registry: Registry, tmp_path: Path
) -> None:
    sv = _sv(registry, tmp_path)
    run_id, _ = _admit_pending(sv, registry, tmp_path, worktree=str(tmp_path))
    run_dir = sv._run_root_path / run_id
    (run_dir / "stdout").write_bytes(b"one\ntwo\nthree\npartial-no-newline")

    first = sv.transcript(run_id, since_seq=0, max_bytes=1024)
    assert first.lines == ("one", "two", "three")
    assert first.truncated is False
    assert first.next_seq == 3

    small = sv.transcript(run_id, since_seq=0, max_bytes=4)
    assert small.lines == ("one",)
    assert small.truncated is True
    assert small.next_seq == 1

    tail = sv.transcript(run_id, since_seq=small.next_seq, max_bytes=1024)
    assert tail.lines == ("two", "three")

    beyond = sv.transcript(run_id, since_seq=99, max_bytes=1024)
    assert beyond.lines == ()
    assert beyond.next_seq == 3
    assert beyond.truncated is False


def test_transcript_always_makes_progress_even_over_a_single_oversized_line(
    registry: Registry, tmp_path: Path
) -> None:
    sv = _sv(registry, tmp_path)
    run_id, _ = _admit_pending(sv, registry, tmp_path, worktree=str(tmp_path))
    run_dir = sv._run_root_path / run_id
    (run_dir / "stdout").write_bytes(b"x" * 100 + b"\n" + b"y\n")

    page = sv.transcript(run_id, since_seq=0, max_bytes=1)
    assert page.lines == ("x" * 100,)
    assert page.next_seq == 1
    assert page.truncated is True


def test_transcript_default_and_nonpositive_max_bytes_fall_back_to_the_cap(
    registry: Registry, tmp_path: Path
) -> None:
    sv = _sv(registry, tmp_path)
    run_id, _ = _admit_pending(sv, registry, tmp_path, worktree=str(tmp_path))
    run_dir = sv._run_root_path / run_id
    (run_dir / "stdout").write_bytes(b"one\ntwo\n")

    assert sv.transcript(run_id).lines == ("one", "two")
    assert sv.transcript(run_id, max_bytes=0).lines == ("one", "two")
    assert sv.transcript(run_id, max_bytes=-5).lines == ("one", "two")


def test_transcript_replaces_invalid_utf8_rather_than_raising(
    registry: Registry, tmp_path: Path
) -> None:
    sv = _sv(registry, tmp_path)
    run_id, _ = _admit_pending(sv, registry, tmp_path, worktree=str(tmp_path))
    run_dir = sv._run_root_path / run_id
    (run_dir / "stdout").write_bytes(b"\xff\xfe\n")

    page = sv.transcript(run_id)
    assert len(page.lines) == 1


#: Deterministic, versioned corpus for the transcript file: bytes the CHILD
#: process wrote (already redacted by L4, but never nSPAWN's own), read back
#: by `transcript()`. Fixed, not hypothesis-derived, so a failure reproduces.
TRANSCRIPT_FUZZ_CORPUS: tuple[bytes, ...] = (
    b"",
    b"\n",
    b"\n\n\n\n\n",
    b"\x00\x00\x00\n",
    b"\xff\xfe\xfd\xfc\n",
    b"no newline at all",
    b"line one\nline two\nno trailing newline",
    b"\xed\xa0\x80\n",  # a lone UTF-16 surrogate half, invalid UTF-8
    b"{" * 1024 + b"\n",
    b"\n" * 1024,
    ("x" * 500_000).encode() + b"\n",  # one absurdly oversized line
    b"line\rwith\rcarriage\rreturns\n",
    "événement complet en UTF-8 válido\n".encode(),
    b"\xc0\xaf\xc0\xae\n",  # overlong UTF-8 encoding
)


@pytest.mark.parametrize("payload", TRANSCRIPT_FUZZ_CORPUS)
def test_transcript_survives_the_versioned_fuzz_corpus(
    registry: Registry, tmp_path: Path, payload: bytes
) -> None:
    """No entry in the fixed corpus may raise an untyped exception; the page
    is always a well-formed TranscriptPage, bounded and string-decoded."""
    sv = _sv(registry, tmp_path)
    run_id, _ = _admit_pending(sv, registry, tmp_path, worktree=str(tmp_path))
    (sv._run_root_path / run_id / "stdout").write_bytes(payload)

    page = sv.transcript(run_id, since_seq=0, max_bytes=4096)

    assert isinstance(page.lines, tuple)
    for line in page.lines:
        line.encode("utf-8")  # must not raise


# -- the fleet listing ---------------------------------------------------------


def test_fleet_lists_every_run_under_one_clock(
    registry: Registry, tmp_path: Path
) -> None:
    """The read an orchestrator makes every tick, against the REAL supervisor.

    `mcp.nspawn_run_show` calls `supervisor.fleet()`; the door's own suite drives a
    FakeSupervisor that carries the method, so those tests stayed green over a
    supervisor that did not have it at all. This one binds the name to the
    object production uses, which is the only binding that matters.
    """
    page = _sv(registry, tmp_path).fleet()
    assert isinstance(page.observed_at, int)
    assert page.runs == ()


def test_fleet_carries_the_lane_of_each_run(registry: Registry, tmp_path: Path) -> None:
    """A fleet of UUIDs cannot be reasoned about: the lane is the field read."""
    from nspawn.rows import CLAUDE_CONFINEMENT_POLICY_ONLY, Backend, RunRow

    registry.open(now=1)
    for run_id, lane, tokens in (
        ("run-fleet-1", "nm-ledger", 7),
        ("run-fleet-2", None, 0),
    ):
        registry.admit_run(
            RunRow(
                run_id=run_id,
                thread_id=None,
                worktree=f"'/opt/test/worktrees/'{lane or 'sem-lane'}",
                phase=RunPhase.PENDING,
                sandbox=CLAUDE_CONFINEMENT_POLICY_ONLY,
                model="opus",
                owner_instance=registry.instance_id,
                created_at=1,
                updated_at=1,
                backend=Backend.CLAUDE,
                lane=lane,
                tokens=tokens,
            ),
            True,
            1,
            Supervisor.RUN_CLAIM_LEASE_MS,
        )
    page = _sv(registry, tmp_path).fleet()
    by_id = {run.run_id: run for run in page.runs}
    assert by_id["run-fleet-1"].lane == "nm-ledger"
    assert by_id["run-fleet-1"].tokens == 7
    assert by_id["run-fleet-2"].lane is None
    # The page carries ONE clock and the rows carry the store's own bytes. A
    # weaker `created_at <= observed_at` looks like it guards this and does not:
    # any plausible past timestamp satisfies it, so a row whose time was
    # rewritten in flight still passes. Pin the exact value instead.
    assert [run.created_at for run in page.runs] == [1, 1]


def test_fleet_pagination_and_prune(registry: Registry, tmp_path: Path) -> None:
    """Fleet pages at MAX_RUN_PAGE (128) and reaper _tick calls prune_closed_runs."""
    from nspawn.rows import CLAUDE_CONFINEMENT_POLICY_ONLY, Backend, RunRow

    for i in range(200):
        run_id = f"r-{i:04d}"
        row = RunRow(
            run_id=run_id,
            worktree=f"/tmp/wt-{i}",
            phase=RunPhase.CLOSED,
            sandbox=CLAUDE_CONFINEMENT_POLICY_ONLY,
            model="opus",
            owner_instance=registry.instance_id,
            created_at=1000,
            updated_at=1000 + i,
            backend=Backend.CLAUDE,
        )
        registry.insert_run(row)

    # Built after the inserts: its reaper ticks on the wall clock, and a tick
    # during a slow insert loop would prune these long-closed rows early.
    sv = _sv(registry, tmp_path)
    page = sv.fleet()
    assert len(page.runs) == 128

    now_ms = 1000 + 200 + getattr(sv, "RETAIN_CLOSED_MS", 24 * 3600 * 1000) + 5000
    sv._tick(now=now_ms)

    remaining = registry.list_runs()
    assert len(remaining) == 0


def test_fleet_shows_the_newest_run_first_and_its_group(
    registry: Registry, tmp_path: Path
) -> None:
    """A store larger than one page still shows what was just dispatched.

    Measured on 2026-09-24: 1565 runs, ordered by `run_id` (a uuid), so the
    first page of 128 was an arbitrary slice and a run launched a minute ago
    was absent from `nspawn_run_list`. Here the newest run carries the HIGHEST
    `run_id`, which is exactly the row a `run_id` order pushes off page one.
    """
    from nspawn.rows import CLAUDE_CONFINEMENT_POLICY_ONLY, Backend, RunRow

    for i in range(200):
        registry.insert_run(
            RunRow(
                run_id=f"r-{i:04d}",
                worktree=f"/tmp/wt-{i}",
                phase=RunPhase.CLOSED,
                sandbox=CLAUDE_CONFINEMENT_POLICY_ONLY,
                model="opus",
                owner_instance=registry.instance_id,
                created_at=1000 + i,
                updated_at=1000 + i,
                backend=Backend.CLAUDE,
                group_name=f"g-{i % 3}",
            )
        )

    page = _sv(registry, tmp_path).fleet()

    assert page.runs[0].run_id == "r-0199"
    assert page.runs[0].group == "g-1"
    created = [run.created_at for run in page.runs]
    assert created == sorted(created, reverse=True)
    assert created[-1] == 1000 + 200 - len(page.runs)


def test_status_live_progress_tokens_seq(registry: Registry, tmp_path: Path) -> None:
    sv = _sv(registry, tmp_path)
    run_id, _ = _admit_pending(sv, registry, tmp_path, worktree=str(tmp_path))
    stdout = sv._run_root_path / run_id / "stdout"
    stdout.write_bytes(
        b'{"type":"assistant","message":{"usage":{"input_tokens":100,"output_tokens":50}}}\n'
        b'{"type":"assistant","message":{"usage":{"input_tokens":150,"output_tokens":60}}}\n'
    )
    status = sv.status(run_id)
    assert status.tokens == 210
    assert status.total_seq == 2


def test_to_status_handles_oserror_on_stdout(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sv = _sv(registry, tmp_path)
    run_id, _ = _admit_pending(sv, registry, tmp_path, worktree=str(tmp_path))

    def _broken_read(self: Path) -> bytes:
        if self.name == "stdout":
            raise OSError("cannot read")
        return Path.read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", _broken_read)
    status = sv.status(run_id)
    assert status.total_seq == 0


# ---------------------------------------------------------------------------
# _update_phase(): a later warning ADDS to the row, it never erases it.
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class _WarningRow:
    """Only the fields `_update_phase` reads: phase, timestamp and warning."""

    phase: RunPhase
    warning: str | None
    updated_at: int = 0


class _OneRowRegistry:
    def __init__(self, row: _WarningRow) -> None:
        self.row = row

    def get_run(self, run_id: str) -> _WarningRow:
        return self.row

    def update_run(
        self, row: _WarningRow, *, drop_claim: bool = False, annotate: bool = False
    ) -> None:
        self.row = row


@pytest.mark.parametrize(
    ("current", "incoming", "expected"),
    [
        ("picker", None, "picker"),
        ("picker", "picker", "picker"),
        ("picker", "terminal", "picker; terminal"),
        ("class; terminal", "terminal", "class; terminal"),
        ("class; terminal", "terminal; telemetry", "class; terminal; telemetry"),
    ],
)
def test_update_phase_composes_warnings_by_segment(
    tmp_path: Path, current: str, incoming: str | None, expected: str
) -> None:
    """Whole-string comparison is not enough on EITHER side.

    `class` then `terminal` then `terminal` would repeat the terminal segment,
    and a composed `terminal; telemetry` arriving on a row that already carries
    `terminal` would duplicate both.
    """
    sv = object.__new__(Supervisor)
    sv._lock = threading.Condition()
    sv._unpublished = {}
    rows = _OneRowRegistry(_WarningRow(phase=RunPhase.RUNNING, warning=current))
    sv._registry = rows  # type: ignore[assignment]
    sv._update_phase("run", RunPhase.RUNNING, warning=incoming)
    assert rows.row.warning == expected
