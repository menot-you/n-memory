"""`nmemory anchor list|check`: the live anchor index, and the judge of code anchors."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path as _Path
from typing import Any

from ..anchor import parse_anchor
from ..anchors import audit_all, drain_spool, spool_dir
from ..capsule import headline_of
from ..queries.sidecars import view_sidecars
from ..subproc import REPO_ROOT_S, run_with_timeout
from .common import open_store_from_args, parse_command


def cmd_anchor_list(argv: list[str]) -> None:
    args = parse_command("anchor", "list", argv)
    store = open_store_from_args(args)
    listed = store.capsules.list()
    sidecars = view_sidecars(store.db)
    rows: list[dict[str, Any]] = []
    for stored in listed:
        tainted = stored.capsule.instruction_taint
        parsed_anc = parse_anchor(stored.capsule.provenance.anchor)
        sidecar = sidecars[stored.id]
        rebind = sidecar.anchor_effective
        kind = sidecar.kind

        row: dict[str, Any] = {
            "id": stored.id,
            "anchorPath": parsed_anc.path if parsed_anc else None,
        }
        if rebind is not None:
            row["anchorEffective"] = rebind
        row["kind"] = kind
        row["authorityClass"] = stored.capsule.authority_class.value
        row["instructionTaint"] = tainted
        if not tainted:
            row["headline"] = headline_of(stored.capsule.content)
        rows.append(row)
    print(json.dumps(rows, indent=2, ensure_ascii=False))


def cmd_anchor_check(argv: list[str]) -> None:
    args = parse_command("anchor", "check", argv)
    if not args.drain and not args.all:
        print("nmemory anchor check needs --drain and/or --all", file=sys.stderr)
        sys.exit(2)
    if not args.project:
        print(
            "nmemory anchor check records verdicts: pass --project <scope> (anchors are "
            "repo-relative; without the fence other scopes are judged against this tree)",
            file=sys.stderr,
        )
        sys.exit(2)
    if args.root:
        root = _Path(args.root)
    else:
        top = run_with_timeout(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=REPO_ROOT_S,
            check=False,
        )
        if top.returncode != 0:
            print(
                "nmemory anchor check: not inside a git repo; pass --root",
                file=sys.stderr,
            )
            sys.exit(2)
        root = _Path(top.stdout.strip())
    store = open_store_from_args(args)
    now_dt = datetime.now(UTC)
    out: dict[str, Any] = {"root": str(root), "project": args.project}
    if args.drain:
        spool = _Path(args.spool) if args.spool else spool_dir()
        d = drain_spool(store, spool, root, now=now_dt, project=args.project)
        out["drain"] = {
            "events": d.events,
            "judged": d.judged,
            "foreign": d.foreign,
            "malformed": d.malformed,
        }
    if args.all:
        a = audit_all(store, root, now=now_dt, project=args.project)
        out["audit"] = {
            "anchored": a.anchored,
            "ok": a.ok,
            "dead": a.dead,
            "moved": a.moved,
            "unverifiable": a.unverifiable,
            "baseline": a.baseline,
            "changed": a.changed,
            "dead_rate_pct": a.dead_rate_pct,
        }
    if args.json:
        print(json.dumps(out, indent=2))
    else:
        for k, v in out.items():
            print(f"{k}: {v}")
