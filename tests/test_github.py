from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from child_env import child_env
from storefile import default_store_path, sample_capsule_id

from nmemory.capsule import (
    AuthorityClass,
    Capsule,
    Freshness,
    Provenance,
    Scope,
)
from nmemory.classify import CandidateKind, ClassificationScope
from nmemory.config import github_table
from nmemory.github import (
    ConfigError,
    EffortError,
    SyncConfig,
    apply_exit_code,
    load_config,
    read_store,
    resolve_effort,
    sync,
    verify,
)
from nmemory.github_client import GhCommandError, GhParseError
from nmemory.github_plan import (
    BODY_FOOTER,
    SUB_ISSUE_CEILING,
    CloseReason,
    DesiredKind,
    DesiredState,
    Finding,
    GhComment,
    GhEvent,
    GhIssue,
    GhIssueRef,
    IssuePatch,
    IssueTypes,
    desired_set,
    parse_marker,
    search_phrase_of,
    sync_owned_labels,
    title_of,
    verify_exit_code,
)
from nmemory.relation import RelationKind
from nmemory.store import Store

NOW = datetime(2026, 8, 23, 12, 0, 0, tzinfo=UTC)
EARLIER = datetime(2026, 8, 22, 12, 0, 0, tzinfo=UTC)
LABEL = "nmemory-sync"
REPO = "acme/repo"


def cap(content: str) -> Capsule:
    return cap_at(content, "docs/plan.md:1")


def cap_at(content: str, anchor: str) -> Capsule:
    from nmemory.capsule import sha256_hex

    return Capsule(
        content=content,
        provenance=Provenance(
            source="session",
            anchor=anchor,
            source_hash=sha256_hex(content.encode()),
        ),
        confidence=0.9,
        freshness=Freshness(valid_from=NOW),
        scope=Scope(project_id="nott"),
        authority_class=AuthorityClass.USER_STATED,
        instruction_taint=False,
    )


def body_of(content: str, cap_id: str) -> str:
    return f"{content}\n\n<!-- {LABEL} {cap_id} -->\n{BODY_FOOTER}"


def make_issue(
    number: int,
    issue_id: int,
    title: str,
    body: str,
    kind: str = "task",
    state: str = "open",
    issue_type: str | None = None,
) -> GhIssue:
    t = (
        issue_type
        if issue_type is not None
        else (
            DesiredKind(kind).as_issue_type()
            if kind in {k.value for k in DesiredKind}
            else "Task"
        )
    )
    return GhIssue(
        number=number,
        id=issue_id,
        title=title,
        body=body,
        state=state,
        state_reason=None,
        issue_type=t,
        labels=[LABEL, kind],
        is_pull_request=False,
    )


class FixtureGh:
    """Stateful test double for GhClient."""

    def __init__(self, issues: list[GhIssue] | None = None) -> None:
        self.issues: list[GhIssue] = list(issues or [])
        self.sub_issues: dict[int, list[GhIssueRef]] = {}
        self.blocked_by: dict[int, list[GhIssueRef]] = {}
        self.calls: list[str] = []
        self.fail_list: bool = False
        self.fail_ops: dict[str, str] = {}
        self.minted: int = 0
        self.issue_types: set[str] | None = None
        self.login = "nott-sync"
        self.timeline: dict[int, list[GhEvent]] = {}
        self.comments: list[GhComment] = []
        self.editors: dict[int, tuple[str, str]] = {}

    def record(self, op: str) -> None:
        self.calls.append(op)

    def _with_summaries(self, iss: GhIssue) -> GhIssue:
        out = make_issue(
            iss.number,
            iss.id,
            iss.title,
            iss.body,
            state=iss.state,
            issue_type=iss.issue_type,
        )
        out.labels = list(iss.labels)
        out.is_pull_request = iss.is_pull_request
        out.state_reason = iss.state_reason
        out.sub_issues_total = len(self.sub_issues.get(iss.number, []))
        out.blocked_by_total = len(self.blocked_by.get(iss.number, []))
        out.updated_at = iss.updated_at
        return out

    def viewer_login(self) -> str:
        self.record("viewer_login")
        return self.login

    def list_timeline(self, repo: str, number: int) -> list[GhEvent]:
        self.record(f"list_timeline #{number}")
        if "list_timeline" in self.fail_ops:
            raise GhCommandError(
                "list_timeline", "exit status: 1", self.fail_ops["list_timeline"]
            )
        return list(self.timeline.get(number, []))

    def list_comments(self, repo: str, since: str | None) -> list[GhComment]:
        self.record(f"list_comments {since}")
        return list(self.comments)

    def body_editor(self, repo: str, number: int) -> tuple[str, str] | None:
        self.record(f"body_editor #{number}")
        return self.editors.get(number)

    def list_issues(self, repo: str, label: str) -> list[GhIssue]:
        self.record("list_issues")
        if self.fail_list:
            raise GhCommandError(
                "list_issues", "exit status: 1", "gh: HTTP 403: rate limit exceeded"
            )
        if repo != REPO:
            return []
        return [self._with_summaries(i) for i in self.issues if label in i.labels]

    def get_issue(self, repo: str, number: int) -> GhIssue:
        self.record("get_issue")
        if "get_issue" in self.fail_ops:
            raise GhCommandError(
                "get_issue", "exit status: 1", self.fail_ops["get_issue"]
            )
        found = next((i for i in self.issues if i.number == number), None)
        if not found:
            raise GhCommandError(
                "get_issue", "exit status: 1", f"no fixture issue {number}"
            )
        return found

    def search_marker(self, repo: str, phrase: str) -> list[GhIssueRef]:
        self.record(f"search_marker {phrase}")
        return [
            GhIssueRef(number=i.number, id=i.id)
            for i in self.issues
            if phrase in i.body
        ]

    def list_sub_issues(self, repo: str, number: int) -> list[GhIssueRef]:
        self.record("list_sub_issues")
        return list(self.sub_issues.get(number, []))

    def list_blocked_by(self, repo: str, number: int) -> list[GhIssueRef]:
        self.record("list_blocked_by")
        return list(self.blocked_by.get(number, []))

    def list_issue_types(self, repo: str) -> set[str]:
        self.record("list_issue_types")
        if "list_issue_types" in self.fail_ops:
            raise GhCommandError(
                "list_issue_types",
                "exit status: 1",
                self.fail_ops["list_issue_types"],
            )
        if self.issue_types is not None:
            return set(self.issue_types)
        return {"Epic", "Task", "Plan", "Brainstorm"}

    def ensure_label(self, repo: str, label: str) -> None:
        self.record("ensure_label")
        if "ensure_label" in self.fail_ops:
            raise GhCommandError(
                "ensure_label", "exit status: 1", self.fail_ops["ensure_label"]
            )

    def create_issue(
        self,
        repo: str,
        title: str,
        body: str,
        labels: list[str],
        issue_type: str | None,
    ) -> GhIssueRef:
        named = parse_marker(LABEL, body) or "?"
        self.record(f"create_issue {named} type:{issue_type or '-'}")
        if "create_issue" in self.fail_ops:
            raise GhCommandError(
                "create_issue", "exit status: 1", self.fail_ops["create_issue"]
            )
        self.minted += 1
        num = 500 + self.minted
        iid = 95000 + self.minted
        iss = GhIssue(
            number=num,
            id=iid,
            title=title,
            body=body,
            state="open",
            state_reason=None,
            issue_type=issue_type,
            labels=list(labels),
            is_pull_request=False,
        )
        self.issues.append(iss)
        return GhIssueRef(number=num, id=iid)

    def update_issue(self, repo: str, number: int, patch: IssuePatch) -> GhIssue:
        self.record(f"update_issue #{number}")
        if "update_issue" in self.fail_ops:
            raise GhCommandError(
                "update_issue", "exit status: 1", self.fail_ops["update_issue"]
            )
        iss = next((i for i in self.issues if i.number == number), None)
        if not iss:
            raise GhCommandError(
                "update_issue", "exit status: 1", f"no issue #{number}"
            )
        if patch.title is not None:
            iss.title = patch.title
        if patch.body is not None:
            iss.body = patch.body
        if patch.issue_type is not None:
            iss.issue_type = patch.issue_type
        if patch.state is not None:
            iss.state = patch.state.wire_state()
            iss.state_reason = patch.state.wire_reason()
        return iss

    def add_labels(self, repo: str, number: int, labels: list[str]) -> None:
        self.record(f"add_labels #{number} {','.join(labels)}")
        iss = next((i for i in self.issues if i.number == number), None)
        if iss:
            for l in labels:
                if l not in iss.labels:
                    iss.labels.append(l)

    def remove_label(self, repo: str, number: int, label: str) -> None:
        self.record(f"remove_label #{number} {label}")
        iss = next((i for i in self.issues if i.number == number), None)
        if iss:
            iss.labels = [l for l in iss.labels if l != label]

    def add_sub_issue(self, repo: str, parent: int, child_id: int) -> None:
        self.record(f"add_sub_issue #{parent}<-{child_id}")
        if any(
            number != parent and any(c.id == child_id for c in children)
            for number, children in self.sub_issues.items()
        ):
            raise GhCommandError(
                "add_sub_issue", "422", "The child has another parent."
            )
        child = next((i for i in self.issues if i.id == child_id), None)
        if child:
            self.sub_issues.setdefault(parent, []).append(
                GhIssueRef(number=child.number, id=child.id)
            )

    def remove_sub_issue(self, repo: str, parent: int, child_id: int) -> None:
        self.record(f"remove_sub_issue #{parent}<-{child_id}")
        children = self.sub_issues.get(parent, [])
        if not any(c.id == child_id for c in children):
            raise GhCommandError(
                "remove_sub_issue", "404", "The relation does not exist."
            )
        self.sub_issues[parent] = [c for c in children if c.id != child_id]

    def add_blocked_by(self, repo: str, number: int, blocker_id: int) -> None:
        self.record(f"add_blocked_by #{number}<-{blocker_id}")
        blocker = next((i for i in self.issues if i.id == blocker_id), None)
        if blocker:
            self.blocked_by.setdefault(number, []).append(
                GhIssueRef(number=blocker.number, id=blocker.id)
            )


