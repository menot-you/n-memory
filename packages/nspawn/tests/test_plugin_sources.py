"""`plugins` carries directories AND GitHub repositories.

Owner ruling 2026-08-29: an entry spelled `gh:org/repo` becomes a
`--plugin-url` pointing at that repository's zip; anything else stays a
`--plugin-dir`. The vendor flag exists and is repeatable — measured against
`claude --help`: "Fetch a plugin .zip from a URL for this session only".
"""

from __future__ import annotations

from pathlib import Path

import pytest
import sample_roster

from nspawn import config as config_module
from nspawn.errors import NSpawnError
from nspawn.owner_roster import Roster


def roster_with(plugins: list[str], tmp_path: Path) -> Roster:
    body = "[defaults]\nplugins = [" + ", ".join(f'"{p}"' for p in plugins) + "]\n"
    path = tmp_path / "spawn.toml"
    path.write_text(body)
    return Roster.read(path)


def test_a_gh_entry_becomes_a_plugin_url(tmp_path: Path) -> None:
    args = roster_with(["gh:example-org/example-plugin"], tmp_path).resolve(None).args
    assert "--plugin-url" in args
    url = args[args.index("--plugin-url") + 1]
    assert url.startswith("https://github.com/example-org/example-plugin")
    assert url.endswith(".zip")
    assert "--plugin-dir" not in args


def test_a_path_entry_stays_a_plugin_dir(tmp_path: Path) -> None:
    args = roster_with(["~/.nott/plugin"], tmp_path).resolve(None).args
    assert "--plugin-dir" in args
    assert "--plugin-url" not in args
    assert not args[args.index("--plugin-dir") + 1].startswith("~")


def test_the_two_forms_mix_and_keep_their_order(tmp_path: Path) -> None:
    args = (
        roster_with(
            ["~/.nott/plugin", "gh:example-org/example-plugin", "/opt/other"], tmp_path
        )
        .resolve(None)
        .args
    )
    flags = [a for a in args if a.startswith("--plugin-")]
    assert flags == ["--plugin-dir", "--plugin-url", "--plugin-dir"]


def test_a_gh_entry_may_pin_a_ref(tmp_path: Path) -> None:
    """`gh:org/repo@ref` pins the ref; without one, the default branch."""
    args = (
        roster_with(["gh:example-org/example-plugin@v2"], tmp_path).resolve(None).args
    )
    url = args[args.index("--plugin-url") + 1]
    assert "/v2.zip" in url


def test_a_malformed_gh_entry_is_refused_by_name(tmp_path: Path) -> None:
    """`gh:` with no `org/repo` cannot become a URL, and must not be guessed."""
    from nspawn.owner_roster import RosterError

    with pytest.raises(RosterError):
        roster_with(["gh:nott-a-repo"], tmp_path).resolve(None)


