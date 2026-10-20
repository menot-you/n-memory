"""The reaper lies about runs that TERMINATED, killing the instance forever.

Executable reproducer of the two defects measured in the live registry on
2026-08-31 (the production nspawn registry): 665 `orphaned` runs
versus ONE `completed`, **501 of them with `final_text` recorded**, and
59 distinct `owner_instance`s without a single row in `instances`.

Neither requires load."""

import pytest
from registry_reset import reset_registry, store

from nspawn.registry import Registry
from nspawn.rows import Backend, RunPhase, RunRow
from nspawn.supervisor import Supervisor


def _url() -> str:
    return str(store())


@pytest.fixture
def owner():  # type: ignore[no-untyped-def]
    registry = Registry(_url(), "i-owner").connect()
    reset_registry(registry, now=1000)
    yield registry
    registry.close()


@pytest.fixture
def rival(owner):  # type: ignore[no-untyped-def]
    registry = Registry(_url(), "i-rival").connect()
    registry.open(now=1000)
    yield registry
    registry.close()


def _row(run_id: str, **overrides: object) -> RunRow:
    base: dict[str, object] = {
        "run_id": run_id,
        "worktree": f"/tmp/{run_id}",
        "phase": RunPhase.PENDING,
        "sandbox": "read-only",
        "model": "opus",
        "owner_instance": "i-rival",
        "created_at": 1,
        "updated_at": 2,
        "backend": Backend.CLAUDE,
    }
    base.update(overrides)
    return RunRow(**base)  # type: ignore[arg-type]


@pytest.mark.parametrize("phase", [RunPhase.COMPLETED, RunPhase.FAILED])
def test_a_terminal_run_is_never_relabelled_orphaned(owner, rival, phase) -> None:  # type: ignore[no-untyped-def]
    """`rows.py` calls COMPLETED and FAILED terminal; the SQL fence does not.

    Measured on live registry: 501 `orphaned` runs carry `final_text`. An
    entire verdict reads the same as a run that never reported — the same defect
    class that D18 exists to prevent.
    """
    assert phase.is_terminal()
    owner.insert_run(_row("r-done", phase=phase, final_text="o veredito inteiro"))
    rival.heartbeat_instance(1000)

    owner.mark_orphans(now=100_000, stale_after_ms=1_000)

    assert owner.get_run("r-done").phase is phase


def _reaper(registry, tmp_path):  # type: ignore[no-untyped-def]
    """The live supervisor, with its reaper. The tick runs by hand, never the clock.

    `TICK_INTERVAL_S` is 10 s and each test finishes in under one; the daemon
    never wakes up on its own within the window, so the synthetic timeline
    holds up.
    """
    supervisor = Supervisor(registry, tmp_path / registry.instance_id)
    yield supervisor
    supervisor._stop_tick.set()


@pytest.fixture
def rival_reaper(rival, tmp_path):  # type: ignore[no-untyped-def]
    yield from _reaper(rival, tmp_path)


def test_a_swept_instance_takes_its_row_back(owner, rival, rival_reaper) -> None:  # type: ignore[no-untyped-def]
    """A missed window cannot erase the instance FOREVER.

    `mark_orphans` DELETES the stale peer's row and `heartbeat_instance` is an
    UPDATE: without re-registration it returns False forever. 59 instances with
    orphans in live registry, zero with row.
    """
    owner.mark_orphans(now=100_000, stale_after_ms=1_000)
    assert rival.heartbeat_instance(100_500) is False

    rival_reaper._tick(now=101_000)

    assert rival.heartbeat_instance(101_500) is True


def test_a_run_admitted_after_the_sweep_is_not_orphaned(
    owner, rival, rival_reaper
) -> None:  # type: ignore[no-untyped-def]
    """The defect seen from outside, and there is no load in this function."""
    owner.mark_orphans(now=100_000, stale_after_ms=1_000)
    rival_reaper._tick(now=101_000)
    rival.insert_run(_row("r-novo", phase=RunPhase.RUNNING))

    owner.mark_orphans(now=101_500, stale_after_ms=1_000)

    assert owner.get_run("r-novo").phase is RunPhase.RUNNING
