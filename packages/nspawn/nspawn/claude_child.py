"""Claude child installation: binary resolution, version readback, the child MCP
config, and the flag door (the argv itself is `child.compose_child_argv`).

One `claude -p` invocation is exactly one turn — one `system/init` … one
`result` (census K4) — so where Codex holds one long-lived app-server
child, the Claude backend spawns one guarded child per turn and the
supervisor keeps the durable session id as the cross-turn thread handle.

Translate the contract: `capabilities/nspawn/src/claude_child.rs` @ `d8e0be5ff`.
"""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Version model
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Version:
    major: int
    minor: int
    patch: int

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


_VERSION_RE = re.compile(r"^\s*(\d+)\.(\d+)\.(\d+)\s*(?:\(.*\))?\s*$")


def parse_version(raw: str) -> Version | None:
    """Parse the output of `claude --version` — measured on v2.1.220 as
    `2.1.220 (Claude Code)`. Returns None on any malformed line.
    """
    if raw is None:
        return None
    stripped = raw.strip()
    match = _VERSION_RE.match(stripped)
    if match is None:
        return None
    return Version(
        major=int(match.group(1)), minor=int(match.group(2)), patch=int(match.group(3))
    )


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ClaudeChildError(Exception):
    """Base for Claude child failures."""


class BinaryIdentityError(ClaudeChildError):
    """The claude binary path cannot be identified."""

    def __init__(self, path: str, detail: str):
        self.path = path
        self.detail = detail
        super().__init__(f"the claude binary at {path} cannot be identified: {detail}")


class NoDeclaredBinary(ClaudeChildError):
    """No usable declaration names the Claude binary.

    Two shapes, one refusal: every source (explicit, environment override,
    provider binary, roster default) is absent; or the winning declaration is
    relative, so the child would resolve it against ambient PATH or cwd —
    ambient state, not a declaration. nSPAWN never resolves `claude` from
    ambient PATH.
    """

    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(f"no declared claude binary: {detail}")


class ChildMcpConfigError(ClaudeChildError):
    """The plugin-owned child MCP config cannot be verified.

    One typed refusal for the whole L8 config surface: the file is absent,
    unreadable, oversized, a symlink, or not a regular file; the document is
    malformed; a required server is missing; or an env entry persists a
    literal value, meaning the config only works with credential bytes
    written into it.
    """

    def __init__(self, path: Path, reason: str):
        self.path = path
        self.reason = reason
        super().__init__(f"child MCP config at {path} is refused: {reason}")


# ---------------------------------------------------------------------------
# BinaryIdentity: stat the file at probe-time, never trust across turns
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BinaryIdentity:
    device: int
    inode: int
    size: int
    modified_ns: int


def native_binary_allowed(path: Path, metadata: os.stat_result) -> bool:
    """The shared executable, ownership and write-permission check."""
    return (
        stat.S_ISREG(metadata.st_mode)
        and os.access(path, os.X_OK)
        and metadata.st_uid in (0, os.geteuid())
        and not metadata.st_mode & 0o022
    )


def binary_identity(path: str) -> BinaryIdentity:
    """Stat the binary so we can detect mid-lifetime replacement.
    This is a CORRECTNESS check against replacement, not a security boundary.
    Same-uid code is outside the threat model either way.
    """
    try:
        st = os.stat(path)
    except OSError as exc:
        raise BinaryIdentityError(path, str(exc))
    # Mirror Rust's i128 modification-time math.
    modified_ns = int(st.st_mtime) * 1_000_000_000 + int(st.st_mtime_ns)
    return BinaryIdentity(
        device=st.st_dev,
        inode=st.st_ino,
        size=st.st_size,
        modified_ns=modified_ns,
    )


# The probe timeout is set to 10 seconds in the contract (`VERSION_TIMEOUT` in
# `claude_child.rs`). The constant lives here, not in `child.rs`, because
# the gate decision is gated to this surface and not to a general "probe
# anything" runner. §43 of the campaign journal: any tuned constant carries
# its OWN assertion against the number from the contract — not the existence of
# the constant, but the value.
VERSION_TIMEOUT_SECONDS: int = 10


#: The probe is injected to keep this module free of subprocess concerns: it
#: takes the binary path and returns the parsed Version — exactly what
#: `parse_version` produces from `claude --version` output.
type Probe = Callable[[str], Version]


