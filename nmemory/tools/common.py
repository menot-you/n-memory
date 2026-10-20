"""What every MCP tool handler shares: its state, its envelopes and its bookkeeping.

A handler is `handle(state, args, context, now, actor)` in `nmemory.tools.<noun>`.
`MemoryServer.call_tool` builds the `HandlerState` once per call, after the store
has opened, so a handler reads what the server holds at that moment and never
the server itself: nothing under `nmemory.tools` imports `nmemory.server`.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..capture import CaptureContext
from ..config import ConfigError
from ..embedding import EmbeddingClient
from ..ingest import IngestDefaults
from ..relation import Dag, DagCycleError, RelationKind, RelationRecord, closed_task_ids
from ..repositories.capsules import StoredCapsule
from ..store import Store
from ..taint import TaintFinding

ADVISORY_LABEL = "ADVISORY_NOT_AUTHORITY"
DATA_FRAMING = "DATA"
CAPTURE_ONLY_REFUSAL = "NOTT_MEMORY_POLICY=push permits capture only"
INHERITED_REFUSAL = (
    "an inherited session has no nMEMORY. Use the "
    "memory in your brief, and report each fact to your parent: the parent "
    "captures it"
)
INHERITED_INSTRUCTIONS = (
    "nMEMORY is not available to an inherited session; report facts to your parent."
)


def inherited_session() -> bool:
    """True in an nSPAWN child or workflow run: only the main session uses nMEMORY."""
    return bool(os.environ.get("NOTT_NSPAWN_RUN_ID"))


def capture_only_policy() -> bool:
    """Read the capture policy. Refuse an unknown value."""
    policy = os.environ.get("NOTT_MEMORY_POLICY")
    if policy is None:
        return False
    if policy != "push":
        raise ConfigError("NOTT_MEMORY_POLICY must be push when set")
    return True


@dataclass(frozen=True, slots=True)
class HandlerState:
    """The server's state a tool handler reads, as it stands for one call.

    Each field keeps the name of the `MemoryServer` attribute it comes from.
    `effective_defaults` and `resolve_hmac_key` are bound to the server: both
    read what `initialize` and the construction set (the client's name, the
    configured project, the HMAC key sources).
    """

    store: Store
    anchor_root: str
    embeddings: EmbeddingClient | None
    read_only: bool
    home_dir: str | None
    project_dir: str
    effective_defaults: Callable[[CaptureContext | None], IngestDefaults]
    resolve_hmac_key: Callable[[], bytes]
    viewer: bool = False
    capture_only: bool = False


type Handler = Callable[
    [HandlerState, dict[str, Any], CaptureContext | None, datetime, str],
    dict[str, Any],
]


def verb_result(data: Any) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}],
        "isError": False,
    }


def error_result(message: str) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": message}],
        "isError": True,
    }


def taint_summary(findings: list[TaintFinding]) -> list[str]:
    summary: list[str] = []
    for f in findings:
        rule_str = f.rule.value if hasattr(f.rule, "value") else str(f.rule)
        terms = [m.term for m in f.matches]
        summary.append(f"{rule_str}: {', '.join(terms)}")
    return summary


def record_read_traffic(
    state: HandlerState,
    surface: str,
    served_ids: list[str],
    terms: str,
    project_id: str | None,
    project_prefix: str | None,
    outcome: str | None,
    context: CaptureContext | None,
    now: datetime,
) -> None:
    """Count an orientation read without turning the reader into a writer.

    `Database.record_read_traffic` is the non-canonical path: the row is written
    after this tool's boundary ends, in its own short write, and a storage
    failure is swallowed so bookkeeping can never fail a read.

    A read that returned NOTHING is still recorded, and is the most
    interesting row in the table: it says the agent asked and the store had
    no answer.
    """
    if state.read_only or state.viewer:
        return
    state.store.db.record_read_traffic(
        surface=surface,
        session_id=context.session_id if context else None,
        terms=terms,
        served_ids=served_ids,
        project_id=project_id,
        project_prefix=project_prefix,
        outcome=outcome,
        now=now,
    )


def _valid_part_of(
    row: RelationRecord,
    task_ids: set[str],
    plan_ids: set[str],
    epic_ids: set[str],
) -> bool:
    return (
        row.from_id in task_ids and row.to_id in (task_ids | plan_ids | epic_ids)
    ) or (row.from_id in plan_ids and row.to_id in epic_ids)


def open_task_ids(
    task_ids: set[str],
    live_by_id: dict[str, StoredCapsule],
    rows: list[RelationRecord],
    staged: set[str],
    now: datetime,
) -> set[str]:
    """READY and OPEN WORK, not mere participation in a `blocks` edge.

    A task leaves here when a `supersedes` names it, when a `witnesses`
    closes it, when freshness expires, when it is forgotten (the classification
    row survives the capsule), or while it is a staged proposal.
    """
    closed = closed_task_ids(rows)
    open_ids: set[str] = set()
    for task_id in task_ids:
        stored = live_by_id.get(task_id)
        if stored is None or task_id in staged or task_id in closed:
            continue
        valid_to = stored.capsule.freshness.valid_to
        if valid_to is not None and valid_to < now:
            continue
        open_ids.add(task_id)
    return open_ids


def dag_section(
    rows: list[RelationRecord],
    tombstoned: set[str],
    open_tasks: set[str],
    task_ids: set[str],
    plan_ids: set[str],
    epic_ids: set[str],
    cap: int,
) -> dict[str, Any]:
    """The wire `dag` section: lists capped at `cap`, EXACT totals.

    Pre-contract rows — a `blocks` with an endpoint outside `task`, a `part_of`
    outside task->plan|epic or plan->epic — remain in history and are INERT
    for readiness; they are counted so that inertia is visible, and omitted
    at zero so that a clean store reads as it always did.
    """
    edges: list[RelationRecord] = []
    legacy_blocks = 0
    legacy_part_of = 0
    for row in rows:
        if row.kind is RelationKind.BLOCKS and (
            row.from_id not in task_ids or row.to_id not in task_ids
        ):
            legacy_blocks += 1
            continue
        if row.kind is RelationKind.PART_OF and not _valid_part_of(
            row, task_ids, plan_ids, epic_ids
        ):
            legacy_part_of += 1
            continue
        edges.append(row)

    legacy: dict[str, Any] = {}
    if legacy_blocks:
        legacy["legacy_blocks_total"] = legacy_blocks
    if legacy_part_of:
        legacy["legacy_part_of_total"] = legacy_part_of

    try:
        dag = Dag.project_excluding(edges, tombstoned)
    except DagCycleError as cycle_error:
        return {
            "status": "cycle",
            "cycle": cycle_error.cycle,
            "entangled_total": len(cycle_error.entangled),
            **legacy,
        }

    # `is_live` is false for exactly two classes: an id that the `blocks`
    # universe never named, and a superseded member. `open_tasks` does not carry
    # superseded members by construction, so here this means "no `blocks` edge
    # named it" — and that task is ready by definition.
    ready = sorted(set(dag.ready()) | {i for i in open_tasks if not dag.is_live(i)})
    done = dag.done()
    participants = {
        i for e in edges if e.kind is RelationKind.BLOCKS for i in (e.from_id, e.to_id)
    }
    blocked = sorted(
        i
        for i in participants
        if dag.is_live(i) and not dag.is_done(i) and dag.blocked_by(i)
    )
    return {
        "status": "ok",
        "ready": ready[:cap],
        "ready_total": len(ready),
        "blocked": blocked[:cap],
        "blocked_total": len(blocked),
        "done": done[:cap],
        "done_total": len(done),
        **legacy,
    }
