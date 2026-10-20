"""Effort and conversation binding share an admission, never a journal payload."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import asdict
from pathlib import Path
from typing import Any

import nott_database
import pytest
from registry_reset import sql, store

from nspawn.registry import Registry, RegistryError, WorktreeHeld
from nspawn.rows import Backend, NativeSessionContext, RunPhase, RunRow


@pytest.fixture
def registry() -> Iterator[Registry]:
    with Registry(store(), "effort-binding-owner") as registry:
        registry.open(1)
        yield registry


def _row(registry: Registry, worktree: Path, run_id: str = "combined") -> RunRow:
    return RunRow(
        run_id=run_id,
        worktree=str(worktree),
        phase=RunPhase.PENDING,
        sandbox="read-only",
        model="fixture-model",
        owner_instance=registry.instance_id,
        created_at=1,
        updated_at=1,
        backend=Backend.CODEX,
        executed_provider="codex",
        executed_account="fixture-account",
    )


def _binding() -> dict[str, Any]:
    return {
        "orchestrator_binding": {
            "session": "conversation-1",
            "profile": "nott-nspawn-orchestrator-combined",
            "tool_contract": {"mcp": "private", "project": "fixture", "tools": ["t"]},
            "repository": "/repository",
            "snapshot": "/snapshot",
            "runtime": "/runtime",
            "database_dir": "/store",
            "interpreter": "/usr/bin/python3",
        }
    }


def _admit(
    registry: Registry,
    worktree: Path,
    *,
    run_id: str = "combined",
    effort: Any = None,
    binding: Any = None,
) -> None:
    registry.admit_run(
        _row(registry, worktree, run_id),
        True,
        1000,
        60_000,
        native_context=NativeSessionContext("/account"),
        effort_profile={"requested": "max", "sent": "high"}
        if effort is None
        else effort,
        orchestrator_binding=_binding() if binding is None else binding,
    )


def _assert_absent(registry: Registry, worktree: Path) -> None:
    assert registry.get_run("combined") is None
    assert registry.read_run_events("combined") == []
    assert registry.read_claim(str(worktree)) is None
    assert sql(registry, "SELECT run_id FROM native_contexts") == []


def test_both_records_are_separate_and_readable_after_one_admission(
    registry: Registry, tmp_path: Path
) -> None:
    _admit(registry, tmp_path)

    events = registry.read_run_events("combined")
    assert [(event.seq, event.kind) for event in events] == [
        (1, "lifecycle"),
        (2, "lifecycle"),
        (3, "lifecycle"),
    ]
    assert events[0].payload == {
        "usage_pending": "exec",
        "usage_managed": False,
        "usage_identity": None,
    }
    assert len([e for e in events if e.payload == _binding()]) == 1
    assert (
        len(
            [
                e
                for e in events
                if e.payload == {"effort_profile": {"requested": "max", "sent": "high"}}
            ]
        )
        == 1
    )
    assert registry.verify_run_event_chain("combined") is True
    assert (
        registry.orchestrator_binding("combined") == _binding()["orchestrator_binding"]
    )
    assert asdict(registry.effort_profile("combined")) == {
        "requested": "max",
        "requested_source": "admission",
        "sent": "high",
        "sent_source": "admission",
        "executed": None,
        "executed_source": "unknown",
    }
    assert registry.get_run("combined").phase is RunPhase.SPAWNING
    assert registry.read_claim(str(tmp_path)).run_id == "combined"


@pytest.mark.parametrize("failure", ["first-record", "second-record", "phase"])
def test_no_partial_admission_survives_either_record_or_final_transition(
    registry: Registry, tmp_path: Path, failure: str
) -> None:
    fence = (
        "BEFORE UPDATE OF phase ON runs WHEN NEW.run_id = 'combined' "
        "AND NEW.phase = 'spawning'"
        if failure == "phase"
        else "BEFORE INSERT ON run_events WHEN NEW.run_id = 'combined' "
        f"AND NEW.seq = {2 if failure == 'first-record' else 3}"
    )
    sql(
        registry,
        f"CREATE TEMP TRIGGER combined_fixture_fence {fence} "
        "BEGIN SELECT RAISE(ABORT, 'combined fixture refuses admission'); END",
    )
    try:
        with pytest.raises((RegistryError, nott_database.IntegrityError)):
            _admit(registry, tmp_path)
    finally:
        sql(registry, "DROP TRIGGER IF EXISTS temp.combined_fixture_fence")
    _assert_absent(registry, tmp_path)


@pytest.mark.parametrize("malformed", ["effort", "binding"])
def test_invalid_combined_input_leaves_no_row_record_context_or_claim(
    registry: Registry, tmp_path: Path, malformed: str
) -> None:
    with pytest.raises(RegistryError):
        _admit(
            registry,
            tmp_path,
            effort={"requested": "max"} if malformed == "effort" else None,
            binding={"orchestrator_binding": {}} if malformed == "binding" else None,
        )
    _assert_absent(registry, tmp_path)


def test_claim_refusal_keeps_the_original_pair_and_no_second_admission(
    registry: Registry, tmp_path: Path
) -> None:
    _admit(registry, tmp_path)
    original = registry.read_run_events("combined")
    with pytest.raises(WorktreeHeld):
        _admit(registry, tmp_path, run_id="refused")

    assert registry.get_run("refused") is None
    assert registry.read_run_events("refused") == []
    assert registry.read_run_events("combined") == original
    assert registry.read_claim(str(tmp_path)).run_id == "combined"
