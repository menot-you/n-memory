//! # Retrieve — FTS5+bm25 recall, grounded-or-ABSTAIN, evidence envelope
//! (unit s4).
//!
//! The engine half of the LLM-first recall contract (`ARCHITECTURE.md` §0):
//! the CALLER is the intelligent half and arrives with an already-expanded
//! multi-term query (synonyms, aliases, rephrasings). This module does
//! honest lexical work only — FTS5 `OR` across the quoted terms, bm25
//! ranking, a deterministic tiebreak — and returns few, dense, layered
//! results under an explicit token budget. No embedder, no network, and no
//! clock read: `now` is injected at the surface boundary, exactly like the
//! store's `created_at`.
//!
//! ## Grounded, missing evidence, or ABSTAIN — never fabricate (W1 tri-state)
//!
//! A query resolves to exactly one honest outcome:
//!
//! - [`RetrieveResponse::Grounded`] — at least one eligible capsule
//!   survives every fence; unchanged shape.
//! - [`RetrieveResponse::MissingEvidence`] — terms DID match stored
//!   capsules (or named a forgotten one — below), but every match was
//!   excluded by an eligibility fence (quarantined, falsified, archived,
//!   superseded, expired, not-yet-valid, tombstoned — counted per
//!   [`ExclusionReason`]): evidence exists (or existed), none of it may
//!   ground recall.
//! - [`RetrieveResponse::Abstain`] — zero raw matches: nothing to ground
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
//! class, confidence, and the match explain (`matched_terms` + a
//! normalized `relevance` + the rounded `bm25` behind it).
//! Stored content is never rendered as directives and never inlined whole:
//! the envelope carries only a `headline` (first line, at most
//! [`HEADLINE_MAX_CHARS`] chars); the full capsule stays one `get` away
//! (layered recall).
//!
//! ## Determinism (PLAN s4 tiebreak + h4 usage late key + w2 decay)
//!
//! Ranking is a pure function of stored fields plus the injected `now`:
//! term coverage descending (w1d — a capsule matching more of the
//! caller's term GROUPS outranks a higher-bm25 single-term match), then
//! bm25 score ascending (SQLite's bm25 is smaller-is-better; scores are
//! negative), then the ADVISORY decayed weight descending (w2 — its
//! section below; it REPLACES the former raw-`confidence` key, and
//! same-age capsules still order by confidence exactly as before), then
//! `freshness.valid_from` descending, then — LATE, ordering full ties
//! only — usage recency descending and `recall_count` descending (the h4
//! sidecar; never-recalled sorts last), then id ascending (numeric `seq`
//! order, so `cap-2` precedes `cap-10`). Usage is a tiebreak input and
//! nothing more: it NEVER touches confidence or authority (ARCHITECTURE
//! §1 law: usage is not success evidence), and no envelope field carries
//! it. `now` decides WHICH capsules are currently valid and feeds the
//! decay ages — nothing else: the same store state queried at the same
//! `now` returns byte-identical JSON (the deliberate exceptions are
//! `anchor_live` and `anchor_drift`, which read the live filesystem —
//! their sections below).
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
    CapsuleId, EpistemicsRecord, RecallMissOutcome, Store, StoreError, StoredCapsule,
    StoredEmbedding, TombstoneRecord, UsageStat, fold_diacritic,
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

