"""`nmemory database create|snapshot`: bring the store file into existence, or copy it."""

from __future__ import annotations

import sys

import nott_database
from nott_database import DatabaseError

from ..database import SCHEMA_VERSION, create_store, store_path
from ..store import Store
from .common import parse_command


def cmd_database_create(argv: list[str]) -> None:
    parse_command("database", "create", argv)
    target = store_path()
    try:
        create_store(target, allow_live_store=True)
    except nott_database.RefusedError:
        raise
    except (DatabaseError, nott_database.Error, OSError) as error:
        print(f"nmemory: database create: {error}", file=sys.stderr)
        sys.exit(1)
    print(f"nmemory database create · {target} · schema v{SCHEMA_VERSION}")


def cmd_database_snapshot(argv: list[str]) -> None:
    args = parse_command("database", "snapshot", argv)
    target = store_path()
    try:
        # Read-only: the last close of a read-write handle would checkpoint and
        # delete the store's WAL under an exclusive lock.
        store = Store.open(target, read_only=True, allow_live_store=True)
        free, pages = store.db.snapshot(args.dest)
    except nott_database.RefusedError:
        raise
    except (DatabaseError, nott_database.Error, OSError) as error:
        print(f"nmemory: database snapshot: {error}", file=sys.stderr)
        sys.exit(1)
    print(
        f"nmemory database snapshot · {target} -> {args.dest}"
        f" · freelist {free}/{pages} ({free / max(pages, 1):.1%})"
    )
