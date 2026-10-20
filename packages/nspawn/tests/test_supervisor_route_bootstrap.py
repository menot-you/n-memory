"""A sleeping route declares the command that wakes it.

SERVER's qwen is not always up; what brings it up is
`systemctl --user start qwen-serving`. Without this dispatch dies with the
vendor error and someone has to manually touch the box — which is the supervision
this system exists to eliminate.

The rule that cost a session to learn is in the third test: a command
exiting with code zero is NOT proof that the service is responding. The proof is reading
the route back.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from nspawn import supervisor
from nspawn.owner_roster import NativeAccount

_UP = ("/usr/bin/true",)


def _account(*, bootstrap: tuple[str, ...] = _UP) -> NativeAccount:
    return NativeAccount(
        home=None, base_url="http://127.0.0.1:59999", bootstrap=bootstrap
    )


def test_a_live_route_is_never_bootstrapped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(supervisor, "_route_answers", lambda url, timeout: True)
    outcome = supervisor.bring_route_up(_account())
    assert outcome.ran is False
    assert outcome.live is True


class _SettleClock:
    """`supervisor.time` with `sleep` recorded instead of slept."""

    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    def sleep(self, seconds: float) -> None:
        self._calls.append(f"settle {seconds}")

    def __getattr__(self, name: str) -> object:
        return getattr(time, name)


def test_a_dead_route_is_bootstrapped_once_and_read_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers = iter([False, True])
    calls: list[str] = []

    def probe(url: str, timeout: float) -> bool:
        calls.append(url)
        return next(answers)

    monkeypatch.setattr(supervisor, "_route_answers", probe)
    # The default settle is observed, not waited out: two real seconds of
    # sleep proved nothing the recorded call does not. Only the supervisor
    # module's own `time` is replaced — `subprocess` polls with `time.sleep`
    # too, and a global patch recorded its backoff under load.
    monkeypatch.setattr(supervisor, "time", _SettleClock(calls))
    outcome = supervisor.bring_route_up(_account())
    assert outcome.ran is True
    assert outcome.live is True
    # Probed before and after, settling in between: the second read is the proof.
    assert len(calls) == 3
    assert calls[1] == "settle 2.0"


def test_a_zero_exit_is_not_proof_that_the_route_serves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The discriminator: `/usr/bin/true` succeeds and the route stays dead."""
    monkeypatch.setattr(supervisor, "_route_answers", lambda url, timeout: False)
    outcome = supervisor.bring_route_up(_account(), settle_s=0.0)
    assert outcome.ran is True
    assert outcome.returncode == 0
    assert outcome.live is False
    assert "did not answer" in (outcome.detail or "")


def test_a_failing_bootstrap_reports_its_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(supervisor, "_route_answers", lambda url, timeout: False)
    outcome = supervisor.bring_route_up(
        _account(bootstrap=("/usr/bin/false",)), settle_s=0.0
    )
    assert outcome.ran is True
    assert outcome.returncode == 1
    assert outcome.live is False


def test_no_declared_bootstrap_invents_no_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Valid progress: nothing is run for a route the owner did not equip.

    Without this, a helper that always shelled out would pass the tests above
    while running an empty argv — or worse, a guessed one — on every dispatch.
    """
    monkeypatch.setattr(supervisor, "_route_answers", lambda url, timeout: False)
    outcome = supervisor.bring_route_up(_account(bootstrap=()))
    assert outcome.ran is False
    assert outcome.live is False


def test_an_account_with_no_endpoint_is_left_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A native account has no `base_url` to probe; it is not a sleeping route."""

    def forbidden(url: str, timeout: float) -> bool:
        raise AssertionError("probed an account that declares no endpoint")

    monkeypatch.setattr(supervisor, "_route_answers", forbidden)
    outcome = supervisor.bring_route_up(NativeAccount(home=None, bootstrap=_UP))
    assert outcome.ran is False


# --- dispatch: what calls this -----------------------------------------------
# A helper without caller is a promise delivered disconnected. These two measure the
# PATH, not helper: `_spawn_and_wait` brings up chosen account route
# before setting up environment, and rejects when unsuccessful.


