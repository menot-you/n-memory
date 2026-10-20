"""nmemory_capsule_show: render capsules and tombstones by reference, with their mentions."""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import UTC, datetime
from typing import Any

from .. import audit_actions
from ..capsule import (
    format_rfc3339,
    headline_of,
)
from ..capsule_ref import CapsuleRefResolution, contains_exact_ref
from ..capture import CaptureContext
from ..classify import (
    CandidateKind,
)
from ..queries.standing import standing_of
from ..queries.work import dod_contradictions
from ..repositories.base import StoreError
from ..repositories.capsules import StoredCapsule, Tombstoned
from ..subproc import HEALTH_S, run_with_timeout
from .common import (
    ADVISORY_LABEL,
    DATA_FRAMING,
    HandlerState,
    record_read_traffic,
    taint_summary,
    verb_result,
)


def git_mentions(anchor_root: str, references: list[str]) -> dict[str, Any]:
    """Find references in git-tracked files under anchor_root via git grep."""
    unique = list(dict.fromkeys(references))
    if not unique:
        return {"status": "ready", "matches": {}, "truncated": False}
    git_bin = shutil.which("git") or "/usr/bin/git"
    try:
        cmd = [
            git_bin,
            "-C",
            anchor_root,
            "grep",
            "-n",
            "-z",
            "--full-name",
            "--no-color",
            "-F",
        ]
        for r in unique:
            cmd.extend(["-e", r])
        cmd.extend(["--", "."])
        proc = run_with_timeout(
            cmd,
            capture_output=True,
            text=True,
            timeout=HEALTH_S,
            check=False,
        )
        if proc.returncode not in (0, 1):
            return {
                "status": "unavailable" if proc.returncode == 128 else "error",
                "matches": {r: [] for r in unique},
                "truncated": False,
            }
        matches: dict[str, list[dict[str, Any]]] = {r: [] for r in unique}
        truncated = False
        lines = proc.stdout.split("\n")
        total_matches = 0
        for line in lines:
            if not line:
                continue
            parts = line.split("\0", 2)
            if len(parts) < 3:
                continue
            path, line_str, content = parts[0], parts[1], parts[2]
            try:
                line_num = int(line_str)
            except ValueError:
                continue
            for r in unique:
                if contains_exact_ref(content, r):
                    entry = {"path": path, "line": line_num}
                    if entry not in matches[r]:
                        if len(matches[r]) < 8 and total_matches < 64:
                            matches[r].append(entry)
                            total_matches += 1
                        else:
                            truncated = True
        return {"status": "ready", "matches": matches, "truncated": truncated}
    except subprocess.TimeoutExpired:
        return {
            "status": "timed_out",
            "matches": {r: [] for r in unique},
            "truncated": False,
        }
    except OSError, ValueError, subprocess.SubprocessError:
        return {
            "status": "unavailable",
            "matches": {r: [] for r in unique},
            "truncated": False,
        }


