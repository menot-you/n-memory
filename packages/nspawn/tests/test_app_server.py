"""Drive the native protocol through real pipes, including hostile ordering."""

import json
import os
import queue
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from fixture_data import WIRE_DIGESTS, wire_fixture
from host_process import process_state
from test_structured_inference import (
    PROFILE,
    codex_inference_peer,
    codex_result,
    native_client,
    start_codex_inference,
)

from nspawn import app_server
from nspawn.app_server import AppServerError, AppServerResult, AppServerTurn
from nspawn.child import ChildIdentityError
from nspawn.pump import PumpLimitExceeded
from nspawn.redact import REDACTION_MARKER, SecretNeedles

__all__ = ["native_client"]

SECRET = "native-test-credential-0123456789abcdef"
SERVER = r"""
import json, sys, time
mode, request_path = sys.argv[1:]
def send(value):
    print(json.dumps(value), flush=True)
def read():
    line = sys.stdin.readline()
    if not line:
        raise SystemExit(0)
    value = json.loads(line)
    with open(request_path, 'a') as log:
        log.write(json.dumps(value) + '\n')
    return value
def chatter():
    # Rate-limit chatter before a response; 30 per call, under any one-call cap.
    if mode == 'chatty-handshake':
        for _ in range(30):
            send({'method': 'account/rateLimits/updated', 'params': {'rateLimits': {}}})
if mode == 'unresponsive-start':
    time.sleep(30)
init = read()
assert init['method'] == 'initialize'
chatter()
if mode == 'credential-echo':
    # Redaction shrinks each line before the run budget charges it; the raw
    # frames held in memory do not shrink.
    for _ in range(100):
        send({'method': 'account/rateLimits/updated',
              'params': {'echo': 'native-test-credential-0123456789abcdef' * 100}})
send({'id': init['id'], 'result': {'userAgent': 'codex-test/1.0'}})
assert read()['method'] == 'initialized'
start = read()
assert start['method'] in ('thread/start', 'thread/resume')
assert start['params']['model'] == 'test-model'
assert start['params']['sandbox'] in ('read-only', 'workspace-write')
assert start['params']['approvalPolicy'] == 'never'
if mode == 'rpc-error':
    send({'id': start['id'], 'error': {'code': -32000, 'message': 'refused'}})
    raise SystemExit(0)
if mode == 'rpc-error-text-code':
    send({'id': start['id'], 'error': {'code': 'VENDOR-MARKER /home/x', 'message': 'r'}})
    raise SystemExit(0)
chatter()
if mode == 'slow-thread':
    time.sleep(1.0)
send({'id': start['id'], 'result': {'thread': {'id': 'thread-native'},
      'model': 'other-model' if mode == 'reported-model' else 'test-model',
      'modelProvider': 'other-provider' if mode == 'reported-provider' else 'test-provider',
      'sandbox': {'type': 'dangerFullAccess' if mode == 'reported-sandbox' else ('readOnly' if start['params']['sandbox'] == 'read-only' else 'workspaceWrite')}}})
turn = read()
assert turn['method'] == 'turn/start'
assert turn['params']['input'][0]['text'] == 'ping'
chatter()
if mode == 'slow-turn':
    time.sleep(1.0)
def capped_command():
    # Codex retains at most 1 MiB of a command's output, and JSON writes a
    # control byte as six: the largest item a capped command can complete with.
    send({'method': 'item/completed', 'params': {'threadId': 'thread-native',
          'turnId': 'turn-native', 'item': {'id': 'cmd', 'type': 'commandExecution',
          'aggregatedOutput': '\x01' * (1024 * 1024)}}})
if mode == 'capped-command-early':
    capped_command()
if mode != 'early-completion':
    send({'id': turn['id'], 'result': {'turn': {'id': 'turn-native'}}})
def delta(index):
    return json.dumps({'method': 'item/commandExecution/outputDelta', 'params': {
        'threadId': 'thread-native', 'turnId': 'turn-native', 'itemId': 'cmd',
        'delta': f'{index}:' + 'x' * 1000}})
if mode == 'flood':
    sys.stdout.write(''.join(delta(index) + '\n' for index in range(3000)))
    sys.stdout.flush()
if mode == 'flood-forever':
    index = 0
    while True:
        print(delta(index), flush=True)
        index += 1
if mode == 'timeout':
    time.sleep(30)
if mode == 'malformed':
    print('[]', flush=True)
    raise SystemExit(0)
if mode == 'line-limit':
    print('x' * (8 * 1024 * 1024 + 1), flush=True)
    raise SystemExit(0)
if mode == 'capped-command':
    capped_command()
if mode == 'server-request':
    send({'id': 'request-approval', 'method': 'item/commandExecution/requestApproval',
          'params': {'threadId': 'thread-native', 'turnId': 'turn-native'}})
    denied = read()
    assert denied['id'] == 'request-approval' and denied['error']['code'] == -32601
send({'method': 'turn/completed', 'params': {'threadId': 'other-thread',
      'turn': {'id': 'other-turn', 'status': 'completed'}}})
if mode in ('wrong-thread', 'wrong-turn'):
    send({'method': 'turn/completed', 'params': {
          'threadId': 'other-thread' if mode == 'wrong-thread' else 'thread-native',
          'turn': {'id': 'other-turn' if mode == 'wrong-turn' else 'turn-native',
                   'status': 'completed'}}})
if mode in ('foreign-only', 'wrong-thread', 'wrong-turn'):
    raise SystemExit(0)
send({'method': 'item/completed', 'params': {'threadId': 'thread-native',
      'turnId': 'turn-native', 'item': {'id': 'comment', 'type': 'agentMessage',
      'text': 'internal commentary', 'phase': 'commentary'}}})
text = 'PONG' if mode != 'redaction' else 'PONG native-test-credential-0123456789abcdef'
item = {'id': 'final', 'type': 'agentMessage', 'text': text, 'phase': 'final_answer'}
message = {'method': 'item/completed', 'params': {'threadId': 'thread-native',
           'turnId': 'turn-native', 'item': item}}
encoded = json.dumps(message)
if mode == 'redaction':
    encoded = encoded.replace('native-test-credential', '\\u006eative-test-credential')
print(encoded, flush=True)
send({'method': 'thread/tokenUsage/updated', 'params': {'threadId': 'thread-native',
      'turnId': 'turn-native', 'tokenUsage': {'total': {'totalTokens': 9,
      'inputTokens': 6, 'outputTokens': 3, 'cachedInputTokens': 0,
      'reasoningOutputTokens': 0}}}})
send({'method': 'turn/completed', 'params': {'threadId': 'thread-native',
      'turn': {'id': 'turn-native', 'status': 'failed' if mode == 'failed' else 'completed',
               'error': 42 if mode == 'malformed-error' else ({'message': 'provider refused'} if mode in ('failed', 'completed-error') else None)}}})
if mode == 'early-completion':
    send({'id': turn['id'], 'result': {'turn': {'id': 'turn-native'}}})
if mode == 'orphan-pipes':
    import os
    pid = os.fork()
    if pid == 0:
        time.sleep(30)
    else:
        with open(request_path + '.descendant', 'w') as handle:
            handle.write(str(pid))
        raise SystemExit(0)
if mode == 'exit-error':
    raise SystemExit(7)
if mode == 'linger':
    time.sleep(30)
for line in sys.stdin:
    pass
"""


