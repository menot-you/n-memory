"""Shared launch/attempt conformance vectors; no native inference or live stores."""

import copy
import hashlib
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from nspawn.admission import (
    AdmissionReceiptV1,
    AdmissionRefusal,
    AttemptKey,
    AttemptReceiptV1,
    PrelaunchProfileV1,
    SessionAdmissionV1,
    canonical_bytes,
    decode_document,
    document_digest,
    policy_generation,
)


def profile() -> dict[str, object]:
    identity = {"path": "/runtime/bin", "digest": "sha256:" + "a" * 64}
    document = {"hooks": [], "permissions": {"allow": []}}
    prepared: dict[str, object] = {
        "version": 1,
        "node_id": "node-1",
        "activation_id": "activation-1",
        "route_ref": "account-a@subscription",
        "cwd": "/project",
        "scope": {
            "read": ["/project"],
            "write": [],
            "audit": True,
            "permission_ceiling": "read-only",
            "network": [],
            "administrative": ["/private/conversation"],
        },
        "runtime": {
            name: copy.deepcopy(identity)
            for name in ("launcher", "node", "consumer", "sdk", "native")
        },
        "constructor": {
            "model": "opus-5-5",
            "effort": "max",
            "permission_mode": "default",
            "system_prompt": "Admitted project contract",
            "tools": [],
            "skills": [],
            "setting_sources": ["project"],
            "limits": {},
            "options": {},
        },
        "configuration": [
            {
                "path": "/project/.claude/settings.json",
                "format": "json",
                "document": document,
                "digest": document_digest(document),
                "role": "project",
            }
        ],
        "project": {
            "id": "project-1",
            "root": "/project",
            "plugin_parent": "/plugin/base",
            "plugin_view": "/private/plugin",
            "mcp_configuration": {"mcpServers": {}},
        },
        "mcp": [],
        "producer": [],
        "policy": {
            "managed_sources": [],
            "trust_filter": {},
            "parent_settings": "admitted",
            "helpers": [],
            "generation_digest": "sha256:" + "b" * 64,
            "refresh_ceiling": {},
        },
        "selection": {"sequence": 3, "constructor_baseline": "sha256:" + "c" * 64},
        "conversation": {
            "kind": "new",
            "native_session": None,
            "frontier": None,
            "operation_index": None,
            "pending_control": [],
        },
        "stop_epoch": 4,
    }
    prepared["selection"]["constructor_baseline"] = document_digest(
        prepared["constructor"]
    )  # type: ignore[index]
    prepared["policy"]["generation_digest"] = policy_generation(prepared)  # type: ignore[index]
    prepared["runtime"]["sdk_package"] = copy.deepcopy(identity)  # type: ignore[index]
    prepared["runtime"]["files"] = [copy.deepcopy(identity)]  # type: ignore[index]
    return prepared


def test_policy_generation_cannot_claim_unbound_managed_inputs_or_producers():
    raw = profile()
    raw["policy"]["generation_digest"] = "sha256:" + "0" * 64
    with pytest.raises(AdmissionRefusal, match="policy generation"):
        PrelaunchProfileV1(raw)


def test_runtime_identity_requires_its_public_package_and_file_manifest():
    raw = profile()
    raw["runtime"].pop("files", None)
    raw["runtime"].pop("sdk_package", None)
    with pytest.raises(AdmissionRefusal, match="runtime"):
        PrelaunchProfileV1(raw)


def key() -> dict[str, str]:
    return {
        "activation_id": "activation-1",
        "attempt_id": "attempt-1",
        "owner_turn_id": "turn-1",
    }


def test_canonical_vector_is_language_independent_and_preserves_array_order() -> None:
    value = {"z": [3, "é", False], "a": {"b": None, "a": 1}}
    expected = b'{"a":{"a":1,"b":null},"z":[3,"\xc3\xa9",false]}'
    assert canonical_bytes(value) == expected
    assert document_digest(value) == "sha256:" + hashlib.sha256(expected).hexdigest()
    assert decode_document(expected) == value
    assert canonical_bytes({"z": [False, "é", 3], "a": value["a"]}) != expected


