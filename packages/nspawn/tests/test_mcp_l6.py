"""L6's surface: every real tool, driven through `McpServer.call_tool`.

The SDK door (`nspawn.main`) owns the envelope. This file proves the catalogue
it lists and the dispatch `call_tool` runs for `tools/call`: schema
validation, the `class` -> `execution_class` wire rename, admission accounting
on the real tool names, typed-refusal translation, and result rendering — all
against a `FakeSupervisor` double, so no test here needs a reachable database,
a run root, or a spawnable child.

Two of the double's methods stand in for L7 surfaces that do not exist yet:
`quota` and `fleet`. `mcp.py` calls both against their declared form.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import patch

import nott_database
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from mcp_fakes import FakeSupervisor, handle
from registry_reset import store

from nspawn import main, mcp, ranks
from nspawn.errors import UNKNOWN, NSpawnError
from nspawn.mcp import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    NSPAWN_TOOL_BY_NAME,
    NSPAWN_TOOL_CONTRACTS,
    McpServer,
    ResponseTooLarge,
    SupervisorUnconfigured,
    _to_jsonable,
    _wire_kwargs,
    validate_arguments,
)
from nspawn.owner_roster import CAPABILITIES, Roster
from nspawn.process_identity import ProcessIdentity
from nspawn.registry import RegistryError, store_path
from nspawn.rows import RunPhase
from nspawn.supervisor import (
    AccountQuota,
    CloseReadback,
    ProcessTreeReadback,
    ProviderListing,
    ProviderQuota,
    ProviderReport,
    QuotaReport,
    QuotaState,
    RunAccepted,
    RunResult,
    RunStatus,
    Supervisor,
    TranscriptPage,
    WindowQuota,
)

FUZZ_CORPUS = json.loads(
    (Path(__file__).parent / "fixtures" / "mcp_l6_fuzz_corpus.json").read_text(
        encoding="utf-8"
    )
)["cases"]


def ask(
    server: McpServer, method: str, params: object = None, request_id: object = 1
) -> dict[str, Any] | None:
    frame: dict[str, object] = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        frame["params"] = params
    return handle(server, frame)


def payload(answer: dict[str, Any] | None) -> Any:
    """The tool's answer, unwrapped from the `CallToolResult` envelope.

    `tools/call` returns `{"content": [{"type": "text", "text": <json>}]}` —
    the spec's shape, and the only shape a conforming client renders. Every
    assertion below is about the payload inside it, so it is unwrapped here
    once instead of at each call site.
    """
    assert answer is not None
    return json.loads(answer["result"]["content"][0]["text"])


# --------------------------------------------------------------------------
# tools/list: exactly the declared contracts, real non-empty schemas.
# --------------------------------------------------------------------------


#: The one tool whose input schema is empty on purpose. Named, not inferred:
#: a placeholder `{}` on any other contract is still refused.
_ARGUMENTLESS_TOOLS = frozenset({"nspawn_run_list"})


def test_tools_list_answers_exactly_the_declared_contracts() -> None:
    listed = [tool.model_dump(by_alias=True, exclude_none=True) for tool in main.TOOLS]
    assert [tool["name"] for tool in listed] == [c.name for c in NSPAWN_TOOL_CONTRACTS]
    for tool, contract in zip(listed, NSPAWN_TOOL_CONTRACTS, strict=True):
        assert tool["description"] == contract.description
        assert tool["inputSchema"] == contract.input_schema
        if tool["name"] in _ARGUMENTLESS_TOOLS:
            # The fleet listing takes nothing; its schema must then be CLOSED,
            # so an argument meant for `nspawn_run_show` is refused, not ignored.
            assert tool["inputSchema"]["properties"] == {}
            assert tool["inputSchema"]["additionalProperties"] is False
            assert tool["inputSchema"]["required"] == []
        else:
            assert tool["inputSchema"]["properties"], "schema must not be empty"


# --------------------------------------------------------------------------
# Argument validation: unknown keys, missing required, string/int boundaries.
# --------------------------------------------------------------------------

_STATUS_SCHEMA = NSPAWN_TOOL_BY_NAME["nspawn_run_show"].input_schema
_EXECUTE_SCHEMA = NSPAWN_TOOL_BY_NAME["nspawn_run_start"].input_schema
#: The fleet read is its own tool, `nspawn_run_list`, which takes nothing; the
#: missing-required rule is proven on `nspawn_result_show`, and `nspawn_run_show`
#: requires the run it reads.
_RESULT_SCHEMA = NSPAWN_TOOL_BY_NAME["nspawn_result_show"].input_schema


def test_missing_required_argument_is_invalid_params() -> None:
    with pytest.raises(mcp.McpError) as caught:
        validate_arguments(_RESULT_SCHEMA, {})
    assert caught.value.code == INVALID_PARAMS
    assert "run_id" in caught.value.rpc_message


def test_the_fleet_read_declares_nothing_required() -> None:
    validate_arguments(NSPAWN_TOOL_BY_NAME["nspawn_run_list"].input_schema, {})


def test_run_show_requires_the_run_it_reads() -> None:
    with pytest.raises(mcp.McpError) as caught:
        validate_arguments(_STATUS_SCHEMA, {})
    assert caught.value.code == INVALID_PARAMS
    assert "run_id" in caught.value.rpc_message


def test_run_show_takes_exactly_one_of_run_id_and_launch_uuid() -> None:
    validate_arguments(_STATUS_SCHEMA, {"run_id": "r1"})
    validate_arguments(_STATUS_SCHEMA, {"launch_uuid": "u1"})
    with pytest.raises(mcp.McpError) as caught:
        validate_arguments(_STATUS_SCHEMA, {"run_id": "r1", "launch_uuid": "u1"})
    assert caught.value.code == INVALID_PARAMS
    assert "launch_uuid" in caught.value.rpc_message


def test_unknown_argument_is_invalid_params() -> None:
    with pytest.raises(mcp.McpError) as caught:
        validate_arguments(_STATUS_SCHEMA, {"run_id": "r1", "vendor": "extra"})
    assert caught.value.code == INVALID_PARAMS
    assert "vendor" in caught.value.rpc_message


def test_every_required_field_is_checked_independently() -> None:
    # execute requires prompt, cwd AND class; each must be caught on its own.
    complete = {"prompt": "hi", "cwd": "/tmp", "class": "judgment"}
    for missing in ("prompt", "cwd", "class"):
        with pytest.raises(mcp.McpError, match=missing):
            validate_arguments(
                _EXECUTE_SCHEMA, {k: v for k, v in complete.items() if k != missing}
            )
    validate_arguments(_EXECUTE_SCHEMA, complete)


@given(st.integers(min_value=1, max_value=20), st.text(max_size=25))
@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
def test_property_string_minlength_boundary(
    minimum_length: int, candidate: str
) -> None:
    schema = {
        "type": "object",
        "properties": {"prompt": {"type": "string", "minLength": minimum_length}},
        "required": ["prompt"],
        "additionalProperties": False,
    }
    if len(candidate) < minimum_length:
        with pytest.raises(mcp.McpError) as caught:
            validate_arguments(schema, {"prompt": candidate})
        assert caught.value.code == INVALID_PARAMS
    else:
        validate_arguments(schema, {"prompt": candidate})


@given(
    st.integers(min_value=-1000, max_value=1000),
    st.integers(min_value=-1000, max_value=1000),
)
@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
def test_property_integer_minimum_boundary(minimum: int, candidate: int) -> None:
    schema = {
        "type": "object",
        "properties": {"since_seq": {"type": "integer", "minimum": minimum}},
        "required": ["since_seq"],
        "additionalProperties": False,
    }
    if candidate < minimum:
        with pytest.raises(mcp.McpError) as caught:
            validate_arguments(schema, {"since_seq": candidate})
        assert caught.value.code == INVALID_PARAMS
    else:
        validate_arguments(schema, {"since_seq": candidate})


@given(
    st.one_of(
        st.text(),
        st.floats(allow_nan=False),
        st.booleans(),
        st.none(),
        st.lists(st.integers()),
    )
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_property_wrong_type_is_always_invalid_params(value: object) -> None:
    schema = {
        "type": "object",
        "properties": {"since_seq": {"type": "integer", "minimum": 0}},
        "required": ["since_seq"],
        "additionalProperties": False,
    }
    with pytest.raises(mcp.McpError) as caught:
        validate_arguments(schema, {"since_seq": value})
    assert caught.value.code == INVALID_PARAMS


def test_bool_is_never_accepted_as_an_integer() -> None:
    # bool is a subclass of int in Python; the schema means integer, not bool.
    schema = {
        "type": "object",
        "properties": {"timeout_ms": {"type": "integer", "minimum": 0}},
        "required": [],
        "additionalProperties": False,
    }
    with pytest.raises(mcp.McpError):
        validate_arguments(schema, {"timeout_ms": True})


@given(st.sets(st.sampled_from(["run_id", "since_seq", "max_bytes"])))
@settings(max_examples=50)
def test_property_every_declared_subset_of_transcript_args_validates(
    present: set[str],
) -> None:
    schema = NSPAWN_TOOL_BY_NAME["nspawn_transcript_show"].input_schema
    values = {"run_id": "r1", "since_seq": 0, "max_bytes": 10}
    payload = {key: values[key] for key in present}
    if "run_id" in present:
        validate_arguments(schema, payload)
    else:
        with pytest.raises(mcp.McpError):
            validate_arguments(schema, payload)


# --------------------------------------------------------------------------
# _wire_kwargs: the ONE wire-key rename, and only for nspawn_run_start.
# --------------------------------------------------------------------------


def test_wire_kwargs_renames_class_to_execution_class_for_execute() -> None:
    mapped = _wire_kwargs(
        "nspawn_run_start", {"prompt": "hi", "cwd": "/tmp", "class": "design"}
    )
    assert mapped == {"prompt": "hi", "cwd": "/tmp", "execution_class": "design"}
    assert "class" not in mapped


def test_wire_kwargs_leaves_execute_without_class_untouched() -> None:
    payload = {"prompt": "hi", "cwd": "/tmp"}
    assert _wire_kwargs("nspawn_run_start", payload) == payload


#: Every verb the `class` rename must NOT touch. DERIVED: a hand-written list
#: of four silently stopped covering the surface the day a sixth verb landed,
#: which is the same disease as the hand-written `SERVER_INSTRUCTIONS`.
_NON_EXECUTE_TOOLS = [
    c.name for c in NSPAWN_TOOL_CONTRACTS if c.name != "nspawn_run_start"
]


@given(st.sampled_from(_NON_EXECUTE_TOOLS))
def test_property_class_rename_never_applies_to_other_tools(name: str) -> None:
    payload = {"class": "should-not-be-renamed"}
    assert _wire_kwargs(name, payload) == payload


# --------------------------------------------------------------------------
# _to_jsonable: nested dataclasses, enums, tuples, over the real result shapes.
# --------------------------------------------------------------------------


def test_to_jsonable_renders_run_accepted() -> None:
    # U3: an admission also names the phase it committed.
    assert _to_jsonable(RunAccepted(run_id="r1")) == {
        "run_id": "r1",
        "phase": "pending",
    }


def test_to_jsonable_renders_run_status_with_enum_value() -> None:
    status = RunStatus(
        run_id="r1",
        phase=RunPhase.COMPLETED,
        run_dir="/runs/r1",
        failure_reason=None,
        total_seq=3,
        requested_provider="claude",
        executed_provider="claude",
        executed_account=None,
        warning=None,
        tokens=42,
        marker="DONE",
        has_result=True,
        stdout_mtime_ns=1_787_900_001_000_000_000,
        stderr_tail="[claude-code:unrecognized_model]",
    )
    rendered = _to_jsonable(status)
    assert rendered["phase"] == "completed"
    assert rendered["tokens"] == 42
    assert rendered["executed_account"] is None
    assert rendered["has_result"] is True
    assert rendered["stdout_mtime_ns"] == 1_787_900_001_000_000_000
    assert rendered["stderr_tail"] == "[claude-code:unrecognized_model]"
    assert json.dumps(rendered)  # must be plain-JSON serializable


def test_to_jsonable_renders_result_stream_observations() -> None:
    rendered = _to_jsonable(
        RunResult(
            run_id="r1",
            final_text="PONG",
            stderr_tail="[claude-code:unrecognized_model]",
        )
    )

    assert rendered == {
        "run_id": "r1",
        "final_text": "PONG",
        "stderr_tail": "[claude-code:unrecognized_model]",
    }


def test_to_jsonable_renders_transcript_lines_as_a_list() -> None:
    page = TranscriptPage(
        run_id="r1", since_seq=0, next_seq=3, lines=("a", "b"), truncated=False
    )
    rendered = _to_jsonable(page)
    assert rendered["lines"] == ["a", "b"]
    assert isinstance(rendered["lines"], list)


def test_to_jsonable_renders_nested_close_readback() -> None:
    identity = ProcessIdentity(pid=123, boot_id="boot-1", start_ticks=99)
    readback = CloseReadback(
        run_id="r1",
        phase=RunPhase.CLOSED,
        tree=ProcessTreeReadback(checked=(identity,), survivors=()),
        claim_released=True,
    )
    rendered = _to_jsonable(readback)
    assert rendered["phase"] == "closed"
    assert rendered["tree"]["checked"] == [
        {"pid": 123, "boot_id": "boot-1", "start_ticks": 99}
    ]
    assert rendered["tree"]["survivors"] == []
    assert json.dumps(rendered)


@given(
    st.builds(
        RunStatus,
        run_id=st.text(min_size=1, max_size=8),
        phase=st.sampled_from(list(RunPhase)),
        requested_provider=st.one_of(st.none(), st.text(max_size=8)),
        executed_provider=st.one_of(st.none(), st.text(max_size=8)),
        executed_account=st.one_of(st.none(), st.text(max_size=8)),
        warning=st.one_of(st.none(), st.text(max_size=8)),
        tokens=st.integers(min_value=0, max_value=10_000),
        marker=st.one_of(st.none(), st.sampled_from(["DONE", "BLOCKED"])),
    )
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_property_to_jsonable_always_yields_plain_json(status: RunStatus) -> None:
    rendered = _to_jsonable(status)
    encoded = json.dumps(rendered)
    assert json.loads(encoded)["phase"] == status.phase.value


# --------------------------------------------------------------------------
# Full wiring, per tool: FakeSupervisor sees the mapped kwargs, and the door
# packages the rendered answer in the spec's `content` envelope. It used to
# return it at the TOP LEVEL of "result", which every test here read happily
# and no conforming MCP client could see at all.
# --------------------------------------------------------------------------


def test_execute_maps_class_and_returns_run_id_in_the_envelope() -> None:
    fake = FakeSupervisor(execute_result=RunAccepted(run_id="run-42"))
    server = McpServer(supervisor=fake)
    answer = ask(
        server,
        "tools/call",
        {
            "name": "nspawn_run_start",
            "arguments": {"prompt": "hi", "cwd": "/tmp", "class": "design"},
        },
    )
    assert answer is not None
    # U1: the answer is the driver's ack — the run id, whether it reattached,
    # and the phase read at admission.
    assert payload(answer) == {
        "run_id": "run-42",
        "deduplicated": False,
        "phase": "pending",
    }
    ((call_name, kwargs),) = fake.calls
    assert call_name == "execute"
    assert kwargs["execution_class"] == "design"
    assert "class" not in kwargs


def test_review_passes_branch_and_cwd_through_unchanged() -> None:
    fake = FakeSupervisor(review_result=RunAccepted(run_id="run-7"))
    server = McpServer(supervisor=fake)
    answer = ask(
        server,
        "tools/call",
        {
            "name": "nspawn_review_start",
            "arguments": {"branch": "feat/x", "cwd": "/tmp"},
        },
    )
    assert answer is not None
    assert payload(answer)["run_id"] == "run-7"
    ((_, kwargs),) = fake.calls
    assert kwargs["branch"] == "feat/x"
    assert kwargs["cwd"] == "/tmp"


def test_review_without_branch_reviews_the_branch_checked_out_in_cwd(
    tmp_path: Path,
) -> None:
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    git = ["git", "-C", str(repo)]
    subprocess.run([*git, "init", "-q", "-b", "lane/head"], check=True)
    subprocess.run(
        [*git, "-c", "user.email=t@t.invalid", "-c", "user.name=T"]
        + ["commit", "-q", "--allow-empty", "-m", "init"],
        check=True,
    )
    fake = FakeSupervisor(review_result=RunAccepted(run_id="run-8"))
    server = McpServer(supervisor=fake)
    arguments = {"cwd": str(repo), "prompt": "judge it"}
    answer = ask(
        server, "tools/call", {"name": "nspawn_review_start", "arguments": arguments}
    )
    assert answer is not None
    assert payload(answer)["run_id"] == "run-8"
    ((_, kwargs),) = fake.calls
    assert kwargs["branch"] == "lane/head"

    subprocess.run([*git, "checkout", "-q", "--detach"], check=True)
    answer = ask(
        server, "tools/call", {"name": "nspawn_review_start", "arguments": arguments}
    )
    assert answer is not None
    assert answer["error"]["code"] == INVALID_PARAMS
    assert "detached HEAD" in answer["error"]["message"]
    assert len(fake.calls) == 1


def test_unrecognized_model_fails_fast() -> None:
    from nspawn.errors import NERR_1105, NSpawnError

    def _execute_raises(*args: Any, **kwargs: Any) -> Any:
        raise NSpawnError(
            NERR_1105,
            f"model {kwargs.get('model')} is not served by provider",
        )

    fake = FakeSupervisor()
    fake.execute = _execute_raises  # type: ignore[assignment]
    server = McpServer(supervisor=fake)
    answer = ask(
        server,
        "tools/call",
        {
            "name": "nspawn_run_start",
            "arguments": {
                "prompt": "hi",
                "cwd": "/tmp",
                "class": "judgment",
                "model": "fugu-ultra",
            },
        },
    )
    assert answer is not None
    assert "error" in answer
    assert answer["error"]["code"] == INVALID_PARAMS
    assert "nERR-1105" in answer["error"]["message"]


def test_status_immediate_returns_phase_in_the_envelope() -> None:
    fake = FakeSupervisor(
        status_result=RunStatus(
            run_id="r1",
            phase=RunPhase.RUNNING,
            run_dir="/runs/r1",
            failure_reason=None,
            total_seq=0,
            requested_provider=None,
            executed_provider=None,
            executed_account=None,
            warning=None,
            tokens=0,
            marker=None,
        )
    )
    server = McpServer(supervisor=fake)
    answer = ask(
        server, "tools/call", {"name": "nspawn_run_show", "arguments": {"run_id": "r1"}}
    )
    assert answer is not None
    assert payload(answer)["phase"] == "running"


def test_status_live_progress_tokens_seq() -> None:
    status = RunStatus(
        run_id="r1",
        phase=RunPhase.RUNNING,
        run_dir="/runs/r1",
        failure_reason=None,
        total_seq=5,
        requested_provider=None,
        executed_provider="claude",
        executed_account=None,
        warning=None,
        tokens=350,
        marker=None,
    )
    fake = FakeSupervisor(status_result=status)
    answer = ask(
        McpServer(supervisor=fake),
        "tools/call",
        {"name": "nspawn_run_show", "arguments": {"run_id": "r1"}},
    )
    assert answer is not None
    p = payload(answer)
    assert p["phase"] == "running"
    assert p["tokens"] == 350
    assert p["total_seq"] == 5


@pytest.mark.parametrize("guard", [True, False, 0, 1, "true", None, [], {}])
def test_missing_only_close_refuses_a_run_id_before_closing(
    monkeypatch: pytest.MonkeyPatch, guard: Any
) -> None:
    fake = FakeSupervisor()

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("invalid guarded close reached run mutation")

    monkeypatch.setattr(fake, "close", forbidden)
    answer = ask(
        McpServer(supervisor=fake),
        "tools/call",
        {
            "name": "nspawn_run_close",
            "arguments": {"run_id": "r1", "only_unadmitted": guard},
        },
    )
    assert answer is not None and answer["error"]["code"] == INVALID_PARAMS


def test_close_idempotent_on_terminal_run() -> None:
    fake = FakeSupervisor(
        close_result=CloseReadback(
            run_id="r1",
            phase=RunPhase.CLOSED,
            tree=ProcessTreeReadback(checked=(), survivors=()),
            claim_released=True,
        )
    )
    server = McpServer(supervisor=fake)
    ans1 = ask(
        server,
        "tools/call",
        {"name": "nspawn_run_close", "arguments": {"run_id": "r1"}},
    )
    assert ans1 is not None
    assert payload(ans1)["phase"] == "closed"
    assert payload(ans1)["claim_released"] is True

    ans2 = ask(
        server,
        "tools/call",
        {"name": "nspawn_run_close", "arguments": {"run_id": "r1"}},
    )
    assert ans2 is not None
    assert payload(ans2)["phase"] == "closed"
    assert payload(ans2)["claim_released"] is True


def test_transcript_returns_lines_and_close_returns_tree() -> None:
    fake = FakeSupervisor(
        transcript_result=TranscriptPage(
            run_id="r1", since_seq=0, next_seq=1, lines=("PONG",), truncated=False
        ),
        close_result=CloseReadback(
            run_id="r1",
            phase=RunPhase.CLOSED,
            tree=ProcessTreeReadback(checked=(), survivors=()),
            claim_released=True,
        ),
    )
    server = McpServer(supervisor=fake)
    transcript = ask(
        server,
        "tools/call",
        {"name": "nspawn_transcript_show", "arguments": {"run_id": "r1"}},
    )
    assert transcript is not None
    assert payload(transcript)["lines"] == ["PONG"]
    close = ask(
        server,
        "tools/call",
        {"name": "nspawn_run_close", "arguments": {"run_id": "r1"}},
    )
    assert close is not None
    assert payload(close)["claim_released"] is True


# --------------------------------------------------------------------------
# Admission: only nspawn_run_show with timeout_ms > 0 touches the wait ceiling.
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# Typed-refusal translation: McpError passes through; NSpawnError is wrapped;
# NotImplementedError (the not-yet-built L7 stub) is never caught here.
# --------------------------------------------------------------------------


def test_a_plain_nspawn_error_from_l7_is_wrapped_as_internal_error() -> None:
    fake = FakeSupervisor(close_result=NSpawnError(UNKNOWN, "opaque L7 failure"))
    server = McpServer(supervisor=fake)
    answer = ask(
        server,
        "tools/call",
        {"name": "nspawn_run_close", "arguments": {"run_id": "r1"}},
    )
    assert answer is not None
    assert answer["error"]["code"] == INTERNAL_ERROR
    assert "opaque L7 failure" in answer["error"]["message"]
    assert answer["error"]["data"]["tool"] == "nspawn_run_close"


def test_an_mcp_error_from_l7_passes_through_with_its_own_code() -> None:
    # SupervisorUnconfigured IS an McpError; it must not be double-wrapped into
    # a generic INTERNAL_ERROR message that loses its `missing_env` data.
    fake = FakeSupervisor(
        close_result=SupervisorUnconfigured("/fixture/nspawn.sqlite3", "absent")
    )
    server = McpServer(supervisor=fake)
    answer = ask(
        server,
        "tools/call",
        {"name": "nspawn_run_close", "arguments": {"run_id": "r1"}},
    )
    assert answer is not None
    assert answer["error"]["data"]["store"] == "/fixture/nspawn.sqlite3"


def _store_of_another_version() -> None:
    """A registry file this build refuses to open: L7's own typed refusal."""
    path = store()
    conn = nott_database.connect(path)
    try:
        conn.execute("PRAGMA query_only = OFF")
        conn.execute("PRAGMA user_version = 99")
    finally:
        conn.close()


