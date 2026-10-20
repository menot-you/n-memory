"""Recorded tokens use retained events and local calendar days."""

import copy
import json
import subprocess
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from nott_socket.mcp import McpClient, McpRefused, StdioServer
from registry_reset import sql
from supervisor_support import registry
from test_admission import profile
from test_agy_acp import bare_agent

from nspawn import agy_acp, native, route, terminal
from nspawn.mcp import NSPAWN_TOOL_BY_NAME, McpError, McpServer
from nspawn.redact import SecretNeedles
from nspawn.registry import Registry, RegistryError, RunKey
from nspawn.rows import (
    Backend,
    NativeSessionContext,
    RunPhase,
    RunRow,
    SessionActivation,
)
from nspawn.supervisor import Supervisor, create_run_directory

__all__ = ["registry"]
ROOT = Path(__file__).parents[3]
FIXTURES = Path(__file__).parent / "fixtures"
NOW = int(datetime(2026, 10, 26, 12, tzinfo=ZoneInfo("UTC")).timestamp() * 1000)


def stamp(day: str, zone: str = "UTC") -> int:
    return int(
        datetime.fromisoformat(day).replace(tzinfo=ZoneInfo(zone)).timestamp() * 1000
    )


def admit(
    reg: Registry, ident: str, *, owner: str | None = "node:a", at: int = NOW
) -> None:
    reg.admit_run(
        RunRow(
            run_id=ident,
            worktree="/project",
            phase=RunPhase.PENDING,
            sandbox="read-only",
            model="m",
            owner_instance=reg.instance_id,
            created_at=at,
            updated_at=at,
            backend=Backend.CLAUDE,
            owner=owner,
        ),
        False,
        at,
        1000,
    )


def usage(
    reg: Registry, ident: str, at: int, amount: object, output: object = 2
) -> None:
    reg.append_run_event(
        ident,
        at=at,
        kind="usage",
        payload={
            "input_tokens": amount,
            "output_tokens": output,
            "cache_read_input_tokens": 40,
            "cache_creation_input_tokens": 20,
        },
    )


def show(reg: Registry, zone: str = "UTC") -> dict[str, Any]:
    return reg.usage_show(zone, now=NOW)


def observer(
    reg: Registry,
    tmp_path: Path,
    provider: str = "claude",
    *,
    subscription: bool = False,
) -> tuple[Supervisor, Any]:
    admit(reg, "managed")
    sql(reg, "UPDATE runs SET phase='pending' WHERE run_id='managed'")
    sv = Supervisor(reg, tmp_path / "runs")
    create_run_directory(sv._run_root_path, "managed", dir_fd=sv._run_root)
    selected = SessionActivation(
        provider=provider, account_route=provider + ":fixture@subscription", model="m"
    )
    if subscription:
        selected = replace(
            selected,
            profile=str(tmp_path / "profile.json"),
            profile_digest=route.document_digest(profile()),
            grant_root=str(tmp_path),
        )
    return sv, sv._session_observer("managed", selected, SecretNeedles.none())


def recorded_prompt(provider: str) -> tuple[dict[str, Any], dict[str, Any]]:
    name = "claude2-node.log.jsonl" if provider == "claude" else "codex-node.log.jsonl"
    frames = [
        json.loads(line)
        for line in (ROOT / "harness/tests/acp/fixtures" / name)
        .read_text()
        .splitlines()
    ]
    answer = next(
        row["frame"] for row in frames if "stopReason" in row["frame"].get("result", {})
    )
    request = next(
        row["frame"]
        for row in frames
        if row["frame"].get("method") == "session/prompt"
        and row["frame"]["id"] == answer["id"]
    )
    return request, answer


@pytest.mark.parametrize("provider,expected", [("claude", 64612), ("codex", 0)])
def test_recorded_acp_counters_and_occupancy(
    registry: Registry, tmp_path: Path, provider: str, expected: int
) -> None:
    sv, observe = observer(registry, tmp_path, provider)
    try:
        request, answer = recorded_prompt(provider)
        observe("input", request)
        observe("output", answer)
        observed = [
            event.payload
            for event in registry.read_run_events("managed")
            if event.kind == "usage"
        ]
        assert len(observed) == 1
        assert observed[0]["input_tokens"] == (64446 if provider == "claude" else None)
        report = registry.usage_show("UTC")
        assert report["agents"][0]["recordedTokens"] == expected
        assert report["agents"][0]["state"] == (
            "recorded" if provider == "claude" else "partial"
        )
    finally:
        sv.shutdown()


