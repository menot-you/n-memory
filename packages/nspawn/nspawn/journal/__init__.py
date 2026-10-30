"""The session journal (layer LJ in RULEBOOK.md): each live vendor session on
this host, translated into NOTT rows of format version 1 in nSPAWN's own store.

This is the only parse layer for vendor session records. `serve` is the only
writer; `show` is the only reader, behind `nspawn session list|show` and the
node host. The row
shape is `nspawn/schema/session-journal.v1.schema.json`.

A vendor is one translator module and one entry in `TRANSLATORS`.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, ClassVar, Final, Protocol

#: The format version of every row and of the `session` row's `format` field.
FORMAT: Final = 1
#: The longest source line that the writer or identity reader accepts.
LINE_MAX_BYTES: Final = 1 << 30


@dataclass(frozen=True, slots=True)
class Row:
    """One journal row. `seq` is given when the writer stores it."""

    TYPE: ClassVar[str] = ""
    #: The fields left out of the JSON when they are None.
    OPTIONAL: ClassVar[frozenset[str]] = frozenset()

    time: str
    src: int
    native: dict[str, Any] = field(default_factory=dict, kw_only=True)

    def key(self) -> str:
        """The row's own id, else its model call id, else empty: with the
        source line and the type, this makes the row unique in its session."""
        for name in ("id", "mid"):
            value = getattr(self, name, None)
            if isinstance(value, str):
                return value
        return ""

    def body(self, seq: int) -> dict[str, Any]:
        """The v1 JSON object: `seq`, `time`, `src`, `type`, then the fields."""
        shown: dict[str, Any] = {
            "seq": seq,
            "time": self.time,
            "src": self.src,
            "type": self.TYPE,
        }
        for item in fields(self):
            if item.name in ("time", "src", "native"):
                continue
            value = getattr(self, item.name)
            if value is None and item.name in self.OPTIONAL:
                continue
            shown[item.name] = value
        shown.update(self.native)
        return shown


@dataclass(frozen=True, slots=True)
class SessionRow(Row):
    TYPE: ClassVar[str] = "session"
    OPTIONAL: ClassVar[frozenset[str]] = frozenset({"metadata"})
    vendor: str = ""
    id: str = ""
    source: dict[str, str] = field(default_factory=dict)
    account: str | None = None
    cwd: str | None = None
    model: str | None = None
    effort: str | None = None
    parent: str | None = None
    format: int = FORMAT
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class MessageRow(Row):
    TYPE: ClassVar[str] = "message"
    OPTIONAL: ClassVar[frozenset[str]] = frozenset({"text", "mid", "injected", "media"})
    role: str = "owner"
    mid: str | None = None
    text: str | None = None
    injected: dict[str, Any] | None = None
    media: list[dict[str, Any]] | None = None


@dataclass(frozen=True, slots=True)
class ReasoningRow(Row):
    TYPE: ClassVar[str] = "reasoning"
    OPTIONAL: ClassVar[frozenset[str]] = frozenset({"text", "redacted"})
    mid: str = ""
    text: str | None = None
    redacted: int | None = None


@dataclass(frozen=True, slots=True)
class ToolCallRow(Row):
    TYPE: ClassVar[str] = "tool_call"
    mid: str = ""
    id: str = ""
    name: str = ""
    input: Any = None


@dataclass(frozen=True, slots=True)
class ToolResultRow(Row):
    TYPE: ClassVar[str] = "tool_result"
    OPTIONAL: ClassVar[frozenset[str]] = frozenset(
        {"media", "injected", "spill_missing"}
    )
    id: str = ""
    ok: bool = True
    output: str = ""
    media: list[dict[str, Any]] | None = None
    injected: dict[str, Any] | None = None
    spill_missing: bool | None = None


@dataclass(frozen=True, slots=True)
class ChildRow(Row):
    TYPE: ClassVar[str] = "child"
    id: str = ""
    kind: str = "subagent"


@dataclass(frozen=True, slots=True)
class HookRow(Row):
    TYPE: ClassVar[str] = "hook"
    OPTIONAL: ClassVar[frozenset[str]] = frozenset({"exit_code", "duration_ms", "text"})
    event: str | None = None
    name: str | None = None
    tool: str | None = None
    outcome: str = "ok"
    exit_code: int | None = None
    duration_ms: int | None = None
    text: str | None = None


@dataclass(frozen=True, slots=True)
class ConfigRow(Row):
    TYPE: ClassVar[str] = "config"
    model: str | None = None
    effort: str | None = None


@dataclass(frozen=True, slots=True)
class CompactionRow(Row):
    TYPE: ClassVar[str] = "compaction"
    trigger: str | None = None
    tokens_before: int | None = None


@dataclass(frozen=True, slots=True)
class StatusRow(Row):
    TYPE: ClassVar[str] = "status"
    status: str = "live"
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class Usage:
    """The usage of one model call (`mid`) as one source line states it."""

    mid: str
    model: str | None
    input: int
    cache_read: int
    cache_write: int
    output: int
    reasoning: int | None
    src: int


@dataclass(frozen=True, slots=True)
class Media:
    """The decoded bytes of one image, kept as `<sha256>.<ext>` under media/."""

    sha256: str
    ext: str
    data: bytes

    @property
    def file(self) -> str:
        return f"{self.sha256}.{self.ext}"


@dataclass(slots=True)
class Output:
    """What one source line gives: rows, usage, images, and the name of a line
    type outside the table (counted, never a row)."""

    rows: list[Row] = field(default_factory=list)
    usage: list[Usage] = field(default_factory=list)
    media: list[Media] = field(default_factory=list)
    unknown: str | None = None
    links: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class Identity:
    """The session a source file feeds, read from its path."""

    session: str
    parent: str | None


@dataclass(frozen=True, slots=True)
class Context:
    """What a translator knows about the source it reads."""

    home: Path
    path: Path
    session: str
    parent: str | None
    account: str | None
    now: str
    vendor_id: str | None = None
    #: False when the caller keeps only links and metadata (the metadata
    #: pass): the translator then reads no spill file.
    rows: bool = True


class Translator(Protocol):
    """One vendor's reader: where its sources are, which session each feeds,
    and the rows of one whole source line. `state` is JSON data, kept with the
    cursor, and the translator changes it in place."""

    vendor: str

    def sources(self, home: Path) -> Iterator[Path]: ...

    def identity(self, home: Path, path: Path) -> Identity | None: ...

    def translate(
        self, line: bytes, src: int, state: dict[str, Any], context: Context
    ) -> Output: ...

    def ended(self, state: dict[str, Any]) -> bool: ...


def dumps(value: Any) -> str:
    """The one JSON spelling of a stored row."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


# The table needs the row types above.
from . import claude

TRANSLATORS: Final[tuple[Translator, ...]] = (claude.TRANSLATOR,)
