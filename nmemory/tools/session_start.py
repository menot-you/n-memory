"""nmemory_session_start: open a session bracket.

The handler reads no capsule and does not replay the audit chain, so its cost
does not grow with the store. The daily `nmemory quality measure` run checks
the audit chain.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .. import audit_actions
from ..capsule import (
    format_rfc3339,
)
from ..capture import CaptureContext
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
    return verb_result({"session_id": sess_id, "started_at": format_rfc3339(now)})
