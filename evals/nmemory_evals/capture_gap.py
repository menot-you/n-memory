"""Subcommand and engine of the historical capture-gap audit (U2).

Measures persistence's numerator and denominator per LOCK 2:
- Denominator: agent findings + owner orders that should have become capsules.
- Numerator: statements whose human label `cap-<id>` VERIFIES against the live
  store.
- Gap: lost statements (LOST) that died in the session's transcript.

The measurer NEVER discovers by similarity. It takes the `cap-<id>` a human
wrote and answers three mechanical questions about THAT capsule: it exists and
is not tombstoned; its anchor or its `capture_context.session_id` names the
item's session; its `valid_from` falls inside that session's window. A label
that fails any one is INVALID — it stays in the denominator, leaves the
numerator, and makes the command exit `3`. Discovery by similarity would be the
judge inventing the verdict (PLAN.md:143-146).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import nott_database
from nmemory import NMemoryError
from nmemory.repositories.capsules import Tombstoned
from nmemory.store import Store
from nott_database import DatabaseError


class EvalRefusal(Exception):
    """Typed D18 refusal (exit 2): the store, transcript, or dataset is missing.

    A missing environment is a REFUSAL, never a skip and never green — a judge
    that skips for lack of a database is a judge that lies.
    """


@dataclass(frozen=True)
class GoldCaptureItem:
    id: str
    session_id: str
    session_type: str
    timestamp: str
    origin_type: str
    statement: str
    target_path: str
    kind: str
    status: str  # "captured" | "lost"
    capsule_id: str | None = None
    #: Transcript `.jsonl`: relative paths resolve beside the dataset on load;
    #: absolute paths retain their legacy meaning. Private raw transcripts stay
    #: external; only reviewed, sanitized corpora belong in the repository.
    #: Used by `session_window` when the store has no session record.
    transcript_path: str | None = None
    #: Verbatim span anchor (N5g), frozen BEFORE any extractor ran:
    #: `{"transcript_path", "turn_index", "start", "end", "span_text"}` over the
    #: turns of `transcript_turns` — `turns[turn_index][start:end] == span_text`.
    #: `None` when the statement was inferred by the labeler or the transcript
    #: is not on this box; `anchor_reason` says which. The capture-gap does not
    #: read it; `tests/test_gold_anchors.py` verifies it.
    anchor: dict[str, Any] | None = None
    anchor_reason: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GoldCaptureItem:
        return cls(
            id=str(data["id"]),
            session_id=str(data["session_id"]),
            session_type=str(data["session_type"]),
            timestamp=str(data["timestamp"]),
            origin_type=str(data["origin_type"]),
            statement=str(data["statement"]),
            target_path=str(data["target_path"]),
            kind=str(data["kind"]),
            status=str(data["status"]),
            capsule_id=str(data["capsule_id"]) if data.get("capsule_id") else None,
            transcript_path=str(data["transcript_path"])
            if data.get("transcript_path")
            else None,
            anchor=dict(data["anchor"]) if data.get("anchor") else None,
            anchor_reason=str(data["anchor_reason"])
            if data.get("anchor_reason")
            else None,
        )


@dataclass(frozen=True)
class SessionWindow:
    """A session's window. `end=None` is a session still open."""

    start: datetime
    end: datetime | None

    def contains(self, at: datetime) -> bool:
        if at < self.start:
            return False
        return self.end is None or at <= self.end


#: Verifies ONE label: returns `None` when it holds, otherwise the refusal's
#: exact reason. It is a function so the test can inject its own without
#: touching the store — never so production can do without it.
CaptureVerifier = Callable[[GoldCaptureItem], str | None]


@dataclass(frozen=True)
class BreakdownStats:
    total: int
    captured: int
    lost: int
    #: Labeled `captured` by a human and refused by the mechanical verification.
    #: Stays in the denominator — the statement was made — and out of the
    #: numerator.
    invalid: int
    capture_rate: float


