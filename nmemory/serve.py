"""`nmemory mcp serve --http`: MCP Streamable HTTP on a loopback door.

One process answers every HTTP client; the official MCP Python SDK owns the
sessions and the protocol. Remote reach is a reverse proxy's job (cloudflared,
`tailscale serve`); the door itself refuses anything but loopback and any
request without the bearer key.
"""

from __future__ import annotations

import os
import secrets
import stat
import string
import sys
from pathlib import Path

from mcp.server.transport_security import TransportSecuritySettings
from nott_netdoor.http import ApiKey, bind, parse, serve_app

from .server import MemoryServer, build

DEFAULT_LISTEN = "127.0.0.1:16699"
MAX_BODY = 8 * 1024 * 1024


class KeyPermissionError(Exception):
    """The key file can be read by someone other than its owner."""

    def __init__(self, path: Path, mode: int) -> None:
        super().__init__(
            f"{path} has mode {mode:04o}; the key must be 0600 (owner only)"
        )


def default_key_path() -> Path:
    return Path.home() / ".nott" / "secrets" / "memory.key"


KEY_PREFIX = "nott_mem_"
_BASE62 = string.digits + string.ascii_letters


def new_key() -> str:
    """`nott_mem_` + 32 base62 characters (190 bits): a prefix scanners and
    logs can recognize, and one word a double-click selects whole."""
    return KEY_PREFIX + "".join(secrets.choice(_BASE62) for _ in range(32))


def load_or_create_key(path: Path) -> ApiKey:
    """Load the key, creating it on first run; a key others can read is refused."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, "w") as f:
            f.write(new_key() + "\n")
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode != 0o600:
        raise KeyPermissionError(path, mode)
    return ApiKey.load(path)


def run_http_server(server: MemoryServer, listen: str, key_file: Path) -> int:
    try:
        key = load_or_create_key(key_file)
    except KeyPermissionError as exc:
        print(f"nmemory: {exc}", file=sys.stderr)
        return 2
    listener = bind(parse(listen))
    host, port = listener.getsockname()[:2]
    print(f"nmemory: serving http://{host}:{port}/mcp", file=sys.stderr, flush=True)
    app = build(server, http=True).streamable_http_app(
        json_response=True,
        max_request_body_size=MAX_BODY,
        # The Host check is off: a reverse proxy forwards the public name, and
        # the bearer is what admits.
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        ),
    )
    serve_app(app, listener, key)
    return 0
