"""Shared MCP stdio transport: real peers, one handshake and typed faults."""

# Capsule IDs in this file are synthetic protocol samples.

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nott_socket import mcp
from nott_socket.mcp import (
    REPLY_CEILING,
    McpClient,
    McpClientError,
    McpRefused,
    StdioServer,
    configured_server,
)

SCRIPT = """import json, sys
mode = sys.argv[1]
for line in sys.stdin:
    req = json.loads(line)
    if "id" not in req:
        continue
    if mode == "invalid-frame":
        print("{", flush=True)
        continue
    if mode == "eof":
        break
    if mode == "rpc-error":
        print(json.dumps({"jsonrpc": "2.0", "id": req["id"], "error": {"code": -32602, "message": "no such run"}}), flush=True)
        continue
    if mode == "store-busy" and req["method"] == "tools/call":
        error = {"code": -32002, "message": "store unavailable: database is locked", "data": {"kind": "store-busy"}}
        print(json.dumps({"jsonrpc": "2.0", "id": req["id"], "error": error}), flush=True)
        continue
    if mode == "walled" and req["method"] == "tools/call":
        error = {"code": -32602, "message": "every eligible Claude route is cooling down", "data": {"tool": "nspawn_run_start", "type": "AllProvidersWalled", "nearest_reset": 1790000000}}
        print(json.dumps({"jsonrpc": "2.0", "id": req["id"], "error": error}), flush=True)
        continue
    if req["method"] == "initialize":
        result = {"serverInfo": {"name": "wrong" if mode == "wrong-server" else "nspawn"}}
    else:
        if mode == "silent":
            import time
            time.sleep(60)
            continue
        if mode == "nested-frame" and req["params"]["arguments"].get("nested"):
            print('{"jsonrpc": "2.0", "id": %d, "result": %s}' % (req["id"], "[" * 100000 + "]" * 100000), flush=True)
            continue
        if mode == "nested-text" and req["params"]["arguments"].get("nested"):
            text = "[" * 100000 + "]" * 100000
            print(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": {"content": [{"type": "text", "text": text}]}}), flush=True)
            continue
        if mode == "slow-first" and req["params"]["arguments"].get("slow"):
            import time
            time.sleep(3)
        if mode == "oversized":
            print("x" * (1 << 23), flush=True)
            break
        args = req["params"]["arguments"]
        payload = {"echo": args, "tool": req["params"]["name"]}
        if mode == "wire-size": payload["wire_bytes"] = len(line.encode("utf-8"))
        text = json.dumps(payload)
        result = {"content": [{"type": "text", "text": "refused by roster" if mode == "tool-error" else text}], "isError": mode == "tool-error"}
        if mode == "missing-content": result = {}
        if mode == "tool-busy":
            result = {"content": [{"type": "text", "text": "database is locked"}], "isError": True, "structuredContent": {"kind": "store-busy"}}
        if mode == "empty-content": result["content"] = []
        if mode == "multiple-content": result["content"] *= 2
        if mode == "nonobject-content": result["content"] = ["text"]
        if mode == "missing-text": result["content"] = [{}]
        if mode == "invalid-tool-json": result["content"][0]["text"] = "{"
        if mode == "nonobject-tool-json": result["content"][0]["text"] = "[]"
    print(json.dumps({"jsonrpc": "2.0", "method": "notifications/progress"}), flush=True)
    print(json.dumps({"jsonrpc": "2.0", "id": 999 if mode == "wrong-id" else req["id"], "result": result}), flush=True)
"""


def server(tmp_path: Path, mode: str = "ok") -> StdioServer:
    script = tmp_path / "server.py"
    script.write_text(SCRIPT)
    return StdioServer(
        sys.executable, (str(script), mode), {"NOTT_CLIENT_FIXTURE": "1"}
    )


def test_call_decodes_the_single_json_content(tmp_path: Path) -> None:
    with McpClient(server(tmp_path), tmp_path, deadline_s=5) as client:
        first = client.call("nspawn_run_show", {"run_id": "r1"})
        second = client.call("nspawn_result_show", {"run_id": "r2"})
    assert first == {"echo": {"run_id": "r1"}, "tool": "nspawn_run_show"}
    assert second == {"echo": {"run_id": "r2"}, "tool": "nspawn_result_show"}


def test_unicode_request_uses_utf8_bytes_without_changing_the_params(
    tmp_path: Path,
) -> None:
    arguments = {"prompt": "é" * 100_000}
    params = {"name": "nspawn_run_start", "arguments": arguments}
    request = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": params}
    expected_bytes = len(json.dumps(request, ensure_ascii=False).encode("utf-8")) + 1
    with McpClient(server(tmp_path, "wire-size"), tmp_path, deadline_s=5) as client:
        first = client.call("nspawn_run_start", arguments)
        second = client.call("nspawn_run_show", {"run_id": "r2"})
    assert first["wire_bytes"] == expected_bytes
    assert first["echo"] == arguments
    assert first["tool"] == "nspawn_run_start"
    assert second["echo"] == {"run_id": "r2"}
    assert second["tool"] == "nspawn_run_show"


