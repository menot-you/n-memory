"""Installed skill access must not grant an account home or arbitrary cache."""

import json
import os
import sys
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from supervisor_support import _admit_pending, _stop_every_reaper, _sv, registry

from nspawn.native import NativePreparationError, prepare_native
from nspawn.owner_roster import Defaults, NativeAccount, Provider, Roster

__all__ = ["_stop_every_reaper", "registry"]


@pytest.fixture
def installed(tmp_path):
    home = tmp_path / "account"
    home.mkdir(mode=0o700)
    auth = home / "auth.json"
    auth.write_text(json.dumps({"tokens": {"access_token": "synthetic-oauth-token"}}))
    auth.chmod(0o600)
    (home / "config.toml").write_text('[plugins."fixture@local"]\nenabled=true\n')
    source = tmp_path / "source"
    cache = home / "plugins/cache/local/fixture/v1"
    manifest = json.dumps({"name": "fixture", "version": "v1", "skills": "./skills/"})
    for root in (source, cache):
        (root / ".codex-plugin").mkdir(parents=True)
        (root / ".codex-plugin/plugin.json").write_text(manifest)
        (root / "skills/readback/references").mkdir(parents=True)
        (root / "skills/readback/SKILL.md").write_text("Read the request.\n")
        (root / "skills/readback/references/rule.md").write_text("Do not edit.\n")
    (source / ".mcp.json").write_text(
        '{"mcpServers": {"nmemory": {"command": "/fixture/nmemory"}}}'
    )
    binary = tmp_path / "native-cli"
    binary.write_text("#!/bin/sh\nexit 99\n")
    binary.chmod(0o700)
    cwd = tmp_path / "work"
    cwd.mkdir()
    provider = Provider(
        name="codex",
        kind="codex",
        bin=binary,
        model="fixture",
        accounts={"one": NativeAccount(home)},
    )
    return provider, Roster(defaults=Defaults(plugin_root=source)), cwd, cache


def launch(installed, **kwargs):
    provider, roster, cwd, _ = installed
    return prepare_native(
        provider, "one", roster=roster, cwd=cwd, read_only=True, ambient={}, **kwargs
    )


def test_installed_skills_are_readable_without_explicit_data_grants(installed):
    provider, _, cwd, cache = installed
    granted = launch(installed).context.read_roots
    assert set(granted) == {str(cwd), str(provider.bin), str(cache / "skills")}
    assert not any(
        provider.accounts["one"].home.is_relative_to(root) for root in granted
    )
    assert (cache / "skills/readback/references/rule.md").is_relative_to(
        cache / "skills"
    )


def test_installed_skills_preserve_explicit_data_grants(installed, tmp_path):
    data = tmp_path / "extra-data"
    data.mkdir()
    assert str(data) in launch(installed, read_paths=[str(data)]).context.read_roots


@pytest.mark.parametrize(
    "fault",
    ["missing", "manifest", "skills-link", "cache-link", "writable", "ambiguous"],
)
def test_installed_skill_identity_refuses_unsafe_or_ambiguous_sources(
    installed, fault, tmp_path
):
    provider, _, _, cache = installed
    if fault == "missing":
        cache.rename(cache.with_name("old-version"))
    elif fault == "manifest":
        (cache / ".codex-plugin/plugin.json").write_text('{"name": "other"}')
    elif fault == "skills-link":
        (cache / "skills").rename(cache / "hidden-skills")
        (cache / "skills").symlink_to(provider.accounts["one"].home)
    elif fault == "cache-link":
        outside = tmp_path / "outside-cache"
        cache.rename(outside)
        cache.symlink_to(outside)
    elif fault == "writable":
        cache.parent.chmod(0o777)
    elif fault == "ambiguous":
        (provider.accounts["one"].home / "config.toml").write_text(
            '[plugins."fixture@local"]\nenabled=true\n[plugins."fixture@other"]\nenabled=true\n'
        )
    with pytest.raises(NativePreparationError, match="plugin"):
        launch(installed)


