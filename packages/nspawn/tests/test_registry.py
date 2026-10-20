"""One worktree, one writer — the claim lifecycle against a real registry file."""

import os
import threading
from typing import cast

import nott_database
import pytest
from hypothesis import given
from hypothesis import strategies as st
from registry_reset import reset_registry, sql, store

from nspawn import registry as registry_module
from nspawn.registry import (
    POISONED_LEASE_UNTIL,
    CloseOutcome,
    Registry,
    RegistryError,
    WorktreeHeld,
    store_path,
)
from nspawn.rows import AccountUsageRow
from nspawn.schema import REGISTRY_SCHEMA_VERSION, WALL_SOURCE_VALUES

WT = "/tmp/wt-claim"


def _url() -> str:
    return str(store())


def _unopened() -> str:
    """The store's path, never created: for tests that must not reach a store."""
    return str(store_path())


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


def _insert_run(registry, run_id: str, phase: str = "running", **cols: object) -> None:  # type: ignore[no-untyped-def]
    names = [
        "run_id",
        "worktree",
        "phase",
        "sandbox",
        "model",
        "owner_instance",
        "created_at",
        "updated_at",
        "backend",
    ]
    values: list[object] = [
        run_id,
        WT,
        phase,
        "read-only",
        "opus",
        registry.instance_id,
        1,
        2,
        "claude",
    ]
    for name, value in cols.items():
        names.append(name)
        values.append(value)
    placeholders = ", ".join(["?"] * len(values))
    sql(
        registry,
        f"INSERT INTO runs({', '.join(names)}) VALUES ({placeholders})",
        tuple(values),
    )


def test_open_is_idempotent_and_registers_the_instance(owner) -> None:  # type: ignore[no-untyped-def]
    assert owner.open(now=1100) == REGISTRY_SCHEMA_VERSION
    (row,) = sql(
        owner, "SELECT heartbeat_at FROM instances WHERE instance_id = 'i-owner'"
    )
    assert row["heartbeat_at"] == 1100


def test_the_heartbeat_reports_whether_the_row_is_still_there(owner) -> None:  # type: ignore[no-untyped-def]
    assert owner.heartbeat_instance(2000)
    owner.remove_instance()
    assert not owner.heartbeat_instance(3000)


def test_removing_an_instance_leaves_the_runs_it_owned(owner) -> None:  # type: ignore[no-untyped-def]
    _insert_run(owner, "r-survivor")
    owner.remove_instance()
    (remaining,) = sql(
        owner, "SELECT count(*) AS n FROM runs WHERE run_id = 'r-survivor'"
    )
    assert remaining["n"] == 1


def test_a_second_claimer_is_refused_while_the_lease_is_live(owner, rival) -> None:  # type: ignore[no-untyped-def]
    claim = owner.claim_worktree(WT, "r-1", now=1000, lease_ms=500)
    assert claim.holder == "i-owner"
    assert claim.lease_until == 1500
    with pytest.raises(WorktreeHeld) as held:
        rival.claim_worktree(WT, "r-2", now=1200, lease_ms=500)
    assert held.value.holder == "i-owner"
    assert held.value.run_id == "r-1"
    assert held.value.lease_until == 1500
    # The refusal names the way out: a finished run keeps its claim until closed.
    assert "nspawn_run_close" in str(held.value)
    assert "'r-1'" in str(held.value)


def test_an_expired_lease_on_a_run_with_no_custody_is_takeable(owner, rival) -> None:  # type: ignore[no-untyped-def]
    owner.claim_worktree(WT, "r-1", now=1000, lease_ms=500)
    _insert_run(owner, "r-1", phase="completed")
    taken = rival.claim_worktree(WT, "r-2", now=2000, lease_ms=500)
    assert taken.holder == "i-rival"
    assert owner.read_claim(WT).run_id == "r-2"


def test_the_lease_boundary_is_exclusive_a_lease_expires_at_its_own_instant(
    owner, rival
) -> None:  # type: ignore[no-untyped-def]
    # lease_until == now means the lease has RUN OUT, not that it is still live:
    # the oracle refuses only while `lease_until > now`.
    owner.claim_worktree(WT, "r-1", now=1000, lease_ms=500)
    _insert_run(owner, "r-1", phase="completed")
    with pytest.raises(WorktreeHeld):
        rival.claim_worktree(WT, "r-2", now=1499, lease_ms=1)
    assert rival.claim_worktree(WT, "r-2", now=1500, lease_ms=1).holder == "i-rival"


def test_an_expired_lease_does_not_release_a_run_that_still_holds_custody(
    owner, rival
) -> None:  # type: ignore[no-untyped-def]
    # The lease is long gone, but the run still carries its pid triple, so the
    # worktree stays fenced: a process may still be alive inside it.
    owner.claim_worktree(WT, "r-1", now=1000, lease_ms=1)
    _insert_run(
        owner,
        "r-1",
        phase="running",
        process_pid=4242,
        process_boot_id="boot-1",
        process_start_ticks=99,
    )
    with pytest.raises(WorktreeHeld):
        rival.claim_worktree(WT, "r-2", now=999_999, lease_ms=500)


def test_an_orphaned_run_fences_its_worktree_even_with_no_process_fields(
    owner, rival
) -> None:  # type: ignore[no-untyped-def]
    owner.claim_worktree(WT, "r-1", now=1000, lease_ms=1)
    _insert_run(owner, "r-1", phase="orphaned")
    with pytest.raises(WorktreeHeld):
        rival.claim_worktree(WT, "r-2", now=999_999, lease_ms=500)


def test_a_closed_run_stops_fencing_even_carrying_a_process_triple(
    owner, rival
) -> None:  # type: ignore[no-untyped-def]
    owner.claim_worktree(WT, "r-1", now=1000, lease_ms=1)
    _insert_run(
        owner,
        "r-1",
        phase="closed",
        process_pid=4242,
        process_boot_id="boot-1",
        process_start_ticks=99,
    )
    assert (
        rival.claim_worktree(WT, "r-2", now=999_999, lease_ms=500).holder == "i-rival"
    )


def test_only_the_holder_can_release_its_claim(owner, rival) -> None:  # type: ignore[no-untyped-def]
    owner.claim_worktree(WT, "r-1", now=1000, lease_ms=500)

    rival.release_claim(WT, "r-1")
    assert owner.read_claim(WT) is not None
    owner.release_claim(WT, "r-1")
    assert owner.read_claim(WT) is None


def test_a_poisoned_claim_fences_the_worktree_against_everyone(owner, rival) -> None:  # type: ignore[no-untyped-def]
    owner.claim_worktree(WT, "r-1", now=1000, lease_ms=1)
    # Poison is deliberately NOT scoped to the holder: the rival can fence a
    # worktree whose custody it could not establish.
    rival.poison_claim(WT, "r-1")
    assert owner.read_claim(WT).lease_until == POISONED_LEASE_UNTIL
    with pytest.raises(WorktreeHeld):
        rival.claim_worktree(WT, "r-2", now=POISONED_LEASE_UNTIL - 1, lease_ms=1)
    with pytest.raises(WorktreeHeld):
        owner.claim_worktree(WT, "r-3", now=POISONED_LEASE_UNTIL - 1, lease_ms=1)


def test_a_lease_that_would_overflow_is_clamped_not_wrapped(owner) -> None:  # type: ignore[no-untyped-def]
    claim = owner.claim_worktree(WT, "r-1", now=POISONED_LEASE_UNTIL - 5, lease_ms=100)
    assert claim.lease_until == POISONED_LEASE_UNTIL