def seeded_store() -> Store:
    store = Store.open_disposable_test_store()
    contents = [
        "Ship the GitHub sync\n\nThe epic body.",
        "Write the detector\n\nDetector body.",
        "Wire the timer\n\nTimer body.",
        "Detector shipped and observed.",
        "Retired approach\n\nOld body.",
        "Retired approach shipped.",
        "New approach\n\nReplacement body.",
        "Draft the docs\n\nDocs body.",
    ]
    for c in contents:
        store.append(cap(c), NOW)

    store.set_birth_classification(
        "cap-1", CandidateKind.EPIC, ClassificationScope.PROJECT, NOW
    )
    for cid in [
        sample_capsule_id(2),
        sample_capsule_id(3),
        sample_capsule_id(5),
        sample_capsule_id(8),
    ]:
        store.set_birth_classification(
            cid, CandidateKind.TASK, ClassificationScope.PROJECT, NOW
        )
    for cid in [sample_capsule_id(4), sample_capsule_id(6), sample_capsule_id(7)]:
        store.set_birth_classification(
            cid, CandidateKind.FACT, ClassificationScope.PROJECT, NOW
        )

    store.add_relation(
        RelationKind.BLOCKS, sample_capsule_id(2), sample_capsule_id(3), now=NOW
    )
    store.add_relation(RelationKind.GROUNDED_IN, sample_capsule_id(2), "cap-1", now=NOW)
    store.add_relation(RelationKind.GROUNDED_IN, sample_capsule_id(3), "cap-1", now=NOW)
    store.add_relation(RelationKind.GROUNDED_IN, sample_capsule_id(8), "cap-1", now=NOW)
    store.add_relation(
        RelationKind.WITNESSES, sample_capsule_id(4), sample_capsule_id(2), now=NOW
    )
    store.add_relation(
        RelationKind.WITNESSES, sample_capsule_id(6), sample_capsule_id(5), now=NOW
    )
    store.add_relation(
        RelationKind.SUPERSEDES, sample_capsule_id(7), sample_capsule_id(5), now=NOW
    )
    return store


def config() -> SyncConfig:
    return SyncConfig(
        repo=REPO,
        label=LABEL,
        project="nott",
    )


# --------------------------------------------------------------- unit tests ---


def test_marker_parser_right_anchored() -> None:
    assert (
        parse_marker(LABEL, f"text\n\n<!-- {LABEL} cap-1 -->\n{BODY_FOOTER}") == "cap-1"
    )
    # Quotes an earlier marker in body:
    body = f"Docs say: <!-- {LABEL} {sample_capsule_id(999)} -->\n\n<!-- {LABEL} {sample_capsule_id(42)} -->\n{BODY_FOOTER}"
    assert parse_marker(LABEL, body) == sample_capsule_id(42)
    assert parse_marker(LABEL, "no marker here") is None
    assert parse_marker(LABEL, f"<!-- {LABEL}  -->") is None
    assert parse_marker(LABEL, f"<!-- {LABEL} cap with spaces -->") is None


def test_search_phrase_is_colon_free() -> None:
    phrase = search_phrase_of(LABEL, sample_capsule_id(123))
    assert ":" not in phrase
    assert phrase == f"{LABEL} {sample_capsule_id(123)}"


def test_title_of_slug_and_fallback() -> None:
    assert title_of("my-task-slug", "Some headline\n\nBody") == "my-task-slug"
    assert title_of(None, "Short headline") == "Short headline"
    long_h = "A" * 60
    assert title_of(None, long_h) == "A" * 48 + "…"


def test_sync_owned_labels() -> None:
    owned = sync_owned_labels("nmemory-sync")
    assert "nmemory-sync" in owned
    assert "epic" in owned
    assert "task" in owned
    assert "plan" in owned
    assert "brainstorm" in owned


def test_load_config(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        """
[github]
repo = "org/my-repo"
label = "sync-label"
project = "my-proj"
"""
    )
    cfg = load_config(cfg_file)
    assert cfg.repo == "org/my-repo"
    assert cfg.label == "sync-label"
    assert cfg.project == "my-proj"
    # Ausente e o grafo inteiro: o comportamento de sempre.
    assert cfg.efforts == ()

    # Missing table
    bad_file = tmp_path / "bad.toml"
    bad_file.write_text("[other]\nfoo = 'bar'")
    with pytest.raises(ConfigError):
        load_config(bad_file)


