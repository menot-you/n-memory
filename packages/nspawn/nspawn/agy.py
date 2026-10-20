"""One native AGY NDJSON turn, held by the existing child custody boundary."""

from __future__ import annotations

import json
import math
import re
import subprocess
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .child import ChildIdentityError, SpawnedChild, spawn_child
from .errors import NERR_2003, NSpawnError
from .limits import MAX_DURABLE_IDENTIFIER_BYTES, MAX_DURABLE_OBSERVATION_BYTES
from .pump import MAX_OUTPUT_LINE_BYTES, MAX_RUN_OUTPUT_BYTES, PumpLimitExceeded
from .redact import SecretNeedles


class AgyError(NSpawnError):
    """A native turn could not establish a bounded, correlated result."""

    def __init__(self, detail: str) -> None:
        super().__init__(NERR_2003, detail)


class AgyDeadlineExceeded(AgyError):
    """The caller's turn deadline elapsed before completion."""


@dataclass(frozen=True, slots=True)
class AgyResult:
    conversation_id: str
    status: str
    final_text: str | None
    usage: dict[str, int]
    error: str | None
    duration_seconds: float

    def __post_init__(self) -> None:
        _identity(self.conversation_id)
        if self.status not in ("SUCCESS", "ERROR", "CANCELED"):
            raise AgyError("AGY returned an unknown terminal status")
        if self.final_text is not None and not isinstance(self.final_text, str):
            raise AgyError("AGY response must be text or null")
        if self.error is not None and not isinstance(self.error, str):
            raise AgyError("AGY error must be text or null")
        if not isinstance(self.usage, dict) or any(
            not isinstance(key, str) or type(value) is not int or value < 0
            for key, value in self.usage.items()
        ):
            raise AgyError("AGY usage must contain nonnegative integer counters")
        if not _finite_seconds(self.duration_seconds) or self.duration_seconds < 0:
            raise AgyError("AGY duration must be finite and nonnegative")
        if self.status == "SUCCESS" and self.error:
            raise AgyError("AGY SUCCESS conflicts with its error field")


def _identity(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or not value.isprintable()
        or value != value.strip()
        or len(value.encode("utf-8")) > MAX_DURABLE_IDENTIFIER_BYTES
    ):
        raise AgyError("AGY conversation identity is missing or invalid")
    return value


def _finite_seconds(value: object) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _diagnostic_excerpt(text: str, maximum: int) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= maximum:
        return text
    suffix = " [truncated]"
    return raw[: maximum - len(suffix)].decode("utf-8", errors="ignore") + suffix


#: AGY refuses a turn for its quota with the wait to its reset, and says it
#: nowhere else: `RESOURCE_EXHAUSTED (code 429): Individual quota reached. ...
#: Resets in 126h54m6s.` The live CLI (2026-10-20) drops the code and starts at
#: `Individual quota reached.`. Only that refusal, with the whole wait, is read.
_QUOTA_RESET = re.compile(
    r"(?:(?<![A-Z_])RESOURCE_EXHAUSTED \(code 429\):[^.]*\bquota\b"
    r"|\bIndividual quota reached\.).*?"
    r"\bResets in (?=\d)(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?(?=\.|\s*$)",
    re.DOTALL,
)


def quota_reset(error: str | None, now: int) -> int | None:
    """The epoch second AGY's quota refusal says it answers again; None for any
    other failure."""
    found = _QUOTA_RESET.search(error or "")
    if found is None or not any(found.groups()):
        return None
    hours, minutes, seconds = (int(part or 0) for part in found.groups())
    return now + hours * 3600 + minutes * 60 + seconds


