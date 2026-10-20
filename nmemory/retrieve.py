"""Multi-lane retrieval engine (lanes, fusion, ranking, decay, exclusions, receipts)."""

from __future__ import annotations

import logging
import math
import threading
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import nott_database
from nott_database import DatabaseError

from . import NMemoryError
from .anchor import probe_anchor_drift, probe_anchor_liveness, quote_verdict
from .capsule import format_rfc3339, headline_of, parse_rfc3339
from .capsule_ref import fold_diacritic
from .classify import CandidateKind
from .consolidate import Tier
from .embedding import EmbeddingClient
from .lexical import lexical_tokens, lexical_words
from .queries.census import last_read_among
from .queries.standing import standing_of
from .repositories.capsules import EventTimeRecord, StoredCapsule, Tombstoned
from .repositories.embeddings import SupersededSpace
from .space import EmbeddingError, EmbeddingQuotaExhausted
from .store import Store

logger = logging.getLogger(__name__)

#: Seconds the vector lane stays off for a store after a failed query embedding, so
#: an offline host costs one recall the deadline, not every recall. Not after an
#: exhausted quota: that refusal is already immediate.
QUERY_COOLDOWN_S = 60.0
#: Per (store, space): when its vector lane may ask for a query embedding again.
_paused_until: dict[tuple[str, str], float] = {}
_paused_lock = threading.Lock()

ADVISORY_LABEL = "ADVISORY_NOT_AUTHORITY"
DATA_FRAMING = "DATA"
RRF_K = 60.0
HALF_LIFE_DAYS = 180.0

# U2: days without ANY reads after which a capsule stops competing.
# Here and not just in the dataclass default because the MCP port needs the SAME
# number to know what to serve when the caller does not send it — two copies of
# the threshold would be two fences diverging the first time one of them changed.
#
# The 30 is NOT an invented threshold: it is the measurement of capsules never served from this
# store — `dod` 416 with less than 14 days, 128 between 14 and 30, 0 above; `task` 544,
# 159, and 535. It is the step where the `task` cohort actually breaks, and the initial target
# is those ~535. With 14 days, half of what just arrived would be caught; above
# 30, there is no step to catch at all.
DEFAULT_UNREAD_DAYS = 30


def decay_factor(
    *,
    now: datetime,
    valid_from: datetime,
    verified_at: datetime | None = None,
    pinned: bool = False,
) -> float:
    """The decay factor, in a single place, for BOTH reading surfaces.

    The clock is that of the LAST VERIFICATION and falls back to `valid_from` when there is
    none. The old rule was "recent write beats old", which rewarded
    whoever rewrote and punished whoever had been right for a year; the honest one is
    "recently verified beats long-ago verified", and it is what the anchor
    loop (U2) feeds without paying for a model.

    A pinned capsule does not decay: the pin is an owner statement about
    relevance, not an observation about freshness.

    WITHOUT any verification, the factor is NEUTRAL (1.0), not decay since
    writing. Decaying from `valid_from` is literally the old rule — "recent
    write beats old" — applied to whatever no one looked at: absence of evidence
    is not evidence of obsolescence, and the ratified plan conditioned this change
    precisely on the sweep covering the corpus, because before that EVERY capsule
    would drop by age of creation. Thus the condition lives per capsule instead of in a
    global switch: whoever was verified uses the verification clock; whoever
    was not is not punished for it.

    A verification that FAILED does not reset the clock (the query only returns
    `VERIFICATION_PASSING`) and also does not decay on its own: what handles the dead
    anchor is the curator queue, not relevance.

    BOTH rankers multiply relevance by this factor ONLY, not by the capsule's
    confidence. Confidence is the writer's own claim, and 0.6 when the writer
    omits it. In a one-lane recall the RRF gap between term rank 1 and 2 is
    1.6%, so a 0.95 capsule at term rank 39 outranked a 0.6 capsule at rank 1.
    Measured 2026-10-16 on the 1132 judged capsules of the 150-query replay:
    rel-2 is 20.2% at confidence >= 0.9 and 31.5% at the 0.6 default, so
    confidence does not predict relevance. Without it the judged term-lane
    replay goes from 66 to 81 queries with a rel-2 answer in the top 5 (exact
    McNemar p = 0.0015), and rel-1+ goes from 122 to 126 (p = 0.42).
    Confidence still feeds `min_confidence` and `decayed_weight` on the wire.
    """
    if pinned or verified_at is None:
        return 1.0
    age_days = max(0.0, (now - verified_at).total_seconds() / 86400.0)
    return float(2.0 ** (-age_days / HALF_LIFE_DAYS))


# pin `retrieve.rs`: the `excluded` map is a `BTreeMap<ExclusionReason, _>` and
# walks VARIANT order, which is a DOMINANCE order (the strongest fence
# first) and not alphabetical. `sorted()` on names coincides for
# some pairs and diverges for others — `expired` before `superseded` — which
# hides the divergence instead of exposing it.
EXCLUSION_ORDER: tuple[str, ...] = (
    "belief_unknown",
    "quarantined",
    "falsified",
    "archived",
    "superseded",
    "expired",
    "lapsed",
    "future_dated",
    "outside_time_window",
    "staged",
    "unread",
    "tombstoned",
    "min_confidence",
)

# Work-state capsules remain addressable by id/graph; ordinary evidence recall
# excludes them. Set to () to roll back without changing any stored data.
# `dod` only. The PLAN fenced `task` too, on the supply-side statistic "task is
# 78.7% never read". The demand side says otherwise. Replaying every historical
# receipt — see the retired replay script (git history), kept so
# these figures can be re-derived instead of believed. Measured over every
# receipt written BEFORE 2026-09-13 — the window matters and naming it is the
# point, because the script run later includes that day's traffic too:
#
#   task   1679 of 8582 result slots (19.6%), the MOST-served kind of all;
#          418 capsules served, 157 of them (37.6%) read across two or more days
#   dod      41 slots (0.5%); 37 capsules served, 1 read across two or more days
#
# Fencing `task` removes a fifth of everything the store serves and empties up
# to 110 real queries (an upper bound: fencing lets lower-ranked candidates rise
# into the freed slots). And 37.6% is MID-TABLE, not a floor — above
# failure_pattern 30%, fact 22%, plan 17% and brainstorm 14%, all unfenced, just
# under decision at 40%. Fencing task on that number would oblige fencing those
# four. `dod` at 1 of 37 is the only real outlier, and stays.
#
# The first version of this comment mixed two corpora in one sentence — «41
# slots» is full-corpus while «two capsules ever served» came from a replay that
# parsed only the JSON encoding. `recall_receipts.returned_ids` is a JSON array
# up to seq 1417 and a bare comma-separated list from 1418 (2026-08-28, the
# Python port), so a `json.loads` that skips what it cannot parse drops the
# newest traffic. Independent review caught it; `usage` corroborates the
# corrected figures (40 dod capsules with recall rows, recall_count summing 51).
#
# What settled it: `usage`, the table behind the "% ever
# retrieved" this fence existed to improve, could only ever see
# `nmemory_capsule_retrieve`. With 920 sessions each contract-bound to call
# `nmemory_context_show` first, it was blind to at least 37.2% of reads. Fencing
# `task` would have improved that number by shrinking the denominator of a
# measure that was not measuring what it claimed.
RECALL_EXCLUDED_KINDS: tuple[str, ...] = ("dod",)

