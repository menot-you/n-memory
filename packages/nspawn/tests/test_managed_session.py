"""A passive native session and its owner turn have one nSPAWN custodian.

The selected ACP executable is a local peer. Registry, driver, process, stdio
and controller close are real; no vendor or shared store is used.
"""

import json
import os
import socket
import sys
import uuid
from pathlib import Path

import pytest
from supervisor_support import _drain_worker_threads, registry
from test_driver import _call, _payload

from nspawn import child, cli, driver, native, picker, route, session
from nspawn import supervisor as supervisor_module
from nspawn.custody import AggregateScope
from nspawn.mcp import McpServer
from nspawn.owner_roster import NativeAccount, Provider, Roster
from nspawn.redact import SecretNeedles
from nspawn.registry import Registry
from nspawn.rows import (
    Backend,
    NativeSessionContext,
    RunPhase,
    RunRow,
    SessionActivation,
)
from nspawn.supervisor import InvalidRunRequest, Supervisor

__all__ = ["_drain_worker_threads", "registry"]


def test_native_fork_resume_keeps_unknown_operations_refused() -> None:
    frame = {
        "id": "resume-fork",
        "method": "session/resume",
        "params": {"sessionId": "forked-thread", "cwd": "/work", "mcpServers": []},
    }
    assert session.operation(frame) == {"jsonrpc": "2.0", **frame}
    with pytest.raises(ValueError, match="native session operation is not supported"):
        session.operation({**frame, "method": "session/resume_unknown"})


@pytest.mark.parametrize("selection", ["configured", "default", "invalid"])
def test_the_managed_codex_session_preserves_its_configured_adapter(
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    selection: str,
) -> None:
    home = tmp_path / "native-account"
    home.mkdir(mode=0o700)
    binaries = tmp_path / "bin"
    binaries.mkdir()
    observed = tmp_path / "native-peer.jsonl"
    configured = tmp_path / "configured-codex-acp"
    default = binaries / "codex-acp"
    for peer, identity in (
        (configured, "configured-native"),
        (default, "default-native"),
    ):
        peer.write_text(
            "#!/usr/bin/python3\nimport json,os,sys\n"
            "for line in sys.stdin:\n"
            " frame=json.loads(line)\n"
            f" with open({str(observed)!r},'a') as output: output.write(json.dumps({{'method':frame['method'],'adapter':{identity!r},'home':os.environ['CODEX_HOME']}})+'\\n')\n"
            " if 'id' not in frame: continue\n"
            " result={'protocolVersion':1,'agentCapabilities':{'loadSession':True}} if frame['method']=='initialize' else "
            f"{{'sessionId':{identity!r}}}\n"
            " print(json.dumps({'jsonrpc':'2.0','id':frame['id'],'result':result}),flush=True)\n"
        )
        peer.chmod(0o700)
    roster = Roster(
        providers=[
            Provider(
                name="codex",
                kind="codex",
                model="fixture",
                accounts={"fixture": NativeAccount(home=home)},
            )
        ]
    )
    monkeypatch.setattr(route, "read_roster", lambda: roster)
    monkeypatch.setenv("PATH", os.pathsep.join((str(binaries), "/usr/bin", "/bin")))
    monkeypatch.setenv("CODEX_PATH", str(tmp_path / "installed-native-codex"))
    if selection == "default":
        monkeypatch.delenv("NOTT_NODE_CODEX_ADAPTER", raising=False)
    else:
        monkeypatch.setenv(
            "NOTT_NODE_CODEX_ADAPTER",
            str(configured) if selection == "configured" else "relative-adapter",
        )
    sv = Supervisor(registry, tmp_path / "runs", host_control=True)
    server = McpServer(supervisor=sv, host_control=True)
    attached = None
    reader = None
    run_id = None
    try:
        response = _call(
            server,
            "nspawn_run_start",
            {
                "cwd": str(tmp_path),
                "activation": {
                    "provider": "codex",
                    "account_route": "codex:fixture@" + str(home),
                    "model": "fixture",
                },
            },
        )
        if "error" not in response:
            run_id = _payload(response)["run_id"]
        if selection == "invalid":
            assert "error" in response, response
            assert not registry.list_runs()
            assert not observed.exists()
            return
        assert "error" not in response, response
        assert run_id is not None
        shown = _payload(_call(server, "nspawn_run_show", {"run_id": run_id}))
        assert not observed.exists()  # Activation issues no native request.
        attached = socket.socket(socket.AF_UNIX)
        attached.settimeout(3)
        attached.connect(shown["transport"]["path"])
        reader = attached.makefile("rb")
        answer = None
        for ident, method in ((1, "initialize"), (2, "session/new")):
            acknowledged = _call(
                server,
                "nspawn_run_start",
                {
                    "cwd": str(tmp_path),
                    "original_run": run_id,
                    "session_operation": {
                        "id": ident,
                        "method": method,
                        "params": {"cwd": str(tmp_path), "mcpServers": []}
                        if method == "session/new"
                        else {"protocolVersion": 1},
                    },
                },
            )
            assert "error" not in acknowledged, acknowledged
            frame = json.loads(reader.readline())
            assert frame["id"] == ident
            answer = frame["result"]
        expected = (
            "configured-native" if selection == "configured" else "default-native"
        )
        assert answer == {"sessionId": expected}
        assert registry.get_run(run_id).thread_id == expected
        assert sv.status(run_id).phase is RunPhase.PENDING
        assert registry.verify_run_event_chain(run_id)
        assert [json.loads(line) for line in observed.read_text().splitlines()] == [
            {"method": method, "adapter": expected, "home": str(home)}
            for method in ("initialize", "session/new")
        ]
    finally:
        if reader is not None:
            reader.close()
        if attached is not None:
            attached.close()
        if run_id is not None:
            sv.close(run_id)
        server.close()
        sv.shutdown()


