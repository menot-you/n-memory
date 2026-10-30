"""One file is the store/roster boundary; the old indirection is refused."""

import json
import os
import sys
import tomllib

import pytest
import test_route
from workspace_root import WORKSPACE_ROOT, workspace_path

from nspawn import agy_acp, cli, config, route, supervisor
from nspawn.journal import serve
from nspawn.owner_roster import Roster, RosterError
from nspawn.registry import create_store, store_path

root = test_route.root


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
    served = supervisor.read_roster(str(root))
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
            supervisor.read_roster()
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
    assert supervisor.read_roster().providers[0].name == "single"
    path.unlink()
    assert supervisor.read_roster().providers[0].name == "compat"


def test_route_verbs_and_supervisor_read_one_roster(
    root, tmp_path, monkeypatch, capsys
):
    """A config file and a compatibility roster that differ: one source serves both.

    The compatibility roster (`root`) declares the accounts alpha and beta. The
    single config file declares the same provider with alpha only. The route verb
    and the supervisor must serve the same accounts: the accounts of the config file.
    """
    secrets = tmp_path / "config-secrets"
    secrets.mkdir(mode=0o700)
    secrets.chmod(0o700)  # whatever the umask
    (secrets / "alpha.token").write_bytes(
        (tmp_path / "claude-secrets" / "alpha.token").read_bytes()
    )
    (secrets / "alpha.token").chmod(0o600)
    single = tmp_path / "config.toml"
    single.write_text(
        (tmp_path / "spawn.toml")
        .read_text()
        .replace(str(tmp_path / "claude-secrets"), str(secrets), 1)
    )
    monkeypatch.setenv("NOTT_NSPAWN_CONFIG", str(single))
    served = supervisor.read_roster()
    assert served.path == single
    assert served.select("claude").ordered_accounts() == ["alpha"]
    create_store(store_path())
    capsys.readouterr()
    listed = ["route", "list", "claude", "--model", "claude-opus-5", "--effort", "high"]
    assert cli.main(listed) == 0
    shown = json.loads(capsys.readouterr().out)
    assert [row["alias"] for row in shown["routes"]] == ["alpha"]


@pytest.mark.parametrize("case", ["absent", "malformed"])
def test_node_roster_refuses_without_a_host_path(tmp_path, monkeypatch, case):
    """The route verbs, the node and the app show this refusal: no host path."""
    from nspawn.errors import NERR_1103, NERR_1104
    from nspawn.route import RosterUnreadable, node_roster

    path = tmp_path / "spawn.toml"
    if case == "malformed":
        path.write_text("[[provider]\n")
    isolate(monkeypatch, tmp_path / "absent-config.toml")
    monkeypatch.setenv("NOTT_NSPAWN_ROSTER", str(path))
    with pytest.raises(RosterUnreadable) as caught:
        node_roster()
    assert caught.value.entry is (NERR_1104 if case == "absent" else NERR_1103)
    assert str(path) in str(caught.value.__cause__)
    assert str(tmp_path) not in caught.value.message
    assert str(tmp_path) not in str(caught.value)


#: A reader that opens a FIFO in blocking mode waits for a writer that never
#: comes. The SIGALRM of pytest-timeout interrupts that wait in the test thread,
#: so the wait fails the test and does not hang the run.
WAIT_BOUND = pytest.mark.timeout(10, method="signal")


@pytest.fixture
def fifo(tmp_path):
    path = tmp_path / "roster.fifo"
    os.mkfifo(path)
    return path


def journal_reader():
    """The roster reader of the journal writer, as `serve.main` builds it."""
    taken = []
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(serve, "run", lambda settings, _stop: taken.append(settings))
        assert serve.main() == 0
    return taken[0].read_roster


def agy_reader():
    """The roster read of the AGY shim, with the refusal of the one reader."""
    try:
        agy_acp.roster_provider()
    except agy_acp.Refused as refused:
        assert "/" not in str(refused)  # the shim shows no host path
        raise refused.__cause__ from refused
    raise AssertionError("the AGY shim served a roster from a FIFO")


def node_reader():
    """The roster read of a node, with the refusal of the one reader."""
    try:
        route.node_roster()
    except route.RosterUnreadable as refused:
        assert "/" not in str(refused)  # the node shows no host path
        raise refused.__cause__ from refused
    raise AssertionError("the node served a roster from a FIFO")


@WAIT_BOUND
@pytest.mark.parametrize("reader", ["supervisor", "journal", "agy", "node"])
def test_a_fifo_at_the_roster_path_refuses_each_reader_without_waiting(
    tmp_path, monkeypatch, fifo, reader
):
    """A FIFO with no writer is a roster that cannot be read, not a wait."""
    isolate(monkeypatch, tmp_path / "absent-config.toml")
    monkeypatch.setenv("NOTT_NSPAWN_ROSTER", str(fifo))
    call = {
        "supervisor": supervisor.read_roster,
        "journal": journal_reader() if reader == "journal" else None,
        "agy": agy_reader,
        "node": node_reader,
    }[reader]
    with pytest.raises(RosterError) as refused:
        call()
    assert str(fifo) in str(refused.value)


