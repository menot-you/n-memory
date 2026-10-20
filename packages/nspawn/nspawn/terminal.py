"""Classify bounded Claude stream-json evidence and derive cooldown walls."""

from __future__ import annotations

import json
import math
import os
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Final, cast

from .admission import AttemptKey
from .errors import NERR_1117, NSpawnError
from .limits import MAX_DURABLE_IDENTIFIER_BYTES, MAX_DURABLE_OBSERVATION_BYTES
from .pump import MAX_OUTPUT_LINE_BYTES, MAX_RUN_OUTPUT_BYTES
from .rows import MARKER_VALUES, RunPhase

_DEFAULT_ACCOUNT_COOLDOWN_SECONDS: Final = 60 * 60
_MAX_U64: Final = 2**64 - 1

#: Cardinality ceiling for `modelUsage`. One label per model becomes a series
#: in the metrics system, and unbounded cardinality is the classic way for an
#: observability system to bring down the host it observes.
_MAX_MODEL_USAGE_ENTRIES: Final = 32


class WallSource(Enum):
    """Closed provenance vocabulary stored in both wall tables."""

    VENDOR_RESET = "vendor_reset"
    DEFAULT_1H = "default_1h"


class TerminalClassificationError(NSpawnError):
    """The terminal evidence could not be classified safely."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(NERR_1117, message)


def _utf8(name: str, value: str) -> bytes:
    try:
        return value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise TerminalClassificationError(f"{name} is not valid UTF-8 text") from error


def _identifier(name: str, value: object) -> str:
    if not isinstance(value, str):
        raise TerminalClassificationError(f"{name} is not a string")
    encoded = _utf8(name, value)
    if not encoded:
        raise TerminalClassificationError(f"{name} is empty")
    if len(encoded) > MAX_DURABLE_IDENTIFIER_BYTES:
        raise TerminalClassificationError(
            f"{name} exceeds {MAX_DURABLE_IDENTIFIER_BYTES} bytes"
        )
    if value != value.strip() or not value.isprintable():
        raise TerminalClassificationError(f"{name} is not a printable identifier")
    return value


def _clean_str(value: str | None, max_len: int = 128) -> str | None:
    """Normalizes a string to ensure it is printable, without newlines, and bounded."""
    if value is None:
        return None
    cleaned = "".join(ch for ch in value if ch.isprintable()).strip()
    if not cleaned:
        return None
    encoded = cleaned.encode("utf-8")
    if len(encoded) <= max_len:
        return cleaned
    return encoded[:max_len].decode("utf-8", errors="ignore")


def _epoch(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TerminalClassificationError(f"{name} is not an integer epoch second")
    if not 0 <= value <= _MAX_U64:
        raise TerminalClassificationError(
            f"{name} is outside the unsigned 64-bit range"
        )
    return value


def _utilization(name: str, value: object) -> float:
    """A vendor utilization fraction, as the wire spells it.

    There is deliberately NO upper bound: `isUsingOverage` makes a value above
    1.0 legal, and inventing a ceiling the vendor never promised would turn a
    real overage reading into a refusal.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TerminalClassificationError(f"{name} is not a number")
    number = float(value)
    if not math.isfinite(number):
        raise TerminalClassificationError(f"{name} is not finite")
    if number < 0.0:
        raise TerminalClassificationError(f"{name} is negative")
    return number


#: A vendor window set is vendor-owned and already changed once: 2.1.241 carried
#: none, 2.1.250 carries two. The cap bounds an untrusted parse, never the
#: vocabulary.
MAX_USAGE_WINDOWS: Final = 16


@dataclass(frozen=True, slots=True)
class WindowUsage:
    """One vendor-named rate-limit window, exactly as the wire spells it."""

    window: str
    utilization: float
    resets_at: int

    def __post_init__(self) -> None:
        _identifier("usage window", self.window)
        _utilization("usage utilization", self.utilization)
        _epoch("usage resets_at", self.resets_at)


@dataclass(frozen=True, slots=True)
class ProviderWallRecord:
    provider: str
    cooling_until: int
    source: WallSource
    observed_at: int

    def __post_init__(self) -> None:
        _identifier("provider wall provider", self.provider)
        observed_at = _epoch("provider wall observed_at", self.observed_at)
        cooling_until = _epoch("provider wall cooling_until", self.cooling_until)
        if cooling_until <= observed_at:
            raise TerminalClassificationError(
                "provider wall cooling_until does not follow observed_at"
            )
        if not isinstance(self.source, WallSource):
            raise TerminalClassificationError(
                "provider wall source is not a WallSource"
            )


@dataclass(frozen=True, slots=True)
class AccountWallRecord:
    account_alias: str
    cooling_until: int
    source: WallSource
    limit_type: str
    observed_at: int

    def __post_init__(self) -> None:
        _identifier("account wall alias", self.account_alias)
        _identifier("account wall limit_type", self.limit_type)
        observed_at = _epoch("account wall observed_at", self.observed_at)
        cooling_until = _epoch("account wall cooling_until", self.cooling_until)
        if cooling_until <= observed_at:
            raise TerminalClassificationError(
                "account wall cooling_until does not follow observed_at"
            )
        if not isinstance(self.source, WallSource):
            raise TerminalClassificationError("account wall source is not a WallSource")


