"""The Claude Code translator: its source files, the session each one feeds and
the v1 rows of each whole line.

Pure: it reads no file except a spill file Claude Code wrote into the session's
own `tool-results/` directory, and that file only when the caller keeps rows
(`Context.rows`). The writer (`serve`) gives it whole lines, the state kept
with the cursor, and writes what it returns.

Sources, under each Claude home:
- `projects/<project>/<session>.jsonl`, a main session, id `<session>`;
- `projects/<project>/<parent>/subagents/agent-<agent>.jsonl`, a subagent,
  id `<parent>.<agent>`. Its lines carry the parent's `sessionId`, so the id
  comes from the path, never from a line;
- `projects/<project>/<parent>/subagents/workflows/wf_<id>/agent-<agent>.jsonl`,
  an agent of the Workflow tool, the same id `<parent>.<agent>` as a subagent.
"""

from __future__ import annotations

import base64
import binascii
import errno
import hashlib
import json
import os
import re
import stat
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any, Final

from ..vendor_home import open_owner_path
from . import (
    ChildRow,
    CompactionRow,
    ConfigRow,
    Context,
    HookRow,
    Identity,
    Media,
    MessageRow,
    Output,
    ReasoningRow,
    Row,
    SessionRow,
    ToolCallRow,
    ToolResultRow,
    Usage,
)

#: A session id the read verbs accept; a source with another name is skipped.
SESSION_ID: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
#: Line types that are Claude Code's own bookkeeping: no row, not counted.
BOOKKEEPING: Final = frozenset(
    {
        "queue-operation",
        "mode",
        "permission-mode",
        "atis-latch",
        "ai-title",
        "custom-title",
        "agent-name",
        "agent-color",
        "frame-link",
        "pr-link",
        "cost-state",
        "last-prompt",
        "output_style",
        "system/turn_duration",
        "system/informational",
    }
)
BOOKKEEPING_PREFIXES: Final = ("api-request", "file-history-")
#: Attachments whose body Claude Code injects into the model's context: the row
#: keeps the kind and the size only.
INJECTING: Final = frozenset(
    {"hook_additional_context", "task_reminder", "total_tokens_reminder"}
)
#: Hook attachments and the outcome each one states (`async_hook_response`
#: follows its exit code).
HOOKS: Final = {
    "hook_success": "ok",
    "async_hook_response": "ok",
    "hook_non_blocking_error": "error",
    "hook_blocking_error": "blocked",
    "hook_cancelled": "cancelled",
}
#: Injected blocks in owner text, by tag, and the kind each one records.
TAGS: Final = {"system-reminder": "reminder", "user-prompt-submit-hook": "hook"}
INLINE: Final = re.compile(
    r"<(system-reminder|user-prompt-submit-hook)>.*?</\1>[ \t]*\n?", re.DOTALL
)
WHOLE: Final = re.compile(
    r"\s*<(system-reminder|user-prompt-submit-hook)>.*</\1>\s*", re.DOTALL
)
REMINDER_OPEN: Final = "<system-reminder>"
REMINDER_CLOSE: Final = "</system-reminder>"
#: The image media types kept as files; another type gives a marker.
IMAGE_TYPES: Final = {
    "image/png": "png",
    "image/jpeg": "jpeg",
    "image/gif": "gif",
    "image/webp": "webp",
}
#: Claude Code's own spill reference, and the names it gives spill files.
SPILL: Final = re.compile(r"Full output saved to: (\S+)")
SPILL_NAME: Final = re.compile(r"[A-Za-z0-9_-]+\.txt")
SPILL_MAX_BYTES: Final = 64 * 1024 * 1024
#: Tools whose result names a subagent, and the nSPAWN start tool's suffix.
SUBAGENT_TOOLS: Final = frozenset({"Task", "Agent"})
RUN_START: Final = "nspawn_run_start"
#: The run id of a Workflow, the name of its `wf_<id>` folder.
WORKFLOW_RUN: Final = re.compile(r"wf_[A-Za-z0-9-]{1,64}")
#: The id of a Workflow agent, the `<id>` of its `agent-<id>.jsonl` file.
WORKFLOW_AGENT: Final = re.compile(r"[A-Za-z0-9_-]{1,64}")
WORKFLOW_TOOL: Final = "Workflow"
#: The longest tool call id that the Workflow state keeps.
CALL_MAX_CHARS: Final = 128
#: The head of the notification that Claude Code gives the parent when one of
#: its background tasks ends: the tool call that started the task, and the
#: terminal status. Claude Code escapes `<` in the task's own text, so a task
#: cannot write this head.
TASK_END: Final = re.compile(
    r"<task-notification>\n<task-id>[^<\n]*</task-id>\n"
    rf"<tool-use-id>([^<\n]{{1,{CALL_MAX_CHARS}}})</tool-use-id>\n"
    r"(?:<output-file>[^<\n]*</output-file>\n)?"
    r"<status>(completed|failed|killed|stopped)</status>\n"
)
TASK_NOTIFICATION: Final = "task-notification"
#: The state keys of the Workflow launches of a main session
#: (`workflow_record`). `workflow_pending` holds each Workflow tool call that
#: waits for its result, as `[position, target]`. `workflows` holds each
#: tracked run with an open launch, and each closed run that waits for its
#: closing sequence or for the certificate of a member. `workflow_calls`
#: holds the run of each open launch.
WORKFLOW_KEYS: Final = ("workflows", "workflow_calls", "workflow_pending")
SYNTHETIC: Final = "<synthetic>"
#: A line that can hold a lone UTF-16 surrogate: a JSON escape of one, or its raw
#: bytes. Claude Code writes one when it cuts text between the two halves of a
#: character outside the BMP.
SURROGATE_LINE: Final = re.compile(rb"\\u[dD][89a-fA-F]|\xed[\xa0-\xbf]")
SURROGATE: Final = re.compile("[\ud800-\udfff]")


