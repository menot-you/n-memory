#!/usr/bin/env python3
"""LD-0..LD-4 over a candidate's real nSPAWN stdio boundary.

Every provider/account/model cell gets its own file and memory observations.
Run this file directly with --binary and --repo-root; it installs nothing.
--evidence-dir writes a digest-bound envelope into a new external directory.
Memory success requires measured, correlated tool-result schemas; protocol
replays and unit parser controls do not establish current native reach.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import selectors
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import uuid4

import nott_database

from nspawn.mcp import MAX_RESPONSE_BYTES, NSPAWN_TOOL_CONTRACTS
from nspawn.owner_roster import Roster, RosterError
from nspawn.pump import MAX_RUN_OUTPUT_BYTES

GREEN = "GREEN"
BLOCKED = "BLOCKED"
RED = "RED"

# GREEN < BLOCKED < RED for picking the worst status inside one row; the
# script-exit mapping (D18: 0 green, 3 findings, 2 refusal, 1 operational)
# is computed separately in `overall_exit_code`.
_STATUS_TO_EXIT = {GREEN: 0, BLOCKED: 2, RED: 3}

RECEIVE_TIMEOUT = 15.0
EXECUTE_TIMEOUT = 180.0  # a real Claude turn, once Supervisor exists, is slow


@dataclass(frozen=True)
class Row:
    row_id: str
    observation: str
    status: str
    detail: str
    evidence: dict[str, Any] | None = None


class Crashed(Exception):
    """The nspawn process died before answering.

    ``reason`` is the process's OWN last stderr line — for the current
    Supervisor stub that is the exact ``NotImplementedError: ...`` message,
    which is the named gap the caller needs, not a guess about it.
    """

    def __init__(self, reason: str, returncode: int | None) -> None:
        self.reason = reason
        self.returncode = returncode
        super().__init__(reason)


def _last_stderr_line(stderr_text: str, returncode: int | None) -> str:
    stripped = stderr_text.strip()
    if not stripped:
        return f"processo saiu sem stderr (rc={returncode})"
    return stripped.splitlines()[-1]


class McpStdio:
    """One real ``nspawn`` subprocess, spoken to over stdio JSON-RPC."""

    def __init__(self, binary: Path, run_root: Path, extra_env: dict[str, str]) -> None:
        environment = dict(os.environ)
        environment.update(extra_env)
        environment["NOTT_NSPAWN_RUN_ROOT"] = str(run_root)
        # The one executable serves MCP only as `nspawn mcp serve`.
        self.process = subprocess.Popen(
            [str(binary), "mcp", "serve"],
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            start_new_session=True,
        )
        if self.process.stdin is None or self.process.stdout is None:
            raise RuntimeError("the nspawn subprocess did not open its stdio pipes")
        self._stdin = self.process.stdin
        self._stdout = self.process.stdout
        self._buffer = bytearray()
        self._next_id = 1

    def _receive(self, timeout: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        descriptor = self._stdout.fileno()
        selector = selectors.DefaultSelector()
        selector.register(descriptor, selectors.EVENT_READ)
        try:
            while True:
                newline = self._buffer.find(b"\n")
                if newline >= 0:
                    raw = bytes(self._buffer[:newline])
                    del self._buffer[: newline + 1]
                    if not raw.strip():
                        continue
                    try:
                        value = json.loads(raw)
                    except (json.JSONDecodeError, UnicodeDecodeError) as error:
                        raise ObservationRefused("stdio: invalid JSON frame") from error
                    if not isinstance(value, dict):
                        raise ObservationRefused("stdio: frame is not an object")
                    return value
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"sem resposta em {timeout}s")
                if not selector.select(remaining):
                    raise TimeoutError(f"sem resposta em {timeout}s")
                chunk = os.read(descriptor, 65536)
                if not chunk:
                    try:
                        self._stdin.close()
                    except OSError:
                        pass
                    returncode = self.process.wait(timeout=10)
                    stderr_text = (
                        self.process.stderr.read().decode("utf-8", "replace")
                        if self.process.stderr is not None
                        else ""
                    )
                    raise Crashed(
                        _last_stderr_line(stderr_text, returncode), returncode
                    )
                self._buffer.extend(chunk)
        finally:
            selector.close()

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float = RECEIVE_TIMEOUT,
    ) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        frame: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            frame["params"] = params
        self._stdin.write(json.dumps(frame, separators=(",", ":")).encode() + b"\n")
        self._stdin.flush()
        while True:
            answer = self._receive(timeout)
            if answer.get("id") == request_id:
                return answer

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        frame: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            frame["params"] = params
        self._stdin.write(json.dumps(frame, separators=(",", ":")).encode() + b"\n")
        self._stdin.flush()

    def handshake(self) -> None:
        self.initialization = self.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "nspawn-live-matrix", "version": "1"},
            },
        )
        result = self.initialization.get("result")
        if (
            "error" in self.initialization
            or not isinstance(result, dict)
            or not isinstance(result.get("protocolVersion"), str)
            or not isinstance(result.get("serverInfo"), dict)
            or not isinstance(result.get("capabilities"), dict)
        ):
            raise ObservationRefused("initialize: missing or refused handshake")
        self.notify("notifications/initialized")

    def call_tool(
        self, name: str, arguments: dict[str, Any], timeout: float = RECEIVE_TIMEOUT
    ) -> dict[str, Any]:
        return self.request(
            "tools/call", {"name": name, "arguments": arguments}, timeout=timeout
        )

    def close(self) -> None:
        if self.process.poll() is None:
            try:
                self._stdin.close()
            except OSError:
                pass
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        for stream in (self.process.stdout, self.process.stderr):
            if stream is not None:
                stream.close()


def _bridge_env() -> dict[str, str]:
    """Pass the configured disposable store under its actual runtime contract."""
    return {
        key: os.environ[key]
        for key in (
            "NOTT_DATABASE_DIR",
            "NOTT_NSPAWN_CONFIG",
            "NOTT_NSPAWN_ROSTER",
        )
        if key in os.environ
    }


def _session(binary: Path, run_root: Path, extra_env: dict[str, str]) -> McpStdio:
    client = McpStdio(binary, run_root, extra_env)
    initialized = False
    try:
        client.handshake()
        initialized = True
        return client
    finally:
        if not initialized:
            client.close()


# -- LD-0 --------------------------------------------------------------------


def ld0(binary: Path, tmp_root: Path, extra_env: dict[str, str]) -> Row:
    name = (
        "stdio initialize→notifications/initialized→tools/list, 5 tools com inputSchema"
    )
    try:
        client = _session(binary, tmp_root / "ld0", extra_env)
    except (Crashed, TimeoutError, ObservationRefused) as crash:
        return Row("LD-0", name, BLOCKED, f"handshake: {crash}")
    try:
        answer = client.request("tools/list")
    except (Crashed, TimeoutError) as crash:
        return Row("LD-0", name, BLOCKED, f"tools/list: {crash}")
    finally:
        client.close()

    result = answer.get("result")
    tools = result.get("tools") if isinstance(result, dict) else None
    if not isinstance(tools, list):
        return Row("LD-0", name, RED, f"tools/list did not return a list: {answer}")
    # DERIVED from this tree's declarations, so this line never needs editing
    # when a verb lands. It is NOT an independent pin on the vocabulary and
    # cannot be one: `run_matrix.sh` runs `uv sync --package nspawn`
    # immediately before this probe, the workspace install is editable, so the
    # binary under test and this import read the same source. What the probe
    # still proves is that the LIVE DOOR serves those names over real stdio
    # with non-empty schemas — never that the names themselves are right.
    expected = {contract.name for contract in NSPAWN_TOOL_CONTRACTS}
    names = {t.get("name") for t in tools if isinstance(t, dict)}
    # A contract that takes nothing (the fleet listing) declares a CLOSED empty
    # schema; served closed, it is a contract, not a placeholder.
    argumentless = {
        contract.name
        for contract in NSPAWN_TOOL_CONTRACTS
        if contract.input_schema.get("properties") == {}
        and contract.input_schema.get("additionalProperties") is False
    }
    schemas_non_empty = all(
        isinstance(t.get("inputSchema"), dict)
        and (
            bool(t["inputSchema"].get("properties"))
            or (
                t.get("name") in argumentless
                and t["inputSchema"].get("additionalProperties") is False
            )
        )
        for t in tools
        if isinstance(t, dict)
    )
    if len(tools) == len(expected) and names == expected and schemas_non_empty:
        return Row(
            "LD-0",
            name,
            GREEN,
            f"{len(tools)} tools with a non-empty schema: {sorted(names)}",
            {"initialize": client.initialization, "tools_list": answer},
        )
    return Row(
        "LD-0",
        name,
        RED,
        f"count={len(tools)} names={sorted(n for n in names if isinstance(n, str))} "
        f"schemas_non_empty={schemas_non_empty}",
    )


# -- shared execute→status→transcript pipeline --------------------------------


def _payload_of(answer: dict[str, Any]) -> dict[str, Any] | None:
    """The payload inside one `tools/call` answer, or ``None``.

    `_bounded_result` in nspawn/mcp.py packages EVERY tool answer as one
    text content item whose `text` is the SERIALIZED payload, so NOTHING the
    caller wants is ever at `result.<field>`. Reading it there made every
    dispatching row report `nspawn_run_start sem run_id` over a payload that
    carried one with `isError` false — and, two steps downstream in the same
    function, made the status loop never find a `phase` (so every run burned
    EXECUTE_TIMEOUT and died `nunca chegou a fase terminal`) and made the
    transcript row hand its caller the envelope instead of the transcript.
    Measured 2026-09-02.
    """
    result = answer.get("result")
    if not isinstance(result, dict) or result.get("isError"):
        return None
    content = result.get("content")
    first = content[0] if isinstance(content, list) and content else None
    text = first.get("text") if isinstance(first, dict) else None
    if not isinstance(text, str):
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _run_id_of(answer: dict[str, Any]) -> str | None:
    """The run id out of one `tools/call` answer, or ``None``."""
    payload = _payload_of(answer)
    run_id = payload.get("run_id") if payload is not None else None
    return run_id if isinstance(run_id, str) else None


class ObservationRefused(Exception):
    """Required consumer evidence is absent, malformed or incomplete."""


class ObservationFailed(Exception):
    """The consumer observation contradicts the required behavior."""


def _call_payload(
    client: McpStdio, name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    answer = client.call_tool(name, arguments, timeout=EXECUTE_TIMEOUT)
    payload = _payload_of(answer)
    if payload is None:
        raise ObservationRefused(f"{name}: {_refusal_text(answer)}")
    return payload


def _refusal_text(answer: dict[str, Any]) -> str:
    """nSPAWN's own refusal message, bounded; a BLOCKED row must say why.

    Only a refusal carries its text out. Other tool output can hold anything,
    so it never reaches the row, stdout or the evidence.
    """
    error = answer.get("error")
    result = answer.get("result")
    if isinstance(error, dict):
        text = error.get("message")
    elif isinstance(result, dict) and result.get("isError") is True:
        content = result.get("content")
        first = content[0] if isinstance(content, list) and content else None
        text = first.get("text") if isinstance(first, dict) else None
    else:
        return "tool answer is not a JSON object"
    if isinstance(text, str) and text:
        return text[:2000]
    return "missing or refused payload"


def _close_run(client: McpStdio, run_id: str) -> dict[str, Any]:
    payload = _call_payload(client, "nspawn_run_close", {"run_id": run_id})
    tree = payload.get("tree")
    if (
        payload.get("run_id") != run_id
        or payload.get("phase") != "closed"
        or not isinstance(tree, dict)
        or not isinstance(tree.get("checked"), list)
        or not isinstance(tree.get("survivors"), list)
        or type(payload.get("claim_released")) is not bool
    ):
        raise ObservationRefused("nspawn_run_close: incomplete readback")
    if tree["survivors"] or not payload["claim_released"]:
        raise ObservationFailed(
            "nspawn_run_close: surviving process or unreleased claim"
        )
    return payload


def _validate_route_observation(
    route: tuple[str, str | None, str | None], status: dict[str, Any], lines: list[str]
) -> None:
    """Bind actual backend and observed initialization model to the declared route."""
    provider_name, _, model = route
    try:
        path = Roster.default_path()
        if path is None:
            raise ObservationRefused("route identity: no roster")
        roster = Roster.read(path)
    except (OSError, RosterError) as error:
        raise ObservationRefused("route identity: unreadable roster") from error
    providers = [
        provider for provider in roster.providers if provider.name == provider_name
    ]
    if len(providers) != 1:
        raise ObservationRefused("route identity: provider is unavailable")
    kind = providers[0].kind
    if status.get("backend") != kind:
        raise ObservationFailed("route identity: backend differs from roster kind")
    if model is None:
        raise ObservationRefused("route identity: effective model is unresolved")
    observed = []
    for line in lines:
        try:
            frame = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ObservationRefused(
                "route identity: invalid persisted wire"
            ) from error
        if not isinstance(frame, dict):
            raise ObservationRefused("route identity: persisted frame is not an object")
        # Exact measured fields: codex-appserver-wire-0.153.4:5/15;
        # agy-stream-wire-1.1.27:1; claude-stream-wire-2.1.251:17.
        result = frame.get("result")
        if kind == "codex" and isinstance(result, dict):
            thread = result.get("thread")
            if isinstance(thread, dict) and thread.get("id") == status.get("thread_id"):
                observed.append(result.get("model"))
        elif kind == "agy" and frame.get("event") == "init":
            init = frame.get("init")
            if frame.get("conversation_id") == status.get("thread_id") and isinstance(
                init, dict
            ):
                observed.append(init.get("model"))
        elif (
            kind == "claude"
            and frame.get("type") == "system"
            and frame.get("subtype") == "init"
            and isinstance(status.get("thread_id"), str)
            and frame.get("session_id") == status["thread_id"]
        ):
            observed.append(frame.get("model"))
    if not observed or any(not isinstance(value, str) for value in observed):
        raise ObservationRefused("route identity: missing effective model observation")
    if any(value != model for value in observed):
        raise ObservationFailed(
            "route identity: observed model differs from requested model"
        )


def _run_to_transcript(
    binary: Path,
    run_root: Path,
    extra_env: dict[str, str],
    arguments: dict[str, Any],
    route: tuple[str, str | None, str | None] | None = None,
) -> tuple[str | None, dict[str, Any] | None, str | None]:
    """Collect one current result, complete transcript and witnessed cleanup."""
    client = None
    door_alive = True
    run_id = None
    blocked = red = None
    observation: dict[str, Any] | None = None
    try:
        client = _session(binary, run_root, extra_env)
        # `class` is mandatory on the wire; the probe declares mechanical work and the
        # line that measures another class passes its own and wins.
        # The rows only read unless one passes read_only False and wins.
        arguments = {"class": "mechanical_fanout", "read_only": True, **arguments}
        accepted = _call_payload(client, "nspawn_run_start", arguments)
        run_id = accepted.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            run_id = None
            raise ObservationRefused("nspawn_run_start: missing run identity")
        deadline = time.monotonic() + EXECUTE_TIMEOUT
        while True:
            status = _call_payload(
                client, "nspawn_run_show", {"run_id": run_id, "timeout_ms": 2000}
            )
            if status.get("run_id") != run_id:
                raise ObservationFailed("nspawn_run_show: run identity mismatch")
            if status.get("phase") in {"completed", "failed", "closed"}:
                if status.get("rate_limited") or status.get("api_error_status") in {
                    402,
                    429,
                }:
                    raise ObservationRefused("nspawn_run_show: provider limit")
                _, _, failure = _interpret_status_phase(run_id, status)
                if failure is not None:
                    raise ObservationFailed(failure)
                break
            if time.monotonic() >= deadline:
                raise ObservationRefused("nspawn_run_show: terminal deadline exceeded")
        if route is not None:
            provider, alias, _ = route
            if status.get("executed_provider") != provider or (
                alias is not None and status.get("executed_account") != alias
            ):
                raise ObservationFailed(
                    "nspawn_run_show: executed route identity mismatch"
                )
        if status.get("has_result") is not True:
            raise ObservationRefused("nspawn_run_show: no observed result")
        result = _call_payload(client, "nspawn_result_show", {"run_id": run_id})
        if result.get("run_id") != run_id or not isinstance(
            result.get("final_text"), str
        ):
            raise ObservationRefused("nspawn_result_show: incomplete current result")
        if result.get("failure_reason"):
            raise ObservationFailed("nspawn_result_show: recorded failure")
        lines: list[str] = []
        since = total_bytes = 0
        while True:
            page = _call_payload(
                client,
                "nspawn_transcript_show",
                {
                    "run_id": run_id,
                    "since_seq": since,
                    "max_bytes": MAX_RESPONSE_BYTES // 8,
                },
            )
            next_seq = page.get("next_seq")
            entries = page.get("lines")
            truncated = page.get("truncated")
            if (
                page.get("run_id") != run_id
                or page.get("since_seq") != since
                or type(next_seq) is not int
                or next_seq < since
                or not isinstance(entries, list)
                or any(not isinstance(line, str) for line in entries)
                or type(truncated) is not bool
                or (truncated and next_seq == since)
            ):
                raise ObservationRefused("nspawn_transcript_show: invalid pagination")
            total_bytes += sum(len(line.encode()) + 1 for line in entries)
            if total_bytes > MAX_RUN_OUTPUT_BYTES:
                raise ObservationRefused(
                    "nspawn_transcript_show: output exceeds run byte limit"
                )
            lines.extend(entries)
            since = next_seq
            if not truncated:
                break
            if time.monotonic() >= deadline:
                raise ObservationRefused(
                    "nspawn_transcript_show: pagination deadline exceeded"
                )
        if route is not None:
            _validate_route_observation(route, status, lines)
        observation = {
            "run_id": run_id,
            "status": status,
            "result": result,
            "lines": lines,
            "next_seq": since,
            "truncated": False,
        }
    except Crashed as error:
        door_alive = False
        blocked = f"Crashed: {error}"
    except (TimeoutError, ObservationRefused) as error:
        blocked = f"{type(error).__name__}: {error}"
    except ObservationFailed as error:
        red = str(error)
    finally:
        if client is not None:
            if run_id is not None and door_alive:
                try:
                    close = _close_run(client, run_id)
                    if observation is not None:
                        observation["close"] = close
                except (Crashed, TimeoutError, ObservationRefused) as error:
                    blocked = f"cleanup: {type(error).__name__}: {error}"
                except ObservationFailed as error:
                    red = str(error)
            client.close()
    return blocked, observation if blocked is None and red is None else None, red


def _interpret_status_phase(
    run_id: str, status_payload: dict[str, Any] | None
) -> tuple[str | None, dict[str, Any] | None, str | None]:
    """Validate terminal phase: only completed is accepted; failed and closed are red."""
    if status_payload is None:
        return None, None, f"run {run_id} status ausente"
    phase = status_payload.get("phase")
    if phase == "completed":
        return None, status_payload, None
    if phase in {"failed", "closed"}:
        return None, None, f"run {run_id} terminou em fase {phase}"
    return None, None, f"run {run_id} non-terminal phase: {phase}"


def _codex_memory_invocation(frames: list[dict[str, Any]], thread: str) -> bool:
    # Measured item/started + item/completed schema; capture provenance is in
    # test_probes._codex_memory_observation. Never infer native success from prose.
    terminals = [
        frame["params"].get("turn")
        for frame in frames
        if frame.get("method") == "turn/completed"
        and isinstance(frame.get("params"), dict)
        and frame["params"].get("threadId") == thread
    ]
    if not terminals or not isinstance(terminals[-1], dict):
        return False
    terminal = terminals[-1]
    turn = terminal.get("id")
    if not isinstance(turn, str) or terminal.get("status") != "completed":
        return False
    calls: set[str] = set()
    for frame in frames:
        params = frame.get("params")
        if (
            not isinstance(params, dict)
            or params.get("threadId") != thread
            or params.get("turnId") != turn
        ):
            continue
        item = params.get("item")
        if (
            not isinstance(item, dict)
            or item.get("type") != "mcpToolCall"
            or item.get("server") != "nmemory"
            or item.get("tool") != "nmemory_context_show"
            or not isinstance(item.get("id"), str)
        ):
            continue
        if frame.get("method") == "item/started" and item.get("status") == "inProgress":
            calls.add(item["id"])
        result = item.get("result")
        if (
            frame.get("method") == "item/completed"
            and item["id"] in calls
            and item.get("status") == "completed"
            and item.get("error") is None
            and isinstance(result, dict)
            and result.get("isError") is not True
            and isinstance(result.get("content"), list)
            and result["content"]
        ):
            return True
    return False


def _agy_memory_invocation(frames: list[dict[str, Any]], thread: str) -> bool:
    """Measured ACTIVE→DONE call_mcp_tool and its actual nmemory_context_show response."""
    # Source digest and field provenance are retained in test_probes; DONE on a
    # builtin alone is not this contract and cannot satisfy an MCP observation.
    terminals = [
        frame["result"]
        for frame in frames
        if frame.get("event") == "result"
        and isinstance(frame.get("result"), dict)
        and frame["result"].get("conversation_id") == thread
    ]
    if (
        not terminals
        or terminals[-1].get("status") != "SUCCESS"
        or terminals[-1].get("error") is not None
    ):
        return False
    calls: dict[int, dict[str, Any]] = {}
    for frame in frames:
        step = frame.get("step_update")
        if frame.get("event") != "step_update" or not isinstance(step, dict):
            continue
        if (
            step.get("conversation_id") != thread
            or step.get("step_type") != "tool"
            or step.get("tool_name") != "call_mcp_tool"
            or type(step.get("step_index")) is not int
        ):
            continue
        index = step["step_index"]
        info = step.get("tool_info")
        if not isinstance(info, dict) or info.get("name") != "call_mcp_tool":
            continue
        parameters = info.get("parameters")
        if (
            not isinstance(parameters, dict)
            or parameters.get("ServerName") != "nmemory"
            or parameters.get("ToolName") != "nmemory_context_show"
        ):
            continue
        arguments = parameters.get("Arguments")
        if (
            not isinstance(arguments, dict)
            or not isinstance(arguments.get("query"), str)
            or not isinstance(arguments.get("project_id"), str)
        ):
            continue
        if step.get("state") == "ACTIVE":
            calls[index] = arguments
            continue
        if step.get("state") == "ERROR":
            calls.pop(index, None)
            continue
        output = info.get("output")
        if (
            step.get("state") != "DONE"
            or calls.get(index) != arguments
            or info.get("error") is not None
            or not isinstance(output, str)
        ):
            continue
        try:
            response = json.loads(output)
        except json.JSONDecodeError, RecursionError:
            continue
        if (
            isinstance(response, dict)
            and response.get("label") == "ADVISORY_NOT_AUTHORITY"
            and response.get("framing") == "DATA"
            and response.get("query") == arguments["query"]
            and isinstance(response.get("scope"), dict)
            and response["scope"].get("project_id") == arguments["project_id"]
            and isinstance(response.get("results"), list)
        ):
            return True
    return False


def _has_memory_tool_invocation(transcript: dict[str, Any] | None) -> bool:
    """Correlate measured Claude/Codex/AGY calls with successful MCP results.

    A tool name, a call without a result or an unfamiliar frame cannot prove success.
    """
    if transcript is None:
        return False
    frames: list[dict[str, Any]] = []
    for line in transcript.get("lines", ()):
        try:
            payload = json.loads(line) if isinstance(line, str) else line
        except json.JSONDecodeError, TypeError:
            return False
        if not isinstance(payload, dict):
            return False
        frames.append(payload)
    status = transcript.get("status", {})
    backend = status.get("backend", "claude")
    if backend == "codex":
        thread = status.get("thread_id")
        return isinstance(thread, str) and _codex_memory_invocation(frames, thread)
    if backend == "agy":
        thread = status.get("thread_id")
        return isinstance(thread, str) and _agy_memory_invocation(frames, thread)
    if backend != "claude":
        return False
    # This exact namespace and connected-server identity were measured in
    # claude-stream-wire-2.1.251:17; the tool is the one served since the nMEMORY
    # rename. Unmeasured aliases stay refused.
    tool_name = "mcp__plugin_nott_nmemory__nmemory_context_show"
    sessions = {
        frame.get("session_id")
        for frame in frames
        if frame.get("type") == "system"
        and frame.get("subtype") == "init"
        and isinstance(frame.get("session_id"), str)
        and frame["session_id"] == status.get("thread_id")
        and tool_name in frame.get("tools", [])
        and {"name": "plugin:nott:nmemory", "status": "connected"}
        in frame.get("mcp_servers", [])
    }
    calls: set[tuple[str, str]] = set()
    for payload in frames:
        session = payload.get("session_id")
        if not isinstance(session, str) or session not in sessions:
            continue
        message = payload.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for item in content:
            if not isinstance(item, dict):
                continue
            if payload.get("type") == "assistant" and item.get("type") == "tool_use":
                name, call_id = item.get("name"), item.get("id")
                if (
                    isinstance(name, str)
                    and isinstance(call_id, str)
                    and name == tool_name
                ):
                    calls.add((session, call_id))
            if (
                payload.get("type") == "user"
                and item.get("type") == "tool_result"
                and (session, item.get("tool_use_id")) in calls
                and item.get("is_error") is False
                and item.get("content")
            ):
                return True
    return False


# -- LD-1 ----------------------------------------------------------------------


def _provider_routes(roster: Any) -> list[tuple[str, str | None, str | None]]:
    """Enumerate (provider, account_alias, model) for every provider in roster."""
    routes: list[tuple[str, str | None, str | None]] = []
    for provider in getattr(roster, "providers", ()):
        models: list[str | None] = []
        for m in getattr(provider, "models", ()):
            if m and m not in models:
                models.append(m)
        model = getattr(provider, "model", None)
        if not model and getattr(provider, "kind", "claude") == "claude":
            model = getattr(getattr(roster, "defaults", None), "model", None)
        if model and model not in models:
            models.append(model)
        model_fast = getattr(provider, "model_fast", None)
        if model_fast and model_fast not in models:
            models.append(model_fast)
        if not models:
            models = [None]

        accounts = getattr(provider, "accounts", None)
        aliases = sorted(accounts) if accounts else []
        if not aliases and getattr(provider, "secrets", None) is not None:
            aliases = provider.discovered_accounts()
            if not aliases:
                raise ObservationRefused(
                    f"provider {provider.name}: no observed account aliases"
                )
        for alias in aliases or [None]:
            for m in models:
                routes.append((provider.name, alias, m))
    return routes


def ld1(
    binary: Path,
    tmp_root: Path,
    extra_env: dict[str, str],
    log: list[str],
    route: tuple[str, str | None, str | None] | None = None,
) -> Row:
    name = "each roster route returns exactly PONG as its current final result"
    roster_path = Roster.default_path()
    if roster_path is None:
        return Row(
            "LD-1",
            name,
            BLOCKED,
            "HOME not set; there is no ~/.nott/spawn.toml to read",
        )
    try:
        roster = Roster.read(roster_path)
    except (OSError, RosterError) as error:
        return Row(
            "LD-1", name, BLOCKED, f"roster {roster_path} ilegível/malformado: {error}"
        )

    routes = [route] if route is not None else _provider_routes(roster)
    if not routes:
        return Row("LD-1", name, BLOCKED, f"roster {roster_path} declares no provider")

    blocked: list[str] = []
    red: list[str] = []
    green: list[str] = []
    observations: list[dict[str, Any]] = []
    for index, (provider, alias, model) in enumerate(routes):
        route_label = (
            f"{provider}/{alias or '<default>'}/{model or '<provider-default>'}"
        )
        cwd = tmp_root / f"ld1-route-{index:03d}"
        cwd.mkdir(parents=True, exist_ok=True)
        arguments: dict[str, Any] = {
            "provider": alias or provider,
            "prompt": "Responda exatamente: PONG",
            "cwd": str(cwd),
        }
        if model is not None:
            arguments["model"] = model
        blocked_reason, transcript, red_reason = _run_to_transcript(
            binary, cwd, extra_env, arguments, (provider, alias, model)
        )
        if blocked_reason is not None:
            blocked.append(route_label)
            log.append(f"LD-1 {route_label}: BLOCKED {blocked_reason}")
            continue
        if red_reason is not None:
            red.append(route_label)
            log.append(f"LD-1 {route_label}: RED {red_reason}")
            continue
        text = transcript["result"]["final_text"] if transcript is not None else None
        if isinstance(text, str) and text.strip() == "PONG":
            green.append(route_label)
            if transcript is not None:
                observations.append(transcript)
            log.append(f"LD-1 {route_label}: GREEN")
        else:
            red.append(route_label)
            log.append(f"LD-1 {route_label}: RED current result was not PONG")

    total = len(routes)
    if red:
        return Row(
            "LD-1",
            name,
            RED,
            f"{len(red)}/{total} rotas com achado; {len(blocked)} bloqueadas; {len(green)} verdes "
            f"(detalhe completo acima da tabela)",
        )
    if blocked:
        reasons = {
            b
            for b in (
                entry.split(": BLOCKED ", 1)[-1]
                for entry in log
                if entry.startswith("LD-1") and "BLOCKED" in entry
            )
        }
        if len(blocked) == total and len(reasons) == 1:
            return Row(
                "LD-1", name, BLOCKED, f"{total}/{total} rotas: {next(iter(reasons))}"
            )
        return Row(
            "LD-1",
            name,
            BLOCKED,
            f"{len(blocked)}/{total} rotas bloqueadas, {len(green)} verdes (detalhe completo acima da tabela)",
        )
    return Row(
        "LD-1",
        name,
        GREEN,
        f"{total}/{total} current results equal PONG",
        {"runs": observations},
    )


# -- LD-2 ------------------------------------------------------------------


def _has_readme_read(transcript: dict[str, Any] | None, target: Path) -> bool:
    if transcript is None:
        return False
    status = transcript.get("status")
    if not isinstance(status, dict) or not isinstance(status.get("thread_id"), str):
        return False
    thread = status["thread_id"]
    lines = transcript.get("lines")
    if not isinstance(lines, list):
        return False
    frames: list[dict[str, Any]] = []
    for line in lines:
        try:
            frame = json.loads(line) if isinstance(line, str) else line
        except json.JSONDecodeError, TypeError:
            return False
        if not isinstance(frame, dict):
            return False
        frames.append(frame)

    def reads(value: Any) -> bool:
        if not isinstance(value, str):
            return False
        try:
            path = Path(value)
            return (
                path if path.is_absolute() else target.parent / path
            ).resolve() == target.resolve()
        except OSError, ValueError:
            return False

    match status.get("backend"):
        case "codex":
            started: set[tuple[str, str]] = set()
            codex_consumed: set[tuple[str, str]] = set()
            codex_successful: set[tuple[str, str]] = set()
            terminal_read = False
            for frame in frames:
                params = frame.get("params")
                if not isinstance(params, dict) or params.get("threadId") != thread:
                    continue
                if frame.get("method") == "turn/completed":
                    turn = params.get("turn")
                    terminal_read = (
                        isinstance(turn, dict)
                        and isinstance(turn.get("id"), str)
                        and turn.get("status") == "completed"
                        and any(
                            turn["id"] == turn_id for turn_id, _ in codex_successful
                        )
                    )
                    continue
                turn_id, item = params.get("turnId"), params.get("item")
                if (
                    not isinstance(turn_id, str)
                    or not isinstance(item, dict)
                    or item.get("type") != "commandExecution"
                    or not isinstance(item.get("id"), str)
                ):
                    continue
                key = (turn_id, item["id"])
                if (
                    frame.get("method") == "item/started"
                    and item.get("status") == "inProgress"
                    and key not in codex_consumed
                ):
                    started.add(key)
                actions = item.get("commandActions")
                if frame.get("method") != "item/completed":
                    continue
                was_started = key in started
                started.discard(key)
                if key in codex_consumed:
                    continue
                codex_consumed.add(key)
                if (
                    was_started
                    and item.get("status") == "completed"
                    and item.get("exitCode") == 0
                    and item.get("error") is None
                    and isinstance(actions, list)
                    and any(
                        isinstance(action, dict)
                        and action.get("type") == "read"
                        and reads(action.get("path"))
                        for action in actions
                    )
                ):
                    codex_successful.add(key)
                else:
                    codex_successful.discard(key)
            return terminal_read
        case "agy":
            agy_started: set[int] = set()
            agy_consumed: set[int] = set()
            agy_successful: set[int] = set()
            for frame in frames:
                step = frame.get("step_update")
                result = frame.get("result")
                if (
                    frame.get("event") == "result"
                    and isinstance(result, dict)
                    and result.get("conversation_id") == thread
                ):
                    return (
                        result.get("status") == "SUCCESS"
                        and result.get("error") is None
                        and bool(agy_successful)
                    )
                if (
                    frame.get("event") != "step_update"
                    or not isinstance(step, dict)
                    or step.get("conversation_id") != thread
                    or type(step.get("step_index")) is not int
                ):
                    continue
                index = step["step_index"]
                if step.get("state") == "ERROR":
                    agy_started.discard(index)
                    agy_successful.discard(index)
                    agy_consumed.add(index)
                    continue
                terminal = step.get("state") == "DONE"
                was_started = index in agy_started
                if terminal:
                    agy_started.discard(index)
                    if index in agy_consumed:
                        continue
                    agy_consumed.add(index)
                if (
                    step.get("step_type") != "tool"
                    or step.get("tool_name") != "view_file"
                ):
                    continue
                info = step.get("tool_info")
                parameters = info.get("parameters") if isinstance(info, dict) else None
                if (
                    not isinstance(info, dict)
                    or info.get("name") != "view_file"
                    or not isinstance(parameters, dict)
                    or not reads(parameters.get("AbsolutePath"))
                ):
                    continue
                if (
                    not terminal
                    and step.get("state") == "ACTIVE"
                    and index not in agy_consumed
                ):
                    agy_started.add(index)
                    agy_successful.discard(index)
                elif terminal:
                    if was_started and info.get("error") is None:
                        agy_successful.add(index)
                    else:
                        agy_successful.discard(index)
            return False
        case "claude":
            initialized = False
            calls: set[str] = set()
            claude_consumed: set[str] = set()
            for frame in frames:
                tools = frame.get("tools")
                if (
                    frame.get("type") == "system"
                    and frame.get("subtype") == "init"
                    and frame.get("session_id") == thread
                    and isinstance(tools, list)
                    and "Read" in tools
                ):
                    initialized = True
                    calls.clear()
                    continue
                message = frame.get("message")
                content = message.get("content") if isinstance(message, dict) else None
                if (
                    not initialized
                    or frame.get("session_id") != thread
                    or not isinstance(content, list)
                ):
                    continue
                if frame.get("type") == "assistant":
                    calls.update(
                        item["id"]
                        for item in content
                        if isinstance(item, dict)
                        and item.get("type") == "tool_use"
                        and item.get("name") == "Read"
                        and isinstance(item.get("id"), str)
                        and item["id"] not in claude_consumed
                        and isinstance(item.get("input"), dict)
                        and reads(item["input"].get("file_path"))
                    )
                elif frame.get("type") == "user":
                    for item in content:
                        if (
                            not isinstance(item, dict)
                            or item.get("type") != "tool_result"
                            or not isinstance(item.get("tool_use_id"), str)
                        ):
                            continue
                        call_id = item["tool_use_id"]
                        pending = call_id in calls
                        calls.discard(call_id)
                        if call_id in claude_consumed:
                            continue
                        claude_consumed.add(call_id)
                        if (
                            pending
                            and item.get("is_error") is False
                            and bool(item.get("content"))
                        ):
                            return True
            return False
        case _:
            return False


def ld2(
    binary: Path,
    tmp_root: Path,
    extra_env: dict[str, str],
    repo_root: Path,
    route: tuple[str, str | None, str | None] | None = None,
) -> Row:
    prompt = (
        "Read README.md from the current directory using an actual file-reading "
        "operation. Return its first line verbatim, preserving every character, "
        "including existing Markdown punctuation. Output only that line; add no "
        "explanation, quotes, or code fences."
    )
    name = "nspawn_run_start(cwd=repo, exact README first line) bate com o disco"
    expected = (
        repo_root.joinpath("README.md").read_bytes().splitlines()[0].decode("utf-8")
    )
    run_root = tmp_root / "ld2"
    run_root.mkdir(parents=True, exist_ok=True)
    arguments: dict[str, Any] = {
        "cwd": str(repo_root),
        "prompt": prompt,
    }
    if route is not None:
        provider, alias, model = route
        arguments["provider"] = alias or provider
        if model is not None:
            arguments["model"] = model
    blocked_reason, transcript, red_reason = _run_to_transcript(
        binary,
        run_root,
        extra_env,
        arguments,
        route,
    )
    if blocked_reason is not None:
        return Row("LD-2", name, BLOCKED, blocked_reason)
    if red_reason is not None:
        return Row("LD-2", name, RED, red_reason)
    readme = repo_root / "README.md"
    if not _has_readme_read(transcript, readme):
        return Row(
            "LD-2",
            name,
            BLOCKED,
            "no correlated successful README read in supported provider wire",
            transcript,
        )
    text = transcript["result"]["final_text"] if transcript is not None else None
    observed = (
        text[:-2]
        if isinstance(text, str) and text.endswith("\r\n")
        else text[:-1]
        if isinstance(text, str) and text.endswith("\n")
        else text
    )
    if observed == expected:
        return Row(
            "LD-2",
            name,
            GREEN,
            "current final result matches README first line",
            transcript,
        )
    return Row(
        "LD-2",
        name,
        RED,
        "current result differs from README first line",
        transcript,
    )


# -- LD-3 ------------------------------------------------------------------


def _says_ok(written: bytes) -> bool:
    """Does the file SAY OK? The row asks for text, so it is read as text.

        Comparing the raw bytes to `b"OK"` reds a file that says exactly what LD-3
        asked for: measured 2026-09-02 as nott-exec-1, the real child
        wrote `OK
    ` and the trailing newline alone moved the row to RED.
    """
    return written.decode("utf-8", "replace").strip() == "OK"


#: The variable that names where LD-3 makes its clone. nSPAWN refuses a native
#: write outside a `[native_write]` root of the roster, so it must name one.
WRITE_ROOT_ENV = "NOTT_NSPAWN_MATRIX_WRITE_ROOT"
LD3_NEEDS_WRITE_ROOT = (
    f"{WRITE_ROOT_ENV} is unset. Set it to a [native_write] root of the roster."
)


def ld3(
    binary: Path,
    tmp_root: Path,
    extra_env: dict[str, str],
    repo_root: Path,
    route: tuple[str, str | None, str | None] | None = None,
) -> Row:
    name = "nspawn_run_start(native_write, cwd=clone, 'write IT_WORKS.md with the text OK') and the file exists with OK"
    write_root = os.environ.get(WRITE_ROOT_ENV)
    if not write_root:
        return Row("LD-3", name, BLOCKED, LD3_NEEDS_WRITE_ROOT)
    run_root = tmp_root / "ld3"
    run_root.mkdir(parents=True, exist_ok=True)
    # A disposable standalone clone: a native write needs cwd/.git as a directory.
    try:
        held = tempfile.mkdtemp(prefix="nspawn-matrix-ld3-", dir=write_root)
    except OSError as error:
        return Row("LD-3", name, BLOCKED, f"{WRITE_ROOT_ENV} is not usable: {error}")
    try:
        cwd = Path(held) / "clone"
        try:
            subprocess.run(
                ["git", "clone", "--quiet", str(repo_root), str(cwd)],
                check=True,
                capture_output=True,
                timeout=EXECUTE_TIMEOUT,
            )
        except (OSError, subprocess.SubprocessError) as error:
            row = Row("LD-3", name, BLOCKED, f"git clone failed: {error}")
        else:
            row = _ld3_in(binary, run_root, extra_env, cwd, name, route)
    finally:
        try:
            shutil.rmtree(held)
        except OSError as error:
            cleanup = f"; cleanup of {held} failed: {error}"
        else:
            cleanup = ""
    return replace(row, detail=row.detail + cleanup)


def _ld3_in(
    binary: Path,
    run_root: Path,
    extra_env: dict[str, str],
    cwd: Path,
    name: str,
    route: tuple[str, str | None, str | None] | None,
) -> Row:
    output = cwd / "IT_WORKS.md"
    arguments: dict[str, Any] = {
        "cwd": str(cwd),
        "prompt": (
            "Use an actual file-writing operation to create IT_WORKS.md in the "
            "current directory with the content OK. Do not merely report or "
            "suggest the content."
        ),
        "read_only": False,
        "native_write": True,
    }
    if route is not None:
        provider, alias, model = route
        arguments["provider"] = alias or provider
        if model is not None:
            arguments["model"] = model
    blocked_reason, _transcript, red_reason = _run_to_transcript(
        binary,
        run_root,
        extra_env,
        arguments,
        route,
    )
    if blocked_reason is not None:
        return Row("LD-3", name, BLOCKED, blocked_reason)
    if red_reason is not None:
        return Row("LD-3", name, RED, red_reason)
    if not output.is_file():
        return Row("LD-3", name, RED, f"{output} does not exist after the terminal run")
    written = output.read_bytes()
    if _says_ok(written):
        return Row(
            "LD-3",
            name,
            GREEN,
            "created file contains OK",
            {"run": _transcript, "file_sha256": hashlib.sha256(written).hexdigest()},
        )
    return Row("LD-3", name, RED, f"{output} contém {written!r}, esperado OK")


# -- LD-4 ------------------------------------------------------------------


def ld4(
    binary: Path,
    tmp_root: Path,
    extra_env: dict[str, str],
    repo_root: Path,
    route: tuple[str, str | None, str | None] | None = None,
) -> Row:
    name = "child performs a correlated successful nMEMORY nmemory_context_show call"
    run_root = tmp_root / "ld4"
    run_root.mkdir(parents=True, exist_ok=True)
    arguments: dict[str, Any] = {
        "cwd": str(repo_root),
        "prompt": (
            "Execute the registered nMEMORY nmemory_context_show tool with query "
            "nspawn matrix isolated fixture and project_id pynott. "
            "Report the tool result; do not list tool names instead."
        ),
    }
    if route is not None:
        provider, alias, model = route
        arguments["provider"] = alias or provider
        if model is not None:
            arguments["model"] = model
    blocked_reason, transcript, red_reason = _run_to_transcript(
        binary,
        run_root,
        extra_env,
        arguments,
        route,
    )
    if blocked_reason is not None:
        return Row("LD-4", name, BLOCKED, blocked_reason)
    if red_reason is not None:
        return Row("LD-4", name, RED, red_reason)
    if _has_memory_tool_invocation(transcript):
        return Row(
            "LD-4",
            name,
            GREEN,
            "correlated successful nmemory_context_show operation",
            transcript,
        )
    return Row(
        "LD-4",
        name,
        BLOCKED,
        "no correlated successful nmemory_context_show result in supported wire",
    )


# -- table + exit code -------------------------------------------------------


def overall_exit_code(rows: list[Row], operational_error: str | None) -> int:
    """D18: 0 green, 3 findings, 2 refusal/BLOCKED, 1 operational failure.

    Aggregation order across rows is 1 beats 2 beats 3 beats 0.
    """
    if operational_error is not None:
        return 1
    present = {_STATUS_TO_EXIT[row.status] for row in rows}
    for code in (1, 2, 3, 0):
        if code in present:
            return code
    return 0


def print_table(rows: list[Row]) -> None:
    id_width = max(len(row.row_id) for row in rows)
    status_width = max(len(row.status) for row in rows)
    for row in rows:
        print(
            f"{row.row_id:<{id_width}}  {row.status:<{status_width}}  {row.observation}"
        )
        print(f"{'':<{id_width}}  {'':<{status_width}}  -> {row.detail}")


def _candidate_sha(repo: Path) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
    ).strip()


def _artifact(path: Path) -> dict[str, str]:
    return {
        "path": str(path.resolve()),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def _route_row_id(route: tuple[str, str | None, str | None], phase: str) -> str:
    provider, alias, model = route
    coordinate = "/".join(
        quote(value, safe="")
        for value in (provider, alias or "<default>", model or "<provider-default>")
    )
    return f"route:{coordinate}/{phase}"


def _selected_row_ids(
    routes: list[tuple[str, str | None, str | None]], requested: list[str] | None
) -> set[str]:
    available = {"LD-0"} | {
        _route_row_id(route, f"LD-{index}") for route in routes for index in range(1, 5)
    }
    if requested is None:
        return available
    if len(requested) != len(set(requested)):
        raise ObservationRefused("selected matrix rows are duplicated")
    unknown = set(requested) - available
    if unknown:
        raise ObservationRefused(f"selected matrix rows are unknown: {sorted(unknown)}")
    return set(requested)


def _accepted_inputs(
    manifest: Path, owning: list[dict[str, str]], row_ids: set[str]
) -> list[dict[str, str]]:
    """Consume the independently bound denominator; never manufacture input roles."""
    document = json.loads(manifest.read_text())
    if (
        not isinstance(document, dict)
        or document.get("schema") != "nott.completion.requirements.v1"
    ):
        raise ObservationRefused("input manifest has no accepted requirements schema")
    inputs, requirements = document.get("inputs"), document.get("requirements")
    if not isinstance(inputs, list) or not inputs or not isinstance(requirements, list):
        raise ObservationRefused("input manifest omits frozen inputs or rows")
    frozen: dict[str, dict[str, str]] = {}
    for item in inputs:
        if (
            not isinstance(item, dict)
            or set(item) != {"name", "path", "sha256"}
            or any(not isinstance(value, str) or not value for value in item.values())
            or item["name"] in frozen
        ):
            raise ObservationRefused("input manifest has malformed or duplicate inputs")
        if not Path(item["path"]).is_absolute():
            raise ObservationRefused("input manifest paths must be absolute")
        frozen[item["name"]] = item
    declared: dict[str, list[str]] = {}
    for row in requirements:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("id"), str)
            or row["id"] in declared
        ):
            raise ObservationRefused("input manifest has malformed or duplicate rows")
        names = row.get("inputs", list(frozen))
        if (
            not isinstance(names, list)
            or any(not isinstance(name, str) or name not in frozen for name in names)
            or len(names) != len(set(names))
        ):
            raise ObservationRefused(
                "input manifest row names unknown or duplicate inputs"
            )
        declared[row["id"]] = names
    if not row_ids.issubset(declared):
        raise ObservationRefused("matrix rows are absent from accepted denominator")
    applicable = {name for row_id in row_ids for name in declared[row_id]}
    for actual in owning:
        if actual["name"] not in applicable or frozen.get(actual["name"]) != actual:
            raise ObservationRefused(
                f"accepted {actual['name']} differs from actual matrix input"
            )
    selected = [frozen[name] for name in sorted(applicable)]
    for item in selected:
        if _artifact(Path(item["path"])) != {
            key: item[key] for key in ("path", "sha256")
        }:
            raise ObservationRefused(
                "accepted input content differs from frozen digest"
            )
    return selected


def _write_evidence(
    directory: Path,
    candidate: str,
    started: str,
    ended: str,
    code: int,
    rows: list[Row],
    inputs: list[dict[str, str]],
    input_manifest: dict[str, str],
) -> None:
    states = {GREEN: "pass", RED: "fail", BLOCKED: "refused"}
    output = []
    for index, row in enumerate(rows):
        path = directory / f"row-{index:04d}.json"
        payload = {
            "id": row.row_id,
            "status": states[row.status],
            "detail": row.detail,
            "observation": row.evidence,
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
        output.append(
            {
                "id": row.row_id,
                "boundary": "component-validator"
                if row.row_id == "LD-0"
                else "real-vendor-consumer",
                "status": states[row.status],
                "artifacts": [_artifact(path)],
            }
        )
    envelope = {
        "schema": "nott.completion.observations.v1",
        "candidate_sha": candidate,
        "run_id": str(uuid4()),
        "started_at": started,
        "ended_at": ended,
        "exit": code,
        "inputs": inputs,
        "rows": output,
        "input_manifest": input_manifest,
    }
    (directory / "observations.json").write_text(
        json.dumps(envelope, sort_keys=True) + "\n"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True, type=Path)
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument(
        "--evidence-dir",
        type=Path,
        help="New, external run directory for bound observation artifacts",
    )
    parser.add_argument(
        "--input-manifest",
        type=Path,
        help="Accepted requirements.v1 inputs; required with --evidence-dir",
    )
    parser.add_argument(
        "--row",
        action="append",
        help="Run one exact denominator row; repeat for additional uncovered rows",
    )
    args = parser.parse_args(argv)
    store = os.environ.get("NOTT_DATABASE_DIR", "")
    if not store or os.path.realpath(store) == os.path.realpath(
        nott_database.live_directory()
    ):
        print(
            "REFUSED: NOTT_DATABASE_DIR must name a disposable store directory,"
            " not be unset or the live store",
            file=sys.stderr,
        )
        return 2
    if (args.evidence_dir is None) != (args.input_manifest is None):
        print("REFUSED: --evidence-dir requires --input-manifest", file=sys.stderr)
        return 2

    binary: Path = args.binary
    repo_root: Path = args.repo_root
    if not binary.is_file() or not os.access(binary, os.X_OK):
        print(
            f"OPERATIONAL: {binary} does not exist or is not executable",
            file=sys.stderr,
        )
        return 1
    if not (repo_root / "README.md").is_file():
        print(
            f"OPERATIONAL: {repo_root}/README.md does not exist; wrong repo_root?",
            file=sys.stderr,
        )
        return 1

    selected_routes: list[tuple[str, str | None, str | None]] | None = None
    selected_rows: set[str] | None = None
    if args.row is not None:
        try:
            roster_path = Roster.default_path()
            if roster_path is None:
                raise ObservationRefused("no configured roster")
            selected_routes = _provider_routes(Roster.read(roster_path))
            if not selected_routes or len(selected_routes) != len(set(selected_routes)):
                raise ObservationRefused("empty or duplicate route inventory")
            selected_rows = _selected_row_ids(selected_routes, args.row)
        except (OSError, RosterError, ObservationRefused) as error:
            print(f"REFUSED: selected matrix rows: {error}", file=sys.stderr)
            return 2

    started = datetime.now(UTC).isoformat()
    candidate = ""
    inputs: list[dict[str, str]] = []
    input_manifest: dict[str, str] = {}
    expected_rows: set[str] = set()
    if args.evidence_dir is not None:
        directory = args.evidence_dir.resolve()
        if directory.is_relative_to(repo_root.resolve()):
            print(
                "REFUSED: evidence directory must be outside the candidate",
                file=sys.stderr,
            )
            return 2
        candidate = _candidate_sha(repo_root)
        roster_path = Roster.default_path()
        if roster_path is None:
            print("REFUSED: no roster input for evidence", file=sys.stderr)
            return 2
        inputs = [
            {"name": name, **_artifact(path)}
            for name, path in (
                ("matrix", Path(__file__)),
                ("binary", binary),
                ("roster", roster_path),
            )
        ]
        try:
            input_manifest = _artifact(args.input_manifest)
            inventory = selected_routes or _provider_routes(Roster.read(roster_path))
            expected_rows = selected_rows or _selected_row_ids(inventory, None)
            inputs = _accepted_inputs(args.input_manifest, inputs, expected_rows)
        except (OSError, ValueError, RosterError, ObservationRefused) as error:
            print(f"REFUSED: accepted matrix inputs: {error}", file=sys.stderr)
            return 2
        directory.mkdir(parents=True, exist_ok=False, mode=0o700)
    extra_env = _bridge_env()
    log: list[str] = []
    with tempfile.TemporaryDirectory(prefix="nspawn-matrix-") as tmp_root_name:
        tmp_root = Path(tmp_root_name)
        rows = (
            [ld0(binary, tmp_root, extra_env)]
            if selected_rows is None or "LD-0" in selected_rows
            else []
        )
        try:
            if selected_routes is None:
                roster_path = Roster.default_path()
                if roster_path is None:
                    raise ObservationRefused("no configured roster")
                routes = _provider_routes(Roster.read(roster_path))
            else:
                routes = selected_routes
            if not routes or len(routes) != len(set(routes)):
                raise ObservationRefused("empty or duplicate route inventory")
        except (OSError, RosterError, ObservationRefused) as error:
            rows.append(Row("routes", "complete route inventory", BLOCKED, str(error)))
            routes = []
        for index, route in enumerate(routes):
            root = tmp_root / f"route-{index:03d}"
            root.mkdir()
            calls = (
                partial(ld1, binary, root, extra_env, log, route),
                partial(ld2, binary, root, extra_env, repo_root, route),
                partial(ld3, binary, root, extra_env, repo_root, route),
                partial(ld4, binary, root, extra_env, repo_root, route),
            )
            for phase, call in enumerate(calls, 1):
                key = _route_row_id(route, f"LD-{phase}")
                if selected_rows is not None and key not in selected_rows:
                    continue
                row = call()
                key = _route_row_id(route, row.row_id)
                rows.append(
                    Row(key, row.observation, row.status, row.detail, row.evidence)
                )

    if log:
        print("-- LD-1 detalhe por rota --")
        for line in log:
            print(line)
        print()

    print_table(rows)
    code = overall_exit_code(rows, operational_error=None)
    if args.evidence_dir is not None:
        if (
            {row.row_id for row in rows} != expected_rows
            or len(rows) != len(expected_rows)
            or _artifact(args.input_manifest) != input_manifest
            or _candidate_sha(repo_root) != candidate
        ) or any(
            _artifact(Path(item["path"]))["sha256"] != item["sha256"] for item in inputs
        ):
            print("REFUSED: observation inputs changed during run", file=sys.stderr)
            return 2
        _write_evidence(
            directory,
            candidate,
            started,
            datetime.now(UTC).isoformat(),
            code,
            rows,
            inputs,
            input_manifest,
        )
    return code


def _selftest() -> None:
    assert _last_stderr_line("", 1) == "processo saiu sem stderr (rc=1)"
    assert (
        _last_stderr_line("a\nb\nNotImplementedError: x\n", 1)
        == "NotImplementedError: x"
    )
    assert overall_exit_code([Row("A", "n", GREEN, "")], None) == 0
    assert (
        overall_exit_code([Row("A", "n", GREEN, ""), Row("B", "n", BLOCKED, "")], None)
        == 2
    )
    assert (
        overall_exit_code([Row("A", "n", RED, ""), Row("B", "n", BLOCKED, "")], None)
        == 2
    )
    assert overall_exit_code([Row("A", "n", RED, "")], None) == 3
    assert overall_exit_code([], "uv sync falhou") == 1
    print("selftest ok")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        _selftest()
        raise SystemExit(0)
    raise SystemExit(main())
