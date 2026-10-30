"""U5: system1 observations' own table, and what it must NOT touch.

The typed cheap model OBSERVES and ORDERS the queue before spending the budget
on `claude -p`; it never signs a verdict. The previous design stored this in
`verification_events` with a new `kind`, and the code explains why that
corrupted two consumers — `curator_candidates` takes the last event of
ANY kind before filtering `anchor`, and `standing_of` classifies by
`result` over a closed set. The falsifiers here are exactly those
two, plus eligibility which prevents re-asking the same capsule.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta

import pytest
from nott_system1 import Answer
from storefile import fresh_store_path, sample_capsule_id

from nmemory import system1
from nmemory.capture import CaptureContext
from nmemory.ingest import IngestDefaults, IngestRequest, ingest
from nmemory.queries.curator import curator_candidates, unclassified_capsules
from nmemory.queries.standing import standing_of
from nmemory.repositories.base import StoreError
from nmemory.server import MemoryServer
from nmemory.store import Store

T0 = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)


def _ctx() -> CaptureContext:
    return CaptureContext(
        harness="pytest",
        model="none",
        cwd="/tmp",
        session_id="sess-system1",
        host="t",
        owner="t",
    )


def _fresh() -> tuple[Store, MemoryServer]:
    url = fresh_store_path("system1")
    store = Store.open(url)
    return store, MemoryServer(store=store, project_id="system1")


def _ingest(server: MemoryServer, content: str, anchor: str, **extra: object) -> str:
    args: dict[str, object] = {
        "content": content,
        "kind": "fact",
        "source": "t",
        "anchor": anchor,
        **extra,
    }
    response = server.call_tool("nmemory_capsule_ingest", args, context=_ctx(), now=T0)
    assert not response["result"]["isError"], response
    return str(
        json.loads(response["result"]["content"][0]["text"])["outcomes"][0]["id"]
    )


def _observe(store: Store, cid: str, **over: object) -> None:
    kwargs: dict[str, object] = {
        "question": "curator",
        "subject": "dead_anchor at src/a.py:9",
        "choice": "holds",
        "p": 0.9,
        "confidence": 0.8,
        "model": "system1-1",
        "now": T0,
    }
    kwargs.update(over)
    store.reviews.record_system1_observation(cid, **kwargs)  # type: ignore[arg-type]


def _sem_classe(store: Store, content: str, anchor: str, projeto: str) -> str:
    """Seeds an UNCLASSIFIED capsule via the REAL internal path.

    Since the `kind` gate closed, `nmemory_capsule_ingest` rejects an item without
    `kind`: an unclassified capsule only reaches the store via internal writers.
    And their path is `ingest()` --- curator.py:966, curator.py:1121,
    server.py:5360 --- not `store.append`. Seeding via `append`
    would bypass the taint scan, hash dedup, and the audit row, and with
    `context=None` would record this machine's REAL host and cwd instead of the
    test context.
    """
    out = ingest(
        store,
        IngestRequest(content=content, source="interno", anchor=anchor),
        IngestDefaults(project_id=projeto),
        T0,
        context=_ctx(),
    )
    return str(out.id)


def test_observations_move_no_consumer_of_verification_events() -> None:
    """The falsifier from the previous design: recording N observations must not remove the
    candidate from the queue, must not count as curator judgment, and must not
    touch the standing published to the owner."""
    store, server = _fresh()
    cid = _ingest(server, "a() returns one", "src/a.py:9")
    store.anchors.record_verification(
        cid,
        kind="anchor",
        result="dead_anchor",
        detail="src/a.py:9",
        actor="anchor-check",
        now=T0,
    )
    fila = curator_candidates(store.db, project="system1", now=T0)
    julgados = store.anchors.list_verifications(cid)
    standing = standing_of(store.db, [cid], now=T0)
    assert [c for c, _ in fila] == [cid]

    for i in range(5):
        _observe(store, cid, now=T0 + timedelta(seconds=i))

    assert curator_candidates(store.db, project="system1", now=T0) == fila
    assert store.anchors.list_verifications(cid) == julgados
    assert standing_of(store.db, [cid], now=T0) == standing


def test_observed_subjects_is_the_eligibility_fence() -> None:
    """One question per capsule PER CONTENT: change the subject, it becomes
    eligible again; the same capsule under another question remains eligible."""
    store, server = _fresh()
    cid = _ingest(server, "a() returns one", "src/a.py:9")
    assert (
        store.reviews.system1_observed_subjects("classify", ["sha-a", "sha-b"]) == set()
    )

    _observe(store, cid, question="classify", subject="sha-a", choice="fact")

    assert store.reviews.system1_observed_subjects("classify", ["sha-a", "sha-b"]) == {
        "sha-a"
    }
    assert store.reviews.system1_observed_subjects("classify", ["sha-b"]) == set()
    assert store.reviews.system1_observed_subjects("curator", ["sha-a"]) == set()
    assert store.reviews.system1_observed_subjects("classify", []) == set()


def test_observations_for_is_keyed_by_capsule_and_subject_of_that_model() -> None:
    """Reuse reads the LAST observation by `(capsule_id, subject)` and only those of the
    requested model. The capsule is in the key: two capsules at the same anchor share the
    subject, and B must not inherit A's answer (review of #660)."""
    store, server = _fresh()
    a = _ingest(server, "a() returns one", "src/a.py:9")
    b = _ingest(server, "a() returns two", "src/a.py:9")
    subj = "dead_anchor at src/a.py:9"
    _observe(store, a, subject=subj, choice="holds", now=T0)
    _observe(
        store,
        a,
        subject=subj,
        choice="nenhuma",
        p=0.7,
        confidence=None,
        now=T0 + timedelta(seconds=1),
    )
    _observe(
        store,
        a,
        subject=subj,
        choice="false",
        model="system1-2",
        now=T0 + timedelta(seconds=2),
    )

    lidas = store.reviews.system1_observations_for(
        "curator", [(a, subj), (b, subj), (a, "outro")], model="system1-1"
    )
    assert lidas == {(a, subj): ("nenhuma", 0.7, None)}, "B herdou a resposta de A"
    assert store.reviews.system1_observations_for(
        "curator", [(a, subj)], model="system1-2"
    ) == {(a, subj): ("false", 0.9, 0.8)}
    assert (
        store.reviews.system1_observations_for(
            "curator", [(a, subj)], model="system1-3"
        )
        == {}
    )
    assert (
        store.reviews.system1_observations_for("curator", [], model="system1-1") == {}
    )
    with pytest.raises(StoreError):
        store.reviews.system1_observations_for(
            "palpite", [(a, subj)], model="system1-1"
        )


