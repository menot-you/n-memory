"""The session journal (cut 1): the Claude translator, the daemon's cursor and
writer, and the `nspawn session list|show` reader.

Every fixture is an invented line in a Claude Code shape, with invented ids and
texts. Each test has its own Claude home, `~/.nott` and registry file (the
`nott-database` pytest plugin); no test reads the owner's homes or stores.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import threading
import time
from calendar import timegm
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import jsonschema  # type: ignore[import-untyped]
import nott_database
import pytest
from registry_reset import store

from nspawn import cli
from nspawn.journal import FORMAT, TRANSLATORS, serve, show
from nspawn.owner_roster import Roster
from nspawn.registry import Registry, store_path
from nspawn.schema import REGISTRY_SCHEMA_VERSION

FIXTURES = Path(__file__).parent / "fixtures" / "journal"
SCHEMA = json.loads(
    (
        Path(__file__).parents[1]
        / "nspawn"
        / "schema"
        / "session-journal.v1.schema.json"
    ).read_text()
)
SID = "5f0c0a2e-1111-4222-8333-444455556666"
OTHER = "9a8b7c6d-0000-4111-8222-333344445555"
PROJECT = "-work-demo"
NOW = timegm((2026, 10, 22, 12, 0, 0, 0, 0, 0))
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGD4DwABBAEAX+XDSwAAAABJRU5ErkJggg=="
)


def validate(row: dict[str, Any]) -> None:
    jsonschema.Draft202012Validator(SCHEMA).validate(row)


class World:
    """A temp HOME with one Claude home, `~/.nott` and this test's store."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.home = root / "home"
        self.claude = self.home / ".claude"
        self.projects = self.claude / "projects" / PROJECT
        self.projects.mkdir(parents=True, mode=0o700)
        self.nott = self.home / ".nott"
        self.nott.mkdir(mode=0o700)
        self.store = store()
        self.roster: Roster | None = None
        self.clock = float(NOW)

    def settings(self, **extra: Any) -> serve.Settings:
        options: dict[str, Any] = {
            "homes": [(None, self.claude)],
            "nott_root": self.nott,
            "store": self.store,
            "roster": self.roster,
            "clock": lambda: self.clock,
        }
        options.update(extra)
        return serve.Settings(**options)

    def daemon(self, **extra: Any) -> serve.Daemon:
        daemon = serve.Daemon(self.settings(**extra))
        daemon.start()
        return daemon

    def source(self, sid: str = SID) -> Path:
        return self.projects / f"{sid}.jsonl"

    def write(self, lines: list[dict[str, Any]] | str, sid: str = SID) -> Path:
        path = self.source(sid)
        text = (
            lines
            if isinstance(lines, str)
            else "".join(json.dumps(line) + "\n" for line in lines)
        )
        with path.open("a") as stream:
            stream.write(text)
        return path

    def rows(self, sid: str = SID) -> list[dict[str, Any]]:
        lines = show.show(self.store, "claude", sid)
        return [json.loads(line) for line in lines[:-1]]

    def total(self, sid: str = SID) -> dict[str, Any]:
        last: dict[str, Any] = json.loads(show.show(self.store, "claude", sid)[-1])
        return last["total"]

    def table(self, sql: str, params: tuple[Any, ...] = ()) -> list[Any]:
        connection = nott_database.connect(self.store, readonly=True)
        try:
            return nott_database.query(connection, sql, params)
        finally:
            connection.close()


def stored_bytes(world: World) -> bytes:
    """Every byte of the store: the file and its write-ahead log."""
    return b"".join(
        path.read_bytes() for path in world.store.parent.iterdir() if path.is_file()
    )


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def base(n: int, sid: str = SID) -> dict[str, Any]:
    return {
        "parentUuid": None,
        "isSidechain": False,
        "userType": "external",
        "cwd": "/work/demo",
        "sessionId": sid,
        "version": "2.1.300",
        "uuid": f"x{n}",
        "timestamp": f"2026-10-22T10:{n // 60:02d}:{n % 60:02d}.000Z",
    }


def user(n: int, content: Any, **extra: Any) -> dict[str, Any]:
    return {
        **base(n),
        "type": "user",
        "message": {"role": "user", "content": content},
        **extra,
    }


def usage(output: int, read: int = 0, write: int = 0, fresh: int = 1) -> dict[str, int]:
    return {
        "input_tokens": fresh,
        "cache_read_input_tokens": read,
        "cache_creation_input_tokens": write,
        "output_tokens": output,
    }


def agent(
    n: int, mid: str, block: dict[str, Any], used: dict[str, int], **extra: Any
) -> dict[str, Any]:
    return {
        **base(n),
        "type": "assistant",
        "message": {
            "id": mid,
            "role": "assistant",
            "model": "claude-opus-5-5",
            "content": [block],
            "usage": used,
            **extra,
        },
    }


def text(words: str) -> dict[str, str]:
    return {"type": "text", "text": words}


def attachment(n: int, body: dict[str, Any]) -> dict[str, Any]:
    return {**base(n), "type": "attachment", "attachment": body}


def result(n: int, call: str, content: Any, **extra: Any) -> dict[str, Any]:
    return user(
        n, [{"type": "tool_result", "tool_use_id": call, "content": content, **extra}]
    )


def drain(daemon: serve.Daemon) -> None:
    """Finish the fixture import and refuse a writer that makes no progress."""
    daemon.cycle(cold=True)
    while any(
        source.new or source.behind or source.metadata_version < 1
        for source in daemon.sources.values()
    ):
        before = [
            (
                key,
                source.consumed,
                source.state,
                source.metadata_version,
                source.new,
                source.behind,
            )
            for key, source in daemon.sources.items()
        ]
        daemon.cycle(cold=True)
        after = [
            (
                key,
                source.consumed,
                source.state,
                source.metadata_version,
                source.new,
                source.behind,
            )
            for key, source in daemon.sources.items()
        ]
        assert after != before, "The fixture writer made no progress."


def run(world: World, **extra: Any) -> serve.Daemon:
    daemon = world.daemon(**extra)
    drain(daemon)
    return daemon


