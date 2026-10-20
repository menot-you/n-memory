"""Native accounts select declared homes and credential names without reading secrets."""

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from nspawn.owner_roster import Provider, Roster, RosterError

#: One `[bins]` table for the whole file: a provider no longer carries its own
#: `bin`, and two concatenated `_provider()` blocks cannot each redeclare it.
_BINS = '[bins]\nclaude = "/usr/bin/claude"\ncodex = "/usr/bin/codex"\nagy = "/usr/bin/agy"\n\n'


def _read(tmp_path: Path, text: str, bins: str = _BINS) -> Roster:
    path = tmp_path / "spawn.toml"
    path.write_text(bins + text, encoding="utf-8")
    return Roster.read(path)


def _provider(kind: str = "codex", extra: str = "") -> str:
    return f'[[provider]]\nname = "{kind}"\nkind = "{kind}"\nmodel = "native-model"\n{extra}'


def _claude(extra: str) -> str:
    return '[[provider]]\nname = "qwen"\nkind = "claude"\n' + extra


@pytest.mark.parametrize("kind", ["codex", "agy"])
def test_native_account_aliases_select_distinct_homes_and_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    roster = _read(
        tmp_path,
        _provider(kind) + '[provider.accounts.second]\nhome = "~/.native-second"\n'
        'sqlite_home = "~/.native-state"\nauth_env = "NOTT_SECOND_API_KEY"\n'
        'model_provider = "second-endpoint"\n'
        '[provider.accounts.first]\nhome = "/native/first"\n'
        'auth_env = "NOTT_FIRST_API_KEY"\nmodel_provider = "first-endpoint"\n',
    )
    provider = roster.select(kind)
    assert provider is not None
    assert provider.discovered_accounts() == ["first", "second"]
    assert roster.select("first") is provider
    assert roster.select("second") is provider
    assert roster.select("absent") is None
    first = provider.resolve_native_account("first")
    second = provider.resolve_native_account("second")
    assert first.home == Path("/native/first")
    assert first.sqlite_home is None
    assert first.auth_env == "NOTT_FIRST_API_KEY"
    assert first.model_provider == "first-endpoint"
    assert second.home == tmp_path / ".native-second"
    assert second.sqlite_home == tmp_path / ".native-state"
    assert second.auth_env == "NOTT_SECOND_API_KEY"
    assert second.model_provider == "second-endpoint"
    assert provider.accounts["second"].home == Path("~/.native-second")
    # DECLARED, not sorted. This file declares `second` first on
    # purpose, and until 2026-09-20 the assertion here was `first`: alphabetical. The
    # picker breaks ties by declared order (`picker.py:201`), and three places
    # resolving the same choice by two rules are two truths. No live route
    # changes: `codex` declares codex1 before codex2, `agy` has only one,
    # and accounts coming from a secrets folder remain sorted because
    # filenames have no intrinsic order.
    assert provider.resolve_native_account(None) == second
    assert provider.resolve_native_account(kind) == second
    assert "ANTHROPIC_AUTH_TOKEN" not in provider.env
    with pytest.raises(ValueError, match="absent"):
        provider.resolve_native_account("absent")
    for field_name in ("home", "sqlite_home", "auth_env", "model_provider"):
        with pytest.raises(FrozenInstanceError):
            setattr(first, field_name, "/changed")


def test_native_home_alone_is_a_declared_auth_source(tmp_path: Path) -> None:
    roster = _read(
        tmp_path,
        _provider() + '[provider.accounts.oauth]\nhome = "/native/oauth"\n',
    )
    account = roster.providers[0].resolve_native_account(None)
    assert account.home == Path("/native/oauth")
    assert account.auth_env is None
    assert account.model_provider is None
    assert account.sqlite_home is None


