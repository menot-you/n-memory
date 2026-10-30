"""`nspawn run list --caller <socket> --json`: the runs of one Claude Code session.

The verb reads nSPAWN's own registry: the notice rows that name the session's
messaging socket, joined to their runs. The notices are an outbox, not a history.
`prune_notices` removes a row after NOTICE_RETENTION_DAYS, and its run leaves the
list with it. A closed run can leave sooner, when nSPAWN prunes its run row. The
host reads the recorded fixture: set NOTT_FIXTURE_RECORD=1 to write it, else it
must equal what the verb prints.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import replace
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from supervisor_support import registry

from nspawn import driver
from nspawn.cli import main
from nspawn.registry import NoticeCaller, Registry
from nspawn.rows import Backend, RunPhase, RunRow

__all__ = ["registry"]

FIXTURE = Path(__file__).parent / "fixtures" / "run-list-caller.json"
SCHEMA = (
    Path(__file__).parents[1] / "nspawn" / "schema" / "run-list-caller.v1.schema.json"
)
SOCKET = "/fixture/claude/4242.sock"
#: 2026-10-21T12:00:00Z, so the host reads real epoch milliseconds.
T0 = 1_792_584_000_000
VERB = ["run", "list", "--caller", SOCKET, "--json"]


def _rid(name: str) -> str:
    """A run id in the minted shape, which a failover note names."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"nspawn-test:caller:{name}"))


def _caller(addr: str) -> NoticeCaller:
    return NoticeCaller(addr=addr, pid=4242, start_ticks=1, project="/fixture/.git")


def _admit(
    registry: Registry,
    run_id: str,
    caller: NoticeCaller | None,
    *,
    created_at: int,
    group: str | None = None,
) -> None:
    registry.admit_run(
        RunRow(
            run_id=run_id,
            worktree=f"/fixture/{run_id}",
            phase=RunPhase.PENDING,
            sandbox="read-only",
            model="m",
            owner_instance=registry.instance_id,
            created_at=created_at,
            updated_at=created_at,
            backend=Backend.CLAUDE,
            group_name=group,
        ),
        False,
        created_at,
        60_000,
        notice=caller,
    )


def _move(
    registry: Registry, run_id: str, phase: RunPhase, warning: str | None = None
) -> None:
    row = registry.get_run(run_id)
    assert row is not None
    registry.update_run(
        replace(row, phase=phase, warning=warning, updated_at=row.updated_at + 1)
    )


def _output(run_root: Path, run_id: str, at_ms: int) -> None:
    """The run's stdout file, last written at `at_ms`."""
    stdout = run_root / run_id / "stdout"
    stdout.parent.mkdir(parents=True)
    stdout.write_bytes(b'{"type":"assistant"}\n')
    os.utime(stdout, ns=(at_ms * 1_000_000, at_ms * 1_000_000))


def _listed(capsys: pytest.CaptureFixture[str]) -> str:
    capsys.readouterr()
    assert main(VERB) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    return captured.out


