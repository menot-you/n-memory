"""Real native writer probes for connected sends and the MCP consumer."""

from __future__ import annotations

import errno
import json
import os
import select
import shutil
import socket
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from test_native_write import SANDBOX_CHECKS, _inline_table

from nspawn import app_server
from nspawn.owner_roster import Roster

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="native Linux cage"
)


@dataclass
class Cage:
    argv: list[str] = field(repr=False)
    env: dict[str, str] = field(repr=False)
    checkout: Path


@pytest.fixture
def cage(tmp_path: Path) -> Iterator[Cage]:
    selected = os.environ.get("NOTT_NSPAWN_PROBE_BIN")
    if not selected:
        pytest.skip("native cage proof needs NOTT_NSPAWN_PROBE_BIN")
    binary = Path(selected).resolve(strict=True)
    roster = tmp_path / "roster.toml"
    roster.write_text(
        f"[bins]\ncodex = {json.dumps(str(binary))}\n"
        '[[provider]]\nname = "probe"\nkind = "codex"\n'
        'model = "fixture"\nmodels = ["fixture"]\naccounts = []\n'
    )
    configured = Roster.read(roster).providers[0].bin
    assert configured == binary
    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)
    home = Path.home() / f"cage-{tmp_path.name}"
    (home / "codex").mkdir(parents=True)
    tool_home = checkout / "home"
    tool_home.mkdir()
    tool_env = {
        "HOME": str(tool_home),
        "NOTT_DATABASE_DIR": str(checkout / "database"),
        "NOTT_NMEMORY_CONFIG": str(checkout / "absent-memory.toml"),
        "OTEL_SDK_DISABLED": "true",
        "PATH": "/usr/bin:/bin",
        "TMPDIR": str(checkout),
    }
    name = "nott-socketpair-probe"
    profile = app_server.permission_profile((str(binary),), str(checkout))
    argv = [
        str(configured),
        "sandbox",
        "-C",
        str(checkout),
        "-P",
        name,
        "-c",
        f"permissions.{name}.filesystem={_inline_table(profile['filesystem'])}",
        "-c",
        f"permissions.{name}.network={_inline_table(profile['network'])}",
        "-c",
        'shell_environment_policy.inherit="none"',
        "-c",
        f"shell_environment_policy.set={_inline_table(tool_env)}",
        "--",
    ]
    try:
        yield Cage(
            argv,
            {
                "HOME": str(home),
                "CODEX_HOME": str(home / "codex"),
                "PATH": "/usr/bin:/bin",
                "TMPDIR": "/var/tmp",
            },
            checkout,
        )
    finally:
        shutil.rmtree(home)


PAIR = """
import socket
left, right = socket.socketpair()
with left, right:
    left.send(b'x')
    assert right.recv(1) == b'x'
print('connected-send-ok')
"""

WAKEUP = """
import asyncio, threading
loop = asyncio.new_event_loop()
entered = threading.Event()
original_select = loop._selector.select
def selecting(timeout):
    if timeout is None:
        entered.set()
    return original_select(timeout)
loop._selector.select = selecting
thread = threading.Thread(target=loop.run_forever, daemon=True)
thread.start()
assert entered.wait(2)
future = asyncio.run_coroutine_threadsafe(asyncio.sleep(0, result=42), loop)
assert future.result(timeout=2) == 42
loop.call_soon_threadsafe(loop.stop)
thread.join(timeout=2)
assert not thread.is_alive()
loop.close()
print('cross-thread-ok')
"""


