"""Writing feedback uses creation time and missing usage rows, never session guesses."""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from child_env import child_env
from storefile import default_store_path, sample_capsule_id
from workspace_root import workspace_path

from nmemory.queries.census import write_read_census
from nmemory.server import MemoryServer
from nmemory.store import Store


def test_write_read_census_fences_time_project_and_missing_usage(
    tmp_path: Path,
) -> None:
    url = default_store_path("writecensus")
    store = Store.open(url)
    server = MemoryServer(store=store)
    now = datetime.now(UTC)
    for index, (project, kind) in enumerate(
        (("nott", "fact"), ("nott/memory", "task"), ("nottx", "task"), ("nott", "fact"))
    ):
        response = server.call_tool(
            "nmemory_capsule_ingest",
            {
                "content": f"Writing census {index}",
                "source": "test_store_census",
                "anchor": str(index),
                "proof_hint": "fixture: a porta de captura exige prova re-obtenível",
                "project_id": project,
                "kind": kind,
            },
        )
        assert not response["result"]["isError"], response
    store.traffic.record_recall("cap-1", now)
    with store.db.writing() as conn, conn.cursor() as cur:
        cur.execute(
            (
                "UPDATE capsules SET created_at=? WHERE id='"
                + sample_capsule_id(4)
                + "'"
            ),
            ((now - timedelta(days=8)).isoformat(),),
        )
    expected = {"fact": (1, 0), "task": (1, 1)}
    assert write_read_census(store.db, now - timedelta(days=7), "nott") == expected
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "nmemory.cli",
            "digest",
            "show",
            "--project-prefix",
            "nott",
        ],
        env=child_env({**os.environ, "NOTT_DATABASE_DIR": str(Path(url).parent)}),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "write→read" in result.stdout
    assert "2 capsules written · 1 never recalled (50%)" in result.stdout
    assert "fact 1/0" in result.stdout
    assert "task 1/1" in result.stdout
    hook = workspace_path("plugin/hooks/nmemory-session.sh")
    injected = subprocess.run(
        ["bash", str(hook)],
        input='{"session_id":"census-test","source":"startup"}',
        env=child_env(
            {
                **os.environ,
                "NOTT_DATABASE_DIR": str(Path(url).parent),
                "NOTT_NMEMORY_BIN": str(Path(sys.executable).with_name("nmemory")),
                "NOTT_NMEMORY_PROJECT": "nott",
                "NOTT_SESSION_DIGEST": "on",
                "XDG_STATE_HOME": str(tmp_path),
            }
        ),
        text=True,
        capture_output=True,
        check=False,
    )
    assert injected.returncode == 0, injected.stderr
    assert "write→read" in injected.stdout
    assert "2 capsules written · 1 never recalled (50%)" in injected.stdout
    store.traffic.record_recall(sample_capsule_id(2), now)
    assert write_read_census(store.db, now - timedelta(days=7), "nott") == {
        "fact": (1, 0),
        "task": (1, 0),
    }
    assert write_read_census(store.db, now - timedelta(days=7), "absent") == {}
