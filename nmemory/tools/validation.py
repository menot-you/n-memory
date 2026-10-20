"""The argument validation every tool call passes before its handler runs.

Each tool's arguments are checked against its own `inputSchema` in
`tools_schema.json`: the fields, the required fields, the types and the enums
the catalogue publishes. A refusal answers `failed to deserialize parameters:
<detail>` in-band, worded as serde words it (unknown field, unknown variant,
invalid type). Value bounds (`minimum`, `maxLength`...) stay with the handlers.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

from .errors import invalid_params

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "tools_schema.json"


class DeserializationError(ValueError):
    """Raised when parameters fail serde-style validation."""


def _format_unknown_field_msg(unknown_key: str, allowed_fields: list[str]) -> str:
    if not allowed_fields:
        return f"unknown field `{unknown_key}`, there are no fields"
    if len(allowed_fields) == 1:
        return f"unknown field `{unknown_key}`, expected `{allowed_fields[0]}`"
    if len(allowed_fields) == 2:
        return f"unknown field `{unknown_key}`, expected `{allowed_fields[0]}` or `{allowed_fields[1]}`"
    joined = ", ".join(f"`{f}`" for f in allowed_fields)
    return f"unknown field `{unknown_key}`, expected one of {joined}"


def _format_unknown_variant_msg(val: str, allowed_variants: list[str]) -> str:
    if len(allowed_variants) == 2:
        return f"unknown variant `{val}`, expected `{allowed_variants[0]}` or `{allowed_variants[1]}`"
    joined = ", ".join(f"`{v}`" for v in allowed_variants)
    return f"unknown variant `{val}`, expected one of {joined}"


def _format_type_error(field: str, val: Any, expected: str) -> str:
    if isinstance(val, bool):
        type_str = f"boolean `{str(val).lower()}`"
    elif isinstance(val, int):
        type_str = f"integer `{val}`"
    elif isinstance(val, float):
        type_str = f"floating point `{val}`"
    elif isinstance(val, str):
        type_str = f'string "{val}"'
    elif isinstance(val, list):
        type_str = "sequence"
    elif isinstance(val, dict):
        type_str = "map"
    elif val is None:
        type_str = "unit"
    else:
        type_str = type(val).__name__
    return f"invalid type: {type_str}, expected {expected}"


#: Each JSON Schema type the catalogue uses: its check and serde's name for it.
_JSON_TYPES: dict[str, tuple[type | tuple[type, ...], str]] = {
    "string": (str, "a string"),
    "boolean": (bool, "a boolean"),
    "integer": (int, "an integer"),
    "number": ((int, float), "f64"),
    "array": (list, "a sequence"),
    "object": (dict, "a map"),
}


def _check_type(val: Any, json_type: str) -> bool:
    if json_type != "boolean" and isinstance(val, bool):
        return False
    return isinstance(val, _JSON_TYPES[json_type][0])


def _type_name(json_type: str) -> str:
    return _JSON_TYPES[json_type][1]


@dataclass(frozen=True)
class _Spec:
    """What a closed object schema admits, read once from the catalogue."""

    fields: list[str]
    required: list[str]
    types: dict[str, str]
    enums: dict[str, list[str]]
    item_types: dict[str, str]


def _resolve(
    node: dict[str, Any], root: dict[str, Any]
) -> tuple[str, list[str] | None, dict[str, Any] | None]:
    """One property's JSON type (null dropped), its enum consts and its items node.

    The shapes the catalogue uses: a `$ref` into `$defs`, an optional value as
    `anyOf [<x>, {"type": "null"}]`, a `oneOf` of string consts and a type list
    carrying `"null"`. Any other shape is refused when the spec is built.
    """
    if "$ref" in node:
        return _resolve(root["$defs"][node["$ref"].removeprefix("#/$defs/")], root)
    if "anyOf" in node:
        (alternative,) = [a for a in node["anyOf"] if a.get("type") != "null"]
        return _resolve(alternative, root)
    if "oneOf" in node:
        (json_type,) = {alt["type"] for alt in node["oneOf"]}
        return json_type, [alt["const"] for alt in node["oneOf"]], None
    declared = node["type"]
    (json_type,) = ({declared} if isinstance(declared, str) else set(declared)) - {
        "null"
    }
    return json_type, node.get("enum"), node.get("items")


def _spec_of(schema: dict[str, Any], root: dict[str, Any]) -> _Spec:
    types: dict[str, str] = {}
    enums: dict[str, list[str]] = {}
    item_types: dict[str, str] = {}
    properties: dict[str, Any] = schema.get("properties", {})
    for field, node in properties.items():
        types[field], consts, items = _resolve(node, root)
        if items is not None:
            item_types[field], consts, _ = _resolve(items, root)
        if consts:
            enums[field] = consts
    return _Spec(
        fields=list(properties),
        required=list(schema.get("required", [])),
        types=types,
        enums=enums,
        item_types=item_types,
    )


@cache
def _catalogue() -> dict[str, dict[str, Any]]:
    tools: list[dict[str, Any]] = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return {tool["name"]: tool["inputSchema"] for tool in tools}


@cache
def tool_spec(name: str) -> _Spec | None:
    """The argument spec of tool `name`; None for an unknown tool or a union root."""
    schema = _catalogue().get(name)
    if schema is None or ("anyOf" in schema and "properties" not in schema):
        return None
    return _spec_of(schema, schema)


@cache
def ingest_item_spec() -> _Spec:
    """The spec of one `nmemory_capsule_ingest` capture item."""
    schema = _catalogue()["nmemory_capsule_ingest"]
    return _spec_of(schema["$defs"]["IngestItemParams"], schema)


#: The hint appended to a refusal of these tools' arguments.
_HINTS: dict[str, str] = {
    "nmemory_capsule_purge": (
        ' — nmemory_capsule_purge needs id (the exact store id, e.g. "cap-<number>"), mode '
        "(purged = hard forget | redacted = provenance retained for audit; "
        "both destroy the content bytes), and reason (mandatory free text, "
        "recorded on the marker)"
    ),
}


INGEST_CONTRACT_HINT = (
    " — a capture item needs content, kind, plus BOTH provenance fields: "
    'source (origin, e.g. "session:2026-07-18") and anchor (path:line / '
    "doc-<id> / a short verbatim quote)"
)


def validate_ingest_item(item: Any) -> tuple[str | None, str] | None:
    """Validate a single capture item dictionary.

    Returns None if valid, or (bad_field, error_detail) if invalid (shape-rejected).
    """
    if not isinstance(item, dict):
        return (
            None,
            f"invalid type: {type(item).__name__}, expected a JSON object{INGEST_CONTRACT_HINT}",
        )

    spec = ingest_item_spec()
    for k in item:
        if k not in spec.fields:
            return (None, _format_unknown_field_msg(k, spec.fields))

    # `kind` is schema-required too, but the handler refuses its absence with
    # the teaching `MISSING_KIND_HINT`, which a plain "missing field" would lose.
    for req in ("content", "source", "anchor"):
        if req not in item:
            return (None, f"missing field `{req}`{INGEST_CONTRACT_HINT}")

    for k in spec.fields:
        if k not in item:
            continue
        v = item[k]
        if k == "expected_revision" and v is None:
            continue
        if not _check_type(v, spec.types[k]):
            err = _format_type_error(k, v, _type_name(spec.types[k]))
            return (k, f"{err}{INGEST_CONTRACT_HINT}")
        if k in spec.enums and v not in spec.enums[k]:
            err = _format_unknown_variant_msg(v, spec.enums[k])
            return (k, f"{err}{INGEST_CONTRACT_HINT}")
        # Shape, with index and type in the row; empties follow IngestRequest law.
        if k == "derived_from":
            for idx, elem in enumerate(v):
                if not isinstance(elem, str):
                    return (
                        k,
                        (
                            f"derived_from[{idx}] must be a string, "
                            f"got {type(elem).__name__}{INGEST_CONTRACT_HINT}"
                        ),
                    )

    return None


def validate_tool_arguments(name: str, args: Any) -> dict[str, Any]:
    """Validate tool call arguments against oracle schema and serde conventions."""
    if not isinstance(args, dict):
        raise DeserializationError(_format_type_error("", args, "a map"))

    dict_args: dict[str, Any] = dict(args)

    if name == "nmemory_capsule_show":
        if "id" in dict_args and "ids" not in dict_args and len(dict_args) == 1:
            if not isinstance(dict_args["id"], str):
                raise DeserializationError("nmemory_capsule_show id must be a string")
            return {"id": dict_args["id"]}
        elif "ids" in dict_args and "id" not in dict_args and len(dict_args) == 1:
            if not isinstance(dict_args["ids"], list):
                raise DeserializationError(
                    _format_type_error("ids", dict_args["ids"], "a sequence")
                )
            for idx, item in enumerate(dict_args["ids"]):
                if not isinstance(item, str):
                    raise DeserializationError(
                        _format_type_error(f"ids[{idx}]", item, "a string")
                    )
            return {"ids": dict_args["ids"]}
        else:
            raise DeserializationError(
                "nmemory_capsule_show needs exactly one of id or ids and accepts no other fields"
            )

    if name == "nmemory_capsule_ingest":
        if "items" in dict_args:
            if len(dict_args) > 1:
                strays = sorted(k for k in dict_args if k != "items")
                noun = "field" if len(strays) == 1 else "fields"
                joined = ", ".join(f'"{s}"' for s in strays)
                raise DeserializationError(
                    f"nmemory_capsule_ingest params mix the batch form with single-item {noun} {joined}: "
                    'pass ONE item object, or {"items": [...]} with nothing else (nothing was captured)'
                )
            if not isinstance(dict_args["items"], list):
                raise DeserializationError(
                    'nmemory_capsule_ingest "items" must be a JSON array of capture items'
                )
            return dict_args
        else:
            item_err = validate_ingest_item(dict_args)
            if item_err is not None:
                bad_field, detail = item_err
                if bad_field:
                    raise DeserializationError(f"{bad_field}: {detail}")
                raise DeserializationError(detail)
            return dict_args

    if name == "nmemory_turn_extract":
        for k in dict_args:
            if k not in ("content", "text"):
                raise DeserializationError(
                    _format_unknown_field_msg(k, ["content", "text"])
                )
        if "content" in dict_args and "text" in dict_args:
            raise DeserializationError("duplicate field `content`")
        if "content" not in dict_args and "text" not in dict_args:
            raise DeserializationError("missing field `content`")
        for k, v in dict_args.items():
            if not isinstance(v, str):
                raise DeserializationError(_format_type_error(k, v, "a string"))
        return dict_args

    if (
        name == "nmemory_kind_classify"
        and "capsule_id" in dict_args
        and "id" in dict_args
    ):
        raise DeserializationError("duplicate field `capsule_id`")

    if (
        name == "nmemory_capsule_retrieve"
        and "query" in dict_args
        and "terms" not in dict_args
    ):
        dict_args["terms"] = [dict_args.pop("query")]

    spec = tool_spec(name)
    if spec is None:
        return dict_args
    hint = _HINTS.get(name, "")

    for k in dict_args:
        if k not in spec.fields:
            raise DeserializationError(_format_unknown_field_msg(k, spec.fields))

    for req in spec.required:
        if req not in dict_args:
            raise DeserializationError(f"missing field `{req}`{hint}")

    for k, v in dict_args.items():
        if not _check_type(v, spec.types[k]):
            err = _format_type_error(k, v, _type_name(spec.types[k]))
            raise DeserializationError(f"{err}{hint}")
        if k in spec.item_types:
            for idx, elem in enumerate(v):
                if not _check_type(elem, spec.item_types[k]):
                    err = _format_type_error(
                        f"{k}[{idx}]", elem, _type_name(spec.item_types[k])
                    )
                    raise DeserializationError(f"{err}{hint}")
        if k == "time_window":
            for tw_k in v:
                if tw_k not in ("from", "to"):
                    raise DeserializationError(
                        _format_unknown_field_msg(tw_k, ["from", "to"])
                    )
            for tw_k, tw_v in v.items():
                if not isinstance(tw_v, str):
                    raise DeserializationError(
                        _format_type_error(tw_k, tw_v, "a string")
                    )
        if k in spec.enums:
            allowed = spec.enums[k]
            for elem in v if isinstance(v, list) else [v]:
                if elem not in allowed:
                    err = _format_unknown_variant_msg(elem, allowed)
                    raise DeserializationError(f"{err}{hint}")

    return dict_args


def validate_project_prefix(prefix: str | None) -> dict[str, Any] | None:
    """The -32602 refusal of a `project_prefix` no project id can match, or None."""
    if prefix is None:
        return None
    if not prefix.strip():
        return invalid_params(
            'project_prefix is empty — it can match no project id; omit the field to search unfenced, or pass a subtree root like "nott"'
        )
    if prefix.endswith("/"):
        clean = prefix.rstrip("/")
        return invalid_params(
            f'project_prefix "{prefix}" ends with the subtree separator and can match no project id — pass "{clean}" (the engine adds "/" itself: it matches the prefix exactly or prefix + "/...")'
        )
    return None
