"""Driven reads, selections and refusals for the owner roster.

Mirrors the oracle's own test set: absent file, shared door tables, the three
selection paths, both validation refusals, the effort ladder decision table,
dev-app slug grammar, the owner-file fence, and the expansion cases.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import sample_roster

from nspawn import classify, system1
from nspawn.owner_roster import (
    UNCLASSIFIED,
    ClassPolicy,
    Roster,
    RosterError,
    expand_value_with,
)
from nspawn.picker import PickerError, pick_execution


def _write(tmp_path: Path, text: str, mode: int = 0o600) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    os.chmod(path, mode)
    return path


def test_sample_native_accounts_pass_the_runtime_parser(tmp_path: Path) -> None:
    roster = Roster.read(sample_roster.write(tmp_path))
    for name, kind, count in (
        ("codex", "codex", 1),
        ("agy", "agy", 1),
    ):
        provider = roster.select(name)
        assert provider is not None
        assert provider.kind == kind
        assert len(provider.accounts) == count
        assert provider.model in provider.models
        assert provider.model_fast in provider.models
        for alias in provider.accounts:
            assert roster.select(alias) is provider


def test_the_sample_roster_declares_its_gateway_routes(tmp_path: Path) -> None:
    """Every gateway route says which wire it speaks, in one of its two shapes.

    `fugu` and `zai` are one endpoint for the whole provider, so they say it in
    `[provider.env]`. `qwen` is one endpoint PER MACHINE, so each account says
    its own — and the provider must NOT also say one, because `[provider.env]`
    wins over the account (`child.py:294`) and would pin every machine to the
    same address.

    What must hold either way: a claude-kind route that is not the Anthropic
    one never reaches an endpoint nobody declared, because that silently talks
    to Anthropic with somebody else's key.
    """
    roster = Roster.read(sample_roster.write(tmp_path))
    for name in ("fugu", "zai", "qwen"):
        provider = roster.select(name)
        assert provider is not None, name
        assert provider.kind == "claude", name
        assert provider.model in provider.models or not provider.models, name
        do_provider = provider.env.get("ANTHROPIC_BASE_URL")
        if provider.accounts:
            # Per-account endpoints: each one declares its own, and the
            # provider stays quiet so it cannot overwrite them.
            assert not do_provider, name
            for alias, account in provider.accounts.items():
                assert account.base_url, f"{name}/{alias}"
        else:
            assert do_provider, name


def test_an_endpoint_above_a_per_account_one_is_refused(tmp_path: Path) -> None:
    """The env that wins over the account is refused where it is written.

    `build_child_env` applies the account and THEN the env blocks, so an
    `ANTHROPIC_BASE_URL` above per-account endpoints pins every account to one
    address and the spread has nowhere to go. It has to be refused in the
    PARSER: a test over the sample file protects that file only, and the
    endpoint the operator's own roster sets is the one that would bite.

    `[defaults.env]` counts, because `Roster.resolve` merges it under
    `[provider.env]` and the child reads the merge.
    """
    provider = (
        '[[provider]]\nname = "qwen"\nkind = "claude"\nmodel = "q"\n'
        '[provider.accounts.server]\nbase_url = "http://127.0.0.1:1"\n'
        'auth_env = "ANTHROPIC_AUTH_TOKEN"\n'
        '[provider.accounts.m5]\nbase_url = "http://127.0.0.1:2"\n'
        'auth_env = "ANTHROPIC_AUTH_TOKEN"\n'
    )
    for text in (
        provider + '[provider.env]\nANTHROPIC_BASE_URL = "http://pin.invalid"\n',
        '[defaults.env]\nANTHROPIC_BASE_URL = "http://pin.invalid"\n' + provider,
    ):
        with pytest.raises(RosterError, match="ANTHROPIC_BASE_URL"):
            Roster.read(_write(tmp_path, text))


def test_an_endpoint_above_accounts_that_declare_none_is_fine(
    tmp_path: Path,
) -> None:
    """Valid progress: only an account that HAS its own endpoint is overridden.

    A native provider declares accounts and no `base_url` anywhere; refusing
    every provider that has both accounts and an endpoint would break it.
    """
    text = (
        '[bins]\ncodex = "/x/codex"\n\n'
        '[defaults.env]\nANTHROPIC_BASE_URL = "http://gateway.invalid"\n'
        '[[provider]]\nname = "codex"\nkind = "codex"\nmodel = "m"\n'
        '[provider.accounts.one]\nhome = "~/.codex"\n'
    )
    roster = Roster.read(_write(tmp_path, text))
    assert roster.select("codex") is not None


def test_the_sample_roster_declares_one_binary_per_kind(tmp_path: Path) -> None:
    """`[bins]` is the only spelling; `claude_bin` and per-provider `bin` are gone."""
    roster = Roster.read(sample_roster.write(tmp_path))
    assert set(roster.bins) == {"claude", "codex", "agy"}
    for kind, binary in roster.bins.items():
        assert binary.is_absolute(), kind
    for name in ("codex", "agy"):
        provider = roster.select(name)
        assert provider is not None
        # Inherited at parse time, so every consumer downstream sees a path.
        assert provider.bin == roster.bins[provider.kind]


def test_an_absent_roster_reads_as_empty(tmp_path: Path) -> None:
    roster = Roster.read(tmp_path / "nonexistent.toml")
    assert roster.providers == []
    assert roster.select(None) is None


def test_the_doors_tables_are_ignored_rather_than_refused(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        '[llm]\nmodel = "claude-haiku-4-5"\neffort = "max"\n\n'
        '[[account]]\nname = "claude4"\ntoken_file = "~/.nott/secrets/claude4.token"\n'
        "default = true\n\n"
        '[[provider]]\nname = "claude1"\nkind = "claude"\n'
        'secrets = "~/.config/claude-tokens"\n',
    )
    roster = Roster.read(path)
    assert len(roster.providers) == 1
    assert roster.providers[0].name == "claude1"


def test_selection_resolves_by_name_by_default_and_by_the_only_one(
    tmp_path: Path,
) -> None:
    path = _write(
        tmp_path,
        '[[provider]]\nname = "claude1"\nkind = "claude"\n\n'
        '[[provider]]\nname = "zai"\nkind = "claude"\ndefault = true\n',
    )
    roster = Roster.read(path)
    assert roster.select("claude1") is not None
    assert roster.select("claude1").name == "claude1"  # type: ignore[union-attr]
    assert roster.select(None).name == "zai"  # type: ignore[union-attr]

    solo = _write(tmp_path, '[[provider]]\nname = "solo"\nkind = "claude"\n')
    assert Roster.read(solo).select(None).name == "solo"  # type: ignore[union-attr]


def test_a_model_off_the_declared_list_is_refused_naming_the_served_set(
    tmp_path: Path,
) -> None:
    path = _write(
        tmp_path,
        '[[provider]]\nname = "ollama"\nkind = "claude"\n'
        'models = ["m5/qwen3.8:27b", "win/qwen2.5:7b"]\n',
    )
    roster = Roster.read(path)
    from nspawn.errors import NERR_1105, NSpawnError

    with pytest.raises(NSpawnError) as refused:
        roster.refuse_unservable_spawn("ollama", "claude/m5/qwen3.8:27b")
    assert refused.value.entry is NERR_1105
    assert "claude/m5/qwen3.8:27b" in str(refused.value)
    assert "m5/qwen3.8:27b" in str(refused.value)
    assert "[[provider]] ollama" in str(refused.value)
    roster.refuse_unservable_spawn("ollama", "win/qwen2.5:7b")


def test_undeclared_lists_and_an_absent_provider_validate_nothing(
    tmp_path: Path,
) -> None:
    path = _write(tmp_path, '[[provider]]\nname = "zai"\nkind = "claude"\n')
    roster = Roster.read(path)
    roster.refuse_unservable_spawn("zai", "glm-5.2")
    roster.refuse_unservable_spawn("missing", "glm-5.2")
    roster.refuse_unservable_spawn(None, "glm-5.2")


def test_a_provider_effort_off_its_own_ladder_is_refused(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        '[[provider]]\nname = "solo"\nkind = "claude"\neffort = "max"\n'
        'efforts = ["low", "medium", "high", "xhigh"]\nmodels = ["m"]\n',
    )
    roster = Roster.read(path)
    from nspawn.errors import NERR_1124, NSpawnError

    with pytest.raises(NSpawnError) as refused:
        roster.refuse_unservable_spawn("solo", "m")
    assert refused.value.entry is NERR_1124
    assert 'effort = "max"' in str(refused.value)
    assert "xhigh" in str(refused.value)


def test_a_repeated_name_and_a_second_default_are_both_refused(tmp_path: Path) -> None:
    twice = _write(
        tmp_path,
        '[[provider]]\nname = "a"\nkind = "claude"\n\n'
        '[[provider]]\nname = "a"\nkind = "claude"\n',
    )
    with pytest.raises(RosterError, match="declares a twice"):
        Roster.read(twice)

    two_defaults = _write(
        tmp_path,
        '[[provider]]\nname = "a"\nkind = "claude"\ndefault = true\n\n'
        '[[provider]]\nname = "b"\nkind = "claude"\ndefault = true\n',
    )
    with pytest.raises(RosterError, match="marks 2 claude providers"):
        Roster.read(two_defaults)


def test_a_malformed_file_is_carried_as_a_value(tmp_path: Path) -> None:
    path = _write(tmp_path, "[[provider]]\nname = \n")
    with pytest.raises(RosterError, match="is malformed"):
        Roster.read(path)


def test_a_non_string_kind_is_refused_as_a_malformed_roster(tmp_path: Path) -> None:
    path = _write(tmp_path, '[[provider]]\nname = "a"\nkind = 42\n')
    with pytest.raises(RosterError) as refused:
        Roster.read(path)
    assert str(refused.value) == (
        f"the provider roster at {path} is malformed: provider.kind is not a string"
    )


def test_exhausted_codes_are_read_as_the_owner_declared_them(tmp_path: Path) -> None:
    """The vendor's own exhaustion codes, declared per provider; absent is none."""
    path = _write(
        tmp_path,
        '[[provider]]\nname = "zai"\nexhausted_codes = ["1113", "1308"]\n'
        '[[provider]]\nname = "plain"\n',
    )
    roster = Roster.read(path)
    zai, plain = roster.select("zai"), roster.select("plain")
    assert zai is not None and plain is not None
    assert zai.exhausted_codes == frozenset({"1113", "1308"})
    assert plain.exhausted_codes == frozenset()


