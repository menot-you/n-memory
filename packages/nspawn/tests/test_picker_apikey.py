"""The key accounts leave
eligibility for any other model, so the existing fallbacks move the run on.
"""

from __future__ import annotations

import pytest

from nspawn.owner_roster import ClassPolicy, Defaults, NativeAccount, Provider, Roster
from nspawn.picker import AllProvidersWalled, PickerError, pick_execution
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
    )
    # The key account is declared first, so with no load the spread takes it.
    claude.accounts = accounts or {
        "apikey": NativeAccount(home=None, auth_env=KEY),
        "oauth": NativeAccount(home=None, auth_env=OAUTH),
    }
    return claude


def _roster(*providers: Provider) -> Roster:
    return Roster(defaults=Defaults(), providers=list(providers) or [_claude()])


@pytest.mark.parametrize("provider", ["claude", None])
@pytest.mark.parametrize("model", [OPUS, None, "opus", FABLE, SONNET])
def test_an_unpinned_non_haiku_run_never_lands_on_the_key(
    provider: str | None, model: str | None
) -> None:
    result = pick_execution(
        _roster(), NOW, requested_provider=provider, requested_model=model
    )
    assert result.executed_account == "oauth"


@pytest.mark.parametrize("model", [OPUS, None])
def test_an_unpinned_opus_run_moves_to_the_next_route_when_oauth_is_walled(
    model: str | None,
) -> None:
    other = Provider(name="other", kind="claude", model=OPUS, models=[OPUS])
    other.accounts = {"other-oauth": NativeAccount(home=None, auth_env=OAUTH)}
    result = pick_execution(
        _roster(_claude(), other),
        NOW,
        requested_model=model,
        account_walls={"oauth": NOW + 600},
    )
    assert (result.executed_provider.name, result.executed_account) == (
        "other",
        "other-oauth",
    )


def test_a_walled_oauth_account_reports_its_own_reset_not_the_key() -> None:
    with pytest.raises(AllProvidersWalled) as walled:
        pick_execution(
            _roster(),
            NOW,
            requested_provider="claude",
            requested_model=OPUS,
            account_walls={"oauth": NOW + 600, "apikey": NOW + 60},
        )
    assert walled.value.nearest_reset == NOW + 600


@pytest.mark.parametrize("model", [OPUS, "opus", FABLE, SONNET])
def test_a_pinned_key_account_refuses_a_non_haiku_model(model: str) -> None:
    with pytest.raises(PickerError, match="sends an Anthropic API key"):
        pick_execution(
            _roster(), NOW, requested_provider="apikey", requested_model=model
        )


def test_a_pinned_key_account_refuses_its_opus_default() -> None:
    with pytest.raises(PickerError, match="sends an Anthropic API key"):
        pick_execution(_roster(), NOW, requested_provider="apikey")


@pytest.mark.parametrize("provider", ["apikey", "claude", None])
def test_haiku_still_runs_on_the_key(provider: str | None) -> None:
    result = pick_execution(
        _roster(), NOW, requested_provider=provider, requested_model=HAIKU
    )
    assert (result.executed_account, result.model) == ("apikey", HAIKU)


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
def test_a_local_endpoint_key_is_not_an_anthropic_key(provider: str | None) -> None:
    result = pick_execution(_roster(_qwen()), NOW, requested_provider=provider)
    assert (result.executed_account, result.model) == ("plain", "qwen3.8-27b")


def test_a_new_qwen_node_still_takes_its_local_endpoint_key() -> None:
    assert route_account(_qwen(), None, str) == "plain"


def test_a_new_claude_node_is_not_made_on_the_key() -> None:
    assert route_account(_claude(), None, str) == "oauth"


def test_a_new_claude_node_is_refused_when_only_the_key_is_open() -> None:
    claude = _claude()
    claude.excluded_accounts = ["oauth"]
    with pytest.raises(RouteRefused):
        route_account(claude, None, str)


def test_a_key_account_that_declares_haiku_is_still_not_a_node_account() -> None:
    claude = _claude(
        apikey=NativeAccount(home=None, auth_env=KEY, model=HAIKU),
        oauth=NativeAccount(home=None, auth_env=OAUTH),
    )
    assert route_account(claude, None, str) == "oauth"


def test_a_node_recorded_on_the_key_is_refused_at_start() -> None:
    with pytest.raises(
        RouteRefused, match="sends an Anthropic API key.*move this node"
    ):
        route_account(_claude(), "apikey", str)


def test_a_node_recorded_on_the_key_can_still_leave_it() -> None:
    assert route_account(_claude(), "apikey", str, leaving=True) == "apikey"


@pytest.mark.parametrize("provider", ["claude", None])
def test_a_pool_the_key_rule_empties_is_refused_with_no_reset(
    provider: str | None,
) -> None:
    claude = _claude()
    claude.excluded_accounts = ["oauth"]
    with pytest.raises(PickerError, match="sends an Anthropic API key"):
        pick_execution(
            _roster(claude), NOW, requested_provider=provider, requested_model=OPUS
        )


def test_the_reset_ignores_a_route_the_key_rule_dropped() -> None:
    direct = Provider(name="direct", kind="claude", model=OPUS, auth_env=KEY)
    with pytest.raises(AllProvidersWalled) as walled:
        pick_execution(
            _roster(_claude(), direct),
            NOW,
            requested_model=OPUS,
            provider_walls={"direct": NOW + 60},
            account_walls={"oauth": NOW + 600},
        )
    assert walled.value.nearest_reset == NOW + 600


def _classed(*providers: Provider, fast: bool = False) -> Roster:
    policy = ClassPolicy(effort="high", ceiling="max", fast=fast)
    return Roster(
        defaults=Defaults(model=OPUS),
        providers=list(providers),
        classes={IMPLEMENTATION: policy},
    )


def test_a_classed_opus_run_moves_to_the_next_route_when_oauth_is_walled() -> None:
    other = Provider(name="other", kind="claude", model=OPUS, models=[OPUS])
    other.accounts = {"other-oauth": NativeAccount(home=None, auth_env=OAUTH)}
    result = pick_execution(
        _classed(_claude(), other),
        NOW,
        execution_class=IMPLEMENTATION,
        account_walls={"oauth": NOW + 600},
    )
    assert result.executed_account == "other-oauth"


def _template() -> Provider:
    # The template roster's shape: family aliases, a fast lane and no `model`.
    template = _claude()
    template.model = None
    template.models = ["opus", "sonnet", "haiku"]
    template.model_fast = "haiku"
    return template


def test_the_template_roster_keeps_its_opus_default_off_the_key() -> None:
    result = pick_execution(_classed(_template()), NOW, execution_class=IMPLEMENTATION)
    assert (result.executed_account, result.model) == ("oauth", "opus")


def test_a_fast_class_runs_haiku_on_the_key() -> None:
    result = pick_execution(
        _classed(_template(), fast=True), NOW, execution_class=IMPLEMENTATION
    )
    assert (result.executed_account, result.model) == ("apikey", "haiku")


def test_the_roster_default_model_keeps_the_key_out() -> None:
    bare = _claude()
    bare.model = None
    bare.models = []
    bare.model_fast = None
    result = pick_execution(_classed(bare), NOW, requested_provider="claude")
    assert (result.executed_account, result.model) == ("oauth", OPUS)