def launch(tmp_path: Path, mode: str) -> AppServerTurn:
    script = tmp_path / "server.py"
    script.write_text(SERVER)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    return AppServerTurn.start(
        [sys.executable, "-u", str(script), mode, str(tmp_path / "requests")],
        cwd=tmp_path,
        env={"PATH": os.environ["PATH"]},
        run_dir=run_dir,
        needles=SecretNeedles.from_credential(SECRET.encode()),
    )


@pytest.mark.parametrize("native_client", ["codex"], indirect=True)
def test_structured_schema_and_actual_answer_cross_the_native_pipe(
    native_client: Any,
) -> None:
    client, cwd = native_client
    codex_inference_peer(cwd)
    run_id = start_codex_inference(client, cwd)
    status = client.terminal(run_id)
    assert status["phase"] == "completed", status
    result = codex_result(client, cwd, run_id)
    requests = [
        json.loads(line)
        for line in (cwd / "inference-requests.jsonl").read_text().splitlines()
    ]
    actual = next(
        frame["params"] for frame in requests if frame.get("method") == "turn/start"
    )
    assert actual["outputSchema"] == PROFILE["output_schema"]
    assert actual["threadId"] == status["thread_id"] == "native-thread"
    responses = [
        json.loads(line)
        for line in (cwd / "runs" / run_id / "stdout").read_text().splitlines()
    ]
    [completion] = [
        frame for frame in responses if frame.get("method") == "turn/completed"
    ]
    assert completion["params"]["turn"]["id"] == "native-turn"
    assert result["final_text"] == '{"decision":"holds"}'
    assert result["structured_output"] == {"decision": "holds"}


