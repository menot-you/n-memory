"""A native turn whose answer exceeds the durable cap still reaches a terminal phase.

Measured 2026-10-09 (run e6a57ab2, codex): the turn completed with a 27,928-byte
answer, `RunRow` refused a `final_text` over 16 KiB, the ValueError killed the
driver's worker thread, and the run sat in `running` until its owner's heartbeat
lapsed and `mark_orphans` called it orphaned. The answer existed; nSPAWN lost it.
"""

from __future__ import annotations

import contextlib
import errno
import os
import threading
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, Self

import pytest
from supervisor_support import (
    FakeSpawnedChild,
    _drain_worker_threads,
    _spawn_real,
    _stop_every_reaper,
    _sv,
    _wire_l8_and_l3,
    registry,
    roster_home,
)

from nspawn import app_server, native, picker, supervisor, terminal
from nspawn.limits import MAX_DURABLE_OBSERVATION_BYTES
from nspawn.owner_roster import NativeAccount, Provider
from nspawn.redact import REDACTION_MARKER, SecretNeedles
from nspawn.registry import STORE_ERRORS, Registry
from nspawn.rows import Backend, NativeSessionContext, RunPhase, RunRow
from nspawn.supervisor import FINAL_TEXT_FILE, Supervisor, create_run_directory

__all__ = ["_drain_worker_threads", "_stop_every_reaper", "registry", "roster_home"]

#: 28,000 UTF-8 bytes, a multibyte codepoint at every cut.
ANSWER = "é" * 14_000


class _DoubleSession:
    def __init__(
        self, child: FakeSpawnedChild, result: app_server.AppServerResult
    ) -> None:
        self.child = child
        self._result = result

    def run(self, *args: object, **kwargs: object) -> app_server.AppServerResult:
        return self._result


