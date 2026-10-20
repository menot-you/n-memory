"""`nmemory portal export`: read-only generated documentation pages."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime

from ..portal import render_portal
from ..project import canonical_project
from .common import open_store_from_args, parse_command


def cmd_portal_export(argv: list[str]) -> None:
    args = parse_command("portal", "export", argv)
    if args.project is None:
        print("nmemory portal export: --project is required", file=sys.stderr)
        sys.exit(2)
    project = canonical_project(args.project)
    store = open_store_from_args(args, read_only=True)
    result = render_portal(store, project, datetime.now(UTC))
    print(
        result.digest
        if args.digest
        else json.dumps(result.to_dict(), ensure_ascii=False)
    )
