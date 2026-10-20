"""U4: one dispatch carries its exact system instructions end to end."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
from class_dispatch import DECLARED_CLASS
from mcp_fakes import FakeSupervisor, handle

from nspawn.limits import MAX_DURABLE_ROW_BYTES
from nspawn.mcp import INVALID_PARAMS, NSPAWN_TOOL_CONTRACTS, McpServer
from nspawn.supervisor import (
    InvalidRunRequest,
    RunAccepted,
    Supervisor,
    agent_system_prompt,
    review_branch,
)

SYSTEM_PROMPT = "Persona: cite `evidence`.\nDo not interpret: $(printf LEAK); --flag\n"


def _contract(name: str) -> dict[str, Any]:
    return next(
        contract.input_schema
        for contract in NSPAWN_TOOL_CONTRACTS
        if contract.name == name
    )


def _request(server: McpServer, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    answer = handle(
        server,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )
    assert answer is not None
    return answer


def test_execute_review_and_batch_advertise_optional_system_prompt_text() -> None:
    for name in ("nspawn_run_start", "nspawn_review_start", "nspawn_batch_start"):
        schema = _contract(name)
        declared = schema["properties"]["system_prompt"]
        assert declared["type"] == "string"
        assert declared["minLength"] == 1
        assert declared["maxLength"] == MAX_DURABLE_ROW_BYTES
        assert "UTF-8 bytes" in declared["description"]
        assert "text" in declared["description"]
        assert "path" in declared["description"]
        assert "system_prompt" not in schema["required"]


def test_review_start_carries_the_exact_system_prompt_to_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    review = FakeSupervisor(review_result=RunAccepted(run_id="review-1"))
    answer = _request(
        McpServer(supervisor=review),
        "nspawn_review_start",
        {"branch": "lane/x", "cwd": "/lane", "system_prompt": SYSTEM_PROMPT},
    )
    assert "error" not in answer
    assert review.calls[0][1]["system_prompt"] == SYSTEM_PROMPT

    admitted: list[dict[str, object]] = []

    def _capture(
        self: Supervisor, prompt: str, cwd: str, **kwargs: object
    ) -> RunAccepted:
        admitted.append(kwargs)
        return RunAccepted(run_id="review-2")

    monkeypatch.setattr(Supervisor, "_admit", _capture)
    Supervisor.review(
        Supervisor.__new__(Supervisor), "lane/x", "/lane", system_prompt=SYSTEM_PROMPT
    )
    text = admitted[0]["system_prompt"]
    assert isinstance(text, str)
    assert (
        hashlib.sha256(text.encode()).digest()
        == hashlib.sha256(SYSTEM_PROMPT.encode()).digest()
    )


@pytest.fixture
def plugin_roster(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A roster whose plugin root is a temp tree with one agent file."""
    root = tmp_path / "plugin"
    (root / "agents").mkdir(parents=True)
    (root / "agents" / "python-reviewer.md").write_text(
        "---\nname: python-reviewer\ntools: Read\n---\n\nYou review Python.\n",
        encoding="utf-8",
    )
    roster = tmp_path / "spawn.toml"
    roster.write_text(f'[defaults]\nplugins = ["{root}"]\n', encoding="utf-8")
    monkeypatch.setenv("NOTT_NSPAWN_ROSTER", str(roster))
    monkeypatch.setenv("NOTT_NSPAWN_CONFIG", str(tmp_path / "absent-spawn.toml"))
    return root


def test_agent_names_a_plugin_agent_whose_body_is_the_system_prompt(
    plugin_roster: Path,
) -> None:
    fake = FakeSupervisor(
        review_result=RunAccepted(run_id="review-1"),
        execute_result=RunAccepted(run_id="execute-1"),
    )
    server = McpServer(supervisor=fake)
    review = {"branch": "lane/x", "cwd": "/lane", "agent": "nott:python-reviewer"}
    assert "error" not in _request(server, "nspawn_review_start", review)
    execute = {
        "prompt": "work",
        "cwd": "/lane",
        "class": DECLARED_CLASS,
        "read_only": True,
        "agent": "python-reviewer",
    }
    assert "error" not in _request(server, "nspawn_run_start", execute)
    assert [call[1]["system_prompt"] for call in fake.calls] == [
        "You review Python.\n"
    ] * 2

    unknown = _request(server, "nspawn_review_start", {**review, "agent": "nope"})
    assert unknown["error"]["code"] == INVALID_PARAMS
    assert "python-reviewer" in unknown["error"]["message"]
    both = _request(
        server, "nspawn_review_start", {**review, "system_prompt": SYSTEM_PROMPT}
    )
    assert both["error"]["code"] == INVALID_PARAMS
    assert len(fake.calls) == 2


