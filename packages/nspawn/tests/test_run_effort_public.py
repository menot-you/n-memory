"""The effort profile, read back through the doors a consumer actually uses.

`test_run_effort.py` judges the record and the profile in process. This file
judges the same answer at the PUBLIC boundary: `nspawn run show` printing JSON
through its real native stdio peer, and a real `nspawn mcp serve` dispatching a
Codex route whose vendor is the fixture `test_native_execute.py` already drives.
Every store is this test's own disposable file under `NOTT_DATABASE_DIR`.

The vendor speaks the installed Codex 0.159.3 shape: after `turn/start` answers
it sends `turn/started` with `{threadId, turn}`, and that `Turn` carries no
effort. So `executed` stays unknown even with the frame on the wire, while
`sent` is exactly the value the vendor received on `turn/start`.
"""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from class_dispatch import DECLARED_CLASS
from registry_reset import store
from test_native_execute import VENDOR, Client

from nspawn import cli
from nspawn.registry import Registry
from nspawn.rows import CLAUDE_CONFINEMENT_POLICY_ONLY, Backend, RunPhase, RunRow

RECORDED = "admission"
UNKNOWN = "unknown"

UNKNOWN_PROFILE = {
    "requested": None,
    "requested_source": UNKNOWN,
    "sent": None,
    "sent_source": UNKNOWN,
    "executed": None,
    "executed_source": UNKNOWN,
}


def _recorded(requested: str | None, sent: str | None) -> dict[str, Any]:
    return {
        "requested": requested,
        "requested_source": RECORDED,
        "sent": sent,
        "sent_source": RECORDED,
        "executed": None,
        "executed_source": UNKNOWN,
    }


# ---------------------------------------------------------------------------
# `nspawn run show`: JSON through the native stdio peer
# ---------------------------------------------------------------------------

OWNER = "fixture-owner"


def _row(run_id: str, worktree: Path, phase: RunPhase) -> RunRow:
    return RunRow(
        run_id=run_id,
        thread_id=run_id,
        worktree=str(worktree),
        phase=phase,
        sandbox=CLAUDE_CONFINEMENT_POLICY_ONLY,
        model="fixture-model",
        owner_instance=OWNER,
        created_at=1,
        updated_at=2,
        backend=Backend.CLAUDE,
        executed_provider="fixture",
    )


@pytest.fixture
def run_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "runs"
    root.mkdir()
    monkeypatch.setenv("NOTT_NSPAWN_RUN_ROOT", str(root))
    return root


def _run_dir(root: Path, run_id: str) -> None:
    directory = root / run_id
    directory.mkdir()
    directory.chmod(0o700)


