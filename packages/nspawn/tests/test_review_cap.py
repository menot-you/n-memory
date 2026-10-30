"""Count review heads through the store, the CLI and the review consumer."""

from __future__ import annotations

import inspect
import multiprocessing
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import nott_database
import pytest
from registry_reset import store

from nspawn import driver
from nspawn import supervisor as sup
from nspawn.registry import Registry
from nspawn.review_cap import InvalidCandidate


def sha(n: int, size: int = 40) -> str:
    return f"{n:0{size}x}"


def admit(
    registry: Registry, n: int, subject: str = "repo/topic", revision: str | None = None
) -> dict[str, Any]:
    assert hasattr(registry, "admit_review_head"), (
        "The registry must count review heads before dispatch."
    )
    return registry.admit_review_head(subject, sha(n), revision)


@pytest.fixture
def registry(tmp_path: Path):
    with Registry(store(), "review-cap") as opened:
        opened.upgrade()
        yield opened


def test_fourth_head_is_admitted(registry: Registry) -> None:
    for n in range(1, 6):
        answer = admit(registry, n)
        assert (answer["heads"], answer["counted"]) == (n, True), (
            "Review rounds have no limit: each new head must be admitted and counted."
        )
    assert answer["budget"] == 1 and "cap" not in answer, (
        "The answer keeps budget 1 for the hook readback and names no limit."
    )


def test_same_head_is_counted_once(registry: Registry) -> None:
    for n in range(1, 5):
        admit(registry, n)
    assert admit(registry, 2)["counted"] is False, (
        "A retry of a counted head must not consume another head."
    )
    assert admit(registry, 2)["heads"] == 4, "All readers of one head share its count."


def test_malformed_spec_revision_refuses_before_write(registry: Registry) -> None:
    malformed = ["not-a-sha", "", "a" * 40, "a" * 63, "a" * 65, "g" * 64]
    observed = []
    for index, revision in enumerate(malformed):
        subject = f"probe/malformed-{index}"
        refused = False
        try:
            admit(registry, index + 1, subject, revision)
        except InvalidCandidate:
            refused = True
        rows = nott_database.query(
            registry.connection,
            "SELECT COUNT(*) FROM review_heads WHERE subject = ?",
            (subject,),
        )[0][0]
        observed.append((revision, refused, rows))

    for n in range(1, 5):
        answer = admit(registry, n, "probe/valid-revision", "A" * 64)
        assert (answer["heads"], answer["counted"], answer["budget"]) == (n, True, 1), (
            "Valid revisions must admit each distinct head, including the fourth."
        )
        assert "cap" not in answer
    saved = nott_database.query(
        registry.connection,
        "SELECT spec_revision FROM review_heads WHERE subject = ?",
        ("probe/valid-revision",),
    )
    assert len(saved) == 4 and all(row[0] == "a" * 64 for row in saved)
    retry = admit(registry, 1, "probe/valid-revision", "a" * 64)
    assert (retry["heads"], retry["counted"]) == (4, False)
    omitted = admit(registry, 1, "probe/no-revision")
    assert (omitted["heads"], omitted["counted"]) == (1, True)
    assert observed == [(revision, True, 0) for revision in malformed], (
        "Malformed spec_revision must raise InvalidCandidate before inserting "
        f"review_heads; observed (revision, typed_refusal, rows) = {observed!r}"
    )


@pytest.mark.parametrize(
    "revision", ["not-a-sha", "", "a" * 40, "a" * 63, "a" * 65, "g" * 64]
)
def test_malformed_spec_revision_refuses_counted_head(
    registry: Registry, revision: str
) -> None:
    admit(registry, 1, revision="a" * 64)
    with pytest.raises(InvalidCandidate):
        admit(registry, 1, revision=revision)
    rows = nott_database.query(
        registry.connection, "SELECT candidate, spec_revision FROM review_heads"
    )
    assert [(row[0], row[1]) for row in rows] == [(sha(1), "a" * 64)], (
        "A malformed revision must refuse a retry without changing its saved head."
    )


@pytest.mark.parametrize("subject", ["main", "master", "origin/main", "origin/master"])
def test_base_branch_is_not_a_subject(registry: Registry, subject: str) -> None:
    assert hasattr(registry, "admit_review_head"), (
        "A base branch must not identify an artifact."
    )
    with pytest.raises(ValueError, match="artifact"):
        admit(registry, 1, subject)


