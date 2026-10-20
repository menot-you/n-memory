"""nmemory_session_finish: close a session, with its handoff and open DoDs."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .. import audit_actions
from ..capsule import (
    AuthorityClass,
    Capsule,
    Freshness,
    Provenance,
    Scope,
    format_rfc3339,
    headline_of,
    sha256_hex,
)
from ..capture import CaptureContext
from ..ingest import DEFAULT_CONFIDENCE
from ..language import foreign_language, glued_words
from ..queries.work import dod_of_session
from ..repositories.capsules import Tombstoned
from ..repositories.sessions import SessionFinished, UnknownSession
from ..taint import is_suspicious
from .common import (
    HandlerState,
    verb_result,
)
from .errors import invalid_params
from .ingest import ENGLISH_HINT, spacing_detail


def handle(
    state: HandlerState,
    args: dict[str, Any],
    context: CaptureContext | None,
    now: datetime,
    actor: str,
) -> dict[str, Any]:
    session_id = args.get("session_id")
    if not session_id:
        return invalid_params("nmemory_session_finish requires session_id")

    summary = args.get("summary")
    handoff = args.get("handoff")

    handoff_cap_id: str | None = None
    handoff_handle: str | None = None

    if handoff:
        prov = Provenance(
            source=f"session:{session_id}:handoff",
            anchor=session_id,
            source_hash=sha256_hex(handoff),
        )
        cap = Capsule(
            content=handoff,
            provenance=prov,
            confidence=DEFAULT_CONFIDENCE,
            freshness=Freshness(valid_from=now),
            scope=Scope(project_id=state.effective_defaults(context).project_id),
            authority_class=AuthorityClass.AGENT_INFERRED,
            instruction_taint=is_suspicious(handoff),
        )
        try:
            cid, _is_new = state.store.append(
                cap, now, session_id=session_id, context=context
            )
        except Tombstoned:
            pass  # Close the session and report the omitted handoff below.
        else:
            state.store.audit.append_audit(
                actor, audit_actions.INGEST, cid, "handoff capture", now
            )
            handoff_cap_id = cid
            if not state.capture_only:
                handoff_handle = state.store.capsules.get_handle(cid)

    try:
        state.store.sessions.finish_session(session_id, summary, now)
    except UnknownSession:
        return {
            "jsonrpc": "2.0",
            "error": {
                "code": -32002,
                "message": f"unknown session {session_id}",
                "data": {"kind": "unknown_session", "id": session_id},
            },
        }
    except SessionFinished:
        return {
            "jsonrpc": "2.0",
            "error": {
                "code": -32002,
                "message": f"session {session_id} is already finished",
                "data": {"kind": "finished_session", "id": session_id},
            },
        }

    state.store.audit.append_audit(
        actor, audit_actions.SESSION_FINISH, session_id, summary, now
    )

    # The closing rule is the ONLY one in the repository: live edge witnesses|
    # supersedes (`CLOSING_KINDS`). `sem_veredito` is another question --- and
    # BOTH are served, because a single number next to the list makes the reader
    # compare incompatible totals (store.py:2168-2174).
    dods = [] if state.capture_only else dod_of_session(state.store.db, session_id)
    abertas = [(cid, texto) for cid, texto, fechada, _v in dods if not fechada]
    # Count over the OPEN ones, the same universe as the list served alongside
    # and that of store.py:2176-2180. Counting closed ones without a verdict as well
    # put "3 without verdict" next to 2 open ones --- the same
    # incomparability of store.py:2168-2174, in reverse.
    sem_veredito = sum(
        1 for _c, _t, fechada, veredito in dods if not fechada and not veredito
    )

    res: dict[str, Any] = {
        "session_id": session_id,
        "finished_at": format_rfc3339(now),
        "summary": summary,
        "dod_opened": len(dods),
        "dod_closed": len(dods) - len(abertas),
        "dod_without_verdict": sem_veredito,
        "dod_open": [
            {"id": cid, "headline": headline_of(texto)} for cid, texto in abertas
        ],
    }
    if handoff_cap_id:
        res["handoff_capsule"] = handoff_cap_id
        res["handoff_handle"] = handoff_handle
    elif handoff:
        res["warning"] = (
            "The handoff matches purged content. "
            "The session closed without a new handoff."
        )
        if state.capture_only:
            return verb_result(
                {
                    "session_id": res["session_id"],
                    "finished_at": res["finished_at"],
                    "warning": "handoff_validation",
                }
            )
        return verb_result(res)
    # Never a refusal: the handoff is called when a session goes idle or
    # ends, and nobody is left to rewrite it. An error here would roll the
    # finish back (`tool_boundary`) and lose the continuity it exists for, so
    # even a broken detector (numpy, the model file) only costs the warning.
    # Glued prose is judged first: the language detector cannot read it.
    try:
        glued = glued_words(handoff) if handoff else None
        foreign = foreign_language(handoff) if handoff and glued is None else None
    except Exception as exc:  # noqa: BLE001
        res["warning"] = f"the handoff was stored; its prose was not checked ({exc!r})"
        glued = foreign = None
    if glued is not None:
        res["warning"] = (
            f"the handoff was stored, but {spacing_detail(glued, 'handoffs')}"
        )
    elif foreign is not None:
        res["warning"] = (
            f"the handoff was stored, but its prose reads as {foreign!r}, not "
            f"English{ENGLISH_HINT}"
        )

    if state.capture_only:
        receipt: dict[str, Any] = {
            key: res[key]
            for key in ("session_id", "finished_at", "handoff_capsule")
            if key in res
        }
        if "warning" in res:
            receipt["warning"] = "handoff_validation"
        return verb_result(receipt)
    return verb_result(res)