def test_the_file_outranks_the_env_and_neither_ever_consults_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole resolution law in one test: file, then env, never the tree.

    L3 (owner ruling 2026-08-29) deleted the project-tree walk and the silent
    `~/.nott/spawn.toml` fallback, so `cwd` is passed here on EVERY call
    and must change nothing — a roster the operator did not choose is the
    defect this asserts against. `.nott/spawn.toml` then supplies the standing
    configuration and `NOTT_NSPAWN_ROSTER` overrides it as legacy fallback: an
    intention outranks a default.
    """
    project = tmp_path / "repo" / ".nott"
    project.mkdir(parents=True)
    (project / "spawn.toml").write_text('[[provider]]\nname = "from-project"\n')
    from_file = tmp_path / "file.toml"
    from_file.write_text('[[provider]]\nname = "from-file"\n')

    # The precondition this assertion NAMES has to be MADE, not assumed: the
    # host that runs the suite declares `NOTT_NSPAWN_ROSTER` in its own login
    # environment, and without these lines the test read the ambient value
    # and asserted about the product.
    monkeypatch.delenv("NOTT_NSPAWN_ROSTER", raising=False)
    monkeypatch.delenv("NOTT_NSPAWN_CONFIG", raising=False)
    # `setenv("HOME", ...)` would be INERT: `DEFAULT_STORE_CONFIG_PATH` is bound
    # at import from `Path.home()`. Pinning `NOTT_NSPAWN_CONFIG` is the only real
    # isolation; otherwise the test reads the operator's `~/.nott/spawn.toml` and
    # stays green only while no host adopts the feature.
    monkeypatch.setenv("NOTT_NSPAWN_CONFIG", str(tmp_path / "absent.toml"))

    assert Roster.default_path(project) is None, (
        "nothing declares a roster; default_path must NOT fall back to the "
        "project tree — absence is a typed NERR_1104 at the door"
    )

    explicit = project / "spawn.toml"
    monkeypatch.setenv("NOTT_NSPAWN_ROSTER", str(explicit))
    assert Roster.default_path(project) == explicit, (
        "with no config file, the env var still answers; cwd is still ignored"
    )
    assert Roster.read(explicit).providers[0].name == "from-project"

    config = tmp_path / "spawn.toml"
    config.write_text(from_file.read_text())
    monkeypatch.setenv("NOTT_NSPAWN_CONFIG", str(config))
    assert Roster.default_path(project) == config, (
        "THE FILE WINS over a value handed down by the launcher, and cwd is "
        "still ignored"
    )
    assert Roster.read(from_file).providers[0].name == "from-file"


def test_a_roster_carries_what_the_supervisor_requires(tmp_path: Path) -> None:
    """What killed LD-2 and LD-4, pinned as the property instead of the key.

    A plugin root must be REACHABLE — it resolves the child's `.mcp.json`
    (supervisor.py refuses without it) and is what `$PLUGIN` expands to. It is
    no longer a key of its own: `plugins` carries it, so the roster cannot
    declare a root that disagrees with the list.

    Every provider must RESOLVE a model. A fleet-wide `[defaults] model` was
    one way to guarantee that and it is not the owner's: he writes the model on
    each provider, where a reader sees which route buys which model.
    """
    roster = Roster.read(sample_roster.write(tmp_path))
    assert roster.defaults.plugin_root is not None
    for provider in roster.providers:
        assert provider.model or roster.defaults.model, provider.name


def _spawn_toml(tmp_path: Path, body: str) -> Path:
    config = tmp_path / "spawn.toml"
    config.write_text(body)
    return config


def test_spawn_toml_carries_the_roster_when_no_env_var_names_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The roster gets the launcher-independent home the store URL already has.

    `config.py` states the rule the store URL follows -- "the env names the
    PATH, the file carries the value" -- and `~/.nott/spawn.toml` exists
    because a value handed over by a launcher ages and a file does not. The
    roster path had no such home: it reached the door only through the
    launcher's environment, so a session started from a shell that predated
    the declaration resolved NOTHING and every roster read refused.
    """
    roster = tmp_path / "spawn.toml"
    roster.write_text('[[provider]]\nname = "from-file"\n')
    monkeypatch.delenv("NOTT_NSPAWN_ROSTER", raising=False)
    monkeypatch.setenv(
        "NOTT_NSPAWN_CONFIG",
        str(_spawn_toml(tmp_path, roster.read_text())),
    )

    assert Roster.default_path() == tmp_path / "spawn.toml"


