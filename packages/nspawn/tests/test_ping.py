"""`nspawn_route_ping` — a D18 verdict PER ROUTE, and the field that makes it honest.

The owner order, 2026-09-02: "an nspawn_route_ping tool that does this
automatically is worth having", always via `model_fast`. The acceptance that wins or loses it is the
D18 convention applied TO EACH ROUTE, never to the whole call:

* **green (0)**: the route answered, and answered what it was asked;
* **typed refusal (2)**, NEVER counted as failure: no quota, missing
  credential, an undeclared model, or an expired deadline — and each one names its
  cause;
* **red (3)**: behavior only — it answered wrong, or the transport
  broke.

"A route with no quota does NOT redden the ping" was an explicit order, and it is the difference
between an honest detector and one that screams: grok today answers
`402 Grok Build usage balance exhausted`, which is an empty account, not a
defect.
"""

from __future__ import annotations

import itertools
import json
from pathlib import Path
from typing import Any

import pytest

from nspawn import mcp as mcp_module
from nspawn import supervisor as supervisor_module
from nspawn.errors import NERR_1105, NERR_1107, NSpawnError
from nspawn.mcp import NSPAWN_TOOL_BY_NAME, McpError
from nspawn.rows import RunPhase


def _write_result_frame(run_dir: Path, **fields: object) -> None:
    """One `type: result` line, the shape `terminal._terminal_result` accepts."""
    frame: dict[str, object] = {
        "type": "result",
        "is_error": True,
        "terminal_reason": "error_during_execution",
        "result": None,
    }
    frame.update(fields)
    (run_dir / "stdout").write_bytes(json.dumps(frame).encode() + b"\n")


def test_the_api_error_status_survives_the_stream_observation(tmp_path: Path) -> None:
    """The vendor's 402 has to REACH the reader, or the ping has to guess.

    `terminal.py:928` already distinguishes the 402 ("provider quota exhausted") from a
    broken transport, and `_TerminalResult` carries the integer. But
    `_observe_streams` read that same `_TerminalResult` and kept only the
    `.text`: the number died two lines from whoever needs it, and a reader
    wanting to separate "no quota" from "broke" only had the
    `failure_reason` string to substring on — the same sniffing that
    `errors.classify()` already is, and that this lane named as debt.

    A route with no quota that reads as red is exactly the detector that
    screams, and the owner's order is the opposite of that.
    """
    run_dir = tmp_path / "run-grok"
    run_dir.mkdir()
    _write_result_frame(run_dir, api_error_status=402)

    observed: Any = supervisor_module._observe_streams(run_dir)

    assert observed.has_valid_result is True
    assert observed.api_error_status == 402, (
        "the 402 died in the stream observation; only the string reaches the reader"
    )


def test_a_clean_run_carries_no_api_error_status(tmp_path: Path) -> None:
    """The field is `int | None`, never a sentinel: absence is `None`."""
    run_dir = tmp_path / "run-agy"
    run_dir.mkdir()
    _write_result_frame(
        run_dir, is_error=False, terminal_reason="completed", result="OK"
    )

    observed: Any = supervisor_module._observe_streams(run_dir)

    assert observed.has_valid_result is True
    assert observed.api_error_status is None


# --- the detector: one D18 verdict per route -------------------------------


