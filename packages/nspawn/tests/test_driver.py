"""U1: every dispatch runs in a per-call driver; the stdio process owns no child.

Unit tier. `serve` runs in-process: the double runs it on a thread, and the
survival cases build a SECOND real Supervisor on the same store through the
factory (its own instance id, `owns_supervisor=True`), so closing the
McpServer's own supervisor is a real stdio death and not a shared object going
away. Every serve thread, second Supervisor and registry a test builds is
reaped by `reaper` before the `registry` fixture drops the schema.
"""

from __future__ import annotations

import contextlib
import fcntl
import importlib
import json
import os
import signal
import stat
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from class_dispatch import DECLARED_CLASS, system1_never_reaches_the_network
from mcp_fakes import FakeSupervisor, handle
from registry_reset import sql
from supervisor_support import (
    _drain_worker_threads,
    _stop_every_reaper,
    _url,
    registry,
    roster_home,
)

from nspawn import child, mcp, native, picker, supervisor
from nspawn.custody import ControllerRefusal
from nspawn.mcp import McpServer
from nspawn.owner_roster import Defaults, NativeAccount, Provider, Roster
from nspawn.process_identity import capture_process_identity
from nspawn.redact import SecretNeedles
from nspawn.registry import DriverCeiling, Registry
from nspawn.rows import (
    CLAUDE_CONFINEMENT_POLICY_ONLY,
    Backend,
    NativeSessionContext,
    RunPhase,
    RunRow,
)
from nspawn.supervisor import RunAccepted, Supervisor

__all__ = [
    "_drain_worker_threads",
    "_stop_every_reaper",
    "registry",
    "roster_home",
    "system1_never_reaches_the_network",
]


@pytest.fixture
def drv() -> ModuleType:
    """The driver module, imported per test so its absence reds each case."""
    return importlib.import_module("nspawn.driver")


def _call(server: McpServer, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    answer = handle(
        server,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )
    assert answer is not None
    return answer


def _payload(answer: dict[str, Any]) -> Any:
    assert "error" not in answer, answer
    return json.loads(answer["result"]["content"][0]["text"])


def _now_ms() -> int:
    return int(time.time() * 1000)


def _wait_for(predicate: Callable[[], Any], timeout: float, what: str) -> Any:
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() >= deadline:
            pytest.fail(f"timed out after {timeout} s waiting for {what}")
        time.sleep(0.05)


def _terminal(registry: Registry, run_id: str, timeout: float) -> RunRow:
    def read() -> RunRow | None:
        row = registry.get_run(run_id)
        return row if row is not None and row.phase.is_terminal() else None

    row: RunRow = _wait_for(read, timeout, f"run {run_id} to reach a terminal phase")
    return row


# ---------------------------------------------------------------------------
# Second real Supervisors, and the fixture that reaps them.
# ---------------------------------------------------------------------------


@dataclass
class Drivers:
    """A supervisor factory: each call is one driver's own real Supervisor."""

    run_root: Path
    announces: bool = False
    built: list[Supervisor] = field(default_factory=list)
    wrap: Callable[[Supervisor], Supervisor] | None = None

    def __call__(self) -> Supervisor:
        store = Registry(_url(), f"drv-{uuid.uuid4().hex}").connect()
        store.open(_now_ms())
        built = Supervisor(
            store,
            self.run_root,
            announces_terminal=self.announces,
            sidecar_kind="driver",
        )
        self.built.append(built)
        return built if self.wrap is None else self.wrap(built)


@dataclass
class Reaper:
    """Releases what a test blocked, joins what it started, and fails on a straggler."""

    events: list[threading.Event] = field(default_factory=list)
    threads: list[threading.Thread] = field(default_factory=list)
    launchers: list[Any] = field(default_factory=list)
    drivers: list[Drivers] = field(default_factory=list)
    stores: list[Registry] = field(default_factory=list)
    processes: list[subprocess.Popen[bytes]] = field(default_factory=list)

    def thread(self, target: Callable[[], object]) -> threading.Thread:
        started = threading.Thread(target=target, daemon=True)
        self.threads.append(started)
        started.start()
        return started


@pytest.fixture
def reaper(registry: Registry) -> Iterator[Reaper]:
    held = Reaper()
    yield held
    for event in held.events:
        event.set()
    serving = [
        *held.threads,
        *(thread for launcher in held.launchers for thread in launcher.threads),
    ]
    deadline = time.monotonic() + 30
    for thread in serving:
        thread.join(timeout=max(0.0, deadline - time.monotonic()))
    stragglers = [thread for thread in serving if thread.is_alive()]
    for drivers in held.drivers:
        for built in drivers.built:
            if stragglers:
                with contextlib.suppress(Exception):
                    built.shutdown()
                for spawned in list(built._children.values()):
                    with contextlib.suppress(OSError):
                        os.killpg(spawned.identity.pid, signal.SIGKILL)
            with contextlib.suppress(Exception):
                built._registry.close()
    for process in held.processes:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
    for store in held.stores:
        with contextlib.suppress(Exception):
            store.close()
    if stragglers:
        pytest.fail(f"{len(stragglers)} serve thread(s) outlived their test")


# ---------------------------------------------------------------------------
# A native-write codex run: a checkout under the roster's declared root.
# ---------------------------------------------------------------------------

#: What codex reports for a thread started with a native write profile
#: (tests/test_native_write.py, observed 2026-09-28).
WRITER_SANDBOX = {
    "type": "workspaceWrite",
    "writableRoots": [],
    "networkAccess": False,
    "excludeTmpdirEnvVar": True,
    "excludeSlashTmp": True,
}

#: A codex app-server double whose turn holds until `$WRITER_GATE` exists.
WRITER_SERVER = r"""
import json, os, sys, time
if sys.argv[-3:] == ['mcp', 'list', '--json']:
    print('[]', flush=True)
    raise SystemExit(0)
WRITER = json.loads(os.environ['WRITER_SANDBOX'])
def send(value):
    print(json.dumps(value), flush=True)
for line in sys.stdin:
    frame = json.loads(line)
    method, params = frame.get('method'), frame.get('params') or {}
    if method == 'initialize':
        send({'id': frame['id'], 'result': {'userAgent': 'writer-fixture'}})
    elif method == 'plugin/installed':
        send({'id': frame['id'], 'result': {'marketplaces': [], 'marketplaceLoadErrors': []}})
    elif method == 'thread/start':
        profiles = params.get('config', {}).get('permissions', {})
        profile = profiles.get(params.get('permissions'), {})
        writer = 'write' in profile.get('filesystem', {}).values()
        send({'id': frame['id'], 'result': {'thread': {'id': 'writer-thread'},
              'model': params['model'],
              'sandbox': WRITER if writer else {'type': 'readOnly'},
              'activePermissionProfile': {'id': params['permissions'], 'extends': None}
              if params.get('permissions') else None}})
    elif method == 'mcpServerStatus/list':
        send({'id': frame['id'], 'result': {'data': [], 'nextCursor': None}})
    elif method == 'turn/start':
        send({'id': frame['id'], 'result': {'turn': {'id': 'writer-turn'}}})
        while not os.path.exists(os.environ['WRITER_GATE']):
            time.sleep(0.05)
        send({'method': 'item/completed', 'params': {'threadId': 'writer-thread',
              'turnId': 'writer-turn', 'item': {'id': 'answer', 'type': 'agentMessage',
              'phase': 'final_answer', 'text': 'DONE'}}})
        send({'method': 'turn/completed', 'params': {'threadId': 'writer-thread',
              'turn': {'id': 'writer-turn', 'status': 'completed', 'error': None}}})
"""


@dataclass
class WriteMode:
    worktree: str
    launch: native.NativeLaunch
    gate: Path
    drivers: Drivers


@contextlib.contextmanager
def write_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reaper: Reaper,
    *,
    admitting: threading.Event | None = None,
    entered: threading.Event | None = None,
) -> Iterator[WriteMode]:
    """A codex route with `native_write`, its checkout under the one root the
    roster (`roster_home`) declares; `admitting`/`entered` hold admission after
    the root check, before `admit_run` and the ack."""
    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)
    roster = Path(os.environ["NOTT_NSPAWN_ROSTER"])
    roster.write_text(
        roster.read_text()
        + f"\n[native_write]\nroots = {json.dumps([str(tmp_path)])}\n"
    )
    gate = tmp_path / "gate"
    script = tmp_path / "writer_server.py"
    script.write_text(WRITER_SERVER)
    provider = Provider(
        name="fixture", kind="codex", model="fixture", models=["fixture"]
    )
    selected = picker.PickerResult(None, provider, "fixture", "fixture", None, None)
    monkeypatch.setattr(supervisor.picker, "pick_execution", lambda *a, **k: selected)
    monkeypatch.setattr(Roster, "refuse_unservable_spawn", lambda *args: None)
    declared = supervisor._refuse_undeclared_writer_root

    def admission(worktree: str, roster: Roster) -> None:
        declared(worktree, roster)
        assert worktree == str(checkout)
        if entered is not None:
            entered.set()
        if admitting is not None:
            assert admitting.wait(30), "admission was never released"

    monkeypatch.setattr(supervisor, "_refuse_undeclared_writer_root", admission)
    launch = native.NativeLaunch(
        (sys.executable, "-u", str(script), "app-server", "--listen", "stdio://"),
        {
            "PATH": "/usr/bin:/bin",
            "WRITER_SANDBOX": json.dumps(WRITER_SANDBOX),
            "WRITER_GATE": str(gate),
        },
        SecretNeedles.none(),
        NativeSessionContext(str(tmp_path), read_roots=(os.path.realpath(checkout),)),
        NativeAccount(tmp_path),
    )
    monkeypatch.setattr(native, "prepare_native", lambda *a, **k: launch)
    drivers = Drivers(tmp_path / "runs")
    reaper.drivers.append(drivers)
    if admitting is not None:
        reaper.events.append(admitting)
    try:
        yield WriteMode(str(checkout), launch, gate, drivers)
    finally:
        gate.touch()


