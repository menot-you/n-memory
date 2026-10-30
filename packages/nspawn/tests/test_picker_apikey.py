"""A spent key walls its account
(`test_credit_refusal.py`), and the picker then walks to the next account. The key accounts serve nSPAWN runs only. A canvas node stays on OAuth, so
`route_account` never gives a node a key account (`test_route_node_wall.py`
proves the same for `route show`, the move and `route wall`).
"""

from __future__ import annotations

import pytest

from nspawn.owner_roster import ClassPolicy, Defaults, NativeAccount, Provider, Roster
from nspawn.picker import pick_execution
from nspawn.ranks import IMPLEMENTATION
from nspawn.route import RouteRefused, route_account

NOW = 1_000_000
OPUS = "claude-opus-5-5"
FABLE = "claude-fable-5-1"
SONNET = "claude-sonnet-5"
HAIKU = "claude-haiku-5-5"
KEY = "ANTHROPIC_API_KEY"
OAUTH = "CLAUDE_CODE_OAUTH_TOKEN"


def _claude(**accounts: NativeAccount) -> Provider:
    claude = Provider(
        name="claude",
        kind="claude",
        model=OPUS,
        model_fast=HAIKU,
        models=[OPUS, "opus", FABLE, SONNET, HAIKU],
        default=True,
        fill_first=True,
    )
    # The key accounts are declared first, so `fill_first` takes them first.
    claude.accounts = accounts or {
        "key-a": NativeAccount(home=None, auth_env=KEY),
        "key-b": NativeAccount(home=None, auth_env=KEY),
        "oauth": NativeAccount(home=None, auth_env=OAUTH),
    }
    return claude


def _roster(*providers: Provider) -> Roster:
    return Roster(defaults=Defaults(), providers=list(providers) or [_claude()])


@pytest.mark.parametrize("provider", ["claude", None])
@pytest.mark.parametrize(
    ("model", "runs"),
    [
        (OPUS, OPUS),
        (None, OPUS),
        ("opus", "opus"),
        (FABLE, FABLE),
        (SONNET, SONNET),
        (HAIKU, HAIKU),
    ],
)
def test_a_run_takes_the_first_declared_key_account(
    provider: str | None, model: str | None, runs: str
) -> None:
    result = pick_execution(
        _roster(), NOW, requested_provider=provider, requested_model=model
    )
    assert (result.executed_account, result.model) == ("key-a", runs)


@pytest.mark.parametrize("model", [OPUS, None, SONNET])
def test_a_pinned_key_account_runs_any_model(model: str | None) -> None:
    result = pick_execution(
        _roster(), NOW, requested_provider="key-b", requested_model=model
    )
    assert (result.executed_account, result.model) == ("key-b", model or OPUS)


def test_a_walled_key_account_is_skipped_until_its_wall_ends() -> None:
    def pick(now: int) -> str | None:
        return pick_execution(
            _roster(), now, requested_model=OPUS, account_walls={"key-a": NOW + 600}
        ).executed_account

    assert pick(NOW) == "key-b"
    assert pick(NOW + 601) == "key-a"


def test_every_key_account_walled_walks_to_the_oauth_account() -> None:
    result = pick_execution(
        _roster(),
        NOW,
        requested_model=OPUS,
        account_walls={"key-a": NOW + 600, "key-b": NOW + 600},
    )
    assert result.executed_account == "oauth"


def _qwen() -> Provider:
    qwen = Provider(name="qwen", kind="claude", model="qwen3.8-27b", default=True)
    qwen.accounts = {
        "plain": NativeAccount(
            home=None, base_url="http://127.0.0.1:18021", auth_env=KEY
        ),
        "server": NativeAccount(
            home=None,
            base_url="http://server.invalid:11434",
            auth_env="ANTHROPIC_AUTH_TOKEN",
        ),
    }
    return qwen


@pytest.mark.parametrize("provider", ["plain", "qwen", None])
def test_a_local_endpoint_key_still_runs_its_own_model(provider: str | None) -> None:
    result = pick_execution(_roster(_qwen()), NOW, requested_provider=provider)
    assert (result.executed_account, result.model) == ("plain", "qwen3.8-27b")


def test_a_new_qwen_node_still_takes_its_local_endpoint_key() -> None:
    assert route_account(_qwen(), None, str) == "plain"


def test_route_account_gives_a_new_claude_node_the_oauth_account() -> None:
    # The key accounts come first under `fill_first`, but a node stays on OAuth.
    assert route_account(_claude(), None, str) == "oauth"


def test_route_account_refuses_a_new_node_when_only_key_accounts_are_open() -> None:
    claude = _claude()
    claude.excluded_accounts = ["oauth"]
    with pytest.raises(RouteRefused):
        route_account(claude, None, str)


def test_route_account_refuses_a_node_recorded_on_a_key_account() -> None:
    with pytest.raises(
        RouteRefused, match="sends an Anthropic API key.*move this node"
    ):
        route_account(_claude(), "key-b", str)


def test_route_account_lets_a_node_leave_a_key_account() -> None:
    # A wall or a switch still records on the key, so the host can move the node.
    assert route_account(_claude(), "key-b", str, leaving=True) == "key-b"


def _classed(*providers: Provider, fast: bool = False) -> Roster:
    policy = ClassPolicy(effort="high", ceiling="max", fast=fast)
    return Roster(
        defaults=Defaults(model=OPUS),
        providers=list(providers),
        classes={IMPLEMENTATION: policy},
    )


def _template() -> Provider:
    # The template roster's shape: family aliases, a fast lane and no `model`.
    template = _claude()
    template.model = None
    template.models = ["opus", "sonnet", "haiku"]
    template.model_fast = "haiku"
    return template


@pytest.mark.parametrize(("fast", "runs"), [(False, "opus"), (True, "haiku")])
def test_a_classed_run_takes_the_key_account(fast: bool, runs: str) -> None:
    result = pick_execution(
        _classed(_template(), fast=fast), NOW, execution_class=IMPLEMENTATION
    )
    assert (result.executed_account, result.model) == ("key-a", runs)


def test_the_roster_default_model_runs_on_the_key_account() -> None:
    bare = _claude()
    bare.model = None
    bare.models = []
    bare.model_fast = None
    result = pick_execution(_classed(bare), NOW, requested_provider="claude")
    assert (result.executed_account, result.model) == ("key-a", OPUS)


def test_an_oauth_only_route_keeps_its_pick() -> None:
    claude = _claude(
        first=NativeAccount(home=None, auth_env=OAUTH),
        second=NativeAccount(home=None, auth_env=OAUTH),
    )
    result = pick_execution(_roster(claude), NOW, requested_model=OPUS)
    assert (result.executed_account, result.model) == ("first", OPUS)
