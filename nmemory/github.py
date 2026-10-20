"""GitHub sync subsystem (W1, github).

GitHub Issues are a window onto the nMEMORY planning graph.
Provides verify (read-only plan reporting) and sync (push writes, change-gated audit).
An apply run first pulls: each change someone else made on GitHub to a task issue
becomes a staged, tainted proposal about that task, and the sync does
not write that field until the owner reviews it. Nothing else is read back.
The planner is `github_plan` (pure) and the `gh` client is `github_client`; this module
reads the store, drives the client and applies the plan.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .capsule import format_rfc3339, parse_rfc3339
from .capsule_ref import CapsuleRefSyntaxError, ParsedCapsuleRef
from .capture import CaptureContext
from .config import _GITHUB_KEYS
from .github_client import (
    GhClient,
    GhCommandError,
    GhError,
    GhSpawnError,
)
from .github_plan import (
    HELD_FIELDS,
    PLANNING_KINDS,
    Close,
    CloseReason,
    DesiredIssue,
    DesiredKind,
    DesiredState,
    Finding,
    GhEvent,
    GhIssue,
    GhIssueRef,
    IssuePatch,
    IssueTypes,
    ObservedRelations,
    Plan,
    StoreSnapshot,
    UnknownMarker,
    desired_set,
    diff,
    edge_writes,
    hold,
    match_issues,
    parse_marker,
    parse_proposal,
    proposal_line,
    search_phrase_of,
    stripped_body,
    sync_owned_labels,
)
from .ingest import effective_defaults
from .queries.board import GITHUB_PULL_SOURCE, waiting_changes
from .repositories.base import StoreError
from .repositories.capsules import ListFilter
from .store import Store
from .tools import TOOLS
from .tools.common import HandlerState
from .tools.validation import validate_tool_arguments

logger = logging.getLogger(__name__)

DEFAULT_MAX_WRITES: int = 30
GITHUB_SYNC_DEFAULT_CONFIG: str = ".nott/nmemory/config.toml"


# ---------------------------------------------------------------- config ---


@dataclass(frozen=True)
class SyncConfig:
    repo: str
    label: str
    project: str
    #: The mirrored epics. Tuple, not list: a mutable field breaks the
    #: generated `__hash__`, and only at runtime, when someone finally hashes
    #: it. Empty means "no fence" — the whole graph, which is what
    #: `--verify` can still measure and what the application refuses.
    efforts: tuple[str, ...] = ()


class ConfigError(Exception):
    """Typed config error."""


def parse_efforts(raw: object, where: str) -> tuple[str, ...]:
    """The list of fenced epics, validated.

    Absent is `()` — the usual behavior, the whole graph. The EMPTY list is
    refused: its two plausible readings, "fence to nothing" (zero writes) and
    "no fence" (the whole graph), differ on the whole problem, and guessing
    one of them is the difference between nine issues and two thousand.

    Whether the input resolves is not validated here: that is
    `resolve_effort`'s job, which needs a `Store` and already gives much
    better diagnostics.
    """
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ConfigError(
            f"{where} is unusable: [github].efforts must be a list of epic cap-ids"
        )
    if not raw:
        raise ConfigError(
            f"{where} is unusable: [github].efforts must not be empty — "
            "remove the key to mirror the whole graph"
        )
    out: list[str] = []
    for entry in raw:
        if not isinstance(entry, str) or not entry.strip():
            raise ConfigError(
                f"{where} is unusable: [github].efforts entries must be "
                "non-empty epic cap-ids"
            )
        out.append(entry.strip())
    return tuple(out)


def load_config(path: Path) -> SyncConfig:
    """Read and validate the [github] table of path."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise ConfigError(f"cannot read config {path}: {e}") from e

    try:
        import tomllib

        parsed = tomllib.loads(text)
    except Exception as e:
        raise ConfigError(f"config {path} is unusable: {e}") from e

    github_tbl = parsed.get("github")
    if not isinstance(github_tbl, dict):
        raise ConfigError(f"config {path} is unusable: missing [github] table")

    if unknown := sorted(set(github_tbl) - _GITHUB_KEYS):
        raise ConfigError(
            f"config {path} is unusable: unrecognized [github] key: {', '.join(unknown)}"
        )

    config = SyncConfig(
        repo=str(github_tbl.get("repo", "")),
        label=str(github_tbl.get("label", "")),
        project=str(github_tbl.get("project", "")),
        efforts=parse_efforts(github_tbl.get("efforts"), f"config {path}"),
    )

    for field_name, val in [
        ("repo", config.repo),
        ("label", config.label),
        ("project", config.project),
    ]:
        if not val.strip():
            raise ConfigError(
                f"config {path} is unusable: [github].{field_name} must not be empty"
            )

    return config