@pytest.mark.parametrize(
    "value",
    [
        1.0,
        float("nan"),
        float("inf"),
        2**53,
        -(2**53),
        {"é": 1},
        {"bad": "\ud800"},
        {"bad": b"bytes"},
        {1: "numeric key"},
    ],
)
def test_noncanonical_values_are_refused(value: object) -> None:
    with pytest.raises(AdmissionRefusal):
        canonical_bytes(value)


@pytest.mark.parametrize(
    "value",
    [
        b'{"a":1,"a":2}',
        b'{"a":1.0}',
        b'{"a":NaN}',
        b'{"a":"\\ud800"}',
        b'{"a":9007199254740992}',
        b"\xff",
        b'{"\xc3\xa9":1}',
    ],
)
def test_ambiguous_or_invalid_wire_bytes_are_refused(value: bytes) -> None:
    with pytest.raises(AdmissionRefusal):
        decode_document(value)


def test_complete_profile_freezes_inputs_and_binds_different_authority() -> None:
    raw = profile()
    audit = PrelaunchProfileV1(raw)
    raw["scope"] = {
        **raw["scope"],
        "audit": False,
        "write": ["/project"],
        "permission_ceiling": "write",
    }  # type: ignore[dict-item]
    writer = PrelaunchProfileV1(raw)
    assert audit.digest != writer.digest
    assert audit.data()["scope"]["audit"] is True
    readback = audit.data()
    readback["scope"]["write"].append("/elsewhere")
    assert audit.data()["scope"]["write"] == []
    assert PrelaunchProfileV1.from_bytes(audit.encoded).digest == audit.digest


@pytest.mark.parametrize(
    "table",
    [
        "scope",
        "runtime",
        "constructor",
        "configuration",
        "project",
        "mcp",
        "producer",
        "policy",
        "selection",
        "conversation",
        "stop_epoch",
    ],
)
def test_profile_refuses_missing_actual_launch_inputs(table: str) -> None:
    raw = profile()
    del raw[table]
    with pytest.raises(AdmissionRefusal):
        PrelaunchProfileV1(raw)


def test_configuration_digest_alone_cannot_replace_selected_data() -> None:
    raw = profile()
    raw["configuration"] = [
        {
            "path": "/config",
            "format": "json",
            "digest": "sha256:" + "0" * 64,
            "role": "managed",
        }
    ]
    with pytest.raises(AdmissionRefusal):
        PrelaunchProfileV1(raw)


def test_constructor_baseline_is_verified_against_its_actual_options() -> None:
    raw = profile()
    raw["selection"]["constructor_baseline"] = document_digest(raw["constructor"])  # type: ignore[index]
    assert PrelaunchProfileV1(raw).data()["constructor"]["model"] == "opus-5-5"
    raw["constructor"]["model"] = "changed-model"  # type: ignore[index]
    with pytest.raises(AdmissionRefusal):
        PrelaunchProfileV1(raw)
    raw = profile()
    raw["configuration"][0]["digest"] = "sha256:" + "0" * 64  # type: ignore[index]
    with pytest.raises(AdmissionRefusal):
        PrelaunchProfileV1(raw)


def test_configuration_with_fractional_numbers_keeps_its_exact_utf8_document() -> None:
    raw = profile()
    document = '{"fraction":1.25}\n'
    raw["configuration"] = [
        {
            "path": "/config",
            "format": "json",
            "document": document,
            "digest": "sha256:" + hashlib.sha256(document.encode()).hexdigest(),
            "role": "managed",
        }
    ]
    raw["policy"]["generation_digest"] = policy_generation(raw)  # type: ignore[index]
    assert PrelaunchProfileV1(raw).data()["configuration"][0]["document"] == document


def admission(raw: dict[str, object] | None = None) -> dict[str, object]:
    prepared = PrelaunchProfileV1(profile() if raw is None else raw)
    return {
        "version": 1,
        "key": key(),
        "profile_digest": prepared.digest,
        "profile": prepared.data(),
        "conversation": prepared.data()["conversation"],
        "initial_input": {
            "id": "input-1",
            "content": [{"type": "text", "text": "owner input"}],
        },
    }