@pytest.mark.parametrize(
    "mode,match",
    [
        ("wrong-server", "not nspawn"),
        ("eof", "truncated"),
        ("rpc-error", "no such run"),
        ("wrong-id", "another request"),
        ("tool-error", "refused by roster"),
    ],
)
def test_every_fault_is_one_typed_error(tmp_path: Path, mode: str, match: str) -> None:
    with (
        pytest.raises(McpClientError, match=match),
        McpClient(server(tmp_path, mode), tmp_path, deadline_s=5) as client,
    ):
        client.call("nspawn_run_show", {"run_id": "r1"})


def test_a_late_answer_to_a_timed_out_request_leaves_the_session_in_step(
    tmp_path: Path,
) -> None:
    """A request whose deadline passed is abandoned, and its answer can still arrive:
    the peer was slow, not gone. That answer belongs to nobody. Read as the answer to
    the next request, it left every later request one answer behind for good — each
    one refused as "answered another request" until the process was restarted."""
    with McpClient(server(tmp_path, "slow-first"), tmp_path, deadline_s=2) as client:
        with pytest.raises(McpClientError, match="deadline exceeded"):
            client.call("nmemory_capsule_list", {"slow": True})
        # Distinct synthetic IDs expose a reply from an earlier request.
        second = client.call("nmemory_capsule_show", {"id": "synthetic-capsule-b"})
        third = client.call("nmemory_capsule_show", {"id": "synthetic-capsule-c"})
    assert second == {
        "echo": {"id": "synthetic-capsule-b"},
        "tool": "nmemory_capsule_show",
    }
    assert third == {
        "echo": {"id": "synthetic-capsule-c"},
        "tool": "nmemory_capsule_show",
    }


@pytest.mark.parametrize("mode", ["nested-frame", "nested-text"])
def test_a_reply_nested_past_the_parser_is_one_typed_error(
    tmp_path: Path, mode: str
) -> None:
    """A reply nested deeper than `json.loads` can recurse raises `RecursionError`,
    which is no `ValueError`: it escaped every caller's `McpClientError` catch, and
    through the ACP read thread it ended `nott acp serve`. It is an undecodable reply
    like any other, and the session goes on answering the next request."""
    with McpClient(server(tmp_path, mode), tmp_path, deadline_s=5) as client:
        with pytest.raises(McpClientError, match="not JSON"):
            client.call("nmemory_capsule_list", {"nested": True})
        after = client.call("nmemory_capsule_show", {"id": "synthetic-capsule-b"})
    assert after == {
        "echo": {"id": "synthetic-capsule-b"},
        "tool": "nmemory_capsule_show",
    }


def test_a_refusal_the_server_answered_carries_its_message_and_data(
    tmp_path: Path,
) -> None:
    """Told apart from a transport fault: nSPAWN answered, and did nothing."""
    with (
        pytest.raises(McpRefused) as refused,
        McpClient(server(tmp_path, "walled"), tmp_path, deadline_s=5) as client,
    ):
        client.call("nspawn_run_start", {"prompt": "p"})
    assert refused.value.message == "every eligible Claude route is cooling down"
    assert refused.value.data["type"] == "AllProvidersWalled"
    assert refused.value.data["nearest_reset"] == 1790000000
    assert str(refused.value) == (
        "tools/call refused: every eligible Claude route is cooling down"
    )


@pytest.mark.parametrize(
    "mode,message,kind",
    [
        ("store-busy", "store unavailable: database is locked", "store-busy"),
        ("tool-busy", "database is locked", "store-busy"),
        ("tool-error", "refused by roster", None),
        ("rpc-error", "no such run", None),
    ],
)
def test_refusal_exposes_kind_from_either_channel(
    tmp_path: Path, mode: str, message: str, kind: str | None
) -> None:
    with (
        pytest.raises(McpRefused) as refused,
        McpClient(server(tmp_path, mode), tmp_path, deadline_s=5) as client,
    ):
        client.call("nmemory_context_show", {"query": "busy"})
    assert refused.value.message == message
    assert refused.value.kind == kind


@pytest.mark.parametrize("marker", [None, [], {"kind": 1}, {"kind": None}, {}])
def test_unrecognized_tool_marker_keeps_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, marker: object
) -> None:
    client = McpClient(StdioServer(sys.executable), tmp_path)
    monkeypatch.setattr(
        client,
        "_request",
        lambda *_: {
            "content": [{"type": "text", "text": "database is locked"}],
            "isError": True,
            "structuredContent": marker,
        },
    )
    with pytest.raises(McpRefused) as refused:
        client.call("nmemory_context_show", {})
    assert refused.value.kind is None
    assert refused.value.message == "database is locked"


@pytest.mark.parametrize(
    "mode,refused",
    [("rpc-error", True), ("tool-error", True), ("eof", False), ("wrong-id", False)],
)
def test_only_an_answered_refusal_is_a_refusal(
    tmp_path: Path, mode: str, refused: bool
) -> None:
    with (
        pytest.raises(McpClientError) as caught,
        McpClient(server(tmp_path, mode), tmp_path, deadline_s=5) as client,
    ):
        client.call("nspawn_run_show", {"run_id": "r1"})
    assert isinstance(caught.value, McpRefused) is refused
    if mode == "eof":
        assert type(caught.value) is McpClientError


def test_a_missing_command_is_a_typed_error(tmp_path: Path) -> None:
    with (
        pytest.raises(McpClientError, match="spawned"),
        McpClient(StdioServer(str(tmp_path / "absent")), tmp_path),
    ):
        pass


