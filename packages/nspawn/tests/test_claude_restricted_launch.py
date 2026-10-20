"""claude_restricted: the fail-closed launch composer for a Claude native writer.

The client measurements these tests hold the composer to live in the P-C E0 probe
(Claude Code 2.1.284 under `--restricted --settings`): garbled settings run the
tools uncaged, a linked worktree's common gitdir stays readable, tools inherit the
launch environment, and the per-user temp is shared across sessions.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

import pytest

from nspawn.claude_child import FORBIDDEN_CLAUDE_FLAGS
from nspawn.claude_restricted import (
    SCRATCH_ROOT,
    RestrictedLaunchRefused,
    compose_restricted_launch,
    make_scratch,
    verify_pinned_settings,
)
from nspawn.errors import nerr_of

TOKEN = "sk-ant-oat01-" + "x" * 24


@pytest.fixture
def layout(tmp_path: Path) -> dict[str, Path]:
    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    home = tmp_path / "home"
    for name in (".claude", ".claude2", ".codex", ".nott", "keep"):
        (home / name).mkdir(parents=True)
    (home / ".claude.json").write_text("{}")
    (home / ".claude.json.tmp.1.abc").write_text("{}")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o700)
    bwrap = bin_dir / "bwrap"
    bwrap.write_text("#!/bin/sh\n")
    bwrap.chmod(0o755)
    return {
        "checkout": checkout,
        "run_dir": run_dir,
        "home": home,
        "bin_dir": bin_dir,
        "scratch": scratch,
    }


def _compose(layout: dict[str, Path], **overrides: object):  # type: ignore[no-untyped-def]
    arguments: dict[str, object] = {
        "binary": Path("/opt/claude/bin/claude"),
        "checkout": layout["checkout"],
        "run_dir": layout["run_dir"],
        "host_auth": {"CLAUDE_CODE_OAUTH_TOKEN": TOKEN},
        "model": "claude-haiku-4-5",
        "effort": "low",
        "home": layout["home"],
        "path": str(layout["bin_dir"]),
        "tmpdir": layout["scratch"],
    }
    arguments.update(overrides)
    return compose_restricted_launch(**arguments)  # type: ignore[arg-type]


def _refused(error: pytest.ExceptionInfo[RestrictedLaunchRefused]) -> None:
    assert nerr_of(error.value) == 1116


def test_argv_is_the_measured_restricted_form(layout: dict[str, Path]) -> None:
    launch = _compose(layout, system_prompt_file=Path("/p/law.md"), session_id="s-1")
    argv = list(launch.argv)
    assert argv[0] == "/opt/claude/bin/claude"
    for pair in (
        ["--permission-mode", "dontAsk"],
        ["--tools", "Bash"],
        ["--allowedTools", "Bash"],
        ["--output-format", "stream-json"],
        ["--settings", str(launch.settings_path)],
        ["--append-system-prompt-file", "/p/law.md"],
        # No settings layer but the pinned one: not the user's, not the checkout's.
        ["--setting-sources", ""],
    ):
        index = argv.index(pair[0])
        assert argv[index : index + 2] == pair
    assert {"-p", "--restricted", "--strict-mcp-config", "--verbose"} <= set(argv)
    assert "--session-id=s-1" in argv
    mode_flags = [a for a in argv if a.partition("=")[0] == "--permission-mode"]
    assert mode_flags == ["--permission-mode"]
    assert not {a.partition("=")[0] for a in argv} & (
        set(FORBIDDEN_CLAUDE_FLAGS) - {"--permission-mode"}
    )
    # Model and effort travel in the env, never on argv.
    assert "--model" not in argv and "--effort" not in argv


def test_env_is_a_closed_allowlist(
    layout: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CANARY_ENV", "SECRET-ENV")
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/1/bus")
    launch = _compose(layout)
    env = dict(launch.env)
    assert env["TMPDIR"] == str(layout["scratch"])
    assert launch.cwd == layout["scratch"] / "work"
    assert launch.preface is not None and str(layout["checkout"]) in launch.preface
    # Measured on 2.1.284 (P-C runs p3 against t3, 2026-09-29): the same
    # settings hid $HOME and an outside canary without the scrub and leaked
    # both with it. The scrub switches off `denyRead: ["/"]`.
    assert "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB" not in env
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN
    assert env["ANTHROPIC_MODEL"] == "claude-haiku-4-5"
    assert env["CLAUDE_CODE_EFFORT_LEVEL"] == "low"
    assert env["HOME"] == str(layout["home"])
    # The client's own state (credentials, transcripts) lives in the run: the
    # pinned token is the only credential it can find.
    config = Path(env["CLAUDE_CONFIG_DIR"])
    assert config.parent == layout["run_dir"] and list(config.iterdir()) == []
    assert stat.S_IMODE(config.stat().st_mode) == 0o700
    assert env["PATH"] == str(layout["bin_dir"])
    assert "CANARY_ENV" not in env and "DBUS_SESSION_BUS_ADDRESS" not in env


@pytest.mark.parametrize(
    ("model", "tiers"),
    [
        ("haiku", ("claude-haiku-5-5",) * 3),
        ("sonnet", ("claude-sonnet-5-5", "claude-sonnet-5-5", "claude-haiku-5-5")),
        ("opus", ("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5")),
        ("opus[1m]", ("claude-opus-5-5[1m]", "claude-sonnet-5-5", "claude-haiku-5-5")),
        ("claude-haiku-4-5", ("claude-haiku-4-5",) * 3),
    ],
)
def test_family_requests_clamp_only_upward(
    layout: dict[str, Path],
    model: str,
    tiers: tuple[str, str, str],
    claude_models: dict[str, str],
) -> None:
    """Higher tiers stay within the family. Haiku fan-out stays Haiku."""
    env = dict(_compose(layout, model=model).env)
    assert (
        tuple(
            env[f"ANTHROPIC_DEFAULT_{tier}_MODEL"]
            for tier in ("OPUS", "SONNET", "HAIKU")
        )
        == tiers
    )
    target = claude_models.get(model.removesuffix("[1m]"), model)
    target += "[1m]" if model.endswith("[1m]") else ""
    if model.removesuffix("[1m]") in {"opus", "sonnet", "haiku"}:
        assert env["ANTHROPIC_DEFAULT_FABLE_MODEL"] == target
    assert env["ANTHROPIC_MODEL"] == target
    if model.removesuffix("[1m]") in {"opus", "sonnet"}:
        assert "CLAUDE_CODE_SUBAGENT_MODEL" not in env
    else:
        assert env["CLAUDE_CODE_SUBAGENT_MODEL"] == target


def test_writer_keeps_native_staging_outside_the_project(
    layout: dict[str, Path],
) -> None:
    """Claude 2.1.296 stages atomic writes under originalCwd/.claude.

    Native setup must not add paths to the project while a declared project
    write still reaches its exact checkout. No project dotfile is ignored.
    """
    checkout = layout["checkout"]
    module = checkout / "src" / "greeting" / "__init__.py"
    module.parent.mkdir(parents=True)
    module.write_text("def shout(text): return text.upper()\n")
    before = {path.relative_to(checkout) for path in checkout.rglob("*")}
    launch = _compose(layout)

    staging = launch.cwd / ".claude" / ".cc-writes"
    staging.mkdir(parents=True, mode=0o700)
    subprocess.run(
        [
            "/bin/sh",
            "-c",
            'printf "%s\\n" "$2" > "$1"',
            "write",
            str(module),
            "changed",
        ],
        cwd=launch.cwd,
        check=True,
    )

    assert module.read_text() == "changed\n"
    assert {path.relative_to(checkout) for path in checkout.rglob("*")} == before
    assert staging.is_relative_to(layout["scratch"])
    assert launch.preface is not None
    assert str(checkout) in launch.preface
    assert "working directory" in launch.preface
    assert "all project work" in launch.preface


@pytest.mark.parametrize("shape", ["symlink", "directory"])
def test_writer_refuses_a_planted_administrative_working_directory(
    layout: dict[str, Path], shape: str
) -> None:
    work = layout["scratch"] / "work"
    if shape == "symlink":
        work.symlink_to(layout["checkout"], target_is_directory=True)
    else:
        work.mkdir(mode=0o777)
    before = {
        path.relative_to(layout["checkout"]) for path in layout["checkout"].rglob("*")
    }
    with pytest.raises(RestrictedLaunchRefused) as error:
        _compose(layout)
    _refused(error)
    assert {
        path.relative_to(layout["checkout"]) for path in layout["checkout"].rglob("*")
    } == before


def test_the_scratch_directory_is_short_private_and_outside_any_run() -> None:
    """The client binds its sandbox bridge sockets under TMPDIR, so a deep run
    directory overruns AF_UNIX's 108-byte path (measured on 2.1.284: 30
    characters worked, 93 failed "to create bridge sockets")."""
    scratch = make_scratch()
    try:
        assert scratch.parent == SCRATCH_ROOT == Path("/var/tmp")
        assert len(str(scratch)) < len("/var/tmp/") + 24 and scratch.is_dir()
        assert stat.S_IMODE(scratch.lstat().st_mode) == 0o700
    finally:
        shutil.rmtree(scratch)


@pytest.mark.parametrize("shape", ["symlink", "open"])
def test_a_scratch_directory_not_privately_ours_is_refused(
    layout: dict[str, Path], shape: str
) -> None:
    other = layout["run_dir"].parent / "other"
    if shape == "symlink":
        other.mkdir(mode=0o700)
        other.rename(other.with_name("real"))
        other.symlink_to(other.with_name("real"))
    else:
        other.mkdir()
        other.chmod(0o777)
    with pytest.raises(RestrictedLaunchRefused) as error:
        _compose(layout, tmpdir=other)
    _refused(error)


def test_settings_are_pinned_and_cage_the_checkout(layout: dict[str, Path]) -> None:
    launch = _compose(layout)
    raw = launch.settings_path.read_bytes()
    assert stat.S_IMODE(launch.settings_path.stat().st_mode) == 0o600
    assert hashlib.sha256(raw).hexdigest() == launch.settings_sha256
    sandbox = json.loads(raw)["sandbox"]
    assert sandbox["enabled"] is True
    assert sandbox["failIfUnavailable"] is True
    assert sandbox["allowUnsandboxedCommands"] is False
    assert sandbox["autoAllowBashIfSandboxed"] is True
    assert sandbox["network"] == {"allowedDomains": [], "strictAllowlist": True}
    fs = sandbox["filesystem"]
    checkout, home = layout["checkout"], layout["home"]
    tmpdir = launch.env["TMPDIR"]
    assert fs["denyRead"][0] == "/"
    assert str(checkout) in fs["allowRead"] and tmpdir in fs["allowRead"]
    assert "/usr" in fs["allowRead"]
    assert str(home) not in fs["allowRead"]
    assert fs["allowWrite"] == [str(checkout), tmpdir]
    guarded = {
        str(checkout / ".git"),
        str(home / ".claude"),
        str(home / ".claude.json"),
        str(home / ".claude2"),
        str(home / ".codex"),
        str(home / ".nott"),
        "/run/user",
    }
    assert guarded <= set(fs["denyRead"]) and guarded <= set(fs["denyWrite"])
    assert str(home / "keep") not in fs["denyRead"]
    # A churned vendor temp file gone by exec time would fail bwrap.
    assert str(home / ".claude.json.tmp.1.abc") not in fs["denyRead"]
    denied = {entry["name"] for entry in sandbox["credentials"]["envVars"]}
    assert {entry["mode"] for entry in sandbox["credentials"]["envVars"]} == {"deny"}
    assert {
        "CLAUDE_CODE_OAUTH_TOKEN",
        "DBUS_SESSION_BUS_ADDRESS",
        "XDG_RUNTIME_DIR",
    } <= denied
    # The pin holds for the file as written.
    verify_pinned_settings(launch.settings_path, launch.settings_sha256)


def test_a_review_writes_nothing_but_its_scratch_and_reads_its_history(
    layout: dict[str, Path],
) -> None:
    """The reviewer's cage: the candidate and its `.git` are read, never written;
    extra trees are read only when named."""
    extra = layout["run_dir"].parent / "extra"
    extra.mkdir()
    launch = _compose(layout, writable=False, read_paths=(extra,))
    fs = json.loads(launch.settings_path.read_bytes())["sandbox"]["filesystem"]
    checkout, tmpdir = layout["checkout"], launch.env["TMPDIR"]
    assert fs["allowWrite"] == [tmpdir]
    assert {str(checkout), str(checkout / ".git")} <= set(fs["denyWrite"])
    assert str(checkout / ".git") not in fs["denyRead"]
    assert {str(checkout), str(extra), tmpdir} <= set(fs["allowRead"])
    assert list(launch.argv[launch.argv.index("--tools") :][:2]) == ["--tools", "Bash"]
    # The client's sandbox creates its dotfile masks (.gitconfig, .bashrc, ...)
    # in its working directory, which it cannot in a read-only candidate
    # (measured on 2.1.284: "bwrap: Can't create file at <checkout>/.gitconfig").
    # The reviewer works in its scratch and is told where the candidate is.
    assert launch.cwd == layout["scratch"] / "work" and launch.cwd.is_dir()
    assert launch.preface is not None and str(checkout) in launch.preface


def test_a_review_of_a_linked_worktree_reads_its_common_gitdir(
    layout: dict[str, Path],
) -> None:
    """A linked worktree's history lives in the main checkout's gitdir: the
    reviewer reads both it and the worktree's own gitdir, and writes neither."""
    common = layout["run_dir"].parent / "main" / ".git"
    gitdir = common / "worktrees" / "wt"
    gitdir.mkdir(parents=True)
    (gitdir / "commondir").write_text("../..\n")
    git = layout["checkout"] / ".git"
    git.rmdir()
    git.write_text(f"gitdir: {gitdir}\n")
    launch = _compose(layout, writable=False)
    fs = json.loads(launch.settings_path.read_bytes())["sandbox"]["filesystem"]
    assert {str(gitdir), str(common)} <= set(fs["allowRead"])
    assert {str(gitdir), str(common)} <= set(fs["denyWrite"])