SUBSCRIPTION_WINDOWS: Final = frozenset(
    {
        "five_hour",
        "seven_day",
        "seven_day_opus",
        "seven_day_sonnet",
        "seven_day_overage_included",
    }
)


#: The error the CLI names when the organization refuses the OAuth account
#: (2.1.257, 2.1.304): the account cannot serve, whatever its quota says.
ORG_REFUSAL: Final = "oauth_org_not_allowed"
#: How long a refused account is walled when the vendor names no reset: long
#: enough that the next dispatches go elsewhere, short enough that a fixed
#: account serves again soon (`Supervisor.CREDENTIAL_REJECTED_WALL_S`).
ACCOUNT_REFUSED_WALL_S: Final = 300


def _main_api_error(message: object, error: str, native_session: str) -> bool:
    """Whether `message` is the main turn's API error `error`, in either
    spelling the CLI gives the flag (`isApiError`, `is_api_error_message`)."""
    return (
        isinstance(message, dict)
        and message.get("type") == "assistant"
        and (
            message.get("isApiError") is True
            or message.get("is_api_error_message") is True
        )
        and message.get("error") == error
        and message.get("session_id") == native_session
        and "parent_tool_use_id" in message
        and message["parent_tool_use_id"] is None
    )


def subscription_limit(
    failure: Mapping[str, object],
    *,
    account_alias: str,
    key: AttemptKey,
    profile_digest: str,
    stop_epoch: int,
    native_session: str,
    now: int,
) -> AccountWallRecord | None:
    """A causal main-turn rejection, never an auxiliary event or generic prose.

    The host supplies its registered native attempt coordinates. Failure and
    rate carriers must name those same coordinates; a terminal main-loop limit
    or categorical main API rejection establishes causality before a wall exists.
    A main API rejection by the organization (`ORG_REFUSAL`) walls the account
    for `ACCOUNT_REFUSED_WALL_S` as a rejected credential.
    """
    if (
        failure.get("version") != 1
        or failure.get("key") != key.data()
        or failure.get("profile_digest") != profile_digest
        or failure.get("stop_epoch") != stop_epoch
    ):
        return None
    terminal = failure.get("terminal")
    if (
        not isinstance(terminal, dict)
        or terminal.get("type") != "result"
        or terminal.get("is_error") is not True
        or terminal.get("session_id") != native_session
        or terminal.get("parent_tool_use_id") is not None
    ):
        return None
    if not isinstance(terminal.get("subtype"), str) or terminal["subtype"] not in {
        "success",
        "error_during_execution",
    }:
        return None
    messages = failure.get("messages")
    if not isinstance(messages, list):
        return None
    reason = terminal.get("terminal_reason")
    if reason is not None and not isinstance(reason, str):
        return None
    if reason in {None, "api_error"} and any(
        _main_api_error(message, ORG_REFUSAL, native_session) for message in messages
    ):
        return AccountWallRecord(
            account_alias,
            now + ACCOUNT_REFUSED_WALL_S,
            WallSource.DEFAULT_1H,
            "credential_rejected",
            now,
        )
    main_rejection = any(
        _main_api_error(message, "rate_limit", native_session) for message in messages
    )
    if reason != "blocking_limit" and not (
        reason in {None, "api_error"} and main_rejection
    ):
        return None
    candidates: list[AccountWallRecord] = []
    rates = failure.get("rate_events", [])
    if not isinstance(rates, list):
        return None
    for message in [*messages, *rates]:
        if (
            not isinstance(message, dict)
            or message.get("type") != "rate_limit_event"
            or message.get("session_id") != native_session
            or message.get("parent_tool_use_id") is not None
            or message.get("isSidechain") is True
        ):
            continue
        info = message.get("rate_limit_info")
        if (
            not isinstance(info, dict)
            or info.get("status") != "rejected"
            or not isinstance(info.get("rateLimitType"), str)
            or info["rateLimitType"] not in SUBSCRIPTION_WINDOWS
            or info.get("errorCode") == "credits_required"
            or info.get("isUsingOverage") is True
            or info.get("overageInUse") is True
        ):
            continue
        reset = info.get("resetsAt")
        if (
            isinstance(reset, bool)
            or not isinstance(reset, int)
            or not now < reset <= 2**53 - 1
        ):
            continue
        candidates.append(
            AccountWallRecord(
                account_alias,
                reset,
                WallSource.VENDOR_RESET,
                info["rateLimitType"],
                now,
            )
        )
    return max(candidates, key=lambda wall: wall.cooling_until) if candidates else None