class _Fleet:
    """A fake supervisor that PROGRAMS the outcome of each route.

    It dispatches nothing: RAIL 4 of this lane forbids a real run, and a real
    run would not falsify any branch anyway — you do not order a 402 from a vendor.
    Each route declares here what `execute` does (accept or raise) and what
    `status` returns next.
    """

    def __init__(
        self,
        fast: dict[str, str | None],
        *,
        admission: dict[str, NSpawnError] | None = None,
        phases: dict[str, RunPhase] | None = None,
        api_error: dict[str, int] | None = None,
        rate_limited: set[str] | None = None,
        texts: dict[str, str | None] | None = None,
        reasons: dict[str, str] | None = None,
        warnings: dict[str, str] | None = None,
    ) -> None:
        self.fast = fast
        self.admission = admission or {}
        self.phases = phases or {}
        self.api_error = api_error or {}
        self.rate_limited = rate_limited or set()
        self.texts = texts or {}
        self.reasons = reasons or {}
        self.warnings = warnings or {}
        self.dispatched: list[dict[str, Any]] = []
        self.polled: list[tuple[str, int]] = []

    def fast_models(self, cwd: str) -> dict[str, str | None]:
        return dict(self.fast)

    def execute(self, prompt: str, cwd: str, **kwargs: Any) -> Any:
        name = str(kwargs["provider"])
        blocked = self.admission.get(name)
        if blocked is not None:
            raise blocked
        self.dispatched.append({"provider": name, "prompt": prompt, **kwargs})

        class _Accepted:
            run_id = f"run-{name}"

        return _Accepted()

    def status(self, run_id: str, timeout_ms: int = 0) -> Any:
        name = run_id.removeprefix("run-")
        self.polled.append((run_id, timeout_ms))
        return supervisor_module.RunStatus(
            run_id=run_id,
            phase=self.phases.get(name, RunPhase.COMPLETED),
            run_dir=f"/tmp/{run_id}",
            failure_reason=self.reasons.get(name),
            total_seq=1,
            requested_provider=name,
            executed_provider=name,
            executed_account=None,
            warning=self.warnings.get(name),
            tokens=7,
            marker=None,
            api_error_status=self.api_error.get(name),
            rate_limited=name in self.rate_limited,
        )

    def result(self, run_id: str) -> Any:
        name = run_id.removeprefix("run-")
        return supervisor_module.RunResult(
            run_id=run_id,
            final_text=self.texts.get(name, mcp_module.PING_TOKEN),
        )


def _row(answer: dict[str, Any], provider: str) -> dict[str, Any]:
    for entry in answer["routes"]:
        if entry["provider"] == provider:
            return entry
    raise AssertionError(f"the ping returned no row for {provider!r}")


def test_every_route_pings_with_its_own_declared_fast_model() -> None:
    """ "always use fast for ping" — owner order, 2026-09-02.

    A route's `model` is the expensive one; `model_fast` is what it serves quickly,
    and a detector that spends the big model to hear one word is a
    detector nobody runs twice.
    """
    fleet = _Fleet({"m1": "gemma4:e4b", "zai": "glm-5.3-flash[1m]"})
    mcp_module.ping_routes(fleet, "/tmp", deadline_ms=5_000)
    preferred = {row["provider"]: row["preferred_model"] for row in fleet.dispatched}
    assert preferred == {"m1": "gemma4:e4b", "zai": "glm-5.3-flash[1m]"}
    # A preference, not a pin: the caller of a ping asked for no model, so an
    # account's own id for it, or the roster's env, still decides (the picker).
    assert all("model" not in row for row in fleet.dispatched)
    assert all(row["read_only"] is True for row in fleet.dispatched)


def test_a_route_that_answers_the_token_is_green() -> None:
    fleet = _Fleet({"agy": "gemini-3-flash"})
    answer = mcp_module.ping_routes(fleet, "/tmp", deadline_ms=5_000)
    row = _row(answer, "agy")
    assert row["code"] == 0
    assert row["verdict"] == "green"


def test_a_ping_follows_its_work_to_the_routes_next_account() -> None:
    """An account of the route refused with 429 and nSPAWN moved the ping to
    the route's next account: the verdict reads that run, and no run is left
    that nobody reads."""
    moved = "33333333-3333-4333-8333-333333333333"
    fleet = _Fleet(
        {"codex": "gpt-6.1-mini"},
        phases={"codex": RunPhase.FAILED},
        api_error={"codex": 429},
        warnings={"codex": f"HTTP 429; continued as run {moved} on codex/second"},
    )
    row = _row(mcp_module.ping_routes(fleet, "/tmp", deadline_ms=5_000), "codex")
    assert row["code"] == 0, row
    assert [run_id for run_id, _ in fleet.polled] == ["run-codex", moved]


def test_a_route_that_answers_the_wrong_thing_is_RED() -> None:
    """Red is BEHAVIOR: the route spoke, and said something else."""
    fleet = _Fleet({"agy": "gemini-3-flash"}, texts={"agy": "bom dia"})
    row = _row(mcp_module.ping_routes(fleet, "/tmp", deadline_ms=5_000), "agy")
    assert row["code"] == 3
    assert row["verdict"] == "red"


