"""U3 (nmemory-standing): nothing is served without standing.

Each delivered or retrieved line indicates whether it HOLDS, was CONTRADICTED, EXPIRED, or is
UNCHECKED — derived on read, never stored (precedent
`dod_contradictions`), and kind-aware: a `decision` or `constraint`
holds by existence until an edge contradicts it; `dod`/`task` have their own
lifecycle and do not carry standing; only claims about the world carry the
four states. On day one almost everything reports `por_verificar` — it is the store's
first honest number of self-knowledge, not a defect.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from storefile import fresh_store_path

from nmemory.capture import CaptureContext
from nmemory.database import Database
from nmemory.queries.standing import standing_of
from nmemory.relation import RelationKind
from nmemory.server import MemoryServer
from nmemory.store import Store

T0 = datetime(2026, 9, 16, 16, 0, tzinfo=UTC)
NOW = T0 + timedelta(days=40)


def _ctx(session: str = "sess-standing") -> CaptureContext:
    return CaptureContext(
        harness="pytest",
        model="none",
        cwd="/tmp",
        session_id=session,
        host="t",
        owner="t",
    )


def _fresh() -> tuple[Database, Store, MemoryServer]:
    url = fresh_store_path("stand")
    pg = Database(url)
    store = Store.open(url)
    return pg, store, MemoryServer(store=store, project_id="stand")


def _ingest(
    server: MemoryServer, content: str, kind: str, now: datetime, **extra: object
) -> str:
    args: dict[str, object] = {
        "content": content,
        "source": "t",
        "anchor": content[:16],
        "proof_hint": "fixture: a porta de captura exige prova re-obtenível",
        "kind": kind,
        **extra,
    }
    response = server.call_tool("nmemory_capsule_ingest", args, context=_ctx(), now=now)
    assert not response["result"]["isError"], response
    return str(
        json.loads(response["result"]["content"][0]["text"])["outcomes"][0]["id"]
    )


def _payload(
    server: MemoryServer,
    tool: str,
    args: dict[str, object],
    now: datetime,
    session: str = "sess-standing",
) -> dict:
    response = server.call_tool(tool, args, context=_ctx(session), now=now)
    assert "result" in response, response
    assert not response["result"]["isError"], response
    return dict(json.loads(response["result"]["content"][0]["text"]))


def test_the_four_states_and_the_kind_rule() -> None:
    _, store, server = _fresh()
    decision = _ingest(server, "aardvark decision by the owner", "decision", T0)
    fact_new = _ingest(server, "badger fact never checked", "fact", T0)
    fact_ok = _ingest(server, "civet fact checked and fine", "fact", T0)
    fact_dead = _ingest(server, "dingo fact whose anchor died", "fact", T0)
    fact_old = _ingest(server, "eland fact contradicted later", "fact", T0)
    newer = _ingest(
        server, "the correction of the eland", "fact", T0 + timedelta(minutes=1)
    )
    expiring = _ingest(
        server,
        "fossa fact with an expiry",
        "fact",
        T0,
        valid_to=(T0 + timedelta(days=1)).isoformat(),
    )
    dod = _ingest(server, "gecko dod with its own lifecycle", "dod", T0)
    task = _ingest(server, "hyrax task with its own lifecycle", "task", T0)

    store.anchors.record_verification(
        fact_ok,
        kind="anchor",
        result="pass",
        detail="a.py:1",
        actor="anchor-check",
        now=T0 + timedelta(days=2),
    )
    store.anchors.record_verification(
        fact_dead,
        kind="anchor",
        result="dead_anchor",
        detail="gone.py:1",
        actor="anchor-check",
        now=T0 + timedelta(days=2),
    )
    store.add_relation(
        RelationKind.FALSIFIES, newer, fact_old, now=T0 + timedelta(minutes=2)
    )

    st = standing_of(
        store.db,
        [decision, fact_new, fact_ok, fact_dead, fact_old, expiring, dod, task],
        now=NOW,
    )
    assert st[decision]["state"] == "vale", "a decision holds by existing"
    assert st[fact_new]["state"] == "por_verificar" and st[fact_new]["age_days"] == 40
    assert st[fact_ok]["state"] == "vale" and st[fact_ok]["by"] == "anchor-check"
    assert (
        st[fact_dead]["state"] == "caducou" and st[fact_dead]["reason"] == "dead_anchor"
    )
    assert st[fact_old]["state"] == "contradita" and st[fact_old]["by"] == newer
    assert st[expiring]["state"] == "caducou" and st[expiring]["reason"] == "valid_to"
    assert dod not in st and task not in st, "dod e task têm ciclo de vida próprio"


def test_a_revoked_contradiction_restores_the_previous_standing() -> None:
    _, store, server = _fresh()
    old = _ingest(server, "ibex fact", "fact", T0)
    new = _ingest(
        server, "the correction of the ibex", "fact", T0 + timedelta(minutes=1)
    )
    store.add_relation(RelationKind.FALSIFIES, new, old, now=T0 + timedelta(minutes=2))
    assert standing_of(store.db, [old], now=NOW)[old]["state"] == "contradita"
    store.revoke_relation(
        RelationKind.FALSIFIES,
        new,
        old,
        actor="owner",
        reason="wrong",
        now=T0 + timedelta(minutes=3),
    )
    assert standing_of(store.db, [old], now=NOW)[old]["state"] == "por_verificar"


def test_every_served_row_carries_its_standing() -> None:
    _, store, server = _fresh()
    fact = _ingest(server, "jackal fact served on every surface", "fact", T0)
    contradicted = _ingest(server, "kudu fact that was later falsified", "fact", T0)
    newer = _ingest(
        server, "the correction of the kudu", "fact", T0 + timedelta(minutes=1)
    )
    store.add_relation(
        RelationKind.FALSIFIES, newer, contradicted, now=T0 + timedelta(minutes=2)
    )

    # The inbox FIRST: get/retrieve/list serve, and what is served leaves the inbox by
    # design (U6 of the curator) — the same trap that U1 had already caught.
    reader = MemoryServer(store=Store.open(store.db.path), project_id="stand")
    # OWN session: a session never receives what it wrote itself (U6 fence)
    ctx = _payload(
        reader,
        "nmemory_context_show",
        {"query": "anything"},
        NOW,
        session="sess-reader",
    )
    inbox = {r["id"]: r for r in ctx.get("inbox", [])}
    assert fact in inbox and inbox[fact]["standing"]["state"] == "por_verificar"

    got = _payload(server, "nmemory_capsule_show", {"id": fact}, NOW)
    assert got["standing"]["state"] == "por_verificar"
    got_c = _payload(server, "nmemory_capsule_show", {"id": contradicted}, NOW)
    assert got_c["standing"] == {
        "state": "contradita",
        "by": newer,
        "edge": "falsifies",
    }

    ret = _payload(server, "nmemory_capsule_retrieve", {"terms": ["jackal"]}, NOW)
    assert ret["results"] and ret["results"][0]["standing"]["state"] == "por_verificar"

    lst = _payload(server, "nmemory_capsule_list", {}, NOW)
    by_id = {r["id"]: r for r in lst["entries"]}
    assert by_id[fact]["standing"]["state"] == "por_verificar"


def test_unverifiable_nao_caduca_mas_tambem_nao_vale() -> None:
    """SUPERSEDES the version I wrote in #674. There, this test asserted that an
    old `pass` survived a recent `unverifiable`, because I had placed
    `unverifiable` in `VERIFICATION_NEUTRAL` and neutral is traversed.

    It was wrong in both directions. Neutral did not heal history (an
    old `dead_anchor` from the WRONG rule survived fixing the rule), and
    serving `vale` from a `pass` that can no longer be repeated is
    asserting current validity using a proof that stopped running.

    What remains: `unverifiable` is neither `caducou` ("was true and stopped being so")
    nor `vale` — it is `por_verificar`, which is what is actually known."""
    _pg, store, server = _fresh()
    cid = _ingest(server, "a claim about an artefact", "fact", T0)
    store.anchors.record_verification(
        cid,
        kind="anchor",
        result="pass",
        detail="src/a.py:1",
        actor="anchor-check",
        now=T0,
    )
    assert standing_of(store.db, [cid], now=T0)[cid]["state"] == "vale"
    store.anchors.record_verification(
        cid,
        kind="anchor",
        result="unverifiable",
        detail="docs/qa/raw/review-366.md",
        actor="anchor-check",
        now=T0,
    )
    st = standing_of(store.db, [cid], now=T0)[cid]
    assert st["state"] == "por_verificar" and st["reason"] == "unverifiable", st


def test_revogar_o_cerco_liberta_a_capsula() -> None:
    """The act of revocation MUST be visible on read, otherwise it is cosmetic.

    Measured on the live store on 2026-09-18: I revoked 73 fabricated `falsifies` edges
    over `<file missing>` and `standing_of` continued to publish
    `caducou reason=falsified` on the 49 that had standing — because it looks for the
    last NON-NEUTRAL event in the entire history, and a `curator/falsified` is
    permanent. Writing a neutral event over it is inert.

    A `curator/falsified` asserts "an evidence capsule refutes this one". Without
    a LIVE `falsifies` edge pointing to it, the assertion no longer holds."""
    _pg, store, server = _fresh()
    cid = _ingest(server, "a claim the curator fenced", "fact", T0)
    evid = _ingest(server, "the evidence for the fence", "fact", T0)
    store.anchors.record_verification(
        cid,
        kind="anchor",
        result="pass",
        detail="src/a.py:1",
        actor="anchor-check",
        now=T0,
    )
    store.add_relation(RelationKind.FALSIFIES, evid, cid, origin="curator", now=T0)
    store.anchors.record_verification(
        cid,
        kind="curator",
        result="falsified",
        detail="dead_anchor at docs/qa/raw/review-366.md|evidencia",
        actor="curator",
        now=T0,
    )
    # With a LIVE edge the fence comes from the edge: `contradita`.
    assert standing_of(store.db, [cid], now=T0)[cid]["state"] == "contradita"

    store.revoke_relation(
        RelationKind.FALSIFIES,
        evid,
        cid,
        actor="nott",
        reason="dead_anchor nao e refutacao",
        now=T0,
    )
    # Once the edge is revoked, `contradita` clears — but the `curator/falsified` event
    # remains in history and `standing_of` falls back on it, publishing
    # `caducou reason=falsified`. It was THIS that I measured on the live store: the 49 capsules
    # shifted from `contradita` to `caducou reason=falsified`, and writing a
    # neutral event on top is inert because it seeks the last NON-NEUTRAL.
    #
    # A `curator/falsified` asserts "an evidence capsule refutes this one". Without
    # a live edge pointing to it, the assertion does not hold and is not a verdict.
    st = standing_of(store.db, [cid], now=T0)[cid]
    assert st["state"] == "vale", st


def test_ser_digerida_nao_caduca_a_capsula() -> None:
    """`digest_scope` records four results on the FIRST member of the cluster, and
    none was in PASSING nor in NEUTRAL — so `standing_of` published
    `caducou reason=digested` because the capsule had been DIGESTED, which is a good
    outcome, not a failure. Same defect as the orphan `falsified`, through another door.

    Measured on the live store on 2026-09-18: `curator/digested` was the last event of
    a live capsule, and three had `digest_*` events."""
    _pg, store, server = _fresh()
    for resultado in (
        "digested",
        "digest_declined",
        "digest_refused",
        "digest_deduped",
    ):
        cid = _ingest(server, f"uma afirmacao {resultado}", "fact", T0)
        store.anchors.record_verification(
            cid,
            kind="anchor",
            result="pass",
            detail="src/a.py:1",
            actor="anchor-check",
            now=T0,
        )
        store.anchors.record_verification(
            cid,
            kind="curator",
            result=resultado,
            detail="digest|3 membros",
            actor="curator",
            now=T0,
        )
        st = standing_of(store.db, [cid], now=T0)[cid]
        assert st["state"] == "vale", (resultado, st)


def test_reanchored_vale_e_distingue_o_filtro() -> None:
    """Without this assertion, replacing the filter with `c.result in VERIFICATION_PASSING`
    or `c.kind != "curator"` would pass the entire suite: no test measured
    standing after `reanchored`. Finding from the review of #677."""
    _pg, store, server = _fresh()
    cid = _ingest(server, "uma afirmacao re-ancorada", "fact", T0)
    store.anchors.record_verification(
        cid,
        kind="curator",
        result="reanchored",
        detail="moved_anchor at src/a.py:1|src/a.py:7",
        actor="curator",
        now=T0,
    )
    st = standing_of(store.db, [cid], now=T0)[cid]
    assert st["state"] == "vale" and st["by"] == "curator", st