@pytest.mark.parametrize(
    "value",
    ['"1113"', "[1113]", '["11a3"]', '[""]', '["-1113"]', '[" 1113"]', '["١١١٣"]'],
)
def test_exhausted_codes_that_are_not_digit_strings_refuse_the_roster(
    tmp_path: Path, value: str
) -> None:
    """A code the parser could never match would declare a wall that never comes."""
    path = _write(tmp_path, f'[[provider]]\nname = "zai"\nexhausted_codes = {value}\n')
    with pytest.raises(RosterError) as refused:
        Roster.read(path)
    assert str(refused.value) == (
        f"the provider roster at {path} is malformed: "
        "provider.exhausted_codes is not a list of digit strings"
    )


def test_the_sample_roster_declares_zai_balance_exhaustion(tmp_path: Path) -> None:
    """Z.AI's `1113` is "Insufficient balance or no resource package" (a 429)."""
    roster = Roster.read(sample_roster.write(tmp_path))
    zai = roster.select("zai")
    assert zai is not None
    assert zai.exhausted_codes == frozenset({"1113"})


def test_a_non_string_bin_in_the_bins_table_is_refused_as_a_malformed_roster(
    tmp_path: Path,
) -> None:
    path = _write(tmp_path, "[bins]\ncodex = 42\n")
    with pytest.raises(RosterError) as refused:
        Roster.read(path)
    assert str(refused.value) == (
        f"the provider roster at {path} is malformed: bins.codex is not a path string"
    )


