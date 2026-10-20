"""8-way stdio concurrency probe.

Tests that eight stdio MCP server processes started at once on one fresh
store file all open it, answering both initialize and the first tool call:
no child dies of a lock another child holds while opening.
"""

from __future__ import annotations

import json
import os
import statistics
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from stdio_peer import exchange
from storefile import default_store_path
from workspace_root import workspace_path

# The tree's package, also under mutmut: the copy's modules load mutmut's
# configuration from the working directory, and this child starts elsewhere.
_MEMBER_ROOT = str(workspace_path("mcps/nmemory"))

MESSAGES: list[dict[str, Any]] = [
    {"method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
    {
        "method": "tools/call",
        "params": {"name": "nmemory_context_show", "arguments": {"query": "probe"}},
    },
]

FRAMES = [{"id": i, **m} for i, m in enumerate(MESSAGES, start=1)]


@dataclass(slots=True)
class ChildResult:
    index: int
    lines: list[str]
    elapsed_ms: float | None
    stderr: str
    returncode: int | None


def _answer_reason(res: dict[str, Any], msg_id: int) -> str | None:
    """None when res is a REAL ANSWER for its reply kind, else the reason.

    The question is not "is this shaped like a reply" — five review rounds
    each found one more shaped non-answer — but "does this carry what a
    genuine answer of its kind carries". initialize answers a non-empty
    protocolVersion string. A tools/call answer carries a non-empty content
    list of well-formed blocks, at least one text block, EVERY text block
    carrying a string in text, and isError exactly false — what verb_result
    emits on every success.
    """
    if msg_id == 1:
        version = res.get("protocolVersion")
        if not isinstance(version, str) or not version:
            return f"initialize answer carries no protocolVersion string: {version!r}"
        return None
    # The PR318-settled result stage, one conjunction: verb_result answers
    # every tools/call success with an explicit isError: false, so null
    # results, error results, mangled flags and dropped flags all die here.
    if res.get("isError") is not False:
        return (
            f"tools/call answer does not carry isError: false: {res.get('isError')!r}"
        )
    content = res.get("content")
    if not isinstance(content, list) or not content:
        return f"tools/call answer carries no content list: {content!r}"
    if any(not isinstance(block, dict) for block in content):
        return f"content carries a malformed block: {content!r}"
    text_blocks = [b for b in content if b.get("type") == "text"]
    if not text_blocks:
        return f"no text block in content: {content!r}"
    if not all(isinstance(b.get("text"), str) for b in text_blocks):
        return f"a text block carries no string in text: {content!r}"
    return None


def _validate_child(r: ChildResult) -> tuple[bool, str]:
    if r.returncode != 0:
        return False, f"nonzero returncode {r.returncode}"
    seen_ids: set[int] = set()
    for raw_line in r.lines:
        line = raw_line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            return False, f"malformed JSON line: {line!r}"
        if not isinstance(msg, dict):
            return False, f"expected JSON object, got {type(msg).__name__}"
        if msg.get("jsonrpc") != "2.0":
            return False, f"missing/wrong jsonrpc field: {msg.get('jsonrpc')!r}"
        if "error" in msg:
            return False, f"JSON-RPC error in response: {msg.get('error')!r}"
        msg_id = msg.get("id")
        if type(msg_id) is not int or msg_id not in (1, 2):
            return False, f"unexpected JSON-RPC id: {msg_id!r}"
        if msg_id in seen_ids:
            return False, f"duplicate JSON-RPC id: {msg_id}"
        if "result" not in msg:
            return False, f"missing result key for id {msg_id}"
        res = msg["result"]
        # initialize and tools/call both answer with a result OBJECT; a null
        # or scalar result is not a usable MCP reply.
        if not isinstance(res, dict):
            return False, f"unusable result for id {msg_id}: {res!r}"
        reason = _answer_reason(res, msg_id)
        if reason is not None:
            return False, reason
        seen_ids.add(msg_id)
    if 1 not in seen_ids or 2 not in seen_ids:
        missing = [i for i in (1, 2) if i not in seen_ids]
        return False, f"missing responses for ids: {missing}"
    return True, ""


def _run_one_child(
    index: int,
    argv: list[str],
    child_env: dict[str, str],
    cwd: Path,
    barrier: threading.Barrier | None = None,
    timeout: float = 60.0,
) -> ChildResult:
    if barrier is not None:
        barrier.wait()
    t0 = time.perf_counter()
    try:
        answers, errors, code = exchange(
            argv, FRAMES, deadline_s=timeout, env=child_env, cwd=cwd
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        return ChildResult(
            index=index,
            lines=[json.dumps(answer) for answer in answers],
            elapsed_ms=elapsed_ms,
            stderr=errors,
            returncode=code,
        )
    except subprocess.TimeoutExpired as exc:
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        stderr = (
            exc.stderr
            if isinstance(exc.stderr, str)
            else (exc.stderr.decode() if exc.stderr else "")
        )
        stderr = (stderr + "\n" if stderr else "") + f"child timed out after {timeout}s"
        return ChildResult(
            index=index,
            lines=[],
            elapsed_ms=elapsed_ms,
            stderr=stderr,
            returncode=-1,
        )
    except OSError as e:
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        return ChildResult(
            index=index,
            lines=[],
            elapsed_ms=elapsed_ms,
            stderr=str(e),
            returncode=-1,
        )


def run_probe(
    argv: list[str],
    env: dict[str, str],
    home_dir: Path,
    num_children: int = 8,
    timeout: float = 60.0,
) -> tuple[int, str]:
    """Run the N-way probe, print the metrics line and error stderrs, return (answers, summary)."""
    barrier = threading.Barrier(num_children)
    with ThreadPoolExecutor(max_workers=num_children) as pool:
        futures = [
            pool.submit(
                _run_one_child,
                i,
                argv,
                env,
                home_dir,
                barrier=barrier,
                timeout=timeout,
            )
            for i in range(num_children)
        ]
        results = [f.result() for f in futures]

    evaluations = [(r, *_validate_child(r)) for r in results]
    total_answers = sum(1 for _, ok, _ in evaluations if ok)

    elapsed_latencies = [r.elapsed_ms for r in results if r.elapsed_ms is not None]
    elapsed_p50 = statistics.median(elapsed_latencies) if elapsed_latencies else 0.0
    elapsed_max = max(elapsed_latencies) if elapsed_latencies else 0.0

    summary = (
        f"answers={total_answers}/{num_children} "
        f"elapsed_p50_ms={elapsed_p50:.1f} elapsed_max_ms={elapsed_max:.1f}"
    )
    print(summary)

    for r, ok, reason in evaluations:
        if not ok:
            print(f"--- child {r.index} miss: {reason} (exit {r.returncode}) ---")
            stderr_tail = "\n".join(r.stderr.strip().splitlines()[-10:])
            if stderr_tail:
                print(stderr_tail)

    return total_answers, summary


def test_stdio_concurrency(tmp_path: Path) -> None:
    """8-way parallel stdio probe against one fresh store file."""
    store = default_store_path()
    child_env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "PYTHONPATH": _MEMBER_ROOT,
        "NOTT_DATABASE_DIR": str(store.parent),
        "NOTT_NMEMORY_CONFIG": str(tmp_path / "no-memory.toml"),
    }
    argv = [sys.executable, "-m", "nmemory", "mcp", "serve"]
    answers, _ = run_probe(argv, child_env, tmp_path, num_children=8)
    assert answers == 8, f"Expected 8 answers, got {answers}"


def test_probe_rejects_bool_id() -> None:
    replies = ['{"id": true, "result": {}}', '{"id": 2, "result": {}}']
    assert not _validate_child(ChildResult(0, replies, 0.0, "fake-stderr-tail", 0))[0]


def test_probe_rejects_null_envelope() -> None:
    # The review falsifier: no jsonrpc field, result null — a parseable line
    # with an id is NOT a usable MCP reply and must not count as an answer.
    replies = ['{"id":1,"result":null}', '{"id":2,"result":null}']
    assert not _validate_child(ChildResult(0, replies, 0.0, "fake-stderr-tail", 0))[0]


def test_probe_rejects_empty_result_object() -> None:
    # Review falsifier, round 2: a WELL-FORMED envelope carrying an EMPTY
    # result object is structurally a JSON-RPC response but is not an ANSWER.
    replies = [
        '{"jsonrpc":"2.0","id":1,"result":{}}',
        '{"jsonrpc":"2.0","id":2,"result":{}}',
    ]
    assert not _validate_child(ChildResult(0, replies, 0.0, "fake-stderr-tail", 0))[0]


def test_probe_accepts_well_formed_replies() -> None:
    # Identity control: a judge that rejected everything would also pass the
    # negative tests above. Mirrors verb_result's real success reply.
    replies = [
        '{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2024-11-05","capabilities":{},"serverInfo":{"name":"nmemory"}}}',
        '{"jsonrpc":"2.0","id":2,"result":{"content":[{"type":"text","text":"ok"}],"isError":false}}',
    ]
    ok, reason = _validate_child(ChildResult(0, replies, 0.0, "", 0))
    assert ok, reason


def test_probe_rejects_jsonrpc_error() -> None:
    # A genuine JSON-RPC error frame is well-formed protocol output and still
    # must not count as an answer.
    replies = [
        '{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2024-11-05"}}',
        '{"jsonrpc":"2.0","id":2,"error":{"code":-32602,"message":"unknown tool"}}',
    ]
    assert not _validate_child(ChildResult(0, replies, 0.0, "fake-stderr-tail", 0))[0]


VALID_INITIALIZE = (
    '{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2024-11-05",'
    '"capabilities":{},"serverInfo":{"name":"nmemory"}}}'
)
# verb_result (server.py) answers every tools/call success with content AND an
# explicit isError: false — the controls mirror the product's real reply.
VALID_TOOL_CALL = (
    '{"jsonrpc":"2.0","id":2,"result":{"content":[{"type":"text","text":"ok"}],'
    '"isError":false}}'
)

# Every malformed variant found across all four review rounds. A fifth round
# must ARGUE that a new shape is a real answer, not discover another unguarded
# layer: each row mutates one layer of a genuine answer and must score 0/8.
MALFORMED_CASES: list[tuple[str, list[str]]] = [
    (
        "bool id",
        [
            '{"jsonrpc":"2.0","id":true,"result":{}}',
            '{"jsonrpc":"2.0","id":2,"result":{}}',
        ],
    ),
    (
        "null result, no jsonrpc field",
        ['{"id":1,"result":null}', '{"id":2,"result":null}'],
    ),
    (
        "missing result key",
        ['{"jsonrpc":"2.0","id":1}', '{"jsonrpc":"2.0","id":2}'],
    ),
    (
        "empty result object",
        [
            '{"jsonrpc":"2.0","id":1,"result":{}}',
            '{"jsonrpc":"2.0","id":2,"result":{}}',
        ],
    ),
    (
        "content list holding null",
        [VALID_INITIALIZE, '{"jsonrpc":"2.0","id":2,"result":{"content":[null]}}'],
    ),
    (
        "initialize protocolVersion null",
        ['{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":null}}', VALID_TOOL_CALL],
    ),
    (
        "text block with null text",
        [
            VALID_INITIALIZE,
            '{"jsonrpc":"2.0","id":2,"result":{"content":[{"type":"text","text":null}]}}',
        ],
    ),
    (
        "isError as a string",
        [
            VALID_INITIALIZE,
            (
                '{"jsonrpc":"2.0","id":2,"result":{"content":[{"type":"text","text":"ok"}],'
                '"isError":"false"}}'
            ),
        ],
    ),
    (
        "isError dropped from the reply",
        [
            VALID_INITIALIZE,
            '{"jsonrpc":"2.0","id":2,"result":{"content":[{"type":"text","text":"ok"}]}}',
        ],
    ),
    (
        "text blocks mixing a string and a null",
        [
            VALID_INITIALIZE,
            (
                '{"jsonrpc":"2.0","id":2,"result":{"isError":false,"content":'
                '[{"type":"text","text":"ok"},{"type":"text","text":null}]}}'
            ),
        ],
    ),
]


@pytest.mark.parametrize(("case", "replies"), MALFORMED_CASES)
def test_probe_scores_malformed_replies_zero(case: str, replies: list[str]) -> None:
    ok, reason = _validate_child(ChildResult(0, replies, 0.0, "fake-stderr-tail", 0))
    assert not ok, f"{case}: must score 0/8, validator accepted it ({reason})"


def test_probe_scores_a_real_answer() -> None:
    # The identity control: a judge that rejected everything would pass every
    # zero-row above.
    ok, reason = _validate_child(
        ChildResult(0, [VALID_INITIALIZE, VALID_TOOL_CALL], 0.0, "", 0)
    )
    assert ok, reason
