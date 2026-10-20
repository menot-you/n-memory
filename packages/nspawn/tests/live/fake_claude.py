#!/usr/bin/env python3
"""Deterministic double of the observed Claude 2.1.241 wire."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

FIXTURE_PATH = (
    Path(__file__).resolve().parent.parent
    / "fixtures"
    / "claude-stream-wire-2.1.241.ndjson"
)
README_FIRST_LINE_PROMPT = (
    "Read README.md from the current directory using an actual file-reading "
    "operation. Return its first line verbatim, preserving every character, "
    "including existing Markdown punctuation. Output only that line; add no "
    "explanation, quotes, or code fences."
)
LONG_PROMPT_PREFIX = "SLEEP_SECONDS="
MODE_ENV = "NOTT_NSPAWN_FAKE_CLAUDE_MODE"
READY_FILE_ENV = "NOTT_NSPAWN_FAKE_READY_FILE"
EMPTY_STDOUT_ERROR = {
    "error": {
        "type": "rate_limit_error",
        "message": (
            "All credentials for model claude-opus-5 are cooling down via "
            "provider claude"
        ),
    }
}


def _option_values(flag: str) -> tuple[str, ...]:
    values: list[str] = []
    arguments = iter(sys.argv[1:])
    for argument in arguments:
        if argument == flag:
            value = next(arguments, None)
            if value is not None:
                values.append(value)
    return tuple(values)


def _extract_prompt(frame: Any) -> str | None:
    if not isinstance(frame, dict):
        return None
    message = frame.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    text = [
        item["text"]
        for item in content
        if isinstance(item, dict)
        and item.get("type") == "text"
        and isinstance(item.get("text"), str)
    ]
    return "".join(text)


def _read_prompt() -> str:
    if "--input-format" in sys.argv:
        raw = sys.stdin.buffer.readline()
        if not raw:
            return ""
        try:
            frame = json.loads(raw)
        except json.JSONDecodeError as error:
            raise ValueError(
                "stdin does not carry a valid stream-json frame"
            ) from error
        return _extract_prompt(frame) or ""
    if "--" in sys.argv:
        separator = sys.argv.index("--")
        trailing = sys.argv[separator + 1 :]
        return trailing[0] if trailing else ""
    # `claude -p` with no prompt argument reads it from stdin: the argv
    # composer's contract since prompts outgrew MAX_ARG_STRLEN.
    if not sys.stdin.isatty():
        piped = sys.stdin.read()
        if piped:
            return piped
    positionals = [
        argument for argument in sys.argv[1:] if not argument.startswith("-")
    ]
    return positionals[-1] if positionals else ""


def _replace_fixture_value(value: Any, replacement: str, cwd: str) -> Any:
    if isinstance(value, str):
        return value.replace("fixture_test", replacement).replace(
            "/tmp/claude_fixture_record", cwd
        )
    if isinstance(value, list):
        return [_replace_fixture_value(item, replacement, cwd) for item in value]
    if isinstance(value, dict):
        return {
            key: _replace_fixture_value(item, replacement, cwd)
            for key, item in value.items()
        }
    return value


def _emit_measured_wire(replacement: str) -> None:
    for raw in FIXTURE_PATH.read_text(encoding="utf-8").splitlines():
        if not raw.startswith("{"):
            continue
        value = json.loads(raw)
        rendered = _replace_fixture_value(value, replacement, os.getcwd())
        sys.stdout.write(json.dumps(rendered, separators=(",", ":")) + "\n")
        sys.stdout.flush()


def _expand_config_value(value: str, environment: dict[str, str]) -> str:
    expanded = value
    for name, replacement in environment.items():
        expanded = expanded.replace(f"${{{name}}}", replacement)
    if "${" in expanded:
        raise ValueError("the MCP configuration carries a missing environment variable")
    return os.path.expanduser(expanded)


def _run_memory_context() -> str:
    plugin_dirs = _option_values("--plugin-dir")
    configs = [path / ".mcp.json" for path in map(Path, plugin_dirs)]
    config_path = next((path for path in configs if path.is_file()), None)
    if config_path is None:
        raise RuntimeError(
            "the child did not receive a plugin with an MCP configuration"
        )
    config = json.loads(config_path.read_text(encoding="utf-8"))
    servers = config.get("mcpServers")
    if not isinstance(servers, dict) or not isinstance(servers.get("nmemory"), dict):
        raise TypeError("the child's configuration does not expose nmemory")
    declaration = servers["nmemory"]
    environment = dict(os.environ)
    configured_env = declaration.get("env", {})
    if not isinstance(configured_env, dict):
        raise TypeError("the nmemory MCP environment is not an object")
    for name, value in configured_env.items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise TypeError("the nmemory MCP environment is not text")
        environment[name] = _expand_config_value(value, environment)
    command = declaration.get("command")
    arguments = declaration.get("args", [])
    if not isinstance(command, str) or not isinstance(arguments, list):
        raise TypeError("o comando MCP de nmemory é inválido")
    if not all(isinstance(argument, str) for argument in arguments):
        raise TypeError("the nmemory MCP arguments are not text")
    argv = [
        _expand_config_value(command, environment),
        *[_expand_config_value(argument, environment) for argument in arguments],
    ]
    frames = (
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "nspawn-live-fake", "version": "1"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "nmemory_context_show",
                "arguments": {
                    "query": "nspawn live child memory operation",
                    "project_id": "pynott",
                    "sources": ["memory"],
                    "limit": 1,
                    "token_budget": 128,
                },
            },
        },
    )
    # stdin stays open until the answer: the MCP SDK cancels whatever is
    # unanswered when its stdin closes, and a real client keeps the pipe open.
    responses: list[dict[str, Any]] = []
    try:
        with subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=environment,
        ) as server:
            assert server.stdin is not None and server.stdout is not None
            timer = threading.Timer(30, server.kill)
            timer.start()
            try:
                for frame in frames:
                    server.stdin.write(
                        json.dumps(frame, separators=(",", ":")).encode("utf-8") + b"\n"
                    )
                    server.stdin.flush()
                for line in server.stdout:
                    try:
                        decoded = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(decoded, dict):
                        responses.append(decoded)
                        if decoded.get("id") == 2:
                            break
                server.stdin.close()
                returncode = server.wait()
            finally:
                timer.cancel()
    except FileNotFoundError as error:
        raise RuntimeError("the nmemory MCP operation could not run") from error
    operation = next((frame for frame in responses if frame.get("id") == 2), None)
    if returncode != 0 or operation is None or "error" in operation:
        raise RuntimeError(
            "nmemory_context_show did not answer successfully through the real MCP"
        )
    return "NMEMORY_OPERATION_OK"


def _spawn_grandchild(prompt: str) -> None:
    marker = "SPAWN_GRANDCHILD_IDENTITY_FILE="
    identity_path = Path(prompt.split(marker, 1)[1].strip())
    grandchild = subprocess.Popen(
        [sys.executable, "-c", "import signal; signal.pause()"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=False,
    )
    temporary = identity_path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps({"parent_pid": os.getpid(), "grandchild_pid": grandchild.pid}),
        encoding="utf-8",
    )
    temporary.replace(identity_path)
    _emit_measured_wire("PROCESS_TREE_RUNNING")
    signal.pause()


def _scenario_result(prompt: str) -> str:
    if prompt == "Responda exatamente: PONG":
        return "PONG"
    # FOURTH LITERAL COPY of the LD-2 prompt — `test_contract_red.py`,
    # `matrix_probe.py` and `test_probes.py` are the other three. 4720cd68 moved
    # those three and left this one behind: the double fell through to the final
    # `return "fixture_test"` and LD-2, which requires the first line of the README,
    # turned red without anything on the wire indicating why. EXACT match, never
    # `"README" in prompt` — a fuzzy double would respond to the wrong prompt and
    # be a weaker oracle than what is being repaired.
    if prompt == README_FIRST_LINE_PROMPT:
        return Path("README.md").read_bytes().splitlines()[0].decode("utf-8")
    if prompt == "write IT_WORKS.md with the text OK":
        Path("IT_WORKS.md").write_bytes(b"OK")
        return "OK"
    if prompt == "use nMEMORY and run a nmemory_context_show operation":
        return _run_memory_context()
    if "SPAWN_GRANDCHILD_IDENTITY_FILE=" in prompt:
        _spawn_grandchild(prompt)
    if LONG_PROMPT_PREFIX in prompt:
        # The long mode: a run that outlives its client by a measured margin.
        time.sleep(float(prompt.split(LONG_PROMPT_PREFIX, 1)[1].split()[0]))
        return "SLEPT"
    return "fixture_test"


def main() -> int:
    if "--version" in sys.argv:
        sys.stdout.write("2.1.241 (Claude Code)\n")
        return 0
    mode = os.environ.get(MODE_ENV, "terminal_clean")
    if mode == "empty_stdout_stderr_error":
        sys.stderr.write(json.dumps(EMPTY_STDOUT_ERROR, separators=(",", ":")) + "\n")
        return 0
    if mode == "signal":
        ready = os.environ.get(READY_FILE_ENV)
        if ready is not None:
            Path(ready).write_text(str(os.getpid()), encoding="utf-8")
        signal.pause()
    if (
        "--output-format" in sys.argv
        and "stream-json" in sys.argv
        and "--verbose" not in sys.argv
    ):
        sys.stderr.write(
            "Error: When using --print, --output-format=stream-json requires "
            "--verbose\n"
        )
        return 1
    try:
        prompt = _read_prompt()
        result = _scenario_result(prompt)
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as error:
        sys.stderr.write(f"fake_claude refusal: {error}\n")
        return 1
    _emit_measured_wire(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
