"""A provider whose balance is spent is walled for the hour, not retried.

Z.AI answers an empty balance with a 429 whose body carries its own code,
`1113`; a transient rate limit is a 429 too. The status alone cannot tell them
apart, so the owner declares the codes that mean exhaustion (`exhausted_codes`)
and nSPAWN reads the code from the CLI's exact rendering of the refusal, and
from nothing else. The fixture is a frame pair recorded from Claude Code 2.1.282
against Z.AI on 2026-09-25, request id redacted: a change in the vendor's
format turns these tests red instead of silently walling.
"""

from __future__ import annotations

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

FIXTURE = Path(__file__).parent / "fixtures" / "claude-zai-1113-2.1.282.ndjson"
RECORDED = FIXTURE.read_text(encoding="utf-8")
LINE = (
    "API Error: Request rejected (429) · [1113][Insufficient balance or no "
    "resource package. Please recharge.][REDACTED_REQUEST_ID]"
)


def test_the_fixture_is_the_recorded_refusal() -> None:
    assert RECORDED.count(LINE) == 2
    assert '"api_error_status":429' in RECORDED


@pytest.mark.parametrize(
    ("text", "status", "code"),
    [
        (LINE, 429, "1113"),
        (LINE.replace("[1113]", "[1302]"), 429, "1302"),
        (LINE.replace("(429)", "(402)"), 402, "1113"),
        # The status in the text must be the status the result reports.
        (LINE, 402, None),
        (LINE, None, None),
        # Anything but the exact rendering is no code at all.
        (LINE.replace("[1113][", "[1113] ["), 429, None),
        (LINE.replace("[1113]", "[11a3]"), 429, None),
        (LINE.replace("[1113]", "[]"), 429, None),
        (LINE.replace("[1113]", "[١١١٣]"), 429, None),
        (LINE.replace(" · ", " - "), 429, None),
        (LINE.replace("Request rejected", "request rejected"), 429, None),
        ("the model said API Error: Request rejected (429) · [1113][", 429, None),
        ("Insufficient balance or no resource package. [1113]", 429, None),
        ("", 429, None),
        (None, 429, None),
    ],
)
def test_the_code_is_read_only_from_the_clis_exact_rendering(
    text: str | None, status: int | None, code: str | None
) -> None:
    assert terminal.rejected_code(text, status) == code


def test_the_recorded_run_reaches_the_parser(tmp_path: Path) -> None:
    """The classifier keeps the line the parser reads, and the status beside it."""
    stdout, stderr = tmp_path / "stdout", tmp_path / "stderr"
    stdout.write_text(RECORDED, encoding="utf-8")
    stderr.write_text("", encoding="utf-8")
    verdict = terminal.classify_terminal_run(
        stdout, stderr, 1, provider="zai", account_alias=None, now=1_000_000
    )
    assert verdict.phase is RunPhase.FAILED
    assert verdict.api_error_status == 429
    assert terminal.rejected_code(verdict.final_text, verdict.api_error_status) == (
        "1113"
    )


def _zai(codes: frozenset[str]) -> tuple[Roster, Provider]:
    provider = Provider(
        name="zai",
        auth_env="ANTHROPIC_AUTH_TOKEN",
        env={"ANTHROPIC_BASE_URL": "https://api.z.ai/api/anthropic"},
        model="glm-5.3",
        models=["glm-5.3"],
        exhausted_codes=codes,
    )
    return Roster(providers=[provider], defaults=Defaults(plugin_root=Path("/"))), (
        provider
    )


def _drive(
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: Provider,
    roster: Roster,
    recorded: str,
) -> tuple[supervisor.Supervisor, str]:
    """One env-route run whose child left `recorded` on its stdout."""
    stdout, stderr = tmp_path / "recorded.stdout", tmp_path / "recorded.stderr"
    stdout.write_text(recorded, encoding="utf-8")
    stderr.write_text("", encoding="utf-8")
    real = terminal.classify_terminal_run

    def replay(*_: object, **named: object) -> terminal.TerminalClassification:
        return real(stdout, stderr, 1, **named)  # type: ignore[arg-type]

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
        executed_account=None,
        model="glm-5.3",
        effort=None,
    )
    try:
        sv._drive_run(
            run_id, str(tmp_path), run_dir, "ping", roster, picked, thread_id,
            False, True,
        )  # fmt: skip
    finally:
        sv.shutdown()
    row = registry.get_run(run_id)
    assert row is not None and row.phase is RunPhase.FAILED
    return sv, run_id


def _refused_with(status: int) -> str:
    return RECORDED.replace("(429)", f"({status})").replace(
        '"api_error_status":429', f'"api_error_status":{status}'
    )


