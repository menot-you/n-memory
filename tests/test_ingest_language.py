"""The capture door speaks English: capsule content whose prose is not English is refused.

The refusal lives in `nmemory.tools.ingest`, like the proof gate in
`test_capture_gate.py`, and NOT in `ingest()`: `nmemory_capsule_import` carries external
files that may legitimately be in another language.

`nmemory_session_finish` never refuses a handoff: it is called when a session goes
idle or ends, and nobody is left to rewrite it. It stores the handoff, finishes the
session and returns a warning.

The non-English vectors live in `fixtures/language_vectors.jsonl`, which Rail L pins
because it is non-English by construction.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from nmemory.server import MemoryServer
from nmemory.store import Store

VECTORS = {
    v["id"]: v
    for v in map(
        json.loads,
        (Path(__file__).parent / "fixtures" / "language_vectors.jsonl")
        .read_text(encoding="utf-8")
        .splitlines(),
    )
}
ANCHOR = "apps/memory/core/nmemory/server.py:2050"


def _server() -> MemoryServer:
    return MemoryServer(store=Store.open_disposable_test_store(), project_id="nmemory")


def _body(resp: dict[str, Any]) -> dict[str, Any]:
    assert "error" not in resp, f"the call should not fail as a whole: {resp}"
    body: dict[str, Any] = json.loads(resp["result"]["content"][0]["text"])
    return body


def _item(text: str, **extra: Any) -> dict[str, Any]:
    return {
        "content": text,
        "source": "session:2026-09-25",
        "anchor": ANCHOR,
        "kind": "fact",
        "project_id": "nmemory",
        **extra,
    }


@pytest.mark.parametrize(
    "vid", [k for k, v in VECTORS.items() if v["expect"] in ("captured", "refused")]
)
def test_prose_language_decides_capture(vid: str) -> None:
    vector = VECTORS[vid]
    body = _body(_server().call_tool("nmemory_capsule_ingest", _item(vector["text"])))
    if vector["expect"] == "captured":
        assert body["captured"] == 1, f"{vid}: English content was refused: {body}"
        return
    assert body["rejected"] == 1, f"{vid}: non-English content was captured: {body}"
    msg = body["outcomes"][0]["error"]
    assert msg.startswith("content: ingest rejected: "), msg
    assert "English" in msg and "source_quote" in msg, (
        f"the refusal must teach the fix, not only refuse: {msg}"
    )


def test_owner_words_stay_verbatim_in_source_quote() -> None:
    quote = VECTORS["quote-pt"]["text"]
    body = _body(
        _server().call_tool(
            "nmemory_capsule_ingest",
            _item(
                "The owner approved the English-at-capture plan.",
                kind="decision",
                authority_class="user-stated",
                anchor=quote,
                source_quote=quote,
            ),
        )
    )
    assert body["captured"] == 1, (
        f"a Portuguese quote must not refuse English content: {body}"
    )


def test_batch_refuses_only_the_non_english_row() -> None:
    items = [
        _item(VECTORS["en-plain"]["text"]),
        _item(VECTORS["pt-plain"]["text"]),
        _item(VECTORS["en-long"]["text"]),
    ]
    body = _body(_server().call_tool("nmemory_capsule_ingest", {"items": items}))
    assert [o["status"] for o in body["outcomes"]] == [
        "captured",
        "rejected",
        "captured",
    ], body
    assert body["outcomes"][1]["error"].startswith(
        "items[1].content: ingest rejected: "
    ), body


def test_non_english_handoff_is_stored_finished_and_warned() -> None:
    server = _server()
    started = _body(server.call_tool("nmemory_session_start", {}))
    text = VECTORS["handoff-pt"]["text"]
    finished = server.call_tool(
        "nmemory_session_finish", {"session_id": started["session_id"], "handoff": text}
    )
    body = _body(finished)
    assert not finished["result"].get("isError"), finished
    assert "finished_at" in body, (
        f"the session must finish even with a non-English handoff: {body}"
    )
    assert "English" in body.get("warning", ""), (
        f"the handoff must carry a warning: {body}"
    )
    got = _body(
        server.call_tool("nmemory_capsule_show", {"id": body["handoff_capsule"]})
    )
    assert got["capsule"]["content"] == text, "the handoff must be stored as written"


def test_english_handoff_has_no_warning() -> None:
    server = _server()
    started = _body(server.call_tool("nmemory_session_start", {}))
    body = _body(
        server.call_tool(
            "nmemory_session_finish",
            {
                "session_id": started["session_id"],
                "handoff": VECTORS["en-long"]["text"],
            },
        )
    )
    assert "warning" not in body, body


def test_a_broken_detector_never_costs_the_handoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The warning is computed inside the tool transaction: a detector that raises (a
    broken numpy, an unreadable model) must not roll back the handoff and the finish."""

    def broken(_: str) -> str | None:
        raise ImportError("numpy ABI mismatch")

    monkeypatch.setattr("nmemory.tools.session_finish.foreign_language", broken)
    server = _server()
    started = _body(server.call_tool("nmemory_session_start", {}))
    body = _body(
        server.call_tool(
            "nmemory_session_finish",
            {
                "session_id": started["session_id"],
                "handoff": VECTORS["handoff-pt"]["text"],
            },
        )
    )
    assert "finished_at" in body, body
    assert "not checked" in body.get("warning", ""), body
