"""The order an unpinned review or verification tries routes in, and the
producer it never runs as.

A review or a judge is never the author or the evaluated model. With no
`provider`, nSPAWN takes the routes in the roster's own order: the
`[preference]` list the owner declared for the class, then the route `[roles]`
binds to the reviewer with its `[role_profile]`, then every route the class
ranks. The producer is the work's own route, account or model. It is kept off a
copy of the roster, so the owner's roster and the next dispatch keep every
account.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from nspawn import picker
from nspawn.owner_roster import NativeAccount, Provider, Roster, RosterError
from nspawn.ranks import REVIEW, VERIFICATION

NOW = 1_000_000


def _roster(**fields: object) -> Roster:
    claude = Provider(
        name="claude",
        strength=9,
        model="opus",
        models=["opus", "sonnet"],
        accounts={"a": NativeAccount(home=None), "b": NativeAccount(home=None)},
        declared=True,
    )
    codex = Provider(
        name="codex",
        kind="codex",
        strength=9,
        model="gpt-main",
        models=["gpt-main", "gpt-sol"],
    )
    qwen = Provider(name="qwen", strength=5, model="qwen-27b")
    return Roster(providers=[claude, codex, qwen], **fields)  # type: ignore[arg-type]


def test_a_review_tries_the_reviewer_role_first_with_its_profile() -> None:
    roster = _roster(
        roles={"reviewer": "codex"},
        role_profiles={"reviewer": {"model": "gpt-sol", "effort": "high"}},
    )
    assert picker.preferences(roster, REVIEW) == [
        picker.Preference("codex", "gpt-sol", "high"),
        picker.Preference("claude"),
        picker.Preference("codex"),
        picker.Preference("qwen"),
    ]


def test_a_review_with_no_reviewer_role_ranks_by_strength() -> None:
    assert picker.preferences(_roster(), REVIEW) == [
        picker.Preference("claude"),
        picker.Preference("codex"),
        picker.Preference("qwen"),
    ]


def test_verification_takes_no_role_and_keeps_its_rank_order() -> None:
    roster = _roster(roles={"reviewer": "codex"})
    assert [p.route for p in picker.preferences(roster, VERIFICATION)] == [
        "claude",
        "codex",
        "qwen",
    ]


def test_the_declared_preference_comes_before_the_role_and_the_rank() -> None:
    "The owner's order: his OAuth account first, then the rest."
    roster = _roster(
        roles={"reviewer": "codex"},
        role_profiles={"reviewer": {"model": "gpt-sol"}},
        preferences={"review": [("b", None), ("codex", "gpt-main")]},
    )
    assert picker.preferences(roster, REVIEW) == [
        picker.Preference("b"),
        picker.Preference("codex", "gpt-main"),
        picker.Preference("codex", "gpt-sol"),
        picker.Preference("claude"),
        picker.Preference("codex"),
        picker.Preference("qwen"),
    ]


def test_a_preference_declared_for_one_class_does_not_order_the_other() -> None:
    roster = _roster(preferences={"verification": [("qwen", None)]})
    assert picker.preferences(roster, REVIEW)[0] == picker.Preference("claude")
    assert picker.preferences(roster, VERIFICATION)[0] == picker.Preference("qwen")


def test_a_preference_listed_twice_keeps_its_first_place() -> None:
    roster = _roster(preferences={"review": [("qwen", None), ("qwen", None)]})
    assert [str(p) for p in picker.preferences(roster, REVIEW)] == [
        "qwen",
        "claude",
        "codex",
    ]


@pytest.mark.parametrize("execution_class", [REVIEW, VERIFICATION])
def test_a_named_only_route_is_tried_only_where_the_roster_names_it(
    execution_class: str,
) -> None:
    """A named_only route never takes an unnamed dispatch (#1661): the rank
    tail leaves it out, and only a declared preference puts it in the order."""
    colab = Provider(name="colab", strength=10, model="qwen-flash", named_only=True)
    roster = _roster()
    roster = Roster(providers=[colab, *roster.providers])
    assert "colab" not in [p.route for p in picker.preferences(roster, execution_class)]
    named = Roster(
        providers=roster.providers,
        preferences={execution_class: [("colab", None)]},
    )
    assert picker.preferences(named, execution_class)[0] == picker.Preference("colab")


def test_a_preference_reads_as_its_route_and_model() -> None:
    assert str(picker.Preference("codex", "gpt-sol")) == "codex/gpt-sol"
    assert str(picker.Preference("claude")) == "claude"


def test_a_producer_name_is_a_provider_then_an_account_then_a_model() -> None:
    producer = picker.producer_of(_roster(), ["codex", "a", "MiniMax-M3"])
    assert producer == picker.Producer(
        providers=frozenset({"codex"}),
        accounts=frozenset({"a"}),
        models=frozenset({"MiniMax-M3"}),
    )


def test_no_producer_excludes_nothing() -> None:
    assert picker.producer_of(_roster(), []) == picker.Producer()


@pytest.mark.parametrize(
    ("names", "route", "model", "reason"),
    [
        (["codex"], "codex", None, "the producer's route"),
        (["a"], "a", None, "the producer's account"),
        (["claude"], "b", None, "the producer's route"),
        (["gpt-sol"], "codex", "gpt-sol", "the producer's model"),
        (["gpt-sol"], "codex", None, None),
        (["a"], "claude", None, None),
        ([], "claude", None, None),
    ],
    ids=[
        "provider",
        "account",
        "account-of-the-producers-provider",
        "model",
        "other-model",
        "provider-with-another-account",
        "no-producer",
    ],
)
def test_a_route_is_refused_only_when_it_is_the_producer(
    names: list[str], route: str, model: str | None, reason: str | None
) -> None:
    roster = _roster()
    producer = picker.producer_of(roster, names)
    assert picker.producer_refusal(roster, producer, route, model) == reason


def test_a_producer_account_is_never_picked_and_the_roster_is_not_mutated() -> None:
    roster = _roster()
    kept_off = picker.without_accounts(roster, frozenset({"a"}))
    picked = picker.pick_execution(kept_off, NOW, requested_provider="claude")
    assert picked.executed_account == "b"
    # The owner's roster is untouched: the next dispatch may still take `a`.
    assert roster.providers[0].excluded_accounts == []
    assert (
        picker.pick_execution(roster, NOW, requested_provider="claude").executed_account
        == "a"
    )


def test_a_provider_whose_every_account_is_the_producers_has_none_left() -> None:
    kept_off = picker.without_accounts(_roster(), frozenset({"a", "b"}))
    with pytest.raises(picker.AllProvidersWalled, match="excluded") as walled:
        picker.pick_execution(kept_off, NOW, requested_provider="claude")
    # An exclusion is no wall: it names no reset to wait for.
    assert walled.value.nearest_reset is None


# -- the roster's `[preference]` table ----------------------------------------


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    os.chmod(path, 0o600)
    return path


_ROUTES = (
    '[bins]\ncodex = "/usr/bin/true"\n'
    '[[provider]]\nname = "codex"\nkind = "codex"\nmodel = "m"\n'
    'models = ["m", "big"]\naccounts = { main = { home = "/h/main" } }\n'
    '[[provider]]\nname = "claude"\nkind = "claude"\nmodel = "m"\n'
    'accounts = { oauth = { home = "/h/oauth" }, key = { home = "/h/key" } }\n'
)


def test_the_preference_table_orders_routes_accounts_and_models(
    tmp_path: Path,
) -> None:
    text = _ROUTES + (
        '[preference]\nreview = ["oauth", "codex/big", "claude"]\n'
        'verification = ["claude"]\n'
    )
    roster = Roster.read(_write(tmp_path, text))
    assert roster.preferences == {
        "review": [("oauth", None), ("codex", "big"), ("claude", None)],
        "verification": [("claude", None)],
    }


def test_no_preference_table_declares_no_preference(tmp_path: Path) -> None:
    assert Roster.read(_write(tmp_path, _ROUTES)).preferences == {}


@pytest.mark.parametrize(
    ("table", "named"),
    [
        ('[preference]\njudgment = ["claude"]\n', "preference.judgment"),
        ('[preference]\nreview = ["gone"]\n', "preference.review names"),
        ('[preference]\nreview = ["codex/gone"]\n', "preference.review: codex"),
        ('[preference]\nreview = ["/big"]\n', "preference.review names"),
        ('[preference]\nreview = ["codex/"]\n', "preference.review: codex"),
        ('[preference]\nreview = "claude"\n', "preference.review is not a list"),
    ],
    ids=[
        "unknown-class",
        "undeclared-route",
        "model-not-served",
        "no-route",
        "empty-model",
        "not-a-list",
    ],
)
def test_a_malformed_preference_is_refused_by_its_key(
    tmp_path: Path, table: str, named: str
) -> None:
    with pytest.raises(RosterError, match=named.replace(".", r"\.")):
        Roster.read(_write(tmp_path, _ROUTES + table))


def test_a_preference_that_is_not_a_table_is_refused(tmp_path: Path) -> None:
    # A top-level key comes before the first table header.
    with pytest.raises(RosterError, match="preference is not a table"):
        Roster.read(_write(tmp_path, "preference = 1\n" + _ROUTES))


@pytest.mark.parametrize(
    ("catalog", "produced", "route_model", "same"),
    [
        ({"opus": "claude-opus-5-5"}, "claude-opus-5-5", "opus", True),
        ({"opus": "claude-opus-5-5"}, "opus", "claude-opus-5-5", True),
        ({"opus": "claude-opus-5-5"}, "claude-opus-4-7", "opus", False),
        ({"opus": "claude-opus-5-5"}, "claude-opus-5-5", "opus[1m]", True),
        (None, "claude-opus-5-5", "opus", True),
        (None, "claude-sonnet-5", "opus", False),
    ],
    ids=[
        "id-on-alias",
        "alias-on-id",
        "older-id",
        "1m-alias",
        "no-catalog",
        "other-family",
    ],
)
def test_a_producer_model_is_matched_through_the_routes_family_alias(
    catalog: dict[str, str] | None, produced: str, route_model: str, same: bool
) -> None:
    """With no catalogue to read, a family alias matches every id of its family:
    the exclusion errs toward keeping the producer out."""
    from dataclasses import replace

    from nspawn.claude_catalog import ModelCatalogError

    roster = _roster()
    claude = replace(
        roster.providers[0],
        model_catalog=ModelCatalogError("no catalogue") if catalog is None else catalog,
    )
    roster = replace(roster, providers=[claude, *roster.providers[1:]])
    producer = picker.producer_of(roster, [produced])
    refused = picker.producer_refusal(roster, producer, "claude", route_model)
    assert (refused == "the producer's model") is same
