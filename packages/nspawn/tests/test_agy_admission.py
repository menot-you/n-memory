"""No new AGY-backed launch is admitted when its enforcing cage is unavailable.
The refusal is chosen by the
backend the picker resolved, never by a provider's spelling, and it falls before
any row, run directory or child exists. What launches nothing — showing, listing,
closing and the idempotent lookup of a run already admitted — keeps working."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from registry_reset import store
from supervisor_support import (
    _drain_worker_threads,
    _stop_every_reaper,
    _sv,
    registry,
    roster_home,
)
from test_driver import _call, _payload
from test_native_execute import _store_rows
from test_native_write import (
    _forbidden,
    _read_only,
    system1_never_reaches_the_network,
    writer_route,
)

from nspawn import child, native, picker, supervisor
from nspawn.mcp import McpServer
from nspawn.owner_roster import Provider
from nspawn.registry import Registry
from nspawn.rows import Backend, RunPhase
from nspawn.supervisor import Supervisor

__all__ = [
    "_drain_worker_threads",
    "_stop_every_reaper",
    "registry",
    "roster_home",
    "store",
    "system1_never_reaches_the_network",
    "writer_route",
]

REFUSED = "AGY launches are refused"


def _route_to(monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    """The picker resolves a route of `kind` whose NAME says nothing about it."""
    routed = Provider(name="fixture", kind=kind, model="fixture", models=["fixture"])
    picked = picker.PickerResult(None, routed, "fixture", "fixture", None, None)
    monkeypatch.setattr(supervisor.picker, "pick_execution", lambda *a, **k: picked)


def _refused(answer: dict[str, Any]) -> None:
    assert answer.get("error", {}).get("data", {}).get("type") == "InvalidRunRequest", (
        answer
    )
    assert REFUSED in answer["error"]["message"], answer
    assert _store_rows("SELECT count(*) FROM runs")[0][0] == 0


@pytest.mark.parametrize(
    "call",
    ["run_read_only", "run_resumed", "review", "resumed_session_recorded_agy"],
)
def test_an_agy_route_is_refused_before_any_row_or_child(
    registry: Registry,
    tmp_path: Any,
    writer_route: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    call: str,
) -> None:
    if call == "resumed_session_recorded_agy":
        # The picker resolves codex; only the session the turn resumes was AGY.
        recorded = SimpleNamespace(backend=Backend.AGY, executed_account="fixture")
        monkeypatch.setattr(
            Registry, "resolve_native_session", lambda *a, **k: (recorded, None)
        )
    else:
        _route_to(monkeypatch, "agy")
    monkeypatch.setattr(native, "agy_cage_available", lambda: False)
    monkeypatch.setattr(native, "prepare_native", _forbidden)
    monkeypatch.setattr(child, "spawn_child", _forbidden)
    monkeypatch.setattr(supervisor, "create_run_directory", _forbidden)
    server = McpServer(supervisor=_sv(registry, tmp_path))
    if call == "review":
        answer = _call(
            server,
            "nspawn_review_start",
            {"branch": "lane", "cwd": str(writer_route.checkout), "prompt": "judge"},
        )
    else:
        resumed = call in ("run_resumed", "resumed_session_recorded_agy")
        extra = {"session_id": str(uuid.uuid4())} if resumed else {}
        answer = _call(
            server, "nspawn_run_start", {**_read_only(writer_route), **extra}
        )
    _refused(answer)


def test_a_codex_route_still_admits(
    registry: Registry,
    tmp_path: Any,
    writer_route: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The positive control: the same dispatch on a codex route is admitted."""
    monkeypatch.setattr(Supervisor, "_drive_native_run", lambda self, *args: None)
    server = McpServer(supervisor=_sv(registry, tmp_path))
    accepted = _payload(_call(server, "nspawn_run_start", _read_only(writer_route)))
    row = registry.get_run(accepted["run_id"])
    assert row is not None and row.backend is Backend.CODEX


def test_an_agy_run_admitted_before_the_refusal_is_shown_listed_found_and_closed(
    registry: Registry,
    tmp_path: Any,
    writer_route: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run that exists launches nothing more by being read, looked up or closed."""
    _route_to(monkeypatch, "agy")
    monkeypatch.setattr(Supervisor, "_drive_native_run", lambda self, *args: None)
    server = McpServer(supervisor=_sv(registry, tmp_path))
    arguments = {**_read_only(writer_route), "idempotency_key": "earlier"}
    with monkeypatch.context() as before:
        before.setattr(native, "agy_cage_available", lambda: True)
        run_id = _payload(_call(server, "nspawn_run_start", arguments))["run_id"]
    row = registry.get_run(run_id)
    assert row is not None and row.backend is Backend.AGY

    monkeypatch.setattr(native, "prepare_native", _forbidden)
    monkeypatch.setattr(native, "agy_cage_available", lambda: False)
    monkeypatch.setattr(child, "spawn_child", _forbidden)
    again = _payload(_call(server, "nspawn_run_start", arguments))
    assert (again["run_id"], again["deduplicated"]) == (run_id, True)
    shown = _payload(_call(server, "nspawn_run_show", {"run_id": run_id}))
    assert shown["backend"] == "agy"
    listed = _payload(_call(server, "nspawn_run_list", {}))
    assert run_id in {run["run_id"] for run in listed["runs"]}
    closed = _payload(_call(server, "nspawn_run_close", {"run_id": run_id}))
    assert closed["phase"] == RunPhase.CLOSED.value
