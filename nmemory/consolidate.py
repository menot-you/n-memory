"""Deterministic consolidation planner (w2-consolidate, u-r11, q6).

Pure offline consolidation planning: exact-dupe detection, token-containment
merge clustering, and rule-based tier promotions/demotions.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from . import taint
from .capsule import AuthorityClass
from .capsule_ref import fold_diacritic

# Measured 2026-09-12: 96.3% of last recalls occurred within 30 days.
# Only superseded capsules qualify: never archive unsuperseded records by
# disuse, especially failure_pattern/constraint (unused does not mean useless).
ARCHIVE_AFTER_DAYS = 30
DEDUP_HINT_MIN_SCORE = 0.5
DEDUP_HINT_MIN_TOKENS = 4
ALIAS_PREFIX_MIN = 4
ALIAS_PROPOSALS_CAP = 20


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
class ExactDupe:
    keep: str
    drop: str
    source_hash: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "keep": self.keep,
            "drop": self.drop,
            "source_hash": self.source_hash,
        }


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


@dataclass(frozen=True)
class AliasProposal:
    term: str
    candidate: str | None
    miss_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "term": self.term,
            "candidate": self.candidate,
            "miss_count": self.miss_count,
        }


@dataclass
class ConsolidationPlan:
    exact_dupes: list[ExactDupe]
    merge_proposals: list[MergeProposal]
    tier_moves: list[TierMove]
    alias_proposals: list[AliasProposal] = field(default_factory=list)

    @property
    def has_store_invariant_breach(self) -> bool:
        return len(self.exact_dupes) > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "exact_dupes": [d.to_dict() for d in self.exact_dupes],
            "merge_proposals": [m.to_dict() for m in self.merge_proposals],
            "tier_moves": [t.to_dict() for t in self.tier_moves],
            "alias_proposals": [a.to_dict() for a in self.alias_proposals],
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


def exact_dupes_by_source_hash(
    ordered: list[ConsolidationRecord],
) -> tuple[list[ExactDupe], set[str]]:
    by_hash: dict[str, list[ConsolidationRecord]] = defaultdict(list)
    for r in ordered:
        by_hash[r.source_hash].append(r)

    groups = [g for g in by_hash.values() if len(g) >= 2]
    groups.sort(key=lambda g: (g[0].seq, g[0].id))

    exact_dupes: list[ExactDupe] = []
    dropped: set[str] = set()
    for group in groups:
        keep = group[0]
        for anomaly in group[1:]:
            exact_dupes.append(
                ExactDupe(
                    keep=keep.id,
                    drop=anomaly.id,
                    source_hash=keep.source_hash,
                )
            )
            dropped.add(anomaly.id)
    return exact_dupes, dropped


def near_duplicate_proposals(
    ordered: list[ConsolidationRecord],
    dropped: set[str],
) -> list[MergeProposal]:
    pool = [
        r
        for r in ordered
        if r.tier == Tier.ACTIVE and not r.is_superseded and r.id not in dropped
    ]

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


def plan_consolidation(
    records: list[ConsolidationRecord],
    now: datetime,
) -> ConsolidationPlan:
    """Derive deterministic consolidation plan over live capsule records."""
    ordered = canonical_order(records)
    exact_dupes, dropped = exact_dupes_by_source_hash(ordered)
    merge_proposals = near_duplicate_proposals(ordered, dropped)
    tier_moves = protective_tier_moves(ordered, now)
    return ConsolidationPlan(
        exact_dupes=exact_dupes,
        merge_proposals=merge_proposals,
        tier_moves=tier_moves,
        alias_proposals=[],
    )


def archive_candidates(records: list[ConsolidationRecord], now: datetime) -> int:
    return sum(
        1
        for m in protective_tier_moves(canonical_order(records), now)
        if m.to == Tier.ARCHIVED
    )


def shared_prefix_len(a: str, b: str) -> int:
    count = 0
    for x, y in zip(a, b):
        if x == y:
            count += 1
        else:
            break
    return count


def folded_vocabulary(records: list[ConsolidationRecord]) -> set[str]:
    vocab: set[str] = set()
    for r in records:
        for token in full_tokens(r.content):
            vocab.add("".join(fold_diacritic(c) for c in token))
    return vocab


def alias_proposals(
    misses: list[tuple[str, int]],
    vocabulary: set[str],
    taught: set[str],
) -> list[AliasProposal]:
    out: list[AliasProposal] = []
    for term, miss_count in misses:
        if term in taught:
            continue
        had_candidate = False
        for candidate in sorted(vocabulary):
            if candidate == term:
                continue
            if shared_prefix_len(term, candidate) >= ALIAS_PREFIX_MIN:
                out.append(
                    AliasProposal(
                        term=term,
                        candidate=candidate,
                        miss_count=miss_count,
                    )
                )
                had_candidate = True
        if not had_candidate:
            out.append(
                AliasProposal(
                    term=term,
                    candidate=None,
                    miss_count=miss_count,
                )
            )
    out.sort(
        key=lambda a: (
            -a.miss_count,
            a.term,
            a.candidate if a.candidate is not None else "",
        )
    )
    return out[:ALIAS_PROPOSALS_CAP]