def _write_frame(drv: ModuleType, tmp_path: Path) -> dict[str, Any]:
    return dict(
        drv.make_frame(
            "nspawn_run_start",
            {
                "prompt": "fix",
                "cwd": str(tmp_path / "checkout"),
                "execution_class": DECLARED_CLASS,
                "native_write": True,
                "timeout_ms": 30000,
            },
            drivers_dir=tmp_path / "runs" / "drivers",
        )
    )


def _write_arguments(fx: WriteMode) -> dict[str, Any]:
    return {
        "prompt": "fix",
        "cwd": fx.worktree,
        "class": DECLARED_CLASS,
        "native_write": True,
    }


def _writer_gone(registry: Registry, run_id: str) -> bool:
    """The run's writer slice went on record and no longer exists."""
    events = [
        json.loads(payload)
        for (payload,) in sql(
            registry,
            "SELECT payload FROM run_events WHERE run_id = ? AND kind = 'lifecycle' "
            "ORDER BY seq",
            (run_id,),
        ).fetchall()
    ]
    scopes = [event["writer_scope"] for event in events if "writer_scope" in event]
    return bool(scopes) and not any(Path(scope["cgroup"]).exists() for scope in scopes)


def _called(frame: dict[str, Any]) -> dict[str, Any]:
    """The frame of a dispatch whose caller is a live session (this process)."""
    frame["caller"] = {
        "addr": f"/fixture/{os.getpid()}.sock",
        "pid": os.getpid(),
        "start_ticks": capture_process_identity(os.getpid()).start_ticks,
    }
    return frame


