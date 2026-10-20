"""A read-only dispatch is a CAGE, never a sentence in the prompt.

F-026, measured 2026-08-29. `nspawn_review_start` has described itself as "read-only"
since it existed, and nothing enforced it: `execution_class` only feeds provider
ranking (`picker.py`), and `compose_child_argv` passes `--setting-sources user`,
which loads the operator's `~/.claude/settings.json` — where
`permissions.defaultMode` is `bypassPermissions`. Every child, review included,
ran with the checks off.

The cage is `claude --restricted`, chosen by measurement, not by reading:

| | | |
|---|---|---|
| A | no flag | the child WROTE `HACKED` into the target |
| B | `--restricted` | `BLOQUEADO Write`, file untouched |
| C | `--restricted --tools Bash,Read,Write` | still blocked — `-p` has nobody to approve, so it fails CLOSED |
| D | `--restricted --tools Bash,...` | `git log --oneline -1` works: a review stays usable |
| E | same | `echo >` and `tee` both refused |
| G | plus `--plugin-dir` | `ToolSearch` loads deferred schemas; cage admits observational `nmemory_context_show`; no mutator |

`--tools` closes only the built-in set; MCP admission uses `--allowedTools` under
`--permission-mode dontAsk`. The floor is deliberate: read-only children get
the readable built-ins, reply messaging, deferred-schema discovery and
nMEMORY's canonical context read.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nspawn import child
from nspawn.mcp import NSPAWN_TOOL_CONTRACTS

NMEMORY_CONTEXT_TOOL = "mcp__plugin_nott_nmemory__nmemory_context_show"
MUTATING_NMEMORY_TOOLS = {
    f"mcp__plugin_nott_nmemory__memory_{verb}"
    for verb in (
        "alias",
        "classify",
        "consolidate",
        "dod",
        "forget",
        "import",
        "index",
        "ingest",
        "outcome",
        "pin",
        "preference",
        "reanchor",
        "relate",
        "retrieve",
        "session_finish",
        "session_start",
    )
}


def _contract(name: str) -> dict[str, object]:
    for contract in NSPAWN_TOOL_CONTRACTS:
        if contract.name == name:
            return contract.input_schema
    raise AssertionError(f"no contract named {name!r}")


def _composed_read_only_tools() -> set[str]:
    argv = child.compose_child_argv("/bin/claude", read_only=True)
    return set(argv[argv.index("--tools") + 1].split(","))


def _composed_read_only_mcp_tools() -> set[str]:
    argv = child.compose_child_argv("/bin/claude", read_only=True)
    return set(argv[argv.index("--allowedTools") + 1].split(","))


def test_read_only_argv_carries_the_cage() -> None:
    argv = child.compose_child_argv("/bin/claude", read_only=True)
    assert "--restricted" in argv
    assert set(argv[argv.index("--tools") + 1].split(",")) == {
        "Bash",
        "Read",
        "Grep",
        "Glob",
        "SendMessage",
        "ToolSearch",
    }
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert argv[argv.index("--allowedTools") + 1].split(",") == [
        "mcp__plugin_nott_nmemory__nmemory_context_show",
    ]
    assert "--" not in argv  # the prompt rides on stdin


def test_a_writable_dispatch_is_not_caged() -> None:
    argv = child.compose_child_argv(
        "/bin/claude",
    )
    for flag in ("--restricted", "--tools", "--permission-mode", "--allowedTools"):
        assert flag not in argv


def test_roster_args_cannot_widen_the_read_only_policy() -> None:
    attempts = (
        ["--tools", "Write"],
        ["--allowedTools", "mcp__plugin_nott_nmemory__nmemory_capsule_purge"],
        ["--allowed-tools", "mcp__plugin_nott_nmemory__nmemory_capsule_purge"],
        ["--permission-mode=bypassPermissions"],
    )
    for declared_args in attempts:
        with pytest.raises(child.ChildEnvironmentError):
            child.compose_child_argv(
                "/bin/claude",
                read_only=True,
                declared_args=declared_args,
            )


def test_the_cage_names_no_mutating_tool() -> None:
    forbidden = {"Write", "Edit", "NotebookEdit", "Task"}
    assert forbidden.isdisjoint(child.READ_ONLY_TOOLS)


def test_the_cage_names_no_mutating_nmemory_tool() -> None:
    assert MUTATING_NMEMORY_TOOLS.isdisjoint(_composed_read_only_mcp_tools())


def test_caged_child_env_omits_openai_api_key() -> None:
    """The closed base environment for child processes never passes OPENAI_API_KEY."""
    from nspawn.supervisor import _allowed_base_environ

    base_env = _allowed_base_environ()
    assert "OPENAI_API_KEY" not in base_env


def test_execute_offers_read_only_and_review_does_not() -> None:
    """The knob belongs to `execute`; `review` IS read-only and cannot opt out.

    A review that can be asked to write is the exact promise that broke.
    """
    execute = _contract("nspawn_run_start")
    properties = execute["properties"]
    assert isinstance(properties, dict)
    assert properties["read_only"]["type"] == "boolean"
    assert "read_only" not in _contract("nspawn_review_start")["properties"]


def _native_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """A git checkout under the one root a roster declares for native writers:
    the only writable dispatch admission still takes."""
    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)
    roster = tmp_path / "spawn.toml"
    roster.write_text(f"[native_write]\nroots = {json.dumps([str(tmp_path)])}\n")
    monkeypatch.setenv("NOTT_NSPAWN_ROSTER", str(roster))
    monkeypatch.setenv("NOTT_NSPAWN_CONFIG", str(tmp_path / "absent-spawn.toml"))
    return str(checkout)


def test_the_knob_reaches_admission_and_review_is_never_writable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The seam a schema-only knob would have left orphaned.

    This repository has shipped mechanisms nothing called; the test that would
    have caught them looks at the CONSUMER, not at the declaration. So: the two
    ends. `compose_child_argv` is proved above; here the wire from the verb down
    to admission. The three positional hops between them (`_admit` →
    `_drive_run` → `_spawn_and_wait`) are held by `mypy --strict`, which fails
    on an arity or type mismatch — named so nobody reads this file as covering
    them by observation.
    """
    from nspawn import supervisor as sup

    seen: list[bool] = []

    def _record(self: object, *args: object, **kwargs: object) -> None:
        seen.append(bool(kwargs["read_only"]))

    monkeypatch.setattr(sup.Supervisor, "_admit", _record)
    bare = sup.Supervisor.__new__(sup.Supervisor)
    checkout = _native_checkout(tmp_path, monkeypatch)

    sup.Supervisor.review(bare, "lane/x", "/tmp", base="main")
    # A writable dispatch that is not a native write never reaches admission.
    with pytest.raises(sup.InvalidRunRequest, match="native_write"):
        sup.Supervisor.execute(bare, "do it", "/tmp")
    sup.Supervisor.execute(bare, "do it", checkout, native_write=True)
    sup.Supervisor.execute(bare, "audit it", "/tmp", read_only=True)

    assert seen == [True, False, True]


