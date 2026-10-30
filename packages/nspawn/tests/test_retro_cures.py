"""Regression tests for the four nSPAWN retrospective results."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import nott_database
import pytest
from registry_reset import store
from test_classify import _install, _roster
from test_terminal import _assistant_text, _classify, _ndjson, _result

from nspawn import classify, cli, picker, system1
from nspawn.errors import NERR_1124
from nspawn.owner_roster import ClassPolicy, Provider, Roster
from nspawn.registry import Registry
from nspawn.rows import Backend, RunPhase, RunRow


def _row(tmp_path: Path, run_id: str, created_at: int = 1) -> RunRow:
    return RunRow(
        run_id=run_id,
        worktree=str(tmp_path),
        phase=RunPhase.RUNNING,
        sandbox="read-only",
        model="fixture-model",
        owner_instance="driver",
        created_at=created_at,
        updated_at=created_at,
        backend=Backend.CLAUDE,
    )


def test_result_1_busy_heartbeat_does_not_orphan_a_live_driver(tmp_path: Path) -> None:
    with Registry(store(), "driver") as driver, Registry(store(), "peer") as peer:
        driver.open(1_000)
        peer.open(1_000)
        driver.insert_run(_row(tmp_path, "healthy"))

        driver._run("PRAGMA busy_timeout = 1")
        with (
            ThreadPoolExecutor(max_workers=1) as worker,
            peer._transaction(),
            pytest.raises(nott_database.BusyError),
        ):
            worker.submit(driver.heartbeat_instance, 100_000).result(timeout=5)

        driver._run("PRAGMA busy_timeout = 5000")
        assert peer.mark_orphans(100_000, 1_000) == 0
        row = peer.get_run("healthy")
        assert row is not None and row.phase is RunPhase.RUNNING
        assert driver.heartbeat_instance(100_001)


def test_result_2_final_text_keeps_all_resumed_answer_blocks(tmp_path: Path) -> None:
    first = _assistant_text("First part. ")
    first["message"]["content"].append({"type": "text", "text": "Second part. "})
    first["message"]["stop_reason"] = "max_tokens"
    last = _assistant_text("Last part.")
    observed = _classify(
        tmp_path,
        _ndjson(first, last, _result(text="Last part.")),
    )
    assert observed.final_text == "First part. Second part. Last part."


def test_result_3_classifier_failure_keeps_declared_model_and_effort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, system1.System1Unavailable("unavailable"))
    roster = _roster()
    route = Provider(
        name="route",
        kind="codex",
        model="class-model",
        models=["class-model"],
        efforts=["low", "medium", "high", "max"],
    )
    roster = replace(roster, providers=[route])
    effective, warning, observation = classify.resolve_execution_class(
        "planning", "Write the agreed plan", roster
    )
    assert effective == "planning"
    picked = picker.pick_execution(
        roster, 1_000, execution_class=effective, preferred_effort="max"
    )
    assert (picked.model, picked.effort) == ("class-model", "max")
    assert warning is not None
    assert observation is not None and observation.observed is None


def test_result_3_unsupported_pin_names_the_chosen_model_and_effort() -> None:
    route = Provider(
        name="route",
        kind="codex",
        model="class-model",
        models=["class-model"],
        efforts=["low", "medium", "high"],
    )
    roster = Roster(
        providers=[route],
        classes={"planning": ClassPolicy(effort="high", ceiling="max")},
    )
    with pytest.raises(picker.PickerError) as refused:
        picker.pick_execution(
            roster, 1_000, execution_class="planning", requested_effort="max"
        )
    assert "class-model" in str(refused.value)
    assert "max" in str(refused.value)


@pytest.mark.parametrize(
    ("since", "first"),
    [
        ("2026-10-24T00:00:00Z", 0),
        ("2026-10-24", 0),
        ("2026-10-24T00:00:00", 0),
        ("2026-10-24T01:00:00+01:00", 0),
        ("2026-10-24T00:00:00.000001Z", 1),
    ],
)
def test_result_4_since_includes_the_boundary_without_a_page_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    since: str,
    first: int,
) -> None:
    start = 1_792_800_000_000
    with Registry(store(), "driver") as registry:
        registry.open(1_000)
        registry.insert_run(_row(tmp_path, "before", start - 1))
        for index in range(140):
            registry.insert_run(_row(tmp_path, f"run-{index:03}", start + index))

    def forbidden(*args: object, **kwargs: object) -> int:
        pytest.fail("--since must read every matching run from the test store")

    monkeypatch.setattr(cli, "_read_tool", forbidden)
    assert cli.main(["run", "list", "--since", since]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert [row["run_id"] for row in payload["runs"]] == [
        f"run-{index:03}" for index in reversed(range(first, 140))
    ]


@pytest.mark.parametrize(
    "unknown", [False, True], ids=["failed-probe", "legacy-identity"]
)
def test_result_1_failed_probes_preserve_runs_but_legacy_rows_expire(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unknown: bool
) -> None:
    from nspawn import registry as registry_module
    from nspawn.process_identity import ProcessIdentityError

    with Registry(store(), "driver") as driver, Registry(store(), "peer") as peer:
        driver.open(1_000)
        peer.open(1_000)
        driver.insert_run(_row(tmp_path, "unknown"))
        if unknown:
            driver._write(
                "UPDATE instances SET process_pid = NULL, process_boot_id = NULL, "
                "process_start_ticks = NULL WHERE instance_id = 'driver'"
            )

        def unreadable(*args: object) -> bool:
            raise ProcessIdentityError("the process cannot be read")

        monkeypatch.setattr(registry_module, "process_identity_matches", unreadable)
        assert peer.mark_orphans(100_000, 1_000) == (1 if unknown else 0)
        row = peer.get_run("unknown")
        assert row is not None
        assert row.phase is (RunPhase.ORPHANED if unknown else RunPhase.RUNNING)
        assert driver.heartbeat_instance(100_001) is (not unknown)


def test_result_1_a_dead_driver_is_still_orphaned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    from nspawn import registry as registry_module
    from nspawn.process_identity import capture_process_identity

    identity = capture_process_identity(os.getpid())
    with Registry(store(), "driver") as driver, Registry(store(), "peer") as peer:
        with monkeypatch.context() as patch:
            patch.setattr(
                registry_module,
                "capture_process_identity",
                lambda pid: replace(identity, start_ticks=identity.start_ticks + 1),
            )
            driver.open(1_000)
        peer.open(1_000)
        driver.insert_run(_row(tmp_path, "dead"))
        assert peer.mark_orphans(100_000, 1_000) == 1
        row = peer.get_run("dead")
        assert row is not None and row.phase is RunPhase.ORPHANED


@pytest.mark.parametrize("result_text", [None, "First. Last.", "Last."])
def test_result_2_final_answer_excludes_tools_and_subagents(
    tmp_path: Path, result_text: str | None
) -> None:
    tool = {
        "type": "assistant",
        "message": {"content": [{"type": "tool_use", "id": "read"}]},
    }
    subagent = _assistant_text("Not the main answer.")
    subagent["parent_tool_use_id"] = "read"
    observed = _classify(
        tmp_path,
        _ndjson(
            _assistant_text("I will read the file."),
            tool,
            _assistant_text("First. "),
            subagent,
            _assistant_text("Last."),
            _result(text=result_text),
        ),
    )
    assert observed.final_text == "First. Last."


@pytest.mark.parametrize("pinned_model", [None, "pinned-model"])
def test_result_3_an_effort_pin_does_not_replace_the_class_model(
    pinned_model: str | None,
) -> None:
    route = Provider(
        name="route",
        kind="codex",
        model="route-model",
        model_fast="class-model",
        models=["route-model", "class-model", "pinned-model"],
        efforts=["low", "medium", "high"],
    )
    roster = Roster(
        providers=[route],
        classes={
            "implementation": ClassPolicy(effort="high", ceiling="high", fast=True)
        },
    )
    picked = picker.pick_execution(
        roster,
        1_000,
        execution_class="implementation",
        requested_effort="high",
        requested_model=pinned_model,
    )
    assert (picked.model, picked.effort) == (pinned_model or "class-model", "high")


@pytest.mark.parametrize("since", ["not-a-date", "2026-13-01"])
def test_result_4_invalid_since_is_a_usage_error(
    capsys: pytest.CaptureFixture[str], since: str
) -> None:
    assert cli.main(["run", "list", "--since", since]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "--since" in captured.err
    assert since in captured.err


def test_result_4_without_since_keeps_the_current_read_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, object]] = []

    def read(name: str, arguments: object) -> int:
        calls.append((name, arguments))
        return 0

    monkeypatch.setattr(cli, "_read_tool", read)
    assert cli.main(["run", "list"]) == 0
    assert calls == [("nspawn_run_list", {"include_terminal": True})]


@pytest.mark.parametrize("model", ["sonnet", "claude-sonnet-5-5"])
def test_result_3_a_model_specific_effort_limit_refuses_the_pin(
    tmp_path: Path, model: str
) -> None:
    from test_model_catalog import client, response

    binary = client(
        tmp_path,
        response(
            [
                {
                    "value": "sonnet",
                    "resolvedModel": "claude-sonnet-5-5",
                    "supportsEffort": True,
                    "supportedEffortLevels": ["low", "medium", "high"],
                }
            ]
        ),
    )
    route = Provider(
        name="route",
        kind="claude",
        bin=str(binary),
        model=model,
        models=[model],
        efforts=["low", "medium", "high", "max"],
    )
    with pytest.raises(picker.PickerError) as refused:
        picker.pick_execution(Roster(providers=[route]), 1_000, requested_effort="max")
    assert model in str(refused.value)
    assert "max" in str(refused.value)


@pytest.mark.parametrize("size", [0, 64_554])
def test_result_2_public_result_prefers_the_complete_recorded_answer(
    tmp_path: Path, size: int
) -> None:
    from nspawn.limits import MAX_DURABLE_OBSERVATION_BYTES
    from nspawn.supervisor import run_result

    answer = "x" * size + "First part. Last part."
    (tmp_path / "stdout").write_bytes(
        _ndjson(
            _assistant_text("First part. "),
            _assistant_text("Last part."),
            _result(text="Last part."),
        )
    )
    (tmp_path / "stderr").write_text("")
    row = replace(
        _row(tmp_path, "whole"),
        phase=RunPhase.COMPLETED,
        final_text=answer[:MAX_DURABLE_OBSERVATION_BYTES],
    )
    if len(answer) > MAX_DURABLE_OBSERVATION_BYTES:
        (tmp_path / "final_text").write_text(answer)
    assert run_result(row, tmp_path).final_text == answer


def test_result_2_a_null_result_uses_the_complete_later_answer(tmp_path: Path) -> None:
    observed = _classify(
        tmp_path,
        _ndjson(
            _assistant_text("An earlier partial answer."),
            _result(text=None),
            _assistant_text("The final "),
            _assistant_text("answer."),
        ),
    )
    assert observed.final_text == "The final answer."


def test_result_1_upgrade_adds_identity_without_claiming_legacy_death() -> None:
    from nspawn.schema import INSTANCE_PROCESS_COLUMNS

    with Registry(store(), "driver") as driver:
        driver.open(1_000)
        for name in INSTANCE_PROCESS_COLUMNS:
            driver._write(f"ALTER TABLE instances DROP COLUMN {name}")
        driver.upgrade()
        legacy = driver._one("SELECT * FROM instances WHERE instance_id = 'driver'")
        assert legacy is not None
        assert all(legacy[name] is None for name in INSTANCE_PROCESS_COLUMNS)
        driver.register_instance(2_000)
        current = driver._one("SELECT * FROM instances WHERE instance_id = 'driver'")
        assert current is not None
        assert all(current[name] is not None for name in INSTANCE_PROCESS_COLUMNS)


@pytest.mark.parametrize("stale", [False, True])
def test_result_1_legacy_rows_use_the_heartbeat_boundary(
    tmp_path: Path, stale: bool
) -> None:
    with Registry(store(), "driver") as driver, Registry(store(), "peer") as peer:
        driver.open(1_000)
        peer.open(1_000)
        driver.insert_run(_row(tmp_path, "legacy"))
        driver._write(
            "UPDATE instances SET process_pid = NULL, process_boot_id = NULL, "
            "process_start_ticks = NULL WHERE instance_id = 'driver'"
        )
        assert peer.mark_orphans(2_000 if stale else 1_999, 1_000) == int(stale)
        row = peer.get_run("legacy")
        assert row is not None
        assert row.phase is (RunPhase.ORPHANED if stale else RunPhase.RUNNING)
        if stale:
            assert row.blocked_reason is not None
            assert "legacy heartbeat expired" in row.blocked_reason
        assert driver.heartbeat_instance(2_001) is (not stale)


@pytest.mark.parametrize("parked", [False, True])
def test_result_1_orphan_reason_names_process_loss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, parked: bool
) -> None:
    import os

    from nspawn import registry as registry_module
    from nspawn.process_identity import capture_process_identity

    identity = capture_process_identity(os.getpid())
    with Registry(store(), "driver") as driver, Registry(store(), "peer") as peer:
        with monkeypatch.context() as patch:
            patch.setattr(
                registry_module,
                "capture_process_identity",
                lambda pid: replace(identity, start_ticks=identity.start_ticks + 1),
            )
            driver.open(1_000)
        peer.open(1_000)
        driver.insert_run(
            replace(
                _row(tmp_path, "lost"),
                phase=RunPhase.WAITING_APPROVAL if parked else RunPhase.RUNNING,
                pending_approvals=int(parked),
            )
        )
        assert peer.mark_orphans(100_000, 1_000) == 1
        row = peer.get_run("lost")
        assert row is not None and row.blocked_reason is not None
        assert "owner process gone" in row.blocked_reason
        assert "heartbeat" not in row.blocked_reason
        if parked:
            assert "parked approvals were lost" in row.blocked_reason
            assert row.pending_approvals == 0


@pytest.mark.parametrize(
    ("levels", "expected"),
    [(["low", "medium", "high"], "high"), (["high"], "high")],
)
def test_result_3_a_model_specific_limit_clamps_a_suggestion(
    tmp_path: Path, levels: list[str], expected: str
) -> None:
    from test_model_catalog import client, response

    binary = client(
        tmp_path,
        response(
            [
                {
                    "value": "sonnet",
                    "resolvedModel": "claude-sonnet-5-5",
                    "supportsEffort": bool(levels),
                    "supportedEffortLevels": levels,
                }
            ]
        ),
    )
    route = Provider(
        name="route",
        kind="claude",
        bin=str(binary),
        model="sonnet",
        models=["sonnet"],
        efforts=["low", "medium", "high", "max"],
    )
    picked = picker.pick_execution(
        Roster(providers=[route]), 1_000, preferred_effort="max"
    )
    assert picked.effort == expected
    assert picked.warning is not None
    assert "sonnet" in picked.warning
    assert "max" in picked.warning
    assert f'"{expected}"' in picked.warning


def test_result_2_error_text_keeps_the_vendor_rejection_code(tmp_path: Path) -> None:
    from nspawn.terminal import rejected_code

    rejection = "API Error: Request rejected (403) · [1101][request-id]"
    observed = _classify(
        tmp_path,
        _ndjson(
            _assistant_text("Earlier answer. "),
            _assistant_text(rejection),
            _result(is_error=True, reason="api_error", text=rejection),
        ),
        exit_code=1,
    )
    assert observed.final_text == rejection
    assert rejected_code(observed.final_text, 403) == "1101"


def _catalogue_roster(
    tmp_path: Path,
    efforts: list[str],
    *,
    effort: str | None = None,
    levels: tuple[str, ...] = ("low", "medium", "high"),
) -> Roster:
    from test_model_catalog import client, response

    binary = client(
        tmp_path,
        response(
            [
                {
                    "value": "sonnet",
                    "resolvedModel": "claude-sonnet-5-5",
                    "supportsEffort": bool(levels),
                    "supportedEffortLevels": levels,
                }
            ]
        ),
    )
    path = tmp_path / "spawn.toml"
    path.write_text(
        f"[bins]\nclaude = {json.dumps(str(binary))}\n"
        '[[provider]]\nname = "route"\nkind = "claude"\n'
        'model = "sonnet"\nmodels = ["sonnet"]\n'
        f"efforts = {json.dumps(efforts)}\n"
        + (f"effort = {json.dumps(effort)}\n" if effort is not None else "")
    )
    return Roster.read(path)


def test_result_3_off_ladder_roster_effort_has_a_typed_catalogue_refusal(
    tmp_path: Path,
) -> None:
    roster = _catalogue_roster(tmp_path, ["auto"], effort="auto")
    with pytest.raises(picker.PickerError) as refused:
        picker.pick_execution(roster, 1_000)
    assert refused.value.entry is NERR_1124
    assert 'model "sonnet"' in str(refused.value)
    assert 'effort "auto"' in str(refused.value)
    assert "[low, medium, high]" in str(refused.value)


def test_result_3_catalogue_clamp_keeps_the_route_effort_ladder(
    tmp_path: Path,
) -> None:
    roster = _catalogue_roster(tmp_path, ["low", "max"])
    picked = picker.pick_execution(roster, 1_000, preferred_effort="max")
    assert picked.effort == "low"
    assert picked.effort in roster.providers[0].efforts
    assert picked.warning is not None
    assert 'model "sonnet"' in picked.warning
    assert '"max"' in picked.warning
    assert '"low"' in picked.warning


@pytest.mark.parametrize("levels", [("low", "medium", "high"), ()])
def test_result_3_no_shared_effort_has_a_typed_catalogue_refusal(
    tmp_path: Path, levels: tuple[str, ...]
) -> None:
    roster = _catalogue_roster(tmp_path, ["max"], levels=levels)
    with pytest.raises(picker.PickerError) as refused:
        picker.pick_execution(roster, 1_000, preferred_effort="max")
    assert refused.value.entry is NERR_1124
    assert 'model "sonnet"' in str(refused.value)
    assert 'effort "max"' in str(refused.value)
    assert f"[{', '.join(levels)}]" in str(refused.value)


def _c1lock_history_profile(registry, run_id, execution_class, native_write=True):
    from nspawn.rows import SessionProfile

    profile = SessionProfile(
        original_run_id=run_id,
        native_write=native_write,
        read_only=not native_write,
        read_paths=(),
        execution_class=execution_class,
    )
    registry.append_run_event(
        run_id,
        at=1_792_800_000_000,
        kind="lifecycle",
        payload={"session_profile": profile.data()},
    )


def _c1lock_history_json(capsys):
    capsys.readouterr()
    code = cli.main(["run", "list", "--since", "2026-10-24T00:00:00Z"])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert captured.err == ""
    return json.loads(captured.out)


def _c1lock_projection_keys(row):
    assert set(row) == {
        "run_id",
        "lane",
        "phase",
        "executed_provider",
        "executed_account",
        "model",
        "tokens",
        "turn_count",
        "failure_reason",
        "created_at",
        "updated_at",
        "group",
        "backend",
        "native_write",
        "execution_class",
        "red_admission",
        "red_admission_reason",
        "red_receipt",
    }


@pytest.mark.parametrize("owned", [False, True])
def test_c1_since_projects_completed_writer_witness_state(
    tmp_path, monkeypatch, capsys, owned
):
    start = 1_792_800_000_000
    phases = [RunPhase.COMPLETED, RunPhase.FAILED, RunPhase.CLOSED]
    session = "11111111-1111-4111-8111-111111111111"
    owner = "node:" + session if owned else None
    if owned:
        monkeypatch.setenv("NOTT_NSPAWN_NODE_SESSION", session)
    with Registry(store(), "driver") as registry:
        registry.open(1_000)
        registry.insert_run(
            replace(_row(tmp_path, "before-boundary", start - 1), owner=owner)
        )
        for index in range(145):
            run_id = f"c1lock-complete-{index:03}-full-retained-identity"
            registry.insert_run(
                replace(
                    _row(tmp_path, run_id, start + index),
                    phase=phases[index % len(phases)],
                    sandbox="workspace-write",
                    owner=owner,
                )
            )
            _c1lock_history_profile(registry, run_id, "implementation")
        registry.insert_run(
            replace(
                _row(tmp_path, "c1lock-between-observations", start + 200),
                sandbox="workspace-write",
                owner=owner,
            )
        )
        _c1lock_history_profile(
            registry, "c1lock-between-observations", "implementation"
        )
        if owned:
            registry.insert_run(_row(tmp_path, "outside-owner-scope", start + 300))

    def forbidden(*args, **kwargs):
        pytest.fail("--since must not use the bounded tool fleet")

    monkeypatch.setattr(cli, "_read_tool", forbidden)
    first = _c1lock_history_json(capsys)
    expected = {
        f"c1lock-complete-{index:03}-full-retained-identity" for index in range(145)
    } | {"c1lock-between-observations"}
    assert {item["run_id"] for item in first["runs"]} == expected
    for projected in first["runs"]:
        _c1lock_projection_keys(projected)
        assert projected["native_write"] is True
        assert projected["execution_class"] == "implementation"
        assert projected["red_admission"] == "missing"
        reason = projected["red_admission_reason"]
        assert isinstance(reason, str) and 0 < len(reason) <= 512
        assert projected["red_receipt"] is None

    with Registry(store(), "driver") as registry:
        current = registry.get_run("c1lock-between-observations")
        registry.update_run(replace(current, phase=RunPhase.COMPLETED))
    second = _c1lock_history_json(capsys)
    assert {item["run_id"] for item in second["runs"]} == expected
    completed = next(
        item
        for item in second["runs"]
        if item["run_id"] == "c1lock-between-observations"
    )
    assert completed["phase"] == "completed"
    assert completed["red_admission"] == "missing"


@pytest.mark.parametrize(
    "execution_class",
    [
        "verification",
        "design",
        "deterministic",
        "review",
        "judgment",
        "planning",
        "mechanical_fanout",
        None,
    ],
)
def test_c1_since_keeps_verification_native_writers_visible(
    tmp_path, capsys, execution_class
):
    run_id = "c1lock-visible-" + str(execution_class)
    with Registry(store(), "driver") as registry:
        registry.open(1_000)
        registry.insert_run(
            replace(
                _row(tmp_path, run_id, 1_792_800_000_000),
                phase=RunPhase.CLOSED,
                sandbox="workspace-write",
            )
        )
        _c1lock_history_profile(registry, run_id, execution_class)
    payload = _c1lock_history_json(capsys)
    projected = next(item for item in payload["runs"] if item["run_id"] == run_id)
    _c1lock_projection_keys(projected)
    assert projected["native_write"] is True
    assert projected["execution_class"] == execution_class
    assert projected["red_admission"] == "not_required"
    assert projected["red_admission_reason"] is None
    assert projected["red_receipt"] is None


@pytest.mark.parametrize("failure", ["history-after-one-row", "evidence"])
def test_c1_failed_history_read_is_a_typed_nonzero_error(
    tmp_path, monkeypatch, capsys, failure
):
    import re

    from nspawn.registry import RegistryError

    with Registry(store(), "driver") as registry:
        registry.open(1_000)
        registry.insert_run(_row(tmp_path, "c1lock-read", 1_792_800_000_000))
        _c1lock_history_profile(registry, "c1lock-read", "implementation")

    if failure == "history-after-one-row":
        original = Registry.runs_since

        def broken(self, *args, **kwargs):
            rows = original(self, *args, **kwargs)
            if rows:
                yield rows[0]
            raise RegistryError("C1LOCK_PRIVATE_EXCEPTION\x1b[31m")

        monkeypatch.setattr(Registry, "runs_since", broken)
    else:

        def unreadable(self, run_id):
            raise RegistryError("C1LOCK_PRIVATE_EXCEPTION\x1b[31m")

        monkeypatch.setattr(Registry, "read_run_events", unreadable)

    capsys.readouterr()
    code = cli.main(["run", "list", "--since", "2026-10-24T00:00:00Z"])
    captured = capsys.readouterr()
    assert code != 0
    assert captured.out == "", "never publish partial or empty successful JSON"
    assert "nspawn run list" in captured.err
    assert re.search(r"\b(?:[A-Z][A-Za-z]*Error|NERR_\d+)\b", captured.err), (
        "history failures must retain an explicit error type",
        captured.err,
    )
    assert "C1LOCK_PRIVATE_EXCEPTION" not in captured.err
    assert "\x1b" not in captured.err


@pytest.mark.parametrize(
    "case", ["missing-profile", "malformed-profile", "malformed-receipt"]
)
def test_c1_since_distinguishes_unknown_and_invalid_evidence(tmp_path, capsys, case):
    run_id = "c1lock-evidence-" + case
    with Registry(store(), "driver") as registry:
        registry.open(1_000)
        registry.insert_run(
            replace(
                _row(tmp_path, run_id, 1_792_800_000_000),
                phase=RunPhase.COMPLETED,
                sandbox="workspace-write",
            )
        )
        if case == "malformed-profile":
            registry.append_run_event(
                run_id,
                at=1_792_800_000_000,
                kind="lifecycle",
                payload={"session_profile": {"native_write": False}},
            )
        elif case == "malformed-receipt":
            _c1lock_history_profile(registry, run_id, "implementation")
            registry.append_run_event(
                run_id,
                at=1_792_800_000_000,
                kind="lifecycle",
                payload={
                    "writer_red": {
                        "schema": "broken",
                        "assertion": "C1LOCK_PRIVATE_ASSERTION",
                    }
                },
            )
        before = registry.read_run_events(run_id)

    payload = _c1lock_history_json(capsys)
    projected = next(item for item in payload["runs"] if item["run_id"] == run_id)
    expected = "unknown" if "profile" in case else "invalid"
    assert projected.get("red_admission") == expected, projected
    _c1lock_projection_keys(projected)
    assert projected["red_receipt"] is None
    reason = projected["red_admission_reason"]
    assert isinstance(reason, str) and 0 < len(reason) <= 512
    assert "C1LOCK_PRIVATE_ASSERTION" not in json.dumps(projected)
    if "profile" in case:
        assert projected["execution_class"] is None
    with Registry(store(), "driver") as registry:
        assert registry.read_run_events(run_id) == before


@pytest.mark.parametrize("corruption", ["receipt", "profile"])
@pytest.mark.parametrize("reader", ["receipt", "history"])
def test_c1_broken_admission_chain_never_projects_healthy_evidence(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    corruption: str,
    reader: str,
) -> None:
    from test_native_write import _c1lock_repo

    from nspawn.registry import WriterRedCorrupt
    from nspawn.rows import SessionProfile, WriterRedReceipt

    run_id = "c1-broken-chain"
    at = 1_792_800_000_000
    root = tmp_path / "checkout"
    head, _ = _c1lock_repo(root)
    receipt = WriterRedReceipt(
        ".nott/plans/chain/red/u1.json",
        "a" * 64,
        ".nott/plans/chain/tests/u1.md",
        "b" * 64,
        "u1",
        head,
        ("chain-case",),
    )
    profile = SessionProfile(run_id, True, False, (), execution_class="implementation")
    with Registry(store(), "driver") as registry:
        registry.open(at)
        registry.admit_run(
            replace(_row(root, run_id, at), phase=RunPhase.PENDING),
            False,
            at,
            100,
            session_profile=profile,
            writer_red=receipt,
        )
        assert registry.writer_red(run_id) == (receipt, at)
        assert _c1lock_history_json(capsys)["runs"][0]["red_admission"] == "valid"
        key = "writer_red" if corruption == "receipt" else "session_profile"
        event = next(e for e in registry.read_run_events(run_id) if key in e.payload)
        payload = json.loads(json.dumps(event.payload))
        if corruption == "receipt":
            payload[key]["commit"] = "b" * 40
        else:
            payload[key]["execution_class"] = "verification"
        trigger = registry._one(
            "SELECT sql FROM sqlite_master WHERE name = ?",
            ("run_events_append_only_update",),
        )["sql"]
        with registry._transaction():
            registry._run("DROP TRIGGER run_events_append_only_update")
            registry._run(
                "UPDATE run_events SET payload = ? WHERE run_id = ? AND seq = ?",
                (json.dumps(payload), run_id, event.seq),
            )
            registry._run(trigger)
        before = registry.read_run_events(run_id)
        assert (
            next(e for e in before if e.seq == event.seq).chained_hash
            == event.chained_hash
        )
        if reader == "receipt":
            with pytest.raises(WriterRedCorrupt, match="chain"):
                registry.writer_red(run_id)
        else:
            projected = _c1lock_history_json(capsys)["runs"][0]
            assert projected["red_admission"] == "invalid", projected
            assert projected["red_receipt"] is None
            assert "chain" in projected["red_admission_reason"]
        assert registry.read_run_events(run_id) == before
