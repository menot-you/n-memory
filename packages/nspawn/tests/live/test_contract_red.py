"""Six live observations that turn RED only for their declared reason."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from live.live_support import FAKE_CLAUDE, McpStdio, store_rows
from workspace_root import WORKSPACE_ROOT

from nspawn.mcp import NSPAWN_TOOL_CONTRACTS
from nspawn.owner_roster import Roster, RosterError
from nspawn.process_identity import (
    ProcessIdentity,
    ProcessIdentityError,
    capture_process_identity,
    process_identity_matches,
)

TERMINAL_PHASES = frozenset({"completed", "failed", "closed"})


FAKE_CODEX = Path(__file__).resolve().parent / "fake_codex.py"


@dataclass(frozen=True)
class RedObservation:
    """Distinguishable reason observed before the contracted result exists."""

    reason: str
    detail: str


def _finish_red(
    scenario: str, expected_reason: str, observed: RedObservation | None
) -> None:
    if observed is None:
        return
    if observed.reason != expected_reason:
        pytest.fail(
            f"{scenario} WRONG RED: expected={expected_reason}; "
            f"observed={observed.reason}; detail={observed.detail}"
        )
    pytest.fail(
        f"{scenario} EXPECTED RED RECOGNIZED: observed={observed.reason}; "
        f"detail={observed.detail}"
    )


def _decoded_transcript(transcript: dict[str, Any] | None) -> str:
    """Renders the transcript with the NDJSON lines decoded one level."""
    if transcript is None:
        return ""
    parts: list[str] = []
    for line in transcript.get("lines", ()):
        try:
            parts.append(
                json.dumps(json.loads(line), ensure_ascii=False, sort_keys=True)
            )
        except json.JSONDecodeError, TypeError:
            parts.append(str(line))
    return "\n".join(parts)


def _prompt_frame(prompt: str) -> bytes:
    frame = {
        "type": "user",
        "message": {"role": "user", "content": prompt},
    }
    return json.dumps(frame, separators=(",", ":")).encode() + b"\n"


def _fake_command() -> list[str]:
    return [
        sys.executable,
        str(FAKE_CLAUDE),
        "-p",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--verbose",
    ]


def _wait_for_path(path: Path, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            return True
        time.sleep(0.01)
    return path.is_file()


def _fake_contract(
    tmp_path: Path, live_environment: dict[str, str]
) -> RedObservation | None:
    environment = dict(live_environment)
    environment["NOTT_NSPAWN_FAKE_CLAUDE_MODE"] = "terminal_clean"
    clean = subprocess.run(
        _fake_command(),
        cwd=WORKSPACE_ROOT,
        env=environment,
        input=_prompt_frame("Responda exatamente: PONG"),
        capture_output=True,
        timeout=10,
        check=False,
    )
    if clean.returncode != 0:
        return RedObservation(
            "ld0_fake_wire_invalid", "a clean terminal did not exit zero"
        )
    try:
        frames = [json.loads(line) for line in clean.stdout.splitlines()]
    except json.JSONDecodeError:
        return RedObservation("ld0_fake_wire_invalid", "stdout is not measured NDJSON")
    rate = next(
        (frame for frame in frames if frame.get("type") == "rate_limit_event"),
        None,
    )
    if not isinstance(rate, dict):
        return RedObservation("ld0_fake_wire_invalid", "rate_limit_event ausente")
    info = rate.get("rate_limit_info")
    expected_info = {
        "status": "allowed",
        "resetsAt": 1787512800,
        "rateLimitType": "five_hour",
        "overageStatus": "rejected",
        "isUsingOverage": False,
    }
    if not isinstance(info, dict) or any(
        info.get(name) != value for name, value in expected_info.items()
    ):
        return RedObservation(
            "ld0_fake_wire_invalid",
            "rate_limit_info does not keep camelCase and epoch seconds",
        )
    if any(name in rate for name in ("limit_type", "status", "resets_at_seconds")):
        return RedObservation(
            "ld0_fake_wire_invalid", "rate limit foi achatado no topo"
        )
    terminal = next(
        (frame for frame in reversed(frames) if frame.get("type") == "result"),
        None,
    )
    if not isinstance(terminal, dict) or terminal.get("terminal_reason") != "completed":
        return RedObservation(
            "ld0_fake_wire_invalid", "the clean terminal was not measured"
        )

    environment["NOTT_NSPAWN_FAKE_CLAUDE_MODE"] = "empty_stdout_stderr_error"
    empty = subprocess.run(
        _fake_command(),
        cwd=WORKSPACE_ROOT,
        env=environment,
        input=_prompt_frame("irrelevante"),
        capture_output=True,
        timeout=10,
        check=False,
    )
    if (
        empty.returncode != 0
        or empty.stdout != b""
        or b"rate_limit_error" not in empty.stderr
    ):
        return RedObservation(
            "ld0_fake_wire_invalid", "exit zero with an error only on stderr diverged"
        )

    ready = tmp_path / "signal-ready"
    environment["NOTT_NSPAWN_FAKE_CLAUDE_MODE"] = "signal"
    environment["NOTT_NSPAWN_FAKE_READY_FILE"] = str(ready)
    signaled = subprocess.Popen(
        _fake_command(),
        cwd=WORKSPACE_ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    identity: ProcessIdentity | None = None
    try:
        if not _wait_for_path(ready, 5):
            return RedObservation("ld0_fake_wire_invalid", "signal mode did not start")
        identity = capture_process_identity(signaled.pid)
        os.killpg(identity.pid, signal.SIGKILL)
        return_code = signaled.wait(timeout=5)
        if return_code != -signal.SIGKILL:
            return RedObservation(
                "ld0_fake_wire_invalid", "death by signal was not observed"
            )
        if process_identity_matches(identity):
            return RedObservation(
                "ld0_fake_wire_invalid", "identidade sobreviveu ao sinal"
            )
    except (OSError, ProcessIdentityError, subprocess.TimeoutExpired) as error:
        return RedObservation("ld0_fake_wire_invalid", type(error).__name__)
    finally:
        if identity is not None and process_identity_matches(identity):
            try:
                os.killpg(identity.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if signaled.poll() is None:
            signaled.kill()
            signaled.wait(timeout=5)
    return None


def _tool_payload(answer: dict[str, Any]) -> dict[str, Any] | None:
    """The payload inside a `tools/call` answer, or None if the envelope is wrong.

    This file is the ONE judge in the repository that reads what actually
    leaves the process, and it is the one that caught the day the dispatch path
    stopped packaging its answer at all. It stays strict on purpose: a result
    without the spec's `content` block is not unwrapped leniently, it is no
    payload, and the caller's typed RedObservation names the verb.
    """
    result = answer.get("result")
    if not isinstance(result, dict):
        return None
    content = result.get("content")
    if not isinstance(content, list) or not content:
        return None
    block = content[0]
    if not isinstance(block, dict) or block.get("type") != "text":
        return None
    try:
        payload = json.loads(block["text"])
    except KeyError, TypeError, json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _handshake(client: McpStdio) -> RedObservation | None:
    answer = client.handshake()
    result = answer.get("result")
    if not isinstance(result, dict):
        return RedObservation(
            "mcp_handshake_invalid", json.dumps(answer, sort_keys=True)
        )
    server = result.get("serverInfo")
    if not isinstance(server, dict) or server.get("name") != "nspawn":
        return RedObservation(
            "mcp_handshake_invalid", json.dumps(answer, sort_keys=True)
        )
    return None


def _error_detail(answer: dict[str, Any]) -> str:
    error = answer.get("error")
    return json.dumps(error, sort_keys=True, ensure_ascii=False)


def _execute(
    client: McpStdio,
    arguments: dict[str, Any],
    absent_reason: str,
) -> tuple[str | None, RedObservation | None]:
    # `class` is mandatory on the wire. Each probe in this file is mechanical
    # work against the route; whoever measures another class passes theirs and wins.
    # `read_only` defaults to True: only LD-3 writes, and it passes
    # `read_only: False` with `native_write` on a declared checkout.
    arguments = {"class": "mechanical_fanout", "read_only": True, **arguments}
    answer = client.call_tool("nspawn_run_start", arguments)
    error = answer.get("error")
    if isinstance(error, dict):
        message = error.get("message")
        if (
            error.get("code") == -32601
            and isinstance(message, str)
            and ("nspawn_run_start" in message)
        ):
            return None, RedObservation(absent_reason, message)
        return None, RedObservation("nspawn_execute_refused", _error_detail(answer))
    result = _tool_payload(answer)
    if result is None or not isinstance(result.get("run_id"), str):
        return None, RedObservation(
            "nspawn_execute_invalid_result", json.dumps(answer, sort_keys=True)
        )
    return result["run_id"], None


def _terminal_status(
    client: McpStdio, run_id: str
) -> tuple[dict[str, Any] | None, RedObservation | None]:
    deadline = time.monotonic() + 20
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        answer = client.call_tool(
            "nspawn_run_show", {"run_id": run_id, "timeout_ms": 2000}
        )
        if "error" in answer:
            return None, RedObservation(
                "nspawn_run_show_refused", _error_detail(answer)
            )
        result = _tool_payload(answer)
        if result is None:
            return None, RedObservation(
                "nspawn_run_show_invalid_result", json.dumps(answer, sort_keys=True)
            )
        last = result
        if result.get("phase") in TERMINAL_PHASES:
            return result, None
    return None, RedObservation(
        "nspawn_run_show_not_terminal", json.dumps(last, sort_keys=True)
    )


def _transcript(
    client: McpStdio, run_id: str
) -> tuple[dict[str, Any] | None, RedObservation | None]:
    answer = client.call_tool(
        "nspawn_transcript_show",
        {"run_id": run_id, "since_seq": 0, "max_bytes": 1048576},
    )
    if "error" in answer:
        return None, RedObservation("nspawn_transcript_refused", _error_detail(answer))
    result = _tool_payload(answer)
    if result is None:
        return None, RedObservation(
            "nspawn_transcript_invalid_result", json.dumps(answer, sort_keys=True)
        )
    return result, None


def _provider_routes(roster: Roster) -> tuple[tuple[str, str | None], ...]:
    routes: list[tuple[str, str | None]] = []
    for provider in roster.providers:
        models = list(provider.models)
        if provider.model is not None and provider.model not in models:
            models.append(provider.model)
        if models:
            routes.extend((provider.name, model) for model in models)
        else:
            routes.append((provider.name, None))
    return tuple(routes)


def test_ld0_stdio_handshake_lists_only_the_declared_tools(
    mcp_client: McpStdio, tmp_path: Path, live_environment: dict[str, str]
) -> None:
    fake_red = _fake_contract(tmp_path, live_environment)
    if fake_red is not None:
        _finish_red("LD-0", "ld0_legacy_thirteen_empty_schemas", fake_red)
        return
    handshake_red = _handshake(mcp_client)
    if handshake_red is not None:
        _finish_red("LD-0", "ld0_legacy_thirteen_empty_schemas", handshake_red)
        return
    answer = mcp_client.request("tools/list")
    result = answer.get("result")
    tools = result.get("tools") if isinstance(result, dict) else None
    if not isinstance(tools, list):
        observed = RedObservation("ld0_tools_list_invalid", json.dumps(answer))
    else:
        # DERIVED, never typed out. A hand-written set of five stopped
        # describing this surface the day a sixth verb landed, and a judge that
        # names a stale vocabulary reports the wrong RED.
        expected = {contract.name for contract in NSPAWN_TOOL_CONTRACTS}
        names = {
            tool.get("name")
            for tool in tools
            if isinstance(tool, dict) and isinstance(tool.get("name"), str)
        }
        # A contract that takes nothing (the fleet listing) declares a CLOSED
        # empty schema; served closed, it is a contract, not a placeholder.
        argumentless = {
            contract.name
            for contract in NSPAWN_TOOL_CONTRACTS
            if contract.input_schema.get("properties") == {}
            and contract.input_schema.get("additionalProperties") is False
        }
        empty = [
            tool.get("name")
            for tool in tools
            if isinstance(tool, dict)
            and isinstance(tool.get("inputSchema"), dict)
            and not tool["inputSchema"].get("properties")
            and not (
                tool.get("name") in argumentless
                and tool["inputSchema"].get("additionalProperties") is False
            )
        ]
        # 13 is HISTORY, not a moving count: the legacy door served exactly
        # thirteen tools with empty schemas, and recognising that shape is this
        # scenario's declared RED reason.
        if len(tools) == 13 and len(empty) == 13:
            observed = RedObservation(
                "ld0_legacy_thirteen_empty_schemas",
                f"tools={sorted(names)}; empty_schemas={sorted(empty)}",
            )
        elif len(tools) != len(expected) or names != expected or empty:
            observed = RedObservation(
                "ld0_five_tool_contract_mismatch",
                f"count={len(tools)}; names={sorted(names)}; empty={sorted(empty)}",
            )
        else:
            observed = None
    _finish_red("LD-0", "ld0_legacy_thirteen_empty_schemas", observed)


def test_u1_harvests_the_persisted_terminal_run(
    mcp_client: McpStdio, tmp_path: Path
) -> None:
    assert _handshake(mcp_client) is None
    cwd = tmp_path / "u1-cwd"
    cwd.mkdir()
    run_id, execution_red = _execute(
        mcp_client,
        {"cwd": str(cwd), "prompt": "Responda exatamente: PONG"},
        "u1_execute_tool_absent",
    )
    assert execution_red is None
    assert run_id is not None

    status, status_red = _terminal_status(mcp_client, run_id)
    assert status_red is None
    assert status is not None
    run_dir = Path(status["run_dir"])
    stdout = run_dir / "stdout"
    stdout_bytes = stdout.read_bytes()
    complete_records = stdout_bytes.count(b"\n")
    frames = [json.loads(line) for line in stdout_bytes.splitlines()]
    terminal_text = next(
        frame["result"] for frame in reversed(frames) if frame.get("type") == "result"
    )
    assert isinstance(terminal_text, str)
    (stored,) = store_rows(
        "SELECT final_text, blocked_reason, records_seen FROM runs WHERE run_id = ?",
        (run_id,),
    )

    assert run_dir == tmp_path / "runs" / run_id
    assert status["failure_reason"] is None
    assert status["total_seq"] == complete_records
    assert stored == (terminal_text, None, complete_records)

    stdout.rename(run_dir / "stdout-witnessed")
    result = _tool_payload(
        mcp_client.call_tool("nspawn_result_show", {"run_id": run_id})
    )
    assert result == {
        "run_id": run_id,
        "final_text": terminal_text,
        "stderr_tail": "",
        "phase": "completed",
    }


def test_ld1_every_disposable_roster_route_answers_pong(
    mcp_client: McpStdio, tmp_path: Path, live_environment: dict[str, str]
) -> None:
    handshake_red = _handshake(mcp_client)
    if handshake_red is not None:
        _finish_red("LD-1", "ld1_execute_tool_absent_for_all_routes", handshake_red)
        return
    roster_path = Path(live_environment["NOTT_NSPAWN_ROSTER"])
    try:
        roster = Roster.read(roster_path)
    except (OSError, RosterError) as error:
        observed = RedObservation("ld1_real_roster_unreadable", str(error))
        _finish_red("LD-1", "ld1_execute_tool_absent_for_all_routes", observed)
        return
    routes = _provider_routes(roster)
    if not routes:
        observed = RedObservation("ld1_real_roster_empty", str(roster_path))
        _finish_red("LD-1", "ld1_execute_tool_absent_for_all_routes", observed)
        return
    absent: list[str] = []
    other_failures: list[str] = []
    for index, (provider, model) in enumerate(routes):
        cwd = tmp_path / f"route-{index:03d}"
        cwd.mkdir()
        arguments: dict[str, Any] = {
            "provider": provider,
            "prompt": "Responda exatamente: PONG",
            "cwd": str(cwd),
        }
        if model is not None:
            arguments["model"] = model
        run_id, execution_red = _execute(
            mcp_client, arguments, "ld1_execute_tool_absent_for_all_routes"
        )
        route = f"{provider}/{model or '<provider-default>'}"
        if execution_red is not None:
            if execution_red.reason == "ld1_execute_tool_absent_for_all_routes":
                absent.append(route)
            else:
                other_failures.append(f"{route}:{execution_red.reason}")
            continue
        if run_id is None:
            other_failures.append(f"{route}:missing_run_id")
            continue
        _, status_red = _terminal_status(mcp_client, run_id)
        if status_red is not None:
            other_failures.append(f"{route}:{status_red.reason}")
            continue
        transcript, transcript_red = _transcript(mcp_client, run_id)
        if transcript_red is not None:
            other_failures.append(f"{route}:{transcript_red.reason}")
            continue
        rendered = json.dumps(transcript, ensure_ascii=False, sort_keys=True)
        if "PONG" not in rendered:
            other_failures.append(f"{route}:pong_absent_from_transcript")
    if len(absent) == len(routes) and not other_failures:
        observed = RedObservation(
            "ld1_execute_tool_absent_for_all_routes",
            f"{len(absent)} rotas exercitadas: {', '.join(absent)}",
        )
    elif absent or other_failures:
        observed = RedObservation(
            "ld1_routes_incomplete",
            f"absent={absent}; other={other_failures}",
        )
    else:
        observed = None
    _finish_red("LD-1", "ld1_execute_tool_absent_for_all_routes", observed)


def test_ld2_child_reads_the_actual_readme_first_line(mcp_client: McpStdio) -> None:
    handshake_red = _handshake(mcp_client)
    if handshake_red is not None:
        _finish_red("LD-2", "ld2_execute_tool_absent", handshake_red)
        return
    expected = (
        (WORKSPACE_ROOT / "README.md").read_bytes().splitlines()[0].decode("utf-8")
    )
    run_id, observed = _execute(
        mcp_client,
        {
            "cwd": str(WORKSPACE_ROOT),
            "prompt": (
                "Read README.md from the current directory using an actual file-reading "
                "operation. Return its first line verbatim, preserving every character, "
                "including existing Markdown punctuation. Output only that line; add no "
                "explanation, quotes, or code fences."
            ),
        },
        "ld2_execute_tool_absent",
    )
    if observed is None and run_id is not None:
        _, observed = _terminal_status(mcp_client, run_id)
    transcript: dict[str, Any] | None = None
    if observed is None and run_id is not None:
        transcript, observed = _transcript(mcp_client, run_id)
    if observed is None:
        # Each line of the transcript IS ALREADY an NDJSON string: a `json.dumps` over
        # it returns literal `\\u2014`, so a decoded em-dash would NEVER
        # be found. Decodes one level before searching.
        rendered = _decoded_transcript(transcript)
        if expected not in rendered:
            observed = RedObservation(
                "ld2_readme_bytes_mismatch",
                f"expected={expected!r}; transcript={rendered}",
            )
    _finish_red("LD-2", "ld2_execute_tool_absent", observed)


def _add_codex_writer(tmp_path: Path) -> None:
    """A codex route for the writer and its declared root: only codex writes
    natively, and only in a checkout under `[native_write] roots`. The roster
    is read per dispatch, so appending it to the served config reaches the
    running process."""
    home = tmp_path / "codex-home"
    home.mkdir(mode=0o700)
    auth = home / "auth.json"
    auth.write_text(json.dumps({"tokens": {"access_token": "disposable-fixture"}}))
    auth.chmod(0o600)
    with (tmp_path / "spawn.toml").open("a") as config:
        config.write(
            f"[bins]\ncodex = {json.dumps(str(FAKE_CODEX))}\n"
            '[[provider]]\nname = "writer"\nkind = "codex"\n'
            'model = "test-model"\nmodels = ["test-model"]\n'
            f"[provider.accounts.first]\nhome = {json.dumps(str(home))}\n"
            f"[native_write]\nroots = {json.dumps([str(tmp_path.resolve())])}\n"
        )


def _ld3_observation(
    mcp_client: McpStdio, tmp_path: Path, prompt: str
) -> RedObservation | None:
    """LD-3's measurement: a served writer asked `prompt` in `write-cwd`."""
    handshake_red = _handshake(mcp_client)
    if handshake_red is not None:
        return handshake_red
    _add_codex_writer(tmp_path)
    output = (tmp_path / "write-cwd").resolve() / "IT_WORKS.md"
    (output.parent / ".git").mkdir(parents=True)
    run_id, observed = _execute(
        mcp_client,
        {
            "provider": "writer",
            "cwd": str(output.parent),
            "prompt": prompt,
            "read_only": False,
            "native_write": True,
        },
        "ld3_execute_tool_absent",
    )
    if observed is None and run_id is not None:
        _, observed = _terminal_status(mcp_client, run_id)
    if observed is None and not output.is_file():
        observed = RedObservation("ld3_file_absent", str(output))
    if observed is None and output.read_bytes() != b"OK":
        observed = RedObservation("ld3_file_bytes_mismatch", repr(output.read_bytes()))
    return observed