def test_disabled_plugin_does_not_gain_automatic_reads(installed):
    provider, _, _, cache = installed
    (provider.accounts["one"].home / "config.toml").write_text(
        '[plugins."fixture@local"]\nenabled=false\n'
    )
    assert str(cache / "skills") not in launch(installed).context.read_roots


def test_selected_plugin_keeps_native_implicit_enabled_default(installed):
    provider, _, _, cache = installed
    (provider.accounts["one"].home / "config.toml").write_text(
        '[plugins."fixture@local"]\n'
    )
    assert str(cache / "skills") in launch(installed).context.read_roots


@pytest.mark.parametrize("absent", ["manifest", "config"])
def test_absent_plugin_inputs_do_not_invent_a_read_grant(installed, absent):
    from nspawn.native import _codex_skill_roots

    provider, roster, _, _ = installed
    account = provider.accounts["one"]
    path = (
        roster.defaults.plugin_root / ".codex-plugin/plugin.json"
        if absent == "manifest"
        else account.home / "config.toml"
    )
    path.unlink()
    assert _codex_skill_roots(roster, account) == {}


def test_private_foreign_owned_cache_ancestor_refuses(installed, monkeypatch):
    assert launch(installed).plugin_skill_roots
    ancestor = installed[3].parent
    original = Path.lstat

    def foreign_owner(path, *args, **kwargs):
        metadata = original(path, *args, **kwargs)
        if path == ancestor:
            fields = list(metadata)
            fields[4] += 1  # Only the observed st_uid differs; mode stays private.
            return os.stat_result(fields)
        return metadata

    monkeypatch.setattr(Path, "lstat", foreign_owner)
    with pytest.raises(NativePreparationError, match="source/owner"):
        launch(installed)


@pytest.mark.parametrize("version", [".", "..", "", "v\n1", "v\\1", "X"])
def test_version_component_validation_precedes_an_existing_cache(installed, version):
    from nspawn.native import _codex_skill_roots

    provider, roster, _, cache = installed
    selected = cache.parent / version
    manifest = json.dumps({"name": "fixture", "version": version, "skills": "skills"})
    for root in (roster.defaults.plugin_root, selected):
        (root / ".codex-plugin").mkdir(parents=True, exist_ok=True)
        (root / ".codex-plugin/plugin.json").write_text(manifest)
        (root / "skills").mkdir(exist_ok=True)
    if version == "X":
        assert _codex_skill_roots(roster, provider.accounts["one"])["fixture@local"][
            0
        ] == str(selected / "skills")
    else:
        with pytest.raises(NativePreparationError, match="plugin"):
            _codex_skill_roots(roster, provider.accounts["one"])


def test_malformed_market_identity_cannot_become_an_unselected_plugin(installed):
    provider, _, _, _ = installed
    (provider.accounts["one"].home / "config.toml").write_text(
        '[plugins."fixture@local@other"]\nenabled=true\n'
    )
    with pytest.raises(NativePreparationError, match="plugin"):
        launch(installed)


@pytest.mark.parametrize("relative", [".", "../v1/skills", "bounded", "oversized"])
def test_skill_subtree_shape_is_validated_before_existing_content(installed, relative):
    from nspawn.native import _codex_skill_roots

    provider, roster, _, cache = installed
    if relative in {"bounded", "oversized"}:
        path = "a" * 255 + "/" + "b" * 255
        path = path + "/" if relative == "bounded" else "./" + path
        assert len(path) == (512 if relative == "bounded" else 513)
    else:
        path = relative
    manifest = json.dumps({"name": "fixture", "version": "v1", "skills": path})
    for root in (roster.defaults.plugin_root, cache):
        (root / ".codex-plugin/plugin.json").write_text(manifest)
        (root / path).mkdir(parents=True, exist_ok=True)
    if relative == "bounded":
        assert _codex_skill_roots(roster, provider.accounts["one"])["fixture@local"][
            0
        ] == str((cache / path).resolve())
    else:
        with pytest.raises(NativePreparationError, match="plugin"):
            _codex_skill_roots(roster, provider.accounts["one"])


