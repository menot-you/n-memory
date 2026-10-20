"""Definition of Done (DoD) two-axis lifecycle state machine (PLAN §7 R12).

Status axis (authority): proposed -> accepted -> waived.
Verdict axis (observation): unmet -> met | partial | failed.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from .capsule import format_rfc3339


@dataclass(frozen=True)
class DodEvent:
    axis: str
    value: str
    actor: str
    reason: str
    at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "axis": self.axis,
            "value": self.value,
            "actor": self.actor,
            "reason": self.reason,
            "at": format_rfc3339(self.at),
        }


@dataclass(frozen=True)
class DodState:
    status: str
    verdict: str
    history: list[DodEvent]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "verdict": self.verdict,
            "history": [e.to_dict() for e in self.history],
        }


@dataclass(frozen=True)
class DodContradiction:
    dod_id: str
    target_id: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
