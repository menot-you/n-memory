"""nmemory_index_build: index a tree under the anchor root into the context corpus."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from ..capture import CaptureContext
from ..project import (
    canonical_project,
)
from .common import (
    HandlerState,
    error_result,
    verb_result,
)


def handle(
    state: HandlerState,
    args: dict[str, Any],
    context: CaptureContext | None,
    now: datetime,
    actor: str,
) -> dict[str, Any]:
    proj = args.get("project_id")
    if proj is not None and not proj.strip():
        return error_result("project_id must be non-empty when supplied")
    if proj is not None:
        # U4: the indexed corpus must be born under the CANONICAL key, otherwise
        # `nmemory_index_build` seeds occurrences under a spelling that no reader
        # fences — folding on read does not reach them and the index is dead
        # on arrival. Caught by the independent review.
        proj = canonical_project(proj)
    root = args.get("root") or state.anchor_root

    # THE FENCE. `root` is a free string from the caller — the schema declares no
    # enum, no pattern, no required prefix — and `index_context` resolves it without
    # ever comparing it to anything. Without this, a caller names any git repository
    # the server process can read and its chunks land in the owner's store.
    #
    # Do not mistake the check inside `prepare_files` for this one: that refuses a
    # tracked symlink escaping the root THE CALLER CHOSE. It bounds the walk inside
    # the named tree; it does not bound which tree may be named.
    #
    # RESOLVED paths, never a string prefix: `..` and a symlink both walk straight
    # through a `startswith`, and a fence that compares unresolved strings is a fence
    # in name only.
    # ONE resolution, and the same one both decides and acts. Resolving twice — once
    # to judge, once to read — leaves a window between them, and a symlink swapped
    # inside that window is judged as the path it was and read as the path it became.
    # Measured 2026-08-30 against a live store: 300 racing calls, and content from a
    # repository OUTSIDE the anchor landed in `corpus_chunks` with this fence enabled.
    # `index_context` derives its source key and its stored root from the resolved
    # path anyway, so handing it `target` is byte-identical for every call that is
    # not racing — and closes the window for the one that is.
    anchor = Path(state.anchor_root).resolve()
    target = Path(root).resolve()
    if not target.is_relative_to(anchor):
        return error_result(
            f"nmemory_index_build refuses a root outside the anchor: {target} is not "
            f"inside {anchor}. The anchor is the tree this server was pointed at; "
            f"indexing another one writes a repository the owner never named into "
            f"the owner's store."
        )

    sources = args.get("sources")

    from ..context import index_context

    try:
        res = index_context(
            store=state.store,
            root=target,
            project_id=proj,
            sources=sources,
            now=now,
            actor=actor,
        )
        return verb_result(res)
    except Exception as e:  # noqa: BLE001
        return error_result(f"nmemory_index_build failed: {e}")
