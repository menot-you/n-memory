"""Supervisor quota contracts."""

from __future__ import annotations

import dataclasses
import threading
import time
from pathlib import Path

import pytest
from hypothesis import given, seed, settings
from hypothesis import strategies as st
from supervisor_support import (
    _drain_worker_threads,
    _stop_every_reaper,
    _sv,
    registry,
)

from nspawn import supervisor, terminal
from nspawn.registry import Registry
from nspawn.rows import AccountUsageRow
from nspawn.supervisor import (
    InvalidRunRequest,
    Supervisor,
)

# Export imported fixtures so pytest discovers them in this module.
__all__ = ["_drain_worker_threads", "_stop_every_reaper", "registry"]


# ---------------------------------------------------------------------------
# quota()
#
# Every test here injects a double shaped exactly like the contracted
# `SourceReading(windows, fetched_at, source, absence)`.
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class _FakeReading:
    """The contracted `SourceReading` shape, and nothing more."""

    windows: tuple[terminal.WindowUsage, ...] = ()
    fetched_at: int = 0
    source: str = "fake-vendor-api"
    absence: str | None = None


class _RecordingSource:
    """A `QuotaSource` that remembers every (provider, alias) it was asked."""

    def __init__(self, reading: _FakeReading) -> None:
        self.reading = reading
        self.calls: list[tuple[str, str | None]] = []

    def __call__(self, provider: str, alias: str | None) -> _FakeReading:
        self.calls.append((provider, alias))
        return self.reading


