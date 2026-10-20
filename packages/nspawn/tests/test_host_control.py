"""Only a trusted server can control a native owner session."""

import time
from pathlib import Path
from typing import Any

import pytest
from supervisor_support import _drain_worker_threads, registry
from test_driver import _call, _payload

from nspawn import cli, driver, main, mcp, route, session
from nspawn.mcp import INVALID_PARAMS, McpServer, tool_result
from nspawn.registry import Registry
from nspawn.rows import Backend, NativeSessionContext, RunPhase, RunRow
from nspawn.supervisor import InvalidRunRequest, Supervisor

__all__ = ["_drain_worker_threads", "registry"]


def control_request(coordinate: str, cwd: Path) -> dict[str, Any]:
    if coordinate == "activation":
        return {
            "cwd": str(cwd),
            "activation": {
                "provider": "claude",
                "account_route": "claude:fixture@subscription",
                "model": "fixture",
            },
        }
    return {
        "cwd": str(cwd),
        "original_run": "host-owned-run",
        "session_operation": {
            "id": 1,
            "method": "session/prompt",
            "params": {"sessionId": "host-session", "prompt": []},
        },
    }


@pytest.mark.parametrize("coordinate", ["activation", "session_operation"])
@pytest.mark.parametrize("caller", ["ordinary", "node", "audit"])
def test_agent_server_refuses_host_control_before_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    coordinate: str,
    caller: str,
) -> None:
    if caller != "ordinary":
        monkeypatch.setenv(
            driver.NODE_SESSION_ENV, "00000000-0000-0000-0000-000000000009"
        )
    if caller == "audit":
        monkeypatch.setenv(driver.GRANT_ENV, "read")
    dispatched: list[str] = []

    def dispatch(self: McpServer, tool: str, arguments: dict[str, Any]) -> dict:
        dispatched.append(tool)
        return tool_result("{}")

    monkeypatch.setattr(McpServer, "_dispatched", dispatch)
    server = McpServer()
    answer = _call(server, "nspawn_run_start", control_request(coordinate, tmp_path))
    assert "error" in answer, "an agent dispatched a host session request"
    assert answer["error"]["code"] == INVALID_PARAMS
    assert "requires host control" in answer["error"]["message"]
    assert dispatched == []
    assert server.supervisor is None


@pytest.mark.parametrize("coordinate", ["activation", "session_operation"])
def test_agent_supervisor_refuses_before_native_or_registry_effects(
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    coordinate: str,
) -> None:
    effects: list[str] = []

    def reached(*args: object, **kwargs: object) -> Any:
        effects.append(coordinate)
        raise InvalidRunRequest("the host control path reached an effect")

    sv = Supervisor(registry, tmp_path / "runs", owner="node:agent")
    monkeypatch.setattr(route, "route_launch", reached)
    monkeypatch.setattr(sv, "_read", reached)
    monkeypatch.setattr(sv, "_open_writer", reached)
    monkeypatch.setattr(registry, "admit_run", reached)
    try:
        with pytest.raises(InvalidRunRequest, match="requires host control"):
            sv.execute(None, **control_request(coordinate, tmp_path))
        assert effects == []
        assert registry.list_runs() == ()
        assert not any((tmp_path / "runs").iterdir())
    finally:
        sv.shutdown()


@pytest.mark.parametrize("coordinate", ["activation", "session_operation"])
def test_tool_arguments_cannot_grant_host_control(
    tmp_path: Path, coordinate: str
) -> None:
    server = McpServer()
    answer = _call(
        server,
        "nspawn_run_start",
        {**control_request(coordinate, tmp_path), "host_control": True},
    )
    assert answer["error"]["code"] == INVALID_PARAMS
    assert server.supervisor is None


@pytest.mark.parametrize("method", ["session/prompt", "session/cancel"])
@pytest.mark.parametrize("owner", ["node:host", "node:other"])
def test_host_session_operations_keep_the_existing_owner_boundary(
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    owner: str,
) -> None:
    row = RunRow(
        run_id="host-owned-run",
        worktree=str(tmp_path),
        phase=RunPhase.PENDING,
        sandbox="workspace-write",
        model="fixture",
        owner_instance=registry.instance_id,
        created_at=1000,
        updated_at=1000,
        backend=Backend.CLAUDE,
        owner="node:host",
    )
    registry.admit_run(
        row,
        False,
        1000,
        1000,
        native_context=NativeSessionContext(str(tmp_path)),
        activation=control_request("activation", tmp_path)["activation"],
    )
    port = {"path": str(tmp_path / "stream"), "control": str(tmp_path / "control")}
    registry.append_run_event(
        row.run_id, at=1000, kind="lifecycle", payload={"session_transport": port}
    )
    submitted: list[dict[str, Any]] = []

    def submit(actual: dict[str, str], frame: dict[str, Any]) -> None:
        assert actual == port
        submitted.append(session.operation(frame))

    monkeypatch.setattr(session, "submit", submit)
    sv = Supervisor(registry, tmp_path / "runs", owner=owner, host_control=True)
    host = McpServer(supervisor=sv, host_control=True)
    arguments = control_request("session_operation", tmp_path)
    arguments["session_operation"]["method"] = method
    try:
        denied = _call(McpServer(supervisor=sv), "nspawn_run_start", arguments)
        assert "requires host control" in denied["error"]["message"]
        assert submitted == []
        answer = _call(host, "nspawn_run_start", arguments)
        if owner == row.owner:
            assert _payload(answer)["run_id"] == row.run_id
            assert submitted == [session.operation(arguments["session_operation"])]
        else:
            assert answer["error"]["code"] == -32603
            assert answer["error"]["data"]["type"] == "UnknownRun"
            assert submitted == []
        assert registry.get_run(row.run_id) == row
        assert len(registry.list_runs()) == 1
    finally:
        host.close()


