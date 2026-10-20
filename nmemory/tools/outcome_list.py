"""nmemory_outcome_list: the newest outcomes, bounded, newest first. It writes nothing."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..capture import CaptureContext
from ..project import canonical_project
from .common import ADVISORY_LABEL, DATA_FRAMING, HandlerState, verb_result
from .errors import invalid_params

LIMIT_MAX = 500


def handle(
    state: HandlerState,
    args: dict[str, Any],
    context: CaptureContext | None,
    now: datetime,
    actor: str,
) -> dict[str, Any]:
    limit = args.get("limit", 50)
    if not 1 <= limit <= LIMIT_MAX:
        return invalid_params(f"limit must be an integer from 1 to {LIMIT_MAX}")
    prefix = args.get("project_prefix")
    outcomes = state.store.substrate.recent_outcomes(
        limit, None if prefix is None else canonical_project(prefix)
    )
    return verb_result(
        {
            "label": ADVISORY_LABEL,
            "framing": DATA_FRAMING,
            "outcomes": [o.to_dict() for o in outcomes],
            "returned": len(outcomes),
        }
    )
