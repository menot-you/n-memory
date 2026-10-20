"""Filesystem reads for executor worktrees: the repository root and the effect.

THIS MODULE NEVER INVOKES GIT: a guard that shells out to git would be trusting the
very tool whose state it is supposed to be checking, on a path an executor can write.
"""

import os
from pathlib import Path
from typing import Final

from .errors import NSpawnError


class WorktreeError(NSpawnError):
    """The root of this module's refusals."""

    def __init__(self, message: str) -> None:
        self.message = message
        Exception.__init__(self, message)


class RepositoryRootNotFound(WorktreeError):
    def __init__(self, path: Path) -> None:
        self.path = path
        super().__init__(f"repository root was not found from: {path}")


def _canonical(path: Path) -> Path | None:
    try:
        return path.resolve(strict=True)
    except OSError:
        return None


def _resolve_gitdir(worktree: Path, raw: str) -> Path:
    candidate = Path(raw)
    return candidate if candidate.is_absolute() else worktree / candidate


def discover_repository_root(start: Path) -> Path:
    """Walk upward until a `.git` is found, or REFUSE."""
    absolute = _canonical(start)
    if absolute is None:
        raise RepositoryRootNotFound(start)
    current = absolute
    while True:
        if (current / ".git").exists():
            return current
        if current.parent == current:
            raise RepositoryRootNotFound(absolute)
        current = current.parent


#: The ceiling for the effect scan. A run that touched more than this has already
#: proven an effect well before the ceiling; the ceiling exists for the EXPENSIVE case — the
#: inert run, which only finishes the scan after looking at the entire tree.
MAX_EFFECT_SCAN_ENTRIES: Final = 20_000


def observed_effect(worktree: Path, *, since_ns: int) -> str:
    """Did any file in the tree change after `since_ns`?

    An EXTERNAL fact to the child. The incident that originated this was an agent
    that reported SUCCESS without writing anything, with the exit code agreeing:
    what the child says about itself is not witness to what it did.

    `.git` is LEFT OUT: it changes on its own — index, logs, refs — even
    in an inert run, and counting it would make the answer a constant "yes".

    Stops at the FIRST new file, so the common case costs almost nothing; it is the
    inert run, which needs to inspect everything, that pays the ceiling.
    """
    seen = 0
    stack = [worktree]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            return "unreadable"
        for entry in entries:
            seen += 1
            if seen > MAX_EFFECT_SCAN_ENTRIES:
                return "unreadable"
            if entry.name == ".git":
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                    continue
                if entry.stat(follow_symlinks=False).st_mtime_ns >= since_ns:
                    return "files"
            except OSError:
                return "unreadable"
    return "none"


#: The ceiling for the write scan of a failover. It is higher than the effect
#: scan's: a checkout with its `.venv` holds some 10^5 entries, and an unsure
#: answer keeps a writer where it is.
MAX_WRITE_SCAN_ENTRIES: Final = 1_000_000


def _changed(st: os.stat_result, since_ns: int) -> bool:
    if st.st_mtime_ns >= since_ns:
        return True
    return st.st_nlink == 1 and st.st_ctime_ns >= since_ns


def unwritten_since(worktree: Path, *, since_ns: int) -> bool:
    """True only when nothing in the tree changed at or after `since_ns`.

    The witness that a writer made no write yet, so nSPAWN may give its work to
    another route. A file, a link or a directory counts, so a
    deletion counts too. For a file with one link the scan also reads the change
    time, which the kernel sets, so a write that sets its mtime back still
    counts. A hard-linked file (a uv cache link in `.venv`) changes its change
    time when another tree links it, so only its mtime counts. Left out: `.git` at any
    depth, which changes on its own, and `.nott/disposable` of the tree, where
    nSPAWN keeps the writer's disposable home. False when the scan is unsure:
    an unreadable entry, or a tree past MAX_WRITE_SCAN_ENTRIES.
    """
    nott = worktree / ".nott"
    try:
        if _changed(worktree.lstat(), since_ns):
            return False
    except OSError:
        return False
    seen = 0
    stack = [worktree]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            return False
        for entry in entries:
            seen += 1
            if seen > MAX_WRITE_SCAN_ENTRIES:
                return False
            if entry.name == ".git" or (current == nott and entry.name == "disposable"):
                continue
            try:
                if _changed(entry.stat(follow_symlinks=False), since_ns):
                    return False
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
            except OSError:
                return False
    return True