# The vector lane's floor, relative to the query: a candidate only the vector
# lane matched is kept when its cosine is within this margin of the best
# eligible cosine for the same query; a candidate the term lane matched at all
# always stays. Measured 2026-10-16 against the lane's raw best, which an
# eligible best never exceeds, so the fence keeps at least what the measurement
# kept: of the vector-only items served, the largest gap to the best is 0.064
# for a judged rel-2 answer (150 replayed queries) and 0.082 for an expected
# PrecisionMemBench belief, while the median irrelevant arena item sits 0.112
# below. 0.083 is the largest relevant gap, 0.08227, rounded up to three
# decimals: the smallest such margin that keeps every judged-relevant item of
# both sets. An absolute cosine does not transfer: the arena's irrelevant items
# (median 0.599) and the replay's rel-2 items (median 0.704) live on different
# levels. `None` turns the fence off.
VECTOR_MARGIN = 0.083

#: Common English and Portuguese stop words (lowercase, diacritic-folded).
#: When a query has at least one content word, a candidate that matches only
#: stop words does not clear the default term floor or bypass VECTOR_MARGIN.
STOP_WORDS: frozenset[str] = frozenset(
    {
        "a",
        "an",
        "and",
        "ao",
        "aos",
        "are",
        "as",
        "at",
        "ate",
        "be",
        "been",
        "but",
        "by",
        "can",
        "com",
        "como",
        "d",
        "da",
        "das",
        "de",
        "did",
        "do",
        "does",
        "dos",
        "e",
        "ela",
        "elas",
        "ele",
        "eles",
        "em",
        "entre",
        "essa",
        "esse",
        "esta",
        "estao",
        "este",
        "eu",
        "foi",
        "for",
        "from",
        "ha",
        "had",
        "has",
        "have",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "isso",
        "isto",
        "it",
        "its",
        "ja",
        "lhe",
        "ll",
        "m",
        "mais",
        "mas",
        "me",
        "meu",
        "minha",
        "my",
        "na",
        "nao",
        "nas",
        "no",
        "nos",
        "nossa",
        "nosso",
        "not",
        "o",
        "of",
        "on",
        "or",
        "os",
        "ou",
        "our",
        "para",
        "por",
        "pra",
        "que",
        "re",
        "s",
        "se",
        "sem",
        "ser",
        "seu",
        "seus",
        "should",
        "sim",
        "so",
        "sobre",
        "sua",
        "suas",
        "t",
        "tem",
        "ter",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "to",
        "um",
        "uma",
        "umas",
        "uns",
        "ve",
        "voce",
        "voces",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "will",
        "with",
        "without",
        "would",
        "you",
        "your",
    }
)


def _is_stop_word(word: str) -> bool:
    return word in STOP_WORDS or (len(word) == 1 and word.isalpha())


def stop_only_term_matches(
    expanded_terms: list[str], term_hits: list[tuple[StoredCapsule, float]]
) -> frozenset[str]:
    """Return term-lane candidate IDs that match only stop words when the query has content words."""
    content_groups = [
        words
        for term in expanded_terms
        if (words := lexical_words(term)) and not all(_is_stop_word(w) for w in words)
    ]
    if not content_groups or not term_hits:
        return frozenset()
    return frozenset(
        sc.id
        for sc, _ in term_hits
        if not any(
            words <= lexical_words(sc.capsule.content) for words in content_groups
        )
    )


class InvalidExcludedKinds(NMemoryError, ValueError):
    """Recall exclusion kinds must name the stored classification vocabulary."""


class InvalidVectorMargin(NMemoryError, ValueError):
    """The vector margin must be None or a finite, non-negative number."""


def order_exclusions(counts: dict[str, int]) -> dict[str, int]:
    """Reorders to the pin's order; an unknown reason goes to the end.

    The fallback is not a courtesy: a new fence not present in
    `EXCLUSION_ORDER` continues to appear on the wire, just without a guaranteed position —
    losing an exclusion would be worse than misordering it.
    """
    known = {r: counts[r] for r in EXCLUSION_ORDER if r in counts}
    rest = {r: n for r, n in counts.items() if r not in known}
    return {**known, **rest}


def clears_relevance_floor(score: float, floor: float) -> bool:
    """The retention test for A SINGLE lane: `score >= floor`, and a NON-POSITIVE floor is OFF.

    `>=` and not `>`: with `>` a floor placed exactly on a score would cut
    the very point it names, and the floor would no longer be selectable from
    the observed distribution.

    The OFF branch is not a courtesy — it is what makes the default `0.0` an EXACT no-op. The
    vector lane reads `sim = 1 - vec_distance_cosine(embedding, query)`
    (`database.py` `search_cosine`), and that distance is in `[0, 2]`,
    hence `sim` lives in `[-1, 1]`: measured on this box, an embedding opposite to the
    query reads exactly `-1.0`. With raw `score >= floor`, the default `0.0` would have
    excluded that capsule — which is served today — and the default would cease to
    preserve behavior. A negative floor is indistinguishable from OFF: the fence
    is a POSITIVE floor, and that is how the grid choosing it must read it.
    """
    return floor <= 0.0 or score >= floor


def _record_term_lane_miss(
    store: Store,
    terms: list[str],
    outcome: str,
    now: datetime,
    *,
    call_id: str,
    lane: str,
    project_id: str | None,
    project_prefix: str | None,
) -> None:
    """One ledger row per folded caller term — pin store.rs:5036-5066.

    The pin takes `terms: &[String]` — the caller's TERM list — and folds each
    RAW term WHOLE (`fold_term`: trim + lowercase + diacritic-fold, internal
    spaces preserved), keeps terms with at least one alphanumeric character,
    deduplicates via a BTreeSet (sorted), and writes one row per surviving term
    with the TERM lane's single outcome. Splitting on whitespace first turned
    one multi-word term into N rows — the same flattening, in the miss ledger.

    THE WRITE IS GUARDED, and the guard lives HERE and not in the caller because both
    callers have the same problem: `retrieve()` is a READ path, and
    this is the only write it carries. Outside the vector lane's `try/except`,
    a typed failure in `record_recall_miss` propagated from inside
    `retrieve()` and killed a response that was already ready — the miss
    ledger taking down the retrieval it exists to measure. Exceptions
    are TYPED: a broad `except` on a persistence path is forbidden by the
    RULEBOOK, and would continue swallowing domain defects.
    """
    folded = {"".join(fold_diacritic(c) for c in t.strip().lower()) for t in terms}
    for term in sorted(folded):
        if any(c.isalnum() for c in term):
            try:
                store.traffic.record_recall_miss(
                    term,
                    outcome,
                    now,
                    call_id=call_id,
                    lane=lane,
                    project_id=project_id,
                    project_prefix=project_prefix,
                )
            except (DatabaseError, nott_database.Error, OSError) as error:
                logger.warning(
                    "recall miss ledger write failed for %r: %s: %s",
                    term,
                    type(error).__name__,
                    error,
                )
                return


