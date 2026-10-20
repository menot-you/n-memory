"""nmemory_turn_extract: the capture candidates a text holds."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..capture import CaptureContext
from ..extract import extract
from .common import (
    ADVISORY_LABEL,
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
    content = args.get("content") or args.get("text", "")
    cands = extract(content)
    return verb_result(
        {
            "label": ADVISORY_LABEL,
            "count": len(cands),
            "candidates": [c.to_dict() for c in cands],
        }
    )
