"""Exact-version conformance for the Codex app-server child, and the v7
detached Claude custody surface: environment/argv composition and the
identity-bound spawn-and-kill path.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fixture_data import synthetic_token
from host_process import process_state
from hypothesis import HealthCheck, given, seed, settings
from hypothesis import strategies as st
from workspace_root import workspace_path

import nspawn.child as child_module
import nspawn.process_identity as process_identity_module
from nspawn import claude_catalog, claude_child, wire
from nspawn.child import (
    ChildArgvError,
    ChildEnvironmentError,
    ChildIdentityError,
    ChildPipeError,
    ChildSpawnError,
    SpawnedChild,
    Version,
    app_server_arguments,
    build_child_env,
    compose_child_argv,
    parse_version,
    spawn_child,
)
from nspawn.claude_child import FORBIDDEN_CLAUDE_FLAGS
from nspawn.owner_roster import NativeAccount, Provider
from nspawn.process_identity import (
    ProcessIdentity,
    ProcessIdentityError,
    capture_process_identity,
    process_identity_matches,
)
from nspawn.pump import OutputPump, OutputStream, PumpLimitExceeded, RunOutputBudget
from nspawn.redact import SecretNeedles


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("codex-cli 0.145.0", Version(0, 145, 0)),
        ("codex-cli 0.146", Version(0, 146, 0)),
        ("prefix 7.8 then 0.145.0", Version(7, 8, 0)),
        ("codex-cli 0.148.bad", Version(0, 148, 0)),
        ("codex-cli 0.148.0.99", Version(0, 148, 0)),
        # A three-part token with a nonzero patch: distinguishes reading
        # parts[2] from the ``patch or 0`` default both would otherwise share.
        ("codex-cli 1.2.3", Version(1, 2, 3)),
        (f"codex-cli {(1 << 64) - 1}.0.0", Version((1 << 64) - 1, 0, 0)),
    ],
)
def test_version_parser_matches_the_oracle_token_rules(
    raw: str, expected: Version
) -> None:
    assert parse_version(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "codex-cli",
        "codex-cli unreleased",
        "codex-cli 0",
        "codex-cli 1..2 then 0.145.0",
        f"codex-cli {(1 << 64)}.1.0",
    ],
)
def test_malformed_or_absent_versions_are_refused_without_invention(raw: str) -> None:
    assert parse_version(raw) is None


@pytest.mark.parametrize(
    "parts",
    [
        (-1, 0, 0),
        (0, 1 << 64, 0),
        (True, 0, 0),
    ],
)
def test_version_cannot_represent_a_value_rust_u64_would_refuse(
    parts: tuple[object, object, object],
) -> None:
    with pytest.raises(ValueError):
        Version(*parts)  # type: ignore[arg-type]


def test_app_server_arguments_match_the_confined_oracle_order() -> None:
    expected = ["app-server", "--stdio", "--strict-config"]
    for feature in wire.DISABLED_CAPABILITY_FEATURES:
        expected.extend(("--disable", feature))
    for feature in ("code_mode", "code_mode_host"):
        expected.extend(("--enable", feature))
    for config in (
        'web_search="disabled"',
        "tools.web_search=false",
        "notify=[]",
        "orchestrator.mcp.enabled=false",
        "orchestrator.skills.enabled=false",
        "analytics.enabled=false",
        'history.persistence="none"',
        'shell_environment_policy.inherit="none"',
    ):
        expected.extend(("-c", config))
    expected.extend(
        (
            "-c",
            f'shell_environment_policy.set.PATH="{wire.SANDBOX_TOOL_PATH}"',
        )
    )

    assert app_server_arguments() == expected


# ---------------------------------------------------------------------------
# L3 test doubles.
#
# L4's real ``OutputPump``/``RunOutputBudget`` still raise ``NotImplementedError``
# (a sibling lane, not yet merged). ``spawn_child`` is proven here against a
# fake double bound to the exact names it imports — the same coverage-by-
# injection pattern ``process_identity.py`` documents for its own probes.
# ---------------------------------------------------------------------------


class _FakeBudget:
    """Stands in for L4's ``RunOutputBudget``; carries no behaviour."""


class _FakeOutputPump:
    """Stands in for L4's ``OutputPump``; records what it was given."""

    def __init__(
        self,
        reader,
        run_dir,
        stream,
        needles,
        budget,
        *,
        max_line_bytes=0,
        transform_line=None,
    ):
        self.reader = reader
        self.run_dir = run_dir
        self.stream = stream
        self.needles = needles
        self.budget = budget
        self.started = False
        self.join_calls: list[float | None] = []

    @property
    def path(self) -> Path:
        return Path(self.run_dir) / self.stream.value

    @property
    def bytes_written(self) -> int:
        return 0

    def start(self) -> _FakeOutputPump:
        self.started = True
        return self

    def join(self, timeout: float | None = None) -> None:
        self.join_calls.append(timeout)


class _FakePopen:
    """Stands in for ``subprocess.Popen`` at the two failure seams that a
    real spawn cannot cheaply force: a missing stdio pipe, and identity
    capture failing right after a real spawn.
    """

    def __init__(self, pid: int, stdout: object, stderr: object) -> None:
        self.pid = pid
        self.stdin = None
        self.stdout = stdout
        self.stderr = stderr
        self.returncode: int | None = None
        self.kill_calls = 0
        self.wait_calls: list[float | None] = []

    def kill(self) -> None:
        self.kill_calls += 1
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls.append(timeout)
        return self.returncode if self.returncode is not None else 0

    def poll(self) -> int | None:
        return self.returncode


def _patch_pump(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(child_module, "OutputPump", _FakeOutputPump)
    monkeypatch.setattr(child_module, "RunOutputBudget", _FakeBudget)


def _real_spawn(
    argv: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> SpawnedChild:
    _patch_pump(monkeypatch)
    return spawn_child(argv, tmp_path, dict(os.environ), tmp_path, SecretNeedles.none())


# Captured before any test can monkeypatch ``child_module.os.killpg`` — that
# patch replaces the ONE shared ``os`` module's attribute process-wide, so a
# cleanup helper calling ``os.killpg`` through the live module would call the
# same broken stand-in the test just installed, leaving the real process
# alive under whichever ``os.killpg`` the most recent test patched in.
_REAL_KILLPG = os.killpg
_REAL_KILL = os.kill


def _force_kill_group(pid: int) -> None:
    """Best-effort cleanup, immune to whatever the exercised mutant broke.

    Tries the group first (the normal case: ``start_new_session=True`` made
    ``pid`` its own process-group leader). A mutant that flips that flag
    off leaves the spawned process in the CALLER's group instead, where
    ``killpg(pid, ...)`` addresses a group nothing owns and raises
    ``ProcessLookupError`` — silently leaving the real process alive. The
    direct ``kill(pid, ...)`` fallback reaches it regardless.
    """
    for killer, target in ((_REAL_KILLPG, pid), (_REAL_KILL, pid)):
        try:
            killer(target, signal.SIGKILL)
        except ProcessLookupError, PermissionError:
            pass


# ---------------------------------------------------------------------------
# build_child_env
# ---------------------------------------------------------------------------


def test_build_child_env_reads_the_token_file_and_delivers_it(
    tmp_path: Path,
) -> None:
    # This test asserted `env == {}`: it CODIFIED the defect. The credential
    # was read only for the needles and discarded, and the child of a provider with no
    # env block of its own was born with nothing to authenticate with. Now the env
    # carries the token, and the needles keep covering the same bytes.
    token = tmp_path / "token"
    token.write_text("a-real-credential-value-that-is-long-enough")
    provider = Provider(name="claude", token_file=token)

    env, needles = build_child_env(provider, None, ambient={})

    assert env == {
        "CLAUDE_CODE_OAUTH_TOKEN": "a-real-credential-value-that-is-long-enough"
    }
    assert not needles.is_empty()


def test_build_child_env_layers_provider_env_over_the_closed_base(
    tmp_path: Path,
) -> None:
    token = tmp_path / "token"
    token.write_text("another-real-credential-value-1234567890")
    provider = Provider(
        name="claude",
        token_file=token,
        env={
            "ANTHROPIC_BASE_URL": "https://example.invalid",
            "PLUGIN_PATH": "$PLUGIN/lab",
        },
    )
    base = {"HOME": "/private/home", "ANTHROPIC_BASE_URL": "https://overridden.invalid"}

    env, _ = build_child_env(
        provider,
        None,
        model="claude-opus-5",
        plugin_root=Path("/opt/test/plugin"),
        base_environ=base,
    )

    assert env["HOME"] == "/private/home"
    assert env["ANTHROPIC_BASE_URL"] == "https://example.invalid"
    assert env["ANTHROPIC_MODEL"] == "claude-opus-5"
    assert env["PLUGIN_PATH"] == "/opt/test/plugin/lab"
    # base_environ is copied, never mutated in place.
    assert base["ANTHROPIC_BASE_URL"] == "https://overridden.invalid"


def test_build_child_env_falls_back_to_auth_env_when_no_file_is_declared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NOTT_NSPAWN_TEST_AUTH_KEY", "zzzzzzzzzzzzzzzzzzzzzzzz")
    provider = Provider(name="zai", auth_env="NOTT_NSPAWN_TEST_AUTH_KEY")

    _, needles = build_child_env(provider, None)

    assert not needles.is_empty()
    assert needles.redact("zzzzzzzzzzzzzzzzzzzzzzzz") != "zzzzzzzzzzzzzzzzzzzzzzzz"


def test_build_child_env_uses_the_explicit_account_alias_not_the_default_one(
    tmp_path: Path,
) -> None:
    """``account_alias`` must reach the exact selected credential: a provider
    with two discovered accounts picks the alphabetically-first one by
    default, so an explicit, later alias only takes effect if it is actually
    threaded through — proving the plumbing, not just that some file loads.
    """
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "alpha.token").write_text("alpha-credential-bytes-1234567890")
    (secrets / "beta.token").write_text("beta-credential-bytes-0987654321")
    provider = Provider(name="claude", secrets=secrets)

    _, needles = build_child_env(provider, "beta")

    assert needles.redact("beta-credential-bytes-0987654321") != (
        "beta-credential-bytes-0987654321"
    )
    assert needles.redact("alpha-credential-bytes-1234567890") == (
        "alpha-credential-bytes-1234567890"
    )


def test_build_child_env_refuses_with_no_credential_source_at_all() -> None:
    provider = Provider(
        name="bare",
    )

    with pytest.raises(ChildEnvironmentError) as caught:
        build_child_env(provider, None)

    message = str(caught.value)
    assert "'bare'" in message
    # The refusal names the two doors the owner CAN write. `token_file`
    # is retired, so naming it would send them to the door the parser refuses.
    assert "secrets directory" in message
    assert "auth_env" in message
    assert "token_file" not in message


def test_build_child_env_refuses_an_unset_auth_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NOTT_NSPAWN_TEST_AUTH_KEY_ABSENT", raising=False)
    provider = Provider(name="zai", auth_env="NOTT_NSPAWN_TEST_AUTH_KEY_ABSENT")

    with pytest.raises(ChildEnvironmentError) as caught:
        build_child_env(provider, None)

    message = str(caught.value)
    assert "'zai'" in message
    assert "auth_env 'NOTT_NSPAWN_TEST_AUTH_KEY_ABSENT' is unset or empty" in message


def test_build_child_env_refuses_an_unreadable_token_file() -> None:
    missing = Path("/nonexistent/nspawn-token-file")
    provider = Provider(name="claude", token_file=missing)

    with pytest.raises(ChildEnvironmentError) as caught:
        build_child_env(provider, None)

    message = str(caught.value)
    assert "'claude'" in message
    assert "credential file" in message
    assert str(missing) in message


def test_build_child_env_refuses_a_blank_credential(tmp_path: Path) -> None:
    token = tmp_path / "token"
    token.write_text("   \n\t  ")
    provider = Provider(name="claude", token_file=token)

    with pytest.raises(ChildEnvironmentError) as caught:
        build_child_env(provider, None)

    assert caught.value.detail == (
        "provider 'claude': credential yields no redactable secret material; "
        "nSPAWN would be unable to keep it out of executor output"
    )


@pytest.mark.parametrize(
    "corpus",
    [
        b"",
        b"\n",
        b"   ",
        b"\xff\xfe\x00\x01",
        json.dumps({"api_key": synthetic_token("child-json")}).encode(),
        b"plain-token-value-not-json-1234567890",
        b"\x00" * 32,
        ("x" * 70_000).encode(),
    ],
    ids=[
        "empty",
        "newline",
        "spaces",
        "invalid-utf8",
        "json",
        "token",
        "nul-bytes",
        "huge",
    ],
)
def test_build_child_env_versioned_credential_corpus_never_raises_untyped(
    tmp_path: Path, corpus: bytes
) -> None:
    token = tmp_path / "token"
    token.write_bytes(corpus)
    provider = Provider(name="claude", token_file=token)

    try:
        build_child_env(provider, None)
    except ChildEnvironmentError:
        pass


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=50)
@seed(20260828)
@given(data=st.binary(max_size=500))
def test_build_child_env_fuzzes_arbitrary_credential_bytes(
    tmp_path: Path, data: bytes
) -> None:
    # One file rewritten per example, not a TemporaryDirectory each: the same
    # bytes reach the same reader without five filesystem operations apiece.
    token = tmp_path / "token"
    token.write_bytes(data)
    provider = Provider(name="claude", token_file=token)
    try:
        build_child_env(provider, None)
    except ChildEnvironmentError:
        pass


