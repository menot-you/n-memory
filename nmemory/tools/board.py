"""nmemory_board_show: a project's tasks in columns, or one card of them."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..capture import CaptureContext
from ..project import canonical_project
from ..queries.board import board
from ..queries.work import WorkPageError
from .common import HandlerState, verb_result
from .errors import invalid_params, unknown_capsule


def handle(
    state: HandlerState,
    args: dict[str, Any],
    context: CaptureContext | None,
    now: datetime,
    actor: str,
) -> dict[str, Any]:
    named = args.get("capsule_id")
    capsule_id = None
    if named is not None:
        capsule_id = state.store.capsules.resolve_id(named)
        if capsule_id is None:
            return unknown_capsule("nmemory_board_show", named)
    try:
        shown = board(
            state.store.db,
            canonical_project(args["project_id"]),
            limit=args["limit"],
            now=now,
            capsule_id=capsule_id,
        )
    except WorkPageError as exc:
        return invalid_params(str(exc))
    return verb_result(shown)
