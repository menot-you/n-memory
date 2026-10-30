"""U3: an agent file's model and effort are a suggestion, never a refusal.

Precedence (PLAN-v4, Invariants): the caller's pin, then the class tier for a
`judgment` class, then the agent file's suggestion, then the class route. A
suggestion that no route serves gives a warning and the class route.

The picker tests carry the suggested model in `preferred_model`, the input the
plan names (`picker.py:1214`). The start tests send `agent=` through
`nspawn_run_start` into the real supervisor and the real picker, so the
suggestion's provider and effort arrive by the input U3 adds.

GPT runs through the Claude client on the claude-kind route `gpt` (owner
decision): a Codex route appears here only to prove it never serves a
suggestion and never records an effort Codex rewrites.
"""

from __future__ import annotations

from pathlib import Path
from threading import RLock
from types import SimpleNamespace
from typing import Any, NoReturn

import pytest
from class_dispatch import system1_never_reaches_the_network
from mcp_fakes import handle
from supervisor_support import (
    FakeSpawnedChild,
    _admit_pending,
    _drain_worker_threads,
    _spawn_real,
    _stop_every_reaper,
    _sv,
    _wire_l8_and_l3,
    fake_picked,
    fake_provider,
    registry,
    roster_home,
)
from test_agent_prompt import (
    _agent_plugin,
    _claude_route,
    _completed,
    _shown,
)

from nspawn import mcp, picker, supervisor, terminal
from nspawn.owner_roster import ClassPolicy, Defaults, NativeAccount, Provider, Roster
from nspawn.ranks import IMPLEMENTATION, JUDGMENT, MECHANICAL_FANOUT, REVIEW
from nspawn.registry import Registry
from nspawn.rows import RunPhase

# Export imported fixtures so pytest discovers them in this module.
__all__ = [
    "_drain_worker_threads",
    "_stop_every_reaper",
    "fake_picked",
    "fake_provider",
    "registry",
    "roster_home",
    "system1_never_reaches_the_network",
]

NOW = 1_000_000
FIXTURE = Path(__file__).parent / "fixtures" / "owner_roster.toml"

#: The slice-1 agents with the frontmatter U1 gives them (PLAN-v4, U1 table).
_AGENTS = {
    "worker": "---\nname: worker\n---\n\nYou work.\n",
    "explorer": "---\nname: explorer\nmodel: haiku\neffort: low\n---\n\nYou explore.\n",
    "planner": "---\nname: planner\nmodel: opus\neffort: max\n---\n\nYou plan.\n",
    "adversarial": (
        "---\nname: adversarial\nnott-provider: gpt\n"
        "nott-model: gpt-6.1-sol\neffort: max\n---\n\nYou argue.\n"
    ),
}

_BINS = '[bins]\nclaude = "/never-execute/vendor"\n'

#: A Claude route whose catalogue is family aliases.
_CLAUDE = (
    '[[provider]]\nname = "claude"\nkind = "claude"\n'
    'secrets = "/never-read/secrets"\nstrength = 9\ndefault = true\n'
    'model = "opus"\nmodels = ["opus", "sonnet", "haiku"]\n'
    'efforts = ["low", "medium", "high"]\n'
)

#: The same route with no catalogue: it takes any model of its own kind.
_OPEN_CLAUDE = _CLAUDE.replace('models = ["opus", "sonnet", "haiku"]\n', "")

#: The claude-kind `gpt` route: the Claude client through a local proxy.
_GPT = (
    '[[provider]]\nname = "gpt"\nkind = "claude"\n'
    'secrets = "/never-read/secrets"\nstrength = 1\n'
    'model = "gpt-5.6-terra"\nmodels = ["gpt-5.6-terra", "gpt-6.1-sol"]\n'
    "[provider.env]\n"
    'ANTHROPIC_BASE_URL = "http://127.0.0.1:1"\n'
)

#: `unclassified` is required beside any class (`owner_roster.py:1632`).
_UNCLASSIFIED = 'unclassified = { effort = "low", ceiling = "medium" }\n'


@pytest.fixture(autouse=True)
def _no_catalogue_binary(claude_models: dict[str, str]) -> None:
    """No pick in this module starts a Claude binary to read its catalogue."""