# ---------------------------------------------------------------------------
# compose_child_argv
# ---------------------------------------------------------------------------


def test_compose_child_argv_fresh_turn_golden_order() -> None:
    argv = compose_child_argv(
        "/usr/bin/claude",
        session_id="deadbeef-0000-4000-8000-000000000000",
        append_system_prompt_file="/run/boot-prompt.md",
        plugin_dir="/opt/test/plugin",
    )

    assert argv == [
        "/usr/bin/claude",
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--setting-sources",
        "user",
        # no --strict-mcp-config: there is a plugin_dir, and the strict flag would erase
        # the plugin's own servers. See the door test just below.
        "--append-system-prompt-file",
        "/run/boot-prompt.md",
        "--plugin-dir",
        "/opt/test/plugin",
        "--session-id=deadbeef-0000-4000-8000-000000000000",
    ]


def test_compose_child_argv_resume_turn_omits_the_session_id_flag() -> None:
    argv = compose_child_argv(
        "/usr/bin/claude",
        session_id="deadbeef-0000-4000-8000-000000000000",
        resume=True,
    )

    assert argv == [
        "/usr/bin/claude",
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--setting-sources",
        "user",
        "--strict-mcp-config",
        "--resume=deadbeef-0000-4000-8000-000000000000",
    ]


def test_compose_child_argv_minimal_call_carries_only_the_base_line() -> None:
    argv = compose_child_argv(
        "/usr/bin/claude",
    )

    assert argv == [
        "/usr/bin/claude",
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--setting-sources",
        "user",
        "--strict-mcp-config",
    ]


def test_compose_child_argv_refuses_resume_without_a_session_id() -> None:
    with pytest.raises(ChildArgvError) as caught:
        compose_child_argv("/usr/bin/claude", resume=True)
    assert caught.value.detail == "resume=True requires a session_id to resume"


def test_compose_child_argv_accepts_a_path_binary() -> None:
    argv = compose_child_argv(
        Path("/usr/bin/claude"),
    )
    assert argv[0] == "/usr/bin/claude"


def test_a_hostile_model_string_cannot_reach_the_argv_at_all() -> None:
    """The model left the command line, so its hostile-string vector left too.

    This used to assert that a `--model` VALUE could not be parsed as a flag —
    a real guard while the model travelled on argv. Owner ruling 2026-08-29
    moved it to the environment, so the composer has no model parameter and the
    strongest statement is now the absence itself.
    """
    forbidden = FORBIDDEN_CLAUDE_FLAGS[0]
    argv = compose_child_argv(
        "/usr/bin/claude",
    )

    assert "--model" not in argv
    assert forbidden not in argv


_SAFE_TOKEN = st.text(
    alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd"), max_codepoint=0x7A),
    min_size=1,
    max_size=24,
)


@st.composite
def _compose_argv_kwargs_fresh(draw: st.DrawFn) -> dict[str, object]:
    """``resume`` fixed False: every draw takes the SAME branch, so no
    assertion below needs to be conditional on it.
    """
    return {
        "session_id": draw(st.none() | _SAFE_TOKEN),
        "append_system_prompt_file": draw(st.none() | _SAFE_TOKEN),
        "plugin_dir": draw(st.none() | _SAFE_TOKEN),
    }


@st.composite
def _compose_argv_kwargs_resume(draw: st.DrawFn) -> dict[str, object]:
    """``resume`` fixed True with a mandatory session_id: the OTHER branch,
    in its own dedicated campaign rather than a shared coin flip.
    """
    return {
        "session_id": draw(_SAFE_TOKEN),
        "append_system_prompt_file": draw(st.none() | _SAFE_TOKEN),
        "plugin_dir": draw(st.none() | _SAFE_TOKEN),
    }


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=200)
@seed(20260828)
@given(kwargs=_compose_argv_kwargs_fresh())
def test_compose_child_argv_fresh_carries_no_prompt_slot_and_no_forbidden_flag(
    kwargs: dict[str, object],
) -> None:
    argv = compose_child_argv("/usr/bin/claude", **kwargs)  # type: ignore[arg-type]

    assert argv[0] == "/usr/bin/claude"
    #: The prompt rides on stdin: no positional slot, so no caller text on argv.
    assert "--" not in argv
    for forbidden in FORBIDDEN_CLAUDE_FLAGS:
        assert forbidden not in argv
    assert "--resume" not in argv
    assert "--model" not in argv
    assert "--effort" not in argv
    assert "--session-id" not in argv
    session_id = kwargs["session_id"]
    assert [arg for arg in argv if arg.startswith("--session-id=")] == (
        [] if session_id is None else [f"--session-id={session_id}"]
    )
    assert ("--plugin-dir" in argv) == (kwargs["plugin_dir"] is not None)
    assert ("--append-system-prompt-file" in argv) == (
        kwargs["append_system_prompt_file"] is not None
    )


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=200)
@seed(20260828)
@given(kwargs=_compose_argv_kwargs_resume())
def test_compose_child_argv_resume_carries_no_prompt_slot_and_no_forbidden_flag(
    kwargs: dict[str, object],
) -> None:
    argv = compose_child_argv(
        "/usr/bin/claude",
        resume=True,
        **kwargs,  # type: ignore[arg-type]
    )

    assert argv[0] == "/usr/bin/claude"
    #: The prompt rides on stdin: no positional slot, so no caller text on argv.
    assert "--" not in argv
    for forbidden in FORBIDDEN_CLAUDE_FLAGS:
        assert forbidden not in argv
    assert "--resume" not in argv
    assert [arg for arg in argv if arg.startswith("--resume=")] == [
        f"--resume={kwargs['session_id']}"
    ]
    assert "--model" not in argv
    assert "--effort" not in argv
    assert "--session-id" not in argv
    assert ("--plugin-dir" in argv) == (kwargs["plugin_dir"] is not None)
    assert ("--append-system-prompt-file" in argv) == (
        kwargs["append_system_prompt_file"] is not None
    )


# ---------------------------------------------------------------------------
# SpawnedChild — construction and delegation, against the fake Popen.
# ---------------------------------------------------------------------------


def test_spawned_child_refuses_a_pid_mismatched_identity(tmp_path: Path) -> None:
    process = _FakePopen(pid=111, stdout=io.BytesIO(), stderr=io.BytesIO())
    mismatched = ProcessIdentity(pid=222, boot_id="a" * 8, start_ticks=1)

    with pytest.raises(ChildIdentityError) as caught:
        SpawnedChild(process, mismatched, object(), object(), tmp_path)  # type: ignore[arg-type]

    message = str(caught.value)
    assert "captured identity pid 222" in message
    assert "spawned process pid 111" in message


def test_spawned_child_polls_then_waits_for_both_pumps_and_exposes_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakePopen(pid=333, stdout=io.BytesIO(), stderr=io.BytesIO())
    identity = ProcessIdentity(pid=333, boot_id="b" * 8, start_ticks=2)
    run_dir = tmp_path / "run"
    budget = _FakeBudget()
    stdout_pump = _FakeOutputPump(
        io.BytesIO(), run_dir, OutputStream.STDOUT, SecretNeedles.none(), budget
    )
    stderr_pump = _FakeOutputPump(
        io.BytesIO(), run_dir, OutputStream.STDERR, SecretNeedles.none(), budget
    )
    child = SpawnedChild(process, identity, stdout_pump, stderr_pump, run_dir)  # type: ignore[arg-type]

    monkeypatch.setattr(child_module.os, "waitid", lambda *args: None)
    assert child.identity == identity
    assert child.poll() is None
    process.returncode = 7
    assert child.poll() == 7
    assert child.wait(timeout=1.5) == 7
    assert process.wait_calls == [], "a cached exit needs no second reap"
    assert stdout_pump.join_calls == [1.5]
    assert stderr_pump.join_calls == [1.5]
    assert child.stdout_pump is stdout_pump
    assert child.stderr_pump is stderr_pump
    assert child.run_dir == run_dir


def test_spawned_child_wait_cannot_return_while_a_pump_join_is_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = _FakePopen(pid=336, stdout=io.BytesIO(), stderr=io.BytesIO())
    process.returncode = 0
    identity = ProcessIdentity(pid=336, boot_id="e" * 8, start_ticks=5)
    budget = _FakeBudget()
    stdout_pump = _FakeOutputPump(
        io.BytesIO(), tmp_path, OutputStream.STDOUT, SecretNeedles.none(), budget
    )
    stderr_pump = _FakeOutputPump(
        io.BytesIO(), tmp_path, OutputStream.STDERR, SecretNeedles.none(), budget
    )
    join_entered = threading.Event()
    release_join = threading.Event()

    def _block_until_released(timeout: float | None = None) -> None:
        stdout_pump.join_calls.append(timeout)
        join_entered.set()
        release_join.wait()

    monkeypatch.setattr(stdout_pump, "join", _block_until_released)
    child = SpawnedChild(process, identity, stdout_pump, stderr_pump, tmp_path)  # type: ignore[arg-type]
    returned: list[int] = []
    waiter = threading.Thread(target=lambda: returned.append(child.wait()), daemon=True)
    waiter.start()
    try:
        assert join_entered.wait(timeout=1)
        assert waiter.is_alive()
        assert returned == []
    finally:
        release_join.set()
    waiter.join(timeout=1)

    assert not waiter.is_alive()
    assert returned == [0]
    assert stdout_pump.join_calls == [None]
    assert stderr_pump.join_calls == [None]


def test_spawned_child_does_not_join_pumps_after_the_process_times_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = _FakePopen(pid=334, stdout=io.BytesIO(), stderr=io.BytesIO())
    identity = ProcessIdentity(pid=334, boot_id="c" * 8, start_ticks=3)
    budget = _FakeBudget()
    stdout_pump = _FakeOutputPump(
        io.BytesIO(), tmp_path, OutputStream.STDOUT, SecretNeedles.none(), budget
    )
    stderr_pump = _FakeOutputPump(
        io.BytesIO(), tmp_path, OutputStream.STDERR, SecretNeedles.none(), budget
    )

    started = time.monotonic()

    def _still_running(*_args: object) -> None:
        # The double never exits, so a `wait` that lost its deadline would spin
        # forever; it fails here instead, 20x past the 0.25 s it was given.
        assert time.monotonic() - started < 5, "wait outlived its own deadline"

    monkeypatch.setattr(child_module.os, "waitid", _still_running)
    child = SpawnedChild(process, identity, stdout_pump, stderr_pump, tmp_path)  # type: ignore[arg-type]

    with pytest.raises(subprocess.TimeoutExpired):
        child.wait(timeout=0.25)

    assert process.wait_calls == [], "a running leader must not be reaped"
    assert stdout_pump.join_calls == []
    assert stderr_pump.join_calls == []


def test_spawned_child_wait_surfaces_a_stored_pump_limit_refusal(
    tmp_path: Path,
) -> None:
    process = _FakePopen(pid=335, stdout=io.BytesIO(), stderr=io.BytesIO())
    process.returncode = 0
    identity = ProcessIdentity(pid=335, boot_id="d" * 8, start_ticks=4)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    budget = RunOutputBudget(maximum_bytes=1)
    stdout_pump = OutputPump(
        io.BytesIO(b"too much\n"),
        run_dir,
        OutputStream.STDOUT,
        SecretNeedles.none(),
        budget,
    ).start()
    stderr_pump = _FakeOutputPump(
        io.BytesIO(), run_dir, OutputStream.STDERR, SecretNeedles.none(), budget
    )
    child = SpawnedChild(process, identity, stdout_pump, stderr_pump, run_dir)  # type: ignore[arg-type]

    with pytest.raises(PumpLimitExceeded, match="run output budget exceeded"):
        child.wait(timeout=5)

    assert process.wait_calls == [], "pump failure preserves the cached exit"
    assert stderr_pump.join_calls == [5]


# ---------------------------------------------------------------------------
# spawn_child
# ---------------------------------------------------------------------------


def test_spawn_child_refuses_empty_argv(tmp_path: Path) -> None:
    with pytest.raises(ChildSpawnError) as caught:
        spawn_child([], tmp_path, {}, tmp_path, SecretNeedles.none())
    assert caught.value.detail == "<empty argv>: argv must name at least the binary"


def test_spawn_child_wraps_a_real_popen_failure(tmp_path: Path) -> None:
    binary = "/definitely/not/a/real/nspawn-binary"
    with pytest.raises(ChildSpawnError) as caught:
        spawn_child([binary], tmp_path, {}, tmp_path, SecretNeedles.none())
    assert caught.value.detail == (
        f"{binary}: [Errno 2] No such file or directory: {binary!r}"
    )


