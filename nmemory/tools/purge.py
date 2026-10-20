"""nmemory_capsule_purge (and its retired name nmemory_capsule_purge): tombstone a capsule."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .. import audit_actions
from ..capsule import (
    format_rfc3339,
)
from ..capture import CaptureContext
from ..repositories.capsules import CapsuleNotFound, Tombstoned
from .common import (
    ADVISORY_LABEL,
    DATA_FRAMING,
    HandlerState,
    verb_result,
)
from .errors import invalid_params
from .get import render_tombstone_get


def purge_item_fields(item: object) -> tuple[str, str, str] | dict[str, Any]:
    """`(id, mode, reason)` of one batch item, or its `invalid_item` row.

    `mode` defaults to `purged`; `id` and `reason` are required.
    """
    if not isinstance(item, dict):
        return {"status": "invalid_item", "detail": "item must be an object"}
    cid = item.get("id") or item.get("capsule_id")
    reason = item.get("reason")
    if not cid or not reason:
        return {
            "id": cid or "",
            "status": "invalid_item",
            "detail": "item requires id and reason",
        }
    return cid, item.get("mode") or "purged", reason


def _purge_one(
    state: HandlerState,
    fields: tuple[str, str, str],
    hmac_key: bytes,
    actor: str,
    now: datetime,
) -> dict[str, Any]:
    """Tombstone one batch item; its row names the outcome, never raises it."""
    cid, mode, reason = fields
    resolved = state.store.capsules.resolve_id(cid) or cid
    try:
        tomb = state.store.capsules.forget_capsule(
            capsule_id=resolved,
            mode=mode,
            reason=reason,
            hmac_key=hmac_key,
            now=now,
        )
        state.store.audit.append_audit(
            actor, audit_actions.FORGET, resolved, tomb.reason, now
        )
        return {
            "id": resolved,
            "handle": state.store.capsules.get_handle(resolved),
            "status": "tombstoned",
            "mode": mode,
            "at": format_rfc3339(now),
            "reason": tomb.reason,
            "content_hmac": tomb.content_hmac,
        }
    except Tombstoned:
        return {
            "id": resolved,
            "handle": state.store.capsules.get_handle(resolved),
            "status": "already_tombstoned",
        }
    except CapsuleNotFound:
        return {"id": resolved, "status": "not_found"}
    except ValueError as e:
        return {"id": resolved, "status": "invalid_reason", "detail": str(e)}


def _batch_purge(
    state: HandlerState, raw_items: Any, hmac_key: bytes, actor: str, now: datetime
) -> dict[str, Any]:
    if not isinstance(raw_items, list) or len(raw_items) < 1 or len(raw_items) > 32:
        return invalid_params(
            "nmemory_capsule_purge items must contain 1..=32 references"
        )
    outcomes: list[dict[str, Any]] = []
    for item in raw_items:
        fields = purge_item_fields(item)
        outcomes.append(
            fields
            if isinstance(fields, dict)
            else _purge_one(state, fields, hmac_key, actor, now)
        )
    return verb_result(
        {"label": ADVISORY_LABEL, "framing": DATA_FRAMING, "results": outcomes}
    )


def _not_purgeable(message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "error": {"code": -32002, "message": message}}


def handle(
    state: HandlerState,
    args: dict[str, Any],
    context: CaptureContext | None,
    now: datetime,
    actor: str,
) -> dict[str, Any]:
    try:
        hmac_key = state.resolve_hmac_key()
    except ValueError as e:
        return {"jsonrpc": "2.0", "error": {"code": -32603, "message": str(e)}}

    if "items" in args:
        return _batch_purge(state, args["items"], hmac_key, actor, now)

    cid = args.get("id") or args.get("capsule_id")
    mode = args.get("mode") or "purged"
    reason = args.get("reason")
    if not cid or not reason:
        return invalid_params("nmemory_capsule_purge requires id, mode, and reason")

    resolved = state.store.capsules.resolve_id(cid) or cid
    try:
        tomb = state.store.capsules.forget_capsule(
            capsule_id=resolved,
            mode=mode,
            reason=reason,
            hmac_key=hmac_key,
            now=now,
        )
    except ValueError as e:
        return invalid_params(f"nmemory_capsule_purge invalid reason: {e}")
    except CapsuleNotFound as e:
        return _not_purgeable(str(e))
    except Tombstoned as e:
        return _not_purgeable(str(e))

    state.store.audit.append_audit(
        actor, audit_actions.FORGET, resolved, tomb.reason, now
    )
    return verb_result(render_tombstone_get(state, tomb, resolved, actor))
