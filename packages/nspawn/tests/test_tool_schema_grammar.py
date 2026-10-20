"""Every nSPAWN tool schema compiles under a strict tool-call grammar.

vLLM 0.31 with `--tool-strict-level parameter` compiles each tool schema with
xgrammar, which refuses an object whose `minProperties` is above the count of
its declared properties unless `additionalProperties` is true. The qwen route
then fails every turn with `400 Invalid grammar specification` (2026-10-20),
whatever the turn asks. This walks every schema `tools/list` serves.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from nspawn import main


def _objects(schema: Any, path: str) -> Iterator[tuple[str, dict[str, Any]]]:
    """Each object schema inside `schema`, with its path."""
    if isinstance(schema, list):
        for index, item in enumerate(schema):
            yield from _objects(item, f"{path}[{index}]")
        return
    if not isinstance(schema, dict):
        return
    if schema.get("type") == "object" or "properties" in schema:
        yield path, schema
    for key, value in schema.items():
        if key in {"properties", "$defs", "definitions", "patternProperties"}:
            for name, child in value.items():
                yield from _objects(child, f"{path}.{key}.{name}")
        elif key in {"items", "additionalProperties", "anyOf", "oneOf", "allOf", "not"}:
            yield from _objects(value, f"{path}.{key}")


def _refused(schema: dict[str, Any]) -> bool:
    """The xgrammar rule: more properties required than declared, and no
    other property allowed."""
    declared = len(schema.get("properties", {}))
    return (
        schema.get("minProperties", 0) > declared
        and schema.get("additionalProperties") is not True
    )


def test_every_tool_schema_compiles_under_the_strict_grammar() -> None:
    listed = [tool.model_dump(by_alias=True, exclude_none=True) for tool in main.TOOLS]
    walked = {
        f"{tool['name']}{path}": schema
        for tool in listed
        for field in ("inputSchema", "outputSchema")
        for path, schema in _objects(tool.get(field), "")
    }
    # The walk reaches the nested maps, so an empty answer is not a vacuous one.
    assert "nspawn_judgment_estimate.properties.questions" in walked
    assert [path for path, schema in walked.items() if _refused(schema)] == []


def test_the_rule_refuses_the_shape_that_broke_the_qwen_route() -> None:
    broken = {"type": "object", "minProperties": 1}
    assert _refused(broken)
    assert not _refused({**broken, "additionalProperties": True})
    assert not _refused({"type": "object", "properties": {"a": {}}, "minProperties": 1})
