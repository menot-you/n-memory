"""nmemory_capsule_import: capture a bridge source (CLAUDE.md, AGENTS.md, a memory dir).

The module name carries a trailing underscore because `import` is a keyword."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from .. import audit_actions
from ..bridge import (
    BridgeCandidate,
    BridgeIoError,
    BridgeNotADirectory,
    BridgeSource,
    BridgeSourceMissing,
    BridgeSymlinkRejected,
    import_block_source_key,
    read_source,
)
from ..capsule import (
    AuthorityClass,
    sha256_hex,
)
from ..capture import CaptureContext
from ..ingest import (
    IngestOutcome,
    IngestRequest,
    ingest,
)
from ..relation import (
    RelationKind,
    RelationOrigin,
)
from ..repositories.base import StoreError
from ..store import Store
from .common import (
    HandlerState,
    error_result,
    taint_summary,
    verb_result,
)


def climb_import_supersession_chain(
    store: Store, capsule_id: str, start_head: str
) -> str | None:
    head = start_head
    visited: set[str] = {capsule_id}
    while True:
        if head in visited:
            return None
        visited.add(head)
        ups = [
            r
            for r in store.relations.list_relations_for(head)
            if r.kind == RelationKind.SUPERSEDES and r.to_id == head
        ]
        if not ups:
            break
        if len(ups) != 1 or ups[0].origin != RelationOrigin.IMPORT:
            return None
        head = ups[0].from_id
    return head


def supersede_if_unowned_elsewhere(
    store: Store,
    actor: str,
    source_key: str,
    old_id: str,
    new_id: str,
    reason: str,
    now: datetime,
) -> bool:
    owners = store.sources.import_block_owners(old_id)
    if any(owner != source_key for owner in owners):
        return False
    store.add_relation(
        RelationKind.SUPERSEDES, from_id=new_id, to_id=old_id, origin="import", now=now
    )
    store.audit.append_audit(actor, audit_actions.IMPORT, new_id, reason, now)
    return True


def apply_import_supersession(
    store: Store,
    block_metas: list[tuple[str, str, int]],
    outcomes: list[dict[str, Any]],
    actor: str,
    now: datetime,
) -> None:
    new_by_source: dict[str, list[tuple[str, str, int, bool]]] = {}
    for (source_key, block_hash, ordinal), outcome in zip(block_metas, outcomes):
        if outcome.get("status") == "rejected":
            continue
        cid = outcome.get("id")
        if not cid:
            continue
        is_fresh = outcome.get("status") == "captured"
        new_by_source.setdefault(source_key, []).append(
            (block_hash, cid, ordinal, is_fresh)
        )

    # Build passes
    passes: list[dict[str, Any]] = []
    for source_key, new_blocks in new_by_source.items():
        new_hashes = {h for (h, _, _, _) in new_blocks}
        old_rows = store.sources.import_blocks_for(source_key)
        old_hashes = {r.block_hash for r in old_rows}
        changed_away = [r for r in old_rows if r.block_hash not in new_hashes]
        changed_away.sort(key=lambda r: (r.ordinal, r.block_hash))
        passes.append(
            {
                "source_key": source_key,
                "new_blocks": new_blocks,
                "old_hashes": old_hashes,
                "changed_away": changed_away,
            }
        )

    # Phase 1: Global adoption
    for p in passes:
        source_key = p["source_key"]
        for block_hash, capsule_id, ordinal, is_fresh in p["new_blocks"]:
            already_owned = len(store.sources.import_block_owners(capsule_id)) > 0
            if is_fresh or already_owned:
                store.record_import_block(
                    source_key, block_hash, capsule_id, ordinal, now
                )

    # Phase 2: Global forget
    for p in passes:
        source_key = p["source_key"]
        for old in p["changed_away"]:
            store.sources.forget_import_block(source_key, old.block_hash)

    # Phase 3: Revive & equal-count supersede
    for p in passes:
        source_key = p["source_key"]
        consumed_changed_away: set[str] = set()

        # 3a. Revive
        for block_hash, capsule_id, ordinal, _ in p["new_blocks"]:
            if not store.relations.is_superseded(capsule_id):
                continue
            direct = [
                r
                for r in store.relations.list_relations_for(capsule_id)
                if r.kind == RelationKind.SUPERSEDES and r.to_id == capsule_id
            ]
            if len(direct) != 1:
                continue
            direct_edge_from = direct[0].from_id
            head = climb_import_supersession_chain(store, capsule_id, direct_edge_from)
            if head is None:
                continue
            head_changed_away = any(
                row.capsule_id == head for row in p["changed_away"]
            ) and (head not in consumed_changed_away)
            head_live_owned = any(
                owner == source_key for owner in store.sources.import_block_owners(head)
            )
            if not head_changed_away and not head_live_owned:
                continue
            try:
                revived = store.relations.unsupersede(capsule_id, direct_edge_from, now)
            except StoreError as refused:
                # U1: the edge has revocation events; its history remains. A
                # re-import must not turn into `isError` because of that (review 2026-09-16).
                store.audit.append_audit(
                    actor, audit_actions.IMPORT, capsule_id, str(refused), now
                )
                continue
            if not revived:
                continue
            store.audit.append_audit(
                actor,
                audit_actions.IMPORT,
                capsule_id,
                f"unsupersede <- {direct_edge_from} (stale-import revive: source content reverted to a prior derived capsule)",
                now,
            )
            if head_changed_away:
                supersede_if_unowned_elsewhere(
                    store,
                    actor,
                    source_key,
                    head,
                    capsule_id,
                    f"supersedes -> {head} (stale-import auto-supersede: reverted content's stale successor chain head)",
                    now,
                )
                consumed_changed_away.add(head)
            store.record_import_block(source_key, block_hash, capsule_id, ordinal, now)

        # 3b. Equal-count pairing
        remaining_changed_away = [
            row
            for row in p["changed_away"]
            if row.capsule_id not in consumed_changed_away
        ]
        seen_fresh: set[str] = set()
        new_content: list[tuple[str, str, int, bool]] = []
        for b in p["new_blocks"]:
            h, cid, _ord_num, is_fresh = b
            if is_fresh and h not in p["old_hashes"] and h not in seen_fresh:
                seen_fresh.add(h)
                new_content.append(b)

        if remaining_changed_away and len(remaining_changed_away) == len(new_content):
            for old, new in zip(remaining_changed_away, new_content):
                new_id = new[1]
                if old.capsule_id == new_id:
                    continue
                supersede_if_unowned_elsewhere(
                    store,
                    actor,
                    source_key,
                    old.capsule_id,
                    new_id,
                    f"supersedes -> {old.capsule_id} (stale-import auto-supersede: source block changed on re-import)",
                    now,
                )


def import_origin(
    args: dict[str, Any], home_dir: str | None, project_dir: str
) -> tuple[BridgeSource, Any, Path] | str:
    """`(source, dir, base)` of an import request, or the refusal message.

    `dir` belongs to `memory-dir` alone and that source requires it. Without
    `base`, the user's CLAUDE.md is read under the home dir and every other
    source under the project dir; an unknown one refuses.
    """
    src_raw = args.get("source")
    if not src_raw:
        return "nmemory_capsule_import requires source"
    try:
        source = BridgeSource.from_str(str(src_raw))
    except ValueError:
        return f"unknown bridge source: {src_raw}"
    dir_param = args.get("dir")
    if dir_param is not None and source != BridgeSource.MEMORY_DIR:
        return 'dir applies only to source "memory-dir"; drop it or switch source'
    if source == BridgeSource.MEMORY_DIR and (
        not dir_param or not str(dir_param).strip()
    ):
        return 'source "memory-dir" requires a non-empty dir'
    base_param = args.get("base")
    if base_param:
        return source, dir_param, Path(base_param)
    if source == BridgeSource.USER_CLAUDE_MD:
        if not home_dir:
            return "no home dir known at this boundary — pass base explicitly"
        return source, dir_param, Path(home_dir)
    if not project_dir:
        return "no project dir known at this boundary — pass base explicitly"
    return source, dir_param, Path(project_dir)


def _read_candidates(
    source: BridgeSource, base_dir: Path, anchor_root: str, dir_param: Any
) -> list[BridgeCandidate] | dict[str, Any]:
    """The source's import candidates, or the answer that ends the call."""
    try:
        return read_source(
            source=source,
            base_dir=base_dir,
            anchor_root=anchor_root,
            memory_dir=dir_param,
        )
    except BridgeSourceMissing as e:
        return verb_result(
            {
                "outcome": "absent",
                "source": e.source_label,
                "tried": [str(p) for p in e.tried],
            }
        )
    except BridgeSymlinkRejected as e:
        return verb_result(
            {
                "outcome": "rejected",
                "path": str(e.path),
                "reason": str(e),
                "source": source.source_label,
            }
        )
    except BridgeNotADirectory as e:
        return error_result(str(e))
    except BridgeIoError as e:
        return error_result(f"nmemory_capsule_import failed: {e}")
    except Exception as e:  # noqa: BLE001
        return error_result(f"nmemory_capsule_import failed: {e}")


