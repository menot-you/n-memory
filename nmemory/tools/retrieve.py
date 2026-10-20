"""nmemory_capsule_retrieve: recall with three honest outcomes and every fence validated."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..capsule import (
    parse_rfc3339,
)
from ..capture import CaptureContext
from ..classify import (
    CandidateKind,
)
from ..project import (
    canonical_project,
    expands_to_subtree_by_default,
)
from ..queries.standing import standing_of
from ..relation import (
    RelationKind,
)
from ..repositories.capsules import CapsuleNotFound, Tombstoned
from ..retrieve import (
    DEFAULT_UNREAD_DAYS,
    RetrieveParams,
    order_exclusions,
    retrieve,
)
from .common import (
    ADVISORY_LABEL,
    DATA_FRAMING,
    HandlerState,
    record_read_traffic,
    verb_result,
)
from .errors import invalid_params

#: Ceiling for `vector_k` at the port. See the refusal in `handle`.
VECTOR_K_MAX = 10_000


def _fence_label(project_id: str | None, project_prefix: str | None) -> str:
    """pin `retrieve.rs:2530` — an empty retrieval blames the FENCE, never the terms.

    With `project_id` alone the text is byte-identical to the previous one;
    it is the prefix-only case that lacked a name.
    """
    match (project_id, project_prefix):
        case (str() as proj, str() as prefix):
            return f" within project '{proj}' and subtree '{prefix}'"
        case (str() as proj, None):
            return f" within project '{proj}'"
        case (None, str() as prefix):
            return f" within project subtree '{prefix}'"
        case _:
            return ""


def resolve_scope(
    requested_project_id: str | None,
    requested_project_prefix: str | None,
    topic_id: object,
    effort_id: object,
    default_project: str | None,
    default_source: str,
) -> tuple[str | None, str | None, dict[str, Any]]:
    """The fence the call runs under: `(project_id, project_prefix, scope)`.

    Whoever passes explicit `project_id` or `project_prefix` chose the fence.
    On omission, U2a (nmemory-curator) makes the SAME decision that
    `nmemory_context_show` already made (`subtree_default`): the server
    default is the AUDIENCE, and the audience is the tree, so `pynott` reaches
    `pynott/tech` (measured in the live store 2026-09-18: 88 capsules under
    `pynott/*` that an EXACT omission did not read).

    `effort_id` stays out of the subtree default alongside `topic_id`: with it,
    omission continues to mean the NODE, as it did before, so children outside
    the effort cannot crowd the vector lane before `outside_effort` runs. A
    mixed bucket root (`SUBTREE_DEFAULT_DENY`) keeps the NODE too; without that
    branch the default fell to `None` and the call had no fence at all.
    """
    proj_id = requested_project_id
    proj_prefix = requested_project_prefix
    if (
        proj_id is None
        and proj_prefix is None
        and topic_id is None
        and effort_id is None
        and expands_to_subtree_by_default(default_project)
    ):
        proj_prefix = default_project
    elif proj_id is None and proj_prefix is None and topic_id is None:
        proj_id = default_project
    scope = {
        "project_id": proj_id if proj_id is not None else proj_prefix,
        "match": "prefix" if proj_id is None else "exact",
        "source": (
            "caller"
            if requested_project_id is not None or requested_project_prefix is not None
            else default_source
        ),
    }
    return proj_id, proj_prefix, scope


def ranking_knobs(args: dict[str, Any]) -> dict[str, Any] | str:
    """The validated numeric fences and blends as `RetrieveParams` fields, or a refusal.

    Each value is refused by VALUE on its own scale, never coerced, in the order
    the port has always checked them.

    `include_unread` is the escape hatch of the read fence and `unread_days`
    only REFINES its threshold: `null` on the wire is indistinguishable from
    "sent nothing" after `args.get`.

    A negative `vector_k` once reached the database as `LIMIT -1` and the
    vector lane swallowed it: the lane vanished and the caller was told that
    nothing matched. Its ceiling exceeds any realistic candidate set and stays
    well below bigint.

    The two relevance floors are not one domain: BM25 is a sum of positive
    terms, so a NEGATIVE term floor is scale confusion (OFF is `0.0`); cosine
    lives in `[-1, 1]`, so a negative vector floor is legal (and OFF) and what
    no score reaches is `> 1.0`. `inf` is not refused: a floor nothing clears
    serves `missing_evidence` with `below_relevance_floor` COUNTED. `nan` dies
    in the range comparisons. The margin gate follows
    the term floor's discipline, with `None` as OFF.
    """
    unread_days: int | None = None
    if not bool(args.get("include_unread", False)):
        unread_arg = args.get("unread_days")
        unread_days = int(unread_arg) if unread_arg is not None else DEFAULT_UNREAD_DAYS
        if unread_days < 0:
            return "unread_days must be >= 0"
    min_confidence: float | None = None
    if (min_conf_arg := args.get("min_confidence")) is not None:
        min_confidence = float(min_conf_arg)
        if not (0.0 <= min_confidence <= 1.0):
            return "min_confidence must be in 0.0..=1.0"
    term_floor = 0.0
    if (term_floor_arg := args.get("term_floor")) is not None:
        term_floor = float(term_floor_arg)
        if not term_floor >= 0.0:
            return "term_floor must be >= 0.0 (BM25 scale; 0.0 is OFF)"
    vector_floor = 0.0
    if (vector_floor_arg := args.get("vector_floor")) is not None:
        vector_floor = float(vector_floor_arg)
        if not (-1.0 <= vector_floor <= 1.0):
            return "vector_floor must be in -1.0..=1.0 (cosine scale; <= 0.0 is OFF)"
    min_term_margin: float | None = None
    if (margin_arg := args.get("min_term_margin")) is not None:
        min_term_margin = float(margin_arg)
        if not min_term_margin >= 0.0:
            return "min_term_margin must be >= 0.0 (BM25 scale; absent/null is OFF)"
    vector_k: int | None = None
    if (vector_k_arg := args.get("vector_k")) is not None:
        vector_k = int(vector_k_arg)
        if not (0 <= vector_k <= VECTOR_K_MAX):
            return f"vector_k must be in 0..={VECTOR_K_MAX}"
    weight_blend: float | None = None
    if (weight_blend_arg := args.get("weight_blend")) is not None:
        weight_blend = float(weight_blend_arg)
        if not (0.0 <= weight_blend <= 1.0):
            return "weight_blend must be in 0.0..=1.0"
    return {
        "unread_days": unread_days,
        "min_confidence": min_confidence,
        "term_floor": term_floor,
        "vector_floor": vector_floor,
        "min_term_margin": min_term_margin,
        "vector_k": vector_k,
        "weight_blend": weight_blend,
    }


def time_refusal(known_at: Any, time_window: Any) -> str | None:
    """The refusal of a malformed `known_at` or `time_window`, or None.

    M1: a malformed bound must REJECT, never widen the fence — the parser once
    raised inside retrieve.py, `pass` swallowed it, the bound remained None and
    the caller received MORE than requested.
    """
    if known_at is not None:
        try:
            parse_rfc3339(known_at)
        except ValueError, TypeError:
            return f"known_at must be an RFC3339 string: {known_at}"
    if time_window is None:
        return None
    if not isinstance(time_window, dict) or not (
        time_window.get("from") or time_window.get("to")
    ):
        return "time_window requires at least one bound ('from' or 'to')"
    for bound in ("from", "to"):
        raw = time_window.get(bound)
        if raw is None:
            continue
        try:
            parse_rfc3339(raw)
        except ValueError, TypeError:
            return f"time_window['{bound}'] must be an RFC3339 string: {raw}"
    return None


def _members_of(
    state: HandlerState, name: str, ref: Any, kind: RelationKind, *, epic: bool
) -> tuple[str, list[str], bool] | str:
    """Resolve an effort (`epic`) or topic reference to `(id, member set, open)`, or a refusal.

    The member set holds the incoming `kind` edges and the node itself. An
    effort must be classified `epic` and have at least one member; it is open
    while nothing `witnesses` it.
    """
    ref_res = state.store.capsules.resolve_capsule_ref(ref)
    if ref_res.status != "resolved" or not ref_res.id:
        return f"{name} '{ref}' cannot be resolved"
    try:
        state.store.capsules.get(ref_res.id)
    except Tombstoned:
        return f"{name} '{ref}' is tombstoned"
    except CapsuleNotFound:
        return f"{name} '{ref}' not found"
    if epic:
        cls = state.store.classifications.get_classification(ref_res.id)
        if not cls or cls.kind != "epic":
            return (
                f"effort_id must name a capsule classified kind=epic "
                f"(got {cls.kind.value if cls else 'none'})"
            )
    rels = state.store.relations.list_relations_for(ref_res.id)
    members = {r.from_id for r in rels if r.to_id == ref_res.id and r.kind == kind}
    if epic and len(members) == 0:
        return f"effort_id '{ref}' has no part_of members"
    is_open = not any(
        r.to_id == ref_res.id and r.kind == RelationKind.WITNESSES for r in rels
    )
    return ref_res.id, list(members | {ref_res.id}), is_open


def abstain_envelope(
    res: dict[str, Any], term_count: int, scope: dict[str, Any], fence: str
) -> dict[str, Any]:
    """The `abstain` answer. F2a: the reason names what RAN, never what did not run.

    Every abstain once said "no stored capsule matched any of the N query
    term(s)" even when `lane="vector"` disabled the term lane (measured in the
    live store 2026-09-12). An EARLY abstain (empty query) has no lane: `term`
    is the historical value and the only honest one for "no routing".
    """
    lanes_run: list[str] = list(res.get("lanes_run", []))
    if "term" in lanes_run:
        cause = f"no stored capsule matched any of the {term_count} query term(s)"
    elif lanes_run:
        cause = (
            f"the {'/'.join(lanes_run)} lane ran and matched no stored "
            "capsule (the term lane did not run)"
        )
    else:
        cause = "no lane ran"
    envelope = {
        "lane": res.get("lane", "term"),
        "lane_used": res.get("lane_used", "term"),
        "lanes_run": lanes_run,
        "outcome": "abstain",
        "scope": scope,
        "reason": f"{cause}{fence}; abstaining instead of fabricating",
    }
    if res.get("degraded"):
        envelope["degraded"] = True
        envelope["lane_error"] = res["lane_error"]
    if "lane_note" in res:
        envelope["lane_note"] = res["lane_note"]
    return envelope


def missing_envelope(
    res: dict[str, Any], scope: dict[str, Any], fence: str
) -> dict[str, Any]:
    """The `missing_evidence` answer — pin `retrieve.rs:3228 fn missing_evidence`.

    "Evidence exists and NONE can substantiate" is its own outcome, never an
    empty-handed `grounded`. The pin's reach clause has three branches because
    there a tombstoned id COUNTS as an exclusion; here the engine skips it
    without counting it, so only this branch can fire.
    """
    excluded = order_exclusions(dict(res.get("exclusions", {})))
    total = int(res.get("total_candidates", 0))
    detail = ", ".join(f"{n} {reason}" for reason, n in excluded.items())
    envelope = {
        "lane": res["lane"],
        "lane_used": res["lane_used"],
        "lanes_run": list(res.get("lanes_run", [])),
        "outcome": "missing_evidence",
        "scope": scope,
        "excluded_count": total,
        "excluded": excluded,
        "reason": (
            f"{total} capsule(s) matched"
            f"{fence} but every one is "
            f"excluded from recall ({detail}); reporting missing "
            "evidence instead of recalling ineligible capsules "
            "(they remain reachable via get/list)"
        ),
    }
    if res.get("degraded"):
        envelope["degraded"] = True
        envelope["lane_error"] = res["lane_error"]
    if "lane_note" in res:
        envelope["lane_note"] = res["lane_note"]
    return envelope


def result_item(
    ev: dict[str, Any],
    provenance: dict[str, Any],
    capture_context: dict[str, Any] | None,
    effort_id: str | None,
    topic_id: str | None,
) -> dict[str, Any]:
    """One served result of a `grounded` answer, built from the engine's evidence."""
    cid = ev["id"]
    item: dict[str, Any] = {
        "label": ADVISORY_LABEL,
        "framing": DATA_FRAMING,
        "id": cid,
        "headline": ev["headline"],
        "instruction_taint": ev["instruction_taint"],
        "authority_class": ev["authority_class"],
        "confidence": ev["confidence"],
        "decayed_weight": ev["decayed_weight"],
        "provenance": provenance,
        "anchor_live": ev["anchor_live"],
        "anchor_drift": ev["anchor_drift"],
        "freshness": {
            "valid_from": ev["valid_from"],
            "valid_to": ev["valid_to"],
        },
        "matched_terms": ev["matched_terms"],
        "capture_context": capture_context,
        "relevance": ev["relevance_score"],
        "bm25": ev["bm25"],
    }
    if ev.get("anchor_effective") is not None:
        item["anchor_effective"] = ev["anchor_effective"]
    if ev.get("source_quote") is not None:
        item["source_quote"] = ev["source_quote"]
    if effort_id is not None:
        item["effort_role"] = "epic" if cid == effort_id else "member"
    if topic_id is not None:
        item["topic_role"] = "topic" if cid == topic_id else "member"
    return item