@pytest.mark.parametrize(
    "mode", ["structured-malformed", "structured-schema", "structured-nonfinite"]
)
@pytest.mark.parametrize("native_client", ["codex"], indirect=True)
def test_structured_native_answer_is_validated_before_success(
    native_client: Any, mode: str
) -> None:
    client, cwd = native_client
    codex_inference_peer(cwd, mode)
    run_id = start_codex_inference(client, cwd)
    status = client.terminal(run_id)
    assert status["phase"] == "failed", status
    result = codex_result(client, cwd, run_id)
    assert result["failure_reason"] == (
        "AppServerError (nERR-2003): "
        "structured output does not match its finite JSON schema"
    )
    assert "structured_output" not in result


@pytest.mark.parametrize("native_client", ["codex"], indirect=True)
def test_structured_native_answer_keeps_the_existing_full_result_bound(
    native_client: Any,
) -> None:
    client, cwd = native_client
    codex_inference_peer(cwd, "whole-answer")
    run_id = start_codex_inference(client, cwd)
    status = client.terminal(run_id)
    assert status["phase"] == "completed", status
    assert codex_result(client, cwd, run_id)["structured_output"] == {
        "decision": "x" * 70000
    }


@pytest.mark.parametrize("mode", ["normal", "early-completion", "server-request"])
@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize("read_only", [True])
def test_native_turn_completes_and_reaps_its_process(tmp_path, mode, resume, read_only):
    with launch(tmp_path, mode) as session:
        result = session.run(
            "ping",
            model="test-model",
            read_only=read_only,
            timeout=3,
            thread_id="thread-native" if resume else None,
            developer_instructions="owner law appended",
            model_provider="test-provider",
        )
    assert session.child.poll() is not None
    assert (result.thread_id, result.turn_id, result.status, result.final_text) == (
        "thread-native",
        "turn-native",
        "completed",
        "PONG",
    )
    assert result.usage["totalTokens"] == 9
    requests = [
        json.loads(line) for line in (tmp_path / "requests").read_text().splitlines()
    ]
    start = requests[2]
    assert start["method"] == ("thread/resume" if resume else "thread/start")
    assert start["params"]["developerInstructions"] == "owner law appended"
    assert "baseInstructions" not in start["params"]
    assert start["params"]["modelProvider"] == "test-provider"
    assert start["params"]["model"] == "test-model"
    assert start["params"]["sandbox"] == (
        "read-only" if read_only else "workspace-write"
    )
    # A read-only turn carries no workspace-write settings, network or otherwise.
    assert not any(
        key.startswith("sandbox_workspace_write.") for key in start["params"]["config"]
    )


@pytest.mark.parametrize("mode", ["rpc-error", "foreign-only", "malformed"])
def test_no_success_without_a_valid_matching_completion(tmp_path, mode):
    with launch(tmp_path, mode) as session, pytest.raises(AppServerError):
        session.run("ping", model="test-model", read_only=True, timeout=3)
    assert session.child.poll() is not None


def test_a_refusal_code_that_is_not_an_integer_never_reaches_the_detail(tmp_path):
    """The detail is stored as the failure reason; a vendor text code is not ours."""
    with (
        launch(tmp_path, "rpc-error-text-code") as session,
        pytest.raises(app_server.AppServerRefused) as caught,
    ):
        session.run("ping", model="test-model", read_only=True, timeout=3)
    assert "VENDOR-MARKER" not in caught.value.detail
    assert "VENDOR-MARKER" not in str(caught.value)
    assert caught.value.detail.startswith("native thread/start request refused")


def test_notifications_stashed_across_handshake_calls_are_not_refused(tmp_path):
    with launch(tmp_path, "chatty-handshake") as session:
        result = session.run("ping", model="test-model", read_only=True, timeout=10)
    assert (result.status, result.final_text) == ("completed", "PONG")


