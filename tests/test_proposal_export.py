"""`nmemory proposal export`: staged observations labelled by a later human verdict.

The export is System1's training feedback: what the model saw, what it chose, and
what the human decided afterwards. An undecided proposal has no label, an
automatic verdict is not a human one, and a forgotten capsule leaves training too.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from storefile import default_store_path

from nmemory.capture import CaptureContext
from nmemory.cli import main
from nmemory.server import MemoryServer
from nmemory.store import Store

T0 = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)


def _ctx() -> CaptureContext:
    return CaptureContext(
        harness="pytest", model="none", cwd="/tmp", session_id="s", host="t", owner="t"
    )


def _ingest(server: MemoryServer, content: str) -> str:
    args: dict[str, Any] = {
        "content": content,
        "kind": "task",
        "source": "t",
        "anchor": "fila/x",
        "project_id": "fila",
        "staged": True,
    }
    r = server.call_tool("nmemory_capsule_ingest", args, context=_ctx(), now=T0)
    assert not r["result"]["isError"], r
    return str(json.loads(r["result"]["content"][0]["text"])["outcomes"][0]["id"])


def _observe(store: Store, cid: str, choice: str, p: float) -> None:
    store.reviews.record_system1_observation(
        cid,
        question="staged",
        subject=f"s-{cid}",
        choice=choice,
        p=p,
        confidence=None,
        model="laya-nott",
        now=T0 + timedelta(minutes=1),
    )


def _review(server: MemoryServer, cid: str, verdict: str) -> None:
    r = server.call_tool(
        "nmemory_proposal_review",
        {"capsule_id": cid, "verdict": verdict, "actor": "owner", "reason": "r"},
        context=_ctx(),
        now=T0 + timedelta(minutes=2),
    )
    assert not r["result"]["isError"], r


def test_export_yields_only_human_decided_live_proposals(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store = Store.open(default_store_path("proposalexport"))
    server = MemoryServer(store=store, project_id="fila", hmac_env_key=b"export-key")
    ratified = _ingest(server, "the API answers in 20 ms")
    rejected = _ingest(server, "the cache never expires")
    undecided = _ingest(server, "the queue drains hourly")
    forgotten = _ingest(server, "a secret that was purged")
    automatic = _ingest(server, "System1 decided this one alone")
    for cid, choice in (
        (ratified, "ratificar"),
        (rejected, "ratificar"),
        (undecided, "nao_decide"),
        (forgotten, "rejeitar"),
        (automatic, "ratificar"),
    ):
        _observe(store, cid, choice, 0.75)
    _review(server, ratified, "ratified")
    _review(server, rejected, "rejected")
    _review(server, forgotten, "rejected")
    store.capsules.forget_capsule(
        forgotten,
        mode="purged",
        reason="test",
        hmac_key=b"test-key-32-bytes-long-for-hmac!!",
        now=T0 + timedelta(minutes=3),
    )
    store.reviews.append_review_event(
        automatic, "ratified", "system1", "system1", T0 + timedelta(minutes=2)
    )

    monkeypatch.setenv("NOTT_DATABASE_DIR", str(Path(store.db.path).parent))
    assert (
        main(["proposal", "export", "--since", "2026-09-18", "--until", "2026-09-19"])
        == 0
    )
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]

    assert {r["capsule_id"]: r["verdict"] for r in rows} == {
        ratified: "ratified",
        rejected: "rejected",
    }
    by_id = {r["capsule_id"]: r for r in rows}
    assert by_id[ratified]["text"] == "the API answers in 20 ms"
    assert by_id[rejected]["choice"] == "ratificar"
    assert by_id[rejected]["p"] == 0.75
    assert by_id[ratified]["observed_at"] == "2026-09-18T12:01:00Z"
    assert by_id[ratified]["reviewed_at"] == "2026-09-18T12:02:00Z"

    # The window is on the observation: a day with none exports nothing.
    assert (
        main(["proposal", "export", "--since", "2026-09-19", "--until", "2026-09-20"])
        == 0
    )
    assert capsys.readouterr().out == ""
