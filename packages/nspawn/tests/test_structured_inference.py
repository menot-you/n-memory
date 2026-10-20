"""A closed inference profile preserves native schema without widening tools."""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import nott_database
import pytest
from registry_reset import sql, store
from supervisor_support import (
    _drain_worker_threads,
    _stop_every_reaper,
    _sv,
    fake_picked,
    fake_provider,
    registry,
    roster_home,
)
from test_native_execute import native_client

from nspawn import picker
from nspawn.child import ChildArgvError, compose_child_argv
from nspawn.driver import request_fingerprint
from nspawn.inference import StructuredInference
from nspawn.registry import Registry
from nspawn.rows import Backend, RunPhase, RunRow
from nspawn.schema import LEGACY_THREAD_INDEX_DDL, REGISTRY_SCHEMA_VERSION
from nspawn.supervisor import DuplicateSession, InvalidRunRequest

__all__ = [
    "_drain_worker_threads",
    "_stop_every_reaper",
    "fake_picked",
    "fake_provider",
    "native_client",
    "registry",
    "roster_home",
]

PROFILE: dict[str, Any] = {
    "tools": "none",
    "output_schema": {
        "type": "object",
        "properties": {"decision": {"type": "string"}},
        "required": ["decision"],
        "additionalProperties": False,
    },
}