def test_configured_server_reads_the_body_configuration(tmp_path: Path) -> None:
    (tmp_path / "plugin").mkdir()
    path = tmp_path / "plugin/.mcp.json"
    with pytest.raises(McpClientError, match="unreadable"):
        configured_server(tmp_path)
    path.write_text("{")
    with pytest.raises(McpClientError, match="not JSON"):
        configured_server(tmp_path)
    path.write_text(json.dumps({"mcpServers": {"nspawn": {"command": "python"}}}))
    with pytest.raises(McpClientError, match="absolute stdio"):
        configured_server(tmp_path)
    path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "nspawn": {
                        "type": "stdio",
                        "command": "/usr/bin/env",
                        "args": ["python3"],
                        "env": {"X": "${NOTT_UNSET_ZZ9}"},
                    }
                }
            }
        )
    )
    with pytest.raises(McpClientError, match="unresolved"):
        configured_server(tmp_path)
    path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "nspawn": {
                        "command": "/usr/bin/env",
                        "args": ["a"],
                        "env": {"X": "1"},
                    }
                }
            }
        )
    )
    assert configured_server(tmp_path) == StdioServer(
        "/usr/bin/env", ("a",), {"X": "1"}
    )


def test_memory_peer_and_wrong_identity(tmp_path: Path) -> None:
    script = tmp_path / "peer.py"
    script.write_text("""import json,sys
for line in sys.stdin:
    request=json.loads(line)
    if "id" not in request: continue
    result={"serverInfo":{"name":"nmemory"}} if request["method"]=="initialize" else {"content":[{"type":"text","text":json.dumps({"id":"cap-1"})}]}
    print(json.dumps({"jsonrpc":"2.0","id":request["id"],"result":result}),flush=True)
""")
    server = StdioServer(sys.executable, (str(script),))
    with McpClient(server, tmp_path, server_name="nmemory") as client:
        assert client.call("nmemory_capsule_show", {"id": "cap-1"}) == {"id": "cap-1"}
    with pytest.raises(McpClientError, match="not nspawn"), McpClient(server, tmp_path):
        pass


@pytest.mark.parametrize(
    "mode,match",
    [
        ("invalid-frame", "frame is not JSON"),
        ("missing-content", "incomplete content"),
        ("empty-content", "incomplete content"),
        ("multiple-content", "incomplete content"),
        ("nonobject-content", "tool content must be a JSON object"),
        ("missing-text", "tool JSON must be a non-empty string"),
        ("invalid-tool-json", "answer is not JSON"),
        ("nonobject-tool-json", "answer must be a JSON object"),
        ("oversized", "truncated or oversized"),
    ],
)
def test_malformed_wire_replies_are_refused(
    tmp_path: Path, mode: str, match: str
) -> None:
    with (
        pytest.raises(McpClientError, match=match),
        McpClient(server(tmp_path, mode), tmp_path, deadline_s=5) as client,
    ):
        client.call("fixture_read", {})


def test_close_reaps_a_peer_left_unresponsive_by_a_timed_out_call(
    tmp_path: Path,
) -> None:
    """The hurried call raises at its own deadline, well inside the handshake's, and
    the peer is still alive after it. close() then ends the silent peer, within its
    five-second wait and what follows it, and closes both pipes. Only the call is
    hurried: a short deadline given to the handshake too timed out the peer's start
    on a loaded host instead of the call."""
    client = McpClient(server(tmp_path, "silent"), tmp_path, deadline_s=5)
    with client:
        process = client._process
        assert process is not None
        hurried(client, 0.2)
        started = time.monotonic()
        with pytest.raises(McpClientError, match="deadline exceeded"):
            client.call("fixture_read", {})
        assert time.monotonic() - started < 2
        # A killed peer that nobody has reaped yet still polls None; waiting tells.
        with pytest.raises(subprocess.TimeoutExpired):
            process.wait(timeout=0.5)
        closing = time.monotonic()
    assert time.monotonic() - closing < 7
    assert process.poll() is not None
    assert process.stdin is not None and process.stdin.closed
    assert process.stdout is not None and process.stdout.closed
    client.close()


def test_a_dead_peer_has_a_typed_write_fault_and_is_reaped(tmp_path: Path) -> None:
    with McpClient(server(tmp_path), tmp_path) as client:
        process = client._process
        assert process is not None
        process.kill()
        process.wait(timeout=5)
        with pytest.raises(McpClientError, match="stdin closed"):
            client.call("fixture_read", {})


def test_closed_client_rejects_calls_and_frames(tmp_path: Path) -> None:
    client = McpClient(server(tmp_path), tmp_path)
    client.close()
    with pytest.raises(McpClientError, match="client is closed"):
        client.call("fixture_read", {})
    with pytest.raises(McpClientError, match="client is closed"):
        client._write(b"{}\n", None)


