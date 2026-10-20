"""A quota reading: windows the vendor published, or the reason there are none.

Every reading answers one shape: [`SourceReading`]. Either it carries windows the
vendor actually published, or it carries the exact reason it could not. A reading
that failed and returned an empty window set would be indistinguishable from an
account with nothing spent, so the constructor REFUSES both halves of that lie:
empty windows without a reason, and a reason beside windows.

Native Codex publishes camelCase account snapshots on its app-server wire; the
parsers below read them and perform no pull. No value here is built from a
token: an absence string is this module's own words.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from typing import Any, Final

from .errors import NSpawnError
from .terminal import MAX_USAGE_WINDOWS, TerminalClassificationError, WindowUsage

#: The per-call network ceiling. A source that cannot answer inside it is an
#: absence, never a hang.
PULL_TIMEOUT_S: Final = 5.0


class QuotaSourceError(NSpawnError):
    """Why a source could not answer. Never carries a credential.

    A typed refusal under this package's error root without minting an `nERR`
    code, because the operator-facing catalog is closed and this failure is
    internal to one source.
    """

    def __init__(self, message: str) -> None:
        self.message = message
        Exception.__init__(self, message)


@dataclass(frozen=True, slots=True)
class SourceReading:
    """One source's answer: windows the vendor published, or why there are none.

    The invariant is an `iff`, checked in the constructor: `windows` is empty if
    and only if `absence` names a reason. Neither an unexplained empty reading
    nor an explained non-empty one is representable.

    `stale` is a FIELD and never a spelling inside `source`: a consumer learns
    that a cached reading outlived its own freshness bound by reading a boolean,
    never by matching a substring of a free-text name. It may only ride on a
    reading that carries windows — an absence has no observation to go stale, so
    a stale reason is refused beside the other two impossible quadrants.
    """

    windows: tuple[WindowUsage, ...]
    fetched_at: int
    source: str
    absence: str | None
    stale: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.windows, tuple):
            raise QuotaSourceError("windows is not a tuple")
        for window in self.windows:
            if not isinstance(window, WindowUsage):
                raise QuotaSourceError("windows carries a value that is not a window")
        if isinstance(self.fetched_at, bool) or not isinstance(self.fetched_at, int):
            raise QuotaSourceError("fetched_at is not an integer epoch second")
        if self.fetched_at < 0:
            raise QuotaSourceError("fetched_at is negative")
        if not isinstance(self.source, str) or not self.source:
            raise QuotaSourceError("source is not a non-empty name")
        if self.absence is not None and (
            not isinstance(self.absence, str) or not self.absence
        ):
            raise QuotaSourceError("absence is neither None nor a non-empty reason")
        if not isinstance(self.stale, bool):
            raise QuotaSourceError("stale is not a boolean")
        if self.stale and self.absence is not None:
            raise QuotaSourceError("a reason cannot be stale: there is no time in it")
        if bool(self.windows) == (self.absence is None):
            return
        raise QuotaSourceError(
            "a reading carries windows without a reason, or a reason with windows"
        )


def _observed(
    windows: tuple[WindowUsage, ...],
    fetched_at: int,
    source: str,
    *,
    stale: bool = False,
) -> SourceReading:
    return SourceReading(
        windows=windows,
        fetched_at=fetched_at,
        source=source,
        absence=None,
        stale=stale,
    )


def _absent(reason: str, fetched_at: int, source: str) -> SourceReading:
    return SourceReading(
        windows=(), fetched_at=fetched_at, source=source, absence=reason
    )


def _integer(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise QuotaSourceError(f"{name} is not an integer")
    return value


def _text(name: str, value: object) -> str:
    if not isinstance(value, str) or not value:
        raise QuotaSourceError(f"{name} is not a non-empty string")
    return value


def _object_member(name: str, value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise QuotaSourceError(f"{name} is not an object")
    return value


def _window(name: str, utilization: float, resets_at: int) -> WindowUsage:
    """Build the shared window type, turning its refusal into this module's."""
    try:
        return WindowUsage(window=name, utilization=utilization, resets_at=resets_at)
    except TerminalClassificationError as error:
        raise QuotaSourceError(
            f"a window is not well formed: {error.message}"
        ) from error


#: The window lengths a reader draws, by the name Claude's wire gives them.
_SPANS: Final = {300: "five_hour", 10080: "seven_day"}


