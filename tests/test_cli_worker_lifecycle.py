"""The CLI stops its workers when the transport ends or startup fails."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from nmemory import cli, curator
from nmemory.embedding import EmbeddingClient
from nmemory.embedding_index import EmbeddingWorker, spawn_embedding_worker
from nmemory.server import MemoryServer
from nmemory.store import Store


@pytest.mark.parametrize("transport", ["stdio", "http"])
@pytest.mark.parametrize(
    "ending", ["return", "transport_error", "startup_error", "busy"]
)
def test_cli_stops_started_workers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, transport: str, ending: str
) -> None:
    store = Store.open_disposable_test_store()
    spawned: list[tuple[EmbeddingWorker | curator.CuratorWorker, threading.Thread]] = []
    embedding_ready = threading.Event()
    curator_ready = threading.Event()
    release = threading.Event()
    failure = OSError("synthetic lifecycle failure")

    def cycle(worker: EmbeddingWorker) -> int:
        embedding_ready.set()
        if ending == "busy":
            assert release.wait(timeout=10)
        return 0

    def expire(worker: curator.CuratorWorker) -> int:
        curator_ready.set()
        return 0

    def embedding_pair(source: Store) -> tuple[EmbeddingWorker, threading.Thread]:
        pair = spawn_embedding_worker(source, EmbeddingClient(api_key=""))
        spawned.append(pair)
        return pair

    real_curator_spawn = curator.spawn_curator_worker

    def curator_pair(
        source: Store, *, root: Path, project: str
    ) -> tuple[curator.CuratorWorker, threading.Thread]:
        if ending == "startup_error":
            raise failure
        pair = real_curator_spawn(source, root=root, project=project)
        spawned.append(pair)
        return pair

    def serve(server: MemoryServer, *args: object) -> int:
        assert not spawned, "the CLI must keep store opening lazy"
        assert server.store is store
        assert embedding_ready.wait(timeout=5)
        assert curator_ready.wait(timeout=5)
        if ending == "transport_error":
            raise failure
        return 0

    monkeypatch.setenv("NOTT_NMEMORY_CURATOR", "1")
    monkeypatch.setattr(cli, "provider_name", lambda: "zai")
    monkeypatch.setattr(cli, "open_store_from_args", lambda *a, **k: store)
    monkeypatch.setattr(cli, "spawn_embedding_worker", embedding_pair)
    monkeypatch.setattr(curator, "spawn_curator_worker", curator_pair)
    monkeypatch.setattr(curator, "curator_root", lambda: tmp_path)
    monkeypatch.setattr(curator.CuratorClient, "is_configured", lambda self: False)
    monkeypatch.setattr(curator.system1, "is_configured", lambda *a, **k: False)
    monkeypatch.setattr(EmbeddingWorker, "cycle", cycle)
    monkeypatch.setattr(curator.CuratorWorker, "expire_staged", expire)
    monkeypatch.setattr(
        curator.CuratorWorker, "reject_unsupported_evidence", lambda self: 0
    )
    monkeypatch.setattr("nmemory.server.run_stdio_server", serve)
    monkeypatch.setattr("nmemory.serve.run_http_server", serve)
    flags = (
        ["--http", "--key-file", str(tmp_path / "key")] if transport == "http" else []
    )

    started = time.monotonic()
    try:
        if ending in {"transport_error", "startup_error"}:
            with pytest.raises(OSError) as raised:
                cli.cmd_mcp_serve(["--project", tmp_path.name, *flags])
            assert raised.value is failure
        elif transport == "http":
            with pytest.raises(SystemExit) as exited:
                cli.cmd_mcp_serve(["--project", tmp_path.name, *flags])
            assert exited.value.code == 0
        else:
            cli.cmd_mcp_serve(["--project", tmp_path.name, *flags])

        # A blocked provider must not hold the CLI beyond the shutdown budget.
        assert time.monotonic() - started < 3
        assert spawned
        assert all(worker.is_stopped() for worker, _ in spawned)
        assert all(
            not thread.is_alive()
            for worker, thread in spawned
            if ending != "busy" or not isinstance(worker, EmbeddingWorker)
        )
    finally:
        release.set()
        for worker, thread in spawned:
            worker.stop()
            thread.join(timeout=5)
            assert not thread.is_alive()
