"""An exhausted account stays walled until the vendor's own reset, not one hour.

Codex reports its windows (`resetsAt`) on every native turn. Walling an
exhausted account for nSPAWN's generic hour re-picked it while the vendor still
refused it, so each hour's first dispatch failed. The reset the vendor named
is the wall; the hour stands in only when no usable future reset exists.
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from class_dispatch import DECLARED_CLASS
from supervisor_support import _drain_worker_threads, _stop_every_reaper, _sv, registry
from test_native_execute import _store_rows, _store_write, native_client

from nspawn import supervisor as supervisor_module
from nspawn.quota_sources import SourceReading
from nspawn.registry import Registry
from nspawn.rows import AccountUsageRow
from nspawn.terminal import WindowUsage

__all__ = ["_drain_worker_threads", "_stop_every_reaper", "native_client", "registry"]

NOW = 2_000_000_000
HOUR = 3600
FIVE_H = NOW + 2 * HOUR
WEEK = NOW + 5 * 24 * HOUR


def _reading(*windows: tuple[str, float, int]) -> SourceReading:
    return SourceReading(
        windows=tuple(WindowUsage(w, u, r) for w, u, r in windows),
        fetched_at=NOW,
        source="run-wire",
        absence=None,
    )


def _wall(
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    limit_type: str,
    quota: SourceReading | None,
) -> None:
    monkeypatch.setattr(supervisor_module.time, "time", lambda: float(NOW))
    sv = _sv(registry, tmp_path)
    try:
        sv._wall_rejected_credential(
            SimpleNamespace(executed_account="codex-me"),  # type: ignore[arg-type]
            limit_type=limit_type,
            quota=quota,
        )
    finally:
        sv.shutdown()


def _walled(registry: Registry, at: int) -> bool:
    return "codex-me" in registry.get_account_walls(at)


def _source(registry: Registry) -> str:
    (row,) = registry.get_account_wall_rows(NOW)
    return row.source


def test_exhausted_with_a_future_reset_is_walled_until_that_reset(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    quota = _reading(("codex.primary", 1.0, FIVE_H), ("codex.secondary", 0.4, WEEK))
    _wall(registry, tmp_path, monkeypatch, "quota_exhausted", quota)
    assert _walled(registry, NOW + HOUR + 1)
    assert _walled(registry, FIVE_H - 1)
    assert not _walled(registry, FIVE_H)
    assert _source(registry) == "vendor_reset"


def test_a_tie_at_100_percent_waits_for_the_latest_reset(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Weekly listed first: picking by utilization alone would take either.
    quota = _reading(("codex.secondary", 1.0, WEEK), ("codex.primary", 1.0, FIVE_H))
    _wall(registry, tmp_path, monkeypatch, "quota_exhausted", quota)
    assert _walled(registry, FIVE_H + 1)
    assert _walled(registry, WEEK - 1)
    assert not _walled(registry, WEEK)


@pytest.mark.parametrize(
    "quota",
    [
        None,
        _reading(("codex.primary", 1.0, NOW - 10)),
        _reading(("codex.primary", 1.0, NOW)),
        # A reset on a window that is not exhausted says nothing of the wall.
        _reading(("codex.primary", 0.5, WEEK)),
    ],
    ids=["no-reading", "past-reset", "reset-now", "not-exhausted"],
)
def test_no_usable_future_reset_keeps_the_default_hour(
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    quota: SourceReading | None,
) -> None:
    _wall(registry, tmp_path, monkeypatch, "quota_exhausted", quota)
    assert _walled(registry, NOW + HOUR - 1)
    assert not _walled(registry, NOW + HOUR)
    assert _source(registry) == "default_1h"


def test_a_rejected_credential_keeps_its_five_minutes(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    quota = _reading(("codex.primary", 1.0, WEEK))
    _wall(registry, tmp_path, monkeypatch, "credential_rejected", quota)
    assert _walled(registry, NOW + 299)
    assert not _walled(registry, NOW + 300)


@pytest.mark.parametrize("native_client", ["codex"], indirect=True)
def test_the_native_route_walls_until_the_vendor_reset(native_client: Any) -> None:
    """The real Codex dispatch hands the turn's windows to the wall."""
    client, cwd = native_client
    reset = int(time.time()) + 5 * HOUR
    (cwd / "exhausted-window").write_text(str(reset))
    run = client.call(
        "nspawn_run_start",
        {
            "provider": "first",
            "cwd": str(cwd),
            "read_only": True,
            "prompt": 'fixture error "usageLimitExceeded"',
            "class": DECLARED_CLASS,
        },
    )
    assert client.terminal(run["run_id"])["phase"] == "failed"
    assert _store_rows(
        "SELECT cooling_until, source FROM account_walls WHERE account_alias = ?",
        ("first",),
    ) == [(reset, "vendor_reset")]