def _valid(value: Any) -> Any:
    """`value` with each lone surrogate in each text replaced by U+FFFD, so that
    every text is valid UTF-8; all other characters stay."""
    if isinstance(value, str):
        return SURROGATE.sub("\ufffd", value)
    if isinstance(value, list):
        return [_valid(item) for item in value]
    if isinstance(value, dict):
        return {_valid(key): _valid(item) for key, item in value.items()}
    return value


def _size(text: str) -> int:
    return len(text.encode("utf-8"))


def _body_size(body: Any) -> int:
    """The bytes of an injected body: a text's own bytes, a list of texts summed,
    anything else as compact JSON."""
    if isinstance(body, str):
        return _size(body)
    if isinstance(body, list) and all(isinstance(item, str) for item in body):
        return sum(_size(item) for item in body)
    return _size(json.dumps(body, ensure_ascii=False, separators=(",", ":")))


class Injected:
    """The injected blocks stripped from one row: their kinds and bytes."""

    def __init__(self) -> None:
        self.kinds: list[str] = []
        self.bytes = 0

    def add(self, kind: str, size: int) -> None:
        if kind not in self.kinds:
            self.kinds.append(kind)
        self.bytes += size

    def shown(self) -> dict[str, Any] | None:
        if not self.kinds:
            return None
        return {"kind": "+".join(self.kinds), "bytes": self.bytes}


def _whole(text: str) -> bool:
    """Whether `text` is injected blocks and white space only."""
    return WHOLE.fullmatch(text) is not None and not INLINE.sub("", text).strip()


def _owner_text(text: str, injected: Injected) -> str:
    """`text` with every injected block removed; each one is sized."""
    if _whole(text):
        for found in INLINE.finditer(text):
            injected.add(TAGS[found.group(1)], 0)
        injected.bytes += _size(text)
        return ""

    def strip(found: re.Match[str]) -> str:
        injected.add(TAGS[found.group(1)], _size(found.group(0)))
        return ""

    return INLINE.sub(strip, text)


def _appended(text: str, injected: Injected) -> str:
    """`text` without the reminder blocks Claude Code appended at its end."""
    kept = text
    while kept.rstrip().endswith(REMINDER_CLOSE):
        start = kept.rfind(REMINDER_OPEN)
        if start < 0:
            break
        while start > 0 and kept[start - 1] == "\n":
            start -= 1
        kept = kept[:start]
    if kept != text:
        injected.add("reminder", _size(text) - _size(kept))
    return kept


def _media(block: dict[str, Any], media: list[Media]) -> dict[str, Any] | None:
    """The row's entry for one image or document block, and its file to write."""
    source = block.get("source")
    if not isinstance(source, dict):
        return None
    mime = source.get("media_type")
    data = source.get("data")
    if not isinstance(mime, str) or not isinstance(data, str):
        return None
    try:
        decoded = base64.b64decode(data, validate=False)
    except binascii.Error, ValueError:
        decoded = b""
    digest = hashlib.sha256(decoded).hexdigest()
    ext = IMAGE_TYPES.get(mime)
    entry: dict[str, Any] = {
        "kind": str(block.get("type")),
        "mime": mime,
        "bytes": len(decoded),
        "sha256": digest,
        "file": None,
    }
    if ext is not None and block.get("type") == "image":
        item = Media(digest, ext, decoded)
        entry["file"] = item.file
        if all(known.file != item.file for known in media):
            media.append(item)
    return entry