def test_the_handshake_stash_is_bounded_by_raw_bytes(tmp_path, monkeypatch):
    cap = 64 * 1024
    monkeypatch.setattr(app_server, "MAX_PENDING_BYTES", cap, raising=False)
    session = launch(tmp_path, "credential-echo")
    with pytest.raises(PumpLimitExceeded, match=f"{cap} byte"), session:
        session.run("ping", model="test-model", read_only=True, timeout=10)
    assert session._pending_bytes <= cap
    assert 0 < len(session._pending) < 100


def test_failed_native_turn_is_not_reported_completed(tmp_path):
    with launch(tmp_path, "failed") as session:
        result = session.run("ping", model="test-model", read_only=True, timeout=3)
    assert result.status == "failed"
    assert result.error == "provider refused"


def test_deadline_kills_and_reaps_the_owned_process(tmp_path):
    session = launch(tmp_path, "timeout")
    with pytest.raises(AppServerError, match="deadline"), session:
        session.run("ping", model="test-model", read_only=True, timeout=0.3)
    assert session.child.poll() is not None


@pytest.mark.parametrize("timeout", [10, None])
@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize("mode", ["slow-thread", "slow-turn"])
def test_thread_and_turn_open_wait_on_the_run_deadline_not_the_rpc_cap(
    tmp_path, monkeypatch, mode, resume, timeout
):
    """A max-effort review opens its thread and turn slowly: the run's own
    deadline bounds that wait, never the fixed per-request cap."""
    monkeypatch.setattr(app_server, "RPC_TIMEOUT_SECONDS", 0.3)
    with launch(tmp_path, mode) as session:
        result = session.run(
            "ping",
            model="test-model",
            read_only=True,
            timeout=timeout,
            thread_id="thread-native" if resume else None,
        )
    assert (result.status, result.final_text) == ("completed", "PONG")


def test_a_startup_that_never_answers_is_named_a_startup_timeout(tmp_path, monkeypatch):
    """The fixed cap expiring is not the run's deadline, and is not called one."""
    monkeypatch.setattr(app_server, "RPC_TIMEOUT_SECONDS", 0.3)
    session = launch(tmp_path, "unresponsive-start")
    with pytest.raises(app_server.AppServerStartupTimeout) as caught, session:
        session.run("ping", model="test-model", read_only=True, timeout=10)
    assert not isinstance(caught.value, app_server.AppServerDeadlineExceeded)


def test_output_ceiling_reaches_the_driver(tmp_path):
    session = launch(tmp_path, "line-limit")
    with pytest.raises(PumpLimitExceeded), session:
        session.run("ping", model="test-model", read_only=True, timeout=3)
    assert session.child.poll() is not None


@pytest.mark.parametrize("mode", ["capped-command", "capped-command-early"])
def test_a_command_at_the_vendor_output_cap_does_not_kill_the_run(tmp_path, mode):
    """Run 2fa0bb0c died on such a line: 1 MiB of `rg` output, escaped, is over 1 MiB.
    Early, it arrives before the `turn/start` response and waits in the stash."""
    with launch(tmp_path, mode) as session:
        result = session.run("ping", model="test-model", read_only=True, timeout=10)
    assert (result.status, result.final_text) == ("completed", "PONG")
    persisted = (tmp_path / "run" / "stdout").read_bytes().splitlines()
    assert max(map(len, persisted)) > 6 * 1024 * 1024


OUTPUT_DELTA = "item/commandExecution/outputDelta"


class _OverflowGauge(queue.Queue):
    """The real bounded queue, recording the first put it had no room for."""

    def __init__(self, maxsize: int) -> None:
        super().__init__(maxsize)
        self.refused = threading.Event()
        self.consumed: list[int] = []

    def put(self, item, block=True, timeout=None):
        try:
            super().put(item, block, timeout)
        except queue.Full:
            self.refused.set()
            raise


def _hold_consumer_until_overflow(session, then=None):
    """Stall the consumer once, on the first output delta, until the queue is overrun.

    A bursting reader outpaces the consumer only sometimes; holding the consumer
    until a put has found the queue full makes that race happen every time. The
    hold and `then` run exactly once, whether or not the overrun came first.
    """
    gauge = _OverflowGauge(app_server.MAX_PENDING_FRAMES)
    session._frames = gauge
    receive = session._receive

    def held(deadline):
        frame, size = receive(deadline)
        if frame.method == OUTPUT_DELTA:
            gauge.consumed.append(int(frame.params["delta"].split(":")[0]))
            if len(gauge.consumed) == 1:
                assert gauge.refused.wait(10)
                if then is not None:
                    then()
        return frame, size

    session._receive = held
    return gauge


