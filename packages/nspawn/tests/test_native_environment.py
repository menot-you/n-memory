"""Native launch preparation uses selected credentials without running a CLI."""

import json
import os
import tomllib
from pathlib import Path

import nott_database
import pytest
from workspace_root import workspace_path

from nspawn.native import prepare_native
from nspawn.owner_roster import NativeAccount, Provider, Roster

#: The retired database variables, read from the canon rather than spelled here.
RETIRED = tomllib.loads((workspace_path("rails/organization.toml")).read_text())[
    "database"
]["retired_env"]


def credential(home: Path, value: str, *, kind: str = "codex") -> Path:
    path = home / (
        "auth.json"
        if kind == "codex"
        else ".gemini/antigravity-cli/antigravity-oauth-token"
    )
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    path.write_text(
        json.dumps({"tokens" if kind == "codex" else "token": {"access_token": value}})
    )
    path.chmod(0o600)
    return path


@pytest.fixture
def provider(tmp_path):
    binary = tmp_path / "native-cli"
    binary.write_text("#!/bin/sh\nexit 99\n")
    binary.chmod(0o700)
    one, two = tmp_path / "conta-um", tmp_path / "conta-dois"
    credential(one, "FIRST-OAUTH-SECRET-FOR-TESTING")
    credential(two, "SECOND-OAUTH-SECRET-FOR-TESTING")
    return Provider(
        name="codex",
        kind="codex",
        bin=binary,
        model="luna",
        accounts={"one": NativeAccount(one), "two": NativeAccount(two)},
    )


def test_selected_native_account_gets_closed_environment(provider, tmp_path):
    from nspawn.native import prepare_native

    launch = prepare_native(
        provider,
        "two",
        roster=Roster(),
        cwd=tmp_path,
        read_only=True,
        ambient={
            "HOME": "/owner",
            "PATH": "/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "OTHER_API_KEY": "FOREIGN-SECRET",
            "PYTHONPATH": "/injected",
            "CODEX_THREAD_ID": "foreign-thread",
            "CODEX_HOME": "/wrong",
            "NOTT_NSPAWN_PLUGIN_ROOT": "/foreign-plugin",
            "CLAUDE_CONFIG_DIR": "/foreign",
            "ANTHROPIC_AUTH_TOKEN": "FOREIGN-TOKEN",
            "NOTT_CUSTOM_API_KEY": "FOREIGN-PROXY",
        },
    )
    assert launch.env == {
        "HOME": "/owner",
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "CODEX_HOME": str(provider.accounts["two"].home),
        "NOTT_DATABASE_DIR": str(nott_database.live_directory()),
    }
    assert launch.context.home == str(provider.accounts["two"].home)
    assert "SECOND-OAUTH-SECRET-FOR-TESTING" not in launch.needles.redact(
        "SECOND-OAUTH-SECRET-FOR-TESTING"
    )
    assert launch.argv[0] == str(provider.bin.resolve())
    assert launch.argv[-3:] == ("app-server", "--listen", "stdio://")
    assert 'cli_auth_credentials_store="file"' in launch.argv
    assert "features.memories=false" in launch.argv
    # Untrusted plugin hooks are skipped silently by Codex; the guards only bite with this.
    assert "--dangerously-bypass-hook-trust" in launch.argv
    assert launch.argv.index("--dangerously-bypass-hook-trust") < launch.argv.index(
        "app-server"
    )


def prepare(provider, tmp_path, **kwargs):
    from nspawn.native import prepare_native

    return prepare_native(
        provider,
        kwargs.pop("account_name", "one"),
        roster=kwargs.pop("roster", Roster()),
        cwd=tmp_path,
        read_only=kwargs.pop("read_only", True),
        ambient=kwargs.pop("ambient", {"HOME": "/owner", "PATH": "/usr/bin:/bin"}),
        **kwargs,
    )


def test_declared_plugin_mcp_preserves_scoped_environment_without_argv_secrets(
    provider, tmp_path
):
    from nspawn.owner_roster import Defaults

    plugin = tmp_path / "plugin"
    plugin.mkdir()
    (plugin / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "nmemory": {
                        "command": "/fixture/nmemory",
                        "args": ["--project", "fixture"],
                    },
                    "nspawn": {"command": "/fixture/nspawn"},
                    "jbcontext": {
                        "command": "jbcontext",
                        "args": ["mcp"],
                        "env": {
                            "DBUS_SESSION_BUS_ADDRESS": "${DBUS_SESSION_BUS_ADDRESS}"
                        },
                    },
                }
            }
        )
    )
    ambient = {
        "HOME": "/owner",
        "PATH": "/usr/bin",
        "NOTT_NSPAWN_CONFIG": "/fixture/spawn.toml",
        "NOTT_DATABASE_DIR": "/fixture/database",
        "NOTT_NMEMORY_CONFIG": "/fixture/memory.toml",
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/fixture/bus",
        "OTHER_API_KEY": "must-not-propagate",
        "NOTT_NSPAWN_PLUGIN_ROOT": "/foreign-plugin",
    }
    launch = prepare(
        provider,
        tmp_path,
        read_only=False,
        ambient=ambient,
        roster=Roster(defaults=Defaults(plugin_root=plugin)),
    )
    assert launch.env["NOTT_NSPAWN_PLUGIN_ROOT"] == str(plugin.resolve())
    servers = launch.mcp_servers
    assert servers["nspawn"]["command"] == "/fixture/nspawn"
    assert "NOTT_NSPAWN_CONFIG" in servers["nspawn"]["env_vars"]
    assert "NOTT_DATABASE_DIR" in servers["nspawn"]["env_vars"]
    assert "NOTT_NMEMORY_CONFIG" in servers["nmemory"]["env_vars"]
    assert "NOTT_NSPAWN_RUN_ID" in servers["nmemory"]["env_vars"]
    # nMEMORY's store is a file in the store directory now (P2), never a URL.
    assert "NOTT_DATABASE_DIR" in servers["nmemory"]["env_vars"]
    assert launch.env["NOTT_DATABASE_DIR"] == "/fixture/database"
    assert servers["nmemory"]["args"] == ["--project", "fixture"]
    assert servers["jbcontext"]["env_vars"] == ["DBUS_SESSION_BUS_ADDRESS"]
    assert launch.env["NOTT_NMEMORY_CONFIG"] == ambient["NOTT_NMEMORY_CONFIG"]
    assert launch.env["DBUS_SESSION_BUS_ADDRESS"] == ambient["DBUS_SESSION_BUS_ADDRESS"]
    assert "OTHER_API_KEY" not in launch.env


def test_oauth_rejects_malformed_data_and_pins_openai(provider, tmp_path):
    from nspawn.native import NativePreparationError

    path = provider.accounts["one"].home / "auth.json"
    path.write_text("not valid credential JSON")
    with pytest.raises(NativePreparationError):
        prepare(provider, tmp_path)
    credential(provider.accounts["one"].home, "REFRESHED-OAUTH-SECRET-FOR-TEST")
    launch = prepare(provider, tmp_path)
    assert launch.context.model_provider == "openai"