@pytest.mark.parametrize("program", [PAIR, WAKEUP], ids=["socketpair", "asyncio"])
def test_writer_local_wakeup(cage: Cage, program: str) -> None:
    argv, env, checkout = cage.argv, cage.env, cage.checkout
    result = subprocess.run(
        [*argv, "/usr/bin/python3", "-c", program],
        env=env,
        cwd=checkout,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    code, error = result.returncode, result.stderr
    assert code == 0, error


def test_writer_addressed_sendto_stays_refused(
    cage: Cage,
) -> None:
    argv, env, checkout = cage.argv, cage.env, cage.checkout
    address = checkout / "receiver.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as receiver:
        receiver.bind(str(address))
        receiver.settimeout(0.2)
        program = """
import errno, socket, sys
left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
with left, right:
    try:
        left.sendto(b'x', sys.argv[1])
    except OSError as error:
        print(error.errno)
    else:
        print(0)
"""
        result = subprocess.run(
            [*argv, "/usr/bin/python3", "-c", program, str(address)],
            env=env,
            cwd=checkout,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        code, error, output = result.returncode, result.stderr, result.stdout
        assert code == 0, error
        observed = int(output.strip())
        if observed == 0:
            assert receiver.recv(1) == b"x"
        assert observed == errno.EPERM


def test_writer_initializes_the_real_memory_server(
    cage: Cage,
) -> None:
    argv, env, checkout = cage.argv, cage.env, cage.checkout
    runtime = os.environ.get("NOTT_NSPAWN_PROBE_RUNTIME")
    if not runtime:
        pytest.skip("MCP cage proof needs NOTT_NSPAWN_PROBE_RUNTIME")
    target = checkout / "runtime"
    shutil.copytree(runtime, target, symlinks=True)
    frames: list[dict[str, Any]] = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "cage-probe", "version": "1"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    ]
    with (checkout / "stderr.txt").open("w+") as error:
        process = subprocess.Popen(
            [*argv, str(target / "bin/python"), "-m", "nmemory", "mcp", "serve"],
            env=env,
            cwd=checkout,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=error,
            text=True,
        )
        assert process.stdin is not None and process.stdout is not None
        try:
            replies = []
            for frame in frames:
                process.stdin.write(json.dumps(frame) + "\n")
                process.stdin.flush()
                if "id" not in frame:
                    continue
                ready = select.select([process.stdout], [], [], 5)[0]
                if not ready:
                    pytest.fail(f"MCP {frame['method']} timed out")
                line = process.stdout.readline()
                error.flush()
                error.seek(0)
                assert line, error.read()
                reply = json.loads(line)
                assert reply.get("id") == frame["id"] and "error" not in reply
                replies.append(reply["result"])
            assert replies[0]["protocolVersion"]
            assert replies[1]["tools"]
        finally:
            process.stdin.close()
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
            process.stdout.close()


def test_writer_rejects_a_nonzero_address_length(cage: Cage) -> None:
    program = """
import ctypes, errno, socket
libc = ctypes.CDLL(None, use_errno=True)
libc.sendto.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t,
                       ctypes.c_int, ctypes.c_void_p, ctypes.c_uint]
left, right = socket.socketpair()
with left, right:
    buffer = ctypes.create_string_buffer(b'x')
    result = libc.sendto(left.fileno(), buffer, 1, 0, None, 1)
    assert result == -1 and ctypes.get_errno() == errno.EPERM
"""
    result = subprocess.run(
        [*cage.argv, "/usr/bin/python3", "-c", program],
        env=cage.env,
        cwd=cage.checkout,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    code, error = result.returncode, result.stderr
    assert code == 0, error


def test_writer_file_and_network_boundary(cage: Cage, tmp_path: Path) -> None:
    checkout = cage.checkout
    (checkout / "tracked.txt").write_text("base")
    (checkout / "doomed.txt").write_text("base")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.txt").write_text("outside")
    program = (
        SANDBOX_CHECKS
        + """
def udp():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
        client.sendto(b'x', ('127.0.0.1', int(port)))
print(json.dumps({
    'outside_read': attempt(lambda: open(os.path.join(outside, 'private.txt')).read()),
    'udp': attempt(udp),
}))
"""
    )
    with socket.create_server(("127.0.0.1", 0)) as listener:
        result = subprocess.run(
            [
                *cage.argv,
                "/usr/bin/python3",
                "-c",
                program,
                str(checkout),
                str(outside),
                str(listener.getsockname()[1]),
                f"nspawn-boundary-{tmp_path.name}",
            ],
            env=cage.env,
            cwd=checkout,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    code, output, error = result.returncode, result.stdout, result.stderr
    assert code == 0, error
    results = {}
    for line in output.splitlines():
        results.update(json.loads(line))
    granted = {name for name, value in results.items() if value == "ok"}
    assert granted == {"create", "edit", "delete", "mkdir"}, results


def test_writer_inherited_stdout_keeps_its_existing_write_capability(
    cage: Cage,
) -> None:
    with (
        socket.create_server(("127.0.0.1", 0)) as listener,
        socket.create_connection(listener.getsockname()) as client,
    ):
        peer, _ = listener.accept()
        with peer:
            peer.settimeout(3)
            result = subprocess.run(
                [
                    *cage.argv,
                    "/usr/bin/python3",
                    "-c",
                    "import os; os.write(1, b'x')",
                ],
                env=cage.env,
                cwd=cage.checkout,
                stdout=client,
                stderr=subprocess.PIPE,
                text=True,
                timeout=15,
                check=False,
            )
            code, error = result.returncode, result.stderr
            assert code == 0, error
            assert peer.recv(1) == b"x"
