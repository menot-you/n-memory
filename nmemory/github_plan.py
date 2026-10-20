"""GitHub sync planner: what the planning graph wants on GitHub, and the plan to get there.

Pure: the wire types a GitHub issue reads as, the markers, the desired set built from
a store snapshot, the findings and the diff into a `Plan`. `github_client` talks to
GitHub, and `github` reads the store, drives the client and applies the plan.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from .capsule import headline_of
from .capsule_ref import MAX_SLUG_LEN
from .relation import RelationKind, RelationRecord

BODY_FOOTER: str = "Synced from nMEMORY. Edits here are overwritten."
SUB_ISSUE_CEILING: int = 100
TITLE_FALLBACK_MAX_CHARS: int = MAX_SLUG_LEN  # 48


# -------------------------------------------------------------- wire types ---


@dataclass(frozen=True)
class GhIssueRef:
    number: int
    id: int


@dataclass
class GhIssue:
    number: int
    id: int
    title: str
    body: str
    state: str
    state_reason: str | None
    issue_type: str | None
    labels: list[str]
    is_pull_request: bool
    sub_issues_total: int | None = None
    blocked_by_total: int | None = None
    updated_at: str | None = None

    def may_have_sub_issues(self) -> bool:
        return self.sub_issues_total != 0

    def may_be_blocked(self) -> bool:
        return self.blocked_by_total != 0


@dataclass(frozen=True)
class GhEvent:
    """One issue timeline event: what changed, who changed it, and when."""

    id: str
    event: str
    actor: str | None
    at: str
    label: str | None = None
    rename_from: str | None = None
    rename_to: str | None = None


@dataclass(frozen=True)
class GhComment:
    """One issue comment: its issue, its author, when, and its text."""

    id: str
    issue: int
    actor: str | None
    at: str
    body: str


class CloseReason(Enum):
    COMPLETED = "completed"
    NOT_PLANNED = "not_planned"

    @property
    def as_str(self) -> str:
        return self.value


@dataclass(frozen=True)
class DesiredState:
    is_open: bool
    close_reason: CloseReason | None = None

    @classmethod
    def open_state(cls) -> DesiredState:
        return cls(is_open=True, close_reason=None)

    @classmethod
    def closed_state(cls, reason: CloseReason) -> DesiredState:
        return cls(is_open=False, close_reason=reason)

    def wire_state(self) -> str:
        return "open" if self.is_open else "closed"

    def wire_reason(self) -> str | None:
        if self.is_open or self.close_reason is None:
            return None
        return self.close_reason.as_str


@dataclass
class IssuePatch:
    title: str | None = None
    body: str | None = None
    issue_type: str | None = None
    state: DesiredState | None = None


# -------------------------------------------------------------- pure core ---


def marker_of(label: str, cap_id: str) -> str:
    return f"<!-- {label} {cap_id} -->"


def parse_marker(label: str, body: str) -> str | None:
    open_tag = f"<!-- {label} "
    start = body.rfind(open_tag)
    if start == -1:
        return None
    rest = body[start + len(open_tag) :]
    end = rest.find(" -->")
    if end == -1:
        return None
    cap_id = rest[:end].strip()
    if not cap_id or any(c.isspace() for c in cap_id):
        return None
    return cap_id


def search_phrase_of(label: str, cap_id: str) -> str:
    return f"{label} {cap_id}"


class DesiredKind(Enum):
    EPIC = "epic"
    TASK = "task"
    PLAN = "plan"
    BRAINSTORM = "brainstorm"

    def as_label(self) -> str:
        return self.value

    def as_issue_type(self) -> str:
        mapping = {
            DesiredKind.EPIC: "Epic",
            DesiredKind.TASK: "Task",
            DesiredKind.PLAN: "Plan",
            DesiredKind.BRAINSTORM: "Brainstorm",
        }
        return mapping[self]


PLANNING_KINDS: list[DesiredKind] = [
    DesiredKind.EPIC,
    DesiredKind.TASK,
    DesiredKind.PLAN,
    DesiredKind.BRAINSTORM,
]


@dataclass
class IssueTypes:
    names: set[str] = field(default_factory=set)

    def missing(self) -> list[DesiredKind]:
        return [k for k in PLANNING_KINDS if k.as_issue_type() not in self.names]

    def resolve(self, kind: DesiredKind) -> str | None:
        t_name = kind.as_issue_type()
        return t_name if t_name in self.names else None


@dataclass
class DesiredInput:
    cap_id: str
    content: str
    slug: str | None
    kind: DesiredKind
    witnessed: bool
    superseded: bool
    parent: str | None
    blockers: set[str]


@dataclass
class DesiredIssue:
    cap_id: str
    title: str
    body: str
    labels: set[str]
    issue_type: str | None
    state: DesiredState
    parent: str | None
    blockers: set[str]


def title_of(slug: str | None, content: str) -> str:
    if slug and slug.strip():
        return slug.strip()
    headline = headline_of(content)
    if len(headline) <= TITLE_FALLBACK_MAX_CHARS:
        return headline
    cut = headline[:TITLE_FALLBACK_MAX_CHARS]
    return f"{cut}…"


def desired_of(input_data: DesiredInput, label: str, types: IssueTypes) -> DesiredIssue:
    if input_data.superseded:
        state = DesiredState.closed_state(CloseReason.NOT_PLANNED)
    elif input_data.witnessed:
        state = DesiredState.closed_state(CloseReason.COMPLETED)
    else:
        state = DesiredState.open_state()

    labels = {label, input_data.kind.as_label()}
    content_trimmed = input_data.content.rstrip()
    marker = marker_of(label, input_data.cap_id)
    body = f"{content_trimmed}\n\n{marker}\n{BODY_FOOTER}"

    return DesiredIssue(
        cap_id=input_data.cap_id,
        title=title_of(input_data.slug, input_data.content),
        body=body,
        labels=labels,
        issue_type=types.resolve(input_data.kind),
        state=state,
        parent=input_data.parent,
        blockers=input_data.blockers,
    )


def sync_owned_labels(label: str) -> set[str]:
    return {label} | {k.as_label() for k in PLANNING_KINDS}


# ------------------------------------------------------------- the pull ---

#: The fields a waiting proposal holds: the sync does not write them over it.
HELD_FIELDS: tuple[str, ...] = ("state", "title", "body", "labels")
#: Line 1 of a staged GitHub change. Only tokens GitHub validates enter it (the
#: repository, the issue number, the login, the time, the event id), so no GitHub
#: prose reaches a capsule's content. The host parses the same line.
PROPOSAL_LINE = re.compile(
    r"GitHub change: (?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(?P<issue>[0-9]+) "
    r"(?P<field>state|title|body|labels|label|comment) (?P<action>[a-z_-]+) "
    r"by @(?P<actor>[A-Za-z0-9-]+(?:\[bot\])?) at (?P<at>[0-9T:.+Z-]+) "
    r"\((?P<kind>event|comment|edit) (?P<ident>[0-9A-Za-z_:.+-]+)\)"
)


def proposal_line(
    repo: str,
    number: int,
    field_name: str,
    action: str,
    actor: str,
    at: str,
    kind: str,
    ident: str,
) -> str | None:
    """Line 1 of a staged GitHub change, or None when a token is not GitHub's."""
    line = (
        f"GitHub change: {repo}#{number} {field_name} {action} by @{actor} "
        f"at {at} ({kind} {ident})"
    )
    return line if PROPOSAL_LINE.fullmatch(line) else None


