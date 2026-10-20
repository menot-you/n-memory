"""Supervisor failover contracts."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from supervisor_support import (
    FakeSpawnedChild,
    _admit_pending,
    _classify_by_exit_code,
    _drain_worker_threads,
    _spawn_real,
    _stop_every_reaper,
    _sv,
    _wire_l8_and_l3,
    fake_picked,
    fake_provider,
    registry,
    roster_home,
)

from nspawn import claude_child, picker, supervisor, terminal
from nspawn.owner_roster import Provider
from nspawn.process_identity import ProcessIdentity, capture_process_identity
from nspawn.registry import Registry
from nspawn.rows import AccountUsageRow, RunPhase
from nspawn.supervisor import (
    Supervisor,
)

# Export imported fixtures so pytest discovers them in this module.
__all__ = [
    "_drain_worker_threads",
    "_stop_every_reaper",
    "fake_picked",
    "fake_provider",
    "registry",
    "roster_home",
]


# U-failover — a rejected credential (401/403) must not kill a run that has
# more accounts to try: the supervisor walls the rejected alias and re-spawns
# the SAME run on the next unwalled account, bounded by one attempt per
# account. A caller-pinned account never fails over — the choice was explicit.


def _failover_roster(
    tmp_path: Path,
    accounts: tuple[str, ...] = ("acct-a", "acct-b"),
) -> tuple[object, object]:
    from nspawn.owner_roster import Defaults, Roster

    secrets = tmp_path / "secrets" / "claude"
    secrets.mkdir(parents=True, exist_ok=True)
    for alias in accounts:
        (secrets / f"{alias}.token").write_text("x", encoding="utf-8")
    provider = Provider(name="claude", secrets=secrets, models=["m"], model="m")
    roster = Roster(providers=[provider], defaults=Defaults(plugin_root=tmp_path))
    return roster, provider


def _failover_picked(provider: object, *, requested: str = "claude") -> object:
    return picker.PickerResult(
        requested_provider=requested,
        executed_provider=provider,
        executed_account="acct-a",
        model="m",
        effort=None,
    )


def _verdict_401() -> terminal.TerminalClassification:
    return terminal.TerminalClassification(
        phase=RunPhase.FAILED,
        final_text=None,
        warning="terminal result did not complete (api_error)",
        marker=None,
        provider_wall=None,
        account_wall=None,
        api_error_status=401,
    )


def _verdict_402() -> terminal.TerminalClassification:
    return terminal.TerminalClassification(
        phase=RunPhase.FAILED,
        final_text=None,
        warning="provider quota exhausted (402: ProviderExhausted/QuotaLimit)",
        marker=None,
        provider_wall=None,
        account_wall=None,
        api_error_status=402,
    )


def _verdict_account_wall_quota(
    account: str = "acct-a",
) -> terminal.TerminalClassification:
    now = int(time.time())
    return terminal.TerminalClassification(
        phase=RunPhase.FAILED,
        final_text=None,
        warning="vendor rate limit rejected (five_hour)",
        marker=None,
        provider_wall=None,
        account_wall=terminal.AccountWallRecord(
            account_alias=account,
            cooling_until=now + 3600,
            source=terminal.WallSource.DEFAULT_1H,
            limit_type="five_hour",
            observed_at=now,
        ),
    )


def _verdict_429() -> terminal.TerminalClassification:
    return terminal.TerminalClassification(
        phase=RunPhase.FAILED,
        final_text=None,
        warning="provider rate limit exceeded (429: ProviderExhausted/QuotaLimit)",
        marker=None,
        provider_wall=None,
        account_wall=None,
        api_error_status=429,
    )


def _verdict_404() -> terminal.TerminalClassification:
    return terminal.TerminalClassification(
        phase=RunPhase.FAILED,
        final_text=None,
        warning="terminal result did not complete (model_not_found)",
        marker=None,
        provider_wall=None,
        account_wall=None,
        api_error_status=404,
    )


def _verdict_completed() -> terminal.TerminalClassification:
    return terminal.TerminalClassification(
        phase=RunPhase.COMPLETED,
        final_text="PING_OK",
        warning=None,
        marker=None,
        provider_wall=None,
        account_wall=None,
    )


def _drive_failover_run(
    sv: Supervisor,
    registry: Registry,
    tmp_path: Path,
    roster: object,
    picked: object,
) -> str:
    run_id, thread_id = _admit_pending(sv, registry, tmp_path, worktree=str(tmp_path))
    run_dir = supervisor.RunDirectory(
        root=sv._run_root_path / run_id,
        stdout=sv._run_root_path / run_id / "stdout",
        stderr=sv._run_root_path / run_id / "stderr",
    )
    try:
        sv._drive_run(
            run_id,
            str(tmp_path),
            run_dir,
            "ping",
            roster,
            picked,
            thread_id,
            False,
            True,
        )
    finally:
        # Stop the reaper daemon before this test's fixtures drop the schema:
        # a live tick against a dropped schema dies on an unrelated test.
        sv.shutdown()
    return run_id


def test_a_rejected_credential_fails_over_to_the_next_account(
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """401 on acct-a walls that alias and re-spawns the run on acct-b."""
    roster, provider = _failover_roster(tmp_path)
    spawned: list[int] = []

    def spawned_factory(*a: object, **k: object) -> FakeSpawnedChild:
        spawned.append(1)
        return FakeSpawnedChild(_spawn_real("true"))

    _wire_l8_and_l3(monkeypatch, spawned_factory)
    verdicts = [_verdict_401(), _verdict_completed()]
    monkeypatch.setattr(
        supervisor.terminal, "classify_terminal_run", lambda *a, **k: verdicts.pop(0)
    )
    sv = _sv(registry, tmp_path)
    picked = _failover_picked(provider)

    run_id = _drive_failover_run(sv, registry, tmp_path, roster, picked)

    row = registry.get_run(run_id)
    assert row is not None
    assert row.phase is RunPhase.COMPLETED
    assert row.executed_account == "acct-b"
    assert len(spawned) == 2
    now = int(time.time()) + 1
    assert registry.get_account_walls(now).get("acct-a", 0) > now


def test_a_failover_goes_where_the_shared_picker_sends_it(
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """401 on acct-a: the retry takes the account the spread scores best
    (acct-c), not the next declared one (acct-b, its week nearly spent)."""
    roster, provider = _failover_roster(tmp_path, ("acct-a", "acct-b", "acct-c"))
    now = int(time.time())
    registry.record_account_usage(
        (AccountUsageRow("acct-b", "seven_day", 0.9, now + 86400, now),)
    )
    _wire_l8_and_l3(monkeypatch, lambda *a, **k: FakeSpawnedChild(_spawn_real("true")))
    verdicts = [_verdict_401(), _verdict_completed()]
    monkeypatch.setattr(
        supervisor.terminal, "classify_terminal_run", lambda *a, **k: verdicts.pop(0)
    )
    sv = _sv(registry, tmp_path)

    run_id = _drive_failover_run(
        sv, registry, tmp_path, roster, _failover_picked(provider)
    )

    row = registry.get_run(run_id)
    assert row is not None
    assert (row.phase, row.executed_account) == (RunPhase.COMPLETED, "acct-c")


def _override_roster(tmp_path: Path) -> tuple[object, object]:
    """acct-a runs the provider's model; acct-b overrides it with its own."""
    from nspawn.owner_roster import Defaults, NativeAccount, Roster

    provider = Provider(name="claude", models=["m", "luna"], model="m")
    provider.accounts = {
        "acct-a": NativeAccount(home=None, capacity=1),
        "acct-b": NativeAccount(home=None, model="luna", capacity=1),
    }
    roster = Roster(providers=[provider], defaults=Defaults(plugin_root=tmp_path))
    return roster, provider