class EffortError(Exception):
    """Why an --effort id cannot scope a tick."""


# ------------------------------------------------------------ effort fence ---


@dataclass(frozen=True)
class EffortFence:
    epic_id: str
    ids: set[str]


def resolve_effort(store: Store, reference: str) -> EffortFence:
    """Resolve an --effort reference into an EffortFence."""
    try:
        parsed = ParsedCapsuleRef.parse(reference)
    except CapsuleRefSyntaxError as e:
        raise EffortError(
            f"effort '{reference}' is not a usable capsule reference"
        ) from e

    epic_id = parsed.canonical_id()
    if store.capsules.get(epic_id) is None:
        if store.capsules.get_tombstone(epic_id) is not None:
            raise EffortError(
                f"effort '{epic_id}' is tombstoned; the content is gone, only the marker remains — pass a live epic's cap-id"
            )
        raise EffortError(
            f"effort '{reference}' names no stored capsule — pass a live epic's cap-id (nmemory_capsule_list {{kind: \"epic\"}})"
        )

    if store.capsules.get_tombstone(epic_id) is not None:
        raise EffortError(
            f"effort '{epic_id}' is tombstoned; the content is gone, only the marker remains — pass a live epic's cap-id"
        )

    classification = store.classifications.get_classification(epic_id)
    if classification is None:
        raise EffortError(
            f"effort '{epic_id}' carries no classification sidecar — the effort fence resolves only a persisted epic capsule"
        )
    kind_str = (
        classification.kind.value
        if hasattr(classification.kind, "value")
        else str(classification.kind)
    )
    if kind_str != "epic":
        raise EffortError(
            f"effort '{epic_id}' is classified '{kind_str}', not 'epic' — the effort fence resolves only a persisted epic capsule; pass the containing epic's cap-id"
        )

    members = store.relations.effort_members(epic_id)
    if not members:
        raise EffortError(
            f"effort '{epic_id}' is a persisted epic but has no members — a part_of edge must point INTO it before it can scope a tick"
        )

    ids = set(members)
    ids.add(epic_id)
    return EffortFence(epic_id=epic_id, ids=ids)


# ------------------------------------------------------------ store reads ---


def read_store(store: Store, project: str) -> StoreSnapshot:
    fenced = set(store.reviews.list_review_fenced())
    live = store.capsules.list(ListFilter(project_prefix=project))
    contents: dict[str, str] = {
        sc.id: sc.capsule.content for sc in live if sc.id not in fenced
    }

    def of_kind(k_str: str) -> set[str]:
        return {
            cid
            for cid in store.classifications.list_ids_of_kind(k_str)
            if cid in contents
        }

    kinds: dict[str, DesiredKind] = {}
    for kind in PLANNING_KINDS:
        for cid in of_kind(kind.as_label()):
            if cid not in kinds:
                kinds[cid] = kind

    slugs: dict[str, str] = {}
    for cid in contents:
        slug = store.capsules.handle_slug(cid)
        if slug:
            slugs[cid] = slug

    return StoreSnapshot(
        contents=contents,
        kinds=kinds,
        slugs=slugs,
        relations=[
            r
            for r in store.relations.list_relations()
            if r.from_id not in fenced and r.to_id not in fenced
        ],
    )


# ------------------------------------------------------ verify shell ---


def list_all_issues(gh: GhClient, repo: str, label: str) -> list[GhIssue]:
    return gh.list_issues(repo, label)


