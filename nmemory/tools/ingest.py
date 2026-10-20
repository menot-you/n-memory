"""nmemory_capsule_ingest: capture one item or a batch, behind the kind and proof gates."""

from __future__ import annotations

import logging
import re
from contextlib import nullcontext
from datetime import datetime
from typing import Any

from nott_system1 import System1Unavailable

from .. import audit_actions, system1
from ..anchor import anchor_file, parse_anchor, quote_verdict
from ..anchors import parse_code_anchor
from ..capsule import (
    RULE_KINDS,
    AuthorityClass,
    is_owner_rule,
    parse_rfc3339,
    sha256_hex,
)
from ..capture import CaptureContext
from ..classify import (
    CandidateKind,
)
from ..ingest import (
    IngestError,
    IngestOutcome,
    IngestRequest,
    ingest_capture,
    system1_taint_surfaces,
)
from ..language import foreign_language, glued_words
from ..project import canonical_project
from ..queries.staged import REVIEW_REASON_PROPOSAL
from ..repositories.base import StoreError
from ..repositories.capsules import EventTimeRange
from .common import (
    HandlerState,
    taint_summary,
    verb_result,
)
from .validation import validate_ingest_item

logger = logging.getLogger(__name__)

TAINT_THRESHOLD = 0.5


#: The kinds that ASSERT fact. `task`, `epic`, `plan`, `brainstorm`, `doc`,
#: `capability`, and `dod` describe intent or work, not a world state
#: that someone can re-check later — the port does not ask them for proof.
FACTUAL_KINDS = frozenset(
    {"fact", "procedure", "decision", "constraint", "failure_pattern"}
)


#: `#683`: the PR or issue is re-obtainable — text, diff, and discussion remain.
_ISSUE_REF = re.compile(r"#\d+")


_ANCHOR_URL = re.compile(r"https?://\S")


_VERBATIM_MIN = 60


#: Measured in the live store digest on 2026-09-19: 147 capsules written in 7 days
#: remained `unclassified` --- more than any real kind except `fact` and
#: `failure_pattern`. Without a kind, the capsule does not pass the proof port, does
#: not enter any census, and has no lifecycle: it is a write that no one ever
#: re-judges. The refusal NAMES the closed vocabulary, otherwise the agent guesses.
MISSING_KIND_HINT = (
    "ingest rejected: `kind` is mandatory — a capture that does not say WHAT it "
    "is cannot be judged, counted or expired. Pick one of: "
    + ", ".join(f"`{k.value}`" for k in CandidateKind)
)


REOBTAINABLE_HINT = (
    " — a factual capture must carry proof someone can re-obtain: an anchor "
    "with a line (`path/to/file.py:120`), a URL, a `#<pr-or-issue>` reference, "
    "a verbatim quote as the anchor, or a `source_quote` / `proof_hint` field"
)


ENGLISH_HINT = (
    " — nMEMORY content is written in English: restate it in English, and put "
    "the owner's original words verbatim in `source_quote` (they are not judged)"
)


def spacing_detail(glued: list[str], future: str) -> str:
    """Which words look glued, and how to write the `future` ones instead.

    Advisory: the text was stored as sent, because the client lost the spaces
    before the call and a refusal would lose a valid memory with them.
    """
    return (
        f"its prose looks glued ({', '.join(repr(w[:40]) for w in glued[:3])}): "
        f"write future {future} with normal spacing, a space between words and "
        "before numbers and references (`PR #1160`, not `PR1160`); code in "
        "backticks is not judged"
    )


def spacing_warning(text: str) -> str | None:
    """The warning a capture's row carries when its prose looks glued, else None."""
    try:
        glued = glued_words(text)
    except Exception as exc:  # noqa: BLE001 — advisory: never costs the capture
        return f"stored; its spacing was not checked ({exc!r})"
    return None if glued is None else f"stored, but {spacing_detail(glued, 'captures')}"