def test_a_review_of_a_snapshot_without_git_is_composed(
    layout: dict[str, Path],
) -> None:
    """The workflow freezes its candidate as a snapshot without history."""
    (layout["checkout"] / ".git").rmdir()
    launch = _compose(layout, writable=False)
    fs = json.loads(launch.settings_path.read_bytes())["sandbox"]["filesystem"]
    assert fs["allowWrite"] == [launch.env["TMPDIR"]]


@pytest.mark.parametrize("shape", ["file", "symlink", "absent"])
def test_a_checkout_without_its_own_git_directory_is_refused(
    layout: dict[str, Path], shape: str
) -> None:
    git = layout["checkout"] / ".git"
    git.rmdir()
    if shape == "file":
        git.write_text("gitdir: /elsewhere/.git/worktrees/wt\n")
    elif shape == "symlink":
        target = layout["run_dir"].parent / "real-git"
        target.mkdir()
        git.symlink_to(target)
    with pytest.raises(RestrictedLaunchRefused) as error:
        _compose(layout)
    _refused(error)
    assert not (layout["run_dir"] / "claude-settings.json").exists()


@pytest.mark.parametrize("writable", [True, False], ids=["writer", "review"])
def test_a_symlinked_checkout_is_refused(
    layout: dict[str, Path], writable: bool
) -> None:
    alias = layout["run_dir"].parent / "alias"
    alias.symlink_to(layout["checkout"])
    with pytest.raises(RestrictedLaunchRefused) as error:
        _compose(layout, checkout=alias, writable=writable)
    _refused(error)