@dataclass(frozen=True)
class CaptureGapReport:
    total_sessions: int
    total_items: int
    captured_items: int
    lost_items: int
    invalid_items: int
    capture_rate: float
    by_session: dict[str, BreakdownStats]
    by_session_type: dict[str, BreakdownStats]
    by_origin_type: dict[str, BreakdownStats]
    lost_details: list[dict[str, Any]]
    invalid_details: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_sessions": self.total_sessions,
            "total_items": self.total_items,
            "captured_items": self.captured_items,
            "lost_items": self.lost_items,
            "invalid_items": self.invalid_items,
            "capture_rate": round(self.capture_rate, 4),
            "by_session": {k: asdict(v) for k, v in self.by_session.items()},
            "by_session_type": {k: asdict(v) for k, v in self.by_session_type.items()},
            "by_origin_type": {k: asdict(v) for k, v in self.by_origin_type.items()},
            "lost_details": self.lost_details,
            "invalid_details": self.invalid_details,
        }


def load_capture_gold_set(path: Path | str | None = None) -> list[GoldCaptureItem]:
    """Load gold metadata, resolving relative transcript paths beside the dataset."""
    if path is None:
        path = Path(__file__).parent / "capture_gold_set.json"
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError as err:
        raise EvalRefusal(f"unreadable capture gold set at {path}: {err}") from err
    data = json.loads(raw)
    dataset_dir = Path(path).resolve().parent
    for item in data["items"]:
        for source in (item, item.get("anchor") or {}):
            transcript = source.get("transcript_path")
            if transcript and not Path(transcript).is_absolute():
                source["transcript_path"] = str(dataset_dir / transcript)
    return [GoldCaptureItem.from_dict(it) for it in data["items"]]


def open_live_store() -> Store:
    """The nMEMORY store, read-only. It is the subject of this audit.

    `allow_live_store` is an ARGUMENT and never the environment: every caller
    WRITES its own choice. The choice here is honest and narrow — the capsules
    this measurer verifies exist only in the live store, and the handle is
    `read_only=True`.
    """
    try:
        return Store.open(read_only=True, allow_live_store=True)
    except (NMemoryError, DatabaseError, nott_database.Error) as err:
        raise EvalRefusal(
            f"the nMEMORY store is unavailable, so there is nothing to verify: {err}"
        ) from err


@dataclass(frozen=True)
class TranscriptEntry:
    """A line of the harness `.jsonl`, reduced to what the evals use."""

    at: datetime | None
    #: The text a human or the agent wrote: the `text` and `thinking` blocks of
    #: `assistant`/`user` turns. Empty for everything else — `tool_use`,
    #: `tool_result`, hooks, modes. A turn without text is not a turn.
    text: str


def _block_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        for key in ("text", "thinking"):
            value = block.get(key)
            if isinstance(value, str):
                parts.append(value)
    return "\n".join(parts)


def read_transcript(path: Path) -> list[TranscriptEntry]:
    """The harness `.jsonl`. ONE parser, two consumers: the window uses the
    timestamps of ALL lines, the turns use the ones carrying text."""
    entries: list[TranscriptEntry] = []
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                raw = json.loads(line)
                at = raw.get("timestamp")
                text = ""
                if raw.get("type") in ("assistant", "user"):
                    message = raw.get("message")
                    if isinstance(message, dict):
                        text = _block_text(message.get("content"))
                entries.append(
                    TranscriptEntry(
                        at=datetime.fromisoformat(at) if isinstance(at, str) else None,
                        text=text,
                    )
                )
    except OSError as err:
        raise EvalRefusal(f"transcript ausente ou ilegivel em {path}: {err}") from err
    return entries


def transcript_turns(path: Path) -> list[str]:
    """The turns with text — what an auto-capture mechanism would see."""
    return [e.text for e in read_transcript(path) if e.text.strip()]


def transcript_window(path: Path) -> SessionWindow:
    """A harness session's window: the `.jsonl`'s first and last turn."""
    stamps = [e.at for e in read_transcript(path) if e.at is not None]
    if not stamps:
        raise EvalRefusal(f"transcript {path} has no turn with a timestamp at all")
    return SessionWindow(start=min(stamps), end=max(stamps))