def parse_proposal(content: str) -> dict[str, str] | None:
    """The fields of a staged GitHub change's line 1, or None."""
    found = PROPOSAL_LINE.fullmatch(content)
    return None if found is None else found.groupdict()


def stripped_body(body: str, label: str, cap_id: str) -> tuple[str, str]:
    """The text the owner wrote in an issue body, and the action that names it.

    The sync's own suffix is removed only when it is exact; otherwise the whole
    body is the change and the action is `edited-footer`.
    """
    text = body.replace("\r\n", "\n")
    suffix = f"\n\n{marker_of(label, cap_id)}\n{BODY_FOOTER}"
    if text.endswith(suffix):
        return text[: -len(suffix)], "edited"
    return text, "edited-footer"


# ------------------------------------------------------------ store snapshot ---


@dataclass
class StoreSnapshot:
    contents: dict[str, str] = field(default_factory=dict)
    kinds: dict[str, DesiredKind] = field(default_factory=dict)
    slugs: dict[str, str] = field(default_factory=dict)
    relations: list[RelationRecord] = field(default_factory=list)


# ----------------------------------------------------------------- findings ---


class Finding:
    @dataclass(frozen=True)
    class MarkerCollision:
        cap_id: str
        numbers: list[int]

        def __str__(self) -> str:
            return (
                f"marker collision: {self.cap_id} claimed by {self.numbers} — skipped"
            )

    @dataclass(frozen=True)
    class ForeignMarker:
        number: int
        cap_id: str

        def __str__(self) -> str:
            return f"foreign marker: #{self.number} names unknown {self.cap_id}"

    @dataclass(frozen=True)
    class OutsideUniverse:
        number: int
        cap_id: str

        def __str__(self) -> str:
            return f"outside universe: #{self.number} names live {self.cap_id}"

    @dataclass(frozen=True)
    class ReopenedWhileDone:
        number: int
        cap_id: str

        def __str__(self) -> str:
            return f"reopened while done: #{self.number} vs witnessed {self.cap_id}"

    @dataclass(frozen=True)
    class MultipleParents:
        cap_id: str
        parents: list[str]

        def __str__(self) -> str:
            return f"multiple parents: {self.cap_id} → {self.parents}"

    @dataclass(frozen=True)
    class ExtraDependency:
        number: int
        cap_id: str
        blocker_number: int
        blocker_cap: str | None

        def __str__(self) -> str:
            named = f" ({self.blocker_cap})" if self.blocker_cap else ""
            return (
                f"extra dependency: #{self.number} ({self.cap_id}) is blocked by "
                f"#{self.blocker_number}{named} on GitHub, not in nMEMORY — NEVER removed here"
            )

    @dataclass(frozen=True)
    class ExtraParent:
        number: int
        cap_id: str
        parent_number: int
        parent_cap: str | None

        def __str__(self) -> str:
            named = f" ({self.parent_cap})" if self.parent_cap else ""
            return (
                f"extra parent: #{self.number} ({self.cap_id}) sits under "
                f"#{self.parent_number}{named} on GitHub, not in nMEMORY — NEVER detached here"
            )

    @dataclass(frozen=True)
    class SubIssueCeiling:
        parent_number: int
        parent_cap: str
        ceiling: int
        refused: int

        def __str__(self) -> str:
            return (
                f"sub-issue ceiling: #{self.parent_number} ({self.parent_cap}) already holds "
                f"GitHub's maximum of {self.ceiling} sub-issues — {self.refused} member(s) "
                "NOT attached, and NEVER will be until a human splits the epic"
            )

    @dataclass(frozen=True)
    class MissingIssueType:
        issue_type: str
        kind: str
        capsules: int

        def __str__(self) -> str:
            return (
                f"missing issue type: `{self.issue_type}` (kind {self.kind}) does not exist on "
                f"this repository — {self.capsules} capsule(s) of that kind cannot carry it: "
                "one mirrored from now on is created with NO type, and one already mirrored KEEPS "
                "whatever type it wears, because this sync has no verb to clear a type. None will "
                f"read `{self.issue_type}` until an organisation owner creates it under Settings → "
                "Planning → Issue types. This bot CANNOT create it (403)."
            )


