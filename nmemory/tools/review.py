"""nmemory_proposal_review: ratify or reject a live proposal."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .. import audit_actions
from ..capsule import (
    format_rfc3339,
)
from ..capture import CaptureContext
from ..queries.board import waiting_changes
from ..queries.staged import standing_proposal
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
    """Review a live proposal or correct an automatic decision.

    Human decisions, forgotten capsules and curator evidence remain fenced.
    """
    cid_arg = str(args.get("capsule_id") or "")
    verdict = str(args.get("verdict") or "")
    reviewer = str(args.get("actor") or "").strip()
    reason = str(args.get("reason") or audit_actions.REVIEW)
    if not cid_arg or not reviewer:
        return invalid_params(
            "nmemory_proposal_review requires capsule_id and a non-empty actor"
        )
    if reviewer in ("jev", "system1", "curator"):
        return invalid_params(
            "nmemory_proposal_review actor is reserved for an automatic writer"
        )
    # `verdict` outside the enum and missing fields die in `validate_tool_arguments`
    # (in-band isError) before reaching here; what remains is semantics.
    cid = state.store.capsules.resolve_id(cid_arg)
    if cid is None:
        return unknown_capsule("nmemory_proposal_review", cid_arg)
    # Eligibility and the review write share the write lock.
    state.store.db.begin_write()
    blocker = standing_proposal(state.store.db, cid, allow_automatic_correction=True)
    if blocker == "ratified" and verdict == "rejected":
        with state.store.db.connection() as conn, conn.cursor() as cur:
            if waiting_changes(cur, cid):
                blocker = None
    if blocker is not None:
        return invalid_params(
            f"{cid} is not a standing proposal (current review state: "
            f"{blocker}) — nmemory_proposal_review requires a live proposal or an "
            "automatic decision that is not curator evidence"
        )
    state.store.reviews.append_review_event(cid, verdict, reason, reviewer, now)
    state.store.audit.append_audit(
        actor, audit_actions.REVIEW, cid, f"{verdict}: {reason}", now
    )
    return verb_result(
        {
            "label": ADVISORY_LABEL,
            "framing": DATA_FRAMING,
            "id": cid,
            "handle": state.store.capsules.get_handle(cid),
            "review": {"state": verdict, "actor": reviewer, "reason": reason},
            "at": format_rfc3339(now),
        }
    )