@pytest.fixture
def quota_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A roster shaped like the live one: one provider with real accounts in a
    secrets directory, one that routes by env and discovers none.

    L3 (owner ruling 2026-08-29) deletes the silent HOME fallback; the
    fixture sets `NOTT_NSPAWN_ROSTER` so the test surface is identical
    to what the live door reads. HOME is set for compatibility with
    legacy call paths that the roster's secrets resolution may consult
    transitively, but `_read_roster` finds the file via NOTT_NSPAWN_ROSTER.
    """
    home = tmp_path / "quota-home"
    secrets = home / "secrets" / "claude"
    secrets.mkdir(parents=True)
    (secrets / "acct-1.token").write_text("x", encoding="utf-8")
    (secrets / "acct-2.token").write_text("x", encoding="utf-8")
    (home / ".nott").mkdir(parents=True)
    (home / ".nott" / "spawn.toml").write_text(
        "[defaults]\n"
        'plugins = ["/tmp/plugin-root"]\n'
        "\n"
        "[[provider]]\n"
        'name = "claude"\n'
        'kind = "claude"\n'
        f'secrets = "{secrets}"\n'
        'model = "opus"\n'
        'models = ["opus", "sonnet"]\n'
        "\n"
        "[[provider]]\n"
        'name = "codex1"\n'
        'kind = "claude"\n'
        'model = "gpt"\n'
        'models = ["gpt"]\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("NOTT_NSPAWN_ROSTER", raising=False)
    monkeypatch.setenv("NOTT_NSPAWN_ROSTER", str(home / ".nott/spawn.toml"))
    # Under file-wins, isolating only by env lets the fixture be overridden
    # by the operator's ~/.nott/spawn.toml: fixing CONFIG is mandatory.
    monkeypatch.setenv("NOTT_NSPAWN_CONFIG", str(tmp_path / "absent-spawn.toml"))
    return home


def _accounts(report: supervisor.QuotaReport) -> dict[str, supervisor.AccountQuota]:
    return {
        account.account_alias: account
        for provider in report.providers
        for account in provider.accounts
    }


def _providers(report: supervisor.QuotaReport) -> dict[str, supervisor.ProviderQuota]:
    return {provider.provider: provider for provider in report.providers}


def _store_usage(
    registry: Registry,
    alias: str,
    window: str,
    utilization: float,
    resets_at: int,
    observed_at: int,
) -> None:
    registry.record_account_usage(
        (
            AccountUsageRow(
                account_alias=alias,
                usage_window=window,
                utilization=utilization,
                resets_at=resets_at,
                observed_at=observed_at,
            ),
        )
    )


# -- the decision ladder, as a pure function --------------------------------


@given(
    resets_at=st.integers(min_value=0, max_value=4_000_000_000),
    observed_at=st.integers(min_value=0, max_value=4_000_000_000),
    now=st.integers(min_value=0, max_value=4_000_000_000),
)
@seed(31337)
@settings(max_examples=400)
def test_window_state_is_exactly_one_of_three_by_a_stated_rule(
    resets_at: int, observed_at: int, now: int
) -> None:
    """The ladder, stated independently of the implementation: a window that has
    reset is EXPIRED whatever its age; otherwise age past the freshness bound is
    STALE; otherwise OBSERVED. No fourth outcome, and never a state that belongs
    to an ABSENCE."""
    quota = supervisor._window_quota(
        terminal.WindowUsage(window="5h", utilization=0.5, resets_at=resets_at),
        observed_at,
        now,
    )

    if resets_at <= now:
        expected = supervisor.QuotaState.EXPIRED
    elif now - observed_at > supervisor.QUOTA_FRESHNESS_S:
        expected = supervisor.QuotaState.STALE
    else:
        expected = supervisor.QuotaState.OBSERVED
    assert quota.state is expected
    assert quota.state not in (
        supervisor.QuotaState.NEVER_OBSERVED,
        supervisor.QuotaState.NO_SOURCE,
    )


@given(
    utilization=st.floats(min_value=0.0, max_value=1.0),
    resets_at=st.integers(min_value=0, max_value=4_000_000_000),
    observed_at=st.integers(min_value=0, max_value=4_000_000_000),
    now=st.integers(min_value=0, max_value=4_000_000_000),
)
@seed(4711)
@settings(max_examples=400)
def test_window_quota_never_rewrites_the_number_it_carries(
    utilization: float, resets_at: int, observed_at: int, now: int
) -> None:
    """Whatever the state, the vendor's own figure survives it — an expired or
    stale reading is LABELLED, never zeroed."""
    quota = supervisor._window_quota(
        terminal.WindowUsage(window="7d", utilization=utilization, resets_at=resets_at),
        observed_at,
        now,
    )

    assert quota.utilization == utilization
    assert quota.resets_at == resets_at
    assert quota.observed_at == observed_at
    assert quota.window == "7d"


@given(
    states=st.lists(
        st.sampled_from(list(supervisor.QuotaState)), min_size=1, max_size=6
    )
)
@seed(1024)
@settings(max_examples=200)
def test_worst_state_is_the_one_furthest_from_a_current_number(
    states: list[supervisor.QuotaState],
) -> None:
    worst = supervisor._worst(states)

    assert worst in states
    assert all(
        supervisor._STATE_DISTANCE[worst] >= supervisor._STATE_DISTANCE[state]
        for state in states
    )


# -- the report --------------------------------------------------------------


def test_quota_refuses_an_undeclared_provider_by_name(
    registry: Registry, tmp_path: Path, quota_home: Path
) -> None:
    """An empty report would read as `this provider has no quota`."""
    sv = _sv(registry, tmp_path)
    with pytest.raises(InvalidRunRequest, match="'nope' is not declared"):
        sv.quota(provider="nope")


def test_quota_refuses_a_negative_refresh(
    registry: Registry, tmp_path: Path, quota_home: Path
) -> None:
    sv = _sv(registry, tmp_path)
    with pytest.raises(InvalidRunRequest, match="refresh"):
        sv.quota(refresh=-1)


def test_quota_covers_every_declared_provider_and_filters_to_one(
    registry: Registry, tmp_path: Path, quota_home: Path
) -> None:
    sv = _sv(registry, tmp_path)

    everything = sv.quota()
    one = sv.quota(provider="codex1")

    assert set(_providers(everything)) == {"claude", "codex1"}
    assert _providers(everything)["claude"].models == ("opus", "sonnet")
    assert _providers(everything)["claude"].default_model == "opus"
    assert set(_providers(one)) == {"codex1"}


def test_quota_calls_an_account_never_observed_exactly_that(
    registry: Registry, tmp_path: Path, quota_home: Path
) -> None:
    """The state that must never be 0.0: a real account with no row at all."""
    sv = _sv(registry, tmp_path)

    accounts = _accounts(sv.quota())

    assert set(accounts) == {"acct-1", "acct-2"}
    for account in accounts.values():
        assert account.state is supervisor.QuotaState.NEVER_OBSERVED
        assert account.windows == ()
        assert account.absence is not None
        assert account.account_alias in account.absence


def test_quota_serves_a_fresh_stored_reading_as_observed(
    registry: Registry, tmp_path: Path, quota_home: Path
) -> None:
    sv = _sv(registry, tmp_path)
    now = int(time.time())
    _store_usage(registry, "acct-1", "5h", 0.42, now + 3600, now)

    account = _accounts(sv.quota())["acct-1"]

    assert account.state is supervisor.QuotaState.OBSERVED
    assert account.source == supervisor.RUN_WIRE_SOURCE
    assert [(w.window, w.utilization, w.state) for w in account.windows] == [
        ("5h", 0.42, supervisor.QuotaState.OBSERVED)
    ]
    assert account.absence is None


def test_quota_keeps_the_number_of_a_reading_past_its_freshness(
    registry: Registry, tmp_path: Path, quota_home: Path
) -> None:
    sv = _sv(registry, tmp_path)
    now = int(time.time())
    _store_usage(
        registry,
        "acct-1",
        "5h",
        0.61,
        now + 3600,
        now - supervisor.QUOTA_FRESHNESS_S - 60,
    )

    account = _accounts(sv.quota())["acct-1"]

    assert account.state is supervisor.QuotaState.STALE
    assert account.windows[0].utilization == 0.61  # kept, and labelled


def test_quota_keeps_the_number_of_a_window_that_already_reset(
    registry: Registry, tmp_path: Path, quota_home: Path
) -> None:
    sv = _sv(registry, tmp_path)
    now = int(time.time())
    _store_usage(registry, "acct-1", "5h", 0.99, now - 10, now)

    account = _accounts(sv.quota())["acct-1"]

    assert account.state is supervisor.QuotaState.EXPIRED
    assert account.windows[0].utilization == 0.99  # never zeroed


def test_quota_states_no_source_by_name_when_none_is_wired(
    registry: Registry, tmp_path: Path, quota_home: Path
) -> None:
    sv = _sv(registry, tmp_path)

    account = _accounts(sv.quota(refresh=1))["acct-1"]

    assert account.state is supervisor.QuotaState.NO_SOURCE
    assert account.absence is not None
    assert "acct-1" in account.absence
    assert account.windows == ()


def test_a_refresh_keeps_the_stored_reading_where_no_source_is_wired(
    registry: Registry, tmp_path: Path, quota_home: Path
) -> None:
    """Nothing to pull is not a reason to forget: a refresh asks the sources it
    has, and an account no source reads still answers with its run's reading."""
    sv = _sv(registry, tmp_path)
    now = int(time.time())
    _store_usage(registry, "acct-1", "seven_day", 0.94, now + 3600, now - 600)

    account = _accounts(sv.quota(refresh=1))["acct-1"]

    assert account.state is supervisor.QuotaState.STALE
    assert account.source == supervisor.RUN_WIRE_SOURCE
    assert [(w.window, w.utilization) for w in account.windows] == [("seven_day", 0.94)]
    assert account.absence is None


