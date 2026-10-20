"""Relation ontology and graph edges (w2-relations, u-r11).

The closed relation kinds and graph structures.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Final

from .capsule import format_rfc3339


class RelationKind(StrEnum):
    SUPERSEDES = "supersedes"
    DERIVED_FROM = "derived_from"
    WITNESSES = "witnesses"
    BLOCKS = "blocks"
    FALSIFIES = "falsifies"
    PROPOSES = "proposes"
    PART_OF = "part_of"
    GROUNDED_IN = "grounded_in"
    ABOUT = "about"
    CONTRADICTS = "contradicts"
    DOD_OF = "dod_of"

    @classmethod
    def from_str(cls, value: str) -> RelationKind:
        try:
            return cls(value.strip().lower().replace("-", "_"))
        except ValueError:
            raise ValueError(f"unknown relation kind: {value}") from None


class RelationOrigin(StrEnum):
    MANUAL = "manual"
    IMPORT = "import"

    @classmethod
    def from_str(cls, value: str) -> RelationOrigin:
        try:
            return cls(value.strip().lower())
        except ValueError:
            return cls.MANUAL


@dataclass(frozen=True)
class RelationRecord:
    kind: RelationKind
    from_id: str
    to_id: str
    reason: str | None
    at: datetime
    origin: RelationOrigin = RelationOrigin.MANUAL

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "from_id": self.from_id,
            "to_id": self.to_id,
            "at": format_rfc3339(self.at),
        }


# The ONLY "closed task" rule: an INCOMING edge of one of these kinds.
# A `nmemory_outcome_record` DOES NOT close, by its own contract ("ADVISORY observation
# record — NOT a witnessed close", accepts score 0.0). In-memory call sites
# call `closed_task_ids`; SQL sites (`Store.delivery_census`,
# the retired census script (git history)) write the SAME predicate over
# `nmemory.relations` parameterized by `CLOSING_KIND_VALUES` — the rule does
# not repeat, change here and all four follow. The board (`queries/board.py`)
# reads the same predicate to put a task in `done` or `dropped`.
CLOSING_KINDS: Final = (RelationKind.WITNESSES, RelationKind.SUPERSEDES)
CLOSING_KIND_VALUES: Final[tuple[str, ...]] = tuple(k.value for k in CLOSING_KINDS)


def closed_task_ids(edges: Iterable[RelationRecord]) -> set[str]:
    """Ids closed by an incoming `witnesses` or `supersedes` edge."""
    return {e.to_id for e in edges if e.kind in CLOSING_KINDS}


class DagCycleError(Exception):
    """A LIVE `blocks` cycle: projection refuses rather than inventing readiness."""

    def __init__(self, cycle: list[str], entangled: list[str]) -> None:
        super().__init__(
            f"relation dag rejected: live blocks-cycle {cycle} "
            f"({len(entangled)} id(s) cycle-entangled in total; append-only "
            "repair: supersede a member and re-project)"
        )
        self.cycle = cycle
        self.entangled = entangled


def _find_cycle(leftover: set[str], blockers: dict[str, set[str]]) -> list[str]:
    """ONE concrete cycle, forward `blocks` direction, lowest id first."""
    if not leftover:
        return []
    start = min(leftover)
    path = [start]
    pos = {start: 0}
    cur = start
    for _ in range(len(leftover) + 1):
        prev = next((b for b in sorted(blockers.get(cur, ())) if b in leftover), None)
        if prev is None:
            break  # unreachable: a leftover node holds a leftover blocker
        if prev in pos:
            j = pos[prev]
            cycle = [path[j], *reversed(path[j + 1 :])]
            pivot = cycle.index(min(cycle))
            return cycle[pivot:] + cycle[:pivot]
        pos[prev] = len(path)
        path.append(prev)
        cur = prev
    return sorted(leftover)


class Dag:
    """Pure projection over `blocks` edges — the oracle's `Dag` (u6d).

    The universe is the set of endpoints of `blocks` edges: whatever never appeared
    in one is not a member, and it is up to the caller to add open tasks
    that no edge named. `supersedes` kills without entering the universe,
    `witnesses` CLOSES WITH PROOF (the id leaves ready/blocked, stops blocking its
    dependents, and remains retrievable), and a tombstoned id is dead by injection
    from the caller — forgetting is a sanctioned dag repair.
    """

    def __init__(
        self,
        universe: set[str],
        dead: set[str],
        done: set[str],
        blockers: dict[str, set[str]],
    ) -> None:
        self._universe = universe
        self._dead = dead
        self._done = done
        self._blockers = blockers

    @classmethod
    def project_excluding(
        cls, edges: list[RelationRecord], extra_dead: set[str]
    ) -> Dag:
        universe: set[str] = set()
        dead = set(extra_dead)
        for e in edges:
            if e.kind is RelationKind.BLOCKS:
                universe.add(e.from_id)
                universe.add(e.to_id)
            elif e.kind is RelationKind.SUPERSEDES:
                dead.add(e.to_id)

        # DEAD BEATS DONE: a superseded or tombstoned capsule is never
        # announced as done — it is fenced out of retrieval, whereas done is not.
        # `done` derives from the single closing rule: closed and not dead.
        done = {i for i in closed_task_ids(edges) if i in universe and i not in dead}

        # A dead OR done blocker blocks nobody: done closed with
        # proof, so it releases its dependents just like dead.
        blockers: dict[str, set[str]] = {}
        for e in edges:
            if (
                e.kind is RelationKind.BLOCKS
                and e.from_id not in dead
                and e.from_id not in done
            ):
                blockers.setdefault(e.to_id, set()).add(e.from_id)

        active = {i for i in universe if i not in dead and i not in done}
        indegree = {i: len(blockers.get(i, ())) for i in active}
        outgoing: dict[str, list[str]] = {}
        for target, sources in blockers.items():
            if target not in active:
                continue
            for source in sources:
                outgoing.setdefault(source, []).append(target)

        # Kahn algorithm over the ACTIVE subgraph. A done member is inactive here just like
        # a dead one, so witnessing a cycle member dissolves it.
        queue = [i for i, d in indegree.items() if d == 0]
        while queue:
            node = queue.pop()
            for target in outgoing.get(node, ()):
                indegree[target] -= 1
                if indegree[target] == 0:
                    queue.append(target)

        leftover = {i for i, d in indegree.items() if d > 0}
        if leftover:
            raise DagCycleError(_find_cycle(leftover, blockers), sorted(leftover))

        return cls(universe, dead, done, blockers)

    def blocked_by(self, capsule_id: str) -> list[str]:
        return sorted(self._blockers.get(capsule_id, ()))

    def ready(self) -> list[str]:
        return sorted(
            i
            for i in self._universe
            if i not in self._dead and i not in self._done and not self._blockers.get(i)
        )

    def done(self) -> list[str]:
        return sorted(self._done)

    def is_done(self, capsule_id: str) -> bool:
        return capsule_id in self._done

    def is_live(self, capsule_id: str) -> bool:
        return capsule_id in self._universe and capsule_id not in self._dead


# U1 (nmemory-standing): an edge is LIVE if the latest event for its triple in
# `relation_revocations` is not a revocation. The table is NATIVE (database.py),
# thus zero DDL: `origin` carries the event and actor (`revoke:<actor>` /
# `reassert:<origin>`), `asserted_at` copies the text of `relations.at` VERBATIM,
# and the reason goes to `append_audit`. `relations.at` is never touched — bumping
# it would rewrite the history that `is_falsified(as_of)` reads. One rule, all
# readers.
def live_relation_sql(alias: str = "r", *, as_of: bool = False) -> str:
    """`NOT EXISTS (...)` snippet excluding revoked edges; `as_of` appends two
    positional parameters (the same timestamp, twice) and evaluates as of that date."""
    cut = " AND rv.revoked_at <= ?" if as_of else ""
    cut2 = " AND rv2.revoked_at <= ?" if as_of else ""
    return f"""NOT EXISTS (
        SELECT 1 FROM relation_revocations rv
        WHERE rv.kind = {alias}.kind AND rv.from_id = {alias}.from_id AND rv.to_id = {alias}.to_id
          AND (rv.origin = 'revoke' OR rv.origin GLOB 'revoke:*'){cut}
          AND rv.seq = (SELECT max(rv2.seq) FROM relation_revocations rv2
                        WHERE rv2.kind = {alias}.kind AND rv2.from_id = {alias}.from_id
                          AND rv2.to_id = {alias}.to_id{cut2}))"""
