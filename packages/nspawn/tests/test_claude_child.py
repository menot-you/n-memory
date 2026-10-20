"""claude_child: version gate, argv composition, owner boot, session minting,
credential source, forbidden flags.

"""

from __future__ import annotations

import builtins
import io
import json
import os
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from nspawn import claude_child
from nspawn.claude_child import (
    FORBIDDEN_CLAUDE_FLAGS,
    MAX_MCP_CONFIG_BYTES,
    SETTING_SOURCES,
    VERSION_TIMEOUT_SECONDS,
    BinaryIdentityError,
    ChildMcpConfig,
    ChildMcpConfigError,
    NoDeclaredBinary,
    Version,
    binary_identity,
    check_claude_version,
    inspect_child_mcp_config,
    inspect_claude_installation,
    mint_session_id,
    parse_version,
    resolve_claude_binary,
)

# --- parse_version ---


def test_parser_accepts_the_measured_claude_version_output():
    assert parse_version("2.1.220 (Claude Code)\n") == Version(2, 1, 220)


def test_parser_rejects_garbage():
    assert parse_version("") is None
    assert parse_version("abc") is None
    assert parse_version("2.1") is None
    assert parse_version(None) is None  # type: ignore[arg-type]


# --- binary_identity ---


def test_binary_identity_stats_a_file(tmp_path):
    p = tmp_path / "claude.bin"
    p.write_bytes(b"#!/bin/sh\nexec claude")
    ident = binary_identity(str(p))
    assert ident.size == p.stat().st_size
    assert ident.device == p.stat().st_dev
    assert ident.inode == p.stat().st_ino


def test_binary_identity_refuses_missing_file(tmp_path):
    with pytest.raises(BinaryIdentityError):
        binary_identity(str(tmp_path / "absent"))


# --- FORBIDDEN_CLAUDE_FLAGS / SETTING_SOURCES / PROMPTSeparator ---


def test_forbidden_flags_list_is_complete():
    # Pin names exactly four forbidden flags in this list.
    assert set(FORBIDDEN_CLAUDE_FLAGS) == {
        "--dangerously-skip-permissions",
        "--permission-mode",
        "--add-dir",
        "--mcp-config",
    }
    assert SETTING_SOURCES == "user"


# --- minted_session_ids_are_v4_uuids_and_distinct ---


def test_minted_session_ids_are_v4_uuids_and_distinct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_open = builtins.open

    def _finite_urandom(path: str, mode: str) -> io.BytesIO:
        # /dev/urandom never ends, so a read that lost its size never returns;
        # 64 real bytes from it keep the read finite and hold more than 16.
        with real_open(path, mode) as device:
            return io.BytesIO(device.read(64))

    monkeypatch.setattr(claude_child, "open", _finite_urandom, raising=False)
    first = mint_session_id()
    second = mint_session_id()
    assert first != second
    for sid in [first, second]:
        assert len(sid) == 36
        assert sid[14] == "4", f"version nibble must be 4: {sid}"
        assert sid[19] in ("8", "9", "a", "b"), (
            f"variant nibble must be RFC 4122: {sid}"
        )


# --- stream_composer_emits_stream_flags_and_no_forbidden_flags ---


# --- VERSION_TIMEOUT_SECONDS — §43 constant lock ---
# The constant carries an explicit assertion against the contract value (10s,
# claude_child.rs line 46). Asserting existence is not enough: §43 requires
# asserting the VALUE.


def test_version_timeout_constant_matches_pin():
    """claude_child.rs:46 sets VERSION_TIMEOUT = Duration::from_secs(10).
    If this drifts, the probe budget changes and the live door's contract
    with the owner changes with it. The constant is named, not magic, so
    the test names it.
    """
    assert VERSION_TIMEOUT_SECONDS == 10, (
        "pin claude_child.rs:46 sets VERSION_TIMEOUT = Duration::from_secs(10); "
        f"drift to {VERSION_TIMEOUT_SECONDS} changes the probe budget"
    )


# --- check_claude_version seam ---


def test_check_claude_version_without_probe_raises_not_implemented():
    """No probe callable: the seam is the boundary between this module
    (composition) and the version runner. Refusing to fall back to a
    default probe is what keeps the surface from leaking subprocess /
    fork concerns into claude_child.
    """
    with pytest.raises(NotImplementedError) as exc_info:
        check_claude_version("/usr/bin/claude")
    assert "spawn-d2" in str(exc_info.value).lower()


def test_check_claude_version_returns_whatever_the_probe_observed():
    """With an injected probe: bypasses the runner, reports what it saw.

    The probe's version is returned as OBSERVED. nSPAWN never judges the
    installed version (owner law, 2026-09-01): the gate left this function on
    2026-08-28 and the warning that replaced it left on 2026-09-01.
    """

    def fake_probe_measured(binary: str) -> Version:
        return Version(2, 1, 220)

    def fake_probe_unmeasured(binary: str) -> Version:
        return Version(2, 1, 999)

    binary = "/usr/bin/claude"
    assert check_claude_version(binary, probe=fake_probe_measured) == Version(2, 1, 220)
    assert check_claude_version(binary, probe=fake_probe_unmeasured) == Version(
        2, 1, 999
    )