def test_an_output_burst_waits_for_the_consumer_instead_of_failing(tmp_path):
    session = launch(tmp_path, "flood")
    gauge = _hold_consumer_until_overflow(session)
    with session:
        result = session.run("ping", model="test-model", read_only=True, timeout=30)
    assert (result.status, result.final_text) == ("completed", "PONG")
    # Lossless where it matters: the consumer saw every frame once, in order.
    assert gauge.consumed == list(range(3000))


class _Abandoned(Exception):
    pass


@pytest.mark.parametrize("overrun_first", [False, True])
def test_close_while_the_server_floods_releases_a_held_reader(tmp_path, overrun_first):
    session = launch(tmp_path, "flood-forever")

    def abandon():
        raise _Abandoned

    gauge = _hold_consumer_until_overflow(session, then=abandon)
    if overrun_first:
        # The interleaving where the overrun is on record before the first delta
        # reaches the consumer: abandonment must still happen exactly once.
        gauge.refused.set()
    try:
        with pytest.raises(_Abandoned):
            session.run("ping", model="test-model", read_only=True, timeout=30)
    finally:
        started = time.monotonic()
        session.close()
        elapsed = time.monotonic() - started
    assert elapsed < AppServerTurn.EXIT_GRACE_S
    assert session.child.poll() is not None
    assert session.child.stdout_pump.finished


def test_escaped_credentials_are_redacted_in_result_and_persisted_frames(tmp_path):
    with launch(tmp_path, "redaction") as session:
        result = session.run("ping", model="test-model", read_only=True, timeout=3)
    assert result.final_text == f"PONG {REDACTION_MARKER}"
    frames = [
        json.loads(line) for line in (tmp_path / "run/stdout").read_text().splitlines()
    ]
    assert SECRET not in json.dumps(frames)
    assert REDACTION_MARKER in json.dumps(frames)


@pytest.mark.parametrize("mode", ["wrong-thread", "wrong-turn"])
def test_each_completion_correlation_axis_is_required(tmp_path, mode):
    with (
        pytest.raises(AppServerError, match="matching completion"),
        launch(tmp_path, mode) as session,
    ):
        session.run("ping", model="test-model", read_only=True, timeout=3)
    assert session.child.poll() is not None


@pytest.mark.parametrize("mode", ["completed-error", "exit-error", "malformed-error"])
def test_completion_never_hides_a_terminal_error(tmp_path, mode):
    with pytest.raises(AppServerError), launch(tmp_path, mode) as session:
        session.run("ping", model="test-model", read_only=True, timeout=3)
    assert session.child.poll() is not None


@pytest.mark.parametrize(
    "field,value",
    [
        ("thread_id", ""),
        ("thread_id", "x" * 1025),
        ("turn_id", ""),
        ("status", "running"),
        ("final_text", 1),
        ("usage", None),
        ("usage", {1: 0}),
        ("usage", {"totalTokens": -1}),
        ("usage", {"totalTokens": True}),
        ("usage", {"totalTokens": "1"}),
        ("error", "contradicts completed"),
        ("failure_reason", "VendorApiError (HTTP 400)"),
    ],
)
def test_result_constructor_refuses_invalid_state(field, value):
    fields = {
        "thread_id": "thread",
        "turn_id": "turn",
        "status": "completed",
        "final_text": "PONG",
        "usage": {},
    }
    fields[field] = value
    with pytest.raises(AppServerError):
        AppServerResult(**fields)


def _body(status: object, kind: object) -> str:
    return json.dumps({"type": "error", "status": status, "error": {"type": kind}})


