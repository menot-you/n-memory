"""Causal subscription routing uses finite declared accounts and a real store."""

import copy
import json

import pytest
import test_route

from nspawn.admission import AttemptKey
from nspawn.registry import Registry, RegistryError, create_store
from nspawn.route import RouteRefused, route_list, route_switch
from nspawn.terminal import subscription_limit

root = test_route.root

KEY = AttemptKey(
    {"activation_id": "activation", "attempt_id": "attempt", "owner_turn_id": "owner"}
)
DIGEST = "sha256:" + "a" * 64


def rejection():
    return {
        "version": 1,
        "key": KEY.data(),
        "profile_digest": DIGEST,
        "admission_id": "admission",
        "stop_epoch": 3,
        "observed_at": 1000,
        "terminal": {
            "type": "result",
            "subtype": "error_during_execution",
            "is_error": True,
            "session_id": "native-1",
            "terminal_reason": "blocking_limit",
        },
        "messages": [
            {
                "type": "rate_limit_event",
                "session_id": "native-1",
                "rate_limit_info": {
                    "status": "rejected",
                    "rateLimitType": "seven_day",
                    "resetsAt": 2000,
                },
            }
        ],
    }


def classify(body):
    return subscription_limit(
        body,
        account_alias="alpha",
        key=KEY,
        profile_digest=DIGEST,
        stop_epoch=3,
        native_session="native-1",
        now=1000,
    )


def test_only_a_correlated_terminal_subscription_rejection_derives_the_exact_vendor_wall():
    wall = classify(rejection())
    assert wall is not None
    assert (
        wall.account_alias,
        wall.limit_type,
        wall.cooling_until,
        wall.observed_at,
        wall.source.value,
    ) == ("alpha", "seven_day", 2000, 1000, "vendor_reset")


@pytest.mark.parametrize(
    "kind",
    [
        "warning",
        "overage",
        "billing",
        "auxiliary",
        "success",
        "no-terminal",
        "wrong-key",
        "wrong-digest",
        "wrong-epoch",
        "wrong-session",
        "no-reset",
        "elapsed",
        "prose",
        "other-error",
    ],
)
def test_noncausal_or_uncorrelated_carriers_cannot_rotate_an_account(kind):
    body = rejection()
    rate = body["messages"][0]
    if kind == "warning":
        rate["rate_limit_info"]["status"] = "allowed_warning"
    if kind == "overage":
        rate["rate_limit_info"]["rateLimitType"] = "overage"
    if kind == "billing":
        rate["rate_limit_info"]["errorCode"] = "credits_required"
    if kind == "auxiliary":
        rate["parent_tool_use_id"] = "subagent-tool"
    if kind == "success":
        body["terminal"].update(subtype="success", is_error=False)
    if kind == "no-terminal":
        body["terminal"] = None
    if kind == "wrong-key":
        body["key"]["attempt_id"] = "other"
    if kind == "wrong-digest":
        body["profile_digest"] = "sha256:" + "b" * 64
    if kind == "wrong-epoch":
        body["stop_epoch"] = 4
    if kind == "wrong-session":
        rate["session_id"] = "other-session"
    if kind == "no-reset":
        rate["rate_limit_info"].pop("resetsAt")
    if kind == "elapsed":
        rate["rate_limit_info"]["resetsAt"] = 1000
    if kind == "prose":
        body["messages"] = [
            {
                "type": "assistant",
                "message": {"content": "rate_limit subscription seven_day"},
            }
        ]
    if kind == "other-error":
        body["terminal"]["terminal_reason"] = "model_error"
    assert classify(body) is None