@pytest.mark.parametrize(
    "case", ["pair", "hex", "prompt", "marker", "marker-tail", "symlink", "two-specs"]
)
def test_candidate_must_match_the_brief(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    assert "candidate" in inspect.signature(sup.Supervisor.review).parameters, (
        "The review consumer must validate candidate fields."
    )
    monkeypatch.setattr(sup, "_validate_review_worktree_branch", lambda *args: None)
    monkeypatch.setattr(
        sup,
        "read_roster",
        lambda cwd: pytest.fail("An invalid candidate must stop before the roster."),
    )
    marker = tmp_path / ".nott-review-sha"
    if case == "marker":
        marker.write_text(sha(9))
    elif case == "marker-tail":
        marker.write_text(sha(1) + " " * 80 + "other")
    elif case == "symlink":
        (tmp_path / "other").write_text(sha(1))
        marker.symlink_to(tmp_path / "other")
    prompt = "Candidate: repo/topic@" + sha(1)
    if case == "prompt":
        prompt = "No candidate here"
    if case == "two-specs":
        prompt += "\nSpec revision: " + sha(90, 64) + "\nSpec revision: " + sha(91, 64)
    with pytest.raises(sup.InvalidRunRequest, match=".+") as refused:
        sup.Supervisor.review(
            SimpleNamespace(_registry=registry),
            "topic",
            str(tmp_path),
            subject="repo/topic",
            candidate=None if case == "pair" else "bad" if case == "hex" else sha(1),
            prompt=prompt,
        )
    assert type(refused.value).__name__ == "ReviewCandidateInvalid", (
        "The caller must receive a typed candidate refusal."
    )


def _race(path: str, n: int, barrier: Any, output: Any) -> None:
    with Registry(Path(path), f"race-{n}") as opened:
        barrier.wait()
        try:
            output.put(admit(opened, n)["heads"])
        except ValueError:
            output.put(None)


def test_concurrent_admissions_count_each_head(registry: Registry) -> None:
    for n in range(1, 4):
        admit(registry, n)
    context = multiprocessing.get_context("spawn")
    barrier, output = context.Barrier(2), context.Queue()
    processes = [
        context.Process(target=_race, args=(str(registry.path), n, barrier, output))
        for n in (4, 5)
    ]
    for process in processes:
        process.start()
    try:
        results = {output.get(timeout=20) for _ in processes}
        assert results == {4, 5}, (
            "Two concurrent new heads must both be admitted with distinct counts."
        )
    finally:
        for process in processes:
            process.join(timeout=20)
            assert process.exitcode == 0, (
                "Each competing ledger process must exit cleanly."
            )
        output.close()
        output.join_thread()


def test_fingerprint_names_fields_only_when_present(tmp_path: Path) -> None:
    kwargs = {"cwd": str(tmp_path), "branch": "topic", "prompt": sha(1)}
    before = driver.request_fingerprint(
        "nspawn_review_start", kwargs, group=None, strict=False
    )
    after = driver.request_fingerprint(
        "nspawn_review_start",
        {**kwargs, "subject": "repo/topic", "candidate": sha(1)},
        group=None,
        strict=False,
    )
    assert before != after, (
        "The idempotency fingerprint must bind subject and candidate."
    )
    assert before == driver.request_fingerprint(
        "nspawn_review_start",
        {**kwargs, "subject": None, "candidate": None},
        group=None,
        strict=False,
    ), "Absent opt-in fields must preserve the old fingerprint."


def test_upgrade_keeps_rows(registry: Registry) -> None:
    tables = nott_database.query(
        registry.connection,
        "SELECT name FROM sqlite_schema WHERE name = 'review_heads'",
    )
    assert tables, "A v7 upgrade must add the review ledger."
    from nott_database import write

    with write(registry.connection):
        registry.connection.execute("DROP TABLE review_heads").close()
    before = nott_database.query(registry.connection, "SELECT * FROM instances")
    assert registry.upgrade() == 7, (
        "The additive ledger must not change the schema version."
    )
    assert (
        nott_database.query(registry.connection, "SELECT * FROM instances") == before
    ), "An additive upgrade must preserve existing rows."
    assert nott_database.query(
        registry.connection,
        "SELECT name FROM sqlite_schema WHERE name = 'review_heads'",
    ), "Opening an old v7 store must restore the ledger table."


def test_review_without_fields_never_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sup, "_validate_review_worktree_branch", lambda *args: None)
    monkeypatch.setattr(sup, "read_roster", lambda cwd: object())
    bare = SimpleNamespace(_witness_admit=lambda *args, **kwargs: "accepted")
    assert sup.Supervisor.review(bare, "topic", str(tmp_path)) == "accepted", (
        "A legacy review must need no ledger."
    )


def test_record_survives_later_admission_failure(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert "candidate" in inspect.signature(sup.Supervisor.review).parameters, (
        "The ledger must commit before later admission checks."
    )
    monkeypatch.setattr(sup, "_validate_review_worktree_branch", lambda *args: None)

    def fail(cwd: str) -> None:
        raise RuntimeError("later admission failure")

    monkeypatch.setattr(sup, "read_roster", fail)
    with pytest.raises(RuntimeError, match="later admission failure"):
        sup.Supervisor.review(
            SimpleNamespace(_registry=registry),
            "topic",
            str(tmp_path),
            subject="repo/topic",
            candidate=sha(1),
            prompt=sha(1),
        )
    assert admit(registry, 1)["counted"] is False, (
        "A later failure must not erase a counted head."
    )


def test_hook_reads_the_real_cli_ledger(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json
    import shlex
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[3]
    binary = tmp_path / "apps/nspawn/current/bin/nspawn"
    binary.parent.mkdir(parents=True)
    binary.write_text(
        "#!/bin/sh\nexec " + shlex.quote(sys.executable) + ' -m nspawn.cli "$@"\n'
    )
    binary.chmod(0o755)
    monkeypatch.setenv("NOTT_MCP_APPS_DIR", str(tmp_path / "apps"))
    for n in range(1, 4):
        result = subprocess.run(
            [str(binary), "review", "admit", "--stdin"],
            input=f"Candidate: repo/topic@{sha(n)}\n",
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, (
            f"The real CLI must admit head {n}: {result.stderr}"
        )
    payload = {
        "tool_name": "Agent",
        "tool_input": {
            "subagent_type": "nott:python-reviewer",
            "prompt": "Candidate: repo/topic@" + sha(4),
        },
    }
    result = subprocess.run(
        [sys.executable, str(root / "plugin/hooks/review-cap.py")],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0 and "repo/topic head 4" in result.stdout, (
        f"The delivered hook must admit and count a fourth head: {result.stderr}"
    )
    payload["tool_input"]["prompt"] = "Candidate: acceptance/fresh@" + sha(5)
    for attempt in range(2):
        result = subprocess.run(
            [sys.executable, str(root / "plugin/hooks/review-cap.py")],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0 and "acceptance/fresh head 1" in result.stdout, (
            f"A fresh head and its retry must be admitted and counted once, attempt {attempt}: {result.stderr}"
        )
