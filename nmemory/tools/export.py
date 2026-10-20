"""nmemory_capsule_export: the Markdown projection of the store."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..capture import CaptureContext
from ..consolidate import Tier
from ..export import ExportLive, ExportRelation, ExportTombstoned, render_markdown
from ..queries.sidecars import view_sidecars
from ..store import Store
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
    # The SAME projection that `nmemory capsule export` builds: there were two copies of
    # the same read over the same store, and the N+1 named in #42 lived in
    # BOTH. One implementation, two callers — the CLI inherits the handler's
    # cost guard instead of needing its own.
    md = export_markdown(state.store, no_stamp=not args.get("stamp", True), now=now)
    payload = {
        "label": ADVISORY_LABEL,
        "framing": DATA_FRAMING,
        "markdown": md,
    }
    res = verb_result(payload)
    res["structuredContent"] = payload
    return res


def export_markdown(
    store: Store,
    no_stamp: bool = False,
    now: datetime | None = None,
) -> str:
    from datetime import UTC

    now_dt = now or datetime.now(UTC)
    caps = store.capsules.list()
    # THE SAME N+1 as the MCP handler, in a sibling caller: `nmemory capsule export` builds the
    # same projection over the same store. Fixing only the handler would leave the CLI
    # paying five connections per capsule against the real store.
    sidecars = view_sidecars(store.db)
    live_records: list[ExportLive] = []
    for sc in caps:
        side = sidecars.get(sc.id)
        live_records.append(
            ExportLive(
                id=sc.id,
                seq=sc.seq,
                handle=side.handle if side else sc.id,
                capsule=sc.capsule,
                classification_kind=side.kind if side else None,
                tier=side.tier if side else Tier.ACTIVE,
                review=side.review if side else None,
                anchor_effective=side.anchor_effective if side else None,
            )
        )
    tomb_records = [
        ExportTombstoned(
            id=t.capsule_id,
            handle=sidecars[t.capsule_id].handle
            if t.capsule_id in sidecars
            else t.capsule_id,
            mode=t.mode,
            at=t.at,
            reason=t.reason,
            content_hmac=t.content_hmac,
            provenance_source=t.provenance_source,
            provenance_anchor=t.provenance_anchor,
        )
        for t in store.capsules.list_tombstones()
    ]
    rel_records = [
        ExportRelation(
            kind=r.kind.value,
            from_id=r.from_id,
            to_id=r.to_id,
            reason=r.reason,
            at=r.at,
        )
        for r in store.relations.list_relations()
    ]
    return render_markdown(
        live_records=live_records,
        tombstones=tomb_records,
        relations=rel_records,
        generated_at=now_dt,
        stamp=not no_stamp,
    )
