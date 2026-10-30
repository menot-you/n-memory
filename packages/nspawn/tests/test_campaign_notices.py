"""Campaign notices use the registry outbox and the SessionStart drain."""

from __future__ import annotations

from pathlib import Path

import pytest
from registry_reset import sql, store

from nspawn import notify
from nspawn.registry import NoticeCaller, Registry, RunKey
from nspawn.rows import Backend, RunPhase, RunRow


def test_project_flag_drains_once_without_a_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    project = str(tmp_path / "project")
    Path(project).mkdir()
    body = 'Campaign detector flag: {"kind": "idle_host", "run_id": null}'
    monkeypatch.delenv("NOTT_NSPAWN_RUN_ID", raising=False)
    with Registry(str(store()), "campaign-test") as registry:
        registry.open(now=1000)
        registry.queue_project_notice(project, body)
        registry.queue_project_notice(project, body)
        (notice,) = registry.ready_notices(project=project)
        assert not notice.addr.startswith(("node:", "remote:"))
        assert registry.get_run(notice.run_id) is None
    assert notify.drain_main(["--project", str(tmp_path / "other")]) == 0
    assert capsys.readouterr().out == ""
    assert notify.drain_main(["--project", project]) == 0
    assert body in capsys.readouterr().out
    assert notify.drain_main(["--project", project]) == 0
    assert capsys.readouterr().out == ""


def test_notice_callers_reads_pending_ready_and_delivered_rows() -> None:
    expected = [
        NoticeCaller("/fixture/321.sock", 321, 123, "/fixture/project"),
        NoticeCaller("/fixture/654.sock", 654, 456, "/fixture/project"),
        NoticeCaller("node:lead", 1, 0, "/fixture/node-project"),
        NoticeCaller("remote:lead", 1, 0, "/fixture/remote-project"),
    ]
    with Registry(str(store()), "campaign-test") as registry:
        registry.open(now=1000)
        for i, caller in enumerate(reversed(expected)):
            sql(
                registry,
                "INSERT INTO notices(run_id, caller_addr, caller_pid, caller_start_ticks, "
                "caller_project, created_at, ready_at, delivered_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "review-4",
                    caller.addr,
                    caller.pid,
                    caller.start_ticks,
                    caller.project,
                    1000,
                    None if i == 0 else 1100,
                    1200 if i == 3 else None,
                ),
            )
        sql(
            registry,
            "INSERT INTO notices(run_id, caller_addr, caller_pid, caller_start_ticks, "
            "caller_project, created_at) VALUES ('other', '/fixture/999.sock', 999, 789, '/other', 1000)",
        )
        assert registry.notice_callers("review-4") == expected
        assert registry.notice_callers("missing") == []


def test_run_project_reads_the_admission_key_without_a_caller() -> None:
    with Registry(str(store()), "campaign-test") as registry:
        registry.open(now=1000)
        registry.admit_run(
            RunRow(
                run_id="work",
                worktree="/fixture/writer",
                phase=RunPhase.PENDING,
                sandbox="read-only",
                model="fixture",
                owner_instance=registry.instance_id,
                created_at=1000,
                updated_at=1000,
                backend=Backend.CLAUDE,
            ),
            False,
            1000,
            60_000,
            run_key=RunKey("launch", "/fixture/project/.git", None, "a" * 64, "b" * 64),
        )
        assert registry.notice_callers("work") == []
        assert registry.run_project("work") == "/fixture/project/.git"
        assert registry.run_project("missing") is None


def test_project_notice_upgrade_keeps_original_callers() -> None:
    with Registry(str(store()), "campaign-test") as registry:
        registry.open(now=1000)
        sql(
            registry,
            "INSERT INTO notices(run_id, caller_addr, caller_pid, caller_start_ticks, "
            "caller_project, created_at, ready_at, delivered_at) "
            "VALUES ('work', '/fixture/321.sock', 321, 123, '/fixture/project', 1000, 1100, 1200)",
        )
        sql(registry, "ALTER TABLE notices DROP COLUMN body")
        version = registry.upgrade()
        assert registry.upgrade() == version
        assert registry.notice_callers("work") == [
            NoticeCaller("/fixture/321.sock", 321, 123, "/fixture/project")
        ]
        registry.queue_project_notice("/fixture/project", "fixture flag")
        assert len(registry.ready_notices(project="/fixture/project")) == 1


def test_project_notice_delivery_rolls_back_when_render_fails() -> None:
    with Registry(str(store()), "campaign-test") as registry:
        registry.open(now=1000)
        registry.queue_project_notice("/fixture/project", "fixture flag")
        (notice,) = registry.ready_notices(project="/fixture/project")
        dead = [(notice.run_id, notice.addr)]
        with pytest.raises(RuntimeError, match="fixture render failure"):
            registry.drain_notices("/fixture/project", dead, _fail_render)
        assert registry.drain_notices("/fixture/project", dead, lambda rows: []) == [
            "fixture flag"
        ]
        registry.queue_project_notice("/fixture/project", "fixture flag")
        assert registry.ready_notices(project="/fixture/project") == []


def _fail_render(rows: list[tuple[str, str, RunRow | None, str]]) -> list[str]:
    raise RuntimeError("fixture render failure")


def test_project_notice_needs_no_dead_caller_observation() -> None:
    with Registry(str(store()), "campaign-test") as registry:
        registry.open(now=1000)
        registry.queue_project_notice("/fixture/project", "fixture flag")
        assert registry.drain_notices("/fixture/project", [], lambda rows: []) == [
            "fixture flag"
        ]
