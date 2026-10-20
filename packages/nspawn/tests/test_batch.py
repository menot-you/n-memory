"""`nspawn_batch_start`: N executors, one prompt, one tree — caged by construction.

The owner's ask, 2026-08-29: fire N executors under a single prompt, for
exploration and for review. The shape decides the safety:

**One `cwd` means N children in ONE tree.** They cannot all be writers — that is
d28's one-writer law, and the worktree claim would kill N-1 of them anyway. So
the batch is read-only WITHOUT a knob: a knob here could only ever name a mode
the tool's own shape refuses to serve. Writers in fan-out are N `nspawn_run_start`
calls into N worktrees, which is a different act.

Refusal splits in two, because the two failures need different answers:

* a name the roster does not declare is the CALLER's error, and it fails the
  whole call BEFORE anything is dispatched — a typo that half-runs burns tokens
  and leaves the caller reconciling a partial fan-out;
* a wall, a quota or a missing credential is the WORLD's, and it fails only its
  own entry: the point of a fan-out is the entries that can still run.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from nspawn import mcp as mcp_module
from nspawn.errors import NERR_1112, NSpawnError
from nspawn.mcp import NSPAWN_TOOL_BY_NAME, McpError
from nspawn.rows import RunPhase


def _schema() -> dict[str, Any]:
    return NSPAWN_TOOL_BY_NAME["nspawn_batch_start"].input_schema


def test_the_contract_adds_only_shared_dispatch_context_to_required_fields() -> None:
    schema = _schema()
    assert set(schema["required"]) == {"prompt", "cwd", "providers"}
    # Cut deliberately: `model` and `effort` are PER-PROVIDER vocabularies —
    # `gpt-5.6-sol` and `glm-5.3[1m]` are not interchangeable, and `ultra`
    # exists on some routes where `max` is the whole ladder on others. One
    # batch-wide pin would refuse most of the fan-out it was asked to spread.
    # `session_id` would collide N ways; `timeout_ms` is what `nspawn_run_show`
    # is for. `read_paths` and `system_prompt` are shared dispatch context, so
    # every caged child receives the same readable trees and identity.
    # `idempotency_key` and `group` (U3) are dispatch identity, not a per-route
    # vocabulary: entries are keyed `key/index` and carry one group.
    assert set(schema["properties"]) == {
        "prompt",
        "cwd",
        "providers",
        "read_paths",
        "system_prompt",
        "idempotency_key",
        "group",
    }
    providers = schema["properties"]["providers"]
    assert providers["type"] == "array"
    assert providers["items"]["type"] == "string"
    assert providers["minItems"] == 1


def test_the_cage_has_no_knob_here() -> None:
    assert "read_only" not in _schema()["properties"]


def test_an_array_of_the_wrong_thing_is_refused_by_type() -> None:
    """Without `items`, `providers: [1, 2]` walks straight past the validator."""
    with pytest.raises(McpError):
        mcp_module.validate_arguments(
            _schema(), {"prompt": "p", "cwd": "/tmp", "providers": [1, 2]}
        )
    with pytest.raises(McpError):
        mcp_module.validate_arguments(
            _schema(), {"prompt": "p", "cwd": "/tmp", "providers": []}
        )
    with pytest.raises(McpError):
        mcp_module.validate_arguments(
            _schema(), {"prompt": "p", "cwd": "/tmp", "providers": "codex1"}
        )


def test_a_boolean_argument_is_checked_as_one() -> None:
    """Shipped 2026-08-29 and never validated: `read_only` is a boolean the
    validator did not know, so `"false"` — a string every shell produces —
    passed and read as TRUE. It caged when the caller asked for no cage.
    """
    execute = NSPAWN_TOOL_BY_NAME["nspawn_run_start"].input_schema
    with pytest.raises(McpError):
        mcp_module.validate_arguments(
            execute, {"prompt": "p", "cwd": "/tmp", "read_only": "false"}
        )


class _Spy:
    """Records what the batch would dispatch, and can fail one named entry."""

    def __init__(self, declared: tuple[str, ...], world_error: str | None = None):
        self.declared = declared
        self.world_error = world_error
        self.calls: list[tuple[str, bool]] = []

    def declared_providers(self, cwd: str) -> tuple[frozenset[str], Path | None]:
        """One roster read for the whole batch — the roster is per-cwd."""
        return frozenset(self.declared), Path("/fixture/roster.toml")

    def execute(self, prompt: str, cwd: str, **kwargs: Any) -> Any:
        provider = str(kwargs["provider"])
        if provider == self.world_error:
            raise NSpawnError(NERR_1112, "account wall is up")
        self.calls.append((provider, bool(kwargs["read_only"])))

        class _Accepted:
            run_id = f"run-{provider}"
            phase = RunPhase.SPAWNING

        return _Accepted()


def test_every_entry_is_dispatched_caged(monkeypatch: Any) -> None:
    spy = _Spy(("codex1", "agy", "zai"))
    result = mcp_module.batch_runs(spy, "explore this", "/tmp", ["codex1", "agy"])
    assert spy.calls == [("codex1", True), ("agy", True)]
    assert [row["provider"] for row in result["runs"]] == ["codex1", "agy"]
    assert result["refused"] == []


def test_an_undeclared_name_fails_the_call_before_anything_is_dispatched() -> None:
    spy = _Spy(("codex1", "agy"))
    with pytest.raises(McpError) as caught:
        mcp_module.batch_runs(spy, "explore", "/tmp", ["codex1", "nosuch"])
    assert "nosuch" in str(caught.value)
    assert spy.calls == []


def test_a_wall_fails_its_own_entry_and_the_siblings_still_run() -> None:
    spy = _Spy(("codex1", "agy", "zai"), world_error="agy")
    result = mcp_module.batch_runs(spy, "explore", "/tmp", ["codex1", "agy", "zai"])
    assert [row["provider"] for row in result["runs"]] == ["codex1", "zai"]
    assert len(result["refused"]) == 1
    assert result["refused"][0]["provider"] == "agy"
    assert "wall" in result["refused"][0]["reason"]


def test_a_repeated_provider_is_two_runs() -> None:
    """Two takes from one route is a legitimate sample, so it is allowed —
    just never advertised as free: the owner's per-account ceiling has no
    executable home yet, and inventing enforcement here would be a gate with
    no consumer."""
    spy = _Spy(("codex1",))
    result = mcp_module.batch_runs(spy, "explore", "/tmp", ["codex1", "codex1"])
    assert len(result["runs"]) == 2


@pytest.mark.parametrize(
    "refusal",
    [
        {"type": "IdempotencyBusy", "message": "lock held", "retryable": True},
        {"type": "FingerprintUnavailable", "message": "git slow", "retryable": True},
        {"type": "IdempotencyConflict", "message": "other request"},
    ],
)
def test_a_keyed_head_refused_for_its_key_refuses_the_whole_batch(
    refusal: dict[str, Any],
) -> None:
    """A retryable or key-level head refusal says nothing about the provider:
    dispatching the rest would interleave this batch with the one holding K."""
    sent: list[list[dict[str, Any]]] = []

    def dispatch(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        sent.append(entries)
        return [{"refused": refusal}] + [
            {"run_id": f"run-{n}", "phase": "spawning"} for n in range(len(entries) - 1)
        ]

    spy = _Spy(("a", "b", "c"))
    result = mcp_module.batch_runs(
        spy, "p", "/tmp", ["a", "b", "c"], dispatch=dispatch, idempotency_key="K"
    )
    assert len(sent) == 1, sent
    assert result["runs"] == []
    assert [entry["type"] for entry in result["refused"]] == [refusal["type"]] * 3


def test_a_keyed_head_walled_by_the_world_still_dispatches_its_siblings() -> None:
    sent: list[list[dict[str, Any]]] = []

    def dispatch(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        sent.append(entries)
        if len(sent) == 1:
            return [{"refused": {"type": "NSpawnError", "message": "wall"}}]
        return [{"run_id": "run-b", "phase": "spawning"}]

    result = mcp_module.batch_runs(
        _Spy(("a", "b")),
        "p",
        "/tmp",
        ["a", "b"],
        dispatch=dispatch,
        idempotency_key="K",
    )
    assert [run["provider"] for run in result["runs"]] == ["b"]
    assert len(sent) == 2