def test_l7s_own_refusal_is_not_swallowed_by_l6(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # L7 refuses a registry of another schema version. L6 must not turn that
    # into a friendlier error it has not earned: it propagates the typed
    # refusal L7 raised, unchanged.
    monkeypatch.setenv("NOTT_NSPAWN_CONFIG", str(tmp_path / "ausente.toml"))
    monkeypatch.setenv("NOTT_NSPAWN_RUN_ROOT", str(tmp_path / "runs"))
    _store_of_another_version()
    server = McpServer()
    with pytest.raises(RegistryError, match="schema version 99"):
        server.nspawn_run_close("r1")


# --------------------------------------------------------------------------
# Response-size enforcement on the NEW dispatch path (not just `tool_result`).
# --------------------------------------------------------------------------


def test_unknown_tool_name_is_invalid_params() -> None:
    answer = ask(McpServer(), "tools/call", {"name": "spawn", "arguments": {}})
    assert answer is not None
    assert answer["error"] == {
        "code": INVALID_PARAMS,
        "message": "nSPAWN has no tool named 'spawn'",
    }


def test_non_object_arguments_are_invalid_params() -> None:
    answer = ask(
        McpServer(),
        "tools/call",
        {"name": "nspawn_run_show", "arguments": "not-an-object"},
    )
    assert answer is not None
    assert answer["error"]["code"] == INVALID_PARAMS


def test_absent_arguments_defaults_to_empty_object() -> None:
    # nspawn_result_show's only property is required, so an absent `arguments`
    # must still be caught as a missing-required refusal, not a crash on `None`.
    answer = ask(McpServer(), "tools/call", {"name": "nspawn_result_show"})
    assert answer is not None
    assert answer["error"]["code"] == INVALID_PARAMS


# --------------------------------------------------------------------------
# Default Supervisor construction from environment: lazy, and refuses by name.
# --------------------------------------------------------------------------


def test_a_missing_registry_file_is_a_typed_refusal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The door never creates the store: the refusal names the file it looked
    # for and the command that creates it.
    monkeypatch.setenv("NOTT_NSPAWN_CONFIG", str(tmp_path / "ausente.toml"))
    monkeypatch.delenv("NOTT_NSPAWN_RUN_ROOT", raising=False)
    server = McpServer()
    with pytest.raises(SupervisorUnconfigured) as caught:
        server.nspawn_run_close("r1")
    assert caught.value.data["store"] == str(store_path())
    # Once: the refusal from nott_database carries the cure; the door adds no copy.
    assert caught.value.rpc_message.count("`nspawn database create`") == 1
    assert not store_path().exists(), "the door created the store it refused"


def test_an_absent_run_root_falls_back_to_the_default_instead_of_refusing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # RUN_ROOT stopped being a refusal: a working path has an obvious place under
    # ~/.nott/state, and requiring the operator to declare it just for the server
    # to accept a call was ceremony with no consumer.
    monkeypatch.setenv("NOTT_NSPAWN_CONFIG", str(tmp_path / "ausente.toml"))
    monkeypatch.delenv("NOTT_NSPAWN_RUN_ROOT", raising=False)
    _store_of_another_version()
    server = McpServer()
    # Reaches L7 and refuses THERE (the store's version), never earlier for a
    # missing run root.
    with pytest.raises(RegistryError, match="schema version 99"):
        server.nspawn_run_close("r1")


def test_handshake_and_tools_list_never_need_a_supervisor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # LD-0's whole point: the handshake must work before L7 exists at all.
    monkeypatch.delenv("NOTT_NSPAWN_RUN_ROOT", raising=False)
    server = McpServer()
    main.build(server)
    assert len(main.TOOLS) == len(NSPAWN_TOOL_CONTRACTS)
    assert server.supervisor is None


# --------------------------------------------------------------------------
# Concurrency: the real dispatch path under contention, same falsifier shape
# as `test_the_wait_ceiling_sits_INSIDE_the_call_ceiling`.
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# Fuzzing: adversarial wire arguments must never raise an UNTYPED exception.
# A fixed, versioned corpus plus a derandomized (fixed-seed) hypothesis pass.
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _StandInFleetRun:
    """One row of the fleet listing, in the shape the contract names it.

    Age is DERIVED from the page's `observed_at` minus this `created_at`, so the
    listing carries one clock instead of two that can disagree.
    """

    run_id: str
    lane: str | None
    phase: RunPhase
    executed_provider: str | None
    tokens: int
    created_at: int


@dataclass(frozen=True, slots=True)
class _StandInFleetPage:
    """What `Supervisor.fleet()` will answer. L7 does not carry it yet.

    L6's whole job with that answer is `_to_jsonable`, which walks ANY dataclass,
    so this stand-in proves the routing and the rendering without the real type
    existing. The day `FleetPage` lands, this is replaced by an import — and
    nothing in the assertions below has to change, because none of them names a
    field the real page will not carry.
    """

    observed_at: int
    runs: tuple[_StandInFleetRun, ...]


def _fleet_page() -> _StandInFleetPage:
    return _StandInFleetPage(
        observed_at=1_787_890_100,
        runs=(
            _StandInFleetRun(
                run_id="r1",
                lane="lane/spawn-q2-mcp",
                phase=RunPhase.RUNNING,
                executed_provider="claude",
                tokens=1234,
                created_at=1_787_890_000,
            ),
            _StandInFleetRun(
                run_id="r2",
                lane=None,
                phase=RunPhase.COMPLETED,
                executed_provider=None,
                tokens=0,
                created_at=1_787_889_000,
            ),
        ),
    )


def _provider_report() -> ProviderReport:
    """Both account shapes, built through the real constructor so the
    `accounts`/`absence` iff is enforced on the way in rather than asserted
    against a hand-shaped dict the door would never see."""
    return ProviderReport(
        providers=(
            ProviderListing(
                provider="claude",
                kind="claude",
                capabilities=("resume", "review"),
                models=("claude-opus-5", "claude-sonnet-5"),
                default_model="claude-opus-5",
                efforts=("low", "max"),
                accounts=("guara-owner-invalid", "strife"),
                absence=None,
                strength=9,
                thrift=None,
                design=None,
            ),
            ProviderListing(
                provider="minimax",
                kind="claude",
                capabilities=("resume", "review"),
                models=("MiniMax-M3",),
                default_model="MiniMax-M3",
                efforts=("max",),
                accounts=(),
                absence=(
                    "provider 'minimax' declares no secrets directory; "
                    "it authenticates from the environment"
                ),
                strength=None,
                thrift=9,
                design=None,
            ),
        ),
        roles={"reviewer": "minimax"},
        role_profiles={"reviewer": {"effort": "max"}},
    )


def _quota_report() -> QuotaReport:
    """One report reaching all five states and every nesting level.

    Built through the real constructors on purpose: the `windows`/`absence` iff
    invariant means a report that BUILDS is already a legal one, so this fixture
    cannot drift into a shape L7 would refuse to produce.
    """
    return QuotaReport(
        observed_at=1_787_890_100,
        providers=(
            ProviderQuota(
                provider="claude",
                models=("opus", "sonnet"),
                state=QuotaState.OBSERVED,
                default_model="opus",
                accounts=(
                    AccountQuota(
                        account_alias="guara-owner-invalid",
                        state=QuotaState.OBSERVED,
                        windows=(
                            WindowQuota(
                                window="five_hour",
                                utilization=0.16,
                                resets_at=1_787_896_800,
                                observed_at=1_787_890_000,
                                state=QuotaState.OBSERVED,
                            ),
                            WindowQuota(
                                window="seven_day",
                                utilization=0.91,
                                resets_at=1_788_400_000,
                                observed_at=1_787_800_000,
                                state=QuotaState.STALE,
                            ),
                        ),
                        source="account_usage",
                        fetched_at=1_787_890_000,
                    ),
                    AccountQuota(
                        account_alias="never-run",
                        state=QuotaState.NEVER_OBSERVED,
                        absence="no reading has ever arrived for this account",
                    ),
                ),
            ),
            ProviderQuota(
                provider="env-route",
                models=("opus",),
                state=QuotaState.NO_SOURCE,
                absence="an env-route provider has no account to attribute to",
            ),
            ProviderQuota(
                provider="expired-route",
                models=("sonnet",),
                state=QuotaState.EXPIRED,
                windows=(
                    WindowQuota(
                        window="five_hour",
                        utilization=0.44,
                        resets_at=1_787_000_000,
                        observed_at=1_786_900_000,
                        state=QuotaState.EXPIRED,
                    ),
                ),
                source="account_usage",
                cooling_until=1_787_900_000,
                wall_source="vendor_reset",
            ),
        ),
    )


def _fuzzing_server() -> McpServer:
    ok_status = RunStatus(
        run_id="r1",
        phase=RunPhase.RUNNING,
        run_dir="/runs/r1",
        failure_reason=None,
        total_seq=0,
        requested_provider=None,
        executed_provider=None,
        executed_account=None,
        warning=None,
        tokens=0,
        marker=None,
    )
    fake = FakeSupervisor(
        execute_result=RunAccepted(run_id="r1"),
        review_result=RunAccepted(run_id="r1"),
        status_result=ok_status,
        result_result=RunResult(run_id="r1", final_text="PONG"),
        transcript_result=TranscriptPage(
            run_id="r1", since_seq=0, next_seq=0, lines=(), truncated=False
        ),
        close_result=CloseReadback(
            run_id="r1",
            phase=RunPhase.CLOSED,
            tree=ProcessTreeReadback(checked=(), survivors=()),
            claim_released=True,
        ),
        # BOTH ARE MANDATORY, NOT DECORATION. The property below drives
        # `sampled_from` over every contract name with `st.none()` among its
        # argument strategies, so `nspawn_quota_show` and an argument-less
        # `nspawn_run_show` both land here; an unconfigured result raises
        # NotImplementedError, which is precisely the untyped escape the fuzz
        # exists to catch, reported against the double instead of the door.
        quota_result=_quota_report(),
        fleet_result=_fleet_page(),
        # THE THIRD MANDATORY ONE, and it was not caught the way the contract
        # predicted. `FakeSupervisor` inherits from the real `Supervisor`, so
        # before `mcp_fakes` carried its own `providers` the fuzz ran the REAL
        # roster read against the ambient `HOME` and was green both against the
        # owner's file and against `HOME=/nonexistent-home` — an untyped escape
        # hidden behind an environment read, not a RED.
        providers_result=_provider_report(),
    )
    return McpServer(supervisor=fake)


@pytest.mark.parametrize(
    "case", FUZZ_CORPUS, ids=[entry["id"] for entry in FUZZ_CORPUS]
)
def test_fuzz_corpus_never_raises_an_untyped_exception(case: dict[str, object]) -> None:
    server = _fuzzing_server()
    answer = ask(
        server, "tools/call", {"name": case["tool"], "arguments": case["arguments"]}
    )
    assert answer is not None
    assert "result" in answer or "error" in answer


_JSON_SCALAR = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(2**31), max_value=2**31),
    st.text(max_size=40),
)
_JSON_VALUE = st.recursive(
    _JSON_SCALAR,
    lambda children: st.one_of(
        st.lists(children, max_size=4),
        st.dictionaries(st.text(max_size=10), children, max_size=4),
    ),
    max_leaves=8,
)
_ARGUMENTS = st.one_of(
    st.none(),
    _JSON_VALUE,
    st.dictionaries(
        st.sampled_from(
            [
                "prompt",
                "cwd",
                "run_id",
                "class",
                "timeout_ms",
                "since_seq",
                "branch",
                "provider",
                "vendor",
            ]
        ),
        _JSON_VALUE,
        max_size=6,
    ),
)