def test_unclassified_capsules_respects_class_fence_and_limit() -> None:
    store, server = _fresh()
    um = _sem_classe(store, "unclassified one", "src/a.py:1", "system1")
    dois = _sem_classe(store, "unclassified two", "src/a.py:2", "system1")
    com_classe = _ingest(server, "already classified", "src/a.py:3", kind="fact")
    sub = _sem_classe(store, "sub unclassified", "src/a.py:4", "system1/sub")
    fora = _sem_classe(store, "outside the fence", "src/a.py:5", "outro")

    fila = unclassified_capsules(store.db, project="system1", limit=10)
    assert [c for c, _ in fila] == [um, dois, sub]
    assert dict(fila)[um] == "unclassified one"
    assert com_classe not in dict(fila) and fora not in dict(fila)

    assert [
        c for c, _ in unclassified_capsules(store.db, project="system1", limit=2)
    ] == [
        um,
        dois,
    ]
    assert fora in dict(unclassified_capsules(store.db, project=None, limit=10))


def test_refusals_are_typed_not_empty_answers() -> None:
    store, _server = _fresh()
    with pytest.raises(StoreError):
        _observe(store, "cap-1", question="palpite")
    with pytest.raises(StoreError):
        _observe(store, "cap-1", p=1.5)
    with pytest.raises(StoreError):
        _observe(store, "cap-1", p=-0.1)
    with pytest.raises(StoreError):
        store.reviews.system1_observed_subjects("palpite", ["sha-a"])
    with pytest.raises(StoreError):
        unclassified_capsules(store.db, project=None, limit=0)


# ── The queue is traversed IN FULL, one request per item ──────────────────────
#
# In the first version `MAX_ITENS` truncated the QUEUE: 70 candidates were 32
# observed and 38 silences. Then came blocks of 32 in a shared state
# — and on 2026-09-18 it was measured that this state blinded the model (32/32 `nenhuma`
# versus 24/32 with the item embedded as state). Now each item is a request of
# TWO questions with its own state; the queue continues to be seen in full.


