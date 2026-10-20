"""Durable pending export retains exact claim custody across later owning calls."""

import pytest
from test_registry import WT, _insert_run, owner, rival

from nspawn.registry import POISONED_LEASE_UNTIL, RegistryError

__all__ = ["owner", "rival"]


def test_pending_export_cannot_be_released_before_reconciliation(
    owner, rival, tmp_path
):
    _insert_run(owner, "controlled", phase="pending")
    owner.claim_worktree(WT, "controlled", 1000, 1000)
    reference = {"path": str(tmp_path / "recovery.json"), "sha256": "a" * 64}
    owner.record_implementation_recovery("controlled", WT, 1100, 1000, reference)
    assert rival.implementation_recovery("controlled") == reference
    assert rival.read_claim(WT).lease_until == POISONED_LEASE_UNTIL
    with pytest.raises(RegistryError):
        owner.release_claim(WT, "controlled")
    assert rival.read_claim(WT).run_id == "controlled"


@pytest.mark.parametrize(
    "reference",
    [
        {"path": "relative.json", "sha256": "a" * 64},
        {"path": "/tmp/recovery.json", "sha256": "invalid"},
        {"path": "/tmp/recovery.json", "sha256": "A" * 64},
        {"path": "/tmp/recovery.json", "sha256": "a" * 64, "extra": "value"},
    ],
)
def test_invalid_recovery_reference_does_not_change_custody(owner, rival, reference):
    _insert_run(owner, "controlled", phase="pending")
    owner.claim_worktree(WT, "controlled", 1000, 1000)
    with pytest.raises(RegistryError, match="artifact"):
        owner.record_implementation_recovery("controlled", WT, 1100, 1000, reference)
    assert rival.implementation_recovery("controlled") is None
    assert rival.read_claim(WT).lease_until == 2000


def test_recovery_that_resolves_its_own_fence_returns(owner, rival, tmp_path):
    """The close path's recovery deletes the poisoned claim it owns, inside the callback.

    Ownership is re-read after the callback; a fence the recovery itself resolved is
    not a claim taken meanwhile. Measured through the native-authority outage case of the
    pre-split harness member (retired in c98b8b2e6): `supervisor.close` raised "no longer owns" after a successful recovery.
    """
    _insert_run(owner, "controlled", phase="failed")
    owner.claim_worktree(WT, "controlled", 1000, 1000)
    reference = {"path": str(tmp_path / "recovery.json"), "sha256": "a" * 64}
    owner.record_implementation_recovery("controlled", WT, 1100, 1000, reference)

    def resolve() -> str:
        owner.resolve_implementation_recovery("controlled", WT, owner.instance_id, 1200)
        return "resolved"

    outcome = owner.with_owned_claim("controlled", WT, 1200, resolve, recovery=True)
    assert outcome == "resolved"
    assert rival.read_claim(WT) is None
    assert rival.implementation_recovery("controlled") is None


@pytest.mark.parametrize("recovery", [False, True])
def test_a_claim_taken_during_the_callback_fails_the_call(owner, rival, recovery):
    _insert_run(owner, "controlled", phase="running")
    owner.claim_worktree(WT, "controlled", 1000, 1000)

    def stolen() -> None:
        owner.release_claim(WT, "controlled")
        _insert_run(rival, "rival", phase="running")
        rival.claim_worktree(WT, "rival", 1200, 1000)

    with pytest.raises(RegistryError, match="no longer owns"):
        owner.with_owned_claim("controlled", WT, 1200, stolen, recovery=recovery)


def test_a_claim_released_during_a_live_callback_fails_the_call(owner):
    _insert_run(owner, "controlled", phase="running")
    owner.claim_worktree(WT, "controlled", 1000, 1000)

    def released() -> None:
        owner.release_claim(WT, "controlled")

    with pytest.raises(RegistryError, match="no longer owns"):
        owner.with_owned_claim("controlled", WT, 1200, released)


def test_a_fence_gone_without_its_resolution_fails_the_recovery(owner, tmp_path):
    """Absence counts as the recovery's own resolution only when it was resolved."""
    _insert_run(owner, "controlled", phase="failed")
    owner.claim_worktree(WT, "controlled", 1000, 1000)
    reference = {"path": str(tmp_path / "recovery.json"), "sha256": "a" * 64}
    owner.record_implementation_recovery("controlled", WT, 1100, 1000, reference)

    def deleted_behind_its_back() -> None:
        with owner._transaction():
            owner._run("DELETE FROM claims WHERE worktree = ?", (WT,))

    with pytest.raises(RegistryError, match="no longer owns"):
        owner.with_owned_claim(
            "controlled", WT, 1200, deleted_behind_its_back, recovery=True
        )


def test_a_lapsed_lease_taken_during_the_callback_fails_the_call(owner, rival):
    """The post-check's `now` is the entry clock; a rival's take is still seen.

    A lease that lapses mid-callback can be taken only by overwriting or deleting
    the one claim row, and the post-check reads that row's run and holder.
    """
    _insert_run(owner, "controlled", phase="pending")
    owner.claim_worktree(WT, "controlled", 1000, 100)

    def lapsed_and_taken() -> None:
        _insert_run(rival, "rival", phase="pending")
        rival.claim_worktree(WT, "rival", 1200, 1000)

    with pytest.raises(RegistryError, match="no longer owns"):
        owner.with_owned_claim("controlled", WT, 1050, lapsed_and_taken)
