"""Supervisor  contracts."""

from __future__ import annotations

import contextlib
import os
import signal
import stat
import subprocess
import threading
import time
from pathlib import Path

import pytest
from hypothesis import HealthCheck, example, given, seed, settings
from hypothesis import strategies as st
from supervisor_support import (
    FakeSpawnedChild,
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
    wired_success,
)

from nspawn import claude_child, picker, supervisor
from nspawn import process_identity as pi
from nspawn.owner_roster import Provider
from nspawn.process_identity import ProcessIdentity, capture_process_identity
from nspawn.registry import Registry, WorktreeHeld
from nspawn.supervisor import (
    DuplicateSession,
    InvalidRunRequest,
    RunAccepted,
    RunDirectory,
    Supervisor,
    create_run_directory,
)

# Export imported fixtures so pytest discovers them in this module.
__all__ = [
    "_drain_worker_threads",
    "_stop_every_reaper",
    "fake_picked",
    "fake_provider",
    "registry",
    "roster_home",
    "wired_success",
]


# ---------------------------------------------------------------------------
# create_run_directory
# ---------------------------------------------------------------------------


def test_create_run_directory_is_private_and_names_both_streams(tmp_path: Path) -> None:
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        created = create_run_directory(tmp_path, "run-1", dir_fd=fd)
    finally:
        os.close(fd)

    assert created == RunDirectory(
        root=tmp_path / "run-1",
        stdout=tmp_path / "run-1" / "stdout",
        stderr=tmp_path / "run-1" / "stderr",
    )
    assert stat.S_IMODE(created.root.stat().st_mode) == 0o700


@pytest.mark.parametrize("bad_id", ["", "a/b", "..", ".", "a\x00b"])
def test_create_run_directory_refuses_unsafe_run_id(
    tmp_path: Path, bad_id: str
) -> None:
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(InvalidRunRequest):
            create_run_directory(tmp_path, bad_id, dir_fd=fd)
    finally:
        os.close(fd)


def test_create_run_directory_refuses_a_collision(tmp_path: Path) -> None:
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        create_run_directory(tmp_path, "dup", dir_fd=fd)
        with pytest.raises(supervisor.RunDirectoryError, match="already exists"):
            create_run_directory(tmp_path, "dup", dir_fd=fd)
    finally:
        os.close(fd)


def test_create_run_directory_refuses_a_nondirectory_descriptor(
    tmp_path: Path,
) -> None:
    target = tmp_path / "file"
    target.write_text("x")
    fd = os.open(target, os.O_RDONLY)
    try:
        with pytest.raises(supervisor.RunDirectoryError, match="could not be created"):
            create_run_directory(tmp_path, "run-1", dir_fd=fd)
    finally:
        os.close(fd)


def test_create_run_directory_via_held_descriptor(tmp_path: Path) -> None:
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        created = create_run_directory(tmp_path, "run-1", dir_fd=fd)
    finally:
        os.close(fd)
    assert created.root == tmp_path / "run-1"
    assert stat.S_IMODE(created.root.stat().st_mode) == 0o700


@given(run_id=st.text(min_size=1, max_size=64))
@example(run_id="../escape")
@example(run_id="\x00")
@seed(1729)
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_create_run_directory_never_escapes_run_root(
    run_id: str, tmp_path: Path
) -> None:
    """Fuzz over run_id: either a safe subdirectory is created under run_root,
    or a typed refusal fires — the directory tree never gets a foreign write.

    Each example gets its own fresh root (hashed from run_id) so reusing the
    function-scoped tmp_path across generated examples is safe.
    """
    root = tmp_path / f"fuzz-{hash(run_id) & 0xFFFFFF}"
    root.mkdir(exist_ok=True)
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        created = create_run_directory(root, run_id, dir_fd=fd)
    except InvalidRunRequest:
        return
    except supervisor.RunDirectoryError:
        return
    finally:
        os.close(fd)
    assert created.root.parent == root
    assert created.root.is_relative_to(root)


# ---------------------------------------------------------------------------
# Small pure helpers
# ---------------------------------------------------------------------------


def test_require_text_rejects_blank_and_non_string() -> None:
    supervisor._require_text("x", "ok")
    with pytest.raises(InvalidRunRequest):
        supervisor._require_text("x", "")
    with pytest.raises(InvalidRunRequest):
        supervisor._require_text("x", "   ")
    with pytest.raises(InvalidRunRequest):
        supervisor._require_text("x", 123)  # type: ignore[arg-type]


