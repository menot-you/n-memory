"""U2: a transcript keeps useful frames without moving raw cursors."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from mcp_fakes import handle

from nspawn import main
from nspawn.mcp import INVALID_PARAMS, McpServer
from nspawn.supervisor import Supervisor

RUN_ID = "run-1"


class _KnownRunRegistry:
    def get_run(self, run_id: str) -> object | None:
        return object() if run_id == RUN_ID else None

    def remove_instance(self) -> None:
        pass


@pytest.fixture
def supervisor(tmp_path: Path) -> Iterator[Supervisor]:
    """A real Supervisor starts its reaper and opens the run root; only
    `shutdown` stops one and closes the other."""
    supervisor = Supervisor(cast(Any, _KnownRunRegistry()), tmp_path / "runs")
    yield supervisor
    supervisor.shutdown()


def _server(
    supervisor: Supervisor, tmp_path: Path, lines: list[str]
) -> tuple[McpServer, Path, bytes]:
    stdout = tmp_path / "runs" / RUN_ID / "stdout"
    stdout.parent.mkdir()
    raw = ("\n".join(lines) + "\n").encode()
    stdout.write_bytes(raw)
    return McpServer(supervisor=supervisor), stdout, raw


def _call(server: McpServer, arguments: dict[str, Any]) -> dict[str, Any]:
    answer = handle(
        server,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "nspawn_transcript_show", "arguments": arguments},
        },
    )
    assert answer is not None
    assert "error" not in answer, answer.get("error")
    return cast(dict[str, Any], json.loads(answer["result"]["content"][0]["text"]))


def _frame(frame_type: str, **fields: object) -> str:
    return json.dumps({"type": frame_type, **fields}, separators=(",", ":"))


def test_default_omits_only_thinking_telemetry_and_preserves_raw_bytes(
    supervisor: Supervisor, tmp_path: Path
) -> None:
    thinking = _frame("system", subtype="thinking_tokens", tokens="noise " * 10_000)
    assistant = _frame("assistant", message="useful answer")
    status = _frame("system", subtype="status", status="running")
    malformed = '{"type":"system","subtype":"thinking_tokens"'
    stream_event = _frame("stream_event", event={"type": "message_stop"})
    result = _frame("result", result="PONG")
    lines = [thinking, assistant, status, malformed, stream_event, result]
    server, stdout, raw = _server(supervisor, tmp_path, lines)

    page = _call(server, {"run_id": RUN_ID})

    assert page["lines"] == [assistant, status, malformed, stream_event, result]
    assert page["next_seq"] == len(lines)
    assert page["truncated"] is False
    assert sum(len(line.split()) for line in page["lines"]) * 10 < len(
        raw.decode().split()
    )
    assert stdout.read_bytes() == raw


def test_explicit_frame_types_paginate_in_raw_stdout_coordinates(
    supervisor: Supervisor, tmp_path: Path
) -> None:
    thinking_1 = _frame("system", subtype="thinking_tokens", tokens="first")
    assistant_1 = _frame("assistant", message="one")
    malformed = '{"type":"assistant"'
    status = _frame("system", subtype="status")
    result = _frame("result", result="two")
    thinking_2 = _frame("system", subtype="thinking_tokens", tokens="second")
    assistant_2 = _frame("assistant", message="three")
    lines = [
        thinking_1,
        assistant_1,
        malformed,
        status,
        result,
        thinking_2,
        assistant_2,
    ]
    server, _, _ = _server(supervisor, tmp_path, lines)
    arguments: dict[str, Any] = {
        "run_id": RUN_ID,
        "frame_types": ["assistant", "result"],
        "max_bytes": len(assistant_1.encode()) + 1,
    }

    first = _call(server, arguments)
    second = _call(server, {**arguments, "since_seq": first["next_seq"]})
    third = _call(server, {**arguments, "since_seq": second["next_seq"]})

    assert [first["next_seq"], second["next_seq"], third["next_seq"]] == [
        4,
        6,
        7,
    ]
    assert first["lines"] + second["lines"] + third["lines"] == [
        assistant_1,
        result,
        assistant_2,
    ]
    assert [first["truncated"], second["truncated"], third["truncated"]] == [
        True,
        True,
        False,
    ]

    system = _call(
        server,
        {"run_id": RUN_ID, "frame_types": ["system"], "max_bytes": 4096},
    )
    assert system["lines"] == [thinking_1, status, thinking_2]
    assert system["next_seq"] == len(lines)


def test_frame_type_selection_is_declared_and_bounded(
    supervisor: Supervisor, tmp_path: Path
) -> None:
    server, _, _ = _server(supervisor, tmp_path, [_frame("result", result="PONG")])
    transcript = next(
        tool for tool in main.TOOLS if tool.name == "nspawn_transcript_show"
    )
    selection = transcript.input_schema["properties"]["frame_types"]
    assert selection["maxItems"] == Supervisor.MAX_TRANSCRIPT_FRAME_TYPES

    refused = handle(
        server,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "nspawn_transcript_show",
                "arguments": {
                    "run_id": RUN_ID,
                    "frame_types": [f"type-{index}" for index in range(17)],
                },
            },
        },
    )
    assert refused is not None
    assert refused["error"]["code"] == INVALID_PARAMS
    assert "maxItems: 16" in refused["error"]["message"]