def _record_pushes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every outbox send, recorded as delivered instead of written to a socket."""
    sent: list[str] = []
    monkeypatch.setattr(
        supervisor.notify,
        "send_notice",
        lambda addr, body, **_: sent.append(body) or True,
    )
    return sent


def _stdio(registry: Registry, tmp_path: Path) -> Supervisor:
    return Supervisor(registry, tmp_path / "runs", sidecar_kind="stdio")


# ---------------------------------------------------------------------------
# The seam: frame round trip and the launcher rule.
# ---------------------------------------------------------------------------


def test_every_tool_argument_round_trips_through_the_frame_into_the_exact_kwargs(
    drv: ModuleType, tmp_path: Path
) -> None:
    fake = FakeSupervisor(
        execute_result=RunAccepted(run_id="run-x"),
        review_result=RunAccepted(run_id="run-r"),
    )
    server = McpServer(supervisor=fake)
    assert isinstance(server.launcher, drv.InProcessLauncher)
    assert server.launcher.owns_supervisor is False
    tree = str(tmp_path)
    answer = _call(
        server,
        "nspawn_run_start",
        {
            "prompt": "p",
            "cwd": tree,
            "provider": "x",
            "model": "m",
            "effort": "high",
            "class": "design",
            "session_id": "s-1",
            "timeout_ms": 5,
            "read_only": True,
            "read_paths": [tree],
            "system_prompt": "sys",
        },
    )
    assert _payload(answer) == {
        "run_id": "run-x",
        "deduplicated": False,
        "phase": "pending",
    }
    answer = _call(
        server,
        "nspawn_review_start",
        {
            "branch": "lane/x",
            "cwd": tree,
            "base": "main",
            "provider": "x",
            "model": "m",
            "effort": "e",
            "prompt": "q",
            "read_paths": [tree],
            "system_prompt": "review sys",
        },
    )
    assert _payload(answer) == {
        "run_id": "run-r",
        "deduplicated": False,
        "phase": "pending",
    }
    assert fake.calls == [
        (
            "execute",
            {
                "prompt": "p",
                "cwd": tree,
                "provider": "x",
                "model": "m",
                "effort": "high",
                "execution_class": "design",
                "session_id": "s-1",
                "read_only": True,
                "read_paths": (tree,),
                "system_prompt": "sys",
                "timeout_ms": 5,
            },
        ),
        (
            "review",
            {
                "branch": "lane/x",
                "cwd": tree,
                "base": "main",
                "provider": "x",
                "model": "m",
                "effort": "e",
                "prompt": "q",
                "read_paths": (tree,),
                "system_prompt": "review sys",
            },
        ),
    ]
    # Served on the double's own thread, never inline on the stdio's.
    assert server.launcher.threads
    assert all(
        thread is not threading.current_thread() for thread in server.launcher.threads
    )


def test_the_launcher_is_fixed_at_construction_never_from_the_lazy_field(
    drv: ModuleType,
) -> None:
    bare = McpServer()
    assert isinstance(bare.launcher, drv.SystemdLauncher)
    # What `_resolve_supervisor` does on the first status or quota call.
    bare.supervisor = FakeSupervisor()
    assert isinstance(bare.launcher, drv.SystemdLauncher)
    injected = McpServer(supervisor=FakeSupervisor())
    assert isinstance(injected.launcher, drv.InProcessLauncher)


def test_the_frame_caller_is_the_socket_owning_parent_never_this_process(
    drv: ModuleType, tmp_path: Path
) -> None:
    parent = os.getppid()
    socket = f"/run/user/0/cc-socks/{parent}.sock"
    identity = drv.caller_identity({"CLAUDE_CODE_MESSAGING_SOCKET": socket})
    assert identity == {
        "addr": socket,
        "pid": parent,
        "start_ticks": capture_process_identity(parent).start_ticks,
    }
    assert identity["pid"] != os.getpid()
    own = f"/run/user/0/cc-socks/{os.getpid()}.sock"
    assert drv.caller_identity({"CLAUDE_CODE_MESSAGING_SOCKET": own}) is None
    assert drv.caller_identity({}) is None
    frame = drv.make_frame(
        "nspawn_run_start",
        {"prompt": "p", "cwd": str(tmp_path)},
        drivers_dir=None,
        environ={"CLAUDE_CODE_MESSAGING_SOCKET": socket},
    )
    assert frame["caller"] == identity
    assert uuid.UUID(frame["launch_uuid"])
    common = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert frame["project"] == os.path.realpath(common)
    outside = drv.project_key(str(tmp_path))
    assert outside == os.path.realpath(tmp_path)


# ---------------------------------------------------------------------------
# Survival: the run lives in the driver, not in the stdio.
# ---------------------------------------------------------------------------


@pytest.mark.needs("systemd_user", "cgroup", "bwrap")
def test_a_write_mode_run_survives_the_stdio_death_on_a_second_real_supervisor(
    drv: ModuleType,
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    reaper: Reaper,
) -> None:
    with write_mode(tmp_path, monkeypatch, reaper) as fx:
        stdio = _stdio(registry, tmp_path)
        server = McpServer(
            supervisor=stdio,
            launcher=drv.InProcessLauncher(fx.drivers, owns_supervisor=True),
        )
        reaper.launchers.append(server.launcher)
        answer = _call(server, "nspawn_run_start", _write_arguments(fx))
        run_id = _payload(answer)["run_id"]
        assert stdio._children == {} and stdio._writers == {}
        server.close()  # the stdio dies: its supervisor shuts down
        # The held turn has not completed: the run is live and keeps its claim.
        live = registry.get_run(run_id)
        assert live is not None and not live.phase.is_terminal()
        claim = registry.read_claim(fx.worktree)
        assert claim is not None and claim.run_id == run_id
        fx.gate.touch()
        row = _terminal(registry, run_id, 30)
        assert row.phase is RunPhase.COMPLETED, row
        assert row.owner_instance == fx.drivers.built[0]._registry.instance_id
        assert row.owner_instance != registry.instance_id
        # Read at the terminal: the slice was emptied before the terminal write.
        assert _writer_gone(registry, run_id)
        assert fx.launch.env[drv.RUN_ID_ENV] == run_id


@pytest.mark.needs("systemd_user", "cgroup", "bwrap")
def test_a_stdio_death_before_the_ack_never_reaches_the_admitted_run(
    drv: ModuleType,
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    reaper: Reaper,
) -> None:
    admitting, entered = threading.Event(), threading.Event()
    with write_mode(
        tmp_path, monkeypatch, reaper, admitting=admitting, entered=entered
    ) as fx:
        frame = _write_frame(drv, tmp_path)
        answers: list[dict[str, Any]] = []

        def broken_pipe(ack: dict[str, Any]) -> None:
            raise BrokenPipeError(32, "the stdio closed the ack pipe")

        reaper.thread(
            lambda: answers.append(
                drv.serve(
                    frame,
                    broken_pipe,
                    supervisor_factory=fx.drivers,
                    owns_supervisor=True,
                )
            )
        )
        assert entered.wait(20)
        server = McpServer(supervisor=_stdio(registry, tmp_path))
        server.close()  # the stdio dies while admission is blocked
        admitting.set()
        fx.gate.touch()
        _wait_for(lambda: answers, 40, "the driver to finish serving")
        (ack,) = answers
        row = _terminal(registry, ack["run_id"], 5)
        assert row.phase is RunPhase.COMPLETED
        assert _writer_gone(registry, ack["run_id"])
        persisted = tmp_path / "runs" / "drivers" / f"{frame['launch_uuid']}.ack"
        assert json.loads(persisted.read_text()) == ack


@pytest.mark.needs("systemd_user", "cgroup", "bwrap")
def test_the_driver_exits_only_after_push_and_implementation_close(
    drv: ModuleType,
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    reaper: Reaper,
) -> None:
    notices = _record_pushes(monkeypatch)
    with write_mode(tmp_path, monkeypatch, reaper) as fx:
        fx.drivers.announces = True
        fx.gate.touch()
        before = set(threading.enumerate())
        acks: list[dict[str, Any]] = []
        ack = drv.serve(
            _called(_write_frame(drv, tmp_path)),
            acks.append,
            supervisor_factory=fx.drivers,
            owns_supervisor=True,
        )
        assert acks == [ack]
        assert set(threading.enumerate()) <= before
        row = registry.get_run(ack["run_id"])
        assert row is not None and row.phase is RunPhase.COMPLETED
        assert len(notices) == 1 and f"{ack['run_id']} is completed" in notices[0]
        assert _writer_gone(registry, ack["run_id"])
        (built,) = fx.drivers.built
        gone = sql(
            registry,
            "SELECT 1 FROM instances WHERE instance_id = ?",
            (built._registry.instance_id,),
        ).fetchone()
        assert gone is None


@pytest.mark.needs("systemd_user", "cgroup", "bwrap")
def test_a_failed_ack_delivery_after_admission_still_reaches_terminal(
    drv: ModuleType,
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    reaper: Reaper,
) -> None:
    """The U1 form of the lock-commit failure: nothing after `_admit` returns may
    cost the run its terminal or the implementation its close."""
    with write_mode(tmp_path, monkeypatch, reaper) as fx:
        fx.gate.touch()
        frame = _write_frame(drv, tmp_path)
        blocked = tmp_path / "not-a-directory"
        blocked.write_text("")
        frame["drivers_dir"] = str(blocked)

        def refused(ack: dict[str, Any]) -> None:
            raise OSError(5, "the ack fd is gone")

        ack = drv.serve(
            frame, refused, supervisor_factory=fx.drivers, owns_supervisor=True
        )
        row = registry.get_run(ack["run_id"])
        assert row is not None and row.phase is RunPhase.COMPLETED
        assert _writer_gone(registry, ack["run_id"])


@pytest.mark.needs("systemd_user", "cgroup", "bwrap")
def test_two_dispatches_on_one_injected_supervisor_never_shut_it_down(
    drv: ModuleType,
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    reaper: Reaper,
) -> None:
    with write_mode(tmp_path, monkeypatch, reaper) as fx:
        fx.gate.touch()
        stdio = _stdio(registry, tmp_path)
        server = McpServer(supervisor=stdio)
        reaper.launchers.append(server.launcher)
        first = _payload(_call(server, "nspawn_run_start", _write_arguments(fx)))
        assert _terminal(registry, first["run_id"], 30).phase is RunPhase.COMPLETED
        assert _payload(_call(server, "nspawn_run_close", {"run_id": first["run_id"]}))[
            "claim_released"
        ]
        # The injected supervisor was not the double's to shut down.
        assert not stdio._stop_tick.is_set()
        reaper.drivers[0].built.clear()


# ---------------------------------------------------------------------------
# Launch: the default launcher, its refusals and its bounds.
# ---------------------------------------------------------------------------

_STUB = """#!{python}
import fcntl, json, os, sys
def fd_path(n):
    if sys.platform == "darwin":
        return fcntl.fcntl(n, fcntl.F_GETPATH, bytes(1024)).rstrip(b"\\0").decode()
    return os.readlink("/proc/self/fd/%d" % n)