def translated(world: World, lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    daemon = world.daemon()
    world.write(lines)
    daemon.cycle(cold=True)
    daemon.close()
    rows = world.rows()
    for row in rows:
        validate(row)
    return rows


# -- 1. golden ----------------------------------------------------------------


def test_1_golden_translation_is_byte_exact_and_valid(world: World) -> None:
    daemon = world.daemon()
    shutil.copyfile(FIXTURES / "claude-session.jsonl", world.source())
    os.utime(world.source(), (NOW, NOW))
    daemon.cycle(cold=True)
    daemon.close()
    printed = "\n".join(show.show(world.store, "claude", SID)) + "\n"
    expected = (
        (FIXTURES / "claude-session.expected.jsonl")
        .read_text()
        .replace("@HOME@", str(world.claude))
    )
    lines = printed.splitlines()
    footer = json.loads(lines[-1])
    activity = footer.pop("native_activity")
    assert activity["mtime_ns"] == NOW * 1_000_000_000
    assert activity["size"] == world.source().stat().st_size
    assert "\n".join([*lines[:-1], show._dumps(footer)]) + "\n" == expected
    for line in lines[:-1]:
        validate(json.loads(line))
    assert FORMAT == 1
    assert [translator.vendor for translator in TRANSLATORS] == ["claude"]


# -- 2, 3, 11. usage ------------------------------------------------------------


def test_2_usage_keeps_the_last_line_of_each_message_id(world: World) -> None:
    translated(
        world,
        [
            agent(1, "msg_1", {"type": "thinking", "thinking": "a"}, usage(1, 10, 5)),
            agent(2, "msg_1", text("b"), usage(2, 10, 5)),
            agent(3, "msg_1", text("c"), usage(9, 11, 6, 2)),
            agent(4, "msg_2", text("d"), usage(3, 20)),
            agent(5, "msg_2", text("e"), usage(4, 21, 1, 3)),
        ],
    )
    found = world.table(
        "SELECT mid, input, cache_read, cache_write, output, reasoning "
        "FROM journal_usage ORDER BY mid"
    )
    assert [tuple(row) for row in found] == [
        ("msg_1", 2, 11, 6, 9, None),
        ("msg_2", 3, 21, 1, 4, None),
    ]
    assert world.total() == {
        "input": 5,
        "cache_read": 32,
        "cache_write": 7,
        "output": 13,
        "reasoning": None,
        "calls": 2,
        "models": ["claude-opus-5-5"],
    }


def test_3_interleaved_results_last_answer_and_subagents_count(world: World) -> None:
    call = {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {}}
    translated(
        world,
        [
            user(1, "go"),
            agent(2, "msg_1", call, usage(5, 100)),
            result(3, "toolu_1", "ok"),
            agent(4, "msg_1", text("after the result"), usage(8, 100)),
            agent(
                5, "msg_2", text("last answer"), usage(6, 50), stop_reason="end_turn"
            ),
        ],
    )
    assert world.total()["output"] == 14
    assert world.total()["calls"] == 2
    daemon = world.daemon()
    sub = world.projects / SID / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-a1.jsonl").write_text(
        json.dumps({**agent(6, "msg_s", text("x"), usage(7)), "isSidechain": True})
        + "\n"
    )
    daemon.cycle(cold=True)
    daemon.close()
    assert world.total(f"{SID}.a1")["output"] == 7
    assert world.total()["output"] == 14


def test_11_a_stop_between_two_blocks_keeps_the_last_usage(world: World) -> None:
    lines = [
        agent(1, "msg_1", text("a"), usage(1)),
        agent(2, "msg_1", text("b"), usage(9)),
    ]
    daemon = world.daemon(batch_lines=1)
    world.write(lines)
    daemon.cycle(cold=True, batches=1)
    daemon.close()
    assert [tuple(r) for r in world.table("SELECT mid, output FROM journal_usage")] == [
        ("msg_1", 1)
    ]
    run(world, batch_lines=1).close()
    assert [tuple(r) for r in world.table("SELECT mid, output FROM journal_usage")] == [
        ("msg_1", 9)
    ]


# -- 4. bookkeeping and unknown types -----------------------------------------------


BOOKKEEPING = [
    {"type": "queue-operation", "operation": "enqueue"},
    {"type": "api-request-start"},
    {"type": "mode", "mode": "plan"},
    {"type": "permission-mode", "permissionMode": "default"},
    {"type": "atis-latch"},
    {"type": "ai-title", "aiTitle": "t"},
    {"type": "custom-title", "customTitle": "t"},
    {"type": "agent-name", "agentName": "n"},
    {"type": "agent-color", "agentColor": "red"},
    {"type": "frame-link"},
    {"type": "pr-link", "url": "https://example.invalid/1"},
    {"type": "cost-state"},
    {"type": "last-prompt", "lastPrompt": "x"},
    {"type": "file-history-snapshot", "snapshot": {}},
    {"type": "output_style"},
    {**base(1), "type": "system", "subtype": "turn_duration", "durationMs": 1},
    {**base(2), "type": "system", "subtype": "informational", "content": "i"},
]


def test_4_bookkeeping_and_unknown_types_make_no_row(world: World) -> None:
    daemon = world.daemon()
    world.write([user(0, "hello"), *BOOKKEEPING, {"type": "brand-new-thing"}])
    daemon.cycle(cold=True)
    assert daemon.unknown == {"brand-new-thing": 1}
    daemon.close()
    assert [row["type"] for row in world.rows()] == ["session", "message", "status"]


# -- 5. injected context ----------------------------------------------------------


def test_5_injected_bodies_are_stripped_and_sized(world: World) -> None:
    reminder = "<system-reminder>\nSecret plan text.\n</system-reminder>"
    hook = "<user-prompt-submit-hook>Hook body.</user-prompt-submit-hook>"
    appended = "\n\n<system-reminder>\nMalware note.\n</system-reminder>\n"
    rows = translated(
        world,
        [
            user(1, f"{reminder}\nTyped words."),
            user(2, [text(hook), text("Second typed.")]),
            attachment(
                3, {"type": "task_reminder", "content": [{"id": "1"}], "itemCount": 1}
            ),
            attachment(4, {"type": "total_tokens_reminder", "content": "50% used"}),
            agent(
                5,
                "msg_1",
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "Bash",
                    "input": {
                        "command": "echo '<system-reminder>in input</system-reminder>'"
                    },
                },
                usage(1),
            ),
            result(6, "toolu_1", "exact output" + appended),
            agent(
                7,
                "msg_2",
                text("Agent says <system-reminder>stays</system-reminder>."),
                usage(1),
            ),
        ],
    )
    stored = json.dumps(rows)
    for body in ("Secret plan text.", "Hook body.", "Malware note.", "50% used"):
        assert body not in stored
    owner = [row for row in rows if row.get("role") == "owner"]
    assert owner[0]["text"] == "Typed words."
    assert owner[0]["injected"] == {"kind": "reminder", "bytes": len(reminder) + 1}
    assert owner[1]["text"] == "Second typed."
    assert owner[1]["injected"] == {"kind": "hook", "bytes": len(hook)}
    assert owner[2] == {
        **owner[2],
        "injected": {"kind": "task_reminder", "bytes": len('[{"id":"1"}]')},
    }
    assert "text" not in owner[2]
    assert owner[3]["injected"] == {
        "kind": "total_tokens_reminder",
        "bytes": len("50% used"),
    }
    output = next(row for row in rows if row["type"] == "tool_result")
    assert output["output"] == "exact output"
    assert output["injected"] == {"kind": "reminder", "bytes": len(appended)}
    call = next(row for row in rows if row["type"] == "tool_call")
    assert "<system-reminder>in input</system-reminder>" in call["input"]["command"]
    said = [row for row in rows if row.get("role") == "agent"]
    assert said[0]["text"] == "Agent says <system-reminder>stays</system-reminder>."


# -- 6. media -----------------------------------------------------------------------


def image(data: bytes, mime: str = "image/png") -> dict[str, Any]:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": mime,
            "data": base64.b64encode(data).decode(),
        },
    }


def test_6_images_become_files_once_and_no_base64_is_stored(world: World) -> None:
    other = b"not really a bmp, but bytes"
    rows = translated(
        world,
        [
            user(1, [text("look"), image(PNG)]),
            result(2, "toolu_1", [text("shot"), image(PNG)]),
            user(3, [image(other, "image/bmp")]),
        ],
    )
    digest = hashlib.sha256(PNG).hexdigest()
    media = world.nott / "sessions" / "media"
    assert sorted(path.name for path in media.iterdir()) == [f"{digest}.png"]
    assert (media / f"{digest}.png").read_bytes() == PNG
    owner = rows[1]
    assert owner["text"] == "look"
    assert owner["media"] == [
        {
            "kind": "image",
            "mime": "image/png",
            "bytes": len(PNG),
            "sha256": digest,
            "file": f"{digest}.png",
        }
    ]
    tool = next(row for row in rows if row["type"] == "tool_result")
    assert tool["media"][0]["file"] == f"{digest}.png" and tool["output"] == "shot"
    marker = rows[3]["media"][0]
    assert marker["mime"] == "image/bmp" and marker["bytes"] == len(other)
    assert marker["file"] is None
    blob = stored_bytes(world)
    assert base64.b64encode(PNG) not in blob
    assert base64.b64encode(other) not in blob