/// Default cap on the vector lane's candidate count (w3 u6a): with
/// `query_embedding` present but no explicit `vector_k`, recall fuses the
/// top-`DEFAULT_VECTOR_K` capsules by cosine similarity. Bounds the vector
/// lane's reach the way `limit`/`token_budget` bound the returned set; the
/// caller widens it via `vector_k`.
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
    /// [`Store::search_fts_scoped`] (`project_id` + w2 `project_prefix`
    /// fences AND-compose).
    fn search_fts(
        &self,
        terms: &[String],
        project_id: Option<&str>,
        project_prefix: Option<&str>,
    ) -> Result<Vec<(StoredCapsule, f64)>, StoreError>;
    /// [`Store::get_tombstone`].
    fn get_tombstone(&self, id: &str) -> Result<Option<TombstoneRecord>, StoreError>;
    /// [`Store::is_superseded`].
    fn is_superseded(&self, id: &str) -> Result<bool, StoreError>;
    /// [`Store::is_falsified`] (u6h): whether a `falsifies` edge names `id`
    /// as target — the eligibility fence between quarantine and archive.
    fn is_falsified(&self, id: &str) -> Result<bool, StoreError>;
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
    ) -> Result<Vec<(StoredCapsule, StoredEmbedding)>, StoreError>;
    /// u-r2 contract: the capture-time anchored-file hash —
    /// [`Store::anchor_hash_of`]. `None` (every capsule the boundary could
    /// not hash at capture) degrades `anchor_drift` to `"unknown"`.
    fn anchor_hash_of(&self, id: &str) -> Result<Option<String>, StoreError>;
    /// u-r2 contract: the epistemic sidecar — [`Store::epistemics_of`].
    /// `None` (never annotated) omits the envelope's epistemic fields.
    fn epistemics_of(&self, id: &str) -> Result<Option<EpistemicsRecord>, StoreError>;
}

impl RecallStore for Store {
    // Pure delegation to the inherent methods.
    fn search_fts(
        &self,
        terms: &[String],
        project_id: Option<&str>,
        project_prefix: Option<&str>,
    ) -> Result<Vec<(StoredCapsule, f64)>, StoreError> {
        Store::search_fts_scoped(self, terms, project_id, project_prefix)
    }
    fn get_tombstone(&self, id: &str) -> Result<Option<TombstoneRecord>, StoreError> {
        Store::get_tombstone(self, id)
    }
    fn is_superseded(&self, id: &str) -> Result<bool, StoreError> {
        Store::is_superseded(self, id)
    }
    fn is_falsified(&self, id: &str) -> Result<bool, StoreError> {
        Store::is_falsified(self, id)
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
    ) -> Result<Vec<(StoredCapsule, StoredEmbedding)>, StoreError> {
        Store::embeddings_for_recall(self, project_id, project_prefix)
    }

    // u-r2 sidecar reads: pure delegation, like every read above.
    fn anchor_hash_of(&self, id: &str) -> Result<Option<String>, StoreError> {
        Store::anchor_hash_of(self, id)
    }
    fn epistemics_of(&self, id: &str) -> Result<Option<EpistemicsRecord>, StoreError> {
        Store::epistemics_of(self, id)
    }
}

