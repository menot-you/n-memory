"""Typed HTTP parsing, binding and key refusals."""

import socket
from pathlib import Path
from unittest.mock import patch

import pytest

from nott_netdoor.http import (
    ApiKey,
    BindError,
    HttpNotAnAddressError,
    NotLoopbackError,
    UnreadableKeyError,
    bind,
)
from nott_netdoor.http import (
    parse as http_parse,
)


def test_netdoor_binderror_on_port_collision() -> None:
    """BindError must raise when bind() refuses the address.

    The OSError path is forced deterministically by stubbing `socket.bind`
    to raise EADDRINUSE; the production code must wrap it into `BindError`.
    """
    fake = OSError(48, "Address already in use")
    with (
        patch("socket.socket.bind", side_effect=fake),
        pytest.raises(BindError) as exc,
    ):
        bind(("127.0.0.1", 0))
    assert exc.value.addr == ("127.0.0.1", 0)


def test_netdoor_binderror_on_an_occupied_port() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder:
        holder.bind(("127.0.0.1", 0))
        holder.listen()
        with pytest.raises(BindError):
            bind(holder.getsockname())


def test_http_bindfault_not_an_address() -> None:
    with pytest.raises(HttpNotAnAddressError):
        http_parse("not-an-ip:8080")


def test_http_bindfault_not_loopback() -> None:
    with pytest.raises(NotLoopbackError):
        http_parse("10.0.0.1:8080")


def test_http_keyfault_unreadable(tmp_path: Path) -> None:
    missing = tmp_path / "non_existent.key"
    with pytest.raises(UnreadableKeyError):
        ApiKey.load(missing)
    with pytest.raises(UnreadableKeyError):
        ApiKey.load(tmp_path)  # a directory, not a file