# -- 6b. hooks ------------------------------------------------------------------------


def test_6b_each_hook_form_gives_one_row_with_its_outcome(world: World) -> None:
    common = {
        "hookName": "PreToolUse:Bash",
        "hookEvent": "PreToolUse",
        "toolUseID": "toolu_9",
    }
    rows = translated(
        world,
        [
            attachment(
                1,
                {
                    "type": "hook_success",
                    **common,
                    "stdout": "fine",
                    "exitCode": 0,
                    "durationMs": 5,
                },
            ),
            attachment(
                2,
                {
                    "type": "async_hook_response",
                    **common,
                    "stdout": "async",
                    "exitCode": 0,
                },
            ),
            attachment(
                3,
                {
                    "type": "hook_non_blocking_error",
                    **common,
                    "stderr": "it broke",
                    "exitCode": 1,
                },
            ),
            attachment(
                4,
                {
                    "type": "hook_blocking_error",
                    **common,
                    "blockingError": {"blockingError": "not allowed", "command": "x"},
                },
            ),
            attachment(5, {"type": "hook_cancelled", **common}),
            attachment(
                6,
                {
                    "type": "hook_cancelled",
                    **common,
                    "timedOut": True,
                    "timeoutMs": 60000,
                },
            ),
            {
                **base(7),
                "type": "system",
                "subtype": "stop_hook_summary",
                "hookCount": 1,
                "hookInfos": [{"command": "stop.sh", "durationMs": 3}],
                "hookErrors": ["stop.sh failed"],
                "preventedContinuation": False,
                "stopReason": "",
            },
            {
                **base(8),
                "type": "system",
                "subtype": "stop_hook_summary",
                "hookCount": 1,
                "hookInfos": [{"command": "gate.sh"}],
                "hookErrors": [],
                "preventedContinuation": True,
                "stopReason": "tests are red",
            },
        ],
    )
    hooks = [row for row in rows if row["type"] == "hook"]
    assert [row["outcome"] for row in hooks] == [
        "ok",
        "ok",
        "error",
        "blocked",
        "cancelled",
        "timeout",
        "error",
        "blocked",
    ]
    assert "text" not in hooks[0] and "text" not in hooks[1]
    assert hooks[0]["event"] == "PreToolUse" and hooks[0]["name"] == "PreToolUse:Bash"
    assert hooks[0]["tool"] == "toolu_9" and hooks[0]["exit_code"] == 0
    assert hooks[2]["text"] == "it broke"
    assert hooks[3]["text"] == "not allowed"
    assert hooks[6]["text"] == "stop.sh failed" and hooks[6]["event"] == "Stop"
    assert hooks[7]["text"] == "tests are red"
    assert "fine" not in json.dumps(rows) and "async" not in json.dumps(rows)


# -- 7. spill -----------------------------------------------------------------------


def test_7_spill_is_inline_and_every_other_reference_is_missing(world: World) -> None:
    results = world.projects / SID / "tool-results"
    results.mkdir(parents=True, mode=0o700)
    (results / "toolu_big.txt").write_text("the full output\n")
    (results / "gone.txt").write_text("deleted below")
    outside = world.root / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("never read")
    (results / "secret.txt").symlink_to(outside / "secret.txt")
    note = "Output too large. Full output saved to: {}\n\nPreview: the full"
    (results / "gone.txt").unlink()
    rows = translated(
        world,
        [
            result(1, "toolu_a", note.format(results / "toolu_big.txt")),
            result(2, "toolu_b", note.format(results / "gone.txt")),
            result(3, "toolu_c", note.format(outside / "secret.txt")),
            result(4, "toolu_d", note.format(f"{results}/sub/../secret.txt")),
            result(5, "toolu_e", note.format(results / "secret.txt")),
        ],
    )
    outputs = [row for row in rows if row["type"] == "tool_result"]
    assert outputs[0]["output"] == "the full output\n"
    assert "spill_missing" not in outputs[0]
    for row in outputs[1:]:
        assert row["spill_missing"] is True
    assert "never read" not in json.dumps(rows)


@pytest.mark.parametrize("parent", [SID, "P.jsonl"])
@pytest.mark.parametrize("folder", ["subagents", "subagents/workflows/wf_x"])
def test_7_a_subagent_spill_is_read_from_its_parent_folder(
    world: World, folder: str, parent: str
) -> None:
    """Review r1 of PR 1931, R2: Claude Code writes the spill of an Agent-tool
    subagent and of a Workflow agent into `<parent>/tool-results/`. The journal
    reads it there for both source shapes. Review r2, F3: a parent folder whose
    name ends in `.jsonl` keeps its full name."""
    results = world.projects / parent / "tool-results"
    results.mkdir(parents=True, mode=0o700)
    (results / "toolu_big.txt").write_text("the full output\n")
    world.write([user(1, "parent prompt")], sid=parent)
    agents = world.projects / parent / folder
    agents.mkdir(parents=True, exist_ok=True, mode=0o700)
    note = "Output too large. Full output saved to: {}\n\nPreview: the full"
    (agents / "agent-a1.jsonl").write_text(
        json.dumps(
            {
                **result(2, "toolu_a", note.format(results / "toolu_big.txt")),
                "isSidechain": True,
                "agentId": "a1",
            }
        )
        + "\n"
    )
    daemon = run(world)
    daemon.close()
    outputs = [
        row for row in world.rows(f"{parent}.a1") if row["type"] == "tool_result"
    ]
    assert len(outputs) == 1
    assert outputs[0]["output"] == "the full output\n"
    assert "spill_missing" not in outputs[0]