@pytest.mark.parametrize(
    "table",
    [
        'accounts = "bad"\n',
        "accounts = {first = 5}\n",
        '[provider.accounts.first]\nauth_env = "NOTT_KEY"\n',
        '[provider.accounts.first]\nhome = ""\n',
        '[provider.accounts.first]\nhome = " "\n',
        '[provider.accounts.first]\nhome = "relative/home"\n',
        '[provider.accounts.first]\nhome = "$HOME/.codex"\n',
        '[provider.accounts.first]\nhome = "/native/${ACCOUNT}"\n',
        '[provider.accounts.first]\nhome = "~other/.codex"\n',
        "[provider.accounts.first]\nhome = 5\n",
        '[provider.accounts.first]\nhome = "/native"\nsqlite_home = "relative"\n',
        '[provider.accounts.first]\nhome = "/native"\nsqlite_home = ""\n',
        '[provider.accounts.first]\nhome = "/native"\nsqlite_home = 5\n',
        '[provider.accounts.first]\nhome = "/native"\nauth_env = ""\n',
        '[provider.accounts.first]\nhome = "/native"\nauth_env = "NOTT-KEY"\n',
        '[provider.accounts.first]\nhome = "/native"\nauth_env = "2KEY"\n',
        '[provider.accounts.first]\nhome = "/native"\nauth_env = "$NOTT_KEY"\n',
        '[provider.accounts.first]\nhome = "/native"\nauth_env = "KEY\\n"\n',
        '[provider.accounts.first]\nhome = "/native"\nauth_env = 5\n',
        '[provider.accounts.first]\nhome = "/native"\nmodel_provider = ""\n',
        '[provider.accounts.first]\nhome = "/native"\nmodel_provider = " "\n',
        '[provider.accounts.first]\nhome = "/native"\nmodel_provider = 5\n',
        '[provider.accounts.""]\nhome = "/native"\n',
    ],
)
def test_malformed_native_accounts_refuse_with_roster_location(
    tmp_path: Path, table: str
) -> None:
    with pytest.raises(RosterError, match="accounts") as refused:
        _read(tmp_path, _provider() + table)
    assert str(tmp_path / "spawn.toml") in str(refused.value)


@pytest.mark.parametrize("field", ["secrets", "oauth_token_file", "auth_env"])
def test_native_accounts_cannot_mix_provider_credential_sources(
    tmp_path: Path, field: str
) -> None:
    with pytest.raises(RosterError, match="accounts"):
        _read(
            tmp_path,
            _provider(extra=f'{field} = "legacy"\n')
            + '[provider.accounts.first]\nhome = "/native/first"\n',
        )


@pytest.mark.parametrize("field", ["oauth_token_file", "auth_env"])
def test_a_claude_kind_provider_still_cannot_mix_a_provider_credential(
    tmp_path: Path, field: str
) -> None:
    """Lifting the DIRECT-only rule did not lift the mixing rule.

    A claude-kind account may now exist, but a provider-level credential
    beside it is still the ambiguity the refusal was written for: nothing
    says which of the two a given account spawns with.
    """
    with pytest.raises(RosterError, match="accounts"):
        _read(
            tmp_path,
            _claude(
                f'{field} = "legacy"\n'
                '[provider.accounts.local]\nbase_url = "http://127.0.0.1:11434"\n'
            ),
        )


def test_native_accounts_do_not_replace_direct_executable_or_model(
    tmp_path: Path,
) -> None:
    text = "\n".join(
        line for line in _provider().splitlines() if not line.startswith("model =")
    )
    with pytest.raises(RosterError, match="no model"):
        _read(tmp_path, text + '\n[provider.accounts.first]\nhome = "/native"\n')
    with pytest.raises(RosterError, match="no bin"):
        _read(
            tmp_path,
            _provider() + '[provider.accounts.first]\nhome = "/native"\n',
            bins="",
        )


def test_native_accounts_never_enable_proxy_routing(tmp_path: Path) -> None:
    with pytest.raises(RosterError, match="proxy"):
        _read(
            tmp_path,
            _provider(extra='proxy = "https://proxy.invalid"\n')
            + '[provider.accounts.first]\nhome = "/native"\n',
        )


@pytest.mark.parametrize("alias", ["codex", "agy"])
def test_explicit_account_alias_cannot_shadow_a_provider(
    tmp_path: Path, alias: str
) -> None:
    with pytest.raises(RosterError, match="alias"):
        _read(
            tmp_path,
            _provider()
            + f'[provider.accounts.{alias}]\nhome = "/native"\n'
            + _provider("agy", 'oauth_token_file = "/legacy/token"\n'),
        )


def test_explicit_alias_cannot_belong_to_two_providers(tmp_path: Path) -> None:
    with pytest.raises(RosterError, match="alias.*shared"):
        _read(
            tmp_path,
            _provider()
            + '[provider.accounts.shared]\nhome = "/native/one"\n'
            + _provider("agy")
            + '[provider.accounts.shared]\nhome = "/native/two"\n',
        )


