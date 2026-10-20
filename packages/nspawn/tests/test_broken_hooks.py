"""U1-hooks: a hook of the child that exits outside {0, 2} reaches the caller.

0 is allow and 2 is block — the two codes the client itself acts on. Every other
exit is a BROKEN hook, and today it dies inside the child's stdout with nobody
reading it. The operator has to see it on the run's result.
"""

from __future__ import annotations

import json
from pathlib import Path

from fixture_data import wire_fixture

from nspawn.registry import RunPhase
from nspawn.terminal import classify_terminal_run

FIXTURE = wire_fixture("claude-stream-wire-2.1.251.ndjson")


def _hook(name: str, exit_code: int, stderr: str = "") -> bytes:
    """The shape the client emits (fixtures/claude-stream-wire-2.1.251.ndjson:16)."""
    return json.dumps(
        {
            "type": "system",
            "subtype": "hook_response",
            "hook_id": "6b5c1da2-1760-4a9a-b603-1d4f0cb91ab0",
            "hook_name": name,
            "hook_event": name.split(":")[0],
            "output": "",
            "stdout": "",
            "stderr": stderr,
            "exit_code": exit_code,
            "outcome": "success" if exit_code == 0 else "failure",
            "uuid": "210bea20-4133-4356-b096-e4d028f4f4c2",
            "session_id": "07969048-012e-4d0b-ab9c-f27d3e499e0b",
        }
    ).encode("utf-8")


def _wire(*extra: bytes) -> bytes:
    lines = [
        line for line in FIXTURE.read_bytes().splitlines() if line.startswith(b"{")
    ]
    # Before the terminal `result` frame, where the client emits its hooks.
    return b"\n".join([*lines[:-1], *extra, lines[-1]]) + b"\n"


def _classify(tmp_path: Path, stdout_value: bytes, exit_code: int = 0):
    stdout = tmp_path / "stdout"
    stderr = tmp_path / "stderr"
    stdout.write_bytes(stdout_value)
    stderr.write_bytes(b"")
    return classify_terminal_run(
        stdout,
        stderr,
        exit_code,
        provider="claude",
        account_alias="owner-primary",
        now=1787984400,
    )


def test_hook_exit_one_surfaces(tmp_path: Path) -> None:
    result = _classify(tmp_path, _wire(_hook("PreToolUse:Bash", 1, "red-first: no")))
    assert result.phase is RunPhase.COMPLETED
    assert len(result.broken_hooks) == 1
    broken = result.broken_hooks[0]
    assert broken.hook_name == "PreToolUse:Bash"
    assert broken.exit_code == 1
    assert "red-first" in (broken.detail or "")


def test_allow_and_block_are_not_broken(tmp_path: Path) -> None:
    result = _classify(
        tmp_path, _wire(_hook("PreToolUse:Bash", 0), _hook("PreToolUse:Write", 2))
    )
    assert result.broken_hooks == ()


def test_a_broken_hook_survives_a_failed_run(tmp_path: Path) -> None:
    """No terminal `result` frame and a non-zero exit: the run failed, and the
    broken hook is exactly the evidence the operator needs to read there."""
    lines = [
        line for line in FIXTURE.read_bytes().splitlines() if line.startswith(b"{")
    ]
    without_result = b"\n".join([*lines[:-1], _hook("Stop:final-state", 127)]) + b"\n"
    result = _classify(tmp_path, without_result, exit_code=1)
    assert result.phase is RunPhase.FAILED
    assert [h.exit_code for h in result.broken_hooks] == [127]


def test_a_clean_wire_reports_no_broken_hook(tmp_path: Path) -> None:
    assert _classify(tmp_path, _wire()).broken_hooks == ()
