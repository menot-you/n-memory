"""A freed pid cannot impersonate.

ZERO skips (journal §31, the SL-B pattern): the platform-refusal branch is
forced to run on every host, and the real probe code paths run against a
FIXTURE /proc tree through the injected root.
"""

import errno
import os
import signal
import struct
import subprocess
import sys
import time
from pathlib import Path

import pytest

from nspawn import process_identity as pi
from nspawn.process_identity import (
    ProcessIdentity,
    ProcessIdentityError,
    capture_process_identity,
    group_is_only_zombies,
    group_member_pids,
    parse_proc_start_ticks,
    process_identity_matches,
)

FIXTURE_PID = 4242


def _stat_line(command: str, start_ticks: int) -> str:
    """A /proc/<pid>/stat line with the start-time field at its real index.

    The command carries a closing parenthesis, so a parser that scans for the
    first ')' reads the wrong field.
    """
    fields = ["S", *[str(n) for n in range(4, 22)], str(start_ticks), "23"]
    return f"{FIXTURE_PID} ({command}) name) " + " ".join(fields)


@pytest.fixture
def fixture_proc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A /proc tree: one boot id and one live process with a known start tick.

    Forces the Linux gate open and points the probes at the fixture root, so
    the REAL read/parse/match code paths execute on any host.
    """
    boot_dir = tmp_path / "sys/kernel/random"
    boot_dir.mkdir(parents=True)
    (boot_dir / "boot_id").write_text("fixture-boot-1234\n", encoding="utf-8")
    stat_dir = tmp_path / str(FIXTURE_PID)
    stat_dir.mkdir()
    (stat_dir / "stat").write_text(
        _stat_line("a command) with )parens", 4242), encoding="utf-8"
    )
    monkeypatch.setattr(pi, "_platform", lambda: "linux")
    monkeypatch.setattr(pi, "PROC_ROOT", tmp_path)
    return tmp_path


def test_the_parser_survives_spaces_and_parentheses_in_the_command() -> None:
    fields = ["S", *[str(n) for n in range(4, 22)], "4242", "23"]
    stat = "7 (a command) name) " + " ".join(fields)
    assert parse_proc_start_ticks(stat) == 4242


def test_the_parser_refuses_a_line_it_cannot_read() -> None:
    with pytest.raises(ProcessIdentityError, match="command terminator"):
        parse_proc_start_ticks("7 a command 4242")
    with pytest.raises(ProcessIdentityError, match="start-time field"):
        parse_proc_start_ticks("7 (cmd) S 1 2 3")
    fields = ["S", *[str(n) for n in range(4, 22)], "not-a-number", "23"]
    with pytest.raises(ProcessIdentityError, match="invalid process start time"):
        parse_proc_start_ticks("7 (cmd) " + " ".join(fields))


def test_an_identity_refuses_a_pid_outside_u32() -> None:
    ProcessIdentity(pid=1, boot_id="b", start_ticks=0)
    with pytest.raises(ValueError):
        ProcessIdentity(pid=-1, boot_id="b", start_ticks=1)
    with pytest.raises(ValueError):
        ProcessIdentity(pid=2**32, boot_id="b", start_ticks=1)
    with pytest.raises(TypeError):
        ProcessIdentity(pid=True, boot_id="b", start_ticks=1)


def test_both_probes_refuse_on_a_platform_that_cannot_prove_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refusal branch RUNS on every host — forced, not gated by skipif.

    NOT a red: on a platform that cannot prove identity the CORRECT behaviour
    is a typed refusal, and that is what is asserted. A fabricated identity
    here would be the defect.
    """
    monkeypatch.setattr(pi, "_platform", lambda: "win32")
    with pytest.raises(ProcessIdentityError, match="only on Linux and macOS"):
        capture_process_identity(1)
    identity = ProcessIdentity(pid=1, boot_id="b", start_ticks=1)
    with pytest.raises(
        ProcessIdentityError, match="cannot be verified on this platform"
    ):
        process_identity_matches(identity)


