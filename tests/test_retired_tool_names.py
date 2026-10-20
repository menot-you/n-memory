"""The retired nmemory tool names answer "unknown tool", like any name never served.

This file spells retired names only to assert they are refused; it is declared in
`[exception.historical_read]` for that reason, and writes none of them.
"""

from __future__ import annotations

import json
from pathlib import Path

from storefile import default_store_path
from test_tools import StdioClient


def test_the_served_name_is_nmemory_capsule_purge_and_the_old_names_are_refused() -> (
    None
):
    """`nmemory_capsule_purge` is the ONE served name; neither old name dispatches.

    The owner renamed forget to purge (2026-08-31) and kept the old name as an
    unlisted alias for a caller outside this repo. The canonical rename
    (`<exe>_<noun>_<verb>`) retires aliases: a retired name answers "unknown tool"
    like any name that was never served, and that outside caller is updated
    instead of being carried by the server.

    What DOES NOT change, intentionally: modes remain `purged|redacted` (vocabulary
    already persisted on each tombstone) and the audit event remains
    `audit_actions.FORGET` (nmemory_capsule_show exposes it in last_mutation.event,
    and the Rust oracle emits the same).
    """
    client = StdioClient(
        env={
            "NOTT_DATABASE_DIR": str(Path(default_store_path()).parent),
            "NOTT_NMEMORY_HMAC_KEY": (
                "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
            ),
        }
    )
    try:
        client.send("initialize", {"protocolVersion": "2024-11-05"})
        names = {t["name"] for t in client.send("tools/list")["result"]["tools"]}
        assert "nmemory_capsule_purge" in names, (
            "tools/list does not serve nmemory_capsule_purge"
        )
        retired = ("memory_forget", "memory_purge")
        assert not names & set(retired), f"a retired name is still LISTED: {names}"

        ingested = client.call_tool(
            "nmemory_capsule_ingest",
            {
                "content": "capsula para purge",
                "kind": "task",
                "source": "session:test",
                "anchor": "test_tools.py:purge",
            },
        )
        cid = json.loads(ingested["result"]["content"][0]["text"])["outcomes"][0]["id"]
        for old in retired:
            refused = client.call_tool(
                old, {"id": cid, "mode": "purged", "reason": "teste via nome antigo"}
            )
            assert refused["error"]["code"] == -32602, refused
            assert f'unknown tool "{old}"' in refused["error"]["message"]
        out = client.call_tool(
            "nmemory_capsule_purge",
            {"id": cid, "mode": "purged", "reason": "teste via purge"},
        )
        body = json.loads(out["result"]["content"][0]["text"])
        assert body["outcome"] == "tombstoned", f"purge nao criou lapide: {body}"
        assert body["mode"] == "purged"
    finally:
        client.close()