def test_the_run_custodian_enforces_the_agy_native_child_boundary(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout, home, outside = (
        tmp_path / name for name in ("checkout", "home", "outside")
    )
    for directory in (checkout, home, outside):
        directory.mkdir(mode=0o750)
    (checkout / ".git").mkdir()
    (checkout / ".git" / "config").write_text("protected metadata")
    (outside / "canary").write_text("protected outside")
    peer = tmp_path / "native-agy-peer.py"
    peer.write_text(
        "import json,os,sys\nfrom pathlib import Path\n"
        "print(json.dumps({'event':'init','conversation_id':'actual-private-agy'}),flush=True)\n"
        "json.loads(sys.stdin.readline())\n"
        "def writes(path):\n"
        " try: Path(path).write_text('changed'); return True\n"
        " except OSError: return False\n"
        "status=dict(line.split(':',1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)\n"
        f"observed={{'project':writes({str(checkout / 'unexpected')!r}),'git':writes({str(checkout / '.git/config')!r}),'outside':writes({str(outside / 'canary')!r}),'home':writes({str(home / 'unexpected')!r}),'no_new_privileges':int(status['NoNewPrivs'])}}\n"
        "print(json.dumps({'event':'result','result':{'conversation_id':'actual-private-agy','status':'SUCCESS','response':json.dumps(observed),'usage':{'total_tokens':0},'duration_seconds':0}}),flush=True)\n"
    )
    provider = Provider(
        name="agy-fixture", kind="agy", model="fixture", models=["fixture"]
    )
    picked = picker.PickerResult(None, provider, "fixture", "fixture", None, None)
    monkeypatch.setattr(
        supervisor_module, "_read_roster", lambda cwd: Roster(providers=[provider])
    )
    monkeypatch.setattr(picker, "pick_execution", lambda *args, **kwargs: picked)
    # Isolate the former blanket admission condition; no product input can lift it.
    monkeypatch.setattr(supervisor_module, "AGY_LAUNCH_REFUSAL", None)
    launch = native.NativeLaunch(
        (str(Path("/usr/bin/python3").resolve()), str(peer)),
        {"HOME": str(home), "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
        SecretNeedles.none(),
        NativeSessionContext(str(home), read_roots=(str(peer), str(checkout))),
        NativeAccount(home=home),
    )
    monkeypatch.setattr(native, "prepare_native", lambda *args, **kwargs: launch)
    sv = Supervisor(registry, tmp_path / "runs")
    run_id = None
    try:
        admitted = sv.execute(
            "measure native boundary", str(checkout), read_only=True, timeout_ms=5000
        )
        run_id = admitted.run_id
        assert sv.status(run_id, 5000).phase is RunPhase.COMPLETED
        measured = json.loads(sv.result(run_id).final_text)
        assert measured == {
            "project": False,
            "git": False,
            "outside": False,
            "home": False,
            "no_new_privileges": 1,
        }
        assert (outside / "canary").read_text() == "protected outside"
        assert (checkout / ".git" / "config").read_text() == "protected metadata"
    finally:
        if run_id is not None:
            sv.close(run_id)
        sv.shutdown()


def test_ordinary_native_custody_is_not_a_duplicate_delegated_canvas_session(
    registry: Registry, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    activation = SessionActivation("codex", "codex:fixture@subscription", "fixture")
    owner = f"node:{uuid.uuid4()}"
    for name, is_owner_channel in (
        ("ordinary-owner", True),
        ("delegated-writer", False),
    ):
        row = RunRow(
            run_id=name,
            worktree=str(tmp_path),
            phase=RunPhase.PENDING,
            sandbox="read-only",
            model="fixture",
            owner_instance=registry.instance_id,
            created_at=1000,
            updated_at=1000,
            backend=Backend.CODEX,
            owner=owner,
            executed_provider="codex",
            executed_account="fixture",
        )
        registry.admit_run(
            row,
            False,
            1000,
            1000,
            native_context=NativeSessionContext(str(tmp_path)),
            activation=activation.data() if is_owner_channel else None,
        )
    assert cli.cmd_run_list(["--owner", owner]) == 0
    observed = json.loads(capsys.readouterr().out)
    assert [row["runId"] for row in observed["runs"]] == ["delegated-writer"]
    assert registry.get_run("ordinary-owner") is not None


@pytest.mark.parametrize("provider", ["claude", "codex", "agy"])
def test_route_start_cannot_bypass_the_owning_run_primitive(
    provider: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    effects: list[str] = []
    monkeypatch.setattr(
        route, "route_launch", lambda *args, **kwargs: (["/bin/true"], {})
    )
    monkeypatch.setattr(
        cli.os, "execve", lambda *args: effects.append("unmanaged native launch")
    )
    assert cli.cmd_route_start([provider, "--cwd", str(tmp_path)]) == 2
    assert effects == []


def test_the_owning_native_spawn_retains_only_the_selected_sealed_profile_descriptor(
    tmp_path: Path,
) -> None:
    scope = AggregateScope(
        memory_bytes=Supervisor.WRITER_MEMORY_BYTES, tasks=Supervisor.WRITER_TASKS
    )
    run_dir = tmp_path / "descriptor-run"
    run_dir.mkdir(mode=0o700)
    spawned = None
    try:
        with route.descriptor_seal(b"original private PRELAUNCH input") as descriptor:
            spawned = child.spawn_child(
                [
                    sys.executable,
                    "-c",
                    f"import os; print(os.read({descriptor}, 100).decode())",
                ],
                tmp_path,
                dict(os.environ),
                run_dir,
                SecretNeedles.none(),
                scope=scope,
                pass_fds=(descriptor,),
            )
            assert spawned.wait(timeout=5) == 0
        assert (
            run_dir / "stdout"
        ).read_text().strip() == "original private PRELAUNCH input"
    finally:
        scope.close()
        if spawned is not None:
            spawned.wait(timeout=5)


@pytest.mark.parametrize("stop_reason", ["end_turn", "cancelled"])
def test_passive_creation_and_the_first_owner_turn_share_the_actual_run(
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stop_reason: str,
) -> None:
    peer = tmp_path / "acp-peer.py"
    events = tmp_path / "native-events.jsonl"
    native_session = str(uuid.uuid4())
    peer.write_text(
        "import json, sys\n"
        f"events={str(events)!r}\n"
        "for line in sys.stdin:\n"
        " frame=json.loads(line)\n"
        " with open(events,'a') as output: output.write(json.dumps(frame)+'\\n')\n"
        " if 'id' not in frame: continue\n"
        " method=frame['method']\n"
        " if method=='initialize': result={'protocolVersion':1,'agentCapabilities':{'loadSession':True}}\n"
        f" elif method=='session/new': result={{'sessionId':{native_session!r}}}\n"
        " elif method=='session/prompt':\n"
        "  print(json.dumps({'jsonrpc':'2.0','method':'session/update','params':{'sessionId':frame['params']['sessionId'],'update':{'sessionUpdate':'agent_message_chunk','content':{'type':'text','text':'native owner answer'}}}}),flush=True)\n"
        f"  result={{'stopReason':{stop_reason!r}}}\n"
        " else: result={}\n"
        " print(json.dumps({'jsonrpc':'2.0','id':frame['id'],'result':result}),flush=True)\n"
    )
    monkeypatch.setattr(
        route,
        "route_launch",
        lambda *args, **kwargs: ([sys.executable, "-u", str(peer)], dict(os.environ)),
    )
    sv = Supervisor(registry, tmp_path / "runs", host_control=True)
    server = McpServer(supervisor=sv, host_control=True)
    run_id = None
    attached = None
    try:
        response = _call(
            server,
            "nspawn_run_start",
            {
                "cwd": str(tmp_path),
                "activation": {
                    "provider": "claude",
                    "account_route": "claude:fixture@subscription",
                    "model": "opus",
                    "effort": "high",
                },
            },
        )
        assert "error" not in response, json.dumps(response)
        accepted = _payload(response)
        run_id = accepted["run_id"]
        shown = _payload(_call(server, "nspawn_run_show", {"run_id": run_id}))
        assert shown["phase"] == "pending"
        assert not events.exists()  # Process creation itself takes zero Query.
        attached = socket.socket(socket.AF_UNIX)
        attached.settimeout(3)
        attached.connect(shown["transport"]["path"])
        reader = attached.makefile("rb")

        def request(ident: int, method: str, params: dict) -> dict:
            acknowledged = _payload(
                _call(
                    server,
                    "nspawn_run_start",
                    {
                        "cwd": str(tmp_path),
                        "original_run": run_id,
                        "session_operation": {
                            "id": ident,
                            "method": method,
                            "params": params,
                        },
                    },
                )
            )
            assert acknowledged["run_id"] == run_id
            while True:
                frame = json.loads(reader.readline())
                if frame.get("id") == ident:
                    return frame["result"]

        request(1, "initialize", {"protocolVersion": 1})
        opened = request(2, "session/new", {"cwd": str(tmp_path), "mcpServers": []})
        assert opened["sessionId"] == native_session
        assert sv.status(run_id).phase is RunPhase.PENDING
        assert registry.get_run(run_id).thread_id == native_session
        agent = McpServer(supervisor=sv)
        activation = registry.session_activation(run_id)
        assert activation is not None
        for parameters in (
            {"cwd": str(tmp_path), "activation": activation.data()},
            {
                "cwd": str(tmp_path),
                "original_run": run_id,
                "session_operation": {
                    "id": 99,
                    "method": "session/prompt",
                    "params": {
                        "sessionId": native_session,
                        "prompt": [{"type": "text", "text": "untrusted input"}],
                    },
                },
            },
        ):
            refused = _call(agent, "nspawn_run_start", parameters)
            assert refused["error"]["code"] == -32602
            assert "requires host control" in refused["error"]["message"]
        assert (
            request(
                3,
                "session/prompt",
                {
                    "sessionId": native_session,
                    "prompt": [{"type": "text", "text": "one actual owner turn"}],
                },
            )["stopReason"]
            == stop_reason
        )
        assert sv.status(run_id).phase is RunPhase.IDLE
        assert sv.result(run_id).final_text == "native owner answer"
        turns = [
            event.payload["session_turn"]
            for event in registry.read_run_events(run_id)
            if "session_turn" in event.payload
        ]
        assert len(turns) == 1 and turns[0]["result"]["stopReason"] == stop_reason
        original_artifact = (
            tmp_path / "runs" / run_id / turns[0]["artifact"]
        ).read_bytes()
        assert (
            request(
                4,
                "session/prompt",
                {
                    "sessionId": native_session,
                    "prompt": [
                        {"type": "text", "text": "continue that original owner"}
                    ],
                },
            )["stopReason"]
            == stop_reason
        )
        assert sv.status(run_id).phase is RunPhase.IDLE
        assert registry.get_run(run_id).thread_id == native_session
        assert (
            tmp_path / "runs" / run_id / turns[0]["artifact"]
        ).read_bytes() == original_artifact
        observed = [json.loads(line) for line in events.read_text().splitlines()]
        assert [frame["method"] for frame in observed] == [
            "initialize",
            "session/new",
            "session/prompt",
            "session/prompt",
        ]
        assert len(registry.list_runs()) == 1
        assert sv.close(run_id).phase is RunPhase.CLOSED
        reader.close()
    finally:
        if attached is not None:
            attached.close()
        if run_id is not None:
            sv.close(run_id)
        server.close()
        sv.shutdown()


@pytest.mark.parametrize(
    "field,value",
    [
        ("provider", "other"),
        ("model", "other"),
        ("preferred_model", "other"),
        ("effort", "low"),
        ("timeout_ms", 1000),
    ],
)
def test_passive_activation_refuses_a_second_constructor_before_any_native_effect(
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("a conflicting native constructor reached the launch port")

    monkeypatch.setattr(route, "route_launch", forbidden)
    sv = Supervisor(registry, tmp_path / "runs", host_control=True)
    try:
        with pytest.raises(InvalidRunRequest, match="additional constructor"):
            sv.execute(
                None,
                str(tmp_path),
                activation={
                    "provider": "claude",
                    "account_route": "claude:fixture@subscription",
                    "model": "opus",
                },
                **{field: value},
            )
        assert not registry.list_runs()
    finally:
        sv.shutdown()


@pytest.mark.parametrize("coordinate", ["activation", "session_operation"])
def test_a_native_request_key_binds_the_exact_constructor_or_owner_operation(
    tmp_path: Path,
    coordinate: str,
) -> None:
    original = {"cwd": str(tmp_path), coordinate: {"model": "original"}}
    changed = {"cwd": str(tmp_path), coordinate: {"model": "changed"}}
    assert driver.request_fingerprint(
        "nspawn_run_start", original, group=None
    ) != driver.request_fingerprint("nspawn_run_start", changed, group=None)