@pytest.mark.parametrize("present", [True, False])
def test_optional_plugin_credential_reaches_only_its_declared_mcp(
    provider, tmp_path, present
):
    from nspawn.native import NativeRedactor
    from nspawn.owner_roster import Defaults

    plugin = tmp_path / "plugin"
    plugin.mkdir()
    (plugin / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "nmemory": {
                        "command": "/fixture/nmemory",
                        "env_vars": ["OPENAI_API_KEY"],
                    },
                    "nspawn": {"command": "/fixture/nspawn"},
                }
            }
        )
    )
    marker = "SYNTHETIC-MCP-EMBEDDING-KEY"
    launch = prepare(
        provider,
        tmp_path,
        ambient={"OPENAI_API_KEY": marker} if present else {},
        roster=Roster(defaults=Defaults(plugin_root=plugin)),
    )
    assert launch.mcp_servers["nmemory"].get("env", {}) == (
        {"OPENAI_API_KEY": marker} if present else {}
    )
    assert "OPENAI_API_KEY" not in launch.env
    assert "OPENAI_API_KEY" not in launch.mcp_servers["nmemory"]["env_vars"]
    assert "OPENAI_API_KEY" not in json.dumps(launch.mcp_servers["nspawn"])
    assert marker not in " ".join(launch.argv)
    assert marker not in repr(launch)
    if present:
        assert (
            marker not in NativeRedactor(launch).redact_value({"echo": marker})["echo"]
        )


def test_fugu_credentials_reach_only_the_declared_nspawn_mcp(provider, tmp_path):
    from nspawn.owner_roster import Defaults

    plugin = tmp_path / "plugin"
    plugin.mkdir()
    variables = ["SAKANA_MAX_API_KEY", "SAKANA_PRO_API_KEY"]
    (plugin / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "nmemory": {"command": "/fixture/nmemory"},
                    "nspawn": {
                        "command": "/fixture/nspawn",
                        "env_vars": variables,
                    },
                }
            }
        )
    )
    ambient = {name: f"SYNTHETIC-{name}" for name in variables}

    launch = prepare(
        provider,
        tmp_path,
        ambient=ambient,
        roster=Roster(defaults=Defaults(plugin_root=plugin)),
    )

    assert launch.mcp_servers["nspawn"]["env"] == ambient
    assert not variables & launch.env.keys()
    assert "env" not in launch.mcp_servers["nmemory"]


@pytest.mark.parametrize("present", [True, False])
def test_system1_credential_reaches_only_nspawn_native_rpc(provider, tmp_path, present):
    from nspawn.native import NativeRedactor
    from nspawn.owner_roster import Defaults

    plugin = tmp_path / "plugin"
    plugin.mkdir()
    declaration = plugin / ".mcp.json"
    declaration.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "nspawn": {
                        "command": "/fixture/nspawn",
                        "env_vars": ["NOTT_SYSTEM1_API_KEY"],
                    },
                    "nmemory": {"command": "/fixture/nmemory"},
                    "nbrowser": {"command": "/fixture/nbrowser"},
                }
            }
        )
    )
    original = declaration.read_bytes()
    marker = "SYNTHETIC-SYSTEM1-MCP-CREDENTIAL"
    ambient = {"NOTT_SYSTEM1_API_KEY": marker} if present else {}

    launch = prepare(
        provider,
        tmp_path,
        ambient=ambient,
        roster=Roster(defaults=Defaults(plugin_root=plugin)),
    )

    assert launch.mcp_servers["nspawn"].get("env", {}) == ambient
    assert "NOTT_SYSTEM1_API_KEY" not in launch.mcp_servers["nspawn"]["env_vars"]
    assert "NOTT_SYSTEM1_API_KEY" not in launch.env
    assert "env" not in launch.mcp_servers["nmemory"]
    assert "env" not in launch.mcp_servers["nbrowser"]
    assert marker not in " ".join(launch.argv)
    assert marker not in repr(launch)
    assert declaration.read_bytes() == original
    assert marker not in declaration.read_text()
    if present:
        assert NativeRedactor(launch).redact_value({"echo": marker}) == {
            "echo": "[redacted-credential]"
        }


@pytest.mark.parametrize("server", ["nbrowser", "jbcontext", "unknown"])
def test_system1_credential_is_refused_for_other_mcp_servers(
    provider, tmp_path, server
):
    from nspawn.native import NativePreparationError
    from nspawn.owner_roster import Defaults

    plugin = tmp_path / "plugin"
    plugin.mkdir()
    (plugin / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "nmemory": {"command": "/fixture/nmemory"},
                    server: {
                        "command": "/fixture/server",
                        "env_vars": ["NOTT_SYSTEM1_API_KEY"],
                    },
                }
            }
        )
    )
    marker = "SYNTHETIC-SYSTEM1-MCP-CREDENTIAL"
    with pytest.raises(NativePreparationError, match="env_vars") as error:
        prepare(
            provider,
            tmp_path,
            ambient={"NOTT_SYSTEM1_API_KEY": marker},
            roster=Roster(defaults=Defaults(plugin_root=plugin)),
        )
    assert marker not in str(error.value)


@pytest.mark.parametrize("variable", ["HOME", "XDG_RUNTIME_DIR"])
def test_a_home_selector_is_refused_from_the_forwarding_class(
    provider, tmp_path, variable
):
    """The forwarding class subtracts `_HOME_SELECTORS`, and that cut needs a guard.

    Those names SELECT where a native account's state lives, and this launcher
    owns that choice -- the same exclusion the provider `env` block applies.
    Measured with every selector in the ambient: `CODEX_HOME` reaches the child
    rewritten to the account home and `CODEX_SQLITE_HOME` is dropped, while
    `HOME` and the `XDG_*` names pass through, so the cut is a boundary over the
    SELECTION rather than a claim that each value is rewritten.

    The refusal must SAY which class was missed: the two classes are spelled
    identically in the file, so an operator who declared the wrong one has no
    other way to tell them apart. `XDG_RUNTIME_DIR` is the price of the cut and
    is pinned here deliberately -- it is an alternative route to the same bus,
    and `DBUS_SESSION_BUS_ADDRESS` is the one that passes.
    """
    from nspawn.native import NativePreparationError
    from nspawn.owner_roster import Defaults

    plugin = tmp_path / "plugin"
    plugin.mkdir()
    (plugin / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "nmemory": {"command": "/fixture/nmemory", "env_vars": [variable]}
                }
            }
        )
    )
    with pytest.raises(NativePreparationError) as error:
        prepare(
            provider,
            tmp_path,
            ambient={"HOME": "/owner", "PATH": "/usr/bin", variable: "/fixture/state"},
            roster=Roster(defaults=Defaults(plugin_root=plugin)),
        )
    message = str(error.value)
    assert "env_vars" in message
    assert "credential" in message and "route" in message, (
        "a name in neither class must be told WHICH class it missed, because "
        f"the two spellings are identical in the file. Got: {message}"
    )


def test_a_forwarded_route_never_enters_the_credential_channel(provider, tmp_path):
    """`env_vars` names two classes, and only ONE of them reads a value.

    A credential is LOOKED UP in the parent environment, retained as a needle
    and copied into the child's own `env`. A route is only a NAME: nSPAWN never
    reads it, so it cannot leave through a receipt -- and it must NOT become a
    needle, because redacting a socket path erases the very coordinate an
    operator reads to diagnose the child that could not reach the session bus.

    The pair is asserted together deliberately: either half alone would stay
    green while the classes collapsed back into one.
    """
    from nspawn.owner_roster import Defaults

    plugin = tmp_path / "plugin"
    plugin.mkdir()
    (plugin / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "nmemory": {
                        "command": "/fixture/nmemory",
                        "env_vars": ["OPENAI_API_KEY", "DBUS_SESSION_BUS_ADDRESS"],
                    }
                }
            }
        )
    )
    secret = "SYNTHETIC-MCP-EMBEDDING-KEY"
    route = "unix:path=/fixture/bus"
    launch = prepare(
        provider,
        tmp_path,
        ambient={
            "HOME": "/owner",
            "PATH": "/usr/bin",
            "OPENAI_API_KEY": secret,
            "DBUS_SESSION_BUS_ADDRESS": route,
        },
        roster=Roster(defaults=Defaults(plugin_root=plugin)),
    )
    server = launch.mcp_servers["nmemory"]

    assert server["env"] == {"OPENAI_API_KEY": secret}
    assert "OPENAI_API_KEY" not in server["env_vars"]
    assert "DBUS_SESSION_BUS_ADDRESS" in server["env_vars"]
    assert launch.needles.redact(secret) != secret
    assert launch.needles.redact(route) == route