def parse_codex_rate_limits(params: object, observed_at: int) -> SourceReading:
    """Read the native camelCase snapshot. A slot name does not imply a length
    (`primary` is the week on a plan with one window), so the length the slot
    declares qualifies it: `codex.primary.seven_day`."""
    try:
        body = _object_member("native quota notification", params)
        snapshot = _object_member("native rateLimits", body.get("rateLimits"))
        limit = snapshot.get("limitId")
        prefix = "" if limit is None else _text("native limitId", limit) + "."
        windows = []
        for slot in ("primary", "secondary"):
            value = snapshot.get(slot)
            if value is None:
                continue
            window = _object_member("native " + slot, value)
            percent = _integer("native usedPercent", window.get("usedPercent"))
            if percent > 2**31 - 1:
                raise QuotaSourceError("native usedPercent exceeds int32")
            resets_at = _integer("native resetsAt", window.get("resetsAt"))
            if resets_at > 2**63 - 1:
                raise QuotaSourceError("native resetsAt exceeds int64")
            minutes = window.get("windowDurationMins")
            span = _SPANS.get(minutes) if type(minutes) is int else None
            name = prefix + slot if span is None else f"{prefix}{slot}.{span}"
            windows.append(_window(name, percent / 100.0, resets_at))
        if not windows:
            return _absent(
                "native account published no quota windows", observed_at, "codex-native"
            )
        return _observed(tuple(windows), observed_at, "codex-native")
    except QuotaSourceError as error:
        return _absent(error.message, observed_at, "codex-native")


def parse_codex_quota_response(response: object, observed_at: int) -> SourceReading:
    """Prefer the SDK's complete bucket map when present."""
    try:
        body = _object_member("native quota response", response)
        _object_member("native rateLimits", body.get("rateLimits"))
        buckets = body.get("rateLimitsByLimitId")
        if buckets is None:
            return parse_codex_rate_limits(body, observed_at)
        buckets = _object_member("native quota buckets", buckets)
        if not buckets or len(buckets) > MAX_USAGE_WINDOWS:
            raise QuotaSourceError("native quota bucket count is invalid")
        windows: list[WindowUsage] = []
        for name, value in buckets.items():
            name = _text("native quota bucket name", name)
            snapshot = _object_member("native quota bucket", value)
            if snapshot.get("limitId") not in (None, name):
                raise QuotaSourceError("native quota bucket identity mismatch")
            reading = parse_codex_rate_limits(
                {"rateLimits": {**snapshot, "limitId": name}}, observed_at
            )
            if reading.absence is not None:
                return reading
            windows.extend(reading.windows)
        if len(windows) > MAX_USAGE_WINDOWS:
            raise QuotaSourceError("native quota window count exceeds limit")
        return _observed(tuple(windows), observed_at, "codex-native")
    except QuotaSourceError as error:
        return _absent(error.message, observed_at, "codex-native")


@dataclass(frozen=True, slots=True)
class ComputeBalance:
    """The Colab account's compute units, as `colab usage` prints them."""

    balance: float
    rate_per_hour: float
    active_assignments: int
    fetched_at: int


#: One pattern for each line of `colab usage`. A missing line is a refusal.
_COLAB_LINES: Final = (
    re.compile(r"^Current balance: (\d+(?:\.\d+)?) compute units$", re.MULTILINE),
    re.compile(r"^Usage rate: (\d+(?:\.\d+)?)/hr$", re.MULTILINE),
    re.compile(r"^Active assignments: (\d+)$", re.MULTILINE),
)


def parse_colab_usage(text: str, fetched_at: int) -> ComputeBalance:
    """Read the three numbers of `colab usage`. Refuse when one is missing."""
    found = [pattern.search(text) for pattern in _COLAB_LINES]
    balance, rate, active = found
    if balance is None or rate is None or active is None:
        raise QuotaSourceError("colab usage did not print a balance, rate and count")
    # A number this module did not expect is a refusal, never inf or a guess.
    if len(balance[1]) > 12 or len(rate[1]) > 12 or len(active[1]) > 9:
        raise QuotaSourceError("colab usage printed an oversized number")
    return ComputeBalance(float(balance[1]), float(rate[1]), int(active[1]), fetched_at)


def read_colab_usage(fetched_at: int) -> ComputeBalance:
    """Run `colab usage` and read it. A failed command is a refusal."""
    try:
        done = subprocess.run(
            ["colab", "usage"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            # A closed environment: the CLI needs its home and PATH, no secret.
            env={key: os.environ[key] for key in ("HOME", "PATH") if key in os.environ},
            timeout=PULL_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise QuotaSourceError(
            f"colab usage did not run: {type(error).__name__}"
        ) from error
    if done.returncode != 0:
        raise QuotaSourceError(f"colab usage exited {done.returncode}")
    return parse_colab_usage(done.stdout, fetched_at)