@pytest.mark.parametrize("cwd_override", [False, True])
def test_configured_open_uses_the_selected_working_directory(
    tmp_path: Path, cwd_override: bool
) -> None:
    selected = tmp_path / "selected"
    selected.mkdir()
    script = tmp_path / "cwd.py"
    script.write_text("""import json, os, sys
for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request: continue
    result = {"serverInfo": {"name": "nspawn"}} if request["method"] == "initialize" else {"content": [{"type": "text", "text": json.dumps({"cwd": os.getcwd()})}]}
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
""")
    (tmp_path / "plugin").mkdir()
    (tmp_path / "plugin/.mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "nspawn": {"command": sys.executable, "args": [str(script)]}
                }
            }
        )
    )
    with McpClient.open(tmp_path, selected if cwd_override else None) as client:
        assert client.call("fixture_read", {}) == {
            "cwd": str(selected if cwd_override else tmp_path)
        }


@pytest.mark.parametrize("stream", ["stdin", "stdout"])
def test_a_call_on_a_closed_client_is_one_typed_error(
    tmp_path: Path, stream: str
) -> None:
    """A conversation turn still in flight at shutdown calls nMEMORY through a client
    whose close is running: close drops the process and then closes its pipes, and a
    call that had already taken the process wrote to a closed pipe and raised a raw
    `ValueError`, which every caller's `McpClientError` catch missed. That state — the
    process still held, one of its pipes closed — is set up here directly, on write
    and on read; and a call after close has finished is refused the same way."""
    client = McpClient(server(tmp_path), tmp_path, deadline_s=5)
    with client:
        process = client._process
        assert process is not None
        getattr(process, stream).close()
        with pytest.raises(McpClientError, match="MCP client is closed"):
            client.call("nmemory_capsule_show", {"id": "cap-1"})
    with pytest.raises(McpClientError, match="MCP client is closed"):
        client.call("nmemory_capsule_show", {"id": "cap-1"})


#: A peer driven by the call's own arguments: `hold` keeps the answer back, `before`
#: is raw lines written ahead of it, `big` one line of that many bytes, and every held
#: answer goes out ahead of the next answer — reversed when the call asks for
#: `reverse`. A call that asks to `report` is also told which ids were cancelled.
#: `notes` is notification lines of those sizes, written in one write with the answer;
#: `split` writes the `big` line and the answer's first 20 bytes, and holds the rest.
#: After a call that asks for `deaf`, stdin goes unread that long.
SCRIPTED = """import json, sys, time
held = []
cancelled = []
honour_cancel = False
def note(size):
    head = '{"jsonrpc": "2.0", "method": "notifications/message", "params": "'
    return head + "x" * (size - len(head) - 2) + '"}'
for line in sys.stdin:
    req = json.loads(line)
    if req.get("method") == "notifications/cancelled":
        cancelled.append(req["params"]["requestId"])
        if honour_cancel:
            held = [raw for raw in held if json.loads(raw)["id"] != req["params"]["requestId"]]
    if "id" not in req:
        continue
    if req["method"] == "initialize":
        print(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": {"serverInfo": {"name": "nspawn"}}}), flush=True)
        continue
    args = req["params"]["arguments"]
    honour_cancel = args.get("honour_cancel", False)
    text = json.dumps({"echo": args, **({"cancelled": cancelled} if args.get("report") else {})})
    if args.get("reply_size"):
        text = json.dumps({"pad": args.get("reply_char", "x") * args["reply_size"]}, ensure_ascii=False)
    answer = json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": {"content": [{"type": "text", "text": text}]}}, ensure_ascii=False)
    if args.get("reply_head"):
        answer = args["reply_head"] + '"result":"' + "x" * args.get("reply_payload_size", 1 << 22) + '"' + args.get("reply_tail", "") + '}'
    if args.get("hold"):
        held.append(answer)
        continue
    if args.get("notes"):
        notes = [note(size) for size in args["notes"]]
        sys.stdout.write("\\n".join([*notes, answer]) + "\\n")
        sys.stdout.flush()
        continue
    if args.get("split"):
        sys.stdout.write("x" * args["big"] + "\\n" + answer[:20])
        sys.stdout.flush()
        held.append(answer[20:])
        continue
    for raw in args.get("before", []):
        print(raw, flush=True)
    if args.get("big"):
        print("x" * args["big"], flush=True)
    for raw in reversed(held) if args.get("reverse") else held:
        print(raw, flush=True)
    held.clear()
    print(answer, flush=True)
    time.sleep(args.get("deaf", 0))
"""


def scripted(tmp_path: Path) -> McpClient:
    script = tmp_path / "scripted.py"
    script.write_text(SCRIPTED)
    return McpClient(StdioServer(sys.executable, (str(script),)), tmp_path, 5)


def hurried(client: McpClient, deadline_s: float) -> McpClient:
    """`client` with a short deadline from here on: the handshake had the long one, so
    a loaded host times out the calls a case holds back and never its start."""
    client._deadline_s = deadline_s
    return client


def test_oversized_batch_is_typed_and_a_single_read_still_works(tmp_path: Path) -> None:
    with scripted(tmp_path) as client:
        with pytest.raises(
            McpClientError, match="^MCP reply truncated or oversized$"
        ) as caught:
            client.call(
                "nmemory_capsule_show",
                {"ids": ["cap-1", "synthetic-capsule-b"], "big": 3 * REPLY_CEILING},
            )
        assert type(caught.value).__name__ == "McpOversized"
        assert client.call("nmemory_capsule_show", {"id": "cap-1"}) == {
            "echo": {"id": "cap-1"}
        }


