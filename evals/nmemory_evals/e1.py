"""E1 Evaluation Suite — Complete Capture and Retention (Gold Set 41-item).

Measures whether nMEMORY retains and retrieves, without losses, the set of 41
atomic statements derived from the production friction reports.
Drives the SUT across its public stdio MCP interface.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nmemory_evals.driver import (
    Driver,
    DriverError,
    EvaluationMode,
    prepare_database,
    tool_payload,
)

GOLD_SET_PATH = Path(__file__).parent / "e1_gold_set.json"
E1_PROJECT = "pynott"


@dataclass(frozen=True)
class E1Item:
    id: str
    category: str
    fact: str
    query_terms: list[str]
    target: str
    kind: str


@dataclass(frozen=True)
class E1QueryReport:
    item_id: str
    category: str
    outcome: str
    retrieved_ids: list[str]
    matched_terms: list[str]
    expected_capsule_id: str
    found_at_rank: int | None
    hit_at_1: bool
    hit_at_3: bool
    hit_at_5: bool


@dataclass(frozen=True)
class E1BenchmarkReport:
    total_items: int
    lane: str
    recall_at_1: float
    recall_at_3: float
    recall_at_5: float
    grounded_rate: float
    by_category: dict[str, dict[str, float]]
    queries: list[E1QueryReport]

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_items": self.total_items,
            "lane": self.lane,
            "recall_at_1": round(self.recall_at_1, 4),
            "recall_at_3": round(self.recall_at_3, 4),
            "recall_at_5": round(self.recall_at_5, 4),
            "grounded_rate": round(self.grounded_rate, 4),
            "by_category": {
                cat: {k: round(v, 4) for k, v in stats.items()}
                for cat, stats in self.by_category.items()
            },
            "queries": [
                {
                    "item_id": q.item_id,
                    "category": q.category,
                    "outcome": q.outcome,
                    "retrieved_ids": q.retrieved_ids,
                    "matched_terms": q.matched_terms,
                    "found_at_rank": q.found_at_rank,
                    "hit_at_1": q.hit_at_1,
                    "hit_at_3": q.hit_at_3,
                    "hit_at_5": q.hit_at_5,
                }
                for q in self.queries
            ],
        }


def load_gold_set(path: Path | None = None) -> list[E1Item]:
    target_path = path or GOLD_SET_PATH
    data = json.loads(target_path.read_text(encoding="utf-8"))
    return [
        E1Item(
            id=item["id"],
            category=item["category"],
            fact=item["fact"],
            query_terms=item["query_terms"],
            target=item["target"],
            kind=item.get("kind", "fact"),
        )
        for item in data["items"]
    ]


def run_e1_driver(
    binary: Path | str,
    store_dir: Path,
    items: list[E1Item] | None = None,
    lane: str = "term",
    k: int = 5,
) -> E1BenchmarkReport:
    """Runs the E1 evaluation end to end over the stdio MCP Driver."""
    dataset = items if items is not None else load_gold_set()
    capsule_id_by_item: dict[str, str] = {}

    xdg_scratch = Path("/var/tmp/nmemory-bench/e1-run")
    xdg_scratch.mkdir(parents=True, exist_ok=True)
    # Opening never creates the store: make it when the directory has none.
    prepare_database(binary, store_dir, EvaluationMode.REUSE)
    driver = Driver.spawn(
        binary=binary,
        run_dir=xdg_scratch,
        store_dir=store_dir,
        project=E1_PROJECT,
        semantic=False,
    )
    driver.handshake()

    try:
        # 1. Ingest of all 41 items with explicit provenance
        ingest_items = [
            {
                "content": item.fact,
                "source": f"e1:gold_set:{item.id}",
                # The anchor is the gold set's own, verbatim — `:1` was a line
                # invented here (see `comparison/adapters.py`).
                "anchor": item.target,
                "kind": item.kind,
                "project_id": E1_PROJECT,
            }
            for item in dataset
        ]

        resp = driver.request(
            1,
            "tools/call",
            {
                "name": "nmemory_capsule_ingest",
                "arguments": {"items": ingest_items},
            },
        )
        payload = tool_payload(resp)
        outcomes = payload.get("outcomes", [])
        if len(outcomes) != len(dataset):
            raise DriverError(
                f"ingest returned {len(outcomes)} outcomes, expected {len(dataset)}"
            )

        for item, outcome in zip(dataset, outcomes, strict=True):
            if outcome.get("status") not in (
                "captured",
                "deduplicated",
            ) or not outcome.get("id"):
                raise DriverError(f"ingest failed for {item.id}: {outcome}")
            capsule_id_by_item[item.id] = str(outcome["id"])

        # 2. Run the retrieval queries
        query_reports: list[E1QueryReport] = []

        for req_id, item in enumerate(dataset, start=2):
            expected_cid = capsule_id_by_item[item.id]
            q_resp = driver.request(
                req_id,
                "tools/call",
                {
                    "name": "nmemory_capsule_retrieve",
                    "arguments": {
                        "terms": item.query_terms,
                        "project_id": E1_PROJECT,
                        "lane": lane,
                        "limit": k,
                        "token_budget": 100_000,
                    },
                },
            )
            q_payload = tool_payload(q_resp)
            outcome_str = q_payload.get("outcome", "abstain")
            results = q_payload.get("results", [])
            retrieved_ids = [
                str(r.get("id")) for r in results if isinstance(r, dict) and "id" in r
            ]

            matched_terms: list[str] = []
            rank: int | None = None
            for idx, r in enumerate(results):
                if str(r.get("id")) == expected_cid:
                    rank = idx + 1
                    matched_terms = r.get("matched_terms", [])
                    break

            hit_1 = rank is not None and rank <= 1
            hit_3 = rank is not None and rank <= 3
            hit_5 = rank is not None and rank <= 5

            query_reports.append(
                E1QueryReport(
                    item_id=item.id,
                    category=item.category,
                    outcome=outcome_str,
                    retrieved_ids=retrieved_ids,
                    matched_terms=matched_terms,
                    expected_capsule_id=expected_cid,
                    found_at_rank=rank,
                    hit_at_1=hit_1,
                    hit_at_3=hit_3,
                    hit_at_5=hit_5,
                )
            )

        # 3. Statistical aggregation
        total = len(query_reports)
        r1 = sum(1 for q in query_reports if q.hit_at_1) / total if total else 0.0
        r3 = sum(1 for q in query_reports if q.hit_at_3) / total if total else 0.0
        r5 = sum(1 for q in query_reports if q.hit_at_5) / total if total else 0.0
        grounded = (
            sum(1 for q in query_reports if q.outcome == "grounded") / total
            if total
            else 0.0
        )

        by_cat: dict[str, dict[str, float]] = {}
        categories = sorted({q.category for q in query_reports})
        for cat in categories:
            cat_q = [q for q in query_reports if q.category == cat]
            cat_tot = len(cat_q)
            by_cat[cat] = {
                "total": float(cat_tot),
                "recall_at_1": sum(1 for q in cat_q if q.hit_at_1) / cat_tot
                if cat_tot
                else 0.0,
                "recall_at_3": sum(1 for q in cat_q if q.hit_at_3) / cat_tot
                if cat_tot
                else 0.0,
                "recall_at_5": sum(1 for q in cat_q if q.hit_at_5) / cat_tot
                if cat_tot
                else 0.0,
                "grounded_rate": sum(1 for q in cat_q if q.outcome == "grounded")
                / cat_tot
                if cat_tot
                else 0.0,
            }

        return E1BenchmarkReport(
            total_items=total,
            lane=lane,
            recall_at_1=r1,
            recall_at_3=r3,
            recall_at_5=r5,
            grounded_rate=grounded,
            by_category=by_cat,
            queries=query_reports,
        )
    finally:
        driver.finish()
