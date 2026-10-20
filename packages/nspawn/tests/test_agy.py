"""Native AGY turns through real subprocess pipes and bounded output custody."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

import nspawn.agy as agy_module
import nspawn.child as child_module
from nspawn import native
from nspawn.agy import AgyError, AgyTurn
from nspawn.limits import MAX_DURABLE_IDENTIFIER_BYTES, MAX_DURABLE_OBSERVATION_BYTES
from nspawn.owner_roster import NativeAccount
from nspawn.process_identity import (
    ProcessIdentityError,
    group_is_only_zombies,
    group_member_pids,
)
from nspawn.pump import MAX_OUTPUT_LINE_BYTES, PumpLimitExceeded
from nspawn.redact import REDACTION_MARKER, SecretNeedles
from nspawn.rows import NativeSessionContext


def _frames(status: str = "SUCCESS", identity: str = "conversation-one") -> list[dict]:
    return [
        {"event": "init", "conversation_id": identity, "init": {"tools": []}},
        {
            "event": "result",
            "result": {
                "conversation_id": identity,
                "status": status,
                "response": "PONG" if status == "SUCCESS" else "",
                "error": None if status == "SUCCESS" else "provider unavailable",
                "duration_seconds": 0.125,
                "num_turns": 1,
                "usage": {"input_tokens": 2, "output_tokens": 1},
            },
        },
    ]


def _argv(frames: list[dict], *, exit_code: int = 0, extra: str = "") -> list[str]:
    script = (
        "import json,sys,time\n"
        "message=json.loads(sys.stdin.readline())\n"
        "assert message == {'event':'user','message':{'content':'PONG'}}\n"
        "assert sys.stdin.read() == ''\n"
        f"frames=json.loads({json.dumps(frames)!r})\n"
        "for frame in frames:\n"
        "    if 'sleep' in frame: time.sleep(frame['sleep'])\n"
        "    else: print(json.dumps(frame),flush=True)\n"
        f"{extra}\n"
        f"sys.exit({exit_code})\n"
    )
    return [sys.executable, "-u", "-c", script]


def _turn(tmp_path: Path, argv: list[str], needles: SecretNeedles | None = None):
    return AgyTurn.start(
        argv,
        cwd=tmp_path,
        env=dict(os.environ),
        run_dir=tmp_path,
        needles=needles or SecretNeedles.none(),
    )


def _kill_leftover(turn: AgyTurn | None) -> bool:
    """Kill and reap what the product's reap left, without the code under test.

    Answers whether anything was left. A mutant or a failed reap otherwise
    leaves the child's session behind, stopped, after this test process exits.
    Only an unreaped leader is signaled: its pid still pins the group id, so no
    reused group is ever reached.
    """
    if turn is None or turn.child._process.returncode is not None:
        return False
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(turn.child.identity.pid, signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):
        turn.child._process.wait(timeout=5)
    return True


@contextmanager
def _start(
    tmp_path: Path, argv: list[str], needles: SecretNeedles | None = None
) -> Iterator[AgyTurn]:
    """The product's turn, whose exit must leave its child reaped."""
    turn = None
    try:
        with _turn(tmp_path, argv, needles) as turn:
            yield turn
    finally:
        # Cleaned up first, so a failing test leaves no orphan; then the leak
        # fails the test (an exception already in flight stays its context).
        if _kill_leftover(turn):
            pytest.fail("the AGY turn exited and left its child unreaped")


def test_success_sends_one_native_envelope_and_reports_identity_on_caller(tmp_path):
    seen = []
    with _start(tmp_path, _argv(_frames())) as turn:
        result = turn.run(
            "PONG",
            timeout=2,
            on_thread=lambda identity: seen.append((identity, threading.get_ident())),
        )
        assert turn.child.poll() == 0
    assert seen == [("conversation-one", threading.get_ident())]
    assert result.conversation_id == "conversation-one"
    assert result.status == "SUCCESS"
    assert result.final_text == "PONG"
    assert result.error is None
    assert result.usage == {"input_tokens": 2, "output_tokens": 1}
    assert result.duration_seconds == 0.125
    assert [
        json.loads(line)["event"]
        for line in (tmp_path / "stdout").read_text().splitlines()
    ] == ["init", "result"]


def test_provider_error_is_a_native_error_result(tmp_path):
    with _start(tmp_path, _argv(_frames("ERROR"), exit_code=1)) as turn:
        result = turn.run("PONG", timeout=2)
    assert result.status == "ERROR"
    assert result.error == "provider unavailable"


