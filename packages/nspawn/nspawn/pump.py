"""L4 bounded redacted output-pump.

Each pump owns one child stream. It reads bounded lines, redacts every line
through ``SecretNeedles`` BEFORE reserving space in the shared run budget, and
only then appends to a private ``0600`` file — so a line that is too long, or
that would blow the shared budget, is refused before any byte of it, redacted
or not, ever reaches disk.

The worker runs on its own thread: this is an I/O boundary, the one place
this package admits a thread instead of `asyncio`. `join()` re-raises
whatever typed failure the thread observed, so a background I/O fault is
never silently dropped.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable
from enum import Enum
from pathlib import Path
from typing import BinaryIO, Final

from .errors import NSpawnError
from .redact import SecretNeedles

#: Codex retains at most 1 MiB of a command's output or an MCP result
#: (`DEFAULT_OUTPUT_BYTES_CAP`, codex 0.160.1) and sends it whole in one
#: `item/completed` line, where JSON writes a control byte as six. A 1 MiB
#: ceiling killed every run whose command reached that cap (2026-10-07).
MAX_OUTPUT_LINE_BYTES: Final = 8 * 1024 * 1024
MAX_RUN_OUTPUT_BYTES: Final = 64 * 1024 * 1024

_NOFOLLOW: Final = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC: Final = getattr(os, "O_CLOEXEC", 0)


class OutputStream(Enum):
    """The two child streams, persisted separately under one run directory."""

    STDOUT = "stdout"
    STDERR = "stderr"


class PumpLimitExceeded(NSpawnError):
    """A line or the shared run budget exceeded its declared byte ceiling."""

    def __init__(self, message: str) -> None:
        self.message = message
        Exception.__init__(self, message)


class RunOutputBudget:
    """One shared byte budget consumed jointly by stdout and stderr."""

    def __init__(self, maximum_bytes: int = MAX_RUN_OUTPUT_BYTES) -> None:
        if maximum_bytes <= 0:
            raise ValueError("maximum_bytes must be positive")
        self._maximum_bytes = maximum_bytes
        self._consumed = 0
        self._lock = threading.Lock()

    def consume(self, count: int) -> None:
        """Reserve persisted bytes or refuse before append."""
        if count < 0:
            raise ValueError("count must be non-negative")
        with self._lock:
            if self._consumed + count > self._maximum_bytes:
                raise PumpLimitExceeded(
                    "run output budget exceeded: "
                    f"{self._consumed + count} > {self._maximum_bytes} bytes"
                )
            self._consumed += count


class _BoundedLineReader:
    """Split a binary stream into newline-terminated lines, bounded per line.

    Built on ``readline(limit)``, never a bounded ``read(limit)``: a live pipe
    can sit well under ``limit`` bytes for an arbitrary stretch of wall time,
    and a buffered ``read(limit)`` blocks until ``limit`` bytes actually
    arrive or EOF, which would hold every line hostage behind however much of
    the cap the child has not yet produced. ``readline`` returns the moment
    its own underlying reads observe a terminator.

    A line — including its terminator — that cannot be assembled within
    ``max_line_bytes`` is refused before it is ever returned. Capping the
    read at ``max_line_bytes + 1`` makes this a single length check: the
    capped read can only come back longer than ``max_line_bytes`` by hitting
    that cap, whether it stopped there because the terminator was one byte
    too far out or because none had appeared yet — either way, the line
    cannot fit.
    """

    def __init__(self, reader: BinaryIO, max_line_bytes: int, label: str) -> None:
        self._reader = reader
        self._max_line_bytes = max_line_bytes
        self._label = label
        self._lines_read = 0

    def read_line(self) -> bytes | None:
        """Return the next line, the trailing unterminated remainder, or ``None`` at EOF."""
        # +1 lets a legitimate line of exactly max_line_bytes (terminator
        # included) be told apart from one that keeps growing past it.
        chunk = self._reader.readline(self._max_line_bytes + 1)
        if not chunk:
            return None
        if len(chunk) > self._max_line_bytes:
            # WHICH stream, WHICH line, HOW MANY bytes, AGAINST WHAT ceiling.
            # A refusal that named only the ceiling reached the operator as the
            # bare class name `PumpLimitExceeded` and left every one of this
            # package's five distinct ceilings indistinguishable. The size is a
            # FLOOR, not the line's length: the reader stops at the ceiling by
            # construction — measuring the real length would mean consuming the
            # dump this refusal exists to refuse.
            raise PumpLimitExceeded(
                f"{self._label} line {self._lines_read + 1} exceeded the "
                f"{self._max_line_bytes} byte line ceiling: "
                f"{len(chunk)} bytes read with no terminator"
            )
        self._lines_read += 1
        return chunk


def _open_private(path: Path) -> BinaryIO:
    """Create ``path`` fresh, mode ``0600``, refusing a pre-existing target."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC
    fd = os.open(path, flags, 0o600)
    os.fchmod(fd, 0o600)  # exact mode regardless of the ambient umask
    return os.fdopen(fd, "wb")


