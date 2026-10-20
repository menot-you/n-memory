"""An unpinned review fails over across the owner's preferences.

nSPAWN chooses the reviewer: it takes the declared preference order, never runs
as the producer, and skips a preference that cannot serve now. The skip is
recorded with its reason on the run that served. A route that refuses the turn
at runtime (a refused model, a spent balance, a wall) hands the review to the
next preference as a new run, and the failed run names it.

The children are fakes. The roster, the picker, the walls, the admission and
the registry are the real ones.
"""

from __future__ import annotations

import re
import threading
import time
from pathlib import Path

import pytest
from supervisor_support import (
    _drain_worker_threads,
    _stop_every_reaper,
    _sv,
    registry,
    roster_home,
)

from nspawn import native, picker, supervisor, terminal
from nspawn.registry import Registry
from nspawn.rows import RunPhase
from nspawn.supervisor import InvalidRunRequest, Supervisor

__all__ = ["_drain_worker_threads", "_stop_every_reaper", "registry", "roster_home"]

FENCE = "native installed plugin skills failed their source/owner fence"


def _roster(home: Path, tmp_path: Path, extra: str = "") -> None:
    """codex ranks above claude and is the reviewer role, as on the live roster."""
    homes = {name: tmp_path / name for name in ("codex-main", "oauth", "key")}
    for directory in homes.values():
        directory.mkdir(exist_ok=True)
    (home / ".nott" / "spawn.toml").write_text(
        '[bins]\nclaude = "/usr/bin/true"\ncodex = "/usr/bin/true"\n'
        '[[provider]]\nname = "codex"\nkind = "codex"\nstrength = 9\n'
        'model = "gpt-main"\nmodels = ["gpt-main", "gpt-sol"]\n'
        f'accounts = {{ main = {{ home = "{homes["codex-main"]}" }} }}\n'
        '[[provider]]\nname = "claude"\nkind = "claude"\nstrength = 8\n'
        'model = "m"\nmodels = ["m", "big"]\n'
        f'accounts = {{ oauth = {{ home = "{homes["oauth"]}" }}, '
        f'key = {{ home = "{homes["key"]}" }} }}\n'
        '[roles]\nreviewer = "codex"\n'
        '[role_profile.reviewer]\nmodel = "gpt-sol"\n' + extra,
        encoding="utf-8",
    )


def _fenced(*_: object, **__: object) -> None:
    raise native.NativePreparationError(FENCE)


@pytest.fixture
def driven(monkeypatch: pytest.MonkeyPatch) -> list[terminal.TerminalClassification]:
    """Codex is fenced; each caged Claude review ends with the next verdict queued
    here, COMPLETED when none is."""
    monkeypatch.setattr(native, "prepare_native", _fenced)
    verdicts: list[terminal.TerminalClassification] = []

    def drive(self: Supervisor, row: object, *_: object, **__: object) -> None:
        verdict = verdicts.pop(0) if verdicts else _completed()
        self._publish_terminal(row.run_id, verdict)  # type: ignore[attr-defined]

    def drive_run(self: Supervisor, run_id: str, *_: object, **__: object) -> None:
        verdict = verdicts.pop(0) if verdicts else _completed()
        self._publish_terminal(run_id, verdict)

    monkeypatch.setattr(Supervisor, "_drive_claude_caged", drive)
    monkeypatch.setattr(Supervisor, "_drive_run", drive_run)
    return verdicts


def _completed() -> terminal.TerminalClassification:
    return terminal.TerminalClassification(
        phase=RunPhase.COMPLETED,
        final_text="SHIP",
        warning=None,
        marker=None,
        provider_wall=None,
        account_wall=None,
    )


def _refused(status: int) -> terminal.TerminalClassification:
    """The vendor refused the turn before it served: a model it does not serve
    (400) or a spent balance (402)."""
    return terminal.TerminalClassification(
        phase=RunPhase.FAILED,
        final_text=None,
        warning="terminal result did not complete (api_error)",
        marker=None,
        provider_wall=None,
        account_wall=None,
        api_error_status=status,
        failure_reason=f"VendorApiError (HTTP {status})",
    )


