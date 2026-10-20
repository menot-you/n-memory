"""nmemory_outcome_record: record an advisory outcome, or list them."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .. import audit_actions
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
    if not args:
        outcomes = state.store.substrate.list_outcomes()
        return verb_result(
            {
                "label": ADVISORY_LABEL,
                "framing": DATA_FRAMING,
                "outcomes": [o.to_dict() for o in outcomes],
                "total": len(outcomes),
            }
        )

    desc = args.get("description")
    act = args.get("actor")
    if not desc or not act:
        return invalid_params(
            "nmemory_outcome_record record requires description AND actor together "
            "(actor names WHO observed — there is no default); evidence_ref and "
            "capsule_id are optional, and receipt_id + score are an optional pair. "
            "Pass NO fields to list."
        )

    cid_arg = args.get("capsule_id")
    cid = None if cid_arg is None else state.store.capsules.resolve_id(cid_arg)
    if cid_arg is not None and cid is None:
        return unknown_capsule("nmemory_outcome_record", cid_arg)

    rec = state.store.substrate.record_outcome(
        actor=act,
        description=desc,
        capsule_id=cid,
        evidence_ref=args.get("evidence_ref"),
        receipt_id=args.get("receipt_id"),
        score=float(args["score"]) if "score" in args else None,
        now=now,
    )
    if args.get("receipt_id") is not None and "score" in args:
        audit_reason = f"actor={act} receipt={args['receipt_id']} score={args['score']}"
    else:
        audit_reason = f"actor={act}"
    state.store.audit.append_audit(
        actor, audit_actions.OUTCOME, rec.id, audit_reason, now
    )
    if state.capture_only:
        return verb_result({"recorded": {"id": rec.id}})
    return verb_result(
        {
            "label": ADVISORY_LABEL,
            "framing": DATA_FRAMING,
            "advisory": (
                "ADVISORY observation record — NOT a witnessed close; nothing in nmemory "
                "treats a recorded outcome as proven (a witnessed close needs the kernel). "
                "Recording one never changes any capsule's recall ELIGIBILITY — only an "
                "explicit nmemory_relation_add falsifies edge fences recall. A SCORED record "
                "(receipt_id + score) is stored with the row and changes no ranking."
            ),
            "recorded": rec.to_dict(),
        }
    )
