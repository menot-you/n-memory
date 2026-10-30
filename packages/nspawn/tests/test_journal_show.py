"""The session journal's read path for the node host (cut 2, lane N).

`nspawn.journal.show` is the one reader of the journal format. The node host
calls its functions in-process, and the CLI verbs `nspawn session list|show`
call the same functions. Every store is written by the real cut 1 writer from
invented Claude lines in a temp home; no test reads the owner's homes or stores.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

import nott_database
import pytest
from test_journal import (
    FIXTURES,
    NOW,
    OTHER,
    PNG,
    PROJECT,
    SID,
    World,
    agent,
    image,
    text,
    translated,
    usage,
    user,
    validate,
)

from nspawn import cli
from nspawn.journal import show
from nspawn.registry import store_path


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    made = World(tmp_path)
    monkeypatch.setenv("HOME", str(made.home))
    return made


def live(world: World, sid: str) -> None:
    """A pid record of this process for `sid`, so the writer sees it live."""
    sessions = world.claude / "sessions"
    sessions.mkdir(exist_ok=True)
    ticks = Path(f"/proc/{os.getpid()}/stat").read_text()
    start = ticks[ticks.rindex(")") + 2 :].split()[19]
    (sessions / f"{os.getpid()}.json").write_text(
        json.dumps({"pid": os.getpid(), "sessionId": sid, "procStart": start})
    )


def reader() -> nott_database.Connection:
    connection = show.open_readonly()
    assert connection is not None
    return connection


def edit(sql: str) -> None:
    """One write on this test's store, outside the reader under test."""
    connection = nott_database.connect(store_path())
    try:
        with nott_database.write(connection):
            connection.execute(sql)
    finally:
        connection.close()


# -- 1. rows_after and show ---------------------------------------------------------


def test_1_rows_after_gives_the_rows_after_k_in_order_up_to_limit(
    world: World,
) -> None:
    translated(
        world,
        [
            user(1, "hello"),
            agent(2, "msg_1", text("one"), usage(1)),
            user(3, "again"),
            agent(4, "msg_2", text("two"), usage(2)),
        ],
    )
    stored = world.rows()
    connection = reader()
    try:
        every = show.rows_after(connection, "claude", SID, -1, 1000)
        assert every == stored
        assert [row["seq"] for row in every] == list(range(len(stored)))
        assert show.rows_after(connection, "claude", SID, 1, 2) == stored[2:4]
        assert show.rows_after(connection, "claude", SID, 2, 1000) == stored[3:]
        last = stored[-1]["seq"]
        assert show.rows_after(connection, "claude", SID, last, 10) == []
        assert show.rows_after(connection, "claude", OTHER, -1, 10) == []
        with pytest.raises(ValueError):
            show.rows_after(connection, "claude", SID, -1, 0)
    finally:
        connection.close()


@pytest.mark.parametrize("limit", [1, 4, 5, 10])
def test_replay_start_keeps_the_last_rows(world: World, limit: int) -> None:
    stored = translated(world, [user(1, "one"), user(2, "two"), user(3, "three")])
    connection = reader()
    try:
        cursor = show.replay_start(connection, "claude", SID, limit)
        assert cursor == (stored[-limit - 1]["seq"] if len(stored) > limit else None)
        assert (
            show.rows_after(
                connection, "claude", SID, -1 if cursor is None else cursor, limit + 1
            )
            == stored[-limit:]
        )
        assert show.replay_start(connection, "claude", OTHER, limit) is None
        for invalid in (0, -1):
            with pytest.raises(ValueError):
                show.replay_start(connection, "claude", SID, invalid)
    finally:
        connection.close()


@pytest.mark.parametrize("limit", [1, 2, 200])
def test_rows_before_returns_the_nearest_rows_in_ascending_order(
    world: World, limit: int
) -> None:
    stored = translated(world, [user(n, str(n)) for n in range(240)])
    daemon = world.daemon()
    world.write([user(1, "other session", sessionId=OTHER)], OTHER)
    daemon.cycle(cold=True)
    daemon.close()
    connection = reader()
    try:
        assert (
            show.rows_before(connection, "claude", SID, 220, limit)
            == stored[max(0, 220 - limit) : 220]
        )
        assert show.rows_before(connection, "claude", SID, 0, limit) == []
        assert show.rows_before(connection, "claude", SID, -1, limit) == []
        assert show.rows_before(connection, "claude", "missing", 220, limit) == []
        assert show.rows_before(connection, "codex", SID, 220, limit) == []
        assert (
            show.rows_before(connection, "claude", SID, 1000, limit) == stored[-limit:]
        )
    finally:
        connection.close()


@pytest.mark.parametrize("limit", [0, -1, 201, True])
def test_rows_before_refuses_an_invalid_limit(world: World, limit: int) -> None:
    translated(world, [user(1, "one")])
    connection = reader()
    try:
        with pytest.raises(ValueError, match="limit"):
            show.rows_before(connection, "claude", SID, 10, limit)
    finally:
        connection.close()


def test_bounds_returns_the_retained_endpoints(world: World) -> None:
    stored = translated(world, [user(n, str(n)) for n in range(10)])
    connection = reader()
    try:
        assert show.bounds(connection, "claude", SID) == (0, stored[-1]["seq"])
        assert show.bounds(connection, "claude", OTHER) is None
        assert show.bounds(connection, "codex", SID) is None
        edit("DELETE FROM journal_row WHERE seq < 3 OR seq = 5")
        assert show.bounds(connection, "claude", SID) == (3, stored[-1]["seq"])
        assert show.rows_before(connection, "claude", SID, 8, 3) == [
            stored[4],
            stored[6],
            stored[7],
        ]
        edit("DELETE FROM journal_row")
        assert show.bounds(connection, "claude", SID) is None
        assert show.rows_before(connection, "claude", SID, 8, 3) == []
    finally:
        connection.close()


def test_replay_stops_at_the_captured_head(world: World) -> None:
    stored = translated(world, [user(n, str(n)) for n in range(10)])
    head = stored[-1]["seq"]
    daemon = world.daemon()
    world.write([user(20, "after capture")])
    daemon.cycle(cold=True)
    daemon.close()
    connection = reader()
    try:
        cursor = show.replay_start(connection, "claude", SID, 3, through_seq=head)
        assert cursor == stored[-4]["seq"]
        assert (
            show.rows_after(connection, "claude", SID, cursor, 200, through_seq=head)
            == stored[-3:]
        )
        assert (
            show.rows_after(connection, "claude", SID, head, 200, through_seq=head)
            == []
        )
        assert (
            show.replay_start(connection, "claude", SID, len(stored), through_seq=head)
            is None
        )
        assert show.rows_after(connection, "claude", SID, head, 200)
    finally:
        connection.close()