@given(text=st.text(), max_bytes=st.integers(min_value=0, max_value=4096))
@seed(2027)
@settings(max_examples=200)
def test_bounded_text_never_exceeds_the_byte_cap(text: str, max_bytes: int) -> None:
    result = supervisor._bounded_text(text, max_bytes)
    assert len(result.encode("utf-8")) <= max_bytes


@given(text=st.text(), slack=st.integers(min_value=0, max_value=4096))
@seed(4242)
@settings(max_examples=200)
def test_bounded_text_is_unchanged_when_it_already_fits(text: str, slack: int) -> None:
    """`slack` is added ON TOP of the exact encoded length, so every generated
    example unconditionally lands in the "already fits" branch — no runtime
    `if` needed to select it."""
    max_bytes = len(text.encode("utf-8")) + slack
    assert supervisor._bounded_text(text, max_bytes) == text


def test_bounded_text_does_not_split_a_multibyte_codepoint() -> None:
    # Each "é" is 2 UTF-8 bytes; cap at an odd count to force a boundary cut.
    text = "é" * 10
    result = supervisor._bounded_text(text, 7)
    result.encode("utf-8")  # must not raise
    assert len(result.encode("utf-8")) <= 7


def test_default_review_prompt_names_base_only_when_given() -> None:
    assert "against base" not in supervisor._default_review_prompt("b", None)
    assert "against base main" in supervisor._default_review_prompt("b", "main")


