"""Activity belongs to the journal cursor, not a second native-file reader."""

from __future__ import annotations

import errno
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import nott_database
import pytest
from test_journal import FIXTURES, SID, World, drain, user

from nspawn import cli
from nspawn.journal import activity, show


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    made = World(tmp_path)
    monkeypatch.setenv("HOME", str(made.home))
    return made


def observed(world: World) -> dict[str, Any]:
    return json.loads(show.show(world.store, "claude", SID)[-1])["native_activity"]


def test_native_order_bookkeeping_untimed_and_partial_records(world: World) -> None:
    snapshot = json.loads(
        (FIXTURES / "claude-session.jsonl").read_text().splitlines()[1]
    )
    snapshot["snapshot"]["timestamp"] = "1970-01-01T00:00:04Z"
    records = [
        user(1, "owner", timestamp="1970-01-01T00:00:01Z"),
        snapshot,
        {"type": "untimed"},
        {"timestamp": "1970-01-01T00:00:10Z"},
        {"timestamp": "1970-01-01T00:00:02Z"},
        {"timestamp": "1970-01-01T00:00:03Z"},
    ]
    path = world.write(
        "\n" + "".join(json.dumps(row) + "\n" for row in records) + '{"partial":'
    )
    daemon = world.daemon(batch_lines=1)
    try:
        for _ in range(8):
            daemon.cycle(cold=True)
        assert set(observed(world)) == {"reason"}
        saved = json.loads(world.table("SELECT activity FROM journal_source")[0][0])
        assert saved["size"] == path.stat().st_size - len('{"partial":')
        world.write("null}\n")
        daemon.cycle(cold=True)
        result = observed(world)
        assert result["last_at"] == 10
        assert result["mtime_ns"] == path.stat().st_mtime_ns
        assert result["size"] == path.stat().st_size
        assert result["max_gap_seconds"] == 6
        assert result["gaps"] == [
            {"start": 1, "end": 4, "seconds": 3},
            {"start": 4, "end": 10, "seconds": 6},
            {"start": 2, "end": 3, "seconds": 1},
        ]
    finally:
        daemon.close()


@pytest.mark.parametrize("data", [b"", b'{"type":"untimed"}'])
def test_no_native_times_are_not_invented(tmp_path: Path, data: bytes) -> None:
    path = tmp_path / "source"
    path.touch()
    result = activity.begin(path.stat())
    activity.add(result, data)
    assert result["last_at"] is None
    assert result["gaps"] == []
    assert result["max_gap_seconds"] == 0


@pytest.mark.parametrize(
    "data",
    [
        b"not-json",
        b"[]",
        b'{"timestamp":42}',
        b'{"timestamp":"bad"}',
        b'{"timestamp":"2026-10-26T00:00:00"}',
        b'{"timestamp":"1970-01-01T00:00:00Z","body":"fake-secret",}',
        b'{"type":"file-history-snapshot","snapshot":{"timestamp":42}}',
    ],
)
def test_unreadable_record_is_a_safe_typed_refusal(tmp_path: Path, data: bytes) -> None:
    path = tmp_path / "source"
    path.touch()
    saved = activity.begin(path.stat())
    with pytest.raises(activity.ActivityRefused) as refused:
        activity.add(saved, data)
    assert "fake-secret" not in str(refused.value)


@pytest.mark.parametrize(
    "kind",
    [
        "missing",
        "symlink",
        "parent-link",
        "fifo",
        "directory",
        "writable-parent",
        "hardlink",
    ],
)
def test_import_source_fences(world: World, kind: str) -> None:
    path = world.write([user(1, "owner")])
    daemon = world.daemon()
    try:
        daemon.cycle(cold=True)
        world.write([user(2, "growth")])
        if kind == "missing":
            path.unlink()
        elif kind == "symlink":
            target = path.with_suffix(".saved")
            path.rename(target)
            path.symlink_to(target)
        elif kind == "parent-link":
            path.parent.rename(path.parent.with_name("other"))
            path.parent.symlink_to(
                path.parent.with_name("other"), target_is_directory=True
            )
        elif kind == "fifo":
            path.unlink()
            os.mkfifo(path)
        elif kind == "directory":
            path.unlink()
            path.mkdir()
        elif kind == "writable-parent":
            path.parent.chmod(0o777)
        elif kind == "hardlink":
            os.link(path, path.with_suffix(".linked"))
        daemon.cycle(cold=False)
        assert set(observed(world)) == {"reason"}
    finally:
        daemon.close()