def resolve_unknown_markers(
    store: Store,
    matched_by_cap: dict[str, GhIssue],
    desired_issues: dict[str, DesiredIssue],
    unfenced: set[str],
) -> dict[str, UnknownMarker]:
    out: dict[str, UnknownMarker] = {}
    for cap_id in matched_by_cap:
        if cap_id in desired_issues:
            continue
        if cap_id in unfenced:
            out[cap_id] = UnknownMarker.FENCED_OUT
            continue
        try:
            stored = store.capsules.get(cap_id)
            if stored is not None:
                out[cap_id] = UnknownMarker.LIVE_OUTSIDE_UNIVERSE
            else:
                out[cap_id] = UnknownMarker.ABSENT
        except StoreError as e:
            if "tombstoned" in str(e).lower():
                out[cap_id] = UnknownMarker.TOMBSTONED
            else:
                raise
    return out


def read_relations(
    gh: GhClient, repo: str, observed: list[GhIssue]
) -> ObservedRelations:
    relations = ObservedRelations()
    for issue in observed:
        if issue.is_pull_request:
            continue
        if issue.may_have_sub_issues():
            relations.sub_issues[issue.number] = set(
                gh.list_sub_issues(repo, issue.number)
            )
        if issue.may_be_blocked():
            relations.blocked_by[issue.number] = set(
                gh.list_blocked_by(repo, issue.number)
            )
    return relations


@dataclass
class Tick:
    plan: Plan
    desired: dict[str, DesiredIssue]
    refs: dict[str, GhIssueRef]
    relations: ObservedRelations
    types: IssueTypes
    issues: dict[str, GhIssue] = field(default_factory=dict)


def read_tick(
    store: Store,
    gh: GhClient,
    config: SyncConfig,
) -> Tick:
    snapshot = read_store(store, config.project)
    fenced: set[str] = set()
    for reference in config.efforts:
        fence = resolve_effort(store, reference)
        # `resolve_effort` validates the epic, not its PROJECT. Without
        # this, fencing to an epic this project does not contain would give
        # an empty desired set, zero work, and exit 0 — and since the fence
        # lives in a file nobody reopens, the sync would go on doing
        # nothing forever while claiming it has converged.
        if fence.ids.isdisjoint(snapshot.contents):
            raise EffortError(
                f"effort '{fence.epic_id}' has no member in project "
                f"'{config.project}' — the fence would be empty and the tick would "
                "report converged while doing nothing"
            )
        fenced |= fence.ids
    types = IssueTypes(gh.list_issue_types(config.repo))
    desired = desired_set(snapshot, config.label, types)
    # Taken ONCE, before the single narrowing: `unfenced` means "desired in
    # the whole graph", and only the union keeps that true. That is what
    # makes `FENCED_OUT` mean "desired out there, in no fence", which
    # `diff` leaves alone on purpose.
    #
    # Keys off `config.efforts`, not off `fenced` having members: today
    # they are equivalent because `resolve_effort` refuses an epic with no
    # members, but keying on the declared intent stops a future change to
    # that rule from silently defencing a run.
    unfenced: set[str] = set()
    if config.efforts:
        unfenced = set(desired.issues.keys())
        desired.narrow_to(fenced)

    observed = list_all_issues(gh, config.repo, config.label)
    matched = match_issues(observed, config.label)
    unknown = resolve_unknown_markers(store, matched.by_cap, desired.issues, unfenced)
    relations = read_relations(gh, config.repo, observed)

    plan = diff(desired.issues, matched, relations, unknown, config.label)
    findings = list(desired.findings)
    findings.extend(plan.findings)
    plan.findings = findings

    return Tick(
        plan=plan,
        desired=desired.issues,
        refs=matched.refs,
        relations=relations,
        types=types,
        issues=matched.by_cap,
    )


def held_fields(store: Store, repo: str) -> set[tuple[int, str]]:
    """The (issue, field) pairs a staged GitHub change still waits on."""
    held: set[tuple[int, str]] = set()
    with store.db.connection() as conn, conn.cursor() as cur:
        waiting = waiting_changes(cur)
    for change in waiting:
        line = parse_proposal(change.content)
        if line is not None and line["repo"] == repo and line["field"] in HELD_FIELDS:
            held.add((int(line["issue"]), line["field"]))
    return held


def verify(
    store: Store,
    gh: GhClient,
    config: SyncConfig,
) -> Plan:
    """Read-only sync tick; it stages nothing, and shows what waiting changes hold."""
    plan = read_tick(store, gh, config).plan
    hold(plan, held_fields(store, config.repo))
    return plan


# ------------------------------------------------------- apply phase ---


