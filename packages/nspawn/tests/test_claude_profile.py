"""Declared Claude directories preserve the host home and recorded routes."""

from pathlib import Path

import pytest
import test_route
from test_route_transition import rejection

from nspawn.child import build_child_env
from nspawn.owner_roster import NativeAccount, Provider, Roster, RosterError
from nspawn.registry import Registry, create_store
from nspawn.route import RouteRefused, claude_route, route_list, route_switch

root = test_route.root


@pytest.mark.parametrize("alias", ["alpha", "beta"])
def test_child_selects_config_without_changing_home(alias: str, tmp_path: Path) -> None:
    for name in ("alpha", "beta"):
        (tmp_path / f"{name}.token").write_text("fixture-token-profile")
    provider = Provider(
        name="claude",
        secrets=tmp_path,
        accounts={
            name: NativeAccount(
                home=tmp_path / name, auth_env="CLAUDE_CODE_OAUTH_TOKEN"
            )
            for name in ("alpha", "beta")
        },
    )
    ambient = {
        "HOME": str(tmp_path / "host"),
        "CLAUDE_CODE_OAUTH_TOKEN": "fixture-token-profile",
    }
    env, _ = build_child_env(provider, alias, ambient=ambient)
    assert env["HOME"] == ambient["HOME"]
    assert env.get("CLAUDE_CONFIG_DIR") == str(tmp_path / alias)
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == ambient["CLAUDE_CODE_OAUTH_TOKEN"]


def test_profile_routes_agree(root: Path, tmp_path: Path) -> None:
    from nspawn.route import read_roster

    roster = read_roster()
    provider = roster.providers[0]
    provider.accounts = {
        alias: NativeAccount(home=root / alias) for alias in ("alpha", "beta")
    }
    create_store(tmp_path / "routes.db")
    with Registry(tmp_path / "routes.db", "profile-test") as registry:
        registry.upgrade()
        listed = route_list(
            roster, registry, model="claude-opus-5", effort="high", now=1000
        )
        for row in listed["routes"]:
            expected = f"{row['alias']}@{root / row['alias']}"
            assert row["route"] == expected
            recorded, env, _ = claude_route(roster, expected)
            assert recorded == expected
            assert env["CLAUDE_CONFIG_DIR"] == str(root / row["alias"])
            assert "HOME" not in env
        switched = route_switch(
            roster, registry, listed["routes"][0]["route"], rejection(), now=1000
        )
        assert switched["routes"][1]["route"] == f"beta@{root / 'beta'}"


def test_duplicate_declared_config_is_refused(tmp_path: Path) -> None:
    config = tmp_path / "spawn.toml"
    config.write_text(
        f'[[provider]]\nname="claude"\n'
        f'[provider.accounts.alpha]\nhome="{tmp_path / "profile"}"\n'
        f'[provider.accounts.beta]\nhome="{tmp_path / "profile" / ".." / "profile"}"\n'
    )
    with pytest.raises(RosterError, match="duplicate Claude config directory"):
        Roster.read(config)


def test_changed_config_refuses_recorded_alias(root: Path) -> None:
    from nspawn.route import read_roster

    roster = read_roster()
    roster.providers[0].accounts = {"alpha": NativeAccount(home=root / "new-profile")}
    with pytest.raises(
        RouteRefused, match="Claude config directory changed for account alpha"
    ):
        claude_route(roster, f"alpha@{root / 'old-profile'}")


def test_external_discovery_reads_declared_config_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading
    from types import SimpleNamespace

    from nspawn import supervisor

    inherited = tmp_path / "inherited"
    inherited.mkdir()
    declared = tmp_path / "declared"
    declared.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(inherited))
    roster = Roster(
        providers=[
            Provider(name="claude", accounts={"alpha": NativeAccount(home=declared)})
        ]
    )
    monkeypatch.setattr(supervisor, "_read_roster", lambda: roster)
    seen: list[Path] = []

    def sessions(
        _operation: str, home: Path, _arguments: list[str]
    ) -> list[dict[str, object]]:
        seen.append(home)
        return [{"threads": []}]

    owner = SimpleNamespace(
        _lock=threading.RLock(),
        _registry=SimpleNamespace(known_threads=lambda ids: set()),
        _claude_sessions=sessions,
        _thread_read=lambda call, *_: call("fixture"),
    )
    supervisor.Supervisor.threads(owner, 50)  # type: ignore[arg-type]
    assert set(seen) == {inherited, declared}


def test_sdk_admission_retains_private_config_with_declared_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hashlib

    from test_profile_launch import boundary_profile

    from nspawn import route
    from nspawn.admission import PrelaunchProfileV1

    prepared, _ = boundary_profile(tmp_path)
    raw = prepared.data()
    private = raw["constructor"]["options"]["env"]["CLAUDE_CONFIG_DIR"]
    declared = tmp_path / "account-config"
    declared.mkdir()
    (tmp_path / "alpha.token").write_text("fixture-profile-token")
    roster = Roster(
        providers=[
            Provider(
                name="claude",
                secrets=tmp_path,
                accounts={"alpha": NativeAccount(home=declared)},
            )
        ]
    )
    raw["route_ref"] = f"alpha@{declared}"
    for name in ("consumer", "sdk"):
        entry = tmp_path / (name + ".mjs")
        entry.write_text("export {};\n")
        raw["runtime"][name] = {
            "path": str(entry),
            "digest": "sha256:" + hashlib.sha256(entry.read_bytes()).hexdigest(),
        }
    raw["runtime"]["files"] = [
        raw["runtime"][name]
        for name in ("launcher", "node", "consumer", "sdk", "sdk_package", "native")
    ]
    prepared = PrelaunchProfileV1(raw)
    staged = tmp_path / "run" / "prelaunch.json"
    staged.write_bytes(prepared.encoded)
    staged.chmod(0o600)
    monkeypatch.setattr(route, "read_roster", lambda: roster)
    with route.profile_prepare(
        "claude",
        raw["route_ref"],
        path=staged,
        digest=prepared.digest,
        grant_root=staged.parent,
    ) as admitted:
        assert admitted.env["CLAUDE_CONFIG_DIR"] == private
        assert admitted.env["CLAUDE_CONFIG_DIR"] != str(declared)
