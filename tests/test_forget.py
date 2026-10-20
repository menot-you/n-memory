"""Tests for nmemory_capsule_purge reason sanitization and batch execution (M4 & M6)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from storefile import sample_capsule_id

from nmemory.capsule import (
    AuthorityClass,
    Capsule,
    Freshness,
    Provenance,
    Scope,
    sha256_hex,
)
from nmemory.server import MemoryServer
from nmemory.store import Store
from nmemory.tools import purge as purge_tool

NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)


def cap_at(content: str, project: str = "nott") -> Capsule:
    return Capsule(
        content=content,
        provenance=Provenance(
            source="test",
            anchor="test:1",
            source_hash=sha256_hex(content.encode("utf-8")),
        ),
        confidence=0.9,
        freshness=Freshness(valid_from=NOW),
        scope=Scope(project_id=project),
        authority_class=AuthorityClass.USER_STATED,
        instruction_taint=False,
    )


# The key is EXPLICIT because nmemory_capsule_purge refuses to run without an HMAC
# source, and the suite must not inherit the owner's ambient NOTT_NMEMORY_HMAC_KEY:
# the body gate runs stripped of it (measured 2026-09-07: the four purge-path
# tests below redden under `-n 1` exactly as under `-n 5` without a key).
_HMAC_KEY = b"k" * 32


def test_forget_reason_sanitization() -> None:
    store = Store.open_disposable_test_store()
    server = MemoryServer(store=store, hmac_env_key=_HMAC_KEY)
    cid, _ = store.append(cap_at("Confidential project details"), NOW)

    dirty_reason = (
        "  Reason line 1\nReason line 2\r\nReason \t line 3\x00 with \x1b[31m escape  "
    )
    res = purge_tool.handle(
        server.handler_state(),
        {"id": cid, "mode": "purged", "reason": dirty_reason},
        None,
        NOW,
        "test-actor",
    )
    assert "error" not in res, res
    payload = json.loads(res["content"][0]["text"])
    assert payload["outcome"] == "tombstoned"

    # Verify sanitized reason in tombstone record
    tomb = store.capsules.get_tombstone(cid)
    assert tomb is not None
    assert "\n" not in tomb.reason
    assert "\r" not in tomb.reason
    assert "\t" not in tomb.reason
    assert "\x00" not in tomb.reason
    assert tomb.reason == "Reason line 1 Reason line 2 Reason line 3 with [31m escape"

    # Verify empty reason is rejected
    cid2, _ = store.append(cap_at("Second capsule"), NOW)
    empty_res = purge_tool.handle(
        server.handler_state(),
        {"id": cid2, "mode": "purged", "reason": "   \n\t  "},
        None,
        NOW,
        "test-actor",
    )
    assert "error" in empty_res or empty_res.get("isError") is True


def test_forget_batch_execution() -> None:
    store = Store.open_disposable_test_store()
    server = MemoryServer(store=store, hmac_env_key=_HMAC_KEY)
    cid1, _ = store.append(cap_at("First capsule to purge"), NOW)
    cid2, _ = store.append(cap_at("Second capsule to purge"), NOW)

    batch_payload = {
        "items": [
            {"id": cid1, "mode": "purged", "reason": "batch item 1"},
            {"id": cid2, "mode": "redacted", "reason": "batch item 2"},
            {
                "id": sample_capsule_id(9999),
                "mode": "purged",
                "reason": "missing item",
            },
        ]
    }
    res = purge_tool.handle(
        server.handler_state(), batch_payload, None, NOW, "batch-actor"
    )
    assert "error" not in res, res
    payload = json.loads(res["content"][0]["text"])
    assert payload["label"] == "ADVISORY_NOT_AUTHORITY"
    assert "results" in payload or "outcomes" in payload
    results = payload.get("results") or payload.get("outcomes")
    assert len(results) == 3

    assert results[0]["id"] == cid1
    assert results[0]["status"] == "tombstoned"

    assert results[1]["id"] == cid2
    assert results[1]["status"] == "tombstoned"

    assert results[2]["id"] == sample_capsule_id(9999)
    assert results[2]["status"] == "not_found"
