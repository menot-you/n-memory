"""nmemory_capsule_pin: pin or unpin a capsule."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .. import audit_actions
from ..capsule import (
    format_rfc3339,
)
from ..capture import CaptureContext
from .common import (
    ADVISORY_LABEL,
    DATA_FRAMING,
    HandlerState,
    verb_result,
)
from .errors import invalid_params, unknown_capsule


def handle(
    state: HandlerState,
    args: dict[str, Any],
    context: CaptureContext | None,
    now: datetime,
    actor: str,
) -> dict[str, Any]:
    cid_arg = args.get("id")
    pinned = bool(args.get("pinned", True))
    reason = args.get("reason", "")
    if not cid_arg:
        return invalid_params("nmemory_capsule_pin requires id")
    cid = state.store.capsules.resolve_id(cid_arg)
    if cid is None:
        return unknown_capsule("nmemory_capsule_pin", cid_arg)

    state.store.pins.set_pin(cid, pinned, reason, actor, now)
    state.store.audit.append_audit(actor, audit_actions.PIN, cid, reason, now)
    return verb_result(
        {
            "label": ADVISORY_LABEL,
            "framing": DATA_FRAMING,
            "id": cid,
            "handle": state.store.capsules.get_handle(cid),
            "pinned": pinned,
            "reason": reason,
            "at": format_rfc3339(now),
        }
    )