def test_a_broken_transport_is_RED() -> None:
    fleet = _Fleet(
        {"agy": "gemini-3-flash"},
        phases={"agy": RunPhase.FAILED},
        reasons={"agy": "child terminated by signal 9"},
    )
    row = _row(mcp_module.ping_routes(fleet, "/tmp", deadline_ms=5_000), "agy")
    assert row["code"] == 3
    assert "signal 9" in row["cause"]


def test_a_route_without_quota_is_a_TYPED_REFUSAL_and_never_red() -> None:
    """Explicit owner order: a route with no quota does NOT redden the ping.

    grok today answers `402 Grok Build usage balance exhausted`. That is an
    empty account — a wallet fact, not a route defect. Counting that
    as red is the detector that screams, and a detector that screams stops
    being read by the third day.

    The distinction is NOT read from prose: `api_error_status` is an integer that
    `terminal.py` already separated out and that an earlier slice of this lane brought
    here. Substring-matching `failure_reason` would be guessing.
    """
    fleet = _Fleet(
        {"grok": "grok-4.5"},
        phases={"grok": RunPhase.FAILED},
        api_error={"grok": 402},
        reasons={
            "grok": "provider quota exhausted (402: ProviderExhausted/QuotaLimit)"
        },
    )
    row = _row(mcp_module.ping_routes(fleet, "/tmp", deadline_ms=5_000), "grok")
    assert row["code"] == 2, "no quota is not a failure"
    assert row["verdict"] == "refused"
    assert "402" in row["cause"]


def test_a_rate_limit_is_the_same_refusal_as_an_empty_balance() -> None:
    fleet = _Fleet(
        {"zai": "glm-5.3-flash[1m]"},
        phases={"zai": RunPhase.FAILED},
        api_error={"zai": 429},
    )
    row = _row(mcp_module.ping_routes(fleet, "/tmp", deadline_ms=5_000), "zai")
    assert row["code"] == 2
    assert "429" in row["cause"]


def test_an_expired_deadline_is_a_TYPED_REFUSAL_naming_it() -> None:
    """With no named deadline, a BUSY route reads as dead.

    Measured on m1 today: `OLLAMA_MAX_LOADED_MODELS=1`, `OLLAMA_NUM_PARALLEL=1`,
    `OLLAMA_KEEP_ALIVE=24h`. A loaded model holds the slot for 24 hours and a
    request with ANOTHER model never starts — it goes silent, `tokens: 0`, and looks
    dead. That is the exact defect this detector exists to undo, so an
    expired deadline is named and does NOT redden.
    """
    fleet = _Fleet({"m1": "gemma4:e4b"}, phases={"m1": RunPhase.RUNNING})
    row = _row(mcp_module.ping_routes(fleet, "/tmp", deadline_ms=5_000), "m1")
    assert row["code"] == 2
    assert row["verdict"] == "refused"
    assert "5000" in row["cause"], "the refusal does not name the deadline"
    assert row["run_id"] == "run-m1", (
        "the run stays alive and the caller does not know how to close it"
    )


def test_an_admission_refusal_is_a_REFUSAL_carrying_its_own_code() -> None:
    """A route the picker refuses never got to behave at all.

    This is the refusal this lane just wrote: `codex` declares neither
    `model` nor `models` and was refused, so the picker refuses with
    NERR_1107 before a process exists. The ping inherits THAT decision instead of
    inventing a second one.
    """
    fleet = _Fleet(
        {"codex": None, "agy": "gemini-3-flash"},
        admission={
            "codex": NSpawnError(
                NERR_1107, 'provider "codex" declared model is unservable'
            )
        },
    )
    answer = mcp_module.ping_routes(fleet, "/tmp", deadline_ms=5_000)
    refused = _row(answer, "codex")
    assert refused["code"] == 2
    assert refused["run_id"] is None
    assert "1107" in refused["cause"], "the refusal does not carry the typed code"
    assert _row(answer, "agy")["code"] == 0, "a closed route killed its siblings"


