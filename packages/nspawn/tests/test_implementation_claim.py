"""A pending export that the retired mode left keeps its exact claim custody.

No current code writes custody events. These tests seed the legacy rows that
the retired export mode left, and they prove the kept reader on them."""

import pytest
from test_registry import WT, _insert_run, owner, rival

from nspawn.registry import POISONED_LEASE_UNTIL, RegistryError

__all__ = ["owner", "rival"]

CUSTODY = "nott.implementation.custody.v1"


def _seed_custody(registry, at: int, payload: dict[str, object]) -> None:
    """Append one legacy custody event, as the retired export mode wrote it."""
    registry.append_run_event(
        "controlled", at=at, kind="lifecycle", payload={"schema": CUSTODY, **payload}
    )


def test_pending_export_cannot_be_released_before_reconciliation(
    owner, rival, tmp_path
):
    """The legacy row is a pending event and a poisoned claim."""
    _insert_run(owner, "controlled", phase="pending")
    owner.claim_worktree(WT, "controlled", 1000, 1000)
    reference = {"path": str(tmp_path / "recovery.json"), "sha256": "a" * 64}
    _seed_custody(owner, 1100, {"state": "pending", "artifact": reference})
    owner.poison_claim(WT, "controlled")
    assert rival.implementation_recovery("controlled") == reference
    assert rival.read_claim(WT).lease_until == POISONED_LEASE_UNTIL
    with pytest.raises(RegistryError, match="reconciliation"):
        owner.release_claim(WT, "controlled")
    assert rival.read_claim(WT).run_id == "controlled"


def test_a_resolved_export_releases_its_claim(owner, rival, tmp_path):
    """A resolved event after the pending one clears the fence."""
    _insert_run(owner, "controlled", phase="failed")
    owner.claim_worktree(WT, "controlled", 1000, 1000)
    reference = {"path": str(tmp_path / "recovery.json"), "sha256": "a" * 64}
    _seed_custody(owner, 1100, {"state": "pending", "artifact": reference})
    _seed_custody(owner, 1200, {"state": "resolved"})
    assert rival.implementation_recovery("controlled") is None
    assert owner.release_claim(WT, "controlled") is True
    assert rival.read_claim(WT) is None


@pytest.mark.parametrize(
    "reference",
    [
        {"path": "relative.json", "sha256": "a" * 64},
        {"path": "/tmp/recovery.json", "sha256": "invalid"},
        {"path": "/tmp/recovery.json", "sha256": "A" * 64},
        {"path": "/tmp/recovery.json", "sha256": "a" * 64, "extra": "value"},
    ],
)
def test_a_malformed_recovery_reference_does_not_change_custody(
    owner, rival, reference
):
    _insert_run(owner, "controlled", phase="pending")
    owner.claim_worktree(WT, "controlled", 1000, 1000)
    _seed_custody(owner, 1100, {"state": "pending", "artifact": reference})
    with pytest.raises(RegistryError, match="artifact"):
        rival.implementation_recovery("controlled")
    with pytest.raises(RegistryError, match="artifact"):
        owner.release_claim(WT, "controlled")
    claim = rival.read_claim(WT)
    assert claim is not None
    assert (claim.run_id, claim.lease_until) == ("controlled", 2000)
