"""A batch that fails midway rolls back IN ITS ENTIRETY — and the message must state so.

The message in `server.py` promised the opposite — "items before this index are
already stored" — and described the world prior to the boundary. Whoever reads it decides not
to re-attempt the previous items, and loses them all. It is the worst kind of lie in a
memory system: one that makes the caller delete work that never existed.
"""

from __future__ import annotations

from typing import Any

import pytest
from storefile import fresh_store_path

from nmemory.repositories.base import StoreError
from nmemory.server import MemoryServer
from nmemory.store import Store
from nmemory.tools import ingest as ingest_tool


def _loja() -> Any:
    url = fresh_store_path()
    return Store.open(url)


def _item(n: int) -> dict[str, Any]:
    return {
        "content": f"o item {n} do lote",
        "source": "session:test",
        "anchor": f"PLAN.md:{n}",
        "kind": "fact",
        "project_id": "nmemory",
    }


def test_um_erro_de_loja_a_meio_do_lote_nao_grava_nada(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _loja()
    server = MemoryServer(store=store)

    real = ingest_tool.ingest_capture
    vistos: list[int] = []

    def _falha_no_terceiro(loja: Any, req: Any, defaults: Any, **kw: Any) -> Any:
        vistos.append(len(vistos) + 1)
        if len(vistos) == 3:
            raise StoreError("falha simulada da loja no terceiro item")
        return real(loja, req, defaults, **kw)

    monkeypatch.setattr(ingest_tool, "ingest_capture", _falha_no_terceiro)

    resp = server.call_tool(
        "nmemory_capsule_ingest", {"items": [_item(i) for i in range(5)]}
    )

    assert "error" in resp, f"the call had to fail: {resp}"
    mensagem = str(resp["error"]["message"])

    # 1. The count that matters: boundary rolled back, so store is EMPTY.
    #    FRESH reader, on purpose: confirming is not the connection that wrote.
    fresca = Store.open(server.store.db.path)
    guardadas = fresca.classifications.list_ids_of_kind("fact")
    assert guardadas == [], (
        "two items passed before the failure and stayed stored — the boundary "
        f"did not roll back: {guardadas}"
    )

    # 2. And the message cannot promise the opposite of what the store shows.
    assert "already stored" not in mensagem, (
        f"the message promises a partial write that the rollback undid: {mensagem}"
    )
    assert "item 2" in mensagem, f"the message locates the item that failed: {mensagem}"
    assert "rolled back" in mensagem or "nothing" in mensagem, (
        f"the message must say that NOTHING was stored: {mensagem}"
    )