def import_request(c: BridgeCandidate) -> IngestRequest:
    """An imported block: externally imported authority, always tainted."""
    return IngestRequest(
        content=c.content,
        source=c.source_label,
        anchor=c.anchor,
        authority_class=AuthorityClass.EXTERNALLY_IMPORTED,
        instruction_taint=True,
        confidence=None,
        valid_from=None,
        project_id=None,
    )


def import_row(outcome: IngestOutcome) -> dict[str, Any]:
    """The wire row of one imported block's capture outcome."""
    t_summary = taint_summary(outcome.taint_findings)
    if outcome.deduped:
        row: dict[str, Any] = {
            "status": "deduplicated",
            "id": outcome.id,
            "dedup_hint": None,
        }
    else:
        row = {
            "status": "captured",
            "id": outcome.id,
            "dedup_hint": (
                {
                    "similar_id": outcome.dedup_hint.similar_id,
                    "score": outcome.dedup_hint.score,
                }
                if outcome.dedup_hint
                else None
            ),
        }
        if outcome.siblings:
            row["siblings"] = [{"id": s.id, "score": s.score} for s in outcome.siblings]
    if outcome.superseded:
        row["superseded"] = outcome.superseded
    if t_summary:
        row["taint_findings"] = t_summary
    return row


def handle(
    state: HandlerState,
    args: dict[str, Any],
    context: CaptureContext | None,
    now: datetime,
    actor: str,
) -> dict[str, Any]:
    origin = import_origin(args, state.home_dir, state.project_dir)
    if isinstance(origin, str):
        return error_result(origin)
    source, dir_param, base_dir = origin
    candidates = _read_candidates(source, base_dir, state.anchor_root, dir_param)
    if isinstance(candidates, dict):
        return candidates

    block_metas = [
        (
            import_block_source_key(c.source_label, c.anchor),
            sha256_hex(c.content.encode("utf-8")),
            i,
        )
        for i, c in enumerate(candidates)
    ]
    outcomes: list[dict[str, Any]] = []
    defaults = state.effective_defaults(context)
    for c in candidates:
        try:
            outcome = ingest(
                store=state.store,
                req=import_request(c),
                defaults=defaults,
                now=now,
                context=context,
            )
            wire_row = import_row(outcome)
            state.store.audit.append_audit(
                actor, audit_actions.IMPORT, outcome.id, wire_row["status"], now
            )
            outcomes.append(wire_row)
        except Exception as exc:  # noqa: BLE001
            outcomes.append({"status": "rejected", "error": str(exc)})

    apply_import_supersession(
        store=state.store,
        block_metas=block_metas,
        outcomes=outcomes,
        actor=actor,
        now=now,
    )

    handles: dict[str, str] = {}
    for o in outcomes:
        cid = o.get("id")
        if cid:
            h = state.store.capsules.get_handle(cid)
            if h:
                handles[cid] = h
    statuses = [o.get("status") for o in outcomes]
    res: dict[str, Any] = {
        "outcome": "imported",
        "source": source.source_label,
        "outcomes": outcomes,
        "captured": statuses.count("captured"),
        "deduped": statuses.count("deduplicated"),
        "rejected": statuses.count("rejected"),
    }
    if handles:
        res["handles"] = handles
    return verb_result(res)