@given(st.sampled_from([c.name for c in NSPAWN_TOOL_CONTRACTS]), _ARGUMENTS)
@settings(
    max_examples=300, derandomize=True, suppress_health_check=[HealthCheck.too_slow]
)
def test_fuzz_property_arbitrary_arguments_never_raise_untyped(
    name: str, arguments: object
) -> None:
    server = _fuzzing_server()
    answer = ask(server, "tools/call", {"name": name, "arguments": arguments})
    assert answer is not None
    assert "result" in answer or "error" in answer


# --------------------------------------------------------------------------
# Whole-module branch coverage.
# --------------------------------------------------------------------------


def _one(name: str, value: object, spec: dict[str, Any]) -> None:
    validate_arguments({"type": "object", "properties": {name: spec}}, {name: value})


# --------------------------------------------------------------------------
# Mutation-closure: exact-value assertions for retained legacy code and for
# this file's own new helpers. A test that only checks "an error happened"
# lets a mutated message, key, or off-by-one boundary survive; every test
# below pins the EXACT text, key set, or boundary a mutation would move.
# --------------------------------------------------------------------------


def test_response_too_large_carries_internal_error_code() -> None:
    assert ResponseTooLarge(99).code == INTERNAL_ERROR


def test_supervisor_unconfigured_carries_internal_error_code_and_message() -> None:
    error = SupervisorUnconfigured("/state/nspawn.sqlite3", "no such file")
    assert error.code == INTERNAL_ERROR
    assert error.rpc_message == (
        "nspawn has no supervisor configured: the registry at /state/nspawn.sqlite3 "
        "cannot be opened (no such file)"
    )


