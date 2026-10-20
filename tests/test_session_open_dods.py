"""`session_finish` reports DoDs that the session opened and did not close.

Measured in the live store on 2026-09-19: 985 of 1108 DoDs are open and NONE is
older than 90 days --- it is not obsolescence, it is opening without closing.
Decay does not touch them; what touches them is the owner seeing the number at the end of the session.

The closing rule is the ONLY one in the repository (`relation.CLOSING_KINDS`):
LIVE INCOMING `witnesses` or `supersedes` edge. A row in `outcomes` is an
advisory observation and does NOT close (store.py:2522) --- and this file reads it
alongside `test_work_closing_rule.py`, not against it. `dod_without_verdict` is the
OTHER question (`dod_events.axis = 'verdict'`) and is served alongside, never in place.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from nmemory.capture import CaptureContext
from nmemory.relation import RelationKind
from nmemory.server import MemoryServer
from nmemory.store import Store
from nmemory.tools import ingest as ingest_tool
from nmemory.tools import purge as purge_tool

NOW = datetime(2026, 9, 19, 12, 0, 0, tzinfo=UTC)


def _ingest(server: MemoryServer, session: str | None, texto: str, kind: str) -> str:
    args: dict[str, object] = {
        "content": texto,
        "source": "session:test",
        "anchor": "tests/test_session_open_dods.py:1",
        "kind": kind,
    }
    if session is not None:
        args["session_id"] = session
    res = server.call_tool("nmemory_capsule_ingest", args)
    payload = json.loads(res["result"]["content"][0]["text"])
    assert payload["captured"] == 1, payload
    return str(payload["outcomes"][0]["id"])


def _finish(server: MemoryServer, session: str) -> dict:
    res = server.call_tool("nmemory_session_finish", {"session_id": session})
    assert not res["result"].get("isError"), res
    return json.loads(res["result"]["content"][0]["text"])


def _start(server: MemoryServer) -> str:
    return str(
        json.loads(
            server.call_tool("nmemory_session_start", {})["result"]["content"][0][
                "text"
            ]
        )["session_id"]
    )


def test_o_session_finish_nomeia_as_dod_que_a_sessao_deixou_abertas() -> None:
    store = Store.open_disposable_test_store()
    server = MemoryServer(store=store)
    session = _start(server)

    # The wrong rule (`outcomes`) and the right one (`witnesses`) must close
    # DIFFERENT numbers, otherwise the assertion is vacuous --- the lesson of
    # test_work_closing_rule.py:97-101.
    so_outcome = _ingest(
        server, session, "DoD with an outcome and nothing else.", "dod"
    )
    testemunhada = _ingest(server, session, "DoD closed by witnesses.", "dod")
    aberta = _ingest(server, session, "DoD that stays open: consumer read.", "dod")
    # Kind discriminators: not every capture in the session is a DoD.
    _ingest(server, session, "A task from the same session.", "task")
    _ingest(server, session, "A measured fact from the same session.", "fact")

    prova = _ingest(server, session, "The proof that witnesses the delivery.", "fact")
    for alvo in (so_outcome, testemunhada):
        store.substrate.record_outcome(
            "observed: entregue", "judge", None, alvo, None, 0.0, NOW
        )
    store.add_relation(RelationKind.WITNESSES, prova, testemunhada, now=NOW)

    # Session discriminator: an open DoD from ANOTHER session does not enter.
    outra = _start(server)
    alheia = _ingest(server, outra, "DoD from another session, also open.", "dod")

    payload = _finish(server, session)

    assert payload["dod_opened"] == 3, payload
    # The wrong rule would give 2 closed; the right one gives 1.
    assert payload["dod_closed"] == 1, payload
    abertas = [d["id"] for d in payload["dod_open"]]
    assert abertas == [so_outcome, aberta], payload
    assert testemunhada not in abertas
    assert alheia not in abertas
    # `dod_without_verdict` counts over OPEN, the universe of the list served
    # alongside: `testemunhada` closed without verdict and does NOT enter,
    # otherwise the reader would see "3 without verdict" alongside 2 open.
    assert payload["dod_without_verdict"] == 2, payload


def test_uma_dod_purgada_nao_conta_como_aberta() -> None:
    """`canonical_json IS NOT NULL` alone does not define the live universe
    (store.py:1071-1077); neighboring censuses fence by `tombstones`.

    The HMAC key is explicit: the purge refuses without one, and the suite does
    not inherit the host's (tests/conftest.py fences it; see tests/test_forget.py).
    """
    store = Store.open_disposable_test_store()
    server = MemoryServer(store=store, hmac_env_key=b"k" * 32)
    session = _start(server)
    viva = _ingest(server, session, "DoD that stays in the store.", "dod")
    purgada = _ingest(server, session, "DoD that is going to be purged.", "dod")
    res = purge_tool.handle(
        server.handler_state(),
        {"id": purgada, "mode": "purged", "reason": "teste"},
        None,
        NOW,
        "t",
    )
    assert "error" not in res, res

    payload = _finish(server, session)
    assert [d["id"] for d in payload["dod_open"]] == [viva], payload
    assert payload["dod_opened"] == 1, payload


def test_a_dod_sem_session_id_proprio_entra_pelo_capture_context() -> None:
    """`capsules.session_id` was NULL in 9085 of 12019 rows: the real identity
    of the harness session lives in `capture_context.session_id`."""
    store = Store.open_disposable_test_store()
    server = MemoryServer(store=store)
    session = _start(server)
    ctx = CaptureContext(
        harness="pytest",
        model="none",
        cwd="/tmp",
        session_id=session,
        host="t",
        owner="t",
    )
    res = ingest_tool.handle(
        server.handler_state(),
        {
            "content": "DoD sem session_id explicito.",
            "source": "session:test",
            "anchor": "tests/test_session_open_dods.py:1",
            "kind": "dod",
        },
        ctx,
        NOW,
        "t",
    )
    sem_campo = str(json.loads(res["content"][0]["text"])["outcomes"][0]["id"])

    payload = _finish(server, session)
    assert sem_campo in [d["id"] for d in payload["dod_open"]], payload


def test_uma_sessao_sem_dod_nao_inventa_o_relatorio() -> None:
    store = Store.open_disposable_test_store()
    server = MemoryServer(store=store)
    session = _start(server)
    _ingest(server, session, "Only a fact, no DoD.", "fact")

    payload = _finish(server, session)
    assert payload["dod_opened"] == 0, payload
    assert payload["dod_closed"] == 0, payload
    assert payload["dod_without_verdict"] == 0, payload
    assert payload["dod_open"] == [], payload


def test_um_evento_de_status_nao_e_veredito() -> None:
    """`dod_events` has two axes. Only `axis='verdict'` is a verdict --- a
    `status` does not close the question, and without the axis fence the report
    would claim that a DoD merely moved already has a judgment."""
    store = Store.open_disposable_test_store()
    server = MemoryServer(store=store)
    session = _start(server)
    so_status = _ingest(server, session, "A DoD that only changed state.", "dod")
    # A verdict requires the observation that justifies it (evidence_state +
    # proof_hint); it is the gate of `nmemory_dod_set` itself, not of this test.
    res = server.call_tool(
        "nmemory_capsule_ingest",
        {
            "content": "DoD que recebeu veredito.",
            "source": "session:test",
            "anchor": "tests/test_session_open_dods.py:1",
            "kind": "dod",
            "session_id": session,
            "evidence_state": "observed",
            "proof_hint": "uv run --group dev pytest tests/test_session_open_dods.py",
        },
    )
    com_veredito = str(
        json.loads(res["result"]["content"][0]["text"])["outcomes"][0]["id"]
    )
    store.append_dod_event(so_status, "status", "accepted", "judge", "", NOW)
    store.append_dod_event(
        com_veredito, "verdict", "met", "judge", "a suite passou, 6 passed", NOW
    )

    payload = _finish(server, session)
    assert payload["dod_opened"] == 2, payload
    # A `status` is NOT a verdict: only the second has judgment.
    assert payload["dod_without_verdict"] == 1, payload
    # And neither closed: a verdict is not a closing edge.
    assert payload["dod_closed"] == 0, payload


def test_uma_aresta_de_fecho_REVOGADA_reabre_a_dod() -> None:
    """U1: a revoked edge does not close. Without `live_relation_sql()` in the fence,
    the DoD would remain closed forever by an edge that was already undone --- the
    divergence that b3074d87 came to close."""
    store = Store.open_disposable_test_store()
    server = MemoryServer(store=store)
    session = _start(server)
    reaberta = _ingest(server, session, "DoD whose replacement was revoked.", "dod")
    # `witnesses` is not revocable (only falsifies/supersedes); `supersedes` is the
    # other half of CLOSING_KINDS and serves the same discriminator.
    substituto = _ingest(server, session, "The replacement that later fell.", "dod")
    store.add_relation(
        RelationKind.SUPERSEDES, from_id=substituto, to_id=reaberta, now=NOW
    )
    store.revoke_relation(
        RelationKind.SUPERSEDES,
        substituto,
        reaberta,
        actor="judge",
        reason="the proof did not hold",
        now=NOW,
    )

    payload = _finish(server, session)
    assert payload["dod_closed"] == 0, payload
    assert [d["id"] for d in payload["dod_open"]] == [reaberta, substituto], payload