@pytest.mark.parametrize(
    "stdout, stderr",
    [
        (None, None),
        (None, io.BytesIO()),
        (io.BytesIO(), None),
    ],
    ids=["both-missing", "stdout-missing", "stderr-missing"],
)
def test_spawn_child_kills_and_refuses_when_a_pipe_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stdout: object,
    stderr: object,
) -> None:
    fake = _FakePopen(pid=555, stdout=stdout, stderr=stderr)
    monkeypatch.setattr(child_module.subprocess, "Popen", lambda *a, **k: fake)

    with pytest.raises(ChildPipeError) as caught:
        spawn_child(["/bin/true"], tmp_path, {}, tmp_path, SecretNeedles.none())

    assert "process 555 did not bind both stdio pipes" in str(caught.value)
    assert fake.kill_calls == 1
    assert fake.wait_calls == [None]


def test_spawn_child_kills_and_refuses_when_identity_capture_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakePopen(pid=556, stdout=io.BytesIO(), stderr=io.BytesIO())
    monkeypatch.setattr(child_module.subprocess, "Popen", lambda *a, **k: fake)

    def _raise(pid: int) -> ProcessIdentity:
        raise ProcessIdentityError("boom")

    monkeypatch.setattr(child_module, "capture_process_identity", _raise)

    with pytest.raises(ChildIdentityError) as caught:
        spawn_child(["/bin/true"], tmp_path, {}, tmp_path, SecretNeedles.none())

    assert "process 556 identity could not be captured: boom" in str(caught.value)
    assert fake.kill_calls == 1


def test_spawn_child_starts_both_pumps_wired_to_their_own_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    needles = SecretNeedles.none()
    _patch_pump(monkeypatch)
    spawned = spawn_child(
        [sys.executable, "-c", "pass"], tmp_path, dict(os.environ), run_dir, needles
    )
    try:
        assert spawned.stdout_pump.started is True
        assert spawned.stderr_pump.started is True
        assert spawned.stdout_pump.budget is spawned.stderr_pump.budget
        assert isinstance(spawned.stdout_pump.budget, _FakeBudget)
        assert spawned.stdout_pump.stream is OutputStream.STDOUT
        assert spawned.stderr_pump.stream is OutputStream.STDERR
        assert spawned.stdout_pump.run_dir == run_dir
        assert spawned.stderr_pump.run_dir == run_dir
        assert spawned.run_dir == run_dir
        assert spawned.stdout_pump.needles is needles
        assert spawned.stderr_pump.needles is needles
        assert spawned.stdout_pump.reader is not None
        assert spawned.stderr_pump.reader is not None
        assert spawned.stdout_pump.reader is not spawned.stderr_pump.reader
        assert spawned.identity.pid == spawned._process.pid  # type: ignore[attr-defined]
        assert spawned.wait(timeout=5) == 0
    finally:
        _force_kill_group(spawned.identity.pid)


def test_spawn_child_uses_the_requested_cwd_and_closed_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    script = (
        "import os, sys\n"
        "sys.stdout.write(os.path.realpath(os.getcwd()) + chr(10))\n"
        "sys.stdout.write(os.environ.get('NOTT_NSPAWN_TEST_MARKER', '<absent>') + chr(10))\n"
    )
    _patch_pump(monkeypatch)
    spawned = spawn_child(
        [sys.executable, "-c", script],
        work_dir,
        {"NOTT_NSPAWN_TEST_MARKER": "hello-mutation-proof"},
        tmp_path,
        SecretNeedles.none(),
    )
    try:
        observed_cwd = spawned.stdout_pump.reader.readline().decode().strip()
        observed_marker = spawned.stdout_pump.reader.readline().decode().strip()
        assert spawned.wait(timeout=5) == 0
        assert observed_cwd == os.path.realpath(work_dir)
        assert observed_marker == "hello-mutation-proof"
    finally:
        _force_kill_group(spawned.identity.pid)