def test_check_claude_version_probe_receives_binary_path_unchanged():
    """The probe callable receives the exact path passed to check_claude_version.

    The seam is a contract: what comes in is what reaches the runner.
    """
    seen: list[str] = []

    def spy_probe(binary: str) -> Version:
        seen.append(binary)
        return Version(2, 1, 220)

    check_claude_version("/path/to/claude-under-test", probe=spy_probe)
    assert seen == ["/path/to/claude-under-test"]


def test_check_claude_version_does_not_call_probe_when_seam_is_unbound():
    """The unbound seam short-circuits BEFORE any probe work, which is the
    load-bearing property that keeps subprocess work out of this module. The probe is not supplied: NotImplementedError fires
    before any callable lookup, and the test's lack of a probe is itself
    the proof.
    """
    with pytest.raises(NotImplementedError):
        check_claude_version("/usr/bin/claude")


# --- L8: resolve_claude_binary -------------------------------------------------
# Precedence is explicit → environment override → provider binary → roster
# default. Absent everywhere is a typed refusal, and a relative winner is the
# same refusal: a relative name defers to ambient PATH/cwd, which is a lookup,
# not a declaration.


def test_resolve_precedence_explicit_beats_every_later_source():
    explicit = Path("/opt/declared/claude")
    assert (
        resolve_claude_binary(
            explicit=explicit,
            environment_override=Path("/opt/env/claude"),
            provider_binary=Path("/opt/provider/claude"),
            roster_default=Path("/opt/roster/claude"),
        )
        == explicit
    )


def test_resolve_precedence_environment_beats_provider_and_roster():
    override = Path("/opt/env/claude")
    assert (
        resolve_claude_binary(
            environment_override=override,
            provider_binary=Path("/opt/provider/claude"),
            roster_default=Path("/opt/roster/claude"),
        )
        == override
    )


def test_resolve_precedence_provider_beats_roster():
    provider = Path("/opt/provider/claude")
    assert (
        resolve_claude_binary(
            provider_binary=provider, roster_default=Path("/opt/roster/claude")
        )
        == provider
    )


def test_resolve_roster_default_alone_is_a_declaration():
    roster = Path("/opt/roster/claude")
    assert resolve_claude_binary(roster_default=roster) == roster


def test_resolve_all_sources_absent_refuses_naming_the_path_law():
    with pytest.raises(NoDeclaredBinary) as refusal:
        resolve_claude_binary()
    assert "ambient PATH" in str(refusal.value)


def test_resolve_relative_winner_refuses_as_an_ambient_lookup():
    with pytest.raises(NoDeclaredBinary) as refusal:
        resolve_claude_binary(explicit=Path("claude"))
    assert "relative" in str(refusal.value)


def test_resolve_relative_lower_source_never_reaches_absolute_fallback():
    # A relative winner refuses even when later sources are absolute: the
    # declaration that won is the one whose target is unpinned.
    with pytest.raises(NoDeclaredBinary):
        resolve_claude_binary(
            explicit=Path("./claude"),
            roster_default=Path("/opt/roster/claude"),
        )


_absolute_binaries = st.builds(
    lambda seg: Path(f"/opt/{seg}/claude"),
    st.from_regex(r"[a-z]{1,8}", fullmatch=True),
)
_optional_binaries = st.none() | _absolute_binaries


@given(
    st.lists(_optional_binaries, min_size=4, max_size=4).filter(
        lambda quad: any(value is not None for value in quad)
    )
)
@settings(derandomize=True, max_examples=200)
def test_resolve_property_winner_is_the_earliest_declared_source(
    quad: list[Path | None],
):
    resolved = resolve_claude_binary(
        explicit=quad[0],
        environment_override=quad[1],
        provider_binary=quad[2],
        roster_default=quad[3],
    )
    # The resolved binary is one of the declared values...
    assert resolved in quad
    # ...sitting at the earliest slot that carries a declaration: nothing
    # earlier was skipped. `index` finds that earliest slot even under
    # duplicate declarations.
    slot = quad.index(resolved)
    assert all(value is None for value in quad[:slot])


_relative_binaries = st.builds(
    lambda seg: Path(f"./{seg}/claude"),
    st.from_regex(r"[a-z]{1,8}", fullmatch=True),
)


@given(st.lists(_relative_binaries, min_size=4, max_size=4))
@settings(derandomize=True, max_examples=100)
def test_resolve_property_relative_declarations_always_refuse(quad: list[Path]):
    with pytest.raises(NoDeclaredBinary):
        resolve_claude_binary(
            explicit=quad[0],
            environment_override=quad[1],
            provider_binary=quad[2],
            roster_default=quad[3],
        )


# --- owner law 2026-08-28: version allowlist stops being a gate ---------------


# --- L8: inspect_claude_installation ------------------------------------------