@dataclass
class DesiredSet:
    issues: dict[str, DesiredIssue] = field(default_factory=dict)
    findings: list[Any] = field(default_factory=list)

    def narrow_to(self, ids: set[str]) -> None:
        """Narrows to ONE already-computed union.

        Takes ids and not an `EffortFence` on purpose: with several fences,
        the composition has to be their union, computed once. Running this
        method fence by fence INTERSECTS — two disjoint epics would give an
        empty set, zero writes, and a lying `CLEAN`.
        """
        self.issues = {cid: want for cid, want in self.issues.items() if cid in ids}
        kept_findings = []
        for f in self.findings:
            if isinstance(f, Finding.MultipleParents):
                if f.cap_id in ids:
                    kept_findings.append(f)
            else:
                kept_findings.append(f)
        self.findings = kept_findings


def desired_set(snapshot: StoreSnapshot, label: str, types: IssueTypes) -> DesiredSet:
    dead: set[str] = set()
    done: set[str] = set()
    participants: set[str] = set()

    for edge in snapshot.relations:
        if edge.kind == RelationKind.SUPERSEDES:
            dead.add(edge.to_id)
        elif edge.kind == RelationKind.WITNESSES:
            done.add(edge.to_id)
        elif edge.kind == RelationKind.BLOCKS:
            participants.add(edge.from_id)
            participants.add(edge.to_id)

    universe: set[str] = {
        cid for cid in snapshot.contents if cid in snapshot.kinds or cid in participants
    }

    # Parent candidates: child -> list of (rank, edge.at, parent_id)
    # rank 0 for grounded_in, 1 for part_of
    parents_map: dict[str, list[tuple[int, datetime, str]]] = {}
    blockers_map: dict[str, set[str]] = {}

    for edge in snapshot.relations:
        if edge.kind in (RelationKind.GROUNDED_IN, RelationKind.PART_OF):
            child, parent = edge.from_id, edge.to_id
            if child in universe and parent in universe and parent not in dead:
                rank = 1 if edge.kind == RelationKind.PART_OF else 0
                parents_map.setdefault(child, []).append((rank, edge.at, parent))
        elif edge.kind == RelationKind.BLOCKS:
            blocker, blocked = edge.from_id, edge.to_id
            if blocker in universe and blocked in universe:
                blockers_map.setdefault(blocked, set()).add(blocker)

    findings: list[Any] = []
    issues: dict[str, DesiredIssue] = {}
    used: dict[DesiredKind, int] = {}

    for cid in sorted(universe):
        content = snapshot.contents.get(cid)
        if content is None:
            continue

        candidates = parents_map.pop(cid, [])
        candidates.sort(key=lambda x: (x[0], x[1], x[2]))
        distinct_parents = sorted({p for _, _, p in candidates})
        if len(distinct_parents) > 1:
            findings.append(
                Finding.MultipleParents(cap_id=cid, parents=distinct_parents)
            )

        kind = snapshot.kinds.get(cid, DesiredKind.TASK)
        used[kind] = used.get(kind, 0) + 1

        chosen_parent = candidates[0][2] if candidates else None
        b_set = blockers_map.pop(cid, set())

        desired = desired_of(
            DesiredInput(
                cap_id=cid,
                content=content,
                slug=snapshot.slugs.get(cid),
                kind=kind,
                witnessed=(cid in done),
                superseded=(cid in dead),
                parent=chosen_parent,
                blockers=b_set,
            ),
            label,
            types,
        )
        issues[cid] = desired

    for k in types.missing():
        if k in used:
            findings.append(
                Finding.MissingIssueType(
                    issue_type=k.as_issue_type(),
                    kind=k.as_label(),
                    capsules=used[k],
                )
            )

    return DesiredSet(issues=issues, findings=findings)


