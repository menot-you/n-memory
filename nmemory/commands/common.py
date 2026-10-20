"""What every CLI command shares: its argument grammar and its store.

`command_parser` declares every command's options, one subparser per
`<noun> <verb>` (the words `nmemory.cli.COMMANDS` dispatches), and `parse_command`
parses one command's arguments against it, so a handler in
`nmemory.commands.<noun>` never reaches back to `nmemory.cli`.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date
from pathlib import Path

import nott_database
from nott_database import DatabaseError

from ..capsule import parse_rfc3339
from ..config import ConfigError
from ..database import store_path
from ..store import Store


def _nonnegative_integer(value: str) -> int:
    try:
        number = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer") from error
    if number < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return number


def _known_at(value: str) -> str:
    try:
        parse_rfc3339(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an RFC3339 timestamp") from error
    return value


def open_store_from_args(
    args: argparse.Namespace,
    *,
    read_only: bool = False,
    read_census: bool = False,
    path: str | os.PathLike[str] | None = None,
) -> Store:
    """Open the store with fail-closed semantics.

    Every verb here is a production entry point, so the account's live store is
    admitted (`nott_database` still refuses it under pytest). The store file is
    `store_path()` unless `path` names another; it is never created here.
    """
    verb = str(getattr(args, "command", None) or "nmemory")
    is_cli_subcommand = getattr(args, "command", None) not in (None, "mcp serve")
    try:
        return Store.open(
            store_path() if path is None else path,
            # Display only: the census never writes.
            read_only=read_only or read_census,
            allow_live_store=True,
        )
    except nott_database.RefusedError:
        raise
    except (DatabaseError, nott_database.Error, ConfigError, OSError) as e:
        if is_cli_subcommand:
            message = " ".join(str(e).splitlines())
            print(f"nmemory: {verb}: {message}", file=sys.stderr)
            sys.exit(1)
        raise


def command_parser() -> argparse.ArgumentParser:
    """One subparser per table command, keyed by `<noun> <verb>`."""
    parser = argparse.ArgumentParser(
        prog="nmemory", description="nMEMORY MCP server and CLI"
    )

    subparsers = parser.add_subparsers(dest="command")

    curator_p = subparsers.add_parser("curator set", help="Select the curator route")
    curator_p.add_argument("--provider")
    curator_p.add_argument("--account")
    curator_p.add_argument("--reset", action="store_true")

    scan_p = subparsers.add_parser(
        "project scan", help="Scan repository facts into proposals"
    )
    scan_p.add_argument("--root", required=True, help="Repository root")
    scan_p.add_argument("--project", required=True, help="Project scope")

    bootstrap_p = subparsers.add_parser(
        "project bootstrap", help="Configure project memory"
    )
    bootstrap_p.add_argument("--root", type=Path, default=Path.cwd())
    bootstrap_p.add_argument("--client", choices=("claude", "codex"), default="claude")
    bootstrap_p.add_argument(
        "--system1", choices=("lexical", "native", "local"), default="lexical"
    )
    bootstrap_p.add_argument("--endpoint")
    bootstrap_p.add_argument("--model")
    bootstrap_p.add_argument("--hooks-dir", type=Path)
    bootstrap_p.add_argument("--restore", action="store_true")

    release_p = subparsers.add_parser(
        "release export", help="Export a standalone source workspace"
    )
    release_p.add_argument(
        "--component",
        choices=["nmemory", "nspawn", "nbrowser", "napple"],
        default="nmemory",
    )
    release_p.add_argument("--source-root", type=Path, required=True)
    release_p.add_argument("--output", type=Path, required=True)

    # database
    subparsers.add_parser(
        "database create",
        help="Create an empty store at the store path (never overwrites)",
    )
    snapshot_p = subparsers.add_parser(
        "database snapshot",
        help="Copy the store, compacted, into DEST (VACUUM INTO; takes no writer lock)",
    )
    snapshot_p.add_argument("dest", help="New file to write; refused if it exists")

    # recall
    recall_p = subparsers.add_parser(
        "capsule retrieve",
        help="Query recall engine directly",
        description="Evidence recall excludes dod by default; use get for a known id.",
    )
    recall_p.add_argument(
        "query", type=str, nargs="?", default=None, help="Search query"
    )
    recall_p.add_argument("--terms", type=str, help="Comma-separated terms")
    recall_p.add_argument(
        "--limit", type=_nonnegative_integer, default=50, help="Max items to retrieve"
    )
    recall_p.add_argument(
        "--budget", type=_nonnegative_integer, default=None, help="Token budget"
    )
    recall_p.add_argument("--project-prefix", type=str, help="Project prefix filter")
    recall_p.add_argument(
        "--known-at", type=_known_at, help="Point-in-time RFC3339 timestamp"
    )

    # digest
    digest_p = subparsers.add_parser("digest show", help="Print store digest")
    feedback_p = subparsers.add_parser(
        "proposal export",
        help="Staged observations with a later human verdict, as JSON lines",
    )
    for flag in ("--since", "--until"):
        feedback_p.add_argument(
            flag, type=date.fromisoformat, required=True, help="YYYY-MM-DD (UTC)"
        )
    digest_p.add_argument(
        "--headlines",
        type=int,
        default=None,
        help="Compatibility option; the census always counts the whole scope",
    )
    digest_p.add_argument("--project-prefix", type=str, help="Project prefix filter")

    list_p = subparsers.add_parser("capsule list", help="List pinned capsule headlines")
    list_p.add_argument("--pinned", action="store_true", required=True)
    list_form = list_p.add_mutually_exclusive_group()
    list_form.add_argument(
        "--json", action="store_true", help="Every pin as JSON lines, no cap"
    )
    list_form.add_argument(
        "--full",
        action="store_true",
        help="Every owner rule with no row cap, each in full while it fits",
    )

    rule_p = subparsers.add_parser("rule write", help="Write the rule files")
    rule_p.add_argument("--root", type=str, default=None, help="Project root")

    # get
    get_p = subparsers.add_parser(
        "capsule show", help="Get a single capsule by reference"
    )
    get_p.add_argument(
        "--id", type=str, required=True, help="Capsule ID or handle reference"
    )

    # export
    export_p = subparsers.add_parser(
        "capsule export", help="Render markdown generated view"
    )
    export_p.add_argument(
        "--no-stamp", action="store_true", help="Omit generated_at timestamp"
    )

    # context
    context_p = subparsers.add_parser(
        "context search", help="Low-friction unified context search"
    )
    context_p.add_argument(
        "query", type=str, nargs="?", default=None, help="Query string"
    )
    context_p.add_argument("--query", dest="flag_query", type=str, help="Query string")
    context_p.add_argument("--source", type=str, help="Comma-separated source filter")
    context_p.add_argument(
        "--project", type=str, default=argparse.SUPPRESS, help="Project filter"
    )
    context_p.add_argument("--limit", type=int, default=12, help="Result limit")
    context_p.add_argument("--budget", type=int, default=2000, help="Token budget")

    # index
    index_p = subparsers.add_parser("index build", help="Explicit context indexing")
    index_p.add_argument("--root", type=str, help="Repository root")
    index_p.add_argument(
        "--retire-root",
        type=str,
        help="Retire an earlier checkout root after a complete successful index",
    )
    index_p.add_argument("--source", type=str, help="Comma-separated source filter")
    index_p.add_argument(
        "--project", type=str, default=argparse.SUPPRESS, help="Project filter"
    )

    portal_p = subparsers.add_parser(
        "portal export", help="Export selected private portal pages as JSON"
    )
    portal_p.add_argument(
        "--digest", action="store_true", help="Print only the selected-pages digest"
    )

    # github-sync
    gh_sync_p = subparsers.add_parser(
        "github sync", help="Sync nMEMORY planning graph with GitHub issues"
    )
    gh_sync_p.add_argument(
        "--verify", action="store_true", help="Report drift without writing"
    )
    gh_sync_p.add_argument("--config", type=str, help="Path to config.toml")
    gh_sync_p.add_argument("--repo", type=str, help="Target repository (owner/name)")
    gh_sync_p.add_argument("--label", type=str, help="Fence label")
    gh_sync_p.add_argument(
        "--project", type=str, default=argparse.SUPPRESS, help="Project prefix filter"
    )
    gh_sync_p.add_argument(
        "--max-writes", type=int, default=None, help="Max plan-derived writes"
    )
    gh_sync_p.add_argument("--effort", type=str, help="Narrow to effort epic reference")

    # relate
    relate_p = subparsers.add_parser(
        "capsule relate", help="Record semantic relation between capsules"
    )
    relate_p.add_argument("--kind", type=str, required=True, help="Relation kind")
    relate_p.add_argument(
        "--from",
        dest="from_",
        type=str,
        required=True,
        help="Source capsule reference",
    )
    relate_p.add_argument(
        "--to", type=str, required=True, help="Target capsule reference"
    )
    # U1 (nmemory-standing): revocation lives in the CLI and curator, not in MCP —
    # `nmemory_relation_add` is sealed in `tools_schema.json` and adding a field
    # would require resealing. Revoking never deletes: it is an event in relation_revocations.
    relate_p.add_argument(
        "--revoke",
        action="store_true",
        help="Revoke the edge instead of asserting it (falsifies/supersedes); needs --actor and --reason",
    )
    relate_p.add_argument("--actor", type=str, default=None, help="Who revokes")
    relate_p.add_argument(
        "--reason", type=str, default=None, help="Why (goes to the audit journal)"
    )

    # anchors
    subparsers.add_parser("anchor list", help="Print live capsule anchor index")

    # anchor-check (U2): hook spool drain, or the full sweep
    ac = subparsers.add_parser(
        "anchor check",
        help="Judge code anchors against the working tree and record verification events",
    )
    ac.add_argument("--drain", action="store_true", help="Consume the hook spool")
    ac.add_argument(
        "--all", action="store_true", help="Audit every live code-anchored capsule"
    )
    ac.add_argument(
        "--root",
        type=str,
        default=None,
        help="Repo root (default: git toplevel of cwd)",
    )
    ac.add_argument(
        "--spool",
        type=str,
        default=None,
        help="Spool dir (default: XDG nott/anchor-queue)",
    )
    ac.add_argument("--json", action="store_true", help="Emit JSON")

    # staleness
    staleness_p = subparsers.add_parser(
        "capsule audit", help="Obsolescence condition checker"
    )
    staleness_p.add_argument("--json", action="store_true", help="Output JSON")
    staleness_p.add_argument(
        "--apply",
        action="store_true",
        help="record a verification event for each TRIGGERED condition "
        "(without it, report only)",
    )

    curate_p = subparsers.add_parser("review run", help="Daily curator review")
    curate_p.add_argument("--daily", action="store_true", required=True)

    # hygiene
    hygiene_p = subparsers.add_parser(
        "quality measure",
        help="Measure store hygiene and capture the result as a capsule",
    )
    hygiene_p.add_argument("--json", action="store_true", help="Output JSON")
    hygiene_p.add_argument(
        "--dry-run",
        action="store_true",
        help="measure and print without capturing the capsule",
    )

    # embeddings
    emb_status_p = subparsers.add_parser("embedding show", help="Show embedding status")
    emb_status_p.add_argument("--json", action="store_true", help="Output JSON")

    # the MCP server
    serve_p = subparsers.add_parser("mcp serve", help="Serve MCP on stdio or HTTP")
    serve_p.add_argument(
        "--http",
        action="store_true",
        help="Serve MCP Streamable HTTP on a loopback port instead of stdio",
    )
    serve_p.add_argument(
        "--listen",
        default=None,
        help="With --http: loopback host:port (default 127.0.0.1:16699)",
    )
    serve_p.add_argument(
        "--key-file",
        type=Path,
        default=None,
        help="With --http: bearer key file, created 0600 on first run "
        "(default ~/.nott/secrets/memory.key)",
    )
    serve_mode = serve_p.add_mutually_exclusive_group()
    serve_mode.add_argument(
        "--read-only",
        action="store_true",
        help="Expose only observational nmemory_context_show",
    )
    serve_mode.add_argument(
        "--viewer",
        action="store_true",
        help="Serve only the four page reads; count no read and start no writer",
    )

    # `--project` is every command's default project scope. The commands that
    # already read it as their own filter keep their definition.
    for sub in subparsers.choices.values():
        if "--project" not in sub._option_string_actions:
            sub.add_argument(
                "--project",
                type=str,
                default=argparse.SUPPRESS,
                help="Default project scope",
            )
    parser.set_defaults(project=None, read_only=False, viewer=False)
    return parser


def parse_command(noun: str, verb: str, argv: list[str]) -> argparse.Namespace:
    """Parse one table command's arguments; `args.command` is `<noun> <verb>`."""
    return command_parser().parse_args([f"{noun} {verb}", *argv])
