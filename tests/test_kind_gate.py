"""Capture declares what it is asserting, or is refused at the door.

Measured on 2026-09-19 in the live store digest: 147 capsules written in 7 days
became `unclassified` --- more than any real kind except `fact` and
`failure_pattern`. A capsule without kind does not pass the proof door
(`FACTUAL_KINDS`), does not enter any census by kind, and has no lifecycle:
it is a write that nobody ever evaluates again.

The door belongs to the tool VERB, not to `ingest()`: internal writers
(curator, GitHub sync, bridge) call `ingest()` directly and are excluded
by construction, as the proof door already is.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from nmemory.server import MemoryServer
from nmemory.store import Store

NOW = datetime(2026, 9, 19, 12, 0, 0, tzinfo=UTC)

BASE = {
    "content": "Some claim or other about the world.",
    "source": "session:test",
    "anchor": "tests/test_kind_gate.py:1",
}


def _call(server: MemoryServer, args: dict) -> dict:
    res = server.call_tool("nmemory_capsule_ingest", args)
    return json.loads(res["result"]["content"][0]["text"])


def test_uma_captura_sem_kind_e_recusada_na_porta() -> None:
    store = Store.open_disposable_test_store()
    server = MemoryServer(store=store)
    payload = _call(server, dict(BASE))
    assert payload["captured"] == 0, payload
    assert payload["rejected"] == 1, payload
    erro = payload["outcomes"][0]["error"]
    assert "kind" in erro, erro
    # The refusal TEACHES: names the closed vocabulary, otherwise the agent guesses.
    assert "fact" in erro and "decision" in erro, erro


def test_a_mesma_captura_COM_kind_passa() -> None:
    store = Store.open_disposable_test_store()
    server = MemoryServer(store=store)
    payload = _call(server, dict(BASE, kind="fact", proof_hint="ls"))
    assert payload["captured"] == 1, payload


def test_no_lote_so_o_item_sem_kind_cai_e_os_irmaos_capturam() -> None:
    store = Store.open_disposable_test_store()
    server = MemoryServer(store=store)
    payload = _call(
        server,
        {
            "items": [
                dict(BASE, content="Com kind, item 0.", kind="fact", proof_hint="ls"),
                dict(BASE, content="Sem kind, item 1."),
                dict(BASE, content="Com kind, item 2.", kind="task"),
            ]
        },
    )
    assert payload["captured"] == 2, payload
    assert payload["rejected"] == 1, payload
    assert payload["outcomes"][1]["status"] == "rejected", payload
    # The locator leads the batch refusal grammar.
    assert payload["outcomes"][1]["error"].startswith("items[1]"), payload
    assert payload["outcomes"][0]["status"] == "captured", payload
    assert payload["outcomes"][2]["status"] == "captured", payload


def test_sem_kind_a_recusa_nomeia_o_kind_e_NAO_a_prova() -> None:
    """An item without kind AND without re-obtainable proof is refused for LACK OF KIND.

    This does NOT measure the order of the two doors: `None in FACTUAL_KINDS` is false,
    so the proof door never fires on an item without kind and the refusal exits by
    `kind` in either order. What is measured is that the message names the RIGHT
    complaint --- a refusal that names the wrong complaint directs the agent to fix the
    wrong thing. Mutant K2 (joining the proof complaint to the text) kills it."""
    store = Store.open_disposable_test_store()
    server = MemoryServer(store=store)
    payload = _call(server, {**BASE, "anchor": "curto"})
    assert payload["rejected"] == 1, payload
    erro = payload["outcomes"][0]["error"]
    assert "kind" in erro, erro
    assert "re-obtainable" not in erro, erro