def test_bins_fills_a_provider_that_declares_no_binary_of_its_own(
    tmp_path: Path,
) -> None:
    path = _write(
        tmp_path,
        '[bins]\ncodex = "/x/codex"\nagy = "/x/agy"\n\n'
        '[[provider]]\nname = "codex"\nkind = "codex"\n'
        'secrets = "/x/s"\nmodel = "m"\n',
    )
    roster = Roster.read(path)
    assert roster.bins == {"codex": Path("/x/codex"), "agy": Path("/x/agy")}
    assert roster.providers[0].bin == Path("/x/codex")


def test_a_bins_key_outside_the_kinds_is_refused_naming_the_kinds(
    tmp_path: Path,
) -> None:
    with pytest.raises(RosterError, match="bins.gemini") as refused:
        Roster.read(_write(tmp_path, '[bins]\ngemini = "/x/gemini"\n'))
    assert "claude, codex, agy" in str(refused.value)


def test_a_direct_kind_with_no_binary_anywhere_names_the_bins_table(
    tmp_path: Path,
) -> None:
    path = _write(
        tmp_path,
        '[[provider]]\nname = "codex"\nkind = "codex"\nsecrets = "/x/s"\nmodel = "m"\n',
    )
    with pytest.raises(RosterError, match="no bin") as refused:
        Roster.read(path)
    assert "[bins].codex" in str(refused.value)


@pytest.mark.parametrize(
    ("text", "retired", "replacement"),
    [
        (
            '[defaults]\nclaude_bin = "/x/claude"\n',
            "defaults.claude_bin",
            "[bins].claude",
        ),
        ('[[provider]]\nname = "a"\nbin = "/x/a"\n', "bin", "[bins].<kind>"),
        ('[[provider]]\nname = "a"\ntoken_file = "/x/t"\n', "token_file", "secrets"),
        (
            '[[provider]]\nname = "a"\nspeed_tiers = ["fast"]\n',
            "speed_tiers",
            "efforts",
        ),
        (
            '[defaults]\nplugin_root = "/x/plugin"\n',
            "defaults.plugin_root",
            "defaults.plugins",
        ),
    ],
)
def test_a_retired_key_is_refused_naming_the_spelling_that_replaces_it(
    tmp_path: Path, text: str, retired: str, replacement: str
) -> None:
    with pytest.raises(RosterError) as refused:
        Roster.read(_write(tmp_path, text))
    assert retired in str(refused.value)
    assert replacement in str(refused.value)


def test_the_plugin_root_is_the_first_declared_plugin_directory(
    tmp_path: Path,
) -> None:
    """One key, not two: `plugins` carries the root the child expands `$PLUGIN` to.

    The owner used to write `plugin_root` beside `plugins` and the two could
    disagree — and did: the live roster pointed the root at a client
    subdirectory while `plugins` named the bundle. A root that is DERIVED
    cannot drift from the list it is derived from.
    """
    path = _write(
        tmp_path,
        '[defaults]\nplugins = ["/x/plugin", "/x/other"]\n',
    )
    assert Roster.read(path).defaults.plugin_root == Path("/x/plugin")


def test_a_roster_naming_no_plugins_has_no_plugin_root(tmp_path: Path) -> None:
    """Valid progress: deriving the root invents nothing when the list is empty."""
    assert (
        Roster.read(_write(tmp_path, '[defaults]\nmodel = "m"\n')).defaults.plugin_root
        is None
    )


def test_only_the_named_keys_are_retired_and_every_other_stays_ignored(
    tmp_path: Path,
) -> None:
    """The docstring's promise survives the refusals above.

    Retiring four NAMED keys is not deny-unknown-fields: the roster still
    shares a file with the door's own tables, and a key this module never
    heard of is still ignored rather than refused.
    """
    path = _write(
        tmp_path,
        "[nbrowser]\nheadless = true\n\n"
        '[defaults]\nmodel = "m"\nfuture_key = "whatever"\n\n'
        '[[provider]]\nname = "a"\nkind = "claude"\nunknown_key = 7\n',
    )
    roster = Roster.read(path)
    assert roster.defaults.model == "m"
    assert roster.providers[0].name == "a"


def test_resolution_falls_through_provider_then_defaults(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        '[defaults]\neffort = "max"\nplugins = ["/p"]\n'
        'args = ["--plugin-dir", "$PLUGIN/"]\n\n'
        '[[provider]]\nname = "plain"\nkind = "claude"\ndefault = true\n'
        'secrets = "~/.nott/secrets/plain"\n\n'
        '[[provider]]\nname = "loud"\nkind = "claude"\neffort = "low"\n'
        'args = ["--own"]\n\n[provider.env]\nANTHROPIC_BASE_URL = "https://x"\n',
    )
    roster = Roster.read(path)

    plain = roster.resolve("plain")
    # `plugins` generates its own pair; the owner's hand-written args follow it.
    assert plain.args == ["--plugin-dir", "/p", "--plugin-dir", "$PLUGIN/"]
    assert plain.effort == "max"
    assert plain.plugin_root == Path("/p")
    assert plain.env is None, "an empty env must not reach the seam"

    loud = roster.resolve("loud")
    assert loud.args == ["--plugin-dir", "/p", "--own"]
    assert loud.effort == "low"
    assert loud.plugin_root == Path("/p")
    assert loud.env is not None

    empty = Roster().resolve(None)
    assert empty.args == [] and empty.effort is None and empty.env is None


def test_a_default_effort_outside_a_providers_ladder_is_dropped(tmp_path: Path) -> None:
    """Live-observed in the oracle: a default the ladder does not carry must
    not reach the vendor — the vendor applies its own default, which runs."""
    path = _write(
        tmp_path,
        '[defaults]\neffort = "max"\n\n'
        '[[provider]]\nname = "narrow"\nkind = "claude"\n'
        'efforts = ["low", "medium", "high", "xhigh"]\n\n'
        '[[provider]]\nname = "wide"\nkind = "claude"\n'
        'efforts = ["low", "medium", "high", "xhigh", "max"]\n\n'
        '[[provider]]\nname = "bare"\nkind = "claude"\n',
    )
    roster = Roster.read(path)
    assert roster.resolve("narrow").effort is None
    assert roster.resolve("wide").effort == "max"
    assert roster.resolve("bare").effort == "max"

    defaults_only = _write(tmp_path, '[defaults]\neffort = "max"\n')
    assert Roster.read(defaults_only).resolve(None).effort is None


