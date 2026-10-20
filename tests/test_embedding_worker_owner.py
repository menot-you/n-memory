"""Observe real queue claims by independent stdio server processes on one store file."""

from __future__ import annotations

import json
import os
import selectors
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from child_env import child_env
from stdio_peer import INITIALIZED, complete

from nmemory.database import STORE_FILE, Database, create_store, vector_blob
from nmemory.embedding import EmbeddingClient
from nmemory.embedding_index import EmbeddingWorker
from nmemory.repositories.embeddings import EmbeddingRepository
from nmemory.server import MemoryServer
from nmemory.space import DIMENSION, SpaceFingerprint
from nmemory.store import Store

# Instrument the real claim, without replacing the worker, queue, or server.
_CHILD = """
import os, threading
from contextlib import contextmanager
from nmemory.database import Database
from nmemory.repositories.embeddings import EmbeddingRepository
from nmemory.embedding import EmbeddingClient
from nmemory.space import DIMENSION
from nmemory import cli

def record(store, event):
    with store.writing() as conn, conn.cursor() as cur:
        cur.execute('INSERT INTO worker_events VALUES (?, ?)', (os.getpid(), event))

original_owner = Database.embedding_worker_owner
@contextmanager
def observed_owner(self):
    with original_owner(self) as owner:
        if owner:
            record(self, 'owner')
        yield owner
Database.embedding_worker_owner = observed_owner

original_spawn = cli.spawn_embedding_worker
def configured_spawn(store):
    client = EmbeddingClient()
    def embed(text):
        record(store.db, 'embedded')
        if os.environ['NOTT_TEST_EMBEDDING_MODE'] == 'once':
            client.set_hook(None)
        return [1.0] + [0.0] * (DIMENSION - 1)
    if os.environ['NOTT_TEST_EMBEDDING_MODE'] != 'unconfigured':
        client.set_hook(embed)
    return original_spawn(store, client)
cli.spawn_embedding_worker = configured_spawn

original = EmbeddingRepository.claim_embedding
def observed(self, *args):
    job = original(self, *args)
    with self.db.writing() as conn, conn.cursor() as cur:
        cur.execute(
            'INSERT INTO worker_observations VALUES (?, ?, ?)',
            (os.getpid(), threading.current_thread().name, job is not None),
        )
    return job
EmbeddingRepository.claim_embedding = observed
cli.main()
"""


def _store(name: str) -> Path:
    """A store of its own, in a directory of its own: each child opens `store_path()`."""
    url = Path(os.environ["NOTT_DATABASE_DIR"]) / name / STORE_FILE
    create_store(url)
    pg = Database(url)
    with pg.writing() as conn, conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE worker_observations (pid integer, thread text, claimed boolean)"
        )
        cur.execute("CREATE TABLE worker_events (pid integer, event text)")
    result = MemoryServer(store=Store.open(url)).call_tool(
        "nmemory_capsule_ingest",
        {
            "content": "Exclusive worker queue evidence",
            "kind": "task",
            "source": "test",
            "anchor": "worker:1",
        },
    )
    assert not result.get("isError"), result
    return url


@contextmanager
def _server(
    url: Path, *, read_only: bool = False, mode: str = "configured"
) -> Iterator[subprocess.Popen[str]]:
    env = child_env()
    env["NOTT_DATABASE_DIR"] = str(url.parent)
    # The intent is "a child with NO provider credential at all". Naming only
    # the OpenAI key made it silently provider-specific: with the default on
    # Gemini, the real environment's GEMINI_API_KEY got through and the child
    # was born configured, and the test measured the opposite of its claim.
    for chave in ("OPENAI_API_KEY", "GEMINI_API_KEY"):
        env.pop(chave, None)
    env["NOTT_TEST_EMBEDDING_MODE"] = mode
    with subprocess.Popen(
        [
            sys.executable,
            "-c",
            _CHILD,
            "mcp",
            "serve",
            *(["--read-only"] if read_only else []),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    ) as proc:
        try:
            assert proc.stdin is not None and proc.stdout is not None
            proc.stdin.write(
                json.dumps(complete({"id": 0, "method": "initialize"})) + "\n"
            )
            proc.stdin.flush()
            proc.stdout.readline()
            proc.stdin.write(json.dumps(INITIALIZED) + "\n")
            proc.stdin.write(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {
                            "name": "nmemory_context_show",
                            "arguments": {
                                "query": "worker",
                                "sources": ["memory"],
                            },
                        },
                    }
                )
                + "\n"
            )
            proc.stdin.flush()
            with selectors.DefaultSelector() as selector:
                selector.register(proc.stdout, selectors.EVENT_READ)
                assert selector.select(20), "stdio store startup timed out"
            response = json.loads(proc.stdout.readline())
            assert "error" not in response, response
            assert not response.get("result", {}).get("isError"), response
            yield proc
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)


def _observations(url: Path) -> list[tuple[int, str, bool]]:
    with Database(url).connection() as conn, conn.cursor() as cur:
        return cur.execute(
            "SELECT pid, thread, claimed FROM worker_observations"
        ).fetchall()


def _wait_for_claim(url: Path, *, excluding: int | None = None) -> int:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        for pid, thread, claimed in _observations(url):
            assert thread == "nmemory-embedding-worker", (pid, thread)
            if claimed and pid != excluding:
                return pid
        time.sleep(0.05)
    raise AssertionError(
        "No exclusive successor processed a real queue job within 10 seconds"
    )


