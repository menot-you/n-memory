"""The curator's policy: what may reach a model, in what order, and what its answer means.

Pure decisions only. `curator.CuratorWorker` keeps the thread loop, the election and every
store, model and file access; it asks this module which candidate is eligible, how the
triaged batch is ordered, how a synthesis draft is judged and which System1 anchor
answer settles a verdict.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from .capsule import Capsule
from .consolidate import ConsolidationRecord
from .taint import is_suspicious

#: The three excerpts the curator reads when there is NO file to read. They are not
#: content: they are its absence, and none of them is judged.
NO_EVIDENCE: Final = frozenset(
    {"<file missing>", "<file unreadable>", "<anchor is not a code path>"}
)

#: How many capsules a cluster needs to have to be worth a synthesis, and how many a
#: draft must cover. Two is the arithmetic minimum of "multiple asserted the same
#: thing"; not a taste threshold.
DIGEST_MIN_MEMBERS: Final = 2


def system1_settles(choice: str) -> bool:
    """System1's anchor answer is applied only when it holds or is false.

    A moved result without a concrete new anchor stays an observation for the daily
    pass.
    """
    return choice in ("holds", "false")


def tainted(capsule: Capsule) -> bool:
    """Bytes that never reach a model: flagged at capture, or suspicious now."""
    return capsule.instruction_taint or is_suspicious(capsule.content)


def clean_proposals(proposals: Sequence[tuple[str, Capsule]]) -> dict[str, str]:
    """The staged proposals whose bytes may leave the box, by id, in order."""
    return {cid: cap.content for cid, cap in proposals if not tainted(cap)}


def excerpt_refusal(judgement: str, path: str, excerpt: str) -> tuple[str, str] | None:
    """The neutral skip `(result, detail)` of an excerpt no verdict may be bought on.

    THE ROOT FENCE: evidence that CANNOT be read never purchases a verdict (95
    `falsifies` edges were once created in the live store from such excerpts without a
    single informative call); `skipped_missing` claims only that there is no way to
    verify here. A suspicious excerpt is a taint skip.
    """
    if excerpt in NO_EVIDENCE:
        return "skipped_missing", f"{judgement}|{excerpt} for {path}"
    if is_suspicious(excerpt):
        return "skipped_taint", f"{judgement}|excerpt of {path} is suspicious"
    return None


def ambiguity(p: float, confidence: float | None) -> float:
    """How worthwhile the expensive judge is on an observed candidate: lowest first.

    `confidence` is OPTIONAL in transport and `p` is not: without this fallback a
    batch without confidence would collapse to 0.0 and yield the usual FIFO order with
    the triage call paid for. An unobserved candidate is worth 0.0 at the caller.
    """
    return p if confidence is None else confidence


def daily_judgement(anchor_results: Sequence[tuple[str, str]], anchor: str) -> str:
    """The judgement a daily anchor review asks about: the last anchor verification's
    `result at detail`, or a plain daily review of the anchor."""
    if anchor_results:
        result, detail = anchor_results[-1]
        return f"{result} at {detail}"
    return f"daily review at {anchor}"


def daily_mark_action(completed: bool, failed: bool) -> str:
    """The audit action of a daily mark: failed wins over complete, else pending."""
    if failed:
        return "curator_daily_failed"
    return "curator_daily_complete" if completed else "curator_daily_pending"


def in_scope(project: str, scope: str) -> bool:
    """A project inside the curator's scope: the scope itself or a project below it."""
    return project == scope or project.startswith(scope + "/")


def digest_source_refusal(members: Sequence[ConsolidationRecord]) -> str | None:
    """LOCK 1 for synthesis: the skip detail of a cluster holding tainted bytes, or None.

    Synthesizing them would place the injection inside a NEW capsule, which promotion
    would later ratify.
    """
    flagged = [m for m in members if m.instruction_taint]
    if flagged or any(is_suspicious(m.content) for m in members):
        return f"digest|{len(members)} members, {len(flagged)} tainted"
    return None


def digest_claims(members: Sequence[ConsolidationRecord]) -> str:
    """The claims a synthesis prompt lists, one per source."""
    return "\n".join(f"- {m.id}: {m.content}" for m in members)


def draft_refusal(
    statement: str, covers: Sequence[str], member_ids: Sequence[str]
) -> tuple[str, str] | list[str]:
    """Judge a synthesis draft: the skip `(result, detail)`, or the covered ids in order.

    An empty statement is the model saying these facts are not one (declined). A
    suspicious statement is refused: the source fence does not cover the RESPONSE.
    `covers` is normalized to a stable order without duplicates; a SUBSET is kept, a
    handle outside the cluster (a fabrication, R3) or fewer than two refuse.
    """
    if not statement:
        return "digest_declined", "digest|empty statement"
    if is_suspicious(statement):
        return "digest_refused", "digest|suspicious statement"
    seen = list(dict.fromkeys(covers))
    invented = [c for c in seen if c not in set(member_ids)]
    if invented:
        return "digest_refused", f"digest|handles outside cluster: {invented[:3]}"
    if len(seen) < DIGEST_MIN_MEMBERS:
        return "digest_refused", f"digest|covers {len(seen)} of {len(member_ids)}"
    return seen