@pytest.mark.parametrize("backlog", [0, 2000, 2001, 5000])
def test_a_cursor_replay_can_be_capped_at_2000_rows(world: World, backlog: int) -> None:
    live(world, SID)
    initial = translated(world, [user(1, "cached")])
    cached = initial[-1]["seq"]
    daemon = world.daemon()
    world.write(
        [
            user(n, str(n), timestamp="2026-10-22T11:00:00.000Z")
            for n in range(2, backlog + 2)
        ]
    )
    for _ in range((backlog + 999) // 1000):
        daemon.cycle(cold=True)
    daemon.close()
    stored = world.rows()
    head = stored[-1]["seq"]
    assert head - cached == backlog
    connection = reader()
    try:
        cursor = cached
        if head - cached > 2000:
            start = show.replay_start(connection, "claude", SID, 2000, through_seq=head)
            assert start is not None
            cursor = start
        replay = []
        while page := show.rows_after(
            connection, "claude", SID, cursor, 200, through_seq=head
        ):
            replay.extend(page)
            cursor = page[-1]["seq"]
        expected = [row for row in stored if row["seq"] > cached][-2000:]
        assert replay == expected
        assert len(replay) == min(backlog, 2000)
    finally:
        connection.close()


def test_1_show_and_list_print_what_cut_1_printed(world: World) -> None:
    daemon = world.daemon()
    shutil.copyfile(FIXTURES / "claude-session.jsonl", world.source())
    # The golden's `modified` is the fixture's time, not the copy's.
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
    footer.pop("native_activity")
    assert "\n".join([*lines[:-1], show._dumps(footer)]) + "\n" == expected
    raw = [
        str(row[0])
        for row in world.table(
            "SELECT row FROM journal_row WHERE vendor = 'claude' AND session = ? "
            "ORDER BY seq",
            (SID,),
        )
    ]
    assert show.show(world.store, "claude", SID)[:-1] == raw
    total = json.loads(printed.splitlines()[-1])["total"]
    [listed] = show.list_sessions(world.store, None)
    session = world.table(
        "SELECT cwd, model, status, time FROM journal_session WHERE id = ?", (SID,)
    )[0]
    listed_row = json.loads(listed)
    assert "native_activity" not in listed_row
    assert show._dumps(listed_row) == show._dumps(
        {
            "vendor": "claude",
            "id": SID,
            "cwd": session[0],
            "model": session[1],
            "status": session[2],
            "time": session[3],
            "total": total,
            "parent": None,
            "source_home": str(world.claude),
            "source_home_refusal": None,
        }
    )
    assert show.list_sessions(world.store, "claude") == [listed]


def test_cli_list_keeps_parent_without_title_or_account(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    daemon = world.daemon(homes=[("work", world.claude)])
    world.write([user(1, "parent prompt")])
    sub = world.projects / SID / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-a77.jsonl").write_text(
        json.dumps({**user(2, "child prompt"), "isSidechain": True, "agentId": "a77"})
        + "\n"
    )
    daemon.cycle(cold=True)
    daemon.close()

    assert cli.main(["session", "list"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    listed = [json.loads(line) for line in captured.out.splitlines()]
    every = {row["id"]: row for row in listed}
    assert len(listed) == 2 and set(every) == {SID, f"{SID}.a77"}
    assert every[f"{SID}.a77"]["parent"] == SID
    assert every[SID]["parent"] is None
    for row in listed:
        assert "title" not in row and "account" not in row
        assert "native_activity" not in row
        assert set(row) == {
            "vendor",
            "id",
            "cwd",
            "model",
            "status",
            "time",
            "total",
            "parent",
            "source_home",
            "source_home_refusal",
        }


def test_cli_list_row_carries_the_session_row_source_home(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    translated(world, [user(1, "hello"), agent(2, "msg_1", text("one"), usage(1))])
    [head] = [row for row in world.rows() if row["type"] == "session"]
    assert head["source"]["home"] == str(world.claude)
    assert cli.main(["session", "list", "claude"]) == 0
    [listed] = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert listed["source_home"] == head["source"]["home"]
    assert listed["source_home_refusal"] is None
    # A `session` row without a source home lists null, never another home.
    edit(
        "UPDATE journal_row SET row = json_remove(row, '$.source') "
        "WHERE type = 'session'"
    )
    assert cli.main(["session", "list", "claude"]) == 0
    [listed] = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert "source_home" in listed and listed["source_home"] is None
    # The row is unique and names this session: no refusal.
    assert listed["source_home_refusal"] is None
    # The node host read keeps its keys.
    connection = reader()
    try:
        assert all(
            "source_home" not in row and "source_home_refusal" not in row
            for row in show.sessions(connection, None)
        )
    finally:
        connection.close()


@pytest.mark.parametrize("count", [0, 2])
def test_cli_list_refuses_the_source_home_without_one_session_row(
    world: World, capsys: pytest.CaptureFixture[str], count: int
) -> None:
    translated(world, [user(1, "hello"), agent(2, "msg_1", text("one"), usage(1))])
    if count == 0:
        edit("DELETE FROM journal_row WHERE type = 'session'")
    else:
        # The table key allows a second `session` row with another source line.
        # The first row still names the session's home.
        edit(
            "INSERT INTO journal_row (vendor, session, seq, type, time, src, mid, "
            "key, row) SELECT vendor, session, seq + 1000, type, time, src + 1000, "
            "mid, key, json_set(row, '$.seq', seq + 1000, '$.source.home', "
            "'/fixture/other') FROM journal_row WHERE type = 'session'"
        )
    assert len([r for r in world.rows() if r["type"] == "session"]) == count
    assert cli.main(["session", "list", "claude"]) == 0
    [listed] = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert listed["source_home"] is None
    assert listed["source_home_refusal"] == "session row is not unique"


@pytest.mark.parametrize(("field", "value"), [("id", OTHER), ("vendor", "codex")])
def test_cli_list_refuses_the_source_home_of_another_session_identity(
    world: World, capsys: pytest.CaptureFixture[str], field: str, value: str
) -> None:
    translated(world, [user(1, "hello")])
    edit(
        f"UPDATE journal_row SET row = json_set(row, '$.{field}', '{value}') "
        "WHERE type = 'session'"
    )
    [head] = [row for row in world.rows() if row["type"] == "session"]
    assert head[field] == value and head["source"]["home"] == str(world.claude)
    assert cli.main(["session", "list", "claude"]) == 0
    [listed] = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert listed["id"] == SID
    assert listed["source_home"] is None
    assert listed["source_home_refusal"] == "session identity does not match"


def test_1_the_reads_search_the_session_seq_index(world: World) -> None:
    translated(world, [user(1, "hello")])
    connection = reader()
    try:
        for sql in (
            show.ROWS_AFTER_SQL,
            show.REPLAY_START_SQL,
            show.TITLE_SQL,
            show.FORMAT_SQL,
        ):
            plan = " ".join(
                str(row[3])
                for row in nott_database.query(
                    connection,
                    f"EXPLAIN QUERY PLAN {sql}",
                    ("claude", SID, -1, 1)[: sql.count("?")],
                )
            )
            assert "SEARCH journal_row USING" in plan, (sql, plan)
            assert "(vendor=? AND session=?" in plan, (sql, plan)
            assert "SCAN journal_row" not in plan, (sql, plan)
        # The list reads two `session` rows to see that one is unique. Only the
        # partial index of `session` rows stops that read at the second row: a
        # search of the primary key reads every row of the session.
        plan = " ".join(
            str(row[3])
            for row in nott_database.query(
                connection,
                f"EXPLAIN QUERY PLAN {show.SESSION_ROWS_SQL}",
                ("claude", SID),
            )
        )
        assert (
            "SEARCH journal_row USING INDEX journal_session_row (vendor=? AND session=?)"
            in plan
        ), plan
        for sql, params in (
            (show.ROWS_BEFORE_SQL, ("claude", SID, 10, 2)),
            (show.BOUNDS_SQL, ("claude", SID, "claude", SID)),
            (show.ROWS_AFTER_THROUGH_SQL, ("claude", SID, 1, 10, 2)),
            (show.REPLAY_START_THROUGH_SQL, ("claude", SID, 10, 2)),
            (show.SESSION_ROWS_SQL, ("claude", SID)),
        ):
            plan = " ".join(
                str(row[3])
                for row in nott_database.query(
                    connection, f"EXPLAIN QUERY PLAN {sql}", params
                )
            )
            assert "SEARCH journal_row USING" in plan, (sql, plan)
            assert "(vendor=? AND session=?" in plan, (sql, plan)
            assert "SCAN journal_row" not in plan, (sql, plan)
            assert "TEMP B-TREE" not in plan, (sql, plan)
    finally:
        connection.close()


# -- 2. sessions ---------------------------------------------------------------------


def test_2_sessions_give_status_totals_and_title(world: World) -> None:
    long = "word " * 60
    live(world, SID)
    daemon = world.daemon()
    world.write([user(1, long), agent(2, "msg_1", text("hi"), usage(4, 9))])
    world.write([user(1, "short ask", sessionId=OTHER)], OTHER)
    daemon.cycle(cold=True)
    daemon.close()
    connection = reader()
    try:
        every = {row["id"]: row for row in show.sessions(connection, None)}
        assert set(every) == {SID, OTHER}
        first = every[SID]
        assert list(first) == [
            "vendor",
            "id",
            "cwd",
            "model",
            "status",
            "time",
            "total",
            "title",
            "parent",
            "account",
        ]
        assert first["status"] == "live" and first["vendor"] == "claude"
        assert first["title"] == long[:200] and len(first["title"]) == 200
        assert first["total"]["output"] == 4 and first["total"]["cache_read"] == 9
        assert first["total"]["calls"] == 1
        assert every[OTHER]["title"] == "short ask"
        assert every[OTHER]["status"] == "ended"
        assert every[OTHER]["total"]["calls"] == 0
        assert [row["id"] for row in show.sessions(connection, ["live"])] == [SID]
        assert [row["id"] for row in show.sessions(connection, ("ended",))] == [OTHER]
        assert show.sessions(connection, []) == []
    finally:
        connection.close()


@pytest.mark.parametrize(
    "home, run",
    [
        (".nott/state/nspawn/runs/native-run/claude", "native-run"),
        (".claude", None),
        (".nott/state/nspawn/runs/native-run/other", None),
        (".nott/state/nspawn/runs/native-run/claude/nested", None),
        (".nott/state/nspawn/runs/native-run", None),
        (".nott/state/nspawn/runs-extra/native-run/claude", None),
        ("elsewhere/.nott/state/nspawn/runs/native-run/claude", None),
        (".nott/state/nspawn/runs/../claude", None),
    ],
)
def test_run_home_identity_changes_only_list_and_show_run(
    world: World, home: str, run: str | None
) -> None:
    world.claude = world.home / home
    world.projects = world.claude / "projects" / PROJECT
    world.projects.mkdir(parents=True, exist_ok=True)
    daemon = world.daemon()
    try:
        world.write([user(1, "run work"), agent(2, "msg_1", text("done"), usage(4))])
        daemon.cycle(cold=True)
    finally:
        daemon.close()
    stored = [
        json.loads(row[0])
        for row in world.table("SELECT row FROM journal_row ORDER BY seq")
    ]
    listed = json.loads(show.list_sessions(world.store, "claude")[0])
    shown = [json.loads(line) for line in show.show(world.store, "claude", SID)]
    if run is None:
        assert "run" not in listed
        assert shown[:-1] == stored
    else:
        assert listed.pop("run") == run
        assert shown[:-1] == [{**row, "run": run} for row in stored]
        for row in shown[:-1]:
            validate(row)
    assert shown[-1]["total"] == listed["total"]
    assert set(shown[-1]) == {"total", "native_activity"}
    assert "native_activity" not in listed
    assert listed["source_home"] == str(world.claude)
    assert set(listed) == {
        "vendor",
        "id",
        "cwd",
        "model",
        "status",
        "time",
        "total",
        "parent",
        "source_home",
        "source_home_refusal",
    }
    connection = reader()
    try:
        assert show.rows_after(connection, "claude", SID, -1, 100) == stored
    finally:
        connection.close()


def test_run_home_keeps_real_subagent_unowned(world: World) -> None:
    world.claude = world.nott / "state/nspawn/runs/native-run/claude"
    world.projects = world.claude / "projects" / PROJECT
    sub = world.projects / "lead/subagents"
    sub.mkdir(parents=True)
    world.write([user(1, "lead work", sessionId="lead")], "lead")
    (sub / "agent-real.jsonl").write_text(
        json.dumps(
            user(2, "Agent work", sessionId="lead", isSidechain=True, agentId="real")
        )
        + "\n"
    )
    live(world, "lead")
    daemon = world.daemon()
    try:
        daemon.cycle(cold=True, batches=-1)
    finally:
        daemon.close()
    connection = reader()
    try:
        listed = {row["id"]: row for row in show.sessions(connection, None)}
        assert listed["lead"]["run"] == "native-run"
        assert listed["lead.real"]["parent"] == "lead"
        assert listed["lead.real"]["status"] == "live"
        assert "run" not in listed["lead.real"]
        stored = show.rows_after(connection, "claude", "lead.real", -1, 100)
    finally:
        connection.close()
    native_list = [
        json.loads(line) for line in show.list_sessions(world.store, "claude")
    ]
    assert "run" not in next(row for row in native_list if row["id"] == "lead.real")
    shown = [json.loads(line) for line in show.show(world.store, "claude", "lead.real")]
    assert shown[:-1] == stored
    assert shown[-1]["total"] == listed["lead.real"]["total"]
    assert "native_activity" in shown[-1]
    lead = [json.loads(line) for line in show.show(world.store, "claude", "lead")]
    assert all(row["run"] == "native-run" for row in lead[:-1])


@pytest.mark.parametrize(
    "link, target, run",
    [
        (".nott/state/nspawn/runs/not-a-run", "ordinary-account", None),
        ("run-alias", ".nott/state/nspawn/runs/native-run", "native-run"),
    ],
    ids=["symlink-outside-runs", "symlink-into-runs"],
)
def test_run_home_uses_resolved_location(
    world: World, link: str, target: str, run: str | None
) -> None:
    target_path = world.home / target
    target_path.mkdir(parents=True)
    link_path = world.home / link
    link_path.parent.mkdir(parents=True, exist_ok=True)
    link_path.symlink_to(target_path, target_is_directory=True)
    world.claude = link_path / "claude"
    world.projects = world.claude / "projects" / PROJECT
    world.projects.mkdir(parents=True)
    translated(world, [user(1, "work")])
    listed = json.loads(show.list_sessions(world.store, "claude")[0])
    assert listed.get("run") == run
    shown = [json.loads(line) for line in show.show(world.store, "claude", SID)]
    assert all(row.get("run") == run for row in shown[:-1])
    assert shown[-1]["total"] == listed["total"]
    assert set(shown[-1]) == {"total", "native_activity"}
    assert "native_activity" not in listed


@pytest.mark.parametrize("error", [OSError, RuntimeError])
def test_run_home_resolution_failure_keeps_rows(
    world: World, monkeypatch: pytest.MonkeyPatch, error: type[Exception]
) -> None:
    world.claude = world.nott / "state/nspawn/runs/native-run/claude"
    world.projects = world.claude / "projects" / PROJECT
    world.projects.mkdir(parents=True)
    translated(world, [user(1, "work")])
    stored = [
        json.loads(row[0])
        for row in world.table("SELECT row FROM journal_row ORDER BY seq")
    ]
    resolve = Path.resolve

    def unavailable(path: Path, strict: bool = False) -> Path:
        if path == world.claude:
            raise error("fixture resolution failure")
        return resolve(path, strict=strict)

    monkeypatch.setattr(Path, "resolve", unavailable)
    listed = json.loads(show.list_sessions(world.store, "claude")[0])
    assert "run" not in listed
    shown = [json.loads(line) for line in show.show(world.store, "claude", SID)]
    assert shown[:-1] == stored
    assert shown[-1]["total"] == listed["total"]
    assert set(shown[-1]) == {"total", "native_activity"}
    assert "native_activity" not in listed


def test_2_a_status_read_totals_only_the_sessions_it_returns(world: World) -> None:
    """The node host reads the live and waiting sessions on each journal change:
    their usage totals search the usage index for those sessions alone, never
    scan the usage rows of every session."""
    live(world, SID)
    daemon = world.daemon()
    world.write([user(1, "live ask"), agent(2, "msg_1", text("hi"), usage(4, 9))])
    world.write(
        [
            user(1, "old ask", sessionId=OTHER),
            agent(2, "msg_2", text("yo"), usage(5, 1), sessionId=OTHER),
        ],
        OTHER,
    )
    daemon.cycle(cold=True)
    daemon.close()
    connection = reader()
    executed: list[str] = []
    try:
        connection.set_trace_callback(executed.append)
        try:
            [only] = show.sessions(connection, ("live", "waiting"))
        finally:
            connection.set_trace_callback(None)
        assert only["id"] == SID and only["total"]["calls"] == 1
        assert only["total"]["output"] == 4
        reads = [sql for sql in executed if "FROM journal_usage" in sql]
        assert reads
        for sql in reads:
            plan = [
                str(row[3])
                for row in connection.execute(f"EXPLAIN QUERY PLAN {sql}").fetchall()
            ]
            assert not any("SCAN journal_usage" in step for step in plan), (sql, plan)
            assert any("SEARCH journal_usage" in step for step in plan), (sql, plan)
    finally:
        connection.close()


def test_2_sessions_give_the_parent_and_the_account(world: World) -> None:
    """A subagent session names its parent session, and a session of a roster
    account names the account. `list` omits the title and account."""
    account = world.root / "account-home"
    (account / "projects" / PROJECT).mkdir(parents=True, mode=0o700)
    daemon = world.daemon(homes=[(None, world.claude), ("work", account)])
    world.write([user(1, "parent prompt")])
    sub = world.projects / SID / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-a77.jsonl").write_text(
        json.dumps({**user(2, "sub prompt"), "isSidechain": True, "agentId": "a77"})
        + "\n"
    )
    (account / "projects" / PROJECT / f"{OTHER}.jsonl").write_text(
        json.dumps(user(3, "account prompt", sessionId=OTHER)) + "\n"
    )
    daemon.cycle(cold=True)
    daemon.close()
    connection = reader()
    try:
        every = {row["id"]: row for row in show.sessions(connection, None)}
    finally:
        connection.close()
    assert set(every) == {SID, f"{SID}.a77", OTHER}
    assert every[f"{SID}.a77"]["parent"] == SID
    assert every[SID]["parent"] is None and every[OTHER]["parent"] is None
    assert every[OTHER]["account"] == "work"
    assert every[SID]["account"] is None
    for line in show.list_sessions(world.store, None):
        assert set(json.loads(line)) == {
            "vendor",
            "id",
            "cwd",
            "model",
            "status",
            "time",
            "total",
            "parent",
            "source_home",
            "source_home_refusal",
        }


def test_2_a_session_with_no_owner_text_has_no_title(world: World) -> None:
    translated(world, [user(1, [image(PNG)])])
    connection = reader()
    try:
        [only] = show.sessions(connection, None)
        assert only["title"] is None
    finally:
        connection.close()


def test_2_a_write_on_the_reader_fails_and_changes_nothing(world: World) -> None:
    translated(world, [user(1, "hello")])
    before = world.table("SELECT count(*) FROM journal_row")[0][0]
    connection = reader()
    try:
        for sql in (
            "DELETE FROM journal_row",
            "UPDATE journal_session SET status = 'failed'",
            "CREATE TABLE intruder (x)",
        ):
            with pytest.raises((nott_database.Error, nott_database.DatabaseError)):
                nott_database.query(connection, sql)
            with pytest.raises(nott_database.Error):
                connection.execute(sql)
    finally:
        connection.close()
    assert world.table("SELECT count(*) FROM journal_row")[0][0] == before
    assert world.table("SELECT status FROM journal_session")[0][0] == "ended"


# -- 3. refusals and an older store ------------------------------------------------------


def test_3_an_unknown_format_is_refused_by_name(world: World) -> None:
    translated(world, [user(1, "hello")])
    edit(
        "UPDATE journal_row SET row = json_set(row, '$.format', 2) "
        "WHERE type = 'session'"
    )
    connection = reader()
    try:
        with pytest.raises(show.JournalFormatRefused, match="format 2"):
            show.sessions(connection, None)
        with pytest.raises(show.JournalFormatRefused, match="format 2"):
            show.rows_after(connection, "claude", SID, -1, 10)
        with pytest.raises(show.JournalFormatRefused, match="format 2"):
            show.rows_after(connection, "claude", SID, 0, 10)
        with pytest.raises(show.JournalFormatRefused, match="format 2"):
            show.replay_start(connection, "claude", SID, 10)
        with pytest.raises(show.JournalFormatRefused, match="format 2"):
            show.rows_before(connection, "claude", SID, 10, 10)
        with pytest.raises(show.JournalFormatRefused, match="format 2"):
            show.bounds(connection, "claude", SID)
        with pytest.raises(show.JournalFormatRefused, match="format 2"):
            show.rows_after(connection, "claude", SID, 0, 10, through_seq=10)
        with pytest.raises(show.JournalFormatRefused, match="format 2"):
            show.replay_start(connection, "claude", SID, 10, through_seq=10)
    finally:
        connection.close()
    with pytest.raises(show.JournalFormatRefused):
        show.show(world.store, "claude", SID)


def test_3_the_cli_exits_2_on_an_unknown_format(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    from nspawn import cli

    translated(world, [user(1, "hello")])
    edit(
        "UPDATE journal_row SET row = json_set(row, '$.format', 2) "
        "WHERE type = 'session'"
    )
    assert cli.cmd_session_show(["claude", SID]) == 2
    assert cli.cmd_session_list([]) == 2
    assert "format 2" in capsys.readouterr().err


def test_3_a_registry_without_journal_tables_gives_no_rows(world: World) -> None:
    world.store  # noqa: B018 - the store exists
    edit("DROP TABLE IF EXISTS journal_row")
    edit("DROP TABLE IF EXISTS journal_usage")
    edit("DROP TABLE IF EXISTS journal_session")
    edit("DROP TABLE IF EXISTS journal_source")
    connection = reader()
    try:
        assert show.sessions(connection, None) == []
        assert show.sessions(connection, ["live"]) == []
        assert show.rows_after(connection, "claude", SID, -1, 10) == []
        assert show.replay_start(connection, "claude", SID, 10) is None
        assert show.rows_before(connection, "claude", SID, 10, 10) == []
        assert show.bounds(connection, "claude", SID) is None
        assert show.rows_after(connection, "claude", SID, -1, 10, through_seq=10) == []
        assert show.replay_start(connection, "claude", SID, 10, through_seq=10) is None
        assert isinstance(show.data_version(connection), int)
    finally:
        connection.close()
    assert show.list_sessions(world.store, None) == []


def test_3_no_store_gives_no_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir(mode=0o700)
    monkeypatch.setenv("NOTT_DATABASE_DIR", str(empty))
    assert show.open_readonly() is None
    assert not (empty / store_path().name).exists()


# -- 4. data_version ---------------------------------------------------------------------


def test_4_data_version_moves_on_a_daemon_commit_only(world: World) -> None:
    translated(world, [user(1, "hello")])
    connection = reader()
    try:
        first = show.data_version(connection)
        show.sessions(connection, None)
        show.rows_after(connection, "claude", SID, -1, 10)
        assert show.data_version(connection) == first
        daemon = world.daemon()
        world.write([agent(2, "msg_1", text("hi"), usage(1))])
        daemon.cycle(cold=True)
        second = show.data_version(connection)
        assert second != first
        assert show.rows_after(connection, "claude", SID, 0, 10)[0]["type"] == "message"
        daemon.cycle(cold=True)
        daemon.close()
        third = show.data_version(connection)
        assert show.data_version(connection) == third
    finally:
        connection.close()


# -- media ----------------------------------------------------------------------------------


def stored_image(world: World) -> str:
    rows = translated(world, [user(1, [text("look"), image(PNG)])])
    name: str = rows[1]["media"][0]["file"]
    return name


def test_media_gives_the_bytes_of_a_stored_image(world: World) -> None:
    name = stored_image(world)
    assert name == f"{hashlib.sha256(PNG).hexdigest()}.png"
    assert show.media(name) == PNG


def test_media_refuses_a_name_outside_the_media_form(world: World) -> None:
    name = stored_image(world)
    digest = name.split(".")[0]
    secret = world.home / "secret.png"
    secret.write_bytes(PNG)
    (world.nott / "sessions" / "media" / "note.png").write_bytes(PNG)
    for bad in (
        "",
        "note.png",
        f"{digest}.svg",
        f"{digest}.PNG",
        f"{digest.upper()}.png",
        f"{digest[:-1]}.png",
        f"{digest}.png.txt",
        f"{digest}.png/",
        f".{name}",
        f"./{name}",
        f"../media/{name}",
        "../../secret.png",
        str(secret),
        f"/{name}",
        f"{name}\x00",
        f"{name}\n",
    ):
        assert show.media(bad) is None, bad


def test_media_refuses_a_symlink_a_missing_file_and_a_non_file(
    world: World,
) -> None:
    name = stored_image(world)
    folder = world.nott / "sessions" / "media"
    outside = world.home / "outside.png"
    outside.write_bytes(b"outside bytes")
    linked = f"{'a' * 64}.png"
    (folder / linked).symlink_to(outside)
    assert show.media(linked) is None
    assert show.media(f"{'b' * 64}.png") is None
    os.mkfifo(folder / f"{'c' * 64}.png")
    assert show.media(f"{'c' * 64}.png") is None
    (folder / f"{'d' * 64}.gif").mkdir()
    assert show.media(f"{'d' * 64}.gif") is None
    assert show.media(name) == PNG


def test_media_refuses_a_media_directory_that_is_a_symlink(world: World) -> None:
    name = stored_image(world)
    folder = world.nott / "sessions" / "media"
    moved = world.home / "elsewhere"
    folder.rename(moved)
    folder.symlink_to(moved)
    assert (moved / name).read_bytes() == PNG
    assert show.media(name) is None


def test_session_list_since_filters_before_totals_and_format_checks(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from nspawn import cli

    translated(world, [user(1, "hello"), agent(2, "msg_1", text("one"), usage(1))])
    edit("UPDATE journal_session SET time = '2026-10-25T12:00:00Z'")
    seen: list[list[tuple[str, str]]] = []
    original = show._session_totals

    def totals(
        connection: nott_database.Connection, keys: list[tuple[str, str]]
    ) -> dict[tuple[str, str], dict[str, object]]:
        seen.append(list(keys))
        return original(connection, keys)

    monkeypatch.setattr(show, "_session_totals", totals)
    assert cli.main(["session", "list", "claude", "--since", "2026-10-23T23:00"]) == 0
    assert json.loads(capsys.readouterr().out)["id"] == SID
    assert seen == [[("claude", SID)]]
    seen.clear()
    edit("UPDATE journal_session SET time = '2026-10-23T22:59:59Z'")
    assert cli.main(["session", "list", "claude", "--since", "2026-10-23T23:00"]) == 0
    assert capsys.readouterr().out == ""
    assert seen == [[]]


def test_cli_list_rows_carry_no_native_activity(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    path = world.source()
    translated(world, [user(1, "one"), user(2, "two"), user(4, "three")])
    assert cli.main(["session", "list", "claude"]) == 0
    listed = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [row["id"] for row in listed] == [SID]
    assert all("native_activity" not in row for row in listed)
    assert cli.main(["session", "show", "claude", SID]) == 0
    shown = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert shown[-1]["native_activity"]["size"] == path.stat().st_size


def test_cli_list_reads_no_activity_source(
    world: World, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    translated(world, [user(1, "one"), user(2, "two"), user(4, "three")])

    def refused(*args: object, **kwargs: object) -> None:
        raise AssertionError("the list read the native activity")

    monkeypatch.setattr(show, "_activity", refused)
    monkeypatch.setattr(show, "observation", refused)
    assert cli.main(["session", "list", "claude"]) == 0
    assert [
        json.loads(line)["id"] for line in capsys.readouterr().out.splitlines()
    ] == [SID]


def five_sessions(world: World) -> str:
    """`SID` and its copy in a second home, its subagent `SID.a77`, and the
    other sessions `SID-x` and `OTHER`. The copy has no `vendor_id`, as for a
    copy whose metadata the writer has not written. Gives the copy's id."""
    second = world.root / "second-home"
    (second / "projects" / PROJECT).mkdir(parents=True, mode=0o700)
    daemon = world.daemon(homes=[(None, world.claude), ("work", second)])
    try:
        world.write([user(1, "lead")])
        sub = world.projects / SID / "subagents"
        sub.mkdir(parents=True)
        (sub / "agent-a77.jsonl").write_text(
            json.dumps({**user(2, "sub"), "isSidechain": True, "agentId": "a77"}) + "\n"
        )
        (second / "projects" / PROJECT / f"{SID}.jsonl").write_text(
            json.dumps(user(3, "copy")) + "\n"
        )
        world.write([user(4, "prefix", sessionId=f"{SID}-x")], f"{SID}-x")
        world.write([user(5, "other", sessionId=OTHER)], OTHER)
        daemon.cycle(cold=True)
    finally:
        daemon.close()
    ids = {str(row[0]) for row in world.table("SELECT id FROM journal_session")}
    [copy] = [
        ident
        for ident in ids
        if len(ident) == len(SID) + 17
        and ident.startswith(f"{SID}.")
        and all(c in "0123456789abcdef" for c in ident[len(SID) + 1 :])
    ]
    assert ids == {SID, copy, f"{SID}.a77", f"{SID}-x", OTHER}
    edit(f"UPDATE journal_session SET vendor_id = NULL WHERE id = '{copy}'")
    assert world.table(
        "SELECT vendor_id FROM journal_session WHERE id = ?", (copy,)
    ) == [(None,)]
    return copy


def listed_ids(capsys: pytest.CaptureFixture[str], *arguments: str) -> list[str]:
    assert cli.main(["session", "list", *arguments]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    return [json.loads(line)["id"] for line in captured.out.splitlines()]


def test_cli_list_id_keeps_the_session_its_copies_and_subagents(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    copy = five_sessions(world)
    every = listed_ids(capsys, "claude")
    assert len(every) == 5

    def newest_first(*wanted: str) -> list[str]:
        return [ident for ident in every if ident in wanted]

    family = (SID, copy, f"{SID}.a77")
    assert listed_ids(capsys, "claude", "--id", SID) == newest_first(*family)
    assert listed_ids(capsys, "claude", "--id", SID, "--id", OTHER) == newest_first(
        *family, OTHER
    )
    assert listed_ids(
        capsys, "claude", "--id", SID, "--id", f"{SID}.a77"
    ) == newest_first(*family)
    assert (
        listed_ids(capsys, "claude", "--id", SID, "--since", "2099-01-01T00:00") == []
    )


def test_the_id_filter_searches_the_session_key(world: World) -> None:
    translated(world, [user(1, "one"), user(2, "two"), user(4, "three")])
    connection = reader()
    executed: list[str] = []
    try:
        plans = [
            " ".join(
                str(row[3])
                for row in nott_database.query(
                    connection,
                    f"EXPLAIN QUERY PLAN {show.IDS_SQL}",
                    (json.dumps([SID]), "claude"),
                )
            )
        ]
        assert "SEARCH s USING" in plans[0], plans[0]
        assert "(vendor=? AND id>? AND id<?)" in plans[0], plans[0]
        connection.set_trace_callback(executed.append)
        try:
            [only] = show.sessions(connection, None, vendor="claude", ids=[SID])
        finally:
            connection.set_trace_callback(None)
        assert only["id"] == SID
        [listing] = [sql for sql in executed if "json_each" in sql]
        plans.append(
            " ".join(
                str(row[3])
                for row in connection.execute(
                    f"EXPLAIN QUERY PLAN {listing}"
                ).fetchall()
            )
        )
        for plan in plans:
            assert "SCAN s " not in f"{plan} ", plan
            assert "SCAN journal_session" not in plan, plan
        # The filter composes with the other filters by AND.
        native = ("claude", SID)
        for statuses, vendor_id, ids, expected in (
            ([only["status"]], native, [SID], [SID]),
            (["no-such-status"], None, [SID], []),
            (None, native, [OTHER], []),
        ):
            assert [
                row["id"]
                for row in show.sessions(
                    connection, statuses, vendor_id, vendor="claude", ids=ids
                )
            ] == expected
        with pytest.raises(ValueError, match="vendor"):
            show.sessions(connection, None, ids=[SID])
    finally:
        connection.close()


@pytest.mark.parametrize("case", ["rows", "empty", "ids"])
def test_cli_list_count_line_ends_the_reply(
    world: World, capsys: pytest.CaptureFixture[str], case: str
) -> None:
    arguments = ["session", "list", "claude", "--count"]
    if case == "rows":
        translated(world, [user(1, "one"), user(2, "two"), user(4, "three")])
    elif case == "empty":
        world.daemon().close()
        connection = reader()
        try:
            assert show._journal(connection)
        finally:
            connection.close()
    else:
        five_sessions(world)
        arguments[3:3] = ["--id", SID]
    assert cli.main(arguments) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.endswith("\n")
    *rows, last = captured.out.splitlines()
    assert last == json.dumps({"count": len(rows)}, separators=(",", ":"))
    assert all("count" not in json.loads(row) for row in rows)
    assert len(rows) == {"rows": 1, "empty": 0, "ids": 3}[case]
    if case == "empty":
        assert captured.out == '{"count":0}\n'


@pytest.mark.parametrize(
    "arguments",
    [
        ["--id", SID],
        ["claude", "--id", "../bad"],
        ["claude", "--id"],
        ["claude", "--since", "2026-10-23T23:00", "--since", "2026-10-24T00:00"],
        ["claude", "--count", "--count"],
    ],
    ids=["no-vendor", "bad-id", "no-value", "two-since", "two-count"],
)
def test_cli_list_id_refuses_a_bad_form(
    world: World, capsys: pytest.CaptureFixture[str], arguments: list[str]
) -> None:
    translated(world, [user(1, "one")])
    assert cli.main(["session", "list", *arguments]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("nspawn: usage: session list ")


def test_cli_list_without_count_prints_only_rows(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    five_sessions(world)
    for arguments in ([], ["claude"], ["claude", "--since", "2026-01-01T00:00"]):
        assert cli.main(["session", "list", *arguments]) == 0
        lines = capsys.readouterr().out.splitlines()
        assert lines
        for line in lines:
            row = json.loads(line)
            assert "count" not in row
            assert {"vendor", "id", "total"} <= set(row)


@pytest.mark.parametrize("verb", ["list", "show"])
def test_run_home_native_activity_through_cli(
    world: World,
    capsys: pytest.CaptureFixture[str],
    verb: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nspawn.registry import Registry
    from nspawn.rows import Backend, RunPhase, RunRow

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(world.home / ".claude"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(world.root / "runtime"))
    world.claude = world.nott / "state/nspawn/runs/gpt-run/claude"
    world.projects = world.claude / "projects" / PROJECT
    world.projects.mkdir(parents=True)
    path = world.write(
        [
            user(1, "owner"),
            {
                "type": "file-history-snapshot",
                "snapshot": {"timestamp": user(4, "")["timestamp"]},
            },
            user(10, "again"),
        ]
    )
    with Registry(str(world.store), "fixture") as registry:
        registry.open(now=1)
        registry.admit_run(
            RunRow(
                run_id="gpt-run",
                thread_id=SID,
                worktree="/work/demo",
                backend=Backend.CLAUDE,
                phase=RunPhase.PENDING,
                sandbox="read-only",
                model="fixture",
                owner_instance="fixture",
                created_at=1,
                updated_at=1,
                requested_provider="gpt",
            ),
            False,
            1,
            1000,
            journal_context={"home": str(world.claude)},
            effort_profile={"requested": "high", "sent": "high"},
        )
    daemon = world.daemon(homes=None)
    try:
        daemon.cycle(cold=True)
    finally:
        daemon.close()
    args = ["session", verb, "claude"] + ([SID] if verb == "show" else [])
    assert cli.main(args) == 0
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert rows[0]["run"] == "gpt-run"
    if verb == "list":
        assert "native_activity" not in rows[0]
        return
    activity = rows[-1]["native_activity"]
    assert activity["mtime_ns"] == path.stat().st_mtime_ns
    assert activity["size"] == path.stat().st_size
    assert activity["max_gap_seconds"] == 6
    assert [gap["seconds"] for gap in activity["gaps"]] == [3, 6]


def activity_cli(capsys: pytest.CaptureFixture[str], verb: str) -> dict[str, object]:
    """The show footer's activity. In `list`, the list rows first have none."""
    if verb == "list":
        assert cli.main(["session", "list", "claude"]) == 0
        listed = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        assert listed and all("native_activity" not in row for row in listed)
    assert cli.main(["session", "show", "claude", SID]) == 0
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    return rows[-1]["native_activity"]


def activity_home(world: World) -> None:
    world.claude = world.nott / "state/nspawn/runs/gpt-run/claude"
    world.projects = world.claude / "projects" / PROJECT
    world.projects.mkdir(parents=True)


@pytest.mark.parametrize("verb", ["list", "show"])
def test_activity_cli_refuses_replaced_shorter_source(
    world: World, capsys: pytest.CaptureFixture[str], verb: str
) -> None:
    activity_home(world)
    path = world.write([user(1, "owner"), user(10, "original thread")])
    daemon = world.daemon()
    try:
        daemon.cycle(cold=True)
        replacement = path.with_suffix(".replacement")
        replacement.write_text(
            json.dumps(user(30, "other thread", sessionId=OTHER)) + "\n"
        )
        assert replacement.stat().st_size < path.stat().st_size
        replacement.replace(path)
        daemon.cycle(cold=True)
        assert world.table("SELECT status, reason FROM journal_source") == [
            ("failed", "source shrank")
        ]
        result = activity_cli(capsys, verb)
        assert result == {"reason": "source shrank"}
    finally:
        daemon.close()


@pytest.mark.parametrize("verb", ["list", "show"])
def test_activity_cli_uses_fixture_snapshot_clock(
    world: World, capsys: pytest.CaptureFixture[str], verb: str
) -> None:
    activity_home(world)
    snapshot = json.loads(
        (FIXTURES / "claude-session.jsonl").read_text().splitlines()[1]
    )
    snapshot["snapshot"]["timestamp"] = user(4, "")["timestamp"]
    assert "timestamp" not in snapshot
    world.write([user(1, "owner"), snapshot, user(10, "again")])
    daemon = world.daemon(batch_lines=1)
    try:
        for _ in range(3):
            daemon.cycle(cold=True)
        result = activity_cli(capsys, verb)
        assert result["max_gap_seconds"] == 6
        assert [gap["seconds"] for gap in result["gaps"]] == [3, 6]
    finally:
        daemon.close()


@pytest.mark.parametrize("verb", ["list", "show"])
def test_activity_cli_large_bookkeeping_source_has_bounded_read(
    world: World,
    capsys: pytest.CaptureFixture[str],
    verb: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    activity_home(world)
    path = world.write([user(1, "owner")])
    snapshot = json.loads(
        (FIXTURES / "claude-session.jsonl").read_text().splitlines()[1]
    )
    duration = next(
        row
        for row in map(
            json.loads, (FIXTURES / "claude-session.jsonl").read_text().splitlines()
        )
        if row.get("subtype") == "turn_duration"
    )
    with path.open("a") as stream:
        for second in range(2, 20002):
            record = snapshot if second % 2 else duration
            clock = record["snapshot"] if record is snapshot else record
            clock["timestamp"] = datetime.fromtimestamp(
                NOW - 7200 + second, UTC
            ).isoformat()
            stream.write(json.dumps(record) + "\n")
    daemon = world.daemon()
    try:
        daemon.cycle(cold=True)
        for _ in range(25):
            daemon.cycle(cold=False)
        assert (
            world.table("SELECT consumed FROM journal_source")[0][0]
            == path.stat().st_size
        )
        opened: list[str] = []
        statted: list[str] = []
        original = os.open
        original_stat = os.stat

        def stat_audit(file, *args, **kwargs):
            if str(file) == str(path):
                statted.append(str(file))
            return original_stat(file, *args, **kwargs)

        def audit(file, *args, **kwargs):
            if str(file).endswith(".jsonl"):
                opened.append(str(file))
            return original(file, *args, **kwargs)

        monkeypatch.setattr(os, "open", audit)
        monkeypatch.setattr(os, "stat", stat_audit)
        result = activity_cli(capsys, verb)
        assert statted == [str(path)]
        assert (opened, len(result["gaps"]) <= 256) == ([], True)
        assert len(result["gaps"]) == 256
        assert result["max_gap_seconds"] == 1
        assert result["size"] == path.stat().st_size
    finally:
        daemon.close()


@pytest.mark.parametrize("verb", ["list", "show"])
@pytest.mark.parametrize("retained", ["missing", "mismatch", "rebuild"])
def test_activity_cli_backfill_rejects_unretained_replacement(
    world: World, capsys: pytest.CaptureFixture[str], verb: str, retained: str
) -> None:
    activity_home(world)
    path = world.write([user(1, "owner"), user(4, "later")])
    daemon = world.daemon()
    try:
        daemon.cycle(cold=True)
        if retained == "missing":
            edit("UPDATE journal_source SET activity = NULL")
        elif retained == "mismatch":
            edit("UPDATE journal_source SET metadata_version = 0")
        else:
            edit("UPDATE journal_source SET activity = json_set(activity, '$.size', 0)")
    finally:
        daemon.close()
    replacement = path.with_suffix(".replacement")
    replacement.write_bytes(path.read_bytes().replace(b"owner", b"other"))
    replacement.replace(path)
    daemon = world.daemon()
    try:
        for _ in range(3):
            daemon.cycle(cold=True)
        assert set(activity_cli(capsys, verb)) == {"reason"}
    finally:
        daemon.close()


@pytest.mark.parametrize("verb", ["list", "show"])
@pytest.mark.parametrize("reason", ["secrets", "disk", "secrets accumulation"])
def test_activity_cli_session_failure_hides_facts(
    world: World, capsys: pytest.CaptureFixture[str], verb: str, reason: str
) -> None:
    activity_home(world)
    world.write([user(1, "owner")])
    daemon = world.daemon()
    try:
        daemon.cycle(cold=True)
        assert "size" in activity_cli(capsys, verb)
        source = next(iter(daemon.sources.values()))
        daemon._status(source, "failed", reason)
        assert world.table("SELECT status FROM journal_source") == [(None,)]
        assert set(activity_cli(capsys, verb)) == {"reason"}
    finally:
        daemon.close()


@pytest.mark.parametrize("verb", ["list", "show"])
@pytest.mark.parametrize("change", ["overwrite", "unreadable", "writable-parent"])
def test_activity_cli_no_growth_invalidates_facts(
    world: World, capsys: pytest.CaptureFixture[str], verb: str, change: str
) -> None:
    activity_home(world)
    path = world.write([user(1, "owner")])
    daemon = world.daemon()
    try:
        daemon.cycle(cold=True)
        assert "size" in activity_cli(capsys, verb)
        before = path.stat()
        if change == "overwrite":
            path.write_bytes(path.read_bytes().replace(b"owner", b"other"))
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 1000000))
        elif change == "unreadable":
            path.chmod(0)
        else:
            path.parent.chmod(0o770)
        assert path.stat().st_size == before.st_size
        # Export must fence the path even before the next import cycle.
        assert set(activity_cli(capsys, verb)) == {"reason"}
        daemon.cycle(cold=True)
        assert set(activity_cli(capsys, verb)) == {"reason"}
    finally:
        path.chmod(0o600)
        path.parent.chmod(0o700)
        daemon.close()


@pytest.mark.parametrize("verb", ["list", "show"])
@pytest.mark.parametrize("bad", ["not-json", '{"timestamp":"invalid"}'])
def test_activity_cli_bad_record_does_not_stop_translation(
    world: World, capsys: pytest.CaptureFixture[str], verb: str, bad: str
) -> None:
    activity_home(world)
    path = world.write(
        json.dumps(user(1, "owner"))
        + "\n"
        + bad
        + "\n"
        + json.dumps(user(4, "later"))
        + "\n"
    )
    daemon = world.daemon(batch_lines=1)
    try:
        for _ in range(5):
            daemon.cycle(cold=True)
        assert set(activity_cli(capsys, verb)) == {"reason"}
        assert world.table("SELECT status, consumed FROM journal_source") == [
            (None, path.stat().st_size)
        ]
        assert len([row for row in world.rows() if row["type"] == "message"]) == 2
        if bad == "not-json":
            assert daemon.unknown["malformed"] == 1
    finally:
        daemon.close()


@pytest.mark.parametrize("verb", ["list", "show"])
@pytest.mark.parametrize("change", ["missing", "symlink", "hardlink", "replace"])
def test_activity_cli_export_fences_before_next_import(
    world: World, capsys: pytest.CaptureFixture[str], verb: str, change: str
) -> None:
    activity_home(world)
    path = world.write([user(1, "owner")])
    daemon = world.daemon()
    try:
        daemon.cycle(cold=True)
        before = path.stat()
        if change == "missing":
            path.unlink()
        elif change == "symlink":
            target = path.with_suffix(".saved")
            path.rename(target)
            path.symlink_to(target)
        elif change == "hardlink":
            os.link(path, path.with_suffix(".linked"))
        else:
            replacement = path.with_suffix(".replacement")
            replacement.write_bytes(path.read_bytes())
            os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
            replacement.replace(path)
        assert set(activity_cli(capsys, verb)) == {"reason"}
    finally:
        daemon.close()


@pytest.mark.parametrize("verb", ["list", "show"])
def test_activity_cli_bad_record_during_rebuild_keeps_translation(
    world: World, capsys: pytest.CaptureFixture[str], verb: str
) -> None:
    activity_home(world)
    path = world.write([user(1, "owner"), user(4, "later")])
    daemon = world.daemon()
    try:
        daemon.cycle(cold=True)
        # A prior release left a stale activity cursor on the same source.
        edit("UPDATE journal_source SET activity = json_set(activity, '$.size', 0)")
    finally:
        daemon.close()
    content = path.read_bytes()
    path.write_bytes(content.replace(b'"owner"', b"!broken", 1))
    daemon = world.daemon(batch_lines=1)
    try:
        for _ in range(4):
            daemon.cycle(cold=True)
        assert set(activity_cli(capsys, verb)) == {"reason"}
        assert world.table("SELECT status, metadata_version FROM journal_source") == [
            (None, 1)
        ]
        world.write([user(8, "after rebuild")])
        daemon.cycle(cold=True)
        assert world.table("SELECT status, consumed FROM journal_source") == [
            (None, path.stat().st_size)
        ]
        assert len([row for row in world.rows() if row["type"] == "message"]) == 3
        assert set(activity_cli(capsys, verb)) == {"reason"}
    finally:
        daemon.close()
