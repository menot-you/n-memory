"""The staged fence must fail CLOSED, or an expiration promotes by omission.

The fence lived in a CLOSED list of verdicts to exclude —
`review_state in ("proposed", "rejected")`. That reverses the burden: any
verdict that the code does not yet recognize passes the fence and is served as full
truth. And that is exactly where the expiration of staged proposals would lead: a
new `expired` verdict, whose purpose is to say "nobody reviewed this within the
horizon", made the capsule SERVED, which is the opposite of what it means and
what LOCK 2 forbids (nothing applied by omission).

The rule becomes one of PERMISSION, which is what `Store.list_review_fenced` already uses
(`verdict <> 'ratified'`): a capsule is served with no verdict at all — the case for the
vast majority — or one explicitly `ratified`. Everything else is fenced.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from storefile import fresh_store_path

from nmemory.server import MemoryServer
from nmemory.store import Store

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)


def _server() -> MemoryServer:
    url = fresh_store_path("fence")
    return MemoryServer(store=Store.open(url))


def _ok(res: dict[str, Any]) -> dict[str, Any]:
    assert "result" in res and res["result"]["isError"] is False, res
    payload: dict[str, Any] = json.loads(res["result"]["content"][0]["text"])
    return payload


def _seed(server: MemoryServer, marca: str) -> str:
    payload = _ok(
        server.call_tool(
            "nmemory_capsule_ingest",
            {
                "content": f"a afirmacao {marca} da cerca staged",
                "source": "test",
                "anchor": f"cerca/{marca}.py:1",
                "kind": "fact",
                "project_id": "pynott",
            },
        )
    )
    return str(payload["outcomes"][0]["id"])


def _served(server: MemoryServer) -> set[str]:
    payload = _ok(
        server.call_tool(
            "nmemory_capsule_retrieve",
            {
                "terms": ["cerca", "staged"],
                "project_id": "pynott",
                "limit": 20,
                "token_budget": 100_000,
            },
        )
    )
    out: set[str] = set()
    for row in payload.get("capsules", payload.get("results", [])):
        if isinstance(row, dict):
            cid = row.get("id") or row.get("capsule_id")
            if cid:
                out.add(str(cid))
    return out


def test_um_veredito_desconhecido_fica_cercado_e_nao_servido() -> None:
    """The expiration falsifier, written BEFORE it existed.

    With the closed list, `expired` was neither `proposed` nor `rejected`, so
    it passed the fence: expiring a proposal PROMOTED IT. With the permission rule,
    only `ratified` (or no verdict) is served.
    """
    server = _server()
    livre = _seed(server, "livre")
    ratificada = _seed(server, "ratificada")
    expirada = _seed(server, "expirada")
    proposta = _seed(server, "proposta")
    server.store.reviews.append_review_event(
        ratificada, "ratified", "revisto", "owner", NOW
    )
    server.store.reviews.append_review_event(
        expirada, "expired", "horizonte", "curator", NOW
    )
    server.store.reviews.append_review_event(
        proposta, "proposed", "staged ingest", "j", NOW
    )

    servidas = _served(server)
    assert livre in servidas, "a capsule with no verdict at all must be served"
    assert ratificada in servidas, "`ratified` is the only verdict that serves"
    assert proposta not in servidas, "the old fence already did this"
    assert expirada not in servidas, (
        "a verdict the fence does not know was SERVED: the closed list "
        "reverses the burden and expiry would promote the proposal by default"
    )


def test_o_ratificado_depois_de_expirado_volta_a_ser_servido() -> None:
    """Expiration is not irreversible, nor can it be: the verdict that counts is the
    LAST by `seq`. The owner ratifying a lapsed proposal returns it to
    service without any undo — this is the same property the census relies on."""
    server = _server()
    cid = _seed(server, "revivida")
    server.store.reviews.append_review_event(
        cid, "expired", "horizonte", "curator", NOW
    )
    assert cid not in _served(server)
    server.store.reviews.append_review_event(
        cid, "ratified", "the owner read it", "owner", NOW
    )
    assert cid in _served(server), "the last verdict by seq is what counts"