@dataclass(frozen=True, slots=True)
class ModelUsage:
    """The accounting of ONE model, as the child itself declared it.

    `canonical_model` is the model that ACTUALLY responded, never what the caller
    requested. The vendor's synthetic usage-limit result names neither it nor the
    provider: no model responded.
    """

    wire_name: str
    canonical_model: str | None
    provider: str | None
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int
    cache_creation_input_tokens: int
    cost_usd: str


@dataclass(frozen=True, slots=True)
class RunTelemetry:
    """Everything the `result` frame carries that classification used to discard.

    `input_tokens` follows the gen_ai convention: it is the TOTAL, with cache
    read and creation as subsets of it.
    """

    cost_usd: str | None
    input_tokens: int | None
    output_tokens: int | None
    cache_read_input_tokens: int | None
    cache_creation_input_tokens: int | None
    duration_ms: int
    duration_api_ms: int | None
    num_turns: int
    ttft_ms: int | None
    ttft_stream_ms: int | None
    time_to_request_ms: int | None
    stop_reason: str | None
    api_error_status: int | None
    permission_denials: int | None
    subagent_spawned: int | None
    model_usage: tuple[ModelUsage, ...]
    failure: str | None = None
    witnessed_effect: str | None = None
    rate_limit_status: str | None = None
    rate_limit_resets_at: int | None = None
    rate_limit_type: str | None = None

    def as_payload(self) -> dict[str, object]:
        """The durable shape stored in `nspawn.run_events`."""
        dominant = self.dominant_model
        return {
            "cost_usd": self.cost_usd,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
            "cache_write_input_tokens": self.cache_creation_input_tokens,
            "duration_ms": self.duration_ms,
            "duration_api_ms": self.duration_api_ms,
            "num_turns": self.num_turns,
            "ttft_ms": self.ttft_ms,
            "ttft_stream_ms": self.ttft_stream_ms,
            "time_to_request_ms": self.time_to_request_ms,
            "stop_reason": _clean_str(self.stop_reason),
            "api_error_status": self.api_error_status,
            "permission_denials": self.permission_denials,
            "subagent_spawned": self.subagent_spawned,
            "model": None if dominant is None else _clean_str(dominant.canonical_model),
            "provider": None if dominant is None else _clean_str(dominant.provider),
            "model_usage": [
                {
                    "wire_name": _clean_str(m.wire_name) or "",
                    "canonical_model": _clean_str(m.canonical_model) or "",
                    "provider": _clean_str(m.provider) or "",
                    "input_tokens": m.input_tokens,
                    "output_tokens": m.output_tokens,
                    "cache_read_input_tokens": m.cache_read_input_tokens,
                    "cache_creation_input_tokens": m.cache_creation_input_tokens,
                    "cache_write_input_tokens": m.cache_creation_input_tokens,
                    "cost_usd": m.cost_usd,
                }
                for m in self.model_usage
            ],
            "failure": _clean_str(self.failure),
            "witnessed_effect": _clean_str(self.witnessed_effect),
            "rate_limit_status": _clean_str(self.rate_limit_status),
            "rate_limit_resets_at": self.rate_limit_resets_at,
            "rate_limit_type": _clean_str(self.rate_limit_type),
        }

    @property
    def dominant_model(self) -> ModelUsage | None:
        """The model that produced the output, never the background model."""
        if not self.model_usage:
            return None
        return max(self.model_usage, key=lambda model: model.output_tokens)


@dataclass(frozen=True, slots=True)
class BrokenHook:
    """A hook of the child that exited outside {0, 2}.

    0 is allow and 2 is block — the two codes the client itself acts on. Any
    other exit is a hook nobody wrote deliberately, and it used to die inside
    the child's stdout with no reader.
    """

    hook_name: str
    hook_event: str
    exit_code: int
    detail: str | None

    def __post_init__(self) -> None:
        if self.exit_code in _HANDLED_HOOK_EXITS:
            raise TerminalClassificationError(
                f"hook exit {self.exit_code} is handled by the client, not broken"
            )


@dataclass(frozen=True, slots=True)
class TerminalClassification:
    """Terminal result derived from both redacted streams and process status."""

    phase: RunPhase
    final_text: str | None
    warning: str | None
    marker: str | None
    provider_wall: ProviderWallRecord | None
    account_wall: AccountWallRecord | None
    tokens: int = 0
    usage: tuple[WindowUsage, ...] = ()
    api_error_status: int | None = None
    telemetry: RunTelemetry | None = None
    broken_hooks: tuple[BrokenHook, ...] = ()
    #: Why a FAILED run failed, typed: an error class and codes, never vendor
    #: prose, which stays in `warning`. It becomes the run's `failure_reason`.
    failure_reason: str | None = None
    #: The error the CLI names on its own API-error message, such as
    #: `oauth_org_not_allowed`: an identifier, never vendor prose.
    vendor_error: str | None = None


@dataclass(frozen=True, slots=True)
class _TerminalResult:
    is_error: bool
    reason: str
    text: str | None
    marker: str | None
    tokens: int
    telemetry: RunTelemetry
    api_error_status: int | None = None
    structured_output: dict[str, object] | None = None
    vendor_error: str | None = None


