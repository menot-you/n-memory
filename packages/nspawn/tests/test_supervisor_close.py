"""Supervisor close contracts."""

from __future__ import annotations

import contextlib
import json
import os
import signal
import sys
import threading
import time
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import nott_database
import pytest
from mcp_fakes import handle
from registry_reset import sql
from supervisor_support import (
    FakeSpawnedChild,
    _admit_claimed,
    _admit_pending,
    _await_running,
    _classify_by_exit_code,
    _drain_worker_threads,
    _spawn_real,
    _stop_every_reaper,
    _sv,
    _url,
    _wire_l8_and_l3,
    fake_picked,
    fake_provider,
    registry,
    roster_home,
    wired_success,
)

from nspawn import picker, supervisor
from nspawn.mcp import McpServer
from nspawn.process_identity import ProcessIdentity, capture_process_identity
from nspawn.redact import SecretNeedles
from nspawn.registry import POISONED_LEASE_UNTIL, Registry
from nspawn.rows import RunPhase
from nspawn.supervisor import (
    CloseReadback,
    ProcessTreeReadback,
    RunCloseRejected,
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


# ---------------------------------------------------------------------------
# close()
# ---------------------------------------------------------------------------


def test_close_refuses_unknown_run(registry: Registry, tmp_path: Path) -> None:
    sv = _sv(registry, tmp_path)
    with pytest.raises(UnknownRun):
        sv.close("no-such-run")


def test_close_is_idempotent_on_an_already_closed_run(
    registry: Registry, tmp_path: Path
) -> None:
    sv = _sv(registry, tmp_path)
    run_id, _ = _admit_pending(sv, registry, tmp_path, worktree=str(tmp_path))
    row = registry.get_run(run_id)
    assert row is not None
    registry.update_run(replace(row, phase=RunPhase.CLOSED))
    readback = sv.close(run_id)
    assert readback == CloseReadback(
        run_id, RunPhase.CLOSED, ProcessTreeReadback((), ()), True
    )


def test_close_transitions_directly_when_no_identity_was_ever_captured(
    registry: Registry, tmp_path: Path
) -> None:
    sv = _sv(registry, tmp_path)
    run_id, _ = _admit_pending(sv, registry, tmp_path, worktree=str(tmp_path))
    readback = sv.close(run_id)
    assert readback.claim_released is True
    assert readback.phase is RunPhase.CLOSED
    assert readback.tree == ProcessTreeReadback((), ())
    assert registry.get_run(run_id) is not None
    assert registry.get_run(run_id).phase is RunPhase.CLOSED  # type: ignore[union-attr]


def test_a_run_closed_before_its_child_took_a_turn_names_why(
    registry: Registry, tmp_path: Path
) -> None:
    """Audit 2026-10-09: such a run read `failure_reason: null`, as if nothing
    had stopped it. The close names the phase it interrupted."""
    sv = _sv(registry, tmp_path)
    run_id, _ = _admit_pending(sv, registry, tmp_path, worktree=str(tmp_path))
    sv.close(run_id)
    reason = sv.status(run_id).failure_reason
    assert reason in ("closed while pending", "closed while spawning")
    assert sv.result(run_id).failure_reason == reason


def test_closing_a_finished_run_invents_no_failure(
    registry: Registry, tmp_path: Path
) -> None:
    sv = _sv(registry, tmp_path)
    run_id, _ = _admit_pending(sv, registry, tmp_path, worktree=str(tmp_path))
    sv._update_phase(run_id, RunPhase.COMPLETED, final_text="done")
    sv.close(run_id)
    assert sv.status(run_id).failure_reason is None


def test_close_without_an_identity_really_deletes_the_claim_it_reports(
    registry: Registry, tmp_path: Path
) -> None:
    """F-012: the path that never signals a process still has a claim to give
    back, and the worktree it fences must be free for the NEXT run."""
    sv = _sv(registry, tmp_path)
    run_id, _ = _admit_pending(sv, registry, tmp_path, worktree=str(tmp_path))
    held = registry.read_claim(str(tmp_path))
    assert held is not None and held.run_id == run_id

    readback = sv.close(run_id)

    assert registry.read_claim(str(tmp_path)) is None
    assert readback.claim_released is True


def test_close_reports_the_claim_still_held_when_the_release_does_not_land(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The discriminating case: the row closes, the DELETE is neutered, and the
    readback must say so. A hardcoded `True` — what this field used to be —
    passes every other close test in this file and fails only here."""
    sv = _sv(registry, tmp_path)
    run_id, _ = _admit_pending(sv, registry, tmp_path, worktree=str(tmp_path))

    def _close_without_drop(
        self: Registry, run_id: str, *a: object, **k: object
    ) -> bool:
        sql(self, "UPDATE runs SET phase = 'closed' WHERE run_id = ?", (run_id,))
        return True

    monkeypatch.setattr(Registry, "close_owned", _close_without_drop)

    readback = sv.close(run_id)

    assert readback.claim_released is False
    still_held = registry.read_claim(str(tmp_path))
    assert still_held is not None and still_held.run_id == run_id


def test_close_of_an_already_closed_run_gives_back_the_leftover_claim(
    registry: Registry, tmp_path: Path
) -> None:
    """A row moved to CLOSED by `update_run` releases its ordinary claim in the
    same transaction. If an already-closed run is closed again with a leftover
    claim, closing it again frees it, and the readback is the table's answer."""
    sv = _sv(registry, tmp_path)
    run_id, _ = _admit_pending(sv, registry, tmp_path, worktree=str(tmp_path))
    row = registry.get_run(run_id)
    assert row is not None
    registry.update_run(replace(row, phase=RunPhase.CLOSED))
    assert registry.read_claim(str(tmp_path)) is None

    # Simulate an external or pre-existing leftover claim on the closed run
    registry.claim_worktree(str(tmp_path), run_id, now=1, lease_ms=10_000)
    leftover = registry.read_claim(str(tmp_path))
    assert leftover is not None and leftover.run_id == run_id

    readback = sv.close(run_id)

    assert registry.read_claim(str(tmp_path)) is None
    assert readback.claim_released is True


def test_close_reports_released_when_a_successor_holds_the_worktree(
    registry: Registry, tmp_path: Path
) -> None:
    """A claim naming ANOTHER run is not this run's fence: the question the
    field answers is whether THIS run still holds the worktree."""
    sv = _sv(registry, tmp_path)
    run_id, _ = _admit_pending(sv, registry, tmp_path, worktree=str(tmp_path))
    sv.close(run_id)
    successor, _ = _admit_pending(sv, registry, tmp_path, worktree=str(tmp_path))

    assert sv._claim_is_released(str(tmp_path), run_id) is True
    assert sv._claim_is_released(str(tmp_path), successor) is False


def test_close_skips_a_redundant_write_when_a_racing_close_already_landed(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No identity was ever captured, and a second close() call's own inner
    read races a first close() that already committed CLOSED — the inner
    `current is not None and current.phase is not RunPhase.CLOSED` guard must
    skip the now-redundant write rather than attempt (and fail) another one."""
    sv = _sv(registry, tmp_path)
    run_id, _ = _admit_pending(sv, registry, tmp_path, worktree=str(tmp_path))
    open_row = registry.get_run(run_id)
    assert open_row is not None

    calls = {"n": 0}

    def _open_then_closed(self: Registry, target_run_id: str) -> object:
        calls["n"] += 1
        if calls["n"] == 1:
            return open_row  # the top-level check: still open
        return replace(open_row, phase=RunPhase.CLOSED)  # the inner race

    monkeypatch.setattr(Registry, "get_run", _open_then_closed)
    update_calls: list[object] = []
    real_update_run = Registry.update_run
    monkeypatch.setattr(
        Registry,
        "update_run",
        lambda self, row: (update_calls.append(row), real_update_run(self, row))[1],
    )

    readback = sv.close(run_id)

    assert readback.claim_released is True
    assert readback.phase is RunPhase.CLOSED
    assert update_calls == []  # the redundant write was skipped, not attempted


def test_close_kills_the_whole_process_group_via_the_live_child(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_picked: picker.PickerResult,
) -> None:
    # A bash leader that keeps a same-group child alive: proves the discover +
    # kill path reaches a descendant, not only the captured leader pid.
    spawn_argv = ["bash", "-c", "sleep 30 & wait"]
    monkeypatch.setattr(
        supervisor.picker, "pick_execution", lambda *a, **k: fake_picked
    )
    _wire_l8_and_l3(
        monkeypatch, lambda *a, **k: FakeSpawnedChild(_spawn_real(*spawn_argv))
    )
    # The worker thread's blocked spawned.wait() naturally unblocks once
    # close() kills the process, so classify_terminal_run DOES still run on
    # that background thread — this is the same fake execute()'s happy path
    # uses, not a guard against it running.
    monkeypatch.setattr(
        supervisor.terminal, "classify_terminal_run", _classify_by_exit_code
    )
    sv = _sv(registry, tmp_path)
    accepted = _admit_claimed(sv, "hi", str(tmp_path))
    _await_running(sv, accepted.run_id)
    leader_pid = sv._children[accepted.run_id].identity.pid
    assert registry.read_claim(str(tmp_path)) is not None  # a fence to give back

    readback = sv.close(accepted.run_id)

    assert readback.claim_released is True
    assert registry.read_claim(str(tmp_path)) is None  # the TABLE, not the return
    assert readback.phase is RunPhase.CLOSED
    assert len(readback.tree.checked) >= 1
    assert readback.tree.survivors == ()
    with contextlib.suppress(ProcessLookupError):
        os.killpg(leader_pid, signal.SIGKILL)  # safety net if the assertion fired first


def test_close_after_a_proven_death_reads_the_claim_back_rather_than_trusting(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_picked: picker.PickerResult,
) -> None:
    """`close_owned` deletes the claim inside its own transaction — but the
    field reports the TABLE, not that method's reputation. Here the transition
    lands and the claim deliberately does not, and the readback says so."""
    monkeypatch.setattr(
        supervisor.picker, "pick_execution", lambda *a, **k: fake_picked
    )
    _wire_l8_and_l3(
        monkeypatch, lambda *a, **k: FakeSpawnedChild(_spawn_real("sleep", "30"))
    )
    monkeypatch.setattr(
        supervisor.terminal, "classify_terminal_run", _classify_by_exit_code
    )
    sv = _sv(registry, tmp_path)
    accepted = _admit_claimed(sv, "hi", str(tmp_path))
    _await_running(sv, accepted.run_id)

    def _close_row_only(
        self: Registry,
        run_id: str,
        expected: ProcessIdentity,
        now: int,
        *,
        drained: bool = False,
    ) -> bool:
        sql(
            self,
            "UPDATE runs SET phase = 'closed' WHERE run_id = ?",
            (run_id,),
        )
        sql(
            self,
            "INSERT INTO claims (worktree, run_id, holder, lease_until) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT (worktree) DO UPDATE SET run_id = EXCLUDED.run_id",
            (str(tmp_path), run_id, self.instance_id, now + 10_000),
        )
        return True

    monkeypatch.setattr(Registry, "close_owned", _close_row_only)

    readback = sv.close(accepted.run_id)

    still_held = registry.read_claim(str(tmp_path))
    assert still_held is not None and still_held.run_id == accepted.run_id
    assert readback.claim_released is False
    assert readback.tree.survivors == ()  # the process really did die


def test_close_reports_survivors_without_releasing_the_claim(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_picked: picker.PickerResult,
) -> None:
    class _UnkillableChild(FakeSpawnedChild):
        def stop_then_kill(self, expected: ProcessIdentity) -> None:
            self.stop_calls.append(expected)  # deliberately does not signal anything

    proc = _spawn_real("sleep", "30")
    monkeypatch.setattr(
        supervisor.picker, "pick_execution", lambda *a, **k: fake_picked
    )
    _wire_l8_and_l3(monkeypatch, lambda *a, **k: _UnkillableChild(proc))
    # The worker thread's blocked spawned.wait() naturally unblocks once
    # close() kills the process, so classify_terminal_run DOES still run on
    # that background thread — this is the same fake execute()'s happy path
    # uses, not a guard against it running.
    monkeypatch.setattr(
        supervisor.terminal, "classify_terminal_run", _classify_by_exit_code
    )
    sv = _sv(registry, tmp_path)
    sv.CLOSE_POLL_TIMEOUT_S = 0.2  # keep the test fast, instance-level override
    accepted = _admit_claimed(sv, "hi", str(tmp_path))
    _await_running(sv, accepted.run_id)

    try:
        readback = sv.close(accepted.run_id)
        assert readback.claim_released is False
        held = registry.read_claim(str(tmp_path))  # the TABLE, not the return
        assert held is not None and held.run_id == accepted.run_id
        assert len(readback.tree.survivors) >= 1
        assert registry.get_run(accepted.run_id).phase is not RunPhase.CLOSED  # type: ignore[union-attr]
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)


def test_close_still_refuses_a_live_child_whose_identity_is_refused(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_picked: picker.PickerResult,
) -> None:
    """The pid-reuse fence survives the close of an already-exited child.

    `close` may treat a refusal from `stop_then_kill` as "already stopped" only
    when our own child has provably exited. A child that is still ALIVE and
    whose identity check refuses is exactly the foreign-identity case: close
    must propagate the refusal, signal nothing, and keep the claim."""
    from nspawn.child import ChildIdentityError

    class _ForeignIdentityChild(FakeSpawnedChild):
        def stop_then_kill(self, expected: ProcessIdentity) -> None:
            self.stop_calls.append(expected)
            raise ChildIdentityError(
                f"process {expected.pid} no longer matches its captured identity"
            )

    proc = _spawn_real("sleep", "30")
    monkeypatch.setattr(
        supervisor.picker, "pick_execution", lambda *a, **k: fake_picked
    )
    _wire_l8_and_l3(monkeypatch, lambda *a, **k: _ForeignIdentityChild(proc))
    monkeypatch.setattr(
        supervisor.terminal, "classify_terminal_run", _classify_by_exit_code
    )
    sv = _sv(registry, tmp_path)
    accepted = _admit_claimed(sv, "hi", str(tmp_path))
    _await_running(sv, accepted.run_id)

    try:
        with pytest.raises(ChildIdentityError):
            sv.close(accepted.run_id)
        assert proc.poll() is None  # nothing was signalled
        held = registry.read_claim(str(tmp_path))
        assert held is not None and held.run_id == accepted.run_id
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)


def test_close_of_a_writable_execute_with_survivors_keeps_its_claim(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The production shape: a writable `execute` claims through
    `claim_worktree = not read_only` — not the `_admit(claim_worktree=True)`
    shortcut the tests above take. The one writable mode is a native write,
    which only codex serves, so the route is codex writing a checkout under a
    declared root; its vendor never starts, and the survivor is planted the way
    the no-claim twin below plants it."""
    from nspawn import native
    from nspawn.owner_roster import NativeAccount, Provider, Roster
    from nspawn.redact import SecretNeedles
    from nspawn.rows import NativeSessionContext

    class _Unkillable(FakeSpawnedChild):
        def stop_then_kill(self, expected: ProcessIdentity) -> None:
            self.stop_calls.append(expected)  # deliberately signals nothing

    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)
    worktree = os.path.realpath(checkout)
    roster = roster_home / ".nott" / "spawn.toml"
    roster.write_text(
        roster.read_text()
        + f"\n[native_write]\nroots = [{json.dumps(str(tmp_path))}]\n"
    )
    provider = Provider(name="fixture", kind="codex", model="m", models=["m"])
    selected = picker.PickerResult(None, provider, "fixture", "m", None, None)
    monkeypatch.setattr(supervisor.picker, "pick_execution", lambda *a, **k: selected)
    monkeypatch.setattr(Roster, "refuse_unservable_spawn", lambda *a: None)
    launch = native.NativeLaunch(
        ("/never-start/vendor",),
        {},
        SecretNeedles.none(),
        NativeSessionContext(str(tmp_path), read_roots=(worktree,)),
        NativeAccount(tmp_path),
    )
    monkeypatch.setattr(native, "prepare_native", lambda *a, **k: launch)
    monkeypatch.setattr(Supervisor, "_drive_native_run", lambda *a, **k: None)
    sv = _sv(registry, tmp_path)
    sv.CLOSE_POLL_TIMEOUT_S = 0.2
    accepted = sv.execute("fix", str(checkout), native_write=True)
    held = registry.read_claim(worktree)
    assert held is not None and held.run_id == accepted.run_id
    proc = _spawn_real("sleep", "30")
    try:
        sv._children[accepted.run_id] = _Unkillable(proc)  # type: ignore[assignment]

        readback = sv.close(accepted.run_id)

        assert readback.tree.survivors  # the process is still alive
        assert readback.claim_released is False
        held = registry.read_claim(worktree)  # the TABLE, not the return
        assert held is not None and held.run_id == accepted.run_id
        assert registry.get_run(accepted.run_id).phase is not RunPhase.CLOSED  # type: ignore[union-attr]
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)


def test_close_with_survivors_reports_released_when_no_claim_was_ever_taken(
    registry: Registry, tmp_path: Path
) -> None:
    """The survivors path READS too. A `review` takes no worktree claim, so a
    survivor there fences nothing — reporting a fence that does not exist is
    the same defect as hiding one that does, pointed the other way."""

    class _Unkillable(FakeSpawnedChild):
        def stop_then_kill(self, expected: ProcessIdentity) -> None:
            self.stop_calls.append(expected)  # deliberately signals nothing

    sv = _sv(registry, tmp_path)
    sv.CLOSE_POLL_TIMEOUT_S = 0.2
    run_id, _ = _admit_pending(sv, registry, tmp_path, worktree=str(tmp_path))
    registry.release_claim(str(tmp_path), run_id)  # as `review` would leave it
    proc = _spawn_real("sleep", "30")
    try:
        sv._children[run_id] = _Unkillable(proc)  # type: ignore[assignment]

        readback = sv.close(run_id)

        assert readback.tree.survivors  # the process is still alive
        assert registry.read_claim(str(tmp_path)) is None
        assert readback.claim_released is True
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)