def _broken() -> terminal.TerminalClassification:
    """A failure that says nothing about the route: no status, no wall."""
    return terminal.TerminalClassification(
        phase=RunPhase.FAILED,
        final_text=None,
        warning=None,
        marker=None,
        provider_wall=None,
        account_wall=None,
        failure_reason="ClaudeRunFailed (terminal_incomplete)",
    )


def _ended(sv: Supervisor, run_id: str) -> supervisor.RunStatus:
    status = sv.status(run_id, timeout_ms=10_000)
    assert status.phase.is_terminal(), status
    return status


def _successor(warning: str | None) -> str:
    found = re.search(r"continued as run (\S+) on ", warning or "")
    assert found is not None, warning
    return found[1]


def test_an_unpinned_review_skips_a_fenced_route_and_records_why(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    driven: list[terminal.TerminalClassification],
) -> None:
    _roster(roster_home, tmp_path)
    sv = _sv(registry, tmp_path)
    try:
        accepted = sv.review("feature-x", str(tmp_path))
        status = _ended(sv, accepted.run_id)
    finally:
        sv.shutdown()
    assert status.executed_provider == "claude"
    assert status.phase is RunPhase.COMPLETED
    assert status.warning is not None
    assert f"skipped codex/gpt-sol (NativePreparationError: {FENCE})" in status.warning
    assert f"skipped codex (NativePreparationError: {FENCE})" in status.warning


def test_the_declared_preference_runs_first(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    driven: list[terminal.TerminalClassification],
) -> None:
    "The owner's OAuth account first, above the role and the rank."
    _roster(roster_home, tmp_path, '[preference]\nreview = ["oauth", "key"]\n')
    sv = _sv(registry, tmp_path)
    try:
        status = _ended(sv, sv.review("feature-x", str(tmp_path)).run_id)
    finally:
        sv.shutdown()
    assert (status.executed_provider, status.executed_account) == ("claude", "oauth")
    assert status.model == "m"
    # Nothing was skipped, so nothing is claimed to have been.
    assert "skipped" not in (status.warning or "")


def test_a_review_never_runs_as_its_producer(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    driven: list[terminal.TerminalClassification],
) -> None:
    _roster(roster_home, tmp_path, '[preference]\nreview = ["oauth", "claude"]\n')
    sv = _sv(registry, tmp_path)
    try:
        status = _ended(
            sv, sv.review("feature-x", str(tmp_path), producer=["oauth"]).run_id
        )
    finally:
        sv.shutdown()
    assert status.executed_account == "key"
    assert "skipped oauth (the producer's account)" in (status.warning or "")


def test_the_producers_model_is_skipped_even_as_a_routes_default(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    driven: list[terminal.TerminalClassification],
) -> None:
    _roster(roster_home, tmp_path, '[preference]\nreview = ["oauth", "key/big"]\n')
    sv = _sv(registry, tmp_path)
    try:
        status = _ended(
            sv, sv.review("feature-x", str(tmp_path), producer=["m"]).run_id
        )
    finally:
        sv.shutdown()
    assert (status.executed_account, status.model) == ("key", "big")
    assert "skipped oauth (the producer's model)" in (status.warning or "")


def test_a_review_no_preference_can_serve_is_refused_naming_each_reason(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    driven: list[terminal.TerminalClassification],
) -> None:
    _roster(roster_home, tmp_path)
    sv = _sv(registry, tmp_path)
    try:
        with pytest.raises(picker.PickerError) as refused:
            sv.review("feature-x", str(tmp_path), producer=["claude"])
    finally:
        sv.shutdown()
    message = refused.value.message
    assert "no review preference can serve now" in message
    assert f"codex/gpt-sol (NativePreparationError: {FENCE})" in message
    assert "claude (the producer's route)" in message
    assert registry.list_runs() == ()


@pytest.mark.parametrize("pinned", ["oauth", "claude"])
def test_a_pinned_review_is_refused_when_the_pin_is_the_producer(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    driven: list[terminal.TerminalClassification],
    pinned: str,
) -> None:
    _roster(roster_home, tmp_path)
    sv = _sv(registry, tmp_path)
    try:
        with pytest.raises(InvalidRunRequest, match="producer"):
            sv.review("feature-x", str(tmp_path), provider=pinned, producer=[pinned])
    finally:
        sv.shutdown()
    assert registry.list_runs() == ()


