"""The caller learns the child's address instead of polling for its phase.

Measured 2026-08-31: a parent that wants to know when a run ends had exactly
one verb, `nspawn_run_show`, and it is a POLL. The platform already carries a
push — `SendMessage{notify_when_idle: true}` against the child's own session
socket — but nothing on this surface ever told the caller what that address is.

For a Claude backend the address is predictable and needs no registry:
the child is spawned by
`child.spawn_child`, whose `argv[0]` IS the claude binary, so `identity.pid` is
the claude pid, and claude binds `<sockets dir>/<pid>.sock`. The sockets dir is
read from the supervisor's OWN inherited `CLAUDE_CODE_MESSAGING_SOCKET` rather
than hardcoded: the binary accepts four namespace layouts plus an explicit
`--messaging-socket-path`, so a literal `/run/user/<uid>/cc-socks` would be a
guess that breaks off this host. Other backends do not bind a Claude session
socket, so their status carries no address even while the process is alive.
"""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path
from typing import Any

import pytest

from nspawn import child, supervisor
from nspawn.process_identity import ProcessIdentityError, capture_process_identity
from nspawn.rows import Backend, RunPhase, RunRow
from nspawn.supervisor import _to_status


@pytest.fixture(autouse=True)
def _caller_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_CODE_MESSAGING_SOCKET", "/run/user/1000/cc-socks/1.sock")


def _row(*, backend: Backend = Backend.CLAUDE, **overrides: Any) -> RunRow:
    return RunRow(
        run_id="run-1",
        worktree="/worktree",
        phase=RunPhase.RUNNING,
        sandbox="workspace-write",
        model="model",
        owner_instance="instance",
        created_at=1,
        updated_at=2,
        backend=backend,
        **overrides,
    )


def test_the_address_is_the_caller_socket_dir_plus_the_child_pid() -> None:
    addr = child.child_socket_address(
        4242,
        environ={"CLAUDE_CODE_MESSAGING_SOCKET": "/run/user/1000/cc-socks/99.sock"},
    )
    assert addr == "uds:/run/user/1000/cc-socks/4242.sock"


def test_no_caller_socket_means_no_address_not_a_guess() -> None:
    """A supervisor booted outside a Claude session has no address to offer.

    Advisory plumbing: a typed refusal here would break every standalone start
    for a field nobody asked for. Absent is absent, and the caller reads null.
    """
    assert child.child_socket_address(4242, environ={}) is None


def test_a_malformed_caller_socket_yields_no_address() -> None:
    assert (
        child.child_socket_address(4242, environ={"CLAUDE_CODE_MESSAGING_SOCKET": ""})
        is None
    )
    # A relative path with no directory component (dirname == "") returns None.
    assert (
        child.child_socket_address(
            4242, environ={"CLAUDE_CODE_MESSAGING_SOCKET": "noslash.sock"}
        )
        is None
    )


def test_status_carries_the_child_address_derived_from_the_persisted_pid(
    tmp_path: Path,
) -> None:
    """No new state: the pid the registry already stores IS the address.

    `RunRow.process_identity` is written at RUNNING (`supervisor.py:1640`), so
    the address needs no file, no column and no second author — `_to_status`
    derives it on read. A row with no identity yet reads null, which is the
    honest answer while the child has not been spawned.
    """
    identity = capture_process_identity(os.getpid())
    row = _row(process_identity=identity)
    status = _to_status(row, tmp_path)
    assert status.child_addr == f"uds:/run/user/1000/cc-socks/{os.getpid()}.sock"

    assert _to_status(_row(), tmp_path).child_addr is None


@pytest.mark.parametrize("backend", [Backend.CODEX, Backend.AGY, Backend.GROK])
def test_a_non_claude_child_has_no_session_socket_address(
    backend: Backend, tmp_path: Path
) -> None:
    identity = capture_process_identity(os.getpid())
    row = _row(backend=backend, process_identity=identity)
    assert _to_status(row, tmp_path).child_addr is None


def test_a_recycled_pid_yields_no_address(tmp_path: Path) -> None:
    """B1, from the adversarial review: the pid alone is NOT the fence.

    `_update_phase` never clears `process_identity` on a natural exit — only an
    explicit `close()` does — so a COMPLETED row keeps naming a pid the kernel
    is free to hand to somebody else. Deriving the address from that pid alone
    pointed a caller's subscription at a STRANGER'S session: the verdict flips
    from "my child is done" to "some unrelated process went idle".

    The supervisor already owns the right fence and uses it elsewhere
    (`supervisor.py:1064`): the pid TRIPLE, pid + boot_id + start_ticks. The
    address is offered only while that triple still matches a live process.
    """
    live = capture_process_identity(os.getpid())
    assert _to_status(_row(process_identity=live), tmp_path).child_addr is not None

    # Same pid, different start ticks — exactly what a recycled pid looks like.
    stale = dataclasses.replace(live, start_ticks=live.start_ticks + 1)
    assert _to_status(_row(process_identity=stale), tmp_path).child_addr is None


def test_a_refused_identity_check_yields_no_address_not_a_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B4, from the adversarial review: the fence RAISES, it does not answer False.

    `process_identity_matches` refuses on a non-Linux host and on either /proc
    read hitting an OSError that is not ENOENT. The measured one is the teardown
    race the sibling caller already documents at `_wait_identity_dead`:
    `/proc/<pid>/stat` opens fine and then reads ESRCH.

    Letting that escape turns the ONE status read the new contract tells a
    caller to make into a typed refusal — at exactly the moment the child dies,
    which is the case the fence exists to serve. Reading status could not fail
    on identity before this candidate.
    """

    def _refuse(_identity: object) -> bool:
        raise ProcessIdentityError("stat read hit ESRCH mid-teardown")

    monkeypatch.setattr(supervisor, "process_identity_matches", _refuse)
    row = _row(process_identity=capture_process_identity(os.getpid()))
    assert _to_status(row, tmp_path).child_addr is None