def test_an_agent_link_out_of_the_agents_directory_is_refused(
    plugin_roster: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text("Not an agent of this plugin.\n", encoding="utf-8")
    (plugin_roster / "agents" / "escape.md").symlink_to(outside)
    with pytest.raises(InvalidRunRequest) as caught:
        agent_system_prompt("escape", str(tmp_path))
    assert "escape" in str(caught.value)
    assert str(plugin_roster / "agents") in str(caught.value)


def test_a_dangling_agent_link_is_a_typed_refusal(
    plugin_roster: Path, tmp_path: Path
) -> None:
    (plugin_roster / "agents" / "gone.md").symlink_to(tmp_path / "absent.md")
    with pytest.raises(InvalidRunRequest) as caught:
        agent_system_prompt("gone", str(tmp_path))
    assert "gone" in str(caught.value)
    assert str(plugin_roster / "agents") in str(caught.value)


def test_a_missing_agents_directory_is_named(
    plugin_roster: Path, tmp_path: Path
) -> None:
    for entry in (plugin_roster / "agents").iterdir():
        entry.unlink()
    (plugin_roster / "agents").rmdir()
    with pytest.raises(InvalidRunRequest) as caught:
        agent_system_prompt("python-reviewer", str(tmp_path))
    assert str(plugin_roster / "agents") in str(caught.value)


def test_crlf_frontmatter_is_removed(plugin_roster: Path, tmp_path: Path) -> None:
    (plugin_roster / "agents" / "windows.md").write_bytes(
        b"---\r\nname: windows\r\ntools: Read\r\n---\r\n\r\nYou review.\r\n"
    )
    assert agent_system_prompt("windows", str(tmp_path)) == "You review.\n"


@pytest.mark.parametrize(
    "head", ["ref: refs/heads/lane\n--upload-pack=touch x", "ref: refs/heads/-x"]
)
def test_a_crafted_head_is_not_a_review_branch(tmp_path: Path, head: str) -> None:
    import subprocess

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".git" / "HEAD").write_text(head + "\n", encoding="utf-8")
    with pytest.raises(InvalidRunRequest, match="give branch"):
        review_branch(str(tmp_path))


def test_mcp_routes_the_exact_text_to_execute_and_every_batch_child() -> None:
    execute = FakeSupervisor(execute_result=RunAccepted(run_id="execute-1"))
    answer = _request(
        McpServer(supervisor=execute),
        "nspawn_run_start",
        {
            "prompt": "work",
            "cwd": "/lane",
            # The measured text is the system instruction, not the class.
            "class": DECLARED_CLASS,
            "system_prompt": SYSTEM_PROMPT,
        },
    )
    assert "error" not in answer
    assert execute.calls[0][1]["system_prompt"] == SYSTEM_PROMPT

    batch = FakeSupervisor(execute_result=RunAccepted(run_id="batch-1"))
    answer = _request(
        McpServer(supervisor=batch),
        "nspawn_batch_start",
        {
            "prompt": "compare",
            "cwd": "/lane",
            "providers": ["x"],
            "system_prompt": SYSTEM_PROMPT,
        },
    )
    assert "error" not in answer
    assert batch.calls[0][1]["system_prompt"] == SYSTEM_PROMPT


