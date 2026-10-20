"""L2 ``pick_execution`` — the four-step execution picker."""

from __future__ import annotations

from itertools import combinations
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from nspawn.errors import NERR_1125, NSpawnError
from nspawn.owner_roster import (
    EFFORT_LADDER,
    ClassPolicy,
    Defaults,
    Provider,
    Roster,
)
from nspawn.picker import (
    AccountUse,
    AllProvidersWalled,
    PickerError,
    PickerResult,
    _clamp_to_served,
    _class_admits,
    _is_eligible,
    _ladder_rank,
    _nearest_reset,
    _select_account,
    _serves_at_or_below,
    pick_execution,
    refuse_roster_override,
    serviceable_routes,
)
from nspawn.ranks import JUDGMENT, MECHANICAL_FANOUT, VERIFICATION
from nspawn.rows import AccountUsageRow

NOW = 1_000_000


@pytest.mark.parametrize("kind", ["codex", "agy"])
@pytest.mark.parametrize("execution_class", [None, "judgment"])
def test_read_path_capability_precedes_ranking_and_walls(kind, execution_class):
    native = Provider(
        name="native", kind=kind, default=True, strength=9, model="m", models=["m"]
    )
    claude = Provider(name="claude", strength=1, model="m", models=["m"])
    roster = Roster(providers=[native, claude])
    request = {"read_paths": ("/granted/read",), "execution_class": execution_class}
    if kind == "codex":
        assert pick_execution(roster, NOW, **request).executed_provider is native
        assert (
            pick_execution(
                roster, NOW, requested_provider="native", **request
            ).executed_provider
            is native
        )
        assert (
            pick_execution(
                roster, NOW, provider_walls={"claude": NOW + 10}, **request
            ).executed_provider
            is native
        )
        return
    assert pick_execution(roster, NOW, **request).executed_provider is claude
    with pytest.raises(PickerError, match="read_paths confinement"):
        pick_execution(roster, NOW, requested_provider="native", **request)
    with pytest.raises(PickerError, match="no declared provider supports"):
        pick_execution(Roster(providers=[native]), NOW, **request)
    with pytest.raises(AllProvidersWalled) as caught:
        pick_execution(roster, NOW, provider_walls={"claude": NOW + 10}, **request)
    assert caught.value.nearest_reset == NOW + 10


def _provider(
    name: str,
    *,
    strength: int | None = None,
    default: bool = False,
    models: list[str] | None = None,
    model: str | None = None,
    efforts: list[str] | None = None,
    effort: str | None = None,
    env: dict[str, str] | None = None,
) -> Provider:
    return Provider(
        name=name,
        strength=strength,
        default=default,
        models=models or [],
        model=model,
        efforts=efforts or [],
        effort=effort,
        env=env or {},
    )


def _with_accounts(provider: Provider, tmp_path: Path, *aliases: str) -> Provider:
    secrets = tmp_path / f"secrets-{provider.name}"
    secrets.mkdir()
    for alias in aliases:
        (secrets / f"{alias}.token").write_text("x")
    provider.secrets = secrets
    return provider


def _roster(*providers: Provider, defaults: Defaults | None = None) -> Roster:
    return Roster(defaults=defaults or Defaults(), providers=list(providers))


# --- explicit provider ---------------------------------------------------


def test_explicit_provider_by_name_is_selected() -> None:
    provider = _provider("solo", model="opus")
    result = pick_execution(_roster(provider), NOW, requested_provider="solo")
    assert result.executed_provider is provider
    assert result.requested_provider == "solo"
    assert result.executed_account is None
    assert result.warning is None


def test_unknown_explicit_provider_is_refused() -> None:
    with pytest.raises(PickerError) as excinfo:
        pick_execution(
            _roster(_provider("solo", model="x")), NOW, requested_provider="ghost"
        )
    assert excinfo.value.message == (
        'no Claude provider or account named "ghost" is declared in the '
        "roster; it declares [solo]"
    )


def test_the_refusal_names_what_the_roster_actually_declares(tmp_path: Path) -> None:
    """A refusal that hides the vocabulary sends the caller to read the roster.

    Measured in real use today, twice: an agent asked for `provider="codex"`, the
    roster declares `codex1`/`codex2`, and the refusal did not say so. The next thing
    it did was `grep` `~/.nott/spawn.toml` — burning context to
    discover what the refusal itself already knew. `unknown_model` and `unknown_effort`
    list what exists three lines below in this same class; the provider one was
    the only one that skipped it, and it is the first one a caller gets wrong.
    """
    roster = _roster(
        _with_accounts(_provider("codex1", model="x"), tmp_path, "primary"),
        _provider("codex2", model="x"),
    )
    with pytest.raises(PickerError) as excinfo:
        pick_execution(roster, NOW, requested_provider="codex")
    message = excinfo.value.message
    assert 'named "codex"' in message
    for declared in ("codex1", "codex2", "primary"):
        assert declared in message, f"a recusa nao nomeia {declared}"


def test_explicit_walled_provider_does_not_reroute() -> None:
    walled = _provider("walled", model="x")
    other = _provider("other", model="y")
    with pytest.raises(AllProvidersWalled) as excinfo:
        pick_execution(
            _roster(walled, other),
            NOW,
            requested_provider="walled",
            provider_walls={"walled": NOW + 100},
        )
    assert excinfo.value.nearest_reset == NOW + 100
    assert '"walled"' in str(excinfo.value)


def test_provider_wall_exactly_at_now_does_not_block() -> None:
    provider = _provider("edge", model="x")
    result = pick_execution(
        _roster(provider),
        NOW,
        requested_provider="edge",
        provider_walls={"edge": NOW},
    )
    assert result.executed_provider is provider


# --- account selection -----------------------------------------------


def test_first_unwalled_discovered_account_is_selected(tmp_path: Path) -> None:
    provider = _with_accounts(_provider("acct", model="m"), tmp_path, "alice", "bob")
    result = pick_execution(_roster(provider), NOW, requested_provider="acct")
    assert result.executed_account == "alice"


def test_walled_account_is_skipped_for_the_next_unwalled_one(tmp_path: Path) -> None:
    provider = _with_accounts(_provider("acct", model="m"), tmp_path, "alice", "bob")
    result = pick_execution(
        _roster(provider),
        NOW,
        requested_provider="acct",
        account_walls={"alice": NOW + 100},
    )
    assert result.executed_account == "bob"


def test_every_account_walled_refuses_without_switching_provider(
    tmp_path: Path,
) -> None:
    provider = _with_accounts(_provider("acct", model="m"), tmp_path, "alice")
    other = _provider("other", model="m")
    with pytest.raises(AllProvidersWalled) as excinfo:
        pick_execution(
            _roster(provider, other),
            NOW,
            requested_provider="acct",
            account_walls={"alice": NOW + 50},
        )
    assert excinfo.value.nearest_reset == NOW + 50
    assert '"acct"' in str(excinfo.value)


def test_requesting_an_account_alias_pins_that_exact_account(tmp_path: Path) -> None:
    provider = _with_accounts(_provider("acct", model="m"), tmp_path, "alice", "bob")
    result = pick_execution(_roster(provider), NOW, requested_provider="bob")
    assert result.executed_provider is provider
    assert result.executed_account == "bob"


def test_requesting_a_walled_account_alias_does_not_reroute(tmp_path: Path) -> None:
    provider = _with_accounts(_provider("acct", model="m"), tmp_path, "alice", "bob")
    with pytest.raises(AllProvidersWalled) as excinfo:
        pick_execution(
            _roster(provider),
            NOW,
            requested_provider="bob",
            account_walls={"bob": NOW + 30, "alice": NOW + 999},
        )
    assert excinfo.value.nearest_reset == NOW + 30
    assert '"bob"' in str(excinfo.value)


# --- model resolution ------------------------------------------------


def test_explicit_model_validated_against_declared_models() -> None:
    provider = _provider("p", models=["haiku", "opus"])
    result = pick_execution(
        _roster(provider), NOW, requested_provider="p", requested_model="opus"
    )
    assert result.model == "opus"


def test_explicit_model_outside_declared_models_is_refused() -> None:
    provider = _provider("p", models=["haiku"])
    with pytest.raises(PickerError) as excinfo:
        pick_execution(
            _roster(provider), NOW, requested_provider="p", requested_model="opus"
        )
    assert excinfo.value.message == (
        'provider "p" declares models [haiku]; "opus" is not one of them'
    )


