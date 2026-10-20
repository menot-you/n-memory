"""U1 consumer boundary: deterministic result and status harvest."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from mcp_fakes import handle

from nspawn.mcp import McpServer
from nspawn.rows import Backend, RunPhase, RunRow
from nspawn.supervisor import _to_status


class _ResultSupervisor:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def result(self, run_id: str) -> dict[str, str]:
        self.calls.append(run_id)
        return {"run_id": run_id, "final_text": "PONG"}


def _payload(answer: dict[str, Any] | None) -> Any:
    assert answer is not None
    return json.loads(answer["result"]["content"][0]["text"])


def test_nspawn_result_returns_persisted_text_without_a_transcript_read() -> None:
    supervisor = _ResultSupervisor()
    server = McpServer(supervisor=cast(Any, supervisor))

    answer = handle(
        server,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "nspawn_result_show", "arguments": {"run_id": "run-1"}},
        },
    )

    assert _payload(answer) == {"run_id": "run-1", "final_text": "PONG"}
    assert supervisor.calls == ["run-1"]


def test_status_projects_run_directory_failure_and_original_sequence(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "runs"
    run_dir = run_root / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "stdout").write_bytes(
        b'{"type":"assistant"}\n'
        b'{"type":"system","subtype":"thinking_tokens"}\n'
        b'{"type":"result"}\n'
    )
    row = RunRow(
        run_id="run-1",
        worktree="/worktree",
        phase=RunPhase.FAILED,
        sandbox="workspace-write",
        model="model",
        owner_instance="instance",
        created_at=1,
        updated_at=2,
        backend=Backend.CLAUDE,
        blocked_reason="child refused",
        records_seen=3,
    )

    status = _to_status(row, run_root)

    assert status.run_dir == str(run_dir)
    assert status.failure_reason == "child refused"
    assert status.total_seq == 3