@pytest.mark.parametrize(
    "fault",
    [
        "document",
        "plugins",
        "policy",
        "enabled",
        "version",
        "version-type",
        "skills-type",
        "skills-text",
        "skills-path",
    ],
)
def test_malformed_installed_skill_declarations_refuse(installed, fault):
    provider, roster, _, _ = installed
    manifest = roster.defaults.plugin_root / ".codex-plugin/plugin.json"
    config = provider.accounts["one"].home / "config.toml"
    if fault == "document":
        manifest.write_text("[]")
    elif fault in {"plugins", "policy", "enabled"}:
        config.write_text(
            {
                "plugins": "plugins=[]\n",
                "policy": '[plugins]\n"fixture@local"="yes"\n',
                "enabled": '[plugins."fixture@local"]\nenabled="yes"\n',
            }[fault]
        )
    else:
        declaration = json.loads(manifest.read_text())
        key, value = {
            "version": ("version", "../other"),
            "version-type": ("version", ["v1"]),
            "skills-type": ("skills", []),
            "skills-text": ("skills", "\n"),
            "skills-path": ("skills", "/outside"),
        }[fault]
        declaration[key] = value
        manifest.write_text(json.dumps(declaration))
    with pytest.raises(NativePreparationError, match="plugin"):
        launch(installed)


def test_unselected_plugin_and_writable_turn_do_not_gain_roots(installed):
    provider, roster, cwd, _ = installed
    (provider.accounts["one"].home / "config.toml").write_text("")
    assert launch(installed).plugin_skill_roots == {}
    prepared = prepare_native(
        provider, "one", roster=roster, cwd=cwd, read_only=False, ambient={}
    )
    assert prepared.plugin_skill_roots == {}
    assert prepared.context.read_roots is None


@pytest.mark.parametrize(
    "damage",
    [None, "changed-content", "wrong-source", "missing-cache", "cache-symlink"],
)
def test_native_source_advertisement_matches_only_its_installed_copy(
    installed, tmp_path, damage
):
    from nspawn.app_server import PluginSkillsRefused, _verify_plugin_skills

    _, roster, _, cache = installed
    source = roster.defaults.plugin_root / "skills"
    path = source / "readback/SKILL.md"
    if damage == "changed-content":
        path.write_text("Different uninstalled source content.\n")
    elif damage == "wrong-source":
        other = tmp_path / "other-source"
        other.mkdir()
        path = other / "SKILL.md"
        path.write_text((cache / "skills/readback/SKILL.md").read_text())
    elif damage == "missing-cache":
        (cache / "skills/readback/SKILL.md").unlink()
    elif damage == "cache-symlink":
        copied = cache / "skills/readback/SKILL.md"
        copied.unlink()
        copied.symlink_to(path)
    body = {
        "summary": {"enabled": True, "installed": True},
        "skills": [{"enabled": True, "path": str(path)}],
    }
    if damage is None:
        _verify_plugin_skills(body, str(cache / "skills"), str(source))
    else:
        with pytest.raises(PluginSkillsRefused):
            _verify_plugin_skills(body, str(cache / "skills"), str(source))


def test_automatic_skill_root_refuses_overflow_without_dropping_data(
    installed, tmp_path
):
    paths = [tmp_path / f"data-{number}" for number in range(8)]
    for path in paths:
        path.mkdir()
    with pytest.raises(NativePreparationError, match="read roots"):
        launch(installed, read_paths=[str(path) for path in paths])


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "old-version",
        "symlink",
        "missing",
        "relative",
        "directory",
        "writable-file",
        "all-disabled",
        "disabled",
        "uninstalled",
        "entry",
        "path-type",
    ],
)
def test_native_catalog_must_match_the_already_admitted_skills(
    installed, tmp_path, fault
):
    from nspawn.app_server import PluginSkillsRefused, _verify_plugin_skills

    cache = installed[3]
    root = cache / "skills"
    path = root / "readback/SKILL.md"
    summary = {"enabled": True, "installed": True}
    if fault == "old-version":
        path = tmp_path / "old-SKILL.md"
        path.write_text("An old cached skill.\n")
    elif fault == "symlink":
        outside = tmp_path / "unrelated-secret"
        outside.write_text("Synthetic unrelated data.\n")
        path = root / "escape.md"
        path.symlink_to(outside)
    elif fault == "missing":
        path = root / "absent.md"
    elif fault == "relative":
        path = Path("SKILL.md")
    elif fault == "directory":
        path = root
    elif fault == "writable-file":
        path.chmod(0o666)
    elif fault == "disabled":
        summary["enabled"] = False
    elif fault == "uninstalled":
        summary["installed"] = False
    body = {
        "summary": summary,
        "skills": [{"enabled": fault != "all-disabled", "path": str(path)}],
    }
    if fault == "entry":
        body["skills"] = [False]
    elif fault == "path-type":
        body["skills"][0]["path"] = None
    if fault is None:
        _verify_plugin_skills(body, str(root))
    else:
        with pytest.raises(PluginSkillsRefused, match="plugin"):
            _verify_plugin_skills(body, str(root))