@pytest.mark.parametrize(
    "variables",
    [
        None,
        "OPENAI_API_KEY",
        [None],
        [[]],
        [""],
        ["OTHER_API_KEY"],
        ["PYTHONPATH"],
        ["HOME"],
        ["OPENAI_API_KEY", "OPENAI_API_KEY"],
        ["DBUS_SESSION_BUS_ADDRESS", "DBUS_SESSION_BUS_ADDRESS"],
    ],
)
def test_optional_plugin_credential_declarations_are_closed(
    provider, tmp_path, variables
):
    from nspawn.native import NativePreparationError
    from nspawn.owner_roster import Defaults

    plugin = tmp_path / "plugin"
    plugin.mkdir()
    (plugin / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "nmemory": {
                        "command": "/fixture/nmemory",
                        "env_vars": variables,
                    }
                }
            }
        )
    )
    with pytest.raises(NativePreparationError, match="env_vars"):
        prepare(
            provider, tmp_path, roster=Roster(defaults=Defaults(plugin_root=plugin))
        )


@pytest.mark.parametrize(
    "server_name,credential_name",
    [("nmemory", "OPENAI_API_KEY"), ("nspawn", "NOTT_SYSTEM1_API_KEY")],
)
@pytest.mark.parametrize(
    "value", [None, 1, "", " ", "bad\x00key", "bad\nkey", "\ud800", "x" * 65537]
)
def test_optional_plugin_credential_values_are_validated(
    provider, tmp_path, server_name, credential_name, value
):
    from nspawn.native import NativePreparationError
    from nspawn.owner_roster import Defaults

    plugin = tmp_path / "plugin"
    plugin.mkdir()
    servers = {"nmemory": {"command": "/fixture/nmemory"}}
    servers[server_name] = {
        "command": f"/fixture/{server_name}",
        "env_vars": [credential_name],
    }
    (plugin / ".mcp.json").write_text(json.dumps({"mcpServers": servers}))
    with pytest.raises(NativePreparationError):
        prepare(
            provider,
            tmp_path,
            ambient={credential_name: value},
            roster=Roster(defaults=Defaults(plugin_root=plugin)),
        )


@pytest.mark.parametrize(
    "damage", ["sibling", "unknown-key", "undeclared", "argv", "invalid-env-key"]
)
def test_optional_plugin_credential_cannot_cross_other_boundaries(
    provider, tmp_path, damage
):
    from nspawn.native import NativePreparationError
    from nspawn.owner_roster import Defaults

    plugin = tmp_path / "plugin"
    plugin.mkdir()
    marker = "SYNTHETIC-MCP-EMBEDDING-KEY"
    servers = {
        "nspawn": {"command": "/fixture/nspawn"},
        "nmemory": {"command": "/fixture/nmemory", "env_vars": ["OPENAI_API_KEY"]},
    }
    match damage:
        case "sibling":
            servers["nspawn"]["env_vars"] = ["OPENAI_API_KEY"]
        case "unknown-key":
            servers["nmemory"]["unknown"] = []
        case "undeclared":
            del servers["nmemory"]["env_vars"]
        case "argv":
            servers["nspawn"]["args"] = [marker]
        case "invalid-env-key":
            servers["nmemory"]["env"] = {marker: "literal"}
    (plugin / ".mcp.json").write_text(json.dumps({"mcpServers": servers}))
    kwargs = {
        "ambient": {"OPENAI_API_KEY": marker},
        "roster": Roster(defaults=Defaults(plugin_root=plugin)),
    }
    if damage == "undeclared":
        launch = prepare(provider, tmp_path, **kwargs)
        assert "OPENAI_API_KEY" not in launch.env
        assert marker not in json.dumps(launch.mcp_servers)
    else:
        with pytest.raises(NativePreparationError) as error:
            prepare(provider, tmp_path, **kwargs)
        assert marker not in str(error.value)


@pytest.mark.parametrize(
    "damage",
    [
        "literal-secret",
        "alias",
        "foreign-env",
        "argv-secret",
        "remote",
        "bad-args",
        "surrogate",
    ],
)
def test_native_mcp_projection_refuses_unrepresentable_or_unsafe_config(
    provider, tmp_path, damage
):
    from nspawn.native import NativePreparationError
    from nspawn.owner_roster import Defaults

    plugin = tmp_path / "plugin"
    plugin.mkdir()
    server = {"command": "/fixture/nmemory"}
    match damage:
        case "literal-secret":
            server["env"] = {"TOKEN": "literal-private-value"}
        case "alias":
            server["env"] = {"DBUS_SESSION_BUS_ADDRESS": "${HOME}"}
        case "foreign-env":
            server["env"] = {"OTHER_API_KEY": "${OTHER_API_KEY}"}
        case "argv-secret":
            server["args"] = ["FIRST-OAUTH-SECRET-FOR-TESTING"]
        case "remote":
            server.update(type="http", url="https://example.invalid/mcp")
        case "bad-args":
            server["args"] = [1]
        case "surrogate":
            server["command"] = "\ud800"
    (plugin / ".mcp.json").write_text(json.dumps({"mcpServers": {"nmemory": server}}))
    with pytest.raises(NativePreparationError) as caught:
        prepare(
            provider, tmp_path, roster=Roster(defaults=Defaults(plugin_root=plugin))
        )
    assert "FIRST-OAUTH-SECRET-FOR-TESTING" not in str(caught.value)
    assert "literal-private-value" not in str(caught.value)


@pytest.mark.parametrize("read_only", [True, False])
def test_native_mcp_projection_skips_the_http_nmemory_entry(
    provider, tmp_path, read_only
):
    """The plugin ships nmemory over stdio AND http; Codex children use stdio."""
    from nspawn.owner_roster import Defaults

    plugin = tmp_path / "plugin"
    plugin.mkdir()
    (plugin / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "nmemory": {"command": "/fixture/nmemory"},
                    "nmemory-http": {
                        "type": "http",
                        "url": "${NOTT_NMEMORY_URL:-https://host.example/mcp}",
                        "headers": {"Authorization": "Bearer ${NOTT_NMEMORY_API_KEY}"},
                    },
                }
            }
        )
    )
    launch = prepare(
        provider,
        tmp_path,
        read_only=read_only,
        roster=Roster(defaults=Defaults(plugin_root=plugin)),
    )
    assert launch.mcp_servers["nmemory"]["command"] == "/fixture/nmemory"
    assert "nmemory-http" not in launch.mcp_servers


