"""The stdio server names the store file it opens on stderr, once, and nothing else does."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from child_env import child_env
from stdio_peer import INITIALIZED, complete
from storefile import default_store_path

from nmemory import cli
from nmemory.store import Store


@pytest.mark.parametrize(
    ("arguments", "notice_count"),
    [(["mcp", "serve", "--project", "x"], 1)],
    ids=["serve"],
)
def test_stdio_server_names_the_store_file_on_stderr(
    arguments: list[str], notice_count: int
) -> None:
    store = default_store_path()
    env = child_env()
    env["NOTT_NMEMORY_CONFIG"] = "/nonexistent/memory.toml"

    proc = subprocess.Popen(
        [sys.executable, "-m", "nmemory", *arguments],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        init_msg = json.dumps(complete({"id": 1, "method": "initialize"}))
        assert proc.stdin is not None
        proc.stdin.write(init_msg + "\n")
        proc.stdin.flush()

        assert proc.stdout is not None
        resp_line = proc.stdout.readline()
        assert resp_line, "server produced no response on stdout"
        resp = json.loads(resp_line)
        assert "result" in resp, resp
        proc.stdin.write(json.dumps(INITIALIZED) + "\n")
        call_msg = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "nmemory_context_show",
                    "arguments": {"query": "store source", "sources": ["memory"]},
                },
            }
        )
        proc.stdin.write(call_msg + "\n")
        proc.stdin.flush()
        called = json.loads(proc.stdout.readline())
        assert "result" in called, called
        assert not called["result"].get("isError"), called
    finally:
        if proc.stdin:
            proc.stdin.close()
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)

    assert proc.stderr is not None
    stderr_out = proc.stderr.read()
    lines = [line.strip() for line in stderr_out.splitlines() if line.strip()]
    notices = [line for line in lines if line.startswith("nmemory: store ")]
    assert notices == [f"nmemory: store {store}"] * notice_count
    if notice_count:
        assert lines[0] == notices[0]


def test_cli_subcommand_prints_no_store_notice() -> None:
    default_store_path()
    proc = subprocess.run(
        [sys.executable, "-m", "nmemory", "digest", "show"],
        capture_output=True,
        text=True,
        env=child_env(dict(os.environ, NOTT_NMEMORY_CONFIG="/nonexistent/memory.toml")),
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "nmemory: store " not in proc.stderr


def test_bare_embeddings_is_refused_and_never_serves(tmp_path: Path) -> None:
    """Bare `embeddings` used to fall through to the stdio server.

    It is now an unknown command: exit 2, nothing on stdout, and no store notice.
    """
    cfg = tmp_path / "memory.toml"
    cfg.write_text("")
    env = dict(os.environ)
    env["NOTT_NMEMORY_CONFIG"] = str(cfg)

    proc = subprocess.run(
        [sys.executable, "-m", "nmemory", "embeddings"],
        input='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n',
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert proc.returncode == 2
    assert proc.stdout == ""
    assert "unknown command: nmemory embeddings" in proc.stderr
    assert "nmemory: store " not in proc.stderr


def test_the_notice_names_the_file_that_is_opened(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = default_store_path()
    opened: list[Path] = []
    real_open = Store.open.__func__  # type: ignore[attr-defined]

    def spy_open(cls: type[Store], path: Any = None, **kwargs: Any) -> Store:
        opened.append(Path(path))
        return real_open(cls, path, **kwargs)  # type: ignore[no-any-return]

    monkeypatch.setattr(Store, "open", classmethod(spy_open))
    monkeypatch.setattr(sys, "argv", ["nmemory", "mcp", "serve", "--read-only"])
    # The transport is the SDK's; what is measured is the store the first call opens.
    monkeypatch.setattr(
        "nmemory.server.run_stdio_server",
        lambda server: server.call_tool(
            "nmemory_context_show", {"query": "source", "sources": ["memory"]}
        ),
    )

    err, out = StringIO(), StringIO()
    with redirect_stderr(err), redirect_stdout(out):
        cli.main()

    assert f"nmemory: store {store}" in err.getvalue()
    assert opened == [store]