def test_trust_fence_refusal_stays_failed_after_repair(world: World) -> None:
    path = world.write([user(1, "first")])
    logs: list[str] = []
    daemon = world.daemon(log=logs.append)
    try:
        daemon.cycle(cold=True)
        consumed = path.stat().st_size
        assert world.table("SELECT status FROM journal_source") == [(None,)]
        world.write([user(2, "pending")])
        path.parent.chmod(0o770)
        daemon.cycle(cold=True)
        path.parent.chmod(0o700)
        daemon.cycle(cold=True)
        reason = "transcript unavailable"
        assert world.table("SELECT status, reason FROM journal_source") == [
            ("failed", reason)
        ]
        assert world.table("SELECT status FROM journal_session") == [("failed",)]
        assert f"journal: {path}: {reason}; no longer tracked" in logs
        assert not daemon.sources
        assert world.table("SELECT consumed FROM journal_source") == [(consumed,)]
        assert [row["text"] for row in world.rows() if row["type"] == "message"] == [
            "first"
        ]
    finally:
        path.parent.chmod(0o700)
        daemon.close()


def test_record_ceiling(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "source"
    path.touch()
    monkeypatch.setattr(activity, "LINE_MAX_BYTES", 8)
    with pytest.raises(activity.ActivityRefused, match="byte ceiling"):
        activity.add(activity.begin(path.stat()), b'{"long":"record"}')


@pytest.mark.parametrize("kind", ["rewrite", "replace", "shrink"])
def test_concurrent_change_is_not_committed(
    world: World, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    path = world.write([user(1, "owner")])
    daemon = world.daemon()
    original = os.pread

    def changing(fd: int, count: int, offset: int) -> bytes:
        data = original(fd, count, offset)
        if os.fstat(fd).st_ino == path.stat().st_ino:
            if kind == "rewrite":
                path.write_bytes(data.replace(b"owner", b"other"))
            elif kind == "shrink":
                path.write_bytes(b"{}")
            else:
                replacement = path.with_suffix(".replacement")
                replacement.write_bytes(data + b"{}\n")
                replacement.replace(path)
        return data

    try:
        monkeypatch.setattr(os, "pread", changing)
        daemon.cycle(cold=True)
        assert world.table("SELECT consumed, status FROM journal_source") == [
            (0, "failed")
        ]
    finally:
        daemon.close()


def test_growth_waits_for_import_and_survives_restart(world: World) -> None:
    path = world.write([user(1, "owner")])
    daemon = world.daemon()
    try:
        daemon.cycle(cold=True)
        first = observed(world)
        world.write([user(8, "append")])
        assert set(observed(world)) == {"reason"}
    finally:
        daemon.close()
    daemon = world.daemon()
    try:
        daemon.cycle(cold=True)
        second = observed(world)
        assert second["size"] == path.stat().st_size > first["size"]
        assert second["last_at"] - first["last_at"] == 7
        assert second["max_gap_seconds"] == 7
        assert [gap["seconds"] for gap in second["gaps"]] == [7]
    finally:
        daemon.close()


@pytest.mark.parametrize("larger", [False, True])
def test_replacement_keeps_no_activity_after_restart(
    world: World, larger: bool
) -> None:
    path = world.write([user(1, "owner")])
    daemon = world.daemon()
    try:
        daemon.cycle(cold=True)
    finally:
        daemon.close()
    replacement = path.with_suffix(".replacement")
    replacement.write_bytes(path.read_bytes() + (b"{}\n" if larger else b""))
    replacement.replace(path)
    daemon = world.daemon()
    try:
        daemon.cycle(cold=True)
        assert observed(world) == {"reason": "transcript replaced"}
    finally:
        daemon.close()


def test_additive_migration_without_identity_keeps_activity_unknown(
    world: World,
) -> None:
    path = world.write([user(1, "owner"), user(4, "later")])
    daemon = world.daemon()
    try:
        drain(daemon)
        rows = world.table(
            "SELECT row FROM journal_row WHERE type = 'message' ORDER BY seq"
        )
        consumed = path.stat().st_size
        with nott_database.write(daemon.connection):
            daemon.connection.execute("ALTER TABLE journal_source DROP COLUMN activity")
    finally:
        daemon.close()
    assert set(observed(world)) == {"reason"}
    daemon = world.daemon(batch_lines=1)
    try:
        assert set(observed(world)) == {"reason"}
        daemon.cycle(cold=True)
        assert set(observed(world)) == {"reason"}
        # Live appends may precede the bounded historical backfill.
        world.write([user(10, "live append")])
        daemon.cycle(cold=True, batches=10)
        assert (
            world.table(
                "SELECT row FROM journal_row WHERE type = 'message' ORDER BY seq"
            )[: len(rows)]
            == rows
        )
        assert world.table("SELECT consumed FROM journal_source")[0][0] > consumed
        assert set(observed(world)) == {"reason"}
        assert world.table("SELECT activity, metadata_version FROM journal_source") == [
            (None, 1)
        ]
    finally:
        daemon.close()


def test_gap_history_is_bounded_but_maximum_is_not_forgotten(tmp_path: Path) -> None:
    path = tmp_path / "source"
    path.touch()
    saved = activity.begin(path.stat())
    for second in [0, *range(1000, 1400)]:
        activity.add(
            saved,
            json.dumps(
                {"timestamp": datetime.fromtimestamp(second, UTC).isoformat()}
            ).encode(),
        )
    assert len(saved["gaps"]) == activity.GAP_LIMIT
    assert all(gap["seconds"] == 1 for gap in saved["gaps"])
    assert saved["max_gap_seconds"] == 1000


def test_import_never_rereads_the_consumed_prefix(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = world.write([user(1, "owner")])
    daemon = world.daemon()
    try:
        daemon.cycle(cold=True)
        consumed = path.stat().st_size
        original = os.pread
        offsets: list[int] = []

        def audit(fd: int, count: int, offset: int) -> bytes:
            if os.fstat(fd).st_ino == path.stat().st_ino:
                offsets.append(offset)
            return original(fd, count, offset)

        monkeypatch.setattr(os, "pread", audit)
        world.write([user(4, "later")])
        daemon.cycle(cold=True)
        assert offsets == [consumed]
        assert [gap["seconds"] for gap in observed(world)["gaps"]] == [3]
    finally:
        daemon.close()


def test_activity_rolls_back_with_rows_and_cursor(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nspawn.journal import serve

    world.write([user(1, "owner")])
    daemon = world.daemon()
    try:
        daemon.cycle(cold=True)
        before = world.table("SELECT consumed, activity FROM journal_source")
        world.write([user(4, "later")])
        original = serve.insert_rows

        def conflict(*args, **kwargs):
            raise serve.Conflict

        monkeypatch.setattr(serve, "insert_rows", conflict)
        daemon.cycle(cold=True)
        assert world.table("SELECT consumed, activity FROM journal_source") == before
        monkeypatch.setattr(serve, "insert_rows", original)
        daemon.cycle(cold=True)
        assert [gap["seconds"] for gap in observed(world)["gaps"]] == [3]
    finally:
        daemon.close()


def test_rollback_cursor_invalidates_stale_activity(world: World) -> None:
    path = world.write([user(1, "owner")])
    daemon = world.daemon()
    try:
        daemon.cycle(cold=True)
        saved = world.table("SELECT activity FROM journal_source")[0][0]
        world.write([user(4, "rollback append")])
        daemon.cycle(cold=True)
        with nott_database.write(daemon.connection):
            # A prior release advances the cursor but knows no activity column.
            daemon.connection.execute(
                "UPDATE journal_source SET activity = ?", (saved,)
            )
    finally:
        daemon.close()
    assert set(observed(world)) == {"reason"}
    daemon = world.daemon(batch_lines=1)
    try:
        for _ in range(3):
            daemon.cycle(cold=True)
        result = observed(world)
        assert result["size"] == path.stat().st_size
        assert [gap["seconds"] for gap in result["gaps"]] == [3]
    finally:
        daemon.close()


def cli_activity(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    assert cli.main(["session", "list", "claude"]) == 0
    listed = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert cli.main(["session", "show", "claude", SID]) == 0
    shown = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert "native_activity" not in listed[0]
    return shown[-1]["native_activity"]


def rewrite_first_clock(path: Path) -> None:
    before = path.stat()
    data = path.read_bytes()
    changed = data.replace(b"00:00:01Z", b"00:00:58Z", 1)
    assert changed != data and len(changed) == len(data)
    path.write_bytes(changed)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000))
    assert path.stat().st_ino == before.st_ino


@pytest.mark.parametrize("restart", [False, True])
def test_overwrite_then_growth_rebuilds_activity(
    world: World, capsys: pytest.CaptureFixture[str], restart: bool
) -> None:
    path = world.write(
        [
            user(1, "first", timestamp="1970-01-01T00:00:01Z"),
            user(59, "second", timestamp="1970-01-01T00:00:59Z"),
        ]
    )
    daemon = world.daemon()
    try:
        daemon.cycle(cold=True)
        assert cli_activity(capsys)["max_gap_seconds"] == 58
        rewrite_first_clock(path)
        daemon.cycle(cold=False, batches=0)
        assert set(cli_activity(capsys)) == {"reason"}
        if restart:
            daemon.close()
            daemon = world.daemon()
        world.write([user(60, "third", timestamp="1970-01-01T00:01:00Z")])
        daemon.cycle(cold=True, batches=1)
        result = cli_activity(capsys)
        assert "reason" in result or result["max_gap_seconds"] == 1
        for _ in range(4):
            daemon.cycle(cold=True)
        result = cli_activity(capsys)
        assert result["max_gap_seconds"] == 1
        assert [gap["seconds"] for gap in result["gaps"]] == [1, 1]
        assert result["size"] == path.stat().st_size
    finally:
        daemon.close()


@pytest.mark.parametrize("current_mtime", [False, True])
@pytest.mark.parametrize("change", ["rewrite", "append"])
def test_change_between_activity_rebuild_batches_stays_unknown(
    world: World,
    capsys: pytest.CaptureFixture[str],
    current_mtime: bool,
    change: str,
) -> None:
    path = world.write([user(1, "first", timestamp="1970-01-01T00:00:01Z")])
    daemon = world.daemon()
    try:
        daemon.cycle(cold=True)
        saved = world.table("SELECT activity FROM journal_source")[0][0]
        world.write([user(59, "second", timestamp="1970-01-01T00:00:59Z")])
        daemon.cycle(cold=True)
        if current_mtime:
            native = json.loads(saved)
            native["mtime_ns"] = path.stat().st_mtime_ns
            saved = json.dumps(native)
        with nott_database.write(daemon.connection):
            daemon.connection.execute(
                "UPDATE journal_source SET activity = ?", (saved,)
            )
    finally:
        daemon.close()
    daemon = world.daemon(batch_lines=1)
    try:
        for _ in range(2):
            daemon.cycle(cold=True, batches=1)
            state = json.loads(world.table("SELECT state FROM journal_source")[0][0])
            if "backfill" in state:
                break
        assert state["backfill"]["offset"] > 0
        assert set(cli_activity(capsys)) == {"reason"}
        if change == "rewrite":
            rewrite_first_clock(path)
        else:
            world.write([user(60, "third", timestamp="1970-01-01T00:01:00Z")])
        for _ in range(3):
            daemon.cycle(cold=True, batches=1)
        assert world.table("SELECT metadata_version FROM journal_source") == [(1,)]
        assert set(cli_activity(capsys)) == {"reason"}
    finally:
        daemon.close()


@pytest.mark.parametrize("restart", [False, True])
def test_transient_read_error_retries_pending_and_later_records(
    world: World,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    restart: bool,
) -> None:
    path = world.write([user(1, "first")])
    daemon = world.daemon()
    try:
        daemon.cycle(cold=True)
        assert "reason" not in cli_activity(capsys)
        world.write([user(2, "pending")])
        original = os.pread
        failed = False

        def read_once(fd: int, count: int, offset: int) -> bytes:
            nonlocal failed
            if not failed and os.fstat(fd).st_ino == path.stat().st_ino:
                failed = True
                raise OSError(errno.EIO, "injected read error")
            return original(fd, count, offset)

        monkeypatch.setattr(os, "pread", read_once)
        assert daemon.cycle(cold=True) == 0
        assert failed
        assert set(cli_activity(capsys)) == {"reason"}
        assert world.table("SELECT status FROM journal_source") == [(None,)]
        assert len(daemon.sources) == 1
        if restart:
            daemon.close()
            daemon = world.daemon()
        daemon.cycle(cold=True)
        assert "reason" not in cli_activity(capsys)
        world.write([user(3, "later")])
        daemon.cycle(cold=True)
        assert "reason" not in cli_activity(capsys)
        assert [row["text"] for row in world.rows() if row["type"] == "message"] == [
            "first",
            "pending",
            "later",
        ]
        assert world.table("SELECT consumed FROM journal_source") == [
            (path.stat().st_size,)
        ]
    finally:
        daemon.close()


def _activity(world: World) -> dict[str, Any]:
    saved: dict[str, Any] = json.loads(
        world.table("SELECT activity FROM journal_source")[0][0]
    )
    return saved


def test_a_steady_poll_decodes_no_activity(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A poll decodes the activity of a source once for each change of it,
    not once for each poll."""
    world.write([user(n, f"timed {n}") for n in range(300)])
    world.write([user(1, "second")], sid="second")
    world.write([user(1, "third")], sid="third")
    daemon = world.daemon()
    try:
        drain(daemon)
        daemon.cycle(cold=False)
        activities = [source.activity for source in daemon.sources.values()]
        assert len(activities) == 3 and None not in activities
        assert len(json.loads(max(activities, key=len) or "{}")["gaps"]) == 256
        decoded: list[str] = []
        original = json.loads

        def audit(value: Any, *args: Any, **kwargs: Any) -> Any:
            if isinstance(value, str) and any(
                value == source.activity for source in daemon.sources.values()
            ):
                decoded.append(value)
            return original(value, *args, **kwargs)

        monkeypatch.setattr(json, "loads", audit)
        for _ in range(5):
            daemon.cycle(cold=False)
        assert len(decoded) == 0
    finally:
        daemon.close()


@pytest.mark.parametrize("change", ["replace", "rewrite", "append"])
def test_a_steady_poll_keeps_each_fence(world: World, change: str) -> None:
    """Guard: the fence of each poll follows the current activity."""
    path = world.write([user(1, "first"), user(2, "second")])
    daemon = world.daemon()
    try:
        drain(daemon)
        daemon.cycle(cold=False)
        daemon.cycle(cold=False)
        if change == "replace":
            copy = path.with_name("copy.tmp")
            copy.write_bytes(path.read_bytes())
            os.replace(copy, path)
            daemon.cycle(cold=False)
            assert world.table("SELECT status, reason FROM journal_source") == [
                ("failed", "transcript replaced")
            ]
        elif change == "rewrite":
            info = path.stat()
            path.write_bytes(path.read_bytes())
            os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000_000))
            daemon.cycle(cold=False)
            saved = _activity(world)
            assert saved["rebuild"] is True
            assert saved["reason"] == "transcript changed without growth"
        else:
            world.write([user(3, "third")])
            daemon.cycle(cold=False)
            assert any(row.get("text") == "third" for row in world.rows())
            daemon.cycle(cold=False)
            saved = _activity(world)
            assert "rebuild" not in saved and "reason" not in saved
            info = path.stat()
            assert (saved["size"], saved["mtime_ns"]) == (
                info.st_size,
                info.st_mtime_ns,
            )
            assert world.table("SELECT metadata_version FROM journal_source") == [(1,)]
    finally:
        daemon.close()


@pytest.mark.parametrize("case", ["current", "linked-behind"])
def test_a_restart_reads_no_byte_of_a_source_without_identity(
    world: World, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """A start sends a source without retained identity through the
    metadata pass only for its links: a stored version 0 or `linked` behind
    `consumed`."""
    path = world.write([user(1, "first"), user(2, "second")])
    daemon = world.daemon()
    try:
        drain(daemon)
        with nott_database.write(daemon.connection):
            daemon.connection.execute("UPDATE journal_source SET activity = NULL")
            if case == "linked-behind":
                daemon.connection.execute("UPDATE journal_source SET linked = 0")
    finally:
        daemon.close()
    rows = world.table("SELECT seq, type, row FROM journal_row ORDER BY seq")
    total = json.loads(show.show(world.store, "claude", SID)[-1])["total"]
    inode = path.stat().st_ino
    seen: list[tuple[int, int]] = []
    original = os.pread

    def audit(fd: int, count: int, offset: int) -> bytes:
        data = original(fd, count, offset)
        if os.fstat(fd).st_ino == inode:
            seen.append((offset, len(data)))
        return data

    monkeypatch.setattr(os, "pread", audit)
    runs: list[list[tuple[int, int]]] = []
    for _ in range(2):
        start = len(seen)
        daemon = world.daemon()
        try:
            drain(daemon)
        finally:
            daemon.close()
        runs.append(seen[start:])
    if case == "current":
        assert runs == [[], []]
    else:
        at = 0
        for offset, length in runs[0]:
            assert offset == at, runs
            at += length
        assert at == path.stat().st_size
        assert runs[1] == []
    assert world.table("SELECT activity, metadata_version FROM journal_source") == [
        (None, 1)
    ]
    assert world.table("SELECT seq, type, row FROM journal_row ORDER BY seq") == rows
    assert json.loads(show.show(world.store, "claude", SID)[-1])["total"] == total