@pytest.mark.parametrize(
    "name", ["NOTT_NMEMORY_CONFIG", "NOTT_NSPAWN_CONFIG", "NOTT_NSPAWN_ROSTER"]
)
def test_provider_environment_cannot_retarget_mcp_scope(provider, tmp_path, name):
    from nspawn.native import NativePreparationError

    provider.env[name] = "/different/config.toml"
    with pytest.raises(NativePreparationError, match="forbidden override"):
        prepare(provider, tmp_path)


@pytest.mark.parametrize("damage", ["writable", "hardlink", "oversize"])
def test_policy_config_reader_keeps_identity_and_write_fences(tmp_path, damage):
    from nspawn.vendor_home import (
        AUTH_MAX_BYTES,
        VendorHomeError,
        read_owner_config,
        read_owner_credential,
    )

    path = tmp_path / "config.toml"
    path.write_text("")
    path.chmod(0o644)
    assert read_owner_config(path) == b""
    with pytest.raises(VendorHomeError):
        read_owner_credential(path)
    path.write_text("# owner configuration\n")
    match damage:
        case "writable":
            path.chmod(0o664)
        case "hardlink":
            path.with_suffix(".link").hardlink_to(path)
        case "oversize":
            path.write_bytes(b" " * (AUTH_MAX_BYTES + 1))
    with pytest.raises(VendorHomeError):
        read_owner_config(path)


def test_configuration_can_share_a_stable_owner_alias_but_credentials_cannot(tmp_path):
    from nspawn.vendor_home import (
        VendorHomeError,
        read_owner_config,
        read_owner_credential,
    )

    target = tmp_path / "shared-config.toml"
    target.write_text("# shared owner policy\n")
    target.chmod(0o600)
    alias = tmp_path / "config.toml"
    alias.symlink_to(target)
    assert read_owner_config(alias) == target.read_bytes()
    with pytest.raises(VendorHomeError):
        read_owner_credential(alias)
    target.chmod(0o664)
    with pytest.raises(VendorHomeError):
        read_owner_config(alias)


def test_configuration_alias_must_not_move_during_read(tmp_path, monkeypatch):
    from nspawn import vendor_home

    original = tmp_path / "original.toml"
    replacement = tmp_path / "replacement.toml"
    for path in (original, replacement):
        path.write_text("# private settings\n")
        path.chmod(0o600)
    alias = tmp_path / "config.toml"
    alias.symlink_to(original)
    read = vendor_home._read_owner_file

    def moving(path, *, configuration):
        data = read(path, configuration=configuration)
        alias.unlink()
        alias.symlink_to(replacement)
        return data

    monkeypatch.setattr(vendor_home, "_read_owner_file", moving)
    with pytest.raises(vendor_home.VendorHomeError, match="changed"):
        vendor_home.read_owner_config(alias)


def test_native_mcp_projection_preserves_existing_plugin_permissions(
    provider, tmp_path
):
    from nspawn.owner_roster import Defaults

    plugin = tmp_path / "plugin"
    (plugin / ".codex-plugin").mkdir(parents=True)
    (plugin / ".codex-plugin/plugin.json").write_text('{"name":"nott"}')
    (plugin / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "nmemory": {"command": "/fixture/nmemory"},
                    "nspawn": {"command": "/fixture/nspawn"},
                }
            }
        )
    )
    config = provider.accounts["one"].home / "config.toml"
    config.write_text("""
[plugins."nott@personal"]
enabled=true
[plugins."nott@personal".mcp_servers.nmemory]
default_tools_approval_mode="approve"
enabled_tools=["nmemory_context_show"]
[plugins."nott@personal".mcp_servers.nspawn]
default_tools_approval_mode="prompt"
[plugins."nott@personal".mcp_servers.nspawn.tools.nspawn_run_close]
approval_mode="prompt"
""")
    config.chmod(0o600)
    servers = prepare(
        provider, tmp_path, roster=Roster(defaults=Defaults(plugin_root=plugin))
    ).mcp_servers
    assert servers["nmemory"]["default_tools_approval_mode"] == "approve"
    assert servers["nmemory"]["enabled_tools"] == ["nmemory_context_show"]
    assert servers["nspawn"]["default_tools_approval_mode"] == "prompt"
    assert servers["nspawn"]["tools"]["nspawn_run_close"]["approval_mode"] == "prompt"
    config.write_text('[plugins."nott@personal"]\nenabled=false\n')
    disabled = prepare(
        provider, tmp_path, roster=Roster(defaults=Defaults(plugin_root=plugin))
    ).mcp_servers
    assert all(server["enabled"] is False for server in disabled.values())
    config.write_text("")
    config.chmod(0o644)
    missing = prepare(
        provider, tmp_path, roster=Roster(defaults=Defaults(plugin_root=plugin))
    ).mcp_servers
    assert all(
        "default_tools_approval_mode" not in server for server in missing.values()
    )
    from nspawn.native import NativePreparationError

    config.write_text(
        '[plugins."nott@one"]\nenabled=true\n[plugins."nott@two"]\nenabled=true\n'
    )
    with pytest.raises(NativePreparationError, match="unambiguously"):
        prepare(
            provider, tmp_path, roster=Roster(defaults=Defaults(plugin_root=plugin))
        )
    (plugin / ".codex-plugin/plugin.json").unlink()
    config.write_text('[mcp_servers.nspawn]\ncommand="/owner/other-server"\n')
    with pytest.raises(NativePreparationError, match="account-level"):
        prepare(
            provider, tmp_path, roster=Roster(defaults=Defaults(plugin_root=plugin))
        )


@pytest.fixture
def api_provider(provider):
    from dataclasses import replace

    home = provider.accounts["one"].home
    config = home / "config.toml"
    config.write_text(
        '[model_providers.sakanamax]\nwire_api="responses"\nenv_key="SAKANA_MAX_API_KEY"\nbase_url="https://example.invalid/v1"\n'
    )
    config.chmod(0o600)
    provider.name = "fugu"
    provider.secrets = home.parent / "api-tokens"
    provider.secrets.mkdir(mode=0o700)
    path = provider.secrets / "max.token"
    path.write_text("EXACT-SELECTED-API-SECRET\n")
    path.chmod(0o600)
    provider.accounts = {
        "max": replace(
            provider.accounts["one"],
            auth_env="SAKANA_MAX_API_KEY",
            model_provider="sakanamax",
        )
    }
    return provider


@pytest.mark.parametrize("alias", ["max", "pro"])
@pytest.mark.parametrize("ambient_key", [None, "STALE-AMBIENT-SECRET"])
def test_api_accounts_use_claude_token_resolution(
    api_provider, tmp_path, monkeypatch, alias, ambient_key
):
    """The native caller reads the selected account file, with no parent key."""
    from dataclasses import replace

    directory = tmp_path / "account-tokens"
    directory.mkdir(mode=0o700)
    for name in ("max", "pro"):
        path = directory / f"{name}.token"
        path.write_text(f"{name}-SELECTED-FILE-SECRET\n")
        path.chmod(0o600)
    api_provider.secrets = directory
    api_provider.accounts["pro"] = replace(
        api_provider.accounts["max"],
        auth_env="SAKANA_PRO_API_KEY",
        model_provider="sakanapro",
    )
    config = api_provider.accounts["max"].home / "config.toml"
    config.write_text(
        config.read_text() + '[model_providers.sakanapro]\nwire_api="responses"\n'
        'env_key="SAKANA_PRO_API_KEY"\nbase_url="https://example.invalid/v1"\n'
    )
    called = []
    resolve = Provider.resolve_token_file

    def witnessed(self, selected):
        called.append(selected)
        return resolve(self, selected)

    monkeypatch.setattr(Provider, "resolve_token_file", witnessed)
    env = {} if ambient_key is None else {"SAKANA_MAX_API_KEY": ambient_key}
    launch = prepare(api_provider, tmp_path, account_name=alias, ambient=env)
    key = api_provider.accounts[alias].auth_env
    secret = f"{alias}-SELECTED-FILE-SECRET"
    assert called == [alias]
    assert launch.env[key] == secret
    assert set(launch.env).intersection(
        {"SAKANA_MAX_API_KEY", "SAKANA_PRO_API_KEY"}
    ) == {key}
    assert secret not in repr(launch)
    assert all(secret not in arg for arg in launch.argv)
    assert launch.needles.redact(secret) == "[redacted-credential]"
    assert launch.context.model_provider == f"sakana{alias}"