def test_tool_result_boundary_exactly_at_the_cap_does_not_raise() -> None:
    mcp.tool_result("x" * mcp.MAX_MCP_RESPONSE_BYTES)


def test_tool_result_over_the_cap_names_the_actual_size() -> None:
    size = mcp.MAX_MCP_RESPONSE_BYTES + 1
    with pytest.raises(ResponseTooLarge) as caught:
        mcp.tool_result("x" * size)
    assert str(size) in caught.value.rpc_message


def test_bounded_result_uses_compact_separators_not_the_json_default() -> None:
    # A crafted payload whose COMPACT encoding sits exactly at the cap, but
    # whose json.dumps-DEFAULT (spaced) encoding would overflow it: any
    # separator mutation, or a `>=` boundary mutation, flips this to a raise.
    filler_count = 4000
    rendered: dict[str, Any] = {"lines": ["x"] * filler_count, "pad": ""}
    compact_len = len(json.dumps(rendered, separators=(",", ":")).encode("utf-8"))
    pad_len = mcp.MAX_RESPONSE_BYTES - compact_len
    assert pad_len > filler_count, (
        "corpus too small to bracket the cap; grow filler_count"
    )
    rendered["pad"] = "p" * pad_len
    compact_len = len(json.dumps(rendered, separators=(",", ":")).encode("utf-8"))
    assert compact_len == mcp.MAX_RESPONSE_BYTES
    default_len = len(json.dumps(rendered).encode("utf-8"))
    assert default_len > mcp.MAX_RESPONSE_BYTES
    # It admits the payload AND ships the compact bytes it just measured: the
    # cap would bound one encoding and send another if these two diverged.
    text = mcp._bounded_result(rendered)["content"][0]["text"]
    assert text == json.dumps(rendered, separators=(",", ":"))