@dataclass(frozen=True, slots=True)
class _AllowedRateLimit:
    """An allowed limit carries rate limit data."""

    resets_at: int | None = None
    limit_type: str | None = None
    windows: tuple[WindowUsage, ...] = ()


@dataclass(frozen=True, slots=True)
class _RejectedRateLimit:
    resets_at: int | None
    limit_type: str
    windows: tuple[WindowUsage, ...] = ()


type _RateLimit = _AllowedRateLimit | _RejectedRateLimit


_HANDLED_HOOK_EXITS: Final = frozenset({0, 2})


def _read_evidence(path: Path | str, stream: str, maximum: int) -> bytes:
    try:
        with Path(path).open("rb") as evidence:
            value = evidence.read(maximum + 1)
    except (OSError, ValueError) as error:
        raise TerminalClassificationError(
            f"{stream} evidence could not be read: {error}"
        ) from error
    if len(value) > maximum:
        raise TerminalClassificationError(
            f"combined terminal evidence exceeds {MAX_RUN_OUTPUT_BYTES} bytes"
        )
    return value


def _bounded_lines(value: bytes, stream: str) -> Iterator[tuple[int, bytes]]:
    for line_number, raw in enumerate(value.split(b"\n"), 1):
        if len(raw) > MAX_OUTPUT_LINE_BYTES:
            raise TerminalClassificationError(
                f"{stream} line {line_number} exceeds {MAX_OUTPUT_LINE_BYTES} bytes"
            )
        line = raw.strip()
        if line:
            yield line_number, line


def _json_object(line: bytes, stream: str, line_number: int) -> dict[str, object]:
    try:
        value: object = json.loads(line)
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        raise TerminalClassificationError(
            f"{stream} line {line_number} is not valid bounded JSON"
        ) from error
    if not isinstance(value, dict):
        raise TerminalClassificationError(
            f"{stream} line {line_number} is not a JSON object"
        )
    return cast(dict[str, object], value)


def _optional_token(usage: dict[str, object], name: str) -> int:
    value = usage.get(name, 0)
    if isinstance(value, bool) or not isinstance(value, int):
        raise TerminalClassificationError(f"result usage.{name} is not an integer")
    if not 0 <= value <= _MAX_U64:
        raise TerminalClassificationError(
            f"result usage.{name} is outside the unsigned 64-bit range"
        )
    return value


