"""`nspawn session serve`: the only writer of the session journal.

The loop polls by `os.stat` (stdlib only): each tracked source every
`hot_s`, and a scan of every vendor home every `cold_s`. Startup queues
existing files. Live writes precede historical files. A source is read from its
committed offset, whole lines only, at most `read_bytes` per cycle, and the
sources take turns. Each batch of at most `batch_lines` lines commits its rows,
its usage, its new offset and its translator state in ONE transaction, and the
offset moves by a compare-and-set on the old one: a second writer or a replay
rolls back and adds nothing. A crash loses at most the batch in flight.

Text goes through the roster's credential needles before it is stored. When
the roster, a secrets directory or a token file that is there cannot be read,
the needles are not complete and the writer writes nothing until a cold scan
reads them all. An account with no token file has no needle and pauses nothing.
Images are files under `~/.nott/sessions/media/`, written before the row that
names them. Below `disk_floor` free bytes on `~/.nott` the writer stops.

A batch whose lines the translator or the store refuses fails its source
only: the lines before the bad line are kept, the source gets one failed
status, and the other sources go on. An error of the store itself or of the
disk (busy, refused, damaged) fails no source: it stops the cycle, and the
next cycle tries again from the committed offset.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import signal
import stat
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType
from typing import Any, Final

import nott_database

from .. import threads
from ..owner_roster import (
    Provider,
    Roster,
    RosterAbsent,
    TokenFileRefused,
    claude_config_dir,
    read_token_file,
)
from ..owner_roster import read_roster as owner_read_roster
from ..redact import SecretNeedles
from ..registry import RegistryError, effort_read, open_store, store_path
from ..vendor_home import AUTH_MAX_BYTES, open_owner_path
from . import (
    LINE_MAX_BYTES,
    TRANSLATORS,
    Context,
    Media,
    Row,
    StatusRow,
    Translator,
    Usage,
    activity,
    dumps,
)
from .claude import (
    WORKFLOW_KEYS,
    WorkflowListingRefused,
    session_folder,
    workflow_agents,
    workflow_events,
    workflow_journal,
)

#: The state key of the terminal certificate of a Workflow agent. It holds
#: the agent id, the run id and the call id of the ended launch. It also holds
#: the device, inode and size of the agent file, and the number of its
#: `started` lines in the Workflow journal at certification.
CERTIFICATE: Final = "workflow_certificate"
#: The needle bytes kept across token rotations before the writer stops.
NEEDLE_MAX_BYTES: Final = 16 * AUTH_MAX_BYTES
#: How far before the newest `updated_at` seen a cold cycle reads runs again:
#: a run another process stamped earlier can commit after a later one.
RUN_WINDOW_MS: Final = 60_000
#: The runs updated since a time, by the journal's `journal_run_updated` index.
RUN_CHANGES_SQL: Final = (
    "SELECT run_id, backend, thread_id, phase, updated_at FROM runs "
    "WHERE updated_at >= ? ORDER BY updated_at"
)
#: The journal id of a parent session in one home, by its vendor id and by its
#: id: two searches, as a UNION merges them over every session of the vendor.
PARENT_SQL: Final = (
    "SELECT id FROM journal_session WHERE vendor = ? AND vendor_id = ? AND home = ?",
    "SELECT id FROM journal_session WHERE vendor = ? AND id = ? AND home = ?",
)
#: A session's status, its last seq and the reason of its last status row.
STATUS_SQL: Final = (
    "SELECT status, seq, (SELECT json_extract(row, '$.reason') FROM journal_row "
    "WHERE vendor = ? AND session = ? AND type = 'status' ORDER BY seq DESC LIMIT 1) "
    "FROM journal_session WHERE vendor = ? AND id = ?"
)
#: The list metadata a session's `session` row holds.
METADATA_SQL: Final = (
    "SELECT json_extract(row, '$.metadata') FROM journal_row "
    "WHERE vendor = ? AND session = ? AND type = 'session' ORDER BY seq LIMIT 1"
)
#: The largest Workflow journal read for the end of its agents. The largest
#: one on SERVER on 2026-10-28 held 2.3 MB.
WORKFLOW_JOURNAL_MAX_BYTES: Final = 64 * 1024 * 1024
#: The run phases a status reads as a turn that is running.
ACTIVE_PHASES: Final = ("pending", "spawning", "running", "recovering", "idle")
#: The fields of an activity that each poll checks (`Daemon._fence`).
FENCE: Final = ("device", "inode", "size", "mtime_ns", "rebuild")


class Refused(Exception):
    """The daemon cannot start: the message names the path and the fix."""


class Conflict(Exception):
    """Another writer moved this source's offset first."""


class WorkflowJournalRefused(Exception):
    """A Workflow journal exists but cannot give trusted terminal evidence. A
    refusal sets no status: the other status rules give the agent's status,
    and the message is the reason of that status row."""


class Untranslated(Exception):
    """The translator raised on a line of one source; `__cause__` is its error."""


def _log(line: str) -> None:
    sys.stderr.write(line + "\n")
    sys.stderr.flush()


@dataclass
class Settings:
    #: The vendor homes to read. None selects default, roster and recorded homes.
    homes: Sequence[tuple[str | None, Path]] | None
    nott_root: Path
    store: Path
    roster: Roster | None = None
    #: Reads the roster (None: no roster is declared) or raises. When given, the
    #: daemon calls it until it succeeds, on each cold scan, and writes nothing
    #: before.
    read_roster: Callable[[], Roster | None] | None = None
    clock: Callable[[], float] = time.time
    batch_lines: int = 1000
    read_bytes: int = 8 * 1024 * 1024
    hot_s: float = 0.5
    cold_s: float = 5.0
    log_s: float = 60.0
    disk_floor: int = 1 << 30
    log: Callable[[str], None] = _log


@dataclass(frozen=True)
class Parsed:
    """What the status rules read from one translator state."""

    #: (ended, failed, waiting).
    flags: tuple[bool, bool, bool]
    #: The tracked Workflow runs of a main session, by run (`WORKFLOW_KEYS`).
    workflows: dict[str, dict[str, Any]]
    #: A Workflow tool call of the main session waits for its result.
    pending: bool
    #: The terminal certificate of a Workflow agent (`CERTIFICATE`).
    certificate: dict[str, Any] | None
    #: The byte offset of the last turn line (state key `turn_at`). None
    #: before the first turn line, and in a state of an older release.
    turn_at: int | None
    #: The committed witness of a sidecar read (state key `sidecar`): the
    #: mark and its bound (`Source.seen`, `Source.bound`). None when absent.
    witness: tuple[tuple[int, int, int, int, int, int], int] | None


@dataclass
class Closing:
    """The closing sequence of one ended Workflow run, while it runs. It
    keeps the launch and the end position that it is for. It also keeps the
    listing of the run folder that followed the parent read with that end,
    or the reason of the listing that failed."""

    mark: tuple[str, int]
    #: The agent ids of the listing. None after a failed listing.
    listing: tuple[str, ...] | None
    #: The reason of the failed listing (`WorkflowListingRefused`). None
    #: after a complete listing.
    refused: str | None = None


@dataclass(frozen=True)
class Observed:
    """What one complete read of a Workflow journal gives (`workflow_events`)."""

    #: The status of each agent whose end line the journal holds.
    ends: dict[str, str]
    #: The number of `started` lines of each agent.
    starts: dict[str, int]
    #: The bytes end with a complete record.
    whole: bool


@dataclass(frozen=True)
class Held:
    """The unread suffix of the last window of one read cursor
    (`Daemon._take`, `Daemon._hold`)."""

    #: The offset of the first held byte in the file.
    offset: int
    #: The device, inode, size, modification and change times in nanoseconds
    #: of the `fstat` of the read that took the bytes.
    mark: tuple[int, int, int, int, int]
    data: bytes


@dataclass
class Source:
    translator: Translator
    home: Path
    alias: str | None
    path: Path
    session: str
    parent: str | None
    consumed: int = 0
    size: int = 0
    state: str = "{}"
    activity: str | None = None
    read_stat: os.stat_result | None = None
    deferred: bool = False
    new: bool = True
    #: The last read stopped before the source's end (a budget or a limit):
    #: its status waits until it catches up.
    behind: bool = False
    native_id: str = ""
    historical: bool = False
    modified: int = 0
    metadata_version: int = 1
    backfill_end: int | None = None
    observed_size: int = 0
    #: The offset up to which the source's links are committed.
    linked: int = 0
    #: The recorded facts (`Daemon._facts`); None until read, and after a run,
    #: a child row or a parent session that can move them.
    facts: dict[str, Any] | None = None
    #: The (size, modified, metadata_version) its list metadata was computed
    #: for; None after a change that the metadata must follow.
    mark: tuple[int, int, int] | None = None
    #: The state string that `parsed` comes from, and what the status rules
    #: read from it. The state is decoded once for each change of it.
    parsed: tuple[str, Parsed] | None = None
    #: The (device, inode, size) of the file at this cycle's `lstat`. None
    #: before it, when it failed, and when the void of its certificate met a
    #: conflict (`Daemon._certificate`).
    stamp: tuple[int, int, int] | None = None
    #: The mark of the `finished: true` sidecar at the last read: the device,
    #: the inode, the size, the modification and change times in nanoseconds,
    #: and the first 8 bytes of the SHA-256 of its bytes. None before a read.
    seen: tuple[int, int, int, int, int, int] | None = None
    #: The size of the first `lstat` after the daemon first read the mark
    #: `seen`. Each byte at or after it came after that read. None until that
    #: `lstat`, and after a new mark.
    bound: int | None = None
    #: The activity string that the fence comes from, and its fields `FENCE`
    #: that each poll checks (`Daemon._fence`). The activity is decoded once
    #: for each change of it.
    fence: tuple[str, dict[str, Any]] | None = None
    #: The (consumed, size, mtime_ns, ctime_ns) of the last read that reached
    #: the end of the file and found no whole record after `consumed`. A poll
    #: that sees the same values does not read the source again.
    tail: tuple[int, int, int, int] | None = None

    @property
    def rel(self) -> str:
        return str(self.path.relative_to(self.home))


@dataclass
class Metrics:
    statted: int = 0
    read: int = 0
    rows: int = 0
    cycles: list[float] = field(default_factory=list)
    transaction: float = 0.0
    #: The held bytes that the bound deleted (`Daemon._hold`).
    deleted: int = 0


def private_directory(path: Path) -> None:
    """Create `path` 0700, or refuse one that is not this user's alone."""
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    except OSError as error:
        raise Refused(f"{path} cannot be created: {error.strerror}") from None
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise Refused(
            f"{path} must be a directory of this user with mode 0700 (chmod 700 {path})"
        )