def test_native_accounts_accept_shared_secrets_directory(tmp_path):
    path = tmp_path / "spawn.toml"
    path.write_text(
        '[bins]\ncodex="/bin/codex"\n'
        '[[provider]]\nname="fugu"\nkind="codex"\n'
        'model="fugu"\nsecrets="/owner/tokens"\n'
        '[provider.accounts.max]\nhome="/owner/codex"\n'
        'auth_env="SAKANA_MAX_API_KEY"\nmodel_provider="sakanamax"\n'
    )
    parsed = Roster.read(path).providers[0]
    assert parsed.secrets == Path("/owner/tokens")
    assert parsed.discovered_accounts() == ["max"]


@pytest.mark.parametrize("selector", [None, "fugu"])
def test_native_default_token_matches_resolved_account(
    api_provider, tmp_path, selector
):
    trap = api_provider.secrets / "fugu.token"
    trap.write_text("WRONG-PROVIDER-NAMED-SECRET")
    trap.chmod(0o600)
    launch = prepare(api_provider, tmp_path, account_name=selector, ambient={})
    assert launch.env["SAKANA_MAX_API_KEY"] == "EXACT-SELECTED-API-SECRET"


@pytest.mark.parametrize("alias", ["../escaped", "nested/escaped"])
def test_native_token_alias_cannot_escape_account_directory(
    api_provider, tmp_path, alias
):
    from nspawn.native import NativePreparationError

    api_provider.accounts = {alias: api_provider.accounts["max"]}
    outside = api_provider.secrets / f"{alias}.token"
    outside.parent.mkdir(exist_ok=True)
    outside.write_text("OUTSIDE-ACCOUNT-SECRET")
    outside.chmod(0o600)
    with pytest.raises(NativePreparationError):
        prepare(api_provider, tmp_path, account_name=alias, ambient={})


def test_api_account_uses_only_selected_key_and_bound_provider(api_provider, tmp_path):
    from dataclasses import FrozenInstanceError

    launch = prepare(
        api_provider,
        tmp_path,
        account_name="max",
        ambient={
            "HOME": "/owner",
            "SAKANA_MAX_API_KEY": "EXACT-SELECTED-API-SECRET",
            "SAKANA_PRO_API_KEY": "FOREIGN-PRO-KEY",
        },
    )
    assert launch.env["SAKANA_MAX_API_KEY"] == "EXACT-SELECTED-API-SECRET"
    assert "SAKANA_PRO_API_KEY" not in launch.env
    assert launch.context.auth_env == "SAKANA_MAX_API_KEY"
    assert launch.context.model_provider == "sakanamax"
    assert all("EXACT-SELECTED-API-SECRET" not in arg for arg in launch.argv)
    assert "EXACT-SELECTED-API-SECRET" not in repr(launch)
    assert launch.needles.redact("EXACT-SELECTED-API-SECRET") == "[redacted-credential]"
    assert (
        launch.refreshed_needles().redact("EXACT-SELECTED-API-SECRET")
        == "[redacted-credential]"
    )
    with pytest.raises(FrozenInstanceError):
        launch.argv = ()


def test_selected_token_file_is_authoritative(api_provider, tmp_path):
    launch = prepare(
        api_provider,
        tmp_path,
        account_name="max",
        ambient={"SAKANA_MAX_API_KEY": "STALE-AMBIENT-SECRET"},
    )
    assert launch.env["SAKANA_MAX_API_KEY"] == "EXACT-SELECTED-API-SECRET"
    assert "SAKANA_PRO_API_KEY" not in launch.env
    assert "STALE-AMBIENT-SECRET" not in repr(launch)


@pytest.mark.parametrize("selected", ["claude", "aaa.token"])
def test_missing_selected_account_cannot_borrow_legacy_fallback(
    api_provider, tmp_path, selected
):
    from dataclasses import replace

    from nspawn.native import NativePreparationError

    sibling = api_provider.accounts["max"]
    api_provider.accounts = {
        "aaa": sibling,
        selected: replace(
            sibling, auth_env="SAKANA_PRO_API_KEY", model_provider="sakanapro"
        ),
    }
    (api_provider.secrets / "max.token").rename(api_provider.secrets / "aaa.token")
    config = sibling.home / "config.toml"
    config.write_text(
        config.read_text() + '[model_providers.sakanapro]\nwire_api="responses"\n'
        'env_key="SAKANA_PRO_API_KEY"\nbase_url="https://example.invalid/v1"\n'
    )
    Roster(providers=[api_provider])._validate(tmp_path / "roster.toml")
    assert not (api_provider.secrets / f"{selected}.token").exists()
    with pytest.raises(NativePreparationError):
        prepare(api_provider, tmp_path, account_name=selected, ambient={})


@pytest.mark.parametrize(
    "damage",
    [
        "missing",
        "empty",
        "multiline",
        "nul",
        "non-utf8",
        "symlink",
        "hardlink",
        "readable",
        "oversize",
        "directory",
        "missing-source",
    ],
)
def test_missing_or_unsafe_account_file_never_falls_back(
    api_provider, tmp_path, damage
):
    from nspawn.native import NativePreparationError
    from nspawn.vendor_home import AUTH_MAX_BYTES

    path = api_provider.secrets / "max.token"
    if damage == "missing":
        path.unlink()
    elif damage == "empty":
        path.write_text(" \n")
    elif damage == "multiline":
        path.write_text("FIRST-SECRET\nSECOND-SECRET")
    elif damage == "nul":
        path.write_bytes(b"FIRST\x00SECRET")
    elif damage == "non-utf8":
        path.write_bytes(b"\xff")
    elif damage == "symlink":
        target = path.with_suffix(".target")
        path.rename(target)
        path.symlink_to(target)
    elif damage == "hardlink":
        path.with_suffix(".link").hardlink_to(path)
    elif damage == "readable":
        path.chmod(0o644)
    elif damage == "oversize":
        path.write_bytes(b"s" * (AUTH_MAX_BYTES + 1))
    elif damage == "directory":
        path.unlink()
        path.mkdir()
    else:
        api_provider.secrets = None
    with pytest.raises(NativePreparationError) as error:
        prepare(
            api_provider,
            tmp_path,
            account_name="max",
            ambient={"SAKANA_MAX_API_KEY": "STALE-SECRET-MUST-NOT-WIN"},
        )
    assert "STALE-SECRET" not in str(error.value)
    assert "FIRST-SECRET" not in str(error.value)