def reobtainable_proof(
    anchor: str, quote: tuple[str, str] | None, epistemic: dict[str, Any] | None
) -> bool:
    """Does the anchor allow someone to re-check this later, without this session?

    `parse_code_anchor` (and not `anchor.parse_anchor`) is the judge for the code case:
    it is EXACTLY the same parser that `anchors._live_code_anchored` uses to
    choose what the audit timer can judge. An anchor that passed
    here by another rule would enter and never be verifiable — the port would promise
    re-obtainable and the verifier would not reach it. `anchor.parse_anchor` is
    broader (source prefixes, `@sha`) and returns the path without the line, so it does
    not answer the question the port asks.
    """
    if quote is not None:
        return True
    if epistemic is not None and str(epistemic.get("proof_hint") or "").strip():
        return True
    a = anchor.strip()
    if len(a) > _VERBATIM_MIN:
        return True
    if _ANCHOR_URL.search(a) or _ISSUE_REF.search(a):
        return True
    parsed = parse_code_anchor(a)
    return parsed is not None and parsed[1] is not None


def uniform_rejection(detail: str) -> str:
    stripped = detail
    if stripped.startswith("ingest rejected: "):
        stripped = stripped[len("ingest rejected: ") :]
    elif stripped.startswith("capsule rejected: "):
        stripped = stripped[len("capsule rejected: ") :]
    return f"ingest rejected: {stripped}"


def rejection_row(index: int | None, field: str | None, detail: str) -> str:
    prefixed = uniform_rejection(detail)
    if index is not None and field is not None:
        return f"items[{index}].{field}: {prefixed}"
    elif index is not None and field is None:
        return f"items[{index}]: {prefixed}"
    elif index is None and field is not None:
        return f"{field}: {prefixed}"
    else:
        return prefixed


def _persist_ingest_kinds(
    state: HandlerState,
    actor: str,
    outcomes: list[dict[str, Any]],
    kinds: list[str | None],
    now: datetime,
) -> None:
    for outcome, kind in zip(outcomes, kinds):
        if kind is None or outcome.get("status") not in (
            "captured",
            "deduplicated",
        ):
            continue
        cid = outcome["id"]
        prior_cls = state.store.classifications.get_classification(cid)
        prior_kind = prior_cls.kind.value if prior_cls else None
        scope = "project"
        freshly_captured = outcome["status"] == "captured"
        if freshly_captured:
            state.store.set_birth_classification(cid, kind, scope, now)
        else:
            state.store.set_classification(cid, kind, scope, now)
        state.store.audit.append_audit(
            actor,
            audit_actions.CLASSIFY,
            cid,
            f"kind={kind} scope={scope} via-ingest",
            now,
        )
        if kind in ("epic", "plan", "task"):
            outcome["unanchored"] = True
        if (
            outcome["status"] == "deduplicated"
            and prior_kind is not None
            and prior_kind != kind
        ):
            outcome["reclassified"] = {"was": prior_kind, "now": kind}


def _persist_ingest_epistemics(
    state: HandlerState,
    actor: str,
    outcomes: list[dict[str, Any]],
    epistemics: list[dict[str, Any] | None],
    now: datetime,
) -> None:
    for outcome, ep in zip(outcomes, epistemics):
        if ep is None or outcome.get("status") not in ("captured", "deduplicated"):
            continue
        cid = outcome["id"]
        ev_state = ep.get("evidence_state")
        proof_hint = ep.get("proof_hint")
        stale_if = ep.get("stale_if")
        state.store.epistemics.set_epistemic(
            cid,
            evidence_state=ev_state,
            proof_hint=proof_hint,
            stale_if=stale_if,
            now=now,
        )
        note_parts = []
        if ev_state:
            note_parts.append(f"evidence_state={ev_state}")
        if proof_hint:
            note_parts.append(f"proof_hint={proof_hint}")
        if stale_if:
            note_parts.append(f"stale_if={stale_if}")
        state.store.audit.append_audit(
            actor,
            audit_actions.CLASSIFY,
            cid,
            f"{' '.join(note_parts)} via-ingest",
            now,
        )


