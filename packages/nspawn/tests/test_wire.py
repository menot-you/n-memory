"""The wire vocabulary and frame shapes."""

import sys

import pytest

from nspawn import limits, wire
from nspawn.wire import (
    ApprovalPolicy,
    ExecutorSandbox,
    RpcErrorObject,
    RpcFrame,
)


def test_the_client_identity_is_the_oracle_s() -> None:
    assert wire.CLIENT_NAME == "nott-nspawn"
    assert wire.CLIENT_TITLE == "nSPAWN Codex control plane"
    assert wire.CLIENT_VERSION == "0.1.0"
    assert wire.OPT_OUT_NOTIFICATIONS == (
        "item/agentMessage/delta",
        "item/reasoning/summaryTextDelta",
    )


def test_the_wire_ceilings_are_the_shared_ones_not_copies() -> None:
    assert wire.MAX_FILES_PER_ITEM == limits.MAX_FILES_PER_ITEM
    assert wire.MAX_IDENTIFIER_BYTES == limits.MAX_DURABLE_IDENTIFIER_BYTES
    assert wire.MAX_METHOD_BYTES == limits.MAX_METHOD_BYTES
    assert wire.MAX_IDENTIFIER_BYTES == 512


def test_the_sandbox_spells_itself_and_defaults_to_read_only() -> None:
    assert ExecutorSandbox.default() is ExecutorSandbox.READ_ONLY
    assert ExecutorSandbox.READ_ONLY.as_str() == "read-only"
    assert ExecutorSandbox.WORKSPACE_WRITE.as_str() == "workspace-write"


def test_the_approval_policy_defaults_to_never() -> None:
    assert ApprovalPolicy.default() is ApprovalPolicy.NEVER
    assert [p.as_str() for p in ApprovalPolicy] == ["untrusted", "on-request", "never"]


def test_parsing_answers_none_outside_the_set_rather_than_guessing() -> None:
    assert ExecutorSandbox.parse("workspace-write") is ExecutorSandbox.WORKSPACE_WRITE
    assert ExecutorSandbox.parse("workspaceWrite") is None
    assert ExecutorSandbox.parse("") is None
    assert ApprovalPolicy.parse("on-request") is ApprovalPolicy.ON_REQUEST
    assert ApprovalPolicy.parse("onRequest") is None


def test_a_present_null_result_is_not_an_absent_result() -> None:
    answered = RpcFrame.from_wire({"id": 1, "result": None})
    assert answered.has_result
    assert answered.result is None
    assert answered.to_wire() == {"id": 1, "result": None}

    unanswered = RpcFrame.from_wire({"id": 1})
    assert not unanswered.has_result
    with pytest.raises(AttributeError):
        _ = unanswered.result
    assert "result" not in unanswered.to_wire()


def test_a_frame_round_trips_and_omits_every_absent_member() -> None:
    raw = {
        "id": "r-1",
        "method": "thread/start",
        "params": {"cwd": "/tmp"},
        "jsonrpc": "2.0",
    }
    assert RpcFrame.from_wire(raw).to_wire() == raw


def test_an_error_frame_carries_its_object_and_drops_absent_data() -> None:
    frame = RpcFrame.from_wire(
        {"id": 2, "error": {"code": -32602, "message": "bad params"}}
    )
    assert frame.error == RpcErrorObject(code=-32602, message="bad params")
    assert frame.to_wire()["error"] == {"code": -32602, "message": "bad params"}
    with_data = RpcErrorObject(code=-1, message="m", data={"why": "x"})
    assert with_data.to_wire()["data"] == {"why": "x"}


def test_unknown_members_are_tolerated_rather_than_refused() -> None:
    # The installed protocol is broader than this slice; a frame carrying a
    # method and fields this port does not name must still parse.
    frame = RpcFrame.from_wire(
        {"method": "item/somethingNew", "params": {"x": 1}, "extra": "kept-out"}
    )
    assert frame.method == "item/somethingNew"
    assert frame.params == {"x": 1}


def test_frame_constructors_shape_what_they_promise() -> None:
    assert wire.request(1, "thread/start", {"cwd": "/tmp"}).to_wire() == {
        "id": 1,
        "method": "thread/start",
        "params": {"cwd": "/tmp"},
    }
    assert wire.notification("warning", {"message": "m"}).id is None
    assert wire.response(3, None).to_wire() == {"id": 3, "result": None}
    err = wire.error_response(4, RpcErrorObject(code=-1, message="no"))
    assert err.to_wire()["error"]["code"] == -1


def test_a_frame_answering_with_null_still_counts_as_a_response() -> None:
    assert wire.is_response(wire.response(1, None))
    assert wire.is_response(
        wire.error_response(1, RpcErrorObject(code=-1, message="m"))
    )
    assert not wire.is_response(wire.request(1, "m", {}))
    assert not wire.is_response(wire.notification("m", {}))