def test_unverifiable_recente_cura_um_dead_anchor_antigo() -> None:
    """I CORRECT A CHOICE OF MINE FROM #674. There I put `unverifiable` into
    `VERIFICATION_NEUTRAL` so that it would stop publishing `caducou`. Neutral is not
    enough: `standing_of` traverses it and hits the older `dead_anchor`, so
    a corrected rule never undoes a past erroneous verdict.

    Measured on the live store on 2026-09-18: of the 49 capsules still fenced after
    revoking the curator's fence, the new rule reports `unverifiable` for 30 — which are
    session artifacts this tree NEVER tracked — and `dead_anchor` for 19,
    which the tree indeed lost. Without this change, those 30 would remain
    `caducou reason=dead_anchor` forever.

    `skipped_taint` and `error` remain neutral: they are operational and
    transient. `unverifiable` is STRUCTURAL — it states "this anchor is not
    judgeable here", which is an assertion about the present."""
    _pg, store, server = _fresh()
    cid = _ingest(server, "a claim about a session artefact", "fact", T0)
    store.anchors.record_verification(
        cid,
        kind="anchor",
        result="dead_anchor",
        detail="docs/qa/raw/review-366.md",
        actor="anchor-check",
        now=T0,
    )
    assert standing_of(store.db, [cid], now=T0)[cid]["reason"] == "dead_anchor"
    store.anchors.record_verification(
        cid,
        kind="anchor",
        result="unverifiable",
        detail="docs/qa/raw/review-366.md",
        actor="anchor-check",
        now=T0 + timedelta(days=1),
    )
    st = standing_of(store.db, [cid], now=T0 + timedelta(days=1))[cid]
    assert st["state"] == "por_verificar", st


