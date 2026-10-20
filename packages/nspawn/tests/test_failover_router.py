"""nSPAWN is the router: an account that fails is its problem.

An account, balance, quota or rate-limit failure is a routing event for every
run class. nSPAWN walls what failed and continues the work on the next eligible
account or route. A writer moves only while its tree shows no write. The answer
of the run carries one substitution alert that names the move.

The supervisor, the roster, the picker, the walls and the registry are the real
ones. The vendor is a fake process: a codex app-server double, or a Claude child
whose stream is a recorded refusal.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from class_dispatch import DECLARED_CLASS, system1_never_reaches_the_network
from supervisor_support import (
    FakeSpawnedChild,
    _admit_pending,
    _drain_worker_threads,
    _spawn_real,
    _stop_every_reaper,
    _sv,
    _wire_l8_and_l3,
    registry,
    roster_home,
)
from test_driver import _call, _payload

from nspawn import app_server, native, picker, supervisor, terminal
from nspawn.mcp import McpServer
from nspawn.owner_roster import Defaults, NativeAccount, Provider, Roster
from nspawn.redact import SecretNeedles
from nspawn.registry import Registry
from nspawn.rows import NativeSessionContext, RunPhase

__all__ = [
    "_drain_worker_threads",
    "_stop_every_reaper",
    "registry",
    "roster_home",
    "system1_never_reaches_the_network",
]

#: A codex app-server double. With `FIXTURE_FAIL` its turn fails with that HTTP
#: status, as codex relays it. With `FIXTURE_WRITE` the turn first writes that
#: file in its cwd, the run's tree. Otherwise the turn answers DONE.
SERVER = r"""
import json, os, sys
from pathlib import Path
if sys.argv[-3:] == ['mcp', 'list', '--json']:
    print('[]', flush=True)
    raise SystemExit(0)
FAIL = os.environ.get('FIXTURE_FAIL')
WRITE = os.environ.get('FIXTURE_WRITE')
def send(value):
    print(json.dumps(value), flush=True)
for line in sys.stdin:
    frame = json.loads(line)
    method, params = frame.get('method'), frame.get('params') or {}
    if method == 'initialize':
        send({'id': frame['id'], 'result': {'userAgent': 'router-fixture'}})
    elif method == 'plugin/installed':
        send({'id': frame['id'], 'result': {'marketplaces': [], 'marketplaceLoadErrors': []}})
    elif method == 'thread/start':
        profiles = params.get('config', {}).get('permissions', {})
        profile = profiles.get(params.get('permissions'), {})
        writer = 'write' in profile.get('filesystem', {}).values()
        send({'id': frame['id'], 'result': {'thread': {'id': 'router-thread'},
              'model': params['model'],
              'sandbox': json.loads(os.environ['WRITER_SANDBOX']) if writer else {'type': 'readOnly'},
              'activePermissionProfile': {'id': params['permissions'], 'extends': None}
              if params.get('permissions') else None}})
    elif method == 'mcpServerStatus/list':
        send({'id': frame['id'], 'result': {'data': [], 'nextCursor': None}})
    elif method == 'turn/start':
        send({'id': frame['id'], 'result': {'turn': {'id': 'router-turn'}}})
        if WRITE:
            Path(WRITE).write_text('half of the work')
        if FAIL:
            send({'method': 'turn/completed', 'params': {'threadId': 'router-thread',
                  'turn': {'id': 'router-turn', 'status': 'failed', 'error': {
                      'message': 'fixture rate limit',
                      'codexErrorInfo': {'responseTooManyFailedAttempts': {
                          'httpStatusCode': int(FAIL)}}}}}})
            continue
        send({'method': 'item/completed', 'params': {'threadId': 'router-thread',
              'turnId': 'router-turn', 'item': {'id': 'answer', 'type': 'agentMessage',
              'phase': 'final_answer', 'text': 'DONE'}}})
        send({'method': 'turn/completed', 'params': {'threadId': 'router-thread',
              'turn': {'id': 'router-turn', 'status': 'completed', 'error': None}}})