def _money(name: str, value: object) -> str:
    """Custo como texto decimal, NUNCA float carregado adiante."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TerminalClassificationError(f"{name} is not a number")
    number = float(value)
    if not math.isfinite(number):
        raise TerminalClassificationError(f"{name} is not a finite number")
    if number < 0:
        raise TerminalClassificationError(f"{name} is negative")
    return repr(number)


def _counter(frame: dict[str, object], name: str) -> int:
    value = frame.get(name, 0)
    if isinstance(value, bool) or not isinstance(value, int):
        raise TerminalClassificationError(f"result.{name} is not an integer")
    if not 0 <= value <= _MAX_U64:
        raise TerminalClassificationError(f"result.{name} is outside unsigned 64-bit")
    return value


def _optional_counter(frame: dict[str, object], name: str) -> int | None:
    if frame.get(name) is None:
        return None
    return _counter(frame, name)


def _optional_identifier(frame: dict[str, object], name: str) -> str | None:
    value = frame.get(name)
    if value is None:
        return None
    return _identifier(f"result.{name}", value)


def _model_usage(frame: dict[str, object]) -> tuple[ModelUsage, ...]:
    raw = frame.get("modelUsage", {})
    if not isinstance(raw, dict):
        raise TerminalClassificationError("result.modelUsage is not an object")
    entries = cast(dict[str, object], raw)
    if len(entries) > _MAX_MODEL_USAGE_ENTRIES:
        raise TerminalClassificationError(
            f"result.modelUsage carries more than {_MAX_MODEL_USAGE_ENTRIES} models"
        )
    parsed: list[ModelUsage] = []
    for wire_name, raw_model in entries.items():
        if not isinstance(raw_model, dict):
            raise TerminalClassificationError(
                f"result.modelUsage[{wire_name!r}] is not an object"
            )
        model = cast(dict[str, object], raw_model)
        parsed.append(
            ModelUsage(
                wire_name=_identifier("result.modelUsage key", wire_name),
                canonical_model=_optional_identifier(model, "canonicalModel"),
                provider=_optional_identifier(model, "provider"),
                input_tokens=_counter(model, "inputTokens"),
                output_tokens=_counter(model, "outputTokens"),
                cache_read_input_tokens=_counter(model, "cacheReadInputTokens"),
                cache_creation_input_tokens=_counter(model, "cacheCreationInputTokens"),
                cost_usd=_money("result.modelUsage.costUSD", model.get("costUSD", 0)),
            )
        )
    return tuple(parsed)


def _marker(text: str | None) -> str | None:
    if text is None:
        return None
    found: list[str] = []
    for line in text.splitlines():
        words = line.strip().split(maxsplit=1)
        if words:
            candidate = words[0].removesuffix(":")
            if candidate in MARKER_VALUES and candidate not in found:
                found.append(candidate)
    if len(found) > 1:
        raise TerminalClassificationError("terminal result carries conflicting markers")
    return found[0] if found else None


def _terminal_result(frame: dict[str, object]) -> _TerminalResult:
    is_error = frame.get("is_error")
    if not isinstance(is_error, bool):
        raise TerminalClassificationError("result.is_error is not a boolean")
    reason = frame.get("terminal_reason")
    if not isinstance(reason, str) or not reason:
        raise TerminalClassificationError("result.terminal_reason is not a string")
    raw_text = frame.get("result")
    if raw_text is None:
        text = None
    elif isinstance(raw_text, str):
        if len(_utf8("result.result", raw_text)) > MAX_DURABLE_OBSERVATION_BYTES:
            raise TerminalClassificationError(
                f"result text exceeds {MAX_DURABLE_OBSERVATION_BYTES} bytes"
            )
        text = raw_text
    else:
        raise TerminalClassificationError("result.result is neither text nor null")
    raw_usage = frame.get("usage", {})
    if not isinstance(raw_usage, dict):
        raise TerminalClassificationError("result.usage is not an object")
    usage = cast(dict[str, object], raw_usage)
    input_tokens = _optional_token(usage, "input_tokens")
    output_tokens = _optional_token(usage, "output_tokens")
    if input_tokens > _MAX_U64 - output_tokens:
        raise TerminalClassificationError("result token total exceeds unsigned 64-bit")
    cache_read = _optional_token(usage, "cache_read_input_tokens")
    cache_creation = _optional_token(usage, "cache_creation_input_tokens")
    if input_tokens > _MAX_U64 - cache_read - cache_creation:
        raise TerminalClassificationError("result input total exceeds unsigned 64-bit")
    raw_denials = frame.get("permission_denials", [])
    if not isinstance(raw_denials, list):
        raise TerminalClassificationError("result.permission_denials is not a list")
    raw_subagents = frame.get("subagent_stats", {})
    if not isinstance(raw_subagents, dict):
        raise TerminalClassificationError("result.subagent_stats is not an object")
    raw_status = frame.get("api_error_status")
    if raw_status is None:
        api_error_status = None
    elif isinstance(raw_status, bool) or not isinstance(raw_status, int):
        raise TerminalClassificationError(
            "result.api_error_status is neither an integer nor null"
        )
    else:
        api_error_status = raw_status

    telemetry = RunTelemetry(
        cost_usd=(
            _money("result.total_cost_usd", frame["total_cost_usd"])
            if "total_cost_usd" in frame
            else None
        ),
        input_tokens=input_tokens + cache_read + cache_creation
        if "input_tokens" in usage
        else None,
        output_tokens=output_tokens if "output_tokens" in usage else None,
        cache_read_input_tokens=cache_read,
        cache_creation_input_tokens=cache_creation,
        duration_ms=_counter(frame, "duration_ms"),
        duration_api_ms=_counter(frame, "duration_api_ms"),
        num_turns=_counter(frame, "num_turns"),
        ttft_ms=_optional_counter(frame, "ttft_ms"),
        ttft_stream_ms=_optional_counter(frame, "ttft_stream_ms"),
        time_to_request_ms=_optional_counter(frame, "time_to_request_ms"),
        stop_reason=_optional_identifier(frame, "stop_reason"),
        api_error_status=api_error_status,
        permission_denials=len(raw_denials),
        subagent_spawned=_counter(cast(dict[str, object], raw_subagents), "spawned"),
        model_usage=_model_usage(frame),
    )
    return _TerminalResult(
        is_error=is_error,
        reason=reason,
        text=text,
        marker=_marker(text),
        tokens=input_tokens + output_tokens,
        telemetry=telemetry,
        api_error_status=api_error_status,
        structured_output=(
            cast(dict[str, object], frame["structured_output"])
            if isinstance(frame.get("structured_output"), dict)
            else None
        ),
    )


def _unified_windows(info: dict[str, object]) -> tuple[WindowUsage, ...]:
    """Read the vendor window set; absence is no observation, never an error."""
    if "unifiedWindows" not in info:
        return ()
    raw_windows = info["unifiedWindows"]
    if not isinstance(raw_windows, dict):
        raise TerminalClassificationError(
            "rate_limit_info.unifiedWindows is not an object"
        )
    windows = cast(dict[str, object], raw_windows)
    if len(windows) > MAX_USAGE_WINDOWS:
        raise TerminalClassificationError(
            "rate_limit_info.unifiedWindows carries more than "
            f"{MAX_USAGE_WINDOWS} windows"
        )
    observed: list[WindowUsage] = []
    for key in sorted(windows):
        name = _identifier("rate_limit_info.unifiedWindows window name", key)
        raw_body = windows[key]
        if not isinstance(raw_body, dict):
            raise TerminalClassificationError(
                f"rate_limit_info.unifiedWindows.{name} is not an object"
            )
        body = cast(dict[str, object], raw_body)
        observed.append(
            WindowUsage(
                window=name,
                utilization=_utilization(
                    f"rate_limit_info.unifiedWindows.{name}.utilization",
                    body.get("utilization"),
                ),
                resets_at=_epoch(
                    f"rate_limit_info.unifiedWindows.{name}.resetsAt",
                    body.get("resetsAt"),
                ),
            )
        )
    return tuple(observed)


def _rate_limit(frame: dict[str, object]) -> _RateLimit:
    flattened = frozenset(
        {"status", "resetsAt", "rateLimitType", "resets_at_seconds", "limit_type"}
    )
    if flattened.intersection(frame):
        raise TerminalClassificationError(
            "rate_limit_event carries flattened fields instead of rate_limit_info"
        )
    raw_info = frame.get("rate_limit_info")
    if not isinstance(raw_info, dict):
        raise TerminalClassificationError("rate_limit_info is not an object")
    info = cast(dict[str, object], raw_info)
    status = info.get("status")
    if status not in ("allowed", "allowed_warning", "rejected"):
        raise TerminalClassificationError("rate_limit_info.status is not recognized")
    windows = _unified_windows(info)
    raw_reset = info.get("resetsAt")
    resets_at = (
        None if raw_reset is None else _epoch("rate_limit_info.resetsAt", raw_reset)
    )
    raw_limit_type = info.get("rateLimitType")
    limit_type = (
        None
        if raw_limit_type is None
        else _identifier("rate_limit_info.rateLimitType", raw_limit_type)
    )
    if status == "rejected":
        if limit_type is None:
            raise TerminalClassificationError(
                "rejected rate_limit_info carries no rateLimitType"
            )
        return _RejectedRateLimit(
            resets_at=resets_at, limit_type=limit_type, windows=windows
        )
    return _AllowedRateLimit(
        resets_at=resets_at, limit_type=limit_type, windows=windows
    )


def _stdout_evidence(
    value: bytes,
) -> tuple[
    _TerminalResult | None, _RateLimit | None, str | None, tuple[BrokenHook, ...]
]:
    terminal: _TerminalResult | None = None
    rate_limit: _RateLimit | None = None
    last_assistant_text: str | None = None
    vendor_error: str | None = None
    broken_hooks: list[BrokenHook] = []
    for line_number, line in _bounded_lines(value, "stdout"):
        frame = _json_object(line, "stdout", line_number)
        frame_type = frame.get("type")
        if not isinstance(frame_type, str):
            raise TerminalClassificationError(
                f"stdout line {line_number} carries no string type"
            )
        if frame_type == "result":
            terminal = _terminal_result(frame)
        elif frame_type == "rate_limit_event":
            rate_limit = _rate_limit(frame)
        elif frame_type == "assistant":
            text = _last_assistant_text(frame)
            if text is not None:
                last_assistant_text = text
            vendor_error = _vendor_error(frame) or vendor_error
        elif frame_type == "system" and frame.get("subtype") == "hook_response":
            broken = _broken_hook(frame)
            if broken is not None:
                broken_hooks.append(broken)
    if terminal is not None and vendor_error is not None:
        terminal = replace(terminal, vendor_error=vendor_error)
    return terminal, rate_limit, last_assistant_text, tuple(broken_hooks)


#: The shape of an error name the CLI writes: anything else is not kept.
_VENDOR_ERROR = re.compile(r"[a-z][a-z0-9_]{0,63}")


def _vendor_error(frame: dict[str, object]) -> str | None:
    """The error the CLI names on a main-thread API-error message, or None."""
    error = frame.get("error")
    if (
        frame.get("is_api_error_message") is not True
        or frame.get("parent_tool_use_id") is not None
        or not isinstance(error, str)
        or _VENDOR_ERROR.fullmatch(error) is None
    ):
        return None
    return error


def _broken_hook(frame: dict[str, object]) -> BrokenHook | None:
    """The hook_response frame as a finding, or None when the client handled it."""
    exit_code = frame.get("exit_code")
    if isinstance(exit_code, bool) or not isinstance(exit_code, int):
        raise TerminalClassificationError("hook response carries no integer exit code")
    if exit_code in _HANDLED_HOOK_EXITS:
        return None
    stderr = frame.get("stderr")
    stdout = frame.get("stdout")
    detail = stderr if isinstance(stderr, str) and stderr else stdout
    return BrokenHook(
        hook_name=_identifier("hook name", frame.get("hook_name")),
        hook_event=_identifier("hook event", frame.get("hook_event")),
        exit_code=exit_code,
        detail=_clean_str(detail if isinstance(detail, str) else None, 512),
    )


def _last_assistant_text(frame: dict[str, object]) -> str | None:
    """Return the last text content of an assistant frame, or None."""
    message = frame.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if not isinstance(content, list):
        return None
    last: str | None = None
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") != "text":
            continue
        text = block.get("text")
        if isinstance(text, str):
            last = text
    return last


LIVE_PROGRESS_TAIL_BYTES: Final = 512 * 1024


@dataclass(frozen=True, slots=True)
class LiveProgress:
    """What a run has spent so far, read off its own stream."""

    tokens: int = 0
    turns: int = 0


def observe_live_progress(stdout: Path) -> LiveProgress:
    try:
        with stdout.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - LIVE_PROGRESS_TAIL_BYTES))
            blob = handle.read()
    except OSError:
        return LiveProgress()
    lines = blob.split(b"\n")
    tokens = 0
    turns = 0
    for line in lines:
        if not line.startswith(b"{"):
            continue
        try:
            frame = json.loads(line)
        except ValueError:
            continue
        if frame.get("type") != "assistant":
            continue
        message = frame.get("message")
        usage = message.get("usage") if isinstance(message, dict) else None
        if not isinstance(usage, dict):
            continue
        turns += 1
        total = _optional_token(usage, "input_tokens") + _optional_token(
            usage, "output_tokens"
        )
        if total > 0:
            tokens = total
    return LiveProgress(tokens=tokens, turns=turns)


def _validate_stderr(value: bytes) -> None:
    for line_number, line in _bounded_lines(value, "stderr"):
        if not line.startswith(b"{"):
            try:
                line.decode("utf-8")
            except UnicodeDecodeError as decode_error:
                raise TerminalClassificationError(
                    f"stderr line {line_number} is not valid UTF-8"
                ) from decode_error
            continue
        frame = _json_object(line, "stderr", line_number)
        raw_error = frame.get("error")
        if raw_error is None:
            continue
        if not isinstance(raw_error, dict):
            raise TerminalClassificationError("stderr error member is not an object")
        error = cast(dict[str, object], raw_error)
        error_type = error.get("type")
        message = error.get("message")
        if error_type == "rate_limit_error" and not isinstance(message, str):
            raise TerminalClassificationError(
                "stderr rate_limit_error carries no string message"
            )


def journal_fallback(failure: str | None) -> RunTelemetry:
    """Keep cost unknown when the vendor supplies no terminal result."""
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
        failure=_clean_str(failure),
    )


def _stamped(
    terminal: _TerminalResult | None,
    provider_wall: ProviderWallRecord | None,
    account_wall: AccountWallRecord | None,
    failure: str,
) -> RunTelemetry:
    """The cause is stamped on EVERY failure, whether there was a `result` frame or not."""
    wall = provider_wall or account_wall
    token = wall.source.value if wall is not None else failure
    if terminal is None:
        return journal_fallback(token)
    return replace(terminal.telemetry, failure=_clean_str(token))


def failure_reason(api_error_status: int | None, cause: str | None) -> str:
    """A failed Claude run's reason: the vendor's status when it gave one, else
    the closed telemetry cause. Never the vendor's prose."""
    if api_error_status is not None:
        return f"VendorApiError (HTTP {api_error_status})"
    return f"ClaudeRunFailed ({cause})"