@dataclass(frozen=True)
class RetrieveParams:
    query: str
    # pin `retrieve.rs` `TermGroup`: the caller sends GROUPS — words within
    # a term match with AND, terms among each other with OR. Flattening this into a string and
    # reconstructing by space makes each word become its own group, and a
    # multi-word term starts grounding on any subset of itself.
    # When absent, the caller is the CLI, whose query is free text: each word is a
    # term, which is exactly the usual `query.split()`.
    terms: list[str] | None = None
    project_id: str | None = None
    project_prefix: str | None = None
    limit: int | None = 10
    anchor_root: str | None = None
    staged: bool = False
    # U1: serving a capsule whose standing is `caducou` by OMISSION was the
    # defect; `True` is the explicit opt-in, like `staged` above.
    lapsed: bool = False
    # U2: days without ANY reads after which the capsule stops
    # competing in the service (see `DEFAULT_UNREAD_DAYS` for the measurement that fixes the
    # number). `None` disables the fence and returns the entire corpus.
    unread_days: int | None = DEFAULT_UNREAD_DAYS
    min_confidence: float | None = None
    lane: str | None = "auto"
    lanes: list[str] | None = None
    token_budget: int | None = None
    time_window: dict[str, str | None] | None = None
    known_at: str | None = None
    session_id: str | None = None
    effort_id: str | None = None
    effort_set: list[str] | None = None
    topic_id: str | None = None
    topic_set: list[str] | None = None
    vector_k: int | None = 10
    term_floor: float = 0.0
    vector_floor: float = 0.0
    vector_margin: float | None = VECTOR_MARGIN
    min_term_margin: float | None = None
    weight_blend: float | None = None
    exclude_kinds: list[str] | None = None

    def __post_init__(self) -> None:
        if self.vector_margin is not None and not (
            math.isfinite(self.vector_margin) and self.vector_margin >= 0
        ):
            raise InvalidVectorMargin(
                f"vector_margin must be None or a finite number >= 0; got {self.vector_margin!r}"
            )
        if self.exclude_kinds is not None and (
            not isinstance(self.exclude_kinds, list)
            or any(
                not isinstance(kind, str)
                or kind not in {k.value for k in CandidateKind}
                for kind in self.exclude_kinds
            )
        ):
            raise InvalidExcludedKinds(
                "exclude_kinds must be an array of known capsule kinds"
            )


@dataclass(frozen=True)
class EvidenceItem:
    id: str
    handle: str
    headline: str
    content: str
    confidence: float
    decayed_weight: float
    relevance_score: float
    matched_terms: list[str]
    anchor_live: str
    anchor_drift: str
    anchor_effective: str | None
    created_at: str
    valid_from: str
    valid_to: str | None
    authority_class: str
    instruction_taint: bool
    tier: str
    source_quote: dict[str, str] | None = None
    bm25: float | None = None

    def to_dict(self) -> dict[str, Any]:
        d = {
            "id": self.id,
            "handle": self.handle,
            "headline": self.headline,
            "content": self.content,
            "confidence": self.confidence,
            "decayed_weight": round(self.decayed_weight, 4),
            "relevance_score": round(self.relevance_score, 4),
            "bm25": self.bm25,
            "matched_terms": self.matched_terms,
            "anchor_live": self.anchor_live,
            "anchor_drift": self.anchor_drift,
            "anchor_effective": self.anchor_effective,
            "created_at": self.created_at,
            "valid_from": self.valid_from,
            "valid_to": self.valid_to,
            "authority_class": self.authority_class,
            "instruction_taint": self.instruction_taint,
            "tier": self.tier,
        }
        if self.source_quote is not None:
            d["source_quote"] = self.source_quote
        return d


def term_matches(content_tokens: set[str], term: str) -> bool:
    """All folded tokens of the term appear in the content.

    pin `retrieve.rs:3995 fn term_matches` — *"the store's within-term AND
    match, mirrored"*: it is the SAME question that `search_fts_scoped` asks to
    decide if it matched, answered over the same tokens.
    """
    tokens = lexical_words(term)
    return bool(tokens) and tokens <= content_tokens


def matched_groups(
    content: str, groups: list[str], group_aliases: dict[str, list[str]]
) -> list[str]:
    """The explain: which TERMS matched, and which only via alias.

    pin `retrieve.rs:3975 fn matched_groups`. Emits the whole term, never its
    words — and `alias:<term>` when what matched was the synonym, which is
    what the tool's own description promises to the caller.
    """
    content_tokens = set(lexical_words(content))
    out: list[str] = []
    for group in groups:
        if term_matches(content_tokens, group):
            out.append(group)
        elif any(term_matches(content_tokens, a) for a in group_aliases.get(group, ())):
            out.append(f"alias:{group}")
    return out


Ranked = tuple[StoredCapsule, float, float]
"""One eligible candidate: the capsule, its decayed weight and its relevance."""


def build_envelope(
    outcome: str,
    *,
    lane: str,
    lane_used: str,
    lanes_run: list[str],
    exclusions: dict[str, int] | None = None,
    total_candidates: int = 0,
    evidence: list[dict[str, Any]] | None = None,
    receipt_id: str | None = None,
    trim: dict[str, int] | None = None,
    lane_error: str | None = None,
    embedding_usage: dict[str, int] | None = None,
    lane_note: str | None = None,
) -> dict[str, Any]:
    """The response envelope of every outcome, in the one key order the wire serves.

    `receipt_id` is a `grounded` key only; the trim counters belong to the
    `grounded` outcome too. `degraded`/`lane_error` mean an EXCEPTION in the
    vector lane, never a lane that ran and returned empty; `lane_note` says why a
    configured vector lane was deliberately not run.
    """
    envelope: dict[str, Any] = {
        "label": ADVISORY_LABEL,
        "framing": DATA_FRAMING,
        "outcome": outcome,
        "evidence": evidence if evidence is not None else [],
    }
    if outcome == "grounded":
        envelope["receipt_id"] = receipt_id
    envelope["exclusions"] = dict(exclusions) if exclusions is not None else {}
    envelope["total_candidates"] = total_candidates
    envelope["lane"] = lane
    envelope["lane_used"] = lane_used
    envelope["lanes_run"] = list(lanes_run)
    if trim is not None:
        envelope.update(trim)
    if lane_error is not None:
        envelope["degraded"] = True
        envelope["lane_error"] = lane_error
    if embedding_usage is not None:
        envelope["embedding_usage"] = embedding_usage
    if lane_note is not None:
        envelope["lane_note"] = lane_note
    return envelope


