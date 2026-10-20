"""Strict one-request/one-response NDJSON MCP driver for the binary under test."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

import nott_database
from nmemory.database import STORE_FILE

from nmemory_evals import qa
from nmemory_evals.artifacts import BENCH_VERSION
from nmemory_evals.path import directory_open
from nmemory_evals.qa import BenchmarkCases, NarratedEvent, Question
from nmemory_evals.renamed import env
from nmemory_evals.score import RecallObservation, RecallOutcome

RESPONSE_TIMEOUT: float = 30.0
SEMANTIC_READY_TIMEOUT: float = 15 * 60.0
SEMANTIC_POLL_INTERVAL: float = 0.25
SHUTDOWN_GRACE: float = 30.0
N1_PROJECT: str = "nmemory-bench"


class Direction(StrEnum):
    REQUEST = "request"
    RESPONSE = "response"


class EvaluationMode(StrEnum):
    FRESH = "fresh"
    REUSE = "reuse"


@dataclass
class TranscriptEntry:
    dir: Direction
    elapsed_ms: int
    frame: dict[str, Any]


@dataclass
class BenchmarkRun:
    questions: list[Question]
    observations: list[RecallObservation]
    transcript: list[TranscriptEntry] = field(default_factory=list)


class DriverError(Exception):
    """Driver I/O, protocol, child lifecycle, or preparation failure."""


def validate_disposable_store_dir(store_dir: Path) -> Path:
    """The benchmark store lives in a disposable directory under /var/tmp, never the live one.

    The evals write and delete. A store directory outside /var/tmp, or the
    account's own store directory, is refused before any child starts.
    """
    path = Path(store_dir)
    parts = path.parts
    disposable = (
        len(parts) >= 4
        and parts[:3] == ("/", "var", "tmp")
        and all(p not in (".", "..") for p in parts[3:])
    )
    if not disposable:
        raise DriverError(
            f"refusing store directory {path}: it must be a subdirectory of /var/tmp"
        )
    try:
        resolved = path.resolve()
        root = Path("/var/tmp").resolve()
    except OSError as err:
        raise DriverError(f"cannot resolve store directory {path}: {err}") from err
    if resolved == root or not resolved.is_relative_to(root):
        raise DriverError(
            f"refusing store directory {path}: its resolved path must stay under /var/tmp"
        )
    if resolved == nott_database.live_directory().resolve():
        raise DriverError(f"refusing store directory {path}: it is the live store")
    return path


def evaluation_mode() -> EvaluationMode:
    """Read the NOTT_NMEMORY_EVALS_REUSE environment variable."""
    val = env("NOTT_NMEMORY_EVALS_REUSE")
    if val is None:
        return EvaluationMode.FRESH
    if val == "1":
        return EvaluationMode.REUSE
    raise DriverError("NOTT_NMEMORY_EVALS_REUSE accepts only the explicit value 1")


def store_command(
    binary: Path | str, store_dir: Path
) -> tuple[list[str], dict[str, str]]:
    validate_disposable_store_dir(store_dir)
    cmd = [str(binary)]
    # The child opens `nmemory.sqlite3` under NOTT_DATABASE_DIR, never the live
    # store; the owner's memory.toml is kept out of the benchmark as well.
    env = {
        "NOTT_DATABASE_DIR": str(store_dir),
        "NOTT_NMEMORY_CONFIG": "/nonexistent/nmemory-evals/memory.toml",
    }
    for name in (
        "HOME",
        "TMPDIR",
        "XDG_CACHE_HOME",
        "UV_CACHE_DIR",
        "PYTHONPYCACHEPREFIX",
        *nott_database.TEST_MARKERS,
    ):
        if name in os.environ:
            env[name] = os.environ[name]
    return cmd, env


@contextmanager
def store_directory(store_dir: Path, env: dict[str, str]) -> Iterator[int]:
    """Keep the admitted directory open until the child inherits its descriptor."""
    validate_disposable_store_dir(store_dir)
    try:
        with directory_open(store_dir, create=True) as descriptor:
            env["NOTT_DATABASE_DIR"] = str(nott_database.directory_bind(descriptor))
            yield descriptor
    except (OSError, nott_database.RefusedError) as err:
        raise DriverError(f"benchmark store directory refused: {err}") from err


def nmemory_command(
    binary: Path | str,
    store_dir: Path,
    project: str,
    semantic: bool,
) -> tuple[list[str], dict[str, str]]:
    cmd, env = store_command(binary, store_dir)
    cmd.extend(["mcp", "serve", "--project", project])
    if semantic:
        for name in ["OPENAI_API_KEY", "OPENAI_API_KEY_FILE"]:
            if name in os.environ:
                env[name] = os.environ[name]
    return cmd, env


def prepare_database(
    binary: Path | str,
    store_dir: Path,
    mode: EvaluationMode,
) -> None:
    """Create the benchmark's empty store; REUSE keeps one that already exists."""
    cmd, env = store_command(binary, store_dir)
    cmd.extend(["database", "create"])
    try:
        with store_directory(store_dir, env) as descriptor:
            if (
                mode == EvaluationMode.REUSE
                and (Path(env["NOTT_DATABASE_DIR"]) / STORE_FILE).exists()
            ):
                return
            res = subprocess.run(
                cmd,
                env=env,
                pass_fds=(descriptor,),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        if res.returncode != 0:
            raise DriverError(
                f"benchmark store preparation failed: exit status: {res.returncode}"
            )
    except OSError as err:
        raise DriverError(f"driver I/O failed: {err}") from err


def embedding_status(binary: Path | str, store_dir: Path) -> dict[str, Any]:
    """Read the measured binary's own embedding status, as JSON."""
    cmd, env = store_command(binary, store_dir)
    cmd.extend(["embedding", "show", "--json"])
    try:
        with store_directory(store_dir, env) as descriptor:
            res = subprocess.run(
                cmd,
                env=env,
                pass_fds=(descriptor,),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        if res.returncode != 0:
            raise DriverError(f"semantic status failed: exit status: {res.returncode}")
        return json.loads(res.stdout)  # type: ignore[no-any-return]
    except (OSError, json.JSONDecodeError) as err:
        raise DriverError(f"semantic status failed: {err}") from err


def wait_for_semantic(binary: Path | str, store_dir: Path) -> None:
    """Wait until the exact database reports a complete semantic index."""
    deadline = time.monotonic() + SEMANTIC_READY_TIMEOUT
    while True:
        status = embedding_status(binary, store_dir)
        phase = status.get("phase", "missing")
        pending = int(status.get("pending", -1))
        active_vectors = int(status.get("active_vectors", -1))
        live_capsules = int(status.get("live_capsules", -1))
        target = status.get("target_space")

        if (
            phase == "ready"
            and pending == 0
            and active_vectors == live_capsules
            and target is None
        ):
            return

        if phase in ("stalled", "lexical_only", "missing"):
            raise DriverError(
                f"semantic status failed: index phase is {phase} with {pending} pending"
            )

        if time.monotonic() >= deadline:
            raise DriverError(
                f"semantic index did not become ready within {SEMANTIC_READY_TIMEOUT}s"
            )

        time.sleep(SEMANTIC_POLL_INTERVAL)


def ingest_request(event: NarratedEvent) -> dict[str, Any]:
    return {
        "name": "nmemory_capsule_ingest",
        "arguments": {
            "content": event.narrative,
            "source": "nmemory-bench:n1-synthetic-world",
            "anchor": f"event:{event.seq}",
            "proof_hint": event.proof_hint,
            "project_id": "nmemory-bench",
            "kind": "fact",
            "valid_from": event.valid_from,
        },
    }


def recall_outcome(payload: dict[str, Any], qid: str) -> RecallOutcome:
    """Parse declared recall outcome of one nmemory_capsule_retrieve payload."""
    outcome = payload.get("outcome")
    if outcome is None:
        raise DriverError(f"retrieve {qid} returned no declared outcome")
    match outcome:
        case "grounded":
            return RecallOutcome.GROUNDED
        case "missing_evidence":
            return RecallOutcome.MISSING_EVIDENCE
        case "abstain":
            return RecallOutcome.ABSTAIN
        case other:
            raise DriverError(f"retrieve {qid} returned unknown outcome {other}")


def take_request_id(next_id: list[int]) -> int:
    current = next_id[0]
    next_id[0] += 1
    return current


def tool_payload(response: dict[str, Any]) -> Any:
    """Extract embedded tool result payload from JSON-RPC response."""
    if "error" in response:
        raise DriverError(f"tool call failed: {response['error']}")
    if "result" not in response:
        raise DriverError("tool call failed: response omitted result")
    result = response["result"]
    content = result.get("content")
    if not content or not isinstance(content, list):
        raise DriverError("tool call failed: tool result omitted text content")
    first = content[0]
    text = first.get("text")
    if text is None:
        raise DriverError("tool call failed: tool result omitted text content")
    if result.get("isError") is True:
        raise DriverError(f"tool call failed: {text}")
    try:
        return json.loads(text)
    except json.JSONDecodeError as err:
        raise DriverError(f"tool call failed: unparseable text content: {err}") from err


class Driver:
    """NDJSON stdio MCP child driver."""

    def __init__(
        self,
        child: subprocess.Popen[bytes],
        response_timeout: float = RESPONSE_TIMEOUT,
    ) -> None:
        self.child = child
        self.response_timeout = response_timeout
        self.started = time.monotonic()
        self.transcript: list[TranscriptEntry] = []
        self.responses: queue.Queue[tuple[str | None, Exception | None]] = queue.Queue()
        self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._reader_thread.start()

    def _read_loop(self) -> None:
        if self.child.stdout is None:
            return
        try:
            for line_bytes in self.child.stdout:
                line = line_bytes.decode("utf-8").strip()
                if line:
                    self.responses.put((line, None))
        except (OSError, UnicodeDecodeError) as err:
            self.responses.put((None, err))

    @classmethod
    def spawn(
        cls,
        binary: Path | str,
        run_dir: Path | str,
        store_dir: Path,
        project: str,
        semantic: bool,
    ) -> Driver:
        """Spawn a fresh nmemory child over the benchmark's own store directory."""
        cmd, env = nmemory_command(binary, store_dir, project, semantic)
        xdg_dir = Path(run_dir) / "xdg"
        xdg_dir.mkdir(parents=True, exist_ok=True)
        env["XDG_STATE_HOME"] = str(xdg_dir)
        with store_directory(store_dir, env) as descriptor:
            return cls.spawn_command(cmd, env, RESPONSE_TIMEOUT, pass_fds=(descriptor,))

    @classmethod
    def spawn_command(
        cls,
        cmd: list[str],
        env: dict[str, str] | None = None,
        response_timeout: float = RESPONSE_TIMEOUT,
        *,
        pass_fds: tuple[int, ...] = (),
    ) -> Driver:
        """Spawn child command with piped stdin/stdout and inherited stderr."""
        try:
            child = subprocess.Popen(
                cmd,
                env=env,
                pass_fds=pass_fds,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=sys.stderr,
                bufsize=0,
            )
            return cls(child, response_timeout)
        except OSError as err:
            raise DriverError(f"driver I/O failed: {err}") from err

    def handshake(self) -> None:
        """Perform MCP initialize handshake and initialized notification."""
        self.request(
            1,
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "nmemory-bench", "version": BENCH_VERSION},
            },
        )
        self.notification("notifications/initialized", {})

    def request(
        self,
        id_num: int,
        method: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Send JSON-RPC request and wait for matched response frame."""
        frame = {
            "jsonrpc": "2.0",
            "id": id_num,
            "method": method,
            "params": params,
        }
        self.write_frame(frame)
        try:
            line, err = self.responses.get(timeout=self.response_timeout)
            if err is not None:
                raise DriverError(f"stdout reader failed: {err}")
            if line is None:
                status = self.child.poll()
                status_str = (
                    f"exit status: {status}" if status is not None else "stdout closed"
                )
                raise DriverError(f"child exited unexpectedly: {status_str}")
        except queue.Empty:
            self.kill_and_wait()
            raise DriverError(
                f"response {id_num} timed out after {self.response_timeout}s; child was killed"
            )

        try:
            response = json.loads(line)
        except json.JSONDecodeError as err:
            raise DriverError(f"driver JSON failed: {err}") from err

        actual = response.get("id")
        if actual != id_num:
            actual_str = str(actual) if actual is not None else "missing/non-numeric"
            raise DriverError(
                f"protocol response id mismatch: expected {id_num}, got {actual_str}"
            )

        self.transcript.append(
            TranscriptEntry(
                dir=Direction.RESPONSE,
                elapsed_ms=self.elapsed_ms(),
                frame=response,
            )
        )

        if "error" in response:
            raise DriverError(f"JSON-RPC request {id_num} failed: {response['error']}")
        if "result" not in response:
            raise DriverError(f"protocol response omitted result for request {id_num}")

        return response  # type: ignore[no-any-return]

    def notification(self, method: str, params: dict[str, Any]) -> None:
        """Send JSON-RPC notification frame."""
        self.write_frame({"jsonrpc": "2.0", "method": method, "params": params})

    def write_frame(self, frame: dict[str, Any]) -> None:
        if self.child.stdin is None or self.child.stdin.closed:
            raise DriverError("child stdin already closed")
        try:
            line = json.dumps(frame, separators=(",", ":")) + "\n"
            self.child.stdin.write(line.encode("utf-8"))
            self.child.stdin.flush()
            self.transcript.append(
                TranscriptEntry(
                    dir=Direction.REQUEST,
                    elapsed_ms=self.elapsed_ms(),
                    frame=frame,
                )
            )
        except OSError as err:
            raise DriverError(f"driver I/O failed: {err}") from err

    def finish(self) -> None:
        """Close stdin and wait up to SHUTDOWN_GRACE seconds for child exit."""
        if self.child.stdin and not self.child.stdin.closed:
            try:
                self.child.stdin.close()
            except OSError:
                pass
        deadline = time.monotonic() + SHUTDOWN_GRACE
        while True:
            poll = self.child.poll()
            if poll is not None:
                if poll == 0:
                    return
                raise DriverError(f"child exited unsuccessfully: exit status: {poll}")
            if time.monotonic() >= deadline:
                self.kill_and_wait()
                raise DriverError(
                    f"child shutdown timed out after {SHUTDOWN_GRACE}s; killed"
                )
            time.sleep(0.02)

    def kill_and_wait(self) -> None:
        try:
            self.child.kill()
            self.child.wait()
        except OSError:
            pass

    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self.started) * 1000)

    def __del__(self) -> None:
        if hasattr(self, "child") and self.child.poll() is None:
            self.kill_and_wait()


def exercise(
    binary: Path | str,
    run_dir: Path | str,
    store_dir: Path,
    cases: BenchmarkCases,
    k: int,
) -> BenchmarkRun:
    """Run end-to-end n1 benchmark against a system-under-test binary."""
    prepare_database(binary, store_dir, EvaluationMode.FRESH)
    driver = Driver.spawn(binary, run_dir, store_dir, N1_PROJECT, False)
    driver.handshake()

    next_req = [2]
    capsule_by_event: dict[int, str] = {}
    for event in cases.events:
        response = driver.request(
            take_request_id(next_req),
            "tools/call",
            ingest_request(event),
        )
        payload = tool_payload(response)
        outcomes = payload.get("outcomes", [])
        if not outcomes or not isinstance(outcomes, list):
            raise DriverError(f"ingest event {event.seq} returned no capsule id")
        outcome = outcomes[0]
        status = outcome.get("status", "missing")
        if status != "captured":
            raise DriverError(
                f"ingest event {event.seq} returned status {status}: "
                f"{outcome.get('error', 'no reason given')}"
            )
        cid = outcome.get("id")
        if not cid:
            raise DriverError(f"ingest event {event.seq} returned no capsule id")
        capsule_by_event[event.seq] = cid

    qa.resolve_capsule_ids(cases.questions, capsule_by_event)

    observations: list[RecallObservation] = []
    for question in cases.questions:
        response = driver.request(
            take_request_id(next_req),
            "tools/call",
            {
                "name": "nmemory_capsule_retrieve",
                "arguments": {
                    "terms": question.terms,
                    "project_id": N1_PROJECT,
                    "lane": "term",
                    "limit": k,
                    "token_budget": 100_000,
                },
            },
        )
        payload = tool_payload(response)
        outcome_val = recall_outcome(payload, question.qid)
        raw_results = payload.get("results", [])
        result_ids = [
            str(r["id"]) for r in raw_results if isinstance(r, dict) and "id" in r
        ]
        observations.append(
            RecallObservation(
                qid=question.qid,
                outcome=outcome_val,
                result_ids=result_ids,
            )
        )

    driver.finish()
    return BenchmarkRun(
        questions=cases.questions,
        observations=observations,
        transcript=driver.transcript,
    )