def _failed(
    warning: str,
    terminal: _TerminalResult | None = None,
    *,
    provider_wall: ProviderWallRecord | None = None,
    account_wall: AccountWallRecord | None = None,
    usage: tuple[WindowUsage, ...] = (),
    failure: str = "no_terminal_result",
    broken_hooks: tuple[BrokenHook, ...] = (),
) -> TerminalClassification:
    telemetry = _stamped(terminal, provider_wall, account_wall, failure)
    status = None if terminal is None else terminal.api_error_status
    return TerminalClassification(
        phase=RunPhase.FAILED,
        final_text=None if terminal is None else terminal.text,
        warning=warning,
        marker=None if terminal is None else terminal.marker,
        provider_wall=provider_wall,
        account_wall=account_wall,
        tokens=0 if terminal is None else terminal.tokens,
        telemetry=telemetry,
        usage=usage,
        api_error_status=status,
        broken_hooks=broken_hooks,
        failure_reason=failure_reason(status, telemetry.failure),
        vendor_error=None if terminal is None else terminal.vendor_error,
    )


#: Claude Code's own rendering of a vendor refusal, from its first byte:
#: `API Error: Request rejected (<status>) · [<code>][<message>][<request id>]`.
_REJECTED = re.compile(r"API Error: Request rejected \(([0-9]{3})\) · \[([0-9]+)\]\[")


