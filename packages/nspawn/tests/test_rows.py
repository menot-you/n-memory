"""The durable run vocabulary."""

import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

from nspawn import rows
from nspawn.limits import (
    MAX_DURABLE_IDENTIFIER_BYTES,
    MAX_DURABLE_ROW_BYTES,
    MAX_LAST_ITEM_BYTES,
)
from nspawn.process_identity import ProcessIdentity
from nspawn.rows import (
    APPROVAL_POLICY_VALUES,
    CLAUDE_CONFINEMENT_POLICY_ONLY,
    IMMUTABLE_COLUMNS,
    INSERT_COLUMNS,
    OBSERVATION_COLUMNS,
    RUN_COLUMNS,
    RUN_EVENT_COLUMNS,
    RUN_EVENT_KIND_VALUES,
    SHA256_HEX_LENGTH,
    UPDATE_COLUMNS,
    Backend,
    RunEventRow,
    RunPage,
    RunPhase,
    RunRow,
    WorktreeClaim,
)


def row(**overrides: object) -> RunRow:
    base: dict[str, object] = {
        "run_id": "r-1",
        "worktree": "/tmp/wt",
        "phase": RunPhase.RUNNING,
        "sandbox": "read-only",
        "model": "opus",
        "owner_instance": "i-1",
        "created_at": 1,
        "updated_at": 2,
        "backend": Backend.CLAUDE,
    }
    base.update(overrides)
    return RunRow(**base)  # type: ignore[arg-type]


def event(**overrides: object) -> RunEventRow:
    base: dict[str, object] = {
        "run_id": "r-1",
        "seq": 1,
        "at": 10,
        "kind": "lifecycle",
        "payload": {"phase": "running"},
        "chained_hash": "a" * SHA256_HEX_LENGTH,
    }
    base.update(overrides)
    return RunEventRow(**base)  # type: ignore[arg-type]


def test_exactly_four_phases_are_terminal() -> None:
    terminal = {p.value for p in RunPhase if p.is_terminal()}
    assert terminal == {"orphaned", "completed", "failed", "closed"}
    assert not RunPhase.RUNNING.is_terminal()
    assert not RunPhase.WAITING_APPROVAL.is_terminal()


def test_every_phase_the_column_admits_exists_and_no_more() -> None:
    assert {p.value for p in RunPhase} == {
        "pending",
        "spawning",
        "running",
        "waiting_approval",
        "idle",
        "interrupted",
        "recovering",
        "orphaned",
        "completed",
        "failed",
        "closed",
    }


def test_backend_parse_is_the_exact_inverse_of_as_str() -> None:
    for backend in Backend:
        assert Backend.parse(backend.as_str()) is backend
    assert {b.as_str() for b in Backend} == {"codex", "claude", "grok", "agy"}
    assert Backend.parse("Codex") is None
    assert Backend.parse("") is None
    # Historical rows stay readable even though new runs currently choose Claude.
    assert Backend.parse("codex") is Backend.CODEX
    assert Backend.parse("grok") is Backend.GROK
    assert Backend.parse("agy") is Backend.AGY


def test_run_event_columns_are_insert_only_by_construction() -> None:
    assert RUN_EVENT_COLUMNS == (
        "run_id",
        "seq",
        "at",
        "kind",
        "payload",
        "chained_hash",
    )
    assert not hasattr(rows, "RUN_EVENT_UPDATE_COLUMNS")


def test_run_event_kind_is_closed_and_refusal_never_echoes_input() -> None:
    assert RUN_EVENT_KIND_VALUES == (
        "lifecycle",
        "item",
        "approval",
        "output",
        "file_change",
        "error",
        "warning",
        "usage",
        "recovery",
    )
    for kind in RUN_EVENT_KIND_VALUES:
        assert event(kind=kind).kind == kind
    rejected = "/home/test-user/.nott/secrets/event-kind.token"
    with pytest.raises(ValueError, match="kind") as excinfo:
        event(kind=rejected)
    assert rejected not in str(excinfo.value)


def test_run_event_constructor_refuses_invalid_sequence_time_payload_and_hash() -> None:
    with pytest.raises(ValueError, match="seq"):
        event(seq=0)
    with pytest.raises(TypeError, match="seq"):
        event(seq=True)
    with pytest.raises(ValueError, match="at"):
        event(at=-1)
    with pytest.raises(TypeError, match="at"):
        event(at=False)
    with pytest.raises(ValueError, match="payload"):
        event(payload={"value": object()})
    with pytest.raises(ValueError, match="payload"):
        event(payload={"value": "x" * MAX_DURABLE_ROW_BYTES})
    with pytest.raises(ValueError, match="chained_hash"):
        event(chained_hash="a" * (SHA256_HEX_LENGTH - 1))
    with pytest.raises(ValueError, match="chained_hash"):
        event(chained_hash="z" * SHA256_HEX_LENGTH)


