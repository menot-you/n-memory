"""The private documentation window follows nMEMORY's live scope and edges."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from nmemory.capsule import AuthorityClass, Capsule, Freshness, Provenance, Scope
from nmemory.portal import render_portal
from nmemory.store import Store

NOW = datetime(2026, 9, 27, 12, tzinfo=UTC)


def add(store: Store, kind: str, content: str, project: str = "pynott") -> str:
    capsule = Capsule(
        content=content,
        provenance=Provenance(source="test", anchor=content, source_hash=content),
        confidence=0.9,
        freshness=Freshness(valid_from=NOW),
        scope=Scope(project_id=project),
        authority_class=AuthorityClass.USER_STATED,
        instruction_taint=False,
    )
    cid, _ = store.append(capsule, NOW)
    store.classifications.upsert(cid, kind, "project", NOW.isoformat())
    return cid


def test_portal_selects_only_current_project_and_open_work() -> None:
    store = Store.open_disposable_test_store()
    decision = add(store, "decision", "Current <decision> ](javascript:alert(1))")
    constraint = add(store, "constraint", "Current constraint")
    plan = add(store, "plan", "Open plan")
    closed_plan = add(store, "plan", "Closed plan")
    epic = add(store, "epic", "Open epic")
    add(store, "decision", "Other project", "monorepo")
    add(store, "decision", "Child project", "pynott/child")
    add(store, "task", "Task is not a portal page")
    witness = add(store, "fact", "Work finished")
    store.relations.assert_edge(
        "witnesses", witness, closed_plan, NOW.isoformat(), "manual"
    )

    result = render_portal(store, "pynott", NOW)
    selected_ids = {row[0] for row in result.selected}
    assert selected_ids == {decision, constraint, plan, epic}
    assert set(result.pages) == {
        "index.md",
        *(f"{cid}.md" for cid in selected_ids),
    }
    assert "&lt;decision&gt;" in result.pages[f"{decision}.md"]
    assert 'href="javascript:' not in result.pages["index.md"]
    assert f'href="{decision}/"' in result.pages["index.md"]
    assert "https://viz.nott.ing/" in result.pages[f"{decision}.md"]
    assert "cap-" in result.pages["index.md"]


def test_portal_digest_changes_for_capsule_only_write() -> None:
    store = Store.open_disposable_test_store()
    add(store, "decision", "First")
    before = render_portal(store, "pynott", NOW)
    add(store, "decision", "Second")
    after = render_portal(store, "pynott", NOW)
    assert before.digest != after.digest


def test_portal_honors_standing_and_staged_review() -> None:
    store = Store.open_disposable_test_store()
    retired = add(store, "decision", "Retired decision")
    current = add(store, "decision", "Replacement decision")
    staged = add(store, "constraint", "Unreviewed proposal")
    store.relations.assert_edge(
        "supersedes", current, retired, NOW.isoformat(), "manual"
    )
    store.reviews.append_review_event(staged, "proposed", "test", "actor", NOW)

    result = render_portal(store, "pynott", NOW)
    assert {row[0] for row in result.selected} == {current}


def test_portal_selection_is_one_read_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    reader = Store.open_disposable_test_store()
    writer = Store.open(reader.db.path)
    listed = reader.reviews.list_review_fenced
    inserted: list[str] = []

    def write_staged_after_first_read() -> list[str]:
        prior = listed()
        cid = add(writer, "decision", "Staged during export")
        writer.reviews.append_review_event(cid, "proposed", "test", "actor", NOW)
        inserted.append(cid)
        return prior

    monkeypatch.setattr(
        reader.reviews, "list_review_fenced", write_staged_after_first_read
    )
    result = render_portal(reader, "pynott", NOW)
    assert inserted[0] not in {row[0] for row in result.selected}
