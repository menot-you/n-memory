"""The live release reaches a LIVE session: the server exits and the client relaunches.

MEASURED 2026-09-15T20:09Z on a disposable Claude Code client (nSPAWN run
8a089819): killing the MCP server process does not lose tools — the next call
to `nmemory_context_show` responded without error and a new process was born in the
live release, in the same client's tree. That makes relaunching a
usable mechanism rather than an accident.

The scale is a SNAPSHOT: the target of the launch path (`sys.argv[0]`) at the
moment the server starts, compared after each answer. The first iteration
compared with `sys.prefix` and turned twenty member tests red — `python -m
nmemory` puts in `sys.argv[0]` the package's `__main__.py`, which does not live
inside `.venv`, and the server exited right after `initialize`. The falsifier
for that regression is `test_a_module_launch_is_never_stale` below.

Every case drives a real server process over real pipes: the child sets its own
`sys.argv[0]` to the launch path under test, as a client's spawn would.
"""

from __future__ import annotations

import json
import os
import selectors
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import anyio
import mcp_types as types
import pytest
from child_env import child_env
from mcp.shared.message import SessionMessage
from stdio_peer import INITIALIZED, complete
from storefile import default_store_path

from nmemory.server import MemoryServer, _ExitOnNewRelease, _spawn_target, build

_MEMBER = str(Path(__file__).resolve().parent.parent)
# argv[1] becomes the child's argv[0]: the launch path the server snapshots.
_CHILD = (
    "import sys\n"
    f"sys.path.insert(0, {_MEMBER!r})\n"
    "sys.argv = sys.argv[1:]\n"
    "from nmemory.cli import main\n"
    "sys.exit(main())\n"
)
CLAUDE = "claude-code/2.1.270"


