"""The Unit of Work's two write declarations, each pinned by a falsifier (B5 rule 10).

(a) A composite writes more than one repository under ONE declaration
(`@writes` on the `Store` method). Called outside any tool boundary, it is its
own transaction: when its second write raises, the first write's rows roll
back with it. Each case below makes the second write raise and asserts that no
row of the first remains. Removing that composite's `@writes` leaves every
repository writer as its own transaction, the first one commits, and the case
fails.

(b) A bookkeeping writer (`with db.writing()`) changes a row without being a
generation: the embedding worker re-syncs on every generation, and the
curator's queue records verifications every minute. Each case below asserts
that the writer changed a row and that `get_generation()` did not move.
Swapping that writer to a canonical write (`db.write_boundary()`, what
`@writes` does) makes the generation advance, and the case fails.

The composites with ONE write (`add_relation`, `set_classification`,
`append_dod_event`) have no second write to fail and
are not cases of (a): their declaration keeps the eligibility reads under the
write lock, which the race tests pin.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from storefile import fresh_store_path

from nmemory.capsule import (
    AuthorityClass,
    Capsule,
    Freshness,
    Provenance,
    Scope,
    format_rfc3339,
    sha256_hex,
)
from nmemory.curator import CURATOR_ACTOR
from nmemory.queries.staged import (
    REVIEW_REASON_EVIDENCE,
    REVIEW_REASON_PROPOSAL,
    REVIEW_VERDICT_EXPIRED,
    REVIEW_VERDICT_REJECTED,
    STAGED_HORIZON_DAYS,
)
from nmemory.relation import RelationKind
from nmemory.repositories.capsules import EventTimeRange
from nmemory.repositories.embeddings import EmbeddingJob
from nmemory.space import DIMENSION
from nmemory.store import Store

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
BORN = NOW - timedelta(days=STAGED_HORIZON_DAYS + 5)


class SecondWriteFailed(Exception):
    """The injected failure of a composite's second write."""


def _store() -> Store:
    return Store.open(fresh_store_path("uow"))


def _capsule(mark: str, project: str = "uow") -> Capsule:
    content = f"unit of work capsule {mark}"
    return Capsule(
        content=content,
        provenance=Provenance(
            source="t",
            anchor="src/uow.py:1",
            source_hash=sha256_hex(content.encode("utf-8")),
        ),
        confidence=0.9,
        freshness=Freshness(valid_from=BORN),
        scope=Scope(project_id=project),
        authority_class=AuthorityClass.USER_STATED,
        instruction_taint=False,
    )


def _cap(store: Store, mark: str, project: str = "uow") -> str:
    cid, _ = store.append(_capsule(mark, project), BORN)
    return cid


def _count(store: Store, sql: str, params: tuple[Any, ...] = ()) -> int:
    with store.db.connection() as conn, conn.cursor() as cur:
        row = cur.execute(sql, params).fetchone()
    assert row is not None
    return int(row[0])


def _fail(monkeypatch: pytest.MonkeyPatch, owner: object, name: str) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise SecondWriteFailed(name)

    monkeypatch.setattr(owner, name, fail)


def _fail_second_call(
    monkeypatch: pytest.MonkeyPatch, owner: object, name: str
) -> None:
    original = getattr(owner, name)
    calls = []

    def second_fails(*args: object, **kwargs: object) -> object:
        calls.append(name)
        if len(calls) == 2:
            raise SecondWriteFailed(name)
        return original(*args, **kwargs)

    monkeypatch.setattr(owner, name, second_fails)


# -- (a) the composites ---------------------------------------------------------
#
# Each case: set up, make the SECOND write fail, run the composite outside any
# boundary, and return the count of the FIRST write's rows (which must be zero).


def _append(store: Store, monkeypatch: pytest.MonkeyPatch) -> int:
    _fail(monkeypatch, store.capsules, "set_capture_context")
    with pytest.raises(SecondWriteFailed):
        store.append(_capsule("append"), NOW)
    return _count(store, "SELECT count(*) FROM capsules")


def _append_capture(store: Store, monkeypatch: pytest.MonkeyPatch) -> int:
    _fail(monkeypatch, store.capsules, "set_event_time")
    with pytest.raises(SecondWriteFailed):
        store.append_capture(
            _capsule("capture"), None, EventTimeRange.point(NOW), None, NOW
        )
    return _count(store, "SELECT count(*) FROM capsules")


def _set_birth_classification(store: Store, monkeypatch: pytest.MonkeyPatch) -> int:
    cid = _cap(store, "birth")
    _fail(monkeypatch, store.capsules, "set_handle_prefix")
    with pytest.raises(SecondWriteFailed):
        store.set_birth_classification(cid, "fact", "project", NOW)
    return _count(
        store, "SELECT count(*) FROM classifications WHERE capsule_id = ?", (cid,)
    )


