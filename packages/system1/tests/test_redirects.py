"""Authenticated requests must stop at the configured endpoint."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from nott_system1 import Answer, System1Unavailable, ask

_KEY = "synthetic-system1-key"
_AUTH_ENV = "AUDIT_SYSTEM1_KEY"
_QUESTIONS = {"q": {"type": "noul", "instructions": "Is this test data?"}}


@pytest.fixture(autouse=True)
def _local_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_AUTH_ENV, _KEY)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")


@contextmanager
def _server(
    status: int = 200, location: str | None = None
) -> Iterator[tuple[str, list[tuple[str, str | None, bytes]]]]:
    received: list[tuple[str, str | None, bytes]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self._answer()

        def do_GET(self) -> None:
            self._answer()

        def _answer(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            received.append((self.command, self.headers.get("Authorization"), body))
            code = status if self.path == "/v1/systemone" else 200
            response = b'{"answers":{"q":{"type":"noul","noul":0.75}}}'
            self.send_response(code)
            if location is not None:
                self.send_header("Location", location)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, format: str, *args: object) -> None:
            pass

    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/v1/systemone", received
        finally:
            server.shutdown()
            thread.join(timeout=2)
            assert not thread.is_alive(), "The local HTTP server did not stop."


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize(
    "same_origin", [False, True], ids=["cross-origin", "same-origin"]
)
def test_authenticated_redirects_are_refused(status: int, same_origin: bool) -> None:
    with _server() as (destination, redirected):
        location = "/destination" if same_origin else destination
        with _server(status, location) as (endpoint, initial):
            failure = None
            try:
                ask(
                    "disposable state",
                    _QUESTIONS,
                    endpoint=endpoint,
                    model="fixture",
                    timeout=1.0,
                    auth_env=_AUTH_ENV,
                )
            except System1Unavailable as error:
                failure = error

            assert redirected == [], "The redirect destination received a request."
            assert len(initial) == 1, "The endpoint followed its own redirect."
            assert initial[0][0:2] == ("POST", f"Bearer {_KEY}")
            assert failure is not None, "System1 accepted a redirect response."
            assert str(status) in str(failure)
            assert _KEY not in str(failure)


@pytest.mark.parametrize("status", [200, 429, 503])
def test_direct_responses_keep_answers_and_typed_failures(status: int) -> None:
    with _server(status) as (endpoint, received):
        if status == 200:
            answers = ask(
                "disposable state",
                _QUESTIONS,
                endpoint=endpoint,
                model="fixture",
                timeout=1.0,
                auth_env=_AUTH_ENV,
            )
            assert answers == {"q": Answer(id="q", type="noul", value=0.75)}
        else:
            with pytest.raises(System1Unavailable, match=str(status)) as failure:
                ask(
                    "disposable state",
                    _QUESTIONS,
                    endpoint=endpoint,
                    model="fixture",
                    timeout=1.0,
                    auth_env=_AUTH_ENV,
                )
            assert _KEY not in str(failure.value)
            if status == 429:
                assert "retry with backoff" in str(failure.value)

        assert len(received) == 1
        method, authorization, body = received[0]
        assert (method, authorization) == ("POST", f"Bearer {_KEY}")
        assert json.loads(body) == {
            "state": "disposable state",
            "model": "fixture",
            "questions": _QUESTIONS,
        }
        assert _KEY.encode() not in body
