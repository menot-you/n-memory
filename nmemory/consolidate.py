"""Deterministic consolidation rules (w2-consolidate, u-r11, q6).

Pure offline rules: the lifecycle tiers, token-containment merge clustering
(the curator's near-duplicate proposals), and the protective tier moves that
the store applies.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from . import taint
from .capsule import AuthorityClass

# Measured 2026-09-12: 96.3% of last recalls occurred within 30 days.
# Only superseded capsules qualify: never archive unsuperseded records by
# disuse, especially failure_pattern/constraint (unused does not mean useless).
ARCHIVE_AFTER_DAYS = 30
DEDUP_HINT_MIN_SCORE = 0.5
DEDUP_HINT_MIN_TOKENS = 4


class Tier(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"
    QUARANTINED = "quarantined"

    @classmethod
    def from_str(cls, value: str) -> Tier:
        try:
            return cls(value.strip().lower())
        except ValueError:
            raise ValueError(f"unknown tier: {value}") from None


#: The journal records every move as `tier=<tier> <reason>`, and every archive
#: this planner makes is for supersession: a restore reads this opening back.
SUPERSESSION_ARCHIVE = f"tier={Tier.ARCHIVED.value} superseded and "
SUPERSESSION_REVOKED = "supersession revoked (no live supersedes edge remains)"


@dataclass(frozen=True)
class ConsolidationRecord:
    id: str
    seq: int
    source_hash: str
    content: str
    instruction_taint: bool
    authority_class: AuthorityClass
    valid_to: datetime | None
    created_at: datetime
    recall_count: int
    is_superseded: bool
    tier: Tier
    pinned: bool
    #: The journal says its archive is one this planner made for supersession.
    #: Only the callers that apply moves read the journal; elsewhere it is False.
    supersession_archive: bool = False


@dataclass(frozen=True)
class MergeProposal:
    ids: list[str]
    containment_score: float
    rationale: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "ids": self.ids,
            "containment_score": self.containment_score,
            "rationale": self.rationale,
        }


@dataclass(frozen=True)
class TierMove:
    id: str
    to: Tier
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "to": self.to.value,
            "reason": self.reason,
        }


def significant_tokens(text: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for token in re.findall(r"[^\W_]+", text.lower()):
        if len(token) >= 3 and token not in seen:
            seen.add(token)
            out.append(token)
    return out


def full_tokens(text: str) -> set[str]:
    return set(re.findall(r"[^\W_]+", text.lower()))


def containment(a: set[str], b: set[str]) -> float:
    larger = max(len(a), len(b))
    if larger == 0:
        return 0.0
    return len(a & b) / larger


def reported_score(raw: float) -> float:
    rounded = round(raw * 100.0) / 100.0
    if rounded >= 1.0:
        return 0.99
    return rounded


def dsu_find(parent: list[int], x: int) -> int:
    while parent[x] != x:
        parent[x] = parent[parent[x]]
        x = parent[x]
    return x


def canonical_key(r: ConsolidationRecord) -> tuple[Any, ...]:
    return (
        r.seq,
        r.id,
        r.source_hash,
        r.content,
        r.instruction_taint,
        r.authority_class.value,
        r.valid_to or datetime.min.replace(tzinfo=UTC),
        r.created_at,
        r.recall_count,
        r.is_superseded,
        r.tier.value,
    )


def canonical_order(
    records: list[ConsolidationRecord],
) -> list[ConsolidationRecord]:
    ordered = sorted(records, key=canonical_key)
    seen_ids: set[str] = set()
    out: list[ConsolidationRecord] = []
    for r in ordered:
        if r.id not in seen_ids:
            seen_ids.add(r.id)
            out.append(r)
    return out


def near_duplicate_proposals(
    ordered: list[ConsolidationRecord],
) -> list[MergeProposal]:
    pool = [r for r in ordered if r.tier == Tier.ACTIVE and not r.is_superseded]

    significant_lens = [len(significant_tokens(r.content)) for r in pool]
    full_sets = [full_tokens(r.content) for r in pool]

    parent = list(range(len(pool)))
    edges: list[tuple[int, int, float]] = []

    for i in range(len(pool)):
        for j in range(i + 1, len(pool)):
            if pool[i].instruction_taint != pool[j].instruction_taint:
                continue
            if min(significant_lens[i], significant_lens[j]) < DEDUP_HINT_MIN_TOKENS:
                continue
            raw = containment(full_sets[i], full_sets[j])
            if raw >= DEDUP_HINT_MIN_SCORE:
                reported = (
                    1.0 if pool[i].content == pool[j].content else reported_score(raw)
                )
                edges.append((i, j, reported))
                ri = dsu_find(parent, i)
                rj = dsu_find(parent, j)
                if ri != rj:
                    lo, hi = (ri, rj) if ri < rj else (rj, ri)
                    parent[hi] = lo

    clusters: dict[int, list[int]] = defaultdict(list)
    for idx in range(len(pool)):
        root = dsu_find(parent, idx)
        clusters[root].append(idx)

    max_score: dict[int, float] = defaultdict(float)
    for i, _j, score in edges:
        root = dsu_find(parent, i)
        max_score[root] = max(max_score[root], score)

    proposals: list[MergeProposal] = []
    for root in sorted(clusters.keys()):
        members = clusters[root]
        if len(members) < 2:
            continue
        score = max_score.get(root, 0.0)
        proposals.append(
            MergeProposal(
                ids=[pool[idx].id for idx in members],
                containment_score=score,
                rationale=(
                    f"near-duplicate cluster of {len(members)} live active capsules "
                    f"(full-vocabulary token containment >= {DEDUP_HINT_MIN_SCORE} "
                    f"with significant-token eligibility, ingest dedup-hint "
                    f"metric family); proposal only \u2014 the caller "
                    f"decides merge/supersede, never automatic"
                ),
            )
        )
    return proposals


def archive_deadline(record: ConsolidationRecord) -> datetime | None:
    """First time after which a superseded capsule may be archived."""
    if record.pinned or record.tier != Tier.ACTIVE or not record.is_superseded:
        return None
    deadlines = []
    if record.valid_to is not None:
        deadlines.append(record.valid_to)
    if record.recall_count == 0:
        deadlines.append(record.created_at + timedelta(days=ARCHIVE_AFTER_DAYS))
    return min(deadlines, default=None)


def protective_tier_moves(
    ordered: list[ConsolidationRecord],
    now: datetime,
) -> list[TierMove]:
    moves: list[TierMove] = []
    for r in ordered:
        if (
            r.instruction_taint
            and r.authority_class == AuthorityClass.EXTERNALLY_IMPORTED
        ):
            findings = taint.scan(r.content)
            if findings:
                if r.tier != Tier.QUARANTINED:
                    rules = sorted({f.rule.id for f in findings})
                    moves.append(
                        TierMove(
                            id=r.id,
                            to=Tier.QUARANTINED,
                            reason=(
                                f"instruction-tainted externally-imported content "
                                f"with scanner findings ({', '.join(rules)})"
                            ),
                        )
                    )
                continue

        # A supersession archive ends with the supersession: nothing else holds it.
        if r.tier == Tier.ARCHIVED and r.supersession_archive and not r.is_superseded:
            moves.append(TierMove(id=r.id, to=Tier.ACTIVE, reason=SUPERSESSION_REVOKED))
            continue

        deadline = archive_deadline(r)
        if deadline is None or deadline >= now:
            continue

        if r.valid_to is not None and r.valid_to < now:
            moves.append(
                TierMove(
                    id=r.id,
                    to=Tier.ARCHIVED,
                    reason="superseded and expired (valid_to before now)",
                )
            )
        else:
            moves.append(
                TierMove(
                    id=r.id,
                    to=Tier.ARCHIVED,
                    reason=f"superseded and stale (age > {ARCHIVE_AFTER_DAYS} days with zero recalls)",
                )
            )
    return moves


def archive_candidates(records: list[ConsolidationRecord], now: datetime) -> int:
    return sum(
        1
        for m in protective_tier_moves(canonical_order(records), now)
        if m.to == Tier.ARCHIVED
    )
