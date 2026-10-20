"""Disposable store files for the suite.

Every test runs with its own `NOTT_DATABASE_DIR` (the `nott_database` pytest
plugin), so a store here is a new file nobody else writes. `fresh_store_path`
gives a new file per call — the old "reset the lane database" — and
`default_store_path` creates the one a child process finds on its own
(`<NOTT_DATABASE_DIR>/nmemory.sqlite3`), empty again on every call.
"""

from __future__ import annotations

import itertools
import os
from collections.abc import Sequence
from pathlib import Path

import nott_database

from nmemory.database import (
    STORE_FILE,
    Database,
    create_store,
    now_us,
    store_path,
    vector_blob,
)

_serial = itertools.count()


def fresh_store_path(prefix: str = "t") -> Path:
    """A new, empty store file under this test's directory."""
    path = nott_database.directory() / f"nmemory-{prefix}-{next(_serial)}.sqlite3"
    create_store(path)
    return path


def default_store_path(prefix: str = "") -> Path:
    """The store a child process resolves by itself, EMPTY on every call.

    `prefix` is only a label kept from the lane databases. The file is replaced,
    so a helper that asked for a fresh store twice gets two clean ones, as the
    old "reset the lane database" did. Refused outside a test's own directory.
    """
    del prefix
    if "NOTT_DATABASE_DIR" not in os.environ:
        raise RuntimeError("default_store_path needs the test's NOTT_DATABASE_DIR")
    path = store_path()
    nott_database.remove(path)
    create_store(path)
    return path


def put_embedding(
    db: Database,
    capsule_id: str,
    vector: Sequence[float],
    space: str,
    text_fingerprint: str = "0" * 64,
) -> None:
    """Write one vector directly, as the worker's `complete_embedding` would, and
    count the store's first index of `space` as complete, as the worker's
    activation would: recall searches a space only once it is whole."""
    with db.writing() as conn:
        conn.execute(
            "INSERT INTO capsule_embeddings (project_id, space_fingerprint, capsule_id,"
            " text_fingerprint, embedding, updated_at)"
            " SELECT project_id, ?, id, ?, ?, ? FROM capsules WHERE id = ?"
            " ON CONFLICT (project_id, space_fingerprint, capsule_id) DO UPDATE"
            " SET embedding = excluded.embedding,"
            " text_fingerprint = excluded.text_fingerprint",
            (space, text_fingerprint, vector_blob(vector), now_us(), capsule_id),
        )
        conn.execute(
            "UPDATE embedding_state SET target_space = NULL, phase = 'ready'"
            " WHERE active_space = ? AND target_space = active_space",
            (space,),
        )


__all__ = ["STORE_FILE", "default_store_path", "fresh_store_path", "put_embedding"]


def sample_capsule_id(number: int) -> str:
    """Create a protocol sample from a disposable store counter."""
    return f"cap-{number}"