def test_load_config_efforts(tmp_path: Path) -> None:
    head = "[github]\nrepo = 'org/r'\nlabel = 'l'\nproject = 'p'\n"

    ok = tmp_path / "ok.toml"
    ok.write_text(
        head
        + (
            "efforts = ['"
            + sample_capsule_id(1)
            + "', ' "
            + sample_capsule_id(2)
            + " ']\n"
        )
    )
    assert load_config(ok).efforts == ("cap-1", sample_capsule_id(2))

    # A string is not a list: `str([...])` would give silent garbage.
    not_list = tmp_path / "not_list.toml"
    not_list.write_text(head + "efforts = 'cap-1'\n")
    with pytest.raises(ConfigError, match="must be a list"):
        load_config(not_list)

    # The EMPTY list is refused: "fence to nothing" and "no fence" differ
    # on the whole problem, and guessing one of them is the difference
    # between nine issues and two thousand.
    empty = tmp_path / "empty.toml"
    empty.write_text(head + "efforts = []\n")
    with pytest.raises(ConfigError, match="must not be empty"):
        load_config(empty)

    blank = tmp_path / "blank.toml"
    blank.write_text(head + "efforts = ['cap-1', '  ']\n")
    with pytest.raises(ConfigError, match="non-empty epic cap-ids"):
        load_config(blank)


def test_desired_set_and_diff_convergence() -> None:
    store = seeded_store()
    cfg = config()
    types = IssueTypes({"Epic", "Task", "Plan", "Brainstorm"})
    snap = read_store(store, cfg.project)
    dset = desired_set(snap, cfg.label, types)

    assert "cap-1" in dset.issues
    assert sample_capsule_id(2) in dset.issues
    assert sample_capsule_id(3) in dset.issues
    assert sample_capsule_id(5) in dset.issues
    assert sample_capsule_id(8) in dset.issues
    assert dset.issues[sample_capsule_id(2)].state == DesiredState.closed_state(
        CloseReason.COMPLETED
    )
    assert dset.issues[sample_capsule_id(5)].state == DesiredState.closed_state(
        CloseReason.NOT_PLANNED
    )
    assert dset.issues[sample_capsule_id(3)].state == DesiredState.open_state()
    assert dset.issues[sample_capsule_id(3)].blockers == {sample_capsule_id(2)}
    assert dset.issues[sample_capsule_id(3)].parent == "cap-1"


def test_verify_read_only_and_detects_drift() -> None:
    store = seeded_store()
    cfg = config()

    gh = FixtureGh(
        [
            make_issue(
                101,
                9101,
                "Wrong Title",
                body_of("Ship the GitHub sync\n\nThe epic body.", "cap-1"),
                "epic",
                "open",
            ),
            make_issue(
                102,
                9102,
                "write-the-detector",
                body_of("Write the detector\n\nDetector body.", sample_capsule_id(2)),
                "task",
                "open",
            ),
            make_issue(
                103,
                9103,
                "wire-the-timer",
                body_of("Wire the timer\n\nTimer body.", sample_capsule_id(3)),
                "task",
                "open",
            ),
            make_issue(
                105,
                9105,
                "retired-approach",
                body_of("Retired approach\n\nOld body.", sample_capsule_id(5)),
                "task",
                "open",
            ),
        ]
    )

    plan = verify(store, gh, cfg)
    assert not plan.is_converged()
    assert verify_exit_code(plan) == 3

    # Check that verify wrote nothing:
    assert len(gh.calls) > 0
    assert not any(c.startswith("create_issue") for c in gh.calls)
    assert not any(c.startswith("update_issue") for c in gh.calls)
    assert not any(c.startswith("ensure_label") for c in gh.calls)

    assert any(c.cap_id == sample_capsule_id(8) for c in plan.creates)
    assert any(rt.number == 101 for rt in plan.retitles)
    assert any(cl.number == 102 for cl in plan.closes)


def test_sync_applies_creates_updates_closes_and_converges() -> None:
    store = seeded_store()
    cfg = config()
    gh = FixtureGh()

    # Tick 1: empty github -> creates all, attaches structure, closes done/superseded
    summary = sync(store, gh, cfg, max_writes=50, now=NOW)
    assert summary.verdict() == "CLEAN"
    assert apply_exit_code(summary) == 0
    assert summary.creates == 5
    assert summary.closes == 2

    # Verify audit row written in store:
    audits = store.audit.list_audit()
    assert any(
        a.actor == "github-sync" and a.action == "apply" and "creates=5" in a.reason
        for a in audits
    )

    # Tick 2: already converged -> 0 writes
    plan2 = verify(store, gh, cfg)
    assert plan2.is_converged()
    assert verify_exit_code(plan2) == 0


def _capsule_and_relation_counts(store: Store) -> tuple[int, int]:
    with store.db.connection() as conn, conn.cursor() as cur:
        capsules = cur.execute("SELECT count(*) FROM capsules").fetchone()[0]
        relations = cur.execute("SELECT count(*) FROM relations").fetchone()[0]
    return int(capsules), int(relations)


def test_sync_never_reads_an_epic_back_into_the_store() -> None:
    """The pull reads task issues only: a close, a reopen or an unmarked issue
    elsewhere is reported as before, and nothing of it is written to the store."""
    store = Store.open_disposable_test_store()
    store.append(cap("Epic to do\n\nBody."), NOW)
    store.append(cap("Done epic\n\nBody."), NOW)
    store.append(cap("Observation witness\n\nProof."), NOW)
    for cid in ("cap-1", sample_capsule_id(2)):
        store.set_birth_classification(
            cid, CandidateKind.EPIC, ClassificationScope.PROJECT, NOW
        )
    store.set_birth_classification(
        sample_capsule_id(3), CandidateKind.FACT, ClassificationScope.PROJECT, NOW
    )
    store.add_relation(
        RelationKind.WITNESSES, sample_capsule_id(3), sample_capsule_id(2), now=NOW
    )

    closed = make_issue(
        101,
        9101,
        "epic-to-do",
        body_of("Epic to do\n\nBody.", "cap-1"),
        "epic",
        "closed",
    )
    reopened = make_issue(
        102,
        9102,
        "done-epic",
        body_of("Done epic\n\nBody.", sample_capsule_id(2)),
        "epic",
        "open",
    )
    reopened.state_reason = "reopened"
    human = make_issue(201, 9201, "Feature idea", "Detailed body from human")
    human.labels = [LABEL]
    gh = FixtureGh([closed, reopened, human])
    gh.timeline = {
        101: [GhEvent("1", "closed", "owner", "2026-08-22T10:00:00Z")],
        102: [GhEvent("2", "reopened", "owner", "2026-08-22T10:00:00Z")],
    }
    before = _capsule_and_relation_counts(store)

    summary = sync(store, gh, config(), max_writes=10, now=NOW)

    assert _capsule_and_relation_counts(store) == before
    assert summary.pull_candidates == 1
    assert summary.adopt_candidates == 1
    assert any(isinstance(f, Finding.ReopenedWhileDone) for f in summary.findings)
    assert gh.issues[2].body == "Detailed body from human"
    assert not any(c.startswith("update_issue #201") for c in gh.calls)
    assert not any(c.startswith("list_timeline") for c in gh.calls)


def _drifted_gh() -> FixtureGh:

    return FixtureGh(
        [
            make_issue(
                101,
                9101,
                "Wrong Title",
                body_of("Ship the GitHub sync\n\nThe epic body.", "cap-1"),
                "epic",
                "open",
            ),
            make_issue(
                102,
                9102,
                "write-the-detector",
                body_of("Write the detector\n\nDetector body.", sample_capsule_id(2)),
                "task",
                "open",
            ),
            make_issue(
                103,
                9103,
                "wire-the-timer",
                body_of("Wire the timer\n\nTimer body.", sample_capsule_id(3)),
                "task",
                "open",
            ),
            make_issue(
                105,
                9105,
                "retired-approach",
                body_of("Retired approach\n\nOld body.", sample_capsule_id(5)),
                "task",
                "open",
            ),
        ]
    )