# ------------------------------------------------------------------- plan ---


@dataclass(frozen=True)
class Retitle:
    number: int
    cap_id: str
    title: str


@dataclass(frozen=True)
class Rebody:
    number: int
    cap_id: str
    body: str


@dataclass(frozen=True)
class Retype:
    number: int
    cap_id: str
    issue_type: str


@dataclass(frozen=True)
class Relabel:
    number: int
    cap_id: str
    add: set[str]
    remove: set[str]


@dataclass(frozen=True)
class Attach:
    parent_number: int
    parent_cap: str
    child_id: int
    child_number: int
    child_cap: str
    previous_parent: int | None = None


@dataclass(frozen=True)
class BlockedByAdd:
    number: int
    cap_id: str
    blocker_id: int
    blocker_number: int
    blocker_cap: str


@dataclass(frozen=True)
class Close:
    number: int
    cap_id: str
    reason: CloseReason


@dataclass(frozen=True)
class Reopen:
    number: int
    cap_id: str


@dataclass(frozen=True)
class PullCandidate:
    number: int
    cap_id: str


@dataclass
class Plan:
    creates: list[DesiredIssue] = field(default_factory=list)
    retitles: list[Retitle] = field(default_factory=list)
    rebodies: list[Rebody] = field(default_factory=list)
    retypes: list[Retype] = field(default_factory=list)
    relabels: list[Relabel] = field(default_factory=list)
    attaches: list[Attach] = field(default_factory=list)
    blocked_by_adds: list[BlockedByAdd] = field(default_factory=list)
    closes: list[Close] = field(default_factory=list)
    reopens: list[Reopen] = field(default_factory=list)
    pull_candidates: list[PullCandidate] = field(default_factory=list)
    adopt_candidates: list[GhIssueRef] = field(default_factory=list)
    findings: list[Any] = field(default_factory=list)
    held: list[tuple[int, str]] = field(default_factory=list)

    def is_converged(self) -> bool:
        return (
            not self.creates
            and not self.retitles
            and not self.rebodies
            and not self.retypes
            and not self.relabels
            and not self.attaches
            and not self.blocked_by_adds
            and not self.closes
            and not self.reopens
            and not self.pull_candidates
            and not self.adopt_candidates
            and not self.findings
        )

    def render(self) -> str:
        header = (
            f"github-sync verify · creates {len(self.creates)} · retitles {len(self.retitles)} · "
            f"rebodies {len(self.rebodies)} · retypes {len(self.retypes)} · relabels {len(self.relabels)} · "
            f"attaches {len(self.attaches)} · blocked_by_adds {len(self.blocked_by_adds)} · "
            f"closes {len(self.closes)} · reopens {len(self.reopens)} · "
            f"pull_candidates {len(self.pull_candidates)} · "
            f"adopt_candidates {len(self.adopt_candidates)} · findings {len(self.findings)}"
        )
        lines = [header]
        for c in self.creates:
            lines.append(f"create {c.cap_id} — {c.title}")
        for rt in self.retitles:
            lines.append(f"retitle #{rt.number} ({rt.cap_id}) — {rt.title}")
        for rb in self.rebodies:
            lines.append(f"rebody #{rb.number} ({rb.cap_id})")
        for rp in self.retypes:
            lines.append(f"retype #{rp.number} ({rp.cap_id}) — {rp.issue_type}")
        for rl in self.relabels:
            lines.append(
                f"relabel #{rl.number} ({rl.cap_id}) — add {sorted(rl.add)} remove {sorted(rl.remove)}"
            )
        for a in self.attaches:
            lines.append(
                f"attach #{a.child_number} under #{a.parent_number} ({a.child_cap} → {a.parent_cap})"
            )
        for b in self.blocked_by_adds:
            lines.append(
                f"blocked_by #{b.number} ← #{b.blocker_number} ({b.cap_id} ← {b.blocker_cap})"
            )
        for cl in self.closes:
            lines.append(f"close #{cl.number} ({cl.cap_id}) — {cl.reason.as_str}")
        for ro in self.reopens:
            lines.append(f"reopen #{ro.number} ({ro.cap_id})")
        lines.extend(f"held #{number} {name}" for number, name in self.held)
        for p in self.pull_candidates:
            lines.append(
                f"pull_candidate #{p.number} ({p.cap_id}) — closed on GitHub, open in nMEMORY"
            )
        for ad in self.adopt_candidates:
            lines.append(f"adopt_candidate #{ad.number}")
        for f in self.findings:
            lines.append(f"finding: {f}")
        return "\n".join(lines)


