"""A file-backed classifier admits a strong route without a vendor launch."""

from __future__ import annotations

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from nspawn import classify, picker, system1
from nspawn.owner_roster import ClassPolicy, Provider, Roster


@pytest.mark.parametrize("origin", ["file", "base_process"])
def test_file_classifier_allows_strength_nine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, origin: str
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
            response = json.dumps(
                {
                    "answers": {
                        name: {
                            "type": "choice",
                            "choice": "planning",
                            "confidence": 0.99,
                        }
                        for name in body["questions"]
                    }
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, *args: object) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = f"http://127.0.0.1:{server.server_port}/classify"
    path = tmp_path / ".env"
    path.write_text(
        f"NOTT_SYSTEM1_ENDPOINT={endpoint}\nNOTT_SYSTEM1_MODEL=fixture-model\n"
        "NOTT_SYSTEM1_API_KEY=fixture-key\n"
    )
    path.chmod(0o600)
    monkeypatch.setenv("NOTT_TEST_SYSTEM1_FILE", str(path))
    if origin == "base_process":
        path.chmod(0o644)
        monkeypatch.setenv("NOTT_SYSTEM1_ENDPOINT", endpoint)
        monkeypatch.setenv("NOTT_SYSTEM1_MODEL", "fixture-model")
        monkeypatch.setenv("NOTT_SYSTEM1_API_KEY", "fixture-key")
    roster = Roster(
        classes={
            "planning": ClassPolicy(effort="high", ceiling="ultra"),
            "unclassified": ClassPolicy(effort="low", ceiling="medium", max_strength=7),
        }
    )
    monkeypatch.setattr(system1, "roster_do_host", lambda: roster)
    provider = Provider(name="codex", kind="codex", strength=9)
    assert not picker._class_admits(provider, roster.classes["unclassified"])
    try:
        effective, warning, observation = classify.resolve_execution_class(
            "planning", "Prepare a plan for the agreed result", roster
        )
        assert effective == "planning"
        assert warning is None
        assert observation is not None and observation.observed == "planning"
        assert picker._class_admits(provider, roster.classes[effective])
        assert captured == [
            (
                "/classify",
                "fixture-model",
                hashlib.sha256(b"Bearer fixture-key").hexdigest(),
            )
        ]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
