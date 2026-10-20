"""Pins the dicts DoD records emit, so the serializer can be swapped without drift."""

from __future__ import annotations

from datetime import UTC, datetime

from nmemory.dod import DodContradiction, DodEvent, DodState


def test_dod_contradiction_to_dict_is_pinned() -> None:
    assert DodContradiction(dod_id="dé-1", target_id="t\n2").to_dict() == {
        "dod_id": "dé-1",
        "target_id": "t\n2",
    }


def test_dod_state_to_dict_is_pinned() -> None:
    at = datetime(2026, 9, 3, 12, 0, 0, 123456, tzinfo=UTC)
    ev = DodEvent(axis="status", value="done", actor="a", reason="r", at=at)
    out = DodState(status="done", verdict="pass", history=[ev]).to_dict()
    assert out == {
        "status": "done",
        "verdict": "pass",
        "history": [ev.to_dict()],
    }
    assert isinstance(out["history"][0]["at"], str)
