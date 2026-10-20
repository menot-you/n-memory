"""The System1 key guard uses the same private fallback as the route."""

import hashlib
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from nott_system1.renamed import RENAMED

from nmemory import system1
from nmemory.config import resolve_system1_config


def test_dotenv_key_enables_the_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / ".env"
    path.write_text("NOTT_SYSTEM1_API_KEY=test-only-key\n")
    path.chmod(0o600)
    monkeypatch.setenv("NOTT_TEST_SYSTEM1_FILE", str(path))
    assert system1.is_configured()
    assert not system1.is_configured({})
    monkeypatch.setenv("NOTT_NMEMORY_SYSTEM1", "0")
    assert not system1.is_configured()


@pytest.mark.parametrize("fault", ["mode", "old_name"])
def test_refused_file_does_not_stop_the_curator_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    from nmemory import curator
    from nmemory.store import Store

    path = tmp_path / ".env"
    path.write_text(
        f"{RENAMED['NOTT_SYSTEM1_API_KEY']}=test-only-key"
        if fault == "old_name"
        else "NOTT_SYSTEM1_API_KEY=test-only-key"
    )
    path.chmod(0o600 if fault == "old_name" else 0o644)
    monkeypatch.setenv("NOTT_TEST_SYSTEM1_FILE", str(path))
    monkeypatch.setenv("NOTT_NMEMORY_CURATOR", "1")
    assert not system1.is_configured()
    monkeypatch.setattr(curator, "CURATOR_POLL_S", 0.01)
    twice = threading.Event()
    passes = 0

    def expired(self: curator.CuratorWorker) -> list[str]:
        nonlocal passes
        passes += 1
        if passes == 2:
            self.stop()
            twice.set()
        return []

    monkeypatch.setattr(curator.CuratorWorker, "expire_staged", expired)
    store = Store.open_disposable_test_store()
    worker, thread = curator.spawn_curator_worker(store, root=tmp_path, project="test")
    try:
        assert twice.wait(2)
    finally:
        worker.stop()
        thread.join(timeout=2)
    assert not thread.is_alive()


def test_process_scope_key_precedes_a_refused_base_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / ".env"
    path.write_text("NOTT_SYSTEM1_API_KEY=file-key")
    path.chmod(0o644)
    monkeypatch.setenv("NOTT_TEST_SYSTEM1_FILE", str(path))
    monkeypatch.setenv("NOTT_SYSTEM1_CURATOR_ENDPOINT", "http://127.0.0.1:9/ask")
    monkeypatch.setenv("NOTT_SYSTEM1_CURATOR_MODEL", "fixture-model")
    monkeypatch.setenv("NOTT_SYSTEM1_CURATOR_API_KEY", "process-key")
    assert system1.is_configured(scope="CURATOR")
    assert system1.is_configured()
    assert not system1.is_configured(scope="TAINT")


def test_refused_old_name_is_logged_without_its_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / ".env"
    old = RENAMED["NOTT_SYSTEM1_API_KEY"]
    path.write_text(f"{old}=not-a-live-key")
    path.chmod(0o600)
    monkeypatch.setenv("NOTT_TEST_SYSTEM1_FILE", str(path))
    assert not system1.is_configured()
    assert len(caplog.records) == 1
    assert old in caplog.text
    assert "not-a-live-key" not in caplog.text


@pytest.mark.parametrize("fault", ["mode", "symlink", "hardlink"])
def test_complete_base_process_route_ignores_a_refused_optional_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    path = tmp_path / ".env"
    path.write_text("NOTT_SYSTEM1_API_KEY=file-key")
    path.chmod(0o600)
    if fault == "mode":
        path.chmod(0o644)
    elif fault == "symlink":
        alias = tmp_path / "alias"
        alias.symlink_to(path)
        path = alias
    else:
        os.link(path, tmp_path / "other")
    monkeypatch.setenv("NOTT_TEST_SYSTEM1_FILE", str(path))
    monkeypatch.setenv("NOTT_SYSTEM1_ENDPOINT", "http://127.0.0.1:9/base")
    monkeypatch.setenv("NOTT_SYSTEM1_MODEL", "process-model")
    monkeypatch.setenv("NOTT_SYSTEM1_API_KEY", "process-key")
    assert system1.is_configured(scope="TAINT")
    configured = resolve_system1_config(scope="TAINT")
    assert (configured.endpoint, configured.model, configured.auth_env) == (
        "http://127.0.0.1:9/base",
        "process-model",
        "NOTT_SYSTEM1_API_KEY",
    )


def test_file_route_and_guard_reach_native_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[tuple[str, str, str]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            captured.append(
                (
                    self.path,
                    body["model"],
                    hashlib.sha256(self.headers["Authorization"].encode()).hexdigest(),
                )
            )
            response = b'{"answers":{"taint":{"type":"noul","noul":0.1}}}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, *args: object) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    path = tmp_path / ".env"
    path.write_text(
        f"NOTT_SYSTEM1_ENDPOINT=http://127.0.0.1:{server.server_port}/taint\n"
        "NOTT_SYSTEM1_MODEL=fixture-model\nNOTT_SYSTEM1_API_KEY=fixture-key\n"
    )
    path.chmod(0o600)
    monkeypatch.setenv("NOTT_TEST_SYSTEM1_FILE", str(path))
    try:
        assert system1.is_configured(scope="TAINT")
        assert system1.taint_probe(["A test-owned surface"]) == ([0.1], None)
        assert captured == [
            (
                "/taint",
                "fixture-model",
                hashlib.sha256(b"Bearer fixture-key").hexdigest(),
            )
        ]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