def test_unresolvable_bwrap_is_refused(layout: dict[str, Path]) -> None:
    empty = layout["run_dir"].parent / "empty"
    empty.mkdir()
    with pytest.raises(RestrictedLaunchRefused) as error:
        _compose(layout, path=str(empty))
    _refused(error)


def test_host_auth_may_carry_only_credential_names(layout: dict[str, Path]) -> None:
    with pytest.raises(RestrictedLaunchRefused) as error:
        _compose(layout, host_auth={"PATH": "/tmp/evil"})
    _refused(error)


def test_a_launch_without_a_credential_is_refused(layout: dict[str, Path]) -> None:
    """An account whose credential arrives under no known name refuses here, not
    as an authentication error from inside the cage."""
    with pytest.raises(RestrictedLaunchRefused, match="no credential") as error:
        _compose(layout, host_auth={})
    _refused(error)


def test_tampered_settings_are_refused(layout: dict[str, Path]) -> None:
    launch = _compose(layout)
    document = json.loads(launch.settings_path.read_bytes())
    document["sandbox"]["filesystem"]["allowWrite"].append("/")
    launch.settings_path.write_bytes(json.dumps(document).encode())
    with pytest.raises(RestrictedLaunchRefused) as error:
        verify_pinned_settings(launch.settings_path, launch.settings_sha256)
    _refused(error)


