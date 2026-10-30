from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fixture_data import synthetic_token
from supervisor_support import (
    _admit_pending,
    _drain_worker_threads,
    _stop_every_reaper,
    _sv,
    registry,
)

from nspawn import claude_child, cli, picker, supervisor
from nspawn.owner_roster import Defaults, Provider, Roster
from nspawn.registry import Registry
from nspawn.rows import CLAUDE_CONFINEMENT_POLICY_ONLY, Backend, RunPhase, RunRow
from nspawn.supervisor import RunDirectory, Supervisor

# Export imported fixtures so pytest discovers them in this module.
__all__ = ["_drain_worker_threads", "_stop_every_reaper", "registry"]

ENDED = "steer-ended"
ABSENT = "steer-absent"
PROMPT = 'say "ping" ✓\nsecond line\twith a tab'

# A stand-in claude. It logs next to itself what it saw: its argv, each message
# on stdin, whether stdin was closed when it asked, and how stdin ended. A prompt
# with the word HOLD keeps its first turn open until a file named release exists.
# A prompt with the word MERGE folds the messages that wait into its own turn.
STEER_CLAUDE = r"""
import json
import os
import select
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
stream = any(
    item == "--input-format" or item.startswith("--input-format=")
    for item in sys.argv[1:]
)
buffer = bytearray()


def record(name, value):
    with (here / name).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value) + "\n")


def pump(seconds):
    ready, _, _ = select.select([0], [], [], max(0.0, seconds))
    if not ready:
        return "idle"
    chunk = os.read(0, 65536)
    if not chunk:
        return "eof"
    buffer.extend(chunk)
    return "data"


def take(seconds):
    deadline = time.monotonic() + seconds
    while True:
        end = buffer.find(b"\n")
        if end >= 0:
            line = bytes(buffer[: end + 1])
            del buffer[: end + 1]
            return line
        state = pump(deadline - time.monotonic())
        if state == "eof":
            line = bytes(buffer)
            buffer.clear()
            return line
        if state == "idle":
            return None


def closed_within(seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        state = pump(deadline - time.monotonic())
        if state == "eof":
            return True
        if state == "idle":
            break
    return False


def classify(line):
    text = line.decode("utf-8", "replace")
    try:
        frame = json.loads(text)
    except ValueError:
        return "raw", text
    message = frame.get("message") if isinstance(frame, dict) else None
    if not isinstance(message, dict) or frame.get("type") != "user":
        return "other", text
    content = message.get("content")
    if isinstance(content, list):
        content = "".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    if message.get("role") == "user" and isinstance(content, str):
        return "user", content
    return "other", text


def answer(kind, text, first):
    record("stdin.log", {"kind": kind, "text": text})
    if "HOLD" in text:
        deadline = time.monotonic() + 60
        while not (here / "release").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
    if "MERGE" in text:
        # Like the real client: the messages that arrived during this turn
        # join it, and the turn gives one result for all of them.
        while True:
            more = take(0.5)
            if not more:
                break
            folded_kind, folded = classify(more)
            record("stdin.log", {"kind": folded_kind, "text": folded})
    if first:
        record("early.json", {"eof": closed_within(0.3)})
    reply = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "terminal_reason": "completed",
        "result": "ECHO:" + text[:200],
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    sys.stdout.write(json.dumps(reply) + "\n")
    sys.stdout.flush()


record("argv.json", sys.argv[1:])
if not stream:
    while pump(60) == "data":
        pass
    prompt = bytes(buffer).decode("utf-8", "replace")
    buffer.clear()
    if not prompt:
        raise SystemExit(1)
    answer("raw", prompt, True)
    record("end.json", {"eof": closed_within(1)})
    raise SystemExit(0)
turns = 0
ended = None
while ended is None:
    line = take(60 if turns == 0 else 15)
    if line:
        kind, text = classify(line)
        if kind == "user":
            answer(kind, text, turns == 0)
            turns += 1
        else:
            record("stdin.log", {"kind": kind, "text": text})
    else:
        ended = line == b""
record("end.json", {"eof": ended})
"""


def _stand_in(directory: Path) -> Path:
    directory.mkdir(parents=True)
    binary = directory / "claude"
    binary.write_text(f"#!{sys.executable}\n{STEER_CLAUDE}", encoding="utf-8")
    binary.chmod(0o755)
    return binary


def _records(directory: Path, name: str) -> list[Any]:
    """The complete JSON lines of one log that the stand-in keeps."""
    path = directory / name
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    return [json.loads(line) for line in text.split("\n")[:-1]]


def _argv(directory: Path) -> list[str]:
    records = _records(directory, "argv.json")
    assert len(records) == 1, f"the child started {len(records)} times, not once"
    argv: list[str] = records[0]
    return argv


def _flag(argv: list[str], name: str) -> list[str]:
    """Every value that argv gives name, as name value or as name=value."""
    spaced = [argv[i + 1] for i, item in enumerate(argv[:-1]) if item == name]
    joined = [item.split("=", 1)[1] for item in argv if item.startswith(name + "=")]
    return spaced + joined


def _plugin(root: Path) -> Path:
    """A plugin directory that declares nmemory beside another server."""
    (root / "skills").mkdir(parents=True)
    servers = {"nmemory": {"command": "m"}, "other": {"command": "other"}}
    config = root / claude_child.MCP_CONFIG_FILE
    config.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
    return root