def _claude(**fields: Any) -> Provider:
    return Provider(
        **{
            "name": "claude",
            "default": True,
            "strength": 9,
            "model": "opus",
            "models": ["opus", "sonnet", "haiku"],
            **fields,
        }
    )


def _gpt() -> Provider:
    return Provider(
        name="gpt",
        kind="claude",
        strength=1,
        model="gpt-5.6-terra",
        models=["gpt-5.6-terra", "gpt-6.1-sol"],
        env={"ANTHROPIC_BASE_URL": "http://127.0.0.1:1"},
    )


def _start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    routes: str,
    arguments: dict[str, object],
    tool: str = "nspawn_run_start",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """`tool` through the real supervisor to the real picker.

    Each pick records what admission asked and what the picker answered, then is
    refused, so no run directory, row or child ever exists.
    """
    root = tmp_path / "plugin"
    (root / "agents").mkdir(parents=True)
    for name, text in _AGENTS.items():
        (root / "agents" / f"{name}.md").write_text(text, encoding="utf-8")
    roster = tmp_path / "spawn.toml"
    roster.write_text(f'[defaults]\nplugins = ["{root}"]\n' + routes, encoding="utf-8")
    monkeypatch.setenv("NOTT_NSPAWN_ROSTER", str(roster))
    monkeypatch.setenv("NOTT_NSPAWN_CONFIG", str(tmp_path / "absent-spawn.toml"))
    picks: list[dict[str, Any]] = []
    real = picker.pick_execution

    def spy(*args: Any, **kwargs: Any) -> NoReturn:
        pick: dict[str, Any] = {"asked": kwargs}
        picks.append(pick)
        pick["picked"] = real(*args, **kwargs)
        raise picker.PickerError("RECORDED")

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("DISPATCH_ESCAPED")

    monkeypatch.setattr(picker, "pick_execution", spy)
    monkeypatch.setattr(supervisor, "create_run_directory", forbidden)
    # The routes' secrets are never read: every credential reads as present.
    monkeypatch.setattr(supervisor.child, "credential_refusal", lambda *a: None)
    instance = object.__new__(supervisor.Supervisor)
    instance._lock = RLock()
    instance._run_root = tmp_path
    instance._registry = SimpleNamespace(
        get_provider_walls=lambda now: {},
        get_account_walls=lambda now: {},
        get_account_load=dict,
        get_account_usage=tuple,
        get_account_last_run=dict,
    )
    shape: dict[str, object] = (
        {"prompt": "never execute", "read_only": True}
        if tool == "nspawn_run_start"
        else {"branch": "main"}
    )
    answer = handle(
        mcp.McpServer(supervisor=instance),
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": tool,
                "arguments": {"cwd": str(tmp_path), **shape, **arguments},
            },
        },
    )
    return picks, answer


def test_an_agent_with_no_pin_runs_on_its_suggested_model_and_effort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    picks, answer = _start(
        tmp_path,
        monkeypatch,
        _BINS + _CLAUDE,
        {"class": IMPLEMENTATION, "agent": "explorer"},
    )
    assert picks, answer
    picked = picks[0]["picked"]
    assert (picked.executed_provider.name, picked.model, picked.effort) == (
        "claude",
        "haiku",
        "low",
    )


def test_an_alias_suggestion_runs_on_its_family_id_on_a_full_id_catalog(
    tmp_path: Path,
) -> None:
    # The host's secrets directory is never listed.
    text = FIXTURE.read_text(encoding="utf-8").replace(
        'secrets = "~/.nott/secrets/claude"', 'secrets = "/never-read/secrets"'
    )
    assert "/never-read/secrets" in text
    path = tmp_path / "spawn.toml"
    path.write_text(text, encoding="utf-8")
    picked = picker.pick_execution(
        Roster.read(path), NOW, execution_class=IMPLEMENTATION, preferred_model="haiku"
    )
    assert (picked.executed_provider.name, picked.model) == (
        "claude",
        "claude-haiku-4-5",
    )


def test_an_alias_suggestion_on_a_pinned_gpt_route_runs_the_gpt_model_with_a_warning() -> (
    None
):
    picked = picker.pick_execution(
        Roster(providers=[_claude(), _gpt()]),
        NOW,
        requested_provider="gpt",
        execution_class=IMPLEMENTATION,
        preferred_model="haiku",
    )
    assert (picked.executed_provider.name, picked.model) == ("gpt", "gpt-5.6-terra")
    assert "haiku" in (picked.warning or "")


