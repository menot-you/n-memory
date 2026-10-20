"""The tool registry, the admitted tool list and the published schema agree.

Two places name the MCP tools: `nmemory.tools.TOOLS` (the dispatch table
`call_tool` reads, from which `MemoryServer.SUPPORTED_TOOLS` derives) and
`tools_schema.json` (what `tools/list` publishes). They can drift silently: a handler with no schema entry is unreachable from any client.
The argument contract the boundary enforces is read from the same schema
(`nmemory.tools.validation`), so it has no copy to drift; these tests hold the
shapes it reads.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from nmemory.server import MemoryServer
from nmemory.tools import TOOLS
from nmemory.tools.validation import (
    ingest_item_spec,
    tool_spec,
    validate_ingest_item,
    validate_tool_arguments,
)

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "nmemory" / "tools_schema.json"


def _catalogue() -> dict[str, dict[str, Any]]:
    tools: list[dict[str, Any]] = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return {tool["name"]: tool["inputSchema"] for tool in tools}


def test_read_only_tools_are_registered() -> None:
    assert set(MemoryServer.READ_ONLY_TOOLS) <= set(TOOLS)


#: The module under `nmemory.tools` that answers each wire name. The wire names
#: are `nmemory_<noun>_<verb>`; the modules keep one short word per tool, so the
#: pairing is written out rather than derived.
MODULE_OF: dict[str, str] = {
    "nmemory_board_show": "board",
    "nmemory_capsule_consolidate": "consolidate",
    "nmemory_capsule_export": "export",
    "nmemory_capsule_import": "import_",
    "nmemory_capsule_ingest": "ingest",
    "nmemory_capsule_list": "list_",
    "nmemory_capsule_pin": "pin",
    "nmemory_capsule_purge": "purge",
    "nmemory_capsule_reanchor": "reanchor",
    "nmemory_capsule_retrieve": "retrieve",
    "nmemory_capsule_show": "get",
    "nmemory_context_show": "context",
    "nmemory_coverage_show": "coverage",
    "nmemory_digest_show": "digest",
    "nmemory_dod_set": "dod",
    "nmemory_index_build": "index",
    "nmemory_kind_classify": "classify",
    "nmemory_outcome_list": "outcome_list",
    "nmemory_outcome_record": "outcome",
    "nmemory_project_bootstrap": "bootstrap",
    "nmemory_proposal_review": "review",
    "nmemory_relation_add": "relate",
    "nmemory_session_finish": "session_finish",
    "nmemory_session_start": "session_start",
    "nmemory_synonym_add": "alias",
    "nmemory_turn_extract": "extract",
    "nmemory_work_list": "work",
}


def test_every_registry_entry_is_its_modules_handle() -> None:
    assert set(MODULE_OF) == set(TOOLS)
    for name, handler in TOOLS.items():
        assert handler.__name__ == "handle", name
        assert handler.__module__ == f"nmemory.tools.{MODULE_OF[name]}", (
            name,
            handler.__module__,
        )


def test_every_wire_name_is_nmemory_noun_verb() -> None:
    for name in TOOLS:
        assert name.startswith("nmemory_"), name
        assert name.count("_") >= 2, name


def test_no_retired_name_is_registered() -> None:
    """The canonical rename retired every alias: no `memory_*` name dispatches."""
    assert not [name for name in TOOLS if name.startswith("memory_")]


def test_the_schema_publishes_exactly_the_registry() -> None:
    assert set(_catalogue()) == set(TOOLS)


@pytest.mark.parametrize("name", sorted(TOOLS))
def test_every_tool_schema_is_a_shape_the_validator_reads(name: str) -> None:
    """`nmemory_capsule_show` and `nmemory_capsule_ingest` are unions, coded by
    hand; every other tool is a closed object whose properties all resolve."""
    schema = _catalogue()[name]
    spec = tool_spec(name)
    if name in {"nmemory_capsule_show", "nmemory_capsule_ingest"}:
        assert spec is None
        return
    assert schema.get("additionalProperties") is False, name
    assert spec is not None
    assert set(spec.types) == set(schema.get("properties", {})), name


def test_a_tool_without_properties_refuses_every_field() -> None:
    from nmemory.tools.validation import DeserializationError

    assert validate_tool_arguments("nmemory_session_start", {}) == {}
    with pytest.raises(DeserializationError, match="there are no fields"):
        validate_tool_arguments("nmemory_session_start", {"stray": 1})


def test_a_schema_shape_the_validator_cannot_read_is_refused() -> None:
    """Falsifier of the test above: a property of two types has no one check."""
    from nmemory.tools.validation import _spec_of

    schema = {"properties": {"x": {"type": ["string", "integer"]}}}
    with pytest.raises(ValueError):
        _spec_of(schema, schema)


def test_ingest_item_matches_the_schema() -> None:
    schema = _catalogue()["nmemory_capsule_ingest"]
    item = schema["$defs"]["IngestItemParams"]
    forms = schema["anyOf"]
    assert forms[0]["properties"]["items"]["items"] == {
        "$ref": "#/$defs/IngestItemParams"
    }
    assert forms[0]["required"] == ["items"]
    assert forms[1]["$ref"] == "#/$defs/IngestItemParams"
    assert item["additionalProperties"] is False
    assert item["properties"]["derived_from"]["items"] == {"type": "string"}
    assert set(ingest_item_spec().types) == set(item["properties"])


def test_ingest_item_required_fields_are_refused_at_the_boundary_or_the_gate() -> None:
    """Every schema-required field is refused when absent, in one of two places.

    `content`, `source` and `anchor` are shape faults the validator refuses.
    `kind` passes the validator on purpose: the handler refuses it with the
    teaching `MISSING_KIND_HINT`, which a plain "missing field" would lose.
    """
    required = _catalogue()["nmemory_capsule_ingest"]["$defs"]["IngestItemParams"][
        "required"
    ]
    assert set(required) == {"content", "source", "anchor", "kind"}
    whole = {"content": "c", "source": "s", "anchor": "a", "kind": "fact"}
    assert validate_ingest_item(whole) is None
    for field in ("content", "source", "anchor"):
        partial = {k: v for k, v in whole.items() if k != field}
        refusal = validate_ingest_item(partial)
        assert refusal is not None
        assert refusal[1].startswith(f"missing field `{field}`"), refusal
    assert validate_ingest_item({k: v for k, v in whole.items() if k != "kind"}) is None


def test_get_forms_match_the_schema() -> None:
    schema = _catalogue()["nmemory_capsule_show"]
    single, batch = schema["anyOf"]
    assert single["required"] == ["id"]
    assert single["properties"]["id"]["type"] == "string"
    assert batch["required"] == ["ids"]
    assert batch["properties"]["ids"]["type"] == "array"
    assert batch["properties"]["ids"]["items"] == {"type": "string"}
    assert validate_tool_arguments("nmemory_capsule_show", {"id": "x"}) == {"id": "x"}
    assert validate_tool_arguments("nmemory_capsule_show", {"ids": ["x"]}) == {
        "ids": ["x"]
    }


def test_an_unknown_field_refusal_lists_the_schema_fields_in_schema_order() -> None:
    """The refusal names the tool's fields in the order its served schema lists them."""
    with pytest.raises(ValueError) as caught:
        validate_tool_arguments("nmemory_synonym_add", {"unexpected": 1})
    assert str(caught.value) == "unknown field `unexpected`, expected `alias` or `term`"
