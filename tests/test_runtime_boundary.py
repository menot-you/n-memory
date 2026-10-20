"""Tests for runtime boundary: embedding worker spawning on the shipping stdio serve path."""

from __future__ import annotations

import json
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import pytest
from child_env import child_env
from storefile import default_store_path
from test_embedding_worker_owner import (
    _CHILD,
    _server,
    _wait_for_completed_vector,
    _wait_for_event,
)

from nmemory.database import Database

TEST_DB = Path()


@pytest.fixture(autouse=True)
def clean_store() -> None:
    """The store the shipping CLI opens: `store_path()` under the test's own directory."""
    global TEST_DB
    TEST_DB = default_store_path()


def _read_mcp_line(proc: subprocess.Popen[str], timeout: float = 5.0) -> dict[str, Any]:
    assert proc.stdout is not None
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        line = proc.stdout.readline()
        if line:
            line = line.strip()
            if line:
                try:
                    return json.loads(line)  # type: ignore[no-any-return]
                except json.JSONDecodeError:
                    continue
        time.sleep(0.05)
    raise TimeoutError("Timed out waiting for JSON-RPC response from process")


def test_shipping_stdio_server_spawns_embedding_worker_probe() -> None:
    """The shipping stdio worker starts keyless and leaves jobs for a configured replica."""
    env = child_env()
    env["NOTT_DATABASE_DIR"] = str(TEST_DB.parent)
    env.pop("OPENAI_API_KEY", None)
    env["NOTT_TEST_EMBEDDING_MODE"] = "unconfigured"
    pg = Database(TEST_DB)
    with pg.writing() as conn, conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE worker_observations (pid integer, thread text, claimed boolean)"
        )
        cur.execute("CREATE TABLE worker_events (pid integer, event text)")

    proc = subprocess.Popen(
        # Observe the real ownership/claim methods around the shipping cli.main().
        [sys.executable, "-c", _CHILD, "mcp", "serve"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env=env,
    )

    try:
        # 1. MCP Initialize
        init_req = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "test-harness", "version": "1.0"},
            },
        }
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(init_req) + "\n")
        proc.stdin.flush()
        init_resp = _read_mcp_line(proc)
        assert init_resp.get("id") == 1

        # 2. Ingest one capsule
        ingest_req = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "nmemory_capsule_ingest",
                "arguments": {
                    "content": "FUGU exact probe test capsule content",
                    "kind": "fact",
                    "source": "fugu_probe",
                    "anchor": "fugu.py:1",
                },
            },
        }
        proc.stdin.write(json.dumps(ingest_req) + "\n")
        proc.stdin.flush()
        ingest_resp = _read_mcp_line(proc)
        assert ingest_resp.get("id") == 2
        assert "error" not in ingest_resp, ingest_resp
        assert not ingest_resp.get("result", {}).get("isError"), ingest_resp

        # 3. Prove that the shipping path actually spawned the worker.
        _wait_for_event(TEST_DB, proc.pid, "owner")
        time.sleep(2.5)

        # 4. A keyless worker must leave the shared job ready and unclaimed.
        with pg.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT attempts, last_error_class, (available_at <= CAST(unixepoch('subsec') * 1000000 AS INTEGER)) AS ready FROM embedding_jobs;"
            )
            rows = cur.fetchall()
            assert len(rows) == 1, "Expected exactly one embedding job in database"
            attempts, last_error_class, ready = rows[0]
            assert attempts == 0, f"Expected attempts=0, got {attempts}"
            assert last_error_class is None, (
                f"Expected no failure class, got {last_error_class}"
            )
            assert ready == 1, f"Expected ready=1, got {ready}"

        # 5. A second shipping server claims and completes while A stays alive.
        with _server(TEST_DB) as configured:
            _wait_for_completed_vector(TEST_DB, configured.pid)
            assert proc.poll() is None
            assert configured.poll() is None
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2.0)


def test_canned_transport_end_to_end_ingest_job_vector_search() -> None:
    """Canned transport observes ingest -> job -> vector -> search end-to-end."""
    from nmemory.embedding import EmbeddingClient
    from nmemory.embedding_index import spawn_embedding_worker
    from nmemory.retrieve import RetrieveParams, retrieve
    from nmemory.server import MemoryServer
    from nmemory.space import DIMENSION
    from nmemory.store import Store

    # Create dummy 3072-dim unit vector
    dummy_vec = [0.0] * DIMENSION
    dummy_vec[0] = 1.0

    client = EmbeddingClient(custom_hook=lambda _text: list(dummy_vec))
    store = Store.open(TEST_DB)
    worker, thread = spawn_embedding_worker(store, client=client)

    try:
        server = MemoryServer(store=store)
        # Unique content per run: identical re-ingest dedups to zero new jobs,
        # so a reused database would leave the worker with nothing to drain.
        unique_marker = uuid.uuid4().hex
        server.call_tool(
            "nmemory_capsule_ingest",
            {
                "content": f"Semantic memory search item for runtime boundary verification {unique_marker}",
                "kind": "fact",
                "source": "canned_test",
                "anchor": "canned.py:1",
            },
        )

        # Bounded eventual observation: poll until background worker creates embedding row
        t0 = time.monotonic()
        count = 0
        while time.monotonic() - t0 < 5.0:
            with (
                Database(TEST_DB).connection() as conn,
                conn.cursor() as cur,
            ):
                cur.execute("SELECT COUNT(*) FROM capsule_embeddings;")
                row = cur.fetchone()
                count = row[0] if row else 0
                if count >= 1:
                    break
            time.sleep(0.05)
        assert count >= 1, (
            "Expected at least 1 capsule embedding created by background worker"
        )

        # Search / retrieve
        res = retrieve(
            store,
            RetrieveParams(
                query="Semantic memory search item",
                limit=5,
            ),
            embeddings=client,
        )
        assert res["outcome"] == "grounded"
        assert len(res["evidence"]) >= 1
        assert "Semantic memory search item" in res["evidence"][0]["content"]
    finally:
        worker.stop()
        thread.join(timeout=2.0)