def test_a_suggestion_runs_on_a_route_of_its_provider_that_serves_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The default Claude route has no catalogue, so it "serves" any model. Only
    # the rule "the route is the suggestion's provider" sends this run to gpt.
    picks, answer = _start(
        tmp_path,
        monkeypatch,
        _BINS + _OPEN_CLAUDE + _GPT,
        {"class": IMPLEMENTATION, "agent": "adversarial"},
    )
    assert picks, answer
    picked = picks[0]["picked"]
    assert (picked.executed_provider.name, picked.model) == ("gpt", "gpt-6.1-sol")


def test_a_suggestion_no_route_serves_is_admitted_with_a_warning_that_names_it() -> (
    None
):
    picked = picker.pick_execution(
        Roster(providers=[_claude()]),
        NOW,
        execution_class=IMPLEMENTATION,
        preferred_model="gpt-6.1-sol",
    )
    assert (picked.executed_provider.name, picked.model) == ("claude", "opus")
    assert "gpt-6.1-sol" in (picked.warning or "")


def test_with_no_serving_route_the_class_runs_its_fast_lane_with_a_warning() -> None:
    route = Provider(
        name="route",
        default=True,
        model="opus",
        model_fast="haiku",
        models=["opus", "haiku"],
        efforts=["low", "medium"],
    )
    roster = Roster(
        providers=[route],
        classes={
            MECHANICAL_FANOUT: ClassPolicy(effort="low", ceiling="medium", fast=True)
        },
    )
    picked = picker.pick_execution(
        roster, NOW, execution_class=MECHANICAL_FANOUT, preferred_model="gpt-6.1-sol"
    )
    assert (picked.model, picked.effort) == ("haiku", "low")
    assert "gpt-6.1-sol" in (picked.warning or "")


def test_a_review_tries_the_route_that_serves_the_suggestion_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    picks, answer = _start(
        tmp_path,
        monkeypatch,
        _BINS + _CLAUDE + _GPT,
        {"class": REVIEW, "agent": "adversarial"},
    )
    assert picks, answer
    first = picks[0]
    assert first["asked"]["requested_provider"] == "gpt"
    assert first["picked"].model == "gpt-6.1-sol"


def test_the_review_verb_tries_the_route_that_serves_the_suggestion_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    picks, answer = _start(
        tmp_path,
        monkeypatch,
        _BINS + _CLAUDE + _GPT,
        {"agent": "adversarial"},
        tool="nspawn_review_start",
    )
    assert picks, answer
    first = picks[0]
    assert first["asked"]["requested_provider"] == "gpt"
    assert (first["picked"].model, first["picked"].effort) == ("gpt-6.1-sol", "max")


def test_a_pinned_model_wins_over_the_agents_suggestion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    picks, answer = _start(
        tmp_path,
        monkeypatch,
        _BINS + _CLAUDE,
        {"class": IMPLEMENTATION, "agent": "explorer", "model": "opus"},
    )
    assert picks, answer
    assert picks[0]["picked"].model == "opus"


def test_a_judgment_class_gives_way_from_a_suggestion_to_the_class_tier() -> None:
    picked = picker.pick_execution(
        Roster(providers=[_claude()]),
        NOW,
        execution_class=JUDGMENT,
        preferred_model="sonnet",
    )
    assert picked.model == "opus"


def test_a_suggested_effort_the_route_does_not_serve_runs_the_nearest_served_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    picks, answer = _start(
        tmp_path,
        monkeypatch,
        _BINS + _CLAUDE,
        {"class": IMPLEMENTATION, "agent": "planner"},
    )
    assert picks, answer
    picked = picks[0]["picked"]
    assert picked.effort == "high"
    assert "max" in (picked.warning or "")


