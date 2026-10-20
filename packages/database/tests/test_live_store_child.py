"""Test children must refuse active stores and captures before they change data."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

import nott_database as db
from nott_database import captures

PNG = b"\x89PNG\r\n\x1a\nchild-test"

_CHILD = """
import json
import pwd
import sys
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import nott_database as db
from nott_database import captures

assert "pytest" not in sys.modules
with patch.object(pwd, "getpwuid", return_value=SimpleNamespace(pw_dir=str(Path.home()))):
    try:
        if sys.argv[1] == "write":
            with closing(db.connect(db.directory() / "s.sqlite3", allow_live_store=True)) as conn:
                with db.write(conn):
                    conn.execute("UPDATE t SET v = 'changed'")
        else:
            captures.clear()
    except db.RefusedError as error:
        assert "live store" in str(error)
        print(json.dumps({"refused": True}))
    else:
        print(json.dumps({"refused": False}))
"""


@pytest.mark.parametrize("marker", [*db.TEST_MARKERS, None])
@pytest.mark.parametrize("consumer", ["write", "clear"])
@pytest.mark.parametrize("active", [True, False], ids=["active", "disposable"])
def test_child_store_and_capture_admission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    marker: str | None,
    consumer: str,
    active: bool,
) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    folder = (
        home / ".nott" / "state" / "database"
        if active
        else tmp_path / "disposable" / "database"
    )
    # Seed and inspect disposable data without consulting the account's active store.
    monkeypatch.setattr(db, "live_directory", lambda: tmp_path / "unused")
    monkeypatch.setenv(db.DIRECTORY_VARIABLE, str(folder))
    store = folder / "s.sqlite3"
    db.create(store, "CREATE TABLE t(v TEXT); INSERT INTO t VALUES ('seed');")
    ident = captures.keep(PNG)
    capture = captures.root() / f"{ident}.png"
    env = {
        name: value for name, value in os.environ.items() if name not in db.TEST_MARKERS
    }
    env["HOME"] = str(home)
    if marker is not None:
        env[marker] = "child-test"
    done = subprocess.run(
        [sys.executable, "-c", _CHILD, consumer],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    with closing(db.connect(store, readonly=True)) as conn:
        rows = db.query(conn, "SELECT v FROM t")
    refused = active and marker is not None
    assert {
        "refused": json.loads(done.stdout)["refused"],
        "rows": rows,
        "capture": capture.read_bytes() if capture.exists() else None,
    } == {
        "refused": refused,
        "rows": [("changed" if consumer == "write" and not refused else "seed",)],
        "capture": None if consumer == "clear" and not refused else PNG,
    }