def _route(tmp_path: Path, binary: Path) -> tuple[Roster, picker.PickerResult]:
    """One claude route that runs the binary, with a plugin that declares nmemory."""
    token = tmp_path / "route.token"
    token.write_text(synthetic_token("test_steer_channel:1"), encoding="utf-8")
    token.chmod(0o600)
    route = Provider(
        name="route",
        bin=binary,
        token_file=token,
        model="m",
        models=["m"],
    )
    defaults = Defaults(plugin_root=_plugin(tmp_path / "plugin"))
    roster = Roster(defaults=defaults, providers=[route])
    picked = picker.PickerResult(
        requested_provider=None,
        executed_provider=route,
        executed_account=None,
        model="m",
        effort=None,
    )
    return roster, picked


def _admitted(
    driven: Supervisor, registry: Registry, tmp_path: Path
) -> tuple[str, str, RunDirectory, Path]:
    cwd = tmp_path / "work"
    cwd.mkdir(exist_ok=True)
    run_id, thread_id = _admit_pending(driven, registry, tmp_path, worktree=str(cwd))
    root = driven._run_root_path / run_id
    run_dir = RunDirectory(root=root, stdout=root / "stdout", stderr=root / "stderr")
    return run_id, thread_id, run_dir, cwd


def _turn(
    driven: Supervisor,
    registry: Registry,
    tmp_path: Path,
    prompt: str,
    *,
    resume: bool = False,
    read_only: bool = False,
) -> Path:
    """Run one turn of the stand-in child. Return the directory it logs in."""
    binary = _stand_in(tmp_path / "bin")
    roster, picked = _route(tmp_path, binary)
    run_id, thread_id, run_dir, cwd = _admitted(driven, registry, tmp_path)
    driven._spawn_and_wait(
        run_id,
        str(cwd),
        run_dir,
        prompt,
        roster,
        picked,
        thread_id,
        resume,
        read_only,
    )
    return binary.parent


def _ended_run(registry: Registry, root: Path) -> None:
    """A run that the store holds as finished, with the directory a run keeps."""
    (root / ENDED).mkdir(mode=0o700)
    row = RunRow(
        run_id=ENDED,
        thread_id=ENDED,
        worktree=str(root.parent),
        phase=RunPhase.COMPLETED,
        sandbox=CLAUDE_CONFINEMENT_POLICY_ONLY,
        model="m",
        owner_instance=registry.instance_id,
        created_at=1,
        updated_at=2,
        backend=Backend.CLAUDE,
        executed_provider="route",
    )
    registry.insert_run(row)


@pytest.fixture(autouse=True)
def _no_claude_bin_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """The operator's own override would outrank the stand-in each test declares."""
    monkeypatch.delenv(supervisor.CLAUDE_BIN_ENV, raising=False)


@pytest.fixture
def driven(registry: Registry, tmp_path: Path) -> Iterator[Supervisor]:
    instance = _sv(registry, tmp_path)
    try:
        yield instance
    finally:
        instance.shutdown()


@pytest.fixture
def run_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "runs"
    root.mkdir(exist_ok=True)
    monkeypatch.setenv("NOTT_NSPAWN_RUN_ROOT", str(root))
    return root


def test_a_fresh_child_starts_with_stream_json_input(
    driven: Supervisor,
    registry: Registry,
    tmp_path: Path,
) -> None:
    argv = _argv(_turn(driven, registry, tmp_path, "ping"))
    values = _flag(argv, "--input-format")
    assert values == ["stream-json"], f"--input-format gave {values!r} in {argv!r}"


def test_a_resumed_child_starts_with_stream_json_input(
    driven: Supervisor,
    registry: Registry,
    tmp_path: Path,
) -> None:
    argv = _argv(_turn(driven, registry, tmp_path, "ping", resume=True))
    resumed = [item for item in argv if item.startswith("--resume=")]
    assert resumed, f"the child did not resume: {argv!r}"
    values = _flag(argv, "--input-format")
    assert values == ["stream-json"], f"--input-format gave {values!r} in {argv!r}"


def test_a_read_only_child_starts_with_stream_json_input(
    driven: Supervisor,
    registry: Registry,
    tmp_path: Path,
) -> None:
    argv = _argv(_turn(driven, registry, tmp_path, "ping", read_only=True))
    values = _flag(argv, "--input-format")
    assert values == ["stream-json"], f"--input-format gave {values!r} in {argv!r}"


def test_the_prompt_is_the_first_user_message(
    driven: Supervisor,
    registry: Registry,
    tmp_path: Path,
) -> None:
    prompt = PROMPT + "x" * 100_000
    records = _records(_turn(driven, registry, tmp_path, prompt), "stdin.log")
    inputs = [item for item in records if item["kind"] != "other"]
    assert inputs, "the child took no prompt"
    kind = inputs[0]["kind"]
    assert kind == "user", f"the prompt reached the child as a {kind!r} input"
    assert inputs[0]["text"] == prompt, "the prompt text changed in transit"


def test_stdin_stays_open_until_the_child_gives_its_result(
    driven: Supervisor,
    registry: Registry,
    tmp_path: Path,
) -> None:
    directory = _turn(driven, registry, tmp_path, "ping")
    early = _records(directory, "early.json")
    assert early == [{"eof": False}], f"stdin closed before the result: {early!r}"


def test_turn_send_refuses_a_run_that_has_ended(
    run_root: Path,
    registry: Registry,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _ended_run(registry, run_root)
    status = cli.main(["turn", "send", ENDED, "keep going"])
    err = capsys.readouterr().err
    assert "unknown command" not in err, f"the dispatcher refused it: {err!r}"
    assert status == 2, f"a run that ended must give exit 2, not {status}: {err!r}"


def test_turn_send_refuses_a_run_the_store_never_saw(
    run_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    status = cli.main(["turn", "send", ABSENT, "keep going"])
    err = capsys.readouterr().err
    assert "unknown command" not in err, f"the dispatcher refused it: {err!r}"
    assert status == 2, f"a run the store never saw must give exit 2, not {status}"
