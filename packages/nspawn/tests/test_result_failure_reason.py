"""F8: a pre-child failure is diagnosable through the instructed result read."""

from __future__ import annotations

import io
import json
import threading
from dataclasses import replace
from pathlib import Path
from unittest.mock import ANY, Mock

import pytest
from class_dispatch import (
    DECLARED_CLASS,
    system1_never_reaches_the_network,  # noqa: F401
)
from fixture_data import synthetic_token
from mcp_fakes import handle

from nspawn import agy_restricted, app_server, child, claude_child, supervisor
from nspawn.mcp import McpServer
from nspawn.owner_roster import Roster
from nspawn.pump import (
    OutputPump,
    OutputStream,
    PumpLimitExceeded,
    RunOutputBudget,
)
from nspawn.redact import REDACTION_MARKER, SecretNeedles
from nspawn.registry import EffortProfile
from nspawn.rows import RunPhase
from nspawn.supervisor import Supervisor


@pytest.fixture
def failed_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Same unambiguous a1/acme control as F8; no plugin_root is declared.
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "acme.token").write_text("synthetic-unused")
    source = tmp_path / "roster.toml"
    source.write_text(
        '[defaults]\nmodel = "m1"\n'
        '[bins]\nclaude = "/fixture/launcher"\n'
        '[[provider]]\nname = "a1"\nkind = "claude"\n'
        f'secrets = "{secrets}"\nmodels = ["m1"]\n'
    )
    roster = Roster.read(source)
    monkeypatch.setattr(supervisor, "_read_roster", lambda cwd: roster)
    monkeypatch.setattr(supervisor, "_lane_of", lambda cwd: None)
    monkeypatch.setattr(child.os, "environ", {"HOME": str(tmp_path), "PATH": "/bin"})
    launcher = Mock(side_effect=AssertionError("launcher unexpectedly reached"))
    installation = Mock(side_effect=AssertionError("installation unexpectedly reached"))
    monkeypatch.setattr(child, "spawn_child", launcher)
    monkeypatch.setattr(claude_child, "inspect_claude_installation", installation)
    # Real admission, selector, worker, failure handler and readers; only storage
    # is in memory. Constructor is bypassed to avoid a database and reaper.
    instance = object.__new__(Supervisor)
    instance._lock = threading.Condition()
    instance._children = {}
    runs = tmp_path / "runs"
    runs.mkdir()
    instance._run_root = runs
    instance.announces_terminal = False
    instance._writers = {}
    instance._unpublished = {}
    instance._failovers = {}
    rows = {}
    registry = Mock()
    registry.instance_id = "f8-memory"
    registry.writer_scope.return_value = None  # a Claude run records no slice
    # This in-memory store keeps no journal, so every run reads the unknown profile.
    registry.effort_profile.return_value = EffortProfile()
    registry.read_run_events.return_value = []
    registry.session_reference.return_value = None
    registry.session_transport.return_value = None
    registry.session_exit_code.return_value = None
    registry.admission_show.return_value = None
    registry.terminal_receipt_show.return_value = None
    registry.inference_profile.return_value = None
    registry.get_provider_walls.return_value = {}
    registry.get_account_walls.return_value = {}
    registry.get_run.side_effect = rows.get
    registry.read_claim.return_value = None
    # This instance owns the run and no close request is pending, so a close
    # takes the local, identity-fenced path (#781's custody dispatch).
    registry.run_custody.return_value = (registry.instance_id, "stdio", True)
    registry.claim_close.return_value = "none"
    # A reader never records an implementation recovery artifact.
    registry.implementation_recovery.return_value = None
    # A pre-child failure records no managed session (#1486 added the read).
    registry.session_reference.return_value = None
    registry.session_transport.return_value = None
    registry.session_exit_code.return_value = None
    registry.admission_show.return_value = None
    registry.terminal_receipt_show.return_value = None

    def admit(row, claim, now, lease, **_):
        # Read-only: a writable dispatch needs `native_write` and a Claude
        # route has no native write mode, so the pre-child F8
        # failure is reached as a reader, which fences no worktree.
        assert claim is False
        rows[row.run_id] = row

    def update(row, **kwargs):
        rows[row.run_id] = row

    def close_owned(run_id, expected, now, *, drained=False):
        # The registry's close moves the phase and nothing else on the row.
        rows[run_id] = replace(rows[run_id], phase=RunPhase.CLOSED, updated_at=now)
        return True

    registry.admit_run.side_effect = admit
    registry.update_run.side_effect = update
    registry.close_owned.side_effect = close_owned
    instance._registry = registry
    server = McpServer(supervisor=instance)

    def call(name, arguments):
        answer = handle(
            server,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
        )
        assert answer is not None and "result" in answer, answer
        return json.loads(answer["result"]["content"][0]["text"])

    accepted = call(
        "nspawn_run_start",
        {
            "prompt": "reply with the word pong",
            "cwd": str(tmp_path),
            "provider": "acme",
            # The provider is pinned and roster does not declare `[classes]`: the
            # class satisfies the mandatory argument without altering selection,
            # which is what this F8 measures before the child exists.
            "class": DECLARED_CLASS,
            "read_only": True,
            "timeout_ms": 60000,
        },
    )
    run_id = accepted["run_id"]
    call("nspawn_run_show", {"run_id": run_id, "timeout_ms": 2000})
    return instance, rows, call, run_id, registry, installation, launcher


