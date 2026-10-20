"Rule commands: write the rule files."

from __future__ import annotations

import sys
from pathlib import Path

from ..ingest import effective_defaults
from ..project import canonical_project
from ..renamed import env
from ..rules import write_rules
from .common import open_store_from_args, parse_command


def cmd_rule_write(argv: list[str]) -> None:
    args = parse_command("rule", "write", argv)
    root = None if args.root is None else Path(args.root).resolve()
    project = canonical_project(
        args.project
        or env("NOTT_NMEMORY_PROJECT")
        or effective_defaults(None, Path.cwd().name, None).project_id
    )
    # A writer: it stores each System1 scope answer it receives.
    store = open_store_from_args(args)
    report = write_rules(store, project, root, Path.home())
    print(f"{report.user_file}: {report.user_rules} rules")
    if report.project_file is not None:
        print(f"{report.project_file}: {report.project_rules} rules")
    if report.record_failure is not None:
        sys.stderr.write(
            "nmemory: rule write: the files are written, the scope answers are not "
            f"stored ({report.record_failure}); they are asked again next time\n"
        )
    for name in report.skipped:
        sys.stderr.write(f"nmemory: rule write: {name} is not UTF-8; skipped\n")
    if report.fallback:
        sys.stderr.write(
            f"nmemory: rule write: System1 gave no scope ({report.system1_failure}); "
            f"{len(report.fallback)} rules are in project scope by default\n"
        )