def session_window(store: Store, item: GoldCaptureItem) -> SessionWindow | None:
    """The item's session window, or `None` when no source knows it.

    There are TWO identifier spaces in the gold set, measured, not chosen:
    `sess-N` is an nMEMORY session record and the store has its window; the
    Claude Code UUID is a harness session, the store does not record it, and
    the window comes from the transcript. Store first — it is the source the
    mechanism itself wrote.
    """
    record = store.sessions.get_session(item.session_id)
    if record is not None:
        started: datetime = record.started_at
        finished: datetime | None = record.finished_at
        return SessionWindow(start=started, end=finished)
    if item.transcript_path is not None:
        return transcript_window(Path(item.transcript_path))
    return None


def names_session(session_id: str, candidate: str) -> bool:
    """The candidate NAMES the session: equal to it, or the UUID it prefixes.

    The prefix is a declared boundary (`6da118b7` + `-` in the UUID), not a
    similarity: the gold set writes the short prefix and `capture_context`
    keeps the whole UUID.
    """
    return candidate == session_id or candidate.startswith(f"{session_id}-")


def store_verifier(store: Store) -> CaptureVerifier:
    """The real verifier. Returns `None` when the label holds."""

    def verify(item: GoldCaptureItem) -> str | None:
        if item.capsule_id is None:
            return "rotulado `captured` sem `capsule_id`"

        try:
            stored = store.capsules.get(item.capsule_id)
        except Tombstoned:
            return f"{item.capsule_id} esta tombstoned"
        if stored is None:
            return f"{item.capsule_id} nao existe no store"

        anchor = str(stored.capsule.provenance.anchor)
        context = store.capsules.get_capture_context(item.capsule_id)
        ctx_session = str(context.session_id) if context is not None else ""
        column = str(stored.session_id) if stored.session_id else ""
        if not any(
            names_session(item.session_id, candidate)
            for candidate in (anchor, ctx_session, column)
        ):
            return (
                f"{item.capsule_id} nao nasceu em {item.session_id}: "
                f"provenance.anchor={anchor!r}, "
                f"capture_context.session_id={ctx_session!r}, "
                f"capsules.session_id={column!r}"
            )

        window = session_window(store, item)
        if window is None:
            return (
                f"session {item.session_id} without a window: no session record in the "
                "store and no `transcript_path` in the gold set"
            )
        valid_from: datetime = stored.capsule.freshness.valid_from
        if not window.contains(valid_from):
            return (
                f"{item.capsule_id}.valid_from={valid_from.isoformat()} fora da "
                f"janela de {item.session_id} "
                f"[{window.start.isoformat()}, "
                f"{window.end.isoformat() if window.end else 'aberta'}]"
            )
        return None

    return verify


def calculate_breakdown(
    items: list[tuple[GoldCaptureItem, str | None]],
    key_fn: Callable[[GoldCaptureItem], str],
) -> dict[str, BreakdownStats]:
    groups: dict[str, list[tuple[GoldCaptureItem, str | None]]] = {}
    for it, reason in items:
        groups.setdefault(key_fn(it), []).append((it, reason))

    breakdown: dict[str, BreakdownStats] = {}
    for key, group in sorted(groups.items()):
        tot = len(group)
        cap = sum(
            1 for it, reason in group if it.status == "captured" and reason is None
        )
        inv = sum(
            1 for it, reason in group if it.status == "captured" and reason is not None
        )
        lst = tot - cap - inv
        breakdown[key] = BreakdownStats(
            total=tot,
            captured=cap,
            lost=lst,
            invalid=inv,
            capture_rate=round(cap / tot if tot > 0 else 0.0, 4),
        )
    return breakdown


