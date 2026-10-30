"""nSPAWN never launches a run on a credential it cannot provide.

Failure 2026-10-23: in one swarm of 39 Haiku runs, 19 failed with
ChildEnvironmentError (nERR-1112) and 0 tokens. The roster's API-key routes
(`auth_env = "ANTHROPIC_API_KEY"`) had no value for that variable in nSPAWN's
environment, the picker took them anyway, and each run died at the launch's
credential read before the walk moved on.

The supervisor, the roster file, the picker, the registry and the credential
reader are the real ones. Only the vendor binary is a fake process.
"""

from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path

import pytest
from supervisor_support import (
    FakeSpawnedChild,
    _classify_by_exit_code,
    _drain_worker_threads,
    _fake_installation,
    _fake_mcp_config,
    _spawn_real,
    _stop_every_reaper,
    _sv,
    registry,
    roster_home,
    worker_plugin,
)

from nspawn import picker, supervisor, terminal
from nspawn.owner_roster import Defaults, NativeAccount, Provider, Roster
from nspawn.registry import Registry
from nspawn.rows import RunPhase

__all__ = ["_drain_worker_threads", "_stop_every_reaper", "registry", "roster_home"]

HAIKU = "claude-haiku-5-5"
KEY = "ANTHROPIC_API_KEY"


def _roster(roster_home: Path, tmp_path: Path) -> None:
    """The default route `keyed` reads `ANTHROPIC_API_KEY`; the route `fleet`
    declares `gone`, whose token file is not there, before `main`, whose is."""
    secrets = tmp_path / "secrets"
    secrets.mkdir(mode=0o700)
    secrets.chmod(0o700)  # whatever the umask
    (secrets / "main.token").write_text("fixture-token-main\n", encoding="utf-8")
    (secrets / "main.token").chmod(0o600)
    (roster_home / ".nott" / "spawn.toml").write_text(
        f'[defaults]\nplugins = ["{worker_plugin(tmp_path)}"]\n\n'
        '[[provider]]\nname = "keyed"\nkind = "claude"\ndefault = true\n'
        f'auth_env = "{KEY}"\nmodel = "{HAIKU}"\nmodel_fast = "{HAIKU}"\n\n'
        '[[provider]]\nname = "fleet"\nkind = "claude"\n'
        f'secrets = "{secrets}"\nmodel = "{HAIKU}"\nmodel_fast = "{HAIKU}"\n'
        '[provider.accounts.gone]\nauth_env = "CLAUDE_CODE_OAUTH_TOKEN"\n'
        '[provider.accounts.main]\nauth_env = "CLAUDE_CODE_OAUTH_TOKEN"\n',
        encoding="utf-8",
    )


@pytest.fixture
def launches(
    monkeypatch: pytest.MonkeyPatch, roster_home: Path, tmp_path: Path
) -> list[str]:
    """The real credential reader (`child.build_child_env`) and a fake vendor:
    each child started records the credential it was handed."""
    monkeypatch.delenv(KEY, raising=False)
    _roster(roster_home, tmp_path)
    monkeypatch.setattr(
        supervisor.claude_child,
        "resolve_claude_binary",
        lambda **k: Path("/usr/bin/claude"),
    )
    monkeypatch.setattr(
        supervisor.claude_child, "inspect_claude_installation", _fake_installation
    )
    monkeypatch.setattr(
        supervisor.claude_child, "inspect_child_mcp_config", _fake_mcp_config
    )
    monkeypatch.setattr(
        supervisor.child, "compose_child_argv", lambda *a, **k: ["claude", "-p"]
    )
    started: list[str] = []

    def spawn(
        _argv: object, _cwd: object, env: dict[str, str], *a: object, **k: object
    ) -> FakeSpawnedChild:
        started.append(env.get("CLAUDE_CODE_OAUTH_TOKEN", ""))
        return FakeSpawnedChild(_spawn_real("true"))

    monkeypatch.setattr(supervisor.child, "spawn_child", spawn)
    monkeypatch.setattr(
        supervisor.terminal, "classify_terminal_run", _classify_by_exit_code
    )
    return started


def _start(sv: supervisor.Supervisor, cwd: Path, provider: str | None = None) -> str:
    return sv._admit(
        "read it",
        str(cwd),
        provider=provider,
        model=None,
        effort=None,
        execution_class=None,
        session_id=None,
        timeout_ms=0,
        claim_worktree=False,
        read_only=True,
    ).run_id


