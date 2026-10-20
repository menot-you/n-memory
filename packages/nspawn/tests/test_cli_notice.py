"""CLI notices observe one selected binary without delaying a session."""

from __future__ import annotations

import importlib
import os
import threading
from pathlib import Path
from typing import Any

import pytest

from nspawn.claude_child import Version


@pytest.fixture(autouse=True)
def isolated_managed_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original_open = os.open
    managed = {
        Path("/etc/claude-code/managed-settings.json"),
        Path("/Library/Application Support/ClaudeCode/managed-settings.json"),
    }

    def private_open(path: Any, *args: Any, **kwargs: Any) -> int:
        if Path(path) in managed:
            path = tmp_path / "managed-settings.json"
        assert Path(path) not in managed, "A test read host managed settings."
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", private_open)


def reader(probe: Any, latest: Any) -> Any:
    module = importlib.import_module("nspawn.cli_notice")
    return module.Notices(probe=probe, latest=latest)


def test_newer_stable_release_has_one_notice_and_one_probe(tmp_path: Path) -> None:
    binary = tmp_path / "claude"
    binary.write_text("installed binary")
    calls: list[Path] = []
    done = threading.Event()

    def probe(path: Path) -> Version:
        calls.append(path)
        return Version(2, 1, 1)

    notices = reader(probe, lambda channel: Version(2, 1, 2))
    notices.start(binary, "stable", done.set)
    assert done.wait(3)
    assert notices.read(binary, "stable") == {
        "installed": "2.1.1",
        "latest": "2.1.2",
        "channel": "stable",
    }
    notices.start(binary, "stable", done.set)
    assert calls == [binary]


def test_equal_stable_versions_have_no_notice(tmp_path: Path) -> None:
    binary = tmp_path / "claude"
    binary.write_text("installed binary")
    done = threading.Event()
    notices = reader(lambda path: Version(2, 1, 1), lambda channel: Version(2, 1, 1))
    notices.start(binary, "stable", done.set)
    assert done.wait(3)
    assert notices.read(binary, "stable") is None


def test_blocked_offline_observation_does_not_block_metadata(tmp_path: Path) -> None:
    binary = tmp_path / "claude"
    binary.write_text("installed binary")
    entered, release, done = threading.Event(), threading.Event(), threading.Event()

    def latest(channel: str) -> None:
        entered.set()
        assert release.wait(3)

    notices = reader(lambda path: Version(2, 1, 1), latest)
    try:
        notices.start(binary, "stable", done.set)
        assert entered.wait(3)
        assert notices.read(binary, "stable") is None
    finally:
        release.set()
    assert done.wait(3)
    assert notices.read(binary, "stable") is None


def test_swapped_target_clears_the_observed_notice_without_a_probe(
    tmp_path: Path,
) -> None:
    old, new = tmp_path / "old", tmp_path / "new"
    old.write_text("old binary")
    new.write_text("new binary")
    binary = tmp_path / "claude"
    binary.symlink_to(old)
    done = threading.Event()
    calls: list[Path] = []

    def probe(path: Path) -> Version:
        calls.append(path)
        return Version(2, 1, 1)

    notices = reader(probe, lambda channel: Version(2, 1, 2))
    notices.start(binary, "stable", done.set)
    assert done.wait(3)
    assert notices.read(binary, "stable") is not None
    binary.unlink()
    binary.symlink_to(new)
    assert notices.read(binary, "stable") is None
    assert calls == [binary]


@pytest.mark.parametrize("channel", ["preview", "", "stable/../../latest"])
def test_unknown_channels_never_start_a_probe(tmp_path: Path, channel: str) -> None:
    binary = tmp_path / "claude"
    binary.write_text("installed binary")

    def forbidden(*args: object) -> None:
        pytest.fail("An unknown channel reached the version probe.")

    notices = reader(forbidden, forbidden)
    notices.start(binary, channel, forbidden)
    assert notices.read(binary, channel) is None


def test_oversized_version_cannot_enter_host_metadata(tmp_path: Path) -> None:
    binary = tmp_path / "claude"
    binary.write_text("installed binary")
    done = threading.Event()
    notices = reader(
        lambda path: Version(2, 1, 1), lambda channel: Version(10**70, 1, 1)
    )
    notices.start(binary, "stable", done.set)
    assert done.wait(3)
    assert notices.read(binary, "stable") is None


def test_selected_profile_channel_drives_the_native_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nspawn import cli_notice
    from nspawn.owner_roster import NativeAccount, Provider, Roster

    binary = tmp_path / ".local/share/claude/versions/2.1.1"
    binary.parent.mkdir(parents=True)
    binary.write_text("native binary")
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "settings.json").write_text('{"autoUpdatesChannel":"stable"}')
    provider = Provider(
        name="claude", bin=binary, accounts={"one": NativeAccount(home=profile)}
    )
    roster = Roster(providers=[provider])
    done = threading.Barrier(3)
    channels: list[str] = []

    def latest(channel: str) -> Version:
        channels.append(channel)
        return Version(2, 1, 1)

    def changed() -> None:
        done.wait(3)

    notices = reader(lambda path: Version(2, 1, 1), latest)
    monkeypatch.setattr(cli_notice, "HOST_NOTICES", notices)
    cli_notice.start_host(roster, changed)
    done.wait(3)
    assert sorted(channels) == ["latest", "stable"]
    assert cli_notice.read_host(roster, "claude", "one", str(tmp_path)) is None
    cli_notice.start_host(roster, changed)
    assert sorted(channels) == ["latest", "stable"]