def test_explicit_alias_cannot_collide_with_a_legacy_account(tmp_path: Path) -> None:
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "shared.token").touch()
    with pytest.raises(RosterError, match="alias.*shared"):
        _read(
            tmp_path,
            _provider()
            + '[provider.accounts.shared]\nhome = "/native"\n'
            + f'[[provider]]\nname = "claude"\nsecrets = "{secrets}"\n',
        )


def test_accounts_as_a_list_of_homes_derives_each_alias_from_the_basename(
    tmp_path: Path,
) -> None:
    roster = _read(
        tmp_path,
        _provider() + 'accounts = ["~/.codex-main", "/srv/codex-two"]\n',
    )
    provider = roster.providers[0]
    assert sorted(provider.accounts) == ["codex-main", "codex-two"]
    assert provider.accounts["codex-main"].home == Path("~/.codex-main")
    assert provider.accounts["codex-two"].home == Path("/srv/codex-two")
    assert provider.accounts["codex-two"].auth_env is None


def test_the_table_form_still_carries_what_a_list_cannot(tmp_path: Path) -> None:
    """The list is a shorthand, never a replacement: sqlite_home, auth_env and
    model_provider have nowhere to live in a bare path."""
    roster = _read(
        tmp_path,
        _provider() + '[provider.accounts.first]\nhome = "/native/first"\n'
        'sqlite_home = "/native/state"\nauth_env = "NOTT_KEY"\n'
        'model_provider = "endpoint"\n',
    )
    account = roster.providers[0].accounts["first"]
    assert account.sqlite_home == Path("/native/state")
    assert account.auth_env == "NOTT_KEY"
    assert account.model_provider == "endpoint"


@pytest.mark.parametrize(
    ("accounts", "alias"),
    [
        ('accounts = ["/a/.codex-one", "/b/codex-one"]\n', "codex-one"),
        ('accounts = ["/a/codex", "/a/.codex"]\n', "codex"),
    ],
)
def test_a_duplicate_derived_alias_is_refused(
    tmp_path: Path, accounts: str, alias: str
) -> None:
    # `tmp_path` carries the test's own name, so a loose regex matches the
    # PATH and passes with the product removed; this one names the alias.
    with pytest.raises(RosterError, match="derives the alias") as refused:
        _read(tmp_path, _provider() + accounts)
    assert f"{alias!r}" in str(refused.value)


@pytest.mark.parametrize(
    "accounts",
    ["accounts = [5]\n", 'accounts = [""]\n', 'accounts = ["/a/..."]\n'],
)
def test_a_malformed_list_entry_is_refused_naming_its_index(
    tmp_path: Path, accounts: str
) -> None:
    with pytest.raises(RosterError, match=r"provider\.accounts\[0\]") as refused:
        _read(tmp_path, _provider() + accounts)
    assert str(tmp_path / "spawn.toml") in str(refused.value)


def test_a_claude_kind_provider_carries_accounts_with_their_own_route(
    tmp_path: Path,
) -> None:
    roster = _read(
        tmp_path,
        _claude(
            'model = "q"\n'
            '[provider.accounts.vllm]\nbase_url = "http://127.0.0.1:8000"\n'
            'model = "qwen3.8-27b"\ncapacity = 4\n'
            'bootstrap = ["systemctl", "--user", "start", "qwen-serving"]\n'
            '[provider.accounts.ollama]\nbase_url = "http://127.0.0.1:11434"\n'
            'model = "qwen3.8:27b"\n'
        ),
    )
    provider = roster.providers[0]
    vllm = provider.accounts["vllm"]
    assert vllm.home is None
    assert vllm.base_url == "http://127.0.0.1:8000"
    assert vllm.model == "qwen3.8-27b"
    assert vllm.capacity == 4
    assert vllm.bootstrap == ("systemctl", "--user", "start", "qwen-serving")
    ollama = provider.accounts["ollama"]
    assert ollama.model == "qwen3.8:27b"
    assert ollama.capacity == 1
    assert ollama.bootstrap == ()
    assert provider.resolve_native_account("vllm") == vllm
    assert roster.select("ollama") is provider


def test_a_claude_account_needs_no_home_and_no_credential_at_all(
    tmp_path: Path,
) -> None:
    """The local ollama route has neither a vendor home nor a key."""
    roster = _read(
        tmp_path,
        _claude('[provider.accounts.local]\nbase_url = "http://127.0.0.1:11434"\n'),
    )
    account = roster.providers[0].resolve_native_account(None)
    assert account.home is None
    assert account.auth_env is None


