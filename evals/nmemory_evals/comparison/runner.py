"""Comparative benchmark runner across multiple agent memory architectures."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from nmemory_evals.comparison.base import (
    BaseMemoryAdapter,
    BenchmarkItem,
    QueryObservation,
    SystemScore,
)
from nmemory_evals.e1 import load_gold_set


@dataclass(frozen=True)
class ComparisonReport:
    total_items: int
    systems: list[SystemScore]

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_items": self.total_items,
            "systems": [s.to_dict() for s in self.systems],
        }


def evaluate_system(
    adapter: BaseMemoryAdapter, items: list[BenchmarkItem], k: int = 5
) -> SystemScore:
    """Executes a full ingestion and query evaluation cycle on a single memory system."""
    # 1. Measure ingestion
    ingest_latency_ms = adapter.ingest_all(items)
    avg_ingest_latency_ms = ingest_latency_ms / len(items) if items else 0.0

    # 2. Measure retrieval for all 41 items
    observations: list[QueryObservation] = []
    for item in items:
        top_k_ids, outcome, latency_ms = adapter.retrieve(item.query_terms, k=k)
        rank: int | None = None
        if item.id in top_k_ids:
            rank = top_k_ids.index(item.id) + 1

        observations.append(
            QueryObservation(
                item_id=item.id,
                found_at_rank=rank,
                retrieved_ids=top_k_ids,
                latency_ms=latency_ms,
                outcome=outcome,
            )
        )

    # 3. Calculate statistics
    total = len(observations)
    r1 = sum(1 for o in observations if o.found_at_rank == 1) / total if total else 0.0
    r3 = (
        sum(
            1
            for o in observations
            if o.found_at_rank is not None and o.found_at_rank <= 3
        )
        / total
        if total
        else 0.0
    )
    r5 = (
        sum(
            1
            for o in observations
            if o.found_at_rank is not None and o.found_at_rank <= 5
        )
        / total
        if total
        else 0.0
    )
    grounded = (
        sum(1 for o in observations if o.outcome == "grounded") / total
        if total
        else 0.0
    )
    avg_query_lat = sum(o.latency_ms for o in observations) / total if total else 0.0

    # Per category scores
    by_cat: dict[str, dict[str, float]] = {}
    categories = sorted({item.category for item in items})
    for cat in categories:
        cat_obs = [
            o for o, it in zip(observations, items, strict=True) if it.category == cat
        ]
        cat_tot = len(cat_obs)
        by_cat[cat] = {
            "total": float(cat_tot),
            "recall_at_1": sum(1 for o in cat_obs if o.found_at_rank == 1) / cat_tot
            if cat_tot
            else 0.0,
            "recall_at_3": sum(
                1
                for o in cat_obs
                if o.found_at_rank is not None and o.found_at_rank <= 3
            )
            / cat_tot
            if cat_tot
            else 0.0,
            "recall_at_5": sum(
                1
                for o in cat_obs
                if o.found_at_rank is not None and o.found_at_rank <= 5
            )
            / cat_tot
            if cat_tot
            else 0.0,
        }

    return SystemScore(
        system=adapter.system,
        total_items=total,
        recall_at_1=r1,
        recall_at_3=r3,
        recall_at_5=r5,
        grounded_rate=grounded,
        avg_query_latency_ms=avg_query_lat,
        avg_ingest_latency_ms=avg_ingest_latency_ms,
        provenance_preserved=adapter.provenance_preserved,
        tamper_evident_audit=adapter.tamper_evident_audit,
        explicit_abstain_support=adapter.explicit_abstain_support,
        category_scores=by_cat,
        queries=observations,
        receipt=adapter.receipt,
    )


def run_comparison_benchmark(
    adapters: list[BaseMemoryAdapter],
    items: list[BenchmarkItem] | None = None,
    k: int = 5,
) -> ComparisonReport:
    """Executes the comparative benchmark over the 41-item Gold Set across all specified adapters."""
    dataset = items
    if dataset is None:
        e1_items = load_gold_set()
        dataset = [
            BenchmarkItem(
                id=item.id,
                category=item.category,
                fact=item.fact,
                query_terms=item.query_terms,
                target=item.target,
                kind=item.kind,
            )
            for item in e1_items
        ]

    system_scores: list[SystemScore] = []
    for adapter in adapters:
        try:
            score = evaluate_system(adapter, dataset, k=k)
            system_scores.append(score)
        finally:
            adapter.close()

    return ComparisonReport(total_items=len(dataset), systems=system_scores)