def test_a_suggested_effort_above_the_class_ceiling_runs_at_the_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The declared class equals the autouse System1 answer
    # (`class_dispatch.py:56-61`), so the class stays `mechanical_fanout`.
    routes = (
        "[classes]\n"
        'mechanical_fanout = { effort = "low", ceiling = "medium" }\n'
        + _UNCLASSIFIED
        + _BINS
        + _CLAUDE.replace(
            'efforts = ["low", "medium", "high"]',
            'efforts = ["low", "medium", "high", "max"]',
        )
    )
    picks, answer = _start(
        tmp_path,
        monkeypatch,
        routes,
        {"class": MECHANICAL_FANOUT, "agent": "planner"},
    )
    assert picks, answer
    picked = picks[0]["picked"]
    assert picked.effort == "medium"
    assert "max" in (picked.warning or "")


def test_a_pinned_effort_the_route_does_not_serve_is_still_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, answer = _start(
        tmp_path,
        monkeypatch,
        _BINS + _CLAUDE,
        {"class": IMPLEMENTATION, "agent": "explorer", "effort": "max"},
    )
    assert '"max" is not one of them' in answer["error"]["message"]


def test_an_agentless_fast_class_start_runs_on_model_fast_at_the_class_effort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    routes = (
        "[classes]\n"
        'mechanical_fanout = { effort = "low", ceiling = "medium", fast = true }\n'
        + _UNCLASSIFIED
        + _BINS
        + _CLAUDE.replace(
            'efforts = ["low", "medium", "high"]\n',
            'efforts = ["low", "medium"]\nmodel_fast = "haiku"\n',
        )
    )
    picks, answer = _start(tmp_path, monkeypatch, routes, {"class": MECHANICAL_FANOUT})
    assert picks, answer
    picked = picks[0]["picked"]
    assert (picked.model, picked.effort) == ("haiku", "low")


def test_the_run_records_the_suggestion_and_nspawn_run_show_names_it(
    registry: Registry,
    tmp_path: Path,
    roster_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_picked: picker.PickerResult,
) -> None:
    plugin = _agent_plugin(tmp_path / "plugin")
    (plugin / "agents" / "explorer.md").write_text(
        _AGENTS["explorer"], encoding="utf-8"
    )
    sv, _ = _claude_route(
        registry, roster_home, tmp_path, monkeypatch, fake_picked, plugin=plugin
    )
    accepted = sv.execute("work", str(tmp_path), read_only=True, agent="explorer")
    _completed(sv, accepted.run_id)

    suggestion = {"provider": "claude", "model": "haiku", "effort": "low"}
    assert (sv.run_agent(accepted.run_id) or {}).get("suggestion") == suggestion
    shown = _shown(mcp.McpServer(supervisor=sv), accepted.run_id)
    assert shown.get("suggestion") == suggestion


def test_a_full_id_preference_on_a_family_route_still_runs_as_the_ping_asks() -> None:
    # The ping prefers a route's own fast model. A full Claude ID that the live
    # catalogue maps to a declared family (`claude_models`) is served, as
    # `_validate_model` serves it, with no warning.
    picked = picker.pick_execution(
        Roster(providers=[_claude()]),
        NOW,
        requested_provider="claude",
        preferred_model="claude-haiku-5-5",
    )
    assert (picked.model, picked.warning) == ("claude-haiku-5-5", None)


HAIKU = "claude-haiku-5-5"
_FAST = {MECHANICAL_FANOUT: ClassPolicy(effort="low", ceiling="medium", fast=True)}


def _keyed_claude(**fields: Any) -> Provider:
    return Provider(
        **{
            "name": "claude",
            "kind": "claude",
            "default": True,
            "model": "claude-opus-5-5",
            "model_fast": HAIKU,
            "models": ["claude-opus-5-5", "claude-sonnet-5", HAIKU],
            "accounts": {
                "apikey": NativeAccount(home=None, auth_env="ANTHROPIC_API_KEY"),
                "oauth": NativeAccount(home=None, auth_env="CLAUDE_CODE_OAUTH_TOKEN"),
            },
            **fields,
        }
    )


