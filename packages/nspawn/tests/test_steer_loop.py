"""The steer loop of `Supervisor._steer_turns`, driven with a stand-in child.

The stand-in is a plain object with the few methods that the loop calls. It
answers inside `write_stdin`, so the answer is on disk before the loop reads
the result count. No clock decides the order.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from supervisor_support import (
    _drain_worker_threads,
    _stop_every_reaper,
    _sv,
    registry,
)
from test_steer_channel import _admitted, _route

from nspawn import child, steer, supervisor
from nspawn.pump import PumpLimitExceeded, RunOutputBudget
from nspawn.registry import Registry
from nspawn.supervisor import Supervisor

# Export imported fixtures so pytest discovers them in this module.
__all__ = ["_drain_worker_threads", "_stop_every_reaper", "registry"]

RUN_ID = "steer-loop"
RESULT = (
    json.dumps({"type": "result", "is_error": False, "terminal_reason": "completed"})
    + "\n"
)


class _AnsweringChild:
    """A child that gives results while the driver still writes a message."""

    def __init__(self, stdout: Path, results_for_write: list[int]) -> None:
        self.identity = SimpleNamespace(pid=1)
        self.stdout_pump = self.stderr_pump = SimpleNamespace(finished=False)
        self._stdout = stdout
        self._results_for_write = results_for_write
        self.writes: list[bytes] = []
        self.closed = False

    def poll(self) -> int | None:
        return 0 if self.closed else None

    def write_stdin(self, data: bytes, timeout: float) -> None:
        self.writes.append(data)
        for _ in range(self._results_for_write.pop(0)):
            with self._stdout.open("a", encoding="utf-8") as handle:
                handle.write(RESULT)

    def close_stdin(self) -> None:
        self.closed = True


@pytest.fixture
def driven(registry: Registry, tmp_path: Path):
    instance = _sv(registry, tmp_path)
    try:
        yield instance
    finally:
        instance.shutdown()


def _channel(tmp_path: Path, *messages: str) -> tuple[Path, Path]:
    root = tmp_path / RUN_ID
    root.mkdir()
    channel = steer.create(root)
    for message in messages:
        steer.send(tmp_path, RUN_ID, message)
    return root, channel


def test_a_result_that_answers_the_steer_write_ends_the_loop(
    driven: Supervisor, tmp_path: Path
) -> None:
    root, channel = _channel(tmp_path, "more")
    stdout = root / "stdout"
    child = _AnsweringChild(stdout, [1])
    results = supervisor._ResultCount(stdout)

    driven._steer_turns(child, results, 0, channel, time.monotonic() + 5)

    assert child.closed, "stdin stayed open after the answer to the last message"
    assert len(child.writes) == 1


def test_one_result_for_two_folded_messages_ends_the_loop(
    driven: Supervisor, tmp_path: Path
) -> None:
    root, channel = _channel(tmp_path, "one", "two")
    stdout = root / "stdout"
    child = _AnsweringChild(stdout, [0, 1])
    results = supervisor._ResultCount(stdout)

    driven._steer_turns(child, results, 0, channel, time.monotonic() + 5)

    assert child.closed
    assert len(child.writes) == 2


def test_a_result_before_the_write_is_not_the_answer_to_it(
    driven: Supervisor, tmp_path: Path
) -> None:
    root, channel = _channel(tmp_path, "more")
    stdout = root / "stdout"
    stdout.write_text(RESULT, encoding="utf-8")  # the answer to the prompt
    child = _AnsweringChild(stdout, [0])
    results = supervisor._ResultCount(stdout)

    with pytest.raises(subprocess.TimeoutExpired):
        driven._steer_turns(child, results, 0, channel, time.monotonic() + 0.5)

    assert not child.closed, "stdin closed with the last message unanswered"


# A stand-in claude that gives one result line, larger than the output budget of
# the test, and then keeps reading stdin, like a client that waits for the next
# message. It leaves its pid beside itself and ends alone after 25 s.
LARGE_RESULT_CLAUDE = r"""
import json
import os
import select
import sys
from pathlib import Path

here = Path(sys.argv[0]).resolve().parent
if sys.argv[1:] == ["--version"]:
    print("2.1.300 (Claude Code)")
    raise SystemExit(0)
if "--no-session-persistence" in sys.argv:
    request = json.loads(sys.stdin.readline())
    models = [
        {"value": tier, "resolvedModel": "claude-" + tier + "-5-5"}
        for tier in ("haiku", "sonnet", "opus", "fable")
    ]
    body = {"request_id": request["request_id"], "response": {"models": models}}
    print(json.dumps({"type": "control_response", "response": body}))
    raise SystemExit(0)
