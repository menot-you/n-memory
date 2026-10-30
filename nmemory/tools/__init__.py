"""The MCP tool handlers, one module per tool, and the table that dispatches them.

`TOOLS` maps each wire name to its handler, `handle(state, args, context, now,
actor)` in `nmemory.tools.<noun>`; `MemoryServer.call_tool` validates the
arguments (`nmemory.tools.validation`), opens the store, builds the call's
`HandlerState` and looks the handler up here. `MemoryServer.SUPPORTED_TOOLS`
derives from it. Nothing here imports
`nmemory.server` or `nmemory.cli`.
"""

from __future__ import annotations

from . import (
    board,
    classify,
    context,
    digest,
    dod,
    get,
    ingest,
    list_,
    outcome,
    outcome_list,
    pin,
    purge,
    relate,
    retrieve,
    review,
    session_finish,
    session_start,
    work,
)
from .common import Handler

TOOLS: dict[str, Handler] = {
    "nmemory_board_show": board.handle,
    "nmemory_capsule_ingest": ingest.handle,
    "nmemory_capsule_list": list_.handle,
    "nmemory_capsule_pin": pin.handle,
    "nmemory_capsule_purge": purge.handle,
    "nmemory_capsule_retrieve": retrieve.handle,
    "nmemory_capsule_show": get.handle,
    "nmemory_context_show": context.handle,
    "nmemory_digest_show": digest.handle,
    "nmemory_dod_set": dod.handle,
    "nmemory_kind_classify": classify.handle,
    "nmemory_outcome_list": outcome_list.handle,
    "nmemory_outcome_record": outcome.handle,
    "nmemory_proposal_review": review.handle,
    "nmemory_relation_add": relate.handle,
    "nmemory_session_finish": session_finish.handle,
    "nmemory_session_start": session_start.handle,
    "nmemory_work_list": work.handle,
}