def test_identifiers_are_refused_before_they_reach_the_database(owner) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(RegistryError, match="claims.worktree"):
        owner.claim_worktree("", "r-1", now=1, lease_ms=1)
    with pytest.raises(RegistryError, match="claims.run_id"):
        owner.claim_worktree(WT, "", now=1, lease_ms=1)
    with pytest.raises(RegistryError, match="instances.instance_id"):
        Registry(_unopened(), "")
    with pytest.raises(RegistryError) as release_run_id:
        owner.release_claim(WT, "")
    assert (
        str(release_run_id.value)
        == "claims.run_id must be between 1 and 512 bytes, got 0"
    )


def test_using_the_registry_before_connecting_is_a_typed_refusal() -> None:
    with pytest.raises(RegistryError, match="not connected"):
        Registry(_unopened(), "i-x").heartbeat_instance(1)


def _row(run_id: str, **overrides: object):  # type: ignore[no-untyped-def]
    from nspawn.rows import Backend, RunPhase, RunRow

    base: dict[str, object] = {
        "run_id": run_id,
        "worktree": WT,
        "phase": RunPhase.PENDING,
        "sandbox": "read-only",
        "model": "opus",
        "owner_instance": "i-owner",
        "created_at": 1,
        "updated_at": 2,
        "backend": Backend.CLAUDE,
    }
    base.update(overrides)
    return RunRow(**base)  # type: ignore[arg-type]


def test_a_run_round_trips_through_the_registry(owner) -> None:  # type: ignore[no-untyped-def]
    from nspawn.process_identity import ProcessIdentity
    from nspawn.rows import RunPhase

    identity = ProcessIdentity(pid=4242, boot_id="boot-1", start_ticks=99)
    owner.insert_run(
        _row(
            "r-1",
            phase=RunPhase.RUNNING,
            process_identity=identity,
            lane="w6",
            tokens=7,
        )
    )
    read = owner.get_run("r-1")
    assert read is not None
    # The triple folds back into ONE value, so a row read cannot carry half of it.
    assert read.process_identity == identity
    assert read.phase is RunPhase.RUNNING
    assert read.lane == "w6"
    assert read.tokens == 7
    assert owner.get_run("r-absent") is None


def test_insert_run_rolls_back_and_reraises_on_a_duplicate_run_id(owner) -> None:  # type: ignore[no-untyped-def]
    owner.insert_run(_row("r-dup"))
    with pytest.raises(nott_database.IntegrityError, match="UNIQUE"):
        owner.insert_run(_row("r-dup"))
    # The failed insert rolled back, so the connection is usable for the next
    # statement rather than left inside a failed transaction.
    owner.insert_run(_row("r-after"))
    assert owner.get_run("r-after") is not None


def test_admit_refuses_when_the_inserted_row_is_not_this_instances_to_start(  # type: ignore[no-untyped-def]
    owner,
) -> None:
    from nspawn.registry import UnknownRun

    with pytest.raises(UnknownRun, match="could not be started"):
        owner.admit_run(
            _row("r-1", owner_instance="i-someone-else"),
            claim_worktree=False,
            now=1,
            lease_ms=1,
        )
    # The insert shared the same transaction as the failed start, so it never
    # survives either.
    assert owner.get_run("r-1") is None


def test_admit_inserts_claims_and_starts_in_one_transaction(owner) -> None:  # type: ignore[no-untyped-def]
    from nspawn.rows import RunPhase

    owner.admit_run(_row("r-1"), claim_worktree=True, now=5000, lease_ms=500)
    started = owner.get_run("r-1")
    assert started is not None
    assert started.phase is RunPhase.SPAWNING
    assert started.updated_at == 5000
    assert owner.read_claim(WT).run_id == "r-1"


def test_admit_refuses_a_run_that_is_not_pending(owner) -> None:  # type: ignore[no-untyped-def]
    from nspawn.registry import InvalidField
    from nspawn.rows import RunPhase

    with pytest.raises(InvalidField, match="pending"):
        owner.admit_run(
            _row("r-1", phase=RunPhase.RUNNING), claim_worktree=False, now=1, lease_ms=1
        )
    assert owner.get_run("r-1") is None


def test_a_held_worktree_leaves_no_run_behind(owner, rival) -> None:  # type: ignore[no-untyped-def]
    rival.claim_worktree(WT, "r-other", now=5000, lease_ms=5000)
    with pytest.raises(WorktreeHeld):
        owner.admit_run(_row("r-1"), claim_worktree=True, now=5000, lease_ms=500)
    # The insert and the claim shared one transaction, so the failed claim took
    # the run with it. A half-admitted run is the state this rules out.
    assert owner.get_run("r-1") is None


def test_update_moves_the_mutable_columns_and_never_the_observed_pair(owner) -> None:  # type: ignore[no-untyped-def]
    from nspawn.rows import RunPhase

    owner.insert_run(_row("r-1", phase=RunPhase.RUNNING))
    owner.note_record_observed("r-1", at_ms=777, count=2)
    owner.note_record_observed("r-1", at_ms=888)
    before = owner.get_run("r-1")
    assert before.last_record_at == 888
    assert before.records_seen == 3

    owner.update_run(_row("r-1", phase=RunPhase.IDLE, updated_at=9, tokens=42))
    after = owner.get_run("r-1")
    assert after.phase is RunPhase.IDLE
    assert after.tokens == 42
    # Ordinary progress cannot reset the counter that testifies about the
    # stretch before it.
    assert (after.last_record_at, after.records_seen) == (888, 3)


def test_update_cannot_move_an_immutable_column(owner) -> None:  # type: ignore[no-untyped-def]
    from nspawn.rows import RunPhase

    owner.insert_run(_row("r-1", phase=RunPhase.RUNNING))
    owner.update_run(
        _row(
            "r-1",
            phase=RunPhase.RUNNING,
            worktree="/tmp/wt-elsewhere",
            created_at=999,
        )
    )
    read = owner.get_run("r-1")
    assert read.worktree == WT
    assert read.created_at == 1


def test_a_closed_run_is_never_reopened_but_can_be_closed(owner) -> None:  # type: ignore[no-untyped-def]
    from nspawn.registry import ClosedRun
    from nspawn.rows import RunPhase

    owner.insert_run(_row("r-1", phase=RunPhase.RUNNING))
    owner.update_run(_row("r-1", phase=RunPhase.CLOSED, updated_at=10))
    # Closing again is allowed; anything else is refused.
    owner.update_run(_row("r-1", phase=RunPhase.CLOSED, updated_at=11))
    with pytest.raises(ClosedRun):
        owner.update_run(_row("r-1", phase=RunPhase.RUNNING, updated_at=12))
    assert owner.get_run("r-1").phase is RunPhase.CLOSED


def test_updating_a_run_that_is_not_there_is_a_typed_refusal(owner) -> None:  # type: ignore[no-untyped-def]
    from nspawn.registry import UnknownRun
    from nspawn.rows import RunPhase

    with pytest.raises(UnknownRun):
        owner.update_run(_row("r-ghost", phase=RunPhase.RUNNING))


def test_record_observation_refuses_a_non_positive_count(owner) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(RegistryError, match="must be positive"):
        owner.note_record_observed("r-1", at_ms=100, count=0)


