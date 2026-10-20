"""Each test's own registry file.

The `nott-database` pytest plugin points `NOTT_DATABASE_DIR` at a directory of the
test's own, so a store is pristine by construction: `store()` creates it on first
use, through the product's own `create_store`, and every later call answers the
same file. Subprocesses the test starts inherit the directory.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import nott_database

from nspawn.registry import Registry, create_store, store_path


def store() -> Path:
    """This test's registry file, created on first use."""
    path = store_path()
    if not path.exists():
        create_store(path)
    return path


class Rows(list[Any]):
    """A statement's rows, read to the end, with the cursor verbs tests use."""

    def fetchone(self) -> Any:
        return self[0] if self else None

    def fetchall(self) -> list[Any]:
        return list(self)


def sql(registry: Registry, statement: str, params: Any = ()) -> Rows:
    """One statement on `registry`'s own connection, inside a short write.

    It goes through the registry's own transaction, so a test thread reading the
    connection a supervisor thread shares waits its turn instead of ending that
    thread's transaction.
    """
    with registry._transaction():
        return Rows(nott_database.query(registry.connection, statement, params))


def reset_registry(registry: Registry, now: int) -> None:
    """Register `registry`'s instance on this test's (fresh) store."""
    registry.open(now=now)