def _revoke_relation(store: Store, monkeypatch: pytest.MonkeyPatch) -> int:
    target, evidence = _cap(store, "target"), _cap(store, "evidence")
    store.add_relation(RelationKind.FALSIFIES, evidence, target, now=BORN)
    _fail(monkeypatch, store.audit, "append_audit")
    with pytest.raises(SecondWriteFailed):
        store.revoke_relation(
            RelationKind.FALSIFIES,
            evidence,
            target,
            actor="owner",
            reason="the proof is gone",
            now=NOW,
        )
    return _count(store, "SELECT count(*) FROM relation_revocations")


def _expire_staged_proposals(store: Store, monkeypatch: pytest.MonkeyPatch) -> int:
    for mark in ("first proposal", "second proposal"):
        cid = _cap(store, mark)
        store.reviews.append_review_event(
            cid, "proposed", REVIEW_REASON_PROPOSAL, "owner", BORN
        )
    _fail_second_call(monkeypatch, store.reviews, "append_review_event")
    with pytest.raises(SecondWriteFailed):
        store.expire_staged_proposals("uow", subtree=False, now=NOW, actor="curator")
    return _count(
        store,
        "SELECT count(*) FROM review_events WHERE verdict = ?",
        (REVIEW_VERDICT_EXPIRED,),
    )


def _reject_unsupported_evidence(store: Store, monkeypatch: pytest.MonkeyPatch) -> int:
    target = _cap(store, "refuted")
    for mark in ("first evidence", "second evidence"):
        cid = _cap(store, mark)
        store.reviews.append_review_event(
            cid, "proposed", REVIEW_REASON_EVIDENCE, CURATOR_ACTOR, BORN
        )
        store.add_relation(RelationKind.FALSIFIES, cid, target, now=BORN)
        store.revoke_relation(
            RelationKind.FALSIFIES,
            cid,
            target,
            actor=CURATOR_ACTOR,
            reason="the proof is gone",
            now=BORN + timedelta(hours=1),
        )
    _fail_second_call(monkeypatch, store.reviews, "append_review_event")
    with pytest.raises(SecondWriteFailed):
        store.reject_unsupported_evidence(
            "uow", subtree=False, now=NOW, actor=CURATOR_ACTOR
        )
    return _count(
        store,
        "SELECT count(*) FROM review_events WHERE verdict = ?",
        (REVIEW_VERDICT_REJECTED,),
    )


COMPOSITES: dict[str, Callable[[Store, pytest.MonkeyPatch], int]] = {
    "append": _append,
    "append_capture": _append_capture,
    "set_birth_classification": _set_birth_classification,
    "revoke_relation": _revoke_relation,
    "expire_staged_proposals": _expire_staged_proposals,
    "reject_unsupported_evidence": _reject_unsupported_evidence,
}