def verify_exit_code(plan: Plan) -> int:
    return 0 if plan.is_converged() else 3


class UnknownMarker(Enum):
    ABSENT = "absent"
    TOMBSTONED = "tombstoned"
    LIVE_OUTSIDE_UNIVERSE = "live_outside_universe"
    FENCED_OUT = "fenced_out"


@dataclass
class ObservedRelations:
    sub_issues: dict[int, set[GhIssueRef]] = field(default_factory=dict)
    blocked_by: dict[int, set[GhIssueRef]] = field(default_factory=dict)


@dataclass
class Matched:
    by_cap: dict[str, GhIssue]
    refs: dict[str, GhIssueRef]
    collided: set[str]
    adoptable: list[GhIssue]
    collisions: list[Any]


def match_issues(observed: list[GhIssue], label: str) -> Matched:
    claims: dict[str, list[GhIssue]] = {}
    adoptable: list[GhIssue] = []

    for issue in observed:
        if issue.is_pull_request:
            continue
        cap_id = parse_marker(label, issue.body)
        if cap_id is not None:
            claims.setdefault(cap_id, []).append(issue)
        else:
            if label in issue.labels:
                adoptable.append(issue)

    by_cap: dict[str, GhIssue] = {}
    collided: set[str] = set()
    collisions: list[Any] = []

    for cap_id, issues_list in claims.items():
        if len(issues_list) > 1:
            collisions.append(
                Finding.MarkerCollision(
                    cap_id=cap_id, numbers=[i.number for i in issues_list]
                )
            )
            collided.add(cap_id)
        elif issues_list:
            by_cap[cap_id] = issues_list[0]

    refs = {
        cid: GhIssueRef(number=iss.number, id=iss.id) for cid, iss in by_cap.items()
    }
    return Matched(
        by_cap=by_cap,
        refs=refs,
        collided=collided,
        adoptable=adoptable,
        collisions=collisions,
    )