@pytest.mark.parametrize("unsafe_tail", [False, True])
def test_disabled_catalog_entry_does_not_skip_later_enabled_entries(
    installed, tmp_path, unsafe_tail
):
    from nspawn.app_server import PluginSkillsRefused, _verify_plugin_skills

    root = installed[3] / "skills"
    valid = {"enabled": True, "path": str(root / "readback/SKILL.md")}
    outside = tmp_path / "unadmitted.md"
    outside.write_text("Synthetic unadmitted skill.\n")
    body = {
        "summary": {"enabled": True, "installed": True},
        "skills": [
            valid,
            {"enabled": False},
            {"enabled": True, "path": str(outside)} if unsafe_tail else valid,
        ],
    }
    if unsafe_tail:
        with pytest.raises(PluginSkillsRefused):
            _verify_plugin_skills(body, str(root))
    else:
        _verify_plugin_skills(body, str(root))
        body["skills"] = [{"enabled": False}, valid]
        _verify_plugin_skills(body, str(root))


@pytest.mark.parametrize("count", [0, 1024, 1025])
def test_native_catalog_size_accepts_the_limit_and_refuses_excess(installed, count):
    from nspawn.app_server import PluginSkillsRefused, _verify_plugin_skills

    root = installed[3] / "skills"
    body = {
        "summary": {"enabled": True, "installed": True},
        "skills": [{"enabled": True, "path": str(root / "readback/SKILL.md")}] * count,
    }
    if count == 1024:
        _verify_plugin_skills(body, str(root))
    else:
        with pytest.raises(PluginSkillsRefused):
            _verify_plugin_skills(body, str(root))