@pytest.mark.parametrize(
    ("requested_model", "rotates_to"),
    [(None, ("acct-b", "luna")), ("m", None)],
    ids=["omitted-takes-the-override", "explicit-is-never-replaced"],
)
def test_a_credential_retry_keeps_the_callers_model_request(
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    requested_model: str | None,
    rotates_to: tuple[str, str] | None,
) -> None:
    """The re-pick asks what the CALLER asked, not the model the first pick
    resolved: an omitted model still rotates onto an overriding account, and an
    explicit one is refused there rather than replaced."""
    roster, _ = _override_roster(tmp_path)
    sv = _sv(registry, tmp_path)
    try:
        picked = picker.pick_execution(
            roster,
            int(time.time()),
            requested_provider="claude",
            requested_model=requested_model,
            account_load={"acct-b": 1},
        )
        assert (picked.executed_account, picked.model) == ("acct-a", "m")
        sv._wall_rejected_credential(picked, limit_type="credential_rejected")
        nxt = sv._repick_after_credential_rejection(roster, picked, {"acct-a"})
    finally:
        sv.shutdown()
    if rotates_to is None:
        # Refused by name on acct-b, never replaced and never read as exhaustion.
        assert isinstance(nxt, picker.PickerError)
        assert '"luna"' in str(nxt)
    else:
        assert isinstance(nxt, picker.PickerResult)
        assert (nxt.executed_account, nxt.model) == rotates_to


@pytest.mark.parametrize("execution_class", [None, "review"], ids=["no-class", "class"])
def test_a_credential_retry_keeps_the_callers_effort_request(
    registry: Registry,
    tmp_path: Path,
    execution_class: str | None,
) -> None:
    """An effort the first pick resolved is not one the caller pinned: the retry
    still rotates where a roster env literal sets the effort."""
    from nspawn.owner_roster import ClassPolicy, Defaults, NativeAccount, Roster

    provider = Provider(
        name="claude",
        models=["m"],
        model="m",
        effort="high",
        efforts=["low", "medium", "high"],
        env={"CLAUDE_CODE_EFFORT_LEVEL": "low"},
    )
    provider.accounts = {
        "acct-a": NativeAccount(home=None, capacity=1),
        "acct-b": NativeAccount(home=None, capacity=1),
    }
    roster = Roster(
        providers=[provider],
        defaults=Defaults(plugin_root=tmp_path),
        classes={"review": ClassPolicy(effort="medium", ceiling="high")},
    )
    sv = _sv(registry, tmp_path)
    try:
        picked = picker.pick_execution(
            roster,
            int(time.time()),
            requested_provider="claude",
            execution_class=execution_class,
            account_load={"acct-b": 1},
        )
        assert picked.executed_account == "acct-a"
        sv._wall_rejected_credential(picked, limit_type="credential_rejected")
        nxt = sv._repick_after_credential_rejection(
            roster, picked, {"acct-a"}, execution_class=execution_class
        )
    finally:
        sv.shutdown()
    assert nxt is not None
    assert (nxt.executed_account, nxt.effort) == ("acct-b", picked.effort)


@pytest.mark.parametrize(
    ("override", "named"),
    [("account-model", 'acct-b runs "luna"'), ("account-env", "sets ANTHROPIC_MODEL")],
)
def test_a_retry_the_next_account_refuses_names_that_refusal(
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    override: str,
    named: str,
) -> None:
    """acct-b was never tried: its model refusal is the cause, not exhaustion.
    An uncaged retry's child takes acct-b's env, so an env override counts too."""
    from nspawn.owner_roster import NativeAccount

    roster, provider = _override_roster(tmp_path)
    if override == "account-env":
        provider.kind = "claude"
        provider.accounts["acct-b"] = NativeAccount(
            home=None, env={"ANTHROPIC_MODEL": "luna"}, capacity=1
        )
    _wire_l8_and_l3(monkeypatch, lambda *a, **k: FakeSpawnedChild(_spawn_real("true")))
    verdicts = [_verdict_401()]
    monkeypatch.setattr(
        supervisor.terminal, "classify_terminal_run", lambda *a, **k: verdicts.pop(0)
    )
    sv = _sv(registry, tmp_path)
    picked = picker.pick_execution(
        roster,
        int(time.time()),
        requested_provider="claude",
        requested_model="m",
        account_load={"acct-b": 1},
    )
    run_id = _drive_failover_run(sv, registry, tmp_path, roster, picked)
    row = registry.get_run(run_id)
    assert row is not None and row.warning is not None
    assert "every account" not in row.warning
    assert named in row.warning