def test_a_late_record_does_not_resurrect_a_closed_run(owner) -> None:  # type: ignore[no-untyped-def]
    from nspawn.rows import RunPhase

    owner.insert_run(_row("r-1", phase=RunPhase.RUNNING))
    owner.note_record_observed("r-1", at_ms=100)
    owner.update_run(_row("r-1", phase=RunPhase.CLOSED, updated_at=10))
    owner.note_record_observed("r-1", at_ms=999)
    observed = owner.get_run("r-1")
    assert observed.last_record_at == 100
    assert observed.records_seen == 1


def test_a_page_is_bounded_and_reports_where_the_next_one_starts(owner) -> None:  # type: ignore[no-untyped-def]
    from nspawn.rows import RunPhase

    for n in range(5):
        owner.insert_run(
            _row(f"r-{n}", phase=RunPhase.RUNNING, worktree=f"/tmp/wt-{n}")
        )
    # Equal `created_at`: the tie breaks on `run_id`, descending, like the rest.
    first = owner.list_runs_page(limit=2)
    assert [row.run_id for row in first.rows] == ["r-4", "r-3"]
    assert first.truncated
    assert first.next_after_run == "r-3"

    second = owner.list_runs_page(after_run=first.next_after_run, limit=2)
    assert [row.run_id for row in second.rows] == ["r-2", "r-1"]
    assert second.truncated

    last = owner.list_runs_page(after_run=second.next_after_run, limit=2)
    assert [row.run_id for row in last.rows] == ["r-0"]
    assert not last.truncated
    assert last.next_after_run is None


def test_pages_run_newest_first_and_the_cursor_follows_that_order(owner) -> None:  # type: ignore[no-untyped-def]
    """Creation time decides the page, not the uuid: a new run is on page one.

    The `run_id` order is deliberately the REVERSE of the time order here, so
    a page that still sorts by `run_id` puts the oldest run first.
    """
    from nspawn.rows import RunPhase

    for n, created in enumerate((50, 40, 40, 30, 10)):
        owner.insert_run(
            _row(
                f"r-{n}",
                phase=RunPhase.RUNNING,
                worktree=f"/tmp/wt-{n}",
                created_at=created,
            )
        )
    seen: list[str] = []
    page = owner.list_runs_page(limit=2)
    seen.extend(row.run_id for row in page.rows)
    while page.next_after_run is not None:
        page = owner.list_runs_page(after_run=page.next_after_run, limit=2)
        seen.extend(row.run_id for row in page.rows)
    assert seen == ["r-0", "r-2", "r-1", "r-3", "r-4"]


def test_a_cursor_naming_a_vanished_run_is_refused(owner) -> None:  # type: ignore[no-untyped-def]
    """The cursor is a run; if the reaper removed it, the page has no anchor."""
    from nspawn.registry import RegistryError

    with pytest.raises(RegistryError):
        owner.list_runs_page(after_run="r-gone", limit=2)


def test_naming_a_run_answers_that_run_and_never_reports_truncation(owner) -> None:  # type: ignore[no-untyped-def]
    from nspawn.rows import RunPhase

    for n in range(3):
        owner.insert_run(
            _row(f"r-{n}", phase=RunPhase.RUNNING, worktree=f"/tmp/wt-{n}")
        )
    page = owner.list_runs_page(run_id="r-1", limit=1)
    assert [row.run_id for row in page.rows] == ["r-1"]
    assert not page.truncated
    assert page.next_after_run is None
    assert owner.list_runs_page(run_id="r-absent").rows == ()


def test_the_page_limit_is_clamped_at_both_ends(owner) -> None:  # type: ignore[no-untyped-def]
    from nspawn.registry import MAX_RUN_PAGE
    from nspawn.rows import RunPhase

    for n in range(3):
        owner.insert_run(
            _row(f"r-{n}", phase=RunPhase.RUNNING, worktree=f"/tmp/wt-{n}")
        )
    assert len(owner.list_runs_page(limit=0).rows) == 1
    assert len(owner.list_runs_page(limit=-5).rows) == 1
    assert len(owner.list_runs_page(limit=MAX_RUN_PAGE * 10).rows) == 3


def test_pruning_drops_history_but_never_custody_or_a_claimed_run(owner) -> None:  # type: ignore[no-untyped-def]
    from nspawn.process_identity import ProcessIdentity
    from nspawn.rows import RunPhase

    owner.insert_run(
        _row("r-old", phase=RunPhase.CLOSED, updated_at=100, worktree="/tmp/a")
    )
    owner.insert_run(
        _row("r-new", phase=RunPhase.CLOSED, updated_at=9000, worktree="/tmp/b")
    )
    owner.insert_run(
        _row("r-live", phase=RunPhase.RUNNING, updated_at=100, worktree="/tmp/c")
    )
    owner.insert_run(
        _row(
            "r-custody",
            phase=RunPhase.CLOSED,
            updated_at=100,
            worktree="/tmp/d",
            process_identity=ProcessIdentity(pid=7, boot_id="b", start_ticks=1),
        )
    )
    owner.insert_run(
        _row("r-claimed", phase=RunPhase.CLOSED, updated_at=100, worktree=WT)
    )
    owner.claim_worktree(WT, "r-claimed", now=1, lease_ms=10_000)

    removed = owner.prune_closed_runs(now=1000, retain_for_ms=500, max_closed=100)
    assert removed == 1
    remaining = {row.run_id for row in owner.list_runs()}
    assert remaining == {"r-new", "r-live", "r-custody", "r-claimed"}


def test_pruning_by_cardinality_keeps_the_newest(owner) -> None:  # type: ignore[no-untyped-def]
    from nspawn.rows import RunPhase

    for n in range(5):
        owner.insert_run(
            _row(
                f"r-{n}",
                phase=RunPhase.CLOSED,
                updated_at=1000 + n,
                worktree=f"/tmp/{n}",
            )
        )
    assert owner.prune_closed_runs(now=0, retain_for_ms=0, max_closed=2) == 3
    assert {row.run_id for row in owner.list_runs()} == {"r-4", "r-3"}


def test_pruning_rolls_back_and_reraises_on_a_real_db_failure(owner) -> None:  # type: ignore[no-untyped-def]
    from nspawn.rows import RunPhase

    owner.insert_run(
        _row("r-old", phase=RunPhase.CLOSED, updated_at=100, worktree="/tmp/a")
    )
    sql(owner, "DROP TABLE claims")
    with pytest.raises(nott_database.OperationalError, match="no such table"):
        owner.prune_closed_runs(now=1000, retain_for_ms=500, max_closed=100)
    assert owner.get_run("r-old") is not None


def test_a_dead_instance_orphans_its_runs_and_this_one_is_protected(
    owner, rival
) -> None:  # type: ignore[no-untyped-def]
    from nspawn.rows import RunPhase

    owner.insert_run(_row("r-mine", phase=RunPhase.RUNNING, worktree="/tmp/mine"))
    owner.insert_run(
        _row(
            "r-theirs",
            phase=RunPhase.RUNNING,
            owner_instance="i-rival",
            worktree="/tmp/theirs",
        )
    )
    rival.heartbeat_instance(1000)

    changed = owner.mark_orphans(now=100_000, stale_after_ms=1_000)
    assert changed == 1
    assert owner.get_run("r-theirs").phase is RunPhase.ORPHANED
    assert "heartbeat expired" in owner.get_run("r-theirs").blocked_reason
    # A supervisor never orphans its own runs by its own reaper pass.
    assert owner.get_run("r-mine").phase is RunPhase.RUNNING


