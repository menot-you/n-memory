"""nmemory_context_show: observational context from capsules and the indexed corpus."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..capture import CaptureContext
from ..project import (
    canonical_project,
    expands_to_subtree_by_default,
)
from ..queries.inbox import inbox_candidates
from ..queries.standing import standing_of
from ..queries.work import open_work
from .common import (
    HandlerState,
    error_result,
    record_read_traffic,
    verb_result,
)


def handle(
    state: HandlerState,
    args: dict[str, Any],
    context: CaptureContext | None,
    now: datetime,
    actor: str,
) -> dict[str, Any]:
    query = args.get("query", "")
    if not any(c.isalnum() for c in query):
        return error_result("query must contain at least one alphanumeric character")
    requested_project_id = args.get("project_id")
    if requested_project_id is not None and not requested_project_id.strip():
        return error_result("project_id must be non-empty when supplied")
    if requested_project_id is not None:
        # U4 (nmemory-curator): the reader in the old spelling continues to
        # reach — folding never removes reach.
        requested_project_id = canonical_project(requested_project_id)
    defaults = state.effective_defaults(context)
    proj = (
        requested_project_id
        if requested_project_id is not None
        else defaults.project_id
    )
    # U4 (nmemory-curator): the server default is the AUDIENCE, and the
    # audience is the tree — `pynott` reaches `pynott/x`. Whoever passes
    # explicit project_id chose the exact fence and remains exact.
    # Measured 2026-09-15: no reader passed a prefix, and the 88 capsules
    # in `pynott/nmemory-escrita` were 100% never read.
    subtree_default = requested_project_id is None and expands_to_subtree_by_default(
        proj
    )
    project_source = "caller" if requested_project_id is not None else defaults.source
    limit = int(args.get("limit", 12))
    if limit > 50 or limit < 0:
        return error_result("limit must be within 0..=50")
    sources = args.get("sources")
    budget = int(args.get("token_budget", 2000))

    from ..context import OBSERVATIONAL_CONTEXT_SOURCES, execute_context

    if state.read_only:
        if sources is None:
            sources = list(OBSERVATIONAL_CONTEXT_SOURCES)
        elif any(source not in OBSERVATIONAL_CONTEXT_SOURCES for source in sources):
            return error_result(
                "read-only nmemory_context_show sources are memory, markdown, code, git and rustdoc"
            )

    try:
        res = execute_context(
            store=state.store,
            query=query,
            project_id=None if subtree_default else proj,
            project_prefix=proj if subtree_default else None,
            sources=sources,
            limit=limit,
            token_budget=budget,
            anchor_root=state.anchor_root,
            embeddings=state.embeddings,
            now=now,
            project_source=project_source,
        )
        if state.read_only:
            res["semantic"] = {
                "detail": "semantic embeddings are disabled by read-only mode",
                "status": "unavailable_lexical_only",
            }
        # `results` carries every provider nmemory_context_show selected, so
        # these ids span namespaces: capsules (`cap-*`), corpus chunks
        # (`ctx-*`) and adapter hits. `read_traffic.served_ids` is declared
        # to hold the surface's OWN namespace, so they go in as served —
        # a consumer joining to `nmemory.capsules` filters by prefix.
        # `project_prefix` is absent on purpose: nmemory_context_show's validated
        # field list does not accept one, so reading `args` for it would be
        # a column that can only ever be NULL.
        # U3 (nmemory-curator): the row states WHICH fence ran.
        # `project_id` continues to be the effective project (the comment above
        # explains why), and `project_prefix` appears when the audience
        # default fenced by subtree — without this the delivery census does not
        # distinguish a tree session from an exact session, and a child
        # would remain "without session" with its audience open.
        served_now = [str(r["id"]) for r in res.get("results", []) if r.get("id")]
        record_read_traffic(
            state,
            "context",
            served_now,
            query,
            proj,
            proj if subtree_default else None,
            None,
            context,
            now,
        )
        # U6 (nmemory-curator): DELIVERY. The lanes above answer the
        # question; this row brings to the session what the scoped audience
        # has not yet received — from BOTH ENDS of the queue (half the
        # oldest, so the tail drains; half the newest, so what was written
        # this week does not wait behind the archive), its own budget,
        # and never what this very call just served. Without this, a
        # capsule only arrives if someone happens to ask the question that
        # reaches it, and "pending delivery" can never reach zero.
        if proj is not None and not state.read_only:
            # The reader session goes to the fence: the new end would catch
            # what this very session just wrote, and delivery marks
            # the capsule as served forever — the subsequent audience
            # would never receive it. `Store` normalizes `unknown` and joins
            # what THIS process wrote, the fence covering Codex.
            inbox = inbox_candidates(
                state.store.db,
                state.store.capsules.appended_in_process,
                proj,
                subtree=subtree_default,
                exclude_ids=served_now,
                reader_session=context.session_id if context else None,
            )
            inbox_rows = [row.to_dict() for row in inbox]
            inbox_standing = standing_of(
                state.store.db, [row.id for row in inbox], now=now
            )
            for row_dict in inbox_rows:
                if row_dict["id"] in inbox_standing:
                    row_dict["standing"] = inbox_standing[row_dict["id"]]
            res["inbox"] = inbox_rows
            # C2b: WORK, alongside delivery and with its own budget.
            # Measured in the live store on 2026-09-17: 1434 open tasks out of 2062 and
            # 407 dods without verdict out of 1112 — numbers that lived only in
            # SessionStart and in the census, so no one saw them while working.
            # `inbox` excludes `task`/`dod` on purpose because the work
            # plan has its own consumer: this one.
            #
            # Additive and OUTSIDE `token_budget`: the list is capped at five and
            # totals are exact — the long list is noise, the hidden
            # total was the defect. The closing rule is the only one in the
            # repository (`CLOSING_KIND_VALUES` with the liveness fence),
            # and this is the fifth site that consumes it without re-deriving it.
            res["work"] = open_work(
                state.store.db, proj, subtree=subtree_default, now=now
            ).to_dict()
            if inbox:
                record_read_traffic(
                    state,
                    "inbox",
                    [row.id for row in inbox],
                    proj,
                    proj,
                    proj if subtree_default else None,
                    None,
                    context,
                    now,
                )
        return verb_result(res)
    except Exception as e:  # noqa: BLE001
        return error_result(f"nmemory_context_show failed: {e}")