@pytest.mark.parametrize(
    "bad", [None, -1, True, "4", 1.5, float("inf"), float("nan"), 2**53]
)
def test_bad_counters_do_not_change_the_turn(
    registry: Registry, tmp_path: Path, bad: object
) -> None:
    sv, observe = observer(registry, tmp_path)
    try:
        request, answer = recorded_prompt("claude")
        answer["result"]["usage"]["cachedWriteTokens"] = bad
        observe("input", request)
        observe("output", answer)
        assert sv._read("managed").phase is RunPhase.IDLE
        assert registry.usage_show("UTC")["agents"][0]["state"] == "partial"
    finally:
        sv.shutdown()


@pytest.mark.parametrize(
    "missing", ["inputTokens", "outputTokens", "cachedReadTokens", "cachedWriteTokens"]
)
def test_missing_required_counters_remain_unknown(
    registry: Registry, tmp_path: Path, missing: str
) -> None:
    sv, observe = observer(registry, tmp_path)
    try:
        request, answer = recorded_prompt("claude")
        del answer["result"]["usage"][missing]
        observe("input", request)
        observe("output", answer)
        assert registry.usage_show("UTC")["agents"][0]["state"] == "partial"
    finally:
        sv.shutdown()


def test_reported_zero_has_coverage_but_exec_zero_is_unknown(
    registry: Registry, tmp_path: Path
) -> None:
    sv, observe = observer(registry, tmp_path)
    try:
        request, answer = recorded_prompt("claude")
        answer["result"]["usage"] = {
            name: 0
            for name in (
                "inputTokens",
                "outputTokens",
                "cachedReadTokens",
                "cachedWriteTokens",
            )
        }
        observe("input", request)
        observe("output", answer)
        report = registry.usage_show("UTC")
        assert report["agents"][0]["recordedTokens"] == 0
        assert report["agents"][0]["state"] == "recorded"
        admit(registry, "exec", owner="node:b")
        usage(registry, "exec", int(report["observedAt"]), 0, 0)
        assert registry.usage_show("UTC")["agents"][1]["state"] == "partial"
    finally:
        sv.shutdown()


def test_actual_admission_binding_preserves_node_identity(registry: Registry) -> None:
    registry.admission_bind(
        group="workflow",
        node="node:00000000-0000-4000-8000-000000000001",
        project="/project",
        decision="decision",
        generation=0,
        key="key",
        fingerprint="a" * 64,
    )
    registry.admit_run(
        RunRow(
            run_id="bound",
            worktree="/project",
            phase=RunPhase.PENDING,
            sandbox="read-only",
            model="m",
            owner_instance=registry.instance_id,
            created_at=NOW,
            updated_at=NOW,
            backend=Backend.CLAUDE,
            owner="centre",
            group_name="workflow",
        ),
        False,
        NOW,
        1000,
        run_key=RunKey("launch", "/project", "key", "a" * 64, "b" * 64),
    )
    usage(registry, "bound", NOW, 8)
    assert (
        show(registry)["agents"][0]["nodeId"] == "00000000-0000-4000-8000-000000000001"
    )
    assert show(registry)["unassigned"]["recordedTokens"] is None


def test_resume_and_account_switch_keep_identity_and_fork_starts_new_identity(
    registry: Registry,
) -> None:
    for ident, node, account in [
        ("first", "node:a", "one"),
        ("resume", "node:a", "two"),
        ("fork", "node:b", "two"),
    ]:
        admit(registry, ident, owner=node)
        row = registry.get_run(ident)
        assert row is not None
        registry.update_run(replace(row, executed_account=account))
        usage(registry, ident, NOW, 3)
    assert [
        (agent["nodeId"], agent["recordedTokens"]) for agent in show(registry)["agents"]
    ] == [("a", 10), ("b", 5)]