def test_an_unexpanded_roster_template_is_refused_by_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`${NOTT_NSPAWN_ROSTER}` is a launcher fault, not a filename.

    Measured on SERVER 2026-08-30: a launcher that cannot expand a `${VAR}`
    passes the TEMPLATE through. Without this guard the roster read fails as a
    missing FILE and blames the operator's path instead of the launcher.
    """
    monkeypatch.setenv("NOTT_NSPAWN_ROSTER", "${NOTT_NSPAWN_ROSTER}")
    # CONFIG must point to an ABSENT file, not be deleted: deleted,
    # it falls back to operator's `~/.nott/spawn.toml`, and on a host that declared
    # `[store].roster` the file wins and the env path is never exercised.
    monkeypatch.setenv("NOTT_NSPAWN_CONFIG", str(tmp_path / "absent.toml"))

    with pytest.raises(NSpawnError) as caught:
        Roster.default_path()
    message = str(caught.value)
    assert "NOTT_NSPAWN_ROSTER" in message
    assert "${NOTT_NSPAWN_ROSTER}" in message


def test_NOTT_NSPAWN_HOME_no_longer_resolves_a_roster(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One default, not two. `NOTT_NSPAWN_HOME` was the second mechanism.

    It named a DIRECTORY under which a roster was assumed, which is the same
    implicit-location guess L3 deleted from the project-tree walk. The file
    default replaces it and nothing else does.
    """
    home = tmp_path / "home"
    (home / ".nott").mkdir(parents=True)
    (home / ".nott/spawn.toml").write_text('[[provider]]\nname = "from-home"\n')
    monkeypatch.delenv("NOTT_NSPAWN_ROSTER", raising=False)
    monkeypatch.setenv("NOTT_NSPAWN_CONFIG", str(tmp_path / "absent.toml"))
    monkeypatch.setenv("NOTT_NSPAWN_HOME", str(home))

    assert Roster.default_path() is None


def test_a_declared_roster_survives_an_unexpanded_template_in_the_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mutant the fix removes, pinned — or it comes back.

    The `${...}` guard used to run BEFORE the file, so the
    DEMOTED variable vetoed a valid `[store].roster`: the refusal told you to declare
    the path that was already declared. Without this test, moving the guard back
    above `config.resolve_roster_path()` leaves the suite GREEN — and in a
    repository whose Rail D exists to mutation-prove its own gates, a
    closed but unpinned cause is how it comes back.

    The two neighboring tests do not reach it: the template one fixes CONFIG at an
    ABSENT file and never enters the file branch, and the precedence one uses
    a SIMPLE path that never enters the guard.
    """
    roster = tmp_path / "declarado.toml"
    roster.write_text('[[provider]]\nname = "do-ficheiro"\n')
    config = tmp_path / "spawn.toml"
    config.write_text(roster.read_text())
    monkeypatch.setenv("NOTT_NSPAWN_CONFIG", str(config))
    monkeypatch.setenv("NOTT_NSPAWN_ROSTER", "${NOTT_NSPAWN_ROSTER}")

    assert Roster.default_path() == tmp_path / "spawn.toml", (
        "the file declared; an unexpanded template in the demoted source cannot "
        "veto the authority nor prescribe what is already done"
    )
    assert Roster.read(roster).providers[0].name == "do-ficheiro"


def test_a_config_without_a_store_table_is_still_the_roster(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A VALID config without `[store]` is not an error: declares nothing.

    The distinction matters because both absences are treated differently — a
    file that does not exist and a file that exists without declaring `[store]`
    both resolve to `None`, and only one of them could be mistaken for a defect.
    """
    config = tmp_path / "spawn.toml"
    config.write_text("[telemetry]\nenabled = true\n")
    monkeypatch.setenv("NOTT_NSPAWN_CONFIG", str(config))
    monkeypatch.delenv("NOTT_NSPAWN_ROSTER", raising=False)
    assert config_module.resolve_roster_path() == config


@pytest.mark.parametrize("value", ['""', '"   "', "123", "true", "[]"])
def test_a_store_roster_that_is_not_a_path_is_a_typed_refusal(
    value: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Illegal state rejects on CONSTRUCTION, never coerced to valid.

    Review of this lane proved this behavior MANUALLY and the suite did not
    prove it: empty `[store].roster`, whitespace-only or non-textual produced
    `ConfigError` in manual probe and in no test. A proof that
    nobody conducts is no proof, and this line was caught by the patch
    coverage bar for lacking a test case.
    """
    config = tmp_path / "spawn.toml"
    config.write_text(f"[store]\nroster = {value}\n")
    monkeypatch.setenv("NOTT_NSPAWN_CONFIG", str(config))
    with pytest.raises(config_module.ConfigError, match="roster"):
        config_module.resolve_roster_path()
