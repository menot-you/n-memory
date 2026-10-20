"""The staged queue, and the distinction imposed by measurements on the live store.

Measured on 2026-09-17 on the owner's store: 110 capsules with current review
verdict `proposed`, **none** ever ratified or rejected in seven weeks.
However, the 110 are not the same thing:

    curator evidence        90   ← fenced BY DESIGN; it is the curator's audit
                                  trail, and was never meant to be ratified
    staged ingest           20   ← proposal awaiting judgment (2 with >14 days)

An expiration treating all 110 as a queue would erase curator evidence. That
is why this census separates the two BEFORE counting, and the discriminator is the
reason recorded in the event, which is now named in a single place instead of being a
literal in two distant locations.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from storefile import fresh_store_path

from nmemory.capsule import (
    AuthorityClass,
    Capsule,
    Freshness,
    Provenance,
    Scope,
    sha256_hex,
)
from nmemory.curator import CURATOR_ACTOR
from nmemory.queries.staged import (
    REVIEW_REASON_EVIDENCE,
    REVIEW_REASON_PROPOSAL,
    REVIEW_REASON_REVOKED,
    REVIEW_VERDICT_REJECTED,
    STAGED_HORIZON_DAYS,
    staged_census,
    standing_proposal,
)
from nmemory.relation import RelationKind
from nmemory.store import Store

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)


def _store() -> Store:
    url = fresh_store_path("staged")

    return Store.open(url)


import pytest


@pytest.fixture
def store_and_now() -> tuple[Store, datetime]:
    return _store(), NOW


def _cap(store: Store, marca: str, project: str, born: datetime) -> str:
    content = f"conteudo de {marca}"
    cap = Capsule(
        content=content,
        provenance=Provenance(
            source="t",
            anchor="src/a.py:1",
            source_hash=sha256_hex(content.encode("utf-8")),
        ),
        confidence=0.9,
        freshness=Freshness(valid_from=born),
        scope=Scope(project_id=project),
        authority_class=AuthorityClass.USER_STATED,
        instruction_taint=False,
    )
    cid, _ = store.append(cap, born)
    return str(cid)


def test_a_evidencia_do_curator_nunca_e_fila(
    store_and_now: tuple[Store, datetime],
) -> None:
    """90 of the 110 measured are this. Counting them as queue put the curator's
    audit trail in the crosshairs of expiration — deleting evidence to lower a
    number is the worst possible trade-off."""
    store, now = store_and_now
    old = now - timedelta(days=STAGED_HORIZON_DAYS + 40)
    cid = _cap(store, "c", "pynott", old)
    store.reviews.append_review_event(
        cid, "proposed", REVIEW_REASON_EVIDENCE, CURATOR_ACTOR, old
    )
    census = staged_census(store.db, "pynott", subtree=True, now=now)
    assert census.evidence == 1
    assert census.awaiting == 0 and census.expired == 0


def test_uma_proposta_velha_expira_e_uma_nova_espera(
    store_and_now: tuple[Store, datetime],
) -> None:
    store, now = store_and_now
    old = now - timedelta(days=STAGED_HORIZON_DAYS + 1)
    fresh = now - timedelta(days=1)
    velha = _cap(store, "v", "pynott", old)
    nova = _cap(store, "n", "pynott", fresh)
    store.reviews.append_review_event(
        velha, "proposed", REVIEW_REASON_PROPOSAL, "judge", old
    )
    store.reviews.append_review_event(
        nova, "proposed", REVIEW_REASON_PROPOSAL, "judge", fresh
    )
    census = staged_census(store.db, "pynott", subtree=True, now=now)
    assert (census.awaiting, census.expired, census.evidence) == (2, 1, 0)
    assert census.oldest_days is not None and census.oldest_days > STAGED_HORIZON_DAYS


def test_o_limiar_e_inclusivo_na_borda(store_and_now: tuple[Store, datetime]) -> None:
    """Exactly at the horizon it has ALREADY expired: a `<` instead of `<=` would cause
    the queue to always retain a row that never expires."""
    store, now = store_and_now
    borda = now - timedelta(days=STAGED_HORIZON_DAYS)
    cid = _cap(store, "b", "pynott", borda)
    store.reviews.append_review_event(
        cid, "proposed", REVIEW_REASON_PROPOSAL, "judge", borda
    )
    assert staged_census(store.db, "pynott", subtree=True, now=now).expired == 1


def test_um_veredito_posterior_tira_a_capsula_da_fila(
    store_and_now: tuple[Store, datetime],
) -> None:
    """The CURRENT verdict is the last one by `seq`. A proposal ratified afterwards
    ceases to count — otherwise the census measured writes, not state."""
    store, now = store_and_now
    old = now - timedelta(days=STAGED_HORIZON_DAYS + 5)
    cid = _cap(store, "r", "pynott", old)
    store.reviews.append_review_event(
        cid, "proposed", REVIEW_REASON_PROPOSAL, "judge", old
    )
    assert staged_census(store.db, "pynott", subtree=True, now=now).expired == 1
    store.reviews.append_review_event(cid, "ratified", "revisto", "owner", now)
    census = staged_census(store.db, "pynott", subtree=True, now=now)
    assert (census.awaiting, census.expired) == (0, 0)


def test_a_cerca_de_escopo_e_por_subarvore(
    store_and_now: tuple[Store, datetime],
) -> None:
    """`pynott` reaches `pynott/x` and never `nottx` — the same fence as the rest of
    the store, not a second derivation of it."""
    store, now = store_and_now
    fresh = now - timedelta(days=1)
    dentro = _cap(store, "d", "pynott/tech", fresh)
    fora = _cap(store, "f", "nottx", fresh)
    for cid in (dentro, fora):
        store.reviews.append_review_event(
            cid, "proposed", REVIEW_REASON_PROPOSAL, "judge", fresh
        )
    assert staged_census(store.db, "pynott", subtree=True, now=now).awaiting == 1
    assert staged_census(store.db, "nottx", subtree=True, now=now).awaiting == 1


def test_uma_capsula_esquecida_sai_da_fila_porque_ninguem_a_pode_drenar(
    store_and_now: tuple[Store, datetime],
) -> None:
    """Finding from independent review (fugu, 2026-09-17), and it is the worst this
    census could have: `forget_capsule` nullifies `canonical_json` and KEEPS the row in
    `nmemory.capsules`, and nothing in the repository deletes from `review_events`. Without
    fencing, a forgotten capsule's proposal would be counted forever —
    `nmemory_capsule_list review_state=proposed` goes through `store.list()`, which filters
    tombstones, and reading raises `Tombstoned`. The number that promises a CEILING
    would count exactly what nobody can drain, never returning to
    zero no matter how much it is judged.

    The fence is the tombstone TABLE and not just the column: `store.py:838-849`
    explains that `canonical_json IS NULL` only describes what
    `forget_capsule` does today, and any other path inserting a tombstone
    without nulling the column would return the id again.
    """
    store, now = store_and_now
    old = now - timedelta(days=STAGED_HORIZON_DAYS + 2)
    vivo = _cap(store, "vivo", "pynott", old)
    morto = _cap(store, "morto", "pynott", old)
    for cid in (vivo, morto):
        store.reviews.append_review_event(
            cid, "proposed", REVIEW_REASON_PROPOSAL, "judge", old
        )
    store.capsules.forget_capsule(morto, "purged", "fora da fila", b"k" * 32, now)
    census = staged_census(store.db, "pynott", subtree=True, now=now)
    assert (census.awaiting, census.expired) == (1, 1), (
        "a esquecida continua na fila: o censo conta o indrenável"
    )


def test_a_borda_pina_a_direcao_e_nao_so_a_igualdade(
    store_and_now: tuple[Store, datetime],
) -> None:
    """Second finding from the same review: the boundary falsifier passed
    equally well with INVERTED comparison, because it only asserted the equality case.
    Here the edge and the inner side enter the SAME assertion, which is
    what eliminates `>=` rather than accidentally relying on another test."""
    store, now = store_and_now
    na_borda = now - timedelta(days=STAGED_HORIZON_DAYS)
    dentro = na_borda + timedelta(seconds=1)
    a = _cap(store, "borda", "pynott", na_borda)
    b = _cap(store, "dentro", "pynott", dentro)
    store.reviews.append_review_event(
        a, "proposed", REVIEW_REASON_PROPOSAL, "judge", na_borda
    )
    store.reviews.append_review_event(
        b, "proposed", REVIEW_REASON_PROPOSAL, "judge", dentro
    )
    census = staged_census(store.db, "pynott", subtree=True, now=now)
    assert (census.awaiting, census.expired) == (2, 1), (
        "the edge expires and one second younger waits"
    )


def test_sem_escopo_o_censo_conta_a_loja_toda(
    store_and_now: tuple[Store, datetime],
) -> None:
    """`nmemory digest show` without `--project-prefix` means "all projects", and
    that is the invocation every session reads at startup. Passing EMPTY scope
    in that position is not "all": it is scope `""`, which reaches no
    project at all — measured on the live store, the line simply did not appear. A number that
    disappears on default invocation puts a ceiling on nothing.
    """
    store, now = store_and_now
    old = now - timedelta(days=STAGED_HORIZON_DAYS + 5)
    for projeto in ("pynott", "menot-you/nott", "outro"):
        cid = _cap(store, projeto, projeto, old)
        store.reviews.append_review_event(
            cid, "proposed", REVIEW_REASON_PROPOSAL, "judge", old
        )
    assert staged_census(store.db, "pynott", subtree=True, now=now).awaiting == 1
    todos = staged_census(store.db, None, subtree=True, now=now)
    assert (todos.awaiting, todos.expired) == (3, 3)


def test_a_expiracao_drena_a_fila_e_nunca_toca_a_evidencia(
    store_and_now: tuple[Store, datetime],
) -> None:
    """The relief valve for the number the census measures. Without it, the census is a thermometer:
    the queue grows, `expired` rises, and nothing drains it.

    What expiration DOES NOT do is essential: it applies nothing (the capsule
    remains fenced, because the verdict is not `ratified`) and does not delete
    curator evidence, which is born `proposed` on purpose so as not to be
    served, and was never meant to be judged.
    """
    from nmemory.queries.staged import REVIEW_VERDICT_EXPIRED

    store, now = store_and_now
    velha = now - timedelta(days=STAGED_HORIZON_DAYS + 3)
    nova = now - timedelta(days=2)
    a = _cap(store, "a", "pynott", velha)
    b = _cap(store, "b", "pynott", nova)
    prova = _cap(store, "p", "pynott", velha)
    store.reviews.append_review_event(
        a, "proposed", REVIEW_REASON_PROPOSAL, "judge", velha
    )
    store.reviews.append_review_event(
        b, "proposed", REVIEW_REASON_PROPOSAL, "judge", nova
    )
    store.reviews.append_review_event(
        prova, "proposed", REVIEW_REASON_EVIDENCE, CURATOR_ACTOR, velha
    )

    before = staged_census(store.db, "pynott", subtree=True, now=now)
    assert (before.awaiting, before.expired, before.evidence) == (2, 1, 1)

    caducadas = store.expire_staged_proposals(
        "pynott", subtree=True, now=now, actor=CURATOR_ACTOR
    )
    assert caducadas == [a], "only the one past the horizon, and never the evidence"

    after = staged_census(store.db, "pynott", subtree=True, now=now)
    assert (after.awaiting, after.expired) == (1, 0), (
        "the queue drained and the number went back to zero: that is what makes it a ceiling"
    )
    assert after.evidence == 1, "the curator's evidence stayed intact"
    assert store.reviews.review_verdict(a) == REVIEW_VERDICT_EXPIRED
    assert a in store.reviews.list_review_fenced(), (
        "expiring does not promote: the capsule stays fenced off from the truth"
    )


def test_expirar_duas_vezes_nao_escreve_duas_vezes(
    store_and_now: tuple[Store, datetime],
) -> None:
    """Idempotent by construction, not by a guard: after the first
    pass, the current verdict is no longer `proposed`, so the query does not
    select it. This matters because the action runs in a 60 s loop."""
    store, now = store_and_now
    velha = now - timedelta(days=STAGED_HORIZON_DAYS + 9)
    cid = _cap(store, "id", "pynott", velha)
    store.reviews.append_review_event(
        cid, "proposed", REVIEW_REASON_PROPOSAL, "judge", velha
    )
    primeira = store.expire_staged_proposals(
        "pynott", subtree=True, now=now, actor=CURATOR_ACTOR
    )
    segunda = store.expire_staged_proposals(
        "pynott", subtree=True, now=now, actor=CURATOR_ACTOR
    )
    assert primeira == [cid] and segunda == []


def test_a_evidencia_cuja_aresta_foi_revogada_ja_nao_sustenta_nada(
    store_and_now: tuple[Store, datetime],
) -> None:
    """U6(b): the mechanical judge of the queue, DERIVED from state and not a threshold.

    A curator evidence capsule exists to support ONE `falsifies`
    edge. When the edge is revoked, the proposal supports nothing — and rejecting it
    states that, rather than deleting proof: the event is append-only, the capsule remains
    fenced (`list_review_fenced` fences everything that is not `ratified`), and the owner
    ratifying it restores it, because the verdict that counts is the last one by `seq`.

    MEASURED on the live store on 2026-09-19: of the 102 `falsifies` edges, 91 are
    revoked (18 on 17/Sep, 73 on 18/Sep) and 4 remain live with target
    `moved_anchor`. The 95 evidence capsules are precisely the origins of 95
    of those edges. The criterion covers those that lost the edge and DOES NOT touch the
    others.

    Four discriminating branches, none of which is decorative:
      - `sem_base`:    revoked edge           → rejects
      - `com_base`:    LIVE edge              → remains `proposed`
      - `sem_aresta`:  never had an edge      → remains `proposed` (nothing was
                       revoked; without this branch, rejecting the entire queue would pass)
      - `proposta`:    `staged ingest` with revoked edge → remains `proposed`
                       (the owner's queue does not belong to this judge)
    """
    store, now = store_and_now
    born = now - timedelta(days=2)
    alvo = _cap(store, "alvo", "pynott", born)

    sem_base = _cap(store, "sem-base", "pynott", born)
    por_existencia = _cap(store, "por-existencia", "pynott", born)
    com_base = _cap(store, "com-base", "pynott", born)
    sem_aresta = _cap(store, "sem-aresta", "pynott", born)
    proposta = _cap(store, "proposta", "pynott", born)

    for cid, reason in (
        (sem_base, REVIEW_REASON_EVIDENCE),
        (com_base, REVIEW_REASON_EVIDENCE),
        (sem_aresta, REVIEW_REASON_EVIDENCE),
        (proposta, REVIEW_REASON_PROPOSAL),
        (por_existencia, REVIEW_REASON_EVIDENCE),
    ):
        store.reviews.append_review_event(cid, "proposed", reason, CURATOR_ACTOR, born)

    # The judge decides by the event REASON and edge state, never by
    # kind: evidence classified under a `STANDING_BY_EXISTENCE` kind does not
    # escape because of that. The lesson comes from #682 -- it was precisely the kind ladder that
    # refuted the guard this branch replaces.
    store.set_classification(por_existencia, "decision", "project", born)

    for cid in (sem_base, com_base, proposta, por_existencia):
        store.add_relation(RelationKind.FALSIFIES, cid, alvo, now=born)
    for cid in (sem_base, proposta, por_existencia):
        store.revoke_relation(
            RelationKind.FALSIFIES,
            cid,
            alvo,
            actor=CURATOR_ACTOR,
            reason="prova ausente",
            now=born + timedelta(hours=1),
        )

    rejeitadas = store.reject_unsupported_evidence(
        "pynott", subtree=True, now=now, actor=CURATOR_ACTOR
    )
    assert rejeitadas == sorted([sem_base, por_existencia]), rejeitadas
    assert store.reviews.review_verdict(sem_base) == "rejected"
    assert store.reviews.review_verdict(com_base) == "proposed"
    assert store.reviews.review_verdict(sem_aresta) == "proposed"
    assert store.reviews.review_verdict(proposta) == "proposed"
    assert store.reviews.review_verdict(por_existencia) == "rejected", (
        "o kind nao isenta"
    )

    # The reason is WRITTEN and names the revocation. Without this assertion the mutant
    # replacing `REVIEW_REASON_REVOKED` with `REVIEW_REASON_HORIZON` in the call
    # survives, and the queue would say "expired by age" about a capsule
    # having nothing to do with age — reason is the only place where the owner reads
    # WHY the judge decided.
    with store.db.writing() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT verdict, reason FROM review_events "
            "WHERE capsule_id = ? ORDER BY seq DESC LIMIT 1",
            (sem_base,),
        )
        ultimo = cur.fetchone()
    assert ultimo == (REVIEW_VERDICT_REJECTED, REVIEW_REASON_REVOKED), ultimo
    # And the capsule remains fenced.
    assert sem_base in store.reviews.list_review_fenced()

    # Idempotent: the verdict that counts is the last one, so the second pass does not
    # re-emit. Without this, the curator would re-write the same rejection every 60 s.
    segunda = store.reject_unsupported_evidence(
        "pynott", subtree=True, now=now, actor=CURATOR_ACTOR
    )
    assert segunda == [], segunda


def test_o_owner_derruba_a_rejeicao_mecanica_pela_porta_do_produto(
    store_and_now: tuple[Store, datetime],
) -> None:
    """The reversibility this unit PROMISES must exist through the port.

    Independent review of #682 (2026-09-19) caught the false promise: the
    docstring, changelog, and comment claimed "the owner ratifying it restores it",
    yet the product's only write port rejected it. `nmemory_proposal_review`
    (`server.py:3636`) queries `standing_proposal`, which after rejection
    returned `"rejected"` and aborted the call; there is no CLI command that writes
    `ratified` (`cli.py:634` delegates to the same port). The rejection could only be undone
    manually against the database.

    This matters because the safety argument for this mechanical judge is
    entirely reversibility: it decides WITHOUT a model and WITHOUT an owner, and what
    makes it admissible is that the owner can override it. An automatic, irreversible
    verdict is not a judge; it is a sentence.

    The `outra_rejeicao` branch is the discriminator preventing lazy remediation: opening
    `standing_proposal` to EVERY `rejected` would also restore what the owner rejected
    manually and what another rule rejected. Opening is solely for the pair
    (`rejected`, `falsifies revoked`) that THIS judge writes.
    """
    store, now = store_and_now
    born = now - timedelta(days=2)
    alvo = _cap(store, "alvo-rev", "pynott", born)
    sem_base = _cap(store, "sem-base-rev", "pynott", born)
    outra_rejeicao = _cap(store, "outra-rejeicao", "pynott", born)

    store.reviews.append_review_event(
        sem_base, "proposed", REVIEW_REASON_EVIDENCE, CURATOR_ACTOR, born
    )
    store.add_relation(RelationKind.FALSIFIES, sem_base, alvo, now=born)
    store.revoke_relation(
        RelationKind.FALSIFIES,
        sem_base,
        alvo,
        actor=CURATOR_ACTOR,
        reason="prova ausente",
        now=born + timedelta(hours=1),
    )
    store.reviews.append_review_event(
        outra_rejeicao,
        REVIEW_VERDICT_REJECTED,
        "the owner read it and does not want it",
        "tiago",
        born,
    )

    assert store.reject_unsupported_evidence(
        "pynott", subtree=True, now=now, actor=CURATOR_ACTOR
    ) == [sem_base]

    assert standing_proposal(store.db, sem_base) is None, (
        "the door must let the owner overturn the MECHANICAL rejection: without this"
        " `nmemory_proposal_review` refuses and the automatic verdict is irreversible"
    )
    assert standing_proposal(store.db, outra_rejeicao) == REVIEW_VERDICT_REJECTED, (
        "only this judge's rejection: a rejection from another origin stays closed"
    )

    store.reviews.append_review_event(
        sem_base, "ratified", "the owner disagrees", "tiago", now
    )
    assert store.reviews.review_verdict(sem_base) == "ratified"
    assert sem_base not in store.reviews.list_review_fenced(), (
        "ratificada, deixa de cercar"
    )