@pytest.mark.parametrize(
    ("codes", "recorded", "reason"),
    [
        (frozenset(), RECORDED, "VendorApiError (HTTP 429)"),
        (frozenset({"1113"}), RECORDED, "VendorApiError (HTTP 429)"),
        (frozenset(), _refused_with(402), "VendorApiError (HTTP 402)"),
        (frozenset(), _refused_with(400), "VendorApiError (HTTP 400)"),
    ],
    ids=["429", "429-walled", "402-walled", "400"],
)
def test_a_run_the_vendor_refused_records_a_typed_failure_reason(
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    codes: frozenset[str],
    recorded: str,
    reason: str,
) -> None:
    """Reviewer 3095744f failed with `failure_reason` null and its cause only in
    the warning. The reason carries the status and never the vendor's prose."""
    roster, provider = _zai(codes)
    sv, run_id = _drive(registry, tmp_path, monkeypatch, provider, roster, recorded)

    status = sv.status(run_id)
    assert status.failure_reason == sv.result(run_id).failure_reason == reason
    assert status.warning is not None and "Insufficient balance" not in reason


def test_a_failure_the_vendor_gave_no_status_records_its_closed_cause(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    roster, provider = _zai(frozenset())
    recorded = RECORDED.replace('"api_error_status":429', '"api_error_status":null')
    sv, run_id = _drive(registry, tmp_path, monkeypatch, provider, roster, recorded)

    reason = "ClaudeRunFailed (terminal_incomplete)"
    assert (
        sv.status(run_id).failure_reason == sv.result(run_id).failure_reason == reason
    )


def test_a_declared_exhaustion_code_walls_the_provider_for_the_hour(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """And the next dispatch is refused at once, carrying when the wall lifts."""
    roster, provider = _zai(frozenset({"1113"}))
    before = int(time.time())
    _drive(registry, tmp_path, monkeypatch, provider, roster, RECORDED)
    after = int(time.time())

    (wall,) = registry.get_provider_wall_rows(after)
    assert wall.provider == "zai"
    assert wall.source == terminal.WallSource.DEFAULT_1H.value
    assert (
        before + supervisor.Supervisor.CREDENTIAL_WALL_S
        <= wall.cooling_until
        <= after + supervisor.Supervisor.CREDENTIAL_WALL_S
    )
    with pytest.raises(picker.AllProvidersWalled) as refused:
        picker.pick_execution(
            roster,
            after,
            requested_provider="zai",
            provider_walls=registry.get_provider_walls(after),
            account_walls=registry.get_account_walls(after),
        )
    assert refused.value.nearest_reset == wall.cooling_until


@pytest.mark.parametrize(
    ("codes", "recorded"),
    [
        # An undeclared code is a rate limit until the owner says otherwise.
        (frozenset({"1113"}), RECORDED.replace("[1113]", "[1302]")),
        # Text that is not the exact rendering carries no code.
        (frozenset({"1113"}), RECORDED.replace("Request rejected (429) · ", "")),
        # A provider that declares no codes walls its route for minutes only.
        (frozenset(), RECORDED),
    ],
    ids=["undeclared-code", "unparseable", "no-declared-codes"],
)
def test_a_429_without_a_declared_exhaustion_code_walls_the_route_for_minutes(
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    codes: frozenset[str],
    recorded: str,
) -> None:
    """nSPAWN is the router: the next dispatches go
    elsewhere for a while, and the route serves again when the wall lifts."""
    roster, provider = _zai(codes)
    before = int(time.time())
    _drive(registry, tmp_path, monkeypatch, provider, roster, recorded)
    after = int(time.time())
    (wall,) = registry.get_provider_wall_rows(after)
    assert wall.provider == "zai"
    assert (
        before + supervisor.Supervisor.RATE_LIMIT_WALL_S
        <= wall.cooling_until
        <= after + supervisor.Supervisor.RATE_LIMIT_WALL_S
    )
    assert registry.get_account_walls(after) == {}


def test_a_402_on_an_env_route_walls_the_provider_for_the_hour(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no account to wall, the spent quota belongs to the provider."""
    roster, provider = _zai(frozenset())
    recorded = RECORDED.replace("(429)", "(402)").replace(
        '"api_error_status":429', '"api_error_status":402'
    )
    before = int(time.time())
    _drive(registry, tmp_path, monkeypatch, provider, roster, recorded)
    after = int(time.time())

    (wall,) = registry.get_provider_wall_rows(after)
    assert wall.provider == "zai"
    assert wall.source == terminal.WallSource.DEFAULT_1H.value
    assert (
        before + supervisor.Supervisor.CREDENTIAL_WALL_S
        <= wall.cooling_until
        <= after + supervisor.Supervisor.CREDENTIAL_WALL_S
    )