@dataclass
class _Protocol:
    """The pump retains only identity and one terminal; raw frames stay in memory."""

    needles: SecretNeedles
    changed: threading.Condition = field(default_factory=threading.Condition)
    identity: str | None = None
    result: AgyResult | None = None
    consumed: int = 0
    # The reply's text deltas not yet taken by the turn.
    texts: list[str] = field(default_factory=list)
    identity_refusal: tuple[AgyError, str] | None = None
    stderr_tail: bytes = b""

    def stderr_accept(self, line: bytes) -> bytes:
        """Retain only pump-authored redacted bytes, independent of its writable file."""
        text = self.needles.redact(line.decode("utf-8", errors="replace"))
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError:
            pass
        except RecursionError as error:
            raise PumpLimitExceeded(
                "output JSON nesting exceeds redaction limits"
            ) from error
        else:
            try:
                safe = self.needles.redact_value(decoded)
                if safe != decoded:
                    newline = "\n" if text.endswith("\n") else ""
                    text = json.dumps(safe, ensure_ascii=True) + newline
            except RecursionError as error:
                raise PumpLimitExceeded(
                    "output JSON nesting exceeds redaction limits"
                ) from error
        with self.changed:
            self.stderr_tail = (self.stderr_tail + text.encode("utf-8"))[
                -MAX_DURABLE_OBSERVATION_BYTES:
            ]
        # The owning pump still performs its normal redaction, limits and append.
        return line

    def event_identity(self, value: Any, frame: dict[str, Any]) -> str:
        try:
            return _identity(value)
        except AgyError as error:
            # Keep diagnostic fields only, never an init's tools/config inventory.
            details = frame.get("result") if frame["event"] == "result" else frame
            evidence = {"event": frame["event"]}
            if isinstance(details, dict):
                evidence.update(
                    (key, details[key])
                    for key in ("status", "error", "message")
                    if isinstance(details.get(key), str)
                )
            safe = json.dumps(self.needles.redact_value(evidence), ensure_ascii=True)
            self.identity_refusal = (
                error,
                _diagnostic_excerpt(safe, MAX_DURABLE_OBSERVATION_BYTES // 2),
            )
            raise

    def accept(self, line: bytes) -> bytes:
        with self.changed:
            self.consumed += len(line)
            if self.consumed > MAX_RUN_OUTPUT_BYTES:
                raise AgyError("AGY raw protocol exceeds the run byte limit")
            try:
                frame = json.loads(line)
            except (ValueError, UnicodeDecodeError, RecursionError) as error:
                raise AgyError("AGY emitted malformed JSON") from error
            if not isinstance(frame, dict):
                raise AgyError("AGY event must be an object")
            if self.result is not None:
                raise AgyError("AGY emitted an event after its terminal result")
            event = frame.get("event")
            if event == "init":
                if self.identity is not None:
                    raise AgyError("AGY emitted duplicate initialization")
                self.identity = self.event_identity(frame.get("conversation_id"), frame)
            elif event == "step_update":
                if self.identity is None:
                    raise AgyError("AGY emitted a step before initialization")
                update = frame.get("step_update")
                if not isinstance(update, dict):
                    raise AgyError("AGY step update must be an object")
                if _identity(update.get("conversation_id")) != self.identity:
                    raise AgyError("AGY step belongs to another conversation")
                if isinstance(update.get("text_delta"), str):
                    self.texts.append(update["text_delta"])
            elif event == "result":
                terminal = frame.get("result")
                if not isinstance(terminal, dict):
                    raise AgyError("AGY terminal result must be an object")
                identity = self.event_identity(terminal.get("conversation_id"), frame)
                if self.identity is None or identity != self.identity:
                    raise AgyError(
                        "AGY result does not match its initialized conversation"
                    )
                try:
                    self.result = AgyResult(
                        conversation_id=identity,
                        status=terminal["status"],
                        final_text=terminal.get("response"),
                        usage=terminal["usage"],
                        error=terminal.get("error"),
                        duration_seconds=terminal["duration_seconds"],
                    )
                except KeyError as error:
                    raise AgyError("AGY terminal is missing required fields") from error
            else:
                raise AgyError("AGY emitted an unknown event")
            self.changed.notify_all()
            try:
                redacted = self.needles.redact_value(frame)
                return (json.dumps(redacted, ensure_ascii=True) + "\n").encode()
            except (ValueError, RecursionError) as error:
                raise AgyError("AGY event exceeds JSON encoding limits") from error


class AgyTurn:
    """A single stdin turn; caller-selected argv owns model, account and resume flags."""

    def __init__(self, child: SpawnedChild, protocol: _Protocol) -> None:
        self.child = child
        self._protocol = protocol
        self._ran = False
        self._closed = False
        # Set once the child is reaped. A reap that raised before that runs
        # again on the context exit instead of leaving a live child behind.
        self._reaped = False
        # A pump refusal already raised to the caller; the reap that follows
        # re-reads it through `wait` and must not bury it under a cleanup error.
        self._pump_error: NSpawnError | OSError | None = None

    @classmethod
    @contextmanager
    def start(
        cls,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: dict[str, str],
        run_dir: Path,
        needles: SecretNeedles,
    ) -> Iterator[AgyTurn]:
        protocol = _Protocol(needles)
        child = spawn_child(
            argv,
            cwd,
            env,
            run_dir,
            needles,
            stdin_pipe=True,
            stdout_transform=protocol.accept,
            stderr_transform=protocol.stderr_accept,
        )
        turn = cls(child, protocol)
        try:
            yield turn
        finally:
            try:
                turn._reap()
            finally:
                # A reap an alarm or Ctrl-C cut short gets one more pass; once
                # the child is reaped this returns at once.
                turn._reap()

    def _reap(self) -> None:
        if self._reaped:
            return
        self._closed = True
        try:
            self.child.close_stdin()
            if self.child.poll() is None:
                try:
                    self.child.stop_then_kill(self.child.identity)
                except ChildIdentityError:
                    if self.child.poll() is None:
                        raise
            self.child.wait(timeout=2)
            self._reaped = True
        except (NSpawnError, OSError) as error:
            # `wait` joins stdout, then stderr in a `finally`: when both pumps
            # failed, the stderr error replaces the stdout one and carries it
            # as its context. Either may be the refusal already raised. Those
            # joins follow the leader's reap.
            if self._pump_error is not None and (
                error is self._pump_error or error.__context__ is self._pump_error
            ):
                self._reaped = True
                return
            if isinstance(error, AgyError):
                raise
            raise AgyError("AGY child cleanup or output failed") from error
        except subprocess.TimeoutExpired as error:
            raise AgyError("AGY child cleanup or output failed") from error

    def _join_pumps(self) -> None:
        """Raise the refusal a finished pump recorded, before any EOF verdict."""
        for pump in (self.child.stdout_pump, self.child.stderr_pump):
            try:
                pump.join(timeout=0)
            except (NSpawnError, OSError) as error:
                self._pump_error = error
                raise

    def _identity_evidence(self, error: AgyError, event: str) -> AgyError:
        """Use the owning pump's bounded memory after reap, never its writable path."""
        prefix = f"{error.detail}; AGY event excerpt: "
        separator = "; AGY stderr tail: "
        budget = MAX_DURABLE_OBSERVATION_BYTES - len(
            str(AgyError(prefix + separator)).encode("utf-8")
        )
        event = _diagnostic_excerpt(event, budget // 2)
        maximum = budget - len(event.encode("utf-8"))
        stderr = self._protocol.stderr_tail[-maximum:].decode("utf-8", errors="ignore")
        return AgyError(prefix + event + separator + stderr)

    def run(
        self,
        prompt: str,
        *,
        timeout: float | None = None,
        conversation_id: str | None = None,
        on_thread: Callable[[str], None] | None = None,
        on_text: Callable[[str], None] | None = None,
    ) -> AgyResult:
        """`on_text` takes the reply as it streams, redacted, once its conversation
        is known."""
        pending = ""
        try:
            if self._ran or self._closed:
                raise AgyError("AGY accepts exactly one turn per child")
            self._ran = True
            if conversation_id is not None:
                _identity(conversation_id)
            if timeout is not None and (not _finite_seconds(timeout) or timeout < 0):
                raise AgyError("AGY turn deadline must be finite and nonnegative")
            deadline = time.monotonic() + timeout if timeout else None
            if not isinstance(prompt, str):
                raise AgyError("AGY prompt must be text")
            try:
                payload = (
                    json.dumps(
                        {
                            "event": "user",
                            "message": {"content": prompt},
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                ).encode("utf-8")
            except UnicodeEncodeError as error:
                raise AgyError("AGY prompt must be valid UTF-8 text") from error
            if len(payload) > MAX_OUTPUT_LINE_BYTES:
                raise AgyError("AGY input exceeds the frame byte limit")
            self.child.write_stdin(
                payload,
                max(0.0, deadline - time.monotonic()) if deadline is not None else None,
            )
            self.child.close_stdin()
            notified = False
            while True:
                remaining = (
                    deadline - time.monotonic() if deadline is not None else None
                )
                if remaining is not None and remaining <= 0:
                    raise AgyDeadlineExceeded("AGY turn deadline exceeded")
                self._join_pumps()
                if self.child.stdout_pump.finished and self._protocol.result is None:
                    self._join_pumps()
                    raise AgyError("AGY reached EOF without a terminal result")
                with self._protocol.changed:
                    identity = self._protocol.identity
                    pending += "".join(self._protocol.texts)
                    self._protocol.texts.clear()
                if identity is not None and not notified:
                    if conversation_id is not None and identity != conversation_id:
                        raise AgyError(
                            "AGY initialized a different resumed conversation"
                        )
                    if on_thread is not None:
                        on_thread(self._protocol.needles.redact(identity))
                    notified = True
                if notified and pending and on_text is not None:
                    # Only its own conversation's text leaves, redacted, and never a
                    # tail that the next delta could complete into a needle.
                    needles = self._protocol.needles
                    pending = needles.redact(pending)
                    cut = needles.releasable(pending)
                    if cut:
                        on_text(pending[:cut])
                    pending = pending[cut:]
                exit_code = self.child.poll()
                if exit_code is not None and all(
                    pump.finished
                    for pump in (self.child.stdout_pump, self.child.stderr_pump)
                ):
                    # A refusal of the last frame lands between the loop-top
                    # join and this exit: record it before `wait` re-raises it
                    # untyped for the reap to bury.
                    self._join_pumps()
                    self.child.wait(timeout=0)
                    result = self._protocol.result
                    if result is None:
                        raise AgyError("AGY reached EOF without a terminal result")
                    if result.status == "SUCCESS" and exit_code != 0:
                        raise AgyError("AGY SUCCESS conflicts with its process exit")
                    needles = self._protocol.needles
                    return AgyResult(
                        conversation_id=needles.redact(result.conversation_id),
                        status=result.status,
                        final_text=(
                            needles.redact(result.final_text)
                            if result.final_text is not None
                            else None
                        ),
                        usage=needles.redact_value(result.usage),
                        error=(
                            needles.redact(result.error)
                            if result.error is not None
                            else None
                        ),
                        duration_seconds=result.duration_seconds,
                    )
                with self._protocol.changed:
                    self._protocol.changed.wait(
                        min(remaining, 0.02) if remaining is not None else 0.02
                    )
        except (NSpawnError, OSError, subprocess.TimeoutExpired) as error:
            self._reap()
            refused = self._protocol.identity_refusal
            if refused is not None and error is refused[0]:
                raise self._identity_evidence(refused[0], refused[1]) from error
            # A typed refusal of a stdout line (a credential refresh, say) is the
            # cause and surfaces as itself; a line over the byte ceiling stays
            # an AGY output failure.
            if isinstance(error, AgyError) or (
                error is self._pump_error
                and isinstance(error, NSpawnError)
                and not isinstance(error, PumpLimitExceeded)
            ):
                raise
            raise AgyError("AGY child transport or output failed") from error