def test_orphaning_a_run_with_parked_approvals_says_they_were_lost(
    owner, rival
) -> None:  # type: ignore[no-untyped-def]
    from nspawn.rows import RunPhase

    owner.insert_run(
        _row(
            "r-parked",
            phase=RunPhase.WAITING_APPROVAL,
            owner_instance="i-rival",
            pending_approvals=2,
            worktree="/tmp/parked",
        )
    )
    rival.heartbeat_instance(1000)
    owner.mark_orphans(now=100_000, stale_after_ms=1_000)
    orphaned = owner.get_run("r-parked")
    assert "parked approvals were lost" in orphaned.blocked_reason
    assert orphaned.pending_approvals == 0


def test_an_orphan_holding_custody_has_its_claim_poisoned(owner, rival) -> None:  # type: ignore[no-untyped-def]
    from nspawn.process_identity import ProcessIdentity
    from nspawn.rows import RunPhase

    owner.insert_run(
        _row(
            "r-custody",
            phase=RunPhase.RUNNING,
            owner_instance="i-rival",
            process_identity=ProcessIdentity(pid=7, boot_id="b", start_ticks=1),
        )
    )
    rival.claim_worktree(WT, "r-custody", now=1, lease_ms=1)
    rival.heartbeat_instance(1000)
    owner.mark_orphans(now=100_000, stale_after_ms=1_000)
    assert owner.read_claim(WT).lease_until == POISONED_LEASE_UNTIL


def test_mark_orphans_rolls_back_and_reraises_on_a_real_db_failure(  # type: ignore[no-untyped-def]
    owner, rival
) -> None:
    from nspawn.rows import RunPhase

    owner.insert_run(
        _row(
            "r-theirs",
            phase=RunPhase.RUNNING,
            owner_instance="i-rival",
            worktree="/tmp/theirs",
        )
    )
    rival.heartbeat_instance(1000)
    sql(owner, "DROP TABLE instances")
    with pytest.raises(nott_database.OperationalError, match="no such table"):
        owner.mark_orphans(now=100_000, stale_after_ms=1_000)
    assert owner.get_run("r-theirs").phase is RunPhase.RUNNING


def test_closing_an_orphan_requires_the_exact_identity(owner, rival) -> None:  # type: ignore[no-untyped-def]
    from nspawn.process_identity import ProcessIdentity
    from nspawn.rows import RunPhase

    identity = ProcessIdentity(pid=7, boot_id="boot-1", start_ticks=99)
    owner.insert_run(
        _row(
            "r-1", phase=RunPhase.ORPHANED, process_identity=identity, worktree="/tmp/o"
        )
    )
    # A pid alone is not an identity: the kernel may have handed it to somebody else.
    assert not owner.close_orphaned(
        "r-1", ProcessIdentity(pid=7, boot_id="boot-2", start_ticks=99), now=5
    )
    assert not owner.close_orphaned(
        "r-1", ProcessIdentity(pid=7, boot_id="boot-1", start_ticks=100), now=5
    )
    assert owner.close_orphaned("r-1", identity, now=5)
    closed = owner.get_run("r-1")
    assert closed.phase is RunPhase.CLOSED
    assert closed.process_identity is None
    assert not owner.close_orphaned("r-1", identity, now=6)


def test_an_orphan_of_an_earlier_boot_closes_whatever_its_legacy_group(  # type: ignore[no-untyped-def]
    owner, rival
) -> None:
    """No process of an earlier boot can live, so the legacy `process_group` column does
    not fence the close of its run. A run of this boot stays fenced by it."""
    import os

    from nspawn.process_identity import ProcessIdentity, capture_process_identity
    from nspawn.rows import RunPhase

    earlier = ProcessIdentity(pid=7, boot_id="an-earlier-boot", start_ticks=99)
    now_boot = capture_process_identity(os.getpid()).boot_id
    this_boot = ProcessIdentity(pid=7, boot_id=now_boot, start_ticks=1)
    for run_id, identity in (("r-earlier", earlier), ("r-this", this_boot)):
        owner.insert_run(
            _row(
                run_id,
                phase=RunPhase.ORPHANED,
                process_identity=identity,
                worktree=f"/tmp/{run_id}",
            )
        )
        sql(owner, f"UPDATE runs SET process_group = 7 WHERE run_id = '{run_id}'")
    assert owner.close_orphaned("r-earlier", earlier, now=5)
    assert owner.get_run("r-earlier").phase is RunPhase.CLOSED
    assert not owner.close_orphaned("r-this", this_boot, now=5)
    assert owner.get_run("r-this").phase is RunPhase.ORPHANED


def test_close_owned_is_scoped_to_this_instance_and_to_a_live_run(owner, rival) -> None:  # type: ignore[no-untyped-def]
    from nspawn.process_identity import ProcessIdentity, capture_process_identity
    from nspawn.rows import RunPhase

    # Live custody: neither peer nor owner can close while process is live.
    live_identity = capture_process_identity(os.getpid())
    owner.insert_run(
        _row(
            "r-live",
            phase=RunPhase.RUNNING,
            process_identity=live_identity,
            worktree="/tmp/live",
        )
    )
    owner.claim_worktree("/tmp/live", "r-live", now=1, lease_ms=10_000)
    assert not rival.close_owned("r-live", live_identity, now=5)
    assert not owner.close_owned("r-live", live_identity, now=5)
    assert owner.read_claim("/tmp/live") is not None

    # Proven-dead orphan: any supervisor can close it and release its claim.
    identity = ProcessIdentity(pid=7, boot_id="boot-1", start_ticks=99)
    owner.insert_run(
        _row("r-1", phase=RunPhase.RUNNING, process_identity=identity, worktree=WT)
    )
    owner.claim_worktree(WT, "r-1", now=1, lease_ms=10_000)
    assert rival.close_owned("r-1", identity, now=5)
    assert owner.get_run("r-1").phase is RunPhase.CLOSED
    # The claim goes with the close.
    assert owner.read_claim(WT) is None
    assert not owner.close_owned("r-1", identity, now=6)


def test_close_owned_rolls_back_and_reraises_on_a_real_db_failure(owner) -> None:  # type: ignore[no-untyped-def]
    from nspawn.process_identity import ProcessIdentity
    from nspawn.rows import RunPhase

    identity = ProcessIdentity(pid=7, boot_id="boot-1", start_ticks=99)
    owner.insert_run(
        _row("r-1", phase=RunPhase.RUNNING, process_identity=identity, worktree=WT)
    )
    owner.claim_worktree(WT, "r-1", now=1, lease_ms=10_000)
    # The UPDATE that closes the run succeeds; the DELETE right after it, in the
    # same transaction, cannot — the table it targets is gone.
    sql(owner, "DROP TABLE claims")
    with pytest.raises(nott_database.OperationalError, match="no such table"):
        owner.close_owned("r-1", identity, now=5)
    run = owner.get_run("r-1")
    assert run.phase is RunPhase.RUNNING
    assert run.process_identity == identity


# -- connection lifecycle ----------------------------------------------------


def test_connect_is_idempotent_and_close_is_safe_before_or_twice(owner) -> None:  # type: ignore[no-untyped-def]
    # Reconnecting the already-connected fixture instance changes nothing;
    # proven by an ordinary statement still working through the same handle.
    same = owner.connect()
    assert same is owner
    assert owner.heartbeat_instance(1)

    # A scratch instance the fixture does not manage, so closing it here does
    # not race the fixture's own teardown.
    scratch = Registry(_url(), "i-scratch")
    scratch.close()  # no-op: nothing to close yet
    scratch.connect()
    # Proves the path argument is the file actually opened.
    (found,) = sql(scratch, "SELECT file FROM pragma_database_list WHERE name = 'main'")
    # Resolved on both sides: macOS reports /var/tmp as /private/var/tmp.
    assert os.path.realpath(found["file"]) == os.path.realpath(_url())
    scratch.close()
    scratch.close()  # no-op: already closed


