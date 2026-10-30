"""Optional per-turn declarations and legacy admission evidence readers."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

from .errors import NSpawnError
from .limits import MAX_DURABLE_OBSERVATION_BYTES, MAX_DURABLE_ROW_BYTES

OMITTED = object()

UNCHECKED_REASON = (
    "operations_unchecked: declared-operation compatibility was not checked"
)


def _object(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


_TEXT = {"type": "string", "minLength": 1, "maxLength": 4096}
_PATH_SCHEMA = _object(
    {
        "base": {"enum": ["cwd", "temp", "absolute"]},
        "path": _TEXT,
        "access": {"enum": ["read", "write"]},
        "scope": {"enum": ["path", "subtree"]},
    }
)
SCHEMA: dict[str, Any] = _object(
    {
        "version": {"type": "integer", "const": 1},
        "tools": {
            "type": "array",
            "items": _TEXT,
            "maxItems": 128,
            "uniqueItems": True,
        },
        "paths": {
            "type": "array",
            "items": _PATH_SCHEMA,
            "maxItems": 128,
            "uniqueItems": True,
        },
        "network": {
            "type": "array",
            "maxItems": 128,
            "uniqueItems": True,
            "items": _object(
                {
                    "host": _TEXT,
                    "port": {"type": "integer", "minimum": 1, "maximum": 65535},
                }
            ),
        },
        "hosts": {
            "type": "array",
            "maxItems": 128,
            "uniqueItems": True,
            "items": _object(
                {
                    "host": _TEXT,
                    "transport": {"enum": ["ssh"]},
                }
            ),
        },
        "actions": {
            "type": "array",
            "maxItems": 128,
            "uniqueItems": True,
            "items": {
                "oneOf": [
                    {
                        "enum": [
                            "file.read",
                            "file.write",
                            "process.exec",
                            "git.commit",
                            "git.push",
                            "host.install",
                            "service.change",
                        ]
                    },
                    _object(
                        {
                            "kind": {
                                "enum": [
                                    "file.read",
                                    "file.write",
                                    "host.install",
                                    "process.exec",
                                ]
                            },
                            "target": _PATH_SCHEMA,
                        }
                    ),
                    _object(
                        {
                            "kind": {"const": "git.commit"},
                            "target": _object({"branch": _TEXT}),
                        }
                    ),
                    _object(
                        {
                            "kind": {"const": "git.push"},
                            "target": _object({"branch": _TEXT, "remote": _TEXT}),
                        }
                    ),
                    _object({"kind": {"const": "service.change"}, "target": _TEXT}),
                ]
            },
        },
    }
)
_VALIDATOR = Draft202012Validator(SCHEMA)


def canonical(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class RequirementFailure:
    cause: str
    field: str
    constraint: str


class OperationRefused(NSpawnError):
    """A safe operation-specific refusal, without request or inventory values."""

    def __init__(
        self,
        failures: tuple[RequirementFailure, ...],
        *,
        mode: str = "unproved",
        installation: bool = False,
    ) -> None:
        self.failures = failures
        remedy = (
            "Use the existing authorized host installation path and its owning installer. "
            "Repository-only preparation "
            "changes the task and leaves installation unmet."
            if installation
            else "Change the task and its declaration to supported work, or use an existing authorized host path. "
            "The declared work remains unmet; no alternative route was proved."
        )
        if mode.startswith("codex") and any(
            f.field.startswith("operations.paths") for f in failures
        ):
            remedy += " For temporary output, change the task to use its actual tool TMPDIR and a temp path declaration. The original absolute output remains unmet."
        self.data: dict[str, Any] = {
            "cause": failures[0].cause,
            "failures": [asdict(f) for f in failures],
            "remedy": remedy,
            "operation_admission": {"checked": False, "reason": "declaration refused"},
        }
        self.code = -32602
        self.rpc_message = (
            "; ".join(f"{f.field}: {f.cause}: {f.constraint}" for f in failures)
            + ". "
            + remedy
        )
        Exception.__init__(self, self.rpc_message)


@dataclass(frozen=True, slots=True)
class Declaration:
    """Canonical explicit caller data, independent of selection and scratch."""

    encoded: str

    @classmethod
    def parse(cls, value: object) -> Declaration:
        try:
            size = len(canonical(value).encode())
        except TypeError, ValueError, RecursionError:
            size = MAX_DURABLE_ROW_BYTES + 1
        if size > MAX_DURABLE_ROW_BYTES:
            raise OperationRefused(
                (
                    RequirementFailure(
                        "invalid",
                        "operations",
                        "declaration exceeds the request byte limit",
                    ),
                )
            )
        errors = list(_VALIDATOR.iter_errors(value))
        if errors:
            path = "operations" + "".join(
                f"[{p}]" if isinstance(p, int) else f".{p}"
                for p in errors[0].absolute_path
            )
            raise OperationRefused(
                (
                    RequirementFailure(
                        "invalid", path, "invalid version-1 declaration shape"
                    ),
                )
            )
        assert isinstance(value, dict)
        if type(value["version"]) is not int:
            raise OperationRefused(
                (
                    RequirementFailure(
                        "invalid", "operations.version", "version must be integer 1"
                    ),
                )
            )
        for field in ("tools", "paths", "network", "hosts"):
            for index, item in enumerate(value[field]):
                strings = (
                    [item]
                    if isinstance(item, str)
                    else [v for v in item.values() if isinstance(v, str)]
                )
                if any(not s.strip() or any(ord(c) < 32 for c in s) for s in strings):
                    raise OperationRefused(
                        (
                            RequirementFailure(
                                "invalid",
                                f"operations.{field}[{index}]",
                                "text must be unambiguous printable text",
                            ),
                        )
                    )
        path_needs = [
            (f"operations.paths[{i}]", item) for i, item in enumerate(value["paths"])
        ]
        for index, action in enumerate(value["actions"]):
            if not isinstance(action, dict):
                continue
            target = action["target"]
            target_texts = target.values() if isinstance(target, dict) else (target,)
            if any(
                isinstance(text, str)
                and (not text.strip() or any(ord(c) < 32 for c in text))
                for text in target_texts
            ):
                raise OperationRefused(
                    (
                        RequirementFailure(
                            "invalid",
                            f"operations.actions[{index}]",
                            "action target must be printable literal text",
                        ),
                    )
                )
            if action["kind"] in (
                "file.read",
                "file.write",
                "host.install",
                "process.exec",
            ):
                field = f"operations.actions[{index}].target"
                access = (
                    "read"
                    if action["kind"] in ("file.read", "process.exec")
                    else "write"
                )
                if target["access"] != access:
                    raise OperationRefused(
                        (
                            RequirementFailure(
                                "invalid", field, "target access must match its action"
                            ),
                        )
                    )
                path_needs.append((field, target))
        for field, item in path_needs:
            text = item["path"]
            try:
                os.fsencode(text)
            except UnicodeEncodeError as error:
                raise OperationRefused(
                    (
                        RequirementFailure(
                            "invalid", field, "path must use the filesystem encoding"
                        ),
                    )
                ) from error
            parts = Path(text).parts
            if (
                any(c in text for c in "*?[]$~{}\\")
                or ".." in parts
                or text != text.strip()
                or (item["base"] == "absolute") != Path(text).is_absolute()
            ):
                raise OperationRefused(
                    (
                        RequirementFailure(
                            "invalid",
                            field,
                            "path must be literal and remain inside its logical base",
                        ),
                    )
                )
        for field in ("network", "hosts"):
            for index, item in enumerate(value[field]):
                host = item["host"]
                try:
                    ipaddress.ip_address(host)
                except ValueError:
                    if (
                        len(host) > 253
                        or host != host.lower()
                        or not all(
                            re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                            for label in host.split(".")
                        )
                    ):
                        raise OperationRefused(
                            (
                                RequirementFailure(
                                    "invalid",
                                    f"operations.{field}[{index}]",
                                    "destination must be a literal hostname or IP address",
                                ),
                            )
                        ) from None
                if field == "network" and type(item["port"]) is not int:
                    raise OperationRefused(
                        (
                            RequirementFailure(
                                "invalid",
                                f"operations.network[{index}]",
                                "port must be an integer",
                            ),
                        )
                    )
        return cls(canonical(value))

    def data(self) -> dict[str, Any]:
        value: dict[str, Any] = json.loads(self.encoded)
        return value

    @property
    def nonempty(self) -> bool:
        return any(
            self.data()[name]
            for name in ("tools", "paths", "network", "hosts", "actions")
        )


@dataclass(frozen=True, slots=True)
class PathAnswer:
    field: str
    access: str
    scope: str
    root: str


@dataclass(frozen=True, slots=True)
class CheckedAdmission:
    checked: bool
    observed_at: int
    requirement_digest: str
    policy_digest: str
    mode: str
    paths: tuple[PathAnswer, ...]
    writable_roots: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class UncheckedAdmission:
    checked: bool = False
    reason: str = UNCHECKED_REASON


def admission_read(value: object) -> CheckedAdmission:
    """Validate safe durable evidence. Never reconstruct a compatibility claim."""
    try:
        size = len(canonical(value).encode())
    except (TypeError, ValueError, RecursionError) as error:
        raise ValueError("malformed operation admission witness") from error
    if size > MAX_DURABLE_OBSERVATION_BYTES:
        raise ValueError("operation admission witness exceeds its byte limit")
    if not isinstance(value, dict) or set(value) != {
        "checked",
        "observed_at",
        "requirement_digest",
        "policy_digest",
        "mode",
        "paths",
        "writable_roots",
    }:
        raise ValueError("malformed operation admission witness")
    if (
        value["checked"] is not True
        or type(value["observed_at"]) is not int
        or value["observed_at"] < 0
        or any(
            not isinstance(value[k], str)
            or re.fullmatch(r"[0-9a-f]{64}", value[k]) is None
            for k in ("requirement_digest", "policy_digest")
        )
        or value["mode"]
        not in (
            "claude-write",
            "claude-review",
            "claude-reader",
            "codex-write",
            "codex-reader",
            "unproved",
        )
        or not isinstance(value["paths"], list)
        or len(value["paths"]) > 256
        or not isinstance(value["writable_roots"], list)
        or len(value["writable_roots"]) > 1
        or any(root != "/var/tmp" for root in value["writable_roots"])
    ):
        raise ValueError("malformed operation admission witness")
    paths = []
    fields: set[str] = set()
    for item in value["paths"]:
        if (
            not isinstance(item, dict)
            or set(item) != {"field", "access", "scope", "root"}
            or not isinstance(item["field"], str)
            or re.fullmatch(
                r"operations\.(?:paths\[\d+\]|actions\[\d+\]\.target)", item["field"]
            )
            is None
            or item["access"] not in ("read", "write")
            or item["scope"] not in ("path", "subtree")
            or item["root"] not in ("cwd", "temp", "absolute", "/var/tmp")
        ):
            raise ValueError("malformed operation path witness")
        index = int(item["field"].split("[", 1)[1].split("]", 1)[0])
        if index >= 128 or item["field"] in fields:
            raise ValueError("malformed operation path witness")
        fields.add(item["field"])
        paths.append(PathAnswer(**item))
    return CheckedAdmission(
        True,
        value["observed_at"],
        value["requirement_digest"],
        value["policy_digest"],
        value["mode"],
        tuple(paths),
        tuple(value["writable_roots"]),
    )
