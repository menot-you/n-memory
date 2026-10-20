"""nmemory_relation_add: record a typed relation between two capsules."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .. import audit_actions
from ..capture import CaptureContext
from ..repositories.base import StoreError
from .common import (
    HandlerState,
    verb_result,
)
from .errors import invalid_params


def handle(
    state: HandlerState,
    args: dict[str, Any],
    context: CaptureContext | None,
    now: datetime,
    actor: str,
) -> dict[str, Any]:
    kind_str = args.get("kind")
    from_id = args.get("from") or args.get("from_id")
    to_id = args.get("to") or args.get("to_id")
    if not kind_str or not from_id or not to_id:
        return invalid_params("nmemory_relation_add requires kind, from, and to")

    try:
        is_new = state.store.add_relation(
            kind_str,
            from_id,
            to_id,
            origin="manual",
            now=now,
            reason=args.get("reason"),
        )
    except (StoreError, ValueError, KeyError) as exc:
        return invalid_params(str(exc))

    state.store.audit.append_audit(
        actor,
        audit_actions.RELATE,
        from_id,
        f"{kind_str} -> {to_id}"
        if is_new
        else f"{kind_str} -> {to_id} (already recorded)",
        now,
    )
    if state.capture_only:
        return verb_result(
            {"kind": kind_str, "from": from_id, "to": to_id, "recorded": True}
        )
    return verb_result(
        {
            "kind": kind_str,
            "from": from_id,
            "from_handle": state.store.capsules.get_handle(from_id),
            "to": to_id,
            "to_handle": state.store.capsules.get_handle(to_id),
            "recorded": True,
            "already_recorded": not is_new,
        }
    )