@pytest.mark.parametrize("replacement", ["regular", "symlink", "fifo"])
def test_confined_stderr_replacement_cannot_forge_refusal_evidence(
    tmp_path, replacement
):
    home, checkout = tmp_path / "home", tmp_path / "checkout"
    home.mkdir(mode=0o750)
    checkout.mkdir()
    secret = "known-private-credential-0123456789"
    unknown = "unknown-private-credential-9876543210"
    injected = f"filesystem-only-marker {secret} {unknown}"
    target = checkout / "injected-stderr"
    target.write_text(injected)
    # Escape the pipe credential so the retained evidence needs decoded redaction.
    pipe = json.dumps({"message": f"pipe-vendor-marker {secret}"}) + "\n"
    pipe = pipe.replace(secret, "".join(f"\\u{ord(c):04x}" for c in secret))
    frame = {"event": "init", "conversation_id": None, "error": "vendor-frame-marker"}
    peer = tmp_path / "agy-peer.py"
    peer.write_text(
        "import json,os,sys,time\nfrom pathlib import Path\n"
        "sys.stdin.read()\n"
        f"sys.stderr.write({pipe!r}); sys.stderr.flush()\n"
        "path=Path(os.environ['TMPDIR']).parent/'stderr'\n"
        "deadline=time.monotonic()+3\n"
        "while True:\n"
        " try: stored=path.read_text()\n"
        " except FileNotFoundError: stored=''\n"
        f" if 'pipe-vendor-marker' in stored and {REDACTION_MARKER!r} in stored: break\n"
        " assert time.monotonic()<deadline, 'owning pipe was not persisted'\n"
        " time.sleep(0.005)\n"
        "path.unlink()\n"
        + (
            f"path.write_text({injected!r}); path.chmod(0o600)\n"
            if replacement == "regular"
            else f"path.symlink_to({str(target)!r})\n"
            if replacement == "symlink"
            else "os.mkfifo(path,0o600)\n"
        )
        + f"print(json.dumps({frame!r}),flush=True)\n"
    )
    launch = native.NativeLaunch(
        (str(Path("/usr/bin/python3").resolve()), str(peer)),
        {"HOME": str(home), "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
        SecretNeedles((secret,)),
        NativeSessionContext(str(home), read_roots=(str(peer),)),
        NativeAccount(home=home),
    )
    turn = None
    threads = []
    with tempfile.TemporaryDirectory(prefix="turn-", dir=tmp_path) as directory:
        run_dir = Path(directory)
        confined = native.agy_confined(
            launch, cwd=checkout, run_dir=run_dir, writable=True
        )
        try:
            with (
                AgyTurn.start(
                    confined.argv,
                    cwd=checkout,
                    env=confined.env,
                    run_dir=run_dir,
                    needles=launch.needles,
                ) as turn,
                pytest.raises(
                    AgyError, match="AGY conversation identity is missing or invalid"
                ) as refused,
            ):
                turn.run("PONG", timeout=5, on_thread=threads.append)
            _assert_reaped(turn)
            assert (run_dir / "stderr").lstat().st_uid == os.geteuid()
            if replacement == "regular":
                assert (run_dir / "stderr").read_text() == injected
            elif replacement == "symlink":
                assert (run_dir / "stderr").is_symlink()
            else:
                assert stat.S_ISFIFO((run_dir / "stderr").lstat().st_mode)
        finally:
            if _kill_leftover(turn):
                pytest.fail("the confined AGY peer was left unreaped")
    assert not run_dir.exists() and threads == []
    message = str(refused.value)
    assert refused.value.entry.code == 2003
    assert "filesystem-only-marker" not in message
    assert secret not in message and unknown not in message
    assert "pipe-vendor-marker" in message and REDACTION_MARKER in message
    assert "vendor-frame-marker" in message
    assert len(message.encode("utf-8")) <= MAX_DURABLE_OBSERVATION_BYTES


@pytest.mark.skipif(
    not Path("/etc/ca-certificates").is_dir(),
    reason="this host keeps no CA tree under /etc/ca-certificates",
)
def test_confined_launch_mounts_the_ca_tree_the_cert_links_point_into(tmp_path):
    """On Arch every /etc/ssl/certs entry links into /etc/ca-certificates. With
    only /etc/ssl/certs mounted the links dangle and TLS has no roots (x509:
    certificate signed by unknown authority, every AGY run since 2026-10-12)."""
    home, checkout = tmp_path / "home", tmp_path / "checkout"
    home.mkdir(mode=0o750)
    checkout.mkdir()
    launch = native.NativeLaunch(
        (str(Path(sys.executable).resolve()),),
        {"HOME": str(home), "PATH": "/usr/bin:/bin"},
        SecretNeedles.none(),
        NativeSessionContext(str(home)),
        NativeAccount(home=home),
    )
    with tempfile.TemporaryDirectory(prefix="turn-", dir=tmp_path) as directory:
        argv = native.agy_confined(
            launch, cwd=checkout, run_dir=Path(directory), writable=False
        ).argv
    binds = {argv[i + 1]: argv[i] for i in range(len(argv) - 2) if "bind" in argv[i]}
    assert binds.get("/etc/ssl/certs") == "--ro-bind"
    assert binds.get("/etc/ca-certificates") == "--ro-bind"


@pytest.mark.parametrize(
    "case",
    [
        "foreign-result",
        "foreign-step",
        "step-without-identity",
        "foreign-resume",
        "no-init",
        "duplicate-init",
        "no-result",
        "duplicate-result",
        "unknown-status",
        "bad-usage",
        "bad-duration",
        "success-exit-one",
        "malformed",
        "too-long",
        "deadline",
    ],
)
def test_bad_protocol_and_deadlines_refuse_and_reap(tmp_path, case):
    frames = _frames()
    extra = ""
    resume = None
    exit_code = 0
    if case == "foreign-result":
        frames[1]["result"]["conversation_id"] = "other-conversation"
    elif case in ("foreign-step", "step-without-identity"):
        update = (
            {"conversation_id": "other-conversation"} if case == "foreign-step" else {}
        )
        frames.insert(1, {"event": "step_update", "step_update": update})
    elif case == "foreign-resume":
        resume = "other-conversation"
    elif case == "no-init":
        frames = frames[1:]
    elif case == "duplicate-init":
        frames.insert(1, frames[0])
    elif case == "no-result":
        frames = frames[:1]
    elif case == "duplicate-result":
        frames.append(frames[1])
    elif case == "unknown-status":
        frames[1]["result"]["status"] = "MAYBE"
    elif case == "bad-usage":
        frames[1]["result"]["usage"] = {"input_tokens": True}
    elif case == "bad-duration":
        frames[1]["result"]["duration_seconds"] = float("nan")
    elif case == "success-exit-one":
        exit_code = 1
    elif case == "malformed":
        frames = []
        extra = "print('{broken-json',flush=True)"
    elif case == "too-long":
        frames = []
        extra = "print('x' * (1024*1024+1),flush=True)"
    elif case == "deadline":
        frames = frames[:1]
        extra = "time.sleep(20)"
    with _start(tmp_path, _argv(frames, exit_code=exit_code, extra=extra)) as turn:
        started = time.monotonic()
        with pytest.raises(AgyError):
            turn.run("PONG", timeout=0.4, conversation_id=resume)
        assert turn.child.poll() is not None
        assert time.monotonic() - started < 3


def test_step_before_init_refuses_with_exact_reason(tmp_path):
    frames = [
        {"event": "step_update", "step_update": {"conversation_id": "conv-1"}},
        {
            "event": "result",
            "result": {"conversation_id": "conv-1", "status": "COMPLETED"},
        },
    ]
    # The child emits its frames and closes stdout before it reads the
    # prompt, and the turn starts only once the stdout pump has finished
    # (refusal or EOF, so an accepting protocol cannot hang this wait). The
    # refusal then does not race the 0.4 s turn deadline against interpreter
    # start-up, which under host load exceeded it and raised the deadline
    # error instead of this reason.
    script = (
        "import json,sys\n"
        f"for frame in {frames!r}: print(json.dumps(frame),flush=True)\n"
        "sys.stdout.close()\n"
        "sys.stdin.read()\n"
    )
    with _start(tmp_path, [sys.executable, "-u", "-c", script]) as turn:
        pump = turn.child.stdout_pump
        while not pump.finished:
            time.sleep(0.01)
        with pytest.raises(AgyError, match="AGY emitted a step before initialization"):
            turn.run("PONG", timeout=0.4)


@pytest.mark.parametrize("case", ["malformed", "oversized"])
def test_unreadable_event_keeps_its_refusal_without_identity_evidence(tmp_path, case):
    extra = (
        "print('{broken-json',flush=True)"
        if case == "malformed"
        else f"print('x' * {MAX_OUTPUT_LINE_BYTES + 1},flush=True)"
    )
    reason = (
        "AGY emitted malformed JSON"
        if case == "malformed"
        else "AGY child transport or output failed"
    )
    with _start(tmp_path, _argv([], extra=extra)) as turn:
        with pytest.raises(AgyError, match=reason) as refused:
            turn.run("PONG", timeout=2)
        assert turn.child.poll() is not None
    assert refused.value.entry.code == 2003
    assert "AGY event excerpt" not in str(refused.value)
    if case == "oversized":
        assert isinstance(refused.value.__cause__, PumpLimitExceeded)
        assert (tmp_path / "stdout").read_bytes() == b""
    _assert_reaped(turn)


def _deltas(frames: list[dict], *deltas: str, identity: str = "conversation-one"):
    for delta in deltas:
        frames.insert(
            -1,
            {
                "event": "step_update",
                "step_update": {"conversation_id": identity, "text_delta": delta},
            },
        )
    return frames


def _streamed(tmp_path, frames, secret: str, seen=None, **run) -> list[str]:
    seen = [] if seen is None else seen
    with _start(tmp_path, _argv(frames), SecretNeedles((secret,))) as turn:
        turn.run("PONG", timeout=2, on_text=seen.append, **run)
    return seen


def test_streamed_text_is_redacted_across_deltas(tmp_path):
    # A needle split across deltas is redacted: no part of it ever leaves.
    secret = "private-credential-0123456789"
    frames = _deltas(
        _frames(),
        "Hello ",
        "wor",
        "ld.\nThe key is private-",
        "credential-0123456789\ntail",
    )
    seen = _streamed(tmp_path, frames, secret)
    assert "".join(seen) == f"Hello world.\nThe key is {REDACTION_MARKER}\ntail", seen


def test_a_multiline_needle_split_across_lines_never_leaves(tmp_path):
    secret = "first-secret-line\nsecond-secret-line"
    # The second line arrives after the first has had time to leave.
    frames = _deltas(_frames(), "Key: first-secret-line\n")
    frames.insert(-1, {"sleep": 0.3})
    frames = _deltas(frames, "second-secret-line\nafter\n")
    seen = "".join(_streamed(tmp_path, frames, secret))
    assert "first-secret" not in seen and "second-secret" not in seen, seen
    assert seen == f"Key: {REDACTION_MARKER}\nafter\n", seen


def test_a_needle_start_is_held_in_linear_time():
    # Only a tail that could begin a needle waits; finding it copies no tails.
    needles = SecretNeedles(("ab" * 262144,))
    assert needles.releasable("c" + "ab" * 1000) == 1
    assert needles.releasable("abc") == 3
    began = time.monotonic()
    assert needles.releasable("x" * 524288) == 524288
    assert time.monotonic() - began < 1, time.monotonic() - began


def test_a_foreign_conversations_text_never_leaves(tmp_path):
    frames = _deltas(_frames(identity="other"), "foreign reply\n", identity="other")
    frames.insert(-1, {"sleep": 0.3})  # the text has time to leave before the result
    seen: list[str] = []
    with pytest.raises(AgyError, match="different resumed conversation"):
        _streamed(tmp_path, frames, "unused", seen, conversation_id="conversation-one")
    assert seen == []


def test_decoded_json_secrets_are_redacted_before_persistence(tmp_path):
    secret = "private-credential-0123456789"
    frames = _frames()
    frames.insert(
        1,
        {
            "event": "step_update",
            "step_update": {
                "conversation_id": "conversation-one",
                "data": {secret: [secret]},
            },
        },
    )
    frames[-1]["result"]["response"] = secret
    script = (
        "import sys\n"
        "sys.stdin.read()\n"
        f"sys.stdout.write({''.join(json.dumps(frame) + chr(10) for frame in frames).replace(secret, ''.join(f'\\u{ord(c):04x}' for c in secret))!r})\n"
    )
    with _start(
        tmp_path, [sys.executable, "-c", script], SecretNeedles((secret,))
    ) as turn:
        result = turn.run("PONG", timeout=2)
    assert result.final_text == REDACTION_MARKER
    stored = (tmp_path / "stdout").read_text()
    decoded = [json.loads(line) for line in stored.splitlines()]
    assert secret not in json.dumps(decoded)
    assert decoded[1]["step_update"]["data"] == {REDACTION_MARKER: [REDACTION_MARKER]}


def test_context_exit_without_run_reaps_child(tmp_path):
    with _start(tmp_path, [sys.executable, "-c", "import time;time.sleep(20)"]) as turn:
        assert turn.child.poll() is None
    assert turn.child.poll() is not None


def test_stdout_eof_without_terminal_refuses_before_a_live_child_deadline(tmp_path):
    argv = _argv(_frames()[:1], extra="import os;os.close(1);time.sleep(20)")
    with _start(tmp_path, argv) as turn:
        started = time.monotonic()
        with pytest.raises(AgyError, match="EOF"):
            turn.run("PONG", timeout=2)
        assert time.monotonic() - started < 1
        assert turn.child.poll() is not None


@pytest.mark.parametrize("timeout", [-1, True, float("nan"), float("inf"), 10**1000])
def test_invalid_deadline_refuses_and_reaps(tmp_path, timeout):
    with _start(tmp_path, _argv(_frames())) as turn:
        with pytest.raises(AgyError, match="deadline"):
            turn.run("PONG", timeout=timeout)
        assert turn.child.poll() is not None


class _Alarm(Exception):
    """What a pytest-timeout alarm or Ctrl-C raises at the next bytecode."""


_SLEEPER = [sys.executable, "-c", "import time;time.sleep(20)"]


def _group_alive(pgid: int) -> bool:
    return bool(group_member_pids(pgid)) and not group_is_only_zombies(pgid)


def _group_survives(pgid: int) -> bool:
    deadline = time.monotonic() + 2
    while _group_alive(pgid):
        if time.monotonic() > deadline:
            return True
        time.sleep(0.01)
    return False


def _assert_reaped(turn: AgyTurn) -> None:
    """The leader was waited (its status read) and nothing, not even a zombie,
    is left in its group."""
    assert turn.child._process.returncode is not None
    assert group_member_pids(turn.child.identity.pid) == ()


@pytest.mark.parametrize("path", ["invalid-deadline", "deadline", "exit-without-run"])
def test_an_interrupt_after_the_group_stop_still_kills_and_reaps(
    tmp_path, monkeypatch, path
):
    # Four `python` children of the invalid-deadline test sat stopped (state T)
    # for days: the group got its SIGSTOP, the SIGKILL never came, and nothing
    # after the reap looked again. One exception right after the stop (as a
    # timeout alarm raises) must neither leave the group frozen nor the leader
    # unreaped, on each path that reaps. The child sleeps through stdin EOF, so
    # it is alive when it is stopped: no case passes on an exit of its own.
    real_killpg = os.killpg
    stopped_alive = []

    def stop_then_alarm(pgid: int, sig: int) -> None:
        first_stop = sig == signal.SIGSTOP and not stopped_alive
        if first_stop:
            stopped_alive.append(_group_alive(pgid))
        real_killpg(pgid, sig)
        if first_stop:
            raise _Alarm

    turn = None
    try:
        with pytest.raises(_Alarm), _turn(tmp_path, _SLEEPER) as turn:
            monkeypatch.setattr(child_module.os, "killpg", stop_then_alarm)
            if path != "exit-without-run":
                turn.run("PONG", timeout=-1 if path == "invalid-deadline" else 0.3)
        assert stopped_alive == [True]
        assert not _group_survives(turn.child.identity.pid)
        _assert_reaped(turn)
    finally:
        monkeypatch.undo()
        _kill_leftover(turn)


def test_a_signal_landing_before_the_group_kill_still_kills_and_reaps(
    tmp_path, monkeypatch
):
    # A real signal handler raising on the main thread inside the stop-to-kill
    # window, on the turn's last reap: Python runs it at the next bytecode of
    # whatever the main thread executes. The kill must be sent before the
    # interrupt leaves the stop, not only by the turn's second reap.
    real_killpg = os.killpg
    main = threading.main_thread().ident
    assert main is not None
    raised = []
    events = []

    def interrupt(_signum, _frame) -> None:
        if not raised:
            raised.append(True)
            raise _Alarm

    def signal_before_the_kill(pgid: int, sig: int) -> None:
        if sig == signal.SIGSTOP and not events:
            events.append(("stop", _group_alive(pgid)))
        if sig == signal.SIGKILL and not raised:
            if threading.get_ident() != main:
                time.sleep(0.1)  # the caller has reached its wait for this thread
            signal.pthread_kill(main, signal.SIGUSR1)
            time.sleep(0.2)
        real_killpg(pgid, sig)
        if sig == signal.SIGKILL:
            events.append(("kill",))

    turn = None
    previous = signal.signal(signal.SIGUSR1, interrupt)
    try:
        with pytest.raises(_Alarm), _turn(tmp_path, _SLEEPER) as turn:
            real_stop_then_kill = turn.child.stop_then_kill

            def recorded_stop_then_kill(expected):
                try:
                    real_stop_then_kill(expected)
                except BaseException:
                    events.append(("escaped",))
                    raise

            monkeypatch.setattr(turn.child, "stop_then_kill", recorded_stop_then_kill)
            monkeypatch.setattr(child_module.os, "killpg", signal_before_the_kill)
        assert raised
        assert events[:3] == [("stop", True), ("kill",), ("escaped",)]
        assert not _group_survives(turn.child.identity.pid)
        _assert_reaped(turn)
    finally:
        monkeypatch.undo()
        signal.signal(signal.SIGUSR1, previous)
        _kill_leftover(turn)


def test_a_reap_refused_before_the_group_stop_is_retried_on_exit(tmp_path, monkeypatch):
    # A reap that raised before any signal (a transient identity recheck
    # failure here) left a live child: the turn marked itself closed first, so
    # its context exit skipped the second reap.
    real_matches = child_module.process_identity_matches
    refused = []

    def refuse_once(identity):
        if not refused:
            refused.append(identity)
            raise ProcessIdentityError("transient /proc read failure")
        return real_matches(identity)

    monkeypatch.setattr(child_module, "process_identity_matches", refuse_once)
    turn = None
    try:
        with (
            pytest.raises(AgyError),
            _turn(tmp_path, _SLEEPER) as turn,
        ):
            turn.run("PONG", timeout=-1)
        assert refused
        assert not _group_survives(turn.child.identity.pid)
        _assert_reaped(turn)
    finally:
        _kill_leftover(turn)


def test_input_write_deadline_reaps_a_child_that_never_reads(tmp_path):
    with _start(tmp_path, [sys.executable, "-c", "import time;time.sleep(20)"]) as turn:
        with pytest.raises(AgyError):
            turn.run("x" * 500_000, timeout=0.1)
        assert turn.child.poll() is not None


def test_prompt_limit_refuses_before_sending(tmp_path):
    with _start(tmp_path, _argv(_frames())) as turn:
        with pytest.raises(AgyError, match="input exceeds"):
            turn.run("x" * MAX_OUTPUT_LINE_BYTES, timeout=2)
        assert turn.child.poll() is not None


def test_second_run_is_refused(tmp_path):
    with _start(tmp_path, _argv(_frames())) as turn:
        turn.run("PONG", timeout=2)
        with pytest.raises(AgyError, match="exactly one"):
            turn.run("PONG", timeout=2)


@pytest.mark.parametrize("timeout", [None, 0])
def test_absent_controller_deadline_does_not_invent_five_minutes(
    tmp_path, monkeypatch, timeout
):
    clock = iter([100.0, 1000.0])
    monkeypatch.setattr(
        agy_module, "time", SimpleNamespace(monotonic=lambda: next(clock, 1000.0))
    )
    with _start(tmp_path, _argv(_frames())) as turn:
        assert turn.run("PONG", timeout=timeout).final_text == "PONG"


@pytest.mark.parametrize(
    "identity",
    [
        "é" * (MAX_DURABLE_IDENTIFIER_BYTES // 2 + 1),
        " bad",
        "bad\n",
        "bad\x00",
        "bad\x85",
        "bad\u200b",
        "bad\ud800",
    ],
)
def test_identity_uses_durable_utf8_bytes_and_control_rules(tmp_path, identity):
    with (
        _start(tmp_path, _argv(_frames(identity=identity))) as turn,
        pytest.raises(AgyError),
    ):
        turn.run("PONG", timeout=2)


def test_durable_identity_byte_boundary_is_accepted(tmp_path):
    identity = "é" * (MAX_DURABLE_IDENTIFIER_BYTES // 2)
    with _start(tmp_path, _argv(_frames(identity=identity))) as turn:
        assert turn.run("PONG", timeout=2).conversation_id == identity


def test_utf8_stdin_does_not_inflate_a_valid_prompt_past_the_byte_limit(tmp_path):
    script = (
        "import json,sys\n"
        "raw=sys.stdin.buffer.read()\n"
        "assert '汉'.encode() in raw\n"
        "assert json.loads(raw)['message']['content'] == '汉' * 200_000\n"
        f"frames=json.loads({json.dumps(_frames())!r})\n"
        "for frame in frames: print(json.dumps(frame),flush=True)\n"
    )
    with _start(tmp_path, [sys.executable, "-u", "-c", script]) as turn:
        assert turn.run("汉" * 200_000, timeout=2).final_text == "PONG"


def test_native_input_requires_event_even_after_successful_init(tmp_path):
    script = (
        "import json,sys\n"
        f"frames=json.loads({json.dumps(_frames())!r})\n"
        "print(json.dumps(frames[0]),flush=True)\n"
        "message=json.loads(sys.stdin.readline())\n"
        "if message != {'event':'user','message':{'content':'PONG'}}:\n"
        " frames[1]['result'].update(status='ERROR',response='',error='stream input message is missing the \"event\" field')\n"
        "print(json.dumps(frames[1]),flush=True)\n"
        "sys.exit(0 if frames[1]['result']['status']=='SUCCESS' else 1)\n"
    )
    with _start(tmp_path, [sys.executable, "-u", "-c", script]) as turn:
        assert turn.run("PONG", timeout=2).status == "SUCCESS"


@pytest.mark.parametrize("turn_index", [0, 1])
def test_observed_native_fixture_preserves_success_and_denial(tmp_path, turn_index):
    fixture = Path(__file__).parent / "fixtures/agy-stream-wire-1.1.27.ndjson"
    metadata = json.loads(fixture.with_suffix(".metadata.json").read_text())
    assert (
        hashlib.sha256(fixture.read_bytes()).hexdigest() == metadata["fixture_sha256"]
    )
    assert metadata["binary"]["version"] == "1.1.27"
    assert len(metadata["binary"]["sha256"]) == 64
    assert metadata["source_commands"][0]["stdin"] == {
        "event": "user",
        "message": {"content": "PONG"},
    }
    turns = []
    for line in fixture.read_text().splitlines():
        frame = json.loads(line)
        if frame["event"] == "init":
            turns.append([])
        turns[-1].append(frame)
    outcome = metadata["outcomes"][turn_index]
    with _start(
        tmp_path, _argv(turns[turn_index], exit_code=outcome["exit_code"])
    ) as turn:
        if turn_index == 0:
            assert outcome["status"] == "CANCELED"
            assert outcome["exit_code"] == 0
            result = turn.run("PONG", timeout=2)
            assert result.status == "CANCELED"
            assert result.usage == outcome["usage"]
        else:
            result = turn.run("PONG", timeout=2, conversation_id="<CONVERSATION_ID>")
            assert result.status == "SUCCESS"
            assert result.final_text.strip() == "PONG"
            assert result.usage == outcome["usage"]
            assert outcome["same_conversation"] is True


@pytest.mark.parametrize("both_pumps", [False, True], ids=["stdout", "both-pumps"])
def test_a_refused_stdout_line_surfaces_its_own_error_not_eof(tmp_path, both_pumps):
    from nspawn.native import NativePreparationError

    class RefreshRefused:
        """Needles whose credential refresh refuses on the first decoded frame."""

        def __init__(self) -> None:
            self._inner = SecretNeedles.none()

        def __getattr__(self, name: str):
            return getattr(self._inner, name)

        def redact_value(self, value):
            if isinstance(value, dict) and value.get("event") == "init":
                raise NativePreparationError("synthetic refresh refusal")
            return self._inner.redact_value(value)

    # With both_pumps the stderr pump fails too (a line over its ceiling),
    # after the stdout refusal already reached the turn and before the reap:
    # the reap must not bury that refusal under the stderr one raised over it.
    extra = (
        f"time.sleep(0.3); sys.stderr.write('x' * {MAX_OUTPUT_LINE_BYTES + 8} + '\\n');"
        " sys.stderr.flush(); time.sleep(5)"
        if both_pumps
        else ""
    )
    refused = pytest.raises(NativePreparationError, match="synthetic refresh refusal")
    with (
        refused,
        _start(
            tmp_path, _argv(_frames()[:1], extra=extra), needles=RefreshRefused()
        ) as turn,
    ):
        if both_pumps:
            joins = turn._join_pumps

            def join_then_let_stderr_fail() -> None:
                try:
                    joins()
                except NativePreparationError:
                    stderr = turn.child.stderr_pump
                    deadline = time.monotonic() + 5
                    while not stderr.finished:
                        assert time.monotonic() < deadline
                        time.sleep(0.005)
                    raise

            turn._join_pumps = join_then_let_stderr_fail
        turn.run("PONG", timeout=5)


class _RefusedAfterTheLoopLooked:
    """Needles that refuse one frame, but only once the turn loop has joined
    its pumps cleanly: the refusal lands after the loop-top join and before
    the EOF or exit verdict, the window a scheduler otherwise only sometimes
    opens."""

    def __init__(self, event: str) -> None:
        self._inner = SecretNeedles.none()
        self._event = event
        self.looked = threading.Event()

    def __getattr__(self, name: str):
        return getattr(self._inner, name)

    def redact_value(self, value):
        from nspawn.native import NativePreparationError

        if isinstance(value, dict) and value.get("event") == self._event:
            assert self.looked.wait(5)
            raise NativePreparationError("synthetic late refusal")
        return self._inner.redact_value(value)


def _hold_after_first_clean_join(turn, needles: _RefusedAfterTheLoopLooked) -> None:
    joins = turn._join_pumps

    def join_then_hold() -> None:
        joins()
        if needles.looked.is_set():
            return
        needles.looked.set()
        pumps = (turn.child.stdout_pump, turn.child.stderr_pump)
        deadline = time.monotonic() + 5
        while not (turn.child.poll() is not None and all(p.finished for p in pumps)):
            assert time.monotonic() < deadline
            time.sleep(0.005)

    turn._join_pumps = join_then_hold


@pytest.mark.parametrize(
    ("frames", "event"),
    [
        # The refused frame carried the result: the child exits with its
        # terminal recorded, and the exit verdict must read the refusal.
        (_frames(), "result"),
        # The refused frame was the only one: stdout reaches EOF with no
        # terminal, and the EOF verdict must read the refusal.
        (_frames()[:1], "init"),
    ],
)
def test_a_refusal_on_the_last_frame_before_exit_surfaces_as_itself(
    tmp_path, frames, event
):
    from nspawn.native import NativePreparationError

    needles = _RefusedAfterTheLoopLooked(event)
    refused = pytest.raises(NativePreparationError, match="synthetic late refusal")
    with refused, _start(tmp_path, _argv(frames), needles=needles) as turn:
        _hold_after_first_clean_join(turn, needles)
        turn.run("PONG", timeout=5)
    assert needles.looked.is_set()


#: AGY's own refusal, as `cli-20261006_120733.log` recorded it.
EXHAUSTED = (
    "generating and executing: RESOURCE_EXHAUSTED (code 429): Individual quota "
    "reached. Please upgrade your subscription to increase your limits. "
    "Resets in 126h54m6s."
)


#: The same refusal as the live AGY CLI gave it on 2026-10-20, without its code.
LIVE = (
    "Individual quota reached. Please upgrade your subscription to increase your "
    "limits. Resets in 85h10m7s."
)


@pytest.mark.parametrize(
    ("error", "wait"),
    [
        (EXHAUSTED, 126 * 3600 + 54 * 60 + 6),
        (EXHAUSTED.replace("126h54m6s", "2h28m55s"), 2 * 3600 + 28 * 60 + 55),
        (EXHAUSTED.replace("126h54m6s", "45s"), 45),
        (EXHAUSTED.replace("Resets in 126h54m6s.", ""), None),
        ("RESOURCE_EXHAUSTED (code 429): Resets in soon", None),
        ("RESOURCE_EXHAUSTED (code 429): Resets in 5 days", None),
        ("UNAVAILABLE (code 503): Resets in 1h", None),
        (EXHAUSTED.replace("126h54m6s.", "1h30m"), 3600 + 30 * 60),
        ("NOT_RESOURCE_EXHAUSTED (code 503): quota. Resets in 45s.", None),
        ("RESOURCE_EXHAUSTED (code 429): Too many requests. Resets in 45s.", None),
        (EXHAUSTED.replace("126h54m6s", "1h 30m"), None),
        (EXHAUSTED.replace("126h54m6s", "2h30"), None),
        (None, None),
        # The live AGY CLI (2026-10-20) gives the same refusal without its code.
        (LIVE, 85 * 3600 + 10 * 60 + 7),
        (LIVE.replace("Resets in 85h10m7s.", ""), None),
        ("Daily quota reached. Resets in 45s.", None),
    ],
)
def test_a_quota_refusal_names_the_instant_agy_answers_again(
    error: str | None, wait: int | None
) -> None:
    """Only AGY's quota refusal walls its account, until the reset it names."""
    now = 1_791_294_496
    reset = agy_module.quota_reset(error, now)
    assert reset == (None if wait is None else now + wait)
