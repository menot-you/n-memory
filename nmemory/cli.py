"""CLI entry point for nmemory: `nmemory <noun> <verb>`.

`mcp serve` serves MCP on stdio; every other command is a bounded operation whose
handler, `cmd_<noun>_<verb>`, lives in `nmemory.commands.<noun>`.
`COMMANDS` is the only list of commands. `tools/organization.py` reads its keys by AST
against the declared grammar, the help is rendered from it, and every handler is a
`cmd_<noun>_<verb>` bound in it exactly once (`tests/test_commands.py`). A spelling that
is not in the table is an unknown command: there are no aliases, and a bare `nmemory`
is a usage error rather than the server.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from threading import Thread
from typing import TYPE_CHECKING, Final, NamedTuple

import nott_database
from nott_database import DatabaseError
from nott_system1.renamed import RENAMED as SYSTEM1_RENAMED
from nott_system1.renamed import RenamedVariableError as System1RenamedVariableError
from nott_system1.renamed import refuse as system1_refuse

from . import NMemoryError, __version__
from .commands.anchor import cmd_anchor_check, cmd_anchor_list
from .commands.capsule import (
    cmd_capsule_audit,
    cmd_capsule_export,
    cmd_capsule_list,
    cmd_capsule_relate,
    cmd_capsule_retrieve,
    cmd_capsule_show,
)
from .commands.common import open_store_from_args, parse_command
from .commands.context import cmd_context_search
from .commands.curator import cmd_curator_set
from .commands.database import cmd_database_create, cmd_database_snapshot
from .commands.digest import cmd_digest_show
from .commands.embedding import cmd_embedding_show
from .commands.github import cmd_github_sync
from .commands.index import cmd_index_build
from .commands.portal import cmd_portal_export
from .commands.project import cmd_project_bootstrap, cmd_project_scan
from .commands.proposal import cmd_proposal_export
from .commands.quality import cmd_quality_measure
from .commands.release import cmd_release_export
from .commands.review import cmd_review_run
from .commands.rule import cmd_rule_write
from .config import ConfigError
from .context_readers import ContextGitError, ContextIndexError, ContextIoError
from .database import SCHEMA_VERSION, store_path
from .embedding_index import EmbeddingWorker, spawn_embedding_worker
from .ingest import client_project_slug
from .provider import ligado, provider_name
from .renamed import READS, RenamedVariableError, refuse
from .space import EmbeddingError
from .store import Store
from .tools.common import (
    CAPTURE_ONLY_REFUSAL,
    INHERITED_REFUSAL,
    capture_only_policy,
    inherited_session,
)

if TYPE_CHECKING:
    from .curator import CuratorWorker
    from .rules_writer import RulesWriter


def cmd_mcp_serve(argv: list[str]) -> None:
    from .server import MemoryServer, run_stdio_server

    args = parse_command("mcp", "serve", argv)
    workers: list[tuple[EmbeddingWorker | CuratorWorker, Thread]] = []

    # Default stdio server.
    #
    # Opening is LAZY: runs on the first `tools/call`, never here.
    # `initialize` and `tools/list` are schema only, and a child whose store is
    # missing must still answer them (measured 2026-09-05: the release installer,
    # under a service account, received zero response).
    # Door workers are spawned with the store for the same reason.
    def open_store() -> Store:
        target = store_path()
        if args.command == "mcp serve":
            print(f"nmemory: store {target}", file=sys.stderr)
        store = open_store_from_args(args, read_only=args.read_only, path=target)
        # A viewer counts nothing, so it starts no background writer either.
        if not args.read_only and not args.viewer:
            workers.append(spawn_embedding_worker(store))
            # U4a: curator is born ON (default 1; `0` turns off); a scope + root
            # (the project where the session opened) turn it on.
            if ligado("NOTT_NMEMORY_CURATOR") and args.project:
                from .curator import CuratorClient, curator_root, spawn_curator_worker

                root = curator_root()
                # A provider name outside the vocabulary turns off only the LLM
                # judge (`has_provider` refuses it), with the cause on stderr; the
                # resident thread still runs the staged System1 suggestions, which
                # need no provider. Never a ValueError on the first tool (lazy open).
                try:
                    provider_name()
                    bad_provider = False
                except (ValueError, NMemoryError) as exc:
                    print(
                        f"nmemory: curator LLM judge off: {exc}; "
                        "staged suggestions still run",
                        file=sys.stderr,
                    )
                    bad_provider = True
                # The `--project` from .mcp.json is a literal for all sessions; the
                # scope the curator judges derives from the root itself, so that a
                # session opened in another repo never judges capsules from this one.
                curator_scope = client_project_slug(root.name) if root else None
                if (
                    root is not None
                    and not bad_provider
                    and not CuratorClient().is_configured()
                ):
                    print(
                        "nmemory: curator on but no claude on PATH or no provider "
                        "in the env; the child does not inherit the session "
                        "credential. Set <PROVIDER>_API_KEY + <PROVIDER>_BASE_URL "
                        "for NOTT_NMEMORY_CURATOR_PROVIDER (zai|minimax|kimi; "
                        "zai by default), or an ANTHROPIC_AUTH_TOKEN/ANTHROPIC_API_KEY of "
                        "your own; a claude.ai OAuth token alone is refused "
                        "unless NOTT_NMEMORY_CURATOR_ALLOW_CLAUDE_QUOTA=1",
                        file=sys.stderr,
                    )
                if root is not None and curator_scope is not None:
                    if curator_scope != args.project:
                        print(
                            f"nmemory: curator judges scope {curator_scope!r} "
                            f"(from {root}), not --project {args.project!r}",
                            file=sys.stderr,
                        )
                    workers.append(
                        spawn_curator_worker(store, root=root, project=curator_scope)
                    )
                else:
                    print(
                        "nmemory: curator on but the server cwd is not a directory; "
                        "curator not started",
                        file=sys.stderr,
                    )
        return store

    server = MemoryServer(
        open_store=open_store,
        project_id=args.project,
        read_only=args.read_only,
        viewer=args.viewer,
    )
    rules: tuple[RulesWriter, Thread] | None = None
    try:
        if args.http:
            if server.capture_only:
                raise ConfigError("NOTT_MEMORY_POLICY=push requires stdio")
            from .serve import DEFAULT_LISTEN, default_key_path, run_http_server

            if not (args.read_only or args.viewer):
                from .rules_writer import spawn_rules_writer

                rules = spawn_rules_writer(lambda: server.store, Path.home())

            sys.exit(
                run_http_server(
                    server,
                    args.listen or DEFAULT_LISTEN,
                    args.key_file or default_key_path(),
                )
            )
        run_stdio_server(server)
    finally:
        if rules is not None:
            rules[0].stop.set()
            rules[1].join(timeout=1.0)
        for worker, _ in workers:
            worker.stop()
        for _, thread in workers:
            # Provider calls can outlast shutdown. These threads are daemons.
            thread.join(timeout=1.0)


# --- the table ---------------------------------------------------------------------


class Command(NamedTuple):
    synopsis: str
    summary: str
    handler: Callable[[list[str]], None]


COMMANDS: Final[dict[tuple[str, str], Command]] = {
    ("release", "export"): Command(
        "--source-root ROOT --output DIR [--component nmemory|nspawn|nbrowser|napple]",
        "export a standalone source workspace",
        cmd_release_export,
    ),
    ("project", "bootstrap"): Command(
        "--root DIR --project P [--client claude|codex] [--system1 lexical|native|local] [--endpoint URL --model M] [--hooks-dir DIR] [--restore]",
        "configure a project and check its memory consumer",
        cmd_project_bootstrap,
    ),
    ("curator", "set"): Command(
        "--provider P [--account A] | --reset",
        "select the curator route while preserving other configuration",
        cmd_curator_set,
    ),
    ("mcp", "serve"): Command(
        "[--project P] [--read-only | --viewer] [--http [--listen H:P] [--key-file F]]",
        "serve MCP on stdio, or Streamable HTTP with --http",
        cmd_mcp_serve,
    ),
    ("database", "create"): Command(
        "",
        "create an empty store at the store path (never overwrites)",
        cmd_database_create,
    ),
    ("database", "snapshot"): Command(
        "DEST",
        "copy the store, compacted, into DEST (VACUUM INTO; refused if DEST exists; takes no writer lock)",
        cmd_database_snapshot,
    ),
    ("capsule", "retrieve"): Command(
        "[QUERY] [--terms T] [--limit N] [--budget N] [--project-prefix P] [--known-at T]",
        "query the recall engine directly (dod excluded; capsule show for a known id)",
        cmd_capsule_retrieve,
    ),
    ("capsule", "show"): Command(
        "--id REF", "one capsule by reference", cmd_capsule_show
    ),
    ("capsule", "list"): Command(
        "--pinned [--json | --full]",
        "list up to 60 live pinned headlines, newest first, and the count left out "
        "(--json: every one as JSON lines; --full: every owner rule with no row cap, "
        "in full while room lasts)",
        cmd_capsule_list,
    ),
    ("capsule", "export"): Command(
        "[--no-stamp]", "render the markdown generated view", cmd_capsule_export
    ),
    ("capsule", "relate"): Command(
        "--kind K --from REF --to REF [--revoke --actor A --reason R]",
        "record, or revoke, a semantic relation between capsules",
        cmd_capsule_relate,
    ),
    ("capsule", "audit"): Command(
        "[--json] [--apply]",
        "check obsolescence conditions (--apply records verification events)",
        cmd_capsule_audit,
    ),
    ("rule", "write"): Command(
        "",
        "write the live rules to ~/.nott/RULES.md, ~/.nott/projects/ and ~/.nott/agents/",
        cmd_rule_write,
    ),
    ("proposal", "export"): Command(
        "--since DATE --until DATE",
        "staged observations with a later human verdict, as JSON lines (read-only)",
        cmd_proposal_export,
    ),
    ("digest", "show"): Command(
        "[--project-prefix P]", "print the store digest", cmd_digest_show
    ),
    ("context", "search"): Command(
        "[QUERY] [--source S] [--limit N] [--budget N]",
        "low-friction unified context search",
        cmd_context_search,
    ),
    ("project", "scan"): Command(
        "--root DIR --project P",
        "scan repository facts into proposals",
        cmd_project_scan,
    ),
    ("index", "build"): Command(
        "[--root DIR] [--source S]", "explicit context indexing", cmd_index_build
    ),
    ("portal", "export"): Command(
        "--project P [--digest]",
        "export selected private portal pages from the read-only store",
        cmd_portal_export,
    ),
    ("github", "sync"): Command(
        "[--verify] [--config F] [--repo R] [--label L] [--max-writes N] [--effort REF]",
        "mirror the nMEMORY planning graph to GitHub issues",
        cmd_github_sync,
    ),
    ("anchor", "list"): Command(
        "", "print the live capsule anchor index", cmd_anchor_list
    ),
    ("anchor", "check"): Command(
        "[--drain | --all] [--root DIR] [--spool DIR] [--json]",
        "judge code anchors against the working tree and record verification events",
        cmd_anchor_check,
    ),
    ("quality", "measure"): Command(
        "[--json] [--dry-run]",
        "measure store hygiene and capture the result as a capsule",
        cmd_quality_measure,
    ),
    ("review", "run"): Command("--daily", "the daily curator review", cmd_review_run),
    ("embedding", "show"): Command("[--json]", "embedding status", cmd_embedding_show),
}

USAGE: Final = (
    "usage: nmemory <noun> <verb> [arguments]   (nmemory --help lists every command)"
)


def render_help() -> str:
    """The help, generated from COMMANDS in table order."""
    lines = [
        "nMEMORY MCP server and CLI; every command also takes --project P.",
        "",
        USAGE,
        "",
    ]
    for (noun, verb), command in COMMANDS.items():
        head = f"nmemory {noun} {verb} {command.synopsis}".rstrip()
        lines.append(f"  {head}\n      {command.summary}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    match args:
        case ["--help" | "-h" | "help"]:
            print(render_help())
            return 0
        case ["--version"]:
            print(f"nmemory {__version__} (schema v{SCHEMA_VERSION})")
            return 0
        case [noun, verb, *rest] if (noun, verb) in COMMANDS:
            try:
                refuse(os.environ, READS)
                system1_refuse(os.environ, SYSTEM1_RENAMED)
                # `mcp serve` starts, and its server refuses each call itself.
                if (noun, verb) != ("mcp", "serve") and inherited_session():
                    raise ConfigError(INHERITED_REFUSAL)
                if (noun, verb) != ("mcp", "serve") and capture_only_policy():
                    raise ConfigError(CAPTURE_ONLY_REFUSAL)
                COMMANDS[(noun, verb)].handler(rest)
            except (RenamedVariableError, System1RenamedVariableError) as error:
                sys.stderr.write(f"nmemory: refused: {error}\n")
                return 2
            except nott_database.RefusedError as error:
                message = " ".join(str(error).splitlines())
                sys.stderr.write(f"nmemory: {noun} {verb}: refused: {message}\n")
                return 2
            except (
                EmbeddingError,
                ContextGitError,
                ContextIoError,
                DatabaseError,
                nott_database.Error,
                OSError,
            ) as error:
                message = " ".join(str(error).splitlines())
                sys.stderr.write(f"nmemory: {noun} {verb}: {message}\n")
                return 1
            except subprocess.SubprocessError as error:
                # These exceptions include argv in their string representation.
                # Report the failure without exposing the child process arguments.
                if isinstance(error, subprocess.TimeoutExpired):
                    message = f"subprocess timed out after {error.timeout} seconds"
                elif isinstance(error, subprocess.CalledProcessError):
                    message = f"subprocess exited with status {error.returncode}"
                else:
                    message = f"subprocess failed: {type(error).__name__}"
                sys.stderr.write(f"nmemory: {noun} {verb}: {message}\n")
                return 1
            except (NMemoryError, ContextIndexError) as error:
                message = " ".join(str(error).splitlines())
                sys.stderr.write(f"nmemory: {noun} {verb}: refused: {message}\n")
                return 2
            return 0
        case []:
            sys.stderr.write(f"nmemory: {USAGE}\n")
            return 2
        case _:
            sys.stderr.write(
                f"unknown command: nmemory {' '.join(args[:2])}\n{USAGE}\n"
            )
            return 2


if __name__ == "__main__":
    sys.exit(main())