def test_registry_is_a_context_manager(owner) -> None:  # type: ignore[no-untyped-def]
    with Registry(_url(), "i-ctx") as registry:
        registry.open(now=1)
        assert registry.heartbeat_instance(2)
    with pytest.raises(RegistryError, match="not connected"):
        registry.heartbeat_instance(3)


# -- provider and account cooldown walls ------------------------------------


def test_provider_wall_round_trips_and_expires_at_its_own_instant(owner) -> None:  # type: ignore[no-untyped-def]
    assert owner.get_provider_walls(now=1000) == {}
    owner.record_provider_wall(
        "anthropic", cooling_until=2000, source="vendor_reset", now=1000
    )
    assert owner.get_provider_walls(now=1999) == {"anthropic": 2000}
    # Exclusive boundary, same rule a lease uses: a wall expires AT its instant.
    assert owner.get_provider_walls(now=2000) == {}


def test_account_wall_round_trips_and_carries_limit_type(owner) -> None:  # type: ignore[no-untyped-def]
    assert owner.get_account_walls(now=1000) == {}
    owner.record_account_wall(
        "acct-1",
        cooling_until=5000,
        source="default_1h",
        limit_type="rate_limit",
        now=1000,
    )
    assert owner.get_account_walls(now=4999) == {"acct-1": 5000}
    assert owner.get_account_walls(now=5000) == {}


def test_recording_a_wall_twice_overwrites_rather_than_accumulates(owner) -> None:  # type: ignore[no-untyped-def]
    owner.record_provider_wall(
        "anthropic", cooling_until=2000, source="vendor_reset", now=1000
    )
    owner.record_provider_wall(
        "anthropic", cooling_until=9000, source="default_1h", now=1500
    )
    assert owner.get_provider_walls(now=8999) == {"anthropic": 9000}
    (found,) = sql(
        owner,
        "SELECT source, observed_at FROM provider_walls WHERE provider = 'anthropic'",
    )
    assert found["source"] == "default_1h"
    assert found["observed_at"] == 1500


def test_provider_and_account_walls_are_independent_namespaces(owner) -> None:  # type: ignore[no-untyped-def]
    owner.record_provider_wall(
        "shared-name", cooling_until=2000, source="vendor_reset", now=1000
    )
    owner.record_account_wall(
        "shared-name",
        cooling_until=3000,
        source="default_1h",
        limit_type="rate_limit",
        now=1000,
    )
    assert owner.get_provider_walls(now=1500) == {"shared-name": 2000}
    assert owner.get_account_walls(now=1500) == {"shared-name": 3000}


def test_walls_written_by_one_instance_are_visible_to_another(owner, rival) -> None:  # type: ignore[no-untyped-def]
    owner.record_provider_wall(
        "anthropic", cooling_until=2000, source="vendor_reset", now=1000
    )
    assert rival.get_provider_walls(now=1500) == {"anthropic": 2000}


def test_wall_identifiers_are_refused_before_they_reach_the_database() -> None:
    # None of these touch the connection: an unconnected Registry proves the
    # refusal happens before any statement is sent. Exact message equality,
    # not a substring match: a substring survives a mutation that only wraps
    # the field name in extra characters instead of naming a different field.
    registry = Registry(_unopened(), "i-x")
    with pytest.raises(RegistryError) as provider_missing:
        registry.record_provider_wall("", 100, "vendor_reset", now=1)
    assert (
        str(provider_missing.value)
        == "provider_walls.provider must be between 1 and 512 bytes, got 0"
    )
    with pytest.raises(RegistryError) as alias_missing:
        registry.record_account_wall("", 100, "vendor_reset", "rate_limit", now=1)
    assert (
        str(alias_missing.value)
        == "account_walls.account_alias must be between 1 and 512 bytes, got 0"
    )
    with pytest.raises(RegistryError) as limit_type_missing:
        registry.record_account_wall("acct-1", 100, "vendor_reset", "", now=1)
    assert (
        str(limit_type_missing.value)
        == "account_walls.limit_type must be between 1 and 512 bytes, got 0"
    )


@given(st.text().filter(lambda s: s not in WALL_SOURCE_VALUES))
def test_an_unknown_wall_source_is_always_refused(source: str) -> None:
    registry = Registry(_unopened(), "i-x")
    with pytest.raises(RegistryError, match="walls.source"):
        registry.record_provider_wall("p", 100, source, now=1)


@given(st.sampled_from(WALL_SOURCE_VALUES))
def test_every_closed_vocabulary_source_passes_validation(source: str) -> None:
    # Reaches the connection property itself, proving validation let it through;
    # no live connection is open, so the failure mode flips from RegistryError
    # (source rejected) to "not connected" (source accepted, DB unreachable).
    registry = Registry(_unopened(), "i-x")
    with pytest.raises(RegistryError, match="not connected"):
        registry.record_provider_wall("p", 100, source, now=1)


@given(st.integers(max_value=-1))
def test_a_negative_cooling_until_is_always_refused(cooling_until: int) -> None:
    registry = Registry(_unopened(), "i-x")
    with pytest.raises(RegistryError, match="cooling_until"):
        registry.record_provider_wall("p", cooling_until, "vendor_reset", now=1)


@given(st.integers(max_value=-1))
def test_a_negative_observed_at_is_always_refused(now: int) -> None:
    registry = Registry(_unopened(), "i-x")
    with pytest.raises(RegistryError, match="observed_at"):
        registry.record_provider_wall("p", 100, "vendor_reset", now=now)


@pytest.mark.parametrize(
    "bad_time", [cast(int, True), cast(int, False), cast(int, 1.5)]
)
def test_a_bool_or_float_wall_time_is_always_refused(bad_time: int) -> None:
    # `True < 0` and `1.5 < 0` are both False, so a size-only comparison lets
    # a bool or a float reach the SQL layer as a wall time.
    registry = Registry(_unopened(), "i-x")
    with pytest.raises(RegistryError, match="cooling_until"):
        registry.record_provider_wall("p", bad_time, "vendor_reset", now=1)


def test_an_unknown_wall_source_refusal_never_echoes_the_value() -> None:
    registry = Registry(_unopened(), "i-x")
    sentinel = "not-a-real-source-sentinel"
    with pytest.raises(RegistryError, match="walls.source") as excinfo:
        registry.record_provider_wall("p", 100, sentinel, now=1)
    assert sentinel not in str(excinfo.value)


def test_concurrent_wall_writes_to_the_same_provider_never_crash_or_corrupt(  # type: ignore[no-untyped-def]
    owner, rival
) -> None:
    errors: list[BaseException] = []

    def write(registry, cooling_until: int, source: str) -> None:  # type: ignore[no-untyped-def]
        try:
            for _ in range(20):
                registry.record_provider_wall(
                    "contended", cooling_until=cooling_until, source=source, now=1
                )
        except BaseException as exc:  # noqa: BLE001 - the outcome is the assertion
            errors.append(exc)

    first = threading.Thread(target=write, args=(owner, 5000, "vendor_reset"))
    second = threading.Thread(target=write, args=(rival, 6000, "default_1h"))
    first.start()
    second.start()
    first.join()
    second.join()

    assert errors == []
    # ON CONFLICT upserts are atomic per statement: the row exists exactly once,
    # holding whichever write landed last, never a torn mix of the two.
    assert owner.get_provider_walls(now=0)["contended"] in (5000, 6000)


