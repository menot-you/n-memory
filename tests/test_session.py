"Tests for session bracket lifecycle and isolation."

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from nmemory.embedding import EmbeddingClient
from nmemory.embedding_index import EmbeddingWorker
from nmemory.server import MemoryServer
from nmemory.space import DIMENSION
from nmemory.store import Store
from nmemory.tools import session_finish as session_finish_tool
from nmemory.tools import session_start as session_start_tool

NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)


def _ok(server: MemoryServer, name: str, args: dict[str, Any]) -> dict[str, Any]:
    reply = server.call_tool(name, args)
    assert "result" in reply and not reply["result"].get("isError"), reply
    return json.loads(reply["result"]["content"][0]["text"])


@pytest.mark.parametrize("mode", ["purged", "redacted"])
def test_a_purged_handoff_does_not_prevent_session_completion(mode: str) -> None:
    store = Store.open_disposable_test_store()
    server = MemoryServer(store=store, hmac_env_key=b"fixture" * 8)
    handoff = "The cobalt handoff contains private session details."
    first = _ok(server, "nmemory_session_start", {})["session_id"]
    captured = _ok(
        server, "nmemory_session_finish", {"session_id": first, "handoff": handoff}
    )["handoff_capsule"]
    _ok(
        server,
        "nmemory_capsule_purge",
        {"id": captured, "mode": mode, "reason": "Remove this private handoff."},
    )
    tombstone = store.capsules.get_tombstone(captured)
    audit_seq = store.audit.list_audit()[0].seq
    second = _ok(server, "nmemory_session_start", {})["session_id"]
    summary = "The second session completed its verification."
    finished = _ok(
        server,
        "nmemory_session_finish",
        {"session_id": second, "summary": summary, "handoff": handoff},
    )

    session = store.sessions.get_session(second)
    assert session is not None
    assert session.finished_at is not None
    assert session.summary == summary
    assert finished["finished_at"] == session.to_dict()["finished_at"]
    assert finished["summary"] == summary
    assert "handoff_capsule" not in finished
    assert "handoff_handle" not in finished
    assert "purged" in finished.get("warning", "")
    assert handoff not in json.dumps(finished)
    assert store.capsules.get_tombstone(captured) == tombstone
    assert store.epistemics.get_source_quote(captured) is None
    assert store.capsules.handle_slug(captured) is None
    events = [event for event in store.audit.list_audit() if event.seq > audit_seq]
    assert not any(event.reason == "handoff capture" for event in events)
    assert any(event.subject == second and event.reason == summary for event in events)

    sent: list[str] = []

    def record(text: str) -> list[float]:
        sent.append(text)
        return [1.0] + [0.0] * (DIMENSION - 1)

    assert EmbeddingWorker(store, EmbeddingClient(custom_hook=record)).cycle() == 0
    assert sent == []
    with store.db.connection() as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM capsules WHERE canonical_json IS NOT NULL"
            ).fetchone()[0]
            == 0
        )
        assert conn.execute("SELECT count(*) FROM embedding_jobs").fetchone()[0] == 0
        assert (
            conn.execute("SELECT count(*) FROM capsule_embeddings").fetchone()[0] == 0
        )


def test_handoff_capsule_uses_the_configured_project() -> None:
    store = Store.open_disposable_test_store()
    server = MemoryServer(store=store, project_id="native-contract-project")
    started = server.call_tool("nmemory_session_start", {})
    session = json.loads(started["result"]["content"][0]["text"])["session_id"]
    finished = server.call_tool(
        "nmemory_session_finish",
        {"session_id": session, "handoff": "Native contract checkpoint."},
    )
    assert not finished["result"].get("isError"), finished
    captured = json.loads(finished["result"]["content"][0]["text"])
    readback = server.call_tool(
        "nmemory_capsule_show", {"id": captured["handoff_capsule"]}
    )
    capsule = json.loads(readback["result"]["content"][0]["text"])["capsule"]
    assert capsule["scope"]["project_id"] == "native-contract-project"
    assert capsule["content"] == "Native contract checkpoint."
    assert captured["session_id"] == session
    assert "finished_at" in captured


def test_session_bracket_lifecycle() -> None:
    store = Store.open_disposable_test_store()
    server = MemoryServer(store=store)

    # 1. Start a session bracket
    start_res = session_start_tool.handle(
        server.handler_state(), {"reason": "test session"}, None, NOW, "test-actor"
    )
    assert "error" not in start_res, start_res
    start_payload = json.loads(start_res["content"][0]["text"])
    session_id = start_payload["session_id"]
    assert session_id.startswith("sess-")

    # 2. Finish the session bracket
    finish_res = session_finish_tool.handle(
        server.handler_state(),
        {"session_id": session_id, "summary": "completed test session"},
        None,
        NOW,
        "test-actor",
    )
    assert "error" not in finish_res, finish_res
    finish_payload = json.loads(finish_res["content"][0]["text"])
    assert finish_payload["session_id"] == session_id
    assert "finished_at" in finish_payload

    # 3. Repeated finish on closed session returns typed error
    repeat_res = session_finish_tool.handle(
        server.handler_state(),
        {"session_id": session_id, "summary": "repeat finish"},
        None,
        NOW,
        "test-actor",
    )
    assert "error" in repeat_res or repeat_res.get("isError") is True