def test_a_swarm_never_runs_on_a_route_whose_credential_is_absent(
    registry: Registry, tmp_path: Path, launches: list[str]
) -> None:
    """Every run of the walk lands on `fleet/main`: neither the unset
    `ANTHROPIC_API_KEY` route nor the account without its token file is
    launched, and the run's warning names each by its variable or file."""
    sv = _sv(registry, tmp_path)
    try:
        runs = [_start(sv, tmp_path) for _ in range(3)]
        rows = [sv.status(run_id, timeout_ms=10_000) for run_id in runs]
    finally:
        sv.shutdown()
    assert [(r.executed_provider, r.executed_account) for r in rows] == [
        ("fleet", "main")
    ] * 3
    assert [r.phase for r in rows] == [RunPhase.COMPLETED] * 3
    assert launches == ["fixture-token-main"] * 3
    for row in rows:
        assert row.warning is not None
        assert f'route "keyed": auth_env {KEY!r} is unset or empty' in row.warning
        assert 'account "gone" of route "fleet": gone.token is not there' in row.warning
        assert "fixture-token" not in row.warning


@pytest.mark.parametrize(
    ("pin", "named", "fix"),
    [
        (
            "keyed",
            f'route "keyed": auth_env {KEY!r} is unset or empty',
            f"set {KEY} in the environment nSPAWN starts with, then restart nSPAWN",
        ),
        (
            "gone",
            'account "gone" of route "fleet": gone.token is not there',
            "put the account's token in it",
        ),
    ],
)
def test_a_pin_on_an_absent_credential_is_refused_before_any_run(
    registry: Registry,
    tmp_path: Path,
    launches: list[str],
    pin: str,
    named: str,
    fix: str,
) -> None:
    sv = _sv(registry, tmp_path)
    try:
        with pytest.raises(picker.PickerError) as refused:
            _start(sv, tmp_path, provider=pin)
    finally:
        sv.shutdown()
    assert named in refused.value.message
    assert fix in refused.value.message
    assert not registry.list_runs()
    assert launches == []


