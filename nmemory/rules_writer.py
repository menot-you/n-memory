"""The reactive rule writer of the HTTP daemon.

The long-lived `nmemory mcp serve --http` process is the one writer of the rule
files. A daemon thread reads the live rule set every `poll` seconds on its own
connection, so it sees only committed state. Any change of the set (a pin or unpin,
a supersede, a falsify, a quarantine, a tainted revision, from a tool call, the
curator or another process), a change of the sector manifest, and each valid_to
expiry change what it reads. When the set has been stable for `debounce` seconds and
differs from the last one written, the thread runs the generator. It wakes at the
next valid_to.

System1 is asked at most once per `retry` seconds, also after a failed write. After
each check the thread writes `~/.nott/rules.state` (last_check, last_write, errors),
which the SessionStart hook reads to tell that the writer is alive. A failure is
logged and backs the thread off (2 s, doubling to 60 s); it never reaches a tool call.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .capsule import format_rfc3339
from .rules import live_pins, next_expiry, rule_set, write_rules
from .store import Store

logger = logging.getLogger(__name__)

STATE_FILE = Path(".nott") / "rules.state"
BACKOFF_FIRST = 2.0
BACKOFF_LAST = 60.0


class RulesWriter:
    def __init__(
        self,
        store: Callable[[], Store],
        home: Path,
        *,
        poll: float = 2.0,
        debounce: float = 3.0,
        retry: float = 300.0,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.store = store
        self.home = home
        self.poll = poll
        self.debounce = debounce
        self.retry = retry
        self.clock = clock
        self.stop = threading.Event()
        self.writes = 0
        self._written: object = None
        self._pending: object = None
        self._pending_since: datetime | None = None
        self._next_ask: datetime | None = None
        self._retry_at: datetime | None = None
        self._last_write: datetime | None = None
        self._errors: list[str] = []
        self._logged: list[str] = []
        self._fail_wait = BACKOFF_FIRST
        self._blocked_until: datetime | None = None

    def step(self) -> float:
        """One look at the live set; returns the seconds until the next look.

        It raises on a failure that it cannot report in the files: `run` logs it and
        backs off.
        """
        now = self.clock()
        store = self.store()
        rows, unreadable = live_pins(store)
        current = rule_set(rows)
        if current != self._pending:
            self._pending, self._pending_since = current, now
        settled = (
            self._pending_since is not None
            and (now - self._pending_since).total_seconds() >= self.debounce
        )
        due_retry = self._retry_at is not None and now >= self._retry_at
        blocked = self._blocked_until is not None and now < self._blocked_until
        if not blocked and ((current != self._written and settled) or due_retry):
            ask = self._next_ask is None or now >= self._next_ask
            report = write_rules(
                store,
                self.home,
                rows=rows,
                unreadable=unreadable,
                ask_system1=ask,
                now=now,
            )
            if report.asked:
                self._next_ask = now + timedelta(seconds=self.retry)
            self.writes += 1
            self._errors = [
                f"{path} not written: {cause}" for path, cause in report.errors.items()
            ]
            # A lost System1 answer is only asked again: logged, never a state error.
            notes = list(self._errors)
            if report.record_failure is not None:
                notes.append(f"System1 answers not stored: {report.record_failure}")
            if notes != self._logged:
                # Logged once per change, not at each retry.
                for line in notes:
                    logger.error("nmemory: rule files: %s", line)
                self._logged = notes
            if report.errors:
                # Not marked as written: retried after a backoff, 2 s doubling to 60 s.
                self._retry_at = None
                self._blocked_until = now + timedelta(seconds=self._fail_wait)
                self._fail_wait = min(self._fail_wait * 2, BACKOFF_LAST)
            else:
                self._blocked_until = None
                self._fail_wait = BACKOFF_FIRST
                self._written = current
                self._last_write = now
                self._retry_at = (
                    now + timedelta(seconds=self.retry) if report.fallback else None
                )
        self._touch(now)
        wait = self.poll
        expiry = next_expiry(rows, now)
        if expiry is not None:
            # Wake at the next valid_to, so an expired rule leaves the files on time.
            wait = min(wait, max(0.0, (expiry - now).total_seconds()))
        return wait

    def _touch(self, now: datetime) -> None:
        """Write the liveness state file atomically."""
        path = self.home / STATE_FILE
        body = json.dumps(
            {
                "last_check": format_rfc3339(now),
                "last_write": None
                if self._last_write is None
                else format_rfc3339(self._last_write),
                "errors": self._errors,
            }
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".rules.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(body + "\n")
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def run(self) -> None:
        # The first look waits one poll: the store opens lazily, never at daemon start.
        wait = self.poll
        backoff = BACKOFF_FIRST
        while not self.stop.wait(wait):
            try:
                wait = self.step()
                backoff = BACKOFF_FIRST
            except Exception:
                # Logged with its traceback; the next look comes after the backoff.
                logger.exception(
                    "nmemory: rule writer failed; retry in %.0f s", backoff
                )
                wait, backoff = backoff, min(backoff * 2, BACKOFF_LAST)


def spawn_rules_writer(
    store: Callable[[], Store], home: Path
) -> tuple[RulesWriter, threading.Thread]:
    writer = RulesWriter(store, home)
    thread = threading.Thread(target=writer.run, name="nmemory-rules", daemon=True)
    thread.start()
    return writer, thread
