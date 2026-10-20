"""The member's test isolation, owned in one place.

Every test's registry lives in the directory the `nott-database` pytest plugin
gives it (`NOTT_DATABASE_DIR`), so tests share no store and xdist workers need no
setup of their own; `tests/registry_reset.store()` creates the file on first use.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator

import pytest
from mutmut.configuration import Config
from nott_system1 import SCOPES, route_variables
from nott_system1.renamed import RENAMED as SYSTEM1_RENAMED
from nott_system1.renamed import RETIRED as SYSTEM1_RETIRED

from nspawn.renamed import RENAMED

#: The host's production identity: the live store's keys, its scope, the session a live
#: client runs under, and the paid System1 key. A box that serves nMEMORY exports them
#: in its login shell, and a test run from that shell inherited them — it, and every
#: child it spawns. A case that needs one sets it itself.
HOST_IDENTITY = (
    "NOTT_MEMORY_POLICY",
    "NOTT_NMEMORY_HMAC_KEY",
    "NOTT_NMEMORY_HMAC_KEY_FILE",
    "NOTT_NMEMORY_PROJECT",
    "NOTT_NMEMORY_HARNESS",
    "NOTT_NMEMORY_MODEL",
    "NOTT_NMEMORY_SESSION_ID",
    *(name for scope in (None, *sorted(SCOPES)) for name in route_variables(scope)),
    # A node's nSPAWN (`driver.NODE_SESSION_ENV`, `driver.GRANT_ENV`).
    "NOTT_NSPAWN_NODE_SESSION",
    "NOTT_NSPAWN_GRANT",
)

#: A unit test NEVER reads the operator's `~/.nott/spawn.toml`. It was only
#: ever reachable implicitly — `store_config_path()` falls back to `$HOME` —
#: and that fallback made a contract test's verdict depend on whichever config
#: the box happens to carry. Measured: with the single-config law a host file
#: that still declares the retired `[store].roster` turned `nspawn_route_ping` into
#: `-32603` inside `tests/test_mcp_contract.py`, which owns a FakeSupervisor
#: precisely so that nothing outside the repository can decide it. Three
#: fixtures in `test_supervisor.py` already pinned this by hand; this pins it
#: once, and a test that wants a config still names one with `monkeypatch`.
STORE_CONFIG_PATH_ENV = "NOTT_NSPAWN_CONFIG"
#: The session socket Claude Code exports to every process it starts, pytest too.
CALLER_SOCKET_ENV = "CLAUDE_CODE_MESSAGING_SOCKET"


@pytest.fixture(scope="session", autouse=True)
def _no_caller_session() -> Iterator[None]:
    """No test writes into the Claude Code session that launched the run.

    MEASURED 2026-09-23: pytest inherited `CLAUDE_CODE_MESSAGING_SOCKET`, and four
    notices from armed test supervisors landed in the owner's live session. A
    test that wants a caller builds its own (tests/fake_session.py);
    tests/test_harness_isolation.py proves this from a child pytest.
    """
    previous = os.environ.pop(CALLER_SOCKET_ENV, None)
    try:
        yield
    finally:
        if previous is not None:
            os.environ[CALLER_SOCKET_ENV] = previous


ROSTER_PATH_ENV = "NOTT_NSPAWN_ROSTER"


@pytest.fixture(scope="session", autouse=True)
def _no_ambient_owner_config(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[None]:
    home = tmp_path_factory.mktemp("no-owner-config")
    roster = home / "roster.toml"
    roster.write_text(
        '[defaults]\nmodel = "fixture-model"\n'
        '[[provider]]\nname = "fixture"\nkind = "claude"\n'
        'model = "fixture-model"\nmodels = ["fixture-model"]\n',
        encoding="utf-8",
    )
    owned = {
        # Absent ON PURPOSE: the tests that want a single config file name one
        # with `monkeypatch`, and the rest must not inherit the operator's.
        STORE_CONFIG_PATH_ENV: str(home / "absent-spawn.toml"),
        ROSTER_PATH_ENV: str(roster),
    }
    previous = {name: os.environ.get(name) for name in owned}
    os.environ.update(owned)
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                del os.environ[name]
            else:
                os.environ[name] = value


#: The OPERATOR's umask cannot decide the verdict. `vendor_home` rejects a
#: parent directory with `st_mode & 0o022` (`_validate_owner_file_parent`), and the
#: installed skills fence repeats the verification on each cache ancestor
#: (`native._codex_skill_roots`). A fixture that constructs the tree with
#: `mkdir()` inherits ambient umask: with `022` directories are `0755` and the
#: acceptance path runs; with `002` they are `0775` and the product — rightly —
#: rejects them. MEASURED 2026-09-14: the gate runs under `umask 002` (today the
#: python gate's `main` sets `os.umask(0o002)`), and therefore 21 tests that passed in a developer shell went red under
#: the gate, across the entire member without anything on the wire stating why. Fixing here
#: constructs the tree the product requires, not loosening the fence: a test that
#: wants the rejected case does explicit `chmod` and continues to see it.
@pytest.fixture(scope="session", autouse=True)
def _owner_controlled_umask() -> Iterator[None]:
    previous = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(previous)


@pytest.fixture(autouse=True)
def _no_old_spelling(monkeypatch: pytest.MonkeyPatch) -> None:
    # A host that still exports a renamed variable's old spelling makes every read
    # refuse by name; the suite measures nSPAWN, not the host's migration. Neither
    # spelling of the host's identity reaches a test or the child it spawns. A case that
    # needs one sets it itself.
    for old in (
        *RENAMED.values(),
        *SYSTEM1_RENAMED.values(),
        *SYSTEM1_RETIRED,
        *HOST_IDENTITY,
    ):
        monkeypatch.delenv(old, raising=False)


def pytest_configure(config: pytest.Config) -> None:
    # The `needs` host-capability marker (tests/needs.py). pytest_configure is a
    # historic hook, so the plugin registered here still configures itself.
    import needs

    config.pluginmanager.register(needs, "nspawn-needs")
    # mutmut's setup check makes every function raise, so the supervisor's threads
    # raise too; under the pyproject's error filter pytest then raises an
    # ExceptionGroup out of pytest.main at session end and kills mutmut itself.
    # Only that check relaxes the rule; mutant runs and plain runs keep it.
    if os.environ.get("MUTANT_UNDER_TEST") == "fail":
        config.addinivalue_line(
            "filterwarnings", "default::pytest.PytestUnhandledThreadExceptionWarning"
        )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    # Under a mutant, each test gets a pytest-timeout bound from its OWN duration in
    # this host's mutmut stats run, by mutmut's own whole-mutant formula
    # ((duration + timeout_constant) * timeout_multiplier, 15 s floor by default)
    # applied per test: a mutant that turns one wait into a hang is killed at that
    # test instead of after every test's wait adds up to mutmut's wall limit. The
    # stats, clean and forced-fail runs get none, so each host calibrates itself.
    # A test the stats run never timed gets none: mutmut selects tests from those
    # stats, and its own wall limit still bounds the mutant.
    if "__mutmut_" not in os.environ.get("MUTANT_UNDER_TEST", ""):
        return

    stats = json.loads((config.rootpath / "mutmut-stats.json").read_text())
    duration_by_test: dict[str, float] = stats["duration_by_test"]
    mutmut = Config.get()
    for item in items:
        duration = duration_by_test.get(item.nodeid)
        if duration is not None:
            limit = (duration + mutmut.timeout_constant) * mutmut.timeout_multiplier
            item.add_marker(pytest.mark.timeout(limit))


@pytest.fixture
def claude_models(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    from nspawn import claude_catalog

    models = {
        "opus": "claude-opus-5-5",
        "sonnet": "claude-sonnet-5-5",
        "haiku": "claude-haiku-5-5",
        "fable": "claude-fable-5-1",
        "default": "claude-opus-5-5",
    }
    monkeypatch.setattr(claude_catalog, "catalog_read", lambda binary: models)
    return models