def test_host_control_preserves_the_audit_read_ceiling(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: object, **kwargs: object) -> Any:
        pytest.fail("an audit activation reached native launch without a profile")

    monkeypatch.setattr(route, "route_launch", forbidden)
    sv = Supervisor(registry, tmp_path / "runs", read_grant=True, host_control=True)
    try:
        with pytest.raises(InvalidRunRequest, match="requires a verified read-only"):
            sv.execute(None, **control_request("activation", tmp_path))
        assert registry.list_runs() == ()
    finally:
        sv.shutdown()


def test_the_host_cli_option_constructs_a_trusted_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    served: list[McpServer] = []
    monkeypatch.setattr(main, "serve", served.append)
    assert cli.main(["mcp", "serve", "--host-control"]) == 0
    assert len(served) == 1
    assert served[0].host_control is True
    assert served[0].supervisor is None
    assert isinstance(served[0].launcher, driver.SystemdLauncher)
    assert served[0].launcher.host_control is True


@pytest.mark.parametrize("host_control", [False, True])
def test_the_live_constructor_uses_only_its_trusted_permission(
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    host_control: bool,
) -> None:
    monkeypatch.setenv("NOTT_NSPAWN_RUN_ROOT", str(tmp_path / "runs"))
    server = McpServer(host_control=host_control)
    try:
        supervisor = server._resolve_supervisor()
        assert supervisor.host_control is host_control
        assert server._resolve_supervisor() is supervisor
    finally:
        server.close()


@pytest.mark.parametrize("platform", ["linux", "darwin"])
@pytest.mark.parametrize("host_control", [False, True])
def test_each_native_launcher_retains_the_trusted_permission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    host_control: bool,
) -> None:
    launcher = driver.default_launcher(platform, host_control=host_control)
    selected: list[bool] = []

    def popen(arguments: list[str], **kwargs: Any) -> Any:
        selected.append("--host-control" in arguments)
        raise OSError("test launch stopped before process creation")

    if isinstance(launcher, driver.LaunchdLauncher):
        document = driver.driver_plist(
            "host-control-test", launcher.argv, {}, stdin=tmp_path, log=tmp_path
        )
        assert ("--host-control" in document["ProgramArguments"]) is host_control
    else:
        monkeypatch.setattr(driver.subprocess, "Popen", popen)
        frame = driver.make_frame(
            "nspawn_run_start", {"cwd": str(tmp_path)}, drivers_dir=tmp_path
        )
        launcher._launch(frame, None, time.monotonic() + 1)
        assert selected == [host_control]


@pytest.mark.parametrize("host_control", [False, True])
def test_driver_construction_retains_the_trusted_permission(
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    host_control: bool,
) -> None:
    built: list[tuple[str, bool]] = []
    sv = Supervisor(registry, tmp_path / "runs", host_control=host_control)

    def construct(
        sidecar_kind: str = "stdio", *, host_control: bool = False
    ) -> Supervisor:
        built.append((sidecar_kind, host_control))
        return sv

    monkeypatch.setattr(mcp, "_default_supervisor", construct)
    try:
        assert (
            driver._driver_supervisor("host-control-test", host_control=host_control)
            is sv
        )
        assert built == [("driver", host_control)]
    finally:
        sv.shutdown()


@pytest.mark.parametrize("coordinate", ["activation", "session_operation"])
def test_driver_refuses_host_control_before_key_lookup(
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    coordinate: str,
) -> None:
    effects: list[str] = []

    def key_lookup(*args: object, **kwargs: object) -> Any:
        effects.append("key lookup")
        raise InvalidRunRequest("the host request reached key lookup")

    sv = Supervisor(registry, tmp_path / "runs")
    monkeypatch.setattr(sv, "lock_key", key_lookup)
    frame = driver.make_frame(
        "nspawn_run_start",
        {**control_request(coordinate, tmp_path), "idempotency_key": "host-request"},
        drivers_dir=None,
    )
    frame["host_control"] = True
    try:
        answer = driver.serve(
            frame,
            lambda ack: None,
            supervisor_factory=lambda: sv,
            owns_supervisor=False,
        )
        assert "requires host control" in answer["refused"]["message"]
        assert effects == []
        assert registry.list_runs() == ()
    finally:
        sv.shutdown()