def test_7_the_file_outside_is_never_opened(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = world.root / "outside"
    outside.mkdir()
    (outside / "x.txt").write_text("never read")
    opened: list[str] = []
    real = os.open

    def spy(path: Any, *args: Any, **kwargs: Any) -> int:
        opened.append(str(path))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", spy)
    translated(
        world, [result(1, "toolu_a", f"Full output saved to: {outside / 'x.txt'}")]
    )
    assert not any(str(outside) in path for path in opened)


# -- 8. partial lines -------------------------------------------------------------------


def test_8_a_partial_line_waits_for_its_newline(world: World) -> None:
    daemon = world.daemon()
    line = json.dumps(user(1, "hello"))
    world.write(line[:20])
    daemon.cycle(cold=True)
    with pytest.raises(show.Missing):
        world.rows()
    world.write(line[20:] + "\n")
    daemon.cycle(cold=True)
    daemon.cycle(cold=True)
    daemon.close()
    assert [row["type"] for row in world.rows()] == ["session", "message", "status"]


def _source_preads(
    monkeypatch: pytest.MonkeyPatch, path: Path
) -> list[tuple[int, int]]:
    """Each pread on the file at `path` from now on, as (offset, length)."""
    seen: list[tuple[int, int]] = []
    original = os.pread

    def audit(fd: int, count: int, offset: int) -> bytes:
        data = original(fd, count, offset)
        if os.fstat(fd).st_ino == path.stat().st_ino:
            seen.append((offset, len(data)))
        return data

    monkeypatch.setattr(os, "pread", audit)
    return seen


@pytest.mark.parametrize("case", ["after-a-record", "first-record"])
def test_8_an_unchanged_unfinished_record_is_read_once(
    world: World, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """The daemon does not read an unchanged unfinished record again."""
    (world.claude / "sessions").mkdir()
    daemon = world.daemon()
    line = json.dumps(user(2, "unfinished"))
    whole = json.dumps(user(1, "whole")) + "\n" if case == "after-a-record" else ""
    path = world.write(whole + line[:20])
    try:
        daemon.cycle(cold=True)
        statuses = world.table("SELECT count(*) FROM journal_row WHERE type = 'status'")
        seen = _source_preads(monkeypatch, path)
        for _ in range(3):
            daemon.cycle(cold=True)
        assert seen == []
        assert (
            world.table("SELECT count(*) FROM journal_row WHERE type = 'status'")
            == statuses
        )
        world.write(line[20:] + "\n")
        daemon.cycle(cold=True)
        assert seen == [(len(whole), len(line) + 1)]
        assert any(row.get("text") == "unfinished" for row in world.rows())
    finally:
        daemon.close()


@pytest.mark.parametrize("change", ["grow", "rewrite"])
def test_8_a_changed_unfinished_record_is_read_again(
    world: World, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    """Guard: a growth or a rewrite of an unfinished record reads it again."""
    daemon = world.daemon()
    line = json.dumps(user(2, "unfinished"))
    path = world.write(json.dumps(user(1, "whole")) + "\n" + line[:20])
    try:
        daemon.cycle(cold=True)
        seen = _source_preads(monkeypatch, path)
        if change == "grow":
            world.write(line[20:30])
        else:
            info = path.stat()
            path.write_bytes(path.read_bytes())
            os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000_000))
        daemon.cycle(cold=True)
        assert len(seen) == 1
        native = json.loads(world.table("SELECT activity FROM journal_source")[0][0])
        if change == "grow":
            assert "reason" not in native and "rebuild" not in native
        else:
            assert native["reason"] == "transcript changed without growth"
            assert native["rebuild"] is True
    finally:
        daemon.close()


def test_the_minute_line_counts_each_byte_read(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each pread starts at the end of the bytes before it, and the minute
    line counts each byte that a pread returns."""
    daemon = world.daemon(read_bytes=16)
    path = world.source()
    seen = _source_preads(monkeypatch, path)
    world.write([user(1, "x" * 100)])
    try:
        daemon.cycle(cold=True)
        line = daemon.report()
    finally:
        daemon.close()
    at = 0
    for offset, length in seen:
        assert offset == at, seen
        at += length
    assert at == path.stat().st_size > 64
    assert f"bytes read {at}," in line


# -- 9, 10, 11b. cursor ------------------------------------------------------------------


def golden_rows(world: World) -> list[str]:
    return show.show(world.store, "claude", SID)


def test_9_a_restart_after_each_batch_equals_one_pass(tmp_path: Path) -> None:
    single = World(tmp_path / "one")
    daemon = single.daemon()
    shutil.copyfile(FIXTURES / "claude-session.jsonl", single.source())
    os.utime(single.source(), (NOW, NOW))
    daemon.cycle(cold=True)
    daemon.close()
    expected = golden_rows(single)

    os.environ["NOTT_DATABASE_DIR"] = str(tmp_path / "second-store")
    many = World(tmp_path / "many")
    first = many.daemon(batch_lines=1)
    shutil.copyfile(FIXTURES / "claude-session.jsonl", many.source())
    os.utime(many.source(), (NOW, NOW))
    first.cycle(cold=True, batches=1)
    first.close()
    for _ in range(40):
        again = many.daemon(batch_lines=1)
        again.cycle(cold=True, batches=1)
        again.close()
    final = many.daemon(batch_lines=1)
    final.cycle(cold=True)
    final.close()
    shown = golden_rows(many)
    assert [line.replace(str(many.claude), "H") for line in shown] == [
        line.replace(str(single.claude), "H") for line in expected
    ]


def test_10_a_failed_transaction_leaves_no_row_and_no_offset(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon = world.daemon()
    world.write([user(1, "one"), user(2, "two")])
    real = serve.insert_rows

    def broken(*args: Any, **kwargs: Any) -> None:
        real(*args, **kwargs)
        raise nott_database.OperationalError("injected")

    monkeypatch.setattr(serve, "insert_rows", broken)
    assert daemon.cycle(cold=True) == 0
    assert "the last cycle stopped: OperationalError" in daemon.report()
    assert world.table("SELECT count(*) FROM journal_row")[0][0] == 0
    assert world.table("SELECT count(*) FROM journal_source")[0][0] == 0
    monkeypatch.setattr(serve, "insert_rows", real)
    daemon.cycle(cold=True)
    daemon.cycle(cold=True)
    daemon.close()
    assert [row["type"] for row in world.rows()] == [
        "session",
        "message",
        "message",
        "status",
    ]


def test_11b_a_second_writer_on_the_same_offset_rolls_back(world: World) -> None:
    starter = world.daemon()
    world.write([user(1, "one")])
    starter.cycle(cold=True)
    starter.close()
    first, second = world.daemon(), world.daemon()
    world.write([user(2, "two")])
    first.cycle(cold=True)
    before = world.table("SELECT count(*) FROM journal_row")[0][0]
    second.cycle(cold=True)
    assert world.table("SELECT count(*) FROM journal_row")[0][0] == before
    first.close()
    second.close()
    texts = [row.get("text") for row in world.rows() if row["type"] == "message"]
    assert texts == ["one", "two"]


def test_11c_a_version_7_store_gains_the_tables_and_stays_at_7(world: World) -> None:
    path = store_path()
    connection = nott_database.connect(path)
    with nott_database.write(connection):
        for table in (
            "journal_row",
            "journal_usage",
            "journal_session",
            "journal_source",
        ):
            connection.execute(f"DROP TABLE IF EXISTS {table}")
    connection.close()
    with Registry(path, "older") as registry:
        registry.open(1)
    with Registry(path, "journal-test") as registry:
        registry.upgrade()
        version = nott_database.user_version(registry.connection)
        names = {
            row[0]
            for row in nott_database.query(
                registry.connection, "SELECT name FROM sqlite_schema WHERE type='table'"
            )
        }
        kept = nott_database.query(
            registry.connection, "SELECT instance_id FROM instances"
        )
        schema = nott_database.query(
            registry.connection, "SELECT sql FROM sqlite_schema ORDER BY name"
        )
    assert version == REGISTRY_SCHEMA_VERSION == 7
    assert {
        "journal_source",
        "journal_session",
        "journal_row",
        "journal_usage",
    } <= names
    assert [row[0] for row in kept] == ["older"]
    with Registry(path, "journal-test") as registry:
        registry.upgrade()
        again = nott_database.query(
            registry.connection, "SELECT sql FROM sqlite_schema ORDER BY name"
        )
    assert [tuple(r) for r in again] == [tuple(r) for r in schema]


# -- 12. subagents and identity ---------------------------------------------------------------


def test_12_a_subagent_is_its_own_session(world: World) -> None:
    daemon = world.daemon()
    world.write([user(1, "parent prompt")])
    sub = world.projects / SID / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-a77.jsonl").write_text(
        json.dumps({**user(2, "sub prompt"), "isSidechain": True, "agentId": "a77"})
        + "\n"
    )
    daemon.cycle(cold=True)
    daemon.close()
    child = world.rows(f"{SID}.a77")
    assert child[0]["id"] == f"{SID}.a77" and child[0]["parent"] == SID
    assert (
        child[0]["source"]["path"]
        == f"projects/{PROJECT}/{SID}/subagents/agent-a77.jsonl"
    )
    assert "sub prompt" not in json.dumps(world.rows())


def test_12_a_workflow_agent_is_a_subagent_session(world: World) -> None:
    """The Workflow tool keeps its agents under `subagents/workflows/wf_<id>/`.
    The journal imports each one as `<parent>.<agent>`, the same as an Agent-tool
    subagent: same parent, same parent evidence, same status."""
    world.write([user(1, "parent prompt")])
    sub = world.projects / SID / "subagents"
    flow = sub / "workflows" / "wf_x"
    flow.mkdir(parents=True)
    for folder, name, meta in (
        (sub, "a77", {"agentType": "general-purpose", "finished": True}),
        (flow, "a4b5e111994658fef", {"agentType": "general-purpose"}),
    ):
        (folder / f"agent-{name}.jsonl").write_text(
            json.dumps({**user(2, "sub prompt"), "isSidechain": True, "agentId": name})
            + "\n"
            + json.dumps(
                agent(
                    3, f"m-{name}", text("sub answer"), usage(1), stop_reason="end_turn"
                )
            )
            + "\n"
        )
        (folder / f"agent-{name}.meta.json").write_text(json.dumps(meta))
    daemon = run(world)
    daemon.close()
    for name, path in (
        ("a77", f"projects/{PROJECT}/{SID}/subagents/agent-a77.jsonl"),
        (
            "a4b5e111994658fef",
            f"projects/{PROJECT}/{SID}/subagents/workflows/wf_x/agent-a4b5e111994658fef.jsonl",
        ),
    ):
        shown = [
            json.loads(line)
            for line in show.show(world.store, "claude", f"{SID}.{name}")[:-1]
        ]
        session = [row for row in shown if row["type"] == "session"]
        assert len(session) == 1, name
        assert session[0]["id"] == f"{SID}.{name}"
        assert session[0]["parent"] == SID
        assert session[0]["source"]["path"] == path
        assert session[0]["metadata"]["parent"] == SID
        assert session[0]["metadata"]["parent_evidence"] == "source"
        statuses = [row["status"] for row in shown if row["type"] == "status"]
        assert statuses and statuses[-1] == "ended", (name, statuses)
    listed = {
        json.loads(line)["id"] for line in show.list_sessions(world.store, "claude")
    }
    assert {f"{SID}.a77", f"{SID}.a4b5e111994658fef"} <= listed
    claude = next(item for item in TRANSLATORS if item.vendor == "claude")
    for other in ("workflows/agent-x.jsonl", "workflows/other/agent-x.jsonl"):
        assert claude.identity(world.claude, sub / other) is None, other


def test_12_a_second_source_for_an_existing_id_is_refused(world: World) -> None:
    second = world.claude / "projects" / "second-project"
    second.mkdir(parents=True, mode=0o700)
    daemon = world.daemon()
    world.write([user(1, "first home")])
    daemon.cycle(cold=True)
    count = world.table("SELECT count(*) FROM journal_row")[0][0]
    (second / f"{SID}.jsonl").write_text(json.dumps(user(2, "second home")) + "\n")
    daemon.cycle(cold=True)
    daemon.close()
    assert world.table("SELECT count(*) FROM journal_row")[0][0] == count
    failed = world.table(
        "SELECT status, reason FROM journal_source WHERE path = ?",
        (f"projects/second-project/{SID}.jsonl",),
    )
    assert [tuple(row) for row in failed] == [("failed", "source mismatch")]
    assert "second home" not in json.dumps(world.rows())


# -- 13. discovery ---------------------------------------------------------------------------------


def test_13_existing_files_and_later_appends_are_translated(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nspawn import threads

    account = world.root / "account-home"
    (account / "projects" / PROJECT).mkdir(parents=True, mode=0o700)
    roster_file = world.root / "spawn.toml"
    roster_file.write_text(
        '[[provider]]\nname = "claude"\nkind = "claude"\n'
        f'[provider.accounts.work]\nhome = "{account}"\n'
    )
    monkeypatch.setenv("HOME", str(world.home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    roster = Roster.read(roster_file)
    homes = threads.claude_homes(roster)
    assert homes == [(None, world.claude), ("work", account)]
    still = world.write([user(1, "old and still")], sid=OTHER)
    grows = account / "projects" / PROJECT / f"{SID}.jsonl"
    grows.write_text(json.dumps(user(2, "line zero")) + "\n")
    daemon = world.daemon(homes=homes)
    daemon.cycle(cold=True)
    with grows.open("a") as stream:
        stream.write(json.dumps(user(3, "line one")) + "\n")
    daemon.cycle(cold=True)
    daemon.close()
    assert still.exists()
    assert any(row.get("text") == "old and still" for row in world.rows(OTHER))
    rows = world.rows()
    assert [row.get("text") for row in rows if row["type"] == "message"] == [
        "line zero",
        "line one",
    ]
    assert rows[0]["account"] == "work"


# -- 14. modes ----------------------------------------------------------------------------------------


def test_14_media_modes_and_a_shared_media_root_is_refused(world: World) -> None:
    translated(world, [user(1, [image(PNG)])])
    media = world.nott / "sessions" / "media"
    assert stat.S_IMODE((world.nott / "sessions").stat().st_mode) == 0o700
    assert stat.S_IMODE(media.stat().st_mode) == 0o700
    assert {stat.S_IMODE(path.stat().st_mode) for path in media.iterdir()} == {0o600}
    assert world.store == store_path()
    assert world.store.parent == Path(os.environ["NOTT_DATABASE_DIR"])
    media.chmod(0o750)
    with pytest.raises(serve.Refused, match=str(media)):
        serve.Daemon(world.settings())


# -- 15. the loop ----------------------------------------------------------------------------------


def test_15_serve_shows_an_appended_line_within_two_seconds(world: World) -> None:
    stop = threading.Event()
    settings = world.settings(clock=time.time)
    worker = threading.Thread(target=serve.run, args=(settings, stop))
    worker.start()
    try:
        # Wait for the lock before the first live append.
        lock = world.nott / "sessions" / "serve.lock"
        deadline = time.monotonic() + 10
        while not lock.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        time.sleep(0.5)
        world.write([user(1, "first")])
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                if len(world.rows()) >= 2:
                    break
            except show.Missing:
                pass
            time.sleep(0.05)
        assert len(world.rows()) >= 2
        world.write([user(2, "second")])
        started = time.monotonic()
        while time.monotonic() - started < 2:
            if any(row.get("text") == "second" for row in world.rows()):
                break
            time.sleep(0.02)
        assert any(row.get("text") == "second" for row in world.rows())
        assert time.monotonic() - started < 2
    finally:
        stop.set()
        worker.join(10)
    assert not worker.is_alive()


# -- 16. CLI ---------------------------------------------------------------------------------------


def test_16_cli_show_list_and_refusals(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    translated(world, [user(1, "hello"), agent(2, "msg_1", text("hi"), usage(4, 9))])
    assert cli.main(["session", "show", "claude", SID]) == 0
    printed = capsys.readouterr().out.splitlines()
    assert printed == show.show(world.store, "claude", SID)
    assert json.loads(printed[-1])["total"]["output"] == 4
    assert cli.main(["session", "list"]) == 0
    listed = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [(row["vendor"], row["id"]) for row in listed] == [("claude", SID)]
    assert listed[0]["total"]["cache_read"] == 9
    assert listed[0]["cwd"] == "/work/demo" and listed[0]["model"] == "claude-opus-5-5"
    assert listed[0]["status"] == "ended"
    assert cli.main(["session", "list", "claude"]) == 0
    capsys.readouterr()
    for arguments in (
        ["session", "show", "gemini", SID],
        ["session", "show", "claude", "../etc"],
        ["session", "show", "claude"],
        ["session", "list", "gemini"],
        ["session", "show", "claude", OTHER],
    ):
        assert cli.main(arguments) == 2, arguments
    assert OTHER in capsys.readouterr().err
    os.environ["NOTT_DATABASE_DIR"] = str(world.root / "no-store")
    assert cli.main(["session", "show", "claude", SID]) == 2
    assert str(world.root / "no-store") in capsys.readouterr().err


def test_16_cli_show_through_the_executable(world: World) -> None:
    translated(world, [user(1, "hello")])
    done = subprocess.run(
        [
            str(Path(sys.executable).with_name("nspawn")),
            "session",
            "show",
            "claude",
            SID,
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == show.show(world.store, "claude", SID)


# -- 17. redaction ---------------------------------------------------------------------------------


def test_17_needles_are_redacted_and_a_rotation_keeps_the_old_one(world: World) -> None:
    secrets = world.root / "secrets"
    secrets.mkdir(mode=0o700)
    token = secrets / "work.token"
    old, new = "sk-ant-oat01-" + "A" * 40, "sk-ant-oat01-" + "B" * 40
    token.write_text(old)
    token.chmod(0o600)
    roster_file = world.root / "spawn.toml"
    roster_file.write_text(
        f'[[provider]]\nname = "claude"\nkind = "claude"\nsecrets = "{secrets}"\n'
    )
    world.roster = Roster.read(roster_file)
    daemon = world.daemon()
    world.write([result(1, "toolu_1", f"token is {old}")])
    daemon.cycle(cold=True)
    token.write_text(new)
    token.chmod(0o600)
    world.clock += 10
    world.write([result(2, "toolu_2", f"old {old} new {new}")])
    daemon.cycle(cold=True)
    daemon.close()
    stored = json.dumps(world.rows())
    assert old not in stored and new not in stored
    assert stored.count("[redacted-credential]") == 3
    assert old.encode() not in stored_bytes(world)


# -- the store's own row shape ----------------------------------------------------------------------


def test_status_rows_follow_liveness(world: World) -> None:
    sessions = world.claude / "sessions"
    sessions.mkdir()
    ticks = Path(f"/proc/{os.getpid()}/stat").read_text()
    start = ticks[ticks.rindex(")") + 2 :].split()[19]
    (sessions / f"{os.getpid()}.json").write_text(
        json.dumps({"pid": os.getpid(), "sessionId": SID, "procStart": start})
    )
    daemon = world.daemon()
    world.write([user(1, "go")])
    daemon.cycle(cold=True)
    world.write([agent(2, "msg_1", text("done"), usage(1), stop_reason="end_turn")])
    daemon.cycle(cold=True)
    (sessions / f"{os.getpid()}.json").unlink()
    daemon.cycle(cold=True)
    daemon.close()
    states = [row["status"] for row in world.rows() if row["type"] == "status"]
    assert states == ["live", "waiting", "ended"]


def test_a_source_that_shrinks_fails_once_and_stops(world: World) -> None:
    daemon = world.daemon()
    path = world.write([user(1, "one"), user(2, "two")])
    daemon.cycle(cold=True)
    path.write_text(json.dumps(user(3, "three")) + "\n")
    daemon.cycle(cold=True)
    world.write([user(4, "four")])
    daemon.cycle(cold=True)
    daemon.close()
    rows = world.rows()
    failed = [
        row for row in rows if row["type"] == "status" and row["status"] == "failed"
    ]
    assert [row["reason"] for row in failed] == ["source shrank"]
    assert "four" not in json.dumps(rows)
    again = world.daemon()
    world.write([user(5, "five")])
    again.cycle(cold=True)
    assert not again.sources and again.metrics.read == 0
    again.close()
    assert world.rows() == rows


def test_low_disk_stops_writing(world: World) -> None:
    daemon = world.daemon(disk_floor=1 << 62)
    world.write([user(1, "one")])
    daemon.cycle(cold=True)
    daemon.close()
    assert (
        world.table("SELECT count(*) FROM journal_row WHERE type != 'status'")[0][0]
        == 0
    )


def iterate(rows: list[dict[str, Any]]) -> Iterator[int]:
    yield from (row["seq"] for row in rows)


def test_seq_has_no_gap_and_src_never_goes_down(world: World) -> None:
    daemon = world.daemon()
    shutil.copyfile(FIXTURES / "claude-session.jsonl", world.source())
    daemon.cycle(cold=True)
    daemon.close()
    rows = world.rows()
    assert list(iterate(rows)) == list(range(len(rows)))
    sources = [row["src"] for row in rows]
    assert sources == sorted(sources)


def test_a_run_start_result_names_its_nspawn_run(world: World) -> None:
    start = {
        "type": "tool_use",
        "id": "toolu_run",
        "name": "mcp__plugin_nott_nspawn__nspawn_run_start",
        "input": {},
    }
    rows = translated(
        world,
        [
            agent(1, "msg_1", start, usage(1)),
            result(
                2, "toolu_run", [text('{"run_id": "run-7f3a", "phase": "running"}')]
            ),
        ],
    )
    child = next(row for row in rows if row["type"] == "child")
    assert (child["id"], child["kind"]) == ("run-7f3a", "nspawn_run")


def test_the_journal_imports_only_what_d3_names() -> None:
    """D3: owner_roster, threads, vendor_home, redact and the registry's open
    path; never supervisor, mcp or the registry's run logic."""
    import ast

    allowed = {"owner_roster", "threads", "vendor_home", "redact", "registry", ""}
    package = Path(__file__).parents[1] / "nspawn" / "journal"
    for module in package.glob("*.py"):
        for node in ast.walk(ast.parse(module.read_text())):
            if isinstance(node, ast.ImportFrom) and node.level == 2:
                named = [node.module] if node.module else [a.name for a in node.names]
                assert set(named) <= allowed, (module.name, named)
                if node.module == "registry":
                    names = {alias.name for alias in node.names}
                    assert names <= {
                        "open_store",
                        "store_path",
                        "RegistryError",
                        "effort_read",
                    }
            if isinstance(node, ast.ImportFrom) and node.level == 0:
                assert not (node.module or "").startswith("nspawn"), module.name


def test_serve_refuses_a_shared_media_root_with_exit_2(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    media = world.nott / "sessions" / "media"
    media.mkdir(parents=True, mode=0o700)
    (world.nott / "sessions").chmod(0o700)
    media.chmod(0o755)
    monkeypatch.setenv("HOME", str(world.home))
    assert cli.main(["session", "serve"]) == 2
    assert str(media) in capsys.readouterr().err
    assert cli.main(["session", "serve", "--extra"]) == 2


# -- one bad line never stops the writer ------------------------------------------------------------


def test_a_lone_surrogate_becomes_a_replacement_character(world: World) -> None:
    daemon = world.daemon()
    world.write(
        [
            result(1, "toolu_1", "a\ud83d"),
            agent(2, "msg_1", text("b\ud83d"), usage(1)),
            agent(
                3,
                "msg_2",
                {"type": "thinking", "thinking": "c\udc00", "signature": "s"},
                usage(1),
            ),
        ]
    )
    with world.source().open("ab") as stream:
        raw = json.dumps(user(4, "d\ud83d"), ensure_ascii=False)
        stream.write(raw.encode("utf-8", "surrogatepass") + b"\n")
    world.write([user(5, "after")])
    daemon.cycle(cold=True)
    daemon.close()
    rows = world.rows()
    for row in rows:
        validate(row)
    assert [row["output"] for row in rows if row["type"] == "tool_result"] == ["a�"]
    assert [row.get("text") for row in rows if row["type"] == "message"] == [
        "b�",
        "d�",
        "after",
    ]
    assert [row["text"] for row in rows if row["type"] == "reasoning"] == ["c�"]


def test_two_blocks_of_one_message_are_two_rows(world: World) -> None:
    said = agent(1, "msg_1", text("one"), usage(1))
    said["message"]["content"].append(text("two"))
    thought = agent(
        2,
        "msg_2",
        {"type": "thinking", "thinking": "first", "signature": "s"},
        usage(1),
    )
    thought["message"]["content"] += [
        {"type": "thinking", "thinking": "second", "signature": "s"},
        {"type": "redacted_thinking", "data": "xx"},
        {"type": "redacted_thinking", "data": "yyy"},
    ]
    rows = translated(world, [said, thought, user(3, "after")])
    assert [row.get("text") for row in rows if row["type"] == "message"] == [
        "one",
        "two",
        "after",
    ]
    reasoning = [row for row in rows if row["type"] == "reasoning"]
    assert [(row.get("text"), row.get("redacted")) for row in reasoning] == [
        ("first", None),
        ("second", None),
        (None, 2),
        (None, 3),
    ]


def test_a_source_whose_batch_raises_fails_alone(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    translator = TRANSLATORS[0]
    original = translator.translate

    def translate(line: bytes, src: int, state: Any, context: Any) -> Any:
        if b"boom" in line:
            raise RuntimeError("boom")
        return original(line, src, state, context)

    monkeypatch.setattr(translator, "translate", translate)
    third = "7e6d5c4b-2222-4333-8444-555566667777"
    daemon = world.daemon()
    world.write([user(1, "one")])
    world.write([user(1, "other one")], sid=OTHER)
    daemon.cycle(cold=True)
    world.write([user(2, "two"), user(3, "boom"), user(4, "four")])
    world.write([user(2, "other two")], sid=OTHER)
    daemon.cycle(cold=True)
    world.write([user(1, "boom")], sid=third)
    world.write([user(3, "other three")], sid=OTHER)
    daemon.cycle(cold=True)
    assert [source.session for source in daemon.sources.values()] == [OTHER]
    daemon.close()
    rows = world.rows()
    assert [row.get("text") for row in rows if row["type"] == "message"] == [
        "one",
        "two",
    ]
    failed = [
        row for row in rows if row["type"] == "status" and row["status"] == "failed"
    ]
    assert [row["reason"] for row in failed] == ["RuntimeError"]
    assert [
        row.get("text") for row in world.rows(OTHER) if row["type"] == "message"
    ] == ["other one", "other two", "other three"]
    assert sorted(
        world.table(
            "SELECT session, status, reason FROM journal_source WHERE status IS NOT NULL"
        )
    ) == sorted([(SID, "failed", "RuntimeError"), (third, "failed", "RuntimeError")])
    again = world.daemon()
    world.write([user(5, "five")])
    world.write([user(4, "other four")], sid=OTHER)
    again.cycle(cold=True)
    again.close()
    assert world.rows() == rows
    assert "other four" in json.dumps(world.rows(OTHER))


# -- redaction fails closed -------------------------------------------------------------------------


def secrets_roster(world: World, token: str) -> tuple[Path, str]:
    """A legacy roster with one token file; returns the token path and the roster text."""
    secrets = world.root / "secrets"
    secrets.mkdir(mode=0o700, exist_ok=True)
    path = secrets / "work.token"
    path.write_text(token)
    path.chmod(0o600)
    return (
        path,
        f'[[provider]]\nname = "claude"\nkind = "claude"\nsecrets = "{secrets}"\n',
    )


def test_an_unreadable_roster_writes_nothing_until_it_is_read(world: World) -> None:
    secret = "sk-ant-oat01-" + "C" * 40
    _, good = secrets_roster(world, secret)
    roster_file = world.root / "spawn.toml"
    roster_file.write_text('[jev]\nendpoint = "https://roster.invalid/v1"\n')
    logged: list[str] = []
    daemon = world.daemon(
        read_roster=lambda: Roster.read(roster_file), log=logged.append
    )
    world.write([result(1, "toolu_1", f"token is {secret}")])
    daemon.cycle(cold=True)
    daemon.cycle(cold=False)
    assert world.table("SELECT count(*) FROM journal_row")[0][0] == 0
    assert world.table("SELECT count(*) FROM journal_source")[0][0] == 0
    assert "writing paused: secrets" in daemon.report()
    assert any("roster" in line and "RosterError" in line for line in logged)
    roster_file.write_text(good)
    daemon.cycle(cold=True)
    assert "paused" not in daemon.report()
    daemon.close()
    stored = json.dumps(world.rows())
    assert secret not in stored and "[redacted-credential]" in stored
    assert secret.encode() not in stored_bytes(world)


def test_a_roster_path_that_is_a_directory_pauses_the_writer(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a path where nothing exists is an absent roster. A directory is a
    roster that cannot be read, so the writer of `main` stays paused."""
    secret = "sk-ant-oat01-" + "G" * 40
    folder = world.root / "roster-folder"
    folder.mkdir()
    monkeypatch.setenv("NOTT_NSPAWN_CONFIG", str(world.root / "absent-config.toml"))
    monkeypatch.setenv("NOTT_NSPAWN_ROSTER", str(folder))
    taken: list[serve.Settings] = []
    monkeypatch.setattr(serve, "run", lambda settings, _stop: taken.append(settings))
    assert serve.main() == 0
    logged: list[str] = []
    daemon = world.daemon(read_roster=taken[0].read_roster, log=logged.append)
    world.write([result(1, "toolu_1", f"token is {secret}")])
    daemon.cycle(cold=True)
    daemon.cycle(cold=False)
    daemon.close()
    assert world.table("SELECT count(*) FROM journal_row")[0][0] == 0
    assert "writing paused: secrets" in daemon.report()
    assert any("RosterError" in line for line in logged)
    assert secret.encode() not in stored_bytes(world)


@pytest.mark.timeout(10, method="signal")
@pytest.mark.parametrize(
    ("source", "refusal"), [("config", "ConfigError"), ("roster", "RosterError")]
)
def test_a_fifo_at_the_roster_source_pauses_the_writer_without_a_wait(
    world: World, monkeypatch: pytest.MonkeyPatch, source: str, refusal: str
) -> None:
    """A FIFO with no writer at the selected source is a roster that cannot be
    read, not a wait. The writer of `main` stays paused and writes no row.

    The SIGALRM of pytest-timeout stops a blocking open of the FIFO in the test
    thread, so a reader that waits fails this test and does not hang the run.
    """
    secret = "sk-ant-oat01-" + "H" * 40
    fifo = world.root / "roster.fifo"
    os.mkfifo(fifo)
    if source == "config":
        monkeypatch.setenv("NOTT_NSPAWN_CONFIG", str(fifo))
        monkeypatch.delenv("NOTT_NSPAWN_ROSTER", raising=False)
    else:
        monkeypatch.setenv("NOTT_NSPAWN_CONFIG", str(world.root / "absent.toml"))
        monkeypatch.setenv("NOTT_NSPAWN_ROSTER", str(fifo))
    taken: list[serve.Settings] = []
    monkeypatch.setattr(serve, "run", lambda settings, _stop: taken.append(settings))
    assert serve.main() == 0
    logged: list[str] = []
    daemon = world.daemon(read_roster=taken[0].read_roster, log=logged.append)
    world.write([result(1, "toolu_1", f"token is {secret}")])
    daemon.cycle(cold=True)
    daemon.cycle(cold=False)
    daemon.close()
    assert world.table("SELECT count(*) FROM journal_row")[0][0] == 0
    assert "writing paused: secrets" in daemon.report()
    assert any(refusal in line for line in logged)
    assert secret.encode() not in stored_bytes(world)


def test_an_unreadable_token_file_writes_nothing_until_it_is_read(
    world: World,
) -> None:
    old, new = "sk-ant-oat01-" + "D" * 40, "sk-ant-oat01-" + "E" * 40
    token, text_ = secrets_roster(world, old)
    roster_file = world.root / "spawn.toml"
    roster_file.write_text(text_)
    world.roster = Roster.read(roster_file)
    daemon = world.daemon()
    world.write([result(1, "toolu_1", f"token is {old}")])
    daemon.cycle(cold=True)
    token.write_text(new)
    token.chmod(0o644)
    world.clock += 10
    world.write([result(2, "toolu_2", f"new {new}")])
    daemon.cycle(cold=True)
    rows = world.rows()
    assert [row["id"] for row in rows if row["type"] == "tool_result"] == ["toolu_1"]
    assert [(row["status"], row["reason"]) for row in rows[-1:]] == [
        ("failed", "secrets")
    ]
    assert "writing paused: secrets" in daemon.report()
    token.chmod(0o600)
    daemon.cycle(cold=True)
    daemon.close()
    stored = json.dumps(world.rows())
    assert [row["id"] for row in world.rows() if row["type"] == "tool_result"] == [
        "toolu_1",
        "toolu_2",
    ]
    assert old not in stored and new not in stored
    assert new.encode() not in stored_bytes(world)


@pytest.mark.parametrize(
    "provider",
    [
        '[provider.accounts.work]\nhome = "{root}/account-home"\n',
        '[provider.accounts.colab-1]\nbase_url = "http://127.0.0.1:18090"\n',
        'secrets = "{root}/absent"\n',
    ],
    ids=["home", "base_url", "absent_secrets"],
)
def test_an_account_with_no_token_file_does_not_pause(
    world: World, provider: str
) -> None:
    roster_file = world.root / "spawn.toml"
    roster_file.write_text(
        '[[provider]]\nname = "claude"\nkind = "claude"\n'
        + provider.format(root=world.root)
    )
    logged: list[str] = []
    daemon = world.daemon(
        read_roster=lambda: Roster.read(roster_file), log=logged.append
    )
    world.write([user(1, "one")])
    daemon.cycle(cold=True)
    world.write([user(2, "two")])
    daemon.cycle(cold=True)
    daemon.close()
    assert "paused" not in daemon.report()
    assert not [line for line in logged if "paused" in line]
    assert [row.get("text") for row in world.rows() if row["type"] == "message"] == [
        "one",
        "two",
    ]


def test_a_declared_token_file_that_exists_and_is_not_read_pauses(
    world: World,
) -> None:
    secret = "sk-ant-oat01-" + "F" * 40
    secrets = world.root / "secrets"
    secrets.mkdir(mode=0o700)
    token = secrets / "work.token"
    token.write_text(secret)
    token.chmod(0o000)
    roster_file = world.root / "spawn.toml"
    roster_file.write_text(
        f'[[provider]]\nname = "claude"\nkind = "claude"\nsecrets = "{secrets}"\n'
        '[provider.accounts.work]\nauth_env = "CLAUDE_CODE_OAUTH_TOKEN"\n'
        '[provider.accounts.spare]\nauth_env = "CLAUDE_CODE_OAUTH_TOKEN"\n'
    )
    world.roster = Roster.read(roster_file)
    daemon = world.daemon()
    world.write([result(1, "toolu_1", f"token is {secret}")])
    daemon.cycle(cold=True)
    assert world.table("SELECT count(*) FROM journal_row")[0][0] == 0
    assert "writing paused: secrets (the token" in daemon.report()
    token.chmod(0o600)
    daemon.cycle(cold=True)
    daemon.close()
    assert "paused" not in daemon.report()
    stored = json.dumps(world.rows())
    assert secret not in stored and "[redacted-credential]" in stored


def test_a_ceiling_stop_holds_when_its_status_write_fails(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    old, new = "sk-ant-oat01-" + "G" * 40, "sk-ant-oat01-" + "H" * 40
    token, text_ = secrets_roster(world, old)
    roster_file = world.root / "spawn.toml"
    roster_file.write_text(text_)
    world.roster = Roster.read(roster_file)
    original = nott_database.write
    busy = [False]

    def write(connection: Any) -> Any:
        if busy[0]:
            raise nott_database.BusyError("busy")
        return original(connection)

    monkeypatch.setattr(nott_database, "write", write)
    daemon = world.daemon()
    world.write([user(1, "one")])
    daemon.cycle(cold=True)
    monkeypatch.setattr(serve, "NEEDLE_MAX_BYTES", 1)
    token.write_text(new)
    world.clock += 10
    world.write([result(2, "toolu_2", f"new {new}")])
    busy[0] = True
    assert daemon.cycle(cold=True) == 0
    assert daemon.cycle(cold=False) == 0
    busy[0] = False
    assert daemon.cycle(cold=False) == 0
    assert daemon.cycle(cold=True) == 0
    daemon.close()
    rows = world.rows()
    assert not [row for row in rows if row["type"] == "tool_result"]
    assert [(row["status"], row["reason"]) for row in rows[-1:]] == [
        ("failed", "secrets")
    ]
    assert new.encode() not in stored_bytes(world)


def test_a_token_file_whose_merge_fails_is_read_again(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    old, new = "sk-ant-oat01-" + "J" * 40, "sk-ant-oat01-" + "K" * 40
    token, text_ = secrets_roster(world, old)
    roster_file = world.root / "spawn.toml"
    roster_file.write_text(text_)
    world.roster = Roster.read(roster_file)
    daemon = world.daemon()
    world.write([user(1, "one")])
    daemon.cycle(cold=True)
    original = serve.SecretNeedles.merged
    failing = [True]

    def merged(self: Any, other: Any, **options: Any) -> Any:
        if failing[0]:
            failing[0] = False
            raise OSError("the disk refused")
        return original(self, other, **options)

    monkeypatch.setattr(serve.SecretNeedles, "merged", merged)
    token.write_text(new)
    world.clock += 10
    world.write([result(2, "toolu_2", f"new {new}")])
    assert daemon.cycle(cold=True) == 0
    daemon.cycle(cold=True)
    daemon.close()
    stored = json.dumps(world.rows())
    assert [row["id"] for row in world.rows() if row["type"] == "tool_result"] == [
        "toolu_2"
    ]
    assert new not in stored and "[redacted-credential]" in stored
    assert new.encode() not in stored_bytes(world)


@pytest.mark.parametrize(
    "error", [nott_database.BusyError("busy"), nott_database.RefusedError("refused")]
)
def test_a_store_error_keeps_the_source_and_the_next_cycle_translates_it(
    world: World, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    original = nott_database.write
    failing = [False]

    def write(connection: Any) -> Any:
        if failing[0]:
            raise error
        return original(connection)

    monkeypatch.setattr(nott_database, "write", write)
    daemon = world.daemon()
    world.write([user(1, "one")])
    daemon.cycle(cold=True)
    world.write([user(2, "two")])
    failing[0] = True
    daemon.cycle(cold=False)
    failing[0] = False
    assert [source.session for source in daemon.sources.values()] == [SID]
    assert (
        world.table("SELECT count(*) FROM journal_source WHERE status IS NOT NULL")[0][
            0
        ]
        == 0
    )
    daemon.cycle(cold=False)
    daemon.close()
    rows = world.rows()
    assert [row.get("text") for row in rows if row["type"] == "message"] == [
        "one",
        "two",
    ]
    assert not [row for row in rows if row.get("status") == "failed"]
