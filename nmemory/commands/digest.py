"""`nmemory digest show`: the store digest, its write-read census and its delivery ruler."""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from typing import NoReturn

import nott_database
from nott_database import DatabaseError

from ..queries.census import (
    WORK_KINDS,
    DeliveryCensus,
    WorkCounts,
    delivery_census,
    write_read_census_partition,
)
from ..queries.sidecars import CapsuleSidecar, digest_sidecars
from ..queries.staged import (
    STAGED_HORIZON_DAYS,
    StagedCensus,
    staged_census,
    staged_triage,
)
from .common import open_store_from_args, parse_command


def write_read_lines(
    sidecars: dict[str, CapsuleSidecar],
    census: dict[str, tuple[int, int]],
    rewrite_census: dict[str, tuple[int, int]],
    prefix: str | None,
) -> list[str]:
    """The store headline and the write→read census of the last 7 days.

    The census sums two populations with different OWNERS: one third of what
    is written is `task`/`dod` (720 of 2117 in 7 days, measured 2026-09-15)
    and those close with a verdict and an outcome, not by reading. Measured,
    both halves are EQUALLY unserved, and delivery can only move one. `epic`
    and `plan` fall on the knowledge side and also do not close by reading —
    26 of 1397, ~2%, stated here so the label does not lie.
    """
    recalled = sum(row.recall_count > 0 for row in sidecars.values())
    lines = [
        f"nMEMORY Digest: {len(sidecars)} capsules stored in SQLite · {recalled} ever recalled"
    ]
    written = sum(counts[0] for counts in census.values())
    unread = sum(counts[1] for counts in census.values())
    fraction = f"{100 * unread / written:.0f}%" if written else "no writes"
    lines.append(f"nMEMORY write→read, last 7 days (project {prefix or 'all'}):")
    lines.append(f"  {written} capsules written · {unread} never recalled ({fraction})")
    if rewrite_census:
        lines.append(
            f"  {sum(w for w, _ in rewrite_census.values())} rewrites (each "
            f"supersedes an older capsule) · "
            f"{sum(u for _, u in rewrite_census.values())} never served"
        )
    work_written = sum(
        counts[0] for kind, counts in census.items() if kind in WORK_KINDS
    )
    work_unread = sum(
        counts[1] for kind, counts in census.items() if kind in WORK_KINDS
    )
    know_written = written - work_written
    know_unread = unread - work_unread
    if work_written and know_written:
        know_pct = f"{100 * know_unread / know_written:.0f}%"
        work_pct = f"{100 * work_unread / work_written:.0f}%"
        lines.append(
            f"  knowledge {know_written} written · {know_unread} never "
            f"served ({know_pct}) — this is what delivery reaches"
        )
        lines.append(
            f"  work (task/dod) {work_written} written · {work_unread} "
            f"never served ({work_pct}) — closes with a verdict or a witness"
        )
    if census:
        lines.append(
            "  written/never recalled: "
            + " · ".join(
                f"{kind} {counts[0]}/{counts[1]}" for kind, counts in census.items()
            )
        )
    return lines


def delivery_lines(delivery: DeliveryCensus, prefix: str | None) -> list[str]:
    """U3 (nmemory-curator): the DELIVERY ruler, alongside the arrival ruler.

    The census counts “never served” and bundles three different things into
    a single number; these lines separate them by name and identify the
    campaign's sole target (`undelivered`).
    """
    if not delivery.knowledge:
        return []
    total = delivery.total()
    lines = [
        f"nMEMORY delivery, last 7 days (project {prefix or 'all'}):",
        (
            f"  undelivered {total.undelivered} · no session "
            f"{total.no_session} · read by question {total.asked} · "
            f"delivered {total.delivered} · retired unread "
            f"{total.retired_unread}"
        ),
    ]
    pending = sorted(
        (
            (scope, counts.undelivered)
            for scope, counts in delivery.knowledge.items()
            if counts.undelivered
        ),
        key=lambda row: (-row[1], row[0]),
    )[:3]
    if pending:
        lines.append(
            "  undelivered by scope: "
            + " · ".join(f"{scope} {n}" for scope, n in pending)
        )
    return lines


def staged_lines(
    staged: StagedCensus, triage: list[tuple[str, str, float, float | None]]
) -> list[str]:
    """The staged queue, with its three counts kept SEPARATE.

    Treating curator evidence as a queue was deleting proof to lower a number.
    What the cheap model OBSERVED follows, “nao_decide” first because that is
    what needs a human. None of this signs off — ratifying is
    `nmemory_proposal_review`.
    """
    if not (staged.awaiting or staged.evidence):
        return []
    line = (
        f"  staged queue: {staged.awaiting} awaiting judgement · "
        f"{staged.expired} expired "
        f"(>{STAGED_HORIZON_DAYS} d)"
    )
    if staged.oldest_days is not None:
        line += f" · the oldest is {staged.oldest_days:.1f} d old"
    lines = [line]
    if staged.evidence:
        lines.append(f"  {staged.evidence} curator evidence (fenced by design)")
    for oid, choice, p, conf in triage:
        if choice == "nao_decide":
            lines.append(
                f"    system1: {oid} does not decide (the two orders disagree)"
            )
        else:
            c = "·" if conf is None else f"conf {conf:.2f}"
            lines.append(f"    system1: {oid} {choice} (p {p:.2f} · {c})")
    return lines


def work_lines(work: WorkCounts) -> list[str]:
    """Work is REPORTED, with no target."""
    if not (work.dod_total or work.task_total):
        return []
    return [
        (
            f"  work (reported, all time): {work.dod_without_verdict} dod without a "
            f"verdict out of {work.dod_total} · {work.task_open} task open "
            f"out of {work.task_total}"
        )
    ]


def _refuse(error: Exception) -> NoReturn:
    if isinstance(error, nott_database.RefusedError):
        raise error
    print(f"nmemory: digest: {type(error).__name__}", file=sys.stderr)
    sys.exit(1)


def cmd_digest_show(argv: list[str]) -> None:
    args = parse_command("digest", "show", argv)
    store = open_store_from_args(args, read_census=True)
    prefix = args.project_prefix or args.project
    # Each block prints before the next query runs: a failure later keeps the
    # lines already written.
    try:
        sidecars = digest_sidecars(store.db, project_prefix=prefix)
        week = datetime.now(UTC) - timedelta(days=7)
        census, rewrite_census = write_read_census_partition(store.db, week, prefix)
    except (DatabaseError, nott_database.Error, OSError) as error:
        _refuse(error)
    for line in write_read_lines(sidecars, census, rewrite_census, prefix):
        print(line)
    try:
        delivery = delivery_census(
            store.db, datetime.now(UTC) - timedelta(days=7), prefix
        )
    except (DatabaseError, nott_database.Error, OSError) as error:
        _refuse(error)
    for line in delivery_lines(delivery, prefix):
        print(line)
    try:
        staged = staged_census(store.db, prefix, subtree=True, now=datetime.now(UTC))
        triage = staged_triage(store.db, prefix, subtree=True, limit=5)
    except (DatabaseError, nott_database.Error, OSError) as error:
        _refuse(error)
    for line in staged_lines(staged, triage) + work_lines(delivery.work):
        print(line)