def test_a_live_identity_matches_and_a_bumped_start_tick_does_not(
    fixture_proc: Path,
) -> None:
    identity = capture_process_identity(FIXTURE_PID)
    assert identity.boot_id == "fixture-boot-1234"
    assert identity.start_ticks == 4242
    assert process_identity_matches(identity)
    bumped = ProcessIdentity(
        pid=identity.pid,
        boot_id=identity.boot_id,
        start_ticks=identity.start_ticks + 1,
    )
    assert not process_identity_matches(bumped)
    other_boot = ProcessIdentity(
        pid=identity.pid, boot_id="not-this-boot", start_ticks=identity.start_ticks
    )
    assert not process_identity_matches(other_boot)


def test_a_vanished_pid_is_a_mismatch_and_a_refusal(
    fixture_proc: Path,
) -> None:
    """The two halves of a gone process: capture REFUSES (it must establish an
    identity), match answers False (the process it named is simply gone)."""
    with pytest.raises(ProcessIdentityError, match="no such process 9999"):
        capture_process_identity(9999)
    gone = ProcessIdentity(pid=9999, boot_id="fixture-boot-1234", start_ticks=1)
    assert not process_identity_matches(gone)


@pytest.mark.parametrize(
    "error_number", [errno.ENOENT, errno.ESRCH, errno.EACCES, errno.EIO]
)
def test_process_stat_read_errors_preserve_absence_and_refusal(
    fixture_proc: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_number: int,
) -> None:
    identity = capture_process_identity(FIXTURE_PID)
    read_text = Path.read_text

    def read(path: Path, *args, **kwargs):
        if path == fixture_proc / str(FIXTURE_PID) / "stat":
            raise OSError(error_number, os.strerror(error_number))
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    if error_number in (errno.ENOENT, errno.ESRCH):
        assert not process_identity_matches(identity)
        with pytest.raises(ProcessIdentityError, match="no such process"):
            capture_process_identity(FIXTURE_PID)
    else:
        with pytest.raises(ProcessIdentityError, match="cannot read process stat"):
            process_identity_matches(identity)
        with pytest.raises(ProcessIdentityError, match="cannot read process stat"):
            capture_process_identity(FIXTURE_PID)


def test_an_empty_or_unreadable_boot_id_refuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pi, "_platform", lambda: "linux")
    empty_dir = tmp_path / "sys/kernel/random"
    empty_dir.mkdir(parents=True)
    (empty_dir / "boot_id").write_text("   \n", encoding="utf-8")
    monkeypatch.setattr(pi, "PROC_ROOT", tmp_path)
    with pytest.raises(ProcessIdentityError, match="boot id is empty"):
        capture_process_identity(FIXTURE_PID)

    missing = tmp_path / "elsewhere"
    missing.mkdir()
    monkeypatch.setattr(pi, "PROC_ROOT", missing)
    with pytest.raises(ProcessIdentityError, match="cannot read boot id"):
        capture_process_identity(FIXTURE_PID)