@pytest.mark.needs("systemd_user", "cgroup", "bwrap")
def test_ld3_child_writes_ok_in_the_requested_cwd(
    mcp_client: McpStdio, tmp_path: Path
) -> None:
    observed = _ld3_observation(
        mcp_client, tmp_path, "write IT_WORKS.md with the text OK"
    )
    _finish_red("LD-3", "ld3_execute_tool_absent", observed)


@pytest.mark.needs("systemd_user", "cgroup", "bwrap")
@pytest.mark.parametrize(
    ("prompt", "reason"),
    [
        # The writer answers BLOCKED without editing: the file LD-3 looks for
        # is absent.
        ("decline this change", "ld3_file_absent"),
        # The writer writes, but not the bytes LD-3 asked for.
        ("write IT_WORKS.md with the text KO", "ld3_file_bytes_mismatch"),
    ],
    # The ids the body-door version gave them, kept across the re-expression.
    ids=[
        "OK-decline this change-ld3_file_absent",
        "KO-write IT_WORKS.md with the text KO-ld3_file_bytes_mismatch",
    ],
)
def test_ld3_names_its_red_when_the_writer_does_not_write_ok(
    mcp_client: McpStdio, tmp_path: Path, prompt: str, reason: str
) -> None:
    observed = _ld3_observation(mcp_client, tmp_path, prompt)
    assert observed is not None
    assert observed.reason == reason, observed