def _render_capsule_get(
    state: HandlerState,
    sc: StoredCapsule,
    context: CaptureContext | None,
    actor: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    cap = sc.capsule
    h = state.store.capsules.get_handle(sc.id)
    ctx = state.store.capsules.get_capture_context(sc.id)
    tier = state.store.tiers.get_tier(sc.id)
    rels = state.store.relations.list_relations_for(sc.id)

    ctx_dict = ctx.to_dict() if ctx else None

    cls = state.store.classifications.get_classification(sc.id)
    audit_events = state.store.audit.list_audit(subject=sc.id, limit=1)
    if audit_events:
        last_ev = audit_events[0]
        last_mut = {
            "actor": last_ev.actor,
            "at": format_rfc3339(last_ev.at),
            "event": last_ev.action,
        }
    else:
        last_mut = {
            "actor": actor,
            "at": format_rfc3339(sc.created_at),
            "event": audit_actions.INGEST,
        }

    rels_list = [
        {
            "at": format_rfc3339(r.at),
            "from": r.from_id,
            "kind": r.kind.value if hasattr(r.kind, "value") else str(r.kind),
            "to": r.to_id,
        }
        for r in rels
    ]
    res_dict: dict[str, Any] = {
        "capsule": {
            "authority_class": cap.authority_class.value,
            "confidence": cap.confidence,
            "content": cap.content,
            "freshness": {
                "valid_from": format_rfc3339(cap.freshness.valid_from),
                "valid_to": (
                    format_rfc3339(cap.freshness.valid_to)
                    if cap.freshness.valid_to
                    else None
                ),
            },
            "instruction_taint": cap.instruction_taint,
            "provenance": {
                "anchor": cap.provenance.anchor,
                "source": cap.provenance.source,
                "source_hash": cap.provenance.source_hash,
            },
            "scope": {
                "project_id": cap.scope.project_id,
            },
        },
        "capture_context": ctx_dict,
        "created_at": format_rfc3339(sc.created_at),
        "framing": DATA_FRAMING,
        "handle": h,
        "headline": headline_of(cap.content),
        "id": sc.id,
        "label": ADVISORY_LABEL,
        "last_mutation": last_mut,
        "relations": rels_list,
        "seq": sc.seq,
        "tier": tier.value,
    }
    reanchor = state.store.anchors.get_reanchor(sc.id)
    if reanchor:
        res_dict["anchor_effective"] = reanchor

    if rels:
        rel_handles = {}
        for r in rels:
            h_from = state.store.capsules.get_handle(r.from_id)
            if h_from:
                rel_handles[r.from_id] = h_from
            h_to = state.store.capsules.get_handle(r.to_id)
            if h_to:
                rel_handles[r.to_id] = h_to
        if rel_handles:
            res_dict["handles"] = dict(sorted(rel_handles.items()))

    pin = state.store.pins.get_pin(sc.id)
    if pin:
        res_dict["pinned"] = True

    if cls:
        res_dict["classification"] = {
            "at": format_rfc3339(cls.at),
            "kind": cls.kind.value,
            "scope": cls.scope.value,
        }

    ep = state.store.epistemics.get_epistemic(sc.id)
    if ep:
        ep_dict = {"at": format_rfc3339(sc.created_at)}
        if ep.get("evidence_state"):
            ep_dict["evidence_state"] = ep["evidence_state"]
        if ep.get("proof_hint"):
            ep_dict["proof_hint"] = ep["proof_hint"]
        if ep.get("stale_if"):
            ep_dict["stale_if"] = ep["stale_if"]
        res_dict["epistemics"] = ep_dict

    sq = state.store.epistemics.get_source_quote(sc.id)
    if sq:
        res_dict["source_quote"] = {"quote": sq["quote"], "verdict": sq["verdict"]}

    et = state.store.capsules.event_time_of(sc.id)
    if et:
        res_dict["event_time"] = {
            "declared_at": format_rfc3339(et.declared_at),
            "event_from": format_rfc3339(et.event_from()),
            "event_to": format_rfc3339(et.event_to()),
        }

    from ..taint import scan as taint_scan

    t_findings = taint_summary(taint_scan(cap.content))
    if t_findings:
        res_dict["taint_findings"] = t_findings

    if cls and cls.kind == CandidateKind.DOD:
        dod_st = state.store.dod.dod_state_of(sc.id)
        res_dict["dod"] = dod_st.to_dict()

    contradictions = dod_contradictions(state.store.db, sc.id)
    if contradictions:
        res_dict["dod_contradictions"] = [c.to_dict() for c in contradictions]

    # U3: nothing is served without standing — derived on read, kind-aware
    # (a dod/task does not take one; a decision is valid by its existence).
    standing = standing_of(state.store.db, [sc.id], now=now or datetime.now(UTC))
    if sc.id in standing:
        res_dict["standing"] = standing[sc.id]

    return res_dict


def render_tombstone_get(
    state: HandlerState, tomb: Any, cid: str, actor: str
) -> dict[str, Any]:
    audit_events = state.store.audit.list_audit(subject=cid, limit=1)
    if audit_events:
        last_ev = audit_events[0]
        last_mut = {
            "actor": last_ev.actor,
            "at": format_rfc3339(last_ev.at),
            "event": last_ev.action,
        }
    else:
        last_mut = {
            "actor": actor,
            "at": format_rfc3339(tomb.at),
            "event": audit_actions.FORGET,
        }
    rels = state.store.relations.list_relations_for(cid)
    rels_list = [
        {
            "at": format_rfc3339(r.at),
            "from": r.from_id,
            "kind": r.kind.value if hasattr(r.kind, "value") else str(r.kind),
            "to": r.to_id,
        }
        for r in rels
    ]
    tomb_res: dict[str, Any] = {
        "at": format_rfc3339(tomb.at),
        "content_hmac": tomb.content_hmac,
        "framing": DATA_FRAMING,
        "handle": state.store.capsules.get_handle(cid),
        "id": cid,
        "label": ADVISORY_LABEL,
        "last_mutation": last_mut,
        "mode": tomb.mode,
        "outcome": "tombstoned",
        "reason": tomb.reason,
        "relations": rels_list,
    }
    if rels:
        rel_handles = {}
        for r in rels:
            h_from = state.store.capsules.get_handle(r.from_id)
            if h_from:
                rel_handles[r.from_id] = h_from
            h_to = state.store.capsules.get_handle(r.to_id)
            if h_to:
                rel_handles[r.to_id] = h_to
        if rel_handles:
            tomb_res["handles"] = dict(sorted(rel_handles.items()))
    return tomb_res


def mentions_of(
    ref: str,
    store_mentions: dict[str, tuple[list[Any], bool]],
    git_report: dict[str, Any],
    *,
    status_first: bool,
) -> dict[str, Any]:
    """Where an unfound reference is still mentioned: the store and git.

    The batch form serves `status` before `matches`, the single form after;
    both orders are on the wire.
    """
    sm, sm_trunc = store_mentions.get(ref, ([], False))
    gm = git_report["matches"].get(ref, [])
    store_part = {"status": "ready", "matches": sm}
    git_part = {"status": git_report["status"], "matches": gm}
    if not status_first:
        store_part = {"matches": sm, "status": "ready"}
        git_part = {"matches": gm, "status": git_report["status"]}
    return {
        "store": {**store_part, **({"truncated": True} if sm_trunc else {})},
        "git": {**git_part, **({"truncated": True} if git_report["truncated"] else {})},
    }


def unresolved_item(ref: str, res: CapsuleRefResolution) -> dict[str, Any] | None:
    """The batch row of a reference that does not resolve; None for an unknown status."""
    if res.status == "not_found":
        return {"requested_ref": ref, "status": "not_found", "id": res.id}
    if res.status == "invalid_ref":
        return {"requested_ref": ref, "status": "invalid_ref", "detail": res.detail}
    if res.status in ("kind_mismatch", "slug_mismatch"):
        return {
            "requested_ref": ref,
            "status": res.status,
            "id": res.id,
            "expected_handle": res.expected_handle,
        }
    return None


def unresolved_outcome(ref: str, res: CapsuleRefResolution) -> dict[str, Any] | None:
    """The single answer of an invalid or mismatched reference; None otherwise."""
    head = {"label": ADVISORY_LABEL, "framing": DATA_FRAMING, "outcome": res.status}
    if res.status == "invalid_ref":
        return {**head, "requested_ref": ref, "detail": res.detail}
    if res.status in ("kind_mismatch", "slug_mismatch"):
        return {
            **head,
            "requested_ref": ref,
            "id": res.id,
            "expected_handle": res.expected_handle,
        }
    return None


def _stored_or_none(state: HandlerState, capsule_id: str) -> StoredCapsule | None:
    """The stored capsule; a tombstone's NULL-content marker row reads as None.

    get() fails closed on the marker (pin store.rs:1770); nmemory_capsule_show
    renders the marker instead.
    """
    try:
        return state.store.capsules.get(capsule_id)
    except Tombstoned:
        return None


def _batch_item(
    state: HandlerState,
    ref: str,
    res: CapsuleRefResolution,
    context: CaptureContext | None,
    actor: str,
    now: datetime,
) -> dict[str, Any] | None:
    """One row of a batch answer; None for an unknown resolution status."""
    if res.status != "resolved" or res.id is None:
        return unresolved_item(ref, res)
    sc = _stored_or_none(state, res.id)
    if sc:
        return {
            "requested_ref": ref,
            "status": "found",
            "id": res.id,
            "handle": res.handle,
            "value": _render_capsule_get(state, sc, context, actor, now),
        }
    tomb = state.store.capsules.get_tombstone(res.id)
    if tomb:
        return {
            "requested_ref": ref,
            "status": "tombstoned",
            "id": res.id,
            "handle": res.handle,
            "value": render_tombstone_get(state, tomb, res.id, actor),
        }
    return {"requested_ref": ref, "status": "not_found", "id": res.id}


def _batch_get(
    state: HandlerState,
    ids: list[str],
    context: CaptureContext | None,
    now: datetime,
    actor: str,
) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for ref in ids:
        item = _batch_item(
            state,
            ref,
            state.store.capsules.resolve_capsule_ref(ref),
            context,
            actor,
            now,
        )
        if item is not None:
            items.append(item)
    missing_refs = [i["requested_ref"] for i in items if i["status"] == "not_found"]
    if missing_refs:
        store_mentions = state.store.capsules.capsule_ref_mentions(missing_refs, 8)
        git_report = git_mentions(state.anchor_root, missing_refs)
        for item in items:
            if item.get("status") == "not_found":
                item["mentions"] = mentions_of(
                    item["requested_ref"], store_mentions, git_report, status_first=True
                )
    return verb_result(
        {"label": ADVISORY_LABEL, "framing": DATA_FRAMING, "results": items}
    )


def _memory_get_inner(
    state: HandlerState,
    args: dict[str, Any],
    context: CaptureContext | None,
    now: datetime,
    actor: str,
) -> dict[str, Any]:
    if "ids" in args:
        ids = args["ids"]
        if len(ids) < 1 or len(ids) > 32:
            return {
                "error": {
                    "code": -32602,
                    "message": f"nmemory_capsule_show ids must contain 1..=32 references (got {len(ids)})",
                }
            }
        return _batch_get(state, ids, context, now, actor)

    ref = args.get("id") or args.get("handle") or ""
    res = state.store.capsules.resolve_capsule_ref(ref)
    if res.status == "resolved" and res.id is not None:
        sc = _stored_or_none(state, res.id)
        if sc:
            return verb_result(_render_capsule_get(state, sc, context, actor, now))
        tomb = state.store.capsules.get_tombstone(res.id)
        if tomb:
            return verb_result(render_tombstone_get(state, tomb, res.id, actor))
    elif res.status != "not_found":
        answer = unresolved_outcome(ref, res)
        if answer is None:
            raise StoreError(f"unexpected capsule reference status: {res.status}")
        return verb_result(answer)
    store_mentions = state.store.capsules.capsule_ref_mentions([ref], 8)
    git_report = git_mentions(state.anchor_root, [ref])
    return verb_result(
        {
            "label": ADVISORY_LABEL,
            "framing": DATA_FRAMING,
            "outcome": "not_found",
            "requested_ref": ref,
            "id": res.id,
            "mentions": mentions_of(
                ref, store_mentions, git_report, status_first=False
            ),
        }
    )


def handle(
    state: HandlerState,
    args: dict[str, Any],
    context: CaptureContext | None,
    now: datetime,
    actor: str,
) -> dict[str, Any]:
    # U1 (nmemory-curator): `get` is a serving surface — it records its
    # row in read_traffic with what it served, including what it did not find.
    # Ranking stays with the allowlist (RANKING_SURFACES); this row is
    # arrival, never recall.
    res = _memory_get_inner(state, args, context, now, actor)
    if not isinstance(res, dict) or res.get("isError") or "error" in res:
        # Shape or domain failure: nothing was served to the store.
        return res
    payload = res.get("structuredContent")
    if payload is None:
        try:
            payload = json.loads(res["content"][0]["text"])
        except KeyError, IndexError, TypeError, json.JSONDecodeError:
            payload = {}
    if "results" in payload:
        served = [
            str(item["id"])
            for item in payload["results"]
            if item.get("id") is not None
            and item.get("status") in ("found", "tombstoned")
        ]
        terms = " ".join(str(ref) for ref in args.get("ids", []))
        outcome: str | None = "batch"
    else:
        served = (
            [str(payload["id"])]
            if payload.get("id") is not None
            and payload.get("outcome") in (None, "tombstoned")
            else []
        )
        terms = str(args.get("id") or "")
        outcome = payload.get("outcome")
    record_read_traffic(state, "get", served, terms, None, None, outcome, context, now)
    return res
