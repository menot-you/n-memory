"""`nmemory quality measure`: measure the store's hygiene and capture it as a capsule."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from ..hygiene import measure, publish, render
from ..project import DEFAULT_PROJECT_ID
from .common import open_store_from_args, parse_command


def cmd_quality_measure(argv: list[str]) -> None:
    args = parse_command("quality", "measure", argv)
    store = open_store_from_args(args)
    now_dt = datetime.now(UTC)
    hyg = measure(store, now_dt)
    payload: dict[str, Any] = hyg.to_dict()
    if not args.dry_run:
        capsule_id, written = publish(
            store, hyg, args.project or DEFAULT_PROJECT_ID, now_dt
        )
        payload["capsule"] = capsule_id
        # One run per day: the second finds the capsule from the first and
        # does not write another.
        payload["written"] = written
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(render(hyg))
        if not args.dry_run:
            verb = "captured" if payload["written"] else "already captured today"
            print(f"\n{verb}: {payload['capsule']}")