def test_close_falls_back_to_row_identity_after_a_supervisor_restart(
    registry: Registry, tmp_path: Path
) -> None:
    sv_a = _sv(registry, tmp_path)
    run_id, _ = _admit_pending(sv_a, registry, tmp_path, worktree=str(tmp_path))
    proc = _spawn_real("sleep", "30")
    identity = capture_process_identity(proc.pid)
    row = registry.get_run(run_id)
    assert row is not None
    registry.update_run(
        replace(
            row,
            phase=RunPhase.RUNNING,
            process_identity=identity,
        )
    )
    # In production, a process orphaned by a dead supervisor is reparented to
    # init/a subreaper, which reaps it once killed. This test's own process IS
    # `proc`'s real parent (it called Popen directly), so nothing reaps it on
    # its own — a background waiter stands in for that reparented reaper so
    # the SIGKILL `_best_effort_killpg` sends actually clears the zombie,
    # exactly like `test_wait_identity_dead_true_once_it_dies_mid_poll`.
    reaper = threading.Thread(target=lambda: proc.wait(timeout=10))
    reaper.start()
    # A second Supervisor instance never held this run's SpawnedChild in memory.
    sv_b = _sv(registry, tmp_path)
    try:
        readback = sv_b.close(run_id)
        assert readback.claim_released is True
        assert readback.phase is RunPhase.CLOSED
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        reaper.join(timeout=10)