def run_capture_gap_audit(
    items: list[GoldCaptureItem], verifier: CaptureVerifier
) -> CaptureGapReport:
    """The audit. `verifier` is MANDATORY: without it the numerator would be a
    string comparison against a field a human wrote, and the measurer would
    start believing the label instead of verifying it."""
    verdicts: list[tuple[GoldCaptureItem, str | None]] = [
        (it, verifier(it) if it.status == "captured" else None) for it in items
    ]

    captured = sum(
        1 for it, reason in verdicts if it.status == "captured" and reason is None
    )
    invalid = sum(
        1 for it, reason in verdicts if it.status == "captured" and reason is not None
    )
    total = len(items)
    lost = total - captured - invalid
    sessions = {it.session_id for it in items}

    return CaptureGapReport(
        total_sessions=len(sessions),
        total_items=total,
        captured_items=captured,
        lost_items=lost,
        invalid_items=invalid,
        capture_rate=round(captured / total if total > 0 else 0.0, 4),
        by_session=calculate_breakdown(verdicts, lambda it: it.session_id),
        by_session_type=calculate_breakdown(verdicts, lambda it: it.session_type),
        by_origin_type=calculate_breakdown(verdicts, lambda it: it.origin_type),
        lost_details=[
            {
                "id": it.id,
                "session_id": it.session_id,
                "session_type": it.session_type,
                "origin_type": it.origin_type,
                "statement": it.statement,
                "target_path": it.target_path,
                "kind": it.kind,
            }
            for it, _ in verdicts
            if it.status == "lost"
        ],
        invalid_details=[
            {
                "id": it.id,
                "session_id": it.session_id,
                "capsule_id": it.capsule_id,
                "statement": it.statement,
                "reason": reason,
            }
            for it, reason in verdicts
            if it.status == "captured" and reason is not None
        ],
    )


def render_capture_gap_markdown(report: CaptureGapReport) -> str:
    lines = [
        "# Capture Gap Audit Report (nMEMORY)",
        "",
        f"- **Total Sessions Audited**: {report.total_sessions}",
        f"- **Total Persistable Statements (Denominator)**: {report.total_items}",
        f"- **Statements Persisted and VERIFIED (Numerator)**: {report.captured_items}",
        f"- **Statements Lost (LOST)**: {report.lost_items}",
        f"- **INVALID Labels (refused by verification)**: {report.invalid_items}",
        f"- **Global Capture Rate**: {report.capture_rate * 100:.1f}%",
        f"- **Measured Gap**: {(1.0 - report.capture_rate) * 100:.1f}%",
        "",
        "## Breakdown by Session",
        "",
        "| Session | Total | Captured | Lost | INVALID | Capture Rate |",
        "|---|---|---|---|---|---|",
    ]

    for sid, stats in report.by_session.items():
        lines.append(
            f"| `{sid}` | {stats.total} | {stats.captured} | {stats.lost} | "
            f"{stats.invalid} | **{stats.capture_rate * 100:.1f}%** |"
        )

    lines.extend(
        [
            "",
            "## Breakdown by Session Type",
            "",
            "| Session Format | Total | Captured | Lost | INVALID | Capture Rate |",
            "|---|---|---|---|---|---|",
        ]
    )

    for stype, stats in report.by_session_type.items():
        lines.append(
            f"| `{stype}` | {stats.total} | {stats.captured} | {stats.lost} | "
            f"{stats.invalid} | **{stats.capture_rate * 100:.1f}%** |"
        )

    lines.extend(
        [
            "",
            "## Breakdown by Information Origin",
            "",
            "| Origin | Total | Captured | Lost | INVALID | Capture Rate |",
            "|---|---|---|---|---|---|",
        ]
    )

    for otype, stats in report.by_origin_type.items():
        lines.append(
            f"| `{otype}` | {stats.total} | {stats.captured} | {stats.lost} | "
            f"{stats.invalid} | **{stats.capture_rate * 100:.1f}%** |"
        )

    if report.invalid_details:
        lines.extend(
            [
                "",
                "## INVALID Labels — verification refused",
                "",
                "| Item | Session | Capsule | Reason |",
                "|---|---|---|---|",
            ]
        )
        for row in report.invalid_details:
            lines.append(
                f"| `{row['id']}` | `{row['session_id']}` | "
                f"`{row['capsule_id']}` | {row['reason']} |"
            )

    return "\n".join(lines) + "\n"