def test_a_pinned_review_keeps_off_the_producers_account(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    driven: list[terminal.TerminalClassification],
) -> None:
    _roster(roster_home, tmp_path)
    sv = _sv(registry, tmp_path)
    try:
        accepted = sv.review(
            "feature-x", str(tmp_path), provider="claude", producer=["oauth"]
        )
        status = _ended(sv, accepted.run_id)
    finally:
        sv.shutdown()
    assert status.executed_account == "key"


@pytest.mark.parametrize("status_code", [400, 402])
def test_a_route_that_refuses_the_turn_hands_the_review_to_the_next_preference(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    driven: list[terminal.TerminalClassification],
    status_code: int,
) -> None:
    """codex-main refuses gpt-6.1-sol with HTTP 400; MiniMax's spent
    balance is a 402. Either way the review goes on, and the record says where."""
    _roster(roster_home, tmp_path, '[preference]\nreview = ["oauth", "key"]\n')
    driven.append(_refused(status_code))
    sv = _sv(registry, tmp_path)
    try:
        first = _ended(sv, sv.review("feature-x", str(tmp_path)).run_id)
        successor = _successor(first.warning)
        second = _ended(sv, successor)
    finally:
        sv.shutdown()
    assert first.phase is RunPhase.FAILED
    assert first.executed_account == "oauth"
    assert second.phase is RunPhase.COMPLETED
    assert second.executed_account == "key"
    assert f"skipped oauth (HTTP {status_code} on run {first.run_id})" in (
        second.warning or ""
    )
    assert len(registry.list_runs()) == 2


#: Frame pairs recorded from Claude Code, ids redacted: an OAuth account the
#: organization refuses (2.1.257) and Z.AI's spent balance (2.1.282).
FIXTURES = Path(__file__).parent / "fixtures"


def _recorded(tmp_path: Path, name: str) -> terminal.TerminalClassification:
    stdout, stderr = tmp_path / f"{name}.stdout", tmp_path / f"{name}.stderr"
    stdout.write_bytes((FIXTURES / name).read_bytes())
    stderr.write_text("", encoding="utf-8")
    return terminal.classify_terminal_run(
        stdout, stderr, 1, provider="claude", account_alias="oauth", now=1_000_000
    )


@pytest.mark.parametrize(
    ("fixture", "reason"),
    [
        ("claude-oauth-org-403-2.1.257.ndjson", "HTTP 403 oauth_org_not_allowed"),
        ("claude-zai-1113-2.1.282.ndjson", "HTTP 429 code 1113"),
    ],
)
def test_a_recorded_refusal_hands_the_review_on_and_names_the_vendors_reason(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    driven: list[terminal.TerminalClassification],
    fixture: str,
    reason: str,
) -> None:
    """The status alone does not say why: the record keeps the vendor's own
    reason, the error the CLI names or the code its rejection line carries."""
    _roster(roster_home, tmp_path, '[preference]\nreview = ["oauth", "key"]\n')
    driven.append(_recorded(tmp_path, fixture))
    sv = _sv(registry, tmp_path)
    try:
        first = _ended(sv, sv.review("feature-x", str(tmp_path)).run_id)
        second = _ended(sv, _successor(first.warning))
    finally:
        sv.shutdown()
    assert second.phase is RunPhase.COMPLETED
    assert second.executed_account == "key"
    assert f"skipped oauth ({reason} on run {first.run_id})" in (second.warning or "")


def test_an_account_skipped_for_its_wall_serves_again_when_the_wall_lifts(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    driven: list[terminal.TerminalClassification],
) -> None:
    """No account is banned: out of quota it is skipped, and the
    first review after its quota returns runs on it again."""
    _roster(roster_home, tmp_path, '[preference]\nreview = ["oauth", "key"]\n')
    now = int(time.time())
    lifts = now + 4
    registry.record_account_wall("oauth", lifts, "vendor_reset", "seven_day", now)
    sv = _sv(registry, tmp_path)
    try:
        walled = _ended(sv, sv.review("feature-x", str(tmp_path)).run_id)
        # The quota returns: the wall's deadline passes, and nothing else changes.
        while time.time() <= lifts + 1:
            time.sleep(0.2)
        returned = _ended(sv, sv.review("feature-y", str(tmp_path)).run_id)
    finally:
        sv.shutdown()
    assert walled.executed_account == "key"
    assert "skipped oauth (" in (walled.warning or "")
    assert returned.executed_account == "oauth"
    assert "skipped" not in (returned.warning or "")