@pytest.mark.parametrize(
    "error, api_status, reason",
    [
        (
            {"message": _body(400, "invalid_request_error"), "codexErrorInfo": "other"},
            None,
            "VendorApiError (HTTP 400, invalid_request_error)",
        ),
        # A type that is prose is the vendor's words, and those stay out.
        (
            {
                "message": _body(400, "the model is not supported"),
                "codexErrorInfo": "other",
            },
            None,
            "VendorApiError (HTTP 400, other)",
        ),
        (
            {"message": _body(True, "x"), "codexErrorInfo": "other"},
            None,
            "VendorTurnError (x)",
        ),
        (
            {"message": "free text 400", "codexErrorInfo": "other"},
            None,
            "VendorTurnError (other)",
        ),
        (
            {"message": "free", "codexErrorInfo": {"httpConnectionFailed": {}}},
            502,
            "VendorApiError (HTTP 502, httpConnectionFailed)",
        ),
        (
            {"message": "free", "codexErrorInfo": "no prose please"},
            None,
            "NativeTurnFailed (failed)",
        ),
        (None, None, "NativeTurnFailed (failed)"),
    ],
)
def test_a_failed_turn_reason_quotes_only_vendor_identifiers(error, api_status, reason):
    assert app_server._failure_reason("failed", error, api_status) == reason


def test_close_reaps_a_leader_that_exits_during_stop(tmp_path, monkeypatch):
    session = launch(tmp_path, "normal")
    original = session.child.stop_then_kill

    def exit_before_signal(identity):
        session.child.close_stdin()
        assert session.child.wait(timeout=3) == 0
        original(identity)

    monkeypatch.setattr(session.child, "stop_then_kill", exit_before_signal)
    session.close()
    assert session.child.poll() == 0
    assert session.child.stdout_pump.finished
    assert session.child.stderr_pump.finished
    assert session.terminated_by_driver is False
    session.close()


@pytest.mark.parametrize(
    "mode", ["reported-model", "reported-provider", "reported-sandbox"]
)
@pytest.mark.parametrize("resume", [False, True])
def test_start_and_resume_refuse_changed_requested_configuration(
    tmp_path, mode, resume
):
    with pytest.raises(AppServerError), launch(tmp_path, mode) as session:
        session.run(
            "ping",
            model="test-model",
            model_provider="test-provider",
            read_only=True,
            timeout=3,
            thread_id="thread-native" if resume else None,
        )
    assert session.child.poll() is not None


def test_completed_server_that_ignores_eof_is_killed_and_reaped(tmp_path, monkeypatch):
    # The server ignores EOF, so the whole grace is paid; its length is not the claim.
    monkeypatch.setattr(AppServerTurn, "EXIT_GRACE_S", 0.3)
    with launch(tmp_path, "linger") as session:
        result = session.run("ping", model="test-model", read_only=True, timeout=3)
    assert result.status == "completed"
    assert session.terminated_by_driver is True
    assert session.child.poll() == -9
    assert session.child.stdout_pump.finished and session.child.stderr_pump.finished


def test_cleanup_refusal_does_not_mask_the_original_failure(tmp_path, monkeypatch):
    session = launch(tmp_path, "unresponsive-start")
    original = session.child.stop_then_kill

    def refuse(identity):
        raise ChildIdentityError("test identity refusal")

    failure = AppServerError("original failure")
    monkeypatch.setattr(session.child, "stop_then_kill", refuse)
    try:
        session.__exit__(AppServerError, failure, None)
        assert failure.__notes__ == ["native cleanup also failed: ChildIdentityError"]
        assert session.child.poll() is None
        with pytest.raises(ChildIdentityError):
            session.close()
    finally:
        monkeypatch.setattr(session.child, "stop_then_kill", original)
        session.close()
    assert session.child.poll() is not None


REPLAY = r"""
import json, sys
path, account, stage, cwd = sys.argv[1:]
for line in open(path):
    row = json.loads(line)
    if (row['account'], row['stage']) != (account, stage):
        continue
    if row['direction'] == 'sent':
        actual = json.loads(sys.stdin.readline().replace(cwd, '<CAPTURE_CWD>'))
        # Recorded while a read-only turn still sent the dormant workspace-write
        # settings (network included); the driver no longer sends them.
        config = row['frame'].get('params', {}).get('config', {})
        for key in [key for key in config if key.startswith('sandbox_workspace_write.')]:
            del config[key]
        assert actual == row['frame'], (actual, row['frame'])
    else:
        print(json.dumps(row['frame']), flush=True)
"""