def _show(run_id: str, capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    assert cli.main(["run", "show", run_id]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["run_id"] == run_id
    return payload


@pytest.mark.parametrize(
    "requested,sent",
    [("ultra", "high"), (None, "high"), (None, None)],
    ids=["pinned", "default", "nothing"],
)
def test_run_show_prints_the_record_its_admission_wrote(
    run_root: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    requested: str | None,
    sent: str | None,
) -> None:
    run_id = "cli-recorded"
    _run_dir(run_root, run_id)
    row = _row(run_id, tmp_path, RunPhase.PENDING)
    with Registry(store(), OWNER) as registry:
        registry.open(1)
        registry.admit_run(
            row,
            False,
            1,
            60_000,
            effort_profile={"requested": requested, "sent": sent},
        )
        # Terminal, so no sweep in the peer has a live run to act on.
        registry.update_run(dataclasses.replace(row, phase=RunPhase.COMPLETED))
    # JSON null beside `admission`, never the string "None" and never "max".
    assert _show(run_id, capsys)["effort"] == _recorded(requested, sent)


def test_run_show_prints_unknown_for_a_run_without_a_record(
    run_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_id = "cli-legacy"
    _run_dir(run_root, run_id)
    with Registry(store(), OWNER) as registry:
        registry.open(1)
        registry.insert_run(_row(run_id, tmp_path, RunPhase.COMPLETED))
        assert _show(run_id, capsys)["effort"] == UNKNOWN_PROFILE
        # The read invented no record on its way out.
        assert not any(
            isinstance(event.payload, dict) and "effort_profile" in event.payload
            for event in registry.read_run_events(run_id)
        )


@pytest.mark.parametrize(
    "stored",
    [
        [{"requested": "high"}],
        [{"requested": "high", "sent": "high", "executed": "max"}],
        [{"requested": "high", "sent": "high"}, {"requested": "low", "sent": "low"}],
    ],
    ids=["malformed", "forged", "duplicated"],
)
def test_run_show_refuses_a_malformed_or_duplicated_record(
    run_root: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    stored: list[object],
) -> None:
    run_id = "cli-damaged"
    _run_dir(run_root, run_id)
    with Registry(store(), OWNER) as registry:
        registry.open(1)
        registry.insert_run(_row(run_id, tmp_path, RunPhase.COMPLETED))
        for record in stored:
            registry.append_run_event(
                run_id, at=2, kind="lifecycle", payload={"effort_profile": record}
            )
    assert cli.main(["run", "show", run_id]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "refused" in captured.err


# ---------------------------------------------------------------------------
# A real Codex dispatch through `nspawn mcp serve`
# ---------------------------------------------------------------------------

#: The one line of the shared vendor that answers `turn/start`.
_TURN_ANSWER = (
    "            send({'id': frame['id'], 'result': {'turn': {'id': 'native-turn'}}})\n"
)
#: The same answer, then what this file adds: the effort the vendor RECEIVED, and
#: the 0.159.3 `turn/started` notification, whose `Turn` has no effort.
_TURN_ANSWER_AND_STARTED = _TURN_ANSWER + (
    "            with open('turn-efforts.log', 'a') as received:\n"
    "                received.write(json.dumps(params.get('effort')) + '\\n')\n"
    "            send({'method': 'turn/started', 'params': {'threadId': thread,\n"
    "                  'turn': {'id': 'native-turn', 'items': [],\n"
    "                           'status': 'inProgress', 'error': None}}})\n"
)


def _vendor() -> str:
    assert VENDOR.count(_TURN_ANSWER) == 1, "the shared vendor fixture moved"
    return VENDOR.replace(_TURN_ANSWER, _TURN_ANSWER_AND_STARTED)


@pytest.fixture
def codex_door(tmp_path: Path) -> Iterator[tuple[Client, Path, dict[str, str]]]:
    """`nspawn mcp serve` over a Codex roster whose provider defaults to `low`
    and serves `low` and `high`, with one account."""
    store()
    binary = tmp_path / "vendor"
    binary.write_text(
        f"#!{sys.executable}\nKIND='codex'\nRESET_USAGE=False\n" + _vendor()
    )
    binary.chmod(0o700)
    home = tmp_path / "first"
    home.mkdir(mode=0o700)
    auth = home / "auth.json"
    auth.write_text(
        json.dumps({"tokens": {"access_token": "synthetic-native-access-token"}})
    )
    auth.chmod(0o600)
    config = tmp_path / "spawn.toml"
    config.write_text(
        "\n".join(
            [
                "[store]",
                f"run_root={json.dumps(str(tmp_path / 'runs'))}",
                "[bins]",
                f"codex={json.dumps(str(binary))}",
                "[[provider]]",
                'name="codex"',
                'kind="codex"',
                'model="test-model"',
                'models=["test-model"]',
                'effort="low"',
                'efforts=["low", "high"]',
                "[provider.accounts.first]",
                f"home={json.dumps(str(home))}",
            ]
        )
        + "\n"
    )
    env = {
        key: os.environ[key]
        for key in ("PATH", "HOME", "LANG", "NOTT_DATABASE_DIR")
        if key in os.environ
    }
    env.update(
        NOTT_NSPAWN_DRIVER_LAUNCH="direct",
        NOTT_NSPAWN_CONFIG=str(config),
        NOTT_NSPAWN_ROSTER=str(config),
        # As `test_native_execute.native_client` serves it: the vendor fixture
        # checks this optional MCP credential is scoped wherever it appears.
    )
    env["OPENAI_API_KEY"] = "synthetic-mcp-only-credential"
    with subprocess.Popen(
        [str(Path(sys.executable).with_name("nspawn")), "mcp", "serve"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=tmp_path,
        env=env,
    ) as process:
        client = Client(process)
        try:
            response = client.request(
                "initialize",
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "effort-test", "version": "1"},
                },
            )
            assert "result" in response
            process.stdin.write(
                b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
            )
            process.stdin.flush()
            yield client, tmp_path, env
        finally:
            client.selector.close()
            process.stdin.close()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def test_a_codex_dispatch_and_its_resume_each_answer_their_own_effort(
    codex_door: tuple[Client, Path, dict[str, str]],
) -> None:
    client, cwd, env = codex_door
    args: dict[str, Any] = {
        "provider": "first",
        "cwd": str(cwd),
        "prompt": "Reply PONG",
        "class": DECLARED_CLASS,
        "read_only": True,
        "timeout_ms": 5000,
    }
    # The caller pins `high`: asked and sent are the same value.
    origin = client.call("nspawn_run_start", {**args, "effort": "high"})["run_id"]
    first = client.terminal(origin)
    assert first["phase"] == "completed", first
    assert first["effort"] == _recorded("high", "high")
    # The resume asks for nothing and the provider line resolves `low`: a NEW
    # run on the SAME thread, with a record of its own.
    resumed = client.call(
        "nspawn_run_start", {**args, "session_id": first["thread_id"]}
    )["run_id"]
    second = client.terminal(resumed)
    assert second["phase"] == "completed", second
    assert second["thread_id"] == first["thread_id"]
    assert second["effort"] == _recorded(None, "low")
    # What `sent` answers is what the vendor received on `turn/start`, turn by
    # turn; each turn also announced itself with an effortless `turn/started`.
    received = (cwd / "turn-efforts.log").read_text().splitlines()
    assert [json.loads(line) for line in received] == ["high", "low"]
    # The resume did not rewrite the origin's answer.
    again = client.call("nspawn_run_show", {"run_id": origin, "timeout_ms": 0})
    assert again["effort"] == _recorded("high", "high")
    # And the operator's JSON CLI prints the same answer from the same store.
    for run_id, expected in ((origin, first), (resumed, second)):
        shown = subprocess.run(
            [str(Path(sys.executable).with_name("nspawn")), "run", "show", run_id],
            capture_output=True,
            text=True,
            cwd=cwd,
            env=env,
            timeout=60,
            check=False,
        )
        assert shown.returncode == 0, shown.stderr
        assert json.loads(shown.stdout)["effort"] == expected["effort"]
