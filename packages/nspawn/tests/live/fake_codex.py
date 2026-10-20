#!/usr/bin/env python3
"""A codex app-server double for the served writer (LD-3).

It writes the way a native-write codex turn does: straight into the turn's
`cwd`, and only when the thread was started with a writable permission profile.
Anything else it is asked, it answers with the smallest well-formed reply the
native path reads.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

#: "write IT_WORKS.md with the text <text>" writes <text>; any other prompt is
#: answered BLOCKED, with nothing edited.
PROMPT = "write IT_WORKS.md with the text "

#: What codex reports for a thread started with a native write profile
#: (tests/test_native_write.py, observed 2026-09-28).
WRITER_SANDBOX = {
    "type": "workspaceWrite",
    "writableRoots": [],
    "networkAccess": False,
    "excludeTmpdirEnvVar": True,
    "excludeSlashTmp": True,
}


def send(value: dict[str, Any]) -> None:
    print(json.dumps(value), flush=True)


def read() -> dict[str, Any]:
    line = sys.stdin.readline()
    if not line:
        raise SystemExit(0)
    frame = json.loads(line)
    assert isinstance(frame, dict)
    return frame


def main() -> None:
    if sys.argv[-3:] == ["mcp", "list", "--json"]:
        print("[]", flush=True)
        return
    thread = "live-writer-thread"
    config: dict[str, Any] = {}
    writer = False
    while True:
        frame = read()
        method = frame.get("method")
        params = frame.get("params") or {}
        if "id" not in frame or method is None:
            continue
        if method == "initialize":
            result: dict[str, Any] = {
                "userAgent": "live-codex-fixture",
                "codexHome": os.environ["CODEX_HOME"],
            }
        elif method == "plugin/installed":
            result = {"marketplaces": [], "marketplaceLoadErrors": []}
        elif method in ("thread/start", "thread/resume"):
            config = params.get("config", {})
            profile = params.get("permissions")
            granted = config.get("permissions", {}).get(profile, {})
            writer = "write" in granted.get("filesystem", {}).values()
            result = {
                "thread": {"id": thread, "ephemeral": params.get("ephemeral", False)},
                "model": params["model"],
                "modelProvider": params.get("modelProvider", "openai"),
                "sandbox": WRITER_SANDBOX if writer else {"type": "readOnly"},
                "activePermissionProfile": {"id": profile, "extends": None}
                if profile
                else None,
            }
        elif method == "mcpServerStatus/list":
            servers = config.get("mcp_servers", {})
            result = {
                "data": [
                    {
                        "name": name,
                        "tools": {tool: {} for tool in entry.get("enabled_tools", [])},
                        "resources": [],
                        "resourceTemplates": [],
                    }
                    for name, entry in servers.items()
                    if entry.get("enabled", True)
                ],
                "nextCursor": None,
            }
        elif method == "turn/start":
            send({"id": frame["id"], "result": {"turn": {"id": "turn"}}})
            prompt = params["input"][0]["text"]
            text = prompt.removeprefix(PROMPT)
            wrote = writer and prompt.startswith(PROMPT)
            if wrote:
                (Path(params["cwd"]) / "IT_WORKS.md").write_text(text)
            send(
                {
                    "method": "item/completed",
                    "params": {
                        "threadId": thread,
                        "turnId": "turn",
                        "item": {
                            "id": "answer",
                            "type": "agentMessage",
                            "phase": "final_answer",
                            "text": "DONE" if wrote else "BLOCKED",
                        },
                    },
                }
            )
            send(
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": thread,
                        "turn": {"id": "turn", "status": "completed", "error": None},
                    },
                }
            )
            continue
        else:
            send(
                {
                    "id": frame["id"],
                    "error": {"code": -32601, "message": f"unsupported: {method}"},
                }
            )
            continue
        send({"id": frame["id"], "result": result})


if __name__ == "__main__":
    main()