def edge_writes(
    desired: dict[str, DesiredIssue],
    refs: dict[str, GhIssueRef],
    relations: ObservedRelations,
) -> tuple[list[Attach], list[BlockedByAdd], list[Any]]:
    attaches: list[Attach] = []
    blocked_by_adds: list[BlockedByAdd] = []
    budget: dict[int, int] = {}
    refused: dict[int, tuple[str, int]] = {}

    for cap_id in sorted(desired.keys()):
        want = desired[cap_id]
        child = refs.get(cap_id)
        if child is None:
            continue

        # `refs` is the map of ALL matched issues, not the fenced set.
        # Without this `in desired`, a task inside with a parent outside
        # would do `add_sub_issue` on the outside issue, which the fence
        # promises to leave intact. Without a fence this changes nothing:
        # `desired_set` only accepts a parent edge when both sides are in
        # the universe.
        if want.parent and want.parent in desired:
            parent = refs.get(want.parent)
            if parent is not None:
                kids = relations.sub_issues.get(parent.number, set())
                if not any(k.id == child.id for k in kids):
                    held = len(kids)
                    if parent.number not in budget:
                        budget[parent.number] = max(0, SUB_ISSUE_CEILING - held)
                    left = budget[parent.number]
                    if left == 0:
                        p_cap, count = refused.get(parent.number, (want.parent, 0))
                        refused[parent.number] = (p_cap, count + 1)
                    else:
                        budget[parent.number] = left - 1
                        attaches.append(
                            Attach(
                                parent_number=parent.number,
                                parent_cap=want.parent,
                                child_id=child.id,
                                child_number=child.number,
                                child_cap=cap_id,
                                previous_parent=next(
                                    (
                                        old_ref.number
                                        for old, prior in desired.items()
                                        if old != want.parent
                                        and prior.state.close_reason
                                        is CloseReason.NOT_PLANNED
                                        and (old_ref := refs.get(old)) is not None
                                        and any(
                                            kid.id == child.id
                                            for kid in relations.sub_issues.get(
                                                old_ref.number, set()
                                            )
                                        )
                                    ),
                                    None,
                                ),
                            )
                        )

        for blocker_cap in sorted(want.blockers):
            blocker = refs.get(blocker_cap)
            if blocker is not None:
                blockers = relations.blocked_by.get(child.number, set())
                if not any(b.id == blocker.id for b in blockers):
                    blocked_by_adds.append(
                        BlockedByAdd(
                            number=child.number,
                            cap_id=cap_id,
                            blocker_id=blocker.id,
                            blocker_number=blocker.number,
                            blocker_cap=blocker_cap,
                        )
                    )

    findings = [
        Finding.SubIssueCeiling(
            parent_number=p_num,
            parent_cap=p_cap,
            ceiling=SUB_ISSUE_CEILING,
            refused=count,
        )
        for p_num, (p_cap, count) in sorted(refused.items())
    ]
    return attaches, blocked_by_adds, findings


def extra_edges(
    desired: dict[str, DesiredIssue],
    matched: Matched,
    relations: ObservedRelations,
) -> list[Any]:
    cap_of_number: dict[int, str] = {
        ref.number: cid for cid, ref in matched.refs.items()
    }
    cap_of_id: dict[int, str] = {ref.id: cid for cid, ref in matched.refs.items()}
    out: list[Any] = []

    for number, blockers in sorted(relations.blocked_by.items()):
        cap_id = cap_of_number.get(number)
        if not cap_id:
            continue
        want = desired.get(cap_id)
        if not want:
            continue
        for blocker in sorted(blockers, key=lambda x: x.number):
            blocker_cap = cap_of_id.get(blocker.id)
            if blocker_cap and blocker_cap in want.blockers:
                continue
            out.append(
                Finding.ExtraDependency(
                    number=number,
                    cap_id=cap_id,
                    blocker_number=blocker.number,
                    blocker_cap=blocker_cap,
                )
            )

    for parent_number, children in sorted(relations.sub_issues.items()):
        parent_cap = cap_of_number.get(parent_number)
        for child in sorted(children, key=lambda x: x.number):
            cap_id = cap_of_id.get(child.id)
            if not cap_id:
                continue
            want = desired.get(cap_id)
            if not want:
                continue
            if (
                want.parent
                and matched.refs.get(want.parent)
                and matched.refs[want.parent].number == parent_number
            ):
                continue
            out.append(
                Finding.ExtraParent(
                    number=child.number,
                    cap_id=cap_id,
                    parent_number=parent_number,
                    parent_cap=parent_cap,
                )
            )

    return out