def test_spawn_child_binds_stdin_to_devnull_not_the_caller_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Redirects THIS test process's own fd 0 to a pipe first, so the child
    cannot pass by coincidentally inheriting an already-``/dev/null`` stdin
    under a harness that itself redirects fd 0 there.
    """
    _patch_pump(monkeypatch)
    read_end, write_end = os.pipe()
    saved_stdin = os.dup(0)
    try:
        os.dup2(read_end, 0)
        os.close(read_end)
        # The same device and inode as /dev/null: `/proc/self/fd` is Linux-only.
        script = (
            "import os, sys; a, b = os.fstat(0), os.stat('/dev/null'); "
            "sys.stdout.write(('/dev/null' if (a.st_dev, a.st_ino) == "
            "(b.st_dev, b.st_ino) else repr(a)) + chr(10))"
        )
        spawned = spawn_child(
            [sys.executable, "-c", script],
            tmp_path,
            dict(os.environ),
            tmp_path,
            SecretNeedles.none(),
        )
        try:
            observed = spawned.stdout_pump.reader.readline().decode().strip()
            assert spawned.wait(timeout=5) == 0
            assert observed == "/dev/null"
        finally:
            _force_kill_group(spawned.identity.pid)
    finally:
        os.dup2(saved_stdin, 0)
        os.close(saved_stdin)
        os.close(write_end)


def test_spawn_child_closes_unrelated_parent_file_descriptors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    leak_source = tmp_path / "leak-source"
    leak_source.write_text("leak")
    leaked_fd = os.open(leak_source, os.O_RDONLY)
    # PEP 446 makes every fd non-inheritable by default; force it inheritable
    # so ``close_fds=True`` is the ONLY thing standing between it and the
    # child — otherwise this proves nothing about the flag at all.
    os.set_inheritable(leaked_fd, True)
    try:
        script = (
            "import os, sys\n"
            f"sys.stdout.write(str(os.path.exists('/dev/fd/{leaked_fd}')) + chr(10))\n"
        )
        _patch_pump(monkeypatch)
        spawned = spawn_child(
            [sys.executable, "-c", script],
            tmp_path,
            dict(os.environ),
            tmp_path,
            SecretNeedles.none(),
        )
        try:
            observed = spawned.stdout_pump.reader.readline().decode().strip()
            assert spawned.wait(timeout=5) == 0
            assert observed == "False"
        finally:
            _force_kill_group(spawned.identity.pid)
    finally:
        os.close(leaked_fd)


# ---------------------------------------------------------------------------
# stop_then_kill — real process-group SIGSTOP-then-SIGKILL custody.
# ---------------------------------------------------------------------------


def test_stop_then_kill_kills_the_whole_process_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = (
        "import os, signal, sys\n"
        "child = os.fork()\n"
        "if child == 0:\n"
        "    signal.pause()\n"
        "else:\n"
        "    sys.stdout.write(str(child) + chr(10)); sys.stdout.flush()\n"
        "    signal.pause()\n"
    )
    spawned = _real_spawn([sys.executable, "-c", script], tmp_path, monkeypatch)
    grandchild_pid: int | None = None
    try:
        raw_line = spawned.stdout_pump.reader.readline()
        grandchild_pid = int(raw_line.decode().strip())
        grandchild_identity = capture_process_identity(grandchild_pid)

        spawned.stop_then_kill(spawned.identity)

        assert spawned.wait(timeout=5) < 0
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and process_identity_matches(
            grandchild_identity
        ):
            time.sleep(0.02)
        assert not process_identity_matches(grandchild_identity)
    finally:
        _force_kill_group(spawned.identity.pid)
        if grandchild_pid is not None:
            _force_kill_group(grandchild_pid)


def test_stop_then_kill_refuses_when_the_process_already_exited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawned = _real_spawn([sys.executable, "-c", "pass"], tmp_path, monkeypatch)
    assert spawned.wait(timeout=5) == 0

    with pytest.raises(ChildIdentityError) as caught:
        spawned.stop_then_kill(spawned.identity)

    assert caught.value.detail == (
        f"process {spawned.identity.pid} no longer matches its captured identity; "
        "refusing to signal a group that may now belong to another process"
    )


def test_stop_then_kill_wraps_a_vanished_process_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawned = _real_spawn(
        [sys.executable, "-c", "import signal; signal.pause()"], tmp_path, monkeypatch
    )
    try:

        def _raise_lookup(pid: int, sig: int) -> None:
            raise ProcessLookupError("gone")

        monkeypatch.setattr(child_module.os, "killpg", _raise_lookup)

        with pytest.raises(ChildIdentityError) as caught:
            spawned.stop_then_kill(spawned.identity)

        message = str(caught.value)
        assert f"process group {spawned.identity.pid} vanished" in message
        assert "verification and signal" in message
    finally:
        _force_kill_group(spawned.identity.pid)


def test_stop_then_kill_wraps_an_arbitrary_os_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawned = _real_spawn(
        [sys.executable, "-c", "import signal; signal.pause()"], tmp_path, monkeypatch
    )
    try:

        def _raise_permission(pid: int, sig: int) -> None:
            raise PermissionError("nope")

        monkeypatch.setattr(child_module.os, "killpg", _raise_permission)

        with pytest.raises(ChildIdentityError) as caught:
            spawned.stop_then_kill(spawned.identity)

        message = str(caught.value)
        assert f"process group {spawned.identity.pid} could not be signaled" in message
    finally:
        _force_kill_group(spawned.identity.pid)


class _Alarm(Exception):
    """What a signal handler (a test-timeout alarm, Ctrl-C) raises."""


@pytest.fixture
def main_thread_alarm() -> Iterator[list[bool]]:
    """A real SIGUSR1 handler that raises `_Alarm` on the main thread."""
    raised: list[bool] = []

    def interrupt(_signum: int, _frame: object) -> None:
        raised.append(True)
        raise _Alarm

    previous = signal.signal(signal.SIGUSR1, interrupt)
    try:
        yield raised
    finally:
        signal.signal(signal.SIGUSR1, previous)


def _signal_the_main_thread() -> None:
    main = threading.main_thread().ident
    assert main is not None
    if threading.get_ident() != main:
        time.sleep(0.1)  # the caller has reached its wait for this thread
    signal.pthread_kill(main, signal.SIGUSR1)
    time.sleep(0.2)  # on the main thread the handler raises here


def test_a_signal_inside_the_stop_to_kill_window_still_kills_the_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, main_thread_alarm: list[bool]
) -> None:
    # The SIGSTOP is sent and the handler's exception lands before the SIGKILL.
    # A group stopped and never killed sits in state T for good: four AGY test
    # children did, for 7.5 days, adopted by init.
    spawned = _real_spawn(
        [sys.executable, "-c", "import signal; signal.pause()"], tmp_path, monkeypatch
    )
    try:

        def stop_then_signal(pid: int, sig: int) -> None:
            _REAL_KILLPG(pid, sig)
            if sig == signal.SIGSTOP and not main_thread_alarm:
                _signal_the_main_thread()

        monkeypatch.setattr(child_module.os, "killpg", stop_then_signal)

        with pytest.raises(_Alarm):
            spawned.stop_then_kill(spawned.identity)

        assert main_thread_alarm == [True]
        deadline = time.monotonic() + 2
        while (state := process_state(spawned.identity.pid)) is not None and (
            not state.startswith("Z") and time.monotonic() < deadline
        ):
            time.sleep(0.02)
        assert state is None or state.startswith("Z"), state
        assert "nspawn-group-kill" not in [t.name for t in threading.enumerate()]
        assert spawned.wait(timeout=5) == -signal.SIGKILL
    finally:
        _force_kill_group(spawned.identity.pid)


def test_a_refusal_behind_an_interrupted_kill_is_chained_not_lost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, main_thread_alarm: list[bool]
) -> None:
    spawned = _real_spawn(
        [sys.executable, "-c", "import signal; signal.pause()"], tmp_path, monkeypatch
    )
    try:

        def refuse_the_stop(pid: int, sig: int) -> None:
            if sig == signal.SIGSTOP and not main_thread_alarm:
                _signal_the_main_thread()
                raise OSError("synthetic stop refusal")
            _REAL_KILLPG(pid, sig)

        monkeypatch.setattr(child_module.os, "killpg", refuse_the_stop)

        with pytest.raises(_Alarm) as caught:
            spawned.stop_then_kill(spawned.identity)

        refusal = caught.value.__cause__
        assert isinstance(refusal, ChildIdentityError)
        assert "could not be signaled: synthetic stop refusal" in str(refusal)
        assert spawned.wait(timeout=5) == -signal.SIGKILL
    finally:
        _force_kill_group(spawned.identity.pid)


class _SecondAlarm(Exception):
    """A later, different interrupt (an alarm after a Ctrl-C, say)."""


def test_the_first_interrupt_of_a_group_kill_is_the_one_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawned = _real_spawn(
        [sys.executable, "-c", "import signal; signal.pause()"], tmp_path, monkeypatch
    )
    interrupts = [_Alarm, _SecondAlarm]

    def interrupt(_signum: int, _frame: object) -> None:
        if interrupts:
            raise interrupts.pop(0)

    previous = signal.signal(signal.SIGUSR1, interrupt)
    try:

        def two_interrupts_then_stop(pid: int, sig: int) -> None:
            if sig == signal.SIGSTOP and interrupts:
                _signal_the_main_thread()
                _signal_the_main_thread()
            _REAL_KILLPG(pid, sig)

        monkeypatch.setattr(child_module.os, "killpg", two_interrupts_then_stop)

        with pytest.raises(_Alarm) as caught:
            spawned.stop_then_kill(spawned.identity)

        assert interrupts == []
        assert any("_SecondAlarm" in note for note in caught.value.__notes__)
        assert spawned.wait(timeout=5) == -signal.SIGKILL
    finally:
        signal.signal(signal.SIGUSR1, previous)
        _force_kill_group(spawned.identity.pid)


def test_two_interrupts_during_a_delayed_signaler_start_raise_the_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The first interrupt escapes Thread.start() before the signaler runs; the
    # second lands while the caller waits for it to start. The first is the
    # one raised, the second a note on it, and the kill still completes.
    spawned = _real_spawn(
        [sys.executable, "-c", "import signal; signal.pause()"], tmp_path, monkeypatch
    )
    main = threading.main_thread().ident
    assert main is not None
    interrupts = [_Alarm, _SecondAlarm]

    def interrupt(_signum: int, _frame: object) -> None:
        if interrupts:
            raise interrupts.pop(0)

    real_start = threading.Thread.start

    def delayed_start(thread: threading.Thread) -> None:
        if thread.name != "nspawn-group-kill":
            real_start(thread)
            return
        threading.Timer(0.3, real_start, args=(thread,)).start()
        threading.Timer(0.15, signal.pthread_kill, args=(main, signal.SIGUSR1)).start()
        _signal_the_main_thread()

    previous = signal.signal(signal.SIGUSR1, interrupt)
    try:
        monkeypatch.setattr(threading.Thread, "start", delayed_start)
        with pytest.raises(_Alarm) as caught:
            spawned.stop_then_kill(spawned.identity)
        assert interrupts == []
        assert any("_SecondAlarm" in note for note in caught.value.__notes__)
        assert spawned.wait(timeout=5) == -signal.SIGKILL
    finally:
        signal.signal(signal.SIGUSR1, previous)
        _force_kill_group(spawned.identity.pid)


def test_a_signaler_that_cannot_start_is_a_typed_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Shutdown paths absorb ChildIdentityError and OSError; a bare RuntimeError
    # from Thread.start() would escape them.
    spawned = _real_spawn(
        [sys.executable, "-c", "import signal; signal.pause()"], tmp_path, monkeypatch
    )
    real_start = threading.Thread.start
    signals: list[int] = []

    def cannot_start(thread: threading.Thread) -> None:
        if thread.name == "nspawn-group-kill":
            raise RuntimeError("can't start new thread")
        real_start(thread)

    def record(pid: int, sig: int) -> None:
        signals.append(sig)

    try:
        monkeypatch.setattr(threading.Thread, "start", cannot_start)
        monkeypatch.setattr(child_module.os, "killpg", record)
        with pytest.raises(ChildIdentityError, match="can't start new thread"):
            spawned.stop_then_kill(spawned.identity)
        assert signals == []
        assert spawned.poll() is None
    finally:
        _force_kill_group(spawned.identity.pid)
        spawned._process.wait(timeout=5)


def test_a_signaler_that_cannot_start_stays_typed_behind_an_interrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, main_thread_alarm: list[bool]
) -> None:
    # Thread exhaustion, then an interrupt while the caller waits to see
    # whether a signaler runs: the start failure is still the typed refusal
    # the shutdown paths absorb, the interrupt a note on it.
    spawned = _real_spawn(
        [sys.executable, "-c", "import signal; signal.pause()"], tmp_path, monkeypatch
    )
    main = threading.main_thread().ident
    assert main is not None
    real_start = threading.Thread.start
    signals: list[int] = []

    def cannot_start(thread: threading.Thread) -> None:
        if thread.name == "nspawn-group-kill":
            threading.Timer(
                0.2, signal.pthread_kill, args=(main, signal.SIGUSR1)
            ).start()
            raise RuntimeError("can't start new thread")
        real_start(thread)

    def record(pid: int, sig: int) -> None:
        signals.append(sig)

    try:
        monkeypatch.setattr(threading.Thread, "start", cannot_start)
        monkeypatch.setattr(child_module.os, "killpg", record)
        with pytest.raises(
            ChildIdentityError, match="can't start new thread"
        ) as caught:
            spawned.stop_then_kill(spawned.identity)
        assert main_thread_alarm == [True]
        assert any("_Alarm" in note for note in caught.value.__notes__)
        assert signals == []
    finally:
        _force_kill_group(spawned.identity.pid)
        spawned._process.wait(timeout=5)


def test_the_signaler_never_outlives_the_group_kill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The signaler's own exit, after it reports done, is slow here: the call
    # must still not return while that thread is alive.
    spawned = _real_spawn(
        [sys.executable, "-c", "import signal; signal.pause()"], tmp_path, monkeypatch
    )
    signaler: list[int] = []
    real_set = threading.Event.set

    def record_signaler(pid: int, sig: int) -> None:
        signaler.append(threading.get_ident())
        _REAL_KILLPG(pid, sig)

    def slow_teardown(event: threading.Event) -> None:
        real_set(event)
        if threading.get_ident() in signaler:
            time.sleep(0.3)

    try:
        monkeypatch.setattr(child_module.os, "killpg", record_signaler)
        monkeypatch.setattr(threading.Event, "set", slow_teardown)
        spawned.stop_then_kill(spawned.identity)
        assert "nspawn-group-kill" not in [t.name for t in threading.enumerate()]
        assert spawned.wait(timeout=5) == -signal.SIGKILL
    finally:
        _force_kill_group(spawned.identity.pid)


@pytest.mark.parametrize("point", ["start", "entered-wait", "done-wait", "join"])
def test_two_interrupts_anywhere_in_the_kill_window_raise_the_first_after_the_kill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, point: str
) -> None:
    # Real signals, two each time, at each place the caller waits: inside
    # Thread.start(), while a delayed signaler starts, while it signals, and
    # while it exits. The first interrupt is raised, the second is a note on
    # it, and the group is killed before either leaves.
    spawned = _real_spawn(
        [sys.executable, "-c", "import signal; signal.pause()"], tmp_path, monkeypatch
    )
    main = threading.main_thread().ident
    assert main is not None
    interrupts = [_Alarm, _SecondAlarm]
    signaler: list[int] = []
    fired: list[str] = []

    def interrupt(_signum: int, _frame: object) -> None:
        if interrupts:
            raise interrupts.pop(0)

    def fire_two() -> None:
        fired.append(point)
        threading.Timer(0.1, signal.pthread_kill, args=(main, signal.SIGUSR1)).start()
        signal.pthread_kill(main, signal.SIGUSR1)
        time.sleep(0.25)

    real_start = threading.Thread.start
    real_set = threading.Event.set

    def start(thread: threading.Thread) -> None:
        if thread.name != "nspawn-group-kill" or fired:
            real_start(thread)
        elif point == "start":
            real_start(thread)
            fire_two()
        elif point == "entered-wait":
            threading.Timer(0.3, real_start, args=(thread,)).start()
            fire_two()
        else:
            real_start(thread)

    def slow_signal(pid: int, sig: int) -> None:
        signaler.append(threading.get_ident())
        if sig == signal.SIGSTOP:
            if point == "done-wait" and not fired:
                time.sleep(0.1)  # the caller has reached its wait
                fire_two()
            else:
                time.sleep(0.3)
        _REAL_KILLPG(pid, sig)

    def slow_teardown(event: threading.Event) -> None:
        real_set(event)
        if point == "join" and not fired and threading.get_ident() in signaler:
            time.sleep(0.1)
            fire_two()

    previous = signal.signal(signal.SIGUSR1, interrupt)
    try:
        monkeypatch.setattr(threading.Thread, "start", start)
        monkeypatch.setattr(child_module.os, "killpg", slow_signal)
        monkeypatch.setattr(threading.Event, "set", slow_teardown)
        with pytest.raises(_Alarm) as caught:
            spawned.stop_then_kill(spawned.identity)
        assert fired == [point]
        assert interrupts == []
        assert any("_SecondAlarm" in note for note in caught.value.__notes__)
        assert "nspawn-group-kill" not in [t.name for t in threading.enumerate()]
        assert spawned.wait(timeout=5) == -signal.SIGKILL
    finally:
        signal.signal(signal.SIGUSR1, previous)
        _force_kill_group(spawned.identity.pid)


def test_an_interrupt_in_the_completion_check_is_held_like_one_in_the_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The first interrupt lands in the wait for the signaler, the second in
    # the check that follows it: the check is no less inside the window.
    spawned = _real_spawn(
        [sys.executable, "-c", "import signal; signal.pause()"], tmp_path, monkeypatch
    )
    main = threading.main_thread().ident
    assert main is not None
    interrupts = [_Alarm, _SecondAlarm]
    first_sent: list[bool] = []
    real_is_set = threading.Event.is_set

    def interrupt(_signum: int, _frame: object) -> None:
        if interrupts:
            raise interrupts.pop(0)

    def stop_after_an_interrupt(pid: int, sig: int) -> None:
        if sig == signal.SIGSTOP and not first_sent:
            time.sleep(0.1)  # the caller has reached its wait
            first_sent.append(True)
            signal.pthread_kill(main, signal.SIGUSR1)
            time.sleep(0.3)
        _REAL_KILLPG(pid, sig)

    def interrupted_check(event: threading.Event) -> bool:
        if threading.get_ident() == main and first_sent and len(interrupts) == 1:
            signal.pthread_kill(main, signal.SIGUSR1)
            time.sleep(0.2)  # the handler raises here
        return real_is_set(event)

    previous = signal.signal(signal.SIGUSR1, interrupt)
    try:
        monkeypatch.setattr(child_module.os, "killpg", stop_after_an_interrupt)
        monkeypatch.setattr(threading.Event, "is_set", interrupted_check)
        with pytest.raises(_Alarm) as caught:
            spawned.stop_then_kill(spawned.identity)
        assert interrupts == []
        assert any("_SecondAlarm" in note for note in caught.value.__notes__)
        assert spawned.wait(timeout=5) == -signal.SIGKILL
    finally:
        signal.signal(signal.SIGUSR1, previous)
        _force_kill_group(spawned.identity.pid)


def test_a_signaler_that_never_finishes_is_a_typed_refusal_within_the_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The fence is held here, so the signaler cannot finish. The kill answers
    # with its typed refusal once the bound has passed (shortened to 0.5 s);
    # the fence is let go after 3 s, so an unbounded wait ends in no refusal
    # instead of a hang. The shipped bound is the documented 10 s.
    assert child_module.SIGNALER_BOUND_SECONDS == 10
    spawned = _real_spawn(
        [sys.executable, "-c", "import signal; signal.pause()"], tmp_path, monkeypatch
    )
    assert spawned._reap_fence.acquire(timeout=5)
    release = threading.Timer(3.0, spawned._reap_fence.release)
    release.start()
    try:
        monkeypatch.setattr(child_module, "SIGNALER_BOUND_SECONDS", 0.5)
        started = time.monotonic()
        with pytest.raises(ChildIdentityError, match="did not finish within 0.5 s"):
            spawned.stop_then_kill(spawned.identity)
        assert time.monotonic() - started < 3.0
        release.join(timeout=5)
        assert spawned.wait(timeout=5) == -signal.SIGKILL
    finally:
        release.join(timeout=5)
        _force_kill_group(spawned.identity.pid)


_INTERRUPT_IN_THE_DEADLINE = """
import os, signal, sys, threading, time
from pathlib import Path

import nspawn.child as child
from nspawn.redact import SecretNeedles

run = Path(sys.argv[1])
spawned = child.spawn_child(
    [sys.executable, "-c", "import signal; signal.pause()"],
    run, dict(os.environ), run, SecretNeedles.none(),
)
print("pid", spawned.identity.pid, flush=True)


class Alarm(Exception):
    pass


def interrupt(_signum, _frame):
    raise Alarm


signal.signal(signal.SIGUSR1, interrupt)
main = threading.get_ident()
real_monotonic = time.monotonic
fired = []


def monotonic():
    # The first clock read once the signaler runs is the one that sets its
    # deadline: the interrupt lands there.
    if not fired and threading.get_ident() == main and any(
        t.name == "nspawn-group-kill" for t in threading.enumerate()
    ):
        fired.append(True)
        signal.pthread_kill(main, signal.SIGUSR1)
        time.sleep(0.2)
    return real_monotonic()


# The signaler can never finish while the fence is held here: only the
# bound (1 s) ends the kill. The fence is let go after 8 s at the latest.
child.SIGNALER_BOUND_SECONDS = 1.0
spawned._reap_fence.acquire()
release = threading.Timer(8.0, spawned._reap_fence.release)
release.start()
child.time.monotonic = monotonic
try:
    spawned.stop_then_kill(spawned.identity)
except Alarm as error:
    print("alarm", type(error.__cause__).__name__, flush=True)