@pytest.mark.parametrize("composite", sorted(COMPOSITES))
def test_a_composite_that_fails_midway_leaves_no_row_of_its_first_write(
    composite: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store()
    first_write_rows = COMPOSITES[composite](store, monkeypatch)
    assert first_write_rows == 0, (
        f"{composite}: the first write committed although the composite failed; "
        "its writes are not one transaction"
    )


# -- (b) the bookkeeping writers ------------------------------------------------
#
# Each case: set up, then return (the write, a probe of the row it changes).


Probe = Callable[[], object]
Case = tuple[Callable[[], object], Probe]


def _record_verification(store: Store) -> Case:
    cid = _cap(store, "verified")
    return (
        lambda: store.anchors.record_verification(
            cid, kind="anchor", result="pass", detail="d", actor="t", now=NOW
        ),
        lambda: _count(store, "SELECT count(*) FROM verification_events"),
    )


def _record_system1_observation(store: Store) -> Case:
    cid = _cap(store, "observed")
    return (
        lambda: store.reviews.record_system1_observation(
            cid,
            question="curator",
            subject="s",
            choice="holds",
            p=0.5,
            confidence=None,
            model="m",
            now=NOW,
        ),
        lambda: _count(store, "SELECT count(*) FROM jev_observations"),
    )


def _append_dod_event(store: Store) -> Case:
    cid = _cap(store, "dod")
    return (
        lambda: store.dod.append_dod_event(
            cid, "status", "accepted", "owner", "", format_rfc3339(NOW)
        ),
        lambda: _count(store, "SELECT count(*) FROM dod_events"),
    )


def _job_column(store: Store, cid: str, column: str) -> object:
    with store.db.connection() as conn, conn.cursor() as cur:
        row = cur.execute(
            f"SELECT {column} FROM embedding_jobs WHERE capsule_id = ?", (cid,)
        ).fetchone()
    return None if row is None else row[0]


def _job_for_capsule(store: Store, cid: str) -> EmbeddingJob | None:
    with store.db.connection() as conn, conn.cursor() as cur:
        row = cur.execute(
            "SELECT capsule_id, index_text, text_fingerprint, target_space"
            " FROM embedding_jobs WHERE capsule_id = ?",
            (cid,),
        ).fetchone()
    return None if row is None else EmbeddingJob(*map(str, row))


def _queue_embeddings(store: Store) -> Case:
    cid = _cap(store, "queued")
    fingerprint = sha256_hex(b"another text")
    return (
        lambda: store.embeddings.queue_embeddings(
            [(cid, "another text", fingerprint)], "space-uow"
        ),
        lambda: _job_column(store, cid, "target_space"),
    )


def _prune_embeddings(store: Store) -> Case:
    cid = _cap(store, "pruned")
    with store.db.write_boundary(), store.db.connection() as conn:
        conn.execute("UPDATE capsules SET canonical_json = NULL WHERE id = ?", (cid,))
    return (
        lambda: store.embeddings.prune_embeddings(),
        lambda: _count(store, "SELECT count(*) FROM embedding_jobs"),
    )


def _claim_embedding(store: Store) -> Case:
    cid = _cap(store, "claimed")
    return (
        lambda: store.embeddings.claim_embedding(),
        lambda: _job_column(store, cid, "attempts"),
    )


def _complete_embedding(store: Store) -> Case:
    cid = _cap(store, "completed")
    job = _job_for_capsule(store, cid)
    assert job is not None
    return (
        lambda: store.embeddings.complete_embedding(
            job, [1.0] + [0.0] * (DIMENSION - 1)
        ),
        lambda: _count(store, "SELECT count(*) FROM capsule_embeddings"),
    )


def _fail_embedding(store: Store) -> Case:
    cid = _cap(store, "failed")
    job = _job_for_capsule(store, cid)
    assert job is not None
    return (
        lambda: store.embeddings.fail_embedding(job, "TimeoutError"),
        lambda: _job_column(store, cid, "last_error_class"),
    )


def _embedding_state(store: Store, active: str, target: str | None) -> Probe:
    with store.db.writing() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE embedding_state SET active_space = ?, target_space = ?",
            (active, target),
        )

    def state() -> object:
        with store.db.connection() as conn, conn.cursor() as cur:
            return cur.execute(
                "SELECT active_space, target_space, phase FROM embedding_state"
            ).fetchone()

    return state


def _begin_space_transition(store: Store) -> Case:
    return (
        lambda: store.embeddings.begin_current_space_transition_if_needed(),
        _embedding_state(store, "space-before", None),
    )


def _activate_target(store: Store) -> Case:
    return (
        lambda: store.embeddings.activate_target_if_complete(),
        _embedding_state(store, "space-before", "space-after"),
    )


def _record_read_traffic(store: Store) -> Case:
    cid = _cap(store, "read")
    return (
        lambda: store.db.record_read_traffic(
            surface="context",
            session_id=None,
            terms="t",
            served_ids=[cid],
            project_id=None,
            project_prefix=None,
            outcome=None,
            now=NOW,
        ),
        lambda: _count(store, "SELECT count(*) FROM read_traffic"),
    )


BOOKKEEPING: dict[str, Callable[[Store], Case]] = {
    "record_verification": _record_verification,
    "record_system1_observation": _record_system1_observation,
    "append_dod_event": _append_dod_event,
    "queue_embeddings": _queue_embeddings,
    "prune_embeddings": _prune_embeddings,
    "claim_embedding": _claim_embedding,
    "complete_embedding": _complete_embedding,
    "fail_embedding": _fail_embedding,
    "begin_current_space_transition_if_needed": _begin_space_transition,
    "activate_target_if_complete": _activate_target,
    "record_read_traffic": _record_read_traffic,
}


@pytest.mark.parametrize("writer", sorted(BOOKKEEPING))
def test_a_bookkeeping_writer_changes_a_row_and_is_not_a_generation(
    writer: str,
) -> None:
    store = _store()
    write, probe = BOOKKEEPING[writer](store)
    before, generation = probe(), store.db.get_generation()
    write()
    assert probe() != before, f"{writer} changed no row; the case measures nothing"
    assert store.db.get_generation() == generation, (
        f"{writer} advanced the generation: a bookkeeping write became canonical"
    )


# -- the repository writers no other test reached ----------------------------------
