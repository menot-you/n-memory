"""Capsule v1 — the frozen atomic memory record (prd.nMEMORY.2 §4).

FROZEN canonical JSON format and strict validation contracts.
"""

from __future__ import annotations

import hashlib
import json
import math
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


def sha256_hex(data: bytes | str) -> str:
    """Compute SHA-256 hex digest over bytes or UTF-8 string."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def format_rfc3339(dt: datetime) -> str:
    """Format datetime as strict RFC3339 string."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    else:
        dt = dt.astimezone(UTC)
    # Output YYYY-MM-DDTHH:MM:SSZ or with subsecond precision
    iso = dt.isoformat()
    if iso.endswith("+00:00"):
        iso = iso[:-6] + "Z"
    return iso


def parse_rfc3339(text: str) -> datetime:
    """Parse RFC3339 timestamp string into timezone-aware datetime."""
    t = text.strip()
    if t.endswith(("Z", "z")):
        t = t[:-1] + "+00:00"
    dt = datetime.fromisoformat(t)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


HEADLINE_MAX_CHARS = 140
_NEWLINE_CHARS = {"\n", "\r", "\u2028", "\u2029"}


def headline_of(content: str) -> str:
    """Read at most 300 characters and return a safe, bounded first line."""
    sample = content[:300]
    chars: list[str] = []
    more = len(content) > len(sample)
    for i, ch in enumerate(sample):
        if ch in _NEWLINE_CHARS:
            end = i + (2 if ch == "\r" and sample[i : i + 2] == "\r\n" else 1)
            more = end < len(content)
            break
        if unicodedata.category(ch) in {"Cc", "Cf"}:
            if ch.isspace():
                chars.append(" ")
            continue
        chars.append(ch)
    headline = "".join(chars[:HEADLINE_MAX_CHARS])
    return headline + "…" if len(chars) > HEADLINE_MAX_CHARS or more else headline


class AuthorityClass(StrEnum):
    """Closed authority ladder."""

    OBSERVED_FACT = "observed-fact"
    USER_STATED = "user-stated"
    AGENT_INFERRED = "agent-inferred"
    EXTERNALLY_IMPORTED = "externally-imported"

    @classmethod
    def from_str(cls, value: str) -> AuthorityClass:
        try:
            return cls(value.strip().lower().replace("_", "-"))
        except ValueError:
            raise ValueError(f"unknown authority class: {value}") from None


from . import NMemoryError


class CapsuleError(NMemoryError):
    """Validation errors rejected at capsule construction."""


class EmptyContent(CapsuleError):
    def __init__(self) -> None:
        super().__init__("capsule rejected: content is empty")


class InvertedFreshnessWindow(CapsuleError):
    def __init__(self, valid_from: datetime, valid_to: datetime) -> None:
        super().__init__(
            f"capsule rejected: valid_to {format_rfc3339(valid_to)} precedes valid_from {format_rfc3339(valid_from)}"
        )
        self.valid_from = valid_from
        self.valid_to = valid_to


class InvalidConfidence(CapsuleError):
    def __init__(self, val: float) -> None:
        super().__init__(f"capsule rejected: confidence {val} outside 0.0..=1.0")
        self.val = val