@pytest.mark.parametrize(
    ("pin", "provider", "walls"),
    [
        # A pinned start while the OAuth account is walled.
        ("claude", _keyed_claude(), {"oauth": NOW + 600}),
        # A pinned key account.
        ("apikey", _keyed_claude(), {}),
        # A failover step onto the key account (`_without_credentials` keeps the
        # route's other accounts off).
        ("claude", _keyed_claude(excluded_accounts=["oauth"]), {}),
    ],
    ids=["pinned-route", "pinned-account", "failover-step"],
)
def test_a_suggestion_runs_on_the_key_account_of_the_pinned_route(
    pin: str, provider: Provider, walls: dict[str, int]
) -> None:
    picked = picker.pick_execution(
        Roster(providers=[provider], classes=_FAST),
        NOW,
        requested_provider=pin,
        execution_class=MECHANICAL_FANOUT,
        preferred_provider="claude",
        preferred_model="sonnet",
        account_walls=walls,
    )
    assert (picked.executed_account, picked.model) == ("apikey", "claude-sonnet-5")
    assert '"sonnet"' not in (picked.warning or "")


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("nspawn_review_start", {}),
        ("nspawn_run_start", {"class": REVIEW}),
    ],
    ids=["review-verb", "review-class"],
)
def test_a_review_with_a_pinned_model_keeps_the_owners_preference_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tool: str,
    arguments: dict[str, object],
) -> None:
    claude_a = _CLAUDE.replace('name = "claude"', 'name = "claude-a"')
    claude_b = (
        _CLAUDE.replace('name = "claude"', 'name = "claude-b"')
        .replace("strength = 9", "strength = 5")
        .replace("default = true\n", "")
    )
    picks, answer = _start(
        tmp_path,
        monkeypatch,
        _BINS + claude_a + claude_b + '[preference]\nreview = ["claude-b"]\n',
        {"agent": "explorer", "model": "sonnet", **arguments},
        tool=tool,
    )
    assert picks, answer
    assert picks[0]["asked"]["requested_provider"] == "claude-b"
    assert picks[0]["picked"].model == "sonnet"


def test_a_codex_route_never_serves_a_suggestion() -> None:
    # The class's own route is `claude`: only the suggestion could send it on.
    codex = Provider(name="codex", kind="codex", strength=1, model="gpt-6.1-sol")
    picked = picker.pick_execution(
        Roster(providers=[_claude(), codex]),
        NOW,
        execution_class=IMPLEMENTATION,
        preferred_provider="codex",
        preferred_model="gpt-6.1-sol",
    )
    assert picked.executed_provider.name == "claude"
    assert "gpt-6.1-sol" in (picked.warning or "")


def test_a_suggested_effort_codex_rewrites_is_not_recorded() -> None:
    codex = Provider(
        name="codex",
        kind="codex",
        default=True,
        model="gpt-6.1-sol",
        efforts=["low", "medium", "high", "ultra"],
    )
    picked = picker.pick_execution(
        Roster(providers=[codex]), NOW, preferred_effort="ultra"
    )
    assert picked.effort != "ultra"
    assert '"ultra"' in (picked.warning or "")


def test_a_walled_account_that_runs_the_suggestion_stays_a_wall() -> None:
    # The suggestion runs on `apikey` once its wall lifts. The answer is the
    # wall and its reset.
    claude = Provider(
        name="claude",
        model="claude-opus-5-5",
        models=["claude-opus-5-5", HAIKU],
        accounts={"apikey": NativeAccount(home=None, auth_env="ANTHROPIC_API_KEY")},
    )
    with pytest.raises(picker.AllProvidersWalled) as walled:
        picker.pick_execution(
            Roster(providers=[claude]),
            NOW,
            requested_provider="claude",
            preferred_provider="claude",
            preferred_model="haiku",
            account_walls={"apikey": NOW + 600},
        )
    assert walled.value.nearest_reset == NOW + 600


def _rejected() -> terminal.TerminalClassification:
    return terminal.TerminalClassification(
        phase=RunPhase.FAILED,
        final_text=None,
        warning="terminal result did not complete (api_error)",
        marker=None,
        provider_wall=None,
        account_wall=None,
        api_error_status=401,
    )