def check_claude_version(binary: str, probe: Probe | None = None) -> Version:
    """Probe `claude --version` and return it as observed.

    When `probe` is `None`, the seam raises NotImplementedError — the runner
    is not wired in this module. When `probe` is supplied, the
    runner is bypassed and its version returned as observed. nSPAWN NEVER
    judges the installed version (owner law, 2026-09-01): it uses what is
    installed. A real wire break surfaces as a typed terminal classification
    error against the observed records, never as silent success.
    """
    if probe is None:
        raise NotImplementedError(
            "Spawn-d2 lane owns the probe; tests inject a callable to drive "
            "this seam. See README.md, check_claude_version, for the wiring contract."
        )
    return probe(binary)


@dataclass(frozen=True, slots=True)
class ClaudeInstallation:
    """One declared binary after identity and version readback."""

    binary: Path
    identity: BinaryIdentity
    version: Version


@dataclass(frozen=True, slots=True)
class ChildMcpConfig:
    """A plugin-owned MCP config identified without copying credential values."""

    plugin_dir: Path
    server_names: tuple[str, ...]
    #: The ambient variables the config REFERENCES, never their values. Every
    #: one was proved a pure ``${NAME}`` reference above, so this is the exact
    #: set the child needs carried in — an allowlist derived from what the
    #: config declares, never a passthrough of the parent environment.
    env_names: tuple[str, ...] = ()


def resolve_claude_binary(
    *,
    explicit: Path | None = None,
    environment_override: Path | None = None,
    provider_binary: Path | None = None,
    roster_default: Path | None = None,
) -> Path:
    """Apply explicit → environment → provider → roster; never guess PATH."""
    for source, candidate in (
        ("explicit", explicit),
        ("environment override", environment_override),
        ("provider binary", provider_binary),
        ("roster default", roster_default),
    ):
        if candidate is None:
            continue
        if not candidate.is_absolute():
            raise NoDeclaredBinary(
                f"the {source} declaration {candidate} is relative, so the "
                "child would resolve it against ambient PATH or cwd"
            )
        return candidate
    raise NoDeclaredBinary(
        "explicit, environment override, provider binary, and roster default "
        "are all absent; nSPAWN never resolves `claude` from ambient PATH"
    )


def inspect_claude_installation(
    binary: Path,
    probe: Probe,
) -> ClaudeInstallation:
    """Bind the resolved path to its current identity and supported version."""
    try:
        metadata = os.stat(binary)
    except OSError as error:
        raise BinaryIdentityError(str(binary), str(error)) from error
    if not stat.S_ISREG(metadata.st_mode):
        raise BinaryIdentityError(str(binary), "is not a regular file")
    before = binary_identity(str(binary))
    version = check_claude_version(str(binary), probe)
    after = binary_identity(str(binary))
    if before != after:
        raise BinaryIdentityError(
            str(binary), "identity changed while the version was probed"
        )
    return ClaudeInstallation(binary=binary, identity=before, version=version)


#: The plugin-owned child MCP config file Claude loads from `--plugin-dir`.
MCP_CONFIG_FILE = ".mcp.json"
#: A config above this size is refused unread: the read is a precondition of
#: spawning a child, and an unbounded read is a denial of service on the
#: broker itself.
MAX_MCP_CONFIG_BYTES = 64 * 1024
#: One pure environment reference, `${NAME}`: the config names a variable the
#: child's own environment supplies. Anything else — literal credential
#: bytes, a prefix, a suffix — means the config only works with secret bytes
#: persisted into it.
_ENV_REFERENCE_RE = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*\}$")
_MCP_SERVERS_KEY = "mcpServers"


def inspect_child_mcp_config(
    plugin_dir: Path,
    *,
    required_servers: tuple[str, ...] = ("nmemory",),
) -> ChildMcpConfig:
    """Verify the child plugin config names required servers without secret bytes."""
    servers = read_child_mcp_config(plugin_dir, required_servers=required_servers)
    return ChildMcpConfig(
        plugin_dir=plugin_dir,
        server_names=tuple(servers),
        env_names=tuple(
            dict.fromkeys(
                variable
                for declaration in servers.values()
                for variable in declaration.get("env", {})
            )
        ),
    )