"""


def _codex_route(
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    write_first: bool = False,
) -> Path:
    """A codex route with the accounts `first` and `second`; the account the
    picker takes first fails its turn with HTTP 429. Answers the checkout."""
    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)
    homes = {alias: tmp_path / alias for alias in ("first", "second")}
    for home in homes.values():
        home.mkdir()
    (roster_home / ".nott" / "spawn.toml").write_text(
        '[defaults]\nplugins = ["/tmp/plugin-root"]\n'
        '[bins]\ncodex = "/usr/bin/true"\n'
        '[[provider]]\nname = "fixture"\nkind = "codex"\n'
        'model = "fixture"\nmodels = ["fixture"]\n'
        f'accounts = {{ first = {{ home = "{homes["first"]}" }}, '
        f'second = {{ home = "{homes["second"]}" }} }}\n'
        f"[native_write]\nroots = [{json.dumps(str(tmp_path))}]\n",
        encoding="utf-8",
    )
    script = tmp_path / "router_server.py"
    script.write_text(SERVER, encoding="utf-8")
    failing: list[str] = []

    def prepare(_provider: object, account: str, **_: object) -> native.NativeLaunch:
        if not failing:
            failing.append(account)
        env = {
            "PATH": "/usr/bin:/bin",
            "WRITER_SANDBOX": json.dumps(app_server.NATIVE_WRITE_SANDBOX),
        }
        if account in failing:
            env["FIXTURE_FAIL"] = "429"
            if write_first:
                env["FIXTURE_WRITE"] = "partial.txt"
        return native.NativeLaunch(
            (sys.executable, "-u", str(script), "app-server", "--listen", "stdio://"),
            env,
            SecretNeedles.none(),
            NativeSessionContext(
                str(tmp_path), read_roots=(os.path.realpath(checkout),)
            ),
            NativeAccount(homes[account]),
        )

    monkeypatch.setattr(native, "prepare_native", prepare)
    return checkout


def _start(server: McpServer, checkout: Path, *, writer: bool) -> str:
    arguments: dict[str, Any] = {
        "prompt": "change it" if writer else "read it",
        "cwd": str(checkout),
        "class": DECLARED_CLASS,
    }
    arguments.update({"native_write": True} if writer else {"read_only": True})
    return str(_payload(_call(server, "nspawn_run_start", arguments))["run_id"])


def _result(server: McpServer, run_id: str) -> dict[str, Any]:
    answer = _payload(
        _call(server, "nspawn_result_show", {"run_id": run_id, "timeout_ms": 30_000})
    )
    assert isinstance(answer, dict)
    return answer


@pytest.mark.parametrize(
    "writer",
    [
        pytest.param(
            True, id="writer", marks=pytest.mark.needs("systemd_user", "cgroup")
        ),
        pytest.param(False, id="reader"),
    ],
)
def test_a_run_refused_with_429_before_any_write_continues_on_the_next_account(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    writer: bool,
) -> None:
    """A plain 429 walls the account, and the work goes on: the caller asks for
    the answer of its own run and gets the answer of the run that served."""
    checkout = _codex_route(tmp_path, roster_home, monkeypatch)
    sv = _sv(registry, tmp_path)
    try:
        server = McpServer(supervisor=sv)
        first = _start(server, checkout, writer=writer)
        answer = _result(server, first)
    finally:
        sv.shutdown()
    failed = registry.get_run(first)
    assert failed is not None and failed.phase is RunPhase.FAILED
    assert failed.executed_account == "first"
    assert (answer["phase"], answer["final_text"]) == ("completed", "DONE")
    assert answer["run_id"] != first
    served = registry.get_run(answer["run_id"])
    assert served is not None and served.executed_account == "second"
    alert = answer["alert"]
    assert alert is not None
    assert "fixture/first" in alert and "HTTP 429" in alert
    assert "fixture/second" in alert and "fixture" in alert
    now = int(time.time())
    assert registry.get_account_walls(now).get("first", 0) > now
    assert len(registry.list_runs()) == 2
    if writer:
        claim = registry.read_claim(os.path.realpath(checkout))
        assert claim is None or claim.run_id == served.run_id


@pytest.mark.needs("systemd_user", "cgroup")
def test_a_writer_that_already_wrote_does_not_move(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two writers never touch one tree: a writer that wrote stops and says why."""
    checkout = _codex_route(tmp_path, roster_home, monkeypatch, write_first=True)
    sv = _sv(registry, tmp_path)
    try:
        server = McpServer(supervisor=sv)
        first = _start(server, checkout, writer=True)
        answer = _result(server, first)
    finally:
        sv.shutdown()
    assert (checkout / "partial.txt").exists()
    assert answer["run_id"] == first and answer["phase"] == "failed"
    assert "alert" not in answer
    assert len(registry.list_runs()) == 1
    row = registry.get_run(first)
    assert row is not None and row.warning is not None
    assert "continued as run" not in row.warning
    assert "wrote to its tree" in row.warning
    now = int(time.time())
    assert registry.get_account_walls(now).get("first", 0) > now