def _still_running(rows, run_id) -> None:  # type: ignore[no-untyped-def]
    """The fixture's run already failed, and an ended run is never rewritten:
    put it back in flight so `_fail_run` records the failure under test."""
    rows[run_id] = replace(rows[run_id], phase=RunPhase.RUNNING, blocked_reason=None)


def test_result_preserves_status_reason_when_child_never_started(failed_run) -> None:
    _instance, rows, call, run_id, registry, installation, launcher = failed_run
    status = call("nspawn_run_show", {"run_id": run_id})
    result = call("nspawn_result_show", {"run_id": run_id})
    row = rows[run_id]
    assert row.phase is RunPhase.FAILED
    assert (row.executed_provider, row.executed_account, row.model) == (
        "a1",
        "acme",
        "m1",
    )
    reason = "SupervisorError"
    assert status["failure_reason"] == row.blocked_reason == reason
    assert registry.update_run.call_args.kwargs["drop_claim"] is True
    installation.assert_not_called()
    launcher.assert_not_called()
    assert result["final_text"] is None
    assert result["stderr_tail"] is None
    assert result.get("failure_reason") == status["failure_reason"]


def test_policy_refusal_records_only_safe_identity(failed_run) -> None:
    instance, rows, call, run_id, *_ = failed_run
    private = "/private/fixture/credentials/f8.token"
    marker = "SYNTHETIC_F8_CREDENTIAL"
    with pytest.raises(child.ChildEnvironmentError) as caught:
        child.compose_child_argv(
            "/fixture/launcher",
            read_only=True,
            declared_args=[f"--mcp-config={private}?credential={marker}"],
        )
    _still_running(rows, run_id)
    instance._fail_run(run_id, caught.value)
    status = call("nspawn_run_show", {"run_id": run_id})
    result = call("nspawn_result_show", {"run_id": run_id})
    reason = rows[run_id].blocked_reason
    assert status["failure_reason"] == result["failure_reason"] == reason
    for value in (reason, json.dumps(status), json.dumps(result)):
        assert private not in value, "private path escaped the recording boundary"
        assert marker not in value, "credential marker escaped the recording boundary"
    assert reason == "ChildEnvironmentError (nERR-1112)"


@pytest.mark.parametrize("recorded_reason", [None, "PriorFailure"])
def test_result_preserves_recorded_failure_independent_of_phase(
    failed_run, recorded_reason
) -> None:
    _instance, rows, call, run_id, *_ = failed_run
    rows[run_id] = replace(
        rows[run_id],
        phase=RunPhase.COMPLETED,
        blocked_reason=recorded_reason,
        final_text="PONG",
    )
    result = call("nspawn_result_show", {"run_id": run_id})
    expected = {"run_id": run_id, "final_text": "PONG", "stderr_tail": None}
    if recorded_reason is not None:
        expected["failure_reason"] = recorded_reason
    expected["phase"] = "completed"
    assert (
        call("nspawn_run_show", {"run_id": run_id})["failure_reason"] == recorded_reason
    )
    assert json.dumps(result, separators=(",", ":")) == json.dumps(
        expected, separators=(",", ":")
    )


def test_recorded_failure_reason_survives_close(failed_run) -> None:
    _instance, rows, call, run_id, registry, *_ = failed_run
    before = call("nspawn_result_show", {"run_id": run_id})
    assert before["failure_reason"] == rows[run_id].blocked_reason == "SupervisorError"
    closed = call("nspawn_run_close", {"run_id": run_id})
    assert closed["phase"] == "closed"
    assert closed["claim_released"] is True
    assert closed["tree"] == {"checked": [], "survivors": []}
    # No process was ever on record: the close is fenced on the row naming none.
    registry.close_owned.assert_called_once_with(run_id, None, ANY, drained=False)
    status = call("nspawn_run_show", {"run_id": run_id})
    result = call("nspawn_result_show", {"run_id": run_id})
    assert status["phase"] == "closed"
    assert status["failure_reason"] == before["failure_reason"]
    assert result.get("failure_reason") == status["failure_reason"]
    assert (before["phase"], result["phase"]) == ("failed", "closed")
    assert {**result, "phase": "failed"} == before


