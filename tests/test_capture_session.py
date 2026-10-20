"""A node's nMEMORY server files every capture under the node, whatever the vendor says.

The node host starts each canvas node's nmemory server with
`NOTT_NMEMORY_SESSION_ID=<node id>`. The vendor session changes when a node
switches vendor or resumes, so a capture filed under the vendor's
`_meta.sessionId` cannot name the node that wrote it. The test drives a real
`nmemory mcp serve` over stdio, as the host does.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from stdio_peer import exchange
from storefile import default_store_path
from workspace_root import WORKSPACE_ROOT

from nmemory.store import Store

_MEMBER_ROOT = str(WORKSPACE_ROOT / "mcps" / "nmemory")


def _filed_session(tmp_path: Path, extra: dict[str, str]) -> str:
    path = default_store_path()
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "PYTHONPATH": _MEMBER_ROOT,
        "NOTT_DATABASE_DIR": os.environ["NOTT_DATABASE_DIR"],
        **extra,
    }
    call = {
        "name": "nmemory_capsule_ingest",
        "arguments": {
            "content": "A node wrote this task about the board.",
            "kind": "task",
            "source": "test_capture_session",
            "anchor": "1",
        },
        "_meta": {"sessionId": "vendor-y"},
    }
    answers, errors, code = exchange(
        [sys.executable, "-m", "nmemory", "mcp", "serve"],
        [
            {"id": 1, "method": "initialize", "params": {}},
            {"id": 2, "method": "tools/call", "params": call},
        ],
        deadline_s=120,
        env=env,
        cwd=tmp_path,
    )
    assert len(answers) == 2, (code, errors)
    outcome = json.loads(answers[1]["result"]["content"][0]["text"])["outcomes"][0]
    assert outcome["status"] == "captured", outcome
    context = Store.open(path).capsules.get_capture_context(outcome["id"])
    assert context is not None
    return context.session_id


def test_the_node_session_wins_over_the_vendor_session(tmp_path: Path) -> None:
    assert _filed_session(tmp_path, {"NOTT_NMEMORY_SESSION_ID": "node-x"}) == "node-x"


def test_without_the_node_session_the_vendor_session_stays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NOTT_NMEMORY_SESSION_ID", raising=False)
    assert _filed_session(tmp_path, {}) == "vendor-y"