def test_a_refresh_starts_no_pull_once_its_time_is_spent(
    registry: Registry,
    tmp_path: Path,
    quota_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The node host waits for the whole report: one refresh over many accounts
    keeps starting vendor pulls only for its budget, and every account past it
    answers with what is stored, or says it was not pulled."""
    monkeypatch.setattr(supervisor, "QUOTA_PULL_BUDGET_S", 0.0)
    sv = _sv(registry, tmp_path)
    now = int(time.time())
    _store_usage(registry, "acct-2", "seven_day", 0.4, now + 3600, now - 60)
    source = _RecordingSource(
        _FakeReading(
            windows=(
                terminal.WindowUsage(window="5h", utilization=0.7, resets_at=now + 60),
            ),
            fetched_at=now,
        )
    )

    accounts = _accounts(sv.quota(refresh=1, provider="claude", source=source))

    assert source.calls == [("claude", "acct-1")]
    assert accounts["acct-1"].windows[0].utilization == 0.7
    assert accounts["acct-2"].windows[0].utilization == 0.4
    assert accounts["acct-2"].source == supervisor.RUN_WIRE_SOURCE


def test_quota_carries_the_sources_own_reason_when_it_cannot_answer(
    registry: Registry, tmp_path: Path, quota_home: Path
) -> None:
    sv = _sv(registry, tmp_path)
    source = _RecordingSource(_FakeReading(absence="vendor returned HTTP 503"))

    account = _accounts(sv.quota(refresh=1, source=source))["acct-1"]

    assert account.state is supervisor.QuotaState.NO_SOURCE
    assert account.absence == "vendor returned HTTP 503"
    # A source that failed is named, so a reader can tell it from none wired.
    assert account.source == "fake-vendor-api"


def test_quota_falls_back_to_the_stored_number_when_the_source_cannot_answer(
    registry: Registry, tmp_path: Path, quota_home: Path
) -> None:
    """A refused pull never erases a number already held."""
    sv = _sv(registry, tmp_path)
    now = int(time.time())
    _store_usage(registry, "acct-1", "5h", 0.33, now + 3600, now)
    source = _RecordingSource(_FakeReading(absence="vendor returned HTTP 503"))

    account = _accounts(sv.quota(refresh=1, source=source))["acct-1"]

    assert account.state is supervisor.QuotaState.OBSERVED
    assert account.windows[0].utilization == 0.33
    assert account.source == supervisor.RUN_WIRE_SOURCE


def test_quota_reports_a_pull_under_the_sources_own_name(
    registry: Registry, tmp_path: Path, quota_home: Path
) -> None:
    sv = _sv(registry, tmp_path)
    now = int(time.time())
    source = _RecordingSource(
        _FakeReading(
            windows=(
                terminal.WindowUsage(window="5h", utilization=0.7, resets_at=now + 60),
                terminal.WindowUsage(window="7d", utilization=0.1, resets_at=now - 5),
            ),
            fetched_at=now,
        )
    )

    account = _accounts(sv.quota(refresh=1, source=source))["acct-1"]

    assert source.calls[0] == ("claude", "acct-1")
    assert account.source == "fake-vendor-api"
    assert account.fetched_at == now
    # The account rolls up to the worst of its windows, not the freshest.
    assert account.state is supervisor.QuotaState.EXPIRED
    assert [w.state for w in account.windows] == [
        supervisor.QuotaState.OBSERVED,
        supervisor.QuotaState.EXPIRED,
    ]


def test_quota_does_not_pull_at_all_without_refresh(
    registry: Registry, tmp_path: Path, quota_home: Path
) -> None:
    sv = _sv(registry, tmp_path)
    source = _RecordingSource(_FakeReading(absence="never asked"))

    sv.quota(source=source)

    assert source.calls == []


def test_quota_respects_a_live_account_wall_even_under_refresh(
    registry: Registry, tmp_path: Path, quota_home: Path
) -> None:
    """A credential or default wall: the vendor refused the credential itself,
    so a pull before the wall lifts is not an attempt. A `vendor_reset` wall is
    the exception: its reset is what a refresh exists to re-read (2026-10-03),
    and a source that cannot answer leaves it whole."""
    sv = _sv(registry, tmp_path)
    now = int(time.time())
    registry.record_account_wall("acct-1", now + 3600, "default_1h", "5h", now)
    source = _RecordingSource(_FakeReading(absence="would have been wasted"))

    accounts = _accounts(sv.quota(refresh=99, source=source))

    assert [call for call in source.calls if call[1] == "acct-1"] == []
    assert accounts["acct-1"].cooling_until == now + 3600
    assert accounts["acct-1"].wall_source == "default_1h"
    assert accounts["acct-1"].wall_limit_type == "5h"
    assert accounts["acct-1"].state is supervisor.QuotaState.NEVER_OBSERVED
    # The account NOT walled is still pulled: the deadline fences one credential.
    assert ("claude", "acct-2") in source.calls

    registry.record_account_wall("acct-2", now + 7200, "vendor_reset", "5h", now)
    source.calls.clear()
    accounts = _accounts(sv.quota(refresh=99, source=source))
    assert ("claude", "acct-2") in source.calls
    assert accounts["acct-2"].cooling_until == now + 7200
    assert accounts["acct-2"].wall_source == "vendor_reset"


def test_quota_respects_a_live_provider_wall_for_every_account_under_it(
    registry: Registry, tmp_path: Path, quota_home: Path
) -> None:
    sv = _sv(registry, tmp_path)
    now = int(time.time())
    registry.record_provider_wall("claude", now + 3600, "vendor_reset", now)
    source = _RecordingSource(_FakeReading(absence="would have been wasted"))

    report = sv.quota(refresh=1, source=source)

    assert [call for call in source.calls if call[0] == "claude"] == []
    assert _providers(report)["claude"].cooling_until == now + 3600
    assert _providers(report)["claude"].wall_source == "vendor_reset"
    assert ("codex1", None) in source.calls  # the unwalled provider still asked


def test_quota_asks_an_env_route_provider_for_its_own_windows(
    registry: Registry, tmp_path: Path, quota_home: Path
) -> None:
    """A provider with no accounts has no alias to attribute a reading to, so it
    carries the windows itself — and is asked with `None` for the account."""
    sv = _sv(registry, tmp_path)
    now = int(time.time())
    source = _RecordingSource(
        _FakeReading(
            windows=(
                terminal.WindowUsage(window="5h", utilization=0.2, resets_at=now + 60),
            ),
            fetched_at=now,
        )
    )

    provider = _providers(sv.quota(provider="codex1", refresh=1, source=source))[
        "codex1"
    ]

    assert source.calls == [("codex1", None)]
    assert provider.accounts == ()
    assert provider.state is supervisor.QuotaState.OBSERVED
    assert provider.windows[0].utilization == 0.2


def test_quota_never_observed_reaches_a_provider_that_was_never_asked(
    registry: Registry, tmp_path: Path, quota_home: Path
) -> None:
    provider = _providers(_sv(registry, tmp_path).quota())["codex1"]

    assert provider.state is supervisor.QuotaState.NEVER_OBSERVED
    assert provider.windows == ()
    assert provider.absence is not None and "codex1" in provider.absence


def test_quota_rolls_a_provider_up_to_the_worst_of_its_accounts(
    registry: Registry, tmp_path: Path, quota_home: Path
) -> None:
    sv = _sv(registry, tmp_path)
    now = int(time.time())
    _store_usage(registry, "acct-1", "5h", 0.42, now + 3600, now)  # OBSERVED

    provider = _providers(sv.quota())["claude"]

    assert {account.state for account in provider.accounts} == {
        supervisor.QuotaState.OBSERVED,
        supervisor.QuotaState.NEVER_OBSERVED,
    }
    assert provider.state is supervisor.QuotaState.NEVER_OBSERVED


def test_quota_reads_the_sources_with_the_registry_lock_free(
    registry: Registry, tmp_path: Path, quota_home: Path
) -> None:
    """Six vendor reads under `self._lock` would freeze execute() and status()
    for as long as the slowest vendor takes. Proved by a SECOND thread taking
    the lock while a source is running — and the probe is proved able to fail
    by running it once while the lock is deliberately held."""
    sv = _sv(registry, tmp_path)

    def _lock_is_free() -> bool:
        seen: list[bool] = []

        def _probe() -> None:
            acquired = sv._lock.acquire(timeout=1.0)
            seen.append(acquired)
            if acquired:
                sv._lock.release()

        prober = threading.Thread(target=_probe)
        prober.start()
        prober.join(timeout=5)
        return seen == [True]

    with sv._lock:
        assert _lock_is_free() is False  # the probe can fail

    observations: list[bool] = []

    def _source(provider: str, alias: str | None) -> _FakeReading:
        observations.append(_lock_is_free())
        return _FakeReading(absence="not the point of this test")

    sv.quota(refresh=1, source=_source)

    assert observations == [True, True, True]  # two accounts and one env provider


# ---------------------------------------------------------------------------
# The windows/absence iff, which every state above depends on
# ---------------------------------------------------------------------------


@given(
    has_windows=st.booleans(),
    has_absence=st.booleans(),
    state=st.sampled_from(list(supervisor.QuotaState)),
)
@seed(60613)
@settings(max_examples=60)
def test_a_quota_carries_windows_or_a_reason_and_never_both_or_neither(
    has_windows: bool, has_absence: bool, state: supervisor.QuotaState
) -> None:
    """The invariant the five states rest on: an empty window tuple is only
    readable because a reason always sits beside it. Both illegal quadrants are
    unrepresentable, so `NEVER_OBSERVED` can never arrive as a silent zero."""
    windows = (
        (
            supervisor.WindowQuota(
                window="5h",
                utilization=0.5,
                resets_at=10,
                observed_at=1,
                state=supervisor.QuotaState.OBSERVED,
            ),
        )
        if has_windows
        else ()
    )
    absence = "nobody asked" if has_absence else None

    if has_windows == has_absence:
        with pytest.raises(InvalidRunRequest):
            supervisor.AccountQuota(
                account_alias="acct-1", state=state, windows=windows, absence=absence
            )
        with pytest.raises(InvalidRunRequest):
            supervisor.ProviderQuota(
                provider="claude",
                models=(),
                state=state,
                windows=windows,
                absence=absence,
            )
        return
    supervisor.AccountQuota(
        account_alias="acct-1", state=state, windows=windows, absence=absence
    )
    supervisor.ProviderQuota(
        provider="claude", models=(), state=state, windows=windows, absence=absence
    )


def test_a_provider_with_accounts_carries_neither_windows_nor_a_reason(
    registry: Registry, tmp_path: Path
) -> None:
    """The one exemption, and it is not a hole: the accounts ARE the answer."""
    account = supervisor.AccountQuota(
        account_alias="acct-1",
        state=supervisor.QuotaState.NEVER_OBSERVED,
        absence="nobody asked",
    )

    provider = supervisor.ProviderQuota(
        provider="claude",
        models=("opus",),
        state=supervisor.QuotaState.NEVER_OBSERVED,
        accounts=(account,),
    )

    assert provider.windows == ()
    assert provider.absence is None


@pytest.mark.parametrize(
    ("resets_in", "age", "expected"),
    [
        (1, 0, supervisor.QuotaState.OBSERVED),
        # A window resets AT its own instant: `resets_at == now` is over, the
        # same exclusive boundary a wall and a lease use.
        (0, 0, supervisor.QuotaState.EXPIRED),
        # Freshness is an age BOUND, not an age limit: exactly at it is fresh.
        (1, supervisor.QUOTA_FRESHNESS_S, supervisor.QuotaState.OBSERVED),
        (1, supervisor.QUOTA_FRESHNESS_S + 1, supervisor.QuotaState.STALE),
        # An expired window that is ALSO stale reads EXPIRED: the fact about
        # the number outranks the fact about when we last asked.
        (0, supervisor.QUOTA_FRESHNESS_S + 1, supervisor.QuotaState.EXPIRED),
    ],
)
def test_window_state_sits_exactly_on_its_two_boundaries(
    resets_in: int, age: int, expected: supervisor.QuotaState
) -> None:
    now = 1_000_000

    quota = supervisor._window_quota(
        terminal.WindowUsage(window="5h", utilization=0.5, resets_at=now + resets_in),
        now - age,
        now,
    )

    assert quota.state is expected


# ---------------------------------------------------------------------------
# The provider list does not open the registry or read quota.
# ---------------------------------------------------------------------------


@pytest.fixture
def provider_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """All three account shapes at once, and a `defaults.model` that shares no
    value with any provider's own.

    The default matters: a report that read `defaults.model` instead of the
    provider's own would be accidentally right against a roster where the two
    agree, and this one makes them disagree everywhere.
    """
    home = tmp_path / "provider-home"
    secrets = home / "secrets" / "claude"
    secrets.mkdir(parents=True)
    (secrets / "guara-owner-invalid.token").write_text("x", encoding="utf-8")
    (secrets / "strife.token").write_text("x", encoding="utf-8")
    (secrets / "notes.txt").write_text("not a token", encoding="utf-8")
    (home / "secrets" / "hollow").mkdir(parents=True)
    (home / ".nott").mkdir(parents=True)
    (home / ".nott" / "spawn.toml").write_text(
        "[defaults]\n"
        'model = "roster-default-never-a-provider-model"\n'
        "\n"
        "[[provider]]\n"
        'name = "claude"\n'
        'kind = "claude"\n'
        f'secrets = "{secrets}"\n'
        'model = "claude-opus-5"\n'
        'models = ["claude-opus-5", "claude-sonnet-5"]\n'
        'efforts = ["low", "max"]\n'
        "strength = 9\n"
        "design = 4\n"
        "\n"
        "[[provider]]\n"
        'name = "minimax"\n'
        'kind = "claude"\n'
        'model = "MiniMax-M3"\n'
        'models = ["MiniMax-M3"]\n'
        'efforts = ["max"]\n'
        "thrift = 9\n"
        "\n"
        "[[provider]]\n"
        'name = "hollow"\n'
        'kind = "claude"\n'
        f'secrets = "{home / "secrets" / "hollow"}"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("NOTT_NSPAWN_ROSTER", raising=False)
    monkeypatch.setenv("NOTT_NSPAWN_ROSTER", str(home / ".nott/spawn.toml"))
    # Under file-wins, isolating only by env lets the fixture be overridden
    # by the operator's ~/.nott/spawn.toml: fixing CONFIG is mandatory.
    monkeypatch.setenv("NOTT_NSPAWN_CONFIG", str(tmp_path / "absent-spawn.toml"))
    return home


def test_provider_report_carries_everything_a_router_needs_to_choose(
    provider_home: Path,
) -> None:
    listing = {
        entry.provider: entry for entry in supervisor.provider_report().providers
    }

    assert list(listing) == ["claude", "minimax", "hollow"]
    claude = listing["claude"]
    assert claude.models == ("claude-opus-5", "claude-sonnet-5")
    # DECLARED, never the roster default, which this fixture makes disagree.
    assert claude.default_model == "claude-opus-5"
    assert claude.efforts == ("low", "max")
    # Sorted, and `notes.txt` is not an account.
    assert claude.accounts == ("guara-owner-invalid", "strife")
    assert claude.absence is None
    assert (claude.strength, claude.thrift, claude.design) == (9, None, 4)
    # An undeclared rank is None — never a zero, which would rank a provider
    # LAST on an axis its owner simply never spoke about.
    assert (listing["minimax"].strength, listing["minimax"].thrift) == (None, 9)


def test_provider_report_states_what_each_route_implements(
    provider_home: Path,
) -> None:
    """The capabilities are the IMPLEMENTED ones for the route, never a roster
    claim: a Claude route reviews and writes caged, each in its own slice. A
    Claude route that reaches its model through another endpoint cannot be
    caged (the cage's closed env carries only the vendor credential), so it
    keeps the uncaged review and neither custody nor write."""
    roster = provider_home / ".nott" / "spawn.toml"
    roster.write_text(
        roster.read_text()
        + '\n[[provider]]\nname = "proxy"\nkind = "claude"\n'
        + '[provider.env]\nANTHROPIC_BASE_URL = "https://proxy.invalid"\n'
    )
    listing = {
        entry.provider: entry for entry in supervisor.provider_report().providers
    }
    assert listing["claude"].kind == "claude"
    assert listing["claude"].capabilities == ("custody", "resume", "review", "write")
    assert listing["proxy"].capabilities == ("resume", "review")
    assert supervisor.CAPABILITIES["codex"] == ("custody", "resume", "review", "write")
    # Measured 2026-09-29 (P-G E0, /var/tmp/p-g-e0/RESULT.md, agy 1.2.13): under
    # nSPAWN's `--sandbox` argv the file tools wrote outside the checkout in plan
    # mode, BypassSandbox ran `sudo` unprompted and a child outlived killpg. No
    # custody until an outer cage is built and proved.
    assert supervisor.CAPABILITIES["agy"] == ("resume", "review")
    assert supervisor.provider_report().roles == {}


@pytest.mark.parametrize(
    "declaration",
    [
        '\n[defaults.env]\nANTHROPIC_BASE_URL = "https://proxy.invalid"\n',
        (
            '\n[[provider]]\nname = "local"\nkind = "claude"\n'
            '[provider.accounts.vllm]\nbase_url = "http://127.0.0.1:8000"\n'
        ),
    ],
    ids=["defaults-env", "account-base-url"],
)
def test_a_claude_route_proxied_from_elsewhere_lists_no_cage(
    provider_home: Path, declaration: str
) -> None:
    """The listing reads the route as dispatch resolves it: `[defaults.env]`
    under the provider's, and each account's own endpoint."""
    roster = provider_home / ".nott" / "spawn.toml"
    roster.write_text(roster.read_text() + declaration)
    listing = {
        entry.provider: entry for entry in supervisor.provider_report().providers
    }
    name = "local" if "local" in listing else "claude"
    assert listing[name].capabilities == ("resume", "review")


def test_provider_report_carries_the_declared_role_binding(
    provider_home: Path,
) -> None:
    roster = provider_home / ".nott" / "spawn.toml"
    roster.write_text(
        roster.read_text(encoding="utf-8") + '\n[roles]\nreviewer = "minimax"\n',
        encoding="utf-8",
    )
    assert supervisor.provider_report().roles == {"reviewer": "minimax"}


def test_provider_report_carries_the_declared_role_profile(
    provider_home: Path,
) -> None:
    roster = provider_home / ".nott" / "spawn.toml"
    roster.write_text(
        roster.read_text(encoding="utf-8")
        + '\n[roles]\nreviewer = "minimax"\n'
        + '[role_profile.reviewer]\nmodel = "MiniMax-M3"\neffort = "max"\n',
        encoding="utf-8",
    )
    report = supervisor.provider_report()
    assert report.roles == {"reviewer": "minimax"}
    assert report.role_profiles == {
        "reviewer": {"model": "MiniMax-M3", "effort": "max"}
    }


def test_provider_report_carries_the_declared_writer_profile(
    provider_home: Path,
) -> None:
    """The writer's profile reaches the report beside the reviewer's, each its own."""
    roster = provider_home / ".nott" / "spawn.toml"
    roster.write_text(
        roster.read_text(encoding="utf-8")
        + '\n[roles]\nwriter = "minimax"\nreviewer = "minimax"\n'
        + '[role_profile.writer]\nmodel = "MiniMax-M3"\n'
        + '[role_profile.reviewer]\neffort = "max"\n',
        encoding="utf-8",
    )
    report = supervisor.provider_report()
    assert report.roles == {"writer": "minimax", "reviewer": "minimax"}
    assert report.role_profiles == {
        "writer": {"model": "MiniMax-M3"},
        "reviewer": {"effort": "max"},
    }


def test_provider_report_names_why_an_env_route_carries_no_account(
    provider_home: Path,
) -> None:
    """The empty account tuple that is NOT a defect: eight of the nine live
    providers authenticate from the environment."""
    minimax = supervisor.provider_report(provider="minimax").providers[0]

    assert minimax.accounts == ()
    assert minimax.absence is not None
    assert "no secrets directory" in minimax.absence
    assert "environment" in minimax.absence


def test_provider_report_separates_a_declared_store_that_answered_nothing(
    provider_home: Path,
) -> None:
    """A DIFFERENT fact about a different roster line, and the wording claims
    only what was observed: `discovered_accounts` collapses every OSError to no
    entries, so an unreadable directory lands here too."""
    hollow = supervisor.provider_report(provider="hollow").providers[0]

    assert hollow.accounts == ()
    assert hollow.absence is not None
    assert str(provider_home / "secrets" / "hollow") in hollow.absence
    assert "no readable .token file was found" in hollow.absence
    assert "environment" not in hollow.absence


def test_provider_report_filters_to_one_and_refuses_an_undeclared_name(
    provider_home: Path,
) -> None:
    assert [
        entry.provider for entry in supervisor.provider_report("claude").providers
    ] == ["claude"]
    with pytest.raises(InvalidRunRequest, match="'nope' is not declared"):
        supervisor.provider_report("nope")


def test_the_roster_refusal_names_what_the_roster_declares(
    provider_home: Path,
) -> None:
    """Both roster verbs refuse THROUGH this gate, so both carry the vocabulary.

    Measured through the door on 2026-08-28: `nspawn_run_start(provider="codex")` already
    returned the accepted names (picker.py) while `nspawn_quota_show(provider=
    "codex")` only said that `codex` does not exist. Two surfaces of the SAME
    roster telling different stories — and the silent one is the one that sends the agent to read
    `~/.nott/spawn.toml` from disk.
    """
    with pytest.raises(InvalidRunRequest) as refusal:
        supervisor.provider_report("nope")
    message = str(refusal.value)
    for declared in ("claude", "minimax", "hollow"):
        assert declared in message, f"a recusa nao nomeia {declared}"


def test_a_declared_roster_that_is_absent_refuses_and_an_empty_one_does_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two absences, two meanings, and the line between them changed.

    BEFORE: the DERIVED absence — a HOME under which no roster existed —
    was an empty page, because `Roster.read` says a missing file is an
    empty roster and never an error. Under file-wins that absence stopped
    existing: whoever declares `[store].roster` NAMES a path, and naming a
    file that is not there is an operator's complaint, not a host's silence.
    Found by adversarial review 2026-08-30, which called the empty page in
    that case "the empty fleet that strict reading exists to
    prevent".

    WHAT REMAINS as an empty page is the one honest case: a roster that
    EXISTS and declares no provider at all. This host declared, and declared
    nothing.
    """
    monkeypatch.delenv("NOTT_NSPAWN_ROSTER", raising=False)

    ausente = tmp_path / "nunca-esteve-la.toml"
    monkeypatch.setenv("NOTT_NSPAWN_CONFIG", str(ausente))
    with pytest.raises(supervisor.NSpawnError) as recusa:
        supervisor.provider_report()
    assert "single config file" in str(recusa.value), (
        "the refusal has to name the SOURCE THAT WON, never the demoted variable"
    )
    assert str(ausente) in str(recusa.value)

    vazio = tmp_path / "declara-nada.toml"
    vazio.write_text("")
    monkeypatch.setenv("NOTT_NSPAWN_CONFIG", str(vazio))
    assert supervisor.provider_report().providers == (), (
        "a roster that EXISTS and declares nothing is the empty page; that host "
        "declared, and declared nothing"
    )


def test_providers_answers_with_the_registry_NEVER_CONNECTED(
    tmp_path: Path, provider_home: Path
) -> None:
    """THE ACCEPTANCE OF THIS VERB, at the supervisor boundary.

    The registry is built against a file that does not exist and `connect`
    is never called, so ANY registry call this path made would raise before it
    could answer. A green run is therefore proof of zero store touches, not an
    assertion about them.
    """
    dead = Registry(tmp_path / "absent" / "nspawn.sqlite3", "i-dead")
    sv = Supervisor(dead, tmp_path / "runs")

    report = sv.providers()

    assert [entry.provider for entry in report.providers] == [
        "claude",
        "minimax",
        "hollow",
    ]
    assert sv.providers(provider="claude").providers[0].accounts == (
        "guara-owner-invalid",
        "strife",
    )


@given(has_accounts=st.booleans(), has_absence=st.booleans())
@seed(90210)
@settings(max_examples=20)
def test_a_listing_carries_accounts_or_a_reason_and_never_both_or_neither(
    has_accounts: bool, has_absence: bool
) -> None:
    """The same iff the quota windows carry, on the same gate: an empty account
    tuple is readable only because a reason always sits beside it."""

    def _build() -> supervisor.ProviderListing:
        return supervisor.ProviderListing(
            provider="claude",
            kind="claude",
            capabilities=("resume", "review"),
            models=(),
            default_model=None,
            efforts=(),
            accounts=("acct-1",) if has_accounts else (),
            absence="nobody declared one" if has_absence else None,
            strength=None,
            thrift=None,
            design=None,
        )

    if has_accounts == has_absence:
        with pytest.raises(InvalidRunRequest, match="accounts"):
            _build()
        return
    _build()


def test_any_refresh_source_reconciles_the_usage_the_picker_reads(
    registry: Registry, tmp_path: Path, quota_home: Path
) -> None:
    """A non-native source (an env route's HTTP read) names its windows
    differently from the run wire. Its fresh 25% must replace the stale 100%
    row, or the picker stays walled for 20 days behind a report that says 25%."""
    sv = _sv(registry, tmp_path)
    now = int(time.time())
    _store_usage(
        registry, "acct-1", "codex.secondary", 1.0, now + 20 * 86400, now - 86400
    )
    assert "acct-1" in registry.get_account_walls(now)
    source = _RecordingSource(
        _FakeReading(
            windows=(terminal.WindowUsage("secondary", 0.25, now + 20 * 86400),),
            fetched_at=now,
        )
    )

    account = _accounts(sv.quota(refresh=1, source=source))["acct-1"]

    assert (account.source, account.cooling_until) == ("fake-vendor-api", None)
    assert "acct-1" not in registry.get_account_walls(now)
    assert [
        (row.usage_window, row.utilization)
        for row in registry.get_account_usage()
        if row.account_alias == "acct-1"
    ] == [("secondary", 0.25)]


def test_an_older_cached_reading_never_overwrites_a_newer_stored_one(
    registry: Registry, tmp_path: Path, quota_home: Path
) -> None:
    """A source may serve a last good answer with its ORIGINAL instant. That
    cached 14% must not replace the 100% a run saw later."""
    now = int(time.time())
    _store_usage(registry, "acct-1", "secondary", 1.0, now + 20 * 86400, now)
    source = _RecordingSource(
        _FakeReading(
            windows=(terminal.WindowUsage("secondary", 0.14, now + 20 * 86400),),
            fetched_at=now - 100,
        )
    )

    _sv(registry, tmp_path).quota(refresh=1, source=source)

    assert registry.get_account_walls(now).get("acct-1") == now + 20 * 86400
    stored = {
        row.usage_window: row.utilization
        for row in registry.get_account_usage()
        if row.account_alias == "acct-1"
    }
    assert stored["secondary"] == 1.0


def test_a_window_below_100_percent_derives_no_wall(registry: Registry) -> None:
    """The brief's known exhausted window is utilization >= 1. An account at 96%
    of a weekly window is not walled for the week on no failure at all."""
    now = int(time.time())
    _store_usage(registry, "acct-1", "secondary", 0.96, now + 5 * 86400, now)
    assert registry.get_account_walls(now) == {}
    _store_usage(registry, "acct-1", "secondary", 1.0, now + 5 * 86400, now)
    assert registry.get_account_walls(now) == {"acct-1": now + 5 * 86400}


def test_provider_report_carries_native_family_catalogue(
    provider_home: Path,
    claude_models: dict[str, str],
) -> None:
    roster = provider_home / ".nott/spawn.toml"
    roster.write_text(
        roster.read_text().replace(
            'models = ["claude-opus-5", "claude-sonnet-5"]',
            'models = ["opus", "sonnet", "haiku"]',
        )
    )
    entries = supervisor.provider_report().providers
    assert entries[0].model_catalog == claude_models
    assert entries[1].model_catalog is None


def test_one_missing_claude_client_does_not_hide_other_routes(
    provider_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nspawn.claude_catalog import ModelCatalogError

    roster = provider_home / ".nott/spawn.toml"
    roster.write_text(
        roster.read_text().replace(
            'models = ["claude-opus-5", "claude-sonnet-5"]',
            'models = ["opus", "sonnet", "haiku"]',
        )
    )

    def unavailable(_: object) -> dict[str, str]:
        raise ModelCatalogError("test client is unavailable")

    monkeypatch.setattr("nspawn.claude_catalog.catalog_read", unavailable)
    entries = supervisor.provider_report().providers
    assert [entry.provider for entry in entries] == ["claude", "minimax", "hollow"]
    assert entries[0].model_catalog is None
    assert entries[0].model_catalog_error == "test client is unavailable"
