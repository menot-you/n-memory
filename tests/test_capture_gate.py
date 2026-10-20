"""The capture door: a statement of fact brings re-obtainable proof, or is refused.

Measured on live store on 2026-09-19 (`nmemory.capsules` ⨝ `classifications` ⨝
`source_quotes` ⨝ `epistemics`, read-only, 7-day window): 912 captures of
factual kind, ZERO arrived without anchor — anchor was already mandatory, so the
door cannot simply be "has anchor". With the rule of this file (code anchor
with line, URL, `#N` reference, verbatim quote > 60 characters, `source_quote`
or `proof_hint`) refusal is 206 of 912 = 22.6%: `fact` 99/312,
`failure_pattern` 37/357, `decision` 56/165, `constraint` 7/26, `procedure` 7/52.

The strict variant (only `path:line`, URL, `source_quote`, `proof_hint`) would refuse
390 of 995 in lead measurement, with `decision` 129/198 — refusing the verbatim quote
of the owner turn is refusing memory of decisions, which is the only proof
a decision can have. Therefore `#N` and long quotes are included.

The door lives in `nmemory.tools.ingest` and NOT in `ingest()`: curator
(`curator.py`), GitHub sync (`github.py`) and bridge call `ingest()`
directly. Lowering it would trade zero exceptions for three.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from nmemory.server import MemoryServer
from nmemory.store import Store

_CITACAO = (
    "the owner said: the gate cannot be «has an anchor», because the anchor is already "
    "mandatory and nobody arrives without one"
)


def _corpo(resp: dict[str, Any]) -> dict[str, Any]:
    """The body of `nmemory_capsule_ingest`: per-item refusal lives in `outcomes`, not
    in a top-level `error` — only a `StoreError` raises across the entire boundary."""
    assert "error" not in resp, f"the call should not fail as a whole: {resp}"
    texto = resp["result"]["content"][0]["text"]
    corpo: dict[str, Any] = json.loads(texto)
    return corpo


def _servidor() -> MemoryServer:
    return MemoryServer(store=Store.open_disposable_test_store(), project_id="nmemory")


def _item(anchor: str, kind: str = "fact", **extra: Any) -> dict[str, Any]:
    return {
        "content": f"a statement of fact anchored at {anchor[:40]}",
        "source": "session:2026-09-19",
        "anchor": anchor,
        "kind": kind,
        "project_id": "nmemory",
        **extra,
    }


@pytest.mark.parametrize(
    "kind",
    ["fact", "failure_pattern", "decision", "constraint", "procedure"],
)
def test_kind_factual_sem_prova_reobtenivel_e_recusado(kind: str) -> None:
    """One case per factual kind: refusal is binary and applies to all."""
    resp = _servidor().call_tool("nmemory_capsule_ingest", _item("PLAN.md", kind=kind))
    corpo = _corpo(resp)
    assert corpo["rejected"] == 1, (
        f"{kind} sem prova re-obtenível passou a porta: {corpo}"
    )
    assert corpo["captured"] == 0, corpo
    msg = corpo["outcomes"][0]["error"]
    # `locator=None` and `field="anchor"`: singular form of `rejection_row`.
    assert msg.startswith("anchor: ingest rejected: "), msg
    assert "re-obtain" in msg, (
        f"the refusal must say what is missing, not only that it failed: {msg}"
    )


@pytest.mark.parametrize(
    ("rotulo", "item"),
    [
        ("path:line", _item("nmemory/server.py:1875")),
        ("url", _item("https://github.com/example/project/pull/123")),
        ("PR reference", _item("the decision in PR #683")),
        ("verbatim quote", _item(_CITACAO)),
        ("source_quote", _item("PLAN.md", source_quote="o owner disse isto")),
        ("proof_hint", _item("PLAN.md", proof_hint="corre tools/gate.sh run python")),
    ],
)
def test_cada_forma_de_prova_aceite_passa(rotulo: str, item: dict[str, Any]) -> None:
    """One case per accepted form of proof — each one alone suffices."""
    corpo = _corpo(_servidor().call_tool("nmemory_capsule_ingest", item))
    assert corpo["captured"] == 1, f"{rotulo} devia passar a porta: {corpo}"
    assert corpo["rejected"] == 0, f"{rotulo} foi recusado em silêncio: {corpo}"


def test_kind_nao_factual_nao_e_julgado() -> None:
    """The door asks for proof from whoever ASSERTS fact. A `plan` does not assert."""
    corpo = _corpo(
        _servidor().call_tool("nmemory_capsule_ingest", _item("PLAN.md", kind="plan"))
    )
    assert corpo["captured"] == 1, f"the gate caught a non-factual kind: {corpo}"


def test_no_lote_a_recusa_nomeia_o_indice_e_os_vizinhos_passam() -> None:
    server = _servidor()
    resp = server.call_tool(
        "nmemory_capsule_ingest",
        {"items": [_item("nmemory/server.py:1875"), _item("PLAN.md")]},
    )
    corpo = _corpo(resp)
    assert corpo["captured"] == 1 and corpo["rejected"] == 1, corpo
    assert corpo["outcomes"][0]["status"] == "captured", corpo
    erro = corpo["outcomes"][1]["error"]
    assert erro.startswith("items[1].anchor: ingest rejected: "), erro