def grounded_envelope(
    res: dict[str, Any],
    scope: dict[str, Any],
    results: list[dict[str, Any]],
    handles: dict[str, str],
    effort: tuple[str, list[str], bool] | None,
    topic: tuple[str, list[str], bool] | None,
) -> dict[str, Any]:
    """The `grounded` answer. B2 — each counter comes from the ENGINE, never a literal."""
    excluded = order_exclusions(dict(res.get("exclusions", {})))
    envelope = {
        "lane": res["lane"],
        "lane_used": res["lane_used"],
        "lanes_run": list(res.get("lanes_run", [])),
        "outcome": "grounded",
        "scope": scope,
        "results": results,
        "matched": res["matched"],
        "returned": res["returned"],
        "trimmed": res["trimmed"],
        "trimmed_by_limit": res["trimmed_by_limit"],
        "trimmed_by_budget": res["trimmed_by_budget"],
        "token_budget": res["token_budget"],
        "receipt_id": res["receipt_id"],
        "handles": handles,
        "excluded": excluded,
        "excluded_count": sum(excluded.values()),
    }
    if res.get("degraded"):
        envelope["degraded"] = True
        envelope["lane_error"] = res["lane_error"]
    if "lane_note" in res:
        envelope["lane_note"] = res["lane_note"]
    if effort is not None:
        envelope["effort"] = {
            "epic_id": effort[0],
            "member_total": len(effort[1]) - 1,
            "open": effort[2],
        }
    if topic is not None:
        envelope["topic"] = {
            "topic_id": topic[0],
            "member_total": len(topic[1]) - 1,
        }
    return envelope


