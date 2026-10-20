"""Review findings: terminal update live custody and peer close terminal notice."""

import os
import socket
import time
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from registry_reset import reset_registry, sql, store

from nspawn import notify
from nspawn.process_identity import (
    ProcessIdentity,
    capture_process_identity,
    process_identity_matches,
)
from nspawn.registry import Registry, WorktreeHeld
from nspawn.rows import Backend, RunPhase, RunRow
from nspawn.supervisor import Supervisor


@pytest.fixture
def peers() -> Iterator[tuple[Registry, Registry, int]]:
    url = str(store())
    with Registry(url, "review-owner") as owner, Registry(url, "review-peer") as peer:
        now = int(time.time() * 1000)
        reset_registry(owner, now=now)
        peer.open(now=now)
        yield owner, peer, now


def admitted(owner: Registry, now: int, identity: ProcessIdentity) -> RunRow:
    row = RunRow(
        run_id="review-live",
        worktree="/review/f6",
        phase=RunPhase.RUNNING,
        sandbox="workspace-write",
        model="test",
        owner_instance=owner.instance_id,
        created_at=now,
        updated_at=now,
        backend=Backend.CLAUDE,
        process_identity=identity,
    )
    owner.insert_run(row)
    owner.claim_worktree(row.worktree, row.run_id, now=now, lease_ms=60000)
    return row


@pytest.mark.parametrize("phase", [RunPhase.FAILED, RunPhase.CLOSED])
def test_terminal_update_cannot_release_live_custody(
    peers: tuple[Registry, Registry, int], phase: RunPhase
) -> None:
    owner, peer, now = peers
    identity = capture_process_identity(os.getpid())
    row = admitted(owner, now, identity)
    owner.update_run(replace(row, phase=phase, updated_at=now + 1))
    assert process_identity_matches(identity)
    claim = peer.read_claim(row.worktree)
    print("phase", phase.value, "holder_alive", True, "claim", claim)
    with pytest.raises(WorktreeHeld):
        peer.claim_worktree(row.worktree, "second-writer", now=now + 2, lease_ms=100)


def test_peer_close_of_a_run_with_a_notices_row_for_a_live_caller_pushes_once(
    peers: tuple[Registry, Registry, int],
    tmp_path: Path,
) -> None:
    """Replaces the env-socket version: the caller is the notices row, and the
    peer that commits the close pushes to it once — a second close pushes nothing."""
    owner, peer, now = peers
    live = capture_process_identity(os.getpid())
    dead = replace(live, start_ticks=live.start_ticks + 1)
    row = admitted(owner, now, dead)
    # The session's socket is `<its pid>.sock`: an address its pid does not
    # name is no caller (D5).
    sock_dir = (
        Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / f"custody-{os.getpid()}"
    )
    sock_dir.mkdir(exist_ok=True)
    sock_path = sock_dir / f"{os.getpid()}.sock"
    sock_path.unlink(missing_ok=True)
    sql(
        owner,
        "INSERT INTO notices"
        "(run_id, caller_addr, caller_pid, caller_start_ticks, caller_project, created_at) "
        "VALUES (?, ?, ?, ?, '/review/.git', ?)",
        (row.run_id, str(sock_path), live.pid, live.start_ticks, now),
    )
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sink:
        sink.bind(str(sock_path))
        sink.listen()
        sink.settimeout(0.5)
        # Positive control: the sink hears a plain send.
        assert notify.send_notice(str(sock_path), "review positive control")
        conn, _ = sink.accept()
        with conn:
            assert b"review positive control" in conn.recv(4096)
        sv = Supervisor(peer, tmp_path / "runs", announces_terminal=True)
        try:
            outcome = sv.close(row.run_id)
            assert outcome.phase is RunPhase.CLOSED and outcome.claim_released
            assert peer.claim_worktree(
                row.worktree, "second-writer", now=now + 2, lease_ms=100
            )
            sv.close(row.run_id)
            received = []
            while True:
                try:
                    conn, _ = sink.accept()
                except TimeoutError:
                    break
                with conn:
                    received.append(conn.recv(4096))
            print(
                "close",
                outcome.phase.value,
                "claim_released",
                outcome.claim_released,
                "terminal_notices",
                len(received),
            )
            assert len(received) == 1, (
                "peer close must push exactly one terminal notice"
            )
            assert f"{row.run_id} is closed".encode() in received[0]
        finally:
            sv.shutdown()
            sock_path.unlink(missing_ok=True)
            sock_dir.rmdir()
