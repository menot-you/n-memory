"""``nspawn.quota_sources`` — a number the vendor published, or the reason."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from nspawn import quota_sources
from nspawn.quota_sources import QuotaSourceError, SourceReading
from nspawn.terminal import WindowUsage

NOW = 1_800_000_000


def _window(name: str = "five_hour") -> WindowUsage:
    return WindowUsage(window=name, utilization=0.25, resets_at=NOW + 60)


# --- SourceReading: the iff invariant --------------------------------------


def test_reading_carries_windows_without_a_reason() -> None:
    reading = SourceReading((_window(),), NOW, "s", None)
    assert reading.absence is None
    assert reading.windows[0].utilization == 0.25


def test_reading_carries_a_reason_without_windows() -> None:
    assert SourceReading((), NOW, "s", "why").windows == ()


def test_empty_windows_without_a_reason_is_refused() -> None:
    with pytest.raises(QuotaSourceError, match="without a reason"):
        SourceReading((), NOW, "s", None)


def test_windows_beside_a_reason_are_refused() -> None:
    with pytest.raises(QuotaSourceError, match="a reason with windows"):
        SourceReading((_window(),), NOW, "s", "why")


@given(
    windows=st.integers(min_value=0, max_value=3),
    absence=st.one_of(st.none(), st.text(min_size=1, max_size=8)),
)
def test_the_iff_invariant_holds_for_every_quadrant(
    windows: int, absence: str | None
) -> None:
    """Empty windows IFF a named absence. The other two quadrants are refused."""
    payload = tuple(_window(f"w{index}") for index in range(windows))
    legal = (payload == ()) == (absence is not None)
    if legal:
        reading = SourceReading(payload, NOW, "s", absence)
        assert (reading.windows == ()) == (reading.absence is not None)
        return
    with pytest.raises(QuotaSourceError):
        SourceReading(payload, NOW, "s", absence)


def test_windows_must_be_a_tuple() -> None:
    with pytest.raises(QuotaSourceError, match="not a tuple"):
        SourceReading([_window()], NOW, "s", None)  # type: ignore[arg-type]


def test_windows_must_carry_windows() -> None:
    with pytest.raises(QuotaSourceError, match="not a window"):
        SourceReading(("five_hour",), NOW, "s", None)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [True, "1800000000", 1.5])
def test_fetched_at_must_be_an_integer_epoch(value: object) -> None:
    with pytest.raises(QuotaSourceError, match="integer epoch"):
        SourceReading((), value, "s", "why")  # type: ignore[arg-type]


def test_fetched_at_must_not_be_negative() -> None:
    with pytest.raises(QuotaSourceError, match="negative"):
        SourceReading((), -1, "s", "why")


@pytest.mark.parametrize("value", ["", 7])
def test_source_must_be_a_non_empty_name(value: object) -> None:
    with pytest.raises(QuotaSourceError, match="non-empty name"):
        SourceReading((), NOW, value, "why")  # type: ignore[arg-type]


@pytest.mark.parametrize("value", ["", 7])
def test_absence_must_be_none_or_a_reason(value: object) -> None:
    with pytest.raises(QuotaSourceError, match="neither None nor"):
        SourceReading((), NOW, "s", value)  # type: ignore[arg-type]


def test_a_reading_is_fresh_unless_it_says_otherwise() -> None:
    assert SourceReading((), NOW, "s", "why").stale is False
    assert SourceReading((_window(),), NOW, "s", None, True).stale is True


def test_a_reason_cannot_be_stale() -> None:
    """There is no observation in an absence, so there is no time in it either."""
    with pytest.raises(QuotaSourceError, match="no time in it"):
        SourceReading((), NOW, "s", "why", True)


@pytest.mark.parametrize("value", ["yes", 1, None])
def test_stale_must_be_a_boolean(value: object) -> None:
    with pytest.raises(QuotaSourceError, match="stale is not a boolean"):
        SourceReading((), NOW, "s", "why", value)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "percent", [-1, 2**31, 25.5, True, "25", None, float("nan"), float("inf")]
)
def test_native_codex_quota_invalid_numbers_are_absence(percent):
    reading = quota_sources.parse_codex_rate_limits(
        {
            "rateLimits": {
                "limitId": "codex",
                "primary": {"usedPercent": percent, "resetsAt": 4102444800},
            }
        },
        100,
    )
    assert reading.windows == ()
    assert reading.absence is not None


def test_native_codex_quota_preserves_slot_and_null_secondary():
    reading = quota_sources.parse_codex_rate_limits(
        {
            "rateLimits": {
                "limitId": "codex",
                "primary": {
                    "usedPercent": 25,
                    "windowDurationMins": 10080,
                    "resetsAt": 4102444800,
                },
                "secondary": None,
            }
        },
        100,
    )
    assert reading.absence is None
    assert [(w.window, w.utilization, w.resets_at) for w in reading.windows] == [
        ("codex.primary.seven_day", 0.25, 4102444800)
    ]
    assert (
        quota_sources.parse_codex_rate_limits(
            {"rateLimits": {"primary": None, "secondary": None}}, 100
        ).absence
        is not None
    )


@pytest.mark.parametrize(
    ("minutes", "name"),
    [
        (300, "codex.secondary.five_hour"),
        (10080, "codex.secondary.seven_day"),
        (1440, "codex.secondary"),
        ("10080", "codex.secondary"),
        ([10080], "codex.secondary"),
        (None, "codex.secondary"),
    ],
)
def test_native_codex_quota_names_the_span_its_window_declares(
    minutes: object, name: str
) -> None:
    """A slot says nothing of its length: on a plan with one window `primary` is
    the week (measured 2026-10-06, codex-main). The length the vendor declares
    qualifies the slot, so a reader can tell a five-hour window from a week."""
    slot: dict[str, object] = {"usedPercent": 80, "resetsAt": 4102444800}
    if minutes is not None:
        slot["windowDurationMins"] = minutes
    reading = quota_sources.parse_codex_rate_limits(
        {"rateLimits": {"limitId": "codex", "secondary": slot}}, 100
    )
    assert [w.window for w in reading.windows] == [name]


def test_native_codex_quota_preserves_integer_overage():
    reading = quota_sources.parse_codex_rate_limits(
        {
            "rateLimits": {
                "limitId": "codex",
                "primary": {"usedPercent": 101, "resetsAt": 4102444800},
            }
        },
        100,
    )
    assert reading.absence is None
    assert reading.windows[0].utilization == 1.01


@pytest.mark.parametrize("reset", [-1, True, 1.5, 2**63])
def test_native_codex_quota_invalid_epoch_is_absence(reset):
    reading = quota_sources.parse_codex_rate_limits(
        {"rateLimits": {"primary": {"usedPercent": 25, "resetsAt": reset}}}, 100
    )
    assert reading.windows == ()
    assert reading.absence is not None


def test_native_cold_quota_full_bucket_map() -> None:
    snapshot = {"primary": {"usedPercent": 101, "resetsAt": 123}, "secondary": None}
    response = {
        "rateLimits": snapshot,
        "rateLimitsByLimitId": {"codex": snapshot, "review": snapshot},
    }
    reading = quota_sources.parse_codex_quota_response(response, 10)
    assert [(w.window, w.utilization) for w in reading.windows] == [
        ("codex.primary", 1.01),
        ("review.primary", 1.01),
    ]
    response["rateLimitsByLimitId"]["review"] = {**snapshot, "limitId": "codex"}
    refused = quota_sources.parse_codex_quota_response(response, 10)
    assert refused.absence == "native quota bucket identity mismatch"
    assert not refused.windows
