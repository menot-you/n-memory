"""The review Candidate lock: a reader reads the tree its `Candidate:` line names.

`Supervisor.review` refuses a review when the first column-0 `Candidate:` line
of its prompt names a commit (7 to 40 hex digits) and cwd is not at that
commit. The tests use real Git repositories, the real `plugin/scripts/review.sh`
and a real registry.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any, NamedTuple

import pytest
from supervisor_support import (
    _drain_worker_threads,
    _stop_every_reaper,
    _sv,
    fake_picked,
    fake_provider,
    registry,
    roster_home,
    wired_success,
)
from test_driver import Drivers, Reaper, _call, _payload, _stdio, drv, reaper

from nspawn import supervisor
from nspawn.mcp import McpServer
from nspawn.registry import Registry
from nspawn.supervisor import InvalidRunRequest, Supervisor

__all__ = [
    "_drain_worker_threads",
    "_stop_every_reaper",
    "drv",
    "fake_picked",
    "fake_provider",
    "reaper",
    "registry",
    "roster_home",
    "wired_success",
]

ROOT = Path(__file__).resolve().parents[3]
REVIEW_SH = ROOT / "plugin" / "scripts" / "review.sh"
SKILL = ROOT / "plugin" / "skills" / "review" / "SKILL.md"
FIX = "plugin/scripts/review.sh snapshot"
#: The real `subprocess.run`: the spy below must not see the tests' own git.
_RUN = subprocess.run
_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t.invalid",
}


def _git(cwd: Path, *args: str) -> str:
    done = _RUN(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **_GIT_ENV},
    )
    return done.stdout.strip()


class Repo(NamedTuple):
    path: Path
    c1: str
    c2: str


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    """A repository on `main` with two commits, HEAD at C2."""
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    (path / "f.txt").write_text("one\n")
    (path / "sub").mkdir()
    (path / "sub" / "g.txt").write_text("g\n")
    _git(path, "add", ".")
    _git(path, "commit", "-qm", "one")
    c1 = _git(path, "rev-parse", "HEAD")
    (path / "f.txt").write_text("two\n")
    _git(path, "commit", "-qam", "two")
    return Repo(path, c1, _git(path, "rev-parse", "HEAD"))


def _snapshot(repo: Repo, rev: str, parent: Path) -> Path:
    """The tree that `review.sh snapshot <rev> <repo>` prints."""
    parent.mkdir(exist_ok=True)
    done = _RUN(
        ["bash", str(REVIEW_SH), "snapshot", rev, str(repo.path)],
        cwd=repo.path,
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "TMPDIR": str(parent)},
    )
    return Path(done.stdout.strip())


def _plain_tree(repo: Repo, rev: str, folder: Path) -> Path:
    """`git archive <rev> | tar -x`: the files, no marker and no `.git`."""
    folder.mkdir()
    archive = _RUN(
        ["git", "archive", rev], cwd=repo.path, check=True, capture_output=True
    )
    _RUN(["tar", "-x", "-C", str(folder)], input=archive.stdout, check=True)
    return folder


@pytest.fixture
def head_reads(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[list[str]]]:
    """Every `git rev-parse ... HEAD^{commit}` that nSPAWN starts."""
    calls: list[list[str]] = []

    def spy(argv: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(argv, list) and "HEAD^{commit}" in argv:
            calls.append(list(argv))
        return _RUN(argv, *args, **kwargs)

    monkeypatch.setattr(supervisor.subprocess, "run", spy)
    yield calls


def _refused(sv: Supervisor, cwd: Path | str, prompt: str, branch: str = "main") -> str:
    with pytest.raises(InvalidRunRequest) as raised:
        sv.review(branch, str(cwd), prompt=prompt)
    return str(raised.value)


# ---------------------------------------------------------------------------
# The parser: a pure function of the prompt.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("prompt", "word"),
    [
        ("Candidate: main@abc", "main@abc"),
        ("Candidate:main@abc tail", "main@abc"),
        ("Reply.\nCandidate: a@b\nCandidate: c@d", "a@b"),
        ("Candidate: `main@abc`.", "`main@abc`."),
        ("Candidate:\nCandidate: c@d", None),
        ("**Candidate:** main@abc", None),
        (" Candidate: main@abc", None),
        ("one line Candidate: x@abc", None),
        ("no line", None),
        ("", None),
    ],
)
def test_review_candidate_reads_the_first_column_zero_line(
    prompt: str, word: str | None
) -> None:
    assert supervisor._review_candidate(prompt) == word


# ---------------------------------------------------------------------------
# RED: a stale or commitless tree is refused, and the refusal names the fix.
# ---------------------------------------------------------------------------


def test_snapshot_at_another_commit_is_refused_with_the_fix(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    wired_success: None,
    repo: Repo,
) -> None:
    stale = _snapshot(repo, repo.c1, tmp_path / "snap")
    message = _refused(_sv(registry, tmp_path), stale, f"Candidate: main@{repo.c2}")
    assert repo.c1[:12] in message
    assert repo.c2 in message
    assert f"{FIX} {repo.c2} <repo>" in message
    # N6: the command works from any cwd; N8: a 40-hex sha gets no opt-out hint.
    assert "relative to the root of the NOTT repository" in message
    assert "64-hex digest" not in message


def test_stale_snapshot_first_named_repair_admits_the_review(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    wired_success: None,
    repo: Repo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale = _snapshot(repo, repo.c1, tmp_path / "snap with spaces").resolve()
    monkeypatch.setenv("TMPDIR", str(stale))
    sv = _sv(registry, tmp_path)
    prompt = f"Candidate: main@{repo.c2}"
    message = _refused(sv, stale, prompt)
    # Follow the first repair, with only its documented placeholders replaced.
    command = message.split("Run ", 1)[1].split(" (the path is ", 1)[0]
    command = command.replace("plugin/scripts/review.sh", shlex.quote(str(REVIEW_SH)))
    command = command.replace("<repo>", shlex.quote(str(repo.path)))
    command = command.replace(" [<base>]", "")
    repaired = _RUN(
        ["bash", "-c", command],
        cwd=stale,
        env={**os.environ, "TMPDIR": str(stale)},
        check=True,
        capture_output=True,
        text=True,
    )
    fixed = Path(repaired.stdout.strip())
    assert sv.review("main", str(fixed), prompt=prompt).run_id
    assert stale not in fixed.resolve().parents


def test_stale_snapshot_repair_keeps_writable_tmpdir_with_read_only_parent(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    wired_success: None,
    repo: Repo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "read only reviews"
    stale = _snapshot(repo, repo.c1, parent).resolve()
    output = tmp_path / "writable output"
    output.mkdir()
    monkeypatch.setenv("TMPDIR", str(output))
    parent.chmod(0o555)
    try:
        assert not os.access(parent, os.W_OK)
        sv = _sv(registry, tmp_path)
        prompt = f"Candidate: main@{repo.c2}"
        message = _refused(sv, stale, prompt)
        command = message.split("Run ", 1)[1].split(" (the path is ", 1)[0]
        command = command.replace(
            "plugin/scripts/review.sh", shlex.quote(str(REVIEW_SH))
        )
        command = command.replace("<repo>", shlex.quote(str(repo.path)))
        command = command.replace(" [<base>]", "")
        repaired = _RUN(
            ["bash", "-c", command],
            cwd=stale,
            check=True,
            capture_output=True,
            text=True,
        )
        fixed = Path(repaired.stdout.strip())
        assert fixed.parent == output
        assert sv.review("main", str(fixed), prompt=prompt).run_id
    finally:
        parent.chmod(0o755)


@pytest.mark.parametrize("shape", ["archive", "git-init", "missing"])
def test_tree_with_no_commit_is_refused_with_the_fix(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    wired_success: None,
    repo: Repo,
    shape: str,
) -> None:
    cwd = tmp_path / "tree"
    if shape == "archive":
        _plain_tree(repo, repo.c2, cwd)
    elif shape == "git-init":
        cwd.mkdir()
        _git(cwd, "init", "-q", "-b", "main")
    message = _refused(_sv(registry, tmp_path), cwd, f"Candidate: main@{repo.c2}")
    assert "holds no commit" in message
    assert f"{FIX} {repo.c2} " in message


def test_a_date_like_revision_names_the_form_that_does_not_bind(
    registry: Registry, tmp_path: Path, roster_home: Path, wired_success: None
) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    message = _refused(_sv(registry, tmp_path), plain, "Candidate: plan@20261025")
    assert "holds no commit" in message
    assert "64-hex digest" in message


@pytest.mark.parametrize(
    ("template", "written"),
    [
        ("Candidate: main@{c1}", "{c1}"),
        ("Candidate: `main@{c1}`", "{c1}"),
        ("Candidate: main@{c1}.", "{c1}"),
        ("Candidate: **main@{c1}**", "{c1}"),
        ("Candidate: {c1}", "{c1}"),
        ("Candidate: main@{c1_7}", "{c1_7}"),
        ("Candidate: main@{c1_upper}", "{c1}"),
        ("Reply in one line.\nCandidate: main@{c1}", "{c1}"),
    ],
)
def test_live_worktree_at_another_head_is_refused(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    wired_success: None,
    repo: Repo,
    template: str,
    written: str,
) -> None:
    values = {"c1": repo.c1, "c1_7": repo.c1[:7], "c1_upper": repo.c1.upper()}
    message = _refused(_sv(registry, tmp_path), repo.path, template.format(**values))
    assert repo.c2[:12] in message
    # The fix names the bare commit: a mark around the word never reaches it.
    assert f"{FIX} {written.format(**values)} <repo>" in message
    # N8: only an abbreviated revision is possibly not a commit.
    assert ("64-hex digest" in message) == (len(written.format(**values)) < 40)


@pytest.mark.parametrize("kind", ["link", "fifo", "not-hex", "two-lines", "filler"])
def test_odd_marker_is_refused_without_reading_it(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    wired_success: None,
    repo: Repo,
    kind: str,
) -> None:
    tree = _plain_tree(repo, repo.c2, tmp_path / "tree")
    marker = tree / ".nott-review-sha"
    if kind == "link":
        target = tmp_path / "elsewhere"
        target.write_text(f"{repo.c2}\n")
        marker.symlink_to(target)
    elif kind == "fifo":
        os.mkfifo(marker)
    elif kind == "not-hex":
        marker.write_text("not-a-sha\n")
    elif kind == "two-lines":
        marker.write_text(f"{repo.c2}\n{repo.c2}\n")
    else:
        marker.write_text("q" * (1 << 20))
    started = time.monotonic()
    message = _refused(_sv(registry, tmp_path), tree, f"Candidate: main@{repo.c2}")
    assert time.monotonic() - started < 5
    assert "is not one commit line" in message
    assert "not-a-sha" not in message
    assert "qqqqqqqq" not in message


@pytest.mark.parametrize("shape", ["plain", "screen-qa"])
def test_the_named_fix_admits_the_review(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    wired_success: None,
    repo: Repo,
    shape: str,
) -> None:
    sv = _sv(registry, tmp_path)
    prompt = f"Candidate: main@{repo.c2}"
    if shape == "plain":
        cwd = _plain_tree(repo, repo.c2, tmp_path / "tree")
        read_paths = None
    else:
        cwd = tmp_path / "shots"
        cwd.mkdir()
        (cwd / "window.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        read_paths = [str(cwd)]
    message = _refused(sv, cwd, prompt)
    assert f"{FIX} {repo.c2} <repo> [<base>]" in message
    fixed = _snapshot(repo, repo.c2, tmp_path / "fixed")
    accepted = sv.review("main", str(fixed), prompt=prompt, read_paths=read_paths)
    assert accepted.run_id


@pytest.mark.parametrize("failure", ["timeout", "oserror"])
def test_git_failure_is_refused_not_admitted(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    wired_success: None,
    repo: Repo,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    def broken(argv: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(argv, list) and "HEAD^{commit}" in argv:
            if failure == "timeout":
                raise subprocess.TimeoutExpired(argv, 10)
            raise FileNotFoundError(2, "No such file or directory", "git")
        return _RUN(argv, *args, **kwargs)

    monkeypatch.setattr(supervisor.subprocess, "run", broken)
    message = _refused(_sv(registry, tmp_path), repo.path, f"Candidate: main@{repo.c2}")
    expected = "Retry the same call" if failure == "timeout" else "git cannot run"
    assert expected in message


def test_live_worktree_marker_does_not_hide_head(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    wired_success: None,
    repo: Repo,
) -> None:
    (repo.path / ".nott-review-sha").write_text(f"{repo.c1}\n")
    _git(repo.path, "add", ".nott-review-sha")
    _git(repo.path, "commit", "-qm", "plant a marker")
    head = _git(repo.path, "rev-parse", "HEAD")
    message = _refused(_sv(registry, tmp_path), repo.path, f"Candidate: main@{repo.c1}")
    assert head[:12] in message


@pytest.fixture
def parked(monkeypatch: pytest.MonkeyPatch, wired_success: None) -> None:
    """Admission is real; the worker is not: the run stays `spawning`."""
    monkeypatch.setattr(Supervisor, "_drive_run", lambda *args, **kwargs: None)


def test_refusal_reaches_the_mcp_caller_and_frees_the_key(
    drv: ModuleType,
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    reaper: Reaper,
    parked: None,
    repo: Repo,
) -> None:
    drivers = Drivers(tmp_path / "runs")
    reaper.drivers.append(drivers)
    server = McpServer(
        supervisor=_stdio(registry, tmp_path),
        launcher=drv.InProcessLauncher(drivers, owns_supervisor=False),
    )
    reaper.launchers.append(server.launcher)
    stale = _snapshot(repo, repo.c1, tmp_path / "snap")
    arguments = {
        "branch": "main",
        "prompt": f"Candidate: main@{repo.c2}\nReply with the word READ.",
        "group": "c5-tb7",
        "idempotency_key": "c5-tb7",
    }
    answer = _call(server, "nspawn_review_start", {**arguments, "cwd": str(stale)})
    assert "error" in answer, answer
    assert FIX in answer["error"]["message"]
    listed = _payload(_call(server, "nspawn_run_list", {"include_terminal": True}))
    assert listed["runs"] == [], listed
    fixed = _snapshot(repo, repo.c2, tmp_path / "fixed")
    ack = _payload(
        _call(server, "nspawn_review_start", {**arguments, "cwd": str(fixed)})
    )
    assert ack["run_id"]


# ---------------------------------------------------------------------------
# Guards: a prompt that names no commit, and the reader shapes on main.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "template",
    [
        None,
        "Review the branch read-only.",
        "Candidate: plan@{hex64}",
        "Candidate: lane@{c1}f",
        "Candidate: plan@r3",
        "Candidate: plan@abc12",
        "Candidate: <sha256>",
        "**Candidate:** main@{c1}",
        "- Candidate: main@{c1}",
        "> Candidate: main@{c1}",
        " Candidate: main@{c1}",
        "-Candidate: main@{c1}",
        "one line Candidate: x@{c1}",
    ],
)
def test_prompts_without_a_commit_candidate_are_admitted(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    wired_success: None,
    head_reads: list[list[str]],
    template: str | None,
) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    c1 = "0123456789abcdef0123456789abcdef01234567"
    prompt = None if template is None else template.format(c1=c1, hex64="ab" * 32)
    accepted = _sv(registry, tmp_path).review("main", str(plain), prompt=prompt)
    assert accepted.run_id
    assert head_reads == []


@pytest.mark.parametrize(
    "shape", ["snapshot", "clone", "upper-8", "two-at", "subfolder", "placeholder"]
)
def test_reader_shapes_on_main_are_admitted(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    wired_success: None,
    repo: Repo,
    head_reads: list[list[str]],
    shape: str,
) -> None:
    branch, cwd = "main", repo.path
    prompt = f"Candidate: lane@{repo.c2}"
    if shape == "snapshot":
        cwd = _snapshot(repo, repo.c2, tmp_path / "snap")
        branch = "lane"
        prompt = (
            f"Candidate: lane@{repo.c2}\n{SKILL.read_text()}\n"
            f"Candidate: other@{repo.c1}\n"
        )
    elif shape == "clone":
        cwd = tmp_path / "clone"
        _git(
            tmp_path,
            "clone",
            "-q",
            "--no-checkout",
            "--no-local",
            str(repo.path),
            "clone",
        )
        _git(cwd, "checkout", "-q", "--detach", repo.c2)
    elif shape == "upper-8":
        prompt = f"Candidate: lane@{repo.c2[:8].upper()} (round 2)"
    elif shape == "two-at":
        prompt = f"Candidate: a@b@{repo.c2}"
    elif shape == "subfolder":
        cwd = repo.path / "sub"
    else:
        cwd = tmp_path / "plain"
        cwd.mkdir()
        prompt = f"Candidate: <slug>@<sha256>\nCandidate: lane@{repo.c1}"
    accepted = _sv(registry, tmp_path).review(branch, str(cwd), prompt=prompt)
    assert accepted.run_id
    if shape == "placeholder":
        assert head_reads == []


@pytest.mark.parametrize(
    "template",
    [
        "Candidate: main@{c2}.",
        "Candidate: `main@{c2}`",
        "Candidate: `main@{c2}`.",
        "Candidate: **main@{c2}**",
        "Candidate: main@{c2},",
        "Candidate: (main@{c2})",
        "Candidate: main@{c2}\r",
    ],
)
def test_a_decorated_candidate_word_at_the_cwd_commit_is_admitted(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    wired_success: None,
    repo: Repo,
    template: str,
) -> None:
    """The lock compares the hex digits it matched, never the marks around them."""
    for cwd in (repo.path, _snapshot(repo, repo.c2, tmp_path / "snap")):
        accepted = _sv(registry, tmp_path).review(
            "main", str(cwd), prompt=template.format(c2=repo.c2)
        )
        assert accepted.run_id


def test_snapshot_inside_a_repository_reads_its_marker(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    wired_success: None,
    repo: Repo,
) -> None:
    inner = _snapshot(repo, repo.c1, repo.path / "inner")
    assert not (inner / ".git").exists()
    accepted = _sv(registry, tmp_path).review(
        "main", str(inner), prompt=f"Candidate: main@{repo.c1}"
    )
    assert accepted.run_id
    shutil.rmtree(repo.path / "inner")


# ---------------------------------------------------------------------------
# Round 1 (A): the lock reads the commit of the files at cwd, not of an
# enclosing repository.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("candidate", ["c2", "c1"])
def test_a_stale_copy_inside_a_work_tree_is_refused(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    wired_success: None,
    repo: Repo,
    candidate: str,
) -> None:
    """A1: git climbs to the enclosing repository at C2. The copy is not at C2,
    and the refusal never says that the copy is at the commit of that repository."""
    (repo.path / ".nott").mkdir()
    frozen = _plain_tree(repo, repo.c1, repo.path / ".nott" / "frozen")
    rev = getattr(repo, candidate)
    message = _refused(_sv(registry, tmp_path), frozen, f"Candidate: main@{rev}")
    assert "holds no commit" in message
    assert f"at commit {repo.c2[:12]}" not in message


def test_a_nested_marker_in_a_commit_does_not_hide_head(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    wired_success: None,
    repo: Repo,
) -> None:
    """A2: a candidate commit carries `sub/.nott-review-sha`."""
    (repo.path / "sub" / ".nott-review-sha").write_text(f"{repo.c1}\n")
    _git(repo.path, "add", "sub/.nott-review-sha")
    _git(repo.path, "commit", "-qm", "plant a nested marker")
    head = _git(repo.path, "rev-parse", "HEAD")
    message = _refused(
        _sv(registry, tmp_path), repo.path / "sub", f"Candidate: main@{repo.c1}"
    )
    assert head[:12] in message


@pytest.mark.parametrize("through", ["path", "link"])
def test_a_nested_marker_in_a_snapshot_is_not_its_commit(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    wired_success: None,
    repo: Repo,
    through: str,
) -> None:
    """A2 in a snapshot: review.sh refuses a marker only at the snapshot root,
    so the commit can carry `sub/.nott-review-sha` with any value."""
    (repo.path / "sub" / ".nott-review-sha").write_text(f"{repo.c1}\n")
    _git(repo.path, "add", "sub/.nott-review-sha")
    _git(repo.path, "commit", "-qm", "plant a nested marker")
    snap = _snapshot(repo, _git(repo.path, "rev-parse", "HEAD"), tmp_path / "snap")
    cwd = snap / "sub"
    if through == "link":
        cwd = tmp_path / "link"
        cwd.symlink_to(snap / "sub")
    message = _refused(_sv(registry, tmp_path), cwd, f"Candidate: main@{repo.c1}")
    assert "holds no commit" in message


def test_a_snapshot_made_inside_a_snapshot_names_an_output_outside_it(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    wired_success: None,
    repo: Repo,
) -> None:
    """D1: review.sh run with TMPDIR inside a snapshot writes a true marker
    under a marked ancestor. That marker stays refused (A2), and the refusal
    names the ancestor and a TMPDIR outside it, whose snapshot is admitted."""
    outer = _snapshot(repo, repo.c1, tmp_path / "snap").resolve()
    inner = _snapshot(repo, repo.c2, outer)
    assert inner.parent == outer
    sv = _sv(registry, tmp_path)
    prompt = f"Candidate: main@{repo.c2}"
    message = _refused(sv, inner, prompt)
    assert f"inside the review.sh snapshot {outer}," in message
    assert f"TMPDIR={outer.parent} {FIX} {repo.c2} <repo>" in message
    assert f"lands outside {outer}," in message
    fixed = _snapshot(repo, repo.c2, outer.parent)
    assert outer not in fixed.resolve().parents
    assert sv.review("main", str(fixed), prompt=prompt).run_id


def test_a_sha256_snapshot_marker_is_read(
    registry: Registry, tmp_path: Path, roster_home: Path, wired_success: None
) -> None:
    """N6: review.sh writes 64 hex for a SHA-256 repository."""
    path = tmp_path / "repo256"
    path.mkdir()
    _git(path, "init", "-q", "-b", "main", "--object-format=sha256")
    (path / "f.txt").write_text("one\n")
    _git(path, "add", ".")
    _git(path, "commit", "-qm", "one")
    head = _git(path, "rev-parse", "HEAD")
    assert len(head) == 64
    snap = _snapshot(Repo(path, head, head), head, tmp_path / "snap")
    accepted = _sv(registry, tmp_path).review(
        "main", str(snap), prompt=f"Candidate: main@{head[:12]}"
    )
    assert accepted.run_id


# ---------------------------------------------------------------------------
# Round 1 (B): one ASCII grammar for the Candidate line.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "template",
    [
        "Candidate: main@`{c1}`",
        'Candidate: main@"{c1}"',
        "Candidate: main@({c1})",
        "Candidate: main@**{c1}**",
        "Candidate: main@'{c1}'",
    ],
)
def test_a_mark_before_the_sha_keeps_the_lock(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    wired_success: None,
    repo: Repo,
    template: str,
) -> None:
    """B1: the stale form is refused, and the same form at the cwd commit passes."""
    sv = _sv(registry, tmp_path)
    message = _refused(sv, repo.path, template.format(c1=repo.c1))
    assert repo.c2[:12] in message
    assert f"{FIX} {repo.c1} " in message
    assert sv.review("main", str(repo.path), prompt=template.format(c1=repo.c2)).run_id


def _lookalike(sha: str) -> str:
    """The sha with one hex letter swapped for its Cyrillic look-alike."""
    at = next(i for i in range(10, 40) if sha[i] in "ace")
    return (
        sha[:at]
        + {"a": chr(0x430), "c": chr(0x441), "e": chr(0x435)}[sha[at]]
        + sha[at + 1 :]
    )


@pytest.mark.parametrize(
    "form",
    [
        "zero-width-before",
        "filler-after",
        "lookalike",
        "lookalike-7",
        "tilde",
        "caret",
        "caret-2",
    ],
)
def test_a_hex_run_outside_the_grammar_is_refused_by_name(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    wired_success: None,
    repo: Repo,
    form: str,
) -> None:
    """B2 and B4: text a reader cannot see, or a revision suffix, never passes,
    even at the tree of the commit that the hex digits name."""
    revision = {
        "zero-width-before": f"{chr(0x200B)}{repo.c2}",
        "filler-after": f"{repo.c2}{chr(0x3164)}",
        "lookalike": _lookalike(repo.c2),
        # Two runs of 3 hex digits: no run of 7 that a filter of ASCII finds.
        "lookalike-7": f"{repo.c2[:3]}{chr(0x430)}{repo.c2[3:6]}",
        "tilde": f"{repo.c2}~1",
        "caret": f"{repo.c2}^",
        "caret-2": f"{repo.c2}^2",
    }[form]
    message = _refused(
        _sv(registry, tmp_path), repo.path, f"Candidate: main@{revision}"
    )
    assert "7 to 40 hex digits" in message
    assert ascii(revision) in message
    assert repo.c2[:12] not in message.replace(ascii(revision), "")


@pytest.mark.parametrize(
    "space", [chr(0xA0), chr(0x202F), chr(0x3000), chr(0x85), chr(0x2028)]
)
def test_a_branch_with_a_unicode_space_keeps_the_lock(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    wired_success: None,
    repo: Repo,
    space: str,
) -> None:
    """B3: git allows these characters in a branch name, and plan_request.py
    writes `Candidate: {branch}@{head}`."""
    message = _refused(
        _sv(registry, tmp_path), repo.path, f"Candidate: lane{space}x@{repo.c1}"
    )
    assert repo.c2[:12] in message


@pytest.mark.parametrize(
    "brk",
    ["\r", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", chr(0x2028), chr(0x2029)],
)
def test_only_a_newline_starts_a_candidate_line(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    wired_success: None,
    head_reads: list[list[str]],
    brk: str,
) -> None:
    """B5: pasted text with another line break never makes a column-0 line."""
    plain = tmp_path / "plain"
    plain.mkdir()
    prompt = f"pasted diff{brk}Candidate: main@{'ab' * 20}\nReply."
    assert _sv(registry, tmp_path).review("main", str(plain), prompt=prompt).run_id
    assert head_reads == []
