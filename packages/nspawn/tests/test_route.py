"""What each vendor's node starts with, pinned whole: the route it records, the
environment set over the host's own, the adapter's arguments and its command, for
every route form a node can carry, and each refusal on the way.

Written before the node's launch profiles moved into nSPAWN's route layer, against
the node's own seam, and kept whole across the move: `native_profile` and
`native_command` below are what `nspawn route start` resolves and execs, so a move
that changes one variable, one argument or one refusal reads RED here. The roster is
shaped like the owner's: a `[defaults.env]` no node receives, discovered claude
accounts, qwen accounts with their own endpoint, model and window, a codex home
under `~/`, and absolute agy homes. Every token is a fixture.
"""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path

import pytest

from nspawn import cli, route
from nspawn.registry import create_store, store_path
from nspawn.route import RouteRefused as NodeRefused


@pytest.mark.parametrize("verb", ["list", "switch"])
def test_route_cli_catalogue_error_is_typed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], verb: str
) -> None:
    from nspawn.claude_catalog import ModelCatalogError

    def refuse():
        raise ModelCatalogError("The test catalogue is unavailable")

    monkeypatch.setattr(route, "read_roster", refuse)
    options = ["claude", "--model", "default", "--effort", "high"]
    if verb == "switch":
        options += ["--route", "alpha@/test-home"]
        monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"{}")))
    handler = cli.cmd_route_list if verb == "list" else cli.cmd_route_switch
    assert handler(options) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "ModelCatalogError" in captured.err
    assert "Traceback" not in captured.err


def test_cli_main_catalogue_error_is_typed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from nspawn.claude_catalog import ModelCatalogError

    def refuse(arguments):
        raise ModelCatalogError("The test catalogue is unavailable")

    command = cli.COMMANDS[("route", "list")]
    monkeypatch.setitem(
        cli.COMMANDS, ("route", "list"), command._replace(handler=refuse)
    )
    assert cli.main(["route", "list", "claude"]) == 1
    captured = capsys.readouterr()
    assert "failed" in captured.err
    assert "Traceback" not in captured.err


TOKENS = {
    "alpha": "fixture-token-alpha",
    "beta": "fixture-token-beta",
    "server": "fixture-token-server",
    "m5": "fixture-token-m5",
    "plain": "fixture-token-plain",
}

#: Spelled out rather than imported: what a claude node blanks over the host's env.
CLAUDE_CLEARED = dict.fromkeys(
    (
        "CLAUDE_CODE_OAUTH_TOKEN",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_SMALL_FAST_MODEL",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
    ),
    "",
)
#: A qwen node blanks the same names but the small fast model.
QWEN_CLEARED = {
    name: "" for name in CLAUDE_CLEARED if name != "ANTHROPIC_SMALL_FAST_MODEL"
}
CODEX_ARGS = ["-c", 'cli_auth_credentials_store="file"']
AGY = ({"NOTT_NSPAWN_AGY_ACCOUNT": "agy1"}, ["-m", "nspawn.agy_acp"])

