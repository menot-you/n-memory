"""Substrate records: outcomes and recall receipts (u6h, u6i).

Observation tracking.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .capsule import format_rfc3339


@dataclass(frozen=True)
class OutcomeRecord:
    id: str
    description: str
    actor: str
    evidence_ref: str | None
    capsule_id: str | None
    receipt_id: str | None
    score: float | None
    at: datetime

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "description": self.description,
            "actor": self.actor,
            "at": format_rfc3339(self.at),
        }
        if self.evidence_ref is not None:
            d["evidence_ref"] = self.evidence_ref
        if self.capsule_id is not None:
            d["capsule_id"] = self.capsule_id
        if self.receipt_id is not None:
            d["receipt_id"] = self.receipt_id
        if self.score is not None:
            d["score"] = self.score
        return d


@dataclass(frozen=True)
class RecallReceipt:
    id: str
    query: str
    returned_ids: list[str]
    project_id: str | None
    at: datetime
