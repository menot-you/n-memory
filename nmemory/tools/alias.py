"""nmemory_synonym_add: record a recall synonym, or list the whole alias table."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .. import audit_actions
from ..capsule import (
    format_rfc3339,
)
from ..capture import CaptureContext
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
    alias = args.get("alias")
    term = args.get("term")
    if alias and term:
        fresh = state.store.aliases.add_alias(term, alias, now)
        state.store.audit.append_audit(
            actor, audit_actions.ALIAS, term, f"alias={alias}", now
        )
        return verb_result(
            {
                "label": ADVISORY_LABEL,
                "recorded": True,
                "already_recorded": not fresh,
                "aliases_for_term": state.store.aliases.aliases_for(term),
            }
        )

    aliases_tuples = state.store.aliases.list_aliases()
    return verb_result(
        {
            "label": ADVISORY_LABEL,
            "aliases": [
                {
                    "term": t,
                    "alias": a,
                    "at": format_rfc3339(at) if isinstance(at, datetime) else str(at),
                }
                for (t, a, at) in aliases_tuples
            ],
            "total": len(aliases_tuples),
        }
    )