@dataclass
class ApplySummary:
    creates: int = 0
    updates: int = 0
    relabels: int = 0
    attaches: int = 0
    blocked_by_adds: int = 0
    closes: int = 0
    reopens: int = 0
    staged: int = 0
    writes: int = 0
    partial: bool = False
    fatal: str | None = None
    errors: list[str] = field(default_factory=list)
    findings: list[Any] = field(default_factory=list)
    pull_candidates: int = 0
    adopt_candidates: int = 0
    held: list[tuple[int, str]] = field(default_factory=list)

    def audit_reason(self) -> str:
        return (
            f"writes={self.writes} creates={self.creates} updates={self.updates} "
            f"relabels={self.relabels} attaches={self.attaches} blocked_by={self.blocked_by_adds} "
            f"closes={self.closes} reopens={self.reopens} staged={self.staged} "
            f"partial={'true' if self.partial else 'false'} "
            f"errors={len(self.errors)} findings={len(self.findings)}"
        )

    def verdict(self) -> str:
        if self.fatal is not None or self.errors:
            return "FAILED"
        if self.findings:
            return "FINDINGS"
        if self.partial:
            return "PARTIAL"
        return "CLEAN"

    def result_line(self) -> str:
        return (
            f"github-sync-result verdict={self.verdict()} writes={self.writes} "
            f"errors={len(self.errors)} findings={len(self.findings)} "
            f"partial={'true' if self.partial else 'false'} fatal={'true' if self.fatal else 'false'}"
        )

    def render(self) -> str:
        header = (
            f"github-sync apply · writes {self.writes} · creates {self.creates} · updates {self.updates} · "
            f"relabels {self.relabels} · attaches {self.attaches} · blocked_by_adds {self.blocked_by_adds} · "
            f"closes {self.closes} · reopens {self.reopens} · staged {self.staged} · "
            f"partial {'true' if self.partial else 'false'} · "
            f"errors {len(self.errors)} · findings {len(self.findings)} · "
            f"pull_candidates {self.pull_candidates} · adopt_candidates {self.adopt_candidates}"
        )
        lines = [header]
        if self.fatal is not None:
            lines.append(f"ABORTED: {self.fatal}")
        lines.extend(f"held #{number} {name}" for number, name in self.held)
        for err in self.errors:
            lines.append(f"error: {err}")
        for f in self.findings:
            lines.append(f"finding: {f}")
        lines.append(self.result_line())
        return "\n".join(lines)


def apply_exit_code(summary: ApplySummary) -> int:
    if summary.fatal is not None or summary.errors:
        return 1
    if not summary.findings:
        return 0
    return 2


def is_fatal(err: GhError) -> bool:
    if isinstance(err, GhSpawnError):
        return True
    if isinstance(err, GhCommandError):
        return any(code in err.stderr for code in ["HTTP 401", "HTTP 403", "HTTP 429"])
    return False


class StopException(Exception):
    pass


