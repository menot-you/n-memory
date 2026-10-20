"""L5 terminal evidence and cooldown-wall contract."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import cast

import pytest
from fixture_data import wire_fixture
from hypothesis import HealthCheck, given, seed, settings
from hypothesis import strategies as st

import nspawn.terminal as terminal_module
from nspawn.limits import MAX_DURABLE_IDENTIFIER_BYTES
from nspawn.rows import RunPhase
from nspawn.terminal import (
    MAX_USAGE_WINDOWS,
    AccountWallRecord,
    ProviderWallRecord,
    RunTelemetry,
    TerminalClassification,
    TerminalClassificationError,
    WallSource,
    WindowUsage,
    classify_terminal_run,
)

FIXTURE = Path(__file__).parent / "fixtures" / "claude-stream-wire-2.1.241.ndjson"
FIXTURE_250 = wire_fixture("claude-stream-wire-2.1.250.ndjson")
NOW = 1_787_000_000
MAX_U64 = 2**64 - 1
FUZZ_CORPUS = (
    b"",
    b"\x00",
    b"\xff",
    b"{",
    b"[]\n",
    b"null\n",
    b'{"type":null}\n',
    b'{"type":"result","result":"\\ud800"}\n',
    b'{"error":{"type":"rate_limit_error"}}\n',
    b"{" + b'"nested":' * 32 + b"null" + b"}" * 32,
)
_VALID_IDENTIFIER = st.text(
    alphabet=st.sampled_from(tuple("abcdefghijklmnopqrstuvwxyz0123456789-_.@")),
    min_size=1,
    max_size=32,
)


def _ndjson(*frames: object) -> bytes:
    return b"".join(
        json.dumps(frame, ensure_ascii=True, separators=(",", ":")).encode() + b"\n"
        for frame in frames
    )


def _result(
    *,
    is_error: object = False,
    reason: object = "completed",
    text: object = "OK",
    usage: object = None,
) -> dict[str, object]:
    frame: dict[str, object] = {
        "type": "result",
        "is_error": is_error,
        "terminal_reason": reason,
        "result": text,
    }
    if usage is not None:
        frame["usage"] = usage
    return frame


def _rate_limit(
    *,
    status: object = "rejected",
    resets_at: object = None,
    limit_type: object = "five_hour",
    windows: object = None,
) -> dict[str, object]:
    info: dict[str, object] = {"status": status}
    if resets_at is not None:
        info["resetsAt"] = resets_at
    if limit_type is not None:
        info["rateLimitType"] = limit_type
    if windows is not None:
        info["unifiedWindows"] = windows
    return {"type": "rate_limit_event", "rate_limit_info": info}


def _fallback(token: str) -> RunTelemetry:
    """The journal telemetry that a run without `result` frame carries."""
    return RunTelemetry(
        cost_usd=None,
        input_tokens=0,
        output_tokens=0,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
        duration_ms=0,
        duration_api_ms=0,
        num_turns=0,
        ttft_ms=None,
        ttft_stream_ms=None,
        time_to_request_ms=None,
        stop_reason=None,
        api_error_status=None,
        permission_denials=0,
        subagent_spawned=0,
        model_usage=(),
        failure=token,
    )


def _classify(
    tmp_path: Path,
    stdout_value: bytes = b"",
    stderr_value: bytes = b"",
    exit_code: int = 0,
    *,
    provider: str = "claude",
    account_alias: str | None = "owner-primary",
    now: int = NOW,
) -> TerminalClassification:
    stdout = tmp_path / "stdout"
    stderr = tmp_path / "stderr"
    stdout.write_bytes(stdout_value)
    stderr.write_bytes(stderr_value)
    return classify_terminal_run(
        stdout,
        stderr,
        exit_code,
        provider=provider,
        account_alias=account_alias,
        now=now,
    )


def _measured_fixture(fixture: Path = FIXTURE) -> bytes:
    return (
        b"\n".join(
            line for line in fixture.read_bytes().splitlines() if line.startswith(b"{")
        )
        + b"\n"
    )


def test_measured_claude_wire_establishes_terminal_success(tmp_path: Path) -> None:
    observed = _classify(tmp_path, _measured_fixture())

    assert observed.telemetry is not None
    assert observed == TerminalClassification(
        phase=RunPhase.COMPLETED,
        final_text="Executed. Output: `fixture_test`",
        warning=None,
        marker=None,
        provider_wall=None,
        account_wall=None,
        tokens=165,
        telemetry=observed.telemetry,
    )


@settings(
    max_examples=40,
    database=None,
    deadline=None,
    suppress_health_check=(HealthCheck.function_scoped_fixture,),
)
@seed(0x15A5)
@given(marker=st.sampled_from(("DONE", "BLOCKED")), suffix=_VALID_IDENTIFIER)
def test_terminal_markers_are_a_closed_property(
    tmp_path: Path, marker: str, suffix: str
) -> None:
    observed = _classify(
        tmp_path,
        _ndjson(_result(text=f"{marker}: {suffix}", usage={})),
    )

    assert observed.phase is RunPhase.COMPLETED
    assert observed.marker == marker


@settings(
    max_examples=80,
    database=None,
    deadline=None,
    suppress_health_check=(HealthCheck.function_scoped_fixture,),
)
@seed(0x15A5)
@given(exit_code=st.integers(min_value=-255, max_value=255))
def test_exit_code_without_terminal_evidence_is_never_success(
    tmp_path: Path, exit_code: int
) -> None:
    observed = _classify(tmp_path, exit_code=exit_code)
    expected_warning = (
        f"child terminated by signal {-exit_code}"
        if exit_code < 0
        else f"child exited with code {exit_code} without terminal result"
    )
    expected_token = "child_signal" if exit_code < 0 else "no_terminal_result"

    assert observed == TerminalClassification(
        phase=RunPhase.FAILED,
        final_text=None,
        warning=expected_warning,
        marker=None,
        provider_wall=None,
        account_wall=None,
        tokens=0,
        telemetry=_fallback(expected_token),
        failure_reason=f"ClaudeRunFailed ({expected_token})",
    )


@settings(
    max_examples=60,
    database=None,
    deadline=None,
    suppress_health_check=(HealthCheck.function_scoped_fixture,),
)
@seed(0x15A5)
@given(
    delta=st.integers(min_value=1, max_value=10_000_000),
    limit_type=_VALID_IDENTIFIER,
)
def test_vendor_reset_derives_the_exact_account_wall(
    tmp_path: Path, delta: int, limit_type: str
) -> None:
    observed = _classify(
        tmp_path,
        _ndjson(_rate_limit(resets_at=NOW + delta, limit_type=limit_type)),
    )

    assert observed == TerminalClassification(
        phase=RunPhase.FAILED,
        final_text=None,
        warning=f"vendor rate limit rejected ({limit_type})",
        marker=None,
        provider_wall=None,
        account_wall=AccountWallRecord(
            account_alias="owner-primary",
            cooling_until=NOW + delta,
            source=WallSource.VENDOR_RESET,
            limit_type=limit_type,
            observed_at=NOW,
        ),
        tokens=0,
        telemetry=_fallback("vendor_reset"),
        failure_reason="ClaudeRunFailed (vendor_reset)",
    )


def test_vendor_rejection_without_reset_uses_one_hour_default(tmp_path: Path) -> None:
    observed = _classify(tmp_path, _ndjson(_rate_limit()))

    assert observed.account_wall == AccountWallRecord(
        account_alias="owner-primary",
        cooling_until=NOW + 3600,
        source=WallSource.DEFAULT_1H,
        limit_type="five_hour",
        observed_at=NOW,
    )


def test_allowed_rate_event_never_creates_a_wall(tmp_path: Path) -> None:
    observed = _classify(
        tmp_path,
        _ndjson(
            _rate_limit(status="allowed", resets_at=NOW + 100),
            _result(usage={"input_tokens": 2, "output_tokens": 3}),
        ),
    )

    assert observed.phase is RunPhase.COMPLETED
    assert observed.provider_wall is None
    assert observed.account_wall is None
    assert observed.tokens == 5


def test_nonterminal_result_is_a_failed_observation(tmp_path: Path) -> None:
    observed = _classify(
        tmp_path,
        _ndjson(
            _result(
                is_error=True,
                reason="error_during_execution",
                text="provider failed",
                usage={"output_tokens": 7},
            )
        ),
        exit_code=1,
    )

    assert observed == TerminalClassification(
        phase=RunPhase.FAILED,
        final_text="provider failed",
        warning="terminal result did not complete (error_during_execution)",
        marker=None,
        provider_wall=None,
        account_wall=None,
        tokens=7,
        telemetry=observed.telemetry,
        failure_reason="ClaudeRunFailed (terminal_incomplete)",
    )


@pytest.mark.parametrize("exit_code", [-9, 1, 255])
def test_process_failure_contradicting_terminal_success_is_refused(
    tmp_path: Path, exit_code: int
) -> None:
    with pytest.raises(TerminalClassificationError, match="contradicts"):
        _classify(tmp_path, _ndjson(_result()), exit_code=exit_code)


def test_vendor_rejection_contradicting_terminal_success_is_refused(
    tmp_path: Path,
) -> None:
    with pytest.raises(TerminalClassificationError, match="contradicts"):
        _classify(
            tmp_path,
            _ndjson(_rate_limit(resets_at=NOW + 1), _result()),
        )


def test_completed_error_result_is_refused_as_contradictory(tmp_path: Path) -> None:
    with pytest.raises(TerminalClassificationError, match="both completed"):
        _classify(tmp_path, _ndjson(_result(is_error=True)))


def test_terminal_success_without_text_is_refused(tmp_path: Path) -> None:
    with pytest.raises(TerminalClassificationError, match="no final result text"):
        _classify(tmp_path, _ndjson(_result(text=None)))


def test_vendor_wall_requires_the_executed_account_alias(tmp_path: Path) -> None:
    with pytest.raises(TerminalClassificationError, match="absent account alias"):
        _classify(
            tmp_path,
            _ndjson(_rate_limit(resets_at=NOW + 1)),
            account_alias=None,
        )


@pytest.mark.parametrize("resets_at", [0, NOW - 1, NOW])
def test_vendor_reset_must_follow_observation_time(
    tmp_path: Path, resets_at: int
) -> None:
    with pytest.raises(TerminalClassificationError, match="does not follow"):
        _classify(tmp_path, _ndjson(_rate_limit(resets_at=resets_at)))


@pytest.mark.parametrize(
    "stdout_value",
    [
        _ndjson({}),
        _ndjson({"type": None}),
        _ndjson(_result(is_error=1)),
        _ndjson(_result(reason=None)),
        _ndjson(_result(reason="")),
        _ndjson(_result(text=[])),
        _ndjson(_result(usage=[])),
        _ndjson(_result(usage={"input_tokens": True})),
        _ndjson(_result(usage={"input_tokens": "1"})),
        _ndjson(_result(usage={"input_tokens": -1})),
        _ndjson(_result(usage={"input_tokens": MAX_U64 + 1})),
        _ndjson(_result(usage={"output_tokens": -1})),
        _ndjson(_result(usage={"input_tokens": MAX_U64, "output_tokens": 1})),
        _ndjson({"type": "rate_limit_event"}),
        _ndjson({"type": "rate_limit_event", "rate_limit_info": []}),
        _ndjson(_rate_limit(status="unknown")),
        _ndjson(_rate_limit(status=[])),
        _ndjson(_rate_limit(resets_at=True)),
        _ndjson(_rate_limit(resets_at=-1)),
        _ndjson(_rate_limit(resets_at=MAX_U64 + 1)),
        _ndjson(_rate_limit(limit_type=1)),
        _ndjson(_rate_limit(limit_type="")),
        _ndjson(_rate_limit(limit_type=None)),
    ],
)
def test_malformed_terminal_frames_are_typed_refusals(
    tmp_path: Path, stdout_value: bytes
) -> None:
    with pytest.raises(TerminalClassificationError):
        _classify(tmp_path, stdout_value)


@pytest.mark.parametrize(
    "flattened_name",
    ["status", "resetsAt", "rateLimitType", "resets_at_seconds", "limit_type"],
)
def test_flattened_rate_limit_wire_is_refused(
    tmp_path: Path, flattened_name: str
) -> None:
    frame = _rate_limit()
    frame[flattened_name] = "flattened"

    with pytest.raises(TerminalClassificationError, match="flattened"):
        _classify(tmp_path, _ndjson(frame))


@pytest.mark.parametrize("stdout_value", [b"[]\n", b"null\n", b'"value"\n', b"1\n"])
def test_nonobject_stdout_json_is_refused(tmp_path: Path, stdout_value: bytes) -> None:
    with pytest.raises(TerminalClassificationError, match="not a JSON object"):
        _classify(tmp_path, stdout_value)


@pytest.mark.parametrize("stdout_value", [b"{\n", b"\xff\n", b"[[]\n"])
def test_malformed_stdout_bytes_are_refused(
    tmp_path: Path, stdout_value: bytes
) -> None:
    with pytest.raises(TerminalClassificationError, match="not valid bounded JSON"):
        _classify(tmp_path, stdout_value)


def test_the_last_terminal_result_is_the_run_terminal_one(tmp_path: Path) -> None:
    """This asserted a REFUSAL until 2026-08-29, when the 2.1.251 corpus showed
    the wire does it: a resumed run writes a second result into the same run
    directory (`num_turns` 158, then 2). Refusing filed a run whose own record
    said `completed` as `failed` — F-019."""
    observed = _classify(
        tmp_path, _ndjson(_result(text="first"), _result(text="second"))
    )
    assert observed.phase is RunPhase.COMPLETED
    assert observed.final_text == "second"


def test_rate_limit_events_are_snapshots_and_the_newest_wins(tmp_path: Path) -> None:
    """Also a refusal until the corpus was read: 24 of 130 runs carry more than
    one, up to 39 — the vendor emits roughly one per turn. The newest describes
    the wall as it stands when the run ends, which is what every consumer of it
    is asking."""
    observed = _classify(
        tmp_path,
        _ndjson(
            _rate_limit(status="allowed", limit_type=None),
            _rate_limit(status="allowed", limit_type=None),
            _result(),
        ),
    )
    assert observed.phase is RunPhase.COMPLETED


def test_conflicting_markers_are_refused(tmp_path: Path) -> None:
    with pytest.raises(TerminalClassificationError, match="conflicting markers"):
        _classify(tmp_path, _ndjson(_result(text="DONE\nBLOCKED")))


def test_blank_and_repeated_marker_lines_are_unambiguous(tmp_path: Path) -> None:
    observed = _classify(
        tmp_path,
        _ndjson(_result(text="\nnot-a-marker\nDONE\nDONE: repeated")),
    )

    assert observed.marker == "DONE"


@pytest.mark.parametrize(
    "stderr_value, message",
    [
        (b"{\n", "not valid bounded JSON"),
        (_ndjson({"error": []}), "not an object"),
        (
            _ndjson({"error": {"type": "rate_limit_error"}}),
            "no string message",
        ),
        (
            _ndjson({"error": {"type": "rate_limit_error", "message": 1}}),
            "no string message",
        ),
        (b"\xff\n", "not valid UTF-8"),
    ],
)
def test_malformed_stderr_evidence_is_a_typed_refusal(
    tmp_path: Path, stderr_value: bytes, message: str
) -> None:
    with pytest.raises(TerminalClassificationError, match=message):
        _classify(tmp_path, stderr_value=stderr_value)


def test_plain_and_unrelated_stderr_do_not_invent_a_wall(
    tmp_path: Path,
) -> None:
    stderr = b"diagnostic text\n" + _ndjson(
        {},
        {"error": {"type": "authentication_error", "message": "denied"}},
        {
            "error": {
                "type": "rate_limit_error",
                "message": "request quota exhausted",
            }
        },
    )
    observed = _classify(tmp_path, stderr_value=stderr)

    assert observed.phase is RunPhase.FAILED
    assert observed.provider_wall is None


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_missing_evidence_path_is_a_typed_refusal(tmp_path: Path, stream: str) -> None:
    stdout = tmp_path / "stdout"
    stderr = tmp_path / "stderr"
    stdout.write_bytes(b"")
    stderr.write_bytes(b"")
    (stdout if stream == "stdout" else stderr).unlink()

    with pytest.raises(TerminalClassificationError, match=f"{stream} evidence"):
        classify_terminal_run(
            stdout,
            stderr,
            0,
            provider="claude",
            account_alias=None,
            now=NOW,
        )


def test_invalid_path_value_is_a_typed_refusal(tmp_path: Path) -> None:
    stderr = tmp_path / "stderr"
    stderr.write_bytes(b"")

    with pytest.raises(TerminalClassificationError, match="could not be read"):
        classify_terminal_run(
            "\x00",
            stderr,
            0,
            provider="claude",
            account_alias=None,
            now=NOW,
        )


def test_combined_run_limit_is_enforced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(terminal_module, "MAX_RUN_OUTPUT_BYTES", 8)

    with pytest.raises(TerminalClassificationError, match="combined"):
        _classify(tmp_path, b"12345678", b"x")


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_per_line_limit_is_enforced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stream: str
) -> None:
    monkeypatch.setattr(terminal_module, "MAX_OUTPUT_LINE_BYTES", 4)
    stdout_value = b"12345" if stream == "stdout" else b""
    stderr_value = b"12345" if stream == "stderr" else b""

    with pytest.raises(TerminalClassificationError, match=f"{stream} line 1 exceeds"):
        _classify(tmp_path, stdout_value, stderr_value)


def test_durable_result_limit_is_enforced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(terminal_module, "MAX_DURABLE_OBSERVATION_BYTES", 4)

    with pytest.raises(TerminalClassificationError, match="result text exceeds"):
        _classify(tmp_path, _ndjson(_result(text="12345")))


@pytest.mark.parametrize(
    "provider",
    [
        cast(str, 1),
        "",
        " leading",
        "trailing ",
        "control\x00",
        "x" * (MAX_DURABLE_IDENTIFIER_BYTES + 1),
        "\ud800",
    ],
)
def test_invalid_executed_provider_is_a_typed_refusal(
    tmp_path: Path, provider: str
) -> None:
    with pytest.raises(TerminalClassificationError):
        _classify(tmp_path, provider=provider)


@pytest.mark.parametrize("account_alias", ["", " alias", "alias\x00", "\ud800"])
def test_invalid_executed_account_alias_is_a_typed_refusal(
    tmp_path: Path, account_alias: str
) -> None:
    with pytest.raises(TerminalClassificationError):
        _classify(tmp_path, account_alias=account_alias)


@pytest.mark.parametrize("now", [cast(int, True), cast(int, "1"), -1, MAX_U64 + 1])
def test_invalid_observation_epoch_is_a_typed_refusal(tmp_path: Path, now: int) -> None:
    with pytest.raises(TerminalClassificationError):
        _classify(tmp_path, now=now)


@pytest.mark.parametrize("exit_code", [cast(int, True), cast(int, "0")])
def test_invalid_exit_code_is_a_typed_refusal(tmp_path: Path, exit_code: int) -> None:
    with pytest.raises(TerminalClassificationError, match="exit code"):
        _classify(tmp_path, exit_code=exit_code)


def test_default_account_cooldown_overflow_is_refused(tmp_path: Path) -> None:
    with pytest.raises(TerminalClassificationError, match="cooldown overflows"):
        _classify(tmp_path, _ndjson(_rate_limit()), now=MAX_U64)


@pytest.mark.parametrize("cooling_until", [0, NOW - 1, NOW])
def test_provider_wall_refuses_nonfuture_cooldown(cooling_until: int) -> None:
    with pytest.raises(TerminalClassificationError, match="does not follow"):
        ProviderWallRecord(
            provider="claude",
            cooling_until=cooling_until,
            source=WallSource.DEFAULT_1H,
            observed_at=NOW,
        )


def test_provider_wall_refuses_non_enum_source() -> None:
    with pytest.raises(TerminalClassificationError, match="WallSource"):
        ProviderWallRecord(
            provider="claude",
            cooling_until=NOW + 1,
            source=cast(WallSource, "invalid_source"),
            observed_at=NOW,
        )


def test_account_wall_refuses_invalid_limit_type() -> None:
    with pytest.raises(TerminalClassificationError, match="limit_type"):
        AccountWallRecord(
            account_alias="owner-primary",
            cooling_until=NOW + 1,
            source=WallSource.VENDOR_RESET,
            limit_type="",
            observed_at=NOW,
        )


def test_account_wall_refuses_non_enum_source() -> None:
    with pytest.raises(TerminalClassificationError, match="WallSource"):
        AccountWallRecord(
            account_alias="owner-primary",
            cooling_until=NOW + 1,
            source=cast(WallSource, "vendor_reset"),
            limit_type="five_hour",
            observed_at=NOW,
        )


def _refusal_message(
    tmp_path: Path,
    stdout_value: bytes = b"",
    stderr_value: bytes = b"",
    exit_code: int = 0,
    *,
    provider: str = "claude",
    account_alias: str | None = "owner-primary",
    now: int = NOW,
) -> str:
    with pytest.raises(TerminalClassificationError) as raised:
        _classify(
            tmp_path,
            stdout_value,
            stderr_value,
            exit_code,
            provider=provider,
            account_alias=account_alias,
            now=now,
        )
    return raised.value.message


@pytest.mark.parametrize(
    "stdout_value, expected",
    [
        (b"{\n", "stdout line 1 is not valid bounded JSON"),
        (b"[]\n", "stdout line 1 is not a JSON object"),
        (_ndjson({}), "stdout line 1 carries no string type"),
        (_ndjson(_result(is_error=1)), "result.is_error is not a boolean"),
        (
            _ndjson(_result(reason=None)),
            "result.terminal_reason is not a string",
        ),
        (
            _ndjson(_result(text=[])),
            "result.result is neither text nor null",
        ),
        (_ndjson(_result(usage=[])), "result.usage is not an object"),
        (
            _ndjson(_result(usage={"input_tokens": True})),
            "result usage.input_tokens is not an integer",
        ),
        (
            _ndjson(_result(usage={"output_tokens": -1})),
            "result usage.output_tokens is outside the unsigned 64-bit range",
        ),
        (
            _ndjson(_result(usage={"input_tokens": MAX_U64, "output_tokens": 1})),
            "result token total exceeds unsigned 64-bit",
        ),
        (
            _ndjson(_rate_limit() | {"status": "flattened"}),
            "rate_limit_event carries flattened fields instead of rate_limit_info",
        ),
        (
            _ndjson({"type": "rate_limit_event"}),
            "rate_limit_info is not an object",
        ),
        (
            _ndjson(_rate_limit(status="unknown")),
            "rate_limit_info.status is not recognized",
        ),
        (
            _ndjson(_rate_limit(resets_at=True)),
            "rate_limit_info.resetsAt is not an integer epoch second",
        ),
        (
            _ndjson(_rate_limit(limit_type=1)),
            "rate_limit_info.rateLimitType is not a string",
        ),
        (
            _ndjson(_rate_limit(limit_type=None)),
            "rejected rate_limit_info carries no rateLimitType",
        ),
        (
            _ndjson(_result(text="DONE\nBLOCKED")),
            "terminal result carries conflicting markers",
        ),
    ],
)
def test_stdout_refusal_details_are_stable(
    tmp_path: Path, stdout_value: bytes, expected: str
) -> None:
    assert _refusal_message(tmp_path, stdout_value) == expected


@pytest.mark.parametrize(
    "stderr_value, expected",
    [
        (b"\xff\n", "stderr line 1 is not valid UTF-8"),
        (b"{\n", "stderr line 1 is not valid bounded JSON"),
        (_ndjson({"error": []}), "stderr error member is not an object"),
        (
            _ndjson({"error": {"type": "rate_limit_error"}}),
            "stderr rate_limit_error carries no string message",
        ),
    ],
)
def test_stderr_refusal_details_are_stable(
    tmp_path: Path, stderr_value: bytes, expected: str
) -> None:
    assert _refusal_message(tmp_path, stderr_value=stderr_value) == expected


@pytest.mark.parametrize(
    "provider, expected",
    [
        (cast(str, 1), "executed provider is not a string"),
        ("", "executed provider is empty"),
        (
            "x" * (MAX_DURABLE_IDENTIFIER_BYTES + 1),
            f"executed provider exceeds {MAX_DURABLE_IDENTIFIER_BYTES} bytes",
        ),
        ("control\x00", "executed provider is not a printable identifier"),
        ("\ud800", "executed provider is not valid UTF-8 text"),
    ],
)
def test_provider_refusal_details_are_stable(
    tmp_path: Path, provider: str, expected: str
) -> None:
    assert _refusal_message(tmp_path, provider=provider) == expected


def test_account_and_epoch_refusal_details_are_stable(tmp_path: Path) -> None:
    assert (
        _refusal_message(tmp_path, account_alias="\x00")
        == "executed account alias is not a printable identifier"
    )
    assert (
        _refusal_message(tmp_path, now=cast(int, True))
        == "observation time is not an integer epoch second"
    )
    assert (
        _refusal_message(tmp_path, now=-1)
        == "observation time is outside the unsigned 64-bit range"
    )
    assert (
        _refusal_message(tmp_path, exit_code=cast(int, True))
        == "exit code is not an integer"
    )


def test_classification_refusal_details_are_stable(tmp_path: Path) -> None:
    assert (
        _refusal_message(tmp_path, _ndjson(_result()), exit_code=1)
        == "terminal success contradicts process or cooldown evidence"
    )
    assert (
        _refusal_message(tmp_path, _ndjson(_result(is_error=True)))
        == "terminal result is both completed and marked as an error"
    )
    assert (
        _refusal_message(
            tmp_path,
            _ndjson(_rate_limit(resets_at=NOW + 1)),
            account_alias=None,
        )
        == "vendor rate limit cannot name an absent account alias"
    )
    assert (
        _refusal_message(tmp_path, _ndjson(_rate_limit()), now=MAX_U64)
        == "default account cooldown overflows epoch"
    )
    assert (
        _refusal_message(tmp_path, _ndjson(_result(text=None)))
        == "terminal success carries no final result text"
    )


def test_terminal_classification_error_preserves_its_detail() -> None:
    observed = TerminalClassificationError("measured detail")

    assert observed.message == "measured detail"


def test_exact_identifier_limit_is_accepted(tmp_path: Path) -> None:
    observed = _classify(tmp_path, provider="x" * MAX_DURABLE_IDENTIFIER_BYTES)

    assert observed.phase is RunPhase.FAILED


def test_exact_combined_limit_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(terminal_module, "MAX_RUN_OUTPUT_BYTES", 8)
    observed = _classify(tmp_path, stderr_value=b"12345678")

    assert observed.phase is RunPhase.FAILED


def test_exact_line_limit_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(terminal_module, "MAX_OUTPUT_LINE_BYTES", 4)
    observed = _classify(tmp_path, stderr_value=b"1234")

    assert observed.phase is RunPhase.FAILED


def test_exact_result_limit_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(terminal_module, "MAX_DURABLE_OBSERVATION_BYTES", 4)
    observed = _classify(tmp_path, _ndjson(_result(text="1234")))

    assert observed.final_text == "1234"


@pytest.mark.parametrize(
    "usage",
    [
        {"input_tokens": MAX_U64},
        {"input_tokens": MAX_U64 - 1, "output_tokens": 1},
    ],
)
def test_exact_token_limits_are_accepted(tmp_path: Path, usage: dict[str, int]) -> None:
    observed = _classify(tmp_path, _ndjson(_result(usage=usage)))

    assert observed.tokens == MAX_U64


def test_exact_cooldown_epoch_limits_are_accepted(tmp_path: Path) -> None:
    account = _classify(
        tmp_path,
        _ndjson(_rate_limit()),
        now=MAX_U64 - 3600,
    )
    assert account.account_wall is not None
    assert account.account_wall.cooling_until == MAX_U64


class _RecordingEvidence(io.BytesIO):
    def __init__(self, value: bytes, sizes: list[int | None]) -> None:
        super().__init__(value)
        self._sizes = sizes

    def read(self, size: int | None = -1) -> bytes:
        self._sizes.append(size)
        return super().read(size)


def test_external_reads_request_only_one_byte_beyond_the_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stdout = tmp_path / "stdout"
    stderr = tmp_path / "stderr"
    stdout.write_bytes(b"")
    stderr.write_bytes(b"")
    sizes: list[int | None] = []
    readers = iter((_RecordingEvidence(b"", sizes), _RecordingEvidence(b"", sizes)))

    def open_evidence(_path: Path, _mode: str) -> _RecordingEvidence:
        return next(readers)

    monkeypatch.setattr(Path, "open", open_evidence)
    classify_terminal_run(
        stdout,
        stderr,
        0,
        provider="claude",
        account_alias=None,
        now=NOW,
    )

    assert sizes == [terminal_module.MAX_RUN_OUTPUT_BYTES + 1] * 2


def test_failed_terminal_marker_survives_vendor_walls(tmp_path: Path) -> None:
    failed_result = _result(
        is_error=True,
        reason="error_during_execution",
        text="BLOCKED: unavailable",
        usage={"output_tokens": 7},
    )
    vendor = _classify(
        tmp_path,
        _ndjson(_rate_limit(resets_at=NOW + 1), failed_result),
    )

    assert vendor.final_text == "BLOCKED: unavailable"
    assert vendor.marker == "BLOCKED"
    assert vendor.tokens == 7


def test_marker_parser_uses_the_first_word(tmp_path: Path) -> None:
    observed = _classify(tmp_path, _ndjson(_result(text="DONE: two words")))

    assert observed.marker == "DONE"


@settings(
    max_examples=300,
    database=None,
    deadline=None,
    suppress_health_check=(HealthCheck.function_scoped_fixture,),
)
@seed(0x15A5)
@given(
    payload=st.one_of(
        st.sampled_from(FUZZ_CORPUS),
        st.binary(min_size=0, max_size=1024),
    )
)
def test_fuzzed_stdout_bytes_never_escape_as_an_untyped_exception(
    tmp_path: Path, payload: bytes
) -> None:
    try:
        _classify(tmp_path, stdout_value=payload)
    except TerminalClassificationError:
        return


@settings(
    max_examples=300,
    database=None,
    deadline=None,
    suppress_health_check=(HealthCheck.function_scoped_fixture,),
)
@seed(0x15A5)
@given(
    payload=st.one_of(
        st.sampled_from(FUZZ_CORPUS),
        st.binary(min_size=0, max_size=1024),
    )
)
def test_fuzzed_stderr_bytes_never_escape_as_an_untyped_exception(
    tmp_path: Path, payload: bytes
) -> None:
    try:
        _classify(tmp_path, stderr_value=payload)
    except TerminalClassificationError:
        return


_WINDOW_NAME = st.text(
    alphabet=st.sampled_from(tuple("abcdefghijklmnopqrstuvwxyz0123456789_")),
    min_size=1,
    max_size=12,
)
_WINDOW_BODY = st.fixed_dictionaries(
    {
        "utilization": st.floats(
            min_value=0.0, max_value=4.0, allow_nan=False, allow_infinity=False
        ),
        "resetsAt": st.integers(min_value=0, max_value=MAX_U64),
    }
)


def _window(
    utilization: object = 0.5, resets_at: object = NOW + 10
) -> dict[str, object]:
    return {"utilization": utilization, "resetsAt": resets_at}


def test_measured_2_1_250_wire_classifies_and_carries_both_windows(
    tmp_path: Path,
) -> None:
    observed = _classify(tmp_path, _measured_fixture(FIXTURE_250))

    assert observed.telemetry is not None
    assert observed == TerminalClassification(
        phase=RunPhase.COMPLETED,
        final_text="PONG",
        warning=None,
        marker=None,
        provider_wall=None,
        account_wall=None,
        tokens=100,
        usage=(
            WindowUsage(window="five_hour", utilization=0.16, resets_at=1_787_896_800),
            WindowUsage(window="seven_day", utilization=0.82, resets_at=1_788_130_800),
        ),
        telemetry=observed.telemetry,
    )


def test_allowed_warning_completes_without_building_any_wall(tmp_path: Path) -> None:
    observed = _classify(
        tmp_path,
        _ndjson(
            _rate_limit(
                status="allowed_warning",
                resets_at=NOW + 100,
                limit_type="seven_day",
                windows={"seven_day": _window(0.82, NOW + 100)},
            ),
            _result(),
        ),
    )

    assert observed.phase is RunPhase.COMPLETED
    assert observed.provider_wall is None
    assert observed.account_wall is None
    assert observed.usage == (
        WindowUsage(window="seven_day", utilization=0.82, resets_at=NOW + 100),
    )


def test_allowed_warning_on_a_failed_run_still_builds_no_wall(tmp_path: Path) -> None:
    observed = _classify(
        tmp_path,
        _ndjson(
            _rate_limit(
                status="allowed_warning",
                windows={"five_hour": _window(0.9, NOW + 5)},
            ),
            _result(is_error=True, reason="error_during_execution", text="nope"),
        ),
        exit_code=1,
    )

    assert observed.phase is RunPhase.FAILED
    assert observed.provider_wall is None
    assert observed.account_wall is None
    assert observed.usage == (
        WindowUsage(window="five_hour", utilization=0.9, resets_at=NOW + 5),
    )


def test_rejected_status_carries_windows_beside_its_account_wall(
    tmp_path: Path,
) -> None:
    observed = _classify(
        tmp_path,
        _ndjson(
            _rate_limit(
                status="rejected",
                resets_at=NOW + 60,
                windows={"five_hour": _window(1.0, NOW + 60)},
            )
        ),
        exit_code=1,
    )

    assert observed.account_wall is not None
    assert observed.usage == (
        WindowUsage(window="five_hour", utilization=1.0, resets_at=NOW + 60),
    )


def test_allowed_status_carries_windows_when_the_wire_sends_them(
    tmp_path: Path,
) -> None:
    observed = _classify(
        tmp_path,
        _ndjson(
            _rate_limit(status="allowed", windows={"five_hour": _window(0.1, NOW + 7)}),
            _result(),
        ),
    )

    assert observed.usage == (
        WindowUsage(window="five_hour", utilization=0.1, resets_at=NOW + 7),
    )


@pytest.mark.parametrize("exit_code", [-9, 3])
def test_windows_survive_a_run_that_produced_no_terminal_result(
    tmp_path: Path, exit_code: int
) -> None:
    observed = _classify(
        tmp_path,
        _ndjson(
            _rate_limit(status="allowed", windows={"five_hour": _window(0.4, NOW + 3)})
        ),
        exit_code=exit_code,
    )

    assert observed.phase is RunPhase.FAILED
    assert observed.usage == (
        WindowUsage(window="five_hour", utilization=0.4, resets_at=NOW + 3),
    )


def test_absent_unified_windows_is_no_observation_never_a_refusal(
    tmp_path: Path,
) -> None:
    observed = _classify(
        tmp_path, _ndjson(_rate_limit(status="allowed_warning"), _result())
    )

    assert observed.phase is RunPhase.COMPLETED
    assert observed.usage == ()


def test_empty_unified_windows_object_is_no_observation(tmp_path: Path) -> None:
    observed = _classify(
        tmp_path, _ndjson(_rate_limit(status="allowed", windows={}), _result())
    )

    assert observed.usage == ()


def test_the_exact_window_cap_is_accepted(tmp_path: Path) -> None:
    windows = {f"w{index:02d}": _window() for index in range(MAX_USAGE_WINDOWS)}

    observed = _classify(
        tmp_path, _ndjson(_rate_limit(status="allowed", windows=windows), _result())
    )

    assert len(observed.usage) == MAX_USAGE_WINDOWS


@pytest.mark.parametrize(
    "stdout_value, expected",
    [
        (
            _ndjson(
                {
                    "type": "rate_limit_event",
                    "rate_limit_info": {"status": "allowed", "unifiedWindows": None},
                }
            ),
            "rate_limit_info.unifiedWindows is not an object",
        ),
        (
            _ndjson(_rate_limit(status="allowed", windows=[])),
            "rate_limit_info.unifiedWindows is not an object",
        ),
        (
            _ndjson(
                _rate_limit(
                    status="allowed",
                    windows={
                        f"w{index:02d}": _window()
                        for index in range(MAX_USAGE_WINDOWS + 1)
                    },
                )
            ),
            (
                "rate_limit_info.unifiedWindows carries more than "
                f"{MAX_USAGE_WINDOWS} windows"
            ),
        ),
        (
            _ndjson(_rate_limit(status="allowed", windows={"": _window()})),
            "rate_limit_info.unifiedWindows window name is empty",
        ),
        (
            _ndjson(_rate_limit(status="allowed", windows={" five_hour": _window()})),
            "rate_limit_info.unifiedWindows window name is not a printable identifier",
        ),
        (
            _ndjson(_rate_limit(status="allowed", windows={"five_hour": []})),
            "rate_limit_info.unifiedWindows.five_hour is not an object",
        ),
        (
            _ndjson(
                _rate_limit(status="allowed", windows={"five_hour": {"resetsAt": 1}})
            ),
            "rate_limit_info.unifiedWindows.five_hour.utilization is not a number",
        ),
        (
            _ndjson(
                _rate_limit(status="allowed", windows={"five_hour": _window(True, 1)})
            ),
            "rate_limit_info.unifiedWindows.five_hour.utilization is not a number",
        ),
        (
            _ndjson(
                _rate_limit(
                    status="allowed", windows={"five_hour": _window(float("inf"), 1)}
                )
            ),
            "rate_limit_info.unifiedWindows.five_hour.utilization is not finite",
        ),
        (
            _ndjson(
                _rate_limit(
                    status="allowed", windows={"five_hour": _window(float("nan"), 1)}
                )
            ),
            "rate_limit_info.unifiedWindows.five_hour.utilization is not finite",
        ),
        (
            _ndjson(
                _rate_limit(status="allowed", windows={"five_hour": _window(-0.1, 1)})
            ),
            "rate_limit_info.unifiedWindows.five_hour.utilization is negative",
        ),
        (
            _ndjson(
                _rate_limit(
                    status="allowed", windows={"five_hour": {"utilization": 0.5}}
                )
            ),
            (
                "rate_limit_info.unifiedWindows.five_hour.resetsAt "
                "is not an integer epoch second"
            ),
        ),
        (
            _ndjson(
                _rate_limit(status="allowed", windows={"five_hour": _window(0.5, True)})
            ),
            (
                "rate_limit_info.unifiedWindows.five_hour.resetsAt "
                "is not an integer epoch second"
            ),
        ),
        (
            _ndjson(
                _rate_limit(status="allowed", windows={"five_hour": _window(0.5, -1)})
            ),
            (
                "rate_limit_info.unifiedWindows.five_hour.resetsAt "
                "is outside the unsigned 64-bit range"
            ),
        ),
        (
            _ndjson(_rate_limit(status="throttled")),
            "rate_limit_info.status is not recognized",
        ),
    ],
)
def test_unified_window_refusal_details_are_stable(
    tmp_path: Path, stdout_value: bytes, expected: str
) -> None:
    assert _refusal_message(tmp_path, stdout_value) == expected


@settings(
    max_examples=60,
    database=None,
    deadline=None,
    suppress_health_check=(HealthCheck.function_scoped_fixture,),
)
@seed(0x15A5)
@given(
    windows=st.dictionaries(
        _WINDOW_NAME, _WINDOW_BODY, min_size=1, max_size=MAX_USAGE_WINDOWS
    ),
    data=st.data(),
)
def test_window_order_never_follows_the_wire_key_order(
    tmp_path: Path, windows: dict[str, dict[str, float | int]], data: st.DataObject
) -> None:
    order = data.draw(st.permutations(sorted(windows)))
    shuffled = {name: windows[name] for name in order}

    observed = _classify(
        tmp_path,
        _ndjson(_rate_limit(status="allowed_warning", windows=shuffled), _result()),
    )

    assert observed.usage == tuple(
        WindowUsage(
            window=name,
            utilization=float(windows[name]["utilization"]),
            resets_at=int(windows[name]["resetsAt"]),
        )
        for name in sorted(windows)
    )


@settings(max_examples=60, database=None, deadline=None)
@seed(0x15A5)
@given(
    utilization=st.floats(
        min_value=0.0, max_value=1_000.0, allow_nan=False, allow_infinity=False
    )
)
def test_every_finite_nonnegative_utilization_is_accepted(utilization: float) -> None:
    assert WindowUsage(
        window="five_hour", utilization=utilization, resets_at=NOW
    ).utilization == pytest.approx(utilization)


def test_overage_utilization_above_one_is_accepted() -> None:
    assert WindowUsage("seven_day", 1.4, NOW).utilization == 1.4


@pytest.mark.parametrize(
    "utilization, expected",
    [
        (True, "usage utilization is not a number"),
        ("0.5", "usage utilization is not a number"),
        (None, "usage utilization is not a number"),
        (float("inf"), "usage utilization is not finite"),
        (float("nan"), "usage utilization is not finite"),
        (-0.5, "usage utilization is negative"),
    ],
)
def test_window_usage_refuses_an_illegal_utilization(
    utilization: object, expected: str
) -> None:
    with pytest.raises(TerminalClassificationError) as raised:
        WindowUsage("five_hour", cast(float, utilization), NOW)
    assert raised.value.message == expected


def test_the_measured_wire_yields_every_field_classification_used_to_drop(
    tmp_path: Path,
) -> None:
    observed = _classify(tmp_path, _measured_fixture())
    telemetry = observed.telemetry
    assert telemetry is not None

    assert telemetry.cost_usd == "0.12205099999999999"
    assert isinstance(telemetry.cost_usd, str)

    assert telemetry.duration_ms == 4456
    assert telemetry.duration_api_ms == 5086
    assert telemetry.num_turns == 2
    assert telemetry.ttft_ms == 1785
    assert telemetry.ttft_stream_ms == 1075
    assert telemetry.time_to_request_ms == 94
    assert telemetry.stop_reason == "end_turn"
    assert telemetry.api_error_status is None
    assert telemetry.permission_denials == 0
    assert telemetry.subagent_spawned == 0
    assert len(telemetry.model_usage) == 2


@pytest.mark.parametrize("cost, expected", [(None, None), (0, "0.0"), (0.125, "0.125")])
def test_terminal_cost_preserves_unknown_and_known_zero(
    tmp_path: Path, cost: float | None, expected: str | None
) -> None:
    frame = _result()
    if cost is not None:
        frame["total_cost_usd"] = cost

    telemetry = _classify(tmp_path, _ndjson(frame)).telemetry

    assert telemetry is not None
    assert telemetry.cost_usd == expected
    assert telemetry.as_payload()["cost_usd"] == expected


def test_input_tokens_are_the_gen_ai_total_with_cache_as_a_subset(
    tmp_path: Path,
) -> None:
    telemetry = _classify(tmp_path, _measured_fixture()).telemetry
    assert telemetry is not None

    assert telemetry.cache_read_input_tokens == 40688
    assert telemetry.cache_creation_input_tokens == 9669
    assert telemetry.output_tokens == 161
    assert telemetry.input_tokens == 50361
    assert telemetry.input_tokens == (
        4 + telemetry.cache_read_input_tokens + telemetry.cache_creation_input_tokens
    )


def test_the_model_recorded_is_the_one_that_answered_not_the_one_requested(
    tmp_path: Path,
) -> None:
    frame = _result(
        usage={"input_tokens": 10, "output_tokens": 10},
    )
    frame["modelUsage"] = {
        "claude-haiku": {
            "canonicalModel": "claude-haiku-4-5",
            "provider": "anthropic",
            "inputTokens": 4,
            "outputTokens": 1,
            "cacheReadInputTokens": 0,
            "cacheCreationInputTokens": 0,
            "costUSD": 0.0001,
        },
        "claude-opus": {
            "canonicalModel": "claude-opus-5",
            "provider": "anthropic",
            "inputTokens": 6,
            "outputTokens": 9,
            "cacheReadInputTokens": 0,
            "cacheCreationInputTokens": 0,
            "costUSD": 0.1,
        },
    }
    observed = _classify(tmp_path, _ndjson(frame))
    assert observed.telemetry is not None
    assert observed.telemetry.dominant_model is not None
    assert observed.telemetry.dominant_model.canonical_model == "claude-opus-5"
    payload = observed.telemetry.as_payload()
    assert payload["model"] == "claude-opus-5"
    assert payload["provider"] == "anthropic"


def test_a_run_that_never_reported_still_carries_a_journal_telemetry(
    tmp_path: Path,
) -> None:
    observed = _classify(tmp_path, exit_code=1)
    telemetry = observed.telemetry
    assert telemetry is not None
    assert telemetry.cost_usd is None
    assert telemetry.input_tokens == 0
    assert telemetry.output_tokens == 0
    assert telemetry.model_usage == ()
    assert telemetry.dominant_model is None
    assert telemetry.failure == "no_terminal_result"


def test_the_failure_token_never_carries_free_prose(tmp_path: Path) -> None:
    for code, expected_token in (
        (-9, "child_signal"),
        (1, "no_terminal_result"),
    ):
        observed = _classify(tmp_path, exit_code=code)
        assert observed.telemetry is not None
        assert observed.telemetry.failure == expected_token


def test_a_failure_that_did_report_still_carries_its_closed_token(
    tmp_path: Path,
) -> None:
    frame = {
        "type": "result",
        "is_error": True,
        "terminal_reason": "error",
        "result": None,
        "total_cost_usd": 0.5,
        "duration_ms": 10,
        "duration_api_ms": 9,
        "num_turns": 1,
        "usage": {
            "input_tokens": 10,
            "output_tokens": 2,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        },
    }
    wall = {
        "type": "rate_limit_event",
        "rate_limit_info": {
            "status": "rejected",
            "resetsAt": 1_800_003_600,
            "rateLimitType": "five_hour",
        },
    }
    walled = _classify(tmp_path, _ndjson(wall, frame), exit_code=1)
    assert walled.phase is RunPhase.FAILED
    assert walled.telemetry is not None
    assert walled.telemetry.failure == "vendor_reset"
    assert walled.telemetry.cost_usd == "0.5"
    assert walled.failure_reason == "ClaudeRunFailed (vendor_reset)"


@pytest.mark.parametrize(
    "window, expected",
    [
        ("", "usage window is empty"),
        (" five_hour", "usage window is not a printable identifier"),
        (1, "usage window is not a string"),
    ],
)
def test_window_usage_refuses_an_illegal_window_name(
    window: object, expected: str
) -> None:
    with pytest.raises(TerminalClassificationError) as raised:
        WindowUsage(cast(str, window), 0.5, NOW)

    assert raised.value.message == expected


def test_terminal_unit_parsers_edge_cases() -> None:
    # _clean_str
    assert terminal_module._clean_str(None) is None
    assert terminal_module._clean_str("   \n\t  ") is None
    assert terminal_module._clean_str("a" * 200, max_len=10) == "a" * 10

    # _money
    with pytest.raises(TerminalClassificationError, match="not a number"):
        terminal_module._money("c", True)
    with pytest.raises(TerminalClassificationError, match="not a number"):
        terminal_module._money("c", "str")
    with pytest.raises(TerminalClassificationError, match="not a finite number"):
        terminal_module._money("c", float("inf"))
    with pytest.raises(TerminalClassificationError, match="negative"):
        terminal_module._money("c", -1.0)

    # _counter
    with pytest.raises(TerminalClassificationError, match="not an integer"):
        terminal_module._counter({"n": True}, "n")
    with pytest.raises(TerminalClassificationError, match="not an integer"):
        terminal_module._counter({"n": "str"}, "n")
    with pytest.raises(TerminalClassificationError, match="outside unsigned"):
        terminal_module._counter({"n": -1}, "n")

    # _model_usage
    with pytest.raises(TerminalClassificationError, match="not an object"):
        terminal_module._model_usage({"modelUsage": "not-dict"})
    with pytest.raises(TerminalClassificationError, match="more than"):
        terminal_module._model_usage({"modelUsage": {f"m_{i}": {} for i in range(35)}})
    with pytest.raises(TerminalClassificationError, match="not an object"):
        terminal_module._model_usage({"modelUsage": {"m": "not-dict"}})


@pytest.mark.parametrize(
    "resets_at, expected",
    [
        (True, "usage resets_at is not an integer epoch second"),
        (0.5, "usage resets_at is not an integer epoch second"),
        (-1, "usage resets_at is outside the unsigned 64-bit range"),
        (MAX_U64 + 1, "usage resets_at is outside the unsigned 64-bit range"),
    ],
)
def test_window_usage_refuses_an_illegal_reset_epoch(
    resets_at: object, expected: str
) -> None:
    with pytest.raises(TerminalClassificationError) as raised:
        WindowUsage("five_hour", 0.5, cast(int, resets_at))

    assert raised.value.message == expected


def test_empty_result_text_is_text(tmp_path: Path) -> None:
    """An empty final result is an ANSWER, not a malformed frame.

    Measured 2026-08-29, run 53b0475c: the `agy` route answered `OK` in its
    assistant frame, the vendor reported `is_error=false`,
    `terminal_reason=completed` and 367 output tokens — and `result.result`
    arrived as `""` because the gemini carrier appends a trailing
    thinking-only message, so the SDK's final text is empty. The guard read
    `isinstance(raw_text, str) and raw_text`, so the empty string fell through
    to a refusal whose own message called it "neither text nor null". It is
    text.
    """
    observed = _classify(tmp_path, _ndjson(_result(text="", usage={})))

    assert observed.phase == RunPhase.COMPLETED
    assert observed.final_text == ""

    # The guard that must SURVIVE: a non-string result is still refused.
    with pytest.raises(TerminalClassificationError, match="neither text nor null"):
        _classify(tmp_path, _ndjson(_result(text=[])))


# U3 — terminal.py falls back to last assistant frame text when result.result
# is empty/null. The fallback is a LAST-RESORT, not a silent rewrite: a stream
# whose only frames are an empty result and no later assistant frame MUST
# still raise.


def _assistant_text(text: object) -> dict[str, object]:
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }


def test_classify_falls_back_to_assistant_text_when_result_is_null(
    tmp_path: Path,
) -> None:
    """A success result with `result: null` falls back to the last assistant text."""
    last_text = "the recoverable answer from the assistant frame"
    stream = _ndjson(
        _assistant_text("first assistant frame, partial"),
        _assistant_text("second assistant frame, partial"),
        _result(text=None),  # null result — vendor saw no terminal text
        _assistant_text(last_text),
    )

    observed = _classify(tmp_path, stream)

    assert observed.phase == RunPhase.COMPLETED
    assert observed.final_text == last_text


def test_classify_without_assistant_fallback_still_raises(tmp_path: Path) -> None:
    """The fallback is LAST-RESORT, not silent: only null result, no assistant = refusal."""
    stream = _ndjson(_result(text=None))

    with pytest.raises(TerminalClassificationError, match="no final result text"):
        _classify(tmp_path, stream)


# U-failover — a rejected credential must be distinguishable from any other
# api_error, because the supervisor's account failover decision keys on it.


def test_classification_carries_the_api_error_status(tmp_path: Path) -> None:
    """A result frame carrying `api_error_status: 401` surfaces it on the
    classification, so a caller can tell 'credential rejected' from any other
    vendor failure without re-parsing the stream."""
    frame: dict[str, object] = {
        "type": "result",
        "is_error": True,
        "terminal_reason": "api_error",
        "result": "Failed to authenticate. API Error: 401 token expired",
        "api_error_status": 401,
    }
    observed = _classify(tmp_path, _ndjson(frame))

    assert observed.phase is RunPhase.FAILED
    assert observed.api_error_status == 401


def test_classification_without_api_error_status_stays_none(tmp_path: Path) -> None:
    """A clean result carries no api_error_status — None, never 0 or a sentinel."""
    observed = _classify(tmp_path, _ndjson(_result(text="OK", usage={})))

    assert observed.phase is RunPhase.COMPLETED
    assert observed.api_error_status is None
