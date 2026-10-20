"""nmemory_capsule_reanchor: point a capsule at a new anchor."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .. import audit_actions
from ..anchor import parse_anchor
from ..capture import CaptureContext
from .common import (
    HandlerState,
    verb_result,
)
from .errors import invalid_params, unknown_capsule
from .ingest import anchor_note


def handle(
    state: HandlerState,
    args: dict[str, Any],
    context: CaptureContext | None,
    now: datetime,
    actor: str,
) -> dict[str, Any]:
    cid_arg = args.get("capsule_id") or args.get("id")
    anchor = args.get("anchor")
    reason = args.get("reason", "")
    reviewed = bool(args.get("reviewed", False))
    if not cid_arg or not anchor:
        return invalid_params("nmemory_capsule_reanchor requires capsule_id and anchor")
    cid = state.store.capsules.resolve_id(cid_arg)
    if cid is None:
        return unknown_capsule("nmemory_capsule_reanchor", cid_arg)

    sc = state.store.capsules.get(cid)
    was = state.store.anchors.get_reanchor(cid) or (
        sc.capsule.provenance.anchor if sc else "unknown:1"
    )
    state.store.anchors.reanchor(cid, anchor, reason, now)
    state.store.audit.append_audit(
        actor, audit_actions.REANCHOR, cid, "reanchored", now
    )

    res: dict[str, Any] = {
        "capsule_id": cid,
        "handle": state.store.capsules.get_handle(cid),
        "was": was,
        "now": anchor,
        "reviewed": reviewed,
    }
    note = anchor_note(state.anchor_root, parse_anchor(anchor).path)
    if note is not None:
        res["anchor_note"] = note

    return verb_result(res)
