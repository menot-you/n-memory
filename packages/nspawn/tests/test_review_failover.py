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

import ast
import contextlib
import itertools
import re
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from supervisor_support import (
    _drain_worker_threads,
    _stop_every_reaper,
    _sv,
    registry,
    roster_home,
)

from nspawn import driver, native, picker, supervisor, terminal
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
    """Codex is fenced; each Claude review ends with the next verdict queued
    here, COMPLETED when none is."""
    monkeypatch.setattr(native, "prepare_native", _fenced)
    verdicts: list[terminal.TerminalClassification] = []

    def drive_run(self: Supervisor, run_id: str, *_: object, **__: object) -> None:
        verdict = verdicts.pop(0) if verdicts else _completed()
        self._publish_terminal(run_id, verdict)

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


def test_a_review_no_route_can_serve_names_when_each_walled_route_serves_again(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    driven: list[terminal.TerminalClassification],
) -> None:
    """A caller that waits names every route it waits on, each with its own reset."""
    _roster(roster_home, tmp_path)
    now = int(time.time())
    lifts = now + 7200
    for alias in ("oauth", "key"):
        registry.record_account_wall(alias, lifts, "vendor_reset", "seven_day", now)
    sv = _sv(registry, tmp_path)
    try:
        with pytest.raises(picker.AllProvidersWalled) as refused:
            sv.review("feature-x", str(tmp_path))
    finally:
        sv.shutdown()
    at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(lifts))
    message = refused.value.message
    assert f"codex/gpt-sol (NativePreparationError: {FENCE})" in message
    assert re.search(rf"claude \(AllProvidersWalled: [^)]*; resets {at}\)", message)
    assert refused.value.nearest_reset == lifts


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
    assert len(call.workers) == 2
    assert not any(worker.is_alive() for worker in call.workers)
    row = registry.get_run(second)
    assert row is not None and row.group_name == "ruler"


def test_join_workers_follows_a_worker_that_hands_over() -> None:
    call = supervisor.DriveCall(launch_uuid="launch-2")
    release = threading.Event()
    later = threading.Thread(target=release.wait, args=(10,))

    def first() -> None:
        supervisor.start_worker(call, later)

    supervisor.start_worker(call, threading.Thread(target=first))
    threading.Timer(0.2, release.set).start()
    supervisor.join_workers(call)
    assert not later.is_alive()


# -- each worker of a call reaches the driver's join -----------------------------

Publish = Callable[[terminal.TerminalClassification], None]
Step = Callable[[Publish], None]


def _stepped(monkeypatch: pytest.MonkeyPatch, steps: list[Step]) -> None:
    """Codex is fenced, as in `driven`. Each run takes the next step in drive
    order. The step publishes the terminal of its run through its argument."""
    monkeypatch.setattr(native, "prepare_native", _fenced)

    def drive_run(self: Supervisor, run_id: str, *_: object, **__: object) -> None:
        steps.pop(0)(lambda verdict: self._publish_terminal(run_id, verdict))

    monkeypatch.setattr(Supervisor, "_drive_run", drive_run)


def _now(verdict: terminal.TerminalClassification) -> Step:
    return lambda publish: publish(verdict)


def _after(
    event: threading.Event, timeout: float, verdict: terminal.TerminalClassification
) -> Step:
    def step(publish: Publish) -> None:
        event.wait(timeout)
        publish(verdict)

    return step


@contextlib.contextmanager
def _worker_starts(
    monkeypatch: pytest.MonkeyPatch,
    *,
    hold_first: bool = False,
    fail_second: bool = False,
) -> Iterator[list[threading.Thread]]:
    """Replace `threading.Thread` with a subclass. It records, in start order,
    each started thread whose target is `_guarded_worker`.

    `hold_first`: the start of the first such thread returns only after that
    worker ended, so the admitting thread stops between `start()` and its next
    statement. `fail_second`: the start of the second such thread raises, as a
    spent thread budget does, and starts nothing.

    `super().start()` is the tracking start of `_drain_worker_threads`, so that
    fixture still joins each thread. The `Thread` name comes back when this
    block ends, before that fixture restores `Thread.start`.
    """
    workers: list[threading.Thread] = []
    starts = itertools.count()

    class Hooked(threading.Thread):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            target = kwargs.get("target")
            self.hooked = getattr(target, "__name__", None) == "_guarded_worker"

        def start(self) -> None:
            if not self.hooked:
                super().start()
                return
            index = next(starts)
            if fail_second and index == 1:
                raise RuntimeError("can't start new thread")
            workers.append(self)
            super().start()
            if hold_first and index == 0:
                self.join(timeout=10)

    with monkeypatch.context() as patch:
        patch.setattr(threading, "Thread", Hooked)
        try:
            yield workers
        finally:
            for worker in workers:
                worker.join(timeout=10)


