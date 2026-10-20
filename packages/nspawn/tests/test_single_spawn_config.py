"""One file is the store/roster boundary; the old indirection is refused."""

import tomllib

import pytest
from workspace_root import WORKSPACE_ROOT, workspace_path

from nspawn import config, supervisor
from nspawn.owner_roster import Roster


def isolate(monkeypatch, path):
    for name in (
        "NOTT_NSPAWN_ROSTER",
        "NOTT_OTLP_ENDPOINT",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NOTT_NSPAWN_CONFIG", str(path))


def test_single_file_consumer_boundary(monkeypatch):
    root = WORKSPACE_ROOT
    path = workspace_path("templates/nott/spawn.toml")
    isolate(monkeypatch, path)
    assert path.is_file(), "the single versioned spawn.toml is absent"
    assert not (root / ".nott/providers.toml").exists()
    document = tomllib.loads(path.read_text())
    assert {"defaults", "provider"} <= document.keys()
    assert config.store_config_path() == Roster.default_path() == path
    direct = Roster.read(path)
    served = supervisor._read_roster(str(root))
    assert served.path == path
    assert served.providers == direct.providers
    assert served.defaults == direct.defaults
    assert served.providers


def test_the_versioned_config_names_no_store() -> None:
    """The roster this repository VERSIONS names no store.

    It is committed and read by anyone who points `NOTT_NSPAWN_CONFIG` at a
    checkout; the registry is a file under `NOTT_DATABASE_DIR`, so a store
    coordinate here could only mislead.
    """
    document = tomllib.loads((workspace_path("templates/nott/spawn.toml")).read_text())
    assert "url" not in document.get("store", {})


@pytest.mark.parametrize("reader", ["store", "roster", "supervisor"])
def test_store_roster_is_retired(tmp_path, monkeypatch, reader):
    path = tmp_path / "spawn.toml"
    path.write_text('[store]\nroster = "providers.toml"\n')
    isolate(monkeypatch, path)
    with pytest.raises(config.ConfigError) as caught:
        if reader == "store":
            config.resolve_roster_path()
        elif reader == "roster":
            Roster.read(path)
        else:
            supervisor._read_roster()
    assert "[store].roster" in str(caught.value)
    assert str(path) in str(caught.value)
    assert "single config file" in str(caught.value)


def test_compatibility_never_overrides_the_single_file(tmp_path, monkeypatch):
    path = tmp_path / "spawn.toml"
    path.write_text('[[provider]]\nname = "single"\n')
    compatibility = tmp_path / "compat.toml"
    compatibility.write_text('[[provider]]\nname = "compat"\n')
    isolate(monkeypatch, path)
    monkeypatch.setenv("NOTT_NSPAWN_ROSTER", str(compatibility))
    assert supervisor._read_roster().providers[0].name == "single"
    path.unlink()
    assert supervisor._read_roster().providers[0].name == "compat"
