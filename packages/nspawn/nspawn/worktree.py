"""Filesystem reads for executor worktrees: the repository root and the effect.

THIS MODULE NEVER INVOKES GIT: a guard that shells out to git would be trusting the
very tool whose state it is supposed to be checking, on a path an executor can write.
"""

import os
import stat
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


#: The ceiling for a text file this module reads under `.git`. git keeps HEAD,
#: commondir and the `.git` marker to one short line; a file past this is not one
#: of them, and a read with no bound is how one planted file takes the whole host.
MAX_GIT_FILE_BYTES: Final = 65_536


class _Unreadable(Exception):
    """A `.git` file is there and cannot be read as text: there is no honest answer.
    It is not the same as that file being absent."""


def _read_git_text(path: Path) -> str | None:
    """The text of `path`, or None when it is not there.

    Refuses, as _Unreadable, what a plain read trusts: a symlink, a FIFO, a
    directory, a file past MAX_GIT_FILE_BYTES, and bytes that are not UTF-8. A
    branch name may hold bytes that are not text, and a FIFO or a special file
    holds the worker open with no end. One opener for the three reads, as
    supervisor._open_regular does for the run directory.
    """
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError:
        raise _Unreadable from None
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise _Unreadable
        raw = os.read(descriptor, MAX_GIT_FILE_BYTES + 1)
    except OSError:
        raise _Unreadable from None
    finally:
        os.close(descriptor)
    if len(raw) > MAX_GIT_FILE_BYTES:
        raise _Unreadable
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise _Unreadable from None


def _git_dir(worktree: Path) -> Path | None:
    """The git directory of `worktree`: `.git`, or the gitdir its `.git` file names.

    None when there is no `.git` there. Raises _Unreadable when the `.git` file is
    there and cannot be read: that is not the same as having no git directory.
    """
    marker = worktree / ".git"
    if marker.is_dir():
        return marker
    if not marker.is_file():
        return None
    raw = _read_git_text(marker)
    if raw is None:
        return None
    named = raw.partition("gitdir:")[2].strip()
    if not named:
        return None
    candidate = Path(named)
    return candidate if candidate.is_absolute() else worktree / candidate


def _committed_branch(worktree: Path, *, since_ns: int) -> bool | None:
    """True when the branch checked out in `worktree` moved at or after `since_ns`;
    None when a `.git` file there cannot be read; False when there is no branch ref.

    A commit changes nothing outside `.git`, which the write scan leaves out, so the
    branch ref is the witness a commit leaves. Git will not check one branch out in
    two worktrees, so a move of that ref is this writer's own. A linked worktree
    keeps its refs in the common git directory. A detached HEAD names no branch: no
    witness. None is never turned into False: an unreadable `.git` read as "no
    commit" would move the writer's work to another route.
    """
    try:
        git_dir = _git_dir(worktree)
        if git_dir is None:
            return False
        head = _read_git_text(git_dir / "HEAD")
        if head is None:
            return False
        if not head.startswith("ref: "):
            return False
        name = Path(head[len("ref: ") :].strip())
        if not name.parts or ".." in name.parts or name.is_absolute():
            return False
        refs = git_dir
        common = _read_git_text(git_dir / "commondir")
        if common is not None:
            named = common.strip()
            if named:
                candidate = Path(named)
                refs = candidate if candidate.is_absolute() else git_dir / candidate
        for path in (refs / name, refs / "logs" / name):
            try:
                changed = _changed(path.lstat(), since_ns)
            except FileNotFoundError:
                continue
            except OSError:
                return None
            if changed:
                return True
        return False
    except _Unreadable:
        return None


def unwritten_since(worktree: Path, *, since_ns: int) -> bool:
    """True only when nothing in the tree changed at or after `since_ns`.

    The witness that a writer made no write yet, so nSPAWN may give its work to
    another route. A file, a link or a directory counts, so a
    deletion counts too. For a file with one link the scan also reads the change
    time, which the kernel sets, so a write that sets its mtime back still
    counts. A hard-linked file (a uv cache link in `.venv`) changes its change
    time when another tree links it, so only its mtime counts. Left out: `.git` at any
    depth, which changes on its own, and `.nott/disposable` of the tree, where
    nSPAWN keeps the writer's disposable home. A commit on the checked-out branch
    counts even so: it writes nothing outside `.git`, so its ref is read directly.
    False when the scan is unsure: an unreadable entry, a tree past
    MAX_WRITE_SCAN_ENTRIES, or a `.git` file that cannot be read.
    """
    if _committed_branch(worktree, since_ns=since_ns) is not False:
        # True: its branch moved, so its work is written. None: no honest answer.
        return False
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