def test_batch_deadline_is_typed_and_a_single_read_still_works(tmp_path: Path) -> None:
    with scripted(tmp_path) as client:
        hurried(client, 0.2)
        with pytest.raises(
            McpClientError, match="^tools/call deadline exceeded$"
        ) as caught:
            client.call(
                "nmemory_capsule_show",
                {"ids": ["cap-1", "synthetic-capsule-b"], "hold": True},
            )
        assert type(caught.value).__name__ == "McpDeadline"
        hurried(client, 5)
        assert client.call("nmemory_capsule_show", {"id": "cap-1", "report": True}) == {
            "echo": {"id": "cap-1", "report": True},
            "cancelled": [2],
        }


@pytest.mark.parametrize("reply_char", ["x", "🐈"])
def test_late_oversized_batch_does_not_fail_the_next_request(
    tmp_path: Path, reply_char: str
) -> None:
    with scripted(tmp_path) as client:
        hurried(client, 0.2)
        with pytest.raises(mcp.McpDeadline):
            client.call(
                "nmemory_capsule_show",
                {
                    "ids": ["cap-1"],
                    "hold": True,
                    "reply_size": 3 * REPLY_CEILING,
                    "reply_char": reply_char,
                },
            )
        hurried(client, 5)
        assert client.call("nmemory_capsule_show", {"id": "cap-1"}) == {
            "echo": {"id": "cap-1"}
        }


def test_honoured_cancel_does_not_hide_the_current_oversized_reply(
    tmp_path: Path,
) -> None:
    with scripted(tmp_path) as client:
        hurried(client, 0.2)
        with pytest.raises(mcp.McpDeadline):
            client.call("held", {"hold": True, "honour_cancel": True})
        hurried(client, 5)
        with pytest.raises(mcp.McpOversized):
            client.call("oversized", {"reply_size": 3 * REPLY_CEILING})
        assert client.call("after", {}) == {"echo": {}}


def test_an_oversized_reply_with_many_envelope_members_is_refused_promptly(
    tmp_path: Path,
) -> None:
    members = ",".join(f'"member{number}":0' for number in range(4000))
    head = '{"jsonrpc":"2.0",' + members + ',"id":2,'
    with scripted(tmp_path) as client:
        deadline_s = 10.0
        hurried(client, deadline_s)
        started = time.monotonic()
        with pytest.raises(mcp.McpOversized):
            client.call("oversized", {"reply_head": head})
        elapsed = time.monotonic() - started
        assert elapsed < deadline_s, elapsed


@pytest.mark.parametrize(
    "suffix",
    [
        '"id":3,',
        '"id":2,',
        '"\\u0069d":3,',
        '"result":{},"id":3,',
        ",".join(f'"member{number}":0' for number in range(4000)) + ',"id":3,',
        '"padding":"' + "x" * 4096 + '",',
    ],
    ids=[
        "current-id",
        "same-id",
        "escaped-id",
        "id-after-payload",
        "id-past-head",
        "unfinished-head",
    ],
)
def test_an_ambiguous_oversized_envelope_is_refused_for_the_current_request(
    tmp_path: Path, suffix: str
) -> None:
    with scripted(tmp_path) as client:
        hurried(client, 0.2)
        with pytest.raises(mcp.McpDeadline):
            client.call("held", {"hold": True})
        hurried(client, 10)
        head = '{"jsonrpc":"2.0","id":2,' + suffix
        with pytest.raises(mcp.McpOversized):
            client.call("oversized", {"reply_head": head})
        hurried(client, 5)
        assert client.call("after", {}) == {"echo": {}}


@pytest.mark.parametrize("tail", [',"id":23', ',"id":7', ',"\\u0069d":23'])
def test_duplicate_id_after_oversized_payload_is_ambiguous(
    tmp_path: Path, tail: str
) -> None:
    with scripted(tmp_path) as client:
        client._next_id = 23
        deadline_s = 10.0
        hurried(client, deadline_s)
        started = time.monotonic()
        with pytest.raises(mcp.McpOversized):
            client.call(
                "oversized",
                {
                    "reply_head": '{"jsonrpc":"2.0","id":2,',
                    "reply_payload_size": 3 * REPLY_CEILING,
                    "reply_tail": tail,
                },
            )
        assert time.monotonic() - started < deadline_s
        assert client.call("after", {}) == {"echo": {}}


def test_unique_id_after_draining_oversized_late_reply_is_discarded(
    tmp_path: Path,
) -> None:
    with scripted(tmp_path) as client:
        hurried(client, 0.2)
        with pytest.raises(mcp.McpDeadline):
            client.call(
                "held",
                {
                    "hold": True,
                    "reply_head": '{"jsonrpc":"2.0","id":2,',
                    "reply_payload_size": 3 * REPLY_CEILING,
                    "reply_tail": ',"extra":{"id":23,"text":"escaped \\" id"}',
                },
            )
        hurried(client, 10)
        assert client.call("after", {}) == {"echo": {}}


@pytest.mark.parametrize("width", [1, 2, 7, 4096])
@pytest.mark.parametrize("duplicate", [False, True])
def test_oversized_envelope_scan_keeps_structure_across_chunks(
    width: int, duplicate: bool
) -> None:
    payload = {"id": 23, "nested": [{"id": 2}], "text": '\\"},"id":23,'}
    raw = (
        b'{"id":2,"result":'
        + json.dumps(payload).encode()
        + b',"long key beyond the bounded id key":"value"'
        + (b',"\\u0069\\u0064":23}' if duplicate else b"}")
    )
    envelope = mcp._EnvelopeIds()
    for start in range(0, len(raw), width):
        envelope.feed(raw[start : start + width])
        assert envelope.key is None or len(envelope.key) <= 14
    assert envelope.ids == (2 if duplicate else 1)