@WAIT_BOUND
def test_a_fifo_at_the_config_path_refuses_without_waiting(monkeypatch, fifo):
    """The reader reads the single config file first: a FIFO there is no wait."""
    isolate(monkeypatch, fifo)
    with pytest.raises(config.ConfigError) as refused:
        supervisor.read_roster()
    assert str(fifo) in str(refused.value)


@WAIT_BOUND
def test_a_roster_swapped_to_a_fifo_after_the_check_is_not_waited_on(
    tmp_path, monkeypatch
):
    """The reader reads the file that it checked, never the path again by name.

    The first check of the roster path (a `stat` by name or an `fstat` of its
    descriptor) puts a FIFO at the path. A reader that opens the path again in
    blocking mode waits on that FIFO.
    """
    path = tmp_path / "spawn.toml"
    path.write_text('[[provider]]\nname = "checked"\n')
    checked = os.stat(path)
    isolate(monkeypatch, tmp_path / "absent-config.toml")
    monkeypatch.setenv("NOTT_NSPAWN_ROSTER", str(path))
    real_stat, real_fstat = os.stat, os.fstat
    swapped = []

    def swap():
        if not swapped:
            swapped.append(True)
            path.unlink()
            os.mkfifo(path)

    def stat_spy(target, *args, **kwargs):
        result = real_stat(target, *args, **kwargs)
        if not isinstance(target, int) and os.fspath(target) == str(path):
            swap()
        return result

    def fstat_spy(descriptor):
        result = real_fstat(descriptor)
        if (result.st_dev, result.st_ino) == (checked.st_dev, checked.st_ino):
            swap()
        return result

    monkeypatch.setattr(os, "stat", stat_spy)
    monkeypatch.setattr(os, "fstat", fstat_spy)
    served = supervisor.read_roster()
    monkeypatch.undo()
    assert swapped, "the reader never checked the roster path"
    assert [provider.name for provider in served.providers] == ["checked"]


#: The `open` events (PEP 578) that this process raised, by path, while a list
#: is set. `os.open`, `io.open` and the `pathlib` reads each raise the event,
#: with the flags of the open. An audit hook cannot be removed, so the one hook
#: records only while a test sets the list.
OPENED: list[tuple[str, int]] | None = None


def record_open(event, arguments):
    if event == "open" and OPENED is not None and arguments[0] is not None:
        target, _mode, flags = arguments
        if not isinstance(target, int):
            OPENED.append((os.fsdecode(os.fspath(target)), flags))


sys.addaudithook(record_open)


def source_roster(tmp_path, monkeypatch, source, name):
    """A regular roster file at the selected source, and its path."""
    path = tmp_path / f"{source}.toml"
    path.write_text(f'[[provider]]\nname = "{name}"\n')
    if source == "config":
        isolate(monkeypatch, path)
    else:
        isolate(monkeypatch, tmp_path / "absent-config.toml")
        monkeypatch.setenv("NOTT_NSPAWN_ROSTER", str(path))
    return path


@WAIT_BOUND
@pytest.mark.parametrize("source", ["config", "roster"])
def test_one_roster_read_opens_the_selected_source_one_time_without_a_wait(
    tmp_path, monkeypatch, source
):
    """Source selection, the parse and the `[store]` check use one read."""
    global OPENED
    path = source_roster(tmp_path, monkeypatch, source, "once")
    OPENED = []
    try:
        served = supervisor.read_roster()
    finally:
        opened, OPENED = OPENED, None
    assert [provider.name for provider in served.providers] == ["once"]
    flags = [flag for target, flag in opened if target == str(path)]
    assert len(flags) == 1, f"the reader opened {path.name} {len(flags)} times"
    assert flags[0] & os.O_NONBLOCK, "the reader opened the source in blocking mode"


@WAIT_BOUND
@pytest.mark.parametrize("source", ["config", "roster"])
def test_a_source_swapped_to_a_fifo_after_the_read_serves_the_read_bytes(
    tmp_path, monkeypatch, source
):
    """The reader parses and checks the bytes that it read. A FIFO that takes
    the place of the file after that read is never opened and never waited on."""
    path = source_roster(tmp_path, monkeypatch, source, "captured")
    real_read = config.read_regular_file
    swapped = []

    def read_then_swap(target):
        raw = real_read(target)
        if os.fspath(target) == str(path) and not swapped:
            swapped.append(True)
            path.unlink()
            os.mkfifo(path)
        return raw

    monkeypatch.setattr(config, "read_regular_file", read_then_swap)
    served = supervisor.read_roster()
    assert swapped, "the reader never read the source through the helper"
    assert [provider.name for provider in served.providers] == ["captured"]
    assert served.path == path