def test_channel_reads_a_profile_with_existing_configuration_size(
    tmp_path: Path,
) -> None:
    from nspawn import cli_notice
    from nspawn.owner_roster import NativeAccount, Provider

    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "settings.json").write_text(
        '{"autoUpdatesChannel":"stable","description":"' + "x" * 4096 + '"}'
    )
    provider = Provider(name="claude", accounts={"one": NativeAccount(home=profile)})
    assert cli_notice.channel_read(provider, "one") == "stable"


def test_nonregular_settings_cannot_block_metadata(tmp_path: Path) -> None:
    import os

    from nspawn import cli_notice
    from nspawn.owner_roster import NativeAccount, Provider

    profile = tmp_path / "profile"
    profile.mkdir()
    settings = profile / "settings.json"
    os.mkfifo(settings)
    provider = Provider(name="claude", accounts={"one": NativeAccount(home=profile)})
    done = threading.Event()
    result: list[str | cli_notice.Unknown] = []

    def read() -> None:
        result.append(cli_notice.channel_read(provider, "one"))
        done.set()

    thread = threading.Thread(target=read, daemon=True)
    thread.start()
    finished = done.wait(0.2)
    if not finished:
        with settings.open("w") as stream:
            stream.write("{}")
    thread.join(1)
    assert finished and len(result) == 1
    assert isinstance(result[0], cli_notice.Unknown)


def test_two_channels_share_one_binary_probe(tmp_path: Path) -> None:
    binary = tmp_path / "claude"
    binary.write_text("installed binary")
    done = threading.Event()
    calls: list[Path] = []

    def probe(path: Path) -> Version:
        calls.append(path)
        return Version(2, 1, 1)

    notices = reader(probe, lambda channel: Version(2, 1, 2))
    for channel in ("stable", "latest"):
        done.clear()
        notices.start(binary, channel, done.set)
        assert done.wait(3)
        assert notices.read(binary, channel) is not None
    assert calls == [binary]


def test_probe_refuses_a_binary_the_route_would_refuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nspawn import cli_notice

    binary = tmp_path / "claude"
    binary.write_text("not an executable")

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("The notice executed a refused binary.")

    monkeypatch.setattr(cli_notice.subprocess, "run", forbidden)
    assert isinstance(cli_notice.probe(binary), cli_notice.Unknown)


def test_real_version_process_runs_once_for_both_channels(tmp_path: Path) -> None:
    import sys

    from nspawn import cli_notice

    calls = tmp_path / "calls.txt"
    binary = tmp_path / "claude"
    binary.write_text(
        f"#!{sys.executable}\nfrom pathlib import Path\nimport os, sys\n"
        "if os.environ.get('TMPDIR') != '/var/tmp' or not os.environ.get('HOME', '').startswith('/var/tmp/nspawn-cli-version-'): sys.exit(9)\n"
        f"with Path({str(calls)!r}).open('a') as stream: stream.write('probe\\n')\n"
        "print('2.1.1 (Claude Code)')\n"
    )
    binary.chmod(0o700)
    notices = reader(cli_notice.probe, lambda channel: Version(2, 1, 2))
    done = threading.Event()
    for channel in ("stable", "latest"):
        done.clear()
        notices.start(binary, channel, done.set)
        assert done.wait(3)
        assert notices.read(binary, channel) is not None
    assert calls.read_text().splitlines() == ["probe"]


def test_repository_channel_has_an_observation_at_host_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nspawn import cli_notice
    from nspawn.owner_roster import NativeAccount, Provider, Roster

    binary = tmp_path / ".local/share/claude/versions/2.1.1"
    binary.parent.mkdir(parents=True)
    binary.write_text("native binary")
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "settings.json").write_text('{"autoUpdatesChannel":"latest"}')
    repo = tmp_path / "repo"
    (repo / ".claude").mkdir(parents=True)
    (repo / ".claude/settings.json").write_text('{"autoUpdatesChannel":"stable"}')
    provider = Provider(
        name="claude", bin=binary, accounts={"one": NativeAccount(home=profile)}
    )
    roster = Roster(providers=[provider])
    observed: list[str] = []

    class Completed:
        def start(
            self, path: Path, channel: str, changed: object, failed: object = None
        ) -> None:
            observed.append(channel)

        def status_read(self, path: Path, channel: str) -> cli_notice.Status:
            return (
                cli_notice.Available(
                    {"installed": "2.1.1", "latest": "2.1.2", "channel": channel}
                )
                if channel in observed
                else cli_notice.Unknown("Claude release check is pending")
            )

    monkeypatch.setattr(cli_notice, "HOST_NOTICES", Completed())
    cli_notice.start_host(roster, lambda: None)
    assert cli_notice.read_host(roster, "claude", "one", str(repo)) == {
        "installed": "2.1.1",
        "latest": "2.1.2",
        "channel": "stable",
    }