def test_a_provider_that_names_its_own_effort_keeps_it(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        '[defaults]\neffort = "max"\n\n'
        '[[provider]]\nname = "solo"\nkind = "claude"\neffort = "xhigh"\n'
        'efforts = ["low", "medium", "high", "xhigh"]\n',
    )
    roster = Roster.read(path)
    assert roster.resolve("solo").effort == "xhigh"


def test_expansion_covers_plugin_env_and_the_unresolved_case() -> None:
    home = os.environ["HOME"]
    assert expand_value_with(f"a-{home[:0]}$HOME-b", None) == f"a-{home}-b"
    assert expand_value_with("$PLUGIN/NOTT.md", Path("/p")) == "/p/NOTT.md"
    assert expand_value_with("$NOTT_NSPAWN_ROSTER_ABSENT_VALUE", None) == ""
    assert expand_value_with("no references", None) == "no references"
    assert expand_value_with("100$", None) == "100$"
    assert expand_value_with("$PLUGIN/x", None) == "/x", (
        "an unresolved $PLUGIN drops, matching every other unresolved name"
    )


def test_a_provider_with_secrets_directory_discovers_accounts_and_resolves_tokens(
    tmp_path: Path,
) -> None:
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    (secrets_dir / "strife.token").write_text("sk-ant-" + "oat01-token-a")
    (secrets_dir / "claude-court.token").write_text("sk-ant-" + "oat01-token-b")

    path = _write(
        tmp_path,
        f'[[provider]]\nname = "claude"\nkind = "claude"\n'
        f'secrets = "{secrets_dir}"\ndefault = true\n',
    )
    roster = Roster.read(path)
    provider = roster.select(None)
    assert provider is not None
    assert provider.discovered_accounts() == [
        "claude-court",
        "strife",
    ]
    by_account = roster.select("strife")
    assert by_account is not None and by_account.name == "claude"
    assert provider.resolve_token_file("strife") == (secrets_dir / "strife.token")
    # None resolves to the FIRST discovered account, so this follows the sort
    # order above rather than restating a name.
    assert provider.resolve_token_file(None) == (secrets_dir / "claude-court.token")


def test_a_provider_without_secrets_is_proxied_by_default(tmp_path: Path) -> None:
    with pytest.raises(RosterError, match="proxy routes are retired"):
        Roster.read(
            _write(tmp_path, '[[provider]]\nname = "p"\nproxy = "http://127.0.0.1:1"\n')
        )
    path = _write(
        tmp_path,
        '[[provider]]\nname = "bare"\n\n'
        '[[provider]]\nname = "own"\nsecrets = "~/.nott/secrets/own"\n\n'
        '[[provider]]\nname = "byfile"\nsecrets = "/x/token"\n\n'
        '[[provider]]\nname = "byenv"\nauth_env = "X_KEY"\n\n'
        '[[provider]]\nname = "byoauth"\noauth_token_file = "/x/oauth"\n\n'
        '[[provider]]\nname = "envroute"\n'
        '[provider.env]\nANTHROPIC_BASE_URL = "https://vendor.invalid"\n',
    )
    roster = Roster.read(path)
    by = {p.name: p for p in roster.providers}
    for name in ("bare", "own", "byfile", "byenv", "byoauth", "envroute"):
        assert "ANTHROPIC_AUTH_TOKEN" not in by[name].env, name


def test_kind_defaults_to_claude_and_an_unknown_kind_is_refused(tmp_path: Path) -> None:
    path = _write(tmp_path, '[[provider]]\nname = "a"\nsecrets = "/x/a"\n')
    assert Roster.read(path).providers[0].kind == "claude"
    bad = _write(tmp_path, '[[provider]]\nname = "a"\nkind = "gemini"\n')
    with pytest.raises(RosterError, match="provider.kind"):
        Roster.read(bad)


def test_a_direct_kind_is_refused_before_the_proxy_default_unless_complete(
    tmp_path: Path,
) -> None:
    def block(extra: str, bins: str = '[bins]\ncodex = "/x/codex"\n\n') -> str:
        return bins + '[[provider]]\nname = "codex"\nkind = "codex"\n' + extra

    complete = block('secrets = "/x/secrets"\nmodel = "m"\n')
    provider = Roster.read(_write(tmp_path, complete)).providers[0]
    assert provider.kind == "codex"
    assert provider.bin == Path("/x/codex")
    assert "ANTHROPIC_AUTH_TOKEN" not in provider.env
    for missing, text in (
        ("no bin", block('secrets = "/x/secrets"\nmodel = "m"\n', bins="")),
        ("no secrets", block('model = "m"\n')),
        ("no model", block('secrets = "/x/secrets"\n')),
        (
            "proxy routes are retired",
            block(
                'secrets = "/x/secrets"\nmodel = "m"\nproxy = "http://127.0.0.1:1"\n'
            ),
        ),
    ):
        with pytest.raises(RosterError, match=missing):
            Roster.read(_write(tmp_path, text))


def test_a_bare_alias_discovered_under_two_providers_is_refused_at_resolution(
    tmp_path: Path,
) -> None:
    for name in ("a", "b"):
        directory = tmp_path / name
        directory.mkdir()
        (directory / "shared.token").write_text("x")
    path = _write(
        tmp_path,
        f'[[provider]]\nname = "a"\nsecrets = "{tmp_path / "a"}"\n\n'
        f'[[provider]]\nname = "b"\nsecrets = "{tmp_path / "b"}"\n',
    )
    roster = Roster.read(path)
    selected = roster.select("a")
    assert selected is not None and selected.name == "a"
    with pytest.raises(RosterError, match="under both a and b"):
        roster.select("shared")


def test_the_home_key_is_ignored_and_provider_carries_no_home(tmp_path: Path) -> None:
    path = _write(tmp_path, '[[provider]]\nname = "a"\nhome = "~/.codex2"\n')
    provider = Roster.read(path).providers[0]
    assert not hasattr(provider, "home")


