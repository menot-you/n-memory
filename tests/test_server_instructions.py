"""Tests for MCP server instructions contract and AST invariants."""

from __future__ import annotations

import ast
import re
from pathlib import Path

from nmemory.server import SERVER_INSTRUCTIONS, MemoryServer, build


def test_server_instructions_initialize_contract() -> None:
    """MCP initialize served instructions contain Triggers within first 2048 bytes and fit budget."""
    server = MemoryServer(store=None)  # type: ignore[arg-type]
    instructions = build(server).instructions
    assert isinstance(instructions, str)
    assert instructions == SERVER_INSTRUCTIONS

    # Instructions must fit the <= 2048 bytes ceiling
    inst_bytes = instructions.encode("utf-8")
    assert len(inst_bytes) <= 2048, (
        f"SERVER_INSTRUCTIONS length {len(inst_bytes)}B exceeds 2048B limit"
    )

    assert b"Triggers" in inst_bytes[:2048], (
        "Triggers missing from first 2048 bytes of instructions"
    )

    # Essential sections check: identity and laws
    assert "nmemory:" in instructions
    assert "ADVISORY_NOT_AUTHORITY" in instructions
    assert (
        "nmemory is mandatory — if unavailable, restore it before substantive work."
        in instructions
    )


def test_server_instructions_single_ast_assignment() -> None:
    """server.py must have exactly one module-level assignment to SERVER_INSTRUCTIONS."""
    server_path = Path(__file__).resolve().parent.parent / "nmemory" / "server.py"
    assert server_path.exists(), f"server.py not found at {server_path}"

    tree = ast.parse(server_path.read_text(encoding="utf-8"), filename=str(server_path))

    assignments: list[int] = []
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name) and target.id == "SERVER_INSTRUCTIONS":
                    assignments.append(stmt.lineno)
        elif (
            isinstance(stmt, ast.AnnAssign)
            and isinstance(stmt.target, ast.Name)
            and stmt.target.id == "SERVER_INSTRUCTIONS"
        ):
            assignments.append(stmt.lineno)

    assert len(assignments) == 1, (
        f"Expected exactly 1 module-level assignment to SERVER_INSTRUCTIONS, found {len(assignments)} at lines {assignments}"
    )


def test_every_tool_the_instructions_name_is_served() -> None:
    """A tool the instructions route to is one tools/list serves, in both modes: a
    renamed or retired `nmemory_*` name fails here instead of reaching a model.
    Bare words are not caught."""
    for read_only in (False, True):
        server = MemoryServer(store=None, read_only=read_only)  # type: ignore[arg-type]
        served = {tool["name"] for tool in server.get_tool_definitions()}
        text = build(server).instructions or ""
        named = set(re.findall(r"nmemory_[a-z_]+", text))
        assert named, "the instructions name no nmemory tool"
        assert named <= served, sorted(named - served)