def insert_rows(
    connection: nott_database.Connection,
    source: Source,
    rows: list[Row],
    usage: list[Usage],
    redact: Callable[[Any], Any],
    now_ms: int,
) -> int:
    """Store `rows` after the session's last `seq`, and upsert each call's
    usage: a line with a higher `src` wins. Inside the caller's transaction."""
    vendor = source.translator.vendor
    found = nott_database.query(
        connection,
        "SELECT seq FROM journal_session WHERE vendor = ? AND id = ?",
        (vendor, source.session),
    )
    seq = int(found[0][0])
    cwd: str | None = None
    #: Rows of one line with one type and one key (two text blocks of one
    #: message, say): the second and later get `#<n>` after the key.
    seen: Counter[tuple[int, str, str]] = Counter()
    for row in rows:
        seq += 1
        body = redact(row.body(seq))
        if row.TYPE == "session":
            cwd = body.get("cwd")
        key = str(seq) if row.TYPE == "status" else row.key()
        repeat = seen[(row.src, row.TYPE, key)]
        seen[(row.src, row.TYPE, key)] += 1
        connection.execute(
            "INSERT INTO journal_row (vendor, session, seq, type, time, src, mid, "
            "key, row) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                vendor,
                source.session,
                seq,
                row.TYPE,
                row.time,
                row.src,
                getattr(row, "mid", None),
                f"{key}#{repeat}" if repeat else key,
                dumps(body),
            ),
        )
    model: str | None = None
    for item in usage:
        connection.execute(
            "INSERT INTO journal_usage (vendor, session, mid, model, input, "
            "cache_read, cache_write, output, reasoning, src) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (vendor, session, mid) DO UPDATE SET model = excluded.model, "
            "input = excluded.input, cache_read = excluded.cache_read, "
            "cache_write = excluded.cache_write, output = excluded.output, "
            "reasoning = excluded.reasoning, src = excluded.src "
            "WHERE excluded.src > journal_usage.src",
            (
                vendor,
                source.session,
                item.mid,
                item.model,
                item.input,
                item.cache_read,
                item.cache_write,
                item.output,
                item.reasoning,
                item.src,
            ),
        )
        if item.model is not None and item.model != "<synthetic>":
            model = item.model
    # Activity time comes from vendor rows: a status row carries the writer's clock.
    last = next((row.time for row in reversed(rows) if row.TYPE != "status"), None)
    connection.execute(
        "UPDATE journal_session SET seq = ?, changed = ?, "
        "time = coalesce(?, time), cwd = coalesce(?, cwd), model = coalesce(?, model) "
        "WHERE vendor = ? AND id = ?",
        (seq, now_ms, last, cwd, model, vendor, source.session),
    )
    return len(rows)