def _installation_binary(tmp_path: Path) -> Path:
    binary = tmp_path / "claude"
    binary.write_bytes(b"#!/bin/sh\nexec claude\n")
    return binary


def test_installation_binds_identity_and_supported_version(tmp_path: Path):
    binary = _installation_binary(tmp_path)
    installation = inspect_claude_installation(binary, lambda _: Version(2, 1, 241))
    assert installation.binary == binary
    assert installation.version == Version(2, 1, 241)
    assert installation.identity == binary_identity(str(binary))


def test_installation_carries_no_verdict_on_the_installed_version(
    tmp_path: Path,
):
    """Owner order 2026-09-01: nSPAWN uses whatever is installed, and that's that.

    The version is READ and recorded; never judged. There is no verdict field
    about it, so there is no warning a host update could trigger on its
    own. A genuinely broken wire still shows up as a typed classification
    error against the observed records.
    """
    binary = _installation_binary(tmp_path)
    installation = inspect_claude_installation(binary, lambda _: Version(9, 9, 9))
    assert installation.version == Version(9, 9, 9)
    assert not hasattr(installation, "version_warning")
    # Binary identity remains a hard refusal: that boundary was not the one
    # opened by the owner.
    assert installation.identity == binary_identity(str(binary))


def test_installation_stats_identity_before_any_probe_work(tmp_path: Path):
    seen: list[str] = []

    def spy(binary: str) -> Version:
        seen.append(binary)
        return Version(2, 1, 220)

    with pytest.raises(BinaryIdentityError):
        inspect_claude_installation(tmp_path / "absent", spy)
    assert seen == []


def test_installation_refuses_a_directory_as_the_binary(tmp_path: Path):
    seen: list[str] = []

    def spy(binary: str) -> Version:
        seen.append(binary)
        return Version(2, 1, 220)

    with pytest.raises(BinaryIdentityError) as refusal:
        inspect_claude_installation(tmp_path, spy)
    assert "regular file" in str(refusal.value)
    assert seen == []


def test_installation_refuses_a_binary_replaced_during_the_probe(tmp_path: Path):
    binary = _installation_binary(tmp_path)

    def replacing_probe(_: str) -> Version:
        # Mid-inspection replacement, driven through the public seam that
        # runs between the two stat calls.
        with open(binary, "ab") as handle:
            handle.write(b"\n")
        return Version(2, 1, 220)

    with pytest.raises(BinaryIdentityError) as refusal:
        inspect_claude_installation(binary, replacing_probe)
    assert "changed" in str(refusal.value)


@given(st.tuples(st.integers(0, 9), st.integers(0, 9), st.integers(0, 9)))
@settings(derandomize=True)
def test_installation_property_binds_every_version_without_a_verdict(
    example_dir: Path,
    parts: tuple[int, int, int],
):
    # The property that remains after the owner's order: NO version is
    # refused by number and NONE carries a verdict. What is recorded is what
    # is installed.
    binary = example_dir / "claude"
    binary.write_bytes(b"#!/bin/sh\nexec claude\n")
    version = Version(*parts)
    installation = inspect_claude_installation(binary, lambda _: version)
    assert installation.version == version
    assert not hasattr(installation, "version_warning")
    assert installation.identity.size == binary.stat().st_size


# --- L8: inspect_child_mcp_config ---------------------------------------------