CODEX_INFERENCE_PEER = r"""
import json, os, sys, tomllib
from pathlib import Path
origin = Path(ORIGIN)
home = Path(os.environ['CODEX_HOME'])
mode = MODE
config = {}
def put(key, value):
    target = config
    keys = key.split('.')
    for part in keys[:-1]:
        target = target.setdefault(part, {})
    target[keys[-1]] = value
for index, arg in enumerate(sys.argv):
    if arg == '-c':
        raw = sys.argv[index + 1]
        key, encoded = raw.split('=', 1)
        put(key, tomllib.loads('value=' + encoded)['value'])
def send(frame):
    print(json.dumps(frame), flush=True)
history = home / 'sessions/2026/10/11/rollout-peer.jsonl'
if mode == 'foreign-history':
    history = origin / 'foreign.jsonl'
for line in sys.stdin:
    frame = json.loads(line)
    method, params = frame.get('method'), frame.get('params') or {}
    with (origin / 'inference-requests.jsonl').open('a') as log:
        log.write(json.dumps(frame) + '\n')
    if method == 'initialize':
        assert '--dangerously-bypass-hook-trust' not in sys.argv
        assert Path.cwd() != origin and os.environ['HOME'] != str(home)
        assert 'NOTT_DATABASE_DIR' not in os.environ
        send({'id': frame['id'], 'result': {'userAgent': 'inference-peer'}})
    elif method == 'config/read':
        effective = json.loads(json.dumps(config))
        if mode == 'closure-drift': effective['features']['hooks'] = True
        if mode == 'open-mcp': effective['mcp_servers']['inherited']['enabled'] = True
        layers = [{'name': {'type': 'sessionFlags'}, 'config': effective, 'version': 'peer'}]
        if mode == 'project-layer':
            layers.append({'name': {'type': 'project'}, 'config': {}, 'version': 'peer'})
        selected = {'developer_instructions': '', 'web_search': 'disabled'}
        if mode == 'legacy-instructions': selected['instructions'] = 'ambient owner-file instruction'
        send({'id': frame['id'], 'result': {'config': selected, 'layers': layers}})
    elif method in ('thread/start', 'thread/resume'):
        assert params.get('dynamicTools') == []
        assert params.get('selectedCapabilityRoots') == []
        assert params.get('developerInstructions') == ''
        send({'id': frame['id'], 'result': {
            'thread': {'id': 'native-thread', 'path': str(history)},
            'model': params['model'], 'modelProvider': 'openai',
            'sandbox': {'type': 'readOnly', 'networkAccess': False},
            'instructionSources': ['ambient.md'] if mode == 'ambient-instructions' else [],
            'cwd': str(Path.cwd()), 'approvalPolicy': 'never'}})
    elif method == 'mcpServerStatus/list':
        send({'id': frame['id'], 'result': {'data': [], 'nextCursor': None}})
    elif method == 'turn/start':
        assert params['collaborationMode'] == {'mode': 'default', 'settings': {
            'model': params['model'], 'reasoning_effort': params.get('effort'),
            'developer_instructions': None}}
        assert params['outputSchema']['required'] == ['decision']
        count = 1 + (history.read_text().count('"type": "task_started"') if history.exists() else 0)
        turn = 'native-turn' if count == 1 else f'native-turn-{count}'
        answer = json.dumps({'decision': 'x' * 70000 if mode == 'whole-answer' else 'holds'}, separators=(',', ':'))
        if mode == 'structured-malformed': answer = '{"decision":'
        if mode == 'structured-schema': answer = '{"decision":7}'
        if mode == 'structured-nonfinite': answer = '{"decision":NaN}'
        records = ([] if history.exists() else [
            {'type': 'session_meta', 'payload': {'id': 'native-thread', 'cwd': str(Path.cwd())}}
        ]) + [
            {'type': 'task_started', 'payload': {'turn_id': turn, 'collaboration_mode_kind': 'default'}},
            {'type': 'turn_context', 'payload': {'turn_id': turn, 'cwd': str(Path.cwd()),
                'model': params['model'], 'effort': params.get('effort'),
                'collaboration_mode': params['collaborationMode']}},
            {'type': 'message', 'payload': {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': params['input'][0]['text']}]}}
        ]
        if mode == 'hidden-tool':
            records += [{'type': 'function_call', 'payload': {'name': 'request_user_input', 'call_id': 'hidden-call'}},
                {'type': 'function_call_output', 'payload': {'call_id': 'hidden-call', 'output': 'unavailable in Default mode'}}]
        records += [{'type': 'message', 'payload': {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': answer}]}},
                    {'type': 'task_complete', 'payload': {'turn_id': turn, 'last_agent_message': answer}}]
        if mode == 'incomplete-history': records.pop()
        if mode == 'wrong-history-turn': records[2]['payload']['turn_id'] = 'foreign-turn'
        if mode != 'missing-history':
            history.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            for record in records:
                kind = record['type']
                if kind in {'message', 'function_call', 'function_call_output'}:
                    record['type'] = 'response_item'
                    record['payload']['type'] = kind
                elif kind in {'task_started', 'task_complete'}:
                    record['type'] = 'event_msg'
                    record['payload']['type'] = kind
            with history.open('a') as native_history:
                native_history.write(''.join(json.dumps(record) + '\n' for record in records))
            history.chmod(0o600)
        send({'id': frame['id'], 'result': {'turn': {'id': turn, 'status': 'inProgress'}}})
        if mode == 'client-request':
            send({'id': 'client-input', 'method': 'item/tool/requestUserInput',
                  'params': {'threadId': 'native-thread', 'turnId': turn, 'itemId': 'input', 'questions': []}})
            denied = json.loads(sys.stdin.readline())
            assert denied['id'] == 'client-input' and 'error' in denied
        usage = {'inputTokens': 6, 'outputTokens': 3, 'totalTokens': 9,
                 'cachedInputTokens': 0, 'reasoningOutputTokens': 0}
        send({'method': 'thread/tokenUsage/updated', 'params': {'threadId': 'native-thread',
              'turnId': turn, 'tokenUsage': {'total': {key: value * count for key, value in usage.items()}, 'last': usage}}})
        send({'method': 'item/completed', 'params': {'threadId': 'native-thread', 'turnId': turn,
             'item': {'id': 'answer', 'type': 'agentMessage', 'text': answer}}})
        send({'method': 'turn/completed', 'params': {'threadId': 'native-thread',
             'turn': {'id': turn, 'status': 'completed'}}})
"""


def codex_inference_peer(cwd: Path, mode: str = "held") -> None:
    """Same native account fixture, with full native history at its owning home."""
    home = cwd / "first"
    (home / "models_cache.json").write_text(
        json.dumps(
            {
                "models": [
                    {
                        "slug": "test-model",
                        "tool_mode": "code_mode",
                        "multi_agent_version": "v2",
                        "node_repl_disabled": False,
                        "apply_patch_tool_type": "freeform",
                        "experimental_supported_tools": ["fixture"],
                    }
                ]
            }
        )
    )
    (home / "config.toml").write_text('[mcp_servers.inherited]\ncommand="false"\n')
    (cwd / "vendor").write_text(
        f"#!{sys.executable}\nORIGIN={str(cwd)!r}\nMODE={mode!r}\n"
        + CODEX_INFERENCE_PEER
    )