def test_a_wall_refuses_only_its_own_route() -> None:
    fleet = _Fleet(
        {"claude": "claude-opus-5", "agy": "gemini-3-flash"},
        admission={"claude": NSpawnError(NERR_1105, "account wall is up")},
    )
    answer = mcp_module.ping_routes(fleet, "/tmp", deadline_ms=5_000)
    assert _row(answer, "claude")["code"] == 2
    assert _row(answer, "agy")["code"] == 0


def test_the_whole_call_is_bounded_by_ONE_deadline_not_one_per_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Eight routes in series under a 60 s deadline are not eight minutes.

    The deadline starts from ONE `monotonic()` and each wait takes what REMAINS of it. A
    per-route deadline would be N times what the caller asked for — the same defect that
    `supervisor.py:1650` already fixed for a run's own accounting, and for the same
    reason: a deadline that multiplies is not a deadline.
    """
    # A COUNTER, not a list: the number of clock readings is an implementation
    # detail and a fixed list makes the test fail on count rather than behavior.
    clock = itertools.count(0.0, 0.5)
    monkeypatch.setattr(mcp_module.time, "monotonic", lambda: next(clock))

    fleet = _Fleet({"a": "m", "b": "m", "c": "m"})
    mcp_module.ping_routes(fleet, "/tmp", deadline_ms=9_000)

    # THE PROPERTY, not arithmetic: each poll gets strictly less than the previous
    # one, because it takes what REMAINS from a deadline taken ONCE. A per-route
    # implementation would say [9000, 9000, 9000] — constant, thus not strictly
    # decreasing — and would live 27 s under a 9 s request. Counting clock
    # readings pinned the implementation, not the behavior.
    waits = [timeout for _, timeout in fleet.polled]
    assert len(waits) == 3
    assert waits[0] < 9_000
    assert all(b < a for a, b in itertools.pairwise(waits)), waits


def test_no_run_outlives_the_verdict_however_late_it_was_dispatched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FINDING: this test pinned the defect, and the code's docstring lied.

    Each `execute` took the WHOLE `timeout_ms=deadline_ms`, but the harvest
    starts from a single instant. The route dispatched last started counting its
    deadline with the ping's already half spent, so the run outlived the verdict
    by exactly the time the earlier dispatches took — burning
    tokens after the caller already had the answer. With nine routes and slow
    spawns that is minutes.

    The deadline is ABSOLUTE: each run takes what REMAINS, like the harvest.
    """
    clock = itertools.count(0.0, 1.0)
    monkeypatch.setattr(mcp_module.time, "monotonic", lambda: next(clock))

    fleet = _Fleet({"a": "m", "b": "m", "c": "m"})
    mcp_module.ping_routes(fleet, "/tmp", deadline_ms=10_000)

    # Clock advancing 1 s per read: all three runs die at the SAME absolute
    # instant, which is when the verdict comes out. The defect produced [10000]*3.
    assert [row["timeout_ms"] for row in fleet.dispatched] == [9_000, 8_000, 7_000]


def test_a_deadline_of_zero_is_refused_because_the_prazo_is_mandatory() -> None:
    fleet = _Fleet({"agy": "gemini-3-flash"})
    with pytest.raises(McpError):
        mcp_module.ping_routes(fleet, "/tmp", deadline_ms=0)
    assert fleet.dispatched == []


def test_an_undeclared_name_fails_the_call_before_anything_is_dispatched() -> None:
    """The same rule as `nspawn_batch_start`: a typo is the CALLER's error."""
    fleet = _Fleet({"agy": "gemini-3-flash"})
    with pytest.raises(McpError) as caught:
        mcp_module.ping_routes(fleet, "/tmp", providers=["agy", "nosuch"])
    assert "nosuch" in str(caught.value)
    assert fleet.dispatched == []


def test_with_no_providers_named_the_whole_roster_is_pinged() -> None:
    """ "does this automatically" — the default fallback is the entire fleet."""
    fleet = _Fleet({"agy": "g", "zai": "z", "m1": "m"})
    answer = mcp_module.ping_routes(fleet, "/tmp", deadline_ms=5_000)
    assert {row["provider"] for row in answer["routes"]} == {"agy", "zai", "m1"}


