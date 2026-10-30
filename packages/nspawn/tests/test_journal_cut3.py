"""Cut 3 uses invented vendor records and private stores."""

from __future__ import annotations

import errno
import json
import os
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self

import nott_database
import pytest
from test_journal import (
    NOW,
    SID,
    World,
    agent,
    attachment,
    drain,
    result,
    run,
    text,
    usage,
    user,
    validate,
)

from nspawn import cli
from nspawn.journal import TRANSLATORS, Context, serve
from nspawn.registry import Registry
from nspawn.rows import Backend, RunPhase, RunRow


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def test_start_imports_old_files_and_recovers_downtime(world: World) -> None:
    world.write([user(1, "old prompt")])
    daemon = world.daemon(batch_lines=1)
    try:
        daemon.cycle(cold=True)
        assert any(row.get("text") == "old prompt" for row in world.rows())
    finally:
        daemon.close()
    world.write(
        [agent(2, "m1", text("downtime answer"), usage(3), stop_reason="end_turn")]
    )
    daemon = world.daemon()
    try:
        daemon.cycle(cold=True)
        assert sum(row.get("text") == "old prompt" for row in world.rows()) == 1
        assert any(row.get("text") == "downtime answer" for row in world.rows())
        assert world.table("SELECT import_state FROM journal_session")[0][0] == "done"
    finally:
        daemon.close()


def test_native_links_include_dropped_records_and_compaction(world: World) -> None:
    records = [
        user(1, "root"),
        {
            "type": "attachment",
            "uuid": "attachment",
            "parentUuid": "x1",
            "attachment": {"type": "unknown", "content": "do not store"},
        },
        {
            "type": "system",
            "subtype": "informational",
            "uuid": "system",
            "parentUuid": "attachment",
            "content": "do not store",
        },
        {
            "type": "system",
            "subtype": "compact_boundary",
            "uuid": "compact",
            "parentUuid": None,
            "logicalParentUuid": "system",
        },
        user(2, "after compact", parentUuid="compact"),
    ]
    world.write(records)
    daemon = world.daemon()
    try:
        daemon.cycle(cold=True)
        links = world.table(
            "SELECT uuid, parent_uuid, logical_parent_uuid FROM journal_link ORDER BY src"
        )
        assert [tuple(link) for link in links] == [
            ("x1", None, None),
            ("attachment", "x1", None),
            ("system", "attachment", None),
            ("compact", None, "system"),
            ("x2", "compact", None),
        ]
        assert "do not store" not in json.dumps(world.rows())
        rows = [row for row in world.rows() if row["type"] == "message"]
        assert rows[0]["uuid"] == "x1" and rows[0]["parentUuid"] is None
    finally:
        daemon.close()


def test_home_copies_keep_native_id_and_stable_identity(world: World) -> None:
    other = world.root / "copy"
    path = other / "projects/demo" / world.source().name
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(user(2, "copy")) + "\n")
    world.write([user(1, "original")])
    homes = [(None, world.claude), (None, other)]
    daemon = world.daemon(homes=homes)
    try:
        drain(daemon)
        ids = world.table("SELECT id, vendor_id, home FROM journal_session")
        assert len(ids) == 2
        assert len({r[0] for r in ids}) == 2
        assert len({r[1] for r in ids}) == 1
        before = {r[2]: r[0] for r in ids}
    finally:
        daemon.close()
    daemon = world.daemon(homes=list(reversed(homes)))
    try:
        drain(daemon)
        assert {
            r[2]: r[0]
            for r in world.table("SELECT id, vendor_id, home FROM journal_session")
        } == before
    finally:
        daemon.close()


def test_discovery_metadata_preserves_titles_and_mtime(world: World) -> None:
    path = world.write(
        [
            user(1, "first prompt", entrypoint="sdk-cli"),
            {"type": "summary", "summary": "latest summary", "leafUuid": "x1"},
            {"type": "custom-title", "customTitle": "custom title"},
        ]
    )
    os.utime(path, (1000, 1000))
    daemon = world.daemon()
    try:
        daemon.cycle(cold=True)
        row = world.table(
            "SELECT entrypoint, summary, title, modified FROM journal_session"
        )[0]
        assert tuple(row) == ("sdk-cli", "latest summary", "custom title", 1000000)
    finally:
        daemon.close()


def test_exit_during_open_turn_is_failed(world: World) -> None:
    sessions = world.claude / "sessions"
    sessions.mkdir()
    ticks = Path(f"/proc/{os.getpid()}/stat").read_text()
    start = ticks[ticks.rindex(")") + 2 :].split()[19]
    pid = sessions / f"{os.getpid()}.json"
    pid.write_text(
        json.dumps(
            {"pid": os.getpid(), "sessionId": world.source().stem, "procStart": start}
        )
    )
    world.write([user(1, "unfinished")])
    daemon = world.daemon()
    try:
        daemon.cycle(cold=True)
        assert world.table("SELECT status FROM journal_session")[0][0] == "live"
        pid.unlink()
        daemon.cycle(cold=True)
        assert world.table("SELECT status FROM journal_session")[0][0] == "failed"
    finally:
        daemon.close()


def test_admission_records_home_parent_and_effort(world: World) -> None:
    sid = world.source().stem
    parent = "parent-session"
    with Registry(str(world.store), "fixture") as registry:
        registry.open(now=1)
        registry.insert_run(
            RunRow(
                run_id="parent-run",
                thread_id=parent,
                worktree="/work/demo",
                backend=Backend.CLAUDE,
                phase=RunPhase.COMPLETED,
                sandbox="read-only",
                model="fixture",
                owner_instance="fixture",
                created_at=1,
                updated_at=1,
            )
        )
        registry.admit_run(
            RunRow(
                run_id="child-run",
                thread_id=sid,
                worktree="/work/demo",
                backend=Backend.CLAUDE,
                phase=RunPhase.PENDING,
                sandbox="read-only",
                model="fixture",
                owner_instance="fixture",
                executed_account="fixture-account",
                created_at=1,
                updated_at=1,
            ),
            False,
            1,
            1000,
            journal_context={"home": str(world.claude), "parent_run": "parent-run"},
            effort_profile={"requested": "max", "sent": "high"},
        )
    world.write([user(1, "running")])
    daemon = world.daemon()
    try:
        daemon.cycle(cold=True)
        row = world.table(
            "SELECT parent, parent_evidence, account, requested_effort, sent_effort, effort, status FROM journal_session"
        )[0]
        assert tuple(row) == (
            parent,
            "caller_run",
            "fixture-account",
            "max",
            "high",
            None,
            "live",
        )
    finally:
        daemon.close()


def test_driver_keeps_parent_context_without_environment_values(tmp_path: Path) -> None:
    from nspawn import driver

    frame = driver.make_frame(
        "nspawn_run_start",
        {"cwd": str(tmp_path)},
        drivers_dir=None,
        environ={
            "NOTT_NSPAWN_RUN_ID": "parent-run",
            "CLAUDE_CODE_SESSION_ID": "parent-session",
            "CLAUDE_CONFIG_DIR": str(tmp_path),
            "UNRELATED_SECRET": "never record",
        },
    )
    assert frame["journal_context"] == {
        "parent_run": "parent-run",
        "caller_session": "parent-session",
        "caller_vendor": "claude",
        "caller_home": str(tmp_path),
    }
    assert "never record" not in json.dumps(frame)


def test_native_mcp_scope_forwards_parent_run() -> None:
    from nspawn import native

    assert "NOTT_NSPAWN_RUN_ID" in native._MCP_SCOPE_ENV["nspawn"]


def test_live_files_precede_the_newest_history_file(world: World) -> None:
    older = world.write([user(1, "older")], sid="older")
    newer = world.write([user(1, "newer"), user(2, "newer tail")], sid="newer")
    os.utime(older, (1000, 1000))
    os.utime(newer, (2000, 2000))
    daemon = world.daemon(batch_lines=1)
    try:
        world.write([user(1, "live")], sid="live")
        daemon.cycle(cold=True, batches=1)
        assert [
            r[0] for r in world.table("SELECT id FROM journal_session WHERE seq >= 0")
        ] == ["live"]
        daemon.cycle(cold=True, batches=1)
        assert (
            world.table("SELECT import_state FROM journal_session WHERE id = 'newer'")[
                0
            ][0]
            == "importing"
        )
        assert not world.table("SELECT 1 FROM journal_session WHERE id = 'older'")
        world.write([user(2, "live tail")], sid="live")
        daemon.cycle(cold=True, batches=1)
        assert any(row.get("text") == "live tail" for row in world.rows("live"))
        drain(daemon)
        assert {
            r[0] for r in world.table("SELECT import_state FROM journal_session")
        } == {"done"}
    finally:
        daemon.close()


def test_metadata_backfill_keeps_usage_rows_and_committed_offset(world: World) -> None:
    import nott_database

    daemon = world.daemon()
    world.write([user(0, "earlier prompt")])
    drain(daemon)
    path = world.write(
        [
            user(1, "root", entrypoint="cli"),
            agent(2, "m1", text("answer"), usage(4), stop_reason="end_turn"),
            {
                "type": "system",
                "subtype": "informational",
                "uuid": "dropped",
                "parentUuid": "x2",
                "content": "private",
            },
        ]
    )
    drain(daemon)
    before = world.total()
    with nott_database.write(daemon.connection):
        daemon.connection.execute("DELETE FROM journal_link")
        daemon.connection.execute(
            "UPDATE journal_source SET metadata_version = 0, state = json_remove(state, '$.metadata')"
        )
        daemon.connection.execute(
            "UPDATE journal_session SET entrypoint = NULL, import_state = 'importing'"
        )
        daemon.connection.execute(
            "UPDATE journal_row SET row = json_remove(row, '$.uuid', '$.parentUuid', '$.logicalParentUuid')"
        )
    daemon.close()
    daemon = world.daemon(batch_lines=1)
    try:
        daemon.cycle(cold=True, batches=1)
        assert (
            world.table("SELECT import_state FROM journal_session")[0][0] == "importing"
        )
        assert (
            world.table("SELECT consumed FROM journal_source")[0][0]
            == path.stat().st_size
        )
        drain(daemon)
        assert world.total() == before
        assert world.table("SELECT entrypoint, import_state FROM journal_session")[0][
            :
        ] == ("cli", "done")
        assert len(world.table("SELECT uuid FROM journal_link")) == 4
        assert len([r for r in world.rows() if r["type"] == "message"]) == 3
        assert world.rows()[1]["parentUuid"] is None
        for row in world.rows():
            validate(row)
    finally:
        daemon.close()


def test_copied_subagent_uses_the_parent_from_its_own_home(world: World) -> None:
    homes = [(None, world.claude), (None, world.root / "copy")]
    for _, home in homes:
        parent = home / "projects/demo" / world.source().name
        parent.parent.mkdir(parents=True, exist_ok=True)
        parent.write_text(json.dumps(user(1, "parent")) + "\n")
        agent_path = parent.with_suffix("") / "subagents/agent-child.jsonl"
        agent_path.parent.mkdir(parents=True)
        agent_path.write_text(json.dumps(user(2, "child")) + "\n")
    daemon = world.daemon(homes=homes)
    try:
        drain(daemon)
        records = world.table("SELECT id, vendor_id, home, parent FROM journal_session")
        parents = {row[2]: row[0] for row in records if row[1] == world.source().stem}
        children = [row for row in records if row[1].endswith(".child")]
        assert len(children) == 2
        assert all(row[3] == parents[row[2]] for row in children)
    finally:
        daemon.close()


def test_a_refused_effort_record_is_logged_and_the_writer_goes_on(world: World) -> None:
    with Registry(str(world.store), "fixture") as registry:
        registry.open(now=1)
        registry.insert_run(
            RunRow(
                run_id="malformed-effort",
                thread_id=world.source().stem,
                worktree="/work/demo",
                backend=Backend.CLAUDE,
                phase=RunPhase.PENDING,
                sandbox="read-only",
                model="fixture",
                owner_instance="fixture",
                created_at=1,
                updated_at=1,
            )
        )
        registry.append_run_event(
            "malformed-effort",
            at=1,
            kind="lifecycle",
            payload={
                "effort_profile": {
                    "requested": "high",
                    "sent": "high",
                    "executed": "high",
                }
            },
        )
    world.write([user(1, "prompt")])
    logged: list[str] = []
    daemon = world.daemon(log=logged.append)
    try:
        # Review r1, point 7: one refused record never stops the writer.
        drain(daemon)
        daemon.cycle(cold=True)
    finally:
        daemon.close()
    assert tuple(
        world.table(
            "SELECT requested_effort, sent_effort, import_state FROM journal_session"
        )[0]
    ) == (None, None, "done")
    assert [line for line in logged if "malformed-effort" in line and "effort" in line]


def test_a_child_row_has_priority_over_the_caller_run(world: World) -> None:
    with Registry(str(world.store), "fixture") as registry:
        registry.open(now=1)
        registry.insert_run(
            RunRow(
                run_id="caller",
                thread_id="caller-parent",
                worktree="/work/demo",
                backend=Backend.CLAUDE,
                phase=RunPhase.COMPLETED,
                sandbox="read-only",
                model="fixture",
                owner_instance="fixture",
                created_at=1,
                updated_at=1,
            )
        )
        registry.admit_run(
            RunRow(
                run_id="child-run",
                thread_id="child-session",
                worktree="/work/demo",
                backend=Backend.CLAUDE,
                phase=RunPhase.PENDING,
                sandbox="read-only",
                model="fixture",
                owner_instance="fixture",
                created_at=1,
                updated_at=1,
            ),
            False,
            1,
            1000,
            journal_context={"home": str(world.claude), "parent_run": "caller"},
        )
    world.write(
        [
            agent(
                1,
                "m1",
                {
                    "type": "tool_use",
                    "id": "start",
                    "name": "mcp__nspawn__nspawn_run_start",
                    "input": {},
                },
                usage(1),
            ),
            result(2, "start", '{"run_id":"child-run"}'),
        ],
        sid="source-parent",
    )
    world.write([user(1, "child")], sid="child-session")
    daemon = world.daemon()
    try:
        drain(daemon)
        assert tuple(
            world.table(
                "SELECT parent, parent_evidence FROM journal_session WHERE vendor_id = 'child-session'"
            )[0]
        ) == ("source-parent", "child_row")
    finally:
        daemon.close()


def test_a_resume_keeps_a_recorded_absence_of_requested_effort(world: World) -> None:
    with Registry(str(world.store), "fixture") as registry:
        registry.open(now=1)
        registry.insert_run(
            RunRow(
                run_id="first",
                thread_id=world.source().stem,
                worktree="/work/demo",
                backend=Backend.CLAUDE,
                phase=RunPhase.CLOSED,
                sandbox="read-only",
                model="fixture",
                owner_instance="fixture",
                created_at=1,
                updated_at=1,
            )
        )
        registry.append_run_event(
            "first",
            at=1,
            kind="lifecycle",
            payload={"effort_profile": {"requested": "max", "sent": "high"}},
        )
    world.write([user(1, "first")])
    daemon = world.daemon()
    try:
        daemon.cycle(cold=True)
        assert tuple(
            world.table("SELECT requested_effort, sent_effort FROM journal_session")[0]
        ) == ("max", "high")
        with Registry(str(world.store), "resume") as registry:
            registry.open(now=2)
            registry.admit_run(
                RunRow(
                    run_id="resume",
                    thread_id=world.source().stem,
                    worktree="/work/demo",
                    backend=Backend.CLAUDE,
                    phase=RunPhase.PENDING,
                    sandbox="read-only",
                    model="fixture",
                    owner_instance="resume",
                    created_at=2,
                    updated_at=2,
                ),
                False,
                2,
                1000,
                effort_profile={"requested": None, "sent": None},
            )
        daemon.cycle(cold=True)
        assert tuple(
            world.table("SELECT requested_effort, sent_effort FROM journal_session")[0]
        ) == (None, None)
    finally:
        daemon.close()


def test_metadata_backfill_does_not_delay_a_live_append(world: World) -> None:
    import nott_database

    world.write([user(at, f"old {at}") for at in range(10)])
    daemon = world.daemon()
    drain(daemon)
    with nott_database.write(daemon.connection):
        daemon.connection.execute(
            "UPDATE journal_source SET metadata_version = 0, state = json_remove(state, '$.metadata')"
        )
    daemon.close()
    daemon = world.daemon(batch_lines=1)
    try:
        world.write([user(20, "live tail")])
        daemon.cycle(cold=True, batches=1)
        assert any(row.get("text") == "live tail" for row in world.rows())
        assert (
            world.table("SELECT import_state FROM journal_session")[0][0] == "importing"
        )
        drain(daemon)
        assert world.table("SELECT import_state FROM journal_session")[0][0] == "done"
        assert world.table("SELECT count(*) FROM journal_link")[0][0] == 11
    finally:
        daemon.close()


def test_workflow_run_uses_its_recorded_node_owner(world: World) -> None:
    owner = "11111111-2222-4333-8444-555555555551"
    with Registry(str(world.store), "fixture") as registry:
        registry.open(now=1)
        registry.insert_run(
            RunRow(
                run_id="workflow-child",
                thread_id=world.source().stem,
                worktree="/work/demo",
                backend=Backend.CLAUDE,
                phase=RunPhase.COMPLETED,
                sandbox="read-only",
                model="fixture",
                owner_instance="fixture",
                owner=f"node:{owner}",
                created_at=1,
                updated_at=1,
            )
        )
    world.write([user(1, "workflow child")])
    daemon = world.daemon()
    try:
        daemon.cycle(cold=True)
        assert tuple(
            world.table(
                "SELECT parent, parent_vendor, parent_evidence FROM journal_session"
            )[0]
        ) == (owner, "node", "owner")
    finally:
        daemon.close()