def test_a_recorded_claude_id_resumes_on_its_declared_family_alias(
    claude_models: dict[str, str],
) -> None:
    """A run recorded on `claude-opus-5-5` resumes with that model after the roster
    moves to aliases; an id of an undeclared family is still refused."""
    provider = _provider("p", models=["opus[1m]", "haiku"])
    roster = _roster(provider)
    result = pick_execution(
        roster, NOW, requested_provider="p", requested_model="claude-opus-5-5"
    )
    assert result.model == "claude-opus-5-5"
    roster.refuse_unservable_spawn("p", "claude-opus-5-5")
    with pytest.raises(PickerError):
        pick_execution(
            roster, NOW, requested_provider="p", requested_model="claude-sonnet-5-5"
        )
    with pytest.raises(NSpawnError):
        roster.refuse_unservable_spawn("p", "claude-sonnet-5-5")


@pytest.mark.parametrize("kind", ["codex", "agy"])
def test_claude_family_names_do_not_expand_other_vendor_model_lists(kind: str) -> None:
    provider = Provider(name="p", kind=kind, models=["haiku"])
    assert not provider.serves("claude-haiku-5-5")


@pytest.mark.parametrize("model", ["claude-haiku-5-6", "claude-opus-5[1m]"])
def test_family_routes_refuse_unknown_ids_and_undeclared_windows(
    claude_models: dict[str, str], model: str
) -> None:
    provider = _provider("p", models=["opus", "haiku"])
    with pytest.raises(PickerError):
        pick_execution(
            _roster(provider), NOW, requested_provider="p", requested_model=model
        )


def test_a_declared_window_accepts_only_a_catalogued_full_id(
    claude_models: dict[str, str],
) -> None:
    provider = _provider("p", models=["opus[1m]"])
    assert provider.serves("claude-opus-5-5[1m]")
    assert not provider.serves("claude-opus-unknown[1m]")


def test_explicit_model_accepted_when_provider_declares_no_model_list() -> None:
    provider = _provider("p")
    result = pick_execution(
        _roster(provider), NOW, requested_provider="p", requested_model="anything"
    )
    assert result.model == "anything"


def test_model_accepted_when_provider_env_routes_off_native() -> None:
    provider = _provider(
        "solo", env={"ANTHROPIC_BASE_URL": "https://api.z.ai/api/anthropic"}
    )
    result = pick_execution(
        _roster(provider), NOW, requested_provider="solo", requested_model="anything"
    )
    assert result.model == "anything"


def test_falls_back_to_provider_model_when_none_requested() -> None:
    provider = _provider("p", model="sonnet")
    result = pick_execution(_roster(provider), NOW, requested_provider="p")
    assert result.model == "sonnet"


def test_falls_back_to_roster_default_model() -> None:
    provider = _provider("p")
    result = pick_execution(
        _roster(provider, defaults=Defaults(model="fleet-default")),
        NOW,
        requested_provider="p",
    )
    assert result.model == "fleet-default"


def test_no_model_anywhere_is_refused() -> None:
    provider = _provider("p")
    with pytest.raises(PickerError) as excinfo:
        pick_execution(_roster(provider), NOW, requested_provider="p")
    assert excinfo.value.message == (
        'provider "p" declares no model and the roster names no default model'
    )


# --- effort resolution -------------------------------------------------


def test_explicit_effort_validated_against_declared_efforts() -> None:
    provider = _provider("p", model="m", efforts=["low", "high"])
    result = pick_execution(
        _roster(provider), NOW, requested_provider="p", requested_effort="high"
    )
    assert result.effort == "high"


def test_explicit_effort_outside_declared_efforts_is_refused() -> None:
    provider = _provider("p", model="m", efforts=["low"])
    with pytest.raises(PickerError) as excinfo:
        pick_execution(
            _roster(provider), NOW, requested_provider="p", requested_effort="high"
        )
    assert excinfo.value.message == (
        'provider "p" declares efforts [low]; "high" is not one of them'
    )


def test_falls_back_to_provider_effort() -> None:
    provider = _provider("p", model="m", effort="med")
    result = pick_execution(_roster(provider), NOW, requested_provider="p")
    assert result.effort == "med"


def test_falls_back_to_roster_default_effort_when_compatible() -> None:
    provider = _provider("p", model="m", efforts=["low", "high"])
    result = pick_execution(
        _roster(provider, defaults=Defaults(effort="high")),
        NOW,
        requested_provider="p",
    )
    assert result.effort == "high"


def test_roster_default_effort_ignored_when_incompatible_with_provider_ladder() -> None:
    provider = _provider("p", model="m", efforts=["low"])
    result = pick_execution(
        _roster(provider, defaults=Defaults(effort="high")),
        NOW,
        requested_provider="p",
    )
    assert result.effort is None


def test_no_effort_anywhere_is_none_not_a_refusal() -> None:
    provider = _provider("p", model="m")
    result = pick_execution(_roster(provider), NOW, requested_provider="p")
    assert result.effort is None


# --- class-ranked selection (no explicit provider) ----------------------


def test_no_provider_and_a_class_ranks_eligible_providers(tmp_path: Path) -> None:
    weak = _provider("weak", strength=1, model="m")
    strong = _with_accounts(
        _provider("strong", strength=9, model="m"), tmp_path, "only"
    )
    result = pick_execution(
        _roster(weak, strong),
        NOW,
        execution_class=JUDGMENT,
        account_walls={"only": NOW - 1},
    )
    assert result.executed_provider is strong
    assert result.executed_account == "only"
    assert result.warning is None


def test_class_ranking_skips_walled_candidates(tmp_path: Path) -> None:
    weak = _with_accounts(_provider("weak", strength=1, model="m"), tmp_path, "only")
    strong = _provider("strong", strength=9, model="m")
    result = pick_execution(
        _roster(weak, strong),
        NOW,
        execution_class=JUDGMENT,
        provider_walls={"strong": NOW + 10},
    )
    assert result.executed_provider is weak
    assert result.executed_account == "only"


def test_class_ranking_all_candidates_walled_reports_nearest_reset(
    tmp_path: Path,
) -> None:
    a = _with_accounts(_provider("a", strength=1, model="m"), tmp_path, "only")
    b = _provider("b", strength=9, model="m")
    with pytest.raises(AllProvidersWalled) as excinfo:
        pick_execution(
            _roster(a, b),
            NOW,
            execution_class=JUDGMENT,
            provider_walls={"a": NOW + 500, "b": NOW + 10},
        )
    assert excinfo.value.nearest_reset == NOW + 10
    assert str(excinfo.value) == "every eligible Claude route is cooling down"


def test_class_ranking_passes_producer_through_to_rank_providers() -> None:
    producer = _provider("producer", strength=9, model="m")
    other = _provider("other", strength=1, model="m")
    result = pick_execution(
        _roster(producer, other),
        NOW,
        execution_class=VERIFICATION,
        producer="producer",
    )
    assert result.executed_provider is other


def test_no_provider_no_class_and_no_declared_provider_is_refused() -> None:
    with pytest.raises(PickerError) as excinfo:
        pick_execution(_roster(), NOW)
    assert excinfo.value.message == "no Claude provider is declared in the roster"


# --- default-provider fallthrough (neither provider nor class) ----------


def test_neither_provider_nor_class_uses_the_marked_default(tmp_path: Path) -> None:
    default = _with_accounts(
        _provider("default", default=True, model="m"), tmp_path, "only"
    )
    other = _provider("other", model="m")
    result = pick_execution(
        _roster(other, default), NOW, account_walls={"only": NOW - 1}
    )
    assert result.executed_provider is default
    assert result.executed_account == "only"
    assert result.warning is None


def test_neither_provider_nor_class_with_no_marked_default_uses_the_first() -> None:
    first = _provider("first", model="m")
    second = _provider("second", model="m")
    result = pick_execution(_roster(first, second), NOW)
    assert result.executed_provider is first


def test_walled_default_falls_through_to_a_ranked_eligible_provider(
    tmp_path: Path,
) -> None:
    default = _provider("default", default=True, model="m")
    fallback = _with_accounts(_provider("fallback", model="m"), tmp_path, "only")
    result = pick_execution(
        _roster(default, fallback),
        NOW,
        provider_walls={"default": NOW + 10},
        account_walls={"only": NOW - 1},
    )
    assert result.executed_provider is fallback
    assert result.executed_account == "only"
    assert result.warning == (
        'default provider "default" is cooling down; routed to "fallback"'
    )


def test_walled_default_with_no_eligible_fallback_reports_nearest_reset(
    tmp_path: Path,
) -> None:
    default = _with_accounts(
        _provider("default", default=True, model="m"), tmp_path, "only"
    )
    with pytest.raises(AllProvidersWalled) as excinfo:
        pick_execution(
            _roster(default),
            NOW,
            provider_walls={"default": NOW + 42},
            account_walls={"only": NOW + 1},
        )
    assert excinfo.value.nearest_reset == NOW + 1
    assert str(excinfo.value) == "every eligible Claude route is cooling down"


