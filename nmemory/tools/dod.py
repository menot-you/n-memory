"""nmemory_dod_set: one audited event on a Definition of Done's status or verdict axis."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .. import audit_actions
from ..capture import CaptureContext
from ..queries.work import dod_contradictions
from ..repositories.base import StoreError
from .common import (
    HandlerState,
    error_result,
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
    if (
        "actor" not in args
        or args.get("actor") is None
        or not str(args.get("actor", "")).strip()
    ):
        return error_result("failed to deserialize parameters: missing field `actor`")

    cid_arg = args.get("capsule_id") or args.get("id")
    if not cid_arg:
        return invalid_params("nmemory_dod_set requires capsule_id")

    cid = state.store.capsules.resolve_id(cid_arg) or cid_arg
    status = args.get("status")
    verdict = args.get("verdict")
    if (status is not None and verdict is not None) or (
        status is None and verdict is None
    ):
        return invalid_params(
            "status and verdict are SEPARATE axes — authority and observation are different acts and each is its own audited event. Pass exactly one per call (nothing was recorded)"
        )

    dod_actor = str(args["actor"])

    reason = args.get("reason", "")
    axis = "status" if status is not None else "verdict"
    val_raw = status if status is not None else verdict
    value = str(val_raw) if val_raw is not None else ""

    try:
        state.store.append_dod_event(
            capsule_id=cid,
            axis=axis,
            value=value,
            actor=dod_actor,
            reason=reason,
            now=now,
        )
    except (StoreError, ValueError, KeyError) as exc:
        return invalid_params(str(exc))

    dod_state = state.store.dod.dod_state_of(cid)
    contradictions = dod_contradictions(state.store.db, cid)
    state.store.audit.append_audit(
        actor,
        audit_actions.DOD,
        cid,
        f"{axis}={value} actor={dod_actor} standing_contradictions={len(contradictions)}",
        now,
    )

    dod_res = {
        "capsule_id": cid,
        "handle": state.store.capsules.get_handle(cid),
        "status": dod_state.status,
        "verdict": dod_state.verdict,
        "recorded": True,
    }
    if contradictions:
        dod_res["contradictions"] = [c.to_dict() for c in contradictions]

    return verb_result(dod_res)