frame = json.loads(sys.stdin.read())
record = {{
    "argv": sys.argv[1:],
    "env": {{k: os.environ.get(k) for k in
             ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "NOTT_PROBE")}},
    "fds": sorted(int(n) for n in os.listdir("/dev/fd")),
    "stdout": fd_path(1),
    "leader": os.getsid(0) == os.getpid(),
    "frame": frame,
}}
with open({record!r}, "w") as out:
    json.dump(record, out)
if {mode!r} == "ack":
    ack = {{"run_id": "stub-run", "deduplicated": False, "phase": "pending"}}
    os.write(frame["ack_fd"], json.dumps(ack).encode() + b"\\n")
    sys.exit(0)
if {mode!r} == "refuse":
    # What the real driver writes for a refusal before its supervisor exists.
    ack = {{"refused": {{"type": "InvalidRunRequest", "message": "NUL", "code": -32602}}}}
    os.write(frame["ack_fd"], json.dumps(ack).encode() + b"\\n")
    sys.exit(0)
if {mode!r} == "hang":
    import time
    time.sleep(1.5)
    sys.exit(0)
sys.stderr.write("stub driver refused\\n")
sys.exit(3)
"""


def _fd_path(number: int) -> str:
    """The file an open descriptor names: `/proc` on Linux, `F_GETPATH` on macOS."""
    if sys.platform == "darwin":
        raw = fcntl.fcntl(number, fcntl.F_GETPATH, bytes(1024))
        return raw.rstrip(b"\0").decode()
    return os.readlink(f"/proc/self/fd/{number}")


_USER_MANAGER = {
    "PATH": "/usr/bin:/bin",
    "XDG_RUNTIME_DIR": "/run/user/4242",
    "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/4242/bus",
}


def _stub_launcher(
    drv: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> Path:
    record = tmp_path / f"stub-{mode}.json"
    stub = tmp_path / f"systemd-run-{mode}"
    stub.write_text(_STUB.format(python=sys.executable, record=str(record), mode=mode))
    stub.chmod(0o755)
    monkeypatch.setattr(drv, "SYSTEMD_RUN", str(stub))
    monkeypatch.setattr(drv, "user_manager_environment", lambda: dict(_USER_MANAGER))
    monkeypatch.setattr(drv, "_linger_enabled", lambda: True)
    weighed = tmp_path / f"weighed-{mode}.json"
    monkeypatch.setattr(
        drv,
        "weigh_custody_parent",
        lambda environment: weighed.write_text(json.dumps(environment)),
    )
    monkeypatch.setenv("NOTT_PROBE", "inherited")
    return record


def test_the_default_launcher_runs_a_scoped_driver_with_the_ack_fd_and_its_log(
    drv: ModuleType,
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _stub_launcher(drv, tmp_path, monkeypatch, "ack")
    server = McpServer(
        supervisor=_stdio(registry, tmp_path), launcher=drv.SystemdLauncher()
    )
    answer = _call(
        server,
        "nspawn_run_start",
        {
            "prompt": "p",
            "cwd": str(tmp_path),
            "class": DECLARED_CLASS,
            "read_only": True,
        },
    )
    assert _payload(answer) == {
        "run_id": "stub-run",
        "deduplicated": False,
        "phase": "pending",
    }
    seen = json.loads(record.read_text())
    launch_uuid = seen["frame"]["launch_uuid"]
    assert seen["argv"] == [
        "--user",
        "--scope",
        "--quiet",
        "--collect",
        f"--unit=nspawn-drive-{launch_uuid}",
        "--slice=nottcontroller.slice",
        "--",
        sys.executable,
        "-P",
        "-m",
        "nspawn.cli",
        "dispatch",
        "run",
    ]
    assert seen["env"] == {
        "XDG_RUNTIME_DIR": _USER_MANAGER["XDG_RUNTIME_DIR"],
        "DBUS_SESSION_BUS_ADDRESS": _USER_MANAGER["DBUS_SESSION_BUS_ADDRESS"],
        "NOTT_PROBE": "inherited",
    }
    assert seen["frame"]["ack_fd"] in seen["fds"]
    assert seen["leader"] is True
    # The shared parent carries its weight before the scope starts under it.
    assert json.loads((tmp_path / "weighed-ack.json").read_text()) == _USER_MANAGER
    log = tmp_path / "runs" / "drivers" / f"{launch_uuid}.log"
    assert seen["stdout"] == os.path.realpath(log)
    assert stat.S_IMODE(log.stat().st_mode) == 0o600
    assert seen["frame"]["arguments"]["read_only"] is True
    # The reservation left when the launch settled.
    left = sql(registry, "SELECT count(*) AS n FROM launch_reservations").fetchone()
    assert left is not None and left["n"] in (0, 1)


def test_a_launcher_that_exits_without_an_ack_is_launch_failed(
    drv: ModuleType,
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_launcher(drv, tmp_path, monkeypatch, "exit")
    server = McpServer(
        supervisor=_stdio(registry, tmp_path), launcher=drv.SystemdLauncher()
    )
    answer = _call(
        server,
        "nspawn_run_start",
        {
            "prompt": "p",
            "cwd": str(tmp_path),
            "class": DECLARED_CLASS,
            "read_only": True,
        },
    )
    data = answer["error"]["data"]
    assert data["type"] == "LaunchFailed"
    assert data["rc"] == 3
    assert (
        data["log"].endswith(".log")
        and "stub driver refused" in Path(data["log"]).read_text()
    )
    left = sql(registry, "SELECT count(*) AS n FROM launch_reservations").fetchone()
    assert left is not None and left["n"] == 0


def test_a_refusal_ack_releases_its_reservation_and_a_pending_one_keeps_it(
    drv: ModuleType,
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A driver that refuses before its supervisor exists never releases its
    slot; left to the lapse, repeated refusals would exhaust DRIVER_CEILING."""

    def reserved() -> int:
        row = sql(registry, "SELECT count(*) AS n FROM launch_reservations").fetchone()
        assert row is not None
        return int(row["n"])

    arguments = {
        "prompt": "p",
        "cwd": str(tmp_path),
        "class": DECLARED_CLASS,
        "read_only": True,
    }
    _stub_launcher(drv, tmp_path, monkeypatch, "refuse")
    server = McpServer(
        supervisor=_stdio(registry, tmp_path), launcher=drv.SystemdLauncher()
    )
    answer = _call(server, "nspawn_run_start", arguments)
    assert answer["error"]["data"]["type"] == "InvalidRunRequest"
    assert reserved() == 0

    # A driver still admitting may yet register: its slot stays until then.
    monkeypatch.setattr(drv, "ACK_WAIT_S", 0.3)
    _stub_launcher(drv, tmp_path, monkeypatch, "hang")
    answer = _call(server, "nspawn_run_start", arguments)
    assert answer["error"]["data"]["type"] == "AdmissionPending"
    assert reserved() == 1