# --- exception construction and message content --------------------------


def test_all_providers_walled_carries_nearest_reset() -> None:
    error = AllProvidersWalled("cooling down", 12345)
    assert error.nearest_reset == 12345
    assert "cooling down" in str(error)


def test_all_providers_walled_defaults_nearest_reset_to_none() -> None:
    error = AllProvidersWalled("cooling down")
    assert error.nearest_reset is None


def test_picker_error_message_attribute_is_set() -> None:
    error = PickerError("boom")
    assert error.message == "boom"
    assert str(error) == "boom"


def test_picker_error_factory_messages_are_exact() -> None:
    assert PickerError.unknown_provider("x", ["a", "b"]).message == (
        'no Claude provider or account named "x" is declared in the '
        "roster; it declares [a, b]"
    )
    assert PickerError.unknown_model("p", "m", ["a", "b"]).message == (
        'provider "p" declares models [a, b]; "m" is not one of them'
    )
    assert PickerError.unknown_effort("p", "e", ["a", "b"]).message == (
        'provider "p" declares efforts [a, b]; "e" is not one of them'
    )
    assert PickerError.no_default_model("p").message == (
        'provider "p" declares no model and the roster names no default model'
    )
    assert PickerError.no_eligible_provider().message == (
        "no Claude provider is declared in the roster"
    )


def test_picker_result_is_frozen() -> None:
    provider = _provider("p", model="m")
    result = pick_execution(_roster(provider), NOW, requested_provider="p")
    assert isinstance(result, PickerResult)
    with pytest.raises(AttributeError):
        result.model = "other"  # type: ignore[misc]


# --- private helpers: boundary and plumbing precision --------------------


def test_nearest_reset_boundary_at_now_for_provider_wall() -> None:
    p = _provider("p", model="m")
    assert _nearest_reset([p], {"p": NOW}, {}, NOW) is None
    assert _nearest_reset([p], {"p": NOW + 1}, {}, NOW) == NOW + 1


def test_nearest_reset_boundary_at_now_for_account_wall(tmp_path: Path) -> None:
    p = _with_accounts(_provider("p", model="m"), tmp_path, "only")
    assert _nearest_reset([p], {}, {"only": NOW}, NOW) is None
    assert _nearest_reset([p], {}, {"only": NOW + 1}, NOW) == NOW + 1


def test_is_eligible_boundary_at_now_for_provider_wall() -> None:
    p = _provider("p", model="m")
    assert _is_eligible(p, {"p": NOW}, {}, NOW) is True
    assert _is_eligible(p, {"p": NOW + 1}, {}, NOW) is False


def test_is_eligible_reads_the_given_account_walls_and_now(tmp_path: Path) -> None:
    p = _with_accounts(_provider("p", model="m"), tmp_path, "only")
    assert _is_eligible(p, {}, {"only": NOW + 1}, NOW) is False
    assert _is_eligible(p, {}, {"only": NOW}, NOW) is True


def test_select_account_pinned_alias_boundary_at_now(tmp_path: Path) -> None:
    p = _with_accounts(_provider("p", model="m"), tmp_path, "only")
    assert _select_account(p, "only", {}, {"only": NOW}, NOW) == "only"
    with pytest.raises(AllProvidersWalled) as excinfo:
        _select_account(p, "only", {}, {"only": NOW + 1}, NOW)
    assert '"only"' in str(excinfo.value)


def test_select_account_provider_wall_message_names_the_provider() -> None:
    p = _provider("p", model="m")
    with pytest.raises(AllProvidersWalled) as excinfo:
        _select_account(p, None, {"p": NOW + 1}, {}, NOW)
    assert '"p"' in str(excinfo.value)


def test_select_account_every_account_walled_message(tmp_path: Path) -> None:
    p = _with_accounts(_provider("p", model="m"), tmp_path, "only")
    with pytest.raises(AllProvidersWalled) as excinfo:
        _select_account(p, None, {}, {"only": NOW + 1}, NOW)
    assert '"p"' in str(excinfo.value)


# --- property tests -----------------------------------------------------


@given(
    walled_combo=st.sampled_from([(True, False), (False, True), (True, True)]),
    reset_offset=st.integers(min_value=1, max_value=10_000),
)
def test_property_a_future_wall_always_blocks(
    tmp_path_factory: pytest.TempPathFactory,
    walled_combo: tuple[bool, bool],
    reset_offset: int,
) -> None:
    provider_walled, account_walled = walled_combo
    secrets = tmp_path_factory.mktemp("secrets")
    (secrets / "only.token").write_text("x")
    provider = _provider("p", model="m")
    provider.secrets = secrets
    provider_walls = {"p": NOW + reset_offset} if provider_walled else {"p": NOW}
    account_walls = {"only": NOW + reset_offset} if account_walled else {"only": NOW}
    with pytest.raises(AllProvidersWalled):
        pick_execution(
            _roster(provider),
            NOW,
            requested_provider="p",
            provider_walls=provider_walls,
            account_walls=account_walls,
        )