def test_a_route_refused_at_runtime_is_not_tried_again_in_the_same_review(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    driven: list[terminal.TerminalClassification],
) -> None:
    """After both accounts refused, the `claude` rank entry has no account left;
    the fenced codex entries are skipped; the last run names every reason."""
    _roster(roster_home, tmp_path, '[preference]\nreview = ["oauth", "key"]\n')
    driven.extend([_refused(400), _refused(400)])
    sv = _sv(registry, tmp_path)
    try:
        first = _ended(sv, sv.review("feature-x", str(tmp_path)).run_id)
        second = _ended(sv, _successor(first.warning))
    finally:
        sv.shutdown()
    assert second.phase is RunPhase.FAILED
    warning = second.warning or ""
    assert "no further review preference can serve now" in warning
    assert f"codex/gpt-sol (NativePreparationError: {FENCE})" in warning
    assert "claude (AllProvidersWalled" in warning
    assert len(registry.list_runs()) == 2


@pytest.mark.parametrize("verdict", [_broken, _completed], ids=["broken", "completed"])
def test_only_a_route_refusal_fails_over(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    driven: list[terminal.TerminalClassification],
    verdict: object,
) -> None:
    _roster(roster_home, tmp_path, '[preference]\nreview = ["oauth", "key"]\n')
    driven.append(verdict())  # type: ignore[operator]
    sv = _sv(registry, tmp_path)
    try:
        status = _ended(sv, sv.review("feature-x", str(tmp_path)).run_id)
    finally:
        sv.shutdown()
    assert "continued as run" not in (status.warning or "")
    assert len(registry.list_runs()) == 1


def test_a_pinned_review_never_fails_over_at_runtime(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    driven: list[terminal.TerminalClassification],
) -> None:
    """The caller chose the route: its refusal is the caller's answer."""
    _roster(roster_home, tmp_path)
    driven.append(_refused(400))
    sv = _sv(registry, tmp_path)
    try:
        status = _ended(
            sv, sv.review("feature-x", str(tmp_path), provider="oauth").run_id
        )
    finally:
        sv.shutdown()
    assert status.phase is RunPhase.FAILED
    assert len(registry.list_runs()) == 1


def test_the_successor_is_admitted_for_the_dispatching_call(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    driven: list[terminal.TerminalClassification],
) -> None:
    """The driver's call follows the review to its successor: the close watcher
    serves the live run and the driver joins its worker before it exits."""
    _roster(roster_home, tmp_path, '[preference]\nreview = ["oauth", "key"]\n')
    driven.append(_refused(400))
    call = supervisor.DriveCall(launch_uuid="launch-1", group="ruler")
    token = supervisor.DRIVE_CALL.set(call)
    sv = _sv(registry, tmp_path)
    try:
        first = sv.review("feature-x", str(tmp_path)).run_id
        supervisor.join_workers(call)
        second = _successor(_ended(sv, first).warning)
    finally:
        supervisor.DRIVE_CALL.reset(token)
        sv.shutdown()
    assert call.run_id == second
    assert call.worker is not None and not call.worker.is_alive()
    row = registry.get_run(second)
    assert row is not None and row.group_name == "ruler"


def test_join_workers_follows_a_worker_that_hands_over() -> None:
    call = supervisor.DriveCall(launch_uuid="launch-2")
    release = threading.Event()
    later = threading.Thread(target=release.wait, args=(10,))

    def first() -> None:
        later.start()
        call.worker = later

    call.worker = threading.Thread(target=first)
    call.worker.start()
    threading.Timer(0.2, release.set).start()
    supervisor.join_workers(call)
    assert not later.is_alive()


# -- a verification run: the eval judge's verb ---------------------------------


