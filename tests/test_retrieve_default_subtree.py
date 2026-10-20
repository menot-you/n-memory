"""The default for `nmemory_capsule_retrieve` is AUDIENCE, and audience is the tree.

`nmemory_context_show` decided this on 2026-09-15 and wrote the rationale in the code itself
(`server.py:4859`): "the server default is AUDIENCE, and audience is the tree
— `pynott` reaches `pynott/x`. Whoever passes explicit `project_id` chose the
exact fence and remains exact." `retrieve` lagged behind: it fenced EXACT by
default (`server.py:2920`, `proj_id = defaults.project_id`), so a reader in
`pynott` did not reach `pynott/tech`. Measured on live store 2026-09-18: 88
capsules in `pynott/*` and the dominant surface failing to read them.

`treex` is here for the same reason as in `test_scope_fences_applied`: a
bare `startswith` matches it with the `tree` prefix and the correct fence does not. It is the only case
separating subtree from exact neighbor.

The second half of RED is not reach: it is the CENSUS ROW. `record_read_traffic`
received the RESOLVED project with a comment warning why ("recording the
raw `None` would file the row against no project"). Moving the default to
prefix without moving logging files every default read against no project
at all — and the delivery census ceases to distinguish a tree session from an
exact session, exactly as `context` already prevents (`nmemory.tools.context`).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from nmemory.capsule import (
    AuthorityClass,
    Capsule,
    Freshness,
    Provenance,
    Scope,
    sha256_hex,
)
from nmemory.database import Database
from nmemory.server import MemoryServer
from nmemory.store import Store

NOW = datetime(2026, 9, 18, 12, 0, 0, tzinfo=UTC)
MARKER = "defaultsubtreemarker"
PROJECTS = ("tree", "tree/child", "other", "treex")


def _cap(project: str) -> Capsule:
    content = f"Capsule {MARKER} of project {project}."
    return Capsule(
        content=content,
        provenance=Provenance(
            source="test:retrieve-default-subtree",
            anchor=f"tests/test_retrieve_default_subtree.py:{project}",
            source_hash=sha256_hex(content.encode("utf-8")),
        ),
        confidence=0.9,
        freshness=Freshness(valid_from=NOW),
        scope=Scope(project_id=project),
        authority_class=AuthorityClass.USER_STATED,
        instruction_taint=False,
    )


def _seeded() -> tuple[Store, dict[str, str]]:
    store = Store.open_disposable_test_store()
    ids: dict[str, str] = {}
    for project in PROJECTS:
        cid, _ = store.append(_cap(project), NOW)
        ids[project] = cid
    return store, ids


def _ok(res: dict[str, Any]) -> dict[str, Any]:
    assert "result" in res and res["result"]["isError"] is False, res
    payload: dict[str, Any] = json.loads(res["result"]["content"][0]["text"])
    return payload


def test_o_default_alcanca_a_subarvore() -> None:
    """Without `project_id`, the reader in `tree` reaches `tree/child` and not `treex`."""
    store, ids = _seeded()
    server = MemoryServer(store=store, project_id="tree")

    # Retrieve reads the clock unless told; capsules unread for over 30 days are
    # excluded, and the fixture date is in the past.
    payload = _ok(
        server.call_tool("nmemory_capsule_retrieve", {"terms": [MARKER]}, now=NOW)
    )

    returned = {r["id"] for r in payload.get("results", [])}
    assert returned == {ids["tree"], ids["tree/child"]}, payload
    assert ids["treex"] not in returned, payload
    assert ids["other"] not in returned, payload


def test_o_project_id_explicito_continua_exato() -> None:
    """Whoever names the fence chose it: explicit DOES NOT become subtree.

    This is the mutant for the test above. Switching the fence to prefix instead of
    switching only the DEFAULT would pass the first test and silently widen
    every explicit call — the exact fence would cease to exist in the tool.
    """
    store, ids = _seeded()
    server = MemoryServer(store=store, project_id="tree")

    # Retrieve reads the clock unless told; capsules unread for over 30 days are
    # excluded, and the fixture date is in the past.
    payload = _ok(
        server.call_tool(
            "nmemory_capsule_retrieve",
            {"terms": [MARKER], "project_id": "tree"},
            now=NOW,
        )
    )

    returned = {r["id"] for r in payload.get("results", [])}
    assert returned == {ids["tree"]}, payload
    assert ids["tree/child"] not in returned, payload


def _read_traffic(store: Store) -> list[tuple[str | None, str | None]]:
    with Database(store.db.path).writing() as conn:
        rows = conn.execute(
            "SELECT project_id, project_prefix FROM read_traffic"
            " WHERE surface = 'retrieve' ORDER BY seq"
        ).fetchall()
    return [(row[0], row[1]) for row in rows]


def test_a_linha_de_censo_mantem_o_projeto_resolvido() -> None:
    """The default read is recorded against `tree`, never against nothing.

    And prefix is recorded TOGETHER, as `context` does: without it the delivery
    census does not know whether the read fenced the tree or the exact node.
    """
    store, _ids = _seeded()
    server = MemoryServer(store=store, project_id="tree")

    _ok(server.call_tool("nmemory_capsule_retrieve", {"terms": [MARKER]}))

    assert _read_traffic(store) == [("tree", "tree")]


def test_a_linha_de_censo_da_chamada_exata_nao_traz_prefixo() -> None:
    """The counter-case: exact fence records project and NULL prefix.

    Without this, always recording prefix would pass the test above and erase the
    distinction that the row exists to preserve.
    """
    store, _ids = _seeded()
    server = MemoryServer(store=store, project_id="tree")

    _ok(
        server.call_tool(
            "nmemory_capsule_retrieve", {"terms": [MARKER], "project_id": "tree"}
        )
    )

    assert _read_traffic(store) == [("tree", None)]