def test_classify_separates_the_three_kinds() -> None:
    from nspawn.wire import IncomingKind, classify

    answered = classify(wire.response(1, {"ok": True}))
    assert answered.kind is IncomingKind.RESPONSE
    assert answered.result == {"ok": True}
    assert answered.error is None

    failed = classify(wire.error_response(1, RpcErrorObject(code=-2, message="bad")))
    assert failed.kind is IncomingKind.RESPONSE
    assert failed.error is not None
    assert failed.result is None

    asked = classify(wire.request("abc", "thread/start", {"cwd": "/tmp"}))
    assert asked.kind is IncomingKind.REQUEST
    assert asked.method == "thread/start"

    told = classify(wire.notification("warning", None))
    assert told.kind is IncomingKind.NOTIFICATION
    # An absent params member becomes an empty object, never None.
    assert told.params == {}


def test_classify_refuses_what_would_cross_into_durable_state() -> None:
    from nspawn.wire import WireError, classify

    with pytest.raises(WireError, match="neither response nor method"):
        classify(RpcFrame(id=1))
    with pytest.raises(WireError, match="exceeds"):
        classify(wire.notification("m" * (wire.MAX_METHOD_BYTES + 1), {}))
    with pytest.raises(WireError, match="bounded scalar"):
        classify(RpcFrame(id={"nested": "id"}, method="m", params={}))
    with pytest.raises(WireError, match="bounded scalar"):
        classify(RpcFrame(id="x" * (wire.MAX_IDENTIFIER_BYTES + 1), method="m"))


def test_an_identifier_that_cannot_be_stored_is_absent_not_truncated() -> None:
    assert wire.bounded_identifier("t-1") == "t-1"
    assert wire.bounded_identifier("") is None
    assert wire.bounded_identifier(None) is None
    assert wire.bounded_identifier("x" * wire.MAX_IDENTIFIER_BYTES) is not None
    assert wire.bounded_identifier("x" * (wire.MAX_IDENTIFIER_BYTES + 1)) is None


def test_the_turn_id_is_selected_before_it_is_converted() -> None:
    from nspawn.wire import parse_notification

    # `turn.id` present but not a string stays a MISS; it does not fall back to
    # the top-level turnId, because the selection happens first.
    parsed = parse_notification(
        "turn/started", {"turn": {"id": 7}, "turnId": "t-fallback"}
    )
    assert parsed.turn_id is None
    # Absent `turn.id` DOES fall back.
    assert parse_notification("turn/started", {"turnId": "t-2"}).turn_id == "t-2"
    assert parse_notification("turn/started", {"turn": {"id": "t-1"}}).turn_id == "t-1"


def test_a_turn_that_reported_no_status_is_unknown_never_completed() -> None:
    from nspawn.wire import TurnCompletionStatus, parse_notification

    missing = parse_notification("turn/completed", {"turn": {"id": "t-1"}})
    assert missing.status == TurnCompletionStatus("missing")
    assert not missing.status.is_known()

    for value in ("completed", "interrupted", "failed"):
        done = parse_notification("turn/completed", {"turn": {"status": value}})
        assert done.status.as_str() == value
        assert done.status.is_known()

    strange = parse_notification("turn/completed", {"status": "exploded"})
    assert strange.status.as_str() == "exploded"
    assert not strange.status.is_known()


def test_the_turn_status_prefers_the_nested_field() -> None:
    from nspawn.wire import parse_notification

    both = parse_notification(
        "turn/completed", {"turn": {"status": "completed"}, "status": "failed"}
    )
    assert both.status.as_str() == "completed"


def test_item_notifications_fall_back_to_the_whole_payload() -> None:
    from nspawn.wire import NotificationKind, parse_notification

    with_item = parse_notification(
        "item/completed", {"item": {"id": "i-1"}, "turnId": "t"}
    )
    assert with_item.kind is NotificationKind.ITEM_COMPLETED
    assert with_item.item == {"id": "i-1"}
    assert with_item.turn_id == "t"

    without = parse_notification("item/started", {"turnId": "t"})
    assert without.item == {"turnId": "t"}


def test_both_output_delta_methods_parse_the_same_way() -> None:
    from nspawn.wire import NotificationKind, parse_notification

    for method in (
        "item/commandExecution/outputDelta",
        "item/fileChange/outputDelta",
    ):
        parsed = parse_notification(method, {"itemId": "i-1", "delta": "abc"})
        assert parsed.kind is NotificationKind.OUTPUT_DELTA
        assert parsed.item_id == "i-1"
        assert parsed.delta == "abc"
    # A missing delta is empty text, never None.
    assert parse_notification("item/fileChange/outputDelta", {}).delta == ""


def test_error_and_warning_default_their_message_rather_than_dropping_it() -> None:
    from nspawn.wire import parse_notification

    assert parse_notification("error", {}).message == wire.DEFAULT_ERROR_MESSAGE
    assert parse_notification("warning", {}).message == wire.DEFAULT_WARNING_MESSAGE
    nested = parse_notification(
        "error", {"error": {"message": "deep"}, "message": "flat"}
    )
    assert nested.message == "deep"
    assert parse_notification("error", {"message": "flat"}).message == "flat"
    assert not parse_notification("error", {}).will_retry
    assert parse_notification("error", {"willRetry": True}).will_retry


