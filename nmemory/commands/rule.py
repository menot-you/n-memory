"Rule commands: write the rule files by hand."

from __future__ import annotations

import sys
from pathlib import Path

from ..rules import write_rules
from .common import open_store_from_args, parse_command


def cmd_rule_write(argv: list[str]) -> None:
    args = parse_command("rule", "write", argv)
    # A writer: it stores each System1 answer it receives. The destinations are the
    # destinations under ~/.nott; the HTTP daemon runs the same generator by itself.
    store = open_store_from_args(args)
    report = write_rules(store, Path.home())
    for path, count in report.files.items():
        state = "written" if path in report.written else "unchanged"
        print(f"{path}: {count} rules, {state}")
    if report.record_failure is not None:
        sys.stderr.write(
            "nmemory: rule write: the files are written, the System1 answers are not "
            f"stored ({report.record_failure}); they are asked again next time\n"
        )
    for path, cause in report.errors.items():
        sys.stderr.write(f"nmemory: rule write: {path}: not written: {cause}\n")
    if report.fallback:
        sys.stderr.write(
            f"nmemory: rule write: System1 gave no decisive answer "
            f"({report.system1_failure}); {len(report.fallback)} rules are in the "
            "project file by default\n"
        )
    if report.dropped:
        sys.stderr.write(
            f"nmemory: rule write: {len(report.dropped)} owner rules have a project "
            f"id with no file name: {' '.join(report.dropped)}\n"
        )
    if report.errors:
        # D18: a destination that was not written is an operational failure.
        sys.exit(1)
