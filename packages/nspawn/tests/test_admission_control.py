"""Coordinator verbs and admission fences on the real, isolated native store."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import nott_database
import pytest
from registry_reset import store

from nspawn import cli, driver, mcp, native, picker
from nspawn import supervisor as supervisor_module
from nspawn.owner_roster import NativeAccount, Provider, Roster
from nspawn.redact import SecretNeedles
from nspawn.registry import KeyFencedRow, NoticeCaller, Registry, RegistryError, RunKey
from nspawn.rows import (
    Backend,
    ClassObservation,
    NativeSessionContext,
    RunPhase,
    RunRow,
)
from nspawn.supervisor import Supervisor

NODE = "node:11111111-1111-4111-8111-111111111111"
OTHER_NODE = "node:22222222-2222-4222-8222-222222222222"
FINGERPRINT = "a" * 64


def _command(capsys: pytest.CaptureFixture[str], arguments: list[str]) -> dict:
    assert cli.main(arguments) == 0
    return json.loads(capsys.readouterr().out)


def _binding(
    verb: str,
    project: str,
    group: str = "workflow",
    generation: int = 0,
    *,
    node: str = NODE,
    decision: str = "decision",
    key: str = "phase",
    fingerprint: str = FINGERPRINT,
) -> list[str]:
    arguments = [
        "admission",
        verb,
        "--group",
        group,
        "--node",
        node,
        "--project",
        project,
        "--decision",
        decision,
        "--generation",
        str(generation),
    ]
    if verb == "bind":
        arguments.extend(["--key", key, "--fingerprint", fingerprint])
    return arguments


def _admit(
    registry: Registry,
    project: str,
    run_id: str = "run",
    *,
    group: str | None = "workflow",
    key: str | None = "phase",
    fingerprint: str = FINGERPRINT,
) -> RunRow:
    row = RunRow(
        run_id=run_id,
        worktree=f"{project}/{run_id}",
        phase=RunPhase.PENDING,
        sandbox="read-only",
        model="fixture-model",
        owner_instance=registry.instance_id,
        created_at=10,
        updated_at=10,
        backend=Backend.CODEX,
        executed_provider="fixture",
        executed_account="fixture-account",
        group_name=group,
    )
    registry.admit_run(
        row,
        True,
        11,
        1000,
        native_context=NativeSessionContext(home=project),
        notice=NoticeCaller(NODE, 1, 0, project),
        run_key=RunKey(f"launch-{run_id}", project, key, fingerprint, "b" * 64),
        class_observation=ClassObservation(
            "fixture", "judgment", None, None, "judgment"
        ),
        effort_profile={"requested": "max", "sent": "max"},
    )
    return replace(row, phase=RunPhase.SPAWNING, updated_at=11)


def _empty_admission(registry: Registry) -> None:
    for table in (
        "runs",
        "claims",
        "notices",
        "run_keys",
        "key_fences",
        "native_contexts",
        "run_events",
        "class_observations",
    ):
        assert nott_database.query(registry.connection, f"SELECT * FROM {table}") == []


def test_stop_commits_before_answer_and_never_fences_a_key(
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = store()
    assert cli.main(["admission", "stop", "--node", NODE, "--generation", "3"]) == 0
    answer = json.loads(capsys.readouterr().out)
    assert answer == {"node": NODE, "generation": 3, "groups": []}
    connection = nott_database.connect(path, readonly=True)
    try:
        assert nott_database.query(
            connection, "SELECT node, generation FROM node_holds"
        ) == [(NODE, 3)]
        assert nott_database.query(connection, "SELECT * FROM key_fences") == []
    finally:
        connection.close()


def test_stop_before_bind_rolls_back_every_admission_effect_then_exact_resume_admits(
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = store()
    project = str(path.parent / "project")
    with Registry(path, "admitter") as registry:
        registry.open(1)
        _command(capsys, ["admission", "stop", "--node", NODE, "--generation", "3"])
        assert _command(capsys, _binding("bind", project, generation=2))["held"]
        with pytest.raises(RegistryError, match="admission held") as refused:
            _admit(registry, project)
        assert type(refused.value).__name__ == "AdmissionHeldRow"
        assert refused.value.data == {"node": NODE, "generation": 3}
        _empty_admission(registry)
        # A stale continuation changes no row and does not poison the saved key.
        assert cli.main(_binding("resume", project, generation=2)) == 2
        assert "current Stop generation" in capsys.readouterr().err
        resumed = _command(capsys, _binding("resume", project, generation=3))
        assert resumed["releasedGeneration"] == resumed["nodeGeneration"] == 3
        assert not resumed["held"]
        _admit(registry, project)
        assert len(registry.list_runs()) == 1
        with pytest.raises(RegistryError):
            _admit(registry, project, "duplicate")
        assert len(registry.list_runs()) == 1


def test_resume_releases_only_its_workflow_and_bind_cannot_release_a_hold(
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = store()
    project = str(path.parent / "project")
    _command(capsys, _binding("bind", project))
    _command(capsys, _binding("bind", project, "workflow-2", key="phase-2"))
    stopped = _command(
        capsys, ["admission", "stop", "--node", NODE, "--generation", "4"]
    )
    assert stopped["groups"] == ["workflow", "workflow-2"]
    assert _command(capsys, _binding("bind", project, generation=4))["held"]
    _command(capsys, _binding("resume", project, generation=4))
    # Retries are harmless; an older Stop request never lowers the stored fence.
    _command(capsys, _binding("resume", project, generation=4))
    again = _command(capsys, ["admission", "stop", "--node", NODE, "--generation", "2"])
    assert again == {"node": NODE, "generation": 4, "groups": ["workflow-2"]}
    with Registry(path, "admitter") as registry:
        registry.open(1)
        _admit(registry, project)
        with pytest.raises(RegistryError, match="admission held"):
            _admit(registry, project, "other", group="workflow-2", key="phase-2")


def test_approval_after_stop_admits_and_resume_can_bind_before_the_first_phase(
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = store()
    project = str(path.parent / "project")
    _command(capsys, ["admission", "stop", "--node", NODE, "--generation", "3"])
    assert not _command(capsys, _binding("bind", project, generation=3))["held"]
    resumed = _command(capsys, _binding("resume", project, "workflow-2", 3))
    assert not resumed["held"]
    assert not _command(
        capsys, _binding("bind", project, "workflow-2", 0, key="phase-2")
    )["held"]
    with Registry(path, "admitter") as registry:
        registry.open(1)
        _admit(registry, project)
        _admit(registry, project, "other", group="workflow-2", key="phase-2")


@pytest.mark.parametrize(
    "group,key,fingerprint",
    [
        ("foreign", "phase", FINGERPRINT),
        (None, "phase", FINGERPRINT),
        ("workflow", "phase", "c" * 64),
        ("workflow", "unreserved", FINGERPRINT),
        ("workflow", None, FINGERPRINT),
    ],
)
def test_binding_mismatch_rolls_back_every_admission_effect(
    capsys: pytest.CaptureFixture[str],
    group: str | None,
    key: str | None,
    fingerprint: str,
) -> None:
    path = store()
    project = str(path.parent / "project")
    _command(capsys, _binding("bind", project))
    with Registry(path, "admitter") as registry:
        registry.open(1)
        with pytest.raises(
            RegistryError, match="admission outside its binding"
        ) as refused:
            _admit(registry, project, group=group, key=key, fingerprint=fingerprint)
        assert type(refused.value).__name__ == "AdmissionUnboundRow"
        _empty_admission(registry)
        _admit(registry, project)


@pytest.mark.parametrize("verb", ["bind", "resume"])
@pytest.mark.parametrize("changed", ["node", "project", "decision"])
def test_binding_identity_is_immutable(
    capsys: pytest.CaptureFixture[str],
    verb: str,
    changed: str,
) -> None:
    project = str(store().parent / "project")
    _command(capsys, _binding("bind", project))
    values = {"node": NODE, "project": project, "decision": "decision"}
    values[changed] = (
        OTHER_NODE
        if changed == "node"
        else "other"
        if changed == "decision"
        else f"{project}/other"
    )
    assert (
        cli.main(
            _binding(
                verb,
                values["project"],
                node=values["node"],
                decision=values["decision"],
            )
        )
        == 2
    )
    assert "binding identity" in capsys.readouterr().err


def test_reservation_conflicts_and_first_bind_of_a_populated_group_refuse_atomically(
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = store()
    project = str(path.parent / "project")
    _command(capsys, _binding("bind", project))
    for arguments in (
        _binding("bind", project, fingerprint="c" * 64),
        _binding("bind", project, "foreign"),
    ):
        assert cli.main(arguments) == 2
        assert "reserved" in capsys.readouterr().err
    with Registry(path, "admitter") as registry:
        registry.open(1)
        assert (
            len(
                nott_database.query(
                    registry.connection, "SELECT * FROM admission_bindings"
                )
            )
            == 1
        )
        _admit(registry, project, "legacy", group="legacy", key="legacy-key")
        assert cli.main(_binding("bind", project, "legacy", key="legacy-key")) == 2
        assert "already holds runs" in capsys.readouterr().err


def test_legacy_unbound_admission_and_explicit_permanent_key_cancellation_stay(
    tmp_path: Path,
) -> None:
    with Registry(store(), "admitter") as registry:
        registry.open(1)
        supervisor = Supervisor(registry, tmp_path / "runs")
        try:
            project = str(tmp_path / "project")
            _admit(registry, project, group="legacy")
            assert supervisor.release_key(project, "cancelled", 1) is None
            with pytest.raises(KeyFencedRow):
                _admit(registry, project, "cancelled", group="legacy", key="cancelled")
            assert supervisor.release_key(project, "cancelled", 1) is None
            assert (
                len(
                    nott_database.query(registry.connection, "SELECT * FROM key_fences")
                )
                == 1
            )
        finally:
            supervisor.shutdown()


@pytest.mark.parametrize("stop_first", [True, False])
def test_stop_and_admission_serialize_at_the_owning_write_lock(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    stop_first: bool,
) -> None:
    path = store()
    project = str(path.parent / "project")
    _command(capsys, _binding("bind", project))
    with Registry(path, "admitter") as admitter, Registry(path, "stopper") as stopper:
        admitter.open(1)
        stopper.open(1)
        waiting = admitter if stop_first else stopper
        entered = threading.Event()
        begin = nott_database.begin

        def observe(connection: nott_database.Connection) -> None:
            if connection is waiting.connection:
                entered.set()
            begin(connection)

        monkeypatch.setattr(nott_database, "begin", observe)
        with ThreadPoolExecutor(max_workers=1) as pool:
            if stop_first:
                with stopper._transaction():
                    assert stopper.admission_stop(NODE, 1)["generation"] == 1
                    pending = pool.submit(_admit, admitter, project)
                    assert entered.wait(5)
                    assert not pending.done()
                with pytest.raises(RegistryError, match="admission held"):
                    pending.result(timeout=5)
                _empty_admission(admitter)
            else:
                with admitter._transaction():
                    _admit(admitter, project)
                    stopped = pool.submit(stopper.admission_stop, NODE, 1)
                    assert entered.wait(5)
                    assert not stopped.done()
                assert stopped.result(timeout=5)["groups"] == ["workflow"]
                assert stopper.admission_close("workflow") == (True, ["run"])


@pytest.mark.parametrize("held", [True, False])
def test_real_supervisor_maps_store_refusal_and_driver_acks_without_a_run(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    held: bool,
) -> None:
    path = store()
    project = str(tmp_path)
    _command(capsys, _binding("bind", project))
    if held:
        _command(capsys, ["admission", "stop", "--node", NODE, "--generation", "1"])
    provider = Provider(
        name="fixture", kind="codex", model="fixture-model", models=["fixture-model"]
    )
    roster = Roster(providers=[provider])
    picked = picker.PickerResult(
        None, provider, "fixture-account", "fixture-model", None, None
    )
    # Only preparation/routing is doubled. The supervisor and the enforcing
    # SQLite admission remain real, and no vendor can be launched by this test.
    monkeypatch.setattr(supervisor_module, "_read_roster", lambda _: roster)
    monkeypatch.setattr(picker, "pick_execution", lambda *args, **kwargs: picked)
    launch = native.NativeLaunch(
        (),
        {},
        SecretNeedles.none(),
        NativeSessionContext(project),
        NativeAccount(tmp_path),
    )
    monkeypatch.setattr(native, "prepare_native", lambda *args, **kwargs: launch)
    monkeypatch.setattr(Supervisor, "_drive_native_run", lambda *args, **kwargs: None)
    with Registry(path, "admitter") as registry:
        registry.open(1)
        supervisor = Supervisor(registry, tmp_path / "runs")
        call = supervisor_module.DriveCall(
            "launch",
            group="workflow",
            project=project,
            run_key=RunKey(
                "launch",
                project,
                "phase" if held else "unreserved",
                FINGERPRINT,
                "b" * 64,
            ),
        )
        token = supervisor_module.DRIVE_CALL.set(call)
        try:
            with pytest.raises(supervisor_module.SupervisorError) as refused:
                supervisor.execute("fixture", project, read_only=True)
            assert type(refused.value).__name__ == (
                "AdmissionHeld" if held else "AdmissionUnbound"
            )
            ack = driver.refusal_ack(refused.value)
            assert set(ack) == {"refused"}
            assert ack["refused"]["code"] == driver.INVALID_PARAMS
            assert "retryable" not in ack["refused"]
            if held:
                assert ack["refused"]["node"] == NODE
                assert ack["refused"]["generation"] == 1
            assert call.run_id is None
            assert list((tmp_path / "runs").iterdir()) == []
            _empty_admission(registry)
        finally:
            supervisor_module.DRIVE_CALL.reset(token)
            supervisor.shutdown()


CONTROL_ARGUMENTS = [
    ["admission", "stop", "--node", NODE, "--generation", "1"],
    _binding("bind", "/project"),
    _binding("resume", "/project"),
    ["run", "close", "--group", "workflow"],
    ["database", "rollback"],
]


@pytest.mark.parametrize("arguments", CONTROL_ARGUMENTS)
@pytest.mark.parametrize(
    "name,value", [(driver.NODE_SESSION_ENV, ""), (driver.GRANT_ENV, "read")]
)
def test_coordinator_verbs_refuse_node_environment_before_opening_any_store(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    arguments: list[str],
    name: str,
    value: str,
) -> None:
    monkeypatch.setenv(name, value)
    assert cli.main(arguments) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "node environment" in captured.err and name in captured.err


def test_control_is_not_on_the_mcp_surface_and_mutation_inputs_remain_closed() -> None:
    server = mcp.McpServer()
    assert not any("admission" in name for name in mcp.NSPAWN_TOOL_BY_NAME)
    for verb in ("bind", "stop", "resume"):
        with pytest.raises(mcp.McpError, match="no tool named"):
            server.call_tool(f"nspawn_admission_{verb}", {})
    with pytest.raises(mcp.McpError, match="Additional properties"):
        server.call_tool(
            "nspawn_run_start",
            {
                "prompt": "fixture",
                "cwd": "/project",
                "class": "judgment",
                "admission": {},
            },
        )


@pytest.mark.parametrize(
    "arguments",
    [
        ["admission", "stop", "--node", NODE, "--generation", "1", "--unknown", "x"],
        ["admission", "stop", "--node", NODE, "--generation", "1", "--generation", "1"],
        ["admission", "stop", "--node", NODE, "--generation", "-1"],
        ["admission", "stop", "--node", NODE, "--generation", "not-an-integer"],
        _binding("bind", "/project")[:-2],
        _binding("resume", "/project") + ["--key", "phase"],
        ["run", "close", "--group", "workflow", "--owner", NODE],
        ["run", "close", "--group", "workflow", "--before", "1"],
        ["database", "rollback", "--version", "5"],
    ],
)
def test_control_grammar_refuses_extra_missing_duplicate_and_invalid_options(
    capsys: pytest.CaptureFixture[str],
    arguments: list[str],
) -> None:
    assert cli.main(arguments) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "usage:" in captured.err


def test_generation_must_fit_the_native_store_integer(
    capsys: pytest.CaptureFixture[str],
) -> None:
    store()
    assert (
        cli.main(["admission", "stop", "--node", NODE, "--generation", str(2**63)]) == 2
    )
    assert "generation" in capsys.readouterr().err
