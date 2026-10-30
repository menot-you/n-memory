"""Busy progress writes must not replace the vendor result."""

import json
import os
import stat
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from functools import partial
from pathlib import Path
from types import SimpleNamespace, TracebackType
from typing import Any, Self

import nott_database
import pytest
from supervisor_support import (
    FakeSpawnedChild,
    _admit_pending,
    _drain_worker_threads,
    _spawn_real,
    _stop_every_reaper,
    _sv,
    _wire_l8_and_l3,
    fake_picked,
    fake_provider,
    registry,
    roster_home,
)
from test_native_final_text_cap import _drive_native

from nspawn import agy, native, picker, supervisor, terminal
from nspawn.owner_roster import NativeAccount, Provider, Roster
from nspawn.redact import SecretNeedles
from nspawn.registry import Registry, RegistryError
from nspawn.rows import Backend, NativeSessionContext, RunPhase, RunRow
from nspawn.supervisor import RunDirectory, Supervisor, create_run_directory

__all__ = [
    "_drain_worker_threads",
    "_stop_every_reaper",
    "fake_picked",
    "fake_provider",
    "registry",
    "roster_home",
]

BUSY_STORE = "nspawn.sqlite3-write.lock is held by another holder past 10.0 s"
RESULT = (
    b'{"type":"assistant"}\n'
    b'{"type":"system","subtype":"thinking_tokens"}\n'
    b'{"type":"result","is_error":false,"terminal_reason":"completed",'
    b'"result":"PONG","usage":{"input_tokens":2,"output_tokens":3}}\n'
)


@pytest.fixture(params=["guarded", "writer"])
def worker(
    request: pytest.FixtureRequest,
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_picked: picker.PickerResult,
) -> tuple[Supervisor, str, RunDirectory, Callable[[], None]]:
    sv = _sv(registry, tmp_path)
    roster = Roster.read(Roster.default_path())
    run_id, thread_id = _admit_pending(sv, registry, tmp_path, worktree=str(tmp_path))
    root = sv._run_root_path / run_id
    run_dir = RunDirectory(root, root / "stdout", root / "stderr")

    def spawn(*args: object, **kwargs: object) -> FakeSpawnedChild:
        run_dir.stdout.write_bytes(RESULT)
        run_dir.stderr.write_bytes(b"")
        return FakeSpawnedChild(_spawn_real("true"))

    _wire_l8_and_l3(monkeypatch, spawn)
    if request.param == "guarded":
        drive = partial(
            sv._drive_run,
            run_id,
            str(tmp_path),
            run_dir,
            "ping",
            roster,
            fake_picked,
            thread_id,
            False,
            False,
        )
    else:
        # Replace the custody boundary, not the worker failure clauses.
        monkeypatch.setattr(sv, "_open_writer", lambda _: object())
        monkeypatch.setattr(sv, "_close_writer", lambda *args: None)
        monkeypatch.setattr(
            registry,
            "session_profile",
            lambda _: SimpleNamespace(original_run_id=run_id, execution_class=None),
        )
        original_spawn = sv._spawn_and_wait

        def uncaged(*args: Any, **kwargs: Any) -> Any:
            kwargs.pop("writer")
            return original_spawn(*args, **kwargs)

        monkeypatch.setattr(sv, "_spawn_and_wait", uncaged)
        row = registry.get_run(run_id)
        assert row is not None
        drive = partial(
            sv._drive_claude_caged,
            row,
            run_dir,
            "ping",
            roster,
            fake_picked,
            None,
            0,
        )
    return sv, run_id, run_dir, drive


