"""What every repository shares."""

from __future__ import annotations

from .. import NMemoryError
from ..database import Database


class StoreError(NMemoryError):
    pass


class EmptyField(StoreError):
    pass


class Repository:
    """One owner's tables, reached through the Unit of Work's `Database`.

    Every statement runs on `db.connection()`, so inside a tool call it joins
    the boundary's transaction; a canonical writer carries `@writes`, a
    bookkeeping writer `with self.db.writing()`.
    """

    def __init__(self, db: Database) -> None:
        self.db = db
