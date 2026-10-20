"""The owner-file fences vendor_home serves still refuse what they must."""

from __future__ import annotations

from pathlib import Path

import pytest

from nspawn import vendor_home


def test_owner_config_is_read_through_an_owner_held_directory_alias(
    tmp_path: Path,
) -> None:
    "A symlinked DIRECTORY on the path is the owner's own bundle layout."
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    document = bundle / "NOTT.md"
    document.write_text("A lei.")
    document.chmod(0o644)
    alias = tmp_path / "alias"
    alias.symlink_to(bundle, target_is_directory=True)

    assert vendor_home.read_owner_config(alias / "NOTT.md") == b"A lei."


def test_owner_config_refuses_a_directory_alias_into_a_group_writable_tree(
    tmp_path: Path,
) -> None:
    """The alias may not launder a directory the invariant would refuse."""
    broad = tmp_path / "broad"
    broad.mkdir()
    broad.chmod(0o775)
    document = broad / "NOTT.md"
    document.write_text("A lei.")
    document.chmod(0o644)
    alias = tmp_path / "alias-broad"
    alias.symlink_to(broad, target_is_directory=True)

    with pytest.raises(vendor_home.VendorHomeError, match="owner-controlled"):
        vendor_home.read_owner_config(alias / "NOTT.md")