def test_bounded_result_refuses_a_path_where_a_descriptor_is_required(
    tmp_path: Path,
) -> None:
    rendered: dict[str, Any] = {"pad": "x" * (mcp.MAX_RESPONSE_BYTES + 1)}
    with pytest.raises(mcp.McpError, match="missing run-root descriptor") as error:
        mcp._bounded_result(rendered, tmp_path)
    assert error.value.code == INTERNAL_ERROR


def test_validation_messages_name_the_argument_and_rule_never_the_value() -> None:
    with pytest.raises(mcp.McpError) as not_a_string:
        _one("prompt", 5, {"type": "string"})
    assert not_a_string.value.code == INVALID_PARAMS
    assert (
        not_a_string.value.rpc_message
        == "argument 'prompt' must satisfy type: 'string'"
    )

    with pytest.raises(mcp.McpError) as too_short:
        _one("prompt", "secret", {"type": "string", "minLength": 30})
    assert too_short.value.code == INVALID_PARAMS
    assert too_short.value.rpc_message == "argument 'prompt' must satisfy minLength: 30"

    with pytest.raises(mcp.McpError) as not_an_integer:
        _one("timeout_ms", 5.0, {"type": "integer"})
    assert (
        not_an_integer.value.rpc_message
        == "argument 'timeout_ms' must satisfy type: 'integer'"
    )

    with pytest.raises(mcp.McpError) as below_minimum:
        _one("timeout_ms", 1, {"type": "integer", "minimum": 5})
    assert (
        below_minimum.value.rpc_message
        == "argument 'timeout_ms' must satisfy minimum: 5"
    )

    with pytest.raises(mcp.McpError) as item:
        _one("providers", ["a", 2], {"type": "array", "items": {"type": "string"}})
    assert (
        item.value.rpc_message == "argument 'providers[1]' must satisfy type: 'string'"
    )


def test_minlength_defaults_to_zero_when_undeclared() -> None:
    # No "minLength" key at all: an empty string must still pass.
    _one("prompt", "", {"type": "string"})


def test_validate_arguments_properties_default_to_empty_when_undeclared() -> None:
    validate_arguments({"type": "object"}, {})


def test_validate_arguments_required_defaults_to_empty_when_undeclared() -> None:
    validate_arguments({"type": "object", "properties": {}}, {})


def test_validate_arguments_names_the_real_property_key_on_type_mismatch() -> None:
    schema = {
        "type": "object",
        "properties": {"prompt": {"type": "string"}},
        "required": [],
        "additionalProperties": False,
    }
    with pytest.raises(mcp.McpError) as caught:
        validate_arguments(schema, {"prompt": 123})
    assert "'prompt'" in caught.value.rpc_message


# --------------------------------------------------------------------------
# The sixth verb: nspawn_quota_show. Zero required arguments, and the whole report
# has to survive the render — `_to_jsonable` does NOT descend dict VALUES, so a
# dict-bearing field would reach `json.dumps` raw and raise inside
# `_bounded_result`, which never truncates and would not catch it either.
# --------------------------------------------------------------------------


def test_the_quota_contract_declares_two_optional_arguments_and_no_required() -> None:
    contract = NSPAWN_TOOL_BY_NAME["nspawn_quota_show"]
    assert contract.input_schema["required"] == []
    assert set(contract.input_schema["properties"]) == {"provider", "refresh"}
    assert contract.input_schema["properties"]["provider"]["type"] == "string"
    assert contract.input_schema["properties"]["refresh"]["type"] == "integer"


def test_quota_with_no_argument_at_all_reaches_the_supervisor() -> None:
    fake = FakeSupervisor(quota_result=_quota_report())
    answer = ask(
        McpServer(supervisor=fake),
        "tools/call",
        {"name": "nspawn_quota_show", "arguments": {}},
    )
    assert answer is not None
    assert fake.calls == [("quota", {"provider": None, "refresh": 0})]
    assert payload(answer)["observed_at"] == 1_787_890_100


def test_quota_forwards_the_named_provider_and_the_refresh_bound() -> None:
    fake = FakeSupervisor(quota_result=_quota_report())
    answer = ask(
        McpServer(supervisor=fake),
        "tools/call",
        {
            "name": "nspawn_quota_show",
            "arguments": {"provider": "claude", "refresh": 30},
        },
    )
    assert answer is not None and "result" in answer
    assert fake.calls == [("quota", {"provider": "claude", "refresh": 30})]


def test_a_non_string_provider_is_refused_before_the_supervisor_is_touched() -> None:
    fake = FakeSupervisor(quota_result=_quota_report())
    for arguments in ({"provider": 7}, {"provider": ""}, {"refresh": -1}, {"x": 1}):
        answer = ask(
            McpServer(supervisor=fake),
            "tools/call",
            {"name": "nspawn_quota_show", "arguments": arguments},
        )
        assert answer is not None
        assert answer["error"]["code"] == INVALID_PARAMS
    assert fake.calls == []


def test_the_WHOLE_quota_report_renders_as_plain_json() -> None:
    # THE DICT TRAP, driven end to end. Every state, every nesting level, and a
    # float, a None and an Enum on the way down.
    fake = FakeSupervisor(quota_result=_quota_report())
    answer = ask(
        McpServer(supervisor=fake),
        "tools/call",
        {"name": "nspawn_quota_show", "arguments": {}},
    )
    assert answer is not None
    rendered = payload(answer)
    # Plain JSON all the way down: a surviving dataclass or Enum raises here.
    assert json.loads(json.dumps(rendered)) == rendered
    providers = rendered["providers"]
    assert isinstance(providers, list)
    assert [p["provider"] for p in providers] == [
        "claude",
        "env-route",
        "expired-route",
    ]
    claude = providers[0]
    assert claude["models"] == ["opus", "sonnet"]
    assert claude["accounts"][0]["windows"][0]["state"] == "observed"
    assert claude["accounts"][0]["windows"][0]["utilization"] == 0.16
    assert claude["accounts"][0]["windows"][1]["state"] == "stale"
    assert claude["accounts"][1]["windows"] == []
    assert claude["accounts"][1]["state"] == "never_observed"
    assert claude["accounts"][1]["absence"]
    assert providers[1]["state"] == "no_source"
    assert providers[2]["windows"][0]["state"] == "expired"
    # Every one of the five states is a STRING here, never an Enum repr.
    seen = {p["state"] for p in providers}
    seen |= {a["state"] for a in claude["accounts"]}
    seen |= {w["state"] for w in claude["accounts"][0]["windows"]}
    assert seen == {state.value for state in QuotaState}


