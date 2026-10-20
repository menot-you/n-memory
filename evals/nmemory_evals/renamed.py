"""The variables the nMEMORY evals read under a new name, each old name spelled here once.

A reader asks for the new name through `env` (an entry point may ask `refuse` for all it
reads at once). If an old spelling is still set, it is refused by name instead of being
ignored or read as the new one: a unit that still exports the old spelling is found when
it starts, not by a missing value. Only the variables a process reads are checked, never
the whole environment. Every member's table has this same shape and text, so they diff;
`tools/organization.py` holds each to [map.env] and refuses a read around `env`.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping

RENAMED: dict[str, str] = {
    "NOTT_NMEMORY_BIN": "NMEMORY_BIN",
    "NOTT_NMEMORY_EVALS_REUSE": "NMEMORY_EVAL_REUSE",
    "NOTT_NMEMORY_EVALS_RUN_ID": "NMEMORY_EVAL_RUN_ID",
}

#: Old spellings with no new name of their own, each with the variable read in its place.
RETIRED: dict[str, str] = {}


class RenamedVariableError(Exception):
    """An old variable name is set where its new name is read."""

    def __init__(self, old: str, new: str) -> None:
        self.old = old
        self.new = new
        super().__init__(f"{old} was renamed to {new}: set {new} and unset {old}")


def old_names(name: str) -> tuple[str, ...]:
    """Every old spelling of `name`."""
    renamed = (RENAMED[name],) if name in RENAMED else ()
    return renamed + tuple(old for old, new in RETIRED.items() if new == name)


def refuse(environ: Mapping[str, str], names: Iterable[str]) -> None:
    """Refuse the first old spelling set in `environ` of one of `names`."""
    for name in names:
        for old in old_names(name):
            if old in environ:
                raise RenamedVariableError(old, name)


def env(name: str) -> str | None:
    """The value of `name`, refusing a set old spelling of it."""
    refuse(os.environ, (name,))
    return os.environ.get(name)