# -- account usage -------------------------------------------------------------


def _usage(
    alias: str, window: str, utilization: float, resets_at: int, observed_at: int
):  # type: ignore[no-untyped-def]
    return AccountUsageRow(
        account_alias=alias,
        usage_window=window,
        utilization=utilization,
        resets_at=resets_at,
        observed_at=observed_at,
    )


def test_both_windows_of_one_observation_land_together(owner) -> None:  # type: ignore[no-untyped-def]
    owner.record_account_usage(
        (
            _usage("guara-owner-invalid", "five_hour", 0.16, 1787896800, 1787890000),
            _usage("guara-owner-invalid", "seven_day", 0.82, 1788130800, 1787890000),
        )
    )
    stored = owner.get_account_usage()
    assert [(r.usage_window, r.utilization) for r in stored] == [
        ("five_hour", 0.16),
        ("seven_day", 0.82),
    ]


def test_a_second_observation_upserts_by_account_AND_window(owner) -> None:  # type: ignore[no-untyped-def]
    """The PK is the pair. Keyed on the alias alone the second window would
    overwrite the first, and an account would only ever report one."""
    owner.record_account_usage(
        (
            _usage("guara-owner-invalid", "five_hour", 0.16, 1787896800, 1787890000),
            _usage("guara-owner-invalid", "seven_day", 0.82, 1788130800, 1787890000),
        )
    )
    owner.record_account_usage(
        (_usage("guara-owner-invalid", "five_hour", 0.31, 1787900000, 1787891000),)
    )
    stored = {r.usage_window: r for r in owner.get_account_usage()}
    assert len(stored) == 2
    assert stored["five_hour"].utilization == 0.31
    assert stored["five_hour"].observed_at == 1787891000
    assert stored["seven_day"].utilization == 0.82


def test_a_slot_named_by_its_span_replaces_the_same_slot_read_without_one(
    owner,
) -> None:  # type: ignore[no-untyped-def]
    """`codex.primary.seven_day` is the `codex.primary` bucket: a reading that
    learned the window's length must not leave the older row of that slot behind,
    where the account's state would read it as stale forever."""
    owner.record_account_usage(
        (_usage("codex-main", "codex.primary", 0.80, 1791587462, 1791290766),)
    )
    owner.record_account_usage(
        (_usage("codex-main", "codex.primary.seven_day", 0.81, 1791587462, 1791294974),)
    )
    assert [(r.usage_window, r.utilization) for r in owner.get_account_usage()] == [
        ("codex.primary.seven_day", 0.81)
    ]


def test_two_accounts_keep_independent_usage(owner) -> None:  # type: ignore[no-untyped-def]
    owner.record_account_usage(
        (_usage("guara-owner-invalid", "seven_day", 0.25, 1788130800, 1787890000),)
    )
    owner.record_account_usage(
        (_usage("tiago-menot-invalid", "seven_day", 0.85, 1788130800, 1787890000),)
    )
    stored = {r.account_alias: r.utilization for r in owner.get_account_usage()}
    assert stored == {"guara-owner-invalid": 0.25, "tiago-menot-invalid": 0.85}


# -- run events ----------------------------------------------------------------


def test_the_chain_hash_matches_the_pinned_vector() -> None:
    import hashlib

    from nspawn.registry import compute_chained_hash

    expected = hashlib.sha256(b"prev\nline").hexdigest()
    assert compute_chained_hash("prev", "line") == expected


def test_an_appended_event_reads_back_identically(owner) -> None:  # type: ignore[no-untyped-def]
    _insert_run(owner, "r-ev-1")

    event = owner.append_run_event(
        "r-ev-1", at=1, kind="usage", payload={"cost": "1.5"}
    )

    assert event.seq == 1
    stored = owner.read_run_events("r-ev-1")
    assert [(e.seq, e.kind, e.payload) for e in stored] == [
        (1, "usage", {"cost": "1.5"})
    ]
    assert stored[0].chained_hash == event.chained_hash


def test_seq_is_monotonic_per_run_and_each_run_starts_at_one(owner) -> None:  # type: ignore[no-untyped-def]
    _insert_run(owner, "r-ev-a")
    _insert_run(owner, "r-ev-b")

    for index in range(3):
        owner.append_run_event("r-ev-a", at=index, kind="usage", payload={"n": index})
    first_of_b = owner.append_run_event("r-ev-b", at=0, kind="usage", payload={})

    assert [e.seq for e in owner.read_run_events("r-ev-a")] == [1, 2, 3]
    assert first_of_b.seq == 1


def test_the_chain_verifies_and_a_tampered_middle_row_breaks_it(owner) -> None:  # type: ignore[no-untyped-def]
    _insert_run(owner, "r-ev-chain")
    for index in range(3):
        owner.append_run_event(
            "r-ev-chain", at=index, kind="usage", payload={"n": index}
        )
    assert owner.verify_run_event_chain("r-ev-chain") is True

    sql(owner, "DROP TRIGGER run_events_append_only_update")
    sql(
        owner,
        "UPDATE run_events SET payload = ? WHERE run_id = ? AND seq = 2",
        ('{"n": 99}', "r-ev-chain"),
    )

    assert owner.verify_run_event_chain("r-ev-chain") is False


def test_an_invalid_row_is_refused_as_typed_and_before_the_database(owner) -> None:  # type: ignore[no-untyped-def]
    _insert_run(owner, "r-typed")
    for at, kind in ((0, "telemetry"), (1, "nao-e-um-kind"), (-1, "usage")):
        with pytest.raises(RegistryError):
            owner.append_run_event("r-typed", at=at, kind=kind, payload={})
    assert owner.read_run_events("r-typed") == []


def test_a_window_whose_reset_has_passed_is_still_returned(owner) -> None:  # type: ignore[no-untyped-def]
    """Filtering here would reach the reader as "never observed" — a different
    claim, and a false one. Expiry is the reader's judgement, not the store's."""
    owner.record_account_usage(
        (_usage("guara-owner-invalid", "five_hour", 0.94, 1, 1),)
    )
    stored = owner.get_account_usage()
    assert len(stored) == 1
    assert stored[0].resets_at == 1
    assert stored[0].utilization == 0.94


def test_recording_no_windows_writes_nothing(owner) -> None:  # type: ignore[no-untyped-def]
    owner.record_account_usage(())
    assert owner.get_account_usage() == ()


def test_a_utilization_above_one_is_stored_because_overage_is_legal(owner) -> None:  # type: ignore[no-untyped-def]
    owner.record_account_usage(
        (_usage("guara-owner-invalid", "seven_day", 1.4, 1788130800, 1787890000),)
    )
    assert owner.get_account_usage()[0].utilization == 1.4


# -- wall rows carry every persisted column ------------------------------------


def test_the_account_wall_rows_carry_source_limit_type_and_observed_at(owner) -> None:  # type: ignore[no-untyped-def]
    """source and limit_type have been written since the walls were born and
    read by nobody. A report that tells an operator WHY needs them."""
    owner.record_account_wall(
        "guara-owner-invalid", 900, "vendor_reset", "five_hour", now=100
    )
    rows = owner.get_account_wall_rows(now=100)
    assert len(rows) == 1
    assert rows[0].account_alias == "guara-owner-invalid"
    assert rows[0].cooling_until == 900
    assert rows[0].source == "vendor_reset"
    assert rows[0].limit_type == "five_hour"
    assert rows[0].observed_at == 100