def test_initial_input_is_kept_outside_the_nonsecret_profile() -> None:
    prepared = SessionAdmissionV1(admission())
    assert prepared.key == AttemptKey(key())
    assert b"owner input" not in PrelaunchProfileV1(prepared.data()["profile"]).encoded


@pytest.mark.parametrize(
    "change", ["activation", "digest", "frontier", "replacement_input"]
)
def test_admission_rejects_rebound_attempt_or_original_payload_replay(
    change: str,
) -> None:
    raw = admission()
    if change == "activation":
        raw["key"]["activation_id"] = "another-activation"  # type: ignore[index]
    elif change == "digest":
        raw["profile_digest"] = "sha256:" + "0" * 64
    elif change == "frontier":
        raw["conversation"]["frontier"] = "unbound-frontier"  # type: ignore[index]
    else:
        modified = profile()
        modified["conversation"] = {
            "kind": "interrupted",
            "native_session": "native-1",
            "frontier": "sealed-1",
            "operation_index": "operations-1",
            "pending_control": [],
        }
        raw = admission(modified)
    with pytest.raises(AdmissionRefusal):
        SessionAdmissionV1(raw)


def test_userless_interrupted_admission_is_valid() -> None:
    raw = profile()
    raw["conversation"] = {
        "kind": "interrupted",
        "native_session": "native-1",
        "frontier": "sealed-1",
        "operation_index": "operations-1",
        "pending_control": [],
    }
    request = admission(raw)
    request["initial_input"] = None
    assert SessionAdmissionV1(request).data()["initial_input"] is None


def test_new_owner_turn_keeps_the_existing_native_history() -> None:
    raw = profile()
    raw["conversation"] = {
        "kind": "new",
        "native_session": "native-existing",
        "frontier": "sealed-existing",
        "operation_index": "operations-existing",
        "pending_control": [],
    }
    accepted = SessionAdmissionV1(admission(raw))
    assert accepted.data()["conversation"]["native_session"] == "native-existing"
    assert accepted.data()["initial_input"]["id"] == "input-1"


@pytest.mark.parametrize("missing", ["native_session", "frontier", "operation_index"])
def test_new_turn_does_not_admit_a_partial_history_identity(missing: str) -> None:
    raw = profile()
    conversation = {
        "kind": "new",
        "native_session": "native-existing",
        "frontier": "sealed-existing",
        "operation_index": "operations-existing",
        "pending_control": [],
    }
    conversation[missing] = None
    raw["conversation"] = conversation
    with pytest.raises(AdmissionRefusal):
        PrelaunchProfileV1(raw)


def test_terminal_without_a_query_handle_preserves_possible_construction() -> None:
    receipt = {
        "version": 1,
        "key": key(),
        "profile_digest": "sha256:" + "a" * 64,
        "admission_id": "admission-1",
        "revision": 2,
        "request_id": 7,
        "state": "terminal",
        "construction": "possible",
        "cancellation": "unknown",
        "outcome": "error",
        "control_ref": None,
        "native_session": None,
    }
    assert AttemptReceiptV1(receipt).data()["construction"] == "possible"
    receipt["construction"] = "observed"
    with pytest.raises(AdmissionRefusal):
        AttemptReceiptV1(receipt)


def test_admission_is_passive_and_cannot_claim_a_constructed_query() -> None:
    receipt = {
        "version": 1,
        "key": key(),
        "profile_digest": "sha256:" + "a" * 64,
        "admission_id": "admission-1",
        "revision": 1,
        "state": "admitted",
        "construction": "excluded",
        "constructor_baseline": "sha256:" + "b" * 64,
        "frontier": None,
        "initial_input": {"id": "input-1", "content": []},
    }
    assert AdmissionReceiptV1(receipt).key == AttemptKey(key())
    receipt["construction"] = "observed"
    with pytest.raises(AdmissionRefusal):
        AdmissionReceiptV1(receipt)


def test_schema_is_packaged_and_matches_the_canonical_profile_vector() -> None:
    path = Path(__file__).parents[1] / "nspawn/schema/admission.v1.schema.json"
    schema = json.loads(path.read_text())
    Draft202012Validator.check_schema(schema)
    Draft202012Validator({**schema, "$ref": "#/$defs/PrelaunchProfileV1"}).validate(
        profile()
    )