class Applier:
    def __init__(
        self,
        gh: GhClient,
        repo: str,
        label: str,
        budget: int,
        summary: ApplySummary,
        types: IssueTypes,
        refs: dict[str, GhIssueRef],
    ) -> None:
        self.gh = gh
        self.repo = repo
        self.label = label
        self.budget = budget
        self.summary = summary
        self.types = types
        self.refs = refs

    def charge(self) -> None:
        if self.summary.writes >= self.budget:
            self.summary.partial = True
            raise StopException()
        self.summary.writes += 1

    def failed(self, op: str, err: GhError) -> None:
        if is_fatal(err):
            self.summary.fatal = f"{op}: {err}"
            raise StopException()
        self.summary.errors.append(f"{op}: {err}")

    def readback_failed(self, op: str, detail: str) -> None:
        self.summary.errors.append(f"{op}: readback mismatch — {detail}")

    def create(self, creates: list[DesiredIssue], closes: list[Close]) -> None:
        for want in creates:
            if want.cap_id in self.refs:
                continue

            phrase = search_phrase_of(self.label, want.cap_id)
            try:
                hits = self.gh.search_marker(self.repo, phrase)
            except GhError as e:
                self.failed("search_marker", e)
                continue

            owned = []
            unreadable = False
            for hit in hits:
                try:
                    iss = self.gh.get_issue(self.repo, hit.number)
                    if (
                        not iss.is_pull_request
                        and parse_marker(self.label, iss.body) == want.cap_id
                    ):
                        owned.append(hit)
                except GhError as e:
                    self.failed("get_issue", e)
                    unreadable = True

            if unreadable:
                continue

            if owned:
                for hit in owned:
                    self.charge()
                    try:
                        self.gh.add_labels(self.repo, hit.number, [self.label])
                        self.summary.relabels += 1
                    except GhError as e:
                        self.failed("add_labels", e)
                continue

            self.charge()
            labels = sorted(want.labels)
            try:
                created = self.gh.create_issue(
                    self.repo,
                    want.title,
                    want.body,
                    labels,
                    want.issue_type,
                )
                if created.id != 0 and created.number != 0:
                    self.summary.creates += 1
                    self.refs[want.cap_id] = created
                    if not want.state.is_open:
                        closes.append(
                            Close(
                                number=created.number,
                                cap_id=want.cap_id,
                                reason=want.state.close_reason or CloseReason.COMPLETED,
                            )
                        )
                else:
                    self.readback_failed(
                        "create_issue",
                        f"{want.cap_id} came back as #{created.number}",
                    )
            except GhError as e:
                self.failed("create_issue", e)

    def update(self, plan: Plan) -> None:
        patches: dict[int, IssuePatch] = {}
        for rt in plan.retitles:
            p = patches.setdefault(rt.number, IssuePatch())
            p.title = rt.title
        for rb in plan.rebodies:
            p = patches.setdefault(rb.number, IssuePatch())
            p.body = rb.body
        for rp in plan.retypes:
            p = patches.setdefault(rp.number, IssuePatch())
            p.issue_type = rp.issue_type

        for number, patch in sorted(patches.items()):
            self.charge()
            try:
                after = self.gh.update_issue(self.repo, number, patch)
                title_ok = patch.title is None or after.title == patch.title
                body_ok = patch.body is None or after.body == patch.body
                type_ok = (
                    patch.issue_type is None or after.issue_type == patch.issue_type
                )
                if title_ok and body_ok and type_ok:
                    self.summary.updates += 1
                else:
                    self.readback_failed("update_issue", f"#{number} did not change")
            except GhError as e:
                self.failed("update_issue", e)

    def relabel(self, plan: Plan) -> None:
        for rl in plan.relabels:
            if rl.add:
                self.charge()
                try:
                    self.gh.add_labels(self.repo, rl.number, sorted(rl.add))
                    self.summary.relabels += 1
                except GhError as e:
                    self.failed("add_labels", e)
            for name in sorted(rl.remove):
                self.charge()
                try:
                    self.gh.remove_label(self.repo, rl.number, name)
                    self.summary.relabels += 1
                except GhError as e:
                    self.failed("remove_label", e)

    def structure(
        self,
        desired: dict[str, DesiredIssue],
        relations: ObservedRelations,
    ) -> None:
        attaches, blocked_by_adds, over_ceiling = edge_writes(
            desired, self.refs, relations
        )
        self.summary.findings = [
            f
            for f in self.summary.findings
            if not isinstance(f, Finding.SubIssueCeiling)
        ]
        self.summary.findings.extend(over_ceiling)

        for a in attaches:
            self.charge()
            try:
                if a.previous_parent is not None:
                    children = self.gh.list_sub_issues(self.repo, a.previous_parent)
                    if not any(child.id == a.child_id for child in children):
                        raise GhError(
                            "The child no longer belongs to the expected parent."
                        )
                    self.gh.remove_sub_issue(self.repo, a.previous_parent, a.child_id)
                    self.charge()
                self.gh.add_sub_issue(self.repo, a.parent_number, a.child_id)
                self.summary.attaches += 1
            except GhError as e:
                self.failed("add_sub_issue", e)

        for b in blocked_by_adds:
            self.charge()
            try:
                self.gh.add_blocked_by(self.repo, b.number, b.blocker_id)
                self.summary.blocked_by_adds += 1
            except GhError as e:
                self.failed("add_blocked_by", e)

    def reopen(self, plan: Plan) -> None:
        for ro in plan.reopens:
            self.charge()
            try:
                after = self.gh.update_issue(
                    self.repo, ro.number, IssuePatch(state=DesiredState.open_state())
                )
                if after.state == "open":
                    self.summary.reopens += 1
                else:
                    self.readback_failed(
                        "update_issue", f"#{ro.number} is still {after.state}"
                    )
            except GhError as e:
                self.failed("update_issue", e)

    def close(self, closes: list[Close]) -> None:
        for cl in closes:
            self.charge()
            want = DesiredState.closed_state(cl.reason)
            patch = IssuePatch(state=want)
            try:
                after = self.gh.update_issue(self.repo, cl.number, patch)
                if after.state == want.wire_state():
                    self.summary.closes += 1
                else:
                    self.readback_failed(
                        "update_issue", f"#{cl.number} is still {after.state}"
                    )
            except GhError as e:
                self.failed("update_issue", e)