# --------------------------------------------------------------------------
# F-008: the fleet read is `nspawn_run_list`; it takes nothing and CANNOT long-poll.
# `nspawn_run_show` reads one run and is the only verb that may.
# --------------------------------------------------------------------------


def test_run_list_lists_the_fleet() -> None:
    fake = FakeSupervisor(fleet_result=_fleet_page())
    answer = ask(
        McpServer(supervisor=fake),
        "tools/call",
        {"name": "nspawn_run_list", "arguments": {}},
    )
    assert answer is not None
    assert fake.calls == [("fleet", {})]
    rendered = payload(answer)
    assert json.loads(json.dumps(rendered)) == rendered
    assert [run["run_id"] for run in rendered["runs"]] == ["r1", "r2"]
    first = rendered["runs"][0]
    assert first["lane"] == "lane/spawn-q2-mcp"
    assert first["phase"] == "running"
    assert first["executed_provider"] == "claude"
    assert first["tokens"] == 1234
    # Age is derivable from ONE clock, never carried twice.
    assert rendered["observed_at"] - first["created_at"] == 100


def test_run_show_reads_that_one_run() -> None:
    status = RunStatus(
        run_id="r1",
        phase=RunPhase.RUNNING,
        run_dir="/runs/r1",
        failure_reason=None,
        total_seq=0,
        requested_provider=None,
        executed_provider=None,
        executed_account=None,
        warning=None,
        tokens=0,
        marker=None,
    )
    fake = FakeSupervisor(status_result=status, fleet_result=_fleet_page())
    answer = ask(
        McpServer(supervisor=fake),
        "tools/call",
        {"name": "nspawn_run_show", "arguments": {"run_id": "r1"}},
    )
    assert answer is not None
    assert fake.calls == [("status", {"run_id": "r1", "timeout_ms": 0})]
    assert payload(answer)["run_id"] == "r1"


@pytest.mark.parametrize(
    ("tool", "arguments", "named"),
    [
        ("nspawn_run_list", {"timeout_ms": 5}, "timeout_ms"),
        ("nspawn_run_show", {"timeout_ms": 5}, "run_id"),
    ],
)
def test_a_timeout_with_no_run_to_poll_is_REFUSED_not_silently_dropped(
    tool: str, arguments: dict[str, int], named: str
) -> None:
    fake = FakeSupervisor(fleet_result=_fleet_page())
    answer = ask(
        McpServer(supervisor=fake),
        "tools/call",
        {"name": tool, "arguments": arguments},
    )
    assert answer is not None
    assert answer["error"]["code"] == INVALID_PARAMS
    assert named in answer["error"]["message"]
    # And it never reached the fleet read: a caller who asked to wait must not
    # get an instant listing back as if that were the same answer.
    assert fake.calls == []


def test_a_run_id_LESS_call_lists_or_is_refused_for_its_missing_run_id() -> None:
    """The listing answers, and the contradictory call (a `timeout_ms` with no run
    to poll) is refused for what it is."""
    fake = FakeSupervisor(fleet_result=_fleet_page())
    server = McpServer(supervisor=fake)
    listing = ask(server, "tools/call", {"name": "nspawn_run_list", "arguments": {}})
    assert listing is not None
    assert "result" in listing, listing

    malformed = ask(
        server,
        "tools/call",
        {"name": "nspawn_run_show", "arguments": {"timeout_ms": 5}},
    )
    assert malformed is not None
    assert malformed["error"]["code"] == INVALID_PARAMS, malformed
    assert "run_id" in malformed["error"]["message"]


def test_the_tools_are_exactly_the_noun_verb_names() -> None:
    """Every tool reads `nspawn_<noun>_<verb>`; the old names are not aliases."""
    assert set(NSPAWN_TOOL_BY_NAME) == {
        "nspawn_run_start",
        "nspawn_review_start",
        "nspawn_batch_start",
        "nspawn_run_list",
        "nspawn_run_show",
        "nspawn_result_show",
        "nspawn_transcript_show",
        "nspawn_run_close",
        "nspawn_route_ping",
        "nspawn_quota_show",
        "nspawn_usage_show",
        "nspawn_judgment_estimate",
        "nspawn_judgment_choose",
        "nspawn_judgment_score",
        "nspawn_provider_list",
    }


# --------------------------------------------------------------------------
# The provider list reads roster routes and native model metadata.
# It does not open a live Supervisor or its registry.
# --------------------------------------------------------------------------


def test_the_providers_contract_declares_one_optional_filter_and_no_required() -> None:
    contract = NSPAWN_TOOL_BY_NAME["nspawn_provider_list"]
    assert contract.input_schema["required"] == []
    assert set(contract.input_schema["properties"]) == {"provider"}
    assert contract.input_schema["properties"]["provider"]["type"] == "string"


def test_providers_with_no_argument_renders_the_WHOLE_listing_as_plain_json() -> None:
    fake = FakeSupervisor(providers_result=_provider_report())
    answer = ask(
        McpServer(supervisor=fake),
        "tools/call",
        {"name": "nspawn_provider_list", "arguments": {}},
    )
    assert answer is not None
    assert fake.calls == [("providers", {"provider": None})]
    rendered = payload(answer)
    # Plain JSON all the way down: a surviving dataclass or tuple raises here.
    assert json.loads(json.dumps(rendered)) == rendered
    listing = rendered["providers"]
    assert [entry["provider"] for entry in listing] == ["claude", "minimax"]
    assert listing[0]["models"] == ["claude-opus-5", "claude-sonnet-5"]
    assert listing[0]["default_model"] == "claude-opus-5"
    assert listing[0]["efforts"] == ["low", "max"]
    assert listing[0]["accounts"] == ["guara-owner-invalid", "strife"]
    assert listing[0]["absence"] is None
    # The ranks: without them the answer says what EXISTS and not what to CHOOSE.
    assert (listing[0]["strength"], listing[0]["thrift"], listing[0]["design"]) == (
        9,
        None,
        None,
    )
    # An empty account list is never bare: the reason sits beside it.
    assert listing[1]["accounts"] == []
    assert "environment" in listing[1]["absence"]
    # What a route implements and the role binding reach the wire: the workflow
    # refuses a role on their absence, so losing them would refuse every role.
    assert listing[0]["kind"] == "claude"
    assert listing[0]["capabilities"] == ["resume", "review"]
    assert rendered["roles"] == {"reviewer": "minimax"}
    assert rendered["role_profiles"] == {"reviewer": {"effort": "max"}}


def test_providers_forwards_the_named_filter() -> None:
    fake = FakeSupervisor(providers_result=_provider_report())
    answer = ask(
        McpServer(supervisor=fake),
        "tools/call",
        {"name": "nspawn_provider_list", "arguments": {"provider": "claude"}},
    )
    assert answer is not None and "result" in answer
    assert fake.calls == [("providers", {"provider": "claude"})]


def test_a_malformed_providers_filter_is_refused_before_the_supervisor() -> None:
    fake = FakeSupervisor(providers_result=_provider_report())
    for arguments in ({"provider": 7}, {"provider": ""}, {"refresh": 1}):
        answer = ask(
            McpServer(supervisor=fake),
            "tools/call",
            {"name": "nspawn_provider_list", "arguments": arguments},
        )
        assert answer is not None
        assert answer["error"]["code"] == INVALID_PARAMS
    assert fake.calls == []


def test_a_typed_l7_refusal_from_providers_still_renders_as_one() -> None:
    fake = FakeSupervisor(
        providers_result=NSpawnError(UNKNOWN, "'nope' is not declared in the roster")
    )
    answer = ask(
        McpServer(supervisor=fake),
        "tools/call",
        {"name": "nspawn_provider_list", "arguments": {"provider": "nope"}},
    )
    assert answer is not None
    assert answer["error"]["code"] == INTERNAL_ERROR
    assert answer["error"]["data"] == {
        "tool": "nspawn_provider_list",
        "type": "NSpawnError",
        "nerr": 9999,
    }


