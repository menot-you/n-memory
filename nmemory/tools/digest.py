"""nmemory_digest_show: the capped census of the store, within a wire ceiling."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any

from ..capsule import (
    format_rfc3339,
    headline_of,
)
from ..capture import CaptureContext
from ..consolidate import (
    ConsolidationRecord,
    Tier,
    archive_candidates,
)
from ..journal import ReplayReport
from ..project import (
    canonical_project,
    in_project_subtree,
)
from ..queries.journal import verify_replay
from ..queries.sidecars import CapsuleSidecar, digest_sidecars
from ..relation import (
    RelationKind,
)
from ..repositories.capsules import StoredCapsule
from ..wire import wire_bytes
from .common import (
    ADVISORY_LABEL,
    DATA_FRAMING,
    HandlerState,
    dag_section,
    open_task_ids,
    record_read_traffic,
    verb_result,
)
from .errors import invalid_params

# nmemory_digest_show: global row cap and response size ceiling.
# DIGEST_HEADLINES_DEFAULT and DIGEST_PINNED_CAP are oracle parity
# (pin d8e0be5ff, src/server/mod.rs:217 and :228): `pinned` has its OWN cap and
# never travels with `headlines`.
DIGEST_HEADLINES_DEFAULT = 10


DIGEST_PINNED_CAP = 20


# The size ceiling does NOT come from the oracle — there it is missing. A row cap limits the
# NUMBER of rows, never their WIDTH, so a high `headlines` on a large
# store still returns half a megabyte at session birth (measured: 560,744
# characters on 2026-08-28).
# ponytail: 64 KiB is the declared ceiling for session start injection; increase only
# when a real consumer proves it reads a larger digest, never because a large
# store can produce it.
DIGEST_MAX_WIRE_BYTES = 64 * 1024


def _fit_digest_wire_ceiling(res: dict[str, Any]) -> None:
    """Shrinks ROW lists until the response fits within DIGEST_MAX_WIRE_BYTES.

    Totals remain intact — they describe the population, never the slice — and the
    trim is DECLARED in `truncated`, because the schema teaches that a list smaller
    than its total is resolved by increasing `headlines`, and here increasing `headlines`
    only makes things worse. Dormant below the ceiling: a small store sees the usual response.
    """
    row_keys = ("newest", "most_recalled", "pinned")

    def row_count(key: str) -> int:
        rows = res.get(key)
        return len(rows) if isinstance(rows, list) else 0

    cut: set[str] = set()
    while wire_bytes(res) > DIGEST_MAX_WIRE_BYTES:
        widest = max(row_keys, key=row_count)
        if row_count(widest) == 0:
            # Only fixed sections remain: there is no row left to cut, and lying
            # about the ceiling would be worse than overflowing declaredly.
            break
        res[widest] = res[widest][: row_count(widest) // 2]
        cut.add(widest)
    if cut:
        res["truncated"] = sorted(cut)


def sidecar_of(sidecars: dict[str, CapsuleSidecar], capsule_id: str) -> CapsuleSidecar:
    """The capsule's sidecar; a missing row reads as an empty one.

    A concurrent capture between the two reads must not crash the whole
    digest; an empty sidecar is what `get_*` returned for a capsule without one.
    """
    return sidecars.get(capsule_id) or CapsuleSidecar(
        tier=Tier.ACTIVE,
        kind=None,
        handle=capsule_id,
        pinned=False,
        recall_count=0,
        last_recalled_at=None,
    )


def headline_row(c: StoredCapsule, sc: CapsuleSidecar) -> dict[str, Any]:
    """The headline row shared by `newest`, `most_recalled`, and `pinned`."""
    row: dict[str, Any] = {
        "id": c.id,
        "handle": sc.handle,
        "project_id": c.capsule.scope.project_id,
        "instruction_taint": c.capsule.instruction_taint,
        "created_at": format_rfc3339(c.created_at),
        "headline": headline_of(c.capsule.content),
    }
    if sc.kind is not None:
        row["kind"] = sc.kind.value
    return row


def project_counts(
    caps: list[StoredCapsule], superseded_ids: set[str]
) -> list[dict[str, Any]]:
    """`by_project`: `count` keeps parity with export (live PLUS superseded), `live` the rest.

    A census whose history weighs more than the present says so instead of
    reading as inventory.
    """
    by_proj: dict[str, tuple[int, int]] = {}
    for c in caps:
        pid = c.capsule.scope.project_id
        count, live = by_proj.get(pid, (0, 0))
        by_proj[pid] = (count + 1, live + (0 if c.id in superseded_ids else 1))
    return [
        {"project_id": p, "count": cnt, "live": live}
        for p, (cnt, live) in sorted(by_proj.items())
    ]


def newest_rows(
    caps: list[StoredCapsule], n: int, sidecars: dict[str, CapsuleSidecar]
) -> list[dict[str, Any]]:
    """Newest-first, sliced to n. Never `caps[-n:]`: with n=0 that is the whole store."""
    rows = []
    for c in list(reversed(caps))[:n]:
        sc = sidecar_of(sidecars, c.id)
        entry = headline_row(c, sc)
        if sc.pinned:
            entry["pinned"] = True
        rows.append(entry)
    return rows


def most_recalled_rows(
    caps: list[StoredCapsule], n: int, sidecars: dict[str, CapsuleSidecar]
) -> tuple[list[dict[str, Any]], int]:
    """The recalled capsules, most recalled first, sliced to n; and their total.

    The same timestamp that goes onto the wire orders the row. `pinned` closes
    the headline and ONLY THEN comes the recall pair: the oracle's field order,
    compared byte-for-byte. Sort BEFORE slicing, least significant key first
    (`sort` is stable): recall_count desc, last_recalled_at desc, seq asc.
    """
    ranked: list[tuple[int, datetime, int, dict[str, Any]]] = []
    for c in caps:
        sc = sidecar_of(sidecars, c.id)
        if sc.recall_count > 0:
            recalled_at = sc.last_recalled_at or c.created_at
            entry = headline_row(c, sc)
            if sc.pinned:
                entry["pinned"] = True
            entry["recall_count"] = sc.recall_count
            entry["last_recalled_at"] = format_rfc3339(recalled_at)
            ranked.append((sc.recall_count, recalled_at, c.seq, entry))
    ranked.sort(key=lambda r: r[2])
    ranked.sort(key=lambda r: r[1], reverse=True)
    ranked.sort(key=lambda r: r[0], reverse=True)
    return [r[3] for r in ranked[:n]], len(ranked)


def pinned_rows(
    caps: list[StoredCapsule], sidecars: dict[str, CapsuleSidecar]
) -> tuple[list[dict[str, Any]], int]:
    """The pinned capsules, newest first, capped at DIGEST_PINNED_CAP (never n); and their total."""
    rows: list[dict[str, Any]] = []
    total = 0
    for c in reversed(caps):
        sc = sidecar_of(sidecars, c.id)
        if not sc.pinned:
            continue
        total += 1
        if len(rows) >= DIGEST_PINNED_CAP:
            continue
        entry = headline_row(c, sc)
        entry["pinned"] = True
        rows.append(entry)
    return rows, total


def consolidation_records(
    caps: list[StoredCapsule],
    superseded_ids: set[str],
    sidecars: dict[str, CapsuleSidecar],
) -> list[ConsolidationRecord]:
    """What the archive COUNTER reads, never the full consolidation plan.

    The near-duplicates section of `plan_consolidation` compares every live
    pair and was 90% of the digest cost at 2,100 capsules;
    `nmemory_capsule_consolidate` serves the complete plan.
    """
    return [
        ConsolidationRecord(
            id=c.id,
            seq=c.seq,
            source_hash=c.capsule.provenance.source_hash,
            content=c.capsule.content,
            instruction_taint=c.capsule.instruction_taint,
            authority_class=c.capsule.authority_class,
            valid_to=c.capsule.freshness.valid_to,
            created_at=c.created_at,
            recall_count=sidecar_of(sidecars, c.id).recall_count,
            is_superseded=c.id in superseded_ids,
            tier=sidecar_of(sidecars, c.id).tier,
            pinned=sidecar_of(sidecars, c.id).pinned,
        )
        for c in caps
    ]


def miss_rows(misses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The recent recall misses as the wire serves them."""
    rows = []
    for m in misses:
        m_at = m.get("at")
        rows.append(
            {
                "at": format_rfc3339(m_at)
                if isinstance(m_at, datetime)
                else str(m_at or ""),
                "outcome": m["outcome"],
                "term": m.get("term", ""),
            }
        )
    return rows


