"""A `stale_if` condition that has triggered must STOP being served as valid.

`staleness.py` is a PURE evaluator: `Store` enters `SystemRunner` and is only
used in four read calls, and `commands/capsule.py` (`capsule audit`) prints the report and exits with
code 3. The `TRIGGERED` verdict dies on stdout — nothing flags the capsule, so
`standing_of` continues to serve it as `vale`. This is the owner's third pain:
"memory serves what has become false".

Measured in the live store on 2026-09-19: 2599 declared `stale_if` conditions. The
scheduled run measured 811 resolvable and 569 already triggered, computed and thrown
away on every run.

The path through `valid_to` DOES NOT exist: it is write-once at ingest (`ingest.py:485`) and
no `UPDATE` in the tree touches it. What exists is the verification event —
`record_verification` (`store.py:4231`) — and the `standing_of` branch that reads it is
already there: a `result` outside `VERIFICATION_PASSING` and outside
`VERIFICATION_NEUTRAL` returns as `caducou reason=<result>`.

What this file guards above all is the half that is costly to get wrong. Of the 2599,
1998 are UNDECIDABLE. Marking an undecidable condition as lapsed would manufacture
falsehood out of absent proof — exactly the defect U1 stripped from the
curator, reintroduced through another door. The writing end may only act upon
what the evaluator decided had triggered.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from nmemory.capsule import (
    AuthorityClass,
    Capsule,
    Freshness,
    Provenance,
    Scope,
    sha256_hex,
)
from nmemory.queries.standing import standing_of
from nmemory.staleness import (
    STALENESS_ACTOR,
    STALENESS_KIND,
    STALENESS_RESULT,
    StalenessClass,
    StalenessHit,
    StalenessReport,
    StalenessVerdict,
    record_staleness,
)
from nmemory.store import Store

NOW = datetime(2026, 9, 19, 12, 0, 0, tzinfo=UTC)
LATER = NOW + timedelta(days=1)
PROJETO = "projetodeteste"


def _cap(nome: str) -> Capsule:
    content = f"Capsule {nome} with a staleness condition."
    return Capsule(
        content=content,
        provenance=Provenance(
            source="test:staleness-escreve",
            anchor=f"tests/test_staleness_escreve.py:{nome}",
            source_hash=sha256_hex(content.encode("utf-8")),
        ),
        confidence=0.9,
        freshness=Freshness(valid_from=NOW),
        scope=Scope(project_id=PROJETO),
        authority_class=AuthorityClass.USER_STATED,
        instruction_taint=False,
    )


def _hit(cid: str, veredito: StalenessVerdict, classe: StalenessClass) -> StalenessHit:
    return StalenessHit(
        capsule_id=cid,
        stale_if="o ficheiro ancorado desaparecer",
        class_=classe,
        entity="apps/memory/core/nmemory/staleness.py",
        verdict=veredito,
        observed_state="deleted" if veredito is StalenessVerdict.TRIGGERED else None,
    )


def _relatorio(hits: list[StalenessHit]) -> StalenessReport:
    return StalenessReport(
        total_evaluated=len(hits),
        resolvable_count=sum(1 for h in hits if h.class_ is not StalenessClass.PROSE),
        prose_count=sum(1 for h in hits if h.class_ is StalenessClass.PROSE),
        triggered_count=sum(1 for h in hits if h.verdict is StalenessVerdict.TRIGGERED),
        not_triggered_count=sum(
            1 for h in hits if h.verdict is StalenessVerdict.NOT_TRIGGERED
        ),
        undecidable_count=sum(
            1 for h in hits if h.verdict is StalenessVerdict.UNDECIDABLE
        ),
        hits=hits,
    )


def _standing(store: Store, cid: str) -> dict[str, object]:
    return standing_of(store.db, [cid], now=LATER)[cid]


def test_uma_condicao_disparada_deixa_de_ser_servida_como_valida() -> None:
    store = Store.open_disposable_test_store()
    cid, _ = store.append(_cap("disparou"), NOW)
    assert _standing(store, cid)["state"] == "por_verificar"

    escritos = record_staleness(
        store,
        _relatorio([_hit(cid, StalenessVerdict.TRIGGERED, StalenessClass.PATH)]),
        NOW,
    )

    assert escritos == 1, escritos
    st = _standing(store, cid)
    assert st["state"] == "caducou", st
    assert st["reason"] == "stale_condition_met", st


def test_uma_indecidivel_nao_e_tocada() -> None:
    """The half that is costly to get wrong, with 1998 members in the store.

    Marking an undecidable as lapsed is manufacturing falsehood from absent
    proof: the condition says "if X disappears" and no one could decide whether X
    disappeared. Not even a NEUTRAL event is written — there is no judgment to
    record, and an extra event is a row someone will interpret as judgment.
    """
    store = Store.open_disposable_test_store()
    cid, _ = store.append(_cap("indecidivel"), NOW)

    escritos = record_staleness(
        store,
        _relatorio([_hit(cid, StalenessVerdict.UNDECIDABLE, StalenessClass.PROSE)]),
        NOW,
    )

    assert escritos == 0, escritos
    assert len(store.anchors.list_verifications(cid)) == 0, (
        "an undecidable one generates NO event, not even a neutral one"
    )
    assert _standing(store, cid)["state"] == "por_verificar"


def test_uma_nao_disparada_nao_e_tocada() -> None:
    store = Store.open_disposable_test_store()
    cid, _ = store.append(_cap("nao_disparou"), NOW)

    escritos = record_staleness(
        store,
        _relatorio([_hit(cid, StalenessVerdict.NOT_TRIGGERED, StalenessClass.PATH)]),
        NOW,
    )

    assert escritos == 0, escritos
    assert len(store.anchors.list_verifications(cid)) == 0


def test_correr_duas_vezes_nao_escreve_duas_linhas() -> None:
    """The evaluator runs on a TIMER, every day at 04:30.

    A condition that triggered remains triggered tomorrow. Without this fence, each
    run adds a row per capsule and `verification_events` grows without
    bound for a truth that has not changed — and `list_verifications` is read per
    capsule on each `standing_of`.
    """
    store = Store.open_disposable_test_store()
    cid, _ = store.append(_cap("teimosa"), NOW)
    relatorio = _relatorio([_hit(cid, StalenessVerdict.TRIGGERED, StalenessClass.PATH)])

    primeira = record_staleness(store, relatorio, NOW)
    segunda = record_staleness(store, relatorio, LATER)

    assert primeira == 1, primeira
    assert segunda == 0, "the second run has no new judgement to record"
    assert len(store.anchors.list_verifications(cid)) == 1


def test_uma_verificacao_posterior_NO_MESMO_EIXO_recupera_a_capsula() -> None:
    """Rollback is the model itself: events are append-only.

    If the condition triggered by mistake, whoever re-evaluates the CONDITION writes a
    staleness `pass` and standing returns. Nothing needs to be deleted, and
    deleting would be worse.

    `kind` matters and is not decorative — see the following test.
    """
    store = Store.open_disposable_test_store()
    cid, _ = store.append(_cap("recuperavel"), NOW)
    record_staleness(
        store,
        _relatorio([_hit(cid, StalenessVerdict.TRIGGERED, StalenessClass.PATH)]),
        NOW,
    )
    assert _standing(store, cid)["state"] == "caducou"

    store.anchors.record_verification(
        cid,
        kind=STALENESS_KIND,
        result="pass",
        detail="a condicao deixou de estar disparada",
        actor=STALENESS_ACTOR,
        now=LATER,
    )

    assert _standing(store, cid)["state"] == "vale"


def test_a_varredura_de_ancoras_NAO_apaga_o_veredito_de_obsolescencia() -> None:
    """The finding that almost shipped this unit doing nothing.

    `audit_all` (`anchors.py:495`) writes an `anchor` event per anchored
    capsule on EACH sweep, and `drift_verdict` (`anchors.py:279`) returns
    `pass` whenever the row states the same — the 04:00 timer, every day.
    If `standing_of` read the last non-neutral across all `kind` combined, that
    `pass` would erase the following morning the `stale_condition_met` written at 04:30, and
    standing would depend on whichever timer ran last. For anchored
    capsules — 834 live in the 2026-09-16 sweep — this unit would undo
    itself after a single day.

    And the two events DO NOT contradict each other: an anchor `pass` says the line did not
    change; it does not say that the file named in the condition still exists. One axis
    cannot erase another without refuting it, and that is why failure wins.
    """
    store = Store.open_disposable_test_store()
    cid, _ = store.append(_cap("ancorada"), NOW)
    relatorio = _relatorio([_hit(cid, StalenessVerdict.TRIGGERED, StalenessClass.PATH)])
    record_staleness(store, relatorio, NOW)

    store.anchors.record_verification(
        cid,
        kind="anchor",
        result="pass",
        detail="a linha ancorada nao mudou",
        actor="anchor-check",
        now=LATER,
    )

    st = _standing(store, cid)
    assert st["state"] == "caducou", (
        "an ANCHOR `pass` does not refute a fired staleness condition:"
        f" resolving per axis is what stops the 04:00 timer from erasing the"
        f" 04:30 one — got {st}"
    )
    assert st["reason"] == STALENESS_RESULT, st

    # AND THE FOLLOWING DAY. The idempotence fence must inspect the last
    # event of THIS axis, not the last across all: read against the whole
    # list it saw yesterday's anchor `pass`, concluded that there was new judgment
    # to record, and rewrote the same staleness every 24 hours. With 569
    # triggered conditions that would be 569 rows per day for a truth that has not
    # changed, and `list_verifications` is read per capsule on each `standing_of`.
    outra_vez = record_staleness(store, relatorio, LATER + timedelta(days=1))
    assert outra_vez == 0, (
        "the condition is still fired and was already recorded on THIS axis: the"
        " anchor `pass` in between does not make the firing a new judgement"
    )
    do_eixo = [
        v for v in store.anchors.list_verifications(cid) if v.kind == STALENESS_KIND
    ]
    assert len(do_eixo) == 1, do_eixo


def test_um_eixo_que_falha_ganha_ao_eixo_que_so_nao_conseguiu_ler() -> None:
    """Precedence between axes, and the leg distinguishing it from "last wins".

    `unverifiable` is an ABSENCE of judgment (U1): "cannot read the proof" is never
    "this is false". An axis that ASSERTS something — here the triggered
    condition — is worth more than an axis that could read nothing, whether it arrives
    later or not.

    The pair uses DISTINCT axes on purpose. A curator `unverifiable` on
    top of a `dead_anchor` is NOT this case: there the curator is reviewing that
    finding, and the last judgment of the anchor axis wins — behavior that U1
    pinned and that this unit does not touch.
    """
    store = Store.open_disposable_test_store()
    cid, _ = store.append(_cap("dois-eixos"), NOW)
    record_staleness(
        store,
        _relatorio([_hit(cid, StalenessVerdict.TRIGGERED, StalenessClass.PATH)]),
        NOW,
    )
    store.anchors.record_verification(
        cid,
        kind="curator",
        result="unverifiable",
        detail="nao consegui ler a prova",
        actor="curator",
        now=LATER,
    )

    st = _standing(store, cid)
    assert st["state"] == "caducou", st
    assert st["reason"] == STALENESS_RESULT, st


def test_o_curator_REVE_o_eixo_da_ancora_e_nao_compete_com_ele() -> None:
    """The regression caught by the full suite, caused by my rule.

    A curator `holds` over a `dead_anchor` says "the tree lost the anchor
    AND the capsule remains true". It is a judgment ON that finding, not a
    parallel observation: if it competed with it as its own axis, failure
    would win and the capsule would revert to `caducou` — the exact opposite of what the
    curator exists to do. `test_holds_records_only_an_event` failed because of this, and
    it was right.
    """
    store = Store.open_disposable_test_store()
    cid, _ = store.append(_cap("revista"), NOW)
    store.anchors.record_verification(
        cid,
        kind="anchor",
        result="dead_anchor",
        detail="src/a.py:9 desapareceu",
        actor="anchor-check",
        now=NOW,
    )
    store.anchors.record_verification(
        cid,
        kind="curator",
        result="holds",
        detail="a funcao continua la",
        actor="curator",
        now=LATER,
    )

    assert _standing(store, cid)["state"] == "vale"


def test_o_detalhe_guarda_a_condicao_e_o_estado_observado() -> None:
    """A verdict without a reason forces someone to re-run the evaluator to find out.

    `standing_of` publishes `detail` under the `caducou` branch, so what is written here
    is what the reader sees when asking why the capsule ceased to hold.
    """
    store = Store.open_disposable_test_store()
    cid, _ = store.append(_cap("explicada"), NOW)
    record_staleness(
        store,
        _relatorio([_hit(cid, StalenessVerdict.TRIGGERED, StalenessClass.PATH)]),
        NOW,
    )

    evento = store.anchors.list_verifications(cid)[-1]
    assert evento.actor == "staleness", evento
    assert evento.kind == "staleness", evento
    assert "o ficheiro ancorado desaparecer" in evento.detail, evento
    assert "deleted" in evento.detail, evento


def test_um_disparo_depois_de_uma_recuperacao_e_juizo_NOVO() -> None:
    """The fence is based on the LAST event, not on "one already exists".

    Real case: the condition triggers, someone restores the anchor and writes a `pass`, and
    then the anchor disappears again. That is a NEW judgment — the capsule is
    `vale` right now and must return to `caducou`. An existence fence
    (`any(e.result == STALENESS_RESULT ...)`) saw the old trigger, kept silent, and
    the capsule remained served as valid with the condition triggered: exactly
    the pain this unit exists to close, reintroduced by the idempotence
    fence.
    """
    store = Store.open_disposable_test_store()
    cid, _ = store.append(_cap("recaida"), NOW)
    relatorio = _relatorio([_hit(cid, StalenessVerdict.TRIGGERED, StalenessClass.PATH)])

    record_staleness(store, relatorio, NOW)
    store.anchors.record_verification(
        cid,
        kind=STALENESS_KIND,
        result="pass",
        detail="a condicao deixou de estar disparada",
        actor=STALENESS_ACTOR,
        now=LATER,
    )
    assert _standing(store, cid)["state"] == "vale"

    outra_vez = record_staleness(store, relatorio, LATER + timedelta(days=1))

    assert outra_vez == 1, (
        "the `pass` in between makes the next firing a NEW judgement: staying silent here"
        " leaves the capsule served as valid with the condition fired"
    )
    assert _standing(store, cid)["state"] == "caducou"


def test_um_relatorio_real_tem_hits_misturados_e_todos_os_disparos_escrevem() -> None:
    """All other cases use a ONE-hit report, which conceals the loop.

    A real run has 2606 conditions: 2004 undecidable and 569 triggered,
    ordered by `capsule_id` and therefore INTERLEAVED. Independent review
    (2026-09-19) showed that `continue` replaced by `break` survived all
    seven cases — and in a real report the first non-triggered hit halted
    writing of ALL subsequent ones, silently. What this case locks is that the
    loop continues: each hit is judged independently.
    """
    store = Store.open_disposable_test_store()
    ids = [store.append(_cap(f"misturada-{i}"), NOW)[0] for i in range(5)]
    relatorio = _relatorio(
        [
            _hit(ids[0], StalenessVerdict.UNDECIDABLE, StalenessClass.PROSE),
            _hit(ids[1], StalenessVerdict.TRIGGERED, StalenessClass.PATH),
            _hit(ids[2], StalenessVerdict.NOT_TRIGGERED, StalenessClass.PR),
            _hit(ids[3], StalenessVerdict.TRIGGERED, StalenessClass.SHA),
            _hit(ids[4], StalenessVerdict.UNDECIDABLE, StalenessClass.PROSE),
        ]
    )

    escritos = record_staleness(store, relatorio, NOW)

    assert escritos == 2, (
        "both firings write, and an unfired hit IN BETWEEN cannot"
        f" stop the following ones — got {escritos}"
    )
    estados = {cid: _standing(store, cid)["state"] for cid in ids}
    assert estados[ids[1]] == "caducou", estados
    assert estados[ids[3]] == "caducou", estados
    for i in (0, 2, 4):
        assert estados[ids[i]] == "por_verificar", estados
        assert len(store.anchors.list_verifications(ids[i])) == 0
