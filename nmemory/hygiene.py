"""U3: store hygiene measured and DELIVERED as a capsule, once per day.

The numbers all already existed — `recall_misses`, `read_traffic`, and the
write/read census — and none of the four units that already run brings them to the
owner: they write verification events, not capsules. The only automatic writer
of capsules is the curator, and so this module follows the SAME path
(`ingest`, never `append`): `append` skips taint scanning, dedup, and auditing.

One SELECT, one formatter, and one call to `ingest`. No report classes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .capsule import AuthorityClass, format_rfc3339
from .classify import ClassificationScope
from .extract import CandidateKind
from .ingest import IngestDefaults, IngestRequest, ingest
from .queries.census import write_read_census
from .queries.hygiene import measure_counts
from .store import Store

#: The day `read_traffic` came into existence. Capsules born BEFORE
#: this have no recorded reads from any surface, so “never served”
#: is ambiguous for them — the report STATES THIS with the number rather than silently
#: counting them as unserved.
READ_TRAFFIC_EPOCH = "2026-07-31"

#: The report's anchor is the meter itself: anyone rechecking reads the SQL from here.
ANCHOR = "mcps/nmemory/nmemory/queries/hygiene.py:1"

WINDOW_DAYS = 7


@dataclass(frozen=True)
class Hygiene:
    day: str
    abstain: int
    missing_evidence: int
    context_calls: int
    context_empty: int
    prior_calls: int
    prior_empty: int
    pre_traffic_capsules: int
    #: `{kind: (born, never served)}` in the `WINDOW_DAYS`-day cohort.
    census: dict[str, tuple[int, int]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "day": self.day,
            "abstain": self.abstain,
            "missing_evidence": self.missing_evidence,
            "context_calls": self.context_calls,
            "context_empty": self.context_empty,
            "prior_calls": self.prior_calls,
            "prior_empty": self.prior_empty,
            "pre_traffic_capsules": self.pre_traffic_capsules,
            "census": {k: list(v) for k, v in self.census.items()},
        }


def _pct(part: int, total: int) -> float | None:
    """The rate, or `None` when there were no calls — never an invented zero."""
    return None if total == 0 else round(100.0 * part / total, 1)


def measure(store: Store, now: datetime) -> Hygiene:
    week = now - timedelta(days=WINDOW_DAYS)
    params = {
        "day": now.date().isoformat(),
        "week": format_rfc3339(week),
        "prior": format_rfc3339(now - timedelta(days=2 * WINDOW_DAYS)),
        "epoch": READ_TRAFFIC_EPOCH,
    }
    row = measure_counts(store.db, params)
    return Hygiene(
        day=params["day"],
        abstain=int(row[0]),
        missing_evidence=int(row[1]),
        context_calls=int(row[2]),
        context_empty=int(row[3]),
        prior_calls=int(row[4]),
        prior_empty=int(row[5]),
        pre_traffic_capsules=int(row[6]),
        census=write_read_census(store.db, week),
    )


def render(h: Hygiene) -> str:
    rate = _pct(h.context_empty, h.context_calls)
    prior = _pct(h.prior_empty, h.prior_calls)
    if rate is None:
        traffic = (
            f"nmemory_context_show was not called in the last {WINDOW_DAYS} days "
            "(no call recorded)."
        )
    else:
        traffic = (
            f"nmemory_context_show served zero capsules in {h.context_empty} of "
            f"{h.context_calls} calls in the last {WINDOW_DAYS} days ({rate}%)"
        )
        if prior is None:
            traffic += "; the previous 7 days had no calls, so there is no change."
        else:
            traffic += (
                f"; in the previous {WINDOW_DAYS} days it was {prior}% "
                f"({rate - prior:+.1f} points)."
            )
    born = sum(total for total, _ in h.census.values())
    unserved = sum(never for _, never in h.census.values())
    by_kind = ", ".join(
        f"{kind} {never}/{total}" for kind, (total, never) in sorted(h.census.items())
    )
    cohort = (
        f"{WINDOW_DAYS}-day cohort: {born} capsules born, {unserved} still "
        f"never served" + (f" ({by_kind})." if by_kind else ".")
    )
    alarm = (
        f"Store hygiene on {h.day}: {h.abstain + h.missing_evidence} "
        f"recalls without an answer today (abstain {h.abstain}, "
        f"missing_evidence {h.missing_evidence}) — something was asked and nothing came back."
    )
    ambiguous = (
        f"The {h.pre_traffic_capsules} capsules born before "
        f"{READ_TRAFFIC_EPOCH} are left out of this count: `read_traffic` "
        "did not exist, so «never served» is not decidable for them."
    )
    return f"{alarm}\n{traffic}\n{cohort}\n{ambiguous}"


def proof_hint(h: Hygiene) -> str:
    return (
        f"nmemory quality measure --json --dry-run (day {h.day}; the SELECT is "
        f"`MEASURE_SQL` in {ANCHOR} and the census is `write_read_census` in queries/census.py)"
    )


def _existente(store: Store, source: str) -> str | None:
    """The capsule already written for this provenance, or `None`.

    One function rather than two copies of the SELECT: the call OUTSIDE the transaction
    is the cheap one, avoiding opening a write needlessly; the one INSIDE, after the lock,
    is the one that decides. If they diverged, the race that the second exists to catch would slip through.
    """
    return store.capsules.id_by_provenance_source(source)


def publish(
    store: Store, h: Hygiene, project_id: str, now: datetime
) -> tuple[str, bool]:
    """The day's capsule, exactly one: returns `(id, written_now)`.

    Idempotence is by PROVENANCE (`hygiene:<day>`) and not by content dedup:
    two runs on the same day measure numbers that may have changed between
    them, and similarity dedup would then let a second capsule slip through.
    """
    source = f"hygiene:{h.day}"
    existing = _existente(store, source)
    if existing is not None:
        return existing, False
    with store.db.write_boundary():
        # The SELECT above runs OUTSIDE the transaction: two concurrent runs on
        # the same day both pass through it empty, and if any `recall_miss`
        # arrives between the two measurements the content differs, similarity
        # dedup does not join them, and TWO capsules end up with the same
        # provenance. There is no unique index on provenance to prevent it.
        # Caught by the independent review (2026-09-20): the asserted idempotence
        # was sequential, not concurrent.
        #
        # The write lock is taken BEFORE the re-read, and it is the store's one
        # writer lock: the second run waits for the first to commit, re-reads
        # inside its own write, and exits without writing. A run dying midway
        # releases it with its transaction.
        ja_escrita = _existente(store, source)
        if ja_escrita is not None:
            return ja_escrita, False
        outcome = ingest(
            store,
            IngestRequest(
                content=render(h),
                source=source,
                anchor=ANCHOR,
                project_id=project_id,
                authority_class=AuthorityClass.AGENT_INFERRED,
            ),
            IngestDefaults(project_id=project_id),
            now,
        )
        store.set_classification(
            outcome.id, CandidateKind.FACT, ClassificationScope.PROJECT, now
        )
        store.epistemics.set_epistemic(outcome.id, "observed", proof_hint(h), None, now)
    return outcome.id, not outcome.deduped