def test_close_refuses_when_the_registry_fence_rejects_release(
    registry: Registry, tmp_path: Path
) -> None:
    run_id, _ = _admit_pending(
        Supervisor(registry, tmp_path / "runs"),
        registry,
        tmp_path,
        worktree=str(tmp_path),
    )
    proc = _spawn_real("true")
    proc.wait(timeout=5)  # already dead: identity match will read False immediately
    identity = ProcessIdentity(pid=proc.pid, boot_id="does-not-matter", start_ticks=0)
    row = registry.get_run(run_id)
    assert row is not None
    other = Registry(_url(), "i-other-instance").connect()
    other.open(now=1)
    try:
        registry.update_run(
            replace(row, phase=RunPhase.RUNNING, process_identity=identity)
        )
        sv = Supervisor(other, tmp_path / "runs2")
        # Under the new contract, an unpoisoned proven-dead orphan closes from any supervisor.
        readback = sv.close(run_id)
        assert readback.phase is RunPhase.CLOSED
        assert readback.claim_released is True

        # Counterexample: a poisoned claim keeps the registry fence intact.
        wt2 = str(tmp_path / "wt2")
        run_id2, _ = _admit_pending(
            Supervisor(registry, tmp_path / "runs"),
            registry,
            tmp_path / "wt2",
            worktree=wt2,
        )
        row2 = registry.get_run(run_id2)
        assert row2 is not None
        registry.update_run(
            replace(row2, phase=RunPhase.RUNNING, process_identity=identity)
        )
        registry.poison_claim(wt2, run_id2)
        with pytest.raises(RunCloseRejected):
            sv.close(run_id2)
    finally:
        other.close()


def test_a_served_close_of_a_pending_recovery_is_refused_and_keeps_serving(
    registry: Registry, tmp_path: Path
) -> None:
    """A run the retired body-accepted mode left mid-export: its close is a
    typed refusal on the wire, its claim stays, and the server answers the
    next request instead of dying with the escaped exception."""
    sv = Supervisor(registry, tmp_path / "runs")
    run_id, _ = _admit_pending(sv, registry, tmp_path, worktree=str(tmp_path))
    reference = {"path": str(tmp_path / "recovery.json"), "sha256": "a" * 64}
    # At the admission's own clock (created_at 1), inside its claim's lease.
    registry.record_implementation_recovery(run_id, str(tmp_path), 2, 60_000, reference)
    server = McpServer(supervisor=sv)

    answer = handle(
        server,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "nspawn_run_close", "arguments": {"run_id": run_id}},
        },
    )

    assert answer is not None
    assert answer["error"]["data"]["type"] == "RunCloseRejected", answer
    assert "implementation recovery" in answer["error"]["message"]
    assert registry.read_claim(str(tmp_path)) is not None
    row = registry.get_run(run_id)
    assert row is not None and row.phase is not RunPhase.CLOSED


@pytest.mark.parametrize("phase", [RunPhase.SPAWNING, RunPhase.FAILED])
def test_close_of_a_child_the_row_never_published_closes_after_its_death(
    registry: Registry, tmp_path: Path, phase: RunPhase
) -> None:
    """A native run's preparation inspector is this run's live child before the
    row carries any identity: the turn publishes one only when it starts. A
    close in that window, or after the driver wrote its own terminal, proves
    that child dead and must close; the fence has no other process to guard."""
    sv = Supervisor(registry, tmp_path / "runs")
    run_id, _ = _admit_pending(sv, registry, tmp_path, worktree=str(tmp_path))
    row = registry.get_run(run_id)
    assert row is not None and row.process_identity is None
    registry.update_run(replace(row, phase=phase))
    inspector = FakeSpawnedChild(_spawn_real("sleep", "30"))
    # Reaped as its driver's pump would: an unreaped zombie still matches.
    threading.Thread(target=inspector.process.wait, daemon=True).start()
    sv._children[run_id] = inspector  # type: ignore[assignment]
    try:
        readback = sv.close(run_id)
        assert readback.phase is RunPhase.CLOSED
        assert readback.tree.survivors == ()
        assert inspector.identity in readback.tree.checked
        assert inspector.process.poll() is not None
        assert readback.claim_released is True
        assert registry.read_claim(str(tmp_path)) is None
        closed = registry.get_run(run_id)
        assert closed is not None and closed.phase is RunPhase.CLOSED
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(inspector.process.pid, signal.SIGKILL)
        inspector.process.wait(timeout=5)


def test_close_of_an_unpublished_child_still_keeps_a_poisoned_claim(
    registry: Registry, tmp_path: Path
) -> None:
    """The unpublished-child close keeps the identity path's poison fence: a
    claim nobody could prove custody of refuses it, and stays."""
    sv = Supervisor(registry, tmp_path / "runs")
    run_id, _ = _admit_pending(sv, registry, tmp_path, worktree=str(tmp_path))
    registry.poison_claim(str(tmp_path), run_id)
    inspector = FakeSpawnedChild(_spawn_real("sleep", "30"))
    threading.Thread(target=inspector.process.wait, daemon=True).start()
    sv._children[run_id] = inspector  # type: ignore[assignment]
    try:
        with pytest.raises(RunCloseRejected):
            sv.close(run_id)
        assert registry.read_claim(str(tmp_path)) is not None
        row = registry.get_run(run_id)
        assert row is not None and row.phase is not RunPhase.CLOSED
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(inspector.process.pid, signal.SIGKILL)
        inspector.process.wait(timeout=5)


def test_the_unidentified_fence_refuses_a_row_that_gained_an_identity(
    registry: Registry, tmp_path: Path
) -> None:
    """The registry, not a read before it, decides: a close fenced on a row
    that names no process matches nothing once an identity is published."""
    run_id, _ = _admit_pending(
        Supervisor(registry, tmp_path / "runs"),
        registry,
        tmp_path,
        worktree=str(tmp_path),
    )
    live = _spawn_real("sleep", "30")
    try:
        row = registry.get_run(run_id)
        assert row is not None
        registry.update_run(
            replace(
                row,
                # ORPHANED passes both phase fences, so only the identity refuses.
                phase=RunPhase.ORPHANED,
                process_identity=capture_process_identity(live.pid),
            )
        )
        assert registry.close_owned(run_id, None, int(time.time() * 1000)) is False
        assert registry.read_claim(str(tmp_path)) is not None
        assert registry.close_orphaned(run_id, None, int(time.time() * 1000)) is False
    finally:
        live.kill()
        live.wait(timeout=5)


