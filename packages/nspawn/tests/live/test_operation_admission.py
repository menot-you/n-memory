"""Explicit-target stdio observations; native writer qualification is separate.

This is not yet the complete M6 integrated consumer judge: native writer,
continuation/retry/drift and mutation qualification remain required.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from live.live_support import MEMBER_ROOT, McpStdio
from nspawn.operations import UNCHECKED_REASON


def _operations(**changes: Any) -> dict[str, Any]:
    return {
        "version": 1,
        "tools": [],
        "paths": [],
        "network": [],
        "hosts": [],
        "actions": [],
        **changes,
    }


def _payload(answer: dict[str, Any]) -> dict[str, Any]:
    assert "error" not in answer, answer
    result = answer["result"]
    assert result["isError"] is False, answer
    return json.loads(result["content"][0]["text"])


def _operation_target(tmp_path: Path) -> Path:
    selected = os.environ.get("NOTT_NSPAWN_OPERATION_TEST_BINARY")
    if selected is None:
        source = MEMBER_ROOT.parents[1] / ".venv" / "bin" / "nspawn"
        if not source.is_file() or not os.access(source, os.X_OK):
            pytest.fail(
                "operation judge needs the checkout entry point; run uv sync at the repository root"
            )
        candidate = tmp_path / "candidate"
        candidate.mkdir()
        target = candidate / "nspawn"
        shutil.copy2(source, target)
    else:
        target = Path(selected)
    if (
        not target.is_absolute()
        or not target.is_file()
        or not os.access(target, os.X_OK)
    ):
        pytest.fail("explicit operation judge target is invalid; no source fallback")
    return target


@pytest.fixture
def explicit_client(
    live_environment: dict[str, str],
    tmp_path: Path,
    record_property: Any,
) -> Iterator[McpStdio]:
    target = _operation_target(tmp_path)
    with target.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    record_property("nspawn_executable_sha256", digest)
    neutral = tmp_path / "neutral"
    neutral.mkdir()
    client = McpStdio(
        live_environment, executable=target, cwd=neutral, expected_sha256=digest
    )
    try:
        assert "result" in client.handshake()
        tools = client.request("tools/list")["result"]["tools"]
        for tool in tools:
            if tool["name"] in {
                "nspawn_run_start",
                "nspawn_review_start",
                "nspawn_batch_start",
            }:
                assert "operations" in tool["inputSchema"]["properties"], (
                    "target lacks operation admission"
                )
        yield client
    finally:
        client.close()


@pytest.mark.parametrize("contract", [None, _operations()])
def test_stdio_unchecked_worker_evidence_survives_close(
    explicit_client: McpStdio,
    tmp_path: Path,
    contract: dict[str, Any] | None,
) -> None:
    arguments: dict[str, Any] = {
        "prompt": "Return a bounded fixture answer.",
        "cwd": str(tmp_path),
        "class": "mechanical_fanout",
        "provider": "fixture",
        "read_only": True,
    }
    if contract is not None:
        arguments["operations"] = contract
    accepted = _payload(explicit_client.call_tool("nspawn_run_start", arguments))
    assert "operation_admission" not in accepted
    run_id = accepted["run_id"]
    shown = _payload(
        explicit_client.call_tool(
            "nspawn_run_show", {"run_id": run_id, "timeout_ms": 20_000}
        )
    )
    assert shown["phase"] == "completed", shown
    expected = {"checked": False, "reason": UNCHECKED_REASON}
    assert shown["operation_admission"] == expected
    assert UNCHECKED_REASON not in (shown.get("warning") or "")
    closed = _payload(explicit_client.call_tool("nspawn_run_close", {"run_id": run_id}))
    assert closed["claim_released"] is True
    assert not closed["tree"]["survivors"]
    for _ in range(2):
        shown = _payload(
            explicit_client.call_tool("nspawn_run_show", {"run_id": run_id})
        )
        assert shown["operation_admission"] == expected