def test_configured_efforts_fence_the_tick() -> None:
    store = seeded_store()
    plan = verify(store, _drifted_gh(), replace(config(), efforts=("cap-1",)))

    assert {c.cap_id for c in plan.creates} == {sample_capsule_id(8)}
    assert not any(cl.cap_id == sample_capsule_id(5) for cl in plan.closes)
    assert not any(sample_capsule_id(5) in str(f) for f in plan.findings)


def test_absent_efforts_mirrors_the_whole_graph() -> None:
    store = seeded_store()
    plan = verify(store, _drifted_gh(), config())

    assert {c.cap_id for c in plan.creates} == {sample_capsule_id(8)}
    assert any(cl.cap_id == sample_capsule_id(5) for cl in plan.closes)


def test_efforts_union_two_epics() -> None:
    """Two disjoint epics fence by the UNION.

    This is the test that catches the intersection: a narrowing by fence,
    instead of one over the union, would leave the set empty and give zero
    writes with a clean verdict.
    """
    store = Store.open_disposable_test_store()
    for content in [
        "Epic A\n\nA body.",
        "Task under A\n\nA task.",
        "Epic B\n\nB body.",
        "Task under B\n\nB task.",
        "Orphan task\n\nNo parent.",
    ]:
        store.append(cap(content), NOW)
    for cid in ("cap-1", sample_capsule_id(3)):
        store.set_birth_classification(
            cid, CandidateKind.EPIC, ClassificationScope.PROJECT, NOW
        )
    for cid in (sample_capsule_id(2), sample_capsule_id(4), sample_capsule_id(5)):
        store.set_birth_classification(
            cid, CandidateKind.TASK, ClassificationScope.PROJECT, NOW
        )
    store.add_relation(RelationKind.PART_OF, sample_capsule_id(2), "cap-1", now=NOW)
    store.add_relation(
        RelationKind.PART_OF, sample_capsule_id(4), sample_capsule_id(3), now=NOW
    )

    plan = verify(
        store,
        FixtureGh([]),
        replace(config(), efforts=("cap-1", sample_capsule_id(3))),
    )
    assert {c.cap_id for c in plan.creates} == {
        "cap-1",
        sample_capsule_id(2),
        sample_capsule_id(3),
        sample_capsule_id(4),
    }


def test_a_cerca_nao_atribui_filho_a_um_pai_de_fora() -> None:
    """Structural writes also respect the fence.

    `edge_writes` used to resolve the parent from the map of ALL issues, not
    the fenced set: a task inside with a parent outside would do
    `add_sub_issue` on the outside issue, which the fence promises to leave
    intact.
    """
    store = Store.open_disposable_test_store()
    for content in [
        "Epic A\n\nA body.",
        "Epic P\n\nP body.",
        "Task T\n\nT body.",
    ]:
        store.append(cap(content), NOW)
    for cid in ("cap-1", sample_capsule_id(2)):
        store.set_birth_classification(
            cid, CandidateKind.EPIC, ClassificationScope.PROJECT, NOW
        )
    store.set_birth_classification(
        sample_capsule_id(3), CandidateKind.TASK, ClassificationScope.PROJECT, NOW
    )
    # T enters the fence via A, but `grounded_in` wins over `part_of` when
    # picking the parent.
    store.add_relation(RelationKind.PART_OF, sample_capsule_id(3), "cap-1", now=NOW)
    store.add_relation(
        RelationKind.GROUNDED_IN,
        sample_capsule_id(3),
        sample_capsule_id(2),
        now=NOW,
    )

    gh = FixtureGh(
        [
            make_issue(
                201, 9201, "epic-a", body_of("Epic A\n\nA body.", "cap-1"), "epic"
            ),
            make_issue(
                202,
                9202,
                "epic-p",
                body_of("Epic P\n\nP body.", sample_capsule_id(2)),
                "epic",
            ),
            make_issue(
                203,
                9203,
                "task-t",
                body_of("Task T\n\nT body.", sample_capsule_id(3)),
                "task",
            ),
        ]
    )
    plan = verify(store, gh, replace(config(), efforts=("cap-1",)))
    assert not any(a.parent_cap == sample_capsule_id(2) for a in plan.attaches), (
        f"issue #202, outside the fence, would gain a sub-issue: {plan.attaches}"
    )


def test_um_effort_de_outro_projeto_recusa_em_vez_de_convergir() -> None:
    """Fencing to an epic the project does not contain would give zero work and exit 0.

    The worst failure mode of this feature: the fence stays declared in a
    file, nobody touches it, and the sync goes on doing nothing forever
    while claiming it has converged.
    """
    store = Store.open_disposable_test_store()
    store.append(cap_at("Epic de outro projeto\n\nCorpo.", "docs/plan.md:1"), NOW)
    store.append(cap_at("Task dela\n\nCorpo.", "docs/plan.md:1"), NOW)
    store.set_birth_classification(
        "cap-1", CandidateKind.EPIC, ClassificationScope.PROJECT, NOW
    )
    store.set_birth_classification(
        sample_capsule_id(2), CandidateKind.TASK, ClassificationScope.PROJECT, NOW
    )
    store.add_relation(RelationKind.PART_OF, sample_capsule_id(2), "cap-1", now=NOW)

    # The sync's project is not that of these capsules.
    alheio = replace(config(), project="outro-projeto-qualquer", efforts=("cap-1",))
    with pytest.raises(EffortError, match="outro-projeto-qualquer"):
        verify(store, FixtureGh([]), alheio)


def test_effort_fence_narrowing() -> None:
    store = seeded_store()

    fence = resolve_effort(store, "cap-1")
    assert fence.epic_id == "cap-1"
    assert sample_capsule_id(2) in fence.ids
    assert sample_capsule_id(3) in fence.ids
    assert sample_capsule_id(8) in fence.ids

    # Unclassified or non-epic effort fails
    with pytest.raises(EffortError):
        resolve_effort(store, sample_capsule_id(2))


def test_sub_issue_ceiling_finding() -> None:
    store = Store.open_disposable_test_store()
    store.append(cap("Big Epic"), NOW)  # cap-1
    store.set_birth_classification(
        "cap-1", CandidateKind.EPIC, ClassificationScope.PROJECT, NOW
    )

    # 105 children under cap-1
    for i in range(2, 107):
        cid = f"cap-{i}"
        store.append(cap(f"Task {i}"), NOW)
        store.set_birth_classification(
            cid, CandidateKind.TASK, ClassificationScope.PROJECT, NOW
        )
        store.add_relation(RelationKind.GROUNDED_IN, cid, "cap-1", now=NOW)

    cfg = config()
    gh = FixtureGh()
    # Apply with high max writes
    summary = sync(store, gh, cfg, max_writes=200, now=NOW)
    assert any(isinstance(f, Finding.SubIssueCeiling) for f in summary.findings)
    ceiling_finding = next(
        f for f in summary.findings if isinstance(f, Finding.SubIssueCeiling)
    )
    assert ceiling_finding.refused == 5
    assert ceiling_finding.ceiling == SUB_ISSUE_CEILING