child.time.monotonic = real_monotonic
print("fired", len(fired), flush=True)
release.cancel()
spawned._reap_fence.release()
print("exit", spawned.wait(timeout=5), flush=True)
"""


def test_an_interrupt_while_the_deadline_is_set_still_ends_within_the_bound(
    tmp_path: Path,
) -> None:
    # Run in its own interpreter: the defect is a loop that never ends on the
    # main thread, which no in-process timeout can stop.
    probe = subprocess.Popen(
        [sys.executable, "-c", _INTERRUPT_IN_THE_DEADLINE, str(tmp_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    assert probe.stdout is not None
    first = probe.stdout.readline().split()
    assert first[0] == "pid", first
    try:
        output, _ = probe.communicate(timeout=6)
    except subprocess.TimeoutExpired:
        # The probe still holds the leader unreaped, so its group id is ours.
        _force_kill_group(int(first[1]))
        probe.kill()
        probe.communicate()
        pytest.fail("the group kill did not end once its deadline was interrupted")
    assert probe.returncode == 0, output
    assert output.split() == [
        "alarm",
        "ChildIdentityError",
        "fired",
        "1",
        "exit",
        str(-signal.SIGKILL),
    ], output


class _GatedFence:
    """The reap fence, with the first signaler held back from taking it until
    a later signaler has released it, and then keeping it a while: a stale
    signaler that outlived its refused attempt."""

    def __init__(self, hold: float) -> None:
        self._lock = threading.Lock()
        self._hold = hold
        self._stale: int | None = None
        self.later_released = threading.Event()
        self.stale_holds = threading.Event()

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        me = threading.get_ident()
        if self._stale is None and threading.current_thread().name == (
            "nspawn-group-kill"
        ):
            self._stale = me
        if me == self._stale:
            self.later_released.wait(10)
            self._lock.acquire()
            self.stale_holds.set()
            time.sleep(self._hold)
            return True
        return self._lock.acquire(blocking, timeout)

    def release(self) -> None:
        stale = threading.get_ident() == self._stale
        self._lock.release()
        if not stale and threading.current_thread().name == "nspawn-group-kill":
            self.later_released.set()
            self.stale_holds.wait(10)  # the stale one has it before anyone else

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, *_exc: object) -> None:
        self.release()


def test_a_stale_signaler_taking_the_fence_late_does_not_delay_the_reap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A refused attempt left its signaler waiting for the fence; it takes the
    # fence only after the reap's own signaler has killed and reaped, and
    # keeps it. The reap answers at once, not after that hold.
    spawned = _real_spawn(
        [sys.executable, "-c", "import signal; signal.pause()"], tmp_path, monkeypatch
    )
    fence = _GatedFence(hold=3.0)
    try:
        monkeypatch.setattr(child_module, "SIGNALER_BOUND_SECONDS", 0.5)
        monkeypatch.setattr(spawned, "_reap_fence", fence)
        with pytest.raises(ChildIdentityError, match="did not finish within"):
            spawned.stop_then_kill(spawned.identity)
        _REAL_KILLPG(spawned.identity.pid, signal.SIGKILL)
        deadline = time.monotonic() + 5
        while (
            os.waitid(
                os.P_PID,
                spawned.identity.pid,
                os.WEXITED | os.WNOHANG | os.WNOWAIT,
            )
            is None
        ):
            assert time.monotonic() < deadline
            time.sleep(0.01)
        started = time.monotonic()
        assert spawned.poll() == -signal.SIGKILL
        assert time.monotonic() - started < 2.0
        assert fence.stale_holds.is_set()
    finally:
        _force_kill_group(spawned.identity.pid)
        fence.later_released.set()
        for thread in threading.enumerate():
            if thread.name == "nspawn-group-kill":
                thread.join(timeout=10)
        spawned._process.wait(timeout=5)


class _InterruptedOnTheMainThread:
    """The reap fence, where an acquisition by the main thread is followed by
    a real signal whose handler raises before the caller's next statement."""

    def __init__(self) -> None:
        self._lock = threading.Lock()

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        acquired = self._lock.acquire(blocking, timeout)
        if acquired and threading.current_thread() is threading.main_thread():
            main = threading.main_thread().ident
            assert main is not None
            signal.pthread_kill(main, signal.SIGUSR1)
        return acquired

    def release(self) -> None:
        self._lock.release()

    def locked(self) -> bool:
        return self._lock.locked()

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, *_exc: object) -> None:
        self.release()


def test_an_interrupt_as_the_reap_takes_the_fence_never_leaves_it_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, main_thread_alarm: list[bool]
) -> None:
    # A signal whose handler raises right after the fence is taken, before
    # the code that would release it: the fence must not stay held, or every
    # later reap of this child waits on it and the leader is never reaped.
    spawned = _real_spawn(
        [sys.executable, "-c", "import signal; signal.pause()"], tmp_path, monkeypatch
    )
    fence = _InterruptedOnTheMainThread()
    try:
        monkeypatch.setattr(child_module, "SIGNALER_BOUND_SECONDS", 0.5)
        monkeypatch.setattr(spawned, "_reap_fence", fence)
        _REAL_KILLPG(spawned.identity.pid, signal.SIGKILL)
        deadline = time.monotonic() + 5
        while (
            os.waitid(
                os.P_PID, spawned.identity.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT
            )
            is None
        ):
            assert time.monotonic() < deadline
            time.sleep(0.01)
        with contextlib.suppress(_Alarm):
            spawned.poll()
        assert not fence.locked()
        assert spawned.poll() == -signal.SIGKILL
    finally:
        _force_kill_group(spawned.identity.pid)
        if spawned._process.returncode is None and not fence.locked():
            spawned._process.wait(timeout=5)


class _FirstSignalerFirst:
    """The reap fence, taken by the first signaler before any later one."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self._first: int | None = None
        self.first_released = threading.Event()
        self.later_waiting = threading.Event()

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        if threading.current_thread().name == "nspawn-group-kill":
            me = threading.get_ident()
            if self._first is None:
                self._first = me
            elif me != self._first:
                self.later_waiting.set()
                self.first_released.wait(10)
        return self.lock.acquire(blocking, timeout)

    def release(self) -> None:
        self.lock.release()
        if threading.get_ident() == self._first:
            self.first_released.set()

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, *_exc: object) -> None:
        self.release()


_REAL_WAITID = os.waitid


@pytest.mark.parametrize("reaped_before", ["the-fence", "the-waitid"])
def test_a_poll_after_a_stale_signaler_reaped_returns_its_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reaped_before: str
) -> None:
    # Poll A is refused past the bound while its signaler waits for the
    # fence. Poll B follows, and A's signaler reaps either once B's own
    # signaler waits at the fence, or before B's exit observation (waitid
    # then answers ECHILD). Either way the code A's reap read is B's answer,
    # not a refusal.
    spawned = _real_spawn(
        [sys.executable, "-c", "import signal; signal.pause()"], tmp_path, monkeypatch
    )
    fence = _FirstSignalerFirst()
    assert fence.lock.acquire(timeout=5)
    armed: list[bool] = []
    held = [True]

    def let_the_first_signaler_go() -> None:
        if held:
            held.clear()
            fence.lock.release()

    def reap_first_then_observe(
        idtype: int, ident: int, options: int
    ) -> os.waitid_result | None:
        if armed and threading.current_thread() is threading.main_thread():
            armed.clear()
            let_the_first_signaler_go()
            assert fence.first_released.wait(10)
        return _REAL_WAITID(idtype, ident, options)

    def release_once_b_waits() -> None:
        if fence.later_waiting.wait(10):
            let_the_first_signaler_go()

    releaser = threading.Thread(target=release_once_b_waits)
    try:
        monkeypatch.setattr(spawned, "_reap_fence", fence)
        _REAL_KILLPG(spawned.identity.pid, signal.SIGKILL)
        deadline = time.monotonic() + 5
        while (
            os.waitid(
                os.P_PID, spawned.identity.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT
            )
            is None
        ):
            assert time.monotonic() < deadline
            time.sleep(0.01)
        monkeypatch.setattr(child_module, "SIGNALER_BOUND_SECONDS", 0.3)
        with pytest.raises(ChildIdentityError, match="did not finish within"):
            spawned.poll()
        monkeypatch.setattr(child_module, "SIGNALER_BOUND_SECONDS", 5.0)
        if reaped_before == "the-fence":
            releaser.start()
        else:
            monkeypatch.setattr(child_module.os, "waitid", reap_first_then_observe)
            armed.append(True)
        assert spawned.poll() == -signal.SIGKILL
        assert fence.first_released.is_set()
        assert fence.later_waiting.is_set() == (reaped_before == "the-fence")
    finally:
        fence.later_waiting.set()
        if releaser.is_alive():
            releaser.join(timeout=10)
        if held:
            let_the_first_signaler_go()
        _force_kill_group(spawned.identity.pid)
        for thread in threading.enumerate():
            if thread.name == "nspawn-group-kill":
                thread.join(timeout=10)


_REAL_WAITPID = os.waitpid


def test_a_poll_between_a_stale_reap_and_its_exit_code_waits_for_the_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A stale signaler's reap has taken the child from the kernel but not yet
    # stored its exit code when poll B's waitid answers ECHILD: B must wait for
    # that code, not refuse a reap that succeeded.
    spawned = _real_spawn(
        [sys.executable, "-c", "import signal; signal.pause()"], tmp_path, monkeypatch
    )
    fence = threading.Lock()
    assert fence.acquire(timeout=5)
    # The cure waits on CPython's private Popen._waitpid_lock: fail loudly,
    # not vacuously, on an interpreter without it.
    assert isinstance(
        getattr(spawned._process, "_waitpid_lock", None), type(threading.Lock())
    )
    a_reaped = threading.Event()
    b_saw_echild = threading.Event()
    b_waits_unpublished = threading.Event()
    release_a = threading.Event()
    real_lock = spawned._process._waitpid_lock

    class _ObservedWaitLock:
        """Popen's wait lock, noting when poll B waits on it unpublished."""

        def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
            if (
                threading.current_thread() is threading.main_thread()
                and spawned._process.returncode is None
            ):
                b_waits_unpublished.set()
            return real_lock.acquire(blocking, timeout)

        def release(self) -> None:
            real_lock.release()

        def __enter__(self) -> bool:
            return self.acquire()

        def __exit__(self, *_exc: object) -> None:
            self.release()

    def reap_then_pause(pid: int, options: int) -> tuple[int, int]:
        result = _REAL_WAITPID(pid, options)
        if threading.current_thread().name == "nspawn-group-kill" and result[0]:
            a_reaped.set()
            release_a.wait(10)
        return result

    def observe(idtype: int, ident: int, options: int) -> os.waitid_result | None:
        try:
            return _REAL_WAITID(idtype, ident, options)
        except ChildProcessError:
            if threading.current_thread() is threading.main_thread():
                b_saw_echild.set()
            raise

    def release_a_once_b_waits() -> None:
        # Only once B waits on Popen's wait lock with the code still unstored:
        # B cannot be answered by a code A stored before B looked for it.
        b_waits_unpublished.wait(10)
        release_a.set()

    helper = threading.Thread(target=release_a_once_b_waits)
    try:
        monkeypatch.setattr(spawned, "_reap_fence", fence)
        _REAL_KILLPG(spawned.identity.pid, signal.SIGKILL)
        deadline = time.monotonic() + 5
        while (
            os.waitid(
                os.P_PID, spawned.identity.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT
            )
            is None
        ):
            assert time.monotonic() < deadline
            time.sleep(0.01)
        monkeypatch.setattr(child_module, "SIGNALER_BOUND_SECONDS", 0.3)
        with pytest.raises(ChildIdentityError, match="did not finish within"):
            spawned.poll()
        monkeypatch.setattr(child_module, "SIGNALER_BOUND_SECONDS", 5.0)
        monkeypatch.setattr(os, "waitpid", reap_then_pause)
        monkeypatch.setattr(child_module.os, "waitid", observe)
        monkeypatch.setattr(spawned._process, "_waitpid_lock", _ObservedWaitLock())
        helper.start()
        fence.release()
        assert a_reaped.wait(10)
        assert spawned.poll() == -signal.SIGKILL
        assert b_saw_echild.is_set()
        assert b_waits_unpublished.is_set()
    finally:
        b_waits_unpublished.set()
        release_a.set()
        if helper.is_alive():
            helper.join(timeout=10)
        if fence.locked() and not a_reaped.is_set():
            with contextlib.suppress(RuntimeError):
                fence.release()
        _force_kill_group(spawned.identity.pid)
        for thread in threading.enumerate():
            if thread.name == "nspawn-group-kill":
                thread.join(timeout=10)