def test_a_route_whose_credential_is_set_keeps_its_place(
    registry: Registry,
    tmp_path: Path,
    launches: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the variable set, the default route runs as it did, with no note."""
    monkeypatch.setenv(KEY, "fixture-api-key")
    sv = _sv(registry, tmp_path)
    try:
        row = sv.status(_start(sv, tmp_path), timeout_ms=10_000)
    finally:
        sv.shutdown()
    assert (row.executed_provider, row.executed_account, row.phase) == (
        "keyed",
        None,
        RunPhase.COMPLETED,
    )
    assert row.warning is None or "keyed" not in row.warning


def test_a_pinned_route_runs_on_its_account_that_has_a_credential(
    registry: Registry, tmp_path: Path, launches: list[str]
) -> None:
    sv = _sv(registry, tmp_path)
    try:
        row = sv.status(_start(sv, tmp_path, provider="fleet"), timeout_ms=10_000)
    finally:
        sv.shutdown()
    assert (row.executed_provider, row.executed_account) == ("fleet", "main")
    assert row.warning is not None and 'account "gone"' in row.warning
    assert launches == ["fixture-token-main"]


def _views(tmp_path: Path) -> Roster:
    return Roster(
        defaults=Defaults(),
        providers=[
            Provider(name="keyed", kind="claude", auth_env=KEY, model=HAIKU),
            Provider(
                name="fleet",
                kind="claude",
                secrets=tmp_path,
                model=HAIKU,
                accounts={"gone": NativeAccount(home=None, auth_env=KEY)},
            ),
        ],
    )


def test_no_route_with_a_credential_is_refused_naming_each_one(
    tmp_path: Path,
) -> None:
    with pytest.raises(picker.PickerError) as refused:
        supervisor._with_credentials(_views(tmp_path), None, {})
    assert refused.value.message == (
        "no declared route has a credential nSPAWN can read: "
        f'route "keyed": auth_env {KEY!r} is unset or empty: set {KEY} in the '
        "environment nSPAWN starts with, then restart nSPAWN; "
        'account "gone" of route "fleet": gone.token is not there: put the '
        "account's token in it"
    )
    with pytest.raises(picker.PickerError, match='^account "gone" of route "fleet"'):
        supervisor._with_credentials(_views(tmp_path), "fleet", {})


def test_a_named_only_route_is_named_only_when_pinned(tmp_path: Path) -> None:
    """An unpinned run never takes a named_only route, so it is neither read
    nor named there; a pin on it is refused by its words."""
    (tmp_path / "main.token").write_text("fixture-token-main\n")
    (tmp_path / "main.token").chmod(0o600)
    roster = _views(tmp_path)
    roster.providers[0].named_only = True
    roster.providers[1].accounts["main"] = NativeAccount(home=None, auth_env=KEY)
    view, note = supervisor._with_credentials(roster, None, {})
    assert note == (
        'account "gone" of route "fleet": gone.token is not there: put the '
        "account's token in it"
    )
    assert [p.name for p in view.providers] == ["keyed", "fleet"]
    assert view.providers[0] is roster.providers[0]
    assert view.providers[1].excluded_accounts == ["gone"]
    with pytest.raises(picker.PickerError, match='^route "keyed": auth_env'):
        supervisor._with_credentials(roster, "keyed", {})


def test_a_wall_beside_an_absent_credential_names_both(
    registry: Registry, tmp_path: Path, launches: list[str]
) -> None:
    """The pool a wall and an absent credential emptied says when it serves
    again and why the rest cannot serve."""
    now = int(time.time())
    registry.record_account_wall("main", now + 600, "default_1h", "fixture", now)
    sv = _sv(registry, tmp_path)
    try:
        with pytest.raises(picker.AllProvidersWalled) as walled:
            _start(sv, tmp_path)
    finally:
        sv.shutdown()
    assert walled.value.nearest_reset is not None
    assert f"auth_env {KEY!r} is unset or empty" in walled.value.message
    assert 'account "gone" of route "fleet"' in walled.value.message
    assert launches == []


def _secrets(tmp_path: Path, state: str) -> Path:
    """A secrets directory with nothing a route can read: not there, empty, or
    holding a token file in a directory others can read (0755)."""
    directory = tmp_path / "subs-secrets"
    if state == "missing":
        return directory
    directory.mkdir()
    directory.chmod(0o700 if state == "empty" else 0o755)
    if state == "0755":
        (directory / "one.token").write_text("fixture-token-one\n")
        (directory / "one.token").chmod(0o600)
    return directory


def _subs_roster(roster_home: Path, tmp_path: Path, directory: Path) -> None:
    """The default route `subs` follows the token files of `directory`; the
    route `fleet` has `main`."""
    secrets = tmp_path / "secrets"
    (roster_home / ".nott" / "spawn.toml").write_text(
        f'[defaults]\nplugins = ["{worker_plugin(tmp_path)}"]\n\n'
        '[[provider]]\nname = "subs"\nkind = "claude"\ndefault = true\n'
        f'secrets = "{directory}"\nmodel = "{HAIKU}"\nmodel_fast = "{HAIKU}"\n\n'
        '[[provider]]\nname = "fleet"\nkind = "claude"\n'
        f'secrets = "{secrets}"\nmodel = "{HAIKU}"\nmodel_fast = "{HAIKU}"\n'
        '[provider.accounts.main]\nauth_env = "CLAUDE_CODE_OAUTH_TOKEN"\n',
        encoding="utf-8",
    )


@pytest.mark.parametrize("state", ["missing", "empty", "0755"])
def test_a_secrets_directory_with_no_usable_token_is_left_out(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    launches: list[str],
    state: str,
) -> None:
    """A secrets directory is a declared source: with nothing usable in it the
    route is left out, named with the fix, and a pin on it is refused."""
    directory = _secrets(tmp_path, state)
    _subs_roster(roster_home, tmp_path, directory)
    sv = _sv(registry, tmp_path)
    try:
        row = sv.status(_start(sv, tmp_path), timeout_ms=10_000)
        with pytest.raises(picker.PickerError) as refused:
            _start(sv, tmp_path, provider="subs")
    finally:
        sv.shutdown()
    assert (row.executed_provider, row.executed_account) == ("fleet", "main")
    assert launches == ["fixture-token-main"]
    named = f'route "subs": no usable .token file in secrets directory {directory}'
    fix = "chmod 600 on it and chmod 700 on the directory"
    assert row.warning is not None and named in row.warning and fix in row.warning
    assert named in refused.value.message and fix in refused.value.message
    assert len(registry.list_runs()) == 1


def test_the_note_of_many_absent_credentials_is_bounded(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    launches: list[str],
) -> None:
    """A row holds a warning of 16 KiB at most: 300 routes without their
    variable still let the run land on the one that has a credential."""
    secrets = tmp_path / "secrets"
    keyed = "".join(
        f'[[provider]]\nname = "keyed-{n}"\nkind = "claude"\n'
        f'auth_env = "NSPAWN_FIXTURE_UNSET_{n}"\nmodel = "{HAIKU}"\n\n'
        for n in range(300)
    )
    (roster_home / ".nott" / "spawn.toml").write_text(
        f'[defaults]\nplugins = ["{worker_plugin(tmp_path)}"]\n\n'
        + keyed
        + '[[provider]]\nname = "fleet"\nkind = "claude"\ndefault = true\n'
        f'secrets = "{secrets}"\nmodel = "{HAIKU}"\nmodel_fast = "{HAIKU}"\n'
        '[provider.accounts.main]\nauth_env = "CLAUDE_CODE_OAUTH_TOKEN"\n',
        encoding="utf-8",
    )
    sv = _sv(registry, tmp_path)
    try:
        row = sv.status(_start(sv, tmp_path), timeout_ms=10_000)
    finally:
        sv.shutdown()
    assert (row.executed_provider, row.executed_account, row.phase) == (
        "fleet",
        "main",
        RunPhase.COMPLETED,
    )
    assert row.warning is not None
    # `_listed` keeps whole notes within its limit, then the count of the rest.
    assert len(row.warning.encode()) <= supervisor.NOTE_LIST_MAX_BYTES + len(
        "; 300 more"
    )
    assert 'route "keyed-0": auth_env' in row.warning
    assert row.warning.endswith(" more")


def test_a_pin_names_only_the_route_it_pins(tmp_path: Path) -> None:
    """A pin never takes another route, so no other route's credential is
    named or read: a Codex pin carries no note, a Claude pin only its own."""
    roster = _views(tmp_path)
    roster.providers.append(
        Provider(
            name="cx",
            kind="codex",
            model="gpt",
            accounts={"a": NativeAccount(home=tmp_path)},
        )
    )
    assert supervisor._with_credentials(roster, "cx", {})[1] is None
    with pytest.raises(picker.PickerError) as refused:
        supervisor._with_credentials(roster, "fleet", {})
    assert "keyed" not in refused.value.message


def test_a_pin_on_an_undeclared_name_is_the_pickers_refusal(tmp_path: Path) -> None:
    """A name the roster does not declare is answered with the declared names,
    never with the credentials of routes it does not name."""
    view, note = supervisor._with_credentials(_views(tmp_path), "typo", {})
    assert note is None
    with pytest.raises(picker.PickerError, match="declares \\[keyed, fleet, gone\\]"):
        picker.pick_execution(view, 0, requested_provider="typo")


def test_a_failover_from_an_account_without_a_home_reads_the_declared_roster(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    launches: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An account with no home of its own resolves the journal's Claude home
    from its declared env; the run's walk still keeps the declared roster, so
    a credential its route rejects moves the run to the next route."""
    secrets, spare = tmp_path / "secrets", tmp_path / "spare-secrets"
    spare.mkdir(mode=0o700)
    spare.chmod(0o700)  # whatever the umask
    (spare / "next.token").write_text("fixture-token-next\n", encoding="utf-8")
    (spare / "next.token").chmod(0o600)
    (roster_home / ".nott" / "spawn.toml").write_text(
        f'[defaults]\nplugins = ["{worker_plugin(tmp_path)}"]\n\n'
        '[[provider]]\nname = "fleet"\nkind = "claude"\ndefault = true\n'
        f'secrets = "{secrets}"\nmodel = "{HAIKU}"\nmodel_fast = "{HAIKU}"\n'
        '[provider.accounts.main]\nauth_env = "CLAUDE_CODE_OAUTH_TOKEN"\n\n'
        '[[provider]]\nname = "spare"\nkind = "claude"\n'
        f'secrets = "{spare}"\nmodel = "{HAIKU}"\nmodel_fast = "{HAIKU}"\n'
        '[provider.accounts.next]\nauth_env = "CLAUDE_CODE_OAUTH_TOKEN"\n',
        encoding="utf-8",
    )
    rejected = terminal.TerminalClassification(
        phase=RunPhase.FAILED,
        final_text=None,
        warning="terminal result did not complete (api_error)",
        marker=None,
        provider_wall=None,
        account_wall=None,
        api_error_status=401,
    )
    completed = replace(
        rejected, phase=RunPhase.COMPLETED, final_text="OK", warning=None
    )
    verdicts = [rejected, completed]
    monkeypatch.setattr(
        supervisor.terminal, "classify_terminal_run", lambda *a, **k: verdicts.pop(0)
    )
    sv = _sv(registry, tmp_path)
    try:
        first = _start(sv, tmp_path)
        sv.status(first, timeout_ms=10_000)
        moved = [row.run_id for row in registry.list_runs() if row.run_id != first]
        assert len(moved) == 1, sv.status(first, timeout_ms=0).warning
        row = sv.status(moved[0], timeout_ms=10_000)
    finally:
        sv.shutdown()
    assert (row.phase, row.executed_provider, row.executed_account) == (
        RunPhase.COMPLETED,
        "spare",
        "next",
    )
    assert launches == ["fixture-token-main", "fixture-token-next"]