def start_codex_inference(client: Any, cwd: Path) -> str:
    return client.call(
        "nspawn_run_start",
        {
            "prompt": "Infer the decision.",
            "cwd": str(cwd),
            "provider": "first",
            "class": "judgment",
            "read_only": True,
            "inference_profile": PROFILE,
        },
    )["run_id"]


def codex_result(client: Any, cwd: Path, run_id: str) -> dict[str, Any]:
    """Read the real MCP full-result projection when its envelope spills."""
    result = client.call("nspawn_result_show", {"run_id": run_id})
    if set(result) == {"preview", "path", "bytes"}:
        path = Path(result["path"])
        assert path.is_relative_to(cwd / "runs" / run_id)
        data = path.read_bytes()
        assert len(data) == result["bytes"]
        result = json.loads(data)
    assert isinstance(result, dict)
    return result


@pytest.mark.parametrize("native_client", ["codex"], indirect=True)
@pytest.mark.parametrize("mode", ["held", "whole-answer"])
def test_codex_structured_inference_uses_the_owning_run_and_result(
    native_client: Any, mode: str
) -> None:
    """Real MCP/Registry, pipes and original-home JSONL; no provider inference."""
    client, cwd = native_client
    codex_inference_peer(cwd, mode)
    run_id = start_codex_inference(client, cwd)
    status = client.terminal(run_id)
    assert status["phase"] == "completed", status
    assert status["inference_profile"] == PROFILE
    result = codex_result(client, cwd, run_id)
    expected = {"decision": "x" * 70000 if mode == "whole-answer" else "holds"}
    assert result.get("structured_output") == expected, {
        "keys": list(result),
        "status": status,
        "text_bytes": len((result.get("final_text") or "").encode()),
    }
    assert json.loads(result["final_text"]) == expected
    assert status["thread_id"] == "native-thread"
    assert status["executed_account"] == "first"
    with nott_database.connect(store(), readonly=True) as connection:
        connection.row_factory = nott_database.Row
        [context] = nott_database.query(
            connection, "SELECT home FROM native_contexts WHERE run_id = ?", (run_id,)
        )
        assert context["home"] == str(cwd / "first")
    client.call("nspawn_run_close", {"run_id": run_id})
    assert client.call("nspawn_run_show", {"run_id": run_id})["phase"] == "closed"
    assert codex_result(client, cwd, run_id)["structured_output"] == expected


@pytest.mark.parametrize("native_client", ["codex"], indirect=True)
@pytest.mark.parametrize(
    "mode",
    [
        "hidden-tool",
        "client-request",
        "missing-history",
        "incomplete-history",
        "wrong-history-turn",
        "foreign-history",
        "closure-drift",
        "project-layer",
        "ambient-instructions",
        "legacy-instructions",
        "open-mcp",
    ],
)
def test_codex_inference_refuses_missing_closure_or_complete_no_tool_history(
    native_client: Any, mode: str
) -> None:
    client, cwd = native_client
    codex_inference_peer(cwd, mode)
    run_id = start_codex_inference(client, cwd)
    status = client.terminal(run_id)
    assert status["phase"] == "failed", status
    assert "structured_output" not in codex_result(client, cwd, run_id)
    requests = [
        json.loads(line)
        for line in (cwd / "inference-requests.jsonl").read_text().splitlines()
    ]
    turns = [frame for frame in requests if frame.get("method") == "turn/start"]
    assert len(turns) == int(
        mode
        in {
            "hidden-tool",
            "client-request",
            "missing-history",
            "incomplete-history",
            "wrong-history-turn",
            "foreign-history",
        }
    )


