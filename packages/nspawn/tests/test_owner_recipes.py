"""The owner's recipes, enforced against what nSPAWN actually composes.

These six invocations lived in `CLAUDE.md` as prose and were lost in a rewrite —
three times. Prose refuses nobody: `--append-system-prompt-file` has been a
declared parameter of `compose_child_argv` since `9db4f19` while the supervisor
never passed it, so every worker nSPAWN has ever spawned ran WITHOUT the NOTT
system prompt, and 151 green tests said nothing because each asserts on the argv
it builds itself rather than on the argv the supervisor builds.

This file derives its expectations from `fixtures/owner_recipes.txt` instead of
restating them, so the fixture stays the single place a recipe is written.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest

RECIPES = Path(__file__).parent / "fixtures" / "owner_recipes.txt"


def parse_recipes() -> dict[str, tuple[dict[str, str], list[str]]]:
    """Each recipe as (env assignments, claude flags), read from the fixture."""
    text = RECIPES.read_text(encoding="utf-8")
    out: dict[str, tuple[dict[str, str], list[str]]] = {}
    for name, block in re.findall(
        r"^## (\w+)\n\n```bash\n(.*?)\n```", text, re.MULTILINE | re.DOTALL
    ):
        words = shlex.split(block, posix=False)
        env: dict[str, str] = {}
        index = 0
        while (
            index < len(words)
            and "=" in words[index]
            and not words[index].startswith("-")
        ):
            key, _, value = words[index].partition("=")
            env[key] = value
            index += 1
        assert words[index] == "claude", f"{name}: recipe does not invoke `claude`"
        out[name] = (env, words[index + 1 :])
    return out


def test_the_fixture_still_carries_all_six_recipes() -> None:
    # A rewrite that empties the fixture must not silently pass every test below.
    recipes = parse_recipes()
    assert set(recipes) == {"CLAUDE", "CODEX", "FUGU", "AGY", "MINIMAX", "ZAI"}


@pytest.mark.parametrize("name", ["CLAUDE", "CODEX", "FUGU", "AGY", "MINIMAX", "ZAI"])
def test_every_recipe_appends_the_nott_system_prompt(name: str) -> None:
    _, flags = parse_recipes()[name]
    assert "--append-system-prompt-file" in flags


@pytest.mark.parametrize("name", ["CLAUDE", "CODEX", "FUGU", "AGY", "MINIMAX", "ZAI"])
def test_no_recipe_pins_the_model_or_effort_on_the_command_line(name: str) -> None:
    _, flags = parse_recipes()[name]
    assert "--model" not in flags
    assert "--effort" not in flags
    assert "--strict-mcp-config" not in flags


def compose_as_the_supervisor_does(model: str | None, effort: str | None) -> list[str]:
    """The argv with EXACTLY the arguments `supervisor.py:1142` hands over."""
    from nspawn import child
    from nspawn.owner_roster import Roster, expand_value_with

    roster = Roster.read(RECIPES.parent / "owner_roster.toml")
    resolved = roster.resolve(None)
    return child.compose_child_argv(
        "/usr/bin/claude",
        plugin_dir=Path("/plugin"),
        declared_args=[
            expand_value_with(arg, resolved.plugin_root) for arg in resolved.args
        ],
    )


def test_the_system_prompt_reaches_the_composed_argv() -> None:
    """Guard, not discovery: this landed in another lane and must not regress.

    `append_system_prompt_file` was a declared parameter that `supervisor.py`
    never passed, so every worker ran without the NOTT prompt. It is passed now,
    through the roster's `[defaults] args`; this pins that.
    """
    argv = compose_as_the_supervisor_does("claude-opus-5", "max")
    assert "--append-system-prompt-file" in argv
    assert "--strict-mcp-config" not in argv


def test_the_model_and_effort_leave_the_command_line() -> None:
    """Owner ruling 2026-08-29: the model travels in the environment, never argv.

    All six recipes set ANTHROPIC_MODEL and friends and NONE passes `--model`;
    four set CLAUDE_CODE_EFFORT_LEVEL and none passes `--effort`. A `--model` on
    the line also fights the provider's own env: `zai` is invoked today with
    ANTHROPIC_MODEL=glm-5.3[1m] AND `--model claude-opus-5`.
    """
    argv = compose_as_the_supervisor_does("claude-opus-5", "max")
    assert "--model" not in argv, (
        "the model must travel in ANTHROPIC_MODEL, never on argv"
    )
    assert "--effort" not in argv, "the effort must travel in CLAUDE_CODE_EFFORT_LEVEL"


def test_the_effort_travels_in_the_environment_never_as_a_flag() -> None:
    recipes = parse_recipes()
    with_effort = {
        n for n, (env, _) in recipes.items() if "CLAUDE_CODE_EFFORT_LEVEL" in env
    }
    assert with_effort == {"CODEX", "AGY", "MINIMAX", "ZAI"}
    for name in with_effort:
        _, flags = recipes[name]
        assert "--effort" not in flags


def test_every_proxied_recipe_sets_the_five_model_variables() -> None:
    """The model is chosen by env, per recipe — never by `--model`."""
    five = {
        "ANTHROPIC_MODEL",
        "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
        "CLAUDE_CODE_SUBAGENT_MODEL",
    }
    for name in ("CODEX", "FUGU", "AGY", "MINIMAX", "ZAI"):
        env, _ = parse_recipes()[name]
        assert five <= set(env), f"{name} is missing {sorted(five - set(env))}"