def journal_section(replay: ReplayReport) -> dict[str, Any]:
    """The journal chain's state.

    `verified` and `broken_seq` are MUTUALLY EXCLUSIVE: announcing "verified N"
    alongside `chain: broken` designates as verified the ledger that just
    failed. The count comes from the chain itself.
    """
    section: dict[str, Any] = {"chain": "ok" if replay.chain.is_ok else "broken"}
    if replay.chain.is_ok:
        section["verified"] = replay.chain.count
    else:
        section["broken_seq"] = replay.chain.broken_seq
    section["out_of_band"] = len(replay.out_of_band)
    return section


def handle(
    state: HandlerState,
    args: dict[str, Any],
    context: CaptureContext | None,
    now: datetime,
    actor: str,
) -> dict[str, Any]:
    # The GLOBAL line cap: a single handle for every capped list that the
    # schema names. `pinned` is the oracle's exception — it has its OWN cap.
    n = int(args.get("headlines", DIGEST_HEADLINES_DEFAULT))
    if n < 0:
        # `lista[:-1]` would drop the last row silently, which is worse than rejecting.
        return invalid_params(
            f"nmemory_digest_show headlines must be >= 0, got {n} — it is "
            "the global list cap, and a negative cap would silently "
            "drop rows instead of limiting them"
        )

    # `global_caps` is the LIVE store-global (with proposals), which the readiness
    # projection needs; `caps` is the TRUTH surface, without them.
    global_caps = state.store.capsules.list()
    relations = state.store.relations.list_relations()
    fenced = set(state.store.reviews.list_review_fenced())
    caps = [c for c in global_caps if c.id not in fenced]
    # pin `server/digest.rs`: the CAPSULE sections honor the prefix; the dag,
    # the relation/audit counters, and the open sessions are store-global.
    digest_prefix = args.get("project_prefix")
    if digest_prefix:
        digest_prefix = canonical_project(digest_prefix)
    if digest_prefix:
        caps = [
            c
            for c in caps
            if in_project_subtree(c.capsule.scope.project_id, digest_prefix)
        ]
    superseded_ids = {r.to_id for r in relations if r.kind == RelationKind.SUPERSEDES}
    # EVERYTHING the digest asks per capsule in a single query: per-row
    # `get_*` cost ~8N connections (20,150 in a store of 2,516 capsules).
    sidecars = digest_sidecars(state.store.db)
    replay = verify_replay(state.store.db)
    # Oracle: oldest-open first, tie-breaker by session_id; the COUNT remains
    # exact and only the id list takes the cap.
    open_sessions = [
        s.session_id
        for s in sorted(
            (s for s in state.store.sessions.list_sessions() if not s.finished_at),
            key=lambda s: (s.started_at, s.session_id),
        )
    ]
    # `tiers` is store-global in the oracle (`list_by_tier`): the review fence
    # does not apply here.
    tier_counts = Counter(sidecar_of(sidecars, c.id).tier for c in global_caps)
    newest_list = newest_rows(caps, n, sidecars)
    most_recalled, most_recalled_total = most_recalled_rows(caps, n, sidecars)
    pinned_list, pinned_total = pinned_rows(caps, sidecars)

    # The readiness projection is store-global: the review fence enters as
    # `staged` (a proposal is not open work), and the live map comes from
    # PRE-pruning so that a cross-project member still resolves.
    tombstoned = set(state.store.capsules.list_tombstoned_ids())
    task_ids = set(state.store.classifications.list_ids_of_kind("task"))
    plan_ids = set(state.store.classifications.list_ids_of_kind("plan"))
    epic_ids = set(state.store.classifications.list_ids_of_kind("epic"))
    global_by_id = {c.id: c for c in global_caps}
    dag_wire = dag_section(
        relations,
        tombstoned,
        open_task_ids(task_ids, global_by_id, relations, fenced, now),
        task_ids,
        plan_ids,
        epic_ids,
        n,
    )
    records = consolidation_records(global_caps, superseded_ids, sidecars)
    misses = state.store.traffic.list_recall_misses(limit=10)

    res_dict: dict[str, Any] = {
        "label": ADVISORY_LABEL,
        "framing": DATA_FRAMING,
        "total": len(caps),
        "by_project": project_counts(caps, superseded_ids),
        "newest": newest_list,
        "most_recalled": most_recalled,
        "relations": len(relations),
        "open_sessions": len(open_sessions),
        "open_session_ids": open_sessions[:n],
        "audit_events": state.store.audit.count_audit_events(),
        "recall_misses": len(misses),
        "dag": dag_wire,
        "tiers": {
            "active": tier_counts[Tier.ACTIVE],
            "archived": tier_counts[Tier.ARCHIVED],
            "quarantined": tier_counts[Tier.QUARANTINED],
        },
        "journal": journal_section(replay),
        "archive_candidates": archive_candidates(records, now),
        "archive_scanned": len(records),
    }
    if caps:
        res_dict["newest_total"] = len(caps)
    # Totals are the PRE-PRUNING population and stay outside the cap. Omission
    # is decided by zero TOTAL, never by an empty list.
    if pinned_total:
        res_dict["pinned"] = pinned_list
        res_dict["pinned_total"] = pinned_total
    if most_recalled_total:
        res_dict["most_recalled_total"] = most_recalled_total
    # pin `server/digest.rs`: miss ledger rows have no project attribution;
    # under a prefix the digest WITHHOLDS them instead of leaking.
    if misses and not digest_prefix:
        res_dict["recent_failures"] = {"recall_misses": miss_rows(misses)}

    _fit_digest_wire_ceiling(res_dict)
    # U1 (nmemory-curator): digest is a serving surface — every id in
    # `newest`, `most_recalled`, and `pinned` reached a session. Arrival,
    # never recall (allowlist in store.RANKING_SURFACES).
    _served = [
        str(entry["id"])
        for entry in (*newest_list, *most_recalled, *pinned_list)
        if entry.get("id") is not None
    ]
    record_read_traffic(
        state,
        "digest",
        sorted(dict.fromkeys(_served)),
        digest_prefix or "digest",
        None,
        digest_prefix,
        None,
        context,
        now,
    )

    return verb_result(res_dict)
