"""`nmemory context search`: low-friction unified context search."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from ..context import execute_context
from ..embedding import EmbeddingClient
from ..ingest import effective_defaults
from ..project import expands_to_subtree_by_default
from ..space import EmbeddingError, SpaceFingerprint
from ..subproc import REPO_ROOT_S, run_with_timeout
from .common import open_store_from_args, parse_command


def cmd_context_search(argv: list[str]) -> None:
    args = parse_command("context", "search", argv)
    query = args.flag_query or args.query
    if not query:
        print("context requires one query", file=sys.stderr)
        sys.exit(1)
    store = open_store_from_args(args)
    # The two refusals of the `MemoryServer` this command once built only to read
    # its defaults, kept with their exit code: an empty project, and an embedding
    # provider outside `PROVIDERS`.
    if args.project is not None and not args.project.strip():
        print(
            "nmemory: context search: project_id must be non-empty when supplied",
            file=sys.stderr,
        )
        sys.exit(1)
    try:
        SpaceFingerprint.current()
    except EmbeddingError as error:
        print(f"nmemory: context search: {error}", file=sys.stderr)
        sys.exit(1)
    client_name = None
    if args.project is None:
        cwd = Path.cwd()
        top = run_with_timeout(
            ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=REPO_ROOT_S,
            check=False,
        )
        root = Path(top.stdout.strip()) if top.returncode == 0 else cwd
        client_name = root.name
    defaults = effective_defaults(args.project, client_name, None)
    subtree_default = args.project is None and expands_to_subtree_by_default(
        defaults.project_id
    )
    sources = (
        [s.strip() for s in args.source.split(",") if s.strip()]
        if args.source
        else None
    )
    now_dt = datetime.now(UTC)
    res = execute_context(
        store=store,
        query=query,
        project_id=None if subtree_default else defaults.project_id,
        project_prefix=defaults.project_id if subtree_default else None,
        sources=sources,
        limit=args.limit,
        token_budget=args.budget,
        now=now_dt,
        project_source=defaults.source,
        embeddings=EmbeddingClient(),
    )
    print(json.dumps(res, indent=2, ensure_ascii=False))
