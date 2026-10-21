"""Curator inference has one durable owning run, including its native schema."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import nott_database
import pytest
from nspawn.mcp import McpError, McpServer
from nspawn.registry import Registry, create_store, store_path
from nspawn.rows import RunPhase

from nmemory.curator import (
    DIGEST_SCHEMA,
    SCHEMA,
    STAGED_REVIEW_SCHEMA,
    CuratorClient,
    CuratorError,
    CuratorVerdict,
    DigestDraft,
)


@pytest.fixture
def native_curator(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A private roster and a fake installed client; no paid endpoint is reachable."""
    binary = tmp_path / "claude"
    log = tmp_path / "native-calls.jsonl"
    binary.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "args = sys.argv[1:]\n"
        "if args == ['--version']:\n"
        " print('2.1.296 (Claude Code)'); sys.exit(0)\n"
        "prompt = sys.stdin.read()\n"
        "schema = json.loads(args[args.index('--json-schema') + 1])\n"
        "props = schema['properties']\n"
        "out = ({'statement': 'A bounded digest', 'covers': ['cap-one', 'cap-two']}\n"
        " if 'covers' in props else {'verdict': 'ratified', 'reason': 'Still true'}\n"
        " if 'ratified' in props['verdict']['enum'] else\n"
        " {'verdict': 'holds', 'reason': 'Still true', 'new_anchor': None})\n"
        "if os.environ.get('NOTT_NMEMORY_TEST_REASON_BYTES'):\n"
        " out['reason'] = 'x' * int(os.environ['NOTT_NMEMORY_TEST_REASON_BYTES'])\n"
        "with open(os.environ['NOTT_NMEMORY_TEST_LOG'], 'a') as stream:\n"
        " stream.write(json.dumps({'argv': args, 'schema': schema,\n"
        "  'curator': os.environ.get('NOTT_NMEMORY_CURATOR'), 'prompt': prompt,\n"
        "  'oauth_present': bool(os.environ.get('CLAUDE_CODE_OAUTH_TOKEN')),\n"
        "  'own_key_present': bool(os.environ.get('ANTHROPIC_API_KEY')),\n"
        "  'selected_token': os.environ.get('ANTHROPIC_AUTH_TOKEN') == 'fixture-only-credential',\n"
        "  'selected_endpoint': os.environ.get('ANTHROPIC_BASE_URL') == 'https://example.invalid/anthropic',\n"
        "  'home': os.environ.get('HOME'), 'config_home': os.environ.get('CLAUDE_CONFIG_DIR')}) + '\\n')\n"
        "reply = {'type': 'result', 'subtype': 'success', 'is_error': False,\n"
        " 'terminal_reason': 'completed', 'result': '', 'structured_output': out,\n"
        " 'duration_ms': 1, 'duration_api_ms': 1, 'num_turns': 1, 'usage': {},\n"
        " 'total_cost_usd': 0}\n"
        "print(json.dumps(reply))\n"
    )
    binary.chmod(0o755)
    roster = tmp_path / "spawn.toml"
    roster.write_text(
        f"[bins]\nclaude = {json.dumps(str(binary))}\n"
        "[[provider]]\nname = 'zai'\nkind = 'claude'\n"
        "auth_env = 'NOTT_NMEMORY_TEST_TOKEN'\n"
        "model = 'glm-curator-test'\nmodels = ['glm-curator-test']\n"
        "effort = 'low'\nefforts = ['low']\n"
        "[provider.env]\nANTHROPIC_AUTH_TOKEN = '$ZAI_API_KEY'\n"
        "ANTHROPIC_BASE_URL = '$ZAI_BASE_URL'\n"
    )
    for name in (
        "ANTHROPIC_MODEL",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_BASE_URL",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "NOTT_NMEMORY_CURATOR_ALLOW_CLAUDE_QUOTA",
        "NOTT_NSPAWN_NODE_SESSION",
        "NOTT_NSPAWN_GRANT",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NOTT_NSPAWN_CONFIG", str(roster))
    monkeypatch.setenv("NOTT_NSPAWN_ROSTER", str(roster))
    monkeypatch.setenv("NOTT_NSPAWN_RUN_ROOT", str(tmp_path / "runs"))
    monkeypatch.setenv("NOTT_NMEMORY_TEST_LOG", str(log))
    monkeypatch.setenv("NOTT_NMEMORY_TEST_TOKEN", "fixture-only-credential")
    monkeypatch.setenv("NOTT_NMEMORY_CURATOR_PROVIDER", "zai")
    monkeypatch.setenv("NOTT_NMEMORY_CURATOR", "1")
    monkeypatch.setenv("ZAI_API_KEY", "fixture-only-credential")
    monkeypatch.setenv("ZAI_BASE_URL", "https://example.invalid/anthropic")
    monkeypatch.setenv("ZAI_DEFAULT_MODEL", "glm-curator-test")
    monkeypatch.setenv("CLAUDE_CODE_EFFORT_LEVEL", "low")
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    create_store(store_path())
    return log


@pytest.mark.parametrize("operation", ["judge", "review_staged", "synthesize"])
def test_curator_dispatch_is_one_durable_nspawn_run(
    native_curator: Path, operation: str
) -> None:
    client = CuratorClient(timeout_s=20)
    assert client.is_configured()
    if operation == "judge":
        actual = client.judge("Verify the supplied statement.")
        assert actual == CuratorVerdict("holds", "Still true", None)
        schema = SCHEMA
    elif operation == "review_staged":
        assert client.review_staged("Review the supplied proposal.") == (
            "ratified",
            "Still true",
        )
        schema = STAGED_REVIEW_SCHEMA
    else:
        assert client.synthesize("Summarize the supplied statements.") == DigestDraft(
            "A bounded digest", ("cap-one", "cap-two")
        )
        schema = DIGEST_SCHEMA
    calls = [json.loads(line) for line in native_curator.read_text().splitlines()]
    assert len(calls) == 1
    registry = Registry(store_path(), "curator-proof-reader").connect()
    try:
        rows = registry.list_runs()
        # The old direct launcher produces a valid answer above, but no owning run.
        assert len(rows) == 1, "valid curator inference must have one durable session"
        [row] = rows
        assert row.requested_provider == row.executed_provider == "zai"
        assert row.model == "glm-curator-test"
        assert row.phase is RunPhase.CLOSED
        assert row.thread_id is not None
        assert registry.inference_profile(row.run_id) == {
            "tools": "none",
            "output_schema": json.loads(schema),
        }
        keys = nott_database.query(
            registry.connection,
            "SELECT launch_uuid FROM run_keys WHERE run_id = ?",
            (row.run_id,),
        )
        assert len(keys) == 1 and keys[0]["launch_uuid"]
    finally:
        registry.close()
    argv = calls[0]["argv"]
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert "--strict-mcp-config" in argv
    assert "--plugin-dir" not in argv
    assert "--model" not in argv
    assert "--no-session-persistence" not in argv
    assert calls[0]["curator"] == "0"
    assert calls[0]["oauth_present"] is False
    assert calls[0]["own_key_present"] is False
    assert calls[0]["selected_token"] is True
    assert calls[0]["selected_endpoint"] is True
    assert calls[0]["home"] == os.environ["HOME"]


@pytest.mark.parametrize("size", [70000, 600000])
def test_curator_reads_the_whole_native_structured_result(
    native_curator: Path, monkeypatch: pytest.MonkeyPatch, size: int
) -> None:
    monkeypatch.setenv("NOTT_NMEMORY_TEST_REASON_BYTES", str(size))
    actual = CuratorClient(timeout_s=20).judge("Verify the supplied statement.")
    assert actual.reason == "x" * size
    with Registry(store_path(), "curator-proof-reader").connect() as registry:
        [row] = registry.list_runs()
        assert row.phase is RunPhase.CLOSED


def test_curator_native_session_keeps_a_resumable_cwd(native_curator: Path) -> None:
    CuratorClient(timeout_s=20).judge("Verify the supplied statement.")
    with Registry(store_path(), "curator-proof-reader").connect() as registry:
        [row] = registry.list_runs()
        assert Path(row.worktree).is_dir(), "native resume needs its recorded cwd"
    owner = McpServer()
    resumed: str | None = None
    try:
        with pytest.raises(McpError, match="explicit inference_profile"):
            owner.nspawn_run_start(
                "Attempt a broader read-only continuation.",
                row.worktree,
                provider="zai",
                execution_class="judgment",
                session_id=row.thread_id,
                read_only=True,
                timeout_ms=5000,
            )
        accepted = owner.nspawn_run_start(
            "Continue the native inference.",
            row.worktree,
            provider="zai",
            execution_class="judgment",
            session_id=row.thread_id,
            read_only=True,
            timeout_ms=5000,
            inference_profile={"tools": "none", "output_schema": json.loads(SCHEMA)},
        )
        resumed = accepted["run_id"]
        assert (
            owner.nspawn_run_show(run_id=resumed, timeout_ms=5000)["phase"]
            == "completed"
        )
        with Registry(store_path(), "curator-proof-reader").connect() as registry:
            assert registry.inference_profile(resumed) == registry.inference_profile(
                row.run_id
            )
        with pytest.raises(McpError, match="already in use"):
            owner.nspawn_run_start(
                "Attempt overlapping custody of the same native session.",
                row.worktree,
                provider="zai",
                execution_class="judgment",
                session_id=row.thread_id,
                read_only=True,
                timeout_ms=5000,
                inference_profile={
                    "tools": "none",
                    "output_schema": json.loads(SCHEMA),
                },
            )
        with pytest.raises(McpError, match="change its origin or profile"):
            owner.nspawn_run_start(
                "Change the profile.",
                row.worktree,
                provider="zai",
                execution_class="judgment",
                session_id=row.thread_id,
                read_only=True,
                timeout_ms=5000,
                inference_profile={
                    "tools": "none",
                    "output_schema": {"type": "object"},
                },
            )
    finally:
        if resumed is not None:
            owner.nspawn_run_close(run_id=resumed)
        owner.close()
    calls = [json.loads(line) for line in native_curator.read_text().splitlines()]
    assert len(calls) == 2
    assert f"--resume={row.thread_id}" in calls[-1]["argv"]
    assert calls[-1]["schema"] == json.loads(SCHEMA)
    with Registry(store_path(), "curator-proof-reader").connect() as registry:
        assert all(run.phase is RunPhase.CLOSED for run in registry.list_runs())
        assert registry.read_claim(row.worktree) is None


def test_curator_resume_refuses_a_changed_native_home_before_launch(
    native_curator: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    CuratorClient(timeout_s=20).judge("Verify the supplied statement.")
    with Registry(store_path(), "curator-proof-reader").connect() as registry:
        [row] = registry.list_runs()
    changed = tmp_path / "changed-native-home"
    changed.mkdir()
    monkeypatch.setenv("HOME", str(changed))
    owner = McpServer()
    resumed: str | None = None
    try:
        accepted = owner.nspawn_run_start(
            "Continue with a changed native home.",
            row.worktree,
            provider="zai",
            execution_class="judgment",
            session_id=row.thread_id,
            read_only=True,
            timeout_ms=5000,
            inference_profile={"tools": "none", "output_schema": json.loads(SCHEMA)},
        )
        resumed = accepted["run_id"]
        status = owner.nspawn_run_show(run_id=resumed, timeout_ms=5000)
        assert status["phase"] == "failed"
        assert owner.nspawn_result_show(resumed).failure_reason == "InvalidRunRequest"
    finally:
        if resumed is not None:
            owner.nspawn_run_close(run_id=resumed)
        owner.close()
    assert len(native_curator.read_text().splitlines()) == 1


def test_missing_selected_route_never_falls_back_to_claude_quota(
    native_curator: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roster = Path(os.environ["NOTT_NSPAWN_CONFIG"])
    roster.write_text(roster.read_text().replace("name = 'zai'", "name = 'claude'"))
    with pytest.raises(CuratorError, match="zai"):
        CuratorClient(timeout_s=20).judge("Verify the supplied statement.")
    assert not native_curator.exists()
    with Registry(store_path(), "curator-proof-reader").connect() as registry:
        assert registry.list_runs() == ()


def test_prompt_is_data_on_stdin_and_never_an_option(native_curator: Path) -> None:
    prompt = "-starts with a dash"
    CuratorClient(timeout_s=20).judge(prompt)
    [call] = [json.loads(line) for line in native_curator.read_text().splitlines()]
    assert call["prompt"] == prompt
    assert prompt not in call["argv"]


def test_curator_poll_follows_the_run_its_work_continued_as(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """nSPAWN moved the work off a failed account. The first run
    failed and names its successor: the curator waits for the successor, reads
    its answer and closes both runs, the successor first: the driver's close
    watcher serves only the run that holds the work."""
    moved = "22222222-2222-4222-8222-222222222222"
    shows = iter(
        [
            {
                "run_id": "curator-1",
                "phase": "failed",
                "warning": f"HTTP 429; continued as run {moved} on codex/second",
            },
            {"run_id": moved, "phase": "running"},
            {"run_id": moved, "phase": "completed"},
        ]
    )
    calls: list[tuple[str, dict[str, Any]]] = []

    class Owner:
        def call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
            calls.append((name, args))
            payload = (
                {"run_id": "curator-1"}
                if name == "nspawn_run_start"
                else next(shows)
                if name == "nspawn_run_show"
                else {"run_id": args["run_id"], "phase": "closed"}
            )
            return {"content": [{"text": json.dumps(payload)}]}

        def nspawn_result_show(self, run_id: str) -> Any:
            calls.append(("nspawn_result_show", {"run_id": run_id}))
            return SimpleNamespace(
                final_text="[]", structured_output=None, failure_reason=None
            )

        def close(self) -> None:
            pass

    monkeypatch.setattr("nspawn.mcp.McpServer", Owner)
    monkeypatch.setattr(
        "nmemory.curator.selected_curator_route",
        lambda: SimpleNamespace(target="fixture"),
    )
    client = CuratorClient(custom_hook=lambda _: None)  # type: ignore[arg-type,return-value]
    monkeypatch.setattr(client, "child_env", dict)
    envelope = json.loads(client._invoke("Read repository facts.", None, cwd=tmp_path))
    assert envelope["is_error"] is False
    assert envelope["result"] == "[]"
    assert [a["run_id"] for n, a in calls if n == "nspawn_run_show"] == [
        "curator-1",
        moved,
        moved,
    ]
    assert ("nspawn_result_show", {"run_id": moved}) in calls
    assert [a for n, a in calls if n == "nspawn_run_close"] == [
        {"run_id": moved},
        {"run_id": "curator-1"},
    ]
