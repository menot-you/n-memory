"""A real Claude writer, started by this checkout's nSPAWN with the real roster.

These tests spend a few cents on the cheapest Claude model of the roster. They
skip unless `NOTT_NSPAWN_LIVE_CLAUDE_ROOT` names a `[native_write]` root of the
roster and `NOTT_NSPAWN_LIVE_CLAUDE_ROSTER` names the roster. The suite replaces
`NOTT_NSPAWN_ROSTER` for every test, so these tests read the roster from their own
variable. Each test closes its run.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from live.live_support import MEMBER_ROOT, McpStdio

ROOT_ENV = "NOTT_NSPAWN_LIVE_CLAUDE_ROOT"
ROSTER_ENV = "NOTT_NSPAWN_LIVE_CLAUDE_ROSTER"
MODEL = "claude-haiku-5-5"
TERMINAL = ("completed", "failed", "closed", "orphaned")
DEADLINE_MS = 170_000

pytestmark = pytest.mark.skipif(
    not os.environ.get(ROOT_ENV) or not os.environ.get(ROSTER_ENV),
    reason=f"{ROOT_ENV} and {ROSTER_ENV} name no live Claude writer root",
)


def _frames(run_directory: Path) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    for line in (run_directory / "stdout").read_text("utf-8", "replace").split("\n"):
        try:
            frame = json.loads(line)
        except ValueError:
            continue
        if isinstance(frame, dict):
            frames.append(frame)
    return frames


def _results(frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [frame for frame in frames if frame.get("type") == "result"]


def _tool_output(frames: list[dict[str, Any]]) -> str:
    texts: list[str] = []
    for frame in frames:
        message = frame.get("message")
        if frame.get("type") != "user" or not isinstance(message, dict):
            continue
        content = message.get("content")
        for block in content if isinstance(content, list) else []:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                texts.append(str(block.get("content")))
    return "\n".join(texts)


@pytest.fixture
def writer(tmp_path: Path) -> Iterator[SimpleNamespace]:
    """A standalone clone under the live root and a served nSPAWN that the
    caller's TMPDIR does not reach."""
    held = Path(
        tempfile.mkdtemp(prefix="nspawn-live-writer-", dir=os.environ[ROOT_ENV])
    )
    clone = held / "clone"
    clone.mkdir()
    git = ["git", "-C", str(clone), "-c", "user.email=t@t.invalid", "-c", "user.name=T"]
    subprocess.run([*git, "init", "-q"], check=True)
    (clone / "README.md").write_text("# fixture\n", encoding="utf-8")
    subprocess.run([*git, "add", "README.md"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "init"], check=True)
    environment = {
        name: value
        for name, value in os.environ.items()
        if name not in ("TMPDIR", "NOTT_NSPAWN_CONFIG", "XDG_CONFIG_HOME")
    }
    environment["NOTT_NSPAWN_ROSTER"] = os.environ[ROSTER_ENV]
    environment["NOTT_NSPAWN_RUN_ROOT"] = str(tmp_path / "runs")
    client = McpStdio(environment)
    client.handshake()
    started: list[str] = []

    def call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        answer = client.call_tool(name, arguments)
        assert "error" not in answer, f"{name} answered {answer!r}"
        payload: dict[str, Any] = json.loads(answer["result"]["content"][0]["text"])
        return payload

    def start(prompt: str) -> str:
        arguments = {
            "prompt": prompt,
            "cwd": str(clone),
            "native_write": True,
            "class": "verification",
            "provider": "claude",
            "model": MODEL,
            "notice": False,
            "timeout_ms": DEADLINE_MS,
        }
        run_id: str = call("nspawn_run_start", arguments)["run_id"]
        started.append(run_id)
        return run_id

    def show(run_id: str) -> dict[str, Any]:
        return call("nspawn_run_show", {"run_id": run_id})

    def finish(run_id: str) -> dict[str, Any]:
        stop = time.monotonic() + DEADLINE_MS / 1000 + 30
        shown = show(run_id)
        while shown["phase"] not in TERMINAL and time.monotonic() < stop:
            time.sleep(1)
            shown = show(run_id)
        assert shown["phase"] in TERMINAL, f"run {run_id} was {shown['phase']!r}"
        return shown

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

    try:
        yield SimpleNamespace(start=start, show=show, finish=finish, send=send)
    finally:
        for run_id in started:
            call("nspawn_run_close", {"run_id": run_id})
        client.close()
        shutil.rmtree(held, ignore_errors=True)


def test_writer_loads_no_nspawn_mcp_server_and_a_tmpdir_under_var_tmp(
    writer: SimpleNamespace,
) -> None:
    run_id = writer.start(
        'Use the Bash tool to run exactly: python3 -c "import tempfile; '
        'print(tempfile.gettempdir())"  Then reply with only the path it printed.'
    )
    shown = writer.finish(run_id)
    assert shown["phase"] == "completed", shown.get("failure_reason")
    frames = _frames(Path(shown["run_dir"]))
    inits = [
        frame
        for frame in frames
        if frame.get("type") == "system" and frame.get("subtype") == "init"
    ]
    assert len(inits) == 1, f"the child gave {len(inits)} init frames"
    servers = [str(server.get("name")) for server in inits[0].get("mcp_servers", [])]
    assert servers, "the init frame lists no MCP server, so it proves nothing"
    assert not [name for name in servers if re.search(r"(^|:)nspawn$", name)], servers
    printed = _tool_output(frames).strip()
    assert Path(printed).is_relative_to("/var/tmp"), (
        f"the writer's tempfile.gettempdir() is {printed!r}"
    )


def test_stdin_closes_when_the_real_client_folds_two_messages_into_one_turn(
    writer: SimpleNamespace,
) -> None:
    run_id = writer.start(
        "Use the Bash tool to run exactly: sleep 20   "
        "Then reply with the single word FIRST-DONE."
    )
    deadline = time.monotonic() + 60
    while writer.show(run_id)["phase"] != "running":
        assert time.monotonic() < deadline, "the run never started its child"
        time.sleep(1)
    time.sleep(6)
    for text in ("Also say the word SECOND-SEEN.", "Also say the word THIRD-SEEN."):
        sent = writer.send(run_id, text)
        assert sent.returncode == 0, sent.stderr
    shown = writer.finish(run_id)
    assert shown["phase"] == "completed", (
        f"the run ended {shown['phase']!r}: {shown.get('failure_reason')!r}"
    )
    results = _results(_frames(Path(shown["run_dir"])))
    answered = "\n".join(str(result.get("result")) for result in results)
    assert "SECOND-SEEN" in answered and "THIRD-SEEN" in answered, answered