FIXTURES = Path(__file__).parent / "fixtures"


def _answered(recorded: str) -> str:
    """The recorded stream as a turn that completed with the answer PONG."""
    frames = [json.loads(line) for line in recorded.splitlines() if line]
    result = frames[-1]
    result.update(
        is_error=False,
        subtype="success",
        terminal_reason="completed",
        api_error_status=None,
        result="PONG",
        stop_reason="end_turn",
    )
    return "".join(json.dumps(frame) + "\n" for frame in frames)


@pytest.mark.parametrize(
    ("fixture", "cause"),
    [
        ("claude-zai-1113-2.1.282.ndjson", "HTTP 429 code 1113"),
        ("claude-oauth-org-403-2.1.257.ndjson", "HTTP 403 oauth_org_not_allowed"),
    ],
    ids=["undeclared-spent-balance", "organization-refusal"],
)
def test_a_refused_account_is_walled_and_the_read_only_run_goes_on(
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fixture: str,
    cause: str,
) -> None:
    """Z.AI's `1113` on a route that declares no `exhausted_codes`, and an
    organization that refuses the OAuth account: the child stream is the
    recorded refusal, then a completed turn on the next account."""
    recorded = (FIXTURES / fixture).read_text(encoding="utf-8")
    secrets = tmp_path / "secrets" / "claude"
    secrets.mkdir(parents=True)
    for alias in ("acct-a", "acct-b"):
        (secrets / f"{alias}.token").write_text("x", encoding="utf-8")
    provider = Provider(name="zai", secrets=secrets, models=["glm"], model="glm")
    roster = Roster(providers=[provider], defaults=Defaults(plugin_root=tmp_path))
    streams = [(recorded, 1), (_answered(recorded), 0)]
    real = terminal.classify_terminal_run
    replays = tmp_path / "replays"
    replays.mkdir()

    def replay(*_: object, **named: Any) -> terminal.TerminalClassification:
        stdout = replays / f"{len(streams)}.stdout"
        recorded, exit_code = streams.pop(0)
        stdout.write_text(recorded, encoding="utf-8")
        stderr = replays / "stderr"
        stderr.write_text("", encoding="utf-8")
        return real(stdout, stderr, exit_code, **named)

    _wire_l8_and_l3(monkeypatch, lambda *a, **k: FakeSpawnedChild(_spawn_real("true")))
    monkeypatch.setattr(terminal, "classify_terminal_run", replay)
    sv = _sv(registry, tmp_path)
    run_id, thread_id = _admit_pending(sv, registry, tmp_path, worktree=str(tmp_path))
    run_dir = supervisor.RunDirectory(
        root=sv._run_root_path / run_id,
        stdout=sv._run_root_path / run_id / "stdout",
        stderr=sv._run_root_path / run_id / "stderr",
    )
    picked = picker.PickerResult(
        requested_provider="zai",
        executed_provider=provider,
        executed_account="acct-a",
        model="glm",
        effort=None,
    )
    try:
        sv._drive_run(
            run_id, str(tmp_path), run_dir, "ping", roster, picked, thread_id,
            False, True,
        )  # fmt: skip
        answer = sv.result(run_id)
    finally:
        sv.shutdown()
    row = registry.get_run(run_id)
    assert row is not None
    assert (row.phase, row.executed_account) == (RunPhase.COMPLETED, "acct-b")
    assert answer.final_text == "PONG"
    assert answer.alert is not None
    assert "zai/acct-a" in answer.alert and cause in answer.alert
    assert "/acct-b with model" in answer.alert
    now = int(time.time())
    walls = registry.get_account_walls(now)
    assert walls.get("acct-a", 0) > now
    assert "acct-b" not in walls


def test_a_writer_moves_only_from_a_claim_it_still_holds(
    registry: Registry, tmp_path: Path
) -> None:
    """A close that took the claim during the tree scan keeps the work where it
    is: two writers never touch one tree."""
    tree = tmp_path / "tree"
    tree.mkdir()
    sv = _sv(registry, tmp_path)
    run_id, _ = _admit_pending(sv, registry, tmp_path, worktree=str(tree))
    sv._write_baselines[run_id] = time.time_ns()
    row = registry.get_run(run_id)
    assert row is not None and registry.read_claim(str(tree)) is not None
    registry.release_claim(str(tree), run_id)
    assert sv._writer_stays(row, "HTTP 429") is not None
    registry.claim_worktree(str(tree), run_id, int(time.time() * 1000), 60_000)
    assert sv._writer_stays(row, "HTTP 429") is None
    assert registry.read_claim(str(tree)) is None