def test_an_empty_string_is_refused_never_read_as_the_current_directory(
    tmp_path: Path,
) -> None:
    for text in (
        '[bins]\ncodex = ""\n',
        (
            '[bins]\ncodex = "/x/codex"\n\n'
            '[[provider]]\nname = "c"\nkind = "codex"\nsecrets = ""\nmodel = "m"\n'
        ),
        '[[provider]]\nname = "a"\nkind = ""\n',
    ):
        with pytest.raises(RosterError, match="is empty"):
            Roster.read(_write(tmp_path, text))


_CLASS_ROSTER = """
[classes]
unclassified = { effort = "low", ceiling = "low" }
"""


def _classes(tmp_path: Path, body: str) -> dict[str, ClassPolicy]:
    return Roster.read(_write(tmp_path, _CLASS_ROSTER + body + "\n")).classes


def test_a_zero_strength_ceiling_parses_as_zero_not_as_absent(tmp_path: Path) -> None:
    """Zero caps to providers with no strength at all; `or` would read absent."""
    policy = _classes(
        tmp_path, 'cheap = { effort = "low", ceiling = "low", max_strength = 0 }'
    )["cheap"]
    assert policy.max_strength == 0


def test_a_class_without_a_strength_ceiling_is_uncapped(tmp_path: Path) -> None:
    policy = _classes(tmp_path, 'free = { effort = "low", ceiling = "low" }')["free"]
    assert policy.max_strength is None


@pytest.mark.parametrize("written", ["256", "-1", '"7"'])
def test_a_strength_ceiling_off_the_u8_range_is_refused(
    tmp_path: Path, written: str
) -> None:
    with pytest.raises(RosterError):
        _classes(
            tmp_path,
            f'cheap = {{ effort = "low", ceiling = "low", max_strength = {written} }}',
        )


def test_the_provider_tiering_refusal_keeps_its_message(tmp_path: Path) -> None:
    """The class ceiling reuses the provider's helper; its refusal is unchanged."""
    with pytest.raises(RosterError) as refusal:
        Roster.read(
            _write(
                tmp_path,
                '[[provider]]\nname = "p"\nstrength = 256\n',
            )
        )
    assert "provider tiering rank is not a u8" in str(refusal.value)


def test_a_bad_strength_ceiling_names_the_class_and_the_field(tmp_path: Path) -> None:
    """The owner must look in `[classes]`, not in `[[provider]]`, for this one."""
    with pytest.raises(RosterError) as refusal:
        _classes(
            tmp_path,
            'cheap = { effort = "low", ceiling = "low", max_strength = "7" }',
        )
    assert "classes.cheap.max_strength is not a u8" in str(refusal.value)


def test_the_u8_guard_on_the_policy_itself_refuses_a_direct_construction() -> None:
    """Reached only in Python: the parser refuses first, so this is its own proof."""
    with pytest.raises(ValueError, match="300"):
        ClassPolicy(effort="low", ceiling="low", max_strength=300)


def test_a_classes_table_without_unclassified_is_refused(tmp_path: Path) -> None:
    """`unclassified` is the fail-closed ceiling; without it a System1 outage escalates."""
    with pytest.raises(RosterError) as refusal:
        Roster.read(
            _write(
                tmp_path,
                '[classes]\njudgment = { effort = "high", ceiling = "ultra" }\n',
            )
        )
    assert "unclassified" in str(refusal.value)


def test_a_classes_table_with_unclassified_loads(tmp_path: Path) -> None:
    classes = _classes(tmp_path, 'judgment = { effort = "high", ceiling = "ultra" }')
    # `implementation` is never absent from a governed table (it inherits).
    assert set(classes) == {"unclassified", "judgment", "implementation"}


@pytest.mark.parametrize(
    ("body", "inherited"),
    [
        pytest.param(
            'review = { effort = "medium", ceiling = "max" }\n'
            'judgment = { effort = "high", ceiling = "ultra" }',
            "review",
            id="review-first",
        ),
        pytest.param(
            'judgment = { effort = "high", ceiling = "ultra" }',
            "judgment",
            id="judgment",
        ),
        pytest.param(
            'planning = { effort = "high", ceiling = "ultra" }\n'
            'verification = { effort = "medium", ceiling = "high", max_strength = 8 }',
            "verification",
            id="most-restrictive-strength-class",
        ),
        pytest.param(
            'design = { effort = "medium", ceiling = "high" }',
            "unclassified",
            id="no-strength-class",
        ),
    ],
)
def test_implementation_without_a_policy_inherits_one(
    tmp_path: Path, body: str, inherited: str
) -> None:
    """A table written before `implementation` existed still governs it."""
    classes = _classes(tmp_path, body)
    assert classes["implementation"] == classes[inherited]


def test_a_declared_implementation_policy_is_kept(tmp_path: Path) -> None:
    classes = _classes(
        tmp_path,
        'review = { effort = "medium", ceiling = "max" }\n'
        'implementation = { effort = "high", ceiling = "high" }',
    )
    assert classes["implementation"] == ClassPolicy(effort="high", ceiling="high")


#: Capped, and with no `review` line: `implementation` inherits `judgment`.
_CAPPED_LEGACY = """
[classes]
unclassified = { effort = "low", ceiling = "medium", max_strength = 7 }
judgment = { effort = "high", ceiling = "high", max_strength = 7 }

[[provider]]
name = "strong"
default = true
strength = 9
model = "m"

[[provider]]
name = "capped"
strength = 7
model = "m"
"""


def test_an_inherited_implementation_policy_binds_the_picker(tmp_path: Path) -> None:
    roster = Roster.read(_write(tmp_path, _CAPPED_LEGACY))
    picked = pick_execution(roster, 1_000_000, execution_class="implementation")
    assert picked.executed_provider.name == "capped"
    assert picked.effort == "high"
    with pytest.raises(PickerError) as refused:
        pick_execution(
            roster,
            1_000_000,
            execution_class="implementation",
            requested_effort="ultra",
        )
    assert "implementation" in refused.value.message