@dataclass(frozen=True)
class Provenance:
    source: str
    anchor: str
    source_hash: str

    def __post_init__(self) -> None:
        if not self.source or not self.source.strip():
            raise CapsuleError(
                "capsule rejected: provenance.source is empty (no capsule without provenance)"
            )
        if not self.anchor or not self.anchor.strip():
            raise CapsuleError(
                "capsule rejected: provenance.anchor is empty (no capsule without provenance)"
            )
        if not self.source_hash or not self.source_hash.strip():
            raise CapsuleError(
                "capsule rejected: provenance.source_hash is empty (no capsule without provenance)"
            )

    def to_dict(self) -> dict[str, str]:
        return {
            "source": self.source,
            "anchor": self.anchor,
            "source_hash": self.source_hash,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Provenance:
        return cls(
            source=data.get("source", ""),
            anchor=data.get("anchor", ""),
            source_hash=data.get("source_hash", ""),
        )


@dataclass(frozen=True)
class Freshness:
    valid_from: datetime
    valid_to: datetime | None = None

    def __post_init__(self) -> None:
        if self.valid_to is not None and self.valid_to < self.valid_from:
            raise InvertedFreshnessWindow(self.valid_from, self.valid_to)

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid_from": format_rfc3339(self.valid_from),
            "valid_to": format_rfc3339(self.valid_to)
            if self.valid_to is not None
            else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Freshness:
        vf = parse_rfc3339(data["valid_from"])
        vt_raw = data.get("valid_to")
        vt = parse_rfc3339(vt_raw) if vt_raw is not None else None
        return cls(valid_from=vf, valid_to=vt)


@dataclass(frozen=True)
class Scope:
    project_id: str

    def __post_init__(self) -> None:
        if not self.project_id or not self.project_id.strip():
            raise CapsuleError("capsule rejected: scope.project_id is empty")

    def to_dict(self) -> dict[str, str]:
        return {"project_id": self.project_id}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Scope:
        return cls(project_id=data.get("project_id", ""))


class Capsule:
    """The atomic memory object (Capsule v1, frozen)."""

    def __init__(
        self,
        content: str,
        provenance: Provenance,
        confidence: float,
        freshness: Freshness,
        scope: Scope,
        authority_class: AuthorityClass | str,
        instruction_taint: bool,
    ):
        if not content or not content.strip():
            raise EmptyContent()

        if isinstance(confidence, (int, float)) and not isinstance(confidence, bool):
            conf = float(confidence)
        else:
            raise CapsuleError(
                f"capsule rejected: confidence {confidence} is not a float"
            )

        if math.isnan(conf) or not (0.0 <= conf <= 1.0):
            raise InvalidConfidence(conf)

        if isinstance(authority_class, AuthorityClass):
            auth = authority_class
        elif isinstance(authority_class, str):
            auth = AuthorityClass.from_str(authority_class)
        else:
            raise CapsuleError(
                "capsule rejected: authority_class is not an authority name"
            )

        if not isinstance(instruction_taint, bool):
            raise CapsuleError("capsule rejected: instruction_taint is not a boolean")

        self._content = content
        self._provenance = provenance
        self._confidence = conf
        self._freshness = freshness
        self._scope = scope
        self._authority_class = auth
        self._instruction_taint = instruction_taint

    @property
    def content(self) -> str:
        return self._content

    @property
    def provenance(self) -> Provenance:
        return self._provenance

    @property
    def confidence(self) -> float:
        return self._confidence

    @property
    def freshness(self) -> Freshness:
        return self._freshness

    @property
    def scope(self) -> Scope:
        return self._scope

    @property
    def authority_class(self) -> AuthorityClass:
        return self._authority_class

    @property
    def instruction_taint(self) -> bool:
        return self._instruction_taint

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict with frozen canonical order."""
        return {
            "content": self._content,
            "provenance": self._provenance.to_dict(),
            "confidence": self._confidence,
            "freshness": self._freshness.to_dict(),
            "scope": self._scope.to_dict(),
            "authority_class": self._authority_class.value,
            "instruction_taint": self._instruction_taint,
        }

    def to_canonical_json(self) -> str:
        """Byte-stable canonical JSON string."""
        return json.dumps(self.to_dict(), separators=(",", ":"), ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Capsule:
        return cls(
            content=data["content"],
            provenance=Provenance.from_dict(data["provenance"]),
            confidence=data["confidence"],
            freshness=Freshness.from_dict(data["freshness"]),
            scope=Scope.from_dict(data["scope"]),
            authority_class=data["authority_class"],
            instruction_taint=data["instruction_taint"],
        )

    @classmethod
    def from_canonical_json(cls, json_str: str) -> Capsule:
        data = json.loads(json_str)
        return cls.from_dict(data)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Capsule):
            return False
        return (
            self._content == other._content
            and self._provenance == other._provenance
            and math.isclose(
                self._confidence, other._confidence, rel_tol=1e-9, abs_tol=1e-9
            )
            and self._freshness == other._freshness
            and self._scope == other._scope
            and self._authority_class == other._authority_class
            and self._instruction_taint == other._instruction_taint
        )

    def __repr__(self) -> str:
        return (
            f"Capsule(content={self._content!r}, provenance={self._provenance!r}, "
            f"confidence={self._confidence}, freshness={self._freshness!r}, "
            f"scope={self._scope!r}, authority_class={self._authority_class.value!r}, "
            f"instruction_taint={self._instruction_taint})"
        )


RULE_KINDS = frozenset({"decision", "procedure", "constraint"})


def is_owner_rule(capsule: Capsule) -> bool:
    """An owner rule is untainted and user-stated."""
    return (
        capsule.instruction_taint is False
        and capsule.authority_class is AuthorityClass.USER_STATED
    )