@pytest.mark.parametrize("native_client", ["codex"], indirect=True)
def test_codex_inference_resume_keeps_the_recorded_profile_and_full_history(
    native_client: Any,
) -> None:
    client, cwd = native_client
    codex_inference_peer(cwd)
    first = start_codex_inference(client, cwd)
    original = client.terminal(first)
    assert original["phase"] == "completed", original
    client.call("nspawn_run_close", {"run_id": first})
    arguments = {
        "prompt": "Infer the decision again.",
        "cwd": str(cwd),
        "session_id": original["thread_id"],
        "class": "judgment",
        "read_only": True,
    }
    refusal = client.request(
        "tools/call", {"name": "nspawn_run_start", "arguments": arguments}
    )
    assert "error" in refusal
    assert "explicit inference_profile" in json.dumps(refusal)
    resumed = client.call(
        "nspawn_run_start", {**arguments, "inference_profile": PROFILE}
    )["run_id"]
    assert resumed != first
    status = client.terminal(resumed)
    assert status["phase"] == "completed", status
    assert status["thread_id"] == original["thread_id"]
    assert status["executed_account"] == original["executed_account"] == "first"
    assert status["inference_profile"] == PROFILE
    assert codex_result(client, cwd, resumed)["structured_output"] == {
        "decision": "holds"
    }
    history = next((cwd / "first/sessions").rglob("*.jsonl"))
    records = [json.loads(line) for line in history.read_text().splitlines()]
    assert sum(record["type"] == "session_meta" for record in records) == 1
    assert [
        record["payload"]["turn_id"]
        for record in records
        if record["payload"].get("type") == "task_complete"
    ] == ["native-turn", "native-turn-2"]
    client.call("nspawn_run_close", {"run_id": resumed})
    assert client.call("nspawn_run_show", {"run_id": first})["phase"] == "closed"