@pytest.mark.parametrize(
    "advertised, reason",
    [
        ("current", None),
        ("source", None),
        ("policy-only", None),
        ("old", "native advertised plugin skills differ from the admitted version"),
        ("missing", "native admitted skill plugin is not enabled/installed"),
        ("disabled", "native admitted plugin is not enabled"),
        ("uninstalled", "native admitted plugin is not installed"),
        ("skills-type", "native admitted plugin skills are not a list"),
        ("skills-empty", "native admitted plugin skill count is outside its limit"),
        ("skill-entry", "native admitted plugin skill is malformed"),
        ("skill-path", "native admitted plugin skill path is invalid"),
        ("skills-disabled", "native admitted plugin has no enabled skill paths"),
    ],
)
def test_native_metadata_operation_checks_persisted_grant_before_a_turn(
    installed, tmp_path, advertised, reason, registry
):
    from nspawn.app_server import AppServerTurn, PluginSkillsRefused
    from nspawn.redact import SecretNeedles

    prepared = launch(installed)
    granted = prepared.context.read_roots
    root = installed[3] / "skills"
    old = tmp_path / "old-version"
    old.mkdir()
    (old / "SKILL.md").write_text("Old content.\n")
    body = {
        "summary": {"id": "fixture@local", "enabled": True, "installed": True},
        "skills": [
            {
                "enabled": True,
                "path": str(
                    old / "SKILL.md"
                    if advertised == "old"
                    else root / "readback/SKILL.md"
                ),
            }
        ],
        "mcpServers": ["nmemory"],
    }
    if advertised == "source":
        body["skills"][0]["path"] = str(
            installed[1].defaults.plugin_root / "skills/readback/SKILL.md"
        )
    elif advertised == "disabled":
        body["summary"]["enabled"] = False
    elif advertised == "uninstalled":
        body["summary"].pop("installed")
    elif advertised == "skills-type":
        body["skills"] = "private-catalog-marker"
    elif advertised == "skills-empty":
        body["skills"] = []
    elif advertised == "skill-entry":
        body["skills"] = [{"enabled": "private-catalog-marker"}]
    elif advertised == "skill-path":
        body["skills"][0]["path"] = "private-catalog-marker\n"
    elif advertised == "skills-disabled":
        body["skills"][0]["enabled"] = False
    body["privateFixture"] = "private-catalog-marker"
    script = r"""
import json, sys
def read(): return json.loads(sys.stdin.readline())
def send(value): print(json.dumps(value), flush=True)
request = read()
assert request['method'] == 'initialize'
send({'id': request['id'], 'result': {'userAgent': 'fixture/1'}})
assert read()['method'] == 'initialized'
request = read()
assert request['method'] == 'plugin/installed'
plugins = [] if sys.argv[2] == 'missing' else [{'id': 'fixture@local', 'name': 'fixture', 'enabled': True, 'installed': True}]
send({'id': request['id'], 'result': {'marketplaces': [{'name': 'local', 'path': None, 'plugins': plugins}]}})
if plugins:
    request = read()
    assert request['method'] == 'plugin/read'
    send({'id': request['id'], 'result': {'plugin': json.loads(sys.argv[1])}})
assert not sys.stdin.readline(), 'metadata must not start a thread or model turn'
"""
    output = tmp_path / "metadata"
    output.mkdir()
    with AppServerTurn.start(
        [sys.executable, "-u", "-c", script, json.dumps(body), advertised],
        cwd=installed[2],
        env={"PATH": os.environ["PATH"]},
        run_dir=output,
        needles=SecretNeedles.none(),
    ) as session:
        if advertised in {"current", "source", "policy-only"}:
            policies = session.readonly_mcp_policies(
                timeout=3,
                expected_skill_roots=prepared.plugin_skill_roots
                if advertised in {"current", "source"}
                else None,
            )
            assert policies == {
                "fixture@local": {"mcp_servers": {"nmemory": {"enabled": False}}}
            }
        else:
            with pytest.raises(PluginSkillsRefused, match="plugin") as refused:
                session.readonly_mcp_policies(
                    timeout=3, expected_skill_roots=prepared.plugin_skill_roots
                )
            # A grant the vendor does not hold is the same on every attempt: it
            # carries its own code, and its text never tells the caller to retry.
            assert refused.value.entry.code == 1126
            assert "retry" not in str(refused.value)
            sv = _sv(registry, tmp_path)
            run_id, _ = _admit_pending(
                sv, registry, tmp_path, worktree=str(installed[2]), claim=False
            )
            sv._fail_run(run_id, refused.value)
            expected = f"PluginSkillsRefused (nERR-1126): {reason}"
            assert registry.get_run(run_id).blocked_reason == expected
            assert sv.status(run_id).failure_reason == expected
            assert sv.result(run_id).failure_reason == expected
            sv.close(run_id)
            assert sv.result(run_id).failure_reason == expected
            assert "private-catalog-marker" not in repr(registry.get_run(run_id))
    assert prepared.context.read_roots == granted
    for stream in ("stdout", "stderr"):
        retained = (output / stream).read_text()
        assert "private-catalog-marker" not in retained
        assert str(root) not in retained
        assert str(old) not in retained


