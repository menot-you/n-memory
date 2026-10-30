"""Anthropic answers a request on a key whose credit balance is spent with HTTP
400 and the text "Your credit balance is too low to access the Anthropic API".
Claude Code prints that text, or its own short form "Credit balance is too
low", as an error result. The frames here take the shape of the recorded
organization refusal (`claude-oauth-org-403-2.1.257.ndjson`) with that text and
status in its place. They are invented, not recorded, and carry no key.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from supervisor_support import (
    FakeSpawnedChild,
    _admit_pending,
    _drain_worker_threads,
    _spawn_real,
    _stop_every_reaper,
    _sv,
    _wire_l8_and_l3,
    registry,
)

from nspawn import picker, supervisor, terminal
from nspawn.owner_roster import Defaults, Provider, Roster
from nspawn.registry import Registry
from nspawn.rows import RunPhase

__all__ = ["_drain_worker_threads", "_stop_every_reaper", "registry"]

ORG_REFUSAL = Path(__file__).parent / "fixtures" / "claude-oauth-org-403-2.1.257.ndjson"
API_TEXT = (
    "Your credit balance is too low to access the Anthropic API. Please go to "
    "Plans & Billing to upgrade or purchase credits."
)
CLI_TEXT = "Credit balance is too low"
NOW = 1_000_000
WRAPPED_TEXT = (
    'API Error: 400 {"type":"error","error":{"type":"invalid_request_error",'
    f'"message":"{API_TEXT}"}}}}'
)


def _refusal(text: str, status: int | None) -> str:
    """The recorded organization refusal with the credit text and `status`."""
    lines = []
    for line in ORG_REFUSAL.read_text(encoding="utf-8").splitlines():
        frame = json.loads(line)
        if frame["type"] == "assistant":
            frame["message"]["content"] = [{"type": "text", "text": text}]
            del frame["error"]
        else:
            frame["result"] = text
            frame["api_error_status"] = status
        lines.append(json.dumps(frame))
    return "\n".join(lines) + "\n"


def _classify(
    tmp_path: Path, stdout_text: str, exit_code: int = 1
) -> terminal.TerminalClassification:
    stdout, stderr = tmp_path / "stdout", tmp_path / "stderr"
    stdout.write_text(stdout_text, encoding="utf-8")
    stderr.write_text("", encoding="utf-8")
    return terminal.classify_terminal_run(
        stdout, stderr, exit_code, provider="claude", account_alias="key-a", now=NOW
    )


@pytest.mark.parametrize("status", [400, None])
@pytest.mark.parametrize(
    "text", [API_TEXT, CLI_TEXT, WRAPPED_TEXT], ids=["api", "cli", "wrapped"]
)
def test_a_credit_refusal_is_classified_as_a_spent_balance(
    tmp_path: Path, text: str, status: int | None
) -> None:
    verdict = _classify(tmp_path, _refusal(text, status))
    assert verdict.phase is RunPhase.FAILED
    assert verdict.api_error_status == 402
    assert "quota exhausted" in (verdict.warning or "")


def test_the_run_status_reads_the_same_class(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "stdout").write_text(_refusal(CLI_TEXT, 400), encoding="utf-8")
    observed = supervisor._observe_streams(run_dir)
    assert observed.api_error_status == 402


@pytest.mark.parametrize("status", [401, 404, 429, 500])
def test_another_status_keeps_its_own_class(tmp_path: Path, status: int) -> None:
    verdict = _classify(tmp_path, _refusal(CLI_TEXT, status))
    assert verdict.phase is RunPhase.FAILED
    assert verdict.api_error_status == status


def test_the_refusal_frames_keep_the_recorded_api_error_shape() -> None:
    # The credit rule reads an API error result only: this shape, not an answer.
    frame = json.loads(_refusal(CLI_TEXT, 400).splitlines()[-1])
    assert (frame["type"], frame["is_error"], frame["terminal_reason"]) == (
        "result",
        True,
        "api_error",
    )


def test_an_answer_that_quotes_the_text_is_not_a_refusal(tmp_path: Path) -> None:
    frame = json.loads(_refusal(CLI_TEXT, None).splitlines()[-1])
    frame.update(is_error=False, terminal_reason="completed", stop_reason="end_turn")
    frame["result"] = f"The key answers {API_TEXT!r} when its credit ends."
    verdict = _classify(tmp_path, json.dumps(frame) + "\n", exit_code=0)
    assert verdict.phase is RunPhase.COMPLETED
    assert verdict.api_error_status is None


def _max_turns(answer: str) -> str:
    """A max_turns failure whose answer is `answer`: the failure shape of
    `test_terminal.py` (`error_during_execution`, `max_turns`), not an API error."""
    frame = json.loads(_refusal(CLI_TEXT, None).splitlines()[-1])
    frame.update(
        is_error=True,
        subtype="error_during_execution",
        terminal_reason="max_turns",
        stop_reason="end_turn",
        result=answer,
    )
    return json.dumps(frame) + "\n"


QUOTES = [
    CLI_TEXT,
    API_TEXT,
    WRAPPED_TEXT,
    f"Turn limit reached. The key answers {API_TEXT!r} when its credit ends.",
]


@pytest.mark.parametrize("answer", QUOTES, ids=["cli", "api", "wrapped", "quoted"])
def test_a_max_turns_failure_that_quotes_the_text_stays_max_turns(
    tmp_path: Path, answer: str
) -> None:
    verdict = _classify(tmp_path, _max_turns(answer))
    assert verdict.phase is RunPhase.FAILED
    assert verdict.api_error_status is None
    assert (verdict.account_wall, verdict.provider_wall) == (None, None)
    assert "did not complete (max_turns)" in (verdict.warning or "")
    assert "quota exhausted" not in (verdict.warning or "")


def _key_roster(tmp_path: Path, *aliases: str) -> tuple[Roster, Provider]:
    secrets = tmp_path / "secrets" / "claude"
    secrets.mkdir(parents=True, exist_ok=True)
    secrets.chmod(0o700)
    for alias in aliases:
        token = secrets / f"{alias}.token"
        token.write_text("invented-test-bytes", encoding="utf-8")
        token.chmod(0o600)
    provider = Provider(
        name="claude",
        secrets=secrets,
        auth_env="ANTHROPIC_API_KEY",
        models=["claude-opus-5-5"],
        model="claude-opus-5-5",
        fill_first=True,
    )
    return Roster(providers=[provider], defaults=Defaults(plugin_root=tmp_path)), (
        provider
    )


def _completed() -> terminal.TerminalClassification:
    return terminal.TerminalClassification(
        phase=RunPhase.COMPLETED,
        final_text="PING_OK",
        warning=None,
        marker=None,
        provider_wall=None,
        account_wall=None,
    )


def _drive(
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    roster: Roster,
    provider: Provider,
    verdicts: list[terminal.TerminalClassification | None],
    stdout: str | None = None,
) -> tuple[str, int]:
    """One unpinned run on key-a; `None` in `verdicts` replays `stdout`, by
    default the credit refusal."""
    refusal = tmp_path / "refusal"
    refusal.mkdir()
    (refusal / "stdout").write_text(
        _refusal(CLI_TEXT, 400) if stdout is None else stdout, encoding="utf-8"
    )
    (refusal / "stderr").write_text("", encoding="utf-8")
    real = terminal.classify_terminal_run
    spawned: list[int] = []

    def classify(*_: object, **named: object) -> terminal.TerminalClassification:
        verdict = verdicts.pop(0)
        if verdict is not None:
            return verdict
        return real(refusal / "stdout", refusal / "stderr", 1, **named)  # type: ignore[arg-type]

    def spawned_factory(*_: object, **__: object) -> FakeSpawnedChild:
        spawned.append(1)
        return FakeSpawnedChild(_spawn_real("true"))

    _wire_l8_and_l3(monkeypatch, spawned_factory)
    monkeypatch.setattr(supervisor.terminal, "classify_terminal_run", classify)
    sv = _sv(registry, tmp_path)
    run_id, thread_id = _admit_pending(sv, registry, tmp_path, worktree=str(tmp_path))
    run_dir = supervisor.RunDirectory(
        root=sv._run_root_path / run_id,
        stdout=sv._run_root_path / run_id / "stdout",
        stderr=sv._run_root_path / run_id / "stderr",
    )
    picked = picker.PickerResult(
        requested_provider="claude",
        executed_provider=provider,
        executed_account="key-a",
        model="claude-opus-5-5",
        effort=None,
    )
    try:
        sv._drive_run(
            run_id,
            str(tmp_path),
            run_dir,
            "ping",
            roster,
            picked,
            thread_id,
            False,
            True,
        )
    finally:
        sv.shutdown()
    return run_id, len(spawned)


def test_a_credit_refusal_walls_the_key_and_walks_to_the_next(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    roster, provider = _key_roster(tmp_path, "key-a", "key-b")
    run_id, spawned = _drive(
        registry, tmp_path, monkeypatch, roster, provider, [None, _completed()]
    )
    row = registry.get_run(run_id)
    assert row is not None
    assert (row.phase, row.executed_account, spawned) == (
        RunPhase.COMPLETED,
        "key-b",
        2,
    )
    now = int(time.time()) + 1
    walls = {wall.account_alias: wall for wall in registry.get_account_wall_rows(now)}
    assert walls["key-a"].limit_type == "quota_exhausted"
    assert walls["key-a"].cooling_until > now
    later = picker.pick_execution(
        roster, now, account_walls=registry.get_account_walls(now)
    )
    assert later.executed_account == "key-b"


def test_a_lone_key_with_no_credit_fails_and_is_walled(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    roster, provider = _key_roster(tmp_path, "key-a")
    run_id, spawned = _drive(registry, tmp_path, monkeypatch, roster, provider, [None])
    row = registry.get_run(run_id)
    assert row is not None
    assert (row.phase, spawned) == (RunPhase.FAILED, 1)
    assert "quota exhausted" in (row.warning or "")
    now = int(time.time()) + 1
    assert registry.get_account_walls(now).get("key-a", 0) > now


def test_a_max_turns_failure_that_quotes_the_text_walls_nothing(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    roster, provider = _key_roster(tmp_path, "key-a", "key-b")
    run_id, spawned = _drive(
        registry,
        tmp_path,
        monkeypatch,
        roster,
        provider,
        [None, _completed()],
        stdout=_max_turns(CLI_TEXT),
    )
    row = registry.get_run(run_id)
    assert row is not None
    # One spawn: the run never walked off key-a.
    assert (row.phase, spawned) == (RunPhase.FAILED, 1)
    assert "quota exhausted" not in (row.warning or "")
    assert registry.get_account_walls(int(time.time()) + 1) == {}
