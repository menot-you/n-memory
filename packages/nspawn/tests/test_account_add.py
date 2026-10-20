"""Registering an account the owner logged in himself: the roster is the only write.

The app's quota menu shows the vendor's own login for the owner to run on the host,
on a fresh home NOTT proposes or, for a token provider, a shell that writes the
token file complete. NOTT never runs a login and never writes, moves or deletes a
credential. "Register" then appends the account to the roster, after the default,
once its credential is there.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nspawn.owner_roster import (
    Roster,
    RosterEditRefused,
    make_default_account,
    register_account,
)

ROSTER = """\
[bins]
codex = "/bin/true"
agy = "/bin/true"

[[provider]]
name = "claude"
kind = "claude"
secrets = "{root}/secrets/claude"

[[provider]]
name = "fugu"
kind = "claude"
secrets = "{root}/secrets/fugu"
auth_env = "ANTHROPIC_AUTH_TOKEN"

[[provider]]
name = "codex"
kind = "codex"
model = "gpt"

# the owner's own words stay
[provider.accounts.main]
home = "{root}/codex-main"

[[provider]]
name = "agy"
kind = "agy"
model = "gemini"

[provider.accounts.agy1]
home = "{root}"

[[provider]]
name = "qwen"
kind = "claude"
secrets = "{root}/secrets/qwen"

[provider.accounts.server]
base_url = "http://127.0.0.1:1"
auth_env = "ANTHROPIC_AUTH_TOKEN"
"""


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A host whose HOME, roster and secrets are all under tmp."""
    for name in ("claude", "fugu", "qwen"):
        (tmp_path / "secrets" / name).mkdir(parents=True)
    (tmp_path / "secrets" / "claude" / "first.token").write_text("x")
    (tmp_path / "secrets" / "fugu" / "f1.token").write_text("x")
    roster = tmp_path / "spawn.toml"
    roster.write_text(ROSTER.format(root=tmp_path))
    roster.chmod(0o600)
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _accounts(host: Path, provider: str) -> list[str]:
    found = Roster.read(host / "spawn.toml").select(provider)
    assert found is not None
    return found.ordered_accounts()


def _logged_in(host: Path, relative: str) -> None:
    """What the vendor's own login leaves, as the owner ran it."""
    credential = host / relative
    credential.parent.mkdir(parents=True, exist_ok=True)
    credential.write_text("{}")


def test_codex_registers_its_logged_in_home_last(host: Path) -> None:
    path = host / "spawn.toml"
    before = path.read_text()
    _logged_in(host, ".codex-work/auth.json")
    backup = register_account(path, "codex", "work")
    assert backup is not None and backup.read_text() == before
    assert _accounts(host, "codex") == ["main", "work"]
    # Only the new table entered, after codex's last line; every other byte stays.
    added = f'\n[provider.accounts.work]\nhome = "{host}/.codex-work"\n'
    after = path.read_text()
    assert after.replace(added, "", 1) == before
    assert after.index(added) < after.index('name = "agy"')
    # The credential is the owner's: read, never written or moved.
    assert (host / ".codex-work/auth.json").read_text() == "{}"


def test_agy_registers_its_logged_in_home(host: Path) -> None:
    _logged_in(host, ".agy-second/.gemini/antigravity-cli/antigravity-oauth-token")
    register_account(host / "spawn.toml", "agy", "second")
    assert _accounts(host, "agy") == ["agy1", "second"]
    # Codex e8af3a3a #2: a node refuses an agy home that is not absolute.
    agy = Roster.read(host / "spawn.toml").select("agy")
    assert agy is not None and agy.accounts["second"].home == host / ".agy-second"


def test_a_home_without_its_login_is_refused(host: Path) -> None:
    path = host / "spawn.toml"
    before = path.read_bytes()
    (host / ".codex-work").mkdir()
    with pytest.raises(RosterEditRefused, match="holds no codex login"):
        register_account(path, "codex", "work")
    assert path.read_bytes() == before


