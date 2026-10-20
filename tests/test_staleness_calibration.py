"""Falsifiers for calibration of the `stale_if` evaluator.

A TRIGGERED verdict must be grounded in the condition's prose. When in doubt,
UNDECIDABLE: not deciding is reversible; fencing a true capsule is invisible.
Each test here bites a site of the product (mutants listed in PR).
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from storefile import sample_capsule_id

from nmemory.capsule import (
    AuthorityClass,
    Capsule,
    Freshness,
    Provenance,
    Scope,
    sha256_hex,
)
from nmemory.staleness import (
    StalenessClass,
    StalenessVerdict,
    SystemRunner,
    evaluate_all,
    evaluate_condition,
    extract_git_sha,
    names_disappearance,
    project_is_this_repository,
)
from nmemory.store import Store


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, str, str]:
    """Repository with two-commit `origin/main`: (root, old sha, tip)."""
    root = tmp_path / "tree"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / "apps").mkdir()
    (root / "apps" / "x.py").write_text("1\n")
    (root / "apps" / "old.py").write_text("old\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "um")
    old = _git(root, "rev-parse", "HEAD")
    _git(root, "rm", "-q", "apps/old.py")
    _git(root, "commit", "-q", "-m", "dois")
    tip = _git(root, "rev-parse", "HEAD")
    _git(root, "update-ref", "refs/remotes/origin/main", tip)
    _git(root, "remote", "add", "origin", "https://github.com/example/project.git")
    return root, old, tip


@dataclass
class _Scope:
    project_id: str


@dataclass
class _Capsule:
    scope: _Scope


@dataclass
class _Stored:
    capsule: _Capsule


class _Capsules:
    def __init__(self, projects: dict[str, str]) -> None:
        self.projects = projects

    def get(self, capsule_id: str) -> _Stored | None:
        p = self.projects.get(capsule_id)
        return None if p is None else _Stored(_Capsule(_Scope(p)))

    def get_tombstone(self, capsule_id: str) -> None:
        return None


class _Store:
    def __init__(self, projects: dict[str, str]) -> None:
        self.capsules = _Capsules(projects)


def _runner(root: Path, projects: dict[str, str] | None = None) -> SystemRunner:
    return SystemRunner(root, _Store(projects or {"cap-1": "project"}))  # type: ignore[arg-type]


# ---- defect 1: check_sha did not read condition ---------------------------------


def test_sha_reverted_prose_does_not_trigger_on_mere_advance(
    repo: tuple[Path, str, str],
) -> None:
    root, old, _tip = repo
    v, _ = _runner(root).check_sha(old[:8], f"se {old[:8]} for revertido")
    assert v == StalenessVerdict.UNDECIDABLE


def test_sha_lane_head_moves_off_is_not_about_main(
    repo: tuple[Path, str, str],
) -> None:
    root, old, _tip = repo
    v, _ = _runner(root).check_sha(old[:8], f"lane/x head moves off {old[:8]}")
    assert v == StalenessVerdict.UNDECIDABLE


def test_sha_main_moves_past_triggers_with_basis(
    repo: tuple[Path, str, str],
) -> None:
    root, old, tip = repo
    v, obs = _runner(root).check_sha(old[:8], f"origin/main moves past {old[:8]}")
    assert v == StalenessVerdict.TRIGGERED
    assert obs is not None and tip[:9] in obs
    v2, _ = _runner(root).check_sha(tip[:8], f"origin/main moves past {tip[:8]}")
    assert v2 == StalenessVerdict.NOT_TRIGGERED


def test_sha_extractor_refuses_all_digit_words() -> None:
    assert extract_git_sha("stale after 20260918 if main moves") is None
    assert extract_git_sha("stale after 2026091a if main moves") == "2026091a"


# ---- defect 2: check_pr reads condition and repository -----------------------


def _fake_gh(state: str) -> Any:
    real = subprocess.run

    def run(cmd: list[str], **kw: Any) -> Any:
        if cmd[:3] == ["gh", "pr", "view"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout=json.dumps({"state": state}), stderr=""
            )
        return real(cmd, **kw)

    return run


@pytest.mark.parametrize("mode", ["purged", "redacted"])
def test_pr_audit_continues_after_tombstoned_capsule(
    repo: tuple[Path, str, str], monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    root, _, _ = repo
    now = datetime(2026, 10, 1, tzinfo=UTC)
    store = Store.open_disposable_test_store()
    ids: list[str] = []
    for label, pr in [("forgotten", 512), ("live", 513)]:
        content = f"PR audit claim for the {label} capsule."
        cid, _ = store.append(
            Capsule(
                content=content,
                provenance=Provenance(
                    source="test:staleness-tombstone",
                    anchor=f"tests/test_staleness_calibration.py:{label}",
                    source_hash=sha256_hex(content.encode()),
                ),
                confidence=0.9,
                freshness=Freshness(valid_from=now),
                scope=Scope(project_id="project"),
                authority_class=AuthorityClass.USER_STATED,
                instruction_taint=False,
            ),
            now,
        )
        store.epistemics.set_epistemic(cid, None, None, f"PR #{pr} merges", now)
        ids.append(cid)
    forgotten_id, live_id = ids
    store.capsules.forget_capsule(forgotten_id, mode, "test", b"k" * 32, now)
    conditions = store.epistemics.all_stale_if()
    assert conditions == [
        (forgotten_id, "PR #512 merges"),
        (live_id, "PR #513 merges"),
    ]
    monkeypatch.setattr(subprocess, "run", _fake_gh("MERGED"))
    runner = SystemRunner(root, store)

    report = evaluate_all(conditions, runner)

    assert report.total_evaluated == report.resolvable_count == 2
    assert report.undecidable_count == report.triggered_count == 1
    assert report.not_triggered_count == report.prose_count == 0
    forgotten_hit, live_hit = report.hits
    assert forgotten_hit.capsule_id == forgotten_id
    assert forgotten_hit.verdict is StalenessVerdict.UNDECIDABLE
    assert forgotten_hit.observed_state is None
    assert live_hit.capsule_id == live_id
    assert live_hit.verdict is StalenessVerdict.TRIGGERED
    assert live_hit.observed_state == "MERGED"
    assert runner.pr_cache == {513: "MERGED"}


@pytest.mark.parametrize(
    ("condition", "state", "expected"),
    [
        ("se o #512 for revertido", "MERGED", StalenessVerdict.UNDECIDABLE),
        ("se o #512 fechar sem merge", "MERGED", StalenessVerdict.UNDECIDABLE),
        ("se o #512 fechar sem merge", "CLOSED", StalenessVerdict.TRIGGERED),
        ("PR #512 merges", "MERGED", StalenessVerdict.TRIGGERED),
        ("PR #512 merges", "CLOSED", StalenessVerdict.UNDECIDABLE),
        ("PR #512 merged or closed", "CLOSED", StalenessVerdict.TRIGGERED),
        ("PR #512 head changes", "MERGED", StalenessVerdict.UNDECIDABLE),
        ("PR #512 merges", "OPEN", StalenessVerdict.NOT_TRIGGERED),
    ],
)
def test_pr_verdict_follows_the_prose(
    repo: tuple[Path, str, str],
    monkeypatch: pytest.MonkeyPatch,
    condition: str,
    state: str,
    expected: StalenessVerdict,
) -> None:
    root, _, _ = repo
    monkeypatch.setattr(subprocess, "run", _fake_gh(state))
    v, _ = _runner(root).check_pr(512, condition, "cap-1")
    assert v == expected


def test_pr_from_another_repository_is_undecidable(
    repo: tuple[Path, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _, _ = repo
    monkeypatch.setattr(subprocess, "run", _fake_gh("MERGED"))
    r = _runner(root, {"cap-1": "other/tech", sample_capsule_id(2): "project/nmemory"})
    v, obs = r.check_pr(512, "PR #512 merges", "cap-1")
    assert v == StalenessVerdict.UNDECIDABLE
    assert obs is not None and "repositório" in obs
    v2, _ = r.check_pr(512, "PR #512 merges", sample_capsule_id(2))
    assert v2 == StalenessVerdict.TRIGGERED
    v3, _ = r.check_pr(512, "PR #512 merges", sample_capsule_id(9))
    assert v3 == StalenessVerdict.UNDECIDABLE


def test_path_removed_in_pr_is_classified_path_not_pr(
    repo: tuple[Path, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _, _ = repo
    monkeypatch.setattr(subprocess, "run", _fake_gh("CLOSED"))
    hit = evaluate_condition(
        "cap-1", "se apps/x.py for removido no #690", _runner(root)
    )
    assert hit.class_ == StalenessClass.PATH
    assert hit.verdict == StalenessVerdict.NOT_TRIGGERED


# ---- defect 3: check_path asks whether tree ever followed the path --


def test_path_never_of_this_tree_is_undecidable(
    repo: tuple[Path, str, str],
) -> None:
    root, _, _ = repo
    v, _ = _runner(root).check_path("scratchpad/run.md", "scratchpad/run.md is deleted")
    assert v == StalenessVerdict.UNDECIDABLE


def test_path_the_tree_lost_triggers(repo: tuple[Path, str, str]) -> None:
    root, _, _ = repo
    v, obs = _runner(root).check_path("apps/old.py", "apps/old.py is deleted")
    assert (v, obs) == (StalenessVerdict.TRIGGERED, "gone")


def test_path_disappearance_word_must_be_in_the_path_clause(
    repo: tuple[Path, str, str],
) -> None:
    root, _, _ = repo
    v, _ = _runner(root).check_path(
        "apps/old.py", "landing/ is removed, or apps/old.py changes its scope"
    )
    assert v == StalenessVerdict.UNDECIDABLE


# ---- defect 4: fence by word, in both directions -------------------------


@pytest.mark.parametrize(
    "condition",
    ["assumir o gate", "presumir verde", "consumir a fila", "the dropdown changes"],
)
def test_disappearance_does_not_match_substrings(condition: str) -> None:
    assert not names_disappearance(condition)


@pytest.mark.parametrize(
    "condition",
    [
        "o ficheiro ancorado desaparecer",
        "docs/x.md desapareceu",
        "src/a.rs is deleted",
        "tools/x.sh drops its filter",
        "o ramo sumir",
    ],
)
def test_disappearance_matches_whole_words(condition: str) -> None:
    assert names_disappearance(condition)


def test_branch_disappearance_word_must_be_in_the_branch_clause(
    repo: tuple[Path, str, str],
) -> None:
    root, _, _ = repo
    v, _ = _runner(root).check_branch(
        "feat/x", "landing/ is removed, or feat/x changes base"
    )
    assert v == StalenessVerdict.UNDECIDABLE


def test_path_without_readable_history_is_undecidable(tmp_path: Path) -> None:
    root = tmp_path / "broken"
    (root / ".git").mkdir(parents=True)  # has `.git`, but git does not read history
    v, _ = _runner(root).check_path("apps/x.py", "apps/x.py is deleted")
    assert v == StalenessVerdict.UNDECIDABLE


def test_sha_naming_main_without_movement_is_undecidable(
    repo: tuple[Path, str, str],
) -> None:
    root, old, _tip = repo
    v, _ = _runner(root).check_sha(
        old[:8], f"the review stays pinned at {old[:8]} on main"
    )
    assert v == StalenessVerdict.UNDECIDABLE


# ---- survivors of 2026-09-19 mutants round ------------------------


def test_sha_main_moving_words_must_be_in_the_sha_clause(
    repo: tuple[Path, str, str],
) -> None:
    root, old, _tip = repo
    v, _ = _runner(root).check_sha(
        old[:8], f"lane/x head moves off {old[:8]}, or origin/main moves past the gate"
    )
    assert v == StalenessVerdict.UNDECIDABLE


def test_project_merely_containing_the_repository_name_is_not_it() -> None:
    assert not project_is_this_repository("menot-you/pynott-fork", "menot-you/pynott")
    assert project_is_this_repository("pynott/nmemory", "menot-you/pynott")


def test_or_without_comma_still_separates_clauses(
    repo: tuple[Path, str, str],
) -> None:
    # Conservative reading: "feat/x rebased or dropped" also returns UNDECIDABLE
    # (5 capsules in live store on 2026-09-19); an `or` between subjects is the case
    # the fence exists not to trigger on.
    root, _, _ = repo
    v, _ = _runner(root).check_branch(
        "feat/x", "landing/ is removed or feat/x changes base"
    )
    assert v == StalenessVerdict.UNDECIDABLE