def test_a_credential_move_keeps_the_suggestion_and_records_what_ran(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    roster = Roster(
        providers=[_keyed_claude()],
        classes=_FAST,
        defaults=Defaults(plugin_root=tmp_path),
    )
    picked = picker.pick_execution(
        roster,
        NOW,
        execution_class=MECHANICAL_FANOUT,
        preferred_provider="claude",
        preferred_model="sonnet",
    )
    assert (picked.executed_account, picked.model) == ("apikey", "claude-sonnet-5")
    _wire_l8_and_l3(monkeypatch, lambda *a, **k: FakeSpawnedChild(_spawn_real("true")))
    completed = terminal.TerminalClassification(
        phase=RunPhase.COMPLETED,
        final_text="done",
        warning=None,
        marker=None,
        provider_wall=None,
        account_wall=None,
    )
    verdicts = [_rejected(), completed]
    monkeypatch.setattr(
        supervisor.terminal, "classify_terminal_run", lambda *a, **k: verdicts.pop(0)
    )
    sv = _sv(registry, tmp_path)
    run_id, thread_id = _admit_pending(sv, registry, tmp_path, worktree=str(tmp_path))
    run_dir = supervisor.RunDirectory(
        root=sv._run_root_path / run_id,
        stdout=sv._run_root_path / run_id / "stdout",
        stderr=sv._run_root_path / run_id / "stderr",
    )
    try:
        sv._drive_run(
            run_id,
            str(tmp_path),
            run_dir,
            "work",
            roster,
            picked,
            thread_id,
            False,
            True,
            execution_class=MECHANICAL_FANOUT,
        )
    finally:
        sv.shutdown()
    row = registry.get_run(run_id)
    assert row is not None
    assert (row.phase, row.executed_account, row.model) == (
        RunPhase.COMPLETED,
        "oauth",
        "claude-sonnet-5",
    )
    assert '"sonnet"' not in (row.warning or "")


def test_a_pinned_route_reports_the_first_account_reset() -> None:
    # Both accounts run sonnet: apikey's wall lifts at +300, oauth's at +10800.
    def pick(now: int) -> picker.PickerResult:
        return picker.pick_execution(
            Roster(providers=[_keyed_claude()], classes=_FAST),
            now,
            requested_provider="claude",
            execution_class=MECHANICAL_FANOUT,
            preferred_provider="claude",
            preferred_model="sonnet",
            account_walls={"apikey": NOW + 300, "oauth": NOW + 10_800},
        )

    with pytest.raises(picker.AllProvidersWalled) as walled:
        pick(NOW)
    assert walled.value.nearest_reset == NOW + 300
    later = pick(NOW + 301)
    assert (later.executed_account, later.model) == ("apikey", "claude-sonnet-5")


def test_an_unpinned_pick_reports_the_first_account_reset() -> None:
    # Both accounts run haiku: apikey's wall lifts at +300, oauth's at +10800.
    def pick(now: int) -> picker.PickerResult:
        return picker.pick_execution(
            Roster(providers=[_keyed_claude()]),
            now,
            execution_class=IMPLEMENTATION,
            preferred_provider="claude",
            preferred_model="haiku",
            account_walls={"apikey": NOW + 300, "oauth": NOW + 10_800},
        )

    with pytest.raises(picker.AllProvidersWalled) as walled:
        pick(NOW)
    assert walled.value.nearest_reset == NOW + 300
    later = pick(NOW + 301)
    assert (later.executed_account, later.model) == ("apikey", HAIKU)


@pytest.mark.parametrize(
    ("preferred", "account_walls"),
    [
        # One account is walled until +300 and the other is open: the route's
        # own wall decides, with a suggestion or without one.
        ("haiku", {"apikey": NOW + 300}),
        (None, {"oauth": NOW + 300}),
    ],
    ids=["suggestion", "no-suggestion"],
)
def test_a_walled_route_reports_its_own_reset_over_a_sooner_account_wall(
    preferred: str | None, account_walls: dict[str, int]
) -> None:
    def pick(now: int) -> picker.PickerResult:
        return picker.pick_execution(
            Roster(providers=[_keyed_claude()]),
            now,
            execution_class=IMPLEMENTATION,
            preferred_provider=None if preferred is None else "claude",
            preferred_model=preferred,
            provider_walls={"claude": NOW + 3600},
            account_walls=account_walls,
        )

    with pytest.raises(picker.AllProvidersWalled) as walled:
        pick(NOW)
    assert walled.value.nearest_reset == NOW + 3600
    with pytest.raises(picker.AllProvidersWalled):
        pick(NOW + 301)
    assert pick(NOW + 3601).executed_provider.name == "claude"