class OutputPump:
    """Append one stream through ``SecretNeedles`` into a private run file."""

    def __init__(
        self,
        reader: BinaryIO,
        run_dir: Path,
        stream: OutputStream,
        needles: SecretNeedles,
        budget: RunOutputBudget,
        *,
        max_line_bytes: int = MAX_OUTPUT_LINE_BYTES,
        transform_line: Callable[[bytes], bytes] | None = None,
    ) -> None:
        if max_line_bytes <= 0:
            raise ValueError("max_line_bytes must be positive")
        self._reader = reader
        self._path = run_dir / stream.value
        self._needles = needles
        self._budget = budget
        self._max_line_bytes = max_line_bytes
        self._transform_line = transform_line
        self._bytes_written = 0
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._error: NSpawnError | OSError | None = None

    @property
    def finished(self) -> bool:
        """Whether EOF or a typed read failure has finished the worker."""
        return self._thread is not None and not self._thread.is_alive()

    @property
    def path(self) -> Path:
        """The stream file beneath the run directory."""
        return self._path

    @property
    def bytes_written(self) -> int:
        """Redacted bytes appended by this pump."""
        with self._lock:
            return self._bytes_written

    def start(self) -> OutputPump:
        """Start the bounded synchronous-I/O worker."""
        thread = threading.Thread(target=self._run, daemon=True)
        self._thread = thread
        thread.start()
        return self

    def join(self, timeout: float | None = None) -> None:
        """Join and re-raise any typed pump refusal."""
        if self._thread is not None:
            self._thread.join(timeout)
        if self._error is not None:
            raise self._error

    def _run(self) -> None:
        try:
            with _open_private(self._path) as handle:
                self._pump_lines(handle)
        except (NSpawnError, OSError) as error:
            self._error = error
        finally:
            self._reader.close()

    def _pump_lines(self, handle: BinaryIO) -> None:
        lines = _BoundedLineReader(self._reader, self._max_line_bytes, self._path.name)
        while True:
            line = lines.read_line()
            if line is None:
                return
            if self._transform_line is not None:
                line = self._transform_line(line)
                if len(line) > self._max_line_bytes:
                    raise PumpLimitExceeded(
                        f"transformed {self._path.name} line measured {len(line)} "
                        f"bytes against its {self._max_line_bytes} byte line ceiling"
                    )
            text = self._needles.redact(line.decode("utf-8", errors="replace"))
            try:
                decoded = json.loads(text)
            except json.JSONDecodeError:
                pass
            except RecursionError as error:
                raise PumpLimitExceeded(
                    "output JSON nesting exceeds redaction limits"
                ) from error
            else:
                try:
                    safe = self._needles.redact_value(decoded)
                    if safe != decoded:
                        newline = "\n" if text.endswith("\n") else ""
                        text = json.dumps(safe, ensure_ascii=True) + newline
                except RecursionError as error:
                    raise PumpLimitExceeded(
                        "output JSON nesting exceeds redaction limits"
                    ) from error
            redacted = text.encode("utf-8")
            self._budget.consume(len(redacted))
            handle.write(redacted)
            handle.flush()
            with self._lock:
                self._bytes_written += len(redacted)
