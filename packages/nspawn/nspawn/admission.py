"""Immutable private launch and attempt contracts shared by host and consumer.

Only nonsecret launch inputs belong in PRELAUNCH. Owner input has a separate
admission field; an interrupted or synthetic replacement cannot carry it.
The packaged schema is the wire shape; semantic identity checks live here.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files
from typing import Any, ClassVar, Self, cast

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

SAFE_INTEGER = 2**53 - 1


class AdmissionRefusal(Exception):
    """An ambiguous, incomplete or mismatched private admission is refused."""


def _check_json(value: object) -> None:
    match value:
        case None | bool():
            return
        case int():
            if abs(value) > SAFE_INTEGER:
                raise AdmissionRefusal("JSON integer is not exactly portable")
        case str():
            try:
                value.encode("utf-8", errors="strict")
            except UnicodeEncodeError as error:
                raise AdmissionRefusal("JSON text is not valid Unicode") from error
        case list():
            for item in value:
                _check_json(item)
        case dict():
            for key, item in value.items():
                if not isinstance(key, str) or not key.isascii():
                    raise AdmissionRefusal("JSON object keys must be ASCII strings")
                _check_json(key)
                _check_json(item)
        case _:
            raise AdmissionRefusal("JSON value is outside the canonical vocabulary")


def canonical_bytes(value: object) -> bytes:
    """UTF-8, sorted ASCII keys, exact integers and ordered arrays."""
    _check_json(value)
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")


def document_digest(value: object) -> str:
    """The digest of a canonical value (raw configuration text is handled separately)."""
    return "sha256:" + hashlib.sha256(canonical_bytes(value)).hexdigest()


def _pairs(items: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in items:
        if key in result:
            raise AdmissionRefusal("duplicate JSON object key")
        result[key] = value
    return result


def _noninteger(value: str) -> object:
    raise AdmissionRefusal("noninteger JSON number is not admitted")


def decode_document(encoded: bytes) -> dict[str, Any]:
    """Reject duplicate keys, lossy numbers and invalid Unicode before schema validation."""
    try:
        value: object = json.loads(
            encoded.decode("utf-8", errors="strict"),
            object_pairs_hook=_pairs,
            parse_float=_noninteger,
            parse_constant=_noninteger,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AdmissionRefusal("invalid JSON document") from error
    _check_json(value)
    if not isinstance(value, dict):
        raise AdmissionRefusal("private contract must be a JSON object")
    return cast("dict[str, Any]", value)


@lru_cache
def _validator(definition: str) -> Draft202012Validator:
    schema = json.loads(
        files("nspawn").joinpath("schema/admission.v1.schema.json").read_text("utf-8")
    )
    return Draft202012Validator({**schema, "$ref": f"#/$defs/{definition}"})


@dataclass(frozen=True, slots=True, init=False)
class _Record:
    encoded: bytes
    DEFINITION: ClassVar[str]

    def __init__(self, data: Mapping[str, object]) -> None:
        encoded = canonical_bytes(dict(data))
        parsed = decode_document(encoded)
        problem = next(_validator(self.DEFINITION).iter_errors(parsed), None)
        if problem is not None:
            path = ".".join(str(part) for part in problem.absolute_path)
            # Never interpolate rejected data: owner input and private references
            # must not escape through a schema validator's verbose message.
            raise AdmissionRefusal(f"invalid {self.DEFINITION} field: {path or 'root'}")
        self._validate(parsed)
        object.__setattr__(self, "encoded", encoded)

    @classmethod
    def from_bytes(cls, encoded: bytes) -> Self:
        value = cls(decode_document(encoded))
        if value.encoded != encoded:
            raise AdmissionRefusal("private contract bytes are not canonical")
        return value

    def _validate(self, data: dict[str, Any]) -> None:
        if "version" in data and type(data["version"]) is not int:
            raise AdmissionRefusal("version must be an integer")

    def data(self) -> dict[str, Any]:
        """Return an isolated wire projection; callers cannot mutate the frozen record."""
        return decode_document(self.encoded)


class AttemptKey(_Record):
    __slots__ = ()
    DEFINITION = "AttemptKey"

    @property
    def activation_id(self) -> str:
        return str(self.data()["activation_id"])

    @property
    def attempt_id(self) -> str:
        return str(self.data()["attempt_id"])

    @property
    def owner_turn_id(self) -> str:
        return str(self.data()["owner_turn_id"])


def _configuration_identity(data: dict[str, Any]) -> None:
    for entry in data["configuration"]:
        document = entry["document"]
        encoded = (
            document.encode("utf-8")
            if isinstance(document, str)
            else canonical_bytes(document)
        )
        if "sha256:" + hashlib.sha256(encoded).hexdigest() != entry["digest"]:
            raise AdmissionRefusal("configuration document identity differs")


def _producer_identity(data: dict[str, Any]) -> None:
    for kind in ("mcp", "producer"):
        seen: set[str] = set()
        for entry in data[kind]:
            if entry["id"] in seen:
                raise AdmissionRefusal("duplicate selected launch input")
            seen.add(entry["id"])
            if entry["command"] not in {
                identity["path"] for identity in entry["executables"]
            }:
                raise AdmissionRefusal(
                    "launch command has no admitted executable identity"
                )


def policy_generation(data: Mapping[str, Any]) -> str:
    """Identify admitted managed inputs and producers, not a native lock token."""
    policy = data["policy"]
    return document_digest(
        {
            "managed_sources": policy["managed_sources"],
            "configuration": [
                item
                for item in data["configuration"]
                if item["role"] == "managed"
                or item["path"] in policy["managed_sources"]
            ],
            "mcp": data["mcp"],
            "producer": data["producer"],
            "helpers": policy["helpers"],
            "trust_filter": policy["trust_filter"],
            "parent_settings": policy["parent_settings"],
            "refresh_ceiling": policy["refresh_ceiling"],
        }
    )


def _policy_identity(data: dict[str, Any]) -> None:
    if policy_generation(data) != data["policy"]["generation_digest"]:
        raise AdmissionRefusal("policy generation differs from admitted managed inputs")


def runtime_files(runtime: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The bound entry identities and complete selected runtime file manifest."""
    return [
        *(
            runtime[name]
            for name in ("launcher", "node", "consumer", "sdk", "sdk_package", "native")
        ),
        *runtime["files"],
    ]