def handle(
    state: HandlerState,
    args: dict[str, Any],
    context: CaptureContext | None,
    now: datetime,
    actor: str,
) -> dict[str, Any]:
    terms_arg = args.get("terms") or args.get("query")
    if not terms_arg:
        return invalid_params("nmemory_capsule_retrieve requires terms")

    terms = terms_arg if isinstance(terms_arg, list) else [terms_arg]
    exclude_kinds = args.get("exclude_kinds")
    if exclude_kinds is not None and any(
        kind not in {k.value for k in CandidateKind} for kind in exclude_kinds
    ):
        return invalid_params("exclude_kinds must name known capsule kinds")
    # U4 (nmemory-curator): the reader in the old spelling continues to reach —
    # folding never removes reach; the prefix folds like `list`'s does.
    requested_project_id = args.get("project_id")
    if requested_project_id is not None:
        requested_project_id = canonical_project(requested_project_id)
    requested_project_prefix = args.get("project_prefix")
    if requested_project_prefix is not None:
        requested_project_prefix = canonical_project(requested_project_prefix)
    topic_id_arg = args.get("topic_id")
    effort_id_arg = args.get("effort_id")
    defaults = state.effective_defaults(context)
    proj_id, proj_prefix, scope = resolve_scope(
        requested_project_id,
        requested_project_prefix,
        topic_id_arg,
        effort_id_arg,
        defaults.project_id,
        defaults.source,
    )
    limit = int(args.get("limit", 10))
    token_budget_arg = args.get("token_budget")
    token_budget = int(token_budget_arg) if token_budget_arg is not None else None
    lane = args.get("lane")
    knobs = ranking_knobs(args)
    if isinstance(knobs, str):
        return invalid_params(knobs)

    session_id = args.get("session_id")
    if session_id is not None and (
        not isinstance(session_id, str) or not session_id.strip()
    ):
        return invalid_params("session_id cannot be empty or whitespace only")

    effort: tuple[str, list[str], bool] | None = None
    if effort_id_arg is not None:
        resolved = _members_of(
            state, "effort_id", effort_id_arg, RelationKind.PART_OF, epic=True
        )
        if isinstance(resolved, str):
            return invalid_params(resolved)
        effort = resolved
    topic: tuple[str, list[str], bool] | None = None
    if topic_id_arg is not None:
        resolved = _members_of(
            state, "topic_id", topic_id_arg, RelationKind.ABOUT, epic=False
        )
        if isinstance(resolved, str):
            return invalid_params(resolved)
        topic = resolved
    known_at = args.get("known_at")
    time_window = args.get("time_window")
    if (refusal := time_refusal(known_at, time_window)) is not None:
        return invalid_params(refusal)

    res = retrieve(
        state.store,
        RetrieveParams(
            query=" ".join(terms),
            terms=terms,
            project_id=proj_id,
            project_prefix=proj_prefix,
            limit=limit,
            anchor_root=state.anchor_root,
            # The escape hatches of the staged and lapsed fences.
            staged=bool(args.get("include_staged", False)),
            lapsed=bool(args.get("include_lapsed", False)),
            lane=lane,
            lanes=[lane] if lane else None,
            token_budget=token_budget,
            time_window=time_window,
            known_at=known_at,
            session_id=session_id,
            effort_id=effort[0] if effort else None,
            effort_set=effort[1] if effort else None,
            topic_id=topic[0] if topic else None,
            topic_set=topic[1] if topic else None,
            exclude_kinds=exclude_kinds,
            **knobs,
        ),
        embeddings=state.embeddings,
        now=now,
        asked_by_session=context.session_id if context else None,
        count=not state.viewer,
    )

    # Retrieve is counted HERE, not inside `retrieve()`: the engine returns
    # early on `abstain` and `missing_evidence`, long before its receipt, and
    # counting only the grounded half would undercount the miss volume — the
    # demand signal this table exists to expose. BOTH project columns take the
    # RESOLVED fence (U2a), as `context` does: the raw argument would file
    # every omission read against no project.
    record_read_traffic(
        state,
        "retrieve",
        [str(e.get("id")) for e in res.get("evidence", []) if e.get("id")],
        " ".join(terms),
        proj_id if proj_id is not None else proj_prefix,
        proj_prefix,
        str(res.get("outcome")),
        context,
        now,
    )

    fence = _fence_label(proj_id, proj_prefix)
    if res["outcome"] == "abstain":
        return verb_result(abstain_envelope(res, len(terms), scope, fence))
    if res["outcome"] == "missing_evidence":
        return verb_result(missing_envelope(res, scope, fence))

    results: list[dict[str, Any]] = []
    handles: dict[str, str] = {}
    for ev in res.get("evidence", []):
        cid = ev["id"]
        handles[cid] = ev["handle"]
        sc = state.store.capsules.get(cid)
        if sc is None:
            raise CapsuleNotFound(f"capsule {cid} disappeared during recall")
        ctx = state.store.capsules.get_capture_context(cid)
        results.append(
            result_item(
                ev,
                {
                    "source": sc.capsule.provenance.source,
                    "anchor": sc.capsule.provenance.anchor,
                    "source_hash": sc.capsule.provenance.source_hash,
                },
                ctx.to_dict() if ctx else None,
                effort[0] if effort else None,
                topic[0] if topic else None,
            )
        )

    # U3: nothing is served without standing (dod/task stay out by design).
    # THE CLOCK IS BELIEF TIME, the clock of the `lapsed` fence inside
    # `retrieve`: recalculating with `now` made the envelope CONTRADICT the
    # fence under a past `known_at` (independent review, 2026-09-20).
    retrieve_standing = standing_of(
        state.store.db,
        [r["id"] for r in results],
        now=parse_rfc3339(known_at) if known_at is not None else now,
    )
    for item in results:
        if item["id"] in retrieve_standing:
            item["standing"] = retrieve_standing[item["id"]]
    return verb_result(grounded_envelope(res, scope, results, handles, effort, topic))
