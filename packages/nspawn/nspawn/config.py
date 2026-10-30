"""The `spawn.toml` path and the `[store]` table nSPAWN reads.

`tomllib` is stdlib: no parser dependency is added. An unrecognised key in a
table read strictly is a refusal, never a silent drop.
"""

import os
import stat
import tomllib
from pathlib import Path
from typing import Any

from .errors import NSpawnError


class ConfigError(NSpawnError):
    """Raised by the loader. A misdeclared file is a refusal, never a default."""

    def __init__(self, message: str) -> None:
        self.message = message
        Exception.__init__(self, message)


# ---------------------------------------------------------------------------
# The config file — the env names the PATH, the file carries the value
# ---------------------------------------------------------------------------
#
# The registry itself is not configured here: it is `nspawn.sqlite3` in
# `NOTT_DATABASE_DIR` (tests) or the account's live store directory, see
# `registry.store_path`. A leftover `[store] url` is ignored, as are the retired
# database variables, so a process restarted on this build keeps running.

#: Where the file lives when nobody says otherwise.
DEFAULT_STORE_CONFIG_PATH = Path.home() / ".nott" / "spawn.toml"

#: Names the PATH of the file, never the value.
STORE_CONFIG_PATH_ENV = "NOTT_NSPAWN_CONFIG"

#: Byte ceiling. The file carries half a dozen keys; reading without a ceiling is the
#: path for the parser to swallow memory from a file that another process
#: is writing.
MAX_STORE_CONFIG_BYTES = 64 * 1024


def store_config_path() -> Path:
    declared = (os.environ.get(STORE_CONFIG_PATH_ENV) or "").strip()
    return Path(declared) if declared else DEFAULT_STORE_CONFIG_PATH


class NotRegularFile(OSError):
    """The path names a directory, a FIFO, a socket or a device, not a regular file."""


def read_regular_file(path: Path) -> bytes:
    """The bytes of `path`, read only when it is a regular file, with no wait.

    This is the one read of each roster source file. The open is non-blocking,
    so a FIFO with no writer does not stop it. The `fstat` checks the open
    descriptor, and the read uses that descriptor, so a path that changes
    after the check is never read. A roster read gives these bytes
    to source selection, the parse and the `[store]` check, and it does not
    read the path again by name.

    Each caller gives the class of its source to an `OSError`:
    `read_store_config` gives `ConfigError` for the single config file, and
    `owner_roster.read_roster` gives `RosterError.unreadable` for the
    compatibility roster (`NOTT_NSPAWN_ROSTER`).

    Raises:
        FileNotFoundError: nothing exists at the path.
        NotRegularFile: the path is not a regular file.
        OSError: the open, the `fstat` or the read failed.
    """
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise NotRegularFile("not a regular file")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1 << 16):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def read_store_config() -> tuple[Path, bytes] | None:
    """The single config file and its bytes, from one read, or None when it is absent.

    Only a path where nothing exists is absent. This function checks the
    `[store]` table of the bytes with refusals, not with tolerance.

    Raises:
        ConfigError: the path is not a readable regular file, or its bytes
            exceed the limit, do not parse or declare a wrong `[store]`.
    """
    path = store_config_path()
    try:
        raw = read_regular_file(path)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ConfigError(f"the config at {path} cannot be read: {error}") from error
    _store_table_of(path, raw)
    return path, raw


def _store_table_of(path: Path, raw: bytes) -> dict[str, Any]:
    """The `[store]` table of the bytes already read from `path`, with refusals."""
    if len(raw) > MAX_STORE_CONFIG_BYTES:
        raise ConfigError(
            f"the config at {path} exceeds {MAX_STORE_CONFIG_BYTES} bytes"
        )
    try:
        document = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
        raise ConfigError(f"the config at {path} is not valid TOML: {error}") from error
    store = document.get("store")
    if store is None:
        return {}
    if not isinstance(store, dict):
        raise ConfigError(
            f"the config at {path} declares a [store] that is not a table"
        )
    if "roster" in store:
        raise ConfigError(
            f"single config file {path}: [store].roster is retired; "
            "move [defaults] and [[provider]] into this file"
        )
    # `url` named the retired registry server: accepted and ignored.
    unknown = sorted(set(store) - {"url", "run_root"})
    if unknown:
        # deny_unknown_fields: an unrecognised key is a refusal, never
        # silent discarding — otherwise a typo becomes a default.
        raise ConfigError(
            f"the config at {path} declares unknown keys in [store]: {unknown}"
        )
    return store


#: Where run directories live when nobody says otherwise.
DEFAULT_RUN_ROOT = Path.home() / ".nott" / "state" / "nspawn" / "runs"

STORE_RUN_ROOT_ENV = "NOTT_NSPAWN_RUN_ROOT"


def _store_table(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except FileNotFoundError, OSError:
        return {}
    try:
        document = tomllib.loads(raw.decode("utf-8"))
    except tomllib.TOMLDecodeError, UnicodeDecodeError:
        return {}
    store = document.get("store")
    return store if isinstance(store, dict) else {}


def resolve_roster_path() -> Path | None:
    """The same declared file serves store and roster. The environment is compatibility.

    A roster read uses `read_store_config` to keep the bytes of this read.
    """
    source = read_store_config()
    return None if source is None else source[0]


def resolve_run_root() -> Path:
    """The root of the run directories: file, then environment, then default.

    Unlike the URL, absence here is NOT a refusal: a working path
    has an obvious place under `~/.nott/state`, and requiring the operator to declare it
    just for the server to accept a call is ceremony without a consumer.
    """
    declared = _store_table(store_config_path()).get("run_root")
    if isinstance(declared, str) and declared.strip():
        return Path(declared.strip()).expanduser()
    from_env = (os.environ.get(STORE_RUN_ROOT_ENV) or "").strip()
    if from_env and not (from_env.startswith("${") and from_env.endswith("}")):
        return Path(from_env).expanduser()
    return DEFAULT_RUN_ROOT
