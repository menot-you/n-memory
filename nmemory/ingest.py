"""Ingest — provenance-mandatory, idempotent capture.

The one door into memory: every capsule is born here, and none is born
without provenance. The flow is validate -> hash -> probe ->
default-fill -> construct -> append.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from .capsule import (
    AuthorityClass,
    Capsule,
    EmptyContent,
    Freshness,
    InvertedFreshnessWindow,
    Provenance,
    Scope,
    sha256_hex,
)
from .capture import CaptureContext
from .lexical import lexical_tokens, lexical_words
from .project import DEFAULT_PROJECT_ID, canonical_project
from .queries.curator import sibling_excluded_among
from .relation import RelationKind
from .renamed import env
from .repositories.capsules import EventTimeRange, Tombstoned
from .store import Store
from .taint import TaintFinding
from .taint import is_suspicious as taint_is_suspicious
from .taint import scan as taint_scan

DEFAULT_CONFIDENCE: float = 0.6
MAX_ANCHOR_STUB_LINES: int = 20
DEDUP_HINT_MIN_SCORE: float = 0.5
DEDUP_HINT_MIN_TOKENS: int = 4
DEDUP_HINT_LONG_FORM_MIN_TOKENS: int = 24
DEDUP_MAX_QUERY_TERMS: int = 12
SIBLING_TOP_K: int = 3
HINT_CANDIDATE_LIMIT: int = 128


@dataclass
class IngestRequest:
    content: str
    source: str
    anchor: str
    confidence: float | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    project_id: str | None = None
    authority_class: AuthorityClass | None = None
    instruction_taint: bool | None = None
    supersedes: str | None = None
    session_id: str | None = None
    event_time: EventTimeRange | None = None
    source_quote: str | None = None
    #: The BYTES of the source turns from which this content is DERIVED; the rule and
    #: fence are in `ingest()` (TAINT INHERITANCE FROM SOURCE BYTES).
    derived_from: tuple[str, ...] | None = None
    #: False skips `near_duplicate_hint` and `sibling_hints`: a machine-written
    #: record that never reads them stops paying their scan of the store.
    hints: bool = True

    def __post_init__(self) -> None:
        # Validating constructor: declaring derivation without bytes (or with a blank
        # member, which scans clean) is back-door laundering — refuse.
        if self.derived_from is not None and (
            not self.derived_from
            or any(not isinstance(b, str) or not b.strip() for b in self.derived_from)
        ):
            raise EmptyDerivedSource()


@dataclass(frozen=True)
class IngestDefaults:
    project_id: str
    source: str = "literal_default"


def client_project_slug(name: str | None) -> str | None:
    """A client name as a project key: lower ASCII words joined by `-`, 32 at most.

    None for an absent name, and for one that slugs to nothing or to `default`.
    """
    if not name:
        return None
    slug: list[str] = []
    for c in name.strip().lower():
        if c.isalnum() and c.isascii():
            slug.append(c)
        elif slug and slug[-1] != "-":
            slug.append("-")
    res = "".join(slug)[:32].rstrip("-")
    if not res or res == "default":
        return None
    return res


def effective_defaults(
    project_id: str | None,
    client_name: str | None,
    context: CaptureContext | None,
) -> IngestDefaults:
    """The project a capture without one lands in, and which source named it.

    In order: the caller's `project_id`; the slug of the client's name (from the
    MCP `initialize`, else the capture context's harness); `NOTT_NMEMORY_PROJECT`;
    `DEFAULT_PROJECT_ID`.
    """
    if project_id is not None:
        return IngestDefaults(project_id=project_id, source="caller")
    if not client_name and context and context.harness:
        client_name = context.harness.split("/")[0]
    derived = client_project_slug(client_name)
    if derived:
        return IngestDefaults(project_id=derived, source="client_info")
    env_project = env("NOTT_NMEMORY_PROJECT")
    # `.strip()` and not `is not None`: the server constructor already rejects an
    # empty project and the env rejected nothing, so `NOTT_NMEMORY_PROJECT=` entered as
    # default `""`. An empty default is absence of project written another way,
    # and downstream it ceased to be harmless: as a PREFIX, `""` caused the vector
    # lane to lose the fence entirely. Caught by independent review 2026-09-18.
    if env_project is not None and env_project.strip():
        return IngestDefaults(project_id=env_project, source="env")
    return IngestDefaults(project_id=DEFAULT_PROJECT_ID, source="literal_default")


@dataclass(frozen=True)
class DedupHint:
    similar_id: str
    score: float


@dataclass(frozen=True)
class SiblingHint:
    id: str
    score: float


@dataclass(frozen=True)
class CoverageMatch:
    id: str
    content: str
    score: float


@dataclass
class IngestOutcome:
    id: str
    deduped: bool
    dedup_hint: DedupHint | None
    taint_findings: list[TaintFinding] = field(default_factory=list)
    superseded: str | None = None
    siblings: list[SiblingHint] = field(default_factory=list)
    #: The scan of source BYTES declared in `derived_from`, turn by turn.
    #: In a dedup against a clean incumbent, this is the only witness that the
    #: derivation was hostile: incumbent bytes do not change, the line is what observes it.
    derivation_findings: list[TaintFinding] = field(default_factory=list)
    #: U4 (nmemory-curator): the declared project was an alias spelling and was
    #: folded to the canonical key — `(was, now)`, reported to caller.
    project_canon: tuple[str, str] | None = None
    #: Another writer inserted this content after the initial lookup.
    deduped_at_insert: bool = False


class IngestError(Exception):
    pass


class ProvenanceMissing(IngestError):
    def __init__(self, field: str) -> None:
        super().__init__(
            f"ingest rejected: provenance.{field} is empty (no capsule without provenance)"
        )
        self.field = field

    def __eq__(self, other: object) -> bool:
        return isinstance(other, ProvenanceMissing) and self.field == other.field


class InvalidAnchor(IngestError):
    def __init__(self, lines: int) -> None:
        super().__init__(
            f"ingest rejected: anchor spans {lines} lines: not path:line / doc-<id> / &<id> / "
            f"PR-<n>, and a content stub may have at most {MAX_ANCHOR_STUB_LINES} lines"
        )
        self.lines = lines

    def __eq__(self, other: object) -> bool:
        return isinstance(other, InvalidAnchor) and self.lines == other.lines


class UnknownSupersedeTarget(IngestError):
    def __init__(self, target: str) -> None:
        super().__init__(
            f"ingest rejected: supersedes target {target} is not a stored capsule; nothing was captured"
        )
        self.target = target

    def __eq__(self, other: object) -> bool:
        return isinstance(other, UnknownSupersedeTarget) and self.target == other.target


class TombstonedSupersedeTarget(IngestError):
    def __init__(self, target: str) -> None:
        super().__init__(
            f"ingest rejected: supersedes target {target} is tombstoned; nothing was captured"
        )
        self.target = target

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, TombstonedSupersedeTarget) and self.target == other.target
        )


class TombstonedContent(IngestError):
    def __init__(self, id: str) -> None:
        super().__init__(
            f"ingest rejected: this exact content was forgotten (tombstone {id}); "
            "forget is sticky, nothing was captured"
        )
        self.id = id

    def __eq__(self, other: object) -> bool:
        return isinstance(other, TombstonedContent) and self.id == other.id


class UnknownSession(IngestError):
    def __init__(self, session_id: str) -> None:
        super().__init__(
            f"ingest rejected: session {session_id} was never opened; nothing was captured"
        )
        self.session_id = session_id

    def __eq__(self, other: object) -> bool:
        return isinstance(other, UnknownSession) and self.session_id == other.session_id


class SessionFinished(IngestError):
    def __init__(self, session_id: str) -> None:
        super().__init__(
            f"ingest rejected: session {session_id} is already finished; nothing was captured"
        )
        self.session_id = session_id

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, SessionFinished) and self.session_id == other.session_id
        )


class EmptyDerivedSource(IngestError):
    """Derivation declaration without bytes to scan (nmemory-supremacy Lock 10).

    `derived_from` is the scan surface for taint inheritance: declaring derivation
    without supplying bytes is declaring a scan without performing it — laundering
    taint back in through the back door.
    """

    def __init__(self) -> None:
        super().__init__(
            "ingest rejected: derived_from declares no source-turn bytes to scan; "
            "an empty derivation declaration is the taint-laundering hole it exists to close"
        )


class CapsuleIngestError(IngestError):
    def __init__(self, err: Exception) -> None:
        super().__init__(f"ingest rejected: {err}")
        self.err = err

    def __eq__(self, other: object) -> bool:
        if isinstance(other, CapsuleIngestError):
            return type(self.err) is type(other.err) or str(self.err) == str(other.err)
        return False


def validate_anchor(anchor: str) -> None:
    lines = len(anchor.splitlines())
    if lines > MAX_ANCHOR_STUB_LINES:
        raise InvalidAnchor(lines)


def significant_tokens(text: str) -> list[str]:
    """Tokens of 3+ characters, without repetition, in order of first appearance.

    `token not in out` on a LIST made this O(tokens²), and ingest calls it
    once per candidate across two loops (the third and fourth tokenization of
    each item). Output is the SAME — the set only decides membership, the list
    still preserves order.
    """
    out: list[str] = []
    vistos: set[str] = set()
    for token in lexical_tokens(text):
        if len(token) >= 3 and token not in vistos:
            vistos.add(token)
            out.append(token)
    return out


def full_tokens(text: str) -> set[str]:
    return lexical_words(text)


def containment(
    a: set[str],
    b: set[str],
    *,
    divisor: Literal["larger", "shorter"] = "larger",
) -> float:
    """Token overlap with the caller's declared containment semantics.

    Ingest keeps its historical ``larger`` denominator: two differently sized
    claims are not near-duplicates merely because the shorter appears inside the
    longer. Read-only coverage asks the opposite question — whether a capsule's
    complete claim appears in a larger message — and therefore uses ``shorter``.
    """
    if divisor == "larger":
        denominator = max(len(a), len(b))
    elif divisor == "shorter":
        denominator = min(len(a), len(b))
    else:
        raise ValueError(f"unknown containment divisor {divisor!r}")
    if denominator == 0:
        return 0.0
    return len(a & b) / denominator


def reported_score(raw: float) -> float:
    rounded = round(raw, 2)
    return 0.99 if rounded >= 1.0 else rounded


def near_duplicate_hint(
    store: Store,
    content: str,
    excluded_id: str | None = None,
) -> DedupHint | None:
    incoming_significant = significant_tokens(content)
    if not incoming_significant:
        return None
    incoming_full = full_tokens(content)
    query = incoming_significant[:DEDUP_MAX_QUERY_TERMS]

    candidates = store.capsules.search_fts_scoped(
        query, project_id=None, limit=HINT_CANDIDATE_LIMIT
    )
    candidate_ids = [c[0].id for c in candidates]
    superseded = store.relations.superseded_among(candidate_ids)

    best: tuple[float, int, str] | None = None
    for stored, _ in candidates:
        if excluded_id == stored.id or stored.id in superseded:
            continue
        candidate_significant = significant_tokens(stored.capsule.content)
        if (
            min(len(incoming_significant), len(candidate_significant))
            < DEDUP_HINT_MIN_TOKENS
        ):
            continue
        candidate_full = full_tokens(stored.capsule.content)
        score = containment(incoming_full, candidate_full)
        shorter = min(len(incoming_full), len(candidate_full))
        if (
            DEDUP_HINT_MIN_SCORE / 2 <= score < DEDUP_HINT_MIN_SCORE
            and shorter >= DEDUP_HINT_LONG_FORM_MIN_TOKENS
        ):
            score = len(incoming_full & candidate_full) / shorter
        stronger = False
        if best is None:
            stronger = True
        else:
            best_score, best_seq, _ = best
            if score > best_score or score == best_score and stored.seq < best_seq:
                stronger = True
        if stronger:
            best = (score, stored.seq, stored.id)

    if best is not None and best[0] >= DEDUP_HINT_MIN_SCORE:
        return DedupHint(similar_id=best[2], score=reported_score(best[0]))
    return None


def sibling_hints(
    store: Store,
    content: str,
    project_id: str,
    excluded_id: str | None = None,
) -> list[SiblingHint]:
    incoming_significant = significant_tokens(content)
    if not incoming_significant:
        return []
    incoming_full = full_tokens(content)
    query = incoming_significant[:DEDUP_MAX_QUERY_TERMS]

    candidates = store.capsules.search_fts_scoped(
        query, project_id=project_id, limit=HINT_CANDIDATE_LIMIT
    )
    candidate_ids = [c[0].id for c in candidates]
    excluded = sibling_excluded_among(store.db, candidate_ids)

    scored: list[tuple[float, int, str]] = []
    for stored, _ in candidates:
        if excluded_id == stored.id or stored.id in excluded:
            continue
        candidate_significant = significant_tokens(stored.capsule.content)
        if (
            min(len(incoming_significant), len(candidate_significant))
            < DEDUP_HINT_MIN_TOKENS
        ):
            continue
        score = containment(incoming_full, full_tokens(stored.capsule.content))
        if score >= DEDUP_HINT_MIN_SCORE:
            scored.append((score, stored.seq, stored.id))

    scored.sort(key=lambda item: (-item[0], item[1]))
    return [
        SiblingHint(id=item[2], score=reported_score(item[0]))
        for item in scored[:SIBLING_TOP_K]
    ]


def coverage_matches(
    store: Store,
    text: str,
    *,
    project_id: str | None,
    session_id: str | None,
    min_score: float,
    limit: int,
    now: datetime,
) -> list[CoverageMatch]:
    """Find live ratified capsules whose full claim appears in ``text``.

    This is the read-only half of compaction. It deliberately does not reuse the
    ingest threshold fallback: coverage always divides by the shorter token set,
    while the two ingest callers retain ``containment(..., divisor="larger")``.
    """
    significant = significant_tokens(text)
    if not significant or limit == 0:
        return []
    incoming = full_tokens(text)
    candidates = store.capsules.search_fts_scoped(
        significant[:DEDUP_MAX_QUERY_TERMS],
        project_id=project_id,
        limit=max(HINT_CANDIDATE_LIMIT, limit),
    )
    excluded = sibling_excluded_among(store.db, [stored.id for stored, _ in candidates])
    scored: list[tuple[float, int, CoverageMatch]] = []
    for stored, _ in candidates:
        if (
            stored.id in excluded
            or stored.capsule.instruction_taint
            or store.reviews.review_verdict(stored.id) != "ratified"
            or (session_id is not None and stored.session_id != session_id)
        ):
            continue
        valid_to = stored.capsule.freshness.valid_to
        if valid_to is not None and valid_to < now:
            continue
        score = containment(
            incoming,
            full_tokens(stored.capsule.content),
            divisor="shorter",
        )
        if score >= min_score:
            match = CoverageMatch(
                id=stored.id,
                content=stored.capsule.content,
                score=round(score, 4),
            )
            scored.append((score, stored.seq, match))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [item[2] for item in scored[:limit]]


def system1_taint_surfaces(
    content: str,
    source_quote: str | None,
    derived_from: Sequence[str] | None,
    authority_class: AuthorityClass | None,
    instruction_taint: bool | None,
) -> tuple[str, ...]:
    """The surfaces System1's taint probe still has to measure.

    The same surfaces `ingest_capture` scans with the regex, each one alone. Empty
    when the capsule is tainted already: externally imported, flagged by the caller,
    or a regex hit on any surface. The probe itself runs in the caller, before the
    store's write lock; this module makes no network call.
    """
    turns = tuple(derived_from or ())
    if (
        authority_class == AuthorityClass.EXTERNALLY_IMPORTED
        or instruction_taint is True
        or taint_scan(content)
        or (source_quote is not None and taint_is_suspicious(source_quote))
        or any(taint_scan(turn) for turn in turns)
    ):
        return ()
    quote = (source_quote,) if source_quote is not None else ()
    return (content, *quote, *turns)


def ingest(
    store: Store,
    req: IngestRequest,
    defaults: IngestDefaults,
    now: datetime,
    context: CaptureContext | None = None,
) -> IngestOutcome:
    return ingest_capture(store, req, defaults, context=context, now=now)


def ingest_capture(
    store: Store,
    req: IngestRequest,
    defaults: IngestDefaults,
    context: CaptureContext | None,
    now: datetime,
) -> IngestOutcome:
    if not req.source.strip():
        raise ProvenanceMissing("source")
    if not req.anchor.strip():
        raise ProvenanceMissing("anchor")
    if not req.content.strip():
        raise CapsuleIngestError(EmptyContent())

    validate_anchor(req.anchor)

    if req.supersedes:
        try:
            target = store.capsules.get(req.supersedes)
            if target is None:
                raise UnknownSupersedeTarget(req.supersedes)
        except Tombstoned:
            raise TombstonedSupersedeTarget(req.supersedes)

    if req.session_id:
        session = store.sessions.get_session(req.session_id)
        if session is None:
            raise UnknownSession(req.session_id)
        if session.finished_at is not None:
            raise SessionFinished(req.session_id)

    # The SERVED contract of `nmemory_capsule_ingest` (`tools_schema.json`): "a
    # hijack-shaped quote flags the capsule instruction_taint=true exactly as
    # hijack-shaped content does". The quote is surfaced verbatim, so a capsule
    # of benign content with a hijack-shaped quote cannot go unflagged.
    #
    # TWO separate judgments, never a join: scanning `"\n".join(...)` of the two
    # fields places terms from different groups into the
    # proximity window — two individually harmless fields manufactured a
    # rule that neither carries. The pin scans the quote ALONE and folds the boolean
    # only into the FLAG (`server/mod.rs:3677-3685`), without ever reporting its findings
    # in the row: the row describes what was scanned from the content.
    taint_findings = taint_scan(req.content)
    quote_is_tainted = req.source_quote is not None and taint_is_suspicious(
        req.source_quote
    )
    derivation_findings = [
        finding for turn in (req.derived_from or ()) for finding in taint_scan(turn)
    ]
    source_hash = sha256_hex(req.content)

    try:
        existing = store.capsules.find_by_source_hash(source_hash)
    except Tombstoned as t:
        raise TombstonedContent(t.id)

    if existing is not None:
        # This session just reaffirmed these bytes: they should not return to it
        # via inbox (the self-delivery fence of `inbox_candidates`).
        store.capsules.note_own_write(existing.id)
        superseded_id = None
        if req.supersedes and req.supersedes != existing.id:
            store.add_relation(
                RelationKind.SUPERSEDES,
                from_id=existing.id,
                to_id=req.supersedes,
                now=now,
            )
            superseded_id = req.supersedes
        return IngestOutcome(
            id=existing.id,
            deduped=True,
            dedup_hint=None,
            taint_findings=taint_findings,
            superseded=superseded_id,
            siblings=[],
            derivation_findings=derivation_findings,
        )

    dedup_hint = (
        near_duplicate_hint(store, req.content, req.supersedes) if req.hints else None
    )
    # U4 (nmemory-curator): the port never refuses; the spelling folds. `was` is
    # provenance from the caller and travels in the outcome — folding is never silent.
    raw_project = req.project_id if req.project_id is not None else defaults.project_id
    project_id = canonical_project(raw_project) if raw_project is not None else None
    project_canon = (
        (raw_project, project_id)
        if raw_project is not None and project_id != raw_project
        else None
    )
    siblings = (
        sibling_hints(store, req.content, project_id, req.supersedes)
        if req.hints
        else []
    )

    authority_class = (
        req.authority_class
        if req.authority_class is not None
        else AuthorityClass.AGENT_INFERRED
    )
    instruction_taint = (
        authority_class == AuthorityClass.EXTERNALLY_IMPORTED
        or (req.instruction_taint is True)
        or bool(taint_findings)
        or quote_is_tainted
        or bool(derivation_findings)
    )
    confidence = req.confidence if req.confidence is not None else DEFAULT_CONFIDENCE

    valid_from = req.valid_from if req.valid_from is not None else now
    if req.valid_to is not None and req.valid_to < valid_from:
        raise CapsuleIngestError(InvertedFreshnessWindow(valid_from, req.valid_to))

    capsule = Capsule(
        content=req.content,
        provenance=Provenance(
            source=req.source,
            anchor=req.anchor,
            source_hash=source_hash,
        ),
        confidence=confidence,
        freshness=Freshness(valid_from=valid_from, valid_to=req.valid_to),
        scope=Scope(project_id=project_id),
        authority_class=authority_class,
        instruction_taint=instruction_taint,
    )

    try:
        cid, created = store.append_capture(
            capsule,
            session_id=req.session_id,
            event_time=req.event_time,
            context=context,
            now=now,
        )
    except Tombstoned as t:
        raise TombstonedContent(t.id) from t
    superseded_id = None
    if req.supersedes and req.supersedes != cid:
        store.add_relation(
            RelationKind.SUPERSEDES, from_id=cid, to_id=req.supersedes, now=now
        )
        superseded_id = req.supersedes

    return IngestOutcome(
        id=cid,
        deduped=not created,
        dedup_hint=dedup_hint if created else None,
        taint_findings=taint_findings,
        superseded=superseded_id,
        siblings=siblings if created else [],
        derivation_findings=derivation_findings,
        project_canon=project_canon if created else None,
        deduped_at_insert=not created,
    )