def test_max_writes_truncation() -> None:
    store = seeded_store()
    cfg = config()
    gh = FixtureGh()

    # Sync with budget of 2
    summary = sync(store, gh, cfg, max_writes=2, now=NOW)
    assert summary.partial is True
    assert summary.writes == 2
    assert summary.verdict() == "PARTIAL"


def _cli(
    args: list[str], env_extra: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    env = child_env()
    env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "nmemory.cli", "github", "sync", *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        cwd=Path(__file__).resolve().parents[1],
    )


def _host_toml(tmp_path: Path, extra: str = "") -> Path:
    cfg = tmp_path / "memory.toml"
    cfg.write_text(extra)
    return cfg


def test_a_aplicacao_sem_cerca_recusa_e_nomeia_a_cura(tmp_path: Path) -> None:
    """Without a declared effort the WRITE is refused; the read is not.

    Run the real route, not a helper: it is `commands/github.py` that decides, and a
    test against the helper would stay green with the call site broken.
    """
    cfg = _host_toml(tmp_path)
    flags = ["--repo", "o/r", "--label", "l", "--project", "p"]
    env = {"NOTT_NMEMORY_CONFIG": str(cfg)}

    out = _cli(flags, env)
    assert out.returncode == 1
    assert "no effort declared" in out.stderr
    assert "~/.nott/memory.toml" in out.stderr
    assert "Traceback" not in out.stderr


def test_um_effort_que_nao_resolve_sai_em_1_e_nao_em_traceback(
    tmp_path: Path,
) -> None:
    default_store_path()  # the store the CLI opens has to exist
    cfg = _host_toml(tmp_path)
    flags = ["--repo", "o/r", "--label", "l", "--project", "p"]
    out = _cli(
        [*flags, "--verify", "--effort", sample_capsule_id(999999)],
        {"NOTT_NMEMORY_CONFIG": str(cfg)},
    )
    assert out.returncode == 1
    assert "Traceback" not in out.stderr
    assert sample_capsule_id(999999) in out.stderr
    # And the message no longer names a flag the config does not use.
    assert "--effort" not in out.stderr


def test_efforts_vazio_no_ficheiro_do_host_recusa(tmp_path: Path) -> None:
    cfg = _host_toml(tmp_path, "\n[github]\nefforts = []\n")
    out = _cli(
        ["--repo", "o/r", "--label", "l", "--project", "p"],
        {"NOTT_NMEMORY_CONFIG": str(cfg)},
    )
    assert out.returncode == 1
    assert "must not be empty" in out.stderr
    assert "Traceback" not in out.stderr


def test_um_ficheiro_ilegivel_sai_nomeado_e_nao_em_traceback(tmp_path: Path) -> None:
    mau = tmp_path / "bytes.toml"
    mau.write_bytes(b"[github]\nrepo = '\xff\xfe nao e utf-8'\n")
    with pytest.raises(ConfigError, match="cannot read config"):
        load_config(mau)


def test_a_cerca_nao_se_mistura_com_o_repo_do_outro_ficheiro(tmp_path: Path) -> None:
    """ONE file wins, wholly.

    Mixing field by field allowed taking the `repo` from one file and the
    FENCE from the other, and a fence made for repository B applied to A
    writes in the wrong place without anyone seeing it.
    """
    do_repo = tmp_path / "repo.toml"
    do_repo.write_text("[github]\nrepo = 'org/A'\nlabel = 'l'\nproject = 'p'\n")
    host = tmp_path / "memory.toml"
    host.write_text(
        "[github]\nrepo = 'org/B'\nefforts = ['" + sample_capsule_id(9) + "']\n"
    )

    # The repository's file does not declare efforts, so the fence is
    # empty: the host's does NOT stick to its repo.
    assert load_config(do_repo).repo == "org/A"
    assert load_config(do_repo).efforts == ()
    assert github_table(host)["repo"] == "org/B"
    assert github_table(host)["efforts"] == [sample_capsule_id(9)]


def test_load_config_refuses_a_retired_key_by_name(tmp_path: Path) -> None:
    cfg = tmp_path / "old.toml"
    cfg.write_text(
        "[github]\nrepo = 'o/r'\nlabel = 'l'\nbot_login = 'b'\nproject = 'p'\n"
    )
    with pytest.raises(ConfigError, match="bot_login"):
        load_config(cfg)


# ------------------------------------------------------------- the pull ---

LATER = datetime(2026, 8, 24, 12, 0, 0, tzinfo=UTC)
OWNER_AT = "2026-08-23T10:00:00Z"
#: The shared vector: the host's parser reads this same line
#: (harness/tests/acp/test_board_host.py).
LINE = (
    "GitHub change: acme/repo#7 comment added by @octo-cat at "
    "2026-10-13T12:00:00Z (comment 4242)"
)


def test_the_proposal_line_is_the_shared_vector() -> None:
    from nmemory.github_plan import proposal_line

    assert (
        proposal_line(
            "acme/repo",
            7,
            "comment",
            "added",
            "octo-cat",
            "2026-10-13T12:00:00Z",
            "comment",
            "4242",
        )
        == LINE
    )


def pull_store() -> Store:

    store = Store.open_disposable_test_store()
    for content in (
        "Write the detector\n\nDetector body.",
        "Done task\n\nBody.",
        "Observation witness\n\nProof.",
        "Ship the epic\n\nEpic body.",
    ):
        store.append(cap(content), EARLIER)
    for cid in ("cap-1", sample_capsule_id(2)):
        store.set_birth_classification(
            cid, CandidateKind.TASK, ClassificationScope.PROJECT, EARLIER
        )
    store.set_birth_classification(
        sample_capsule_id(3), CandidateKind.FACT, ClassificationScope.PROJECT, EARLIER
    )
    store.set_birth_classification(
        sample_capsule_id(4), CandidateKind.EPIC, ClassificationScope.PROJECT, EARLIER
    )
    store.add_relation(
        RelationKind.WITNESSES, sample_capsule_id(3), sample_capsule_id(2), now=EARLIER
    )
    return store


def pull_gh() -> FixtureGh:
    """GitHub as the sync left it: every issue converged, no change by anyone."""
    issues = [
        make_issue(
            101,
            9101,
            "write-the-detector",
            body_of("Write the detector\n\nDetector body.", "cap-1"),
        ),
        make_issue(
            102, 9102, "done-task", body_of("Done task\n\nBody.", sample_capsule_id(2))
        ),
        make_issue(
            104,
            9104,
            "ship-the-epic",
            body_of("Ship the epic\n\nEpic body.", sample_capsule_id(4)),
            "epic",
        ),
    ]
    issues[1].state, issues[1].state_reason = "closed", "completed"
    for issue in issues:
        issue.updated_at = "2026-08-23T11:00:00Z"
    return FixtureGh(issues)


def owner_edits_101(gh: FixtureGh) -> None:
    """The owner changes every field of #101 on GitHub and comments on it."""
    issue = gh.issues[0]
    issue.state, issue.state_reason = "closed", "not_planned"
    issue.title = "the-owner-title"
    issue.body = f"O detector muda.\r\n\r\n<!-- {LABEL} cap-1 -->\r\n{BODY_FOOTER}"
    issue.labels = [LABEL, "bug"]
    gh.timeline[101] = [
        GhEvent("11", "closed", "owner", OWNER_AT),
        GhEvent(
            "12",
            "renamed",
            "owner",
            OWNER_AT,
            rename_from="write-the-detector",
            rename_to="the-owner-title",
        ),
        GhEvent("13", "unlabeled", "owner", OWNER_AT, label="task"),
        GhEvent("14", "labeled", "owner", OWNER_AT, label="bug"),
    ]
    gh.editors[101] = ("owner", OWNER_AT)
    gh.comments = [
        GhComment("15", 101, "owner", OWNER_AT, "O comentario muda."),
        GhComment("16", 104, "owner", OWNER_AT, "A comment on the epic."),
    ]


