"""LongMemEval dataset streaming loader, timestamp parser, and query term projection."""

from __future__ import annotations

import io
import itertools
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Turn:
    role: str
    content: str


@dataclass(frozen=True)
class Session:
    question_id: str
    position: int
    session_id: str
    valid_from: str
    content: str


@dataclass(frozen=True)
class Instance:
    question_id: str
    question_type: str
    question: str
    answer_session_ids: list[str]
    haystack_dates: list[str]
    haystack_session_ids: list[str]
    haystack_sessions: list[list[Turn]]

    def is_abstention(self) -> bool:
        """The instance is an abstention probe (question_id contains '_abs')."""
        return "_abs" in self.question_id

    def check(self) -> None:
        """Verify the 3 parallel arrays match and evidence constraints hold."""
        sessions = len(self.haystack_sessions)
        ids = len(self.haystack_session_ids)
        dates = len(self.haystack_dates)
        if sessions != ids or sessions != dates:
            raise DatasetError(
                f"instance {self.question_id} is misshapen: "
                f"{sessions} session(s), {ids} id(s), {dates} date(s)"
            )
        if not self.is_abstention() and not self.answer_session_ids:
            raise DatasetError(
                f"instance {self.question_id} carries no evidence session"
            )
        for evidence in self.answer_session_ids:
            if evidence not in self.haystack_session_ids:
                raise DatasetError(
                    f"instance {self.question_id} annotates evidence session {evidence}, "
                    "absent from its haystack"
                )

    def sessions(self) -> list[Session]:
        """Project every haystack session into a timestamped Session capsule."""
        self.check()
        result: list[Session] = []
        for position, (turns, session_id, stamp) in enumerate(
            zip(
                self.haystack_sessions,
                self.haystack_session_ids,
                self.haystack_dates,
                strict=True,
            )
        ):
            result.append(
                Session(
                    question_id=self.question_id,
                    position=position,
                    session_id=session_id,
                    valid_from=valid_from(stamp),
                    content=session_content(turns),
                )
            )
        return result


class DatasetError(Exception):
    """Dataset streaming, parsing, or shape verification failure."""


def session_content(turns: list[Turn]) -> str:
    """The bytes one session capsule carries: every turn, both roles, in order.

    The role is carried as `(role) content`, never `role: content`: the
    latter is literally one of the shapes the core's ROLE_PREFIX_INJECTION
    rule looks for, and the formatter was flagging 100% of what it ingested
    (longmemeval/full-results.json, 4700 of 4700 returned). The closing
    parenthesis sits between the role and any colon the content may start
    with, so no turn can reassemble the pattern. These bytes changed on
    2026-09-02 (N4c): no recall measured before that date compares with one
    measured after, so the re-baseline (N6) is mandatory, not optional.
    """
    parts: list[str] = []
    for turn in turns:
        parts.append(f"({turn.role}) {turn.content}")
    return "\n\n".join(parts)


def valid_from(stamp: str) -> str:
    """Parse 'YYYY/MM/DD (Day) HH:MM' into RFC3339 UTC instant."""
    reject_msg = (
        f"session timestamp {stamp!r} is not the dataset's YYYY/MM/DD (Day) HH:MM shape"
    )
    fields = stamp.split()
    if len(fields) != 3:
        raise DatasetError(reject_msg)
    date, weekday, time = fields
    if not (weekday.startswith("(") and weekday.endswith(")")):
        raise DatasetError(reject_msg)

    date_parts = date.split("/")
    if len(date_parts) != 3:
        raise DatasetError(reject_msg)
    y_str, m_str, d_str = date_parts
    if (
        len(y_str) != 4
        or not y_str.isdigit()
        or len(m_str) != 2
        or not m_str.isdigit()
        or len(d_str) != 2
        or not d_str.isdigit()
    ):
        raise DatasetError(reject_msg)
    year, month, day = int(y_str), int(m_str), int(d_str)
    if not (1970 <= year <= 9999 and 1 <= month <= 12 and 1 <= day <= 31):
        raise DatasetError(reject_msg)

    time_parts = time.split(":")
    if len(time_parts) != 2:
        raise DatasetError(reject_msg)
    h_str, min_str = time_parts
    if (
        len(h_str) != 2
        or not h_str.isdigit()
        or len(min_str) != 2
        or not min_str.isdigit()
    ):
        raise DatasetError(reject_msg)
    hour, minute = int(h_str), int(min_str)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise DatasetError(reject_msg)

    return f"{year:04d}-{month:02d}-{day:02d}T{hour:02d}:{minute:02d}:00Z"


def question_terms(question: str) -> list[str]:
    """The terms one recall call carries: alphanumeric runs, lowercased, deduplicated."""
    terms: list[str] = []
    runs = re.split(r"[^a-zA-Z0-9]+", question)
    for run in runs:
        if not run:
            continue
        term = run.lower()
        if term not in terms:
            terms.append(term)
    return terms


