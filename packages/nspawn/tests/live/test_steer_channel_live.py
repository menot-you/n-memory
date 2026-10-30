from __future__ import annotations

import json
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_steer_channel import STEER_CLAUDE

from live.live_support import MEMBER_ROOT, McpStdio

TERMINAL = ("completed", "failed", "closed", "orphaned")
HOLD = "HOLD the first turn"
ONE = 'steer "one"\nsecond line ✓'
TWO = "steer two"


def _said(sent: subprocess.CompletedProcess[str]) -> str:
    return f"turn send exited {sent.returncode} and said {sent.stderr.strip()!r}"


# One real nspawn stdio server and driver, launched directly, with the stand-in
# claude of test_steer_channel as the child. The stand-in keeps its logs in its own
# directory. The prompt of a run that holds the word HOLD keeps the first turn open
# until the test calls release. The teardown calls release too.
@pytest.fixture
def door(
    live_environment: dict[str, str],
    tmp_path: Path,
) -> Iterator[SimpleNamespace]:
    directory = tmp_path / "stand-in"
    directory.mkdir()
    binary = directory / "claude"
    binary.write_text(f"#!{sys.executable}\n{STEER_CLAUDE}", encoding="utf-8")
    binary.chmod(0o755)
    work = tmp_path / "work"
    work.mkdir()
    environment = {
        **live_environment,
        "NOTT_NSPAWN_CLAUDE_BIN": str(binary),
        "NOTT_NSPAWN_DRIVER_LAUNCH": "direct",
    }
    client = McpStdio(environment)

    def call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        answer = client.call_tool(name, arguments)
        assert "error" not in answer, f"{name} answered {answer!r}"
        payload: dict[str, Any] = json.loads(answer["result"]["content"][0]["text"])
        return payload

    def start(prompt: str) -> str:
        arguments = {
            "prompt": prompt,
            "cwd": str(work),
            "class": "mechanical_fanout",
            "read_only": True,
        }
        run_id: str = call("nspawn_run_start", arguments)["run_id"]
        return run_id

    def records(name: str) -> list[Any]:
        path = directory / name
        if not path.exists():
            return []
        text = path.read_text(encoding="utf-8")
        return [json.loads(line) for line in text.split("\n")[:-1]]

    def users() -> list[str]:
        return [r["text"] for r in records("stdin.log") if r["kind"] == "user"]

    def taken() -> bool:
        return any(r["kind"] != "other" for r in records("stdin.log"))

    def wait(ready: Callable[[], bool], what: str) -> None:
        deadline = time.monotonic() + 30
        while not ready():
            assert time.monotonic() < deadline, f"timed out waiting for {what}"
            time.sleep(0.05)

    def finish(run_id: str) -> str:
        deadline = time.monotonic() + 60
        phase = ""
        while phase not in TERMINAL and time.monotonic() < deadline:
            shown = call("nspawn_run_show", {"run_id": run_id, "timeout_ms": 2000})
            phase = shown["phase"]
        assert phase in TERMINAL, f"run {run_id} was still {phase!r} after 60 s"
        return phase

    def transcript(run_id: str) -> str:
        page = call("nspawn_transcript_show", {"run_id": run_id, "since_seq": 0})
        return "\n".join(page["lines"])

    def send(run_id: str, text: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "nspawn.cli", "turn", "send", run_id, text],
            cwd=MEMBER_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def release() -> None:
        (directory / "release").write_text("go", encoding="utf-8")

    try:
        client.handshake()
        yield SimpleNamespace(
            start=start,
            records=records,
            users=users,
            taken=taken,
            wait=wait,
            finish=finish,
            transcript=transcript,
            send=send,
            release=release,
        )
    finally:
        release()
        client.close()


def test_turn_send_queues_a_message_for_a_live_run(
    door: SimpleNamespace,
) -> None:
    run_id = door.start(HOLD)
    door.wait(door.taken, "the child to take its prompt")
    sent = door.send(run_id, TWO)
    assert sent.returncode == 0, _said(sent)


def test_a_queued_message_reaches_the_child_as_one_user_message(
    door: SimpleNamespace,
) -> None:
    run_id = door.start(HOLD)
    door.wait(door.taken, "the child to take its prompt")
    sent = door.send(run_id, ONE)
    assert sent.returncode == 0, _said(sent)
    door.release()
    door.wait(lambda: len(door.users()) >= 2, "the steer message on stdin")
    door.finish(run_id)
    users = door.users()
    assert users == [HOLD, ONE], f"the child read {users!r}"


def test_two_queued_messages_reach_the_child_in_order(
    door: SimpleNamespace,
) -> None:
    run_id = door.start(HOLD)
    door.wait(door.taken, "the child to take its prompt")
    first = door.send(run_id, ONE)
    second = door.send(run_id, TWO)
    assert first.returncode == 0, _said(first)
    assert second.returncode == 0, _said(second)
    door.release()
    door.wait(lambda: len(door.users()) >= 3, "both steer messages on stdin")
    door.finish(run_id)
    users = door.users()
    assert users == [HOLD, ONE, TWO], f"the child read {users!r}"


def test_stdin_closes_after_the_last_answer_and_not_before(
    door: SimpleNamespace,
) -> None:
    run_id = door.start(HOLD)
    door.wait(door.taken, "the child to take its prompt")
    sent = door.send(run_id, ONE)
    assert sent.returncode == 0, _said(sent)
    door.release()
    door.finish(run_id)
    early = door.records("early.json")
    assert early == [{"eof": False}], f"stdin closed early: {early!r}"
    users = door.users()
    assert users == [HOLD, ONE], f"the queued message was lost: {users!r}"
    end = door.records("end.json")
    assert end == [{"eof": True}], f"stdin never closed: {end!r}"


def test_the_answer_to_a_steer_message_is_in_the_transcript(
    door: SimpleNamespace,
) -> None:
    run_id = door.start(HOLD)
    door.wait(door.taken, "the child to take its prompt")
    sent = door.send(run_id, TWO)
    assert sent.returncode == 0, _said(sent)
    door.release()
    door.finish(run_id)
    text = door.transcript(run_id)
    first, second = text.find(f"ECHO:{HOLD}"), text.find(f"ECHO:{TWO}")
    assert 0 <= first < second, f"answers at {first} and {second} in {text!r}"


def test_turn_send_refuses_a_run_that_finished(
    door: SimpleNamespace,
) -> None:
    run_id = door.start("a quick turn")
    door.finish(run_id)
    sent = door.send(run_id, TWO)
    assert "unknown command" not in sent.stderr, f"dispatcher said {sent.stderr!r}"
    assert sent.returncode == 2, _said(sent)


def test_stdin_closes_when_the_child_folds_queued_messages_into_one_turn(
    door: SimpleNamespace,
) -> None:
    """The real client gives one result for the messages that arrive during one
    turn. The driver must not wait for a result for each message."""
    prompt = HOLD + " MERGE"
    run_id = door.start(prompt)
    door.wait(door.taken, "the child to take its prompt")
    first = door.send(run_id, ONE)
    second = door.send(run_id, TWO)
    assert first.returncode == 0, _said(first)
    assert second.returncode == 0, _said(second)
    door.release()
    door.finish(run_id)
    users = door.users()
    assert users == [prompt, ONE, TWO], f"the child read {users!r}"
    end = door.records("end.json")
    assert end == [{"eof": True}], f"stdin never closed: {end!r}"
