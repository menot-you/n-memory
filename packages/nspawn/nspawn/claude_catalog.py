"""Read Claude model metadata through the native control protocol."""

from __future__ import annotations

import functools
import json
import os
import select
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from . import claude_child
from .errors import NERR_2002, NSpawnError


class ModelCatalogError(NSpawnError):
    """The native client did not supply a usable model catalogue."""

    def __init__(self, detail: str) -> None:
        super().__init__(NERR_2002, detail)


def catalog_read(binary: str | Path | None) -> dict[str, str]:
    """Read native model metadata without a prompt or a credential."""
    if binary is None or not Path(binary).is_absolute():
        raise ModelCatalogError("Claude model catalogue needs a declared binary")
    try:
        path = str(Path(binary).resolve())
        metadata = os.stat(path)
        if not claude_child.native_binary_allowed(Path(path), metadata):
            raise ModelCatalogError(
                "Claude catalogue binary must be an owner-controlled executable"
            )
        identity = claude_child.binary_identity(path)
    except (OSError, RuntimeError, claude_child.BinaryIdentityError) as error:
        raise ModelCatalogError(str(error)) from error
    return dict(_catalog_read(path, identity))


@functools.lru_cache(maxsize=16)
def _catalog_read(
    binary: str, identity: claude_child.BinaryIdentity
) -> tuple[tuple[str, str], ...]:
    with tempfile.TemporaryDirectory(prefix="nott-catalog-", dir="/var/tmp") as home:
        environment = {
            "HOME": home,
            "CLAUDE_CONFIG_DIR": home,
            "TMPDIR": home,
            "PATH": "/usr/bin:/bin",
            "DISABLE_AUTOUPDATER": "1",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        }
        command = [
            binary,
            "--print",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--verbose",
            "--no-session-persistence",
            "--setting-sources",
            "",
            "--strict-mcp-config",
            "--mcp-config",
            '{"mcpServers":{}}',
            "--tools",
            "",
        ]
        if Path(binary).suffix in {".js", ".mjs"}:
            runtime = shutil.which("node")
            if runtime is None:
                raise ModelCatalogError(
                    "Claude catalogue requires the installed node runtime"
                )
            command = [runtime, *command]
            environment["PATH"] = (
                f"{Path(runtime).parent}:{Path(binary).parent}:/usr/bin:/bin"
            )
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=environment,
                bufsize=0,
            )
        except OSError:
            raise ModelCatalogError("Claude model catalogue could not start") from None
        try:
            assert process.stdin is not None and process.stdout is not None
            try:
                process.stdin.write(
                    b'{"type":"control_request","request_id":"nott-catalog","request":{"subtype":"initialize"}}\n'
                )
                process.stdin.close()
            except OSError:
                raise ModelCatalogError("Claude model catalogue input failed") from None
            deadline = time.monotonic() + claude_child.VERSION_TIMEOUT_SECONDS
            pending = b""
            received = 0
            while time.monotonic() < deadline:
                ready, _, _ = select.select(
                    [process.stdout], [], [], max(0, deadline - time.monotonic())
                )
                if not ready:
                    break
                block = os.read(process.stdout.fileno(), 65536)
                if not block:
                    break
                received += len(block)
                if received > 1024 * 1024:
                    raise ModelCatalogError(
                        "Claude model catalogue exceeds the byte limit"
                    )
                pending += block
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    try:
                        frame = json.loads(line)
                    except ValueError:
                        raise ModelCatalogError(
                            "Claude model catalogue is not JSON"
                        ) from None
                    if (
                        not isinstance(frame, dict)
                        or frame.get("type") != "control_response"
                    ):
                        continue
                    response = frame.get("response")
                    if (
                        not isinstance(response, dict)
                        or response.get("request_id") != "nott-catalog"
                    ):
                        continue
                    body = response.get("response")
                    rows = body.get("models") if isinstance(body, dict) else None
                    if not isinstance(rows, list):
                        raise ModelCatalogError(
                            "Claude model catalogue did not return models"
                        )
                    models: dict[str, str] = {}
                    for row in rows:
                        if not isinstance(row, dict) or not isinstance(
                            row.get("value"), str
                        ):
                            raise ModelCatalogError(
                                "Claude model catalogue has an invalid entry"
                            )
                        value = row["value"]
                        resolved = row.get("resolvedModel")
                        if resolved is None and value.startswith("claude-"):
                            resolved = value
                        if isinstance(resolved, str) and resolved:
                            models[value] = resolved
                    try:
                        current = claude_child.binary_identity(binary)
                    except claude_child.BinaryIdentityError as error:
                        raise ModelCatalogError(str(error)) from error
                    if current != identity:
                        raise ModelCatalogError(
                            "Claude binary changed during the catalogue read"
                        )
                    return tuple(models.items())
            raise ModelCatalogError("Claude model catalogue did not answer")
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
            if process.stdin is not None:
                process.stdin.close()
            if process.stdout is not None:
                process.stdout.close()
