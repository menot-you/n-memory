"""Driven only by `test_harness_isolation.py`, in a child pytest whose parent
environment names a session socket; the outer test asserts the socket heard nothing."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from supervisor_support import _drain_worker_threads, _stop_every_reaper, registry

from nspawn import terminal
from nspawn.child import CALLER_SOCKET_ENV
from nspawn.registry import Registry
from nspawn.rows import RunPhase
from nspawn.supervisor import Supervisor

__all__ = ["_drain_worker_threads", "_stop_every_reaper", "registry"]


def test_inner_an_armed_seam_in_a_test_process_reaches_no_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, registry: Registry
) -> None:
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise OSError("the registry socket is gone")

    supervisor = Supervisor(registry, tmp_path)
    supervisor.announces_terminal = True
    (tmp_path / "run-leak").mkdir()
    monkeypatch.setattr(Supervisor, "_update_phase", refuse, raising=True)
    supervisor._publish_terminal(
        "run-leak",
        terminal.TerminalClassification(
            phase=RunPhase.COMPLETED,
            final_text=None,
            warning=None,
            marker=None,
            provider_wall=None,
            account_wall=None,
        ),
    )
    assert CALLER_SOCKET_ENV not in os.environ