def test_close_still_refuses_when_the_row_names_another_live_process(
    registry: Registry, tmp_path: Path
) -> None:
    """The counterpart: the tracked child is dead, but the row names a
    different process that is alive. The fence refuses, and the claim stays."""
    sv = Supervisor(registry, tmp_path / "runs")
    run_id, _ = _admit_pending(sv, registry, tmp_path, worktree=str(tmp_path))
    stranger = _spawn_real("sleep", "30")
    try:
        row = registry.get_run(run_id)
        assert row is not None
        registry.update_run(
            replace(
                row,
                phase=RunPhase.RUNNING,
                process_identity=capture_process_identity(stranger.pid),
            )
        )
        tracked = FakeSpawnedChild(_spawn_real("true"))
        tracked.process.wait(timeout=5)
        sv._children[run_id] = tracked  # type: ignore[assignment]
        with pytest.raises(RunCloseRejected):
            sv.close(run_id)
        assert stranger.poll() is None
        assert registry.read_claim(str(tmp_path)) is not None
    finally:
        stranger.kill()
        stranger.wait(timeout=5)


def test_close_with_no_child_refuses_a_row_that_gained_an_identity(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No child is tracked and the row named no process when the close read it;
    a driver publishes one before the close lands. The close must not settle
    that run over a live process: the registry fence decides, not the read."""
    sv = Supervisor(registry, tmp_path / "runs")
    run_id, _ = _admit_pending(sv, registry, tmp_path, worktree=str(tmp_path))
    before = registry.get_run(run_id)
    assert before is not None and before.process_identity is None
    live = _spawn_real("sleep", "30")
    try:
        registry.update_run(
            replace(
                before,
                phase=RunPhase.RUNNING,
                process_identity=capture_process_identity(live.pid),
            )
        )
        reads = iter([before])
        read = Registry.get_run

        def first_read_before_publication(self: Registry, key: str) -> object:
            return next(reads, None) or read(self, key)

        monkeypatch.setattr(Registry, "get_run", first_read_before_publication)
        with pytest.raises(RunCloseRejected):
            sv.close(run_id)
        monkeypatch.setattr(Registry, "get_run", read)
        assert live.poll() is None
        row = registry.get_run(run_id)
        assert row is not None and row.phase is RunPhase.RUNNING
        assert row.process_identity == capture_process_identity(live.pid)
        assert registry.read_claim(str(tmp_path)) is not None
    finally:
        live.kill()
        live.wait(timeout=5)


def test_close_with_no_process_and_no_slice_keeps_a_poisoned_claim(
    registry: Registry, tmp_path: Path
) -> None:
    """A poisoned claim says nobody could prove custody. With no process on the
    row and no slice on record there is no death proof: the close refuses,
    names the manual step, and the claim stays."""
    sv = Supervisor(registry, tmp_path / "runs")
    run_id, _ = _admit_pending(sv, registry, tmp_path, worktree=str(tmp_path))
    registry.poison_claim(str(tmp_path), run_id)

    with pytest.raises(RunCloseRejected, match="by hand"):
        sv.close(run_id)

    claim = registry.read_claim(str(tmp_path))
    assert claim is not None and claim.lease_until == POISONED_LEASE_UNTIL
    row = registry.get_run(run_id)
    assert row is not None and row.phase is not RunPhase.CLOSED


def test_close_of_a_closed_row_keeps_a_poisoned_claim_without_a_slice(
    registry: Registry, tmp_path: Path
) -> None:
    """A row already CLOSED by an unfenced write still carries a poisoned claim:
    the leftover release is the close finishing, and it needs the same death
    proof as any other, so it refuses with the manual step instead."""
    sv = Supervisor(registry, tmp_path / "runs")
    run_id, _ = _admit_pending(sv, registry, tmp_path, worktree=str(tmp_path))
    registry.poison_claim(str(tmp_path), run_id)
    row = registry.get_run(run_id)
    assert row is not None
    registry.update_run(replace(row, phase=RunPhase.CLOSED))

    with pytest.raises(RunCloseRejected, match="by hand"):
        sv.close(run_id)

    claim = registry.read_claim(str(tmp_path))
    assert claim is not None and claim.lease_until == POISONED_LEASE_UNTIL


def test_close_with_no_child_accepts_a_close_another_process_landed(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another process closes the run between this close's read and its fenced
    write. The run is closed, which is what was asked: the answer is CLOSED,
    not a refusal."""
    sv = Supervisor(registry, tmp_path / "runs")
    run_id, _ = _admit_pending(sv, registry, tmp_path, worktree=str(tmp_path))
    before = registry.get_run(run_id)
    assert before is not None
    assert registry.close_owned(run_id, None, int(time.time() * 1000)) is True
    reads = iter([before])
    read = Registry.get_run

    def first_read_before_the_other_close(self: Registry, key: str) -> object:
        return next(reads, None) or read(self, key)

    monkeypatch.setattr(Registry, "get_run", first_read_before_the_other_close)

    readback = sv.close(run_id)

    assert (readback.phase, readback.claim_released) == (RunPhase.CLOSED, True)


@pytest.mark.needs("systemd_user", "cgroup")
def test_close_with_no_process_and_an_absent_slice_releases_a_poisoned_claim(
    registry: Registry, tmp_path: Path
) -> None:
    """The recorded slice is gone: systemd removed its group, so nothing of the
    run can still write. That is the death proof, and it counts as drained."""
    sv = Supervisor(registry, tmp_path / "runs")
    run_id, _ = _admit_pending(sv, registry, tmp_path, worktree=str(tmp_path))
    unit = f"nottcontroller{uuid.uuid4().hex}.slice"
    uid = os.geteuid()
    cgroup = f"/sys/fs/cgroup/user.slice/user-{uid}.slice/user@{uid}.service/{unit}"
    assert not Path(cgroup).exists()
    registry.append_run_event(
        run_id,
        at=2,
        kind="lifecycle",
        payload={"writer_scope": {"unit": unit, "cgroup": cgroup}},
    )
    registry.poison_claim(str(tmp_path), run_id)

    readback = sv.close(run_id)

    assert (readback.phase, readback.claim_released) == (RunPhase.CLOSED, True)
    assert registry.read_claim(str(tmp_path)) is None


@pytest.mark.parametrize(
    "failure",
    [
        nott_database.BusyError("database is locked"),
        nott_database.Error("disk I/O error"),
    ],
    ids=["busy", "sqlite"],
)
def test_a_store_failure_in_a_served_close_is_an_error_frame_and_keeps_serving(
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    """The store fails mid-close. The caller gets a typed error frame naming
    the failure, the claim stays, and the server answers the next request
    instead of dying with the escaped exception."""
    sv = Supervisor(registry, tmp_path / "runs")
    run_id, _ = _admit_pending(sv, registry, tmp_path, worktree=str(tmp_path))

    def failing(self: Registry, *args: object, **kwargs: object) -> bool:
        raise failure

    monkeypatch.setattr(Registry, "close_owned", failing)
    server = McpServer(supervisor=sv)

    answer = handle(
        server,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "nspawn_run_close", "arguments": {"run_id": run_id}},
        },
    )

    assert answer is not None and answer["error"]["code"] == -32603, answer
    assert answer["error"]["data"] == {
        "tool": "nspawn_run_close",
        "type": type(failure).__name__,
    }
    assert str(failure) in answer["error"]["message"]
    assert registry.read_claim(str(tmp_path)) is not None


# ---------------------------------------------------------------------------
# Concurrency: shared registry connection, shared close() race
# ---------------------------------------------------------------------------