def proposals(store: Store) -> dict[str, tuple[str, str | None]]:
    """Each staged GitHub change: its line 1 and its source quote."""
    out: dict[str, tuple[str, str | None]] = {}
    for stored in store.capsules.list():
        if stored.capsule.provenance.source != "github-pull":
            continue
        quote = store.epistemics.get_source_quote(stored.id)
        out[stored.id] = (stored.capsule.content, quote and quote["quote"])
    return out


def test_each_github_change_to_a_task_is_staged_as_a_tainted_proposal() -> None:
    store, gh = pull_store(), pull_gh()
    owner_edits_101(gh)

    summary = sync(store, gh, config(), max_writes=30, now=NOW)

    staged = proposals(store)
    lines = sorted(line for line, _ in staged.values())
    at = f"by @owner at {OWNER_AT}"
    assert lines == sorted(
        f"GitHub change: {REPO}#101 {change} {at} ({source})"
        for change, source in [
            ("state closed-not_planned", "event 11"),
            ("title renamed", "event 12"),
            ("labels unlabeled", "event 13"),
            ("label labeled", "event 14"),
            ("body edited", f"edit {OWNER_AT}"),
            ("comment added", "comment 15"),
        ]
    )
    quotes = {line.split()[3]: quote for line, quote in staged.values()}
    assert quotes == {
        "state": "not_planned",
        "title": "the-owner-title",
        "labels": "task",
        "label": "bug",
        "body": "O detector muda.",
        "comment": "O comentario muda.",
    }
    for cid in staged:
        stored = store.capsules.get(cid)
        assert stored is not None
        assert stored.capsule.authority_class == AuthorityClass.EXTERNALLY_IMPORTED
        assert stored.capsule.instruction_taint is True
        assert store.reviews.review_verdict(cid) == "proposed"
        assert any(
            r.kind == RelationKind.ABOUT and r.to_id == "cap-1"
            for r in store.relations.list_relations_for(cid)
        )
    # Every waiting field is held: the board state is not written over it.
    assert not any(c.startswith("update_issue #101") for c in gh.calls)
    assert not any(
        c.startswith(("add_labels #101", "remove_label #101")) for c in gh.calls
    )
    assert {"held #101 state", "held #101 title", "held #101 body"} <= set(
        summary.render().splitlines()
    )
    assert summary.writes == 6 and summary.verdict() == "CLEAN"

    again = sync(store, gh, config(), max_writes=30, now=LATER)
    assert proposals(store) == staged
    assert again.writes == 0


def review(store: Store, cid: str, verdict: str) -> None:
    from nmemory.server import MemoryServer

    response = MemoryServer(store=store).call_tool(
        "nmemory_proposal_review",
        {"capsule_id": cid, "verdict": verdict, "actor": "owner"},
    )
    assert not response["result"]["isError"], response


def test_a_rejected_close_reopens_and_a_rejected_reopen_closes() -> None:
    store, gh = pull_store(), pull_gh()
    gh.issues[0].state, gh.issues[0].state_reason = "closed", "completed"
    gh.issues[1].state, gh.issues[1].state_reason = "open", "reopened"
    gh.timeline[101] = [GhEvent("21", "closed", "owner", OWNER_AT)]
    gh.timeline[102] = [GhEvent("22", "reopened", "owner", OWNER_AT)]

    sync(store, gh, config(), max_writes=30, now=NOW)
    assert gh.issues[0].state == "closed" and gh.issues[1].state == "open"
    for cid in proposals(store):
        review(store, cid, "rejected")

    summary = sync(store, gh, config(), max_writes=30, now=LATER)

    assert gh.issues[0].state == "open"
    assert gh.issues[1].state == "closed"
    assert not any(isinstance(f, Finding.ReopenedWhileDone) for f in summary.findings)
    assert len(proposals(store)) == 2


def test_the_sync_reopens_its_own_close_after_a_revoked_supersede() -> None:
    store, gh = pull_store(), pull_gh()
    store.append(cap("The decision that dropped the detector."), NOW)
    store.add_relation(RelationKind.SUPERSEDES, sample_capsule_id(5), "cap-1", now=NOW)
    store.revoke_relation(
        RelationKind.SUPERSEDES,
        sample_capsule_id(5),
        "cap-1",
        actor="owner",
        reason="wrong",
        now=NOW,
    )
    gh.issues[0].state, gh.issues[0].state_reason = "closed", "not_planned"
    gh.timeline[101] = [GhEvent("31", "closed", gh.login, OWNER_AT)]

    sync(store, gh, config(), max_writes=30, now=NOW)

    assert proposals(store) == {}
    assert gh.issues[0].state == "open"


def test_changes_by_the_sync_itself_or_without_an_event_are_not_staged() -> None:
    store, gh = pull_store(), pull_gh()
    owner_edits_101(gh)
    gh.timeline[101] = [replace(event, actor=gh.login) for event in gh.timeline[101]]
    gh.editors[101] = (gh.login, OWNER_AT)
    gh.comments = [replace(c, actor=gh.login) for c in gh.comments]
    # A title change no renamed event explains (a slug change) is the board's.
    gh.timeline[101] = [e for e in gh.timeline[101] if e.event != "renamed"]

    sync(store, gh, config(), max_writes=30, now=NOW)

    assert proposals(store) == {}
    issue = gh.issues[0]
    assert issue.title == "write-the-detector" and issue.state == "open"


def test_a_footer_edited_on_github_is_staged_whole() -> None:
    store, gh = pull_store(), pull_gh()
    gh.issues[
        0
    ].body = (
        f"Write the detector\n\nDetector body.\n\n<!-- {LABEL} cap-1 -->\nMine now."
    )
    gh.editors[101] = ("owner", OWNER_AT)

    sync(store, gh, config(), max_writes=30, now=NOW)

    [(line, quote)] = proposals(store).values()
    assert " body edited-footer by @owner " in line
    assert quote == gh.issues[0].body


def test_staged_proposals_count_against_the_write_budget() -> None:
    store, gh = pull_store(), pull_gh()
    owner_edits_101(gh)

    summary = sync(store, gh, config(), max_writes=2, now=NOW)

    assert len(proposals(store)) == 2
    assert summary.writes == 2 and summary.partial
    assert not any(a for a in store.audit.list_audit() if "pulled_at=" in a.reason)


def test_a_403_on_the_timeline_stops_the_run() -> None:
    store, gh = pull_store(), pull_gh()
    owner_edits_101(gh)
    gh.fail_ops["list_timeline"] = "gh: HTTP 403: rate limit exceeded"

    summary = sync(store, gh, config(), max_writes=30, now=NOW)

    assert summary.fatal is not None and apply_exit_code(summary) == 1
    assert proposals(store) == {}
    assert not any(c.startswith(("update_issue", "add_labels")) for c in gh.calls)