def _runtime_identity(data: dict[str, Any]) -> None:
    runtime = data["runtime"]
    files: dict[str, str] = {}
    for identity in runtime["files"]:
        if identity["path"] in files and files[identity["path"]] != identity["digest"]:
            raise AdmissionRefusal("runtime file has incompatible identity bindings")
        files[identity["path"]] = identity["digest"]
    for name in ("launcher", "node", "consumer", "sdk", "sdk_package", "native"):
        identity = runtime[name]
        if files.get(identity["path"]) != identity["digest"]:
            raise AdmissionRefusal("runtime entry is absent from its file manifest")


class PrelaunchProfileV1(_Record):
    __slots__ = ()
    DEFINITION = "PrelaunchProfileV1"

    def _validate(self, data: dict[str, Any]) -> None:
        super()._validate(data)
        _configuration_identity(data)
        _producer_identity(data)
        _policy_identity(data)
        _runtime_identity(data)
        if (
            document_digest(data["constructor"])
            != data["selection"]["constructor_baseline"]
        ):
            raise AdmissionRefusal("constructor baseline differs from selected options")
        if (
            data["scope"]["permission_ceiling"] == "read-only"
            and data["scope"]["write"]
        ):
            raise AdmissionRefusal("write destinations exceed the permission ceiling")
        ids = [control["id"] for control in data["conversation"]["pending_control"]]
        if len(ids) != len(set(ids)):
            raise AdmissionRefusal("duplicate pending control reference")

    @property
    def digest(self) -> str:
        return "sha256:" + hashlib.sha256(self.encoded).hexdigest()


class _AttemptRecord(_Record):
    __slots__ = ()

    @property
    def key(self) -> AttemptKey:
        return AttemptKey(self.data()["key"])

    @property
    def profile_digest(self) -> str:
        return str(self.data()["profile_digest"])


class SessionAdmissionV1(_AttemptRecord):
    __slots__ = ()
    DEFINITION = "SessionAdmissionV1"

    def _validate(self, data: dict[str, Any]) -> None:
        super()._validate(data)
        profile = PrelaunchProfileV1(data["profile"])
        if profile.digest != data["profile_digest"]:
            raise AdmissionRefusal("admission profile digest differs")
        if data["key"]["activation_id"] != data["profile"]["activation_id"]:
            raise AdmissionRefusal("admission activation differs")
        if data["conversation"] != data["profile"]["conversation"]:
            raise AdmissionRefusal("admission conversation differs")
        if data["conversation"]["kind"] == "new":
            if data["initial_input"] is None:
                raise AdmissionRefusal("new submission requires its prepared input")
        elif data["initial_input"] is not None:
            raise AdmissionRefusal("replacement must not resubmit owner input")


class AdmissionReceiptV1(_AttemptRecord):
    __slots__ = ()
    DEFINITION = "AdmissionReceiptV1"


class AttemptReceiptV1(_AttemptRecord):
    __slots__ = ()
    DEFINITION = "AttemptReceiptV1"


class AttemptRequestV1(_AttemptRecord):
    __slots__ = ()
    DEFINITION = "AttemptRequestV1"


class LaunchReceiptV1(_Record):
    __slots__ = ()
    DEFINITION = "LaunchReceiptV1"

    def _validate(self, data: dict[str, Any]) -> None:
        super()._validate(data)
        composition = data["composition"]
        _configuration_identity(composition)
        _producer_identity(composition)
        _policy_identity(composition)
        _runtime_identity(composition)
        if document_digest(composition["runtime"]) != data["runtime_digest"]:
            raise AdmissionRefusal("launch runtime identity differs")
