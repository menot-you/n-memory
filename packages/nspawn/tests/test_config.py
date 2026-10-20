"""The spawn.toml tables nSPAWN reads parse fail-closed."""

from pathlib import Path

from nspawn import config
from nspawn.config import resolve_run_root

# --- the store: the env names the PATH, the file carries the value ------------


def test_a_retired_store_url_in_the_file_is_ignored_not_refused(tmp_path, monkeypatch):
    """The registry is a file under `NOTT_DATABASE_DIR` now; an old `url` key stays inert."""
    cfg = tmp_path / "spawn.toml"
    cfg.write_text('[store]\nurl = "the retired registry"\nrun_root = "/tmp/runs"\n')
    monkeypatch.setenv("NOTT_NSPAWN_CONFIG", str(cfg))
    monkeypatch.delenv("NOTT_NSPAWN_RUN_ROOT", raising=False)
    assert config.resolve_roster_path() == cfg
    assert resolve_run_root() == Path("/tmp/runs")


def test_the_run_root_comes_from_the_file_and_defaults_without_one(
    tmp_path, monkeypatch
):
    # The file wins, and without a file there is a default
    # under ~/.nott/state — there is no reason for an operator to declare where runs
    # live for the server to even accept a call.
    cfg = tmp_path / "spawn.toml"
    cfg.write_text('[store]\nrun_root = "/tmp/runs-do-arquivo"\n')
    monkeypatch.setenv("NOTT_NSPAWN_CONFIG", str(cfg))
    monkeypatch.delenv("NOTT_NSPAWN_RUN_ROOT", raising=False)
    assert resolve_run_root() == Path("/tmp/runs-do-arquivo")

    monkeypatch.setenv("NOTT_NSPAWN_CONFIG", str(tmp_path / "ausente.toml"))
    assert resolve_run_root() == Path.home() / ".nott" / "state" / "nspawn" / "runs"