@pytest.mark.parametrize("name", ["nspawn_run_start", "nspawn_batch_start"])
@pytest.mark.parametrize(
    "invalid",
    [
        pytest.param(" \t\n", id="whitespace"),
        pytest.param("x" * (MAX_DURABLE_ROW_BYTES + 1), id="ascii-byte-limit"),
        pytest.param("é" * (MAX_DURABLE_ROW_BYTES // 2 + 1), id="multibyte-byte-limit"),
        pytest.param("\ud800", id="surrogate"),
        pytest.param("\x00", id="postgres-null-byte"),
    ],
)
def test_mcp_refuses_invalid_system_prompt_before_dispatch(
    name: str, invalid: str
) -> None:
    supervisor = FakeSupervisor(execute_result=RunAccepted(run_id="must-not-run"))
    arguments: dict[str, Any] = {
        "prompt": "work",
        "cwd": "/lane",
        "system_prompt": invalid,
    }
    if name == "nspawn_batch_start":
        arguments["providers"] = ["x"]

    server = McpServer(supervisor=supervisor)
    answer = _request(server, name, arguments)

    assert answer["error"]["code"] == INVALID_PARAMS
    assert supervisor.calls == []
    valid = {**arguments, "system_prompt": SYSTEM_PROMPT}
    if name == "nspawn_run_start":
        valid["class"] = DECLARED_CLASS
    accepted = _request(server, name, valid)
    assert "error" not in accepted
    assert supervisor.calls[-1][1]["system_prompt"] == SYSTEM_PROMPT


def test_combine_system_prompt_file_refuses_invalid_roster_args(tmp_path: Path) -> None:
    from nspawn.supervisor import SupervisorError, _combine_system_prompt_file

    dest = tmp_path / "dest.txt"
    dest.write_text("dest")
    flag = "--append-system-prompt-file"

    with pytest.raises(SupervisorError, match="separately"):
        _combine_system_prompt_file(str(tmp_path), dest, [f"{flag}=/path"])

    with pytest.raises(SupervisorError, match="more than once"):
        _combine_system_prompt_file(str(tmp_path), dest, [flag, "/a", flag, "/b"])

    with pytest.raises(SupervisorError, match="without a path"):
        _combine_system_prompt_file(str(tmp_path), dest, [flag])

    with pytest.raises(SupervisorError, match="without a path"):
        _combine_system_prompt_file(str(tmp_path), dest, [flag, "--flag"])

    with pytest.raises(SupervisorError, match="without a path"):
        _combine_system_prompt_file(
            str(tmp_path), dest, ["--model", "opus", flag, "--next-flag"]
        )

    rel_source = tmp_path / "rel.txt"
    rel_source.write_text("relative content")
    args = _combine_system_prompt_file(str(tmp_path), dest, [flag, "rel.txt"])
    assert args == []
    assert b"relative content" in dest.read_bytes()


def test_combine_system_prompt_file_duplicate_flag(tmp_path: Path) -> None:
    from nspawn.supervisor import SupervisorError, _combine_system_prompt_file

    dest = tmp_path / "dest.txt"
    dest.write_text("dest")
    flag = "--append-system-prompt-file"

    with pytest.raises(SupervisorError, match="more than once"):
        _combine_system_prompt_file(str(tmp_path), dest, [flag, "a.txt", flag, "b.txt"])


def test_combine_system_prompt_file_absolute_source(tmp_path: Path) -> None:
    from nspawn.supervisor import _combine_system_prompt_file

    dest = tmp_path / "dest.txt"
    dest.write_text("dest")
    abs_source = tmp_path / "abs.txt"
    abs_source.write_text("absolute content")
    args = _combine_system_prompt_file(
        str(tmp_path), dest, ["--append-system-prompt-file", str(abs_source)]
    )
    assert args == []
    assert b"absolute content" in dest.read_bytes()


def test_combine_system_prompt_file_keeps_one_global_law_across_retries(
    tmp_path: Path,
) -> None:
    from nspawn.supervisor import _combine_system_prompt_file

    destination = tmp_path / "system-prompt"
    destination.write_bytes(b"dispatch instructions")
    source = tmp_path / "NOTT.md"
    source.write_bytes(b"# **NOTT** System Prompt\nowner law")
    declared = ["--append-system-prompt-file", str(source)]

    assert _combine_system_prompt_file(str(tmp_path), destination, declared) == []
    first = destination.read_bytes()
    assert first == b"dispatch instructions\n\n# **NOTT** System Prompt\nowner law"

    assert _combine_system_prompt_file(str(tmp_path), destination, declared) == []
    assert destination.read_bytes() == first


def test_combine_system_prompt_file_keeps_trailing_args_on_first_try_and_retry(
    tmp_path: Path,
) -> None:
    from nspawn.supervisor import _combine_system_prompt_file

    destination = tmp_path / "system-prompt"
    destination.write_bytes(b"dispatch")
    source = tmp_path / "NOTT.md"
    source.write_bytes(b"law")
    declared = ["--append-system-prompt-file", str(source), "--model", "opus"]

    assert _combine_system_prompt_file(str(tmp_path), destination, declared) == [
        "--model",
        "opus",
    ]
    first = destination.read_bytes()
    assert _combine_system_prompt_file(str(tmp_path), destination, declared) == [
        "--model",
        "opus",
    ]
    assert destination.read_bytes() == first


def test_combine_system_prompt_file_recognizes_law_as_entire_existing_file(
    tmp_path: Path,
) -> None:
    from nspawn.supervisor import _combine_system_prompt_file

    destination = tmp_path / "system-prompt"
    destination.write_bytes(b"\n\nlaw")
    source = tmp_path / "NOTT.md"
    source.write_bytes(b"law")

    assert (
        _combine_system_prompt_file(
            str(tmp_path), destination, ["--append-system-prompt-file", str(source)]
        )
        == []
    )
    assert destination.read_bytes() == b"\n\nlaw"


def test_combine_system_prompt_file_refuses_destination_symlink(tmp_path: Path) -> None:
    from nspawn.supervisor import _combine_system_prompt_file

    victim = tmp_path / "victim"
    victim.write_bytes(b"untouched")
    destination = tmp_path / "system-prompt"
    destination.symlink_to(victim)
    source = tmp_path / "NOTT.md"
    source.write_bytes(b"law")

    with pytest.raises(OSError):
        _combine_system_prompt_file(
            str(tmp_path), destination, ["--append-system-prompt-file", str(source)]
        )
    assert victim.read_bytes() == b"untouched"


def test_combine_system_prompt_file_no_flag(tmp_path: Path) -> None:
    from nspawn.supervisor import _combine_system_prompt_file

    dest = tmp_path / "dest.txt"
    dest.write_text("dest")
    declared = ["--verbose", "--setting-sources", "user"]
    args = _combine_system_prompt_file(str(tmp_path), dest, declared)
    assert args == declared
    assert dest.read_bytes() == b"dest"