def test_three_stdio_servers_have_one_queue_owner_and_bounded_takeover() -> None:
    url = _store("owner")
    with _server(url) as first, _server(url) as second, _server(url) as third:
        owner = _wait_for_claim(url)
        time.sleep(3)
        observations = _observations(url)
        owners = {pid for pid, _, _ in observations}
        assert owners == {owner}, f"Multiple queue processors: {observations}"
        processes = {proc.pid: proc for proc in (first, second, third)}
        processes[owner].kill()
        processes[owner].wait(timeout=5)
        with Database(url).writing() as conn, conn.cursor() as cur:
            # The configured owner completed the original job. Delete vectors and advance
            # the generation so the successor's generation sync creates the job via queue_embeddings.
            cur.execute("DELETE FROM capsule_embeddings")
            cur.execute("UPDATE generation SET value = value + 1")
        successor = _wait_for_claim(url, excluding=owner)
        time.sleep(3)
        successor_owners = {pid for pid, _, _ in _observations(url) if pid != owner}
        assert successor_owners == {successor}
        assert successor in processes and successor != owner


def test_read_only_server_does_not_process_queue() -> None:
    url = _store("ownerro")
    with _server(url, read_only=True):
        time.sleep(3)
        assert _observations(url) == []
        with Database(url).connection() as conn, conn.cursor() as cur:
            assert cur.execute(
                "SELECT COALESCE(sum(attempts), 0) FROM embedding_jobs"
            ).fetchone() == (0,)


def test_distinct_stores_process_independently() -> None:
    left, right = _store("ownerleft"), _store("ownerright")
    with _server(left) as first, _server(right) as second:
        assert _wait_for_claim(left) == first.pid
        assert _wait_for_claim(right) == second.pid


def _wait_for_event(url: Path, pid: int, event: str) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with Database(url).connection() as conn, conn.cursor() as cur:
            if cur.execute(
                "SELECT 1 FROM worker_events WHERE pid = ? AND event = ?",
                (pid, event),
            ).fetchone():
                return
        time.sleep(0.05)
    raise AssertionError(f"Server {pid} did not reach {event}")


def _wait_for_completed_vector(url: Path, pid: int) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with Database(url).connection() as conn, conn.cursor() as cur:
            row = cur.execute("SELECT embedding FROM capsule_embeddings").fetchone()
            if row is not None:
                assert row[0] == vector_blob([1.0] + [0.0] * (DIMENSION - 1))
                assert cur.execute(
                    "SELECT DISTINCT pid FROM worker_events WHERE event = 'embedded'"
                ).fetchall() == [(pid,)]
                assert cur.execute(
                    "SELECT count(*) FROM embedding_jobs"
                ).fetchone() == (0,)
                return
        time.sleep(0.05)
    with Database(url).connection() as conn, conn.cursor() as cur:
        pending = cur.execute(
            "SELECT attempts, last_error_class FROM embedding_jobs"
        ).fetchall()
    raise AssertionError(
        f"Configured server {pid} never completed a vector; pending={pending}"
    )


def test_unconfigured_first_owner_yields_to_configured_replica() -> None:
    url = _store("keyless")
    with _server(url, mode="unconfigured") as first:
        # A has really held the election lock before B even starts.
        _wait_for_event(url, first.pid, "owner")
        with _server(url) as second:
            _wait_for_completed_vector(url, second.pid)
            assert first.poll() is None
            assert second.poll() is None
            assert all(pid == second.pid for pid, _, _ in _observations(url))


def test_keyless_cycle_leaves_shared_jobs_available() -> None:
    url = _store("idle")
    store = Store.open(url)
    assert EmbeddingWorker(store, EmbeddingClient(api_key="")).cycle() == 0
    with store.db.connection() as conn, conn.cursor() as cur:
        assert cur.execute(
            "SELECT attempts, last_error_class,"
            " available_at <= CAST(unixepoch('subsec') * 1000000 AS INTEGER)"
            " FROM embedding_jobs"
        ).fetchall() == [(0, None, 1)]


def test_owner_that_loses_configuration_releases_queue() -> None:
    url = _store("lostkey")
    with _server(url, mode="once") as first:
        _wait_for_completed_vector(url, first.pid)
        with Database(url).writing() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM capsule_embeddings")
            cur.execute("DELETE FROM worker_events")
        with _server(url) as second:
            _wait_for_completed_vector(url, second.pid)
            assert first.poll() is None
            assert second.poll() is None


def test_complete_embedding_is_atomic_under_worker_owner() -> None:
    url = _store("atomicemb")
    pg = Database(url)
    with pg.writing() as conn, conn.cursor() as cur:
        cur.execute(
            "CREATE TRIGGER trg_fail_del BEFORE DELETE ON embedding_jobs"
            " BEGIN SELECT RAISE(ABORT, 'simulated delete failure'); END"
        )

    with pg.embedding_worker_owner():
        EmbeddingRepository(pg).queue_embeddings(
            [("cap-1", "text", "a" * 64)], SpaceFingerprint.current().as_str()
        )
        job = EmbeddingRepository(pg).claim_embedding()
        assert job is not None and job.capsule_id == "cap-1"
        with pytest.raises(Exception, match="simulated delete failure"):
            EmbeddingRepository(pg).complete_embedding(
                job, [1.0] + [0.0] * (DIMENSION - 1)
            )

    with pg.connection() as conn, conn.cursor() as cur:
        assert cur.execute("SELECT count(*) FROM capsule_embeddings").fetchone() == (0,)
