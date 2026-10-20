"""A typed double for `nspawn.supervisor.Supervisor`.

Proving L6's wiring — argument mapping, admission accounting, typed-refusal
translation, result rendering — must not depend on a reachable PostgreSQL, a
run root, or a spawnable child, so every mcp.py test that needs a Supervisor
injects this double instead of the real one.

A `@dataclass` subclass never calls the base class's `__init__`: its own
generated constructor only assigns the declared fields below, so the real
`Supervisor.__init__` — which makes directories and takes a registry — never
runs. (It used to raise `NotImplementedError` unconditionally, and this
docstring said so long after that stopped being true; `supervisor.py` carries
no `NotImplementedError` at all today.)

`quota` and `fleet` are doubles for methods L7 DOES NOT CARRY YET. They are
declared here against the form `mcp.py` calls, which is what lets L6's routing
be proven before the wire exists.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nspawn.mcp import McpError, McpServer
from nspawn.supervisor import Supervisor

if TYPE_CHECKING:
    from nspawn.supervisor import (
        CloseReadback,
        ProviderReport,
        QuotaReport,
        RunAccepted,
        RunResult,
        RunStatus,
        TranscriptPage,
    )

#: One recorded call: the method name and the exact keyword arguments it saw.
RecordedCall = tuple[str, dict[str, object]]


@dataclass
class FakeSupervisor(Supervisor):
    """Each method returns its configured result or raises its configured error."""

    execute_result: RunAccepted | Exception | None = None
    review_result: RunAccepted | Exception | None = None
    status_result: RunStatus | Exception | None = None
    result_result: RunResult | Exception | None = None
    transcript_result: TranscriptPage | Exception | None = None
    close_result: CloseReadback | Exception | None = None
    quota_result: QuotaReport | Exception | None = None
    providers_result: ProviderReport | Exception | None = None
    usage_result: dict[str, Any] | Exception | None = None
    #: The fleet page L7 does not carry yet, so the double holds whatever the
    #: test hands it: `mcp._to_jsonable` walks any dataclass, which is the whole
    #: of what L6 does with the answer. Typed `object` on purpose — narrowing it
    #: would mean naming a type that does not exist.
    fleet_result: object | Exception | None = None
    calls: list[RecordedCall] = field(default_factory=list)
    #: Held while a method runs, AFTER admission takes its permit — lets a
    #: concurrency test force real overlap instead of racing an instant call.
    delay: float = 0.0

    def declared_providers(self, cwd: str) -> tuple[frozenset[str], Path | None]:
        """Whatever the batch asks for is declared: this fake stands in for the
        roster, and the name-resolution law has its own test in `test_batch`."""
        return frozenset({"x"}), Path("/fixture/roster.toml")

    def execute(
        self,
        prompt: str,
        cwd: str,
        *,
        provider: str | None = None,
        model: str | None = None,
        preferred_model: str | None = None,
        effort: str | None = None,
        execution_class: str | None = None,
        session_id: str | None = None,
        timeout_ms: int = 0,
        read_only: bool = False,
        read_paths: Sequence[str] | None = None,
        system_prompt: str | None = None,
        producer: Sequence[str] = (),
    ) -> RunAccepted:
        self.calls.append(
            (
                "execute",
                {
                    # Only when named, so every other record keeps its shape.
                    **({"producer": producer} if producer else {}),
                    # Only the ping prefers a model; every other verb's record
                    # stays the shape its tests compare.
                    **(
                        {"preferred_model": preferred_model}
                        if preferred_model is not None
                        else {}
                    ),
                    "prompt": prompt,
                    "cwd": cwd,
                    "provider": provider,
                    "model": model,
                    "effort": effort,
                    "execution_class": execution_class,
                    "session_id": session_id,
                    "read_only": read_only,
                    "read_paths": read_paths,
                    "system_prompt": system_prompt,
                    "timeout_ms": timeout_ms,
                },
            )
        )
        time.sleep(self.delay)
        return _settle(self.execute_result)

    def review(
        self,
        branch: str,
        cwd: str,
        *,
        base: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        prompt: str | None = None,
        read_paths: Sequence[str] | None = None,
        system_prompt: str | None = None,
        producer: Sequence[str] = (),
    ) -> RunAccepted:
        self.calls.append(
            (
                "review",
                {
                    **({"producer": producer} if producer else {}),
                    "branch": branch,
                    "cwd": cwd,
                    "base": base,
                    "provider": provider,
                    "model": model,
                    "effort": effort,
                    "prompt": prompt,
                    "read_paths": read_paths,
                    "system_prompt": system_prompt,
                },
            )
        )
        time.sleep(self.delay)
        return _settle(self.review_result)

    def status(self, run_id: str, timeout_ms: int = 0) -> RunStatus:
        self.calls.append(("status", {"run_id": run_id, "timeout_ms": timeout_ms}))
        time.sleep(self.delay)
        return _settle(self.status_result)

    def result(self, run_id: str, timeout_ms: int = 0) -> RunResult:
        if timeout_ms > 0:
            self.calls.append(("status", {"run_id": run_id, "timeout_ms": timeout_ms}))
        self.calls.append(("result", {"run_id": run_id}))
        time.sleep(self.delay)
        return _settle(self.result_result)

    def transcript(
        self,
        run_id: str,
        since_seq: int = 0,
        max_bytes: int | None = None,
        frame_types: Sequence[str] | None = None,
    ) -> TranscriptPage:
        self.calls.append(
            (
                "transcript",
                {
                    "run_id": run_id,
                    "since_seq": since_seq,
                    "max_bytes": max_bytes,
                    "frame_types": frame_types,
                },
            )
        )
        time.sleep(self.delay)
        return _settle(self.transcript_result)

    def close(self, run_id: str) -> CloseReadback:
        self.calls.append(("close", {"run_id": run_id}))
        time.sleep(self.delay)
        return _settle(self.close_result)

    def quota(
        self,
        *,
        provider: str | None = None,
        refresh: int = 0,
    ) -> QuotaReport:
        self.calls.append(("quota", {"provider": provider, "refresh": refresh}))
        time.sleep(self.delay)
        return _settle(self.quota_result)

    def fleet(self) -> object:
        self.calls.append(("fleet", {}))
        time.sleep(self.delay)
        return _settle(self.fleet_result)

    def usage_show(
        self, time_zone: str, *, now_ms: int | None = None
    ) -> dict[str, Any]:
        self.calls.append(("usage_show", {"time_zone": time_zone, "now_ms": now_ms}))
        time.sleep(self.delay)
        return _settle(self.usage_result)

    def providers(self, provider: str | None = None) -> ProviderReport:
        """Overridden for HERMETICITY, not only for recording.

        `Supervisor.providers` is real and reads `~/.nott/spawn.toml` off
        the ambient `HOME`. Inherited, it made every L6 test that reaches this
        verb — the fuzz property included — depend on whatever roster the box
        running the suite happens to carry, and pass either way. Measured: with
        no override the fuzz was green against the owner's real roster AND
        green against `HOME=/nonexistent-home`.
        """
        self.calls.append(("providers", {"provider": provider}))
        time.sleep(self.delay)
        return _settle(self.providers_result)


def _settle[T](outcome: T | Exception | None) -> T:
    if isinstance(outcome, Exception):
        raise outcome
    if outcome is None:
        raise NotImplementedError(
            "FakeSupervisor method invoked with no result configured"
        )
    return outcome


def handle(server: McpServer, frame: dict[str, Any]) -> dict[str, Any]:
    """One `tools/call` frame answered the way the SDK door answers it.

    The SDK (`nspawn.main`) owns the envelope; this reaches the same boundary,
    `McpServer.call_tool`, and frames its result or its typed refusal.
    """
    params = frame.get("params") or {}
    head = {"jsonrpc": "2.0", "id": frame.get("id")}
    try:
        result = server.call_tool(params.get("name"), params.get("arguments"))
    except McpError as error:
        return {**head, "error": error.to_wire()}
    return {**head, "result": result}
