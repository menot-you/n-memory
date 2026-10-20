"""The Colab route: named-only selection, fill-first accounts, and the balance reader."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from supervisor_support import (
    _drain_worker_threads,
    _stop_every_reaper,
    _sv,
    registry,
)

from nspawn import cli, quota_sources, supervisor
from nspawn.owner_roster import NativeAccount, Provider, Roster, RosterError
from nspawn.picker import (
    AllProvidersWalled,
    PickerError,
    pick_execution,
    serviceable_routes,
)
from nspawn.quota_sources import ComputeBalance, QuotaSourceError, parse_colab_usage
from nspawn.registry import Registry
from nspawn.rows import ComputeBalanceRow

__all__ = ["_drain_worker_threads", "_stop_every_reaper", "registry"]

NOW = 1_000_000
TEMPLATE = Path(__file__).resolve().parents[3] / "templates" / "nott" / "spawn.toml"
#: Recorded from the real `colab usage` on SERVER, 2026-10-19.
USAGE = (
    "Current balance: 4724.87 compute units\n"
    "Usage rate: 17.80/hr\n"
    "Active assignments: 2\n"
)


def _colab(*, fill_first: bool = True, named_only: bool = True) -> Provider:
    return Provider(
        name="colab",
        model="qwen3.8-flash-next",
        named_only=named_only,
        fill_first=fill_first,
        declared=True,
        accounts={
            alias: NativeAccount(
                home=None, base_url=f"http://127.0.0.1:{port}", capacity=4
            )
            for alias, port in (("colab-1", 18090), ("colab-2", 18092))
        },
    )


def _claude() -> Provider:
    return Provider(name="claude", model="opus", models=["opus"])


@pytest.mark.parametrize("load", [{}, {"colab-1": 1}, {"colab-1": 3}])
def test_fill_first_keeps_the_warm_account_until_it_is_full(
    load: dict[str, int],
) -> None:
    roster = Roster(providers=[_colab()])
    picked = pick_execution(roster, NOW, requested_provider="colab", account_load=load)
    assert picked.executed_account == "colab-1"


def test_fill_first_opens_the_cold_account_only_when_the_warm_one_is_full() -> None:
    roster = Roster(providers=[_colab()])
    picked = pick_execution(
        roster, NOW, requested_provider="colab", account_load={"colab-1": 4}
    )
    assert picked.executed_account == "colab-2"


def test_without_fill_first_the_emptiest_account_still_wins() -> None:
    roster = Roster(providers=[_colab(fill_first=False)])
    picked = pick_execution(
        roster, NOW, requested_provider="colab", account_load={"colab-1": 1}
    )
    assert picked.executed_account == "colab-2"


def test_a_named_only_route_never_takes_an_unnamed_dispatch() -> None:
    roster = Roster(providers=[_colab(), _claude()])
    assert pick_execution(roster, NOW).executed_provider.name == "claude"
    walled = {"claude": NOW + 60}
    with pytest.raises(AllProvidersWalled):
        pick_execution(roster, NOW, provider_walls=walled)
    with pytest.raises(AllProvidersWalled):
        pick_execution(roster, NOW, execution_class="judgment", provider_walls=walled)
    with pytest.raises(PickerError):
        pick_execution(roster, NOW, requested_model="qwen3.8-flash-next")
    assert serviceable_routes(roster, NOW) == ["claude"]


def test_a_named_only_route_answers_when_the_caller_names_it() -> None:
    roster = Roster(providers=[_colab(), _claude()])
    picked = pick_execution(roster, NOW, requested_provider="colab")
    assert picked.executed_provider.name == "colab"
    assert pick_execution(
        roster, NOW, requested_provider="colab-2"
    ).executed_account == ("colab-2")


def test_the_template_declares_colab_named_only_and_fill_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    template = TEMPLATE.read_text(encoding="utf-8")
    # The template seeds no host route: the colab block is a commented example.
    assert Roster.read(TEMPLATE).select("colab") is None
    start = template.index('# [[provider]]\n# name = "colab"')
    end = template.index("\n\n", start)
    example = "\n".join(line[2:] for line in template[start:end].split("\n"))
    path = tmp_path / "spawn.toml"
    path.write_text(template + "\n" + example + "\n", encoding="utf-8")
    roster = Roster.read(path)
    colab = roster.select("colab")
    assert colab is not None
    assert (colab.kind, colab.named_only, colab.fill_first, colab.quota_source) == (
        "claude",
        True,
        True,
        "colab",
    )
    assert [a.base_url for a in colab.accounts.values()] == [
        "http://127.0.0.1:18090",
        "http://127.0.0.1:18092",
    ]
    assert [a.bootstrap for a in colab.accounts.values()] == [
        ("systemctl", "--user", "start", "colab-llm@1"),
        ("systemctl", "--user", "start", "colab-llm@2"),
    ]
    assert {a.capacity for a in colab.accounts.values()} == {4}
    assert sorted(colab.env) == ["CLAUDE_CODE_ATTRIBUTION_HEADER", "ENABLE_TOOL_SEARCH"]
    assert pick_execution(roster, NOW).executed_provider.name != "colab"


@pytest.mark.parametrize(
    ("key", "value"),
    [("named_only", '"yes"'), ("fill_first", "1"), ("quota_source", '"aws"')],
)
def test_the_roster_refuses_a_malformed_colab_key(
    tmp_path: Path, key: str, value: str
) -> None:
    path = tmp_path / "spawn.toml"
    path.write_text(
        f'[[provider]]\nname = "colab"\nmodel = "m"\n{key} = {value}\n',
        encoding="utf-8",
    )
    with pytest.raises(RosterError, match=key):
        Roster.read(path)


def test_parse_colab_usage_reads_the_three_numbers() -> None:
    assert parse_colab_usage(USAGE, 7) == ComputeBalance(4724.87, 17.8, 2, 7)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "Current balance: lots\nUsage rate: 1/hr\nActive assignments: 0\n",
        USAGE.replace("Active assignments: 2\n", ""),
    ],
)
def test_parse_colab_usage_refuses_what_it_cannot_read(text: str) -> None:
    with pytest.raises(QuotaSourceError):
        parse_colab_usage(text, 7)


def test_read_colab_usage_turns_a_failed_command_into_a_typed_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(["colab"], 1, "", "boom")

    monkeypatch.setattr(quota_sources.subprocess, "run", fail)
    with pytest.raises(QuotaSourceError):
        quota_sources.read_colab_usage(7)


def _quota_roster(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "spawn.toml"
    path.write_text(
        '[defaults]\nplugins = ["/tmp/plugin-root"]\n\n'
        '[[provider]]\nname = "colab"\nkind = "claude"\nmodel = "q"\n'
        'named_only = true\nquota_source = "colab"\n'
        '[provider.accounts.colab-1]\nbase_url = "http://127.0.0.1:18090"\n'
        "capacity = 4\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("NOTT_NSPAWN_ROSTER", str(path))
    monkeypatch.setenv("NOTT_NSPAWN_CONFIG", str(tmp_path / "absent-spawn.toml"))


def test_read_colab_usage_turns_a_missing_binary_into_a_typed_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("colab")

    monkeypatch.setattr(quota_sources.subprocess, "run", missing)
    with pytest.raises(QuotaSourceError, match="FileNotFoundError"):
        quota_sources.read_colab_usage(7)


def test_the_roster_refuses_a_named_only_default(tmp_path: Path) -> None:
    path = tmp_path / "spawn.toml"
    path.write_text(
        '[[provider]]\nname = "colab"\nmodel = "m"\nnamed_only = true\n'
        "default = true\n",
        encoding="utf-8",
    )
    with pytest.raises(RosterError, match="named_only and default"):
        Roster.read(path)


@pytest.mark.parametrize("balance", ["1" * 400, "1" * 13])
def test_parse_colab_usage_refuses_an_absurd_balance(balance: str) -> None:
    with pytest.raises(QuotaSourceError):
        parse_colab_usage(USAGE.replace("4724.87", balance), 7)


def test_parse_colab_usage_refuses_an_oversized_count() -> None:
    with pytest.raises(QuotaSourceError):
        parse_colab_usage(USAGE.replace("2\n", "9" * 5000 + "\n"), 7)


def test_read_colab_usage_closes_stdin_and_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    def run(*_args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.update(kwargs)
        return subprocess.CompletedProcess(["colab"], 0, USAGE, "")

    monkeypatch.setenv("NSPAWN_TEST_SECRET", "x")
    monkeypatch.setattr(quota_sources.subprocess, "run", run)
    assert quota_sources.read_colab_usage(7).balance == 4724.87
    assert seen["stdin"] is subprocess.DEVNULL
    env = seen["env"]
    assert isinstance(env, dict) and set(env) <= {"HOME", "PATH"}


def _pull(
    registry: Registry, monkeypatch: pytest.MonkeyPatch, now: int
) -> dict[str, str]:
    monkeypatch.setattr(supervisor.time, "time", lambda: now)
    return supervisor.pull_compute(registry, supervisor._read_roster())


def _colab_quota(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, now: int
) -> supervisor.ProviderQuota:
    monkeypatch.setattr(supervisor.time, "time", lambda: now)
    (colab,) = _sv(registry, tmp_path).quota("colab").providers  # refresh=0, as the CLI
    return colab


def _reads(monkeypatch: pytest.MonkeyPatch, *answers: str | Exception) -> list[int]:
    calls: list[int] = []
    pending = list(answers)

    def read(now: int) -> ComputeBalance:
        calls.append(now)
        answer = pending.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return parse_colab_usage(answer, now)

    monkeypatch.setattr(quota_sources, "read_colab_usage", read)
    return calls


def test_quota_show_never_runs_the_command_and_names_a_missing_pull(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _quota_roster(tmp_path, monkeypatch)
    calls = _reads(monkeypatch)
    colab = _colab_quota(registry, tmp_path, monkeypatch, 1000)
    assert colab.compute is None and calls == []
    assert colab.compute_absence == "not read yet: nspawn quota record has not run"


def test_a_pull_stores_the_reading_and_show_gives_its_age(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _quota_roster(tmp_path, monkeypatch)
    _reads(monkeypatch, USAGE)
    assert _pull(registry, monkeypatch, 1000) == {"colab": "ok"}
    colab = _colab_quota(registry, tmp_path, monkeypatch, 1150)
    assert colab.compute == ComputeBalance(4724.87, 17.8, 2, 1000)
    assert colab.compute_updated == "updated 2 min ago"
    assert not colab.compute_stale and colab.compute_failure is None


def test_the_store_refuses_an_older_reading_and_the_record_says_so(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _quota_roster(tmp_path, monkeypatch)
    _reads(monkeypatch, USAGE, USAGE.replace("4724.87", "1.00"))
    _pull(registry, monkeypatch, 2000)
    outcome = _pull(registry, monkeypatch, 1000)  # the clock went back
    assert outcome["colab"] != "ok"
    row = registry.get_compute_balance("colab")
    assert row is not None and (row.balance, row.observed_at) == (4724.87, 2000)
    assert _colab_quota(registry, tmp_path, monkeypatch, 1000).compute_stale


def test_a_failed_pull_keeps_the_good_reading_and_reports_why(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _quota_roster(tmp_path, monkeypatch)
    _reads(monkeypatch, USAGE, QuotaSourceError("colab usage exited 1"))
    _pull(registry, monkeypatch, 1000)
    assert _pull(registry, monkeypatch, 1060) == {"colab": "colab usage exited 1"}
    colab = _colab_quota(registry, tmp_path, monkeypatch, 1070)
    assert colab.compute is not None and colab.compute.balance == 4724.87
    assert (colab.compute_failure, colab.compute_failed_at) == (
        "colab usage exited 1",
        1060,
    )


def test_a_reading_past_ten_intervals_is_marked_stale(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _quota_roster(tmp_path, monkeypatch)
    _reads(monkeypatch, USAGE)
    _pull(registry, monkeypatch, 1000)
    assert not _colab_quota(registry, tmp_path, monkeypatch, 1600).compute_stale
    assert _colab_quota(registry, tmp_path, monkeypatch, 1601).compute_stale


@pytest.mark.parametrize(
    "fields",
    [
        {"balance": 1.0},
        {"failure": "x"},
        {"failure": "", "failed_at": 1},
    ],
)
def test_a_compute_row_refuses_half_a_value(fields: dict[str, object]) -> None:
    base: dict[str, object] = dict.fromkeys(
        (
            "balance",
            "rate_per_hour",
            "active_assignments",
            "observed_at",
            "failure",
            "failed_at",
        )
    )
    with pytest.raises(ValueError):
        ComputeBalanceRow(provider="colab", **{**base, **fields})  # type: ignore[arg-type]


def test_a_provider_quota_refuses_an_age_without_a_reading() -> None:
    with pytest.raises(supervisor.InvalidRunRequest):
        supervisor.ProviderQuota(
            provider="colab",
            models=(),
            state=supervisor.QuotaState.NO_SOURCE,
            absence="x",
            compute_absence="none",
            compute_stale=True,
        )


def test_the_record_verb_stores_and_exits_by_its_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _quota_roster(tmp_path, monkeypatch)
    _reads(monkeypatch, USAGE, QuotaSourceError("colab usage exited 1"))
    assert cli.cmd_quota_record(["extra"]) == 2
    assert cli.cmd_quota_record([]) == 0
    assert '"colab": "ok"' in capsys.readouterr().out
    assert cli.cmd_quota_record([]) == 1
    assert "exited 1" in capsys.readouterr().out


def test_a_success_after_a_failure_clears_it(registry: Registry) -> None:
    registry.record_compute_failure("colab", "down", 100)
    registry.record_compute_reading(
        ComputeBalanceRow("colab", 1.0, 1.0, 1, 110, None, None)
    )
    row = registry.get_compute_balance("colab")
    assert row is not None and (row.failure, row.failed_at) == (None, None)
