"""`nmemory review run --daily`: the daily curator review."""

from __future__ import annotations

import sys
from datetime import UTC, datetime

from ..curator import CuratorError, CuratorWorker, curator_root
from ..project import DEFAULT_PROJECT_ID
from ..provider import ligado
from .common import open_store_from_args, parse_command


def cmd_review_run(argv: list[str]) -> None:
    args = parse_command("review", "run", argv)
    if not ligado("NOTT_NMEMORY_CURATOR_CRON", default="0"):
        print("curator daily disabled: NOTT_NMEMORY_CURATOR_CRON")
        return
    daily_root = curator_root()
    if daily_root is None:
        print("curator daily: no project root", file=sys.stderr)
        sys.exit(1)
    store = open_store_from_args(args)
    project = args.project or DEFAULT_PROJECT_ID
    worker = CuratorWorker(store, root=daily_root, project=project)
    if not worker.client.is_configured():
        print("curator daily: provider unavailable", file=sys.stderr)
        sys.exit(1)
    # A distinct lock: the resident owns its election for its entire lifetime.
    with store.db.daily_curator_owner(project) as owner:
        if not owner:
            print(
                "curator daily: another daily pass owns this project",
                file=sys.stderr,
            )
            sys.exit(2)
        try:
            daily_stamp = datetime.now(UTC)
            count = worker.daily_cycle(now=daily_stamp)
            worker.digest_scope(now=daily_stamp, raise_errors=True)
        except CuratorError as error:
            print(f"curator daily: {error}", file=sys.stderr)
            sys.exit(1)
    print(f"curator daily: reviewed {count}")
    return
