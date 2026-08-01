//! # Retrieve — routed term/vector recall, grounded-or-ABSTAIN, evidence envelope
//! (unit s4).
//!
//! The engine half of the LLM-first recall contract (`ARCHITECTURE.md` §0):
//! the CALLER is the intelligent half and arrives with an already-expanded
//! multi-term query (synonyms, aliases, rephrasings) and, when requested, a
//! caller-computed query embedding. This module does deterministic local term
//! and caller-fed vector matching: the term lane uses FTS5 `OR` across quoted
//! terms, the vector lane uses positive cosine similarity, and fused recall
//! combines their independent ranks. It returns few, dense, layered results
//! under an explicit token budget. There is no embedder, no network, and no
//! clock read: `now` is injected at the surface boundary, exactly like the
//! store's `created_at`.
//!
//! ## Grounded, missing evidence, or ABSTAIN — never fabricate (W1 tri-state)
//!
//! A query resolves to exactly one honest outcome:
//!
//! - [`RetrieveResponse::Grounded`] — at least one eligible capsule from
//!   an executed lane survives every fence; unchanged shape.
//! - [`RetrieveResponse::MissingEvidence`] — an executed lane DID match
//!   stored capsules (or a term named a forgotten one — below), but every match was
//!   excluded by an eligibility fence (quarantined, falsified, archived,
//!   superseded, expired, not-yet-valid, outside the requested fact-time
//!   window, undated under that window, or tombstoned — counted per
//!   [`ExclusionReason`]): evidence exists (or existed), none of it may
//!   ground recall.
//! - [`RetrieveResponse::Abstain`] — zero raw matches in the executed
//!   lanes and the lane-independent tombstone id probe: nothing to ground
//!   and nothing to exclude.
//!
//! Nothing is ever invented. (Donor B's `RecallMode` names the SAME three
//! states with the ungrounded pair swapped — there `missing_evidence`
//! meant no-candidate-at-all and `abstain` meant candidates-not-usable.
//! The mapping above keeps the wire value `abstain` exactly where the h2
//! conformance pin holds it — a query matching nothing abstains — and
//! reserves `missing_evidence` for the genuinely new state: matches
//! existed and every one was fenced out.)
//!
//! ## Tombstoned capsules: the forgotten-id probe (W1 forget)
//!
//! A forgotten capsule ([`Store::forget_capsule`]) has NO content bytes
//! left anywhere — its FTS mirror row is emptied in the forget
//! transaction, so its former content can never lexically match a query
//! again. That is forget working, not a reporting gap. The one honest
//! channel left is the id: a query TERM that exactly names a tombstoned
//! capsule id (`"cap-<n>"`) counts as a raw match excluded as
//! [`ExclusionReason::Tombstoned`] — so an agent that remembered an id
//! and asks again learns "forgotten (marker via `get`)" instead of a
//! false "never existed" abstain. Live capsule ids get no such probe:
//! live content matches lexically or not at all.
//!
//! ## Superseded capsules are excluded by default (h4)
//!
//! A capsule marked superseded (store sidecar, [`Store::is_superseded`])
//! never grounds recall: the live successor speaks — replace-over-append
//! discipline. The old record is excluded, not erased: `get`/`list` still
//! return it (the audit path), and the `missing_evidence` outcome counts
//! it when the exclusion emptied the result.
//!
//! ## The evidence envelope (injection armor)
//!
//! Recalled content lands in a prompt, so every result is wrapped as DATA
//! ([`Evidence`]): the literal label [`ADVISORY_NOT_AUTHORITY`] and the
//! `DATA` framing are UNFORGEABLE zero-sized fields — they serialize on
//! every item and cannot be constructed with any other value — next to the
//! capsule's own `instruction_taint` flag, provenance, freshness, authority
//! class, confidence, and lane-specific match explain: `matched_terms`,
//! normalized `relevance`, and rounded `bm25` when the term lane matched;
//! `vector_similarity` when the vector lane matched; and `fusion_rank` when
//! vector-bearing ranking ran.
//! Stored content is never rendered as directives and never inlined whole:
//! the envelope carries only a `headline` (first line, at most
//! [`HEADLINE_MAX_CHARS`] chars); the full capsule stays one `get` away
//! (layered recall).
//!
//! ## Determinism (PLAN s4 tiebreak + h4 usage late key + w2 decay)
//!
//! Ranking is a pure function of stored fields plus the injected `now`, with
//! one explicit contract per effective lane. Term-only ranking uses coverage
//! descending, then bm25 ascending (SQLite's smaller-is-better negative
//! score), advisory decayed weight descending, `freshness.valid_from`
//! descending, usage recency and count descending as late full-tie keys, then
//! numeric `seq` ascending. Forced-vector ranking uses one-lane RRF over cosine
//! rank (cosine descending, `seq` ascending). Fused ranking uses two-lane RRF
//! over the independent term and vector ranks: the term input uses the full
//! term-only comparator, the vector input uses cosine rank, their reciprocal
//! ranks sum, and a fused tie uses `seq` ascending. `fusion_rank` records this
//! pre-blend RRF position. An enabled feedback-weight blend may reorder only
//! after the selected base ranking; it never changes lane membership or
//! eligibility.
//!
//! Usage is a tiebreak input in the term rank and nothing more: it NEVER
//! touches confidence or authority (ARCHITECTURE §1 law: usage is not success
//! evidence), and no envelope field carries it. `now` decides WHICH capsules
//! are currently valid and feeds the term-rank decay ages — nothing else: the
//! same store state queried at the same `now` returns byte-identical JSON (the
//! deliberate exceptions are `anchor_live` and `anchor_drift`, which read the
//! live filesystem — their sections below).
//! Returning results IS a store write, though: every returned id is
//! counted ([`Store::record_recall`] at the injected `now`), so a
//! repeated query may re-order exact ties — that is the late key doing
//! its job.
//!
//! ## Advisory confidence decay (w2-recall2)
//!
//! The decay tiebreak key is `confidence × 2^(-age_days /`
//! [`DECAY_HALF_LIFE_DAYS`]`)`, age measured from `freshness.valid_from`
//! to the injected `now` — `valid_from` rather than the mechanical
//! append instant, so a caller can recompute the weight from envelope
//! fields alone. Decay is ADVISORY and ranking-only, by law: it NEVER
//! mutates the stored confidence (the envelope carries both —
//! `confidence` verbatim, `decayed_weight` rounded to 2 decimals) and
//! NEVER gates matching (an ancient capsule still matches, grounds, and
//! returns; it merely ranks below fresher same-score evidence).
//!
//! ## Synonym expansion (w2-recall2, store-fed)
//!
//! The caller stays the intelligent half, and the store remembers what
//! the caller taught it: each query term expands into an OR-group of the
//! term plus its recorded aliases (`aliases_for`, the w2-store2
//! caller-fed synonym sidecar — derived, rebuildable, never authority).
//! A capsule matched only via an alias attributes in `matched_terms` as
//! `alias:<term>` (the CALLER's term, so the explain maps back to the
//! question actually asked); a direct match attributes as the plain term
//! and subsumes its aliases. Term coverage counts GROUPS, not raw
//! strings — an alias hit advances its group exactly like a direct hit.
//! The forgotten-id probe stays on the caller's literal terms: an alias
//! is store-derived data, not the caller naming an id.
//!
//! ## Anchor liveness (w2-recall2)
//!
//! Every envelope reports whether its `provenance.anchor` still points
//! at something real: for `path:line`-shaped anchors (everything after
//! the LAST `:` all ASCII digits; earlier colons belong to the path) the
//! engine does a cheap symlink-refusing existence probe of the path
//! resolved against the boot-injected anchor root (the server
//! boundary's [`crate::server::BoundaryConfig::anchor_root`], resolved
//! at boot as `NMEMORY_ANCHOR_ROOT` > the boot cwd; tests inject a
//! hermetic temp root) — `anchor_live` is `true` (the path
//! exists — file OR directory; the line itself is never verified),
//! `false` (missing, or ANY symlink component — the probe never follows
//! links, so a repo-internal symlink can never existence-probe outside
//! the root; fail-closed, v3 fence), or `"unknown"` (an io error, a
//! non-`path:line` anchor, or a path the fence rejects: absolute /
//! `..`-traversing, which never leaves the root). The probe reads
//! metadata only, never content; it never panics and never blocks recall
//! — every failure degrades to `"unknown"`. Together with `anchor_drift`
//! (below) these are the only envelope fields read from the live
//! filesystem rather than the store (the deliberate byte-determinism
//! exceptions above).
//!
//! ## Anchor drift (u-r2)
//!
//! Existence is not integrity: a live anchor may point at a file whose
//! CONTENT changed since capture. Beside `anchor_live`, every envelope
//! carries `anchor_drift`, the closed tri-state `"unchanged"` |
//! `"drifted"` | `"unknown"`: the anchored file is re-hashed through the
//! SAME fail-closed root fence the liveness probe uses
//! ([`anchor_content_hash`] — symlink components, out-of-root paths, and
//! non-`path:line` anchors never resolve) and compared against the
//! CAPTURE-TIME hash the boundary recorded in the `anchor_hashes` sidecar
//! ([`Store::anchor_hash_of`]). `provenance.source_hash` cannot serve
//! here — it hashes the capsule's own content bytes (the ingest
//! idempotency key), never the anchored file. `"unknown"` is the honest
//! verdict whenever EITHER hash is unavailable: a non-path anchor, a
//! fence-rejected path, a symlink, a missing/unreadable file, or a
//! capsule with no recorded capture hash — never a guess. Advisory
//! explain data, never authority, never a gate.
//!
//! ## Epistemic sidecar on the envelope (u-r2)
//!
//! When a capsule carries epistemic annotations
//! ([`Store::epistemics_of`]) the envelope surfaces them —
//! `evidence_state` (closed set `observed` / `inferred` / `unverified`),
//! `proof_hint`, and `stale_if` — each omitted when absent (the q109/q91
//! row-flag idiom). `proof_hint` and `stale_if` are ADVISORY STRINGS
//! surfaced verbatim: no code path executes or evaluates them, ever.
//!
//! ## Lifecycle tier fence (w2-recall2)
//!
//! Capsules tiered `archived` or `quarantined` (w2-store2 sidecar,
//! `get_tier`, default `active`) are excluded from grounding by default
//! — counted per [`ExclusionReason`] exactly like the other fences
//! (`{"archived": n, "quarantined": n}` in `excluded`), and still
//! reachable via `get`/`list`: a tier retires evidence from recall, it
//! never hides bytes. Fence order (the first fence wins the count) is a
//! documented dominance LAW (w2-fix, u6h-extended): quarantined →
//! FALSIFIED → archived → superseded → currency. Quarantine dominates
//! everything — the taint signal must never disappear (the consolidation
//! planner's own rule, now mirrored on the recall surface: superseding a
//! quarantined capsule no longer launders its `excluded` bucket into
//! `superseded`). Archived dominates superseded — the planner archives
//! ONLY superseded records, so this is what makes `apply_tiers` observable
//! on recall at all.
//!
//! ## Falsified fence (u6h)
//!
//! A capsule named as the target (`to_id`) of a `falsifies` edge
//! ([`Store::is_falsified`]) is excluded from grounding: an observed
//! outcome (an `out-<n>` record) — or another capsule — contradicts the
//! claim, so recall stops speaking it. This is ELIGIBILITY, never history:
//! the bytes are untouched and `get`/`list` still serve the capsule (unlike
//! forget, which destroys content). It sits SECOND in the dominance law,
//! above archived and superseded — a falsified fact must never hide behind
//! a softer lifecycle bucket — and only quarantine (the taint signal)
//! outranks it. An outcome record alone never triggers this fence: only the
//! explicit `falsifies` edge does (the u6h self-attest guard).
//!
//! ## w2-store2 contract seam
//!
//! Recall consumes the store through the module-private `RecallStore`
//! trait — the W1 surface plus the two w2-store2 contract calls
//! (`aliases_for`, `get_tier`). Because this unit's base predates
//! store2, the `impl RecallStore for Store` bodies for those two are the
//! store2 DEFAULTS (no alias recorded; every capsule active) — honest
//! empty-sidecar semantics, not stubs; the marked integration point in
//! that impl swaps them to real delegation when store2 lands.

use std::cmp::Ordering;
use std::collections::{BTreeMap, HashMap, HashSet};
use std::path::{Component, Path};

use serde::Serialize;
use time::OffsetDateTime;

use crate::capsule::{AuthorityClass, Confidence, Freshness, Provenance, sha256_hex};
use crate::store::{
    CapsuleId, CorroborationSummary, EpistemicsRecord, EventTimeRecord, FEEDBACK_NEUTRAL_WEIGHT,
    LaneOverride, RecallMissOutcome, Store, StoreError, StoredCapsule, StoredEmbedding,
    TombstoneRecord, UsageStat, fold_diacritic,
};

/// The literal advisory label carried by every recall result: recall
/// locates evidence, it never closes or influences an outcome.
pub const ADVISORY_NOT_AUTHORITY: &str = "ADVISORY_NOT_AUTHORITY";

/// Default token budget for a response's result list when the caller does
/// not pass one — sized so a handful of compact envelopes fit without
/// flooding the caller's working context (tokens approximated as
/// `chars / 4`).
pub const DEFAULT_TOKEN_BUDGET: usize = 1500;

/// Maximum headline length in chars (first line of content, truncated
/// with `…` when the line is longer or more content follows).
pub const HEADLINE_MAX_CHARS: usize = 140;

/// Half-life, in days, of the ADVISORY confidence-decay tiebreak key
/// (module doc): `decayed_weight = confidence × 2^(-age_days / 90)`,
/// age measured from `freshness.valid_from` to the injected query
/// instant. 90 days ≈ one quarter: a capsule loses half its rank boost
/// per quarter of age. Ranking-only by law — it never mutates the
/// stored confidence and never gates matching.
pub const DECAY_HALF_LIFE_DAYS: f64 = 90.0;

/// Reciprocal-rank-fusion constant (w3 u6a): each lane contributes
/// `1 / (RRF_K + rank)` (1-based rank) to a candidate's fused score, summed
/// across the lanes that ranked it. The canonical value from Cormack,
/// Clarke & Büttcher (2009) — large enough that raw score magnitudes never
/// dominate rank order (fusion is rank-based, not score-based), so the FTS
/// bm25 scale and the cosine scale combine on equal footing. Fixed and
/// documented so fusion is deterministic: same lane ranks in → same fused
/// order out.
pub const RRF_K: f64 = 60.0;

/// S6 neutral corroboration weight — the midpoint a capsule ranks at when no
/// git-witness corroboration is recorded (or the corroboration blend is
/// dormant). `1.0` is fully corroborated, `0.0` fully drifted; `0.5` asserts
/// nothing either way, so the blend factor `(1 + corroboration_blend × (c −
/// CORROBORATION_NEUTRAL_WEIGHT))` is exactly `1.0` (a no-op) at neutral.
const CORROBORATION_NEUTRAL_WEIGHT: f64 = 0.5;

/// Default cap on the vector lane's candidate count (w3 u6a): when lane
/// routing executes the vector lane and no explicit `vector_k` is supplied,
/// the top-`DEFAULT_VECTOR_K` eligible capsules by cosine similarity enter
/// ranking. An explicit fact-time window is evaluated before this cap; with
/// no window the historical raw cosine top-K path stays unchanged. Bounds the
/// vector lane's reach the way `limit`/`token_budget` bound the returned set;
/// the caller widens it via `vector_k`.
pub const DEFAULT_VECTOR_K: usize = 10;

/// Zero-sized field that always serializes as the literal
/// [`ADVISORY_NOT_AUTHORITY`] — the label cannot be forged, altered, or
/// omitted on any [`Evidence`] value.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct AdvisoryLabel;

impl Serialize for AdvisoryLabel {
    fn serialize<S>(&self, serializer: S) -> Result<S::Ok, S::Error>
    where
        S: serde::Serializer,
    {
        serializer.serialize_str(ADVISORY_NOT_AUTHORITY)
    }
}

/// Zero-sized field that always serializes as `"DATA"` — every result is
/// framed as recalled data/evidence, never as instructions.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct DataFraming;

impl Serialize for DataFraming {
    fn serialize<S>(&self, serializer: S) -> Result<S::Ok, S::Error>
    where
        S: serde::Serializer,
    {
        serializer.serialize_str("DATA")
    }
}

/// Liveness of a `path:line` anchor at recall time (module doc): a
/// cheap symlink-refusing existence probe against the caller-injected
/// anchor root (symlinks are never followed — fail-closed `false`).
/// Wire form is the documented tri-state: `true` | `false` | `"unknown"`.
/// Advisory explain data — never authority, never a gate.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AnchorLive {
    /// The anchored path exists under the root (existence only — the
    /// line itself is never verified). Wire: `true`.
    Live,
    /// The anchored path does not exist under the root. Wire: `false`.
    Missing,
    /// Not a `path:line` anchor, a fence-rejected path (absolute or
    /// `..`-traversing), or an io error — never a guess. Wire:
    /// `"unknown"`.
    Unknown,
}

impl Serialize for AnchorLive {
    fn serialize<S>(&self, serializer: S) -> Result<S::Ok, S::Error>
    where
        S: serde::Serializer,
    {
        match self {
            AnchorLive::Live => serializer.serialize_bool(true),
            AnchorLive::Missing => serializer.serialize_bool(false),
            AnchorLive::Unknown => serializer.serialize_str("unknown"),
        }
    }
}

/// Content drift of a `path:line` anchor at recall time (module doc:
/// Anchor drift): the anchored file re-hashed through the same fail-closed
/// root fence as [`AnchorLive`] and compared against the capture-time
/// hash in the `anchor_hashes` sidecar. Wire form is the closed
/// tri-state string `"unchanged"` | `"drifted"` | `"unknown"`. Advisory
/// explain data — never authority, never a gate.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum AnchorDrift {
    /// The anchored file's bytes hash to the capture-time hash — the
    /// content the anchor grounded on is intact. Wire: `"unchanged"`.
    Unchanged,
    /// The anchored file exists but its bytes hash DIFFERENTLY from the
    /// capture-time hash — the grounding content changed since capture.
    /// Wire: `"drifted"`.
    Drifted,
    /// No comparison was possible: a non-`path:line` anchor, a
    /// fence-rejected path (absolute / `..`-traversing), a symlink
    /// component, a missing or unreadable file, or no capture-time hash
    /// recorded — never a guess. Wire: `"unknown"`.
    Unknown,
}

/// Why an eligibility fence excluded a lexically-matched capsule from
/// recall (the W1 tri-state plumbing behind
/// [`RetrieveResponse::MissingEvidence`]). Every excluded capsule is
/// counted under exactly ONE reason — the first fence that caught it, in
/// the variant order below, which is also the deterministic wire order of
/// the `excluded` map (`BTreeMap` over this `Ord`).
///
/// Extensibility contract (u6h realized it): a new eligibility fence is ONE
/// new variant here plus its classification arm in [`retrieve`] — the
/// response shape and its prose derive from the counts map and need no
/// change. (Tombstoned proved the contract with the forgotten-id probe;
/// `Falsified` proved it again with an `is_falsified` fence arm.)
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum ExclusionReason {
    /// Lifecycle tier `quarantined` (w2-store2 sidecar): suspect content
    /// fenced from grounding by default; still reachable via
    /// `get`/`list`. FIRST fence by design (w2-fix): the taint signal
    /// must never disappear — a quarantined capsule that is ALSO
    /// superseded or archived still reports `quarantined` (the same
    /// dominance law the consolidation planner enforces).
    Quarantined,
    /// Falsified by a `falsifies` edge (u6h, [`Store::is_falsified`]): an
    /// observed outcome (or a capsule) contradicts this claim, so it is
    /// fenced from grounding — its bytes untouched, still reachable via
    /// `get`/`list` (eligibility, never history). SECOND fence, ABOVE
    /// archived/superseded (u6h dominance): a falsified fact must never
    /// hide behind a softer lifecycle bucket — falsified+archived counts
    /// `falsified`, falsified+superseded counts `falsified`. Only quarantine
    /// (the taint signal) outranks it.
    Falsified,
    /// Lifecycle tier `archived` (w2-store2 sidecar): retired from
    /// grounding by default; still reachable via `get`/`list`. Above
    /// `superseded` (w2-fix): the planner archives only superseded
    /// records, so archive-then-recall must attribute the tier or
    /// applying tiers would have zero observable recall effect.
    Archived,
    /// Replaced via the h4 supersede chain; the live successor speaks.
    Superseded,
    /// The capsule's `valid_to` lies before the query instant `now`.
    Expired,
    /// The capsule's `valid_from` lies after the query instant `now`.
    NotYetValid,
    /// A declared event range does not intersect the caller's inclusive
    /// query window.
    OutsideTimeWindow,
    /// The caller supplied a time window but this capsule has no declared
    /// fact time. It cannot claim membership and is counted explicitly.
    Undated,
    /// A standing proposal (b2 staged review): the capsule carries review
    /// history whose latest verdict is not `ratified`, so it is fenced from
    /// grounding by default (`include_staged: true` includes it). The LAST
    /// eligibility fence before grounding — every quality fence (quarantined,
    /// falsified, archived, superseded) and the currency/fact-time fences
    /// DOMINATE it: a capsule that is both reports the stronger reason. Its
    /// bytes are untouched, still reachable via `get`/`list`.
    Proposed,
    /// A query term named a forgotten capsule id (`Store::forget_capsule`)
    /// — only the marker remains, reachable via `get`; the content can
    /// never match or ground again (module doc: the forgotten-id probe).
    Tombstoned,
}

impl ExclusionReason {
    /// The wire name — the `excluded` map key AND the vocabulary inside
    /// the human-readable reason (one vocabulary, never two).
    #[must_use]
    pub const fn wire_name(self) -> &'static str {
        match self {
            ExclusionReason::Superseded => "superseded",
            ExclusionReason::Archived => "archived",
            ExclusionReason::Quarantined => "quarantined",
            ExclusionReason::Falsified => "falsified",
            ExclusionReason::Expired => "expired",
            ExclusionReason::NotYetValid => "not_yet_valid",
            ExclusionReason::OutsideTimeWindow => "outside_time_window",
            ExclusionReason::Undated => "undated",
            ExclusionReason::Proposed => "proposed",
            ExclusionReason::Tombstoned => "tombstoned",
        }
    }
}

impl Serialize for ExclusionReason {
    fn serialize<S>(&self, serializer: S) -> Result<S::Ok, S::Error>
    where
        S: serde::Serializer,
    {
        // Plain-string serialization keeps the variant valid as a JSON
        // map key and single-sources the vocabulary in `wire_name`.
        serializer.serialize_str(self.wire_name())
    }
}

// Lifecycle tier of a capsule — the w2-store2 contract enum (`Active` is
// the default; `Archived`/`Quarantined` are fenced from grounding).
use crate::store::Tier;

/// The store surface recall consumes (module doc: w2-store2 contract
/// seam) — the W1 calls plus the two w2-store2 sidecar reads. Private on
/// purpose: the crate API stays [`retrieve`]`(&mut Store, …)`; the trait
/// exists so the engine is coded against the store2 CONTRACT while this
/// base predates it, and so tests can drive the full pipeline with
/// contract-true sidecar data.
trait RecallStore {
    /// [`Store::search_fts_effort`] (`project_id` + w2 `project_prefix` +
    /// `session_id` fences AND-compose, plus the S3 effort membership fence).
    /// `effort_ids` is `None` on every dormant (effort-free) recall, keeping
    /// the term lane byte-identical to the pre-S3 engine.
    fn search_fts(
        &self,
        terms: &[String],
        project_id: Option<&str>,
        project_prefix: Option<&str>,
        session_id: Option<&str>,
        effort_ids: Option<&[String]>,
    ) -> Result<Vec<(StoredCapsule, f64)>, StoreError>;
    /// [`Store::get_tombstone`].
    fn get_tombstone(&self, id: &str) -> Result<Option<TombstoneRecord>, StoreError>;
    /// [`Store::get_tombstone_for_session_label`].
    fn get_tombstone_for_session_label(
        &self,
        id: &str,
        session_id: &str,
    ) -> Result<Option<TombstoneRecord>, StoreError>;
    /// [`Store::is_superseded`].
    fn is_superseded(&self, id: &str) -> Result<bool, StoreError>;
    /// [`Store::is_falsified`] (u6h): whether a `falsifies` edge names `id`
    /// as target — the eligibility fence between quarantine and archive.
    fn is_falsified(&self, id: &str) -> Result<bool, StoreError>;
    /// [`Store::is_pinned`] (S1): whether `id`'s latest pin event set it
    /// pinned. Read on the decay KEY only — a pinned capsule ranks by full
    /// confidence (decay exempted at the call site), NEVER an eligibility
    /// fence (the fences above run byte-unchanged).
    fn is_pinned(&self, id: &str) -> Result<bool, StoreError>;
    /// [`Store::usage_of`].
    fn usage_of(&self, id: &str) -> Result<Option<UsageStat>, StoreError>;
    /// [`Store::record_recall`].
    fn record_recall(&mut self, ids: &[&str], now: OffsetDateTime) -> Result<(), StoreError>;
    /// w2-store2 contract: the recorded aliases of `term` (normalization
    /// — lowercase + diacritic fold — is the store's job on both write
    /// and lookup). Derived, rebuildable, never authority.
    fn aliases_for(&self, term: &str) -> Result<Vec<String>, StoreError>;
    /// w2-store2 contract: the capsule's lifecycle tier, [`Tier::Active`]
    /// when none was ever set.
    fn get_tier(&self, id: &str) -> Result<Tier, StoreError>;
    /// w3 u6a contract: the vector-lane candidate source —
    /// [`Store::embeddings_for_recall`]. Every LIVE capsule carrying an
    /// embedding under the scope fences, paired with its decoded vector;
    /// the eligibility fences (tier/superseded/currency) are applied by the
    /// engine, IDENTICALLY to both lanes. Empty when no embedding is stored
    /// (the dormant default — every store predating u6a).
    fn embeddings_for_recall(
        &self,
        project_id: Option<&str>,
        project_prefix: Option<&str>,
        session_id: Option<&str>,
        effort_ids: Option<&[String]>,
    ) -> Result<Vec<(StoredCapsule, StoredEmbedding)>, StoreError>;
    /// u-r2 contract: the capture-time anchored-file hash —
    /// [`Store::anchor_hash_of`]. `None` (every capsule the boundary could
    /// not hash at capture) degrades `anchor_drift` to `"unknown"`.
    fn anchor_hash_of(&self, id: &str) -> Result<Option<String>, StoreError>;
    /// u-r2 contract: the epistemic sidecar — [`Store::epistemics_of`].
    /// `None` (never annotated) omits the envelope's epistemic fields.
    fn epistemics_of(&self, id: &str) -> Result<Option<EpistemicsRecord>, StoreError>;
    /// u04 advisory scored-outcome weight. Called ONLY when `weight_blend`
    /// is greater than zero; the dormant path performs zero sidecar reads.
    fn feedback_weight_of(&self, id: &str) -> Result<Option<f64>, StoreError>;
    /// u06 caller-declared fact time. Called ONLY when the query carries a
    /// [`TimeWindow`]; the absent-window path performs zero sidecar reads.
    fn event_time_of(&self, id: &str) -> Result<Option<EventTimeRecord>, StoreError>;
    /// b2 staged review: whether `id` is fenced by a standing proposal (its
    /// latest review verdict is not `ratified`) — [`Store::review_fenced`].
    /// The LAST eligibility fence; a store with no proposals answers `false`
    /// for every candidate (byte-identical dormancy).
    fn review_fenced(&self, id: &str) -> Result<bool, StoreError>;
    /// b2 staged review: the standing review verdict of `id` when it carries
    /// review history ([`Store::review_verdict`]) — read ONLY to stamp an
    /// INCLUDED staged row's envelope, so the common (unfenced) path never
    /// calls it.
    fn review_verdict(&self, id: &str) -> Result<Option<String>, StoreError>;
    /// S2 git witness lane: the newest git corroboration of `id` —
    /// [`Store::latest_corroborations`]. `None` (never scanned) omits the
    /// envelope's `corroboration` field. Read per RETURNED row, like
    /// [`RecallStore::anchor_hash_of`]; a store-only SQL read, never a
    /// process spawn.
    fn latest_corroborations_of(
        &self,
        id: &str,
    ) -> Result<Option<CorroborationSummary>, StoreError>;
    /// S6 corroboration weight hook — the read-path seam for the git-witness
    /// ranking blend. Called ONLY when `corroboration_blend` is greater than
    /// zero; the dormant path performs zero corroboration-weight ranking
    /// reads. The independent [`RecallStore::latest_corroborations_of`]
    /// envelope explain still reads per returned row. `None` maps to the
    /// neutral [`CORROBORATION_NEUTRAL_WEIGHT`] in the blend, so the default
    /// (and every store without corroboration data) ranks byte-identically.
    /// `id` is the typed [`CapsuleId`] so an integrator can key S2's
    /// `latest_corroborations` directly. Infallible by contract: a
    /// corroboration lookup failure fails OPEN to neutral, never failing the
    /// whole recall.
    fn corroboration_weight(&self, _id: &CapsuleId) -> Option<f64> {
        None
    }
}

impl RecallStore for Store {
    // Pure delegation to the inherent methods.
    fn search_fts(
        &self,
        terms: &[String],
        project_id: Option<&str>,
        project_prefix: Option<&str>,
        session_id: Option<&str>,
        effort_ids: Option<&[String]>,
    ) -> Result<Vec<(StoredCapsule, f64)>, StoreError> {
        Store::search_fts_effort(
            self,
            terms,
            project_id,
            project_prefix,
            session_id,
            effort_ids,
        )
    }
    fn get_tombstone(&self, id: &str) -> Result<Option<TombstoneRecord>, StoreError> {
        Store::get_tombstone(self, id)
    }
    fn get_tombstone_for_session_label(
        &self,
        id: &str,
        session_id: &str,
    ) -> Result<Option<TombstoneRecord>, StoreError> {
        Store::get_tombstone_for_session_label(self, id, session_id)
    }
    fn is_superseded(&self, id: &str) -> Result<bool, StoreError> {
        Store::is_superseded(self, id)
    }
    fn is_falsified(&self, id: &str) -> Result<bool, StoreError> {
        Store::is_falsified(self, id)
    }
    fn is_pinned(&self, id: &str) -> Result<bool, StoreError> {
        Store::is_pinned(self, id)
    }
    fn usage_of(&self, id: &str) -> Result<Option<UsageStat>, StoreError> {
        Store::usage_of(self, id)
    }
    fn record_recall(&mut self, ids: &[&str], now: OffsetDateTime) -> Result<(), StoreError> {
        Store::record_recall(self, ids, now)
    }

    // w2-store2 sidecar reads: real delegation (integrated w2). The
    // real-Store end-to-end tests in this module are the tripwire —
    // they fail if these ever regress to the pre-store2 defaults.
    fn aliases_for(&self, term: &str) -> Result<Vec<String>, StoreError> {
        Store::aliases_for(self, term)
    }
    fn get_tier(&self, id: &str) -> Result<Tier, StoreError> {
        Store::get_tier(self, id)
    }
    fn embeddings_for_recall(
        &self,
        project_id: Option<&str>,
        project_prefix: Option<&str>,
        session_id: Option<&str>,
        effort_ids: Option<&[String]>,
    ) -> Result<Vec<(StoredCapsule, StoredEmbedding)>, StoreError> {
        Store::embeddings_for_recall_effort(
            self,
            project_id,
            project_prefix,
            session_id,
            effort_ids,
        )
    }

    // u-r2 sidecar reads: pure delegation, like every read above.
    fn anchor_hash_of(&self, id: &str) -> Result<Option<String>, StoreError> {
        Store::anchor_hash_of(self, id)
    }
    fn epistemics_of(&self, id: &str) -> Result<Option<EpistemicsRecord>, StoreError> {
        Store::epistemics_of(self, id)
    }
    fn feedback_weight_of(&self, id: &str) -> Result<Option<f64>, StoreError> {
        Store::feedback_weight_of(self, id)
    }
    fn event_time_of(&self, id: &str) -> Result<Option<EventTimeRecord>, StoreError> {
        Store::event_time_of(self, id)
    }
    fn review_fenced(&self, id: &str) -> Result<bool, StoreError> {
        Store::review_fenced(self, id)
    }
    fn review_verdict(&self, id: &str) -> Result<Option<String>, StoreError> {
        Store::review_verdict(self, id)
    }
    fn latest_corroborations_of(
        &self,
        id: &str,
    ) -> Result<Option<CorroborationSummary>, StoreError> {
        Store::latest_corroborations(self, id)
    }
    /// S6→S2 wiring: the git-witness ranking signal reads the capsule's
    /// newest `anchor_content` corroboration verdict — `corroborated` ranks
    /// at full weight (`1.0`), `drifted` at zero (`0.0`); a capsule never
    /// scanned (or carrying no `anchor_content` row) returns `None`, which the
    /// blend treats as neutral. Infallible by contract: a sidecar read error
    /// fails OPEN to neutral, never failing the whole recall.
    fn corroboration_weight(&self, id: &CapsuleId) -> Option<f64> {
        match Store::latest_corroborations(self, id.as_str()) {
            Ok(Some(summary)) => match summary.anchor_content.as_deref() {
                Some("corroborated") => Some(1.0),
                Some("drifted") => Some(0.0),
                _ => None,
            },
            Ok(None) => None,
            Err(_) => None,
        }
    }
}

/// A recall request. Terms are caller-expanded: the LLM brings its own
/// synonyms/aliases/rephrasings as separate terms; the engine matches
/// FTS5 `OR` across them — a multi-word term matches as the AND of its
/// words (order/adjacency-insensitive), never as FTS5 syntax. The
/// w2-store2 synonym sidecar additionally expands each term with its
/// recorded aliases (module doc: Synonym expansion).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Lane {
    /// Choose `term` without an embedding and `fused` with one.
    Auto,
    /// Run only the FTS term lane.
    Term,
    /// Run only the caller-fed vector lane.
    Vector,
    /// Run both lanes and combine their ranks with RRF.
    Fused,
}

impl Lane {
    const fn as_str(self) -> &'static str {
        match self {
            Lane::Auto => "auto",
            Lane::Term => "term",
            Lane::Vector => "vector",
            Lane::Fused => "fused",
        }
    }
}

/// An optional-bounds caller query window. The constructor makes the two
/// illegal states — no bounds and a backwards range — unrepresentable in a
/// [`RetrieveQuery`]. Intersection with declared event ranges is inclusive.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct TimeWindow {
    from: Option<OffsetDateTime>,
    to: Option<OffsetDateTime>,
}

impl TimeWindow {
    /// Construct a nonempty, forward query window.
    pub fn new(
        from: Option<OffsetDateTime>,
        to: Option<OffsetDateTime>,
    ) -> Result<Self, TimeWindowError> {
        if from.is_none() && to.is_none() {
            return Err(TimeWindowError::Empty);
        }
        if let (Some(from), Some(to)) = (from, to)
            && to < from
        {
            return Err(TimeWindowError::Backwards);
        }
        Ok(Self { from, to })
    }

    const fn from(&self) -> Option<OffsetDateTime> {
        self.from
    }

    const fn to(&self) -> Option<OffsetDateTime> {
        self.to
    }
}

/// Why a wire-valid timestamp pair cannot become a [`TimeWindow`].
#[derive(Debug, Clone, Copy, PartialEq, Eq, thiserror::Error)]
pub enum TimeWindowError {
    /// Both optional bounds were omitted.
    #[error("time_window needs at least one bound (from and/or to, RFC3339)")]
    Empty,
    /// The upper bound lies before the lower bound.
    #[error("time_window.to lies before time_window.from — a window runs forward")]
    Backwards,
}

/// S3 effort-lifecycle: the RESOLVED effort scope injected by the server
/// after it validates `effort_id` (exists → not tombstoned → persisted kind
/// `epic` → ≥ 1 member). The engine receives an ALREADY-VALID scope — every
/// teaching rejection (`unknown_capsule` / `tombstoned_capsule` / not-an-epic
/// / unclassified / zero-members) fires at the boundary before this is built,
/// so a degenerate fence can never reach recall. `None` on the query keeps the
/// entire engine byte-identical (dormancy).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EffortScope {
    /// The epic capsule id (`cap-<n>`) that names the effort.
    pub epic_id: String,
    /// The effort's members — the `from` side of every `part_of` edge INTO
    /// the epic, GRAPH TRUTH (dead members included). The SQL fence is this
    /// set ∪ `{epic_id}`; the echoed `member_total` is exactly its length,
    /// identical to digest/bootstrap (cross-surface parity).
    pub member_ids: Vec<String>,
    /// Whether the epic is OPEN (`¬witnessed ∧ ¬superseded`; tombstoned
    /// already refused at the boundary). A CLOSED effort stays queryable for
    /// post-mortem recall — this flag rides the echo as `open:false`.
    pub open: bool,
}

impl EffortScope {
    /// The SQL membership fence id-set: members ∪ {epic}, deterministic order.
    /// Built ONCE per recall and passed to both lanes.
    fn fence_ids(&self) -> Vec<String> {
        let mut ids = self.member_ids.clone();
        if !ids.iter().any(|id| id == &self.epic_id) {
            ids.push(self.epic_id.clone());
        }
        ids
    }

    /// Graph-truth membership size — the echoed `member_total`.
    fn member_total(&self) -> usize {
        self.member_ids.len()
    }
}

/// S3 effort-lifecycle: the per-response echo of the resolved effort scope,
/// so a caller sees WHICH effort fenced the recall (post-mortem included).
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct EffortEcho {
    /// The epic capsule id the fence resolved.
    pub epic_id: String,
    /// Graph-truth member count (dead members included), identical to
    /// digest/bootstrap.
    pub member_total: usize,
    /// Whether the epic is open; a closed effort answers `open:false` yet
    /// still grounds.
    pub open: bool,
}

/// S3 effort-lifecycle: a grounded row's relationship to the fencing effort.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum EffortRole {
    /// This row IS the effort's epic capsule.
    Epic,
    /// This row is a `part_of` member of the effort.
    Member,
}

/// topic-anchor s1: the RESOLVED topic scope injected by the server after it
/// validates `topic_id` (exists → not tombstoned). The engine receives an
/// ALREADY-VALID scope — both teaching rejections (`unknown_capsule`, which a
/// slug also lands on, and `tombstoned_capsule`) fire at the boundary before
/// this is built. `None` on the query keeps the entire engine byte-identical
/// (dormancy).
///
/// A topic needs NO further precondition: unlike an effort it has no
/// lifecycle to be open or closed, no persisted kind to check (the `doc`
/// convention is documentation, never a gate), and no zero-member rejection —
/// the fence always contains the topic node itself, so it is never degenerate.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TopicScope {
    /// The capsule id (`cap-<n>`) serving as the topic node.
    pub topic_id: String,
    /// The topic's members — the `from` side of every `about` edge INTO the
    /// topic, GRAPH TRUTH (dead members included). The SQL fence is this set ∪
    /// `{topic_id}`; the echoed `member_total` is exactly its length.
    pub member_ids: Vec<String>,
}

impl TopicScope {
    /// The SQL membership fence id-set: members ∪ {topic}, deterministic
    /// order. Built ONCE per recall, then AND-composed with any effort set.
    fn fence_ids(&self) -> Vec<String> {
        let mut ids = self.member_ids.clone();
        if !ids.iter().any(|id| id == &self.topic_id) {
            ids.push(self.topic_id.clone());
        }
        ids
    }

    /// Graph-truth membership size — the echoed `member_total`.
    fn member_total(&self) -> usize {
        self.member_ids.len()
    }
}

/// topic-anchor s1: the per-response echo of the resolved topic scope, so a
/// caller sees WHICH topic fenced the recall. It carries no `open` flag by
/// design: a topic has no lifecycle to report.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct TopicEcho {
    /// The topic capsule id the fence resolved.
    pub topic_id: String,
    /// Graph-truth member count (dead members included).
    pub member_total: usize,
}

/// topic-anchor s1: a grounded row's relationship to the fencing topic.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum TopicRole {
    /// This row IS the topic capsule.
    Topic,
    /// This row carries an `about` edge into the topic.
    Member,
}

/// One recall request: the caller-expanded terms plus every fence applied to
/// them.
///
/// FENCES AND-COMPOSE, so setting one can only narrow what grounds the answer,
/// never widen it, and a field left unset applies NO fence rather than a
/// permissive default. `terms` is the exception that keeps the empty case
/// honest: a query with no tokenizable term is refused with
/// [`RetrieveError::EmptyQuery`] instead of matching everything.
#[derive(Debug, Clone, Default)]
pub struct RetrieveQuery {
    /// Caller-expanded search terms. A term's words are AND-matched
    /// (each individually quoted for FTS5 — order- and
    /// adjacency-insensitive within the capsule); terms without a single
    /// alphanumeric character cannot tokenize and are dropped. No usable
    /// term at all → [`RetrieveError::EmptyQuery`]. Duplicate terms
    /// collapse (first occurrence wins).
    pub terms: Vec<String>,
    /// Project fence: when set, only capsules whose `scope.project_id`
    /// equals this ground the query.
    pub project_id: Option<String>,
    /// Scope-hierarchy fence (w2): only capsules whose
    /// `scope.project_id` equals this prefix exactly OR starts with the
    /// prefix plus `"/"` ground the query — `"nott"` covers `nott` and
    /// `nott/x`, never `nottx`. AND-composes with `project_id`.
    /// Character-exact ([`crate::store::ListFilter::project_prefix`]).
    pub project_prefix: Option<String>,
    /// Character-exact store-local capsule label fence. This is not a
    /// globally unique bracket identity and performs no `sessions` lookup:
    /// finished, orphaned, or merge-imported labels remain recallable.
    pub session_id: Option<String>,
    /// Optional inclusive fact-time fence. Applied after every state and
    /// currency fence, before ranking. Undated capsules do not claim
    /// membership: they are excluded and counted separately.
    pub time_window: Option<TimeWindow>,
    /// Maximum number of results. `None` = no count cap (the token
    /// budget is the real guard). `Some(0)` is honored literally: a
    /// count-only probe — grounded outcome with `matched` filled and
    /// zero envelopes.
    pub limit: Option<usize>,
    /// Token budget for the serialized result list, approximated as
    /// `chars / 4`; `None` = [`DEFAULT_TOKEN_BUDGET`]. With a NONZERO
    /// budget the top-ranked result is always returned even when it alone
    /// exceeds the budget (grounded means at least one envelope, unless
    /// `limit` forbids) — the documented floor of one; `Some(0)` is
    /// honored literally like `limit: 0`: a count-only probe, zero
    /// envelopes.
    pub token_budget: Option<usize>,
    /// Optional recall-lane selector. Omitted or [`Lane::Auto`] preserves
    /// the historical rule: term-only without `query_embedding`, fused
    /// with it. Explicit term/vector/fused requests override that choice.
    pub lane: Option<Lane>,
    /// w3 u6a caller-fed query vector. Under an omitted/auto lane, `None`
    /// preserves the historical FTS-only path and `Some(v)` selects fused
    /// recall. An explicit term lane never reads stored vectors even when
    /// this value is present; explicit vector/fused lanes require it. Any
    /// executed vector lane admits ONLY positively-similar
    /// embeddings (cosine > 0; fleet-8 c7: an orthogonal or
    /// anti-correlated embedding never solely-grounds a result). The
    /// embedding is caller-supplied (no embedder dependency; the store
    /// computes nothing); its dimension must match the stored embeddings'
    /// ([`RetrieveError::DimensionMismatch`]), and it must be
    /// non-empty/finite/non-zero
    /// ([`RetrieveError::InvalidQueryEmbedding`]).
    pub query_embedding: Option<Vec<f32>>,
    /// w3 u6a: cap on the vector lane's candidate count — the top
    /// `vector_k` eligible capsules by cosine feed vector-bearing ranking.
    /// An explicit fact-time window runs before this cap; without one the
    /// historical raw cosine top-K path is unchanged. `None` →
    /// [`DEFAULT_VECTOR_K`]. Ignored entirely when routing does not execute
    /// the vector lane. `Some(0)` yields an empty vector lane (a fused
    /// request then degenerates to the FTS order).
    pub vector_k: Option<usize>,
    /// Opt-in scored-outcome ranking blend in `0.0..=1.0`. Omitted or
    /// `0.0` is structurally DORMANT: ranking bytes stay identical and the
    /// feedback sidecar is never read. Above zero, the deterministic base
    /// rank `r` is re-scored as `1/(60+r) × (1 + blend × (weight−0.5))`,
    /// then sorted score-desc/sequence-asc. Ranking only: eligibility and
    /// the three honest outcomes are unchanged.
    pub weight_blend: Option<f64>,
    /// b2 staged review: when `false` (the default), a standing proposal
    /// (latest review verdict not `ratified`) is fenced from grounding and
    /// counted under `excluded{proposed}`. `true` INCLUDES fenced proposals,
    /// each carrying its `review_state` on the envelope. Dormant by default:
    /// a store with no proposals is byte-identical either way.
    pub include_staged: bool,
    /// S6 opt-in corroboration ranking blend in `0.0..=1.0`. Omitted or
    /// `0.0` is structurally DORMANT: ranking bytes stay identical and the
    /// corroboration-weight ranking seam is never read. The independent
    /// corroboration envelope explain still reads per returned row. Above
    /// zero, the per-capsule corroboration weight `c`
    /// ([`RecallStore::corroboration_weight`], a git-witness signal — `1.0`
    /// corroborated, `0.0` drifted, `0.5` neutral) enters the ONE blend factor
    /// beside `weight_blend`: `score = 1/(60+r) × (1 + weight_blend ×
    /// (w−0.5)) × (1 + corroboration_blend × (c−0.5))`, then a single
    /// re-sort. Ranking only: eligibility and the three honest outcomes are
    /// unchanged; stored confidence is untouched.
    pub corroboration_blend: Option<f64>,
    /// S3 effort-lifecycle: the RESOLVED effort scope. `None` (the default)
    /// is DORMANT — byte-identical to the pre-S3 engine, zero new reads.
    /// `Some(scope)` fences BOTH lanes to the effort's members ∪ {epic} (an
    /// AND-composed id-set, never a post-filter), echoes `effort{…}` on the
    /// outcome, stamps `effort_role` on each grounded row, and names the
    /// effort in the honest-empty fence label. SCOPE only — downstream
    /// eligibility is untouched, so a fenced-in dead member still surfaces
    /// under `excluded{…}`.
    pub effort: Option<EffortScope>,
    /// topic-anchor s1: the RESOLVED topic scope. `None` (the default) is
    /// DORMANT — byte-identical to the pre-topic engine, zero new reads.
    /// `Some(scope)` fences BOTH lanes to the topic's members ∪ {topic} (an
    /// AND-composed id-set, never a post-filter — INTERSECTED with the
    /// [`RetrieveQuery::effort`] set when both are present), echoes
    /// `topic{…}` on the outcome, stamps `topic_role` on each grounded row,
    /// and names the topic in the honest-empty fence label. SCOPE only —
    /// downstream eligibility is untouched, so a fenced-in dead member still
    /// surfaces under `excluded{…}`.
    pub topic: Option<TopicScope>,
}

/// One recall result wrapped as DATA — the evidence envelope
/// (`ARCHITECTURE.md` §1–2). Field declaration order IS the JSON order:
/// the armor (`label`, `framing`) reads first. The full content is NOT
/// here by design — fetch the capsule via `get` with `id` (layered
/// recall).
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Evidence {
    /// Always the literal `ADVISORY_NOT_AUTHORITY` (unforgeable).
    pub label: AdvisoryLabel,
    /// Always the literal `DATA` (unforgeable): evidence, not
    /// instructions.
    pub framing: DataFraming,
    /// Store id (`cap-<seq>`) — the `get` handle for the full capsule.
    pub id: CapsuleId,
    /// First line of the content, at most [`HEADLINE_MAX_CHARS`] chars,
    /// `…`-terminated when truncated or when more content follows.
    pub headline: String,
    /// The capsule's own taint flag: directive-shaped content may only
    /// ever ground as quoted/cited DATA.
    pub instruction_taint: bool,
    /// Who asserted the content (kebab-case on the wire).
    pub authority_class: AuthorityClass,
    /// Calibrated confidence in `0.0..=1.0` (serializes as a number).
    pub confidence: Confidence,
    /// ADVISORY decayed rank weight (module doc): `confidence ×
    /// 2^(-age_days /` [`DECAY_HALF_LIFE_DAYS`]`)`, age from
    /// `freshness.valid_from` to the query instant, rounded to 2
    /// decimals. Explain for the decay tiebreak — the stored
    /// `confidence` above is never mutated by it.
    pub decayed_weight: f64,
    /// Origin + anchor + source hash — recall is traceable or it is not
    /// returned.
    pub provenance: Provenance,
    /// Whether `provenance.anchor` still points at an existing path
    /// (module doc: Anchor liveness): `true` | `false` | `"unknown"`.
    /// Metadata-only, symlink-refusing probe against the boot-injected
    /// anchor root — advisory explain, never authority, never a gate.
    pub anchor_live: AnchorLive,
    /// Whether the anchored file's CONTENT still hashes to its
    /// capture-time hash (module doc: Anchor drift): `"unchanged"` |
    /// `"drifted"` | `"unknown"` — `"unknown"` whenever either hash is
    /// unavailable (non-path anchor, fence-rejected path, symlink,
    /// missing/unreadable file, or no recorded capture hash). Advisory
    /// explain, never authority, never a gate.
    pub anchor_drift: AnchorDrift,
    /// u-r2 epistemic sidecar: how this claim relates to observation —
    /// the closed set `"observed"` / `"inferred"` / `"unverified"`.
    /// Omitted when the capsule was never annotated (q109/q91 row-flag
    /// idiom).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub evidence_state: Option<String>,
    /// u-r2 epistemic sidecar: the command that re-proves this claim.
    /// ADVISORY STRING surfaced verbatim — no code path executes it,
    /// ever. Omitted when absent.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub proof_hint: Option<String>,
    /// u-r2 epistemic sidecar: the condition under which this claim
    /// expires. ADVISORY STRING surfaced verbatim — no code path
    /// evaluates it, ever. Omitted when absent.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub stale_if: Option<String>,
    /// b2 staged review: the standing review verdict (`"proposed"` /
    /// `"rejected"`) of an INCLUDED staged row — present ONLY when
    /// `include_staged` surfaced a fenced proposal, so a plain live row (no
    /// review history, or a ratified one) omits it and stays byte-identical.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub review_state: Option<String>,
    /// Validity window (RFC3339), for the caller's own staleness
    /// judgment.
    pub freshness: Freshness,
    /// Which of the caller's terms ground THIS result — explain
    /// re-derived with a tokenizer mirroring FTS5's `unicode61`
    /// (lowercase, split on non-alphanumeric, Latin diacritics folded, a
    /// multi-word term attributed when ALL its words appear). Residual
    /// deltas (non-Latin folding, CJK segmentation) may still leave a
    /// grounded row unattributed. Explain data, never authority.
    pub matched_terms: Vec<String>,
    /// Readable relative strength WITHIN this result set, in
    /// `0.0..=1.0`: the top-ranked hit is `1.0` and weaker hits shrink
    /// toward `0.0` (bm25 ratio against the top hit, 2 decimals).
    /// Comparable only across results of the SAME response, never
    /// across queries. Explain data, never authority — ranking orders
    /// by the raw scores, not this. FTS-lane explain: `Some` for any row
    /// the term lane matched, OMITTED for a vector-only match (no bm25 to
    /// normalize). A dormant (FTS-only) query always fills it, so its
    /// wire bytes are unchanged (`Some(x)` serializes identically to the
    /// former bare `x`).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub relevance: Option<f64>,
    /// The SQLite bm25 score behind `relevance`, rounded to 3
    /// significant digits for the wire (raw noise like
    /// `-0.000005541987962232948` was unreadable and token-wasteful):
    /// negative, and MORE negative = stronger match. Ranking still
    /// orders by the raw unrounded score. FTS-lane explain: `Some` for a
    /// term-matched row, OMITTED for a vector-only match (same dormant
    /// byte-identity as `relevance`).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub bm25: Option<f64>,
    /// w3 u6a vector-lane explain: cosine similarity of this capsule's
    /// stored embedding to the caller's `query_embedding`, rounded to 4
    /// decimals — present ONLY when the vector lane matched this row (a
    /// vector-only match grounds WITH this explain; a term-only row omits
    /// it). Always absent in a dormant query. Advisory explain, never
    /// authority — fusion orders by rank, not by this magnitude.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub vector_similarity: Option<f64>,
    /// w3 u6a rank explain: this row's 1-based position in the RRF ranking
    /// — present on EVERY returned row whenever the vector lane executes
    /// (one-lane RRF for forced vector, two-lane RRF for fused), absent when
    /// only the term lane executes. In fused recall, a row with
    /// `fusion_rank` but no `vector_similarity` matched only the term lane.
    /// The opt-in feedback blend may reorder the final list;
    /// `fusion_rank` keeps naming this PRE-blend RRF position. Advisory
    /// explain, never authority.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub fusion_rank: Option<usize>,
    /// u04 advisory scored-outcome weight, rounded to 2 decimals. Present
    /// only when `weight_blend > 0`; omitted on the byte-identical dormant
    /// path. Ranking explain only, never eligibility or authority.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub feedback_weight: Option<f64>,
    /// S2 git witness lane: the newest git corroboration of THIS row's
    /// anchors and mentions ([`crate::store::CorroborationSummary`]) — the
    /// derived explain from the `corroborations` sidecar, read per returned
    /// row like the anchor-drift hash. Omitted when the capsule was never
    /// scanned (the byte-identical dormant path). ADVISORY explain, never
    /// authority: a witness observes, it never mutates confidence or ranks.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub corroboration: Option<CorroborationWire>,
    /// S6 advisory corroboration weight, rounded to 2 decimals — the
    /// git-witness signal (`1.0` corroborated, `0.0` drifted, `0.5` neutral)
    /// that fed the ranking blend. Present ONLY when `corroboration_blend >
    /// 0`; omitted on the byte-identical dormant path. Ranking explain only,
    /// never eligibility or authority; stored confidence is untouched by it.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub corroboration_weight: Option<f64>,
    /// S3 effort-lifecycle: this row's role in the fencing effort —
    /// `"epic"` for the effort's epic capsule, `"member"` for a `part_of`
    /// member. Present ONLY when the query carried an `effort_id`; omitted on
    /// the byte-identical dormant path. Explain data, never authority.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub effort_role: Option<EffortRole>,
    /// topic-anchor s1: this row's role in the fencing topic — `"topic"` for
    /// the topic capsule itself, `"member"` for a row carrying an `about` edge
    /// into it. Present ONLY when the query carried a `topic_id`; omitted on
    /// the byte-identical dormant path. Explain data, never authority.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub topic_role: Option<TopicRole>,
}

/// The wire form of a capsule's newest git corroboration (S2 git witness
/// lane) — the retrieve envelope's `corroboration` explain. Field order IS
/// the JSON order. Every optional field is omitted when absent, so a capsule
/// probed for only some kinds shows only those (and a never-scanned capsule
/// omits the whole envelope). ADVISORY DATA, never authority.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct CorroborationWire {
    /// The witness source (currently only `"git"`).
    pub source: String,
    /// The scan `HEAD` the newest verdict was observed at, when recorded.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub git_ref: Option<String>,
    /// Latest `anchor_path` verdict (`corroborated` / `missing`).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub path: Option<String>,
    /// Latest `anchor_sha` verdict (`corroborated` / `missing`).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub sha: Option<String>,
    /// Latest `anchor_content` verdict (`corroborated` / `drifted`).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub content: Option<String>,
    /// How many commit mentions cite this capsule, when any.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub mentions: Option<usize>,
    /// The newest verdict instant (RFC3339).
    pub at: String,
}

impl From<CorroborationSummary> for CorroborationWire {
    fn from(summary: CorroborationSummary) -> Self {
        CorroborationWire {
            source: summary.source,
            git_ref: summary.git_ref,
            path: summary.anchor_path,
            sha: summary.anchor_sha,
            content: summary.anchor_content,
            mentions: (summary.mentions > 0).then_some(summary.mentions),
            at: summary.at,
        }
    }
}

/// The recall outcome — serializes cleanly to JSON (tag `outcome`, wire
/// values `grounded` / `missing_evidence` / `abstain`), for the s5
/// surface to return verbatim. `snake_case` here is byte-identical to the
/// former `kebab-case` for the single-word variants and makes the new one
/// exactly the documented `missing_evidence`.
#[derive(Debug, Clone, PartialEq, Serialize)]
#[serde(tag = "outcome", rename_all = "snake_case")]
pub enum RetrieveResponse {
    /// At least one capsule grounds the query.
    Grounded {
        /// Ranked evidence envelopes, best match first.
        results: Vec<Evidence>,
        /// Eligible matches from the executed lane(s), before limit/budget
        /// trimming.
        matched: usize,
        /// Envelopes actually returned (`results.len()`).
        returned: usize,
        /// Matches trimmed away by `limit` + token budget
        /// (`matched - returned`); the caller can narrow terms or raise
        /// the budget to see them.
        trimmed: usize,
        /// Of `trimmed`, how many the count `limit` cut.
        trimmed_by_limit: usize,
        /// Of `trimmed`, how many the token budget cut.
        trimmed_by_budget: usize,
        /// The effective token budget applied to the result list.
        token_budget: usize,
        /// Matches that ALSO occurred but were excluded by an eligibility
        /// fence (superseded / archived / quarantined / expired /
        /// not_yet_valid / outside_time_window / undated /
        /// tombstoned-id probe), by reason — present only when nonzero,
        /// so a grounded outcome no longer hides that ineligible evidence
        /// existed.
        #[serde(skip_serializing_if = "BTreeMap::is_empty")]
        excluded: BTreeMap<ExclusionReason, usize>,
        /// Persisted address of this grounded response's returned capsule ids.
        /// Minted by the PUBLIC [`retrieve`] wrapper on every grounded
        /// outcome, including a count-only response with zero returned
        /// envelopes. `None` exists only on the engine-direct test seam, so
        /// engine differentials keep their pre-receipt bytes.
        #[serde(skip_serializing_if = "Option::is_none")]
        receipt_id: Option<String>,
        /// S3 effort-lifecycle: the resolved effort scope echo, present ONLY
        /// when the query carried an `effort_id` (omitted on the dormant
        /// path). A closed effort still grounds, echoing `open:false`.
        #[serde(skip_serializing_if = "Option::is_none")]
        effort: Option<EffortEcho>,
        /// topic-anchor s1: the resolved topic scope echo, present ONLY when
        /// the query carried a `topic_id` (omitted on the dormant path).
        #[serde(skip_serializing_if = "Option::is_none")]
        topic: Option<TopicEcho>,
    },
    /// One or more executed lanes (or the tombstone id probe) DID match
    /// stored capsules, but every match was excluded by an eligibility
    /// fence — evidence exists, none of it may ground
    /// recall. Distinct from [`RetrieveResponse::Abstain`]: the caller
    /// learns that relevant-but-ineligible capsules exist (reachable via
    /// `get`/`list`) while not one excluded byte reaches the response.
    MissingEvidence {
        /// Raw matches excluded across executed lanes plus terms naming a
        /// forgotten id (equals the sum over `excluded`).
        excluded_count: usize,
        /// Exclusion breakdown by reason, e.g. `{"superseded": 2}`.
        /// Deterministic key order: [`ExclusionReason`] variant order.
        excluded: BTreeMap<ExclusionReason, usize>,
        /// Honest human-readable account — counts per reason plus the
        /// `get`/`list` escape hatch. Pure function of the counts.
        reason: String,
        /// S3 effort-lifecycle: the resolved effort scope echo, present ONLY
        /// when the query carried an `effort_id`. The fenced-in dead members
        /// that produced this outcome are still named per-reason in `excluded`.
        #[serde(skip_serializing_if = "Option::is_none")]
        effort: Option<EffortEcho>,
        /// topic-anchor s1: the resolved topic scope echo, present ONLY when
        /// the query carried a `topic_id`. The fenced-in dead members that
        /// produced this outcome are still named per-reason in `excluded`.
        #[serde(skip_serializing_if = "Option::is_none")]
        topic: Option<TopicEcho>,
    },
    /// Nothing matched at all — the honest empty answer, never a
    /// fabricated one.
    Abstain {
        /// Why recall abstained: zero raw matches in the executed lanes and
        /// tombstone id probe (matched-but-excluded is
        /// [`RetrieveResponse::MissingEvidence`] instead).
        reason: String,
        /// S3 effort-lifecycle: the resolved effort scope echo, present ONLY
        /// when the query carried an `effort_id`. The `reason` text already
        /// names the effort in its composed fence label; this is the machine
        /// twin. A fenced zero-match ABSTAINS (never floored).
        #[serde(skip_serializing_if = "Option::is_none")]
        effort: Option<EffortEcho>,
        /// topic-anchor s1: the resolved topic scope echo, present ONLY when
        /// the query carried a `topic_id`. The `reason` text already names the
        /// topic in its composed fence label; this is the machine twin. A
        /// fenced zero-match ABSTAINS (never floored).
        #[serde(skip_serializing_if = "Option::is_none")]
        topic: Option<TopicEcho>,
    },
}

/// Errors crossing the retrieve boundary.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum RetrieveError {
    /// No term contained a single alphanumeric character — nothing could
    /// ever tokenize, so the request is malformed (distinct from a valid
    /// query that grounds nothing, which ABSTAINS).
    #[error(
        "retrieve rejected: query has no searchable term (each term needs at least one alphanumeric character)"
    )]
    EmptyQuery,
    /// An explicitly selected vector-bearing lane had no query vector.
    #[error(
        "retrieve rejected: lane \"{0}\" requires query_embedding (attach vectors via memory_vector and pass query_embedding, or use lane \"term\"/\"auto\")"
    )]
    LaneNeedsEmbedding(&'static str),
    /// The store failed underneath.
    #[error("retrieve: {0}")]
    Store(#[from] StoreError),
    /// Serializing an envelope for token accounting failed (e.g. a
    /// timestamp outside the RFC3339-representable year range).
    #[error("retrieve: response serialization failed: {0}")]
    Serialize(String),
    /// w3 u6a: `query_embedding` was passed but is unusable for cosine —
    /// empty, carrying a non-finite component, or of zero magnitude. A
    /// caller-side fault, taught rather than silently dropped.
    #[error("retrieve rejected: query_embedding {0}")]
    InvalidQueryEmbedding(String),
    /// w3 u6a: `query_embedding`'s dimension does not match a stored
    /// embedding's — cosine similarity is undefined across dimensions.
    /// Names BOTH so the caller can reconcile its embedder (the stored
    /// capsule is named for a concrete anchor).
    #[error(
        "retrieve rejected: query_embedding has dimension {query} but capsule {capsule_id} carries a dimension-{stored} embedding — vector recall needs matching dimensions (one embedder per store)"
    )]
    DimensionMismatch {
        /// The caller's `query_embedding` length.
        query: usize,
        /// The stored embedding's recorded dimension.
        stored: usize,
        /// The first (lowest-`seq`) capsule whose dimension differs — a
        /// deterministic, concrete anchor for the mismatch.
        capsule_id: String,
    },
    /// `weight_blend` was non-finite or outside its closed range.
    #[error("retrieve rejected: weight_blend {0}")]
    InvalidWeightBlend(String),
    /// `corroboration_blend` was non-finite or outside its closed range.
    #[error("retrieve rejected: corroboration_blend {0}")]
    InvalidCorroborationBlend(String),
}

/// Run one recall pass over the store at the injected instant `now`.
///
/// Pipeline: validate terms and every supplied query embedding → choose the
/// effective lane (omitted/auto picks term without an embedding and fused
/// with one) → execute the selected lane(s): term performs alias expansion
/// and project-fenced FTS5 `OR`; vector performs project-fenced positive
/// cosine matching; the lane-independent forgotten-id probe always runs → no
/// selected-lane candidate and no tombstone hit yields
/// [`RetrieveResponse::Abstain`] → apply the same eligibility fences to every
/// candidate, counting the FIRST fence that caught it (an explicit fact-time
/// window runs before the vector lane's `vector_k`; no window preserves the
/// historical raw cosine top-K path) → every candidate
/// excluded yields [`RetrieveResponse::MissingEvidence`] → rank under the
/// effective lane's complete contract. Term-only ranking uses coverage
/// descending, then bm25 ascending, advisory decay, freshness, usage late
/// keys, then `seq`. Forced-vector ranking uses one-lane RRF over cosine rank.
/// Fused ranking uses two-lane RRF over the independent term and vector ranks.
/// An enabled feedback blend runs only after that base rank → `limit` and
/// token-budget trim → build lane-specific evidence envelopes → count only
/// RETURNED ids via [`Store::record_recall`] at `now` →
/// [`RetrieveResponse::Grounded`].
///
/// u03 receipt ledger: AFTER the engine returns a grounded response, the
/// public wrapper persists its raw caller terms and response-ordered ids via
/// [`Store::record_recall_receipt`], then attaches the minted id. This write
/// is FAIL-CLOSED and precedes the fail-open miss telemetry below: a surfaced
/// receipt id always resolves. Engine-direct calls retain `receipt_id: None`.
///
/// u-r5 miss-ledger: AFTER the response is computed, the term lane's
/// PRE-TRIM observation controls the recall-miss ledger
/// ([`Store::record_recall_miss`]) — `missing_evidence` / `abstain` teach
/// vocabulary; a term hit records nothing even if limit/budget removes every
/// envelope. When FTS did not execute there is no term-lane observation and
/// no miss row, regardless of the overall vector result. The record is
/// FAIL-OPEN telemetry: the write error is SWALLOWED here so a ledger
/// failure can never fail or delay recall — the ONE deliberate exception
/// to the crate's fail-closed default, sound because a lost miss row costs
/// only an advisory alias hint, never a canonical byte. The recording runs
/// on the concrete [`Store`] (not the [`RecallStore`] recall seam) so the
/// pure recall algorithm stays untouched. Successful explicit choices that
/// differ from auto are recorded separately as fail-open lane telemetry;
/// rejected requests and choices equal to auto write none.
///
/// `anchor_root` is the base the `anchor_live`/`anchor_drift` probes
/// resolve `path:line` anchors against — boot-injected by the caller
/// ([`crate::server::BoundaryConfig::anchor_root`]:
/// `NMEMORY_ANCHOR_ROOT` > the boot cwd), NEVER a compiled-in path;
/// tests inject a hermetic temp root.
pub fn retrieve(
    store: &mut Store,
    query: &RetrieveQuery,
    now: OffsetDateTime,
    anchor_root: &Path,
) -> Result<RetrieveResponse, RetrieveError> {
    let RetrieveExecution {
        mut response,
        term_lane,
        lane_override,
    } = retrieve_observed(store, query, now, anchor_root)?;
    if let RetrieveResponse::Grounded {
        receipt_id,
        results,
        ..
    } = &mut response
    {
        let returned: Vec<&str> = results.iter().map(|e| e.id.as_str()).collect();
        let minted = store.record_recall_receipt(
            &query.terms,
            &returned,
            query.project_id.as_deref(),
            query.project_prefix.as_deref(),
            query.session_id.as_deref(),
            now,
        )?;
        *receipt_id = Some(minted);
    }
    // Miss classification is the TERM lane's pre-trim observation, never
    // the returned envelope list: limit/budget trimming cannot turn a hit
    // into a vocabulary miss, and a vector-only request has no term-lane
    // observation to record. Recording uses the RAW caller terms — the
    // store folds and deduplicates them (the alias-key normalization).
    let miss_outcome = term_lane.and_then(TermLaneObservation::miss_outcome);
    if let Some(outcome) = miss_outcome {
        // FAIL-OPEN: swallow the ledger write error — telemetry never
        // fails or delays the retrieve.
        let _ = store.record_recall_miss(&query.terms, outcome, now);
    }
    if let Some(override_) = lane_override {
        // FAIL-OPEN advisory telemetry. This point is reachable only after
        // the engine and the fail-closed grounded receipt write succeeded,
        // so rejected or otherwise failed requests never record an override.
        let _ = store.record_lane_override(override_, now);
    }
    Ok(response)
}

/// One caller term with its store-fed aliases — the OR-group the w2
/// synonym expansion works on (module doc).
struct TermGroup {
    /// The caller's term, verbatim (the explain vocabulary).
    term: String,
    /// Recorded aliases that widen this group's reach (deduplicated on
    /// folded form within the group; an alias that only re-spells its
    /// own term is dropped).
    aliases: Vec<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct LaneDecision {
    effective: Lane,
    run_fts: bool,
    run_vector: bool,
    override_: Option<LaneOverride>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum AutoPick {
    Term,
    Fused,
}

impl AutoPick {
    const fn lane(self) -> Lane {
        match self {
            AutoPick::Term => Lane::Term,
            AutoPick::Fused => Lane::Fused,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum TermLaneObservation {
    Grounded,
    MissingEvidence,
    Abstain,
}

impl TermLaneObservation {
    const fn miss_outcome(self) -> Option<RecallMissOutcome> {
        match self {
            TermLaneObservation::Grounded => None,
            TermLaneObservation::MissingEvidence => Some(RecallMissOutcome::MissingEvidence),
            TermLaneObservation::Abstain => Some(RecallMissOutcome::Abstain),
        }
    }
}

struct RetrieveExecution {
    response: RetrieveResponse,
    term_lane: Option<TermLaneObservation>,
    lane_override: Option<LaneOverride>,
}

fn lane_decision(
    requested: Option<Lane>,
    has_embedding: bool,
) -> Result<LaneDecision, RetrieveError> {
    let auto_pick = if has_embedding {
        AutoPick::Fused
    } else {
        AutoPick::Term
    };
    let effective = match requested.unwrap_or(Lane::Auto) {
        Lane::Auto => auto_pick.lane(),
        Lane::Term => Lane::Term,
        lane @ (Lane::Vector | Lane::Fused) if !has_embedding => {
            return Err(RetrieveError::LaneNeedsEmbedding(lane.as_str()));
        }
        lane @ (Lane::Vector | Lane::Fused) => lane,
    };
    let override_ = match (requested, auto_pick) {
        (None | Some(Lane::Auto), _) => None,
        (Some(Lane::Term), AutoPick::Term) => None,
        (Some(Lane::Term), AutoPick::Fused) => Some(LaneOverride::TermOverFused),
        (Some(Lane::Vector), AutoPick::Term) => Some(LaneOverride::VectorOverTerm),
        (Some(Lane::Vector), AutoPick::Fused) => Some(LaneOverride::VectorOverFused),
        (Some(Lane::Fused), AutoPick::Term) => Some(LaneOverride::FusedOverTerm),
        (Some(Lane::Fused), AutoPick::Fused) => None,
    };
    Ok(LaneDecision {
        effective,
        run_fts: matches!(effective, Lane::Term | Lane::Fused),
        run_vector: matches!(effective, Lane::Vector | Lane::Fused),
        override_,
    })
}

/// The lane evidence attached to one raw match before eligibility fences.
/// Keeping the pair together makes it impossible to swap the FTS and vector
/// slots at a call site.
#[derive(Debug, Clone, Copy)]
struct LaneMatch {
    score: Option<f64>,
    cosine: Option<f64>,
}

/// One fence-surviving match with every precomputed rank key. A candidate
/// may be reached by the FTS lane, the vector lane, or both (w3 u6a
/// fusion) — the two lane keys are `Option` so a lane that did not match it
/// contributes nothing.
struct Candidate {
    /// Matched term GROUPS (module doc: an alias hit advances its group
    /// exactly like a direct hit). Zero for a vector-only match.
    coverage: usize,
    /// Raw (unrounded) decay key — ordering uses this; the envelope
    /// carries the 2-decimal rounding.
    decayed: f64,
    stored: StoredCapsule,
    /// FTS bm25 score (smaller = stronger). `None` for a vector-only
    /// match; always `Some` in a dormant (FTS-only) query.
    score: Option<f64>,
    /// Cosine similarity to `query_embedding` (w3 u6a). `None` for an
    /// FTS-only match and for every dormant query.
    cosine: Option<f64>,
    /// 1-based position in the RRF-fused ranking (w3 u6a). `None` in a
    /// dormant query; set on every candidate once fusion has run.
    fusion_rank: Option<usize>,
    /// Advisory scored-outcome weight, loaded only for an enabled blend.
    feedback_weight: Option<f64>,
    /// S6 advisory corroboration weight (raw), loaded only when the
    /// corroboration blend is enabled; the envelope rounds it to 2 decimals.
    corroboration_weight: Option<f64>,
    usage: Option<UsageStat>,
    /// b2 staged review: the standing verdict of an INCLUDED staged row
    /// (`include_staged: true` surfaced a fenced proposal) — `None` for a
    /// plain live row, so the envelope omits `review_state`.
    review_state: Option<String>,
}

/// The engine behind [`retrieve`], generic over the [`RecallStore`]
/// contract seam; `anchor_root` is injected so tests probe liveness
/// against a hermetic temp root instead of the boot-injected production
/// root ([`crate::server::BoundaryConfig::anchor_root`]).
#[cfg(test)]
fn retrieve_core<S: RecallStore>(
    store: &mut S,
    query: &RetrieveQuery,
    now: OffsetDateTime,
    anchor_root: &Path,
) -> Result<RetrieveResponse, RetrieveError> {
    Ok(retrieve_observed(store, query, now, anchor_root)?.response)
}

/// Engine execution plus the pre-trim term-lane observation and successful
/// explicit override needed by the public wrapper's advisory ledgers. The
/// wire response itself stays unchanged.
fn retrieve_observed<S: RecallStore>(
    store: &mut S,
    query: &RetrieveQuery,
    now: OffsetDateTime,
    anchor_root: &Path,
) -> Result<RetrieveExecution, RetrieveError> {
    let blend = query.weight_blend.unwrap_or(0.0);
    if !blend.is_finite() || !(0.0..=1.0).contains(&blend) {
        return Err(RetrieveError::InvalidWeightBlend(format!(
            "must be finite and within 0.0..=1.0 (got {blend:?})"
        )));
    }
    let corroboration_blend = query.corroboration_blend.unwrap_or(0.0);
    if !corroboration_blend.is_finite() || !(0.0..=1.0).contains(&corroboration_blend) {
        return Err(RetrieveError::InvalidCorroborationBlend(format!(
            "must be finite and within 0.0..=1.0 (got {corroboration_blend:?})"
        )));
    }
    // Usable terms: trimmed, tokenizable, deduplicated (order-preserving).
    let mut terms: Vec<String> = Vec::new();
    for term in &query.terms {
        let term = term.trim();
        if term.chars().any(char::is_alphanumeric) && !terms.iter().any(|t| t == term) {
            terms.push(term.to_string());
        }
    }
    if terms.is_empty() {
        return Err(RetrieveError::EmptyQuery);
    }
    let lane = lane_decision(query.lane, query.query_embedding.is_some())?;

    // Synonym expansion (module doc): each term becomes an OR-group of
    // itself plus its recorded aliases; the flattened list feeds ONE
    // FTS5 OR query. Group aliases dedup on folded form WITHIN their
    // group only — attribution stays per-group truthful even when terms
    // overlap — while the search list dedups globally (a string already
    // searched widens nothing).
    let mut groups: Vec<TermGroup> = Vec::with_capacity(terms.len());
    let mut search_terms: Vec<String> = terms.clone();
    let mut alias_count = 0usize;
    if lane.run_fts {
        for term in &terms {
            let term_fold = folded(term);
            let mut aliases: Vec<String> = Vec::new();
            for alias in store.aliases_for(term)? {
                let alias = alias.trim();
                if !alias.chars().any(char::is_alphanumeric) {
                    continue;
                }
                let fold = folded(alias);
                if fold == term_fold || aliases.iter().any(|a| folded(a) == fold) {
                    continue;
                }
                if !search_terms.iter().any(|s| folded(s) == fold) {
                    search_terms.push(alias.to_string());
                }
                aliases.push(alias.to_string());
                alias_count += 1;
            }
            groups.push(TermGroup {
                term: term.clone(),
                aliases,
            });
        }
    }

    // S3 effort-lifecycle + topic-anchor s1: the resolved id-set scopes
    // (already valid — every teaching rejection fired at the boundary).
    // Compute each SQL fence id-set ONCE — effort members ∪ {epic}, topic
    // members ∪ {topic} — then AND-compose them into the ONE set both lanes
    // apply on top of the project/session fences. Two id-set fences compose by
    // INTERSECTION, which is what AND means: a row must sit in both. An empty
    // intersection (disjoint effort and topic) matches nothing and abstains —
    // the honest answer, never a widened one. With neither scope the set stays
    // `None`, so the store queries and their bytes are byte-identical to the
    // pre-S3 engine (dormancy). The echoes are built once and shared by all
    // three honest outcomes.
    let effort_fence_ids: Option<Vec<String>> = query.effort.as_ref().map(EffortScope::fence_ids);
    let topic_fence_ids: Option<Vec<String>> = query.topic.as_ref().map(TopicScope::fence_ids);
    let scope_fence_ids: Option<Vec<String>> = match (&effort_fence_ids, &topic_fence_ids) {
        (Some(effort), Some(topic)) => {
            let topic_set: HashSet<&str> = topic.iter().map(String::as_str).collect();
            Some(
                effort
                    .iter()
                    .filter(|id| topic_set.contains(id.as_str()))
                    .cloned()
                    .collect(),
            )
        }
        (Some(ids), None) | (None, Some(ids)) => Some(ids.clone()),
        (None, None) => None,
    };
    let effort_ids: Option<&[String]> = scope_fence_ids.as_deref();
    let effort_echo = query.effort.as_ref().map(|scope| EffortEcho {
        epic_id: scope.epic_id.clone(),
        member_total: scope.member_total(),
        open: scope.open,
    });
    let topic_echo = query.topic.as_ref().map(|scope| TopicEcho {
        topic_id: scope.topic_id.clone(),
        member_total: scope.member_total(),
    });

    // Fence label for the honest empty answers (w2-fix): BOTH scope
    // fences are named — an empty recall caused by a project_prefix must
    // blame the fence, never the terms (symmetric with project_id).
    let mut fence = match (&query.project_id, &query.project_prefix) {
        (Some(project), Some(prefix)) => {
            format!(" within project '{project}' and subtree '{prefix}'")
        }
        (Some(project), None) => format!(" within project '{project}'"),
        (None, Some(prefix)) => format!(" within project subtree '{prefix}'"),
        (None, None) => String::new(),
    };
    if let Some(session_id) = &query.session_id {
        if fence.is_empty() {
            fence = format!(" within store-local capsule label '{session_id}'");
        } else {
            fence.push_str(&format!(" and store-local capsule label '{session_id}'"));
        }
    }
    // topic-anchor s1: the topic clause prepends BEFORE the effort clause
    // below, so when both are present the EFFORT still LEADS (S3's frozen
    // contract) and the topic reads second. A topic-free query leaves `fence`
    // byte-identical.
    if let Some(scope) = &query.topic {
        let topic_clause = format!(
            "topic '{}' ({} members)",
            scope.topic_id,
            scope.member_total()
        );
        fence = match fence.strip_prefix(" within ") {
            Some(rest) => format!(" within {topic_clause} and {rest}"),
            None => format!(" within {topic_clause}"),
        };
    }
    // S3: the effort clause LEADS the composed fence label (the contract
    // example: ` within effort 'cap-42' (7 members) and project 'nott'`). It
    // AND-composes with whatever project/session/topic fence already built
    // above; an effort-free query leaves `fence` byte-identical.
    if let Some(scope) = &query.effort {
        let effort_clause = format!(
            "effort '{}' ({} members)",
            scope.epic_id,
            scope.member_total()
        );
        fence = match fence.strip_prefix(" within ") {
            Some(rest) => format!(" within {effort_clause} and {rest}"),
            None => format!(" within {effort_clause}"),
        };
    }

    let matches = if lane.run_fts {
        store.search_fts(
            &search_terms,
            query.project_id.as_deref(),
            query.project_prefix.as_deref(),
            query.session_id.as_deref(),
            effort_ids,
        )?
    } else {
        Vec::new()
    };
    let lexical = matches.len();

    // Forgotten-id probe (module doc): a CALLER term that exactly names
    // a tombstoned capsule id is a raw match excluded as Tombstoned —
    // the content is gone by design and can never match lexically, but
    // the caller who named the id deserves "forgotten", not "never
    // existed". Terms are already deduplicated, so one tombstone counts
    // once; aliases are store-derived data and never probe.
    let mut tombstone_hits = 0usize;
    for term in &terms {
        let tombstone = match query.session_id.as_deref() {
            Some(session_id) => store.get_tombstone_for_session_label(term, session_id)?,
            None => store.get_tombstone(term)?,
        };
        if tombstone.is_some() {
            tombstone_hits += 1;
        }
    }

    // w3 u6a caller-fed vector lane. Omitted/auto preserves the historical
    // presence rule; an explicit term lane keeps this path dormant even
    // when a query vector is present. When routing executes the vector lane,
    // the caller's embedding is validated and cosine similarity is computed
    // against every LIVE in-scope stored embedding (the store computes NO
    // embedding — zero embedder dependency). With no fact-time window, the
    // top `vector_k` by cosine become the historical raw matches. With one,
    // the ordered candidates are fenced first and only eligible candidates
    // consume that cap. The dimension check names both sides on a mismatch,
    // at the first (lowest-seq) offender — deterministic.
    let rank_with_rrf = lane.run_vector;
    let mut vector_scored: Vec<(StoredCapsule, f64)> = Vec::new();
    let mut positive_vector_matches = 0usize;
    if let Some(query_embedding) = query.query_embedding.as_deref() {
        validate_query_embedding(query_embedding)?;
    }
    if lane.run_vector {
        let query_embedding = query
            .query_embedding
            .as_deref()
            .ok_or(RetrieveError::LaneNeedsEmbedding(lane.effective.as_str()))?;
        let vector_k = query.vector_k.unwrap_or(DEFAULT_VECTOR_K);
        for (stored, embedding) in store.embeddings_for_recall(
            query.project_id.as_deref(),
            query.project_prefix.as_deref(),
            query.session_id.as_deref(),
            effort_ids,
        )? {
            if embedding.dimension != query_embedding.len() {
                return Err(RetrieveError::DimensionMismatch {
                    query: query_embedding.len(),
                    stored: embedding.dimension,
                    capsule_id: stored.id.as_str().to_string(),
                });
            }
            let cosine = cosine_similarity(query_embedding, &embedding.vector);
            // fleet-8 c7 F1: a cosine ≤ 0 declares NO positive relation —
            // the lane proposes nothing on it. Without this floor an
            // orthogonal (0.0) or anti-correlated (-1.0) embedding could
            // solely-ground a result at rank 1, over-claiming "grounded"
            // and silently starving the R5 miss-ledger. Not a magic
            // threshold: zero is exactly where the metric itself stops
            // asserting any positive relation.
            if cosine > 0.0 {
                vector_scored.push((stored, cosine));
            }
        }
        positive_vector_matches = vector_scored.len();
        // Vector-lane raw order: cosine desc, ties by seq asc
        // (deterministic). With no fact-time window the historical path is
        // preserved byte-for-byte: truncate before the fences. A window is
        // different by contract — its full dominance-ordered predicate runs
        // before top-K, so keep the ordered candidates for the fence pass
        // below to scan until `vector_k` eligible rows survive.
        vector_scored.sort_by(|a, b| b.1.total_cmp(&a.1).then_with(|| a.0.seq.cmp(&b.0.seq)));
        if query.time_window.is_none() || vector_k == 0 {
            vector_scored.truncate(vector_k);
        }
    }

    let empty_lane_reason = || {
        if lane.effective == Lane::Vector {
            if positive_vector_matches > 0 {
                let vector_k = query.vector_k.unwrap_or(DEFAULT_VECTOR_K);
                format!(
                    "the forced vector lane found {positive_vector_matches} positively similar \
                     stored capsule(s){fence}, but vector_k={vector_k} excluded every candidate \
                     before ranking; abstaining instead of fabricating"
                )
            } else {
                format!(
                    "the forced vector lane found no stored capsule{fence} with a positively \
                     similar embedding; abstaining instead of fabricating"
                )
            }
        } else {
            let alias_note = if alias_count > 0 {
                format!(" (terms expanded with {alias_count} store-fed alias(es))")
            } else {
                String::new()
            };
            let vector_note = if positive_vector_matches > 0 {
                let vector_k = query.vector_k.unwrap_or(DEFAULT_VECTOR_K);
                format!(
                    " ({positive_vector_matches} positively similar stored embedding \
                     candidate(s) were excluded by vector_k={vector_k} before ranking)"
                )
            } else if lane.run_vector {
                " (no stored embedding was available to compare with positive similarity)"
                    .to_string()
            } else {
                String::new()
            };
            format!(
                "no stored capsule matched any of the {} query term(s){fence}{alias_note}{vector_note}; \
                 abstaining instead of fabricating",
                terms.len()
            )
        }
    };

    // Abstain only when BOTH lanes (and the tombstone probe) are empty —
    // an honest empty answer, never fabricated. DORMANT reduces to the
    // former `lexical == 0 && tombstone_hits == 0` (vector_scored is
    // always empty), and the reason text is byte-identical (the vector
    // note is empty). FUSED adds the note only when a vector lane ran but
    // found no in-scope embedding to compare.
    if lexical == 0 && tombstone_hits == 0 && vector_scored.is_empty() {
        return Ok(RetrieveExecution {
            response: RetrieveResponse::Abstain {
                reason: empty_lane_reason(),
                effort: effort_echo,
                topic: topic_echo,
            },
            term_lane: lane.run_fts.then_some(TermLaneObservation::Abstain),
            lane_override: lane.override_,
        });
    }

    // Eligibility fences (W1 tri-state + w2 tier + u6h falsified): each raw
    // match either survives into `current` or is counted under the FIRST
    // fence that caught it — quarantined, then FALSIFIED, then archived,
    // then superseded (h4), then currency, then the optional fact-time
    // window. Precedence is a LAW, not an accident (w2-fix + u6h): quarantine dominates everything (the taint
    // signal must never disappear — the planner's own dominance rule);
    // falsified (u6h) dominates archived AND superseded (a falsified fact
    // must never hide behind a softer lifecycle bucket); archived dominates
    // superseded (the planner archives only superseded records; the tier
    // must stay observable on recall). So a capsule quarantined AND anything
    // counts quarantined; falsified AND archived/superseded counts falsified;
    // superseded AND archived counts archived. w3 u6a: the fences are
    // LANE-AGNOSTIC — `fence_candidate` applies the SAME dominance to an
    // FTS match and a vector match, so a quarantined or falsified capsule
    // can never surface via the vector lane (fence red-test). Fence order ==
    // `ExclusionReason` variant order == wire order. A new fence is one
    // variant plus one arm in `fence_candidate` (tombstoned landed as the
    // term-probe above — content matches are structurally impossible for it).
    // u06: fact time is last and query-shaped, so every stored-state and
    // currency reason remains dominant and an absent window stays dormant.
    let mut excluded: BTreeMap<ExclusionReason, usize> = BTreeMap::new();
    if tombstone_hits > 0 {
        excluded.insert(ExclusionReason::Tombstoned, tombstone_hits);
    }
    let mut current: Vec<Candidate> = Vec::with_capacity(lexical);
    // `seen`/`survivor_idx` keep the fusion HONEST across lanes: each
    // distinct capsule is fenced ONCE (no double-counted exclusion), a
    // capsule matched by both lanes carries both explains, and an
    // FTS-excluded capsule never resurfaces through the vector lane.
    let mut seen: HashSet<i64> = HashSet::new();
    let mut survivor_idx: HashMap<i64, usize> = HashMap::new();
    // FTS lane fence pass — identical order and counts to the FTS-only
    // engine (dormant byte-identity).
    for (stored, score) in matches {
        let seq = stored.seq;
        seen.insert(seq);
        if let Some(candidate) = fence_candidate(
            stored,
            LaneMatch {
                score: Some(score),
                cosine: None,
            },
            store,
            &groups,
            now,
            query.time_window.as_ref(),
            query.include_staged,
            &mut excluded,
        )? {
            survivor_idx.insert(seq, current.len());
            current.push(candidate);
        }
    }
    let term_survivors = current.len();
    let term_lane = if lane.run_fts {
        Some(if term_survivors > 0 {
            TermLaneObservation::Grounded
        } else if lexical + tombstone_hits > 0 {
            TermLaneObservation::MissingEvidence
        } else {
            TermLaneObservation::Abstain
        })
    } else {
        None
    };
    // Vector lane fence pass: annotate a both-lanes survivor in fused mode
    // with its cosine, skip an already-fenced capsule (dominance holds),
    // and fence a brand-new vector-only match through the SAME gate. Under
    // a fact-time window this is the PRE-RANKING top-K scan: rejected rows
    // do not consume K, so a lower-cosine eligible row can still enter the
    // lane; once K eligible rows survive, lower-ranked vectors remain beyond
    // the lane's reach and are neither read nor counted. With no window the
    // vec was already truncated above, preserving historical behavior.
    let pre_rank_time_window = query.time_window.is_some();
    let vector_k = query.vector_k.unwrap_or(DEFAULT_VECTOR_K);
    let mut vector_survivors = 0usize;
    for (stored, cosine) in vector_scored {
        if pre_rank_time_window && vector_survivors >= vector_k {
            break;
        }
        let seq = stored.seq;
        if let Some(&idx) = survivor_idx.get(&seq) {
            current[idx].cosine = Some(cosine);
            vector_survivors += 1;
            continue;
        }
        if seen.contains(&seq) {
            continue;
        }
        seen.insert(seq);
        if let Some(candidate) = fence_candidate(
            stored,
            LaneMatch {
                score: None,
                cosine: Some(cosine),
            },
            store,
            &groups,
            now,
            query.time_window.as_ref(),
            query.include_staged,
            &mut excluded,
        )? {
            survivor_idx.insert(seq, current.len());
            current.push(candidate);
            vector_survivors += 1;
        }
    }
    if current.is_empty() {
        // Every distinct match was excluded; the count is the sum over the
        // reason map (== `lexical + tombstone_hits` in the dormant case,
        // where FTS matches are the only distinct candidates).
        let total = excluded.values().sum();
        let match_clause = if lane.effective == Lane::Vector {
            if tombstone_hits == 0 {
                format!("matched the forced vector lane{fence}")
            } else if tombstone_hits == total {
                format!(
                    "were identified by the lane-independent tombstone id probe while the forced \
                     vector lane ran{fence}"
                )
            } else {
                format!(
                    "matched the forced vector lane or were identified by its lane-independent \
                     tombstone id probe{fence}"
                )
            }
        } else {
            format!("matched{fence}")
        };
        return Ok(RetrieveExecution {
            response: missing_evidence(total, excluded, &match_clause, effort_echo, topic_echo),
            term_lane,
            lane_override: lane.override_,
        });
    }

    if rank_with_rrf {
        // Reciprocal Rank Fusion (w3 u6a): rank each lane independently,
        // then fuse by `sum 1/(RRF_K + rank)`. The FTS lane ranks by the
        // SAME deterministic key the dormant engine sorts by
        // ([`fts_rank_key`]); the vector lane ranks by cosine desc, seq
        // asc. A candidate absent from a lane contributes 0 for it. The
        // fused order sorts by RRF desc, seq asc — fully deterministic:
        // same lane inputs always yield the same order.
        let mut fts_sorted: Vec<&Candidate> =
            current.iter().filter(|c| c.score.is_some()).collect();
        fts_sorted.sort_by(|a, b| fts_rank_key(a, b));
        let fts_rank: HashMap<i64, usize> = fts_sorted
            .iter()
            .enumerate()
            .map(|(rank, c)| (c.stored.seq, rank + 1))
            .collect();
        let mut vector_sorted: Vec<&Candidate> =
            current.iter().filter(|c| c.cosine.is_some()).collect();
        vector_sorted.sort_by(|a, b| {
            cosine_key(b)
                .total_cmp(&cosine_key(a))
                .then_with(|| a.stored.seq.cmp(&b.stored.seq))
        });
        let vector_rank: HashMap<i64, usize> = vector_sorted
            .iter()
            .enumerate()
            .map(|(rank, c)| (c.stored.seq, rank + 1))
            .collect();
        let rrf_of = |c: &Candidate| -> f64 {
            let fts = fts_rank
                .get(&c.stored.seq)
                .map_or(0.0, |&r| 1.0 / (RRF_K + r as f64));
            let vector = vector_rank
                .get(&c.stored.seq)
                .map_or(0.0, |&r| 1.0 / (RRF_K + r as f64));
            fts + vector
        };
        current.sort_by(|a, b| {
            rrf_of(b)
                .total_cmp(&rrf_of(a))
                .then_with(|| a.stored.seq.cmp(&b.stored.seq))
        });
        // The fused position is explain data on every returned row.
        for (index, candidate) in current.iter_mut().enumerate() {
            candidate.fusion_rank = Some(index + 1);
        }
    } else {
        // DORMANT rank key (module doc): GROUP COVERAGE desc (w1d,
        // alias-aware), then the PLAN s4 tiebreak (bm25 asc, decayed
        // weight desc, valid_from desc, the h4 late usage key, id asc).
        // `now` enters ONLY through the decay ages — same store + same
        // `now` = same order. Byte-identical to the pre-u6a engine.
        current.sort_by(fts_rank_key);
    }

    // Preserve the PRE-blend relevance anchor. In dormant FTS mode this is
    // exactly the former base leader (coverage-first, then bm25); in fused
    // mode it is the strongest FTS score independent of final order.
    let top_score = if rank_with_rrf {
        current
            .iter()
            .filter_map(|candidate| candidate.score)
            .reduce(f64::min)
            .unwrap_or(0.0)
    } else {
        current
            .first()
            .map_or(0.0, |candidate| candidate.score.unwrap_or(0.0))
    };

    // u04 + S6 opt-in ranking blend: decorate only AFTER every eligibility
    // fence and the deterministic base ranking. The base position is the
    // only rank input, so FTS and fused lanes share one mechanism; an RRF
    // `fusion_rank` above remains the PRE-blend explain. Crucially, the
    // entire block is skipped when BOTH blends are zero — zero feedback or
    // corroboration RANKING-weight reads and byte-identical order/envelopes
    // on the default path. The independent corroboration summary is still
    // read later per returned envelope. Each blend reads its OWN ranking
    // sidecar only when active, so a dormant blend leaves its ranking explain
    // field absent; when one blend is zero its factor is exactly `1.0`,
    // preserving the other blend's byte-identical behavior.
    if blend != 0.0 || corroboration_blend != 0.0 {
        let mut decorated: Vec<(f64, Candidate)> = Vec::with_capacity(current.len());
        for (rank0, mut candidate) in current.drain(..).enumerate() {
            let weight = if blend != 0.0 {
                let weight = store
                    .feedback_weight_of(candidate.stored.id.as_str())?
                    .unwrap_or(FEEDBACK_NEUTRAL_WEIGHT);
                candidate.feedback_weight = Some(weight);
                weight
            } else {
                FEEDBACK_NEUTRAL_WEIGHT
            };
            let corroboration = if corroboration_blend != 0.0 {
                let corroboration = store
                    .corroboration_weight(&candidate.stored.id)
                    .unwrap_or(CORROBORATION_NEUTRAL_WEIGHT);
                candidate.corroboration_weight = Some(corroboration);
                corroboration
            } else {
                CORROBORATION_NEUTRAL_WEIGHT
            };
            let score = (1.0 / (RRF_K + rank0 as f64 + 1.0))
                * (1.0 + blend * (weight - FEEDBACK_NEUTRAL_WEIGHT))
                * (1.0 + corroboration_blend * (corroboration - CORROBORATION_NEUTRAL_WEIGHT));
            decorated.push((score, candidate));
        }
        decorated.sort_by(|a, b| {
            b.0.total_cmp(&a.0)
                .then_with(|| a.1.stored.seq.cmp(&b.1.stored.seq))
        });
        current.extend(decorated.into_iter().map(|(_, candidate)| candidate));
    }

    let token_budget = query.token_budget.unwrap_or(DEFAULT_TOKEN_BUDGET);
    // Anchor of the relevance scale (FTS-lane explain): the strongest
    // (most negative) bm25 score present. DORMANT: this is the top-ranked
    // row's score exactly as before (the FTS sort puts the strongest first
    // within the top coverage tier). FUSED: the min over FTS-matched rows,
    // so FTS relevance stays a sane 0..1 even when a vector-only row leads
    // the fused order. `current` is non-empty here.
    let mut results: Vec<Evidence> = Vec::new();
    let mut used_tokens = 0usize;
    let mut trimmed_by_limit = 0usize;
    let mut trimmed_by_budget = 0usize;
    // Once the budget trims a row, every later row is budget-trimmed too:
    // the returned set is always a rank PREFIX (a cheaper row never slips
    // in past a trimmed better-ranked one).
    let mut budget_closed = false;
    for candidate in &current {
        if let Some(limit) = query.limit
            && results.len() >= limit
        {
            trimmed_by_limit += 1;
            continue;
        }
        if budget_closed {
            trimmed_by_budget += 1;
            continue;
        }
        // u-r2 sidecar reads, per RETURNED row only (trimmed rows never
        // pay them): the capture-time anchor hash feeds the drift probe;
        // the epistemic record rides the envelope when present.
        let capture_hash = store.anchor_hash_of(candidate.stored.id.as_str())?;
        let epistemics = store.epistemics_of(candidate.stored.id.as_str())?;
        // S2 git witness lane: the newest corroboration decorates the
        // envelope, read per RETURNED row like the anchor-drift hash — a
        // store-only SQL read (the serve path spawns no git). `None` (never
        // scanned) omits the field, so a store with zero rows is
        // byte-identical.
        let corroboration = store.latest_corroborations_of(candidate.stored.id.as_str())?;
        // S3 effort-lifecycle: stamp this row's role in the fencing effort —
        // the epic itself vs a `part_of` member. Only reachable when the query
        // carried an effort scope; `None` keeps the envelope byte-identical.
        let effort_role = query.effort.as_ref().map(|scope| {
            if candidate.stored.id.as_str() == scope.epic_id {
                EffortRole::Epic
            } else {
                EffortRole::Member
            }
        });
        // topic-anchor s1: the same stamp for the fencing topic — the topic
        // node itself vs an `about` member. `None` keeps the envelope
        // byte-identical.
        let topic_role = query.topic.as_ref().map(|scope| {
            if candidate.stored.id.as_str() == scope.topic_id {
                TopicRole::Topic
            } else {
                TopicRole::Member
            }
        });
        let envelope = evidence_for(
            candidate,
            top_score,
            &groups,
            anchor_root,
            capture_hash.as_deref(),
            epistemics,
            corroboration,
            effort_role,
            topic_role,
        );
        let serialized = serde_json::to_string(&envelope)
            .map_err(|e| RetrieveError::Serialize(e.to_string()))?;
        let cost = approx_tokens(&serialized);
        // Budget floor: with a NONZERO budget the top-ranked envelope
        // always fits (grounded means at least one result; documented on
        // the wire); the tail trims first. Budget 0 is honored literally
        // — zero results, mirroring `limit: 0` (zero-cap consistency).
        if (results.is_empty() && token_budget == 0)
            || (!results.is_empty() && used_tokens + cost > token_budget)
        {
            budget_closed = true;
            trimmed_by_budget += 1;
            continue;
        }
        used_tokens += cost;
        results.push(envelope);
    }

    // Count the recall — RETURNED ids only (a trimmed match was not
    // recalled into anyone's context), at the same injected `now`.
    let returned_ids: Vec<&str> = results.iter().map(|e| e.id.as_str()).collect();
    store.record_recall(&returned_ids, now)?;

    let matched = current.len();
    let returned = results.len();
    Ok(RetrieveExecution {
        response: RetrieveResponse::Grounded {
            results,
            matched,
            returned,
            trimmed: matched - returned,
            trimmed_by_limit,
            trimmed_by_budget,
            token_budget,
            excluded,
            receipt_id: None,
            effort: effort_echo,
            topic: topic_echo,
        },
        term_lane,
        lane_override: lane.override_,
    })
}

/// The h4 LATE usage tiebreak key: most-recent recall first, then higher
/// `recall_count`; a never-recalled capsule (`None` — also the state
/// after the derived table is dropped) sorts after any recalled one.
fn usage_key(usage: Option<UsageStat>) -> (Option<OffsetDateTime>, i64) {
    usage.map_or((None, 0), |u| (Some(u.last_recalled_at), u.recall_count))
}

/// Build the [`RetrieveResponse::MissingEvidence`] outcome: every raw match
/// from the executed lane(s) or the tombstone id probe was excluded by an
/// eligibility fence. Pure function of the exclusion counts and the
/// caller-supplied match clause — deterministic bytes: the map and the prose
/// both walk [`ExclusionReason`] variant order.
fn missing_evidence(
    total: usize,
    excluded: BTreeMap<ExclusionReason, usize>,
    match_clause: &str,
    effort: Option<EffortEcho>,
    topic: Option<TopicEcho>,
) -> RetrieveResponse {
    let detail: Vec<String> = excluded
        .iter()
        .map(|(reason, count)| format!("{count} {}", reason.wire_name()))
        .collect();
    // Reachability clause, accurate PER EXCLUSION CLASS present (w1d
    // stress fix: tombstoned capsules are absent from list — claiming
    // get/list for them was false): every non-tombstoned exclusion
    // (superseded / archived / quarantined / expired / not-yet-valid /
    // outside-time-window / undated)
    // stays reachable via get/list; a tombstoned id answers get only,
    // with its marker.
    let has_tombstoned = excluded.contains_key(&ExclusionReason::Tombstoned);
    let has_other = excluded.keys().any(|r| *r != ExclusionReason::Tombstoned);
    let reachability = match (has_other, has_tombstoned) {
        (true, true) => {
            "(the non-tombstoned ones remain reachable via get/list; \
             a tombstoned id answers get only, with its marker)"
        }
        (false, true) => "(a tombstoned id answers get only, with its marker — never list)",
        _ => "(they remain reachable via get/list)",
    };
    let reason = format!(
        "{total} capsule(s) {match_clause} but every one is excluded from \
         recall ({}); reporting missing evidence instead of recalling ineligible \
         capsules {reachability}",
        detail.join(", ")
    );
    RetrieveResponse::MissingEvidence {
        excluded_count: total,
        excluded,
        reason,
        effort,
        topic,
    }
}

/// Currency fence at the injected instant: `None` means currently valid —
/// `valid_from <= now` and, when a `valid_to` exists, `now <= valid_to`
/// (both bounds inclusive, unchanged from s4) — otherwise the exclusion
/// reason, `valid_from` checked first. Decides survival only — never
/// order.
fn currency_exclusion(freshness: Freshness, now: OffsetDateTime) -> Option<ExclusionReason> {
    if freshness.valid_from > now {
        return Some(ExclusionReason::NotYetValid);
    }
    if freshness.valid_to.is_some_and(|valid_to| now > valid_to) {
        return Some(ExclusionReason::Expired);
    }
    None
}

/// Build one evidence envelope for a surviving [`Candidate`]. `top` is
/// the result set's strongest (most negative) score — the anchor
/// `relevance` normalizes against; `anchor_root` is the liveness/drift
/// probe root (module doc); `capture_hash` is the capsule's recorded
/// capture-time anchored-file hash (`None` → drift `"unknown"`), and
/// `epistemics` its optional sidecar annotations (omitted when `None`).
#[allow(clippy::too_many_arguments)]
fn evidence_for(
    candidate: &Candidate,
    top: f64,
    groups: &[TermGroup],
    anchor_root: &Path,
    capture_hash: Option<&str>,
    epistemics: Option<EpistemicsRecord>,
    corroboration: Option<CorroborationSummary>,
    effort_role: Option<EffortRole>,
    topic_role: Option<TopicRole>,
) -> Evidence {
    let stored = &candidate.stored;
    let capsule = &stored.capsule;
    let (evidence_state, proof_hint, stale_if) = match epistemics {
        None => (None, None, None),
        Some(record) => (record.evidence_state, record.proof_hint, record.stale_if),
    };
    Evidence {
        label: AdvisoryLabel,
        framing: DataFraming,
        id: stored.id.clone(),
        headline: headline_of(capsule.content()),
        instruction_taint: capsule.instruction_taint(),
        authority_class: capsule.authority_class(),
        confidence: capsule.confidence(),
        decayed_weight: round2(candidate.decayed),
        provenance: capsule.provenance().clone(),
        anchor_live: anchor_liveness(&capsule.provenance().anchor, anchor_root),
        anchor_drift: anchor_drift_of(&capsule.provenance().anchor, anchor_root, capture_hash),
        evidence_state,
        proof_hint,
        stale_if,
        review_state: candidate.review_state.clone(),
        freshness: capsule.freshness(),
        matched_terms: matched_groups(capsule.content(), groups),
        // FTS-lane explain: present iff the term lane matched this row. In
        // a dormant query every candidate has a score, so both fields fill
        // exactly as before (`Some(x)` serializes as the bare `x`).
        relevance: candidate.score.map(|score| relevance_of(score, top)),
        bm25: candidate.score.map(rounded_bm25),
        // Vector-lane explain: present iff the vector lane matched this row
        // (a vector-only match grounds WITH this). Absent in a dormant
        // query. Fusion rank is present on every row of a fused query.
        vector_similarity: candidate.cosine.map(round4),
        fusion_rank: candidate.fusion_rank,
        feedback_weight: candidate.feedback_weight.map(round2),
        // S2 git witness lane: skip-if-none, so a never-scanned capsule's
        // envelope bytes are unchanged.
        corroboration: corroboration.map(CorroborationWire::from),
        corroboration_weight: candidate.corroboration_weight.map(round2),
        effort_role,
        topic_role,
    }
}

/// The ADVISORY decay key (module doc): `confidence × 2^(-age_days /`
/// [`DECAY_HALF_LIFE_DAYS`]`)`. Age runs from `freshness.valid_from` to
/// the injected `now`, clamped at zero for totality (ranked capsules
/// already passed the currency fence, so `valid_from <= now` holds).
/// Raw value — the envelope rounds to 2 decimals via [`round2`].
/// `pub(crate)` so `memory_bootstrap` ranks its kind sections on the SAME
/// decay key (u-r9), never a drifting second copy of the formula.
pub(crate) fn decay_weight(
    confidence: f64,
    valid_from: OffsetDateTime,
    now: OffsetDateTime,
) -> f64 {
    let age_days = ((now - valid_from).as_seconds_f64() / 86_400.0).max(0.0);
    confidence * (-age_days / DECAY_HALF_LIFE_DAYS).exp2()
}

/// How many of the caller's RAW terms ground this content, by the store's
/// within-term AND rule (all a term's folded tokens present, order- and
/// adjacency-insensitive) — the same match [`term_matches`] backs for the
/// retrieve explain. `pub(crate)` for `memory_bootstrap`'s deterministic
/// term-coverage rank (u-r9): caller-expanded terms ONLY, NO alias
/// expansion and NO server-side intent guessing — bootstrap's determinism
/// law is stricter than retrieve's alias-aware recall. Zero terms → 0 (a
/// pure decay order downstream).
pub(crate) fn term_coverage(content: &str, terms: &[String]) -> usize {
    if terms.is_empty() {
        return 0;
    }
    let content_tokens: std::collections::BTreeSet<String> = tokens(content).into_iter().collect();
    terms
        .iter()
        .filter(|term| term_matches(&content_tokens, term))
        .count()
}

/// Round to 2 decimals for the wire (the house pattern `relevance` also
/// uses).
fn round2(value: f64) -> f64 {
    (value * 100.0).round() / 100.0
}

/// Anchor-liveness probe (module doc): `path:line` anchors (the suffix
/// after the LAST `:` all ASCII digits; earlier colons belong to the
/// path) get a symlink-refusing existence check against `root`: the
/// relative path is walked one component at a time with
/// `fs::symlink_metadata` (which never follows links), so ANY symlink
/// component — interior or leaf, wherever it points — answers
/// [`AnchorLive::Missing`] without ever being followed. `fs::metadata`
/// would follow a repo-internal link and existence-probe OUTSIDE the
/// root (v3 fence, fail-closed: a symlinked anchor is `false`, never an
/// out-of-root probe). Absolute and `..`-traversing paths never leave
/// the fence → [`AnchorLive::Unknown`]; a missing path is
/// [`AnchorLive::Missing`]; any io failure degrades to
/// [`AnchorLive::Unknown`]. Never panics, never reads content, never
/// blocks recall.
fn anchor_liveness(anchor: &str, root: &Path) -> AnchorLive {
    let Some((path_part, line_part)) = anchor.rsplit_once(':') else {
        return AnchorLive::Unknown;
    };
    if path_part.is_empty()
        || line_part.is_empty()
        || !line_part.bytes().all(|b| b.is_ascii_digit())
    {
        return AnchorLive::Unknown;
    }
    let rel = Path::new(path_part);
    if rel.is_absolute()
        || rel
            .components()
            .any(|c| !matches!(c, Component::Normal(_) | Component::CurDir))
    {
        return AnchorLive::Unknown;
    }
    let mut probe = root.to_path_buf();
    let mut probed = false;
    for component in rel.components() {
        let Component::Normal(part) = component else {
            continue; // CurDir: `./x` probes the same path as `x`
        };
        probe.push(part);
        probed = true;
        match std::fs::symlink_metadata(&probe) {
            // Fail-closed: a symlink is never followed — no verdict about
            // its target's existence can leak, in or out of the root.
            Ok(meta) if meta.file_type().is_symlink() => return AnchorLive::Missing,
            Ok(_) => {}
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                // A probe with no root has no verdict: when `root` itself
                // is absent (foreign host, CI container), every anchor
                // would read as a confident dead link — degrade to
                // Unknown instead of over-claiming Missing (w2 review).
                return if std::fs::metadata(root).is_ok() {
                    AnchorLive::Missing
                } else {
                    AnchorLive::Unknown
                };
            }
            Err(_) => return AnchorLive::Unknown,
        }
    }
    // Every component existed symlink-free. A path of only `.` components
    // probed nothing — fall back to the root's own existence, exactly
    // what the pre-walk single probe of `root.join(".")` answered.
    if probed || std::fs::metadata(root).is_ok() {
        AnchorLive::Live
    } else {
        AnchorLive::Unknown
    }
}

/// SHA-256 hex of the anchored file's CURRENT bytes, for a `path:line`
/// anchor that resolves through the SAME fail-closed v3 fence as
/// [`anchor_liveness`] — the probe IS the fence: only an anchor the
/// liveness walk answers [`AnchorLive::Live`] for (every component
/// existing and symlink-free under `root`) is read; everything else —
/// non-path anchors, absolute / `..`-traversing paths, symlink
/// components, missing paths — is `None`, never an out-of-root read. A
/// read failure after the walk (a directory anchor, permissions, a race)
/// degrades to `None` too. Total: never panics, never blocks.
///
/// Both ends of the drift comparison use this ONE function: the boundary
/// at capture (recording into the `anchor_hashes` sidecar against the
/// boot-injected anchor root) and the recall probe ([`anchor_drift_of`])
/// — so the two hashes can only ever differ when the file's bytes did.
pub(crate) fn anchor_content_hash(anchor: &str, root: &Path) -> Option<String> {
    if anchor_liveness(anchor, root) != AnchorLive::Live {
        return None;
    }
    // Live guarantees the `path:line` split succeeded and the path passed
    // the fence; re-derive the resolved path for the content read.
    let (path_part, _line) = anchor.rsplit_once(':')?;
    let bytes = std::fs::read(root.join(path_part)).ok()?;
    Some(sha256_hex(&bytes))
}

/// The `anchor_drift` verdict (module doc: Anchor drift): compare the
/// anchored file's current content hash ([`anchor_content_hash`]) against
/// the capture-time hash from the `anchor_hashes` sidecar. Either side
/// unavailable → [`AnchorDrift::Unknown`] — the probe never guesses.
fn anchor_drift_of(anchor: &str, root: &Path, capture_hash: Option<&str>) -> AnchorDrift {
    let Some(capture) = capture_hash else {
        return AnchorDrift::Unknown;
    };
    match anchor_content_hash(anchor, root) {
        Some(current) if current == capture => AnchorDrift::Unchanged,
        Some(_) => AnchorDrift::Drifted,
        None => AnchorDrift::Unknown,
    }
}

/// Relative relevance of `score` within a set whose top score is `top`:
/// `score / top` (both negative, so the ratio is positive and the top
/// hit is exactly `1.0`), clamped into `0.0..=1.0` and rounded to 2
/// decimals. Pure arithmetic — deterministic bytes on every host. FTS5
/// clamps idf strictly positive, so a match score is strictly negative
/// and `top` is nonzero; the zero guard still makes the function total
/// (no NaN/inf can ever reach the envelope).
fn relevance_of(score: f64, top: f64) -> f64 {
    if top == 0.0 {
        return 1.0;
    }
    ((score / top).clamp(0.0, 1.0) * 100.0).round() / 100.0
}

/// Wire form of a bm25 score: 3 significant digits, via the decimal
/// formatter (`{:.2e}`) and re-parse — both correctly rounded and
/// platform-independent (no libm), so recall stays byte-deterministic.
/// The fallback to the raw value is unreachable for the finite scores
/// SQLite produces; it only keeps the function total.
fn rounded_bm25(score: f64) -> f64 {
    format!("{score:.2e}").parse().unwrap_or(score)
}

/// Round a cosine similarity to 4 decimals for the wire (`vector_similarity`
/// explain) — deterministic on every host, like [`round2`].
fn round4(value: f64) -> f64 {
    (value * 10_000.0).round() / 10_000.0
}

/// Deterministic cosine similarity of two equal-length vectors (w3 u6a):
/// `dot(a,b) / (‖a‖·‖b‖)`, in `-1.0..=1.0`. Computed as a fixed
/// left-to-right `f64` fold in index order, so the bytes are identical on
/// every host. The caller-side dimension check upstream guarantees
/// `a.len() == b.len()`; the zero-magnitude guard keeps the function total
/// (validation already refuses zero-norm vectors, so `0.0` here is only the
/// unreachable safety floor — never a NaN into fusion).
fn cosine_similarity(a: &[f32], b: &[f32]) -> f64 {
    let mut dot = 0.0f64;
    let mut norm_a = 0.0f64;
    let mut norm_b = 0.0f64;
    for (x, y) in a.iter().zip(b.iter()) {
        let x = f64::from(*x);
        let y = f64::from(*y);
        dot += x * y;
        norm_a += x * x;
        norm_b += y * y;
    }
    let denom = norm_a.sqrt() * norm_b.sqrt();
    if denom == 0.0 { 0.0 } else { dot / denom }
}

/// The vector-lane sort key — a candidate's cosine, or `-inf` when it has
/// none (never happens for the filtered vector lane; keeps the comparator
/// total).
fn cosine_key(c: &Candidate) -> f64 {
    c.cosine.unwrap_or(f64::NEG_INFINITY)
}

/// The FTS-lane sort key of a candidate — its bm25 score, or the worst
/// possible value when it has none (a vector-only match, filtered out
/// before this is used). A dormant candidate always has `Some`, so the
/// dormant sort is byte-identical to the pre-u6a `a.score.total_cmp(...)`.
fn fts_score_key(c: &Candidate) -> f64 {
    c.score.unwrap_or(f64::MAX)
}

/// The deterministic FTS ranking order (module doc): coverage desc, bm25
/// asc, decayed weight desc, valid_from desc, the h4 late usage key, id
/// asc. Shared by the dormant sort and the fused FTS-lane rank so the two
/// can never drift.
fn fts_rank_key(a: &Candidate, b: &Candidate) -> Ordering {
    b.coverage
        .cmp(&a.coverage)
        .then_with(|| fts_score_key(a).total_cmp(&fts_score_key(b)))
        .then_with(|| b.decayed.total_cmp(&a.decayed))
        .then_with(|| {
            b.stored
                .capsule
                .freshness()
                .valid_from
                .cmp(&a.stored.capsule.freshness().valid_from)
        })
        .then_with(|| {
            let (recency_a, count_a) = usage_key(a.usage);
            let (recency_b, count_b) = usage_key(b.usage);
            recency_b
                .cmp(&recency_a)
                .then_with(|| count_b.cmp(&count_a))
        })
        .then_with(|| a.stored.seq.cmp(&b.stored.seq))
}

/// Validate a caller's `query_embedding` for cosine (w3 u6a): non-empty,
/// finite, non-zero magnitude — a caller-side fault taught with a teaching
/// [`RetrieveError::InvalidQueryEmbedding`] rather than silently dropped or
/// allowed to emit a NaN into fusion.
fn validate_query_embedding(embedding: &[f32]) -> Result<(), RetrieveError> {
    if embedding.is_empty() {
        return Err(RetrieveError::InvalidQueryEmbedding(
            "is empty (dimension 0)".to_string(),
        ));
    }
    if let Some(bad) = embedding.iter().position(|v| !v.is_finite()) {
        return Err(RetrieveError::InvalidQueryEmbedding(format!(
            "component {bad} is not finite (NaN or +/-inf)"
        )));
    }
    let sum_sq: f64 = embedding
        .iter()
        .map(|v| f64::from(*v) * f64::from(*v))
        .sum();
    if sum_sq == 0.0 {
        return Err(RetrieveError::InvalidQueryEmbedding(
            "has zero magnitude (all components zero)".to_string(),
        ));
    }
    Ok(())
}

/// Apply the eligibility fences to ONE raw match — the LANE-AGNOSTIC gate
/// (w3 u6a): the SAME dominance (quarantined, then falsified, then
/// archived, then superseded, then currency, then optional fact time) runs
/// for an FTS match and a vector match, so a fenced capsule can never surface
/// via either lane.
/// Returns the built [`Candidate`] when the match survives, or `None` after
/// counting the exclusion under the first fence that caught it.
// The fence pipeline threads the raw match, the store seam, and the four
// query-shaped inputs (now, time_window, include_staged, the excluded tally)
// through one gate; bundling them would only rename the same eight values.
#[allow(clippy::too_many_arguments)]
fn fence_candidate<S: RecallStore>(
    stored: StoredCapsule,
    lane_match: LaneMatch,
    store: &S,
    groups: &[TermGroup],
    now: OffsetDateTime,
    time_window: Option<&TimeWindow>,
    include_staged: bool,
    excluded: &mut BTreeMap<ExclusionReason, usize>,
) -> Result<Option<Candidate>, RetrieveError> {
    // Dominance-ordered fences (u6h-extended law): the tier is read once
    // and probed at its two dominance positions.
    let tier = store.get_tier(stored.id.as_str())?;
    // Quarantine fence (FIRST — the taint signal dominates everything).
    if matches!(tier, Tier::Quarantined) {
        *excluded.entry(ExclusionReason::Quarantined).or_insert(0) += 1;
        return Ok(None);
    }
    // Falsified fence (u6h, SECOND — ABOVE archived/superseded): an
    // observed outcome contradicts this claim, so it stops grounding recall
    // — its bytes untouched, still served by get/list (eligibility, never
    // history). A falsified fact must never hide behind a softer lifecycle
    // bucket; only quarantine outranks it.
    if store.is_falsified(stored.id.as_str())? {
        *excluded.entry(ExclusionReason::Falsified).or_insert(0) += 1;
        return Ok(None);
    }
    // Archive fence (THIRD).
    if matches!(tier, Tier::Archived) {
        *excluded.entry(ExclusionReason::Archived).or_insert(0) += 1;
        return Ok(None);
    }
    // Superseded fence (h4): replaced capsules never ground recall by
    // default — excluded, not erased (get/list still return them).
    if store.is_superseded(stored.id.as_str())? {
        *excluded.entry(ExclusionReason::Superseded).or_insert(0) += 1;
        return Ok(None);
    }
    // Currency fence: outside the validity window at `now`.
    if let Some(reason) = currency_exclusion(stored.capsule.freshness(), now) {
        *excluded.entry(reason).or_insert(0) += 1;
        return Ok(None);
    }
    // Fact-time fence (LAST): state and currency always dominate this
    // query-shaped filter. A range intersects inclusively; only a strict
    // gap excludes it. An undated capsule cannot claim membership in a
    // caller-supplied window, so it gets its own visible count.
    if let Some(window) = time_window {
        let Some(event) = store.event_time_of(stored.id.as_str())? else {
            *excluded.entry(ExclusionReason::Undated).or_insert(0) += 1;
            return Ok(None);
        };
        let starts_after = window.to().is_some_and(|to| event.event_from() > to);
        let ends_before = window.from().is_some_and(|from| event.event_to() < from);
        if starts_after || ends_before {
            *excluded
                .entry(ExclusionReason::OutsideTimeWindow)
                .or_insert(0) += 1;
            return Ok(None);
        }
    }
    // Staged-review fence (b2, ABSOLUTE LAST): a standing proposal (latest
    // review verdict not `ratified`) is fenced from grounding unless the
    // caller opted into `include_staged`; every quality and query fence above
    // DOMINATES it. `review_fenced` reads a cheap indexed projection per
    // candidate (like is_superseded / is_falsified), so a store with no
    // proposals answers `false` for all and the output stays byte-identical.
    // An INCLUDED proposal surfaces its standing verdict on the envelope — a
    // second, rare read only on the included branch.
    let review_state = if store.review_fenced(stored.id.as_str())? {
        if !include_staged {
            *excluded.entry(ExclusionReason::Proposed).or_insert(0) += 1;
            return Ok(None);
        }
        store.review_verdict(stored.id.as_str())?
    } else {
        None
    };
    let usage = store.usage_of(stored.id.as_str())?;
    let coverage = matched_groups(stored.capsule.content(), groups).len();
    // Pin decay-exemption AT THE CALL SITE (S1): a pinned capsule ranks by
    // its FULL confidence — decay never erodes a load-bearing anchor. This
    // is NOT eligibility: every fence above already ran, so a pinned+
    // superseded/quarantined/falsified capsule is already excluded; a pinned
    // row that reaches here passed the SAME gate as any other. `decay_weight`
    // stays byte-untouched (shared with bootstrap ranking, u-r9 "never a
    // drifting second copy"). Zero pins ⇒ the else-arm exactly as before
    // (byte-identical dormancy). Unpin resumes decay from `valid_from`.
    let decayed = if store.is_pinned(stored.id.as_str())? {
        stored.capsule.confidence().value()
    } else {
        decay_weight(
            stored.capsule.confidence().value(),
            stored.capsule.freshness().valid_from,
            now,
        )
    };
    Ok(Some(Candidate {
        coverage,
        decayed,
        stored,
        score: lane_match.score,
        cosine: lane_match.cosine,
        fusion_rank: None,
        feedback_weight: None,
        corroboration_weight: None,
        usage,
        review_state,
    }))
}

/// First line of `content`, capped at [`HEADLINE_MAX_CHARS`] chars, with
/// `…` appended whenever anything (rest of the line or further lines)
/// was left out. The line boundary set is CRLF plus the Unicode newline
/// controls LF, CR, VT, FF, NEL, LS, and PS. A lone trailing boundary is
/// not content, so it never earns the ellipsis (w3 review: false `…` on
/// `"x\n"`).
///
/// `pub(crate)`: the ONE headline law across surfaces — `export` renders
/// through this same fn (v7 convergence), so the two windows can never
/// tell two stories about one first line.
pub(crate) fn headline_of(content: &str) -> String {
    let boundary = content.char_indices().find_map(|(index, ch)| {
        let boundary_len = match ch {
            '\r' if content[index..].starts_with("\r\n") => 2,
            '\n' | '\r' | '\u{000b}' | '\u{000c}' | '\u{0085}' | '\u{2028}' | '\u{2029}' => {
                ch.len_utf8()
            }
            _ => return None,
        };
        Some((index, boundary_len))
    });
    let (first_line, more_content) = match boundary {
        Some((index, boundary_len)) => (&content[..index], index + boundary_len < content.len()),
        None => (content, false),
    };
    let headline: String = first_line.chars().take(HEADLINE_MAX_CHARS).collect();
    let cut_line = first_line.chars().count() > HEADLINE_MAX_CHARS;
    if cut_line || more_content {
        format!("{headline}…")
    } else {
        headline
    }
}

/// Lowercased, diacritic-folded alphanumeric tokens — the simplified
/// explain-side mirror of FTS5's `unicode61 remove_diacritics` (see
/// [`Evidence::matched_terms`] for the residual delta).
fn tokens(text: &str) -> Vec<String> {
    text.to_lowercase()
        .split(|c: char| !c.is_alphanumeric())
        .filter(|s| !s.is_empty())
        .map(|s| s.chars().map(fold_diacritic).collect())
        .collect()
}

/// Which term GROUPS ground this content, in the caller's own term
/// order (module doc: Synonym expansion): the plain term when the term
/// itself matches (all its tokens appear — order- and
/// adjacency-insensitive, mirroring the store's within-term AND match),
/// else `alias:<term>` when any recorded alias fully matches.
/// Comparison is on folded tokens, so accent-variant terms are
/// attributed too. The entry count IS the coverage rank key.
fn matched_groups(content: &str, groups: &[TermGroup]) -> Vec<String> {
    let content_tokens: std::collections::BTreeSet<String> = tokens(content).into_iter().collect();
    let mut out = Vec::new();
    for group in groups {
        if term_matches(&content_tokens, &group.term) {
            out.push(group.term.clone());
        } else if group
            .aliases
            .iter()
            .any(|alias| term_matches(&content_tokens, alias))
        {
            out.push(format!("alias:{}", group.term));
        }
    }
    out
}

/// All of `term`'s folded tokens appear in the content token set
/// (order- and adjacency-insensitive — the store's within-term AND
/// match, mirrored).
fn term_matches(content_tokens: &std::collections::BTreeSet<String>, term: &str) -> bool {
    let term_tokens = tokens(term);
    !term_tokens.is_empty() && term_tokens.iter().all(|t| content_tokens.contains(t))
}

/// Fold a whole term for alias/search-list dedup the same way the
/// explain tokenizer folds content: lowercase + Latin diacritic fold.
fn folded(term: &str) -> String {
    term.to_lowercase().chars().map(fold_diacritic).collect()
}

/// Token approximation for budget accounting: `chars / 4`, rounded up.
/// `pub(crate)` so `memory_bootstrap` costs its pack rows on the SAME
/// approximation retrieve's token budget uses (u-r9) — one budget arithmetic.
pub(crate) fn approx_tokens(text: &str) -> usize {
    text.chars().count().div_ceil(4)
}

#[cfg(test)]
mod tests {
    #![allow(
        clippy::unwrap_used,
        clippy::expect_used,
        reason = "tests use unwrap/expect so fixture failures fail at the assertion site"
    )]

    use super::*;
    use crate::capsule::{Capsule, Scope, sha256_hex};
    use crate::store::{EventTimeRange, RelationKind, TombstoneMode};
    use std::cell::Cell;
    use time::macros::datetime;

    /// Injected query instant — retrieve reads no clock.
    const NOW: OffsetDateTime = datetime!(2026-07-18 20:00:00 UTC);

    /// Test seam for the public entry (this fn item shadows the
    /// glob-imported [`super::retrieve`]): every recall in this module
    /// injects the SAME hermetic, nonexistent anchor root, so probe
    /// verdicts are box-independent — a relative `path:line` anchor
    /// reads missing, everything else unknown. Tests that need a LIVE
    /// root call [`retrieve_core`] with a temp dir instead.
    fn retrieve(
        store: &mut Store,
        query: &RetrieveQuery,
        now: OffsetDateTime,
    ) -> Result<RetrieveResponse, RetrieveError> {
        super::retrieve(
            store,
            query,
            now,
            Path::new("/nmemory-hermetic-test-anchor-root"),
        )
    }

    /// Default fixture validity start, safely before [`NOW`].
    const VF: OffsetDateTime = datetime!(2026-07-18 12:00:00 UTC);
    /// Injected append instant (any fixed value works — never read back
    /// by retrieve).
    const APPENDED: OffsetDateTime = datetime!(2026-07-18 12:00:01 UTC);

    fn cap(
        content: &str,
        project: &str,
        confidence: f64,
        valid_from: OffsetDateTime,
        valid_to: Option<OffsetDateTime>,
    ) -> Capsule {
        Capsule::new(
            content.to_string(),
            Provenance {
                source: "session:2026-07-18".to_string(),
                anchor: "PLAN.md:88".to_string(),
                source_hash: sha256_hex(content.as_bytes()),
            },
            Confidence::new(confidence).unwrap(),
            Freshness {
                valid_from,
                valid_to,
            },
            Scope {
                project_id: project.to_string(),
            },
            AuthorityClass::UserStated,
            false,
        )
        .unwrap()
    }

    fn query(terms: &[&str]) -> RetrieveQuery {
        RetrieveQuery {
            terms: terms.iter().map(|t| (*t).to_string()).collect(),
            ..RetrieveQuery::default()
        }
    }

    #[test]
    fn module_and_pipeline_docs_pin_all_lane_ranking_contracts() {
        let source = include_str!("retrieve.rs");
        let module_docs = source
            .split("\nuse std::")
            .next()
            .expect("module documentation prefix");
        let pipeline_docs = source
            .split("/// Run one recall pass over the store")
            .nth(1)
            .and_then(|tail| tail.split("pub fn retrieve(").next())
            .expect("public retrieve pipeline documentation");
        let normalize = |docs: &str| {
            docs.replace("//!", " ")
                .replace("///", " ")
                .split_whitespace()
                .collect::<Vec<_>>()
                .join(" ")
        };
        let module_docs = normalize(module_docs);
        let pipeline_docs = normalize(pipeline_docs);

        assert!(module_docs.contains(concat!(
            "deterministic local term and caller-fed ",
            "vector matching"
        )));
        for statement in [
            concat!(
                "Term-only ranking uses coverage descending, then ",
                "bm25 ascending"
            ),
            concat!(
                "Forced-vector ranking uses one-lane RRF over ",
                "cosine rank"
            ),
            concat!(
                "Fused ranking uses two-lane RRF over the independent ",
                "term and vector ranks"
            ),
        ] {
            assert!(
                module_docs.contains(statement),
                "module docs omit {statement:?}"
            );
            assert!(
                pipeline_docs.contains(statement),
                "pipeline docs omit {statement:?}"
            );
        }
    }

    fn grounded_ids(response: &RetrieveResponse) -> Vec<String> {
        match response {
            RetrieveResponse::Grounded { results, .. } => {
                results.iter().map(|r| r.id.to_string()).collect()
            }
            other => panic!("expected grounded, got: {other:?}"),
        }
    }

    /// w2-store2 contract test double: a REAL store underneath (real
    /// FTS, usage, tombstones, supersedes) plus contract-true sidecar
    /// data for the two calls this base predates (`aliases_for`,
    /// `get_tier`) — so the full pipeline is driven with store2
    /// semantics before store2 lands.
    struct ContractStore {
        inner: Store,
        aliases: BTreeMap<String, Vec<String>>,
        tiers: BTreeMap<String, Tier>,
        event_time_reads: Cell<usize>,
        reject_event_time_reads: bool,
        corroboration_weights: BTreeMap<String, f64>,
        corroboration_ranking_reads: Cell<usize>,
    }

    impl ContractStore {
        fn new(inner: Store) -> Self {
            ContractStore {
                inner,
                aliases: BTreeMap::new(),
                tiers: BTreeMap::new(),
                event_time_reads: Cell::new(0),
                reject_event_time_reads: false,
                corroboration_weights: BTreeMap::new(),
                corroboration_ranking_reads: Cell::new(0),
            }
        }
    }

    impl RecallStore for ContractStore {
        fn search_fts(
            &self,
            terms: &[String],
            project_id: Option<&str>,
            project_prefix: Option<&str>,
            session_id: Option<&str>,
            effort_ids: Option<&[String]>,
        ) -> Result<Vec<(StoredCapsule, f64)>, StoreError> {
            self.inner
                .search_fts_effort(terms, project_id, project_prefix, session_id, effort_ids)
        }
        fn get_tombstone(&self, id: &str) -> Result<Option<TombstoneRecord>, StoreError> {
            self.inner.get_tombstone(id)
        }
        fn get_tombstone_for_session_label(
            &self,
            id: &str,
            session_id: &str,
        ) -> Result<Option<TombstoneRecord>, StoreError> {
            self.inner.get_tombstone_for_session_label(id, session_id)
        }
        fn is_superseded(&self, id: &str) -> Result<bool, StoreError> {
            self.inner.is_superseded(id)
        }
        fn is_falsified(&self, id: &str) -> Result<bool, StoreError> {
            self.inner.is_falsified(id)
        }
        fn is_pinned(&self, id: &str) -> Result<bool, StoreError> {
            // Real delegation (S1): the pin sidecar lives on the inner Store,
            // so the decay-exemption seam reads it exactly as production.
            self.inner.is_pinned(id)
        }
        fn usage_of(&self, id: &str) -> Result<Option<UsageStat>, StoreError> {
            self.inner.usage_of(id)
        }
        fn record_recall(&mut self, ids: &[&str], now: OffsetDateTime) -> Result<(), StoreError> {
            self.inner.record_recall(ids, now)
        }
        fn aliases_for(&self, term: &str) -> Result<Vec<String>, StoreError> {
            // Contract: the store normalizes lookups the way it
            // normalizes writes (lowercase + diacritic fold).
            Ok(self.aliases.get(&folded(term)).cloned().unwrap_or_default())
        }
        fn get_tier(&self, id: &str) -> Result<Tier, StoreError> {
            Ok(self.tiers.get(id).copied().unwrap_or(Tier::Active))
        }
        fn embeddings_for_recall(
            &self,
            project_id: Option<&str>,
            project_prefix: Option<&str>,
            session_id: Option<&str>,
            effort_ids: Option<&[String]>,
        ) -> Result<Vec<(StoredCapsule, StoredEmbedding)>, StoreError> {
            // Real delegation: the vector sidecar lives on the inner Store,
            // so contract tests populate it with `inner.put_embedding` and
            // the engine reads it through this seam exactly as production.
            self.inner.embeddings_for_recall_effort(
                project_id,
                project_prefix,
                session_id,
                effort_ids,
            )
        }
        fn anchor_hash_of(&self, id: &str) -> Result<Option<String>, StoreError> {
            // Real delegation (u-r2): the sidecar lives on the inner Store.
            self.inner.anchor_hash_of(id)
        }
        fn epistemics_of(&self, id: &str) -> Result<Option<EpistemicsRecord>, StoreError> {
            self.inner.epistemics_of(id)
        }
        fn feedback_weight_of(&self, id: &str) -> Result<Option<f64>, StoreError> {
            self.inner.feedback_weight_of(id)
        }
        fn event_time_of(&self, id: &str) -> Result<Option<EventTimeRecord>, StoreError> {
            self.event_time_reads
                .set(self.event_time_reads.get().saturating_add(1));
            if self.reject_event_time_reads {
                return Err(StoreError::Backend(
                    "event_time must not be read on this path".to_string(),
                ));
            }
            self.inner.event_time_of(id)
        }
        fn review_fenced(&self, id: &str) -> Result<bool, StoreError> {
            self.inner.review_fenced(id)
        }
        fn review_verdict(&self, id: &str) -> Result<Option<String>, StoreError> {
            self.inner.review_verdict(id)
        }
        fn latest_corroborations_of(
            &self,
            id: &str,
        ) -> Result<Option<CorroborationSummary>, StoreError> {
            // Real delegation (S2): the sidecar lives on the inner Store.
            self.inner.latest_corroborations(id)
        }
        // S6 counting seam: every corroboration read is tallied so the
        // dormant-path tests can prove zero reads, and the fixture map lets a
        // test inject known git-witness weights (drifted 0.0 / corroborated
        // 1.0 / absent → None → neutral).
        fn corroboration_weight(&self, id: &CapsuleId) -> Option<f64> {
            self.corroboration_ranking_reads
                .set(self.corroboration_ranking_reads.get().saturating_add(1));
            self.corroboration_weights.get(id.as_str()).copied()
        }
    }

    #[test]
    fn multi_term_recall_finds_planted_capsules() {
        let mut store = Store::open_in_memory().unwrap();
        store
            .append(
                &cap(
                    "the nmemory store is single-file sqlite with wal",
                    "nott",
                    0.9,
                    VF,
                    None,
                ),
                APPENDED,
            )
            .unwrap();
        store
            .append(
                &cap(
                    "recall is grounded or abstain, never fabricated",
                    "nott",
                    0.9,
                    VF,
                    None,
                ),
                APPENDED,
            )
            .unwrap();
        store
            .append(
                &cap(
                    "the spool organ persists with fsync temp files",
                    "nott",
                    0.9,
                    VF,
                    None,
                ),
                APPENDED,
            )
            .unwrap();

        // Caller-expanded multi-term query: OR across terms.
        let response = retrieve(&mut store, &query(&["sqlite", "grounded"]), NOW).unwrap();
        let mut ids = grounded_ids(&response);
        ids.sort();
        assert_eq!(ids, ["cap-1", "cap-2"]);

        let RetrieveResponse::Grounded {
            results,
            matched,
            returned,
            trimmed,
            ..
        } = &response
        else {
            panic!("expected grounded");
        };
        assert_eq!((*matched, *returned, *trimmed), (2, 2, 0));
        // Per-result explain names the term that grounded it.
        for result in results {
            let expected_term = if result.id.as_str() == "cap-1" {
                "sqlite"
            } else {
                "grounded"
            };
            assert_eq!(result.matched_terms, [expected_term]);
        }
    }

    #[test]
    fn multi_word_terms_match_unordered_and_rank_by_coverage() {
        // w1d stress fix: "tokio pin" must find "pin tokio at 1.38.0"
        // (within-term AND, order/adjacency-insensitive) instead of
        // silently abstaining on the phrase.
        let mut store = Store::open_in_memory().unwrap();
        store
            .append(
                &cap(
                    "Decision: pin tokio at 1.38.0 because 1.39 broke our io_uring feature gate",
                    "nott",
                    0.9,
                    VF,
                    None,
                ),
                APPENDED,
            )
            .unwrap();
        store
            .append(
                &cap("the autoscaler had doubled replicas", "nott", 0.9, VF, None),
                APPENDED,
            )
            .unwrap();

        let response = retrieve(&mut store, &query(&["tokio pin", "pinned tokio"]), NOW).unwrap();
        assert_eq!(grounded_ids(&response), ["cap-1"]);
        let RetrieveResponse::Grounded { results, .. } = &response else {
            panic!("expected grounded");
        };
        // Both rephrasings attribute: all their words appear in cap-1.
        assert_eq!(results[0].matched_terms, ["tokio pin"]);

        let response = retrieve(&mut store, &query(&["autoscaler doubled replicas"]), NOW).unwrap();
        assert_eq!(grounded_ids(&response), ["cap-2"]);

        // Coverage outranks single-term bm25: a capsule matching two
        // distinct terms sorts above one matching only a generic term.
        store
            .append(&cap("replicas replicas", "nott", 0.9, VF, None), APPENDED)
            .unwrap();
        let response = retrieve(
            &mut store,
            &query(&["autoscaler", "doubled", "replicas"]),
            NOW,
        )
        .unwrap();
        assert_eq!(
            grounded_ids(&response)[0],
            "cap-2",
            "the three-term-covering capsule ranks first"
        );
    }

    #[test]
    fn accent_folded_matches_are_attributed_in_matched_terms() {
        // w1d stress fix: FTS grounds "configuracao" onto "configuração"
        // (unicode61 remove_diacritics) — the explain must agree instead
        // of answering matched_terms: [].
        let mut store = Store::open_in_memory().unwrap();
        store
            .append(
                &cap(
                    "a configuração de memória do zayout fica no boot",
                    "nott",
                    0.9,
                    VF,
                    None,
                ),
                APPENDED,
            )
            .unwrap();
        let response = retrieve(&mut store, &query(&["configuracao", "orbita"]), NOW).unwrap();
        let RetrieveResponse::Grounded { results, .. } = &response else {
            panic!("expected grounded, got {response:?}");
        };
        assert_eq!(results[0].matched_terms, ["configuracao"]);
        // And the accented spelling still attributes too.
        let response = retrieve(&mut store, &query(&["configuração"]), NOW).unwrap();
        let RetrieveResponse::Grounded { results, .. } = &response else {
            panic!("expected grounded");
        };
        assert_eq!(results[0].matched_terms, ["configuração"]);
    }

    #[test]
    fn no_match_abstains_never_fabricates() {
        let mut store = Store::open_in_memory().unwrap();
        store
            .append(&cap("alpha beta gamma", "nott", 0.9, VF, None), APPENDED)
            .unwrap();

        let response = retrieve(&mut store, &query(&["zzz", "qqq"]), NOW).unwrap();
        let RetrieveResponse::Abstain { reason, .. } = &response else {
            panic!("expected abstain, got: {response:?}");
        };
        assert!(
            reason.contains("abstaining instead of fabricating"),
            "honest reason, got: {reason}"
        );

        // Wire shape: tagged outcome, no results field at all, and none
        // of the missing_evidence exclusion fields (zero raw matches ≠
        // matched-but-excluded).
        let value = serde_json::to_value(&response).unwrap();
        assert_eq!(value["outcome"], "abstain");
        assert!(value["reason"].is_string());
        assert!(value.get("results").is_none());
        assert!(
            value.get("excluded_count").is_none() && value.get("excluded").is_none(),
            "abstain carries no exclusion fields"
        );
    }

    #[test]
    fn grounded_recall_mints_a_resolvable_receipt() {
        let mut store = Store::open_in_memory().unwrap();
        store
            .append(
                &cap("sqlite recall receipt alpha", "nott", 0.9, VF, None),
                APPENDED,
            )
            .unwrap();
        store
            .append(
                &cap("sqlite recall receipt beta", "nott", 0.9, VF, None),
                APPENDED,
            )
            .unwrap();
        let response = retrieve(&mut store, &query(&["sqlite", "receipt"]), NOW).unwrap();
        let RetrieveResponse::Grounded {
            receipt_id,
            results,
            ..
        } = &response
        else {
            panic!("expected grounded, got: {response:?}");
        };
        assert_eq!(receipt_id.as_deref(), Some("rcpt-1"));
        let returned: Vec<String> = results
            .iter()
            .map(|result| result.id.as_str().to_string())
            .collect();
        assert_eq!(
            store.receipt_returned_ids("rcpt-1").unwrap(),
            Some(returned),
            "the public response id resolves to its exact returned capsule order"
        );
    }

    #[test]
    fn ungrounded_outcomes_mint_no_receipt() {
        let mut store = Store::open_in_memory().unwrap();
        store
            .append(
                &cap(
                    "expired receipt probe",
                    "nott",
                    0.9,
                    datetime!(2026-07-01 00:00:00 UTC),
                    Some(datetime!(2026-07-10 00:00:00 UTC)),
                ),
                APPENDED,
            )
            .unwrap();

        let abstain = retrieve(&mut store, &query(&["never-stored"]), NOW).unwrap();
        let missing = retrieve(&mut store, &query(&["expired", "probe"]), NOW).unwrap();
        assert!(matches!(abstain, RetrieveResponse::Abstain { .. }));
        assert!(matches!(missing, RetrieveResponse::MissingEvidence { .. }));
        for response in [&abstain, &missing] {
            let raw = serde_json::to_string(response).unwrap();
            assert!(
                !raw.contains("receipt_id"),
                "ungrounded response must remain receipt-dormant: {raw}"
            );
        }
        assert_eq!(
            store.receipt_returned_ids("rcpt-1").unwrap(),
            None,
            "neither ungrounded outcome mints a receipt"
        );
    }

    #[test]
    fn zero_result_grounded_still_mints_a_receipt() {
        let mut store = Store::open_in_memory().unwrap();
        store
            .append(
                &cap("zero result receipt probe", "nott", 0.9, VF, None),
                APPENDED,
            )
            .unwrap();
        let mut probe = query(&["receipt", "probe"]);
        probe.limit = Some(0);

        let response = retrieve(&mut store, &probe, NOW).unwrap();
        let RetrieveResponse::Grounded {
            receipt_id,
            results,
            matched,
            returned,
            ..
        } = &response
        else {
            panic!("expected grounded count-only probe, got: {response:?}");
        };
        assert_eq!(receipt_id.as_deref(), Some("rcpt-1"));
        assert_eq!((*matched, *returned), (1, 0));
        assert!(results.is_empty());
        assert_eq!(
            store.receipt_returned_ids("rcpt-1").unwrap(),
            Some(Vec::new()),
            "zero-result grounded receipt resolves to the empty returned-id array"
        );
    }

    #[test]
    fn engine_direct_grounded_stays_receipt_dormant() {
        let mut store = Store::open_in_memory().unwrap();
        store
            .append(
                &cap("engine direct receipt probe", "nott", 0.9, VF, None),
                APPENDED,
            )
            .unwrap();

        let response = retrieve_core(
            &mut store,
            &query(&["receipt", "probe"]),
            NOW,
            Path::new("/nmemory-hermetic-test-anchor-root"),
        )
        .unwrap();
        let RetrieveResponse::Grounded { receipt_id, .. } = &response else {
            panic!("expected grounded, got: {response:?}");
        };
        assert_eq!(receipt_id, &None);
        assert!(
            !serde_json::to_string(&response)
                .unwrap()
                .contains("receipt_id")
        );
        assert_eq!(store.receipt_returned_ids("rcpt-1").unwrap(), None);
    }

    #[test]
    fn stale_matches_yield_missing_evidence_and_stay_reachable_via_get() {
        let mut store = Store::open_in_memory().unwrap();
        // Expired well before NOW.
        store
            .append(
                &cap(
                    "stale fact about the sqlite index",
                    "nott",
                    0.9,
                    datetime!(2026-07-01 00:00:00 UTC),
                    Some(datetime!(2026-07-10 00:00:00 UTC)),
                ),
                APPENDED,
            )
            .unwrap();
        // Not yet valid at NOW.
        store
            .append(
                &cap(
                    "future fact about the sqlite index",
                    "nott",
                    0.9,
                    datetime!(2026-08-01 00:00:00 UTC),
                    None,
                ),
                APPENDED,
            )
            .unwrap();

        let response = retrieve(&mut store, &query(&["sqlite"]), NOW).unwrap();
        let RetrieveResponse::MissingEvidence {
            excluded_count,
            excluded,
            reason,
            ..
        } = &response
        else {
            panic!("expected missing_evidence, got: {response:?}");
        };
        assert_eq!(*excluded_count, 2);
        assert_eq!(
            excluded,
            &BTreeMap::from([
                (ExclusionReason::Expired, 1),
                (ExclusionReason::NotYetValid, 1),
            ])
        );
        assert!(
            reason.contains("1 expired") && reason.contains("1 not_yet_valid"),
            "honest per-reason counts, got: {reason}"
        );
        assert!(
            reason.contains("2 capsule(s)"),
            "names the lexical matches, got: {reason}"
        );

        // Layered recall: the capsules themselves are not hidden.
        assert!(store.get("cap-1").unwrap().is_some());
        assert!(store.get("cap-2").unwrap().is_some());
    }

    /// u-r5: the retrieve path records a miss ledger row for the ungrounded
    /// outcomes (abstain, missing_evidence) and NOTHING for a grounded one
    /// — misses teach vocabulary. Recording uses the raw caller terms (the
    /// store folds them); `now` is injected and the ledger is a pure side
    /// effect that never changes the returned response.
    #[test]
    fn retrieve_records_a_miss_on_ungrounded_outcomes_never_on_grounded() {
        let mut store = Store::open_in_memory().unwrap();
        // cap-1 live and matchable; cap-2 expired well before NOW.
        store
            .append(
                &cap("the sqlite index is grounded here", "nott", 0.9, VF, None),
                APPENDED,
            )
            .unwrap();
        store
            .append(
                &cap(
                    "stale postgres fact",
                    "nott",
                    0.9,
                    datetime!(2026-07-01 00:00:00 UTC),
                    Some(datetime!(2026-07-10 00:00:00 UTC)),
                ),
                APPENDED,
            )
            .unwrap();

        // Grounded → records NOTHING.
        let grounded = retrieve(&mut store, &query(&["sqlite"]), NOW).unwrap();
        assert!(matches!(grounded, RetrieveResponse::Grounded { .. }));
        assert!(
            store.recall_miss_terms().unwrap().is_empty(),
            "a grounded query teaches no vocabulary"
        );

        // Abstain (nothing matched) → records the folded query term.
        let abstain = retrieve(&mut store, &query(&["Retreival"]), NOW).unwrap();
        assert!(matches!(abstain, RetrieveResponse::Abstain { .. }));

        // Missing_evidence (the expired match is fenced) → records too.
        let missing = retrieve(&mut store, &query(&["postgres"]), NOW).unwrap();
        assert!(matches!(missing, RetrieveResponse::MissingEvidence { .. }));

        assert_eq!(
            store.recall_miss_terms().unwrap(),
            vec![("postgres".to_string(), 1), ("retreival".to_string(), 1)],
            "both ungrounded outcomes recorded (folded); grounded added nothing"
        );
        assert_eq!(store.count_recall_misses().unwrap(), 2);
    }

    /// u-r5 FAIL-OPEN: a broken miss ledger (the write target dropped out
    /// from under the store, mirroring `fts_drop_then_rebuild`) never fails
    /// or delays recall — the ledger error is swallowed at the retrieve
    /// boundary, and recall still answers its honest Abstain.
    #[test]
    fn a_broken_miss_ledger_never_fails_recall() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("memory.sqlite3");
        let mut store = Store::open(&path).unwrap();
        store
            .append(&cap("grounded content", "nott", 0.9, VF, None), APPENDED)
            .unwrap();

        // Break the ledger from a second connection to the same file.
        let raw = rusqlite::Connection::open(&path).unwrap();
        raw.execute_batch("DROP TABLE recall_misses").unwrap();
        drop(raw);

        // The ungrounded write now fails — recall must still succeed.
        let response = retrieve(&mut store, &query(&["absent-term"]), NOW).unwrap();
        assert!(
            matches!(response, RetrieveResponse::Abstain { .. }),
            "the ledger failure is swallowed; recall answers honestly"
        );
    }

    #[test]
    fn forgotten_id_term_yields_missing_evidence_tombstoned_never_abstain() {
        let mut store = Store::open_in_memory().unwrap();
        store
            .append(
                &cap("ephemeral secret fact to forget", "nott", 0.9, VF, None),
                APPENDED,
            )
            .unwrap();
        store
            .forget_capsule(
                "cap-1",
                crate::store::TombstoneMode::Purged,
                "smoke: owner asked",
                b"test-hmac-key",
                APPENDED,
            )
            .unwrap();

        // Content is gone by design: the former topic word matches nothing
        // on its own — honest abstain, not a fabricated tombstone report.
        let by_content = retrieve(&mut store, &query(&["ephemeral"]), NOW).unwrap();
        assert!(
            matches!(by_content, RetrieveResponse::Abstain { .. }),
            "content of a forgotten capsule can never match again, got: {by_content:?}"
        );

        // A term NAMING the forgotten id is the probe channel: raw match,
        // excluded as tombstoned — it was the only match.
        let response = retrieve(&mut store, &query(&["ephemeral", "cap-1"]), NOW).unwrap();
        let RetrieveResponse::MissingEvidence {
            excluded_count,
            excluded,
            reason,
            ..
        } = &response
        else {
            panic!("expected missing_evidence, got: {response:?}");
        };
        assert_eq!(*excluded_count, 1);
        assert_eq!(
            excluded,
            &BTreeMap::from([(ExclusionReason::Tombstoned, 1)])
        );
        assert!(
            reason.contains("1 tombstoned"),
            "honest tombstone count, got: {reason}"
        );
        // Not one forgotten byte reaches the response.
        let serialized = serde_json::to_string(&response).unwrap();
        assert!(!serialized.contains("ephemeral secret"));

        // Wire key: the excluded map serializes the documented name.
        let value = serde_json::to_value(&response).unwrap();
        assert_eq!(value["excluded"]["tombstoned"], 1);
    }

    #[test]
    fn envelope_fields_present_in_every_result() {
        let mut store = Store::open_in_memory().unwrap();
        let long_tail = "x".repeat(160);
        let tainted = Capsule::new(
            format!("envelope armor headline that runs long {long_tail}\nsecond-line-secret-tail"),
            Provenance {
                source: "import:external-doc".to_string(),
                anchor: "doc-42".to_string(),
                source_hash: sha256_hex(b"envelope-tainted"),
            },
            Confidence::new(0.7).unwrap(),
            Freshness {
                valid_from: VF,
                valid_to: Some(datetime!(2026-12-31 00:00:00 UTC)),
            },
            Scope {
                project_id: "nott".to_string(),
            },
            AuthorityClass::ExternallyImported,
            true,
        )
        .unwrap();
        store.append(&tainted, APPENDED).unwrap();
        store
            .append(
                &cap("envelope second plain capsule", "nott", 0.9, VF, None),
                APPENDED,
            )
            .unwrap();

        let response = retrieve(&mut store, &query(&["envelope"]), NOW).unwrap();
        let value = serde_json::to_value(&response).unwrap();
        assert_eq!(value["outcome"], "grounded");
        let results = value["results"].as_array().unwrap();
        assert_eq!(results.len(), 2);

        // EVERY result carries the full envelope, literal label included.
        for result in results {
            assert_eq!(result["label"], "ADVISORY_NOT_AUTHORITY");
            assert_eq!(result["framing"], "DATA");
            assert!(result["id"].as_str().unwrap().starts_with("cap-"));
            assert!(result["headline"].is_string());
            assert!(result["instruction_taint"].is_boolean());
            assert!(result["authority_class"].is_string());
            assert!(result["confidence"].is_number());
            for field in ["source", "anchor", "source_hash"] {
                assert!(
                    result["provenance"][field].is_string(),
                    "provenance.{field} missing"
                );
            }
            assert!(result["freshness"]["valid_from"].is_string());
            assert!(
                result["freshness"].get("valid_to").is_some(),
                "valid_to must be explicit"
            );
            assert!(!result["matched_terms"].as_array().unwrap().is_empty());
            let relevance = result["relevance"].as_f64().unwrap();
            assert!(
                (0.0..=1.0).contains(&relevance),
                "relevance is the normalized 0..=1 explain, got {relevance}"
            );
            let bm25 = result["bm25"].as_f64().unwrap();
            assert!(
                bm25 < 0.0,
                "bm25 stays a (rounded) negative match score, got {bm25}"
            );
        }
        // The relevance scale is anchored: the top-ranked hit IS 1.0.
        assert_eq!(
            results[0]["relevance"].as_f64().unwrap(),
            1.0,
            "top hit anchors the relevance scale at 1.0"
        );

        // The tainted import keeps its flags — flagged, never hidden.
        let by_id = |id: &str| {
            results
                .iter()
                .find(|r| r["id"] == id)
                .unwrap_or_else(|| panic!("{id} missing"))
        };
        assert_eq!(by_id("cap-1")["instruction_taint"], true);
        assert_eq!(by_id("cap-1")["authority_class"], "externally-imported");
        assert_eq!(by_id("cap-2")["instruction_taint"], false);

        // Layered recall: headline truncated with …, full content NOT inlined.
        let headline = by_id("cap-1")["headline"].as_str().unwrap();
        assert_eq!(headline.chars().count(), HEADLINE_MAX_CHARS + 1);
        assert!(headline.ends_with('…'));
        let raw = serde_json::to_string(&response).unwrap();
        assert!(
            !raw.contains("second-line-secret-tail"),
            "full content must not be inlined"
        );
    }

    #[test]
    fn relevance_normalizes_top_to_one_and_rounds() {
        // Top hit anchors the scale; weaker (less negative) scores
        // shrink toward 0.0 at 2 decimals.
        assert_eq!(relevance_of(-5.0, -5.0), 1.0);
        assert_eq!(relevance_of(-1.0, -5.0), 0.2);
        // The dogfood day-1 noise pair, now legible.
        assert_eq!(
            relevance_of(-9.838_998_211_091_236e-7, -5.541_987_962_232_948e-6),
            0.18
        );
        // Total: impossible zero top and a (theoretical) positive score
        // stay inside 0.0..=1.0 — never NaN/inf on the wire.
        assert_eq!(relevance_of(0.0, 0.0), 1.0);
        assert_eq!(relevance_of(1.0, -5.0), 0.0);
    }

    #[test]
    fn bm25_wire_value_is_rounded_to_three_significant_digits() {
        // The observed friction values, no longer 17-digit blobs.
        assert_eq!(rounded_bm25(-0.000_005_541_987_962_232_948), -5.54e-6);
        assert_eq!(rounded_bm25(-9.838_998_211_091_236e-7), -9.84e-7);
        assert_eq!(rounded_bm25(-1.2345), -1.23);
    }

    #[test]
    fn headline_trailing_newline_never_earns_ellipsis() {
        // w3 review: "short headline\n" rendered "short headline…" — an
        // ellipsis claiming elided content that was only a terminator.
        assert_eq!(headline_of("short headline\n"), "short headline");
        assert_eq!(headline_of("crlf headline\r\n"), "crlf headline");
        // Real further lines still earn it — even terminator-final.
        assert_eq!(headline_of("a\nb\n"), "a…");
        assert_eq!(headline_of("a\nb"), "a…");
    }

    #[test]
    fn headline_is_single_line_across_every_unicode_line_boundary() {
        let separators = [
            ("LF", "\n"),
            ("CRLF", "\r\n"),
            ("CR", "\r"),
            ("VT", "\u{000b}"),
            ("FF", "\u{000c}"),
            ("NEL", "\u{0085}"),
            ("LS", "\u{2028}"),
            ("PS", "\u{2029}"),
        ];
        for (name, separator) in separators {
            assert_eq!(
                headline_of(&format!("ordinary{separator}hidden")),
                "ordinary…",
                "{name} must terminate the visible line"
            );
            assert_eq!(
                headline_of(&format!("ordinary{separator}")),
                "ordinary",
                "a terminal {name} carries no elided content"
            );
        }

        let long = "é".repeat(HEADLINE_MAX_CHARS + 1);
        assert_eq!(
            headline_of(&long),
            format!("{}…", "é".repeat(HEADLINE_MAX_CHARS)),
            "the character bound and ellipsis stay intact"
        );
        assert_eq!(
            headline_of("ordinary café\tbytes"),
            "ordinary café\tbytes",
            "ordinary single-line bytes stay unchanged"
        );
    }

    #[test]
    fn tiebreak_confidence_desc_when_scores_equal() {
        let mut store = Store::open_in_memory().unwrap();
        // Same token shape (tf and doc length equal) → identical bm25.
        // Lower confidence appended first, so append order cannot fake
        // the expected ranking.
        store
            .append(
                &cap("tiebreak alpha probe one", "nott", 0.5, VF, None),
                APPENDED,
            )
            .unwrap();
        store
            .append(
                &cap("tiebreak alpha probe two", "nott", 0.9, VF, None),
                APPENDED,
            )
            .unwrap();

        let response = retrieve(&mut store, &query(&["tiebreak"]), NOW).unwrap();
        let RetrieveResponse::Grounded { results, .. } = &response else {
            panic!("expected grounded");
        };
        assert!(
            (results[0].bm25.unwrap() - results[1].bm25.unwrap()).abs() < 1e-9,
            "fixture must produce equal bm25 scores, got {:?} vs {:?}",
            results[0].bm25,
            results[1].bm25
        );
        assert_eq!(
            grounded_ids(&response),
            ["cap-2", "cap-1"],
            "higher confidence first"
        );
    }

    #[test]
    fn tiebreak_valid_from_desc_then_id_asc() {
        // Equal score + confidence → newer valid_from first, even though
        // the older one has the earlier seq.
        let mut store = Store::open_in_memory().unwrap();
        store
            .append(
                &cap(
                    "tiebreak beta probe one",
                    "nott",
                    0.9,
                    datetime!(2026-07-17 12:00:00 UTC),
                    None,
                ),
                APPENDED,
            )
            .unwrap();
        store
            .append(
                &cap(
                    "tiebreak beta probe two",
                    "nott",
                    0.9,
                    datetime!(2026-07-18 12:00:00 UTC),
                    None,
                ),
                APPENDED,
            )
            .unwrap();
        let response = retrieve(&mut store, &query(&["tiebreak"]), NOW).unwrap();
        assert_eq!(
            grounded_ids(&response),
            ["cap-2", "cap-1"],
            "newer valid_from first"
        );

        // Everything equal → id ascending in NUMERIC seq order.
        let mut store = Store::open_in_memory().unwrap();
        for n in 0..11 {
            store
                .append(
                    &cap(&format!("idtie gamma probe v{n:02}"), "nott", 0.9, VF, None),
                    APPENDED,
                )
                .unwrap();
        }
        let response = retrieve(&mut store, &query(&["idtie"]), NOW).unwrap();
        let expected: Vec<String> = (1..=11).map(|n| format!("cap-{n}")).collect();
        assert_eq!(
            grounded_ids(&response),
            expected,
            "cap-2 before cap-10: numeric id order"
        );
    }

    #[test]
    fn fts_drop_then_rebuild_yields_identical_recall() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("memory.sqlite3");
        let mut store = Store::open(&path).unwrap();
        store
            .append(
                &cap("derived table proof sqlite", "nott", 0.9, VF, None),
                APPENDED,
            )
            .unwrap();
        store
            .append(
                &cap("derived table proof spool", "nott", 0.8, VF, None),
                APPENDED,
            )
            .unwrap();
        store
            .append(&cap("unrelated capsule", "nott", 0.9, VF, None), APPENDED)
            .unwrap();

        let q = query(&["derived", "sqlite"]);
        let mut before = serde_json::to_value(retrieve(&mut store, &q, NOW).unwrap()).unwrap();
        assert_eq!(before["receipt_id"], "rcpt-1");
        before.as_object_mut().unwrap().remove("receipt_id");

        // Drop the derived table out from under the store.
        let raw = rusqlite::Connection::open(&path).unwrap();
        raw.execute_batch("DROP TABLE capsules_fts").unwrap();
        drop(raw);

        assert_eq!(store.rebuild_fts().unwrap(), 3);
        let mut after = serde_json::to_value(retrieve(&mut store, &q, NOW).unwrap()).unwrap();
        assert_eq!(after["receipt_id"], "rcpt-2");
        after.as_object_mut().unwrap().remove("receipt_id");
        assert_eq!(
            serde_json::to_vec(&before).unwrap(),
            serde_json::to_vec(&after).unwrap(),
            "recall except its fresh receipt id must be byte-identical after drop→rebuild"
        );
        assert_eq!(before["outcome"], "grounded");
    }

    #[test]
    fn token_budget_floor_and_default() {
        let mut store = Store::open_in_memory().unwrap();
        for n in 0..4 {
            store
                .append(
                    &cap(&format!("budget probe capsule v{n}"), "nott", 0.9, VF, None),
                    APPENDED,
                )
                .unwrap();
        }

        // Budget floor: even a budget of 1 returns the top result.
        let mut q = query(&["budget"]);
        q.token_budget = Some(1);
        let RetrieveResponse::Grounded {
            results,
            matched,
            returned,
            trimmed,
            trimmed_by_limit,
            trimmed_by_budget,
            token_budget,
            excluded,
            ..
        } = retrieve(&mut store, &q, NOW).unwrap()
        else {
            panic!("expected grounded");
        };
        assert_eq!((matched, returned, trimmed, token_budget), (4, 1, 3, 1));
        assert_eq!((trimmed_by_limit, trimmed_by_budget), (0, 3));
        assert!(excluded.is_empty(), "no eligibility exclusions planted");
        assert_eq!(results.len(), 1);

        // Budget ZERO is honored literally — zero envelopes, mirroring
        // limit 0 (w1d zero-cap consistency; the floor of one applies to
        // NONZERO budgets only).
        let mut q0 = query(&["budget"]);
        q0.token_budget = Some(0);
        let RetrieveResponse::Grounded {
            results,
            matched,
            returned,
            trimmed,
            trimmed_by_budget,
            token_budget,
            ..
        } = retrieve(&mut store, &q0, NOW).unwrap()
        else {
            panic!("expected grounded");
        };
        assert_eq!(
            (matched, returned, trimmed, trimmed_by_budget, token_budget),
            (4, 0, 4, 4, 0)
        );
        assert!(results.is_empty(), "budget 0 returns no envelopes");

        // Default budget: documented constant, compact envelopes all fit.
        let RetrieveResponse::Grounded {
            returned,
            trimmed,
            token_budget,
            ..
        } = retrieve(&mut store, &query(&["budget"]), NOW).unwrap()
        else {
            panic!("expected grounded");
        };
        assert_eq!(token_budget, DEFAULT_TOKEN_BUDGET);
        assert_eq!((returned, trimmed), (4, 0));
    }

    #[test]
    fn limit_trims_and_reports() {
        let mut store = Store::open_in_memory().unwrap();
        // Same token shape, descending confidence → known rank order.
        for (n, conf) in [0.9, 0.7, 0.5, 0.3].iter().enumerate() {
            store
                .append(
                    &cap(
                        &format!("limit probe capsule v{n}"),
                        "nott",
                        *conf,
                        VF,
                        None,
                    ),
                    APPENDED,
                )
                .unwrap();
        }

        let mut q = query(&["limit"]);
        q.limit = Some(2);
        let response = retrieve(&mut store, &q, NOW).unwrap();
        assert_eq!(
            grounded_ids(&response),
            ["cap-1", "cap-2"],
            "best-ranked kept"
        );
        let RetrieveResponse::Grounded {
            matched,
            returned,
            trimmed,
            ..
        } = response
        else {
            panic!("expected grounded");
        };
        assert_eq!((matched, returned, trimmed), (4, 2, 2));

        // limit 0 = count-only probe: grounded, zero envelopes.
        let mut q0 = query(&["limit"]);
        q0.limit = Some(0);
        let RetrieveResponse::Grounded {
            results,
            matched,
            returned,
            trimmed,
            ..
        } = retrieve(&mut store, &q0, NOW).unwrap()
        else {
            panic!("expected grounded");
        };
        assert!(results.is_empty());
        assert_eq!((matched, returned, trimmed), (4, 0, 4));
    }

    #[test]
    fn project_fence_scopes_recall() {
        let mut store = Store::open_in_memory().unwrap();
        store
            .append(
                &cap("fence probe in project a", "proj-a", 0.9, VF, None),
                APPENDED,
            )
            .unwrap();
        store
            .append(
                &cap("fence probe in project b", "proj-b", 0.9, VF, None),
                APPENDED,
            )
            .unwrap();

        let mut fenced = query(&["fence"]);
        fenced.project_id = Some("proj-a".to_string());
        assert_eq!(
            grounded_ids(&retrieve(&mut store, &fenced, NOW).unwrap()),
            ["cap-1"]
        );

        let open = query(&["fence"]);
        assert_eq!(
            grounded_ids(&retrieve(&mut store, &open, NOW).unwrap()).len(),
            2
        );

        let mut nowhere = query(&["fence"]);
        nowhere.project_id = Some("proj-c".to_string());
        let response = retrieve(&mut store, &nowhere, NOW).unwrap();
        let RetrieveResponse::Abstain { reason, .. } = response else {
            panic!("expected abstain");
        };
        assert!(
            reason.contains("proj-c"),
            "fence named in the reason, got: {reason}"
        );
    }

    #[test]
    fn superseded_excluded_from_retrieve_but_present_via_get() {
        let mut store = Store::open_in_memory().unwrap();
        store
            .append(
                &cap("supersede probe old claim", "nott", 0.9, VF, None),
                APPENDED,
            )
            .unwrap();
        store
            .append(
                &cap("supersede probe new claim", "nott", 0.9, VF, None),
                APPENDED,
            )
            .unwrap();
        // Both ground before the supersede.
        assert_eq!(
            grounded_ids(&retrieve(&mut store, &query(&["supersede"]), NOW).unwrap()).len(),
            2
        );

        store.supersede("cap-1", "cap-2", APPENDED).unwrap();

        // The acceptance negative: absent from retrieve...
        let response = retrieve(&mut store, &query(&["supersede"]), NOW).unwrap();
        assert_eq!(
            grounded_ids(&response),
            ["cap-2"],
            "only the live successor grounds"
        );
        let RetrieveResponse::Grounded { matched, .. } = &response else {
            panic!("expected grounded");
        };
        assert_eq!(
            *matched, 1,
            "the superseded capsule is not an eligible match"
        );
        // w1d: a partial exclusion grounds on the survivors AND names the
        // matched-but-excluded evidence (a grounded outcome no longer
        // hides that ineligible matches existed).
        let value = serde_json::to_value(&response).unwrap();
        assert_eq!(value["excluded"], serde_json::json!({ "superseded": 1 }));
        assert!(
            value.get("excluded_count").is_none(),
            "excluded_count stays missing_evidence-only"
        );
        // ...AND present via get, bytes intact.
        let old = store.get("cap-1").unwrap().unwrap();
        assert_eq!(old.capsule.content(), "supersede probe old claim");
    }

    #[test]
    fn only_superseded_match_yields_missing_evidence_never_the_content() {
        let mut store = Store::open_in_memory().unwrap();
        store
            .append(&cap("lone replaced fact", "nott", 0.9, VF, None), APPENDED)
            .unwrap();
        store
            .append(
                &cap("its unmatched successor", "nott", 0.9, VF, None),
                APPENDED,
            )
            .unwrap();
        store.supersede("cap-1", "cap-2", APPENDED).unwrap();

        // The ONLY lexical match is the superseded capsule → the third
        // honest state, distinct from abstain.
        let response = retrieve(&mut store, &query(&["replaced"]), NOW).unwrap();
        let RetrieveResponse::MissingEvidence {
            excluded_count,
            excluded,
            reason,
            ..
        } = &response
        else {
            panic!("expected missing_evidence, got: {response:?}");
        };
        assert_eq!(*excluded_count, 1);
        assert_eq!(
            excluded,
            &BTreeMap::from([(ExclusionReason::Superseded, 1)])
        );
        assert!(
            reason.contains("1 superseded") && reason.contains("get/list"),
            "honest reason naming the exclusion and the escape hatch, got: {reason}"
        );

        // Wire shape: the documented outcome string, the counts, and no
        // results field.
        let value = serde_json::to_value(&response).unwrap();
        assert_eq!(value["outcome"], "missing_evidence");
        assert_eq!(value["excluded_count"], 1);
        assert_eq!(value["excluded"]["superseded"], 1);
        assert!(value.get("results").is_none());

        // The acceptance negative: not one excluded byte reaches the
        // response — neither the superseded content nor its id.
        let raw = serde_json::to_string(&response).unwrap();
        assert!(
            !raw.contains("lone replaced fact") && !raw.contains("cap-1"),
            "excluded capsule leaked into the response: {raw}"
        );

        // Tri-state boundary: zero raw matches still ABSTAINS.
        let zero = retrieve(&mut store, &query(&["zzz-nothing"]), NOW).unwrap();
        assert!(
            matches!(zero, RetrieveResponse::Abstain { .. }),
            "zero-match must stay abstain, got: {zero:?}"
        );
    }

    #[test]
    fn mixed_exclusions_count_per_reason_first_fence_wins_deterministic_bytes() {
        let mut store = Store::open_in_memory().unwrap();
        // cap-1: superseded AND expired → counted ONCE, as superseded
        // (fence order), never double-counted.
        store
            .append(
                &cap(
                    "mixedfence probe doubly dead",
                    "nott",
                    0.9,
                    datetime!(2026-07-01 00:00:00 UTC),
                    Some(datetime!(2026-07-10 00:00:00 UTC)),
                ),
                APPENDED,
            )
            .unwrap();
        // cap-2: live successor that does NOT match the query term.
        store
            .append(&cap("unrelated successor", "nott", 0.9, VF, None), APPENDED)
            .unwrap();
        // cap-3: expired only.
        store
            .append(
                &cap(
                    "mixedfence probe expired",
                    "nott",
                    0.9,
                    datetime!(2026-07-01 00:00:00 UTC),
                    Some(datetime!(2026-07-10 00:00:00 UTC)),
                ),
                APPENDED,
            )
            .unwrap();
        // cap-4: not yet valid at NOW.
        store
            .append(
                &cap(
                    "mixedfence probe future",
                    "nott",
                    0.9,
                    datetime!(2026-08-01 00:00:00 UTC),
                    None,
                ),
                APPENDED,
            )
            .unwrap();
        store.supersede("cap-1", "cap-2", APPENDED).unwrap();

        let response = retrieve(&mut store, &query(&["mixedfence"]), NOW).unwrap();
        let RetrieveResponse::MissingEvidence {
            excluded_count,
            excluded,
            ..
        } = &response
        else {
            panic!("expected missing_evidence, got: {response:?}");
        };
        assert_eq!(*excluded_count, 3, "sum over all fences, no double count");
        assert_eq!(
            excluded,
            &BTreeMap::from([
                (ExclusionReason::Superseded, 1),
                (ExclusionReason::Expired, 1),
                (ExclusionReason::NotYetValid, 1),
            ])
        );

        // Deterministic bytes: fixed wire order (ExclusionReason variant
        // order) and a byte-identical repeat at the same injected instant.
        let first = serde_json::to_string(&response).unwrap();
        assert!(
            first.contains(r#""excluded":{"superseded":1,"expired":1,"not_yet_valid":1}"#),
            "deterministic excluded map order, got: {first}"
        );
        let again =
            serde_json::to_string(&retrieve(&mut store, &query(&["mixedfence"]), NOW).unwrap())
                .unwrap();
        assert_eq!(first, again, "missing_evidence must be byte-deterministic");
    }

    #[test]
    fn usage_orders_full_ties_and_never_mutates_the_capsule() {
        let mut store = Store::open_in_memory().unwrap();
        // Identical token shape, confidence, valid_from → full tie;
        // baseline order is id ascending.
        store
            .append(&cap("usagetie probe one", "nott", 0.9, VF, None), APPENDED)
            .unwrap();
        store
            .append(&cap("usagetie probe two", "nott", 0.9, VF, None), APPENDED)
            .unwrap();
        assert_eq!(
            grounded_ids(&retrieve(&mut store, &query(&["usagetie"]), NOW).unwrap()),
            ["cap-1", "cap-2"]
        );
        let before = store.get("cap-2").unwrap().unwrap();

        // Recall cap-2 at a later instant than the shared recall above.
        store
            .record_recall(&["cap-2"], datetime!(2026-07-18 21:00:00 UTC))
            .unwrap();
        assert_eq!(
            grounded_ids(&retrieve(&mut store, &query(&["usagetie"]), NOW).unwrap()),
            ["cap-2", "cap-1"],
            "more recent recall wins the full tie"
        );

        // The law: usage touched NOTHING on the capsule — not confidence,
        // not authority, not a byte.
        let after = store.get("cap-2").unwrap().unwrap();
        assert_eq!(before.capsule, after.capsule);
        assert_eq!(
            before.capsule.to_canonical_json().unwrap(),
            after.capsule.to_canonical_json().unwrap()
        );
    }

    #[test]
    fn usage_is_late_never_outranks_confidence() {
        let mut store = Store::open_in_memory().unwrap();
        store
            .append(
                &cap("latekey probe strong", "nott", 0.9, VF, None),
                APPENDED,
            )
            .unwrap();
        store
            .append(&cap("latekey probe weak", "nott", 0.5, VF, None), APPENDED)
            .unwrap();
        // Hammer the weak capsule's counters.
        for _ in 0..5 {
            store.record_recall(&["cap-2"], NOW).unwrap();
        }
        assert_eq!(
            grounded_ids(&retrieve(&mut store, &query(&["latekey"]), NOW).unwrap()),
            ["cap-1", "cap-2"],
            "confidence still outranks any amount of usage"
        );
    }

    #[test]
    fn recall_counts_returned_ids_only() {
        let mut store = Store::open_in_memory().unwrap();
        // Descending confidence → known rank order.
        for (n, conf) in [0.9, 0.7].iter().enumerate() {
            store
                .append(
                    &cap(&format!("countprobe capsule v{n}"), "nott", *conf, VF, None),
                    APPENDED,
                )
                .unwrap();
        }

        // limit 1: only the returned top result is counted, at the
        // injected query instant.
        let mut q = query(&["countprobe"]);
        q.limit = Some(1);
        assert_eq!(
            grounded_ids(&retrieve(&mut store, &q, NOW).unwrap()),
            ["cap-1"]
        );
        let stat = store.usage_of("cap-1").unwrap().unwrap();
        assert_eq!(stat.recall_count, 1);
        assert_eq!(stat.last_recalled_at, NOW);
        assert_eq!(
            store.usage_of("cap-2").unwrap(),
            None,
            "trimmed → not counted"
        );

        // Second recall increments.
        retrieve(&mut store, &q, NOW).unwrap();
        assert_eq!(store.usage_of("cap-1").unwrap().unwrap().recall_count, 2);

        // limit 0 count-only probe returns no envelope and counts nothing.
        let mut q0 = query(&["countprobe"]);
        q0.limit = Some(0);
        retrieve(&mut store, &q0, NOW).unwrap();
        assert_eq!(store.usage_of("cap-1").unwrap().unwrap().recall_count, 2);
        assert_eq!(store.usage_of("cap-2").unwrap(), None);

        // Abstaining counts nothing either.
        let _ = retrieve(&mut store, &query(&["zzz-absent"]), NOW).unwrap();
        assert_eq!(store.usage_of("cap-1").unwrap().unwrap().recall_count, 2);
    }

    #[test]
    fn empty_query_rejected() {
        let mut store = Store::open_in_memory().unwrap();
        for terms in [&[] as &[&str], &["   "], &["*-*", "--"]] {
            let err = retrieve(&mut store, &query(terms), NOW).unwrap_err();
            assert_eq!(
                err,
                RetrieveError::EmptyQuery,
                "terms {terms:?} must be rejected"
            );
        }
    }

    #[test]
    fn quoted_terms_cannot_inject_fts5_syntax() {
        let mut store = Store::open_in_memory().unwrap();
        store
            .append(&cap("beta gamma delta", "nott", 0.9, VF, None), APPENDED)
            .unwrap();

        // If quoting leaked, this would parse as "beta" OR "delta" and
        // ground; quoted, it is the literal phrase "beta or delta",
        // which is absent → abstain.
        let response = retrieve(&mut store, &query(&[r#"beta" OR "delta"#]), NOW).unwrap();
        assert!(
            matches!(response, RetrieveResponse::Abstain { .. }),
            "OR injection must not match"
        );

        // FTS5 operators/specials as literal terms: never a syntax error.
        for weird in ["NEAR(beta", "beta AND delta", "-beta", "content:beta"] {
            assert!(
                retrieve(&mut store, &query(&[weird]), NOW).is_ok(),
                "term {weird:?} errored"
            );
        }
    }

    #[test]
    fn decay_tiebreak_fresh_lower_conf_outranks_old_higher_conf_at_same_score() {
        let mut store = Store::open_in_memory().unwrap();
        // Identical token shape → identical bm25. The OLD capsule holds
        // the HIGHER stored confidence (0.8), but at exactly one
        // half-life of age (90 days before NOW) it decays to 0.4, so the
        // fresh 0.5 wins the tiebreak — raw-confidence ranking would
        // order the other way around.
        store
            .append(
                &cap(
                    "decaytie probe old",
                    "nott",
                    0.8,
                    datetime!(2026-04-19 20:00:00 UTC), // NOW minus exactly 90 days
                    None,
                ),
                APPENDED,
            )
            .unwrap();
        store
            .append(&cap("decaytie probe new", "nott", 0.5, VF, None), APPENDED)
            .unwrap();
        let before = store.get("cap-1").unwrap().unwrap();

        let response = retrieve(&mut store, &query(&["decaytie"]), NOW).unwrap();
        let RetrieveResponse::Grounded { results, .. } = &response else {
            panic!("expected grounded, got: {response:?}");
        };
        assert!(
            (results[0].bm25.unwrap() - results[1].bm25.unwrap()).abs() < 1e-9,
            "fixture must tie on bm25, got {:?} vs {:?}",
            results[0].bm25,
            results[1].bm25
        );
        assert_eq!(
            grounded_ids(&response),
            ["cap-2", "cap-1"],
            "fresh 0.5 outranks 90-day-old 0.8 (decayed to 0.4)"
        );

        // Envelope explain: stored confidence verbatim, decay at 2
        // decimals.
        let by_id = |id: &str| {
            results
                .iter()
                .find(|r| r.id.as_str() == id)
                .unwrap_or_else(|| panic!("{id} missing"))
        };
        assert_eq!(by_id("cap-1").confidence.value(), 0.8);
        assert_eq!(by_id("cap-1").decayed_weight, 0.4);
        assert_eq!(by_id("cap-2").confidence.value(), 0.5);
        assert_eq!(by_id("cap-2").decayed_weight, 0.5);

        // The law: decay never gates matching (the old capsule still
        // grounds and returns) and never mutates the stored capsule.
        assert_eq!(results.len(), 2);
        let after = store.get("cap-1").unwrap().unwrap();
        assert_eq!(before.capsule, after.capsule);
        assert_eq!(
            before.capsule.to_canonical_json().unwrap(),
            after.capsule.to_canonical_json().unwrap()
        );
    }

    #[test]
    fn decay_is_late_never_outranks_bm25_score() {
        let mut store = Store::open_in_memory().unwrap();
        // cap-1 matches the term twice (stronger bm25) but is a year old
        // at rock-bottom confidence; cap-2 matches once, fresh and
        // confident. Score must still rank cap-1 first — decay orders
        // score ties only, it never overrides the match strength.
        store
            .append(
                &cap(
                    "latedecay latedecay probe",
                    "nott",
                    0.2,
                    datetime!(2025-07-18 20:00:00 UTC),
                    None,
                ),
                APPENDED,
            )
            .unwrap();
        store
            .append(
                &cap("latedecay fresh confident probe", "nott", 0.9, VF, None),
                APPENDED,
            )
            .unwrap();
        let response = retrieve(&mut store, &query(&["latedecay"]), NOW).unwrap();
        let RetrieveResponse::Grounded { results, .. } = &response else {
            panic!("expected grounded, got: {response:?}");
        };
        assert!(
            results[0].bm25.unwrap() < results[1].bm25.unwrap(),
            "fixture must produce distinct bm25 scores, got {:?} vs {:?}",
            results[0].bm25,
            results[1].bm25
        );
        assert_eq!(grounded_ids(&response), ["cap-1", "cap-2"]);
    }

    #[test]
    fn alias_expansion_grounds_and_explains_alias_matches() {
        let mut inner = Store::open_in_memory().unwrap();
        inner
            .append(
                &cap("pg wal checkpoint tuning note", "nott", 0.9, VF, None),
                APPENDED,
            )
            .unwrap();
        inner
            .append(
                &cap("postgres upgrade to 16 done", "nott", 0.9, VF, None),
                APPENDED,
            )
            .unwrap();
        let mut store = ContractStore::new(inner);
        store
            .aliases
            .insert("postgres".to_string(), vec!["pg".to_string()]);

        // ONE caller term finds both capsules through its OR-group, and
        // the explain says HOW each grounded: plain term vs alias:<term>.
        let response = retrieve_core(
            &mut store,
            &query(&["postgres"]),
            NOW,
            Path::new("/nonexistent-root"),
        )
        .unwrap();
        let RetrieveResponse::Grounded {
            results, matched, ..
        } = &response
        else {
            panic!("expected grounded, got: {response:?}");
        };
        assert_eq!(*matched, 2);
        let by_id = |id: &str| {
            results
                .iter()
                .find(|r| r.id.as_str() == id)
                .unwrap_or_else(|| panic!("{id} missing"))
        };
        assert_eq!(by_id("cap-1").matched_terms, ["alias:postgres"]);
        assert_eq!(by_id("cap-2").matched_terms, ["postgres"]);

        // Store-side normalization contract: the folded lookup expands
        // an accent-variant spelling of the same term identically.
        let response = retrieve_core(
            &mut store,
            &query(&["Postgrés"]),
            NOW,
            Path::new("/nonexistent-root"),
        )
        .unwrap();
        let RetrieveResponse::Grounded { results, .. } = &response else {
            panic!("expected grounded, got: {response:?}");
        };
        let explained: Vec<&[String]> =
            results.iter().map(|r| r.matched_terms.as_slice()).collect();
        assert!(
            explained.contains(&["alias:Postgrés".to_string()].as_slice()),
            "alias attribution keeps the caller's own spelling, got: {explained:?}"
        );
    }

    #[test]
    fn abstain_reason_names_alias_expansion_when_it_ran() {
        let mut store = ContractStore::new(Store::open_in_memory().unwrap());
        store
            .aliases
            .insert("postgres".to_string(), vec!["pg".to_string()]);
        let response = retrieve_core(
            &mut store,
            &query(&["postgres"]),
            NOW,
            Path::new("/nonexistent-root"),
        )
        .unwrap();
        let RetrieveResponse::Abstain { reason, .. } = &response else {
            panic!("expected abstain, got: {response:?}");
        };
        assert!(
            reason.contains("expanded with 1 store-fed alias"),
            "expansion must be visible in the honest reason, got: {reason}"
        );
    }

    #[test]
    fn anchor_liveness_reports_live_missing_and_unknown_in_envelopes() {
        let root = tempfile::tempdir().unwrap();
        std::fs::create_dir_all(root.path().join("src")).unwrap();
        std::fs::write(root.path().join("src/lib.rs"), b"// probe\n").unwrap();

        let plant = |content: &str, anchor: &str| -> Capsule {
            Capsule::new(
                content.to_string(),
                Provenance {
                    source: "session:2026-07-18".to_string(),
                    anchor: anchor.to_string(),
                    source_hash: sha256_hex(content.as_bytes()),
                },
                Confidence::new(0.9).unwrap(),
                Freshness {
                    valid_from: VF,
                    valid_to: None,
                },
                Scope {
                    project_id: "nott".to_string(),
                },
                AuthorityClass::UserStated,
                false,
            )
            .unwrap()
        };
        let mut store = Store::open_in_memory().unwrap();
        store
            .append(&plant("liveprobe one", "src/lib.rs:1"), APPENDED)
            .unwrap();
        store
            .append(&plant("liveprobe two", "gone/nope.rs:7"), APPENDED)
            .unwrap();
        store
            .append(&plant("liveprobe three", "doc-42"), APPENDED)
            .unwrap();

        let response = retrieve_core(&mut store, &query(&["liveprobe"]), NOW, root.path()).unwrap();
        let value = serde_json::to_value(&response).unwrap();
        let results = value["results"].as_array().unwrap();
        assert_eq!(results.len(), 3, "liveness never gates or blocks recall");
        let by_id = |id: &str| {
            results
                .iter()
                .find(|r| r["id"] == id)
                .unwrap_or_else(|| panic!("{id} missing"))
        };
        // The documented wire tri-state: true | false | "unknown".
        assert_eq!(by_id("cap-1")["anchor_live"], serde_json::json!(true));
        assert_eq!(by_id("cap-2")["anchor_live"], serde_json::json!(false));
        assert_eq!(by_id("cap-3")["anchor_live"], serde_json::json!("unknown"));
    }

    #[test]
    fn anchor_liveness_probe_is_total_and_fenced() {
        let root = tempfile::tempdir().unwrap();
        std::fs::write(root.path().join("real.md"), b"x").unwrap();
        std::fs::create_dir(root.path().join("dir")).unwrap();

        // Existence probe: files and directories both count as live.
        assert_eq!(anchor_liveness("real.md:12", root.path()), AnchorLive::Live);
        assert_eq!(anchor_liveness("dir:3", root.path()), AnchorLive::Live);
        assert_eq!(
            anchor_liveness("absent.md:1", root.path()),
            AnchorLive::Missing
        );

        // Non-`path:line` shapes never guess.
        for anchor in [
            "doc-42",
            "real.md",
            "real.md:",
            ":7",
            "real.md:12a",
            "real.md:1:x",
        ] {
            assert_eq!(
                anchor_liveness(anchor, root.path()),
                AnchorLive::Unknown,
                "anchor {anchor:?}"
            );
        }
        // The fence: absolute and `..`-traversing paths never leave root.
        assert_eq!(
            anchor_liveness("/etc/hostname:1", root.path()),
            AnchorLive::Unknown
        );
        assert_eq!(
            anchor_liveness("../real.md:1", root.path()),
            AnchorLive::Unknown
        );
        // io failure degrades to unknown, never a panic: NUL is invalid
        // in a Linux path (InvalidInput, not NotFound).
        assert_eq!(
            anchor_liveness("nul\0byte.md:1", root.path()),
            AnchorLive::Unknown
        );
    }

    /// v3 fence: `fs::metadata` follows symlinks, so a repo-internal link
    /// could existence-probe OUTSIDE the anchor root. The probe must
    /// refuse to follow ANY symlink component — fail-closed `Missing`
    /// (wire `false`), never an out-of-root probe.
    #[cfg(unix)]
    #[test]
    fn anchor_liveness_never_follows_symlinks_out_of_the_root() {
        let outside = tempfile::tempdir().unwrap();
        std::fs::write(outside.path().join("secret.txt"), b"x").unwrap();
        let root = tempfile::tempdir().unwrap();
        std::fs::write(root.path().join("real.md"), b"x").unwrap();
        std::os::unix::fs::symlink(outside.path(), root.path().join("dirlink")).unwrap();
        std::os::unix::fs::symlink(
            outside.path().join("secret.txt"),
            root.path().join("leaflink.md"),
        )
        .unwrap();
        std::os::unix::fs::symlink(root.path().join("real.md"), root.path().join("inlink.md"))
            .unwrap();

        // The escape probes: both targets EXIST outside the root, and the
        // answer is still Missing — their existence never leaks.
        assert_eq!(
            anchor_liveness("dirlink/secret.txt:1", root.path()),
            AnchorLive::Missing
        );
        assert_eq!(
            anchor_liveness("leaflink.md:1", root.path()),
            AnchorLive::Missing
        );
        // Fail-closed uniformly: even an IN-root symlink answers Missing
        // (symlinked anchors are never followed, wherever they point).
        assert_eq!(
            anchor_liveness("inlink.md:1", root.path()),
            AnchorLive::Missing
        );
        // Symlink-free anchors keep their verdicts.
        assert_eq!(anchor_liveness("real.md:12", root.path()), AnchorLive::Live);
        assert_eq!(
            anchor_liveness("absent.md:1", root.path()),
            AnchorLive::Missing
        );
    }

    /// u-r2 RED (PRD R2): anchors detect content CHANGE, not just
    /// existence. Capture-time hashes recorded through the SAME
    /// [`anchor_content_hash`] call the boundary uses; then the envelope's
    /// `anchor_drift` answers the closed tri-state — `"drifted"` for an
    /// edited anchored file, `"unchanged"` for an untouched one,
    /// `"unknown"` for a non-path anchor and for a capsule with no
    /// recorded capture hash. Drift never gates: all four rows ground.
    #[test]
    fn anchor_drift_reports_drifted_unchanged_and_unknown_in_envelopes() {
        let root = tempfile::tempdir().unwrap();
        std::fs::create_dir_all(root.path().join("notes")).unwrap();
        std::fs::write(root.path().join("notes/fact.md"), b"the original fact\n").unwrap();
        std::fs::write(root.path().join("notes/stable.md"), b"the stable fact\n").unwrap();

        let plant = |content: &str, anchor: &str| -> Capsule {
            Capsule::new(
                content.to_string(),
                Provenance {
                    source: "session:2026-07-19".to_string(),
                    anchor: anchor.to_string(),
                    source_hash: sha256_hex(content.as_bytes()),
                },
                Confidence::new(0.9).unwrap(),
                Freshness {
                    valid_from: VF,
                    valid_to: None,
                },
                Scope {
                    project_id: "nott".to_string(),
                },
                AuthorityClass::UserStated,
                false,
            )
            .unwrap()
        };
        let mut store = Store::open_in_memory().unwrap();
        store
            .append(
                &plant("driftprobe edited claim", "notes/fact.md:1"),
                APPENDED,
            )
            .unwrap(); // cap-1
        store
            .append(
                &plant("driftprobe stable claim", "notes/stable.md:1"),
                APPENDED,
            )
            .unwrap(); // cap-2
        store
            .append(&plant("driftprobe doc claim", "doc-42"), APPENDED)
            .unwrap(); // cap-3
        store
            .append(
                &plant("driftprobe unhashed claim", "notes/fact.md:9"),
                APPENDED,
            )
            .unwrap(); // cap-4

        // Capture-time hashes for cap-1/cap-2, via the ONE boundary
        // function — cap-3 (non-path) resolves to None and records
        // nothing; cap-4 deliberately records nothing.
        let h1 = anchor_content_hash("notes/fact.md:1", root.path()).unwrap();
        let h2 = anchor_content_hash("notes/stable.md:1", root.path()).unwrap();
        assert_eq!(anchor_content_hash("doc-42", root.path()), None);
        assert!(store.set_anchor_hash("cap-1", &h1, APPENDED).unwrap());
        assert!(store.set_anchor_hash("cap-2", &h2, APPENDED).unwrap());

        // THE EDIT: the anchored file's bytes change after capture.
        std::fs::write(root.path().join("notes/fact.md"), b"the fact, rewritten\n").unwrap();

        let response =
            retrieve_core(&mut store, &query(&["driftprobe"]), NOW, root.path()).unwrap();
        let value = serde_json::to_value(&response).unwrap();
        let results = value["results"].as_array().unwrap();
        assert_eq!(results.len(), 4, "drift never gates or blocks recall");
        let by_id = |id: &str| {
            results
                .iter()
                .find(|r| r["id"] == id)
                .unwrap_or_else(|| panic!("{id} missing"))
        };
        // The documented closed tri-state.
        assert_eq!(by_id("cap-1")["anchor_drift"], serde_json::json!("drifted"));
        assert_eq!(
            by_id("cap-2")["anchor_drift"],
            serde_json::json!("unchanged")
        );
        assert_eq!(by_id("cap-3")["anchor_drift"], serde_json::json!("unknown"));
        assert_eq!(by_id("cap-4")["anchor_drift"], serde_json::json!("unknown"));
    }

    /// u-r2, v3 fence carried into the drift probe: a symlinked anchor is
    /// NEVER read through — even when the link target holds the exact
    /// capture-time bytes (an "unchanged" verdict through the link would
    /// be an out-of-root read). Fail-closed `"unknown"`, and a
    /// missing/deleted file is `"unknown"` too (no current bytes to
    /// compare — deletion is `anchor_live: false`'s message).
    #[cfg(unix)]
    #[test]
    fn anchor_drift_never_reads_through_symlinks_and_missing_is_unknown() {
        let outside = tempfile::tempdir().unwrap();
        std::fs::write(outside.path().join("twin.md"), b"identical bytes\n").unwrap();
        let root = tempfile::tempdir().unwrap();
        std::fs::write(root.path().join("real.md"), b"identical bytes\n").unwrap();

        // Capture while the anchor is a real in-root file.
        let capture = anchor_content_hash("real.md:1", root.path()).unwrap();

        // The file becomes a symlink to an OUTSIDE twin with the SAME
        // bytes: following it would answer "unchanged" — the fence must
        // answer None → unknown instead.
        std::fs::remove_file(root.path().join("real.md")).unwrap();
        std::os::unix::fs::symlink(outside.path().join("twin.md"), root.path().join("real.md"))
            .unwrap();
        assert_eq!(anchor_content_hash("real.md:1", root.path()), None);
        assert_eq!(
            anchor_drift_of("real.md:1", root.path(), Some(&capture)),
            AnchorDrift::Unknown
        );

        // Deleted outright: no current bytes → unknown, never "drifted".
        std::fs::remove_file(root.path().join("real.md")).unwrap();
        assert_eq!(
            anchor_drift_of("real.md:1", root.path(), Some(&capture)),
            AnchorDrift::Unknown
        );
    }

    /// u-r2: the epistemic sidecar rides the envelope when present —
    /// `evidence_state` / `proof_hint` / `stale_if` verbatim — and the
    /// keys are ABSENT (not null) on a never-annotated capsule
    /// (skip-serializing-if-none, the q109/q91 row-flag idiom).
    #[test]
    fn epistemic_sidecar_rides_the_envelope_when_present_and_is_absent_otherwise() {
        let mut store = Store::open_in_memory().unwrap();
        store
            .append(&cap("epiprobe annotated", "nott", 0.9, VF, None), APPENDED)
            .unwrap(); // cap-1
        store
            .append(&cap("epiprobe bare", "nott", 0.9, VF, None), APPENDED)
            .unwrap(); // cap-2
        store
            .set_epistemics(
                "cap-1",
                Some("observed"),
                Some("cargo test -p nmemory"),
                Some("schema v8 lands"),
                APPENDED,
            )
            .unwrap();

        let response = retrieve_core(
            &mut store,
            &query(&["epiprobe"]),
            NOW,
            Path::new("/nonexistent-root"),
        )
        .unwrap();
        let value = serde_json::to_value(&response).unwrap();
        let results = value["results"].as_array().unwrap();
        assert_eq!(results.len(), 2);
        let by_id = |id: &str| {
            results
                .iter()
                .find(|r| r["id"] == id)
                .unwrap_or_else(|| panic!("{id} missing"))
        };
        let annotated = by_id("cap-1");
        assert_eq!(annotated["evidence_state"], serde_json::json!("observed"));
        assert_eq!(
            annotated["proof_hint"],
            serde_json::json!("cargo test -p nmemory")
        );
        assert_eq!(annotated["stale_if"], serde_json::json!("schema v8 lands"));
        let bare = by_id("cap-2").as_object().unwrap();
        for key in ["evidence_state", "proof_hint", "stale_if"] {
            assert!(
                !bare.contains_key(key),
                "{key} must be OMITTED on a never-annotated capsule"
            );
        }
    }

    #[test]
    fn tier_fence_excludes_archived_and_quarantined_with_counts() {
        let mut inner = Store::open_in_memory().unwrap();
        inner
            .append(&cap("tierprobe alpha", "nott", 0.9, VF, None), APPENDED)
            .unwrap();
        inner
            .append(&cap("tierprobe beta", "nott", 0.9, VF, None), APPENDED)
            .unwrap();
        inner
            .append(&cap("tierprobe gamma", "nott", 0.9, VF, None), APPENDED)
            .unwrap();
        let mut store = ContractStore::new(inner);
        store.tiers.insert("cap-1".to_string(), Tier::Archived);
        store.tiers.insert("cap-2".to_string(), Tier::Quarantined);

        // Partial exclusion: the active capsule grounds; the tiered two
        // are counted, never returned.
        let response = retrieve_core(
            &mut store,
            &query(&["tierprobe"]),
            NOW,
            Path::new("/nonexistent-root"),
        )
        .unwrap();
        assert_eq!(grounded_ids(&response), ["cap-3"]);
        let value = serde_json::to_value(&response).unwrap();
        assert_eq!(
            value["excluded"],
            serde_json::json!({ "archived": 1, "quarantined": 1 })
        );

        // Every match tiered out → the third honest state with the
        // documented per-reason counts {archived: n, quarantined: n}.
        store.tiers.insert("cap-3".to_string(), Tier::Archived);
        let response = retrieve_core(
            &mut store,
            &query(&["tierprobe"]),
            NOW,
            Path::new("/nonexistent-root"),
        )
        .unwrap();
        let RetrieveResponse::MissingEvidence {
            excluded_count,
            excluded,
            reason,
            ..
        } = &response
        else {
            panic!("expected missing_evidence, got: {response:?}");
        };
        assert_eq!(*excluded_count, 3);
        assert_eq!(
            excluded,
            &BTreeMap::from([
                (ExclusionReason::Archived, 2),
                (ExclusionReason::Quarantined, 1),
            ])
        );
        assert!(
            reason.contains("2 archived")
                && reason.contains("1 quarantined")
                && reason.contains("get/list"),
            "honest tier counts + reachability escape hatch, got: {reason}"
        );
        let value = serde_json::to_value(&response).unwrap();
        assert_eq!(value["excluded"]["archived"], 2);
        assert_eq!(value["excluded"]["quarantined"], 1);
        // Tiered capsules stay reachable — retired, never hidden.
        assert!(store.inner.get("cap-1").unwrap().is_some());

        // Dominance law (w2-fix): a capsule both superseded and archived
        // counts as ARCHIVED (fence order: quarantined → archived →
        // superseded → currency), never double-counted — superseding an
        // archived capsule cannot make the tier invisible on recall.
        store.inner.supersede("cap-1", "cap-3", APPENDED).unwrap();
        let response = retrieve_core(
            &mut store,
            &query(&["tierprobe"]),
            NOW,
            Path::new("/nonexistent-root"),
        )
        .unwrap();
        let RetrieveResponse::MissingEvidence { excluded, .. } = &response else {
            panic!("expected missing_evidence, got: {response:?}");
        };
        assert_eq!(
            excluded,
            &BTreeMap::from([
                (ExclusionReason::Archived, 2),
                (ExclusionReason::Quarantined, 1),
            ])
        );

        // Quarantine dominates everything — the taint signal must never
        // disappear: superseding the quarantined capsule still reports
        // it as quarantined (the laundering hole fleet-2 found).
        store.inner.supersede("cap-2", "cap-3", APPENDED).unwrap();
        let response = retrieve_core(
            &mut store,
            &query(&["tierprobe"]),
            NOW,
            Path::new("/nonexistent-root"),
        )
        .unwrap();
        let RetrieveResponse::MissingEvidence { excluded, .. } = &response else {
            panic!("expected missing_evidence, got: {response:?}");
        };
        assert_eq!(
            excluded,
            &BTreeMap::from([
                (ExclusionReason::Archived, 2),
                (ExclusionReason::Quarantined, 1),
            ])
        );
    }

    // ── w2 integrate: real-Store swap tripwires ──────────────────────
    // These drive the PUBLIC `retrieve(&mut Store, …)` — they fail if
    // `impl RecallStore for Store` ever regresses to the pre-store2
    // defaults (empty aliases / always-Active), which would silently
    // disable synonym expansion and the tier fence in the shipped
    // binary.

    #[test]
    fn real_store_alias_expansion_is_wired_end_to_end() {
        let mut store = Store::open_in_memory().unwrap();
        store
            .append(
                &cap("postgres upgrade to sixteen done", "nott", 0.9, VF, None),
                APPENDED,
            )
            .unwrap();
        store.add_alias("pg", "postgres", APPENDED).unwrap();

        let response = retrieve(&mut store, &query(&["pg"]), NOW).unwrap();
        let RetrieveResponse::Grounded { results, .. } = &response else {
            panic!("expected grounded via store-taught alias, got: {response:?}");
        };
        assert_eq!(results.len(), 1);
        assert_eq!(results[0].id.as_str(), "cap-1");
        assert_eq!(results[0].matched_terms, ["alias:pg"]);
    }

    #[test]
    fn real_store_tier_fence_is_wired_end_to_end() {
        let mut store = Store::open_in_memory().unwrap();
        store
            .append(
                &cap("tokio pinned at 1.38", "nott", 0.9, VF, None),
                APPENDED,
            )
            .unwrap();
        store
            .append(
                &cap("tokio upgrade blocked", "nott", 0.9, VF, None),
                APPENDED,
            )
            .unwrap();
        store.set_tier("cap-1", Tier::Archived, APPENDED).unwrap();
        store
            .set_tier("cap-2", Tier::Quarantined, APPENDED)
            .unwrap();

        let response = retrieve(&mut store, &query(&["tokio"]), NOW).unwrap();
        let RetrieveResponse::MissingEvidence {
            excluded_count,
            excluded,
            ..
        } = &response
        else {
            panic!("expected missing_evidence via real tier fence, got: {response:?}");
        };
        assert_eq!(*excluded_count, 2);
        assert_eq!(
            excluded,
            &BTreeMap::from([
                (ExclusionReason::Archived, 1),
                (ExclusionReason::Quarantined, 1),
            ])
        );
    }

    #[test]
    fn corroboration_wire_is_dormant_without_rows_and_present_with_them() {
        // S2 git witness lane: the envelope's `corroboration` field is
        // read per returned row from the store (never a process spawn) and
        // omitted when the capsule was never scanned — byte-identical
        // dormancy — then folds the latest verdict per kind when present.
        let mut store = Store::open_in_memory().unwrap();
        store
            .append(
                &cap("git witness anchor probe", "nott", 0.9, VF, None),
                APPENDED,
            )
            .unwrap();

        let response = retrieve(&mut store, &query(&["witness"]), NOW).unwrap();
        let RetrieveResponse::Grounded { results, .. } = &response else {
            panic!("expected grounded, got: {response:?}");
        };
        let dormant = serde_json::to_value(&results[0]).unwrap();
        assert!(
            dormant.get("corroboration").is_none(),
            "a never-scanned capsule omits the corroboration field: {dormant}"
        );

        store
            .append_corroboration(
                "cap-1",
                "git",
                "anchor_path",
                "PLAN.md",
                "corroborated",
                Some("head1"),
                APPENDED,
            )
            .unwrap();
        store
            .append_corroboration(
                "cap-1",
                "git",
                "mention",
                "commitX",
                "corroborated",
                Some("head1"),
                APPENDED,
            )
            .unwrap();
        let response = retrieve(&mut store, &query(&["witness"]), NOW).unwrap();
        let RetrieveResponse::Grounded { results, .. } = &response else {
            panic!("expected grounded, got: {response:?}");
        };
        let decorated = serde_json::to_value(&results[0]).unwrap();
        let corr = decorated
            .get("corroboration")
            .expect("a scanned capsule carries the corroboration field");
        assert_eq!(corr["source"], "git");
        assert_eq!(corr["path"], "corroborated");
        assert_eq!(corr["git_ref"], "head1");
        assert_eq!(corr["mentions"], 1);
        // A kind never probed stays absent (skip-if-none within the wire).
        assert!(corr.get("content").is_none());
    }

    // ── u6h falsified-fence crux: real-Store regression net ──────────────
    // These drive the PUBLIC `retrieve(&mut Store, …)` against a REAL store
    // (a mock is disqualified — it could hand-set `is_falsified` and prove
    // nothing). They pin the kernel boundary the reviewer proved live: an
    // outcome record NEVER fences recall; only an explicit `falsifies` edge
    // does, and that edge dominates the softer lifecycle buckets.

    /// u6h SELF-ATTEST GUARD (the kernel boundary): recording an outcome
    /// that NAMES a capsule (`capsule_id` set) leaves that capsule's recall
    /// eligibility UNTOUCHED — it still grounds. Only the explicit
    /// `falsifies` edge fences it, flipping recall to
    /// `missing_evidence {falsified: 1}`. Outcome-record-alone can never
    /// fence: this is the whole u6h promise that an observation is not a
    /// consequence.
    #[test]
    fn outcome_naming_a_capsule_never_fences_it_only_the_falsifies_edge_does() {
        let mut store = Store::open_in_memory().unwrap();
        store
            .append(
                &cap("self attest probe alpha", "nott", 0.9, VF, None),
                APPENDED,
            )
            .unwrap();

        // An outcome that BEARS ON cap-1 (capsule_id set) — the strongest
        // self-attestation short of the edge. It mints out-1 and touches no
        // relation table.
        let outcome = store
            .append_outcome(
                "recall regressed after the pin bump",
                "session:2026-07-19",
                Some("ci://run/4821"),
                Some("cap-1"),
                None,
                None,
                APPENDED,
            )
            .unwrap();
        assert_eq!(outcome.record.id, "out-1");
        assert_eq!(outcome.record.capsule_id.as_deref(), Some("cap-1"));
        // The guard, at the store seam: the outcome alone set no fence.
        assert!(!store.is_falsified("cap-1").unwrap());

        // Recall still GROUNDS the capsule — outcome-record-alone never
        // fences. (Non-vacuity: if the outcome fenced, `grounded_ids`
        // panics on the missing_evidence it would return instead.)
        let response = retrieve(&mut store, &query(&["probe"]), NOW).unwrap();
        assert_eq!(grounded_ids(&response), ["cap-1"]);

        // Now the EXPLICIT edge: an observed outcome falsifies the claim.
        assert!(
            store
                .upsert_relation(RelationKind::Falsifies, "out-1", "cap-1", APPENDED)
                .unwrap(),
            "the falsifies edge is freshly inserted"
        );
        assert!(store.is_falsified("cap-1").unwrap());

        // Same query, same capsule — now fenced, counted exactly falsified.
        let response = retrieve(&mut store, &query(&["probe"]), NOW).unwrap();
        let RetrieveResponse::MissingEvidence {
            excluded_count,
            excluded,
            ..
        } = &response
        else {
            panic!("expected missing_evidence once falsified, got: {response:?}");
        };
        assert_eq!(*excluded_count, 1);
        assert_eq!(excluded, &BTreeMap::from([(ExclusionReason::Falsified, 1)]));
    }

    /// u6h FALSIFIED FENCE DOMINANCE (the extended law
    /// `quarantined → FALSIFIED → archived → superseded → currency`): a
    /// capsule that is BOTH falsified and lifecycle-fenced is counted under
    /// the DOMINANT reason. Falsified dominates superseded and archived (a
    /// falsified claim must never hide behind a softer bucket); quarantine —
    /// the taint signal — still dominates falsified. Each overlap is
    /// constructed on the REAL store and queried in isolation so the single
    /// dominant bucket is asserted directly.
    #[test]
    fn falsified_fence_dominates_archived_and_superseded_but_yields_to_quarantine() {
        let mut store = Store::open_in_memory().unwrap();
        store
            .append(
                &cap("alphaclaim under review", "nott", 0.9, VF, None),
                APPENDED,
            )
            .unwrap(); // cap-1
        store
            .append(
                &cap("betaclaim under review", "nott", 0.9, VF, None),
                APPENDED,
            )
            .unwrap(); // cap-2
        store
            .append(
                &cap("gammaclaim under review", "nott", 0.9, VF, None),
                APPENDED,
            )
            .unwrap(); // cap-3
        store
            .append(
                &cap("neutral successor capsule", "nott", 0.9, VF, None),
                APPENDED,
            )
            .unwrap(); // cap-4 — a live successor; matches none of the queries

        // Every claim is falsified (capsule→capsule falsifies is allowed;
        // the fence reads only `to_id`).
        for id in ["cap-1", "cap-2", "cap-3"] {
            assert!(
                store
                    .upsert_relation(RelationKind::Falsifies, "cap-4", id, APPENDED)
                    .unwrap()
            );
        }
        store.supersede("cap-1", "cap-4", APPENDED).unwrap(); // cap-1: +superseded
        store.set_tier("cap-2", Tier::Archived, APPENDED).unwrap(); // cap-2: +archived
        store
            .set_tier("cap-3", Tier::Quarantined, APPENDED)
            .unwrap(); // cap-3: +quarantined

        // Assert the single dominant bucket for one overlap in isolation.
        // (Non-vacuity: expecting the NON-dominant bucket — Superseded or
        // Archived below, or Falsified for the quarantined case — is red.)
        let only = |response: &RetrieveResponse, reason: ExclusionReason| {
            let RetrieveResponse::MissingEvidence {
                excluded_count,
                excluded,
                ..
            } = response
            else {
                panic!("expected missing_evidence, got: {response:?}");
            };
            assert_eq!(*excluded_count, 1);
            assert_eq!(excluded, &BTreeMap::from([(reason, 1)]));
        };

        // superseded + falsified → FALSIFIED (falsified dominates superseded).
        only(
            &retrieve(&mut store, &query(&["alphaclaim"]), NOW).unwrap(),
            ExclusionReason::Falsified,
        );
        // archived + falsified → FALSIFIED (falsified dominates archived).
        only(
            &retrieve(&mut store, &query(&["betaclaim"]), NOW).unwrap(),
            ExclusionReason::Falsified,
        );
        // quarantined + falsified → QUARANTINED (taint dominates falsified).
        only(
            &retrieve(&mut store, &query(&["gammaclaim"]), NOW).unwrap(),
            ExclusionReason::Quarantined,
        );
    }

    #[test]
    fn alias_matching_a_tombstoned_id_never_probes_tombstones() {
        // Aliases are store-derived data: only the caller's LITERAL
        // terms probe forgotten ids. An alias string equal to a
        // tombstoned id must not manufacture a Tombstoned exclusion.
        let mut store = Store::open_in_memory().unwrap();
        store
            .append(
                &cap("victim capsule content", "nott", 0.9, VF, None),
                APPENDED,
            )
            .unwrap();
        store
            .forget_capsule("cap-1", TombstoneMode::Purged, "test", b"key", APPENDED)
            .unwrap();
        store.add_alias("ghost", "cap-1", APPENDED).unwrap();

        let response = retrieve(&mut store, &query(&["ghost"]), NOW).unwrap();
        assert!(
            matches!(response, RetrieveResponse::Abstain { .. }),
            "alias-fed id string must not probe tombstones: {response:?}"
        );
    }

    #[test]
    fn real_store_project_prefix_fences_retrieve() {
        let mut store = Store::open_in_memory().unwrap();
        store
            .append(&cap("prefix subtree note", "nott", 0.9, VF, None), APPENDED)
            .unwrap();
        store
            .append(
                &cap("prefix subtree note two", "nott/sub", 0.9, VF, None),
                APPENDED,
            )
            .unwrap();
        store
            .append(
                &cap("prefix impostor note", "nottx", 0.9, VF, None),
                APPENDED,
            )
            .unwrap();

        let response = retrieve(
            &mut store,
            &RetrieveQuery {
                terms: vec!["prefix".to_string()],
                project_prefix: Some("nott".to_string()),
                ..RetrieveQuery::default()
            },
            NOW,
        )
        .unwrap();
        let mut ids = grounded_ids(&response);
        ids.sort();
        assert_eq!(ids, ["cap-1", "cap-2"], "nott + nott/sub in, nottx out");
    }

    #[test]
    fn anchor_probe_with_absent_root_degrades_to_unknown() {
        // w2 review: a probe with no root must not over-claim Missing.
        assert_eq!(
            anchor_liveness("src/lib.rs:1", Path::new("/nonexistent-root-xyzzy")),
            AnchorLive::Unknown
        );
    }

    #[test]
    fn empty_answers_name_the_prefix_fence_not_the_terms() {
        // w2-fix (fleet-2): terms that match store-wide but are excluded
        // by project_prefix must blame the FENCE in the reason —
        // symmetric with project_id.
        let mut store = Store::open_in_memory().unwrap();
        store
            .append(
                &cap("fencedterm lives in nott", "nott", 0.9, VF, None),
                APPENDED,
            )
            .unwrap();

        let abstain = retrieve(
            &mut store,
            &RetrieveQuery {
                terms: vec!["fencedterm".to_string()],
                project_prefix: Some("nott/zzz".to_string()),
                ..RetrieveQuery::default()
            },
            NOW,
        )
        .unwrap();
        let RetrieveResponse::Abstain { reason, .. } = &abstain else {
            panic!("expected abstain, got: {abstain:?}");
        };
        assert!(
            reason.contains("within project subtree 'nott/zzz'"),
            "the fence, not the terms, excluded everything: {reason}"
        );

        // Both fences set → both named.
        let both = retrieve(
            &mut store,
            &RetrieveQuery {
                terms: vec!["fencedterm".to_string()],
                project_id: Some("zzz".to_string()),
                project_prefix: Some("nott/zzz".to_string()),
                ..RetrieveQuery::default()
            },
            NOW,
        )
        .unwrap();
        let RetrieveResponse::Abstain { reason, .. } = &both else {
            panic!("expected abstain, got: {both:?}");
        };
        assert!(
            reason.contains("within project 'zzz' and subtree 'nott/zzz'"),
            "got: {reason}"
        );
    }

    // --- w3 u6a caller-fed vector lane + RRF fusion ---------------------

    /// Injected anchor root for the vector tests — a path that never exists,
    /// so `anchor_live` never touches the real repo.
    const NO_ROOT: &str = "/nonexistent-root";

    fn query_vec(terms: &[&str], embedding: Vec<f32>) -> RetrieveQuery {
        RetrieveQuery {
            terms: terms.iter().map(|t| (*t).to_string()).collect(),
            query_embedding: Some(embedding),
            ..RetrieveQuery::default()
        }
    }

    /// u05 regression: miss telemetry observes the term lane before result
    /// trimming. A count-only response still grounded on the term lane and
    /// therefore must not teach its query term as missing vocabulary.
    #[test]
    fn count_only_term_hit_does_not_record_a_recall_miss() {
        for (limit, token_budget) in [(Some(0), None), (None, Some(0))] {
            let mut store = seeded_store();
            let response = retrieve(
                &mut store,
                &RetrieveQuery {
                    terms: vec!["alpha".to_string()],
                    limit,
                    token_budget,
                    ..RetrieveQuery::default()
                },
                NOW,
            )
            .unwrap();
            assert!(
                matches!(
                    response,
                    RetrieveResponse::Grounded {
                        matched: 1,
                        returned: 0,
                        ..
                    }
                ),
                "count-only retrieve remains grounded: {response:?}"
            );
            assert_eq!(
                store.count_recall_misses().unwrap(),
                0,
                "pre-trim term evidence is not a vocabulary miss"
            );
        }
    }

    /// u05 regression: an explicitly forced vector lane never runs FTS.
    /// Its empty answer names the lane actually executed and cannot teach
    /// the unused term lane's query as missing vocabulary.
    #[test]
    fn forced_vector_empty_names_vector_lane_and_records_no_term_miss() {
        let mut store = seeded_store();
        let response = retrieve(
            &mut store,
            &RetrieveQuery {
                terms: vec!["alpha".to_string()],
                lane: Some(Lane::Vector),
                query_embedding: Some(vec![1.0, 0.0, 0.0]),
                ..RetrieveQuery::default()
            },
            NOW,
        )
        .unwrap();
        let RetrieveResponse::Abstain { reason, .. } = response else {
            panic!("forced vector with no stored vectors must abstain: {response:?}");
        };
        let misses = store.count_recall_misses().unwrap();
        assert!(
            reason.contains("forced vector lane") && !reason.contains("query term") && misses == 0,
            "reason={reason:?}; recall_miss_rows={misses}"
        );
    }

    /// Review regression: `vector_k = 0` is a valid cap, not evidence that
    /// the store had no positively similar vector. The forced-vector empty
    /// reason names the cap that removed the observed candidate and still
    /// says nothing about the unexecuted term lane.
    #[test]
    fn forced_vector_k_zero_names_the_cap_not_a_false_vector_miss() {
        let mut store = seeded_store();
        store
            .put_embedding("cap-2", &[1.0, 0.0, 0.0], "m", APPENDED)
            .unwrap();
        let response = retrieve(
            &mut store,
            &RetrieveQuery {
                terms: vec!["alpha".to_string()],
                lane: Some(Lane::Vector),
                query_embedding: Some(vec![1.0, 0.0, 0.0]),
                vector_k: Some(0),
                ..RetrieveQuery::default()
            },
            NOW,
        )
        .unwrap();
        let RetrieveResponse::Abstain { reason, .. } = response else {
            panic!("zero-cap forced vector must abstain: {response:?}");
        };
        assert!(
            reason.contains("found 1 positively similar stored capsule")
                && reason.contains("vector_k=0 excluded every candidate")
                && !reason.contains("found no stored capsule")
                && !reason.contains("query term"),
            "reason={reason:?}"
        );
        assert_eq!(store.count_recall_misses().unwrap(), 0);
    }

    /// Build a store with three planted capsules; caller decides embeddings.
    fn seeded_store() -> Store {
        let mut store = Store::open_in_memory().unwrap();
        store
            .append(&cap("alpha token budget", "nott", 0.9, VF, None), APPENDED)
            .unwrap();
        store
            .append(&cap("beta gravity waves", "nott", 0.9, VF, None), APPENDED)
            .unwrap();
        store
            .append(&cap("gamma vector fusion", "nott", 0.9, VF, None), APPENDED)
            .unwrap();
        store
    }

    fn seeded_store_with_vectors() -> Store {
        let mut store = seeded_store();
        store
            .put_embedding("cap-1", &[1.0, 0.0, 0.0], "m", APPENDED)
            .unwrap();
        store
            .put_embedding("cap-2", &[0.2, 0.8, 0.0], "m", APPENDED)
            .unwrap();
        store
    }

    #[test]
    fn omitted_auto_and_explicit_fused_preserve_historical_response_bytes() {
        // No embedding: omitted and auto both choose the historical term
        // path byte-for-byte.
        let mut omitted = seeded_store();
        let mut auto = seeded_store();
        let base = retrieve_core(
            &mut omitted,
            &query(&["alpha", "gravity"]),
            NOW,
            Path::new(NO_ROOT),
        )
        .unwrap();
        let auto_response = retrieve_core(
            &mut auto,
            &RetrieveQuery {
                terms: vec!["alpha".to_string(), "gravity".to_string()],
                lane: Some(Lane::Auto),
                ..RetrieveQuery::default()
            },
            NOW,
            Path::new(NO_ROOT),
        )
        .unwrap();
        assert_eq!(
            serde_json::to_vec(&base).unwrap(),
            serde_json::to_vec(&auto_response).unwrap()
        );

        // Embedding present: omitted, auto, and explicitly fused all choose
        // the historical fused path byte-for-byte.
        let request = || RetrieveQuery {
            terms: vec!["alpha".to_string(), "gravity".to_string()],
            query_embedding: Some(vec![1.0, 0.0, 0.0]),
            ..RetrieveQuery::default()
        };
        let mut omitted = seeded_store_with_vectors();
        let mut auto = seeded_store_with_vectors();
        let mut fused = seeded_store_with_vectors();
        let base = retrieve_core(&mut omitted, &request(), NOW, Path::new(NO_ROOT)).unwrap();
        let auto_response = retrieve_core(
            &mut auto,
            &RetrieveQuery {
                lane: Some(Lane::Auto),
                ..request()
            },
            NOW,
            Path::new(NO_ROOT),
        )
        .unwrap();
        let fused_response = retrieve_core(
            &mut fused,
            &RetrieveQuery {
                lane: Some(Lane::Fused),
                ..request()
            },
            NOW,
            Path::new(NO_ROOT),
        )
        .unwrap();
        let bytes = serde_json::to_vec(&base).unwrap();
        assert_eq!(bytes, serde_json::to_vec(&auto_response).unwrap());
        assert_eq!(bytes, serde_json::to_vec(&fused_response).unwrap());
    }

    #[test]
    fn forced_term_with_embedding_never_reads_vectors_and_matches_term_only_bytes() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("memory.sqlite3");
        let mut without_vector_table = Store::open(&path).unwrap();
        without_vector_table
            .append(&cap("alpha token budget", "nott", 0.9, VF, None), APPENDED)
            .unwrap();
        rusqlite::Connection::open(&path)
            .unwrap()
            .execute_batch("DROP TABLE embeddings")
            .unwrap();

        let with_embedding = retrieve_core(
            &mut without_vector_table,
            &RetrieveQuery {
                terms: vec!["alpha".to_string()],
                lane: Some(Lane::Term),
                query_embedding: Some(vec![1.0, 0.0, 0.0]),
                ..RetrieveQuery::default()
            },
            NOW,
            Path::new(NO_ROOT),
        )
        .unwrap();
        let mut term_only_store = Store::open_in_memory().unwrap();
        term_only_store
            .append(&cap("alpha token budget", "nott", 0.9, VF, None), APPENDED)
            .unwrap();
        let term_only = retrieve_core(
            &mut term_only_store,
            &RetrieveQuery {
                terms: vec!["alpha".to_string()],
                lane: Some(Lane::Term),
                ..RetrieveQuery::default()
            },
            NOW,
            Path::new(NO_ROOT),
        )
        .unwrap();
        let bytes = serde_json::to_vec(&with_embedding).unwrap();
        assert_eq!(bytes, serde_json::to_vec(&term_only).unwrap());
        let text = String::from_utf8(bytes).unwrap();
        assert!(!text.contains("vector_similarity") && !text.contains("fusion_rank"));
    }

    #[test]
    fn forced_term_rejects_intrinsically_invalid_embedding_without_vector_read_or_telemetry() {
        for bad in [vec![], vec![1.0, f32::NAN], vec![0.0, 0.0]] {
            let dir = tempfile::tempdir().unwrap();
            let path = dir.path().join("memory.sqlite3");
            let mut store = Store::open(&path).unwrap();
            store
                .append(&cap("alpha token budget", "nott", 0.9, VF, None), APPENDED)
                .unwrap();
            rusqlite::Connection::open(&path)
                .unwrap()
                .execute_batch("DROP TABLE embeddings")
                .unwrap();

            assert!(matches!(
                retrieve(
                    &mut store,
                    &RetrieveQuery {
                        terms: vec!["alpha".to_string()],
                        lane: Some(Lane::Term),
                        query_embedding: Some(bad),
                        ..RetrieveQuery::default()
                    },
                    NOW,
                ),
                Err(RetrieveError::InvalidQueryEmbedding(_))
            ));
            assert!(store.lane_override_totals().unwrap().is_empty());
            assert_eq!(store.count_recall_misses().unwrap(), 0);
            assert_eq!(store.receipt_returned_ids("rcpt-1").unwrap(), None);
        }
    }

    #[test]
    fn forced_vector_never_reads_fts_and_weight_blend_cannot_reintroduce_term_only_rows() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("memory.sqlite3");
        let mut store = Store::open(&path).unwrap();
        store
            .append(&cap("alpha term only", "nott", 0.9, VF, None), APPENDED)
            .unwrap(); // cap-1: term-only
        store
            .append(&cap("beta vector only", "nott", 0.9, VF, None), APPENDED)
            .unwrap(); // cap-2: vector-only
        store
            .put_embedding("cap-2", &[1.0, 0.0], "m", APPENDED)
            .unwrap();
        store.apply_feedback(&["cap-1"], 1.0, APPENDED).unwrap();
        store.apply_feedback(&["cap-2"], 0.0, APPENDED).unwrap();
        rusqlite::Connection::open(&path)
            .unwrap()
            .execute_batch("DROP TABLE capsules_fts; DROP TABLE synonyms")
            .unwrap();

        let response = retrieve_core(
            &mut store,
            &RetrieveQuery {
                terms: vec!["alpha".to_string()],
                lane: Some(Lane::Vector),
                query_embedding: Some(vec![1.0, 0.0]),
                weight_blend: Some(1.0),
                ..RetrieveQuery::default()
            },
            NOW,
            Path::new(NO_ROOT),
        )
        .unwrap();
        let RetrieveResponse::Grounded { results, .. } = response else {
            panic!("forced vector must ground on cap-2: {response:?}");
        };
        assert_eq!(results.len(), 1);
        let row = &results[0];
        assert_eq!(row.id.as_str(), "cap-2");
        assert!(row.matched_terms.is_empty());
        assert_eq!(row.vector_similarity, Some(1.0));
        assert_eq!(row.fusion_rank, Some(1));
        assert_eq!(row.feedback_weight, Some(0.45));
        assert!(row.relevance.is_none() && row.bm25.is_none());
    }

    #[test]
    fn fused_limit_trimming_a_term_hit_does_not_create_a_false_miss() {
        let mut store = Store::open_in_memory().unwrap();
        store
            .append(&cap("beta vector leader", "nott", 0.9, VF, None), APPENDED)
            .unwrap(); // cap-1 wins the one-lane RRF tie by seq
        store
            .append(&cap("alpha term survivor", "nott", 0.9, VF, None), APPENDED)
            .unwrap();
        store
            .put_embedding("cap-1", &[1.0, 0.0], "m", APPENDED)
            .unwrap();
        let response = retrieve(
            &mut store,
            &RetrieveQuery {
                terms: vec!["alpha".to_string()],
                lane: Some(Lane::Fused),
                query_embedding: Some(vec![1.0, 0.0]),
                limit: Some(1),
                ..RetrieveQuery::default()
            },
            NOW,
        )
        .unwrap();
        let RetrieveResponse::Grounded {
            results, matched, ..
        } = response
        else {
            panic!("expected grounded response: {response:?}");
        };
        assert_eq!(matched, 2);
        assert_eq!(results.len(), 1);
        assert!(results[0].matched_terms.is_empty());
        assert_eq!(store.count_recall_misses().unwrap(), 0);
    }

    #[test]
    fn forced_vector_keeps_the_lane_independent_tombstone_probe_honest() {
        let mut store = Store::open_in_memory().unwrap();
        store
            .append(
                &cap("forgotten vector host", "nott", 0.9, VF, None),
                APPENDED,
            )
            .unwrap();
        store
            .forget_capsule("cap-1", TombstoneMode::Purged, "test", b"key", APPENDED)
            .unwrap();
        let response = retrieve(
            &mut store,
            &RetrieveQuery {
                terms: vec!["cap-1".to_string()],
                lane: Some(Lane::Vector),
                query_embedding: Some(vec![1.0, 0.0]),
                ..RetrieveQuery::default()
            },
            NOW,
        )
        .unwrap();
        let RetrieveResponse::MissingEvidence {
            excluded, reason, ..
        } = response
        else {
            panic!("tombstone id probe must report missing evidence: {response:?}");
        };
        assert_eq!(excluded.get(&ExclusionReason::Tombstoned), Some(&1));
        assert!(reason.contains("forced vector lane") && reason.contains("tombstone id probe"));
        assert_eq!(store.count_recall_misses().unwrap(), 0);
    }

    #[test]
    fn only_successful_explicit_disagreements_record_override_telemetry() {
        let mut store = seeded_store();
        let term_override = RetrieveQuery {
            terms: vec!["alpha".to_string()],
            lane: Some(Lane::Term),
            query_embedding: Some(vec![1.0, 0.0, 0.0]),
            ..RetrieveQuery::default()
        };
        retrieve(&mut store, &term_override, NOW).unwrap();
        let vector_override = RetrieveQuery {
            terms: vec!["no-vector-row".to_string()],
            lane: Some(Lane::Vector),
            query_embedding: Some(vec![1.0, 0.0, 0.0]),
            ..RetrieveQuery::default()
        };
        retrieve(&mut store, &vector_override, NOW).unwrap();

        // Explicit choices equal to auto are not overrides.
        retrieve(
            &mut store,
            &RetrieveQuery {
                terms: vec!["alpha".to_string()],
                lane: Some(Lane::Term),
                ..RetrieveQuery::default()
            },
            NOW,
        )
        .unwrap();
        retrieve(
            &mut store,
            &RetrieveQuery {
                terms: vec!["alpha".to_string()],
                lane: Some(Lane::Fused),
                query_embedding: Some(vec![1.0, 0.0, 0.0]),
                ..RetrieveQuery::default()
            },
            NOW,
        )
        .unwrap();
        retrieve(
            &mut store,
            &RetrieveQuery {
                terms: vec!["alpha".to_string()],
                lane: Some(Lane::Auto),
                query_embedding: Some(vec![1.0, 0.0, 0.0]),
                ..RetrieveQuery::default()
            },
            NOW,
        )
        .unwrap();

        // Rejected vector-bearing lanes record nothing.
        for lane in [Lane::Vector, Lane::Fused] {
            let error = retrieve(
                &mut store,
                &RetrieveQuery {
                    terms: vec!["alpha".to_string()],
                    lane: Some(lane),
                    ..RetrieveQuery::default()
                },
                NOW,
            )
            .unwrap_err();
            assert_eq!(
                error,
                RetrieveError::LaneNeedsEmbedding(lane.as_str()),
                "lane {lane:?} teaches the missing vector"
            );
        }
        assert_eq!(
            store.lane_override_totals().unwrap(),
            vec![
                ("term".to_string(), "fused".to_string(), 1),
                ("vector".to_string(), "fused".to_string(), 1),
            ]
        );
    }

    #[test]
    fn missing_override_table_never_fails_a_successful_retrieve() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("memory.sqlite3");
        let mut store = Store::open(&path).unwrap();
        store
            .append(
                &cap("alpha telemetry host", "nott", 0.9, VF, None),
                APPENDED,
            )
            .unwrap();
        rusqlite::Connection::open(&path)
            .unwrap()
            .execute_batch("DROP TABLE lane_overrides")
            .unwrap();
        let response = retrieve(
            &mut store,
            &RetrieveQuery {
                terms: vec!["alpha".to_string()],
                lane: Some(Lane::Term),
                query_embedding: Some(vec![1.0, 0.0]),
                ..RetrieveQuery::default()
            },
            NOW,
        )
        .unwrap();
        assert!(matches!(response, RetrieveResponse::Grounded { .. }));
    }

    #[test]
    fn weight_blend_reorders_by_feedback_weight() {
        let mut store = Store::open_in_memory().unwrap();
        store
            .append(
                &cap("feedback blend alpha one", "nott", 0.9, VF, None),
                APPENDED,
            )
            .unwrap();
        store
            .append(
                &cap("feedback blend alpha two", "nott", 0.9, VF, None),
                APPENDED,
            )
            .unwrap();

        let base = retrieve_core(
            &mut store,
            &query(&["feedback blend alpha"]),
            NOW,
            Path::new(NO_ROOT),
        )
        .unwrap();
        assert_eq!(grounded_ids(&base), ["cap-1", "cap-2"]);
        store.apply_feedback(&["cap-2"], 1.0, APPENDED).unwrap();

        let blended = retrieve_core(
            &mut store,
            &RetrieveQuery {
                terms: vec!["feedback blend alpha".to_string()],
                weight_blend: Some(1.0),
                ..RetrieveQuery::default()
            },
            NOW,
            Path::new(NO_ROOT),
        )
        .unwrap();
        assert_eq!(grounded_ids(&blended), ["cap-2", "cap-1"]);
        let RetrieveResponse::Grounded { results, .. } = blended else {
            panic!("expected grounded response");
        };
        assert_eq!(results[0].feedback_weight, Some(0.55));
        assert_eq!(results[1].feedback_weight, Some(0.5));
    }

    #[test]
    fn weight_blend_reorders_fused_results_but_keeps_preblend_fusion_rank() {
        let mut store = Store::open_in_memory().unwrap();
        for content in ["fused feedback alpha one", "fused feedback alpha two"] {
            store
                .append(&cap(content, "nott", 0.9, VF, None), APPENDED)
                .unwrap();
        }
        for id in ["cap-1", "cap-2"] {
            store
                .put_embedding(id, &[1.0, 0.0], "feedback-model", APPENDED)
                .unwrap();
        }
        store.apply_feedback(&["cap-2"], 1.0, APPENDED).unwrap();

        let response = retrieve_core(
            &mut store,
            &RetrieveQuery {
                terms: vec!["fused feedback alpha".to_string()],
                query_embedding: Some(vec![1.0, 0.0]),
                weight_blend: Some(1.0),
                ..RetrieveQuery::default()
            },
            NOW,
            Path::new(NO_ROOT),
        )
        .unwrap();
        let RetrieveResponse::Grounded { results, .. } = response else {
            panic!("expected grounded response");
        };
        assert_eq!(
            results
                .iter()
                .map(|result| result.id.as_str())
                .collect::<Vec<_>>(),
            ["cap-2", "cap-1"]
        );
        assert_eq!(
            results
                .iter()
                .map(|result| result.fusion_rank)
                .collect::<Vec<_>>(),
            [Some(2), Some(1)],
            "fusion_rank names the pre-blend RRF order"
        );
    }

    #[test]
    fn weight_blend_zero_is_a_mathematical_noop_with_no_weight_table() {
        fn seeded_without_weights() -> (tempfile::TempDir, Store) {
            let dir = tempfile::tempdir().unwrap();
            let path = dir.path().join("memory.sqlite3");
            let mut store = Store::open(&path).unwrap();
            store
                .append(
                    &cap("dormant feedback alpha one", "nott", 0.9, VF, None),
                    APPENDED,
                )
                .unwrap();
            store
                .append(
                    &cap("dormant feedback alpha two", "nott", 0.9, VF, None),
                    APPENDED,
                )
                .unwrap();
            rusqlite::Connection::open(&path)
                .unwrap()
                .execute_batch("DROP TABLE feedback_weights")
                .unwrap();
            (dir, store)
        }

        let (_none_dir, mut none_store) = seeded_without_weights();
        let (_zero_dir, mut zero_store) = seeded_without_weights();
        let none = retrieve_core(
            &mut none_store,
            &query(&["dormant feedback alpha"]),
            NOW,
            Path::new(NO_ROOT),
        )
        .unwrap();
        let zero = retrieve_core(
            &mut zero_store,
            &RetrieveQuery {
                terms: vec!["dormant feedback alpha".to_string()],
                weight_blend: Some(0.0),
                ..RetrieveQuery::default()
            },
            NOW,
            Path::new(NO_ROOT),
        )
        .unwrap();
        let none_bytes = serde_json::to_vec(&none).unwrap();
        let zero_bytes = serde_json::to_vec(&zero).unwrap();
        assert_eq!(none_bytes, zero_bytes);
        assert!(
            !String::from_utf8(none_bytes)
                .unwrap()
                .contains("feedback_weight")
        );
    }

    #[test]
    fn weight_blend_rejects_non_finite_and_out_of_range_before_store_reads() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("memory.sqlite3");
        let mut store = Store::open(&path).unwrap();
        store
            .append(&cap("valid term", "nott", 0.9, VF, None), APPENDED)
            .unwrap();
        rusqlite::Connection::open(&path)
            .unwrap()
            .execute_batch("DROP TABLE feedback_weights")
            .unwrap();
        for blend in [-0.01, 1.01, f64::NAN, f64::INFINITY] {
            let error = retrieve_core(
                &mut store,
                &RetrieveQuery {
                    terms: vec!["valid term".to_string()],
                    weight_blend: Some(blend),
                    ..RetrieveQuery::default()
                },
                NOW,
                Path::new(NO_ROOT),
            )
            .unwrap_err();
            assert!(matches!(error, RetrieveError::InvalidWeightBlend(_)));
        }
    }

    /// S6 fixture seam: a REAL inner store (real FTS + ranking) plus injected
    /// git-witness corroboration weights, so the blend is driven with S2
    /// semantics before S2 lands. Base order is deterministic ([cap-1,
    /// cap-2]) for two symmetric capsules — the append-order tiebreak.
    fn corroboration_fixture(contents: [&str; 2], weights: [(&str, f64); 2]) -> ContractStore {
        let mut inner = Store::open_in_memory().unwrap();
        for content in contents {
            inner
                .append(&cap(content, "nott", 0.9, VF, None), APPENDED)
                .unwrap();
        }
        let mut store = ContractStore::new(inner);
        for (id, weight) in weights {
            store.corroboration_weights.insert(id.to_string(), weight);
        }
        store
    }

    /// S6 red-test 1 — DORMANCY: an omitted `corroboration_blend` and an
    /// explicit `0.0` produce byte-identical envelopes AND perform zero
    /// corroboration RANKING-weight reads, even though the independent
    /// envelope explain may read real corroboration data. The
    /// `corroboration_weight` field never reaches the wire.
    #[test]
    fn corroboration_blend_dormant_is_byte_identical_without_ranking_reads() {
        let contents = [
            "corroboration dormant alpha one",
            "corroboration dormant alpha two",
        ];
        let weights = [("cap-1", 0.0), ("cap-2", 1.0)];

        let mut omitted = corroboration_fixture(contents, weights);
        let base = retrieve_core(
            &mut omitted,
            &query(&["corroboration dormant alpha"]),
            NOW,
            Path::new(NO_ROOT),
        )
        .unwrap();
        assert_eq!(
            omitted.corroboration_ranking_reads.get(),
            0,
            "omitted corroboration_blend reads no corroboration ranking weight"
        );

        let mut zeroed = corroboration_fixture(contents, weights);
        let zero = retrieve_core(
            &mut zeroed,
            &RetrieveQuery {
                terms: vec!["corroboration dormant alpha".to_string()],
                corroboration_blend: Some(0.0),
                ..RetrieveQuery::default()
            },
            NOW,
            Path::new(NO_ROOT),
        )
        .unwrap();
        assert_eq!(
            zeroed.corroboration_ranking_reads.get(),
            0,
            "corroboration_blend 0.0 reads no corroboration ranking weight"
        );

        let base_bytes = serde_json::to_vec(&base).unwrap();
        let zero_bytes = serde_json::to_vec(&zero).unwrap();
        assert_eq!(
            base_bytes, zero_bytes,
            "dormant envelopes are byte-identical"
        );
        assert!(
            !String::from_utf8(zero_bytes)
                .unwrap()
                .contains("corroboration_weight"),
            "the dormant path never serializes corroboration_weight"
        );
    }

    /// S6 red-test 2 — cb>0 with every corroboration weight ABSENT (all
    /// `None`) leaves the base order untouched (every factor is neutral 1.0)
    /// and stamps `corroboration_weight: 0.5` (neutral) on each envelope.
    #[test]
    fn corroboration_blend_with_absent_weights_is_neutral_and_present() {
        let mut store = corroboration_fixture(
            [
                "corroboration neutral alpha one",
                "corroboration neutral alpha two",
            ],
            [("cap-other", 1.0), ("cap-none", 0.0)],
        );
        let base = retrieve_core(
            &mut store,
            &query(&["corroboration neutral alpha"]),
            NOW,
            Path::new(NO_ROOT),
        )
        .unwrap();
        assert_eq!(grounded_ids(&base), ["cap-1", "cap-2"]);

        let blended = retrieve_core(
            &mut store,
            &RetrieveQuery {
                terms: vec!["corroboration neutral alpha".to_string()],
                corroboration_blend: Some(1.0),
                ..RetrieveQuery::default()
            },
            NOW,
            Path::new(NO_ROOT),
        )
        .unwrap();
        assert_eq!(
            grounded_ids(&blended),
            ["cap-1", "cap-2"],
            "all-absent corroboration weights preserve the base order"
        );
        let RetrieveResponse::Grounded { results, .. } = blended else {
            panic!("expected grounded response");
        };
        assert_eq!(results[0].corroboration_weight, Some(0.5));
        assert_eq!(results[1].corroboration_weight, Some(0.5));
    }

    /// S6 red-test 3 — a DRIFTED top hit (corroboration weight `0.0`) with
    /// `corroboration_blend: 1.0` re-ranks BELOW a corroborated peer (weight
    /// `1.0`); `corroboration_blend: 0.0` restores the base order and performs
    /// no corroboration ranking-weight reads.
    #[test]
    fn corroboration_blend_demotes_drifted_below_corroborated_peer() {
        let contents = [
            "corroboration rank alpha one",
            "corroboration rank alpha two",
        ];
        // cap-1 leads the base order but is drifted; cap-2 trails but is
        // corroborated.
        let weights = [("cap-1", 0.0), ("cap-2", 1.0)];

        let mut base_store = corroboration_fixture(contents, weights);
        let base = retrieve_core(
            &mut base_store,
            &query(&["corroboration rank alpha"]),
            NOW,
            Path::new(NO_ROOT),
        )
        .unwrap();
        assert_eq!(
            grounded_ids(&base),
            ["cap-1", "cap-2"],
            "base order: cap-1 leads"
        );

        let mut blended_store = corroboration_fixture(contents, weights);
        let blended = retrieve_core(
            &mut blended_store,
            &RetrieveQuery {
                terms: vec!["corroboration rank alpha".to_string()],
                corroboration_blend: Some(1.0),
                ..RetrieveQuery::default()
            },
            NOW,
            Path::new(NO_ROOT),
        )
        .unwrap();
        assert_eq!(
            grounded_ids(&blended),
            ["cap-2", "cap-1"],
            "cb=1.0 demotes the drifted top hit below its corroborated peer"
        );
        let RetrieveResponse::Grounded { results, .. } = &blended else {
            panic!("expected grounded response");
        };
        assert_eq!(results[0].corroboration_weight, Some(1.0));
        assert_eq!(results[1].corroboration_weight, Some(0.0));

        let mut restore_store = corroboration_fixture(contents, weights);
        let restored = retrieve_core(
            &mut restore_store,
            &RetrieveQuery {
                terms: vec!["corroboration rank alpha".to_string()],
                corroboration_blend: Some(0.0),
                ..RetrieveQuery::default()
            },
            NOW,
            Path::new(NO_ROOT),
        )
        .unwrap();
        assert_eq!(
            grounded_ids(&restored),
            ["cap-1", "cap-2"],
            "cb=0.0 restores the base order"
        );
        assert_eq!(
            restore_store.corroboration_ranking_reads.get(),
            0,
            "cb=0.0 reads no corroboration ranking weight"
        );
    }

    /// S6→S2 integration proof (the deferred join): the REAL
    /// `impl RecallStore for Store::corroboration_weight` reads a capsule's
    /// newest `anchor_content` verdict from S2's `corroborations` sidecar —
    /// `corroborated` → `1.0`, `drifted` → `0.0`, a never-scanned capsule
    /// (no `anchor_content` row) → `None` (neutral). Then a
    /// `corroboration_blend: 1.0` retrieve over REAL store data demotes the
    /// drifted capsule below its corroborated peer. The fixture double could
    /// only inject weights; this exercises the actual sidecar read.
    #[test]
    fn corroboration_weight_reads_real_anchor_content_verdict_and_demotes() {
        let mut store = Store::open_in_memory().unwrap();
        let corroborated = store
            .append(
                &cap("realwitness weight alpha one", "nott", 0.9, VF, None),
                APPENDED,
            )
            .unwrap();
        let drifted = store
            .append(
                &cap("realwitness weight alpha two", "nott", 0.9, VF, None),
                APPENDED,
            )
            .unwrap();
        let unscanned = store
            .append(
                &cap("realwitness weight alpha three", "nott", 0.9, VF, None),
                APPENDED,
            )
            .unwrap();
        store
            .append_corroboration(
                corroborated.as_str(),
                "git",
                "anchor_content",
                "a.rs",
                "corroborated",
                Some("head1"),
                APPENDED,
            )
            .unwrap();
        store
            .append_corroboration(
                drifted.as_str(),
                "git",
                "anchor_content",
                "b.rs",
                "drifted",
                Some("head1"),
                APPENDED,
            )
            .unwrap();

        // Direct wiring: the newest anchor_content verdict maps to the ranking
        // weight; a never-scanned capsule stays None (never a fabricated 0.5).
        assert_eq!(
            RecallStore::corroboration_weight(&store, &corroborated),
            Some(1.0)
        );
        assert_eq!(
            RecallStore::corroboration_weight(&store, &drifted),
            Some(0.0)
        );
        assert_eq!(RecallStore::corroboration_weight(&store, &unscanned), None);

        // Base order (dormant): append order, cap-2 (drifted) ranks mid.
        let base = retrieve_core(
            &mut store,
            &query(&["realwitness weight alpha"]),
            NOW,
            Path::new(NO_ROOT),
        )
        .unwrap();
        assert_eq!(grounded_ids(&base), ["cap-1", "cap-2", "cap-3"]);

        // cb=1.0 over REAL sidecar rows: the drifted cap-2 (weight 0.0) sinks
        // BELOW the corroborated cap-1 (1.0) and the neutral cap-3 (None→0.5).
        let blended = retrieve_core(
            &mut store,
            &RetrieveQuery {
                terms: vec!["realwitness weight alpha".to_string()],
                corroboration_blend: Some(1.0),
                ..RetrieveQuery::default()
            },
            NOW,
            Path::new(NO_ROOT),
        )
        .unwrap();
        assert_eq!(
            grounded_ids(&blended),
            ["cap-1", "cap-3", "cap-2"],
            "cb=1.0 demotes the drifted capsule below its corroborated peer using REAL store data"
        );
        let RetrieveResponse::Grounded { results, .. } = &blended else {
            panic!("expected grounded response, got: {blended:?}");
        };
        assert_eq!(
            results[0].corroboration_weight,
            Some(1.0),
            "cap-1 corroborated"
        );
        assert_eq!(
            results[1].corroboration_weight,
            Some(0.5),
            "cap-3 never-scanned → neutral"
        );
        assert_eq!(results[2].corroboration_weight, Some(0.0), "cap-2 drifted");
    }

    /// S6 red-test 4 (engine) — an out-of-range or non-finite
    /// `corroboration_blend` is rejected by the engine BEFORE any store read,
    /// mirroring the `weight_blend` guard.
    #[test]
    fn corroboration_blend_rejects_non_finite_and_out_of_range_before_store_reads() {
        let mut store = Store::open_in_memory().unwrap();
        store
            .append(&cap("valid term", "nott", 0.9, VF, None), APPENDED)
            .unwrap();
        for blend in [-0.01, 1.01, f64::NAN, f64::INFINITY] {
            let error = retrieve_core(
                &mut store,
                &RetrieveQuery {
                    terms: vec!["valid term".to_string()],
                    corroboration_blend: Some(blend),
                    ..RetrieveQuery::default()
                },
                NOW,
                Path::new(NO_ROOT),
            )
            .unwrap_err();
            assert!(matches!(error, RetrieveError::InvalidCorroborationBlend(_)));
        }
    }

    /// S6 red-test 5 — both blends set COMPOSE multiplicatively in ONE
    /// re-sort, each reading its OWN sidecar (independence). Opposing signals:
    /// feedback favors cap-1 (0.55), corroboration favors cap-2 (1.0 vs 0.0).
    /// weight_blend alone keeps cap-1 on top; corroboration_blend alone (and
    /// the composed product) flips to cap-2 — and both explain weights ride
    /// the envelope only when their blend is active.
    #[test]
    fn both_blends_compose_multiplicatively_in_one_sort() {
        fn seeded() -> ContractStore {
            let mut inner = Store::open_in_memory().unwrap();
            for content in ["compose blend alpha one", "compose blend alpha two"] {
                inner
                    .append(&cap(content, "nott", 0.9, VF, None), APPENDED)
                    .unwrap();
            }
            // Feedback nudges cap-1 up to 0.55 (EMA alpha 0.1 from 0.5).
            inner.apply_feedback(&["cap-1"], 1.0, APPENDED).unwrap();
            let mut store = ContractStore::new(inner);
            store.corroboration_weights.insert("cap-1".to_string(), 0.0);
            store.corroboration_weights.insert("cap-2".to_string(), 1.0);
            store
        }

        // weight_blend ALONE: feedback keeps cap-1 leading; corroboration
        // ranking-weight seam untouched, corroboration_weight absent.
        let mut wb_only = seeded();
        let wb = retrieve_core(
            &mut wb_only,
            &RetrieveQuery {
                terms: vec!["compose blend alpha".to_string()],
                weight_blend: Some(1.0),
                ..RetrieveQuery::default()
            },
            NOW,
            Path::new(NO_ROOT),
        )
        .unwrap();
        assert_eq!(grounded_ids(&wb), ["cap-1", "cap-2"]);
        assert_eq!(
            wb_only.corroboration_ranking_reads.get(),
            0,
            "weight_blend alone reads no corroboration ranking weight"
        );
        let RetrieveResponse::Grounded { results, .. } = &wb else {
            panic!("expected grounded response");
        };
        assert_eq!(results[0].feedback_weight, Some(0.55));
        assert_eq!(
            results[0].corroboration_weight, None,
            "corroboration_weight is absent when its blend is dormant"
        );

        // corroboration_blend ALONE: flips to cap-2; feedback dormant/absent.
        let mut cb_only = seeded();
        let cb = retrieve_core(
            &mut cb_only,
            &RetrieveQuery {
                terms: vec!["compose blend alpha".to_string()],
                corroboration_blend: Some(1.0),
                ..RetrieveQuery::default()
            },
            NOW,
            Path::new(NO_ROOT),
        )
        .unwrap();
        assert_eq!(grounded_ids(&cb), ["cap-2", "cap-1"]);
        let RetrieveResponse::Grounded { results, .. } = &cb else {
            panic!("expected grounded response");
        };
        assert_eq!(results[0].corroboration_weight, Some(1.0));
        assert_eq!(
            results[0].feedback_weight, None,
            "feedback_weight is absent when its blend is dormant"
        );

        // BOTH: the two factors multiply in ONE sort; corroboration's swing
        // wins the product, and BOTH explain weights ride the envelope.
        let mut both = seeded();
        let composed = retrieve_core(
            &mut both,
            &RetrieveQuery {
                terms: vec!["compose blend alpha".to_string()],
                weight_blend: Some(1.0),
                corroboration_blend: Some(1.0),
                ..RetrieveQuery::default()
            },
            NOW,
            Path::new(NO_ROOT),
        )
        .unwrap();
        assert_eq!(
            grounded_ids(&composed),
            ["cap-2", "cap-1"],
            "the multiplied factors re-sort once: cap-2 leads"
        );
        let RetrieveResponse::Grounded { results, .. } = &composed else {
            panic!("expected grounded response");
        };
        // cap-2 leads: feedback neutral 0.5, corroboration 1.0.
        assert_eq!(results[0].feedback_weight, Some(0.5));
        assert_eq!(results[0].corroboration_weight, Some(1.0));
        // cap-1 trails: feedback 0.55, corroboration 0.0.
        assert_eq!(results[1].feedback_weight, Some(0.55));
        assert_eq!(results[1].corroboration_weight, Some(0.0));
    }

    /// RED (dormant differential): with `query_embedding` ABSENT the
    /// response is byte-identical whether or not embeddings are stored — the
    /// vector table is inert, and no vector field reaches the wire. This is
    /// the u6a dormancy law: absent ⇒ today's engine, exactly.
    #[test]
    fn dormant_query_is_byte_identical_regardless_of_embeddings() {
        let mut bare = seeded_store();
        let mut with_vectors = seeded_store();
        // Load embeddings ONLY into the second store.
        for id in ["cap-1", "cap-2", "cap-3"] {
            with_vectors
                .put_embedding(id, &[0.5, 0.5, 0.5], "m", APPENDED)
                .unwrap();
        }
        let q = query(&["alpha", "gravity"]); // query_embedding: None (dormant)
        let a = retrieve_core(&mut bare, &q, NOW, Path::new(NO_ROOT)).unwrap();
        let b = retrieve_core(&mut with_vectors, &q, NOW, Path::new(NO_ROOT)).unwrap();
        let a_json = serde_json::to_string(&a).unwrap();
        let b_json = serde_json::to_string(&b).unwrap();
        assert_eq!(a_json, b_json, "dormant recall ignores the vector sidecar");
        assert!(
            !a_json.contains("vector_similarity") && !a_json.contains("fusion_rank"),
            "no vector explain fields on a dormant response: {a_json}"
        );
    }

    /// A fused query annotates the wire: `fusion_rank` on every returned
    /// row, `vector_similarity` on a row the vector lane matched. A row
    /// matched by BOTH lanes carries matched_terms AND vector_similarity.
    #[test]
    fn fused_query_carries_vector_explain() {
        let mut store = seeded_store();
        // cap-1 gets the query's exact vector (cosine 1.0); cap-2 a small
        // but POSITIVE similarity (≈0.1) — the lane admits only cosine > 0
        // (fleet-8 c7 F1), so the vector-only explain is proven on a
        // legally-admitted row, never an orthogonal one.
        store
            .put_embedding("cap-1", &[1.0, 0.0, 0.0], "m", APPENDED)
            .unwrap();
        store
            .put_embedding("cap-2", &[0.1, 0.99, 0.0], "m", APPENDED)
            .unwrap();
        let q = query_vec(&["alpha"], vec![1.0, 0.0, 0.0]);
        let response = retrieve_core(&mut store, &q, NOW, Path::new(NO_ROOT)).unwrap();
        let RetrieveResponse::Grounded { results, .. } = &response else {
            panic!("expected grounded, got {response:?}");
        };
        let cap1 = results.iter().find(|e| e.id.as_str() == "cap-1").unwrap();
        // cap-1 matched BOTH lanes: term "alpha" AND vector cosine 1.0.
        assert_eq!(cap1.matched_terms, vec!["alpha".to_string()]);
        assert_eq!(cap1.vector_similarity, Some(1.0));
        assert!(cap1.fusion_rank.is_some(), "fused row carries fusion_rank");
        // cap-2 matched ONLY the vector lane: no term, but a similarity.
        let cap2 = results.iter().find(|e| e.id.as_str() == "cap-2").unwrap();
        assert!(cap2.matched_terms.is_empty(), "cap-2 has no term match");
        assert!(
            cap2.vector_similarity.is_some(),
            "cap-2 has a vector explain"
        );
    }

    /// fleet-8 c7 F1: the vector lane admits only POSITIVE similarity —
    /// an orthogonal (0.0) or anti-correlated (<0) embedding never
    /// solely-grounds, the outcome stays honest, and the term-miss reaches
    /// the u-r5 ledger even when a positive vector match grounds (pre-fix
    /// the lane grounded at cosine 0.0/-1.0 and silently starved the
    /// ledger; observed live by the fleet consumer).
    #[test]
    fn nonpositive_cosine_never_solely_grounds_and_the_term_miss_is_recorded() {
        let mut store = seeded_store();
        store
            .put_embedding("cap-1", &[0.0, 1.0, 0.0], "m", APPENDED)
            .unwrap();
        // Orthogonal embedding + terms matching nothing → NOT grounded,
        // and the miss reaches the ledger.
        let q = query_vec(&["qzxnomatch"], vec![1.0, 0.0, 0.0]);
        let response = retrieve(&mut store, &q, NOW).unwrap();
        assert!(
            !matches!(response, RetrieveResponse::Grounded { .. }),
            "an orthogonal embedding must not solely-ground: {response:?}"
        );
        assert_eq!(
            store.count_recall_misses().unwrap(),
            1,
            "the miss reached the ledger"
        );
        // Anti-correlated is equally inadmissible.
        let q = query_vec(&["qzxnomatch"], vec![0.0, -1.0, 0.0]);
        let response = retrieve(&mut store, &q, NOW).unwrap();
        assert!(!matches!(response, RetrieveResponse::Grounded { .. }));
        assert_eq!(store.count_recall_misses().unwrap(), 2);
        // POSITIVE similarity grounds (vector-only) — and the TERM miss
        // STILL reaches the ledger: the R5 vocabulary loop survives the
        // very lane its evidence gates.
        let q = query_vec(&["qzxnomatch"], vec![0.0, 0.9, 0.1]);
        let response = retrieve(&mut store, &q, NOW).unwrap();
        assert!(
            matches!(response, RetrieveResponse::Grounded { .. }),
            "positive cosine grounds: {response:?}"
        );
        assert_eq!(
            store.count_recall_misses().unwrap(),
            3,
            "a vector-grounded term-miss still records"
        );
        // A term HIT records nothing — the unchanged law.
        let q = query(&["alpha"]);
        let _ = retrieve(&mut store, &q, NOW).unwrap();
        assert_eq!(store.count_recall_misses().unwrap(), 3);
    }

    /// RED (RRF determinism): the same lane inputs always yield the same
    /// fused order. Proven on two freshly-built identical stores (so recall
    /// counting cannot perturb the second run).
    #[test]
    fn rrf_fusion_is_deterministic() {
        let build = || {
            let mut store = seeded_store();
            store
                .put_embedding("cap-1", &[0.9, 0.1, 0.0], "m", APPENDED)
                .unwrap();
            store
                .put_embedding("cap-2", &[0.1, 0.9, 0.0], "m", APPENDED)
                .unwrap();
            store
                .put_embedding("cap-3", &[0.0, 0.1, 0.9], "m", APPENDED)
                .unwrap();
            store
        };
        let q = query_vec(&["alpha", "gamma"], vec![0.5, 0.1, 0.4]);
        let mut first = build();
        let mut second = build();
        let a = retrieve_core(&mut first, &q, NOW, Path::new(NO_ROOT)).unwrap();
        let b = retrieve_core(&mut second, &q, NOW, Path::new(NO_ROOT)).unwrap();
        assert_eq!(
            grounded_ids(&a),
            grounded_ids(&b),
            "fused order is deterministic"
        );
        // And identical bytes end to end.
        assert_eq!(
            serde_json::to_string(&a).unwrap(),
            serde_json::to_string(&b).unwrap()
        );
    }

    /// RED (fence dominance, vector lane): a QUARANTINED capsule whose
    /// embedding is the query's exact vector (cosine 1.0 — it would lead the
    /// vector lane) must NOT surface, and is counted under `quarantined`.
    /// The term lane never matched it either, so this isolates the vector
    /// lane: without the lane-agnostic fence it would rank first.
    #[test]
    fn quarantined_capsule_never_surfaces_via_vector_lane() {
        let mut store = seeded_store();
        // cap-2 ("beta gravity waves") is quarantined and carries the exact
        // query vector; the query term "alpha" matches cap-1 only.
        store
            .put_embedding("cap-2", &[1.0, 0.0, 0.0], "m", APPENDED)
            .unwrap();
        store
            .set_tier("cap-2", Tier::Quarantined, APPENDED)
            .unwrap();
        let q = query_vec(&["alpha"], vec![1.0, 0.0, 0.0]);
        let response = retrieve_core(&mut store, &q, NOW, Path::new(NO_ROOT)).unwrap();
        let RetrieveResponse::Grounded {
            results, excluded, ..
        } = &response
        else {
            panic!("expected grounded, got {response:?}");
        };
        let ids: Vec<&str> = results.iter().map(|e| e.id.as_str()).collect();
        assert!(
            !ids.contains(&"cap-2"),
            "quarantined capsule must not surface: {ids:?}"
        );
        assert_eq!(
            excluded.get(&ExclusionReason::Quarantined),
            Some(&1),
            "the vector-lane match is counted quarantined"
        );
    }

    /// A superseded capsule is likewise fenced from the vector lane.
    #[test]
    fn superseded_capsule_never_surfaces_via_vector_lane() {
        let mut store = seeded_store();
        store
            .put_embedding("cap-2", &[1.0, 0.0, 0.0], "m", APPENDED)
            .unwrap();
        // cap-3 supersedes cap-2.
        store
            .upsert_relation(
                crate::store::RelationKind::Supersedes,
                "cap-3",
                "cap-2",
                APPENDED,
            )
            .unwrap();
        let q = query_vec(&["alpha"], vec![1.0, 0.0, 0.0]);
        let response = retrieve_core(&mut store, &q, NOW, Path::new(NO_ROOT)).unwrap();
        let RetrieveResponse::Grounded {
            results, excluded, ..
        } = &response
        else {
            panic!("expected grounded, got {response:?}");
        };
        let ids: Vec<&str> = results.iter().map(|e| e.id.as_str()).collect();
        assert!(
            !ids.contains(&"cap-2"),
            "superseded capsule must not surface: {ids:?}"
        );
        assert_eq!(excluded.get(&ExclusionReason::Superseded), Some(&1));
    }

    /// RED (dimension mismatch): a query_embedding whose length differs from
    /// a stored embedding is a teaching error naming BOTH dimensions.
    #[test]
    fn dimension_mismatch_teaches_both_dimensions() {
        let mut store = seeded_store();
        store
            .put_embedding("cap-1", &[1.0, 2.0, 3.0], "m", APPENDED)
            .unwrap();
        let q = query_vec(&["alpha"], vec![1.0, 2.0, 3.0, 4.0]); // dim 4 vs stored 3
        let err = retrieve_core(&mut store, &q, NOW, Path::new(NO_ROOT)).unwrap_err();
        match &err {
            RetrieveError::DimensionMismatch {
                query,
                stored,
                capsule_id,
            } => {
                assert_eq!(*query, 4);
                assert_eq!(*stored, 3);
                assert_eq!(capsule_id, "cap-1");
            }
            other => panic!("expected DimensionMismatch, got {other:?}"),
        }
        // The message names both dimensions.
        let msg = err.to_string();
        assert!(
            msg.contains('4') && msg.contains('3'),
            "names both dims: {msg}"
        );
    }

    /// A capsule the term lane misses but the vector lane finds still
    /// grounds — WITH the vector explain, matched_terms empty.
    #[test]
    fn vector_only_match_grounds_with_explain() {
        let mut store = seeded_store();
        // "beta gravity waves" (cap-2) does not contain the query term, but
        // its embedding is the query vector.
        store
            .put_embedding("cap-2", &[1.0, 0.0], "m", APPENDED)
            .unwrap();
        let q = query_vec(&["nonmatchingterm"], vec![1.0, 0.0]);
        let response = retrieve_core(&mut store, &q, NOW, Path::new(NO_ROOT)).unwrap();
        let RetrieveResponse::Grounded { results, .. } = &response else {
            panic!("expected grounded, got {response:?}");
        };
        assert_eq!(results.len(), 1);
        assert_eq!(results[0].id.as_str(), "cap-2");
        assert!(results[0].matched_terms.is_empty());
        assert_eq!(results[0].vector_similarity, Some(1.0));
        assert!(results[0].bm25.is_none(), "vector-only row omits bm25");
    }

    /// Both lanes empty (term matches nothing, no embedding stored) →
    /// abstain, with the fused note.
    #[test]
    fn both_lanes_empty_abstains() {
        let mut store = seeded_store();
        // No embeddings stored; the term matches nothing.
        let q = query_vec(&["zzznomatch"], vec![1.0, 0.0, 0.0]);
        let response = retrieve_core(&mut store, &q, NOW, Path::new(NO_ROOT)).unwrap();
        let RetrieveResponse::Abstain { reason, .. } = &response else {
            panic!("expected abstain, got {response:?}");
        };
        assert!(
            reason.contains("no stored embedding was available"),
            "got: {reason}"
        );
    }

    /// RED (invalid query_embedding): empty, non-finite, and zero-magnitude
    /// vectors are teaching errors.
    #[test]
    fn invalid_query_embedding_is_rejected() {
        let mut store = seeded_store();
        for bad in [vec![], vec![1.0, f32::NAN], vec![0.0, 0.0]] {
            let q = query_vec(&["alpha"], bad);
            assert!(matches!(
                retrieve_core(&mut store, &q, NOW, Path::new(NO_ROOT)),
                Err(RetrieveError::InvalidQueryEmbedding(_))
            ));
        }
    }

    /// `vector_k` caps the vector lane: with k = 0 the vector lane is empty
    /// and fusion degenerates to the term order (still fused_rank-stamped).
    #[test]
    fn vector_k_zero_empties_the_vector_lane() {
        let mut store = seeded_store();
        store
            .put_embedding("cap-2", &[1.0, 0.0, 0.0], "m", APPENDED)
            .unwrap();
        let q = RetrieveQuery {
            terms: vec!["alpha".to_string()],
            query_embedding: Some(vec![1.0, 0.0, 0.0]),
            vector_k: Some(0),
            ..RetrieveQuery::default()
        };
        let response = retrieve_core(&mut store, &q, NOW, Path::new(NO_ROOT)).unwrap();
        let RetrieveResponse::Grounded { results, .. } = &response else {
            panic!("expected grounded, got {response:?}");
        };
        // cap-2 (vector-only) is gone; cap-1 (term) grounds with no vector
        // explain.
        let ids: Vec<&str> = results.iter().map(|e| e.id.as_str()).collect();
        assert_eq!(ids, vec!["cap-1"]);
        assert!(results[0].vector_similarity.is_none());
    }

    fn vector_time_window_starvation_store() -> Store {
        let mut store = Store::open_in_memory().unwrap();
        store
            .append_with_event_time(
                &cap("vector-window higher cosine outside", "nott", 0.9, VF, None),
                &event_range(VF, VF),
                APPENDED,
            )
            .unwrap(); // cap-1
        store
            .append_with_event_time(
                &cap("vector-window lower cosine eligible", "nott", 0.9, VF, None),
                &event_range(NOW, NOW),
                APPENDED,
            )
            .unwrap(); // cap-2
        store
            .put_embedding("cap-1", &[1.0, 0.0], "event-test", APPENDED)
            .unwrap();
        store
            .put_embedding("cap-2", &[0.8, 0.6], "event-test", APPENDED)
            .unwrap();
        store
    }

    fn vector_time_window_starvation_query(lane: Lane) -> RetrieveQuery {
        RetrieveQuery {
            // Matches only the higher-cosine OUTSIDE row. The eligible row
            // can enter a fused response only through the vector lane, while
            // the shared outside row must be excluded exactly once.
            terms: vec!["higher cosine outside".to_string()],
            lane: Some(lane),
            query_embedding: Some(vec![1.0, 0.0]),
            vector_k: Some(1),
            time_window: Some(TimeWindow::new(Some(NOW), Some(NOW)).unwrap()),
            ..RetrieveQuery::default()
        }
    }

    fn assert_lower_cosine_window_survivor(response: &RetrieveResponse, lane: Lane) {
        let RetrieveResponse::Grounded {
            results, excluded, ..
        } = response
        else {
            panic!("{lane:?}: eligible lower-cosine row must ground, got {response:?}");
        };
        assert_eq!(results.len(), 1, "{lane:?}: vector_k=1 stays a hard cap");
        assert_eq!(results[0].id.as_str(), "cap-2");
        assert_eq!(results[0].vector_similarity, Some(0.8));
        assert!(
            results[0].matched_terms.is_empty(),
            "{lane:?}: result came from the vector lane, not a lexical escape"
        );
        assert_eq!(
            excluded,
            &BTreeMap::from([(ExclusionReason::OutsideTimeWindow, 1)]),
            "{lane:?}: the higher-cosine rejected row remains visible exactly once"
        );
    }

    #[test]
    fn forced_vector_time_window_filters_before_vector_k() {
        let mut store = vector_time_window_starvation_store();
        let response = retrieve(
            &mut store,
            &vector_time_window_starvation_query(Lane::Vector),
            NOW,
        )
        .unwrap();
        assert_lower_cosine_window_survivor(&response, Lane::Vector);
    }

    #[test]
    fn fused_time_window_filters_vector_lane_before_vector_k() {
        let mut store = vector_time_window_starvation_store();
        let response = retrieve(
            &mut store,
            &vector_time_window_starvation_query(Lane::Fused),
            NOW,
        )
        .unwrap();
        assert_lower_cosine_window_survivor(&response, Lane::Fused);
    }

    #[test]
    fn omitted_time_window_preserves_vector_k_bytes_and_reads_no_fact_time() {
        let build = |dated: bool| {
            let mut store = Store::open_in_memory().unwrap();
            for (content, event) in [
                ("vector-window omitted leader", VF),
                ("vector-window omitted runner-up", NOW),
            ] {
                let capsule = cap(content, "nott", 0.9, VF, None);
                if dated {
                    store
                        .append_with_event_time(&capsule, &event_range(event, event), APPENDED)
                        .unwrap();
                } else {
                    store.append(&capsule, APPENDED).unwrap();
                }
            }
            store
                .put_embedding("cap-1", &[1.0, 0.0], "event-test", APPENDED)
                .unwrap();
            store
                .put_embedding("cap-2", &[0.8, 0.6], "event-test", APPENDED)
                .unwrap();
            let mut contract = ContractStore::new(store);
            contract.reject_event_time_reads = true;
            contract
        };
        for lane in [Lane::Vector, Lane::Fused] {
            let query = RetrieveQuery {
                terms: vec!["qzx-no-lexical-match".to_string()],
                lane: Some(lane),
                query_embedding: Some(vec![1.0, 0.0]),
                vector_k: Some(1),
                ..RetrieveQuery::default()
            };
            let mut plain = build(false);
            let mut dated = build(true);
            let plain_response =
                retrieve_core(&mut plain, &query, NOW, Path::new(NO_ROOT)).unwrap();
            let dated_response =
                retrieve_core(&mut dated, &query, NOW, Path::new(NO_ROOT)).unwrap();
            assert_eq!(
                serde_json::to_vec(&plain_response).unwrap(),
                serde_json::to_vec(&dated_response).unwrap(),
                "{lane:?}: omitting time_window leaves historical vector bytes unchanged"
            );
            assert_eq!(grounded_ids(&dated_response), ["cap-1"]);
            assert_eq!(plain.event_time_reads.get(), 0);
            assert_eq!(dated.event_time_reads.get(), 0);
        }
    }

    fn event_range(from: OffsetDateTime, to: OffsetDateTime) -> EventTimeRange {
        EventTimeRange::new(from, to).unwrap()
    }

    #[test]
    fn time_window_grounds_only_intersecting_ranges_and_never_changes_decay() {
        let mut store = Store::open_in_memory().unwrap();
        store
            .append_with_event_time(
                &cap("chronoprobe intersects at boundary", "nott", 0.9, VF, None),
                &event_range(NOW - time::Duration::HOUR, NOW),
                APPENDED,
            )
            .unwrap(); // cap-1
        store
            .append_with_event_time(
                &cap("chronoprobe ends one tick early", "nott", 0.9, VF, None),
                &event_range(NOW - time::Duration::HOUR, NOW - time::Duration::NANOSECOND),
                APPENDED,
            )
            .unwrap(); // cap-2
        store
            .append(
                &cap("chronoprobe has no fact time", "nott", 0.9, VF, None),
                APPENDED,
            )
            .unwrap(); // cap-3

        let without = retrieve(&mut store, &query(&["chronoprobe"]), NOW).unwrap();
        let baseline_decay = match &without {
            RetrieveResponse::Grounded { results, .. } => {
                results
                    .iter()
                    .find(|result| result.id.as_str() == "cap-1")
                    .unwrap()
                    .decayed_weight
            }
            other => panic!("expected grounded baseline, got {other:?}"),
        };
        let response = retrieve(
            &mut store,
            &RetrieveQuery {
                terms: vec!["chronoprobe".to_string()],
                time_window: Some(TimeWindow::new(Some(NOW), Some(NOW)).unwrap()),
                ..RetrieveQuery::default()
            },
            NOW,
        )
        .unwrap();
        let RetrieveResponse::Grounded {
            results, excluded, ..
        } = &response
        else {
            panic!("expected grounded, got {response:?}");
        };
        assert_eq!(grounded_ids(&response), ["cap-1"]);
        assert_eq!(
            excluded,
            &BTreeMap::from([
                (ExclusionReason::OutsideTimeWindow, 1),
                (ExclusionReason::Undated, 1),
            ])
        );
        assert_eq!(
            results[0].decayed_weight, baseline_decay,
            "fact-time is a fence only, never a decay input"
        );
    }

    #[test]
    fn open_time_window_bounds_are_inclusive_to_one_tick() {
        let build = || {
            let mut store = Store::open_in_memory().unwrap();
            for (content, instant) in [
                ("openbound before", NOW - time::Duration::NANOSECOND),
                ("openbound equal", NOW),
                ("openbound after", NOW + time::Duration::NANOSECOND),
            ] {
                store
                    .append_with_event_time(
                        &cap(content, "nott", 0.9, VF, None),
                        &event_range(instant, instant),
                        APPENDED,
                    )
                    .unwrap();
            }
            store
        };

        let mut lower = build();
        let from_now = retrieve(
            &mut lower,
            &RetrieveQuery {
                terms: vec!["openbound".to_string()],
                time_window: Some(TimeWindow::new(Some(NOW), None).unwrap()),
                ..RetrieveQuery::default()
            },
            NOW,
        )
        .unwrap();
        let mut ids = grounded_ids(&from_now);
        ids.sort();
        assert_eq!(ids, ["cap-2", "cap-3"]);

        let mut upper = build();
        let to_now = retrieve(
            &mut upper,
            &RetrieveQuery {
                terms: vec!["openbound".to_string()],
                time_window: Some(TimeWindow::new(None, Some(NOW)).unwrap()),
                ..RetrieveQuery::default()
            },
            NOW,
        )
        .unwrap();
        let mut ids = grounded_ids(&to_now);
        ids.sort();
        assert_eq!(ids, ["cap-1", "cap-2"]);
    }

    #[test]
    fn time_window_is_last_fence_in_term_vector_and_fused_lanes() {
        for lane in [Lane::Term, Lane::Vector, Lane::Fused] {
            let mut store = Store::open_in_memory().unwrap();
            store
                .append_with_event_time(
                    &cap("dominanceprobe quarantined", "nott", 0.9, VF, None),
                    &event_range(VF, VF),
                    APPENDED,
                )
                .unwrap(); // cap-1
            store
                .append_with_event_time(
                    &cap("dominanceprobe falsified", "nott", 0.9, VF, None),
                    &event_range(VF, VF),
                    APPENDED,
                )
                .unwrap(); // cap-2
            store
                .append_with_event_time(
                    &cap("dominanceprobe archived", "nott", 0.9, VF, None),
                    &event_range(VF, VF),
                    APPENDED,
                )
                .unwrap(); // cap-3
            store
                .append_with_event_time(
                    &cap("dominanceprobe superseded", "nott", 0.9, VF, None),
                    &event_range(VF, VF),
                    APPENDED,
                )
                .unwrap(); // cap-4
            store
                .append_with_event_time(
                    &cap(
                        "dominanceprobe expired",
                        "nott",
                        0.9,
                        VF,
                        Some(NOW - time::Duration::SECOND),
                    ),
                    &event_range(VF, VF),
                    APPENDED,
                )
                .unwrap(); // cap-5
            store
                .append_with_event_time(
                    &cap(
                        "dominanceprobe not yet valid",
                        "nott",
                        0.9,
                        NOW + time::Duration::hours(2),
                        None,
                    ),
                    &event_range(VF, VF),
                    APPENDED,
                )
                .unwrap(); // cap-6
            store
                .append_with_event_time(
                    &cap("dominanceprobe outside", "nott", 0.9, VF, None),
                    &event_range(VF, VF),
                    APPENDED,
                )
                .unwrap(); // cap-7
            store
                .append(
                    &cap("dominanceprobe undated", "nott", 0.9, VF, None),
                    APPENDED,
                )
                .unwrap(); // cap-8
            store
                .append(&cap("dominance actor", "nott", 0.9, VF, None), APPENDED)
                .unwrap(); // cap-9; matches neither lane
            store
                .set_tier("cap-1", Tier::Quarantined, APPENDED)
                .unwrap();
            store
                .upsert_relation(RelationKind::Falsifies, "cap-9", "cap-2", APPENDED)
                .unwrap();
            store.set_tier("cap-3", Tier::Archived, APPENDED).unwrap();
            store.supersede("cap-4", "cap-9", APPENDED).unwrap();
            for id in [
                "cap-1", "cap-2", "cap-3", "cap-4", "cap-5", "cap-6", "cap-7", "cap-8",
            ] {
                store
                    .put_embedding(id, &[1.0, 0.0], "event-test", APPENDED)
                    .unwrap();
            }
            let vector =
                matches!(lane, Lane::Vector | Lane::Fused).then_some(vec![1.0_f32, 0.0_f32]);
            let response = retrieve(
                &mut store,
                &RetrieveQuery {
                    terms: vec!["dominanceprobe".to_string()],
                    lane: Some(lane),
                    query_embedding: vector,
                    time_window: Some(
                        TimeWindow::new(
                            Some(NOW + time::Duration::HOUR),
                            Some(NOW + time::Duration::HOUR),
                        )
                        .unwrap(),
                    ),
                    ..RetrieveQuery::default()
                },
                NOW,
            )
            .unwrap();
            let RetrieveResponse::MissingEvidence { excluded, .. } = response else {
                panic!("{lane:?}: expected missing_evidence, got {response:?}");
            };
            assert_eq!(
                excluded,
                BTreeMap::from([
                    (ExclusionReason::Quarantined, 1),
                    (ExclusionReason::Falsified, 1),
                    (ExclusionReason::Archived, 1),
                    (ExclusionReason::Superseded, 1),
                    (ExclusionReason::Expired, 1),
                    (ExclusionReason::NotYetValid, 1),
                    (ExclusionReason::OutsideTimeWindow, 1),
                    (ExclusionReason::Undated, 1),
                ]),
                "lane {lane:?} must apply the same dominance"
            );
        }
    }

    #[test]
    fn every_time_window_match_excluded_reports_both_fact_time_reasons() {
        let mut store = Store::open_in_memory().unwrap();
        store
            .append_with_event_time(
                &cap("factmiss dated", "nott", 0.9, VF, None),
                &event_range(VF, VF),
                APPENDED,
            )
            .unwrap();
        store
            .append(&cap("factmiss undated", "nott", 0.9, VF, None), APPENDED)
            .unwrap();
        let response = retrieve(
            &mut store,
            &RetrieveQuery {
                terms: vec!["factmiss".to_string()],
                time_window: Some(TimeWindow::new(Some(NOW), None).unwrap()),
                ..RetrieveQuery::default()
            },
            NOW,
        )
        .unwrap();
        let RetrieveResponse::MissingEvidence {
            excluded_count,
            excluded,
            reason,
            ..
        } = &response
        else {
            panic!("expected missing_evidence, got {response:?}");
        };
        assert_eq!(*excluded_count, 2);
        assert_eq!(
            excluded,
            &BTreeMap::from([
                (ExclusionReason::OutsideTimeWindow, 1),
                (ExclusionReason::Undated, 1),
            ])
        );
        assert!(reason.contains("outside_time_window") && reason.contains("undated"));
        let wire = serde_json::to_string(&response).unwrap();
        assert!(wire.contains(r#""outside_time_window":1"#));
        assert!(wire.contains(r#""undated":1"#));
    }

    #[test]
    fn absent_time_window_is_byte_identical_and_performs_zero_sidecar_reads() {
        let capsule = cap("inerttime same capsule", "nott", 0.9, VF, None);
        let mut plain_inner = Store::open_in_memory().unwrap();
        plain_inner.append(&capsule, APPENDED).unwrap();
        let mut dated_inner = Store::open_in_memory().unwrap();
        dated_inner
            .append_with_event_time(&capsule, &event_range(VF, NOW), APPENDED)
            .unwrap();
        let mut plain = ContractStore::new(plain_inner);
        let mut dated = ContractStore::new(dated_inner);
        plain.reject_event_time_reads = true;
        dated.reject_event_time_reads = true;

        let a = retrieve_core(&mut plain, &query(&["inerttime"]), NOW, Path::new(NO_ROOT)).unwrap();
        let b = retrieve_core(&mut dated, &query(&["inerttime"]), NOW, Path::new(NO_ROOT)).unwrap();
        assert_eq!(
            serde_json::to_vec(&a).unwrap(),
            serde_json::to_vec(&b).unwrap()
        );
        assert_eq!(plain.event_time_reads.get(), 0);
        assert_eq!(dated.event_time_reads.get(), 0);
    }

    #[test]
    fn tombstone_id_probe_never_claims_fact_time_membership() {
        let mut inner = Store::open_in_memory().unwrap();
        inner
            .append_with_event_time(
                &cap("forgotten timed content", "nott", 0.9, VF, None),
                &event_range(VF, NOW),
                APPENDED,
            )
            .unwrap();
        inner
            .forget_capsule(
                "cap-1",
                TombstoneMode::Purged,
                "event tombstone test",
                b"key",
                APPENDED,
            )
            .unwrap();
        let mut store = ContractStore::new(inner);
        store.reject_event_time_reads = true;
        let response = retrieve_core(
            &mut store,
            &RetrieveQuery {
                terms: vec!["cap-1".to_string()],
                time_window: Some(TimeWindow::new(Some(NOW), None).unwrap()),
                ..RetrieveQuery::default()
            },
            NOW,
            Path::new(NO_ROOT),
        )
        .unwrap();
        let RetrieveResponse::MissingEvidence { excluded, .. } = response else {
            panic!("expected tombstone missing_evidence, got {response:?}");
        };
        assert_eq!(excluded, BTreeMap::from([(ExclusionReason::Tombstoned, 1)]));
        assert_eq!(store.event_time_reads.get(), 0);
    }

    #[test]
    fn session_label_and_project_time_fences_compose_in_every_lane_before_top_k() {
        for lane in [Lane::Term, Lane::Vector, Lane::Fused] {
            let mut store = Store::open_in_memory().unwrap();
            store.open_session("sess-selected", APPENDED).unwrap();
            store.open_session("sess-other", APPENDED).unwrap();
            store
                .append_with_session_and_event_time(
                    &cap("sessioncompose selected", "nott/sub", 0.9, VF, None),
                    "sess-selected",
                    &event_range(NOW, NOW),
                    APPENDED,
                )
                .unwrap(); // cap-1
            store
                .append_with_session_and_event_time(
                    &cap("sessioncompose wrong project", "other", 0.9, VF, None),
                    "sess-selected",
                    &event_range(NOW, NOW),
                    APPENDED,
                )
                .unwrap(); // cap-2
            store
                .append_with_session_and_event_time(
                    &cap("sessioncompose outside time", "nott/sub", 0.9, VF, None),
                    "sess-selected",
                    &event_range(VF, VF),
                    APPENDED,
                )
                .unwrap(); // cap-3
            store
                .append_with_session_and_event_time(
                    &cap("sessioncompose wrong session", "nott/sub", 0.9, VF, None),
                    "sess-other",
                    &event_range(NOW, NOW),
                    APPENDED,
                )
                .unwrap(); // cap-4
            // The selected row deliberately has the weakest cosine. The
            // other-session/project rows must be SQL-filtered before decode
            // and the outside-time row must not consume vector_k=1.
            for (id, vector) in [
                ("cap-1", [0.6, 0.8]),
                ("cap-2", [1.0, 0.0]),
                ("cap-3", [1.0, 0.0]),
                ("cap-4", [1.0, 0.0]),
            ] {
                store
                    .put_embedding(id, &vector, "session-test", APPENDED)
                    .unwrap();
            }
            let response = retrieve(
                &mut store,
                &RetrieveQuery {
                    terms: vec!["sessioncompose".to_string()],
                    project_id: Some("nott/sub".to_string()),
                    project_prefix: Some("nott".to_string()),
                    session_id: Some("sess-selected".to_string()),
                    time_window: Some(TimeWindow::new(Some(NOW), Some(NOW)).unwrap()),
                    lane: Some(lane),
                    query_embedding: matches!(lane, Lane::Vector | Lane::Fused)
                        .then_some(vec![1.0, 0.0]),
                    vector_k: Some(1),
                    ..RetrieveQuery::default()
                },
                NOW,
            )
            .unwrap();
            let RetrieveResponse::Grounded {
                results, excluded, ..
            } = response
            else {
                panic!("{lane:?}: expected grounded, got {response:?}");
            };
            let ids: Vec<&str> = results.iter().map(|row| row.id.as_str()).collect();
            assert_eq!(ids, ["cap-1"], "lane {lane:?}");
            assert_eq!(
                excluded,
                BTreeMap::from([(ExclusionReason::OutsideTimeWindow, 1)]),
                "wrong-project/session rows are non-matches, never exclusions ({lane:?})"
            );
        }
    }

    #[test]
    fn session_label_tombstone_probe_is_private_and_absence_is_legacy_global() {
        let mut store = Store::open_in_memory().unwrap();
        store.open_session("sess-a", APPENDED).unwrap();
        store.open_session("sess-b", APPENDED).unwrap();
        store
            .append_with_session(
                &cap("session-private forgotten", "nott", 0.9, VF, None),
                "sess-a",
                APPENDED,
            )
            .unwrap();
        store
            .forget_capsule(
                "cap-1",
                TombstoneMode::Purged,
                "session privacy",
                b"key",
                APPENDED,
            )
            .unwrap();

        for (session_id, tombstoned) in [
            (Some("sess-a"), true),
            (Some("sess-b"), false),
            (None, true),
        ] {
            let response = retrieve(
                &mut store,
                &RetrieveQuery {
                    terms: vec!["cap-1".to_string()],
                    session_id: session_id.map(str::to_string),
                    ..RetrieveQuery::default()
                },
                NOW,
            )
            .unwrap();
            if tombstoned {
                let RetrieveResponse::MissingEvidence { excluded, .. } = response else {
                    panic!("{session_id:?}: expected missing_evidence, got {response:?}");
                };
                assert_eq!(excluded, BTreeMap::from([(ExclusionReason::Tombstoned, 1)]));
            } else {
                let RetrieveResponse::Abstain { reason, .. } = response else {
                    panic!("{session_id:?}: expected abstain, got {response:?}");
                };
                assert!(reason.contains("store-local capsule label 'sess-b'"));
                assert!(!reason.contains("unknown session"));
            }
        }
    }

    #[test]
    fn grounded_receipt_records_the_exact_supplied_session_label() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("receipt.sqlite3");
        let exact = " Sess-\u{00e9}\0 ";
        let mut store = Store::open(&path).unwrap();
        store.open_session(exact, APPENDED).unwrap();
        store
            .append_with_session(
                &cap("receipt exact label", "nott", 0.9, VF, None),
                exact,
                APPENDED,
            )
            .unwrap();
        store.finish_session(exact, None, APPENDED).unwrap();
        let response = retrieve(
            &mut store,
            &RetrieveQuery {
                terms: vec!["receipt exact".to_string()],
                session_id: Some(exact.to_string()),
                ..RetrieveQuery::default()
            },
            NOW,
        )
        .unwrap();
        assert_eq!(grounded_ids(&response), ["cap-1"]);
        drop(store);

        let conn = rusqlite::Connection::open(&path).unwrap();
        let recorded: Option<String> = conn
            .query_row(
                "SELECT session_id FROM recall_receipts WHERE id = 'rcpt-1'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(recorded.as_deref(), Some(exact));
    }

    #[test]
    fn finished_orphaned_and_colliding_merged_session_labels_remain_recallable() {
        let dir = tempfile::tempdir().unwrap();
        let local_path = dir.path().join("local.sqlite3");
        let incoming_path = dir.path().join("incoming.sqlite3");
        {
            let mut local = Store::open(&local_path).unwrap();
            local.open_session("sess-1", APPENDED).unwrap();
            local
                .append_with_session(
                    &cap("merge label local", "nott", 0.9, VF, None),
                    "sess-1",
                    APPENDED,
                )
                .unwrap();
            local.finish_session("sess-1", None, APPENDED).unwrap();
        }
        {
            let mut incoming = Store::open(&incoming_path).unwrap();
            incoming.open_session("sess-1", APPENDED).unwrap();
            incoming.open_session("import-only", APPENDED).unwrap();
            incoming
                .append_with_session(
                    &cap("merge label incoming", "nott", 0.9, VF, None),
                    "sess-1",
                    APPENDED,
                )
                .unwrap();
            incoming
                .append_with_session(
                    &cap("merge label orphan", "nott", 0.9, VF, None),
                    "import-only",
                    APPENDED,
                )
                .unwrap();
        }

        let mut local = Store::open(&local_path).unwrap();
        local.merge_from(&incoming_path, b"local-key").unwrap();
        assert!(
            local
                .get_session("sess-1")
                .unwrap()
                .unwrap()
                .finished_at
                .is_some(),
            "the finished local bracket stays closed"
        );
        assert!(
            local.get_session("import-only").unwrap().is_none(),
            "merge preserves capsule labels but does not import session rows"
        );
        let collision = retrieve(
            &mut local,
            &RetrieveQuery {
                terms: vec!["merge label".to_string()],
                session_id: Some("sess-1".to_string()),
                ..RetrieveQuery::default()
            },
            NOW + time::Duration::days(365),
        )
        .unwrap();
        assert_eq!(grounded_ids(&collision), ["cap-1", "cap-2"]);
        let orphan = retrieve(
            &mut local,
            &RetrieveQuery {
                terms: vec!["merge label".to_string()],
                session_id: Some("import-only".to_string()),
                ..RetrieveQuery::default()
            },
            NOW + time::Duration::days(365),
        )
        .unwrap();
        assert_eq!(grounded_ids(&orphan), ["cap-3"]);
    }

    #[test]
    fn absent_session_label_is_byte_identical_across_term_vector_and_fused() {
        for lane in [Lane::Term, Lane::Vector, Lane::Fused] {
            let build = |labeled: bool| {
                let mut store = Store::open_in_memory().unwrap();
                if labeled {
                    store.open_session("sess-dormant", APPENDED).unwrap();
                    store
                        .append_with_session(
                            &cap("dormant label alpha", "nott", 0.9, VF, None),
                            "sess-dormant",
                            APPENDED,
                        )
                        .unwrap();
                    store
                        .append_with_session(
                            &cap("dormant label beta", "nott", 0.8, VF, None),
                            "sess-dormant",
                            APPENDED,
                        )
                        .unwrap();
                } else {
                    store
                        .append(&cap("dormant label alpha", "nott", 0.9, VF, None), APPENDED)
                        .unwrap();
                    store
                        .append(&cap("dormant label beta", "nott", 0.8, VF, None), APPENDED)
                        .unwrap();
                }
                store
                    .put_embedding("cap-1", &[1.0, 0.0], "dormant-test", APPENDED)
                    .unwrap();
                store
                    .put_embedding("cap-2", &[0.5, 0.5], "dormant-test", APPENDED)
                    .unwrap();
                store
            };
            let query = RetrieveQuery {
                terms: vec!["dormant label".to_string()],
                lane: Some(lane),
                query_embedding: matches!(lane, Lane::Vector | Lane::Fused)
                    .then_some(vec![1.0, 0.0]),
                session_id: None,
                ..RetrieveQuery::default()
            };
            let mut unbracketed = build(false);
            let mut labeled = build(true);
            let a = retrieve_core(
                &mut unbracketed,
                &query,
                NOW,
                Path::new("/nmemory-hermetic-test-anchor-root"),
            )
            .unwrap();
            let b = retrieve_core(
                &mut labeled,
                &query,
                NOW,
                Path::new("/nmemory-hermetic-test-anchor-root"),
            )
            .unwrap();
            assert_eq!(
                serde_json::to_vec(&a).unwrap(),
                serde_json::to_vec(&b).unwrap(),
                "session_id: None must be structurally dormant in {lane:?} recall"
            );
        }
    }

    // --- S3 effort-lifecycle: effort_id scoped retrieve ------------------

    /// Build a two-effort world sharing a term across BOTH efforts' members,
    /// so a fence that leaks would surface the other effort's row. Returns
    /// the store; ids: cap-1 epic A, cap-2 member A, cap-3 epic B, cap-4
    /// member B — every content carries "token".
    fn two_effort_world() -> Store {
        let mut store = Store::open_in_memory().unwrap();
        store
            .append(
                &cap("token rollout epic alpha", "nott", 0.9, VF, None),
                APPENDED,
            )
            .unwrap(); // cap-1
        store
            .append(
                &cap("token detail member alpha", "nott", 0.9, VF, None),
                APPENDED,
            )
            .unwrap(); // cap-2
        store
            .append(
                &cap("token rollout epic beta", "nott", 0.9, VF, None),
                APPENDED,
            )
            .unwrap(); // cap-3
        store
            .append(
                &cap("token detail member beta", "nott", 0.9, VF, None),
                APPENDED,
            )
            .unwrap(); // cap-4
        store
            .set_classification("cap-1", "epic", "project", NOW)
            .unwrap();
        store
            .set_classification("cap-3", "epic", "project", NOW)
            .unwrap();
        store
            .upsert_relation(RelationKind::PartOf, "cap-2", "cap-1", APPENDED)
            .unwrap();
        store
            .upsert_relation(RelationKind::PartOf, "cap-4", "cap-3", APPENDED)
            .unwrap();
        store
    }

    /// Effort A's scope: epic cap-1, member cap-2, open.
    fn effort_a() -> EffortScope {
        EffortScope {
            epic_id: "cap-1".to_string(),
            member_ids: vec!["cap-2".to_string()],
            open: true,
        }
    }

    #[test]
    fn effort_fence_grounds_only_its_members_in_the_term_lane() {
        let mut store = two_effort_world();
        let mut q = query(&["token"]);
        q.effort = Some(effort_a());
        let response = retrieve(&mut store, &q, NOW).unwrap();
        let RetrieveResponse::Grounded { results, .. } = &response else {
            panic!("effort A must ground its own rows, got {response:?}");
        };
        let ids: Vec<&str> = results.iter().map(|e| e.id.as_str()).collect();
        assert!(ids.contains(&"cap-1"), "the epic grounds: {ids:?}");
        assert!(ids.contains(&"cap-2"), "the member grounds: {ids:?}");
        // Leakage rejection: another effort's rows carry the IDENTICAL term
        // yet must never ground under this fence.
        assert!(
            !ids.contains(&"cap-3") && !ids.contains(&"cap-4"),
            "effort B rows leaked past the term-lane fence: {ids:?}"
        );
    }

    #[test]
    fn effort_fence_grounds_only_its_members_in_the_vector_lane() {
        let mut store = two_effort_world();
        // IDENTICAL embedding on both efforts' members: only the fence can
        // separate them in the vector lane.
        store
            .put_embedding("cap-2", &[1.0, 0.0], "m", APPENDED)
            .unwrap();
        store
            .put_embedding("cap-4", &[1.0, 0.0], "m", APPENDED)
            .unwrap();
        let mut q = RetrieveQuery {
            terms: vec!["token".to_string()],
            lane: Some(Lane::Vector),
            query_embedding: Some(vec![1.0, 0.0]),
            ..RetrieveQuery::default()
        };
        q.effort = Some(effort_a());
        let response = retrieve(&mut store, &q, NOW).unwrap();
        let RetrieveResponse::Grounded { results, .. } = &response else {
            panic!("vector lane must ground effort A's member, got {response:?}");
        };
        let ids: Vec<&str> = results.iter().map(|e| e.id.as_str()).collect();
        assert!(
            ids.contains(&"cap-2"),
            "effort A's embedded member grounds: {ids:?}"
        );
        assert!(
            !ids.contains(&"cap-4"),
            "effort B's identically-embedded member leaked past the vector fence: {ids:?}"
        );
    }

    #[test]
    fn effort_absent_is_byte_identical_dormant() {
        // Byte-golden dormancy (NOT a substring probe): a query WITHOUT an
        // effort scope must be byte-for-byte identical to the pre-S3 engine —
        // i.e. to the SAME four capsules in a store that never learned the
        // effort machinery (no epic classification, no part_of edges). A
        // `contains("effort")` probe both false-passes on a null field and
        // false-FAILS on content bearing the token; full-wire identity is neither.
        let mut with_efforts = two_effort_world();
        let mut pre_s3 = Store::open_in_memory().unwrap();
        for content in [
            "token rollout epic alpha",
            "token detail member alpha",
            "token rollout epic beta",
            "token detail member beta",
        ] {
            pre_s3
                .append(&cap(content, "nott", 0.9, VF, None), APPENDED)
                .unwrap();
        }
        let dormant =
            serde_json::to_string(&retrieve(&mut with_efforts, &query(&["token"]), NOW).unwrap())
                .unwrap();
        let baseline =
            serde_json::to_string(&retrieve(&mut pre_s3, &query(&["token"]), NOW).unwrap())
                .unwrap();
        assert_eq!(
            dormant, baseline,
            "an effort-free recall is byte-identical to the pre-S3 engine: {dormant}"
        );
    }

    #[test]
    fn effort_grounded_echoes_scope_and_stamps_roles() {
        let mut store = two_effort_world();
        let mut q = query(&["token"]);
        q.effort = Some(effort_a());
        let response = retrieve(&mut store, &q, NOW).unwrap();
        let value = serde_json::to_value(&response).unwrap();
        assert_eq!(value["effort"]["epic_id"], "cap-1");
        assert_eq!(value["effort"]["member_total"], 1);
        assert_eq!(value["effort"]["open"], true);
        // Per-row effort_role: the epic is "epic", the member is "member".
        let by_id = |id: &str| {
            value["results"]
                .as_array()
                .unwrap()
                .iter()
                .find(|r| r["id"] == id)
                .cloned()
                .unwrap()
        };
        assert_eq!(by_id("cap-1")["effort_role"], "epic");
        assert_eq!(by_id("cap-2")["effort_role"], "member");
    }

    #[test]
    fn closed_effort_is_queryable_with_open_false() {
        let mut store = two_effort_world();
        // A witnessed (closed) epic still grounds — only tombstoned refuses
        // (that rejection lives at the server resolution boundary).
        let mut q = query(&["token"]);
        q.effort = Some(EffortScope {
            epic_id: "cap-1".to_string(),
            member_ids: vec!["cap-2".to_string()],
            open: false,
        });
        let response = retrieve(&mut store, &q, NOW).unwrap();
        let value = serde_json::to_value(&response).unwrap();
        assert_eq!(value["outcome"], "grounded", "closed effort still grounds");
        assert_eq!(value["effort"]["open"], false, "the echo names it closed");
    }

    #[test]
    fn fenced_dead_member_surfaces_in_excluded_never_collapses_to_abstain() {
        let mut store = Store::open_in_memory().unwrap();
        store
            .append(
                &cap("alpha effort container headline", "nott", 0.9, VF, None),
                APPENDED,
            )
            .unwrap(); // cap-1 epic
        store
            .append(&cap("widget live member", "nott", 0.9, VF, None), APPENDED)
            .unwrap(); // cap-2 live member
        store
            .append(&cap("widget stale member", "nott", 0.9, VF, None), APPENDED)
            .unwrap(); // cap-3 superseded member
        store
            .append(
                &cap("widget successor outside", "nott", 0.9, VF, None),
                APPENDED,
            )
            .unwrap(); // cap-4 successor (NOT in the effort)
        store
            .set_classification("cap-1", "epic", "project", NOW)
            .unwrap();
        store
            .upsert_relation(RelationKind::PartOf, "cap-2", "cap-1", APPENDED)
            .unwrap();
        store
            .upsert_relation(RelationKind::PartOf, "cap-3", "cap-1", APPENDED)
            .unwrap();
        store.supersede("cap-3", "cap-4", APPENDED).unwrap();

        let mut q = query(&["widget"]);
        q.effort = Some(EffortScope {
            epic_id: "cap-1".to_string(),
            member_ids: vec!["cap-2".to_string(), "cap-3".to_string()],
            open: true,
        });
        let response = retrieve(&mut store, &q, NOW).unwrap();
        let RetrieveResponse::Grounded {
            results, excluded, ..
        } = &response
        else {
            panic!("the live member must ground; the dead one must not abstain: {response:?}");
        };
        let ids: Vec<&str> = results.iter().map(|e| e.id.as_str()).collect();
        assert_eq!(ids, vec!["cap-2"], "only the live fenced member grounds");
        assert_eq!(
            excluded.get(&ExclusionReason::Superseded).copied(),
            Some(1),
            "the fenced-in dead member surfaces under excluded, not abstain: {excluded:?}"
        );
        // The successor is outside the fence — it never even reaches
        // eligibility (scope, not exclusion).
        assert!(!ids.contains(&"cap-4"));
    }

    #[test]
    fn fenced_zero_match_abstains_and_names_the_effort_fence() {
        let mut store = Store::open_in_memory().unwrap();
        store
            .append(
                &cap("effort epic headline", "nott", 0.9, VF, None),
                APPENDED,
            )
            .unwrap(); // cap-1
        store
            .append(
                &cap("banana member content", "nott", 0.9, VF, None),
                APPENDED,
            )
            .unwrap(); // cap-2
        store
            .set_classification("cap-1", "epic", "project", NOW)
            .unwrap();
        store
            .upsert_relation(RelationKind::PartOf, "cap-2", "cap-1", APPENDED)
            .unwrap();

        let mut q = query(&["kiwi"]);
        q.effort = Some(EffortScope {
            epic_id: "cap-1".to_string(),
            member_ids: vec!["cap-2".to_string()],
            open: true,
        });
        let response = retrieve(&mut store, &q, NOW).unwrap();
        // A fenced zero-match ABSTAINS — never floored to a fabricated row.
        let RetrieveResponse::Abstain { reason, .. } = &response else {
            panic!("a fenced zero-match must abstain, never floor: {response:?}");
        };
        assert!(
            reason.contains("within effort 'cap-1' (1 members)"),
            "the honest-empty text names the effort fence: {reason}"
        );
    }

    #[test]
    fn effort_fence_label_composes_with_the_project_fence() {
        let mut store = Store::open_in_memory().unwrap();
        store
            .append(
                &cap("effort epic headline", "nott", 0.9, VF, None),
                APPENDED,
            )
            .unwrap(); // cap-1
        store
            .append(
                &cap("banana member content", "nott", 0.9, VF, None),
                APPENDED,
            )
            .unwrap(); // cap-2
        store
            .set_classification("cap-1", "epic", "project", NOW)
            .unwrap();
        store
            .upsert_relation(RelationKind::PartOf, "cap-2", "cap-1", APPENDED)
            .unwrap();
        let q = RetrieveQuery {
            terms: vec!["kiwi".to_string()],
            project_id: Some("nott".to_string()),
            effort: Some(EffortScope {
                epic_id: "cap-1".to_string(),
                member_ids: vec!["cap-2".to_string()],
                open: true,
            }),
            ..RetrieveQuery::default()
        };
        let response = retrieve(&mut store, &q, NOW).unwrap();
        let RetrieveResponse::Abstain { reason, .. } = &response else {
            panic!("expected abstain, got {response:?}");
        };
        assert!(
            reason.contains("within effort 'cap-1' (1 members) and project 'nott'"),
            "the effort clause LEADS the composed fence label: {reason}"
        );
    }

    #[test]
    fn effort_fence_scales_past_the_sql_variable_limit() {
        // 1050 members > SQLite's 999-variable ceiling: the json_each fence
        // is ONE bound parameter, so this must ground without a variable-limit
        // error and without a full scan of the 1050 rows (the FTS driver
        // narrows first).
        let mut store = Store::open_in_memory().unwrap();
        store
            .append(&cap("scale effort epic", "nott", 0.9, VF, None), APPENDED)
            .unwrap(); // cap-1
        let mut member_ids = Vec::new();
        for i in 0..1050 {
            store
                .append(
                    &cap(&format!("scale member number {i}"), "nott", 0.9, VF, None),
                    APPENDED,
                )
                .unwrap();
            member_ids.push(format!("cap-{}", i + 2));
        }
        store
            .set_classification("cap-1", "epic", "project", NOW)
            .unwrap();
        for m in &member_ids {
            store
                .upsert_relation(RelationKind::PartOf, m, "cap-1", APPENDED)
                .unwrap();
        }
        // Non-members that ALSO carry "scale" — a FULL SCAN of the term would
        // ground these too. They are NOT part_of the effort, so the id-set
        // fence must exclude them: the proof that the fence NARROWS the
        // candidate set rather than scanning every "scale" row is that
        // `matched` counts ONLY the in-fence rows (1050 members + the epic),
        // never these outsiders.
        for i in 0..8 {
            store
                .append(
                    &cap(&format!("scale outsider number {i}"), "nott", 0.9, VF, None),
                    APPENDED,
                )
                .unwrap();
        }
        let q = RetrieveQuery {
            terms: vec!["scale".to_string()],
            effort: Some(EffortScope {
                epic_id: "cap-1".to_string(),
                member_ids,
                open: true,
            }),
            limit: Some(5),
            ..RetrieveQuery::default()
        };
        let response = retrieve(&mut store, &q, NOW).unwrap();
        let RetrieveResponse::Grounded {
            matched, effort, ..
        } = &response
        else {
            panic!("a 1050-member fence must ground, not error: {response:?}");
        };
        // Exactly the 1050 members + the epic are eligible in-fence "scale"
        // matches; the 8 outside "scale" rows are fence-excluded, never
        // full-scanned. A full scan would report 1059 here.
        assert_eq!(
            *matched, 1051,
            "the id-set fence narrows to members ∪ {{epic}}; outsiders never count: {matched}"
        );
        assert_eq!(effort.as_ref().unwrap().member_total, 1050);
    }

    #[test]
    fn effort_scoped_recall_is_deterministic() {
        let run = || {
            let mut store = two_effort_world();
            let mut q = query(&["token"]);
            q.effort = Some(effort_a());
            serde_json::to_vec(&retrieve(&mut store, &q, NOW).unwrap()).unwrap()
        };
        assert_eq!(run(), run(), "seq/id only — no clock, no nondeterminism");
    }

    // --- topic-anchor s1: topic_id scoped retrieve ------------------------

    /// A CROSS-PROJECT topic world — the whole point of the topic anchor.
    /// cap-1 is a member in project A, cap-2 a member in project B, cap-3 the
    /// topic node in project C, and cap-4 an off-topic row in project A that
    /// carries the SAME term, so a fence that leaks would surface it.
    fn cross_project_topic_world() -> Store {
        let mut store = Store::open_in_memory().unwrap();
        store
            .append(
                &cap("token note from alpha", "proj-a", 0.9, VF, None),
                APPENDED,
            )
            .unwrap(); // cap-1
        store
            .append(
                &cap("token note from beta", "proj-b", 0.9, VF, None),
                APPENDED,
            )
            .unwrap(); // cap-2
        store
            .append(
                &cap("token the topic node", "proj-c", 0.9, VF, None),
                APPENDED,
            )
            .unwrap(); // cap-3
        store
            .append(
                &cap("token unrelated in alpha", "proj-a", 0.9, VF, None),
                APPENDED,
            )
            .unwrap(); // cap-4
        store
            .upsert_relation(RelationKind::About, "cap-1", "cap-3", APPENDED)
            .unwrap();
        store
            .upsert_relation(RelationKind::About, "cap-2", "cap-3", APPENDED)
            .unwrap();
        store
    }

    /// The topic scope for [`cross_project_topic_world`]: node cap-3, members
    /// cap-1 (project A) and cap-2 (project B).
    fn topic_c() -> TopicScope {
        TopicScope {
            topic_id: "cap-3".to_string(),
            member_ids: vec!["cap-1".to_string(), "cap-2".to_string()],
        }
    }

    /// T1 — the fence crosses project boundaries and stamps each role.
    #[test]
    fn topic_fence_unions_members_across_projects_and_stamps_roles() {
        let mut store = cross_project_topic_world();
        let mut q = query(&["token"]);
        q.topic = Some(topic_c());
        let response = retrieve(&mut store, &q, NOW).unwrap();
        let value = serde_json::to_value(&response).unwrap();
        assert_eq!(value["outcome"], "grounded", "{value}");
        let roles: BTreeMap<String, String> = value["results"]
            .as_array()
            .unwrap()
            .iter()
            .map(|r| {
                (
                    r["id"].as_str().unwrap().to_string(),
                    r["topic_role"].as_str().unwrap().to_string(),
                )
            })
            .collect();
        // All three ground with NO project fence — two projects, one topic.
        assert_eq!(roles.get("cap-1").map(String::as_str), Some("member"));
        assert_eq!(roles.get("cap-2").map(String::as_str), Some("member"));
        assert_eq!(roles.get("cap-3").map(String::as_str), Some("topic"));
        // The off-topic row carries the SAME term and must never leak in.
        assert!(
            !roles.contains_key("cap-4"),
            "a non-member with the same term leaked past the fence: {roles:?}"
        );
        assert_eq!(value["topic"]["topic_id"], "cap-3");
        assert_eq!(value["topic"]["member_total"], 2);
        assert!(
            value["topic"].get("open").is_none(),
            "a topic has no lifecycle, so the echo carries no open flag: {}",
            value["topic"]
        );
    }

    /// T2 — the topic fence AND-composes with the project fence: only the
    /// member that is ALSO in project A grounds. The other member is outside
    /// the composed scope entirely (scope, not exclusion), so it is absent
    /// from `excluded` — exactly the effort fence's semantics.
    #[test]
    fn topic_fence_and_composes_with_the_project_fence() {
        let mut store = cross_project_topic_world();
        let q = RetrieveQuery {
            terms: vec!["token".to_string()],
            project_id: Some("proj-a".to_string()),
            topic: Some(topic_c()),
            ..RetrieveQuery::default()
        };
        let response = retrieve(&mut store, &q, NOW).unwrap();
        let RetrieveResponse::Grounded {
            results, excluded, ..
        } = &response
        else {
            panic!("project A's member must ground: {response:?}");
        };
        let ids: Vec<&str> = results.iter().map(|e| e.id.as_str()).collect();
        assert_eq!(
            ids,
            vec!["cap-1"],
            "only the member inside BOTH fences grounds"
        );
        assert!(
            excluded.is_empty(),
            "out-of-scope rows are fenced, never excluded: {excluded:?}"
        );
    }

    /// The vector lane obeys the same fence: two identically-embedded rows,
    /// only the topic member survives.
    #[test]
    fn topic_fence_holds_in_the_vector_lane() {
        let mut store = cross_project_topic_world();
        store
            .put_embedding("cap-1", &[1.0, 0.0], "m", APPENDED)
            .unwrap();
        store
            .put_embedding("cap-4", &[1.0, 0.0], "m", APPENDED)
            .unwrap();
        let q = RetrieveQuery {
            terms: vec!["token".to_string()],
            lane: Some(Lane::Vector),
            query_embedding: Some(vec![1.0, 0.0]),
            topic: Some(topic_c()),
            ..RetrieveQuery::default()
        };
        let response = retrieve(&mut store, &q, NOW).unwrap();
        let RetrieveResponse::Grounded { results, .. } = &response else {
            panic!("the vector lane must ground the topic member: {response:?}");
        };
        let ids: Vec<&str> = results.iter().map(|e| e.id.as_str()).collect();
        assert!(
            ids.contains(&"cap-1"),
            "the embedded member grounds: {ids:?}"
        );
        assert!(
            !ids.contains(&"cap-4"),
            "an identically-embedded non-member leaked past the vector fence: {ids:?}"
        );
    }

    /// T4 at the engine seam — byte-golden dormancy: a query WITHOUT a topic
    /// scope must be byte-for-byte identical to the pre-topic engine, i.e. to
    /// the SAME four capsules in a store that never learned the topic
    /// machinery (no `about` edges). A `contains("topic")` probe both
    /// false-passes on a null field and false-FAILS on content bearing the
    /// token; full-wire identity is neither.
    ///
    /// Two-store identity ALONE is not enough, and the difference matters: a
    /// field that serializes to `null` in BOTH stores is identical in both and
    /// slips through, so dropping a `skip_serializing_if` would still pass.
    /// The STRUCTURAL half below closes that hole by asserting the new keys are
    /// ABSENT from the parsed object — key lookup, never a substring probe, so
    /// content bearing the word "topic" cannot false-fail it.
    #[test]
    fn topic_absent_is_byte_identical_dormant() {
        let mut with_topics = cross_project_topic_world();
        let mut pre_topic = Store::open_in_memory().unwrap();
        for (content, project) in [
            ("token note from alpha", "proj-a"),
            ("token note from beta", "proj-b"),
            ("token the topic node", "proj-c"),
            ("token unrelated in alpha", "proj-a"),
        ] {
            pre_topic
                .append(&cap(content, project, 0.9, VF, None), APPENDED)
                .unwrap();
        }
        let dormant =
            serde_json::to_string(&retrieve(&mut with_topics, &query(&["token"]), NOW).unwrap())
                .unwrap();
        let baseline =
            serde_json::to_string(&retrieve(&mut pre_topic, &query(&["token"]), NOW).unwrap())
                .unwrap();
        assert_eq!(
            dormant, baseline,
            "a topic-free recall is byte-identical to the pre-topic engine: {dormant}"
        );
        // Structural half: the two new keys must be ABSENT, not merely null.
        let value: serde_json::Value = serde_json::from_str(&dormant).unwrap();
        assert!(
            value.get("topic").is_none(),
            "the topic echo key must be absent on a dormant recall: {dormant}"
        );
        for row in value["results"].as_array().unwrap() {
            assert!(
                row.get("topic_role").is_none(),
                "topic_role must be absent on a dormant envelope: {row}"
            );
        }
    }

    /// T5 — a fenced-in DEAD member surfaces under `excluded`, never collapsing
    /// the outcome to abstain. Scope is not eligibility.
    #[test]
    fn fenced_dead_topic_member_surfaces_in_excluded() {
        let mut store = Store::open_in_memory().unwrap();
        store
            .append(&cap("topic node headline", "nott", 0.9, VF, None), APPENDED)
            .unwrap(); // cap-1 topic
        store
            .append(&cap("widget live member", "nott", 0.9, VF, None), APPENDED)
            .unwrap(); // cap-2 live member
        store
            .append(&cap("widget stale member", "nott", 0.9, VF, None), APPENDED)
            .unwrap(); // cap-3 superseded member
        store
            .append(
                &cap("widget successor outside", "nott", 0.9, VF, None),
                APPENDED,
            )
            .unwrap(); // cap-4 successor, NOT about the topic
        store
            .upsert_relation(RelationKind::About, "cap-2", "cap-1", APPENDED)
            .unwrap();
        store
            .upsert_relation(RelationKind::About, "cap-3", "cap-1", APPENDED)
            .unwrap();
        store.supersede("cap-3", "cap-4", APPENDED).unwrap();

        let mut q = query(&["widget"]);
        q.topic = Some(TopicScope {
            topic_id: "cap-1".to_string(),
            member_ids: vec!["cap-2".to_string(), "cap-3".to_string()],
        });
        let response = retrieve(&mut store, &q, NOW).unwrap();
        let RetrieveResponse::Grounded {
            results, excluded, ..
        } = &response
        else {
            panic!("the live member must ground; the dead one must not abstain: {response:?}");
        };
        let ids: Vec<&str> = results.iter().map(|e| e.id.as_str()).collect();
        assert_eq!(ids, vec!["cap-2"], "only the live fenced member grounds");
        assert_eq!(
            excluded.get(&ExclusionReason::Superseded).copied(),
            Some(1),
            "the fenced-in dead member surfaces under excluded, not abstain: {excluded:?}"
        );
        assert!(
            !ids.contains(&"cap-4"),
            "the successor is outside the fence"
        );
    }

    /// A fenced zero-match ABSTAINS and the honest-empty text NAMES the topic
    /// fence — never a floored, fabricated row.
    #[test]
    fn fenced_zero_match_abstains_and_names_the_topic_fence() {
        let mut store = cross_project_topic_world();
        let mut q = query(&["kiwi"]);
        q.topic = Some(topic_c());
        let response = retrieve(&mut store, &q, NOW).unwrap();
        let RetrieveResponse::Abstain { reason, topic, .. } = &response else {
            panic!("a fenced zero-match must abstain, never floor: {response:?}");
        };
        assert!(
            reason.contains("within topic 'cap-3' (2 members)"),
            "the honest-empty text names the topic fence: {reason}"
        );
        assert_eq!(
            topic.as_ref().map(|t| t.member_total),
            Some(2),
            "the machine twin of the fence label rides the abstain"
        );
    }

    /// Composed with an effort, the EFFORT clause still LEADS the fence label
    /// (S3's frozen contract) and the topic reads second.
    #[test]
    fn topic_clause_follows_the_effort_clause_in_the_fence_label() {
        let mut store = Store::open_in_memory().unwrap();
        store
            .append(&cap("epic and topic node", "nott", 0.9, VF, None), APPENDED)
            .unwrap(); // cap-1
        store
            .append(&cap("banana member", "nott", 0.9, VF, None), APPENDED)
            .unwrap(); // cap-2
        store
            .set_classification("cap-1", "epic", "project", NOW)
            .unwrap();
        store
            .upsert_relation(RelationKind::PartOf, "cap-2", "cap-1", APPENDED)
            .unwrap();
        store
            .upsert_relation(RelationKind::About, "cap-2", "cap-1", APPENDED)
            .unwrap();
        let q = RetrieveQuery {
            terms: vec!["kiwi".to_string()],
            project_id: Some("nott".to_string()),
            effort: Some(EffortScope {
                epic_id: "cap-1".to_string(),
                member_ids: vec!["cap-2".to_string()],
                open: true,
            }),
            topic: Some(TopicScope {
                topic_id: "cap-1".to_string(),
                member_ids: vec!["cap-2".to_string()],
            }),
            ..RetrieveQuery::default()
        };
        let response = retrieve(&mut store, &q, NOW).unwrap();
        let RetrieveResponse::Abstain { reason, .. } = &response else {
            panic!("expected abstain, got {response:?}");
        };
        assert!(
            reason.contains(
                "within effort 'cap-1' (1 members) and topic 'cap-1' (1 members) and project 'nott'"
            ),
            "effort leads, topic follows, project last: {reason}"
        );
    }

    /// Two id-set fences compose by INTERSECTION, which is what AND means.
    /// The effort holds {cap-1, cap-2}; the topic holds {cap-3, cap-4}; they
    /// are disjoint, so nothing can satisfy both and recall ABSTAINS — never
    /// widened to the union, never floored.
    #[test]
    fn disjoint_effort_and_topic_fences_intersect_to_nothing_and_abstain() {
        let mut store = Store::open_in_memory().unwrap();
        for content in [
            "widget epic node",
            "widget effort member",
            "widget topic node",
            "widget topic member",
        ] {
            store
                .append(&cap(content, "nott", 0.9, VF, None), APPENDED)
                .unwrap();
        }
        store
            .set_classification("cap-1", "epic", "project", NOW)
            .unwrap();
        store
            .upsert_relation(RelationKind::PartOf, "cap-2", "cap-1", APPENDED)
            .unwrap();
        store
            .upsert_relation(RelationKind::About, "cap-4", "cap-3", APPENDED)
            .unwrap();
        // Each fence ALONE grounds — the differential that makes the
        // intersection result meaningful rather than a vacuous empty store.
        let effort_only = RetrieveQuery {
            terms: vec!["widget".to_string()],
            effort: Some(EffortScope {
                epic_id: "cap-1".to_string(),
                member_ids: vec!["cap-2".to_string()],
                open: true,
            }),
            ..RetrieveQuery::default()
        };
        assert!(
            matches!(
                retrieve(&mut store, &effort_only, NOW).unwrap(),
                RetrieveResponse::Grounded { .. }
            ),
            "the effort fence alone grounds"
        );
        let topic_only = RetrieveQuery {
            terms: vec!["widget".to_string()],
            topic: Some(TopicScope {
                topic_id: "cap-3".to_string(),
                member_ids: vec!["cap-4".to_string()],
            }),
            ..RetrieveQuery::default()
        };
        assert!(
            matches!(
                retrieve(&mut store, &topic_only, NOW).unwrap(),
                RetrieveResponse::Grounded { .. }
            ),
            "the topic fence alone grounds"
        );
        // Together: disjoint sets intersect to nothing.
        let both = RetrieveQuery {
            terms: vec!["widget".to_string()],
            effort: effort_only.effort.clone(),
            topic: topic_only.topic.clone(),
            ..RetrieveQuery::default()
        };
        let response = retrieve(&mut store, &both, NOW).unwrap();
        let RetrieveResponse::Abstain { effort, topic, .. } = &response else {
            panic!("disjoint id-set fences must abstain, never union: {response:?}");
        };
        assert!(
            effort.is_some() && topic.is_some(),
            "both echoes ride the abstain so the caller sees WHICH pair emptied it"
        );
    }

    /// A topic with NO members is NOT degenerate: the fence still contains the
    /// topic node, so the topic capsule itself grounds. This is exactly why
    /// `topic_id` has no zero-member rejection while `effort_id` does.
    #[test]
    fn memberless_topic_still_grounds_the_topic_node_itself() {
        let mut store = Store::open_in_memory().unwrap();
        store
            .append(&cap("widget lonely topic", "nott", 0.9, VF, None), APPENDED)
            .unwrap(); // cap-1
        store
            .append(&cap("widget outsider row", "nott", 0.9, VF, None), APPENDED)
            .unwrap(); // cap-2
        let mut q = query(&["widget"]);
        q.topic = Some(TopicScope {
            topic_id: "cap-1".to_string(),
            member_ids: Vec::new(),
        });
        let response = retrieve(&mut store, &q, NOW).unwrap();
        let value = serde_json::to_value(&response).unwrap();
        assert_eq!(value["outcome"], "grounded", "{value}");
        let ids: Vec<&str> = value["results"]
            .as_array()
            .unwrap()
            .iter()
            .map(|r| r["id"].as_str().unwrap())
            .collect();
        assert_eq!(ids, vec!["cap-1"], "only the topic node is in the fence");
        assert_eq!(value["results"][0]["topic_role"], "topic");
        assert_eq!(value["topic"]["member_total"], 0);
    }

    #[test]
    fn topic_scoped_recall_is_deterministic() {
        let run = || {
            let mut store = cross_project_topic_world();
            let mut q = query(&["token"]);
            q.topic = Some(topic_c());
            serde_json::to_vec(&retrieve(&mut store, &q, NOW).unwrap()).unwrap()
        };
        assert_eq!(run(), run(), "seq/id only — no clock, no nondeterminism");
    }
}