def test_read_roster_refuses_when_nothing_declares_a_roster(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Neither home declares one, so the door refuses and NAMES both.

    The precondition has to be MADE: this host declares `NOTT_NSPAWN_ROSTER`
    in its own login environment, and the version of this test that only
    deleted `HOME` passed for the wrong reason — the old message happened to
    contain the substring "HOME" as part of `NOTT_NSPAWN_HOME`, so the match
    succeeded on a variable name rather than on the behaviour.
    """
    monkeypatch.delenv("NOTT_NSPAWN_ROSTER", raising=False)
    monkeypatch.setenv("NOTT_NSPAWN_CONFIG", str(tmp_path / "absent.toml"))

    with pytest.raises(supervisor.NSpawnError) as refused:
        supervisor._read_roster()
    message = str(refused.value)
    assert "NOTT_NSPAWN_ROSTER" in message
    assert "spawn.toml" in message


def test_read_roster_reads_the_real_file(roster_home: Path) -> None:
    roster = supervisor._read_roster()
    assert roster.defaults.plugin_root is not None


def _linked_worktree(parent: Path, name: str) -> Path:
    """A directory git would call a linked worktree: `.git` is a FILE."""
    root = parent / name
    root.mkdir(parents=True)
    (root / ".git").write_text(
        "gitdir: /somewhere/.git/worktrees/x\n", encoding="utf-8"
    )
    return root


def test_lane_of_names_the_linked_worktree_that_contains_the_cwd(
    tmp_path: Path,
) -> None:
    root = _linked_worktree(tmp_path, "nm-ledger")
    nested = root / "apps" / "spawn"
    nested.mkdir(parents=True)

    assert supervisor._lane_of(str(root)) == "nm-ledger"
    assert supervisor._lane_of(str(nested)) == "nm-ledger"


def test_lane_of_invents_nothing_for_a_main_checkout_or_a_bare_directory(
    tmp_path: Path,
) -> None:
    """The two ways a cwd names no lane: the main checkout (`.git` is a
    DIRECTORY) and a path with no repository above it at all."""
    main = tmp_path / "monorepo"
    (main / ".git").mkdir(parents=True)
    loose = tmp_path / "loose"
    loose.mkdir()

    assert supervisor._lane_of(str(main)) is None
    assert supervisor._lane_of(str(loose)) is None


@given(
    name=st.text(
        alphabet=st.characters(whitelist_categories=("Ll", "Lu", "Nd")),
        min_size=1,
        max_size=24,
    )
)
@seed(90210)
@settings(max_examples=50, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_lane_of_returns_the_worktree_directory_name_and_never_another(
    name: str, tmp_path: Path
) -> None:
    """Fuzz over the directory name: the lane is that exact name, never a
    prefix, a parent, or a value derived from anything else on the path."""
    parent = tmp_path / f"case-{hash(name) & 0xFFFFFF}"
    root = _linked_worktree(parent, name)

    assert supervisor._lane_of(str(root)) == name


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, None), ("", None), ("/opt/claude", Path("/opt/claude"))],
)
def test_claude_bin_override_reads_the_env_var(
    monkeypatch: pytest.MonkeyPatch, value: str | None, expected: Path | None
) -> None:
    if value is None:
        monkeypatch.delenv(supervisor.CLAUDE_BIN_ENV, raising=False)
    else:
        monkeypatch.setenv(supervisor.CLAUDE_BIN_ENV, value)
    assert supervisor._claude_bin_override() == expected


def test_probe_claude_version_parses_real_subprocess_output(tmp_path: Path) -> None:
    script = tmp_path / "claude"
    script.write_text('#!/bin/sh\necho "2.1.220 (Claude Code)"\n', encoding="utf-8")
    script.chmod(0o755)
    version = supervisor._probe_claude_version(str(script))
    assert version == claude_child.Version(2, 1, 220)


def test_probe_claude_version_refuses_unparseable_output(tmp_path: Path) -> None:
    script = tmp_path / "claude"
    script.write_text("#!/bin/sh\necho garbage\n", encoding="utf-8")
    script.chmod(0o755)
    with pytest.raises(supervisor.SupervisorError, match="unparseable"):
        supervisor._probe_claude_version(str(script))


def test_probe_claude_version_refuses_a_missing_binary() -> None:
    with pytest.raises(supervisor.SupervisorError, match="probe failed"):
        supervisor._probe_claude_version("/nonexistent/claude-xyz")


def test_probe_claude_version_refuses_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise_timeout(*a: object, **k: object) -> None:
        raise subprocess.TimeoutExpired(cmd="claude", timeout=1)

    monkeypatch.setattr(supervisor.subprocess, "run", _raise_timeout)
    with pytest.raises(supervisor.SupervisorError, match="probe failed"):
        supervisor._probe_claude_version("/usr/bin/claude")


def test_best_effort_killpg_suppresses_process_lookup_error() -> None:
    supervisor._best_effort_killpg(2**31 - 1)  # must not raise


def test_best_effort_killpg_suppresses_permission_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise_eperm(pid: int, sig: int) -> None:
        raise PermissionError("eperm")

    monkeypatch.setattr(supervisor.os, "killpg", _raise_eperm)
    supervisor._best_effort_killpg(1)  # must not raise


# ---------------------------------------------------------------------------
# /proc(-shaped) parsing and group discovery
# ---------------------------------------------------------------------------


def test_parse_stat_pgrp_reads_the_field_after_the_last_paren() -> None:
    fields = ["S", "1", "77", *[str(n) for n in range(7, 22)], "999", "23"]
    line = "42 (a cmd) with )parens) " + " ".join(fields)
    assert pi.parse_proc_pgrp(line) == 77


@pytest.mark.parametrize(
    "bad",
    ["no parens at all", "42 (cmd)", "42 (cmd) S 1 not-a-number"],
)
def test_parse_stat_pgrp_returns_none_on_malformed_input(bad: str) -> None:
    assert pi.parse_proc_pgrp(bad) is None


@given(text=st.text(max_size=200))
@seed(31337)
@settings(max_examples=500)
def test_parse_stat_pgrp_never_raises_on_arbitrary_bytes(text: str) -> None:
    """Fuzz corpus over untrusted /proc-shaped text: only int|None ever comes back."""
    result = pi.parse_proc_pgrp(text)
    assert result is None or isinstance(result, int)


#: Deterministic, versioned corpus for `/proc/<pid>/stat` — bytes this process
#: never wrote, read from a kernel-controlled file. Fixed strings, not derived
#: from hypothesis's own seed, so a failure here always reproduces byte-for-byte.
STAT_FUZZ_CORPUS: tuple[str, ...] = (
    "",
    "\x00",
    "\x00" * 8192,
    "x" * 8192,
    ")" * 8192,
    "42 (cmd",  # no closing paren at all
    "42 (cmd)",  # closing paren, nothing after
    "42 (cmd) ",  # trailing space, no fields
    "42 (cmd) S",  # one field only
    "42 (cmd) S 1",  # two fields
    "42 (cmd) S 1 not-an-int",  # pgrp field non-numeric
    "42 (cmd) S 1 " + "9" * 4096,  # pgrp field absurdly large
    "42 (cmd) S 1 -77",  # negative pgrp
    "42 (a (nested (parens (in (the (command",
    "42 () S 1 5",  # empty command
    "42 (cmd\nwith\nnewlines) S 1 5",
    "42 (cmd) S 1 5 " + "�" * 64,  # replacement-character noise
    "\xff\xfe" + "42 (cmd) S 1 5",  # leading invalid-looking prefix bytes as str
    "not even proc-shaped at all, just prose",
    "42" + " " * 100000,  # only whitespace after the pid, no parens
)


def test_parse_stat_pgrp_survives_the_versioned_fuzz_corpus() -> None:
    """No entry in the fixed corpus may raise; only int|None is ever returned."""
    for text in STAT_FUZZ_CORPUS:
        result = pi.parse_proc_pgrp(text)
        assert result is None or isinstance(result, int)


def _write_stat(directory: Path, pgrp: int, extra_fields: int = 19) -> None:
    fields = ["S", "1", str(pgrp), *[str(n) for n in range(7, 7 + extra_fields)]]
    (directory / "stat").write_text(
        f"{directory.name} (proc) " + " ".join(fields) + "\n", encoding="utf-8"
    )


@pytest.fixture
def fake_proc_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "proc"
    (root / "sys/kernel/random").mkdir(parents=True)
    (root / "sys/kernel/random/boot_id").write_text("boot-1\n", encoding="utf-8")
    monkeypatch.setattr(pi, "_platform", lambda: "linux")
    monkeypatch.setattr(pi, "PROC_ROOT", root)
    return root


def test_discover_group_members_covers_every_skip_branch(fake_proc_root: Path) -> None:
    leader = ProcessIdentity(pid=100, boot_id="boot-1", start_ticks=1)
    (fake_proc_root / "100").mkdir()
    _write_stat(fake_proc_root / "100", pgrp=100)  # the leader itself: skipped by pid
    (fake_proc_root / "notapid").mkdir()  # non-digit entry: skipped
    (fake_proc_root / "200").mkdir()
    _write_stat(fake_proc_root / "200", pgrp=100)  # real member: included
    (fake_proc_root / "300").mkdir()
    _write_stat(fake_proc_root / "300", pgrp=999)  # different group: skipped
    (fake_proc_root / "400").mkdir()  # stat unreadable (no file at all): skipped
    (fake_proc_root / "500").mkdir()
    (fake_proc_root / "500" / "stat").write_text("garbage no parens", encoding="utf-8")
    # a stat line naming pgrp=100 but too short for capture_process_identity's
    # own start-ticks field -> ProcessIdentityError inside the try, skipped
    (fake_proc_root / "600").mkdir()
    (fake_proc_root / "600" / "stat").write_text("600 (p) S 1 100\n", encoding="utf-8")

    members = supervisor._discover_group_members(leader)

    assert {member.pid for member in members} == {200}


def test_discover_group_members_returns_empty_when_proc_root_unreadable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(pi, "_platform", lambda: "linux")
    monkeypatch.setattr(pi, "PROC_ROOT", tmp_path / "does-not-exist")
    leader = ProcessIdentity(pid=1, boot_id="b", start_ticks=1)
    assert supervisor._discover_group_members(leader) == ()


def test_wait_identity_dead_true_when_already_gone() -> None:
    identity = ProcessIdentity(pid=2**31 - 1, boot_id="impossible-boot", start_ticks=1)
    assert supervisor._wait_identity_dead(identity, 1.0) is True


def test_wait_identity_dead_false_on_timeout_while_alive() -> None:
    proc = _spawn_real("sleep", "5")
    # Reaped when `sleep` ends: an unreaped zombie still matches its identity, so
    # a wait that lost its 0.1 s deadline would otherwise poll a zombie forever.
    threading.Thread(target=proc.wait, daemon=True).start()
    try:
        identity = capture_process_identity(proc.pid)
        started = time.monotonic()
        assert supervisor._wait_identity_dead(identity, 0.1) is False
        assert time.monotonic() - started >= 0.1
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)


def test_wait_identity_dead_true_once_it_dies_mid_poll() -> None:
    proc = _spawn_real("sleep", "5")
    identity = capture_process_identity(proc.pid)

    def _kill_and_reap_shortly() -> None:
        # A killed-but-unreaped child is a zombie: its /proc entry still
        # matches until something waits on it, exactly like the real
        # SpawnedChild.wait() would. Reap it here to simulate that.
        time.sleep(0.05)
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)

    killer = threading.Thread(target=_kill_and_reap_shortly)
    killer.start()
    try:
        assert supervisor._wait_identity_dead(identity, 5.0) is True
    finally:
        killer.join()


def test_wait_identity_dead_treats_a_mid_check_process_identity_error_as_death(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """process_identity_matches can raise ProcessIdentityError for a pid
    that vanishes BETWEEN the /proc directory listing and the stat read
    (ESRCH on read, not ENOENT on open) — a real race, observed directly
    against `close()` under `test_execute_timeout_returns_promptly_once_running`.
    `_wait_identity_dead` must count that as proof of death, not propagate."""

    def _raise(identity: ProcessIdentity) -> bool:
        raise pi.ProcessIdentityError(
            "cannot read process stat: [Errno 3] No such process"
        )

    monkeypatch.setattr(supervisor, "process_identity_matches", _raise)
    identity = ProcessIdentity(pid=1, boot_id="b", start_ticks=1)
    assert supervisor._wait_identity_dead(identity, 5.0) is True


# ---------------------------------------------------------------------------
# Supervisor construction
# ---------------------------------------------------------------------------


def test_init_creates_run_root_privately_and_is_idempotent(
    registry: Registry, tmp_path: Path
) -> None:
    run_root = tmp_path / "runs"
    _sv(registry, tmp_path)
    assert stat.S_IMODE(run_root.stat().st_mode) == 0o700
    _sv(registry, tmp_path)  # exist_ok path, must not raise


def test_init_holds_run_root_directory_descriptor(
    registry: Registry, tmp_path: Path
) -> None:
    sv = _sv(registry, tmp_path)
    fd = sv._run_root_fd
    assert fd is not None
    assert os.path.samestat(os.fstat(fd), (tmp_path / "runs").stat())
    sv.shutdown()
    with pytest.raises(OSError):
        os.fstat(fd)


# ---------------------------------------------------------------------------
# Admission: execute / review
# ---------------------------------------------------------------------------


def test_execute_refuses_blank_prompt_or_cwd(
    registry: Registry, tmp_path: Path, roster_home: Path
) -> None:
    sv = _sv(registry, tmp_path)
    with pytest.raises(InvalidRunRequest):
        sv.execute("", str(tmp_path))
    with pytest.raises(InvalidRunRequest):
        sv.execute("hi", "")


def test_execute_admits_a_run_and_returns_run_accepted(
    registry: Registry, tmp_path: Path, roster_home: Path, wired_success: None
) -> None:
    sv = _sv(registry, tmp_path)
    accepted = sv.execute("hello", str(tmp_path), read_only=True)
    assert isinstance(accepted, RunAccepted)
    row = registry.get_run(accepted.run_id)
    assert row is not None
    assert row.worktree == str(tmp_path)
    assert row.backend.as_str() == "claude"
    assert row.sandbox == "policy-only"


def test_execute_fills_the_lane_from_the_worktree_it_was_dispatched_into(
    registry: Registry, tmp_path: Path, roster_home: Path, wired_success: None
) -> None:
    """F-007: the operator reads a lane name off the row, never a bare UUID."""
    lane_root = _linked_worktree(tmp_path, "nm-ledger")
    sv = _sv(registry, tmp_path)

    accepted = sv.execute("hello", str(lane_root), read_only=True)

    row = registry.get_run(accepted.run_id)
    assert row is not None
    assert row.lane == "nm-ledger"


def test_execute_leaves_the_lane_empty_when_the_cwd_names_none(
    registry: Registry, tmp_path: Path, roster_home: Path, wired_success: None
) -> None:
    sv = _sv(registry, tmp_path)

    accepted = sv.execute("hello", str(tmp_path), read_only=True)

    row = registry.get_run(accepted.run_id)
    assert row is not None
    assert row.lane is None


def test_admission_claim_excludes_second_claim_but_review_does_not(
    registry: Registry, tmp_path: Path, roster_home: Path, wired_success: None
) -> None:
    sv = _sv(registry, tmp_path)
    cwd = str(tmp_path / "wt")
    (tmp_path / "wt").mkdir()
    sv._admit(
        "hello",
        cwd,
        provider=None,
        model=None,
        effort=None,
        execution_class=None,
        session_id=None,
        timeout_ms=0,
        claim_worktree=True,
        read_only=True,
    )
    with pytest.raises(WorktreeHeld):
        sv._admit(
            "second",
            cwd,
            provider=None,
            model=None,
            effort=None,
            execution_class=None,
            session_id=None,
            timeout_ms=0,
            claim_worktree=True,
            read_only=True,
        )  # WorktreeHeld, an execute-claimed worktree

    review_cwd = str(tmp_path / "wt2")
    (tmp_path / "wt2").mkdir()
    sv.review("some-branch", review_cwd)
    sv.review("other-branch", review_cwd)  # no claim taken; a second review is fine


def test_admission_cleans_up_run_dir_on_claim_failure(
    registry: Registry, tmp_path: Path, roster_home: Path, wired_success: None
) -> None:
    sv = _sv(registry, tmp_path)
    cwd = str(tmp_path / "wt")
    (tmp_path / "wt").mkdir()
    sv._admit(
        "hello",
        cwd,
        provider=None,
        model=None,
        effort=None,
        execution_class=None,
        session_id=None,
        timeout_ms=0,
        claim_worktree=True,
        read_only=True,
    )
    before = set((tmp_path / "runs").iterdir())
    with pytest.raises(WorktreeHeld):
        sv._admit(
            "second",
            cwd,
            provider=None,
            model=None,
            effort=None,
            execution_class=None,
            session_id=None,
            timeout_ms=0,
            claim_worktree=True,
            read_only=True,
        )
    after = set((tmp_path / "runs").iterdir())
    assert after == before  # the failed admission's directory was removed


def test_execute_refuses_duplicate_session_id(
    registry: Registry, tmp_path: Path, roster_home: Path, wired_success: None
) -> None:
    sv = _sv(registry, tmp_path)
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    accepted = sv.execute(
        "first", str(tmp_path / "a"), session_id="dup-session", read_only=True
    )
    row = registry.get_run(accepted.run_id)
    assert row is not None
    with pytest.raises(DuplicateSession):
        sv.execute(
            "second", str(tmp_path / "b"), session_id=row.thread_id, read_only=True
        )


def test_execute_propagates_picker_refusals_untouched(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _refuse(*a: object, **k: object) -> None:
        raise supervisor.SupervisorError("no eligible route")

    monkeypatch.setattr(supervisor.picker, "pick_execution", _refuse)
    sv = _sv(registry, tmp_path)
    with pytest.raises(supervisor.SupervisorError, match="no eligible route"):
        sv.execute("hello", str(tmp_path), read_only=True)


def test_review_builds_default_prompt_and_passes_review_class(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    fake_picked: picker.PickerResult,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    def _capture(roster: object, now: int, **kwargs: object) -> picker.PickerResult:
        seen.update(kwargs)
        return fake_picked

    monkeypatch.setattr(supervisor.picker, "pick_execution", _capture)
    _wire_l8_and_l3(monkeypatch, lambda *a, **k: FakeSpawnedChild(_spawn_real("true")))
    monkeypatch.setattr(
        supervisor.terminal, "classify_terminal_run", _classify_by_exit_code
    )
    sv = _sv(registry, tmp_path)
    sv.review("feature-x", str(tmp_path), base="main")
    assert seen["execution_class"] == "review"


def test_review_refuses_blank_branch_or_cwd(registry: Registry, tmp_path: Path) -> None:
    sv = _sv(registry, tmp_path)
    with pytest.raises(InvalidRunRequest):
        sv.review("", str(tmp_path))
    with pytest.raises(InvalidRunRequest):
        sv.review("b", "")


def test_review_refuses_blank_explicit_prompt(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        supervisor.picker, "pick_execution", lambda *a, **k: pytest.fail("unreachable")
    )
    sv = _sv(registry, tmp_path)
    with pytest.raises(InvalidRunRequest):
        sv.review("b", str(tmp_path), prompt="   ")


def test_review_validates_worktree_branch_against_head(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    wired_success: None,
) -> None:
    sv = _sv(registry, tmp_path)

    # 1. Plain non-repo dir -> skip validation
    plain_dir = tmp_path / "plain_dir"
    plain_dir.mkdir()
    res = sv.review("some-branch", str(plain_dir))
    assert res.run_id is not None

    # 2. Real git repository on branch "main"
    repo = tmp_path / "git_repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.invalid"], cwd=repo, check=True
    )
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
    (repo / "f.txt").write_text("hello\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)

    # Match passes
    res_main = sv.review("main", str(repo))
    assert res_main.run_id is not None
    res_refs_main = sv.review("refs/heads/main", str(repo))
    assert res_refs_main.run_id is not None

    # Mismatch raises InvalidRunRequest
    with pytest.raises(InvalidRunRequest, match="worktree is on branch 'main'"):
        sv.review("feature-x", str(repo))

    # 3. Linked worktree on branch "feature-wt"
    wt = tmp_path / "git_wt"
    subprocess.run(
        ["git", "worktree", "add", "-q", str(wt), "-b", "feature-wt"],
        cwd=repo,
        check=True,
    )

    # Match passes on linked worktree
    res_wt = sv.review("feature-wt", str(wt))
    assert res_wt.run_id is not None

    # Mismatch on linked worktree raises InvalidRunRequest
    with pytest.raises(InvalidRunRequest, match="worktree is on branch 'feature-wt'"):
        sv.review("main", str(wt))

    # 4. Detached HEAD -> skip validation
    subprocess.run(["git", "checkout", "-q", "--detach"], cwd=wt, check=True)
    res_detached = sv.review("main", str(wt))
    assert res_detached.run_id is not None


def test_result_show_with_timeout_waits_for_the_answer(
    registry: Registry, tmp_path: Path
) -> None:
    import json
    from dataclasses import replace

    from mcp_fakes import handle
    from supervisor_support import _admit_pending

    from nspawn.mcp import McpServer
    from nspawn.rows import RunPhase

    sv = _sv(registry, tmp_path)
    run_id, _ = _admit_pending(sv, registry, tmp_path, worktree=str(tmp_path))

    def _finish() -> None:
        time.sleep(0.5)
        with sv._lock:
            row = registry.get_run(run_id)
            assert row is not None
            registry.update_run(
                replace(row, phase=RunPhase.COMPLETED, final_text="PONG", updated_at=2)
            )
            sv._lock.notify_all()

    finisher = threading.Thread(target=_finish)
    finisher.start()
    answer = handle(
        McpServer(supervisor=sv),
        {
            "id": 1,
            "params": {
                "name": "nspawn_result_show",
                "arguments": {"run_id": run_id, "timeout_ms": 5000},
            },
        },
    )
    finisher.join()
    assert "error" not in answer
    shown = json.loads(answer["result"]["content"][0]["text"])
    assert shown["final_text"] == "PONG"


def test_execute_schedules_a_background_worker(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    def _fake_drive(self: Supervisor, run_id: str, *a: object, **k: object) -> None:
        seen.append(run_id)

    monkeypatch.setattr(Supervisor, "_drive_run", _fake_drive)
    monkeypatch.setattr(
        supervisor.picker,
        "pick_execution",
        lambda *a, **k: picker.PickerResult(
            requested_provider=None,
            executed_provider=Provider(
                name="acme",
            ),
            executed_account=None,
            model="opus",
            effort=None,
            warning=None,
        ),
    )
    sv = _sv(registry, tmp_path)
    accepted = sv.execute("hello", str(tmp_path), read_only=True)
    deadline = time.monotonic() + 2
    while not seen and time.monotonic() < deadline:
        time.sleep(0.01)
    assert seen == [accepted.run_id]


# --------------------------------------------------------------------------
# The child's inherited environment is a CLOSED allowlist, never the ambient one.
# --------------------------------------------------------------------------


def test_the_child_inherits_the_allowlist_and_nothing_else(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Before `base_environ` was passed the child received `{}`: no `PATH`, so a
    # real executor could not find `bash`; no `HOME`, so it could not read its
    # own `~/.claude`. The falsifier pins BOTH directions, because an allowlist
    # that leaked the ambient environment would satisfy the first half alone.
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("HOME", "/home/someone")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "a-real-looking-secret")
    monkeypatch.delenv("LANG", raising=False)
    monkeypatch.delenv("LC_ALL", raising=False)

    base = supervisor._allowed_base_environ()

    assert base == {"PATH": "/usr/bin", "HOME": "/home/someone"}
    assert "ANTHROPIC_AUTH_TOKEN" not in base


def test_an_absent_allowlist_name_is_omitted_never_empty_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An empty string is a failure sentinel: `PATH=""` means "search nothing"
    # and would look identical to "inherited". Absent stays absent.
    for name in supervisor.CHILD_ENV_ALLOWLIST:
        monkeypatch.delenv(name, raising=False)

    assert supervisor._allowed_base_environ() == {}
