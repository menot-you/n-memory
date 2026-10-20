"""The three typed verbs: `nspawn_judgment_estimate`, `nspawn_judgment_choose`, `nspawn_judgment_score`.

What these tests protect is not the System1 API — it is the BOUNDARY. A verb querying
an external service can go wrong in three subtle ways:

  1. caller choosing the endpoint or passing the credential (exfiltration by another name);
  2. unavailability being interpreted as "the answer is no" (an operational fact dressed
     as a verdict, which is the inversion D18 exists to prevent);
  3. configuration resolution from the roster remaining unapplied — a bare re-export of
     lib verbs would pass in the other tests with nothing failing.

TRANSPORT IS NOT HERE. It moved down to `libs/system1`, and that is where the error body,
the batch ceiling, and off-request credential are measured (`libs/system1/tests/test_transport.py`).
None of these tests touches the network: the lib is substituted at the seam used by the app module.
"""

from __future__ import annotations

from typing import Any

import pytest

from nspawn import system1
from nspawn.mcp import NSPAWN_TOOL_BY_NAME, McpError, McpServer


@pytest.fixture
def servidor() -> Any:
    return McpServer()


def _respostas(**kwargs: Any) -> dict[str, system1.Answer]:
    return {k: v for k, v in kwargs.items()}


def test_the_three_verbs_are_declared_with_a_real_schema() -> None:
    for nome in (
        "nspawn_judgment_estimate",
        "nspawn_judgment_choose",
        "nspawn_judgment_score",
    ):
        contrato = NSPAWN_TOOL_BY_NAME[nome]
        props = contrato.input_schema["properties"]
        assert set(contrato.input_schema["required"]) == {"state", "questions"}
        # The caller DOES NOT choose where this goes nor with what credential: if one day
        # `endpoint` or `api_key` appear in the schema, this test must fail.
        assert (
            "endpoint" not in props
            and "api_key" not in props
            and "base_url" not in props
        )


def test_a_noul_answer_crosses_the_wire_with_its_probability(
    servidor: Any, monkeypatch: Any
) -> None:
    """The probability IS the product. Collapsing it into a boolean here would defeat the purpose."""
    monkeypatch.setattr(
        system1,
        "noul",
        lambda *a, **k: _respostas(
            live=system1.Answer(id="live", type="noul", value=0.08)
        ),
    )
    out = servidor.nspawn_judgment_estimate(
        state="RUNNING: nothing", questions={"live": "names live work?"}
    )
    assert out["answers"]["live"] == {"type": "noul", "value": 0.08}


def test_a_choice_carries_its_distribution_and_confidence(
    servidor: Any, monkeypatch: Any
) -> None:
    """Confidence is the second axis: choice says WHAT, confidence says WHETHER to act."""
    monkeypatch.setattr(
        system1,
        "choice",
        lambda *a, **k: _respostas(
            state=system1.Answer(
                id="state",
                type="choice",
                value="running",
                probabilities={"running": 0.6, "stopped": 0.38},
                confidence=0.39,
            )
        ),
    )
    out = servidor.nspawn_judgment_choose(
        state="…",
        questions={"state": {"instructions": "which?", "criteria": {"a": "b"}}},
    )
    a = out["answers"]["state"]
    assert a["value"] == "running" and a["confidence"] == 0.39
    assert a["probabilities"]["stopped"] == 0.38


def test_a_score_carries_its_legend(servidor: Any, monkeypatch: Any) -> None:
    """Without a legend the number is a value whose meaning the reader has to guess."""
    monkeypatch.setattr(
        system1,
        "score",
        lambda *a, **k: _respostas(
            clareza=system1.Answer(
                id="clareza",
                type="score",
                value=1.82,
                legend={"0": "contraditória", "1": "ambígua", "2": "inequívoca"},
                confidence=0.74,
            )
        ),
    )
    out = servidor.nspawn_judgment_score(
        state="…",
        questions={"clareza": {"instructions": "q", "criteria": ["a", "b", "c"]}},
    )
    assert out["answers"]["clareza"]["legend"]["2"] == "inequívoca"