@pytest.mark.parametrize(
    "profile",
    [
        {},
        {**PROFILE, "tools": "Bash"},
        {**PROFILE, "args": ["--plugin-dir", "/foreign"]},
        {**PROFILE, "output_schema": {"type": "invalid"}},
        {**PROFILE, "output_schema": {"enum": [float("nan")]}},
    ],
)
def test_profile_rejects_widening_and_invalid_schema(profile: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        StructuredInference.from_request(profile)


def test_native_profile_uses_schema_without_plugins_or_ambient_settings() -> None:
    profile = StructuredInference.from_request(PROFILE)
    argv = compose_child_argv(
        "/installed/claude",
        read_only=True,
        inference_profile=profile,
        session_id="native-id",
        declared_args=["--plugin-dir", "/foreign"],
    )
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert json.loads(argv[argv.index("--json-schema") + 1]) == PROFILE["output_schema"]
    assert "--strict-mcp-config" in argv
    assert "--session-id=native-id" in argv
    assert "--plugin-dir" not in argv
    assert "--no-session-persistence" not in argv


@pytest.mark.parametrize(
    "grant",
    [
        {"read_only": False},
        {"read_paths": ["/foreign"]},
        {"plugin_dir": "/foreign"},
        {"append_system_prompt_file": "/foreign/system"},
    ],
)
def test_composer_refuses_extra_authority(grant: dict[str, Any]) -> None:
    with pytest.raises(ChildArgvError):
        compose_child_argv(
            "/installed/claude",
            inference_profile=StructuredInference.from_request(PROFILE),
            **({"read_only": True} | grant),
        )


@pytest.mark.parametrize(
    "grant",
    [
        {"read_only": False},
        {"native_write": True},
        {"read_paths": ["/foreign"]},
        {"system_prompt": "Additional authority"},
        {"orchestrator": {}},
    ],
)
def test_supervisor_refuses_extra_authority_before_admission(
    registry: Registry,
    tmp_path: Path,
    grant: dict[str, Any],
) -> None:
    owner = _sv(registry, tmp_path)
    with pytest.raises(InvalidRunRequest, match="no additional grants"):
        owner.execute(
            "Infer the decision.",
            str(tmp_path),
            inference_profile=PROFILE,
            **({"read_only": True} | grant),
        )
    assert registry.list_runs() == ()


def test_unsupported_transport_refuses_before_a_native_launch(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    fake_picked: picker.PickerResult,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unsupported = replace(
        fake_picked,
        executed_provider=replace(fake_picked.executed_provider, kind="agy"),
    )
    monkeypatch.setattr(picker, "pick_execution", lambda *a, **k: unsupported)
    owner = _sv(registry, tmp_path)
    with pytest.raises(InvalidRunRequest, match="requires a Claude or Codex transport"):
        owner.execute(
            "Infer the decision.",
            str(tmp_path),
            read_only=True,
            execution_class="judgment",
            inference_profile=PROFILE,
        )
    assert registry.list_runs() == ()


def test_keyed_request_fingerprint_binds_native_schema() -> None:
    arguments = {"prompt": "Infer", "cwd": "/private", "execution_class": "judgment"}
    legacy = request_fingerprint("nspawn_run_start", arguments, group=None)
    assert (
        request_fingerprint(
            "nspawn_run_start", {**arguments, "inference_profile": None}, group=None
        )
        == legacy
    )
    first = request_fingerprint(
        "nspawn_run_start", {**arguments, "inference_profile": PROFILE}, group=None
    )
    changed = PROFILE | {"output_schema": {"type": "object", "required": ["different"]}}
    assert first != legacy
    assert (
        request_fingerprint(
            "nspawn_run_start", {**arguments, "inference_profile": changed}, group=None
        )
        != first
    )


def _native_origin(run_id: str, phase: RunPhase, registry: Registry) -> RunRow:
    return RunRow(
        run_id=run_id,
        worktree="/private/inference",
        phase=phase,
        sandbox="read-only",
        model="opus",
        owner_instance=registry.instance_id,
        created_at=1,
        updated_at=2,
        backend=Backend.CLAUDE,
        thread_id="same-authentic-native-session",
    )


def test_existing_index_upgrade_retains_rows_and_active_attempt_exclusivity(
    registry: Registry,
) -> None:
    registry.insert_run(_native_origin("old-origin", RunPhase.CLOSED, registry))
    old_index = (
        "CREATE UNIQUE INDEX runs_legacy_thread_key ON runs(thread_id) "
        "WHERE backend NOT IN ('codex', 'agy')"
    )
    with registry._transaction():
        sql(registry, "DROP INDEX runs_legacy_thread_key")
        sql(registry, old_index)
    assert registry.upgrade() == REGISTRY_SCHEMA_VERSION == 6
    assert registry.upgrade() == 6
    assert registry.get_run("old-origin") is not None
    registry.insert_run(_native_origin("next-attempt", RunPhase.PENDING, registry))
    with pytest.raises(nott_database.IntegrityError, match="UNIQUE"):
        registry.insert_run(_native_origin("overlap", RunPhase.PENDING, registry))
    assert len(registry.list_runs()) == 2
    assert nott_database.user_version(registry.connection) == 6


def test_restoring_old_global_index_is_atomic_after_native_resume_history(
    registry: Registry,
) -> None:
    registry.insert_run(_native_origin("old-origin", RunPhase.CLOSED, registry))
    registry.insert_run(_native_origin("closed-resume", RunPhase.CLOSED, registry))
    with (
        pytest.raises(nott_database.IntegrityError, match="UNIQUE"),
        registry._transaction(),
    ):
        sql(registry, "DROP INDEX runs_legacy_thread_key")
        sql(
            registry,
            "CREATE UNIQUE INDEX runs_legacy_thread_key ON runs(thread_id) "
            "WHERE backend NOT IN ('codex', 'agy')",
        )
    [index] = sql(
        registry, "SELECT sql FROM sqlite_schema WHERE name = 'runs_legacy_thread_key'"
    )
    assert index["sql"].split() == LEGACY_THREAD_INDEX_DDL.split()
    assert len(registry.list_runs()) == 2
    assert nott_database.user_version(registry.connection) == 6


def test_unverified_closed_legacy_origin_still_refuses_resume(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
) -> None:
    registry.insert_run(
        _native_origin("unverified-old-origin", RunPhase.CLOSED, registry)
    )
    owner = _sv(registry, tmp_path)
    with pytest.raises(DuplicateSession, match="unverified legacy Claude origin"):
        owner.execute(
            "Attempt an unrelated legacy resume.",
            "/private/inference",
            session_id="same-authentic-native-session",
            read_only=True,
        )
    assert len(registry.list_runs()) == 1