def test_a_direct_kind_account_still_requires_its_home(tmp_path: Path) -> None:
    with pytest.raises(RosterError, match="home is required"):
        _read(tmp_path, _provider() + '[provider.accounts.first]\nauth_env = "K"\n')


def test_a_direct_kind_account_may_not_declare_a_base_url(tmp_path: Path) -> None:
    with pytest.raises(RosterError, match=r"accounts\.first\.base_url") as refused:
        _read(
            tmp_path,
            _provider() + '[provider.accounts.first]\nhome = "/native"\n'
            'base_url = "http://127.0.0.1:8000"\n',
        )
    assert "codex" in str(refused.value)


def test_bootstrap_is_an_argv_list_never_a_shell_string(tmp_path: Path) -> None:
    with pytest.raises(RosterError, match="bootstrap") as refused:
        _read(
            tmp_path,
            _claude(
                "[provider.accounts.local]\n"
                'bootstrap = "systemctl --user start qwen-serving"\n'
            ),
        )
    assert "shell" in str(refused.value)


@pytest.mark.parametrize(
    "bootstrap",
    [
        "bootstrap = []\n",
        'bootstrap = [""]\n',
        'bootstrap = ["systemctl", 5]\n',
        'bootstrap = ["systemctl\\nrm -rf /"]\n',
        'bootstrap = ["", "systemctl"]\n',
    ],
)
def test_a_malformed_bootstrap_is_refused(tmp_path: Path, bootstrap: str) -> None:
    with pytest.raises(RosterError, match=r"accounts\.local\.bootstrap"):
        _read(tmp_path, _claude("[provider.accounts.local]\n" + bootstrap))


@pytest.mark.parametrize(
    "capacity",
    [
        "capacity = 0\n",
        "capacity = -1\n",
        'capacity = "2"\n',
        "capacity = true\n",
        "capacity = 1.5\n",
    ],
)
def test_capacity_is_a_positive_integer(tmp_path: Path, capacity: str) -> None:
    with pytest.raises(RosterError, match=r"accounts\.local\.capacity"):
        _read(tmp_path, _claude("[provider.accounts.local]\n" + capacity))


@pytest.mark.parametrize(
    "base_url",
    ['base_url = ""\n', "base_url = 5\n", 'base_url = "127.0.0.1:8000"\n'],
)
def test_a_base_url_without_an_http_scheme_is_refused(
    tmp_path: Path, base_url: str
) -> None:
    with pytest.raises(RosterError, match=r"accounts\.local\.base_url"):
        _read(tmp_path, _claude("[provider.accounts.local]\n" + base_url))


def test_empty_native_accounts_do_not_invent_an_account(tmp_path: Path) -> None:
    provider = Provider("codex", kind="codex")
    assert provider.discovered_accounts() == []
    with pytest.raises(ValueError, match="codex"):
        provider.resolve_native_account(None)
    # An explicit empty list reads (Register takes its first account, Codex
    # 58a8a8b2 #6), and still invents none: every run on it is refused.
    empty = _read(tmp_path, _provider() + "accounts = {}\n").select("codex")
    assert empty is not None and empty.ordered_accounts() == []
    with pytest.raises(ValueError, match="codex"):
        empty.resolve_native_account(None)


def test_a_refusal_never_names_a_retired_key(tmp_path: Path) -> None:
    """The vocabulary a refusal hands back has to be writable.

    `token_file` is in `RETIRED`: an owner who follows the message and writes
    it gets «is retired, use secrets» — the refusal sending them at the door
    it refuses. And a claude account CAN name its own `auth_env`, so the
    message also has to say that.
    """
    text = (
        '[[provider]]\nname = "local"\nkind = "claude"\nmodel = "q"\n'
        '[provider.accounts.box]\nbase_url = "http://127.0.0.1:11434"\n'
        'auth_env = "ANTHROPIC_AUTH_TOKEN"\n'
    )
    path = tmp_path / "r.toml"
    path.write_text(text, encoding="utf-8")
    provider = Roster.read(path).select("local")
    assert provider is not None

    with pytest.raises(ValueError) as refused:
        provider.resolve_token_file("box")
    mensagem = str(refused.value)
    for retirada in ("token_file",):
        assert retirada not in mensagem, mensagem
    assert "secrets" in mensagem
    assert "auth_env" in mensagem