def test_the_provider_wall_rows_carry_source_and_observed_at(owner) -> None:  # type: ignore[no-untyped-def]
    owner.record_provider_wall("agy", 400, "default_1h", now=100)
    rows = owner.get_provider_wall_rows(now=100)
    assert len(rows) == 1
    assert rows[0].provider == "agy"
    assert rows[0].source == "default_1h"
    assert rows[0].observed_at == 100


def test_the_dict_getters_still_answer_exactly_the_shape_the_picker_reads(
    owner,
) -> None:  # type: ignore[no-untyped-def]
    """The dicts are now derived from the row getters. The picker contracts on
    dict[str, int] and twenty call sites read it; drift here breaks routing."""
    owner.record_provider_wall("agy", 400, "default_1h", now=100)
    owner.record_account_wall(
        "guara-owner-invalid", 900, "vendor_reset", "five_hour", now=100
    )
    assert owner.get_provider_walls(now=100) == {"agy": 400}
    assert owner.get_account_walls(now=100) == {"guara-owner-invalid": 900}


def test_the_row_getters_honour_the_same_exclusive_boundary(owner) -> None:  # type: ignore[no-untyped-def]
    """A wall expires AT its own instant, not after it — the same boundary the
    dict getters and the lease use."""
    owner.record_account_wall(
        "guara-owner-invalid", 900, "vendor_reset", "five_hour", now=100
    )
    assert owner.get_account_wall_rows(now=899) != ()
    assert owner.get_account_wall_rows(now=900) == ()
    assert owner.get_account_walls(now=900) == {}


# -- how many of nSPAWN's own runs are alive on each account ----------------


def test_account_load_counts_one_live_run_per_account(owner) -> None:  # type: ignore[no-untyped-def]
    """The only load signal every route shares: nSPAWN's OWN live runs.

    A terminal run is not load — it occupies nothing. Counting it would hold a
    finished account off the picker forever, which is the whole point of
    fencing on `rows.TERMINAL_PHASES` instead of a second hand-written list.
    """
    _insert_run(owner, "r-a1", phase="running", executed_account="guara-owner-invalid")
    _insert_run(
        owner, "r-a2", phase="waiting_approval", executed_account="guara-owner-invalid"
    )
    _insert_run(owner, "r-b1", phase="pending", executed_account="fugumax")
    for terminal in ("completed", "failed", "closed", "orphaned"):
        _insert_run(
            owner,
            f"r-done-{terminal}",
            phase=terminal,
            executed_account="guara-owner-invalid",
        )
    assert owner.get_account_load() == {"guara-owner-invalid": 2, "fugumax": 1}


def test_account_load_omits_an_account_with_no_live_run(owner) -> None:  # type: ignore[no-untyped-def]
    """A missing key IS zero. The absent account must not arrive as 0, or the
    picker cannot tell "idle" from "never dispatched to"."""
    _insert_run(
        owner, "r-gone", phase="completed", executed_account="guara-owner-invalid"
    )
    assert owner.get_account_load() == {}


def test_account_load_drops_runs_attributable_to_no_account(owner) -> None:  # type: ignore[no-untyped-def]
    """`executed_account` is nullable: a run that never reached a vendor has no
    account to be load ON. A None or "" key would be a phantom account the
    picker would then try to route to."""
    _insert_run(owner, "r-null", phase="running")
    _insert_run(owner, "r-named", phase="running", executed_account="fugumax")
    load = owner.get_account_load()
    assert load == {"fugumax": 1}
    assert None not in load
    assert "" not in load


def test_account_load_refuses_a_registry_that_never_connected() -> None:
    """Same refusal as the wall readers: no store, no answer. An empty dict
    here reads as "every account idle" and the picker would pile onto one."""
    with pytest.raises(RegistryError, match="not connected"):
        Registry(_unopened(), "i-x").get_account_load()


# -- F-012: the claim half comes from the claims DELETE, never from another ---


def test_a_close_reports_the_claim_half_from_the_claims_delete(owner) -> None:  # type: ignore[no-untyped-def]
    from nspawn.process_identity import ProcessIdentity
    from nspawn.rows import RunPhase

    identity = ProcessIdentity(pid=7, boot_id="boot-1", start_ticks=99)
    owner.insert_run(
        _row("r-1", phase=RunPhase.RUNNING, process_identity=identity, worktree=WT)
    )
    owner.claim_worktree(WT, "r-1", now=1, lease_ms=10_000)
    outcome = owner.close_owned_outcome("r-1", identity, now=5)
    assert outcome == CloseOutcome(run_closed=True, claim_released=True)
    # The table, not the return value.
    assert owner.read_claim(WT) is None


def test_a_close_that_released_no_claim_says_so(owner) -> None:  # type: ignore[no-untyped-def]
    """The run closed and NO claim went, because there was none to go.

    Sourcing this from the run statement's rowcount answers True here, which is
    exactly the sentence eight closed runs got while their claims stayed put.
    """
    from nspawn.process_identity import ProcessIdentity
    from nspawn.rows import RunPhase

    identity = ProcessIdentity(pid=7, boot_id="boot-1", start_ticks=99)
    owner.insert_run(
        _row("r-1", phase=RunPhase.RUNNING, process_identity=identity, worktree=WT)
    )
    outcome = owner.close_owned_outcome("r-1", identity, now=5)
    assert outcome == CloseOutcome(run_closed=True, claim_released=False)
    assert owner.get_run("r-1").phase is RunPhase.CLOSED


def test_a_refused_close_releases_nothing_and_says_so(owner, rival) -> None:  # type: ignore[no-untyped-def]
    from nspawn.process_identity import capture_process_identity
    from nspawn.rows import RunPhase

    # Live custody: process is alive, so close is refused and releases nothing.
    identity = capture_process_identity(os.getpid())
    owner.insert_run(
        _row("r-1", phase=RunPhase.RUNNING, process_identity=identity, worktree=WT)
    )
    owner.claim_worktree(WT, "r-1", now=1, lease_ms=10_000)
    outcome = rival.close_owned_outcome("r-1", identity, now=5)
    assert outcome == CloseOutcome(run_closed=False, claim_released=False)
    held = owner.read_claim(WT)
    assert held is not None and held.holder == "i-owner"


def test_a_dead_holders_claim_is_releasable_by_anyone(owner, rival) -> None:  # type: ignore[no-untyped-def]
    """A supervisor that died must not fence a worktree until a human acts.

    That fence is `poison_claim`'s job. An ordinary death leaves a holder that is
    no longer in `instances`, and the worktree behind it is free.
    """
    owner.claim_worktree(WT, "r-1", now=1000, lease_ms=10_000)
    owner.remove_instance()
    assert rival.release_claim(WT, "r-1") is True
    assert rival.read_claim(WT) is None


def test_a_live_foreign_holders_claim_stays_protected(owner, rival) -> None:  # type: ignore[no-untyped-def]
    owner.claim_worktree(WT, "r-1", now=1000, lease_ms=10_000)
    assert rival.release_claim(WT, "r-1") is False
    held = rival.read_claim(WT)
    assert held is not None and held.holder == "i-owner"


def test_a_dead_holders_claim_is_kept_while_its_run_holds_custody(owner, rival) -> None:  # type: ignore[no-untyped-def]
    """Dead holder, live process. Releasing here hands the worktree to a second
    writer while the first one is still running in it — the exact race the claim
    exists to prevent."""
    from nspawn.process_identity import ProcessIdentity
    from nspawn.rows import RunPhase

    owner.insert_run(
        _row(
            "r-1",
            phase=RunPhase.RUNNING,
            process_identity=ProcessIdentity(pid=7, boot_id="boot-1", start_ticks=99),
            worktree=WT,
        )
    )
    owner.claim_worktree(WT, "r-1", now=1000, lease_ms=10_000)
    owner.remove_instance()
    assert rival.release_claim(WT, "r-1") is False
    assert rival.read_claim(WT) is not None


