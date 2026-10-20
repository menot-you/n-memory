"""The Unit of Work over the SQLite store.

`Store` holds the `Database` (the transaction protocol), one repository per
owner of tables, and the composites: the operations that write one owner and
read or write another, each under one write boundary.
"""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import UTC, datetime

from . import audit_actions
from .anchor import parse_anchor
from .capsule import (
    Capsule,
    format_rfc3339,
    headline_of,
    sha256_hex,
)
from .capture import CaptureContext
from .classify import CandidateKind, ClassificationScope
from .consolidate import ConsolidationRecord, TierMove, protective_tier_moves
from .database import Database, create_store, store_path, writes
from .dod import DodEvent
from .queries.sidecars import recalls_after
from .queries.staged import (
    REVIEW_REASON_HORIZON,
    REVIEW_REASON_REVOKED,
    REVIEW_VERDICT_EXPIRED,
    REVIEW_VERDICT_REJECTED,
    expirable_proposals,
    unsupported_evidence,
)
from .queries.work import reject_invalidated_work_relations
from .relation import CLOSING_KIND_VALUES, RelationKind
from .repositories.aliases import AliasRepository
from .repositories.anchors import AnchorRepository
from .repositories.audit import AuditRepository
from .repositories.base import EmptyField, StoreError
from .repositories.capsules import (
    CapsuleRepository,
    EventTimeRange,
    Tombstoned,
    UnknownCapsule,
)
from .repositories.classifications import ClassificationRepository
from .repositories.corpus import CorpusRepository
from .repositories.dod import DodRepository
from .repositories.embeddings import EmbeddingRepository
from .repositories.epistemics import EpistemicsRepository
from .repositories.pins import PinRepository
from .repositories.relations import REVOCABLE_KINDS, RelationRepository
from .repositories.reviews import ReviewRepository
from .repositories.sessions import SessionRepository
from .repositories.sources import SourceRepository
from .repositories.substrate import SubstrateRepository
from .repositories.tiers import TierRepository
from .repositories.traffic import TrafficRepository
from .space import EmbeddingError, SpaceFingerprint


