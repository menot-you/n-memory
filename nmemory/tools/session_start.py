"""nmemory_session_start: open a session and report the store's health."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .. import audit_actions
from ..capsule import (
    format_rfc3339,
)
from ..capture import CaptureContext
from ..queries.journal import verify_replay
from .common import (
    HandlerState,
    verb_result,
)


def handle(
    state: HandlerState,
    args: dict[str, Any],
    context: CaptureContext | None,
    now: datetime,
    actor: str,
) -> dict[str, Any]:
    sess_id = state.store.sessions.start_session(now=now)
    state.store.audit.append_audit(
        actor, audit_actions.SESSION_START, sess_id, None, now
    )
    if state.capture_only:
        return verb_result({"session_id": sess_id, "started_at": format_rfc3339(now)})

    # Honest health probe
    replay = verify_replay(state.store.db)
    if not replay.chain.is_ok:
        health = "broken_chain"
    elif not state.store.capsules.list():
        health = "empty_store"
    else:
        health = "healthy"

    return verb_result(
        {
            "session_id": sess_id,
            "started_at": format_rfc3339(now),
            "store_health": health,
        }
    )