def group_alias_forms(group: str, table: list[tuple[str, str]]) -> list[str]:
    """The group with each taught term replaced by its alias, one form per alias.

    The group and each taught term are split and folded into search words
    (`lexical_tokens`), so "gate-dbs", "keyboard," and "résumé" match the
    words that search matches. One form replaces every occurrence of the
    taught words in the group ("weekly e-mail" with e-mail -> email gives
    "weekly email"). A group equal to a taught term gives the alias itself. Only taught terms are looked up, never aliases."""
    words = lexical_tokens(group)
    whole = group.strip().lower()
    forms: list[str] = []
    for term, alias in table:
        taught = lexical_tokens(term)
        n = len(taught)
        out: list[str] = []
        hit = False
        i = 0
        while i < len(words):
            if n and words[i : i + n] == taught:
                out.append(alias)
                hit = True
                i += n
            else:
                out.append(words[i])
                i += 1
        if hit:
            form = " ".join(out)
        elif term == whole:  # A term with no search words matches only as a whole.
            form = alias
        else:
            continue
        if form not in forms:
            forms.append(form)
    return forms


def expand_terms(groups: list[str], group_aliases: dict[str, list[str]]) -> list[str]:
    """The groups, then each group's aliases not already present, in order.

    A group enters search IN FULL — `search_fts_scoped` already does the AND
    within it; aliases enter as sibling groups, which is the OR.
    """
    expanded: list[str] = list(groups)
    for g in groups:
        for a in group_aliases[g]:
            if a not in expanded:
                expanded.append(a)
    return expanded


def lane_used(term_ranks: dict[str, int], vector_ranks: dict[str, int]) -> str:
    """The lanes whose RANKS fed the ranking — identical across all three outcomes.

    `lane_used` names the lanes whose ranks fed the ranking, never those that
    merely executed: a vector lane that ran and returned nothing fused nothing.
    With no rank at all it is `"term"` — inventing `"vector"` would be the
    symmetric lie (`test_retrieve_vector_lane_accounting.py`).
    """
    if term_ranks and vector_ranks:
        return "fused"
    return "vector" if vector_ranks else "term"


def time_window_bounds(
    window: dict[str, str | None] | None,
) -> tuple[datetime | None, datetime | None]:
    """The parsed `time_window` bounds; an absent bound is None."""
    win_from: datetime | None = None
    win_to: datetime | None = None
    if window is not None:
        if win_from_raw := window.get("from"):
            win_from = parse_rfc3339(win_from_raw)
        if win_to_raw := window.get("to"):
            win_to = parse_rfc3339(win_to_raw)
    return win_from, win_to


def effective_term_floor(
    term_floor: float,
    min_term_margin: float | None,
    term_hits: list[tuple[StoredCapsule, float]],
) -> float:
    """THE MARGIN GATE: FLAT evidence does not ground.

    Measured on pra2 — the top1-top2 BM25 margin separates unanswerable probes
    from answerable ones (AUC 0.908); ABSOLUTE score separates nothing. When the
    distance between the lane's first and second match falls below
    `min_term_margin`, the lane's effective floor rises to `inf` and all its
    candidates are COUNTED as `below_relevance_floor`, with the outcome
    `missing_evidence`, never `abstain`: there was a match.

    REFUSAL FORM, never `max(term_floor, top1 - tau)`: a floor derived from
    top-1 itself is always cleared by top-1 — a TIE (margin 0), which is
    exactly the flat case the gate exists to catch, would pass entirely. With
    a single match there is no second position: OFF, because a single match is
    not flat.
    """
    if (
        min_term_margin is not None
        and term_hits
        and len(term_hits) >= 2
        and (term_hits[0][1] - term_hits[1][1]) < min_term_margin
    ):
        return math.inf
    return term_floor


@dataclass(frozen=True)
class FenceClock:
    """The instants and bounds every fence of one call reads.

    `belief` is `known_at` when the caller sent one, else `now`; `as_of` is
    `belief` only when `known_at` was sent. `now` stays the decay clock.
    """

    now: datetime
    belief: datetime
    as_of: datetime | None
    window_from: datetime | None
    window_to: datetime | None


def scope_exclusion(
    sc: StoredCapsule, params: RetrieveParams, belief: datetime
) -> str | None:
    """The first scope fence a candidate fails: session, effort, topic, then belief time.

    A capsule created after `known_at` did not exist yet at that instant.
    """
    if params.session_id is not None and sc.session_id != params.session_id:
        return "out_of_session"
    if params.effort_set is not None and sc.id not in params.effort_set:
        return "outside_effort"
    if params.topic_set is not None and sc.id not in params.topic_set:
        return "outside_topic"
    if params.known_at is not None and sc.created_at > belief:
        return "future_dated"
    return None


def currency_exclusion(sc: StoredCapsule, reference: datetime) -> str | None:
    """Currency and expiry relative to belief time (or now)."""
    freshness = sc.capsule.freshness
    if freshness.valid_to is not None and freshness.valid_to < reference:
        return "expired"
    if freshness.valid_from > reference:
        return "future_dated"
    return None


def window_exclusion(
    event_time: EventTimeRecord | None, capture_time: datetime, clock: FenceClock
) -> str | None:
    """Use declared event time, or capture time when no event time is declared."""
    fact_from = event_time.event_from() if event_time is not None else capture_time
    fact_to = event_time.event_to() if event_time is not None else capture_time
    if (clock.window_from is not None and fact_to < clock.window_from) or (
        clock.window_to is not None and fact_from > clock.window_to
    ):
        return "outside_time_window"
    return None


def staged_exclusion(review_state: str | None, serve_staged: bool) -> str | None:
    """Staged fence, by PERMISSION and not by exclusion.

    A closed list of verdicts to exclude (`("proposed", "rejected")`) reverses
    the burden: every verdict this code does not yet know passes the fence and
    is served with full authority. The case this opened was `expired` from the
    staged queue — a verdict whose purpose is to say "no one judged this within
    the horizon" made the capsule SERVED, which is the opposite of what it
    means and what LOCK 2 forbids. The rule is that of
    `Store.list_review_fenced` (`verdict <> 'ratified'`): whoever has no
    verdict at all, or whoever has `ratified`, is served.
    """
    fenced = review_state is not None and review_state != "ratified"
    return "staged" if fenced and not serve_staged else None


def unread_exclusion(
    unread_days: int | None,
    pinned: bool,
    awake_since: datetime,
    reference: datetime,
) -> str | None:
    """THE READ FENCE (U2): the second clock, ORTHOGONAL to `decay_factor`.

    That one measures PROOF and is neutral for whatever was never verified —
    measured, 669 of 12411 capsules (5%) —, and it is right that it be so:
    absence of evidence is not evidence of obsolescence. This one measures
    DEMAND, which is a much weaker statement and the only one ranking needs:
    no one has touched it for N days, so it does not compete.

    FOR ALL KINDS, and here lies the half that the standing fence does not
    reach: `dod` and `task` are `STANDING_EXEMPT_KINDS` and escape it by having
    no standing at all. The read clock does not ask them their kind.

    THE PIN continues to exempt, by the law `decay_factor` already writes: the
    pin is an owner statement about relevance, and this fence is an observation
    about attention. The statement wins.

    WITHOUT any reads, the caller passes `created_at` as `awake_since`: the
    capsule that arrived yesterday and that no one has read yet is not a
    dormant capsule, it is a new capsule.

    Reading AWAKENS: the service records `record_recall` on each returned id
    and `read_traffic` from other surfaces enters the same union. State is
    DERIVED from reading — nothing here writes to keep a capsule alive.
    """
    if unread_days is None or pinned:
        return None
    idle_days = (reference - awake_since).total_seconds() / 86400.0
    return "unread" if idle_days > unread_days else None