@pytest.mark.parametrize(
    "stray",
    [
        "not json at all",
        json.dumps({"jsonrpc": "2.0", "id": True, "result": {}}),
        json.dumps(
            {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "x"}}
        ),
    ],
    ids=["non-json", "id-true", "id-null"],
)
def test_a_call_that_ends_without_its_answer_leaves_the_session_in_step(
    tmp_path: Path, stray: str
) -> None:
    """A deadline is not the only way a call can stop reading before its own answer
    came: a stray line or a frame with an id that is no request's ends it too, and
    is refused. Its answer still arrives, and it is nobody's: the next call gets its
    own answer, never that one."""
    with scripted(tmp_path) as client:
        with pytest.raises(McpClientError):
            client.call("first", {"before": [stray]})
        second = client.call("second", {"n": 2})
        third = client.call("third", {"n": 3})
    assert second == {"echo": {"n": 2}}
    assert third == {"echo": {"n": 3}}


def test_the_peers_own_requests_and_blank_lines_are_read_past(tmp_path: Path) -> None:
    """A frame that carries a `method` is the peer's own request or a notification,
    whatever its id — one with an id above every request's included — and a blank
    line between frames is no frame: none of them stops a call short of its answer."""
    server_request = json.dumps({"jsonrpc": "2.0", "id": 1000, "method": "ping"})
    with scripted(tmp_path) as client:
        first = client.call("first", {"before": [server_request, "", "   "]})
    assert first == {"echo": {"before": [server_request, "", "   "]}}


def test_late_answers_to_two_timed_out_calls_are_dropped_in_any_order(
    tmp_path: Path,
) -> None:
    """Two calls time out and their answers come back newest first: both are read
    past, not only the first frame after a timeout. Each timed-out id was cancelled
    to the peer by name, as MCP has a sender do."""
    with scripted(tmp_path) as client:
        hurried(client, 0.5)
        for _ in range(2):
            with pytest.raises(McpClientError, match="deadline exceeded"):
                client.call("held", {"hold": True})
        arguments = {"reverse": True, "report": True}
        assert client.call("after", arguments) == {
            "echo": arguments,
            # The handshake is request 1, so the two held calls are 2 and 3.
            "cancelled": [2, 3],
        }


def test_an_older_id_is_read_past_and_a_newer_one_refused(tmp_path: Path) -> None:
    """Requests go one at a time and ids only grow, so a frame naming an id below the
    current request's can only be a late answer, whoever asked: it is read past. One
    naming an id above it answers no request this client has sent, and is refused."""
    with scripted(tmp_path) as client:
        assert client.call("first", {"n": 1}) == {"echo": {"n": 1}}
        hurried(client, 0.5)
        with pytest.raises(McpClientError, match="deadline exceeded"):
            client.call("held", {"hold": True})
        older = json.dumps({"jsonrpc": "2.0", "id": 2, "result": {}})
        assert client.call("after", {"before": [older]}) == {
            "echo": {"before": [older]}
        }
        newer = json.dumps({"jsonrpc": "2.0", "id": 99, "result": {}})
        with pytest.raises(McpClientError, match="answered another request"):
            client.call("ahead", {"before": [newer]})


def test_many_timed_out_calls_then_their_answers_leave_the_session_in_step(
    tmp_path: Path,
) -> None:
    """However many calls timed out, their answers arriving together are all read
    past: a window of remembered ids once refused the oldest of them, abandoned the
    call that met it, and pushed out one more id each time, so every later call
    failed for good."""
    with scripted(tmp_path) as client:
        hurried(client, 0.05)
        for _ in range(66):
            with pytest.raises(McpClientError, match="deadline exceeded"):
                client.call("held", {"hold": True})
        hurried(client, 5)
        assert client.call("flush", {"n": 0}) == {"echo": {"n": 0}}
        for number in range(1, 6):
            assert client.call("later", {"n": number}) == {"echo": {"n": number}}


def test_an_oversized_reply_line_is_discarded_and_the_session_goes_on(
    tmp_path: Path,
) -> None:
    """A line past the reply ceiling is refused, and the rest of it is read and thrown
    away up to its newline as it arrives: left in the buffer, it made every later call
    on the client "truncated or oversized" too. The line is three times the ceiling,
    so most of it arrives after the refusal."""
    with scripted(tmp_path) as client:
        with pytest.raises(McpClientError, match="oversized"):
            client.call("first", {"big": 3 * REPLY_CEILING})
        assert client.call("second", {"n": 2}) == {"echo": {"n": 2}}
        assert client.call("third", {"n": 3}) == {"echo": {"n": 3}}


def test_a_line_under_the_ceiling_is_measured_alone_not_with_what_follows_it(
    tmp_path: Path,
) -> None:
    """The ceiling is per line: a notification just under it, read in the same chunk
    as the answer after it, is no oversized reply. Counting the whole chunk refused
    the answer for the bytes of the frames behind its own line."""
    sizes = [1000, REPLY_CEILING - 5]
    with scripted(tmp_path) as client:
        assert client.call("first", {"notes": sizes}) == {"echo": {"notes": sizes}}


