"""Deterministic n1 metrics over programmatic goldens."""

from __future__ import annotations

from collections.abc import Sized
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class RecallOutcome(StrEnum):
    GROUNDED = "grounded"
    MISSING_EVIDENCE = "missing_evidence"
    ABSTAIN = "abstain"


@dataclass(frozen=True)
class RecallObservation:
    qid: str
    outcome: RecallOutcome
    result_ids: list[str]


@dataclass(frozen=True)
class QuestionCounts:
    answerable: int
    out_of_world: int


@dataclass(frozen=True)
class HitRate:
    hits: int
    total: int
    rate: float


@dataclass(frozen=True)
class AbstainRate:
    abstains: int
    total: int
    rate: float


@dataclass(frozen=True)
class Abstention:
    """How the abstention probes (`_abs`) came out: right when the store held back."""

    true_abstain: int
    false_abstain: int


def is_true_abstain(outcome: RecallOutcome, returned: Sized) -> bool:
    """A probe is answered right when the store says abstain OR returns nothing.

    Anything returned is a false abstain, whatever the outcome says: the probe
    asks about something the history never held, so every result is noise.
    """
    return outcome is RecallOutcome.ABSTAIN or len(returned) == 0


@dataclass(frozen=True)
class Scores:
    bench: str
    seed: int
    k: int
    questions: QuestionCounts
    grounded_hit_at_k: HitRate
    true_abstain: AbstainRate
    false_abstain: AbstainRate
    headline: str


class ScoreError(Exception):
    """Raised when score calculation encounters invalid inputs."""


def rate(numerator: int, denominator: int) -> float:
    """A hit rate, and zero for an empty denominator rather than a NaN."""
    if denominator == 0:
        return 0.0
    return float(numerator) / float(denominator)


def calculate(
    seed: int,
    k: int,
    questions: list[Any],
    observations: list[RecallObservation],
) -> Scores:
    """Calculate n1 scores matching the Rust oracle contract."""
    by_qid: dict[str, RecallObservation] = {}
    for obs in observations:
        if obs.qid in by_qid:
            raise ScoreError(f"score input has duplicate observation {obs.qid}")
        by_qid[obs.qid] = obs

    answerable = sum(1 for q in questions if q.answerable)
    out_of_world = len(questions) - answerable
    grounded_hits = 0
    true_abstains = 0
    false_abstains = 0

    for question in questions:
        obs_item = by_qid.get(question.qid)
        if obs_item is None:
            raise ScoreError(f"score input is missing observation {question.qid}")

        if question.answerable:
            if obs_item.outcome != RecallOutcome.GROUNDED:
                false_abstains += 1
            hit = obs_item.outcome == RecallOutcome.GROUNDED and any(
                res_id in question.golden_capsule_ids
                for res_id in obs_item.result_ids[:k]
            )
            if hit:
                grounded_hits += 1
        elif obs_item.outcome == RecallOutcome.ABSTAIN:
            # The n1 contract is the owner's live Rust oracle (monorepo
            # mcps/nmemory/evals/src/score.rs:105): it counts ONLY outcome ==
            # Abstain. The `is_true_abstain` helper ("or empty return") is the
            # ARENA A.3 definition, which lives in that arena's runner — there
            # the empty return counts; here it does not.
            true_abstains += 1

    return Scores(
        bench="n1",
        seed=seed,
        k=k,
        questions=QuestionCounts(
            answerable=answerable,
            out_of_world=out_of_world,
        ),
        grounded_hit_at_k=HitRate(
            hits=grounded_hits,
            total=answerable,
            rate=rate(grounded_hits, answerable),
        ),
        true_abstain=AbstainRate(
            abstains=true_abstains,
            total=out_of_world,
            rate=rate(true_abstains, out_of_world),
        ),
        false_abstain=AbstainRate(
            abstains=false_abstains,
            total=answerable,
            rate=rate(false_abstains, answerable),
        ),
        headline="true_abstain.rate",
    )
