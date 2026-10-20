"""Observe native Claude releases without changing an installation."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from urllib.error import URLError
from urllib.request import urlopen

from .claude_child import Version, parse_version
from .native import NativePreparationError
from .native import _path as native_path
from .owner_roster import Provider, Roster, claude_config_dir
from .vendor_home import AUTH_MAX_BYTES

RELEASE_ROOT: Final = "https://storage.googleapis.com/claude-code-dist-86c565f3-f756-42ad-8dfa-d59b1c096819/claude-code-releases"
CHANNELS: Final = frozenset({"stable", "latest"})
TIMEOUT: Final = 2
MAX_BYTES: Final = 4096
RETRY_INTERVAL: Final = 3600

type Identity = tuple[str, int, int, int, int, int]
type Notice = dict[str, str]


@dataclass(frozen=True)
class Unknown:
    reason: str
    pending: bool = False


@dataclass(frozen=True)
class Current:
    installed: Version
    available: Version


@dataclass(frozen=True)
class Available:
    notice: Notice


type Status = Unknown | Current | Available
type Probe = Callable[[Path], Version | Unknown | None]
type Latest = Callable[[str], Version | Unknown | None]


def identity(binary: Path) -> Identity | None:
    try:
        resolved = binary.resolve(strict=True)
        s = resolved.stat()
        return (
            str(resolved),
            s.st_dev,
            s.st_ino,
            s.st_size,
            s.st_mtime_ns,
            s.st_ctime_ns,
        )
    except OSError, RuntimeError:
        return None


def probe(binary: Path) -> Version | Unknown:
    try:
        selected = native_path(binary, directory=False)
        with tempfile.TemporaryDirectory(
            prefix="nspawn-cli-version-", dir="/var/tmp"
        ) as home:
            result = subprocess.run(
                [str(selected), "--version"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=TIMEOUT,
                check=False,
                env={
                    "PATH": os.defpath,
                    "HOME": home,
                    "CLAUDE_CONFIG_DIR": str(Path(home) / ".claude"),
                    "TMPDIR": "/var/tmp",
                    "NOTT_DATABASE_DIR": str(Path(home) / "database"),
                    "DISABLE_AUTOUPDATER": "1",
                    "DISABLE_UPDATES": "1",
                },
            )
        if result.returncode != 0:
            return Unknown("Claude version process failed")
        version = parse_version(result.stdout[:MAX_BYTES])
        return (
            version
            if version is not None
            else Unknown("Claude version response has no version")
        )
    except subprocess.TimeoutExpired:
        return Unknown("Claude version process timed out")
    except NativePreparationError:
        return Unknown("Claude binary failed executable validation")
    except OSError:
        return Unknown("Claude version process could not start")
    except UnicodeError:
        return Unknown("Claude version response is not valid text")


def latest(channel: str) -> Version | Unknown:
    if channel not in CHANNELS:
        return Unknown("Claude release channel is unsupported")
    try:
        with urlopen(f"{RELEASE_ROOT}/{channel}", timeout=TIMEOUT) as response:
            raw = response.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            return Unknown("Claude release response exceeds 4096 bytes")
        version = parse_version(raw.decode("utf-8"))
        return (
            version
            if version is not None
            else Unknown("Claude release response has no version")
        )
    except OSError, URLError:
        return Unknown("Claude release request failed")
    except UnicodeError, ValueError:
        return Unknown("Claude release response is not valid text")


class Notices:
    def __init__(self, *, probe: Probe = probe, latest: Latest = latest) -> None:
        self.probe, self.latest = probe, latest
        self.wait: Callable[[float], None] = time.sleep
        self.lock = threading.Lock()
        self.version_lock = threading.Lock()
        self.versions: dict[str, tuple[Identity, Version]] = {}
        self.started: set[tuple[str, str]] = set()
        self.observed: dict[tuple[str, str], tuple[Identity | None, Status]] = {}

    def start(
        self,
        binary: Path,
        channel: str,
        changed: Callable[[], None],
        failed: Callable[[str], None] | None = None,
    ) -> None:
        """Observe in the background. Retry failed checks after one hour."""
        if channel not in CHANNELS:
            return
        key = str(binary), channel
        with self.lock:
            if key in self.started:
                return
            self.started.add(key)
        threading.Thread(
            target=self.watch,
            args=(binary, channel, changed, failed),
            name="nspawn-cli-notice",
            daemon=True,
        ).start()

    def watch(
        self,
        binary: Path,
        channel: str,
        changed: Callable[[], None],
        failed: Callable[[str], None] | None,
    ) -> None:
        while isinstance(status := self.observe(binary, channel, changed), Unknown):
            if failed is not None:
                failed(status.reason)
            self.wait(RETRY_INTERVAL)

    def installed_read(self, binary: Path) -> Version | Unknown:
        with self.version_lock:
            key = str(binary)
            if key not in self.versions:
                before = identity(binary)
                if before is None:
                    return Unknown("Claude binary identity is unavailable")
                installed = self.probe(binary)
                if installed is None:
                    return Unknown("Claude version check returned no version")
                if isinstance(installed, Unknown):
                    return installed
                if before != identity(binary):
                    return Unknown("Claude binary changed during its version check")
                self.versions[key] = before, installed
            observed, installed = self.versions[key]
        return (
            installed
            if observed == identity(binary)
            else Unknown("Claude binary changed after its version check")
        )

    def observe(
        self, binary: Path, channel: str, changed: Callable[[], None]
    ) -> Status:
        before = identity(binary)
        installed = self.installed_read(binary)
        status: Status
        if isinstance(installed, Unknown):
            status = installed
        else:
            available = self.latest(channel)
            if available is None:
                status = Unknown("Claude release check returned no version")
            elif isinstance(available, Unknown):
                status = available
            elif before is None or before != identity(binary):
                status = Unknown("Claude binary changed during its release check")
            elif len(str(installed)) > 64 or len(str(available)) > 64:
                status = Unknown("Claude version exceeds the metadata limit")
            elif (available.major, available.minor, available.patch) > (
                installed.major,
                installed.minor,
                installed.patch,
            ):
                status = Available(
                    {
                        "installed": str(installed),
                        "latest": str(available),
                        "channel": channel,
                    }
                )
            else:
                status = Current(installed, available)
        with self.lock:
            self.observed[str(binary), channel] = before, status
        changed()
        return status

    def status_read(self, binary: Path, channel: str) -> Status:
        with self.lock:
            observed = self.observed.get((str(binary), channel))
        if observed is None:
            return Unknown("Claude release check is pending", pending=True)
        if observed[0] != identity(binary):
            return Unknown("Claude binary changed after its release check")
        return observed[1]

    def read(self, binary: Path, channel: str) -> Notice | None:
        """Return an update notice only for the unchanged selected binary."""
        status = self.status_read(binary, channel)
        return dict(status.notice) if isinstance(status, Available) else None


def channel_read(
    provider: Provider, account: str | None, cwd: str | None = None
) -> str | Unknown:
    channel = "latest"
    paths = [
        claude_config_dir(
            provider.accounts.get(account) if account is not None else None
        )
        / "settings.json"
    ]
    if cwd is not None:
        paths.extend(
            Path(cwd) / ".claude" / name
            for name in ("settings.json", "settings.local.json")
        )
    paths.append(
        Path(
            "/Library/Application Support/ClaudeCode/managed-settings.json"
            if sys.platform == "darwin"
            else "/etc/claude-code/managed-settings.json"
        )
    )
    for path in paths:
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
            with os.fdopen(descriptor, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    return Unknown("Claude settings file is not a regular file")
                raw = stream.read(AUTH_MAX_BYTES + 1)
        except FileNotFoundError:
            continue
        except OSError:
            return Unknown("Claude settings file could not be read")
        if len(raw) > AUTH_MAX_BYTES:
            return Unknown("Claude settings file exceeds the size limit")
        try:
            settings = json.loads(raw)
        except ValueError, UnicodeError, RecursionError:
            return Unknown("Claude settings file is not valid JSON")
        if not isinstance(settings, dict):
            return Unknown("Claude settings file is not a JSON object")
        if "autoUpdatesChannel" in settings:
            value = settings["autoUpdatesChannel"]
            if not isinstance(value, str) or value not in CHANNELS:
                return Unknown("Claude settings name an unsupported release channel")
            channel = value
    return channel


HOST_NOTICES = Notices()


def start_host(
    roster: Roster,
    changed: Callable[[], None],
    failed: Callable[[str], None] | None = None,
) -> Unknown | None:
    """Observe at host startup, never from a session start or listing."""
    provider = next((p for p in roster.providers if p.name == "claude"), None)
    if provider is None or provider.bin is None:
        return None
    # Only the native installer's channel describes this installation.
    try:
        if "/.local/share/claude/versions/" not in str(
            provider.bin.resolve(strict=True)
        ):
            return None
    except OSError, RuntimeError:
        return Unknown("Claude binary identity is unavailable at host startup")
    # A repository can select either native channel after host startup.
    for channel in sorted(CHANNELS):
        HOST_NOTICES.start(provider.bin, channel, changed, failed)
    return None


def read_host(
    roster: Roster, provider_name: str, account: str | None, cwd: str
) -> Notice | Unknown | None:
    provider = next(
        (p for p in roster.providers if p.name == provider_name == "claude"), None
    )
    if provider is None or provider.bin is None:
        return None
    channel = channel_read(provider, account, cwd)
    if isinstance(channel, Unknown):
        return channel
    status = HOST_NOTICES.status_read(provider.bin, channel)
    if isinstance(status, Unknown):
        return status
    return dict(status.notice) if isinstance(status, Available) else None