@pytest.mark.parametrize(
    "key",
    [
        "HOME",
        "CODEX_HOME",
        "CODEX_SQLITE_HOME",
        "PYTHONPATH",
        "NODE_OPTIONS",
        "ANTHROPIC_API_KEY",
        "CLAUDE_CONFIG_DIR",
        "NOTT_CUSTOM_API_KEY",
        "UNRELATED_TOKEN",
        "XDG_CONFIG_HOME",
        "NOTT_DATABASE_DIR",
    ],
)
def test_provider_env_cannot_smuggle_credentials_or_redirect_home(
    provider, tmp_path, key
):
    from nspawn.native import NativePreparationError

    provider.env[key] = "DO-NOT-ECHO-THIS-SENTINEL"
    with pytest.raises(NativePreparationError) as error:
        prepare(provider, tmp_path)
    assert "DO-NOT-ECHO-THIS-SENTINEL" not in str(error.value)


def test_operational_environment_and_named_mcps_survive(provider, tmp_path):
    provider.env = {"LANG": "pt_PT.UTF-8", "UV_CACHE_DIR": "/cache"}
    launch = prepare(
        provider,
        tmp_path,
        ambient={
            "HOME": "/owner",
            "LANG": "C",
            "NOTT_NSPAWN_CONFIG": "/opt/test/spawn.toml",
            "NOTT_NSPAWN_ROSTER": "/opt/test/spawn.toml",
            **{name: "example/retired_pytest" for name in RETIRED},
            "SSL_CERT_FILE": "/cert.pem",
            "XDG_CACHE_HOME": "/cache-parent",
            "CUSTOM_API_KEY": "SECRET-ABSENT",
        },
    )
    assert launch.env["LANG"] == "pt_PT.UTF-8"
    assert launch.env["NOTT_NSPAWN_CONFIG"] == "/opt/test/spawn.toml"
    assert launch.env["SSL_CERT_FILE"] == "/cert.pem"
    assert launch.env["XDG_CACHE_HOME"] == "/cache-parent"
    assert "CUSTOM_API_KEY" not in launch.env
    # No retired database variable reaches a child.
    assert RETIRED
    assert not set(RETIRED) & set(launch.env)


def test_the_store_directory_is_the_dispatching_accounts_whatever_the_child_home(
    provider, tmp_path
):
    """The child's HOME is rewritten to the account home, so the parent names its
    own store directory explicitly: the one it was given, else its live one."""

    launch = prepare(provider, tmp_path, ambient={"HOME": "/owner", "PATH": "/bin"})
    assert launch.env["NOTT_DATABASE_DIR"] == str(nott_database.live_directory())
    launch = prepare(
        provider,
        tmp_path,
        ambient={"HOME": "/owner", "NOTT_DATABASE_DIR": "/fixture/database"},
    )
    assert launch.env["NOTT_DATABASE_DIR"] == "/fixture/database"


def test_a_relative_store_directory_is_refused(provider, tmp_path):
    from nspawn.native import NativePreparationError

    with pytest.raises(NativePreparationError):
        prepare(provider, tmp_path, ambient={"NOTT_DATABASE_DIR": "relative/dir"})


@pytest.mark.parametrize("kind", ["codex", "agy"])
def test_oauth_refresh_redacts_both_generations_without_copy_or_chmod(
    provider, tmp_path, kind
):
    provider.kind = kind
    provider.model = "gemini-test" if kind == "agy" else "luna"
    path = credential(
        provider.accounts["one"].home, "OLD-OAUTH-SECRET-EXACT", kind=kind
    )
    home = provider.accounts["one"].home
    before_names = sorted(str(p.relative_to(home)) for p in home.rglob("*"))
    before_modes = {p: p.stat().st_mode for p in home.rglob("*")}
    launch = prepare(provider, tmp_path)
    credential(home, "NEW-OAUTH-SECRET-EXACT", kind=kind)
    refreshed = launch.refreshed_needles()
    assert (
        refreshed.redact("OLD-OAUTH-SECRET-EXACT NEW-OAUTH-SECRET-EXACT")
        == "[redacted-credential] [redacted-credential]"
    )
    assert launch.needles.redact("NEW-OAUTH-SECRET-EXACT") == "NEW-OAUTH-SECRET-EXACT"
    assert sorted(str(p.relative_to(home)) for p in home.rglob("*")) == before_names
    assert {p: p.stat().st_mode for p in home.rglob("*")} == before_modes
    assert len(launch.refreshed_needles()) == len(refreshed)
    path.unlink()
    from nspawn.native import NativePreparationError

    with pytest.raises(NativePreparationError):
        launch.refreshed_needles()