def test_an_inherited_implementation_policy_falls_closed_on_a_system1_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unavailable(*_args: object, **_kwargs: object) -> object:
        raise system1.System1Unavailable("down")

    monkeypatch.setattr(system1, "ask", unavailable)
    roster = Roster.read(_write(tmp_path, _CAPPED_LEGACY))
    effective, warning, _ = classify.resolve_execution_class(
        "implementation", "implement the agreed fix", roster
    )
    assert effective == UNCLASSIFIED
    assert warning is not None and "implementation" in warning


def test_a_roster_with_no_classes_table_at_all_still_loads(tmp_path: Path) -> None:
    """Governs nothing, so the old behaviour stands: no table, no System1, no cap."""
    roster = Roster.read(_write(tmp_path, '[[provider]]\nname = "p"\n'))
    assert roster.classes == {}


# The HOST roster is not tested here. This test read `~/.nott/spawn.toml` and, on a
# host without that file — runner container, another owner's worktree —
# `Roster.read` returns `classes={}` and it FAILS instead of skipping, red due to
# absent environment and not a defect. A `skip` also won't do: it would be
# inert exactly where the guarantee matters. The plan already names the right owner —
# `plugin/tests/roster-reachability-contract.sh`, where absence is refusal (2) and
# not a skip, and where reading the real consumer (`nspawn_provider_list`) closes the
# same question on the service side.


_ROSTER_SYSTEM_PROMPT_AND_ARGS = (
    '[defaults]\nsystem_prompt = "/law/NOTT.md"\n\n'
    '[[provider]]\nname = "claude1"\nkind = "claude"\n'
    'args = ["--append-system-prompt-file", "/other.md"]\n'
)
_ROSTER_ARGS_TWICE = (
    '[defaults]\nargs = ["--append-system-prompt-file", "/law/NOTT.md", '
    '"--append-system-prompt-file", "/other.md"]\n\n'
    '[[provider]]\nname = "claude1"\nkind = "claude"\n'
)
_ROSTER_JOINED_FORM = (
    '[[provider]]\nname = "claude1"\nkind = "claude"\n'
    'args = ["--append-system-prompt-file=/law/NOTT.md"]\n'
)
_ROSTER_SYSTEM_PROMPT_ONLY = (
    '[defaults]\nsystem_prompt = "/law/NOTT.md"\n\n'
    '[[provider]]\nname = "claude1"\nkind = "claude"\n'
)
_ROSTER_ARGS_ONCE = (
    '[defaults]\nargs = ["--append-system-prompt-file", "/law/NOTT.md"]\n\n'
    '[[provider]]\nname = "claude1"\nkind = "claude"\n'
)


@pytest.mark.parametrize(
    "roster",
    [_ROSTER_SYSTEM_PROMPT_AND_ARGS, _ROSTER_ARGS_TWICE, _ROSTER_JOINED_FORM],
    ids=["system-prompt-and-args", "args-twice", "joined-form"],
)
def test_a_system_prompt_the_child_would_not_read_as_declared_is_refused(
    tmp_path: Path, roster: str
) -> None:
    """The claude CLI appends only the LAST --append-system-prompt-file, and nSPAWN
    tells whether a child already carries the law from the flag and its path as two
    items. A second flag, or the joined form, would let the child run without the law
    while its hook copy is silenced; the roster says so instead, naming the provider."""
    with pytest.raises(RosterError, match="claude1") as refused:
        Roster.read(_write(tmp_path, roster)).resolve("claude1")
    assert "declare the prompt once" in refused.value.message
    assert "the flag and its path as separate items" in refused.value.message


@pytest.mark.parametrize(
    "roster",
    [_ROSTER_SYSTEM_PROMPT_ONLY, _ROSTER_ARGS_ONCE],
    ids=["system-prompt", "args"],
)
def test_a_system_prompt_declared_once_resolves_to_one_flag(
    tmp_path: Path, roster: str
) -> None:
    args = Roster.read(_write(tmp_path, roster)).resolve("claude1").args
    assert args.count("--append-system-prompt-file") == 1
    assert args[args.index("--append-system-prompt-file") + 1] == "/law/NOTT.md"


def test_native_write_roots_expand_home_and_default_to_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert Roster.read(_write(tmp_path, "[defaults]\n")).native_write_roots == ()
    roster = Roster.read(
        _write(tmp_path, '[native_write]\nroots = ["~/areas", "/srv/clones"]\n')
    )
    assert roster.native_write_roots == (tmp_path / "areas", Path("/srv/clones"))


@pytest.mark.parametrize(
    "text",
    [
        '[native_write]\nroots = ["areas"]\n',
        '[native_write]\nroots = [""]\n',
        '[native_write]\nroots = "/srv/clones"\n',
        "native_write = 1\n",
    ],
    ids=["relative", "empty", "not-a-list", "not-a-table"],
)
def test_a_malformed_native_write_root_is_refused(tmp_path: Path, text: str) -> None:
    with pytest.raises(RosterError, match="native_write"):
        Roster.read(_write(tmp_path, text))


_ROUTES = (
    '[bins]\ncodex = "/usr/bin/true"\n'
    '[[provider]]\nname = "codex"\nkind = "codex"\nmodel = "m"\n'
    'accounts = { a = { home = "/h/a" } }\n'
    '[[provider]]\nname = "claude"\nkind = "claude"\nmodel = "m"\n'
)


def test_roles_bind_each_workflow_role_to_a_declared_route(tmp_path: Path) -> None:
    text = _ROUTES + '[roles]\nwriter = "codex"\nreviewer = "claude"\n'
    roster = Roster.read(_write(tmp_path, text))
    assert roster.roles == {"writer": "codex", "reviewer": "claude"}


_CATALOGUED = (
    '[bins]\ncodex = "/usr/bin/true"\n'
    '[[provider]]\nname = "claude"\nkind = "claude"\nmodel = "m"\n'
    'models = ["m", "big"]\nefforts = ["low", "xhigh"]\n'
    '[roles]\nwriter = "claude"\nreviewer = "claude"\n'
)


def test_a_role_profile_gives_the_reviewer_its_model_and_effort(tmp_path: Path) -> None:
    text = _CATALOGUED + '[role_profile.reviewer]\nmodel = "big"\neffort = "xhigh"\n'
    roster = Roster.read(_write(tmp_path, text))
    # The route is still declared once, in [roles]; the profile adds only these two.
    assert roster.roles == {"writer": "claude", "reviewer": "claude"}
    assert roster.role_profiles == {"reviewer": {"model": "big", "effort": "xhigh"}}