/// A recall request. Terms are caller-expanded: the LLM brings its own
/// synonyms/aliases/rephrasings as separate terms; the engine matches
/// FTS5 `OR` across them — a multi-word term matches as the AND of its
/// words (order/adjacency-insensitive), never as FTS5 syntax. The
/// w2-store2 synonym sidecar additionally expands each term with its
/// recorded aliases (module doc: Synonym expansion).
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
    /// w3 u6a caller-fed semantic lane. `None` (the DORMANT default) →
    /// recall is byte-identical to the FTS-only engine: no vector table is
    /// read, no fusion runs, envelopes carry no vector fields. `Some(v)` →
    /// the cosine-similarity vector lane runs and its ranks are RRF-fused
    /// with the FTS term lane — admitting ONLY positively-similar
    /// embeddings (cosine > 0; fleet-8 c7: an orthogonal or
    /// anti-correlated embedding never solely-grounds a result). The
    /// embedding is caller-supplied (no embedder dependency; the store
    /// computes nothing); its dimension must match the stored embeddings'
    /// ([`RetrieveError::DimensionMismatch`]), and it must be
    /// non-empty/finite/non-zero
    /// ([`RetrieveError::InvalidQueryEmbedding`]).
    pub query_embedding: Option<Vec<f32>>,
    /// w3 u6a: cap on the vector lane's candidate count — the top
    /// `vector_k` capsules by cosine feed fusion. `None` →
    /// [`DEFAULT_VECTOR_K`]. Ignored entirely when `query_embedding` is
    /// `None` (dormant). `Some(0)` yields an empty vector lane (fusion
    /// degenerates to the FTS order).
    pub vector_k: Option<usize>,
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
    /// w3 u6a fusion explain: this row's 1-based position in the
    /// RRF-fused ranking — present on EVERY returned row of a fused query
    /// (so the caller can read the fused order), absent in a dormant
    /// (FTS-only) query. A row with `fusion_rank` but no
    /// `vector_similarity` was ranked by fusion but matched only the term
    /// lane. Advisory explain, never authority.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub fusion_rank: Option<usize>,
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
        /// Grounded matches (lexical, not superseded, currently valid)
        /// before limit/budget trimming.
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
        /// not_yet_valid / tombstoned-id probe), by reason — present only
        /// when nonzero, so a grounded outcome no longer hides that
        /// ineligible evidence existed.
        #[serde(skip_serializing_if = "BTreeMap::is_empty")]
        excluded: BTreeMap<ExclusionReason, usize>,
    },
    /// Terms DID match stored capsules, but every match was excluded by
    /// an eligibility fence — evidence exists, none of it may ground
    /// recall. Distinct from [`RetrieveResponse::Abstain`]: the caller
    /// learns that relevant-but-ineligible capsules exist (reachable via
    /// `get`/`list`) while not one excluded byte reaches the response.
    MissingEvidence {
        /// Raw matches excluded — lexical matches plus terms naming a
        /// forgotten id (equals the sum over `excluded`).
        excluded_count: usize,
        /// Exclusion breakdown by reason, e.g. `{"superseded": 2}`.
        /// Deterministic key order: [`ExclusionReason`] variant order.
        excluded: BTreeMap<ExclusionReason, usize>,
        /// Honest human-readable account — counts per reason plus the
        /// `get`/`list` escape hatch. Pure function of the counts.
        reason: String,
    },
    /// Nothing matched at all — the honest empty answer, never a
    /// fabricated one.
    Abstain {
        /// Why recall abstained: zero raw lexical matches (matched-but-
        /// excluded is [`RetrieveResponse::MissingEvidence`] instead).
        reason: String,
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
}

/// Run one recall pass over the store at the injected instant `now`.
///
/// Pipeline: validate terms → synonym expansion into per-term OR-groups
/// (module doc; `aliases_for` on the w2-store2 seam) → FTS5 `OR` match
/// via [`Store::search_fts`] (project-fenced) + the forgotten-id probe
/// (module doc; each caller term that names a tombstoned id counts one
/// [`ExclusionReason::Tombstoned`] raw match); zero of either →
/// [`RetrieveResponse::Abstain`] → eligibility fences, each exclusion
/// counted per [`ExclusionReason`] under the FIRST fence that caught
/// it: lifecycle tier (quarantined, then archived — retired from
/// grounding by default; the module-doc dominance law), then superseded
/// (replaced capsules never ground recall; the live successor speaks),
/// then currency at `now` (expired / not-yet-valid) — all of them stay
/// reachable via `get`/`list`;
/// every match excluded → [`RetrieveResponse::MissingEvidence`] with
/// the counts → deterministic sort (module doc: coverage, bm25, the w2
/// decay key, valid_from, the usage late key, id) → `limit` +
/// token-budget trim → envelope build (incl. `decayed_weight` and the
/// `anchor_live` probe) → recall counting on the RETURNED ids
/// ([`Store::record_recall`] at `now` — the reason for `&mut`) →
/// [`RetrieveResponse::Grounded`].
///
/// u-r5 miss-ledger: AFTER the response is computed, an ungrounded outcome
/// (`missing_evidence` / `abstain`) records its query terms to the
/// recall-miss ledger ([`Store::record_recall_miss`]) — misses teach
/// vocabulary. A `grounded` outcome records nothing. The record is
/// FAIL-OPEN telemetry: the write error is SWALLOWED here so a ledger
/// failure can never fail or delay recall — the ONE deliberate exception
/// to the crate's fail-closed default, sound because a lost miss row costs
/// only an advisory alias hint, never a canonical byte. The recording runs
/// on the concrete [`Store`] (not the [`RecallStore`] recall seam) so the
/// pure recall algorithm stays untouched.
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
    let response = retrieve_core(store, query, now, anchor_root)?;
    // Map the ungrounded outcomes to a ledger entry; grounded records
    // nothing. Recording uses the RAW caller terms — the store folds and
    // deduplicates them (the alias-key normalization).
    let miss_outcome = match &response {
        RetrieveResponse::MissingEvidence { .. } => Some(RecallMissOutcome::MissingEvidence),
        RetrieveResponse::Abstain { .. } => Some(RecallMissOutcome::Abstain),
        // fleet-8 c7 F1: a vector-grounded answer whose TERM lane matched
        // nothing still records its terms as an abstain — the terms DID
        // miss (only the embedding hit), and the R5 vocabulary loop must
        // not be silently disabled by the very lane its evidence gates.
        RetrieveResponse::Grounded { results, .. }
            if results.iter().all(|r| r.matched_terms.is_empty()) =>
        {
            Some(RecallMissOutcome::Abstain)
        }
        RetrieveResponse::Grounded { .. } => None,
    };
    if let Some(outcome) = miss_outcome {
        // FAIL-OPEN: swallow the ledger write error — telemetry never
        // fails or delays the retrieve.
        let _ = store.record_recall_miss(&query.terms, outcome, now);
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
    usage: Option<UsageStat>,
}