#: The actor, session and harness of every write the sync makes to the store.
SYNC_ACTOR = "github-sync"
_EFFORTS = re.compile(r"\befforts=(\S+)")
_PULLED_AT = re.compile(r"\bpulled_at=(\S+)")


class PullRefused(StoreError):
    """nMEMORY refused a staged GitHub change; the sync writes nothing over it."""


def _no_key() -> bytes:
    raise StoreError("the GitHub pull forgets nothing and holds no HMAC key")


def _tool(
    store: Store, project: str, name: str, arguments: dict[str, Any], now: datetime
) -> dict[str, Any]:
    """One nMEMORY tool call through its MCP handler, in one transaction.

    The same gates a client meets (taint, language, proof, the staged review
    event) judge what the pull writes.
    """
    state = HandlerState(
        store=store,
        anchor_root=os.getcwd(),
        embeddings=None,
        read_only=False,
        home_dir=os.environ.get("HOME"),
        project_dir=os.getcwd(),
        effective_defaults=lambda context: effective_defaults(project, None, context),
        resolve_hmac_key=_no_key,
    )
    context = CaptureContext(harness=SYNC_ACTOR, session_id=SYNC_ACTOR)
    validated = validate_tool_arguments(name, arguments)
    with store.db.tool_boundary():
        result = TOOLS[name](state, validated, context, now, SYNC_ACTOR)
        if "error" in result or result.get("isError"):
            # Raised inside the boundary, so nothing the handler wrote commits.
            raise PullRefused(f"{name}: {json.dumps(result)[:500]}")
    payload: dict[str, Any] = json.loads(result["content"][0]["text"])
    return payload


def watermark(
    store: Store, repo: str, efforts: tuple[str, ...] = ()
) -> datetime | None:
    """The start of the newest complete apply run that covered `efforts`, or None."""
    wanted = {ParsedCapsuleRef.parse(e).canonical_id() for e in efforts}
    covered: dict[str, datetime] = {}
    for event in store.audit.list_audit(subject=repo):
        found = _PULLED_AT.search(event.reason or "")
        if event.actor != SYNC_ACTOR or found is None:
            continue
        at = parse_rfc3339(found[1])
        fenced = _EFFORTS.search(event.reason or "")
        scope = fenced[1] if fenced is not None else "all"
        if not wanted:
            if scope == "all":
                return at
            continue
        names = (
            wanted
            if scope == "all"
            else {
                ParsedCapsuleRef.parse(x).canonical_id() for x in scope.split(",") if x
            }
        )
        for epic in wanted & names:
            covered.setdefault(epic, at)
        if len(covered) == len(wanted):
            return min(covered.values())
    return None


@dataclass(frozen=True)
class Change:
    """One change someone made to a task issue on GitHub."""

    field: str
    action: str
    actor: str | None
    at: str
    kind: str
    ident: str
    after: str


def _latest(events: list[GhEvent], *names: str) -> GhEvent | None:
    return next((e for e in reversed(events) if e.event in names), None)