def test_the_watermark_bounds_the_reads_and_moves_only_on_a_complete_run() -> None:
    store, gh = pull_store(), pull_gh()

    sync(store, gh, config(), max_writes=30, now=NOW)

    stamped = [
        a.reason
        for a in store.audit.list_audit(subject=REPO)
        if a.actor == "github-sync"
    ]
    assert stamped and "pulled_at=2026-08-23T12:00:00" in stamped[0]
    assert [c for c in gh.calls if c.startswith("list_comments")] == [
        "list_comments 2026-08-22T12:00:00Z"
    ]
    assert {c for c in gh.calls if c.startswith("list_timeline")} == {
        "list_timeline #101",
        "list_timeline #102",
    }

    gh.calls.clear()
    gh.issues[1].updated_at = "2026-08-24T09:00:00Z"
    sync(store, gh, config(), max_writes=30, now=LATER)

    assert [c for c in gh.calls if c.startswith("list_timeline")] == [
        "list_timeline #102"
    ]
    assert [c for c in gh.calls if c.startswith("list_comments")] == [
        "list_comments 2026-08-23T12:00:00Z"
    ]


def test_a_fenced_run_does_not_advance_the_watermark_for_other_efforts_and_chained_renames_stage() -> (
    None
):
    store, gh = pull_store(), pull_gh()
    store.append(cap("Second epic\n\nEpic two."), EARLIER)
    store.append(cap("Second task\n\nTask two."), EARLIER)
    store.set_birth_classification(
        sample_capsule_id(5), CandidateKind.EPIC, ClassificationScope.PROJECT, EARLIER
    )
    store.set_birth_classification(
        sample_capsule_id(6), CandidateKind.TASK, ClassificationScope.PROJECT, EARLIER
    )
    store.add_relation(RelationKind.PART_OF, "cap-1", sample_capsule_id(4), now=EARLIER)
    store.add_relation(
        RelationKind.PART_OF, sample_capsule_id(6), sample_capsule_id(5), now=EARLIER
    )
    gh.issues.append(
        make_issue(
            105,
            9105,
            "second-epic",
            body_of("Second epic\n\nEpic two.", sample_capsule_id(5)),
            "epic",
        )
    )
    issue_106 = make_issue(
        106,
        9106,
        "second-task",
        body_of("Second task\n\nTask two.", sample_capsule_id(6)),
    )
    issue_106.state, issue_106.state_reason = "closed", "not_planned"
    issue_106.updated_at = "2026-08-23T11:00:00Z"
    gh.issues.append(issue_106)
    gh.timeline[106] = [GhEvent("61", "closed", "owner", OWNER_AT)]
    gh.sub_issues[104] = [GhIssueRef(101, 9101)]
    gh.sub_issues[105] = [GhIssueRef(106, 9106)]

    gh.issues[0].title = "final-owner-title"
    gh.timeline[101] = [
        GhEvent(
            "41",
            "renamed",
            "owner",
            OWNER_AT,
            rename_from="write-the-detector",
            rename_to="middle-title",
        ),
        GhEvent(
            "42",
            "renamed",
            "owner",
            OWNER_AT,
            rename_from="middle-title",
            rename_to="final-owner-title",
        ),
    ]

    sync(
        store,
        gh,
        replace(config(), efforts=(sample_capsule_id(4),)),
        max_writes=30,
        now=NOW,
    )
    assert any(
        "title renamed" in line and quote == "final-owner-title"
        for line, quote in proposals(store).values()
    )

    sync(
        store,
        gh,
        replace(config(), efforts=(sample_capsule_id(4), sample_capsule_id(5))),
        max_writes=30,
        now=LATER,
    )
    assert any(
        "#106 state closed-not_planned" in line for line, _ in proposals(store).values()
    )
    assert gh.issues[-1].state == "closed"


def test_unratified_closing_edge_in_store_does_not_close_github_issue() -> None:
    store, gh = pull_store(), pull_gh()
    staged_id = store.append(cap("Staged close"), NOW)
    store.set_birth_classification(
        staged_id, CandidateKind.FACT, ClassificationScope.PROJECT, NOW
    )
    store.reviews.append_review_event(
        staged_id, "proposed", "staged on ingest", "owner", NOW
    )
    store.relations.assert_edge(
        "witnesses", staged_id, "cap-1", "2026-08-23T12:00:00Z", "manual"
    )

    sync(store, gh, config(), max_writes=30, now=NOW)

    assert gh.issues[0].state == "open"


def test_non_contiguous_rename_events_are_not_staged() -> None:
    store, gh = pull_store(), pull_gh()
    gh.issues[0].title = "third-title"
    gh.timeline[101] = [
        GhEvent(
            "21",
            "renamed",
            "owner",
            "2026-08-23T10:00:00Z",
            rename_from="write-the-detector",
            rename_to="second-title",
        ),
        GhEvent(
            "22",
            "renamed",
            "owner",
            "2026-08-23T10:01:00Z",
            rename_from="other-from",
            rename_to="other-to",
        ),
        GhEvent(
            "23",
            "renamed",
            "owner",
            "2026-08-23T10:02:00Z",
            rename_from="second-title",
            rename_to="third-title",
        ),
    ]

    sync(store, gh, config(), max_writes=30, now=NOW)

    assert proposals(store) == {}
    assert gh.issues[0].title == "write-the-detector"


def test_verify_stages_nothing() -> None:
    store, gh = pull_store(), pull_gh()
    owner_edits_101(gh)

    verify(store, gh, config())

    assert proposals(store) == {}
    assert not any(c.startswith(("viewer_login", "list_timeline")) for c in gh.calls)