def test_garbled_settings_are_refused_even_with_a_matching_pin(
    layout: dict[str, Path],
) -> None:
    path = layout["run_dir"] / "garbled.json"
    garbled = b'{"sandbox": {"enabled": true,'
    path.write_bytes(garbled)
    path.chmod(0o600)
    with pytest.raises(RestrictedLaunchRefused) as error:
        verify_pinned_settings(path, hashlib.sha256(garbled).hexdigest())
    _refused(error)


@pytest.mark.parametrize(
    "drop",
    [
        ("failIfUnavailable",),
        ("allowUnsandboxedCommands",),
        ("network", "strictAllowlist"),
        ("filesystem", "denyRead"),
        ("credentials",),
    ],
)
def test_a_missing_required_key_is_refused(
    layout: dict[str, Path], drop: tuple[str, ...]
) -> None:
    launch = _compose(layout)
    document = json.loads(launch.settings_path.read_bytes())
    node = document["sandbox"]
    for key in drop[:-1]:
        node = node[key]
    del node[drop[-1]]
    raw = json.dumps(document).encode()
    launch.settings_path.write_bytes(raw)
    with pytest.raises(RestrictedLaunchRefused) as error:
        verify_pinned_settings(launch.settings_path, hashlib.sha256(raw).hexdigest())
    _refused(error)