def test_route_list_is_ordered_and_route_switch_is_a_monotonic_real_wall_write(
    root, tmp_path
):
    from nspawn.route import read_roster

    roster = read_roster()
    create_store(tmp_path / "routes.db")
    with Registry(tmp_path / "routes.db", "route-transition-test") as registry:
        registry.upgrade()
        first = route_list(
            roster, registry, model="claude-opus-5", effort="high", now=1000
        )
        assert [row["alias"] for row in first["routes"]] == ["alpha", "beta"]
        assert all(row["eligible"] for row in first["routes"])
        source = first["routes"][0]["route"]
        result = route_switch(roster, registry, source, rejection(), now=1000)
        assert result["wall"]["cooling_until"] == 2000
        assert not result["routes"][0]["eligible"]
        assert result["routes"][1]["eligible"]
        assert (
            route_switch(roster, registry, source, rejection(), now=1000)["wall"]
            == result["wall"]
        )
        invalid = copy.deepcopy(rejection())
        invalid["terminal"]["is_error"] = False
        with pytest.raises(RouteRefused, match="causal"):
            route_switch(roster, registry, source, invalid, now=1000)
        assert json.loads(json.dumps(result))["wall"]["observed_at"] == 1000


@pytest.mark.parametrize(
    "mode,expected", [("landed", 1), ("unsatisfied", 2), ("unreadable", 1)]
)
def test_unknown_wall_write_is_read_before_one_bounded_identical_replay(
    root, tmp_path, monkeypatch, mode, expected
):
    from nspawn.route import read_roster

    create_store(tmp_path / "routes.db")
    with Registry(tmp_path / "routes.db", "route-transition-test") as registry:
        attempts = []
        original = registry.record_account_wall

        def write(*args):
            attempts.append(args)
            if len(attempts) == 1:
                if mode == "landed":
                    original(*args)
                raise RegistryError("wall acknowledgement lost")
            original(*args)

        monkeypatch.setattr(registry, "record_account_wall", write)
        if mode == "unreadable":

            def missing(*args):
                raise RegistryError("read unavailable")

            monkeypatch.setattr(registry, "get_enforced_account_wall_rows", missing)
            with pytest.raises(RegistryError, match="read unavailable"):
                route_switch(
                    read_roster(),
                    registry,
                    f"alpha@{root / 'claude-home'}",
                    rejection(),
                    now=1000,
                )
        else:
            assert (
                route_switch(
                    read_roster(),
                    registry,
                    f"alpha@{root / 'claude-home'}",
                    rejection(),
                    now=1000,
                )["wall"]["cooling_until"]
                == 2000
            )
        assert len(attempts) == expected
        assert all(args == attempts[0] for args in attempts)


def test_the_sdk_route_list_excludes_a_banned_account(root, tmp_path):
    from nspawn.route import read_roster

    roster = read_roster()
    provider = roster.select("claude")
    assert provider is not None
    provider.excluded_accounts = ["alpha"]
    create_store(tmp_path / "routes.db")
    with Registry(tmp_path / "routes.db", "route-exclusion-test") as registry:
        registry.upgrade()
        result = route_list(
            roster, registry, model="claude-opus-5", effort="high", now=1000
        )
    banned = next(row for row in result["routes"] if row["alias"] == "alpha")
    assert not banned["eligible"]
    assert "excluded in the roster" in banned["exclusions"]


@pytest.mark.parametrize("verb", ["list", "switch"])
@pytest.mark.parametrize("model", ["default", "haiku", "claude-haiku-5-5"])
def test_route_read_and_switch_use_declared_names_without_a_binary(
    root, tmp_path, monkeypatch, verb, model
):
    from nspawn import claude_catalog
    from nspawn.route import RosterUnreadable, read_roster

    def refuse(binary):
        pytest.fail("The roster read must not start the test client")

    monkeypatch.setattr(claude_catalog, "catalog_read", refuse)
    test_route.edit(
        root,
        'model = "claude-opus-5"',
        'model = "haiku"\nmodels = ["opus", "haiku"]',
    )
    path = root / "spawn.toml"
    path.write_text(
        path.read_text() + '\n[roles]\nwriter = "claude"\n'
        '[role_profile.writer]\nmodel = "claude-opus-5-5"\n'
    )
    with pytest.raises(RosterUnreadable):
        read_roster()
    test_route.edit(root, 'model = "claude-opus-5-5"', 'model = "haiku"')
    roster = read_roster()
    assert roster.role_profiles["writer"]["model"] == "haiku"
    create_store(tmp_path / "routes.db")
    with Registry(tmp_path / "routes.db", "route-declared-test") as registry:
        registry.upgrade()
        if verb == "list":
            result = route_list(roster, registry, model=model, effort="high", now=1000)
        else:
            result = route_switch(
                roster,
                registry,
                f"alpha@{root / 'claude-home'}",
                rejection(),
                model=model,
                effort="high",
                now=1000,
            )
            assert result["wall"]["cooling_until"] == 2000
        beta = next(row for row in result["routes"] if row["alias"] == "beta")
        assert beta["eligible"] == (model == "haiku")
        assert ("model is incompatible" in beta["exclusions"]) == (model != "haiku")