def test_an_unreadable_stat_is_a_typed_error_not_a_silent_match(
    fixture_proc: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stat that exists but cannot be read is a failure, never a False."""
    stat_file = fixture_proc / str(FIXTURE_PID) / "stat"
    stat_file.chmod(0o000)
    identity = ProcessIdentity(
        pid=FIXTURE_PID, boot_id="fixture-boot-1234", start_ticks=1
    )
    with pytest.raises(ProcessIdentityError, match="cannot read process stat"):
        process_identity_matches(identity)
    stat_file.chmod(0o644)


# ---------------------------------------------------------------------------
# The real probes on this host. NOTT runs on Linux and macOS, so these carry no
# platform marker: each must hold on both, against the host's own kernel.
# ---------------------------------------------------------------------------


def test_this_host_gives_a_live_process_an_identity_that_a_bump_does_not_match() -> (
    None
):
    identity = capture_process_identity(os.getpid())
    assert identity.boot_id
    assert process_identity_matches(identity)
    bumped = ProcessIdentity(
        pid=identity.pid, boot_id=identity.boot_id, start_ticks=identity.start_ticks + 1
    )
    assert not process_identity_matches(bumped)
    assert capture_process_identity(os.getpid()) == identity


def test_an_unreaped_child_still_matches_until_it_is_reaped() -> None:
    """`child.poll` sees the exit without reaping and kills the group while the
    zombie pins the pid; the identity must keep matching until the reap."""
    child = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    identity = capture_process_identity(child.pid)
    deadline = time.monotonic() + 10
    while os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is None:
        assert time.monotonic() < deadline, "the child never exited"
        time.sleep(0.01)
    assert process_identity_matches(identity), "a zombie must still match"
    child.wait()
    assert not process_identity_matches(identity), "a reaped child must not match"


def test_group_member_pids_lists_the_leader_and_its_child() -> None:
    leader = subprocess.Popen(
        ["/bin/sh", "-c", "sleep 30 & echo $!; wait"],
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        assert leader.stdout is not None
        sleeper = int(leader.stdout.readline())
        assert {leader.pid, sleeper} <= set(group_member_pids(leader.pid))
        assert os.getpid() not in group_member_pids(leader.pid)
    finally:
        os.killpg(leader.pid, signal.SIGKILL)
        leader.wait()
    assert leader.pid not in group_member_pids(leader.pid)


def test_a_group_of_only_zombies_is_told_apart_from_a_live_one() -> None:
    """macOS answers `killpg` with EPERM both for an all-zombie group and for a
    live member it may not signal; only the first may be read as "nothing alive"."""
    leader = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    deadline = time.monotonic() + 10
    while os.waitid(os.P_PID, leader.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is None:
        assert time.monotonic() < deadline, "the leader never exited"
        time.sleep(0.01)
    assert group_is_only_zombies(leader.pid)
    leader.wait()
    assert not group_is_only_zombies(leader.pid), "an empty group is not all-zombie"
    live = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        assert not group_is_only_zombies(live.pid)
    finally:
        live.kill()
        live.wait()


def _kinfo(pid: int, state: int, flag: int) -> bytes:
    """One 648-byte `kinfo_proc` row with the fields the darwin probes read."""
    row = bytearray(pi._KINFO_PROC_SIZE)
    pi._P_STARTTIME.pack_into(row, 0, 1_790_000_000, 1)
    struct.pack_into("=i", row, 32, flag)  # p_flag
    pi._P_STAT.pack_into(row, pi._P_STAT_OFFSET, state)
    pi._P_PID.pack_into(row, pi._P_PID_OFFSET, pid)
    return bytes(row)


@pytest.mark.parametrize(
    ("rows", "expected"),
    [
        # Measured on macOS 26 (2026-09-25), 1 in 300 group kills: killpg answered
        # EPERM while one member was still SRUN with P_WEXIT (0x2000) set, exiting
        # but not yet a zombie; the kernel does not signal it either.
        ([(10, 5, 0x4004), (11, 2, 0x2004)], True),
        ([(10, 5, 0x4004), (11, 5, 0x2004)], True),
        ([(10, 5, 0x4004), (11, 2, 0x0004)], False),
        ([], False),
    ],
)
def test_on_macos_an_exiting_member_counts_as_no_longer_running(
    monkeypatch: pytest.MonkeyPatch, rows: list[tuple[int, int, int]], expected: bool
) -> None:
    monkeypatch.setattr(pi, "_platform", lambda: "darwin")
    monkeypatch.setattr(
        pi, "_sysctl", lambda mib: b"".join(_kinfo(*row) for row in rows)
    )
    assert group_is_only_zombies(10) is expected


def test_read_stat_returns_none_when_process_exits_during_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _vanished(*_args: object, **_kwargs: object) -> str:
        raise ProcessLookupError(3, "No such process")

    monkeypatch.setattr(Path, "read_text", _vanished)
    assert pi._read_stat(999999) is None