def _dict_to_instance(d: dict[str, Any]) -> Instance:
    """Convert raw decoded dict to typed Instance."""
    try:
        raw_sessions = d.get("haystack_sessions", [])
        haystack_sessions: list[list[Turn]] = []
        for session_turns in raw_sessions:
            haystack_sessions.append(
                [
                    Turn(role=t.get("role", ""), content=t.get("content", ""))
                    for t in session_turns
                ]
            )
        return Instance(
            question_id=d["question_id"],
            question_type=d.get("question_type", ""),
            question=d.get("question", ""),
            answer_session_ids=d.get("answer_session_ids", []),
            haystack_dates=d.get("haystack_dates", []),
            haystack_session_ids=d.get("haystack_session_ids", []),
            haystack_sessions=haystack_sessions,
        )
    except (KeyError, TypeError) as err:
        raise DatasetError(f"dataset JSON failed: {err}") from err


def read_first(
    source: io.BufferedReader | io.BytesIO | io.StringIO | bytes | str | Any,
    count: int,
) -> list[Instance]:
    """Read and validate the first count instances from any stream or bytes without whole-file heap bloat."""
    instances = list(itertools.islice(stream(source), count))
    if len(instances) < count:
        raise DatasetError(
            f"dataset holds {len(instances)} instance(s), fewer than the {count} the slice names"
        )
    return instances


def stream(
    source: io.BufferedReader | io.BytesIO | io.StringIO | bytes | str | Any,
) -> Iterator[Instance]:
    """Yield each validated instance in file order, reading only as far as the consumer asks.

    A question id names one instance: a repeated one is refused, since the
    proof a session capsule carries finds its question by that id.
    """
    seen: set[str] = set()
    if isinstance(source, bytes):
        text_stream: io.TextIOBase = io.StringIO(source.decode("utf-8"))
    elif isinstance(source, str):
        text_stream = io.StringIO(source)
    elif isinstance(source, io.TextIOBase):
        text_stream = source
    elif hasattr(source, "read"):
        text_stream = io.TextIOWrapper(source, encoding="utf-8")
    else:
        raise DatasetError(f"unsupported reader source: {type(source)}")

    decoder = json.JSONDecoder()
    chunk_size = 65536
    buffer = ""

    # Find opening '['
    while True:
        chunk = text_stream.read(chunk_size)
        if not chunk:
            break
        buffer += chunk
        bracket_idx = buffer.find("[")
        if bracket_idx != -1:
            buffer = buffer[bracket_idx + 1 :]
            break
    else:
        raise DatasetError("dataset JSON failed: missing opening bracket")

    while True:
        buffer = buffer.lstrip(" \t\r\n,")
        while not buffer or not buffer.startswith("{"):
            if buffer.startswith("]"):
                break
            chunk = text_stream.read(chunk_size)
            if not chunk:
                break
            buffer += chunk
            buffer = buffer.lstrip(" \t\r\n,")

        if not buffer or buffer.startswith("]"):
            break

        while True:
            try:
                obj, end_idx = decoder.raw_decode(buffer)
                instance = _dict_to_instance(obj)
                instance.check()
                buffer = buffer[end_idx:]
                break
            except json.JSONDecodeError:
                chunk = text_stream.read(chunk_size)
                if not chunk:
                    raise DatasetError("dataset JSON failed: malformed JSON")
                buffer += chunk
        if instance.question_id in seen:
            raise DatasetError(f"dataset repeats question {instance.question_id}")
        seen.add(instance.question_id)
        yield instance


def load_first(path: Path | str, count: int) -> list[Instance]:
    """Load the first count instances from a file on disk."""
    p = Path(path)
    try:
        with open(p, "rb") as f:
            return read_first(f, count)
    except FileNotFoundError as err:
        raise DatasetError(f"dataset I/O failed at {p}: {err}") from err
    except OSError as err:
        raise DatasetError(f"dataset I/O failed at {p}: {err}") from err


def find_session(
    path: Path | str, question_id: str, position: int, session_id: str
) -> Session | None:
    """The session one question's haystack holds at one position, exactly as ingested.

    The question and the position are both part of the name: the dataset reuses
    a session id across questions, and even within one question's haystack,
    with the same turns and a different date. The id is a cross-check.
    """
    p = Path(path)
    try:
        with open(p, "rb") as f:
            for instance in stream(f):
                if instance.question_id == question_id:
                    sessions = instance.sessions()
                    if 0 <= position < len(sessions):
                        session = sessions[position]
                        if session.session_id == session_id:
                            return session
                    return None
    except OSError as err:
        raise DatasetError(f"dataset I/O failed at {p}: {err}") from err
    return None
