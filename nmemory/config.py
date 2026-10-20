"""File configuration for nMEMORY — `~/.nott/memory.toml`.

Port of the nspawn pattern (`nspawn/config.py`): stdlib `tomllib`, no
parser dependency, and `deny_unknown_fields` across all tables — an unrecognized
key causes a refusal at parse time, never silent discarding.

The environment variable names the file PATH (`NOTT_NMEMORY_CONFIG`), never a
value: a path does not age, a value does.

The store is not configured here. It is one SQLite file under
`NOTT_DATABASE_DIR` (`nmemory.database.store_path`). A `[store]` table left by the
retired PostgreSQL store is ignored, not refused, so an old file keeps working.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import stat
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nott_system1 import AUTH_ENV_PADRAO, MODELO_PADRAO, TIMEOUT_PADRAO
from nott_system1 import env as system1_env
from nott_system1 import route as system1_route

from . import NMemoryError

#: Where the file lives when nothing specifies otherwise.
DEFAULT_CONFIG_PATH = Path.home() / ".nott" / "memory.toml"

#: Names the file PATH, never the value.
CONFIG_PATH_ENV = "NOTT_NMEMORY_CONFIG"

#: Byte ceiling for the file. It carries half a dozen keys; anything
#: larger is an error or the wrong file, and reading without a ceiling is how the parser
#: consumes memory from a file written by another process.
MAX_CONFIG_BYTES = 64 * 1024

#: The environment's spelling of the same two values, for hosts without a file.
SYSTEM1_ENDPOINT_ENV = "NOTT_SYSTEM1_ENDPOINT"
SYSTEM1_MODEL_ENV = "NOTT_SYSTEM1_MODEL"

#: `store` is the retired PostgreSQL table: accepted and ignored.
_DOCUMENT_KEYS = frozenset({"store", "system1", "github", "curator"})
_CURATOR_KEYS = frozenset({"provider", "account"})
CURATOR_PROVIDERS = frozenset({"zai", "minimax", "kimi", "codex"})

#: The github-sync fence lives HERE and not in `.nott/nmemory/config.toml`, and
#: the reason is the path: the repository one is relative to the CWD and is only
#: loaded `if exists()`, so a process started in the wrong directory finds no
#: config, gains no fence, and proposes the whole graph in silence. This path is
#: absolute by construction (`Path.home()`) and host-local — each machine decides
#: what it mirrors, without going through the repository.
_GITHUB_KEYS = frozenset({"repo", "label", "project", "efforts"})

#: CREDENTIALS DO NOT LIVE IN THE FILE. `api_key` is not here, so writing it in
#: [system1] is refused by the allowlist — and the refusal names the key, never the value. The
#: key comes from NOTT_SYSTEM1_API_KEY in the environment, read in the transport.
_SYSTEM1_KEYS = frozenset(
    {"curator", "endpoint", "model", "timeout_seconds", "protocol"}
)

#: The table's name before System1 replaced the external Jev service. A file that still
#: carries it is refused by that name, never read as the new one nor dropped as unknown.
_RENAMED_TABLE = "jev"


class ConfigError(NMemoryError):
    """Typed loader refusal. NEVER includes a secret value in the message."""


@dataclass(frozen=True, slots=True)
class CuratorRoute:
    """The declared curator route; native model and effort remain roster policy."""

    provider: str
    account: str | None = None

    def __post_init__(self) -> None:
        if self.provider not in CURATOR_PROVIDERS:
            raise ConfigError("memory.toml: [curator].provider is unsupported")
        if self.provider == "codex":
            if (
                not isinstance(self.account, str)
                or not self.account
                or self.account != self.account.strip()
                or not self.account.isprintable()
            ):
                raise ConfigError(
                    "memory.toml: [curator].account is required for codex"
                )
        elif self.account is not None:
            raise ConfigError("memory.toml: [curator].account requires provider codex")

    @property
    def target(self) -> str:
        """nSPAWN already resolves an exact account alias as a provider selector."""
        return self.account if self.account is not None else self.provider


def config_path() -> Path:
    override = os.environ.get(CONFIG_PATH_ENV)
    return Path(override) if override else DEFAULT_CONFIG_PATH


@dataclass(frozen=True, slots=True)
class System1Config:
    """The EFFECTIVE System1 configuration — already resolved, with no undecided fields.

    The credential is not here and never will be: it comes from the environment
    variable `auth_env` names, read inside the transport.
    """

    endpoint: str
    model: str
    timeout_seconds: float
    #: Whether the resident worker asks System1 the curator question. Off unless
    #: `[system1].curator = true`: the staged suggestions do not depend on it.
    curator: bool = False
    #: The NAME of the variable that holds the key of this route, never the key.
    auth_env: str = AUTH_ENV_PADRAO
    protocol: str = "native"


def _deny_unknown(table: dict[str, Any], allowed: frozenset[str], where: str) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise ConfigError(
            f"memory.toml: unrecognized key in {where}: "
            f"{', '.join(unknown)} (known: {', '.join(sorted(allowed))})"
        )


def _document(path: Path) -> dict[str, Any] | None:
    """The file's TOML with unknown tables already refused, or None when
    the file does not exist.

    A file that exists but is invalid raises — it never returns None, because None
    falls back to the environment, and the environment is the source this module exists to avoid.
    """
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ConfigError(f"memory.toml: {path} unreadable: {error}") from error

    return _parse_document(path, data)


def _parse_document(path: Path, data: bytes) -> dict[str, Any]:

    if len(data) > MAX_CONFIG_BYTES:
        raise ConfigError(
            f"memory.toml: {path} has {len(data)} bytes, above the ceiling of "
            f"{MAX_CONFIG_BYTES}"
        )

    try:
        raw = tomllib.loads(data.decode("utf-8"))
    except UnicodeDecodeError as error:
        raise ConfigError(f"memory.toml: {path} is not UTF-8: {error}") from error
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"memory.toml: {path} failed to parse: {error}") from error

    if _RENAMED_TABLE in raw:
        raise ConfigError(
            f"memory.toml: {path}: [{_RENAMED_TABLE}] was renamed to [system1]; "
            "rename the table"
        )
    _deny_unknown(raw, _DOCUMENT_KEYS, "the document")
    return raw


def _system1_from_file(path: Path) -> dict[str, Any]:
    """The validated [system1] table, or empty when there is no file or table.

    Returns only what the file STATES: an absent field remains absent, so that env
    and factory defaults still have a turn in `resolve_system1_config`.
    """
    return _system1_values(path, _document(path) or {})


def _system1_values(path: Path, raw: dict[str, Any]) -> dict[str, Any]:
    system1 = (raw or {}).get("system1")
    if system1 is None:
        return {}
    if not isinstance(system1, dict):
        raise ConfigError(f"memory.toml: {path}: [system1] is not a table")
    _deny_unknown(system1, _SYSTEM1_KEYS, "[system1]")

    dito: dict[str, Any] = {}
    protocol = system1.get("protocol", "native")
    if protocol not in ("native", "openai-compatible"):
        raise ConfigError("memory.toml: [system1].protocol is unsupported")
    dito["protocol"] = protocol
    for chave in ("endpoint", "model"):
        valor = system1.get(chave)
        if valor is None:
            continue
        if not isinstance(valor, str):
            raise ConfigError(
                f"memory.toml: {path}: [system1].{chave} is {type(valor).__name__}, "
                "expected string"
            )
        if valor.strip():
            dito[chave] = valor.strip()

    timeout = system1.get("timeout_seconds")
    if timeout is not None:
        if not isinstance(timeout, int | float) or isinstance(timeout, bool):
            raise ConfigError(
                f"memory.toml: {path}: [system1].timeout_seconds is "
                f"{type(timeout).__name__}, expected number"
            )
        dito["timeout_seconds"] = float(timeout)

    curator = system1.get("curator")
    if curator is not None:
        if not isinstance(curator, bool):
            raise ConfigError(
                f"memory.toml: {path}: [system1].curator is "
                f"{type(curator).__name__}, expected boolean"
            )
        dito["curator"] = curator
    return dito


def github_table(path: Path | None = None) -> dict[str, Any]:
    """The validated [github] table, or empty when there is neither file nor table.

    Returns only what the file SAYS — a missing field stays missing, so the
    CLI flags still get their turn. A file that exists and carries a wrong
    key is refused; there is no silent discard.
    """
    if path is None:
        path = config_path()
    return _github_values(path, _document(path) or {})


def _github_values(alvo: Path, raw: dict[str, Any]) -> dict[str, Any]:
    github = (raw or {}).get("github")
    if github is None:
        return {}
    if not isinstance(github, dict):
        raise ConfigError(f"memory.toml: {alvo}: [github] is not a table")
    _deny_unknown(github, _GITHUB_KEYS, "[github]")

    dito: dict[str, Any] = {}
    for chave in ("repo", "label", "project"):
        valor = github.get(chave)
        if valor is None:
            continue
        if not isinstance(valor, str):
            raise ConfigError(
                f"memory.toml: {alvo}: [github].{chave} e {type(valor).__name__}, "
                "esperava string"
            )
        if valor.strip():
            dito[chave] = valor.strip()

    if "efforts" in github:
        dito["efforts"] = github["efforts"]
    return dito


def resolve_system1_config(
    path: Path | None = None, scope: str | None = None
) -> System1Config:
    """The effective System1 configuration: file overrides env, library provides the rest.

    A question with its own route takes
    its endpoint, model and key from that route, over the file and the base variables.

    Precedence is the REVERSE of nSPAWN, deliberately: on this machine the VALUE
    variable already proved to lag behind the corrected file (the incident documented in
    this module's header), so it serves as compatibility for hosts without a file, and
    not as authority. A missing file or a file without [system1] is not a defect — only INVALID
    configuration raises.

    There is no default endpoint: an unconfigured one stays empty, and the transport
    answers it with an operational `System1Unavailable`.

    `path` exists to let tests point to a temporary file; None resolves via the
    same path as the rest of the module.
    """
    if path is None:
        path = config_path()
    dito = _system1_from_file(path)
    if scope is not None and dito.get("protocol") != "openai-compatible":
        propria = system1_route(scope)
        if propria.auth_env != AUTH_ENV_PADRAO:
            return System1Config(
                endpoint=propria.endpoint or "",
                model=propria.model or MODELO_PADRAO,
                timeout_seconds=dito.get("timeout_seconds") or TIMEOUT_PADRAO,
                curator=dito.get("curator", False),
                auth_env=propria.auth_env,
            )
    return System1Config(
        endpoint=dito.get("endpoint")
        or (system1_env(SYSTEM1_ENDPOINT_ENV) or "").strip(),
        model=dito.get("model")
        or (system1_env(SYSTEM1_MODEL_ENV) or "").strip()
        or MODELO_PADRAO,
        timeout_seconds=dito.get("timeout_seconds") or TIMEOUT_PADRAO,
        curator=dito.get("curator", False),
        protocol=dito.get("protocol", "native"),
    )


def curator_route(path: Path | None = None) -> CuratorRoute | None:
    """An absent table permits compatibility env; an invalid one never falls back."""
    target = config_path() if path is None else path
    return _curator_values(target, _document(target) or {})


def _curator_values(path: Path, raw: dict[str, Any]) -> CuratorRoute | None:
    if "curator" not in raw:
        return None
    table = raw["curator"]
    if not isinstance(table, dict):
        raise ConfigError(f"memory.toml: {path}: [curator] is not a table")
    _deny_unknown(table, _CURATOR_KEYS, "[curator]")
    provider = table.get("provider")
    account = table.get("account")
    if not isinstance(provider, str) or (
        account is not None and not isinstance(account, str)
    ):
        raise ConfigError("memory.toml: [curator] requires string provider/account")
    return CuratorRoute(provider, account)


def _validate_document(path: Path, data: bytes) -> dict[str, Any]:
    document = _parse_document(path, data)
    _system1_values(path, document)
    _github_values(path, document)
    _curator_values(path, document)
    return document


def _read_for_write(path: Path) -> tuple[bytes, os.stat_result | None]:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return b"", None
    except OSError:
        raise ConfigError(
            "memory.toml: configuration cannot be opened safely"
        ) from None
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_mode & 0o022
            or before.st_nlink != 1
            or before.st_size > MAX_CONFIG_BYTES
        ):
            raise ConfigError("memory.toml: configuration must be owner-controlled")
        data = stream.read(MAX_CONFIG_BYTES + 1)
        if _file_identity(os.fstat(stream.fileno())) != _file_identity(before):
            raise ConfigError("memory.toml: configuration changed during read")
    return data, before


def _file_identity(metadata: os.stat_result | None) -> tuple[int, ...] | None:
    if metadata is None:
        return None
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _replace_curator(text: str, route: CuratorRoute | None) -> str:
    lines = text.splitlines(keepends=True)
    headers = [
        index
        for index, line in enumerate(lines)
        if re.fullmatch(
            r"\s*\[\s*(?:curator|\"curator\"|'curator')\s*\]\s*(?:#.*)?", line.strip()
        )
    ]
    if len(headers) > 1:
        raise ConfigError("memory.toml: curator table cannot be replaced safely")
    replacement = ""
    if route is not None:
        replacement = "[curator]\nprovider = " + json.dumps(route.provider) + "\n"
        if route.account is not None:
            replacement += "account = " + json.dumps(route.account) + "\n"
    if not headers:
        if replacement and text and not text.endswith("\n"):
            return text + "\n" + replacement
        return text + replacement
    start = headers[0]
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith("[")),
        len(lines),
    )
    return "".join(lines[:start]) + replacement + "".join(lines[end:])


def set_curator_route(route: CuratorRoute | None, path: Path | None = None) -> None:
    """Atomically replace only the owned table under a lock and current-input fence."""
    target = config_path() if path is None else path
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent = target.parent.stat()
    if parent.st_uid != os.geteuid() or parent.st_mode & 0o022:
        raise ConfigError(
            "memory.toml: configuration directory must be owner-controlled"
        )
    lock = target.with_name(f".{target.name}.curator.lock")
    descriptor = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "rb+") as owner:
        metadata = os.fstat(owner.fileno())
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o077
            or metadata.st_nlink != 1
        ):
            raise ConfigError(
                "memory.toml: curator lock must be private and owner-held"
            )
        fcntl.flock(owner.fileno(), fcntl.LOCK_EX)
        raw, before = _read_for_write(target)
        original = _validate_document(target, raw)
        changed = _replace_curator(raw.decode("utf-8"), route).encode("utf-8")
        updated = _validate_document(target, changed)
        if {k: v for k, v in original.items() if k != "curator"} != {
            k: v for k, v in updated.items() if k != "curator"
        } or _curator_values(target, updated) != route:
            raise ConfigError(
                "memory.toml: curator edit would change foreign configuration"
            )
        if changed == raw:
            return
        descriptor, name = tempfile.mkstemp(
            prefix=f".{target.name}.curator-", dir=target.parent
        )
        staged = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                os.fchmod(
                    stream.fileno(), stat.S_IMODE(before.st_mode) if before else 0o600
                )
                stream.write(changed)
                stream.flush()
                os.fsync(stream.fileno())
            current, identity = _read_for_write(target)
            if current != raw or _file_identity(identity) != _file_identity(before):
                raise ConfigError(
                    "memory.toml: configuration changed before replacement"
                )
            os.replace(staged, target)
        finally:
            staged.unlink(missing_ok=True)
