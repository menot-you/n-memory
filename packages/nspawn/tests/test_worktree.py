"""The worktree reads, proved against REAL git worktrees on disk.

The module never invokes git — that is its contract. The FIXTURES do, because
the fixture is the world the reads see.
"""

import os
import subprocess
import time
from pathlib import Path

import pytest

from nspawn import worktree as wt
from nspawn.worktree import RepositoryRootNotFound


def git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        env=dict(
            os.environ,
            GIT_AUTHOR_NAME="t",
            GIT_AUTHOR_EMAIL="t@t",
            GIT_COMMITTER_NAME="t",
            GIT_COMMITTER_EMAIL="t@t",
        ),
    )


def _age(*paths: Path) -> None:
    """Set each path's modification time back, so a later baseline sees a clean tree."""
    old = (time.time_ns() - 10_000_000_000) / 1e9
    for path in paths:
        os.utime(path, (old, old))


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git("init", "-q", "-b", "main", cwd=root)
    (root / "file.txt").write_text("one\n", encoding="utf-8")
    git("add", ".", cwd=root)
    git("commit", "-qm", "one", cwd=root)
    return root.resolve()


@pytest.fixture
def linked(repo: Path) -> Path:
    tree = repo.parent / "linked"
    git("worktree", "add", "-q", str(tree), "-b", "work", cwd=repo)
    return tree.resolve()


def test_discover_walks_up_and_refuses_when_there_is_no_repository(
    repo: Path, linked: Path, tmp_path: Path
) -> None:
    deep = linked / "a" / "b"
    deep.mkdir(parents=True)
    assert wt.discover_repository_root(deep) == linked
    lonely = tmp_path.parent / "definitely-not-a-repo-here"
    lonely.mkdir(exist_ok=True)
    with pytest.raises(RepositoryRootNotFound):
        wt.discover_repository_root(lonely / "missing")


def test_observed_effect_detects_modified_files(tmp_path: Path) -> None:
    wt_dir = tmp_path / "wt"
    wt_dir.mkdir()
    sub = wt_dir / "sub"
    sub.mkdir()
    f = sub / "file.txt"
    f.write_text("hello")
    mtime = f.stat().st_mtime_ns

    # Before change
    assert wt.observed_effect(wt_dir, since_ns=mtime + 1_000_000) == "none"

    # After change
    assert wt.observed_effect(wt_dir, since_ns=mtime - 1_000_000) == "files"


def test_observed_effect_ignores_git_directory(tmp_path: Path) -> None:
    wt_dir = tmp_path / "wt_git"
    wt_dir.mkdir()
    git_dir = wt_dir / ".git"
    git_dir.mkdir()
    git_file = git_dir / "HEAD"
    git_file.write_text("ref: refs/heads/main")
    mtime = git_file.stat().st_mtime_ns

    assert wt.observed_effect(wt_dir, since_ns=mtime - 1_000_000) == "none"


def test_observed_effect_returns_unreadable_on_inaccessible_path(
    tmp_path: Path,
) -> None:
    non_existent = tmp_path / "does_not_exist"
    assert wt.observed_effect(non_existent, since_ns=0) == "unreadable"


def test_observed_effect_caps_at_max_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(wt, "MAX_EFFECT_SCAN_ENTRIES", 3)
    wt_dir = tmp_path / "wt_many"
    wt_dir.mkdir()
    for i in range(5):
        (wt_dir / f"f_{i}.txt").write_text("x")

    assert (
        wt.observed_effect(wt_dir, since_ns=9_999_999_999_999_999_999) == "unreadable"
    )


def test_observed_effect_handles_stat_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wt_dir = tmp_path / "wt_stat_fail"
    wt_dir.mkdir()
    (wt_dir / "f.txt").write_text("x")

    def _failing_stat(*a: object, **kw: object) -> object:
        raise OSError("permission denied")

    monkeypatch.setattr(os.DirEntry, "stat", _failing_stat)
    assert wt.observed_effect(wt_dir, since_ns=0) == "unreadable"


def test_unwritten_since_counts_a_deletion_and_skips_git_and_nott(
    tmp_path: Path,
) -> None:
    "A writer moves only while nothing of its tree changed."
    tree = tmp_path / "tree"
    (tree / "src").mkdir(parents=True)
    (tree / "src" / "kept.py").write_text("x")
    (tree / ".git").mkdir()
    (tree / ".nott").mkdir()
    since = time.time_ns() + 50_000_000
    old = (since - 1_000_000_000) / 1e9
    for path in (tree / "src" / "kept.py", tree / "src", tree / ".git", tree):
        os.utime(path, (old, old))
    assert wt.unwritten_since(tree, since_ns=since)
    # nSPAWN's own areas change by themselves: they are not the writer's.
    (tree / ".git" / "index").write_text("i")
    (tree / ".nott" / "disposable").mkdir()
    assert wt.unwritten_since(tree, since_ns=0) is False  # the root itself is new
    assert wt.unwritten_since(tree, since_ns=since)
    time.sleep(0.06)
    (tree / "src" / "kept.py").unlink()  # a deletion changes only its directory
    assert wt.unwritten_since(tree, since_ns=since) is False
    assert wt.unwritten_since(tmp_path / "absent", since_ns=0) is False


