"""Base interfaces and protocols for memory system comparative benchmarking."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Set as AbstractSet
from dataclasses import asdict, dataclass, fields
from enum import StrEnum
from typing import Any

from nmemory_evals.capture_gap import EvalRefusal


class MemorySystem(StrEnum):
    NMEMORY = "nmemory"
    MEM0 = "mem0"
    LETTA = "letta"
    ZEP = "zep"
    LANGGRAPH = "langgraph"


@dataclass(frozen=True)
class Receipt:
    """Proof that a number came out of the real process of the product it names.

    D18 extended to the comparison: a third-party product's name beside a
    number that did not come out of that product's process is a MISSING ORACLE.
    The fence lives in the CONSTRUCTORS — a receipt with a blank field is never
    born, and a third-party `SystemScore` without a receipt is never born — so
    no writer (markdown or JSON) ever sees the line. The carrier is
    `EvalRefusal`, the only path to exit 2 in the CLI, never `BenchError` (1):
    what was missing was not the tool, it was the oracle. nMEMORY needs no
    receipt: its numbers come out of the very binary under test.
    """

    endpoint: str
    sdk_version: str
    response_id: str
    data: str = ""

    def __post_init__(self) -> None:
        blank = [
            f.name
            for f in fields(self)
            if not isinstance(getattr(self, f.name), str)
            or not getattr(self, f.name).strip()
        ]
        if blank:
            raise EvalRefusal(f"incomplete receipt: missing {', '.join(blank)}")


@dataclass(frozen=True)
class BenchmarkItem:
    id: str
    category: str
    fact: str
    query_terms: list[str]
    target: str
    kind: str


@dataclass(frozen=True)
class QueryObservation:
    item_id: str
    found_at_rank: int | None
    retrieved_ids: list[str]
    latency_ms: float
    outcome: str  # "grounded", "abstain", "hit", "miss"


@dataclass(frozen=True)
class SystemScore:
    system: MemorySystem
    total_items: int
    recall_at_1: float
    recall_at_3: float
    recall_at_5: float
    grounded_rate: float
    avg_query_latency_ms: float
    avg_ingest_latency_ms: float
    provenance_preserved: bool
    tamper_evident_audit: bool
    explicit_abstain_support: bool
    category_scores: dict[str, dict[str, float]]
    queries: list[QueryObservation]
    receipt: Receipt | None = None

    def __post_init__(self) -> None:
        if self.system is not MemorySystem.NMEMORY and self.receipt is None:
            raise EvalRefusal(
                f"system {self.system.value!r} without a receipt: a product "
                "number without proof of the process that measured it is a "
                "missing oracle"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "system": self.system.value,
            "total_items": self.total_items,
            "recall_at_1": round(self.recall_at_1, 4),
            "recall_at_3": round(self.recall_at_3, 4),
            "recall_at_5": round(self.recall_at_5, 4),
            "grounded_rate": round(self.grounded_rate, 4),
            "avg_query_latency_ms": round(self.avg_query_latency_ms, 2),
            "avg_ingest_latency_ms": round(self.avg_ingest_latency_ms, 2),
            "provenance_preserved": self.provenance_preserved,
            "tamper_evident_audit": self.tamper_evident_audit,
            "explicit_abstain_support": self.explicit_abstain_support,
            "category_scores": {
                c: {k: round(v, 4) for k, v in s.items()}
                for c, s in self.category_scores.items()
            },
            "receipt": asdict(self.receipt) if self.receipt is not None else None,
        }


class BaseMemoryAdapter(ABC):
    """Abstract adapter interface for benchmarked memory systems."""

    #: Only the one that talked to the real product has it; the law lives in `Receipt`.
    receipt: Receipt | None = None

    @property
    @abstractmethod
    def system(self) -> MemorySystem:
        """The identified memory system."""

    @property
    @abstractmethod
    def provenance_preserved(self) -> bool:
        """Whether the system preserves physical file:line provenance."""

    @property
    @abstractmethod
    def tamper_evident_audit(self) -> bool:
        """Whether the system maintains cryptographic audit hash chains."""

    @property
    @abstractmethod
    def explicit_abstain_support(self) -> bool:
        """Whether the system explicitly abstains on out-of-world queries."""

    @abstractmethod
    def ingest_all(self, items: list[BenchmarkItem]) -> float:
        """Ingest all benchmark items. Returns total ingest duration in ms."""

    @abstractmethod
    def retrieve(self, terms: list[str], k: int = 5) -> tuple[list[str], str, float]:
        """Query memory with terms. Returns (retrieved_ids, outcome, latency_ms)."""

    @abstractmethod
    def retrieve_texts(
        self,
        terms: list[str],
        k: int = 5,
        *,
        allowed_ids: AbstractSet[str] | None = None,
    ) -> tuple[list[str], str, float]:
        """Query memory and return the TEXTS the system served, in rank order.

        Axis B: the reader's window is what the product returns — not the seed
        texts re-mapped through my ids. No dedup: if the product spent two
        slots on the same text, that is what it served.
        """

    def close(self) -> None:
        """Release any open resources or connections."""