def test_the_frame_after_an_oversized_line_in_the_same_chunk_is_kept(
    tmp_path: Path,
) -> None:
    """An oversized line that ends inside the chunk being read leaves the bytes after
    its newline in the buffer: they begin the next frame. That frame is the refused
    call's own late answer, whose rest the peer writes only once the next request
    comes; dropped, its head left that rest unreadable as a frame."""
    with scripted(tmp_path) as client:
        with pytest.raises(McpClientError, match="oversized"):
            client.call("first", {"big": REPLY_CEILING + 10, "split": True})
        assert client.call("second", {"n": 2}) == {"echo": {"n": 2}}


@pytest.mark.skipif(
    not hasattr(fcntl, "F_GETPIPE_SZ"), reason="a pipe's capacity is read on Linux"
)
def test_a_cancellation_the_peer_cannot_take_waits_no_longer_than_the_call(
    tmp_path: Path,
) -> None:
    """A call past its deadline cancels its request to the peer, and that write gets
    no time of its own: a peer that stopped reading, behind a request that filled its
    stdin pipe, held the timed-out call until it read again, and one that never does
    held the client for good. The cancellation is skipped, the call fails on time, and
    the session goes on once the peer reads again."""
    with scripted(tmp_path) as client:
        assert client.call("first", {"deaf": 1}) == {"echo": {"deaf": 1}}
        # The handshake is request 1 and `first` request 2: this frame, request 3,
        # fills the peer's stdin pipe exactly. The capacity is read, not assumed: an
        # account past its pipe-page allowance gets pipes of two pages.
        assert client._process is not None and client._process.stdin is not None
        capacity = fcntl.fcntl(client._process.stdin, fcntl.F_GETPIPE_SZ)
        frame = {"jsonrpc": "2.0", "id": 3, "method": "tools/call"}
        bare = json.dumps({**frame, "params": {"name": "full", "arguments": {}}})
        pad = "x" * (capacity - len(bare) - len(', "pad": ""') - 1)
        hurried(client, 0.02)
        started = time.monotonic()
        with pytest.raises(McpClientError, match="deadline exceeded"):
            client.call("full", {"pad": pad})
        assert time.monotonic() - started < 0.5
        hurried(client, 5)
        assert client.call("after", {"n": 4}) == {"echo": {"n": 4}}