def test_run_list_by_caller_answers_that_sessions_runs_as_the_fixture_records(
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_root = tmp_path / "runs"
    monkeypatch.setenv("NOTT_NSPAWN_RUN_ROOT", str(run_root))
    mine = _caller(SOCKET)
    spec, lane, successor, queued = (
        _rid(name) for name in ("spec", "lane", "successor", "queued")
    )
    _admit(registry, spec, mine, created_at=T0, group="live-plan-0")
    _move(registry, spec, RunPhase.RUNNING)
    _move(registry, spec, RunPhase.COMPLETED)
    _output(run_root, spec, T0 + 120_000)
    _admit(registry, lane, mine, created_at=T0 + 60_000, group="live-plan-0")
    _move(registry, lane, RunPhase.RUNNING)
    # A failover admits the successor for the same call, so for the same caller.
    _admit(registry, successor, mine, created_at=T0 + 95_000, group="live-plan-0")
    _move(registry, successor, RunPhase.RUNNING)
    _move(
        registry,
        lane,
        RunPhase.FAILED,
        warning=f"rate limited. continued as run {successor} on codex/work",
    )
    _output(run_root, lane, T0 + 90_000)
    _output(run_root, successor, T0 + 300_000)
    # Admission moves a run to spawning. Its child has no stdout file yet.
    _admit(registry, queued, mine, created_at=T0 + 400_000)
    # Neither another session's run nor a run with no caller is this caller's.
    _admit(
        registry, _rid("theirs"), _caller("/fixture/claude/4343.sock"), created_at=T0
    )
    _admit(registry, _rid("uncalled"), None, created_at=T0)

    out = _listed(capsys)
    answer = json.loads(out)
    assert answer == {
        "runs": [
            {
                "runId": queued,
                "phase": "spawning",
                "createdAt": T0 + 400_000,
                "group": None,
                "lastOutputAt": None,
                "continuedAs": None,
            },
            {
                "runId": successor,
                "phase": "running",
                "createdAt": T0 + 95_000,
                "group": "live-plan-0",
                "lastOutputAt": T0 + 300_000,
                "continuedAs": None,
            },
            {
                "runId": lane,
                "phase": "failed",
                "createdAt": T0 + 60_000,
                "group": "live-plan-0",
                "lastOutputAt": T0 + 90_000,
                "continuedAs": successor,
            },
            {
                "runId": spec,
                "phase": "completed",
                "createdAt": T0,
                "group": "live-plan-0",
                "lastOutputAt": T0 + 120_000,
                "continuedAs": None,
            },
        ]
    }
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(answer)
    if os.environ.get("NOTT_FIXTURE_RECORD") == "1":
        FIXTURE.write_text(out, encoding="utf-8")
    assert FIXTURE.read_text(encoding="utf-8") == out

    # The outbox prunes a notice row after its retention, and the run leaves with it.
    registry._write("UPDATE notices SET created_at = 0 WHERE run_id = ?", (spec,))
    assert registry.prune_notices() == 1
    listed = [run["runId"] for run in json.loads(_listed(capsys))["runs"]]
    assert listed == [queued, successor, lane]
    # A closed run can leave sooner: nSPAWN prunes its run row.
    _move(registry, lane, RunPhase.CLOSED)
    assert registry.prune_closed_runs(T0 + 86_400_000, 0, 1000) == 1
    listed = [run["runId"] for run in json.loads(_listed(capsys))["runs"]]
    assert listed == [queued, successor]


def test_a_failed_run_that_nspawn_closes_stays_failed_in_the_caller_list(
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A close moves a failed run to closed. The host reads the list each 30 seconds,
    so a close before its next read would hide the failure: the list keeps the first
    terminal phase of a failed run. A completed run that closes shows closed."""
    monkeypatch.setenv("NOTT_NSPAWN_RUN_ROOT", str(tmp_path / "runs"))
    mine = _caller(SOCKET)
    failed, completed = _rid("failed-closed"), _rid("completed-closed")
    for created, run_id, end in (
        (T0, failed, RunPhase.FAILED),
        (T0 + 1000, completed, RunPhase.COMPLETED),
    ):
        _admit(registry, run_id, mine, created_at=created)
        _move(registry, run_id, RunPhase.RUNNING)
        _move(registry, run_id, end)
        _move(registry, run_id, RunPhase.CLOSED)
    answer = json.loads(_listed(capsys))
    assert [(run["runId"], run["phase"]) for run in answer["runs"]] == [
        (completed, "closed"),
        (failed, "failed"),
    ]
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(answer)


def test_the_schema_is_closed_and_names_every_phase() -> None:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    run = schema["$defs"]["run"]
    assert set(run["properties"]["phase"]["enum"]) == {p.value for p in RunPhase}
    validator = Draft202012Validator(schema)
    entry = {
        "runId": _rid("one"),
        "phase": "running",
        "createdAt": T0,
        "group": None,
        "lastOutputAt": None,
        "continuedAs": None,
    }
    assert validator.is_valid({"runs": [entry]})
    assert not validator.is_valid({"runs": [entry | {"owner": None}]})
    assert not validator.is_valid({"runs": [entry | {"phase": "done"}]})
    assert not validator.is_valid({"runs": [{"runId": _rid("one")}]})


@pytest.mark.parametrize(
    "arguments",
    [
        ["--caller", SOCKET],
        ["--caller", "fixture.sock", "--json"],
        ["--caller", "--json"],
        ["--caller", SOCKET, "--json", "--json"],
        ["--caller", SOCKET, "--caller", SOCKET, "--json"],
        ["--caller", SOCKET, "--json", "--group", "g"],
        ["--caller", SOCKET, "--json", "--owner", "node:x"],
    ],
)
def test_run_list_by_caller_refuses_any_other_spelling(
    registry: Registry, arguments: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["run", "list", *arguments]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "usage: run list" in captured.err


@pytest.mark.parametrize("name", [driver.NODE_SESSION_ENV, driver.GRANT_ENV])
def test_a_node_scoped_nspawn_lists_no_session_runs(
    registry: Registry,
    name: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _admit(registry, _rid("mine"), _caller(SOCKET), created_at=T0)
    monkeypatch.setenv(name, str(uuid.uuid4()))
    assert main(VERB) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "refused" in captured.err