def test_an_unnamed_method_is_retained_whole_never_dropped() -> None:
    from nspawn.wire import NotificationKind, parse_notification

    other = parse_notification("item/somethingNew", {"x": 1})
    assert other.kind is NotificationKind.OTHER
    assert other.method == "item/somethingNew"
    assert other.params == {"x": 1}


def test_the_handshake_opts_out_of_the_delta_firehose() -> None:
    params = wire.initialize_params()
    assert params["clientInfo"]["name"] == "nott-nspawn"
    assert params["capabilities"]["experimentalApi"] is True
    assert params["capabilities"]["requestAttestation"] is False
    assert params["capabilities"]["optOutNotificationMethods"] == [
        "item/agentMessage/delta",
        "item/reasoning/summaryTextDelta",
    ]
    assert wire.initialized_notification().method == "initialized"
    assert wire.initialized_notification().id is None


def test_the_thread_this_client_opens_is_confined() -> None:
    from nspawn.wire import ApprovalPolicy, ExecutorSandbox

    read_only = wire.thread_start_params(
        "gpt-5", "/tmp/wt", ApprovalPolicy.NEVER, ExecutorSandbox.READ_ONLY
    )
    config = read_only["config"]
    assert config["sandbox_workspace_write.network_access"] is False
    assert config["tools.web_search"] is False
    assert config["web_search"] == "disabled"
    assert config["notify"] == []
    assert config["orchestrator.mcp.enabled"] is False
    assert config["shell_environment_policy.inherit"] == "none"
    # The literal per platform is pinned below.
    assert config["shell_environment_policy.set.PATH"] == wire.SANDBOX_TOOL_PATH
    assert read_only["allowProviderModelFallback"] is False
    # A read-only thread names no writable root at all.
    assert "sandbox_workspace_write.writable_roots" not in config


def test_a_writable_thread_names_its_one_root_and_excludes_the_temp_dirs() -> None:
    from nspawn.wire import ApprovalPolicy, ExecutorSandbox

    params = wire.thread_start_params(
        "gpt-5", "/tmp/wt", ApprovalPolicy.ON_REQUEST, ExecutorSandbox.WORKSPACE_WRITE
    )
    config = params["config"]
    assert config["sandbox_workspace_write.writable_roots"] == ["/tmp/wt"]
    assert config["sandbox_workspace_write.exclude_tmpdir_env_var"] is True
    assert config["sandbox_workspace_write.exclude_slash_tmp"] is True
    assert params["approvalPolicy"] == "on-request"
    assert params["sandbox"] == "workspace-write"


def test_the_optional_params_appear_only_when_named() -> None:
    bare = wire.turn_start_params("t-1", "do it")
    assert bare == {"threadId": "t-1", "input": [{"type": "text", "text": "do it"}]}
    full = wire.turn_start_params("t-1", "do it", cwd="/tmp", model="gpt-5")
    assert full["cwd"] == "/tmp"
    assert full["model"] == "gpt-5"


def test_ids_read_nested_first_then_top_level() -> None:
    assert wire.thread_id({"thread": {"id": "t-1"}, "id": "other"}) == "t-1"
    assert wire.thread_id({"id": "t-2"}) == "t-2"
    assert wire.thread_id({"thread": {"id": 7}, "id": "t-3"}) == "t-3"
    assert wire.thread_id({}) is None
    assert wire.turn_id({"turn": {"id": "u-1"}}) == "u-1"
    assert wire.turn_id({"id": "u-2"}) == "u-2"
    assert wire.thread_id({"id": "x" * (wire.MAX_IDENTIFIER_BYTES + 1)}) is None


@pytest.mark.parametrize("raw", [[], "a string", 42, None, True])
def test_a_frame_that_is_not_an_object_is_refused_at_this_boundary(raw: object) -> None:
    # Defence in depth: the reader also closes on anything it cannot read, but
    # the type that OWNS the frame shape refuses here, where the value first
    # arrives decoded. Testing it at the reader alone would leave this layer
    # unfalsifiable.
    from nspawn.wire import WireError

    with pytest.raises(WireError, match="not an object"):
        RpcFrame.from_wire(raw)


@pytest.mark.parametrize("error", ["not-a-map", [], 7])
def test_an_error_member_that_is_not_an_object_is_refused(error: object) -> None:
    from nspawn.wire import WireError

    with pytest.raises(WireError, match="error member"):
        RpcFrame.from_wire({"id": 1, "error": error})


def test_an_error_member_missing_its_fields_is_refused() -> None:
    from nspawn.wire import WireError

    with pytest.raises(WireError, match="malformed"):
        RpcFrame.from_wire({"id": 1, "error": {"code": -1}})


def test_the_sandbox_path_reaches_this_hosts_tools() -> None:
    """macOS keeps git, rg and python3 under Homebrew; `/usr/bin` holds only
    Apple's shims there. Linux keeps the oracle's exact PATH."""
    expected = (
        "/opt/homebrew/bin:/usr/bin:/bin"
        if sys.platform == "darwin"
        else "/usr/bin:/bin"
    )
    assert wire.SANDBOX_TOOL_PATH == expected