def _drive_native(
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    quota_warning: str | None = None,
    announces: bool = False,
    answer: str = ANSWER,
    plant: Callable[[Path], None] | None = None,
    needles: SecretNeedles | None = None,
) -> tuple[Supervisor, str]:
    """One completed Codex turn answering `ANSWER`, driven to its terminal."""
    sv = _sv(registry, tmp_path)
    sv.announces_terminal = announces
    context = NativeSessionContext(str(tmp_path))
    row = RunRow(
        run_id="run-long",
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
    run_dir = create_run_directory(sv._run_root_path, row.run_id, dir_fd=sv._run_root)
    run_dir.stdout.write_bytes(b'{"method":"turn/completed"}\n')
    if plant is not None:
        plant(run_dir.root)
    session = _DoubleSession(
        FakeSpawnedChild(_spawn_real("true")),
        app_server.AppServerResult(
            "thread-long",
            "turn-long",
            "completed",
            answer,
            {"totalTokens": 9},
            quota_warning=quota_warning,
        ),
    )
    monkeypatch.setattr(Supervisor, "_native_readonly_mcp", lambda *a, **k: ({}, {}))
    monkeypatch.setattr(
        app_server.AppServerTurn,
        "start",
        classmethod(lambda cls, *a, **k: contextlib.nullcontext(session)),
    )
    launch = native.NativeLaunch(
        ("/never-start/vendor",),
        {},
        SecretNeedles.none() if needles is None else needles,
        context,
        NativeAccount(tmp_path),
    )
    picked = picker.PickerResult(
        "codex", Provider(name="codex", kind="codex", model="m"), "one", "m", "high"
    )
    sv._drive_native_run(row, run_dir, "ping", picked, launch, 0, {})
    return sv, row.run_id


def test_an_oversized_native_answer_completes_bounded_instead_of_orphaning(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sv, run_id = _drive_native(registry, tmp_path, monkeypatch)
    stored = registry.get_run(run_id)
    assert stored is not None
    assert stored.phase is RunPhase.COMPLETED
    assert stored.final_text is not None
    kept = stored.final_text.encode("utf-8")
    assert 0 < len(kept) <= MAX_DURABLE_OBSERVATION_BYTES
    assert ANSWER.startswith(stored.final_text)
    # Not cut silently: the warning says so, and the result answers it whole.
    assert stored.warning is not None
    assert "28000 bytes" in stored.warning
    assert "final_text" in stored.warning
    assert sv.result(run_id).final_text == ANSWER


def test_a_native_failure_detail_passes_the_live_redactor(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run's `NativeRedactor` holds no needles of its own, so `len()` is 0
    and `needles or none()` once replaced it with a redactor that removes nothing."""
    secret = "native-secret-0123456789abcdef"

    def refused(*args: object, **kwargs: object) -> app_server.AppServerResult:
        raise app_server.AppServerRefused(f"native thread/start {secret} refused")

    monkeypatch.setattr(_DoubleSession, "run", refused)
    _, run_id = _drive_native(
        registry, tmp_path, monkeypatch, needles=SecretNeedles((secret,))
    )
    stored = registry.get_run(run_id)
    assert stored is not None
    assert stored.phase is RunPhase.FAILED
    assert stored.blocked_reason is not None
    assert secret not in stored.blocked_reason
    assert REDACTION_MARKER in stored.blocked_reason


def test_a_full_warning_still_names_the_cut(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review round 1: a warning already at the cap swallowed the notice."""
    _, run_id = _drive_native(
        registry,
        tmp_path,
        monkeypatch,
        quota_warning="w" * MAX_DURABLE_OBSERVATION_BYTES,
    )
    stored = registry.get_run(run_id)
    assert stored is not None and stored.warning is not None
    assert "final answer of 28000 bytes" in stored.warning
    assert len(stored.warning.encode("utf-8")) <= MAX_DURABLE_OBSERVATION_BYTES


def test_a_failed_whole_answer_write_leaves_no_partial_file(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review round 1: ENOSPC mid-write left a partial `final_text` that the
    result then preferred to the bounded row, or could not even decode."""
    real_fdopen = os.fdopen

    def half_then_full(fd: int, *args: Any, **kwargs: Any) -> Any:
        stream = real_fdopen(fd, *args, **kwargs)

        real_write = stream.write

        def write(data: bytes) -> int:
            real_write(data[:3])  # inside a codepoint
            stream.flush()
            raise OSError(errno.ENOSPC, "No space left on device")

        stream.write = write  # type: ignore[method-assign]
        return stream

    monkeypatch.setattr(supervisor.os, "fdopen", half_then_full)
    sv, run_id = _drive_native(registry, tmp_path, monkeypatch)
    stored = registry.get_run(run_id)
    assert stored is not None and stored.warning is not None
    assert "the whole was not kept (OSError)" in stored.warning
    assert not (sv._run_root_path / run_id / FINAL_TEXT_FILE).exists()
    assert [p.name for p in (sv._run_root_path / run_id).iterdir()].count(
        FINAL_TEXT_FILE
    ) == 0
    assert sv.result(run_id).final_text == stored.final_text


def _unexpected_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        supervisor.picker,
        "pick_execution",
        lambda *a, **k: picker.PickerResult(
            requested_provider=None,
            executed_provider=Provider(
                name="acme", bin=Path("/usr/bin/claude"), model="opus", models=["opus"]
            ),
            executed_account="acct-1",
            model="opus",
            effort=None,
            warning=None,
        ),
    )
    _wire_l8_and_l3(monkeypatch, lambda *a, **k: FakeSpawnedChild(_spawn_real("true")))

    def unexpected(*args: object, **kwargs: object) -> object:
        raise LookupError("nothing in the worker expects this")

    monkeypatch.setattr(supervisor.terminal, "classify_terminal_run", unexpected)


def test_a_worker_that_dies_ends_its_run_failed_with_a_named_reason(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The e6a57ab2 shape on the whole route: an exception no worker clause
    expects must end the run FAILED and name it, not leave it `running` for
    `mark_orphans` to call orphaned once the heartbeat lapses."""
    _unexpected_worker(monkeypatch)
    sv = _sv(registry, tmp_path)
    accepted = sv.execute("hi", str(tmp_path), read_only=True)
    status = sv.status(accepted.run_id, timeout_ms=5000)
    assert status.phase is RunPhase.FAILED
    assert status.failure_reason == "WorkerDied: LookupError"


def test_a_dead_workers_failure_survives_a_store_refusal(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review round 1: the FAILED write itself refused by the store escaped the
    guard, the row stayed `running`, and nothing was left for the driver's
    terminal retry. It must wait in `_unpublished` and land on retry."""
    _unexpected_worker(monkeypatch)
    sv = _sv(registry, tmp_path)
    real_update = registry.update_run
    refusals = [STORE_ERRORS[0]("database is locked")]

    def refused_once(row: RunRow, **kwargs: Any) -> Any:
        if row.phase is RunPhase.FAILED and refusals:
            raise refusals.pop()
        return real_update(row, **kwargs)

    monkeypatch.setattr(registry, "update_run", refused_once)
    accepted = sv.execute("hi", str(tmp_path), read_only=True)
    _drain_worker_threads_of(sv)
    assert accepted.run_id in sv._unpublished
    monkeypatch.setattr(registry, "reconnect", lambda: None)
    assert sv.publish_pending() is True
    status = sv.status(accepted.run_id)
    assert status.phase is RunPhase.FAILED
    assert status.failure_reason == "WorkerDied: LookupError"


def _drain_worker_threads_of(sv: Supervisor) -> None:
    import threading

    for thread in threading.enumerate():
        if "_guarded_worker" in thread.name:
            thread.join(timeout=10)


def test_a_cleanup_failure_after_completion_never_publishes_failed(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review round 2: the worker raised in cleanup after COMPLETED was
    published, the guard's read of the row was refused, and the guard published
    a synthetic FAILED over the completed run with zero tokens."""
    sv, run_id = _drive_native(registry, tmp_path, monkeypatch)
    before = registry.get_run(run_id)
    assert before is not None and before.phase is RunPhase.COMPLETED
    assert before.tokens
    real_get = registry.get_run
    refusals = [STORE_ERRORS[0]("database is locked")]

    def refused_once(rid: str) -> RunRow | None:
        if refusals:
            raise refusals.pop()
        return real_get(rid)

    monkeypatch.setattr(registry, "get_run", refused_once)

    def cleanup() -> None:
        raise PermissionError("system prompt file could not be removed")

    sv._guarded_worker(run_id, cleanup)
    sv.publish_pending()
    after = registry.get_run(run_id)
    assert after is not None
    assert after.phase is RunPhase.COMPLETED
    assert after.tokens == before.tokens
    assert after.blocked_reason is None


def test_a_refused_terminal_waiting_for_retry_is_not_replaced_by_worker_died(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review round 2: the real terminal write was refused and waited in
    `_unpublished`; a later store error killed the worker, the guard published
    WorkerDied, was refused too, and replaced the waiting classification. The
    retry then landed WorkerDied and the answer was gone."""
    real_update = registry.update_run
    refusals = [STORE_ERRORS[0]("database is locked")]

    def refused_once(row: RunRow, **kwargs: Any) -> Any:
        if row.phase is RunPhase.COMPLETED and refusals:
            raise refusals.pop()
        return real_update(row, **kwargs)

    monkeypatch.setattr(registry, "update_run", refused_once)
    sv, run_id = _drive_native(registry, tmp_path, monkeypatch)
    assert run_id in sv._unpublished
    monkeypatch.setattr(
        registry,
        "get_run",
        lambda rid: (_ for _ in ()).throw(STORE_ERRORS[0]("database is locked")),
    )

    def after_the_refusal() -> None:
        raise STORE_ERRORS[0]("database is locked")

    sv._guarded_worker(run_id, after_the_refusal)
    monkeypatch.undo()
    monkeypatch.setattr(registry, "reconnect", lambda: None)
    assert sv.publish_pending() is True
    stored = registry.get_run(run_id)
    assert stored is not None
    assert stored.phase is RunPhase.COMPLETED
    assert stored.blocked_reason is None
    assert sv.result(run_id).final_text == ANSWER


def test_the_whole_answer_is_on_disk_before_it_is_named(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review round 2: without fsync the rename can reach disk before the
    data, and after a host crash `run_result` prefers an empty file. The
    partial file is synced holding the whole answer, then renamed, then its
    directory is synced so the name survives too."""
    events: list[tuple[str, str]] = []
    real_fsync, real_replace = os.fsync, os.replace

    def fsync(fd: int) -> None:
        target = os.readlink(f"/proc/self/fd/{fd}")
        held = (
            Path(target).read_text(encoding="utf-8") if Path(target).is_file() else ""
        )
        events.append(("fsync", target if held != ANSWER else f"{target}=ANSWER"))
        real_fsync(fd)

    def replace_(src: Any, dst: Any, **kwargs: Any) -> None:
        events.append(("replace", f"{src}->{dst}"))
        real_replace(src, dst, **kwargs)

    monkeypatch.setattr(supervisor.os, "fsync", fsync)
    monkeypatch.setattr(supervisor.os, "replace", replace_)
    sv, run_id = _drive_native(registry, tmp_path, monkeypatch)
    run_dir = sv._run_root_path / run_id
    partial = run_dir / f"{FINAL_TEXT_FILE}.partial"
    named = run_dir / FINAL_TEXT_FILE
    assert events == [
        ("fsync", f"{partial}=ANSWER"),
        ("replace", f"{partial.name}->{named.name}"),
        ("fsync", str(run_dir)),
    ]


def _late(sv: Supervisor, run_id: str) -> str:
    """The run's late-write diagnostics, or "" when none was kept."""
    path = sv._run_root_path / run_id / "late_writes"
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def _refuse_completed_once(registry: Registry, monkeypatch: pytest.MonkeyPatch) -> None:
    real_update = registry.update_run
    refusals = [STORE_ERRORS[0]("database is locked")]

    def refused_once(row: RunRow, **kwargs: Any) -> Any:
        if row.phase is RunPhase.COMPLETED and refusals:
            raise refusals.pop()
        return real_update(row, **kwargs)

    monkeypatch.setattr(registry, "update_run", refused_once)


def test_a_failure_after_a_refused_completion_never_replaces_it(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review round 3: COMPLETED was refused and queued; a cleanup failure then
    wrote FAILED over the still-RUNNING row, and the retry hit the ended-run
    fence: the completion, its answer and its tokens were lost. While a
    terminal is queued no other write is applied; the late failure is a
    diagnostic line, and the completion lands once and is announced once."""
    _refuse_completed_once(registry, monkeypatch)
    sv, run_id = _drive_native(registry, tmp_path, monkeypatch)
    assert run_id in sv._unpublished
    announced: list[str] = []
    monkeypatch.setattr(sv, "_announce", announced.append)
    sv._fail_run(run_id, supervisor.RunDirectoryError("prompt file left"))
    monkeypatch.setattr(registry, "reconnect", lambda: None)
    assert sv.publish_pending() is True
    stored = registry.get_run(run_id)
    assert stored is not None
    assert stored.phase is RunPhase.COMPLETED
    assert stored.tokens
    assert stored.blocked_reason is None
    assert sv.result(run_id).final_text == ANSWER
    assert announced == [run_id]
    assert "RunDirectoryError" not in (stored.warning or "")
    assert "a failed write was not applied; reason='RunDirectoryError'" in _late(
        sv, run_id
    )


@pytest.mark.parametrize(
    "answer", [ANSWER, "\r\n".join([ANSWER[:9000]] * 2)], ids=["plain", "crlf"]
)
def test_a_retry_keeps_the_whole_answer_an_earlier_attempt_kept(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answer: str
) -> None:
    """Review round 4: the retry failed to rewrite the file an earlier attempt
    had kept whole, and its cleanup deleted that file; round 5: a text
    comparison missed a kept answer holding `\\r`. The kept bytes are reused."""
    sv, run_id = _drive_native(registry, tmp_path, monkeypatch)
    assert "answers it whole" in sv._keep_whole_answer(run_id, answer)
    kept = sv._run_root_path / run_id / FINAL_TEXT_FILE

    def full(*args: Any, **kwargs: Any) -> Any:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(supervisor.os, "fdopen", full)
    assert "answers it whole" in sv._keep_whole_answer(run_id, answer)
    assert kept.read_bytes() == answer.encode("utf-8")
    assert sv.result(run_id).final_text == answer


def test_a_kept_file_the_check_misses_is_not_removed_by_a_failed_rewrite(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review round 5: only this call's own partial is ever removed."""
    sv, run_id = _drive_native(registry, tmp_path, monkeypatch)
    kept = sv._run_root_path / run_id / FINAL_TEXT_FILE
    real_open = supervisor._open_regular

    def unreadable(directory: int, name: str, flags: int) -> int:
        if name == FINAL_TEXT_FILE and not flags & os.O_WRONLY:
            raise OSError(errno.EIO, "I/O error")
        return real_open(directory, name, flags)

    def full(*args: Any, **kwargs: Any) -> Any:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(supervisor, "_open_regular", unreadable)
    monkeypatch.setattr(supervisor.os, "fdopen", full)
    assert "the whole was not kept" in sv._keep_whole_answer(run_id, ANSWER)
    monkeypatch.undo()
    assert kept.read_bytes() == ANSWER.encode("utf-8")


def test_a_partial_that_cannot_be_removed_is_named_and_never_blocks(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review round 5: a cleanup that raised escaped `_keep_whole_answer` and
    queued the terminal. It is named in the note instead."""

    def full(*args: Any, **kwargs: Any) -> Any:
        raise OSError(errno.ENOSPC, "No space left on device")

    real_unlink = os.unlink

    def refused(path: Any, *args: Any, **kwargs: Any) -> None:
        if Path(path).name == f"{FINAL_TEXT_FILE}.partial":
            raise PermissionError(errno.EACCES, "refused")
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(supervisor.os, "fdopen", full)
    monkeypatch.setattr(supervisor.os, "unlink", refused)
    _, run_id = _drive_native(registry, tmp_path, monkeypatch)
    stored = registry.get_run(run_id)
    assert stored is not None and stored.phase is RunPhase.COMPLETED
    assert "final_text.partial left behind (PermissionError)" in (stored.warning or "")


def test_an_unsynced_directory_is_named_on_every_attempt(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review round 5: the retry found the kept answer and returned the plain
    note, so the unsynced directory vanished from the row. Every attempt
    recomputes it from disk."""
    real_fsync = os.fsync

    def fsync(fd: int) -> None:
        if Path(os.readlink(f"/proc/self/fd/{fd}")).is_dir():
            raise OSError(errno.EIO, "I/O error")
        real_fsync(fd)

    monkeypatch.setattr(supervisor.os, "fsync", fsync)
    _refuse_completed_once(registry, monkeypatch)
    sv, run_id = _drive_native(registry, tmp_path, monkeypatch)
    assert run_id in sv._unpublished
    monkeypatch.setattr(registry, "reconnect", lambda: None)
    assert sv.publish_pending() is True
    stored = registry.get_run(run_id)
    assert stored is not None and stored.phase is RunPhase.COMPLETED
    assert "directory was not synced (OSError)" in (stored.warning or "")
    assert sv.result(run_id).final_text == ANSWER


def test_a_fenced_retry_is_logged(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review round 4: a close landing before the retry fenced it, and the
    queued terminal was dropped without a line. A close wins; it is logged."""
    _refuse_completed_once(registry, monkeypatch)
    sv, run_id = _drive_native(registry, tmp_path, monkeypatch)
    row = registry.get_run(run_id)
    assert row is not None
    registry.update_run(replace(row, phase=RunPhase.CLOSED))
    monkeypatch.setattr(registry, "reconnect", lambda: None)
    sv.publish_pending()
    assert f"run {run_id} is closed: a completed write was not applied" in _late(
        sv, run_id
    )


def test_a_close_at_write_time_is_logged(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review round 5: a close committed between the read and the write raised
    `ClosedRun`, and the dropped terminal left no line; round 6: the line named
    the phase read before the close. It names what the store said."""
    from nspawn.registry import ClosedRun

    def closed(row: RunRow, **kwargs: Any) -> None:
        raise ClosedRun("closed between the read and the write")

    monkeypatch.setattr(registry, "update_run", closed)
    sv, run_id = _drive_native(registry, tmp_path, monkeypatch)
    late = _late(sv, run_id)
    assert (
        "ended at write time (closed between the read and the write): "
        "a completed write was not applied"
    ) in late
    assert "is pending" not in late and "is running" not in late


def test_a_diagnostic_that_cannot_be_written_changes_nothing(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review round 5: a late note that failed to write failed the caller. A
    diagnostic is best effort."""
    _refuse_completed_once(registry, monkeypatch)
    sv, run_id = _drive_native(registry, tmp_path, monkeypatch)
    (sv._run_root_path / run_id / "late_writes").mkdir()
    sv._fail_run(run_id, supervisor.RunDirectoryError("prompt file left"))
    monkeypatch.setattr(registry, "reconnect", lambda: None)
    assert sv.publish_pending() is True
    stored = registry.get_run(run_id)
    assert stored is not None and stored.phase is RunPhase.COMPLETED


def test_a_late_note_never_holds_the_lock_on_a_stalled_stderr(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review round 6: `_launching` holds the reentrant lock around its phase
    write, so a late note printed to a stalled stderr pipe (the MCP server's
    client) kept every other writer and close out. Late notes never touch
    stderr; they land in the run's own `late_writes` file."""
    sv, run_id = _drive_native(registry, tmp_path, monkeypatch)
    stalled = threading.Event()
    release = threading.Event()

    class Stalled:
        def write(self, text: str) -> int:
            stalled.set()
            release.wait(30)
            return len(text)

        def flush(self) -> None:
            pass

    monkeypatch.setattr(supervisor.sys, "stderr", Stalled())

    def launching() -> None:
        with sv._lock:  # as `_launching` holds it around the RUNNING write
            sv._update_phase(run_id, RunPhase.RUNNING)

    writer = threading.Thread(target=launching, daemon=True)
    writer.start()
    try:
        writer.join(10)
        acquired = sv._lock.acquire(timeout=5)
        if acquired:
            sv._lock.release()
        assert acquired and not stalled.is_set()
    finally:
        release.set()
        writer.join(10)
    assert f"run {run_id} is completed: a running write was not applied" in _late(
        sv, run_id
    )


def test_a_refused_terminal_survives_a_broken_stderr(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review round 6 (inherited): the store-refusal log ran before the terminal
    was queued and did not swallow its own failure, so a broken stderr dropped
    the terminal and let a later FAILED replace the completion."""

    class Broken:
        def write(self, text: str) -> int:
            raise BrokenPipeError(errno.EPIPE, "Broken pipe")

        def flush(self) -> None:
            raise BrokenPipeError(errno.EPIPE, "Broken pipe")

    _refuse_completed_once(registry, monkeypatch)
    monkeypatch.setattr(supervisor.sys, "stderr", Broken())
    sv, run_id = _drive_native(registry, tmp_path, monkeypatch)
    assert run_id in sv._unpublished
    monkeypatch.setattr(registry, "reconnect", lambda: None)
    assert sv.publish_pending() is True
    stored = registry.get_run(run_id)
    assert stored is not None and stored.phase is RunPhase.COMPLETED


def test_a_fifo_at_the_late_sink_never_blocks_the_lock(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review round 7: an append-mode open of a FIFO with no reader blocks, and
    `_launching` holds the lock around it. The sink opens non-blocking and
    writes only a regular file."""
    sv, run_id = _drive_native(registry, tmp_path, monkeypatch)
    sink = sv._run_root_path / run_id / "late_writes"
    os.mkfifo(sink, 0o600)

    def launching() -> None:
        with sv._lock:
            sv._update_phase(run_id, RunPhase.RUNNING)

    writer = threading.Thread(target=launching, daemon=True)
    writer.start()
    try:
        writer.join(10)
        acquired = sv._lock.acquire(timeout=5)
        if acquired:
            sv._lock.release()
        assert acquired and not writer.is_alive()
    finally:
        with contextlib.suppress(OSError):
            os.close(os.open(sink, os.O_RDONLY | os.O_NONBLOCK))
        writer.join(10)


def test_a_run_directory_swapped_for_a_link_is_not_followed(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review round 7: `O_NOFOLLOW` guarded only the file, so a run directory
    replaced by a symlink sent the late note into the link's target."""
    sv, run_id = _drive_native(registry, tmp_path, monkeypatch)
    run_dir = sv._run_root_path / run_id
    elsewhere = tmp_path / "elsewhere"
    run_dir.rename(elsewhere)
    run_dir.symlink_to(elsewhere, target_is_directory=True)
    sv._update_phase(run_id, RunPhase.RUNNING)
    assert not (elsewhere / "late_writes").exists()


class _BrokenStderr:
    def write(self, text: str) -> int:
        raise BrokenPipeError(errno.EPIPE, "Broken pipe")

    def flush(self) -> None:
        raise BrokenPipeError(errno.EPIPE, "Broken pipe")


def test_a_dead_worker_fails_named_on_a_broken_stderr(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review round 7: `_log_tick_failure` raised on a broken stderr before
    `_guarded_worker` wrote WorkerDied, and the run stayed live."""
    monkeypatch.setattr(supervisor.sys, "stderr", _BrokenStderr())
    _unexpected_worker(monkeypatch)
    sv = _sv(registry, tmp_path)
    accepted = sv.execute("hi", str(tmp_path), read_only=True)
    status = sv.status(accepted.run_id, timeout_ms=5000)
    assert status.phase is RunPhase.FAILED
    assert status.failure_reason == "WorkerDied: LookupError"


def test_a_committed_terminal_is_never_requeued_on_a_broken_stderr(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review round 7: a committed terminal whose push the store refused logged
    to a broken stderr; that OSError reached `_publish_terminal`'s refusal
    branch, which queued the committed terminal again as if its write failed."""
    pushes: list[object] = []

    def refused(*args: object, **kwargs: object) -> None:
        pushes.append(args)
        raise STORE_ERRORS[0]("database is locked")

    monkeypatch.setattr(supervisor.sys, "stderr", _BrokenStderr())
    monkeypatch.setattr(supervisor.notify, "push_ready", refused)
    sv, run_id = _drive_native(registry, tmp_path, monkeypatch, announces=True)
    assert pushes, "the refused push was never reached"
    stored = registry.get_run(run_id)
    assert stored is not None and stored.phase is RunPhase.COMPLETED
    assert run_id not in sv._unpublished


def test_a_fifo_with_a_reader_at_the_late_sink_is_never_written(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review round 7: only a regular file takes a late note; a FIFO someone is
    reading opens, is not written, and is closed."""
    sv, run_id = _drive_native(registry, tmp_path, monkeypatch)
    sink = sv._run_root_path / run_id / "late_writes"
    os.mkfifo(sink, 0o600)
    reader = os.open(sink, os.O_RDONLY | os.O_NONBLOCK)
    try:
        sv._update_phase(run_id, RunPhase.RUNNING)
        # EOF, not EAGAIN: the refused write end was closed, not leaked.
        assert os.read(reader, 4096) == b""
    finally:
        os.close(reader)


def _refused_terminal_in_thread(sv: Supervisor, run_id: str) -> threading.Thread:
    def refused(*args: object, **kwargs: object) -> bool:
        raise STORE_ERRORS[0]("database is locked")

    sv._update_phase = refused  # type: ignore[method-assign]
    classification = terminal.TerminalClassification(
        phase=RunPhase.COMPLETED,
        final_text="done",
        warning=None,
        marker=None,
        provider_wall=None,
        account_wall=None,
        tokens=5,
    )
    writer = threading.Thread(
        target=sv._publish_terminal, args=(run_id, classification), daemon=True
    )
    writer.start()
    writer.join(10)
    return writer


@pytest.mark.parametrize("planted", ["fifo", "link"])
def test_a_planted_terminal_file_never_blocks_or_redirects_the_queue(
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    planted: str,
) -> None:
    """Review round 7: `terminal.json` was written with a plain `write_text`
    before the terminal was queued: a FIFO there hung the worker, and a link
    there truncated its target. The terminal is queued first, and the file is
    written only as a regular file reached without a link."""
    sv, run_id = _drive_native(registry, tmp_path, monkeypatch)
    queued_before_the_file: list[bool] = []
    real_open = supervisor._open_regular

    def recording(directory: int, name: str, flags: int) -> int:
        if name == supervisor.TERMINAL_FILE:
            queued_before_the_file.append(run_id in sv._unpublished)
        return real_open(directory, name, flags)

    monkeypatch.setattr(supervisor, "_open_regular", recording)
    target = sv._run_root_path / run_id / "terminal.json"
    outside = tmp_path / "outside"
    outside.write_text("keep", encoding="utf-8")
    if planted == "fifo":
        os.mkfifo(target, 0o600)
    else:
        target.symlink_to(outside)
    writer = _refused_terminal_in_thread(sv, run_id)
    try:
        assert not writer.is_alive()
        assert run_id in sv._unpublished
        assert queued_before_the_file == [True]
        assert outside.read_text(encoding="utf-8") == "keep"
    finally:
        if planted == "fifo":
            with contextlib.suppress(OSError):
                os.close(os.open(target, os.O_RDONLY | os.O_NONBLOCK))
        writer.join(10)


@pytest.mark.parametrize(
    ("name", "planted"),
    [
        (FINAL_TEXT_FILE, "fifo"),
        (FINAL_TEXT_FILE, "symlink"),
        (f"{FINAL_TEXT_FILE}.partial", "fifo"),
        (f"{FINAL_TEXT_FILE}.partial", "symlink"),
    ],
)
def test_a_planted_answer_file_never_blocks_or_stands_in_for_the_answer(
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    planted: str,
) -> None:
    """Review round 9: a FIFO at `final_text` blocked the kept-answer check and
    one at its partial blocked the write, each before the terminal write, and a
    symlink at `final_text` was taken for the kept answer. The answer lands
    whole as a regular file or is named as not kept; nothing outside changes."""
    sv, run_id = _drive_native(registry, tmp_path, monkeypatch)
    run_dir = sv._run_root_path / run_id
    (run_dir / FINAL_TEXT_FILE).unlink()
    target = run_dir / name
    outside = tmp_path / "outside"
    outside.write_text(ANSWER, encoding="utf-8")
    if planted == "fifo":
        os.mkfifo(target, 0o600)
    else:
        target.symlink_to(outside)
    notes: list[str] = []
    writer = threading.Thread(
        target=lambda: notes.append(sv._keep_whole_answer(run_id, ANSWER)),
        daemon=True,
    )
    writer.start()
    writer.join(10)
    try:
        assert not writer.is_alive() and notes
        assert outside.read_text(encoding="utf-8") == ANSWER
        kept = run_dir / FINAL_TEXT_FILE
        if "answers it whole" in notes[0]:
            assert not kept.is_symlink()
            assert kept.read_bytes() == ANSWER.encode("utf-8")
        else:
            assert "the whole was not kept" in notes[0]
    finally:
        if planted == "fifo":
            with contextlib.suppress(OSError):
                os.close(os.open(target, os.O_RDWR | os.O_NONBLOCK))
        writer.join(10)


def test_an_oversized_kept_file_is_read_bounded(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review round 9: the kept-answer check read `final_text` whole, so a
    planted sparse file raised MemoryError, which no branch catches, and a
    completed run became WorkerDied. It reads at most one byte past the answer.
    (Measured by the read's size, not by allocating: this host overcommits.)"""
    sv, run_id = _drive_native(registry, tmp_path, monkeypatch)
    sizes: list[int] = []

    class Guarded:
        def __init__(self, fd: int, mode: str) -> None:
            self._stream = os.fdopen(fd, mode)

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *exc: object) -> None:
            self._stream.close()

        def read(self, size: int = -1) -> bytes:
            sizes.append(size)
            if size < 0:
                raise MemoryError("a sparse file of a terabyte")
            return self._stream.read(size)  # type: ignore[no-any-return]

    monkeypatch.setattr(supervisor, "open", Guarded, raising=False)
    assert "answers it whole" in sv._keep_whole_answer(run_id, ANSWER)
    assert sizes == [len(ANSWER.encode("utf-8")) + 1]


SMALL = "the row's own answer"


def _plant(kind: str, outside: Path) -> Callable[[Path], None]:
    def plant(run_dir: Path) -> None:
        target = run_dir / FINAL_TEXT_FILE
        if kind == "fifo":
            os.mkfifo(target, 0o600)
        elif kind == "symlink":
            target.symlink_to(outside)
        elif kind == "directory":
            target.mkdir()
        else:
            target.write_bytes(b"\xff\xfe not utf-8")

    return plant


def _pushed_bodies(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """`notify.push_ready` as a double that builds the real notice body, so
    `run_result` runs on the announce path after the commit."""
    bodies: list[str] = []

    def push(registry_: Registry, run_ids: Any, **kwargs: Any) -> int:
        (run_id,) = run_ids
        row = registry_.get_run(run_id)
        bodies.append(
            supervisor.notify.notice_body(
                run_id, "completed", row=row, run_dir=kwargs["run_root"] / run_id
            )
        )
        return 1

    monkeypatch.setattr(supervisor.notify, "push_ready", push)
    return bodies


@pytest.mark.parametrize("kind", ["fifo", "symlink", "directory", "non-utf-8"])
def test_a_planted_final_text_never_reaches_the_announce_or_the_result(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """Review round 10: `run_result` read `final_text` by path whenever a
    native stream held no result. On the announce path, after the commit, a
    FIFO there hung the worker and lost the notice, a symlink served outside
    bytes as the answer, a directory raised into `_publish_terminal`'s
    refusal branch, which re-queued the committed terminal, and non-UTF-8
    bytes raised UnicodeDecodeError out of the drive. Round 12: a row answer
    shorter than the cut cannot be a prefix, so nothing in the run directory
    is read for it; the row's answer is the notice and the result."""
    outside = tmp_path / "outside"
    outside.write_text("OUTSIDE BYTES", encoding="utf-8")
    bodies = _pushed_bodies(monkeypatch)
    holder: list[Supervisor] = []

    def drive() -> None:
        sv, _ = _drive_native(
            registry,
            tmp_path,
            monkeypatch,
            announces=True,
            answer=SMALL,
            plant=_plant(kind, outside),
        )
        holder.append(sv)

    writer = threading.Thread(target=drive, daemon=True)
    writer.start()
    writer.join(10)
    target = tmp_path / "runs" / "run-long" / FINAL_TEXT_FILE
    try:
        assert not writer.is_alive(), "the announce hung on the planted file"
        (sv,) = holder
        stored = registry.get_run("run-long")
        assert stored is not None and stored.phase is RunPhase.COMPLETED
        assert "run-long" not in sv._unpublished
        assert not (tmp_path / "runs" / "run-long" / "terminal.json").exists()
        assert len(bodies) == 1 and SMALL in bodies[0]
        assert "OUTSIDE" not in bodies[0]
        assert sv.result("run-long").final_text == SMALL
    finally:
        if kind == "fifo":
            with contextlib.suppress(OSError):
                os.close(os.open(target, os.O_WRONLY | os.O_NONBLOCK))
        writer.join(10)


def test_a_kept_answer_past_the_stream_tail_is_served_whole(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review round 11: the reader refused a kept file over 512 KiB, so an
    answer of 524,289 bytes, kept whole and announced as whole, was served as
    its 16 KiB row prefix and the notice reported that prefix as the total."""
    answer = "a" * (512 * 1024 + 1)
    bodies = _pushed_bodies(monkeypatch)
    sv, run_id = _drive_native(
        registry, tmp_path, monkeypatch, announces=True, answer=answer
    )
    assert sv.result(run_id).final_text == answer
    assert len(bodies) == 1 and f"of {len(answer)} bytes" in bodies[0]


def test_an_answer_past_what_a_run_may_output_is_never_kept_as_whole(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review round 11: the keep step and the reader share one bound, so an
    answer the reader would not serve whole is never kept or named as whole."""
    answer = "a" * (supervisor.pump.MAX_RUN_OUTPUT_BYTES + 1)
    sv, run_id = _drive_native(registry, tmp_path, monkeypatch, answer=answer)
    stored = registry.get_run(run_id)
    assert stored is not None and stored.phase is RunPhase.COMPLETED
    assert "answers it whole" not in (stored.warning or "")
    assert "the whole was not kept" in (stored.warning or "")
    assert not (sv._run_root_path / run_id / FINAL_TEXT_FILE).exists()


def test_an_unreadable_kept_answer_is_a_typed_error_not_a_prefix(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review round 11: EACCES on a `final_text` nSPAWN kept fell back to the
    row's 16 KiB prefix, unmarked, as if it were the whole answer."""
    sv, run_id = _drive_native(registry, tmp_path, monkeypatch)
    kept = sv._run_root_path / run_id / FINAL_TEXT_FILE
    kept.chmod(0)
    try:
        with pytest.raises(supervisor.SupervisorError, match="final_text"):
            sv.result(run_id)
    finally:
        kept.chmod(0o600)


def test_a_short_answer_never_reads_the_run_directory(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review round 12: any failure to open the run directory (EMFILE, EACCES)
    failed `nspawn_result_show` for every run, short whole answers included.
    Only an answer that may be a cut prefix needs the kept file."""
    sv, run_id = _drive_native(registry, tmp_path, monkeypatch, answer=SMALL)
    run_dir = sv._run_root_path / run_id
    run_dir.chmod(0)
    try:
        assert sv.result(run_id).final_text == SMALL
    finally:
        run_dir.chmod(0o700)


def test_the_kept_answer_read_is_sized_by_the_file(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review round 12: `read(bound + 1)` reserved 64 MiB up front for any
    file, so under an address-space cap a small kept answer raised MemoryError."""
    sv, run_id = _drive_native(registry, tmp_path, monkeypatch)
    size = (sv._run_root_path / run_id / FINAL_TEXT_FILE).stat().st_size
    sizes: list[int] = []

    class Sized:
        def __init__(self, fd: int, mode: str) -> None:
            self._stream = os.fdopen(fd, mode)

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *exc: object) -> None:
            self._stream.close()

        def fileno(self) -> int:
            return self._stream.fileno()

        def read(self, wanted: int = -1) -> bytes:
            sizes.append(wanted)
            return self._stream.read(wanted)  # type: ignore[no-any-return]

    monkeypatch.setattr(supervisor, "open", Sized, raising=False)
    assert sv.result(run_id).final_text == ANSWER
    assert sizes == [size + 1]


def test_a_closed_run_still_serves_its_whole_answer(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review round 14: reading the kept answer only for a COMPLETED row served
    a completed-then-closed run (a close frees its worktree claim, and a result
    survives a close) as its 16 KiB prefix, under a warning naming it whole,
    and a completed notice built after the close spilled the prefix as whole."""
    sv, run_id = _drive_native(registry, tmp_path, monkeypatch)
    row = registry.get_run(run_id)
    assert row is not None
    registry.update_run(replace(row, phase=RunPhase.CLOSED))
    assert sv.result(run_id).final_text == ANSWER
    closed = registry.get_run(run_id)
    body = supervisor.notify.notice_body(
        run_id, "completed", row=closed, run_dir=sv._run_root_path / run_id
    )
    assert f"of {len(ANSWER.encode('utf-8'))} bytes" in body