def test_a_reader_does_not_hold_the_writer_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lease serialises WRITERS; a caged child is not one.

    Holding it for a read-only run blocks a legitimate execute on the same tree
    while protecting nothing — and it is why several audits could not share one
    worktree.
    """
    from nspawn import supervisor as sup

    seen: list[tuple[bool, bool]] = []

    def _record(self: object, *args: object, **kwargs: object) -> None:
        seen.append((bool(kwargs["claim_worktree"]), bool(kwargs["read_only"])))

    monkeypatch.setattr(sup.Supervisor, "_admit", _record)
    bare = sup.Supervisor.__new__(sup.Supervisor)
    checkout = _native_checkout(tmp_path, monkeypatch)

    # The writer is a native write: the one writable mode admission takes.
    sup.Supervisor.execute(bare, "build it", checkout, native_write=True)
    sup.Supervisor.execute(bare, "audit it", "/tmp", read_only=True)
    sup.Supervisor.review(bare, "lane/x", "/tmp")

    assert seen == [(True, False), (False, True), (False, True)]


def test_the_cage_admits_send_message() -> None:
    """A caged child MUST be able to answer its caller. Owner order 2026-08-31.

    Measured that day: `nspawn_run_start` with `read_only=True` produced a child
    that replied, in Portuguese, "the ListAgents and SendMessage tools do not
    exist in my registered toolset" — the cage's `--tools` allowlist (`child.py:518`)
    left it mute. That silences exactly the long runs nobody wants to poll, and
    `nspawn_review_start`/`nspawn_batch_start` are caged by construction.

    The tension is named, not hidden: a message DOES mutate another session's
    inbox, so this is the one write the cage grants. It is bounded — a message
    carries no filesystem, store or network authority, and the peer decides what
    to do with it. `ListAgents` deliberately stays OUT: bare-name and `uds:`
    addressing need no listing, so discovery is not part of the grant.
    """
    assert "SendMessage" in _composed_read_only_tools()


def _description(name: str) -> str:
    for contract in NSPAWN_TOOL_CONTRACTS:
        if contract.name == name:
            return contract.description
    raise AssertionError(f"no contract named {name!r}")


def test_d3_the_cage_admits_no_shell_by_allowlist_and_says_so() -> None:
    """That IS the cage: under
    `dontAsk` the vendor auto-admits only what it classifies as read-only and
    refuses everything else with nobody to ask. Widening it would take a
    `Bash(...)` allow pattern, and `bash -c` runs writes as well as reads, so
    the grant stays where it is and the CONTRACT names the limit — a reviewer
    dispatched to DRIVE a gate was caged because nothing said it could not."""
    argv = child.compose_child_argv("/bin/claude", read_only=True)
    allowed = argv[argv.index("--allowedTools") + 1].split(",")
    assert not any(entry.startswith("Bash") for entry in allowed)
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    execute = _contract("nspawn_run_start")["properties"]
    assert isinstance(execute, dict)
    for text in (
        execute["read_only"]["description"],
        _description("nspawn_review_start"),
        child.compose_child_argv.__doc__,
    ):
        assert isinstance(text, str)
        assert "never runs a script" in text
        assert "not caged" in text


def test_the_cage_denies_git_by_name() -> None:
    """Measured 2026-10-09 on claude 2.1.295 in a caged child (`--restricted
    --permission-mode dontAsk`) run inside a repository: without a deny rule
    plain `git log`, `git status`, `git rev-parse`, `cd . && git log`,
    `command git log`, `"git" log`, `\\git log`, `time git log` and
    `nice git log` all RAN; with `--disallowedTools Bash(git:*)` every one was
    denied while `ls -a` and `pwd` still ran. A repository's own config can
    make a read-only git spelling run a program, so the cage grants no git."""
    argv = child.compose_child_argv("/bin/claude", read_only=True)
    denied = argv[argv.index("--disallowedTools") + 1].split(",")
    assert "Bash(git:*)" in denied
    assert "--disallowedTools" not in child.compose_child_argv("/bin/claude")
    for text in (
        _contract("nspawn_run_start")["properties"]["read_only"]["description"],  # type: ignore[index]
        child.compose_child_argv.__doc__,
    ):
        assert isinstance(text, str)
        assert "Bash(git:*)" in text


@pytest.mark.parametrize("flag", ["--disallowedTools", "--disallowed-tools"])
def test_roster_args_cannot_touch_the_git_deny(flag: str) -> None:
    with pytest.raises(child.ChildEnvironmentError):
        child.compose_child_argv(
            "/bin/claude", read_only=True, declared_args=[flag, "Read"]
        )