def test_no_role_profile_leaves_every_request_as_it_was(tmp_path: Path) -> None:
    assert Roster.read(_write(tmp_path, _CATALOGUED)).role_profiles == {}


def test_a_role_profile_gives_the_writer_its_model_and_effort(tmp_path: Path) -> None:
    text = (
        _CATALOGUED
        + '[role_profile.writer]\nmodel = "big"\neffort = "xhigh"\n'
        + '[role_profile.reviewer]\nmodel = "m"\neffort = "low"\n'
    )
    roster = Roster.read(_write(tmp_path, text))
    assert roster.roles == {"writer": "claude", "reviewer": "claude"}
    assert roster.role_profiles == {
        "writer": {"model": "big", "effort": "xhigh"},
        "reviewer": {"model": "m", "effort": "low"},
    }


@pytest.mark.parametrize(
    ("table", "named"),
    [
        (
            '[role_profile.writer]\nprovider = "claude"\n',
            "role_profile.writer.provider",
        ),
        ('[role_profile.writer]\nmodel = "gone"\n', "role_profile.writer.model"),
        ('[role_profile.writer]\neffort = "max"\n', "role_profile.writer.effort"),
        ('[role_profile.writer]\nmodel = ""\n', "role_profile.writer.model"),
        ("[role_profile.writer]\n", "role_profile.writer"),
        ("[role_profile]\nwriter = 1\n", "role_profile.writer"),
    ],
    ids=[
        "unknown-key",
        "model-not-served",
        "effort-not-served",
        "empty-model",
        "empty-profile",
        "not-a-table",
    ],
)
def test_a_malformed_writer_profile_is_refused_by_its_key(
    tmp_path: Path, table: str, named: str
) -> None:
    with pytest.raises(RosterError, match=named.replace(".", r"\.")):
        Roster.read(_write(tmp_path, _CATALOGUED + table))


def test_a_writer_profile_needs_a_writer_route(tmp_path: Path) -> None:
    text = (
        '[[provider]]\nname = "claude"\nkind = "claude"\nmodel = "m"\n'
        '[roles]\nreviewer = "claude"\n'
        '[role_profile.writer]\nmodel = "m"\n'
    )
    with pytest.raises(RosterError, match=r"role_profile\.writer needs roles\.writer"):
        Roster.read(_write(tmp_path, text))


def test_a_writer_effort_the_codex_transport_rewrites_is_refused(
    tmp_path: Path,
) -> None:
    text = (
        '[bins]\ncodex = "/usr/bin/true"\n'
        '[[provider]]\nname = "codex"\nkind = "codex"\nmodel = "m"\n'
        'efforts = ["low", "ultra"]\n'
        'accounts = { a = { home = "/h/a" } }\n'
        '[roles]\nwriter = "codex"\n'
        '[role_profile.writer]\neffort = "ultra"\n'
    )
    with pytest.raises(RosterError, match=r"role_profile\.writer\.effort 'ultra'"):
        Roster.read(_write(tmp_path, text))


@pytest.mark.parametrize("role", ["writer", "reviewer"])
def test_a_profile_cannot_use_an_effort_absent_from_a_rewritten_only_route(
    tmp_path: Path, role: str
) -> None:
    text = (
        '[bins]\ncodex = "/usr/bin/true"\n'
        '[[provider]]\nname = "codex"\nkind = "codex"\nmodel = "m"\n'
        'efforts = ["ultra"]\naccounts = { a = { home = "/h/a" } }\n'
        f'[roles]\n{role} = "codex"\n'
        f'[role_profile.{role}]\neffort = "high"\n'
    )
    with pytest.raises(RosterError, match=f"role_profile.{role}.effort"):
        Roster.read(_write(tmp_path, text))


#: The writer runs as `judgment`, the reviewer as `review`: two ceilings that
#: differ, so each profile must meet its own class and not the other's.
_SPLIT_CEILINGS = (
    '[classes]\nunclassified = { effort = "low", ceiling = "low" }\n'
    'judgment = { effort = "low", ceiling = "xhigh" }\n'
    'review = { effort = "low", ceiling = "low" }\n'
)


def test_a_writer_effort_meets_the_judgment_ceiling_not_the_review_one(
    tmp_path: Path,
) -> None:
    text = _CATALOGUED + '[role_profile.writer]\neffort = "xhigh"\n' + _SPLIT_CEILINGS
    roster = Roster.read(_write(tmp_path, text))
    assert roster.role_profiles == {"writer": {"effort": "xhigh"}}


def test_a_writer_effort_above_the_judgment_ceiling_is_refused(
    tmp_path: Path,
) -> None:
    text = (
        _CATALOGUED
        + '[role_profile.writer]\neffort = "xhigh"\n'
        + '[classes]\nunclassified = { effort = "low", ceiling = "low" }\n'
        + 'judgment = { effort = "low", ceiling = "low" }\n'
        + 'review = { effort = "low", ceiling = "xhigh" }\n'
    )
    with pytest.raises(
        RosterError, match=r"role_profile\.writer\.effort 'xhigh' is above the judgment"
    ):
        Roster.read(_write(tmp_path, text))


def test_a_reviewer_effort_still_meets_the_review_ceiling_not_the_judgment_one(
    tmp_path: Path,
) -> None:
    text = _CATALOGUED + '[role_profile.reviewer]\neffort = "xhigh"\n' + _SPLIT_CEILINGS
    with pytest.raises(
        RosterError, match=r"role_profile\.reviewer\.effort 'xhigh' is above the review"
    ):
        Roster.read(_write(tmp_path, text))


