"""F7: the MCP review path must retain the loader for deferred context tools.

The recording child models deferred-schema loading, not provider inference.
Admission/persistence and vendor installation are fakes; the MCP dispatch,
review policy, plugin projection, argv/env builders and classifier are real.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from class_dispatch import (
    system1_never_reaches_the_network,  # noqa: F401
)
from mcp_fakes import FakeSupervisor, handle

from nspawn import child, claude_child, supervisor
from nspawn.mcp import McpServer
from nspawn.owner_roster import Defaults, Provider, Roster
from nspawn.picker import PickerResult
from nspawn.process_identity import ProcessIdentity
from nspawn.rows import RunPhase
from nspawn.supervisor import (
    RunAccepted,
    RunResult,
    Supervisor,
    create_run_directory,
)


@pytest.mark.parametrize(
    ("verb", "supply_loader"),
    [
        pytest.param("nspawn_review_start", True, id="caged-loader-flip"),
        pytest.param("nspawn_review_start", False, id="caged-regression"),
    ],
)
def test_mcp_child_can_load_deferred_context_and_reply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str, supply_loader: bool
) -> None:
    if supply_loader:
        monkeypatch.setattr(
            child,
            "READ_ONLY_TOOLS",
            tuple(dict.fromkeys((*child.READ_ONLY_TOOLS, "ToolSearch"))),
        )
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    # The shipped plugin also declares nmemory over http; the cage keeps stdio only.
    (plugin / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "nmemory": {"command": "fake-memory", "args": []},
                    "nmemory-http": {
                        "type": "http",
                        "url": "https://host.example/mcp",
                        "headers": {"Authorization": "Bearer ${NOTT_NMEMORY_API_KEY}"},
                    },
                }
            }
        )
    )
    provider = Provider(
        name="m1",
        bin=Path("/fake/claude"),
        model="gemma4:e4b",
        models=["gemma4:e4b"],
        auth_env="ANTHROPIC_AUTH_TOKEN",
        env={"ANTHROPIC_AUTH_TOKEN": "synthetic-f7-credential"},
    )
    roster = Roster(defaults=Defaults(plugin_root=plugin), providers=[provider])
    config = tmp_path / "spawn.toml"
    config.write_text(
        '[bins]\nclaude = "/fake/claude"\n'
        '[[provider]]\nname = "m1"\nkind = "claude"\n'
        'model = "gemma4:e4b"\nmodels = ["gemma4:e4b"]\n'
    )
    picked = PickerResult(None, provider, None, "gemma4:e4b", None, None)
    # No owner home, inherited credential, real MCP server or provider is used.
    monkeypatch.setattr(
        child.os,
        "environ",
        {
            "HOME": str(tmp_path),
            "PATH": "/bin",
            "ANTHROPIC_AUTH_TOKEN": "synthetic-f7-credential",
            # The replaced environ drops the conftest fence: this test's own
            # config keeps the operator's ~/.nott/spawn.toml out of this dispatch.
            "NOTT_NSPAWN_CONFIG": str(config),
        },
    )
    monkeypatch.setattr(
        claude_child,
        "inspect_claude_installation",
        lambda binary, probe: SimpleNamespace(binary=binary),
    )
    monkeypatch.setattr(supervisor, "_validate_review_worktree_branch", lambda *a: None)
    monkeypatch.setattr(FakeSupervisor, "review", Supervisor.review)
    monkeypatch.setattr(FakeSupervisor, "execute", Supervisor.execute)
    fake = FakeSupervisor()
    fake._lock = threading.Condition()
    fake._children = {}
    fake._registry = Mock()
    fake._registry.inference_profile.return_value = None
    fake._registry.get_run.return_value = SimpleNamespace(phase=RunPhase.PENDING)
    monkeypatch.setattr(fake, "_update_phase", lambda *a, **k: None)
    received = []

    written: list[bytes] = []

    def record_child(argv, cwd, env, run_dir, needles, **spawn_kwargs):
        assert spawn_kwargs == {"stdin_pipe": True}
        received.append((list(argv), cwd, dict(env)))
        tools = (
            argv[argv.index("--tools") + 1].split(",") if "--tools" in argv else None
        )
        loader_available = tools is None or "ToolSearch" in tools
        if "--restricted" in argv:
            assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
            assert argv[argv.index("--allowedTools") + 1].split(",") == [
                "mcp__plugin_nott_nmemory__nmemory_context_show"
            ]
            assert {"Write", "Edit", "Task"}.isdisjoint(tools)
            projected = Path(argv[argv.index("--plugin-dir") + 1]) / ".mcp.json"
            servers = json.loads(projected.read_text())["mcpServers"]
            assert set(servers) == {"nmemory"}
            assert servers["nmemory"]["args"] == ["--read-only"]
        frame = {
            "type": "result",
            "is_error": not loader_available,
            "terminal_reason": "completed" if loader_available else "failed",
            "result": "pong" if loader_available else "ToolSearch unavailable",
        }
        (run_dir / "stdout").write_text(json.dumps(frame) + "\n")
        (run_dir / "stderr").write_text("")
        return SimpleNamespace(
            identity=ProcessIdentity(12345, "fake-boot", 1),
            wait=lambda: 0 if loader_available else 1,
            write_stdin=lambda payload, timeout: written.append(payload),
            close_stdin=lambda: None,
        )

    monkeypatch.setattr(child, "spawn_child", record_child)

    def admit(prompt, cwd, **kwargs):
        root_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            run_dir = create_run_directory(tmp_path, "f7-run", dir_fd=root_fd)
        finally:
            os.close(root_fd)
        result = fake._spawn_and_wait(
            "f7-run",
            cwd,
            run_dir,
            prompt,
            roster,
            picked,
            "f7-thread",
            False,
            kwargs["read_only"],
        )
        fake.result_result = RunResult("f7-run", result.final_text)
        return RunAccepted("f7-run")

    monkeypatch.setattr(fake, "_admit", admit)
    server = McpServer(supervisor=fake)
    # The writable uncaged control retired with the body-accepted mode: a
    # Claude route has no writable dispatch left to compare against.
    args = {
        "prompt": "Reply with pong after loading nmemory_context_show.",
        "cwd": str(tmp_path),
        "branch": "lane/f7",
    }
    answer = handle(
        server,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": verb, "arguments": args},
        },
    )
    assert answer is not None
    assert "result" in answer, answer
    result = handle(
        server,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "nspawn_result_show", "arguments": {"run_id": "f7-run"}},
        },
    )
    argv, cwd, env = received[0]
    assert "--" not in argv
    assert written == [args["prompt"].encode()]
    assert cwd == str(tmp_path)
    assert env["HOME"] == str(tmp_path)
    assert env["ANTHROPIC_MODEL"] == "gemma4:e4b"
    assert result is not None and "result" in result, result
    payload = json.loads(result["result"]["content"][0]["text"])
    assert payload["final_text"] == "pong", payload