def diff(
    desired: dict[str, DesiredIssue],
    matched: Matched,
    relations: ObservedRelations,
    unknown: dict[str, UnknownMarker],
    label: str,
) -> Plan:
    owned = sync_owned_labels(label)
    plan = Plan(
        adopt_candidates=[
            GhIssueRef(number=i.number, id=i.id) for i in matched.adoptable
        ],
        findings=list(matched.collisions),
    )

    for cap_id in sorted(desired.keys()):
        want = desired[cap_id]
        if cap_id in matched.collided:
            continue
        have = matched.by_cap.get(cap_id)
        if have is None:
            plan.creates.append(want)
            continue

        if have.title != want.title:
            plan.retitles.append(
                Retitle(number=have.number, cap_id=cap_id, title=want.title)
            )
        if have.body != want.body:
            plan.rebodies.append(
                Rebody(number=have.number, cap_id=cap_id, body=want.body)
            )
        if want.issue_type and have.issue_type != want.issue_type:
            plan.retypes.append(
                Retype(number=have.number, cap_id=cap_id, issue_type=want.issue_type)
            )

        present = set(have.labels)
        add = want.labels - present
        remove = (present & owned) - want.labels
        if add or remove:
            plan.relabels.append(
                Relabel(number=have.number, cap_id=cap_id, add=add, remove=remove)
            )

        # A task's GitHub change waits as a proposal (`hold`); once none waits, the
        # board's state is written back. Other kinds keep the report.
        is_task = DesiredKind.TASK.as_label() in want.labels
        if not want.state.is_open and have.state == "open":
            if have.state_reason == "reopened" and not is_task:
                plan.findings.append(
                    Finding.ReopenedWhileDone(number=have.number, cap_id=cap_id)
                )
            else:
                reason = want.state.close_reason or CloseReason.COMPLETED
                plan.closes.append(
                    Close(number=have.number, cap_id=cap_id, reason=reason)
                )
        elif want.state.is_open and have.state == "closed":
            if is_task:
                plan.reopens.append(Reopen(number=have.number, cap_id=cap_id))
            else:
                plan.pull_candidates.append(
                    PullCandidate(number=have.number, cap_id=cap_id)
                )

    attaches, blocked_by_adds, over_ceiling = edge_writes(
        desired, matched.refs, relations
    )
    plan.attaches = attaches
    plan.blocked_by_adds = blocked_by_adds
    plan.findings.extend(over_ceiling)
    plan.findings.extend(extra_edges(desired, matched, relations))

    for cap_id in sorted(matched.by_cap.keys()):
        if cap_id in desired:
            continue
        issue = matched.by_cap[cap_id]
        status = unknown.get(cap_id)
        if status == UnknownMarker.TOMBSTONED:
            if issue.state == "open" and issue.state_reason != "reopened":
                plan.closes.append(
                    Close(
                        number=issue.number,
                        cap_id=cap_id,
                        reason=CloseReason.NOT_PLANNED,
                    )
                )
        elif status == UnknownMarker.LIVE_OUTSIDE_UNIVERSE:
            plan.findings.append(
                Finding.OutsideUniverse(number=issue.number, cap_id=cap_id)
            )
        elif status == UnknownMarker.FENCED_OUT:
            pass
        else:
            plan.findings.append(
                Finding.ForeignMarker(number=issue.number, cap_id=cap_id)
            )

    return plan


def hold(plan: Plan, held: set[tuple[int, str]]) -> None:
    """Drop every write to a field of an issue a waiting proposal holds."""
    plan.retitles = [w for w in plan.retitles if (w.number, "title") not in held]
    plan.rebodies = [w for w in plan.rebodies if (w.number, "body") not in held]
    plan.relabels = [w for w in plan.relabels if (w.number, "labels") not in held]
    plan.closes = [w for w in plan.closes if (w.number, "state") not in held]
    plan.reopens = [w for w in plan.reopens if (w.number, "state") not in held]
    plan.held = sorted(held)