def test_concurrent_admissions_do_not_corrupt_the_shared_connection(
    registry: Registry, tmp_path: Path, roster_home: Path, wired_success: None
) -> None:
    sv = _sv(registry, tmp_path)
    run_ids: list[str] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def _one(index: int) -> None:
        try:
            cwd = tmp_path / f"cwd-{index}"
            cwd.mkdir()
            accepted = _admit_claimed(sv, f"hello {index}", str(cwd))
            status = sv.status(accepted.run_id, timeout_ms=5000)
            with lock:
                run_ids.append(status.run_id)
                if not status.phase.is_terminal():
                    errors.append(RuntimeError(f"run {status.run_id} never terminated"))
        except BaseException as error:  # noqa: BLE001 - collected, asserted below
            with lock:
                errors.append(error)

    threads = [threading.Thread(target=_one, args=(i,)) for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors
    assert len(run_ids) == len(set(run_ids)) == 6


def test_concurrent_close_on_the_same_run_resolves_exactly_once(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_picked: picker.PickerResult,
) -> None:
    monkeypatch.setattr(
        supervisor.picker, "pick_execution", lambda *a, **k: fake_picked
    )
    _wire_l8_and_l3(
        monkeypatch, lambda *a, **k: FakeSpawnedChild(_spawn_real("sleep", "5"))
    )
    # The worker thread's blocked spawned.wait() naturally unblocks once
    # close() kills the process, so classify_terminal_run DOES still run on
    # that background thread — this is the same fake execute()'s happy path
    # uses, not a guard against it running.
    monkeypatch.setattr(
        supervisor.terminal, "classify_terminal_run", _classify_by_exit_code
    )
    sv = _sv(registry, tmp_path)
    accepted = _admit_claimed(sv, "hi", str(tmp_path))
    _await_running(sv, accepted.run_id)

    results: list[object] = []
    lock = threading.Lock()

    def _close_once() -> None:
        try:
            readback = sv.close(accepted.run_id)
            with lock:
                results.append(readback)
        except (UnknownRun, RunCloseRejected) as error:
            with lock:
                results.append(error)

    threads = [threading.Thread(target=_close_once) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert len(results) == 4
    closed_readbacks = [
        r for r in results if isinstance(r, CloseReadback) and r.claim_released
    ]
    assert len(closed_readbacks) >= 1
    final_row = registry.get_run(accepted.run_id)
    assert final_row is not None
    assert final_row.phase is RunPhase.CLOSED


def test_status_long_poll_wakes_promptly_on_notify(
    registry: Registry, tmp_path: Path, roster_home: Path, wired_success: None
) -> None:
    sv = _sv(registry, tmp_path)
    accepted = sv.execute("hi", str(tmp_path), read_only=True)
    started = time.monotonic()
    status = sv.status(accepted.run_id, timeout_ms=4000)
    elapsed = time.monotonic() - started
    assert status.phase.is_terminal()
    assert elapsed < 3.5


# U4 — reaper wiring. The reaper functions (heartbeat_instance,
# mark_orphans, remove_instance) existed for a long time as dead code
# (zero production callers, refuter measured); U4 turns them on. The two
# end-to-end behaviours are:
#   (a) every supervisor tick refreshes its instance's heartbeat AND
#       orphans stale runs;
#   (b) clean shutdown drops the instance row.
# The proof is at the supervisor level — does `_tick()` reach the registry,
# and does `close()` reach the registry — without spinning the daemon.


class _FakeRegistry:
    """A stand-in for `Registry` that records its method calls.

    The supervisor talks to the registry only through `heartbeat_instance`,
    `mark_orphans`, `remove_instance`, and a handful of admission methods.
    U4 needs to prove `_tick()` reaches the first two and `close()` reaches
    the third — anything beyond that is the registry's contract, exercised
    by `test_registry.py`.
    """

    def __init__(self, instance_id: str = "i-fake") -> None:
        self.instance_id = instance_id
        self.heartbeat_calls: list[int] = []
        self.orphan_calls: list[tuple[int, int]] = []
        self.remove_calls: int = 0

    def heartbeat_instance(self, now: int) -> bool:
        self.heartbeat_calls.append(now)
        return True

    def mark_orphans(self, now: int, stale_after_ms: int) -> int:
        self.orphan_calls.append((now, stale_after_ms))
        return 0

    def remove_instance(self) -> None:
        self.remove_calls += 1


def test_tick_refreshes_heartbeat_and_calls_mark_orphans(tmp_path: Path) -> None:
    """U4 — `_tick()` calls heartbeat_instance and mark_orphans at the registry."""
    fake = _FakeRegistry()
    # Stop the daemon before assertions so the test does not race the wall clock.
    sup = supervisor.Supervisor(fake, tmp_path / "runs")  # type: ignore[arg-type]
    sup._stop_tick.set()
    sup._tick_thread.join(timeout=2)
    fake.heartbeat_calls.clear()
    fake.orphan_calls.clear()
    try:
        alive, orphans = sup._tick(now=42_000)
        assert alive is True
        assert orphans == 0
        assert fake.heartbeat_calls == [42_000]
        assert fake.orphan_calls == [(42_000, supervisor.Supervisor.STALE_AFTER_MS)]
    finally:
        sup.shutdown()


def test_close_drops_the_instance_row(tmp_path: Path) -> None:
    """U4 — `shutdown()` reaches `registry.remove_instance`."""
    fake = _FakeRegistry()
    sup = supervisor.Supervisor(fake, tmp_path / "runs")  # type: ignore[arg-type]
    assert fake.remove_calls == 0
    sup.shutdown()
    assert fake.remove_calls == 1


class _RemoveFailureRegistry(_FakeRegistry):
    def __init__(self, error: Exception) -> None:
        super().__init__()
        self.error = error

    def remove_instance(self) -> None:
        raise self.error


class _CustodyFailure:
    def __init__(self, error: Exception) -> None:
        self.identity = capture_process_identity(os.getpid())
        self.error = error
        self.stop_calls: list[ProcessIdentity] = []

    def stop_then_kill(self, expected: ProcessIdentity) -> None:
        self.stop_calls.append(expected)
        raise self.error


def _stopped_supervisor(fake: _FakeRegistry, run_root: Path) -> Supervisor:
    sup = Supervisor(fake, run_root)
    sup._stop_tick.set()
    sup._tick_thread.join(timeout=2)
    return sup


def test_session_close_exposes_an_unexpected_registry_failure(
    tmp_path: Path,
) -> None:
    fake = _RemoveFailureRegistry(RuntimeError("registry invariant broke"))
    sup = _stopped_supervisor(fake, tmp_path / "runs")
    server = McpServer(supervisor=sup)

    with pytest.raises(RuntimeError, match="registry invariant broke"):
        server.close()

    assert server.supervisor is None


def test_session_close_logs_and_contains_a_typed_store_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _RemoveFailureRegistry(nott_database.OperationalError("registry offline"))
    sup = _stopped_supervisor(fake, tmp_path / "runs")
    server = McpServer(supervisor=sup)

    server.close()

    assert server.supervisor is None
    captured = capsys.readouterr()
    assert "nspawn.shutdown" in captured.err
    assert "OperationalError: registry offline" in captured.err


def test_session_close_exposes_an_unexpected_custody_failure(tmp_path: Path) -> None:
    fake = _FakeRegistry()
    sup = _stopped_supervisor(fake, tmp_path / "runs")
    spawned = _CustodyFailure(RuntimeError("custody invariant broke"))
    sup._children["r-broken"] = spawned
    server = McpServer(supervisor=sup)

    with pytest.raises(RuntimeError, match="custody invariant broke"):
        server.close()

    assert spawned.stop_calls == [spawned.identity]
    assert fake.remove_calls == 0
    assert server.supervisor is None


def test_session_close_logs_and_contains_an_expected_custody_race(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _FakeRegistry()
    sup = _stopped_supervisor(fake, tmp_path / "runs")
    spawned = _CustodyFailure(supervisor.child.ChildIdentityError("child vanished"))
    sup._children["r-gone"] = spawned
    server = McpServer(supervisor=sup)

    server.close()

    assert spawned.stop_calls == [spawned.identity]
    assert fake.remove_calls == 1
    assert server.supervisor is None
    captured = capsys.readouterr()
    assert "nspawn.shutdown" in captured.err
    assert "ChildIdentityError:" in captured.err
    assert "child vanished" in captured.err


def test_tick_loop_is_a_daemon_started_in_init(tmp_path: Path) -> None:
    """U4 — the tick daemon is alive after Supervisor construction and stops on shutdown."""
    fake = _FakeRegistry()
    sup = supervisor.Supervisor(fake, tmp_path / "runs")  # type: ignore[arg-type]
    try:
        # Give the daemon one tick worth of wall time.
        time.sleep(0.05)
        # At least one heartbeat call must have landed (daemon runs every
        # TICK_INTERVAL_S — for a 50 ms sleep vs 10 s tick this may or may
        # not have fired; assert the thread is alive instead).
        assert sup._tick_thread.is_alive()
        assert not sup._stop_tick.is_set()
    finally:
        sup.shutdown()
    assert sup._stop_tick.is_set()
    # Thread terminates within the join timeout.
    sup._tick_thread.join(timeout=5)
    assert not sup._tick_thread.is_alive()


def test_supervisor_shutdown_handles_remove_instance_store_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _RemoveFailureRegistry(
        nott_database.OperationalError("registry drop failed")
    )
    sup = _stopped_supervisor(fake, tmp_path / "runs")
    sup.shutdown()
    captured = capsys.readouterr()
    assert "nspawn.shutdown: remove_instance failed" in captured.err
    assert "registry drop failed" in captured.err


def test_supervisor_shutdown_handles_remove_instance_oserror(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _RemoveFailureRegistry(OSError("disk failure"))
    sup = _stopped_supervisor(fake, tmp_path / "runs")
    sup.shutdown()
    captured = capsys.readouterr()
    assert "nspawn.shutdown: remove_instance failed" in captured.err
    assert "disk failure" in captured.err


def test_supervisor_shutdown_handles_spawned_without_stop_then_kill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _PlainChild:
        def __init__(self) -> None:
            self.identity = supervisor.ProcessIdentity(
                pid=1, boot_id="b", start_ticks=1
            )

    killed = False

    def _fake_kill(pid: int) -> None:
        nonlocal killed
        killed = True

    monkeypatch.setattr(supervisor, "_best_effort_killpg", _fake_kill)
    fake = _FakeRegistry()
    sup = _stopped_supervisor(fake, tmp_path / "runs")
    sup._children["r-plain"] = _PlainChild()  # type: ignore[assignment]
    sup.shutdown()
    assert killed


def test_log_tick_failure_during_finalizing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """When the interpreter is finalizing, _log_tick_failure does not write to stderr."""
    monkeypatch.setattr(sys, "is_finalizing", lambda: True)
    supervisor._log_tick_failure(RuntimeError("tick error in shutdown"))
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == ""


@pytest.mark.parametrize("kind", ["codex", "agy"])
def test_closed_native_admission_never_starts_its_delayed_worker(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    from nspawn import agy, app_server, native
    from nspawn.native import SecretNeedles
    from nspawn.owner_roster import NativeAccount, Provider
    from nspawn.rows import Backend, NativeSessionContext, RunRow
    from nspawn.supervisor import create_run_directory

    sv = _sv(registry, tmp_path)
    run_id = "closed-before-native-start"
    context = NativeSessionContext(str(tmp_path))
    row = RunRow(
        run_id=run_id,
        worktree=str(tmp_path),
        phase=RunPhase.PENDING,
        sandbox="workspace-write",
        model="m",
        backend=Backend(kind),
        owner_instance=registry.instance_id,
        created_at=1,
        updated_at=1,
        executed_provider=kind,
        executed_account="one",
    )
    registry.admit_run(row, True, 1, sv.RUN_CLAIM_LEASE_MS, native_context=context)
    directory = create_run_directory(sv._run_root_path, run_id, dir_fd=sv._run_root)
    assert sv.close(run_id).phase is RunPhase.CLOSED
    launch = native.NativeLaunch(
        ("/never-start/vendor",),
        {},
        SecretNeedles.none(),
        context,
        NativeAccount(tmp_path),
    )
    picked = picker.PickerResult(
        kind, Provider(name=kind, kind=kind, model="m"), "one", "m", None, None
    )

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("closed run launched a vendor process")

    monkeypatch.setattr(app_server.AppServerTurn, "start", forbidden)
    monkeypatch.setattr(agy.AgyTurn, "start", forbidden)
    sv._drive_native_run(row, directory, "must never execute", picked, launch, 1000, {})
    assert registry.get_run(run_id).phase is RunPhase.CLOSED
    assert run_id not in sv._children


def test_close_idempotent_on_terminal_run(registry: Registry, tmp_path: Path) -> None:
    sv = _sv(registry, tmp_path)
    run_id, _ = _admit_pending(sv, registry, tmp_path, worktree=str(tmp_path))
    row = registry.get_run(run_id)
    assert row is not None
    registry.update_run(replace(row, phase=RunPhase.COMPLETED, updated_at=2000))

    first = sv.close(run_id)
    assert first.phase is RunPhase.CLOSED
    assert first.claim_released is True

    second = sv.close(run_id)
    assert second.phase is RunPhase.CLOSED
    assert second.claim_released is True


class _RealSignalChild(FakeSpawnedChild):
    """The production `stop_then_kill`: refuses, typed, when the identity no
    longer matches."""

    def stop_then_kill(self, expected: ProcessIdentity) -> None:
        self.stop_calls.append(expected)
        supervisor.child.SpawnedChild.stop_then_kill(self, expected)  # type: ignore[arg-type]


def test_d1_close_of_a_completed_run_releases_the_claim_without_signalling(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_picked: picker.PickerResult,
) -> None:
    monkeypatch.setattr(
        supervisor.picker, "pick_execution", lambda *a, **k: fake_picked
    )
    children: list[_RealSignalChild] = []

    def _factory(*a: object, **k: object) -> _RealSignalChild:
        children.append(_RealSignalChild(_spawn_real("true")))
        return children[-1]

    _wire_l8_and_l3(monkeypatch, _factory)
    monkeypatch.setattr(
        supervisor.terminal, "classify_terminal_run", _classify_by_exit_code
    )
    sv = _sv(registry, tmp_path)
    accepted = _admit_claimed(sv, "hi", str(tmp_path))
    status = sv.status(accepted.run_id, timeout_ms=10_000)
    assert status.phase is RunPhase.COMPLETED  # the child exited and was reaped
    assert registry.read_claim(str(tmp_path)) is not None  # the lease outlives it

    readback = sv.close(accepted.run_id)

    assert children[0].stop_calls == []  # nothing was signalled
    assert readback.phase is RunPhase.CLOSED
    assert registry.read_claim(str(tmp_path)) is None
    assert readback.claim_released is True


def test_d1_close_never_signals_a_recycled_pid_on_the_row_fallback_path(
    registry: Registry, tmp_path: Path
) -> None:
    """The row names a pid that is LIVE with a different start_ticks — the
    kernel handed our dead child's pid to a stranger. The stranger stays
    untouched and the claim still goes."""
    sv = _sv(registry, tmp_path)
    run_id, _ = _admit_pending(sv, registry, tmp_path, worktree=str(tmp_path))
    stranger = _spawn_real("sleep", "30")
    try:
        real = capture_process_identity(stranger.pid)
        recycled = replace(real, start_ticks=real.start_ticks + 1)
        row = registry.get_run(run_id)
        assert row is not None
        registry.update_run(
            replace(row, phase=RunPhase.COMPLETED, process_identity=recycled)
        )

        readback = sv.close(run_id)

        assert stranger.poll() is None  # the stranger was never signalled
        assert readback.phase is RunPhase.CLOSED
        assert readback.tree.survivors == ()
        assert registry.read_claim(str(tmp_path)) is None
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(stranger.pid, signal.SIGKILL)
        stranger.wait(timeout=10)


# ---------------------------------------------------------------------------
# C2: an ORPHANED row — its owning instance died and a peer's sweep relabelled
# it — is closed through `close_orphaned`, whose fence names the PHASE. The
# `close_owned` fence names THIS instance, which an orphan by definition is
# not, so it refused and left the worktree fenced until the lease expired.
# ---------------------------------------------------------------------------


def test_c2_close_of_an_orphaned_row_releases_the_claim_without_signalling(
    registry: Registry, tmp_path: Path
) -> None:
    """The row is ORPHANED and owned by an instance that is gone; its triple
    names no live process. Nothing is signalled, and the phase moves while the
    claim goes in the SAME transaction."""
    sv = _sv(registry, tmp_path)
    run_id, _ = _admit_pending(sv, registry, tmp_path, worktree=str(tmp_path))
    stranger = _spawn_real("sleep", "30")
    try:
        real = capture_process_identity(stranger.pid)
        recycled = replace(real, start_ticks=real.start_ticks + 1)
        row = registry.get_run(run_id)
        assert row is not None
        registry.update_run(
            replace(
                row,
                phase=RunPhase.ORPHANED,
                owner_instance="i-dead",
                process_identity=recycled,
            )
        )
        assert registry.read_claim(str(tmp_path)) is not None

        readback = sv.close(run_id)

        assert stranger.poll() is None  # the stranger was never signalled
        assert readback.phase is RunPhase.CLOSED
        assert readback.tree.survivors == ()
        closed = registry.get_run(run_id)
        assert closed is not None
        assert closed.phase is RunPhase.CLOSED
        assert registry.read_claim(str(tmp_path)) is None
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(stranger.pid, signal.SIGKILL)
        stranger.wait(timeout=10)


def test_c2_close_of_an_orphaned_row_still_refuses_on_an_identity_mismatch(
    registry: Registry, tmp_path: Path
) -> None:
    """`close_orphaned` is a narrower fence, never an open door: the triple the
    close carries is not the one the ORPHANED row holds, so the transaction
    matches nothing, the claim stays, and the refusal is still typed."""
    sv = _sv(registry, tmp_path)
    run_id, _ = _admit_pending(sv, registry, tmp_path, worktree=str(tmp_path))
    child = FakeSpawnedChild(_spawn_real("true"))
    child.wait(timeout=10)  # reaped: its triple names no live process either
    sv._children[run_id] = child  # type: ignore[assignment]
    row = registry.get_run(run_id)
    assert row is not None
    registry.update_run(
        replace(
            row,
            phase=RunPhase.ORPHANED,
            owner_instance="i-dead",
            # A DIFFERENT triple from the one `close()` will carry in.
            process_identity=ProcessIdentity(
                pid=child.identity.pid + 1, boot_id="boot-elsewhere", start_ticks=1
            ),
        )
    )

    with pytest.raises(RunCloseRejected, match="rejected the transition to closed"):
        sv.close(run_id)

    assert child.stop_calls == []  # a triple that names nothing is not signalled
    still = registry.get_run(run_id)
    assert still is not None
    assert still.phase is RunPhase.ORPHANED
    assert registry.read_claim(str(tmp_path)) is not None


def test_close_stops_the_concurrent_preparation_sibling(
    registry: Registry, tmp_path: Path
) -> None:
    """Read-only codex preparation runs the CLI catalog beside the metadata
    app-server. A close that lands before the leader is published (no row
    identity, no leader child) still owns the catalog child: it dies, and the
    readback names it, instead of a CLOSED row over a live vendor process."""

    class _Reaping(FakeSpawnedChild):
        def stop_then_kill(self, expected: ProcessIdentity) -> None:
            super().stop_then_kill(expected)
            self.process.wait(timeout=5)  # SpawnedChild reaps under its custody

    sv = _sv(registry, tmp_path)
    run_id, _ = _admit_pending(sv, registry, tmp_path, worktree=str(tmp_path))
    proc = _spawn_real("sleep", "30")
    try:
        sibling = _Reaping(proc)
        sv._children[supervisor.preparation_sibling_key(run_id)] = sibling  # type: ignore[assignment]

        readback = sv.close(run_id)

        assert sibling.stop_calls == [sibling.identity]
        assert proc.returncode == -signal.SIGKILL
        assert readback.phase is RunPhase.CLOSED
        assert readback.tree.checked == (sibling.identity,)
        assert readback.tree.survivors == ()
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)


class _SlowInspector:
    """Metadata inspector that outlasts the catalog's own cap."""

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        self.child = None

    def readonly_mcp_policies(self, **_: object) -> dict[str, object]:
        time.sleep(self.seconds)
        return {}


class _OneLiveRun:
    instance_id = "i-fake"

    def get_run(self, run_id: str) -> object:
        return SimpleNamespace(phase=RunPhase.RUNNING)

    def heartbeat_instance(self, now: int) -> bool:
        return True

    def mark_orphans(self, now: int, stale_after_ms: int) -> int:
        return 0

    def remove_instance(self) -> None:
        return None


def test_native_catalog_past_its_cap_is_refused_behind_a_slower_inspector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the caller gives no deadline, the default cap bounds preparation:
    a catalog that runs past that cap is killed and refused on its own even
    while a slower inspector is still reading."""
    monkeypatch.setattr(Supervisor, "NATIVE_CATALOG_CAP_S", 0.3)
    monkeypatch.setattr(
        supervisor.app_server.AppServerTurn,
        "start",
        classmethod(lambda cls, *a, **k: contextlib.nullcontext(_SlowInspector(1.5))),
    )
    vendor = tmp_path / "vendor"
    vendor.write_text(
        f"#!{sys.executable}\nimport time\ntime.sleep(0.8)\nprint('[]', flush=True)\n"
    )
    vendor.chmod(0o700)
    run_dir = tmp_path / "run"
    run_dir.mkdir(mode=0o700)
    sv = _stopped_supervisor(_OneLiveRun(), tmp_path / "runs")  # type: ignore[arg-type]
    launch = SimpleNamespace(
        argv=(str(vendor), "app-server", "--listen", "stdio://"),
        env={"PATH": os.environ.get("PATH", "")},
        plugin_skill_roots={},
        mcp_servers={},
    )
    row = SimpleNamespace(run_id="run-catalog-cap", worktree=str(tmp_path))
    # No caller deadline: the cap is a startup timeout, not a run deadline.
    with pytest.raises(
        supervisor.app_server.AppServerStartupTimeout,
        match=r"native MCP preparation got no answer within 0\.3s",
    ):
        sv._native_readonly_mcp(
            row,  # type: ignore[arg-type]
            SimpleNamespace(root=run_dir),  # type: ignore[arg-type]
            launch,  # type: ignore[arg-type]
            SecretNeedles(),  # type: ignore[arg-type]
            None,
        )
    assert supervisor.preparation_sibling_key(row.run_id) not in sv._children


def test_native_preparation_cap_is_twice_the_observed_maximum() -> None:
    """1491 codex preparations took at most 29 s; the cap is 60 s."""
    assert Supervisor.NATIVE_CATALOG_CAP_S == 60.0


def test_unanswered_plugin_installed_without_caller_deadline_is_a_startup_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real app-server child answers `initialize` and never answers
    `plugin/installed`. The caller gave no deadline, so the cap expiry is a
    startup timeout, never a run deadline."""
    monkeypatch.setattr(Supervisor, "NATIVE_CATALOG_CAP_S", 0.3)
    vendor = tmp_path / "vendor"
    vendor.write_text(
        f"#!{sys.executable}\n"
        "import json, sys, time\n"
        "if 'mcp' in sys.argv:\n"
        "    print('[]', flush=True)\n"
        "    sys.exit(0)\n"
        "def read(): return json.loads(sys.stdin.readline())\n"
        "init = read()\n"
        "print(json.dumps({'id': init['id'], 'result': {'userAgent': 't/1'}}),"
        " flush=True)\n"
        "assert read()['method'] == 'initialized'\n"
        "assert read()['method'] == 'plugin/installed'\n"
        "time.sleep(30)\n"
    )
    vendor.chmod(0o700)
    run_dir = tmp_path / "run"
    run_dir.mkdir(mode=0o700)
    sv = _stopped_supervisor(_OneLiveRun(), tmp_path / "runs")  # type: ignore[arg-type]
    launch = SimpleNamespace(
        argv=(str(vendor), "app-server", "--listen", "stdio://"),
        env={"PATH": os.environ.get("PATH", "")},
        plugin_skill_roots={},
        mcp_servers={},
    )
    row = SimpleNamespace(run_id="run-plugin-installed", worktree=str(tmp_path))
    # The reason names the step that got no answer.
    with pytest.raises(
        supervisor.app_server.AppServerStartupTimeout, match="plugin/installed"
    ):
        sv._native_readonly_mcp(
            row,  # type: ignore[arg-type]
            SimpleNamespace(root=run_dir),  # type: ignore[arg-type]
            launch,  # type: ignore[arg-type]
            SecretNeedles(),  # type: ignore[arg-type]
            None,
        )


def test_native_preparation_past_its_cap_without_caller_deadline_fails_as_startup(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """25 runs in 14 days: no caller deadline, and the stored reason said
    RunDeadlineExceeded. It names the startup timeout and its cap."""
    monkeypatch.setattr(Supervisor, "NATIVE_CATALOG_CAP_S", 0.3)
    sv, _, _ = _drive_catalog_deadline_case(
        registry,
        tmp_path,
        monkeypatch,
        run_id="run-no-deadline",
        catalog_sleep_s=30.0,
        binding_delay_s=0.0,
        timeout_ms=0,
    )
    status = sv.status("run-no-deadline")
    assert status.phase is RunPhase.FAILED
    assert status.failure_reason == (
        "AppServerStartupTimeout (nERR-2003): "
        "native MCP preparation got no answer within 0.3s: "
        "native MCP catalog deadline exceeded"
    )


class _DeadlineSession:
    """Stand-in app-server session that enforces a positive binding timeout."""

    def __init__(self, child: FakeSpawnedChild, delay_s: float = 0.0) -> None:
        self.child = child
        self._delay_s = delay_s
        self.observed_timeout: float | None = None

    def readonly_mcp_policies(
        self, *, timeout: float, **_: object
    ) -> dict[str, dict[str, object]]:
        self.observed_timeout = timeout
        if timeout <= 0:
            raise supervisor.app_server.AppServerDeadlineExceeded(
                "native MCP binding deadline exceeded"
            )
        if self._delay_s > 0:
            if self._delay_s >= timeout:
                time.sleep(max(0.0, timeout))
                raise supervisor.app_server.AppServerDeadlineExceeded(
                    "native MCP binding deadline exceeded"
                )
            time.sleep(self._delay_s)
        return {}

    def run(self, *args: object, **kwargs: object) -> object:
        timeout = kwargs.get("timeout")
        assert isinstance(timeout, float) and timeout > 0
        return supervisor.app_server.AppServerResult(
            "thread-native", "turn-native", "completed", "PONG", {"totalTokens": 5}
        )


def _drive_catalog_deadline_case(
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    run_id: str,
    catalog_sleep_s: float,
    binding_delay_s: float,
    timeout_ms: int,
) -> tuple[Supervisor, Path, float | None]:
    from nspawn import native
    from nspawn.owner_roster import NativeAccount, Provider
    from nspawn.rows import Backend, NativeSessionContext, RunRow
    from nspawn.supervisor import create_run_directory

    sv = _sv(registry, tmp_path)
    context = NativeSessionContext(str(tmp_path))
    row = RunRow(
        run_id=run_id,
        worktree=str(tmp_path),
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
    registry.admit_run(row, False, 1, sv.RUN_CLAIM_LEASE_MS, native_context=context)
    run_dir = create_run_directory(sv._run_root_path, run_id, dir_fd=sv._run_root)
    run_dir.stdout.write_bytes(
        b'{"method":"turn/completed","params":{"threadId":"thread-native",'
        b'"turn":{"id":"turn-native","status":"completed"}}}\n'
    )
    run_dir.stderr.write_bytes(b"")
    pid_file = tmp_path / f"{run_id}-catalog.pid"
    vendor = tmp_path / f"{run_id}-vendor"
    vendor.write_text(
        f"#!/bin/sh\necho $$ > '{pid_file}'\nsleep {catalog_sleep_s}\nprintf '[]\\n'\n"
    )
    vendor.chmod(0o700)
    launch = native.NativeLaunch(
        (str(vendor), "app-server", "--listen", "stdio://"),
        {"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        SecretNeedles.none(),
        context,
        NativeAccount(tmp_path),
    )
    picked = picker.PickerResult(
        "codex", Provider(name="codex", kind="codex", model="m"), "one", "m", None, None
    )
    observed: list[float] = []

    @contextlib.contextmanager
    def _start(*args: object, **kwargs: object) -> object:
        if binding_delay_s > 0:
            time.sleep(binding_delay_s)
        spawned = FakeSpawnedChild(_spawn_real("true"))
        session = _DeadlineSession(spawned, delay_s=binding_delay_s)
        try:
            yield session
        finally:
            if session.observed_timeout is not None:
                observed.append(session.observed_timeout)
            spawned.wait(timeout=5)

    monkeypatch.setattr(
        supervisor.app_server.AppServerTurn, "start", classmethod(_start)
    )
    sv._drive_native_run(row, run_dir, "ping", picked, launch, timeout_ms, {})
    return sv, pid_file, (observed[0] if observed else None)


def test_native_catalog_uses_caller_deadline_beyond_default_cap(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the caller gives a run deadline longer than the default cap,
    both catalog and binding preparation receive the caller's remaining
    deadline instead of failing at the default cap."""
    monkeypatch.setattr(Supervisor, "NATIVE_CATALOG_CAP_S", 0.2)
    sv, pid_file, observed_timeout = _drive_catalog_deadline_case(
        registry,
        tmp_path,
        monkeypatch,
        run_id="run-beyond-cap",
        catalog_sleep_s=0.5,
        binding_delay_s=0.25,
        timeout_ms=60_000,
    )
    status = sv.status("run-beyond-cap")
    assert status.phase is RunPhase.COMPLETED
    assert status.failure_reason is None
    assert observed_timeout is not None and observed_timeout > 50.0
    assert pid_file.exists()
    assert supervisor.preparation_sibling_key("run-beyond-cap") not in sv._children


def test_native_catalog_past_caller_deadline_fails_and_kills_child(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A catalog read that runs past the caller's deadline fails the run with
    RunDeadlineExceeded and kills the catalog child."""
    monkeypatch.setattr(Supervisor, "NATIVE_CATALOG_CAP_S", 30.0)
    sv, pid_file, _ = _drive_catalog_deadline_case(
        registry,
        tmp_path,
        monkeypatch,
        run_id="run-past-deadline",
        catalog_sleep_s=30.0,
        binding_delay_s=0.0,
        timeout_ms=400,
    )
    status = sv.status("run-past-deadline")
    assert status.phase is RunPhase.FAILED
    assert status.failure_reason == "RunDeadlineExceeded"
    assert pid_file.exists()
    pid = int(pid_file.read_text().strip())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert supervisor.preparation_sibling_key("run-past-deadline") not in sv._children


def test_native_binding_rpc_uses_caller_deadline_beyond_rpc_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Binding inspection RPCs (initialize, plugin/installed, plugin/read) use
    the preparation deadline rather than failing at RPC_TIMEOUT_SECONDS."""
    monkeypatch.setattr(supervisor.app_server, "RPC_TIMEOUT_SECONDS", 0.15)
    script = (
        "import json, sys, time\n"
        "def send(v): print(json.dumps(v), flush=True)\n"
        "def read(): return json.loads(sys.stdin.readline())\n"
        "init = read()\n"
        "time.sleep(0.3)\n"
        "send({'id': init['id'], 'result': {'userAgent': 'test/1.0'}})\n"
        "assert read()['method'] == 'initialized'\n"
        "req1 = read()\n"
        "time.sleep(0.3)\n"
        "send({'id': req1['id'], 'result': {'marketplaces': [{'name': 'm1', 'plugins': [{'id': 'p1', 'name': 'plug1', 'enabled': True}]}]}})\n"
        "req2 = read()\n"
        "time.sleep(0.3)\n"
        "send({'id': req2['id'], 'result': {'plugin': {'summary': {'id': 'p1'}, 'mcpServers': ['srv1']}}})\n"
    )
    output = tmp_path / "binding"
    output.mkdir()
    with supervisor.app_server.AppServerTurn.start(
        [sys.executable, "-u", "-c", script],
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        run_dir=output,
        needles=SecretNeedles.none(),
    ) as session:
        policies = session.readonly_mcp_policies(timeout=5.0)
    assert policies == {"p1": {"mcp_servers": {"srv1": {"enabled": False}}}}


@pytest.mark.parametrize(
    ("writer_sleep_s", "catalog_sleep_s", "timeout_ms", "spawned"),
    [
        (0.35, 0.0, 200, False),
        (0.3, 0.45, 600, True),
    ],
)
def test_native_catalog_uses_remaining_deadline_after_prior_setup(
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    writer_sleep_s: float,
    catalog_sleep_s: float,
    timeout_ms: int,
    spawned: bool,
) -> None:
    """Catalog preparation receives the remaining deadline after prior setup,
    refusing before spawn when already exhausted and killing the child when the
    remaining budget expires."""
    from nspawn import native
    from nspawn.owner_roster import NativeAccount, Provider
    from nspawn.rows import Backend, NativeSessionContext, RunRow
    from nspawn.supervisor import create_run_directory

    sv = _sv(registry, tmp_path)
    run_id = "run-remaining-deadline"
    context = NativeSessionContext(str(tmp_path))
    row = RunRow(
        run_id=run_id,
        worktree=str(tmp_path),
        phase=RunPhase.PENDING,
        sandbox="workspace-write",
        model="m",
        backend=Backend.CODEX,
        owner_instance=registry.instance_id,
        created_at=1,
        updated_at=1,
        executed_provider="codex",
        executed_account="one",
    )
    registry.admit_run(row, True, 1, sv.RUN_CLAIM_LEASE_MS, native_context=context)
    run_dir = create_run_directory(sv._run_root_path, run_id, dir_fd=sv._run_root)
    pid_file = tmp_path / "catalog.pid"
    done_file = tmp_path / "catalog.done"
    vendor = tmp_path / "vendor"
    vendor.write_text(
        f"#!/bin/sh\n"
        f"echo $$ > '{pid_file}'\n"
        f"sleep {catalog_sleep_s}\n"
        f": > '{done_file}'\n"
        f"printf '[]\\n'\n"
    )
    vendor.chmod(0o700)
    launch = native.NativeLaunch(
        (str(vendor), "app-server", "--listen", "stdio://"),
        {"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        SecretNeedles.none(),
        context,
        NativeAccount(tmp_path),
    )
    picked = picker.PickerResult(
        "codex", Provider(name="codex", kind="codex", model="m"), "one", "m", None, None
    )

    @contextlib.contextmanager
    def _start(*args: object, **kwargs: object) -> object:
        child_proc = FakeSpawnedChild(_spawn_real("true"))
        try:
            yield _DeadlineSession(child_proc)
        finally:
            child_proc.wait(timeout=5)

    def _slow_writer(self: Supervisor, rid: str) -> None:
        time.sleep(writer_sleep_s)

    monkeypatch.setattr(
        supervisor.app_server.AppServerTurn, "start", classmethod(_start)
    )
    monkeypatch.setattr(Supervisor, "_open_writer", _slow_writer)
    monkeypatch.setattr(Supervisor, "_close_writer", lambda self, r, w: None)
    sv._drive_native_run(
        row, run_dir, "ping", picked, launch, timeout_ms, {}, native_write=True
    )
    status = sv.status(run_id)
    assert status.phase is RunPhase.FAILED
    assert status.failure_reason == "RunDeadlineExceeded"
    assert not done_file.exists()
    assert pid_file.exists() is spawned
    if spawned:
        pid = int(pid_file.read_text().strip())
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