STALE = NOW - 3 * 24 * HOUR


def _store_usage(registry: Registry, utilization: float, resets_at: int) -> None:
    """A window a run observed days ago and nothing has re-read since."""
    registry.record_account_usage(
        (AccountUsageRow("codex-me", "codex.secondary", utilization, resets_at, STALE),)
    )


def test_a_stale_exhausted_window_walls_the_picker_until_its_reset(
    registry: Registry,
) -> None:
    """Measured 2026-10-03: codex-me at 1.0 until 10-25, observed long ago, and
    the picker kept choosing it once its one-hour wall lifted."""
    _store_usage(registry, 1.0, WEEK)
    assert registry.get_account_walls(NOW) == {"codex-me": WEEK}
    assert _walled(registry, WEEK - 1)
    assert not _walled(registry, WEEK)


@pytest.mark.parametrize(
    ("utilization", "resets_at"),
    [(1.0, NOW - 10), (1.0, NOW), (0.5, WEEK)],
    ids=["reset-crossed", "reset-now", "not-exhausted"],
)
def test_a_rolled_over_or_unspent_window_does_not_wall(
    registry: Registry, utilization: float, resets_at: int
) -> None:
    _store_usage(registry, utilization, resets_at)
    assert registry.get_account_walls(NOW) == {}