def _supervisor_stub(tmp_path: Path):
    from nspawn.owner_roster import Provider, Roster

    instance = object.__new__(supervisor.Supervisor)
    instance._lock = threading.Condition()
    instance._registry = SimpleNamespace(inference_profile=lambda run_id: None)
    provider = Provider(name="qwen", kind="claude", model="q", models=["q"])
    provider.accounts = {"server": _account()}
    provider.bin = Path("/usr/bin/true")
    roster = Roster(providers=[provider])
    roster.bins = {"claude": Path("/usr/bin/true")}
    roster.defaults.plugin_root = str(tmp_path)
    picked = SimpleNamespace(
        executed_provider=provider,
        executed_account="server",
        model="q",
        effort="low",
    )
    return instance, roster, picked


def _stop_before_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cut the dispatch the moment it would go looking for the binary.

    That is the first step AFTER the route has to be up: inspecting a vendor
    binary for a machine that is not serving is work nobody needs. Everything
    past it wants a real binary, a real plugin dir and a real store, and this
    marker says «reached the end of the path that matters» without any of them.
    """
    from nspawn import claude_child

    def marker(*args: object, **kwargs: object):
        raise RuntimeError("REACHED_BUILD_ENV")

    monkeypatch.setattr(claude_child, "resolve_claude_binary", marker)


def test_the_dispatch_raises_the_route_of_the_account_it_picked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    levantadas: list[str | None] = []

    def registar(account, **kwargs: object):
        levantadas.append(account.base_url)
        return supervisor.BootstrapOutcome(
            ran=True, live=True, argv=_UP, returncode=0, detail=None
        )

    monkeypatch.setattr(supervisor, "bring_route_up", registar)
    _stop_before_spawn(monkeypatch)
    instance, roster, picked = _supervisor_stub(tmp_path)

    with pytest.raises(RuntimeError, match="REACHED_BUILD_ENV"):
        instance._spawn_and_wait(
            "run-1",
            str(tmp_path),
            SimpleNamespace(root=tmp_path),
            "oi",
            roster,
            picked,
            "thread-1",
            False,
            False,
        )

    assert levantadas == ["http://127.0.0.1:59999"]


def test_a_route_that_would_not_come_up_refuses_instead_of_spawning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal names the account, the command and the endpoint.

    Without this the child starts up and dies with `connection refused` from the vendor, which is
    the error that tells nobody what to do — and the reason the bootstrap
    exists.
    """
    monkeypatch.setattr(
        supervisor,
        "bring_route_up",
        lambda account, **kwargs: supervisor.BootstrapOutcome(
            ran=True,
            live=False,
            argv=_UP,
            returncode=0,
            detail="route did not answer after /usr/bin/true",
        ),
    )
    _stop_before_spawn(monkeypatch)
    instance, roster, picked = _supervisor_stub(tmp_path)

    with pytest.raises(supervisor.SupervisorError) as erro:
        instance._spawn_and_wait(
            "run-2",
            str(tmp_path),
            SimpleNamespace(root=tmp_path),
            "oi",
            roster,
            picked,
            "thread-2",
            False,
            False,
        )
    mensagem = str(erro.value)
    assert "server" in mensagem
    assert "127.0.0.1:59999" in mensagem
    assert "/usr/bin/true" in mensagem


def test_a_route_already_up_lets_the_dispatch_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Valid progress: `ran=False` is never read as failure.

    Without this pair, a guard that refused whenever `live` was false
    would pass the test above and block every native route, which has
    no endpoint at all to probe.
    """
    monkeypatch.setattr(
        supervisor,
        "bring_route_up",
        lambda account, **kwargs: supervisor.BootstrapOutcome(
            ran=False, live=False, argv=(), returncode=None, detail=None
        ),
    )
    _stop_before_spawn(monkeypatch)
    instance, roster, picked = _supervisor_stub(tmp_path)

    with pytest.raises(RuntimeError, match="REACHED_BUILD_ENV"):
        instance._spawn_and_wait(
            "run-3",
            str(tmp_path),
            SimpleNamespace(root=tmp_path),
            "oi",
            roster,
            picked,
            "thread-3",
            False,
            False,
        )