def changes_of(
    issue: GhIssue,
    want: DesiredIssue,
    events: list[GhEvent],
    editor: tuple[str, str] | None,
    label: str,
    cap_id: str,
    since: datetime | None,
) -> list[Change]:
    """What GitHub holds that the board does not, each with the event that made it.

    A difference no event explains is the board's own (a store-side change), so
    the sync writes it as before.
    """
    out: list[Change] = []
    if want.state.is_open and issue.state == "closed":
        event = _latest(events, "closed")
        reason = issue.state_reason or "completed"
        if event is not None:
            out.append(
                Change(
                    "state",
                    f"closed-{reason}",
                    event.actor,
                    event.at,
                    "event",
                    event.id,
                    reason,
                )
            )
    elif not want.state.is_open and issue.state_reason == "reopened":
        event = _latest(events, "reopened")
        if issue.state == "open" and event is not None:
            out.append(
                Change(
                    "state",
                    "reopened",
                    event.actor,
                    event.at,
                    "event",
                    event.id,
                    "reopened",
                )
            )
    if issue.title != want.title:
        renames = [e for e in events if e.event == "renamed"]
        current = issue.title
        for event in reversed(renames):
            if event.rename_to != current:
                break
            current = event.rename_from or ""
            if current == want.title:
                break
        if renames and renames[-1].rename_to == issue.title and current == want.title:
            event = renames[-1]
            out.append(
                Change(
                    "title",
                    "renamed",
                    event.actor,
                    event.at,
                    "event",
                    event.id,
                    issue.title,
                )
            )
    if issue.body.replace("\r\n", "\n") != want.body and editor is not None:
        after, action = stripped_body(issue.body, label, cap_id)
        out.append(
            Change("body", action, editor[0], editor[1], "edit", editor[1], after)
        )
    owned = sync_owned_labels(label)
    moved = want.labels ^ (set(issue.labels) & owned)
    for event in events:
        if event.event not in ("labeled", "unlabeled") or event.label is None:
            continue
        if event.label in owned:
            latest = _latest(
                [e for e in events if e.label == event.label], "labeled", "unlabeled"
            )
            if event.label in moved and latest is event:
                out.append(
                    Change(
                        "labels",
                        event.event,
                        event.actor,
                        event.at,
                        "event",
                        event.id,
                        event.label,
                    )
                )
        elif since is None or parse_rfc3339(event.at) >= since:
            out.append(
                Change(
                    "label",
                    event.event,
                    event.actor,
                    event.at,
                    "event",
                    event.id,
                    event.label,
                )
            )
    return out


class Puller:
    """Stages each GitHub change to a task issue as a proposal about its task."""

    def __init__(
        self,
        store: Store,
        gh: GhClient,
        config: SyncConfig,
        applier: Applier,
        now: datetime,
    ) -> None:
        self.store, self.gh, self.config = store, gh, config
        self.applier, self.now = applier, now
        self.failed: set[int] = set()

    def stage(self, number: int, cap_id: str, change: Change, login: str) -> None:
        if change.actor is None or change.actor == login:
            return
        summary = self.applier.summary
        line = proposal_line(
            self.config.repo,
            number,
            change.field,
            change.action,
            change.actor,
            change.at,
            change.kind,
            change.ident,
        )
        if line is None:
            self.failed.add(number)
            summary.errors.append(f"stage #{number} {change.field}: not a GitHub token")
            return
        if summary.writes >= self.applier.budget:
            summary.partial = True
            raise StopException()
        item: dict[str, Any] = {
            "kind": "fact",
            "content": line,
            "source": GITHUB_PULL_SOURCE,
            "anchor": f"github:{self.config.repo}#{number}",
            "project_id": self.config.project,
            "authority_class": "externally-imported",
            "staged": True,
        }
        if change.after:
            item["source_quote"] = change.after
            item["derived_from"] = [change.after]
        try:
            row = _tool(
                self.store,
                self.config.project,
                "nmemory_capsule_ingest",
                item,
                self.now,
            )["outcomes"][0]
            if row.get("status") not in ("captured", "deduplicated"):
                raise PullRefused(json.dumps(row)[:500])
            _tool(
                self.store,
                self.config.project,
                "nmemory_relation_add",
                {"kind": "about", "from": row["id"], "to": cap_id},
                self.now,
            )
        except (StoreError, ValueError) as e:
            self.failed.add(number)
            summary.errors.append(f"stage #{number} {change.field}: {e}")
            return
        if row["status"] == "captured":
            summary.writes += 1
            summary.staged += 1

    def pull(self, tick: Tick, since: datetime | None) -> None:
        try:
            login = self.gh.viewer_login()
        except GhError as e:
            self.applier.failed("viewer_login", e)
            raise StopException() from e
        tasks = {
            issue.number: (cap_id, issue)
            for cap_id, issue in tick.issues.items()
            if cap_id in tick.desired
            and DesiredKind.TASK.as_label() in tick.desired[cap_id].labels
        }
        for number, (cap_id, issue) in sorted(tasks.items()):
            if (
                since is not None
                and issue.updated_at is not None
                and parse_rfc3339(issue.updated_at) < since
            ):
                continue
            want = tick.desired[cap_id]
            try:
                events = self.gh.list_timeline(self.config.repo, number)
                editor = (
                    self.gh.body_editor(self.config.repo, number)
                    if issue.body.replace("\r\n", "\n") != want.body
                    else None
                )
            except GhError as e:
                self.failed.add(number)
                self.applier.failed("list_timeline", e)
                continue
            for change in changes_of(
                issue, want, events, editor, self.config.label, cap_id, since
            ):
                self.stage(number, cap_id, change, login)
        if not tasks:
            return
        comment_since = since or min(
            (
                c.created_at
                for cap_id, _ in tasks.values()
                if (c := self.store.capsules.get(cap_id)) is not None
            ),
            default=None,
        )
        try:
            comments = self.gh.list_comments(
                self.config.repo,
                None if comment_since is None else format_rfc3339(comment_since),
            )
        except GhError as e:
            self.applier.failed("list_comments", e)
            return
        for comment in comments:
            if comment.issue in tasks:
                self.stage(
                    comment.issue,
                    tasks[comment.issue][0],
                    Change(
                        "comment",
                        "added",
                        comment.actor,
                        comment.at,
                        "comment",
                        comment.id,
                        comment.body,
                    ),
                    login,
                )