def _persist_ingest_source_quotes(
    state: HandlerState,
    actor: str,
    outcomes: list[dict[str, Any]],
    quotes: list[tuple[str, str] | None],
    now: datetime,
) -> dict[str, str]:
    """Store each captured source quote with its verdict; return the verdicts by id."""
    verdicts: dict[str, str] = {}
    for outcome, pair in zip(outcomes, quotes):
        if pair is None or outcome.get("status") != "captured":
            continue
        cid = outcome["id"]
        anchor, quote = pair
        verdict = quote_verdict(anchor, state.anchor_root, quote, errors="ignore")
        state.store.epistemics.set_source_quote(cid, quote, verdict, now)
        state.store.audit.append_audit(
            actor,
            audit_actions.CLASSIFY,
            cid,
            f"source_quote {verdict} via-ingest",
            now,
        )
        verdicts[cid] = verdict
    return verdicts


def _pin_owner_rules(
    state: HandlerState,
    actor: str,
    outcomes: list[dict[str, Any]],
    kinds: list[str | None],
    verdicts: dict[str, str],
    now: datetime,
) -> None:
    """Pin a new owner rule in the ingest transaction.

    Only a capture whose source quote is verified counts: a declared
    authority_class alone is not proof that the owner said it. A rule that is not
    pinned for that reason says so in its row, so the agent can pin it by hand.
    """
    for outcome, kind in zip(outcomes, kinds):
        if outcome.get("status") != "captured" or kind not in RULE_KINDS:
            continue
        cid = str(outcome["id"])
        stored = state.store.capsules.get(cid)
        if stored is None or not is_owner_rule(stored.capsule):
            continue
        verdict = verdicts.get(cid)
        if verdict != "verified":
            outcome["pin"] = (
                "skipped: no source quote"
                if verdict is None
                else f"skipped: quote {verdict}"
            )
            continue
        state.store.pins.set_pin(cid, True, "owner rule", actor, now)
        state.store.audit.append_audit(
            actor, audit_actions.PIN, cid, "owner rule via-ingest", now
        )
        outcome["pinned"] = True


def _persist_ingest_review(
    state: HandlerState,
    actor: str,
    outcomes: list[dict[str, Any]],
    staged: list[bool],
    now: datetime,
) -> None:
    for outcome, want_staged in zip(outcomes, staged):
        st = outcome.get("status")
        if st == "captured":
            if want_staged:
                cid = outcome["id"]
                state.store.reviews.append_review_event(
                    cid, "proposed", REVIEW_REASON_PROPOSAL, actor, now
                )
                state.store.audit.append_audit(
                    actor, audit_actions.INGEST, cid, "staged proposed", now
                )
                outcome["review"] = {"state": "proposed"}
        elif st == "deduplicated":
            cid = outcome["id"]
            verdict = state.store.reviews.review_verdict(cid)
            if verdict is not None:
                outcome["review"] = {"state": verdict}
            elif want_staged:
                outcome["review"] = {"state": "already-live"}


def item_sidecars(
    raw_items: list[dict[str, Any]],
) -> tuple[
    list[str | None],
    list[dict[str, Any] | None],
    list[tuple[str, str] | None],
    list[bool],
]:
    """Per item: its kind, its epistemics, its (anchor, source_quote) pair and `staged`."""
    kinds: list[str | None] = [item.get("kind") for item in raw_items]
    epistemics: list[dict[str, Any] | None] = [
        {
            "evidence_state": item.get("evidence_state"),
            "proof_hint": item.get("proof_hint"),
            "stale_if": item.get("stale_if"),
        }
        if any(
            item.get(k) is not None
            for k in ("evidence_state", "proof_hint", "stale_if")
        )
        else None
        for item in raw_items
    ]
    quotes: list[tuple[str, str] | None] = [
        (item["anchor"], item["source_quote"])
        if item.get("source_quote") and item.get("anchor")
        else None
        for item in raw_items
    ]
    staged: list[bool] = [bool(item.get("staged", False)) for item in raw_items]
    return kinds, epistemics, quotes, staged


