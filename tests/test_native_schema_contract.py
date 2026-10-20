"""Portable object schemas preserve both disjoint forms of the frozen contract."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import nmemory.server as server_module
from nmemory.server import MemoryServer


def test_native_union_projection_preserves_the_frozen_input_domains() -> None:
    source = Path(__file__).parents[1] / "nmemory" / "tools_schema.json"
    original = source.read_bytes()
    raw = {tool["name"]: tool for tool in json.loads(original)}
    served = {tool["name"]: tool for tool in MemoryServer().get_tool_definitions()}
    assert served.keys() == raw.keys()
    for name, discriminator in (
        ("nmemory_capsule_show", "ids"),
        ("nmemory_capsule_ingest", "items"),
    ):
        before, after = raw[name]["inputSchema"], served[name]["inputSchema"]
        assert not {"anyOf", "oneOf", "allOf"} & after.keys()
        variants = before["anyOf"]
        assert len(variants) == 2

        def resolved(
            variant: dict[str, Any], schema: dict[str, Any] = before
        ) -> dict[str, Any]:
            if "$ref" not in variant:
                return variant
            reference: str = variant["$ref"]
            assert reference.startswith("#/$defs/")
            definition: dict[str, Any] = schema["$defs"][
                reference.removeprefix("#/$defs/")
            ]
            return definition

        batch = next(v for v in variants if discriminator in resolved(v)["properties"])
        single = next(v for v in variants if v is not batch)
        # This partitions every input: the batch requires the discriminator,
        # while the single form rejects it as an additional property.
        assert discriminator in resolved(batch)["required"]
        assert discriminator not in resolved(single)["properties"]
        assert all(resolved(v)["additionalProperties"] is False for v in variants)
        assert after["if"] == {"required": [discriminator]}
        assert after["then"] == batch
        assert after["else"] == single
        assert after["properties"] == {
            **resolved(single)["properties"],
            **resolved(batch)["properties"],
        }
        assert after["additionalProperties"] is False
        assert all(
            after[key] == value for key, value in before.items() if key != "anyOf"
        )
    assert all(
        served[name] == tool
        for name, tool in raw.items()
        if name not in {"nmemory_capsule_show", "nmemory_capsule_ingest"}
    )
    assert source.read_bytes() == original


@pytest.mark.parametrize(
    "change", ["arity", "reference", "open", "optional", "overlap"]
)
def test_native_projection_refuses_unproved_object_partitions(
    change: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = Path(__file__).parents[1] / "nmemory" / "tools_schema.json"
    tools = json.loads(source.read_bytes())
    schema = next(
        t["inputSchema"] for t in tools if t["name"] == "nmemory_capsule_ingest"
    )
    batch, single = schema["anyOf"]
    if change == "arity":
        schema["anyOf"].append(batch)
    elif change == "reference":
        single["$ref"] = "https://example.invalid/foreign-schema"
    elif change == "open":
        batch["additionalProperties"] = True
    elif change == "optional":
        batch["required"] = []
    else:
        batch["properties"]["content"] = {"type": "string"}
    (tmp_path / "tools_schema.json").write_text(json.dumps(tools))
    monkeypatch.setattr(server_module, "__file__", str(tmp_path / "server.py"))
    with pytest.raises(ValueError, match="native schema"):
        MemoryServer().get_tool_definitions()