@pytest.mark.parametrize("account", ["account-1", "account-2"])
@pytest.mark.parametrize("stage", ["start", "resume"])
def test_real_versioned_fixture_replays_through_the_driver(tmp_path, account, stage):
    name = "codex-appserver-wire-0.153.4.ndjson"
    fixture = wire_fixture(name)
    metadata = json.loads(
        (fixture.parent / name.replace(".ndjson", ".metadata.json")).read_text()
    )
    assert metadata["fixture_sha256"] == WIRE_DIGESTS[name][0]
    rows = [json.loads(line) for line in fixture.read_text().splitlines()]
    rows = [row for row in rows if (row["account"], row["stage"]) == (account, stage)]
    sent = [row["frame"] for row in rows if row["direction"] == "sent"]
    incoming = [row["frame"] for row in rows if row["direction"] == "received"]
    assert f"/{metadata['version']} " in incoming[0]["result"]["userAgent"]
    assert (
        incoming[0]["result"]["codexHome"] == metadata["accounts"][int(account[-1]) - 1]
    )
    expected = next(
        row
        for row in metadata["outcomes"]
        if (row["account"], row["stage"]) == (account, stage)
    )
    output = tmp_path / "run"
    output.mkdir()
    with AppServerTurn.start(
        [
            sys.executable,
            "-u",
            "-c",
            REPLAY,
            str(fixture),
            account,
            stage,
            str(tmp_path),
        ],
        cwd=tmp_path,
        env={"PATH": os.environ["PATH"]},
        run_dir=output,
        needles=SecretNeedles.none(),
    ) as session:
        result = session.run(
            sent[3]["params"]["input"][0]["text"],
            model=metadata["model"],
            read_only=True,
            timeout=3,
            effort="low",
            developer_instructions=sent[2]["params"]["developerInstructions"],
            thread_id=sent[2]["params"].get("threadId"),
        )
    assert result.thread_id == expected["thread_id"]
    assert result.turn_id == expected["turn_id"]
    assert result.final_text == expected["expected"]
    assert result.status == "completed"
    assert result.usage == {**expected["usage"], "cacheWriteInputTokens": 0}
    assert session.child.poll() == 0