@pytest.mark.parametrize("phase", [RunPhase.COMPLETED, RunPhase.FAILED])
def test_busy_record_write_is_skipped_and_the_vendor_result_wins(
    worker: tuple[Supervisor, str, RunDirectory, Callable[[], None]],
    registry: Registry,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    phase: RunPhase,
) -> None:
    sv, run_id, run_dir, drive = worker
    original = registry._write
    classify = terminal.classify_terminal_run
    attempts = 0

    def classified(*args: Any, **kwargs: Any) -> terminal.TerminalClassification:
        return replace(classify(*args, **kwargs), phase=phase)

    def busy(statement: str, params: Any = ()) -> int:
        nonlocal attempts
        if statement.startswith("UPDATE runs SET last_record_at"):
            attempts += 1
            if attempts > 1:
                # Stop the old infinite loop without a test timeout or a live child.
                raise nott_database.OperationalError("progress was retried")
            raise nott_database.BusyError(BUSY_STORE)
        return original(statement, params)

    monkeypatch.setattr(terminal, "classify_terminal_run", classified)
    monkeypatch.setattr(registry, "_write", busy)
    sv._guarded_worker(run_id, drive)

    status = sv.status(run_id)
    assert status.phase is phase, status.failure_reason
    assert status.tokens == 5
    assert status.total_seq == 3
    assert sv.result(run_id).final_text == "PONG"
    assert attempts == 1
    row = registry.get_run(run_id)
    assert row is not None and row.records_seen == 0
    assert not (run_dir.root / "worker_errors").exists()
    lines = capsys.readouterr().err.splitlines()
    assert lines == [
        f"nspawn.worker: {run_id}: skipped note_record_observed: BusyError"
    ]
    # A new full-stream observation can replace the empty count cache.
    monkeypatch.setattr(registry, "_write", original)
    registry.note_record_observed(
        run_id, row.updated_at + 1, count=run_dir.stdout.read_bytes().count(b"\n")
    )
    refreshed = registry.get_run(run_id)
    assert refreshed is not None and refreshed.records_seen == 3
    assert refreshed.last_record_at == row.updated_at + 1
    assert refreshed.phase is phase