def fused_relevance(
    cid: str,
    *,
    term_scores: dict[str, float],
    vector_sims: dict[str, float],
    term_ranks: dict[str, int],
    vector_ranks: dict[str, int],
    term_floor: float,
    vector_floor: float,
    stop_only_term_ids: frozenset[str] = frozenset(),
) -> float | None:
    """THE RELEVANCE FLOOR, per lane, then the RRF score; None when no lane clears.

    Per LANE because scales are not comparable: BM25 is positive without
    ceiling (`store.capsules.search_fts_scoped`), cosine lives in `[-1, 1]`. A
    single floor downstream of the union would compare the two.

    Surviving means passing the floor of its OWN lane — ONE is enough. A
    capsule that the term lane brought weakly but cosine puts at the top is
    relevant by cosine, and cutting it would require both scales to agree,
    which is what the per-lane floor exists not to ask.

    It is the LAST fence of the loop, and a COUNTED exclusion, never an id that
    vanishes: (a) a capsule that a stronger fence already excluded keeps that
    fence's NAME — exclusions are a dominance order (see EXCLUSION_ORDER) and
    relevance is the weakest statement; (b) the floor runs BEFORE the cap
    `vector_k_capped`, which only sees `eligible`, so a capsule cut here is
    never counted twice. The floor is not a filter INSIDE the lane — there
    matches would never enter `candidate_ids` and `missing_evidence` would
    collapse into `abstain`.
    """
    in_term = cid in term_scores and (
        (term_floor > 0.0 and term_scores[cid] >= term_floor)
        if cid in stop_only_term_ids
        else clears_relevance_floor(term_scores[cid], term_floor)
    )
    in_vector = cid in vector_sims and clears_relevance_floor(
        vector_sims[cid], vector_floor
    )
    if not (in_term or in_vector):
        return None
    r_term = (
        term_ranks.get(cid, 1000) if cid not in stop_only_term_ids or in_term else 1000
    )
    r_vec = vector_ranks.get(cid, 1000)
    return (1.0 / (RRF_K + r_term)) + (1.0 / (RRF_K + r_vec))


def cap_vector_lane(
    eligible: list[Ranked],
    term_ranks: dict[str, int],
    vector_ranks: dict[str, int],
    v_limit: int,
) -> tuple[list[Ranked], int]:
    """THE VECTOR CAP over ELIGIBLE candidates; returns the kept ones and how many it cut.

    A capsule that also matched by term survives by its own lane — the cap
    limits what the VECTOR lane brings into the ranking, never what the term
    lane already brought. The cap COUNTS what it cuts: without the count, with
    `vector_k=0` the envelope served `missing_evidence` with six matched and no
    excluder named.
    """
    by_cosine = sorted(
        (sc.id for sc, _, _ in eligible if sc.id in vector_ranks),
        key=lambda cid: vector_ranks[cid],
    )
    keep = set(by_cosine[:v_limit])
    kept = [item for item in eligible if item[0].id in term_ranks or item[0].id in keep]
    return kept, len(eligible) - len(kept)


def blend_multiplier(weight_blend: float, feedback_weight: float | None) -> float:
    """The relevance multiplier of the weight blend; an inactive blend (<= 0) is 1.0.

    A capsule with no feedback weight blends as 0.5.
    """
    if weight_blend <= 0.0:
        return 1.0
    w_val = feedback_weight if feedback_weight is not None else 0.5
    return (1.0 - weight_blend) + weight_blend * w_val


