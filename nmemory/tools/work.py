"""nmemory_work_list: bounded, scoped enumeration of canonical open work."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..capture import CaptureContext
from ..project import canonical_project
from ..queries.work import WorkPageError, work_page
from .common import HandlerState, verb_result
from .errors import invalid_params


def handle(
    state: HandlerState,
    args: dict[str, Any],
    context: CaptureContext | None,
    now: datetime,
    actor: str,
) -> dict[str, Any]:
    try:
        rows, next_cursor = work_page(
            state.store.db,
            canonical_project(args["project_id"]),
            subtree=args.get("subtree", False),
            limit=args["limit"],
            cursor=args.get("cursor"),
            now=now,
            headline_prefix=args.get("headline_prefix"),
            include_content=args.get("include_content", False),
            source=args.get("source"),
        )
    except WorkPageError as exc:
        return invalid_params(str(exc))
    return verb_result(
        {"rows": [row.to_dict() for row in rows], "next_cursor": next_cursor}
    )
