from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

SCHEMA_PATH = Path(__file__).parents[1] / "nmemory" / "tools_schema.json"
EXPECTED_NAMES = (
    "nmemory_board_show",
    "nmemory_capsule_consolidate",
    "nmemory_capsule_export",
    "nmemory_capsule_import",
    "nmemory_capsule_ingest",
    "nmemory_capsule_list",
    "nmemory_capsule_pin",
    "nmemory_capsule_purge",
    "nmemory_capsule_reanchor",
    "nmemory_capsule_retrieve",
    "nmemory_capsule_show",
    "nmemory_context_show",
    "nmemory_coverage_show",
    "nmemory_digest_show",
    "nmemory_dod_set",
    "nmemory_index_build",
    "nmemory_kind_classify",
    "nmemory_outcome_list",
    "nmemory_outcome_record",
    "nmemory_project_bootstrap",
    "nmemory_proposal_review",
    "nmemory_relation_add",
    "nmemory_session_finish",
    "nmemory_session_start",
    "nmemory_synonym_add",
    "nmemory_turn_extract",
    "nmemory_work_list",
)
EXPECTED_NON_DESCRIPTION_SHA256 = (
    "d8f7e126df0ee080a6b3d0399de800a773e23ef4c2b25060f1640125c1972958"
)
MAX_DESCRIPTION_BYTES = 4096
MAX_TOTAL_DESCRIPTION_BYTES = 35_000
# The budget guarded only the TOP description and let inputSchema run unbounded,
# and inputSchema weighs twice as much: measured 2026-09-02, 50.923 B of schema versus
# 29.583 B of description. Every byte here is paid in EVERY session that hook
# ToolSearch loads, and 57%-73% of each inputSchema consists of internal descriptions
# per property and per enum variant -- prose, not contract.
# 2026-09-18: the 25th tool (`nmemory_proposal_review`, the only verb that ratifies/rejects
# a staged proposal) entered with MINIMAL inputSchema — 329 B, zero prose per
# property, top description carries the contract — and the total reached 38.286 B:
# the previous ceiling had 43 B of headroom, so NO new tool fit in it. Raised
# to the measured byte count plus tight headroom; not a license for prose.
# 2026-09-21: the 26th tool (`nmemory_coverage_show`) costs 310 B of minimal structure.
# The measured total is 38.740 B; headroom remains tight and prose stays at the top.
# 2026-09-23: the transcript-compaction tool removed; the total is back to 38.740 B and the
# ceiling returns to its 26-tool value.
MAX_TOTAL_SCHEMA_BYTES = 38_800


def _load_tools() -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return tools


def _validation_errors(tools: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    names = tuple(tool.get("name") for tool in tools)
    if names != EXPECTED_NAMES:
        errors.append(f"tools/names/order: {names!r}")

    sizes: dict[str, int] = {}
    for tool in tools:
        name = str(tool.get("name"))
        description = tool.get("description")
        if not isinstance(description, str):
            errors.append(f"top description is not text: {name}")
            continue
        sizes[name] = len(description.encode("utf-8"))

    oversized = {
        name: size for name, size in sizes.items() if size > MAX_DESCRIPTION_BYTES
    }
    if oversized:
        errors.append(f"individual UTF-8 bytes: {oversized!r}")

    total = sum(sizes.values())
    if total > MAX_TOTAL_DESCRIPTION_BYTES:
        errors.append(f"total UTF-8 bytes: {total} > {MAX_TOTAL_DESCRIPTION_BYTES}")

    schema_total = sum(
        len(
            json.dumps(
                tool.get("inputSchema", {}),
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        for tool in tools
    )
    if schema_total > MAX_TOTAL_SCHEMA_BYTES:
        errors.append(
            f"total inputSchema UTF-8 bytes: {schema_total} > {MAX_TOTAL_SCHEMA_BYTES}"
        )

    non_descriptions = [
        {key: value for key, value in tool.items() if key != "description"}
        for tool in tools
    ]
    canonical = json.dumps(
        non_descriptions,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    digest = hashlib.sha256(canonical).hexdigest()
    if digest != EXPECTED_NON_DESCRIPTION_SHA256:
        errors.append(f"non-description SHA-256: {digest}")
    return errors


def _assert_valid(tools: list[dict[str, Any]]) -> None:
    assert not (errors := _validation_errors(tools)), "\n".join(errors)


def _assert_only_error(tools: list[dict[str, Any]], prefix: str) -> None:
    errors = _validation_errors(tools)
    assert len(errors) == 1 and errors[0].startswith(prefix), errors


def test_tools_schema_budget_and_non_description_contract() -> None:
    tools = _load_tools()
    _assert_valid(tools)

    individual = deepcopy(tools)
    smallest = min(
        individual, key=lambda tool: len(tool["description"].encode("utf-8"))
    )
    for tool in individual:
        if tool is not smallest:
            tool["description"] = ""
    description = smallest["description"]
    smallest["description"] = description + "x" * (
        MAX_DESCRIPTION_BYTES + 500 - len(description.encode("utf-8"))
    )
    _assert_only_error(individual, "individual UTF-8 bytes:")

    total_only = deepcopy(tools)
    missing = (
        MAX_TOTAL_DESCRIPTION_BYTES
        + 1
        - sum(len(tool["description"].encode("utf-8")) for tool in total_only)
    )
    for tool in total_only:
        description = tool["description"]
        added = min(MAX_DESCRIPTION_BYTES - len(description.encode("utf-8")), missing)
        tool["description"] = description + "x" * added
        missing -= added
    assert missing == 0
    _assert_only_error(total_only, "total UTF-8 bytes:")

    nested = deepcopy(tools)
    assert nested[0]["inputSchema"]["type"] == "object"
    nested[0]["inputSchema"]["type"] = "objects"
    _assert_only_error(nested, "non-description SHA-256:")