def test_failed_observation_has_a_typed_unknown_state(tmp_path: Path) -> None:
    from nspawn import cli_notice

    binary = tmp_path / "claude"
    binary.write_text("installed binary")
    notices = reader(lambda path: Version(2, 1, 1), lambda channel: None)
    notices.observe(binary, "stable", lambda: None)
    status = notices.observed[str(binary), "stable"][1]
    assert status is not None, "A failed check must retain its unknown reason."
    assert isinstance(status, cli_notice.Unknown)
    assert status.reason == "Claude release check returned no version"
    assert notices.read(binary, "stable") is None


def test_current_observation_has_a_typed_current_state(tmp_path: Path) -> None:
    from nspawn import cli_notice

    binary = tmp_path / "claude"
    binary.write_text("installed binary")
    notices = reader(lambda path: Version(2, 1, 1), lambda channel: Version(2, 1, 1))
    notices.observe(binary, "stable", lambda: None)
    status = notices.observed[str(binary), "stable"][1]
    assert status is not None, "A current version must retain a successful check."
    assert isinstance(status, cli_notice.Current)
    assert notices.read(binary, "stable") is None


def test_failed_check_retries_in_the_background_then_stops(tmp_path: Path) -> None:
    binary = tmp_path / "claude"
    binary.write_text("installed binary")
    checks: list[str] = []
    probes: list[Path] = []
    waiting, release, finished = threading.Event(), threading.Event(), threading.Event()

    def latest(channel: str) -> Version | None:
        checks.append(channel)
        return None if len(checks) == 1 else Version(2, 1, 2)

    def probe(path: Path) -> Version:
        probes.append(path)
        return Version(2, 1, 1)

    def wait(seconds: float) -> None:
        assert seconds == 3600
        waiting.set()
        assert release.wait(3)

    notices = reader(probe, latest)
    notices.wait = wait
    try:
        notices.start(
            binary, "stable", lambda: finished.set() if len(checks) == 2 else None
        )
        assert waiting.wait(0.2), "A failed check must schedule a retry."
        assert notices.read(binary, "stable") is None
    finally:
        release.set()
    assert finished.wait(3)
    assert checks == ["stable", "stable"]
    assert probes == [binary]
    assert notices.read(binary, "stable") is not None


def test_failed_binary_probe_retries_then_reuses_success(tmp_path: Path) -> None:
    from nspawn import cli_notice

    binary = tmp_path / "claude"
    binary.write_text("installed binary")
    probes: list[Path] = []

    def probe(path: Path) -> Version | cli_notice.Unknown:
        probes.append(path)
        return (
            cli_notice.Unknown("Claude version process timed out")
            if len(probes) == 1
            else Version(2, 1, 1)
        )

    notices = reader(probe, lambda channel: Version(2, 1, 2))
    assert isinstance(
        notices.observe(binary, "stable", lambda: None), cli_notice.Unknown
    )
    notices.observe(binary, "stable", lambda: None)
    notices.observe(binary, "latest", lambda: None)
    assert probes == [binary, binary]
    assert notices.read(binary, "stable") is not None
    assert notices.read(binary, "latest") is not None


def test_malformed_profile_recovers_without_restarting_observations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nspawn import cli_notice
    from nspawn.owner_roster import NativeAccount, Provider, Roster

    binary = tmp_path / ".local/share/claude/versions/2.1.1"
    binary.parent.mkdir(parents=True)
    binary.write_text("native binary")
    profile = tmp_path / "profile"
    profile.mkdir()
    settings = profile / "settings.json"
    settings.write_text("{")
    roster = Roster(
        providers=[
            Provider(
                name="claude",
                bin=binary,
                accounts={"one": NativeAccount(home=profile)},
            )
        ]
    )
    checks: list[str] = []
    probes: list[Path] = []
    finished = threading.Event()

    def probe(path: Path) -> Version:
        probes.append(path)
        return Version(2, 1, 1)

    def latest(channel: str) -> Version:
        checks.append(channel)
        return Version(2, 1, 2)

    notices = reader(probe, latest)
    monkeypatch.setattr(cli_notice, "HOST_NOTICES", notices)
    cli_notice.start_host(roster, lambda: finished.set() if len(checks) == 2 else None)
    assert finished.wait(3)
    assert isinstance(
        cli_notice.read_host(roster, "claude", "one", str(tmp_path)), cli_notice.Unknown
    )
    settings.write_text('{"autoUpdatesChannel":"stable"}')
    assert cli_notice.read_host(roster, "claude", "one", str(tmp_path)) == {
        "installed": "2.1.1",
        "latest": "2.1.2",
        "channel": "stable",
    }
    cli_notice.start_host(roster, lambda: None)
    assert sorted(checks) == ["latest", "stable"]
    assert probes == [binary]