@pytest.mark.parametrize("kind", ["codex", "agy"])
def test_a_rotation_landing_mid_read_is_read_again_not_fatal(
    provider: Provider, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """Measured 2026-09-23: the vendor rotated its token while the output pump
    was refreshing needles; the double-stat fence saw the file change mid-read
    (an atomic replace trips it too: the old inode loses its link) and the whole
    run failed as `AgyError (nERR-2003)` in about half the runs. The fence is right to refuse THAT read; the rotation is
    exactly what the refresh exists to observe, so it reads again."""
    provider.kind = kind
    provider.model = "gemini-test" if kind == "agy" else "luna"
    home = provider.accounts["one"].home
    assert home is not None
    path = credential(home, "OLD-OAUTH-SECRET-EXACT", kind=kind)
    launch = prepare_native(
        provider,
        "one",
        roster=Roster(),
        cwd=tmp_path,
        read_only=True,
        ambient={"HOME": "/owner", "PATH": "/usr/bin:/bin"},
    )
    real_read = os.read
    rotated: list[bool] = []

    def read_while_rotating(fd: int, size: int) -> bytes:
        data = real_read(fd, size)
        if not rotated:
            rotated.append(True)
            staged = path.with_name(path.name + ".rotating")
            staged.write_text(
                json.dumps(
                    {
                        "tokens" if kind == "codex" else "token": {
                            "access_token": "NEW-OAUTH-SECRET-EXACT"
                        }
                    }
                )
            )
            staged.chmod(0o600)
            staged.replace(path)
        return data

    monkeypatch.setattr(os, "read", read_while_rotating)
    refreshed = launch.refreshed_needles()
    assert rotated
    assert (
        refreshed.redact("OLD-OAUTH-SECRET-EXACT NEW-OAUTH-SECRET-EXACT")
        == "[redacted-credential] [redacted-credential]"
    )


@pytest.mark.parametrize("read_only,mode", [(True, "plan"), (False, "accept-edits")])
def test_agy_launch_has_native_json_flags_and_selected_home(
    provider, tmp_path, read_only, mode
):
    provider.kind = "agy"
    provider.model = "gemini-test"
    credential(provider.accounts["two"].home, "SECOND-AGY-OAUTH-SECRET", kind="agy")
    launch = prepare(
        provider,
        tmp_path,
        account_name="two",
        read_only=read_only,
        effort="high",
        resume_id="native-conversation",
    )
    assert launch.env["HOME"] == str(provider.accounts["two"].home)
    assert "CODEX_HOME" not in launch.env
    assert launch.argv == (
        str(provider.bin.resolve()),
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--model=gemini-test",
        "--mode=" + mode,
        "--sandbox",
        "--effort=high",
        "--conversation=native-conversation",
    )


@pytest.mark.parametrize("kind", ["agy", "codex"])
def test_only_agy_disables_its_updater_without_parent_override(
    provider, tmp_path, kind
):
    from nspawn.native import NativePreparationError

    provider.kind = kind
    provider.model = "gemini-test" if kind == "agy" else "luna"
    credential(provider.accounts["one"].home, "UPDATER-FIXTURE-OAUTH", kind=kind)
    launch = prepare(
        provider,
        tmp_path,
        ambient={
            "HOME": "/owner",
            "PATH": "/usr/bin:/bin",
            "AGY_CLI_DISABLE_AUTO_UPDATE": "false",
        },
    )
    assert launch.env.get("AGY_CLI_DISABLE_AUTO_UPDATE") == (
        "true" if kind == "agy" else None
    )
    provider.env["AGY_CLI_DISABLE_AUTO_UPDATE"] = "false"
    with pytest.raises(NativePreparationError, match="forbidden override"):
        prepare(provider, tmp_path)


def test_canonical_utf8_home_sqlite_and_binary_do_not_follow_moving_alias(
    provider, tmp_path
):
    from dataclasses import replace

    home = tmp_path / "conta-é-東京"
    credential(home, "UTF8-ACCOUNT-SECRET-EXACT")
    sqlite = tmp_path / "shared-sqlite"
    sqlite.mkdir(mode=0o700)
    home_link = tmp_path / "home-alias"
    home_link.symlink_to(home, target_is_directory=True)
    binary_link = tmp_path / "bin-alias"
    binary_link.symlink_to(provider.bin)
    provider.bin = binary_link
    provider.accounts["one"] = replace(
        provider.accounts["one"], home=home_link, sqlite_home=sqlite
    )
    launch = prepare(provider, tmp_path)
    assert launch.context.home == str(home)
    assert launch.context.sqlite_home == str(sqlite)
    assert launch.env["CODEX_SQLITE_HOME"] == str(sqlite)
    assert launch.env["HOME"] == "/owner"
    binary_link.unlink()
    assert Path(launch.argv[0]).is_file()


@pytest.mark.parametrize(
    "damage",
    ["missing", "symlink", "hardlink", "public", "oversize", "empty", "directory"],
)
def test_owner_credential_leaf_fences_are_preserved(provider, tmp_path, damage):
    from nspawn.native import NativePreparationError
    from nspawn.vendor_home import AUTH_MAX_BYTES

    path = provider.accounts["one"].home / "auth.json"
    if damage == "missing":
        path.unlink()
    elif damage == "symlink":
        target = path.with_suffix(".target")
        path.rename(target)
        path.symlink_to(target)
    elif damage == "hardlink":
        path.with_suffix(".second").hardlink_to(path)
    elif damage == "public":
        path.chmod(0o644)
    elif damage == "oversize":
        path.write_bytes(b"x" * (AUTH_MAX_BYTES + 1))
    elif damage == "empty":
        path.write_bytes(b"")
    else:
        path.unlink()
        path.mkdir()
    with pytest.raises(NativePreparationError):
        prepare(provider, tmp_path)


@pytest.mark.parametrize(
    "change",
    [
        "missing-profile",
        "wrong-env",
        "chat-api",
        "openai-auth",
        "inline-key",
        "command-auth",
        "no-binding",
        "openai-binding",
        "missing-key",
        "unsafe-name",
    ],
)
def test_api_binding_never_falls_back_to_openai(api_provider, tmp_path, change):
    from dataclasses import replace

    from nspawn.native import NativePreparationError

    account = api_provider.accounts["max"]
    path = account.home / "config.toml"
    text = path.read_text()
    if change == "missing-profile":
        text = '[model_providers.other]\nwire_api="responses"\n'
    elif change == "wrong-env":
        text = text.replace("SAKANA_MAX_API_KEY", "OTHER_API_KEY")
    elif change == "chat-api":
        text = text.replace("responses", "chat")
    elif change == "openai-auth":
        text += "requires_openai_auth=true\n"
    elif change == "inline-key":
        text += 'experimental_bearer_token="NEVER-ECHO-INLINE"\n'
    elif change == "command-auth":
        text += '[model_providers.sakanamax.auth]\ncommand="anything"\n'
    elif change == "no-binding":
        api_provider.accounts["max"] = replace(account, model_provider=None)
    elif change == "openai-binding":
        api_provider.accounts["max"] = replace(account, model_provider="openai")
    elif change == "unsafe-name":
        api_provider.accounts["max"] = replace(account, auth_env="PYTHONPATH")
        text = text.replace("SAKANA_MAX_API_KEY", "PYTHONPATH")
    elif change == "missing-key":
        (api_provider.secrets / "max.token").unlink()
    path.write_text(text)
    with pytest.raises(NativePreparationError) as error:
        prepare(
            api_provider,
            tmp_path,
            account_name="max",
            ambient={}
            if change == "missing-key"
            else {
                "SAKANA_MAX_API_KEY": "SELECTED-API-SECRET",
                "OPENAI_API_KEY": "OTHER-OPENAI-SECRET",
                "PYTHONPATH": "/injected",
            },
        )
    for secret in ("NEVER-ECHO-INLINE", "SELECTED-API-SECRET", "OTHER-OPENAI-SECRET"):
        assert secret not in str(error.value)


@pytest.mark.parametrize(
    "kwargs", [{"read_paths": ["/etc"]}, {"read_paths": ["/work/file"]}]
)
def test_agy_read_paths_still_refuse(provider, tmp_path, kwargs):
    from nspawn.native import NativePreparationError

    provider.kind = "agy"

    with pytest.raises(NativePreparationError, match="read_paths"):
        prepare(provider, tmp_path, **kwargs)


def test_arbitrary_native_args_refuse_and_defaults_claude_args_are_not_forwarded(
    provider, tmp_path
):
    from nspawn.native import NativePreparationError
    from nspawn.owner_roster import Defaults

    defaults = Defaults(
        args=["--dangerously-skip-permissions"],
        env={"ANTHROPIC_AUTH_TOKEN": "DO-NOT-INHERIT"},
    )
    launch = prepare(provider, tmp_path, roster=Roster(defaults=defaults))
    assert "--dangerously-skip-permissions" not in launch.argv
    assert "ANTHROPIC_AUTH_TOKEN" not in launch.env
    provider.args = ["--dangerously-skip-permissions"]
    with pytest.raises(NativePreparationError, match="args"):
        prepare(provider, tmp_path)


def test_api_profile_also_redacts_visible_oauth_and_refresh(api_provider, tmp_path):
    launch = prepare(
        api_provider,
        tmp_path,
        account_name="max",
        ambient={"SAKANA_MAX_API_KEY": "SELECTED-API-KEY-ONLY"},
    )
    assert (
        launch.needles.redact("FIRST-OAUTH-SECRET-FOR-TESTING")
        == "[redacted-credential]"
    )
    credential(api_provider.accounts["max"].home, "REFRESHED-SUBSCRIPTION-TOKEN")
    text = "EXACT-SELECTED-API-SECRET FIRST-OAUTH-SECRET-FOR-TESTING REFRESHED-SUBSCRIPTION-TOKEN"
    assert (
        launch.refreshed_needles().redact(text)
        == "[redacted-credential] [redacted-credential] [redacted-credential]"
    )


def test_api_only_home_needs_no_oauth_file(api_provider, tmp_path):
    (api_provider.accounts["max"].home / "auth.json").unlink()
    launch = prepare(
        api_provider,
        tmp_path,
        account_name="max",
        ambient={"SAKANA_MAX_API_KEY": "SELECTED-API-SECRET"},
    )
    assert launch.refreshed_needles() is launch.needles


def test_three_oauth_generations_are_retained_with_explicit_bounded_previous(
    provider, tmp_path
):
    from nspawn.native import MAX_NATIVE_NEEDLE_BYTES, NativePreparationError
    from nspawn.redact import SecretNeedles

    launch = prepare(provider, tmp_path)
    credential(provider.accounts["one"].home, "SECOND-ROTATION-SECRET")
    previous = launch.refreshed_needles()
    credential(provider.accounts["one"].home, "THIRD-ROTATION-SECRET")
    retained = launch.refreshed_needles(previous)
    assert (
        retained.redact(
            "FIRST-OAUTH-SECRET-FOR-TESTING SECOND-ROTATION-SECRET THIRD-ROTATION-SECRET"
        )
        == "[redacted-credential] [redacted-credential] [redacted-credential]"
    )
    with pytest.raises(NativePreparationError, match="budget"):
        launch.refreshed_needles(SecretNeedles(("s" * (MAX_NATIVE_NEEDLE_BYTES + 1),)))


@pytest.mark.parametrize("configured", [None, "", "Account instructions."])
def test_native_prompt_snapshot_is_bounded_and_not_stored_in_argv(
    provider, tmp_path, configured
):
    from nspawn.app_server import MAX_REQUEST_BYTES
    from nspawn.owner_roster import Defaults
    from nspawn.rows import MAX_DURABLE_ROW_BYTES

    config = provider.accounts["one"].home / "config.toml"
    if configured is not None:
        config.write_text("developer_instructions=" + json.dumps(configured))
        config.chmod(0o600)
    canonical = tmp_path / "canonical.md"
    canonical.write_text("\x01" * MAX_DURABLE_ROW_BYTES)
    canonical.chmod(0o600)
    run_prompt = "\x02" * MAX_DURABLE_ROW_BYTES
    launch = prepare(
        provider,
        tmp_path,
        roster=Roster(defaults=Defaults(system_prompt=str(canonical))),
        system_prompt=run_prompt,
    )
    expected = "\n\n".join(
        ([configured] if configured else []) + [canonical.read_text(), run_prompt]
    )
    assert launch.developer_instructions == expected
    assert len(expected.encode()) > MAX_DURABLE_ROW_BYTES
    assert len(json.dumps(expected).encode()) < MAX_REQUEST_BYTES - 16384
    assert expected not in repr(launch)
    assert all(run_prompt not in arg for arg in launch.argv)
    canonical.write_text("Changed after preparation.")
    assert launch.developer_instructions == expected


@pytest.mark.parametrize(
    "prompt",
    ["", " ", "\x00", "\ud800", "x" * 65537],
    ids=["empty", "whitespace", "nul", "unpaired-surrogate", "oversized"],
)
def test_native_per_run_prompt_refuses_invalid_text(provider, tmp_path, prompt):
    from nspawn.native import NativePreparationError

    with pytest.raises(NativePreparationError, match="native developer instructions"):
        prepare(provider, tmp_path, system_prompt=prompt)


def test_native_read_roots_are_canonical_and_include_exact_runtime(provider, tmp_path):
    granted = tmp_path.parent / (tmp_path.name + "-granted")
    granted.mkdir()
    alias = tmp_path / "grant-link"
    alias.symlink_to(granted, target_is_directory=True)
    launch = prepare(provider, tmp_path, read_paths=[str(alias)])
    assert set(launch.context.read_roots) == {
        str(tmp_path.resolve()),
        str(granted.resolve()),
        str(provider.bin.resolve()),
    }


def test_fugu_credentials_reach_codex_and_the_declared_nspawn_mcp(
    api_provider, tmp_path
):
    from nspawn.owner_roster import Defaults

    plugin = tmp_path / "plugin"
    plugin.mkdir()
    variables = ["SAKANA_MAX_API_KEY", "SAKANA_PRO_API_KEY"]
    (plugin / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "nmemory": {"command": "/fixture/nmemory"},
                    "nspawn": {
                        "command": "/fixture/nspawn",
                        "env_vars": variables,
                    },
                }
            }
        )
    )
    ambient = {name: f"SYNTHETIC-{name}" for name in variables}

    launch = prepare(
        api_provider,
        tmp_path,
        account_name="max",
        ambient=ambient,
        roster=Roster(defaults=Defaults(plugin_root=plugin)),
    )

    assert launch.mcp_servers["nspawn"]["env"] == ambient
    assert launch.env["SAKANA_MAX_API_KEY"] == "EXACT-SELECTED-API-SECRET"
    assert "SAKANA_PRO_API_KEY" not in launch.env
    assert "env" not in launch.mcp_servers["nmemory"]


