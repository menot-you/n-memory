"""nmemory_coverage_show: the stored capsules that already cover a text."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from ..capture import CaptureContext
from ..ingest import (
    coverage_matches,
)
from ..project import (
    canonical_project,
)
from .common import (
    ADVISORY_LABEL,
    DATA_FRAMING,
    HandlerState,
    verb_result,
)


def handle(
    state: HandlerState,
    args: dict[str, Any],
    context: CaptureContext | None,
    now: datetime,
    actor: str,
) -> dict[str, Any]:
    text = args["text"]
    if not text.strip():
        raise ValueError("nmemory_coverage_show text must be non-empty")
    min_score = float(args.get("min_score", 0.8))
    if not math.isfinite(min_score) or not 0.0 <= min_score <= 1.0:
        raise ValueError("nmemory_coverage_show min_score must be between 0 and 1")
    limit = int(args.get("limit", 10))
    if not 0 <= limit <= 20:
        raise ValueError("nmemory_coverage_show limit must be between 0 and 20")
    project_id = args.get("project_id")
    if project_id is not None:
        project_id = canonical_project(project_id)
    matches = coverage_matches(
        state.store,
        text,
        project_id=project_id,
        session_id=args.get("session_id"),
        min_score=min_score,
        limit=limit,
        now=now,
    )
    return verb_result(
        {
            "label": ADVISORY_LABEL,
            "framing": DATA_FRAMING,
            "matches": [
                {
                    "id": match.id,
                    "content": match.content,
                    "score": match.score,
                }
                for match in matches
            ],
        }
    )