def rejected_code(text: str | None, status: int | None) -> str | None:
    """The vendor's `[<code>]` in the CLI's exact rejection line, or None.

    Only that rendering counts, and only when its status is the one the result
    reported: a line the model wrote, or a format the vendor changed, carries
    no code, so nothing walls on a guess.
    """
    if text is None or status is None:
        return None
    found = _REJECTED.match(text)
    if found is None or int(found.group(1)) != status:
        return None
    return found.group(2)


def classify_terminal_run(
    stdout_path: Path | str,
    stderr_path: Path | str,
    exit_code: int,
    *,
    provider: str,
    account_alias: str | None,
    now: int,
) -> TerminalClassification:
    """Classify nested stream-json evidence; exit zero alone is never success."""
    _identifier("executed provider", provider)
    executed_account = (
        None
        if account_alias is None
        else _identifier("executed account alias", account_alias)
    )
    observed_at = _epoch("observation time", now)
    if isinstance(exit_code, bool) or not isinstance(exit_code, int):
        raise TerminalClassificationError("exit code is not an integer")

    stdout = _read_evidence(stdout_path, "stdout", MAX_RUN_OUTPUT_BYTES)
    stderr = _read_evidence(stderr_path, "stderr", MAX_RUN_OUTPUT_BYTES - len(stdout))
    _validate_stderr(stderr)
    terminal, rate_limit, last_assistant_text, broken_hooks = _stdout_evidence(stdout)
    windows: tuple[WindowUsage, ...] = () if rate_limit is None else rate_limit.windows

    if terminal is not None and rate_limit is not None:
        terminal = replace(
            terminal,
            telemetry=replace(
                terminal.telemetry,
                rate_limit_status=(
                    "allowed"
                    if isinstance(rate_limit, _AllowedRateLimit)
                    else "rejected"
                ),
                rate_limit_resets_at=rate_limit.resets_at,
                rate_limit_type=rate_limit.limit_type,
            ),
        )

    rejected_rate_limit = (
        rate_limit if isinstance(rate_limit, _RejectedRateLimit) else None
    )
    successful = (
        terminal is not None
        and not terminal.is_error
        and terminal.reason == "completed"
    )
    if successful and (exit_code != 0 or rejected_rate_limit is not None):
        raise TerminalClassificationError(
            "terminal success contradicts process or cooldown evidence"
        )
    if terminal is not None and terminal.is_error and terminal.reason == "completed":
        raise TerminalClassificationError(
            "terminal result is both completed and marked as an error"
        )

    if rejected_rate_limit is not None:
        if executed_account is None:
            raise TerminalClassificationError(
                "vendor rate limit cannot name an absent account alias"
            )
        resets_at = rejected_rate_limit.resets_at
        source = WallSource.VENDOR_RESET
        if resets_at is None:
            if observed_at > _MAX_U64 - _DEFAULT_ACCOUNT_COOLDOWN_SECONDS:
                raise TerminalClassificationError(
                    "default account cooldown overflows epoch"
                )
            resets_at = observed_at + _DEFAULT_ACCOUNT_COOLDOWN_SECONDS
            source = WallSource.DEFAULT_1H
        return _failed(
            f"vendor rate limit rejected ({rejected_rate_limit.limit_type})",
            terminal,
            account_wall=AccountWallRecord(
                account_alias=executed_account,
                cooling_until=resets_at,
                source=source,
                limit_type=rejected_rate_limit.limit_type,
                observed_at=observed_at,
            ),
            usage=windows,
            broken_hooks=broken_hooks,
        )

    if successful:
        assert terminal is not None, (
            "successful classification must carry a terminal frame"
        )
        text: str | None = terminal.text
        marker: str | None = terminal.marker
        if text is None:
            text = last_assistant_text
        if text is None:
            raise TerminalClassificationError(
                "terminal success carries no final result text"
            )
        text = text.encode("utf-8", errors="ignore")[
            :MAX_DURABLE_OBSERVATION_BYTES
        ].decode("utf-8", errors="ignore")
        return TerminalClassification(
            phase=RunPhase.COMPLETED,
            final_text=text,
            warning=None,
            marker=marker,
            provider_wall=None,
            account_wall=None,
            tokens=terminal.tokens,
            telemetry=terminal.telemetry,
            usage=windows,
            api_error_status=terminal.api_error_status,
            broken_hooks=broken_hooks,
        )

    if terminal is not None:
        if terminal.api_error_status == 402:
            return _failed(
                "provider quota exhausted (402: ProviderExhausted/QuotaLimit)",
                terminal,
                usage=windows,
            )
        if terminal.api_error_status == 429:
            return _failed(
                "provider rate limit exceeded (429: ProviderExhausted/QuotaLimit)",
                terminal,
                usage=windows,
            )
        return _failed(
            f"terminal result did not complete ({terminal.reason})",
            terminal,
            usage=windows,
            broken_hooks=broken_hooks,
            failure="terminal_incomplete",
        )
    if exit_code < 0:
        return _failed(
            f"child terminated by signal {-exit_code}",
            usage=windows,
            broken_hooks=broken_hooks,
            failure="child_signal",
        )
    return _failed(
        f"child exited with code {exit_code} without terminal result",
        usage=windows,
        broken_hooks=broken_hooks,
        failure="no_terminal_result",
    )