def test_a_home_another_account_uses_is_refused(host: Path) -> None:
    """The proposed home of a new alias may already be an account's own."""
    path = host / "spawn.toml"
    text = path.read_text().replace(
        f'home = "{host}/codex-main"', 'home = "~/.codex-work"'
    )
    path.write_text(text)
    _logged_in(host, ".codex-work/auth.json")
    with pytest.raises(RosterEditRefused, match="already uses"):
        register_account(path, "codex", "work")
    assert path.read_text() == text


def test_a_name_that_is_a_provider_is_refused(host: Path) -> None:
    """`Roster.select("claude")` takes the provider, never a fugu account."""
    (host / "secrets" / "fugu" / "claude.token").write_text("key")
    path = host / "spawn.toml"
    before = path.read_bytes()
    with pytest.raises(RosterEditRefused, match="names a provider"):
        register_account(path, "fugu", "claude")
    assert path.read_bytes() == before


def test_a_token_named_as_its_provider_is_refused_by_name(host: Path) -> None:
    """Codex e8af3a3a #1 and d64e7a34: `claude.token` under claude cannot be
    declared, since an alias that is its provider's name means the default to
    `child._selected_account` and `prepare_native`. Opening Add says so by name and
    writes nothing; the token stays the owner's to rename, and runs still find it."""
    path = host / "spawn.toml"
    (host / "secrets" / "claude" / "claude.token").write_text("sk")
    before = path.read_bytes()
    with pytest.raises(RosterEditRefused, match="rename claude.token"):
        _open_add(host, "claude")
    assert path.read_bytes() == before
    assert _accounts(host, "claude") == ["claude", "first"]
    (host / "secrets" / "fugu" / "codex.token").write_text("sk")
    with pytest.raises(RosterEditRefused, match="nothing was written") as refused:
        make_default_account(path, "fugu", "f1")
    assert "collides with a provider name" in str(refused.value.__cause__)


@pytest.mark.parametrize(
    ("provider", "alias", "says"),
    [
        ("codex", "main", "codex already has"),
        ("fugu", "first", "claude already has"),
        ("qwen", "work", "no login"),
        ("nobody", "work", "no provider"),
        ("codex", "../escape", "letters, digits"),
        ("codex", "ghost", "holds no codex login"),
        ("claude", "work", "not declared yet"),
    ],
)
def test_what_cannot_be_registered_is_refused_untouched(
    host: Path, provider: str, alias: str, says: str
) -> None:
    (host / "secrets" / "fugu" / "first.token").write_text("key")
    path = host / "spawn.toml"
    before = path.read_bytes()
    with pytest.raises(RosterEditRefused, match=says):
        register_account(path, provider, alias)
    assert path.read_bytes() == before


def _open_add(host: Path, provider: str) -> None:
    """What opening Add does: the default made the default, which declares a secrets
    directory's discovered accounts as they stand."""
    make_default_account(host / "spawn.toml", provider, _accounts(host, provider)[0])


def test_a_new_token_that_sorts_first_never_becomes_the_default(host: Path) -> None:
    """Codex 58a8a8b2 #3: `aaa.token` beside `first.token` sorted first."""
    _open_add(host, "claude")
    (host / "secrets" / "claude" / "aaa.token").write_text("sk")
    assert _accounts(host, "claude") == ["first"]
    register_account(host / "spawn.toml", "claude", "aaa")
    assert _accounts(host, "claude") == ["first", "aaa"]
    found = Roster.read(host / "spawn.toml").select("claude")
    assert found is not None
    assert found.accounts["aaa"].auth_env == "CLAUDE_CODE_OAUTH_TOKEN"


def test_an_existing_discovered_account_is_never_registered_again(host: Path) -> None:
    """Codex 58a8a8b2 #2: `alpha` beside `alpha, beta` moved the default to beta."""
    path = host / "spawn.toml"
    (host / "secrets" / "claude" / "beta.token").write_text("sk")
    before = path.read_bytes()
    with pytest.raises(RosterEditRefused, match="not declared yet"):
        register_account(path, "claude", "first")
    assert path.read_bytes() == before
    _open_add(host, "claude")
    declared = path.read_bytes()
    with pytest.raises(RosterEditRefused, match="claude already has"):
        register_account(path, "claude", "first")
    assert path.read_bytes() == declared
    assert _accounts(host, "claude") == ["beta", "first"]


