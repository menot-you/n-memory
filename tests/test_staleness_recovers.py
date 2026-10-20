"""A condition that ceased to fire returns the capsule to service.

The independent review of the fences campaign (2026-09-20) caught an ABSORBING
state: `record_staleness` was the only writer on the `staleness` axis and
always wrote `stale_condition_met`; nobody, in any tree, wrote a
`pass` on that axis. The curator's `holds` is mapped to the `anchor` axis
(`store.standing_of`), so it does not reach this one.

While `standing_of` was just a field in the envelope this was ugly but inert.
With U1's `lapsed` fence it became a perpetual sentence: a capsule
whose condition fired ONCE was permanently removed from service by omission, and
this house's promise — hide, do not delete, with a later passing event
restoring it — applied only to the anchor axis. `record_staleness` itself promised
"a `pass` that recovered it" with nothing producing it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from storefile import fresh_store_path

from nmemory.queries.standing import standing_of
from nmemory.retrieve import RetrieveParams, retrieve
from nmemory.server import MemoryServer
from nmemory.staleness import (
    STALENESS_KIND,
    StalenessClass,
    StalenessHit,
    StalenessReport,
    StalenessVerdict,
    record_staleness,
)
from nmemory.store import Store


def _report(verdict: StalenessVerdict) -> StalenessReport:
    hit = StalenessHit(
        capsule_id="cap-1",
        stale_if="o ficheiro alvo existe",
        class_=StalenessClass.PATH,
        entity="alvo.py",
        verdict=verdict,
        observed_state="present" if verdict is StalenessVerdict.TRIGGERED else "absent",
    )
    triggered = verdict is StalenessVerdict.TRIGGERED
    return StalenessReport(
        total_evaluated=1,
        resolvable_count=1,
        prose_count=0,
        triggered_count=1 if triggered else 0,
        not_triggered_count=0 if triggered else 1,
        undecidable_count=0,
        hits=[hit],
    )


def _open() -> tuple[Store, MemoryServer]:
    url = fresh_store_path("stalerecupera")
    store = Store.open(url)
    server = MemoryServer(store=store)
    response = server.call_tool(
        "nmemory_capsule_ingest",
        {
            "content": "staleprobe a claim with a staleness condition",
            "source": "test_staleness_recovers",
            "anchor": "a:1",
            "proof_hint": "fixture: a porta de captura exige prova re-obtenível",
            "kind": "fact",
        },
    )
    assert not response["result"]["isError"], response
    return store, server


def _served(store: Store) -> list[str]:
    result = retrieve(store, RetrieveParams(query="staleprobe", lane="term"))
    return sorted(e["id"] for e in result["evidence"])


def test_uma_condicao_que_deixou_de_disparar_repoe_a_capsula_no_servico() -> None:
    store, _ = _open()
    now = datetime.now(UTC)

    # Fires: the capsule lapses and U1 fence removes it from service.
    assert record_staleness(store, _report(StalenessVerdict.TRIGGERED), now) == 1
    assert standing_of(store.db, ["cap-1"], now=now)["cap-1"]["state"] == "caducou"
    assert _served(store) == []

    # Stops firing: it is a NEW judgment on the same axis, and must be written.
    depois = now + timedelta(days=1)
    assert record_staleness(store, _report(StalenessVerdict.NOT_TRIGGERED), depois) == 1
    assert standing_of(store.db, ["cap-1"], now=depois)["cap-1"]["state"] == "vale"
    assert _served(store) == ["cap-1"]


def test_nao_dispara_e_nunca_disparou_NAO_escreve_nada() -> None:
    """Recovery cannot turn into one row per capsule per day.

    The axis is append-only and `standing_of` reads `list_verifications` per capsule on
    each service: writing a `pass` for one that never lapsed would cause daily sweeping
    to grow the table for a truth that nobody contested. There is only a new verdict
    when the PREVIOUS state of this axis was firing.
    """
    store, _ = _open()
    now = datetime.now(UTC)

    assert record_staleness(store, _report(StalenessVerdict.NOT_TRIGGERED), now) == 0
    assert [
        v for v in store.anchors.list_verifications("cap-1") if v.kind == STALENESS_KIND
    ] == []
    assert standing_of(store.db, ["cap-1"], now=now)["cap-1"]["state"] != "caducou"


def test_o_pass_nao_se_repete_enquanto_a_condicao_continuar_calma() -> None:
    """The same firing idempotence fence, on the other side."""
    store, _ = _open()
    now = datetime.now(UTC)

    record_staleness(store, _report(StalenessVerdict.TRIGGERED), now)
    assert record_staleness(store, _report(StalenessVerdict.NOT_TRIGGERED), now) == 1
    assert record_staleness(store, _report(StalenessVerdict.NOT_TRIGGERED), now) == 0
    eixo = [
        v for v in store.anchors.list_verifications("cap-1") if v.kind == STALENESS_KIND
    ]
    assert [v.result for v in eixo] == ["stale_condition_met", "pass"]