def test_unwritten_since_counts_the_writers_nott_files_and_a_reset_mtime(
    tmp_path: Path,
) -> None:
    """Only `.nott/disposable` is nSPAWN's: an audit under `.nott` is a write,
    and so is a write that sets its mtime back."""
    tree = tmp_path / "tree"
    (tree / ".nott" / "disposable").mkdir(parents=True)
    (tree / "kept.py").write_text("x")
    since = time.time_ns()
    time.sleep(0.01)
    (tree / ".nott" / "disposable" / "home").mkdir()
    assert wt.unwritten_since(tree, since_ns=since)
    (tree / ".nott" / "audit").mkdir()
    assert wt.unwritten_since(tree, since_ns=since) is False
    (tree / ".nott" / "audit").rmdir()
    since = time.time_ns()
    time.sleep(0.01)
    old = (since - 1_000_000_000) / 1e9
    (tree / "kept.py").write_text("y")
    os.utime(tree / "kept.py", (old, old))
    assert wt.unwritten_since(tree, since_ns=since) is False


def test_unwritten_since_counts_a_commit_on_the_runs_branch(repo: Path) -> None:
    """A run whose only change is a commit leaves a clean tree: that commit is
    its write, so the writer keeps its tree."""
    (repo / "next.py").write_text("y\n", encoding="utf-8")
    git("add", "next.py", cwd=repo)
    since = time.time_ns()
    time.sleep(0.01)
    git("commit", "-qm", "committed after the baseline", cwd=repo)
    assert wt.unwritten_since(repo, since_ns=since) is False
    # The commit alone is not a write for a later baseline: `.git` still changes
    # by itself, and that is not the writer's.
    assert wt.unwritten_since(repo, since_ns=time.time_ns())


def test_unwritten_since_counts_a_commit_in_a_linked_worktree(
    repo: Path, linked: Path
) -> None:
    """The branch ref of a linked worktree lives in the common git directory."""
    (linked / "w.py").write_text("w\n", encoding="utf-8")
    git("add", "w.py", cwd=linked)
    since = time.time_ns()
    time.sleep(0.01)
    git("commit", "-qm", "committed in the linked worktree", cwd=linked)
    assert wt.unwritten_since(linked, since_ns=since) is False


def test_unwritten_since_is_unsure_when_head_is_not_text(repo: Path) -> None:
    """A branch name can hold bytes that are not UTF-8. The witness must answer
    unsure, not raise: a raised error ends the run as a dead worker, with no
    failover and no moved notes."""
    (repo / ".git" / "HEAD").write_bytes(b"ref: refs/heads/p\xe1cio\n")
    _age(repo, repo / "file.txt")
    assert wt.unwritten_since(repo, since_ns=time.time_ns()) is False


@pytest.mark.timeout(10)
def test_unwritten_since_does_not_block_on_a_fifo_head(repo: Path) -> None:
    """A planted FIFO at .git/HEAD must not hold the worker open with no end."""
    (repo / ".git" / "HEAD").unlink()
    os.mkfifo(repo / ".git" / "HEAD")
    _age(repo, repo / "file.txt")
    assert wt.unwritten_since(repo, since_ns=time.time_ns()) is False


def test_unwritten_since_is_unsure_when_head_cannot_be_read(repo: Path) -> None:
    """An unreadable HEAD is not an absent one: the answer is unsure, not "no
    commit"."""
    (repo / ".git" / "HEAD").chmod(0o000)
    _age(repo, repo / "file.txt")
    assert wt.unwritten_since(repo, since_ns=time.time_ns()) is False


def test_unwritten_since_is_unsure_when_the_git_marker_cannot_be_read(
    repo: Path, linked: Path
) -> None:
    """A linked worktree names its git directory in a `.git` file. That read has
    three answers: a git directory, no git there, or no honest answer."""
    (linked / ".git").chmod(0o000)
    _age(linked, linked / "file.txt")
    assert wt.unwritten_since(linked, since_ns=time.time_ns()) is False


def test_unwritten_since_does_not_read_through_a_symlinked_head(
    repo: Path, linked: Path
) -> None:
    """A planted symlink in the git directory is not followed: what lives outside
    this tree is not the writer's, and the answer for it is unsure."""
    outside = repo.parent / "elsewhere"
    outside.write_text("not a ref\n", encoding="utf-8")
    head = repo / ".git" / "worktrees" / linked.name / "HEAD"
    head.unlink()
    head.symlink_to(outside)
    _age(linked, linked / "file.txt")
    assert wt.unwritten_since(linked, since_ns=time.time_ns()) is False


def test_unwritten_since_refuses_a_git_file_past_its_ceiling(repo: Path) -> None:
    """git keeps HEAD to one short line. A file there is not HEAD: reading it
    without a bound is what lets one planted file take the whole host."""
    (repo / ".git" / "HEAD").write_bytes(b"\x00" * 1_000_000)
    _age(repo, repo / "file.txt")
    assert wt.unwritten_since(repo, since_ns=time.time_ns()) is False


def test_unwritten_since_ignores_a_link_that_another_tree_adds(
    tmp_path: Path,
) -> None:
    """A uv cache file hard-linked in `.venv` changes its change time when
    another tree links it: that is not this writer's write."""
    tree = tmp_path / "tree"
    (tree / ".venv").mkdir(parents=True)
    cache = tmp_path / "cache-file"
    cache.write_text("x")
    os.link(cache, tree / ".venv" / "module.py")
    since = time.time_ns()
    time.sleep(0.01)
    os.link(cache, tmp_path / "other-tree-link")
    assert wt.unwritten_since(tree, since_ns=since)
