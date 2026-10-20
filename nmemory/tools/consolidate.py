"""nmemory_capsule_consolidate: the consolidation plan, and optionally its tier moves."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..capture import CaptureContext
from ..consolidate import (
    ConsolidationRecord,
    Tier,
    alias_proposals,
    folded_vocabulary,
    plan_consolidation,
)
from ..queries.sidecars import digest_sidecars, recall_mark
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
    apply_tiers = bool(args.get("apply_tiers", False))
    # Before the counts the plan reads: what lands after it is re-read under the lock.
    mark = recall_mark(state.store.db)
    caps = state.store.capsules.list()
    # A single batch pass, the embeddings worker pattern: sidecars (which already
    # carry recall_count/last/tier/pinned with the new signal from #434) and
    # superseded in TWO queries, instead of 4 queries PER CAPSULE. Measured in the
    # #434 reviewer: 0.35 ms × N capsules grew linearly with read_traffic.
    sidecars = digest_sidecars(state.store.db)
    superseded = state.store.relations.superseded_among([c.id for c in caps])
    supersession_archives = state.store.tiers.supersession_archives(
        [cid for cid, sc in sidecars.items() if sc.tier is Tier.ARCHIVED]
    )
    records: list[ConsolidationRecord] = []
    for stored in caps:
        sc = sidecars.get(stored.id)
        pinned = sc.pinned if sc else False
        tier = sc.tier if sc else Tier.ACTIVE
        recall_count = sc.recall_count if sc else 0
        is_superseded = stored.id in superseded
        records.append(
            ConsolidationRecord(
                id=stored.id,
                seq=stored.seq,
                source_hash=stored.capsule.provenance.source_hash,
                content=stored.capsule.content,
                instruction_taint=stored.capsule.instruction_taint,
                authority_class=stored.capsule.authority_class,
                valid_to=stored.capsule.freshness.valid_to,
                created_at=stored.created_at,
                recall_count=recall_count,
                is_superseded=is_superseded,
                tier=tier,
                pinned=pinned,
                supersession_archive=stored.id in supersession_archives,
            )
        )

    considered = len(records)
    plan = plan_consolidation(records, now)

    misses = [(t, cnt) for t, cnt in state.store.traffic.recall_miss_terms()]
    vocabulary = folded_vocabulary(records)
    aliases = state.store.aliases.list_aliases()
    taught = {a[0] for a in aliases}
    plan.alias_proposals = alias_proposals(misses, vocabulary, taught)

    applied_tier_moves = 0
    if apply_tiers and plan.tier_moves:
        applied_tier_moves = len(
            state.store.apply_tier_moves(records, plan.tier_moves, mark, actor, now)
        )

    handle_ids: set[str] = set()
    for d in plan.exact_dupes:
        handle_ids.add(d.keep)
        handle_ids.add(d.drop)
    for p in plan.merge_proposals:
        handle_ids.update(p.ids)
    for tm in plan.tier_moves:
        handle_ids.add(tm.id)

    # The handle already comes in the sidecar (one query in the batch above): N+1 of get_handle
    # per plan item would be the same query repeated.
    handles = {
        cid: sidecars[cid].handle for cid in sorted(handle_ids) if cid in sidecars
    }

    res_dict: dict[str, Any] = {
        "label": ADVISORY_LABEL,
        "framing": DATA_FRAMING,
        "considered": considered,
        "plan": plan.to_dict(),
        "store_invariant_breach": plan.has_store_invariant_breach,
        "applied": apply_tiers,
        "applied_tier_moves": applied_tier_moves,
    }
    if handles:
        res_dict["handles"] = handles

    return verb_result(res_dict)
