"""nmemory_project_bootstrap: the deterministic cold-start pack for a fresh agent."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from ..capsule import (
    headline_of,
)
from ..capture import CaptureContext
from ..classify import (
    CandidateKind,
)
from ..consolidate import (
    Tier,
)
from ..project import (
    canonical_project,
    in_project_subtree,
)
from ..queries.sidecars import CapsuleSidecar, digest_sidecars
from ..relation import (
    RelationKind,
    RelationRecord,
)
from ..repositories.capsules import StoredCapsule
from .common import (
    ADVISORY_LABEL,
    DATA_FRAMING,
    HandlerState,
    dag_section,
    open_task_ids,
    record_read_traffic,
    verb_result,
)

Sections = dict[str, list[dict[str, Any]]]


def orientation_scope(
    global_caps: list[StoredCapsule],
    relations: list[RelationRecord],
    fenced: set[str],
    sidecars: dict[str, CapsuleSidecar],
    project_id: str | None,
    prefix: str | None,
    now: datetime,
) -> set[str]:
    """The ids every section and `ready` may serve.

    The two fences that `retrieve()` evaluates FIRST, here and not
    per-section: quarantined and falsified, read from memory (no N+1).
    `archived` is not added on purpose: a degraded trap remains a warning
    (`test_scope_terms_lifecycle_and_usage`).
    """
    quarantined = {cid for cid, sc in sidecars.items() if sc.tier is Tier.QUARANTINED}
    falsified = {
        r.to_id for r in relations if r.kind is RelationKind.FALSIFIES and r.at <= now
    }
    return {
        c.id
        for c in global_caps
        if c.id not in fenced
        and c.id not in quarantined
        and c.id not in falsified
        and (project_id is None or c.capsule.scope.project_id == project_id)
        and (prefix is None or in_project_subtree(c.capsule.scope.project_id, prefix))
    }


def pack_row(
    c: StoredCapsule,
    sidecars: dict[str, CapsuleSidecar],
    superseded: set[str],
    now: datetime,
) -> dict[str, Any]:
    """One orientation row."""
    sc = sidecars.get(c.id)
    return {
        "id": c.id,
        "headline": headline_of(c.capsule.content),
        "instruction_taint": c.capsule.instruction_taint,
        "tier": sc.tier.value if sc else Tier.ACTIVE.value,
        "superseded": c.id in superseded,
        "expired": c.capsule.freshness.valid_to is not None
        and c.capsule.freshness.valid_to < now,
    }


def guidance_sections(
    global_caps: list[StoredCapsule],
    matches: set[str],
    sidecars: dict[str, CapsuleSidecar],
    superseded: set[str],
    now: datetime,
) -> Sections:
    """Constraints, decisions and traps, newest first; `ready` is filled later.

    A trap stays whatever its currency; a constraint must be active, and
    neither a constraint nor a decision is served expired or superseded.
    """
    sections: Sections = {"constraints": [], "ready": [], "decisions": [], "traps": []}
    for c in reversed(global_caps):
        if c.id not in matches:
            continue
        sc = sidecars.get(c.id)
        if sc is None:
            continue
        entry = pack_row(c, sidecars, superseded, now)
        if sc.kind is CandidateKind.FAILURE_PATTERN:
            sections["traps"].append(entry)
        elif not entry["expired"] and not entry["superseded"]:
            if sc.kind is CandidateKind.CONSTRAINT and sc.tier is Tier.ACTIVE:
                sections["constraints"].append(entry)
            elif sc.kind is CandidateKind.DECISION:
                sections["decisions"].append(entry)
    return sections


def render_pack(
    count: int,
    sections: Sections,
    dag: dict[str, Any],
    ready_total: int,
    sidecars: dict[str, CapsuleSidecar],
    budget: int,
) -> dict[str, Any]:
    """The pack holding the first `count` rows in section priority, with its own token count.

    `used_tokens` counts the serialized payload including this counter itself.
    """
    ordered = [(key, entry) for key, entries in sections.items() for entry in entries]
    selected: Sections = {key: [] for key in sections}
    ids = set(dag.get("cycle", []))
    for key, entry in ordered[:count]:
        selected[key].append(entry)
        ids.add(entry["id"])
    ready = (
        dag
        if dag["status"] == "cycle"
        else {
            "status": "ok",
            "ready": selected["ready"],
            "ready_total": ready_total,
            "next_action": selected["ready"][0]["headline"]
            if selected["ready"]
            else None,
        }
    )
    result: dict[str, Any] = {
        "label": ADVISORY_LABEL,
        "framing": DATA_FRAMING,
        "constraints": selected["constraints"],
        "constraints_total": len(sections["constraints"]),
        "ready": ready,
        "decisions": selected["decisions"],
        "decisions_total": len(sections["decisions"]),
        "traps": selected["traps"],
        "traps_total": len(sections["traps"]),
        "handles": {
            cid: sidecars[cid].handle if cid in sidecars else cid for cid in sorted(ids)
        },
        "budget": {
            "token_budget": budget,
            "trimmed_by_budget": len(ordered) - count,
            "used_tokens": 0,
        },
    }
    while True:
        used = (len(json.dumps(result, ensure_ascii=False)) + 3) // 4
        if result["budget"]["used_tokens"] == used:
            return result
        result["budget"]["used_tokens"] = used


def fit_pack(
    sections: Sections,
    dag: dict[str, Any],
    ready_total: int,
    sidecars: dict[str, CapsuleSidecar],
    budget: int,
) -> dict[str, Any] | int:
    """The largest pack within `budget`, or the minimum it needs when even none fits.

    Section priority is preserved and the longest prefix that fits is chosen by
    binary search, bounding serialization to O(N log N); every omitted row is
    counted in `trimmed_by_budget`.
    """
    minimum = render_pack(0, sections, dag, ready_total, sidecars, budget)
    if minimum["budget"]["used_tokens"] > budget:
        return int(minimum["budget"]["used_tokens"])
    low, high = 0, sum(len(entries) for entries in sections.values())
    while low < high:
        mid = (low + high + 1) // 2
        probe = render_pack(mid, sections, dag, ready_total, sidecars, budget)
        if probe["budget"]["used_tokens"] <= budget:
            low = mid
        else:
            high = mid - 1
    return render_pack(low, sections, dag, ready_total, sidecars, budget)


def handle(
    state: HandlerState,
    args: dict[str, Any],
    context: CaptureContext | None,
    now: datetime,
    actor: str,
) -> dict[str, Any]:
    budget = int(args.get("token_budget", 1500))
    # U4: bootstrap is a DELIVERY surface in the census — a client in the old
    # spelling would lose reach right at startup.
    project_id = args.get("project_id")
    if project_id is not None:
        project_id = canonical_project(project_id)
    prefix = args.get("project_prefix")
    if prefix is not None:
        prefix = canonical_project(prefix)
    global_caps = state.store.capsules.list()
    relations = state.store.relations.list_relations()
    fenced = set(state.store.reviews.list_review_fenced())
    sidecars = digest_sidecars(state.store.db)
    global_by_id = {c.id: c for c in global_caps}
    scoped = orientation_scope(
        global_caps, relations, fenced, sidecars, project_id, prefix, now
    )
    task_ids = set(state.store.classifications.list_ids_of_kind("task"))
    dag = dag_section(
        relations,
        set(state.store.capsules.list_tombstoned_ids()),
        open_task_ids(task_ids, global_by_id, relations, fenced, now),
        task_ids,
        set(state.store.classifications.list_ids_of_kind("plan")),
        set(state.store.classifications.list_ids_of_kind("epic")),
        len(global_caps),
    )
    superseded = {r.to_id for r in relations if r.kind is RelationKind.SUPERSEDES}

    # Text narrows guidance only. Readiness must still see cross-project
    # blockers, and orientation must never increment recall usage.
    matches = scoped
    if args.get("terms"):
        matches = scoped & {
            c.id
            for c, _ in state.store.capsules.search_fts_scoped(
                args["terms"], project_id=project_id, project_prefix=prefix
            )
        }
    sections = guidance_sections(global_caps, matches, sidecars, superseded, now)
    ready_ids = [cid for cid in dag.get("ready", []) if cid in scoped]
    sections["ready"] = [
        pack_row(global_by_id[cid], sidecars, superseded, now) for cid in ready_ids[:10]
    ]
    served = fit_pack(sections, dag, len(ready_ids), sidecars, budget)
    if isinstance(served, int):
        return {
            "error": {
                "code": -32602,
                "message": (
                    f"nmemory_project_bootstrap token_budget={budget} cannot hold the required "
                    f"sections and diagnostics; need at least {served}"
                ),
            }
        }
    # The binary search renders the pack many times to SIZE it; only the pack
    # the caller receives is recorded as a read.
    record_read_traffic(
        state,
        "bootstrap",
        sorted(served.get("handles", {})),
        " ".join(str(t) for t in (args.get("terms") or [])),
        project_id,
        prefix,
        None,
        context,
        now,
    )
    return verb_result(served)