def test_calendar_coverage_retention_duplicates_and_totals(registry: Registry) -> None:
    admit(registry, "one", at=stamp("2026-10-24T23:50:00"))
    usage(registry, "one", stamp("2026-10-25T00:01:00"), 10)
    usage(registry, "one", stamp("2026-10-25T00:02:00"), 10)
    usage(registry, "one", stamp("2026-10-25T01:30:00"), None)
    admit(registry, "unassigned", owner="centre", at=NOW)
    usage(registry, "unassigned", NOW, 5)
    before = show(registry)
    assert before["days"][-3:] == [
        {"day": "2026-10-24", "recordedTokens": 0, "state": "recorded"},
        {"day": "2026-10-25", "recordedTokens": 12, "state": "partial"},
        {"day": "2026-10-26", "recordedTokens": 7, "state": "recorded"},
    ]
    assert before["agents"][0]["recordedTokens"] == 12
    assert "name" not in before["agents"][0]
    assert before["days"][0]["state"] == "not_recorded"
    assert before["unassigned"]["recordedTokens"] == 7
    sql(registry, "UPDATE runs SET phase='closed', updated_at=1")
    assert registry.prune_closed_runs(NOW, 0, 0) == 2
    assert show(registry) == before


def test_local_midnight_and_dst_use_calendar_dates(registry: Registry) -> None:
    admit(registry, "one", at=stamp("2026-09-01T12:00:00"))
    usage(registry, "one", stamp("2026-10-24T22:01:00"), 10)
    admit(registry, "two", at=stamp("2026-09-01T12:00:00"))
    usage(registry, "two", stamp("2026-10-25T22:30:00"), 20)
    report = show(registry, "Europe/Amsterdam")
    assert len(report["days"]) == 30
    assert report["days"][-2]["day"] == "2026-10-25"
    assert report["days"][-2]["recordedTokens"] == 34
    assert report["days"][-1]["recordedTokens"] == 0
    assert show(registry)["days"][-3]["recordedTokens"] == 12