def test_no_frame_is_taken_once_the_deadline_has_passed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once the deadline has passed, a complete frame already buffered is not taken:
    the call fails without decoding it, as no read happens to check the deadline
    between buffered frames. Three notifications and the answer go in one write under
    `PIPE_BUF`, so one read takes them all; the clock moves past the deadline while the
    first is decoded, so the other three are buffered and expired. The session goes on
    afterwards."""
    skew = [0.0]
    taken: list[str] = []
    decoded = mcp._decoded

    def counted(raw: str | bytes | bytearray, label: str) -> dict[str, Any]:
        taken.append(label)
        if len(taken) == 1:
            skew[0] += 60
        return decoded(raw, label)

    with scripted(tmp_path) as client:
        clock = SimpleNamespace(monotonic=lambda: time.monotonic() + skew[0])
        monkeypatch.setattr(mcp, "time", clock)
        monkeypatch.setattr(mcp, "_decoded", counted)
        with pytest.raises(mcp.McpDeadline, match="deadline exceeded"):
            client.call("first", {"notes": [100, 100, 100]})
        assert taken == ["MCP frame"]
        assert client.call("after", {"n": 2}) == {"echo": {"n": 2}}


@pytest.mark.parametrize("slow_frame", ["notification", "reply"])
def test_a_frame_taken_after_the_deadline_is_refused_one_taken_before_is_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, slow_frame: str
) -> None:
    """The deadline bounds the wait for the peer, checked before each frame is taken,
    not the client's own decode. A large notification read in with the reply behind it
    took the call past its deadline as it was decoded, and the reply after it was
    accepted anyway: it is refused. A reply taken in time is returned, however long
    its own decode. The decode is made slower than the deadline by moving the
    client's clock, not by the host's speed."""
    skew = [0.0]
    decoded = mcp._decoded

    def slow(raw: str | bytes | bytearray, label: str) -> dict[str, Any]:
        reply = isinstance(raw, bytearray) and b'"result"' in raw
        if (len(raw) > 1_000_000, reply)[slow_frame == "reply"]:
            skew[0] += 60
        return decoded(raw, label)

    arguments = {"notes": [4_000_057]}
    with scripted(tmp_path) as client:
        clock = SimpleNamespace(monotonic=lambda: time.monotonic() + skew[0])
        monkeypatch.setattr(mcp, "time", clock)
        monkeypatch.setattr(mcp, "_decoded", slow)
        if slow_frame == "reply":
            assert client.call("first", arguments) == {"echo": arguments}
        else:
            with pytest.raises(McpClientError, match="deadline exceeded"):
                client.call("first", arguments)


@pytest.mark.parametrize("response_id", [True, "2", 99])
def test_invalid_response_ids_are_permanent_wire_errors(
    tmp_path: Path, response_id: object
) -> None:
    with scripted(tmp_path) as client:
        frame = json.dumps({"jsonrpc": "2.0", "id": response_id, "result": {}})
        with pytest.raises(mcp.McpProtocolError, match="another request"):
            client.call("invalid", {"before": [frame]})


def test_oversized_wire_reply_is_permanent_but_eof_is_transport(tmp_path: Path) -> None:
    with (
        McpClient(server(tmp_path, "oversized"), tmp_path) as client,
        pytest.raises(mcp.McpProtocolError, match="oversized"),
    ):
        client.call("oversized", {})
    with (
        pytest.raises(McpClientError) as eof,
        McpClient(server(tmp_path, "eof"), tmp_path),
    ):
        pytest.fail("EOF completed the handshake")
    assert type(eof.value) is McpClientError


@pytest.mark.parametrize(
    "name,permanent",
    [
        ("ENOENT", True),
        ("EACCES", True),
        ("ENOEXEC", True),
        ("ENOTDIR", True),
        ("EISDIR", True),
        ("ELOOP", True),
        ("ENAMETOOLONG", True),
        ("EAGAIN", False),
        ("ENOMEM", False),
        ("EMFILE", False),
        ("ENFILE", False),
    ],
)
def test_spawn_error_classification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, permanent: bool
) -> None:
    import errno

    error = OSError(getattr(errno, name), name)

    def failed(*_: Any, **__: Any) -> Any:
        raise error

    monkeypatch.setattr(subprocess, "Popen", failed)
    with (
        pytest.raises(McpClientError, match="spawned") as caught,
        McpClient(StdioServer(sys.executable), tmp_path),
    ):
        pytest.fail("An unusable command was started")
    expected = mcp.McpConfigurationError if permanent else McpClientError
    assert type(caught.value) is expected
    assert caught.value.__cause__ is error


#: Answers the handshake, then never reads stdin again.
DEAF = """import json, sys, time
line = sys.stdin.readline()
print(json.dumps({"jsonrpc": "2.0", "id": json.loads(line)["id"], "result": {"serverInfo": {"name": "nspawn"}}}), flush=True)
time.sleep(30)
"""


def test_a_peer_that_never_reads_fails_each_call_within_its_deadline(
    tmp_path: Path,
) -> None:
    """The request's own write had no time limit: a peer that stopped reading stdin
    held the call, and the thread making it, until that peer exited. The write is
    bounded by the call's deadline; a frame cut short ends the session, so the next
    call is refused at once rather than corrupting the stream."""
    script = tmp_path / "deaf.py"
    script.write_text(DEAF)
    client = McpClient(StdioServer(sys.executable, (str(script),)), tmp_path, 1)
    outcomes: list[tuple[float, BaseException | None]] = []

    def two_calls() -> None:
        for _ in range(2):
            started = time.monotonic()
            try:
                client.call("fixture_write", {"pad": "x" * (1 << 20)})
            except McpClientError as error:
                outcomes.append((time.monotonic() - started, error))
            else:
                outcomes.append((time.monotonic() - started, None))

    with client:
        process = client._process
        assert process is not None
        caller = threading.Thread(target=two_calls, daemon=True)
        caller.start()
        caller.join(10)
        hung = caller.is_alive()
        process.kill()
        caller.join(10)
    assert not hung, "A call to a peer that never reads must end on its deadline."
    assert len(outcomes) == 2
    assert all(error is not None for _, error in outcomes)
    assert all(elapsed < 3 for elapsed, _ in outcomes)
    assert isinstance(outcomes[0][1], mcp.McpDeadline)


#: Answers the handshake, then marks the first complete request it receives.
MARKS = """import json, pathlib, sys
line = sys.stdin.readline()
print(json.dumps({"jsonrpc": "2.0", "id": json.loads(line)["id"], "result": {"serverInfo": {"name": "nspawn"}}}), flush=True)
for line in sys.stdin:
    if '"tools/call"' in line:
        pathlib.Path(sys.argv[1]).write_text(line[:40])
"""


def test_the_rest_of_a_frame_never_goes_out_after_its_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The clock was read only when a write would block: once the deadline passed
    mid-frame, a peer that drained the pipe again took the rest of the request, so
    it could run a request whose call had already failed. Moving the clock past
    the deadline after the frame's first part, not the host's speed, puts the
    deadline mid-frame."""
    script = tmp_path / "marks.py"
    script.write_text(MARKS)
    received = tmp_path / "received"
    skew = [0.0]
    clock = SimpleNamespace(monotonic=lambda: time.monotonic() + skew[0])
    server = StdioServer(sys.executable, (str(script), str(received)))
    with McpClient(server, tmp_path, 5) as client:
        assert client._process is not None and client._process.stdin is not None
        stdin = client._process.stdin.fileno()
        write = os.write

        def first_part(descriptor: int, data: Any) -> int:
            if descriptor != stdin or skew[0]:
                return write(descriptor, data)
            skew[0] = 60
            return write(descriptor, bytes(data[:1000]))

        monkeypatch.setattr(mcp, "time", clock)
        monkeypatch.setattr(mcp.os, "write", first_part)
        with pytest.raises(mcp.McpDeadline):
            client.call("fixture_write", {"pad": "x" * 20_000})
        monkeypatch.undo()
        time.sleep(0.5)
    assert not received.exists(), "The peer received the request after its deadline."