ROSTER = """\
[defaults]
env = {{ CLAUDE_CODE_ENABLE_TELEMETRY = "0", CLAUDE_CODE_AUTO_COMPACT_WINDOW = "200000" }}

[bins]
codex = "{root}/bin/codex"
agy = "{root}/bin/agy"

[[provider]]
name = "claude"
kind = "claude"
default = true
secrets = "{root}/claude-secrets"
model = "claude-opus-5"
model_fast = "claude-haiku-4-5"

[[provider]]
name = "qwen"
kind = "claude"
secrets = "{root}/qwen-secrets"
model = "qwen-provider-model"
model_fast = "qwen-provider-fast"
env = {{ CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS = "1", ENABLE_TOOL_SEARCH = "false" }}

[provider.accounts.server]
base_url = "http://127.0.0.1:18020"
auth_env = "ANTHROPIC_AUTH_TOKEN"
model = "qwen3.8-27b"
env = {{ CLAUDE_CODE_AUTO_COMPACT_WINDOW = "245760" }}

[provider.accounts.m5]
base_url = "http://m5.invalid:11434"
auth_env = "ANTHROPIC_AUTH_TOKEN"
model = "qwen3.8:27b"
env = {{ CLAUDE_CODE_AUTO_COMPACT_WINDOW = "65536" }}

[provider.accounts.plain]
base_url = "http://127.0.0.1:18021"
auth_env = "ANTHROPIC_API_KEY"

[[provider]]
name = "codex"
kind = "codex"
model = "gpt-provider-model"

[provider.accounts.codex-main]
home = "~/.codex-main"

[provider.accounts.codex-work]
home = "{root}/codex-work"
sqlite_home = "{root}/codex-work-db"
model = "gpt-account-model"

[[provider]]
name = "agy"
kind = "agy"
model = "gemini-high"

[provider.accounts.agy1]
home = "{root}/agy1"

[provider.accounts.agy2]
home = "{root}/agy2"
"""


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A roster beside its secrets, HOME and the claude home, all in `tmp_path`."""
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-home"))
    for name in ("NOTT_NODE_CODEX_ADAPTER", "CODEX_PATH", "CODEX_CONFIG"):
        monkeypatch.delenv(name, raising=False)
    for provider, aliases in (
        ("claude", ("alpha", "beta")),
        ("qwen", ("server", "m5", "plain")),
    ):
        secrets = tmp_path / f"{provider}-secrets"
        secrets.mkdir(mode=0o700)
        for alias in aliases:
            (secrets / f"{alias}.token").write_text(TOKENS[alias] + "\n")
    roster = tmp_path / "spawn.toml"
    roster.write_text(ROSTER.format(root=tmp_path))
    monkeypatch.setenv("NOTT_NSPAWN_ROSTER", str(roster))
    return tmp_path


def native_profile(
    provider: str, recorded: str | None = None
) -> tuple[str, dict[str, str], list[str]]:
    return route.route_start(
        route.read_roster(),
        provider,
        recorded,
        codex_adapter=os.environ.get("NOTT_NODE_CODEX_ADAPTER"),
    )


def native_command(provider: str, arguments: list[str]) -> list[str]:
    return route.adapter_command(
        provider, arguments, codex_adapter=os.environ.get("NOTT_NODE_CODEX_ADAPTER")
    )


def edit(root: Path, old: str, new: str) -> None:
    roster = root / "spawn.toml"
    text = roster.read_text()
    assert old in text, old
    roster.write_text(text.replace(old, new, 1))


def test_claude_new_recorded_and_bare_home_routes(root: Path) -> None:
    home = str(root / "claude-home")
    oauth = "CLAUDE_CODE_OAUTH_TOKEN"
    # A new node takes the first discovered account; no model, effort or
    # `[defaults.env]` rides in its environment, only the account's token.
    assert native_profile("claude") == (
        f"alpha@{home}",
        {**CLAUDE_CLEARED, oauth: TOKENS["alpha"]},
        [],
    )
    assert native_profile("claude", f"beta@{home}") == (
        f"beta@{home}",
        {**CLAUDE_CLEARED, oauth: TOKENS["beta"]},
        [],
    )
    # A node made before accounts keeps the home's own login.
    assert native_profile("claude", home) == (home, CLAUDE_CLEARED, [])


def test_an_excluded_claude_account_is_no_node_route(root: Path) -> None:
    home = str(root / "claude-home")
    edit(root, 'name = "claude"\n', 'name = "claude"\nexcluded_accounts = ["alpha"]\n')
    # A new node skips it, as the picker does.
    assert native_profile("claude")[0] == f"beta@{home}"
    # A node made on it before the exclusion is refused, before its token is read.
    with pytest.raises(NodeRefused, match="excluded in the roster"):
        native_profile("claude", f"alpha@{home}")
    edit(root, '["alpha"]', '["alpha", "beta"]')
    with pytest.raises(NodeRefused, match="cooling down or excluded"):
        native_profile("claude")


def test_a_declared_claude_account_brings_its_endpoint_and_variable(
    root: Path,
) -> None:
    home = str(root / "claude-home")
    (root / "claude-secrets/gateway.token").write_text("fixture-token-gateway\n")
    edit(
        root,
        'model_fast = "claude-haiku-4-5"\n',
        'model_fast = "claude-haiku-4-5"\n\n[provider.accounts.alpha]\n\n'
        '[provider.accounts.gateway]\nbase_url = "https://gateway.invalid"\n'
        'auth_env = "ANTHROPIC_AUTH_TOKEN"\n',
    )
    assert native_profile("claude", f"gateway@{home}") == (
        f"gateway@{home}",
        {
            **CLAUDE_CLEARED,
            "ANTHROPIC_BASE_URL": "https://gateway.invalid",
            "ANTHROPIC_AUTH_TOKEN": "fixture-token-gateway",
        },
        [],
    )
    assert native_profile("claude")[1] == {
        **CLAUDE_CLEARED,
        "CLAUDE_CODE_OAUTH_TOKEN": TOKENS["alpha"],
    }


def test_a_claude_node_runs_the_roster_claude_code_not_the_adapter_bundled_one(
    root: Path,
) -> None:
    # claude-agent-acp bundles an older Claude Code, whose catalog lacks the roster's
    # newer models; `[bins].claude` is the one the owner runs.
    (root / "bin").mkdir(exist_ok=True)
    binary = root / "bin/claude"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o700)
    edit(root, "[bins]\n", f'[bins]\nclaude = "{root}/bin/claude"\n')
    home = str(root / "claude-home")
    for recorded in (None, home):
        environment = native_profile("claude", recorded)[1]
        assert environment["CLAUDE_CODE_EXECUTABLE"] == str(binary.resolve())
    # One others may write is refused, as nSPAWN's own runs refuse it.
    binary.chmod(0o722)
    with pytest.raises(NodeRefused, match="bins"):
        native_profile("claude")
    binary.chmod(0o700)
    # A roster that names no Claude Code leaves the adapter's own.
    edit(root, f'claude = "{root}/bin/claude"\n', "")
    assert "CLAUDE_CODE_EXECUTABLE" not in native_profile("claude")[1]


def test_darwin_route_refuses_a_missing_product_executable_before_credentials(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(route.sys, "platform", "darwin")

    def account(*args, **kwargs):
        pytest.fail("The route read credentials before it selected an executable.")

    monkeypatch.setattr(route, "account_token", account)
    with pytest.raises(NodeRefused, match=r"\[bins\]\.claude"):
        native_profile("claude")


def test_a_roster_without_claude_accounts_runs_the_home_login(root: Path) -> None:
    home = str(root / "claude-home")
    edit(root, 'name = "claude"', 'name = "anthropic"')
    assert native_profile("claude") == (home, CLAUDE_CLEARED, [])
    assert native_profile("claude", home) == (home, CLAUDE_CLEARED, [])


def qwen_env(
    account: str, model: str, fast: str, variable: str, url: str, window: str | None
) -> dict[str, str]:
    environment = {
        **QWEN_CLEARED,
        "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
        "ENABLE_TOOL_SEARCH": "false",
        "ANTHROPIC_MODEL": model,
        "ANTHROPIC_DEFAULT_OPUS_MODEL": model,
        "ANTHROPIC_DEFAULT_SONNET_MODEL": model,
        "CLAUDE_CODE_SUBAGENT_MODEL": model,
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": fast,
        "ANTHROPIC_BASE_URL": url,
        variable: TOKENS[account],
    }
    if window is not None:
        environment["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = window
    return environment


def test_qwen_new_and_recorded_routes(root: Path) -> None:
    server = "server@http://127.0.0.1:18020"
    # An account's own model is its fast lane too; the provider's fast lane serves
    # only an account that names no model.
    assert native_profile("qwen") == (
        server,
        qwen_env(
            "server",
            "qwen3.8-27b",
            "qwen3.8-27b",
            "ANTHROPIC_AUTH_TOKEN",
            "http://127.0.0.1:18020",
            "245760",
        ),
        [],
    )
    m5 = "m5@http://m5.invalid:11434"
    assert native_profile("qwen", m5) == (
        m5,
        qwen_env(
            "m5",
            "qwen3.8:27b",
            "qwen3.8:27b",
            "ANTHROPIC_AUTH_TOKEN",
            "http://m5.invalid:11434",
            "65536",
        ),
        [],
    )
    plain = "plain@http://127.0.0.1:18021"
    assert native_profile("qwen", plain) == (
        plain,
        qwen_env(
            "plain",
            "qwen-provider-model",
            "qwen-provider-fast",
            "ANTHROPIC_API_KEY",
            "http://127.0.0.1:18021",
            None,
        ),
        [],
    )


def test_route_show_names_the_route_and_the_models_a_node_reports(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Without --route the picker reads the account walls in the store.
    create_store(store_path())
    plain = "plain@http://127.0.0.1:18021"
    assert cli.main(["route", "show", "qwen", "--route", plain]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "route": plain,
        "model": "qwen-provider-model",
        "model_fast": "qwen-provider-fast",
        "config_dir": None,
    }
    assert cli.main(["route", "show", "claude"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "route": f"alpha@{root / 'claude-home'}",
        "model": None,
        "model_fast": None,
        "config_dir": None,
    }
    assert cli.main(["route", "show", "agy", "--route", "gone@/x"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "the agy account this node was made on is no longer in the roster\n"
    )


def test_codex_new_recorded_and_bare_home_routes(root: Path) -> None:
    main = str(root / "home/.codex-main")
    assert native_profile("codex") == (
        f"codex-main@{main}",
        {"CODEX_HOME": main, "CODEX_SQLITE_HOME": main},
        [*CODEX_ARGS, "-c", 'model="gpt-provider-model"'],
    )
    work = str(root / "codex-work")
    assert native_profile("codex", f"codex-work@{work}") == (
        f"codex-work@{work}",
        {"CODEX_HOME": work, "CODEX_SQLITE_HOME": str(root / "codex-work-db")},
        [*CODEX_ARGS, "-c", 'model="gpt-account-model"'],
    )
    # A route made before accounts names only the home of its one account.
    assert native_profile("codex", work) == (
        work,
        {"CODEX_HOME": work, "CODEX_SQLITE_HOME": str(root / "codex-work-db")},
        [*CODEX_ARGS, "-c", 'model="gpt-account-model"'],
    )


def test_a_bare_home_route_on_an_excluded_codex_account_is_refused(
    root: Path,
) -> None:
    work = str(root / "codex-work")
    edit(
        root, 'name = "codex"\n', 'name = "codex"\nexcluded_accounts = ["codex-work"]\n'
    )
    for recorded in (work, f"codex-work@{work}"):
        with pytest.raises(NodeRefused, match="excluded in the roster"):
            native_profile("codex", recorded)


def test_an_external_codex_adapter_takes_its_configuration_as_json(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main = str(root / "home/.codex-main")
    monkeypatch.setenv("NOTT_NODE_CODEX_ADAPTER", str(root / "bin/codex-adapter"))
    monkeypatch.setenv("CODEX_PATH", str(root / "bin/codex"))
    monkeypatch.setenv("CODEX_CONFIG", json.dumps({"features": {"fixture": True}}))
    assert native_profile("codex") == (
        f"codex-main@{main}",
        {
            "CODEX_HOME": main,
            "CODEX_SQLITE_HOME": main,
            "CODEX_PATH": str(root / "bin/codex"),
            "CODEX_CONFIG": json.dumps(
                {
                    "features": {"fixture": True},
                    "cli_auth_credentials_store": "file",
                    "model": "gpt-provider-model",
                }
            ),
        },
        [],
    )


def test_agy_new_and_recorded_routes(root: Path) -> None:
    environment, arguments = AGY
    assert native_profile("agy") == (
        f"agy1@{root / 'agy1'}",
        environment,
        arguments,
    )
    assert native_profile("agy", f"agy2@{root / 'agy2'}") == (
        f"agy2@{root / 'agy2'}",
        {"NOTT_NSPAWN_AGY_ACCOUNT": "agy2"},
        arguments,
    )


@pytest.mark.parametrize(
    ("provider", "recorded", "change", "reason"),
    [
        (
            "claude",
            "gone@{root}/claude-home",
            None,
            "the claude account this node was made on is no longer in the roster",
        ),
        (
            "claude",
            None,
            ("claude-secrets/alpha.token", b"\xff\xfe\n"),
            "claude account alpha has no readable token",
        ),
        (
            "claude",
            None,
            ("claude-secrets/alpha.token", "fixture-one\nfixture-two\n"),
            "claude account alpha token file holds no single token",
        ),
        (
            "claude",
            None,
            ("claude-secrets/alpha.token", " \n"),
            "claude account alpha token file holds no single token",
        ),
        (
            "qwen",
            None,
            ("qwen-secrets/server.token", None),
            "qwen account token file is unreadable",
        ),
        (
            "qwen",
            None,
            ("qwen-secrets/server.token", "fixture-one\nfixture-two\n"),
            "qwen account token file holds no single token",
        ),
        (
            "qwen",
            "gone@http://127.0.0.1:18020",
            None,
            "the qwen account this node was made on is no longer in the roster",
        ),
        (
            "codex",
            "{root}/elsewhere",
            None,
            (
                "the Codex account this node was made on is no longer one account "
                "in the roster"
            ),
        ),
        (
            "agy",
            "gone@{root}/agy1",
            None,
            "the agy account this node was made on is no longer in the roster",
        ),
    ],
)
def test_a_route_it_cannot_start_is_refused(
    root: Path,
    provider: str,
    recorded: str | None,
    change: tuple[str, str | bytes | None] | None,
    reason: str,
) -> None:
    if change is not None:
        path, content = change
        if content is None:
            (root / path).unlink()
        else:
            (root / path).write_bytes(
                content if isinstance(content, bytes) else content.encode()
            )
    with pytest.raises(NodeRefused) as refused:
        native_profile(
            provider, None if recorded is None else recorded.format(root=root)
        )
    assert str(refused.value) == reason


@pytest.mark.parametrize(
    ("provider", "old", "new", "reason"),
    [
        (
            "qwen",
            'auth_env = "ANTHROPIC_AUTH_TOKEN"\nmodel = "qwen3.8-27b"',
            'model = "qwen3.8-27b"',
            "qwen account requires base_url, auth_env and secrets",
        ),
        (
            "qwen",
            'auth_env = "ANTHROPIC_AUTH_TOKEN"\nmodel = "qwen3.8-27b"',
            'auth_env = "QWEN_TOKEN"\nmodel = "qwen3.8-27b"',
            "qwen auth_env must name an Anthropic API credential",
        ),
        ("qwen", 'name = "qwen"', 'name = "qwen-off"', None),
        ("codex", 'name = "codex"', 'name = "codex-off"', None),
        ("agy", 'name = "agy"\nkind = "agy"', 'name = "agy"\nkind = "codex"', None),
        (
            "agy",
            'home = "{root}/agy1"',
            'home = "~/agy1"',
            "agy account requires an absolute home",
        ),
    ],
)
def test_a_roster_that_cannot_route_a_node_is_refused(
    root: Path, provider: str, old: str, new: str, reason: str | None
) -> None:
    edit(root, old.format(root=root), new)
    label = {"qwen": "qwen", "codex": "Codex", "agy": "agy"}[provider]
    with pytest.raises(NodeRefused) as refused:
        native_profile(provider)
    assert str(refused.value) == (
        reason or f"{label} roster must declare a native account"
    )


@pytest.mark.parametrize(
    ("adapter", "cli", "config", "reason"),
    [
        (
            "codex-adapter",
            "/bin/codex",
            "{}",
            "NOTT_NODE_CODEX_ADAPTER must name an absolute native adapter",
        ),
        (
            "/bin/codex-adapter",
            "codex",
            "{}",
            "CODEX_PATH must name the configured absolute native CLI",
        ),
        (
            "/bin/codex-adapter",
            "/bin/codex",
            "{not json",
            "CODEX_CONFIG must contain a JSON configuration object",
        ),
        (
            "/bin/codex-adapter",
            "/bin/codex",
            "[]",
            "CODEX_CONFIG must contain a JSON configuration object",
        ),
    ],
)
def test_an_external_codex_adapter_it_cannot_configure_is_refused(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    adapter: str,
    cli: str,
    config: str,
    reason: str,
) -> None:
    monkeypatch.setenv("NOTT_NODE_CODEX_ADAPTER", adapter)
    monkeypatch.setenv("CODEX_PATH", cli)
    monkeypatch.setenv("CODEX_CONFIG", config)
    with pytest.raises(NodeRefused) as refused:
        native_profile("codex")
    assert str(refused.value) == reason


def executable(path: Path, text: str = "#!/bin/sh\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o700)
    return path


def test_each_vendor_starts_its_own_adapter(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands = root / "commands"
    claude = executable(commands / "claude-agent-acp")
    codex = executable(commands / "codex-acp")
    monkeypatch.setenv("PATH", str(commands))
    assert native_command("agy", ["-m", "nspawn.agy_acp"]) == [
        sys.executable,
        "-m",
        "nspawn.agy_acp",
    ]
    assert native_command("claude", []) == [str(claude)]
    assert native_command("qwen", ["--x"]) == [str(claude), "--x"]
    assert native_command("codex", CODEX_ARGS) == [str(codex), *CODEX_ARGS]
    external = executable(root / "external/codex-adapter")
    monkeypatch.setenv("NOTT_NODE_CODEX_ADAPTER", str(external))
    assert native_command("codex", []) == [str(external)]


def test_an_adapter_found_beside_npm_or_as_a_script_runs_as_installed(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands = root / "commands"
    commands.mkdir()
    monkeypatch.setenv("PATH", str(commands))
    with pytest.raises(NodeRefused) as refused:
        native_command("claude", [])
    assert str(refused.value) == "native adapter claude-agent-acp is not installed"
    installed = executable(root / "home/.npm-global/bin/codex-acp")
    assert native_command("codex", ["--y"]) == [str(installed), "--y"]
    entry = executable(root / "package/dist/index.js", "// fixture\n")
    os.symlink(entry, commands / "claude-agent-acp")
    with pytest.raises(NodeRefused) as refused:
        native_command("claude", [])
    assert str(refused.value) == "claude ACP requires the installed node runtime"
    runtime = executable(commands / "node")
    assert native_command("claude", ["--z"]) == [str(runtime), str(entry), "--z"]


def test_route_discovery_preserves_the_account_and_refuses_an_unmanaged_launch(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    commands = root / "commands"
    executable(commands / "claude-agent-acp")
    monkeypatch.setenv("PATH", str(commands))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fixture-inherited")
    started: list[tuple[str, list[str], dict[str, str]]] = []
    monkeypatch.setattr(
        os, "execve", lambda path, argv, env: started.append((path, argv, env))
    )
    home = str(root / "claude-home")
    assert cli.main(["route", "start", "claude", "--route", f"beta@{home}"]) == 2
    assert started == []
    assert "nspawn_run_start" in capsys.readouterr().err
    argv, environment = route.route_launch("claude", f"beta@{home}")
    assert argv == [str(commands / "claude-agent-acp")]
    assert environment["CLAUDE_CODE_OAUTH_TOKEN"] == TOKENS["beta"]
    assert environment["ANTHROPIC_API_KEY"] == ""
    assert environment["PATH"] == str(commands)
    # --check resolves and refuses the same launch, and starts nothing.
    assert cli.main(["route", "start", "claude", "--route", home, "--check"]) == 0
    assert started == []
    # A recorded route the roster no longer holds is refused, not moved.
    assert (
        cli.main(["route", "start", "qwen", "--route", "server@elsewhere", "--check"])
        == 2
    )
    assert capsys.readouterr().err == (
        "the qwen account this node was made on is no longer in the roster\n"
    )
    (root / "spawn.toml").write_text("[[provider\n")
    assert cli.main(["route", "start", "claude", "--check"]) == 1
    assert capsys.readouterr().err == "the provider roster could not be read\n"
    assert started == []


@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["--route", "x"],
        ["claude", "--route"],
        ["claude", "--route", "a", "--route", "b"],
        ["claude", "--bogus"],
    ],
)
def test_route_start_refuses_arguments_it_does_not_take(
    root: Path, arguments: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["route", "start", *arguments]) == 2
    assert "usage: route start <provider>" in capsys.readouterr().err
    assert cli.main(["route", "show", "claude", "--check"]) == 2


def test_the_account_alone_names_a_nodes_endpoint_credential_and_model(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Roster env never outranks what the account declares, and stays literal."""
    monkeypatch.setenv("NOTT_TEST_SECRET", "fixture-expanded")
    edit(
        root,
        'ENABLE_TOOL_SEARCH = "false" }',
        'ENABLE_TOOL_SEARCH = "false", ANTHROPIC_AUTH_TOKEN = "fixture-provider",'
        ' CLAUDE_CONFIG_DIR = "/fixture/qwen-home" }',
    )
    edit(
        root,
        'env = { CLAUDE_CODE_AUTO_COMPACT_WINDOW = "245760" }',
        'env = { ANTHROPIC_BASE_URL = "http://elsewhere.invalid",'
        ' ANTHROPIC_MODEL = "$NOTT_TEST_SECRET", LITERAL = "$NOTT_TEST_SECRET" }',
    )
    edit(
        root,
        'model_fast = "claude-haiku-4-5"\n',
        'model_fast = "claude-haiku-4-5"\n'
        'env = { CLAUDE_CODE_USE_BEDROCK = "1" }\n\n'
        '[provider.accounts.alpha]\nmodel = "claude-account-model"\n'
        'auth_env = "CLAUDE_CODE_OAUTH_TOKEN"\n',
    )
    _, environment, _ = native_profile("qwen")
    assert environment["ANTHROPIC_AUTH_TOKEN"] == TOKENS["server"]
    assert environment["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:18020"
    assert environment["ANTHROPIC_MODEL"] == "qwen3.8-27b"
    assert environment["LITERAL"] == "$NOTT_TEST_SECRET"
    home = str(root / "claude-home")
    assert native_profile("claude") == (
        f"alpha@{home}",
        {**CLAUDE_CLEARED, "CLAUDE_CODE_OAUTH_TOKEN": TOKENS["alpha"]},
        [],
    )
    create_store(store_path())
    assert cli.main(["route", "show", "qwen"]) == 0
    assert json.loads(capsys.readouterr().out)["config_dir"] == "/fixture/qwen-home"


def test_route_start_refuses_a_provider_or_a_route_it_cannot_keep(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["route", "start", "codexx", "--check"]) == 2
    assert capsys.readouterr().err == "provider must name claude, codex, qwen or agy\n"
    # A claude node made on an account keeps it: once the roster holds no claude
    # accounts, the home login it would now get is refused, not started.
    edit(root, 'name = "claude"', 'name = "anthropic"')
    home = str(root / "claude-home")
    assert native_profile("claude", f"alpha@{home}")[0] == home
    assert (
        cli.main(["route", "start", "claude", "--route", f"alpha@{home}", "--check"])
        == 2
    )
    assert capsys.readouterr().err == (
        "node's persisted native account route does not match this configured account\n"
    )


def test_route_discovery_checks_the_selected_cwd_without_running_the_adapter(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    commands = root / "commands"
    executable(commands / "claude-agent-acp")
    monkeypatch.setenv("PATH", str(commands))
    monkeypatch.chdir(root)
    monkeypatch.setenv("NOTT_NSPAWN_ROSTER", "spawn.toml")
    started: list[str] = []
    monkeypatch.setattr(os, "execve", lambda *_: started.append(os.getcwd()))
    work = root / "work"
    work.mkdir()
    assert cli.main(["route", "start", "qwen", "--cwd", str(work), "--check"]) == 0
    assert os.getcwd() == str(work) and started == []
    monkeypatch.chdir(root)
    gone = ["route", "start", "qwen", "--cwd", str(root / "gone"), "--check"]
    assert cli.main(gone) == 1
    assert capsys.readouterr().err == (
        "qwen node directory is not reachable: No such file or directory\n"
    )
    assert cli.main(["route", "show", "qwen", "--cwd", str(work)]) == 2