def test_triage_observes_the_whole_pool_in_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(system1, "is_configured", lambda *a, **k: True)
    por_chamada: list[int] = []

    def falso_ask(estado, perguntas, **kw):  # type: ignore[no-untyped-def]
        por_chamada.append(len(perguntas))
        respostas = {}
        for qid in perguntas:
            if qid.endswith("#taint"):
                respostas[qid] = Answer(id=qid, type="noul", value=0.1)
            else:
                respostas[qid] = Answer(
                    id=qid, type="choice", value="false", probabilities={"false": 0.9}
                )
        return respostas

    monkeypatch.setattr(system1, "_ask", falso_ask)
    itens = [
        system1.JudgeItem(
            capsule_id=f"cap-{i}",
            judgement="dead_anchor at src/a.py:1",
            content=f"afirmacao {i}",
            path="src/a.py",
            excerpt="x",
        )
        for i in range(70)
    ]
    res = system1.triage_verdicts(itens)
    assert len(res.verdicts) == 70, (
        "the queue truncated: fewer were observed than prepared"
    )
    assert len(res.taint) == 70
    assert por_chamada == [2] * 70, por_chamada
    assert max(por_chamada) <= 64, "the lib question ceiling is per call"


def test_a_partial_batch_keeps_what_was_paid(monkeypatch: pytest.MonkeyPatch) -> None:
    """One request per item: the service drops midway and what already responded stays; without
    any response, unavailability bubbles up as always."""
    monkeypatch.setattr(system1, "is_configured", lambda *a, **k: True)
    chamadas = 0
    paid: set[str] = set()
    lock = threading.Lock()

    def cai_ao_terceiro(estado, perguntas, **kw):  # type: ignore[no-untyped-def]
        nonlocal chamadas
        with lock:
            chamadas += 1
            if chamadas >= 3:
                raise system1.System1Unavailable("429")
            paid.update(qid for qid in perguntas if not qid.endswith("#taint"))
        return {
            qid: (
                Answer(id=qid, type="noul", value=0.1)
                if qid.endswith("#taint")
                else Answer(
                    id=qid, type="choice", value="false", probabilities={"false": 0.9}
                )
            )
            for qid in perguntas
        }

    monkeypatch.setattr(system1, "_ask", cai_ao_terceiro)
    itens = [
        system1.JudgeItem(
            f"cap-{i}", "dead_anchor at src/a.py:1", f"afirmacao {i}", "src/a.py", "x"
        )
        for i in range(40)
    ]
    res = system1.triage_verdicts(itens)
    assert set(res.verdicts) == paid and len(paid) == 2, "dropped what was paid for"
    assert chamadas <= 2 + system1.MAX_IN_FLIGHT, "admitted more work after the failure"
    chamadas = 2
    with pytest.raises(system1.System1Unavailable):
        system1.triage_verdicts(itens)


@pytest.mark.parametrize("ligado", [" 1 ", "TRUE", "yes", "sim"])
def test_a_caixa_liga_com_a_palavra_que_o_owner_escreveu(ligado: str) -> None:
    """On 2026-09-18 the owner put `NOTT_NMEMORY_SYSTEM1=1` in `.env` and nothing happened:
    the gate checked against exact `"on"` and no call site distinguishes "disabled by
    choice" from "enabled with the wrong word" — the box stayed silent for a day."""
    assert system1.is_configured(
        {"NOTT_NMEMORY_SYSTEM1": ligado, "NOTT_SYSTEM1_API_KEY": "k"}
    )


@pytest.mark.parametrize("desligado", ["", "off", "on", "0", "false", "nao", "talvez"])
def test_o_que_nao_e_sim_continua_a_ser_nao(desligado: str) -> None:
    assert not system1.is_configured(
        {"NOTT_NMEMORY_SYSTEM1": desligado, "NOTT_SYSTEM1_API_KEY": "k"}
    )


def test_ausente_e_ligado_por_omissao() -> None:
    """Owner law (2026-09-18): the default is 1. Without the variable, the box is enabled
    — and "on" ceased to be yes, because it is not standard."""
    assert system1.is_configured({"NOTT_SYSTEM1_API_KEY": "k"})
    assert not system1.is_configured(
        {"NOTT_NMEMORY_SYSTEM1": "on", "NOTT_SYSTEM1_API_KEY": "k"}
    )


def test_ligada_sem_credencial_continua_desligada() -> None:
    """The key alone was never enough: without a credential there is no call to pay for."""
    assert not system1.is_configured(
        {"NOTT_NMEMORY_SYSTEM1": "1", "NOTT_SYSTEM1_API_KEY": " "}
    )