def test_a_loosened_mode_is_refused(layout: dict[str, Path]) -> None:
    launch = _compose(layout)
    launch.settings_path.chmod(0o644)
    with pytest.raises(RestrictedLaunchRefused) as error:
        verify_pinned_settings(launch.settings_path, launch.settings_sha256)
    _refused(error)


def test_a_planted_settings_path_is_never_written_through(
    layout: dict[str, Path],
) -> None:
    victim = layout["run_dir"].parent / "victim.json"
    victim.write_text("{}")
    (layout["run_dir"] / "claude-settings.json").symlink_to(victim)
    with pytest.raises(RestrictedLaunchRefused) as error:
        _compose(layout)
    _refused(error)
    assert victim.read_text() == "{}"


# ---------------------------------------------------------------------------
# Native proof: the real pinned client under the composed launch.
# ---------------------------------------------------------------------------

_PROBE = """\
echo edited-by-model > file.txt
cat {outside} > leak.txt 2>&1
cat {home_canary} > home-leak.txt 2>&1
if [ -n "${{CLAUDE_CODE_OAUTH_TOKEN+x}}" ]; then echo present; else echo absent; fi > env.txt
ls -d /proc/[0-9]* | wc -l > proc.txt
"""


@pytest.mark.skipif(
    os.environ.get("NOTT_NSPAWN_LIVE_CLAUDE") != "1",
    reason="native proof: set NOTT_NSPAWN_LIVE_CLAUDE=1, NOTT_NSPAWN_LIVE_CLAUDE_BIN "
    "and NOTT_NSPAWN_LIVE_CLAUDE_TOKEN_FILE",
)
def test_live_pinned_client_honours_the_composed_cage() -> None:
    binary = Path(os.environ["NOTT_NSPAWN_LIVE_CLAUDE_BIN"])
    token_file = Path(os.environ["NOTT_NSPAWN_LIVE_CLAUDE_TOKEN_FILE"])
    token = token_file.read_text().strip()
    base = Path(tempfile.mkdtemp(prefix="pc-claude-launch-", dir="/var/tmp"))
    # A $HOME file no named deny covers: only `denyRead: ["/"]` hides it, so a
    # client default that reopens the root read (as the env scrub did) shows here.
    home_canary = Path.home() / f".pc-claude-launch-canary-{base.name}"
    scratch: Path | None = None
    try:
        home_canary.write_text("SECRET-HOME\n")
        outside = base / "outside" / "secret.txt"
        outside.parent.mkdir()
        outside.write_text("SECRET-OUTSIDE\n")
        checkout = base / "checkout"
        checkout.mkdir()
        subprocess.run(["git", "init", "-q", str(checkout)], check=True)
        (checkout / "file.txt").write_text("hello\n")
        # A settings layer the model could have planted in an earlier attempt:
        # it would switch `denyRead: ["/"]` off if the client read it.
        planted = json.dumps({"env": {"CLAUDE_CODE_SUBPROCESS_ENV_SCRUB": "1"}})
        (checkout / ".claude").mkdir()
        for name in ("settings.json", "settings.local.json"):
            (checkout / ".claude" / name).write_text(planted)
        (checkout / "probe.sh").write_text(
            _PROBE.format(outside=outside, home_canary=home_canary)
        )
        # As deep as an installed run's `~/.nott/state/nspawn/runs/<uuid>`.
        run_dir = base / ("run-" + "d" * 64)
        run_dir.mkdir()
        scratch = make_scratch()
        launch = compose_restricted_launch(
            binary=binary,
            checkout=checkout,
            run_dir=run_dir,
            host_auth={"CLAUDE_CODE_OAUTH_TOKEN": token},
            model="claude-haiku-4-5-20251001",
            tmpdir=scratch,
        )
        verify_pinned_settings(launch.settings_path, launch.settings_sha256)
        completed = subprocess.run(
            list(launch.argv),
            cwd=launch.cwd,
            env=dict(launch.env),
            input=f"{launch.preface}\n\nRun `cd {shlex.quote(str(checkout))} "
            "&& bash probe.sh` once with the Bash tool, then reply DONE.",
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        assert token not in completed.stdout and token not in completed.stderr
        assert completed.returncode == 0, completed.stderr[-2000:]
        init = json.loads(completed.stdout.splitlines()[0])
        assert init["tools"] == ["Bash"] and init["mcp_servers"] == []
        assert (checkout / "file.txt").read_text() == "edited-by-model\n"
        assert "SECRET-HOME" not in (checkout / "home-leak.txt").read_text()
        assert "SECRET-OUTSIDE" not in (checkout / "leak.txt").read_text()
        assert (checkout / "env.txt").read_text().strip() == "absent"
        assert int((checkout / "proc.txt").read_text()) < 20
        assert not (checkout / ".claude" / ".cc-writes").exists()
        # The client keeps its state in the run, never in the operator's home.
        transcripts = Path.home() / ".claude" / "projects"
        assert not (transcripts / str(checkout).replace("/", "-")).exists()
    finally:
        home_canary.unlink(missing_ok=True)
        if scratch is not None:
            shutil.rmtree(scratch, ignore_errors=True)
        shutil.rmtree(base, ignore_errors=True)


_REVIEW_PROBE = """\
echo edited-by-model > file.txt 2> "{scratch}/edit.txt" || echo denied >> "{scratch}/edit.txt"
git log --oneline -1 > "{scratch}/log.txt" 2>&1
touch .git/planted 2> "{scratch}/git.txt" || echo denied >> "{scratch}/git.txt"
cat {home_canary} > "{scratch}/leak.txt" 2>&1
"""


@pytest.mark.skipif(
    os.environ.get("NOTT_NSPAWN_LIVE_CLAUDE") != "1",
    reason="native proof: set NOTT_NSPAWN_LIVE_CLAUDE=1, NOTT_NSPAWN_LIVE_CLAUDE_BIN "
    "and NOTT_NSPAWN_LIVE_CLAUDE_TOKEN_FILE",
)
def test_live_pinned_client_honours_the_review_cage() -> None:
    """The reviewer reads the candidate and its history and writes none of it."""
    binary = Path(os.environ["NOTT_NSPAWN_LIVE_CLAUDE_BIN"])
    token = Path(os.environ["NOTT_NSPAWN_LIVE_CLAUDE_TOKEN_FILE"]).read_text().strip()
    base = Path(tempfile.mkdtemp(prefix="pc-claude-review-", dir="/var/tmp"))
    home_canary = Path.home() / f".pc-claude-review-canary-{base.name}"
    scratch = make_scratch()
    try:
        home_canary.write_text("SECRET-HOME\n")
        checkout = base / "checkout"
        checkout.mkdir()
        subprocess.run(["git", "init", "-q", str(checkout)], check=True)
        (checkout / "file.txt").write_text("hello\n")
        (checkout / "probe.sh").write_text(
            f"cd {checkout}\n"
            + _REVIEW_PROBE.format(home_canary=home_canary, scratch=scratch)
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(checkout),
                "-c",
                "user.email=p@x",
                "-c",
                "user.name=p",
                "commit",
                "-qam",
                "init",
                "--allow-empty",
            ],
            check=True,
        )
        subprocess.run(["git", "-C", str(checkout), "add", "."], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(checkout),
                "-c",
                "user.email=p@x",
                "-c",
                "user.name=p",
                "commit",
                "-qm",
                "candidate",
            ],
            check=True,
        )
        run_dir = base / ("run-" + "d" * 64)
        run_dir.mkdir()
        launch = compose_restricted_launch(
            binary=binary,
            checkout=checkout,
            run_dir=run_dir,
            host_auth={"CLAUDE_CODE_OAUTH_TOKEN": token},
            model="claude-haiku-4-5-20251001",
            tmpdir=scratch,
            writable=False,
        )
        completed = subprocess.run(
            list(launch.argv),
            cwd=launch.cwd,
            env=dict(launch.env),
            input=f"{launch.preface}\n\nRun `bash {checkout}/probe.sh` once with the "
            "Bash tool, then reply DONE.",
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr[-2000:]
        assert (checkout / "file.txt").read_text() == "hello\n"
        assert "denied" in (scratch / "edit.txt").read_text()
        assert "candidate" in (scratch / "log.txt").read_text()
        assert "denied" in (scratch / "git.txt").read_text()
        assert not (checkout / ".git" / "planted").exists()
        assert "SECRET-HOME" not in (scratch / "leak.txt").read_text()
    finally:
        home_canary.unlink(missing_ok=True)
        shutil.rmtree(base, ignore_errors=True)
        shutil.rmtree(scratch, ignore_errors=True)