def trim_to_budget(
    eligible: list[Ranked], limit: int, token_budget: int
) -> tuple[list[Ranked], int, int]:
    """Trim the ranked list by `limit`, then by `token_budget`.

    Returns the kept items, the count trimmed by limit and the count trimmed by
    budget. The cost is the pin's `approx_tokens = chars.div_ceil(4)`, at least
    1. Packing skips every item that does not fit the remaining budget and
    continues, so one long capsule does not hide the matches ranked after it
    (measured 2026-10-16: the median block held one capsule). Only when no item
    fits is the first one served, so a grounded answer is never empty. A zero
    budget serves nothing.
    """
    top_items: list[Ranked] = []
    used_tokens = 0
    trimmed_by_limit = 0
    trimmed_by_budget = 0
    for item in eligible:
        if len(top_items) >= limit:
            trimmed_by_limit += 1
            continue
        cost = max(1, (len(item[0].capsule.content) + 3) // 4)
        if used_tokens + cost > token_budget:
            trimmed_by_budget += 1
            continue
        used_tokens += cost
        top_items.append(item)
    if not top_items and eligible and token_budget > 0:
        top_items.append(eligible[0])
        trimmed_by_budget -= 1
    return top_items, trimmed_by_limit, trimmed_by_budget


def _pause_key(store: Store, lane: EmbeddingClient) -> tuple[str, str]:
    return str(store.db.path), lane.space_fingerprint.as_str()


def vector_lane_note(store: Store, lane: EmbeddingClient) -> str | None:
    """Why a configured vector lane must not run now, or None when it may. The one
    gate of every consumer of the vector lane (recall and context)."""
    with _paused_lock:
        wait = _paused_until.get(_pause_key(store, lane), 0.0) - time.monotonic()
    if wait > 0:
        return f"vector lane paused for {wait:.0f} s after a failed query embedding"
    if store.embeddings.serving_space() != lane.space_fingerprint.as_str():
        return "vector lane off until the embedding index of this space is complete"
    return None


def query_vector(store: Store, lane: EmbeddingClient, query: str) -> list[float]:
    """The query's embedding; a failure pauses this store's vector lane."""
    key = _pause_key(store, lane)
    try:
        vec = lane.embed_query(query)
    except EmbeddingQuotaExhausted:
        raise
    except EmbeddingError:
        with _paused_lock:
            _paused_until[key] = time.monotonic() + QUERY_COOLDOWN_S
        raise
    with _paused_lock:
        _paused_until.pop(key, None)
    return vec


def _search_vector_lane(
    store: Store,
    params: RetrieveParams,
    lane: EmbeddingClient,
    query: str,
    v_limit: int,
    stored_map: dict[str, StoredCapsule],
    vector_rank_map: dict[str, int],
    vector_sim_map: dict[str, float],
    count: bool = True,
) -> tuple[dict[str, int] | None, str | None]:
    """Run the vector lane into the given maps; returns its usage and its error.

    A failure keeps what the lane had already recorded, and is reported, never
    raised: the response continues on the term lane. With `count` False (the
    viewer) the lane does not write the store's embedding phase.
    """
    embedding_usage: dict[str, int] | None = None
    try:
        misses = lane.embed_query.cache_info().misses
        try:
            q_vec = query_vector(store, lane, query)
        except EmbeddingError:
            # A query-only outage (no job pending) must not read as `ready`.
            if count:
                store.embeddings.set_lexical_only(True)
            raise
        if count and lane.embed_query.cache_info().misses > misses:  # provider asked
            store.embeddings.set_lexical_only(False)
        stats = lane.usage_stats()
        if not stats.is_empty():
            embedding_usage = {
                "openai_requests": stats.openai_requests,
                "openai_prompt_tokens": stats.openai_prompt_tokens,
            }
        # U0g: `vector_k` IS the vector lane cap (schema default 10). We search
        # WIDE, ALWAYS, and do not filter here: the cap is applied AFTER
        # eligibility, where it COUNTS what it cuts (`vector_k_capped`).
        # Cutting in SQL cuts BEFORE the late fences — session, effort, staged,
        # lifecycle — so capsules the fence would remove anyway occupied the
        # window and took with them an ELIGIBLE capsule that was never fetched
        # (measured by independent review, 2026-09-18). The invariant is that
        # every id cosine returns ends up a candidate or a COUNTED exclusion.
        v_matches = store.embeddings.search_cosine(
            query_vector=q_vec,
            limit=max(v_limit, 50),
            project_id=params.project_id,
            project_prefix=params.project_prefix,
            space_fingerprint=lane.space_fingerprint.as_str(),
        )
        for idx, (cid, sim) in enumerate(v_matches):
            vector_rank_map[cid] = idx + 1
            vector_sim_map[cid] = sim
            if cid not in stored_map:
                try:
                    sc = store.capsules.get(cid)
                except Tombstoned:
                    # Pin retrieve.rs:142 — a tombstoned id surfaced by a
                    # stale vector is skipped, never surfaced, never fatal.
                    continue
                if sc:
                    stored_map[cid] = sc
    except (
        EmbeddingError,
        SupersededSpace,
        DatabaseError,
        nott_database.Error,
        OSError,
    ) as e:
        lane_error = f"{type(e).__name__}: {e}"
        logger.warning("vector search lane failed: %s", lane_error)
        return embedding_usage, lane_error
    return embedding_usage, None


@dataclass(frozen=True)
class _Fences:
    """What the per-candidate fence reads, gathered once per call."""

    params: RetrieveParams
    clock: FenceClock
    verified_at: dict[str, datetime]
    last_read: dict[str, datetime]
    term_scores: dict[str, float]
    vector_sims: dict[str, float]
    term_ranks: dict[str, int]
    vector_ranks: dict[str, int]
    term_floor: float
    stop_only_term_ids: frozenset[str] = frozenset()


def eligibility_exclusion(
    store: Store,
    clock: FenceClock,
    sc: StoredCapsule,
    *,
    time_window: bool = False,
    serve_staged: bool = False,
    review_verdicts: dict[str, str] | None = None,
) -> str | None:
    """Apply the shared lifecycle, currency, time and review fences."""
    cid = sc.id
    tier = store.tiers.get_tier(cid, as_of=clock.as_of)
    if tier == Tier.QUARANTINED:
        return "quarantined"
    if store.relations.is_falsified(cid, as_of=clock.as_of):
        return "falsified"
    if tier == Tier.ARCHIVED:
        return "archived"
    if store.relations.is_superseded(cid, as_of=clock.as_of):
        return "superseded"
    reason = currency_exclusion(sc, clock.belief)
    if reason is not None:
        return reason
    if time_window:
        reason = window_exclusion(
            store.capsules.event_time_of(cid), sc.capsule.freshness.valid_from, clock
        )
        if reason is not None:
            return reason
    reason = staged_exclusion(
        store.reviews.review_verdict(cid, as_of=clock.as_of)
        if review_verdicts is None
        else review_verdicts.get(cid),
        serve_staged,
    )
    if reason is not None:
        return reason
    return None


def _fence(store: Store, fences: _Fences, sc: StoredCapsule) -> str | Ranked:
    """The first exclusion reason of one candidate, or its ranked entry.

    The fences run in the published dominance order, each read only once the
    fences before it passed: scope, lifecycle (quarantined, falsified,
    archived, superseded), currency, time window, staged, unread, confidence
    and, last, relevance.
    """
    params, clock = fences.params, fences.clock
    cid = sc.id
    reason = scope_exclusion(sc, params, clock.belief)
    if reason is not None:
        return reason
    reason = eligibility_exclusion(
        store,
        clock,
        sc,
        time_window=params.time_window is not None,
        serve_staged=params.staged,
    )
    if reason is not None:
        return reason
    pinned = store.pins.get_pin(cid, as_of=clock.as_of)
    reason = unread_exclusion(
        params.unread_days,
        pinned,
        fences.last_read.get(cid, sc.created_at),
        clock.belief,
    )
    if reason is not None:
        return reason
    decay = decay_factor(
        now=clock.now,
        valid_from=sc.capsule.freshness.valid_from,
        verified_at=fences.verified_at.get(cid),
        pinned=pinned,
    )
    decayed_weight = sc.capsule.confidence * decay
    if params.min_confidence is not None and decayed_weight < params.min_confidence:
        return "min_confidence"
    rrf = fused_relevance(
        cid,
        term_scores=fences.term_scores,
        vector_sims=fences.vector_sims,
        term_ranks=fences.term_ranks,
        vector_ranks=fences.vector_ranks,
        term_floor=fences.term_floor,
        vector_floor=params.vector_floor,
        stop_only_term_ids=fences.stop_only_term_ids,
    )
    if rrf is None:
        return "below_relevance_floor"
    return (sc, decayed_weight, rrf * decay)


def cut_vector_margin(
    eligible: list[Ranked], fences: _Fences, margin: float | None
) -> tuple[list[Ranked], int]:
    """THE VECTOR MARGIN over ELIGIBLE candidates; returns the kept ones and how many it cut.

    The best cosine is read among the eligible candidates only: a capsule that
    a scope or lifecycle fence removed must not raise the bar for the rest. A
    candidate the term lane matched on a content word (or that cleared a
    positive term floor) survives by its own lane.
    """
    # A NaN cosine (a NULL distance) must not set the bar for the rest.
    sims = [
        fences.vector_sims[sc.id]
        for sc, _, _ in eligible
        if sc.id in fences.vector_sims and math.isfinite(fences.vector_sims[sc.id])
    ]
    if margin is None or not sims:
        return eligible, 0
    floor = max(sims) - margin
    kept = [
        item
        for item in eligible
        if (
            item[0].id in fences.term_scores
            and (
                item[0].id not in fences.stop_only_term_ids
                or (
                    fences.term_floor > 0.0
                    and fences.term_scores[item[0].id] >= fences.term_floor
                )
            )
        )
        or fences.vector_sims.get(item[0].id, -math.inf) >= floor
    ]
    return kept, len(eligible) - len(kept)


def _evidence_item(
    store: Store,
    item: Ranked,
    groups: list[str],
    group_aliases: dict[str, list[str]],
    anchor_root: str,
    term_scores: dict[str, float],
) -> EvidenceItem:
    """The served evidence of one returned capsule, with its anchor probes."""
    sc, decayed_weight, rel_score = item
    cid = sc.id
    cap = sc.capsule
    # Matched terms explanation — the SAME ranker matcher, never a second one.
    hits = matched_groups(cap.content, groups, group_aliases)
    # Anchor probes (pin retrieve.rs:3312-3319: both liveness and drift probe
    # against the effective/rebound anchor if one exists).
    a_eff = store.anchors.get_reanchor(cid)
    effective_anchor = a_eff or cap.provenance.anchor
    a_live = probe_anchor_liveness(effective_anchor, anchor_root)
    stored_a_hash = store.anchors.get_anchor_hash(cid)
    a_drift = probe_anchor_drift(effective_anchor, stored_a_hash, anchor_root)
    # honesty-pair: the STANDING verdict, folded with the drift probe (pin
    # retrieve.rs:3317): a capture-time `verified` beside a drifted anchor is
    # re-verified dynamically against current bytes.
    sq_rec = store.epistemics.get_source_quote(cid)
    standing_sq: dict[str, str] | None = None
    if sq_rec is not None:
        raw_quote = sq_rec["quote"]
        if a_drift == "unchanged":
            standing_v = sq_rec["verdict"]
        else:
            standing_v = quote_verdict(effective_anchor, anchor_root, raw_quote)
        standing_sq = {"quote": raw_quote, "verdict": standing_v}
    return EvidenceItem(
        id=cid,
        handle=store.capsules.get_handle(cid),
        headline=headline_of(cap.content),
        content=cap.content,
        confidence=cap.confidence,
        decayed_weight=decayed_weight,
        relevance_score=rel_score,
        bm25=term_scores.get(cid),
        matched_terms=hits,
        anchor_live=a_live,
        anchor_drift=a_drift,
        anchor_effective=a_eff,
        created_at=format_rfc3339(sc.created_at),
        valid_from=format_rfc3339(cap.freshness.valid_from),
        valid_to=format_rfc3339(cap.freshness.valid_to)
        if cap.freshness.valid_to
        else None,
        authority_class=cap.authority_class.value,
        instruction_taint=cap.instruction_taint,
        tier=store.tiers.get_tier(cid).value,
        source_quote=standing_sq,
    )


def retrieve(
    store: Store,
    params: RetrieveParams,
    embeddings: EmbeddingClient | None = None,
    now: datetime | None = None,
    record_receipt: bool = True,
    asked_by_session: str | None = None,
    count: bool = True,
) -> dict[str, Any]:
    """Execute multi-lane grounded retrieval.

    With `count` False (the viewer) the read writes nothing: no miss-ledger row,
    no `record_recall`, no receipt and no embedding phase.

    The shell makes every store and embedding call, and each write (the two
    miss-ledger rows, `record_recall`, the receipt), in the order and under the
    early returns that define the three outcomes; the decisions are the pure
    steps above.
    """
    now_dt = now or datetime.now(UTC)
    # Even an EARLY abstain echoes the lane the caller requested.
    lane_pref = params.lane or (params.lanes[0] if params.lanes else "auto")
    query = params.query.strip()
    if not query:
        # No lane ran: `lanes_run` is EMPTY and `lane_used` is "term" — inventing
        # `vector` would be the symmetric lie.
        return build_envelope("abstain", lane=lane_pref, lane_used="term", lanes_run=[])

    groups = [t for t in (params.terms or query.split()) if t.strip()]
    alias_table = [(t, a) for t, a, _ in store.aliases.list_aliases()]
    group_aliases: dict[str, list[str]] = {
        g: group_alias_forms(g, alias_table) for g in groups
    }
    expanded_terms = expand_terms(groups, group_aliases)
    run_term = lane_pref in ("auto", "term", "fused")
    run_vector = lane_pref in ("auto", "vector", "fused")
    # L8: one call, one id — the ledger groups the terms of the SAME query.
    call_id = uuid.uuid4().hex
    # ONE expression decides both what RUNS and what `lanes_run` SAYS; it keeps
    # the narrowed client so the lane site needs no second predicate.
    vector_lane: EmbeddingClient | None = (
        embeddings
        if run_vector and embeddings and embeddings.is_configured() and store.db
        else None
    )
    vector_note = vector_lane_note(store, vector_lane) if vector_lane else None
    if vector_note is not None:
        vector_lane = None
    # F2: `lanes_run` is the lanes that EXECUTED, pure fact, never a judgement.
    lanes_run: list[str] = []
    if run_term:
        lanes_run.append("term")
    if vector_lane is not None:
        lanes_run.append("vector")

    # 1. Term search lane (FTS5); BM25 is kept beside the rank.
    term_hits: list[tuple[StoredCapsule, float]] = []
    if run_term:
        term_hits = store.capsules.search_fts_scoped(
            terms=expanded_terms,
            project_id=params.project_id,
            project_prefix=params.project_prefix,
            session_id=params.session_id,
            effort_set=params.effort_set,
            topic_set=params.topic_set,
            limit=max(params.limit or 10, 50),
        )
    stop_only_ids = stop_only_term_matches(expanded_terms, term_hits)
    term_rank_map = {sc.id: idx + 1 for idx, (sc, _) in enumerate(term_hits)}
    term_score_map = {sc.id: bm25 for sc, bm25 in term_hits}
    stored_map = {sc.id: sc for sc, _ in term_hits}

    # The window is read BEFORE the lanes: "an explicit `time_window` runs
    # before this cap" (schema; U0b).
    win_from_dt, win_to_dt = time_window_bounds(params.time_window)
    v_limit = params.vector_k if params.vector_k is not None else 10

    # 2. Vector search lane (sqlite-vec exact cosine)
    vector_rank_map: dict[str, int] = {}
    vector_sim_map: dict[str, float] = {}
    embedding_usage: dict[str, int] | None = None
    vector_lane_error: str | None = None
    if vector_lane is not None:
        embedding_usage, vector_lane_error = _search_vector_lane(
            store,
            params,
            vector_lane,
            query,
            v_limit,
            stored_map,
            vector_rank_map,
            vector_sim_map,
            count,
        )

    # Pin retrieve.rs:1793-1803 — the miss ledger is keyed on the TERM lane's own
    # observation, independent of the vector lane and of the final outcome.
    # F2b: a term lane that NEVER RAN records nothing; one that ran and found
    # nothing records its abstain.
    if count and run_term and groups and not term_hits:
        _record_term_lane_miss(
            store,
            groups,
            "abstain",
            now_dt,
            call_id=call_id,
            lane="term",
            project_id=params.project_id,
            project_prefix=params.project_prefix,
        )

    candidate_ids = set(term_rank_map.keys()) | set(vector_rank_map.keys())
    if not candidate_ids:
        # `abstain` IS `not candidate_ids`: both rank maps are empty, so the lane
        # used can only be "term".
        return build_envelope(
            "abstain",
            lane=lane_pref,
            lane_used="term",
            lanes_run=lanes_run,
            lane_error=vector_lane_error,
            embedding_usage=embedding_usage,
            lane_note=vector_note,
        )

    # 3. Exclusions & Fences. A bad `known_at` raises the typed ValueError; the
    # boundary (`nmemory.tools.retrieve`) refuses it before this point.
    exclusions_count: dict[str, int] = defaultdict(int)
    belief_dt = (
        parse_rfc3339(params.known_at) if params.known_at is not None else now_dt
    )
    clock = FenceClock(
        now=now_dt,
        belief=belief_dt,
        as_of=belief_dt if params.known_at is not None else None,
        window_from=win_from_dt,
        window_to=win_to_dt,
    )
    excluded_kinds = (
        RECALL_EXCLUDED_KINDS if params.exclude_kinds is None else params.exclude_kinds
    )
    kind_excluded = (
        store.classifications.kind_ids_among(excluded_kinds, list(candidate_ids))
        if params.effort_set is None and params.topic_set is None
        else set()
    )
    # Both clocks in a single query over the entire pool, never one per capsule.
    fences = _Fences(
        params=params,
        clock=clock,
        verified_at=store.anchors.last_passing_verification_among(
            sorted(candidate_ids)
        ),
        last_read=(
            last_read_among(store.db, sorted(candidate_ids))
            if params.unread_days is not None
            else {}
        ),
        term_scores=term_score_map,
        vector_sims=vector_sim_map,
        term_ranks=term_rank_map,
        vector_ranks=vector_rank_map,
        term_floor=effective_term_floor(
            params.term_floor, params.min_term_margin, term_hits
        ),
        stop_only_term_ids=stop_only_ids,
    )

    eligible: list[Ranked] = []
    for cid in candidate_ids:
        if cid in kind_excluded:
            exclusions_count["kind_excluded"] += 1
            continue
        sc = stored_map.get(cid)
        if not sc:
            continue
        verdict = _fence(store, fences, sc)
        if isinstance(verdict, str):
            exclusions_count[verdict] += 1
        else:
            eligible.append(verdict)

    # THE STANDING FENCE (U1), in batch and after the loop: `standing_of` makes
    # ~3 roundtrips PER CAPSULE, so only survivors pay for it, and a stronger
    # fence keeps its NAME. BEFORE the vector cap: the cap is budget, this is a
    # statement about the capsule's truth. `STANDING_EXEMPT_KINDS` never enters
    # the dictionary `standing_of` returns. HIDDEN, never deleted: `lapsed=True`
    # returns it to service. `belief_dt` is the clock of the currency fences.
    if eligible and not params.lapsed:
        standing = standing_of(
            store.db, [sc.id for sc, _, _ in eligible], now=belief_dt
        )
        surviving: list[Ranked] = []
        for item in eligible:
            if standing.get(item[0].id, {}).get("state") == "caducou":
                exclusions_count["lapsed"] += 1
            else:
                surviving.append(item)
        eligible = surviving

    active_term_ranks = {
        cid: r
        for cid, r in term_rank_map.items()
        if cid not in stop_only_ids
        or (
            fences.term_floor > 0.0
            and term_score_map.get(cid, 0.0) >= fences.term_floor
        )
    }
    if (
        count
        and term_hits
        and not any(sc.id in active_term_ranks for sc, _, _ in eligible)
    ):
        # Term lane matched raw content but every term candidate was fenced out:
        # the term-lane observation records missing_evidence even when the vector
        # lane grounds the response.
        _record_term_lane_miss(
            store,
            groups,
            "missing_evidence",
            now_dt,
            call_id=call_id,
            lane="term",
            project_id=params.project_id,
            project_prefix=params.project_prefix,
        )

    before_margin = eligible
    eligible, below_margin = cut_vector_margin(eligible, fences, params.vector_margin)
    if below_margin:
        kept_ids = {sc.id for sc, _, _ in eligible}
        for sc, _, _ in before_margin:
            if sc.id in kept_ids:
                continue
            if sc.id in stop_only_ids:
                exclusions_count["below_relevance_floor"] += 1
            else:
                exclusions_count["below_vector_margin"] += 1
    if vector_rank_map:
        eligible, capped = cap_vector_lane(
            eligible, active_term_ranks, vector_rank_map, v_limit
        )
        if capped:
            exclusions_count["vector_k_capped"] += capped

    if not eligible:
        return build_envelope(
            "missing_evidence",
            lane=lane_pref,
            lane_used=lane_used(term_rank_map, vector_rank_map),
            lanes_run=lanes_run,
            exclusions=exclusions_count,
            total_candidates=len(candidate_ids),
            lane_error=vector_lane_error,
            embedding_usage=embedding_usage,
            lane_note=vector_note,
        )

    w_blend = params.weight_blend or 0.0
    if w_blend > 0.0:
        eligible = [
            (
                sc,
                dec_w,
                base_rel
                * blend_multiplier(w_blend, store.traffic.get_feedback_weight(sc.id)),
            )
            for sc, dec_w, base_rel in eligible
        ]

    # Relevance descending, then seq ascending; trim by limit, then by budget.
    eligible.sort(key=lambda x: (-x[2], x[0].seq))
    token_budget = params.token_budget if params.token_budget is not None else 1500
    top_items, trimmed_by_limit, trimmed_by_budget = trim_to_budget(
        eligible, params.limit or 10, token_budget
    )

    evidence: list[EvidenceItem] = []
    for item in top_items:
        if count:
            store.traffic.record_recall(item[0].id, now_dt)
        evidence.append(
            _evidence_item(
                store,
                item,
                groups,
                group_aliases,
                params.anchor_root or "",
                term_score_map,
            )
        )

    receipt_id: str | None = None
    if record_receipt and count:
        receipt_id = store.substrate.record_receipt(
            terms=query,
            returned_ids=[e.id for e in evidence],
            project_id=params.project_id,
            now=now_dt,
            session_id=asked_by_session,
            project_prefix=params.project_prefix,
        ).id

    # B2 — the counters are DERIVED from the engine, never served as literals.
    return build_envelope(
        "grounded",
        lane=lane_pref,
        lane_used=lane_used(term_rank_map, vector_rank_map),
        lanes_run=lanes_run,
        exclusions=exclusions_count,
        total_candidates=len(candidate_ids),
        evidence=[e.to_dict() for e in evidence],
        receipt_id=receipt_id,
        trim={
            "token_budget": token_budget,
            "matched": len(eligible),
            "returned": len(top_items),
            "trimmed": trimmed_by_limit + trimmed_by_budget,
            "trimmed_by_limit": trimmed_by_limit,
            "trimmed_by_budget": trimmed_by_budget,
        },
        lane_error=vector_lane_error,
        embedding_usage=embedding_usage,
        lane_note=vector_note,
    )
