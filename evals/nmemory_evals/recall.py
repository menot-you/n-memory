"""LongMemEval retrieval recall, precision, and categorization."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from nmemory_evals.score import HitRate, RecallOutcome, rate


@dataclass(frozen=True)
class ScoredQuestion:
    question_id: str
    question_type: str
    evidence_sessions: list[str]
    ranked_sessions: list[list[str]]

    def retrieved(self, k: int) -> list[str]:
        """The sessions carried by the first k returned capsules."""
        flat: list[str] = []
        for sessions in self.ranked_sessions[:k]:
            flat.extend(sessions)
        return flat

    def recall_any(self, k: int) -> bool:
        """At least one annotated evidence session came back inside the first k."""
        ret = self.retrieved(k)
        return any(ev in ret for ev in self.evidence_sessions)

    def recall_all(self, k: int) -> bool:
        """Every annotated evidence session came back inside the first k.

        A question with no annotated evidence scores False here rather than
        vacuously true.
        """
        ret = self.retrieved(k)
        return bool(self.evidence_sessions) and all(
            ev in ret for ev in self.evidence_sessions
        )

    def precision(self, k: int) -> float | None:
        """The share of what came back inside the first k that is annotated evidence.

        The denominator is WHAT CAME BACK, never k: a store that learns to
        return less moves this number, where dividing by k would punish it.
        A question that returned nothing has no precision (None), not a zero.
        A session carried by two capsules counts once in the numerator and
        twice in the denominator. This is not the PrecisionMemBench
        precision, which judges answer text; this one judges retrieval only.
        """
        ret = self.retrieved(k)
        if not ret:
            return None
        return len(set(ret) & set(self.evidence_sessions)) / len(ret)


@dataclass(frozen=True)
class ScoredRow(ScoredQuestion):
    """One persisted question: the scored question plus the outcome the store declared."""

    outcome: RecallOutcome


@dataclass(frozen=True)
class RecallAtK:
    k: int
    session_recall_any: HitRate
    session_recall_all: HitRate


@dataclass(frozen=True)
class PrecisionAtK:
    k: int
    mean: float
    scored: int
    nothing_returned: int


@dataclass(frozen=True)
class CategoryRecall:
    question_type: str
    scored: int
    recall: list[RecallAtK]


def table(questions: Sequence[ScoredQuestion], k_values: list[int]) -> list[RecallAtK]:
    """Recall at every declared k over every scored question."""
    rows: list[RecallAtK] = []
    total = len(questions)
    for k in k_values:
        any_hits = sum(1 for q in questions if q.recall_any(k))
        all_hits = sum(1 for q in questions if q.recall_all(k))
        rows.append(
            RecallAtK(
                k=k,
                session_recall_any=HitRate(
                    hits=any_hits,
                    total=total,
                    rate=rate(any_hits, total),
                ),
                session_recall_all=HitRate(
                    hits=all_hits,
                    total=total,
                    rate=rate(all_hits, total),
                ),
            )
        )
    return rows


def precision(
    questions: Sequence[ScoredQuestion], k_values: list[int]
) -> list[PrecisionAtK]:
    """Mean precision@k over the questions that returned something.

    A question that returned nothing is counted in `nothing_returned` and
    left out of the mean; an empty mean is 0.0, never a NaN.
    """
    rows: list[PrecisionAtK] = []
    for k in k_values:
        values = [p for q in questions if (p := q.precision(k)) is not None]
        rows.append(
            PrecisionAtK(
                k=k,
                mean=sum(values) / len(values) if values else 0.0,
                scored=len(values),
                nothing_returned=len(questions) - len(values),
            )
        )
    return rows


def by_category(
    questions: Sequence[ScoredQuestion], k_values: list[int]
) -> list[CategoryRecall]:
    """Split recall by question categories, ordered by first appearance."""
    categories: list[str] = []
    for question in questions:
        if question.question_type not in categories:
            categories.append(question.question_type)

    result: list[CategoryRecall] = []
    for category in categories:
        members = [q for q in questions if q.question_type == category]
        result.append(
            CategoryRecall(
                question_type=category,
                scored=len(members),
                recall=table(members, k_values),
            )
        )
    return result
