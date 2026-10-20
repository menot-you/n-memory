"""nmemory_kind_classify: classify content, and persist the result on a named capsule."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .. import audit_actions
from ..capture import CaptureContext
from ..classify import (
    CandidateKind,
    ClassificationContext,
    ClassificationScope,
    ClassifyError,
    ContentOrigin,
    classify,
)
from ..repositories.base import StoreError
from .common import (
    ADVISORY_LABEL,
    HandlerState,
    verb_result,
)
from .errors import invalid_params


def handle(
    state: HandlerState,
    args: dict[str, Any],
    context: CaptureContext | None,
    now: datetime,
    actor: str,
) -> dict[str, Any]:
    cid_arg = args.get("capsule_id") or args.get("id")
    content = args.get("content", "")
    kind_arg = args.get("kind")
    scope_arg = args.get("scope")
    evidence_state = args.get("evidence_state")
    proof_hint = args.get("proof_hint")
    stale_if = args.get("stale_if")

    cid = state.store.capsules.resolve_id(cid_arg) if cid_arg else None
    if cid_arg and not cid:
        cid = cid_arg

    sc = state.store.capsules.get(cid) if cid else None
    if sc and not content:
        content = sc.capsule.content

    origin = ContentOrigin.from_str(args.get("origin", "extracted-candidate"))
    ctx = ClassificationContext(
        project_id=state.effective_defaults(context).project_id,
        taint_hint=bool(args.get("taint_hint", False)),
        kind_override=CandidateKind.from_str(kind_arg) if kind_arg else None,
        scope_override=ClassificationScope.from_str(scope_arg) if scope_arg else None,
    )
    try:
        res = classify(content, origin=origin, context=ctx)
    except ClassifyError as exc:
        return {"error": {"code": -32602, "message": str(exc)}}

    if cid:
        try:
            state.store.set_classification(
                cid,
                res.kind,
                res.scope,
                now=now,
            )
        except (StoreError, ValueError, KeyError) as exc:
            return invalid_params(str(exc))
        epi_parts = []
        if evidence_state:
            epi_parts.append(f"evidence_state={evidence_state}")
        if proof_hint:
            epi_parts.append("+proof_hint")
        if stale_if:
            epi_parts.append("+stale_if")
        epi_note = f" epistemics {' '.join(epi_parts)}" if epi_parts else ""

        content_matches = (sc.capsule.content == content) if sc else True
        mismatch_note = " content-mismatch" if content_matches is False else ""

        if evidence_state or proof_hint or stale_if:
            state.store.epistemics.set_epistemic(
                cid,
                evidence_state=evidence_state,
                proof_hint=proof_hint,
                stale_if=stale_if,
                now=now,
            )
        state.store.audit.append_audit(
            actor,
            audit_actions.CLASSIFY,
            cid,
            f"kind={res.kind.value} scope={res.scope.value}{mismatch_note}{epi_note}",
            now,
        )

    resp: dict[str, Any] = {
        "label": ADVISORY_LABEL,
        "kind": res.kind.value,
        "scope": res.scope.value,
        "authority_class": res.authority_class.value,
        "instruction_taint": res.instruction_taint,
    }
    if cid:
        resp["persisted"] = cid
        resp["persisted_handle"] = state.store.capsules.get_handle(cid)
        resp["content_matches_capsule"] = sc.capsule.content == content if sc else True
    return verb_result(resp)