def test_a_verification_that_names_its_producer_takes_the_verification_order(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    driven: list[terminal.TerminalClassification],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A judge is a witness by declaration: no classifier lowers its class past
    the owner's order, and it never runs as the evaluated model."""

    def asked(*_: object, **__: object) -> None:
        raise AssertionError("a witness that names its producer keeps its class")

    monkeypatch.setattr(supervisor.classify, "resolve_execution_class", asked)
    _roster(roster_home, tmp_path, '[preference]\nverification = ["oauth", "key"]\n')
    driven.append(_refused(402))
    sv = _sv(registry, tmp_path)
    try:
        first = _ended(
            sv,
            sv.execute(
                "judge this",
                str(tmp_path),
                execution_class="verification",
                read_only=True,
                timeout_ms=60_000,
                producer=["MiniMax-M3"],
            ).run_id,
        )
        second = _ended(sv, _successor(first.warning))
    finally:
        sv.shutdown()
    assert (first.executed_account, second.executed_account) == ("oauth", "key")
    assert second.phase is RunPhase.COMPLETED
    assert first.requested_provider is None and second.requested_provider is None
    assert "verification preference: skipped oauth (HTTP 402" in (second.warning or "")


def test_a_resumed_or_writing_turn_takes_no_producer(
    registry: Registry, tmp_path: Path, roster_home: Path
) -> None:
    _roster(roster_home, tmp_path)
    sv = _sv(registry, tmp_path)
    try:
        with pytest.raises(InvalidRunRequest, match="producer applies to a fresh"):
            sv.execute(
                "p",
                str(tmp_path),
                session_id="thread-1",
                read_only=True,
                producer=["claude"],
            )
    finally:
        sv.shutdown()
    assert registry.list_runs() == ()


# -- round 1 findings of the silent-failure axis (b2382cc1) ---------------------


def test_the_producers_model_is_skipped_by_its_family_alias(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    driven: list[terminal.TerminalClassification],
) -> None:
    """A Claude route that lists family names runs `opus` for `claude-opus-5-5`:
    the evaluated model must not judge itself through the alias."""
    homes = {name: tmp_path / name for name in ("oauth", "key")}
    for directory in homes.values():
        directory.mkdir(exist_ok=True)
    (roster_home / ".nott" / "spawn.toml").write_text(
        '[bins]\nclaude = "/usr/bin/true"\n'
        '[[provider]]\nname = "claude"\nkind = "claude"\nstrength = 8\n'
        'model = "opus"\nmodels = ["opus", "haiku"]\n'
        f'accounts = {{ oauth = {{ home = "{homes["oauth"]}" }}, '
        f'key = {{ home = "{homes["key"]}" }} }}\n'
        '[preference]\nreview = ["oauth", "key/haiku"]\n',
        encoding="utf-8",
    )
    sv = _sv(registry, tmp_path)
    try:
        status = _ended(
            sv,
            sv.review("feature-x", str(tmp_path), producer=["claude-opus-5-5"]).run_id,
        )
    finally:
        sv.shutdown()
    assert (status.executed_account, status.model) == ("key", "haiku")
    assert "skipped oauth (the producer's model)" in (status.warning or "")


def test_a_store_fault_at_failover_keeps_the_refusal_and_the_failover(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    driven: list[terminal.TerminalClassification],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fault is queued with the real terminal data, and the retry of that
    write hands the work on, as a write the store refused always was."""
    _roster(roster_home, tmp_path, '[preference]\nreview = ["oauth", "key"]\n')
    driven.append(_refused(400))
    armed = threading.Event()
    real_refusal = supervisor._route_refusal
    calls: list[str | None] = []

    def refusal(classification: terminal.TerminalClassification) -> str | None:
        # One fault, at the first terminal write only: its retry meets a sound store.
        if not calls:
            armed.set()
        calls.append(reason := real_refusal(classification))
        return reason

    real_get = Registry.get_run

    def get_run(self: Registry, run_id: str) -> object:
        if armed.is_set():
            armed.clear()
            raise supervisor.STORE_ERRORS[0]("database is locked")
        return real_get(self, run_id)

    monkeypatch.setattr(supervisor, "_route_refusal", refusal)
    monkeypatch.setattr(Registry, "get_run", get_run)
    sv = _sv(registry, tmp_path)
    try:
        first = sv.review("feature-x", str(tmp_path)).run_id
        deadline = time.monotonic() + 10
        while first not in sv._unpublished and time.monotonic() < deadline:
            time.sleep(0.05)
        assert first in sv._unpublished
        assert sv.publish_pending()
        failed = _ended(sv, first)
        second = _ended(sv, _successor(failed.warning))
    finally:
        sv.shutdown()
    assert failed.phase is RunPhase.FAILED
    assert failed.failure_reason == "VendorApiError (HTTP 400)"
    assert second.phase is RunPhase.COMPLETED
    assert second.executed_account == "key"


def test_a_store_fault_at_a_preparation_refusal_keeps_the_refusal_and_the_failover(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    driven: list[terminal.TerminalClassification],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The second terminal seam (`_fail_run`): a route that cannot prepare the
    turn after admission, and one store fault as it fails over. The real
    refusal is queued, and its retry hands the work on."""
    _roster(roster_home, tmp_path, '[preference]\nreview = ["oauth", "key"]\n')
    armed: list[int] = []
    real_drive = Supervisor._drive_claude_caged

    def drive(self: Supervisor, row: object, *args: object, **kwargs: object) -> None:
        if row.executed_account != "oauth":  # type: ignore[attr-defined]
            real_drive(self, row, *args, **kwargs)  # type: ignore[arg-type]
            return
        armed.append(threading.get_ident())
        self._fail_run(row.run_id, native.NativePreparationError(FENCE))  # type: ignore[attr-defined]

    real_get = Registry.get_run

    def get_run(self: Registry, run_id: str) -> object:
        # Only the refused run's own worker: a reaper tick reads rows too.
        if threading.get_ident() in armed:
            armed.clear()
            raise supervisor.STORE_ERRORS[0]("database is locked")
        return real_get(self, run_id)

    monkeypatch.setattr(Supervisor, "_drive_claude_caged", drive)
    monkeypatch.setattr(Registry, "get_run", get_run)
    sv = _sv(registry, tmp_path)
    try:
        first = sv.review("feature-x", str(tmp_path)).run_id
        deadline = time.monotonic() + 10
        while first not in sv._unpublished and time.monotonic() < deadline:
            time.sleep(0.05)
        assert first in sv._unpublished
        assert sv.publish_pending()
        failed = _ended(sv, first)
        assert (failed.failure_reason or "").startswith("NativePreparationError")
        second = _ended(sv, _successor(failed.warning))
    finally:
        sv.shutdown()
    assert failed.phase is RunPhase.FAILED
    assert second.phase is RunPhase.COMPLETED
    assert second.executed_account == "key"


def test_a_store_fault_at_the_failed_write_of_a_preparation_refusal_names_the_successor(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    driven: list[terminal.TerminalClassification],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The failover already admitted the successor when the store refuses the
    refused run's FAILED write. That write waits for its retry with the note,
    so the failed run still names the refusal and its successor."""
    _roster(roster_home, tmp_path, '[preference]\nreview = ["oauth", "key"]\n')
    armed: list[int] = []
    real_drive = Supervisor._drive_claude_caged

    def drive(self: Supervisor, row: object, *args: object, **kwargs: object) -> None:
        if row.executed_account != "oauth":  # type: ignore[attr-defined]
            real_drive(self, row, *args, **kwargs)  # type: ignore[arg-type]
            return
        armed.append(threading.get_ident())
        self._fail_run(row.run_id, native.NativePreparationError(FENCE))  # type: ignore[attr-defined]

    real_update = Registry.update_run

    def update_run(self: Registry, row: object, **kwargs: object) -> None:
        if threading.get_ident() in armed and row.phase is RunPhase.FAILED:  # type: ignore[attr-defined]
            armed.clear()
            raise supervisor.STORE_ERRORS[0]("database is locked")
        real_update(self, row, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Supervisor, "_drive_claude_caged", drive)
    monkeypatch.setattr(Registry, "update_run", update_run)
    sv = _sv(registry, tmp_path)
    try:
        first = sv.review("feature-x", str(tmp_path)).run_id
        deadline = time.monotonic() + 10
        while first not in sv._unpublished and time.monotonic() < deadline:
            time.sleep(0.05)
        assert first in sv._unpublished
        assert sv.publish_pending()
        failed = _ended(sv, first)
        assert (failed.failure_reason or "").startswith("NativePreparationError")
        second = _ended(sv, _successor(failed.warning))
    finally:
        sv.shutdown()
    assert second.phase is RunPhase.COMPLETED
    assert second.executed_account == "key"


def test_a_store_fault_at_a_pinned_preparation_refusal_still_ends_the_run(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    driven: list[terminal.TerminalClassification],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run with no failover is never queued: no retry may come (a route ping
    on the MCP server's own supervisor), so the run ends FAILED at once."""
    _roster(roster_home, tmp_path)
    armed: list[int] = []

    def drive(self: Supervisor, row: object, *_: object, **__: object) -> None:
        armed.append(threading.get_ident())
        self._fail_run(row.run_id, native.NativePreparationError(FENCE))  # type: ignore[attr-defined]

    real_update = Registry.update_run

    def update_run(self: Registry, row: object, **kwargs: object) -> None:
        if threading.get_ident() in armed and row.phase is RunPhase.FAILED:  # type: ignore[attr-defined]
            armed.clear()
            raise supervisor.STORE_ERRORS[0]("database is locked")
        real_update(self, row, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Supervisor, "_drive_claude_caged", drive)
    monkeypatch.setattr(Registry, "update_run", update_run)
    sv = _sv(registry, tmp_path)
    try:
        run_id = sv.review("feature-x", str(tmp_path), provider="oauth").run_id
        status = _ended(sv, run_id)
        assert run_id not in sv._unpublished
    finally:
        sv.shutdown()
    assert status.phase is RunPhase.FAILED


def test_driver_exit_leaves_a_queued_terminal_to_its_retry(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    driven: list[terminal.TerminalClassification],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`fail_unended` never writes DriverExit over a run whose real terminal is
    queued: the queued refusal still hands the work on."""
    _roster(roster_home, tmp_path, '[preference]\nreview = ["oauth", "key"]\n')
    driven.append(_refused(400))
    armed = threading.Event()
    real_refusal = supervisor._route_refusal
    calls: list[str | None] = []

    def refusal(classification: terminal.TerminalClassification) -> str | None:
        if not calls:
            armed.set()
        calls.append(reason := real_refusal(classification))
        return reason

    real_get = Registry.get_run

    def get_run(self: Registry, run_id: str) -> object:
        if armed.is_set():
            armed.clear()
            raise supervisor.STORE_ERRORS[0]("database is locked")
        return real_get(self, run_id)

    monkeypatch.setattr(supervisor, "_route_refusal", refusal)
    monkeypatch.setattr(Registry, "get_run", get_run)
    sv = _sv(registry, tmp_path)
    try:
        first = sv.review("feature-x", str(tmp_path)).run_id
        deadline = time.monotonic() + 10
        while first not in sv._unpublished and time.monotonic() < deadline:
            time.sleep(0.05)
        assert first in sv._unpublished
        sv.fail_unended(first)
        assert sv.publish_pending()
        failed = _ended(sv, first)
        second = _ended(sv, _successor(failed.warning))
    finally:
        sv.shutdown()
    assert failed.failure_reason == "VendorApiError (HTTP 400)"
    assert second.executed_account == "key"


@pytest.mark.parametrize("execution_class", [None, "judgment"])
def test_a_producer_is_refused_outside_a_review_or_a_verification(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    execution_class: str | None,
) -> None:
    """Only a witness class excludes the producer's route: any other class would
    run as the producer while the schema says it never does."""
    _roster(roster_home, tmp_path)
    sv = _sv(registry, tmp_path)
    try:
        with pytest.raises(InvalidRunRequest, match="review or verification"):
            sv.execute(
                "p",
                str(tmp_path),
                execution_class=execution_class,
                read_only=True,
                timeout_ms=60_000,
                producer=["claude"],
            )
    finally:
        sv.shutdown()
    assert registry.list_runs() == ()


def test_a_spent_balance_keeps_its_code_beside_the_provider_wall() -> None:
    """On a zai route that declares `1113` the run is walled too: the record
    still names the code the README promises."""
    line = (
        "API Error: Request rejected (429) · [1113][Insufficient balance or no "
        "resource package. Please recharge.][REDACTED_REQUEST_ID]"
    )
    walled = terminal.TerminalClassification(
        phase=RunPhase.FAILED,
        final_text=line,
        warning=None,
        marker=None,
        provider_wall=terminal.ProviderWallRecord(
            provider="zai",
            cooling_until=2_000,
            source=terminal.WallSource.DEFAULT_1H,
            observed_at=1_000,
        ),
        account_wall=None,
        api_error_status=429,
    )
    assert supervisor._route_refusal(walled) == "provider walled, HTTP 429 code 1113"