def test_the_tool_is_declared_on_the_wire() -> None:
    contract = NSPAWN_TOOL_BY_NAME["nspawn_route_ping"]
    schema = contract.input_schema
    assert set(schema["required"]) == {"cwd"}
    assert set(schema["properties"]) == {"cwd", "providers", "deadline_ms"}
    assert "nspawn_route_ping" in mcp_module.SERVER_INSTRUCTIONS


def test_an_admission_error_without_a_catalog_entry_still_answers() -> None:
    """FINDING of this unit: `.entry` is NOT guaranteed by `NSpawnError`.

    Subclasses that define their own `__init__` (`VendorHomeError` among them)
    never set `.entry`. Reading `error.entry.code_str()` blind would raise
    `AttributeError` and kill the WHOLE ping, with a traceback, on the first such
    refusal —
    worse than one wrong line, because it takes its sisters down with it.

    `mcp.py:1337` had already learned this and reads `.entry` through `getattr`. The
    refusal carries the code WHEN one exists, and the message when not —
    absence is `None`, never an invented code.
    """

    class _NoEntry(NSpawnError):
        def __init__(self, message: str) -> None:
            self.message = message
            Exception.__init__(self, message)

    fleet = _Fleet(
        {"claude": "claude-haiku-4-5-20251001", "agy": "gemini-3-flash"},
        admission={"claude": _NoEntry("no credential file for account primary")},
    )
    answer = mcp_module.ping_routes(fleet, "/tmp", deadline_ms=5_000)
    row = _row(answer, "claude")
    assert row["code"] == 2
    assert "no credential file" in row["cause"]
    assert _row(answer, "agy")["code"] == 0, (
        "a route without an entry killed its siblings"
    )


def _write_rate_limit_event(run_dir: Path, status: str = "rejected") -> None:
    """The `rate_limit_event` frame — the TYPED quota channel, no HTTP status."""
    lines = [
        json.dumps(
            {
                "type": "rate_limit_event",
                "rate_limit_info": {
                    "status": status,
                    "rateLimitType": "five_hour",
                    "resetsAt": 1_800_000_000,
                },
            }
        ).encode(),
        json.dumps(
            {
                "type": "result",
                "is_error": True,
                "terminal_reason": "error_during_execution",
                "result": None,
            }
        ).encode(),
    ]
    (run_dir / "stdout").write_bytes(b"\n".join(lines) + b"\n")


def test_a_typed_rate_limit_rejection_survives_the_stream_observation(
    tmp_path: Path,
) -> None:
    """Quota has TWO channels, and the ping only saw one.

    The 402/429 arrives through `api_error_status`, in the `result` frame. But the
    native vendor rejects through `rate_limit_event` with `rate_limit_info.status ==
    "rejected"` — and `terminal.py:871` turns that into an `AccountWallRecord`,
    with no HTTP status at all. `api_error_status` stays `None`.

    For the ping, that run read as `FAILED` with no known quota, hence RED —
    and the owner's law is explicit: no quota never reddens. A five-hour
    ceiling on claude made the detector scream about the owner's wallet.

    The parser already exists (`terminal._rate_limit`); it was the observation that
    discarded it.
    """
    run_dir = tmp_path / "run-claude"
    run_dir.mkdir()
    _write_rate_limit_event(run_dir)

    observed: Any = supervisor_module._observe_streams(run_dir)

    assert observed.api_error_status is None, "this channel carries no HTTP status"
    assert observed.rate_limited is True, (
        "a rejeicao tipada morreu na observacao; so a prosa sobra ao leitor"
    )


def test_an_allowed_rate_limit_is_not_a_rejection(tmp_path: Path) -> None:
    """`allowed` is the NORMAL case — every healthy run carries one."""
    run_dir = tmp_path / "run-agy"
    run_dir.mkdir()
    _write_rate_limit_event(run_dir, status="allowed")

    observed: Any = supervisor_module._observe_streams(run_dir)

    assert observed.rate_limited is False


