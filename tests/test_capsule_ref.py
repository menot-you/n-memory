from __future__ import annotations

import pytest
from storefile import sample_capsule_id

from nmemory.capsule_ref import (
    CapsuleHandle,
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

    assert contains_exact_ref(
        ("see (" + sample_capsule_id(12) + "), now"), sample_capsule_id(12)
    )
    assert not contains_exact_ref(
        ("see " + sample_capsule_id(123)), sample_capsule_id(12)
    )
    assert not contains_exact_ref(
        ("see task-" + sample_capsule_id(12) + "-old"), sample_capsule_id(12)
    )


def test_capsule_handle_display_and_validation() -> None:
    h = CapsuleHandle.new(sample_capsule_id(42), "task", "index-project-context")
    assert h is not None
    assert str(h) == "task-42-index-project-context"
    assert h.sequence == 42
    assert h.prefix == "task"
    assert h.slug == "index-project-context"

    h2 = CapsuleHandle.new(sample_capsule_id(42), "cap", None)
    assert h2 is not None
    assert str(h2) == sample_capsule_id(42)
    assert h2.slug is None

    # Invalid handle
    assert CapsuleHandle.new("invalid-42", "task", "slug") is None
    assert CapsuleHandle.new(sample_capsule_id(42), "invalid_prefix", "slug") is None
    assert CapsuleHandle.new(sample_capsule_id(42), "task", "Invalid_Slug!") is None


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
