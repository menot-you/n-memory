from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

_NOTICE_TEST = (
    "tests/test_campaign_notices.py::test_project_flag_drains_once_without_a_run"
)
_SAFE_TESTS = (
    "tests/test_errors.py::test_codes_are_unique_and_the_catalog_stays_ordered",
    "tests/test_needs.py::test_every_capability_names_the_platforms_that_owe_it",
)

_CHILD = r"""
import json
import sys
from pathlib import Path

import pytest


events_path = Path(sys.argv[1])
if sys.argv[2] == "forbid-basetemp":
    from _pytest.tmpdir import TempPathFactory

    def forbidden_basetemp(self):
        raise AssertionError("The guard must never call getbasetemp()")

    TempPathFactory.getbasetemp = forbidden_basetemp


class Recorder:
    def _record(self, event):
        with events_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event) + "\n")

    def pytest_runtest_logstart(self, nodeid, location):
        self._record({"event": "start", "nodeid": nodeid})

    def pytest_runtest_logreport(self, report):
        self._record({
            "event": "report",
            "nodeid": report.nodeid,
            "when": report.when,
            "outcome": report.outcome,
        })


raise SystemExit(pytest.main(sys.argv[3:], plugins=[Recorder()]))
"""


def _child_environment(temporary: Path) -> dict[str, str]:
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(("PYTEST_", "GIT_"))
    }
    return environment | {
        "TMPDIR": str(temporary),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
    }