def test_ld3_writable_dispatch_knocks_only_on_the_test_door(
    mcp_client: McpStdio, tmp_path: Path
) -> None:
    """The refusal half of LD-3: a writable dispatch that does not ask for
    `native_write` is refused before any child starts or any run exists."""
    assert _handshake(mcp_client) is None
    cwd = tmp_path / "write-cwd"
    cwd.mkdir()
    answer = mcp_client.call_tool(
        "nspawn_run_start",
        {
            "class": "mechanical_fanout",
            "cwd": str(cwd),
            "prompt": "write IT_WORKS.md with the text OK",
            "read_only": False,
        },
    )
    assert answer["error"]["data"]["type"] == "InvalidRunRequest", answer
    assert "native_write" in answer["error"]["message"], answer
    assert not (cwd / "IT_WORKS.md").exists()
    # `runs/drivers` holds the per-call drivers' launch acks, not a run.
    runs = tmp_path / "runs"
    assert not runs.exists() or [p for p in runs.iterdir() if p.name != "drivers"] == []


def _tree_identities(path: Path) -> tuple[ProcessIdentity, ProcessIdentity]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError("the tree receipt is not an object")
    parent_pid = value.get("parent_pid")
    grandchild_pid = value.get("grandchild_pid")
    if not isinstance(parent_pid, int) or not isinstance(grandchild_pid, int):
        raise TypeError("the tree receipt does not carry two pids")
    return capture_process_identity(parent_pid), capture_process_identity(
        grandchild_pid
    )


