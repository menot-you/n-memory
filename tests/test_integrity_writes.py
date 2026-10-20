"""Verify recovery after process death and a full test filesystem."""

from __future__ import annotations

import json
import os
import random
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from child_env import child_env
from nott_database import Error
from storefile import fresh_store_path

from nmemory import audit_actions
from nmemory.ingest import IngestDefaults, IngestRequest, ingest
from nmemory.store import Store


def content_for(run: int, index: int, repeats: int = 256) -> str:
    return (
        f"Write run {run} record {index}. "
        + "The store keeps this test record. " * repeats
    )


def capture(store: Store, content: str) -> str:
    now = datetime.now(UTC)
    with store.db.tool_boundary():
        outcome = ingest(
            store,
            IngestRequest(
                content=content,
                source="test:integrity-writes",
                anchor="tests/test_integrity_writes.py:1",
                hints=False,
            ),
            IngestDefaults(project_id="integrity"),
            now,
        )
        assert not outcome.deduped
        store.audit.append_audit("test", audit_actions.INGEST, outcome.id, None, now)
    return outcome.id


def assert_integrity(store: Store) -> None:
    with store.db.connection() as conn:
        assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    chain = store.audit.verify_chain()
    assert chain.is_ok, chain


@pytest.mark.skipif(
    sys.platform != "linux", reason="This test requires SIGKILL on Linux."
)
def test_committed_capsules_survive_twenty_kills(tmp_path: Path) -> None:
    path = fresh_store_path("crash")
    rng = random.Random(20261016)
    committed: dict[str, str] = {}
    for run in range(20):
        report = tmp_path / f"committed-{run}.jsonl"
        target = rng.randint(1, 5)
        with (
            report.open("w") as output,
            report.with_suffix(".err").open("w+") as errors,
        ):
            proc = subprocess.Popen(
                [sys.executable, __file__, str(path), str(run)],
                stdout=output,
                stderr=errors,
                env=child_env(),
            )
            try:
                deadline = time.monotonic() + 10
                while len(report.read_text().splitlines()) < target:
                    assert proc.poll() is None, report.with_suffix(".err").read_text()
                    assert time.monotonic() < deadline, (
                        "The child did not report a commit."
                    )
                    time.sleep(0.002)
                time.sleep(rng.uniform(0, 0.02))
                proc.send_signal(signal.SIGKILL)
                assert proc.wait(timeout=5) == -signal.SIGKILL
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=5)
        for line in report.read_text().splitlines():
            row = json.loads(line)
            committed[row["id"]] = content_for(run, row["index"])
        reopened = Store.open(path)
        assert_integrity(reopened)
        assert reopened.audit.verify_chain().count >= len(committed)
        for cid, content in committed.items():
            stored = reopened.capsules.get(cid)
            assert stored is not None, (run, cid)
            assert stored.capsule.content == content, (run, cid)


@pytest.mark.skipif(
    not os.environ.get("NOTT_FULLFS"),
    reason="Set NOTT_FULLFS to a small test filesystem.",
)
def test_disk_full_preserves_store(monkeypatch: pytest.MonkeyPatch) -> None:
    root = Path(os.environ["NOTT_FULLFS"])
    assert root.is_absolute() and root.is_mount(), (
        "NOTT_FULLFS must be a mounted test filesystem."
    )
    assert os.statvfs(root).f_blocks * os.statvfs(root).f_frsize <= 24 * 2**20
    with TemporaryDirectory(prefix="integrity-", dir=root) as directory:
        monkeypatch.setenv("NOTT_DATABASE_DIR", str(Path(directory) / "database"))
        store = Store.open(fresh_store_path("full"))
        committed: dict[str, str] = {}
        with pytest.raises(Error, match="full"):
            for index in range(200):
                content = content_for(0, index, repeats=8192)
                committed[capture(store, content)] = content
            pytest.fail("The test store did not fill the test filesystem.")
        assert committed
        reopened = Store.open(store.db.path)
        assert_integrity(reopened)
        assert reopened.audit.verify_chain().count == len(committed)
        for cid, content in committed.items():
            stored = reopened.capsules.get(cid)
            assert stored is not None
            assert stored.capsule.content == content


if __name__ == "__main__":
    child_store = Store.open(sys.argv[1])
    child_run = int(sys.argv[2])
    for child_index in range(1000):
        child_id = capture(child_store, content_for(child_run, child_index))
        print(json.dumps({"id": child_id, "index": child_index}), flush=True)