class Store:
    """The Unit of Work: `db`, one repository attribute per owner, and the composites."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self.capsules = CapsuleRepository(db)
        self.anchors = AnchorRepository(db)
        self.reviews = ReviewRepository(db)
        self.audit = AuditRepository(db)
        self.relations = RelationRepository(db)
        self.embeddings = EmbeddingRepository(db)
        self.tiers = TierRepository(db)
        self.classifications = ClassificationRepository(db)
        self.traffic = TrafficRepository(db)
        self.sources = SourceRepository(db)
        self.epistemics = EpistemicsRepository(db)
        self.pins = PinRepository(db)
        self.substrate = SubstrateRepository(db)
        self.corpus = CorpusRepository(db)
        self.sessions = SessionRepository(db)
        self.aliases = AliasRepository(db)
        self.dod = DodRepository(db)

    @classmethod
    def open(
        cls,
        path: str | os.PathLike[str] | None = None,
        *,
        read_only: bool = False,
        allow_live_store: bool = False,
    ) -> Store:
        """Open an existing store file (default: `store_path()`); never creates one."""
        return cls(
            Database(
                store_path() if path is None else path,
                read_only=read_only,
                allow_live_store=allow_live_store,
            )
        )

    @classmethod
    def open_disposable_test_store(cls) -> Store:
        """A new empty store file of its own, under `NOTT_DATABASE_DIR`."""
        import uuid

        import nott_database

        path = nott_database.directory() / f"nmemory-{uuid.uuid4().hex}.sqlite3"
        create_store(path)
        return cls.open(path)

    @writes
    def append(
        self,
        capsule: Capsule,
        now: datetime,
        session_id: str | None = None,
        context: CaptureContext | None = None,
        kind: CandidateKind | str | None = None,
    ) -> tuple[str, bool]:
        """Append capsule or return existing id if deduplicated by source_hash."""
        cid, created = self.capsules.insert(capsule, now, session_id, kind)
        if not created:
            return (cid, False)

        if kind:
            k_obj = CandidateKind.from_str(kind) if isinstance(kind, str) else kind
            self.set_classification(cid, k_obj, ClassificationScope.PROJECT, now)

        anc = capsule.provenance.anchor
        if anc:
            parsed_anc = parse_anchor(anc)
            anc_path = parsed_anc.path
            if anc_path and os.path.exists(anc_path) and os.path.isfile(anc_path):
                try:
                    with open(anc_path, "rb") as f:
                        f_hash = sha256_hex(f.read(2 * 1024 * 1024))
                    self.anchors.set_anchor_hash(cid, f_hash, now)
                except OSError:
                    pass

        ctx = context or CaptureContext.detect()
        self.capsules.set_capture_context(cid, ctx)

        idx_text = (
            f"headline: {headline_of(capsule.content)}\ncontent: {capsule.content}"
        )
        fp = sha256_hex(idx_text.encode("utf-8"))
        # A bad embedding configuration costs the vector, never the capture: the
        # worker's sync queues every live capsule once the configuration is fixed.
        try:
            target_space = SpaceFingerprint.current().as_str()
        except EmbeddingError:
            target_space = None
        if target_space is not None:
            self.embeddings.queue_embeddings([(cid, idx_text, fp)], target_space)

        self.capsules.note_own_write(cid)
        return (cid, True)

    @writes
    def expire_staged_proposals(
        self,
        scope: str | None,
        *,
        subtree: bool,
        now: datetime,
        actor: str,
    ) -> list[str]:
        """Expires staged proposals that no one judged within the horizon.

        The valve for the number that `staged_census` measures. Without it the census is a
        thermometer: the queue grows, `expired` climbs, and nothing drains it -- and the
        ratified plan closes D9 with "every staged proposal EXPIRES unjudged in N
        days, never promoted by default".

        Expiring DOES NOT apply anything and DOES NOT undo anything. The capsule remains fenced
        from every truth surface for the same reason as before (the verdict is not
        `ratified`), and the fence in `retrieve.py` is on PERMISSION precisely
        so that this new verdict does not promote it. What changes is that the curator's
        queue stops spending budget rereading the same proposal from two months ago.

        Reversible without undo: the verdict that counts is the LATEST by `seq`, so the
        owner ratifying an expired proposal returns it to service.

        NEVER touches `REVIEW_REASON_EVIDENCE`: the curator's proof trail is born
        `proposed` on purpose so as not to be served, and was never meant to be
        judged -- expiring it would be deleting evidence to lower a number.
        Tombstones are excluded by the same fence as the census.
        """
        stale = expirable_proposals(self.db, scope, subtree=subtree, now=now)
        for cid in stale:
            self.reviews.append_review_event(
                cid, REVIEW_VERDICT_EXPIRED, REVIEW_REASON_HORIZON, actor, now
            )
        return stale

    @writes
    def reject_unsupported_evidence(
        self,
        scope: str | None,
        *,
        subtree: bool,
        now: datetime,
        actor: str,
    ) -> list[str]:
        """Rejects curator evidence whose `falsifies` edge was revoked.

        The MECHANICAL judge of the queue, and the only one this house admits on
        evidence: it is DERIVED from state, not a threshold. An evidence
        capsule is born to support ONE `falsifies` edge; once the edge is revoked,
        the proposal supports nothing, and stating so is a judgment, not an
        expiration — which is why the verdict is `rejected` and the reason names the
        revocation.

        NOT its sibling `expire_staged_proposals`, and the difference matters: that one
        refuses to touch `REVIEW_REASON_EVIDENCE` because expiring evidence was
        erasing proof to lower a number. Here no number is lowered due to
        age — what is rejected is what the store's own state already disproved, and only
        that. The capsule remains fenced from every truth surface
        (`list_review_fenced` fences everything that is not `ratified`), the event is
        append-only, and the owner ratifying it returns it, because the verdict that
        counts is the LATEST by `seq`.

        The `EXISTS` fence is not decorative: an evidence capsule that NEVER
        had an edge had nothing revoked, and rejecting it would be inventing a judgment.
        Without it, the criterion degenerated into "reject the entire queue".

        MEASURED in the live store on 2026-09-19: 102 `falsifies` edges, 91 revoked
        (18 on Sep 17, 73 on Sep 18) and 4 live with target `moved_anchor`; the 95
        evidence capsules are the origins of 95 of those edges.
        """
        sem_base = unsupported_evidence(self.db, scope, subtree=subtree)
        for cid in sem_base:
            self.reviews.append_review_event(
                cid, REVIEW_VERDICT_REJECTED, REVIEW_REASON_REVOKED, actor, now
            )
        return sem_base

    @writes
    def add_relation(
        self,
        kind: RelationKind | str,
        from_id: str,
        to_id: str,
        origin: str = "manual",
        now: datetime | None = None,
        reason: str | None = None,
    ) -> bool:
        self.classifications.validate_work_relation(kind, from_id, to_id)
        k_str = kind.value if isinstance(kind, RelationKind) else kind
        if k_str in CLOSING_KIND_VALUES:
            verdict = self.reviews.review_verdict(from_id)
            if verdict not in (None, "ratified"):
                raise StoreError(
                    f"relation '{k_str}' from {from_id} is refused while review state is {verdict}"
                )
        at_dt = now or datetime.now(UTC)
        at_str = format_rfc3339(at_dt)

        return self.relations.assert_edge(k_str, from_id, to_id, at_str, origin)

    @writes
    def revoke_relation(
        self,
        kind: RelationKind | str,
        from_id: str,
        to_id: str,
        *,
        actor: str,
        reason: str,
        now: datetime,
    ) -> bool:
        """U1: undoes a `falsifies`/`supersedes` without deleting anything.

        Inert (returns False) if the edge does not exist or is already revoked; never touches
        `relations`. The why lives in the chained journal, not in a column.
        """
        k_str = kind.value if isinstance(kind, RelationKind) else kind
        if k_str not in REVOCABLE_KINDS:
            # Review 2026-09-16: revoking `dod_of`/`part_of` hid them from `list_relations`
            # while `reject_invalidated_work_relations` read them raw. Undo of verdicts, only.
            raise StoreError(
                f"revoke_relation: {k_str!r} is not revocable; only falsifies/supersedes"
            )
        if not actor.strip() or not reason.strip():
            raise StoreError("revoke_relation needs a non-empty actor and reason")
        if not self.relations.revoke(
            k_str, from_id, to_id, format_rfc3339(now), f"revoke:{actor}"
        ):
            return False
        self.audit.append_audit(
            actor,
            "revoke_relation",
            to_id,
            f"{k_str} {from_id}->{to_id}: {reason}",
            now,
        )
        return True

    @writes
    def apply_tier_moves(
        self,
        records: list[ConsolidationRecord],
        moves: list[TierMove],
        recall_mark: int,
        actor: str,
        now: datetime,
    ) -> list[TierMove]:
        """Apply, under the write lock, each planned move the store still plans.

        The plan read a snapshot that taking the lock ends, and another writer
        can land in between: an edge, a tier, a pin, a forget, or a recall,
        whose read traffic moves neither the journal nor the generation. Each
        move is planned again from its capsule as the store holds it now, and
        one the store no longer plans is dropped with nothing journaled.
        `recall_mark` is a `recall_mark` read before the plan read its counts.
        """
        planned = {r.id: r for r in records}
        ids = [m.id for m in moves]
        pinned = self.pins.pinned_among(ids)
        superseded = self.relations.superseded_among(ids)
        archives = self.tiers.supersession_archives(ids)
        recalled = recalls_after(self.db, ids, recall_mark)
        applied: list[TierMove] = []
        for move in moves:
            try:
                live = self.capsules.get(move.id) is not None
            except Tombstoned:
                live = False
            present = replace(
                planned[move.id],
                tier=self.tiers.get_tier(move.id),
                pinned=move.id in pinned,
                is_superseded=move.id in superseded,
                supersession_archive=move.id in archives,
                # Only zero matters (`archive_deadline`), and a capsule planned at
                # zero had no `usage` then: zero exactly when nothing recalled it.
                recall_count=planned[move.id].recall_count + recalled.get(move.id, 0),
            )
            if not live or protective_tier_moves([present], now) != [move]:
                continue
            self.tiers.set_tier(move.id, move.to, now)
            self.audit.append_audit(
                actor,
                audit_actions.CONSOLIDATE,
                move.id,
                f"tier={move.to.value} {move.reason}",
                now,
            )
            applied.append(move)
        return applied

    @writes
    def record_import_block(
        self,
        source_key: str,
        block_hash: str,
        capsule_id: str,
        ordinal: int,
        now: datetime,
    ) -> bool:
        if not source_key.strip():
            raise EmptyField("import block source_key")
        if not block_hash.strip():
            raise EmptyField("import block hash")
        if not self.capsules.get(capsule_id):
            raise UnknownCapsule(capsule_id)
        return self.sources.insert_import_block(
            source_key, block_hash, capsule_id, ordinal, format_rfc3339(now)
        )

    @writes
    def set_classification(
        self,
        capsule_id: str,
        kind: CandidateKind | str,
        scope: ClassificationScope | str,
        now: datetime,
    ) -> None:
        k_str = kind.value if isinstance(kind, CandidateKind) else kind
        s_str = scope.value if isinstance(scope, ClassificationScope) else scope
        at_str = format_rfc3339(now)

        with self.db.connection() as conn, conn.cursor() as cur:
            reject_invalidated_work_relations(cur, capsule_id, k_str)
        self.classifications.upsert(capsule_id, k_str, s_str, at_str)

    @writes
    def append_dod_event(
        self,
        capsule_id: str,
        axis: str,
        value: str,
        actor: str,
        reason: str = "",
        now: datetime | None = None,
    ) -> DodEvent:
        if not actor or not actor.strip():
            raise StoreError("actor is mandatory for nmemory_dod_set")

        sc = self.capsules.get(capsule_id)
        if not sc:
            raise StoreError(f"unknown capsule {capsule_id!r}")

        classification = self.classifications.get_classification(capsule_id)
        if not classification or classification.kind != CandidateKind.DOD:
            kind_str = (
                f'Some("{classification.kind.value}")'
                if classification and classification.kind
                else "None"
            )
            raise StoreError(
                f"capsule {capsule_id} is not a dod capsule (kind {kind_str})"
            )

        axis_str = axis.lower()
        if axis_str not in ("status", "verdict"):
            raise StoreError(f"invalid dod axis: {axis_str}")

        current_state = self.dod.dod_state_of(capsule_id)

        if axis_str == "status":
            legal_transitions = {
                "proposed": ["accepted"],
                "accepted": ["waived"],
                "waived": [],
            }
            curr = current_state.status
            legal = legal_transitions.get(curr, [])
            if value not in legal:
                if not legal:
                    raise StoreError(
                        f'dod {capsule_id} status is {curr!r}, which is TERMINAL — {value!r} is not reachable from it. A DoD that turned out to be wrong is REPLACED, never re-labelled: ingest the corrected dod capsule and record nmemory_relation_add {{kind: "supersedes", from: <new dod>, to: {capsule_id!r}}}'
                    )
                else:
                    raise StoreError(
                        f"dod {capsule_id} status is {curr!r}; {value!r} is not reachable in one move. Legal next: {' | '.join(legal)}"
                    )
            if value == "waived" and not reason.strip():
                raise StoreError(
                    f"waived status on dod {capsule_id} requires a non-empty reason beside the actor"
                )

        elif axis_str == "verdict":
            legal_transitions = {
                "unmet": ["met", "partial", "failed"],
                "met": [],
                "partial": [],
                "failed": [],
            }
            curr = current_state.verdict
            legal = legal_transitions.get(curr, [])
            if value not in legal:
                if not legal:
                    raise StoreError(
                        f'dod {capsule_id} verdict is {curr!r}, which is TERMINAL — {value!r} is not reachable from it. A DoD that turned out to be wrong is REPLACED, never re-labelled: ingest the corrected dod capsule and record nmemory_relation_add {{kind: "supersedes", from: <new dod>, to: {capsule_id!r}}}'
                    )
                else:
                    raise StoreError(
                        f"dod {capsule_id} verdict is {curr!r}; {value!r} is not reachable in one move. Legal next: {' | '.join(legal)}"
                    )
            ep = self.epistemics.get_epistemic(capsule_id)
            if (
                not ep
                or ep.get("evidence_state") != "observed"
                or not ep.get("proof_hint")
            ):
                missing = 'evidence_state="observed" AND a non-empty proof_hint'
                raise StoreError(
                    f'verdict {value!r} on dod {capsule_id} needs the observation that justifies it, and it is missing {missing}. Record what you drove — nmemory_kind_classify {{capsule_id: {capsule_id!r}, content: "<the DoD text>", kind: "dod", evidence_state: "observed", proof_hint: "<the command>"}} — then call nmemory_dod_set again passing reason as the signal you saw.'
                )
            if not reason.strip():
                raise StoreError(
                    f"verdict {value!r} on dod {capsule_id} requires reason as the signal you saw"
                )

        at_dt = now or datetime.now(UTC)
        at_str = format_rfc3339(at_dt)
        self.dod.append_dod_event(capsule_id, axis_str, value, actor, reason, at_str)
        return DodEvent(
            axis=axis_str, value=value, actor=actor, reason=reason, at=at_dt
        )

    @writes
    def append_capture(
        self,
        capsule: Capsule,
        session_id: str | None,
        event_time: EventTimeRange | None,
        context: CaptureContext | None,
        now: datetime,
    ) -> tuple[str, bool]:
        cid, created = self.append(
            capsule, now, session_id=session_id, context=context, kind=None
        )
        if created and event_time is not None:
            self.capsules.set_event_time(cid, event_time, now)
        if not created:
            self.capsules.note_own_write(cid)
        return cid, created

    @writes
    def set_birth_classification(
        self, capsule_id: str, kind: str, scope: str, now: datetime
    ) -> None:
        k_obj = CandidateKind.from_str(kind) if isinstance(kind, str) else kind
        s_obj = ClassificationScope.from_str(scope) if isinstance(scope, str) else scope
        self.set_classification(capsule_id, k_obj, s_obj, now)

        prefix_map = {
            "fact": "fact",
            "procedure": "procedure",
            "decision": "decision",
            "task": "task",
            "epic": "epic",
            "plan": "plan",
            "brainstorm": "brainstorm",
            "doc": "doc",
            "constraint": "constraint",
            "capability": "capability",
            "failure_pattern": "failure-pattern",
            "dod": "dod",
        }
        k_str = k_obj.value if hasattr(k_obj, "value") else str(k_obj)
        typed_prefix = prefix_map.get(k_str)
        if typed_prefix:
            self.capsules.set_handle_prefix(capsule_id, typed_prefix)
