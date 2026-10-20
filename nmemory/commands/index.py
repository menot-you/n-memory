"""`nmemory index build`: explicit context indexing."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from ..context import index_context
from .common import open_store_from_args, parse_command


def cmd_index_build(argv: list[str]) -> None:
    args = parse_command("index", "build", argv)
    store = open_store_from_args(args)
    sources = (
        [s.strip() for s in args.source.split(",") if s.strip()]
        if args.source
        else None
    )
    now_dt = datetime.now(UTC)
    res = index_context(
        store=store,
        root=args.root,
        project_id=args.project,
        sources=sources,
        now=now_dt,
        retire_root=args.retire_root,
    )
    print(json.dumps(res, indent=2, ensure_ascii=False))
