"""Standing: whether a served capsule still holds, derived on read from four owners."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..capsule import format_rfc3339
from ..database import Database
from ..relation import RelationKind
from ..repositories.anchors import VERIFICATION_PASSING, AnchorRepository
from ..repositories.capsules import CapsuleRepository
from ..repositories.classifications import ClassificationRepository
from ..repositories.relations import RelationRepository

# `baseline` is intentionally NEUTRAL: it is the first time the summary for that
# line was recorded, so there is no past to compare against. Kept out of PASSING
# to avoid falsely claiming `vale`, and neutral to avoid falling into the
# `caducou` branch — without this, the first sweep marked 1571 capsules as expired
# all at once.
# `skipped_missing`: the curator could not READ the proof (missing or unreadable
# file, or anchor that is not a code path). Intentionally neutral — "cannot
# verify" is never "this is false", and that very confusion produced the 95
# `falsifies` edges in 2026-09. Kept out of PASSING to avoid falsely claiming `vale`.
# The four `digest_*` values are clustering BOOKKEEPING, not verdicts on the
# capsule's truth: `digest_scope` records them on the FIRST member of the cluster.
# Outside both sets, `standing_of` used to publish `caducou reason=digested` because
# the capsule was DIGESTED, which is a good outcome. Measured on the live store on
# 2026-09-18: `curator/digested` was the latest event for a live capsule.
# `skipped_missing` REMAINS neutral, and does not contradict the `unverifiable`
# branch in `standing_of`: it never rests on a `pass`. The curator queue only
# admits capsules whose latest event is `dead_anchor`, `moved_anchor`, or
# `changed_anchor`, so a `skipped_missing` always rests on a mechanical verdict
# that already says what needs to be said. Do not "correct" it for symmetry.
VERIFICATION_NEUTRAL = frozenset(
    {
        "skipped_taint",
        "error",
        "baseline",
        "skipped_missing",
        # `unverifiable` is NOT neutral, and #674 was mistaken to place it here.
        # Neutral means "does not alter standing", and `standing_of` traverses
        # through it to the latest NON-neutral — meaning an old `dead_anchor`,
        # written under the WRONG rule, survived rule corrections indefinitely.
        # `unverifiable` is STRUCTURAL ("this anchor cannot be evaluated here"),
        # not operational like `skipped_taint`/`error`, and thus has its own
        # branch in `standing_of`: returning `por_verificar`, which is what is
        # known — nothing.
        "digested",
        "digest_declined",
        "digest_refused",
        "digest_deduped",
    }
)
# U3: kinds whose lifecycle is self-contained (status/verdict; outcome/supersede)
# and therefore do not carry standing; and kinds that hold by existence until an
# edge contradicts them — an owner lock is not "por_verificar".
STANDING_EXEMPT_KINDS: frozenset[str] = frozenset({"dod", "task"})
STANDING_BY_EXISTENCE: frozenset[str] = frozenset(
    {"decision", "constraint", "epic", "plan", "brainstorm"}
)


def standing_of(
    db: Database, ids: list[str], *, now: datetime
) -> dict[str, dict[str, Any]]:
    """U3: the standing of each served capsule, DERIVED on read, never stored.

    Precedent `dod_contradictions`: a `standing` column would be a cache
    that becomes wrong on the next edge. Kind-aware — `dod`/`task`
    have their own lifecycle and stay OUT of the dictionary; `decision`,
    `constraint`, `epic`, `plan`, `brainstorm` are valid by existence until an
    edge contradicts them; claims about the world take the four
    states. `caducou` comes from `valid_to` or the latest verification event
    (U2); `vale` requires a `pass`; `por_verificar` is default and reports age.

    ponytail: one database trip per capsule (get + relations + verifications);
    surfaces serve ≤ 50 rows, so ~150 queries in the worst case.
    Batching into a single query will only be born when the cost appears in digest.
    """
    capsules = CapsuleRepository(db)
    classifications = ClassificationRepository(db)
    relations = RelationRepository(db)
    anchors = AnchorRepository(db)
    out: dict[str, dict[str, Any]] = {}
    for cid in ids:
        sc = capsules.get(cid)
        if sc is None:
            continue
        cls = classifications.get_classification(cid)
        kind = cls.kind.value if cls is not None else None
        if kind in STANDING_EXEMPT_KINDS:
            continue
        inbound = [
            r
            for r in relations.list_relations_for(cid)
            if r.to_id == cid
            and r.kind in (RelationKind.FALSIFIES, RelationKind.SUPERSEDES)
        ]
        if inbound:
            last = inbound[-1]
            out[cid] = {
                "state": "contradita",
                "by": last.from_id,
                "edge": last.kind.value,
            }
            continue
        valid_to = sc.capsule.freshness.valid_to
        if valid_to is not None and valid_to <= now:
            out[cid] = {"state": "caducou", "reason": "valid_to"}
            continue
        if kind in STANDING_BY_EXISTENCE:
            out[cid] = {"state": "vale", "reason": "by_existence"}
            continue
        # The `result` set is CLOSED here (review 2026-09-16): curator
        # writes `holds`/`reanchored` (vale), `falsified`, and the neutrals
        # `skipped_taint`/`error`, which do not change standing — counts the latest
        # NON-neutral event.
        #
        # The set WAS NOT closed as the old comment claimed: the four
        # `digest_*` were missing, now in `VERIFICATION_NEUTRAL`.
        #
        # `falsified` LEAVES here, and the reason is that the previous assumption ("the
        # edge already did `contradita` above") ceased to hold when
        # revocation entered. Reaching this line PROVES there is no live `falsifies`
        # edge pointing to this capsule: the `if inbound` block
        # above would have exited. And a `curator/falsified` asserts nothing by itself
        # — it asserts "this evidence capsule refutes that one", and without the live
        # edge the claim cannot stand.
        #
        # MEASURED in the live store on 2026-09-18: I revoked 73 fabricated edges
        # over `<file missing>` and the 49 that had standing transitioned from
        # `contradita` to `caducou reason=falsified` — the fence changed
        # doors instead of dropping. A neutral event on top is inert, because
        # this list searches for the latest NON-neutral.
        checks = [
            c
            for c in anchors.list_verifications(cid)
            if c.result not in VERIFICATION_NEUTRAL and c.result != "falsified"
        ]
        if checks:
            # ONE AXIS PER `kind`, and not a single list. Independent review of
            # #684 (2026-09-19) caught the defect this line fixes, and I
            # confirmed both sites: `audit_all` (`anchors.py:495`) writes
            # an `anchor` event PER CAPSULE on each sweep, and
            # `drift_verdict` (`anchors.py:279`) returns `pass` whenever the
            # anchored line says the same. With a single list, the `pass` at
            # 04:00 erased next morning's `stale_condition_met` from
            # 04:30, and standing came to depend on which timer ran
            # last. The sweep of 2026-09-16 counted 834 live capsules
            # anchored in code, so intersection with the 569 fired
            # conditions is not small.
            #
            # And the two events do not contradict each other: an anchor `pass` says
            # that the LINE has not changed; it does not say the PR was not merged or that
            # the file named by the condition still exists. They are distinct
            # axes serialized in a single list, and whichever arrived last
            # erased the other without refuting it.
            #
            # Precedence, and it is this house's D18 order: a failure on
            # any axis wins (1), then a pass (0), and only at the end the
            # ABSENCE of judgment (`unverifiable`, 2) — an axis that asserts something
            # is worth more than an axis that could not read the proof. With a
            # single `kind` present this is exactly the previous
            # behavior, which is why the change does not touch single-axis
            # capsules.
            # `curator` IS NOT its own axis: it is the REVIEWER of the anchor
            # axis. A `holds` from curator says "the anchor died and the capsule
            # remains true" — it is a judgment ON that finding, not a
            # parallel observation, and therefore replaces it rather than competing
            # with it. Without this grouping, a capsule judged `holds`
            # on a `dead_anchor` reverted to `caducou`, which is the opposite
            # of what curator exists to do (caught by the suite:
            # `test_holds_records_only_an_event`).
            #
            # `staleness` is what is orthogonal: nothing in what curator says about
            # the ANCHOR refutes the `stale_if` condition that the capsule itself
            # declared, and vice versa.
            eixo_de = {"curator": "anchor"}
            por_eixo: dict[str, Any] = {}
            for c in checks:
                por_eixo[eixo_de.get(c.kind, c.kind)] = c
            eixos = sorted(por_eixo.values(), key=lambda c: c.at)
            falhados = [
                c
                for c in eixos
                if c.result not in VERIFICATION_PASSING and c.result != "unverifiable"
            ]
            passados = [c for c in eixos if c.result in VERIFICATION_PASSING]
            last_check = (
                falhados[-1] if falhados else (passados[-1] if passados else eixos[-1])
            )
            if last_check.result == "unverifiable":
                # Neither `caducou` ("was true and stopped being true") nor `vale`:
                # it is absence of judgment. MEASURED in the live store on 2026-09-18: of
                # 49 capsules still fenced after revoking curator's fence,
                # the new rule gives `unverifiable` to 30 — session
                # artifacts this tree NEVER tracked — and `dead_anchor` to
                # 19, which the tree indeed lost.
                # `age_days` counts from the LATEST VERIFICATION, not from
                # creation: a capsule created 300 days ago and swept yesterday
                # reported 300. The "never verified" branch below counts
                # from `valid_from` because there is no verification there.
                out[cid] = {
                    "state": "por_verificar",
                    "reason": "unverifiable",
                    "detail": last_check.detail,
                    "verified_at": format_rfc3339(last_check.at),
                    "age_days": max(0, (now - last_check.at).days),
                }
                continue
            if last_check.result in VERIFICATION_PASSING:
                out[cid] = {
                    "state": "vale",
                    "by": last_check.actor,
                    "verified_at": format_rfc3339(last_check.at),
                }
            else:
                out[cid] = {
                    "state": "caducou",
                    "reason": last_check.result,
                    "detail": last_check.detail,
                    "verified_at": format_rfc3339(last_check.at),
                }
            continue
        age = max(0, (now - sc.capsule.freshness.valid_from).days)
        out[cid] = {"state": "por_verificar", "age_days": age}
    return out