def _number(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _agents(folder: Path, *, nested: bool = False) -> Iterator[Path]:
    """Each `agent-<agent>.jsonl` in `folder`, and when `nested`, each one in
    `folder/workflows/wf_<id>/`, where the Workflow tool keeps its agents."""
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return
    for entry in entries:
        if (
            entry.name.startswith("agent-")
            and entry.name.endswith(".jsonl")
            and entry.is_file(follow_symlinks=False)
        ):
            yield Path(entry.path)
        elif (
            nested and entry.name == "workflows" and entry.is_dir(follow_symlinks=False)
        ):
            try:
                flows = list(os.scandir(entry.path))
            except OSError:
                continue
            for flow in flows:
                if flow.name.startswith("wf_") and flow.is_dir(follow_symlinks=False):
                    yield from _agents(Path(flow.path))


def session_folder(home: Path, path: Path) -> Path | None:
    """The folder `projects/<project>/<session>/` of the session that owns the
    source `path`, which holds its `tool-results/`. It is the folder of the main
    session itself, or the parent's folder of an Agent-tool subagent or a
    Workflow agent. Only the name of a main transcript loses its `.jsonl`: a
    parent folder keeps its full name. None for a path outside `projects/`."""
    try:
        parts = path.relative_to(home / "projects").parts
    except ValueError:
        return None
    if len(parts) < 2:
        return None
    folder = parts[1].removesuffix(".jsonl") if len(parts) == 2 else parts[1]
    return home / "projects" / parts[0] / folder


def workflow_journal(home: Path, path: Path) -> Path | None:
    """The journal `wf_<id>/journal.jsonl` of the Workflow tool, for the
    Workflow agent at `path`. None for a source of another shape."""
    try:
        parts = path.relative_to(home / "projects").parts
    except ValueError:
        return None
    if (
        len(parts) == 6
        and parts[2:4] == ("subagents", "workflows")
        and parts[4].startswith("wf_")
    ):
        return path.parent / "journal.jsonl"
    return None


class WorkflowListingRefused(Exception):
    """A listing of a Workflow run folder failed. The message is the reason:
    the code of the operating system error."""


def _listing_refused(error: OSError) -> WorkflowListingRefused:
    code = errno.errorcode.get(error.errno) if error.errno is not None else None
    return WorkflowListingRefused(
        f"workflow folder listing failed: {code or type(error).__name__}"
    )


def workflow_agents(flow: Path) -> tuple[str, ...]:
    """The agent ids of the Workflow run folder `flow`. One complete
    listing, made for this call, gives the `<id>` of each regular file
    `agent-<id>.jsonl`. A folder that does not exist gives no ids.

    Raises:
        WorkflowListingRefused: the listing, or the file check of one of its
            entries, failed. No caller takes a part of a listing for all of
            it.
    """
    try:
        with os.scandir(flow) as entries:
            found = list(entries)
    except FileNotFoundError:
        return ()
    except OSError as error:
        raise _listing_refused(error) from error
    agents: list[str] = []
    for entry in found:
        name = entry.name
        if not (name.startswith("agent-") and name.endswith(".jsonl")):
            continue
        agent = name.removeprefix("agent-").removesuffix(".jsonl")
        try:
            regular = entry.is_file(follow_symlinks=False)
        except OSError as error:
            raise _listing_refused(error) from error
        if WORKFLOW_AGENT.fullmatch(agent) and regular:
            agents.append(agent)
    return tuple(sorted(agents))


def _workflow_forget(run: str, state: dict[str, Any]) -> None:
    """Stop tracking `run`: its agents keep the other status rules."""
    runs = state.get("workflows", {})
    calls = state.get("workflow_calls", {})
    entry = runs.pop(run, None)
    if isinstance(entry, dict):
        for call in entry.get("open", []):
            calls.pop(call, None)


def _workflow_result(
    result: Any, call: str, src: int, single: bool, state: dict[str, Any]
) -> None:
    """Record the result of the Workflow tool call `call`, at the parent
    position `src`. A launch result names the run. The journal tracks a run
    only from a launch of a new run that it reads: a call with no
    `resumeFromRunId`. So it knows each launch of the run. A resume adds an
    open launch to a tracked run. A resume of a run that the journal does
    not track changes nothing. A Workflow call can have resumed its target
    when its result is not one clear launch. Then the journal stops tracking
    that run, or each run when it does not know the target."""
    key = call[:CALL_MAX_CHARS]
    pending = state.get("workflow_pending", {}).pop(key, None)
    run = result.get("runId") if isinstance(result, dict) else None
    if (
        single
        and isinstance(result, dict)
        and result.get("taskType") == "local_workflow"
        and isinstance(run, str)
        and WORKFLOW_RUN.fullmatch(run)
        and 0 < len(call) <= CALL_MAX_CHARS
    ):
        target = pending[1] if isinstance(pending, list) else None
        if isinstance(target, str) and target not in ("", run):
            # A resume of one run that names another: the target can run.
            _workflow_forget(target, state)
        runs = state.setdefault("workflows", {})
        entry = runs.get(run)
        if not isinstance(entry, dict):
            if not (isinstance(pending, list) and pending[1] == ""):
                return
            entry = runs[run] = {"open": []}
        entry.pop("members", None)
        entry.update(
            call=call,
            called=pending[0] if isinstance(pending, list) else None,
            at=src,
            status=None,
            end=None,
        )
        entry["open"].append(call)
        state.setdefault("workflow_calls", {})[call] = run
        return
    if not isinstance(pending, list) or pending[1] == "":
        # Not a Workflow call, or a launch of a new run that failed.
        return
    target = pending[1]
    for each in (
        [target] if isinstance(target, str) else list(state.get("workflows", {}))
    ):
        _workflow_forget(each, state)


def _task_ended(text: str, src: int, state: dict[str, Any]) -> None:
    """Record each end of a tracked Workflow launch that a task notification
    in `text` holds, at the parent position `src`. One lookup in
    `workflow_calls` finds the launch. The end of the latest launch of a run
    keeps its status and position. The end of an older launch removes only
    that launch from the open launches of the run. A notification for
    another task changes nothing."""
    calls = state.get("workflow_calls")
    runs = state.get("workflows")
    if not isinstance(calls, dict) or not isinstance(runs, dict):
        return
    for found in TASK_END.finditer(text):
        call, status = found.groups()
        run = calls.pop(call, None)
        entry = runs.get(run) if isinstance(run, str) else None
        if not isinstance(entry, dict):
            continue
        if call in entry["open"]:
            entry["open"].remove(call)
        if entry.get("call") == call:
            entry.update(status=status, end=src)


def workflow_record(record: dict[str, Any], src: int, state: dict[str, Any]) -> None:
    """Record the Workflow lifecycle that one record of a main session holds,
    at the parent position `src`. The record can hold a Workflow tool call,
    its result, or the task notification of an end. The notification is a
    user turn with the origin `task-notification`, or a `queued_command`
    attachment with that command mode. Owner text that looks like a
    notification is neither."""
    kind = record.get("type")
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if kind == "assistant":
        for block in content if isinstance(content, list) else []:
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_use"
                and block.get("name") == WORKFLOW_TOOL
            ):
                given = block.get("input")
                target: str | None = None
                if isinstance(given, dict):
                    resume = given.get("resumeFromRunId", "")
                    if resume == "" and "resumeFromRunId" not in given:
                        target = ""
                    elif isinstance(resume, str) and WORKFLOW_RUN.fullmatch(resume):
                        target = resume
                call = str(block.get("id") or "")[:CALL_MAX_CHARS]
                state.setdefault("workflow_pending", {})[call] = [src, target]
    elif kind == "user":
        blocks = content if isinstance(content, list) else [{"text": content}]
        origin = record.get("origin")
        if isinstance(origin, dict) and origin.get("kind") == TASK_NOTIFICATION:
            for block in blocks:
                words = block.get("text") if isinstance(block, dict) else None
                if isinstance(words, str):
                    _task_ended(words, src, state)
        results = [
            block
            for block in blocks
            if isinstance(block, dict) and block.get("type") == "tool_result"
        ]
        for block in results:
            call = str(block.get("tool_use_id") or "")
            _workflow_result(
                record.get("toolUseResult"), call, src, len(results) == 1, state
            )
    elif kind == "attachment":
        body = record.get("attachment")
        if (
            isinstance(body, dict)
            and body.get("type") == "queued_command"
            and body.get("commandMode") == TASK_NOTIFICATION
            and isinstance(body.get("prompt"), str)
        ):
            _task_ended(body["prompt"], src, state)


