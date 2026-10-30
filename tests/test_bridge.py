"""The Markdown splitter of the context corpus, and the capture outcome row."""

from __future__ import annotations

import json

import pytest
from storefile import sample_capsule_id

from nmemory.bridge import heading_level, split_markdown


def test_heading_boundaries_keep_preamble_and_nested_subsections() -> None:
    assert split_markdown("intro\n## Only\nbody") == [
        (1, "intro"),
        (2, "## Only\nbody"),
    ]
    assert split_markdown("intro\n\n## Only\nbody") == [
        (1, "intro"),
        (3, "## Only\nbody"),
    ]
    assert split_markdown("# One\nbody\n## Nested\ninside\n# Two\nend") == [
        (1, "# One\nbody\n## Nested\ninside"),
        (5, "# Two\nend"),
    ]


def test_frontmatter_is_skipped_and_line_numbers_stay_original() -> None:
    doc = "---\ntitle: cfg\n---\n\nFirst block.\n\nSecond block.\n"
    got = split_markdown(doc)
    assert got == [
        (5, "First block."),
        (7, "Second block."),
    ]


def test_unclosed_frontmatter_opener_is_plain_content() -> None:
    doc = "---\nnot frontmatter, never closed"
    got = split_markdown(doc)
    assert got == [(1, "---\nnot frontmatter, never closed")]


def test_heading_documents_split_per_top_level_section_with_preamble() -> None:
    doc = "intro before any heading\n\n# One\nbody one\n\n# Two\nbody two\n"
    got = split_markdown(doc)
    assert got == [
        (1, "intro before any heading"),
        (3, "# One\nbody one"),
        (6, "# Two\nbody two"),
    ]


def test_lone_h1_title_splits_at_the_first_repeated_deeper_level() -> None:
    doc = "# Title\n\nintro\n\n## A\na body\n\n## B\nb body\n"
    got = split_markdown(doc)
    assert got == [
        (1, "# Title\n\nintro"),
        (5, "## A\na body"),
        (8, "## B\nb body"),
    ]


def test_single_heading_document_is_one_candidate() -> None:
    doc = "# Memory index\n\n- [a](a.md)\n- [b](b.md)\n"
    got = split_markdown(doc)
    assert got == [(1, "# Memory index\n\n- [a](a.md)\n- [b](b.md)")]


def test_fenced_hash_lines_are_not_headings_and_fenced_blanks_do_not_split() -> None:
    doc = "```bash\n# fenced comment\n\nmake build\n```\ntail line\n"
    got = split_markdown(doc)
    assert got == [(1, "```bash\n# fenced comment\n\nmake build\n```\ntail line")]


def test_fenced_hash_lines_never_open_a_section_in_heading_mode() -> None:
    doc = "# Real\n\n```\n# fake heading\n```\n\n# Also real\nbody\n"
    got = split_markdown(doc)
    assert got == [
        (1, "# Real\n\n```\n# fake heading\n```"),
        (7, "# Also real\nbody"),
    ]


def test_plain_paragraph_blocks_split_on_blank_lines() -> None:
    doc = "first spans\ntwo lines\n\n\nsecond block\n"
    got = split_markdown(doc)
    assert got == [
        (1, "first spans\ntwo lines"),
        (5, "second block"),
    ]


def test_long_heading_section_is_bounded_without_losing_lines() -> None:
    lines = ["# Search", *(f"capability {i:04d} " + "detail " * 18 for i in range(180))]
    chunks = split_markdown("\n".join(lines))
    assert len(chunks) > 1
    assert all(len(body) <= 4096 for _, body in chunks)
    assert [line for _, body in chunks for line in body.splitlines()] == lines
    assert chunks[0][0] == 1
    assert chunks[-1][0] > 1


def test_empty_and_whitespace_only_content_yield_zero_candidates() -> None:
    assert split_markdown("") == []
    assert split_markdown("   \n\n\t\n") == []
    assert split_markdown("---\nx: y\n---\n") == []


def test_bom_is_stripped_before_splitting() -> None:
    doc = "\ufeff# H\nbody\n"
    got = split_markdown(doc)
    assert got == [(1, "# H\nbody")]


def test_heading_detection_requires_column_zero_and_a_space() -> None:
    assert heading_level("# ok") == 1
    assert heading_level("###### deep") == 6
    assert heading_level("#") == 1
    assert heading_level("#nope") is None
    assert heading_level("  # indented") is None
    assert heading_level("####### seven") is None


def test_all_blank_preamble_region_is_dropped() -> None:
    doc = "\n\n# H\nbody\n"
    got = split_markdown(doc)
    assert got == [(3, "# H\nbody")]


@pytest.mark.parametrize("deduped", [False, True])
@pytest.mark.parametrize("populated", [False, True])
def test_capture_outcome_row_keeps_exact_values_and_field_order(
    deduped: bool, populated: bool
) -> None:
    from nmemory.ingest import DedupHint, IngestOutcome, SiblingHint
    from nmemory.taint import scan
    from nmemory.tools.ingest import outcome_row

    captured_id = sample_capsule_id(2)
    superseded_id = sample_capsule_id(0)
    findings = scan("system:") if populated else []
    outcome = IngestOutcome(
        id=captured_id,
        deduped=deduped,
        dedup_hint=DedupHint("cap-1", 0.875) if populated else None,
        taint_findings=findings,
        siblings=[SiblingHint("cap-1", 0.75)] if populated else [],
        superseded=superseded_id if populated else None,
        project_canon=("alias", "canonical") if populated else None,
        derivation_findings=findings,
    )
    status = "deduplicated" if deduped else "captured"
    expected = {
        "status": status,
        "id": captured_id,
        "dedup_hint": {"similar_id": "cap-1", "score": 0.875}
        if populated and not deduped
        else None,
    }
    if populated:
        if not deduped:
            expected["siblings"] = [{"id": "cap-1", "score": 0.75}]
        expected["superseded"] = superseded_id
        expected["project_id"] = {"was": "alias", "now": "canonical"}
        if deduped:
            expected["derivation_tainted"] = True
        expected["taint_findings"] = ["role_prefix_injection: system:"] * (
            2 if deduped else 1
        )
        expected["anchor_note"] = "anchor absent"
    actual, reason = outcome_row(
        outcome,
        ["role_prefix_injection: system:"] if populated else [],
        "anchor absent" if populated else None,
    )
    assert json.dumps(actual) == json.dumps(expected)
    assert reason == (
        "deduplicated; derivation tainted" if deduped and populated else status
    )
