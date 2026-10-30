"""claude_restricted.make_scratch: the per-run scratch of the node cage.

The node host (`harness/nott_harness/acp`) imports it, so the writer cage change
must not remove it.
"""

import stat
from pathlib import Path

import pytest

from nspawn import claude_restricted
from nspawn.claude_restricted import RestrictedLaunchRefused, make_scratch


def test_scratch_is_a_private_directory_under_the_given_root(tmp_path: Path) -> None:
    scratch = make_scratch(tmp_path)
    assert scratch.parent == tmp_path
    assert scratch.name.startswith("nott-claude-")
    assert stat.S_IMODE(scratch.stat().st_mode) == 0o700


def test_scratch_defaults_to_the_shared_var_tmp() -> None:
    assert claude_restricted.SCRATCH_ROOT == Path("/var/tmp")
    scratch = make_scratch()
    try:
        assert scratch.parent == Path("/var/tmp")
    finally:
        scratch.rmdir()


def test_scratch_refuses_a_missing_root(tmp_path: Path) -> None:
    with pytest.raises(RestrictedLaunchRefused, match="scratch root"):
        make_scratch(tmp_path / "absent")