def _identities_dead(
    parent: ProcessIdentity, grandchild: ProcessIdentity, timeout: float
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not process_identity_matches(parent) and not process_identity_matches(
            grandchild
        ):
            return True
        time.sleep(0.02)
    return not process_identity_matches(parent) and not process_identity_matches(
        grandchild
    )


def _cleanup_owned_tree(parent: ProcessIdentity | None) -> None:
    if parent is None or not process_identity_matches(parent):
        return
    try:
        os.killpg(parent.pid, signal.SIGKILL)
    except ProcessLookupError:
        return


def test_close_kills_parent_and_grandchild_by_identity(
    mcp_client: McpStdio, tmp_path: Path
) -> None:
    handshake_red = _handshake(mcp_client)
    if handshake_red is not None:
        _finish_red("CLOSE", "close_execute_tool_absent", handshake_red)
        return
    cwd = tmp_path / "tree-cwd"
    cwd.mkdir()
    receipt = cwd / "tree-pids.json"
    parent: ProcessIdentity | None = None
    observed: RedObservation | None = None
    run_id: str | None = None
    try:
        run_id, observed = _execute(
            mcp_client,
            {
                "cwd": str(cwd),
                "prompt": f"SPAWN_GRANDCHILD_IDENTITY_FILE={receipt}",
            },
            "close_execute_tool_absent",
        )
        if observed is None and not _wait_for_path(receipt, 5):
            observed = RedObservation("close_tree_receipt_absent", str(receipt))
        grandchild: ProcessIdentity | None = None
        if observed is None:
            try:
                parent, grandchild = _tree_identities(receipt)
            except (OSError, TypeError, ProcessIdentityError) as error:
                observed = RedObservation("close_tree_identity_invalid", str(error))
        if observed is None and run_id is not None:
            answer = mcp_client.call_tool("nspawn_run_close", {"run_id": run_id})
            if "error" in answer:
                observed = RedObservation("close_tool_refused", _error_detail(answer))
            elif _tool_payload(answer) is None:
                observed = RedObservation(
                    "close_invalid_result", json.dumps(answer, sort_keys=True)
                )
        if (
            observed is None
            and parent is not None
            and grandchild is not None
            and not _identities_dead(parent, grandchild, 5)
        ):
            observed = RedObservation(
                "close_tree_survived",
                f"parent_alive={process_identity_matches(parent)}; "
                f"grandchild_alive={process_identity_matches(grandchild)}",
            )
    finally:
        _cleanup_owned_tree(parent)
    _finish_red("CLOSE", "close_execute_tool_absent", observed)


def test_contract_ignores_ambient_owner_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    """Poison only disposable owner state before constructing the real client."""
    home = tmp_path / "ambient-home"
    (home / ".nott").mkdir(parents=True)
    poison = home / ".nott" / "spawn.toml"
    poison.write_text("[store]\nowner_state_must_not_be_read = true\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("NOTT_NSPAWN_CONFIG", str(poison))
    monkeypatch.setenv("NOTT_NSPAWN_ROSTER", str(home / ".nott" / "providers.toml"))
    client = request.getfixturevalue("mcp_client")
    assert _handshake(client) is None
    run_id, refused = _execute(
        client,
        {"cwd": str(tmp_path), "prompt": "Responda exatamente: PONG"},
        "execute_missing",
    )
    assert refused is None
    assert run_id is not None
    status, refused = _terminal_status(client, run_id)
    assert refused is None
    assert status is not None and status["phase"] == "completed"
