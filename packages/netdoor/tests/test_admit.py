"""The bearer in front of an ASGI app, and the loopback-only bind."""

import asyncio
from collections.abc import MutableMapping
from typing import Any

import pytest

from nott_netdoor import http
from nott_netdoor.http import ApiKey, NotLoopbackError, admit, bind

KEY = ApiKey(b"k-0123456789")


def _answer(headers: list[tuple[bytes, bytes]]) -> tuple[int, bool]:
    """The status `admit` answers with, and whether the app was reached."""
    reached: list[bool] = []
    sent: list[MutableMapping[str, Any]] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        reached.append(True)
        await send({"type": "http.response.start", "status": 200, "headers": []})

    async def receive() -> MutableMapping[str, Any]:
        return {"type": "http.request", "body": b""}

    async def send(message: MutableMapping[str, Any]) -> None:
        sent.append(message)

    scope = {"type": "http", "headers": headers}
    asyncio.run(admit(app, KEY)(scope, receive, send))
    return sent[0]["status"], bool(reached)


@pytest.mark.parametrize(
    "headers",
    [
        [],
        [(b"authorization", b"Bearer wrong")],
        [(b"authorization", b"Basic k-0123456789")],
        [(b"authorization", b"k-0123456789")],
        [
            (b"authorization", b"Bearer k-0123456789"),
            (b"authorization", b"Bearer k-0123456789"),
        ],
    ],
    ids=["absent", "wrong", "basic", "bare", "twice"],
)
def test_a_request_without_exactly_the_key_is_refused_before_the_app(
    headers: list[tuple[bytes, bytes]],
) -> None:
    assert _answer(headers) == (401, False)


def test_the_key_reaches_the_app() -> None:
    assert _answer([(b"Authorization", b"Bearer k-0123456789")]) == (200, True)


def test_bind_refuses_a_public_address() -> None:
    with pytest.raises(NotLoopbackError):
        bind(("0.0.0.0", 0))


def test_bind_listens_on_loopback() -> None:
    with bind(("127.0.0.1", 0)) as listener:
        assert listener.getsockname()[0] == "127.0.0.1"


def _reached(scope: dict[str, Any]) -> tuple[bool, list[MutableMapping[str, Any]]]:
    reached: list[bool] = []
    sent: list[MutableMapping[str, Any]] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        reached.append(True)

    async def receive() -> MutableMapping[str, Any]:
        return {"type": "websocket.connect"}

    async def send(message: MutableMapping[str, Any]) -> None:
        sent.append(message)

    asyncio.run(admit(app, KEY)(scope, receive, send))
    return bool(reached), sent


def test_a_websocket_without_the_key_is_closed_before_the_app() -> None:
    """Only `http` scopes were checked: a WebSocket upgrade without a bearer went
    straight to the app."""
    reached, sent = _reached({"type": "websocket", "headers": []})
    assert not reached
    assert sent == [{"type": "websocket.close", "code": 1008}]


def test_a_websocket_with_the_key_reaches_the_app() -> None:
    scope = {
        "type": "websocket",
        "headers": [(b"authorization", b"Bearer k-0123456789")],
    }
    assert _reached(scope) == (True, [])


def test_lifespan_reaches_the_app_and_an_unknown_scope_does_not() -> None:
    assert _reached({"type": "lifespan"}) == (True, [])
    assert _reached({"type": "other", "headers": []}) == (False, [])


def test_the_server_bounds_its_connections(monkeypatch: pytest.MonkeyPatch) -> None:
    """uvicorn ran with no concurrency limit and no keep-alive bound."""
    configs: list[Any] = []

    class Server:
        def __init__(self, config: Any) -> None:
            configs.append(config)

        def run(self, sockets: Any) -> None:
            pass

    monkeypatch.setattr(http.uvicorn, "Server", Server)
    with bind(("127.0.0.1", 0)) as listener:
        http.serve_app(lambda *args: None, listener, KEY)  # type: ignore[arg-type,return-value]
    assert configs[0].limit_concurrency == http.LIMIT_CONCURRENCY
    assert configs[0].timeout_keep_alive == http.KEEP_ALIVE_S