@pytest.mark.parametrize(
    "detail",
    [
        "private-credential-marker /private/account/home",
        "native admitted skill plugin is not enabled/installed",
    ],
)
def test_unknown_plugin_refusal_detail_stays_private(registry, tmp_path, detail):
    from nspawn.app_server import PluginSkillsRefused

    sv = _sv(registry, tmp_path)
    run_id, _ = _admit_pending(
        sv, registry, tmp_path, worktree=str(tmp_path), claim=False
    )
    sv._fail_run(run_id, PluginSkillsRefused(detail))
    expected = "PluginSkillsRefused (nERR-1126)"
    assert registry.get_run(run_id).blocked_reason == expected
    assert sv.status(run_id).failure_reason == expected
    assert sv.result(run_id).failure_reason == expected


@settings(max_examples=40, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    name=st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=1, max_size=20),
    location=st.sampled_from(["admitted", "outside", "parent", "relative"]),
)
def test_generated_skill_paths_never_extend_the_admitted_root(
    installed, name, location
):
    from nspawn.app_server import PluginSkillsRefused, _verify_plugin_skills

    cache = installed[3]
    root = cache / "skills"
    target = (root if location == "admitted" else cache) / f"{name}.md"
    target.write_text("Synthetic installed skill.\n")
    if location == "parent":
        advertised = root / ".." / target.name
    elif location == "relative":
        advertised = Path(target.name)
    else:
        advertised = target
    body = {
        "summary": {"enabled": True, "installed": True},
        "skills": [{"enabled": True, "path": str(advertised)}],
    }
    if location == "admitted":
        _verify_plugin_skills(body, str(root))
    else:
        with pytest.raises(PluginSkillsRefused):
            _verify_plugin_skills(body, str(root))


def test_native_source_behind_a_live_symlink_still_matches_its_installed_copy(
    installed, tmp_path
):
    """The installer stages a versioned tree and repoints a live link at it.

    `plugin/read` advertises the RESOLVED file, so a declared source that is
    itself the link never matched lexically -- and every codex-kind dispatch
    refused with the advertisement mismatch.
    """
    from nspawn.app_server import PluginSkillsRefused, _verify_plugin_skills

    _, roster, _, cache = installed
    real = roster.defaults.plugin_root
    link = tmp_path / "live"
    link.symlink_to(real, target_is_directory=True)

    body = {
        "summary": {"enabled": True, "installed": True},
        "skills": [{"enabled": True, "path": str(real / "skills/readback/SKILL.md")}],
    }
    # The declared source is the LINK; the advertised path is the real tree.
    _verify_plugin_skills(body, str(cache / "skills"), str(link / "skills"))

    # The tolerance is the link alone: a file outside the declared tree stays refused.
    outside = tmp_path / "outside"
    outside.mkdir()
    stray = outside / "SKILL.md"
    stray.write_text((cache / "skills/readback/SKILL.md").read_text())
    with pytest.raises(PluginSkillsRefused):
        _verify_plugin_skills(
            {
                "summary": {"enabled": True, "installed": True},
                "skills": [{"enabled": True, "path": str(stray)}],
            },
            str(cache / "skills"),
            str(link / "skills"),
        )


def test_codex_skills_come_from_the_client_subtree_the_marketplace_installs(installed):
    """The owner's bundle ships one skill tree PER CLIENT, and codex gets its own.

    Measured 2026-09-20 on the live host: the personal marketplace declares the
    plugin source as `./plugins/nott/clients/codex`, so `plugin/read` advertises
    `<bundle>/clients/codex/skills/<name>/SKILL.md`. nSPAWN read the manifest at
    the BUNDLE root instead, whose own `./skills/` is the claude tree, and every
    advertised path then failed `relative_to` the declared source -- which is
    the nERR-2003 that refused every `nspawn_review_start` on codex.
    """
    _provider, roster, _cwd, _cache = installed
    source = roster.defaults.plugin_root
    client = source / "clients/codex"
    (client / ".codex-plugin").mkdir(parents=True)
    (client / ".codex-plugin/plugin.json").write_text(
        (source / ".codex-plugin/plugin.json").read_text()
    )
    (client / "skills/readback").mkdir(parents=True)
    (client / "skills/readback/SKILL.md").write_text("Read the request.\n")

    roots = launch(installed).plugin_skill_roots
    assert roots["fixture@local"][1] == str(client / "skills")
