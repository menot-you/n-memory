"""Thread verbs use the writer's rows when its unit is installed."""

import json
import time
from pathlib import Path

import pytest
from test_thread_list import (
    A,
    B,
    C,
    E,
    World,
    assistant,
    codex_rows,
    codex_thread,
    user,
)


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def test_installed_writer_keeps_visibility_title_order_and_exclusion(
    world: World,
) -> None:
    now = time.time()
    world.session(
        A,
        [
            user("a", None, "first A"),
            {"type": "custom-title", "customTitle": "Custom title A"},
        ],
        mtime=now,
    )
    world.session(
        B,
        [user("b", None, "first B"), {"type": "summary", "summary": "Summary B"}],
        mtime=now - 1,
    )
    world.session(C, [user("c", None, "first C")], mtime=now - 2)
    world.session(E, [user("e", None, "headless", entrypoint="sdk-cli")], mtime=now + 1)
    world.journal(world.claude)
    shown = world.listed()
    rows = shown["threads"]
    assert [r["sessionId"] for r in rows] == [A, B, C]
    assert [r["title"] for r in rows] == ["Custom title A", "Summary B", "first C"]
    import subprocess
    import sys

    done = subprocess.run(
        [
            str(Path(sys.executable).with_name("nspawn")),
            "thread",
            "list",
            "--limit",
            "1",
            "--exclude",
            "-",
        ],
        input=json.dumps([A]),
        env=world.env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert [r["sessionId"] for r in json.loads(done.stdout)["threads"]] == [B]


def test_installed_writer_reads_no_source_at_request_time(world: World) -> None:
    source = world.session(
        A, [user("a", None, "stored"), assistant("b", "a", "answer", ident="m1")]
    )
    world.journal(world.claude)
    source.unlink()
    shown = world.verb(
        "show", f"claude:{A}", "--messages", "--claude-home", str(world.claude)
    )
    assert shown.returncode == 0, shown.stderr
    assert json.loads(shown.stdout)["messages"][0]["message"]["content"] == "stored"


def test_installed_writer_keeps_codex_on_the_app_server(world: World) -> None:
    world.threads(world.codex, [[codex_thread("t1", world.root)]])
    world.session(A, [user("a", None, "first A")])
    world.journal(world.claude)
    shown = world.listed()
    assert list(codex_rows(shown)) == ["t1"]
    assert A in [r["sessionId"] for r in shown["threads"] if r["provider"] == "claude"]
    one = world.verb("show", "codex:t1")
    assert one.returncode == 0, one.stderr
    assert json.loads(one.stdout)["thread"]["sessionId"] == "t1"


def test_installed_writer_follows_links_of_dropped_records(world: World) -> None:
    world.session(
        A,
        [
            user("a1", None, "root"),
            assistant("a2", "a1", "first answer", ident="m1"),
            {
                "type": "attachment",
                "uuid": "a3",
                "parentUuid": "a2",
                "attachment": {"type": "unknown"},
            },
            {
                "type": "system",
                "subtype": "informational",
                "uuid": "a4",
                "parentUuid": "a3",
                "content": "dropped",
            },
            {
                "type": "system",
                "subtype": "compact_boundary",
                "uuid": "a5",
                "parentUuid": None,
                "logicalParentUuid": "a4",
                "compactMetadata": {"trigger": "auto", "preTokens": 10},
            },
            user("a6", "a5", "after compact"),
            assistant("a7", "a6", "kept answer", ident="m2"),
            user("a8", "a7", "abandoned prompt"),
            assistant("a9", "a8", "abandoned answer", ident="m3"),
            user("a10", "a7", "rewound prompt"),
            assistant("a11", "a10", "rewound answer", ident="m4"),
        ],
    )
    world.journal(world.claude)
    done = world.verb(
        "show", f"claude:{A}", "--messages", "--claude-home", str(world.claude)
    )
    assert done.returncode == 0, done.stderr
    shown = json.loads(done.stdout)
    assert [m["uuid"] for m in shown["messages"]] == [
        "a1",
        "a2",
        "a6",
        "a7",
        "a10",
        "a11",
    ]


def test_installed_writer_lists_no_transcript_that_is_gone(world: World) -> None:
    """Review r1, blocker 4: Claude Code deletes old transcripts; a session
    whose file is gone is neither listed nor shown as continuable, as the SDK
    reader answered. Its stored history stays readable."""
    gone = world.session(A, [user("a", None, "gone")])
    world.session(B, [user("b", None, "kept")])
    world.journal(world.claude)
    gone.unlink()
    listed = [r["sessionId"] for r in world.listed()["threads"]]
    assert listed == [B]
    # The verb answers `{"thread": null}`, which the CLI prints as not found.
    one = world.verb("show", f"claude:{A}")
    assert (one.returncode, one.stdout) == (3, ""), one.stderr
    assert "not found" in one.stderr
    kept = world.verb("show", f"claude:{B}")
    assert json.loads(kept.stdout)["thread"]["continuable"] is True