def sync(
    store: Store,
    gh: GhClient,
    config: SyncConfig,
    max_writes: int,
    now: datetime,
) -> ApplySummary:
    """One apply tick: pull GitHub's changes as proposals, then write the minimal
    diff to GitHub, leaving every field a waiting proposal holds."""
    tick = read_tick(store, gh, config)
    applier = Applier(
        gh=gh,
        repo=config.repo,
        label=config.label,
        budget=max_writes,
        summary=ApplySummary(
            findings=list(tick.plan.findings),
            pull_candidates=len(tick.plan.pull_candidates),
            adopt_candidates=len(tick.plan.adopt_candidates),
        ),
        types=tick.types,
        refs=dict(tick.refs),
    )
    puller = Puller(store, gh, config, applier, now)

    try:
        puller.pull(tick, watermark(store, config.repo, config.efforts))
        held = held_fields(store, config.repo)
        held |= {(n, name) for n in puller.failed for name in HELD_FIELDS}
        hold(tick.plan, held)
        applier.summary.held = tick.plan.held
        has_work = bool(
            tick.plan.creates
            or tick.plan.retitles
            or tick.plan.rebodies
            or tick.plan.retypes
            or tick.plan.relabels
            or tick.plan.attaches
            or tick.plan.blocked_by_adds
            or tick.plan.closes
            or tick.plan.reopens
        )
        closes = list(tick.plan.closes)
        if has_work:
            try:
                gh.ensure_label(config.repo, config.label)
            except GhError as e:
                applier.failed("ensure_label", e)
            applier.create(tick.plan.creates, closes)
            applier.update(tick.plan)
            applier.relabel(tick.plan)
            applier.structure(tick.desired, tick.relations)
            applier.reopen(tick.plan)
            applier.close(closes)
    except StopException:
        pass

    summary = applier.summary
    complete = summary.fatal is None and not summary.errors and not summary.partial
    if summary.writes > 0 or complete:
        # A line `writes=9` with no record that a fence suppressed the rest
        # is forensically misleading, precisely for this change.
        fence_note = (
            ",".join(ParsedCapsuleRef.parse(e).canonical_id() for e in config.efforts)
            if config.efforts
            else "all"
        )
        reason = f"{summary.audit_reason()} efforts={fence_note}"
        if complete:
            # The next pull reads only what changed since this run started.
            reason += f" pulled_at={format_rfc3339(now)}"
        try:
            store.audit.append_audit(SYNC_ACTOR, "apply", config.repo, reason, now)
        except Exception as e:  # noqa: BLE001
            summary.errors.append(f"append_audit: {e}")

    return summary