def test_run_event_payload_bound_uses_jsonb_numeric_width() -> None:
    compact_number = 1e308
    compact_item_bytes = len(json.dumps(compact_number)) + len(", ")
    payload = [compact_number] * (MAX_DURABLE_ROW_BYTES // compact_item_bytes)

    assert len(json.dumps(payload).encode("utf-8")) <= MAX_DURABLE_ROW_BYTES
    with pytest.raises(ValueError, match="payload"):
        event(payload=payload)


def test_run_event_payload_accepts_json_and_refuses_every_non_json_edge() -> None:
    payload = {"values": [None, True, 1, 1.5, {"nested": "ok"}]}
    assert event(payload=payload).payload is payload

    for rejected in (
        {"number": float("nan")},
        {"number": float("inf")},
        {1: "non-string key"},
        ("tuple",),
    ):
        with pytest.raises(ValueError, match="payload"):
            event(payload=rejected)

    cyclic: list[object] = []
    cyclic.append(cyclic)
    with pytest.raises(ValueError, match="payload"):
        event(payload=cyclic)


def test_run_event_canonical_line_is_stable_across_payload_key_order() -> None:
    first = event(payload={"b": 2, "a": 1})
    second = event(payload={"a": 1, "b": 2})
    assert first.canonical_line() == second.canonical_line()
    assert first.canonical_line() == (
        '{"at":10,"kind":"lifecycle","payload":{"a":1,"b":2},"run_id":"r-1","seq":1}'
    )


def test_the_pid_triple_is_one_value_so_half_of_it_cannot_exist() -> None:
    identity = ProcessIdentity(pid=42, boot_id="b", start_ticks=7)
    assert row(process_identity=identity).process_identity == identity
    assert row().process_identity is None
    # There is no way to name a pid without a boot id: the field is the triple.
    assert not hasattr(row(), "process_pid")


def test_the_sandbox_admits_policy_only_which_is_not_an_executor_sandbox() -> None:
    assert row(sandbox=CLAUDE_CONFINEMENT_POLICY_ONLY).sandbox == "policy-only"
    with pytest.raises(ValueError, match="sandbox"):
        row(sandbox="workspaceWrite")


def test_the_column_check_sets_are_enforced_in_the_constructor() -> None:
    assert APPROVAL_POLICY_VALUES == ("untrusted", "on-request", "never")
    with pytest.raises(ValueError, match="approval_policy"):
        row(approval_policy="always")
    with pytest.raises(ValueError, match="marker"):
        row(marker="MAYBE")
    for marker in ("DONE", "BLOCKED"):
        assert row(marker=marker).marker == marker


def test_bounds_come_from_limits_and_reject_at_the_edge() -> None:
    edge = "x" * MAX_DURABLE_IDENTIFIER_BYTES
    assert row(run_id=edge).run_id == edge
    with pytest.raises(ValueError, match="run_id"):
        row(run_id=edge + "x")
    with pytest.raises(ValueError, match="run_id"):
        row(run_id="")
    # A capped column has no lower bound: empty is legal, oversized is not.
    assert row(last_item="").last_item == ""
    with pytest.raises(ValueError, match="last_item"):
        row(last_item="x" * (MAX_LAST_ITEM_BYTES + 1))


def test_bounds_are_measured_in_bytes_not_characters() -> None:
    # A multi-byte identifier that fits in characters but not in bytes must be
    # refused, because the column's CHECK casts to BLOB.
    too_long = "é" * MAX_DURABLE_IDENTIFIER_BYTES
    assert len(too_long) == MAX_DURABLE_IDENTIFIER_BYTES
    with pytest.raises(ValueError, match="run_id"):
        row(run_id=too_long)


def test_counters_refuse_negatives_and_booleans() -> None:
    with pytest.raises(ValueError, match="tokens"):
        row(tokens=-1)
    with pytest.raises(TypeError, match="turn_count"):
        row(turn_count=True)
    with pytest.raises(ValueError, match="process_group"):
        row(process_group=0)


def test_the_observation_pair_is_excluded_from_every_write() -> None:
    assert OBSERVATION_COLUMNS == ("last_record_at", "records_seen")
    for column in OBSERVATION_COLUMNS:
        assert column in RUN_COLUMNS
        assert column not in INSERT_COLUMNS
        assert column not in UPDATE_COLUMNS


def test_the_immutable_columns_are_excluded_from_updates_but_not_inserts() -> None:
    assert IMMUTABLE_COLUMNS == (
        "run_id",
        "worktree",
        "created_at",
        "backend",
        "owner",
    )
    for column in IMMUTABLE_COLUMNS:
        assert column in INSERT_COLUMNS
        assert column not in UPDATE_COLUMNS


def test_v7_split_the_provider_field_into_requested_and_executed() -> None:
    r = row(
        requested_provider="anthropic",
        executed_provider="anthropic",
        executed_account="acct-1",
    )
    assert r.requested_provider == "anthropic"
    assert r.executed_provider == "anthropic"
    assert r.executed_account == "acct-1"
    # All three are optional: None means UNKNOWN, never the roster default.
    absent = row()
    assert absent.requested_provider is None
    assert absent.executed_provider is None
    assert absent.executed_account is None
    for field in ("requested_provider", "executed_provider", "executed_account"):
        with pytest.raises(ValueError, match=field):
            row(**{field: "x" * (MAX_DURABLE_IDENTIFIER_BYTES + 1)})


@pytest.mark.parametrize(
    "alias",
    ["guara-owner-invalid", "strife", "tiago-menot-invalid", "acct-1", "claude"],
)
def test_executed_account_accepts_real_roster_alias_shapes(alias: str) -> None:
    assert row(executed_account=alias).executed_account == alias


def test_executed_account_refuses_a_credential_path() -> None:
    # A caller-controlled token path must never round-trip into the durable
    # roster-alias column, and the refusal must never echo it back.
    token_path = "/home/test-user/.nott/secrets/claude/guara-owner-invalid.token"
    with pytest.raises(ValueError, match="executed_account") as excinfo:
        row(executed_account=token_path)
    assert token_path not in str(excinfo.value)


def test_executed_account_refuses_token_bytes() -> None:
    sentinel = "sk-ant-oat01-" + "Z" * 80
    with pytest.raises(ValueError, match="executed_account") as excinfo:
        row(executed_account=sentinel)
    assert sentinel not in str(excinfo.value)


def test_executed_account_refuses_a_grammatical_but_oversized_secret() -> None:
    # An all-lowercase-hex secret is shaped exactly like the alias grammar and
    # would slip past a regex-only check; the alias length ceiling is what
    # keeps a real credential from riding through under this cover.
    sentinel = "a1b2c3d4" * 8
    with pytest.raises(ValueError, match="executed_account") as excinfo:
        row(executed_account=sentinel)
    assert sentinel not in str(excinfo.value)


#: ASCII printable text is exactly one byte per character in UTF-8, so its
#: character length IS its byte length — the property below can pick a size
#: relative to the byte ceiling without a conditional branch in the test body.
_ASCII_TEXT = st.text(
    alphabet=st.characters(min_codepoint=0x20, max_codepoint=0x7E), min_size=1
)


@given(_ASCII_TEXT.filter(lambda s: len(s) <= MAX_DURABLE_IDENTIFIER_BYTES))
def test_requested_provider_within_bounds_is_always_accepted(value: str) -> None:
    assert row(requested_provider=value).requested_provider == value


@given(
    _ASCII_TEXT.map(
        lambda s: (s * (MAX_DURABLE_IDENTIFIER_BYTES + 1))[
            : MAX_DURABLE_IDENTIFIER_BYTES + 1
        ]
    )
)
def test_requested_provider_over_the_ceiling_is_always_refused(value: str) -> None:
    with pytest.raises(ValueError, match="requested_provider"):
        row(requested_provider=value)


def test_a_claim_and_a_page_carry_their_own_bounds() -> None:
    claim = WorktreeClaim(worktree="/tmp/wt", run_id="r-1", holder="i-1", lease_until=9)
    assert claim.lease_until == 9
    with pytest.raises(ValueError, match="holder"):
        WorktreeClaim(worktree="/tmp/wt", run_id="r-1", holder="", lease_until=9)
    page = RunPage(rows=(row(),))
    assert page.next_after_run is None
    assert not page.truncated


@pytest.mark.parametrize(
    "field,value",
    [
        ("home", None),
        ("home", ""),
        ("home", "relative"),
        ("home", "/bad\x00path"),
        ("home", "/bad\npath"),
        ("home", "/" + "a" * 4096),
        ("sqlite_home", ""),
        ("sqlite_home", "relative"),
        ("sqlite_home", "/bad\x7fpath"),
        ("model_provider", ""),
        ("model_provider", "a" * 64),
        ("model_provider", "secret/token"),
        ("auth_env", ""),
        ("auth_env", "A" * 64),
        ("auth_env", "1BAD"),
        ("auth_env", "sk-secret"),
    ],
)
def test_native_context_rejects_invalid_coordinates_without_echoing(field, value):
    from nspawn.rows import NativeSessionContext

    arguments = {
        "home": "/accounts/one",
        "sqlite_home": None,
        "model_provider": None,
        "auth_env": None,
    }
    arguments[field] = value
    with pytest.raises(ValueError) as refusal:
        NativeSessionContext(**arguments)
    if value:
        assert value not in str(refusal.value)


def test_native_context_is_frozen_and_carries_only_nonsecret_coordinates():
    from dataclasses import FrozenInstanceError, fields

    from nspawn.rows import NativeSessionContext

    context = NativeSessionContext("/accounts/one", "/state", "fugu-1", "NOTT_FUGU_KEY")
    assert [f.name for f in fields(context)] == [
        "home",
        "sqlite_home",
        "model_provider",
        "auth_env",
        "read_roots",
    ]
    with pytest.raises(FrozenInstanceError):
        context.home = "/accounts/two"