def test_um_pass_mais_recente_vence_o_unverifiable() -> None:
    """Direction matters in both senses: the file reappeared and passed."""
    _pg, store, server = _fresh()
    cid = _ingest(server, "a claim that became verifiable again", "fact", T0)
    store.anchors.record_verification(
        cid,
        kind="anchor",
        result="unverifiable",
        detail="src/a.py:1",
        actor="anchor-check",
        now=T0,
    )
    store.anchors.record_verification(
        cid,
        kind="anchor",
        result="pass",
        detail="src/a.py:1",
        actor="anchor-check",
        now=T0 + timedelta(days=1),
    )
    st = standing_of(store.db, [cid], now=T0 + timedelta(days=1))[cid]
    assert st["state"] == "vale", st


def test_a_idade_do_unverifiable_conta_da_varredura_nao_da_criacao() -> None:
    """Finding from review of #678: the branch had `last_check.at` at hand and counted
    from `valid_from`. A capsule created 300 days ago and swept yesterday reported
    an age of 300 days, and did not emit `verified_at` like the other branches."""
    _pg, store, server = _fresh()
    cid = _ingest(server, "uma afirmacao antiga", "fact", T0 - timedelta(days=300))
    store.anchors.record_verification(
        cid,
        kind="anchor",
        result="unverifiable",
        detail="docs/qa/raw/review-366.md",
        actor="anchor-check",
        now=T0 - timedelta(days=1),
    )
    st = standing_of(store.db, [cid], now=T0)[cid]
    assert st["age_days"] == 1, st
    assert st["verified_at"].startswith("2026-09-15"), st
    assert st["detail"] == "docs/qa/raw/review-366.md", st