def test_the_execution_class_reaches_the_credential_retry(
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roster, provider = _failover_roster(tmp_path)
    _wire_l8_and_l3(monkeypatch, lambda *a, **k: FakeSpawnedChild(_spawn_real("true")))
    verdicts = [_verdict_401()]
    monkeypatch.setattr(
        supervisor.terminal, "classify_terminal_run", lambda *a, **k: verdicts.pop(0)
    )
    seen: list[object] = []

    def spy(self: object, *args: object, **kwargs: object) -> None:
        seen.append(kwargs.get("execution_class"))

    monkeypatch.setattr(Supervisor, "_repick_after_credential_rejection", spy)
    sv = _sv(registry, tmp_path)
    run_id, thread_id = _admit_pending(sv, registry, tmp_path, worktree=str(tmp_path))
    run_dir = supervisor.RunDirectory(
        root=sv._run_root_path / run_id,
        stdout=sv._run_root_path / run_id / "stdout",
        stderr=sv._run_root_path / run_id / "stderr",
    )
    try:
        sv._drive_run(
            run_id,
            str(tmp_path),
            run_dir,
            "ping",
            roster,
            _failover_picked(provider),
            thread_id,
            False,
            True,
            execution_class="review",
        )
    finally:
        sv.shutdown()
    assert seen == ["review"]


def _env_roster(home: Path, tmp_path: Path, *, proxied: bool = False) -> None:
    """A claude route whose `[defaults.env]` pins an effort; direct unless its
    account reaches the model through another endpoint."""
    account_home = tmp_path / "claude-a"
    account_home.mkdir()
    endpoint = ', base_url = "http://proxy.invalid"' if proxied else ""
    (home / ".nott" / "spawn.toml").write_text(
        f'[bins]\nclaude = "/usr/bin/true"\n'
        f'[defaults.env]\nCLAUDE_CODE_EFFORT_LEVEL = "low"\n'
        f'[[provider]]\nname = "claude"\nkind = "claude"\nmodel = "m"\n'
        f'models = ["m"]\nefforts = ["low", "high"]\n'
        f'accounts = {{ a = {{ home = "{account_home}"{endpoint} }} }}\n',
        encoding="utf-8",
    )


def test_a_caged_review_is_not_judged_by_env_the_cage_never_passes_on(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cage builds a closed env: `[defaults.env]` never reaches it, so an
    explicit effort there runs as requested and is admitted."""
    _env_roster(roster_home, tmp_path)
    started: list[object] = []
    monkeypatch.setattr(
        Supervisor, "_drive_claude_caged", lambda self, *a, **k: started.append(a)
    )
    sv = _sv(registry, tmp_path)
    try:
        sv.review("feature-x", str(tmp_path), provider="a", effort="high")
    finally:
        sv.shutdown()
    assert len(started) == 1


@pytest.mark.parametrize("run", ["read-only-execute", "proxied-review"])
def test_an_uncaged_run_is_judged_by_the_env_its_child_receives(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    run: str,
) -> None:
    """A review on a route proxied elsewhere runs uncaged, with the roster's env."""
    _env_roster(roster_home, tmp_path, proxied=run == "proxied-review")
    sv = _sv(registry, tmp_path)
    try:
        with pytest.raises(picker.PickerError, match="CLAUDE_CODE_EFFORT_LEVEL"):
            if run == "proxied-review":
                sv.review("feature-x", str(tmp_path), provider="a", effort="high")
            else:
                sv.execute(
                    "p", str(tmp_path), provider="a", effort="high", read_only=True
                )
    finally:
        sv.shutdown()


def test_a_codex_review_is_never_judged_by_claude_only_roster_args(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`[defaults] args` that only a Claude launch resolves (and refuses) never
    reach a native Codex review: its admission goes on to native preparation."""
    from nspawn import native
    from nspawn.owner_roster import Provider

    (roster_home / ".nott" / "spawn.toml").write_text(
        '[defaults]\nargs = ["--append-system-prompt-file=/law/NOTT.md"]\n',
        encoding="utf-8",
    )
    codex = Provider(name="codex", kind="codex", model="m", models=["m"])
    picked = picker.PickerResult("codex", codex, "one", "m", None, None)
    monkeypatch.setattr(supervisor.picker, "pick_execution", lambda *a, **k: picked)

    class Prepared(Exception):
        pass

    def prepared(*args: object, **kwargs: object) -> None:
        raise Prepared

    monkeypatch.setattr(native, "prepare_native", prepared)
    sv = _sv(registry, tmp_path)
    try:
        with pytest.raises(Prepared):
            sv.review("feature-x", str(tmp_path), provider="codex")
    finally:
        sv.shutdown()


def test_a_pinned_account_never_fails_over(
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller who pinned the alias by name owns the failure: one spawn, no retry."""
    roster, provider = _failover_roster(tmp_path)
    spawned: list[int] = []

    def spawned_factory(*a: object, **k: object) -> FakeSpawnedChild:
        spawned.append(1)
        return FakeSpawnedChild(_spawn_real("true"))

    _wire_l8_and_l3(monkeypatch, spawned_factory)
    verdicts = [_verdict_401()]
    monkeypatch.setattr(
        supervisor.terminal, "classify_terminal_run", lambda *a, **k: verdicts.pop(0)
    )
    sv = _sv(registry, tmp_path)
    picked = _failover_picked(provider, requested="acct-a")

    run_id = _drive_failover_run(sv, registry, tmp_path, roster, picked)

    row = registry.get_run(run_id)
    assert row is not None
    assert row.phase is RunPhase.FAILED
    assert len(spawned) == 1


def test_every_account_rejected_names_them_all(
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both accounts 401 → run FAILED and the row names every rejected alias."""
    roster, provider = _failover_roster(tmp_path)
    spawned: list[int] = []

    def spawned_factory(*a: object, **k: object) -> FakeSpawnedChild:
        spawned.append(1)
        return FakeSpawnedChild(_spawn_real("true"))

    _wire_l8_and_l3(monkeypatch, spawned_factory)
    verdicts = [_verdict_401(), _verdict_401()]
    monkeypatch.setattr(
        supervisor.terminal, "classify_terminal_run", lambda *a, **k: verdicts.pop(0)
    )
    sv = _sv(registry, tmp_path)
    picked = _failover_picked(provider)

    run_id = _drive_failover_run(sv, registry, tmp_path, roster, picked)

    row = registry.get_run(run_id)
    assert row is not None
    assert row.phase is RunPhase.FAILED
    assert len(spawned) == 2
    reason = (row.warning or "") + (row.blocked_reason or "")
    assert "acct-a" in reason and "acct-b" in reason


def test_quota_402_fails_over_to_the_next_account(
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """402 on acct-a walls that alias and re-spawns the run on acct-b."""
    roster, provider = _failover_roster(tmp_path)
    spawned: list[int] = []

    def spawned_factory(*a: object, **k: object) -> FakeSpawnedChild:
        spawned.append(1)
        return FakeSpawnedChild(_spawn_real("true"))

    _wire_l8_and_l3(monkeypatch, spawned_factory)
    verdicts = [_verdict_402(), _verdict_completed()]
    monkeypatch.setattr(
        supervisor.terminal, "classify_terminal_run", lambda *a, **k: verdicts.pop(0)
    )
    sv = _sv(registry, tmp_path)
    picked = _failover_picked(provider)

    run_id = _drive_failover_run(sv, registry, tmp_path, roster, picked)

    row = registry.get_run(run_id)
    assert row is not None
    assert row.phase is RunPhase.COMPLETED
    assert row.executed_account == "acct-b"
    assert len(spawned) == 2
    now = int(time.time()) + 1
    assert registry.get_account_walls(now).get("acct-a", 0) > now


def test_quota_account_wall_fails_over_to_the_next_account(
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Vendor rate-limit event rejection on acct-a re-spawns the run on acct-b."""
    roster, provider = _failover_roster(tmp_path)
    spawned: list[int] = []

    def spawned_factory(*a: object, **k: object) -> FakeSpawnedChild:
        spawned.append(1)
        return FakeSpawnedChild(_spawn_real("true"))

    _wire_l8_and_l3(monkeypatch, spawned_factory)
    verdicts = [_verdict_account_wall_quota("acct-a"), _verdict_completed()]
    monkeypatch.setattr(
        supervisor.terminal, "classify_terminal_run", lambda *a, **k: verdicts.pop(0)
    )
    sv = _sv(registry, tmp_path)
    picked = _failover_picked(provider)

    run_id = _drive_failover_run(sv, registry, tmp_path, roster, picked)

    row = registry.get_run(run_id)
    assert row is not None
    assert row.phase is RunPhase.COMPLETED
    assert row.executed_account == "acct-b"
    assert len(spawned) == 2
    now = int(time.time()) + 1
    assert registry.get_account_walls(now).get("acct-a", 0) > now


def test_every_account_quota_exhausted_names_them_all(
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both accounts quota exhausted → run FAILED and the row names every exhausted alias."""
    roster, provider = _failover_roster(tmp_path)
    spawned: list[int] = []

    def spawned_factory(*a: object, **k: object) -> FakeSpawnedChild:
        spawned.append(1)
        return FakeSpawnedChild(_spawn_real("true"))

    _wire_l8_and_l3(monkeypatch, spawned_factory)
    verdicts = [_verdict_402(), _verdict_402()]
    monkeypatch.setattr(
        supervisor.terminal, "classify_terminal_run", lambda *a, **k: verdicts.pop(0)
    )
    sv = _sv(registry, tmp_path)
    picked = _failover_picked(provider)

    run_id = _drive_failover_run(sv, registry, tmp_path, roster, picked)

    row = registry.get_run(run_id)
    assert row is not None
    assert row.phase is RunPhase.FAILED
    assert len(spawned) == 2
    reason = (row.warning or "") + (row.blocked_reason or "")
    assert "acct-a" in reason and "acct-b" in reason


@pytest.mark.parametrize("verdict_fn", [_verdict_404])
def test_non_rotatable_error_classes_never_fail_over(
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    verdict_fn: object,
) -> None:
    """A model 404 must not fail over to another account."""
    roster, provider = _failover_roster(tmp_path)
    spawned: list[int] = []

    def spawned_factory(*a: object, **k: object) -> FakeSpawnedChild:
        spawned.append(1)
        return FakeSpawnedChild(_spawn_real("true"))

    _wire_l8_and_l3(monkeypatch, spawned_factory)
    verdicts = [verdict_fn()]  # type: ignore[operator]
    monkeypatch.setattr(
        supervisor.terminal, "classify_terminal_run", lambda *a, **k: verdicts.pop(0)
    )
    sv = _sv(registry, tmp_path)
    picked = _failover_picked(provider)

    run_id = _drive_failover_run(sv, registry, tmp_path, roster, picked)

    row = registry.get_run(run_id)
    assert row is not None
    assert row.phase is RunPhase.FAILED
    assert len(spawned) == 1


@pytest.mark.parametrize(
    ("frames", "stderr_text", "exit_code"),
    [
        (
            [
                {
                    "type": "rate_limit_event",
                    "rate_limit_info": {
                        "status": "rejected",
                        "rateLimitType": "five_hour",
                    },
                },
                {
                    "type": "result",
                    "is_error": True,
                    "terminal_reason": "error",
                    "result": "refused",
                    "api_error_status": 404,
                },
            ],
            "",
            1,
        ),
        (
            [
                {
                    "type": "rate_limit_event",
                    "rate_limit_info": {
                        "status": "rejected",
                        "rateLimitType": "five_hour",
                    },
                },
                {
                    "type": "result",
                    "is_error": True,
                    "terminal_reason": "error",
                    "result": "refused",
                    "api_error_status": 400,
                },
            ],
            "",
            1,
        ),
        (
            [
                {
                    "type": "rate_limit_event",
                    "rate_limit_info": {
                        "status": "rejected",
                        "rateLimitType": "five_hour",
                    },
                },
            ],
            "",
            -9,
        ),
    ],
    ids=[
        "model404_with_wall",
        "caller400_with_wall",
        "signal_with_wall",
    ],
)
def test_non_rotatable_error_classes_with_wall_event_never_fail_over(
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    frames: list[dict[str, object]],
    stderr_text: str,
    exit_code: int,
) -> None:
    """Non-rotatable failures (404, 400, signal) must not rotate even with a wall event."""
    roster, provider = _failover_roster(tmp_path)
    spawned: list[int] = []

    def spawned_factory(*a: object, **k: object) -> FakeSpawnedChild:
        spawned.append(1)
        run_dir_path = a[3]
        assert isinstance(run_dir_path, Path)
        if len(spawned) == 1:
            out = "".join(json.dumps(f) + "\n" for f in frames)
            err = stderr_text
            code = exit_code
        else:
            out = (
                json.dumps(
                    {
                        "type": "result",
                        "is_error": False,
                        "terminal_reason": "completed",
                        "result": "PONG",
                    }
                )
                + "\n"
            )
            err = ""
            code = 0
        (run_dir_path / "stdout").write_text(out, encoding="utf-8")
        (run_dir_path / "stderr").write_text(err, encoding="utf-8")
        proc = _spawn_real("sleep", "0.01")
        fake = FakeSpawnedChild(proc)
        fake.wait = lambda timeout=None: (proc.wait(timeout=timeout), code)[1]  # type: ignore[assignment]
        return fake

    _wire_l8_and_l3(monkeypatch, spawned_factory)
    sv = _sv(registry, tmp_path)
    picked = _failover_picked(provider)

    run_id = _drive_failover_run(sv, registry, tmp_path, roster, picked)

    row = registry.get_run(run_id)
    assert row is not None
    assert row.phase is RunPhase.FAILED
    assert len(spawned) == 1


def test_non_rotatable_signal_with_wall_after_failover_preserves_signal_warning(
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-rotatable signal death with wall event after failover preserves signal warning."""
    roster, provider = _failover_roster(
        tmp_path, accounts=("acct-a", "acct-b", "acct-c")
    )
    spawned: list[int] = []

    def spawned_factory(*a: object, **k: object) -> FakeSpawnedChild:
        spawned.append(1)
        run_dir_path = a[3]
        assert isinstance(run_dir_path, Path)
        if len(spawned) == 1:
            frames = [
                {
                    "type": "result",
                    "is_error": True,
                    "terminal_reason": "error",
                    "result": "refused",
                    "api_error_status": 402,
                }
            ]
            err = ""
            code = 1
        elif len(spawned) == 2:
            frames = [
                {
                    "type": "rate_limit_event",
                    "rate_limit_info": {
                        "status": "rejected",
                        "rateLimitType": "five_hour",
                    },
                }
            ]
            err = ""
            code = -9
        else:
            frames = []
            err = ""
            code = 0
        out = "".join(json.dumps(f) + "\n" for f in frames)
        (run_dir_path / "stdout").write_text(out, encoding="utf-8")
        (run_dir_path / "stderr").write_text(err, encoding="utf-8")
        proc = _spawn_real("sleep", "0.01")
        fake = FakeSpawnedChild(proc)
        fake.wait = lambda timeout=None: (proc.wait(timeout=timeout), code)[1]  # type: ignore[assignment]
        return fake

    _wire_l8_and_l3(monkeypatch, spawned_factory)
    sv = _sv(registry, tmp_path)
    picked = _failover_picked(provider)

    run_id = _drive_failover_run(sv, registry, tmp_path, roster, picked)

    row = registry.get_run(run_id)
    assert row is not None
    assert row.phase is RunPhase.FAILED
    assert len(spawned) == 2
    assert row.executed_account == "acct-b"
    assert row.warning == (
        "child terminated by signal 9; moved off claude/acct-a (HTTP 402)"
    )
    # The signal overrides the wall's cause, and the reason follows it.
    assert row.blocked_reason == "ClaudeRunFailed (child_signal)"


def test_an_explicitly_named_absent_roster_is_a_typed_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NOTT_NSPAWN_ROSTER naming a missing file refuses; the FILE-declared
    absence stays the empty page (see test_provider_report_answers_an_
    absent_roster_with_an_empty_page). One absence, two meanings: explicit
    is the operator's claim, derived is the host's silence."""
    monkeypatch.delenv("NOTT_NSPAWN_CONFIG", raising=False)
    monkeypatch.setenv("NOTT_NSPAWN_ROSTER", str(tmp_path / "nope.toml"))
    # Under file-wins, isolating only by env lets the fixture be overridden
    # by the operator's ~/.nott/spawn.toml: fixing CONFIG is mandatory.
    monkeypatch.setenv("NOTT_NSPAWN_CONFIG", str(tmp_path / "absent-spawn.toml"))

    with pytest.raises(supervisor.NSpawnError) as refused:
        supervisor.provider_report()
    assert "NOTT_NSPAWN_ROSTER" in str(refused.value)
    assert "nope.toml" in str(refused.value)


# CODEX round-1 blockers on the failover (PR #108): the second attempt must
# actually reach the vendor, and no reader may see a terminal phase for a run
# that is about to run again.


def _pump_like_factory(paths_written: list[Path]) -> object:
    """A spawned child whose pumps create stdout/stderr the real way: O_EXCL.

    The real OutputPump opens the run's stream files with O_CREAT|O_EXCL
    (pump.py `_open_private`). A failover attempt that reuses the same run
    directory therefore only works if the previous attempt's evidence was
    moved aside first.
    """

    class _PumpLikeChild:
        def __init__(self) -> None:
            self._proc = _spawn_real("sleep", "10")
            self._identity = capture_process_identity(self._proc.pid)

        @property
        def identity(self) -> ProcessIdentity:
            return self._identity

        def write_stdin(self, payload: bytes, timeout: float | None) -> None:
            pass

        def close_stdin(self) -> None:
            pass

        def wait(self, timeout: float | None = None) -> int:
            for path in paths_written:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                os.close(fd)
            self._proc.kill()
            self._proc.wait()
            return 0

        def stop_then_kill(self, expected: ProcessIdentity) -> None:
            self._proc.kill()
            self._proc.wait()

    def factory(*a: object, **k: object) -> _PumpLikeChild:
        return _PumpLikeChild()

    return factory


def test_failover_preserves_the_previous_attempt_evidence(
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The second attempt re-opens stdout/stderr with O_EXCL, so the first
    attempt's streams must be archived aside (stdout.attempt-1), never
    overwritten in place and never left to crash the pumps."""
    roster, provider = _failover_roster(tmp_path)
    sv = _sv(registry, tmp_path)
    run_id, thread_id = _admit_pending(sv, registry, tmp_path, worktree=str(tmp_path))
    run_dir = supervisor.RunDirectory(
        root=sv._run_root_path / run_id,
        stdout=sv._run_root_path / run_id / "stdout",
        stderr=sv._run_root_path / run_id / "stderr",
    )
    _wire_l8_and_l3(monkeypatch, _pump_like_factory([run_dir.stdout, run_dir.stderr]))
    verdicts = [_verdict_401(), _verdict_completed()]
    monkeypatch.setattr(
        supervisor.terminal, "classify_terminal_run", lambda *a, **k: verdicts.pop(0)
    )
    picked = _failover_picked(provider)
    try:
        sv._drive_run(
            run_id,
            str(tmp_path),
            run_dir,
            "ping",
            roster,
            picked,
            thread_id,
            False,
            True,
        )
    finally:
        sv.shutdown()

    row = registry.get_run(run_id)
    assert row is not None
    assert row.phase is RunPhase.COMPLETED
    root = sv._run_root_path / run_id
    assert (root / "stdout").exists(), "second attempt wrote its streams"
    assert (root / "stdout.attempt-1").exists(), "first attempt evidence archived"


def test_spawn_and_wait_never_publishes_a_terminal_phase(
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A classified 401 leaves the row RUNNING: only the driver, which knows
    whether another account will be tried, may publish the terminal phase."""
    roster, provider = _failover_roster(tmp_path)
    sv = _sv(registry, tmp_path)
    run_id, thread_id = _admit_pending(sv, registry, tmp_path, worktree=str(tmp_path))
    run_dir = supervisor.RunDirectory(
        root=sv._run_root_path / run_id,
        stdout=sv._run_root_path / run_id / "stdout",
        stderr=sv._run_root_path / run_id / "stderr",
    )
    _wire_l8_and_l3(monkeypatch, _pump_like_factory([run_dir.stdout, run_dir.stderr]))
    monkeypatch.setattr(
        supervisor.terminal, "classify_terminal_run", lambda *a, **k: _verdict_401()
    )
    picked = _failover_picked(provider)
    try:
        sv._spawn_and_wait(
            run_id,
            str(tmp_path),
            run_dir,
            "ping",
            roster,
            picked,
            thread_id,
            False,
            True,
        )
    finally:
        sv.shutdown()

    row = registry.get_run(run_id)
    assert row is not None
    assert row.phase is RunPhase.RUNNING


def test_quota_error_taxonomy_classification(
    registry: Registry, tmp_path: Path
) -> None:
    stdout = tmp_path / "stdout"
    stderr = tmp_path / "stderr"
    stdout.write_bytes(
        b'{"type":"result","is_error":true,"terminal_reason":"error",'
        b'"api_error_status":402,"result":"Payment Required"}\n'
    )
    stderr.write_bytes(b"")
    classification = terminal.classify_terminal_run(
        stdout,
        stderr,
        exit_code=1,
        provider="claude",
        account_alias="default",
        now=1000,
    )
    assert classification.phase is RunPhase.FAILED
    assert classification.warning is not None
    assert (
        "402" in classification.warning
        and "quota" in classification.warning.lower()
        and "ProviderExhausted" in classification.warning
    )
    assert classification.api_error_status == 402


def test_rate_limit_429_taxonomy_classification(
    registry: Registry, tmp_path: Path
) -> None:
    stdout = tmp_path / "stdout"
    stderr = tmp_path / "stderr"
    stdout.write_bytes(
        b'{"type":"result","is_error":true,"terminal_reason":"error",'
        b'"api_error_status":429,"result":"Rate Limit Exceeded"}\n'
    )
    stderr.write_bytes(b"")
    classification = terminal.classify_terminal_run(
        stdout,
        stderr,
        exit_code=1,
        provider="claude",
        account_alias="default",
        now=1000,
    )
    assert classification.phase is RunPhase.FAILED
    assert classification.warning is not None
    assert "429" in classification.warning
    assert classification.api_error_status == 429


def test_a_run_with_a_deadline_ends_at_the_deadline(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_picked: picker.PickerResult,
) -> None:
    """`timeout_ms` on `nspawn_run_start` did not bound the run. It bounds it now.

    MEASURED 2026-08-31, by reading: `_admit` called `_wait_past_spawning`, which
    blocks the CALL until the phase leaves PENDING/SPAWNING and returns — and the
    run itself was `spawned.wait()` with no timeout at all. An operator planned a
    silent run's recovery around «pass `timeout_ms` and it fails loud» and got a
    deadline that did not exist: a mute child stayed mute forever, and nothing
    ever killed it.

    The child here sleeps far past the deadline. What is asserted is the whole
    promise: the run REACHES a terminal phase, the phase is FAILED, the reason
    NAMES the deadline rather than inventing a vendor error, and the child is
    signalled instead of left orphaned behind a terminal row.
    """
    child_double = FakeSpawnedChild(_spawn_real("sleep", "30"))
    _wire_l8_and_l3(monkeypatch, lambda *a, **k: child_double)
    sv = _sv(registry, tmp_path)
    from nspawn.owner_roster import Roster

    roster = Roster.read(Roster.default_path())  # type: ignore[arg-type]
    run_id, thread_id = _admit_pending(sv, registry, tmp_path, worktree=str(tmp_path))
    run_dir = supervisor.RunDirectory(
        root=sv._run_root_path / run_id,
        stdout=sv._run_root_path / run_id / "stdout",
        stderr=sv._run_root_path / run_id / "stderr",
    )

    started = time.monotonic()
    sv._drive_run(
        run_id,
        str(tmp_path),
        run_dir,
        "hi",
        roster,
        fake_picked,
        thread_id,
        False,
        False,
        deadline_ms=500,
    )
    elapsed = time.monotonic() - started

    status = sv.status(run_id)
    assert status.phase is RunPhase.FAILED
    assert status.failure_reason is not None
    assert status.failure_reason == "RunDeadlineExceeded"
    # The child sleeps 30s; the deadline is 0.5s. Anything near 30 means the
    # deadline was decoration.
    assert elapsed < 10, f"the run outlived its deadline by {elapsed:.1f}s"
    assert child_double.stop_calls, "a child past its deadline must be signalled"


def test_no_deadline_is_still_no_deadline(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_picked: picker.PickerResult,
) -> None:
    """The bound is OPT-IN. A caller that names no deadline keeps today's run.

    A deadline nobody asked for is a killer nobody authorized: every existing
    caller passes nothing, and a default would start ending long legitimate runs
    the day this lands.
    """
    child_double = FakeSpawnedChild(_spawn_real("true"))
    _wire_l8_and_l3(monkeypatch, lambda *a, **k: child_double)
    monkeypatch.setattr(
        supervisor.terminal, "classify_terminal_run", _classify_by_exit_code
    )
    sv = _sv(registry, tmp_path)
    from nspawn.owner_roster import Roster

    roster = Roster.read(Roster.default_path())  # type: ignore[arg-type]
    run_id, thread_id = _admit_pending(sv, registry, tmp_path, worktree=str(tmp_path))
    run_dir = supervisor.RunDirectory(
        root=sv._run_root_path / run_id,
        stdout=sv._run_root_path / run_id / "stdout",
        stderr=sv._run_root_path / run_id / "stderr",
    )

    sv._drive_run(
        run_id,
        str(tmp_path),
        run_dir,
        "hi",
        roster,
        fake_picked,
        thread_id,
        False,
        False,
    )

    assert sv.status(run_id).phase is RunPhase.COMPLETED
    assert not child_double.stop_calls


def test_the_deadline_does_not_multiply_across_credential_failover(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_picked: picker.PickerResult,
) -> None:
    """A deadline that grows with the number of accounts is not a deadline.

    `_drive_run` re-spawns the SAME run on the next account when the vendor
    rejects a credential, and each turn of that loop used to receive the FULL
    `deadline_ms` again: three accounts turned a caller's 60 s into ~180 s. The
    code said «O PRAZO E DO RUN» in its own comment while doing it per ATTEMPT —
    found by an independent review of this very candidate.

    Here every attempt burns the whole budget. The assertion is that the SECOND
    attempt is refused outright because remaining time <= 0.
    """
    budget_ms = 100
    attempts: list[int] = []

    def _burn(*_args: object, **kwargs: object) -> terminal.TerminalClassification:
        attempts.append(int(kwargs["deadline_ms"]))
        time.sleep((budget_ms + 20) / 1000)
        return terminal.TerminalClassification(
            phase=RunPhase.FAILED,
            final_text=None,
            warning="auth 401",
            marker=None,
            provider_wall=None,
            account_wall=None,
            api_error_status=401,
        )

    sv = _sv(registry, tmp_path)
    monkeypatch.setattr(Supervisor, "_spawn_and_wait", _burn, raising=True)
    # Every account keeps rejecting, so a run that lost its deadline would fail
    # over forever; the pool runs dry at five, and the count below says why.
    monkeypatch.setattr(
        Supervisor, "_should_fail_over", lambda *a, **k: len(attempts) < 5
    )
    monkeypatch.setattr(
        Supervisor, "_repick_after_credential_rejection", lambda *a, **k: fake_picked
    )
    monkeypatch.setattr(Supervisor, "_wall_rejected_credential", lambda *a, **k: None)
    monkeypatch.setattr(Supervisor, "_archive_attempt_evidence", lambda *a, **k: None)
    from nspawn.owner_roster import Roster

    roster = Roster.read(Roster.default_path())  # type: ignore[arg-type]
    run_id, thread_id = _admit_pending(sv, registry, tmp_path, worktree=str(tmp_path))
    run_dir = supervisor.RunDirectory(
        root=sv._run_root_path / run_id,
        stdout=sv._run_root_path / run_id / "stdout",
        stderr=sv._run_root_path / run_id / "stderr",
    )

    sv._drive_run(
        run_id,
        str(tmp_path),
        run_dir,
        "hi",
        roster,
        fake_picked,
        thread_id,
        False,
        False,
        deadline_ms=budget_ms,
    )

    status = sv.status(run_id)
    assert status.phase is RunPhase.FAILED
    assert status.failure_reason is not None
    assert status.failure_reason == "RunDeadlineExceeded"
    assert len(attempts) == 1, f"the deadline renewed itself: {attempts}"
    assert attempts[0] <= budget_ms


_SESSION_COLLISION_STDERR = (
    b"Error: Session ID 787c75f8-0a0b-4c1d-8e2f-3a4b5c6d7e8f is already in use.\n"
)


def _wire_session_collisions(
    monkeypatch: pytest.MonkeyPatch,
    fake_picked: picker.PickerResult,
    *,
    collisions: int,
) -> tuple[list[object], list[Path]]:
    """First `collisions` spawns die with the vendor's collision line on
    stderr; every spawn after that exits 0. Returns the session ids composed
    and the run dirs spawned into, one entry per spawn."""
    monkeypatch.setattr(
        supervisor.picker, "pick_execution", lambda *a, **k: fake_picked
    )
    session_ids: list[object] = []
    spawns: list[Path] = []

    def _spawn(
        argv: object,
        cwd: object,
        env: object,
        run_root: Path,
        needles: object,
        **spawn_kwargs: object,
    ) -> FakeSpawnedChild:
        assert spawn_kwargs == {"stdin_pipe": True}  # the prompt travels on stdin
        spawns.append(run_root)
        if len(spawns) <= collisions:
            (run_root / "stderr").write_bytes(_SESSION_COLLISION_STDERR)
            return FakeSpawnedChild(_spawn_real("false"))
        return FakeSpawnedChild(_spawn_real("true"))

    _wire_l8_and_l3(monkeypatch, _spawn)

    def _compose(*a: object, **k: object) -> list[str]:
        session_ids.append(k["session_id"])
        return ["claude", "-p"]

    monkeypatch.setattr(supervisor.child, "compose_child_argv", _compose)
    monkeypatch.setattr(
        supervisor.terminal, "classify_terminal_run", _classify_by_exit_code
    )
    return session_ids, spawns


def test_d2_a_session_id_collision_respawns_once_with_a_fresh_id(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_picked: picker.PickerResult,
) -> None:
    session_ids, spawns = _wire_session_collisions(
        monkeypatch, fake_picked, collisions=1
    )
    sv = _sv(registry, tmp_path)
    accepted = sv.execute("hi", str(tmp_path), read_only=True)
    status = sv.status(accepted.run_id, timeout_ms=10_000)

    assert len(spawns) == 2
    assert status.phase is RunPhase.COMPLETED
    assert session_ids[0] != session_ids[1]
    row = registry.get_run(accepted.run_id)
    assert row is not None
    assert row.thread_id == session_ids[1]  # the thread that actually ran
    assert status.warning is not None
    assert "already in use" in status.warning
    # The first attempt's evidence survives beside the second's.
    assert (spawns[0] / "stderr.attempt-1").read_bytes() == _SESSION_COLLISION_STDERR


def test_d2_a_second_collision_fails_the_run_naming_the_collision(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_picked: picker.PickerResult,
) -> None:
    session_ids, spawns = _wire_session_collisions(
        monkeypatch, fake_picked, collisions=2
    )
    sv = _sv(registry, tmp_path)
    accepted = sv.execute("hi", str(tmp_path), read_only=True)
    status = sv.status(accepted.run_id, timeout_ms=10_000)

    assert len(spawns) == 2  # once, never a third
    assert status.phase is RunPhase.FAILED
    assert status.failure_reason is not None
    assert status.failure_reason == "SessionIdCollision"
    assert all(
        str(session_id) not in status.failure_reason for session_id in session_ids
    )


def test_d2_a_resume_is_never_respawned_on_a_collision(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_picked: picker.PickerResult,
) -> None:
    """The resumed id IS the thread: a fresh id would be a different
    conversation wearing the caller's name."""
    session_ids, spawns = _wire_session_collisions(
        monkeypatch, fake_picked, collisions=1
    )
    thread = claude_child.mint_session_id()
    sv = _sv(registry, tmp_path)
    accepted = sv.execute("hi", str(tmp_path), session_id=thread, read_only=True)
    status = sv.status(accepted.run_id, timeout_ms=10_000)

    assert len(spawns) == 1
    assert session_ids == [thread]
    assert status.phase is RunPhase.FAILED


# U-lonely-credential — a rejected credential is walled even when it is the ONLY
# one. Measured 2026-09-19 (run 4a73702c): the organization refused Claude Code with
# 403 `oauth_not_allowed_for_organization`, the secrets folder had a single token,
# and `_should_fail_over` returned False on its last branch (`len(accounts) > 1`).
# That same branch governed the WALL, so the dead alias remained eligible and the
# next dispatch knocked on the same door. The dispatching session stopped to ask
# the owner which account to use — which is exactly the work nSPAWN exists not to return.
#
# Rotating requires a spare; WALLING does not.


def _verdict_403() -> terminal.TerminalClassification:
    return terminal.TerminalClassification(
        phase=RunPhase.FAILED,
        final_text=None,
        warning="terminal result did not complete (api_error)",
        marker=None,
        provider_wall=None,
        account_wall=None,
        api_error_status=403,
    )


def test_a_lone_rejected_credential_is_still_walled(
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """403 on the only account: the run fails, and the alias is walled anyway."""
    roster, provider = _failover_roster(tmp_path, accounts=("acct-a",))
    spawned: list[int] = []

    def spawned_factory(*a: object, **k: object) -> FakeSpawnedChild:
        spawned.append(1)
        return FakeSpawnedChild(_spawn_real("true"))

    _wire_l8_and_l3(monkeypatch, spawned_factory)
    monkeypatch.setattr(
        supervisor.terminal, "classify_terminal_run", lambda *a, **k: _verdict_403()
    )
    sv = _sv(registry, tmp_path)
    picked = _failover_picked(provider)

    run_id = _drive_failover_run(sv, registry, tmp_path, roster, picked)

    row = registry.get_run(run_id)
    assert row is not None
    assert row.phase is RunPhase.FAILED
    # One attempt: with no spare there is nothing to rotate to.
    assert len(spawned) == 1
    now = int(time.time()) + 1
    assert registry.get_account_walls(now).get("acct-a", 0) > now, (
        "a credential the vendor rejected stays eligible, so the next dispatch "
        "repeats it"
    )


def test_a_lone_pinned_rejected_credential_is_still_walled(
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Who CHOSE the credential does not change that the vendor rejected it.

    A caller naming the account never fails over — that choice was explicit —
    but the wall belongs to the credential, not to the caller.
    """
    roster, provider = _failover_roster(tmp_path)
    spawned: list[int] = []

    def spawned_factory(*a: object, **k: object) -> FakeSpawnedChild:
        spawned.append(1)
        return FakeSpawnedChild(_spawn_real("true"))

    _wire_l8_and_l3(monkeypatch, spawned_factory)
    monkeypatch.setattr(
        supervisor.terminal, "classify_terminal_run", lambda *a, **k: _verdict_403()
    )
    sv = _sv(registry, tmp_path)
    picked = _failover_picked(provider, requested="acct-a")

    run_id = _drive_failover_run(sv, registry, tmp_path, roster, picked)

    row = registry.get_run(run_id)
    assert row is not None
    assert row.phase is RunPhase.FAILED
    assert len(spawned) == 1
    now = int(time.time()) + 1
    assert registry.get_account_walls(now).get("acct-a", 0) > now


def test_a_non_rotatable_failure_never_walls_the_credential(
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Valid progress: widening the wall must not wall every kind of failure.

    A 404 is a bad model id: it does not say the account is dead. Without this,
    a guard that walled unconditionally would pass the two tests above while
    taking every account out of service.
    """
    roster, provider = _failover_roster(tmp_path, accounts=("acct-a",))

    def spawned_factory(*a: object, **k: object) -> FakeSpawnedChild:
        return FakeSpawnedChild(_spawn_real("true"))

    _wire_l8_and_l3(monkeypatch, spawned_factory)
    monkeypatch.setattr(
        supervisor.terminal, "classify_terminal_run", lambda *a, **k: _verdict_404()
    )
    sv = _sv(registry, tmp_path)
    picked = _failover_picked(provider)

    _drive_failover_run(sv, registry, tmp_path, roster, picked)

    now = int(time.time()) + 1
    assert "acct-a" not in registry.get_account_walls(now)


@pytest.mark.parametrize("wall_event", [True, False], ids=["with_wall", "plain"])
def test_a_rate_limit_429_moves_to_the_next_account_and_walls_for_minutes(
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    wall_event: bool,
) -> None:
    """nSPAWN is the router: a 429 is the account's refusal.
    The account is walled for minutes, not the hour, and the run goes on."""
    roster, provider = _failover_roster(tmp_path)
    spawned: list[int] = []
    refused = [
        *(
            [
                {
                    "type": "rate_limit_event",
                    "rate_limit_info": {
                        "status": "rejected",
                        "rateLimitType": "five_hour",
                    },
                }
            ]
            if wall_event
            else []
        ),
        {
            "type": "result",
            "is_error": True,
            "terminal_reason": "error",
            "result": "refused",
            "api_error_status": 429,
        },
    ]
    answered = {
        "type": "result",
        "is_error": False,
        "terminal_reason": "completed",
        "result": "PONG",
    }

    def spawned_factory(*a: object, **k: object) -> FakeSpawnedChild:
        spawned.append(1)
        frames, code = (refused, 1) if len(spawned) == 1 else ([answered], 0)
        run_dir_path = a[3]
        assert isinstance(run_dir_path, Path)
        (run_dir_path / "stdout").write_text(
            "".join(json.dumps(f) + "\n" for f in frames), encoding="utf-8"
        )
        (run_dir_path / "stderr").write_text("", encoding="utf-8")
        proc = _spawn_real("sleep", "0.01")
        fake = FakeSpawnedChild(proc)
        fake.wait = lambda timeout=None: (proc.wait(timeout=timeout), code)[1]  # type: ignore[assignment]
        return fake

    _wire_l8_and_l3(monkeypatch, spawned_factory)
    sv = _sv(registry, tmp_path)
    before = int(time.time())

    run_id = _drive_failover_run(
        sv, registry, tmp_path, roster, _failover_picked(provider)
    )

    row = registry.get_run(run_id)
    assert row is not None
    assert (row.phase, row.executed_account) == (RunPhase.COMPLETED, "acct-b")
    assert len(spawned) == 2
    assert "moved off claude/acct-a (" in (row.warning or "")
    walls = registry.get_account_walls(before + 1)
    assert walls["acct-a"] > before
    if not wall_event:
        assert walls["acct-a"] <= int(time.time()) + Supervisor.RATE_LIMIT_WALL_S


# U-substitute — when all accounts of the requested provider are dead, the
# run row names the routes that SERVE at that instant. Without this, the agent
# that dispatched receives raw vendor text and has no way forward; that is what
# happened on 2026-09-19, with the session stopping to ask the owner which
# account to use. The list comes from the roster and live walls — nothing to infer.


def _roster_with_a_substitute(tmp_path: Path) -> tuple[object, object]:
    from nspawn.owner_roster import Defaults, Roster

    secrets = tmp_path / "secrets" / "claude"
    secrets.mkdir(parents=True, exist_ok=True)
    (secrets / "acct-a.token").write_text("x", encoding="utf-8")
    spare_secrets = tmp_path / "secrets" / "spare"
    spare_secrets.mkdir(parents=True, exist_ok=True)
    (spare_secrets / "spare-a.token").write_text("x", encoding="utf-8")
    provider = Provider(name="claude", secrets=secrets, models=["m"], model="m")
    spare = Provider(name="spare", secrets=spare_secrets, models=["m"], model="m")
    roster = Roster(
        providers=[provider, spare], defaults=Defaults(plugin_root=tmp_path)
    )
    return roster, provider


def test_every_account_rejected_names_a_route_that_can_serve(
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The only account is refused: the row names the provider that still serves."""
    roster, provider = _roster_with_a_substitute(tmp_path)

    def spawned_factory(*a: object, **k: object) -> FakeSpawnedChild:
        return FakeSpawnedChild(_spawn_real("true"))

    _wire_l8_and_l3(monkeypatch, spawned_factory)
    monkeypatch.setattr(
        supervisor.terminal, "classify_terminal_run", lambda *a, **k: _verdict_403()
    )
    sv = _sv(registry, tmp_path)
    picked = _failover_picked(provider)

    run_id = _drive_failover_run(sv, registry, tmp_path, roster, picked)

    row = registry.get_run(run_id)
    assert row is not None
    assert row.phase is RunPhase.FAILED
    reason = (row.warning or "") + (row.blocked_reason or "")
    # The dead route and the live one, so the caller can act without reading
    # the roster off the disk.
    assert "acct-a" in reason
    assert "spare" in reason


def test_no_substitute_says_so_instead_of_naming_a_dead_route(
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Valid progress: with nothing else declared, the row must not invent a route.

    Without this, a substitute list that simply echoed every declared provider
    would pass the test above while sending the caller to a route that cannot
    serve either.
    """
    roster, provider = _failover_roster(tmp_path, accounts=("acct-a",))

    def spawned_factory(*a: object, **k: object) -> FakeSpawnedChild:
        return FakeSpawnedChild(_spawn_real("true"))

    _wire_l8_and_l3(monkeypatch, spawned_factory)
    monkeypatch.setattr(
        supervisor.terminal, "classify_terminal_run", lambda *a, **k: _verdict_403()
    )
    sv = _sv(registry, tmp_path)
    picked = _failover_picked(provider)

    run_id = _drive_failover_run(sv, registry, tmp_path, roster, picked)

    row = registry.get_run(run_id)
    assert row is not None
    reason = (row.warning or "") + (row.blocked_reason or "")
    assert "acct-a" in reason
    assert "no other declared route" in reason


def test_a_rejected_credential_is_walled_for_minutes_not_an_hour(
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Measured 2026-09-23: one 401 caused by the CALLER's environment (its
    ANTHROPIC_BASE_URL reached the child) walled a valid account for an hour,
    twice, and auto-compaction fell back to the filter both times. A rejection
    has no vendor reset to wait for: the wall only has to outlast a burst of
    dispatches."""
    roster, provider = _failover_roster(tmp_path, accounts=("acct-a",))
    _wire_l8_and_l3(monkeypatch, lambda *a, **k: FakeSpawnedChild(_spawn_real("true")))
    monkeypatch.setattr(
        supervisor.terminal, "classify_terminal_run", lambda *a, **k: _verdict_403()
    )
    sv = _sv(registry, tmp_path)
    before = int(time.time())
    _drive_failover_run(sv, registry, tmp_path, roster, _failover_picked(provider))
    until = registry.get_account_walls(before).get("acct-a", 0)
    assert before < until <= int(time.time()) + 300


def test_an_exhausted_quota_is_still_walled_for_the_hour(
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The counterpart: a 402 carries no reset time, so the hour stands."""
    roster, provider = _failover_roster(tmp_path, accounts=("acct-a",))
    _wire_l8_and_l3(monkeypatch, lambda *a, **k: FakeSpawnedChild(_spawn_real("true")))
    from dataclasses import replace

    verdict = replace(_verdict_403(), api_error_status=402)
    monkeypatch.setattr(
        supervisor.terminal, "classify_terminal_run", lambda *a, **k: verdict
    )
    sv = _sv(registry, tmp_path)
    before = int(time.time())
    _drive_failover_run(sv, registry, tmp_path, roster, _failover_picked(provider))
    assert registry.get_account_walls(before).get("acct-a", 0) >= before + 3600


def test_a_refused_terminal_keeps_its_reason_through_the_exhaustion_note(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review round 3: the FAILED write was refused and queued for retry; the
    exhaustion note then moved the RUNNING row to FAILED with only a warning,
    and the retry hit the ended-run fence: the classification's own warning was
    lost and no notice went out. A run has one terminal write: it lands once,
    whole, and is announced once; the late note is a diagnostic line."""
    from nspawn.registry import STORE_ERRORS

    roster, provider = _failover_roster(tmp_path)
    _wire_l8_and_l3(monkeypatch, lambda *a, **k: FakeSpawnedChild(_spawn_real("true")))
    from dataclasses import replace

    verdicts = [
        replace(_verdict_401(), tokens=7, failure_reason="ApiError (401)")
        for _ in range(2)
    ]
    monkeypatch.setattr(
        supervisor.terminal, "classify_terminal_run", lambda *a, **k: verdicts.pop(0)
    )
    real_update = registry.update_run
    refusals = [STORE_ERRORS[0]("database is locked")]

    def refused_once(row: object, **kwargs: object) -> None:
        if getattr(row, "phase", None) is RunPhase.FAILED and refusals:
            raise refusals.pop()
        real_update(row, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(registry, "update_run", refused_once)
    sv = _sv(registry, tmp_path)
    announced: list[str] = []
    monkeypatch.setattr(sv, "_announce", announced.append)
    run_id = _drive_failover_run(
        sv, registry, tmp_path, roster, _failover_picked(provider)
    )
    monkeypatch.setattr(registry, "reconnect", lambda: None)
    assert sv.publish_pending() is True
    row = registry.get_run(run_id)
    assert row is not None
    assert row.phase is RunPhase.FAILED
    assert row.blocked_reason == "ApiError (401)"
    assert row.tokens == 7
    assert "terminal result did not complete (api_error)" in (row.warning or "")
    assert announced == [run_id]
    # The exhaustion note arrived while the terminal waited: a diagnostic line.
    assert "account of" not in (row.warning or "")
    logged = (sv._run_root_path / run_id / "late_writes").read_text(encoding="utf-8")
    assert "acct-a" in logged and "acct-b" in logged


def test_exhausted_accounts_keep_the_warning_when_another_route_has_a_typo(
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roster, provider = _failover_roster(tmp_path)
    roster.providers.append(
        Provider(name="other", model="m", excluded_accounts=["other-typo"])
    )
    spawned: list[int] = []

    def spawned_factory(*a: object, **k: object) -> FakeSpawnedChild:
        spawned.append(1)
        return FakeSpawnedChild(_spawn_real("true"))

    _wire_l8_and_l3(monkeypatch, spawned_factory)
    verdicts = [_verdict_402(), _verdict_402()]
    monkeypatch.setattr(
        supervisor.terminal, "classify_terminal_run", lambda *a, **k: verdicts.pop(0)
    )
    sv = _sv(registry, tmp_path)
    picked = _failover_picked(provider)
    run_id = _drive_failover_run(sv, registry, tmp_path, roster, picked)
    row = registry.get_run(run_id)
    assert row is not None and row.phase is RunPhase.FAILED
    assert len(spawned) == 2
    assert row.warning is not None
    assert all(alias in row.warning for alias in ("acct-a", "acct-b", "other-typo"))
