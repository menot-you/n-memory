"""`nmemory github sync`: mirror the nMEMORY planning graph to GitHub issues."""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn

from ..config import ConfigError, github_table
from ..github import (
    DEFAULT_MAX_WRITES,
    GITHUB_SYNC_DEFAULT_CONFIG,
    EffortError,
    SyncConfig,
    apply_exit_code,
    load_config,
    parse_efforts,
    sync,
    verify,
)

# `ConfigError` is bound to the `.config` one above; the github one is another
# type and needs its own name.
from ..github import ConfigError as GithubConfigError
from ..github_client import SubprocessGh
from ..github_plan import verify_exit_code
from .common import open_store_from_args, parse_command


def sync_config(
    args: argparse.Namespace, base_cfg: SyncConfig | None, host_tbl: dict[str, Any]
) -> SyncConfig | str:
    """The run's configuration, or the refusal to print.

    ONE file wins, wholly (the caller loads it): mixing field by field allowed
    taking the `repo` from one file and the FENCE from the other. Flags win
    over whichever file wins. `--effort` REPLACES the declared list, never
    merges into it: union is the unsafe direction — whoever writes
    `--effort cap-1` to touch one epic would silently write over every other.
    """
    try:
        if args.effort:
            efforts: tuple[str, ...] = (args.effort.strip(),)
        elif base_cfg is not None:
            efforts = base_cfg.efforts
        else:
            efforts = parse_efforts(host_tbl.get("efforts"), "memory.toml")
    except GithubConfigError as e:
        return f"nmemory: github-sync: {e}"
    fields: dict[str, str] = {}
    for name, flag in (
        ("repo", "--repo"),
        ("label", "--label"),
        ("project", "--project"),
    ):
        base_value: str | None
        if base_cfg is not None:
            value = getattr(base_cfg, name)
            base_value = str(value) if value and value.strip() else None
        else:
            from_host = host_tbl.get(name)
            base_value = str(from_host) if isinstance(from_host, str) else None
        chosen = getattr(args, name) or base_value
        if not chosen or not chosen.strip():
            return f"{flag} is required (pass it, or set it in a --config file)"
        fields[name] = chosen.strip()
    return SyncConfig(efforts=efforts, **fields)


def _fail(message: str) -> NoReturn:
    print(message, file=sys.stderr)
    sys.exit(1)


def cmd_github_sync(argv: list[str]) -> None:
    args = parse_command("github", "sync", argv)
    # If the repository's file exists (or `--config`), it wins; otherwise
    # `~/.nott/memory.toml`, which is host-local.
    explicit_config = Path(args.config) if args.config else None
    default_config = Path(GITHUB_SYNC_DEFAULT_CONFIG)
    cfg_path = (
        explicit_config
        if explicit_config
        else (default_config if default_config.exists() else None)
    )
    try:
        base_cfg = load_config(cfg_path) if cfg_path else None
        host_tbl: dict[str, Any] = {} if base_cfg else github_table()
    except (ConfigError, GithubConfigError) as e:
        _fail(f"nmemory: github-sync: {e}")
    config = sync_config(args, base_cfg, host_tbl)
    if isinstance(config, str):
        _fail(config)

    max_writes = args.max_writes if args.max_writes is not None else DEFAULT_MAX_WRITES
    if max_writes <= 0:
        _fail("--max-writes must be a positive integer")
    # Before opening the store: this is configuration, and refusing early
    # keeps the message clean of any connection failure that might follow.
    if not args.verify and not config.efforts:
        _fail(
            "nmemory: github-sync: no effort declared — pass "
            "--effort <cap-id> or declare [github].efforts in "
            "~/.nott/memory.toml. Without a fence this run would mirror the "
            "whole planning graph."
        )

    store = open_store_from_args(args)
    gh = SubprocessGh()
    if args.verify:
        # The global diagnostic still stands: it writes nothing, and it is the
        # only way to still measure the whole graph.
        try:
            plan = verify(store, gh, config)
        except EffortError as e:
            _fail(f"nmemory: github-sync: {e}")
        print(plan.render())
        code = verify_exit_code(plan)
    else:
        try:
            summary = sync(
                store, gh, config, max_writes=max_writes, now=datetime.now(UTC)
            )
        except EffortError as e:
            _fail(f"nmemory: github-sync: {e}")
        print(summary.render())
        code = apply_exit_code(summary)
    if code != 0:
        sys.exit(code)
