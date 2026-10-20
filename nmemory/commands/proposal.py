"""`nmemory proposal export`: System1's staged choices labelled by the human verdict."""

from __future__ import annotations

import json
from datetime import UTC, datetime, time

from ..queries.staged import staged_feedback
from .common import open_store_from_args, parse_command


def cmd_proposal_export(argv: list[str]) -> None:
    """Read-only: System1's staged choices labelled by the human verdict."""
    args = parse_command("proposal", "export", argv)
    store = open_store_from_args(args, read_only=True)
    since, until = (
        datetime.combine(d, time(), tzinfo=UTC) for d in (args.since, args.until)
    )
    for row in staged_feedback(store.db, since, until):
        print(json.dumps(row, ensure_ascii=False))