def _layout(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Two releases and the stable link that the client launches, as in installation."""
    old = tmp_path / "rel-aaaaaaaa"
    new = tmp_path / "rel-bbbbbbbb"
    for rel in (old, new):
        (rel / "bin").mkdir(parents=True)
        (rel / "bin" / "nmemory").write_text("#!/bin/sh\n")
    link = tmp_path / "live"
    link.symlink_to(old / "bin" / "nmemory")
    return old, new, link


def _listing(id_: int) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "method": "tools/list"}


def _drive(
    argv0: str,
    frames: list[dict[str, Any]],
    *,
    client: str = CLAUDE,
    after_answer: Callable[[], None] | None = None,
    env: dict[str, str] | None = None,
    mode: tuple[str, ...] = ("--read-only",),
) -> tuple[list[dict[str, Any]], int]:
    """Handshake as `client`, then send each frame and read its answer.

    `after_answer` runs once, after the first answer past the handshake. Returns
    the answers past the handshake and the exit code once stdin closes."""
    name, _, version = client.partition("/")
    with subprocess.Popen(
        [sys.executable, "-c", _CHILD, argv0, "mcp", "serve", *mode],
        env=env or child_env(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    ) as proc:
        assert proc.stdin is not None and proc.stdout is not None
        init = {"id": 0, "method": "initialize"}
        init["params"] = {"clientInfo": {"name": name, "version": version}}
        proc.stdin.write(json.dumps(complete(init)) + "\n")
        proc.stdin.flush()
        assert "result" in proc.stdout.readline()
        proc.stdin.write(json.dumps(INITIALIZED) + "\n")
        answers: list[dict[str, Any]] = []
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdout, selectors.EVENT_READ)
            for frame in frames:
                try:
                    proc.stdin.write(json.dumps(frame) + "\n")
                    proc.stdin.flush()
                except BrokenPipeError:
                    break
                if not selector.select(30):
                    break
                line = proc.stdout.readline()
                if not line:
                    break
                answers.append(json.loads(line))
                if after_answer is not None and len(answers) == 1:
                    after_answer()
        try:
            proc.stdin.close()
        except BrokenPipeError:
            pass
        return answers, proc.wait(timeout=30)


def _repoint(link: Path, target: Path) -> Callable[[], None]:
    def swap() -> None:
        link.unlink()
        link.symlink_to(target)

    return swap


def test_the_server_answers_first_and_only_then_exits(tmp_path: Path) -> None:
    """The order is load-bearing: exiting before responding left a request unanswered."""
    _old, new, link = _layout(tmp_path)
    frames = [_listing(i) for i in range(1, 5)]

    answers, code = _drive(
        str(link), frames, after_answer=_repoint(link, new / "bin" / "nmemory")
    )

    assert code == 0, "the exit is clean, exit code 0"
    assert answers[0]["id"] == 1 and "result" in answers[0]
    assert all("result" in a for a in answers), "every answer goes out WHOLE"
    # The swap lands after answer 1: at most the next answer, then the exit.
    assert len(answers) <= 2, [a["id"] for a in answers]


def test_the_server_keeps_serving_while_the_link_points_here(tmp_path: Path) -> None:
    _old, _new, link = _layout(tmp_path)
    answers, code = _drive(str(link), [_listing(i) for i in (1, 2, 3)])
    assert [a["id"] for a in answers] == [1, 2, 3], "without a swap, it serves all"
    assert code == 0


def test_a_module_launch_is_never_stale(tmp_path: Path) -> None:
    """`python -m nmemory` — the launch that the first form killed.

    The package's `__main__.py` does not live inside `.venv`, and comparing with
    `sys.prefix` declared a newly born server stale. No link, no swap.
    """
    script = tmp_path / "nmemory" / "__main__.py"
    script.parent.mkdir()
    script.write_text("# entry point\n")
    answers, _ = _drive(str(script), [_listing(1), _listing(2)])
    assert [a["id"] for a in answers] == [1, 2], "a start by module is not stale"


def test_a_process_launched_straight_from_the_release_is_never_stale(
    tmp_path: Path,
) -> None:
    """Without this there would be a cycle: exit, relaunch from the live release, exit again."""
    _old, new, link = _layout(tmp_path)
    release = new / "bin" / "nmemory"
    answers, _ = _drive(
        str(release),
        [_listing(i) for i in (1, 2, 3)],
        after_answer=_repoint(link, release),
    )
    assert [a["id"] for a in answers] == [1, 2, 3], (
        "a process launched through the release path sees no swap at all"
    )


def test_an_unresolvable_spawn_path_never_exits_the_server() -> None:
    """Without a target there is no swap to detect; and a read error does not crash memory."""
    assert _spawn_target("") is None
    assert _spawn_target("-c") is None
    assert _spawn_target("nmemory") is None
    assert _spawn_target("/does/not/exist/nmemory") is None

    answers, _ = _drive("-c", [_listing(1), _listing(2)])
    assert [a["id"] for a in answers] == [1, 2]


def test_a_spawn_path_that_only_appears_later_is_not_a_release_change(
    tmp_path: Path,
) -> None:
    """Starting without a target and gaining one is not a release swap.

    This is the case of the installer creating the link after this process is born: without the
    startup guard, the first target seen would be read as a change and the
    server would exit without ever having been stale.
    """
    _old, new, _link = _layout(tmp_path)
    late = tmp_path / "late-link"
    answers, _ = _drive(
        str(late),
        [_listing(i) for i in (1, 2, 3)],
        after_answer=lambda: late.symlink_to(new / "bin" / "nmemory"),
    )
    assert [a["id"] for a in answers] == [1, 2, 3], (
        "a process started without a target has no previous release to compare with"
    )


def test_the_link_vanishing_does_not_exit_the_server(tmp_path: Path) -> None:
    """With nowhere to relaunch to, exiting would leave the session without memory."""
    _old, _new, link = _layout(tmp_path)
    answers, _ = _drive(
        str(link), [_listing(i) for i in (1, 2, 3)], after_answer=link.unlink
    )
    assert [a["id"] for a in answers] == [1, 2, 3], (
        "a deleted link is not a new release"
    )


def test_a_pipelined_request_dies_with_the_process_and_is_not_silent(
    tmp_path: Path,
) -> None:
    """The real cost of the swap: a request already read dies with the process.

    The new process receives a new pipe, and what this one had already read dies
    with it. The client is left with no response for that request, which it sees
    as a failed call (visible, not silent) and retries. A test that asserts the
    opposite of production is worse than none: it prevents someone from looking
    for the defect.
    """
    _old, new, link = _layout(tmp_path)
    name, _, version = CLAUDE.partition("/")
    with subprocess.Popen(
        [sys.executable, "-c", _CHILD, str(link), "mcp", "serve", "--read-only"],
        env=child_env(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    ) as proc:
        assert proc.stdin is not None and proc.stdout is not None
        init = {"id": 0, "method": "initialize"}
        init["params"] = {"clientInfo": {"name": name, "version": version}}
        proc.stdin.write(json.dumps(complete(init)) + "\n")
        proc.stdin.flush()
        assert "result" in proc.stdout.readline()
        # The handshake answer runs no check: the client is named to the tools
        # only on the first listing or call.
        _repoint(link, new / "bin" / "nmemory")()
        # Two requests at once: this is the case named by the review.
        proc.stdin.write(
            json.dumps(INITIALIZED)
            + "\n"
            + json.dumps(_listing(2))
            + "\n"
            + json.dumps(_listing(3))
            + "\n"
        )
        proc.stdin.flush()
        answered = [json.loads(line)["id"] for line in proc.stdout]
        assert proc.wait(timeout=30) == 0, "the exit is clean, exit code 0"
    assert 2 in answered, "the request in hand is answered"
    assert 3 not in answered, (
        "and the one behind it gets NO answer — the declared cost of the swap"
    )


def test_a_client_whose_respawn_is_not_measured_never_loses_its_server(
    tmp_path: Path,
) -> None:
    """The list is of MEASUREMENTS: exiting a client that does not relaunch wipes memory.

    `codex-mcp-client` wrote 3457 capsules in this store and its behavior
    upon MCP server death is not measured — nSPAWN refuses dispatch
    for this route. A Codex session remains as it is today: pinned to its release until
    it restarts. Without this fence, an install would drop memory for 96% of traffic
    all at once (independent review).
    """
    _old, new, link = _layout(tmp_path)
    answers, _ = _drive(
        str(link),
        [_listing(i) for i in (1, 2, 3)],
        client="codex-mcp-client/0.154.0",
        after_answer=_repoint(link, new / "bin" / "nmemory"),
    )
    assert [a["id"] for a in answers] == [1, 2, 3], "serves all, despite the swap"


def test_a_client_that_names_itself_nothing_never_loses_its_server(
    tmp_path: Path,
) -> None:
    """With an empty client name the harness is `unknown` — and `unknown` relaunches nothing."""
    _old, new, link = _layout(tmp_path)
    answers, _ = _drive(
        str(link),
        [_listing(i) for i in (1, 2, 3)],
        client="",
        after_answer=_repoint(link, new / "bin" / "nmemory"),
    )
    assert [a["id"] for a in answers] == [1, 2, 3]


def test_the_check_runs_after_a_tool_call(tmp_path: Path) -> None:
    """The path that matters is `tools/call`: the swap after its answer ends the process."""
    store = default_store_path()
    env = child_env()
    env["NOTT_DATABASE_DIR"] = str(store.parent)
    env.pop("NOTT_NMEMORY_CONFIG", None)
    _old, new, link = _layout(tmp_path)
    call = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "nmemory_context_show",
            "arguments": {"query": "release probe", "sources": ["memory"]},
        },
    }
    answers, code = _drive(
        str(link),
        [call, _listing(2), _listing(3)],
        after_answer=_repoint(link, new / "bin" / "nmemory"),
        env=env,
    )
    assert answers[0]["id"] == 1 and "result" in answers[0], answers[0]
    assert len(answers) <= 2 and code == 0, [a["id"] for a in answers]


def test_a_call_that_arrives_after_the_swap_never_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No call commits without an answer.

    Call 1 swaps the release and is still running when call 2 arrives. Call 2
    never runs, and the exit waits for answer 1, even past another answer: the client sees it fail and
    retries it on the new release (had it run here, the retry would write it
    twice)."""
    _old, new, link = _layout(tmp_path)
    monkeypatch.setattr(sys, "argv", [str(link)])
    ran: list[int] = []
    swapped, go = threading.Event(), threading.Event()

    class Server:
        client_name = None
        client_harness = "unknown"
        read_only = True
        inherited = False

        def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            ran.append(arguments["n"])
            if arguments["n"] == 1:
                _repoint(link, new / "bin" / "nmemory")()
                swapped.set()
                assert go.wait(10)
            return {"result": {"content": [], "isError": False}}

        def flush_read_traffic(self) -> None:
            pass

    fake = cast(MemoryServer, Server())

    def call(n: int) -> dict[str, Any]:
        params = {"name": "nmemory_context_show", "arguments": {"n": n}}
        return {"id": n, "method": "tools/call", "params": params}

    async def run() -> tuple[list[int], bool]:
        answered: list[int] = []
        with anyio.CancelScope() as scope:
            release = _ExitOnNewRelease(_Sink(), fake, _spawn_target(str(link)), scope)
            sdk = build(fake, release=release)
            send, inbox = anyio.create_memory_object_stream[Any](8)
            outbox, receive = anyio.create_memory_object_stream[Any](8)

            async def push(frame: dict[str, Any]) -> None:
                message = types.jsonrpc_message_adapter.validate_python(complete(frame))
                await send.send(SessionMessage(message))

            async def answer() -> None:
                got = await receive.receive()
                answered.append(got.message.id)
                line = got.message.model_dump_json(by_alias=True, exclude_unset=True)
                await release.write(line + "\n")
                await release.flush()  # what the stdio writer does after each answer

            async with anyio.create_task_group() as tg:
                tg.start_soon(
                    sdk.run, inbox, outbox, sdk.create_initialization_options()
                )
                try:
                    info = {"clientInfo": {"name": "claude-code", "version": "2"}}
                    await push({"id": 0, "method": "initialize", "params": info})
                    await answer()
                    await push(INITIALIZED)
                    await push(call(1))
                    await anyio.to_thread.run_sync(swapped.wait, 10)
                    await push(call(2))
                    await anyio.sleep(0.2)  # call 2 meets the swap while 1 runs
                    # Another answer flushed meanwhile does not end the session.
                    await push({"id": 9, "method": "tools/list"})
                    await answer()
                    assert not scope.cancel_called, "answer 1 is still owed"
                    go.set()
                    await anyio.sleep(
                        0.2
                    )  # call 1 has returned; its answer is unwritten
                    await push(call(3))
                    await anyio.sleep(0.2)
                    assert not scope.cancel_called, "answer 1 is still unwritten"
                    await answer()
                    await anyio.sleep(10)
                finally:
                    # As the stdio transport does when its scope is cancelled.
                    send.close()
                    receive.close()
        return answered, scope.cancel_called

    answered, ended = anyio.run(run)
    time.sleep(0.3)  # a call wrongly admitted would run on the worker by now
    assert answered == [0, 9, 1] and ended
    assert ran == [1], "the call after the swap never ran"


def test_a_cancelled_call_does_not_hold_the_release_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The client cancels a running call, which then gets no answer: once its
    worker settles it is no longer owed, and the swap still ends the session."""
    _old, new, link = _layout(tmp_path)
    monkeypatch.setattr(sys, "argv", [str(link)])
    go = threading.Event()

    class Server:
        client_name = None
        client_harness = "unknown"
        read_only = True
        inherited = False

        def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            assert go.wait(10)
            return {"result": {"content": [], "isError": False}}

        def flush_read_traffic(self) -> None:
            pass

    fake = cast(MemoryServer, Server())

    async def run() -> bool:
        with anyio.CancelScope() as scope:
            release = _ExitOnNewRelease(_Sink(), fake, _spawn_target(str(link)), scope)
            sdk = build(fake, release=release)
            send, inbox = anyio.create_memory_object_stream[Any](8)
            outbox, receive = anyio.create_memory_object_stream[Any](8)

            async def push(frame: dict[str, Any]) -> None:
                message = types.jsonrpc_message_adapter.validate_python(complete(frame))
                await send.send(SessionMessage(message))

            async with anyio.create_task_group() as tg:
                tg.start_soon(
                    sdk.run, inbox, outbox, sdk.create_initialization_options()
                )
                try:
                    info = {"clientInfo": {"name": "claude-code", "version": "2"}}
                    await push({"id": 0, "method": "initialize", "params": info})
                    await receive.receive()
                    await push(INITIALIZED)
                    params = {"name": "nmemory_context_show", "arguments": {}}
                    await push({"id": 1, "method": "tools/call", "params": params})
                    await anyio.sleep(0.2)
                    cancel = {"requestId": 1}
                    await push({"method": "notifications/cancelled", "params": cancel})
                    await anyio.sleep(0.2)
                    _repoint(link, new / "bin" / "nmemory")()
                    go.set()  # the cancelled call's worker settles
                    await anyio.sleep(10)
                finally:
                    send.close()
                    receive.close()
        return scope.cancel_called

    assert anyio.run(run), "the session ended on the new release"


class _Sink:
    async def write(self, text: str) -> None:
        pass

    async def flush(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _no_stray_store() -> None:
    assert os.environ.get("NOTT_DATABASE_DIR"), "the plugin gives each test a store"