def read_child_mcp_config(
    plugin_dir: Path,
    *,
    required_servers: tuple[str, ...] = ("nmemory",),
) -> dict[str, Any]:
    """Read the bounded, validated declarations shared by vendor launchers."""
    config_path = plugin_dir / MCP_CONFIG_FILE
    try:
        metadata = os.lstat(config_path)
    except OSError as error:
        raise ChildMcpConfigError(
            config_path, f"cannot be inspected ({error})"
        ) from error
    if stat.S_ISLNK(metadata.st_mode):
        raise ChildMcpConfigError(
            config_path,
            "is a symlink, so what the child loads is not what this inspection read",
        )
    if not stat.S_ISREG(metadata.st_mode):
        raise ChildMcpConfigError(config_path, "is not a regular file")
    try:
        with open(config_path, "rb") as handle:
            document_bytes = handle.read(MAX_MCP_CONFIG_BYTES + 1)
    except OSError as error:
        raise ChildMcpConfigError(config_path, f"cannot be read ({error})") from error
    if len(document_bytes) > MAX_MCP_CONFIG_BYTES:
        raise ChildMcpConfigError(
            config_path, f"exceeds the {MAX_MCP_CONFIG_BYTES}-byte read cap"
        )
    try:
        document = json.loads(document_bytes)
    except (ValueError, UnicodeDecodeError, RecursionError) as error:
        raise ChildMcpConfigError(
            config_path, f"is not parseable JSON ({error})"
        ) from error
    if not isinstance(document, dict):
        raise ChildMcpConfigError(config_path, "is not a JSON object")
    servers = document.get(_MCP_SERVERS_KEY)
    if not isinstance(servers, dict):
        raise ChildMcpConfigError(config_path, f'has no "{_MCP_SERVERS_KEY}" object')
    for required in required_servers:
        if required not in servers:
            raise ChildMcpConfigError(
                config_path, f'does not declare the required server "{required}"'
            )
    for name, declaration in servers.items():
        if not isinstance(declaration, dict):
            raise ChildMcpConfigError(
                config_path, f'declares server "{name}" as a non-object'
            )
        env = declaration.get("env", {})
        if not isinstance(env, dict):
            raise ChildMcpConfigError(
                config_path, f'declares a non-object env for server "{name}"'
            )
        for variable, value in env.items():
            if not isinstance(value, str):
                raise ChildMcpConfigError(
                    config_path,
                    f'declares a non-textual env value for server "{name}"',
                )
            if _ENV_REFERENCE_RE.match(value) is None:
                raise ChildMcpConfigError(
                    config_path,
                    f'persists a literal value for the "{name}" env entry '
                    f'"{variable}"; the child config must carry environment '
                    "references, never credential bytes",
                )
    return servers


# ---------------------------------------------------------------------------
# Owner boot and the flag door
# ---------------------------------------------------------------------------

SETTING_SOURCES = "user"

# The flag door: a vendor flag a Claude child MUST NEVER receive.
FORBIDDEN_CLAUDE_FLAGS: tuple[str, ...] = (
    "--dangerously-skip-permissions",
    "--permission-mode",
    "--add-dir",
    "--mcp-config",
)


# ---------------------------------------------------------------------------
# mint_session_id: RFC 4122 v4 UUID from /dev/urandom
# ---------------------------------------------------------------------------


def mint_session_id() -> str:
    """Mint a version-4 UUID before any vendor effect so crash recovery always
    knows which id to probe (`--session-id` is single-use; the "already in use"
    refusal is the proof a prior spawn reached the CLI).
    """
    with open("/dev/urandom", "rb") as f:
        bytes_ = bytearray(f.read(16))
    if len(bytes_) != 16:
        raise OSError("could not read 16 bytes from /dev/urandom")
    # Force version 4
    bytes_[6] = (bytes_[6] & 0x0F) | 0x40
    # Force RFC 4122 variant
    bytes_[8] = (bytes_[8] & 0x3F) | 0x80
    hex_ = bytes(bytes_).hex()
    return f"{hex_[0:8]}-{hex_[8:12]}-{hex_[12:16]}-{hex_[16:20]}-{hex_[20:32]}"