def test_a_registered_token_account_is_always_declared(host: Path) -> None:
    """Codex 58a8a8b2 #4: `work` after `first` left the roster as it was, since the
    sorted discovery already read that way, so no table said the account was there."""
    path = host / "spawn.toml"
    _open_add(host, "claude")
    (host / "secrets" / "claude" / "work.token").write_text("sk")
    register_account(path, "claude", "work")
    assert "[provider.accounts.work]" in path.read_text()
    assert _accounts(host, "claude") == ["first", "work"]


def test_a_token_provider_with_no_account_takes_its_first(host: Path) -> None:
    path = host / "spawn.toml"
    (host / "secrets" / "claude" / "first.token").unlink()
    (host / "secrets" / "claude" / "solo.token").write_text("sk")
    register_account(path, "claude", "solo")
    assert "[provider.accounts.solo]" in path.read_text()
    assert _accounts(host, "claude") == ["solo"]


def test_an_empty_native_provider_takes_its_first_account(host: Path) -> None:
    """Codex 58a8a8b2 #6: a codex provider with no account could not be read, so
    nothing could register its first. An explicit empty list reads, and a run on it
    is refused until an account is there."""
    path = host / "spawn.toml"
    text = path.read_text().replace(
        f'# the owner\'s own words stay\n[provider.accounts.main]\nhome = "{host}/codex-main"\n',
        "[provider.accounts]\n",
    )
    path.write_text(text)
    empty = Roster.read(path).select("codex")
    assert empty is not None and empty.ordered_accounts() == []
    with pytest.raises(ValueError, match="unavailable"):
        empty.resolve_native_account(None)
    _logged_in(host, ".codex-work/auth.json")
    register_account(path, "codex", "work")
    assert _accounts(host, "codex") == ["work"]


def test_a_token_that_is_missing_is_refused(host: Path) -> None:
    _open_add(host, "claude")
    path = host / "spawn.toml"
    before = path.read_bytes()
    with pytest.raises(RosterEditRefused, match="no token for work"):
        register_account(path, "claude", "work")
    assert path.read_bytes() == before


def test_a_declared_token_provider_gets_the_others_variable(host: Path) -> None:
    (host / "secrets" / "fugu" / "zero.token").write_text("x")
    make_default_account(host / "spawn.toml", "fugu", "zero")
    (host / "secrets" / "fugu" / "work.token").write_text("key")
    register_account(host / "spawn.toml", "fugu", "work")
    assert _accounts(host, "fugu") == ["zero", "f1", "work"]
    found = Roster.read(host / "spawn.toml").select("fugu")
    assert found is not None
    assert found.accounts["work"].auth_env == "ANTHROPIC_AUTH_TOKEN"


def test_an_append_that_would_reorder_the_accounts_is_refused(host: Path) -> None:
    """Valid TOML may put another table between a provider and its accounts; the
    new table then lands first. The documents compare equal, the ORDER does not."""
    path = host / "spawn.toml"
    text = path.read_text().replace(
        "# the owner's own words stay\n", '[defaults]\nmodel = "x"\n\n', 1
    )
    path.write_text(text)
    _logged_in(host, ".codex-work/auth.json")
    with pytest.raises(RosterEditRefused, match="nothing was written"):
        register_account(path, "codex", "work")
    assert path.read_text() == text


def test_accounts_that_are_a_list_of_homes_cannot_be_joined(host: Path) -> None:
    path = host / "spawn.toml"
    text = path.read_text().replace(
        "\n# the owner's own words stay\n[provider.accounts.main]\n"
        f'home = "{host}/codex-main"\n',
        f'accounts = ["{host}/codex-main"]\n',
    )
    path.write_text(text)
    _logged_in(host, ".codex-work/auth.json")
    with pytest.raises(RosterEditRefused, match="their own"):
        register_account(path, "codex", "work")
    assert path.read_text() == text