def workflow_events(data: bytes) -> tuple[dict[str, str], dict[str, int]]:
    """What the Workflow journal records for its agents. First, the status of
    each agent whose end it records: `ended` for its `result` line and
    `failed` for its `failed` line. Second, the number of `started` lines of
    each agent: a launch writes one for each agent that it runs. As for a
    transcript, a line feed alone ends a record, and the bytes after the last
    line feed are not yet a record. A record that is not a JSON object, or
    that holds a carriage return, gives nothing."""
    ends: dict[str, str] = {}
    starts: dict[str, int] = {}
    for line in data.split(b"\n")[:-1]:
        if b"\r" in line:
            continue
        try:
            record = json.loads(line)
        except ValueError, RecursionError:
            continue
        if not isinstance(record, dict):
            continue
        agent, kind = record.get("agentId"), record.get("type")
        if not isinstance(agent, str) or not isinstance(kind, str):
            continue
        if kind == "result":
            ends[agent] = "ended"
        elif kind == "failed":
            ends[agent] = "failed"
        elif kind == "started":
            starts[agent] = starts.get(agent, 0) + 1
    return ends, starts


class ClaudeTranslator:
    vendor = "claude"

    # -- discovery ---------------------------------------------------------------

    def sources(self, home: Path) -> Iterator[Path]:
        """Each session file under `<home>/projects`, by `os.scandir`."""
        try:
            projects = list(os.scandir(home / "projects"))
        except OSError:
            return
        for project in projects:
            if not project.is_dir(follow_symlinks=False):
                continue
            try:
                entries = list(os.scandir(project.path))
            except OSError:
                continue
            for entry in entries:
                if entry.name.endswith(".jsonl") and entry.is_file(
                    follow_symlinks=False
                ):
                    yield Path(entry.path)
                elif entry.is_dir(follow_symlinks=False):
                    yield from _agents(Path(entry.path) / "subagents", nested=True)

    def identity(self, home: Path, path: Path) -> Identity | None:
        try:
            parts = path.relative_to(home / "projects").parts
        except ValueError:
            return None
        if len(parts) == 2 and parts[1].endswith(".jsonl"):
            session, parent = parts[1].removesuffix(".jsonl"), None
        elif (
            (
                len(parts) == 4
                or len(parts) == 6
                and parts[3] == "workflows"
                and parts[4].startswith("wf_")
            )
            and parts[2] == "subagents"
            and parts[-1].startswith("agent-")
            and parts[-1].endswith(".jsonl")
        ):
            parent = parts[1]
            session = (
                f"{parent}.{parts[-1].removeprefix('agent-').removesuffix('.jsonl')}"
            )
            if not SESSION_ID.fullmatch(parent):
                return None
        else:
            return None
        return Identity(session, parent) if SESSION_ID.fullmatch(session) else None

    def ended(self, state: dict[str, Any]) -> bool:
        """Whether the last agent line ended its turn."""
        return bool(state.get("end"))

    # -- one line ----------------------------------------------------------------

    def translate(
        self, line: bytes, src: int, state: dict[str, Any], context: Context
    ) -> Output:
        output = Output()
        try:
            record = json.loads(line)
            if SURROGATE_LINE.search(line):
                record = _valid(record)
        except ValueError, RecursionError:
            output.unknown = "malformed"
            return output
        if not isinstance(record, dict):
            output.unknown = "malformed"
            return output
        if context.parent is None:
            workflow_record(record, src, state)
        metadata = state.setdefault("metadata", {})
        for key in ("cwd", "entrypoint"):
            if isinstance(record.get(key), str):
                metadata[key] = record[key]
        if isinstance(record.get("uuid"), str):
            link = {
                key: record[key]
                for key in ("uuid", "parentUuid", "logicalParentUuid")
                if key in record
                and (record[key] is None or isinstance(record[key], str))
            }
            output.links.append({**link, "src": src})
            if "parentUuid" in link:
                metadata["leaf_uuid"] = record["uuid"]
        else:
            link = {}
        kind = record.get("type")
        if kind == "summary" and isinstance(record.get("summary"), str):
            metadata["summary"] = record["summary"]
            return output
        for title_kind, key in (
            ("custom-title", "customTitle"),
            ("ai-title", "aiTitle"),
        ):
            if kind == title_kind and isinstance(record.get(key), str):
                metadata[key] = record[key]
        name = str(kind)
        if kind == "system":
            name = f"system/{record.get('subtype')}"
        if name in BOOKKEEPING or name.startswith(BOOKKEEPING_PREFIXES):
            return output
        stamp = record.get("timestamp")
        time = stamp if isinstance(stamp, str) and stamp else None
        time = time or state.get("time") or context.now
        state["time"] = time
        if kind == "user":
            self._user(record, time, src, state, context, output)
        elif kind == "assistant":
            self._agent(record, time, src, state, output)
        elif kind == "attachment":
            self._attachment(record, time, src, state, output)
        elif name == "system/compact_boundary":
            metadata = record.get("compactMetadata")
            metadata = metadata if isinstance(metadata, dict) else {}
            output.rows.append(
                CompactionRow(
                    time,
                    src,
                    trigger=_text(metadata.get("trigger")),
                    tokens_before=_number(metadata.get("preTokens")),
                )
            )
        elif name == "system/stop_hook_summary":
            output.rows.append(self._stop_hooks(record, time, src))
        else:
            output.unknown = name
        if output.rows and not state.get("session"):
            state["session"] = True
            message = record.get("message")
            model = (
                message.get("model")
                if kind == "assistant" and isinstance(message, dict)
                else None
            )
            output.rows.insert(
                0,
                SessionRow(
                    time,
                    src,
                    vendor=self.vendor,
                    id=context.session,
                    source={
                        "home": str(context.home),
                        "path": str(context.path.relative_to(context.home)),
                    },
                    account=context.account,
                    cwd=_text(record.get("cwd")),
                    model=model
                    if isinstance(model, str) and model != SYNTHETIC
                    else None,
                    effort=None,
                    parent=context.parent,
                ),
            )
        output.rows = [
            replace(row, native=link) if row.TYPE != "session" else row
            for row in output.rows
        ]
        if kind == "user":
            for row in output.rows:
                if isinstance(row, MessageRow) and row.text:
                    metadata.setdefault("first_prompt", row.text[:200])
        return output

    def _user(
        self,
        record: dict[str, Any],
        time: str,
        src: int,
        state: dict[str, Any],
        context: Context,
        output: Output,
    ) -> None:
        state["end"] = False
        state["turn_at"] = src
        message = record.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if record.get("isCompactSummary") or record.get("isMeta"):
            kind = "compact_summary" if record.get("isCompactSummary") else "meta"
            output.rows.append(
                MessageRow(
                    time,
                    src,
                    role="owner",
                    injected={"kind": kind, "bytes": _content_size(content)},
                )
            )
            return
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        if not isinstance(content, list):
            output.unknown = "user/content"
            return
        blocks = [block for block in content if isinstance(block, dict)]
        results = [block for block in blocks if block.get("type") == "tool_result"]
        rest = [block for block in blocks if block.get("type") != "tool_result"]
        if results:
            appended = Injected()
            leftover: list[dict[str, Any]] = []
            for block in rest:
                words = block.get("text")
                if (
                    block.get("type") == "text"
                    and isinstance(words, str)
                    and _whole(words)
                ):
                    appended.add("reminder", _size(words))
                    continue
                leftover.append(block)
            for at, block in enumerate(results):
                last = at == len(results) - 1
                self._result(
                    block,
                    record,
                    time,
                    src,
                    state,
                    context,
                    output,
                    appended if last else None,
                )
            if leftover:
                self._owner(leftover, time, src, output)
            return
        self._owner(rest, time, src, output)

    def _owner(
        self, blocks: list[dict[str, Any]], time: str, src: int, output: Output
    ) -> None:
        injected = Injected()
        texts: list[str] = []
        media: list[dict[str, Any]] = []
        for block in blocks:
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                kept = _owner_text(block["text"], injected)
                if kept:
                    texts.append(kept)
            elif block.get("type") in ("image", "document"):
                entry = _media(block, output.media)
                if entry is not None:
                    media.append(entry)
        text = "\n".join(texts) if texts else None
        if text is None and not media and injected.shown() is None:
            return
        output.rows.append(
            MessageRow(
                time,
                src,
                role="owner",
                text=text,
                injected=injected.shown(),
                media=media or None,
            )
        )

    def _result(
        self,
        block: dict[str, Any],
        record: dict[str, Any],
        time: str,
        src: int,
        state: dict[str, Any],
        context: Context,
        output: Output,
        appended: Injected | None,
    ) -> None:
        call = str(block.get("tool_use_id") or "")
        injected = appended or Injected()
        media: list[dict[str, Any]] = []
        content = block.get("content")
        parts: list[str] = []
        if isinstance(content, str):
            parts.append(_appended(content, injected))
        elif isinstance(content, list):
            for item in content:
                if not isinstance(item, dict):
                    continue
                words = item.get("text")
                if item.get("type") == "text" and isinstance(words, str):
                    if words.lstrip().startswith(REMINDER_OPEN) and _whole(words):
                        injected.add("reminder", _size(words))
                    else:
                        parts.append(words)
                elif item.get("type") in ("image", "document"):
                    entry = _media(item, output.media)
                    if entry is not None:
                        media.append(entry)
        text = "\n".join(parts)
        missing: bool | None = None
        # A caller that keeps no row (the metadata pass) discards this row:
        # its text keeps the spill reference.
        reference = SPILL.search(text) if context.rows else None
        if reference is not None:
            spilled = self._spill(reference.group(1), context)
            if spilled is None:
                missing = True
            else:
                text = spilled
        output.rows.append(
            ToolResultRow(
                time,
                src,
                id=call,
                ok=block.get("is_error") is not True,
                output=text,
                media=media or None,
                injected=injected.shown(),
                spill_missing=missing,
            )
        )
        calls = state.setdefault("calls", {})
        tool = calls.pop(call, None)
        child = self._child(tool, record.get("toolUseResult"), text, context)
        if child is not None:
            output.rows.append(ChildRow(time, src, id=child[0], kind=child[1]))

    @staticmethod
    def _child(
        tool: Any, result: Any, text: str, context: Context
    ) -> tuple[str, str] | None:
        if tool in SUBAGENT_TOOLS and isinstance(result, dict):
            agent = result.get("agentId")
            if isinstance(agent, str) and agent:
                return (
                    f"{context.parent or context.vendor_id or context.session}.{agent}",
                    "subagent",
                )
        if isinstance(tool, str) and tool.endswith(RUN_START):
            found = result.get("run_id") if isinstance(result, dict) else None
            if not isinstance(found, str):
                try:
                    parsed = json.loads(text)
                except ValueError, RecursionError:
                    parsed = None
                found = parsed.get("run_id") if isinstance(parsed, dict) else None
            if isinstance(found, str) and found:
                return found, "nspawn_run"
        return None

    @staticmethod
    def _spill(reference: str, context: Context) -> str | None:
        """The text of Claude Code's spill file, read only from this session's
        own `tool-results/` directory, or None."""
        reference_path = Path(reference)
        name = reference_path.name
        session_dir = session_folder(context.home, context.path)
        if (
            session_dir is None
            or not SPILL_NAME.fullmatch(name)
            or reference_path.parent.name != "tool-results"
            or reference_path.parent.parent.name != session_dir.name
        ):
            return None
        try:
            descriptor = open_owner_path(
                context.home, session_dir / "tool-results" / name
            )
        except ValueError:
            return None
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_size > SPILL_MAX_BYTES:
                return None
            chunks: list[bytes] = []
            while chunk := os.read(descriptor, 1 << 20):
                chunks.append(chunk)
        except OSError:
            return None
        finally:
            os.close(descriptor)
        return b"".join(chunks).decode("utf-8", errors="replace")

    def _agent(
        self,
        record: dict[str, Any],
        time: str,
        src: int,
        state: dict[str, Any],
        output: Output,
    ) -> None:
        message = record.get("message")
        if not isinstance(message, dict):
            output.unknown = "assistant/message"
            return
        mid = _text(message.get("id")) or _text(record.get("uuid")) or ""
        model = _text(message.get("model"))
        if model is not None and model != SYNTHETIC:
            if state.get("model") not in (None, model):
                output.rows.append(ConfigRow(time, src, model=model, effort=None))
            state["model"] = model
        used = message.get("usage")
        if mid and isinstance(used, dict):
            output.usage.append(
                Usage(
                    mid=mid,
                    model=model,
                    input=_number(used.get("input_tokens")) or 0,
                    cache_read=_number(used.get("cache_read_input_tokens")) or 0,
                    cache_write=_number(used.get("cache_creation_input_tokens")) or 0,
                    output=_number(used.get("output_tokens")) or 0,
                    reasoning=None,
                    src=src,
                )
            )
        state["end"] = message.get("stop_reason") == "end_turn"
        state["turn_at"] = src
        content = message.get("content")
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            row: Row | None = None
            if kind == "text" and isinstance(block.get("text"), str):
                row = MessageRow(time, src, role="agent", text=block["text"], mid=mid)
            elif kind == "thinking" and isinstance(block.get("thinking"), str):
                row = ReasoningRow(time, src, mid=mid, text=block["thinking"])
            elif kind == "redacted_thinking":
                row = ReasoningRow(
                    time, src, mid=mid, redacted=_size(str(block.get("data") or ""))
                )
            elif kind == "tool_use":
                call, tool = str(block.get("id") or ""), str(block.get("name") or "")
                state.setdefault("calls", {})[call] = tool
                row = ToolCallRow(
                    time, src, mid=mid, id=call, name=tool, input=block.get("input")
                )
            else:
                output.unknown = f"assistant/{kind}"
            if row is not None:
                output.rows.append(row)

    def _attachment(
        self,
        record: dict[str, Any],
        time: str,
        src: int,
        state: dict[str, Any],
        output: Output,
    ) -> None:
        body = record.get("attachment")
        if not isinstance(body, dict):
            output.unknown = "attachment"
            return
        kind = str(body.get("type"))
        if kind in INJECTING:
            size = _body_size(
                body["content"]
                if "content" in body
                else {key: value for key, value in body.items() if key != "type"}
            )
            output.rows.append(
                MessageRow(
                    time, src, role="owner", injected={"kind": kind, "bytes": size}
                )
            )
        elif kind == "queued_command":
            state["end"] = False
            state["turn_at"] = src
            prompt = body.get("prompt")
            if isinstance(prompt, str):
                self._owner([{"type": "text", "text": prompt}], time, src, output)
        elif kind in HOOKS:
            output.rows.append(self._hook(kind, body, time, src))
        else:
            output.unknown = f"attachment/{kind}"

    @staticmethod
    def _hook(kind: str, body: dict[str, Any], time: str, src: int) -> HookRow:
        outcome = HOOKS[kind]
        # Claude Code records a hook that ran past its timeout as a cancelled hook with timedOut.
        if kind == "hook_cancelled" and body.get("timedOut") is True:
            outcome = "timeout"
        code = _number(body.get("exitCode"))
        if kind == "async_hook_response" and code not in (None, 0):
            outcome = "error"
        text: str | None = None
        if outcome == "blocked":
            blocking = body.get("blockingError")
            if isinstance(blocking, dict):
                blocking = blocking.get("blockingError")
            text = _text(blocking) or _text(body.get("content"))
        elif outcome == "error":
            text = (
                _text(body.get("stderr"))
                or _text(body.get("stdout"))
                or _text(body.get("content"))
            )
        return HookRow(
            time,
            src,
            event=_text(body.get("hookEvent")),
            name=_text(body.get("hookName")),
            tool=_text(body.get("toolName")) or _text(body.get("toolUseID")),
            outcome=outcome,
            exit_code=code,
            duration_ms=_number(body.get("durationMs")),
            text=text,
        )

    @staticmethod
    def _stop_hooks(record: dict[str, Any], time: str, src: int) -> HookRow:
        infos = record.get("hookInfos")
        infos = (
            [item for item in infos if isinstance(item, dict)]
            if isinstance(infos, list)
            else []
        )
        commands = [
            item["command"] for item in infos if isinstance(item.get("command"), str)
        ]
        durations = [_number(item.get("durationMs")) for item in infos]
        known = [value for value in durations if value is not None]
        errors = record.get("hookErrors")
        errors = [str(item) for item in errors] if isinstance(errors, list) else []
        if record.get("preventedContinuation") is True:
            outcome, text = "blocked", _text(record.get("stopReason")) or None
        elif errors:
            outcome, text = "error", "\n".join(errors)
        else:
            outcome, text = "ok", None
        return HookRow(
            time,
            src,
            event=_text(record.get("hookEvent")) or "Stop",
            name="; ".join(commands) or None,
            tool=_text(record.get("toolUseID")),
            outcome=outcome,
            duration_ms=sum(known) if known else None,
            text=text,
        )


def _content_size(content: Any) -> int:
    if isinstance(content, str):
        return _size(content)
    if isinstance(content, list):
        return sum(
            _size(block["text"])
            for block in content
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        )
    return 0


TRANSLATOR: Final[ClaudeTranslator] = ClaudeTranslator()
