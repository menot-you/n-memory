"""`producer` on the wire: declared, carried to admission, part of the request.

A caller names the agent whose work a review or a judge witnesses, and nSPAWN
keeps the run off it. The name list reaches `review` and `execute`
as sent. It is part of a keyed request's print, and a request that names none
keeps the print it had before the field existed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mcp_fakes import FakeSupervisor, handle

from nspawn import driver
from nspawn.mcp import NSPAWN_TOOL_CONTRACTS, McpServer
from nspawn.supervisor import RunAccepted


def _schema(name: str) -> dict[str, Any]:
    return next(c.input_schema for c in NSPAWN_TOOL_CONTRACTS if c.name == name)


def _call(server: McpServer, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return handle(
        server,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )


def test_both_witness_verbs_declare_an_optional_producer_list() -> None:
    for name in ("nspawn_review_start", "nspawn_run_start"):
        schema = _schema(name)
        declared = schema["properties"]["producer"]
        assert declared["type"] == "array"
        assert declared["minItems"] == 1
        assert declared["items"] == {"type": "string", "minLength": 1}
        assert "producer" not in schema["required"]
        assert "provider" not in schema["required"]


def test_the_review_description_tells_the_caller_not_to_pin_a_reviewer() -> None:
    contract = next(c for c in NSPAWN_TOOL_CONTRACTS if c.name == "nspawn_review_start")
    assert "Do not pin `provider` or `model`" in contract.description


def test_a_review_carries_its_producer_to_admission() -> None:
    fake = FakeSupervisor(review_result=RunAccepted(run_id="review-1"))
    answer = _call(
        McpServer(supervisor=fake),
        "nspawn_review_start",
        {"branch": "lane/x", "cwd": "/lane", "producer": ["claude", "opus"]},
    )
    assert "error" not in answer, answer
    assert list(fake.calls[0][1]["producer"]) == ["claude", "opus"]  # type: ignore[call-overload]


def test_a_review_that_names_no_producer_reaches_admission_as_before() -> None:
    fake = FakeSupervisor(review_result=RunAccepted(run_id="review-1"))
    _call(
        McpServer(supervisor=fake),
        "nspawn_review_start",
        {"cwd": "/lane", "branch": "x"},
    )
    assert "producer" not in fake.calls[0][1]


def test_a_verification_run_carries_its_producer_to_admission() -> None:
    fake = FakeSupervisor(execute_result=RunAccepted(run_id="judge-1"))
    answer = _call(
        McpServer(supervisor=fake),
        "nspawn_run_start",
        {
            "prompt": "judge",
            "cwd": "/lane",
            "class": "verification",
            "read_only": True,
            "producer": ["MiniMax-M3"],
        },
    )
    assert "error" not in answer, answer
    assert list(fake.calls[0][1]["producer"]) == ["MiniMax-M3"]  # type: ignore[call-overload]


def test_the_producer_is_part_of_a_keyed_request(tmp_path: Path) -> None:
    def printed(**extra: object) -> str:
        fingerprint: str = driver.request_fingerprint(
            "nspawn_review_start",
            {"branch": "lane/x", "cwd": str(tmp_path), **extra},
            group=None,
        )
        return fingerprint

    before = printed()
    assert printed(producer=None) == before
    assert printed(producer=["claude", "opus"]) == printed(producer=["opus", "claude"])
    assert printed(producer=["claude"]) != before
    assert printed(producer=["claude"]) != printed(producer=["codex"])