def test_a_typed_rate_limit_refuses_the_route_and_never_reddens_it() -> None:
    """The owner's law, in the branch that was missing: no quota never reddens."""
    fleet = _Fleet(
        {"claude": "claude-haiku-4-5-20251001"},
        phases={"claude": RunPhase.FAILED},
        rate_limited={"claude"},
        reasons={"claude": "vendor rate limit rejected (five_hour)"},
    )
    row = _row(mcp_module.ping_routes(fleet, "/tmp", deadline_ms=5_000), "claude")
    assert row["code"] == 2, "a five-hour ceiling is not a route defect"
    assert row["verdict"] == "refused"
    assert "rate limit" in row["cause"]


def test_a_route_whose_dispatch_turn_arrives_after_the_deadline_is_never_started(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """EDGE of the absolute-deadline fix: `0` is not "now", it is "forever".

    `supervisor.py:2027` is explicit — "Zero is still NO deadline", and
    `if deadline_ms <= 0: exit_code = spawned.wait()`, a wait with no ceiling. With the
    deadline already spent by earlier dispatches, `max(0, ...)` handed
    `timeout_ms=0` to the late route, which is the exact OPPOSITE of what the fix
    promised: the run would outlive the verdict forever.

    With no budget nothing is dispatched. The refusal names the deadline and there is no `run_id`,
    because there is no run.
    """
    clock = itertools.count(0.0, 6.0)
    monkeypatch.setattr(mcp_module.time, "monotonic", lambda: next(clock))

    fleet = _Fleet({"a": "m", "b": "m", "c": "m"})
    answer = mcp_module.ping_routes(fleet, "/tmp", deadline_ms=10_000)

    assert len(fleet.dispatched) == 1, "dispatched a run with no deadline at all"
    for late in ("b", "c"):
        row = _row(answer, late)
        assert row["code"] == 2
        assert row["run_id"] is None
        assert "10000" in row["cause"], "the refusal does not name the deadline"


def test_a_run_killed_by_its_own_deadline_is_a_REFUSAL_not_a_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TWIN edge: the deadline has TWO possible winners, and one read as red.

    The ping's deadline and the run's are the same absolute instant, so the race
    between them decides which fires. If the poll wins, the phase is not yet
    terminal and the route was already a refusal. If the run's `RunDeadlineExceeded` wins, the
    phase turns `FAILED`, the funnel drops to the behavior branch, and the route
    read as RED — over the same fact, with the same deadline.

    This is the m1 single-seat case: gemma4 loading, the deadline passes, and the detector screamed
    "dead route". The exact defect this tool exists to undo.

    The distinction is read from MY clock, never from prose: if the deadline has already passed
    when I read the phase, the deadline is the cause. A transport that breaks at the
    exact instant of the deadline reads as refusal — an honest tie, and refusal is the safe side.
    """
    clock = itertools.count(0.0, 4.0)
    monkeypatch.setattr(mcp_module.time, "monotonic", lambda: next(clock))

    fleet = _Fleet(
        {"m1": "gemma4:e4b"},
        phases={"m1": RunPhase.FAILED},
        reasons={"m1": "run exceeded its caller-named deadline of 5000 ms"},
    )
    row = _row(mcp_module.ping_routes(fleet, "/tmp", deadline_ms=5_000), "m1")
    assert row["code"] == 2, "o proprio prazo do ping leu-se como defeito da rota"
    assert row["verdict"] == "refused"
    assert "5000" in row["cause"]


def test_a_malformed_rate_limit_frame_does_not_sink_the_observation(
    tmp_path: Path,
) -> None:
    """The re-read skips a frame `terminal._rate_limit` refuses and keeps reading.

    Not a vendor shape: a malformed frame, and the property is that it cannot
    drown the rest of the observation (the result frame after it still lands).
    `classify_terminal_run` refuses the same frame, and that stays its job.
    """
    run_dir = tmp_path / "run-claude"
    run_dir.mkdir()
    lines = [
        {"type": "rate_limit_event", "rate_limit_info": []},
        {
            "type": "result",
            "is_error": True,
            "terminal_reason": "error_during_execution",
            "result": None,
            "api_error_status": 402,
        },
    ]
    (run_dir / "stdout").write_text("".join(json.dumps(x) + "\n" for x in lines))

    observed: Any = supervisor_module._observe_streams(run_dir)

    assert observed.rate_limited is False
    assert observed.api_error_status == 402