def test_the_roster_ANSWERS_WHILE_THE_STORE_IS_DEAD(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """THE ACCEPTANCE OF THIS VERB, end to end through the live door.

    No supervisor is injected, so this is the path a real MCP client takes, and
    the registry file does not exist. The
    listing must arrive anyway: an operator asking WHERE HE MAY DISPATCH is
    often asking because something is broken, and a routing answer a dead store
    can silence is a routing answer nobody can use.

    The second half is what makes the first half mean anything. Without it a
    green test could not tell a genuinely dead store from an accidentally
    healthy one, so the same door, in the same environment, is driven through
    `nspawn_quota_show` — which DOES need the registry — and must fail there.
    """
    home = tmp_path / "home"
    secrets = home / "secrets" / "claude"
    secrets.mkdir(parents=True)
    (secrets / "guara-owner-invalid.token").write_text("x", encoding="utf-8")
    (home / ".nott").mkdir(parents=True)
    (home / ".nott" / "spawn.toml").write_text(
        "[[provider]]\n"
        'name = "claude"\n'
        'kind = "claude"\n'
        f'secrets = "{secrets}"\n'
        'model = "claude-opus-5"\n'
        'models = ["claude-opus-5"]\n'
        "\n"
        "[[provider]]\n"
        'name = "minimax"\n'
        'kind = "claude"\n'
        'models = ["MiniMax-M3"]\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    # THE ROSTER HOME CHANGED, and this test still pointed to the old one. It
    # wrote the fixture to `$HOME/.nott/spawn.toml` — the derivation of
    # `NOTT_NSPAWN_HOME`, which #117 DELETED. After it, the roster resolves via the
    # single file `.nott/spawn.toml`, or via the env in its absence, and nothing else;
    # therefore the fixture was written to a place no one reads and the port
    # responded `nERR-1104` instead of the page this case exists to prove.
    #
    # Declared via FILE and not via env, on purpose: it is the path that
    # wins under the new rule, so it is what this test must exercise.
    spawn_config = tmp_path / "spawn.toml"
    spawn_config.write_text(
        (home / ".nott" / "spawn.toml").read_text(), encoding="utf-8"
    )
    monkeypatch.setenv("NOTT_NSPAWN_CONFIG", str(spawn_config))
    monkeypatch.delenv("NOTT_NSPAWN_ROSTER", raising=False)
    server = McpServer()

    answer = ask(
        server, "tools/call", {"name": "nspawn_provider_list", "arguments": {}}
    )

    assert answer is not None, answer
    assert "result" in answer, answer
    listing = payload(answer)["providers"]
    assert [entry["provider"] for entry in listing] == ["claude", "minimax"]
    assert listing[0]["accounts"] == ["guara-owner-invalid"]
    assert listing[1]["absence"]
    # Nothing was built: a Supervisor cached here would prove the store was
    # reached and merely happened to answer.
    assert server.supervisor is None

    dead = ask(server, "tools/call", {"name": "nspawn_quota_show", "arguments": {}})

    assert dead is not None
    assert dead["error"]["code"] == INTERNAL_ERROR, dead
    assert "cannot be opened" in dead["error"]["message"], dead


# --------------------------------------------------------------------------
# F-001/F-016: the instructions are DERIVED, so they cannot announce a verb
# that does not exist or omit one that does.
# --------------------------------------------------------------------------

#: Every `nspawn_*` token the instructions actually spell.
_CITED_TOOLS = set(re.findall(r"nspawn_[a-z_]+", mcp.SERVER_INSTRUCTIONS))


def test_the_instructions_cite_every_tool_that_exists_and_no_other() -> None:
    # The hand-written string announced spawn, status, wait, send, transcript,
    # close and operation_lookup — four of which never existed on this surface,
    # while nspawn_review_start existed unannounced. A cold agent plans against that
    # vocabulary and pays one failed call per invented verb.
    assert _CITED_TOOLS == {c.name for c in NSPAWN_TOOL_CONTRACTS}


def test_the_instructions_name_the_retired_legacy_verbs_nowhere() -> None:
    for dead in ("wait", "send", "operation_lookup"):
        assert f" {dead}," not in mcp.SERVER_INSTRUCTIONS
        assert f" {dead}." not in mcp.SERVER_INSTRUCTIONS


def test_the_instructions_keep_the_pinned_prefix_and_describe_the_fleet_read() -> None:
    assert mcp.SERVER_INSTRUCTIONS.startswith("nSPAWN controls executors")
    assert "nspawn_run_list lists the fleet" in mcp.SERVER_INSTRUCTIONS


# --------------------------------------------------------------------------
# Q5/F-005: the schema CARRIES the routing vocabulary, so a cold caller stops
# reading the owner's roster off the disk to learn it — and `class`, whose
# vocabulary is a code constant, is refused by name instead of degrading the
# routing in silence.
# --------------------------------------------------------------------------

_EXECUTE_PROPERTIES = _EXECUTE_SCHEMA["properties"]
_REVIEW_PROPERTIES = NSPAWN_TOOL_BY_NAME["nspawn_review_start"].input_schema[
    "properties"
]

#: The roster as measured on 2026-09-20. The description is a SNAPSHOT of owner
#: data, so this is what a cold caller must be able to read out of `tools/list`.
#: The 2026-08-31 snapshot had drifted into the exact defect it warns about:
#: it named grok, minimax, servervllm and m1, none of which the roster has had
#: a `[[provider]]` block for, and omitted qwen, which it does.
_ROSTER_SNAPSHOT = (
    "claude",
    "fugu",
    "agy",
    "codex",
    "zai",
    "qwen",
)


def test_the_class_enum_is_exactly_the_vocabulary_ranks_owns() -> None:
    # The literal pin: a ninth value in `ranks.py` — or an unrelated
    # uppercase string landing in that module — turns this RED by name.
    assert list(mcp.EXECUTION_CLASSES) == [
        "judgment",
        "planning",
        "review",
        "mechanical_fanout",
        "design",
        "verification",
        "deterministic",
        "implementation",
    ]
    # And READ, never retyped: the same eight, taken from `ranks.py` itself.
    assert set(mcp.EXECUTION_CLASSES) == {
        ranks.JUDGMENT,
        ranks.PLANNING,
        ranks.REVIEW,
        ranks.IMPLEMENTATION,
        ranks.MECHANICAL_FANOUT,
        ranks.DESIGN,
        ranks.VERIFICATION,
        ranks.DETERMINISTIC,
    }
    # `_AXES` is the RANKED subset, never the vocabulary: `deterministic` is a
    # class precisely by being absent from it, so an enum built from that table
    # would refuse a class the router accepts.
    assert ranks.DETERMINISTIC not in ranks._AXES


def test_tools_list_publishes_the_closed_class_vocabulary() -> None:
    execute = next(
        tool.model_dump(by_alias=True)
        for tool in main.TOOLS
        if tool.name == "nspawn_run_start"
    )
    assert execute["inputSchema"]["properties"]["class"]["enum"] == list(
        mcp.EXECUTION_CLASSES
    )


def test_no_contract_can_ANNOUNCE_an_enum_it_does_not_enforce() -> None:
    """THE TRAP, closed by derivation. A validator that understood only `string`
    and `integer` would let an `enum` sitting in a schema be published to every
    client and checked by nothing — a contract that announces and does not
    refuse, which is the same lie this work exists to remove. Derived over every
    declared property, so a SECOND enum cannot arrive unenforced either.
    """
    enforced = 0
    for contract in NSPAWN_TOOL_CONTRACTS:
        for name, spec in contract.input_schema["properties"].items():
            if "enum" not in spec:
                continue
            enforced += 1
            with pytest.raises(mcp.McpError) as caught:
                _one(name, "definitely-outside-any-vocabulary", spec)
            assert caught.value.code == INVALID_PARAMS
    assert enforced == 1, "one enum is declared today; a new one must be enforced too"


def test_a_class_outside_the_vocabulary_is_REFUSED_naming_the_eight() -> None:
    fake = FakeSupervisor(execute_result=RunAccepted(run_id="r1"))
    answer = ask(
        McpServer(supervisor=fake),
        "tools/call",
        {
            "name": "nspawn_run_start",
            # The measured failure mode: a plausible spelling, not garbage. It
            # used to reach `rank_providers`, miss `_AXES`, and route by name.
            "arguments": {"prompt": "hi", "cwd": "/tmp", "class": "judgement"},
        },
    )
    assert answer is not None
    assert answer["error"]["code"] == INVALID_PARAMS
    message = answer["error"]["message"]
    for declared in mcp.EXECUTION_CLASSES:
        assert declared in message
    # Refused BEFORE dispatch: a typo must not become a run at all.
    assert fake.calls == []


@pytest.mark.parametrize("declared", mcp.EXECUTION_CLASSES)
def test_every_declared_class_still_reaches_the_supervisor(declared: str) -> None:
    fake = FakeSupervisor(execute_result=RunAccepted(run_id="r1"))
    answer = ask(
        McpServer(supervisor=fake),
        "tools/call",
        {
            "name": "nspawn_run_start",
            "arguments": {"prompt": "hi", "cwd": "/tmp", "class": declared},
        },
    )
    assert answer is not None and "result" in answer, answer
    ((_, kwargs),) = fake.calls
    assert kwargs["execution_class"] == declared


def test_an_ABSENT_class_is_REFUSED_so_no_dispatch_escapes_classification() -> None:
    """`class` is REQUIRED. An absent class used to mean "no preference" and
    fell through to the default provider, which is the expensive one — the hole
    this closes at the schema instead of at the picker.
    """
    fake = FakeSupervisor(execute_result=RunAccepted(run_id="r1"))
    answer = ask(
        McpServer(supervisor=fake),
        "tools/call",
        {"name": "nspawn_run_start", "arguments": {"prompt": "hi", "cwd": "/tmp"}},
    )
    assert answer is not None
    assert answer["error"]["code"] == INVALID_PARAMS
    assert answer["error"]["message"] == "'class' is a required property"
    # Refused BEFORE dispatch: an unclassified run must not exist at all.
    assert fake.calls == []


#: The node host's gist: a tool-free, read-only inference on a pinned route.
_GIST = {
    "read_only": True,
    "provider": "claude",
    "model": "m",
    "effort": "low",
    "inference_profile": {"tools": "none", "output_schema": {"type": "object"}},
}


@pytest.mark.parametrize(
    ("changes", "admitted"),
    [
        ({}, True),
        ({"inference_profile": None}, False),
        ({"native_write": True}, False),
        ({"read_only": False}, False),
        ({"read_only": None}, False),
        ({"model": None}, False),
        ({"effort": None}, False),
        ({"provider": None}, False),
    ],
)
def test_only_a_pinned_tool_free_read_only_inference_omits_its_class(
    changes: dict[str, object], admitted: bool
) -> None:
    """The gist's call (no tool, no write, route, model and effort pinned) may
    omit `class`: it leaves nothing to route or price. Any other call without a
    class is refused at the door, a tool-using or writing run included."""
    arguments = {"prompt": "hi", "cwd": "/tmp", **_GIST, **changes}
    arguments = {key: value for key, value in arguments.items() if value is not None}

    class Inferring(FakeSupervisor):
        """Takes the gist's own fields, which the shared fake does not."""

        def execute(  # type: ignore[override]
            self,
            prompt: str,
            cwd: str,
            *,
            inference_profile: dict[str, Any] | None = None,
            native_write: bool = False,
            **rest: Any,
        ) -> RunAccepted:
            return super().execute(prompt, cwd, **rest)

    fake = Inferring(execute_result=RunAccepted(run_id="r1"))
    answer = ask(
        McpServer(supervisor=fake),
        "tools/call",
        {"name": "nspawn_run_start", "arguments": arguments},
    )
    assert answer is not None
    if admitted:
        assert "result" in answer, answer
        ((_, kwargs),) = fake.calls
        assert kwargs["execution_class"] is None
    else:
        assert answer["error"]["message"] == "'class' is a required property"
        assert fake.calls == []


def test_a_cold_caller_names_agy_FROM_TOOLS_LIST_ALONE() -> None:
    """THE ACCEPTANCE OF THIS UNIT. No supervisor, no roster read, no `HOME`:
    only the frame every MCP client already receives at handshake. The friction
    this closes was measured — an agent asked to dispatch "CODEX + AGY" ran
    `cat ~/.nott/spawn.toml` and `grep -A12 'name = "agy"'` first, and was
    right to, because these arguments said nothing.
    """
    execute = next(
        tool.model_dump(by_alias=True)
        for tool in main.TOOLS
        if tool.name == "nspawn_run_start"
    )
    described = execute["inputSchema"]["properties"]["provider"]["description"]
    for name in _ROSTER_SNAPSHOT:
        assert name in described, name
    # And the LIVE source is named, because the snapshot above is owner data.
    assert "nspawn_provider_list" in described


def test_provider_carries_NO_enum_so_the_schema_can_never_refuse_a_real_route() -> None:
    """The decision behind the description, stated as a check.

    An enum over the roster is a SNAPSHOT enforcing what a live file already
    enforces: `picker.PickerError.unknown_provider` refuses an undeclared name
    against the roster read at dispatch. A snapshot can only add a refusal the
    live gate would not make — of a provider the owner added afterwards.
    """
    assert "enum" not in _EXECUTE_PROPERTIES["provider"]
    fake = FakeSupervisor(execute_result=RunAccepted(run_id="r1"))
    answer = ask(
        McpServer(supervisor=fake),
        "tools/call",
        {
            "name": "nspawn_run_start",
            # A name no snapshot could carry: it does not exist yet.
            "arguments": {
                "prompt": "hi",
                "cwd": "/tmp",
                "class": "mechanical_fanout",
                "provider": "provider-novo",
            },
        },
    )
    assert answer is not None and "result" in answer, answer
    ((_, kwargs),) = fake.calls
    assert kwargs["provider"] == "provider-novo"


def test_model_and_effort_say_WHERE_their_vocabulary_lives_and_close_nothing() -> None:
    for key in ("model", "effort"):
        spec = _EXECUTE_PROPERTIES[key]
        # A single global enum would lie: both vocabularies are per-provider.
        assert "enum" not in spec
        assert "per-provider" in spec["description"]
        assert "nspawn_provider_list" in spec["description"]


def test_review_carries_THE_SAME_routing_sentences_as_execute() -> None:
    # One argument, one meaning, one source: three bespoke strings would drift.
    for key in ("provider", "model", "effort"):
        assert _REVIEW_PROPERTIES[key] == _EXECUTE_PROPERTIES[key]


def test_writer_descriptions_name_every_kind_that_can_write() -> None:
    """The `tools/list` text is the only routing source a caller reads. When it
    said "Codex only", callers sent write work to their own client instead of
    nSPAWN, although a claude route can write since #942."""
    writers = sorted(k for k, caps in CAPABILITIES.items() if "write" in caps)
    assert len(writers) > 1, writers
    listed = {
        tool.name: tool.model_dump(by_alias=True)
        for tool in main.TOOLS
        if tool.name in {"nspawn_run_start", "nspawn_review_start"}
    }
    texts = {
        "native_write": listed["nspawn_run_start"]["inputSchema"]["properties"][
            "native_write"
        ]["description"],
        "nspawn_run_start": listed["nspawn_run_start"]["description"],
        "nspawn_review_start": listed["nspawn_review_start"]["description"],
    }
    for where, text in texts.items():
        lowered = text.lower()
        for kind in writers:
            # A refusal of a writer kind, or a writer bound to one kind only.
            assert f"non-{kind} route" not in lowered, (where, kind)
            assert not re.search(rf"\bon an? {kind} route\b", lowered), (where, kind)
            assert not re.search(rf"\bone {kind} turn\b", lowered), (where, kind)
    for where in ("native_write", "nspawn_review_start"):
        for kind in writers:
            assert re.search(rf"\b{kind}\b", texts[where].lower()), (where, kind)
    # `class` is execute's alone — a review IS the `review` class, pinned by L7.
    assert "class" not in _REVIEW_PROPERTIES


def test_batch_unknown_provider_uses_resolver_wording_before_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "actual-roster.toml"
    source.write_text('[[provider]]\nname = "x"\n')
    fake = FakeSupervisor()
    monkeypatch.setattr(
        FakeSupervisor, "declared_providers", Supervisor.declared_providers
    )
    with (
        patch.object(Roster, "default_path", return_value=source) as resolve,
        patch.object(Roster, "read", wraps=Roster.read) as read,
    ):
        answer = ask(
            McpServer(supervisor=fake),
            "tools/call",
            {
                "name": "nspawn_batch_start",
                "arguments": {
                    "prompt": "inspect",
                    "cwd": "/dispatch/worktree",
                    "providers": ["x", "nope-io"],
                },
            },
        )
    resolve.assert_called_once_with(Path("/dispatch/worktree"))
    read.assert_called_once_with(source)
    assert fake.calls == []
    assert answer == {
        "jsonrpc": "2.0",
        "id": 1,
        "error": {
            "code": INVALID_PARAMS,
            "message": 'no Claude provider or account named "nope-io" is declared '
            f"in the roster; it declares [x]; roster source: {source}",
        },
    }


def test_dedup_properties_serve_exactly_this_contract() -> None:
    # The text a caller reads is the contract: pinned whole, so a changed word is a
    # changed contract here too (mutation gate on #850, 2026-09-26: 53 survivors).
    assert mcp._dedup_properties() == {
        "idempotency_key": {
            "type": "string",
            "minLength": 1,
            "maxLength": mcp.DEDUP_TEXT_MAX_CHARS,
            "description": (
                "Dedup key, scoped to the calling project. The same key with the "
                "same request reattaches to its run (`deduplicated: true` and the "
                "run's current phase) instead of dispatching again; the same key "
                "with a different request is refused as IdempotencyConflict. "
                "The request is compared as sent: omitting provider, model or "
                "effort and naming its default are different requests. "
                "Holds until the run is closed, then 24 h. No `/`: a batch keys "
                "its entries `key/index`."
            ),
        },
        "group": {
            "type": "string",
            "minLength": 1,
            "maxLength": mcp.DEDUP_TEXT_MAX_CHARS,
            "description": (
                "A label the run carries, read back by nspawn_run_show. It "
                "must not start with `swarm:`."
            ),
        },
    }
