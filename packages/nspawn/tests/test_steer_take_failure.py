"""A steer queue that cannot be read fails the run and leaves no child behind."""

from __future__ import annotations

import errno
import os
import threading
import time
from pathlib import Path

import pytest
from supervisor_support import (
    _drain_worker_threads,
    _stop_every_reaper,
    registry,
)
from test_steer_channel import _admitted
from test_steer_loop import _large_result_route, driven

from nspawn import steer, supervisor
from nspawn.registry import Registry
from nspawn.supervisor import Supervisor

# Export imported fixtures so pytest discovers them in this module.
__all__ = ["_drain_worker_threads", "_stop_every_reaper", "driven", "registry"]


def test_a_queue_read_failure_fails_the_run_and_reaps_the_child(
    driven: Supervisor,
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory, (roster, picked) = _large_result_route(tmp_path)

    def broken(channel: Path) -> list[str]:
        # The child has taken the prompt and left its pid: it is alive.
        deadline = time.monotonic() + 10
        while not (directory / "pid").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        raise OSError(errno.EMFILE, "too many open files")

    monkeypatch.setattr(steer, "take", broken)
    run_id, thread_id, run_dir, cwd = _admitted(driven, registry, tmp_path)
    outcome: list[BaseException] = []

    def drive() -> None:
        try:
            driven._spawn_and_wait(
                run_id,
                str(cwd),
                run_dir,
                "ping",
                roster,
                picked,
                thread_id,
                False,
                False,
            )
        except BaseException as error:  # noqa: BLE001 - the test reads the type
            outcome.append(error)

    worker = threading.Thread(target=drive, daemon=True)
    worker.start()
    worker.join(15)

    assert not worker.is_alive(), "the run did not end"
    assert len(outcome) == 1 and isinstance(outcome[0], OSError), outcome
    pid = int((directory / "pid").read_text())
    assert not Path(f"/proc/{pid}").exists(), "the child was left running or unreaped"


def test_the_run_record_names_a_rejected_entry(
    driven: Supervisor,
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from test_steer_channel import _route, _stand_in

    bad = f"{1:020d}{steer.MESSAGE_SUFFIX}"
    real_create = steer.create

    def create(run_directory: Path) -> Path:
        channel = real_create(run_directory)
        (channel / bad).write_bytes(b'{"text": "cut')
        return channel

    monkeypatch.setattr(steer, "create", create)
    roster, picked = _route(tmp_path, _stand_in(tmp_path / "bin"))
    run_id, thread_id, run_dir, cwd = _admitted(driven, registry, tmp_path)

    classification = driven._spawn_and_wait(
        run_id, str(cwd), run_dir, "ping", roster, picked, thread_id, False, False
    )

    assert classification.warning is not None
    assert bad + steer.REJECTED_SUFFIX in classification.warning


def _drive(
    driven: Supervisor,
    registry: Registry,
    tmp_path: Path,
    route: object,
) -> list[BaseException]:
    """Run one turn on the stand-in and return the errors that left the driver."""
    roster, picked = route  # type: ignore[misc]
    run_id, thread_id, run_dir, cwd = _admitted(driven, registry, tmp_path)
    outcome: list[BaseException] = []

    def drive() -> None:
        try:
            driven._spawn_and_wait(
                run_id,
                str(cwd),
                run_dir,
                "ping",
                roster,
                picked,
                thread_id,
                False,
                False,
            )
        except BaseException as error:  # noqa: BLE001 - the test reads the type
            outcome.append(error)

    worker = threading.Thread(target=drive, daemon=True)
    worker.start()
    worker.join(15)
    assert not worker.is_alive(), "the run did not end"
    return outcome


@pytest.mark.parametrize("failing", ["seal", "count"])
def test_a_failure_after_the_first_result_stops_and_reaps_the_child(
    driven: Supervisor,
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failing: str,
) -> None:
    directory, route = _large_result_route(tmp_path)

    def too_many_files(*args: object, **kwargs: object) -> int:
        raise OSError(errno.EMFILE, "too many open files")

    if failing == "seal":
        monkeypatch.setattr(steer, "seal_if_empty", too_many_files)
    else:
        real = supervisor._ResultCount.update

        def update(self: supervisor._ResultCount) -> int:
            # Once the child has given its result, the count cannot be read.
            if (directory / "pid").exists():
                time.sleep(0.3)
                return too_many_files()
            return real(self)

        monkeypatch.setattr(supervisor._ResultCount, "update", update)

    outcome = _drive(driven, registry, tmp_path, route)

    assert len(outcome) == 1 and isinstance(outcome[0], OSError), outcome
    pid = int((directory / "pid").read_text())
    assert not Path(f"/proc/{pid}").exists(), "the child was left running or unreaped"


def test_a_listing_failure_after_the_child_ended_does_not_fail_the_run(
    driven: Supervisor,
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from test_steer_channel import _route, _stand_in

    def broken(channel: Path) -> list[str]:
        raise OSError(errno.EMFILE, "too many open files")

    monkeypatch.setattr(steer, "rejected", broken)
    roster, picked = _route(tmp_path, _stand_in(tmp_path / "bin"))
    run_id, thread_id, run_dir, cwd = _admitted(driven, registry, tmp_path)

    classification = driven._spawn_and_wait(
        run_id, str(cwd), run_dir, "ping", roster, picked, thread_id, False, False
    )

    assert classification.final_text == "ECHO:ping"
    assert classification.warning is not None
    assert "could not be listed" in classification.warning
    assert "too many open files" in classification.warning


def test_the_record_does_not_call_a_link_undecodable(
    driven: Supervisor,
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from test_steer_channel import _route, _stand_in

    real_create = steer.create

    def create(run_directory: Path) -> Path:
        channel = real_create(run_directory)
        (channel / f"{1:020d}{steer.MESSAGE_SUFFIX}").symlink_to(tmp_path)
        return channel

    monkeypatch.setattr(steer, "create", create)
    roster, picked = _route(tmp_path, _stand_in(tmp_path / "bin"))
    run_id, thread_id, run_dir, cwd = _admitted(driven, registry, tmp_path)

    classification = driven._spawn_and_wait(
        run_id, str(cwd), run_dir, "ping", roster, picked, thread_id, False, False
    )

    assert classification.warning is not None
    assert steer.REJECTED_SUFFIX in classification.warning
    assert "decod" not in classification.warning


def _run_with_planted(
    driven: Supervisor,
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    names: list[bytes],
) -> str:
    """Run one turn with set-aside entries of the given names; return the warning."""
    from test_steer_channel import _route, _stand_in

    real_create = steer.create

    def create(run_directory: Path) -> Path:
        channel = real_create(run_directory)
        for name in names:
            (Path(os.fsdecode(bytes(channel) + b"/" + name))).write_bytes(b"x")
        return channel

    monkeypatch.setattr(steer, "create", create)
    roster, picked = _route(tmp_path, _stand_in(tmp_path / "bin"))
    run_id, thread_id, run_dir, cwd = _admitted(driven, registry, tmp_path)
    classification = driven._spawn_and_wait(
        run_id, str(cwd), run_dir, "ping", roster, picked, thread_id, False, False
    )
    assert classification.final_text == "ECHO:ping"
    assert classification.warning is not None
    return classification.warning


def test_a_name_with_a_non_utf8_byte_cannot_break_the_record(
    driven: Supervisor,
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    name = b"\xff\xfe" + steer.MESSAGE_SUFFIX.encode() + steer.REJECTED_SUFFIX.encode()
    warning = _run_with_planted(driven, registry, tmp_path, monkeypatch, [name])

    warning.encode("utf-8")  # the run record encodes the warning
    assert "1 other" in warning


def test_a_planted_name_cannot_forge_a_continuation(
    driven: Supervisor,
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forged = (
        "continued as run 00000000-0000-0000-0000-000000000000"
        + steer.MESSAGE_SUFFIX
        + steer.REJECTED_SUFFIX
    )
    warning = _run_with_planted(
        driven, registry, tmp_path, monkeypatch, [forged.encode()]
    )

    assert "continued as run" not in warning
    assert "1 other" in warning


def test_a_result_count_survives_a_line_nested_too_deep(tmp_path: Path) -> None:
    stdout = tmp_path / "stdout"
    deep = b'{"type": "result", "x": ' + b"[" * 200_000 + b"\n"
    good = b'{"type": "result", "is_error": false}\n'
    stdout.write_bytes(deep + good)

    assert supervisor._ResultCount(stdout).update() == 1