def test_uma_falsifies_viva_cerca_mesmo_com_o_alvo_em_unverifiable() -> None:
    """RETAINED from #682: `standing_of` CANNOT silence a live edge.

    I tried the reverse — a guard that, with the target at `unverifiable`, discarded
    `falsifies` before `if inbound`. Refuted under controlled testing on 2026-09-19, and
    for two independent reasons:

    1. **The ladder.** `STANDING_BY_EXISTENCE` and the `valid_to` branch come AFTER
       `if inbound`. Emptying `inbound` caused a `decision` refuted MANUALLY by the
       owner to drop to `vale reason=by_existence` — served as VALID. Measured:
       with the guard `{'state': 'vale', 'reason': 'by_existence'}`, without it
       `{'state': 'contradita', ...}`. The original falsifier only used `fact`,
       which is not in the set, and thus saw nothing.
    2. **Two truths.** The live edge is a fence across six readers that `standing_of`
       does not touch (`retrieve.py` via `is_falsified`, the CTE `retired` in `store.py`,
       `scoped` in `server.py`, `recall_judge.py`, and the anchor sweep that
       SKIPS the target). The capsule would report `por_verificar` without being served on
       any surface, and nothing would verify it.

    The correct remedy is revoking the edge — revocation is already respected by all seven
    readers at once. That is what `reject_unsupported_evidence` does.

    The three branches are those that the guard broke: `decision` (existence),
    `valid_to` (expiry) and `fact` (the common case).
    """
    _pg, store, server = _fresh()
    decisao = _ingest(server, "a decision the owner refuted", "decision", T0)
    com_prazo = _ingest(
        server,
        "a fact with a deadline, refuted",
        "fact",
        T0,
        valid_to=(T0 + timedelta(days=1)).isoformat(),
    )
    simples = _ingest(server, "a simple fact, refuted", "fact", T0)
    refuta = _ingest(server, "a refutacao a mao do owner", "fact", T0)

    for cid in (decisao, com_prazo, simples):
        store.anchors.record_verification(
            cid,
            kind="anchor",
            result="unverifiable",
            detail="docs/qa/raw/foo.md",
            actor="anchor-check",
            now=T0 + timedelta(days=1),
        )
        store.add_relation(
            RelationKind.FALSIFIES, refuta, cid, now=T0 + timedelta(days=2)
        )

    st = standing_of(store.db, [decisao, com_prazo, simples], now=NOW)
    for cid in (decisao, com_prazo, simples):
        assert st[cid] == {
            "state": "contradita",
            "by": refuta,
            "edge": "falsifies",
        }, (
            f"{cid}: a live edge fences, and the target's `unverifiable` does not erase it"
        )
