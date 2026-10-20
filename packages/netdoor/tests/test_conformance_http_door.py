"""Conformance tests for the bearer gate (`admit`) driven by the shared auth table."""

import asyncio
import tempfile
from collections.abc import MutableMapping
from pathlib import Path
from typing import Any

import pytest

from nott_netdoor.http import (
    ApiKey,
    NotLoopbackError,
    UnreadableKeyError,
    admit,
)
from nott_netdoor.http import (
    parse as parse_http,
)

AUTH_TABLE_PATH = Path(__file__).parent.parent / "http-door-auth.tsv"
CONFORMANCE_KEY = b"conformance-key"


def knock(method: str, header: str) -> tuple[int, list[tuple[bytes, bytes]], bool]:
    """One request through `admit`: its status, its headers, and whether the app ran."""
    reached: list[bool] = []
    sent: list[MutableMapping[str, Any]] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        reached.append(True)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"reached"})

    async def receive() -> MutableMapping[str, Any]:
        return {"type": "http.request", "body": b""}

    async def send(message: MutableMapping[str, Any]) -> None:
        sent.append(message)

    headers = [(b"host", b"127.0.0.1")]
    if header:
        headers.append((b"authorization", header.encode("latin-1")))
    scope = {"type": "http", "method": method, "path": "/mcp", "headers": headers}
    asyncio.run(admit(app, ApiKey(CONFORMANCE_KEY))(scope, receive, send))
    return sent[0]["status"], list(sent[0]["headers"]), bool(reached)


def test_the_non_loopback_address_is_refused_in_both_families() -> None:
    with pytest.raises(NotLoopbackError) as exc_v4:
        parse_http("0.0.0.0:4501")
    assert exc_v4.value.addr == ("0.0.0.0", 4501)

    with pytest.raises(NotLoopbackError) as exc_v6:
        parse_http("[::]:4501")
    assert exc_v6.value.addr == ("::", 4501)

    with pytest.raises(NotLoopbackError) as exc_tailnet:
        parse_http("100.64.10.3:4501")
    assert exc_tailnet.value.addr == ("100.64.10.3", 4501)


def test_a_named_loopback_address_is_taken() -> None:
    assert parse_http("127.0.0.1:4501") == ("127.0.0.1", 4501)


def test_api_key_compare_is_byte_exact() -> None:
    with tempfile.NamedTemporaryFile() as f:
        f.write(b"a" * 32)
        f.flush()
        key = ApiKey.load(f.name)
        exact = b"a" * 32
        padded = exact + b"x" * 256
        assert key.matches(exact)
        assert not key.matches(padded)


def test_api_key_load_trims_only_boundary_whitespace() -> None:
    with tempfile.NamedTemporaryFile() as f:
        f.write(b"\n alpha beta \t")
        f.flush()
        key = ApiKey.load(f.name)
        assert key.matches(b"alpha beta")
        assert not key.matches(b"alpha")


def test_keyless_boot_is_refused_when_the_file_is_missing() -> None:
    with pytest.raises(UnreadableKeyError):
        ApiKey.load("/tmp/this-key-file-does-not-exist-and-must-not")


def test_keyless_boot_is_refused_when_the_file_is_empty() -> None:
    with tempfile.NamedTemporaryFile() as f:
        f.write(b"   \n\t  ")
        f.flush()
        with pytest.raises(UnreadableKeyError):
            ApiKey.load(f.name)


def test_all_http_door_auth_tsv_rows() -> None:
    cases = 0
    for line in AUTH_TABLE_PATH.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        case_id, header, status, auth, handler = line.split("\t")
        if case_id == "id":
            continue
        method = "GET" if case_id == "right-key-get" else "POST"
        got_status, got_headers, reached = knock(method, header)
        assert got_status == int(status), case_id
        challenged = (b"www-authenticate", b"Bearer") in got_headers
        assert challenged is (auth == "yes"), case_id
        assert reached is (handler == "reached"), case_id
        cases += 1

    assert cases >= 8, f"a table that drove {cases} cases proves nothing"
