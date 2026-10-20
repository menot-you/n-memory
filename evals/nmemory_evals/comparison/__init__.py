"""Comparative memory benchmarks package."""

from nmemory_evals.comparison.adapters import NMemoryAdapter
from nmemory_evals.comparison.base import (
    BaseMemoryAdapter,
    BenchmarkItem,
    MemorySystem,
    QueryObservation,
    SystemScore,
)
from nmemory_evals.comparison.http_vendors import (
    LettaHttpAdapter,
    Mem0HttpAdapter,
    ZepHttpAdapter,
    build_http_adapter,
    build_seeding_adapter,
)
from nmemory_evals.comparison.locomo import (
    LOCOMO_CATEGORIES,
    conversation_session_ids,
    locomo_items,
    locomo_questions,
    run_locomo,
    verify_dataset_sha256,
)
from nmemory_evals.comparison.report import render_markdown_matrix
from nmemory_evals.comparison.runner import (
    ComparisonReport,
    evaluate_system,
    run_comparison_benchmark,
)

__all__ = [
    "LOCOMO_CATEGORIES",
    "BaseMemoryAdapter",
    "BenchmarkItem",
    "ComparisonReport",
    "LettaHttpAdapter",
    "Mem0HttpAdapter",
    "MemorySystem",
    "NMemoryAdapter",
    "QueryObservation",
    "SystemScore",
    "ZepHttpAdapter",
    "build_http_adapter",
    "build_seeding_adapter",
    "conversation_session_ids",
    "evaluate_system",
    "locomo_items",
    "locomo_questions",
    "render_markdown_matrix",
    "run_comparison_benchmark",
    "run_locomo",
    "verify_dataset_sha256",
]