def test_a_switch_moves_a_node_off_an_excluded_account(root, tmp_path):
    from nspawn.route import read_roster

    roster = read_roster()
    provider = roster.select("claude")
    assert provider is not None
    provider.excluded_accounts = ["alpha"]
    create_store(tmp_path / "routes.db")
    with Registry(tmp_path / "routes.db", "route-exclusion-test") as registry:
        registry.upgrade()
        # The node was made on alpha before its exclusion: its wall still lands.
        result = route_switch(
            roster, registry, f"alpha@{root / 'claude-home'}", rejection(), now=1000
        )
    assert result["wall"]["account_alias"] == "alpha"
    assert [row["alias"] for row in result["routes"] if row["eligible"]] == ["beta"]


#: The `_nott/turn/failure` frame a node turn sent on 2026-10-20 (run b4c77c67,
#: Claude Code 2.1.304) when the organization refused the OAuth account. Ids,
#: digests and the request id are synthetic.
ORG_REFUSAL = json.loads(
    (
        test_route.Path(__file__).parent
        / "fixtures"
        / "claude-node-org-403-2.1.304.json"
    ).read_text()
)


def _org(body):
    native = body["terminal"]["session_id"]
    return subscription_limit(
        body,
        account_alias="alpha",
        key=AttemptKey(body["key"]),
        profile_digest=body["profile_digest"],
        stop_epoch=body["stop_epoch"],
        native_session=native,
        now=body["observed_at"],
    )


def test_an_organization_refusal_walls_the_account_as_a_rejected_credential():
    """nSPAWN is the router: the node host moves the node
    off an account its organization refuses, as it does for a spent window."""
    wall = _org(copy.deepcopy(ORG_REFUSAL))
    assert wall is not None
    observed = ORG_REFUSAL["observed_at"]
    assert (wall.account_alias, wall.limit_type, wall.source.value) == (
        "alpha",
        "credential_rejected",
        "default_1h",
    )
    assert (wall.observed_at, wall.cooling_until) == (observed, observed + 300)


@pytest.mark.parametrize("kind", ["subagent", "other-session", "other-error", "prose"])
def test_an_organization_refusal_of_another_turn_walls_nothing(kind):
    body = copy.deepcopy(ORG_REFUSAL)
    refusal = body["messages"][0]
    if kind == "subagent":
        refusal["parent_tool_use_id"] = "subagent-tool"
    if kind == "other-session":
        refusal["session_id"] = "other-session"
    if kind == "other-error":
        refusal["error"] = "invalid_request"
    if kind == "prose":
        refusal.pop("is_api_error_message")
    assert _org(body) is None


def test_route_switch_moves_a_node_off_an_account_its_organization_refuses(
    root, tmp_path
):
    from nspawn.route import read_roster

    roster = read_roster()
    create_store(tmp_path / "routes.db")
    now = ORG_REFUSAL["observed_at"]
    with Registry(tmp_path / "routes.db", "route-org-test") as registry:
        registry.upgrade()
        source = route_list(
            roster, registry, model="claude-opus-5", effort="high", now=now
        )["routes"][0]["route"]
        result = route_switch(
            roster, registry, source, copy.deepcopy(ORG_REFUSAL), now=now
        )
    assert result["wall"]["limit_type"] == "credential_rejected"
    assert result["wall"]["cooling_until"] == now + 300
    assert [row["eligible"] for row in result["routes"]] == [False, True]
