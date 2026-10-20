"""The suite's store helpers never delete a store in the live directory."""

from __future__ import annotations

import nott_database
import pytest
from storefile import default_store_path


def test_default_store_path_leaves_the_live_store_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 2026-09-26 20:13: run by hand in an nSPAWN child, whose NOTT_DATABASE_DIR is
    # the live directory, this helper deleted the live store before create_store
    # refused the directory. Here the test's own directory plays the live one.
    live = nott_database.directory()
    live.mkdir(mode=0o700, parents=True, exist_ok=True)
    monkeypatch.setattr(nott_database, "live_directory", lambda: live)
    store = live / "nmemory.sqlite3"
    store.write_bytes(b"the owner's memory")
    with pytest.raises(nott_database.RefusedError):
        default_store_path()
    assert store.read_bytes() == b"the owner's memory"