@pytest.mark.parametrize("tier", ["low", "medium", "high"])
def test_agy_model_tier_carries_the_effort_and_is_not_passed_twice(
    provider, tmp_path, tier
):
    """AGY spells the effort INSIDE the model id, and refuses the pair.

    Measured 2026-09-20 against the live CLI: `--model=gemini-3.8-flash-low`
    with `--effort=low` is accepted, and the same model with `medium` or
    `high` dies as `invalid model selection`. So the tier suffix IS the
    effort axis. A review dispatched with the roster's own effort killed
    the run after launch, which is the friction this closes: the flag is
    omitted when the model already carries the tier.
    """
    provider.kind = "agy"
    provider.model = f"gemini-3.8-flash-{tier}"
    credential(provider.accounts["two"].home, "SECOND-AGY-OAUTH-SECRET", kind="agy")
    launch = prepare(provider, tmp_path, account_name="two", effort=tier)
    assert f"--model={provider.model}" in launch.argv
    assert not any(arg.startswith("--effort=") for arg in launch.argv)


def test_agy_effort_selects_the_served_model_of_that_tier(provider, tmp_path):
    """The effort is not a second knob beside the model — it PICKS the model.

    Measured 2026-09-20: with `model = "gemini-3.8-flash-high"` in the roster,
    six of the eight declared classes ask for `low` or `medium` and were all
    refused before the child existed, including `design`, the very class the
    roster ranks this route highest for. Rewriting the suffix to the tier the
    caller asked for serves the request the route does serve.
    """
    provider.kind = "agy"
    provider.model = "gemini-3.8-flash-high"
    provider.models = ["gemini-3.8-flash-high", "gemini-3.8-flash-medium"]
    credential(provider.accounts["two"].home, "SECOND-AGY-OAUTH-SECRET", kind="agy")
    launch = prepare(provider, tmp_path, account_name="two", effort="medium")
    assert "--model=gemini-3.8-flash-medium" in launch.argv
    assert not any(arg.startswith("--effort=") for arg in launch.argv)


def test_agy_refuses_a_tier_the_route_does_not_serve(provider, tmp_path):
    """Substituting a tier the roster never declared would invent a route."""
    from nspawn.native import NativePreparationError

    provider.kind = "agy"
    provider.model = "gemini-3.8-flash-high"
    provider.models = ["gemini-3.8-flash-high", "gemini-3.8-flash-medium"]
    credential(provider.accounts["two"].home, "SECOND-AGY-OAUTH-SECRET", kind="agy")
    with pytest.raises(NativePreparationError, match="does not serve"):
        prepare(provider, tmp_path, account_name="two", effort="low")


def test_agy_still_passes_an_effort_for_a_model_without_a_tier(provider, tmp_path):
    provider.kind = "agy"
    provider.model = "gemini-test"
    credential(provider.accounts["two"].home, "SECOND-AGY-OAUTH-SECRET", kind="agy")
    launch = prepare(provider, tmp_path, account_name="two", effort="high")
    assert "--effort=high" in launch.argv