def test_completed_leader_reaps_group_and_closes_descendant_pipes(tmp_path):
    session = launch(tmp_path, "orphan-pipes")
    completed = False
    try:
        result = session.run("ping", model="test-model", read_only=True, timeout=3)
        assert result.status == "completed"
        assert session.child.poll() == 0
        assert session.child.stdout_pump.finished and session.child.stderr_pump.finished
        pid = int((tmp_path / "requests.descendant").read_text())
        # SIGKILL is delivered asynchronously: a killed task can still read as
        # running until the scheduler runs its exit, observed under host load
        # after `run` returned (2026-09-24). Wait out that exit on a deadline; a
        # descendant the group kill missed never becomes a zombie or leaves.
        deadline = time.monotonic() + 3
        while (state := process_state(pid)) is not None and state[0] not in ("Z", "X"):
            assert time.monotonic() < deadline, (
                f"descendant must be exiting, zombie or dead, never running: state {state}"
            )
            time.sleep(0.005)
        completed = True
    finally:
        descendant = tmp_path / "requests.descendant"
        if not completed and descendant.exists():
            try:
                os.kill(int(descendant.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass
        session.close()
    assert session.child.stdout_pump.finished and session.child.stderr_pump.finished


def test_max_request_bytes_constant():
    from nspawn.app_server import MAX_REQUEST_BYTES
    from nspawn.pump import MAX_OUTPUT_LINE_BYTES

    assert MAX_REQUEST_BYTES == MAX_OUTPUT_LINE_BYTES + 16384


def test_read_quota_home_identity_and_mismatch(tmp_path):
    server_script = r"""
import json, sys
def send(v): print(json.dumps(v), flush=True)
def read(): return json.loads(sys.stdin.readline())
init = read()
assert init['method'] == 'initialize'
send({'id': init['id'], 'result': {'codexHome': str(sys.argv[1])}})
assert read()['method'] == 'initialized'
req = read()
assert req['method'] == 'account/rateLimits/read'
send({'id': req['id'], 'result': {'accountId': 'acc-1', 'rateLimits': {'primary': {'usedPercent': 10, 'resetsAt': 12345}}}})
"""
    output = tmp_path / "run_quota"
    output.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    with AppServerTurn.start(
        [sys.executable, "-u", "-c", server_script, str(home)],
        cwd=tmp_path,
        env={"PATH": os.environ["PATH"]},
        run_dir=output,
        needles=SecretNeedles.none(),
    ) as session:
        reading = session.read_account_quota(home, "acc-1", timeout=3)
        assert reading.absence is None

    output2 = tmp_path / "run_quota_mismatch"
    output2.mkdir()
    with (
        AppServerTurn.start(
            [sys.executable, "-u", "-c", server_script, str(tmp_path / "wrong")],
            cwd=tmp_path,
            env={"PATH": os.environ["PATH"]},
            run_dir=output2,
            needles=SecretNeedles.none(),
        ) as session,
        pytest.raises(AppServerError, match="native quota home identity mismatch"),
    ):
        session.read_account_quota(home, "acc-1", timeout=3)


def test_turn_rate_limits_updated_openai(tmp_path):
    server_script = r"""
import json, sys
def send(v): print(json.dumps(v), flush=True)
def read(): return json.loads(sys.stdin.readline())
init = read()
send({'id': init['id'], 'result': {'userAgent': 'test/1.0'}})
assert read()['method'] == 'initialized'
start = read()
send({'id': start['id'], 'result': {'thread': {'id': 'thread-native'}, 'model': 'test-model', 'modelProvider': 'openai', 'sandbox': {'type': 'readOnly'}}})
turn = read()
assert turn['method'] == 'turn/start'
send({'id': turn['id'], 'result': {'turn': {'id': 'turn-native'}}})
send({'method': 'turn/started', 'params': {'threadId': 'thread-native', 'turnId': 'turn-native'}})
send({'method': 'account/rateLimits/updated', 'params': {'rateLimits': {'primary': {'usedPercent': 25, 'resetsAt': 99999}}}})
send({'method': 'item/completed', 'params': {'threadId': 'thread-native', 'turnId': 'turn-native', 'item': {'type': 'agentMessage', 'id': 'item-1', 'text': 'ok'}}})
send({'method': 'turn/completed', 'params': {'threadId': 'thread-native', 'turnId': 'turn-native', 'turn': {'id': 'turn-native', 'status': 'completed'}}})
"""
    output = tmp_path / "run_ratelimits"
    output.mkdir()
    with AppServerTurn.start(
        [sys.executable, "-u", "-c", server_script],
        cwd=tmp_path,
        env={"PATH": os.environ["PATH"]},
        run_dir=output,
        needles=SecretNeedles.none(),
    ) as session:
        result = session.run(
            "ping",
            model="test-model",
            read_only=True,
            timeout=3,
            model_provider="openai",
        )
        assert result.quota is not None
        assert result.quota.source == "codex-native"


def test_readonly_plugin_detail_malformed_raises(tmp_path):
    server_script = r"""
import json, sys
def send(v): print(json.dumps(v), flush=True)
def read(): return json.loads(sys.stdin.readline())
init = read()
send({'id': init['id'], 'result': {'userAgent': 'test/1.0'}})
assert read()['method'] == 'initialized'
req1 = read()
assert req1['method'] == 'plugin/installed'
send({'id': req1['id'], 'result': {'marketplaces': [{'name': 'm1', 'plugins': [{'id': 'p1', 'name': 'plug1', 'enabled': True}]}]}})
req2 = read()
assert req2['method'] == 'plugin/read'
send({'id': req2['id'], 'result': {'plugin': 'not-a-dict'}})
"""
    output = tmp_path / "run_plugin_malformed"
    output.mkdir()
    with (
        AppServerTurn.start(
            [sys.executable, "-u", "-c", server_script],
            cwd=tmp_path,
            env={"PATH": os.environ["PATH"]},
            run_dir=output,
            needles=SecretNeedles.none(),
        ) as session,
        pytest.raises(
            AppServerError, match="native read-only plugin detail is malformed"
        ),
    ):
        session.readonly_mcp_policies(timeout=3)


def test_writable_turn_without_accepted_controller_is_refused(tmp_path):
    # The body-accepted controller is retired: a writable turn needs native write.
    with (
        launch(tmp_path, "normal") as session,
        pytest.raises(AppServerError, match="needs native write"),
    ):
        session.run("ping", model="test-model", read_only=False, timeout=3)
    requests = tmp_path / "requests"
    if requests.exists():
        assert not any(
            json.loads(line).get("method") == "thread/start"
            for line in requests.read_text().splitlines()
        )
