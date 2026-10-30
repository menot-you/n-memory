"""A refusal names a tool the server serves, and its advice works.

Every `nmemory_*` name in a refusal is a tool in `tools/list`. The DoD verdict
gate and the dod_of shape gate tell the agent to call `nmemory_kind_classify`.
That tool sets the kind and the observation of a stored capsule, and it does not
run the capture gates. Thus its advice also works for a row that predates the
English capture gate: the capture route refuses that text, so a second capture
cannot reach the row. The context source status tells the agent to run the
`nmemory index build` command.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nmemory.capsule import (
    AuthorityClass,
    Capsule,
    Freshness,
    Provenance,
    Scope,
    sha256_hex,
)
from nmemory.server import MemoryServer
from nmemory.store import Store

NOW = datetime(2026, 10, 27, tzinfo=UTC)
TOOL_NAME = re.compile(r"\bnmemory_[a-z_]+\b")
VECTORS = {
    v["id"]: v
    for v in map(
        json.loads,
        (Path(__file__).parent / "fixtures" / "language_vectors.jsonl")
        .read_text(encoding="utf-8")
        .splitlines(),
    )
}
#: Text that the capture language gate refuses. A live store holds rows like it
#: from before that gate.
PRE_GATE_TEXT = VECTORS["pt-plain"]["text"]
assert VECTORS["pt-plain"]["expect"] == "refused"


def _server() -> MemoryServer:
    return MemoryServer(store=Store.open_disposable_test_store())


def _payload(response: dict[str, Any]) -> dict[str, Any]:
    assert "result" in response and not response["result"]["isError"], response
    payload: dict[str, Any] = json.loads(response["result"]["content"][0]["text"])
    return payload


def _named_tools(server: MemoryServer, refused: dict[str, Any]) -> set[str]:
    """The tools that a refusal names. Each one must be a served tool."""
    named = set(TOOL_NAME.findall(json.dumps(refused)))
    served = {tool["name"] for tool in server.get_tool_definitions()}
    assert named <= served, (named - served, refused)
    return named


def _seed_pre_gate(server: MemoryServer, kind: str | None) -> str:
    """Write a row the way the store held it before the capture gates."""
    cid, created = server.store.append(
        Capsule(
            content=PRE_GATE_TEXT,
            provenance=Provenance(
                source="session:pre-gate",
                anchor="tests/test_refusals_name_served_tools.py:1",
                source_hash=sha256_hex(PRE_GATE_TEXT),
            ),
            confidence=0.9,
            freshness=Freshness(valid_from=NOW),
            scope=Scope(project_id="nott"),
            authority_class=AuthorityClass.OBSERVED_FACT,
            instruction_taint=False,
        ),
        NOW,
        kind=kind,
    )
    assert created
    return cid


def _capture(server: MemoryServer, content: str, kind: str) -> str:
    args = {
        "content": content,
        "source": "session:test",
        "anchor": "tests/test_refusals_name_served_tools.py:1",
        "kind": kind,
    }
    response = server.call_tool("nmemory_capsule_ingest", args, now=NOW)
    return str(_payload(response)["outcomes"][0]["id"])


def _dod_set(server: MemoryServer, **args: Any) -> dict[str, Any]:
    return server.call_tool("nmemory_dod_set", {"actor": "owner", **args}, now=NOW)


def test_the_dod_verdict_refusal_names_a_served_tool_and_its_advice_works() -> None:
    server = _server()
    dod = _seed_pre_gate(server, "dod")
    _payload(_dod_set(server, capsule_id=dod, status="accepted"))

    refused = _dod_set(server, capsule_id=dod, verdict="met", reason="it printed")
    assert "nmemory_kind_classify" in _named_tools(server, refused), refused

    recapture = server.call_tool(
        "nmemory_capsule_ingest",
        {
            "content": PRE_GATE_TEXT,
            "source": "session:test",
            "anchor": "tests/test_refusals_name_served_tools.py:1",
            "kind": "dod",
            "evidence_state": "observed",
            "proof_hint": "nmemory capsule export --no-stamp",
        },
        now=NOW,
    )
    assert "not English" in json.dumps(recapture), recapture

    classified = _payload(
        server.call_tool(
            "nmemory_kind_classify",
            {
                "capsule_id": dod,
                "content": PRE_GATE_TEXT,
                "kind": "dod",
                "evidence_state": "observed",
                "proof_hint": "nmemory capsule export --no-stamp",
            },
            now=NOW,
        )
    )
    assert classified["persisted"] == dod, classified
    met = _payload(_dod_set(server, capsule_id=dod, verdict="met", reason="it printed"))
    assert met["verdict"] == "met", met


def test_the_dod_of_shape_refusal_names_a_served_tool_and_its_advice_works() -> None:
    server = _server()
    unclassified = _seed_pre_gate(server, None)
    task = _capture(server, "Ship the export command.", "task")
    edge = {"kind": "dod_of", "from": unclassified, "to": task}

    refused = server.call_tool("nmemory_relation_add", edge, now=NOW)
    assert "dod_of binds a Definition of Done" in json.dumps(refused), refused
    assert "nmemory_kind_classify" in _named_tools(server, refused), refused

    _payload(
        server.call_tool(
            "nmemory_kind_classify",
            {"capsule_id": unclassified, "content": PRE_GATE_TEXT, "kind": "dod"},
            now=NOW,
        )
    )
    _payload(server.call_tool("nmemory_relation_add", edge, now=NOW))


def test_an_unindexed_source_names_the_index_command() -> None:
    server = _server()
    payload = _payload(
        server.call_tool(
            "nmemory_context_show", {"query": "export", "sources": ["markdown"]}
        )
    )
    markdown = next(s for s in payload["sources"] if s["source"] == "markdown")
    assert markdown["status"] == "not_indexed", markdown
    assert markdown["detail"] == "run nmemory index build", markdown