@pytest.fixture(scope="module")
def example_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One directory for every example of this module's property tests.

    Each example rewrites its file in place. A fresh `TemporaryDirectory` per
    example was a mkdir, create, unlink and two rmdirs on every one of up to
    300 examples; under load that crossed Hypothesis's 200 ms per-example
    deadline and failed the fuzz test as flaky, measured 2026-09-24."""
    return tmp_path_factory.mktemp("examples")


def _example_plugin(example_dir: Path) -> Path:
    plugin_dir = example_dir / "plugin"
    plugin_dir.mkdir(exist_ok=True)
    return plugin_dir


def _plugin(tmp_path: Path) -> Path:
    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir()
    return plugin_dir


def _write_config(plugin_dir: Path, document: object) -> Path:
    config_path = plugin_dir / ".mcp.json"
    if isinstance(document, bytes):
        config_path.write_bytes(document)
    else:
        config_path.write_text(json.dumps(document), encoding="utf-8")
    return config_path


_REAL_PLUGIN_SHAPE: dict[str, object] = {
    "mcpServers": {
        "nmemory": {
            "type": "stdio",
            "command": "${HOME}/.nott/bin/nmemory",
            "args": ["--project", "nott"],
            "env": {"OPENAI_API_KEY": "${OPENAI_API_KEY}"},
        },
        "nbrowser": {
            "type": "stdio",
            "command": "${HOME}/.nott/bin/nbrowser",
            "env": {
                "NOTT_CONTENT_AUTHORITY_KEY_HEX": "${NOTT_CONTENT_AUTHORITY_KEY_HEX}"
            },
        },
        "nspawn": {"type": "stdio", "command": "${HOME}/.nott/bin/nspawn"},
    }
}


def test_config_reports_server_names_in_file_order(tmp_path: Path):
    plugin_dir = _plugin(tmp_path)
    _write_config(plugin_dir, _REAL_PLUGIN_SHAPE)
    config = inspect_child_mcp_config(plugin_dir)
    assert config.plugin_dir == plugin_dir
    assert config.server_names == ("nmemory", "nbrowser", "nspawn")


def test_config_absent_file_refuses(tmp_path: Path):
    with pytest.raises(ChildMcpConfigError) as refusal:
        inspect_child_mcp_config(_plugin(tmp_path))
    assert refusal.value.path.name == ".mcp.json"


def test_config_symlink_refuses(tmp_path: Path):
    plugin_dir = _plugin(tmp_path)
    target = tmp_path / "real.json"
    target.write_text(json.dumps(_REAL_PLUGIN_SHAPE), encoding="utf-8")
    (plugin_dir / ".mcp.json").symlink_to(target)
    with pytest.raises(ChildMcpConfigError) as refusal:
        inspect_child_mcp_config(plugin_dir)
    assert "symlink" in refusal.value.reason


def test_config_directory_instead_of_file_refuses(tmp_path: Path):
    plugin_dir = _plugin(tmp_path)
    (plugin_dir / ".mcp.json").mkdir()
    with pytest.raises(ChildMcpConfigError) as refusal:
        inspect_child_mcp_config(plugin_dir)
    assert "regular file" in refusal.value.reason


def test_config_truncated_json_refuses(tmp_path: Path):
    plugin_dir = _plugin(tmp_path)
    _write_config(plugin_dir, b'{"mcpServers": {"nmemory"')
    with pytest.raises(ChildMcpConfigError) as refusal:
        inspect_child_mcp_config(plugin_dir)
    assert "parseable JSON" in refusal.value.reason


def test_config_invalid_utf8_refuses(tmp_path: Path):
    plugin_dir = _plugin(tmp_path)
    _write_config(plugin_dir, b"\xff\xfe\x7b\x00")
    with pytest.raises(ChildMcpConfigError) as refusal:
        inspect_child_mcp_config(plugin_dir)
    assert "parseable JSON" in refusal.value.reason


def test_config_non_object_document_refuses(tmp_path: Path):
    plugin_dir = _plugin(tmp_path)
    _write_config(plugin_dir, [])
    with pytest.raises(ChildMcpConfigError) as refusal:
        inspect_child_mcp_config(plugin_dir)
    assert "JSON object" in refusal.value.reason


def test_config_missing_mcp_servers_key_refuses(tmp_path: Path):
    plugin_dir = _plugin(tmp_path)
    _write_config(plugin_dir, {"other": {}})
    with pytest.raises(ChildMcpConfigError):
        inspect_child_mcp_config(plugin_dir)


def test_config_mcp_servers_not_an_object_refuses(tmp_path: Path):
    plugin_dir = _plugin(tmp_path)
    _write_config(plugin_dir, {"mcpServers": 3})
    with pytest.raises(ChildMcpConfigError):
        inspect_child_mcp_config(plugin_dir)


def test_config_missing_required_nmemory_refuses(tmp_path: Path):
    plugin_dir = _plugin(tmp_path)
    _write_config(
        plugin_dir,
        {"mcpServers": {"nbrowser": {"command": "${HOME}/bin/nbrowser"}}},
    )
    with pytest.raises(ChildMcpConfigError) as refusal:
        inspect_child_mcp_config(plugin_dir)
    assert "nmemory" in refusal.value.reason


def test_config_custom_required_servers(tmp_path: Path):
    plugin_dir = _plugin(tmp_path)
    _write_config(plugin_dir, _REAL_PLUGIN_SHAPE)
    assert inspect_child_mcp_config(
        plugin_dir, required_servers=("nbrowser", "nspawn")
    ).server_names == ("nmemory", "nbrowser", "nspawn")
    with pytest.raises(ChildMcpConfigError):
        inspect_child_mcp_config(plugin_dir, required_servers=("ghost",))


def test_config_non_object_server_declaration_refuses(tmp_path: Path):
    plugin_dir = _plugin(tmp_path)
    _write_config(
        plugin_dir,
        {"mcpServers": {"nmemory": None, "nspawn": {"command": "x"}}},
    )
    with pytest.raises(ChildMcpConfigError) as refusal:
        inspect_child_mcp_config(plugin_dir)
    assert "non-object" in refusal.value.reason


def test_config_non_object_env_refuses(tmp_path: Path):
    plugin_dir = _plugin(tmp_path)
    _write_config(
        plugin_dir,
        {"mcpServers": {"nmemory": {"env": 3}}},
    )
    with pytest.raises(ChildMcpConfigError) as refusal:
        inspect_child_mcp_config(plugin_dir)
    assert "env" in refusal.value.reason


def test_config_non_textual_env_value_refuses(tmp_path: Path):
    plugin_dir = _plugin(tmp_path)
    _write_config(
        plugin_dir,
        {"mcpServers": {"nmemory": {"env": {"TOKEN": 7}}}},
    )
    with pytest.raises(ChildMcpConfigError) as refusal:
        inspect_child_mcp_config(plugin_dir)
    assert "non-textual" in refusal.value.reason


def test_config_literal_credential_bytes_refuse(tmp_path: Path):
    plugin_dir = _plugin(tmp_path)
    _write_config(
        plugin_dir,
        {
            "mcpServers": {
                "nmemory": {"env": {"OPENAI_API_KEY": "${OPENAI_API_KEY}"}},
                "nvision": {"env": {"NOTT_NVISION_STORE_KEY_HEX": "deadbeefcafebabe"}},
            }
        },
    )
    with pytest.raises(ChildMcpConfigError) as refusal:
        inspect_child_mcp_config(plugin_dir)
    assert "credential bytes" in refusal.value.reason
    assert "deadbeefcafebabe" not in refusal.value.reason


def test_config_prefixed_reference_refuses(tmp_path: Path):
    plugin_dir = _plugin(tmp_path)
    _write_config(
        plugin_dir,
        {"mcpServers": {"nmemory": {"env": {"X": "prefix${HOME}"}}}},
    )
    with pytest.raises(ChildMcpConfigError):
        inspect_child_mcp_config(plugin_dir)


def test_config_suffixed_reference_refuses(tmp_path: Path):
    plugin_dir = _plugin(tmp_path)
    _write_config(
        plugin_dir,
        {"mcpServers": {"nmemory": {"env": {"X": "${HOME}/suffix"}}}},
    )
    with pytest.raises(ChildMcpConfigError):
        inspect_child_mcp_config(plugin_dir)


def test_config_doubled_reference_refuses(tmp_path: Path):
    plugin_dir = _plugin(tmp_path)
    _write_config(
        plugin_dir,
        {"mcpServers": {"nmemory": {"env": {"X": "${A}${B}"}}}},
    )
    with pytest.raises(ChildMcpConfigError):
        inspect_child_mcp_config(plugin_dir)


def test_config_empty_env_value_refuses(tmp_path: Path):
    plugin_dir = _plugin(tmp_path)
    _write_config(
        plugin_dir,
        {"mcpServers": {"nmemory": {"env": {"X": ""}}}},
    )
    with pytest.raises(ChildMcpConfigError):
        inspect_child_mcp_config(plugin_dir)


def test_config_oversized_document_refuses_unread(tmp_path: Path):
    plugin_dir = _plugin(tmp_path)
    _write_config(plugin_dir, b'{"pad": "' + b"a" * (MAX_MCP_CONFIG_BYTES + 1) + b'"}')
    with pytest.raises(ChildMcpConfigError) as refusal:
        inspect_child_mcp_config(plugin_dir)
    assert "read cap" in refusal.value.reason


def test_config_read_failure_refuses(tmp_path: Path):
    plugin_dir = _plugin(tmp_path)
    config_path = _write_config(plugin_dir, _REAL_PLUGIN_SHAPE)

    def refusing_open(*args: object, **kwargs: object):
        raise OSError("device gone")

    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(builtins, "open", refusing_open)
        with pytest.raises(ChildMcpConfigError) as refusal:
            inspect_child_mcp_config(plugin_dir)
    assert "cannot be read" in refusal.value.reason
    assert config_path.is_file()


def test_config_read_cap_constant_is_locked_to_its_declared_value():
    # §43-style value lock: the cap is 64 KiB because the config is a
    # handful of servers; a silent change resizes the refusal surface.
    assert MAX_MCP_CONFIG_BYTES == 64 * 1024


_pure_env_values = st.builds(
    lambda name: "${" + name + "}",
    st.from_regex(r"[A-Z][A-Z0-9_]{0,15}", fullmatch=True),
)
_server_names_strategy = st.from_regex(r"srv_[a-z]{1,8}", fullmatch=True)
_other_servers = st.dictionaries(
    _server_names_strategy,
    st.dictionaries(
        st.from_regex(r"[A-Z][A-Z0-9_]{0,15}", fullmatch=True),
        _pure_env_values,
        max_size=4,
    ),
    max_size=5,
)


@given(_other_servers)
@settings(derandomize=True, max_examples=200)
def test_config_property_pure_references_round_trip_names(
    example_dir: Path,
    others: dict[str, dict[str, str]],
):
    servers = {"nmemory": {"env": {"OPENAI_API_KEY": "${OPENAI_API_KEY}"}}}
    servers.update(others)
    plugin_dir = _example_plugin(example_dir)
    (plugin_dir / ".mcp.json").write_text(
        json.dumps({"mcpServers": servers}), encoding="utf-8"
    )
    config = inspect_child_mcp_config(plugin_dir)
    assert config.server_names == tuple(servers)


# Structurally non-reference values: a leading non-`${` character, a
# trailing non-`}` character, or text drawn from an alphabet without any of
# `$`, `{`, `}` — none of these can match `^\$\{[A-Za-z_][A-Za-z0-9_]*\}$`.
_poisoned_values = st.one_of(
    st.builds(lambda v: "a" + v, st.text(max_size=20)),
    st.builds(lambda v: v + "a", st.text(max_size=20)),
    st.text(alphabet="abcXYZ019_.-", min_size=0, max_size=24),
    st.integers(),
)


@given(
    _server_names_strategy,
    st.from_regex(r"[A-Z][A-Z0-9_]{0,15}", fullmatch=True),
    _poisoned_values,
)
@settings(derandomize=True, max_examples=200)
def test_config_property_any_non_reference_value_refuses(
    example_dir: Path, server: str, variable: str, poison: str
):
    document = {"mcpServers": {"nmemory": {}, server: {"env": {variable: poison}}}}
    plugin_dir = _example_plugin(example_dir)
    (plugin_dir / ".mcp.json").write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ChildMcpConfigError):
        inspect_child_mcp_config(plugin_dir)


# --- L8: fuzz — external bytes never escape the typed refusal ------------------
# Corpus is versioned in THIS file; the random stage is pinned by
# derandomize=True so the same bytes replay on every run.

_FUZZ_CORPUS: tuple[bytes, ...] = (
    b"",
    b"\x00",
    b"\xff\xfe\x7b\x00",
    b"{",
    b"}",
    b"[]",
    b"null",
    b'{"mcpServers":',
    b'{"mcpServers": null}',
    b'{"mcpServers": []}',
    b'{"mcpServers": {"nmemory": null}}',
    b'{"mcpServers": {"nmemory": {"env": null}}}',
    b'{"mcpServers": {"nmemory": {"env": {"A": null}}}}',
    b'{"mcpServers": {"nmemory": {"env": {"A": 3.5}}}}',
    b'{"mcpServers": {"nmemory": {"env": {"A": "sk-ant-' + b'oat01-CORPUS"}}}}',
    b'{"mcpServers": {"nmemory": {"env": {"A": "${A}${B}"}}}}',
    b'{"mcpServers": {"nmemory": {}, "x": {"env": {"A": "literal"}}}}',
    b'\xef\xbb\xbf{"mcpServers": {"nmemory": {}}}',
    b'{"mcpServers": {"nmemory": {}}}\x00trailing',
    b'{"mcpServers": {"\\ud800": {}}}',
)


@pytest.mark.parametrize("raw", _FUZZ_CORPUS)
def test_config_fuzz_corpus_is_always_a_typed_outcome(raw: bytes, tmp_path: Path):
    plugin_dir = _plugin(tmp_path)
    _write_config(plugin_dir, raw)
    try:
        config = inspect_child_mcp_config(plugin_dir)
    except ChildMcpConfigError:
        return
    assert isinstance(config, ChildMcpConfig)
    assert "nmemory" in config.server_names


@given(st.binary(max_size=256))
@settings(derandomize=True, max_examples=300)
def test_config_fuzz_arbitrary_bytes_are_always_a_typed_outcome(
    example_dir: Path, raw: bytes
):
    plugin_dir = _example_plugin(example_dir)
    (plugin_dir / ".mcp.json").write_bytes(raw)
    try:
        config = inspect_child_mcp_config(plugin_dir)
    except ChildMcpConfigError:
        return
    assert isinstance(config, ChildMcpConfig)
    assert "nmemory" in config.server_names


# --- module-wide branch closure (pre-existing surface owned by this module) ----


def test_mint_session_id_refuses_a_short_urandom_read():
    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(builtins, "open", lambda *args, **kwargs: io.BytesIO(b"ab"))
        with pytest.raises(OSError):
            mint_session_id()


# --- mutation hardening: exact operator-facing contracts -----------------------
# Each assertion here pins a surface a mutant proved unobserved: the wire
# text of a refusal, the attributes a consumer reads, the exact argv the
# vendor parses, and the boundary values the caps enforce.


def test_binary_identity_error_names_path_and_reason(tmp_path):
    absent = tmp_path / "absent"
    with pytest.raises(BinaryIdentityError) as refusal:
        binary_identity(str(absent))
    assert refusal.value.path == str(absent)
    assert "No such file" in refusal.value.detail


def test_binary_identity_mtime_math_is_exact(tmp_path):
    p = tmp_path / "claude.bin"
    p.write_bytes(b"#!/bin/sh\nexec claude")
    os.utime(p, (100.5, 100.5))
    ident = binary_identity(str(p))
    stat_result = p.stat()
    assert (
        ident.modified_ns
        == int(stat_result.st_mtime) * 1_000_000_000 + stat_result.st_mtime_ns
    )


def test_no_declared_binary_all_absent_message_is_exact():
    with pytest.raises(NoDeclaredBinary) as refusal:
        resolve_claude_binary()
    assert str(refusal.value) == (
        "no declared claude binary: explicit, environment override, provider binary, "
        "and roster default are all absent; nSPAWN never resolves `claude` from "
        "ambient PATH"
    )
    assert refusal.value.detail.endswith("ambient PATH")


@pytest.mark.parametrize(
    ("kwargs", "label"),
    [
        ({"explicit": Path("claude")}, "explicit"),
        ({"environment_override": Path("claude")}, "environment override"),
        ({"provider_binary": Path("claude")}, "provider binary"),
        ({"roster_default": Path("claude")}, "roster default"),
    ],
)
def test_relative_refusal_names_the_winning_source(kwargs, label):
    with pytest.raises(NoDeclaredBinary) as refusal:
        resolve_claude_binary(**kwargs)
    assert str(refusal.value) == (
        f"no declared claude binary: the {label} declaration claude is relative, "
        "so the child would resolve it against ambient PATH or cwd"
    )


def test_unbound_seam_message_is_pinned():
    with pytest.raises(NotImplementedError) as refusal:
        check_claude_version("/usr/bin/claude")
    message = str(refusal.value)
    assert message.startswith(
        "Spawn-d2 lane owns the probe; tests inject a callable to drive "
    )
    assert "See README.md, check_claude_version, for the wiring contract." in message


def test_installation_passes_the_exact_binary_path_to_the_probe(tmp_path):
    binary = _installation_binary(tmp_path)
    seen: list[str] = []

    def spy(path: str) -> Version:
        seen.append(path)
        return Version(2, 1, 241)

    inspect_claude_installation(binary, spy)
    assert seen == [str(binary)]


def test_installation_refusals_carry_path_and_detail(tmp_path):
    absent = tmp_path / "absent"
    with pytest.raises(BinaryIdentityError) as missing:
        inspect_claude_installation(absent, lambda _: Version(2, 1, 220))
    assert missing.value.path == str(absent)
    assert "No such file" in missing.value.detail

    with pytest.raises(BinaryIdentityError) as directory:
        inspect_claude_installation(tmp_path, lambda _: Version(2, 1, 220))
    assert directory.value.path == str(tmp_path)
    assert directory.value.detail == "is not a regular file"

    binary = _installation_binary(tmp_path)

    def replacing_probe(_: str) -> Version:
        with open(binary, "ab") as handle:
            handle.write(b"\n")
        return Version(2, 1, 220)

    with pytest.raises(BinaryIdentityError) as changed:
        inspect_claude_installation(binary, replacing_probe)
    assert changed.value.path == str(binary)
    assert changed.value.detail == "identity changed while the version was probed"


def test_config_refusal_wire_text_and_attributes(tmp_path):
    plugin_dir = _plugin(tmp_path)
    config_path = plugin_dir / ".mcp.json"
    _write_config(plugin_dir, {"mcpServers": {"other": {}}})
    with pytest.raises(ChildMcpConfigError) as refusal:
        inspect_child_mcp_config(plugin_dir)
    assert refusal.value.path == config_path
    assert refusal.value.reason == 'does not declare the required server "nmemory"'
    assert str(refusal.value) == (
        f"child MCP config at {config_path} is refused: {refusal.value.reason}"
    )


def test_config_refusal_reasons_are_exact(tmp_path):
    plugin_dir = _plugin(tmp_path)
    config_path = plugin_dir / ".mcp.json"

    with pytest.raises(ChildMcpConfigError) as absent:
        inspect_child_mcp_config(plugin_dir)
    assert absent.value.path == config_path
    assert absent.value.reason.startswith("cannot be inspected (")

    target = tmp_path / "real.json"
    target.write_text(json.dumps(_REAL_PLUGIN_SHAPE), encoding="utf-8")
    (plugin_dir / ".mcp.json").symlink_to(target)
    with pytest.raises(ChildMcpConfigError) as linked:
        inspect_child_mcp_config(plugin_dir)
    assert linked.value.path == config_path
    assert linked.value.reason == (
        "is a symlink, so what the child loads is not what this inspection read"
    )

    (plugin_dir / ".mcp.json").unlink()
    (plugin_dir / ".mcp.json").mkdir()
    with pytest.raises(ChildMcpConfigError) as directory:
        inspect_child_mcp_config(plugin_dir)
    assert directory.value.path == config_path
    assert directory.value.reason == "is not a regular file"
    (plugin_dir / ".mcp.json").rmdir()

    _write_config(plugin_dir, b'{"mcpServers": {"nmemory"')
    with pytest.raises(ChildMcpConfigError) as malformed:
        inspect_child_mcp_config(plugin_dir)
    assert malformed.value.path == config_path
    assert malformed.value.reason.startswith("is not parseable JSON (")

    _write_config(plugin_dir, [])
    with pytest.raises(ChildMcpConfigError) as not_object:
        inspect_child_mcp_config(plugin_dir)
    assert not_object.value.path == config_path
    assert not_object.value.reason == "is not a JSON object"

    _write_config(plugin_dir, {"other": {}})
    with pytest.raises(ChildMcpConfigError) as no_servers:
        inspect_child_mcp_config(plugin_dir)
    assert no_servers.value.path == config_path
    assert no_servers.value.reason == 'has no "mcpServers" object'

    _write_config(plugin_dir, {"mcpServers": {"nmemory": None}})
    with pytest.raises(ChildMcpConfigError) as declaration:
        inspect_child_mcp_config(plugin_dir)
    assert declaration.value.path == config_path
    assert declaration.value.reason == 'declares server "nmemory" as a non-object'

    _write_config(plugin_dir, {"mcpServers": {"nmemory": {"env": 3}}})
    with pytest.raises(ChildMcpConfigError) as env_shape:
        inspect_child_mcp_config(plugin_dir)
    assert env_shape.value.path == config_path
    assert env_shape.value.reason == 'declares a non-object env for server "nmemory"'

    _write_config(plugin_dir, {"mcpServers": {"nmemory": {"env": {"X": 7}}}})
    with pytest.raises(ChildMcpConfigError) as non_textual:
        inspect_child_mcp_config(plugin_dir)
    assert non_textual.value.path == config_path
    assert non_textual.value.reason == (
        'declares a non-textual env value for server "nmemory"'
    )

    _write_config(
        plugin_dir,
        {
            "mcpServers": {
                "nmemory": {},
                "nvision": {"env": {"NOTT_NVISION_STORE_KEY_HEX": "deadbeefcafebabe"}},
            }
        },
    )
    with pytest.raises(ChildMcpConfigError) as literal:
        inspect_child_mcp_config(plugin_dir)
    assert literal.value.path == config_path
    assert literal.value.reason == (
        'persists a literal value for the "nvision" env entry "NOTT_NVISION_STORE_KEY_HEX"; '
        "the child config must carry environment references, never credential bytes"
    )

    _write_config(plugin_dir, b'{"pad": "' + b"a" * (MAX_MCP_CONFIG_BYTES + 1) + b'"}')
    with pytest.raises(ChildMcpConfigError) as oversized:
        inspect_child_mcp_config(plugin_dir)
    assert oversized.value.path == config_path
    assert oversized.value.reason == (
        f"exceeds the {MAX_MCP_CONFIG_BYTES}-byte read cap"
    )

    _write_config(plugin_dir, _REAL_PLUGIN_SHAPE)

    def refusing_open(*args: object, **kwargs: object):
        raise OSError("device gone")

    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(builtins, "open", refusing_open)
        with pytest.raises(ChildMcpConfigError) as unreadable:
            inspect_child_mcp_config(plugin_dir)
    assert unreadable.value.path == config_path
    assert unreadable.value.reason.startswith("cannot be read (")


def test_config_at_exactly_the_cap_still_parses(tmp_path):
    plugin_dir = _plugin(tmp_path)
    document = '{"mcpServers": {"nmemory": {}}}'
    padded = document + " " * (MAX_MCP_CONFIG_BYTES - len(document))
    _write_config(plugin_dir, padded.encode())
    assert len(padded) == MAX_MCP_CONFIG_BYTES
    assert inspect_child_mcp_config(plugin_dir).server_names == ("nmemory",)


def test_mint_session_id_from_fixed_entropy_is_the_exact_uuid():
    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(builtins, "open", lambda *a, **k: io.BytesIO(b"\x00" * 16))
        assert mint_session_id() == "00000000-0000-4000-8000-000000000000"


def test_mint_session_id_short_read_message_is_exact():
    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(builtins, "open", lambda *a, **k: io.BytesIO(b"ab"))
        with pytest.raises(OSError) as refusal:
            mint_session_id()
        assert str(refusal.value) == "could not read 16 bytes from /dev/urandom"


def test_mint_session_id_from_nonuniform_entropy_is_the_exact_uuid():
    # Distinguishing entropy: byte 6 (0x06) and byte 8 (0x08) differ from
    # their neighbours, so a nibble mask or an off-by-one byte index in the
    # version/variant math changes the rendered uuid.
    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(builtins, "open", lambda *a, **k: io.BytesIO(bytes(range(16))))
        assert mint_session_id() == "00010203-0405-4607-8809-0a0b0c0d0e0f"


def test_the_child_mcp_config_reports_the_env_names_it_references(tmp_path: Path):
    # The child started with plugin:nott:nmemory FAILED because the variable
    # referenced by .mcp.json did not arrive: the sealed allowlist carries HOME,
    # TMPDIR and the XDGs, and nothing declared by plugin config. Inspection already
    # PROVES that every value is pure ${NAME}, so names can be safely enumerated —
    # passing exactly those, and no other, is the derived allowlist.
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    (plugin / ".mcp.json").write_text(
        '{"mcpServers": {"nmemory": {"command": "/bin/true",'
        ' "env": {"OPENAI_API_KEY": "${OPENAI_API_KEY}"}}}}'
    )
    config = inspect_child_mcp_config(plugin)
    assert config.env_names == ("OPENAI_API_KEY",)


def test_claude_child_mutant_kills():
    import dataclasses

    from nspawn.claude_child import ChildMcpConfig, Version

    # Mutant 0: Version.__str__
    v = Version(2, 1, 241)
    assert str(v) == "2.1.241"

    # Mutant 20: ChildMcpConfig frozen=True
    cfg = ChildMcpConfig(Path("/tmp"), ("srv",), ())
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.plugin_dir = Path("/other")  # type: ignore[misc]
