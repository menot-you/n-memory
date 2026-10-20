"""QA F6: peer recovery uses live process identity, never a lease or assertion."""

import os
import subprocess
from collections.abc import Iterator
from dataclasses import replace

import nott_database
import pytest
from registry_reset import reset_registry, sql, store

from nspawn.process_identity import (
    ProcessIdentity,
    capture_process_identity,
    process_identity_matches,
)
from nspawn.registry import POISONED_LEASE_UNTIL, Registry, WorktreeHeld
from nspawn.rows import Backend, RunPhase, RunRow

WT = "/f6/worktree"


@pytest.fixture
def peers() -> Iterator[tuple[Registry, Registry]]:
    url = str(store())
    with Registry(url, "f6-a") as owner, Registry(url, "f6-b") as peer:
        reset_registry(owner, now=1000)
        peer.open(now=1000)
        yield owner, peer


@pytest.fixture(params=["exited", "identity-mismatch"])
def dead_identity(request: pytest.FixtureRequest) -> ProcessIdentity:
    if request.param == "exited":
        with subprocess.Popen(["sleep", "60"]) as child:
            identity = capture_process_identity(child.pid)
            child.kill()  # SIGTERM may be blocked by the process-guardian tests.
            child.wait(timeout=5)
    else:
        identity = capture_process_identity(os.getpid())
        identity = replace(identity, start_ticks=identity.start_ticks + 1)
    assert not process_identity_matches(identity)
    return identity


def claim(owner: Registry, identity: ProcessIdentity) -> RunRow:
    row = RunRow(
        run_id="f6-run",
        worktree=WT,
        phase=RunPhase.RUNNING,
        sandbox="workspace-write",
        model="test",
        owner_instance=owner.instance_id,
        created_at=1000,
        updated_at=1000,
        backend=Backend.CLAUDE,
        process_identity=identity,
    )
    owner.insert_run(row)
    owner.claim_worktree(WT, row.run_id, now=1000, lease_ms=60_000)
    return row


def test_peer_closes_proven_dead_orphan_before_lease_expiry(
    peers: tuple[Registry, Registry], dead_identity: ProcessIdentity
) -> None:
    owner, peer = peers
    row = claim(owner, dead_identity)
    # Crash leaves the instance row and RUNNING phase behind. No sweep is needed.
    outcome = peer.close_owned_outcome(row.run_id, dead_identity, now=1001)
    assert outcome.run_closed, "a peer must close a proven-dead orphan"
    assert outcome.claim_released, "close must release the claim in its transaction"
    closed = owner.get_run(row.run_id)
    assert closed is not None and closed.phase is RunPhase.CLOSED
    assert closed.process_identity is None
    assert owner.read_claim(WT) is None
    assert (
        peer.claim_worktree(WT, "f6-next", now=1002, lease_ms=100).run_id == "f6-next"
    )


@pytest.mark.parametrize("phase", [RunPhase.FAILED, RunPhase.CLOSED])
def test_terminal_update_releases_claim(
    peers: tuple[Registry, Registry], dead_identity: ProcessIdentity, phase: RunPhase
) -> None:
    owner, peer = peers
    row = claim(owner, dead_identity)
    owner.update_run(replace(row, phase=phase, updated_at=1001))
    assert peer.read_claim(WT) is None, "a terminal row must not retain its claim"
    assert (
        peer.claim_worktree(WT, "f6-next", now=1002, lease_ms=100).run_id == "f6-next"
    )


@pytest.mark.parametrize("now", [1001, 100_000], ids=["live-lease", "expired-lease"])
def test_live_custody_stays_fenced(peers: tuple[Registry, Registry], now: int) -> None:
    owner, peer = peers
    identity = capture_process_identity(os.getpid())
    row = claim(owner, identity)
    outcome = peer.close_owned_outcome(row.run_id, identity, now=now)
    assert not outcome.run_closed, "a live process must not be closed by the registry"
    assert not outcome.claim_released
    with pytest.raises(WorktreeHeld):
        peer.claim_worktree(WT, "f6-next", now=now, lease_ms=100)


def test_poisoned_claim_stays_fenced(
    peers: tuple[Registry, Registry], dead_identity: ProcessIdentity
) -> None:
    owner, peer = peers
    row = claim(owner, dead_identity)
    owner.poison_claim(WT, row.run_id)
    owner.remove_instance()
    outcome = peer.close_owned_outcome(row.run_id, dead_identity, now=1001)
    assert not outcome.claim_released, "proven death does not override poison"
    owner.update_run(replace(row, phase=RunPhase.FAILED, updated_at=1002))
    held = peer.read_claim(WT)
    assert held is not None and held.lease_until == POISONED_LEASE_UNTIL
    with pytest.raises(WorktreeHeld):
        peer.claim_worktree(WT, "f6-next", now=100_000, lease_ms=100)


def test_failed_update_rolls_back_if_claim_delete_fails(
    peers: tuple[Registry, Registry], dead_identity: ProcessIdentity
) -> None:
    owner, peer = peers
    row = claim(owner, dead_identity)
    sql(
        owner,
        "CREATE TRIGGER f6_delete BEFORE DELETE ON claims "
        "BEGIN SELECT RAISE(ABORT, 'F6 delete refused'); END",
    )
    with pytest.raises(nott_database.IntegrityError, match="F6 delete refused"):
        owner.update_run(replace(row, phase=RunPhase.FAILED, updated_at=1001))
    assert peer.get_run(row.run_id) == row
    assert peer.read_claim(WT) is not None
