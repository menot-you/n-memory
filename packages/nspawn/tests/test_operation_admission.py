"""Behavioral RED preparation for optional native operation admission."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from class_dispatch import system1_never_reaches_the_network
from hypothesis import given, settings
from hypothesis import strategies as st
from registry_reset import sql
from supervisor_support import (
    _drain_worker_threads,
    _stop_every_reaper,
    _sv,
    registry,
    roster_home,
)
from test_driver import _call, _payload
from test_native_write import (
    _write,
    claude_writer,
    paper_slices,
    writer_route,
)

from nspawn.driver import request_fingerprint
from nspawn.mcp import McpServer, batch_runs
from nspawn.registry import Registry
from nspawn.rows import RunPhase

__all__ = [
    "_drain_worker_threads",
    "_stop_every_reaper",
    "claude_writer",
    "paper_slices",
    "registry",
    "roster_home",
    "system1_never_reaches_the_network",
    "writer_route",
]

UNCHECKED = "operations_unchecked: declared-operation compatibility was not checked"


def _operations(**changes: Any) -> dict[str, Any]:
    return {
        "version": 1,
        "tools": [],
        "paths": [],
        "network": [],
        "hosts": [],
        "actions": [],
        **changes,
    }


@pytest.mark.needs("systemd_user", "cgroup")
def test_omitted_operations_keep_admission_and_report_unchecked(
    registry: Registry,
    tmp_path: Path,
    writer_route: SimpleNamespace,
) -> None:
    sv = _sv(registry, tmp_path)
    server = McpServer(supervisor=sv)
    run_id = _payload(_call(server, "nspawn_run_start", _write(writer_route)))["run_id"]
    try:
        terminal = sv.status(run_id, timeout_ms=20_000)
        assert terminal.phase is RunPhase.COMPLETED, terminal.failure_reason
        assert sv.result(run_id).final_text == "DONE"
        shown = _payload(_call(server, "nspawn_run_show", {"run_id": run_id}))
        assert shown["warning"] == terminal.warning
        assert shown.get("operation_admission") == {
            "checked": False,
            "reason": UNCHECKED,
        }
    finally:
        sv.close(run_id)
        sv.shutdown()


@pytest.mark.parametrize("declaration", [_operations(), _operations(tools=["shell"])])
def test_operations_identity_preserves_omitted_historical_requests(
    declaration: dict[str, Any],
) -> None:
    arguments = {
        "prompt": "bounded fixture work",
        "cwd": "/var/tmp/m6-identity",
        "read_only": True,
    }
    omitted = request_fingerprint("nspawn_run_start", arguments, group=None)
    assert omitted == "a8d21bd4173c3a22169e062941472f2681d472511a3c12391dd7b9c176a56922"
    explicit = request_fingerprint(
        "nspawn_run_start", {**arguments, "operations": declaration}, group=None
    )
    assert explicit != omitted, (
        "An explicit declaration must participate in request identity"
    )


def _path(path: str, *, base: str = "absolute", scope: str = "path") -> dict[str, str]:
    return {"base": base, "path": path, "access": "write", "scope": scope}


def _refusal(answer: dict[str, Any], field: str, cause: str) -> dict[str, Any]:
    assert "error" in answer, answer
    assert answer["error"]["code"] == -32602, answer
    assert "nerr" not in answer["error"].get("data", {}), answer
    data = answer["error"].get("data", {})
    assert data.get("type") == "OperationRefused", answer
    assert data.get("cause") == cause, answer
    failures = data.get("failures", [])
    assert any(item["field"].startswith(field) for item in failures), answer
    assert data.get("remedy"), answer
    assert data.get("operation_admission", {}).get("checked") is False, answer
    return data


def _no_start(registry: Registry, route: SimpleNamespace) -> None:
    assert sql(registry, "SELECT run_id FROM runs").fetchall() == []
    assert registry.read_claim(route.worktree) is None
    assert not (route.record / "frames.ndjson").exists()
    assert not list(route.record.glob("cgroup-*"))


@pytest.mark.needs("systemd_user", "cgroup")
def test_empty_operations_remain_unchecked(
    registry: Registry,
    tmp_path: Path,
    writer_route: SimpleNamespace,
) -> None:
    sv = _sv(registry, tmp_path)
    try:
        answer = _call(
            McpServer(supervisor=sv),
            "nspawn_run_start",
            _write(writer_route, operations=_operations()),
        )
        assert "error" not in answer, answer
        accepted = _payload(answer)
        assert "operation_admission" not in accepted
        run_id = accepted["run_id"]
        assert sv.status(run_id, timeout_ms=20_000).phase is RunPhase.COMPLETED
        shown = _payload(
            _call(McpServer(supervisor=sv), "nspawn_run_show", {"run_id": run_id})
        )
        assert shown["operation_admission"] == {"checked": False, "reason": UNCHECKED}
        sv.close(run_id)
    finally:
        sv.shutdown()


@settings(max_examples=20)
@given(
    value=st.one_of(
        st.none(), st.booleans(), st.integers(), st.lists(st.integers(), max_size=5)
    )
)
def test_nested_declaration_shapes_are_validated_at_the_public_boundary(
    value: object,
) -> None:
    # Shape validation precedes even supervisor construction.
    class NoSupervisor:
        def execute(self, *args: Any, **kwargs: Any) -> Any:
            pytest.fail("invalid nested declaration reached native admission")

    answer = _call(
        McpServer(supervisor=NoSupervisor()),
        "nspawn_run_start",
        {
            "prompt": "bounded",
            "cwd": "/var/tmp/m6-shapes",
            "read_only": True,
            "operations": _operations(network=[{"host": value, "port": 443}]),
        },
    )
    _refusal(answer, "operations.network", "invalid")


@pytest.mark.parametrize("problem", ["missing", "relative", "digest"])
def test_explicit_stdio_target_never_falls_back_to_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, problem: str
) -> None:
    from live.live_support import McpStdio

    target = tmp_path / "nspawn"
    if problem != "missing":
        target.write_text("#!/bin/false\n")
        target.chmod(0o700)
    if problem == "relative":
        target = Path("nspawn")
    monkeypatch.setattr(
        subprocess, "Popen", lambda *a, **k: pytest.fail("invalid target launched")
    )
    with pytest.raises(ValueError, match="target"):
        McpStdio({}, executable=target, cwd=tmp_path, expected_sha256="0" * 64)


def test_explicit_stdio_target_uses_neutral_directory_without_source_injection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hashlib
    import io

    from live.live_support import McpStdio

    target = tmp_path / "nspawn"
    target.write_text("#!/bin/false\n")
    target.chmod(0o700)
    observed: dict[str, Any] = {}

    def opened(argv: list[str], **kwargs: Any) -> Any:
        observed.update(argv=argv, **kwargs)
        return SimpleNamespace(stdin=io.BytesIO(), stdout=io.BytesIO())

    monkeypatch.setattr(subprocess, "Popen", opened)
    McpStdio(
        {"PYTHONPATH": "source-injection", "HOME": str(tmp_path)},
        executable=target,
        cwd=tmp_path,
        expected_sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
    )
    assert observed["argv"] == [str(target), "mcp", "serve"]
    assert observed["cwd"] == tmp_path
    assert "PYTHONPATH" not in observed["env"]


def test_batch_keeps_operation_evidence_without_copying_unknown_driver_data() -> None:
    answer = batch_runs(
        SimpleNamespace(declared_providers=lambda cwd: (["fixture"], "fixture")),
        "bounded",
        "/var/tmp",
        ["fixture"],
        dispatch=lambda entries: [
            {
                "refused": {
                    "type": "OperationRefused",
                    "message": "safe",
                    "code": -32602,
                    "cause": "unproved",
                    "failures": [],
                    "remedy": "changed task",
                    "operation_admission": {
                        "checked": False,
                        "reason": "declaration refused",
                    },
                    "unrelated": "synthetic-private-driver-canary",
                }
            }
        ],
    )
    assert answer["refused"][0]["remedy"] == "changed task"
    assert "synthetic-private-driver-canary" not in json.dumps(answer)