def test_the_gh_client_reads_the_pull_from_github_shapes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`SubprocessGh` against a stub `gh` on PATH that answers recorded shapes."""
    from nmemory.github_client import SubprocessGh

    fixture = Path(__file__).parent / "fixtures" / "github-pull.json"
    stub = tmp_path / "gh"
    stub.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        f"answers = json.load(open({str(fixture)!r}))\n"
        "args = sys.argv[1:]\n"
        "with open(sys.argv[0] + '.log', 'a') as log:\n"
        "    log.write(json.dumps(args) + '\\n')\n"
        "if 'number=404' in args:\n"
        "    print(json.dumps({'data': {'repository': {'issue': None}}}))\n"
        "    sys.exit(0)\n"
        "key = next(a for a in args if a in answers)\n"
        "print(json.dumps(answers[key]))\n"
    )
    stub.chmod(0o700)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    gh = SubprocessGh()

    assert gh.viewer_login() == "nott-sync"
    events = gh.list_timeline("acme/repo", 7)
    assert [(e.id, e.event, e.actor) for e in events] == [
        ("11", "closed", "owner"),
        ("12", "renamed", "owner"),
        ("13", "labeled", "owner"),
        ("", "commented", None),
    ]
    assert (events[1].rename_from, events[1].rename_to) == ("old-slug", "new title")
    assert events[2].label == "bug"
    [comment] = gh.list_comments("acme/repo", "2026-10-13T00:00:00Z")
    assert comment == GhComment(
        "4242", 7, "octo-cat", "2026-10-13T12:05:00Z", "Please change this."
    )
    assert gh.body_editor("acme/repo", 7) == ("owner", "2026-10-13T10:04:00Z")
    with pytest.raises(GhParseError):
        gh.body_editor("acme/repo", 404)
    calls = [
        json.loads(line) for line in (tmp_path / "gh.log").read_text().splitlines()
    ]
    assert "since=2026-10-13T00:00:00Z" in calls[2]
    assert "number=7" in calls[3] and "owner=acme" in calls[3]


def test_a_replacement_workstream_becomes_the_childs_current_parent() -> None:
    store = pull_store()
    old, new, child = [
        store.append(cap(content), NOW)[0]
        for content in ("Old workstream", "New workstream", "Transferred child")
    ]
    for cid in (old, new, child):
        store.set_birth_classification(
            cid, CandidateKind.TASK, ClassificationScope.PROJECT, NOW
        )
    store.add_relation(RelationKind.PART_OF, child, old, now=EARLIER)
    store.add_relation(RelationKind.PART_OF, child, new, now=NOW)
    store.add_relation(RelationKind.SUPERSEDES, new, old, now=NOW)

    desired = desired_set(read_store(store, "nott"), LABEL, IssueTypes())
    assert desired.issues[child].parent == new
    assert not any(isinstance(f, Finding.MultipleParents) for f in desired.findings)


def test_parent_replacement_requires_a_dropped_parent_inside_the_fence() -> None:
    from nmemory.github_plan import ObservedRelations, edge_writes

    store = pull_store()
    old, new, child = [
        store.append(cap(content), NOW)[0]
        for content in ("Dropped parent", "Approved replacement", "Moved child")
    ]
    for cid in (old, new, child):
        store.set_birth_classification(
            cid, CandidateKind.TASK, ClassificationScope.PROJECT, NOW
        )
    store.add_relation(RelationKind.SUPERSEDES, new, old, now=NOW)
    store.add_relation(RelationKind.PART_OF, child, new, now=NOW)
    desired = desired_set(read_store(store, "nott"), LABEL, IssueTypes()).issues
    refs = {
        old: GhIssueRef(number=7, id=70),
        new: GhIssueRef(number=8, id=80),
        child: GhIssueRef(number=9, id=90),
    }
    observed = ObservedRelations(sub_issues={7: {refs[child]}})
    attaches, _, _ = edge_writes(desired, refs, observed)
    assert len(attaches) == 1
    assert attaches[0].previous_parent == 7

    from nmemory.github import Applier, ApplySummary

    gh = FixtureGh([make_issue(9, 90, "Moved child", body_of("Moved child", child))])
    gh.sub_issues = {7: [refs[child]]}
    summary = ApplySummary()
    applier = Applier(gh, REPO, LABEL, 10, summary, IssueTypes(), refs)
    applier.structure(desired, observed)
    assert gh.sub_issues[7] == []
    assert gh.sub_issues[8] == [refs[child]]
    assert summary.attaches == 1 and not summary.errors
    applier.structure(desired, ObservedRelations(sub_issues={8: {refs[child]}}))
    assert summary.writes == 2

    prior = desired.pop(old)
    attaches, _, _ = edge_writes(desired, refs, observed)
    assert len(attaches) == 1
    assert attaches[0].previous_parent is None

    desired[old] = replace(prior, state=DesiredState.open_state())
    attaches, _, _ = edge_writes(desired, refs, observed)
    assert len(attaches) == 1
    assert attaches[0].previous_parent is None

    del desired[old]
    attaches, _, _ = edge_writes(desired, refs, observed)
    assert len(attaches) == 1
    assert attaches[0].previous_parent is None


def test_the_gh_client_removes_only_the_expected_parent(tmp_path: Path) -> None:
    from nmemory.github_client import SubprocessGh

    stub = tmp_path / "gh"
    stub.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "with open(sys.argv[0] + '.log', 'w') as log:\n"
        "    json.dump(sys.argv[1:], log)\n"
    )
    stub.chmod(0o700)
    gh = SubprocessGh(str(stub))
    gh.remove_sub_issue("acme/repo", 7, 90)
    args = json.loads((tmp_path / "gh.log").read_text())
    assert "DELETE" in args
    assert "repos/acme/repo/issues/7/sub_issue" in args
    assert "sub_issue_id=90" in args
    gh.add_sub_issue("acme/repo", 8, 90)
    args = json.loads((tmp_path / "gh.log").read_text())
    assert not any(arg.startswith("replace_parent=") for arg in args)


@pytest.mark.parametrize("new_parent", [8, 10])
def test_a_changed_parent_is_not_replaced_after_the_snapshot(new_parent: int) -> None:
    from nmemory.github import Applier, ApplySummary
    from nmemory.github_plan import ObservedRelations

    store = pull_store()
    old, new, child = [
        store.append(cap(content), NOW)[0]
        for content in ("Dropped parent", "New parent", "Child with a new parent")
    ]
    for cid in (old, new, child):
        store.set_birth_classification(
            cid, CandidateKind.TASK, ClassificationScope.PROJECT, NOW
        )
    store.add_relation(RelationKind.SUPERSEDES, new, old, now=NOW)
    store.add_relation(RelationKind.PART_OF, child, new, now=NOW)
    desired = desired_set(read_store(store, "nott"), LABEL, IssueTypes()).issues
    refs = {old: GhIssueRef(7, 70), new: GhIssueRef(8, 80), child: GhIssueRef(9, 90)}
    old_snapshot = ObservedRelations(sub_issues={7: {refs[child]}})
    gh = FixtureGh([make_issue(9, 90, "Child", body_of("Child", child))])
    gh.sub_issues = {7: [], new_parent: [refs[child]]}
    summary = ApplySummary()
    Applier(gh, REPO, LABEL, 10, summary, IssueTypes(), refs).structure(
        desired, old_snapshot
    )
    assert gh.sub_issues[new_parent] == [refs[child]]
    assert not any(call.startswith("add_sub_issue") for call in gh.calls)
    assert summary.errors


@pytest.mark.parametrize("moment", ["remove", "attach"])
def test_a_parent_change_during_transfer_is_not_forced(moment: str) -> None:
    from nmemory.github import Applier, ApplySummary
    from nmemory.github_plan import ObservedRelations

    store = pull_store()
    old, new, child = [
        store.append(cap(content), NOW)[0]
        for content in ("Dropped parent", "Approved parent", "Racing child")
    ]
    for cid in (old, new, child):
        store.set_birth_classification(
            cid, CandidateKind.TASK, ClassificationScope.PROJECT, NOW
        )
    store.add_relation(RelationKind.SUPERSEDES, new, old, now=NOW)
    store.add_relation(RelationKind.PART_OF, child, new, now=NOW)
    desired = desired_set(read_store(store, "nott"), LABEL, IssueTypes()).issues
    refs = {old: GhIssueRef(7, 70), new: GhIssueRef(8, 80), child: GhIssueRef(9, 90)}
    snapshot = ObservedRelations(sub_issues={7: {refs[child]}})
    gh = FixtureGh([make_issue(9, 90, "Child", body_of("Child", child))])
    gh.sub_issues = {7: [refs[child]]}
    real = gh.remove_sub_issue if moment == "remove" else gh.add_sub_issue

    def race(repo: str, parent: int, child_id: int) -> None:
        gh.sub_issues = {7: [], 10: [refs[child]]}
        real(repo, parent, child_id)

    if moment == "remove":
        gh.remove_sub_issue = race
    else:
        gh.add_sub_issue = race
    summary = ApplySummary()
    Applier(gh, REPO, LABEL, 10, summary, IssueTypes(), refs).structure(
        desired, snapshot
    )
    assert gh.sub_issues[10] == [refs[child]]
    assert not gh.sub_issues.get(8)
    assert summary.errors
