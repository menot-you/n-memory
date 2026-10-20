"""The variables nMEMORY reads under a new name, each old name spelled here once.

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

from nmemory import NMemoryError

RENAMED: dict[str, str] = {
    "NOTT_NMEMORY_ANCHOR_ROOT": "NMEMORY_ANCHOR_ROOT",
    "NOTT_NMEMORY_CODEGRAPH_BIN": "NMEMORY_CODEGRAPH_BIN",
    "NOTT_NMEMORY_HARNESS": "NOTT_HARNESS",
    "NOTT_NMEMORY_HMAC_KEY": "NMEMORY_HMAC_KEY",
    "NOTT_NMEMORY_HMAC_KEY_FILE": "NMEMORY_HMAC_KEY_FILE",
    "NOTT_NMEMORY_JBCONTEXT_BIN": "NMEMORY_JBCONTEXT_BIN",
    "NOTT_NMEMORY_JBCONTEXT_ENABLED": "NMEMORY_JBCONTEXT_ENABLED",
    "NOTT_NMEMORY_MODEL": "NOTT_MODEL",
    "NOTT_NMEMORY_PROJECT": "NMEMORY_PROJECT",
    "NOTT_NMEMORY_SESSION_ID": "NOTT_SESSION_ID",
    "NOTT_NMEMORY_SYSTEM1": "NOTT_NMEMORY_JEV",
}

#: Old spellings with no new name of their own, each with the variable read in its place.
RETIRED: dict[str, str] = {}

#: What the `nmemory` commands read.
READS: tuple[str, ...] = tuple(RENAMED)


class RenamedVariableError(NMemoryError):
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