def _make_checkout(parent: Path, gitfile: bool = False) -> Path:
    checkout = parent / "checkout with spaces"
    checkout.mkdir()
    command = [
        "git",
        "-c",
        "core.hooksPath=/dev/null",
        "init",
        "--quiet",
        "--template=",
    ]
    if gitfile:
        command.append(f"--separate-git-dir={parent / 'git-metadata'}")
    command.append(str(checkout))
    initialized = subprocess.run(
        command,
        cwd=parent,
        env=_child_environment(parent),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert initialized.returncode == 0, initialized.stdout + initialized.stderr
    marker = checkout / ".git"
    if gitfile:
        assert marker.is_file()
    else:
        assert marker.is_dir()
    return checkout


def _run_member_pytest(
    work: Path,
    temporary: Path,
    selection: tuple[str, ...],
    basetemp: Path | None = None,
    *,
    environment: dict[str, str | None] | None = None,
    forbid_basetemp: bool = False,
) -> tuple[subprocess.CompletedProcess[str], list[dict[str, str]]]:
    work.mkdir()
    events_path = work / "events.jsonl"
    events_path.write_text("", encoding="utf-8")
    command = [
        sys.executable,
        "-I",
        "-c",
        _CHILD,
        str(events_path),
        "forbid-basetemp" if forbid_basetemp else "allow-basetemp",
        "-q",
        "-p",
        "no:cacheprovider",
        "--color=no",
        "--tb=short",
    ]
    if basetemp is not None:
        command.append(f"--basetemp={basetemp}")
    command.extend(selection)
    child_environment = _child_environment(temporary)
    for name, value in (environment or {}).items():
        if value is None:
            child_environment.pop(name, None)
        else:
            child_environment[name] = value
    completed = subprocess.run(
        command,
        cwd=Path(__file__).resolve().parents[1],
        env=child_environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    events = [
        json.loads(line)
        for line in events_path.read_text(encoding="utf-8").splitlines()
    ]
    return completed, events


def _assert_named_path(output: str, path: Path) -> None:
    # Name the checkout root on its own, not only inside the temporary path.
    pattern = re.escape(str(path.resolve())) + r"/?(?=$|[\s'\"`),;:\]}])"
    assert re.search(pattern, output), (
        f"The refusal must name this complete path: {path.resolve()}\n{output}"
    )


def _assert_refused(
    completed: subprocess.CompletedProcess[str],
    events: list[dict[str, str]],
    temporary: Path,
    checkout: Path,
) -> None:
    output = completed.stdout + completed.stderr
    assert completed.returncode in (
        pytest.ExitCode.TESTS_FAILED,
        pytest.ExitCode.INTERRUPTED,
        pytest.ExitCode.USAGE_ERROR,
    ), f"The contaminated session must refuse, not finish normally:\n{output}"
    assert events == [], (
        "The session started a test instead of refusing its temporary directory:"
        f"\n{events}\n{output}"
    )
    assert "INTERNALERROR" not in output, output
    assert str(temporary.resolve()) in output, output
    _assert_named_path(output, checkout)
    assert "TMPDIR" in output, output
    normalized = " ".join(output.casefold().split())
    assert re.search(r"\bneutral\b", normalized), output
    assert re.search(r"\b(?:cwd|working directory)\b", normalized), output
    assert re.search(r"\boutside\b.*\bgit\b", normalized), output
    assert re.search(r"\bdisk\b", normalized), output


def _assert_running(
    completed: subprocess.CompletedProcess[str],
    events: list[dict[str, str]],
    selection: tuple[str, ...],
) -> None:
    output = completed.stdout + completed.stderr
    assert completed.returncode == pytest.ExitCode.OK, output
    started = [event["nodeid"] for event in events if event["event"] == "start"]
    assert started == list(selection), f"{events}\n{output}"
    reports = [
        (event["nodeid"], event["when"], event["outcome"])
        for event in events
        if event["event"] == "report"
    ]
    expected = [
        (nodeid, phase, "passed")
        for nodeid in selection
        for phase in ("setup", "call", "teardown")
    ]
    assert reports == expected, f"{events}\n{output}"


def _assert_quality_names_a_neutral_disk_tmpdir() -> None:
    quality = (Path(__file__).resolve().parents[1] / "docs" / "QUALITY.md").read_text(
        encoding="utf-8"
    )
    paragraphs = re.split(r"\n\s*\n", quality)
    guidance = " ".join(
        paragraph.replace("`", "") for paragraph in paragraphs if "TMPDIR" in paragraph
    )
    guidance = " ".join(guidance.casefold().split())
    assert re.search(r"\bdisk\b", guidance), quality
    assert re.search(
        r"\boutside\b.*\bgit\b.*\b(?:checkouts?|repositories|repository)\b",
        guidance,
    ), quality
    assert (
        "set tmpdir to an owned disk directory under .nott/disposable before pytest."
    ) not in guidance, quality


@pytest.mark.parametrize("marker", ("directory", "gitfile", "symlink"))
def test_checkout_tmpdir_is_refused_before_any_test(
    tmp_path: Path, marker: str
) -> None:
    checkout = _make_checkout(tmp_path, gitfile=marker == "gitfile")
    temporary = checkout / ".nott" / "disposable" / "tmp"
    temporary.mkdir(parents=True)
    if marker == "symlink":
        alias = tmp_path / "temporary alias"
        alias.symlink_to(temporary, target_is_directory=True)
        temporary = alias

    completed, events = _run_member_pytest(
        tmp_path / "refused-session",
        temporary,
        (_NOTICE_TEST,),
    )

    _assert_refused(completed, events, temporary, checkout)


def test_neutral_tmpdir_remedy_preserves_the_session(tmp_path: Path) -> None:
    checkout = _make_checkout(tmp_path)
    contaminated = checkout / ".nott" / "disposable" / "tmp"
    contaminated.mkdir(parents=True)
    neutral = tmp_path / "neutral run tmpdir"
    neutral.mkdir()
    # A descendant checkout does not make the pytest base directory a project.
    _make_checkout(neutral)
    selection = (_NOTICE_TEST, *_SAFE_TESTS)

    refused, refused_events = _run_member_pytest(
        tmp_path / "contaminated-session",
        contaminated,
        selection,
    )
    running, running_events = _run_member_pytest(
        tmp_path / "neutral-session",
        neutral,
        selection,
    )

    _assert_running(running, running_events, selection)
    _assert_refused(refused, refused_events, contaminated, checkout)
    _assert_quality_names_a_neutral_disk_tmpdir()


@pytest.mark.parametrize(
    "selection",
    (
        ("tests/test_errors.py::test_codes_are_unique_and_the_catalog_stays_ordered",),
        ("tests/test_needs.py::test_every_capability_names_the_platforms_that_owe_it",),
        (
            "tests/test_errors.py::test_codes_are_unique_and_the_catalog_stays_ordered",
            (
                "tests/test_needs.py::"
                "test_every_capability_names_the_platforms_that_owe_it"
            ),
        ),
        (
            (
                "tests/test_needs.py::"
                "test_every_capability_names_the_platforms_that_owe_it"
            ),
            "tests/test_errors.py::test_codes_are_unique_and_the_catalog_stays_ordered",
        ),
        (
            "tests/test_errors.py",
            "-k",
            "no_tmpdir_guard_probe_matches_this_name",
        ),
    ),
    ids=(
        "catalog-only",
        "capability-only",
        "catalog-first",
        "capability-first",
        "no-selection",
    ),
)
def test_refusal_does_not_depend_on_selection_or_order(
    tmp_path: Path, selection: tuple[str, ...]
) -> None:
    checkout = _make_checkout(tmp_path)
    temporary = checkout / ".nott" / "disposable" / "tmp"
    temporary.mkdir(parents=True)

    completed, events = _run_member_pytest(
        tmp_path / "selected-session",
        temporary,
        selection,
    )

    _assert_refused(completed, events, temporary, checkout)


def test_effective_basetemp_decides_the_refusal(tmp_path: Path) -> None:
    checkout = _make_checkout(tmp_path)
    contaminated = checkout / ".nott" / "disposable" / "tmp"
    contaminated.mkdir(parents=True)
    neutral = tmp_path / "neutral tmpdir"
    neutral.mkdir()
    unsafe_base = contaminated / "explicit pytest base"
    safe_base = tmp_path / "explicit neutral base"

    refused, refused_events = _run_member_pytest(
        tmp_path / "unsafe-base-session",
        neutral,
        _SAFE_TESTS,
        basetemp=unsafe_base,
    )
    inverse, inverse_events = _run_member_pytest(
        tmp_path / "safe-base-session",
        contaminated,
        _SAFE_TESTS,
        basetemp=safe_base,
    )

    _assert_refused(inverse, inverse_events, contaminated, checkout)
    _assert_refused(refused, refused_events, unsafe_base, checkout)
    _assert_named_path(refused.stdout + refused.stderr, unsafe_base)


@pytest.mark.parametrize("base_location", ("checkout", "descendant", "symlink"))
@pytest.mark.parametrize("collect_only", (False, True), ids=("run", "collect-only"))
def test_existing_checkout_basetemp_is_refused_without_deleting_files(
    tmp_path: Path, base_location: str, collect_only: bool
) -> None:
    checkout = _make_checkout(tmp_path)
    basetemp = checkout
    if base_location == "descendant":
        basetemp = checkout / "existing pytest base"
        basetemp.mkdir()
    elif base_location == "symlink":
        basetemp = tmp_path / "checkout alias"
        basetemp.symlink_to(checkout, target_is_directory=True)
    sentinel = basetemp / "keep.txt"
    sentinel.write_text("keep this checkout", encoding="utf-8")
    neutral = tmp_path / "neutral tmpdir"
    neutral.mkdir()
    selection = ("tests/test_errors.py",)
    if collect_only:
        selection = ("--collect-only", *selection)

    completed, events = _run_member_pytest(
        tmp_path / "existing-base-session",
        neutral,
        selection,
        basetemp=basetemp,
    )

    assert basetemp.is_dir(), completed.stdout + completed.stderr
    assert (checkout / ".git").is_dir(), completed.stdout + completed.stderr
    assert sentinel.read_text(encoding="utf-8") == "keep this checkout"
    assert completed.returncode == pytest.ExitCode.USAGE_ERROR
    _assert_refused(completed, events, basetemp, checkout)


@pytest.mark.parametrize("setting", ("TMPDIR", "TEMP", "TMP", "PYTEST_DEBUG_TEMPROOT"))
def test_relative_temporary_setting_is_refused(tmp_path: Path, setting: str) -> None:
    neutral = tmp_path / "neutral tmpdir"
    neutral.mkdir()
    member = Path(__file__).resolve().parents[1]
    relative = os.path.relpath(neutral, member)

    completed, events = _run_member_pytest(
        tmp_path / "relative-session",
        neutral,
        _SAFE_TESTS,
        environment={setting: relative},
    )

    output = completed.stdout + completed.stderr
    assert completed.returncode == pytest.ExitCode.USAGE_ERROR, output
    assert events == [], output
    assert setting in output, output
    _assert_named_path(output, neutral)
    assert "relative" in output.casefold(), output
    assert "absolute" in output.casefold(), output


def test_checkout_debug_temproot_is_refused(tmp_path: Path) -> None:
    checkout = _make_checkout(tmp_path)
    contaminated = checkout / "debug root"
    contaminated.mkdir()
    neutral = tmp_path / "neutral tmpdir"
    neutral.mkdir()

    completed, events = _run_member_pytest(
        tmp_path / "debug-session",
        neutral,
        _SAFE_TESTS,
        environment={"PYTEST_DEBUG_TEMPROOT": str(contaminated)},
    )

    _assert_refused(completed, events, contaminated, checkout)
    assert "PYTEST_DEBUG_TEMPROOT" in completed.stdout + completed.stderr


def test_neutral_debug_temproot_does_not_hide_checkout_tmpdir(tmp_path: Path) -> None:
    checkout = _make_checkout(tmp_path)
    contaminated = checkout / "tempfile root"
    contaminated.mkdir()
    neutral = tmp_path / "neutral debug root"
    neutral.mkdir()

    completed, events = _run_member_pytest(
        tmp_path / "tempfile-session",
        contaminated,
        _SAFE_TESTS,
        environment={"PYTEST_DEBUG_TEMPROOT": str(neutral)},
    )

    _assert_refused(completed, events, contaminated, checkout)


def test_empty_debug_temproot_uses_neutral_tmpdir(tmp_path: Path) -> None:
    neutral = tmp_path / "neutral tmpdir"
    neutral.mkdir()

    completed, events = _run_member_pytest(
        tmp_path / "empty-debug-session",
        neutral,
        ("--collect-only", "tests/test_errors.py"),
        environment={"PYTEST_DEBUG_TEMPROOT": ""},
        forbid_basetemp=True,
    )

    # Admit collection from the suite checkout without allocating a base.
    output = completed.stdout + completed.stderr
    assert completed.returncode == pytest.ExitCode.OK, output
    assert events == [], output
    assert list(neutral.iterdir()) == []


def test_default_collect_only_never_calls_getbasetemp(tmp_path: Path) -> None:
    neutral = tmp_path / "neutral tmpdir"
    neutral.mkdir()

    completed, events = _run_member_pytest(
        tmp_path / "no-allocation-session",
        neutral,
        ("--collect-only", "tests/test_errors.py"),
        forbid_basetemp=True,
    )

    output = completed.stdout + completed.stderr
    assert completed.returncode == pytest.ExitCode.OK, output
    assert events == [], output
    assert list(neutral.iterdir()) == []


@pytest.mark.parametrize("checkout_base", (False, True), ids=("neutral", "checkout"))
def test_explicit_basetemp_collapses_parent_before_following_symlinks(
    tmp_path: Path, checkout_base: bool
) -> None:
    checkout = _make_checkout(tmp_path)
    neutral = tmp_path / "neutral tmpdir"
    neutral.mkdir()
    lexical_parent, target = (
        (checkout, neutral) if checkout_base else (neutral, checkout)
    )
    nested = target / "nested"
    nested.mkdir()
    alias = lexical_parent / "alias"
    alias.symlink_to(nested, target_is_directory=True)
    basetemp = alias / ".." / "pytest base"
    effective = lexical_parent / "pytest base"
    effective.mkdir()
    sentinel = effective / "keep.txt"
    sentinel.write_text("keep this checkout", encoding="utf-8")

    completed, events = _run_member_pytest(
        tmp_path / "lexical-base-session",
        neutral,
        _SAFE_TESTS,
        basetemp=basetemp,
    )

    if checkout_base:
        _assert_refused(completed, events, effective, checkout)
        assert "--basetemp" in completed.stdout + completed.stderr
        assert sentinel.read_text(encoding="utf-8") == "keep this checkout"
    else:
        _assert_running(completed, events, _SAFE_TESTS)


@pytest.mark.parametrize("base_location", ("checkout", "descendant"))
def test_dangling_git_marker_refuses_basetemp_without_deleting_files(
    tmp_path: Path, base_location: str
) -> None:
    checkout = tmp_path / "work"
    checkout.mkdir()
    marker = checkout / ".git"
    marker.symlink_to(tmp_path / "missing git metadata", target_is_directory=True)
    assert marker.is_symlink() and not marker.exists()
    basetemp = checkout
    if base_location == "descendant":
        basetemp = checkout / "existing pytest base"
        basetemp.mkdir()
    sentinel = basetemp / "keep.txt"
    sentinel.write_text("keep this checkout", encoding="utf-8")
    neutral = tmp_path / "neutral tmpdir"
    neutral.mkdir()

    completed, events = _run_member_pytest(
        tmp_path / "dangling-marker-session",
        neutral,
        _SAFE_TESTS,
        basetemp=basetemp,
    )

    output = completed.stdout + completed.stderr
    assert sentinel.exists(), f"pytest deleted the protected base:\n{output}"
    assert sentinel.read_text(encoding="utf-8") == "keep this checkout"
    assert marker.is_symlink() and not marker.exists()
    assert completed.returncode == pytest.ExitCode.USAGE_ERROR, output
    _assert_refused(completed, events, basetemp, checkout)
    assert "--basetemp resolves to " in output, output


@pytest.mark.parametrize("setting", ("TEMP", "TMP"))
def test_tempfile_refusal_names_setting_after_lexical_normalization(
    tmp_path: Path, setting: str
) -> None:
    checkout = _make_checkout(tmp_path)
    temporary = checkout / "tmp"
    temporary.mkdir()
    neutral = tmp_path / "neutral tmpdir"
    neutral.mkdir()
    alias = checkout / "alias"
    alias.symlink_to(neutral, target_is_directory=True)
    candidate = alias / ".." / "tmp"
    assert os.path.abspath(candidate) == str(temporary)
    assert candidate.resolve() != temporary

    completed, events = _run_member_pytest(
        tmp_path / "lexical-tempfile-session",
        neutral,
        _SAFE_TESTS,
        environment={
            "TMPDIR": None,
            "TEMP": None,
            "TMP": None,
            setting: str(candidate),
        },
    )

    _assert_refused(completed, events, temporary, checkout)
    output = completed.stdout + completed.stderr
    assert f"{setting} resolves to " in output, output
    assert f"Set {setting} to an absolute disk directory" in output, output