@pytest.mark.parametrize(
    "arguments",
    [
        {"launch_uuid": "5f0c6c52-3c1f-4f7e-9a51-0b8f6f1d2e3a"},
        {"launch_uuid": "not-a-uuid"},
        {"run_id": "5f0c6c52-3c1f-4f7e-9a51-0b8f6f1d2e3a"},
    ],
    ids=["forgotten-launch", "malformed-launch", "unknown-run"],
)
def test_a_refusal_raised_in_the_mcp_process_names_its_type(
    registry: Registry, tmp_path: Path, arguments: dict[str, str]
) -> None:
    """Only a driver's ack carried `type`: a launch nSPAWN had forgotten reached
    the workflow untyped, which could only retry it, so it stayed pending."""
    server = McpServer(supervisor=_stdio(registry, tmp_path))
    answer = _call(server, "nspawn_run_show", arguments)
    assert answer["error"]["data"] == {"tool": "nspawn_run_show", "type": "UnknownRun"}


def _unservable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Roster:
    """A picked model its provider does not serve: the real
    `refuse_unservable_spawn` refuses it with NERR_1105."""
    provider = Provider(name="served", kind="codex", model="m1", models=["m1"])
    roster = Roster(providers=[provider], defaults=Defaults(plugin_root=tmp_path))
    picked = picker.PickerResult(
        requested_provider="served",
        executed_provider=provider,
        executed_account=None,
        model="m2",
        effort=None,
    )
    monkeypatch.setattr(supervisor, "_read_roster", lambda cwd: roster)
    monkeypatch.setattr(supervisor.picker, "pick_execution", lambda *a, **k: picked)
    return roster