def test_unavailable_is_operational_never_a_verdict(
    servidor: Any, monkeypatch: Any
) -> None:
    """NO RESPONSE IS NOT "NO". It is the inversion D18 exists to prevent: an
    operational fact (`1`) dressed as a behavioral verdict."""

    def morre(*a: Any, **k: Any) -> None:
        raise system1.System1Unavailable("the service answered 429 Too Many Requests")

    monkeypatch.setattr(system1, "noul", morre)
    with pytest.raises(McpError) as erro:
        servidor.nspawn_judgment_estimate(state="x", questions={"q": "y"})
    assert erro.value.code == -32002  # unavailable, not "invalid parameter"
    assert "429" in erro.value.rpc_message


def test_a_question_without_instructions_is_refused_before_the_call(
    servidor: Any, monkeypatch: Any
) -> None:
    """A question without instructions measures nothing, and wasting a call to discover it
    pays to learn nothing."""
    chamou = []
    monkeypatch.setattr(system1, "noul", lambda *a, **k: chamou.append(1) or {})
    with pytest.raises(McpError) as erro:
        servidor.nspawn_judgment_estimate(state="x", questions={"q": {"criteria": {}}})
    assert erro.value.code == -32602 and "instructions" in erro.value.rpc_message
    assert not chamou, "the external call was made anyway"


def test_an_empty_batch_is_refused(servidor: Any) -> None:
    with pytest.raises(McpError):
        servidor.nspawn_judgment_estimate(state="x", questions={})


class _System1DoRoster:
    endpoint = "https://roster.example/v1/systemone"
    model = "system1-do-roster"
    timeout_seconds = 7.5


class _RosterFalso:
    system1 = _System1DoRoster()


def test_the_wrappers_actually_apply_the_roster_resolution(monkeypatch: Any) -> None:
    """RESOLUTION MUST BE APPLIED, and this test proves it.

    The lib requires `endpoint`, `model` and `timeout` per argument and has no
    hidden default; a bare re-export of verbs (`from nott_system1 import noul`) would pass in
    all other tests of this file leaving the owner's roster unapplied. The seam
    is the private alias through which the module imports `ask` from the lib.
    """
    visto: dict[str, Any] = {}

    def espia(state: Any, questions: Any, **config: Any) -> dict[str, Any]:
        visto.update(config)
        visto["questions"] = questions
        return {}

    monkeypatch.setattr(system1, "_ask", espia)
    monkeypatch.setattr(system1, "roster_do_host", lambda: _RosterFalso())
    monkeypatch.delenv("NOTT_SYSTEM1_ENDPOINT", raising=False)
    monkeypatch.delenv("NOTT_SYSTEM1_MODEL", raising=False)

    system1.noul("estado", {"q": "?"})
    assert visto["endpoint"] == "https://roster.example/v1/systemone"
    assert visto["model"] == "system1-do-roster"
    assert visto["timeout"] == 7.5
    # The type goes in the body: a wrapper that only forwarded would lose it and the service would refuse.
    assert visto["questions"]["q"]["type"] == "noul"

    # The model REQUESTED by the caller wins over the roster; the endpoint never does, because a
    # destination chosen by the caller is exfiltration by another name.
    system1.noul("estado", {"q": "?"}, model="system1-pedido")
    assert visto["model"] == "system1-pedido"
    assert visto["endpoint"] == "https://roster.example/v1/systemone"


def test_the_three_verbs_go_through_this_module_ask(monkeypatch: Any) -> None:
    """A DOUBLE PLACED ON `system1.ask` MUST CATCH ALL THREE VERBS.

    This is the falsifier for the 2026-09-17 regression: when wrappers started calling
    `noul`/`choice`/`score` FROM THE LIB, the double in `tests/test_mcp_contract.py::_system1_duplo`
    fell out of the path and the test suite started talking to the vendor's public service for real — two
    `422`s from the live service in a test run. No test previously stated that this seam was
    a contract; this one does.
    """
    chamadas: list[str] = []

    def duplo(state: Any, questions: Any, model: str | None = None) -> dict[str, Any]:
        chamadas.append(next(iter(questions.values()))["type"])
        return {}

    monkeypatch.setattr(system1, "ask", duplo)
    # Without roster or network: if any verb bypassed `ask` here, it would hit real urlopen.
    system1.noul("estado", {"q": "?"})
    system1.choice("estado", {"q": {"instructions": "?", "criteria": ["a", "b"]}})
    system1.score("estado", {"q": {"instructions": "?", "criteria": ["baixo", "alto"]}})
    assert chamadas == ["noul", "choice", "score"]
