from __future__ import annotations

import pytest
from storefile import sample_capsule_id

from nmemory.capsule_ref import (
    CapsuleRefSyntaxError,
    ParsedCapsuleRef,
    contains_exact_ref,
    prefix_for_kind,
    slugify,
)

# --- Capsule Ref Tests ---


def test_slug_and_reference_grammar_are_deterministic() -> None:
    assert (
        slugify("# Implementar memória em lote — agora")
        == "implementar-memoria-em-lote-agora"
    )
    parsed = ParsedCapsuleRef.parse("failure-pattern-12-timeout-repetido")
    assert parsed.canonical_id() == sample_capsule_id(12)
    assert parsed.prefix == "failure-pattern"
    assert parsed.sequence == 12
    assert parsed.slug == "timeout-repetido"

    assert ParsedCapsuleRef.parse("task-12").canonical_id() == sample_capsule_id(12)
    assert ParsedCapsuleRef.parse(sample_capsule_id(12)).is_canonical() is True

    with pytest.raises(CapsuleRefSyntaxError):
        ParsedCapsuleRef.parse("task-012")

    with pytest.raises(CapsuleRefSyntaxError):
        ParsedCapsuleRef.parse("unknown-12")

    with pytest.raises(CapsuleRefSyntaxError):
        ParsedCapsuleRef.parse("task-42-Invalid_Slug!")

    assert contains_exact_ref(
        ("see (" + sample_capsule_id(12) + "), now"), sample_capsule_id(12)
    )
    assert not contains_exact_ref(
        ("see " + sample_capsule_id(123)), sample_capsule_id(12)
    )
    assert not contains_exact_ref(
        ("see task-" + sample_capsule_id(12) + "-old"), sample_capsule_id(12)
    )


def test_prefix_for_kind_mappings() -> None:
    assert prefix_for_kind("fact") == "fact"
    assert prefix_for_kind("procedure") == "procedure"
    assert prefix_for_kind("decision") == "decision"
    assert prefix_for_kind("task") == "task"
    assert prefix_for_kind("epic") == "epic"
    assert prefix_for_kind("plan") == "plan"
    assert prefix_for_kind("brainstorm") == "brainstorm"
    assert prefix_for_kind("doc") == "doc"
    assert prefix_for_kind("constraint") == "constraint"
    assert prefix_for_kind("capability") == "capability"
    assert prefix_for_kind("failure_pattern") == "failure-pattern"
    assert prefix_for_kind("dod") == "dod"
    assert prefix_for_kind("unknown") is None
    assert prefix_for_kind(None) is None


@pytest.mark.parametrize(
    "kind,prefix",
    [
        ("fact", "fact"),
        ("procedure", "procedure"),
        ("decision", "decision"),
        ("task", "task"),
        ("epic", "epic"),
        ("plan", "plan"),
        ("brainstorm", "brainstorm"),
        ("doc", "doc"),
        ("constraint", "constraint"),
        ("capability", "capability"),
        ("failure_pattern", "failure-pattern"),
        ("dod", "dod"),
    ],
)
def test_birth_classification_keeps_exact_persisted_handle(
    kind: str, prefix: str
) -> None:
    from datetime import UTC, datetime

    from nmemory.capsule import Capsule, Freshness, Provenance, Scope, sha256_hex
    from nmemory.store import Store

    now = datetime(2026, 8, 21, tzinfo=UTC)
    store = Store.open_disposable_test_store()
    capsule = Capsule(
        content="Stable handle",
        provenance=Provenance("test", "test:1", sha256_hex(b"Stable handle")),
        confidence=0.9,
        freshness=Freshness(valid_from=now),
        scope=Scope(project_id="test"),
        authority_class="observed-fact",
        instruction_taint=False,
    )
    cid, created = store.append(capsule, now)
    assert (cid, created) == ("cap-1", True)
    store.set_birth_classification(cid, " " + kind.upper() + " ", "project", now)
    with store.db.connection() as conn, conn.cursor() as cur:
        row = cur.execute(
            "SELECT capsule_id, prefix, slug FROM capsule_handles"
        ).fetchone()
        assert row == ("cap-1", prefix, "stable-handle")
    assert store.capsules.get_handle(cid) == prefix + "-1-stable-handle"
    store.set_birth_classification(cid, "task", "project", now)
    assert store.capsules.get_handle(cid) == prefix + "-1-stable-handle"