def test_caps_and_safe_totals_are_visible(
    registry: Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nspawn import registry as module

    admit(registry, "one")
    usage(registry, "one", NOW, 2**53 - 2, 0)
    admit(registry, "two")
    usage(registry, "two", NOW, 2, 0)
    report = show(registry)
    assert report["days"][-1] == {
        "day": "2026-10-26",
        "recordedTokens": 2**53 - 2,
        "state": "partial",
    }
    monkeypatch.setattr(module, "USAGE_EVENT_CAP", 1)
    assert show(registry)["incomplete"] is True
    assert show(registry)["days"][-1]["state"] == "partial"
    monkeypatch.setattr(module, "USAGE_EVENT_CAP", 100)
    admit(registry, "three", owner="node:b")
    monkeypatch.setattr(module, "USAGE_AGENT_CAP", 1)
    assert show(registry)["incomplete"] is True


def test_tool_contract_and_invalid_zone(registry: Registry, tmp_path: Path) -> None:
    assert "nspawn_usage_show" in NSPAWN_TOOL_BY_NAME
    sv = Supervisor(registry, tmp_path / "runs")
    try:
        server = McpServer(supervisor=sv)
        reply = server.call_tool("nspawn_usage_show", {"timeZone": "UTC"})
        payload = json.loads(reply["content"][0]["text"])
        assert payload["version"] == 1
        assert payload["timeZone"] == "UTC"
        assert payload["agents"] == []
        assert all(day["recordedTokens"] is None for day in payload["days"])
        for zone in ["bad/zone", "../UTC", "", "/etc/passwd"]:
            with pytest.raises(McpError) as failure:
                server.call_tool("nspawn_usage_show", {"timeZone": zone})
            assert failure.value.code == -32602
        monkey = pytest.MonkeyPatch()
        monkey.setattr(
            registry,
            "_rows",
            lambda *args: (_ for _ in ()).throw(
                RegistryError("History is unavailable")
            ),
        )
        with pytest.raises(McpError):
            server.call_tool("nspawn_usage_show", {"timeZone": "UTC"})
        monkey.undo()
    finally:
        sv.shutdown()


def subscription_frames(tmp_path: Path) -> list[dict[str, Any]]:
    prepared = tmp_path / "profile.json"
    prepared.write_text(json.dumps(profile()))
    script = r"""
import {readFileSync} from 'node:fs';
const {AdmissionConsumer} = await import(process.argv[1]);
const profile = JSON.parse(readFileSync(process.argv[2], 'utf8'));
const schema = JSON.parse(readFileSync(process.argv[3], 'utf8'));
const records = readFileSync(process.argv[4], 'utf8').split('\n').filter(line => line.startsWith('{')).map(JSON.parse);
const consumer = new AdmissionConsumer(profile, schema, {
 sdk:{query(){},resolveSettings(){}},
 notify:(method,params)=>process.stdout.write(JSON.stringify({method,params})+'\n')
});
for (const id of ['attempt-1','attempt-2']) {
 const attempt = consumer.register({profile_digest:consumer.digest,key:{activation_id:'activation-1',attempt_id:id,owner_turn_id:'turn-1'}});
 attempt.input = {close(){}};
 attempt.query = {async *[Symbol.asyncIterator](){yield* records;}};
 await consumer.consume(attempt);
}
"""
    result = subprocess.run(
        [
            "node",
            "--input-type=module",
            "-e",
            script,
            (ROOT / "harness/nott_harness/acp/claude.mjs").as_uri(),
            str(prepared),
            str(ROOT / "mcps/nspawn/nspawn/schema/admission.v1.schema.json"),
            str(FIXTURES / "claude-stream-wire-2.1.251.ndjson"),
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return [json.loads(line) for line in result.stdout.splitlines()]


def test_actual_subscription_consumer_counts_attempts_once(
    registry: Registry, tmp_path: Path
) -> None:
    sv, observe = observer(registry, tmp_path, subscription=True)
    try:
        frames = subscription_frames(tmp_path)
        for frame in frames + frames:
            observe("output", frame)
        report = registry.usage_show("UTC")
        assert report["agents"][0]["recordedTokens"] == 65067 * 2
        assert report["agents"][0]["state"] == "recorded"
        assert (
            len([e for e in registry.read_run_events("managed") if e.kind == "usage"])
            == 2
        )
    finally:
        sv.shutdown()


def test_actual_agy_adapter_reads_recorded_source_stream(
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from nspawn.owner_roster import NativeAccount

    records = [
        json.loads(line)
        for line in (FIXTURES / "agy-stream-wire-1.1.27.ndjson")
        .read_text()
        .splitlines()
    ]
    second = next(i for i, row in enumerate(records[1:], 1) if row["event"] == "init")
    stream = tmp_path / "stream.ndjson"
    stream.write_text("".join(json.dumps(row) + "\n" for row in records[second:]))
    peer = tmp_path / "peer.py"
    peer.write_text(
        "import sys\nfrom pathlib import Path\nlines=Path(sys.argv[1]).read_text().splitlines()\nprint(lines[0],flush=True)\nsys.stdin.readline()\nfor line in lines[1:]: print(line,flush=True)\n"
    )
    launch = native.NativeLaunch(
        (sys.executable, str(peer), str(stream)),
        {},
        SecretNeedles.none(),
        NativeSessionContext(str(tmp_path)),
        NativeAccount(home=tmp_path),
    )
    monkeypatch.setattr(native, "prepare_native", lambda *args, **kwargs: launch)
    monkeypatch.setattr(native, "agy_confined", lambda launch, **kwargs: launch)
    agent = bare_agent()
    agent.sessions["s"] = agy_acp.Session(tmp_path, "m")
    agent.sessions["s"].counted = copy.deepcopy(records[second - 1]["result"]["usage"])
    result = agent.prompt(
        {"sessionId": "s", "prompt": [{"type": "text", "text": "PONG"}]}
    )
    capsys.readouterr()
    assert result["usage"]["inputTokens"] == 5493
    sv, observe = observer(registry, tmp_path, "agy")
    try:
        request = {"id": 1, "method": "session/prompt"}
        observe("input", request)
        observe("output", {"id": 1, "result": result})
        assert registry.usage_show("UTC")["agents"][0]["recordedTokens"] == 34093
    finally:
        sv.shutdown()


def test_safe_totals_match_visible_series(registry: Registry) -> None:
    admit(registry, "large", owner="node:a")
    usage(registry, "large", NOW, 2**53 - 2, 0)
    admit(registry, "small", owner="node:b")
    usage(registry, "small", NOW, 2, 0)
    report = show(registry)
    assert sum(a["recordedTokens"] or 0 for a in report["agents"]) + (
        report["unassigned"]["recordedTokens"] or 0
    ) == sum(d["recordedTokens"] or 0 for d in report["days"])
    assert all(a["recordedTokens"] <= 2**53 - 1 for a in report["agents"])
    assert report["agents"][1]["state"] == "partial"


def test_unknown_prompt_answer_and_duplicate_replay(
    registry: Registry, tmp_path: Path
) -> None:
    sv, observe = observer(registry, tmp_path)
    try:
        request, answer = recorded_prompt("claude")
        del answer["result"]["usage"]
        observe("input", request)
        observe("output", answer)
        observe("output", answer)
        assert registry.usage_show("UTC")["agents"][0]["state"] == "partial"
        assert (
            len([e for e in registry.read_run_events("managed") if e.kind == "usage"])
            == 1
        )
    finally:
        sv.shutdown()


def test_pending_turn_is_partial_until_its_answer(
    registry: Registry, tmp_path: Path
) -> None:
    sv, observe = observer(registry, tmp_path)
    try:
        request, answer = recorded_prompt("claude")
        observe("input", request)
        assert registry.usage_show("UTC")["agents"][0]["state"] == "partial"
        observe("output", answer)
        assert registry.usage_show("UTC")["agents"][0]["state"] == "recorded"
    finally:
        sv.shutdown()


def test_subscription_missing_result_is_unknown(
    registry: Registry, tmp_path: Path
) -> None:
    sv, observe = observer(registry, tmp_path, subscription=True)
    try:
        frames = subscription_frames(tmp_path)
        for frame in frames:
            if frame.get("method") != "_nott/turn/result":
                observe("output", frame)
        assert registry.usage_show("UTC")["agents"][0]["state"] == "partial"
    finally:
        sv.shutdown()


def test_agent_cap_keeps_totals_and_inline_reply(registry: Registry) -> None:
    from nspawn.mcp import MAX_RESPONSE_BYTES
    from nspawn.registry import USAGE_AGENT_CAP

    for index in range(USAGE_AGENT_CAP + 1):
        admit(registry, str(index), owner="node:" + str(index) + "x" * 500)
        usage(registry, str(index), NOW, 4)
    report = show(registry)
    assert report["incomplete"] is True
    assert len(report["agents"]) == USAGE_AGENT_CAP
    assert report["days"][-1]["recordedTokens"] == sum(
        agent["recordedTokens"] for agent in report["agents"]
    )
    assert len(json.dumps(report).encode()) < MAX_RESPONSE_BYTES


def test_managed_tokens_do_not_count_parent_exec_total(
    registry: Registry, tmp_path: Path
) -> None:
    sv, observe = observer(registry, tmp_path)
    try:
        request, answer = recorded_prompt("claude")
        observe("input", request)
        observe("output", answer)
        usage(
            registry, "managed", int(registry.usage_show("UTC")["observedAt"]), 64612, 0
        )
        assert registry.usage_show("UTC")["agents"][0]["recordedTokens"] == 64612
    finally:
        sv.shutdown()


def test_terminal_without_counters_does_not_copy_classification(
    registry: Registry, tmp_path: Path
) -> None:
    admit(registry, "exec", at=0)
    sv = Supervisor(registry, tmp_path / "runs")
    try:
        sv._publish_terminal(
            "exec",
            terminal.TerminalClassification(
                phase=RunPhase.FAILED,
                final_text=None,
                warning=None,
                marker=None,
                provider_wall=None,
                account_wall=None,
                tokens=999,
            ),
        )
        report = registry.usage_show("UTC")
        assert report["agents"][0]["recordedTokens"] == 0
        assert report["agents"][0]["state"] == "partial"
    finally:
        sv.shutdown()


def test_usage_through_the_real_mcp_stdio(registry: Registry, tmp_path: Path) -> None:
    sv, observe = observer(registry, tmp_path)
    try:
        request, answer = recorded_prompt("claude")
        observe("input", request)
        observe("output", answer)
        peer = StdioServer(sys.executable, ("-m", "nspawn.cli", "mcp", "serve"))
        with McpClient(peer, tmp_path, deadline_s=3) as client:
            report = client.call("nspawn_usage_show", {"timeZone": "UTC"})
            assert report["version"] == 1
            assert report["agents"][0]["recordedTokens"] == 64612
            assert "name" not in report["agents"][0]
            with pytest.raises(McpRefused) as refused:
                client.call("nspawn_usage_show", {"timeZone": "../UTC"})
            assert "timeZone must identify an IANA time zone" in refused.value.message
            assert (
                client.call("nspawn_usage_show", {"timeZone": "UTC"})["agents"][0][
                    "recordedTokens"
                ]
                == 64612
            )
    finally:
        sv.shutdown()


def test_old_history_does_not_consume_the_current_window_cap(
    registry: Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nspawn import registry as module

    admit(registry, "old", at=stamp("2026-08-01T12:00:00"))
    usage(registry, "old", stamp("2026-08-01T12:01:00"), 10)
    usage(registry, "old", NOW, 5)
    monkeypatch.setattr(module, "USAGE_EVENT_CAP", 2)
    report = show(registry)
    assert report["days"][-1]["recordedTokens"] == 7
    assert report["days"][0]["state"] == "recorded"
    assert report["incomplete"] is False


def test_exact_event_and_agent_limits_do_not_claim_truncation(
    registry: Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nspawn import registry as module

    admit(registry, "one", owner="node:a")
    admit(registry, "two", owner="node:b")
    usage(registry, "one", NOW, 3)
    usage(registry, "two", NOW, 4)
    monkeypatch.setattr(module, "USAGE_EVENT_CAP", 4)
    monkeypatch.setattr(module, "USAGE_AGENT_CAP", 2)
    report = show(registry)
    assert report["incomplete"] is False
    assert report["days"][-1]["recordedTokens"] == 11


def test_active_and_failed_execs_never_report_complete_zero(
    registry: Registry, tmp_path: Path
) -> None:
    from nspawn.supervisor import RunDeadlineExceeded

    admit(registry, "no-counters")
    sv = Supervisor(registry, tmp_path / "runs")
    try:
        assert show(registry)["agents"][0]["state"] == "partial"
        sv._fail_run("no-counters", RunDeadlineExceeded("deadline"))
        assert show(registry)["agents"][0]["state"] == "partial"
        sql(registry, "UPDATE runs SET phase='closed', updated_at=1")
        registry.prune_closed_runs(NOW, 0, 0)
        assert show(registry)["agents"][0]["state"] == "partial"
    finally:
        sv.shutdown()


@pytest.mark.parametrize("missing", ["input_tokens", "output_tokens"])
def test_terminal_keeps_an_absent_counter_unknown(
    registry: Registry, tmp_path: Path, missing: str
) -> None:
    frame = next(
        json.loads(line)
        for line in (FIXTURES / "claude-stream-wire-2.1.251.ndjson")
        .read_text()
        .splitlines()
        if line.startswith("{") and json.loads(line).get("type") == "result"
    )
    frame["usage"] = {"input_tokens": 5, "output_tokens": 7}
    frame["usage"].pop(missing)
    stdout, stderr = tmp_path / "stdout", tmp_path / "stderr"
    stdout.write_text(json.dumps(frame) + "\n")
    stderr.write_text("")
    result = terminal.classify_terminal_run(
        stdout, stderr, 0, provider="claude", account_alias="fixture", now=NOW // 1000
    )
    assert result.tokens == (7 if missing == "input_tokens" else 5)
    admit(registry, "incomplete-exec", at=0)
    sv = Supervisor(registry, tmp_path / "runs")
    try:
        sv._publish_terminal("incomplete-exec", result)
        report = registry.usage_show("UTC")
        assert report["agents"][0]["state"] == "partial"
        assert report["agents"][0]["recordedTokens"] == 0
    finally:
        sv.shutdown()
