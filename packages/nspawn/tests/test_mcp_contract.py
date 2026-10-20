"""The wire contract every client actually reads: `CallToolResult`.

The whole L6 battery asserts on the dictionary `mcp.py` builds internally, and
that is why a real regression got through it green: the dispatch path stopped
packaging its answer in the `content` envelope the MCP spec requires, so every
verb went silent for a conforming client while the internal dictionary stayed
perfect. This file asserts on what LEAVES the process, once, for every declared
verb — so the class cannot come back one tool at a time.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from mcp_fakes import FakeSupervisor, handle

from nspawn.mcp import EXACTLY_ONE_OF, NSPAWN_TOOL_CONTRACTS, McpServer
from nspawn.supervisor import (
    CloseReadback,
    ProcessTreeReadback,
    ProviderReport,
    QuotaReport,
    RunAccepted,
    RunPhase,
    RunResult,
    RunStatus,
    TranscriptPage,
)

#: One successful answer per verb, so the double never raises and the envelope
#: is the only thing under test.
SUCCESSES = {
    "execute_result": RunAccepted(run_id="run-1"),
    "review_result": RunAccepted(run_id="run-1"),
    "status_result": RunStatus(
        run_id="run-1",
        phase=RunPhase.COMPLETED,
        run_dir="/runs/run-1",
        failure_reason=None,
        total_seq=1,
        requested_provider=None,
        executed_provider="claude",
        executed_account=None,
        warning=None,
        tokens=7,
        marker="DONE",
    ),
    "result_result": RunResult(run_id="run-1", final_text="PONG"),
    "transcript_result": TranscriptPage(
        run_id="run-1", since_seq=0, next_seq=1, lines=("a",), truncated=False
    ),
    "close_result": CloseReadback(
        run_id="run-1",
        phase=RunPhase.CLOSED,
        tree=ProcessTreeReadback(checked=(), survivors=()),
        claim_released=True,
    ),
    "quota_result": QuotaReport(observed_at=1, providers=()),
    "providers_result": ProviderReport(providers=()),
    "fleet_result": {"observed_at": 1, "runs": []},
    "usage_result": {"timeZone": "UTC", "agents": []},
}


def minimal_arguments(schema: dict[str, Any]) -> dict[str, Any]:
    """The smallest argument object the contract's own schema accepts."""
    properties = schema.get("properties", {})
    arguments: dict[str, Any] = {}
    for name in [*schema.get("required", []), *schema.get(EXACTLY_ONE_OF, [])[:1]]:
        arguments[name] = (
            "UTC" if name == "timeZone" else _minimal_value(properties.get(name, {}))
        )
    for condition in schema.get("allOf", []):
        if "if" in condition and Draft202012Validator(condition["if"]).is_valid(
            arguments
        ):
            for name in condition.get("then", {}).get("required", []):
                arguments.setdefault(name, _minimal_value(properties[name]))
    return arguments


def _minimal_value(declared: dict[str, Any]) -> Any:
    """The smallest value one declared property accepts.

    It derives from the schema rather than from a table, so a verb that adds
    an argument is exercised without anyone remembering to teach this helper —
    which is the whole reason F-023 went unseen for as long as it did.
    """
    kind = declared.get("type", "string")
    if kind == "object":
        # An object without `minProperties` accepts `{}`; with it, the generator must
        # supply as many entries as schema requires. Before this the generator
        # returned string "x" for an object, and the three typed verbs
        # rejected with -32602 -- read as verb defect, but was the harness.
        forma = declared.get("additionalProperties")
        valor = _minimal_value(forma if isinstance(forma, dict) else {})
        quantas = max(0, int(declared.get("minProperties", 0)))
        return {f"q{indice}": valor for indice in range(quantas)}
    if kind == "integer":
        return 1
    if kind == "boolean":
        return False
    if kind == "array":
        items = declared.get("items", {"type": "string"})
        return [_minimal_value(items)] * max(1, int(declared.get("minItems", 1)))
    enum = declared.get("enum")
    return enum[0] if enum else "x"


@pytest.fixture(autouse=True)
def _system1_duplo(monkeypatch: Any) -> None:
    """The three typed verbs talk to System1; a contract test does NOT.

    Without this, this file made a paid call per run and per verb, and the
    verdict depended on provider availability -- a 422 was read as "the verb
    responds outside the envelope". What is judged here is the RESPONSE ENVELOPE.
    """
    from nspawn import system1

    def responde(
        state: Any, questions: dict[str, Any], model: Any = None
    ) -> dict[str, Any]:
        return {
            ident: system1.Answer(
                id=ident,
                type=(pergunta or {}).get("type", "noul")
                if isinstance(pergunta, dict)
                else "noul",
                value=0.5,
                probabilities=None,
                confidence=None,
                legend=None,
            )
            for ident, pergunta in questions.items()
        }

    monkeypatch.setattr(system1, "ask", responde)


@pytest.mark.parametrize("contract", NSPAWN_TOOL_CONTRACTS, ids=lambda c: c.name)
def test_every_verb_answers_in_the_content_envelope(contract: Any) -> None:
    server = McpServer(supervisor=FakeSupervisor(**SUCCESSES))
    arguments = minimal_arguments(contract.input_schema)
    if contract.name == "nspawn_review_start":
        # The minimal `cwd` is no git worktree, so it has no branch to default to.
        arguments["branch"] = "x"
    answer = handle(
        server,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": contract.name,
                "arguments": arguments,
            },
        },
    )

    assert answer is not None, f"{contract.name} answered no frame at all"
    assert "error" not in answer, f"{contract.name} refused: {answer.get('error')}"
    result = answer["result"]

    # The spec's CallToolResult: content is REQUIRED, and a conforming client
    # renders nothing without it. A payload at the top level of `result` is
    # invisible no matter how correct it is.
    assert "content" in result, (
        f"{contract.name} answered without `content` — a conforming MCP client "
        f"renders this as empty. Top-level keys were: {sorted(result)}"
    )
    assert result["content"], f"{contract.name} answered an empty content list"

    block = result["content"][0]
    assert block["type"] == "text"
    # The structured answer survives the envelope: the text IS the payload.
    assert isinstance(json.loads(block["text"]), (dict, list))
