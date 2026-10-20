"""U1 (nmemory-standing): marking false ceases to be irreversible.

The query terms exist ONLY in the old capsule: with two texts matching, the
BM25 top1-top2 margin (`retrieve.py:614-623`) abstains and the test would measure the margin,
not the fence.

`nmemory.relation_revocations` existed in the schema with zero writers and zero
readers, so a `falsifies` or `supersedes` was permanent — and under LOCK 2 an irreversible act can only be a proposal, so nobody did it:
7 `falsifies` edges across 12,088 capsules. This unit gives it a writer, a reader,
and ONE liveness rule, and teaches `add_relation` to revive — because today
it returns `False` for any existing re-assertion (store.py:1985-1991) and all
callers inherit the defect.

The table is NATIVE (postgres.py:155), so zero DDL: `origin` carries the event and
actor, the reason goes to `append_audit`. `relations.at` is NEVER touched — an
`UPDATE at = now` would rewrite the history that `is_falsified(as_of)` reads.

Each test is a falsifier of a promise; an unconverted reader becomes
red in its own test, never just in the helper test (measured law).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from storefile import default_store_path

from nmemory.capture import CaptureContext
from nmemory.database import Database
from nmemory.queries.inbox import inbox_candidates
from nmemory.relation import RelationKind
from nmemory.repositories.base import StoreError
from nmemory.server import MemoryServer
from nmemory.store import Store

T0 = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
SESSION = "sess-revocation"


def _ctx() -> CaptureContext:
    return CaptureContext(
        harness="pytest",
        model="none",
        cwd="/tmp",
        session_id=SESSION,
        host="t",
        owner="t",
    )


def _fresh() -> tuple[Database, Store, MemoryServer]:
    url = default_store_path("revoc")
    pg = Database(url)
    store = Store.open(url)
    return pg, store, MemoryServer(store=store, project_id="revoc")


def _inbox_ids(pg: Database) -> set[str]:
    """A separate READER `Store`: inbox excludes what the writer process
    just wrote (`appended_in_process`), as in real delivery."""
    reader = Store.open(pg.path)
    return {
        row.id
        for row in inbox_candidates(
            reader.db, reader.capsules.appended_in_process, "revoc", subtree=False
        )
    }


def _ingest(server: MemoryServer, content: str, now: datetime) -> str:
    response = server.call_tool(
        "nmemory_capsule_ingest",
        {
            "content": content,
            "source": "t",
            "anchor": content[:16],
            "kind": "fact",
            "proof_hint": "fixture: a porta de captura exige prova re-obtenível",
        },
        context=_ctx(),
        now=now,
    )
    assert not response["result"]["isError"], response
    return str(
        json.loads(response["result"]["content"][0]["text"])["outcomes"][0]["id"]
    )


def _retrieved_ids(server: MemoryServer, term: str, now: datetime, **extra) -> set[str]:
    response = server.call_tool(
        "nmemory_capsule_retrieve", {"terms": [term], **extra}, context=_ctx(), now=now
    )
    payload = json.loads(response["result"]["content"][0]["text"])
    return {r["id"] for r in payload.get("results", [])}


def test_a_revoked_falsifies_returns_the_capsule_to_both_surfaces() -> None:
    pg, store, server = _fresh()
    old = _ingest(server, "zebra fact that later turned out false", T0)
    new = _ingest(
        server, "the correction that falsifies the first one", T0 + timedelta(minutes=1)
    )
    t_assert = T0 + timedelta(minutes=2)
    assert store.add_relation(RelationKind.FALSIFIES, new, old, now=t_assert) is True
    assert old not in _inbox_ids(pg)
    assert old not in _retrieved_ids(server, "zebra", t_assert + timedelta(minutes=1))

    t_revoke = T0 + timedelta(minutes=3)
    store.revoke_relation(
        RelationKind.FALSIFIES,
        new,
        old,
        actor="owner",
        reason="the correction was wrong",
        now=t_revoke,
    )
    # inbox first: `nmemory_capsule_retrieve` serves, and what is served leaves inbox by design
    assert old in _inbox_ids(pg)
    assert old in _retrieved_ids(server, "zebra", t_revoke + timedelta(minutes=1))


def test_add_relation_revives_a_revoked_edge_and_says_so() -> None:
    """Today `add_relation` returns False if the row exists — the re-assertion becomes silent."""
    _, store, server = _fresh()
    old = _ingest(server, "yak fact", T0)
    new = _ingest(server, "a later correction of the first", T0 + timedelta(minutes=1))
    assert store.add_relation(
        RelationKind.FALSIFIES, new, old, now=T0 + timedelta(minutes=2)
    )
    store.revoke_relation(
        RelationKind.FALSIFIES,
        new,
        old,
        actor="owner",
        reason="r",
        now=T0 + timedelta(minutes=3),
    )
    revived = store.add_relation(
        RelationKind.FALSIFIES, new, old, now=T0 + timedelta(minutes=4)
    )
    assert revived is True, (
        "re-asserting a revoked edge must revive it, not return False"
    )
    assert old not in _retrieved_ids(server, "yak", T0 + timedelta(minutes=5))


def test_revocation_never_rewrites_history() -> None:
    """`is_falsified(as_of=T2)` remains true after a revocation at T3."""
    _, store, server = _fresh()
    old = _ingest(server, "xylo fact", T0)
    new = _ingest(server, "a later correction of the first", T0 + timedelta(minutes=1))
    t_assert = T0 + timedelta(minutes=2)
    t_mid = T0 + timedelta(minutes=3)
    t_revoke = T0 + timedelta(minutes=4)
    store.add_relation(RelationKind.FALSIFIES, new, old, now=t_assert)
    store.revoke_relation(
        RelationKind.FALSIFIES, new, old, actor="owner", reason="r", now=t_revoke
    )
    assert store.relations.is_falsified(old, as_of=t_mid) is True
    assert store.relations.is_falsified(old) is False
    # the row in `relations` keeps the original `at`: revoking never touches it
    rec = [
        r
        for r in store.relations.list_relations_for(old, include_revoked=True)
        if r.kind is RelationKind.FALSIFIES
    ]
    assert len(rec) == 1 and rec[0].at == t_assert


def test_revoked_supersedes_is_visible_on_the_question_surface() -> None:
    """`is_superseded` is the supersedes fence of retrieve/context; without converting it, revocation is cosmetic."""
    _, store, server = _fresh()
    old = _ingest(server, "wombat fact v1", T0)
    new = _ingest(
        server,
        "the second version that supersedes the first",
        T0 + timedelta(minutes=1),
    )
    store.add_relation(
        RelationKind.SUPERSEDES, from_id=new, to_id=old, now=T0 + timedelta(minutes=2)
    )
    assert store.relations.is_superseded(old) is True
    assert old not in _retrieved_ids(server, "wombat", T0 + timedelta(minutes=3))
    store.revoke_relation(
        RelationKind.SUPERSEDES,
        new,
        old,
        actor="owner",
        reason="v2 was wrong",
        now=T0 + timedelta(minutes=4),
    )
    assert store.relations.is_superseded(old) is False
    assert old in _retrieved_ids(server, "wombat", T0 + timedelta(minutes=5))


def test_revoking_twice_or_revoking_nothing_is_inert_and_deletes_nothing() -> None:
    _, store, server = _fresh()
    old = _ingest(server, "vole fact", T0)
    new = _ingest(server, "a later correction of the first", T0 + timedelta(minutes=1))
    store.add_relation(RelationKind.FALSIFIES, new, old, now=T0 + timedelta(minutes=2))
    before = len(store.relations.list_relations(include_revoked=True))
    store.revoke_relation(
        RelationKind.FALSIFIES,
        new,
        old,
        actor="o",
        reason="r",
        now=T0 + timedelta(minutes=3),
    )
    store.revoke_relation(
        RelationKind.FALSIFIES,
        new,
        old,
        actor="o",
        reason="again",
        now=T0 + timedelta(minutes=4),
    )
    store.revoke_relation(
        RelationKind.FALSIFIES,
        old,
        new,
        actor="o",
        reason="never existed",
        now=T0 + timedelta(minutes=5),
    )
    assert len(store.relations.list_relations(include_revoked=True)) == before, (
        "revocation never deletes a row of relations"
    )
    assert store.relations.list_relations() == [], "and the live view does not show it"
    assert store.relations.is_falsified(old) is False


def test_the_revocation_is_audited_with_its_reason() -> None:
    _, store, server = _fresh()
    old = _ingest(server, "urchin fact", T0)
    new = _ingest(server, "a later correction of the first", T0 + timedelta(minutes=1))
    store.add_relation(RelationKind.FALSIFIES, new, old, now=T0 + timedelta(minutes=2))
    store.revoke_relation(
        RelationKind.FALSIFIES,
        new,
        old,
        actor="curator",
        reason="anchor moved",
        now=T0 + timedelta(minutes=3),
    )
    events = [e for e in store.audit.list_audit() if e.action == "revoke_relation"]
    assert (
        events and "anchor moved" in events[-1].reason and events[-1].actor == "curator"
    )


def test_the_cli_revokes_and_never_touches_the_mcp_schema(monkeypatch, capsys) -> None:
    """Revocation lives in the CLI (and in the curator): `nmemory_relation_add` is sealed."""
    from nmemory.cli import main

    pg, store, server = _fresh()
    old = _ingest(server, "tapir fact", T0)
    new = _ingest(server, "a later correction of the first", T0 + timedelta(minutes=1))
    store.add_relation(RelationKind.FALSIFIES, new, old, now=T0 + timedelta(minutes=2))
    assert store.relations.is_falsified(old) is True
    monkeypatch.setenv("NOTT_DATABASE_DIR", str(Path(pg.path).parent))
    argv = [
        "capsule",
        "relate",
        "--kind",
        "falsifies",
        "--from",
        new,
        "--to",
        old,
        "--revoke",
        "--actor",
        "owner",
        "--reason",
        "measured false positive",
    ]
    import sys as _sys

    monkeypatch.setattr(_sys, "argv", ["nmemory", *argv])
    try:
        main()
    except SystemExit as exc:  # a CLI may exit 0 explicitly
        assert exc.code in (0, None), exc.code
    out = capsys.readouterr().out
    assert '"outcome": "revoked"' in out, out
    assert Store.open(pg.path).relations.is_falsified(old) is False


def test_only_falsifies_and_supersedes_are_revocable() -> None:
    """Review 2026-09-16: revoking a `dod_of`/`part_of` hid it from `list_relations`
    while `reject_invalidated_work_relations` read it raw — two views of the same
    work edge. Revocation is the undo of both verdicts, and only them."""
    _pg, store, server = _fresh()
    a = _ingest(server, "a task", T0)
    b = _ingest(server, "its dod", T0)
    for kind in ("dod_of", "part_of", "blocks", "derived_from"):
        with pytest.raises(StoreError):
            store.revoke_relation(kind, b, a, actor="t", reason="no", now=T0)