def test_verdict_transport_overlaps_requests_without_sharing_item_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A serial transport cannot release the first pair; every item stays isolated."""
    monkeypatch.setattr(system1, "is_configured", lambda *a, **k: True)
    rendezvous = threading.Barrier(2, timeout=2)
    lock = threading.Lock()
    seen: set[str] = set()

    def answer(state, questions, **kwargs):  # type: ignore[no-untyped-def]
        cid = next(q for q in questions if not q.endswith("#taint"))
        assert state["claim"] == f"claim {cid}"
        assert set(questions) == {cid, f"{cid}#taint"}
        with lock:
            seen.add(cid)
        rendezvous.wait()
        return {
            cid: Answer(
                id=cid, type="choice", value="holds", probabilities={"holds": 1.0}
            ),
            f"{cid}#taint": Answer(id=f"{cid}#taint", type="noul", value=0.0),
        }

    monkeypatch.setattr(system1, "_ask", answer)
    items = [
        system1.JudgeItem(
            f"cap-{i}", "changed_anchor", f"claim cap-{i}", "a.py", "x = 1"
        )
        for i in range(2)
    ]
    result = system1.triage_verdicts(items)
    assert set(result.verdicts) == seen == {sample_capsule_id(0), "cap-1"}
    assert set(result.taint) == seen


def test_an_unexpected_failure_also_stops_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Any exception, not only `System1Unavailable`, ends admission: it propagates
    after the work already in flight, and no later item buys a request."""
    monkeypatch.setattr(system1, "is_configured", lambda *a, **k: True)
    calls = 0
    lock = threading.Lock()

    def fail_on_third(state, questions, **kw):  # type: ignore[no-untyped-def]
        nonlocal calls
        with lock:
            calls += 1
            if calls >= 3:
                raise RuntimeError("malformed answer")
        return {}

    monkeypatch.setattr(system1, "_ask", fail_on_third)
    items = [
        system1.JudgeItem(f"cap-{i}", "dead_anchor at src/a.py:1", "x", "src/a.py", "x")
        for i in range(40)
    ]
    with pytest.raises(RuntimeError):
        system1.triage_verdicts(items)
    assert calls <= 2 + system1.MAX_IN_FLIGHT, "admitted more work after the failure"


@pytest.mark.parametrize(
    "scope,triage",
    [
        (
            "CURATOR",
            lambda: system1.triage_verdicts(
                [system1.JudgeItem("cap-1", "anchor", "claim", "a.py", "excerpt")]
            ),
        ),
        ("KIND", lambda: system1.triage_kinds([("cap-1", "content")])),
        ("STAGED", lambda: system1.triage_staged([("cap-1", "content")])),
        ("TAINT", lambda: system1.taint_probe(["surface"])),
    ],
)
def test_each_triage_asks_on_its_own_route(
    monkeypatch: pytest.MonkeyPatch, tmp_path, scope, triage
) -> None:
    "Only the question's own key is set: the gate reads it."
    monkeypatch.delenv("NOTT_SYSTEM1_API_KEY", raising=False)
    monkeypatch.delenv("NOTT_NMEMORY_SYSTEM1", raising=False)
    monkeypatch.setenv("NOTT_NMEMORY_CONFIG", str(tmp_path / "absent.toml"))
    for other in ("TAINT", "CURATOR", "KIND", "STAGED"):
        for part in ("ENDPOINT", "MODEL", "API_KEY"):
            monkeypatch.delenv(f"NOTT_SYSTEM1_{other}_{part}", raising=False)
    monkeypatch.setenv(f"NOTT_SYSTEM1_{scope}_ENDPOINT", "https://own.invalid/v1")
    monkeypatch.setenv(f"NOTT_SYSTEM1_{scope}_MODEL", "own-model")
    monkeypatch.setenv(f"NOTT_SYSTEM1_{scope}_API_KEY", "own-key")
    vistos: list[dict[str, object]] = []

    def falso_ask(estado, perguntas, **kw):  # type: ignore[no-untyped-def]
        vistos.append(kw)
        # The ingest fence's noul; the triage questions accept an empty answer.
        return {
            q: Answer(id=q, type="noul", value=0.1) for q in perguntas if q == "taint"
        }

    monkeypatch.setattr(system1, "_ask", falso_ask)
    triage()
    assert vistos and all(
        (kw["endpoint"], kw["model"], kw["auth_env"])
        == ("https://own.invalid/v1", "own-model", f"NOTT_SYSTEM1_{scope}_API_KEY")
        for kw in vistos
    )