def test_busy_launch_identity_keeps_main_refusal_and_releases_lock(
    worker: tuple[Supervisor, str, RunDirectory, Callable[[], None]],
    registry: Registry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sv, run_id, _, drive = worker
    original = registry.update_run
    attempts = 0
    sleeps: list[float] = []

    def busy_once(row: RunRow, **kwargs: Any) -> None:
        nonlocal attempts
        if row.phase is RunPhase.RUNNING:
            attempts += 1
            if attempts == 1:
                raise nott_database.BusyError(BUSY_STORE)
        original(row, **kwargs)

    def no_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        raise nott_database.OperationalError("launch retry retained its lock")

    monkeypatch.setattr(registry, "update_run", busy_once)
    monkeypatch.setattr(supervisor.time, "sleep", no_sleep)
    sv._guarded_worker(run_id, drive)

    reads: list[RunPhase] = []
    reader = threading.Thread(target=lambda: reads.append(sv.status(run_id).phase))
    reader.start()
    reader.join(timeout=2)
    assert not reader.is_alive()
    assert sleeps == [], "a launch write must not retry under the custody lock"
    assert attempts == 1
    assert reads == [RunPhase.FAILED]
    assert sv.status(run_id).failure_reason in ("WorkerDied: BusyError", "BusyError")


def test_worker_failure_keeps_safe_stack_in_its_own_log(
    worker: tuple[Supervisor, str, RunDirectory, Callable[[], None]],
    registry: Registry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sv, run_id, run_dir, drive = worker
    secret = "synthetic-worker-diagnostic-secret"

    def failed_progress_write(*args: object, **kwargs: object) -> None:
        raise nott_database.OperationalError(secret)

    monkeypatch.setattr(registry, "note_record_observed", failed_progress_write)
    sv._guarded_worker(run_id, drive)

    status = sv.status(run_id)
    assert status.phase is RunPhase.FAILED
    assert status.failure_reason in (
        "WorkerDied: OperationalError",
        "OperationalError",
    )
    log = (run_dir.root / "worker_errors").read_text()
    assert "Traceback (most recent call last):" in log
    assert "_spawn_and_wait" in log
    assert "failed_progress_write" in log
    assert "OperationalError" in log
    assert secret not in log
    assert "raise " not in log
    assert stat.S_IMODE((run_dir.root / "worker_errors").stat().st_mode) == 0o600


def test_busy_terminal_write_keeps_the_real_result_for_retry(
    worker: tuple[Supervisor, str, RunDirectory, Callable[[], None]],
    registry: Registry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sv, run_id, _, drive = worker
    original = registry.update_run
    attempts = 0

    def busy_once(row: RunRow, **kwargs: Any) -> None:
        nonlocal attempts
        if row.phase is RunPhase.COMPLETED:
            attempts += 1
            if attempts == 1:
                raise nott_database.BusyError(BUSY_STORE)
        original(row, **kwargs)

    monkeypatch.setattr(registry, "update_run", busy_once)
    sv._guarded_worker(run_id, drive)

    assert sv.status(run_id).phase is RunPhase.RUNNING
    assert run_id in sv._unpublished
    assert sv._unpublished[run_id].final_text == "PONG"
    monkeypatch.setattr(registry, "reconnect", lambda: None)
    assert sv.publish_pending() is True
    assert sv.status(run_id).phase is RunPhase.COMPLETED
    assert sv.result(run_id).final_text == "PONG"
    assert attempts == 2


@pytest.mark.parametrize(
    "method", ["record_provider_wall", "record_account_wall", "record_account_usage"]
)
def test_busy_quota_and_wall_writes_keep_main_refusal(
    worker: tuple[Supervisor, str, RunDirectory, Callable[[], None]],
    registry: Registry,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
) -> None:
    sv, run_id, _, drive = worker
    original_classify = terminal.classify_terminal_run

    def observed(*args: Any, **kwargs: Any) -> terminal.TerminalClassification:
        result = original_classify(*args, **kwargs)
        return replace(
            result,
            provider_wall=terminal.ProviderWallRecord(
                "acme", 4_102_444_800, terminal.WallSource.VENDOR_RESET, 1000
            ),
            account_wall=terminal.AccountWallRecord(
                "acct-1", 4_102_444_800, terminal.WallSource.VENDOR_RESET, "5h", 1000
            ),
            usage=(terminal.WindowUsage("5h", 0.5, 4_102_444_800),),
        )

    monkeypatch.setattr(terminal, "classify_terminal_run", observed)
    original = getattr(registry, method)
    attempts = 0

    def busy_once(*args: Any, **kwargs: Any) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise nott_database.BusyError(BUSY_STORE)
        original(*args, **kwargs)

    monkeypatch.setattr(registry, method, busy_once)
    sv._guarded_worker(run_id, drive)

    assert sv.status(run_id).phase is RunPhase.FAILED
    assert sv.status(run_id).failure_reason in ("WorkerDied: BusyError", "BusyError")
    assert attempts == 1


def test_busy_progress_leaves_the_launch_lock_free_during_skip_and_afterward(
    worker: tuple[Supervisor, str, RunDirectory, Callable[[], None]],
    registry: Registry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sv, run_id, _, drive = worker
    original_print = print
    reads: list[RunPhase] = []
    readers: list[threading.Thread] = []
    blocked: list[bool] = []
    attempts = 0

    def probe() -> None:
        reader = threading.Thread(target=lambda: reads.append(sv.status(run_id).phase))
        readers.append(reader)
        reader.start()
        reader.join(timeout=1)
        blocked.append(reader.is_alive())

    def busy(*args: Any, **kwargs: Any) -> None:
        nonlocal attempts
        attempts += 1
        if attempts > 1:
            raise nott_database.OperationalError("progress was retried")
        raise nott_database.BusyError(BUSY_STORE)

    def logged(*args: Any, **kwargs: Any) -> None:
        if args and "skipped note_record_observed" in str(args[0]):
            probe()
        original_print(*args, **kwargs)

    monkeypatch.setattr(registry, "note_record_observed", busy)
    monkeypatch.setattr(supervisor, "print", logged, raising=False)
    sv._guarded_worker(run_id, drive)
    probe()
    for reader in readers:
        reader.join(timeout=2)
    assert reads == [RunPhase.RUNNING, RunPhase.COMPLETED]
    assert blocked == [False, False]
    assert attempts == 1


@pytest.mark.parametrize("plant", ["symlink", "fifo", "directory"])
def test_worker_diagnostic_refusal_does_not_change_the_failure(
    worker: tuple[Supervisor, str, RunDirectory, Callable[[], None]],
    registry: Registry,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    plant: str,
) -> None:
    sv, run_id, run_dir, drive = worker
    path = run_dir.root / "worker_errors"
    outside = tmp_path / "outside"
    outside.write_text("keep")
    if plant == "symlink":
        path.symlink_to(outside)
    elif plant == "fifo":
        os.mkfifo(path)
    else:
        path.mkdir()

    def refused(*args: object, **kwargs: object) -> None:
        raise nott_database.OperationalError("synthetic detail")

    monkeypatch.setattr(registry, "note_record_observed", refused)
    sv._guarded_worker(run_id, drive)

    assert sv.status(run_id).phase is RunPhase.FAILED
    assert "OperationalError" in (sv.status(run_id).failure_reason or "")
    assert outside.read_text() == "keep"


@pytest.mark.parametrize("method", ["note_record_observed", "last_item"])
def test_native_worker_keeps_snapshot_refusal_or_skips_record_counts(
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    method: str,
) -> None:
    target = "update_run" if method == "last_item" else method
    original = getattr(registry, target)
    attempts = 0

    def busy(*args: Any, **kwargs: Any) -> None:
        nonlocal attempts
        progress = method == "note_record_observed" or (
            args[0].phase is RunPhase.RUNNING and args[0].last_item is not None
        )
        if progress:
            attempts += 1
            if attempts > 1:
                raise nott_database.OperationalError("native progress was retried")
            raise nott_database.BusyError(BUSY_STORE)
        original(*args, **kwargs)

    monkeypatch.setattr(registry, target, busy)
    sv, run_id = _drive_native(registry, tmp_path, monkeypatch, answer="PONG")

    status = sv.status(run_id)
    assert attempts == 1
    row = registry.get_run(run_id)
    assert row is not None
    if method == "last_item":
        assert status.phase is RunPhase.FAILED
        assert status.failure_reason == "BusyError"
        assert row.last_item is None
        assert capsys.readouterr().err == ""
    else:
        assert status.phase is RunPhase.COMPLETED, status.failure_reason
        assert status.tokens == 9
        assert sv.result(run_id).final_text == "PONG"
        assert row.records_seen == 0
        assert status.total_seq == 1
        assert capsys.readouterr().err.splitlines() == [
            f"nspawn.worker: {run_id}: skipped note_record_observed: BusyError"
        ]


@pytest.mark.parametrize("chain", ["cause", "context", "suppressed", "both", "cycle"])
def test_worker_failure_keeps_all_chained_types_and_frames(
    worker: tuple[Supervisor, str, RunDirectory, Callable[[], None]],
    registry: Registry,
    monkeypatch: pytest.MonkeyPatch,
    chain: str,
) -> None:
    sv, run_id, run_dir, drive = worker
    secret = "synthetic-chain-secret"

    def busy_cause() -> None:
        raise nott_database.BusyError(secret)

    def extra_context() -> BaseException:
        try:
            raise nott_database.OperationalError(secret)
        except nott_database.OperationalError as error:
            return error

    def wrapped_progress(*args: Any, **kwargs: Any) -> None:
        try:
            busy_cause()
        except nott_database.BusyError as error:
            wrapped = RegistryError(secret)
            if chain == "both":
                wrapped.__cause__ = error
                error.__context__ = extra_context()
            elif chain == "cycle":
                error.__cause__ = wrapped
            if chain in ("cause", "both", "cycle"):
                raise wrapped from error
            if chain == "suppressed":
                raise wrapped from None
            raise wrapped

    monkeypatch.setattr(registry, "note_record_observed", wrapped_progress)
    sv._guarded_worker(run_id, drive)

    log = (run_dir.root / "worker_errors").read_text()
    assert "RegistryError" in log
    assert "BusyError" in log
    assert "busy_cause" in log
    assert "wrapped_progress" in log
    if chain == "both":
        assert "OperationalError" in log
        assert "extra_context" in log
    assert secret not in log
    assert log.count("nspawn.worker:") == 1


def test_worker_errors_has_a_fixed_ceiling_and_keeps_the_newest_failure(
    worker: tuple[Supervisor, str, RunDirectory, Callable[[], None]],
) -> None:
    sv, run_id, run_dir, _ = worker
    ceiling = 64 * 1024

    class OlderFailure(Exception):
        pass

    class NewestFailure(Exception):
        pass

    def failed(kind: type[Exception]) -> None:
        raise kind("synthetic message must be omitted")

    for _ in range(700):
        sv._guarded_worker(run_id, failed, OlderFailure)
    sv._guarded_worker(run_id, failed, NewestFailure)

    data = (run_dir.root / "worker_errors").read_bytes()
    assert len(data) <= ceiling
    assert b"cut older text" in data
    assert b"NewestFailure" in data
    assert b"synthetic message" not in data


def test_one_large_worker_failure_stays_within_the_ceiling(
    worker: tuple[Supervisor, str, RunDirectory, Callable[[], None]],
) -> None:
    sv, run_id, run_dir, _ = worker

    ceiling = supervisor.WORKER_ERRORS_MAX_BYTES
    try:
        raise RuntimeError("synthetic deep failure")
    except RuntimeError as error:
        original = error.__traceback__
        assert original is not None
        code = original.tb_frame.f_code
        frame_line = (
            f"  File {code.co_filename!r}, line {original.tb_lineno}, in {code.co_name}\n"
        ).encode()
        frame_count = ceiling // len(frame_line) + 1
        uncapped_frames = frame_line * frame_count
        assert len(uncapped_frames) > ceiling
        trace = None
        for _ in range(frame_count):
            trace = TracebackType(
                trace, original.tb_frame, original.tb_lasti, original.tb_lineno
            )
        oversized = error.with_traceback(trace)

    def oversized_failure() -> None:
        raise oversized

    sv._guarded_worker(run_id, oversized_failure)
    data = (run_dir.root / "worker_errors").read_bytes()
    assert len(data) <= ceiling
    assert b"cut older text" in data
    assert run_id.encode() in data
    assert b"RuntimeError" in data


@pytest.mark.parametrize("fault", ["open", "write"])
def test_diagnostic_failure_fallback_prints_types_without_chained_messages(
    worker: tuple[Supervisor, str, RunDirectory, Callable[[], None]],
    registry: Registry,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fault: str,
) -> None:
    sv, run_id, _, drive = worker
    secret = "synthetic-original-credential"
    detail = "synthetic-diagnostic-detail"
    real_open = open

    def failed_progress(*args: Any, **kwargs: Any) -> None:
        raise nott_database.OperationalError(secret)

    def refused_open(*args: Any, **kwargs: Any) -> int:
        raise PermissionError(detail)

    class RefusedSink:
        def __init__(self, stream: Any) -> None:
            self.stream = stream

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *args: object) -> None:
            self.stream.close()

        def __getattr__(self, name: str) -> Any:
            return getattr(self.stream, name)

        def write(self, data: object) -> None:
            raise OSError(detail)

    def refused_write(file: Any, *args: Any, **kwargs: Any) -> Any:
        stream = real_open(file, *args, **kwargs)
        return RefusedSink(stream) if isinstance(file, int) else stream

    monkeypatch.setattr(registry, "note_record_observed", failed_progress)
    if fault == "open":
        monkeypatch.setattr(supervisor, "_open_regular", refused_open)
    else:
        monkeypatch.setattr(supervisor, "open", refused_write, raising=False)
    sv._guarded_worker(run_id, drive)

    assert sv.status(run_id).phase is RunPhase.FAILED
    stderr = capsys.readouterr().err
    assert secret not in stderr
    assert detail not in stderr
    assert "Traceback" not in stderr
    assert "OperationalError" in stderr
    assert ("PermissionError" if fault == "open" else "OSError") in stderr
    assert len(stderr.splitlines()) == 1


@pytest.mark.parametrize("busy_write", ["snapshot", "record_count", None])
def test_agy_required_usage_snapshot_keeps_named_busy_refusal(
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    busy_write: str | None,
) -> None:
    sv = _sv(registry, tmp_path)
    context = NativeSessionContext(str(tmp_path))
    row = RunRow(
        run_id="run-agy",
        worktree=str(tmp_path),
        phase=RunPhase.PENDING,
        sandbox="read-only",
        model="m",
        backend=Backend.AGY,
        owner_instance=registry.instance_id,
        created_at=1,
        updated_at=1,
        executed_provider="agy",
        executed_account="fixture",
    )
    registry.admit_run(row, False, 1, sv.RUN_CLAIM_LEASE_MS, native_context=context)
    run_dir = create_run_directory(sv._run_root_path, row.run_id, dir_fd=sv._run_root)
    run_dir.stdout.write_bytes(b'{"event":"result"}\n')
    usage = {"input_tokens": 4, "output_tokens": 5, "total_tokens": 9}

    def run(*args: Any, **kwargs: Any) -> agy.AgyResult:
        kwargs["on_thread"]("agy-conversation")
        return agy.AgyResult("agy-conversation", "SUCCESS", "PONG", usage, None, 0.1)

    @contextmanager
    def start(*args: Any, **kwargs: Any) -> Iterator[SimpleNamespace]:
        child = FakeSpawnedChild(_spawn_real("true"))
        try:
            yield SimpleNamespace(child=child, run=run)
        finally:
            child.wait(timeout=2)

    launch = native.NativeLaunch(
        ("/never-start/vendor",),
        {},
        SecretNeedles.none(),
        context,
        NativeAccount(tmp_path),
    )
    picked = picker.PickerResult(
        "agy", Provider(name="agy", kind="agy", model="m"), "fixture", "m", "high"
    )
    monkeypatch.setattr(agy.AgyTurn, "start", start)
    monkeypatch.setattr(native, "agy_confined", lambda launch, **kwargs: launch)
    original_update = registry.update_run
    original_count = registry.note_record_observed
    misses = 0

    def update(updated: RunRow, **kwargs: Any) -> None:
        nonlocal misses
        if (
            busy_write == "snapshot"
            and updated.phase is RunPhase.RUNNING
            and updated.last_item is not None
        ):
            misses += 1
            raise nott_database.BusyError(BUSY_STORE)
        original_update(updated, **kwargs)

    def counted(*args: Any, **kwargs: Any) -> None:
        nonlocal misses
        if busy_write == "record_count":
            misses += 1
            raise nott_database.BusyError(BUSY_STORE)
        original_count(*args, **kwargs)

    monkeypatch.setattr(registry, "update_run", update)
    monkeypatch.setattr(registry, "note_record_observed", counted)
    sv._drive_native_run(row, run_dir, "ping", picked, launch, 0, {})
    stored = registry.get_run(row.run_id)
    assert stored is not None
    assert misses == (0 if busy_write is None else 1)
    if busy_write == "snapshot":
        assert stored.last_item is None
        assert stored.phase is RunPhase.FAILED, (
            "AGY completed without its required resume evidence"
        )
        assert stored.blocked_reason == "BusyError"
        assert "BusyError" in (run_dir.root / "worker_errors").read_text()
        assert capsys.readouterr().err == ""
    else:
        assert stored.phase is RunPhase.COMPLETED
        assert stored.tokens == 9
        assert json.loads(stored.last_item or "null") == {
            "native_usage": usage,
            "native_usage_scope": "conversation",
        }
        assert sv.status(row.run_id).total_seq == 1
        if busy_write == "record_count":
            assert stored.records_seen == 0
            assert capsys.readouterr().err.splitlines() == [
                f"nspawn.worker: {row.run_id}: skipped note_record_observed: BusyError"
            ]
