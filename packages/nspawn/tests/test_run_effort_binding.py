"""The effort record shares its admission, and an old orchestrator binding reads."""

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


#: The lifecycle payload an orchestrator turn recorded before #1297 deleted the
#: private MCP it launched. nSPAWN no longer writes it, and a store still holds it.
HISTORICAL_BINDING: dict[str, Any] = {
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
    )


def _assert_absent(registry: Registry, worktree: Path) -> None:
    assert registry.get_run("combined") is None
    assert registry.read_run_events("combined") == []
    assert registry.read_claim(str(worktree)) is None
    assert sql(registry, "SELECT run_id FROM native_contexts") == []


def test_the_effort_record_is_readable_after_one_admission(
    registry: Registry, tmp_path: Path
) -> None:
    _admit(registry, tmp_path)

    events = registry.read_run_events("combined")
    assert [(event.seq, event.kind) for event in events] == [
        (1, "lifecycle"),
        (2, "lifecycle"),
    ]
    assert events[0].payload == {
        "usage_pending": "exec",
        "usage_managed": False,
        "usage_identity": None,
    }
    assert events[1].payload == {"effort_profile": {"requested": "max", "sent": "high"}}
    assert registry.verify_run_event_chain("combined") is True
    assert registry.orchestrator_bound("combined") is False
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


@pytest.mark.parametrize("failure", ["record", "phase"])
def test_no_partial_admission_survives_the_record_or_final_transition(
    registry: Registry, tmp_path: Path, failure: str
) -> None:
    fence = (
        "BEFORE UPDATE OF phase ON runs WHEN NEW.run_id = 'combined' "
        "AND NEW.phase = 'spawning'"
        if failure == "phase"
        else "BEFORE INSERT ON run_events WHEN NEW.run_id = 'combined' AND NEW.seq = 2"
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


def test_invalid_effort_leaves_no_row_record_context_or_claim(
    registry: Registry, tmp_path: Path
) -> None:
    with pytest.raises(RegistryError):
        _admit(registry, tmp_path, effort={"requested": "max"})
    _assert_absent(registry, tmp_path)


def test_claim_refusal_keeps_the_original_record_and_no_second_admission(
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


@pytest.mark.parametrize(
    "payload",
    [HISTORICAL_BINDING, {"orchestrator_binding": {}}, {"orchestrator_binding": 7}],
    ids=["whole", "empty", "not-an-object"],
)
def test_a_historical_orchestrator_binding_still_reads(
    registry: Registry, tmp_path: Path, payload: dict[str, Any]
) -> None:
    """The store keeps what an old orchestrator turn recorded. The chain still
    closes, the other records still read, and only the key is answered, so no
    shape of that old payload makes a read fail."""
    _admit(registry, tmp_path)
    registry.append_run_event("combined", at=2000, kind="lifecycle", payload=payload)

    events = registry.read_run_events("combined")
    assert events[-1].payload == payload
    assert registry.verify_run_event_chain("combined") is True
    assert registry.orchestrator_bound("combined") is True
    assert registry.effort_profile("combined").sent == "high"
    assert registry.inference_profile("combined") is None
    assert registry.writer_scope("combined") is None