def test_walls_exactly_at_now_never_block(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    secrets = tmp_path_factory.mktemp("secrets")
    (secrets / "only.token").write_text("x")
    provider = _provider("p", model="m")
    provider.secrets = secrets
    result = pick_execution(
        _roster(provider),
        NOW,
        requested_provider="p",
        provider_walls={"p": NOW},
        account_walls={"only": NOW},
    )
    assert result.executed_account == "only"


@given(
    strength_a=st.integers(min_value=0, max_value=255),
    strength_b=st.integers(min_value=0, max_value=255),
)
def test_property_class_ranking_always_picks_the_higher_strength(
    strength_a: int, strength_b: int
) -> None:
    a = _provider("a", strength=strength_a, model="m")
    b = _provider("b", strength=strength_b, model="m")
    result = pick_execution(_roster(a, b), NOW, execution_class=JUDGMENT)
    # Descending strength, ties broken by name — the same key rank_providers uses.
    winner = min((a, b), key=lambda p: (-(p.strength or 0), p.name))
    assert result.executed_provider.name == winner.name


def test_a_provider_that_declares_models_never_falls_back_to_a_foreign_default():
    # With no requested model, the ladder went straight to the global default and returned
    # "claude-opus-5" for agy, which serves gemini. The pair came out invalid from
    # the picker and only the vendor discovered it, already mid-run. The owner reported
    # the symptom as "didn't understand 'zai claude-opus-5'".
    provider = _provider("agy", models=["gemini-3.8-flash-high", "gpt-oss-120b-medium"])
    roster = _roster(provider, defaults=Defaults(model="claude-opus-5"))
    result = pick_execution(roster, NOW, requested_provider="agy")
    assert result.model == "gemini-3.8-flash-high"


def test_the_global_default_still_serves_a_provider_that_declares_nothing():
    provider = _provider("qualquer")
    roster = _roster(provider, defaults=Defaults(model="claude-opus-5"))
    result = pick_execution(roster, NOW, requested_provider="qualquer")
    assert result.model == "claude-opus-5"


def test_model_fast_is_validated_like_the_model() -> None:
    provider = _provider("p", models=["a", "b"])
    provider.model = "a"
    provider.model_fast = "ghost"
    with pytest.raises(PickerError) as excinfo:
        pick_execution(_roster(provider), NOW, requested_provider="p")
    assert "ghost" in excinfo.value.message


# --- load spreading across the accounts of one provider --------------------
#
# Three machines serve the same model and none of them speaks the same load
# language: vLLM publishes a queue at `/metrics`, ollama returns 404 there. The signal
# that exists for all is what nSPAWN already has at home — how many of ITS
# runs are alive on each account — and capacity is not equal: the 3090 eats
# several in parallel and a laptop struggles with one. That is why choice is by
# RELATIVE OCCUPANCY, never raw count.


def _loaded_provider(tmp_path: Path, **capacities: int) -> Provider:
    from nspawn.owner_roster import NativeAccount

    provider = _provider("qwen", model="m", models=["m"], default=True)
    provider.accounts = {
        alias: NativeAccount(
            home=None,
            base_url=f"http://{alias}.invalid:11434",
            capacity=capacity,
        )
        for alias, capacity in capacities.items()
    }
    return provider


def test_the_least_occupied_account_is_chosen(tmp_path: Path) -> None:
    provider = _loaded_provider(tmp_path, big=4, small=1)
    # Four live runs on a route worth four is full; one on a route worth one
    # is equally full — but three on the big one leaves room the small has not.
    picked = pick_execution(
        _roster(provider),
        NOW,
        requested_provider="qwen",
        account_load={"big": 3, "small": 0},
    )
    assert picked.executed_account == "small"

    # The case that SEPARATES relative from raw. `big` is declared after
    # `small`, and raw counts tie at 1: reading the count, or falling back to
    # declared tie-breaking, yields `small` in both. Only relative occupancy —
    # 1/4 vs 1/1 — yields `big`. Without this ordering the test passed with a picker
    # that never divided by capacity.
    invertido = _loaded_provider(tmp_path, small=1, big=4)
    picked = pick_execution(
        _roster(invertido),
        NOW,
        requested_provider="qwen",
        account_load={"big": 1, "small": 1},
    )
    assert picked.executed_account == "big"


def test_an_unknown_account_counts_as_idle(tmp_path: Path) -> None:
    """A row absent from the load map means zero, never unknown-so-skip."""
    provider = _loaded_provider(tmp_path, big=4, small=1)
    picked = pick_execution(
        _roster(provider),
        NOW,
        requested_provider="qwen",
        account_load={"big": 2},
    )
    assert picked.executed_account == "small"


def test_load_never_overrides_a_wall(tmp_path: Path) -> None:
    """Valid progress: the emptiest account is still skipped when it is walled.

    Without this, a picker that sorted purely by occupancy would pass the
    tests above while sending every run at a route that is cooling down.
    """
    provider = _loaded_provider(tmp_path, big=4, small=1)
    picked = pick_execution(
        _roster(provider),
        NOW,
        requested_provider="qwen",
        account_load={"big": 4, "small": 0},
        account_walls={"small": NOW + 600},
    )
    assert picked.executed_account == "big"


def test_a_substitute_route_that_would_refuse_the_work_is_not_named(
    tmp_path: Path,
) -> None:
    """Naming a route that will refuse is worse than naming none.

    `pick_execution` runs `request_refusal` with the run's `read_paths` and
    `system_prompt`; `serviceable_routes` did not, so a cageada review with
    `read_paths` was told to go to a route whose own launcher refuses them.
    The caller follows the advice and gets a second refusal — the supervision
    this clause exists to remove, handed back with extra steps.
    """
    agy = _provider("agy", model="g", models=["g"])
    agy.kind = "agy"
    agy.bin = tmp_path / "agy"
    agy.bin.write_text("#!/bin/sh\n")
    claude = _provider("claude", model="c", models=["c"], default=True)
    roster = _roster(claude, agy)

    sem_leituras = serviceable_routes(roster, NOW, exclude="claude")
    assert "agy" in sem_leituras

    com_leituras = serviceable_routes(
        roster, NOW, exclude="claude", read_paths=("/srv/outro",)
    )
    assert "agy" not in com_leituras


def test_the_picked_model_is_the_one_the_account_will_actually_run(
    tmp_path: Path,
) -> None:
    """What the row records has to be what the child receives.

    `build_child_env` lets `account.model` win over the provider's, because
    the same logical model has different ids at different servers — vLLM says
    `qwen3.8-27b` and ollama says `qwen3.8:27b`. If the picker does not apply
    it too, the run row names one model and the child runs another, and the
    fleet's own record of what it spent is wrong.
    """
    from nspawn.owner_roster import NativeAccount

    provider = _provider("qwen", model="qwen3.8-27b", models=["qwen3.8-27b"])
    provider.accounts = {
        "server": NativeAccount(
            home=None, base_url="http://s.invalid", model="qwen3.8-27b", capacity=4
        ),
        "m5": NativeAccount(
            home=None, base_url="http://m.invalid", model="qwen3.8:27b", capacity=2
        ),
    }
    picked = pick_execution(
        _roster(provider),
        NOW,
        requested_provider="qwen",
        account_load={"server": 4, "m5": 1},
    )
    assert picked.executed_account == "m5"
    assert picked.model == "qwen3.8:27b"


def _two_machine_qwen() -> Provider:
    from nspawn.owner_roster import NativeAccount

    provider = _provider("qwen", model="qwen3.8-27b", models=["qwen3.8-27b"])
    provider.accounts = {
        "server": NativeAccount(
            home=None, base_url="http://s.invalid", model="qwen3.8-27b", capacity=2
        ),
        "m5": NativeAccount(
            home=None, base_url="http://m.invalid", model="qwen3.8:27b", capacity=2
        ),
    }
    return provider


def test_a_pinned_model_picks_the_account_that_runs_it_over_the_emptier_one() -> None:
    """Audit 2026-10-09: eight dispatches pinned `qwen3.8-27b`, load chose m5
    (which serves it as `qwen3.8:27b`), and the pick refused. A pin selects
    among the unwalled accounts that run it; load decides only among those."""
    picked = pick_execution(
        _roster(_two_machine_qwen()),
        NOW,
        requested_provider="qwen",
        requested_model="qwen3.8-27b",
        account_load={"server": 2, "m5": 0},
    )
    assert (picked.executed_account, picked.model) == ("server", "qwen3.8-27b")


def test_a_pinned_model_runs_on_an_account_that_overrides_nothing() -> None:
    """Review round 1: `codex-main` declares no model, so it runs any model the
    provider declares; load choosing `codex-me` (which runs `luna`) must not
    make a pin of `sol` refuse."""
    picked = pick_execution(
        _roster(_two_account_codex()),
        NOW,
        requested_provider="codex",
        requested_model="sol",
        account_load={"codex-main": 1, "codex-me": 0},
    )
    assert (picked.executed_account, picked.model) == ("codex-main", "sol")


def test_a_pinned_model_no_unwalled_account_runs_is_refused_naming_what_it_saw() -> (
    None
):
    with pytest.raises(PickerError) as refused:
        pick_execution(
            _roster(_two_machine_qwen()),
            NOW,
            requested_provider="qwen",
            requested_model="qwen3.8-27b",
            account_load={"server": 0, "m5": 0},
            account_walls={"server": NOW + 60_000},
        )
    message = str(refused.value)
    assert '"qwen3.8-27b"' in message
    assert 'm5 runs "qwen3.8:27b"' in message
    assert "server" in message


def test_an_account_that_declares_no_model_keeps_the_provider_one(
    tmp_path: Path,
) -> None:
    """Valid progress: the account's id is an override, never a requirement."""
    from nspawn.owner_roster import NativeAccount

    provider = _provider("qwen", model="qwen3.8-27b", models=["qwen3.8-27b"])
    provider.accounts = {
        "server": NativeAccount(home=None, base_url="http://s.invalid", capacity=4)
    }
    picked = pick_execution(
        _roster(provider), NOW, requested_provider="qwen", account_load={}
    )
    assert picked.model == "qwen3.8-27b"


def _two_account_codex() -> Provider:
    from nspawn.owner_roster import NativeAccount

    provider = _provider("codex", model="astra", models=["astra", "luna", "sol"])
    provider.accounts = {
        "codex-main": NativeAccount(home=None, capacity=1),
        "codex-me": NativeAccount(home=None, model="luna", capacity=1),
    }
    return provider


def test_an_explicit_model_the_account_would_replace_is_refused_by_name() -> None:
    """A caller-pinned model is never swapped for the account's: the picker
    refuses before any run exists, naming the account and both models."""
    with pytest.raises(PickerError) as refused:
        pick_execution(
            _roster(_two_account_codex()),
            NOW,
            requested_provider="codex-me",
            requested_model="sol",
        )
    message = str(refused.value)
    assert "codex-me" in message
    assert '"sol"' in message and '"luna"' in message


@pytest.mark.parametrize(
    ("alias", "model", "runs"),
    [
        ("codex-main", "sol", "sol"),
        ("codex-me", None, "luna"),
        ("codex-me", "luna", "luna"),
    ],
    ids=["explicit-on-plain-account", "omitted-keeps-override", "explicit-agrees"],
)
def test_an_explicit_model_runs_where_no_account_overrides_it(
    alias: str, model: str | None, runs: str
) -> None:
    picked = pick_execution(
        _roster(_two_account_codex()),
        NOW,
        requested_provider=alias,
        requested_model=model,
    )
    assert (picked.executed_account, picked.model) == (alias, runs)


def _env_claude(**account_env: str) -> Provider:
    from nspawn.owner_roster import NativeAccount

    provider = _provider(
        "claude",
        model="strong",
        models=["strong", "small"],
        efforts=["low", "high"],
        env={"ANTHROPIC_MODEL": "small", "CLAUDE_CODE_EFFORT_LEVEL": "low"},
    )
    provider.kind = "claude"
    provider.accounts = {"a": NativeAccount(home=None, env=account_env or None)}
    return provider


@pytest.mark.parametrize(
    ("model", "effort", "account_env", "named"),
    [
        ("strong", None, {}, "ANTHROPIC_MODEL"),
        (None, "high", {}, "CLAUDE_CODE_EFFORT_LEVEL"),
        ("small", "low", {"ANTHROPIC_MODEL": "strong"}, "ANTHROPIC_MODEL"),
    ],
    ids=["provider-env-model", "provider-env-effort", "account-env-model"],
)
def test_an_explicit_profile_a_roster_env_literal_would_replace_is_refused(
    model: str | None, effort: str | None, account_env: dict[str, str], named: str
) -> None:
    """The child's env applies `[provider.env]`/`[defaults.env]` and the account's
    env AFTER the requested model and effort: a caller-pinned value there would
    run as the roster's literal instead."""
    provider = _env_claude(**account_env)
    with pytest.raises(PickerError, match=named):
        refuse_roster_override(
            _roster(provider), provider, provider.accounts["a"], model, effort
        )
    # The pick itself admits it: only a child that receives the roster's env is
    # refused, and a caged review never does.
    assert pick_execution(
        _roster(provider),
        NOW,
        requested_provider="claude",
        requested_model=model,
        requested_effort=effort,
    )


@pytest.mark.parametrize(
    ("args", "model", "effort", "named"),
    [
        (["--model", "small"], "strong", None, "--model"),
        (["--model=small"], "strong", None, "--model"),
        (["--effort", "low"], None, "high", "--effort"),
    ],
    ids=["model-flag", "model-flag-joined", "effort-flag"],
)
def test_an_explicit_profile_a_roster_argument_would_replace_is_refused(
    args: list[str], model: str | None, effort: str | None, named: str
) -> None:
    """The CLI ranks `--model`/`--effort` above the env the child gets them in."""
    provider = _env_claude()
    provider.env = {}
    provider.args = args
    with pytest.raises(PickerError, match=named):
        refuse_roster_override(_roster(provider), provider, None, model, effort)
    # Valid progress: an omitted request still takes the roster's argument.
    refuse_roster_override(_roster(provider), provider, None, None, None)


@pytest.mark.parametrize(
    ("model", "effort"),
    [(None, None), ("small", "low")],
    ids=["omitted-takes-the-roster-env", "explicit-agrees-with-it"],
)
def test_a_roster_env_literal_still_serves_a_request_it_does_not_contradict(
    model: str | None, effort: str | None
) -> None:
    picked = pick_execution(
        _roster(_env_claude()),
        NOW,
        requested_provider="claude",
        requested_model=model,
        requested_effort=effort,
    )
    assert picked.executed_account == "a"


def test_a_preferred_model_takes_the_accounts_own_id_and_the_roster_env() -> None:
    """The ping's fast lane is a preference the verb fills in, never a caller pin:
    the account's id for the same model and the roster env still win, as before."""
    from nspawn.owner_roster import NativeAccount

    provider = _provider("qwen", model="qwen3.8-27b", models=["qwen3.8-27b"])
    provider.accounts = {
        "server": NativeAccount(home=None, base_url="http://s.invalid", capacity=4),
        "m5": NativeAccount(
            home=None, base_url="http://m.invalid", model="qwen3.8:27b", capacity=1
        ),
    }
    picked = pick_execution(
        _roster(provider),
        NOW,
        requested_provider="qwen",
        preferred_model="qwen3.8-27b",
        account_load={"server": 4},
    )
    assert (picked.executed_account, picked.model) == ("m5", "qwen3.8:27b")
    env_route = _env_claude()
    assert (
        pick_execution(
            _roster(env_route),
            NOW,
            requested_provider="claude",
            preferred_model="strong",
        ).model
        == "strong"
    )


@pytest.mark.parametrize(
    ("args", "env", "variable", "refused"),
    [
        (["$NOTT_TEST_OPTION"], {}, "--model=small", True),
        (["--model", "$NOTT_TEST_OPTION"], {}, "strong", False),
        ([], {"ANTHROPIC_MODEL": "$NOTT_TEST_OPTION"}, "strong", False),
        ([], {"ANTHROPIC_MODEL": "$NOTT_TEST_OPTION"}, "small", True),
        ([], {"ANTHROPIC_MODEL": "$NOTT_TEST_UNSET"}, "small", False),
    ],
    ids=[
        "flag-from-a-variable",
        "value-that-expands-to-the-request",
        "env-that-expands-to-the-request",
        "env-that-expands-to-another",
        "env-reference-that-resolves-to-nothing",
    ],
)
def test_the_roster_is_judged_as_the_child_receives_it(
    monkeypatch: pytest.MonkeyPatch,
    args: list[str],
    env: dict[str, str],
    variable: str,
    refused: bool,
) -> None:
    """The supervisor expands the roster's args and env before the child sees
    them, and drops a reference that resolves to nothing; the refusal reads the
    same values, never the raw `$NAME`."""
    monkeypatch.setenv("NOTT_TEST_OPTION", variable)
    monkeypatch.delenv("NOTT_TEST_UNSET", raising=False)
    provider = _env_claude()
    provider.env = env
    provider.args = args
    if refused:
        with pytest.raises(PickerError):
            refuse_roster_override(_roster(provider), provider, None, "strong", None)
    else:
        refuse_roster_override(_roster(provider), provider, None, "strong", None)


def test_a_route_whose_accounts_carry_the_endpoint_never_takes_the_default(
    tmp_path: Path,
) -> None:
    """The same rule, for the shape the endpoint lives on the ACCOUNT.

    `_routes_off_native` read `provider.env` only, so a provider that declares
    nothing and routes per account inherited the fleet default — the exact
    mistake the branch above exists to stop, arriving by the other door: the
    gateway routes by model id, so `claude-opus-5` at an ollama box is either
    refused there or served by somebody else under this label.
    """
    from nspawn.owner_roster import NativeAccount

    provider = _provider("qwen")
    provider.accounts = {
        "server": NativeAccount(home=None, base_url="http://s.invalid", capacity=4)
    }
    with pytest.raises(PickerError, match="qwen"):
        pick_execution(
            _roster(provider, defaults=Defaults(model="claude-opus-5")),
            NOW,
            requested_provider="qwen",
            account_load={},
        )


def test_a_tie_breaks_on_the_declared_order_not_the_alphabet(
    tmp_path: Path,
) -> None:
    """The real roster declares `server` first and `m5` sorts first.

    Every other fixture in this file names its accounts so that the alphabet
    and the declaration agree (`big` before `small`, `acct-a` before
    `acct-b`), which is exactly why nothing here noticed that the tie was
    breaking on `sorted()`. With the two qwen machines idle both occupancies
    are 0.0, and the alphabet sends every run at the laptop while the 3090
    sits empty.
    """
    provider = _loaded_provider(tmp_path)
    from nspawn.owner_roster import NativeAccount

    # Declared order: the 3090 first, as the owner wrote it.
    provider.accounts = {
        "server": NativeAccount(home=None, base_url="http://s.invalid", capacity=4),
        "m5": NativeAccount(home=None, base_url="http://m.invalid", capacity=2),
    }
    picked = pick_execution(
        _roster(provider), NOW, requested_provider="qwen", account_load={}
    )
    assert picked.executed_account == "server"

    # And a REAL tie in occupancy, 2/4 against 1/2, goes the same way.
    picked = pick_execution(
        _roster(provider),
        NOW,
        requested_provider="qwen",
        account_load={"server": 2, "m5": 1},
    )
    assert picked.executed_account == "server"

    # The supervisor always passes the registry's use. An endpoint account (a
    # capacity host) has no reset clock to start, so the declared order decides
    # even when the 3090 ran last and the laptop is idle longer.
    for last_run in ({}, {"server": 2000, "m5": 1000}):
        picked = pick_execution(
            _roster(provider),
            NOW,
            requested_provider="qwen",
            account_load={},
            account_use=AccountUse((), last_run),
        )
        assert picked.executed_account == "server"


def test_the_alphabet_still_loses_when_it_disagrees_with_occupancy(
    tmp_path: Path,
) -> None:
    """Valid progress: declared order is the TIE-break, never the rule.

    Without this, a picker that simply returned the first declared account
    would pass the case above while ignoring load entirely.
    """
    from nspawn.owner_roster import NativeAccount

    provider = _loaded_provider(tmp_path)
    provider.accounts = {
        "server": NativeAccount(home=None, base_url="http://s.invalid", capacity=4),
        "m5": NativeAccount(home=None, base_url="http://m.invalid", capacity=2),
    }
    picked = pick_execution(
        _roster(provider),
        NOW,
        requested_provider="qwen",
        account_load={"server": 4, "m5": 1},
    )
    assert picked.executed_account == "m5"


def test_without_a_load_map_the_declared_order_decides(tmp_path: Path) -> None:
    """Every other route keeps exactly the behaviour it had.

    Accounts discovered from a secrets directory carry no capacity, and a
    caller that passes no load map must still get the first unwalled alias.
    """
    provider = _with_accounts(
        _provider("claude", model="m", models=["m"], default=True),
        tmp_path,
        "acct-a",
        "acct-b",
    )
    picked = pick_execution(_roster(provider), NOW, requested_provider="claude")
    assert picked.executed_account == "acct-a"


# --- the class strength ceiling: eligibility, never ranking ----------------


def _capped_roster(max_strength: int | None = 7) -> Roster:
    """`claude` wins the thrift tie on name alone; only the cap can unseat it."""
    classes = (
        {}
        if max_strength is None
        else {
            MECHANICAL_FANOUT: ClassPolicy(
                effort="low", ceiling="medium", max_strength=max_strength
            )
        }
    )
    return Roster(
        providers=[
            _provider("claude", strength=9, default=True, model="m", models=["m"]),
            _provider("glm", strength=7, model="m", models=["m"]),
        ],
        classes=classes,
    )


def test_the_cap_makes_a_strong_provider_ineligible_not_last():
    picked = pick_execution(_capped_roster(), NOW, execution_class=MECHANICAL_FANOUT)
    assert picked.executed_provider.name == "glm"


def test_strength_equal_to_the_cap_is_eligible():
    picked = pick_execution(_capped_roster(9), NOW, execution_class=MECHANICAL_FANOUT)
    assert picked.executed_provider.name == "claude"


def test_a_class_without_a_cap_still_picks_the_strong_provider():
    for roster in (_capped_roster(None), _capped_roster(255)):
        picked = pick_execution(roster, NOW, execution_class=MECHANICAL_FANOUT)
        assert picked.executed_provider.name == "claude"


def test_no_class_leaves_the_default_branch_uncapped():
    picked = pick_execution(_capped_roster(), NOW)
    assert picked.executed_provider.name == "claude"


def test_a_pinned_provider_above_the_cap_is_refused_never_rerouted():
    with pytest.raises(PickerError) as caught:
        pick_execution(
            _capped_roster(),
            NOW,
            requested_provider="claude",
            execution_class=MECHANICAL_FANOUT,
        )
    message = caught.value.message
    assert MECHANICAL_FANOUT in message
    assert "7" in message and "9" in message
    assert "glm" in message


def test_a_pool_emptied_by_the_cap_is_not_a_cooling_wall():
    roster = _capped_roster(6)
    with pytest.raises(PickerError) as caught:
        pick_execution(roster, NOW, execution_class=MECHANICAL_FANOUT)
    assert not isinstance(caught.value, AllProvidersWalled)
    assert caught.value.entry is NERR_1125


def test_a_pool_emptied_by_walls_is_still_a_cooling_wall():
    with pytest.raises(AllProvidersWalled):
        pick_execution(
            _capped_roster(),
            NOW,
            execution_class=MECHANICAL_FANOUT,
            provider_walls={"claude": NOW + 10, "glm": NOW + 10},
        )


def test_serviceable_routes_never_names_a_route_above_the_cap():
    assert serviceable_routes(
        _capped_roster(), NOW, execution_class=MECHANICAL_FANOUT
    ) == ["glm"]
    assert serviceable_routes(_capped_roster(), NOW) == ["claude", "glm"]


def test_undeclared_strength_clears_every_cap_including_zero():
    roster = Roster(
        providers=[_provider("agy", model="m", models=["m"])],
        classes={
            MECHANICAL_FANOUT: ClassPolicy(
                effort="low", ceiling="medium", max_strength=0
            )
        },
    )
    picked = pick_execution(roster, NOW, execution_class=MECHANICAL_FANOUT)
    assert picked.executed_provider.name == "agy"


def test_a_zero_cap_is_a_cap_not_an_absence():
    roster = Roster(
        providers=[_provider("glm", strength=7, model="m", models=["m"])],
        classes={
            MECHANICAL_FANOUT: ClassPolicy(
                effort="low", ceiling="medium", max_strength=0
            )
        },
    )
    with pytest.raises(PickerError) as caught:
        pick_execution(roster, NOW, execution_class=MECHANICAL_FANOUT)
    assert caught.value.entry is NERR_1125


def test_a_class_initial_the_route_undershoots_never_clamps_upward() -> None:
    """The class bought "this much"; a route with nothing under it gives LESS.

    `_clamp_to_served` reads "the highest effort the provider serves at or
    below" — and when the route serves nothing at or below, the only honest
    answer is the cheapest it does serve. Taking the HIGHEST instead inverts
    the whole axis: a class that starts LOWER on the ladder ends up running
    HIGHER, so `classify`'s guarantee that an observation can only lower the
    bill is void on exactly the rosters where it matters most. The fail-closed
    path is the worst case, because it fires with the System1 down.

    Measured by an independent review, 2026-09-21: the route serves `high` and
    `ultra`; a class starting at `low` ran at `ultra`, while the same route
    under a class starting at `max` ran at `high`.
    """
    route = Provider(
        name="route",
        strength=9,
        models=["m"],
        model="m",
        efforts=["high", "ultra"],
        effort=None,
        env={},
    )
    assert _clamp_to_served(route, "low") == "high"
    # Valid progress, unchanged: with something served at or below, the route
    # still gives the most it has under the ask.
    assert _clamp_to_served(route, "max") == "high"
    assert _clamp_to_served(route, "ultra") == "ultra"


def test_the_clamp_is_monotone_over_every_small_effort_list() -> None:
    """Asking for LESS never runs MORE, on any route the roster can declare.

    The whole guarantee of `classify` rests on this: it compares classes by
    `policy.effort`, and that comparison is only meaningful if a lower initial
    yields a lower-or-equal effort actually served. One route with an awkward
    ladder would void it silently, so this sweeps every list of up to three
    efforts — including the empty one, and values OFF the ladder, which
    `_ladder_rank` sorts past the end.
    """
    pool = [*EFFORT_LADDER, "weird", "alsoweird"]
    for size in range(4):
        for combo in combinations(pool, size):
            route = Provider(
                name="p",
                strength=1,
                models=["m"],
                model="m",
                efforts=list(combo),
                effort=None,
                env={},
            )
            served = [_ladder_rank(_clamp_to_served(route, e)) for e in EFFORT_LADDER]
            assert served == sorted(served), (combo, served)


def test_a_route_that_cannot_run_the_class_initial_is_not_eligible_for_it() -> None:
    """Undershooting the class is a refusal, not a promotion to the top rung.

    `_clamp_to_served` used to answer a class that the route cannot serve by
    handing back something it CAN serve — and when the route serves nothing at
    or below the ask, that answer is necessarily dearer than the class bought.
    Three separate escalations in this campaign trace to that one line, the
    last of them real on the owner's own roster: `agy` serves only `high` and
    up, so a `low` class dispatched there ran `high`.

    It also broke the comparison `classify` rests on. `policy.effort` is not a
    number you can hold up against another class's: it is the INPUT to a
    route-dependent function, and the strength cap decides which route. With
    the upward clamp gone, the served effort is always at or below the class
    initial on every eligible route, so comparing the initials is sound again
    whichever route each class would pick.

    Found by an independent review, 2026-09-21, with a roster where the route
    the cap leaves standing serves only `ultra`.
    """
    only_high_and_up = Provider(
        name="agy-like",
        strength=6,
        models=["m"],
        model="m",
        efforts=["high", "xhigh", "max", "ultra"],
        effort=None,
        env={},
    )
    assert not _serves_at_or_below(only_high_and_up, "low")
    assert not _serves_at_or_below(only_high_and_up, "medium")
    # Valid progress: at or above its floor it serves, and still clamps DOWN.
    assert _serves_at_or_below(only_high_and_up, "high")
    assert _clamp_to_served(only_high_and_up, "max") == "max"
    # A route with no declared ladder serves anything, as it always did.
    assert _serves_at_or_below(_provider("open", strength=1), "low")


def _ladder_gap_roster() -> Roster:
    """The owner's real shape: a mid-strength route that starts at `high`.

    `agy` declares `efforts = ["high", "xhigh", "max", "ultra"]` in the host
    roster, and `mechanical_fanout` buys `low`. Nothing else in the campaign
    stopped that pairing.
    """
    return Roster(
        path=Path("/never-read/roster.toml"),
        providers=[
            Provider(
                name="agy-like",
                strength=6,
                models=["m"],
                model="m",
                efforts=["high", "xhigh", "max", "ultra"],
            ),
            Provider(
                name="qwen-like",
                strength=5,
                models=["m"],
                model="m",
                efforts=["low", "medium", "high"],
            ),
        ],
        defaults=Defaults(),
        classes={
            "mechanical_fanout": ClassPolicy(
                effort="low", ceiling="medium", max_strength=7
            ),
            "unclassified": ClassPolicy(effort="low", ceiling="low"),
        },
    )


def test_a_route_whose_ladder_starts_above_the_class_is_not_serviceable() -> None:
    """On the route, not in the helper: the cheap class cannot name the dear route.

    Both routes pass the strength cap of 7. Only one of them can run anything
    at or below `low`, and the other would have been promoted to `high` by the
    old upward clamp — turning the cheapest class on the roster into a dearer
    dispatch than the caller asked for.
    """
    roster = _ladder_gap_roster()
    assert serviceable_routes(roster, NOW, execution_class=MECHANICAL_FANOUT) == [
        "qwen-like"
    ]
    # Valid progress: with no class governing, the ladder does not filter and
    # both routes stand, exactly as before.
    assert sorted(serviceable_routes(roster, NOW)) == ["agy-like", "qwen-like"]


def test_the_default_branch_never_lets_a_class_raise_the_effort() -> None:
    """The unfiltered branch is deliberate; measure what actually protects it.

    A dispatch naming no class still takes the `unclassified` policy for
    effort — `_class_effort` reads `execution_class or UNCLASSIFIED` — but
    this branch picks its route without `_class_admits`, by the owner's
    decision that internal dispatches (batch, ping, the credential repick) sit
    outside the cap. An independent review read that as an open escalation.

    It is not, and this leg is what says so: the only thing standing between
    the floor's `low` and a route that starts at `high` is `_clamp_to_served`
    refusing to clamp UPWARD, and the route gets its own rung — what it would
    have run before this campaign existed. The class never raises what the
    route does by itself. Flip that one `min` back to `max` and this leg goes
    RED, which is exactly the guarantee it is here to hold.

    What the review was right about is narrower: the served effort CAN exceed
    `policy.effort` here, so "never exceeds it" is not true of this branch.
    `test_the_class_initials_are_comparable_where_classify_compares_them` is
    where that premise is pinned — in the branches that have a class, which
    are the only ones `classify` is ever consulted for.
    """
    roster = Roster(
        path=Path("/never-read/roster.toml"),
        providers=[
            Provider(
                name="only-high",
                kind="claude",
                strength=9,
                models=["m"],
                model="m",
                efforts=["high", "ultra"],
                effort="high",
                default=True,
            )
        ],
        defaults=Defaults(),
        classes={"unclassified": ClassPolicy(effort="low", ceiling="low")},
    )
    chosen = pick_execution(roster, NOW)
    assert chosen.executed_provider.name == "only-high"
    # Not `ultra`, which the old upward clamp would have produced, and not a
    # refusal either: the route's own rung.
    assert chosen.effort == "high"


def test_the_class_initials_are_comparable_where_classify_compares_them() -> None:
    """The premise `classify._is_cheaper` rests on, pinned where it is used.

    `classify` compares two classes by `policy.effort`, which is sound only if
    no route either class could take runs ABOVE its own initial. `_class_admits`
    is what makes that true, and it runs on every branch that carries a class:
    the ranked pool, the pinned provider, and `serviceable_routes`. This walks
    the whole ladder against a route that starts high and confirms that every
    class it is eligible for runs at or below what it bought.
    """
    route = Provider(
        name="only-high",
        kind="claude",
        strength=1,
        models=["m"],
        model="m",
        efforts=["high", "ultra"],
        effort="high",
    )
    for initial in EFFORT_LADDER:
        policy = ClassPolicy(effort=initial, ceiling="ultra")
        if not _class_admits(route, policy):
            continue
        served = _clamp_to_served(route, policy.effort)
        assert _ladder_rank(served) <= EFFORT_LADDER.index(initial), (initial, served)
    # And the ones it refuses are exactly those it could only have served by
    # going over: everything below its own first rung.
    refused = [
        e
        for e in EFFORT_LADDER
        if not _class_admits(route, ClassPolicy(effort=e, ceiling="ultra"))
    ]
    assert refused == ["low", "medium"]


def test_an_excluded_discovered_account_is_never_picked_and_its_pin_is_refused(
    tmp_path: Path,
) -> None:
    # Accounts discovered from the secrets directory, the first one busy, so load
    # spreading takes the next one.
    claude = _with_accounts(
        _provider("claude", default=True, model="m"),
        tmp_path,
        "first",
        "forbidden",
        "other",
    )
    roster = _roster(claude)
    load = {"first": 1}
    assert _select_account(claude, None, {}, {}, NOW, load) == "forbidden"
    claude.excluded_accounts = ["forbidden"]
    assert _select_account(claude, None, {}, {}, NOW, load) == "other"
    assert pick_execution(roster, NOW, account_load=load).executed_account == "other"
    # Not even when it is the only idle account.
    busy = {"first": 1, "other": 1}
    assert pick_execution(roster, NOW, account_load=busy).executed_account == "first"
    for pinned in (
        lambda: _select_account(claude, "forbidden", {}, {}, NOW, load),
        lambda: pick_execution(roster, NOW, requested_provider="forbidden"),
    ):
        with pytest.raises(PickerError, match='"forbidden" is excluded'):
            pinned()


def _week(alias: str, utilization: float, resets_at: int) -> AccountUsageRow:
    return AccountUsageRow(alias, "seven_day", utilization, resets_at, NOW)


def test_a_spent_week_without_a_wall_is_taken_only_when_every_week_is_spent(
    tmp_path: Path,
) -> None:
    claude = _with_accounts(
        _provider("claude", default=True, model="m"), tmp_path, "other", "spent"
    )
    spent = AccountUse((_week("other", 1.0, NOW + 86400),), {})
    assert _select_account(claude, None, {}, {}, NOW, {}, spent) == "spent"
    assert (
        pick_execution(_roster(claude), NOW, account_use=spent).executed_account
        == "spent"
    )
    # Every score is 0: the least recently used account takes the run, not the
    # first one in the roster.
    both = AccountUse(
        (_week("other", 1.0, NOW + 86400), _week("spent", 1.0, NOW + 86400)),
        {"other": 2000, "spent": 1000},
    )
    assert _select_account(claude, None, {}, {}, NOW, {}, both) == "spent"


def test_another_models_codex_bucket_does_not_lower_the_weekly_score(
    tmp_path: Path,
) -> None:
    codex = _with_accounts(
        _provider("codex", default=True, models=["sol"], model="sol"),
        tmp_path,
        "first",
        "second",
    )
    week = NOW + 86400
    use = AccountUse(
        (
            # The main `codex` limit meters every model: `first` has more left.
            AccountUsageRow("first", "codex.primary.seven_day", 0.4, week, NOW),
            AccountUsageRow("second", "codex.primary.seven_day", 0.5, week, NOW),
            # The limit of one other model, nearly spent, is not the run's limit.
            AccountUsageRow("first", "codex_other.primary.seven_day", 0.95, week, NOW),
        ),
        {"first": 2000, "second": 1000},
    )
    picked = pick_execution(
        _roster(codex),
        NOW,
        requested_provider="codex",
        requested_model="sol",
        account_load={},
        account_use=use,
    )
    assert picked.executed_account == "first"


def test_a_window_past_its_reset_counts_as_empty(tmp_path: Path) -> None:
    claude = _with_accounts(
        _provider("claude", default=True, model="m"), tmp_path, "fresh", "rolled"
    )
    # A spent week that has reset is as empty as a week never observed, so the
    # account last used longest ago takes the run.
    use = AccountUse((_week("rolled", 1.0, NOW - 1),), {"fresh": 2000, "rolled": 1000})
    assert _select_account(claude, None, {}, {}, NOW, None, use) == "rolled"


def test_an_excluded_provider_does_not_supply_the_pool_reset(tmp_path: Path) -> None:
    excluded = _with_accounts(_provider("excluded"), tmp_path, "banned")
    excluded.excluded_accounts = ["banned"]
    allowed = _with_accounts(_provider("allowed"), tmp_path, "free-later")
    assert (
        _nearest_reset(
            [excluded, allowed],
            {"excluded": NOW + 60},
            {"free-later": NOW + 600},
            NOW,
        )
        == NOW + 600
    )


def test_a_mixed_route_keeps_its_explicit_full_ids() -> None:
    provider = Provider(
        name="claude",
        kind="claude",
        models=["haiku", "claude-opus-5"],
        model_catalog={"claude-opus-5": "claude-opus-5"},
    )
    assert provider.serves("claude-opus-5")
    assert not provider.serves("claude-opus-5-5")


def test_a_mixed_route_keeps_an_id_missing_from_the_catalogue() -> None:
    provider = Provider(
        name="claude",
        kind="claude",
        models=["haiku", "claude-opus-5"],
        model_catalog={},
    )
    assert provider.serves("claude-opus-5")
    assert not provider.serves("claude-haiku-5-6")


# --- a pinned model with no pinned provider -------------------------------
# With a pinned model, the picker behaves as if the roster held only the
# routes that serve it.


def _outcome(roster: Roster, **request: object) -> object:
    try:
        return pick_execution(roster, NOW, **request)  # type: ignore[arg-type]
    except AllProvidersWalled as error:
        return ("walled", str(error), error.nearest_reset)
    except PickerError as error:
        return ("refused", error.message)


def test_pinned_model_skips_a_higher_ranked_route_that_does_not_serve_it() -> None:
    strong = _provider("strong", strength=9, model="x", models=["x"])
    serving = _provider("serving", strength=1, model="m", models=["m"])
    result = pick_execution(
        _roster(strong, serving), NOW, execution_class=JUDGMENT, requested_model="m"
    )
    assert result.executed_provider is serving
    assert result.model == "m"


def test_pinned_model_wall_reset_comes_from_the_serving_route_only() -> None:
    other = _provider("other", strength=9, model="x", models=["x"])
    serving = _provider("serving", strength=1, model="m", models=["m"])
    with pytest.raises(AllProvidersWalled) as excinfo:
        pick_execution(
            _roster(other, serving),
            NOW,
            execution_class=JUDGMENT,
            requested_model="m",
            provider_walls={"other": NOW + 10, "serving": NOW + 500},
        )
    assert excinfo.value.nearest_reset == NOW + 500


def test_pinned_model_no_declaring_route_falls_to_a_route_without_catalog() -> None:
    declared = _provider("declared", strength=9, model="x", models=["x"])
    open_route = _provider("open", strength=1, model="y")
    result = pick_execution(
        _roster(declared, open_route),
        NOW,
        execution_class=JUDGMENT,
        requested_model="m",
    )
    assert result.executed_provider is open_route
    assert result.model == "m"


def test_pinned_model_no_route_serves_it_is_refused_by_name() -> None:
    a = _provider("a", model="x", models=["x"])
    b = _provider("b", model="y", models=["y", "z"])
    with pytest.raises(PickerError) as excinfo:
        pick_execution(_roster(a, b), NOW, requested_model="m")
    assert excinfo.value.message == (
        'no declared provider serves model "m"; the providers declare models [x, y, z]'
    )


def test_pinned_model_skips_a_default_route_that_does_not_serve_it() -> None:
    default = _provider("default", default=True, model="x", models=["x"])
    serving = _provider("serving", model="m", models=["m"])
    result = pick_execution(_roster(default, serving), NOW, requested_model="m")
    assert result.executed_provider is serving
    assert result.warning is None


def test_pinned_model_verification_equals_a_roster_of_the_serving_route_only() -> None:
    producer = _provider("producer", strength=9, model="m", models=["m"])
    other = _provider("other", strength=1, model="x", models=["x"])
    request = {
        "execution_class": VERIFICATION,
        "producer": "producer",
        "requested_model": "m",
    }
    oracle = _outcome(_roster(producer), **request)
    assert _outcome(_roster(producer, other), **request) == oracle


def test_no_pinned_model_keeps_the_unfiltered_choice() -> None:
    strong = _provider("strong", strength=9, model="x", models=["x"])
    weak = _provider("weak", strength=1, model="m", models=["m"])
    default = _provider("default", default=True, model="x", models=["x"])
    ranked = pick_execution(_roster(strong, weak), NOW, execution_class=JUDGMENT)
    assert ranked.executed_provider is strong
    assert ranked.model == "x"
    fallback = pick_execution(_roster(default, weak), NOW)
    assert fallback.executed_provider is default
    assert fallback.model == "x"
    # Only `weak` declares `m`. Main refuses on `strong`, so a filter that acts
    # on preferred_model (and picks `weak`) fails here.
    assert _outcome(
        _roster(strong, weak), execution_class=JUDGMENT, preferred_model="m"
    ) == ("refused", 'provider "strong" declares models [x]; "m" is not one of them')


def _account_model_qwen() -> Provider:
    """The live qwen shape: no `models`, and every account sets its own model."""
    provider = _two_machine_qwen()
    provider.models = []
    provider.strength = 9
    return provider


def test_pinned_model_an_account_model_route_is_closed_to_a_model_it_lacks() -> None:
    qwen = _account_model_qwen()
    declaring = _provider("declaring", strength=1, model="m", models=["m"])
    roster = _roster(qwen, declaring)
    result = pick_execution(roster, NOW, execution_class=JUDGMENT, requested_model="m")
    assert result.executed_provider is declaring
    assert _outcome(
        roster,
        execution_class=JUDGMENT,
        requested_model="mm",
        provider_walls={"qwen": NOW + 500},
    ) == (
        "refused",
        (
            'no declared provider serves model "mm"; '
            "the providers declare models [qwen3.8-27b, qwen3.8:27b, m]"
        ),
    )


def test_pinned_model_an_account_model_route_serves_its_account_model() -> None:
    qwen = _account_model_qwen()
    qwen.strength = 1
    open_route = _provider("open", strength=9, model="y")
    result = pick_execution(
        _roster(open_route, qwen),
        NOW,
        execution_class=JUDGMENT,
        requested_model="qwen3.8:27b",
    )
    assert (result.executed_provider, result.executed_account, result.model) == (
        qwen,
        "m5",
        "qwen3.8:27b",
    )


def test_pinned_model_an_excluded_account_adds_nothing_to_the_catalog() -> None:
    qwen = _account_model_qwen()
    qwen.strength = 1
    qwen.excluded_accounts = ["m5"]
    open_route = _provider("open", strength=9, model="y")
    result = pick_execution(
        _roster(open_route, qwen),
        NOW,
        execution_class=JUDGMENT,
        requested_model="qwen3.8:27b",
    )
    assert (result.executed_provider, result.model) == (open_route, "qwen3.8:27b")


def test_pinned_model_a_route_with_every_account_excluded_serves_nothing() -> None:
    qwen = _account_model_qwen()
    qwen.excluded_accounts = ["server", "m5"]
    declaring = _provider("declaring", strength=1, model="m", models=["m"])
    assert _outcome(
        _roster(qwen, declaring), execution_class=JUDGMENT, requested_model="mm"
    ) == (
        "refused",
        'no declared provider serves model "mm"; the providers declare models [m]',
    )
    open_route = _provider("open", strength=1, model="y")
    qwen.model = "q"
    result = pick_execution(
        _roster(qwen, open_route), NOW, execution_class=JUDGMENT, requested_model="q"
    )
    assert (result.executed_provider, result.model) == (open_route, "q")


def test_pinned_model_a_route_model_no_account_runs_adds_nothing() -> None:
    qwen = _account_model_qwen()
    qwen.strength = 1
    qwen.model = "q"
    open_route = _provider("open", strength=9, model="y")
    result = pick_execution(
        _roster(open_route, qwen), NOW, execution_class=JUDGMENT, requested_model="q"
    )
    assert (result.executed_provider, result.model) == (open_route, "q")


def test_pinned_model_a_declaring_route_refused_for_capability_is_not_skipped() -> None:
    agy = Provider(name="agy", kind="agy", strength=9, model="g", models=["g"])
    open_route = _provider("open", strength=1, model="y")
    assert _outcome(
        _roster(agy, open_route),
        execution_class=JUDGMENT,
        requested_model="g",
        system_prompt="s",
    ) == (
        "refused",
        (
            "no declared provider supports the requested "
            "read_paths/system_prompt capabilities"
        ),
    )


def test_pinned_model_a_walled_declaring_route_does_not_open_tier_two() -> None:
    declaring = _provider("declaring", strength=1, model="m", models=["m"])
    open_route = _provider("open", strength=9, model="y")
    assert _outcome(
        _roster(declaring, open_route),
        execution_class=JUDGMENT,
        requested_model="m",
        provider_walls={"declaring": NOW + 500},
    ) == ("walled", "every eligible Claude route is cooling down", NOW + 500)


def test_pinned_model_an_open_route_whose_model_is_the_pin_is_tier_one() -> None:
    own = _provider("own", strength=1, model="m")
    other = _provider("other", strength=9, model="y")
    result = pick_execution(
        _roster(other, own), NOW, execution_class=JUDGMENT, requested_model="m"
    )
    assert result.executed_provider is own