def test_discovery_includes_default_roster_cage_and_node_homes(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nspawn.owner_roster import NativeAccount, Provider, Roster

    monkeypatch.setenv("HOME", str(world.home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(world.claude))
    runtime = world.root / "runtime"
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    account = world.root / "claude-account"
    cage = world.root / "cage"
    node_id = "11111111-2222-4333-8444-555555555551"
    node = runtime / "nott-node/native" / node_id / "native"
    for home, sid in (
        (account, "account-session"),
        (cage, "cage-session"),
        (node, "node-session"),
    ):
        source = home / "projects/demo" / f"{sid}.jsonl"
        source.parent.mkdir(parents=True)
        source.write_text(json.dumps(user(1, sid)) + "\n")
    with Registry(str(world.store), "fixture") as registry:
        registry.open(now=1)
        registry.admit_run(
            RunRow(
                run_id="cage-run",
                thread_id="cage-session",
                worktree="/work/demo",
                backend=Backend.CLAUDE,
                phase=RunPhase.PENDING,
                sandbox="read-only",
                model="fixture",
                owner_instance="fixture",
                executed_account="cage-account",
                created_at=1,
                updated_at=1,
            ),
            False,
            1,
            1000,
            journal_context={"home": str(cage)},
        )
    world.write([user(1, "default")])
    roster = Roster(
        providers=[
            # A Claude-client route other than `claude`, as GPT runs.
            Provider(
                name="fixture-gpt",
                kind="claude",
                model="fixture",
                accounts={"gpt-account": NativeAccount(account)},
            )
        ]
    )
    daemon = world.daemon(homes=None, roster=roster)
    try:
        drain(daemon)
        rows = {
            row[0]: tuple(row[1:])
            for row in world.table(
                "SELECT home, account, owner_node FROM journal_session"
            )
        }
        assert set(rows) == {str(world.claude), str(account), str(cage), str(node)}
        assert rows[str(account)][0] == "gpt-account"
        assert rows[str(cage)][0] == "cage-account"
        assert rows[str(node)][1] == node_id
    finally:
        daemon.close()


def test_history_yields_between_batches_to_a_live_append(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.write([user(0, "live root")], sid="live")
    first = world.daemon()
    first.cycle(cold=True)
    first.close()
    older = world.write([user(1, "older")], sid="older")
    newer = world.write([user(1, "newer")], sid="newer")
    os.utime(older, (1000, 1000))
    os.utime(newer, (2000, 2000))
    translator = TRANSLATORS[0]
    original = translator.translate
    order: list[str] = []

    def translate(raw: bytes, src: int, state: dict[str, Any], context: Context):
        order.append(context.session)
        if context.session == "newer":
            world.write([user(2, "live tail")], sid="live")
        return original(raw, src, state, context)

    monkeypatch.setattr(translator, "translate", translate)
    daemon = world.daemon(batch_lines=1)
    try:
        daemon.cycle(cold=True)
        assert order == ["newer"]
        daemon.cycle(cold=True)
        assert order == ["newer", "live", "older"]
    finally:
        daemon.close()


def test_cold_discovery_queues_old_files_in_modified_order(world: World) -> None:
    daemon = world.daemon(batch_lines=1)
    older = world.write([user(1, "older")], sid="older")
    newer = world.write([user(1, "newer")], sid="newer")
    os.utime(older, (1000, 1000))
    os.utime(newer, (2000, 2000))
    try:
        daemon.cycle(cold=True)
        assert [
            row[0] for row in world.table("SELECT vendor_id FROM journal_session")
        ] == ["newer"]
        drain(daemon)
        assert world.table("SELECT count(*) FROM journal_session")[0][0] == 2
    finally:
        daemon.close()


def test_a_growing_history_file_uses_the_live_queue(world: World) -> None:
    growing = world.write([user(1, "old root")], sid="growing")
    newest = world.write([user(1, "history")], sid="newest")
    os.utime(growing, (1000, 1000))
    os.utime(newest, (2000, 2000))
    daemon = world.daemon(batch_lines=1)
    try:
        world.write([user(2, "live tail")], sid="growing")
        daemon.cycle(cold=True)
        assert any(row.get("text") == "live tail" for row in world.rows("growing"))
        assert (
            world.table(
                "SELECT import_state FROM journal_session WHERE vendor_id = 'growing'"
            )[0][0]
            == "done"
        )
    finally:
        daemon.close()


def test_a_status_row_keeps_the_last_vendor_time(world: World) -> None:
    old = "2026-09-22T10:00:01.000Z"
    world.write(
        [
            user(1, "old prompt", timestamp=old),
            agent(
                2,
                "m1",
                text("old answer"),
                usage(1),
                stop_reason="end_turn",
            )
            | {"timestamp": old},
        ]
    )
    (world.claude / "sessions").mkdir()
    daemon = world.daemon()
    try:
        drain(daemon)
        daemon.cycle(cold=True)
        status, at = world.table("SELECT status, time FROM journal_session")[0]
        vendor = [r["time"] for r in world.rows() if r["type"] == "message"][-1]
        assert status == "ended"
        assert at == vendor and at.startswith("2026-09-22")
    finally:
        daemon.close()


def test_the_shared_reader_selects_the_active_chain(world: World) -> None:
    from nspawn.journal import show

    world.write(
        [
            user(1, "root"),
            agent(2, "m1", text("first answer"), usage(1)) | {"parentUuid": "x1"},
            {
                "type": "attachment",
                "uuid": "x3",
                "parentUuid": "x2",
                "attachment": {"type": "unknown"},
            },
            {
                "type": "system",
                "subtype": "informational",
                "uuid": "x4",
                "parentUuid": "x3",
                "content": "dropped",
            },
            {
                "type": "system",
                "subtype": "compact_boundary",
                "uuid": "x5",
                "parentUuid": None,
                "logicalParentUuid": "x4",
                "compactMetadata": {"trigger": "auto", "preTokens": 10},
            },
            user(6, "after compact", parentUuid="x5"),
            agent(7, "m2", text("kept answer"), usage(1)) | {"parentUuid": "x6"},
            user(8, "abandoned prompt", parentUuid="x7"),
            agent(9, "m3", text("abandoned answer"), usage(1)) | {"parentUuid": "x8"},
            user(10, "rewound prompt", parentUuid="x7"),
            agent(11, "m4", text("rewound answer"), usage(1)) | {"parentUuid": "x10"},
        ]
    )
    daemon = world.daemon()
    try:
        drain(daemon)
    finally:
        daemon.close()
    connection = show.open_readonly()
    assert connection is not None
    try:
        active = show.active_rows(connection, "claude", world.source().stem)
    finally:
        connection.close()
    assert [r["text"] for r in active if r["type"] == "message"] == [
        "root",
        "first answer",
        "after compact",
        "kept answer",
        "rewound prompt",
        "rewound answer",
    ]
    assert any(r["type"] == "compaction" for r in active)


def test_a_cold_cycle_over_unchanged_sessions_takes_no_write(world: World) -> None:
    """The registry is nSPAWN's: a write lock every cold scan per session
    contends with every run admission."""
    for at in range(3):
        world.write(
            [
                user(1, f"prompt {at}"),
                agent(2, "m1", text("done"), usage(1), stop_reason="end_turn"),
            ],
            sid=f"quiet-{at}",
        )
    (world.claude / "sessions").mkdir()
    daemon = world.daemon()
    try:
        drain(daemon)
        daemon.cycle(cold=True)
        statements: list[str] = []
        daemon.connection.set_trace_callback(statements.append)
        daemon.cycle(cold=True)
        daemon.cycle(cold=True)
        daemon.connection.set_trace_callback(None)
        assert [s for s in statements if s.startswith("BEGIN")] == []
    finally:
        daemon.close()


# -- review r1: the cold cycle's cost, subagent status, rollback, gone files -----------


def _finished(world: World, sid: str, agent_id: str | None = None) -> None:
    """One finished turn in session `sid`, or in its subagent `agent_id`."""
    lines = [
        user(1, f"prompt {sid}"),
        agent(2, "m1", text("done"), usage(1), stop_reason="end_turn")
        | {"parentUuid": "x1"},
    ]
    if agent_id is None:
        world.write(lines, sid=sid)
        return
    path = world.projects / sid / "subagents" / f"agent-{agent_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            json.dumps({**line, "isSidechain": True, "agentId": agent_id}) + "\n"
            for line in lines
        )
    )


def _traced(daemon: Any, cycles: int = 1) -> list[str]:
    statements: list[str] = []
    daemon.connection.set_trace_callback(statements.append)
    try:
        for _ in range(cycles):
            daemon.cycle(cold=True)
    finally:
        daemon.connection.set_trace_callback(None)
    return statements


def _hold(world: World, sid: str) -> Path:
    """A live pid record of this process for session `sid`."""
    sessions = world.claude / "sessions"
    sessions.mkdir(exist_ok=True)
    ticks = Path(f"/proc/{os.getpid()}/stat").read_text()
    start = ticks[ticks.rindex(")") + 2 :].split()[19]
    record = sessions / f"{os.getpid()}.json"
    record.write_text(
        json.dumps({"pid": os.getpid(), "sessionId": sid, "procStart": start})
    )
    return record


def test_a_steady_cold_cycle_runs_no_query_per_session(world: World) -> None:
    """Review r1, blocker 1: an unchanged source costs no query. Six more
    sessions and subagents, with runs, and six more sessions with no row (a
    transcript of bookkeeping lines only) leave a cold cycle's statements as
    they were, and a restart over them all opens no write."""
    from nspawn.journal import serve

    (world.claude / "sessions").mkdir()

    def add(first: int, count: int) -> None:
        with Registry(str(world.store), "fixture") as registry:
            registry.open(now=1)
            for n in range(first, first + count):
                sid = f"quiet-{n}"
                _finished(world, sid)
                _finished(world, sid, f"a{n}")
                world.write(
                    [{"type": "summary", "summary": "s", "leafUuid": "x"}],
                    sid=f"empty-{n}",
                )
                registry.admit_run(
                    RunRow(
                        run_id=f"run-{n}",
                        thread_id=sid,
                        worktree="/work/demo",
                        backend=Backend.CLAUDE,
                        phase=RunPhase.PENDING,
                        sandbox="read-only",
                        model="fixture",
                        owner_instance="fixture",
                        created_at=1,
                        updated_at=1,
                    ),
                    False,
                    1,
                    1000,
                    journal_context={"home": str(world.claude)},
                    effort_profile={"requested": "high", "sent": "high"},
                )

    daemon = world.daemon()
    try:
        add(0, 2)
        drain(daemon)
        daemon.cycle(cold=True)
        few = _traced(daemon)
        add(2, 6)
        drain(daemon)
        daemon.cycle(cold=True)
        many = _traced(daemon)
        assert len(daemon.sources) == 24
        assert len(many) == len(few), many
    finally:
        daemon.close()
    restart = serve.Daemon(world.settings())
    statements: list[str] = []
    restart.connection.set_trace_callback(statements.append)
    try:
        restart.start()
        restart.cycle(cold=True)
    finally:
        restart.close()
    assert [s for s in statements if s.startswith("BEGIN")] == []


def test_every_writer_lookup_searches_an_index(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review r1, blocker 1: no statement of discovery, a batch, a cold cycle
    or a thread read scans a journal table, `runs` or `run_events` whole."""
    import nott_database

    monkeypatch.setenv("HOME", str(world.home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(world.claude))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(world.root / "runtime"))
    _hold(world, "quiet-0")
    _finished(world, "quiet-0")
    daemon = world.daemon(homes=None)
    try:
        drain(daemon)
        with Registry(str(world.store), "fixture") as registry:
            registry.open(now=1)
            registry.admit_run(
                RunRow(
                    run_id="run-1",
                    thread_id="quiet-1",
                    worktree="/work/demo",
                    backend=Backend.CLAUDE,
                    phase=RunPhase.PENDING,
                    sandbox="read-only",
                    model="fixture",
                    owner_instance="fixture",
                    created_at=1,
                    updated_at=1,
                ),
                False,
                1,
                1000,
                journal_context={"home": str(world.claude), "parent_run": "run-1"},
            )
        _finished(world, "quiet-1")
        _finished(world, "quiet-1", "a1")
        statements = _traced(daemon, 3)
    finally:
        daemon.close()
    from nspawn import threads
    from nspawn.journal import show

    opened = show.open_readonly

    def traced() -> Any:
        connection = opened()
        assert connection is not None
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(show, "open_readonly", traced)
    assert threads.journal_history("quiet-1", world.claude, 100, 1 << 20)["messages"]
    scans: set[str] = set()
    connection = nott_database.connect(world.store, readonly=True)
    try:
        for statement in set(statements):
            if statement.split(None, 1)[0].upper() not in (
                "SELECT",
                "UPDATE",
                "INSERT",
            ):
                continue
            for step in nott_database.query(
                connection, f"EXPLAIN QUERY PLAN {statement}"
            ):
                detail = str(step[3])
                # A search by the vendor alone walks every session of it.
                whole = detail.startswith("SCAN ") or detail.endswith("(vendor=?)")
                if whole and detail.split()[1] in (
                    "journal_source",
                    "journal_session",
                    "journal_row",
                    "journal_link",
                    "runs",
                    "run_events",
                ):
                    scans.add(f"{detail}: {statement}")
    finally:
        connection.close()
    assert scans == set()


def test_a_thread_show_reads_only_its_own_session(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review r1, blocker 1: a node replay of one session costs the same over
    two sessions as over eight."""
    from nspawn import threads
    from nspawn.journal import show

    opened = show.open_readonly
    statements: list[str] = []

    def traced() -> Any:
        connection = opened()
        assert connection is not None
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(show, "open_readonly", traced)

    def replay() -> int:
        statements.clear()
        shown = threads.journal_history("quiet-0", world.claude, 100, 1 << 20)
        assert [m["message"]["content"] for m in shown["messages"]][:1] == [
            "prompt quiet-0"
        ]
        return len(statements)

    daemon = world.daemon()
    try:
        for n in range(2):
            _finished(world, f"quiet-{n}")
        drain(daemon)
        few = replay()
        for n in range(2, 8):
            _finished(world, f"quiet-{n}")
        drain(daemon)
        assert replay() == few
    finally:
        daemon.close()


def test_a_finished_subagent_of_a_live_session_is_ended(world: World) -> None:
    """Review r1, blocker 2: a Task child whose turn ended is `ended` while
    its parent runs, as main wrote it; a running child is `live`."""
    _hold(world, "parent")
    world.write([user(1, "parent prompt")], sid="parent")
    _finished(world, "parent", "done")
    running = world.projects / "parent" / "subagents" / "agent-running.jsonl"
    running.write_text(
        json.dumps({**user(1, "working"), "isSidechain": True, "agentId": "running"})
        + "\n"
    )
    daemon = world.daemon()
    try:
        drain(daemon)
        daemon.cycle(cold=True)
        statuses = dict(world.table("SELECT id, status FROM journal_session"))
    finally:
        daemon.close()
    assert statuses == {
        "parent": "live",
        "parent.done": "ended",
        "parent.running": "live",
    }


@pytest.mark.parametrize(
    ("sidecar", "status"),
    [
        pytest.param(b'{"finished": true}', "ended", id="finished"),
        pytest.param(None, "live", id="missing"),
        pytest.param(b'{"agentType": "Explore"}', "live", id="no-finished"),
        pytest.param(b'{"finished":', "live", id="malformed"),
        pytest.param(b'{"finished": false}', "live", id="unfinished"),
        pytest.param(b'{"finished": "true"}', "live", id="string"),
        pytest.param(b'{"finished": 1}', "live", id="number"),
        pytest.param(b"null", "live", id="null"),
        pytest.param(b"[]", "live", id="array"),
        pytest.param(b"\xff", "live", id="invalid-encoding"),
        pytest.param(b"directory", "live", id="unreadable"),
    ],
)
@pytest.mark.parametrize("after_import", [False, True], ids=["initial", "later"])
def test_subagent_sidecar_finished_status(
    world: World,
    capsys: pytest.CaptureFixture[str],
    sidecar: bytes | None,
    status: str,
    after_import: bool,
) -> None:
    """Only the boolean finished marker ends an API-error child of a live parent."""
    _hold(world, "parent")
    world.write([user(1, "parent prompt")], sid="parent")
    world.source("parent").with_suffix(".meta.json").write_text('{"finished": true}')
    path = world.projects / "parent" / "subagents" / "agent-limited.jsonl"
    path.parent.mkdir(parents=True)
    error = agent(2, "m1", text("API Error: 429"), usage(0)) | {
        "error": "rate_limit",
        "isApiErrorMessage": True,
        "status": 429,
        "isSidechain": True,
        "agentId": "limited",
    }
    path.write_text(json.dumps(error) + "\n")
    meta = path.with_suffix(".meta.json")

    def write_sidecar() -> None:
        if sidecar == b"directory":
            meta.mkdir()
        elif sidecar is not None:
            meta.write_bytes(sidecar)

    if not after_import:
        write_sidecar()
    daemon = world.daemon()
    try:
        drain(daemon)
        if after_import:
            assert dict(world.table("SELECT id, status FROM journal_session")) == {
                "parent": "live",
                "parent.limited": "live",
            }
            write_sidecar()
        daemon.cycle(cold=True)
        assert cli.main(["session", "list", "claude"]) == 0
        listed = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        assert {row["id"]: row["status"] for row in listed} == {
            "parent": "live",
            "parent.limited": status,
        }
    finally:
        daemon.close()


#: The sidecar that Claude Code keeps while SendMessage resumes the agent.
RESUMED: bytes = b'{"agentType": "general-purpose", "finished": true}'
#: The opening line of a resumed turn, by its kind.
OPENINGS: dict[str, dict[str, Any]] = {
    "coordinator": user(4, "resume", isMeta=True, origin={"kind": "coordinator"}),
    "turn-companion": user(4, "resume", isMeta=True, turnCompanion=True),
    "queued-command": attachment(4, {"type": "queued_command", "prompt": "resume"}),
}


def _append(path: Path, agent_id: str, *records: dict[str, Any]) -> None:
    """Append `records` to the subagent transcript `path` of `agent_id`."""
    with path.open("a") as stream:
        stream.write(
            "".join(
                json.dumps({**record, "isSidechain": True, "agentId": agent_id}) + "\n"
                for record in records
            )
        )


def _sidecar(path: Path, second: int, body: bytes = RESUMED) -> None:
    """Write the sidecar of the transcript `path` with the modification time
    `2026-10-22T10:00:<second>Z`. The cure does not read this time."""
    meta = path.with_suffix(".meta.json")
    meta.write_bytes(body)
    stamp = datetime(2026, 10, 22, 10, 0, second, tzinfo=UTC)
    ns = int(stamp.timestamp()) * 1_000_000_000
    os.utime(meta, ns=(ns, ns))


def _resumed_agent(
    world: World, agent_id: str = "resumed", sidecar: bool = True
) -> Path:
    """The parent session and its finished Agent-tool subagent `agent_id`,
    with a `finished: true` sidecar at 10:00:03 when `sidecar` is true."""
    world.write([user(1, "parent prompt")], sid="parent")
    world.source("parent").with_suffix(".meta.json").write_text('{"finished": true}')
    path = world.projects / "parent" / "subagents" / f"agent-{agent_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    _append(
        path,
        agent_id,
        user(1, "work"),
        agent(2, "m1", text("done"), usage(1), stop_reason="end_turn"),
    )
    if sidecar:
        _sidecar(path, 3)
    return path


def _shown(capsys: pytest.CaptureFixture[str], sid: str) -> str:
    """The last status row of `nspawn session show claude <sid>`."""
    capsys.readouterr()
    assert cli.main(["session", "show", "claude", sid]) == 0
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    return str([row for row in rows if row.get("type") == "status"][-1]["status"])


def _listed(capsys: pytest.CaptureFixture[str], sid: str) -> str:
    """The status of `sid` in `nspawn session list claude`."""
    capsys.readouterr()
    assert cli.main(["session", "list", "claude"]) == 0
    listed = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    return str({row["id"]: row["status"] for row in listed}[sid])


@pytest.mark.parametrize("opening", list(OPENINGS))
def test_a_resumed_subagent_reads_live_until_its_next_end_line(
    world: World, capsys: pytest.CaptureFixture[str], opening: str
) -> None:
    """A SendMessage resume keeps the `finished: true` sidecar. The opening
    line comes after the daemon read that sidecar, so the subagent reads
    `live` while it writes, and `ended` after its next end line."""
    _hold(world, "parent")
    path = _resumed_agent(world)
    daemon = world.daemon()
    try:
        drain(daemon)
        daemon.cycle(cold=True)
        seen = [_shown(capsys, "parent.resumed")]
        _append(path, "resumed", OPENINGS[opening])
        daemon.cycle(cold=True)
        seen.append(_shown(capsys, "parent.resumed"))
        _append(
            path,
            "resumed",
            agent(5, "m2", text("again"), usage(1), stop_reason="end_turn"),
        )
        daemon.cycle(cold=True)
        seen.append(_shown(capsys, "parent.resumed"))
    finally:
        daemon.close()
    assert seen == ["ended", "live", "ended"]


def test_a_restart_keeps_a_resumed_subagent_live(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    """The batch of the opening line commits the witness of the sidecar
    read. A new daemon restores it, and the subagent stays `live`."""
    _hold(world, "parent")
    path = _resumed_agent(world)
    daemon = world.daemon()
    try:
        drain(daemon)
        daemon.cycle(cold=True)
        _append(path, "resumed", OPENINGS["coordinator"])
        daemon.cycle(cold=True)
    finally:
        daemon.close()
    daemon = world.daemon()
    try:
        daemon.cycle(cold=True)
        assert _listed(capsys, "parent.resumed") == "live"
    finally:
        daemon.close()


@pytest.mark.parametrize(
    "writes", ["no-new-bytes", "sidecar-after-opening", "unproven-window"]
)
def test_a_finished_sidecar_with_no_later_opening_line_keeps_ended(
    world: World, capsys: pytest.CaptureFixture[str], writes: str
) -> None:
    """With no proven turn line after the sidecar read, the sidecar ends the
    subagent: no new bytes, a new sidecar write after the opening line (a
    stop after a resume), and an opening line that the first `lstat` after
    the sidecar read holds already."""
    _hold(world, "parent")
    path = _resumed_agent(world, sidecar=writes != "unproven-window")
    daemon = world.daemon()
    try:
        drain(daemon)
        if writes == "unproven-window":
            _sidecar(path, 3)
        daemon.cycle(cold=True)
        if writes == "sidecar-after-opening":
            _append(
                path,
                "resumed",
                OPENINGS["coordinator"],
                agent(5, "m2", text("tool"), usage(1), stop_reason="tool_use"),
            )
            _sidecar(path, 6)
        elif writes == "unproven-window":
            _append(path, "resumed", OPENINGS["coordinator"])
        seen = []
        for _ in range(3):
            daemon.cycle(cold=True)
            seen.append(_listed(capsys, "parent.resumed"))
    finally:
        daemon.close()
    assert seen == ["ended"] * 3


def test_an_old_sidecar_without_a_holding_parent_is_ended(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    """No process of the parent holds the subagent, so a proven old sidecar
    gives `ended` through the turn rule."""
    path = _resumed_agent(world)
    daemon = world.daemon()
    try:
        drain(daemon)
        daemon.cycle(cold=True)
        _append(path, "resumed", OPENINGS["coordinator"])
        daemon.cycle(cold=True)
        assert _listed(capsys, "parent.resumed") == "ended"
    finally:
        daemon.close()


def test_a_new_marker_after_a_backward_clock_step_is_ended(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    """A sidecar write with a time before the opening line and before the
    first sidecar time (the clock stepped back) changes the mark. It is a
    new completion marker."""
    _hold(world, "parent")
    path = _resumed_agent(world)
    daemon = world.daemon()
    try:
        drain(daemon)
        daemon.cycle(cold=True)
        _append(path, "resumed", OPENINGS["coordinator"])
        daemon.cycle(cold=True)
        seen = [_shown(capsys, "parent.resumed")]
        _append(
            path,
            "resumed",
            agent(5, "m2", text("tool"), usage(1), stop_reason="tool_use"),
        )
        _sidecar(path, 2)
        daemon.cycle(cold=True)
        seen.append(_shown(capsys, "parent.resumed"))
    finally:
        daemon.close()
    assert seen == ["live", "ended"]


@pytest.mark.parametrize("order", ["read-then-stopped", "both-while-stopped"])
def test_a_restart_after_both_writes_keeps_ended(
    world: World, capsys: pytest.CaptureFixture[str], order: str
) -> None:
    """A turn line and a new sidecar write while the daemon is stopped: the
    committed witness is absent or names an older mark, so the subagent is
    `ended`."""
    _hold(world, "parent")
    path = _resumed_agent(world)
    daemon = world.daemon()
    try:
        drain(daemon)
        daemon.cycle(cold=True)
        if order == "read-then-stopped":
            _append(path, "resumed", OPENINGS["coordinator"])
            daemon.cycle(cold=True)
    finally:
        daemon.close()
    if order == "both-while-stopped":
        _append(path, "resumed", OPENINGS["coordinator"])
    _append(
        path,
        "resumed",
        agent(5, "m2", text("tool"), usage(1), stop_reason="tool_use"),
    )
    _sidecar(path, 2)
    daemon = world.daemon()
    try:
        seen = []
        for _ in range(3):
            daemon.cycle(cold=True)
            seen.append(_listed(capsys, "parent.resumed"))
    finally:
        daemon.close()
    assert seen == ["ended"] * 3


@pytest.mark.parametrize("kind", ["teammate", "workflow-agent"])
def test_a_later_line_keeps_a_finished_teammate_or_workflow_agent_ended(
    world: World, capsys: pytest.CaptureFixture[str], kind: str
) -> None:
    """The order rule is for Agent-tool subagents only. A finished teammate
    and a finished Workflow agent stay `ended` after a later line."""
    _hold(world, "parent")
    if kind == "teammate":
        agent_id = "worker"
        path = _resumed_agent(world, agent_id, sidecar=False)
        _sidecar(
            path,
            3,
            json.dumps(
                {
                    "taskKind": "in_process_teammate",
                    "teamName": "team",
                    "name": "worker",
                    "finished": True,
                }
            ).encode(),
        )
        config = world.claude / "teams" / "team" / "config.json"
        config.parent.mkdir(parents=True)
        config.write_text('{"members": [{"name": "worker"}]}')
    else:
        agent_id = "wa"
        world.write([user(1, "parent prompt")], sid="parent")
        _workflow_agent(world, "parent", agent_id, "end_turn")
        path = world.projects / "parent" / "subagents" / "workflows" / "wf_x"
        path = path / f"agent-{agent_id}.jsonl"
        _sidecar(
            path,
            3,
            b'{"agentType": "general-purpose", "workflowPhase": "build", '
            b'"finished": true}',
        )
    daemon = world.daemon()
    try:
        drain(daemon)
        daemon.cycle(cold=True)
        _append(path, agent_id, OPENINGS["coordinator"])
        seen = []
        for _ in range(3):
            daemon.cycle(cold=True)
            seen.append(_listed(capsys, f"parent.{agent_id}"))
    finally:
        daemon.close()
    assert seen == ["ended"] * 3


def test_a_resumed_turn_of_agent_records_only_reads_live(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    """The opening line comes before the bound, and the turn goes on with
    agent records only. An agent record is order evidence, so the subagent
    reads `live` from its first agent record after the bound."""
    _hold(world, "parent")
    path = _resumed_agent(world, sidecar=False)
    daemon = world.daemon()
    try:
        drain(daemon)
        _sidecar(path, 3)
        daemon.cycle(cold=True)
        _append(path, "resumed", OPENINGS["coordinator"])
        daemon.cycle(cold=True)
        seen = [_shown(capsys, "parent.resumed")]
        _append(
            path,
            "resumed",
            agent(5, "m2", {"type": "thinking", "thinking": "work"}, usage(0)),
            agent(6, "m2", text("answer"), usage(1)),
        )
        daemon.cycle(cold=True)
        seen.append(_shown(capsys, "parent.resumed"))
        _append(
            path,
            "resumed",
            agent(7, "m3", text("done"), usage(1), stop_reason="end_turn"),
        )
        daemon.cycle(cold=True)
        seen.append(_shown(capsys, "parent.resumed"))
    finally:
        daemon.close()
    assert seen == ["ended", "live", "ended"]


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(
            {
                "type": "hook_success",
                "hookEvent": "SubagentStop",
                "hookName": "SubagentStop",
            },
            id="hook_success",
        ),
        pytest.param(
            {"type": "hook_system_message", "hookEvent": "SubagentStop"},
            id="hook_system_message",
        ),
        pytest.param({"type": "prompt_snapshot"}, id="prompt_snapshot"),
    ],
)
def test_an_attachment_after_a_new_marker_keeps_ended(
    world: World, capsys: pytest.CaptureFixture[str], body: dict[str, Any]
) -> None:
    """An attachment is not order evidence. The record kinds that come after
    a final answer cannot reopen a turn after a new completion marker."""
    _hold(world, "parent")
    path = _resumed_agent(world)
    daemon = world.daemon()
    try:
        drain(daemon)
        daemon.cycle(cold=True)
        _append(path, "resumed", OPENINGS["coordinator"])
        daemon.cycle(cold=True)
        _append(
            path,
            "resumed",
            agent(5, "m2", text("tool"), usage(1), stop_reason="tool_use"),
        )
        _sidecar(path, 6)
        seen = []
        for _ in range(2):
            daemon.cycle(cold=True)
            seen.append(_listed(capsys, "parent.resumed"))
        _append(path, "resumed", attachment(6, body))
        daemon.cycle(cold=True)
        seen.append(_listed(capsys, "parent.resumed"))
    finally:
        daemon.close()
    assert seen == ["ended"] * 3


@pytest.mark.parametrize("restart", [False, True], ids=["running", "restart"])
@pytest.mark.parametrize("rewritten", [True, False], ids=["new-mark", "same-mark"])
@pytest.mark.parametrize("failure", ["pread", "lstat"])
def test_a_transcript_read_error_ends_a_resumed_subagent(
    world: World,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    rewritten: bool,
    restart: bool,
) -> None:
    """Review r1 of PR 1950, blocker 1: a `live` from an old sidecar does not
    survive a read error of the transcript. The resumed subagent stops (its
    final record, and a new sidecar write or none). Then each `pread` or
    `lstat` of its transcript raises EIO while the sidecar stays readable.
    The finished sidecar is the only evidence, so the subagent is `ended`.
    With `restart`, a new daemon starts while the read error lasts: the
    committed witness names the sidecar."""
    _hold(world, "parent")
    path = _resumed_agent(world)
    daemon = world.daemon()
    try:
        drain(daemon)
        daemon.cycle(cold=True)
        _append(path, "resumed", OPENINGS["coordinator"])
        daemon.cycle(cold=True)
        assert _listed(capsys, "parent.resumed") == "live"
        if restart:
            daemon.close()
        _append(
            path,
            "resumed",
            agent(5, "m2", text("done"), usage(1), stop_reason="end_turn"),
        )
        if rewritten:
            _sidecar(path, 6)
        target = path.stat()
        if failure == "pread":
            real_pread = os.pread

            def pread(descriptor: int, length: int, offset: int) -> bytes:
                info = os.fstat(descriptor)
                if (info.st_dev, info.st_ino) == (target.st_dev, target.st_ino):
                    raise OSError(errno.EIO, os.strerror(errno.EIO))
                return real_pread(descriptor, length, offset)

            monkeypatch.setattr(os, "pread", pread)
        else:
            real_lstat = Path.lstat

            def lstat(self: Path) -> os.stat_result:
                if self == path:
                    raise OSError(errno.EIO, os.strerror(errno.EIO))
                return real_lstat(self)

            monkeypatch.setattr(Path, "lstat", lstat)
        if restart:
            daemon = world.daemon()
        seen = []
        for _ in range(3):
            daemon.cycle(cold=True)
            seen.append(_listed(capsys, "parent.resumed"))
        monkeypatch.undo()
        daemon.cycle(cold=True)
        seen.append(_listed(capsys, "parent.resumed"))
    finally:
        daemon.close()
    assert seen == ["ended"] * 4


def _workflow_agent(
    world: World,
    sid: str,
    agent_id: str,
    stop_reason: str | None,
    run: str = "wf_x",
    n: int = 1,
) -> Path:
    """One Workflow agent of session `sid` in the Workflow `run`, whose two
    records have the times `n` and `n + 1` and whose last answer has
    `stop_reason`, with the Workflow metadata (no `finished` key). The path of
    its Workflow journal, not written."""
    flow = world.projects / sid / "subagents" / "workflows" / run
    flow.mkdir(parents=True, exist_ok=True)
    path = flow / f"agent-{agent_id}.jsonl"
    lines = [
        user(n, "work"),
        agent(n + 1, "m1", text("done"), usage(1), stop_reason=stop_reason),
    ]
    path.write_text(
        "".join(
            json.dumps({**line, "isSidechain": True, "agentId": agent_id}) + "\n"
            for line in lines
        )
    )
    path.with_suffix(".meta.json").write_text(
        json.dumps({"agentType": "general-purpose", "workflowPhase": "build"})
    )
    return flow / "journal.jsonl"


def _workflow_lines(*records: dict[str, Any] | str) -> str:
    return "".join(
        (record if isinstance(record, str) else json.dumps(record)) + "\n"
        for record in records
    )


LAUNCHED: dict[str, Any] = {"type": "launched"}
STARTED: dict[str, Any] = {
    "type": "started",
    "agentId": "wa",
    "key": "k",
    "label": "l",
    "phase": "build",
}


@pytest.mark.parametrize(
    ("journal", "status"),
    [
        pytest.param(None, "live", id="no-journal"),
        pytest.param(_workflow_lines(LAUNCHED, STARTED), "live", id="started"),
        pytest.param(
            _workflow_lines(
                LAUNCHED,
                STARTED,
                {"type": "result", "agentId": "other", "key": "k", "result": "r"},
            ),
            "live",
            id="other-agent",
        ),
        pytest.param(
            _workflow_lines(
                LAUNCHED,
                STARTED,
                {"type": "result", "agentId": "wa", "key": "k", "result": "r"},
            ),
            "ended",
            id="result",
        ),
        pytest.param(
            _workflow_lines(
                LAUNCHED,
                STARTED,
                {
                    "type": "result",
                    "agentId": "wa",
                    "key": "k",
                    "result": {"verdict": "SHIP"},
                },
            ),
            "ended",
            id="object-result",
        ),
        pytest.param(
            _workflow_lines(
                LAUNCHED, STARTED, {"type": "failed", "agentId": "wa", "key": "k"}
            ),
            "failed",
            id="failed",
        ),
        pytest.param(
            _workflow_lines(
                LAUNCHED,
                STARTED,
                '{"type": "result", "agentId":',
                "[]",
                {"type": ["result"], "agentId": "wa"},
                {"type": "result", "agentId": 1},
            ),
            "live",
            id="malformed",
        ),
        # Review r2 of PR 1931, F4: a line feed alone ends a record.
        pytest.param(
            _workflow_lines(LAUNCHED, STARTED)
            + '{"type":"result","agentId":"wa"}\rnot-json\n',
            "live",
            id="carriage-return-inside",
        ),
        pytest.param(
            _workflow_lines(LAUNCHED, STARTED) + '{"type":"result","agentId":"wa"}\r\n',
            "live",
            id="carriage-return-before-line-feed",
        ),
        pytest.param(
            _workflow_lines(LAUNCHED, STARTED)
            + json.dumps({"type": "result", "agentId": "wa", "key": "k"}),
            "live",
            id="no-final-line-feed",
        ),
    ],
)
@pytest.mark.parametrize("after_import", [False, True], ids=["initial", "later"])
def test_workflow_agent_journal_result_ends_it_while_the_lead_lives(
    world: World,
    capsys: pytest.CaptureFixture[str],
    journal: str | None,
    status: str,
    after_import: bool,
) -> None:
    """Review r1 of PR 1931, R1: a Workflow agent's metadata has no `finished`
    key. The Workflow journal's `result` line for the agent is its independent
    finished signal, so the agent ends while the lead process holds it, even
    when its last answer has no `stop_reason`."""
    _hold(world, "parent")
    world.write([user(1, "parent prompt")], sid="parent")
    flow = _workflow_agent(world, "parent", "wa", None)
    if not after_import and journal is not None:
        flow.write_text(journal)
    elif after_import:
        # The agent runs at the first import, so a later end needs a new read.
        flow.write_text(_workflow_lines(LAUNCHED, STARTED))
    daemon = world.daemon()
    try:
        drain(daemon)
        if after_import:
            daemon.cycle(cold=True)
            assert dict(world.table("SELECT id, status FROM journal_session")) == {
                "parent": "live",
                "parent.wa": "live",
            }
            if journal is not None:
                flow.write_text(journal)
        daemon.cycle(cold=True)
        assert cli.main(["session", "list", "claude"]) == 0
        listed = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        assert {row["id"]: row["status"] for row in listed} == {
            "parent": "live",
            "parent.wa": status,
        }
    finally:
        daemon.close()


@pytest.mark.parametrize("stop_reason", [None, "end_turn"])
def test_workflow_agent_journal_result_ends_it_without_proc(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    stop_reason: str | None,
) -> None:
    """Review r1 of PR 1931, R1: on a host without `/proc` no process liveness
    exists, so only the Workflow journal's `result` line can end the agent."""
    from nspawn import threads

    monkeypatch.setattr(threads, "PROC", world.root / "no-proc")
    world.write([user(1, "parent prompt")], sid="parent")
    _workflow_agent(world, "parent", "wa", stop_reason).write_text(
        _workflow_lines(
            LAUNCHED,
            STARTED,
            {"type": "result", "agentId": "wa", "key": "k", "result": "r"},
        )
    )
    daemon = world.daemon()
    try:
        drain(daemon)
        daemon.cycle(cold=True)
        statuses = dict(world.table("SELECT id, status FROM journal_session"))
    finally:
        daemon.close()
    assert statuses["parent.wa"] == "ended", statuses


ENDED = _workflow_lines(
    LAUNCHED, STARTED, {"type": "result", "agentId": "wa", "key": "k", "result": "r"}
)


def _last_status(world: World, sid: str) -> tuple[str, str | None]:
    rows = [row for row in world.rows(sid) if row["type"] == "status"]
    return rows[-1]["status"], rows[-1]["reason"]


@pytest.mark.parametrize("after_read", [False, True], ids=["initial", "later"])
def test_workflow_journal_with_two_links_is_refused(
    world: World, after_read: bool
) -> None:
    """Review r2 of PR 1931, F1: the Workflow journal is terminal evidence only
    as an owner-held readable regular file with one link. A second link can
    write a `result` line from outside the workflow folder, so the journal is
    refused, also when an earlier read of the same inode is cached. Review r3,
    B1: the refusal sets no status. The lead holds the agent and its turn is
    open, so the other rules give `live`, and the refusal is the reason."""
    _hold(world, "parent")
    world.write([user(1, "parent prompt")], sid="parent")
    flow = _workflow_agent(world, "parent", "wa", None)
    flow.write_text(ENDED)
    daemon = world.daemon()
    try:
        drain(daemon)
        if after_read:
            daemon.cycle(cold=True)
            assert _last_status(world, "parent.wa") == ("ended", None)
        os.link(flow, world.root / "outside.jsonl")
        daemon.cycle(cold=True)
        statuses = dict(world.table("SELECT id, status FROM journal_session"))
    finally:
        daemon.close()
    assert statuses["parent.wa"] == "live", statuses
    assert _last_status(world, "parent.wa") == (
        "live",
        "workflow journal is not an owner regular file",
    )


def _call(n: int, call: str, run: str = "wf_x", resume: bool = False) -> dict[str, Any]:
    """A Workflow tool call of the parent: the launch of a new run, or with
    `resume` a resume of `run`."""
    given = {"resumeFromRunId": run} if resume else {}
    use = {"type": "tool_use", "id": call, "name": "Workflow", "input": given}
    return agent(n, f"m{n}", use, usage(1))


def _launched(n: int, call: str, run: str = "wf_x") -> dict[str, Any]:
    """The launch result of the Workflow tool call `call` for `run`."""
    launched = {"status": "async_launched", "taskType": "local_workflow", "runId": run}
    return {**result(n, call, "Workflow launched."), "toolUseResult": launched}


def _launch(
    n: int, call: str, run: str = "wf_x", resume: bool = False
) -> list[dict[str, Any]]:
    """A Workflow tool call of the parent and its launch result for `run`."""
    return [_call(n, call, run, resume), _launched(n + 1, call, run)]


def _notice(call: str, status: str) -> str:
    """The end notification that Claude Code gives the parent for one task."""
    return (
        "<task-notification>\n<task-id>wt</task-id>\n"
        f"<tool-use-id>{call}</tool-use-id>\n<status>{status}</status>\n"
        '<summary>Background workflow "w" (run wf_x) ended</summary>\n'
        "</task-notification>"
    )


def _ends(n: int, call: str, status: str) -> dict[str, Any]:
    return user(n, _notice(call, status), origin={"kind": "task-notification"})


def _queued(n: int, call: str, status: str) -> dict[str, Any]:
    return attachment(
        n,
        {
            "type": "queued_command",
            "prompt": _notice(call, status),
            "commandMode": "task-notification",
        },
    )


def _statuses(world: World) -> dict[str, str]:
    return dict(world.table("SELECT id, status FROM journal_session"))


def _runs(world: World, sid: str = "parent") -> dict[str, Any]:
    """The Workflow runs that the committed state of session `sid` keeps."""
    state = json.loads(
        world.table("SELECT state FROM journal_source WHERE session = ?", (sid,))[0][0]
    )
    runs = state.get("workflows", {})
    return runs if isinstance(runs, dict) else {}


def _members(runs: dict[str, Any], run: str) -> Any:
    """The members of `run`. The run keeps them until each one has durable
    terminal evidence."""
    entry = runs.get(run)
    return entry.get("members") if isinstance(entry, dict) else None


def _started(world: World, sid: str, agent_id: str, run: str = "wf_x") -> Path:
    """Workflow agent `agent_id` of `run`, with its clock behind every end in
    these tests, and its `started` line in the Workflow journal."""
    journal = _workflow_agent(world, sid, agent_id, None, run=run)
    lines = [] if journal.exists() else [LAUNCHED]
    with journal.open("a") as stream:
        stream.write(_workflow_lines(*lines, {**STARTED, "agentId": agent_id}))
    return journal


@pytest.mark.parametrize(
    ("parent", "journal", "status"),
    [
        pytest.param(
            [*_launch(2, "c1"), _ends(4, "c1", "stopped")],
            None,
            "failed",
            id="stopped",
        ),
        pytest.param([*_launch(2, "c1")], None, "live", id="no-notification"),
        pytest.param(
            [*_launch(2, "c1"), _ends(4, "c1", "killed")], None, "failed", id="killed"
        ),
        pytest.param(
            [*_launch(2, "c1"), _ends(4, "c1", "failed")], None, "failed", id="failed"
        ),
        pytest.param(
            [*_launch(2, "c1"), _queued(4, "c1", "completed")],
            None,
            "failed",
            id="queued-completed",
        ),
        pytest.param(
            [
                *_launch(2, "c1"),
                _ends(4, "c1", "stopped"),
                *_launch(5, "c2", resume=True),
            ],
            None,
            "live",
            id="resumed",
        ),
        pytest.param(
            [
                *_launch(2, "c1"),
                _ends(4, "c1", "stopped"),
                *_launch(5, "c2", resume=True),
                _ends(7, "c2", "completed"),
            ],
            None,
            "failed",
            id="resumed-and-ended",
        ),
        pytest.param(
            [
                *_launch(2, "c1"),
                *_launch(4, "c2", resume=True),
                _ends(6, "c1", "stopped"),
            ],
            None,
            "live",
            id="end-of-an-older-launch",
        ),
        pytest.param(
            [*_launch(2, "c1"), _ends(4, "other", "stopped")],
            None,
            "live",
            id="other-call",
        ),
        pytest.param(
            [*_launch(2, "c1", run="wf_y"), _ends(4, "c1", "stopped")],
            None,
            "live",
            id="other-workflow",
        ),
        pytest.param(
            [*_launch(2, "c1"), user(4, _notice("c1", "stopped"))],
            None,
            "live",
            id="owner-text",
        ),
        pytest.param(
            [*_launch(2, "c1"), _ends(4, "c1", "running")],
            None,
            "live",
            id="not-terminal",
        ),
        pytest.param([_ends(4, "c1", "stopped")], None, "live", id="no-launch"),
        pytest.param(
            [*_launch(2, "c1"), _ends(4, "c1", "stopped")],
            ENDED,
            "ended",
            id="own-result-first",
        ),
        pytest.param(
            [*_launch(2, "c1"), _ends(4, "c1", "stopped")],
            _workflow_lines(
                LAUNCHED, STARTED, {"type": "failed", "agentId": "wa", "key": "k"}
            ),
            "failed",
            id="own-failure-first",
        ),
    ],
)
@pytest.mark.parametrize("after_import", [False, True], ids=["initial", "later"])
def test_workflow_end_notification_ends_its_open_agents(
    world: World,
    parent: list[dict[str, Any]],
    journal: str | None,
    status: str,
    after_import: bool,
) -> None:
    """Spec PR 1944, failure modes 1 and 2. A Workflow that stops writes no
    end line for its agents in flight. The parent's own end notification of
    each launch of that Workflow ends each of its agents with no end line.
    The status is `failed`, because the agent did not finish its work. A
    Workflow with no end notification, or one launched again after it, keeps
    the other rules. A parent line that the journal has not read yet changes
    nothing until a later read. An agent's own `result` or `failed` line
    comes first."""
    _hold(world, "parent")
    world.write([user(1, "parent prompt")], sid="parent")
    _workflow_agent(world, "parent", "wa", None).write_text(
        journal or _workflow_lines(LAUNCHED, STARTED)
    )
    if not after_import:
        world.write(parent, sid="parent")
    daemon = world.daemon()
    try:
        drain(daemon)
        if after_import:
            daemon.cycle(cold=True)
            before = _statuses(world)
            assert before["parent.wa"] == ("live" if journal is None else status)
            world.write(parent, sid="parent")
            drain(daemon)
        daemon.cycle(cold=True)
        statuses = _statuses(world)
    finally:
        daemon.close()
    assert statuses == {"parent": "live", "parent.wa": status}, statuses


@pytest.mark.parametrize("boundary", ["batch", "bytes", "unfinished"])
def test_a_resume_agent_is_never_failed_before_its_call_is_read(
    world: World, boundary: str
) -> None:
    """Spec PR 1944, failure mode 3. The parent holds the end of launch `c1`
    and then the resume `c2` of the same run. The resume call is after a batch
    boundary, after the read-byte boundary, or in a line with no line feed.
    The agent `wb` of the resume exists before the journal reads that call,
    and its clock is behind the end. No status sweep shows it `failed`."""
    _hold(world, "parent")
    world.write(
        [user(1, "parent prompt"), *_launch(2, "c1"), _ends(4, "c1", "stopped")],
        sid="parent",
    )
    _started(world, "parent", "wa")
    _started(world, "parent", "wb")
    resume = _launch(20, "c2", resume=True)
    if boundary == "unfinished":
        world.write(json.dumps(resume[0]), sid="parent")
    else:
        world.write(
            [*(user(10 + k, "x" * 3000) for k in range(3)), *resume], sid="parent"
        )
    extra: dict[str, Any] = {
        "batch": {"batch_lines": 2},
        "bytes": {"read_bytes": 4096},
        "unfinished": {},
    }[boundary]
    daemon = world.daemon(**extra)
    seen: list[str | None] = []
    try:
        for _ in range(12):
            daemon.cycle(cold=True, batches=1 if boundary == "batch" else None)
            seen.append(_statuses(world).get("parent.wb"))
        if boundary == "unfinished":
            world.write("\n" + json.dumps(resume[1]) + "\n", sid="parent")
            for _ in range(3):
                daemon.cycle(cold=True)
                seen.append(_statuses(world).get("parent.wb"))
        parent = daemon.by_session[("claude", "parent")]
        read = parent.consumed == parent.path.stat().st_size
    finally:
        daemon.close()
    assert read
    assert "failed" not in seen, seen
    assert seen[-1] == "live", seen


@pytest.mark.parametrize(
    ("parent", "status"),
    [
        # A resume call whose result is not written yet.
        pytest.param(
            [*_launch(2, "c1"), _ends(4, "c1", "stopped"), _call(5, "c2", resume=True)],
            "live",
            id="pending-resume",
        ),
        # Measured on this host: an earlier launch with no end notification
        # can still start agents after the end of a later launch.
        pytest.param(
            [
                *_launch(2, "c1"),
                *_launch(4, "c2", resume=True),
                _ends(6, "c2", "stopped"),
            ],
            "live",
            id="earlier-launch-open",
        ),
        pytest.param(
            [
                *_launch(2, "c1"),
                *_launch(4, "c2", resume=True),
                _ends(6, "c1", "stopped"),
            ],
            "live",
            id="delayed-notification",
        ),
        pytest.param(
            [
                *_launch(2, "c1"),
                *_launch(4, "c2", resume=True),
                _ends(6, "c1", "stopped"),
                _ends(7, "c2", "stopped"),
            ],
            "failed",
            id="every-launch-ended",
        ),
    ],
)
def test_an_open_launch_of_the_run_blocks_the_end_of_another(
    world: World, parent: list[dict[str, Any]], status: str
) -> None:
    """Spec PR 1944, failure mode 4, and the narrowing of rule 5. The end of
    one launch gives no inferred status while another launch of the same run
    has no end. That launch can be a resume call that waits for its result.
    It can also be an earlier launch with no end in the parent, or a later
    launch.
    The agents' clocks are behind every end, so no time comparison can give
    this result."""
    _hold(world, "parent")
    _started(world, "parent", "wa")
    _started(world, "parent", "wb")
    world.write([user(1, "parent prompt"), *parent], sid="parent")
    daemon = world.daemon()
    try:
        drain(daemon)
        for _ in range(3):
            daemon.cycle(cold=True)
        statuses = _statuses(world)
    finally:
        daemon.close()
    assert statuses == {
        "parent": "live",
        "parent.wa": status,
        "parent.wb": status,
    }, statuses


@pytest.mark.parametrize("stamp", ["backward", "equal", "missing", "malformed"])
def test_record_times_do_not_change_the_workflow_rule(world: World, stamp: str) -> None:
    """Spec PR 1944, failure mode 5. The time of the end notification is
    earlier than the agent's records, equal to its last record, missing or not
    a time. The causal records are valid, so the agent is `failed` in each
    case."""
    _hold(world, "parent")
    end = _ends(4, "c1", "stopped")
    if stamp == "backward":
        end["timestamp"] = "2026-10-22T09:00:00.000Z"
    elif stamp == "equal":
        end["timestamp"] = "2026-10-22T10:00:02.000Z"
    elif stamp == "missing":
        del end["timestamp"]
    else:
        end["timestamp"] = "not a time"
    world.write([user(1, "parent prompt"), *_launch(2, "c1"), end], sid="parent")
    _started(world, "parent", "wa")
    daemon = world.daemon()
    try:
        drain(daemon)
        for _ in range(3):
            daemon.cycle(cold=True)
        statuses = _statuses(world)
    finally:
        daemon.close()
    assert statuses == {"parent": "live", "parent.wa": "failed"}, statuses


def test_a_late_member_gets_its_certificate_before_the_launch_retires(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec PR 1944, failure modes 6 and 11. The journal finds agent `wa`
    before the stop. Agent `wb` exists before the end notification, but the
    scans find it only later. The listing of the run directory after the end
    holds both. So the launch keeps both members, and it retires only after
    `wb` has its certificate. A restart keeps both agents `failed`."""
    _hold(world, "parent")
    world.write([user(1, "parent prompt"), *_launch(2, "c1")], sid="parent")
    _started(world, "parent", "wa")
    hidden = _hide(monkeypatch, "agent-wb.jsonl")
    daemon = world.daemon()
    try:
        drain(daemon)
        _started(world, "parent", "wb")
        world.write([_ends(4, "c1", "stopped")], sid="parent")
        for _ in range(3):
            daemon.cycle(cold=True)
        before = _statuses(world)
        waiting = _members(_runs(world), "wf_x")
        hidden[0] = False
        for _ in range(3):
            daemon.cycle(cold=True)
        after = _statuses(world)
        retired = _runs(world)
    finally:
        daemon.close()
    daemon = world.daemon()
    try:
        for _ in range(3):
            daemon.cycle(cold=True)
        restarted = _statuses(world)
    finally:
        daemon.close()
    assert before == {"parent": "live", "parent.wa": "failed"}, before
    assert waiting == ["wa", "wb"], waiting
    assert after == {**before, "parent.wb": "failed"}, after
    assert retired == {}, retired
    assert restarted == after, restarted


@pytest.mark.parametrize("folder", ["journal-only", "none"])
def test_a_launch_with_no_agent_files_retires_with_zero_members(
    world: World, folder: str
) -> None:
    """Spec PR 1944, failure modes 6 and 11. A complete listing of the run
    directory with no agent file is a true zero membership. So the ended
    launch leaves the retained state."""
    world.write(
        [user(1, "parent prompt"), *_launch(2, "c1"), _ends(4, "c1", "completed")],
        sid="parent",
    )
    if folder == "journal-only":
        flow = world.projects / "parent" / "subagents" / "workflows" / "wf_x"
        flow.mkdir(parents=True)
        (flow / "journal.jsonl").write_text(_workflow_lines(LAUNCHED))
    daemon = world.daemon()
    try:
        drain(daemon)
        for _ in range(3):
            daemon.cycle(cold=True)
    finally:
        daemon.close()
    assert _runs(world) == {}, _runs(world)


def test_a_resume_call_after_the_listing_restarts_the_closing_sequence(
    world: World,
) -> None:
    """Spec PR 1944, failure mode 11. The journal reads the end of `c1` and
    lists the run directory. Before its second read of the parent, a resume
    call of the same run lands. The journal does the sequence again, and no
    agent gets an inferred status from the old end."""
    _hold(world, "parent")
    world.write([user(1, "parent prompt"), *_launch(2, "c1")], sid="parent")
    _started(world, "parent", "wa")
    daemon = world.daemon()
    seen: list[dict[str, str]] = []
    try:
        drain(daemon)
        world.write([_ends(4, "c1", "stopped")], sid="parent")
        daemon.cycle(cold=True)
        seen.append(_statuses(world))
        world.write([_call(5, "c2", resume=True)], sid="parent")
        _started(world, "parent", "wb")
        for _ in range(3):
            daemon.cycle(cold=True)
            seen.append(_statuses(world))
        world.write([_launched(6, "c2")], sid="parent")
        for _ in range(3):
            daemon.cycle(cold=True)
            seen.append(_statuses(world))
    finally:
        daemon.close()
    assert all("failed" not in statuses.values() for statuses in seen), seen
    assert seen[-1] == {
        "parent": "live",
        "parent.wa": "live",
        "parent.wb": "live",
    }, seen[-1]


def test_an_unfinished_workflow_journal_gives_no_inferred_status(
    world: World,
) -> None:
    """Spec PR 1944, failure mode 6. The Workflow journal ends in a record
    with no line feed. Then the journal keeps the launch and gives no
    inferred status. When the record is complete, the agent's own `result`
    applies. The listing failures are in
    `test_a_failed_listing_keeps_the_launch_and_shows_its_reason`."""
    _hold(world, "parent")
    world.write([user(1, "parent prompt"), *_launch(2, "c1")], sid="parent")
    journal = _started(world, "parent", "wa")
    daemon = world.daemon()
    seen: list[str | None] = []
    try:
        drain(daemon)
        with journal.open("a") as stream:
            stream.write('{"type": "result", "agentId": "wa", "key": "k"')
        world.write([_ends(4, "c1", "stopped")], sid="parent")
        for _ in range(4):
            daemon.cycle(cold=True)
            seen.append(_statuses(world).get("parent.wa"))
        kept = "wf_x" in _runs(world)
        with journal.open("a") as stream:
            stream.write(', "result": "r"}\n')
        for _ in range(3):
            daemon.cycle(cold=True)
        final = _statuses(world).get("parent.wa")
        retired = _runs(world)
    finally:
        daemon.close()
    assert seen == ["live"] * 4, seen
    assert kept
    assert final == "ended", final
    assert retired == {}, retired


@pytest.mark.parametrize("later", ["restart", "grown", "reused"])
def test_a_certificate_outlives_its_launch_and_holds_only_for_its_file(
    world: World, later: str
) -> None:
    """Spec PR 1944, failure mode 7. After the launch retires, a restart and
    cold cycles keep the certified agent `failed`. The agent file can get a
    record after its certificate, alone or in a resume that reuses the agent
    id. That record voids the certificate, also when its time is behind the
    end."""
    _hold(world, "parent")
    world.write([user(1, "parent prompt"), *_launch(2, "c1")], sid="parent")
    journal = _started(world, "parent", "wa")
    daemon = world.daemon()
    try:
        drain(daemon)
        world.write([_ends(4, "c1", "stopped")], sid="parent")
        for _ in range(3):
            daemon.cycle(cold=True)
        certified = _statuses(world)
        retired = _runs(world)
        if later == "restart":
            daemon.close()
            daemon = world.daemon()
        else:
            if later == "reused":
                world.write(_launch(5, "c2", resume=True), sid="parent")
                with journal.open("a") as stream:
                    stream.write(_workflow_lines(STARTED))
            more = {**user(3, "more work"), "isSidechain": True, "agentId": "wa"}
            with (journal.parent / "agent-wa.jsonl").open("a") as stream:
                stream.write(json.dumps(more) + "\n")
        for _ in range(3):
            daemon.cycle(cold=True)
        statuses = _statuses(world)
        held = _certificate_of(world)
    finally:
        daemon.close()
    assert certified == {"parent": "live", "parent.wa": "failed"}, certified
    assert retired == {}, retired
    assert statuses["parent.wa"] == ("failed" if later == "restart" else "live")
    # The cycle that sees the growth deletes the certificate.
    assert (held is None) == (later != "restart"), held


def test_a_cycle_that_cannot_stat_a_certified_agent_keeps_it_failed(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec PR 1944, failure mode 7. A cycle whose `lstat` of a certified
    agent fails cannot check the certificate. The agent keeps its `failed`
    status, and the next cycle checks the certificate again."""
    _hold(world, "parent")
    world.write([user(1, "parent prompt"), *_launch(2, "c1")], sid="parent")
    _started(world, "parent", "wa")
    daemon = world.daemon()
    try:
        drain(daemon)
        world.write([_ends(4, "c1", "stopped")], sid="parent")
        for _ in range(3):
            daemon.cycle(cold=True)
        before = _last_status(world, "parent.wa")
        lstat = Path.lstat

        def failing(self: Path) -> Any:
            if self.name == "agent-wa.jsonl":
                raise PermissionError(errno.EACCES, "denied", str(self))
            return lstat(self)

        monkeypatch.setattr(Path, "lstat", failing)
        daemon.cycle(cold=True)
        during = _last_status(world, "parent.wa")
        monkeypatch.undo()
        drain(daemon)
        for _ in range(2):
            daemon.cycle(cold=True)
        after = _last_status(world, "parent.wa")
    finally:
        daemon.close()
    assert before == during == after == ("failed", None), (before, during, after)


def _certificate_of(world: World, sid: str = "parent.wa") -> Any:
    """The terminal certificate that the committed state of `sid` holds."""
    state = json.loads(
        world.table("SELECT state FROM journal_source WHERE session = ?", (sid,))[0][0]
    )
    return state.get("workflow_certificate")


def _certify(world: World) -> tuple[serve.Daemon, Path]:
    """A daemon after launch `c1` of run `wf_x` stopped, its agent `wa` got
    its terminal certificate and the launch retired. Also the file of
    `wa`."""
    _hold(world, "parent")
    world.write([user(1, "parent prompt"), *_launch(2, "c1")], sid="parent")
    journal = _started(world, "parent", "wa")
    daemon = world.daemon()
    drain(daemon)
    world.write([_ends(4, "c1", "stopped")], sid="parent")
    for _ in range(3):
        daemon.cycle(cold=True)
    return daemon, journal.parent / "agent-wa.jsonl"


def _rewrite(path: Path, data: bytes) -> None:
    """Write `data` over the file at `path` in place, with the same length,
    and move its modification time one second later."""
    before = path.stat()
    assert len(data) == before.st_size
    with path.open("r+b") as stream:
        stream.write(data)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))


@pytest.mark.parametrize("change", ["modification-time", "equal-length-rewrite"])
@pytest.mark.parametrize("later", ["cycles", "restart"])
def test_the_producer_model_bounds_the_certificate(
    world: World, change: str, later: str
) -> None:
    """Spec PR 1944, failure mode 7, rule 4. A change of the modification
    time alone keeps the certificate, so a stopped agent does not become
    `live` again. The test also pins the documented limit of the rule. Claude
    Code only appends to an agent file, and it gives each launch new agent
    ids. So a rewrite in place with the same length and other records comes
    only from another writer. Such a rewrite keeps the certificate and the
    status `failed`. Both results hold after a restart."""
    daemon, path = _certify(world)
    try:
        held = _certificate_of(world)
        data = path.read_bytes()
        if change == "equal-length-rewrite":
            data = data.replace(b'"done"', b'"busy"')
        _rewrite(path, data)
        drain(daemon)
        for _ in range(3):
            daemon.cycle(cold=True)
        if later == "restart":
            daemon.close()
            daemon = world.daemon()
            drain(daemon)
            for _ in range(3):
                daemon.cycle(cold=True)
        statuses = _statuses(world)
        kept = _certificate_of(world)
    finally:
        daemon.close()
    assert statuses == {"parent": "live", "parent.wa": "failed"}, statuses
    assert held is not None and kept == held, (held, kept)


@pytest.mark.parametrize("change", ["shrink", "replace"])
def test_a_smaller_or_replaced_agent_file_voids_the_certificate(
    world: World, change: str
) -> None:
    """Spec PR 1944, failure mode 7, rule 4. A smaller size or another inode
    voids the certificate, and the cycle that sees it deletes it. The
    refusal rules of the journal still apply. A source that shrinks, or a
    transcript that another file replaces, gets the status `failed` with
    that reason. The certificate does not give that status."""
    daemon, path = _certify(world)
    try:
        held = _certificate_of(world)
        data = path.read_bytes()
        if change == "shrink":
            with path.open("r+b") as stream:
                stream.truncate(len(data) - 1)
        else:
            copy = path.with_name("agent-wa.copy")
            copy.write_bytes(data)
            os.replace(copy, path)
        for _ in range(3):
            daemon.cycle(cold=True)
        last = _last_status(world, "parent.wa")
        voided = _certificate_of(world)
    finally:
        daemon.close()
    reason = "source shrank" if change == "shrink" else "transcript replaced"
    assert held is not None
    assert voided is None, voided
    assert last == ("failed", reason), last


@pytest.mark.parametrize("started", ["wa", "wc"])
@pytest.mark.parametrize("later", ["cycles", "restart"])
def test_a_later_launch_that_starts_a_certified_agent_voids_its_certificate(
    world: World, started: str, later: str
) -> None:
    """Spec PR 1944, failure mode 7, review r3 F1. After `wa` got its
    certificate, a resume of the same run starts an agent. The Workflow
    call and its launch result name no agent id: the `started` line that
    the later launch writes in the run's Workflow journal names it. When it
    names `wa`, the certificate of `wa` is void before `agent-wa.jsonl`
    changes, so `wa` is `live`. When it names another agent, `wa` stays
    `failed`. A restart keeps the result."""
    daemon, path = _certify(world)
    try:
        world.write(_launch(5, "c2", resume=True), sid="parent")
        with (path.parent / "journal.jsonl").open("a") as stream:
            stream.write(_workflow_lines({**STARTED, "agentId": started}))
        for _ in range(3):
            daemon.cycle(cold=True)
        statuses = _statuses(world)
        held = _certificate_of(world)
        if later == "restart":
            daemon.close()
            daemon = world.daemon()
            drain(daemon)
            for _ in range(3):
                daemon.cycle(cold=True)
        final = _statuses(world)
    finally:
        daemon.close()
    expected = "live" if started == "wa" else "failed"
    assert statuses == {"parent": "live", "parent.wa": expected}, statuses
    assert (held is None) == (started == "wa"), held
    assert final == statuses, final


def test_a_later_start_voids_the_certificate_of_an_agent_with_its_own_end(
    world: World,
) -> None:
    """Spec PR 1944, failure mode 7, rule 4, review r4 F3. After `wa` got its
    certificate, the Workflow journal gets a late `result` line of `wa`.
    Then a resume of the run writes a new `started` line of `wa`, before
    `agent-wa.jsonl` changes. The own end line stays the status of `wa`, as
    on main. But the new `started` line voids the certificate in that sweep
    too, and a restart does not bring it back. So when the Workflow journal
    is refused later, `wa` gets the other rules with the refusal as its
    reason, not an inferred `failed`."""
    daemon, path = _certify(world)
    journal = path.parent / "journal.jsonl"
    try:
        certified = _statuses(world)
        with journal.open("a") as stream:
            stream.write(
                _workflow_lines(
                    {"type": "result", "agentId": "wa", "key": "k", "result": "r"}
                )
            )
        world.write(_launch(5, "c2", resume=True), sid="parent")
        with journal.open("a") as stream:
            stream.write(_workflow_lines(STARTED))
        for _ in range(3):
            daemon.cycle(cold=True)
        own = _statuses(world)
        voided = _certificate_of(world)
        daemon.close()
        daemon = world.daemon()
        drain(daemon)
        for _ in range(3):
            daemon.cycle(cold=True)
        restarted = _certificate_of(world)
        # A second link makes the reader refuse the Workflow journal.
        os.link(journal, journal.with_name("journal.link"))
        for _ in range(3):
            daemon.cycle(cold=True)
        refused = _last_status(world, "parent.wa")
    finally:
        daemon.close()
    assert certified == {"parent": "live", "parent.wa": "failed"}, certified
    assert own == {"parent": "live", "parent.wa": "ended"}, own
    assert voided is None, voided
    assert restarted is None, restarted
    assert refused == ("live", "workflow journal is not an owner regular file"), refused


@pytest.mark.parametrize("later", ["started", "none"])
def test_a_refused_journal_gives_main_s_row_and_keeps_the_certificate(
    world: World, later: str
) -> None:
    """Spec PR 1944 revision h, failure mode 7, review r5 F4. After `wa` got
    its certificate, a resume of the run can write a new `started` line of
    `wa` before `agent-wa.jsonl` changes. When a second link makes the
    reader refuse the Workflow journal before a sweep reads that line, the
    journal cannot see it. So the certificate gives no status: `wa` gets the
    row of main, the live-parent rule with the refusal as its reason. The
    certificate stays. The next read of the journal decides by rule 4: the
    new `started` line voids the certificate, so `wa` is `live`. With no new
    `started` line, `wa` is `failed` again, also after a restart."""
    daemon, path = _certify(world)
    journal = path.parent / "journal.jsonl"
    link = journal.with_name("journal.link")
    try:
        certified = _statuses(world)
        held = _certificate_of(world)
        if later == "started":
            world.write(_launch(5, "c2", resume=True), sid="parent")
            with journal.open("a") as stream:
                stream.write(_workflow_lines(STARTED))
        os.link(journal, link)
        for _ in range(3):
            daemon.cycle(cold=True)
        refused = _last_status(world, "parent.wa")
        kept = _certificate_of(world)
        link.unlink()
        for _ in range(3):
            daemon.cycle(cold=True)
        read = (_last_status(world, "parent.wa"), _certificate_of(world))
        daemon.close()
        daemon = world.daemon()
        drain(daemon)
        for _ in range(3):
            daemon.cycle(cold=True)
        restarted = (_last_status(world, "parent.wa"), _certificate_of(world))
    finally:
        daemon.close()
    assert certified == {"parent": "live", "parent.wa": "failed"}, certified
    assert refused == ("live", "workflow journal is not an owner regular file"), refused
    assert held is not None and kept == held, (held, kept)
    expected = (
        (("live", None), None) if later == "started" else (("failed", None), held)
    )
    assert read == expected, read
    assert restarted == expected, restarted


def _hide(monkeypatch: pytest.MonkeyPatch, name: str) -> list[bool]:
    """Keep the agent file `name` out of each scan until the test clears the
    flag."""
    hidden = [True]
    discover = serve.Daemon._discover

    def late(self: serve.Daemon) -> list[Any]:
        return [
            item for item in discover(self) if not (hidden[0] and item[2].name == name)
        ]

    monkeypatch.setattr(serve.Daemon, "_discover", late)
    return hidden


def _held(world: World) -> Any:
    """The committed certificate of agent `wa`. None also before the journal
    has a row for `wa`."""
    rows = world.table("SELECT 1 FROM journal_source WHERE session = 'parent.wa'")
    return _certificate_of(world) if rows else None


def _valid_certificate(world: World, path: Path) -> bool:
    """Whether the committed certificate of agent `wa` holds for its file at
    `path` and for the `started` lines of `wa` in the Workflow journal now."""
    held = _held(world)
    if not isinstance(held, dict):
        return False
    info = os.lstat(path)
    starts = sum(
        1
        for line in (path.parent / "journal.jsonl").read_text().splitlines()
        if json.loads(line).get("type") == "started"
        and json.loads(line).get("agentId") == "wa"
    )
    return (
        held.get("agent"),
        held.get("run"),
        held.get("device"),
        held.get("inode"),
        held.get("size"),
        held.get("started"),
    ) == ("wa", "wf_x", info.st_dev, info.st_ino, info.st_size, starts)


def _refused_agent(daemon: serve.Daemon) -> bool:
    """Whether the journal refuses the source of agent `wa`, which is then
    terminal evidence of its own."""
    return any(key[2].endswith("agent-wa.jsonl") for key in daemon.failed)


READERS = ("membership", "reconciliation", "retirement", "status")
VOIDS = ("growth", "smaller", "replace", "started", "started-in-refusal")


def _watch(
    monkeypatch: pytest.MonkeyPatch,
    world: World,
    path: Path,
    reader: str,
    refused: list[bool],
) -> tuple[list[str], list[int]]:
    """Record each use of a certificate of agent `wa` (file `path`) by
    `reader` that the certificate does not support. Also count the calls
    of the reader. While the test sets `refused`, the journal keeps the
    certificate by rule 4. Then only the use of it counts."""
    misses: list[str] = []
    calls = [0]

    def held_is_void() -> bool:
        return (
            not refused[0]
            and _held(world) is not None
            and not _valid_certificate(world, path)
        )

    if reader == "membership":
        certify = serve.Daemon._certify

        def membership(
            self: serve.Daemon, parent: Any, run: str, *rest: Any
        ) -> list[str]:
            certificates = rest[-1]
            before = len(certificates)
            left: list[str] = certify(self, parent, run, *rest)
            calls[0] += 1
            new = {held["agent"] for _, held in certificates[before:]}
            if (
                "wa" not in left
                and "wa" not in new
                and not _refused_agent(self)
                and not _valid_certificate(world, path)
            ):
                misses.append(f"settled on {_held(world)}")
            return left

        monkeypatch.setattr(serve.Daemon, "_certify", membership)
    elif reader == "reconciliation":
        reconcile = serve.Daemon._reconcile
        certify = serve.Daemon._certify
        reached = [False]

        def members(self: serve.Daemon, *rest: Any) -> list[str]:
            reached[0] = True
            left: list[str] = certify(self, *rest)
            return left

        def reconciliation(self: serve.Daemon, parent: Any) -> None:
            reached[0] = False
            reconcile(self, parent)
            if reached[0]:
                # This reconciliation read the members of the run.
                calls[0] += 1
                if held_is_void():
                    misses.append(f"kept {_held(world)}")

        monkeypatch.setattr(serve.Daemon, "_certify", members)
        monkeypatch.setattr(serve.Daemon, "_reconcile", reconciliation)
    elif reader == "retirement":
        settle = serve.Daemon._settle

        def retirement(self: serve.Daemon, parent: Any, *rest: Any) -> None:
            tracked = "wf_x" in _runs(world)
            settle(self, parent, *rest)
            calls[0] += 1
            if (
                tracked
                and "wf_x" not in _runs(world)
                and not _refused_agent(self)
                and not _valid_certificate(world, path)
            ):
                misses.append(f"retired on {_held(world)}")

        monkeypatch.setattr(serve.Daemon, "_settle", retirement)
    else:
        status = serve.Daemon._workflow_status

        def classify(self: serve.Daemon, source: Any) -> str | None:
            try:
                given: str | None = status(self, source)
            finally:
                if source.path == path:
                    calls[0] += 1
                    if held_is_void():
                        misses.append(f"kept {_held(world)}")
            if (
                source.path == path
                and given == "failed"
                and not _valid_certificate(world, path)
            ):
                misses.append(f"failed on {_held(world)}")
            return given

        monkeypatch.setattr(serve.Daemon, "_workflow_status", classify)
    return misses, calls


@pytest.mark.parametrize("event", VOIDS)
@pytest.mark.parametrize("reader", READERS)
def test_no_reader_uses_a_void_certificate(
    world: World, monkeypatch: pytest.MonkeyPatch, reader: str, event: str
) -> None:
    """Spec PR 1944, revision i, failure mode 7. Launch `c1` stops. Agent
    `wa` gets its certificate, and the run stays tracked because the scans
    find agent `wb` only later. Then one void event changes `wa` while the
    journal tracks the run. In each cell, the reader does not use the void
    certificate. Membership settles `wa` only on a valid certificate. A
    reconciliation that reads the members keeps no void certificate. The run
    retires only when `wa` holds a valid certificate or its own terminal
    evidence. The status `failed` comes only from a valid certificate. A
    restart keeps each result. While a second link refuses the Workflow
    journal, the certificate stays by rule 4, and only its use counts.

    In the `started` events, a resume `c2` of the run writes a new `started`
    line of `wa` and stops. A hot cycle reads the end of `c2` before any
    status read of `wa`, so membership reads the new `started` line first.
    In `started-in-refusal`, a second link of the Workflow journal hides
    that line until the closing sequence of `c2` has its members."""
    _hold(world, "parent")
    world.write([user(1, "parent prompt"), *_launch(2, "c1")], sid="parent")
    journal = _started(world, "parent", "wa")
    _started(world, "parent", "wb")
    path = journal.parent / "agent-wa.jsonl"
    hidden = _hide(monkeypatch, "agent-wb.jsonl")
    refused = [False]
    misses, calls = _watch(monkeypatch, world, path, reader, refused)
    link = journal.with_name("journal.link")
    daemon = world.daemon()
    try:
        drain(daemon)
        world.write([_ends(4, "c1", "stopped")], sid="parent")
        for _ in range(3):
            daemon.cycle(cold=True)
        held = _certificate_of(world)
        tracked = "wf_x" in _runs(world)
        calls[0] = 0
        if event == "growth":
            more = {**user(3, "more work"), "isSidechain": True, "agentId": "wa"}
            with path.open("a") as stream:
                stream.write(json.dumps(more) + "\n")
        elif event == "smaller":
            with path.open("r+b") as stream:
                stream.truncate(path.stat().st_size - 1)
        elif event == "replace":
            copy = path.with_name("agent-wa.copy")
            copy.write_bytes(path.read_bytes())
            os.replace(copy, path)
        else:
            world.write(_launch(5, "c2", resume=True), sid="parent")
            with journal.open("a") as stream:
                stream.write(_workflow_lines(STARTED))
            if event == "started-in-refusal":
                os.link(journal, link)
                refused[0] = True
            world.write([_ends(7, "c2", "stopped")], sid="parent")
            daemon.cycle()
            if event == "started-in-refusal":
                for _ in range(3):
                    daemon.cycle(cold=True)
                link.unlink()
                refused[0] = False
        for _ in range(3):
            daemon.cycle(cold=True)
        hidden[0] = False
        for _ in range(3):
            daemon.cycle(cold=True)
        retired = _runs(world)
        daemon.close()
        daemon = world.daemon()
        for _ in range(3):
            daemon.cycle(cold=True)
        last = _last_status(world, "parent.wa")
    finally:
        daemon.close()
    assert held is not None and tracked, (held, tracked)
    assert retired == {}, retired
    refusal = last[1] in ("source shrank", "transcript replaced")
    assert calls[0] > 0 or (reader == "status" and refusal), calls
    assert misses == [], misses


def test_a_run_does_not_retire_on_a_certificate_that_a_later_start_voids(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec PR 1944, revision i, review r6 F5. Launch `c1` stops. Agent
    `wa` gets its certificate, and `wb` keeps the run tracked. Resume `c2`
    writes a new `started` line of `wa` and stops. A second link of the
    Workflow journal hides that line. During the refusal, the scans find
    `wb` and the closing sequence of `c2` gets its members. Then the link
    goes, and one cold sweep reads the journal. The run must not retire on
    the old certificate of `wa`. Agent `wa` gets a new certificate for the
    new `started` line. It is `failed`, not `live`, after the retirement and
    after a restart."""
    _hold(world, "parent")
    world.write([user(1, "parent prompt"), *_launch(2, "c1")], sid="parent")
    journal = _started(world, "parent", "wa")
    path = journal.parent / "agent-wa.jsonl"
    hidden = _hide(monkeypatch, "agent-wb.jsonl")
    link = journal.with_name("journal.link")
    daemon = world.daemon()
    try:
        drain(daemon)
        _started(world, "parent", "wb")
        world.write([_ends(4, "c1", "stopped")], sid="parent")
        for _ in range(3):
            daemon.cycle(cold=True)
        old = _certificate_of(world)
        waiting = "wf_x" in _runs(world)
        world.write(_launch(5, "c2", resume=True), sid="parent")
        with journal.open("a") as stream:
            stream.write(_workflow_lines(STARTED))
        os.link(journal, link)
        world.write([_ends(7, "c2", "stopped")], sid="parent")
        hidden[0] = False
        for _ in range(4):
            daemon.cycle(cold=True)
        refused = _last_status(world, "parent.wa")
        link.unlink()
        daemon.cycle(cold=True)
        retired = (_runs(world), _certificate_of(world))
        swept = _statuses(world)
        valid = _valid_certificate(world, path)
        daemon.close()
        daemon = world.daemon()
        for _ in range(3):
            daemon.cycle(cold=True)
        restarted = _statuses(world)
    finally:
        daemon.close()
    assert old is not None and old["started"] == 1 and waiting, (old, waiting)
    assert refused == ("live", "workflow journal is not an owner regular file")
    assert retired[0] == {}, retired
    assert valid and retired[1]["started"] == 2, retired
    expected = {"parent": "live", "parent.wa": "failed", "parent.wb": "failed"}
    assert swept == expected, swept
    assert restarted == expected, restarted


class _Entry:
    """A listing entry whose file check fails with EIO."""

    def __init__(self, path: Path) -> None:
        self.name = path.name
        self.path = str(path)

    def is_file(self, *, follow_symlinks: bool = True) -> bool:
        raise OSError(errno.EIO, os.strerror(errno.EIO), self.path)

    def is_dir(self, *, follow_symlinks: bool = True) -> bool:
        return False


class _Entries:
    """A listing of the run folder `flow` with one entry, `agent-wa.jsonl`,
    whose file check fails."""

    def __init__(self, flow: Path) -> None:
        self.entries = [_Entry(flow / "agent-wa.jsonl")]

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def __iter__(self) -> Any:
        return iter(self.entries)


@pytest.mark.parametrize("failure", ["EACCES", "EIO", "entry-check"])
def test_a_failed_listing_keeps_the_launch_and_shows_its_reason(
    world: World, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """Spec PR 1944, failure mode 9, review r3 F2. The listing of the run
    folder fails: `os.scandir` with EACCES or EIO, or the file check of an
    entry. The journal keeps the launch and gives no inferred status. The
    reason of the failure is the reason of the status row of `wa`, as for a
    refused Workflow journal. It stays while a parent read is not valid. One log
    line names the folder and the reason. A later cycle lists the folder
    again: then `wa` is `failed` and the launch retires."""
    _hold(world, "parent")
    world.write([user(1, "parent prompt"), *_launch(2, "c1")], sid="parent")
    flow = _started(world, "parent", "wa").parent
    lines: list[str] = []
    daemon = world.daemon(log=lines.append)
    seen: list[tuple[str, str | None]] = []
    try:
        drain(daemon)
        # The scans found `wa`. Only the listing of the closing sequence
        # meets the failure.
        monkeypatch.setattr(serve.Daemon, "_discover", lambda self: [])
        scandir = os.scandir

        def listing(path: Any = ".") -> Any:
            if Path(path) != flow:
                return scandir(path)
            if failure == "entry-check":
                return _Entries(flow)
            code = errno.EACCES if failure == "EACCES" else errno.EIO
            raise OSError(code, os.strerror(code), str(path))

        monkeypatch.setattr(os, "scandir", listing)
        world.write([_ends(4, "c1", "stopped")], sid="parent")
        for _ in range(3):
            daemon.cycle(cold=True)
            seen.append(_last_status(world, "parent.wa"))
        # A Workflow call that waits for its result makes the parent read
        # invalid. The reason of the failed listing stays.
        world.write([_call(5, "c9")], sid="parent")
        for _ in range(2):
            daemon.cycle(cold=True)
            seen.append(_last_status(world, "parent.wa"))
        world.write([_launched(6, "c9", run="wf_y")], sid="parent")
        daemon.cycle(cold=True)
        seen.append(_last_status(world, "parent.wa"))
        kept = "wf_x" in _runs(world)
        monkeypatch.undo()
        for _ in range(3):
            daemon.cycle(cold=True)
        final = _last_status(world, "parent.wa")
        retired = _runs(world)
    finally:
        daemon.close()
    reason = "workflow folder listing failed: " + (
        "EACCES" if failure == "EACCES" else "EIO"
    )
    logged = [line for line in lines if "workflow folder listing failed" in line]
    assert seen == [("live", reason)] * 6, seen
    assert kept
    assert len(logged) == 1 and str(flow) in logged[0] and reason in logged[0], lines
    assert final == ("failed", None), final
    assert list(retired) == ["wf_y"], retired


def test_completed_workflows_leave_no_state_or_work_behind(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec PR 1944, failure mode 8. After one and after six reconciled
    Workflows, the journal appends ordinary parent records. The parent's
    committed state has the same size in both cases. These cycles run no
    reconciliation step, so completed history adds no work to a changed
    cycle."""
    _hold(world, "parent")
    world.write([user(1, "parent prompt")], sid="parent")
    steps = [0]
    close = getattr(serve.Daemon, "_close_run", None)
    if close is not None:

        def counting(self: serve.Daemon, *args: Any, **kwargs: Any) -> Any:
            steps[0] += 1
            return close(self, *args, **kwargs)

        monkeypatch.setattr(serve.Daemon, "_close_run", counting)

    def stop(k: int) -> None:
        run, call = f"wf_{k}", f"c{k}"
        world.write(
            [*_launch(10 * k, call, run=run), _ends(10 * k + 2, call, "stopped")],
            sid="parent",
        )
        _started(world, "parent", f"a{k}", run=run)

    def ordinary(base: int) -> tuple[int, int]:
        drain(daemon)
        for _ in range(3):
            daemon.cycle(cold=True)
        steps[0] = 0
        world.write([user(base + k, "next") for k in range(3)], sid="parent")
        drain(daemon)
        for _ in range(3):
            daemon.cycle(cold=True)
        state = json.loads(
            world.table(
                "SELECT state FROM journal_source WHERE session = ?", ("parent",)
            )[0][0]
        )
        # `turn_at` is the byte offset of the last turn line. Its digits grow
        # with the transcript, not with the completed history.
        assert type(state.pop("turn_at")) is int
        return len(json.dumps(state)), steps[0]

    daemon = world.daemon()
    try:
        stop(1)
        one = ordinary(300)
        for k in range(2, 7):
            stop(k)
        six = ordinary(310)
        statuses = _statuses(world)
    finally:
        daemon.close()
    assert statuses == {
        "parent": "live",
        **{f"parent.a{k}": "failed" for k in range(1, 7)},
    }, statuses
    assert six == one, (one, six)
    assert one[1] == 0, one


@pytest.mark.parametrize("change", ["rewrite", "replace", "unfinished"])
def test_a_changed_parent_authorizes_no_stale_end(world: World, change: str) -> None:
    """Spec PR 1944, failure mode 9. After the journal reads the end, the
    parent is rewritten in place, replaced, or holds a record with no line
    feed. No status sweep gives the agent an inferred `failed` from that
    parent read. A completed record ends the wait."""
    _hold(world, "parent")
    world.write([user(1, "parent prompt"), *_launch(2, "c1")], sid="parent")
    _started(world, "parent", "wa")
    daemon = world.daemon()
    seen: list[str | None] = []
    try:
        drain(daemon)
        world.write([_ends(4, "c1", "stopped")], sid="parent")
        path = world.source("parent")
        if change == "unfinished":
            world.write(json.dumps(user(5, "next")), sid="parent")
        daemon.cycle(cold=True)
        seen.append(_statuses(world).get("parent.wa"))
        if change == "rewrite":
            data = path.read_bytes()
            path.write_bytes(
                data.replace(b"<status>stopped</status>", b"<status>running</status>")
            )
        elif change == "replace":
            copy = path.with_name("parent.copy")
            copy.write_bytes(path.read_bytes())
            os.replace(copy, path)
        for _ in range(4):
            daemon.cycle(cold=True)
            seen.append(_statuses(world).get("parent.wa"))
        if change == "unfinished":
            world.write("\n", sid="parent")
            for _ in range(3):
                daemon.cycle(cold=True)
        final = _statuses(world).get("parent.wa")
    finally:
        daemon.close()
    assert "failed" not in seen, seen
    assert final == ("failed" if change == "unfinished" else "live"), final


def test_a_parent_with_no_retained_identity_gives_no_inferred_status(
    world: World,
) -> None:
    """Spec PR 1944, failure mode 9. The committed row of the parent keeps no
    source identity, as a release before the activity fence wrote it. The
    journal cannot show that a rewrite or a replacement did not change the
    bytes it read, so the end gives no inferred status."""
    _hold(world, "parent")
    world.write([user(1, "parent prompt"), *_launch(2, "c1")], sid="parent")
    _started(world, "parent", "wa")
    run(world).close()
    connection = nott_database.connect(world.store)
    try:
        with nott_database.write(connection):
            connection.execute(
                "UPDATE journal_source SET activity = NULL WHERE session = ?",
                ("parent",),
            )
    finally:
        connection.close()
    world.write([_ends(4, "c1", "stopped")], sid="parent")
    daemon = world.daemon()
    try:
        drain(daemon)
        for _ in range(3):
            daemon.cycle(cold=True)
        statuses = _statuses(world)
    finally:
        daemon.close()
    assert statuses == {"parent": "live", "parent.wa": "live"}, statuses


def test_a_refused_workflow_journal_keeps_the_launch_and_names_the_refusal(
    world: World,
) -> None:
    """Spec PR 1944, failure mode 9. A refused Workflow journal gives no
    evidence of the agent's own end. So the launch keeps the agent as an
    open member, and the refusal is the reason of its `live` status row."""
    _hold(world, "parent")
    world.write([user(1, "parent prompt"), *_launch(2, "c1")], sid="parent")
    journal = _started(world, "parent", "wa")
    daemon = world.daemon()
    try:
        drain(daemon)
        os.link(journal, world.root / "outside.jsonl")
        world.write([_ends(4, "c1", "stopped")], sid="parent")
        for _ in range(4):
            daemon.cycle(cold=True)
        kept = "wf_x" in _runs(world)
    finally:
        daemon.close()
    assert kept
    assert _last_status(world, "parent.wa") == (
        "live",
        "workflow journal is not an owner regular file",
    )


@pytest.mark.parametrize("later", ["new-run", "resume"])
def test_a_legacy_parent_gets_no_scan_and_tracks_new_runs(
    world: World, later: str
) -> None:
    """Spec PR 1944, failure mode 10. The journal read the parent with launch
    `c1` before this release, so its state has no Workflow keys. Its end comes
    after the upgrade, and the agent of `c1` keeps the other rules: `live`.
    The journal reads no parent byte for it. A new run launched after the
    upgrade works. The journal does not track a resume of the old run, because it did not
    read the first launch of that run."""
    _hold(world, "parent")
    world.write([user(1, "parent prompt"), *_launch(2, "c1")], sid="parent")
    _started(world, "parent", "wa")
    run(world).close()
    connection = nott_database.connect(world.store)
    try:
        with nott_database.write(connection):
            for path, saved in nott_database.query(
                connection, "SELECT path, state FROM journal_source"
            ):
                old = {
                    key: value
                    for key, value in json.loads(saved).items()
                    if key
                    not in (
                        "workflows",
                        "workflow_calls",
                        "workflow_pending",
                        "workflow_map",
                    )
                }
                connection.execute(
                    "UPDATE journal_source SET state = ? WHERE path = ?",
                    (json.dumps(old), path),
                )
    finally:
        connection.close()
    world.write([_ends(4, "c1", "stopped")], sid="parent")
    if later == "new-run":
        world.write(
            [*_launch(5, "c3", run="wf_y"), _ends(7, "c3", "stopped")], sid="parent"
        )
        _started(world, "parent", "wy", run="wf_y")
        expected = {"parent.wa": "live", "parent.wy": "failed"}
    else:
        world.write(
            [*_launch(5, "c2", resume=True), _ends(7, "c2", "stopped")], sid="parent"
        )
        _started(world, "parent", "wb")
        expected = {"parent.wa": "live", "parent.wb": "live"}
    daemon = world.daemon()
    try:
        drain(daemon)
        for _ in range(3):
            daemon.cycle(cold=True)
        statuses = _statuses(world)
        read = daemon.metrics.read
        for _ in range(3):
            daemon.cycle(cold=True)
        again = daemon.metrics.read
    finally:
        daemon.close()
    assert statuses == {"parent": "live", **expected}, statuses
    assert again == read, (read, again)


def test_a_steady_cold_cycle_decodes_no_parent_state_per_agent(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review r1 of PR 1944, F2: an agent with no end line of its own reads the
    end of its Workflow from the parent's state. The journal decodes that state
    once for each change of it, not once for each agent on each cold cycle, so
    a steady cold cycle costs the same with one stopped Workflow as with
    five."""
    _hold(world, "parent")
    world.write([user(1, "parent prompt")], sid="parent")

    def stop(k: int) -> None:
        run, call = f"wf_{k}", f"c{k}"
        world.write(
            [*_launch(10 * k, call, run=run), _ends(10 * k + 2, call, "stopped")],
            sid="parent",
        )
        _workflow_agent(world, "parent", f"a{k}", None, run=run, n=10 * k).write_text(
            _workflow_lines(LAUNCHED, {**STARTED, "agentId": f"a{k}"})
        )

    def decodes() -> int:
        drain(daemon)
        daemon.cycle(cold=True)
        parent = daemon.by_session[("claude", "parent")]
        count = 0
        loads = json.loads

        def counting(data: Any, *args: Any, **kwargs: Any) -> Any:
            nonlocal count
            if isinstance(data, str) and data == parent.state:
                count += 1
            return loads(data, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(json, "loads", counting)
            for _ in range(3):
                daemon.cycle(cold=True)
        return count

    daemon = world.daemon()
    try:
        stop(1)
        one = decodes()
        for k in range(2, 6):
            stop(k)
        five = decodes()
        statuses = dict(world.table("SELECT id, status FROM journal_session"))
    finally:
        daemon.close()
    assert statuses == {
        "parent": "live",
        **{f"parent.a{k}": "failed" for k in range(1, 6)},
    }, statuses
    assert five == one, (one, five)


def test_workflow_journal_read_stops_at_its_fstat_size(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review r2 of PR 1931, F2: the reader reads at most the size of its
    `fstat` snapshot, which the byte ceiling bounds. Bytes that the workflow
    appends after the snapshot wait for the next read."""
    from nspawn.journal import serve

    _hold(world, "parent")
    world.write([user(1, "parent prompt")], sid="parent")
    flow = _workflow_agent(world, "parent", "wa", None)
    head = _workflow_lines(LAUNCHED, STARTED)
    tail = _workflow_lines({"type": "result", "agentId": "wa", "key": "k"})
    monkeypatch.setattr(serve, "WORKFLOW_JOURNAL_MAX_BYTES", len(head))
    parsed: list[int] = []
    real_ends = serve.workflow_events

    def ends(data: bytes) -> tuple[dict[str, str], dict[str, int]]:
        parsed.append(len(data))
        return real_ends(data)

    monkeypatch.setattr(serve, "workflow_events", ends)
    daemon = world.daemon()
    try:
        drain(daemon)
        flow.write_text(head)
        before = flow.stat()
        real_fstat = os.fstat
        appended: list[int] = []

        def fstat(descriptor: int) -> os.stat_result:
            # The seam: the workflow appends after the reader's snapshot.
            info = real_fstat(descriptor)
            if (info.st_dev, info.st_ino) == (before.st_dev, before.st_ino) and (
                not appended
            ):
                with flow.open("a") as stream:
                    appended.append(stream.write(tail))
            return info

        monkeypatch.setattr(os, "fstat", fstat)
        daemon.cycle(cold=True)
        monkeypatch.setattr(os, "fstat", real_fstat)
        statuses = dict(world.table("SELECT id, status FROM journal_session"))
    finally:
        daemon.close()
    assert appended == [len(tail)]
    assert parsed == [len(head)], parsed
    assert statuses["parent.wa"] == "live", statuses


@pytest.mark.parametrize(
    ("refusal", "reason"),
    [
        pytest.param("symlink", "workflow journal unavailable", id="symlink"),
        pytest.param("unreadable", "workflow journal unavailable", id="unreadable"),
        pytest.param(
            "directory",
            "workflow journal is not an owner regular file",
            id="directory",
        ),
        pytest.param("ceiling", "workflow journal exceeds byte ceiling", id="ceiling"),
        pytest.param("io-error", "workflow journal unavailable", id="io-error"),
        pytest.param(
            "two-links",
            "workflow journal is not an owner regular file",
            id="two-links",
        ),
        pytest.param(
            "lead-gone",
            "workflow journal is not an owner regular file",
            id="lead-gone",
        ),
    ],
)
def test_workflow_journal_refusal_is_a_reason_not_a_status(
    world: World, monkeypatch: pytest.MonkeyPatch, refusal: str, reason: str
) -> None:
    """Review r2 of PR 1931, F5, and review r3, B1: a refused Workflow journal
    gives no status of its own, because a refusal is not terminal evidence.
    The other rules give the status, as for an absent journal, and the
    refusal is the reason of that status row."""
    from nspawn.journal import serve

    if refusal != "lead-gone":
        _hold(world, "parent")
    world.write([user(1, "parent prompt")], sid="parent")
    flow = _workflow_agent(world, "parent", "wa", None)
    outside = world.root / "outside.jsonl"
    if refusal in ("two-links", "lead-gone"):
        flow.write_text(ENDED)
        os.link(flow, outside)
    elif refusal == "symlink":
        outside.write_text(ENDED)
        flow.symlink_to(outside)
    elif refusal == "unreadable":
        flow.write_text(ENDED)
        flow.chmod(0)
    elif refusal == "directory":
        flow.mkdir()
    elif refusal == "ceiling":
        flow.write_text(ENDED)
        monkeypatch.setattr(serve, "WORKFLOW_JOURNAL_MAX_BYTES", len(ENDED) - 1)
    else:
        flow.write_text(ENDED)
        before = flow.stat()
        real_fstat = os.fstat

        def fstat(descriptor: int) -> os.stat_result:
            info = real_fstat(descriptor)
            if (info.st_dev, info.st_ino) == (before.st_dev, before.st_ino):
                raise OSError(errno.EIO, os.strerror(errno.EIO))
            return info

        monkeypatch.setattr(os, "fstat", fstat)
    daemon = world.daemon()
    try:
        drain(daemon)
        daemon.cycle(cold=True)
        statuses = dict(world.table("SELECT id, status FROM journal_session"))
    finally:
        daemon.close()
    if refusal == "lead-gone":
        # No process holds the agent: the other rules give `ended`.
        assert statuses["parent.wa"] == "ended", statuses
        assert _last_status(world, "parent.wa") == ("ended", reason)
    else:
        assert statuses == {"parent": "live", "parent.wa": "live"}, statuses
        assert _last_status(world, "parent.wa") == ("live", reason)
    for row in world.rows("parent.wa"):
        validate(row)


def _short_read(
    monkeypatch: pytest.MonkeyPatch, flow: Path, mode: str
) -> Callable[[int, int, int], bytes]:
    """The `pread` seam on the Workflow journal `flow`. A call at offset 0
    gives the bytes up to the end of the `STARTED` line. A call at a larger
    offset gives the real bytes (`partial`), zero bytes (`eof`) or raises
    `OSError(EIO)` (`io-error`). A call on another file is real. The real
    `os.pread`, to remove the seam."""
    real = os.pread
    target = flow.stat()
    prefix = len(_workflow_lines(LAUNCHED, STARTED))

    def pread(descriptor: int, length: int, offset: int) -> bytes:
        info = os.fstat(descriptor)
        if (info.st_dev, info.st_ino) != (target.st_dev, target.st_ino):
            return real(descriptor, length, offset)
        if offset == 0:
            return real(descriptor, min(length, prefix), 0)
        if mode == "partial":
            return real(descriptor, length, offset)
        if mode == "eof":
            return b""
        raise OSError(errno.EIO, os.strerror(errno.EIO))

    monkeypatch.setattr(os, "pread", pread)
    return real


def test_workflow_journal_partial_read_continues_to_the_snapshot_size(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review r3 of PR 1931, B2: a read can give fewer bytes than it asks for.
    The reader then reads on from the new offset to the `fstat` size, and only
    the complete read goes into the cache."""
    _hold(world, "parent")
    world.write([user(1, "parent prompt")], sid="parent")
    flow = _workflow_agent(world, "parent", "wa", None)
    daemon = world.daemon()
    try:
        drain(daemon)
        flow.write_text(ENDED)
        _short_read(monkeypatch, flow, "partial")
        daemon.cycle(cold=True)
        statuses = dict(world.table("SELECT id, status FROM journal_session"))
        cached = daemon.workflow_journals.get(flow)
    finally:
        daemon.close()
    assert statuses["parent.wa"] == "ended", statuses
    assert cached is not None and cached[1] == serve.Observed(
        {"wa": "ended"}, {"wa": 1}, True
    ), cached


@pytest.mark.parametrize(
    ("mode", "reason"),
    [
        pytest.param("eof", "workflow journal shorter than its snapshot", id="eof"),
        pytest.param("io-error", "workflow journal unavailable", id="io-error"),
    ],
)
def test_workflow_journal_short_read_is_refused_and_not_cached(
    world: World, monkeypatch: pytest.MonkeyPatch, mode: str, reason: str
) -> None:
    """Review r3 of PR 1931, B2: a read that ends before the `fstat` size is a
    refusal, and the cache keeps no entry for it. The next read of the same
    unchanged journal reads it again and gives its end."""
    _hold(world, "parent")
    world.write([user(1, "parent prompt")], sid="parent")
    flow = _workflow_agent(world, "parent", "wa", None)
    daemon = world.daemon()
    try:
        drain(daemon)
        flow.write_text(ENDED)
        real = _short_read(monkeypatch, flow, mode)
        daemon.cycle(cold=True)
        assert _last_status(world, "parent.wa") == ("live", reason)
        assert flow not in daemon.workflow_journals
        monkeypatch.setattr(os, "pread", real)
        daemon.cycle(cold=True)
        assert _last_status(world, "parent.wa") == ("ended", None)
    finally:
        daemon.close()


@pytest.mark.parametrize(
    ("team", "changes", "status"),
    [
        pytest.param(None, {}, "ended", id="missing-team"),
        pytest.param(b"missing-config", {}, "ended", id="missing-config"),
        pytest.param(b'{"members": [{"name": "other"}]}', {}, "ended", id="unlisted"),
        pytest.param(b'{"members": []}', {}, "ended", id="empty-members"),
        pytest.param(b'{"members": [{"name": "worker"}]}', {}, "live", id="listed"),
        pytest.param(b'{"members":', {}, "live", id="malformed"),
        pytest.param(b"{}", {}, "live", id="no-members"),
        pytest.param(b'{"members": null}', {}, "live", id="null-members"),
        pytest.param(b'{"members": {}}', {}, "live", id="object-members"),
        pytest.param(b'{"members": [null]}', {}, "live", id="invalid-member"),
        pytest.param(b"null", {}, "live", id="null-team"),
        pytest.param(b"[]", {}, "live", id="array-team"),
        pytest.param(b"\xff", {}, "live", id="invalid-encoding"),
        pytest.param(b"directory", {}, "live", id="unreadable"),
        pytest.param(b"permission-denied", {}, "live", id="permission-denied"),
        pytest.param(b"symlink-config", {}, "live", id="symlink-config"),
        pytest.param(b"symlink-team", {}, "live", id="symlink-team"),
        pytest.param(b"symlink-teams", {}, "live", id="symlink-teams"),
        pytest.param(b"dangling-team", {}, "live", id="dangling-team"),
        pytest.param(None, {"taskKind": None}, "live", id="no-taskKind"),
        pytest.param(None, {"taskKind": "subagent"}, "live", id="other-taskKind"),
        pytest.param(None, {"teamName": 1}, "live", id="non-string-teamName"),
        pytest.param(None, {"name": 1}, "live", id="non-string-name"),
        pytest.param(
            b'{"members": [{"name": "worker"}]}',
            {"finished": True},
            "ended",
            id="finished-listed",
        ),
    ],
)
@pytest.mark.parametrize("after_import", [False, True], ids=["initial", "later"])
def test_teammate_team_status(
    world: World,
    capsys: pytest.CaptureFixture[str],
    team: bytes | None,
    changes: dict[str, Any],
    status: str,
    after_import: bool,
) -> None:
    """Team absence ends a thinking teammate, but uncertain reads keep its status."""
    _hold(world, "parent")
    world.write([user(1, "parent prompt")], sid="parent")
    metadata = {
        "taskKind": "in_process_teammate",
        "teamName": "team",
        "name": "worker",
    } | changes
    if metadata["taskKind"] is None:
        del metadata["taskKind"]
    world.source("parent").with_suffix(".meta.json").write_text(json.dumps(metadata))
    path = world.projects / "parent" / "subagents" / "agent-worker.jsonl"
    path.parent.mkdir(parents=True)
    thinking = agent(2, "m1", {"type": "thinking", "thinking": "work"}, usage(0)) | {
        "isSidechain": True,
        "agentId": "worker",
    }
    path.write_text(json.dumps(thinking) + "\n")

    def write_team() -> None:
        path.with_suffix(".meta.json").write_text(json.dumps(metadata))
        if team is None:
            return
        config = world.claude / "teams" / "team" / "config.json"
        if team in (b"symlink-team", b"symlink-teams", b"dangling-team"):
            target = world.root / "target"
            if team != b"dangling-team":
                target.mkdir()
                record = target / "config.json"
                if team == b"symlink-teams":
                    record = target / "team" / "config.json"
                    record.parent.mkdir()
                record.write_text('{"members": []}')
            link = config.parent.parent if team == b"symlink-teams" else config.parent
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(target, target_is_directory=True)
            return
        config.parent.mkdir(parents=True)
        if team == b"missing-config":
            return
        if team == b"directory":
            config.mkdir()
        elif team == b"symlink-config":
            target = world.root / "target.json"
            target.write_text('{"members": []}')
            config.symlink_to(target)
        elif team == b"permission-denied":
            config.write_text('{"members": []}')
            config.chmod(0)
        else:
            config.write_bytes(team)

    if not after_import:
        write_team()
    daemon = world.daemon()
    try:
        drain(daemon)
        if after_import:
            assert dict(world.table("SELECT id, status FROM journal_session")) == {
                "parent": "live",
                "parent.worker": "live",
            }
            write_team()
        daemon.cycle(cold=True)
        assert cli.main(["session", "list", "claude"]) == 0
        listed = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        assert {row["id"]: row["status"] for row in listed} == {
            "parent": "live",
            "parent.worker": status,
        }
    finally:
        daemon.close()


@pytest.mark.parametrize("team_name", ["../x", "/x", "x/y", "", "..", ".", "x..y"])
def test_teammate_refuses_unsafe_team_names(
    world: World,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    team_name: str,
) -> None:
    from nspawn.journal import serve

    _hold(world, "parent")
    world.write([user(1, "parent prompt")], sid="parent")
    path = world.projects / "parent" / "subagents" / "agent-worker.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            agent(2, "m1", {"type": "thinking", "thinking": "work"}, usage(0))
            | {"isSidechain": True, "agentId": "worker"}
        )
        + "\n"
    )
    path.with_suffix(".meta.json").write_text(
        json.dumps(
            {"taskKind": "in_process_teammate", "teamName": team_name, "name": "worker"}
        )
    )
    opened: list[Path] = []
    owner_open = serve.open_owner_path

    def record_open(home: Path, candidate: Path) -> int:
        opened.append(candidate)
        return owner_open(home, candidate)

    monkeypatch.setattr(serve, "open_owner_path", record_open)
    daemon = world.daemon()
    try:
        drain(daemon)
        daemon.cycle(cold=True)
        assert cli.main(["session", "list", "claude"]) == 0
        listed = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        assert {row["id"]: row["status"] for row in listed} == {
            "parent": "live",
            "parent.worker": "live",
        }
        assert path.with_suffix(".meta.json") in opened
        assert all(candidate.name != "config.json" for candidate in opened)
    finally:
        daemon.close()


def _previous_release_appends(world: World, records: list[dict[str, Any]]) -> None:
    """What the previous release's writer commits for appended lines: the same
    rows without native fields, no `journal_link` row, and only the columns it
    names (size, consumed, state)."""
    from dataclasses import replace

    import nott_database

    from nspawn.journal import serve

    path = world.write(records)
    connection = serve.open_store(world.store)
    try:
        consumed, state = nott_database.query(
            connection, "SELECT consumed, state FROM journal_source"
        )[0]
        translator = TRANSLATORS[0]
        source = serve.Source(
            translator, world.claude, None, path, world.source().stem, None
        )
        context = Context(
            world.claude, path, source.session, None, None, "2026-10-22T10:00:00Z"
        )
        saved = json.loads(state)
        rows: list[Any] = []
        used: list[Any] = []
        data = path.read_bytes()
        at = int(consumed)
        for raw in data[at:].splitlines(keepends=True):
            output = translator.translate(raw, at, saved, context)
            rows.extend(replace(row, native={}) for row in output.rows)
            used.extend(output.usage)
            at += len(raw)
        with nott_database.write(connection):
            serve.insert_rows(connection, source, rows, used, lambda value: value, 1)
            connection.execute(
                "UPDATE journal_source SET size = ?, consumed = ?, state = ?",
                (len(data), at, json.dumps(saved)),
            )
    finally:
        connection.close()


def test_a_rollback_and_reupgrade_keeps_the_history(world: World) -> None:
    """Review r1, blocker 3: the previous release appends turn T with no
    links; after the re-upgrade the history still reads T and the turn after."""
    from nspawn import threads

    world.write(
        [
            user(1, "root"),
            agent(2, "m1", text("first"), usage(1), stop_reason="end_turn")
            | {"parentUuid": "x1"},
        ]
    )
    daemon = world.daemon()
    try:
        drain(daemon)
    finally:
        daemon.close()
    _previous_release_appends(
        world,
        [
            user(3, "rollback prompt", parentUuid="x2"),
            agent(4, "m2", text("rollback answer"), usage(1), stop_reason="end_turn")
            | {"parentUuid": "x3"},
        ],
    )
    world.write([user(5, "after upgrade", parentUuid="x4")])
    daemon = world.daemon()
    try:
        drain(daemon)
    finally:
        daemon.close()
    shown = threads.journal_history(world.source().stem, world.claude, 100, 1 << 20)
    assert shown.get("reason") is None, shown
    assert [
        block["text"] if isinstance(block, dict) else block
        for message in shown["messages"]
        for block in (
            message["message"]["content"]
            if isinstance(message["message"]["content"], list)
            else [message["message"]["content"]]
        )
    ] == ["root", "first", "rollback prompt", "rollback answer", "after upgrade"]


def _texts(shown: dict[str, Any]) -> list[str]:
    return [
        block["text"] if isinstance(block, dict) else block
        for message in shown["messages"]
        for block in (
            message["message"]["content"]
            if isinstance(message["message"]["content"], list)
            else [message["message"]["content"]]
        )
    ]


def test_a_rollback_during_a_part_done_metadata_pass_keeps_the_history(
    world: World,
) -> None:
    """Review r2, finding A: the metadata pass is part-done when the previous
    release takes over and appends turn T; after the re-upgrade the pass reads
    up to the end, and `linked` never passes rows whose links are not written."""
    import nott_database

    from nspawn import threads

    world.write(
        [
            user(1, "root"),
            agent(2, "m1", text("first"), usage(1), stop_reason="end_turn")
            | {"parentUuid": "x1"},
            user(3, "second", parentUuid="x2"),
        ]
    )
    daemon = world.daemon()
    drain(daemon)
    # As the previous release left it: no link, no native field, no pass.
    with nott_database.write(daemon.connection):
        daemon.connection.execute("DELETE FROM journal_link")
        daemon.connection.execute(
            "UPDATE journal_source SET metadata_version = 0, linked = 0"
        )
        daemon.connection.execute(
            "UPDATE journal_row SET row = json_remove(row, '$.uuid', "
            "'$.parentUuid', '$.logicalParentUuid')"
        )
    daemon.close()
    # This release: a live turn commits, and the pass reads one line of three.
    daemon = world.daemon(batch_lines=1)
    try:
        world.write(
            [
                agent(4, "m2", text("live answer"), usage(1), stop_reason="end_turn")
                | {"parentUuid": "x3"}
            ]
        )
        daemon.cycle(cold=False, batches=2)
    finally:
        daemon.close()
    part_done = tuple(
        world.table("SELECT metadata_version, linked < consumed FROM journal_source")[0]
    )
    _previous_release_appends(
        world,
        [
            user(5, "rollback prompt", parentUuid="x4"),
            agent(6, "m3", text("rollback answer"), usage(1), stop_reason="end_turn")
            | {"parentUuid": "x5"},
        ],
    )
    world.write([user(7, "after upgrade", parentUuid="x6")])
    daemon = world.daemon()
    try:
        drain(daemon)
    finally:
        daemon.close()
    shown = threads.journal_history(world.source().stem, world.claude, 100, 1 << 20)
    assert shown.get("reason") is None, shown
    assert _texts(shown) == [
        "root",
        "first",
        "second",
        "live answer",
        "rollback prompt",
        "rollback answer",
        "after upgrade",
    ]
    assert part_done == (0, 1)
    assert world.table("SELECT linked = consumed FROM journal_source")[0][0] == 1


def test_a_deleted_transcript_keeps_its_stored_modified_time(world: World) -> None:
    """Review r1, blocker 4: a restart over a deleted source keeps the mtime
    the journal recorded; it never writes 0."""
    path = world.write([user(1, "kept")])
    os.utime(path, (1000, 1000))
    daemon = world.daemon()
    try:
        drain(daemon)
    finally:
        daemon.close()
    path.unlink()
    daemon = world.daemon()
    try:
        drain(daemon)
        daemon.cycle(cold=True)
    finally:
        daemon.close()
    assert world.table("SELECT modified FROM journal_session")[0][0] == 1000000


def test_a_backfill_takes_the_newest_session_first(world: World) -> None:
    """Review r1, point 6: the post-upgrade backfill runs newest first."""
    import nott_database

    # The store keeps sources in (vendor, home, path) order: the older file
    # sorts first, so only an order by modification time takes the newer one.
    older = world.write([user(1, "older")], sid="a-older")
    newer = world.write([user(1, "newer")], sid="b-newer")
    daemon = world.daemon()
    drain(daemon)
    with nott_database.write(daemon.connection):
        daemon.connection.execute("UPDATE journal_source SET metadata_version = 0")
    daemon.close()
    os.utime(older, (2000, 2000))
    os.utime(newer, (3000, 3000))
    daemon = world.daemon(batch_lines=1)
    try:
        daemon.cycle(cold=False, batches=1)
        assert dict(
            world.table("SELECT session, metadata_version FROM journal_source")
        ) == {"a-older": 0, "b-newer": 1}
    finally:
        daemon.close()


def test_a_backfill_of_a_gone_source_ends_with_the_stored_rows(world: World) -> None:
    """Review r1, point 5: a main-era session whose file is gone is not
    `importing` forever; its stored rows stay readable."""
    import nott_database

    from nspawn import threads

    path = world.write([user(1, "stored")])
    daemon = world.daemon()
    drain(daemon)
    with nott_database.write(daemon.connection):
        daemon.connection.execute("UPDATE journal_source SET metadata_version = 0")
        daemon.connection.execute("DELETE FROM journal_link")
    daemon.close()
    path.unlink()
    daemon = world.daemon()
    try:
        drain(daemon)
    finally:
        daemon.close()
    assert world.table("SELECT import_state FROM journal_session")[0][0] == "done"
    shown = threads.journal_history(world.source().stem, world.claude, 100, 1 << 20)
    assert [m["message"]["content"] for m in shown["messages"]] == ["stored"]


def test_a_home_through_a_symlink_keeps_its_stored_form(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review r1, point 8: main stored the home as configured; resolving it
    must not import every session again under a second id."""
    link = world.root / "home-link"
    link.symlink_to(world.home)
    configured = link / ".claude"
    monkeypatch.setenv("HOME", str(world.home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(configured))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(world.root / "runtime"))
    world.write([user(1, "once")])
    daemon = world.daemon(homes=[(None, configured)])
    try:
        drain(daemon)
    finally:
        daemon.close()
    daemon = world.daemon(homes=None)
    try:
        drain(daemon)
    finally:
        daemon.close()
    assert [
        tuple(row) for row in world.table("SELECT id, home FROM journal_session")
    ] == [(world.source().stem, str(configured))]


# -- unit scan: the session journal reads only new bytes --------------------------------


def _preads(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, int, int]]:
    """Each pread from now on, as (inode, offset, returned length)."""
    seen: list[tuple[int, int, int]] = []
    original = os.pread

    def audit(fd: int, count: int, offset: int) -> bytes:
        data = original(fd, count, offset)
        seen.append((os.fstat(fd).st_ino, offset, len(data)))
        return data

    monkeypatch.setattr(os, "pread", audit)
    return seen


def _on(seen: list[tuple[int, int, int]], path: Path) -> list[tuple[int, int]]:
    """The (offset, length) of each pread on the file at `path`."""
    inode = path.stat().st_ino
    return [(offset, length) for found, offset, length in seen if found == inode]


def _once(reads: list[tuple[int, int]], size: int) -> None:
    """The preads take the bytes from 0 to `size` in order, each byte once."""
    at = 0
    for offset, length in reads:
        assert offset == at, reads
        at += length
    assert at == size, reads


def _line(n: int, length: int) -> bytes:
    """One user record of exactly `length` bytes with its LF."""
    empty = json.dumps(user(n, ""))
    line = json.dumps(user(n, "x" * (length - 1 - len(empty)))) + "\n"
    assert len(line) == length
    return line.encode()


def _deleted(line: str) -> int:
    """The `bytes deleted by the bound` of one minute line."""
    found = re.search(r"bytes deleted by the bound ([0-9]+)", line)
    assert found is not None, line
    return int(found[1])


def _imported(world: World, sid: str = SID) -> list[str]:
    """The rows of `sid`, with the vendor home of `world` as `H`."""
    return [
        json.dumps(row, sort_keys=True).replace(str(world.claude), "H")
        for row in world.rows(sid)
    ]


def _links(world: World) -> list[tuple[Any, ...]]:
    return [
        tuple(row)
        for row in world.table(
            "SELECT session, uuid, parent_uuid, parent_known, logical_parent_uuid, "
            "src FROM journal_link ORDER BY session, src"
        )
    ]


def _other(tmp_path: Path, name: str) -> World:
    """A world with a store of its own."""
    folder = tmp_path / f"{name}-store"
    folder.mkdir(mode=0o700)
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("NOTT_DATABASE_DIR", str(folder))
        return World(tmp_path / name)


def _reference(tmp_path: Path, files: dict[str, tuple[bytes, int]]) -> dict[str, Any]:
    """The rows of a one-pass import of `files` (session: bytes, mtime)."""
    reference = _other(tmp_path, "reference")
    for sid, (data, mtime) in files.items():
        reference.source(sid).write_bytes(data)
        os.utime(reference.source(sid), (mtime, mtime))
    daemon = reference.daemon()
    try:
        drain(daemon)
    finally:
        daemon.close()
    return {sid: _imported(reference, sid) for sid in files}


def _drained(daemon: serve.Daemon) -> list[str]:
    """`drain`, with the minute line of each cycle."""
    reports: list[str] = []
    for _ in range(200):
        daemon.cycle(cold=True)
        reports.append(daemon.report())
        if not any(
            source.new or source.behind or source.metadata_version < 1
            for source in daemon.sources.values()
        ):
            return reports
    raise AssertionError("The fixture writer made no progress.")


def _version_zero(daemon: serve.Daemon, links: bool = False) -> None:
    """Send each source through the metadata pass at the next start."""
    with nott_database.write(daemon.connection):
        if links:
            daemon.connection.execute("DELETE FROM journal_link")
        daemon.connection.execute("UPDATE journal_source SET metadata_version = 0")


def _state(daemon: serve.Daemon, sid: str = SID) -> dict[str, Any]:
    state: dict[str, Any] = json.loads(daemon.by_session[("claude", sid)].state)
    return state


@pytest.mark.parametrize("case", ["history", "pass"])
def test_a_budgeted_read_takes_each_byte_once(
    world: World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, case: str
) -> None:
    """A budget stop keeps the whole lines that it read: the next batch does
    not read them again from the disk."""
    path = world.write([user(n, f"short {n}") for n in range(25)])
    os.utime(path, (1000, 1000))
    if case == "pass":
        first = world.daemon()
        try:
            drain(first)
            _version_zero(first)
        finally:
            first.close()
        expected = _imported(world)
    else:
        expected = _reference(tmp_path, {SID: (path.read_bytes(), 1000)})[SID]
    seen = _preads(monkeypatch)
    daemon = world.daemon(batch_lines=10)
    try:
        drain(daemon)
    finally:
        daemon.close()
    _once(_on(seen, path), path.stat().st_size)
    assert _imported(world) == expected


@pytest.mark.parametrize("change", ["append", "rewrite"])
def test_a_change_between_budgeted_batches_reads_again(
    world: World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, change: str
) -> None:
    """Guard: a held suffix serves no read after a change of the file."""
    path = world.write([user(n, f"short {n}") for n in range(25)])
    os.utime(path, (1000, 1000))
    seen = _preads(monkeypatch)
    daemon = world.daemon(batch_lines=10)
    try:
        daemon.cycle(cold=True)
        committed = world.table("SELECT consumed FROM journal_source")[0][0]
        assert 0 < committed < path.stat().st_size
        if change == "append":
            world.write([user(30, "appended")])
            os.utime(path, (1000, 1000))
        else:
            path.write_bytes(path.read_bytes())
            os.utime(path, (2000, 2000))
        cut = len(seen)
        drain(daemon)
    finally:
        daemon.close()
    assert _on(seen[cut:], path)[0][0] == committed
    if change == "append":
        assert (
            _imported(world)
            == _reference(tmp_path, {SID: (path.read_bytes(), 1000)})[SID]
        )
    else:
        # As on main: the rewrite invalidates the activity, and the pass
        # rebuilds it from the bytes of the file.
        native = json.loads(world.table("SELECT activity FROM journal_source")[0][0])
        assert "reason" not in native and "rebuild" not in native
        info = path.stat()
        assert (native["size"], native["mtime_ns"]) == (info.st_size, info.st_mtime_ns)
        assert world.table("SELECT metadata_version FROM journal_source") == [(1,)]
        assert (
            _imported(world)
            == _reference(tmp_path, {SID: (path.read_bytes(), 2000)})[SID]
        )


@pytest.mark.parametrize("case", ["read", "pass"])
def test_a_window_that_ends_inside_a_record_is_read_once(
    world: World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, case: str
) -> None:
    """The part record at the end of a window is held: the next read takes
    only the bytes after the window."""
    data = _line(1, 300) + _line(2, 500)
    path = world.source()
    links: list[tuple[Any, ...]] | None = None
    if case == "read":
        daemon = world.daemon(read_bytes=512)
        path.write_bytes(data)
        os.utime(path, (NOW + 100, NOW + 100))
        expected = _reference(tmp_path, {SID: (data, NOW + 100)})[SID]
        seen = _preads(monkeypatch)
    else:
        path.write_bytes(data)
        first = world.daemon()
        try:
            drain(first)
            expected, links = _imported(world), _links(world)
            _version_zero(first, links=True)
        finally:
            first.close()
        seen = _preads(monkeypatch)
        daemon = world.daemon(read_bytes=512)
    try:
        drain(daemon)
    finally:
        daemon.close()
    assert _on(seen, path) == [(0, 512), (512, 288)]
    assert _imported(world) == expected
    if links is not None:
        assert _links(world) == links


@pytest.mark.parametrize("case", ["history", "pass"])
def test_a_source_that_loses_the_background_slot_keeps_its_bytes(
    world: World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, case: str
) -> None:
    """Each read cursor keeps its own suffix: a second owner of the
    background slot does not take the held bytes of the first one."""
    files: dict[str, tuple[bytes, int]] = {}
    if case == "pass":
        other = world.write([user(1, "c one"), user(2, "c two")], sid="c-pass")
        first = world.daemon()
        try:
            drain(first)
        finally:
            first.close()
    path = world.write([user(n, f"a {n}") for n in range(25)], sid="a-history")
    os.utime(path, (1000, 1000))
    files["a-history"] = (path.read_bytes(), 1000)
    seen = _preads(monkeypatch)
    daemon = world.daemon(batch_lines=10)
    try:
        daemon.cycle(cold=True)
        assert daemon.by_session[("claude", "a-history")].consumed == len(
            b"".join(path.read_bytes().splitlines(keepends=True)[:10])
        )
        if case == "history":
            other = world.write([user(n, f"b {n}") for n in range(25)], sid="b-history")
            os.utime(other, (2000, 2000))
            files["b-history"] = (other.read_bytes(), 2000)
        else:
            other.write_bytes(other.read_bytes())
            os.utime(other, (NOW - 100, NOW - 100))
            files["c-pass"] = (other.read_bytes(), NOW - 100)
        drain(daemon)
    finally:
        daemon.close()
    _once(_on(seen, path), path.stat().st_size)
    reference = _reference(tmp_path, files)
    for sid in files:
        assert _imported(world, sid) == reference[sid], sid


@pytest.mark.parametrize("case", ["read", "pass"])
def test_a_held_window_keeps_the_boundaries_of_main(
    world: World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, case: str
) -> None:
    """Review r1 of PR 1954: held bytes that are longer than the window of
    main make no larger window. Each cycle commits the records of the window
    of main, so the session reaches `done` at the same cycle as on main."""
    data = _line(1, 1100) + _line(2, 1000) + _line(3, 500)
    path = world.source()
    links: list[tuple[Any, ...]] | None = None
    if case == "read":
        daemon = world.daemon(read_bytes=512)
        path.write_bytes(data)
        os.utime(path, (NOW + 100, NOW + 100))
        expected = _reference(tmp_path, {SID: (data, NOW + 100)})[SID]
        seen = _preads(monkeypatch)
    else:
        path.write_bytes(data)
        first = world.daemon()
        try:
            drain(first)
            expected, links = _imported(world), _links(world)
            _version_zero(first, links=True)
        finally:
            first.close()
        seen = _preads(monkeypatch)
        daemon = world.daemon(read_bytes=512)
    states: list[tuple[str, int]] = []
    try:
        for _ in range(3):
            daemon.cycle(cold=True)
            states.append(
                tuple(
                    world.table(
                        "SELECT j.import_state, s.metadata_version "
                        "FROM journal_session j JOIN journal_source s "
                        "ON s.vendor = j.vendor AND s.session = j.id"
                    )[0]
                )
            )
    finally:
        daemon.close()
    if case == "read":
        assert states == [("importing", 1), ("importing", 1), ("done", 1)]
    else:
        assert states == [("importing", 0), ("importing", 0), ("done", 1)]
    _once(_on(seen, path), len(data))
    assert _imported(world) == expected
    if links is not None:
        assert _links(world) == links


@pytest.mark.parametrize("count", [2, 3], ids=["two", "three"])
def test_held_bytes_stay_within_their_bound(
    world: World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, count: int
) -> None:
    """All held suffixes keep at most 2 x `read_bytes` bytes. The bound
    deletes the oldest suffix, and the minute line counts its bytes."""
    data = _line(1, 300) + _line(2, 800)
    daemon = world.daemon(read_bytes=1024)
    seen = _preads(monkeypatch)
    paths = []
    for at in range(count):
        path = world.source(f"live-{at}")
        path.write_bytes(data)
        os.utime(path, (NOW + 100, NOW + 100))
        paths.append(path)
    try:
        daemon.cycle(cold=True)
        first = daemon.report()
        cut = len(seen)
        daemon.cycle(cold=True)
        second = daemon.report()
    finally:
        daemon.close()
    inodes = [path.stat().st_ino for path in paths]
    read = [found for found, _, _ in seen[:cut] if found in inodes]
    assert sorted(read) == sorted(inodes)
    assert all(_on(seen[:cut], path) == [(0, 1024)] for path in paths)
    again = {
        found: [
            (offset, length) for inode, offset, length in seen[cut:] if inode == found
        ]
        for found in inodes
    }
    if count == 2:
        assert all(reads == [(1024, 76)] for reads in again.values()), again
        assert (_deleted(first), _deleted(second)) == (0, 0)
    else:
        assert again[read[0]] == [(300, 800)], again
        assert all(again[found] == [(1024, 76)] for found in read[1:]), again
        assert (_deleted(first), _deleted(second)) == (724, 0)
    reference = _reference(
        tmp_path, {f"live-{at}": (data, NOW + 100) for at in range(count)}
    )
    for at in range(count):
        assert _imported(world, f"live-{at}") == reference[f"live-{at}"]


def test_a_read_error_reset_counts_only_the_deletions_of_the_bound(
    world: World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A read error deletes the held suffix of its cursor without a count.
    The replay after the reset deletes the same bytes as a pass with no
    error, and the counter counts each of them."""
    data = _line(1, 512) + _line(2, 1450) + _line(3, 512) + _line(4, 512)

    def prepared(target: World) -> Path:
        path = target.source()
        path.write_bytes(data)
        os.utime(path, (1000, 1000))
        daemon = target.daemon()
        try:
            drain(daemon)
            _version_zero(daemon)
        finally:
            daemon.close()
        return path

    other = _other(tmp_path, "control")
    prepared(other)
    control = other.daemon(read_bytes=16, batch_lines=1)
    try:
        expected = sum(_deleted(line) for line in _drained(control))
    finally:
        control.close()
    assert expected == 566

    path = prepared(world)
    links, rows, total = _links(world), _imported(world), world.total()
    daemon = world.daemon(read_bytes=16, batch_lines=1)
    try:
        before: list[str] = []
        while _state(daemon).get("backfill", {}).get("offset") != 512 + 1450:
            assert len(before) < 10
            daemon.cycle(cold=True)
            before.append(daemon.report())
        assert _deleted(before[-1]) == 566
        original = os.pread
        failed = False

        def read_once(fd: int, count: int, offset: int) -> bytes:
            nonlocal failed
            if not failed and os.fstat(fd).st_ino == path.stat().st_ino:
                failed = True
                raise OSError(errno.EIO, "injected read error")
            return original(fd, count, offset)

        monkeypatch.setattr(os, "pread", read_once)
        daemon.cycle(cold=True)
        assert failed
        assert _deleted(daemon.report()) == 0
        after = _drained(daemon)
    finally:
        daemon.close()
    assert sum(_deleted(line) for line in after) == expected
    assert _links(world) == links
    assert _imported(world) == rows
    assert world.total() == total


def test_a_metadata_pass_crosses_a_lone_carriage_return(world: World) -> None:
    """The metadata pass splits records on LF only, as the import does."""
    record = json.dumps(user(1, "carriage")).replace(", ", ",\r ", 1)
    assert "\r" in record
    world.source().write_bytes(
        (record + "\n" + json.dumps(user(2, "after")) + "\n").encode()
    )
    daemon = world.daemon()
    try:
        drain(daemon)
        rows, total = _imported(world), world.total()
        _version_zero(daemon, links=True)
    finally:
        daemon.close()
    daemon = world.daemon()
    try:
        drain(daemon)
    finally:
        daemon.close()
    assert world.table("SELECT import_state FROM journal_session") == [("done",)]
    assert len(_links(world)) == 2
    assert _imported(world) == rows
    assert world.total() == total


def test_a_metadata_pass_opens_no_spill_file(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The metadata pass keeps only links and metadata: it reads no spill
    file."""
    results = world.projects / SID / "tool-results"
    results.mkdir(parents=True, mode=0o700)
    (results / "toolu_big.txt").write_text("the full output\n")
    note = "Output too large. Full output saved to: {}\n\nPreview: the full"
    world.write(
        [
            user(1, "root", entrypoint="cli"),
            result(2, "toolu_a", note.format(results / "toolu_big.txt")),
        ]
    )
    daemon = world.daemon()
    try:
        drain(daemon)
        links, total = _links(world), world.total()
        with nott_database.write(daemon.connection):
            daemon.connection.execute("DELETE FROM journal_link")
            daemon.connection.execute(
                "UPDATE journal_source SET metadata_version = 0, "
                "state = json_remove(state, '$.metadata')"
            )
            daemon.connection.execute("UPDATE journal_session SET entrypoint = NULL")
    finally:
        daemon.close()
    opened: list[str] = []
    real = os.open

    def spy(path: Any, *args: Any, **kwargs: Any) -> int:
        opened.append(str(path))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", spy)
    daemon = world.daemon()
    try:
        drain(daemon)
    finally:
        daemon.close()
    assert not any("toolu_big.txt" in name for name in opened)
    assert _links(world) == links
    assert world.table("SELECT entrypoint FROM journal_session") == [("cli",)]
    outputs = [row["output"] for row in world.rows() if row["type"] == "tool_result"]
    assert outputs == ["the full output\n"]
    assert world.total() == total


def test_a_steady_daemon_reads_only_appended_bytes(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Consumer boundary: after a restart, a steady daemon reads no byte and
    decodes no activity. It reads only an unfinished record once, and then
    only the bytes that a source gains."""
    path_a = world.write([user(n, f"timed {n}") for n in range(300)], sid="a-main")
    _finished(world, "a-main", "a1")
    subagent = world.projects / "a-main" / "subagents" / "agent-a1.jsonl"
    path_l = world.write([user(1, "legacy")], sid="l-legacy")
    partial = json.dumps(user(2, "unfinished"))[:20]
    path_u = world.write(json.dumps(user(1, "whole")) + "\n" + partial, sid="u-open")
    results = world.projects / "s-spill" / "tool-results"
    results.mkdir(parents=True, mode=0o700)
    (results / "toolu_big.txt").write_text("the full output\n")
    note = "Output too large. Full output saved to: {}\n\nPreview: the full"
    path_s = world.write(
        [result(1, "toolu_a", note.format(results / "toolu_big.txt"))], sid="s-spill"
    )
    daemon = world.daemon()
    try:
        drain(daemon)
        activity = daemon.by_session[("claude", "a-main")].activity
        assert activity is not None
        assert len(json.loads(activity)["gaps"]) == 256
        with nott_database.write(daemon.connection):
            daemon.connection.execute(
                "UPDATE journal_source SET activity = NULL WHERE session = 'l-legacy'"
            )
    finally:
        daemon.close()
    tracked = {
        path.stat().st_ino for path in (path_a, subagent, path_l, path_u, path_s)
    }
    consumed_u = path_u.stat().st_size - len(partial)
    seen = _preads(monkeypatch)
    holder: list[serve.Daemon] = []
    decoded: list[str] = []
    original = json.loads

    def audit(value: Any, *args: Any, **kwargs: Any) -> Any:
        if (
            holder
            and isinstance(value, str)
            and any(value == source.activity for source in holder[0].sources.values())
        ):
            decoded.append(value)
        return original(value, *args, **kwargs)

    monkeypatch.setattr(json, "loads", audit)
    daemon = world.daemon()
    holder.append(daemon)
    try:
        drain(daemon)
        assert [read for read in seen if read[0] in tracked] == [
            (path_u.stat().st_ino, consumed_u, len(partial))
        ]
        daemon.report()
        cut = len(seen)
        decoded.clear()
        daemon.cycle(cold=True)
        for _ in range(5):
            daemon.cycle(cold=False)
        line = daemon.report()
        assert [read for read in seen[cut:] if read[0] in tracked] == []
        assert decoded == []
        assert "bytes read 0," in line
        assert len(daemon.sources) == 5
        assert f"files statted {6 * len(daemon.sources)}," in line
        consumed_a = path_a.stat().st_size
        appended = (json.dumps(user(400, "appended")) + "\n").encode()
        with path_a.open("ab") as stream:
            stream.write(appended)
        cut = len(seen)
        daemon.cycle(cold=False)
        line = daemon.report()
        assert [read for read in seen[cut:] if read[0] in tracked] == [
            (path_a.stat().st_ino, consumed_a, len(appended))
        ]
        assert f"bytes read {len(appended)}," in line
        assert any(row.get("text") == "appended" for row in world.rows("a-main"))
    finally:
        daemon.close()
