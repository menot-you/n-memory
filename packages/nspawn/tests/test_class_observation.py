"""What System1 saw and said about a run's execution class is kept with the run.

The weekly feedback job labels each finished run by the class it ran as. That needs,
per admitted run, the exact text System1 read, the declared class, the observed
class with its confidence, and the effective class — and an export that yields a
row only once the run's phase is terminal.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import nott_database
import pytest
from registry_reset import store
from supervisor_support import (
    _drain_worker_threads,
    _stop_every_reaper,
    _sv,
    fake_picked,
    fake_provider,
    registry,
    roster_home,
    wired_success,
)

from nspawn import cli, system1
from nspawn.registry import Registry
from nspawn.rows import Backend, ClassObservation, RunPhase, RunRow
from nspawn.schema import REGISTRY_SCHEMA_VERSION
from nspawn.supervisor import Supervisor

__all__ = [
    "_drain_worker_threads",
    "_stop_every_reaper",
    "fake_picked",
    "fake_provider",
    "registry",
    "roster_home",
    "wired_success",
]

#: The host's table: review and verification cost the same, so the observed
#: `review` is not cheaper on every axis and the run keeps `verification`.
#: (Declared `review` a Claude read-only run is caged; this measures the class.)
_CLASSES = (
    "[classes]\n"
    'verification = { effort = "medium", ceiling = "high" }\n'
    'review = { effort = "medium", ceiling = "high" }\n'
    'unclassified = { effort = "low", ceiling = "medium" }\n'
)

_PROMPT = "check that the migration keeps every existing row"


def _observing(value: str, confidence: float) -> object:
    def ask(
        state: object,
        questions: dict[str, object],
        model: object = None,
        timeout: float | None = None,
        scope: str | None = None,
    ) -> dict[str, system1.Answer]:
        ident = next(iter(questions))
        return {
            ident: system1.Answer(
                id=ident,
                type="choice",
                value=value,
                probabilities={value: confidence},
                confidence=confidence,
                legend=None,
            )
        }

    return ask


def test_a_dispatch_keeps_what_system1_observed_and_exports_it_once_completed(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    wired_success: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roster = roster_home / ".nott" / "spawn.toml"
    roster.write_text(roster.read_text(encoding="utf-8") + _CLASSES, encoding="utf-8")
    monkeypatch.setattr(system1, "ask", _observing("review", 0.9))
    sv = _sv(registry, tmp_path)

    accepted = sv.execute(
        _PROMPT, str(tmp_path), read_only=True, execution_class="verification"
    )
    assert sv.status(accepted.run_id, timeout_ms=5000).phase is RunPhase.COMPLETED

    rows = registry.export_class_observations()
    assert len(rows) == 1
    (row,) = rows
    assert row.run_id == accepted.run_id
    assert row.phase is RunPhase.COMPLETED
    assert row.observation == ClassObservation(
        text=_PROMPT,
        declared="verification",
        observed="review",
        confidence=0.9,
        effective="verification",
    )
    assert row.label == "verification"


def test_a_run_still_in_progress_exports_nothing(
    registry: Registry, capsys: pytest.CaptureFixture[str]
) -> None:
    observation = ClassObservation(
        text="rename x to y",
        declared="review",
        observed="verification",
        confidence=0.8,
        effective="review",
    )
    row = RunRow(
        run_id="r-1",
        worktree="/w",
        phase=RunPhase.PENDING,
        sandbox="read-only",
        model="opus",
        owner_instance=registry.instance_id,
        created_at=5,
        updated_at=5,
        backend=Backend.CLAUDE,
    )
    registry.admit_run(row, False, 5, 1000, class_observation=observation)

    assert registry.export_class_observations() == ()
    assert cli.main(["learning", "export"]) == 0
    assert capsys.readouterr().out == ""

    stored = registry.get_run("r-1")
    assert stored is not None
    registry.update_run(replace(stored, phase=RunPhase.COMPLETED, updated_at=9))
    (exported,) = registry.export_class_observations()
    assert (exported.run_id, exported.observed_at) == ("r-1", 5)
    assert exported.observation == observation
    assert exported.label == "review"
    assert cli.main(["learning", "export"]) == 0
    (line,) = capsys.readouterr().out.splitlines()
    assert json.loads(line) == {
        "run_id": "r-1",
        "observed_at": 5,
        "phase": "completed",
        "outcome_at": 9,
        "text": "rename x to y",
        "declared": "review",
        "observed": "verification",
        "confidence": 0.8,
        "label": "review",
    }


def test_an_unavailable_system1_is_kept_as_no_observation() -> None:
    observation = ClassObservation(
        text="t", declared="review", observed=None, confidence=None, effective="review"
    )
    assert observation.observed is None
    with pytest.raises(ValueError, match="confidence"):
        ClassObservation(
            text="t",
            declared="review",
            observed="review",
            confidence=None,
            effective="review",
        )


def test_a_version_1_store_gains_the_table_on_open_and_keeps_its_runs() -> None:
    path = store()
    connection = nott_database.connect(path)
    try:
        with nott_database.write(connection):
            connection.execute("DROP TRIGGER runs_record_class_outcome")
            connection.execute("DROP TABLE class_observations")
            connection.execute("DROP TRIGGER run_keys_refuse_fenced")
            connection.execute("DROP TABLE key_fences")
            connection.execute("DROP TABLE owner_stamps")
            connection.execute(
                "INSERT INTO runs(run_id, worktree, phase, sandbox, model, "
                "owner_instance, created_at, updated_at, backend) VALUES "
                "('old', '/w', 'completed', 'read-only', 'opus', 'i', 1, 1, 'claude')"
            )
            connection.execute("PRAGMA user_version = 1")
    finally:
        connection.close()

    with Registry(path, "i-upgrade") as upgraded:
        assert upgraded.open(now=1) == REGISTRY_SCHEMA_VERSION == 6
        assert upgraded.get_run("old") is not None
        assert upgraded.export_class_observations() == ()
        # The upgraded store records the outcome too: its trigger came with it.
        upgraded.admit_run(
            RunRow(
                run_id="new",
                worktree="/w",
                phase=RunPhase.PENDING,
                sandbox="read-only",
                model="opus",
                owner_instance="i-upgrade",
                created_at=3,
                updated_at=3,
                backend=Backend.CLAUDE,
            ),
            False,
            3,
            1000,
            class_observation=ClassObservation(
                text="t",
                declared="review",
                observed=None,
                confidence=None,
                effective="review",
            ),
        )
        stored = upgraded.get_run("new")
        assert stored is not None
        upgraded.update_run(replace(stored, phase=RunPhase.FAILED, updated_at=4))
        (exported,) = upgraded.export_class_observations()
        assert (exported.run_id, exported.phase) == ("new", RunPhase.FAILED)
    # A second open finds the current version and adds nothing.
    with Registry(path, "i-again") as again:
        assert again.open(now=2) == 6


def test_the_observation_outlives_its_run_being_pruned(registry: Registry) -> None:
    observation = ClassObservation(
        text="audit the diff",
        declared="review",
        observed="verification",
        confidence=0.7,
        effective="review",
    )
    row = RunRow(
        run_id="r-pruned",
        worktree="/w",
        phase=RunPhase.PENDING,
        sandbox="read-only",
        model="opus",
        owner_instance=registry.instance_id,
        created_at=5,
        updated_at=5,
        backend=Backend.CLAUDE,
    )
    registry.admit_run(row, False, 5, 1000, class_observation=observation)
    stored = registry.get_run("r-pruned")
    assert stored is not None
    registry.update_run(replace(stored, phase=RunPhase.COMPLETED, updated_at=9))
    registry.update_run(replace(stored, phase=RunPhase.CLOSED, updated_at=10))

    later = 10 + Supervisor.RETAIN_CLOSED_MS + 1
    assert (
        registry.prune_closed_runs(
            later, Supervisor.RETAIN_CLOSED_MS, Supervisor.MAX_CLOSED_RUNS
        )
        == 1
    )
    assert registry.get_run("r-pruned") is None

    (exported,) = registry.export_class_observations()
    assert exported.run_id == "r-pruned"
    # The outcome is the first terminal phase the run reached; its close after
    # completing does not overwrite it.
    assert (exported.phase, exported.outcome_at) == (RunPhase.COMPLETED, 9)
    assert exported.observation == observation
    assert exported.label == "review"
