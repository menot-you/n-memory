"""One repository, one project key: the identity fold (U4 nmemory-curator).

Measured read-only on the live store 2026-09-15: 85 `project_id` spellings;
readers fence `pynott` EXACT (95/133 context rows, `project_prefix` always
NULL), so `/workspace/example` (304 capsules, 84% never read), `menot-you/pynott`
(121, 87%) and `pynott/nmemory-escrita` (88, 100%) were invisible to every
reader. This file pins the identity fold:

1. WRITE side: the ingest choke point folds the alias; the outcome REPORTS
   `{was, now}` — the caller's declared spelling is provenance, not a secret.
2. READ side: a caller still on the old spelling reaches the canonical rows —
   the fold must never remove reach (alcance finding #5).
3. DEFAULT scope fences by SUBTREE (`pynott` reaches `pynott/x`); an EXPLICIT
   project_id stays exact. The 88 capsules of `pynott/nmemory-escrita` were
   100% never read because no reader ever passed their prefix.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from storefile import fresh_store_path

from nmemory.capture import CaptureContext
from nmemory.database import Database
from nmemory.project import PROJECT_ALIASES, canonical_project
from nmemory.server import MemoryServer
from nmemory.store import Store

SESSION = "sess-project-canon"
NOW = datetime(2026, 9, 15, tzinfo=UTC)


@pytest.fixture(autouse=True)
def project_aliases(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in tuple(PROJECT_ALIASES):
        monkeypatch.delitem(PROJECT_ALIASES, name)
    aliases = {
        "/workspace/example": "pynott",
        "/workspace/example-worktree": "pynott",
        "nott/tech": "pynott/tech",
        "nott/ops": "pynott/ops",
        "nmemory": "pynott",
        "nott/nmemory": "pynott/nmemory",
        **{
            name: "pynott"
            for name in (
                "nspawn",
                "nspawn-codex-agy",
                "donors",
                "nott-donors",
                "resume",
            )
        },
    }
    for name, target in aliases.items():
        monkeypatch.setitem(PROJECT_ALIASES, name, target)


def _fresh(project: str | None = None) -> tuple[Database, Store, MemoryServer]:
    url = fresh_store_path("project_canon")
    pg = Database(url)
    store = Store.open(url)
    return pg, store, MemoryServer(store=store, project_id=project)


def _ctx() -> CaptureContext:
    return CaptureContext.detect({"session_id": SESSION, "harness": "pytest/1"})


def _ingest(server: MemoryServer, content: str, project_id: str | None) -> dict:
    args: dict = {
        "content": content,
        "kind": "fact",
        "source": "test_project_canon",
        "anchor": content[:16],
        "proof_hint": "fixture: a porta de captura exige prova re-obtenível",
    }
    if project_id is not None:
        args["project_id"] = project_id
    response = server.call_tool("nmemory_capsule_ingest", args, context=_ctx())
    assert not response["result"]["isError"], response
    payload = json.loads(response["result"]["content"][0]["text"])
    return payload["outcomes"][0]


def _ingest_kind(server: MemoryServer, content: str, project_id: str, kind: str) -> str:
    response = server.call_tool(
        "nmemory_capsule_ingest",
        {
            "content": content,
            "source": "test_project_canon",
            "anchor": content[:16],
            "proof_hint": "fixture: a porta de captura exige prova re-obtenível",
            "project_id": project_id,
            "kind": kind,
        },
        context=_ctx(),
    )
    assert not response["result"]["isError"], response
    payload = json.loads(response["result"]["content"][0]["text"])
    return str(payload["outcomes"][0]["id"])


def _project_of(pg: Database, cid: str) -> str:
    with pg.writing() as conn, conn.cursor() as cur:
        cur.execute("SELECT project_id FROM capsules WHERE id = ?", (cid,))
        return str(cur.fetchone()[0])


def test_ingest_folds_the_alias_and_reports_it() -> None:
    pg, _, server = _fresh()
    row = _ingest(server, "identity fold observation", "/workspace/example")

    assert _project_of(pg, row["id"]) == "pynott", "the stored key is canonical"
    assert row["project_id"] == {"was": "/workspace/example", "now": "pynott"}, (
        "the fold is reported, never silent"
    )


def test_a_reader_on_the_old_spelling_still_reaches() -> None:
    _pg, _, server = _fresh()
    _ingest(server, "old spelling reader observation", "/workspace/example")

    response = server.call_tool(
        "nmemory_context_show",
        {"query": "old spelling reader", "project_id": "/workspace/example"},
        context=_ctx(),
    )
    text = json.loads(response["result"]["content"][0]["text"])
    assert text["scope"]["project_id"] == "pynott"
    assert text["results"], "the fold adds reach, never removes it"


def test_the_default_scope_fences_by_subtree_and_explicit_stays_exact() -> None:
    _pg, _store, server = _fresh(project="pynott")
    _ingest(server, "subtree child observation", "pynott/curator-sub")
    _ingest(server, "subtree root observation", "pynott")

    default = json.loads(
        server.call_tool(
            "nmemory_context_show",
            {"query": "subtree observation"},
            context=_ctx(),
        )["result"]["content"][0]["text"]
    )
    assert default["scope"]["match"] == "subtree", (
        "the server default is the audience, and the audience is the tree"
    )
    assert len(default["results"]) == 2, "root and child both belong to pynott"

    explicit = json.loads(
        server.call_tool(
            "nmemory_context_show",
            {"query": "subtree observation", "project_id": "pynott"},
            context=_ctx(),
        )["result"]["content"][0]["text"]
    )
    assert explicit["scope"]["match"] == "exact"
    assert len(explicit["results"]) == 1, (
        "an explicit project_id keeps the exact fence — the caller chose it"
    )


def test_the_bootstrap_reader_folds_the_alias_too() -> None:
    """Bootstrap is a DELIVERY surface: losing reach there happens at startup.

    "EVERY reader" wasn't every reader — `nmemory_project_bootstrap` and `project_prefix` in
    `retrieve` were left out of the initial shape. Caught by independent
    review.
    """
    _pg, _store, server = _fresh()
    # The bootstrap bundle has sections per kind (constraints, decisions,
    # traps, tasks); a `fact` enters none of them.
    _ingest_kind(
        server, "bootstrap fold observation", "/workspace/example", "constraint"
    )

    boot = json.loads(
        server.call_tool(
            "nmemory_project_bootstrap",
            {"project_id": "/workspace/example"},
            context=_ctx(),
        )["result"]["content"][0]["text"]
    )
    assert boot["handles"], "a client on the old spelling still orients"

    retrieved = json.loads(
        server.call_tool(
            "nmemory_capsule_retrieve",
            {"terms": ["bootstrap", "fold"], "project_prefix": "/workspace/example"},
            context=_ctx(),
        )["result"]["content"][0]["text"]
    )
    assert retrieved["results"], (
        "and the retrieve prefix folds like the list prefix — one tool, one fence"
    )


def test_canonical_project_leaves_unknown_keys_alone() -> None:
    """The property is "a key OUTSIDE the map passes through", not "these strings pass through".

    The initial version used `nott/tech` and `nmemory` as examples of unknown
    keys, with the note "a semantic fold is a different decision, staged
    for the owner". The owner delegated that decision to the session on 2026-09-17 ("complete
    all up to D10 and try to solve problems on your own"), and both
    spellings entered the map — so the example became obsolete, not the property. Tying it
    to the map itself is what keeps it true after the next fold.
    """
    assert canonical_project("pynott") == "pynott"
    for fora_do_mapa in ("project-that-never-existed", "other-project", "nottx"):
        assert fora_do_mapa not in PROJECT_ALIASES, fora_do_mapa
        assert canonical_project(fora_do_mapa) == fora_do_mapa, fora_do_mapa


def test_a_dobra_preserva_o_substantivo_e_nunca_contamina() -> None:
    """D5: the capsules that no reader reaches fall into FOUR families, and only one
    folds. Measured on the live store on 2026-09-17: 36 candidate spellings covered 3250
    capsules; the 34 that enter cover **1187**, and the two bare ones left out
    (`nott`, `nmemory`) cover the other 2063 for measured reasons, in 1b.

    Without this fence the map grows by name similarity and contaminates: a
    bootstrap of `pynott` would start serving facts from `happyday` or
    `menot-you/monorepo`, which are other systems. The exclusions are the part of the
    map that CANNOT be inferred from the name, so that is the part needing tests.
    """
    # 1. A sub-spelling preserves the noun: the fence is by subtree prefix,
    # so `pynott` already reaches `pynott/tech` — flattening would lose the
    # distinction without gaining reach.
    assert canonical_project("nott/tech") == "pynott/tech"
    assert canonical_project("nott/ops") == "pynott/ops"

    # 1a. ORPHANS flatten, and this is an owner decision against this principle, not
    # an oversight. Reaffirmed on 2026-09-19 with `pynott/<suffix>` placed before him and
    # with the cost documented: the noun ceases to exist as a scope, and it is no
    # longer possible to fence a reader solely into nSPAWN or solely into donors. Reach is
    # the same under both options — since #679 a reader in `pynott` reaches
    # `pynott/*` — so what he chose was to lose the distinction, with eyes
    # open.
    for orfao in ("nspawn", "nspawn-codex-agy", "donors", "nott-donors", "resume"):
        assert canonical_project(orfao) == "pynott", orfao

    assert "nott" not in PROJECT_ALIASES
    assert canonical_project("nott") == "nott"

    # Bare `nmemory` (653) was REMOVED from this exclusion by owner decision, 2026-09-19.
    # The reason that held it back was measured and remains true — it is a COMPONENT
    # name (the package, the MCP server), and folding it at WRITE time
    # caused 30 tests to fail. But those 30 failed due to asymmetry in the
    # FIXTURE, not the product: they seeded RAW (`Store.append`) and read through the PORT
    # (`server.py:2918`, which canonicalizes), whereas a real session writes
    # through ingest (`ingest.py:451`) and reads through the port — folding on both sides.
    # The fixtures were changed to `projetodeteste`; the product shape did not change.
    #
    # And writing to `nmemory` is NOT just fixtures: measured on the live store, real
    # sessions (`codex-mcp-client`, `claude-code`) wrote there until 2026-09-12 with
    # explicit `project_id`. Moving only the data would let the orphan return on the
    # next capture — that is why the map had to change as well.
    assert canonical_project("nmemory") == "pynott"
    # And unambiguous sub-spellings continue to fold: the mixed bucket is only the bare name.
    assert canonical_project("nott/nmemory") == "pynott/nmemory"
    # A worktree had the literal cwd as scope, and the name is not a noun.
    assert canonical_project("/workspace/example-worktree") == "pynott"

    # 2. NEVER folds another project of the owner: folding would be contamination.
    for outro in (
        "other-project",
        "courses",
        "contacts",
        "example/career",
        "sample-game",
    ):
        assert canonical_project(outro) == outro, outro

    # 3. NEVER folds the monorepo: live `menot-you/monorepo` is another system that
    # CLAUDE.md instructs to preserve, and forbids acting on by ambiguous name.
    for mono in ("nott/monorepo", "nott-monorepo", "nott/monorepo/pr-747"):
        assert canonical_project(mono) == mono, mono

    # 4. `nottx` is the exact neighbor that the docstring of `in_project_subtree`
    # names as the pitfall of a bare `startswith`: ambiguous, left out.
    assert canonical_project("nottx") == "nottx"

    # 5. No target falls outside the canonical subtree, or the fold would create a
    # new unreachable scope instead of curing the old one.
    for was, became in PROJECT_ALIASES.items():
        assert became == "pynott" or became.startswith("pynott/"), (was, became)
        assert became != was, was