def test_a_driver_refusal_names_its_nerr_and_only_when_it_has_one(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _unservable(tmp_path, monkeypatch)
    server = McpServer(supervisor=_stdio(registry, tmp_path))
    arguments = {
        "prompt": "p",
        "cwd": str(tmp_path),
        "class": DECLARED_CLASS,
        "provider": "served",
        "read_only": True,
    }
    answer = _call(server, "nspawn_run_start", arguments)
    data = answer["error"]["data"]
    assert (data["tool"], data["type"], data["nerr"]) == (
        "nspawn_run_start",
        "NSpawnError",
        1105,
    )
    # A refusal without a catalog entry names no code, not a null one.
    writer = {**arguments, "read_only": False, "native_write": True}
    answer = _call(server, "nspawn_run_start", writer)
    assert answer["error"]["data"]["type"] == "InvalidRunRequest", answer
    assert "nerr" not in answer["error"]["data"], answer


def test_a_refusal_raised_in_the_mcp_process_names_its_nerr(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    roster = _unservable(tmp_path, monkeypatch)
    monkeypatch.setattr(
        Supervisor,
        "status",
        lambda self, run_id, timeout_ms=0: roster.refuse_unservable_spawn(
            "served", "m2"
        ),
    )
    server = McpServer(supervisor=_stdio(registry, tmp_path))
    answer = _call(server, "nspawn_run_show", {"run_id": "any"})
    assert answer["error"]["data"] == {
        "tool": "nspawn_run_show",
        "type": "NSpawnError",
        "nerr": 1105,
    }


def test_on_macos_the_default_launcher_bootstraps_a_launchd_job_without_a_manager(
    drv: ModuleType,
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """macOS has no systemd: the driver is a launchd job in the user's GUI domain
    (else the user domain), and no user manager is consulted. The job's own run,
    ack and log are measured on a Mac in test_driver_launchd.py."""
    calls = tmp_path / "launchctl.calls"
    launchctl = tmp_path / "launchctl"
    launchctl.write_text(f'#!/bin/sh\necho "$*" >> {calls}\nexit 5\n')
    launchctl.chmod(0o755)

    def no_manager() -> dict[str, str]:
        pytest.fail("the macOS launcher consulted a user manager")

    monkeypatch.setattr(drv, "user_manager_environment", no_manager)
    monkeypatch.setattr(drv, "_linger_enabled", no_manager)
    monkeypatch.setattr(drv, "LAUNCHCTL", str(launchctl))
    launcher = drv.default_launcher("darwin")
    assert isinstance(drv.default_launcher("linux"), drv.SystemdLauncher)
    server = McpServer(supervisor=_stdio(registry, tmp_path), launcher=launcher)
    answer = _call(
        server,
        "nspawn_run_start",
        {
            "prompt": "p",
            "cwd": str(tmp_path),
            "class": DECLARED_CLASS,
            "read_only": True,
        },
    )
    assert answer["error"]["data"]["type"] == "LaunchFailed"
    domains = [line.split()[:2] for line in calls.read_text().splitlines()]
    assert domains == [
        ["bootstrap", f"gui/{os.getuid()}"],
        ["bootstrap", f"user/{os.getuid()}"],
    ]


def test_no_user_manager_or_no_linger_is_a_typed_refusal_before_any_launch(
    drv: ModuleType,
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_launch(*args: object, **kwargs: object) -> None:
        pytest.fail("a refused launch reached the launch")

    monkeypatch.setattr(drv.SystemdLauncher, "_launch", no_launch)

    def no_manager() -> dict[str, str]:
        raise ControllerRefusal(
            "aggregate custody requires the controller's user manager"
        )

    monkeypatch.setattr(drv, "user_manager_environment", no_manager)
    server = McpServer(
        supervisor=_stdio(registry, tmp_path), launcher=drv.SystemdLauncher()
    )
    arguments = {
        "prompt": "p",
        "cwd": str(tmp_path),
        "class": DECLARED_CLASS,
        "read_only": True,
    }
    answer = _call(server, "nspawn_run_start", arguments)
    assert answer["error"]["data"]["type"] == "DriverUnavailable"
    assert "user manager" in answer["error"]["message"]

    monkeypatch.setattr(drv, "user_manager_environment", lambda: dict(_USER_MANAGER))
    monkeypatch.setattr(drv, "_linger_enabled", lambda: False)
    answer = _call(server, "nspawn_run_start", arguments)
    assert answer["error"]["data"]["type"] == "DriverUnavailable"
    assert "linger" in answer["error"]["message"]


def test_an_ack_past_the_wait_is_admission_pending_then_resolves_by_launch_uuid(
    drv: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reaper: Reaper
) -> None:
    monkeypatch.setattr(drv, "ACK_WAIT_S", 0.3)
    status = supervisor.RunStatus(
        run_id="run-slow",
        phase=RunPhase.RUNNING,
        run_dir="/runs/run-slow",
        failure_reason=None,
        total_seq=0,
        requested_provider=None,
        executed_provider=None,
        executed_account=None,
        warning=None,
        tokens=0,
        marker=None,
    )
    fake = FakeSupervisor(
        execute_result=RunAccepted(run_id="run-slow"), status_result=status, delay=1.5
    )
    fake._run_root_path = tmp_path / "runs"
    server = McpServer(supervisor=fake)
    reaper.launchers.append(server.launcher)
    answer = _call(
        server,
        "nspawn_run_start",
        {"prompt": "p", "cwd": str(tmp_path), "class": DECLARED_CLASS},
    )
    data = answer["error"]["data"]
    assert data["type"] == "AdmissionPending"
    launch_uuid = data["launch_uuid"]
    ack = tmp_path / "runs" / "drivers" / f"{launch_uuid}.ack"
    # Nothing was killed: the admission lands after the answer.
    _wait_for(ack.exists, 10, "the late admission's .ack")
    resolved = _payload(_call(server, "nspawn_run_show", {"launch_uuid": launch_uuid}))
    assert resolved["run_id"] == "run-slow"
    assert ("status", {"run_id": "run-slow", "timeout_ms": 0}) in fake.calls


def test_an_ack_committed_before_the_deadline_but_delivered_after_is_ack_late(
    drv: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reaper: Reaper
) -> None:
    monkeypatch.setattr(drv, "ACK_WAIT_S", 0.5)
    delivered = drv._write_ack

    def slow(writer: Callable[[dict[str, Any]], None], ack: dict[str, Any]) -> None:
        time.sleep(1.5)
        delivered(writer, ack)

    monkeypatch.setattr(drv, "_write_ack", slow)
    fake = FakeSupervisor(execute_result=RunAccepted(run_id="run-late"))
    fake._run_root_path = tmp_path / "runs"
    server = McpServer(supervisor=fake)
    reaper.launchers.append(server.launcher)
    answer = _call(
        server,
        "nspawn_run_start",
        {"prompt": "p", "cwd": str(tmp_path), "class": DECLARED_CLASS},
    )
    assert _payload(answer) == {
        "run_id": "run-late",
        "deduplicated": False,
        "phase": "pending",
        "ack_late": True,
    }


def test_a_late_refusal_is_resolved_through_the_ack_file(
    drv: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reaper: Reaper
) -> None:
    monkeypatch.setattr(drv, "ACK_WAIT_S", 0.3)
    fake = FakeSupervisor(
        execute_result=supervisor.InvalidRunRequest("refused late"), delay=1.0
    )
    fake._run_root_path = tmp_path / "runs"
    server = McpServer(supervisor=fake)
    reaper.launchers.append(server.launcher)
    answer = _call(
        server,
        "nspawn_run_start",
        {"prompt": "p", "cwd": str(tmp_path), "class": DECLARED_CLASS},
    )
    launch_uuid = answer["error"]["data"]["launch_uuid"]
    ack = tmp_path / "runs" / "drivers" / f"{launch_uuid}.ack"
    _wait_for(ack.exists, 10, "the late refusal's .ack")
    late = _call(server, "nspawn_run_show", {"launch_uuid": launch_uuid})
    assert late["error"]["message"] == "refused late"
    assert late["error"]["data"]["type"] == "InvalidRunRequest"


def test_a_batch_entry_whose_driver_dies_is_refused_and_its_siblings_run(
    drv: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reaper: Reaper
) -> None:
    served = drv.serve

    def serve(frame: dict[str, Any], writer: Any, **kwargs: Any) -> Any:
        if frame["arguments"]["provider"] == "bad":
            raise RuntimeError("the driver died before its ack")
        return served(frame, writer, **kwargs)

    monkeypatch.setattr(drv, "serve", serve)
    fake = FakeSupervisor(execute_result=RunAccepted(run_id="r-ok"))
    fake.declared_providers = lambda cwd: (  # type: ignore[method-assign]
        frozenset({"x", "bad"}),
        Path("/fixture/roster.toml"),
    )
    server = McpServer(supervisor=fake)
    reaper.launchers.append(server.launcher)
    answer = _payload(
        _call(
            server,
            "nspawn_batch_start",
            {"prompt": "p", "cwd": str(tmp_path), "providers": ["x", "bad", "x"]},
        )
    )
    # U3: each entry answers whether it reattached and the phase it stands in.
    entry = {
        "provider": "x",
        "run_id": "r-ok",
        "deduplicated": False,
        "phase": "pending",
    }
    assert answer["runs"] == [entry, entry]
    (refused,) = answer["refused"]
    assert refused["provider"] == "bad"
    assert refused["type"] == "LaunchFailed"


@pytest.mark.needs("systemd_user", "cgroup", "bwrap")
def test_a_bind_refusal_after_admit_run_carries_the_run_id_and_failed(
    drv: ModuleType,
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    reaper: Reaper,
) -> None:
    """Native write refuses nothing synchronously after `admit_run`: the nearest
    refusal is the writer slice refused in the driver's worker, after the ack.
    The ack names the admitted run; the run it failed ends FAILED on the
    refusal, releases its claim and leaves no slice on record."""
    with write_mode(tmp_path, monkeypatch, reaper) as fx:

        def refuse(self: Supervisor, run_id: str) -> Any:
            raise ControllerRefusal("the gate refused the writer slice")

        monkeypatch.setattr(Supervisor, "_open_writer", refuse)
        frame = _write_frame(drv, tmp_path)
        ack = drv.serve(
            frame, lambda ack: None, supervisor_factory=fx.drivers, owns_supervisor=True
        )
        assert "refused" not in ack and ack["phase"] == "spawning"
        row = registry.get_run(ack["run_id"])
        assert row is not None and row.phase is RunPhase.FAILED
        assert row.blocked_reason == "ControllerRefusal"
        assert registry.read_claim(fx.worktree) is None
        assert not _writer_gone(registry, ack["run_id"])  # none was ever recorded
        persisted = tmp_path / "runs" / "drivers" / f"{frame['launch_uuid']}.ack"
        assert json.loads(persisted.read_text()) == ack


def test_two_stdios_reserving_batches_at_once_never_exceed_the_ceiling(
    registry: Registry, reaper: Reaper
) -> None:
    stores = [Registry(_url(), f"stdio-{n}").connect() for n in range(2)]
    reaper.stores.extend(stores)
    live = Registry(_url(), "drv-live").connect()
    reaper.stores.append(live)
    live.register_instance(_now_ms())
    live.register_sidecar_instance("driver", _now_ms())
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def reserve(store: Registry) -> None:
        batch = [str(uuid.uuid4()) for _ in range(3)]
        barrier.wait()
        try:
            store.reserve_launches(
                batch, _now_ms(), ceiling=5, stale_after_ms=30_000, lapse_ms=180_000
            )
        except DriverCeiling:
            outcomes.append("refused")
        else:
            outcomes.append("reserved")

    threads = [reaper.thread(lambda store=store: reserve(store)) for store in stores]
    for thread in threads:
        thread.join(timeout=20)
    assert sorted(outcomes) == ["refused", "reserved"]
    counted = sql(registry, "SELECT count(*) AS n FROM launch_reservations").fetchone()
    assert counted is not None and counted["n"] == 3


# ---------------------------------------------------------------------------
# D3 / D4: foreign status and close routing.
# ---------------------------------------------------------------------------


def _foreign_run(owner: Registry, worktree: Path, *, claim: bool = True) -> str:
    run_id = str(uuid.uuid4())
    now = _now_ms()
    owner.admit_run(
        RunRow(
            run_id=run_id,
            thread_id=f"thread-{uuid.uuid4().hex}",
            worktree=str(worktree),
            phase=RunPhase.PENDING,
            sandbox=CLAUDE_CONFINEMENT_POLICY_ONLY,
            model="opus",
            owner_instance=owner.instance_id,
            created_at=now,
            updated_at=now,
            backend=Backend.CLAUDE,
        ),
        claim,
        now,
        Supervisor.RUN_CLAIM_LEASE_MS,
    )
    return run_id


def _peer(reaper: Reaper, instance_id: str, kind: str | None) -> Registry:
    peer = Registry(_url(), instance_id).connect()
    reaper.stores.append(peer)
    peer.register_instance(_now_ms())
    if kind is not None:
        peer.register_sidecar_instance(kind, _now_ms())
    return peer


def _control(registry: Registry, run_id: str) -> dict[str, Any] | None:
    row = sql(
        registry, "SELECT * FROM run_control WHERE run_id = ?", (run_id,)
    ).fetchone()
    if row is None:
        return None
    control = dict(row)
    if control["outcome"] is not None:
        control["outcome"] = json.loads(control["outcome"])
    return control


def test_status_on_a_foreign_run_rereads_the_row_every_second(
    registry: Registry, tmp_path: Path, reaper: Reaper
) -> None:
    peer = _peer(reaper, "drv-foreign", None)
    run_id = _foreign_run(peer, tmp_path, claim=False)
    stdio = Supervisor(registry, tmp_path / "runs")

    def finish() -> None:
        time.sleep(0.5)
        row = peer.get_run(run_id)
        assert row is not None
        from dataclasses import replace

        peer.update_run(replace(row, phase=RunPhase.COMPLETED, updated_at=_now_ms()))

    reaper.thread(finish)
    started = time.monotonic()
    status = stdio.status(run_id, timeout_ms=8000)
    assert status.phase is RunPhase.COMPLETED
    assert time.monotonic() - started < 3


def test_closing_a_run_owned_by_a_live_non_driver_instance_closes_locally(
    registry: Registry, tmp_path: Path, reaper: Reaper
) -> None:
    peer = _peer(reaper, "v10-session", None)
    run_id = _foreign_run(peer, tmp_path)
    server = McpServer(supervisor=_stdio(registry, tmp_path))
    started = time.monotonic()
    readback = _payload(_call(server, "nspawn_run_close", {"run_id": run_id}))
    assert readback["phase"] == "closed" and readback["claim_released"] is True
    assert time.monotonic() - started < 3
    assert _control(registry, run_id) is None


def test_a_stale_owner_mid_wait_switches_to_the_local_close(
    registry: Registry, tmp_path: Path, reaper: Reaper
) -> None:
    peer = _peer(reaper, "drv-going-stale", "driver")
    run_id = _foreign_run(peer, tmp_path)
    stdio = _stdio(registry, tmp_path)

    def go_stale() -> None:
        time.sleep(1.5)
        sql(
            peer,
            "UPDATE instances SET heartbeat_at = 1 WHERE instance_id = ?",
            (peer.instance_id,),
        )

    reaper.thread(go_stale)
    started = time.monotonic()
    readback = stdio.close(run_id)
    elapsed = time.monotonic() - started
    assert readback.phase is RunPhase.CLOSED and readback.claim_released
    assert 1.0 < elapsed < 5.0
    control = _control(registry, run_id)
    assert control is not None and control["close_requested_at"] is not None
    assert control["claimed_by"] == registry.instance_id
    assert control["outcome"]["phase"] == "closed"


_HEARTBEATING_DRIVER = """
import sys, time
from nspawn.registry import Registry
store = Registry(sys.argv[1], sys.argv[2]).connect()
while True:
    now = int(time.time() * 1000)
    store.heartbeat_instance(now)
    store.register_sidecar_instance("driver", now)
    time.sleep(0.2)
"""


def test_a_close_claimed_by_a_sigkilled_driver_is_taken_over_by_the_next_close(
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reaper: Reaper,
) -> None:
    monkeypatch.setattr(Supervisor, "STALE_AFTER_MS", 2000)
    owner = _peer(reaper, f"drv-{uuid.uuid4().hex}", "driver")
    run_id = _foreign_run(owner, tmp_path)
    driver = subprocess.Popen(
        [sys.executable, "-c", _HEARTBEATING_DRIVER, _url(), owner.instance_id]
    )
    reaper.processes.append(driver)
    sql(
        registry,
        "INSERT INTO run_control"
        "(run_id, close_requested_at, claimed_at, claimed_by) "
        "VALUES (?, ?, ?, ?)",
        (run_id, _now_ms(), _now_ms(), owner.instance_id),
    )
    time.sleep(0.5)
    driver.send_signal(signal.SIGKILL)
    driver.wait(timeout=5)
    stdio = _stdio(registry, tmp_path)
    started = time.monotonic()
    readback = stdio.close(run_id)
    assert readback.phase is RunPhase.CLOSED and readback.claim_released
    assert time.monotonic() - started < 8
    control = _control(registry, run_id)
    assert control is not None and control["claimed_by"] == registry.instance_id


@pytest.mark.needs("systemd_user", "cgroup", "bwrap")
def test_a_close_request_is_served_by_the_driver_in_process_close(
    drv: ModuleType,
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    reaper: Reaper,
) -> None:
    notices = _record_pushes(monkeypatch)
    with write_mode(tmp_path, monkeypatch, reaper) as fx:
        fx.drivers.announces = True
        frame = _called(_write_frame(drv, tmp_path))
        acks: list[dict[str, Any]] = []
        reaper.thread(
            lambda: drv.serve(
                frame, acks.append, supervisor_factory=fx.drivers, owns_supervisor=True
            )
        )
        (ack,) = _wait_for(lambda: acks, 30, "the driver's ack")
        run_id = ack["run_id"]

        def running() -> bool:
            row = registry.get_run(run_id)
            return row is not None and row.process_identity is not None

        _wait_for(running, 30, "the codex child to publish its identity")
        stdio = _stdio(registry, tmp_path)
        readback = stdio.close(run_id)
        assert readback.phase is RunPhase.CLOSED
        control = _control(registry, run_id)
        assert control is not None
        assert control["claimed_by"] == fx.drivers.built[0]._registry.instance_id
        assert control["outcome"]["phase"] == "closed"
        assert _writer_gone(registry, run_id)
        assert registry.read_claim(fx.worktree) is None
        # The held turn never completed: the store records the close, not a finish.
        closed_row = registry.get_run(run_id)
        assert closed_row is not None and closed_row.phase is RunPhase.CLOSED
        closed = [notice for notice in notices if f"{run_id} is closed" in notice]
        assert len(closed) == 1 and len(notices) == 1


def _holds_within(predicate: Callable[[], bool], timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
    return True


@pytest.mark.needs("systemd_user", "cgroup", "bwrap")
def test_a_served_close_records_its_outcome_before_the_driver_leaves(
    drv: ModuleType,
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    reaper: Reaper,
) -> None:
    """The pause sits where the watcher has closed the run but not yet recorded
    it, outside the supervisor's lock. A driver that exits there drops its
    instance row, and the waiting stdio reads a claim held by a dead instance
    and takes over a close the driver already made."""

    def paused(built: Supervisor) -> Supervisor:
        real = built.close_local
        me = built._registry.instance_id

        def close_local(run_id: str) -> Any:
            readback = real(run_id)

            def deregistered() -> bool:
                custody = registry.run_custody(
                    run_id, _now_ms(), Supervisor.STALE_AFTER_MS
                )
                return custody is not None and not custody[2]

            # A driver that leaves without joining deregisters within milliseconds:
            # with `stop()`'s join removed this failed 3 of 3 at 1 s (2026-09-25).
            # The correct driver never deregisters here, so the window is paid in full.
            if _holds_within(deregistered, 1):
                _holds_within(
                    lambda: (_control(registry, run_id) or {}).get("claimed_by") != me,
                    3,
                )
            return readback

        built.close_local = close_local  # type: ignore[method-assign]
        return built

    with write_mode(tmp_path, monkeypatch, reaper) as fx:
        fx.drivers.wrap = paused
        frame = _write_frame(drv, tmp_path)
        acks: list[dict[str, Any]] = []
        reaper.thread(
            lambda: drv.serve(
                frame, acks.append, supervisor_factory=fx.drivers, owns_supervisor=True
            )
        )
        (ack,) = _wait_for(lambda: acks, 30, "the driver's ack")
        run_id = ack["run_id"]
        _wait_for(
            lambda: getattr(registry.get_run(run_id), "process_identity", None),
            30,
            "the codex child to publish its identity",
        )
        readback = _stdio(registry, tmp_path).close(run_id)
        assert readback.phase is RunPhase.CLOSED
        control = _control(registry, run_id)
        assert control is not None
        assert control["claimed_by"] == fx.drivers.built[0]._registry.instance_id
        assert control["outcome"]["phase"] == "closed"


@pytest.mark.needs("systemd_user", "cgroup", "bwrap")
def test_a_close_between_the_push_and_remove_instance_is_still_served(
    drv: ModuleType,
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    reaper: Reaper,
) -> None:
    at_shutdown, release = threading.Event(), threading.Event()
    reaper.events.append(release)

    def paused(built: Supervisor) -> Supervisor:
        real = built.shutdown

        def shutdown() -> None:
            at_shutdown.set()
            release.wait(20)
            real()

        built.shutdown = shutdown  # type: ignore[method-assign]
        return built

    with write_mode(tmp_path, monkeypatch, reaper) as fx:
        fx.drivers.wrap = paused
        fx.gate.touch()
        frame = _write_frame(drv, tmp_path)
        acks: list[dict[str, Any]] = []
        server_thread = reaper.thread(
            lambda: drv.serve(
                frame, acks.append, supervisor_factory=fx.drivers, owns_supervisor=True
            )
        )
        assert at_shutdown.wait(30)
        (ack,) = acks
        assert registry.get_run(ack["run_id"]).phase is RunPhase.COMPLETED  # type: ignore[union-attr]
        stdio = _stdio(registry, tmp_path)
        answers: list[Any] = []
        closer = reaper.thread(lambda: answers.append(stdio.close(ack["run_id"])))
        _wait_for(
            lambda: (_control(registry, ack["run_id"]) or {}).get("close_requested_at"),
            10,
            "the close request",
        )
        release.set()
        closer.join(timeout=30)
        server_thread.join(timeout=30)
        (readback,) = answers
        assert readback.phase is RunPhase.CLOSED and readback.claim_released
        control = _control(registry, ack["run_id"])
        assert control is not None and control["outcome"]["phase"] == "closed"


@pytest.mark.needs("systemd_user", "cgroup", "bwrap")
def test_closing_a_completed_write_mode_run_after_its_driver_exited(
    drv: ModuleType,
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    reaper: Reaper,
) -> None:
    with write_mode(tmp_path, monkeypatch, reaper) as fx:
        fx.gate.touch()
        ack = drv.serve(
            _write_frame(drv, tmp_path),
            lambda ack: None,
            supervisor_factory=fx.drivers,
            owns_supervisor=True,
        )
        server = McpServer(
            supervisor=_stdio(registry, tmp_path),
            launcher=drv.InProcessLauncher(fx.drivers, owns_supervisor=True),
        )
        readback = _payload(
            _call(server, "nspawn_run_close", {"run_id": ack["run_id"]})
        )
        assert readback["phase"] == "closed" and readback["claim_released"] is True
        assert registry.read_claim(fx.worktree) is None


# ---------------------------------------------------------------------------
# The driver process entry, in-process, and the run marker.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("host_control", [False, True])
def test_drive_main_moves_its_fds_to_the_log_and_hands_serve_the_frame(
    drv: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host_control: bool
) -> None:
    run_root = tmp_path / "runs"
    monkeypatch.setenv("NOTT_NSPAWN_RUN_ROOT", str(run_root))
    seen: dict[str, Any] = {}

    def serve(frame: dict[str, Any], writer: Any, **kwargs: Any) -> dict[str, Any]:
        seen["frame"] = frame
        seen["kwargs"] = kwargs
        seen["fds"] = [_fd_path(n) for n in (0, 1, 2)]
        kwargs["supervisor_factory"]()
        return {}

    monkeypatch.setattr(drv, "serve", serve)
    monkeypatch.setattr(
        drv,
        "_driver_supervisor",
        lambda launch_uuid, **options: seen.update(options),
    )
    frame = drv.make_frame(
        "nspawn_run_start", {"prompt": "p", "cwd": str(tmp_path)}, drivers_dir=None
    )
    frame["host_control"] = not host_control
    reader, writer = os.pipe()
    os.write(writer, json.dumps(frame).encode())
    os.close(writer)
    saved = [os.dup(n) for n in (0, 1, 2)]
    try:
        os.dup2(reader, 0)
        os.close(reader)
        rc = drv.drive_main(["--host-control"] if host_control else [])
    finally:
        for number, copy in enumerate(saved):
            os.dup2(copy, number)
            os.close(copy)
    assert rc == 0
    log = run_root / "drivers" / f"{frame['launch_uuid']}.log"
    assert seen["fds"] == [os.path.realpath(log)] * 3
    assert stat.S_IMODE(log.stat().st_mode) == 0o600
    assert seen["frame"]["launch_uuid"] == frame["launch_uuid"]
    assert seen["kwargs"]["owns_supervisor"] is True
    assert seen["host_control"] is host_control
    recorded = json.loads(
        (run_root / "drivers" / f"{frame['launch_uuid']}.pid").read_text()
    )
    assert recorded["pid"] == os.getpid()
    assert recorded["start_ticks"] == capture_process_identity(os.getpid()).start_ticks
    assert recorded["unit"] == f"nspawn-drive-{frame['launch_uuid']}.scope"

    from nspawn.cli import main

    assert main(["dispatch", "run", "--check"]) == 0


def test_the_run_marker_reaches_the_claude_child_env(
    drv: ModuleType, tmp_path: Path
) -> None:
    token = tmp_path / "token"
    token.write_text("a-real-credential-value-that-is-long-enough")
    provider = Provider(name="claude", token_file=token)
    env, _ = child.build_child_env(provider, None, ambient={}, run_id="run-marker")
    assert env[drv.RUN_ID_ENV] == "run-marker"
    unmarked, _ = child.build_child_env(provider, None, ambient={})
    assert drv.RUN_ID_ENV not in unmarked


def test_the_stdio_answers_ping_without_any_launcher(drv: ModuleType) -> None:
    """`nspawn_route_ping` is the stated in-process exemption."""

    class Refusing:
        def __init__(self) -> None:
            self.threads: list[threading.Thread] = []

        def dispatch(self, *args: object, **kwargs: object) -> list[dict[str, Any]]:
            pytest.fail("ping reached the launcher")

    fake = FakeSupervisor()
    fake.fast_models = lambda cwd: {}  # type: ignore[method-assign]
    server = McpServer(supervisor=fake, launcher=Refusing())  # type: ignore[arg-type]
    answer = _payload(_call(server, "nspawn_route_ping", {"cwd": "/tmp"}))
    assert answer["routes"] == []
    assert mcp.McpServer.nspawn_route_ping.__qualname__.startswith("McpServer")


@pytest.mark.parametrize("reset", [1_790_000_000, None])
def test_a_walled_refusal_carries_its_reset_to_the_caller(
    drv: ModuleType, reset: int | None
) -> None:
    """The picker's typed reset survives the ack and the MCP error, or is absent."""
    walled = picker.AllProvidersWalled('provider "zai" is cooling down', reset)
    refused = drv.refusal_ack(walled)
    assert refused["refused"]["type"] == "AllProvidersWalled"
    with pytest.raises(mcp.McpError) as caught:
        mcp._acknowledged("nspawn_run_start", refused)
    data = caught.value.data or {}
    if reset is None:
        assert "nearest_reset" not in refused["refused"]
        assert "nearest_reset" not in data
    else:
        assert refused["refused"]["nearest_reset"] == reset
        assert data["nearest_reset"] == reset