def test_a_pump_ceiling_refusal_names_the_line_the_size_and_the_ceiling(
    failed_run, tmp_path
) -> None:
    """The measured defect: 690 events, then `PumpLimitExceeded` and nothing else.

    The reason recorded for a pump refusal was the bare class name, and FIVE
    distinct ceilings raise that class — a 1 MiB line, the shared 64 MiB run
    budget, a transformed line, JSON nesting and the pre-response notification
    stash all read identically. The only stderr on record named
    the broken pipe DOWNSTREAM of the refusal, which is its consequence.

    Both seams run for real here: the message comes out of a real `OutputPump`
    over a real oversized line, and the recording comes out of the real
    `_fail_run`, read back through `nspawn_run_show` and `nspawn_result_show`.
    """
    instance, rows, call, run_id, *_ = failed_run
    pumped = tmp_path / "pumped"
    pumped.mkdir()
    overflowing = OutputPump(
        io.BytesIO(b"x" * 10 + b"\n"),
        pumped,
        OutputStream.STDOUT,
        SecretNeedles.none(),
        RunOutputBudget(),
        max_line_bytes=4,
    )
    overflowing.start()
    with pytest.raises(PumpLimitExceeded) as caught:
        overflowing.join(timeout=5)

    _still_running(rows, run_id)
    instance._fail_run(run_id, caught.value)

    reason = rows[run_id].blocked_reason
    status = call("nspawn_run_show", {"run_id": run_id})
    result = call("nspawn_result_show", {"run_id": run_id})
    assert status["failure_reason"] == result["failure_reason"] == reason
    assert reason == (
        "PumpLimitExceeded: stdout line 1 exceeded the 4 byte line ceiling: "
        "5 bytes read with no terminator"
    )


def test_a_restricted_launch_refusal_names_its_own_reason(failed_run) -> None:
    """An x509 failure, a linked-git refusal and a marketplace error all read
    as a bare type and code. nSPAWN's own refusal text goes with them."""
    instance, rows, call, run_id, *_ = failed_run
    _still_running(rows, run_id)
    instance._fail_run(
        run_id,
        agy_restricted.RestrictedLaunchRefused(
            "linked Git metadata has no admitted read root"
        ),
    )
    result = call("nspawn_result_show", {"run_id": run_id})
    assert result["failure_reason"] == (
        "RestrictedLaunchRefused (nERR-1116): "
        "agy restricted launch: linked Git metadata has no admitted read root"
    )


def test_an_app_server_failure_message_is_redacted_before_it_is_stored(
    failed_run,
) -> None:
    instance, rows, call, run_id, *_ = failed_run
    secret = synthetic_token("test_result_failure_reason:1")
    _still_running(rows, run_id)
    instance._fail_run(
        run_id,
        app_server.AppServerRefused(f"native plugin/installed {secret} refused"),
        needles=SecretNeedles((secret,)),
    )
    reason = rows[run_id].blocked_reason
    assert reason == (
        f"AppServerRefused (nERR-2003): native plugin/installed {REDACTION_MARKER} "
        "refused"
    )
    assert secret not in json.dumps(call("nspawn_result_show", {"run_id": run_id}))


def test_a_failed_run_keeps_the_progress_it_had_made(failed_run, tmp_path) -> None:
    """`total_seq` reset to zero at the exact moment the failure made it matter.

    `records_seen` has one writer and it writes only after a clean `wait()`, so
    a run that died mid-stream never reached it; the live stream count that had
    been answering 690 was gated on a non-terminal phase and stopped answering
    on the same transition. The operator lost the one progress figure they had.
    """
    instance, rows, call, run_id, *_ = failed_run
    run_dir = tmp_path / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "stdout").write_bytes(b'{"type":"assistant"}\n' * 690)

    assert rows[run_id].records_seen == 0, "the failed run never recorded a count"
    instance._fail_run(run_id, PumpLimitExceeded("stdout line 691 over its ceiling"))

    status = call("nspawn_run_show", {"run_id": run_id})
    assert status["phase"] == "failed"
    assert status["total_seq"] == 690