/// The engine behind [`retrieve`], generic over the [`RecallStore`]
/// contract seam; `anchor_root` is injected so tests probe liveness
/// against a hermetic temp root instead of the boot-injected production
/// root ([`crate::server::BoundaryConfig::anchor_root`]).
fn retrieve_core<S: RecallStore>(
    store: &mut S,
    query: &RetrieveQuery,
    now: OffsetDateTime,
    anchor_root: &Path,
) -> Result<RetrieveResponse, RetrieveError> {
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

    // Synonym expansion (module doc): each term becomes an OR-group of
    // itself plus its recorded aliases; the flattened list feeds ONE
    // FTS5 OR query. Group aliases dedup on folded form WITHIN their
    // group only — attribution stays per-group truthful even when terms
    // overlap — while the search list dedups globally (a string already
    // searched widens nothing).
    let mut groups: Vec<TermGroup> = Vec::with_capacity(terms.len());
    let mut search_terms: Vec<String> = terms.clone();
    let mut alias_count = 0usize;
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

    // Fence label for the honest empty answers (w2-fix): BOTH scope
    // fences are named — an empty recall caused by a project_prefix must
    // blame the fence, never the terms (symmetric with project_id).
    let fence = match (&query.project_id, &query.project_prefix) {
        (Some(project), Some(prefix)) => {
            format!(" within project '{project}' and subtree '{prefix}'")
        }
        (Some(project), None) => format!(" within project '{project}'"),
        (None, Some(prefix)) => format!(" within project subtree '{prefix}'"),
        (None, None) => String::new(),
    };

    let matches = store.search_fts(
        &search_terms,
        query.project_id.as_deref(),
        query.project_prefix.as_deref(),
    )?;
    let lexical = matches.len();

    // Forgotten-id probe (module doc): a CALLER term that exactly names
    // a tombstoned capsule id is a raw match excluded as Tombstoned —
    // the content is gone by design and can never match lexically, but
    // the caller who named the id deserves "forgotten", not "never
    // existed". Terms are already deduplicated, so one tombstone counts
    // once; aliases are store-derived data and never probe.
    let mut tombstone_hits = 0usize;
    for term in &terms {
        if store.get_tombstone(term)?.is_some() {
            tombstone_hits += 1;
        }
    }

    // w3 u6a caller-fed vector lane. DORMANT when `query_embedding` is
    // absent: `vector_scored` stays empty, `fused` is false, and every
    // branch below collapses to the exact FTS-only engine (proven
    // byte-identical by the dormant differential). PRESENT: the caller's
    // embedding is validated, cosine similarity is computed against every
    // LIVE in-scope stored embedding (the store computes NO embedding —
    // zero embedder dependency), and the top `vector_k` by cosine become
    // the vector lane's raw matches. The dimension check names both sides
    // on a mismatch, at the first (lowest-seq) offender — deterministic.
    let fused = query.query_embedding.is_some();
    let mut vector_scored: Vec<(StoredCapsule, f64)> = Vec::new();
    if let Some(query_embedding) = query.query_embedding.as_deref() {
        validate_query_embedding(query_embedding)?;
        let vector_k = query.vector_k.unwrap_or(DEFAULT_VECTOR_K);
        for (stored, embedding) in store
            .embeddings_for_recall(query.project_id.as_deref(), query.project_prefix.as_deref())?
        {
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
        // Vector-lane raw matches: the top `vector_k` by cosine desc, ties
        // broken by seq asc (deterministic). Truncated BEFORE the fences
        // so `vector_k` caps the lane's reach; the eligibility fences then
        // apply to these top-K exactly as they do to the FTS matches.
        vector_scored.sort_by(|a, b| b.1.total_cmp(&a.1).then_with(|| a.0.seq.cmp(&b.0.seq)));
        vector_scored.truncate(vector_k);
    }

    // Abstain only when BOTH lanes (and the tombstone probe) are empty —
    // an honest empty answer, never fabricated. DORMANT reduces to the
    // former `lexical == 0 && tombstone_hits == 0` (vector_scored is
    // always empty), and the reason text is byte-identical (the vector
    // note is empty). FUSED adds the note only when a vector lane ran but
    // found no in-scope embedding to compare.
    if lexical == 0 && tombstone_hits == 0 && vector_scored.is_empty() {
        let alias_note = if alias_count > 0 {
            format!(" (terms expanded with {alias_count} store-fed alias(es))")
        } else {
            String::new()
        };
        let vector_note = if fused {
            " (no stored embedding was available to compare with positive similarity)".to_string()
        } else {
            String::new()
        };
        return Ok(RetrieveResponse::Abstain {
            reason: format!(
                "no stored capsule matched any of the {} query term(s){fence}{alias_note}{vector_note}; \
                 abstaining instead of fabricating",
                terms.len()
            ),
        });
    }

    // Eligibility fences (W1 tri-state + w2 tier + u6h falsified): each raw
    // match either survives into `current` or is counted under the FIRST
    // fence that caught it — quarantined, then FALSIFIED, then archived,
    // then superseded (h4), then currency. Precedence is a LAW, not an
    // accident (w2-fix + u6h): quarantine dominates everything (the taint
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
            Some(score),
            None,
            store,
            &groups,
            now,
            &mut excluded,
        )? {
            survivor_idx.insert(seq, current.len());
            current.push(candidate);
        }
    }
    // Vector lane fence pass (fused only): annotate a both-lanes survivor
    // with its cosine, skip an already-fenced capsule (dominance holds),
    // and fence a brand-new vector-only match through the SAME gate.
    for (stored, cosine) in vector_scored {
        let seq = stored.seq;
        if let Some(&idx) = survivor_idx.get(&seq) {
            current[idx].cosine = Some(cosine);
            continue;
        }
        if seen.contains(&seq) {
            continue;
        }
        seen.insert(seq);
        if let Some(candidate) = fence_candidate(
            stored,
            None,
            Some(cosine),
            store,
            &groups,
            now,
            &mut excluded,
        )? {
            survivor_idx.insert(seq, current.len());
            current.push(candidate);
        }
    }
    if current.is_empty() {
        // Every distinct match was excluded; the count is the sum over the
        // reason map (== `lexical + tombstone_hits` in the dormant case,
        // where FTS matches are the only distinct candidates).
        return Ok(missing_evidence(excluded.values().sum(), excluded, &fence));
    }

    if fused {
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

    let token_budget = query.token_budget.unwrap_or(DEFAULT_TOKEN_BUDGET);
    // Anchor of the relevance scale (FTS-lane explain): the strongest
    // (most negative) bm25 score present. DORMANT: this is the top-ranked
    // row's score exactly as before (the FTS sort puts the strongest first
    // within the top coverage tier). FUSED: the min over FTS-matched rows,
    // so FTS relevance stays a sane 0..1 even when a vector-only row leads
    // the fused order. `current` is non-empty here.
    let top_score = if fused {
        current
            .iter()
            .filter_map(|c| c.score)
            .reduce(f64::min)
            .unwrap_or(0.0)
    } else {
        current.first().map_or(0.0, |c| c.score.unwrap_or(0.0))
    };
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
        let envelope = evidence_for(
            candidate,
            top_score,
            &groups,
            anchor_root,
            capture_hash.as_deref(),
            epistemics,
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
    Ok(RetrieveResponse::Grounded {
        results,
        matched,
        returned,
        trimmed: matched - returned,
        trimmed_by_limit,
        trimmed_by_budget,
        token_budget,
        excluded,
    })
}

/// The h4 LATE usage tiebreak key: most-recent recall first, then higher
/// `recall_count`; a never-recalled capsule (`None` — also the state
/// after the derived table is dropped) sorts after any recalled one.
fn usage_key(usage: Option<UsageStat>) -> (Option<OffsetDateTime>, i64) {
    usage.map_or((None, 0), |u| (Some(u.last_recalled_at), u.recall_count))
}

/// Build the [`RetrieveResponse::MissingEvidence`] outcome: every raw
/// match (lexical, or a term naming a forgotten id) was excluded by an
/// eligibility fence. Pure function of the exclusion counts and the
/// query's fence label — deterministic bytes: the map and the prose both
/// walk [`ExclusionReason`] variant order.
fn missing_evidence(
    total: usize,
    excluded: BTreeMap<ExclusionReason, usize>,
    fence: &str,
) -> RetrieveResponse {
    let detail: Vec<String> = excluded
        .iter()
        .map(|(reason, count)| format!("{count} {}", reason.wire_name()))
        .collect();
    // Reachability clause, accurate PER EXCLUSION CLASS present (w1d
    // stress fix: tombstoned capsules are absent from list — claiming
    // get/list for them was false): every non-tombstoned exclusion
    // (superseded / archived / quarantined / expired / not-yet-valid)
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
        "{total} capsule(s) matched{fence} but every one is excluded from \
         recall ({}); reporting missing evidence instead of recalling ineligible \
         capsules {reachability}",
        detail.join(", ")
    );
    RetrieveResponse::MissingEvidence {
        excluded_count: total,
        excluded,
        reason,
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
fn evidence_for(
    candidate: &Candidate,
    top: f64,
    groups: &[TermGroup],
    anchor_root: &Path,
    capture_hash: Option<&str>,
    epistemics: Option<EpistemicsRecord>,
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
/// archived, then superseded, then currency) runs for an FTS match and a
/// vector match, so a fenced capsule can never surface via either lane.
/// Returns the built [`Candidate`] when the match survives, or `None` after
/// counting the exclusion under the first fence that caught it.
fn fence_candidate<S: RecallStore>(
    stored: StoredCapsule,
    score: Option<f64>,
    cosine: Option<f64>,
    store: &S,
    groups: &[TermGroup],
    now: OffsetDateTime,
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
    let usage = store.usage_of(stored.id.as_str())?;
    let coverage = matched_groups(stored.capsule.content(), groups).len();
    let decayed = decay_weight(
        stored.capsule.confidence().value(),
        stored.capsule.freshness().valid_from,
        now,
    );
    Ok(Some(Candidate {
        coverage,
        decayed,
        stored,
        score,
        cosine,
        fusion_rank: None,
        usage,
    }))
}

/// First line of `content`, capped at [`HEADLINE_MAX_CHARS`] chars, with
/// `…` appended whenever anything (rest of the line or further lines)
/// was left out. A lone trailing line terminator is not content — it
/// never earns the ellipsis (w3 review: false `…` on `"x\n"`).
///
/// `pub(crate)`: the ONE headline law across surfaces — `export` renders
/// through this same fn (v7 convergence), so the two windows can never
/// tell two stories about one first line.
pub(crate) fn headline_of(content: &str) -> String {
    let content = content
        .strip_suffix("\r\n")
        .or_else(|| content.strip_suffix('\n'))
        .unwrap_or(content);
    let first_line = content.lines().next().unwrap_or("");
    let headline: String = first_line.chars().take(HEADLINE_MAX_CHARS).collect();
    let cut_line = first_line.chars().count() > HEADLINE_MAX_CHARS;
    // `first_line` is a prefix slice of `content`, so a byte-length
    // comparison exactly answers "is there content beyond it".
    let more_content = content.len() > first_line.len();
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
    use crate::store::RelationKind;
    use crate::store::TombstoneMode;
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
    }

    impl ContractStore {
        fn new(inner: Store) -> Self {
            ContractStore {
                inner,
                aliases: BTreeMap::new(),
                tiers: BTreeMap::new(),
            }
        }
    }

    impl RecallStore for ContractStore {
        fn search_fts(
            &self,
            terms: &[String],
            project_id: Option<&str>,
            project_prefix: Option<&str>,
        ) -> Result<Vec<(StoredCapsule, f64)>, StoreError> {
            self.inner
                .search_fts_scoped(terms, project_id, project_prefix)
        }
        fn get_tombstone(&self, id: &str) -> Result<Option<TombstoneRecord>, StoreError> {
            self.inner.get_tombstone(id)
        }
        fn is_superseded(&self, id: &str) -> Result<bool, StoreError> {
            self.inner.is_superseded(id)
        }
        fn is_falsified(&self, id: &str) -> Result<bool, StoreError> {
            self.inner.is_falsified(id)
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
        ) -> Result<Vec<(StoredCapsule, StoredEmbedding)>, StoreError> {
            // Real delegation: the vector sidecar lives on the inner Store,
            // so contract tests populate it with `inner.put_embedding` and
            // the engine reads it through this seam exactly as production.
            self.inner.embeddings_for_recall(project_id, project_prefix)
        }
        fn anchor_hash_of(&self, id: &str) -> Result<Option<String>, StoreError> {
            // Real delegation (u-r2): the sidecar lives on the inner Store.
            self.inner.anchor_hash_of(id)
        }
        fn epistemics_of(&self, id: &str) -> Result<Option<EpistemicsRecord>, StoreError> {
            self.inner.epistemics_of(id)
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
        let RetrieveResponse::Abstain { reason } = &response else {
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
        let before = serde_json::to_string(&retrieve(&mut store, &q, NOW).unwrap()).unwrap();

        // Drop the derived table out from under the store.
        let raw = rusqlite::Connection::open(&path).unwrap();
        raw.execute_batch("DROP TABLE capsules_fts").unwrap();
        drop(raw);

        assert_eq!(store.rebuild_fts().unwrap(), 3);
        let after = serde_json::to_string(&retrieve(&mut store, &q, NOW).unwrap()).unwrap();
        assert_eq!(
            before, after,
            "recall must be byte-identical after drop→rebuild"
        );
        assert!(before.contains("\"outcome\":\"grounded\""));
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
        let RetrieveResponse::Abstain { reason } = response else {
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
        let RetrieveResponse::Abstain { reason } = &response else {
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
                APPENDED,
            )
            .unwrap();
        assert_eq!(outcome.id, "out-1");
        assert_eq!(outcome.capsule_id.as_deref(), Some("cap-1"));
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
        let RetrieveResponse::Abstain { reason } = &abstain else {
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
        let RetrieveResponse::Abstain { reason } = &both else {
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
        let RetrieveResponse::Abstain { reason } = &response else {
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
}