def test_an_interrupt_while_the_signaler_starts_never_signals_a_reaped_leader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, main_thread_alarm: list[bool]
) -> None:
    # The interrupt escapes from Thread.start() while the signaler still owes
    # its signals. The caller then reaps the leader, which exited on its own;
    # a signal sent after that reap addresses a group id the kernel may have
    # handed to another process.
    spawned = _real_spawn(
        [sys.executable, "-c", "import time; time.sleep(0.1)"], tmp_path, monkeypatch
    )
    late: list[int] = []
    delayed: list[bool] = []
    real_start = threading.Thread.start

    def interrupted_start(thread: threading.Thread) -> None:
        real_start(thread)
        if thread.name == "nspawn-group-kill" and not main_thread_alarm:
            _signal_the_main_thread()

    def slow_signal(pid: int, sig: int) -> None:
        if not delayed:
            delayed.append(True)
            time.sleep(0.5)
        if spawned._process.returncode is not None:
            late.append(sig)
        _REAL_KILLPG(pid, sig)

    try:
        monkeypatch.setattr(threading.Thread, "start", interrupted_start)
        monkeypatch.setattr(child_module.os, "killpg", slow_signal)
        with pytest.raises(_Alarm):
            spawned.stop_then_kill(spawned.identity)
        assert main_thread_alarm == [True]
        assert spawned.wait(timeout=5) is not None
        for thread in threading.enumerate():
            if thread.name == "nspawn-group-kill":
                thread.join(timeout=5)
        assert late == []
    finally:
        _force_kill_group(spawned.identity.pid)