def _rejected(
    locator: int | None, bad_field: str | None, detail: str
) -> dict[str, Any]:
    return {"status": "rejected", "error": rejection_row(locator, bad_field, detail)}


def gate_refusal(
    item: dict[str, Any],
    locator: int | None,
    kind: str | None,
    quote: tuple[str, str] | None,
    epistemic: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """The rejected row of an item the capture gates refuse, or None.

    Shape (batch items only, q1/q24), then kind, then proof, then language.
    The kind gate comes before the proof gate for convenience, NOT necessity:
    `None in FACTUAL_KINDS` is false, so the cheapest item to reject exits
    first. The proof gate — whoever asserts fact brings re-obtainable proof —
    runs before `supersedes` so no query is wasted on a rejected item, and
    judges the singular item by the same yardstick. The language gate: capsule content is English; the owner's own words
    go verbatim in `source_quote`, which is not judged. NOT in `ingest()`:
    `nmemory_capsule_import` carries external files.
    """
    if locator is not None:
        shape_err = validate_ingest_item(item)
        if shape_err is not None:
            return _rejected(locator, *shape_err)
    if kind is None:
        return _rejected(locator, "kind", MISSING_KIND_HINT)
    if kind in FACTUAL_KINDS and not reobtainable_proof(
        str(item.get("anchor") or ""), quote, epistemic
    ):
        return _rejected(
            locator,
            "anchor",
            f"anchor {item.get('anchor')!r} is not re-obtainable{REOBTAINABLE_HINT}",
        )
    foreign = foreign_language(str(item.get("content") or ""))
    if foreign is not None:
        return _rejected(
            locator,
            "content",
            f"content prose reads as {foreign!r}, not English{ENGLISH_HINT}",
        )
    if item.get("staged") and item.get("supersedes"):
        return _rejected(
            locator,
            "supersedes",
            "The requested review state is proposed. "
            "Approve a staged capsule before adding a closing relation.",
        )
    return None


def parse_item_times(
    item: dict[str, Any], locator: int | None
) -> tuple[EventTimeRange | None, datetime | None, datetime | None] | dict[str, Any]:
    """`(event_time, valid_from, valid_to)` of one item, or its rejected row."""
    event_time = None
    if item.get("event_at"):
        try:
            event_time = EventTimeRange.point(parse_rfc3339(item["event_at"]))
        except (ValueError, TypeError) as exc:
            return _rejected(locator, "event_at", str(exc))
    elif item.get("event_from") and item.get("event_to"):
        try:
            dt_from = parse_rfc3339(item["event_from"])
            dt_to = parse_rfc3339(item["event_to"])
            event_time = EventTimeRange.new(dt_from, dt_to)
        except (ValueError, TypeError) as exc:
            return _rejected(locator, None, str(exc))
    valid_from = None
    valid_to = None
    if item.get("valid_from"):
        try:
            valid_from = parse_rfc3339(item["valid_from"])
        except (ValueError, TypeError) as exc:
            return _rejected(locator, "valid_from", str(exc))
    if item.get("valid_to"):
        try:
            valid_to = parse_rfc3339(item["valid_to"])
        except (ValueError, TypeError) as exc:
            return _rejected(locator, "valid_to", str(exc))
    return event_time, valid_from, valid_to


def ingest_request(
    item: dict[str, Any],
    times: tuple[EventTimeRange | None, datetime | None, datetime | None],
    supersedes_id: str | None,
    authority_class: AuthorityClass | None,
) -> IngestRequest:
    """The capture request of one item; the constructor's typed refusal is IngestError."""
    event_time, valid_from, valid_to = times
    return IngestRequest(
        content=item.get("content", ""),
        source=item.get("source", ""),
        anchor=item.get("anchor", ""),
        confidence=float(item["confidence"])
        if "confidence" in item and item["confidence"] is not None
        else None,
        valid_from=valid_from,
        valid_to=valid_to,
        project_id=item.get("project_id"),
        authority_class=authority_class,
        instruction_taint=bool(item["instruction_taint"])
        if "instruction_taint" in item and item["instruction_taint"] is not None
        else None,
        supersedes=supersedes_id,
        session_id=item.get("session_id"),
        event_time=event_time,
        source_quote=item.get("source_quote"),
        derived_from=tuple(item["derived_from"])
        if item.get("derived_from") is not None
        else None,
        hints=item.get("hints") is not False,
    )


_DEAD_ANCHOR_NOTE = (
    "this anchor names a path that does not exist under the anchor root: "
    "recall will report it as a dead link — check the path, or use an anchor "
    "form that does not claim to be one"
)


def anchor_note(anchor_root: str, path: str | None) -> str | None:
    """The dead-link note of an anchor path that names no file under the root."""
    if not path:
        return None
    try:
        return None if anchor_file(anchor_root, path) is not None else _DEAD_ANCHOR_NOTE
    except OSError:
        return _DEAD_ANCHOR_NOTE


def _record_anchor_hash(
    state: HandlerState, capsule_id: str, path: str | None, now: datetime
) -> None:
    """Record the hash of a freshly captured capsule's anchor file, when it names one."""
    if not path:
        return
    try:
        target = anchor_file(state.anchor_root, path)
        if target is not None:
            with open(target, "rb") as f:
                b_hash = sha256_hex(
                    f.read(2 * 1024 * 1024).decode("utf-8", errors="ignore")
                )
            state.store.anchors.set_anchor_hash(capsule_id, b_hash, now)
    except (OSError, UnicodeDecodeError) as exc:
        logger.debug("Failed to hash anchor content: %s", exc)


def outcome_row(
    outcome: IngestOutcome, t_summary: list[str], note: str | None
) -> tuple[dict[str, Any], str]:
    """The wire row of one capture outcome and its audit reason.

    Dedup against a CLEAN incumbent with hostile derivation: the incumbent's
    bytes do not change (it was cleansed by its own bytes); the row and journal
    are what observe it. Fail-visible, never silence — the fence lives in
    ingest.py (TAINT INHERITANCE FROM ORIGIN BYTES).
    """
    reason = "captured"
    if outcome.deduped:
        row: dict[str, Any] = {
            "status": "deduplicated",
            "id": outcome.id,
            "dedup_hint": None,
        }
        reason = "deduplicated"
    else:
        row = {
            "status": "captured",
            "id": outcome.id,
            "dedup_hint": (
                {
                    "similar_id": outcome.dedup_hint.similar_id,
                    "score": outcome.dedup_hint.score,
                }
                if outcome.dedup_hint
                else None
            ),
        }
        if outcome.siblings:
            row["siblings"] = [{"id": s.id, "score": s.score} for s in outcome.siblings]
    if outcome.superseded:
        row["superseded"] = outcome.superseded
    if outcome.project_canon is not None:
        was, became = outcome.project_canon
        row["project_id"] = {"was": was, "now": became}
    if outcome.deduped and outcome.derivation_findings:
        row["derivation_tainted"] = True
        t_summary = t_summary + taint_summary(outcome.derivation_findings)
        reason = "deduplicated; derivation tainted"
    if t_summary:
        row["taint_findings"] = t_summary
    if note:
        row["anchor_note"] = note
    return row, reason


def referenced_ids(outcomes: list[dict[str, Any]]) -> set[str]:
    """Every capsule id an outcome names: its own, its dedup hint, siblings, superseded."""
    all_ids: set[str] = set()
    for o in outcomes:
        if o.get("id"):
            all_ids.add(o["id"])
        if (
            o.get("dedup_hint")
            and isinstance(o["dedup_hint"], dict)
            and o["dedup_hint"].get("similar_id")
        ):
            all_ids.add(o["dedup_hint"]["similar_id"])
        if o.get("siblings") and isinstance(o["siblings"], list):
            for s in o["siblings"]:
                if isinstance(s, dict) and s.get("id"):
                    all_ids.add(s["id"])
        if o.get("superseded"):
            all_ids.add(o["superseded"])
    return all_ids


def _store_failure(idx: int, exc: StoreError) -> dict[str, Any]:
    """The JSON-RPC error of a store failure on one item.

    The boundary is ONE per call (`server.call_tool`): this dict has `error` at
    the top level, so `_ToolRejected` and `tool_boundary`'s `conn.rollback()`
    undo the WHOLE BATCH. Retrying the batch is safe: capture is idempotent by
    content hash.
    """
    return {
        "jsonrpc": "2.0",
        "error": {
            "code": -32603,
            "message": (
                f"nmemory_capsule_ingest failed on item {idx}: {exc} "
                "— the whole batch was rolled back and nothing was "
                "stored; retry the batch (capture is idempotent by "
                "content hash)"
            ),
        },
    }


def system1_taint_hits(items: list[tuple[int, dict[str, Any]]]) -> set[int]:
    """The indexes of the items System1 measures as instructions (p >= 0.5).

    The fence beside the regex. It runs before the item loop, so no
    network call holds the store's write lock: the first capture of a batch takes
    that lock until the call ends. One probe for the whole batch, each surface its
    own request. A surface System1 failed to measure keeps the regex verdict.
    """
    owners: list[int] = []
    surfaces: list[str] = []
    for idx, item in items:
        content = item.get("content")
        quote = item.get("source_quote")
        turns = item.get("derived_from")
        if (
            not isinstance(content, str)
            or not (quote is None or isinstance(quote, str))
            or not (turns is None or all(isinstance(t, str) for t in turns))
        ):
            continue  # the loop rejects this item; there is nothing to measure
        found = system1_taint_surfaces(
            content,
            quote,
            turns,
            AuthorityClass.from_str(item["authority_class"])
            if item.get("authority_class")
            else None,
            item.get("instruction_taint"),
        )
        owners.extend(idx for _ in found)
        surfaces.extend(found)
    if not surfaces:
        return set()
    try:
        probed = system1.taint_probe(surfaces)
    except System1Unavailable as exc:
        probed = [], exc
    if probed is None:
        return set()
    probabilities, failure = probed
    if failure is not None:
        # The cause only: the surfaces are capsule bytes and never enter a log.
        logger.warning(
            "System1 taint probe failed; the regex verdict stands for every surface "
            "it did not measure: %s",
            failure,
        )
    return {
        owners[i]
        for i, p in enumerate(probabilities)
        if p is not None and p >= TAINT_THRESHOLD
    }


def handle(
    state: HandlerState,
    args: dict[str, Any],
    context: CaptureContext | None,
    now: datetime,
    actor: str,
) -> dict[str, Any]:
    indexed = "items" in args
    raw_items: list[dict[str, Any]] = args.get("items", [args])
    defaults = state.effective_defaults(context)
    kinds, epistemics, quotes, staged = item_sidecars(raw_items)
    refusals = [
        gate_refusal(
            item, idx if indexed else None, kinds[idx], quotes[idx], epistemics[idx]
        )
        for idx, item in enumerate(raw_items)
    ]
    tainted = system1_taint_hits(
        [(idx, item) for idx, item in enumerate(raw_items) if refusals[idx] is None]
    )

    outcomes: list[dict[str, Any]] = []
    for idx, item in enumerate(raw_items):
        locator = idx if indexed else None
        refusal = refusals[idx]
        if refusal is not None:
            outcomes.append(refusal)
            continue

        supersedes_id = None
        if item.get("supersedes"):
            ref = item["supersedes"]
            ref_res = state.store.capsules.resolve_capsule_ref(ref)
            if ref_res.status != "resolved":
                outcomes.append(
                    _rejected(
                        locator,
                        "supersedes",
                        ref_res.detail
                        if ref_res.detail
                        else f"supersedes target {ref} is not a stored capsule; nothing was captured",
                    )
                )
                continue
            supersedes_id = ref_res.id

        times = parse_item_times(item, locator)
        if isinstance(times, dict):
            outcomes.append(times)
            continue

        auth_class = (
            AuthorityClass.from_str(item["authority_class"])
            if item.get("authority_class")
            else None
        )
        # The request is built INSIDE the try: the constructor validates, and
        # its typed refusal rejects the ITEM, never crashes the call.
        try:
            req = ingest_request(item, times, supersedes_id, auth_class)
            if state.capture_only:
                req.hints = False
            if idx in tainted:
                req.instruction_taint = True
            with (
                state.store.db.write_boundary()
                if "expected_revision" in item
                else nullcontext()
            ):
                if "expected_revision" in item:
                    current = state.store.capsules.latest_revision(
                        canonical_project(
                            req.project_id
                            if req.project_id is not None
                            else defaults.project_id
                        ),
                        req.source,
                        req.anchor,
                    )
                    if current != item["expected_revision"]:
                        outcomes.append(
                            _rejected(locator, "expected_revision", "revision is stale")
                        )
                        continue
                    existing = state.store.capsules.find_by_source_hash(
                        sha256_hex(req.content)
                    )
                    if existing is not None and existing.id != current:
                        outcomes.append(
                            _rejected(
                                locator,
                                "expected_revision",
                                "content belongs to another revision",
                            )
                        )
                        continue
                outcome = ingest_capture(
                    state.store, req, defaults, context=context, now=now
                )
        except IngestError as exc:
            outcomes.append(_rejected(locator, None, str(exc)))
            continue
        except StoreError as exc:
            return _store_failure(idx, exc)

        if outcome.deduped_at_insert:
            kinds[idx] = None
            epistemics[idx] = None
        parsed_anchor = parse_anchor(req.anchor) if req.anchor else None
        path = parsed_anchor.path if parsed_anchor else None
        note = anchor_note(state.anchor_root, path)
        if not outcome.deduped:
            _record_anchor_hash(state, outcome.id, path, now)
        wire_row, audit_reason = outcome_row(
            outcome, taint_summary(outcome.taint_findings), note
        )
        if outcome.deduped and idx in tainted:
            # As a hostile derivation: the clean incumbent keeps its bytes, the
            # row and the journal observe the hit.
            wire_row["system1_tainted"] = True
            audit_reason += "; System1 tainted"
        if (warning := spacing_warning(req.content)) is not None:
            wire_row["warning"] = warning
        state.store.audit.append_audit(
            actor, audit_actions.INGEST, outcome.id, audit_reason, now
        )
        _persist_ingest_review(state, actor, [wire_row], [staged[idx]], now)
        outcomes.append(wire_row)

    _persist_ingest_kinds(state, actor, outcomes, kinds, now)
    _persist_ingest_epistemics(state, actor, outcomes, epistemics, now)
    verdicts = _persist_ingest_source_quotes(state, actor, outcomes, quotes, now)
    _pin_owner_rules(state, actor, outcomes, kinds, verdicts, now)

    handles: dict[str, str] = {}
    if not state.capture_only:
        for cid in sorted(referenced_ids(outcomes)):
            h = state.store.capsules.get_handle(cid)
            if h:
                handles[cid] = h

    statuses = [o["status"] for o in outcomes]
    res: dict[str, Any] = {
        "outcomes": outcomes,
        "captured": statuses.count("captured"),
        "deduped": statuses.count("deduplicated"),
        "rejected": statuses.count("rejected"),
    }
    if state.capture_only:
        res["outcomes"] = [
            {"status": row["status"], "id": row["id"]}
            if row["status"] in {"captured", "deduplicated"}
            else {"status": "rejected", "error": "Capture refused."}
            for row in outcomes
        ]
    if handles:
        res["handles"] = handles
    return verb_result(res)