@pytest.mark.parametrize(
    ("table", "named"),
    [
        ('[role_profile.planner]\nmodel = "m"\n', "role_profile.planner"),
        (
            '[role_profile.reviewer]\nprovider = "claude"\n',
            "role_profile.reviewer.provider",
        ),
        ('[role_profile.reviewer]\nmodel = "gone"\n', "role_profile.reviewer.model"),
        ('[role_profile.reviewer]\neffort = "max"\n', "role_profile.reviewer.effort"),
        ('[role_profile.reviewer]\nmodel = ""\n', "role_profile.reviewer.model"),
        ("[role_profile.reviewer]\neffort = 1\n", "role_profile.reviewer.effort"),
        ("[role_profile.reviewer]\n", "role_profile.reviewer"),
        ("[role_profile]\nreviewer = 1\n", "role_profile.reviewer"),
    ],
    ids=[
        "unknown-role",
        "route-is-not-a-profile-key",
        "model-not-served",
        "effort-not-served",
        "empty-model",
        "effort-not-a-name",
        "empty-profile",
        "not-a-table",
    ],
)
def test_a_malformed_role_profile_is_refused_by_its_key(
    tmp_path: Path, table: str, named: str
) -> None:
    with pytest.raises(RosterError, match=named.replace(".", r"\.")):
        Roster.read(_write(tmp_path, _CATALOGUED + table))


@pytest.mark.parametrize(
    "text",
    [
        _CATALOGUED
        + '[role_profile.reviewer]\neffort = "xhigh"\n'
        + '[classes]\nunclassified = { effort = "low", ceiling = "low" }\n'
        + 'review = { effort = "low", ceiling = "low" }\n',
        (
            '[bins]\ncodex = "/usr/bin/true"\n'
            '[[provider]]\nname = "codex"\nkind = "codex"\nmodel = "m"\n'
            'efforts = ["low", "ultra"]\n'
            'accounts = { a = { home = "/h/a" } }\n'
            '[roles]\nreviewer = "codex"\n'
            '[role_profile.reviewer]\neffort = "ultra"\n'
        ),
        (
            '[bins]\ncodex = "/usr/bin/true"\n'
            '[[provider]]\nname = "codex"\nkind = "codex"\nmodel = "m"\n'
            'accounts = { a = { home = "/h/a" } }\n'
            '[roles]\nreviewer = "codex"\n'
            '[role_profile.reviewer]\neffort = "ultra"\n'
        ),
    ],
    ids=[
        "above-the-review-ceiling",
        "rewritten-by-the-codex-transport",
        "rewritten-with-no-declared-efforts",
    ],
)
def test_a_profile_effort_every_review_would_refuse_is_refused_at_load(
    tmp_path: Path, text: str
) -> None:
    """The workflow freezes the profile into its request, so an effort the picker
    refuses for every review would leave that workflow unable to review at all."""
    with pytest.raises(RosterError, match=r"role_profile\.reviewer\.effort"):
        Roster.read(_write(tmp_path, text))


@pytest.mark.parametrize(
    "route",
    [
        '[provider.env]\nCLAUDE_CODE_EFFORT_LEVEL = "low"\n',
        '[provider.env]\nANTHROPIC_MODEL = "m"\n',
        'args = ["--model", "m"]\n',
    ],
    ids=["env-effort", "env-model", "args-model"],
)
def test_a_profile_loads_beside_env_or_args_the_caged_reviewer_never_receives(
    tmp_path: Path, route: str
) -> None:
    """The workflow reviewer needs `custody`, which only a direct route serves, and
    a direct route reviews in the cage: its closed env and argv never carry the
    roster's env or args, so they cannot replace the profile there."""
    text = (
        '[[provider]]\nname = "claude"\nkind = "claude"\nmodel = "m"\n'
        'models = ["m", "big"]\nefforts = ["low", "xhigh"]\n'
        + route
        + '[roles]\nreviewer = "claude"\n'
        + '[role_profile.reviewer]\nmodel = "big"\neffort = "xhigh"\n'
    )
    roster = Roster.read(_write(tmp_path, text))
    assert roster.role_profiles == {"reviewer": {"model": "big", "effort": "xhigh"}}


def test_a_profile_for_a_role_left_unbound_is_refused(tmp_path: Path) -> None:
    """The profile belongs to a route the owner declared; the workflow default
    route is another provider, so its model and effort would not apply."""
    text = _ROUTES + '[role_profile.reviewer]\neffort = "low"\n'
    with pytest.raises(RosterError, match=r"role_profile\.reviewer"):
        Roster.read(_write(tmp_path, text))


def test_no_roles_table_binds_no_role(tmp_path: Path) -> None:
    # The workflow owns the default; the roster states only what the owner wrote.
    assert Roster.read(_write(tmp_path, _ROUTES)).roles == {}


@pytest.mark.parametrize(
    ("table", "named"),
    [
        ('[roles]\norchestrator = "codex"\n', "roles.orchestrator"),
        ('[roles]\nwriter = "gone"\n', "roles.writer"),
        ("[roles]\nwriter = 1\n", "roles.writer"),
        ("roles = 1\n", "roles"),
    ],
    ids=["unknown-role", "undeclared-route", "not-a-name", "not-a-table"],
)
def test_a_malformed_role_binding_is_refused_by_its_key(
    tmp_path: Path, table: str, named: str
) -> None:
    text = table + _ROUTES if table.startswith("roles =") else _ROUTES + table
    with pytest.raises(RosterError, match=named):
        Roster.read(_write(tmp_path, text))


def test_roster_profiles_require_declared_names_without_a_catalogue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = _CATALOGUED.replace('models = ["m", "big"]', 'models = ["opus", "haiku"]')
    text += '[role_profile.writer]\nmodel = "claude-opus-5-5"\n'

    def unavailable(_: object) -> dict[str, str]:
        pytest.fail("The roster read must not start the test client")

    monkeypatch.setattr("nspawn.claude_catalog.catalog_read", unavailable)
    with pytest.raises(RosterError, match="not served"):
        Roster.read(_write(tmp_path, text))
    text = text.replace(
        'models = ["opus", "haiku"]', 'models = ["opus", "haiku", "claude-opus-5-5"]'
    )
    roster = Roster.read(_write(tmp_path, text))
    assert roster.role_profiles["writer"]["model"] == "claude-opus-5-5"


def test_versioned_template_roster_loads() -> None:
    from workspace_root import WORKSPACE_ROOT

    roster = Roster.read(WORKSPACE_ROOT / "templates" / "nott" / "spawn.toml")
    assert roster.providers