def test_a_quota_less_usage_limit_reuses_the_stored_reset(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """usageLimitExceeded carried no windows; the last known exhausted one did."""
    _store_usage(registry, 1.0, WEEK)
    _wall(registry, tmp_path, monkeypatch, "quota_exhausted", None)
    (row,) = registry.get_account_wall_rows(NOW)
    assert (row.cooling_until, row.source) == (WEEK, "vendor_reset")


def test_a_quota_less_usage_limit_past_the_stored_reset_keeps_the_hour(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _store_usage(registry, 1.0, NOW - 10)
    _wall(registry, tmp_path, monkeypatch, "quota_exhausted", None)
    (row,) = registry.get_account_wall_rows(NOW)
    assert (row.cooling_until, row.source) == (NOW + HOUR, "default_1h")


@pytest.mark.parametrize("native_client", ["codex"], indirect=True)
@pytest.mark.parametrize(
    ("offset", "expected"),
    [(5 * 24 * HOUR, "second"), (-10, "first")],
    ids=["future-reset", "crossed-reset"],
)
def test_the_picker_skips_an_account_whose_stale_window_is_exhausted(
    native_client: Any, offset: int, expected: str
) -> None:
    """The real dispatch: no wall row, only a stale exhausted observation."""
    client, cwd = native_client
    now = int(time.time())
    _store_write(
        "INSERT INTO account_usage"
        "(account_alias, usage_window, utilization, resets_at, observed_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("first", "codex.secondary", 1.0, now + offset, now - 3 * 24 * HOUR),
    )
    run = client.call(
        "nspawn_run_start",
        {
            "provider": "codex",
            "cwd": str(cwd),
            "read_only": True,
            "prompt": "Reply PONG",
            "class": DECLARED_CLASS,
        },
    )
    status = client.terminal(run["run_id"])
    assert status["phase"] == "completed", status
    assert status["executed_account"] == expected


@pytest.mark.parametrize("native_client", ["codex"], indirect=True)
@pytest.mark.parametrize(
    ("offset", "expected"),
    [(5 * 24 * HOUR, "vendor_reset"), (-10, "default_1h")],
    ids=["future-reset", "crossed-reset"],
)
def test_quota_show_reports_the_wall_the_picker_enforces(
    native_client: Any, offset: int, expected: str
) -> None:
    """The 2026-10-03 shape: a one-hour wall row beside a stale exhausted window.
    The report read the hour while the picker walled until the reset."""
    client, _ = native_client
    now = int(time.time())
    _store_write(
        "INSERT INTO account_usage"
        "(account_alias, usage_window, utilization, resets_at, observed_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("first", "codex.secondary", 1.0, now + offset, now - 3 * 24 * HOUR),
    )
    _store_write(
        "INSERT INTO account_walls"
        "(account_alias, cooling_until, source, limit_type, observed_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("first", now + HOUR, "default_1h", "quota_exhausted", now),
    )
    report = client.call("nspawn_quota_show", {"provider": "codex", "refresh": 0})
    (account,) = (
        a for a in report["providers"][0]["accounts"] if a["account_alias"] == "first"
    )
    until = now + offset if expected == "vendor_reset" else now + HOUR
    assert (account["cooling_until"], account["wall_source"]) == (until, expected)


@pytest.mark.parametrize("native_client", ["codex"], indirect=True)
def test_a_derived_wall_never_blocks_the_live_read_that_corrects_it(
    native_client: Any,
) -> None:
    """A stale 1.0 window must not seal its account: `refresh` still asks the
    vendor, and the vendor's answer (25%) lifts the wall the picker reads."""
    client, _ = native_client
    now = int(time.time())
    _store_write(
        "INSERT INTO account_usage"
        "(account_alias, usage_window, utilization, resets_at, observed_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("first", "codex.primary", 1.0, now + 20 * 24 * HOUR, now - 3 * 24 * HOUR),
    )

    def first(refresh: int) -> dict[str, Any]:
        report = client.call(
            "nspawn_quota_show", {"provider": "codex", "refresh": refresh}
        )
        (account,) = (
            a
            for a in report["providers"][0]["accounts"]
            if a["account_alias"] == "first"
        )
        return account  # type: ignore[no-any-return]

    live = first(1)
    assert (live["state"], live["source"]) == ("observed", "codex-native"), live
    assert live["cooling_until"] is None, live
    stored = first(0)
    assert stored["cooling_until"] is None, stored
    assert [w["utilization"] for w in stored["windows"]] == [0.25]


def test_a_shorter_wall_never_shortens_a_longer_one(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A credential wall of 300 s, then an in-flight quota-less failure whose
    stored reset is 60 s away: the account stays walled for the 300 s, under
    the credential's cause, and no later write shortens it."""
    _wall(registry, tmp_path, monkeypatch, "credential_rejected", None)
    _store_usage(registry, 1.0, NOW + 60)
    _wall(registry, tmp_path, monkeypatch, "quota_exhausted", None)
    (row,) = registry.get_account_wall_rows(NOW)
    assert (row.cooling_until, row.source, row.limit_type) == (
        NOW + 300,
        "default_1h",
        "credential_rejected",
    )
    registry.record_account_wall("codex-me", NOW + 100, "default_1h", "x", NOW)
    registry.record_account_wall("codex-me", NOW + 10, "vendor_reset", "x", NOW)
    assert registry.get_account_walls(NOW) == {"codex-me": NOW + 300}
    registry.record_account_wall("codex-me", NOW + 900, "vendor_reset", "x", NOW)
    (row,) = registry.get_account_wall_rows(NOW)
    # The live credential cause takes precedence: the longer row is not liftable.
    assert (row.cooling_until, row.source) == (NOW + 900, "default_1h")


def test_a_shorter_provider_wall_never_shortens_a_longer_one(
    registry: Registry,
) -> None:
    registry.record_provider_wall("codex", NOW + 3600, "default_1h", NOW)
    registry.record_provider_wall("codex", NOW + 60, "vendor_reset", NOW)
    assert registry.get_provider_walls(NOW) == {"codex": NOW + 3600}
    registry.record_provider_wall("codex", NOW + 7200, "vendor_reset", NOW)
    assert registry.get_provider_walls(NOW) == {"codex": NOW + 7200}


@pytest.mark.parametrize("native_client", ["codex"], indirect=True)
def test_a_refresh_reads_usage_past_a_vendor_reset_wall_and_the_row_holds(
    native_client: Any,
) -> None:
    """An explicit refresh is a metadata read: a `vendor_reset` wall lets it
    through, so the usage the derived walls read follows the vendor. The stored
    wall row itself ends only at its deadline. A credential wall still blocks
    the read."""
    client, cwd = native_client
    now = int(time.time())
    far = now + 20 * 24 * HOUR
    _store_write(
        "INSERT INTO account_usage"
        "(account_alias, usage_window, utilization, resets_at, observed_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("first", "codex.primary", 1.0, far, now - 3 * 24 * HOUR),
    )
    for alias, until, source, limit_type in (
        ("first", far, "vendor_reset", "quota_exhausted"),
        ("second", now + 300, "default_1h", "credential_rejected"),
    ):
        _store_write(
            "INSERT INTO account_walls"
            "(account_alias, cooling_until, source, limit_type, observed_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (alias, until, source, limit_type, now),
        )
    report = client.call("nspawn_quota_show", {"provider": "codex", "refresh": 1})
    accounts = {a["account_alias"]: a for a in report["providers"][0]["accounts"]}
    first, second = accounts["first"], accounts["second"]
    assert first["source"] == "codex-native", first
    assert (first["cooling_until"], first["wall_source"]) == (far, "vendor_reset")
    assert _store_rows(
        "SELECT utilization FROM account_usage WHERE account_alias = 'first'"
    ) == [(0.25,)]
    assert (second["cooling_until"], second["wall_source"]) == (
        now + 300,
        "default_1h",
    ), second
    assert not (cwd / "second/quota-read-called").exists()
    assert _store_rows(
        "SELECT account_alias FROM account_walls ORDER BY account_alias"
    ) == [("first",), ("second",)]


@pytest.mark.parametrize("native_client", ["codex"], indirect=True)
def test_a_reading_keeps_the_exhausted_window_it_did_not_observe(
    native_client: Any,
) -> None:
    """The native read publishes only `primary` (25%). The exhausted `secondary`
    it never observed is not erased, so the account stays walled."""
    client, _ = native_client
    now = int(time.time())
    far = now + 20 * 24 * HOUR
    _store_write(
        "INSERT INTO account_usage"
        "(account_alias, usage_window, utilization, resets_at, observed_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("first", "codex.secondary", 1.0, far, now - 3 * 24 * HOUR),
    )
    report = client.call("nspawn_quota_show", {"provider": "codex", "refresh": 1})
    (first,) = (
        a for a in report["providers"][0]["accounts"] if a["account_alias"] == "first"
    )
    assert (first["cooling_until"], first["wall_source"]) == (far, "vendor_reset")
    assert sorted(
        _store_rows(
            "SELECT usage_window, utilization FROM account_usage "
            "WHERE account_alias = ?",
            ("first",),
        )
    ) == [("codex.primary.seven_day", 0.25), ("codex.secondary", 1.0)]


@pytest.mark.parametrize("native_client", ["codex"], indirect=True)
def test_a_reading_keeps_another_limits_exhausted_bucket(native_client: Any) -> None:
    """`review.primary` and `codex.primary` are two buckets. A refresh that
    observed only `codex.primary` (25%) keeps the exhausted `review.primary`."""
    client, _ = native_client
    now = int(time.time())
    far = now + 20 * 24 * HOUR
    _store_write(
        "INSERT INTO account_usage"
        "(account_alias, usage_window, utilization, resets_at, observed_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("first", "review.primary", 1.0, far, now - 3 * 24 * HOUR),
    )
    report = client.call("nspawn_quota_show", {"provider": "codex", "refresh": 1})
    (first,) = (
        a for a in report["providers"][0]["accounts"] if a["account_alias"] == "first"
    )
    assert (first["cooling_until"], first["wall_source"]) == (far, "vendor_reset")
    assert sorted(
        _store_rows(
            "SELECT usage_window, utilization FROM account_usage "
            "WHERE account_alias = ?",
            ("first",),
        )
    ) == [("codex.primary.seven_day", 0.25), ("review.primary", 1.0)]


@pytest.mark.parametrize(
    "order",
    [
        ("credential_rejected", "quota_exhausted"),
        ("quota_exhausted", "credential_rejected"),
    ],
    ids=["credential-first", "quota-first"],
)
def test_a_credential_cause_wins_the_merge_in_either_order(
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    order: tuple[str, str],
) -> None:
    for limit_type in order:
        _wall(registry, tmp_path, monkeypatch, limit_type, None)
    (row,) = registry.get_account_wall_rows(NOW)
    assert (row.cooling_until, row.source, row.limit_type) == (
        NOW + HOUR,
        "default_1h",
        "credential_rejected",
    )


def test_a_96_percent_window_does_not_set_the_failure_wall(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """usageLimitExceeded with primary at 100% (resets in 2 h) and secondary at
    96% (20 days): the exhausted window is the primary, so the wall is 2 h."""
    two_h, twenty_d = NOW + 2 * HOUR, NOW + 20 * 24 * HOUR
    quota = _reading(("codex.primary", 1.0, two_h), ("codex.secondary", 0.96, twenty_d))
    _wall(registry, tmp_path, monkeypatch, "quota_exhausted", quota)
    (row,) = registry.get_account_wall_rows(NOW)
    assert (row.cooling_until, row.source) == (two_h, "vendor_reset")


def test_a_turn_completing_late_never_overwrites_a_newer_exhaustion(
    registry: Registry,
) -> None:
    """t0 < t1 < t2 < R: a turn observes 25% at t0, a refresh stores 100% (reset
    R) at t1, and the turn completes at t2 writing its t0 reading. The newer
    exhaustion stands, so the picker still walls the account until R."""
    t0, t1, reset = NOW - 60, NOW - 30, NOW + 5 * HOUR
    registry.record_account_usage(
        (AccountUsageRow("codex-me", "codex.primary", 1.0, reset, t1),)
    )
    registry.record_account_usage(
        (AccountUsageRow("codex-me", "codex.primary", 0.25, reset, t0),)
    )
    assert registry.get_account_walls(NOW) == {"codex-me": reset}
    (row,) = registry.get_account_usage()
    assert (row.utilization, row.observed_at) == (1.0, t1)


@pytest.mark.parametrize("native_client", ["codex_frozen_clock"], indirect=True)
def test_a_same_second_completion_keeps_the_higher_reading(native_client: Any) -> None:
    """A turn observes 99% at T, a refresh stores 100% at T while the turn runs,
    and the turn completes: both readings carry T. The one that errs toward
    walling, 100%, stands, through the real native completion writer."""
    client, cwd = native_client
    pinned = int((cwd / "frozen-clock").read_text())
    reset = pinned + 5 * HOUR
    (cwd / "exhausted-window").write_text(str(reset))
    (cwd / "window-percent").write_text("99")
    (cwd / "pause-before-complete").touch()
    run = client.call(
        "nspawn_run_start",
        {
            "provider": "first",
            "cwd": str(cwd),
            "read_only": True,
            "prompt": "Reply PONG",
            "class": DECLARED_CLASS,
        },
    )
    deadline = time.monotonic() + 15
    while not (cwd / "turn-paused").exists():
        assert time.monotonic() < deadline, "the turn never reached completion"
        time.sleep(0.02)
    _store_write(
        "INSERT INTO account_usage"
        "(account_alias, usage_window, utilization, resets_at, observed_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("first", "codex.primary", 1.0, reset, pinned),
    )
    (cwd / "resume-turn").touch()
    assert client.terminal(run["run_id"])["phase"] == "completed"
    assert _store_rows(
        "SELECT utilization, observed_at FROM account_usage WHERE account_alias = ?",
        ("first",),
    ) == [(1.0, pinned)]


def test_a_strictly_newer_lower_reading_still_wins(registry: Registry) -> None:
    registry.record_account_usage(
        (AccountUsageRow("codex-me", "codex.primary", 1.0, NOW + HOUR, NOW),)
    )
    registry.record_account_usage(
        (AccountUsageRow("codex-me", "codex.primary", 0.25, NOW + HOUR, NOW + 1),)
    )
    (row,) = registry.get_account_usage()
    assert (row.utilization, row.observed_at) == (0.25, NOW + 1)
