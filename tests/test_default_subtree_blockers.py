"""The three blockers that the independent review (fugu, 2026-09-18) caught.

The default for `nmemory_capsule_retrieve` changed to fence by SUBTREE. The review proved
three things that my four original tests did not measure, and two of them are
defects that the change EXPOSES rather than creates.

B1 — LEAK. `nott` is a live default (`--project nott`, `NOTT_NMEMORY_PROJECT`) and
     `in_project_subtree("nott/monorepo", "nott")` is True. The alias map
     declares `nott/monorepo*` as "ANOTHER system that CLAUDE.md orders to
     preserve" (`postgres.py:94`), with 36 capsules counted. The new fence
     served them to a reader that did not ask for them. This is the only one
     of the three that the change CREATES.

B2 — FAIL-OPEN. `NOTT_NMEMORY_PROJECT=""` is accepted as default because it only tests
     `is not None`. And the two lanes disagree on `""`: lexical tests
     `is not None` and filters, vector tests `if project_prefix:` and does not
     add ANY condition — cosine over the entire store. Previously,
     `project_id=""` yielded `WHERE project_id = ''` and zero rows: CLOSED failure.
     The change replaces closed failure with open failure.

B3 — CROWDING. The vector lane cap is cut in SQL BEFORE the late fences,
     even though the comment in `retrieve.py:518` claims it is
     "applied AFTER eligibility" and the schema promises "the top
     `vector_k` ELIGIBLE capsules". The post-eligibility cap ALREADY exists and ALREADY
     counts exclusions (`retrieve.py:831-849`); what is missing is fetching widely so
     that it has something to cut. Widening the fence caused children to win the window and
     then be removed by late fences, taking the root capsule with
     them — REMOVING results, the opposite of what the PR claimed.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from storefile import put_embedding

from nmemory.capsule import (
    AuthorityClass,
    Capsule,
    Freshness,
    Provenance,
    Scope,
    sha256_hex,
)
from nmemory.embedding import EmbeddingClient
from nmemory.ingest import effective_defaults
from nmemory.server import MemoryServer
from nmemory.space import DIMENSION
from nmemory.store import Store
from nmemory.tools import context as context_tool

NOW = datetime(2026, 9, 18, 12, 0, 0, tzinfo=UTC)
MARKER = "bloqueantemarcador"


def _cap(project: str, nome: str) -> Capsule:
    content = f"Capsule `{MARKER}` `{nome}` in {project}."
    return Capsule(
        content=content,
        provenance=Provenance(
            source="test:bloqueantes",
            anchor=f"tests/test_default_subtree_blockers.py:{nome}",
            source_hash=sha256_hex(content.encode("utf-8")),
        ),
        confidence=0.9,
        freshness=Freshness(valid_from=NOW),
        scope=Scope(project_id=project),
        authority_class=AuthorityClass.USER_STATED,
        instruction_taint=False,
    )


def _ok(res: dict[str, Any]) -> dict[str, Any]:
    assert "result" in res and res["result"]["isError"] is False, res
    return json.loads(res["result"]["content"][0]["text"])


# ---------------------------------------------------------------------------
# B1 — the mixed bucket `nott` does not expand to the subtree
# ---------------------------------------------------------------------------


def test_o_default_nott_nao_alcanca_o_monorepo_do_owner() -> None:
    """`nott/monorepo` is another live system, and the repository mandates preserving it.

    The subtree predicate matches it (`in_project_subtree("nott/monorepo",
    "nott")` is True), so only an explicit rule prevents it. Without it, a
    server with `--project nott` would start serving monorepo capsules to whoever
    requested memory from nott.
    """
    store = Store.open_disposable_test_store()
    raiz, _ = store.append(_cap("nott", "raiz"), NOW)
    alheia, _ = store.append(_cap("nott/monorepo", "alheia"), NOW)
    server = MemoryServer(store=store, project_id="nott")

    # Retrieve reads the clock unless told; capsules unread for over 30 days are
    # excluded, and the fixture date is in the past.
    payload = _ok(
        server.call_tool("nmemory_capsule_retrieve", {"terms": [MARKER]}, now=NOW)
    )

    devolvidas = {r["id"] for r in payload.get("results", [])}
    assert alheia not in devolvidas, payload
    assert devolvidas == {raiz}, payload


def test_um_default_sem_vizinho_alheio_continua_a_expandir() -> None:
    """The counter-case: the rule closes `nott`, not the entire subtree.

    Without this, "disable subtree default" would pass the test above and
    silently undo the entire unit.
    """
    store = Store.open_disposable_test_store()
    raiz, _ = store.append(_cap("pynott", "raiz"), NOW)
    filha, _ = store.append(_cap("pynott/tech", "filha"), NOW)
    server = MemoryServer(store=store, project_id="pynott")

    # Retrieve reads the clock unless told; capsules unread for over 30 days are
    # excluded, and the fixture date is in the past.
    payload = _ok(
        server.call_tool("nmemory_capsule_retrieve", {"terms": [MARKER]}, now=NOW)
    )

    assert {r["id"] for r in payload.get("results", [])} == {raiz, filha}, payload


def test_o_context_fecha_o_mesmo_buraco() -> None:
    """The same exposure exists in `context` since 2026-09-15.

    Propagating the gap to a second surface instead of closing it was what the
    review called a defect. The rule is shared.
    """
    store = Store.open_disposable_test_store()
    _raiz, _ = store.append(_cap("nott", "raiz"), NOW)
    alheia, _ = store.append(_cap("nott/monorepo", "alheia"), NOW)
    server = MemoryServer(store=store, project_id="nott")

    res = context_tool.handle(
        server.handler_state(),
        {"query": MARKER, "sources": ["memory"]},
        None,
        NOW,
        "test",
    )
    payload = json.loads(res["content"][0]["text"])

    assert alheia not in {r["id"] for r in payload.get("results", [])}, payload


# ---------------------------------------------------------------------------
# B2 — empty prefix fails CLOSED, in both lanes
# ---------------------------------------------------------------------------


def test_a_lane_vetorial_com_prefixo_vazio_nao_varre_a_loja() -> None:
    """`""` must add a condition, as lexical already does.

    `if project_prefix:` is false for `""` and the project clause DISAPPEARS
    from SQL: cosine over the entire store. It is the route, not the variable — the test
    builds the SQL through the real path.
    """
    client = EmbeddingClient(custom_hook=lambda text: [1.0] + [0.0] * (DIMENSION - 1))
    store = Store.open_disposable_test_store()
    cid, _ = store.append(_cap("algum", "unica"), NOW)
    put_embedding(
        store.db,
        cid,
        [1.0] + [0.0] * (DIMENSION - 1),
        client.space_fingerprint.as_str(),
        sha256_hex(cid.encode("utf-8")),
    )

    # MAIN SITE (`postgres.py:2104`): WITHOUT `space_fingerprint` the verification
    # block doesn't even run, so the main query decides. Reverting only
    # this site returns the capsule from `algum` — the entire store.
    so_principal = store.embeddings.search_cosine(
        query_vector=[1.0] + [0.0] * (DIMENSION - 1),
        limit=10,
        project_prefix="",
    )
    assert so_principal == [], (
        "an empty prefix matches no project: the MAIN query must"
        f" fail CLOSED, and it returned {len(so_principal)} store row(s)"
    )

    # VERIFICATION SITE (`postgres.py:2066`): the main query always fences,
    # so `[]` does not distinguish it. Its OWN observable is the other one: with the
    # fence, `total_cnt` is 0 and the call returns `[]`; without it, it counts the entire
    # store, sees `space_cnt == 0` and RAISES `DatabaseError`.
    outro_espaco = "espaco-superado-" + "0" * 8
    cid2, _ = store.append(_cap("algum", "segunda"), NOW)
    put_embedding(
        store.db,
        cid2,
        [1.0] + [0.0] * (DIMENSION - 1),
        outro_espaco,
        sha256_hex(cid2.encode("utf-8")),
    )
    so_verificacao = store.embeddings.search_cosine(
        query_vector=[1.0] + [0.0] * (DIMENSION - 1),
        limit=10,
        project_prefix="",
        space_fingerprint=outro_espaco + "-novo",
    )
    assert so_verificacao == [], (
        "an empty prefix matches no project: the VERIFICATION query must"
        " count ZERO under the fence, instead of flagging a superseded space for the whole store"
    )


def test_um_nmemory_project_vazio_nao_vira_default() -> None:
    """The env accepted `""` because it only tested `is not None`.

    The constructor rejects the equivalent; the env does not. An empty default is
    absence of project written another way.
    """
    import os

    anterior = os.environ.get("NOTT_NMEMORY_PROJECT")
    os.environ["NOTT_NMEMORY_PROJECT"] = ""
    try:
        defaults = effective_defaults(None, None, None)
    finally:
        if anterior is None:
            os.environ.pop("NOTT_NMEMORY_PROJECT", None)
        else:
            os.environ["NOTT_NMEMORY_PROJECT"] = anterior

    assert defaults.project_id != "", defaults
    assert defaults.source == "literal_default", defaults


# ---------------------------------------------------------------------------
# B3 — widening the fence must not REMOVE results
# ---------------------------------------------------------------------------


def test_os_filhos_nao_expulsam_a_raiz_da_janela_vetorial() -> None:
    """The vector cap cuts AFTER eligibility, as the schema promises.

    `cap-root` is in the requested session with a LOWER cosine; ten children from
    another session have higher cosine. With the SQL cutoff at ten, the ten children
    fill the window, the late fences remove them by session, and the root — which
    was eligible — was never fetched. Result: `missing_evidence`
    with `out_of_session: 10`, when the exact fence gave `grounded`.

    The post-eligibility cap ALREADY exists (`retrieve.py:831`) and ALREADY counts what
    it cuts; what was missing was fetching widely so that it has something to cut.
    """
    client = EmbeddingClient(custom_hook=lambda text: [1.0] + [0.0] * (DIMENSION - 1))
    store = Store.open_disposable_test_store()
    espaco = client.space_fingerprint.as_str()

    raiz, _ = store.append(_cap("tree", "raiz"), NOW, session_id="wanted")
    # LOWER cosine than that of the children: a vector further away from the probe.
    vec_raiz = [0.5, 0.866] + [0.0] * (DIMENSION - 2)
    put_embedding(store.db, raiz, vec_raiz, espaco, sha256_hex(raiz.encode("utf-8")))

    for i in range(10):
        filho, _ = store.append(
            _cap("tree/child", f"filho{i}"), NOW, session_id="outra"
        )
        vec = [1.0 - i * 0.001, 0.02] + [0.0] * (DIMENSION - 2)
        put_embedding(store.db, filho, vec, espaco, sha256_hex(filho.encode("utf-8")))

    from nmemory.retrieve import RetrieveParams, retrieve

    # The query CANNOT match by term. With the lexical lane answering, it
    # brings the root back via its own path (limit 50) and completely masks
    # the defect — this was how the first form of this test passed
    # with the product broken. Only the vector lane may run.
    sonda = "zzzsondaquenaocasatermonenhum"

    def _corre(**cerca: str) -> dict[str, Any]:
        # EXPLICIT `vector_k`. With the implicit default, a mutant that touched
        # it caused this test to pass with the product broken — caught by the
        # re-review. The cap must be that of the scenario, not whatever happens to be there.
        return retrieve(
            store,
            RetrieveParams(
                query=sonda,
                terms=[sonda],
                session_id="wanted",
                vector_k=10,
                **cerca,
            ),
            embeddings=client,
            now=NOW,
        )

    # THE CONTROL: with the exact fence the root is served.
    exato = _corre(project_id="tree")
    assert {ev["id"] for ev in exato.get("evidence", [])} == {raiz}, exato

    # And widening the fence cannot lose it.
    largo = _corre(project_prefix="tree")
    assert raiz in {ev["id"] for ev in largo.get("evidence", [])}, (
        "a raiz era elegivel e o alargamento da cerca expulsou-a da janela: "
        f"outcome={largo.get('outcome')} exclusions={largo.get('exclusions')}"
    )


# ---------------------------------------------------------------------------
# R2 — `effort_id` cannot silently change fences
# ---------------------------------------------------------------------------


def test_o_effort_id_conserva_a_cerca_que_tinha() -> None:
    """The PR must not change `effort_id`, and it was changing it.

    The default guard names `topic_id` and not `effort_id`, so a call
    with `effort_id` and without project went from EXACT fence to subtree without
    anyone deciding that — and in the vector lane children outside the effort cause
    crowding before `outside_effort` runs. Two sibling parameters with two
    fences for the same omission is exactly the defect that this PR closes between
    `retrieve` and `context`; leaving it standing here would open it elsewhere.

    The conservative approach is correct: with `effort_id`, omission continues to mean
    the NODE, as it meant before this PR.
    """
    server = MemoryServer(store=Store.open_disposable_test_store(), project_id="tree")

    def _ingere(project: str, nome: str, kind: str) -> str:
        return _ok(
            server.call_tool(
                "nmemory_capsule_ingest",
                {
                    "content": f"Capsule `{MARKER}` `{nome}` in {project}.",
                    "source": "test",
                    "anchor": f"tests/test_default_subtree_blockers.py:{nome}",
                    "kind": kind,
                    "project_id": project,
                },
            )
        )["outcomes"][0]["id"]

    epico = _ingere("tree", "epico", "epic")
    raiz = _ingere("tree", "raiz", "task")
    filha = _ingere("tree/child", "filha", "task")

    # The effort and epic UNION their `part_of` members. The two tasks
    # enter it, one on each side of the project fence: that is what makes the
    # result depend ONLY on the project fence, which is what is measured here.
    for membro in (raiz, filha):
        _ok(
            server.call_tool(
                "nmemory_relation_add", {"kind": "part_of", "from": membro, "to": epico}
            )
        )

    payload = _ok(
        server.call_tool(
            "nmemory_capsule_retrieve", {"terms": [MARKER], "effort_id": epico}
        )
    )

    devolvidas = {r["id"] for r in payload.get("results", [])}
    assert filha not in devolvidas, (
        "with `effort_id` the omitted project still fences the NODE, as"
        f" before this PR — and it returned the child: {payload}"
    )
    assert raiz in devolvidas, payload