def test_a_poisoned_claim_survives_its_holders_death(owner, rival) -> None:  # type: ignore[no-untyped-def]
    """Poison says a human must look. The holder's absence is not that human."""
    owner.claim_worktree(WT, "r-1", now=1000, lease_ms=10_000)
    owner.poison_claim(WT, "r-1")
    owner.remove_instance()
    assert rival.release_claim(WT, "r-1") is False
    held = rival.read_claim(WT)
    assert held is not None and held.lease_until == POISONED_LEASE_UNTIL


def test_the_holder_releases_its_own_claim_whatever_state_it_is_in(owner) -> None:  # type: ignore[no-untyped-def]
    """The owner arm is unconditional, as it always was: the instance that took
    the worktree may give it back without asking custody or poison."""
    owner.claim_worktree(WT, "r-1", now=1000, lease_ms=10_000)
    owner.poison_claim(WT, "r-1")
    assert owner.release_claim(WT, "r-1") is True
    assert owner.read_claim(WT) is None


def test_releasing_a_claim_that_is_not_there_reports_false(owner) -> None:  # type: ignore[no-untyped-def]
    assert owner.release_claim(WT, "r-nothing") is False


# -- statement-level churn -----------------------------------------------------


class _Recording:
    """Delegates to a registry's connection and records each statement's rowcount."""

    def __init__(self, connection) -> None:  # type: ignore[no-untyped-def]
        self.inner = connection
        self.statements: list[tuple[str, int]] = []

    def execute(self, sql, *args, **kwargs):  # type: ignore[no-untyped-def]
        cursor = self.inner.execute(sql, *args, **kwargs)
        self.statements.append((" ".join(str(sql).split()), cursor.rowcount))
        return cursor

    def __getattr__(self, name):  # type: ignore[no-untyped-def]
        return getattr(self.inner, name)


def _recorded(registry, call):  # type: ignore[no-untyped-def]
    recording = _Recording(registry._connection)
    registry._connection = recording
    try:
        call()
    finally:
        registry._connection = recording.inner
    return recording.statements


def test_mark_orphans_does_not_rewrite_a_claim_that_is_already_poisoned(
    owner, rival
) -> None:  # type: ignore[no-untyped-def]
    """54 M no-op updates and 165 MB of bloat: every sweep re-poisoned every
    poisoned claim. A claim already at the poisoned lease is left alone."""
    from nspawn.process_identity import ProcessIdentity
    from nspawn.rows import RunPhase

    owner.insert_run(
        _row(
            "r-custody",
            phase=RunPhase.RUNNING,
            owner_instance="i-rival",
            process_identity=ProcessIdentity(pid=7, boot_id="b", start_ticks=1),
        )
    )
    rival.claim_worktree(WT, "r-custody", now=1, lease_ms=1)
    rival.heartbeat_instance(1000)
    owner.mark_orphans(now=100_000, stale_after_ms=1_000)
    assert owner.read_claim(WT).lease_until == POISONED_LEASE_UNTIL

    statements = _recorded(
        owner, lambda: owner.mark_orphans(now=200_000, stale_after_ms=1_000)
    )
    poisons = [
        count
        for sql, count in statements
        if sql.startswith("UPDATE") and "claims SET lease_until" in sql
    ]
    assert poisons == [0]
    assert owner.read_claim(WT).lease_until == POISONED_LEASE_UNTIL


def test_claim_close_with_nothing_pending_issues_no_update(owner) -> None:  # type: ignore[no-untyped-def]
    """Every driver polls `claim_close` each second: with no pending request it
    only reads, so the poll never takes the write lock."""
    owner.insert_run(_row("r-idle", worktree="/tmp/idle"))
    answers: list[str] = []
    statements = _recorded(
        owner,
        lambda: answers.append(owner.claim_close("r-idle", 1_500, 1_000, 30.0)),
    )
    assert answers == ["none"]
    assert not [sql for sql, _ in statements if sql.startswith("UPDATE")]

    owner.request_close("r-idle")
    assert owner.claim_close("r-idle", 1_500, 1_000, 30.0) == "won"
    assert owner.claim_close("r-idle", 1_500, 1_000, 30.0) == "held"


@pytest.mark.parametrize(
    ("name", "bucket"),
    [
        ("codex.primary.seven_day", "codex.primary"),
        ("primary.five_hour", "codex.primary"),
        ("review.primary.seven_day", "review.primary"),
        ("secondary_window", "codex.secondary"),
        ("seven_day", "seven_day"),
        ("seven_day.five_hour", "seven_day.five_hour"),
        ("review.seven_day", "review.seven_day"),
    ],
)
def test_only_a_slot_sheds_the_length_it_declared(name: str, bucket: str) -> None:
    """The length qualifies a Codex slot (`<limit>.<slot>.<length>`) and is that
    slot; any other name keeps its own bucket, so it never takes another's row."""
    assert registry_module._window_kind(name) == bucket


@pytest.mark.parametrize("ended", ["completed", "failed"])
def test_an_ended_run_is_never_rewritten_but_can_be_closed(owner, ended) -> None:  # type: ignore[no-untyped-def]
    """Review round 2: a cleanup failure after COMPLETED published a synthetic
    FAILED over it and zeroed its tokens. The first terminal phase wins; only
    a close moves it on."""
    from nspawn.registry import ClosedRun
    from nspawn.rows import RunPhase

    owner.insert_run(_row("r-1", phase=RunPhase.RUNNING))
    owner.update_run(_row("r-1", phase=RunPhase(ended), updated_at=10, tokens=42))
    for later in (RunPhase.FAILED, RunPhase.COMPLETED, RunPhase.RUNNING):
        with pytest.raises(ClosedRun):
            owner.update_run(_row("r-1", phase=later, updated_at=11, tokens=0))
    read = owner.get_run("r-1")
    assert (read.phase, read.tokens) == (RunPhase(ended), 42)
    # An annotation keeps the phase; it never moves it.
    other = RunPhase.FAILED if ended == "completed" else RunPhase.COMPLETED
    with pytest.raises(ClosedRun):
        owner.update_run(_row("r-1", phase=other, updated_at=11), annotate=True)
    owner.update_run(
        _row("r-1", phase=RunPhase(ended), updated_at=11, tokens=42, warning="w"),
        annotate=True,
    )
    assert owner.get_run("r-1").warning == "w"
    owner.update_run(_row("r-1", phase=RunPhase.CLOSED, updated_at=12, tokens=42))
    assert owner.get_run("r-1").phase is RunPhase.CLOSED


def test_an_annotation_never_moves_a_live_run(owner) -> None:  # type: ignore[no-untyped-def]
    """Review round 3: `annotate` on a RUNNING row moved it to FAILED with only
    a warning. An annotation writes a completed or failed row's own phase."""
    from nspawn.registry import ClosedRun
    from nspawn.rows import RunPhase

    owner.insert_run(_row("r-1", phase=RunPhase.RUNNING))
    for phase in (RunPhase.FAILED, RunPhase.RUNNING):
        with pytest.raises(ClosedRun):
            owner.update_run(_row("r-1", phase=phase, warning="w"), annotate=True)
    read = owner.get_run("r-1")
    assert (read.phase, read.warning) == (RunPhase.RUNNING, None)