def _review_frame(tmp_path: Path) -> dict[str, Any]:
    return {
        "tool": "nspawn_review_start",
        "arguments": {"branch": "feature-x", "cwd": str(tmp_path)},
        "launch_uuid": str(uuid.uuid4()),
        "caller": None,
        "project": None,
        "drivers_dir": None,
    }


def test_join_workers_joins_a_successor_admitted_before_the_first_admission_returns(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first worker refuses and admits its successor while the admitting
    thread is between `start()` and its next statement. The join still waits
    for the successor."""
    _roster(roster_home, tmp_path, '[preference]\nreview = ["oauth", "key"]\n')
    gate = threading.Event()
    _stepped(monkeypatch, [_now(_refused(400)), _after(gate, 2, _completed())])
    call = supervisor.DriveCall(launch_uuid="launch-hold", group="ruler")
    sv = _sv(registry, tmp_path)
    with _worker_starts(monkeypatch, hold_first=True) as workers:
        token = supervisor.DRIVE_CALL.set(call)
        try:
            first = sv.review("feature-x", str(tmp_path)).run_id
            supervisor.join_workers(call)
            successor_alive = workers[1].is_alive()
            gate.set()
            second = _successor(_ended(sv, first).warning)
            ended = _ended(sv, second)
        finally:
            gate.set()
            supervisor.DRIVE_CALL.reset(token)
            sv.shutdown()
    assert not successor_alive
    assert ended.phase is RunPhase.COMPLETED
    assert call.run_id == second
    # The held admission adds the first worker after its successor.
    assert len(call.workers) == 2 and set(call.workers) == set(workers)


def test_serve_shuts_down_after_a_successor_admitted_before_the_first_admission_returns(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The consumer boundary: `serve` ends the successor FAILED and shuts down
    while the successor runs, unless its join waits for that worker."""
    _roster(roster_home, tmp_path, '[preference]\nreview = ["oauth", "key"]\n')
    stopping = threading.Event()
    _stepped(monkeypatch, [_now(_refused(400)), _after(stopping, 2, _completed())])
    acks: list[dict[str, Any]] = []
    seen: list[tuple[bool, list[RunPhase], list[str | None]]] = []
    real_shutdown = Supervisor.shutdown

    def shutdown(self: Supervisor) -> None:
        later = [row for row in registry.list_runs() if row.run_id != acks[0]["run_id"]]
        seen.append(
            (
                workers[1].is_alive(),
                [row.phase for row in later],
                [row.blocked_reason for row in later],
            )
        )
        stopping.set()
        real_shutdown(self)

    monkeypatch.setattr(Supervisor, "shutdown", shutdown)
    sv = _sv(registry, tmp_path)
    with _worker_starts(monkeypatch, hold_first=True) as workers:
        try:
            driver.serve(
                _review_frame(tmp_path),
                acks.append,
                supervisor_factory=lambda: sv,
                owns_supervisor=True,
            )
        finally:
            stopping.set()
    first = registry.get_run(acks[0]["run_id"])
    assert first is not None
    _successor(first.warning)
    assert seen == [(False, [RunPhase.COMPLETED], [None])]


def test_join_workers_joins_each_worker_of_a_failover_chain(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Worker 2 hands the call to worker 3 while worker 1 still runs. The join
    waits for worker 2 too, though the last worker ended first."""
    homes = {name: tmp_path / name for name in ("oauth", "key", "spare")}
    for directory in homes.values():
        directory.mkdir()
    accounts = ", ".join(
        f'{name} = {{ home = "{home}" }}' for name, home in homes.items()
    )
    (roster_home / ".nott" / "spawn.toml").write_text(
        '[bins]\nclaude = "/usr/bin/true"\n'
        '[[provider]]\nname = "claude"\nkind = "claude"\nstrength = 8\n'
        'model = "m"\nmodels = ["m", "big"]\n'
        f"accounts = {{ {accounts} }}\n"
        '[preference]\nreview = ["oauth", "key", "spare"]\n',
        encoding="utf-8",
    )
    admitted, b_admitted, b_tail, release = (threading.Event() for _ in range(4))
    chain: list[threading.Thread] = []

    def first_step(publish: Publish) -> None:
        chain.append(threading.current_thread())
        assert admitted.wait(5)
        publish(_refused(400))
        b_admitted.set()
        assert b_tail.wait(5)

    def second_step(publish: Publish) -> None:
        chain.append(threading.current_thread())
        assert b_admitted.wait(5)
        publish(_refused(400))
        b_tail.set()
        release.wait(2)

    def third_step(publish: Publish) -> None:
        chain.append(threading.current_thread())
        publish(_completed())

    _stepped(monkeypatch, [first_step, second_step, third_step])
    call = supervisor.DriveCall(launch_uuid="launch-chain")
    token = supervisor.DRIVE_CALL.set(call)
    sv = _sv(registry, tmp_path)
    try:
        first = sv.review("feature-x", str(tmp_path)).run_id
        admitted.set()
        supervisor.join_workers(call)
        second_alive = chain[1].is_alive()
        release.set()
        for worker in chain:
            worker.join(timeout=10)
        second = _successor(_ended(sv, first).warning)
        third = _successor(_ended(sv, second).warning)
        ended = _ended(sv, third)
    finally:
        admitted.set()
        release.set()
        supervisor.DRIVE_CALL.reset(token)
        sv.shutdown()
    assert not second_alive
    assert [worker.is_alive() for worker in chain] == [False, False, False]
    assert ended.phase is RunPhase.COMPLETED
    for run_id in (first, second):
        row = registry.get_run(run_id)
        assert row is not None and row.phase is RunPhase.FAILED


def test_serve_ends_a_successor_whose_worker_does_not_start(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A guard: a worker whose start raises is never joined, so `serve` still
    ends the admitted successor and shuts down."""
    _roster(roster_home, tmp_path, '[preference]\nreview = ["oauth", "key"]\n')
    _stepped(monkeypatch, [_now(_refused(400))])
    shutdowns: list[bool] = []
    real_shutdown = Supervisor.shutdown

    def shutdown(self: Supervisor) -> None:
        shutdowns.append(True)
        real_shutdown(self)

    monkeypatch.setattr(Supervisor, "shutdown", shutdown)
    sv = _sv(registry, tmp_path)
    acks: list[dict[str, Any]] = []
    with _worker_starts(monkeypatch, fail_second=True):
        ack = driver.serve(
            _review_frame(tmp_path),
            acks.append,
            supervisor_factory=lambda: sv,
            owns_supervisor=True,
        )
    assert acks == [ack]
    assert shutdowns == [True]
    first = registry.get_run(ack["run_id"])
    assert first is not None
    assert (first.phase, first.blocked_reason) == (
        RunPhase.FAILED,
        "WorkerDied: RuntimeError",
    )
    later = [row for row in registry.list_runs() if row.run_id != ack["run_id"]]
    assert [(row.phase, row.blocked_reason) for row in later] == [
        (RunPhase.FAILED, "DriverExit: the worker ended with no terminal write")
    ]


def _is_call_attribute(node: ast.AST, attr: str) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == attr
        and isinstance(node.value, ast.Name)
        and node.value.id == "call"
    )


def _targets(node: ast.AST) -> Iterator[ast.AST]:
    if isinstance(node, (ast.Tuple, ast.List)):
        for element in node.elts:
            yield from _targets(element)
    elif isinstance(node, ast.Starred):
        yield from _targets(node.value)
    else:
        yield node


def _owners(tree: ast.AST) -> dict[ast.AST, str | None]:
    """Each node mapped to the name of its innermost enclosing function."""
    owners: dict[ast.AST, str | None] = {}

    def visit(node: ast.AST, owner: str | None) -> None:
        owners[node] = owner
        inner = (
            node.name
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            else owner
        )
        for child in ast.iter_child_nodes(node):
            visit(child, inner)

    visit(tree, None)
    return owners


def test_each_worker_reaches_its_call_only_through_start_worker() -> None:
    """A static guard: the only write to the workers of a call is the append in
    `start_worker`, after the start of the worker."""
    package = Path(supervisor.__file__).parent
    slot_writes: list[str] = []
    appends: list[str] = []
    starts: dict[str, list[tuple[int, int]]] = {}
    callers: dict[str, int] = {}
    worker_starts: dict[str, int] = {}
    for path in sorted(package.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        owners = _owners(tree)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                written = (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
                for target in written:
                    for leaf in _targets(target):
                        if _is_call_attribute(leaf, "worker"):
                            slot_writes.append(f"{path.name}:{node.lineno}")
            if not isinstance(node, ast.Call):
                continue
            owner = owners[node] or ""
            func = node.func
            if (isinstance(func, ast.Name) and func.id == "start_worker") or (
                isinstance(func, ast.Attribute) and func.attr == "start_worker"
            ):
                callers[owner] = callers.get(owner, 0) + 1
            if not isinstance(func, ast.Attribute):
                continue
            if func.attr == "append" and _is_call_attribute(func.value, "workers"):
                appends.append(f"{path.name}:{owner}:{node.lineno}:{node.col_offset}")
            if (
                func.attr == "start"
                and isinstance(func.value, ast.Name)
                and func.value.id == "worker"
            ):
                starts.setdefault(owner, []).append((node.lineno, node.col_offset))
                worker_starts[owner] = worker_starts.get(owner, 0) + 1
    assert slot_writes == []
    assert appends, "no code adds a worker to `call.workers`"
    for append in appends:
        _, owner, line, column = append.split(":")
        assert owner == "start_worker", append
        assert any(
            start < (int(line), int(column)) for start in starts.get(owner, [])
        ), append
    assert {name: callers.get(name, 0) for name in _ADMISSIONS} == _ADMISSIONS
    assert {name: worker_starts.get(name, 0) for name in _ADMISSIONS} == {
        name: 0 for name in _ADMISSIONS
    }


#: Each admission path, and how many workers it starts through `start_worker`.
_ADMISSIONS = {"_command_start": 1, "_activate": 1, "_admit_attempt": 3}


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
    real_drive = Supervisor._drive_run

    def drive(
        self: Supervisor,
        run_id: str,
        cwd: object,
        run_dir: object,
        prompt: object,
        roster: object,
        picked: picker.PickerResult,
        *args: object,
        **kwargs: object,
    ) -> None:
        if picked.executed_account != "oauth":
            real_drive(
                self, run_id, cwd, run_dir, prompt, roster, picked, *args, **kwargs
            )  # type: ignore[arg-type]
            return
        armed.append(threading.get_ident())
        self._fail_run(run_id, native.NativePreparationError(FENCE))

    real_get = Registry.get_run

    def get_run(self: Registry, run_id: str) -> object:
        # Only the refused run's own worker: a reaper tick reads rows too.
        if threading.get_ident() in armed:
            armed.clear()
            raise supervisor.STORE_ERRORS[0]("database is locked")
        return real_get(self, run_id)

    monkeypatch.setattr(Supervisor, "_drive_run", drive)
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
    real_drive = Supervisor._drive_run

    def drive(
        self: Supervisor,
        run_id: str,
        cwd: object,
        run_dir: object,
        prompt: object,
        roster: object,
        picked: picker.PickerResult,
        *args: object,
        **kwargs: object,
    ) -> None:
        if picked.executed_account != "oauth":
            real_drive(
                self, run_id, cwd, run_dir, prompt, roster, picked, *args, **kwargs
            )  # type: ignore[arg-type]
            return
        armed.append(threading.get_ident())
        self._fail_run(run_id, native.NativePreparationError(FENCE))

    real_update = Registry.update_run

    def update_run(self: Registry, row: object, **kwargs: object) -> None:
        if threading.get_ident() in armed and row.phase is RunPhase.FAILED:  # type: ignore[attr-defined]
            armed.clear()
            raise supervisor.STORE_ERRORS[0]("database is locked")
        real_update(self, row, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Supervisor, "_drive_run", drive)
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

    def drive(self: Supervisor, run_id: str, *_: object, **__: object) -> None:
        armed.append(threading.get_ident())
        self._fail_run(run_id, native.NativePreparationError(FENCE))

    real_update = Registry.update_run

    def update_run(self: Registry, row: object, **kwargs: object) -> None:
        if threading.get_ident() in armed and row.phase is RunPhase.FAILED:  # type: ignore[attr-defined]
            armed.clear()
            raise supervisor.STORE_ERRORS[0]("database is locked")
        real_update(self, row, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Supervisor, "_drive_run", drive)
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


#: A route on this host, below every other route by strength.
_LOCAL = (
    '[[provider]]\nname = "qwen"\nkind = "claude"\nstrength = 5\nmodel = "q"\n'
    'accounts = { server = { base_url = "http://127.0.0.1:9" } }\n'
)


@pytest.mark.parametrize("walled", [False, True], ids=["claude-open", "claude-walled"])
def test_a_review_falls_to_the_local_route_only_when_no_stronger_one_serves(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    driven: list[terminal.TerminalClassification],
    walled: bool,
) -> None:
    """The order stays strongest first (owner, 2026-10-21): the local route serves
    a review only when every route before it is skipped, and the warning says so.
    The review runs caged there: the cage serves the route's own endpoint."""
    _roster(roster_home, tmp_path, _LOCAL)
    if walled:
        now = int(time.time())
        for alias in ("oauth", "key"):
            registry.record_account_wall(
                alias, now + 3600, "vendor_reset", "seven_day", now
            )
    sv = _sv(registry, tmp_path)
    try:
        run_id = sv.review("feature-x", str(tmp_path)).run_id
        status = _ended(sv, run_id)
    finally:
        sv.shutdown()
    warning = status.warning or ""
    if not walled:
        assert status.executed_provider == "claude"
        assert "local or private endpoint" not in warning
        return
    assert (status.executed_provider, status.executed_account) == ("qwen", "server")
    assert f"skipped codex (NativePreparationError: {FENCE})" in warning
    assert "skipped claude (AllProvidersWalled" in warning
    assert (
        "review ran on qwen, a route on a local or private endpoint: no route "
        "before it in the order can serve now"
    ) in warning
    assert registry.session_profile(run_id) is not None