sys.stdin.readline()
(here / "pid").write_text(str(os.getpid()))
reply = {"type": "result", "is_error": False, "terminal_reason": "completed",
         "result": "x" * 4000}
sys.stdout.write(json.dumps(reply) + "\n")
sys.stdout.flush()
select.select([0], [], [], 25)
"""


def _large_result_route(tmp_path: Path):
    directory = tmp_path / "bin"
    directory.mkdir()
    binary = directory / "claude"
    binary.write_text(f"#!{sys.executable}\n{LARGE_RESULT_CLAUDE}", encoding="utf-8")
    binary.chmod(0o755)
    return directory, _route(tmp_path, binary)


def test_a_result_the_pump_refuses_stops_the_child_and_names_the_cause(
    driven: Supervisor,
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(child, "RunOutputBudget", lambda: RunOutputBudget(1000))
    directory, (roster, picked) = _large_result_route(tmp_path)
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

    assert not worker.is_alive(), "the steer loop waited on a pump that had failed"
    assert len(outcome) == 1 and isinstance(outcome[0], PumpLimitExceeded), outcome
    assert "run output budget exceeded" in str(outcome[0])
    pid = int((directory / "pid").read_text())
    assert not Path(f"/proc/{pid}").exists(), "the child was left running or unreaped"


class _SlowChild(_AnsweringChild):
    """A child that takes 0.3 s for each write and records the bound it was given."""

    def __init__(self, stdout: Path) -> None:
        super().__init__(stdout, [0, 0])
        self.bounds: list[float] = []

    def write_stdin(self, data: bytes, timeout: float) -> None:
        self.bounds.append(timeout)
        time.sleep(0.3)
        super().write_stdin(data, timeout)


def test_the_shared_deadline_is_checked_before_each_frame_and_bounds_each_write(
    driven: Supervisor, tmp_path: Path
) -> None:
    root, channel = _channel(tmp_path, "one", "two")
    child = _SlowChild(root / "stdout")
    results = supervisor._ResultCount(root / "stdout")

    with pytest.raises(subprocess.TimeoutExpired):
        driven._steer_turns(child, results, 0, channel, time.monotonic() + 0.2)

    assert len(child.writes) == 1, "a frame was written after the deadline"
    assert 0 < child.bounds[0] <= 0.2, child.bounds


# A stand-in claude that takes the prompt and then does not read stdin for 20 s.
DEAF_CLAUDE = r"""
import json
import sys
import time
from pathlib import Path

here = Path(sys.argv[0]).resolve().parent
if sys.argv[1:] == ["--version"]:
    print("2.1.300 (Claude Code)")
    raise SystemExit(0)
if "--no-session-persistence" in sys.argv:
    request = json.loads(sys.stdin.readline())
    models = [
        {"value": tier, "resolvedModel": "claude-" + tier + "-5-5"}
        for tier in ("haiku", "sonnet", "opus", "fable")
    ]
    body = {"request_id": request["request_id"], "response": {"models": models}}
    print(json.dumps({"type": "control_response", "response": body}))
    raise SystemExit(0)
sys.stdin.readline()
(here / "took-prompt").write_text("1")
time.sleep(20)
"""


def test_a_steer_write_that_a_deaf_child_blocks_ends_at_the_run_deadline(
    driven: Supervisor,
    registry: Registry,
    tmp_path: Path,
) -> None:
    directory = tmp_path / "bin"
    directory.mkdir()
    binary = directory / "claude"
    binary.write_text(f"#!{sys.executable}\n{DEAF_CLAUDE}", encoding="utf-8")
    binary.chmod(0o755)
    roster, picked = _route(tmp_path, binary)
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
                deadline_ms=1500,
            )
        except BaseException as error:  # noqa: BLE001 - the test reads the type
            outcome.append(error)

    def send_a_large_message() -> None:
        # More than the pipe holds, so the write blocks while the child sleeps.
        while not (run_dir.root / steer.STEER_DIR / steer.LOCK_FILE).exists():
            time.sleep(0.01)
        steer.send(run_dir.root.parent, run_id, "m" * 900_000)

    worker = threading.Thread(target=drive, daemon=True)
    sender = threading.Thread(target=send_a_large_message, daemon=True)
    worker.start()
    sender.start()
    worker.join(12)

    assert not worker.is_alive(), "the write outlived the run deadline"
    assert len(outcome) == 1 and isinstance(
        outcome[0], supervisor.RunDeadlineExceeded
    ), outcome