class Daemon:
    """One writer's cursor over every tracked source."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.time_started = settings.clock()
        sessions = settings.nott_root / "sessions"
        private_directory(sessions)
        self.media = sessions / "media"
        private_directory(self.media)
        self.connection = open_store(settings.store)
        self.sources: dict[tuple[str, str, str], Source] = {}
        #: Sources with a failed `journal_source` row: never tracked again.
        self.failed: set[tuple[str, str, str]] = set()
        self.roster = settings.roster
        #: Whether the roster is read: always without `read_roster`.
        self.read = settings.read_roster is None
        #: The homes that runs recorded, and the last `run_events` rowid read.
        self.recorded: dict[Path, None] = {}
        self.events_seen = 0
        self.homes: list[tuple[str | None, Path]] = []
        self.aliases: dict[Path, str | None] = {}
        self._homes()
        self.needles = SecretNeedles.none()
        self.tokens: dict[Path, tuple[int, int, int, int]] = {}
        self.halted: str | None = None
        #: Why the needles are not complete: nothing is written until it is None.
        self.paused: str | None = None
        #: The class of the store or disk error that stopped the last cycle.
        self.stalled: str | None = None
        self.unknown: Counter[str] = Counter()
        self.metrics = Metrics()
        self.turn = 0
        self.last_cold = float("-inf")
        #: Each session's last status, so an unchanged status opens no write.
        self.statuses: dict[tuple[str, str], tuple[str | None, str | None]] = {}
        #: Each session's metadata as it was last committed, so an unchanged
        #: session opens no write.
        self.written: dict[tuple[str, str], dict[str, Any]] = {}
        #: Tracked sources by (vendor, session) and by (vendor, vendor id).
        self.by_session: dict[tuple[str, str], Source] = {}
        self.by_native: dict[tuple[str, str], list[Source]] = {}
        #: Each tracked or failed source by its resolved home, so a home
        #: reached by a second path is not read twice.
        self.resolved: set[tuple[str, str, str]] = set()
        self.real_homes: dict[str, str] = {}
        #: Sources whose parent had no session, by (vendor, parent, home).
        self.unlinked: dict[tuple[str, str, str], set[tuple[str, str, str]]] = {}
        #: The runs of the last window, as (phase, updated_at), and the newest
        #: `updated_at` read; None before the first cold cycle.
        self.runs: dict[str, tuple[str, int]] = {}
        self.runs_seen: int | None = None
        #: Sessions with no row yet: no status row until their first row.
        self.empty: set[tuple[str, str]] = set()
        #: Each Workflow journal's (inode, size, mtime) and what it records for
        #: its agents, so an unchanged journal is not read again.
        self.workflow_journals: dict[Path, tuple[tuple[int, int, int], Observed]] = {}
        #: The closing sequence of each ended Workflow run while it runs, by
        #: the source key of its main session and by run. It is in memory
        #: only: after a restart, a sequence starts again.
        self.closing: dict[tuple[str, str, str], dict[str, Closing]] = {}
        #: The held suffix of each read cursor, by source key and cursor:
        #: `import` (`_read`) or `pass` (`_backfill`). The first entry is the
        #: oldest. `held_bytes` is the sum of the lengths of the held data.
        self.held: dict[tuple[tuple[str, str, str], str], Held] = {}
        self.held_bytes = 0

    # -- lifecycle -----------------------------------------------------------------

    def start(self) -> None:
        """Resume committed sources and queue the files written before startup."""
        self._needles()
        vendors = {translator.vendor: translator for translator in TRANSLATORS}
        stale: list[tuple[str, str, str]] = []
        for row in nott_database.query(
            self.connection,
            "SELECT s.vendor, s.home, s.path, s.session, s.size, s.consumed, "
            "s.state, s.status, s.metadata_version, s.linked, j.modified, j.seq, s.activity "
            "FROM journal_source s LEFT JOIN journal_session j "
            "ON j.vendor = s.vendor AND j.id = s.session",
        ):
            if row[7] is not None:
                self.failed.add((row[0], row[1], row[2]))
                self.resolved.add(self._resolved((row[0], row[1], row[2])))
                continue
            version = int(row[8])
            native = row[12]
            # A source without retained identity keeps its activity unknown,
            # so it keeps its stored version: the pass can give it only links
            # and metadata (a stored version 0, or `linked` behind below).
            if native is not None:
                saved = json.loads(native)
                if saved["size"] != int(row[5]) or saved.get("rebuild"):
                    version = 0
            behind = int(row[9]) < int(row[5])
            if behind and version == 1:
                stale.append((row[0], row[1], row[2]))
            if behind:
                # Links stop at `linked`: a release without links (a rollback,
                # then this release again) or the pass itself left rows past
                # it. The pass reads up to `consumed`, whatever it had done.
                version = 0
            if row[11] is None or int(row[11]) < 0:
                # No row yet: no status, and no metadata until its first row.
                self.empty.add((row[0], row[3]))
            translator = vendors.get(row[0])
            if translator is None:
                continue
            home = Path(row[1])
            identity = translator.identity(home, home / row[2])
            retained = (
                nott_database.query(
                    self.connection,
                    "SELECT vendor_id, parent FROM journal_session WHERE vendor = ? AND id = ?",
                    (row[0], row[3]),
                )
                if identity is None
                else []
            )
            source = Source(
                translator,
                home,
                self.aliases.get(home),
                home / row[2],
                row[3],
                (retained[0][1] if retained else None)
                if identity is None
                else identity.parent,
                consumed=int(row[5]),
                size=int(row[4]),
                state=row[6],
                activity=native,
                new=False,
                native_id=(retained[0][0] or row[3] if retained else row[3])
                if identity is None
                else identity.session,
                metadata_version=version,
                backfill_end=int(row[5]) if version == 0 else None,
                observed_size=int(row[4]),
                linked=int(row[9]),
                modified=int(row[10] or 0),
            )
            self._track((row[0], row[1], row[2]), source)
        if stale:
            with nott_database.write(self.connection):
                for key in stale:
                    self.connection.execute(
                        "UPDATE journal_source SET metadata_version = 0 "
                        "WHERE vendor = ? AND home = ? AND path = ?",
                        key,
                    )
        self._scan(historical=True)

    def _resolved(self, key: tuple[str, str, str]) -> tuple[str, str, str]:
        home = self.real_homes.get(key[1])
        if home is None:
            home = self.real_homes[key[1]] = str(Path(key[1]).resolve())
        return (key[0], home, key[2])

    def _track(self, key: tuple[str, str, str], source: Source) -> None:
        self.sources[key] = source
        self.by_session[(key[0], source.session)] = source
        self.by_native.setdefault(
            (key[0], source.native_id or source.session), []
        ).append(source)
        self.resolved.add(self._resolved(key))

    def _untrack(self, key: tuple[str, str, str], failed: bool) -> None:
        source = self.sources.pop(key, None)
        self.closing.pop(key, None)
        for cursor in ("import", "pass"):
            self._drop((key, cursor))
        if failed:
            self.failed.add(key)
        else:
            self.resolved.discard(self._resolved(key))
        if source is None:
            return
        if self.by_session.get((key[0], source.session)) is source:
            del self.by_session[(key[0], source.session)]
        native = self.by_native.get((key[0], source.native_id or source.session), [])
        if source in native:
            native.remove(source)

    def _touch(self, vendor: str, native: str) -> None:
        """The facts and metadata of `native`'s sources follow a change."""
        for source in self.by_native.get((vendor, native), []):
            source.facts = None
            source.mark = None

    def _homes(self) -> list[Path]:
        """Set the homes and their aliases from the settings or the roster; the
        homes that were not known before."""
        homes = self.settings.homes
        if homes is None:
            homes = [(None, claude_config_dir())]
            if self.roster is not None:
                homes = list(threads.claude_homes(self.roster))
                # Every Claude-client route's accounts, beside the `claude` provider's.
                homes.extend(
                    (alias, claude_config_dir(account))
                    for provider in self.roster.providers
                    if provider.kind == "claude"
                    for alias, account in provider.accounts.items()
                    if account.home is not None
                )
            # Only the events appended since the last read: the ledger only grows.
            last = nott_database.query(
                self.connection, "SELECT max(rowid) FROM run_events"
            )[0][0]
            if last is not None and int(last) > self.events_seen:
                for row in nott_database.query(
                    self.connection,
                    "SELECT json_extract(payload, '$.journal_context.home') "
                    "FROM run_events WHERE rowid > ? AND rowid <= ? "
                    "AND kind = 'lifecycle' "
                    "AND json_type(payload, '$.journal_context.home') = 'text'",
                    (self.events_seen, int(last)),
                ):
                    self.recorded[Path(row[0])] = None
                self.events_seen = int(last)
            homes.extend((None, path) for path in self.recorded)
            runtime = Path(
                os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.geteuid()}")
            )
            for root in (
                self.settings.nott_root / "native",
                self.settings.nott_root / "run/nott-node/native",
                runtime / "nott-node/native",
            ):
                homes.extend((None, path) for path in root.glob("*/native"))
            # One entry per directory, in the form first named: the stored
            # rows keep the form a home was configured with.
            unique: dict[Path, tuple[str | None, Path]] = {}
            for alias, home in homes:
                first = unique.setdefault(home.resolve(), (alias, home))
                if first[0] is None and alias is not None:
                    unique[home.resolve()] = (alias, first[1])
            homes = list(unique.values())
        known = {home for _, home in self.homes}
        self.homes = list(homes)
        self.aliases = {home: alias for alias, home in self.homes}
        for source in self.sources.values():
            source.alias = self.aliases.get(source.home)
        return [home for _, home in self.homes if home not in known]

    def close(self) -> None:
        self.connection.close()

    # -- one cycle -----------------------------------------------------------------

    def cycle(self, cold: bool | None = None, batches: int | None = None) -> int:
        """Translate what each tracked source gained; on a cold cycle, find new
        and changed sources and read liveness first. Returns the rows written.
        An error of the store or the disk stops the cycle and fails no source."""
        try:
            written = self._cycle(cold, batches)
        except Exception as error:
            if not _shared(error):
                raise
            stalled: str | None = type(error).__name__
            written = 0
        else:
            stalled = None
        if stalled != self.stalled:
            self.settings.log(
                f"journal: the store or the disk refused the cycle ({stalled}); "
                "the next cycle tries again"
                if stalled is not None
                else "journal: the store and the disk accept writes again"
            )
        self.stalled = stalled
        return written

    def _cycle(self, cold: bool | None, batches: int | None) -> int:
        now = time.monotonic()
        if cold or (cold is None and now - self.last_cold >= self.settings.cold_s):
            self.last_cold = now
            self._needles()
            self._scan()
            cold = True
        if self.halted is not None:
            self._stop(self.halted)
            return 0
        if self.paused is not None:
            self._pause()
            return 0
        budget = [batches if batches is not None else -1]
        written = 0
        tracked = list(self.sources.values())
        if tracked:
            start = self.turn % len(tracked)
            tracked = tracked[start:] + tracked[:start]
            self.turn += 1
        tracked.sort(
            key=lambda source: (
                source.historical,
                -source.modified if source.historical else 0,
            )
        )
        changed: list[Source] = []
        historical: list[tuple[Source, int]] = []
        for source in tracked:
            source.deferred = False
            source.stamp = None
            try:
                info = source.path.lstat()
            except FileNotFoundError:
                if not source.new:
                    self._fail(source, "transcript unavailable")
                continue
            except OSError:
                self._invalidate_activity(source, "transcript unavailable")
                continue
            source.stamp = (info.st_dev, info.st_ino, info.st_size)
            # The `lstat` is an observation of rule 4: a certificate that it
            # voids goes now, also before the source fails for its size or
            # inode. The journal of the run is not read yet, so the readers
            # compare the `started` count (`_certificate`).
            self._certificate(source, None)
            if source.stamp is None:
                # The void of the agent's certificate met a conflict: the
                # committed state applies at a later cycle, and the source
                # keeps its status until then.
                continue
            size = info.st_size
            if source.seen is not None and source.bound is None:
                source.bound = size
            source.modified = info.st_mtime_ns // 1_000_000
            self.metrics.statted += 1
            if size < source.consumed:
                self._fail(source, "source shrank")
                continue
            retained = self._fence(source)
            try:
                activity.check(info, retained)
            except activity.ActivityRefused as error:
                self._fail(source, str(error))
                continue
            # A partial rebuild checks its own snapshot before the next batch.
            if (
                retained is not None
                and not retained.get("rebuild")
                and info.st_mtime_ns != retained["mtime_ns"]
                and size <= source.size
                and json.loads(source.state).get("backfill", {}).get("activity") is None
            ):
                self._invalidate_activity(
                    source,
                    "transcript changed without growth",
                    defer=source.metadata_version == 1,
                )
            source.behind = size > source.consumed and source.tail != (
                source.consumed,
                size,
                info.st_mtime_ns,
                info.st_ctime_ns,
            )
            if source.historical and size > source.observed_size:
                source.historical = False
            source.observed_size = size
            if source.historical:
                historical.append((source, size))
                continue
            if source.behind and budget[0] != 0:
                added = self._read(source, size, budget)
                written += added
                changed.append(source)
            if not source.new and not source.behind and source.metadata_version == 1:
                source.historical = False
        # One existing batch is the background slot between hot polls; the
        # metadata pass takes the newest session first.
        background = [1] if batches is None else budget
        pending = [
            s
            for s in self.sources.values()
            if s.metadata_version < 1 and not s.deferred
        ]
        pending.sort(key=lambda source: -source.modified)
        for source in pending:
            if background[0] == 0:
                break
            self._backfill(source, background)
        for source, size in historical:
            if source.behind and background[0] != 0:
                written += self._read(source, size, background)
                changed.append(source)
            if not source.new and not source.behind:
                source.historical = False
        if cold:
            self._run_changes()
        self._statuses(self.sources.values() if cold else changed)
        return written

    def _run_changes(self) -> None:
        """The facts of each session whose run changed since the last cold
        cycle follow it: a new run, a phase, an account."""
        since = -1 if self.runs_seen is None else self.runs_seen - RUN_WINDOW_MS
        window: dict[str, tuple[str, int]] = {}
        for run_id, backend, thread, phase, updated in nott_database.query(
            self.connection, RUN_CHANGES_SQL, (since,)
        ):
            mark = (str(phase), int(updated))
            window[run_id] = mark
            if self.runs.get(run_id) != mark and thread:
                self._touch(backend, thread)
            self.runs_seen = max(self.runs_seen or 0, int(updated))
        self.runs = window

    def _pause(self) -> None:
        """While the needles are not complete: a failed status, reason
        `secrets`, on each session whose source has lines to write."""
        for source in list(self.sources.values()):
            try:
                size = source.path.lstat().st_size
            except OSError:
                continue
            if size > source.consumed and not source.new:
                self._status(source, "failed", "secrets")

    def _discover(self) -> list[tuple[Translator, Path, Path]]:
        found: list[tuple[Translator, Path, Path]] = []
        for _, home in self.homes:
            for translator in TRANSLATORS:
                found.extend(
                    (translator, home, path) for path in translator.sources(home)
                )
        return found

    def _scan(self, historical: bool = False) -> None:
        self._homes()
        for translator, home, path in self._discover():
            key = (translator.vendor, str(home), str(path.relative_to(home)))
            if (
                key in self.sources
                or key in self.failed
                or self._resolved(key) in self.resolved
            ):
                continue
            try:
                info = path.lstat()
                size = info.st_size
            except OSError:
                continue
            if size == 0:
                continue
            identity = translator.identity(home, path)
            if identity is None:
                continue
            source = Source(
                translator,
                home,
                self.aliases.get(home),
                path,
                identity.session,
                identity.parent,
                native_id=identity.session,
                historical=historical or info.st_mtime <= self.time_started,
                modified=info.st_mtime_ns // 1_000_000,
                observed_size=size,
            )
            feeds = nott_database.query(
                self.connection,
                "SELECT home FROM journal_session WHERE vendor = ? AND id = ?",
                (translator.vendor, source.session),
            )
            other = self.by_session.get((translator.vendor, source.session))
            if (
                feeds
                and feeds[0][0] != str(home)
                or other is not None
                and other.home != home
            ):
                source.session = f"{source.session}.{hashlib.sha256(str(home.resolve()).encode()).hexdigest()[:16]}"
            if self._mismatch(source):
                self.failed.add(key)
                self.resolved.add(self._resolved(key))
                continue
            self._track(key, source)

    def _mismatch(self, source: Source) -> bool:
        """A second source for a session another source feeds: one failed row
        on that source, and it is not tracked."""
        vendor = source.translator.vendor
        feeds = nott_database.query(
            self.connection,
            "SELECT home, path FROM journal_source WHERE vendor = ? AND session = ? "
            "AND NOT (home = ? AND path = ?)",
            (vendor, source.session, str(source.home), source.rel),
        )
        if not feeds and (vendor, source.session) not in self.by_session:
            return False
        with nott_database.write(self.connection):
            self.connection.execute(
                "INSERT INTO journal_source (vendor, home, path, session, size, "
                "consumed, state, status, reason) VALUES (?, ?, ?, ?, 0, 0, '{}', "
                "'failed', 'source mismatch') ON CONFLICT DO NOTHING",
                (vendor, str(source.home), source.rel, source.session),
            )
        self.settings.log(
            f"journal: {source.path} feeds {vendor} {source.session}, which "
            "another source already feeds: not tracked (source mismatch)"
        )
        return True

    # -- reading and writing ----------------------------------------------------------

    def _invalidate_activity(
        self, source: Source, reason: str, *, defer: bool = True
    ) -> None:
        """Retain identity and require a full activity rebuild before publishing."""
        source.deferred = defer
        if source.activity is None:
            return
        native = json.loads(source.activity)
        native.update(reason=reason, rebuild=True)
        state = json.loads(source.state)
        state.pop("backfill", None)
        # The committed Workflow lifecycle came from bytes that changed, so
        # it is no longer evidence: the runs are no longer tracked.
        for name in WORKFLOW_KEYS:
            state.pop(name, None)
        encoded, saved = dumps(native), dumps(state)
        with nott_database.write(self.connection):
            moved = self.connection.execute(
                "UPDATE journal_source SET activity = ?, state = ?, metadata_version = 0 "
                "WHERE vendor = ? AND home = ? AND path = ? AND consumed = ? AND state = ? "
                "AND activity = ? AND status IS NULL",
                (
                    encoded,
                    saved,
                    source.translator.vendor,
                    str(source.home),
                    source.rel,
                    source.consumed,
                    source.state,
                    source.activity,
                ),
            ).rowcount
        if moved != 1:
            self._reload(source)
            return
        source.activity, source.state = encoded, saved
        source.metadata_version = 0
        source.backfill_end = source.consumed
        source.mark = None

    def _read(self, source: Source, size: int, budget: list[int]) -> int:
        """Whole lines from the committed offset, at most `read_bytes` (one
        longer line is read whole), committed in batches."""
        source.tail = None
        try:
            descriptor = open_owner_path(source.home, source.path)
        except ValueError:
            self._fail(source, "transcript unavailable")
            return 0
        written = 0
        start = source.consumed
        try:
            info = os.fstat(descriptor)
            activity.check(info, self._fence(source))
            if info.st_size < size:
                raise activity.ActivityRefused("source shrank")
            data, length = self._take(source, "import", descriptor, info, start, size)
            after = os.fstat(descriptor)
            path_info = source.path.lstat()
            if (info.st_dev, info.st_ino) != (path_info.st_dev, path_info.st_ino):
                raise activity.ActivityRefused("transcript replaced")
            if after.st_size < info.st_size or (
                after.st_size == info.st_size
                and (after.st_mtime_ns, after.st_ctime_ns)
                != (info.st_mtime_ns, info.st_ctime_ns)
            ):
                raise activity.ActivityRefused("transcript changed while reading")
            source.read_stat = info
        except activity.ActivityRefused as error:
            self._fail(source, str(error))
            return 0
        except OSError:
            self._drop((self._key(source), "import"))
            self._invalidate_activity(source, "transcript unavailable")
            return 0
        finally:
            os.close(descriptor)
        source.behind = length < size - start
        end = data.rfind(b"\n", 0, length)
        lines: list[tuple[int, bytes]] = []
        at = start
        for raw in data[: end + 1].split(b"\n")[:-1]:
            lines.append((at, raw))
            at += len(raw) + 1
        for first in range(0, len(lines), self.settings.batch_lines):
            if budget[0] == 0 or self.halted is not None or not self._room(source):
                source.behind = True
                break
            batch = lines[first : first + self.settings.batch_lines]
            last_src, last_raw = batch[-1]
            try:
                written += self._commit(
                    source, batch, last_src + len(last_raw) + 1, size
                )
            except Conflict:
                self._reload(source)
                source.behind = True
                return written
            except Exception as error:
                if _shared(error):
                    raise
                return written + self._salvage(source, batch, size)
            if budget[0] > 0:
                budget[0] -= 1
        rest = data[source.consumed - start :]
        if rest and (b"\n" in rest or start + len(data) < info.st_size):
            self._hold(source, "import", Held(source.consumed, _mark(info), rest))
        elif start + len(data) == info.st_size:
            # The read reached the end of the file and found no whole record
            # after `consumed`: the poll does not read that file again until
            # it changes.
            source.tail = (
                source.consumed,
                info.st_size,
                info.st_mtime_ns,
                info.st_ctime_ns,
            )
        return written

    def _take(
        self,
        source: Source,
        cursor: str,
        descriptor: int,
        info: os.stat_result,
        offset: int,
        stop: int,
    ) -> tuple[bytes, int]:
        """The window of one read cursor from `offset`, with the bounds of
        main: at most `read_bytes` up to `stop`, or one longer record up to
        `LINE_MAX_BYTES`. Returns the bytes from `offset` and the length of
        the window. The bytes can go past the window when the held suffix of
        the cursor is longer: the caller parses only the window and holds
        the rest. The held suffix serves the bytes that it covers only at its
        offset, while the file keeps the mark of the read that took them.
        Each other case deletes it. Each pread starts at the end of the bytes
        before it, and the minute line counts each byte that a pread
        returns."""
        key = (self._key(source), cursor)
        held = self.held.pop(key, None)
        data = b""
        if held is not None:
            self.held_bytes -= len(held.data)
            if held.offset == offset and held.mark == _mark(info):
                data = held.data[: stop - offset]
        want = min(self.settings.read_bytes, stop - offset)
        while True:
            if len(data) < want:
                chunk = os.pread(descriptor, want - len(data), offset + len(data))
                self.metrics.read += len(chunk)
                data += chunk
                if len(data) < want:
                    # The file ends before `stop`: the fence after the read
                    # refuses a file that shrank.
                    return data, len(data)
            if (
                data.find(b"\n", 0, want) >= 0
                or offset + want >= stop
                or want >= LINE_MAX_BYTES
            ):
                return data, want
            want = min(want * 2, stop - offset, LINE_MAX_BYTES)

    def _hold(self, source: Source, cursor: str, held: Held) -> None:
        """Keep the unread suffix of one read cursor. All held suffixes
        together keep at most 2 x `read_bytes` bytes: a longer suffix keeps
        its first bytes, and the oldest suffixes of the other cursors go
        first. This is the one place that the bound deletes held bytes, and
        the minute line counts each of them."""
        bound = 2 * self.settings.read_bytes
        if len(held.data) > bound:
            self.metrics.deleted += len(held.data) - bound
            held = Held(held.offset, held.mark, held.data[:bound])
        key = (self._key(source), cursor)
        self._drop(key)
        while self.held_bytes + len(held.data) > bound:
            oldest = self.held.pop(next(iter(self.held)))
            self.held_bytes -= len(oldest.data)
            self.metrics.deleted += len(oldest.data)
        self.held[key] = held
        self.held_bytes += len(held.data)

    def _drop(self, key: tuple[tuple[str, str, str], str]) -> None:
        """Delete the held suffix of one read cursor, with no count: the
        bound did not delete it."""
        held = self.held.pop(key, None)
        if held is not None:
            self.held_bytes -= len(held.data)

    @staticmethod
    def _key(source: Source) -> tuple[str, str, str]:
        return (source.translator.vendor, str(source.home), source.rel)

    def _salvage(
        self, source: Source, batch: list[tuple[int, bytes]], size: int
    ) -> int:
        """After a batch was refused: commit its lines one at a time up to the
        line that is refused, then fail the source with that error's class."""
        written = 0
        for src, raw in batch:
            try:
                written += self._commit(source, [(src, raw)], src + len(raw) + 1, size)
            except Conflict:
                self._reload(source)
                return written
            except Exception as error:
                if _shared(error):
                    raise
                cause = error.__cause__ if isinstance(error, Untranslated) else error
                self._fail(source, type(cause).__name__)
                return written
        return written

    def _commit(
        self, source: Source, batch: list[tuple[int, bytes]], end: int, size: int
    ) -> int:
        state: dict[str, Any] = json.loads(source.state)
        native = json.loads(source.activity) if source.activity else None
        if native is not None and (
            native["size"] != source.consumed or native.get("rebuild")
        ):
            # Preserve the retained identity until the bounded rebuild finishes.
            native = None
        if (
            source.new
            and source.activity is None
            and source.consumed == 0
            and source.read_stat is not None
        ):
            native = activity.begin(source.read_stat)
        context = Context(
            source.home,
            source.path,
            source.session,
            source.parent,
            source.alias,
            self._now(),
            source.native_id,
        )
        rows: list[Row] = []
        usage: list[Usage] = []
        media: list[Media] = []
        links: list[dict[str, Any]] = []
        unknown: Counter[str] = Counter()
        for src, raw in batch:
            if native is not None and "reason" not in native:
                try:
                    activity.add(native, raw)
                except activity.ActivityRefused as error:
                    native["reason"] = str(error)
            try:
                output = source.translator.translate(raw, src, state, context)
            except Exception as error:
                raise Untranslated from error
            rows.extend(output.rows)
            usage.extend(output.usage)
            media.extend(output.media)
            links.extend(output.links)
            if output.unknown is not None:
                unknown[output.unknown] += 1
        if native is not None:
            assert source.read_stat is not None
            native.update(size=end, mtime_ns=source.read_stat.st_mtime_ns)
        encoded_activity = dumps(native) if native is not None else source.activity
        for item in media:
            self._write_media(item)
        if source.seen is not None and source.bound is not None:
            state["sidecar"] = {"mark": list(source.seen), "bound": source.bound}
        state = self.needles.redact_value(state)
        # Read before the write: the transaction holds the registry's lock.
        metadata = self._metadata_of(
            source,
            state,
            "importing" if end < size or source.metadata_version < 1 else "done",
        )
        started = time.monotonic()
        vendor = source.translator.vendor
        created = source.new
        previous = source.consumed
        with nott_database.write(self.connection):
            if source.new:
                moved = self.connection.execute(
                    "INSERT INTO journal_source (vendor, home, path, session, size, "
                    "consumed, state, metadata_version, linked, activity) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?) ON CONFLICT DO NOTHING",
                    (
                        vendor,
                        str(source.home),
                        source.rel,
                        source.session,
                        size,
                        end,
                        dumps(state),
                        end,
                        encoded_activity,
                    ),
                ).rowcount
            else:
                moved = self.connection.execute(
                    "UPDATE journal_source SET size = ?, consumed = ?, state = ?, activity = ?, "
                    "linked = CASE WHEN linked >= consumed THEN ? ELSE linked END "
                    "WHERE vendor = ? AND home = ? AND path = ? "
                    "AND consumed = ? AND status IS NULL",
                    (
                        size,
                        end,
                        dumps(state),
                        encoded_activity,
                        end,
                        vendor,
                        str(source.home),
                        source.rel,
                        source.consumed,
                    ),
                ).rowcount
            if moved != 1:
                raise Conflict
            self._session(source)
            written = 0
            if rows or usage:
                written = insert_rows(
                    self.connection,
                    source,
                    rows,
                    usage,
                    self.needles.redact_value,
                    int(self.settings.clock() * 1000),
                )
            self._links(source, links)
            self._write_metadata(source, metadata)
        self.metrics.transaction = max(
            self.metrics.transaction, time.monotonic() - started
        )
        source.consumed, source.size, source.state, source.new = (
            end,
            size,
            dumps(state),
            False,
        )
        source.activity = encoded_activity
        if created or source.linked >= previous:
            # Never past rows whose links the metadata pass has not written.
            source.linked = end
        self.written[(vendor, source.session)] = metadata
        source.mark = None
        if rows:
            self.empty.discard((vendor, source.session))
        elif created:
            self.empty.add((vendor, source.session))
        if created:
            # A child that named this session as its parent can link to it now.
            for parent in {source.native_id or source.session, source.session}:
                for key in self.unlinked.pop((vendor, parent, str(source.home)), ()):
                    if (child := self.sources.get(key)) is not None:
                        child.facts = child.mark = None
        for row in rows:
            if row.TYPE == "child":
                self._child(vendor, row)
        self.unknown.update(unknown)
        self.metrics.rows += written
        return written

    def _child(self, vendor: str, row: Row) -> None:
        """A child row is a fact of the session it names, or of its run's."""
        ident = str(getattr(row, "id", ""))
        self._touch(vendor, ident)
        if getattr(row, "kind", None) == "nspawn_run":
            for backend, thread in nott_database.query(
                self.connection,
                "SELECT backend, thread_id FROM runs WHERE run_id = ?",
                (ident,),
            ):
                if thread:
                    self._touch(backend, thread)

    def _links(self, source: Source, links: list[dict[str, Any]]) -> None:
        for link in links:
            link = self.needles.redact_value(link)
            self.connection.execute(
                "INSERT INTO journal_link (vendor, session, uuid, parent_uuid, "
                "parent_known, logical_parent_uuid, src) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (vendor, session, uuid) DO UPDATE SET "
                "parent_uuid = excluded.parent_uuid, parent_known = excluded.parent_known, "
                "logical_parent_uuid = excluded.logical_parent_uuid, src = excluded.src "
                "WHERE excluded.src >= journal_link.src",
                (
                    source.translator.vendor,
                    source.session,
                    link["uuid"],
                    link.get("parentUuid"),
                    int("parentUuid" in link),
                    link.get("logicalParentUuid"),
                    link["src"],
                ),
            )

    def _metadata_of(
        self,
        source: Source,
        state: dict[str, Any],
        import_state: str,
        facts: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """The session's list metadata from its translator state and the
        registry's recorded facts, redacted."""
        metadata = dict(state.get("metadata", {}))
        for key in ("model", "effort"):
            if state.get(key) is not None:
                metadata[key] = state[key]
        if facts is None:
            facts = self._facts(source)
        for key, value in facts.items():
            if key != "phase" and (
                value is not None or key in ("requested_effort", "sent_effort")
            ):
                metadata[key] = value
        try:
            info = source.path.stat()
            modified = info.st_mtime_ns // 1_000_000
            size = info.st_size
        except OSError:
            # A source that is gone keeps the time the journal recorded.
            modified = source.modified
            size = source.size
        metadata.update(
            vendor_id=source.native_id or source.session,
            modified=modified,
            import_state=import_state,
            size=size,
        )
        metadata["title"] = (
            metadata.get("customTitle")
            or metadata.get("summary")
            or metadata.get("aiTitle")
            or metadata.get("first_prompt")
        )
        if source.parent:
            metadata.update(
                parent=source.parent,
                parent_vendor=source.translator.vendor,
                parent_evidence="source",
            )
        parent = metadata.get("parent")
        if parent:
            parent_vendor = metadata.get("parent_vendor", source.translator.vendor)
            home = metadata.get("parent_home", str(source.home))
            linked = {
                row[0]
                for sql in PARENT_SQL
                for row in nott_database.query(
                    self.connection, sql, (parent_vendor, parent, home)
                )
            }
            if len(linked) == 1:
                metadata["parent"] = linked.pop()
            else:
                # Its session is not written yet: its first commit links it.
                self.unlinked.setdefault((parent_vendor, parent, home), set()).add(
                    (source.translator.vendor, str(source.home), source.rel)
                )
        for part in source.home.parents:
            if part.name == "native" and part.parent.parent.name == "native":
                metadata["owner_node"] = part.parent.name
                break
        if source.home.name == "native" and source.home.parent.parent.name == "native":
            metadata["owner_node"] = source.home.parent.name
        redacted: dict[str, Any] = self.needles.redact_value(metadata)
        return redacted

    def _write_metadata(self, source: Source, metadata: dict[str, Any]) -> None:
        keys = (
            "vendor_id",
            "entrypoint",
            "summary",
            "title",
            "owner_node",
            "parent_vendor",
            "parent_evidence",
            "requested_effort",
            "sent_effort",
            "import_state",
            "modified",
            "cwd",
            "model",
            "effort",
            "parent",
        )
        self.connection.execute(
            "UPDATE journal_session SET "
            + ", ".join(
                f"{key} = ?"
                if key in ("effort", "requested_effort", "sent_effort")
                and key in metadata
                else f"{key} = coalesce(?, {key})"
                for key in keys
            )
            + ", account = coalesce(?, account) WHERE vendor = ? AND id = ?",
            (
                *[metadata.get(key) for key in keys],
                metadata.get("account", source.alias),
                source.translator.vendor,
                source.session,
            ),
        )
        # The shared reader can use the same metadata before its list API expands.
        # The `session` row is the session's first row of its type: the update
        # searches no other row.
        self.connection.execute(
            "UPDATE journal_row SET row = json_set(row, '$.metadata', json(?)) "
            "WHERE vendor = ? AND session = ? AND seq = (SELECT seq FROM journal_row "
            "WHERE vendor = ? AND session = ? AND type = 'session' ORDER BY seq LIMIT 1) "
            "AND coalesce(json_extract(row, '$.metadata'), '') != ?",
            (
                dumps(metadata),
                source.translator.vendor,
                source.session,
                source.translator.vendor,
                source.session,
                dumps(metadata),
            ),
        )

    def _backfill(self, source: Source, budget: list[int]) -> None:
        """Read consumed bytes for metadata only. Keep rows and usage counts.
        A source that is gone ends the pass with the rows the journal holds."""
        saved = json.loads(source.state)
        progress = saved.get(
            "backfill",
            {
                "offset": 0,
                "state": {},
                "end": source.consumed
                if source.backfill_end is None
                else source.backfill_end,
            },
        )
        retained = json.loads(source.activity) if source.activity else None
        rebuild = (
            retained is None
            or retained["size"] != source.consumed
            or retained.get("rebuild")
        )
        if rebuild and "activity" not in progress:
            # Rebuild the prefix without discarding its retained source identity.
            progress = {"offset": 0, "state": {}, "end": source.consumed}
        offset = start = int(progress["offset"])
        native = progress.get("activity") if retained is not None else None
        # A pass saved before a rollback ends where `start` found the source:
        # the previous release appended past its old end without links.
        end = max(
            int(progress.get("end", source.consumed)),
            source.backfill_end or 0,
            source.consumed if rebuild else 0,
        )
        info: os.stat_result | None = None
        try:
            descriptor = open_owner_path(source.home, source.path)
        except ValueError:
            self._drop((self._key(source), "pass"))
            if os.path.lexists(source.path):
                self._invalidate_activity(source, "transcript unavailable")
                return
            # Gone: its links cannot be read again, and its rows stay.
            data, length, end = b"", 0, offset
            native = None
        else:
            try:
                info = os.fstat(descriptor)
                activity.check(
                    info,
                    retained,
                )
                if info.st_size < end:
                    raise activity.ActivityRefused("source shrank")
                mark = [info.st_size, info.st_mtime_ns, info.st_ctime_ns]
                if native is not None and progress.get("activity_mark") != mark:
                    native["reason"] = "transcript changed during activity rebuild"
                if rebuild and retained is not None and native is None and offset == 0:
                    native = activity.begin(info)
                    progress["activity_mark"] = mark
                data, length = self._take(source, "pass", descriptor, info, offset, end)
                path_info = source.path.lstat()
                after = os.fstat(descriptor)
                if (info.st_dev, info.st_ino) != (path_info.st_dev, path_info.st_ino):
                    raise activity.ActivityRefused("transcript replaced")
                if after.st_size < info.st_size or (
                    after.st_size == info.st_size
                    and (after.st_mtime_ns, after.st_ctime_ns)
                    != (info.st_mtime_ns, info.st_ctime_ns)
                ):
                    raise activity.ActivityRefused("transcript changed while reading")
            except activity.ActivityRefused as error:
                self._fail(source, str(error))
                return
            except OSError:
                self._drop((self._key(source), "pass"))
                self._invalidate_activity(source, "transcript unavailable")
                return
            finally:
                os.close(descriptor)
        # Records end at LF only, as in the import: a CR is a byte of its
        # record.
        records = data[: data.rfind(b"\n", 0, length) + 1].split(b"\n")[:-1]
        state = progress["state"]
        context = Context(
            source.home,
            source.path,
            source.session,
            source.parent,
            source.alias,
            self._now(),
            source.native_id,
            rows=False,
        )
        vendor = source.translator.vendor
        for first in range(0, max(len(records), 1), self.settings.batch_lines):
            if budget[0] == 0:
                break
            batch = records[first : first + self.settings.batch_lines]
            links: list[dict[str, Any]] = []
            for raw in batch:
                if native is not None and "reason" not in native:
                    try:
                        activity.add(native, raw)
                    except activity.ActivityRefused as error:
                        native["reason"] = str(error)
                output = source.translator.translate(raw, offset, state, context)
                links.extend(output.links)
                offset += len(raw) + 1
            # The pass keeps metadata only. The Workflow lifecycle comes from
            # the ordinary import, so the pass does not keep it.
            for name in WORKFLOW_KEYS:
                state.pop(name, None)
            complete = offset == end
            if native is not None:
                assert info is not None
                native.update(size=offset, mtime_ns=info.st_mtime_ns)
            encoded_activity = (
                dumps(native) if complete and native is not None else source.activity
            )
            metadata = {**state.get("metadata", {}), **saved.get("metadata", {})}
            if state.get("metadata", {}).get("first_prompt"):
                metadata["first_prompt"] = state["metadata"]["first_prompt"]
            current = {key: value for key, value in saved.items() if key != "backfill"}
            next_state = self.needles.redact_value(
                {**current, "metadata": metadata}
                if complete
                else {
                    **saved,
                    "backfill": {
                        "offset": offset,
                        "end": end,
                        "state": state,
                        "activity": native,
                        "activity_mark": progress.get("activity_mark"),
                    },
                }
            )
            links = self.needles.redact_value(links)
            try:
                whole = source.consumed == source.path.stat().st_size
            except OSError:
                whole = True
            listed = self._metadata_of(
                source,
                {**current, "metadata": metadata},
                "done" if complete and whole else "importing",
            )
            with nott_database.write(self.connection):
                moved = self.connection.execute(
                    # Complete: the pass read up to where `start` found the
                    # source, and this writer's own commits linked the rest.
                    "UPDATE journal_source SET state = ?, metadata_version = ?, activity = ?, "
                    "linked = CASE WHEN ? THEN consumed ELSE linked END "
                    "WHERE vendor = ? AND home = ? AND path = ? AND consumed = ? "
                    "AND state = ?",
                    (
                        dumps(next_state),
                        int(complete),
                        encoded_activity,
                        int(complete),
                        vendor,
                        str(source.home),
                        source.rel,
                        source.consumed,
                        source.state,
                    ),
                ).rowcount
                if moved != 1:
                    self._reload(source)
                    return
                self._links(source, links)
                for link in links:
                    native_link = {
                        key: value for key, value in link.items() if key != "src"
                    }
                    self.connection.execute(
                        "UPDATE journal_row SET row = json_patch(row, ?) WHERE vendor = ? AND session = ? AND src = ? AND type NOT IN ('session', 'status')",
                        (
                            dumps(native_link),
                            vendor,
                            source.session,
                            link["src"],
                        ),
                    )
                    if (
                        "parentUuid" in native_link
                        and native_link["parentUuid"] is None
                    ):
                        self.connection.execute(
                            "UPDATE journal_row SET row = json_set(row, '$.parentUuid', NULL) WHERE vendor = ? AND session = ? AND src = ? AND type NOT IN ('session', 'status')",
                            (vendor, source.session, link["src"]),
                        )
                self._write_metadata(source, listed)
            self.written[(vendor, source.session)] = listed
            source.mark = None
            source.state = dumps(next_state)
            source.activity = encoded_activity
            source.metadata_version = int(complete)
            if complete:
                source.linked = source.consumed
            if budget[0] > 0:
                budget[0] -= 1
        rest = data[offset - start :]
        if (
            info is not None
            and rest
            and (b"\n" in rest or start + len(data) < info.st_size)
        ):
            self._hold(source, "pass", Held(offset, _mark(info), rest))

    def _session(self, source: Source) -> None:
        self.connection.execute(
            "INSERT INTO journal_session (vendor, id, home, path, account, parent, "
            "seq, changed) VALUES (?, ?, ?, ?, ?, ?, -1, ?) ON CONFLICT DO NOTHING",
            (
                source.translator.vendor,
                source.session,
                str(source.home),
                source.rel,
                source.alias,
                source.parent,
                int(self.settings.clock() * 1000),
            ),
        )

    def _reload(self, source: Source) -> None:
        """After a conflict: the committed offset and state, or untracked."""
        found = nott_database.query(
            self.connection,
            "SELECT consumed, size, state, status, activity, metadata_version FROM journal_source "
            "WHERE vendor = ? AND home = ? AND path = ?",
            (source.translator.vendor, str(source.home), source.rel),
        )
        key = (source.translator.vendor, str(source.home), source.rel)
        if not found or found[0][3] is not None:
            self._untrack(key, failed=bool(found))
            return
        source.consumed, source.size, source.state = (
            int(found[0][0]),
            int(found[0][1]),
            found[0][2],
        )
        source.activity = found[0][4]
        source.metadata_version = int(found[0][5])
        source.new = False

    def _write_media(self, item: Media) -> None:
        """One image file, once: a temporary file in media/, then a rename."""
        final = self.media / item.file
        if final.exists():
            return
        temporary = self.media / f".{item.file}.{os.getpid()}.{threading.get_ident()}"
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(item.data)
                stream.flush()
                os.fsync(stream.fileno())
            os.rename(temporary, final)
        except BaseException:
            with contextlib.suppress(OSError):
                temporary.unlink()
            raise

    # -- status --------------------------------------------------------------------

    def _facts(self, source: Source) -> dict[str, Any]:
        """The source's recorded facts, read once and again after a change
        (`_touch`): a cold cycle over unchanged sessions runs no query."""
        if source.facts is None:
            source.facts = self._read_facts(source)
        return source.facts

    def _read_facts(self, source: Source) -> dict[str, Any]:
        """Use recorded launch facts. Do not infer a parent from a process."""
        vendor = source.translator.vendor
        found = nott_database.query(
            self.connection,
            "SELECT run_id, phase, executed_account, owner FROM runs WHERE backend = ? AND thread_id = ? ORDER BY updated_at DESC",
            (vendor, source.native_id or source.session),
        )
        facts: dict[str, Any] = {}
        for run_id, phase, account, owner in found:
            events = nott_database.query(
                self.connection,
                "SELECT payload FROM run_events WHERE run_id = ? AND kind = 'lifecycle' ORDER BY seq",
                (run_id,),
            )
            context: dict[str, Any] = {}
            for event in events:
                payload = json.loads(event[0])
                if isinstance(payload.get("journal_context"), dict):
                    context = payload["journal_context"]
            if (
                context.get("home") is not None
                and Path(context["home"]).resolve() != source.home.resolve()
            ):
                continue
            if not context and source.session != source.native_id:
                continue
            try:
                effort = effort_read(self.connection, run_id)
            except RegistryError as error:
                # One refused record leaves that effort unknown, never the writer.
                self.settings.log(f"journal: {error}; its effort stays unknown")
                facts.update(phase=phase, account=account)
            else:
                facts.update(
                    phase=phase,
                    account=account,
                    requested_effort=effort.requested,
                    sent_effort=effort.sent,
                )
            if context.get("parent_run"):
                parent = nott_database.query(
                    self.connection,
                    "SELECT backend, thread_id FROM runs WHERE run_id = ?",
                    (context["parent_run"],),
                )
                if parent and parent[0][1]:
                    facts.update(
                        parent=parent[0][1],
                        parent_vendor=parent[0][0],
                        parent_evidence="caller_run",
                    )
                    parent_home = nott_database.query(
                        self.connection,
                        "SELECT json_extract(payload, '$.journal_context.home') FROM run_events WHERE run_id = ? AND kind = 'lifecycle' AND json_type(payload, '$.journal_context.home') = 'text' ORDER BY seq LIMIT 1",
                        (context["parent_run"],),
                    )
                    if parent_home:
                        facts["parent_home"] = parent_home[0][0]
            if (
                not facts.get("parent")
                and context.get("caller_session")
                and context.get("caller_vendor")
            ):
                facts.update(
                    parent=context["caller_session"],
                    parent_vendor=context["caller_vendor"],
                    parent_evidence="caller_session",
                )
                if context.get("caller_home"):
                    facts["parent_home"] = context["caller_home"]
            if not facts.get("parent") and owner is not None:
                kind, _, parent = owner.partition(":")
                facts.update(parent=parent, parent_vendor=kind, parent_evidence="owner")
            break
        # A child row names this session (a subagent) or one of its runs.
        ids = [source.native_id or source.session, *(row[0] for row in found)]
        for row in nott_database.query(
            self.connection,
            "SELECT r.vendor, r.session, r.row, s.home FROM journal_row r "
            "JOIN journal_session s ON s.vendor = r.vendor AND s.id = r.session "
            "WHERE r.type = 'child' AND json_extract(r.row, '$.id') IN "
            f"({', '.join('?' * len(ids))}) ORDER BY r.vendor, r.session, r.seq",
            ids,
        ):
            child = json.loads(row[2])
            if child.get("kind") == "nspawn_run":
                target = nott_database.query(
                    self.connection,
                    "SELECT backend, thread_id FROM runs WHERE run_id = ?",
                    (child.get("id"),),
                )
                target_home = nott_database.query(
                    self.connection,
                    "SELECT json_extract(payload, '$.journal_context.home') FROM run_events WHERE run_id = ? AND kind = 'lifecycle' AND json_type(payload, '$.journal_context.home') = 'text' ORDER BY seq LIMIT 1",
                    (child.get("id"),),
                )
                matches = bool(
                    target
                    and target[0][0] == vendor
                    and target[0][1] == source.native_id
                    and (
                        Path(target_home[0][0]).resolve() == source.home.resolve()
                        if target_home
                        else source.session == source.native_id
                    )
                )
            else:
                matches = (
                    row[0] == vendor
                    and child.get("id") == source.native_id
                    and Path(row[3]).resolve() == source.home.resolve()
                )
            if matches:
                facts.update(
                    parent=row[1],
                    parent_vendor=row[0],
                    parent_home=row[3],
                    parent_evidence="child_row",
                )
                break
        return facts

    def _now(self) -> str:
        return datetime.fromtimestamp(self.settings.clock(), UTC).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

    def _status(self, source: Source, status: str, reason: str | None) -> None:
        """One status row, only when the status changes."""
        vendor = source.translator.vendor
        key = (vendor, source.session)
        if self.statuses.get(key) == (status, reason) or key in self.empty:
            return
        if key not in self.statuses:
            # The first status since start: read the stored one without a
            # write, so a restart over unchanged sessions takes no lock.
            found = nott_database.query(self.connection, STATUS_SQL, key * 2)
            if not found or found[0][1] < 0:
                # No row yet: its first commit drops it from `empty`.
                self.empty.add(key)
                return
            self.statuses[key] = (found[0][0], found[0][2])
            if self.statuses[key] == (status, reason):
                return
        with nott_database.write(self.connection):
            found = nott_database.query(self.connection, STATUS_SQL, key * 2)
            if not found or found[0][1] < 0:
                self.empty.add(key)
                return
            self.statuses[(vendor, source.session)] = (found[0][0], found[0][2])
            if (found[0][0], found[0][2]) == (status, reason):
                return
            insert_rows(
                self.connection,
                source,
                [StatusRow(self._now(), source.consumed, status=status, reason=reason)],
                [],
                self.needles.redact_value,
                int(self.settings.clock() * 1000),
            )
            self.connection.execute(
                "UPDATE journal_session SET status = ? WHERE vendor = ? AND id = ?",
                (status, vendor, source.session),
            )
        self.statuses[(vendor, source.session)] = (status, reason)

    def _fail(self, source: Source, reason: str) -> None:
        self._status(source, "failed", reason)
        with nott_database.write(self.connection):
            self.connection.execute(
                "INSERT INTO journal_source (vendor, home, path, session, size, "
                "consumed, state, status, reason) VALUES (?, ?, ?, ?, ?, ?, ?, "
                "'failed', ?) ON CONFLICT (vendor, home, path) DO UPDATE SET "
                "status = 'failed', reason = excluded.reason",
                (
                    source.translator.vendor,
                    str(source.home),
                    source.rel,
                    source.session,
                    source.size,
                    source.consumed,
                    source.state,
                    reason,
                ),
            )
        self._untrack(
            (source.translator.vendor, str(source.home), source.rel), failed=True
        )
        self.settings.log(f"journal: {source.path}: {reason}; no longer tracked")

    def _fence(self, source: Source) -> dict[str, Any] | None:
        """The fields `FENCE` of the source's activity, which each poll and
        each read check. The activity is decoded once for each change of
        it, as `_parse` does for the state. None for a source without
        activity."""
        if not source.activity:
            return None
        if source.fence is None or source.fence[0] is not source.activity:
            native = json.loads(source.activity)
            source.fence = (
                source.activity,
                {key: native[key] for key in FENCE if key in native},
            )
        return source.fence[1]

    def _parse(self, source: Source) -> Parsed:
        """What the status rules read from the source's state. The journal
        decodes the state once for each change of it, not once for each cycle or
        for each agent that reads its parent."""
        if source.parsed is None or source.parsed[0] is not source.state:
            state = json.loads(source.state)
            runs = state.get("workflows")
            certificate = state.get(CERTIFICATE)
            turn_at = state.get("turn_at")
            sidecar = state.get("sidecar")
            mark = sidecar.get("mark") if isinstance(sidecar, dict) else None
            bound = sidecar.get("bound") if isinstance(sidecar, dict) else None
            source.parsed = (
                source.state,
                Parsed(
                    flags=(
                        source.translator.ended(state),
                        bool(state.get("failed")),
                        bool(state.get("waiting")),
                    ),
                    workflows={
                        run: entry
                        for run, entry in runs.items()
                        if isinstance(entry, dict)
                    }
                    if isinstance(runs, dict)
                    else {},
                    pending=bool(state.get("workflow_pending")),
                    certificate=certificate if isinstance(certificate, dict) else None,
                    turn_at=turn_at if type(turn_at) is int else None,
                    witness=(
                        (
                            (mark[0], mark[1], mark[2], mark[3], mark[4], mark[5]),
                            bound,
                        )
                        if isinstance(mark, list)
                        and len(mark) == 6
                        and all(type(value) is int for value in mark)
                        and type(bound) is int
                        else None
                    ),
                ),
            )
        return source.parsed[1]

    def _flags(self, source: Source) -> tuple[bool, bool, bool]:
        """(ended, failed, waiting) of the source's state, parsed once per state."""
        return self._parse(source).flags

    def _workflow_status(self, source: Source) -> str | None:
        """`ended` or `failed` when the Workflow tool's journal records the end of
        this Workflow agent. Else `failed` when the agent holds a valid terminal
        certificate (`_reconcile`). Else None. A Workflow agent's metadata has
        no `finished` key, so this line is its independent finished signal. The
        line comes before a certificate. None means absence only: no journal
        file, no end line for this agent, and no valid certificate.

        The status reader calls `_certificate` with the journal that it read,
        before it uses the certificate and before the own end line. So a void
        certificate goes, also when the agent has its own end line. As on
        main, an end line before a later `started` line stays the status.

        Raises:
            WorkflowJournalRefused: as `_journal`, also for an agent with a
                valid certificate (spec PR 1944, revision h). It sets no status
                and keeps the certificate. The caller gives the status of the
                other rules, with the refusal as its reason.
            WorkflowListingRefused: the last listing of the run folder failed,
                for an agent with no end line and no valid certificate. As for
                a refused journal, it sets no status, and its message is the
                reason.
        """
        if source.translator.vendor != "claude" or source.parent is None:
            return None
        journal = workflow_journal(source.home, source.path)
        if journal is None:
            return None
        # A refusal gives no status, also for an agent with a certificate:
        # the refused journal can hold a later `started` line of the agent.
        # The certificate stays, and the next read decides by rule 4.
        observed = self._journal(source.home, journal)
        if observed is None:
            observed = Observed({}, {}, True)
        held = self._certificate(source, observed)
        own = observed.ends.get(source.path.stem.removeprefix("agent-"))
        if own is not None:
            return own
        if held is not None:
            return "failed"
        refused = self._listing_refusal(source)
        if refused is not None:
            raise WorkflowListingRefused(refused)
        return None

    def _journal(self, home: Path, journal: Path) -> Observed | None:
        """What the Workflow journal `journal` records for its agents
        (`workflow_events`), and whether its bytes end with a complete record.
        None when the journal does not exist. The read continues to the `fstat`
        size, and only a complete read goes into the cache.

        Raises:
            WorkflowJournalRefused: the journal is not an owner-held readable
                regular file with one link under owner directories. Or it is
                larger than `WORKFLOW_JOURNAL_MAX_BYTES`, a read fails, or it ends
                before the `fstat` size. A refusal deletes the cache entry of the
                journal.
        """
        try:
            descriptor = open_owner_path(home, journal)
        except ValueError as error:
            # The owner-path opener wraps OS errors but retains their context.
            if isinstance(error.__context__, FileNotFoundError):
                return None
            self.workflow_journals.pop(journal, None)
            raise WorkflowJournalRefused("workflow journal unavailable") from None
        try:
            info = os.fstat(descriptor)
            try:
                activity.check(info, None)
            except activity.ActivityRefused:
                raise WorkflowJournalRefused(
                    "workflow journal is not an owner regular file"
                ) from None
            if info.st_size > WORKFLOW_JOURNAL_MAX_BYTES:
                raise WorkflowJournalRefused("workflow journal exceeds byte ceiling")
            mark = (info.st_ino, info.st_size, info.st_mtime_ns)
            cached = self.workflow_journals.get(journal)
            if cached is None or cached[0] != mark:
                # At most the snapshot's size: bytes appended after it wait
                # for the next read, so the ceiling bounds every read. A call
                # can give fewer bytes than it asks for, so the read goes on
                # from the new offset. Zero bytes before the size means that
                # the file became shorter after the `fstat`.
                size = info.st_size
                data = bytearray()
                while len(data) < size:
                    chunk = os.pread(descriptor, size - len(data), len(data))
                    if not chunk:
                        raise WorkflowJournalRefused(
                            "workflow journal shorter than its snapshot"
                        )
                    data += chunk
                ends, starts = workflow_events(bytes(data))
                whole = not data or data.endswith(b"\n")
                cached = (mark, Observed(ends, starts, whole))
                self.workflow_journals[journal] = cached
        except OSError:
            self.workflow_journals.pop(journal, None)
            raise WorkflowJournalRefused("workflow journal unavailable") from None
        except WorkflowJournalRefused:
            self.workflow_journals.pop(journal, None)
            raise
        finally:
            os.close(descriptor)
        return cached[1]

    def _certificate(
        self, source: Source, observed: Observed | None
    ) -> dict[str, Any] | None:
        """Rule 4 of spec PR 1944, revision i: the one function that decides
        whether the terminal certificate of the Workflow agent `source` is
        void. Every reader calls it before it uses the certificate. `_certify`
        calls it for membership and retirement, in the reconciliation that
        `_reconcile` does before the statuses of the sweep.
        `_workflow_status` calls it for the status. Each reader gives the
        Workflow journal of the run that the sweep read as `observed`. The
        `lstat` of the cycle gives None, because the journal is not read yet.
        The cycle uses no certificate: it only deletes a certificate that the
        `lstat` voids.

        A valid certificate names this agent and run. The file has the
        device, inode and size that the certificate holds. Also, `observed`
        holds no more `started` lines of the agent than at certification. The
        Workflow call and its launch result name no agent id. So a later
        `started` line is the record of a later call that names the agent. A
        change of the modification time alone keeps the certificate.

        The producer model bounds this rule: Claude Code only appends to an
        agent file, and it gives each launch new agent ids. A rewrite in place
        with the same length, by another writer, keeps the certificate. That
        is the limit of the rule.

        Returns the certificate when it is valid. Else the function deletes it
        (`_void`) before it returns None, also while the journal tracks the
        run. So no reader acts on it. None also when the cycle could not stat
        the file: then no reader can check the certificate, and it stays."""
        if source.translator.vendor != "claude" or source.parent is None:
            return None
        held = self._parse(source).certificate
        if held is None or source.stamp is None:
            return None
        agent = source.path.stem.removeprefix("agent-")
        started = held.get("started")
        if (
            held.get("agent"),
            held.get("run"),
            held.get("device"),
            held.get("inode"),
            held.get("size"),
        ) == (agent, source.path.parent.name, *source.stamp) and (
            observed is None
            or (isinstance(started, int) and observed.starts.get(agent, 0) <= started)
        ):
            return held
        self._void(source)
        return None

    def _horizon(self, parent: Source, parsed: Parsed) -> bool:
        """Whether this cycle's read of the main session `parent` reached the end
        of a complete record. The read has nothing unread, no partial record and
        no Workflow call that waits for its result. A deferred or invalidated
        observation is not valid. A source with no stamp of this cycle is not
        valid, because it cannot show a rewrite or a replacement."""
        return not (
            parent.deferred
            or parent.metadata_version < 1
            or parent.stamp is None
            or parent.observed_size != parent.consumed
            or parent.activity is None
            or parsed.pending
        )

    def _reconcile(self, parent: Source) -> None:
        """The closing sequence of each tracked Workflow run of the main session
        `parent` whose launches all ended, and the certificates of its members.

        A run closes when its latest launch has an end and it has no other
        open launch. The sequence has three steps (`_close_run`). First, a
        parent read to the end of a complete record holds the end. Second, one
        complete listing of the run folder follows. Third, a parent read of a
        later cycle still holds the end, with no waiting Workflow call and no new
        launch. Each agent file of that listing is a member. Claude Code writes
        the Workflow call before the file of any agent of that launch. It writes
        the end notification after each agent file of the launch exists. So the
        listing holds every member, also one that no scan found yet. It holds no
        agent of a later launch.

        A member with its own end line needs nothing, and so does a refused
        source. A member with a valid certificate already has one, from this
        launch or from an earlier launch of the run. Each other member gets a
        certificate when the journal tracks its source and reads it to its end.
        The run's entry keeps all its members, and each sweep checks each
        member again (`_certify`). So a certificate that an event voids while
        the run is tracked holds the run until the member has a new valid
        certificate. The run leaves the state when no member waits. A refused
        or incomplete Workflow journal certifies no member. One transaction
        commits each certificate and the change of the parent's entry, by
        compare-and-set on the offset and state of each source.

        A failed listing keeps the run and its reason. An invalid parent read
        does not delete that reason, because the next valid read lists the
        folder again in any case."""
        key = (parent.translator.vendor, str(parent.home), parent.rel)
        parsed = self._parse(parent)
        if not parsed.workflows:
            self.closing.pop(key, None)
            return
        folder = session_folder(parent.home, parent.path)
        valid = folder is not None and self._horizon(parent, parsed)
        sequences = self.closing.setdefault(key, {})
        for run in list(sequences):
            if run not in parsed.workflows:
                del sequences[run]
        changes: dict[str, list[str] | None] = {}
        certificates: list[tuple[Source, dict[str, Any]]] = []
        for run, entry in parsed.workflows.items():
            closed = entry.get("status") is not None and not entry.get("open")
            if folder is None or not closed:
                sequences.pop(run, None)
                continue
            if not valid:
                seen = sequences.get(run)
                if seen is not None and seen.listing is not None:
                    del sequences[run]
                continue
            flow = folder / "subagents" / "workflows" / run
            fixed = entry.get("members")
            members = (
                fixed
                if isinstance(fixed, list)
                else self._close_run(sequences, run, entry, flow)
            )
            if members is None:
                continue
            if not self._certify(parent, run, entry, flow, members, certificates):
                changes[run] = None
            elif not isinstance(fixed, list):
                changes[run] = members
        if changes or certificates:
            self._settle(parent, changes, certificates)

    def _close_run(
        self,
        sequences: dict[str, Closing],
        run: str,
        entry: dict[str, Any],
        flow: Path,
    ) -> list[str] | None:
        """One step of the closing sequence of the closed `run`. The parent read
        of this cycle is valid. The members when the sequence is complete, else
        None. The first valid read with this end makes one complete listing of
        the run folder. The next valid read with the same end completes the
        sequence with that listing. `_statuses` calls `_reconcile` once, at the
        end of a cycle, so that read is the parent read of a later cycle.

        A failed listing stops the sequence and keeps its reason. The status
        rows of the run's agents show it (`_workflow_status`). The log shows it
        once for each new reason. The next valid read lists the folder again."""
        mark = (str(entry.get("call")), int(entry.get("end") or 0))
        seen = sequences.get(run)
        if seen is not None and seen.mark == mark and seen.listing is not None:
            del sequences[run]
            return list(seen.listing)
        try:
            listing = workflow_agents(flow)
        except WorkflowListingRefused as error:
            reason = str(error)
            if seen is None or seen.refused != reason:
                self.settings.log(
                    f"journal: {flow}: {reason}; the Workflow run stays open"
                )
            sequences[run] = Closing(mark, None, reason)
            return None
        sequences[run] = Closing(mark, listing)
        return None

    def _certify(
        self,
        parent: Source,
        run: str,
        entry: dict[str, Any],
        flow: Path,
        members: list[str],
        certificates: list[tuple[Source, dict[str, Any]]],
    ) -> list[str]:
        """The members of `run` that still wait for terminal evidence. Adds a
        certificate to `certificates` for each member that gets one now. The
        run retires only when no member waits, so each member holds its own
        end line, a refusal or a certificate that `_certificate` finds valid
        for this read of the journal. A void certificate goes before the
        member counts."""
        try:
            observed = self._journal(parent.home, flow / "journal.jsonl")
        except WorkflowJournalRefused:
            return members
        if observed is None:
            observed = Observed({}, {}, True)
        if not observed.whole:
            # An end line of a member can be in the record that is not
            # complete yet.
            return members
        left: list[str] = []
        for agent in members:
            path = flow / f"agent-{agent}.jsonl"
            key = (
                parent.translator.vendor,
                str(parent.home),
                str(path.relative_to(parent.home)),
            )
            child = self.sources.get(key)
            held = None if child is None else self._certificate(child, observed)
            if key in self.failed or agent in observed.ends or held is not None:
                continue
            if child is None:
                left.append(agent)
                continue
            if (
                child.new
                or child.behind
                or child.deferred
                or child.stamp is None
                or child.observed_size != child.consumed
            ):
                left.append(agent)
                continue
            device, inode, size = child.stamp
            certificates.append(
                (
                    child,
                    {
                        "agent": agent,
                        "run": run,
                        "call": entry.get("call"),
                        "device": device,
                        "inode": inode,
                        "size": size,
                        "started": observed.starts.get(agent, 0),
                    },
                )
            )
        return left

    def _settle(
        self,
        parent: Source,
        changes: dict[str, list[str] | None],
        certificates: list[tuple[Source, dict[str, Any]]],
    ) -> None:
        """Commit the certificates and the parent's run entries in one
        transaction. A run gets its members, or with None it leaves the state.
        With no change of a run, the parent's state stays, and its
        compare-and-set still guards the certificates. A conflict on any
        source rolls back all, and the next sweep tries again from the
        committed state."""
        state = json.loads(parent.state)
        runs = state.get("workflows", {})
        for run, members in changes.items():
            if members is None:
                del runs[run]
            else:
                runs[run]["members"] = members
        encoded = dumps(state) if changes else parent.state
        written: list[tuple[Source, str]] = []
        try:
            with nott_database.write(self.connection):
                for child, certificate in certificates:
                    held = json.loads(child.state)
                    held[CERTIFICATE] = certificate
                    text = dumps(held)
                    if self._swap_state(child, text) != 1:
                        raise Conflict
                    written.append((child, text))
                if self._swap_state(parent, encoded) != 1:
                    raise Conflict
        except Conflict:
            for source in (parent, *(child for child, _ in certificates)):
                self._reload(source)
            return
        for child, text in written:
            child.state = text
        parent.state = encoded

    def _swap_state(self, source: Source, state: str) -> int:
        """Compare-and-set the state of a tracked source on its committed
        offset and state: the number of rows it moved."""
        moved: int = self.connection.execute(
            "UPDATE journal_source SET state = ? WHERE vendor = ? AND home = ? "
            "AND path = ? AND consumed = ? AND state = ? AND status IS NULL",
            (
                state,
                source.translator.vendor,
                str(source.home),
                source.rel,
                source.consumed,
                source.state,
            ),
        ).rowcount
        return moved

    def _void(self, source: Source) -> None:
        """Delete the agent's terminal certificate, by compare-and-set on the
        source's offset and state. After a conflict the committed state
        applies at a later cycle, and this cycle changes no status of the
        source."""
        state = json.loads(source.state)
        state.pop(CERTIFICATE, None)
        text = dumps(state)
        with nott_database.write(self.connection):
            moved = self._swap_state(source, text)
        if moved != 1:
            self._reload(source)
            source.stamp = None
            return
        source.state = text

    def _listing_refusal(self, source: Source) -> str | None:
        """The reason of the failed last listing of the run folder of the
        Workflow agent `source` (`_close_run`). None when no listing of it
        failed."""
        folder = session_folder(source.home, source.path)
        if folder is None:
            return None
        main = folder.parent / f"{folder.name}.jsonl"
        key = (
            source.translator.vendor,
            str(source.home),
            str(main.relative_to(source.home)),
        )
        seen = self.closing.get(key, {}).get(source.path.parent.name)
        return seen.refused if seen is not None else None

    def _finished_subagent(self, source: Source) -> bool:
        """Read Claude subagent completion without changing the transcript state.
        An Agent-tool subagent whose sidecar the daemon proves old
        (`_resumed`) is not finished."""
        if source.translator.vendor != "claude" or source.parent is None:
            return False
        try:
            descriptor = open_owner_path(
                source.home, source.path.with_suffix(".meta.json")
            )
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode) or info.st_size > LINE_MAX_BYTES:
                    return False
                data = os.pread(descriptor, LINE_MAX_BYTES + 1, 0)
                metadata = json.loads(data)
            finally:
                os.close(descriptor)
        except OSError, ValueError, RecursionError:
            return False
        if not isinstance(metadata, dict):
            return False
        if metadata.get("finished") is True:
            return not self._resumed(source, info, data, metadata)
        if metadata.get("taskKind") != "in_process_teammate":
            return False
        team, name = metadata.get("teamName"), metadata.get("name")
        if not isinstance(team, str) or not isinstance(name, str):
            return False
        if not team or team == "." or "/" in team or ".." in team:
            return False
        return self._absent_teammate(source, team, name)

    def _resumed(
        self,
        source: Source,
        info: os.stat_result,
        data: bytes,
        metadata: dict[str, Any],
    ) -> bool:
        """Whether the daemon proves that a turn line came after the write of
        the `finished: true` sidecar of an Agent-tool subagent. The proof uses
        the read order of the daemon, never a clock. The daemon read the mark,
        the last turn line starts at or after the size of the next `lstat`,
        and the sidecar has that mark now. A new mark is a new completion
        marker. Teammates and Workflow agents keep the `finished` rule."""
        if (
            "taskKind" in metadata
            or workflow_journal(source.home, source.path) is not None
        ):
            return False
        mark = (
            info.st_dev,
            info.st_ino,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
            int.from_bytes(hashlib.sha256(data).digest()[:8], "big"),
        )
        parsed = self._parse(source)
        if source.seen is None and parsed.witness is not None:
            source.seen, source.bound = parsed.witness
        if source.seen != mark:
            source.seen, source.bound = mark, None
            return False
        return (
            source.bound is not None
            and parsed.turn_at is not None
            and parsed.turn_at >= source.bound
        )

    def _absent_teammate(self, source: Source, team: str, name: str) -> bool:
        """Only a missing team record or a valid roster can prove absence."""
        try:
            descriptor = open_owner_path(
                source.home, source.home / "teams" / team / "config.json"
            )
        except ValueError as error:
            # The owner-path opener wraps OS errors but retains their context.
            return isinstance(error.__context__, FileNotFoundError)
        try:
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode) or info.st_size > LINE_MAX_BYTES:
                    return False
                record = json.loads(os.pread(descriptor, LINE_MAX_BYTES + 1, 0))
            finally:
                os.close(descriptor)
        except OSError, ValueError, RecursionError:
            return False
        members = record.get("members") if isinstance(record, dict) else None
        if not isinstance(members, list) or any(
            not isinstance(member, dict) or not isinstance(member.get("name"), str)
            for member in members
        ):
            return False
        return not any(member["name"] == name for member in members)

    def _statuses(self, sources: Any) -> None:
        """Each source's status, and its metadata when the source, its facts or
        its metadata pass moved. An unchanged source costs no query."""
        if self.halted is not None or self.paused is not None:
            return
        live: dict[Path, dict[str, bool] | None] = {}
        sources = list(sources)
        for source in sources:
            # The certificates of a closed Workflow run come before the
            # statuses of its agents in the same sweep.
            if source.translator.vendor == "claude" and source.parent is None:
                self._reconcile(source)
        for source in sources:
            if (
                source.deferred
                and not source.new
                and (source.seen is not None or self._parse(source).witness is not None)
            ):
                # This cycle could not read the transcript. An old sidecar
                # (`_resumed`) is then not proven, and its `finished: true`
                # is the only evidence: the subagent is `ended`.
                self._status(source, "ended", None)
                continue
            if source.new or source.behind:
                continue
            if source.stamp is None:
                # This cycle could not stat the source, or the void of its
                # certificate met a conflict. It cannot check a terminal
                # certificate, so the source keeps its status.
                continue
            if (source.translator.vendor, source.session) in self.empty:
                # No row: no reader lists it, a status row needs a row, and the
                # batch that writes its first row writes its metadata.
                continue
            facts = self._facts(source)
            phase = facts.get("phase")
            if source.home not in live:
                live[source.home] = threads.claude_live(source.home)
            held = live[source.home]
            held_now = (
                None
                if held is None
                else (source.parent or source.native_id or source.session) in held
            )
            ended, failed, waiting = self._flags(source)
            key = (source.translator.vendor, source.session)
            reason: str | None = None
            try:
                workflow = self._workflow_status(source)
            except (WorkflowJournalRefused, WorkflowListingRefused) as error:
                # A refusal is not terminal evidence, so it sets no status:
                # the other rules give it, as for an absent journal, and the
                # refusal is the reason of that status row.
                workflow, reason = None, str(error)
            if workflow is not None:
                status = workflow
            elif self._finished_subagent(source):
                status = "ended"
            elif phase in ("failed", "interrupted", "orphaned") or failed:
                status = "failed"
            elif phase in ("completed", "closed"):
                status = "ended"
            elif phase in ("waiting_approval", "idle") or waiting:
                status = "waiting"
            elif source.parent is not None and phase not in ACTIVE_PHASES:
                # A subagent is live while its parent's process holds it and
                # its turn is open; one whose turn ended is ended.
                if held_now is None:
                    continue
                status = "live" if held_now and not ended else "ended"
            elif held_now or phase in ACTIVE_PHASES:
                status = "waiting" if ended else "live"
            elif held_now is None:
                continue
            else:
                known = self.statuses.get(key)
                previous = (
                    known[0]
                    if known is not None
                    else next(
                        iter(
                            nott_database.query(
                                self.connection,
                                "SELECT status FROM journal_session "
                                "WHERE vendor = ? AND id = ?",
                                key,
                            )
                        ),
                        [None],
                    )[0]
                )
                status = (
                    "failed"
                    if not ended and previous in ("live", "failed")
                    else "ended"
                )
            mark = (source.observed_size, source.modified, source.metadata_version)
            if source.mark != mark:
                metadata = self._metadata_of(
                    source,
                    json.loads(source.state),
                    "done" if source.metadata_version >= 1 else "importing",
                    facts,
                )
                if key not in self.written:
                    # The first pass since start compares the stored metadata.
                    stored = nott_database.query(self.connection, METADATA_SQL, key)
                    if stored and stored[0][0] is not None:
                        self.written[key] = json.loads(stored[0][0])
                if self.written.get(key) != metadata:
                    with nott_database.write(self.connection):
                        self._write_metadata(source, metadata)
                    self.written[key] = metadata
                source.mark = mark
            self._status(source, status, reason)

    # -- limits --------------------------------------------------------------------

    def _room(self, source: Source) -> bool:
        """Whether `~/.nott` keeps `disk_floor` free bytes; below it, a failed
        status row on a known session and no write."""
        try:
            info = os.statvfs(self.settings.nott_root)
        except OSError:
            return True
        if info.f_bavail * info.f_frsize >= self.settings.disk_floor:
            return True
        if not source.new:
            self._status(source, "failed", "disk")
        return False

    def _needles(self) -> None:
        """Read the roster if it is not read yet, and add the secrets of each
        token file that changed; keep the old. When the roster or a declared
        token file cannot be read, writing pauses until both are read."""
        if self.halted is not None:
            return
        reason = self._roster() or self._tokens()
        if reason != self.paused:
            self.settings.log(
                f"journal: writing paused: secrets ({reason})"
                if reason is not None
                else "journal: writing resumed: the secrets are read"
            )
        self.paused = reason

    def _roster(self) -> str | None:
        """Why the roster is not read, or None."""
        if self.read or self.settings.read_roster is None:
            return None
        try:
            self.roster = self.settings.read_roster()
        except Exception as error:  # noqa: BLE001 - any refusal pauses the writer
            return f"the roster is not read: {type(error).__name__}"
        self.read = True
        self._homes()
        return None

    def _tokens(self) -> str | None:
        """Add the secrets of each token file that changed; why one declared
        token file is not read, or None. A ceiling halts the writer."""
        roster = self.roster
        if roster is None:
            return None
        reason: str | None = None
        for provider in roster.providers:
            if provider.kind != "claude" or (
                provider.secrets_dir() is None
                and provider.oauth_token_file is None
                and provider.token_file is None
            ):
                # No credential source (a `home` or a `base_url` account): no
                # token file, so no needle.
                continue
            try:
                aliases = _token_aliases(provider)
            except OSError as error:
                reason = (
                    f"the secrets of {provider.name} are not listed: {error.strerror}"
                )
                continue
            for alias in aliases:
                try:
                    path = provider.resolve_token_file(alias)
                    info = path.stat()
                except FileNotFoundError as error:
                    if not os.path.lexists(error.filename):
                        continue
                    reason = f"the token of {alias} is not read: {type(error).__name__}"
                    continue
                except (OSError, ValueError) as error:
                    reason = f"the token of {alias} is not read: {type(error).__name__}"
                    continue
                mark = (info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                if self.tokens.get(path) == mark:
                    continue
                try:
                    data = read_token_file(path)
                except TokenFileRefused as error:
                    reason = f"the token is refused: {error.message}"
                    continue
                try:
                    self.needles = self.needles.merged(
                        SecretNeedles.from_credential(data),
                        maximum_bytes=NEEDLE_MAX_BYTES,
                    )
                except ValueError:
                    self._halt("secrets")
                    return None
                # Read only once merged: a failed merge reads the file again.
                self.tokens[path] = mark
        return reason

    def _halt(self, reason: str) -> None:
        """Stop the writer before any status write: a failed write leaves it
        stopped, and each next cycle writes the status again."""
        self.halted = reason
        self.settings.log(f"journal: writing stopped: {reason}")
        self._stop(reason)

    def _stop(self, reason: str) -> None:
        """A failed status, `reason`, on each known session; a status already
        written opens no write."""
        for source in list(self.sources.values()):
            if not source.new:
                self._status(source, "failed", reason)

    # -- the minute line -------------------------------------------------------------

    def report(self) -> str:
        cycles = sorted(self.metrics.cycles)
        p95 = cycles[max(0, int(len(cycles) * 0.95) - 1)] if cycles else 0.0
        line = (
            f"journal: files statted {self.metrics.statted}, bytes read "
            f"{self.metrics.read}, rows written {self.metrics.rows}, unknown "
            f"{json.dumps(dict(sorted(self.unknown.items())))}, cycle p95 "
            f"{p95 * 1000:.1f} ms max {(cycles[-1] if cycles else 0.0) * 1000:.1f} ms, "
            f"longest transaction {self.metrics.transaction * 1000:.1f} ms, "
            f"bytes deleted by the bound {self.metrics.deleted}"
        )
        if self.paused is not None:
            line += f", writing paused: secrets ({self.paused})"
        if self.stalled is not None:
            line += f", the last cycle stopped: {self.stalled}"
        self.metrics = Metrics()
        self.unknown.clear()
        return line


def _mark(info: os.stat_result) -> tuple[int, int, int, int, int]:
    """The device, inode, size, modification and change times in
    nanoseconds of one `fstat`: the mark of a held suffix (`Held`)."""
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _shared(error: Exception) -> bool:
    """Whether `error` is the store's or the disk's, not one source's: it
    stops the cycle instead of failing the source. `nott_database` names the
    store's errors (busy, refused, damaged); an error of one row, such as a
    constraint, is the source's."""
    return isinstance(
        error,
        (
            OSError,
            nott_database.DatabaseError,
            nott_database.OperationalError,
            nott_database.InternalError,
        ),
    )


def _token_aliases(provider: Provider) -> list[str]:
    """Each account whose token file the needles need: the declared accounts,
    or, with no accounts table, each `.token` file in the secrets directory,
    also one that `discovered_accounts` leaves out because of its mode. A
    secrets directory that is not there holds no token file."""
    aliases = set(provider.accounts) | set(provider.discovered_accounts())
    directory = provider.secrets_dir()
    if not provider.accounts and not provider.declared and directory is not None:
        try:
            with os.scandir(directory) as entries:
                aliases.update(
                    Path(entry.name).stem
                    for entry in entries
                    if Path(entry.name).suffix == ".token"
                )
        except FileNotFoundError:
            pass
    return sorted(aliases)


def run(settings: Settings, stop: threading.Event) -> None:
    """The loop, until `stop` is set. One daemon per `~/.nott` (a lock file)."""
    daemon = Daemon(settings)
    lock = settings.nott_root / "sessions" / "serve.lock"
    descriptor = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise Refused(f"another session serve holds {lock}") from None
        daemon.start()
        logged = time.monotonic()
        while not stop.is_set():
            started = time.monotonic()
            daemon.cycle()
            spent = time.monotonic() - started
            daemon.metrics.cycles.append(spent)
            if started - logged >= settings.log_s:
                settings.log(daemon.report())
                logged = started
            stop.wait(max(0.0, settings.hot_s - spent))
    finally:
        os.close(descriptor)
        daemon.close()


def main() -> int:
    """`nspawn session serve`: default, roster, cage and node homes, `~/.nott` and this account's registry, until SIGTERM. A roster that
    cannot be read pauses the writer until it can."""

    def read_roster() -> Roster | None:
        # The one reader. Without a roster the writer keeps the discovery of
        # main (`Daemon._homes`): the inherited Claude home, the homes of the
        # recorded lifecycle events, and the node and cage homes. Each other
        # refusal pauses the writer (`Daemon._roster`).
        try:
            return owner_read_roster()
        except RosterAbsent:
            return None

    settings = Settings(
        homes=None,
        nott_root=Path.home() / ".nott",
        store=store_path(),
        read_roster=read_roster,
    )
    stop = threading.Event()

    def ended(signum: int, frame: FrameType | None) -> None:
        stop.set()

    previous = {
        number: signal.signal(number, ended)
        for number in (signal.SIGTERM, signal.SIGINT)
    }
    try:
        run(settings, stop)
    except (Refused, RegistryError) as error:
        _log(f"nspawn session serve refused: {error}")
        return 2
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)
    return 0