def test_stop_then_kill_wraps_a_recheck_failure_on_an_unsupported_platform(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = ProcessIdentity(pid=999_999, boot_id="c" * 8, start_ticks=3)
    process = _FakePopen(pid=999_999, stdout=io.BytesIO(), stderr=io.BytesIO())
    child = SpawnedChild(process, identity, object(), object(), tmp_path)  # type: ignore[arg-type]
    monkeypatch.setattr(process_identity_module, "_platform", lambda: "win32")

    with pytest.raises(ChildIdentityError) as caught:
        child.stop_then_kill(identity)

    message = str(caught.value)
    assert "process 999999 identity could not be rechecked" in message
    assert "stale process identity cannot be verified on this platform" in message


def test_concurrent_session_id_isolation() -> None:
    """Dispatches without explicit session_id generate unique version-4 UUIDs."""
    s1 = claude_child.mint_session_id()
    s2 = claude_child.mint_session_id()
    assert s1 != s2
    argv1 = compose_child_argv("/usr/bin/claude", session_id=s1)
    argv2 = compose_child_argv("/usr/bin/claude", session_id=s2)
    assert f"--session-id={s1}" in argv1
    assert f"--session-id={s2}" in argv2
    assert f"--session-id={s1}" not in argv2


def test_read_only_child_bootstrap_success(tmp_path: Path) -> None:
    """Read-only child includes nmemory_context_show in allowedTools and composes clean argv."""
    from nspawn.child import READ_ONLY_MCP_TOOLS, compose_child_argv

    assert "mcp__plugin_nott_nmemory__nmemory_context_show" in READ_ONLY_MCP_TOOLS
    argv = compose_child_argv(
        "/usr/bin/claude",
        read_only=True,
        read_paths=[str(tmp_path)],
    )
    assert "--restricted" in argv
    assert "--permission-mode" in argv
    assert "dontAsk" in argv
    allowed_idx = argv.index("--allowedTools")
    assert "mcp__plugin_nott_nmemory__nmemory_context_show" in argv[allowed_idx + 1]
    tools_idx = argv.index("--tools")
    assert "Read" in argv[tools_idx + 1]
    assert "SendMessage" in argv[tools_idx + 1]


# --- the account credential reaches the child (end of the path L3 left open)


def _claude_provider_with_secrets(tmp_path: Path) -> Provider:
    secrets = tmp_path / "claude"
    secrets.mkdir()
    # the real file ends in a newline; an env var with \n silently breaks auth
    (secrets / "conta-a.token").write_text(synthetic_token("test_child:2") + "\n")
    return Provider(name="claude", secrets=secrets)


def test_the_account_credential_reaches_the_child_as_the_oauth_token(tmp_path: Path):
    # The credential was read ONLY to build the redaction needles and discarded:
    # the child was born with nothing to authenticate with, and claude answered
    # "OAuth session expired". Observed on 2026-08-28 against the real binary.
    provider = _claude_provider_with_secrets(tmp_path)
    env, needles = build_child_env(provider, "conta-a")
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == synthetic_token("test_child:2")
    assert not needles.is_empty()


def test_the_injected_token_carries_no_trailing_newline(tmp_path: Path):
    provider = _claude_provider_with_secrets(tmp_path)
    env, _ = build_child_env(provider, "conta-a")
    assert "\n" not in env["CLAUDE_CODE_OAUTH_TOKEN"]
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == env["CLAUDE_CODE_OAUTH_TOKEN"].strip()


def test_a_provider_that_declares_its_own_credential_env_is_not_overwritten(
    tmp_path: Path,
):
    # A proxy provider already delivers a credential through its own env block
    # (ANTHROPIC_AUTH_TOKEN). Injecting a second path would give TWO
    # credentials to the same child, and whichever wins would be an accident of order.
    secrets = tmp_path / "px"
    secrets.mkdir()
    (secrets / "conta-a.token").write_text("chave-de-proxy-exemplo\n")
    provider = Provider(
        name="codex1",
        secrets=secrets,
        env={"ANTHROPIC_AUTH_TOKEN": "$NOTT_TEST_PROXY_KEY"},
    )
    env, _ = build_child_env(provider, "conta-a")
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env


def test_the_flag_door_holds_even_when_a_plugin_dir_is_handed_over():
    # LD-4 wants the child to see nMEMORY, and the obvious route would be
    # --mcp-config. It is FORBIDDEN: it is behind the same door as
    # --dangerously-skip-permissions and --add-dir, because a .mcp.json names
    # commands to execute, and handing one to the child widens its authority.
    # plugin_dir is the sanctioned channel; the door stays closed.
    argv = compose_child_argv(
        "/usr/bin/claude", plugin_dir="/home/o/.claude/plugins/nott"
    )
    assert "--plugin-dir" in argv
    for forbidden in FORBIDDEN_CLAUDE_FLAGS:
        assert forbidden not in argv


def test_a_plugin_dir_child_is_not_locked_out_of_its_own_plugins_servers():
    # --strict-mcp-config tells claude to ignore ALL MCP config except what is
    # passed via --mcp-config, which is a forbidden flag. The two together guaranteed
    # `mcp_servers: []` by construction, and LD-4 requires the child to see
    # nMEMORY. Measured on 2026-08-28 under the sealed home: WITHOUT the strict flag,
    # the plugin's six servers come in and NONE of the operator's thirteen do — the
    # sealing comes from the sealed CLAUDE_CONFIG_DIR, not from it.
    argv = compose_child_argv("/usr/bin/claude", plugin_dir="/home/o/.nott/plugin")
    assert "--plugin-dir" in argv
    assert "--strict-mcp-config" not in argv


def test_a_child_with_no_plugin_dir_keeps_the_strict_lock():
    # With no plugin declared there is no sanctioned channel at all, and the child goes back to
    # being hermetic: the strict flag closes the door nobody is going to use.
    argv = compose_child_argv(
        "/usr/bin/claude",
    )
    assert "--strict-mcp-config" in argv


def test_the_account_window_wins_and_carries_the_second_spelling(
    tmp_path: Path,
) -> None:
    """The context window belongs to the HOST, and the roster says it ONCE.

    MEASURED on 2026-09-20: qwen's three machines serve 245760, 65536 and 32768
    tokens, and the roster said 200000 for all three. On m1, six times the real
    window, the client answered `500 no user query found in messages` — the
    user's message was pushed out of the window.

    Two consequences, both here: the account declares ITS OWN window and wins over
    `[provider.env]`, and `CLAUDE_AUTO_COMPACT_WINDOW` is no longer written directly,
    now derived from the spelling the owner writes.
    """
    token = tmp_path / "token"
    token.write_text("another-real-credential-value-1234567890")
    provider = Provider(
        name="qwen",
        kind="claude",
        token_file=token,
        env={"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "200000"},
        accounts={
            "m1": NativeAccount(
                home=None,
                base_url="http://m1:11434",
                auth_env="ANTHROPIC_AUTH_TOKEN",
                env={"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "32768"},
            )
        },
    )
    env, _ = build_child_env(provider, "m1", base_environ={"HOME": "/h"})
    assert env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "32768"
    assert env["CLAUDE_AUTO_COMPACT_WINDOW"] == "32768"


def test_a_provider_window_still_reaches_an_account_that_declares_none(
    tmp_path: Path,
) -> None:
    """Valid progress: the account with no window of its own inherits the provider's, and the
    second spelling keeps following the first."""
    token = tmp_path / "token"
    token.write_text("another-real-credential-value-1234567890")
    provider = Provider(
        name="qwen",
        kind="claude",
        token_file=token,
        env={"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "245760"},
        accounts={
            "server": NativeAccount(
                home=None,
                base_url="http://127.0.0.1:18020",
                auth_env="ANTHROPIC_AUTH_TOKEN",
            )
        },
    )
    env, _ = build_child_env(provider, "server", base_environ={"HOME": "/h"})
    assert env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "245760"
    assert env["CLAUDE_AUTO_COMPACT_WINDOW"] == "245760"


def test_a_routed_child_environment_is_pinned_whole(tmp_path: Path) -> None:
    """Every variable a claude-kind route sets, at once: the ambient without its
    credentials and route names, the account's endpoint and model, the pick's fast
    lane and effort, the roster's env under the account's own, the second window
    spelling, the credential and the run marker. Nothing else rides along."""
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "m5.token").write_text("fixture-token-m5\n")
    provider = Provider(
        name="qwen",
        kind="claude",
        secrets=secrets,
        accounts={
            "m5": NativeAccount(
                home=None,
                base_url="http://m5.invalid:11434",
                auth_env="ANTHROPIC_AUTH_TOKEN",
                model="qwen3.8:27b",
                env={"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "65536"},
            )
        },
    )
    env, _ = build_child_env(
        provider,
        "m5",
        defaults_env={
            "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "200000",
            "ENABLE_TOOL_SEARCH": "false",
            "API_TIMEOUT_MS": "$NOTT_TEST_TIMEOUT",
            "NOTT_TEST_UNSET": "$NOTT_TEST_NOTHING",
        },
        model="qwen-provider-model",
        model_fast="qwen-provider-fast",
        effort="low",
        ambient={
            "HOME": "/h",
            "NOTT_TEST_TIMEOUT": "600000",
            "ANTHROPIC_BASE_URL": "https://inherited.invalid",
            "ANTHROPIC_SMALL_FAST_MODEL": "inherited",
            "OTHER_API_KEY": "fixture-ambient-key",
        },
        run_id="run-1",
    )
    model = "qwen3.8:27b"
    assert env == {
        "HOME": "/h",
        "NOTT_TEST_TIMEOUT": "600000",
        "ANTHROPIC_BASE_URL": "http://m5.invalid:11434",
        "ANTHROPIC_MODEL": model,
        "ANTHROPIC_DEFAULT_OPUS_MODEL": model,
        "ANTHROPIC_DEFAULT_SONNET_MODEL": model,
        "CLAUDE_CODE_SUBAGENT_MODEL": model,
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": "qwen-provider-fast",
        "CLAUDE_CODE_EFFORT_LEVEL": "low",
        "ENABLE_TOOL_SEARCH": "false",
        "API_TIMEOUT_MS": "600000",
        "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "65536",
        "CLAUDE_AUTO_COMPACT_WINDOW": "65536",
        "ANTHROPIC_AUTH_TOKEN": "fixture-token-m5",
        "NOTT_NSPAWN_RUN_ID": "run-1",
    }


def test_the_read_only_cage_is_the_only_plugin_dir_the_child_gets():
    """The read-only cage rewrites `.mcp.json`; the whole catalog does not get in.

    MEASURED on run f28f47db on 2026-09-20: the child of an `nspawn_review_start` was born
    with TWO `--plugin-dir` — the full catalog, coming from the roster's
    `plugins` via `declared_args`, and the filtered directory the supervisor
    builds with a `.mcp.json` reduced to nmemory `--read-only`. Mounting both
    undoes the filter: what was holding the cage was only `--allowedTools`.
    A defense that depends on a single layer is not defense in depth.
    """
    argv = compose_child_argv(
        "/usr/bin/claude",
        read_only=True,
        plugin_dir="/run/nspawn/abc/read_only_plugin",
        declared_args=["--plugin-dir", "/home/o/plugins/nott"],
    )
    dirs = [argv[i + 1] for i, a in enumerate(argv) if a == "--plugin-dir"]
    assert dirs == ["/run/nspawn/abc/read_only_plugin"], dirs


def test_a_writable_child_keeps_every_plugin_the_owner_declared():
    """Valid progress: outside the cage, filtering only removes the DUPLICATE.

    A roster with several `plugins` still mounts all of them; the only one that falls is
    the one that repeats the directory the supervisor already passes explicitly.
    """
    argv = compose_child_argv(
        "/usr/bin/claude",
        plugin_dir="/home/o/plugins/nott",
        declared_args=[
            "--plugin-dir",
            "/home/o/plugins/nott",
            "--plugin-dir",
            "/home/o/plugins/outro",
        ],
    )
    dirs = [argv[i + 1] for i, a in enumerate(argv) if a == "--plugin-dir"]
    assert dirs == ["/home/o/plugins/outro", "/home/o/plugins/nott"], dirs


def test_the_roster_declared_args_reach_the_child_argv():
    # `[defaults].args` from the roster never reached argv: the supervisor did
    # not pass resolved.args, so the command declared by the owner — including
    # --append-system-prompt-file with the canon — was INERT, and the child
    # was born without its system prompt. Measured on 2026-08-28.
    argv = compose_child_argv(
        "/usr/bin/claude",
        declared_args=[
            "--plugin-dir",
            "/p",
            "--append-system-prompt-file",
            "/p/NOTT.md",
        ],
    )
    assert argv[argv.index("--append-system-prompt-file") + 1] == ("/p/NOTT.md")
    assert "--" not in argv  # the prompt rides on stdin


def test_a_declared_arg_can_never_smuggle_a_forbidden_flag():
    # Args come from the roster, which belongs to the owner, but the flags gate
    # applies to EVERY input: a --dangerously-skip-permissions declared there
    # would expand child authority through the backdoor.
    with pytest.raises(ChildEnvironmentError):
        compose_child_argv(
            "/usr/bin/claude", declared_args=["--dangerously-skip-permissions"]
        )


def test_the_child_inherits_the_operator_environment_not_a_sealed_empty_one(
    tmp_path: Path,
):
    # The owner recipe does not set CLAUDE_CONFIG_DIR: the child inherits its
    # ~/.claude, and with it permissions.defaultMode. Sealing the config dir in an
    # empty directory per run erased precisely that, and the child started
    # asking approval from no one — LD-3 died with the child saying "waiting
    # for your approval". The operator environment is the base; the provider
    # overrides above; the account credential overrides last.
    token = tmp_path / "token"
    token.write_text("a-real-credential-value-that-is-long-enough")
    provider = Provider(name="claude", token_file=token, env={"MEU": "do-provider"})
    ambiente = {
        "CLAUDE_CONFIG_DIR": "/home/o/.claude",
        "HOME": "/home/o",
        "MEU": "do-ambiente",
    }

    env, _ = build_child_env(provider, None, ambient=ambiente)

    assert env["CLAUDE_CONFIG_DIR"] == "/home/o/.claude"
    assert env["HOME"] == "/home/o"
    assert env["MEU"] == "do-provider"


def test_an_ambient_credential_never_survives_the_account_the_run_was_routed_to(
    tmp_path: Path,
):
    # Inheriting the environment brings the OPERATOR's token along. A run routed
    # to another account that spent this token would bill the wrong account, so
    # the chosen credential always overwrites the environment.
    token = tmp_path / "token"
    token.write_text("o-token-DA-CONTA-escolhida-pelo-picker")
    provider = Provider(name="claude", token_file=token)

    env, _ = build_child_env(
        provider, None, ambient={"CLAUDE_CODE_OAUTH_TOKEN": "o-token-do-OPERADOR"}
    )

    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "o-token-DA-CONTA-escolhida-pelo-picker"


def test_build_child_env_sets_the_six_model_variables_from_the_pick(
    tmp_path: Path,
) -> None:
    """The roster never spells out where the model goes: nSPAWN does, and the
    injected value beats one leaked from the parent's environment."""
    token = tmp_path / "token"
    token.write_text("a-real-credential-value-that-is-long-enough")
    provider = Provider(
        name="claude", token_file=token, env={"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "1"}
    )

    env, _ = build_child_env(
        provider,
        None,
        model="fable-5-1",
        model_fast="claude-haiku-4-5",
        effort="low",
        base_environ={},
        ambient={"ANTHROPIC_MODEL": "leaked-from-the-parent"},
    )

    assert env["ANTHROPIC_MODEL"] == "fable-5-1"
    assert env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "fable-5-1"
    assert env["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "fable-5-1"
    assert env["CLAUDE_CODE_SUBAGENT_MODEL"] == "fable-5-1"
    assert env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "claude-haiku-4-5"
    assert env["CLAUDE_CODE_EFFORT_LEVEL"] == "low"


def test_a_literal_model_in_the_roster_still_wins_and_no_effort_sets_nothing(
    tmp_path: Path,
) -> None:
    token = tmp_path / "token"
    token.write_text("a-real-credential-value-that-is-long-enough")
    provider = Provider(name="kimi", token_file=token, env={"ANTHROPIC_MODEL": "k2.7"})

    env, _ = build_child_env(
        provider, None, model="fable-5-1", effort=None, base_environ={}, ambient={}
    )

    assert env["ANTHROPIC_MODEL"] == "k2.7"
    assert env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "fable-5-1"
    # No `model_fast` falls back to the model, never to an empty HAIKU.
    assert env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "fable-5-1"
    assert "CLAUDE_CODE_EFFORT_LEVEL" not in env


@pytest.mark.parametrize(
    ("model", "fast", "tiers"),
    [
        ("haiku", "haiku", ("claude-haiku-5-5",) * 3),
        (
            "sonnet",
            "haiku",
            ("claude-sonnet-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"),
        ),
        ("opus", "haiku", ("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5")),
        (
            "opus[1m]",
            "haiku",
            ("claude-opus-5-5[1m]", "claude-sonnet-5-5", "claude-haiku-5-5"),
        ),
        (
            "claude-opus-5",
            "haiku",
            ("claude-opus-5", "claude-opus-5", "claude-opus-5"),
        ),
        (
            "sonnet",
            "claude-haiku-5-5",
            ("claude-sonnet-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"),
        ),
    ],
)
def test_family_requests_clamp_only_upward(
    tmp_path: Path,
    model: str,
    fast: str,
    tiers: tuple[str, str, str],
    claude_models: dict[str, str],
) -> None:
    """Higher tiers stay within the family. Haiku fan-out stays Haiku."""
    token = tmp_path / "token"
    token.write_text("a-test-credential-value-that-is-long-enough")
    names = tuple(
        f"ANTHROPIC_DEFAULT_{tier}_MODEL" for tier in ("OPUS", "SONNET", "HAIKU")
    )
    env, _ = build_child_env(
        Provider(
            name="claude",
            token_file=token,
            env={
                **dict.fromkeys(names, "claude-opus-5"),
                "CLAUDE_CODE_SUBAGENT_MODEL": "opus",
            }
            if model != "claude-opus-5"
            else {},
        ),
        None,
        model=model,
        model_fast=fast,
        base_environ={},
        ambient=dict.fromkeys(names, "parent-model-canary"),
    )
    assert tuple(env[name] for name in names) == tiers
    target = claude_models.get(model.removesuffix("[1m]"), model)
    target += "[1m]" if model.endswith("[1m]") else ""
    if model.removesuffix("[1m]") in {"opus", "sonnet", "haiku"}:
        assert env["ANTHROPIC_DEFAULT_FABLE_MODEL"] == target
    assert env["ANTHROPIC_MODEL"] == target
    if model == "claude-opus-5" or model.removesuffix("[1m]") == "haiku":
        assert env["CLAUDE_CODE_SUBAGENT_MODEL"] == target
    else:
        assert "CLAUDE_CODE_SUBAGENT_MODEL" not in env


# --- the credential target and the endpoint of the chosen account


_SEGREDO = synthetic_token("test_child:1")


def _provider_com_conta(
    tmp_path: Path, conta: NativeAccount | None = None, **campos: object
) -> Provider:
    secrets = tmp_path / "fugu"
    secrets.mkdir()
    (secrets / "conta-a.token").write_text(f"{_SEGREDO}\n")
    contas = {} if conta is None else {"conta-a": conta}
    return Provider(name="fugu", secrets=secrets, accounts=contas, **campos)  # type: ignore[arg-type]


def test_a_credencial_vai_para_o_auth_env_que_o_provider_declara(
    tmp_path: Path,
) -> None:
    # Claude Code talking to a non-Anthropic gateway reads the same
    # credential from ANTHROPIC_AUTH_TOKEN, not CLAUDE_CODE_OAUTH_TOKEN.
    provider = _provider_com_conta(tmp_path, auth_env="ANTHROPIC_AUTH_TOKEN")

    env, _ = build_child_env(provider, "conta-a", ambient={})

    assert env["ANTHROPIC_AUTH_TOKEN"] == _SEGREDO
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env


def test_sem_auth_env_declarado_a_credencial_fica_onde_sempre_esteve(
    tmp_path: Path,
) -> None:
    provider = _provider_com_conta(tmp_path)

    env, _ = build_child_env(provider, "conta-a", ambient={})

    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == _SEGREDO
    assert "ANTHROPIC_AUTH_TOKEN" not in env


def test_o_alias_ausente_cai_na_primeira_DECLARADA_nao_na_primeira_do_alfabeto(
    tmp_path: Path,
) -> None:
    """The same order the picker uses, in the two places that resolve it.

    The picker already breaks ties by declared order; `_selected_account` and
    `resolve_native_account` kept using `sorted()`. With `server`
    declared first and `m5` sorting first, a missing alias gave different
    accounts depending on who asked — two truths about the same choice.
    """
    from nspawn.owner_roster import NativeAccount

    secrets = tmp_path / "qwen"
    secrets.mkdir()
    for alias in ("server", "m5"):
        (secrets / f"{alias}.token").write_text(f"{_SEGREDO}\n")
    provider = Provider(
        name="qwen",
        accounts={
            "server": NativeAccount(
                home=None,
                base_url="http://s.invalid",
                model="qwen3.8-27b",
                auth_env="ANTHROPIC_AUTH_TOKEN",
            ),
            "m5": NativeAccount(
                home=None,
                base_url="http://m.invalid",
                model="qwen3.8:27b",
                auth_env="ANTHROPIC_AUTH_TOKEN",
            ),
        },
        secrets=secrets,
    )

    env, _ = build_child_env(provider, None, ambient={})
    assert env["ANTHROPIC_BASE_URL"] == "http://s.invalid"
    assert provider.resolve_native_account(None).base_url == "http://s.invalid"


def test_o_auth_env_da_conta_escolhida_ganha_do_provider(tmp_path: Path) -> None:
    # With declared accounts, the roster REFUSES an auth_env at provider level
    # (owner_roster.py:757 — the mixture does not say which of the two credentials
    # an account spends). Therefore the only way qwen delivers the token in
    # ANTHROPIC_AUTH_TOKEN is for the account to declare it, and the account must win.
    conta = NativeAccount(
        home=None,
        base_url="http://127.0.0.1:18020",
        auth_env="ANTHROPIC_AUTH_TOKEN",
    )
    provider = _provider_com_conta(tmp_path, conta)

    env, _ = build_child_env(provider, "conta-a", ambient={})

    assert env["ANTHROPIC_AUTH_TOKEN"] == _SEGREDO
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env


def test_sem_conta_declarada_o_auth_env_do_provider_continua_a_valer(
    tmp_path: Path,
) -> None:
    """Valid progress: the provider's destination is the shape fugu and zai use.

    The earlier version of this test built accounts AND a provider-level
    `auth_env`, a combination the parser REFUSES (`owner_roster.py:757`): it proved
    a branch no roster can produce. The reachable shape is the one
    `fugu` and `zai` use — one endpoint and one destination for the whole provider,
    accounts coming from the secrets folder.
    """
    provider = _provider_com_conta(tmp_path, auth_env="ANTHROPIC_AUTH_TOKEN")

    env, _ = build_child_env(provider, "conta-a", ambient={})

    assert env["ANTHROPIC_AUTH_TOKEN"] == _SEGREDO
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env


def test_uma_credencial_entregue_a_outra_variavel_continua_redigida(
    tmp_path: Path,
) -> None:
    # The one that bites in silence: changing the destination without bringing
    # the needles along would let the credential leak in full in the executor transcript.
    provider = _provider_com_conta(tmp_path, auth_env="ANTHROPIC_AUTH_TOKEN")

    env, needles = build_child_env(provider, "conta-a", ambient={})

    assert env["ANTHROPIC_AUTH_TOKEN"] == _SEGREDO
    assert _SEGREDO not in needles.redact(f"vazou {_SEGREDO} no stdout")


def test_o_base_url_da_conta_escolhida_vira_o_endpoint_do_filho(
    tmp_path: Path,
) -> None:
    conta = NativeAccount(
        home=Path("/home/o/.claude-fugu"), base_url="https://fugu.exemplo"
    )
    provider = _provider_com_conta(tmp_path, conta)

    env, _ = build_child_env(provider, "conta-a", ambient={})

    assert env["ANTHROPIC_BASE_URL"] == "https://fugu.exemplo"


def test_um_base_url_declarado_no_env_do_provider_ainda_vence_o_da_conta(
    tmp_path: Path,
) -> None:
    conta = NativeAccount(
        home=Path("/home/o/.claude-fugu"), base_url="https://fugu.exemplo"
    )
    provider = _provider_com_conta(
        tmp_path, conta, env={"ANTHROPIC_BASE_URL": "https://declarado.exemplo"}
    )

    env, _ = build_child_env(provider, "conta-a", ambient={})

    assert env["ANTHROPIC_BASE_URL"] == "https://declarado.exemplo"


def test_uma_conta_sem_base_url_nao_mexe_no_endpoint(tmp_path: Path) -> None:
    provider = _provider_com_conta(tmp_path, NativeAccount(home=Path("/home/o/.c")))

    env, _ = build_child_env(provider, "conta-a", ambient={})

    assert "ANTHROPIC_BASE_URL" not in env


def test_o_modelo_da_conta_escolhida_vence_o_do_provider(tmp_path: Path) -> None:
    # Alias None falls back to the only declared account, same roster rule.
    conta = NativeAccount(home=Path("/home/o/.claude-fugu"), model="glm-4.7")
    provider = _provider_com_conta(tmp_path, conta)

    env, _ = build_child_env(provider, None, model="fable-5-1", ambient={})

    assert env["ANTHROPIC_MODEL"] == "glm-4.7"
    assert env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "glm-4.7"
    assert env["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "glm-4.7"
    assert env["CLAUDE_CODE_SUBAGENT_MODEL"] == "glm-4.7"
    assert env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "glm-4.7"


def test_a_rota_de_gateway_medida_chega_inteira_ao_ambiente_do_filho(
    tmp_path: Path,
) -> None:
    # The combination measured today against the real client: local endpoint,
    # credential in ANTHROPIC_AUTH_TOKEN coming from file, gateway model.
    # CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS belongs to the ROSTER, not this code:
    # what is proven here is that nothing in account delivery overrides the env
    # declared by the provider.
    conta = NativeAccount(
        home=Path("/home/o/.claude-qwen"),
        base_url="http://127.0.0.1:18020",
        model="qwen3.8-27b",
    )
    provider = _provider_com_conta(
        tmp_path,
        conta,
        auth_env="ANTHROPIC_AUTH_TOKEN",
        env={"CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1"},
    )

    env, needles = build_child_env(provider, "conta-a", model="ignorado", ambient={})

    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:18020"
    assert env["ANTHROPIC_AUTH_TOKEN"] == _SEGREDO
    assert env["ANTHROPIC_MODEL"] == "qwen3.8-27b"
    assert env["CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS"] == "1"
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert _SEGREDO not in needles.redact(f"vazou {_SEGREDO} no stdout")


def test_stop_rejects_another_live_childs_valid_identity(tmp_path):
    first_dir, other_dir = tmp_path / "first", tmp_path / "other"
    first_dir.mkdir()
    other_dir.mkdir()
    argv = [sys.executable, "-c", "import time; time.sleep(30)"]
    first = spawn_child(
        argv, tmp_path, dict(os.environ), first_dir, SecretNeedles.none()
    )
    other = spawn_child(
        argv, tmp_path, dict(os.environ), other_dir, SecretNeedles.none()
    )
    try:
        with pytest.raises(ChildIdentityError):
            first.stop_then_kill(other.identity)
        assert other.poll() is None
        assert first.poll() is None
    finally:
        for owned in (first, other):
            _force_kill_group(owned.identity.pid)
            owned.wait(timeout=3)


@pytest.mark.parametrize("first_method", ["poll", "wait"])
@pytest.mark.parametrize("second_method", ["poll", "wait", "stop"])
def test_concurrent_exit_observers_keep_identity_pinned_until_group_cleanup(
    tmp_path, monkeypatch, first_method, second_method
):
    owned = spawn_child(
        [sys.executable, "-c", "import time; time.sleep(0.05)"],
        tmp_path,
        dict(os.environ),
        tmp_path,
        SecretNeedles.none(),
    )
    observed = threading.Event()
    release = threading.Event()
    real_waitid = os.waitid
    real_wait = owned._process.wait
    signals = []
    reaps = []
    outcomes = []
    failures = []

    def hold_observation(idtype, pid, options):
        result = real_waitid(idtype, pid, options)
        if result is not None:
            assert options & os.WNOWAIT
            assert process_identity_matches(owned.identity)
            observed.set()
            assert release.wait(timeout=3)
        return result

    def record_signal(pid, sig):
        assert process_identity_matches(owned.identity)
        signals.append((pid, sig))
        _REAL_KILLPG(pid, sig)

    def record_reap(timeout=None):
        assert signals == [
            (owned.identity.pid, signal.SIGSTOP),
            (owned.identity.pid, signal.SIGKILL),
        ]
        reaps.append(True)
        return real_wait(timeout=timeout)

    def invoke(method):
        try:
            if method == "stop":
                owned.stop_then_kill(owned.identity)
            elif method == "wait":
                outcomes.append(owned.wait(timeout=3))
            else:
                deadline = time.monotonic() + 3
                while (status := owned.poll()) is None:
                    assert time.monotonic() < deadline
                    time.sleep(0.005)
                outcomes.append(status)
        except ChildIdentityError as error:
            failures.append(error)

    monkeypatch.setattr(child_module.os, "waitid", hold_observation)
    monkeypatch.setattr(child_module.os, "killpg", record_signal)
    monkeypatch.setattr(owned._process, "wait", record_reap)
    first = threading.Thread(target=invoke, args=(first_method,))
    second = threading.Thread(target=invoke, args=(second_method,))
    try:
        first.start()
        assert observed.wait(timeout=3)
        second.start()
        release.set()
        first.join(timeout=3)
        second.join(timeout=3)
        assert not first.is_alive() and not second.is_alive()
        assert reaps == [True]
        assert outcomes == ([0] if second_method == "stop" else [0, 0])
        assert len(failures) == (1 if second_method == "stop" else 0)
        assert owned.poll() == 0
        assert not process_identity_matches(owned.identity)
    finally:
        release.set()
        first.join(timeout=3)
        if second.ident is not None:
            second.join(timeout=3)
        _force_kill_group(owned.identity.pid)
        owned.wait(timeout=3)


@pytest.mark.parametrize("failure", [ChildProcessError, PermissionError])
def test_exit_custody_observation_failure_refuses_without_signaling(
    tmp_path, monkeypatch, failure
):
    owned = spawn_child(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        tmp_path,
        dict(os.environ),
        tmp_path,
        SecretNeedles.none(),
    )
    real_waitid = os.waitid
    signals = []

    def refuse(*args):
        raise failure("test waitid refusal")

    monkeypatch.setattr(child_module.os, "waitid", refuse)
    monkeypatch.setattr(child_module.os, "killpg", lambda *args: signals.append(args))
    try:
        with pytest.raises(ChildIdentityError, match="exit custody"):
            owned.poll()
        assert signals == []
    finally:
        monkeypatch.setattr(child_module.os, "waitid", real_waitid)
        monkeypatch.setattr(child_module.os, "killpg", _REAL_KILLPG)
        owned.stop_then_kill(owned.identity)
        owned.wait(timeout=3)


def test_an_echild_no_reaper_explains_refuses_at_once(tmp_path, monkeypatch):
    # ECHILD while no reaper holds Popen's wait lock: nothing will store a
    # code, so the refusal is immediate and nothing reaps the live child.
    owned = spawn_child(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        tmp_path,
        dict(os.environ),
        tmp_path,
        SecretNeedles.none(),
    )

    def refuse(*args):
        raise ChildProcessError("test waitid refusal")

    monkeypatch.setattr(child_module.os, "waitid", refuse)
    try:
        started = time.monotonic()
        with pytest.raises(ChildIdentityError, match="exit custody"):
            owned.poll()
        assert time.monotonic() - started < 1.0
        assert owned._process.returncode is None
    finally:
        monkeypatch.undo()
        owned.stop_then_kill(owned.identity)
        owned.wait(timeout=3)


@pytest.mark.parametrize("method", ["poll", "wait"])
def test_exit_observation_cleans_descendants_before_reaping(tmp_path, method):
    pid_file = tmp_path / "descendant"
    script = """
import os, sys, time
pid = os.fork()
if pid == 0:
    time.sleep(30)
else:
    with open(sys.argv[1], 'w') as handle:
        handle.write(str(pid))
    raise SystemExit(7)
"""
    owned = spawn_child(
        [sys.executable, "-c", script, str(pid_file)],
        tmp_path,
        dict(os.environ),
        tmp_path,
        SecretNeedles.none(),
    )
    complete = False
    deadline = time.monotonic() + 3
    try:
        if method == "wait":
            assert owned.wait(timeout=3) == 7
        else:
            while (status := owned.poll()) is None:
                assert time.monotonic() < deadline
                time.sleep(0.005)
            assert status == 7
            # AGY waits for EOF after poll; cleanup must progress without wait().
            owned.stdout_pump.join(timeout=1)
            owned.stderr_pump.join(timeout=1)
        pid = int(pid_file.read_text())
        # killpg returning means SIGKILL was delivered, not that the kernel has
        # finished exit on every CPU. Spend only the existing three-second
        # deadline observing death; an unsignaled descendant still fails.
        while True:
            state = process_state(pid)
            if state is None or state[0] in ("Z", "X"):
                break
            assert time.monotonic() < deadline, "descendant survived group cleanup"
            time.sleep(0.005)
        assert not process_identity_matches(owned.identity)
        assert owned.stdout_pump.finished and owned.stderr_pump.finished
        complete = True
    finally:
        if not complete:
            if pid_file.exists():
                _force_kill_group(int(pid_file.read_text()))
            _force_kill_group(owned.identity.pid)
        owned.wait(timeout=3)


def test_child_socket_address():
    from nspawn.child import CALLER_SOCKET_ENV, child_socket_address

    assert (
        child_socket_address(
            1234, environ={CALLER_SOCKET_ENV: "/tmp/cc-socks/caller.sock"}
        )
        == "uds:/tmp/cc-socks/1234.sock"
    )
    assert child_socket_address(1234, environ={}) is None
    assert child_socket_address(1234, environ={CALLER_SOCKET_ENV: "noslash"}) is None


def test_appends_law_reads_a_relative_prompt_from_the_childs_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A relative --append-system-prompt-file is the child's: the claude CLI opens it
    from the child's cwd, so the law is looked for there, never in the supervisor's."""
    (tmp_path / "law.md").write_bytes(child_module.LAW_TITLE + b"\n")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert child_module.appends_law(["--append-system-prompt-file", "law.md"], tmp_path)


def test_the_plugin_law_begins_with_the_title_the_supervisor_detects() -> None:
    """`appends_law` knows the law by its first line. A retitled plugin/NOTT.md would
    be appended by the roster AND printed by the plugin's hook, twice, silently."""
    law = (workspace_path("plugin/NOTT.md")).read_bytes()
    assert law.startswith(child_module.LAW_TITLE)


def test_family_resolution_refuses_a_cross_family_catalogue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nspawn.owner_roster import model_env_build

    monkeypatch.setattr(
        claude_catalog, "catalog_read", lambda _: {"haiku": "claude-opus-5-5"}
    )
    with pytest.raises(claude_catalog.ModelCatalogError, match="requested family"):
        model_env_build("haiku", None, None, {})


def test_full_ids_do_not_promote_the_haiku_tier(
    tmp_path: Path, claude_models: dict[str, str]
) -> None:
    token = tmp_path / "token"
    token.write_text("test-credential-value-long-enough")
    env, _ = build_child_env(
        Provider(name="claude", token_file=token),
        None,
        model="claude-opus-5-5",
        model_fast="sonnet",
        base_environ={},
        ambient={},
    )
    assert env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "claude-haiku-5-5"


def test_unclassified_full_ids_clamp_every_tier(claude_models: dict[str, str]) -> None:
    from nspawn.owner_roster import model_env_build

    env: dict[str, str] = {}
    model_env_build("claude-custom-4", "sonnet", None, env)
    for tier in ("FABLE", "OPUS", "SONNET", "HAIKU"):
        assert env[f"ANTHROPIC_DEFAULT_{tier}_MODEL"] == "claude-custom-4"


@pytest.mark.parametrize("source", ["parent", "defaults", "provider", "account"])
@pytest.mark.parametrize("model", ["haiku", "claude-haiku-5-5"])
def test_small_fast_roster_values_cannot_promote_a_haiku_run(
    tmp_path: Path, claude_models: dict[str, str], source: str, model: str
) -> None:
    token = tmp_path / "token"
    token.write_text("test-credential-value-long-enough")
    override = {"ANTHROPIC_SMALL_FAST_MODEL": "claude-opus-5-5"}
    provider = Provider(
        name="claude",
        token_file=token,
        env=override if source == "provider" else {},
        accounts={
            "test": NativeAccount(
                home=None,
                env=override if source == "account" else {},
            )
        },
    )
    env, _ = build_child_env(
        provider,
        "test",
        model=model,
        model_fast="claude-opus-5-5",
        defaults_env=override if source == "defaults" else None,
        ambient=override if source == "parent" else {},
        base_environ={},
    )
    assert env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "claude-haiku-5-5"
    assert (
        env.get("ANTHROPIC_SMALL_FAST_MODEL", env["ANTHROPIC_DEFAULT_HAIKU_MODEL"])
        == "claude-haiku-5-5"
    )


def test_full_id_inference_refuses_a_cross_family_catalogue(
    claude_models: dict[str, str],
) -> None:
    from nspawn.owner_roster import model_env_build

    claude_models["opus"] = "claude-haiku-4-5"
    with pytest.raises(claude_catalog.ModelCatalogError, match="requested family"):
        model_env_build("claude-haiku-4-5", None, None, {})
