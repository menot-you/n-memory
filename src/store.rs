//! # SQLite store — append/get/list over the frozen Capsule v1 (unit s2),
//! plus the w1/w2 sidecar planes (schema v3).
//!
//! Single-file SQLite (bundled — hermetic, no system library). The capsule
//! is persisted as its canonical JSON ([`Capsule::to_canonical_json`]); the
//! four filter columns (`source_hash`, `project_id`, `authority_class`,
//! `valid_from`) are projections DERIVED from the capsule at append time and
//! exist only for indexed filtering. Reads always decode the canonical JSON
//! through the Capsule's validated deserialization, so every read is a full
//! contract re-validation — a corrupt row surfaces as [`StoreError::Corrupt`],
//! never as a silently-drifted capsule.
//!
//! ## Determinism law
//!
//! The store is a pure function of its append sequence: it reads NO wall
//! clock and NO randomness. `created_at` arrives as the injected `now`
//! parameter of [`Store::append`] (the surface boundary owns time), every
//! sidecar `at` is likewise an injected `now`, the tombstone HMAC key is an
//! injected parameter, and ids are derived from append order — `cap-<seq>`,
//! starting at `cap-1`. Replaying the same mutation sequence into two fresh
//! stores yields byte-identical [`Store::canonical_snapshot`] output (the
//! h3 determinism-conformance comparand).
//!
//! ## Schema v2 (w1) — the sidecar plane
//!
//! Capsule v1 is FROZEN (`.2` §4): every new fact about a capsule lands as
//! a SIDECAR table or column, NEVER a Capsule field. The v2 sidecars:
//!
//! | table | rung | holds |
//! |---|---|---|
//! | `relations` | canonical | typed edges, closed kinds `supersedes` / `derived_from` / `witnesses` / `blocks` (donor B ontology) |
//! | `audit_events` | canonical | append-only mutation ledger (`actor`/`action`/`subject`/`reason`) |
//! | `classifications` | canonical | one `{fact,procedure,decision}` × `{project,global,session}` label per capsule |
//! | `tombstones` | canonical | what remains after [`Store::forget_capsule`]: mode + keyed content HMAC + reason |
//! | `sessions` | canonical | start/finish bracketing records; capsules link via the nullable `capsules.session_id` column |
//! | `capsules_fts` | derived | FTS5 recall mirror — droppable, re-derivable |
//! | `usage` | derived | recall counters — droppable, ranking tiebreak only |
//!
//! ## Schema v3 (w2) — lifecycle tiers, synonyms, journal chain, scope prefix
//!
//! | table / column | rung | holds |
//! |---|---|---|
//! | `tiers` | canonical | one lifecycle tier per capsule (`active` / `archived` / `quarantined`); absent row = `active` (the default tier is a rule, not a row) |
//! | `synonyms` | derived | caller-fed alias pairs (the LLM teaches the index its own vocabulary); lowercased + diacritic-folded on write; droppable — the caller re-teaches |
//! | `audit_events.chained_hash` | derived | per-row journal hash chain: `sha256(prev_hash + canonical audit line)` — deterministically re-derivable from the rows, backfilled on migration |
//!
//! **Journal chain (w2):** every [`Store::append_audit`] computes
//! `chained_hash = sha256(prev_hash + canonical_line)` where `prev_hash` is
//! the previous row's `chained_hash` (`""` for seq 1) and `canonical_line`
//! is the row's fixed-order JSON ([`audit_canonical_line`]). The head is
//! [`Store::journal_head`]; [`Store::verify_chain`] recomputes the whole
//! chain and names the FIRST broken seq on any in-place tamper
//! ([`StoreError::JournalBroken`]). The chain proves internal consistency
//! (no row edited, reordered, or deleted mid-ledger); TRUNCATION of the
//! tail is out of its reach by construction — detecting it needs the head
//! pinned outside the file (boundary concern, documented honestly).
//!
//! **Scope prefix (w2):** [`ListFilter::project_prefix`] fences a listing
//! or search to a project subtree: it matches `project_id == p` OR
//! `project_id` starting with `p + "/"` (so `nott` covers `nott` and
//! `nott/x`, never `nottx`). [`Store::list`] and
//! [`Store::search_fts_scoped`] honor it; present fences AND-compose.
//!
//! **Audit policy (documented law):** every mutating call site — capture,
//! supersede/relation, classification, forget, session open/finish, tier
//! moves ([`Store::set_tier`]), synonym teaching ([`Store::add_alias`]) —
//! is expected to record an [`Store::append_audit`] event naming its actor
//! and subject. The store exposes the ledger; the SURFACE wires the call sites
//! (the store does not self-audit: the actor is boundary knowledge, and a
//! store-minted actor string would be a fabricated attribution).
//!
//! **Forget honesty:** [`Store::forget_capsule`] NULLs the one
//! content-bearing column (`canonical_json`), empties the FTS mirror row in
//! the same transaction, and records a tombstone whose `content_hmac` is a
//! keyed HMAC-SHA-256 (donor `fingerprint.rs` behavior — keyed, so a
//! dictionary of likely secrets cannot be matched against tombstones in
//! bulk). Connections run with `PRAGMA secure_delete = ON`, so the
//! overwritten cells are zeroed rather than left in free pages. Reads
//! return the typed [`StoreError::Tombstoned`] marker, never the content;
//! the UNIQUE `source_hash` backstop survives, so a forgotten capture
//! cannot silently resurrect via re-ingest. Irreversible by construction.
//!
//! The FTS5 mirror `capsules_fts` (unit s4) is DERIVED, never authority:
//! its row is inserted in the same transaction as the canonical row on
//! append, the whole table re-derives from `capsules` at will
//! ([`Store::rebuild_fts`] — tombstoned rows re-derive as the empty string,
//! unfindable), and open heals a mirror whose row count drifted from the
//! canonical table (so a pre-fts file upgrades in place). Dropping it loses
//! nothing. `usage` (unit h4) holds per-capsule recall counters (derived
//! advisory data, droppable: [`Store::record_recall`], [`Store::usage_of`]).
//! Usage is a LATE ranking tiebreak input only, never confidence/authority
//! (ARCHITECTURE §1 law: usage is not success evidence).

use std::collections::{BTreeMap, BTreeSet};
use std::fmt;
use std::path::Path;

use hmac::{Hmac, KeyInit, Mac};
use rusqlite::{
    Connection, MAIN_DB, OpenFlags, OptionalExtension, TransactionBehavior, params,
    params_from_iter, types::ValueRef,
};
use serde::{Deserialize, Serialize};
use sha2::Sha256;
use time::OffsetDateTime;
use time::format_description::well_known::Rfc3339;

use crate::capsule::{AuthorityClass, Capsule, sha256_hex};
use crate::substrate::{OutcomeRecord, PreferenceRecord, SubstrateError};

/// The `capsules` column block, shared verbatim by the fresh-create path
/// and the v1→v2 rebuild so the two shapes can NEVER drift (a test compares
/// `pragma_table_info` of both paths). `seq` is the rowid alias and the
/// determinism spine: append order IS identity (`id = "cap-" + seq`),
/// assigned explicitly inside the insert transaction. `canonical_json` is
/// the authority column — nullable since v2: NULL is the tombstone state
/// (content removed by [`Store::forget_capsule`]); the filter columns and
/// the `session_id` sidecar column survive as the id/provenance skeleton.
fn capsules_create_sql(head: &str) -> String {
    format!(
        "{head} (
    seq             INTEGER PRIMARY KEY,
    id              TEXT NOT NULL UNIQUE,
    canonical_json  TEXT,
    created_at      TEXT NOT NULL,
    source_hash     TEXT NOT NULL,
    project_id      TEXT NOT NULL,
    authority_class TEXT NOT NULL,
    valid_from      TEXT NOT NULL,
    session_id      TEXT
);"
    )
}

/// The `relations` column block, shared by fresh-create and the v1→v2
/// rebuild (same no-drift discipline as [`capsules_create_sql`]). A
/// relation is a directed, typed edge `from --kind--> to` over the donor B
/// closed ontology; the CHECK fences the closed enum at the SQL layer too.
/// The composite primary key makes re-recording the same edge a no-op
/// (keeping the FIRST `at` AND the first `origin` — records, not columns),
/// while a capsule may participate in any number of edges. `origin`
/// (u-r8 round 3) records WHO wrote the edge: `manual` — a caller decision
/// — or `import` — the stale-import supersession mechanism; only `import`
/// edges are ever machine-reversed ([`Store::unsupersede`]).
fn relations_create_sql(head: &str) -> String {
    format!(
        "{head} (
    kind    TEXT NOT NULL CHECK (kind IN ('supersedes', 'derived_from', 'witnesses', 'blocks', 'falsifies', 'proposes', 'part_of', 'grounded_in')),
    from_id TEXT NOT NULL,
    to_id   TEXT NOT NULL,
    at      TEXT NOT NULL,
    origin  TEXT NOT NULL DEFAULT 'manual' CHECK (origin IN ('manual', 'import')),
    PRIMARY KEY (kind, from_id, to_id)
);"
    )
}

/// The `audit_events` column block, shared verbatim by the fresh-create
/// path and the v2→v3 rebuild so the two shapes can NEVER drift (same
/// no-drift discipline as [`capsules_create_sql`]). `seq` is assigned
/// explicitly (MAX+1, same discipline as capsules); `reason` is the one
/// nullable column; `chained_hash` (v3) is the journal hash-chain link —
/// `sha256(prev_hash + canonical line)`, derived and re-derivable. Rows
/// are never updated or deleted (the backfill inside [`migrate_to_current`]
/// fills the then-new column exactly once, in the migration transaction).
fn audit_events_create_sql(head: &str) -> String {
    format!(
        "{head} (
    seq          INTEGER PRIMARY KEY,
    at           TEXT NOT NULL,
    actor        TEXT NOT NULL,
    action       TEXT NOT NULL,
    subject      TEXT NOT NULL,
    reason       TEXT,
    chained_hash TEXT NOT NULL
);"
    )
}

/// The `classifications` column block, shared verbatim by the
/// fresh-create path and the CHECK rebuilds (v3→v4, v6→v7) so the shapes
/// can NEVER drift (same no-drift discipline as [`capsules_create_sql`]).
/// One label per capsule (`capsule_id` PK — a re-classification
/// upserts). Closed string sets are CHECK-fenced, mirroring the
/// extract/classify vocabulary without importing its types; the kind set
/// is the u-r11 ten ([`CLASSIFICATION_KINDS`]).
fn classifications_create_sql(head: &str) -> String {
    format!(
        "{head} (
    capsule_id TEXT PRIMARY KEY,
    kind       TEXT NOT NULL CHECK (kind IN ('fact', 'procedure', 'decision', \
'task', 'epic', 'brainstorm', 'doc', 'constraint', 'capability', 'failure_pattern')),
    scope      TEXT NOT NULL CHECK (scope IN ('project', 'global', 'session')),
    at         TEXT NOT NULL
);"
    )
}

/// Indexes + the remaining w1/w2 sidecar tables (tombstones / sessions /
/// tiers / synonyms). All `IF NOT EXISTS`: executed on every open (fresh
/// create, post-migration, and re-open) inside one transaction, AFTER
/// the shared-DDL tables (`capsules`, `relations`, `audit_events`,
/// `classifications`) exist.
///
/// - `tombstones`: one per forgotten capsule; `reason` is NOT NULL — a
///   forget without a stated reason is not recordable, by construction.
///   `provenance_source`/`provenance_anchor` are populated ONLY for mode
///   `redacted` (the mode's whole point: provenance deliberately retained
///   for audit); `purged` leaves them NULL. Existing v2 files gain the two
///   nullable columns via the conditional ALTER in [`migrate_to_current`].
///   `source_hash` (v11) records the forgotten capsule's content identity
///   for BOTH modes — content is a HASH, not the removed bytes — so a
///   forget propagates cross-store by content ([`crate::merge`]); a pre-v11
///   marker backfills NULL and simply cannot propagate that way.
/// - `sessions`: start/finish bracketing; `finished_at`/`summary` stay NULL
///   until [`Store::finish_session`].
/// - `tiers` (w2, canonical): one lifecycle tier per capsule; the CHECK
///   fences the closed snake_case set. NO row means `active` — the default
///   is a rule ([`Store::get_tier`]), never a materialized row.
/// - `synonyms` (w2, derived): caller-fed alias pairs, both columns
///   normalized on write ([`fold_term`]). The composite PK makes re-adding
///   the same pair a no-op keeping the FIRST `at`. Droppable: the caller
///   is the source and re-teaches; open recreates it empty.
/// - `outcomes` (u6h, canonical): APPEND-ONLY outcome-observation records
///   ([`Store::append_outcome`]). `id` is `out-<seq>`; `description`/`actor`
///   NOT NULL (the caller names who observed — no default); `evidence_ref`
///   and `capsule_id` nullable. ADVISORY substrate — an observation record,
///   never a witnessed close; no verb updates or deletes a row.
/// - `preferences` (u6i, canonical): APPEND-ONLY pairwise preference-evidence
///   ([`Store::append_preference`]). `id` is `pref-<seq>`; both endpoint ids +
///   `context` + `actor` NOT NULL. Pairwise substrate for a FUTURE mechanism;
///   no scores, no aggregation, no update/delete verb.
const SIDECAR_SCHEMA: &str = "
CREATE UNIQUE INDEX IF NOT EXISTS idx_capsules_source_hash
    ON capsules (source_hash);
CREATE INDEX IF NOT EXISTS idx_capsules_project_id
    ON capsules (project_id);
CREATE INDEX IF NOT EXISTS idx_capsules_authority_class
    ON capsules (authority_class);
CREATE INDEX IF NOT EXISTS idx_capsules_valid_from
    ON capsules (valid_from);
CREATE INDEX IF NOT EXISTS idx_capsules_session_id
    ON capsules (session_id);
CREATE INDEX IF NOT EXISTS idx_relations_from
    ON relations (from_id);
CREATE INDEX IF NOT EXISTS idx_relations_to
    ON relations (to_id);
CREATE INDEX IF NOT EXISTS idx_classifications_kind
    ON classifications (kind);
CREATE INDEX IF NOT EXISTS idx_audit_events_subject
    ON audit_events (subject);
CREATE TABLE IF NOT EXISTS tiers (
    capsule_id TEXT PRIMARY KEY,
    tier       TEXT NOT NULL CHECK (tier IN ('active', 'archived', 'quarantined')),
    at         TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS synonyms (
    term  TEXT NOT NULL,
    alias TEXT NOT NULL,
    at    TEXT NOT NULL,
    PRIMARY KEY (term, alias)
);
CREATE TABLE IF NOT EXISTS tombstones (
    capsule_id        TEXT PRIMARY KEY,
    mode              TEXT NOT NULL CHECK (mode IN ('purged', 'redacted')),
    content_hmac      TEXT NOT NULL,
    at                TEXT NOT NULL,
    reason            TEXT NOT NULL,
    provenance_source TEXT,
    provenance_anchor TEXT,
    source_hash       TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
    session_id  TEXT PRIMARY KEY,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    summary     TEXT
);
CREATE TABLE IF NOT EXISTS outcomes (
    seq          INTEGER PRIMARY KEY,
    id           TEXT NOT NULL UNIQUE,
    description  TEXT NOT NULL,
    actor        TEXT NOT NULL,
    evidence_ref TEXT,
    capsule_id   TEXT,
    receipt_id   TEXT,
    score        REAL,
    at           TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS preferences (
    seq          INTEGER PRIMARY KEY,
    id           TEXT NOT NULL UNIQUE,
    preferred_id TEXT NOT NULL,
    rejected_id  TEXT NOT NULL,
    context      TEXT NOT NULL,
    actor        TEXT NOT NULL,
    at           TEXT NOT NULL
);
";

/// Derived FTS5 mirror of the capsules' `content` field (unit s4) — the
/// recall index, NEVER a second authority. Its rowid IS `capsules.seq`, so
/// a match joins back to the canonical row by sequence. Sync contract
/// (explicit transactional inserts, no triggers — every write stays
/// visible in Rust): [`Store::append`] inserts the mirror row inside the
/// same transaction as the canonical row; [`Store::forget_capsule`]
/// empties it in the same transaction as the tombstone;
/// [`Store::rebuild_fts`] re-derives the whole table; opening a store
/// whose mirror row count drifted from the canonical table re-derives it
/// on the spot.
///
/// `unicode61` is FTS5's default tokenizer, pinned explicitly so the index
/// shape never silently drifts with a bundled-SQLite upgrade.
const FTS_DDL: &str = "
CREATE VIRTUAL TABLE IF NOT EXISTS capsules_fts
    USING fts5(content, tokenize = 'unicode61');
";

/// Re-derivation of the mirror from the canonical table: `content` is
/// extracted from the authority column (`canonical_json`) via SQLite's
/// json1 — byte-identical to the string [`Store::append`] indexes, because
/// a JSON string round-trip is lossless. A tombstoned row (NULL
/// `canonical_json`) re-derives as the empty string: present for the
/// count-parity heal probe, unfindable by any term.
const FTS_POPULATE: &str = "
INSERT INTO capsules_fts (rowid, content)
    SELECT seq, COALESCE(json_extract(canonical_json, '$.content'), '')
    FROM capsules;
";

/// DERIVED usage sidecar (unit h4): per-capsule recall counters —
/// advisory ranking data only, never confidence/authority (ARCHITECTURE
/// §1 law: usage is not success evidence; §2: deleting `fts`+`usage`
/// loses nothing). `last_recalled_at` is always an INJECTED `now` — the
/// store reads no clock. Dropping the table resets counters harmlessly;
/// open recreates it empty. (`usage` is not an SQLite keyword.)
const USAGE_DDL: &str = "
CREATE TABLE IF NOT EXISTS usage (
    capsule_id       TEXT PRIMARY KEY,
    recall_count     INTEGER NOT NULL,
    last_recalled_at TEXT NOT NULL
);
";

/// CALLER-FED vector SIDECAR (w3 u6a) — one optional embedding per capsule,
/// PRIMARY KEY on `capsule_id` so a re-`put` REPLACES the row (replace-on-
/// write; the store never accumulates a vector history). The embedding is a
/// `dimension`-length `f32` vector stored as `dimension * 4` little-endian
/// bytes (`vector` blob); `dimension` is recorded alongside so decode is
/// self-describing and a corrupt-length blob is caught on read.
/// `model_tag` is the caller-declared provenance of the embedding (the u6a
/// provenance law — an embedding without a declared model is a fabrication
/// waiting to happen), never interpreted by the store. Hermetic laws this
/// table upholds: the store computes NO embedding (zero embedder
/// dependency, zero network) and reads NO clock (`at` is the injected
/// `now`); the vector is advisory recall fuel, never authority; dropping
/// the whole table loses no canonical byte (Capsule v1 is frozen, vectors
/// are a pure sidecar — [ARCHITECTURE §2 rung], same as `fts`/`usage`).
/// Additive and order-independent: a separate `IF NOT EXISTS` const so a
/// sibling lane adding its own table in the same wave never conflicts.
const EMBEDDINGS_DDL: &str = "
CREATE TABLE IF NOT EXISTS embeddings (
    capsule_id TEXT PRIMARY KEY,
    dimension  INTEGER NOT NULL,
    model_tag  TEXT NOT NULL,
    vector     BLOB NOT NULL,
    at         TEXT NOT NULL
);
";

/// CAPTURE-TIME anchored-file hash SIDECAR (u-r2 anchor-drift, schema v8).
/// `provenance.source_hash` is the hash of the capsule's own CONTENT bytes
/// (the ingest idempotency key — s3 policy), so it can never answer "did
/// the anchored FILE change?". This sidecar records the SHA-256 hex of the
/// anchored file's bytes at capture time — written once by the boundary
/// right after a fresh append, for `path:line` anchors that resolve
/// through the same fail-closed root fence the `anchor_live` probe uses
/// (symlinks, out-of-root, and non-path anchors record nothing). Recall
/// re-hashes the file and compares to answer `anchor_drift`
/// (`unchanged` / `drifted`); a capsule with no row here reads `unknown`
/// — no comparable hash, never a guess. Keep-first: the capture instant
/// is the ONLY honest comparison base, so re-recording is a no-op
/// (records, not columns — the relations discipline). Additive and
/// order-independent (`IF NOT EXISTS`, own const so a sibling lane never
/// conflicts); dropping it degrades every drift verdict to `unknown` and
/// loses no canonical byte (Capsule v1 stays frozen).
const ANCHOR_HASHES_DDL: &str = "
CREATE TABLE IF NOT EXISTS anchor_hashes (
    capsule_id TEXT PRIMARY KEY,
    hash       TEXT NOT NULL,
    at         TEXT NOT NULL
);
";

/// EPISTEMIC SIDECAR (u-r2, schema v8) — one OPTIONAL epistemic annotation
/// row per capsule, three independently optional fields:
///
/// - `evidence_state`: the closed set `observed` / `inferred` /
///   `unverified` (the module-doc claim ladder, persisted) — SQL CHECK +
///   [`EVIDENCE_STATES`] validation, typed rejection outside it.
/// - `proof_hint`: a short free string naming the command that RE-PROVES
///   the claim. ADVISORY DATA ONLY — no code path executes it, ever.
/// - `stale_if`: a short free string naming the condition under which the
///   claim expires. Same advisory-only law.
///
/// A SIBLING of `classifications`, not columns on it (recorded design
/// choice): a classification row carries NOT NULL `kind`+`scope`, so an
/// epistemic-only capsule would need a fabricated label. Upsert merges
/// PER FIELD — setting one field never erases a sibling field
/// ([`Store::set_epistemics`]). Additive `IF NOT EXISTS`; dropping it
/// loses only the annotations (Capsule v1 stays frozen).
const EPISTEMICS_DDL: &str = "
CREATE TABLE IF NOT EXISTS epistemics (
    capsule_id     TEXT PRIMARY KEY,
    evidence_state TEXT CHECK (evidence_state IN ('observed', 'inferred', 'unverified')),
    proof_hint     TEXT,
    stale_if       TEXT,
    at             TEXT NOT NULL
);
";

/// RECALL-MISS LEDGER SIDECAR (u-r5 miss-ledger, schema v9) — an
/// APPEND-ONLY telemetry ledger of the query terms that failed to ground:
/// misses teach vocabulary. Recall records ONE row per normalized (folded)
/// query term when the FTS term lane's pre-trim observation is
/// `missing_evidence` or `abstain` ([`Store::record_recall_miss`]); a
/// pre-trim term hit records nothing even if output trimming returns zero
/// envelopes, and a request that never ran FTS records nothing.
/// The term is folded exactly like [`Store::add_alias`]'s key
/// ([`fold_term`]: trim + lowercase + diacritic-fold), so a recorded miss
/// term is a ready alias LHS; the `outcome` is the closed
/// `missing_evidence` / `abstain` set (SQL CHECK + [`RecallMissOutcome`]).
/// One row per term (not per query): a query's terms are already
/// deduplicated upstream, so `COUNT(*) GROUP BY term` is the number of
/// missing queries that carried the term — the miss_count
/// [`crate::consolidate::alias_proposals`] orders by (deterministic, no
/// embedder). Recording is FAIL-OPEN telemetry: [`crate::retrieve`]
/// swallows a write failure so the ledger never fails or delays recall —
/// the deliberate exception to the crate's fail-closed default, sound only
/// because a lost miss row costs an advisory alias hint, never a canonical
/// byte. Additive and order-independent (`IF NOT EXISTS`, own const so a
/// sibling lane never conflicts); dropping it loses only the pending
/// vocabulary hints (Capsule v1 stays frozen).
const RECALL_MISSES_DDL: &str = "
CREATE TABLE IF NOT EXISTS recall_misses (
    seq     INTEGER PRIMARY KEY,
    term    TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('missing_evidence', 'abstain')),
    at      TEXT NOT NULL
);
";

/// RECALL-RECEIPT LEDGER SIDECAR (u03 recall receipts, schema v12) — an
/// APPEND-ONLY, GROUNDED-ONLY ledger that binds each public recall response
/// to the raw caller terms and the capsule ids actually returned, in response
/// order. Recording is FAIL-CLOSED: a returned `receipt_id` MUST resolve, in
/// deliberate contrast to the fail-open [`RECALL_MISSES_DDL`] telemetry whose
/// rows nothing references by id. `session_id` is nullable from birth; u13
/// supplies it. Dropping this additive table loses only feedback
/// addressability and never rewrites a canonical capsule byte.
const RECALL_RECEIPTS_DDL: &str = "
CREATE TABLE IF NOT EXISTS recall_receipts (
    seq            INTEGER PRIMARY KEY,
    id             TEXT NOT NULL UNIQUE,
    terms          TEXT NOT NULL,
    returned_ids   TEXT NOT NULL,
    project_id     TEXT,
    project_prefix TEXT,
    session_id     TEXT,
    at             TEXT NOT NULL
);
";

/// ADVISORY scored-outcome ranking sidecar (u04, schema v13). One row per
/// capsule stores the latest EMA weight and its injected update instant.
/// Dropping it loses only opt-in ranking feedback: capsule bytes and recall
/// eligibility remain untouched.
const FEEDBACK_WEIGHTS_DDL: &str = "
CREATE TABLE IF NOT EXISTS feedback_weights (
    capsule_id TEXT PRIMARY KEY,
    weight     REAL NOT NULL,
    at         TEXT NOT NULL
);
";

/// Successful explicit recall-lane overrides (u05, schema v14). This
/// append-only advisory telemetry records the forced lane and the lane the
/// auto policy would have chosen for the same successful request.
const LANE_OVERRIDES_DDL: &str = "
CREATE TABLE IF NOT EXISTS lane_overrides (
    seq       INTEGER PRIMARY KEY,
    forced    TEXT NOT NULL,
    auto_pick TEXT NOT NULL,
    at        TEXT NOT NULL,
    CHECK (
        (forced = 'term'   AND auto_pick = 'fused') OR
        (forced = 'vector' AND auto_pick = 'fused') OR
        (forced = 'vector' AND auto_pick = 'term')  OR
        (forced = 'fused'  AND auto_pick = 'term')
    )
);
";

/// Caller-declared fact time (u06, schema v15). One inclusive range per
/// capsule; a point is stored as the degenerate range `event_from ==
/// event_to`. `declared_at` is the same injected boundary instant as the
/// capsule append. The row is inserted inside the capsule + FTS transaction,
/// so a declaration can never lag its capsule. This is a per-store sidecar:
/// the merge primitive never moves its rows. Sync push restores only the
/// fetched destination's own verified rows into the private candidate, never
/// sender declarations.
const EVENT_TIME_DDL: &str = "
CREATE TABLE IF NOT EXISTS event_time (
    capsule_id  TEXT PRIMARY KEY,
    event_from  TEXT NOT NULL,
    event_to    TEXT NOT NULL,
    declared_at TEXT NOT NULL
);
";

/// STAGED-REVIEW SIDECAR (b2 staged ingests, schema v18) — the APPEND-ONLY
/// verdict history behind a capsule's review state (`proposed` → optional
/// `ratified`/`rejected` verdicts). Review state is ITS OWN orthogonal
/// sidecar, NEVER a tier value and NEVER a Capsule field (Capsule v1 is
/// frozen; tier is single-valued and consolidate re-tiers by taint — a
/// `proposed` tier value would be erased by a consolidate pass and become
/// un-ratifiable). The FENCE STATE is DERIVED, never stored: a capsule is
/// fenced from grounding IFF it carries at least one row here AND its LATEST
/// verdict is not `ratified` — a projection of this log, one source per fact
/// (NOTT.md §12), never a separately-cleared flag that can drift. Rejection
/// NEVER tombstones (that would deny the content to every future ingest —
/// `source_hash` is a global UNIQUE and forget is sticky); a later
/// `ratified` verdict reverses a `rejected` one. [`Store::consolidate`]
/// NEVER reads or writes this table — tier keeps its existing semantics.
/// Append-only: `seq` is assigned MAX+1 like the other ledgers; the index
/// serves the per-capsule latest-verdict projection. Additive and
/// order-independent (`IF NOT EXISTS`, own const so a sibling lane never
/// conflicts); dropping it loses only the review history and returns every
/// proposal to plain truth (no canonical byte moves — Capsule v1 stays
/// frozen).
const REVIEW_EVENTS_DDL: &str = "
CREATE TABLE IF NOT EXISTS review_events (
    seq        INTEGER PRIMARY KEY,
    capsule_id TEXT NOT NULL,
    verdict    TEXT NOT NULL CHECK (verdict IN ('proposed', 'ratified', 'rejected')),
    reason     TEXT NOT NULL,
    actor      TEXT NOT NULL,
    at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_review_events_capsule_id
    ON review_events (capsule_id);
";

/// PIN sidecar (S1, schema v16) — the APPEND-ONLY pin/unpin ledger. Pin
/// state is DERIVED: the highest-`seq` row per `capsule_id` is the current
/// verdict (`pinned` 0/1), never a mutated flag. `reason`/`actor` witness
/// the event that set it; `at` is the injected boundary instant. Pin is a
/// pure sidecar signal — it NEVER touches a capsule byte, tier, relation,
/// or the stored `confidence`. It affects only the retrieve/bootstrap decay
/// KEY (a pinned capsule ranks by full confidence, decay exempted at the
/// call site — [`crate::retrieve::decay_weight`] stays pure), the archive
/// veto ([`crate::consolidate`]), and surfacing/flags. Pin is NEVER
/// eligibility: the recall fences (quarantine/falsified/archive/superseded/
/// currency) run UNCHANGED, so a pinned+superseded capsule stays excluded.
/// Additive `IF NOT EXISTS`, own const so a sibling lane never conflicts;
/// dropping it loses no canonical byte (Capsule v1 stays frozen) and only
/// disables the decay-exemption/archive-veto (recorded audit rows survive).
const PIN_EVENTS_DDL: &str = "
CREATE TABLE IF NOT EXISTS pin_events (
    seq        INTEGER PRIMARY KEY,
    capsule_id TEXT NOT NULL,
    pinned     INTEGER NOT NULL CHECK (pinned IN (0, 1)),
    reason     TEXT NOT NULL,
    actor      TEXT NOT NULL,
    at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pin_events_capsule_id ON pin_events (capsule_id);
";

/// EMA step applied by scored outcomes.
pub const FEEDBACK_EMA_ALPHA: f64 = 0.1;
/// Prior used until a capsule receives its first scored outcome.
pub const FEEDBACK_NEUTRAL_WEIGHT: f64 = 0.5;

/// IMPORT-BLOCK LINEAGE SIDECAR (u-r8-REDESIGN stale-import-supersession,
/// schema v10) — the machine-derived-from-import lineage the auto-supersede
/// fence keys on. One row per `(source_key, block_hash)` the import
/// boundary has ADOPTED as machine-derived: `block_hash` IS the capsule's
/// `provenance.source_hash` (content identity, never a `path:line`
/// position — a position is advisory rendering only, never identity).
/// `source_key` is the source label + resolved file path (stable across
/// re-imports; a memory-dir import spans several files, each its own
/// source_key). `ordinal` is the block's 0-based position at adoption —
/// ADVISORY pairing order ONLY, never identity or change-detection:
/// whether a block changed is decided PURELY by content-hash set
/// difference.
///
/// MULTI-OWNER (bug 2, shared-block fix): the SAME `capsule_id` can carry
/// rows under SEVERAL DISTINCT `source_key`s when two sources share
/// byte-identical content — content-hash idempotency dedupes the second
/// source's block onto the first source's capsule, and BOTH source_keys
/// adopt it into lineage (see [`crate::server::MemoryServer::apply_import_supersession`]).
/// A capsule stays a live grounding capsule as long as ANY owning
/// source_key's row still names it; auto-supersede SKIPS a capsule that
/// still has a row under a source_key other than the one being
/// re-imported ([`Store::import_block_owners`]) — editing one file must
/// never bury content a sibling file still contains verbatim.
///
/// This sidecar IS the import `derived_from` lineage: a `relations` edge
/// cannot express it (both endpoints must be stored capsules and a file
/// source is not a capsule). Membership is the fence — a hand-ingested
/// capsule NEVER has a row here, so it can NEVER be auto-superseded or
/// auto-revived; the machine only rewrites/revives what the machine
/// adopted. The composite primary key `(source_key, block_hash)` makes
/// re-recording an unchanged block a keep-first no-op. Additive and
/// order-independent (`IF NOT EXISTS`, own const so a sibling lane never
/// conflicts); dropping it disables ONLY future auto-supersede/revive
/// (recorded `supersedes` edges survive) and loses no canonical byte
/// (Capsule v1 stays frozen).
const IMPORT_BLOCKS_DDL: &str = "
CREATE TABLE IF NOT EXISTS import_blocks (
    source_key TEXT NOT NULL,
    block_hash TEXT NOT NULL,
    capsule_id TEXT NOT NULL,
    ordinal    INTEGER NOT NULL,
    at         TEXT NOT NULL,
    PRIMARY KEY (source_key, block_hash)
);
CREATE INDEX IF NOT EXISTS idx_import_blocks_capsule_id
    ON import_blocks (capsule_id);
";

/// GIT-WITNESS CORROBORATION SIDECAR (S2 git witness lane, schema v17) — an
/// APPEND-ONLY ledger of what an external witness (currently only `git`)
/// observed about a stored capsule. Each row is ONE verdict at one scan:
/// `kind` names WHAT was probed (`anchor_path` / `anchor_sha` /
/// `anchor_content` existence-or-hash checks, or a `mention` of the capsule
/// in a commit message), `ref` the probed reference (the anchor path, the
/// `@sha`, or the citing commit sha), `verdict` the closed observation, and
/// `detail` optional context (the scan `HEAD` the verdict was observed at).
/// There is NO `unknown` verdict on purpose: a probe that cannot answer
/// records NOTHING (the [`ANCHOR_HASHES_DDL`] "never a guess" rule). Read =
/// LATEST per (capsule_id, kind, ref) — [`Store::latest_corroborations`];
/// the git-scan verb ([`crate::git::scan`]) appends only when a verdict
/// CHANGES, so re-scanning an unchanged tree writes zero rows. This is a
/// WITNESS lane, NEVER a truth lane: nothing here mutates a capsule's stored
/// `confidence`, authority, or tier. Additive and order-independent
/// (`IF NOT EXISTS`, own const so a sibling lane never conflicts); dropping
/// it loses only the derived corroboration explain (Capsule v1 stays
/// frozen).
const CORROBORATIONS_DDL: &str = "
CREATE TABLE IF NOT EXISTS corroborations (
  seq INTEGER PRIMARY KEY, capsule_id TEXT NOT NULL,
  source TEXT NOT NULL CHECK (source IN ('git')),
  kind TEXT NOT NULL CHECK (kind IN ('anchor_path','anchor_sha','anchor_content','mention')),
  ref TEXT NOT NULL, verdict TEXT NOT NULL CHECK (verdict IN ('corroborated','drifted','missing')),
  detail TEXT, at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_corroborations_capsule_id ON corroborations (capsule_id);
";

/// GIT-WITNESS SCAN CURSOR SIDECAR (S2 git witness lane, schema v17) — the
/// incremental-scan bookmark: one row per witness source
/// (`source_key = "git:<canonical repo path>"`) holding the last scanned
/// commit sha (`cursor`) and the instant it advanced (`at`). The git-scan
/// mention lane pages from this completed frontier to a fixed target recorded
/// in [`SOURCE_BACKFILLS_DDL`]; the cursor advances only when that range is
/// complete. PRIMARY KEY on `source_key` — the cursor is REPLACED in place,
/// never accumulated. Additive and disposable: dropping it re-does a full
/// mention scan (idempotent by the corroboration dedup) and loses no canonical
/// byte.
const SOURCE_CURSORS_DDL: &str = "
CREATE TABLE IF NOT EXISTS source_cursors (
  source_key TEXT PRIMARY KEY, cursor TEXT NOT NULL, at TEXT NOT NULL);
";

/// In-progress bounded mention-history traversal for one git witness source.
/// `source_cursors` remains the last fully consumed frontier; this sidecar
/// pins a target head and the next newest-first offset until the complete
/// range has been consumed.
const SOURCE_BACKFILLS_DDL: &str = "
CREATE TABLE IF NOT EXISTS source_backfills (
  source_key TEXT PRIMARY KEY,
  base_cursor TEXT,
  target_head TEXT NOT NULL,
  next_offset INTEGER NOT NULL CHECK (next_offset > 0),
  at TEXT NOT NULL);
";

fn validate_source_backfill_fields(
    source_key: &str,
    base_cursor: Option<&str>,
    target_head: &str,
    next_offset: usize,
) -> Result<(), StoreError> {
    if source_key.trim().is_empty() {
        return Err(StoreError::EmptyField("source_key"));
    }
    if base_cursor.is_some_and(|cursor| cursor.trim().is_empty()) {
        return Err(StoreError::EmptyField("base_cursor"));
    }
    if target_head.trim().is_empty() {
        return Err(StoreError::EmptyField("target_head"));
    }
    if next_offset == 0 {
        return Err(StoreError::Corrupt {
            id: source_key.to_string(),
            reason: "source backfill next offset must be positive".to_string(),
        });
    }
    Ok(())
}

/// A witness's LATEST observations about ONE capsule (S2 git witness lane;
/// see [`CORROBORATIONS_DDL`]), folded to the newest verdict per anchor kind
/// plus a mention tally — the derived explain the retrieve envelope
/// decorates a returned row with. NEVER authority.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CorroborationSummary {
    /// The witness source (currently only `"git"`).
    pub source: String,
    /// The scan `HEAD` the newest verdict was observed at, when recorded.
    pub git_ref: Option<String>,
    /// Latest `anchor_path` verdict, when the path was probed.
    pub anchor_path: Option<String>,
    /// Latest `anchor_sha` verdict, when an `@sha` anchor was probed.
    pub anchor_sha: Option<String>,
    /// Latest `anchor_content` verdict, when a capture hash was re-checked.
    pub anchor_content: Option<String>,
    /// How many distinct commit mentions cite this capsule.
    pub mentions: usize,
    /// The newest `at` across the folded rows.
    pub at: String,
}

/// Store-global corroboration tallies for ONE witness source (S2 git witness
/// lane), counting the LATEST verdict per (capsule_id, kind, ref) — the
/// digest `sources` section's per-source counts.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct CorroborationCounts {
    /// Anchors whose latest verdict is `corroborated`.
    pub corroborated: usize,
    /// Anchor-content rows whose latest verdict is `drifted`.
    pub drifted: usize,
    /// Anchors whose latest verdict is `missing`.
    pub missing: usize,
    /// Mention rows (each a distinct capsule+commit citation).
    pub mentions: usize,
}

/// One incomplete bounded git-history traversal. The completed source cursor
/// does not advance until this fixed target has been fully consumed.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SourceBackfill {
    /// Last fully consumed frontier when this traversal began.
    pub base_cursor: Option<String>,
    /// Fixed head whose `base_cursor..target_head` range is being paged.
    pub target_head: String,
    /// Number of newest commits already processed from that fixed range.
    pub next_offset: usize,
    /// Timestamp of the last checkpoint update.
    pub at: String,
}

fn source_cursor_on(conn: &Connection, source_key: &str) -> Result<Option<String>, StoreError> {
    conn.query_row(
        "SELECT cursor FROM source_cursors WHERE source_key = ?1",
        [source_key],
        |row| row.get(0),
    )
    .optional()
    .map_err(backend)
}

fn source_backfill_on(
    conn: &Connection,
    source_key: &str,
) -> Result<Option<SourceBackfill>, StoreError> {
    conn.query_row(
        "SELECT base_cursor, target_head, next_offset, at \
         FROM source_backfills WHERE source_key = ?1",
        [source_key],
        |row| {
            Ok((
                row.get::<_, Option<String>>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, i64>(2)?,
                row.get::<_, String>(3)?,
            ))
        },
    )
    .optional()
    .map_err(backend)?
    .map(|(base_cursor, target_head, next_offset, at)| {
        let next_offset = usize::try_from(next_offset).map_err(|_| StoreError::Corrupt {
            id: source_key.to_string(),
            reason: format!("source_backfills.next_offset {next_offset} is not a usize"),
        })?;
        if next_offset == 0 {
            return Err(StoreError::Corrupt {
                id: source_key.to_string(),
                reason: "source_backfills.next_offset must be positive".to_string(),
            });
        }
        Ok(SourceBackfill {
            base_cursor,
            target_head,
            next_offset,
            at,
        })
    })
    .transpose()
}

fn require_source_cursor(
    conn: &Connection,
    source_key: &str,
    expected: Option<&str>,
) -> Result<(), StoreError> {
    if source_cursor_on(conn, source_key)?.as_deref() != expected {
        return Err(StoreError::StaleSourceBackfill(source_key.to_string()));
    }
    Ok(())
}

/// Encode an `f32` vector as its deterministic little-endian byte blob
/// (`4 × len` bytes) — the exact bytes [`Store::put_embedding`] persists.
/// Paired with [`decode_embedding`] for a bit-exact round-trip: IEEE-754
/// bytes are preserved verbatim, so `decode(encode(v), v.len()) == v` for
/// every finite vector, on every host (no float formatting, no endianness
/// drift).
fn encode_embedding(vector: &[f32]) -> Vec<u8> {
    let mut bytes = Vec::with_capacity(vector.len() * 4);
    for value in vector {
        bytes.extend_from_slice(&value.to_le_bytes());
    }
    bytes
}

/// Decode a little-endian `f32` blob back to its vector, validating that
/// the byte length is exactly `dimension × 4`. A corrupt or short blob is a
/// typed [`StoreError::Corrupt`] — never a silent truncation or a panic.
fn decode_embedding(id: &str, blob: &[u8], dimension: usize) -> Result<Vec<f32>, StoreError> {
    let expected = dimension
        .checked_mul(4)
        .ok_or_else(|| StoreError::Corrupt {
            id: id.to_string(),
            reason: format!("embeddings.dimension {dimension} overflows a byte length"),
        })?;
    if blob.len() != expected {
        return Err(StoreError::Corrupt {
            id: id.to_string(),
            reason: format!(
                "embeddings.vector is {} bytes, expected {expected} (dimension {dimension} x 4)",
                blob.len()
            ),
        });
    }
    let mut out = Vec::with_capacity(dimension);
    // `chunks_exact(4)` yields only full 4-byte chunks; the exact-length
    // check above guarantees no remainder, so the array conversion is
    // infallible (the `?` funnel keeps it total without unwrap/expect).
    for chunk in blob.chunks_exact(4) {
        let arr: [u8; 4] = chunk.try_into().map_err(|_| StoreError::Corrupt {
            id: id.to_string(),
            reason: "embeddings.vector chunk was not 4 bytes".to_string(),
        })?;
        out.push(f32::from_le_bytes(arr));
    }
    Ok(out)
}

/// Validate a caller-fed embedding at the storage boundary: non-empty,
/// every component finite (no NaN/±inf), and non-zero magnitude. Cosine
/// similarity is undefined for an empty, non-finite, or zero-norm vector,
/// so these are refused here ([`StoreError::InvalidEmbedding`]) rather than
/// allowed to poison the deterministic RRF fusion downstream (u6a hermetic
/// law: no NaN reaches recall).
fn validate_embedding(vector: &[f32]) -> Result<(), StoreError> {
    if vector.is_empty() {
        return Err(StoreError::InvalidEmbedding(
            "vector is empty (dimension 0)".to_string(),
        ));
    }
    if let Some(bad) = vector.iter().position(|v| !v.is_finite()) {
        return Err(StoreError::InvalidEmbedding(format!(
            "component {bad} is not finite (NaN or +/-inf)"
        )));
    }
    let sum_sq: f64 = vector.iter().map(|v| f64::from(*v) * f64::from(*v)).sum();
    if sum_sq == 0.0 {
        return Err(StoreError::InvalidEmbedding(
            "vector has zero magnitude (all components zero)".to_string(),
        ));
    }
    Ok(())
}

/// On-disk schema version stamped into `PRAGMA user_version`. Version 2
/// (unit w1) added the sidecar plane: nullable `canonical_json` +
/// `session_id` on `capsules`, generalized `relations`, and the
/// `audit_events` / `classifications` / `tombstones` / `sessions` tables.
/// Version 3 (unit w2-store2) added `tiers`, `synonyms`, and the
/// `audit_events.chained_hash` journal chain (backfilled deterministically
/// for pre-v3 audit rows). Version 4 (w2-kinds landing) widened the
/// `classifications.kind` CHECK to the seven-kind set (table rebuild —
/// SQLite cannot ALTER a CHECK). Version 5 (w3 u6a vector sidecar) added
/// the additive `embeddings` table — a caller-fed per-capsule vector
/// SIDECAR (`IF NOT EXISTS`, no canonical byte touched; Capsule v1 stays
/// frozen, a dropped `embeddings` table loses no authority). Version 6
/// (u6h/u6i substrates) widened the `relations.kind` CHECK to add
/// `falsifies` (the same table-rebuild discipline) and added the
/// append-only `outcomes` / `preferences` sidecar tables (additive,
/// `IF NOT EXISTS`). Version 7 (u-r11 kind-vocabulary) widened the
/// `classifications.kind` CHECK to the ten-kind set — the three
/// governance kinds `constraint` / `capability` / `failure_pattern`
/// (the same table-rebuild discipline). Version 8 (u-r2 anchor-drift +
/// epistemic sidecar) added the additive `anchor_hashes` / `epistemics`
/// tables ([`ANCHOR_HASHES_DDL`] / [`EPISTEMICS_DDL`] — `IF NOT EXISTS`,
/// no canonical byte touched). Version 9 (u-r5 miss-ledger) added the
/// additive append-only `recall_misses` table ([`RECALL_MISSES_DDL`] —
/// `IF NOT EXISTS`, no canonical byte touched). Version 10 (u-r8-REDESIGN
/// stale-import-supersession) added the additive `import_blocks` lineage
/// sidecar ([`IMPORT_BLOCKS_DDL`] — `IF NOT EXISTS`, no canonical byte
/// touched). Version 11 (store-merge u2) added the additive nullable
/// `tombstones.source_hash` column: the forgotten capsule's content
/// identity, so a forget can propagate cross-store by content
/// ([`crate::merge`]); a pre-v11 marker backfills NULL and simply cannot
/// propagate by content (acceptable). Version 12 (u03 recall receipts) added
/// the additive append-only `recall_receipts` ledger
/// ([`RECALL_RECEIPTS_DDL`] — `IF NOT EXISTS`, no canonical byte touched).
/// Version 13 (u04 scored outcomes) added nullable `outcomes.receipt_id` /
/// `outcomes.score` columns plus the additive advisory `feedback_weights`
/// sidecar ([`FEEDBACK_WEIGHTS_DDL`] — `IF NOT EXISTS`, no canonical byte
/// touched). Version 14 (u05 lane router) added the append-only advisory
/// `lane_overrides` telemetry sidecar ([`LANE_OVERRIDES_DDL`] — `IF NOT
/// EXISTS`, no canonical byte touched). Version 15 (u06 event time) added
/// the caller-declared [`EVENT_TIME_DDL`] sidecar. Version 16 (S1 pin) added
/// the append-only [`PIN_EVENTS_DDL`] pin/unpin ledger — a pure sidecar
/// signal (decay-exemption + archive-veto + surfacing; `IF NOT EXISTS`, no
/// canonical byte touched). Version 17 (S2 git witness lane) added the
/// append-only, order-independent [`CORROBORATIONS_DDL`] and
/// [`SOURCE_CURSORS_DDL`] sidecars (`IF NOT EXISTS`, no canonical byte
/// touched). Version 18 (b2 staged review) added the append-only
/// [`REVIEW_EVENTS_DDL`] sidecar (additive `IF NOT EXISTS`, no canonical byte
/// touched) and widened the `relations.kind` CHECK to add `proposes` — a
/// shared-DDL table rebuild (`relations_v6`, probed on the stored CHECK text
/// via [`relations_missing_proposes_check`], the SAME no-drift discipline as
/// the `falsifies` rebuild) whose copy PRESERVES the `origin` column so
/// import provenance survives byte-for-byte. The current v18 shape also
/// carries the disposable [`SOURCE_BACKFILLS_DDL`] checkpoint so a bounded
/// git scan cannot advance past unseen history. Version 19 (effort-lifecycle
/// s1) widened the `relations.kind` CHECK again to add `part_of` (pure
/// membership; NEVER a dag input) — the SAME shared-DDL rebuild, FOLDED into
/// the v18 `proposes` rebuild so a store missing EITHER token (a `proposes`
/// store lacking `part_of` OR an out-of-order `part_of` store lacking
/// `proposes`) converges to the seven-kind set in ONE pass, `origin`
/// preserved; probed on the stored CHECK text via
/// [`relations_missing_part_of_check`], newest-token `'part_of'`. Version 20
/// (planning-plane s1) widened the `relations.kind` CHECK a third time to add
/// `grounded_in` (the mission-anchoring kind; also NEVER a dag input) — the
/// SAME shared-DDL rebuild extended to a THREE-token disjunction, so a store
/// missing ANY of `proposes` / `part_of` / `grounded_in` converges to the
/// EIGHT-kind set in ONE pass; the shadow table moves to `relations_v7` (the
/// next free name after the v19 fold's `relations_v6`), probed on the stored
/// CHECK text via [`relations_lacks_grounded_in`], `origin` preserved
/// byte-for-byte. Version 1–19 files migrate
/// in place via [`migrate_to_current`];
/// versions this build does not know fail closed
/// ([`StoreError::UnsupportedSchemaVersion`]). Every migration step keys on
/// the observed DDL shape (`relations_has_old_check` /
/// `relations_missing_proposes_check` / `relations_missing_part_of_check` /
/// `relations_lacks_grounded_in` / `classifications_has_old_check`) or
/// `IF NOT EXISTS`, never on the
/// version integer, so the stamp renumbers mechanically when lanes land
/// out of authoring order.
const SCHEMA_VERSION: i64 = 20;

/// Milliseconds a connection waits for a held write lock before giving up
/// with `SQLITE_BUSY`. Concurrent sessions on one store — the owner runs
/// two machines against the same `--db` over SSH, "one store, both machines
/// live on the same memory" — then wait briefly for the in-flight writer
/// instead of dying immediately with "database is locked". Effective only
/// paired with up-front write-lock acquisition (`BEGIN IMMEDIATE`, the
/// connection default set in [`Store::from_connection`]): a DEFERRED
/// transaction that reads before it writes is refused a lock upgrade at
/// once and never reaches this wait.
const BUSY_TIMEOUT_MS: i64 = 5000;

/// Domain-separation tag for the tombstone content HMAC: a value computed
/// here can never be replayed as any other HMAC-SHA-256 use keyed on the
/// same key (donor `fingerprint.rs` discipline).
const TOMBSTONE_HMAC_DOMAIN_TAG: &[u8] = b"nmemory-tombstone-hmac-v1";

/// Errors crossing the store boundary. Backend (SQLite) failures arrive
/// stringified so no `rusqlite` type leaks through the API.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum StoreError {
    /// Backend (SQL / I-O) failure, stringified.
    #[error("store backend error: {0}")]
    Backend(String),
    /// Persisted bytes failed re-validation on read.
    #[error("store: row {id} is corrupt: {reason}")]
    Corrupt {
        /// Id of the offending row.
        id: String,
        /// What failed to decode or validate.
        reason: String,
    },
    /// A capsule with this `provenance.source_hash` is already stored —
    /// the UNIQUE backstop behind ingest idempotency (s3).
    #[error("store: source_hash {0} already stored (idempotency backstop)")]
    DuplicateSourceHash(String),
    /// Canonical serialization of a write failed (capsule canonical JSON,
    /// snapshot line, or an RFC3339 timestamp).
    #[error("store: canonical serialization failed: {0}")]
    Serialize(String),
    /// The file's `PRAGMA user_version` names a schema this build does not
    /// know — fail closed instead of guessing at columns.
    #[error(
        "store: unsupported schema version {0} (this build migrates v1..=18 in place and reads v{SCHEMA_VERSION} natively)"
    )]
    UnsupportedSchemaVersion(i64),
    /// A relation/classification endpoint named a capsule id that is not
    /// stored — nothing was recorded.
    #[error("store: operation references unknown capsule {0}")]
    UnknownCapsule(String),
    /// A scored outcome named a recall receipt that was never recorded.
    #[error("store: operation references unknown recall receipt {0}")]
    UnknownReceipt(String),
    /// Both endpoints of a relation named the same capsule: no kind is
    /// reflexive (donor B law — a capsule cannot supersede, derive from,
    /// witness, or block itself).
    #[error("store: capsule {id} cannot be in a '{kind}' relation with itself")]
    SelfRelation {
        /// The rejected reflexive kind.
        kind: RelationKind,
        /// The capsule named on both ends.
        id: String,
    },
    /// The typed forget marker: this id names a capsule whose content was
    /// removed by [`Store::forget_capsule`]. The content is gone — only
    /// the tombstone record ([`Store::get_tombstone`]) remains.
    #[error("store: capsule {id} is tombstoned; the content is gone, only the marker remains")]
    Tombstoned {
        /// Id of the forgotten capsule.
        id: String,
    },
    /// [`Store::forget_capsule`] requires a non-empty reason — a forget
    /// without a stated reason is not recordable (donor CAP-13 law).
    #[error("store: forget requires a non-empty reason")]
    EmptyReason,
    /// A required text field was empty (audit actor/action/subject,
    /// session_id, synonym term/alias — for synonyms: empty AFTER
    /// [`fold_term`] normalization).
    #[error("store: {0} must be non-empty")]
    EmptyField(&'static str),
    /// A git history page attempted to advance a checkpoint that no longer
    /// matches the fixed target and offset it read.
    #[error("store: stale git source backfill checkpoint for {0}")]
    StaleSourceBackfill(String),
    /// A classification value fell outside its closed set
    /// ([`CLASSIFICATION_KINDS`] / [`CLASSIFICATION_SCOPES`]).
    #[error("store: classification {field} {value:?} is outside the closed set")]
    InvalidClassification {
        /// Which field was rejected (`"kind"` or `"scope"`).
        field: &'static str,
        /// The rejected value.
        value: String,
    },
    /// A session operation named a `session_id` that was never opened.
    #[error("store: unknown session {0}")]
    UnknownSession(String),
    /// [`Store::open_session`] on a `session_id` that already exists —
    /// session ids are unique, a bracket opens once.
    #[error("store: session {0} already exists (session_id is unique)")]
    DuplicateSession(String),
    /// The session is already finished: it cannot be finished again and
    /// cannot accept new captures (bracketing honesty).
    #[error("store: session {0} is already finished")]
    SessionFinished(String),
    /// [`Store::verify_chain`] found a row whose `chained_hash` does not
    /// match the recomputation — the journal was tampered with (a row
    /// edited in place, or a mid-ledger row removed). Names the FIRST
    /// broken sequence number; everything before it is verified intact.
    #[error("store: audit journal hash chain broken at seq {seq}")]
    JournalBroken {
        /// The first audit `seq` whose stored hash fails recomputation.
        seq: i64,
    },
    /// [`Store::add_alias`] with term == alias after [`fold_term`]
    /// normalization: a synonym must name a DIFFERENT word — a self-alias
    /// row would be pure noise in every expansion.
    #[error("store: alias equals its term {term:?} after folding — a synonym must differ")]
    SelfAlias {
        /// The folded term both sides collapsed to.
        term: String,
    },
    /// A caller-fed embedding failed validation on [`Store::put_embedding`]:
    /// empty, carrying a non-finite (NaN/±inf) component, or of zero
    /// magnitude. Cosine similarity is undefined for these, so the store
    /// refuses to persist a vector that could never ground an honest recall
    /// (u6a hermetic law: no NaN may reach the deterministic fusion).
    #[error("store: embedding rejected: {0}")]
    InvalidEmbedding(String),
    /// A scored outcome omitted half of its receipt/score pair or supplied
    /// a non-finite or out-of-range score.
    #[error("store: outcome scoring rejected: {0}")]
    InvalidOutcomeScoring(&'static str),
    /// An `evidence_state` fell outside the closed set — the message
    /// TEACHES the whole set ([`EVIDENCE_STATES`]), so a rejected caller
    /// learns the vocabulary in one round-trip (u-r2).
    #[error(
        "store: evidence_state {0:?} is outside the closed set \
         \"observed\" | \"inferred\" | \"unverified\""
    )]
    InvalidEvidenceState(String),
}

/// A shape rejection from the pure substrate constructors
/// ([`crate::substrate`]) crosses the store boundary as the same
/// [`StoreError::EmptyField`] the audit/alias paths use — one empty-field
/// vocabulary store-wide.
impl From<SubstrateError> for StoreError {
    fn from(e: SubstrateError) -> StoreError {
        match e {
            SubstrateError::EmptyField(field) => StoreError::EmptyField(field),
            SubstrateError::InvalidOutcomeScoring(reason) => {
                StoreError::InvalidOutcomeScoring(reason)
            }
        }
    }
}

/// One committed outcome append and its optional post-EMA weights. Unscored
/// observations carry `None`; a scored receipt that returned no capsules
/// carries `Some([])` so the wire can distinguish those two honest states.
#[derive(Debug, Clone, PartialEq)]
pub struct AppendedOutcome {
    /// The committed append-only outcome record.
    pub record: OutcomeRecord,
    /// Post-EMA weights in the grounded receipt's original response order.
    pub weights_updated: Option<Vec<(String, f64)>>,
}

/// Store-assigned deterministic capsule id: `cap-<seq>` with `<seq>` the
/// 1-based append sequence (`cap-1`, `cap-2`, …). The store is the only
/// mint — there is no public constructor; serde exists for the canonical
/// snapshot / replay tooling, never as a caller-supplied authority.
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(transparent)]
pub struct CapsuleId(String);

impl CapsuleId {
    /// The id as text (`"cap-<seq>"`), e.g. for [`Store::get`].
    #[must_use]
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl fmt::Display for CapsuleId {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

/// A capsule as persisted: store-assigned identity + append position + the
/// validated capsule + the INJECTED creation instant. Field declaration
/// order here IS the canonical snapshot line order — do not reorder.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct StoredCapsule {
    /// Store-assigned deterministic id (`cap-<seq>`).
    pub id: CapsuleId,
    /// 1-based append sequence (the determinism spine).
    pub seq: i64,
    /// The capsule, re-validated on read via serde's `try_from` funnel.
    pub capsule: Capsule,
    /// Creation instant exactly as injected at append time — the store
    /// never reads a wall clock.
    #[serde(with = "time::serde::rfc3339")]
    pub created_at: OffsetDateTime,
    /// Session bracketing link (v2 `capsules.session_id` sidecar column;
    /// the Capsule JSON itself is untouched) — `None` for captures outside
    /// any session and for every pre-v2 row. Skipped from the canonical
    /// snapshot line when absent, so pre-session snapshot bytes are
    /// unchanged; when present it serializes deterministically (it is
    /// append input, not derived state).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub session_id: Option<String>,
}

/// A caller-declared inclusive fact-time range. The fields are private so a
/// backwards range cannot cross the store boundary; [`EventTimeRange::new`]
/// is the single validating constructor. A point is represented by equal
/// bounds.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct EventTimeRange {
    event_from: OffsetDateTime,
    event_to: OffsetDateTime,
}

impl EventTimeRange {
    /// Construct a point event as an equal-bounds range.
    #[must_use]
    pub const fn point(at: OffsetDateTime) -> Self {
        Self {
            event_from: at,
            event_to: at,
        }
    }

    /// Construct an inclusive range. Rejects `event_to < event_from`.
    pub fn new(
        event_from: OffsetDateTime,
        event_to: OffsetDateTime,
    ) -> Result<Self, EventTimeRangeError> {
        if event_to < event_from {
            return Err(EventTimeRangeError {
                event_from,
                event_to,
            });
        }
        Ok(Self {
            event_from,
            event_to,
        })
    }

    /// Inclusive start of the declared event range.
    #[must_use]
    pub const fn event_from(&self) -> OffsetDateTime {
        self.event_from
    }

    /// Inclusive end of the declared event range.
    #[must_use]
    pub const fn event_to(&self) -> OffsetDateTime {
        self.event_to
    }
}

/// A backwards caller-declared event range. Kept distinct from
/// [`StoreError`] because it is a pre-persistence value error; persisted
/// backwards rows surface as [`StoreError::Corrupt`] on read.
#[derive(Debug, Clone, Copy, PartialEq, Eq, thiserror::Error)]
#[error("event_to {event_to} lies before event_from {event_from} — an event range runs forward")]
pub struct EventTimeRangeError {
    event_from: OffsetDateTime,
    event_to: OffsetDateTime,
}

/// One validated row from [`EVENT_TIME_DDL`]. Private fields preserve the
/// same closed-range invariant after persistence; callers use accessors.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct EventTimeRecord {
    range: EventTimeRange,
    declared_at: OffsetDateTime,
}

impl EventTimeRecord {
    /// Inclusive event-range start.
    #[must_use]
    pub const fn event_from(&self) -> OffsetDateTime {
        self.range.event_from()
    }

    /// Inclusive event-range end.
    #[must_use]
    pub const fn event_to(&self) -> OffsetDateTime {
        self.range.event_to()
    }

    /// Injected instant when this declaration was first stored.
    #[must_use]
    pub const fn declared_at(&self) -> OffsetDateTime {
        self.declared_at
    }
}

/// The closed set of review verdicts (b2 staged review). `proposed` is the
/// birth verdict of a staged capsule; `ratified` / `rejected` remain readable
/// for compatible history and authority-bearing internal consumers. The
/// standalone connector exposes no CLOSE verb. Wire names are the exact SQL
/// CHECK strings ([`REVIEW_EVENTS_DDL`]).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ReviewVerdict {
    /// A staged capsule's birth verdict — fenced from grounding.
    Proposed,
    /// Promoted to plain truth at its tier — no longer fenced.
    Ratified,
    /// A verdict of rejection — stays fenced; NEVER tombstones, and a later
    /// `ratified` reverses it.
    Rejected,
}

impl ReviewVerdict {
    /// The persisted/wire word — exactly the SQL CHECK set.
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            ReviewVerdict::Proposed => "proposed",
            ReviewVerdict::Ratified => "ratified",
            ReviewVerdict::Rejected => "rejected",
        }
    }

    /// Parse a stored/wire verdict; `None` for anything outside the closed
    /// set — the caller turns that into a fail-closed [`StoreError::Corrupt`]
    /// (the CHECK makes it near-unreachable, but a corrupt file is decidable).
    #[must_use]
    pub fn from_wire(text: &str) -> Option<Self> {
        match text {
            "proposed" => Some(ReviewVerdict::Proposed),
            "ratified" => Some(ReviewVerdict::Ratified),
            "rejected" => Some(ReviewVerdict::Rejected),
            _ => None,
        }
    }
}

/// One append-only review-verdict row (b2 staged review): the verdict, the
/// caller's reason, the recorded actor (the boundary `clientInfo.name`), and
/// the injected instant.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ReviewEvent {
    /// The recorded verdict.
    pub verdict: ReviewVerdict,
    /// Why the verdict was recorded (non-empty).
    pub reason: String,
    /// Who recorded it — boundary knowledge, never store-minted.
    pub actor: String,
    /// Injected instant — the store reads no clock.
    pub at: OffsetDateTime,
}

/// A capsule's DERIVED review state (b2 staged review): its full append-only
/// verdict history plus the projected latest verdict. [`ReviewState::fenced`]
/// is the ONE grounding-fence rule — a capsule is fenced from recall IFF it
/// carries review history AND its latest verdict is not `ratified` — never a
/// stored flag (one source per fact). Present (`Some`) whenever ANY review
/// row exists, so a ratified (live) proposal stays auditable here.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ReviewState {
    latest: ReviewVerdict,
    history: Vec<ReviewEvent>,
}

impl ReviewState {
    /// The projected standing verdict — the latest row's verdict.
    #[must_use]
    pub const fn latest(&self) -> ReviewVerdict {
        self.latest
    }

    /// Whether this capsule is fenced from grounding: it carries review
    /// history and the latest verdict is not `ratified`.
    #[must_use]
    pub const fn fenced(&self) -> bool {
        !matches!(self.latest, ReviewVerdict::Ratified)
    }

    /// The append-only verdict history, oldest first.
    #[must_use]
    pub fn history(&self) -> &[ReviewEvent] {
        &self.history
    }
}

/// Usage counters for one capsule (h4 sidecar) — DERIVED advisory data
/// for a LATE ranking tiebreak only; it never touches confidence or
/// authority (ARCHITECTURE §1 law: usage is not success evidence).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct UsageStat {
    /// How many times recall has returned this capsule.
    pub recall_count: i64,
    /// Instant of the most recent recall — exactly the INJECTED `now` of
    /// that [`Store::record_recall`] call; the store reads no clock.
    pub last_recalled_at: OffsetDateTime,
}

/// The DERIVED pin state of one capsule (S1, schema v16) — the highest-`seq`
/// row from [`PIN_EVENTS_DDL`]. `pinned` is the current verdict; the other
/// fields witness the event that set it. Produced by [`Store::pin_state_of`];
/// [`Store::is_pinned`] is the hot-path boolean the decay key reads.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PinRecord {
    /// The capsule this pin state is about (`cap-<n>`).
    pub capsule_id: String,
    /// Current pin verdict — `true` pinned, `false` unpinned.
    pub pinned: bool,
    /// Why the latest event set this state (non-empty).
    pub reason: String,
    /// Who set it (non-empty).
    pub actor: String,
    /// When the latest event was recorded (RFC3339).
    pub at: String,
}

/// A caller-fed embedding as stored (w3 u6a sidecar) — the full vector plus
/// its recorded dimension and `model_tag` provenance. Advisory recall fuel,
/// never authority; one per capsule (replace-on-write).
#[derive(Debug, Clone, PartialEq)]
pub struct StoredEmbedding {
    /// Capsule this vector is attached to (`cap-<seq>`).
    pub capsule_id: String,
    /// Vector length — equals `vector.len()`; recorded so decode is
    /// self-describing and a wrong-length blob is caught on read.
    pub dimension: usize,
    /// Caller-declared provenance of the embedding (the u6a provenance
    /// law) — opaque to the store.
    pub model_tag: String,
    /// The embedding, decoded from the deterministic little-endian `f32`
    /// blob (bit-exact round-trip of what the caller `put`).
    pub vector: Vec<f32>,
}

/// One row of the [`Store::list_embeddings`] index — the metadata a caller
/// enumerates without pulling every vector's bytes (the vector itself stays
/// one [`Store::get_embedding`] away).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EmbeddingRow {
    /// Capsule the embedding is attached to (`cap-<seq>`).
    pub capsule_id: String,
    /// Recorded vector length.
    pub dimension: usize,
    /// Caller-declared provenance.
    pub model_tag: String,
}

/// The eight declared relation kinds (donor B closed enum — mcps/memory-
/// contract `relation.rs`). Wire names are the snake_case forms; adding a
/// kind is a deliberate, reviewed change to the public ontology. Each edge
/// reads `from --kind--> to`:
///
/// | kind | `from_id` | `to_id` |
/// |---|---|---|
/// | `supersedes` | the newer capsule | the replaced one |
/// | `derived_from` | the derivative | its origin |
/// | `witnesses` | the evidence capsule | the attested capsule |
/// | `blocks` | the blocker | the blocked |
/// | `falsifies` | an outcome `out-<n>` OR a capsule | the falsified capsule |
/// | `proposes` | the proposing capsule | the target it proposes to replace |
/// | `part_of` | the member capsule | the container epic/task |
/// | `grounded_in` | the child work | its parent epic |
///
/// `falsifies` (u6h) is unique: its `from_id` may name an OUTCOME record
/// (`out-<n>`, [`Store::append_outcome`]) as well as a capsule, and its
/// target becomes recall-ineligible (a fence in `crate::retrieve`, not a
/// state change — [`Store::is_falsified`]). `proposes` (b2 staged review) is
/// navigational only — no dag or recall effect. `part_of` (effort-lifecycle
/// s1) is pure membership — `from` is a member of container `to`; like
/// `falsifies` and `proposes` it is NOT a dag input.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum RelationKind {
    /// `from` replaces `to` (the replace-over-append discipline).
    Supersedes,
    /// `from` was materialized out of `to`.
    DerivedFrom,
    /// `from` is evidence attesting `to`.
    Witnesses,
    /// `from` blocks `to` (the dag/blocked_by projection input).
    Blocks,
    /// `from` (an outcome `out-<n>` or a capsule) falsifies capsule `to`:
    /// the target stops grounding recall (eligibility fence), its bytes
    /// untouched. NOT a dag input.
    Falsifies,
    /// `from` PROPOSES to replace `to` (b2 staged review) — NAVIGATIONAL
    /// ONLY: no dag/ready/done effect and no recall-exclusion effect. The
    /// staged-import path (S5b) records it new-proposal→incumbent so a
    /// changed source never silently supersedes; on ratification the caller
    /// MAY convert it to `supersedes` explicitly, the machine never does.
    Proposes,
    /// `from` is a member of container `to` (an epic/task): pure
    /// membership. NOT a dag input.
    PartOf,
    /// `from` (the child task/epic/plan node) hangs off `to` (its parent
    /// epic) — the planning-plane anchor surfaced by `memory_digest`'s
    /// mission section. NOT a dag input.
    GroundedIn,
}

impl RelationKind {
    /// All declared kinds, in contract order.
    pub const ALL: [RelationKind; 8] = [
        RelationKind::Supersedes,
        RelationKind::DerivedFrom,
        RelationKind::Witnesses,
        RelationKind::Blocks,
        RelationKind::Falsifies,
        RelationKind::Proposes,
        RelationKind::PartOf,
        RelationKind::GroundedIn,
    ];

    /// The wire name, e.g. `"derived_from"` — exactly the SQL CHECK set.
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            RelationKind::Supersedes => "supersedes",
            RelationKind::DerivedFrom => "derived_from",
            RelationKind::Witnesses => "witnesses",
            RelationKind::Blocks => "blocks",
            RelationKind::Falsifies => "falsifies",
            RelationKind::Proposes => "proposes",
            RelationKind::PartOf => "part_of",
            RelationKind::GroundedIn => "grounded_in",
        }
    }

    /// Parse a wire name back to its kind; `None` for anything outside the
    /// closed set.
    #[must_use]
    pub fn from_wire(text: &str) -> Option<Self> {
        Self::ALL.into_iter().find(|kind| kind.as_str() == text)
    }
}

impl fmt::Display for RelationKind {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

/// One directed, typed capsule-to-capsule edge as persisted. `at` is the
/// INJECTED `now` of the recording call (first record wins on re-record).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RelationRecord {
    /// The edge's kind (closed set).
    pub kind: RelationKind,
    /// Source endpoint (`from --kind--> to`).
    pub from_id: String,
    /// Target endpoint.
    pub to_id: String,
    /// Instant the edge was recorded — injected, never a store clock.
    pub at: OffsetDateTime,
    /// Who wrote the edge (u-r8 round 3) — first write wins on re-record.
    pub origin: RelationOrigin,
}

/// Who wrote a relation edge (u-r8 round 3, closed set): `manual` — a
/// caller decision (memory_relate, an ingest `supersedes`, any surface
/// verb) — is NEVER machine-reversed; `import` — written by the
/// stale-import supersession mechanism, the ONLY edges
/// [`Store::unsupersede`] may delete. The provenance lives on the edge
/// itself so eligibility is decided by "did THIS mechanism write it?",
/// never by transient pass state.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RelationOrigin {
    /// A caller-recorded edge — human/agent authority, machine-untouchable.
    Manual,
    /// Written by stale-import supersession — machine-reversible.
    Import,
}

impl RelationOrigin {
    /// The persisted wire word (matches the SQL CHECK).
    pub fn as_str(self) -> &'static str {
        match self {
            RelationOrigin::Manual => "manual",
            RelationOrigin::Import => "import",
        }
    }
}

/// One append-only audit ledger row. Every mutation is expected to be
/// audited by its call site (module-level audit policy); the store only
/// holds the ledger.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AuditEvent {
    /// 1-based append sequence within the audit ledger.
    pub seq: i64,
    /// Instant of the event — injected, never a store clock.
    pub at: OffsetDateTime,
    /// Who acted (boundary knowledge — e.g. `"session:2026-07-18"`).
    pub actor: String,
    /// What was done (e.g. `"memory.ingest"`, `"memory.forget"`).
    pub action: String,
    /// What it was done to (typically a capsule or session id).
    pub subject: String,
    /// Optional free-text why.
    pub reason: Option<String>,
    /// Journal chain link (v3): `sha256(prev_hash + canonical line)` —
    /// hex, derived at append time, re-verifiable via
    /// [`Store::verify_chain`].
    pub chained_hash: String,
}

/// The closed classification kinds (mirrors the extract/classify
/// vocabulary as plain strings — deliberately decoupled from the
/// `extract.rs` types; the SQL CHECK enforces the same set, and the
/// cross-copy parity test in `server.rs` pins all four copies —
/// `extract::CandidateKind::ALL` ↔ the server's `CandidateKindParam` ↔
/// this const ↔ the CHECK — to land atomically). w2-kinds extended the
/// set with the four work/docs-plane kinds; u-r11 appended the three
/// governance kinds (`proof`/`outcome` are DELIBERATE non-kinds —
/// witnesses edges + provenance are the proof, and outcomes are the
/// `out-<n>` record class).
pub const CLASSIFICATION_KINDS: [&str; 10] = [
    "fact",
    "procedure",
    "decision",
    "task",
    "epic",
    "brainstorm",
    "doc",
    "constraint",
    "capability",
    "failure_pattern",
];

/// The closed classification scopes (same decoupling as
/// [`CLASSIFICATION_KINDS`]).
pub const CLASSIFICATION_SCOPES: [&str; 3] = ["project", "global", "session"];

/// One capsule's classification label (at most one per capsule;
/// [`Store::set_classification`] upserts).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ClassificationRecord {
    /// A member of [`CLASSIFICATION_KINDS`].
    pub kind: String,
    /// A member of [`CLASSIFICATION_SCOPES`].
    pub scope: String,
    /// Instant of the (latest) classification — injected.
    pub at: OffsetDateTime,
}

/// The closed `evidence_state` vocabulary of the epistemic sidecar (u-r2):
/// how the capsule's claim relates to observation. Mirrors the SQL CHECK
/// in [`EPISTEMICS_DDL`]; [`Store::set_epistemics`] rejects anything else
/// with the teaching [`StoreError::InvalidEvidenceState`].
pub const EVIDENCE_STATES: [&str; 3] = ["observed", "inferred", "unverified"];

/// The closed outcome set a term-lane recall miss records (u-r5
/// miss-ledger): `missing_evidence` or `abstain` as observed BEFORE output
/// trimming. A pre-trim term hit and a request that never ran FTS have no
/// variant here and record nothing. Wire names match the response tags
/// exactly and the SQL CHECK in [`RECALL_MISSES_DDL`], so an illegal outcome
/// is unrepresentable at the type layer before the CHECK ever sees it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RecallMissOutcome {
    /// Terms matched stored capsules but every match was fenced out.
    MissingEvidence,
    /// Zero raw term-lane matches — the honest term miss.
    Abstain,
}

impl RecallMissOutcome {
    /// The wire name (`"missing_evidence"` / `"abstain"`) — exactly the
    /// SQL CHECK set and the retrieve response tags.
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            RecallMissOutcome::MissingEvidence => "missing_evidence",
            RecallMissOutcome::Abstain => "abstain",
        }
    }

    /// Parse the persisted wire name back into the closed outcome set.
    /// Anything else is corrupt store data, never an extensible string.
    #[must_use]
    pub fn from_wire(text: &str) -> Option<Self> {
        match text {
            "missing_evidence" => Some(Self::MissingEvidence),
            "abstain" => Some(Self::Abstain),
            _ => None,
        }
    }
}

/// One typed recall-miss ledger row. The reader re-validates every field
/// even for a database already stamped at the current schema version, so a
/// hand-shaped table cannot turn the digest into an open-string channel.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RecallMissRow {
    /// Positive append sequence; newest-first readers order by this field.
    pub seq: i64,
    /// Non-empty canonical folded term (the exact key persisted by the writer).
    pub term: String,
    /// Closed pre-trim term-lane miss outcome.
    pub outcome: RecallMissOutcome,
    /// Parsed RFC3339 recording instant.
    pub at: OffsetDateTime,
}

/// A successful explicit recall-lane choice that differs from the auto
/// policy's choice for the same request. The four variants are the complete
/// disagreement set; equal lanes and `auto` as a forced value are
/// unrepresentable at the Rust writer boundary and rejected by
/// [`LANE_OVERRIDES_DDL`] at the persistence boundary.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum LaneOverride {
    /// Explicit term instead of auto-fused.
    TermOverFused,
    /// Explicit vector instead of auto-fused.
    VectorOverFused,
    /// Explicit vector instead of auto-term.
    VectorOverTerm,
    /// Explicit fused instead of auto-term.
    FusedOverTerm,
}

impl LaneOverride {
    /// Every representable successful disagreement. Readers use this same
    /// closed set to re-validate persisted rows rather than trusting that a
    /// hand-shaped current-version table preserved the SQL CHECK.
    const ALL: [Self; 4] = [
        Self::TermOverFused,
        Self::VectorOverFused,
        Self::VectorOverTerm,
        Self::FusedOverTerm,
    ];

    /// The exact `(forced, auto_pick)` pair persisted by the telemetry
    /// writer. This is the sole Rust source of those strings.
    const fn pair(self) -> (&'static str, &'static str) {
        match self {
            LaneOverride::TermOverFused => ("term", "fused"),
            LaneOverride::VectorOverFused => ("vector", "fused"),
            LaneOverride::VectorOverTerm => ("vector", "term"),
            LaneOverride::FusedOverTerm => ("fused", "term"),
        }
    }

    /// Whether persisted wire strings name one member of the closed typed
    /// disagreement set.
    fn contains_pair(forced: &str, auto_pick: &str) -> bool {
        Self::ALL
            .into_iter()
            .any(|override_| override_.pair() == (forced, auto_pick))
    }
}

/// One capsule's epistemic annotations (at most one row per capsule;
/// [`Store::set_epistemics`] merges per field). Every payload field is
/// independently optional; a returned record carries at least one `Some`
/// (an all-`None` write records nothing). `proof_hint` and `stale_if` are
/// ADVISORY STRINGS — no code path executes or interprets them, ever.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EpistemicsRecord {
    /// A member of [`EVIDENCE_STATES`], when set.
    pub evidence_state: Option<String>,
    /// The command that re-proves the claim — advisory, never executed.
    pub proof_hint: Option<String>,
    /// The condition under which the claim expires — advisory, never
    /// evaluated.
    pub stale_if: Option<String>,
    /// Instant of the latest epistemic write — injected.
    pub at: OffsetDateTime,
}

/// One LIVE import-block lineage row (u-r8-REDESIGN stale-import-supersession;
/// see [`IMPORT_BLOCKS_DDL`]): a capsule the import boundary has ADOPTED as
/// machine-derived from a `(source_key, block_hash)` pair. `block_hash` IS
/// the capsule's `provenance.source_hash` (the identity — content, never
/// position); `ordinal` is advisory document-order for pairing only. A
/// capsule may be named by rows under several DISTINCT source_keys
/// (multi-owner, bug 2) — [`Store::import_block_owners`] reads the set of
/// owners across every source_key.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ImportBlockRow {
    /// SHA-256 hex of the block's content bytes — the capsule's
    /// `provenance.source_hash`, and the block's identity.
    pub block_hash: String,
    /// The machine-derived capsule id (`cap-<seq>`).
    pub capsule_id: String,
    /// 0-based block position at adoption — ADVISORY pairing order only.
    pub ordinal: i64,
}

/// How a capsule was forgotten. Both modes NULL the content column and
/// keep the row's id/provenance skeleton (the derived filter columns —
/// which carry hashes and labels, never content bytes); the mode records
/// the caller's intent. (Delta from donor B, where `purged` also dropped
/// the skeleton: the v2 row IS the skeleton, and dropping the row would
/// break the append-sequence determinism spine.)
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TombstoneMode {
    /// Hard forget — content removed, nothing about it should be inferred.
    Purged,
    /// Content scrubbed, provenance deliberately retained for audit.
    Redacted,
}

impl TombstoneMode {
    /// The wire name (`"purged"` / `"redacted"`) — exactly the SQL CHECK
    /// set.
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            TombstoneMode::Purged => "purged",
            TombstoneMode::Redacted => "redacted",
        }
    }

    /// Parse a wire name back to its mode; `None` outside the closed set.
    #[must_use]
    pub fn from_wire(text: &str) -> Option<Self> {
        match text {
            "purged" => Some(TombstoneMode::Purged),
            "redacted" => Some(TombstoneMode::Redacted),
            _ => None,
        }
    }
}

impl fmt::Display for TombstoneMode {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

/// Lifecycle tier of a capsule (w2 `tiers` sidecar — closed set, snake_case
/// in the SQL CHECK). The tier is ADVISORY lifecycle state about the
/// RECORD: a ranking/visibility input for the engine, never authority and
/// never a content mutation. Every capsule is `Active` until a caller says
/// otherwise — the default is a rule, not a row ([`Store::get_tier`]).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Tier {
    /// Normal working memory — the default for every capsule.
    Active,
    /// Consolidated/cold: kept, but a consumer may down-rank or skip it.
    Archived,
    /// Suspect (e.g. failed a taint review): kept, flagged for isolation.
    Quarantined,
}

impl Tier {
    /// All declared tiers, in contract order.
    pub const ALL: [Tier; 3] = [Tier::Active, Tier::Archived, Tier::Quarantined];

    /// The wire name (`"active"` / `"archived"` / `"quarantined"`) —
    /// exactly the SQL CHECK set.
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Tier::Active => "active",
            Tier::Archived => "archived",
            Tier::Quarantined => "quarantined",
        }
    }

    /// Parse a wire name back to its tier; `None` outside the closed set.
    #[must_use]
    pub fn from_wire(text: &str) -> Option<Self> {
        Self::ALL.into_iter().find(|tier| tier.as_str() == text)
    }
}

impl fmt::Display for Tier {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

/// What remains of a forgotten capsule: the marker, never the content.
/// `content_hmac` is `"hmac-sha256:" + hex` of a KEYED digest of the former
/// content — correlatable by someone holding the key, irreversible for
/// everyone (and unmatchable in bulk without the key).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TombstoneRecord {
    /// The forgotten capsule's id.
    pub capsule_id: String,
    /// How it was forgotten.
    pub mode: TombstoneMode,
    /// Keyed HMAC-SHA-256 of the removed content (`hmac-sha256:<hex>`).
    pub content_hmac: String,
    /// Instant of the forget — injected.
    pub at: OffsetDateTime,
    /// The mandatory stated reason.
    pub reason: String,
    /// The retained `provenance.source` — populated ONLY for mode
    /// `redacted` (its documented purpose: provenance kept for audit);
    /// `None` for `purged`.
    pub provenance_source: Option<String>,
    /// The retained `provenance.anchor` — same `redacted`-only rule.
    pub provenance_anchor: Option<String>,
    /// The forgotten capsule's `provenance.source_hash` (content identity),
    /// recorded for BOTH modes from v11 — a HASH, never the removed bytes.
    /// It lets a forget propagate cross-store by content ([`crate::merge`]):
    /// an incoming marker matches a LOCAL live capsule of the same content.
    /// `None` for a pre-v11 marker (backfilled), which cannot propagate that
    /// way.
    pub source_hash: Option<String>,
}

/// Deterministic tally of one [`Store::merge_from`] apply. Every count
/// derives PURELY from the applied [`crate::merge::MergePlan`], so merging
/// identical stores yields an identical summary.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct MergeSummary {
    /// Genuinely-new capsules appended to LOCAL (the plan's new capsules).
    pub capsules_added: usize,
    /// Incoming capsules that COLLAPSED onto existing LOCAL content by
    /// `source_hash` (content dedup) — each contributed no new row.
    pub capsules_collapsed: usize,
    /// New relation edges inserted (remapped, deduped, danglers dropped).
    pub relations_added: usize,
    /// Forget-wins tombstones APPLIED — a LOCAL live capsule the incoming
    /// side had forgotten (matched by content) is forgotten locally too.
    pub tombstones_applied: usize,
    /// Size of the incoming-id -> LOCAL-id remap (collapsed + newly minted).
    pub id_remap_size: usize,
}

/// The full outcome of a [`Store::merge_from`] apply: the wire [`MergeSummary`]
/// plus the LOCAL ids the merge touched, so the caller can AUDIT each one.
/// Every mutation is audited by its call site (module audit policy), and
/// [`crate::journal::verify_replay`] coverage requires every live capsule to
/// be an audit subject and every tombstone a recognized forget event — so the
/// surface audits one row per added capsule and one per propagated forget,
/// exactly as `memory_ingest`/`memory_forget` audit per affected id.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MergeApplied {
    /// The deterministic count summary (the wire shape).
    pub summary: MergeSummary,
    /// LOCAL ids of the capsules this merge appended, ascending — each MUST
    /// be audited (subject = the id) or replay coverage flags it out-of-band.
    pub added_ids: Vec<String>,
    /// LOCAL ids forgotten by forget-wins propagation — each MUST be audited
    /// with a recognized forget action (subject = the id).
    pub forgotten_ids: Vec<String>,
}

/// One session bracketing record. `finished_at`/`summary` are `None` until
/// [`Store::finish_session`] closes the bracket.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SessionRecord {
    /// Caller-chosen unique session id.
    pub session_id: String,
    /// Instant the bracket opened — injected.
    pub started_at: OffsetDateTime,
    /// Instant the bracket closed; `None` while the session is open.
    pub finished_at: Option<OffsetDateTime>,
    /// Optional close-time summary.
    pub summary: Option<String>,
}

/// Store-local state of one exact session label in the activity projection.
/// A label can outlive or never have a local bracket because merge copies
/// capsule labels but deliberately does not import [`SessionRecord`] rows.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SessionLabelState {
    /// A local bracket exists and has no `finished_at`.
    Open,
    /// A local bracket exists and carries a `finished_at`.
    Closed,
    /// Capsules and/or receipts carry the label, but no local bracket exists.
    LabelOnly,
}

/// One exact-label row in [`Store::session_activity`]. Counts are physical
/// store rows: `saves` includes retained tombstone skeletons and `recalls`
/// counts grounded receipt rows, never ids inside `returned_ids`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SessionActivityRow {
    /// Character-exact store-local label.
    pub session_id: String,
    /// Capsule rows carrying the label, including tombstone skeletons.
    pub saves: usize,
    /// Grounded recall receipt rows carrying the label.
    pub recalls: usize,
    /// Local bracket state, or [`SessionLabelState::LabelOnly`].
    pub state: SessionLabelState,
}

/// Filter for [`Store::list`]. `Default` = everything. Present fences
/// AND-compose: a row must pass every fence that is `Some`.
#[derive(Debug, Clone, Default)]
pub struct ListFilter {
    /// Keep only capsules whose `scope.project_id` equals this.
    pub project_id: Option<String>,
    /// Keep at most this many rows (applied after the project fences, in
    /// append order).
    pub limit: Option<usize>,
    /// Scope-hierarchy fence (w2): keep capsules whose `scope.project_id`
    /// equals this prefix exactly OR starts with it + `"/"` — `"nott"`
    /// covers `nott` and `nott/x`, never `nottx`. The same matching rule
    /// backs [`Store::search_fts_scoped`]. Character-exact: no glob, no
    /// case folding.
    pub project_prefix: Option<String>,
}

/// Single-file SQLite store. Writes take `&mut self` — single-writer by
/// contract (the donor-proven determinism discipline); reads take `&self`.
#[derive(Debug)]
pub struct Store {
    conn: Connection,
}

#[cfg(test)]
thread_local! {
    /// S2 effort-lifecycle read-count guard: how many times [`Store::all_relations`]
    /// ran on THIS thread. Thread-local so the count is private to the
    /// current `#[tokio::test]` (each runs its handler on its own thread) and
    /// never races a parallel test. Compiled out of release builds.
    pub(crate) static ALL_RELATIONS_READS: std::cell::Cell<usize> =
        const { std::cell::Cell::new(0) };
    /// #156-c read-count guard: how many times [`Store::list`] ran on THIS
    /// thread. Same thread-local discipline as [`ALL_RELATIONS_READS`] — the
    /// redundant-scan fix means an unfenced digest/bootstrap call reuses its
    /// already-loaded capsule list instead of a second full-store `list()`,
    /// so this stays a cheap regression guard on that perf contract.
    pub(crate) static STORE_LIST_READS: std::cell::Cell<usize> =
        const { std::cell::Cell::new(0) };
}

impl Store {
    /// Open a store at `path`, creating the file and schema if absent. Any
    /// older known version migrates in place ([`migrate_to_current`]); a
    /// file stamped with a version this build does not know fails
    /// closed.
    pub fn open(path: &Path) -> Result<Self, StoreError> {
        let conn = Connection::open(path).map_err(backend)?;
        Self::from_connection(conn)
    }

    /// Open an ephemeral in-memory store (tests, dry runs).
    pub fn open_in_memory() -> Result<Self, StoreError> {
        let conn = Connection::open_in_memory().map_err(backend)?;
        Self::from_connection(conn)
    }

    fn from_connection(mut conn: Connection) -> Result<Self, StoreError> {
        // The wait budget for a held write lock: with the up-front lock
        // acquisition set just below, a concurrent same-store session waits
        // out the peer's write instead of failing immediately with "database
        // is locked" (see [`BUSY_TIMEOUT_MS`]). Set before WAL and
        // schema-init so open-time contention is covered too. The pragma
        // answers with the resulting timeout as a row, so query it.
        let _timeout: i64 = conn
            .query_row(
                &format!("PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}"),
                [],
                |row| row.get(0),
            )
            .map_err(backend)?;
        // Every store transaction WRITES, and each reads before it writes
        // (append checks for a duplicate; migrate probes the schema). Under a
        // peer's held write lock a DEFERRED transaction would try to UPGRADE
        // a read to a write, which SQLite refuses at once with SQLITE_BUSY
        // and WITHOUT invoking the busy handler (deadlock avoidance) — so the
        // timeout above would never engage. Taking the write lock up front
        // (BEGIN IMMEDIATE) keeps the busy handler in play, so a concurrent
        // session waits for the lock rather than dying "database is locked".
        // Connection default: every `conn.transaction()` here inherits it.
        conn.set_transaction_behavior(TransactionBehavior::Immediate);
        // WAL for file-backed durability semantics; the pragma answers with
        // the resulting mode as a row, so query it rather than execute it.
        // In-memory databases report their own mode and are unaffected.
        let _mode: String = conn
            .query_row("PRAGMA journal_mode = WAL", [], |row| row.get(0))
            .map_err(backend)?;
        // Forget honesty: zero freed cells/pages on delete and update, so
        // content removed by `forget_capsule` does not linger in free
        // space. Connection-scoped; answers with the resulting value.
        let _secure: i64 = conn
            .query_row("PRAGMA secure_delete = ON", [], |row| row.get(0))
            .map_err(backend)?;
        // Fail closed BEFORE touching any table: an unknown version is not
        // ours to modify.
        let version: i64 = conn
            .query_row("PRAGMA user_version", [], |row| row.get(0))
            .map_err(backend)?;
        // Every migratable version enumerated EXPLICITLY (w2-store2
        // lesson: leaning on the const silently rejects older files
        // after a bump).
        match version {
            0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 | 12 | 13 | 14 | 15 | 16 | 17 | 18
            | 19 | SCHEMA_VERSION => migrate_to_current(&mut conn)?,
            other => return Err(StoreError::UnsupportedSchemaVersion(other)),
        }
        // Derived-table heal: the mirror must cover the canonical table
        // (a pre-fts file, or an externally dropped mirror, opens empty
        // over existing capsules — recall would silently abstain). A row
        // count delta is the drift this probe can see; full re-derivation
        // is always available via [`Store::rebuild_fts`].
        let (canonical_rows, indexed_rows): (i64, i64) = conn
            .query_row(
                "SELECT (SELECT count(*) FROM capsules), \
                        (SELECT count(*) FROM capsules_fts)",
                [],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .map_err(backend)?;
        if canonical_rows != indexed_rows {
            rebuild_fts_on(&mut conn)?;
        }
        Ok(Store { conn })
    }

    /// Open the store at `path` READ-ONLY — the INCOMING side of a merge
    /// ([`Store::merge_from`]). Reads its core rows without migrating,
    /// healing, or touching a single byte: no `CREATE` flag (a missing file
    /// fails closed, never spawns an empty store), no WAL/secure-delete
    /// pragma (those write), no [`migrate_to_current`]. A non-SQLite or
    /// corrupt file surfaces on the first read as a typed
    /// [`StoreError::Backend`]/[`StoreError::Corrupt`]. The schema must be
    /// the CURRENT version this build reads natively — an older source is
    /// [`StoreError::UnsupportedSchemaVersion`] (migrate it by opening it
    /// read-write with this build first; a read-only handle cannot migrate).
    /// Only the `&self` read methods are safe on the returned handle; a
    /// write would fail at the SQLite layer (this is a private merge helper,
    /// never handed a write path).
    fn open_readonly(path: &Path) -> Result<Self, StoreError> {
        let conn = Connection::open_with_flags(
            path,
            OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
        )
        .map_err(backend)?;
        let version: i64 = conn
            .query_row("PRAGMA user_version", [], |row| row.get(0))
            .map_err(backend)?;
        if version != SCHEMA_VERSION {
            return Err(StoreError::UnsupportedSchemaVersion(version));
        }
        Ok(Store { conn })
    }

    /// Append a validated capsule; returns the store-assigned id
    /// (`cap-<seq>`). `now` is the surface-boundary instant persisted as
    /// `created_at` — injected, because the store itself never reads a
    /// clock.
    ///
    /// A capsule whose `provenance.source_hash` is already stored is
    /// rejected with [`StoreError::DuplicateSourceHash`] and nothing is
    /// written (the append-level idempotency backstop; ingest pre-checks
    /// via [`Store::find_by_source_hash`]). The backstop covers tombstoned
    /// rows too: a forgotten capture cannot silently resurrect.
    ///
    /// The FTS5 mirror row is inserted in the same transaction — the
    /// recall index can never lag the canonical table.
    pub fn append(
        &mut self,
        capsule: &Capsule,
        now: OffsetDateTime,
    ) -> Result<CapsuleId, StoreError> {
        self.append_inner(capsule, None, None, now)
    }

    /// [`Store::append`] with one caller-declared fact-time range. The
    /// event row is inserted after the capsule and FTS rows but before the
    /// SAME transaction commits. There is deliberately no post-append
    /// event-time writer: a declaration is fresh-capture input only.
    pub fn append_with_event_time(
        &mut self,
        capsule: &Capsule,
        event_time: &EventTimeRange,
        now: OffsetDateTime,
    ) -> Result<CapsuleId, StoreError> {
        self.append_inner(capsule, None, Some(event_time), now)
    }

    /// [`Store::append`] with a session bracketing link: the capsule row's
    /// `session_id` sidecar column is set (the Capsule JSON is untouched).
    /// The session must exist and still be open — captures into an unknown
    /// ([`StoreError::UnknownSession`]) or finished
    /// ([`StoreError::SessionFinished`]) bracket are rejected before
    /// anything is written.
    pub fn append_with_session(
        &mut self,
        capsule: &Capsule,
        session_id: &str,
        now: OffsetDateTime,
    ) -> Result<CapsuleId, StoreError> {
        let finished: Option<Option<String>> = self
            .conn
            .query_row(
                "SELECT finished_at FROM sessions WHERE session_id = ?1",
                [session_id],
                |row| row.get(0),
            )
            .optional()
            .map_err(backend)?;
        match finished {
            None => Err(StoreError::UnknownSession(session_id.to_string())),
            Some(Some(_)) => Err(StoreError::SessionFinished(session_id.to_string())),
            Some(None) => self.append_inner(capsule, Some(session_id), None, now),
        }
    }

    /// [`Store::append_with_session`] plus caller-declared fact time. The
    /// session gate runs before the append and the fact-time row then shares
    /// the capsule + FTS transaction.
    pub fn append_with_session_and_event_time(
        &mut self,
        capsule: &Capsule,
        session_id: &str,
        event_time: &EventTimeRange,
        now: OffsetDateTime,
    ) -> Result<CapsuleId, StoreError> {
        let finished: Option<Option<String>> = self
            .conn
            .query_row(
                "SELECT finished_at FROM sessions WHERE session_id = ?1",
                [session_id],
                |row| row.get(0),
            )
            .optional()
            .map_err(backend)?;
        match finished {
            None => Err(StoreError::UnknownSession(session_id.to_string())),
            Some(Some(_)) => Err(StoreError::SessionFinished(session_id.to_string())),
            Some(None) => self.append_inner(capsule, Some(session_id), Some(event_time), now),
        }
    }

    fn append_inner(
        &mut self,
        capsule: &Capsule,
        session_id: Option<&str>,
        event_time: Option<&EventTimeRange>,
        now: OffsetDateTime,
    ) -> Result<CapsuleId, StoreError> {
        let canonical_json = capsule
            .to_canonical_json()
            .map_err(|e| StoreError::Serialize(e.to_string()))?;
        let created_at = rfc3339_text(now)?;
        let valid_from = rfc3339_text(capsule.freshness().valid_from)?;
        let authority_class = authority_class_text(capsule.authority_class())?;

        let tx = self.conn.transaction().map_err(backend)?;
        let seq: i64 = tx
            .query_row(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM capsules",
                [],
                |row| row.get(0),
            )
            .map_err(backend)?;
        let id = format!("cap-{seq}");
        tx.execute(
            "INSERT INTO capsules \
             (seq, id, canonical_json, created_at, source_hash, project_id, \
              authority_class, valid_from, session_id) \
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)",
            params![
                seq,
                id,
                canonical_json,
                created_at,
                capsule.provenance().source_hash,
                capsule.scope().project_id,
                authority_class,
                valid_from,
                session_id
            ],
        )
        .map_err(|e| map_unique_source_hash(e, &capsule.provenance().source_hash))?;
        tx.execute(
            "INSERT INTO capsules_fts (rowid, content) VALUES (?1, ?2)",
            params![seq, capsule.content()],
        )
        .map_err(backend)?;
        if let Some(event_time) = event_time {
            let event_from = rfc3339_text(event_time.event_from())?;
            let event_to = rfc3339_text(event_time.event_to())?;
            tx.execute(
                "INSERT INTO event_time (capsule_id, event_from, event_to, declared_at) \
                 VALUES (?1, ?2, ?3, ?4)",
                params![id, event_from, event_to, created_at],
            )
            .map_err(backend)?;
        }
        tx.commit().map_err(backend)?;
        Ok(CapsuleId(id))
    }

    /// Fetch one capsule by id (`"cap-<n>"`); `Ok(None)` when absent. A
    /// forgotten capsule returns the typed [`StoreError::Tombstoned`]
    /// marker — never the content, never a silent `None`
    /// ([`Store::get_tombstone`] has the full marker record).
    pub fn get(&self, id: &str) -> Result<Option<StoredCapsule>, StoreError> {
        self.conn
            .query_row(
                "SELECT id, seq, canonical_json, created_at, session_id \
                 FROM capsules WHERE id = ?1",
                [id],
                row_to_raw,
            )
            .optional()
            .map_err(backend)?
            .map(RawRow::decode)
            .transpose()
    }

    /// Read a capsule's caller-declared fact-time sidecar. `Ok(None)` means
    /// no declaration was made. Every timestamp and the forward-range
    /// invariant are re-validated on read so a hand-shaped current-version
    /// row fails as [`StoreError::Corrupt`] rather than fabricating time.
    pub fn event_time_of(&self, id: &str) -> Result<Option<EventTimeRecord>, StoreError> {
        let row: Option<(String, String, String)> = self
            .conn
            .query_row(
                "SELECT event_from, event_to, declared_at \
                 FROM event_time WHERE capsule_id = ?1",
                [id],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )
            .optional()
            .map_err(backend)?;
        let Some((event_from_text, event_to_text, declared_at_text)) = row else {
            return Ok(None);
        };
        let event_from = parse_at(id, "event_time.event_from", &event_from_text)?;
        let event_to = parse_at(id, "event_time.event_to", &event_to_text)?;
        let declared_at = parse_at(id, "event_time.declared_at", &declared_at_text)?;
        let range = EventTimeRange::new(event_from, event_to).map_err(|_| StoreError::Corrupt {
            id: id.to_string(),
            reason: format!(
                "event_time.event_to {event_to_text:?} lies before \
                 event_time.event_from {event_from_text:?}"
            ),
        })?;
        Ok(Some(EventTimeRecord { range, declared_at }))
    }

    /// Write a transactionally consistent SQLite snapshot to
    /// `destination_path` from this LIVE connection. SQLite's online backup
    /// API reads the connection's committed database state, including pages
    /// still resident in WAL because another connection remains open; copying
    /// only the main database file cannot provide that guarantee.
    ///
    /// The caller owns the destination path and phase-specific error mapping.
    ///
    /// # Errors
    /// Returns [`StoreError::Backend`] when SQLite cannot create or complete
    /// the snapshot.
    pub fn snapshot_to(&self, destination_path: &Path) -> Result<(), StoreError> {
        self.conn
            .backup(MAIN_DB, destination_path, None)
            .map_err(backend)
    }

    /// Rebase a private sync push candidate onto the destination store's
    /// fact-time declarations. The candidate already carries the merged
    /// core rows and historical whole-file push state; this method replaces
    /// ONLY its `event_time` table. Every destination declaration is decoded,
    /// validated, and rebound by the capsule's unique content identity
    /// (`source_hash`) before one transaction deletes any candidate row.
    ///
    /// This is deliberately not a general sidecar copier. Fact time is local
    /// to each store, while the existing sync push contract for older
    /// sidecars remains unchanged. The indexed `capsules.source_hash` column
    /// is only a projection: it must agree with decoded canonical provenance
    /// for a live row, or the validated tombstone identity for a forgotten
    /// skeleton, on BOTH destination and candidate. An orphan declaration,
    /// corrupt timestamp, backwards range, ambiguous/drifted identity, or
    /// destination identity missing from the merged candidate fails closed
    /// before the candidate is pushed.
    pub(crate) fn rebase_event_time_from(
        &mut self,
        destination_path: &Path,
    ) -> Result<(), StoreError> {
        let destination = Store::open_readonly(destination_path)?;
        validate_all_capsule_identity_projections(&destination.conn)?;
        let mut stmt = destination
            .conn
            .prepare(
                "SELECT e.capsule_id, c.source_hash, c.canonical_json, \
                        t.capsule_id, t.source_hash, \
                        e.event_from, e.event_to, e.declared_at, \
                        (SELECT COUNT(*) FROM capsules c2 \
                         WHERE c2.source_hash = c.source_hash) \
                 FROM event_time e \
                 LEFT JOIN capsules c ON c.id = e.capsule_id \
                 LEFT JOIN tombstones t ON t.capsule_id = c.id \
                 ORDER BY e.capsule_id",
            )
            .map_err(backend)?;
        let rows = stmt
            .query_map([], |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, Option<String>>(1)?,
                    row.get::<_, Option<String>>(2)?,
                    row.get::<_, Option<String>>(3)?,
                    row.get::<_, Option<String>>(4)?,
                    row.get::<_, String>(5)?,
                    row.get::<_, String>(6)?,
                    row.get::<_, String>(7)?,
                    row.get::<_, i64>(8)?,
                ))
            })
            .map_err(backend)?;

        // Read and validate the complete destination declaration set before
        // opening the candidate transaction. Raw RFC3339 text is retained so
        // preservation is byte-for-byte even for a valid non-UTC offset.
        let mut destination_rows: Vec<(String, String, String, String, String)> = Vec::new();
        for row in rows {
            let (
                destination_id,
                source_hash,
                canonical_json,
                tombstone_id,
                tombstone_source_hash,
                event_from,
                event_to,
                declared_at,
                identity_count,
            ) = row.map_err(backend)?;
            let source_hash = source_hash.ok_or_else(|| StoreError::Corrupt {
                id: destination_id.clone(),
                reason: "event_time row has no destination capsule identity".to_string(),
            })?;
            if identity_count != 1 {
                return Err(StoreError::Corrupt {
                    id: destination_id.clone(),
                    reason: format!(
                        "event_time source_hash {source_hash:?} resolves to \
                         {identity_count} destination capsules, expected exactly one"
                    ),
                });
            }
            validate_capsule_identity_projection(
                &destination_id,
                &source_hash,
                canonical_json.as_deref(),
                tombstone_id.as_deref(),
                tombstone_source_hash.as_deref(),
            )?;
            let from = parse_at(&destination_id, "event_time.event_from", &event_from)?;
            let to = parse_at(&destination_id, "event_time.event_to", &event_to)?;
            parse_at(&destination_id, "event_time.declared_at", &declared_at)?;
            EventTimeRange::new(from, to).map_err(|_| StoreError::Corrupt {
                id: destination_id.clone(),
                reason: format!(
                    "event_time.event_to {event_to:?} lies before \
                     event_time.event_from {event_from:?}"
                ),
            })?;
            destination_rows.push((
                destination_id,
                source_hash,
                event_from,
                event_to,
                declared_at,
            ));
        }
        drop(stmt);

        validate_all_capsule_identity_projections(&self.conn)?;

        // Resolve every destination identity against the already-merged push
        // candidate before deleting its sender-local rows. Never bind by id:
        // cap-N values are store-local and can name different contents.
        let mut rebound: Vec<(String, String, String, String)> =
            Vec::with_capacity(destination_rows.len());
        let mut rebound_ids: BTreeSet<String> = BTreeSet::new();
        for (destination_id, source_hash, event_from, event_to, declared_at) in destination_rows {
            let mut candidate_stmt = self
                .conn
                .prepare(
                    "SELECT c.id, c.canonical_json, t.capsule_id, t.source_hash \
                     FROM capsules c \
                     LEFT JOIN tombstones t ON t.capsule_id = c.id \
                     WHERE c.source_hash = ?1 \
                     ORDER BY c.seq",
                )
                .map_err(backend)?;
            let candidate_rows = candidate_stmt
                .query_map([source_hash.as_str()], |row| {
                    Ok((
                        row.get::<_, String>(0)?,
                        row.get::<_, Option<String>>(1)?,
                        row.get::<_, Option<String>>(2)?,
                        row.get::<_, Option<String>>(3)?,
                    ))
                })
                .map_err(backend)?;
            let mut candidates = Vec::new();
            for candidate in candidate_rows {
                candidates.push(candidate.map_err(backend)?);
            }
            drop(candidate_stmt);
            if candidates.len() != 1 {
                return Err(StoreError::Corrupt {
                    id: destination_id,
                    reason: format!(
                        "event_time source_hash {source_hash:?} resolves to \
                         {} capsules in the merged push candidate, expected exactly one",
                        candidates.len()
                    ),
                });
            }
            let (candidate_id, canonical_json, tombstone_id, tombstone_source_hash) =
                candidates.pop().ok_or_else(|| StoreError::Corrupt {
                    id: destination_id,
                    reason: format!(
                        "event_time source_hash {source_hash:?} vanished from the merged push candidate"
                    ),
                })?;
            validate_capsule_identity_projection(
                &candidate_id,
                &source_hash,
                canonical_json.as_deref(),
                tombstone_id.as_deref(),
                tombstone_source_hash.as_deref(),
            )?;
            if !rebound_ids.insert(candidate_id.clone()) {
                return Err(StoreError::Corrupt {
                    id: candidate_id,
                    reason: format!(
                        "multiple destination event_time rows resolve to source_hash {source_hash:?}"
                    ),
                });
            }
            rebound.push((candidate_id, event_from, event_to, declared_at));
        }

        let tx = self.conn.transaction().map_err(backend)?;
        tx.execute("DELETE FROM event_time", []).map_err(backend)?;
        for (candidate_id, event_from, event_to, declared_at) in &rebound {
            tx.execute(
                "INSERT INTO event_time (capsule_id, event_from, event_to, declared_at) \
                 VALUES (?1, ?2, ?3, ?4)",
                params![candidate_id, event_from, event_to, declared_at],
            )
            .map_err(backend)?;
        }
        let stored_count: i64 = tx
            .query_row("SELECT COUNT(*) FROM event_time", [], |row| row.get(0))
            .map_err(backend)?;
        let expected_count = i64::try_from(rebound.len()).map_err(|error| StoreError::Corrupt {
            id: "event_time".to_string(),
            reason: format!("destination declaration count cannot fit i64: {error}"),
        })?;
        if stored_count != expected_count {
            return Err(StoreError::Corrupt {
                id: "event_time".to_string(),
                reason: format!(
                    "rebased {stored_count} destination declaration(s), expected {expected_count}"
                ),
            });
        }
        tx.commit().map_err(backend)
    }

    /// List LIVE capsules in append (`seq`) order, optionally fenced to a
    /// project and/or a project-prefix subtree ([`ListFilter`]; present
    /// fences AND-compose). `limit` keeps the NEWEST rows (the tail of the
    /// append order — "show my recent memories" is the operative ask on a
    /// memory index); the returned slice itself stays in ascending append
    /// order. Tombstoned rows are excluded — they have no capsule bytes to
    /// list; their markers live in [`Store::get_tombstone`].
    pub fn list(&self, filter: ListFilter) -> Result<Vec<StoredCapsule>, StoreError> {
        #[cfg(test)]
        STORE_LIST_READS.with(|c| c.set(c.get() + 1));
        // SQLite treats a negative LIMIT as "unlimited". The inner query
        // takes the newest N by seq desc; the outer re-sorts ascending so
        // callers always read append order. NULL-tolerant fences: a NULL
        // parameter disables its clause, so one prepared shape serves
        // every filter combination. The prefix arm is `substr`-exact
        // (character semantics, like `length`) — no LIKE/GLOB, so prefix
        // bytes can never act as pattern metacharacters.
        let limit = match filter.limit {
            None => -1_i64,
            Some(n) => i64::try_from(n).unwrap_or(i64::MAX),
        };
        let mut out = Vec::new();
        let mut stmt = self
            .conn
            .prepare(
                "SELECT id, seq, canonical_json, created_at, session_id FROM ( \
                     SELECT id, seq, canonical_json, created_at, session_id \
                     FROM capsules \
                     WHERE canonical_json IS NOT NULL \
                       AND (?1 IS NULL OR project_id = ?1) \
                       AND (?2 IS NULL OR project_id = ?2 \
                            OR substr(project_id, 1, length(?2) + 1) = ?2 || '/') \
                     ORDER BY seq DESC LIMIT ?3) \
                 ORDER BY seq",
            )
            .map_err(backend)?;
        let rows = stmt
            .query_map(
                params![filter.project_id, filter.project_prefix, limit],
                row_to_raw,
            )
            .map_err(backend)?;
        for row in rows {
            out.push(row.map_err(backend)?.decode()?);
        }
        Ok(out)
    }

    /// Fetch the capsule whose `provenance.source_hash` equals
    /// `source_hash`, if any — the ingest idempotency probe (s3). At most
    /// one can exist (UNIQUE index). If that capture was forgotten, the
    /// probe surfaces the typed [`StoreError::Tombstoned`] marker: the
    /// hash is still claimed (forget is sticky — no silent resurrection),
    /// but there is no content to return.
    pub fn find_by_source_hash(
        &self,
        source_hash: &str,
    ) -> Result<Option<StoredCapsule>, StoreError> {
        self.conn
            .query_row(
                "SELECT id, seq, canonical_json, created_at, session_id \
                 FROM capsules WHERE source_hash = ?1",
                [source_hash],
                row_to_raw,
            )
            .optional()
            .map_err(backend)?
            .map(RawRow::decode)
            .transpose()
    }

    /// Canonical snapshot: every LIVE capsule in append (`seq`) order, one
    /// canonical-JSON line each, `\n`-terminated (empty store → empty
    /// string). Line shape is the [`StoredCapsule`] field order with the
    /// embedded capsule in its own frozen canonical order — byte-stable:
    /// the same mutation sequence always yields identical bytes. This is
    /// the h3 determinism-conformance and replay comparand.
    ///
    /// Deterministic inclusion/exclusion rules (v2):
    /// - a capsule appended with a session link carries its `session_id`
    ///   field (append INPUT data); session-less lines are byte-identical
    ///   to their v1 form (the field is skipped);
    /// - tombstoned rows are EXCLUDED — their canonical bytes no longer
    ///   exist, and replaying the same append+forget sequence reproduces
    ///   the same snapshot;
    /// - sidecar tables (`relations`, `audit_events` — chain links
    ///   included, `classifications`, `tombstones`, `sessions`, `tiers`,
    ///   `synonyms`, `usage`, `capsules_fts`, `review_events`, `pin_events` +
    ///   `idx_pin_events_capsule_id`, `corroborations`, `source_cursors`,
    ///   `source_backfills`) are EXCLUDED by documented
    ///   rule: the snapshot is the CAPSULE comparand; each sidecar is
    ///   separately queryable and deterministic (the audit chain has its
    ///   own comparand, [`Store::journal_head`]). Sidecar writes therefore
    ///   NEVER move snapshot bytes — v2 snapshots stay byte-identical
    ///   under v3.
    pub fn canonical_snapshot(&self) -> Result<String, StoreError> {
        let all = self.list(ListFilter::default())?;
        let mut out = String::new();
        for stored in all {
            let line =
                serde_json::to_string(&stored).map_err(|e| StoreError::Serialize(e.to_string()))?;
            out.push_str(&line);
            out.push('\n');
        }
        Ok(out)
    }

    /// Re-derive the FTS5 mirror (`capsules_fts`) from the canonical
    /// `capsules` table: drop, recreate, repopulate — atomically. Returns
    /// the number of rows indexed. Recall over the rebuilt mirror is
    /// identical to recall before a drop (the derived-table proof): bm25
    /// depends only on the indexed rows, and the mirror's rowids are the
    /// canonical `seq` values. Tombstoned rows re-derive as the empty
    /// string — counted, unfindable.
    pub fn rebuild_fts(&mut self) -> Result<usize, StoreError> {
        rebuild_fts_on(&mut self.conn)
    }

    /// Store-level recall primitive: FTS5 `OR` match across `terms`. A
    /// multi-word term matches as the AND of its words (order- and
    /// adjacency-insensitive — "tokio pin" finds "pin tokio at 1.38"),
    /// mirroring how callers expand natural rephrasings; every word is
    /// individually quoted as an FTS5 string, so caller terms can NEVER
    /// inject FTS5 syntax (`OR`/`NEAR`/`-`/`*`/column filters are matched
    /// as literal text; see [`fts_phrase`]); embedded NUL — the one
    /// character that would end the MATCH string at the parser — is
    /// replaced by a space, the separator `unicode61` makes of control
    /// characters. Terms without a single alphanumeric character cannot
    /// tokenize and are skipped; no usable term → empty result, never an
    /// error. Tombstoned rows can never match (their mirror row is empty
    /// and the join re-fences on a live canonical row).
    ///
    /// Returns every match (no limit — trimming belongs to the engine,
    /// which owns the full deterministic tiebreak) as
    /// `(capsule, bm25_score)` with SQLite bm25 semantics: smaller =
    /// stronger match (scores are negative). Order at this layer: bm25
    /// ascending, then `seq` ascending — already deterministic; the
    /// retrieve engine re-sorts with the full PLAN s4 key (score,
    /// confidence, valid_from, id).
    pub fn search_fts(
        &self,
        terms: &[String],
        project_id: Option<&str>,
    ) -> Result<Vec<(StoredCapsule, f64)>, StoreError> {
        self.search_fts_inner(terms, project_id, None, None, None, None)
    }

    /// [`Store::search_fts`] capped to the top-`limit` matches by bm25 rank
    /// (best first) — the bounded candidate set for the write-time hint
    /// scans (perf-ingest). The cap is a `LIMIT` applied AFTER the
    /// `ORDER BY score, seq`, so it keeps the strongest-ranked candidates
    /// and drops the long tail a common token would otherwise return; the
    /// per-capture scan therefore stays flat as the store grows instead of
    /// scoring every match. Advisory ceiling only: exact `source_hash`
    /// idempotency ([`Store::find_by_source_hash`]) is a SEPARATE exact
    /// lookup, never this ranked scan.
    pub fn search_fts_limited(
        &self,
        terms: &[String],
        project_id: Option<&str>,
        limit: usize,
    ) -> Result<Vec<(StoredCapsule, f64)>, StoreError> {
        self.search_fts_inner(terms, project_id, None, None, Some(limit), None)
    }

    /// [`Store::search_fts`] with the full recall scope fences: `project_id`
    /// (exact) and `project_prefix` (the [`ListFilter::project_prefix`]
    /// subtree rule — `project_id == p` OR starting with `p + "/"`).
    /// and `session_id` (the character-exact store-local capsule label).
    /// Present fences AND-compose; all `None` is the unfenced search.
    /// Everything else — match semantics, quoting, ordering, tombstone
    /// exclusion — is exactly [`Store::search_fts`].
    pub fn search_fts_scoped(
        &self,
        terms: &[String],
        project_id: Option<&str>,
        project_prefix: Option<&str>,
        session_id: Option<&str>,
    ) -> Result<Vec<(StoredCapsule, f64)>, StoreError> {
        self.search_fts_inner(terms, project_id, project_prefix, session_id, None, None)
    }

    /// [`Store::search_fts_scoped`] with the S3 effort-lifecycle membership
    /// fence AND-composed onto the project/session fences: only capsules
    /// whose id appears in `effort_ids` (the effort's members ∪ {epic}) can
    /// ground. The fence is a single JSON-array bind matched with `json_each`
    /// (never a per-id variable and never post-filtering), so a 1000-member
    /// effort neither trips SQLite's variable limit nor forces a full scan —
    /// the `capsules_fts MATCH` driver still narrows first, then the unique
    /// `id` index probes membership. `None` is exactly [`Store::search_fts_scoped`]
    /// (byte-identical dormancy); an empty slice is a degenerate fence the
    /// caller must reject BEFORE reaching this seam (the engine never passes
    /// one).
    pub fn search_fts_effort(
        &self,
        terms: &[String],
        project_id: Option<&str>,
        project_prefix: Option<&str>,
        session_id: Option<&str>,
        effort_ids: Option<&[String]>,
    ) -> Result<Vec<(StoredCapsule, f64)>, StoreError> {
        self.search_fts_inner(
            terms,
            project_id,
            project_prefix,
            session_id,
            None,
            effort_ids,
        )
    }

    /// The one FTS query body behind [`Store::search_fts`],
    /// [`Store::search_fts_scoped`], [`Store::search_fts_effort`], and
    /// [`Store::search_fts_limited`].
    /// `limit` bounds the ranked result: `None` is unbounded (SQLite reads
    /// a negative `LIMIT` as unlimited, the same convention as
    /// [`Store::list`]); `Some(k)` keeps the top-`k` by `ORDER BY score,
    /// seq`. `effort_ids` is the S3 membership fence: `None` disables it
    /// entirely (byte-identical dormancy), `Some(ids)` keeps only capsules
    /// whose id is in the JSON-array set (`json_each`, one bind).
    fn search_fts_inner(
        &self,
        terms: &[String],
        project_id: Option<&str>,
        project_prefix: Option<&str>,
        session_id: Option<&str>,
        limit: Option<usize>,
        effort_ids: Option<&[String]>,
    ) -> Result<Vec<(StoredCapsule, f64)>, StoreError> {
        let phrases: Vec<String> = terms
            .iter()
            // NUL is the ONE character quoting cannot neutralize — it ends
            // the MATCH string at the FTS5 parser ("unterminated string",
            // w3 review). Map it to a space: exactly the token separator
            // unicode61 makes of every control character.
            .map(|term| term.replace('\0', " "))
            .filter(|term| term.chars().any(char::is_alphanumeric))
            .map(|term| fts_term_expr(&term))
            .collect();
        if phrases.is_empty() {
            return Ok(Vec::new());
        }
        let match_expr = phrases.join(" OR ");
        // SQLite treats a negative LIMIT as "unlimited" (same convention as
        // [`Store::list`]); a bounded hint scan passes a real top-K cap.
        let row_limit = match limit {
            None => -1_i64,
            Some(n) => i64::try_from(n).unwrap_or(i64::MAX),
        };
        // S3 effort membership fence: one JSON-array bind (NULL disables the
        // clause, exactly like the project/session fences). `json_each`
        // expands the array in-engine, so a 1000-member effort is a SINGLE
        // bound parameter — never `params_from_iter` (which would trip the
        // 999-variable ceiling) and never a post-filter.
        let effort_json = effort_ids_json(effort_ids)?;
        let mut out = Vec::new();
        // Same NULL-tolerant fence shape as [`Store::list`]: a NULL
        // parameter disables its clause; `substr` keeps prefix bytes
        // data-only (no LIKE/GLOB metacharacters).
        let mut stmt = self
            .conn
            .prepare(
                "SELECT c.id, c.seq, c.canonical_json, c.created_at, \
                        c.session_id, bm25(capsules_fts) AS score \
                 FROM capsules_fts \
                 JOIN capsules c ON c.seq = capsules_fts.rowid \
                 WHERE capsules_fts MATCH ?1 \
                   AND c.canonical_json IS NOT NULL \
                   AND (?2 IS NULL OR c.project_id = ?2) \
                   AND (?3 IS NULL OR c.project_id = ?3 \
                        OR substr(c.project_id, 1, length(?3) + 1) = ?3 || '/') \
                   AND (?4 IS NULL OR c.session_id = ?4) \
                   AND (?6 IS NULL OR c.id IN (SELECT value FROM json_each(?6))) \
                 ORDER BY score, c.seq LIMIT ?5",
            )
            .map_err(backend)?;
        let rows = stmt
            .query_map(
                params![
                    match_expr,
                    project_id,
                    project_prefix,
                    session_id,
                    row_limit,
                    effort_json
                ],
                row_to_scored,
            )
            .map_err(backend)?;
        for row in rows {
            let (raw, score) = row.map_err(backend)?;
            out.push((raw.decode()?, score));
        }
        Ok(out)
    }

    /// Record that `new_id` supersedes `old_id` — the replace-over-append
    /// discipline, executed by the caller (typically after a dedup hint).
    /// Thin wrapper over the generalized edge store: exactly
    /// `upsert_relation(Supersedes, from = new_id, to = old_id, now)`
    /// (donor orientation: `from` is the newer capsule, `to` the replaced
    /// one). The old capsule's bytes are never mutated and it stays
    /// reachable via [`Store::get`]/[`Store::list`] — recall (the engine)
    /// excludes it by default.
    pub fn supersede(
        &mut self,
        old_id: &str,
        new_id: &str,
        now: OffsetDateTime,
    ) -> Result<(), StoreError> {
        self.upsert_relation(RelationKind::Supersedes, new_id, old_id, now)
            .map(|_freshly_inserted| ())
    }

    /// [`Store::supersede`], recorded with `origin = 'import'` (u-r8
    /// round 3): the stale-import supersession mechanism's OWN edges —
    /// the only ones [`Store::unsupersede`] may later reverse. Every
    /// caller-driven supersede stays [`Store::supersede`] (`manual`).
    pub fn supersede_imported(
        &mut self,
        old_id: &str,
        new_id: &str,
        now: OffsetDateTime,
    ) -> Result<(), StoreError> {
        self.upsert_relation_origin(
            RelationKind::Supersedes,
            new_id,
            old_id,
            now,
            RelationOrigin::Import,
        )
        .map(|_freshly_inserted| ())
    }

    /// Whether any supersede relation names `id` as replaced. An unknown
    /// id is simply not superseded — `false`, never an error.
    pub fn is_superseded(&self, id: &str) -> Result<bool, StoreError> {
        self.conn
            .query_row(
                "SELECT EXISTS(SELECT 1 FROM relations \
                 WHERE kind = 'supersedes' AND to_id = ?1)",
                [id],
                |row| row.get(0),
            )
            .map_err(backend)
    }

    /// Append one pin/unpin event for `id` (S1, schema v16), returning the
    /// resulting [`PinRecord`] (the new latest state). APPEND-ONLY: state is
    /// the highest-`seq` row per `capsule_id`; a re-pin or unpin appends a
    /// fresh row, never mutates one. `reason`/`actor` must be non-empty
    /// ([`StoreError::EmptyField`]); `id` must name a stored capsule
    /// ([`StoreError::UnknownCapsule`] — a tombstoned or never-stored id).
    /// `at` is the INJECTED `now`. Pin is a pure sidecar: no capsule byte,
    /// tier, relation, or the stored `confidence` is touched here.
    pub fn append_pin_event(
        &mut self,
        id: &str,
        pinned: bool,
        reason: &str,
        actor: &str,
        now: OffsetDateTime,
    ) -> Result<PinRecord, StoreError> {
        if reason.trim().is_empty() {
            return Err(StoreError::EmptyField("reason"));
        }
        if actor.trim().is_empty() {
            return Err(StoreError::EmptyField("actor"));
        }
        let at = rfc3339_text(now)?;
        let tx = self.conn.transaction().map_err(backend)?;
        let exists: bool = tx
            .query_row(
                "SELECT EXISTS(SELECT 1 FROM capsules WHERE id = ?1)",
                [id],
                |row| row.get(0),
            )
            .map_err(backend)?;
        if !exists {
            return Err(StoreError::UnknownCapsule(id.to_string()));
        }
        let seq: i64 = tx
            .query_row(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM pin_events",
                [],
                |row| row.get(0),
            )
            .map_err(backend)?;
        tx.execute(
            "INSERT INTO pin_events (seq, capsule_id, pinned, reason, actor, at) \
             VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
            params![seq, id, i64::from(pinned), reason, actor, at],
        )
        .map_err(backend)?;
        tx.commit().map_err(backend)?;
        Ok(PinRecord {
            capsule_id: id.to_string(),
            pinned,
            reason: reason.to_string(),
            actor: actor.to_string(),
            at,
        })
    }

    /// Whether `id`'s LATEST pin event set it pinned (S1). The highest-`seq`
    /// row per capsule is the state; no row (the dormant default, every store
    /// predating v16) is NOT pinned — `false`, never an error. Sibling of
    /// [`Store::is_superseded`]; the retrieve/bootstrap decay key and the
    /// archive veto read it. A `false` here restores the exact pre-pin decay
    /// behavior (byte-identical dormancy).
    pub fn is_pinned(&self, id: &str) -> Result<bool, StoreError> {
        let pinned: Option<i64> = self
            .conn
            .query_row(
                "SELECT pinned FROM pin_events WHERE capsule_id = ?1 \
                 ORDER BY seq DESC LIMIT 1",
                [id],
                |row| row.get(0),
            )
            .optional()
            .map_err(backend)?;
        Ok(pinned == Some(1))
    }

    /// Every capsule whose LATEST pin event is pinned (S1), ordered by
    /// `capsule_id` for determinism. Empty on a fresh store; never an error.
    /// A capsule later unpinned (its newest event `pinned = 0`) is absent.
    pub fn list_pinned(&self) -> Result<Vec<CapsuleId>, StoreError> {
        let mut stmt = self
            .conn
            .prepare(
                "SELECT capsule_id FROM pin_events AS pe \
                 WHERE pe.seq = (SELECT MAX(seq) FROM pin_events \
                                 WHERE capsule_id = pe.capsule_id) \
                   AND pe.pinned = 1 \
                 ORDER BY pe.capsule_id",
            )
            .map_err(backend)?;
        let ids = stmt
            .query_map([], |row| row.get::<_, String>(0))
            .map_err(backend)?
            .collect::<Result<Vec<String>, _>>()
            .map_err(backend)?;
        Ok(ids.into_iter().map(CapsuleId).collect())
    }

    /// The full latest pin state for `id` (S1) — [`PinRecord`] from the
    /// highest-`seq` row, or `None` when the capsule was never pinned or
    /// unpinned. The audit/inspection read; [`Store::is_pinned`] is the
    /// hot-path boolean.
    pub fn pin_state_of(&self, id: &str) -> Result<Option<PinRecord>, StoreError> {
        self.conn
            .query_row(
                "SELECT capsule_id, pinned, reason, actor, at FROM pin_events \
                 WHERE capsule_id = ?1 ORDER BY seq DESC LIMIT 1",
                [id],
                |row| {
                    Ok(PinRecord {
                        capsule_id: row.get(0)?,
                        pinned: row.get::<_, i64>(1)? == 1,
                        reason: row.get(2)?,
                        actor: row.get(3)?,
                        at: row.get(4)?,
                    })
                },
            )
            .optional()
            .map_err(backend)
    }

    /// Whether any `falsifies` edge names `id` as the falsified target
    /// (u6h). The recall-eligibility signal: a `true` here makes recall
    /// fence the capsule (`crate::retrieve`), its bytes untouched and still
    /// served by `get`/`list`. Sibling of [`Store::is_superseded`]; an
    /// unknown id is simply not falsified — `false`, never an error.
    pub fn is_falsified(&self, id: &str) -> Result<bool, StoreError> {
        self.conn
            .query_row(
                "SELECT EXISTS(SELECT 1 FROM relations \
                 WHERE kind = 'falsifies' AND to_id = ?1)",
                [id],
                |row| row.get(0),
            )
            .map_err(backend)
    }

    /// Append one review-verdict row for `id` (b2 staged review) —
    /// append-only, `seq` assigned MAX+1 like every ledger. The birth event
    /// is `proposed` (a fresh staged ingest); authority-bearing internal
    /// consumers may append compatible `ratified` / `rejected` history.
    /// NEVER tombstones and NEVER deletes: fence state is DERIVED from the
    /// latest verdict, so a `rejected` is reversible by a later `ratified`.
    /// `now` is injected; the store reads no clock. Writes NO audit row — the
    /// boundary audits (module audit policy).
    pub fn append_review_event(
        &mut self,
        id: &str,
        verdict: ReviewVerdict,
        reason: &str,
        actor: &str,
        now: OffsetDateTime,
    ) -> Result<(), StoreError> {
        let at = rfc3339_text(now)?;
        let tx = self.conn.transaction().map_err(backend)?;
        let seq: i64 = tx
            .query_row(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM review_events",
                [],
                |row| row.get(0),
            )
            .map_err(backend)?;
        tx.execute(
            "INSERT INTO review_events (seq, capsule_id, verdict, reason, actor, at) \
             VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
            params![seq, id, verdict.as_str(), reason, actor, at],
        )
        .map_err(backend)?;
        tx.commit().map_err(backend)?;
        Ok(())
    }

    /// The DERIVED review state of `id` (b2 staged review): `Some` with the
    /// full verdict history + projected latest verdict when ANY review row
    /// exists, `None` when the capsule was never staged. Fence state reads off
    /// [`ReviewState::fenced`] — one source per fact. A verdict outside the
    /// closed set, or an unparseable instant (a corrupt file the CHECK could
    /// not have written), is a fail-closed [`StoreError::Corrupt`].
    pub fn review_state_of(&self, id: &str) -> Result<Option<ReviewState>, StoreError> {
        let mut stmt = self
            .conn
            .prepare(
                "SELECT verdict, reason, actor, at FROM review_events \
                 WHERE capsule_id = ?1 ORDER BY seq",
            )
            .map_err(backend)?;
        let rows = stmt
            .query_map([id], |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, String>(2)?,
                    row.get::<_, String>(3)?,
                ))
            })
            .map_err(backend)?;
        let mut history: Vec<ReviewEvent> = Vec::new();
        for row in rows {
            let (verdict, reason, actor, at) = row.map_err(backend)?;
            let verdict =
                ReviewVerdict::from_wire(&verdict).ok_or_else(|| StoreError::Corrupt {
                    id: id.to_owned(),
                    reason: format!("review_events verdict {verdict:?} is outside the closed set"),
                })?;
            let at = OffsetDateTime::parse(&at, &Rfc3339).map_err(|e| StoreError::Corrupt {
                id: id.to_owned(),
                reason: format!("review_events at: {e}"),
            })?;
            history.push(ReviewEvent {
                verdict,
                reason,
                actor,
                at,
            });
        }
        match history.last() {
            None => Ok(None),
            Some(last) => {
                let latest = last.verdict;
                Ok(Some(ReviewState { latest, history }))
            }
        }
    }

    /// Whether `id` is fenced from grounding by a standing proposal (b2) —
    /// review history exists and the latest verdict is not `ratified`.
    /// Derived from [`Store::review_state_of`], one source per fact. An
    /// unstaged id is simply not fenced — `false`, never an error.
    pub fn review_fenced(&self, id: &str) -> Result<bool, StoreError> {
        Ok(self
            .review_state_of(id)?
            .is_some_and(|state| state.fenced()))
    }

    /// The standing review verdict of `id` (b2) when it carries review
    /// history (`Some("proposed" | "ratified" | "rejected")`), else `None` —
    /// the verdict surfaced on an INCLUDED staged retrieve envelope and echoed
    /// on an ingest collision.
    pub fn review_verdict(&self, id: &str) -> Result<Option<String>, StoreError> {
        Ok(self
            .review_state_of(id)?
            .map(|state| state.latest().as_str().to_owned()))
    }

    /// The full append-only review history of `id` (b2) in seq order — the
    /// read the merge path carries into a freshly-minted capsule so a proposal
    /// stays fenced across a store boundary (fence durability). Empty when the
    /// capsule was never staged.
    pub fn review_events_of(&self, id: &str) -> Result<Vec<ReviewEvent>, StoreError> {
        Ok(self
            .review_state_of(id)?
            .map_or_else(Vec::new, |state| state.history))
    }

    /// Every capsule id currently fenced by a standing proposal (b2) — the
    /// capsule whose LATEST review verdict is not `ratified`. Deterministic id
    /// order. The digest excludes these from its truth counts.
    pub fn list_review_fenced(&self) -> Result<Vec<String>, StoreError> {
        let mut stmt = self
            .conn
            .prepare(
                "SELECT capsule_id FROM review_events r1 \
                 WHERE seq = (SELECT MAX(seq) FROM review_events r2 \
                              WHERE r2.capsule_id = r1.capsule_id) \
                   AND verdict <> 'ratified' \
                 ORDER BY capsule_id",
            )
            .map_err(backend)?;
        let rows = stmt
            .query_map([], |row| row.get::<_, String>(0))
            .map_err(backend)?;
        let mut ids = Vec::new();
        for row in rows {
            ids.push(row.map_err(backend)?);
        }
        Ok(ids)
    }

    /// How many capsules are fenced by a standing proposal (b2) — the digest
    /// `staged.proposed` count. Same latest-verdict projection as
    /// [`Store::list_review_fenced`].
    pub fn count_review_fenced(&self) -> Result<usize, StoreError> {
        let count: i64 = self
            .conn
            .query_row(
                "SELECT COUNT(*) FROM review_events r1 \
                 WHERE seq = (SELECT MAX(seq) FROM review_events r2 \
                              WHERE r2.capsule_id = r1.capsule_id) \
                   AND verdict <> 'ratified'",
                [],
                |row| row.get(0),
            )
            .map_err(backend)?;
        Ok(count.max(0) as usize)
    }

    /// How many standing proposals (b2) are STALE — fenced (latest verdict
    /// not `ratified`) and last touched more than `window_days` before `now`.
    /// Age runs from the proposal's LATEST review instant to `now` (injected;
    /// the store reads no clock). The digest `staged.stale_proposals` pressure
    /// count — proposals are visible pressure, never silent backlog.
    pub fn stale_proposals(
        &self,
        now: OffsetDateTime,
        window_days: i64,
    ) -> Result<usize, StoreError> {
        let cutoff = rfc3339_text(now - time::Duration::days(window_days))?;
        let count: i64 = self
            .conn
            .query_row(
                "SELECT COUNT(*) FROM review_events r1 \
                 WHERE seq = (SELECT MAX(seq) FROM review_events r2 \
                              WHERE r2.capsule_id = r1.capsule_id) \
                   AND verdict <> 'ratified' \
                   AND at < ?1",
                [cutoff],
                |row| row.get(0),
            )
            .map_err(backend)?;
        Ok(count.max(0) as usize)
    }

    /// Of the bounded candidate `ids`, those a `supersedes` edge names as
    /// replaced — the batched form of [`Store::is_superseded`]: ONE query
    /// over the whole candidate set instead of one point query per
    /// candidate (perf-ingest, so the write-time near-duplicate hint scan's
    /// DB round-trips stay flat as the store grows). An id absent from the
    /// result is live; an unknown id is simply not superseded. Empty `ids`
    /// short-circuits to the empty set (no query — `IN ()` is not valid
    /// SQL).
    pub fn superseded_among(&self, ids: &[&str]) -> Result<BTreeSet<String>, StoreError> {
        if ids.is_empty() {
            return Ok(BTreeSet::new());
        }
        let sql = format!(
            "SELECT DISTINCT to_id FROM relations \
             WHERE kind = 'supersedes' AND to_id IN ({})",
            sql_placeholders(ids.len()),
        );
        let mut stmt = self.conn.prepare(&sql).map_err(backend)?;
        let rows = stmt
            .query_map(params_from_iter(ids.iter().copied()), |row| {
                row.get::<_, String>(0)
            })
            .map_err(backend)?;
        let mut out = BTreeSet::new();
        for row in rows {
            out.insert(row.map_err(backend)?);
        }
        Ok(out)
    }

    /// Of the bounded candidate `ids`, those the write-time SIBLING scan
    /// must drop — the batched union of the three per-candidate fences it
    /// otherwise fires ([`Store::is_superseded`] + [`Store::is_falsified`] +
    /// a non-Active [`Store::get_tier`]): an id is excluded when a
    /// `supersedes` OR `falsifies` edge names it, OR its `tiers` row holds a
    /// value other than `active`. A MISSING `tiers` row is Active by the
    /// default rule, so absence from `tiers` never excludes — exactly
    /// [`Store::get_tier`]'s semantics for these stored candidates. ONE
    /// round-trip for the whole candidate set (perf-ingest). Empty `ids`
    /// short-circuits to the empty set (no query).
    pub fn sibling_excluded_among(&self, ids: &[&str]) -> Result<BTreeSet<String>, StoreError> {
        if ids.is_empty() {
            return Ok(BTreeSet::new());
        }
        let placeholders = sql_placeholders(ids.len());
        let sql = format!(
            "SELECT to_id AS id FROM relations \
                 WHERE kind IN ('supersedes', 'falsifies') AND to_id IN ({placeholders}) \
             UNION \
             SELECT capsule_id AS id FROM tiers \
                 WHERE tier != 'active' AND capsule_id IN ({placeholders})",
        );
        let mut stmt = self.conn.prepare(&sql).map_err(backend)?;
        // The two IN clauses bind the candidate set twice, in order.
        let rows = stmt
            .query_map(
                params_from_iter(ids.iter().copied().chain(ids.iter().copied())),
                |row| row.get::<_, String>(0),
            )
            .map_err(backend)?;
        let mut out = BTreeSet::new();
        for row in rows {
            out.insert(row.map_err(backend)?);
        }
        Ok(out)
    }

    /// Record one directed, typed edge `from --kind--> to`. Validates that
    /// the endpoints differ ([`StoreError::SelfRelation`] — no kind is
    /// reflexive) and that both ids are stored
    /// ([`StoreError::UnknownCapsule`]; a tombstoned capsule still counts
    /// as stored — edges are history and may name forgotten nodes); on any
    /// rejection nothing is written. Endpoint rule: `to_id` is ALWAYS a
    /// stored capsule, and so is `from_id` — EXCEPT the u6h `falsifies`
    /// kind, whose `from_id` may instead name a stored OUTCOME record
    /// (`out-<n>`, [`Store::append_outcome`]): an observed outcome
    /// falsifying a claim capsule. No other kind admits a non-capsule
    /// endpoint. Re-recording the same
    /// `(kind, from, to)` is an idempotent no-op that keeps the FIRST `at`
    /// (records, not columns); the same pair may carry several kinds, and
    /// a capsule any number of edges. `at` is persisted from the INJECTED
    /// `now` — the store reads no clock. Returns `true` when the edge was
    /// freshly inserted, `false` when it already existed (the no-op) — so
    /// the surface can tell the caller which happened.
    pub fn upsert_relation(
        &mut self,
        kind: RelationKind,
        from_id: &str,
        to_id: &str,
        now: OffsetDateTime,
    ) -> Result<bool, StoreError> {
        self.upsert_relation_origin(kind, from_id, to_id, now, RelationOrigin::Manual)
    }

    /// [`Store::upsert_relation`] with an explicit [`RelationOrigin`] —
    /// the one write path for edges; `origin` rides the same
    /// first-write-wins idempotency as `at` (an INSERT OR IGNORE replay
    /// never rewrites either, whatever origin it carries).
    pub fn upsert_relation_origin(
        &mut self,
        kind: RelationKind,
        from_id: &str,
        to_id: &str,
        now: OffsetDateTime,
        origin: RelationOrigin,
    ) -> Result<bool, StoreError> {
        if from_id == to_id {
            return Err(StoreError::SelfRelation {
                kind,
                id: from_id.to_string(),
            });
        }
        let at = rfc3339_text(now)?;
        let tx = self.conn.transaction().map_err(backend)?;
        for (id, is_from) in [(from_id, true), (to_id, false)] {
            let is_capsule: bool = tx
                .query_row(
                    "SELECT EXISTS(SELECT 1 FROM capsules WHERE id = ?1)",
                    [id],
                    |row| row.get(0),
                )
                .map_err(backend)?;
            // The one non-capsule endpoint the ontology admits: an outcome
            // record on the FROM side of a `falsifies` edge (u6h).
            let is_falsifier_outcome = is_from
                && kind == RelationKind::Falsifies
                && tx
                    .query_row(
                        "SELECT EXISTS(SELECT 1 FROM outcomes WHERE id = ?1)",
                        [id],
                        |row| row.get(0),
                    )
                    .map_err(backend)?;
            if !is_capsule && !is_falsifier_outcome {
                // Dropping the uncommitted transaction rolls back.
                return Err(StoreError::UnknownCapsule(id.to_string()));
            }
        }
        let inserted = tx
            .execute(
                "INSERT OR IGNORE INTO relations (kind, from_id, to_id, at, origin) \
                 VALUES (?1, ?2, ?3, ?4, ?5)",
                params![kind.as_str(), from_id, to_id, at, origin.as_str()],
            )
            .map_err(backend)?;
        tx.commit().map_err(backend)?;
        Ok(inserted > 0)
    }

    /// Every edge touching `id` (either endpoint), in the deterministic
    /// data order `(at, kind, from_id, to_id)` — pure row data, stable
    /// under VACUUM/replay. An unknown id has no edges: empty, never an
    /// error.
    pub fn list_relations(&self, id: &str) -> Result<Vec<RelationRecord>, StoreError> {
        let mut stmt = self
            .conn
            .prepare(
                "SELECT kind, from_id, to_id, at, origin FROM relations \
                 WHERE from_id = ?1 OR to_id = ?1 \
                 ORDER BY at, kind, from_id, to_id",
            )
            .map_err(backend)?;
        let rows = stmt.query_map([id], row_to_relation).map_err(backend)?;
        let mut out = Vec::new();
        for row in rows {
            out.push(row.map_err(backend)?.decode()?);
        }
        Ok(out)
    }

    /// The FULL edge list (every relation in the store), deterministic
    /// data order `(at, kind, from_id, to_id)` — the digest/dag projection
    /// input: a consumer can fold `blocks` edges into a dependency graph,
    /// `supersedes` chains into lineage, etc.
    pub fn all_relations(&self) -> Result<Vec<RelationRecord>, StoreError> {
        // S2 effort-lifecycle perf guard: the digest/bootstrap contract is
        // exactly ONE `all_relations` read per call. A test-only thread-local
        // tally (each `#[tokio::test]` runs its handler on its own thread, so
        // the count never races a parallel test) lets the read-count test
        // prove it; compiled out of release.
        #[cfg(test)]
        ALL_RELATIONS_READS.with(|c| c.set(c.get() + 1));
        let mut stmt = self
            .conn
            .prepare(
                "SELECT kind, from_id, to_id, at, origin FROM relations \
                 ORDER BY at, kind, from_id, to_id",
            )
            .map_err(backend)?;
        let rows = stmt.query_map([], row_to_relation).map_err(backend)?;
        let mut out = Vec::new();
        for row in rows {
            out.push(row.map_err(backend)?.decode()?);
        }
        Ok(out)
    }

    /// The `blocked_by` projection: ids of every capsule that blocks `id`
    /// (edges `blocker --blocks--> id`), deterministic `(at, from_id)`
    /// order. Empty for unknown or unblocked ids — a projection, never an
    /// error.
    pub fn blockers_of(&self, id: &str) -> Result<Vec<String>, StoreError> {
        let mut stmt = self
            .conn
            .prepare(
                "SELECT from_id FROM relations \
                 WHERE kind = 'blocks' AND to_id = ?1 \
                 ORDER BY at, from_id",
            )
            .map_err(backend)?;
        let rows = stmt
            .query_map([id], |row| row.get::<_, String>(0))
            .map_err(backend)?;
        let mut out = Vec::new();
        for row in rows {
            out.push(row.map_err(backend)?);
        }
        Ok(out)
    }

    /// Append one APPEND-ONLY outcome-observation record (u6h), returning
    /// the stored [`OutcomeRecord`] with its minted `out-<seq>` id (the same
    /// `MAX(seq)+1` determinism spine as capsule append). ADVISORY substrate:
    /// an OBSERVATION record, NEVER a witnessed close — nothing here treats
    /// it as proven, and recording one NEVER changes any capsule's recall
    /// eligibility (only a `falsifies` edge does). `description`/`actor` must
    /// be non-empty ([`StoreError::EmptyField`] — the caller names who
    /// observed, no default); a present `capsule_id` must name a stored
    /// capsule ([`StoreError::UnknownCapsule`]). A scored observation carries
    /// `receipt_id` and `score` together: the receipt is resolved before the
    /// row is inserted, then every returned capsule's advisory EMA weight is
    /// updated in the SAME transaction. Unknown/corrupt receipts or any EMA
    /// write failure roll back both the outcome and all weights. No verb
    /// updates or deletes an outcome row. `at` is the INJECTED `now` — the
    /// store reads no clock.
    #[allow(
        clippy::too_many_arguments,
        reason = "the store boundary keeps each outcome/scoring field explicit and separately validated"
    )]
    pub fn append_outcome(
        &mut self,
        description: &str,
        actor: &str,
        evidence_ref: Option<&str>,
        capsule_id: Option<&str>,
        receipt_id: Option<&str>,
        score: Option<f64>,
        now: OffsetDateTime,
    ) -> Result<AppendedOutcome, StoreError> {
        let at = rfc3339_text(now)?;
        let tx = self.conn.transaction().map_err(backend)?;
        // Pair/range validation is shape validation, not an effect. For a
        // valid scored request, receipt resolution is the FIRST database
        // operation inside the transaction: no sequence is minted and no
        // row is inserted before the grounding address proves usable.
        let scored_feedback = match (receipt_id, score) {
            (None, None) => None,
            (Some(receipt_id), Some(score)) => {
                validate_outcome_scoring(receipt_id, score)?;
                let returned_ids = receipt_returned_ids_on(&tx, receipt_id)?
                    .ok_or_else(|| StoreError::UnknownReceipt(receipt_id.to_string()))?;
                validate_receipt_capsules_on(&tx, receipt_id, &returned_ids)?;
                Some((returned_ids, score))
            }
            _ => {
                return Err(StoreError::InvalidOutcomeScoring(
                    "receipt_id and score must be present together",
                ));
            }
        };
        let seq: i64 = tx
            .query_row(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM outcomes",
                [],
                |row| row.get(0),
            )
            .map_err(backend)?;
        // Shape validated in the pure constructor (non-empty id/description/
        // actor) BEFORE the existence probe, so an empty mandatory field
        // surfaces regardless of capsule_id.
        let record = OutcomeRecord::new(
            format!("out-{seq}"),
            description.to_string(),
            actor.to_string(),
            evidence_ref.map(str::to_string),
            capsule_id.map(str::to_string),
            receipt_id.map(str::to_string),
            score,
            now,
        )?;
        if let Some(cap) = &record.capsule_id {
            let exists: bool = tx
                .query_row(
                    "SELECT EXISTS(SELECT 1 FROM capsules WHERE id = ?1)",
                    [cap],
                    |row| row.get(0),
                )
                .map_err(backend)?;
            if !exists {
                // Dropping the uncommitted transaction rolls back.
                return Err(StoreError::UnknownCapsule(cap.clone()));
            }
        }
        tx.execute(
            "INSERT INTO outcomes \
             (seq, id, description, actor, evidence_ref, capsule_id, receipt_id, score, at) \
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)",
            params![
                seq,
                &record.id,
                &record.description,
                &record.actor,
                &record.evidence_ref,
                &record.capsule_id,
                &record.receipt_id,
                record.score,
                at
            ],
        )
        .map_err(backend)?;
        let weights_updated = match scored_feedback {
            Some((ids, score)) => {
                let ids: Vec<&str> = ids.iter().map(String::as_str).collect();
                Some(apply_feedback_on(&tx, &ids, score, &at)?)
            }
            None => None,
        };
        tx.commit().map_err(backend)?;
        Ok(AppendedOutcome {
            record,
            weights_updated,
        })
    }

    /// Every outcome record, in append order (`seq` asc) — the deterministic
    /// list surface (u6h). Empty on a fresh store; never an error. Rows
    /// re-validate through [`OutcomeRecord::new`] on the way out, the same
    /// read-revalidation discipline the capsule/relation reads use.
    pub fn list_outcomes(&self) -> Result<Vec<OutcomeRecord>, StoreError> {
        let mut stmt = self
            .conn
            .prepare(
                "SELECT id, description, actor, evidence_ref, capsule_id, receipt_id, score, at \
                 FROM outcomes ORDER BY seq",
            )
            .map_err(backend)?;
        let rows = stmt.query_map([], row_to_outcome).map_err(backend)?;
        let mut out = Vec::new();
        for row in rows {
            out.push(row.map_err(backend)?.decode()?);
        }
        Ok(out)
    }

    /// Apply one scored-feedback EMA to `ids`, preserving input order, in a
    /// single transaction. Missing weights start at
    /// [`FEEDBACK_NEUTRAL_WEIGHT`]; every stored and computed value is
    /// revalidated as finite and inside `0.0..=1.0`.
    pub fn apply_feedback(
        &mut self,
        ids: &[&str],
        score: f64,
        now: OffsetDateTime,
    ) -> Result<Vec<(String, f64)>, StoreError> {
        validate_feedback_score(score)?;
        let at = rfc3339_text(now)?;
        let tx = self.conn.transaction().map_err(backend)?;
        let updated = apply_feedback_on(&tx, ids, score, &at)?;
        tx.commit().map_err(backend)?;
        Ok(updated)
    }

    /// Read one advisory feedback weight. An absent row is `None`; corrupt
    /// persisted values fail closed instead of reaching ranking arithmetic.
    pub fn feedback_weight_of(&self, id: &str) -> Result<Option<f64>, StoreError> {
        feedback_weight_on(&self.conn, id)
    }

    /// Append one APPEND-ONLY pairwise preference-evidence record (u6i),
    /// returning the stored [`PreferenceRecord`] with its minted `pref-<seq>`
    /// id. `context`/`actor` must be non-empty ([`StoreError::EmptyField`]);
    /// BOTH `preferred_id` and `rejected_id` must name stored capsules
    /// ([`StoreError::UnknownCapsule`]). Pairwise substrate ONLY — no score,
    /// no aggregation; nothing consumes it yet. No verb updates or deletes a
    /// row. `at` is the INJECTED `now`.
    pub fn append_preference(
        &mut self,
        preferred_id: &str,
        rejected_id: &str,
        context: &str,
        actor: &str,
        now: OffsetDateTime,
    ) -> Result<PreferenceRecord, StoreError> {
        let at = rfc3339_text(now)?;
        let tx = self.conn.transaction().map_err(backend)?;
        let seq: i64 = tx
            .query_row(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM preferences",
                [],
                |row| row.get(0),
            )
            .map_err(backend)?;
        let record = PreferenceRecord::new(
            format!("pref-{seq}"),
            preferred_id.to_string(),
            rejected_id.to_string(),
            context.to_string(),
            actor.to_string(),
            now,
        )?;
        for id in [&record.preferred_id, &record.rejected_id] {
            let exists: bool = tx
                .query_row(
                    "SELECT EXISTS(SELECT 1 FROM capsules WHERE id = ?1)",
                    [id],
                    |row| row.get(0),
                )
                .map_err(backend)?;
            if !exists {
                return Err(StoreError::UnknownCapsule(id.clone()));
            }
        }
        tx.execute(
            "INSERT INTO preferences \
             (seq, id, preferred_id, rejected_id, context, actor, at) \
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
            params![
                seq,
                record.id,
                record.preferred_id,
                record.rejected_id,
                record.context,
                record.actor,
                at
            ],
        )
        .map_err(backend)?;
        tx.commit().map_err(backend)?;
        Ok(record)
    }

    /// Every preference record, in append order (`seq` asc) — the
    /// deterministic list surface (u6i). Empty on a fresh store; never an
    /// error. Rows re-validate through [`PreferenceRecord::new`] on read.
    pub fn list_preferences(&self) -> Result<Vec<PreferenceRecord>, StoreError> {
        let mut stmt = self
            .conn
            .prepare(
                "SELECT id, preferred_id, rejected_id, context, actor, at \
                 FROM preferences ORDER BY seq",
            )
            .map_err(backend)?;
        let rows = stmt.query_map([], row_to_preference).map_err(backend)?;
        let mut out = Vec::new();
        for row in rows {
            out.push(row.map_err(backend)?.decode()?);
        }
        Ok(out)
    }

    /// Append one audit ledger row; returns its 1-based ledger `seq`
    /// (explicitly assigned — same determinism discipline as capsule
    /// append). `actor`, `action`, `subject` must be non-empty
    /// ([`StoreError::EmptyField`]); `reason` is optional. `at` is the
    /// INJECTED `now`.
    ///
    /// Journal chain (w2): the row's `chained_hash` is computed inside the
    /// same transaction as `sha256(prev_hash + canonical_line)` — where
    /// `prev_hash` is the previous row's `chained_hash` (`""` when this is
    /// seq 1) and `canonical_line` is [`audit_canonical_line`] over
    /// exactly the bytes being inserted. Pure function of the ledger
    /// contents: no clock, no randomness, replay-identical.
    ///
    /// Module-level audit policy: EVERY mutation gets audited — the
    /// integrator wires this call next to each mutating call site
    /// (ingest/append, supersede/relation, classification, forget,
    /// session open/finish). The ledger itself is append-only: no update
    /// or delete API exists.
    pub fn append_audit(
        &mut self,
        actor: &str,
        action: &str,
        subject: &str,
        reason: Option<&str>,
        now: OffsetDateTime,
    ) -> Result<i64, StoreError> {
        for (field, value) in [("actor", actor), ("action", action), ("subject", subject)] {
            if value.trim().is_empty() {
                return Err(StoreError::EmptyField(field));
            }
        }
        let at = rfc3339_text(now)?;
        let tx = self.conn.transaction().map_err(backend)?;
        let seq: i64 = tx
            .query_row(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM audit_events",
                [],
                |row| row.get(0),
            )
            .map_err(backend)?;
        let prev: Option<String> = tx
            .query_row(
                "SELECT chained_hash FROM audit_events ORDER BY seq DESC LIMIT 1",
                [],
                |row| row.get(0),
            )
            .optional()
            .map_err(backend)?;
        let line = audit_canonical_line(seq, &at, actor, action, subject, reason)?;
        let chained_hash = chained_hash_of(prev.as_deref().unwrap_or(""), &line);
        tx.execute(
            "INSERT INTO audit_events \
             (seq, at, actor, action, subject, reason, chained_hash) \
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
            params![seq, at, actor, action, subject, reason, chained_hash],
        )
        .map_err(backend)?;
        tx.commit().map_err(backend)?;
        Ok(seq)
    }

    /// The journal head: the newest audit row's `chained_hash`;
    /// `Ok(None)` on an empty ledger. Pinning this value OUTSIDE the file
    /// (e.g. in a session close note) is what turns the chain's internal
    /// consistency into truncation evidence — the chain alone cannot see
    /// its own tail being cut ([module docs](self)).
    pub fn journal_head(&self) -> Result<Option<String>, StoreError> {
        self.conn
            .query_row(
                "SELECT chained_hash FROM audit_events ORDER BY seq DESC LIMIT 1",
                [],
                |row| row.get(0),
            )
            .optional()
            .map_err(backend)
    }

    /// Recompute the whole journal chain from row bytes and compare it to
    /// the stored `chained_hash` links, in `seq` order. Returns the number
    /// of verified rows (0 for an empty ledger). The FIRST row whose
    /// stored hash fails recomputation — an edited row, a removed
    /// mid-ledger row, or a forged hash — is named in the typed
    /// [`StoreError::JournalBroken`]. A verified prefix stays vouched-for:
    /// everything before the named seq re-hashed correctly.
    pub fn verify_chain(&self) -> Result<u64, StoreError> {
        let mut stmt = self
            .conn
            .prepare(
                "SELECT seq, at, actor, action, subject, reason, chained_hash \
                 FROM audit_events ORDER BY seq",
            )
            .map_err(backend)?;
        let rows = stmt.query_map([], row_to_audit).map_err(backend)?;
        let mut prev = String::new();
        let mut count: u64 = 0;
        for row in rows {
            let raw = row.map_err(backend)?;
            let line = audit_canonical_line(
                raw.seq,
                &raw.at,
                &raw.actor,
                &raw.action,
                &raw.subject,
                raw.reason.as_deref(),
            )?;
            let expected = chained_hash_of(&prev, &line);
            if raw.chained_hash != expected {
                return Err(StoreError::JournalBroken { seq: raw.seq });
            }
            prev = raw.chained_hash;
            count += 1;
        }
        Ok(count)
    }

    /// Read the audit ledger, MOST RECENT FIRST (`seq` descending — the
    /// natural audit view), optionally fenced to one `subject` and
    /// truncated to `limit`. Deterministic: `seq` is totally ordered.
    pub fn list_audit(
        &self,
        limit: Option<usize>,
        subject: Option<&str>,
    ) -> Result<Vec<AuditEvent>, StoreError> {
        let limit = match limit {
            None => -1_i64,
            Some(n) => i64::try_from(n).unwrap_or(i64::MAX),
        };
        let mut out = Vec::new();
        match subject {
            Some(subject) => {
                let mut stmt = self
                    .conn
                    .prepare(
                        "SELECT seq, at, actor, action, subject, reason, chained_hash \
                         FROM audit_events WHERE subject = ?1 \
                         ORDER BY seq DESC LIMIT ?2",
                    )
                    .map_err(backend)?;
                let rows = stmt
                    .query_map(params![subject, limit], row_to_audit)
                    .map_err(backend)?;
                for row in rows {
                    out.push(row.map_err(backend)?.decode()?);
                }
            }
            None => {
                let mut stmt = self
                    .conn
                    .prepare(
                        "SELECT seq, at, actor, action, subject, reason, chained_hash \
                         FROM audit_events ORDER BY seq DESC LIMIT ?1",
                    )
                    .map_err(backend)?;
                let rows = stmt
                    .query_map(params![limit], row_to_audit)
                    .map_err(backend)?;
                for row in rows {
                    out.push(row.map_err(backend)?.decode()?);
                }
            }
        }
        Ok(out)
    }

    /// Set (or replace) `capsule_id`'s classification label. `kind` and
    /// `scope` are validated against the closed sets
    /// ([`CLASSIFICATION_KINDS`] / [`CLASSIFICATION_SCOPES`] —
    /// [`StoreError::InvalidClassification`] outside them); the capsule
    /// must be stored ([`StoreError::UnknownCapsule`]; tombstoned still
    /// counts — the label is about the record, not the content). Upsert:
    /// a re-classification replaces the previous label and stamps the new
    /// INJECTED `at`.
    pub fn set_classification(
        &mut self,
        capsule_id: &str,
        kind: &str,
        scope: &str,
        now: OffsetDateTime,
    ) -> Result<(), StoreError> {
        if !CLASSIFICATION_KINDS.contains(&kind) {
            return Err(StoreError::InvalidClassification {
                field: "kind",
                value: kind.to_string(),
            });
        }
        if !CLASSIFICATION_SCOPES.contains(&scope) {
            return Err(StoreError::InvalidClassification {
                field: "scope",
                value: scope.to_string(),
            });
        }
        let at = rfc3339_text(now)?;
        let tx = self.conn.transaction().map_err(backend)?;
        let exists: bool = tx
            .query_row(
                "SELECT EXISTS(SELECT 1 FROM capsules WHERE id = ?1)",
                [capsule_id],
                |row| row.get(0),
            )
            .map_err(backend)?;
        if !exists {
            return Err(StoreError::UnknownCapsule(capsule_id.to_string()));
        }
        tx.execute(
            "INSERT INTO classifications (capsule_id, kind, scope, at) \
             VALUES (?1, ?2, ?3, ?4) \
             ON CONFLICT(capsule_id) DO UPDATE SET \
                 kind = excluded.kind, \
                 scope = excluded.scope, \
                 at = excluded.at",
            params![capsule_id, kind, scope, at],
        )
        .map_err(backend)?;
        tx.commit().map_err(backend)?;
        Ok(())
    }

    /// `capsule_id`'s classification label; `Ok(None)` when never
    /// classified.
    pub fn get_classification(
        &self,
        capsule_id: &str,
    ) -> Result<Option<ClassificationRecord>, StoreError> {
        let row = self
            .conn
            .query_row(
                "SELECT kind, scope, at FROM classifications WHERE capsule_id = ?1",
                [capsule_id],
                |row| {
                    Ok((
                        row.get::<_, String>(0)?,
                        row.get::<_, String>(1)?,
                        row.get::<_, String>(2)?,
                    ))
                },
            )
            .optional()
            .map_err(backend)?;
        match row {
            None => Ok(None),
            Some((kind, scope, at_text)) => Ok(Some(ClassificationRecord {
                kind,
                scope,
                at: parse_at(capsule_id, "classifications.at", &at_text)?,
            })),
        }
    }

    /// Capsule ids whose PERSISTED classification kind is `epic` (S2
    /// effort-lifecycle), in ascending id order. ONE indexed query over
    /// `classifications` (`idx_classifications_kind`) — the digest/bootstrap
    /// open-efforts projection resolves its epic universe here rather than
    /// fanning `get_classification` over every capsule. A tombstoned capsule
    /// keeps its classification row, so callers still intersect the result
    /// with the live/scope set before surfacing a row.
    pub fn list_epic_ids(&self) -> Result<Vec<String>, StoreError> {
        let mut stmt = self
            .conn
            .prepare(
                "SELECT capsule_id FROM classifications \
                 WHERE kind = 'epic' ORDER BY capsule_id",
            )
            .map_err(backend)?;
        let rows = stmt
            .query_map([], |row| row.get::<_, String>(0))
            .map_err(backend)?;
        let mut out = Vec::new();
        for row in rows {
            out.push(row.map_err(backend)?);
        }
        Ok(out)
    }

    /// Whether a capsule row named `id` exists AT ALL — live OR a tombstoned
    /// skeleton (forget only NULLs `canonical_json`, the row persists). S3
    /// effort resolution reads this FIRST: a missing row is `unknown_capsule`,
    /// a present-but-tombstoned row is the distinct `tombstoned_capsule`
    /// state. A slug is simply a non-existent id — it never resolves scope.
    pub fn capsule_exists(&self, id: &str) -> Result<bool, StoreError> {
        self.conn
            .query_row(
                "SELECT EXISTS(SELECT 1 FROM capsules WHERE id = ?1)",
                [id],
                |row| row.get(0),
            )
            .map_err(backend)
    }

    /// Whether any `witnesses` edge names `id` as its target — the u-r3
    /// proof-carrying closure signal. S3 effort resolution reads it (with
    /// [`Store::is_superseded`]) to compute the echoed `open` flag: a
    /// witnessed OR superseded epic is CLOSED (`open:false`) yet still
    /// queryable for post-mortem recall. An unknown id is simply not
    /// witnessed — `false`, never an error.
    pub fn is_witnessed(&self, id: &str) -> Result<bool, StoreError> {
        self.conn
            .query_row(
                "SELECT EXISTS(SELECT 1 FROM relations \
                 WHERE kind = 'witnesses' AND to_id = ?1)",
                [id],
                |row| row.get(0),
            )
            .map_err(backend)
    }

    /// The effort's members: the `from` side of every `part_of` edge pointing
    /// AT `epic` (1-hop INTO the container, non-transitive — a sub-effort is
    /// scoped by its own sub-epic id). GRAPH TRUTH: every member is returned,
    /// including dead ones (superseded / falsified / archived / tombstoned) —
    /// the fence is SCOPE, downstream eligibility is untouched, and the count
    /// matches digest/bootstrap's `member_total` (cross-surface parity).
    /// Distinct `from_id`, ascending, for a deterministic id-set.
    pub fn effort_members(&self, epic: &str) -> Result<Vec<String>, StoreError> {
        let mut stmt = self
            .conn
            .prepare(
                "SELECT DISTINCT from_id FROM relations \
                 WHERE kind = 'part_of' AND to_id = ?1 ORDER BY from_id",
            )
            .map_err(backend)?;
        let rows = stmt
            .query_map([epic], |row| row.get::<_, String>(0))
            .map_err(backend)?;
        let mut out = Vec::new();
        for row in rows {
            out.push(row.map_err(backend)?);
        }
        Ok(out)
    }

    /// Record the CAPTURE-TIME hash of `capsule_id`'s anchored file (u-r2
    /// anchor-drift; see [`ANCHOR_HASHES_DDL`]): `hash` is the SHA-256 hex
    /// of the anchored file's bytes, computed by the BOUNDARY through the
    /// same fail-closed root fence the `anchor_live` probe uses — the
    /// store persists, it never touches the filesystem. The capsule must
    /// be stored ([`StoreError::UnknownCapsule`]). Keep-first: the capture
    /// instant is the only honest comparison base, so a second write for
    /// the same capsule is a no-op keeping the FIRST row (returns `false`;
    /// a fresh record returns `true`).
    pub fn set_anchor_hash(
        &mut self,
        capsule_id: &str,
        hash: &str,
        now: OffsetDateTime,
    ) -> Result<bool, StoreError> {
        if hash.trim().is_empty() {
            return Err(StoreError::EmptyField("anchor hash"));
        }
        let at = rfc3339_text(now)?;
        let tx = self.conn.transaction().map_err(backend)?;
        let exists: bool = tx
            .query_row(
                "SELECT EXISTS(SELECT 1 FROM capsules WHERE id = ?1)",
                [capsule_id],
                |row| row.get(0),
            )
            .map_err(backend)?;
        if !exists {
            return Err(StoreError::UnknownCapsule(capsule_id.to_string()));
        }
        let inserted = tx
            .execute(
                "INSERT OR IGNORE INTO anchor_hashes (capsule_id, hash, at) \
                 VALUES (?1, ?2, ?3)",
                params![capsule_id, hash, at],
            )
            .map_err(backend)?;
        tx.commit().map_err(backend)?;
        Ok(inserted == 1)
    }

    /// The capture-time anchored-file hash of `capsule_id`; `Ok(None)`
    /// when none was recorded (non-path anchor, fence-rejected path, or a
    /// file the boundary could not read at capture) — recall then answers
    /// `anchor_drift: "unknown"`, never a guess.
    pub fn anchor_hash_of(&self, capsule_id: &str) -> Result<Option<String>, StoreError> {
        self.conn
            .query_row(
                "SELECT hash FROM anchor_hashes WHERE capsule_id = ?1",
                [capsule_id],
                |row| row.get(0),
            )
            .optional()
            .map_err(backend)
    }

    /// Merge `capsule_id`'s epistemic annotations (u-r2; see
    /// [`EPISTEMICS_DDL`]): each `Some` field replaces its column, each
    /// `None` LEAVES the stored value — setting `evidence_state` never
    /// erases a recorded `proof_hint`, and vice versa (per-field merge, a
    /// deliberate delta from the classification upsert whose two fields
    /// are jointly mandatory). An all-`None` call records nothing and is
    /// `Ok`. `evidence_state` outside [`EVIDENCE_STATES`] is the teaching
    /// [`StoreError::InvalidEvidenceState`]; the capsule must be stored
    /// ([`StoreError::UnknownCapsule`]; tombstoned still counts — the
    /// annotation is about the record). `proof_hint` / `stale_if` are
    /// ADVISORY STRINGS: persisted and surfaced verbatim, NEVER executed
    /// or evaluated by any code path.
    pub fn set_epistemics(
        &mut self,
        capsule_id: &str,
        evidence_state: Option<&str>,
        proof_hint: Option<&str>,
        stale_if: Option<&str>,
        now: OffsetDateTime,
    ) -> Result<(), StoreError> {
        if let Some(state) = evidence_state
            && !EVIDENCE_STATES.contains(&state)
        {
            return Err(StoreError::InvalidEvidenceState(state.to_string()));
        }
        if evidence_state.is_none() && proof_hint.is_none() && stale_if.is_none() {
            return Ok(());
        }
        let at = rfc3339_text(now)?;
        let tx = self.conn.transaction().map_err(backend)?;
        let exists: bool = tx
            .query_row(
                "SELECT EXISTS(SELECT 1 FROM capsules WHERE id = ?1)",
                [capsule_id],
                |row| row.get(0),
            )
            .map_err(backend)?;
        if !exists {
            return Err(StoreError::UnknownCapsule(capsule_id.to_string()));
        }
        // Per-field merge: COALESCE keeps the stored value where the call
        // passed `None` (`excluded.<col>` is NULL there).
        tx.execute(
            "INSERT INTO epistemics \
                 (capsule_id, evidence_state, proof_hint, stale_if, at) \
             VALUES (?1, ?2, ?3, ?4, ?5) \
             ON CONFLICT(capsule_id) DO UPDATE SET \
                 evidence_state = COALESCE(excluded.evidence_state, evidence_state), \
                 proof_hint     = COALESCE(excluded.proof_hint, proof_hint), \
                 stale_if       = COALESCE(excluded.stale_if, stale_if), \
                 at             = excluded.at",
            params![capsule_id, evidence_state, proof_hint, stale_if, at],
        )
        .map_err(backend)?;
        tx.commit().map_err(backend)?;
        Ok(())
    }

    /// `capsule_id`'s epistemic annotations; `Ok(None)` when never
    /// annotated. A returned record carries at least one `Some` payload
    /// field ([`Store::set_epistemics`] refuses to materialize an empty
    /// row).
    pub fn epistemics_of(&self, capsule_id: &str) -> Result<Option<EpistemicsRecord>, StoreError> {
        let row = self
            .conn
            .query_row(
                "SELECT evidence_state, proof_hint, stale_if, at \
                 FROM epistemics WHERE capsule_id = ?1",
                [capsule_id],
                |row| {
                    Ok((
                        row.get::<_, Option<String>>(0)?,
                        row.get::<_, Option<String>>(1)?,
                        row.get::<_, Option<String>>(2)?,
                        row.get::<_, String>(3)?,
                    ))
                },
            )
            .optional()
            .map_err(backend)?;
        match row {
            None => Ok(None),
            Some((evidence_state, proof_hint, stale_if, at_text)) => Ok(Some(EpistemicsRecord {
                evidence_state,
                proof_hint,
                stale_if,
                at: parse_at(capsule_id, "epistemics.at", &at_text)?,
            })),
        }
    }

    /// Append one grounded recall receipt (u03; see
    /// [`RECALL_RECEIPTS_DDL`]), returning its deterministic `rcpt-<seq>` id.
    /// The raw caller `terms` and the `returned_ids` in response order are
    /// stored as JSON arrays; project fences and `session_id` retain their
    /// exact nullable boundary values. `now` is injected — the store reads no
    /// clock. One transaction mints `MAX(seq)+1` and inserts the row, so a
    /// returned id always names a committed receipt. Any backend or
    /// serialization failure propagates honestly: this ledger is
    /// FAIL-CLOSED because feedback addresses it by id.
    pub fn record_recall_receipt(
        &mut self,
        terms: &[String],
        returned_ids: &[&str],
        project_id: Option<&str>,
        project_prefix: Option<&str>,
        session_id: Option<&str>,
        now: OffsetDateTime,
    ) -> Result<String, StoreError> {
        let terms_json = serde_json::to_string(terms)
            .map_err(|error| StoreError::Serialize(error.to_string()))?;
        let returned_ids_json = serde_json::to_string(returned_ids)
            .map_err(|error| StoreError::Serialize(error.to_string()))?;
        let at = rfc3339_text(now)?;
        let tx = self.conn.transaction().map_err(backend)?;
        let seq: i64 = tx
            .query_row(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM recall_receipts",
                [],
                |row| row.get(0),
            )
            .map_err(backend)?;
        let id = format!("rcpt-{seq}");
        tx.execute(
            "INSERT INTO recall_receipts \
             (seq, id, terms, returned_ids, project_id, project_prefix, session_id, at) \
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)",
            params![
                seq,
                id,
                terms_json,
                returned_ids_json,
                project_id,
                project_prefix,
                session_id,
                at,
            ],
        )
        .map_err(backend)?;
        tx.commit().map_err(backend)?;
        Ok(id)
    }

    /// Resolve a grounded recall receipt to the capsule ids returned on that
    /// response, preserving response order. `Ok(None)` means the id is
    /// unknown. Persisted JSON that is not a string array fails safely as the
    /// named [`StoreError::Corrupt`] row instead of fabricating an empty set.
    pub fn receipt_returned_ids(
        &self,
        receipt_id: &str,
    ) -> Result<Option<Vec<String>>, StoreError> {
        receipt_returned_ids_on(&self.conn, receipt_id)
    }

    /// Append one successful explicit lane override. Callers can supply only
    /// the closed [`LaneOverride`] disagreement set; there is no raw-string
    /// writer. The method reports persistence errors honestly. Retrieve's
    /// public wrapper deliberately swallows that error because this table is
    /// advisory telemetry and never part of recall correctness.
    pub(crate) fn record_lane_override(
        &mut self,
        override_: LaneOverride,
        now: OffsetDateTime,
    ) -> Result<(), StoreError> {
        let (forced, auto_pick) = override_.pair();
        let at = rfc3339_text(now)?;
        self.conn
            .execute(
                "INSERT INTO lane_overrides (forced, auto_pick, at) VALUES (?1, ?2, ?3)",
                params![forced, auto_pick, at],
            )
            .map_err(backend)?;
        Ok(())
    }

    /// Aggregate successful explicit lane overrides in deterministic
    /// `(forced asc, auto_pick asc)` order. Every contributing row is read:
    /// its auto-minted sequence must be positive, its pair is re-validated
    /// against the closed [`LaneOverride`] type, and its timestamp is parsed
    /// as RFC3339 before it can contribute. A malformed row fails as
    /// [`StoreError::Corrupt`], even if a hand-shaped current-version table
    /// omitted the SQL CHECK. This is the sole u05-to-u10 consumption
    /// interface; individual telemetry rows stay internal.
    pub fn lane_override_totals(&self) -> Result<Vec<(String, String, i64)>, StoreError> {
        let mut stmt = self
            .conn
            .prepare("SELECT seq, forced, auto_pick, at FROM lane_overrides ORDER BY seq ASC")
            .map_err(backend)?;
        let rows = stmt
            .query_map([], |row| {
                Ok((
                    row.get::<_, i64>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, String>(2)?,
                    row.get::<_, String>(3)?,
                ))
            })
            .map_err(backend)?;
        let mut totals = BTreeMap::<(String, String), i64>::new();
        for row in rows {
            let (seq, forced, auto_pick, at) = row.map_err(backend)?;
            let id = format!("lane_overrides:{seq}");
            if seq <= 0 {
                return Err(StoreError::Corrupt {
                    id,
                    reason: format!("lane override seq must be positive, got {seq}"),
                });
            }
            if !LaneOverride::contains_pair(&forced, &auto_pick) {
                return Err(StoreError::Corrupt {
                    id,
                    reason: format!(
                        "illegal lane override pair forced={forced:?}, auto_pick={auto_pick:?}"
                    ),
                });
            }
            OffsetDateTime::parse(&at, &Rfc3339).map_err(|error| StoreError::Corrupt {
                id: id.clone(),
                reason: format!("lane override timestamp is not RFC3339: {error}"),
            })?;
            let count = totals.entry((forced, auto_pick)).or_insert(0);
            *count = count.checked_add(1).ok_or_else(|| StoreError::Corrupt {
                id,
                reason: "lane override aggregate count overflowed i64".to_string(),
            })?;
        }
        Ok(totals
            .into_iter()
            .map(|((forced, auto_pick), count)| (forced, auto_pick, count))
            .collect())
    }

    /// Append the recall-miss ledger rows for one pre-trim term-lane miss
    /// (u-r5 miss-ledger; see [`RECALL_MISSES_DDL`]): fold each `term` exactly
    /// like [`Store::add_alias`]'s key ([`fold_term`]: trim + lowercase +
    /// diacritic-fold), then insert ONE row per UNIQUE folded term with the
    /// injected `at` and the closed [`RecallMissOutcome`]. A term that
    /// folds to empty (no alphanumeric) is dropped — it carries no
    /// vocabulary signal — and a folded term is recorded once even if the
    /// query repeated it, so `COUNT(*) GROUP BY term` is a per-query count.
    /// Returns how many rows were inserted (`0` when no term carried a
    /// searchable token). APPEND-ONLY: no verb updates or deletes a row.
    ///
    /// A term with NO alphanumeric character (punctuation- or
    /// whitespace-only) is dropped — it carries no vocabulary signal and is
    /// exactly what the retrieve search fence also drops.
    ///
    /// Telemetry semantics: recall calls this only when FTS ran and the
    /// pre-trim term observation missed; it calls it FAIL-OPEN
    /// ([`crate::retrieve`] swallows the `Err`) so a ledger write can never
    /// fail or delay the retrieve — the deliberate exception to the crate's
    /// fail-closed default. The method itself still returns the error
    /// HONESTLY; the swallow lives at exactly one call site.
    pub fn record_recall_miss(
        &mut self,
        terms: &[String],
        outcome: RecallMissOutcome,
        now: OffsetDateTime,
    ) -> Result<usize, StoreError> {
        let at = rfc3339_text(now)?;
        let mut folded_terms: Vec<String> = Vec::new();
        for term in terms {
            let folded = fold_term(term);
            if !folded.chars().any(char::is_alphanumeric)
                || folded_terms.iter().any(|t| t == &folded)
            {
                continue;
            }
            folded_terms.push(folded);
        }
        if folded_terms.is_empty() {
            return Ok(0);
        }
        let tx = self.conn.transaction().map_err(backend)?;
        {
            // `seq` is the INTEGER PRIMARY KEY (rowid) — SQLite assigns the
            // next value, so no manual MAX(seq)+1 and no minted id (the
            // ledger is internal telemetry, never referenced by id).
            let mut stmt = tx
                .prepare("INSERT INTO recall_misses (term, outcome, at) VALUES (?1, ?2, ?3)")
                .map_err(backend)?;
            for term in &folded_terms {
                stmt.execute(params![term, outcome.as_str(), at])
                    .map_err(backend)?;
            }
        }
        tx.commit().map_err(backend)?;
        Ok(folded_terms.len())
    }

    /// Read at most `n` recall-miss ROWS newest-first by append sequence.
    /// `n` counts folded terms, not queries: a multi-term miss writes one row
    /// per unique folded term, and descending sequence therefore reverses that
    /// query's insertion order. Every selected row is re-validated before the
    /// vector is returned: positive sequence, canonical non-empty folded term,
    /// closed outcome, and RFC3339 timestamp. One bad row returns a typed
    /// [`StoreError::Corrupt`] and no partial result.
    pub fn recent_recall_misses(&self, n: usize) -> Result<Vec<RecallMissRow>, StoreError> {
        let limit = i64::try_from(n)
            .map_err(|e| StoreError::Backend(format!("recent recall-miss limit: {e}")))?;
        let mut stmt = self
            .conn
            .prepare(
                "SELECT seq, term, outcome, at
                 FROM recall_misses
                 ORDER BY seq DESC
                 LIMIT ?1",
            )
            .map_err(backend)?;
        let rows = stmt
            .query_map([limit], |row| {
                Ok((
                    row.get::<_, i64>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, String>(2)?,
                    row.get::<_, String>(3)?,
                ))
            })
            .map_err(backend)?;
        let mut out = Vec::new();
        for row in rows {
            let (seq, term, outcome_text, at_text) = row.map_err(backend)?;
            let id = format!("recall_misses:{seq}");
            if seq <= 0 {
                return Err(StoreError::Corrupt {
                    id,
                    reason: format!("recall miss seq must be positive, got {seq}"),
                });
            }
            let folded = fold_term(&term);
            if term.is_empty() || !term.chars().any(char::is_alphanumeric) || folded != term {
                return Err(StoreError::Corrupt {
                    id,
                    reason: format!(
                        "recall miss term must be a canonical non-empty folded term, got {term:?}"
                    ),
                });
            }
            let outcome =
                RecallMissOutcome::from_wire(&outcome_text).ok_or_else(|| StoreError::Corrupt {
                    id: id.clone(),
                    reason: format!("illegal recall miss outcome {outcome_text:?}"),
                })?;
            let at =
                OffsetDateTime::parse(&at_text, &Rfc3339).map_err(|error| StoreError::Corrupt {
                    id: id.clone(),
                    reason: format!("recall miss timestamp is not RFC3339: {error}"),
                })?;
            out.push(RecallMissRow {
                seq,
                term,
                outcome,
                at,
            });
        }
        Ok(out)
    }

    /// Every recorded miss term with its miss_count — `(term, count)` pairs
    /// (u-r5), deterministic `term asc` order (the planner re-sorts by
    /// count desc). `count` is `COUNT(*) GROUP BY term`; since a query's
    /// terms are deduplicated before recording, it is the number of missing
    /// queries that carried the term. Empty on a fresh store; never an
    /// error. Feeds [`crate::consolidate::alias_proposals`].
    pub fn recall_miss_terms(&self) -> Result<Vec<(String, i64)>, StoreError> {
        let mut stmt = self
            .conn
            .prepare("SELECT term, COUNT(*) FROM recall_misses GROUP BY term ORDER BY term")
            .map_err(backend)?;
        let rows = stmt
            .query_map([], |row| {
                Ok((row.get::<_, String>(0)?, row.get::<_, i64>(1)?))
            })
            .map_err(backend)?;
        let mut out = Vec::new();
        for row in rows {
            out.push(row.map_err(backend)?);
        }
        Ok(out)
    }

    /// Total rows in the recall-miss ledger (u-r5) — the additive digest
    /// counter beside `audit_events`. Empty store → `0`; never an error.
    pub fn count_recall_misses(&self) -> Result<usize, StoreError> {
        let count: i64 = self
            .conn
            .query_row("SELECT COUNT(*) FROM recall_misses", [], |row| row.get(0))
            .map_err(backend)?;
        usize::try_from(count).map_err(|e| StoreError::Backend(format!("recall_misses count: {e}")))
    }

    /// Append one git-witness verdict for `capsule_id` (S2 git witness lane;
    /// see [`CORROBORATIONS_DDL`]) — APPEND-ONLY and CHANGE-GATED: a row is
    /// written only when the newest existing verdict for the same
    /// (`source`, `kind`, `ref`) DIFFERS (or none exists), so re-scanning an
    /// unchanged tree writes nothing (returns `false`; a fresh observation
    /// returns `true`). The closed `source`/`kind`/`verdict` vocabularies are
    /// enforced by the SQL CHECK — an illegal value is a
    /// [`StoreError::Backend`], never a silent write. The caller guarantees
    /// `capsule_id` names a stored capsule (the git-scan verb enumerates
    /// [`Store::list`] for anchors and probes [`Store::get`] for mentions).
    /// `now` is INJECTED; the store reads no clock. NEVER mutates a capsule's
    /// confidence, authority, or tier — a witness observes, it does not
    /// decide.
    #[allow(
        clippy::too_many_arguments,
        reason = "the witness boundary keeps each corroboration field explicit and CHECK-validated"
    )]
    pub fn append_corroboration(
        &mut self,
        capsule_id: &str,
        source: &str,
        kind: &str,
        ref_: &str,
        verdict: &str,
        detail: Option<&str>,
        now: OffsetDateTime,
    ) -> Result<bool, StoreError> {
        let at = rfc3339_text(now)?;
        let tx = self.conn.transaction().map_err(backend)?;
        let latest: Option<String> = tx
            .query_row(
                "SELECT verdict FROM corroborations \
                 WHERE capsule_id = ?1 AND source = ?2 AND kind = ?3 AND ref = ?4 \
                 ORDER BY seq DESC LIMIT 1",
                params![capsule_id, source, kind, ref_],
                |row| row.get(0),
            )
            .optional()
            .map_err(backend)?;
        if latest.as_deref() == Some(verdict) {
            // Unchanged verdict — the read-only transaction rolls back on
            // drop (a no-op), so a re-scan churns nothing.
            return Ok(false);
        }
        tx.execute(
            "INSERT INTO corroborations \
             (capsule_id, source, kind, ref, verdict, detail, at) \
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
            params![capsule_id, source, kind, ref_, verdict, detail, at],
        )
        .map_err(backend)?;
        tx.commit().map_err(backend)?;
        Ok(true)
    }

    /// The newest witness observations about `capsule_id` (S2 git witness
    /// lane), folded to one verdict per anchor kind plus a mention tally —
    /// `Ok(None)` when nothing was ever recorded. Rows are read seq-ascending
    /// so the LAST seen per kind is the latest; `git_ref` is the newest
    /// recorded scan `HEAD` (`detail`). Derived explain for the retrieve
    /// envelope; never authority.
    pub fn latest_corroborations(
        &self,
        capsule_id: &str,
    ) -> Result<Option<CorroborationSummary>, StoreError> {
        let mut stmt = self
            .conn
            .prepare(
                "SELECT source, kind, verdict, detail, at FROM corroborations \
                 WHERE capsule_id = ?1 ORDER BY seq ASC",
            )
            .map_err(backend)?;
        let rows = stmt
            .query_map([capsule_id], |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, String>(2)?,
                    row.get::<_, Option<String>>(3)?,
                    row.get::<_, String>(4)?,
                ))
            })
            .map_err(backend)?;
        let mut summary: Option<CorroborationSummary> = None;
        let mut mentions = 0usize;
        for row in rows {
            let (source, kind, verdict, detail, at) = row.map_err(backend)?;
            let entry = summary.get_or_insert_with(|| CorroborationSummary {
                source: source.clone(),
                git_ref: None,
                anchor_path: None,
                anchor_sha: None,
                anchor_content: None,
                mentions: 0,
                at: String::new(),
            });
            entry.source = source;
            entry.at = at; // seq-asc ⇒ ends at the newest row's instant.
            if detail.is_some() {
                entry.git_ref = detail; // newest recorded scan HEAD wins.
            }
            match kind.as_str() {
                "anchor_path" => entry.anchor_path = Some(verdict),
                "anchor_sha" => entry.anchor_sha = Some(verdict),
                "anchor_content" => entry.anchor_content = Some(verdict),
                "mention" => mentions += 1,
                _ => {}
            }
        }
        if let Some(entry) = summary.as_mut() {
            entry.mentions = mentions;
        }
        Ok(summary)
    }

    /// Store-global corroboration tallies grouped by witness source (S2 git
    /// witness lane), counting the LATEST verdict per (capsule_id, kind, ref)
    /// so a superseded earlier verdict never double-counts — the digest
    /// `sources` section's per-source counts. Empty map when nothing was
    /// recorded.
    pub fn corroboration_counts(
        &self,
    ) -> Result<BTreeMap<String, CorroborationCounts>, StoreError> {
        let mut stmt = self
            .conn
            .prepare(
                "SELECT c.source, c.kind, c.verdict FROM corroborations c \
                 WHERE c.seq = (SELECT MAX(c2.seq) FROM corroborations c2 \
                                WHERE c2.capsule_id = c.capsule_id \
                                  AND c2.kind = c.kind AND c2.ref = c.ref)",
            )
            .map_err(backend)?;
        let rows = stmt
            .query_map([], |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, String>(2)?,
                ))
            })
            .map_err(backend)?;
        let mut counts: BTreeMap<String, CorroborationCounts> = BTreeMap::new();
        for row in rows {
            let (source, kind, verdict) = row.map_err(backend)?;
            let entry = counts.entry(source).or_default();
            if kind == "mention" {
                entry.mentions += 1;
            } else {
                match verdict.as_str() {
                    "corroborated" => entry.corroborated += 1,
                    "drifted" => entry.drifted += 1,
                    "missing" => entry.missing += 1,
                    _ => {}
                }
            }
        }
        Ok(counts)
    }

    /// Read the git-scan cursor for `source_key` (S2 git witness lane; see
    /// [`SOURCE_CURSORS_DDL`]) — the last scanned commit sha, or `Ok(None)`
    /// when the source was never scanned.
    pub fn get_source_cursor(&self, source_key: &str) -> Result<Option<String>, StoreError> {
        source_cursor_on(&self.conn, source_key)
    }

    /// All git-scan cursors, `(source_key, cursor, at)`, source_key-sorted
    /// (S2 git witness lane) — the digest `sources` section input. Empty when
    /// no source was ever scanned.
    pub fn list_source_cursors(&self) -> Result<Vec<(String, String, String)>, StoreError> {
        let mut stmt = self
            .conn
            .prepare("SELECT source_key, cursor, at FROM source_cursors ORDER BY source_key ASC")
            .map_err(backend)?;
        let rows = stmt
            .query_map([], |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, String>(2)?,
                ))
            })
            .map_err(backend)?;
        let mut out = Vec::new();
        for row in rows {
            out.push(row.map_err(backend)?);
        }
        Ok(out)
    }

    /// Advance the git-scan cursor for `source_key` to `cursor` at the
    /// injected `now` (S2 git witness lane) — REPLACE in place (PRIMARY KEY
    /// on `source_key`), never accumulated. Empty inputs fail closed.
    pub fn set_source_cursor(
        &mut self,
        source_key: &str,
        cursor: &str,
        now: OffsetDateTime,
    ) -> Result<(), StoreError> {
        if source_key.trim().is_empty() {
            return Err(StoreError::EmptyField("source_key"));
        }
        if cursor.trim().is_empty() {
            return Err(StoreError::EmptyField("cursor"));
        }
        let at = rfc3339_text(now)?;
        self.conn
            .execute(
                "INSERT INTO source_cursors (source_key, cursor, at) VALUES (?1, ?2, ?3) \
                 ON CONFLICT(source_key) DO UPDATE SET cursor = excluded.cursor, at = excluded.at",
                params![source_key, cursor, at],
            )
            .map_err(backend)?;
        Ok(())
    }

    /// Read the durable page checkpoint for an incomplete git-history scan.
    /// The completed cursor remains unchanged until this fixed target is
    /// fully consumed.
    pub fn get_source_backfill(
        &self,
        source_key: &str,
    ) -> Result<Option<SourceBackfill>, StoreError> {
        source_backfill_on(&self.conn, source_key)
    }

    /// Persist the next page offset for a fixed git-history range with a
    /// compare-and-swap guard. Offset zero starts a new traversal; later
    /// calls must match the exact base, target, and prior offset.
    pub fn checkpoint_source_backfill(
        &mut self,
        source_key: &str,
        base_cursor: Option<&str>,
        target_head: &str,
        expected_offset: usize,
        next_offset: usize,
        now: OffsetDateTime,
    ) -> Result<(), StoreError> {
        validate_source_backfill_fields(source_key, base_cursor, target_head, next_offset)?;
        if next_offset <= expected_offset {
            return Err(StoreError::Corrupt {
                id: source_key.to_string(),
                reason: format!(
                    "source backfill offset must advance ({expected_offset} -> {next_offset})"
                ),
            });
        }
        let expected_offset = i64::try_from(expected_offset).map_err(|_| StoreError::Corrupt {
            id: source_key.to_string(),
            reason: "source backfill expected offset exceeds i64".to_string(),
        })?;
        let next_offset = i64::try_from(next_offset).map_err(|_| StoreError::Corrupt {
            id: source_key.to_string(),
            reason: "source backfill next offset exceeds i64".to_string(),
        })?;
        let at = rfc3339_text(now)?;
        let tx = self
            .conn
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(backend)?;
        require_source_cursor(&tx, source_key, base_cursor)?;
        let changed = if expected_offset == 0 {
            tx.execute(
                "INSERT INTO source_backfills \
                     (source_key, base_cursor, target_head, next_offset, at) \
                     SELECT ?1, ?2, ?3, ?4, ?5 \
                     WHERE NOT EXISTS \
                       (SELECT 1 FROM source_backfills WHERE source_key = ?1)",
                params![source_key, base_cursor, target_head, next_offset, at],
            )
            .map_err(backend)?
        } else {
            tx.execute(
                "UPDATE source_backfills \
                     SET next_offset = ?5, at = ?6 \
                     WHERE source_key = ?1 \
                       AND base_cursor IS ?2 \
                       AND target_head = ?3 \
                       AND next_offset = ?4",
                params![
                    source_key,
                    base_cursor,
                    target_head,
                    expected_offset,
                    next_offset,
                    at
                ],
            )
            .map_err(backend)?
        };
        if changed != 1 {
            return Err(StoreError::StaleSourceBackfill(source_key.to_string()));
        }
        tx.commit().map_err(backend)
    }

    /// Atomically publish a fully consumed target and remove its checkpoint.
    /// A paged completion must still match the state observed by the caller.
    pub fn complete_source_backfill(
        &mut self,
        source_key: &str,
        base_cursor: Option<&str>,
        target_head: &str,
        expected_offset: usize,
        now: OffsetDateTime,
    ) -> Result<(), StoreError> {
        validate_source_backfill_fields(source_key, base_cursor, target_head, 1)?;
        let expected_offset = i64::try_from(expected_offset).map_err(|_| StoreError::Corrupt {
            id: source_key.to_string(),
            reason: "source backfill expected offset exceeds i64".to_string(),
        })?;
        let at = rfc3339_text(now)?;
        let tx = self
            .conn
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(backend)?;
        require_source_cursor(&tx, source_key, base_cursor)?;
        if expected_offset == 0 {
            let exists: bool = tx
                .query_row(
                    "SELECT EXISTS(SELECT 1 FROM source_backfills WHERE source_key = ?1)",
                    [source_key],
                    |row| row.get(0),
                )
                .map_err(backend)?;
            if exists {
                return Err(StoreError::StaleSourceBackfill(source_key.to_string()));
            }
        } else {
            let removed = tx
                .execute(
                    "DELETE FROM source_backfills \
                     WHERE source_key = ?1 \
                       AND base_cursor IS ?2 \
                       AND target_head = ?3 \
                       AND next_offset = ?4",
                    params![source_key, base_cursor, target_head, expected_offset],
                )
                .map_err(backend)?;
            if removed != 1 {
                return Err(StoreError::StaleSourceBackfill(source_key.to_string()));
            }
        }
        tx.execute(
            "INSERT INTO source_cursors (source_key, cursor, at) VALUES (?1, ?2, ?3) \
             ON CONFLICT(source_key) DO UPDATE SET cursor = excluded.cursor, at = excluded.at",
            params![source_key, target_head, at],
        )
        .map_err(backend)?;
        tx.commit().map_err(backend)
    }

    /// Atomically discard an obsolete completed cursor and in-progress page
    /// checkpoint after a history range can no longer be read. Both rows
    /// must still match the state observed by the caller.
    pub fn clear_source_traversal(
        &mut self,
        source_key: &str,
        expected_cursor: Option<&str>,
        expected_backfill: Option<&SourceBackfill>,
    ) -> Result<(), StoreError> {
        if source_key.trim().is_empty() {
            return Err(StoreError::EmptyField("source_key"));
        }
        let tx = self
            .conn
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(backend)?;
        require_source_cursor(&tx, source_key, expected_cursor)?;
        if source_backfill_on(&tx, source_key)?.as_ref() != expected_backfill {
            return Err(StoreError::StaleSourceBackfill(source_key.to_string()));
        }
        tx.execute(
            "DELETE FROM source_cursors WHERE source_key = ?1",
            [source_key],
        )
        .map_err(backend)?;
        tx.execute(
            "DELETE FROM source_backfills WHERE source_key = ?1",
            [source_key],
        )
        .map_err(backend)?;
        tx.commit().map_err(backend)
    }

    /// Record that `capsule_id` is the machine-derived view of
    /// `source_key`'s block whose content hash is `block_hash`
    /// (u-r8-REDESIGN stale-import-supersession; see [`IMPORT_BLOCKS_DDL`]).
    /// The capsule must be stored ([`StoreError::UnknownCapsule`]).
    /// `block_hash` is the capsule's `provenance.source_hash` (content
    /// identity, never a `path:line` position); `ordinal` is advisory
    /// document-order for pairing. Keep-first on the `(source_key,
    /// block_hash)` key: a second write for the same live block is a no-op
    /// (returns `false`; a fresh record returns `true`) — so re-importing
    /// an unchanged block never churns the row, and adopting a SECOND
    /// source_key's already-owned block (multi-owner, bug 2) is equally
    /// idempotent. `at` is the INJECTED `now`; the store reads no clock.
    pub fn record_import_block(
        &mut self,
        source_key: &str,
        block_hash: &str,
        capsule_id: &str,
        ordinal: i64,
        now: OffsetDateTime,
    ) -> Result<bool, StoreError> {
        if source_key.trim().is_empty() {
            return Err(StoreError::EmptyField("import block source_key"));
        }
        if block_hash.trim().is_empty() {
            return Err(StoreError::EmptyField("import block hash"));
        }
        let at = rfc3339_text(now)?;
        let tx = self.conn.transaction().map_err(backend)?;
        let exists: bool = tx
            .query_row(
                "SELECT EXISTS(SELECT 1 FROM capsules WHERE id = ?1)",
                [capsule_id],
                |row| row.get(0),
            )
            .map_err(backend)?;
        if !exists {
            return Err(StoreError::UnknownCapsule(capsule_id.to_string()));
        }
        let inserted = tx
            .execute(
                "INSERT OR IGNORE INTO import_blocks \
                     (source_key, block_hash, capsule_id, ordinal, at) \
                 VALUES (?1, ?2, ?3, ?4, ?5)",
                params![source_key, block_hash, capsule_id, ordinal, at],
            )
            .map_err(backend)?;
        tx.commit().map_err(backend)?;
        Ok(inserted == 1)
    }

    /// Every LIVE import-block lineage row for `source_key`
    /// (u-r8-REDESIGN), ordered by `ordinal` then `block_hash`
    /// (deterministic). Empty when the source was never imported; never an
    /// error. This is the membership set the auto-supersede/revive fence
    /// keys on — a capsule with NO row here (under this source_key) was
    /// not adopted from this source and can never be auto-superseded or
    /// auto-revived on its account.
    pub fn import_blocks_for(&self, source_key: &str) -> Result<Vec<ImportBlockRow>, StoreError> {
        let mut stmt = self
            .conn
            .prepare(
                "SELECT block_hash, capsule_id, ordinal FROM import_blocks \
                 WHERE source_key = ?1 ORDER BY ordinal, block_hash",
            )
            .map_err(backend)?;
        let rows = stmt
            .query_map([source_key], |row| {
                Ok(ImportBlockRow {
                    block_hash: row.get(0)?,
                    capsule_id: row.get(1)?,
                    ordinal: row.get(2)?,
                })
            })
            .map_err(backend)?;
        let mut out = Vec::new();
        for row in rows {
            out.push(row.map_err(backend)?);
        }
        Ok(out)
    }

    /// Drop the import-block lineage row for `(source_key, block_hash)`
    /// (u-r8-REDESIGN): the block is no longer in the source (edited away
    /// or removed), so its row retires and can never mispair a future
    /// re-import. A no-op when the row is absent; never an error. The
    /// capsule and any recorded `supersedes` edge are untouched — only the
    /// live-block map shrinks. When `capsule_id` still carries a row under
    /// a DIFFERENT source_key (multi-owner, bug 2), it stays reachable via
    /// [`Store::import_block_owners`] and therefore stays exempt from
    /// auto-supersede.
    pub fn forget_import_block(
        &mut self,
        source_key: &str,
        block_hash: &str,
    ) -> Result<(), StoreError> {
        self.conn
            .execute(
                "DELETE FROM import_blocks WHERE source_key = ?1 AND block_hash = ?2",
                params![source_key, block_hash],
            )
            .map_err(backend)?;
        Ok(())
    }

    /// Every DISTINCT `source_key` that currently names `capsule_id` in a
    /// LIVE import-block lineage row (u-r8-REDESIGN, bug 2 multi-owner
    /// fix), deterministic ascending order. Empty for a capsule that was
    /// never adopted from an import — a hand-ingested capsule always
    /// answers empty here, which IS the fence. Two or more owners means
    /// two or more re-importable sources currently carry byte-identical
    /// content that resolved to this ONE capsule; auto-supersede MUST skip
    /// a capsule that still has an owner other than the source_key being
    /// re-imported — see
    /// [`crate::server::MemoryServer::apply_import_supersession`].
    pub fn import_block_owners(&self, capsule_id: &str) -> Result<Vec<String>, StoreError> {
        let mut stmt = self
            .conn
            .prepare(
                "SELECT DISTINCT source_key FROM import_blocks \
                 WHERE capsule_id = ?1 ORDER BY source_key",
            )
            .map_err(backend)?;
        let rows = stmt
            .query_map([capsule_id], |row| row.get::<_, String>(0))
            .map_err(backend)?;
        let mut out = Vec::new();
        for row in rows {
            out.push(row.map_err(backend)?);
        }
        Ok(out)
    }

    /// Reverse EXACTLY the recorded `supersedes` edge
    /// `superseder_id --supersedes--> revived_id` (u-r8-REDESIGN bug 1
    /// revive fix): deletes that one relation row, so `revived_id` grounds
    /// again ([`Store::is_superseded`] re-answers `false` for it) unless
    /// some OTHER edge also names it superseded. No other capsule's
    /// supersede state is touched — a targeted reversal, never a blanket
    /// unsupersede.
    ///
    /// THE ORIGIN FENCE (u-r8 round 3, non-negotiable): only an edge with
    /// `origin = 'import'` — one the stale-import mechanism itself wrote
    /// ([`Store::supersede_imported`]) — is deletable here. A `manual`
    /// edge (memory_relate, an ingest `supersedes`) is a caller decision
    /// and survives every machine reversal attempt: the call answers
    /// `false` and the row stays. The machine only unwrites what the
    /// machine wrote.
    ///
    /// Returns `true` when a row was actually removed, `false` on a no-op
    /// (the edge was absent OR manual — never an error). The store
    /// performs no audit of its own; callers audit a revive like any
    /// other mutation (donor: [`Store::supersede`] is likewise unaudited
    /// at this layer — the server records the audit event).
    pub fn unsupersede(
        &mut self,
        revived_id: &str,
        superseder_id: &str,
    ) -> Result<bool, StoreError> {
        let removed = self
            .conn
            .execute(
                "DELETE FROM relations \
                 WHERE kind = 'supersedes' AND from_id = ?1 AND to_id = ?2 \
                 AND origin = 'import'",
                params![superseder_id, revived_id],
            )
            .map_err(backend)?;
        Ok(removed > 0)
    }

    /// q116: the most recent audit-ledger row whose `subject` is `id` —
    /// the API read surface for "who mutated this last?" (`Ok(None)` when
    /// the id was never a mutation subject). The full ledger stays
    /// append-only and SQLite-resident; this is the per-capsule window the
    /// compliance question actually asks.
    pub fn last_mutation_of(&self, id: &str) -> Result<Option<AuditEvent>, StoreError> {
        let row = self
            .conn
            .query_row(
                "SELECT seq, at, actor, action, subject, reason, chained_hash \
                 FROM audit_events WHERE subject = ?1 ORDER BY seq DESC LIMIT 1",
                [id],
                |row| {
                    Ok((
                        row.get::<_, i64>(0)?,
                        row.get::<_, String>(1)?,
                        row.get::<_, String>(2)?,
                        row.get::<_, String>(3)?,
                        row.get::<_, String>(4)?,
                        row.get::<_, Option<String>>(5)?,
                        row.get::<_, String>(6)?,
                    ))
                },
            )
            .optional()
            .map_err(backend)?;
        match row {
            None => Ok(None),
            Some((seq, at_text, actor, action, subject, reason, chained_hash)) => {
                Ok(Some(AuditEvent {
                    seq,
                    at: parse_at(id, "audit_events.at", &at_text)?,
                    actor,
                    action,
                    subject,
                    reason,
                    chained_hash,
                }))
            }
        }
    }

    /// Forget a capsule's content, irreversibly, in one transaction:
    ///
    /// 1. the `capsules` row KEEPS its id/provenance skeleton (`seq`,
    ///    `id`, `created_at` and the derived filter columns — hashes and
    ///    labels, never content bytes) but its `canonical_json` — the one
    ///    content-bearing column — is set to NULL;
    /// 2. the FTS mirror row is emptied in the same transaction (the
    ///    removed content can never match a recall term again);
    /// 3. a `tombstones` row records `mode`, the mandatory non-empty
    ///    `reason` ([`StoreError::EmptyReason`]), the INJECTED `at`, and
    ///    `content_hmac` — a KEYED HMAC-SHA-256 over the former content
    ///    (`hmac_key` is injected by the boundary; keyed means no bulk
    ///    dictionary matching against tombstones, donor `fingerprint.rs`
    ///    behavior). Mode `redacted` additionally retains the capsule's
    ///    `provenance.source`/`provenance.anchor` on the marker (that
    ///    retention IS the documented reason to choose it over `purged`,
    ///    which retains neither).
    ///
    /// With `PRAGMA secure_delete = ON` (set at open) the overwritten
    /// cells are zeroed in the file, not left in free pages. After the
    /// commit, [`Store::get`]/[`Store::find_by_source_hash`] return the
    /// typed [`StoreError::Tombstoned`] marker, [`Store::list`]/
    /// [`Store::search_fts`]/[`Store::canonical_snapshot`] exclude the
    /// row, and re-ingesting the same source is still blocked by the
    /// UNIQUE `source_hash` backstop — forget is sticky, not a silent
    /// resurrection channel. Forgetting an unknown id is
    /// [`StoreError::UnknownCapsule`]; forgetting twice is
    /// [`StoreError::Tombstoned`] (there is no content left to hash).
    /// Usage counters, classifications, relations, and audit rows are
    /// deliberately untouched: they are history/advisory sidecars that
    /// carry no content bytes.
    pub fn forget_capsule(
        &mut self,
        id: &str,
        mode: TombstoneMode,
        reason: &str,
        hmac_key: &[u8],
        now: OffsetDateTime,
    ) -> Result<(), StoreError> {
        if reason.trim().is_empty() {
            return Err(StoreError::EmptyReason);
        }
        let at = rfc3339_text(now)?;
        let tx = self.conn.transaction().map_err(backend)?;
        let row: Option<(i64, Option<String>)> = tx
            .query_row(
                "SELECT seq, canonical_json FROM capsules WHERE id = ?1",
                [id],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .optional()
            .map_err(backend)?;
        let (seq, canonical_json) = match row {
            None => return Err(StoreError::UnknownCapsule(id.to_string())),
            Some((_, None)) => {
                return Err(StoreError::Tombstoned { id: id.to_string() });
            }
            Some((seq, Some(json))) => (seq, json),
        };
        let capsule: Capsule =
            serde_json::from_str(&canonical_json).map_err(|e| StoreError::Corrupt {
                id: id.to_string(),
                reason: format!("canonical_json: {e}"),
            })?;
        let content_hmac = content_hmac_hex(hmac_key, id, capsule.content());
        // Mode law: `redacted` deliberately RETAINS provenance on the
        // marker (the documented reason to choose it over `purged` — an
        // audit can still say where the removed content came from);
        // `purged` retains nothing.
        let (provenance_source, provenance_anchor) = match mode {
            TombstoneMode::Purged => (None, None),
            TombstoneMode::Redacted => (
                Some(capsule.provenance().source.clone()),
                Some(capsule.provenance().anchor.clone()),
            ),
        };
        tx.execute(
            "UPDATE capsules SET canonical_json = NULL WHERE seq = ?1",
            [seq],
        )
        .map_err(backend)?;
        tx.execute("DELETE FROM capsules_fts WHERE rowid = ?1", [seq])
            .map_err(backend)?;
        tx.execute(
            "INSERT INTO capsules_fts (rowid, content) VALUES (?1, '')",
            [seq],
        )
        .map_err(backend)?;
        // fleet-8 c7 F2: forget destroys the vector sidecar WITH the
        // content — an embedding is derived from the destroyed bytes
        // (invertible in principle) and its row would otherwise keep the
        // forgotten id enumerable on memory_vector's list. Both modes
        // cascade; the connection's secure_delete overwrites freed pages.
        tx.execute("DELETE FROM embeddings WHERE capsule_id = ?1", [id])
            .map_err(backend)?;
        tx.execute(
            "INSERT INTO tombstones (capsule_id, mode, content_hmac, at, reason, \
                                     provenance_source, provenance_anchor, source_hash) \
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)",
            params![
                id,
                mode.as_str(),
                content_hmac,
                at,
                reason,
                provenance_source,
                provenance_anchor,
                capsule.provenance().source_hash,
            ],
        )
        .map_err(backend)?;
        tx.commit().map_err(backend)?;
        Ok(())
    }

    /// The tombstone marker for `id`; `Ok(None)` when the capsule was
    /// never forgotten (or never existed — a marker only exists for a
    /// real forget).
    pub fn get_tombstone(&self, id: &str) -> Result<Option<TombstoneRecord>, StoreError> {
        let row = self
            .conn
            .query_row(
                "SELECT capsule_id, mode, content_hmac, at, reason, \
                        provenance_source, provenance_anchor, source_hash \
                 FROM tombstones WHERE capsule_id = ?1",
                [id],
                row_to_tombstone,
            )
            .optional()
            .map_err(backend)?;
        row.map(RawTombstone::decode).transpose()
    }

    /// The tombstone marker for `id` only when the retained capsule
    /// skeleton carries the exact store-local `session_id` label. This is
    /// the session-supplied recall id probe: the equality is applied in SQL
    /// so another label cannot learn that the marker exists. No `sessions`
    /// row is consulted; finished, orphaned, and merge-imported labels stay
    /// queryable for as long as the capsule skeleton exists.
    pub fn get_tombstone_for_session_label(
        &self,
        id: &str,
        session_id: &str,
    ) -> Result<Option<TombstoneRecord>, StoreError> {
        let row = self
            .conn
            .query_row(
                "SELECT t.capsule_id, t.mode, t.content_hmac, t.at, t.reason, \
                        t.provenance_source, t.provenance_anchor, t.source_hash \
                 FROM tombstones t \
                 JOIN capsules c ON c.id = t.capsule_id \
                 WHERE t.capsule_id = ?1 AND c.session_id = ?2",
                params![id, session_id],
                row_to_tombstone,
            )
            .optional()
            .map_err(backend)?;
        row.map(RawTombstone::decode).transpose()
    }

    /// Every tombstoned capsule id, sorted — the digest's dag projection
    /// input: tombstoned nodes are DEAD to the projection (a destroyed
    /// capsule is never ready work and never gates anything).
    pub fn list_tombstoned_ids(&self) -> Result<Vec<String>, StoreError> {
        let mut stmt = self
            .conn
            .prepare("SELECT capsule_id FROM tombstones ORDER BY capsule_id")
            .map_err(backend)?;
        let rows = stmt
            .query_map([], |row| row.get::<_, String>(0))
            .map_err(backend)?;
        let mut out = Vec::new();
        for row in rows {
            out.push(row.map_err(backend)?);
        }
        Ok(out)
    }

    /// Every tombstone marker as a full [`TombstoneRecord`], ordered by
    /// `capsule_id` — the merge core's LOCAL/INCOMING tombstone input
    /// ([`crate::merge::plan_merge`]). Same per-row decode + validation as
    /// [`Store::get_tombstone`] (an unknown `mode` or unparseable `at` is a
    /// typed [`StoreError::Corrupt`], never a silent skip).
    pub fn all_tombstones(&self) -> Result<Vec<TombstoneRecord>, StoreError> {
        let mut stmt = self
            .conn
            .prepare(
                "SELECT capsule_id, mode, content_hmac, at, reason, \
                        provenance_source, provenance_anchor, source_hash \
                 FROM tombstones ORDER BY capsule_id",
            )
            .map_err(backend)?;
        let rows = stmt
            .query_map([], |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, String>(2)?,
                    row.get::<_, String>(3)?,
                    row.get::<_, String>(4)?,
                    row.get::<_, Option<String>>(5)?,
                    row.get::<_, Option<String>>(6)?,
                    row.get::<_, Option<String>>(7)?,
                ))
            })
            .map_err(backend)?;
        let mut out = Vec::new();
        for row in rows {
            let (
                capsule_id,
                mode_text,
                content_hmac,
                at_text,
                reason,
                provenance_source,
                provenance_anchor,
                source_hash,
            ) = row.map_err(backend)?;
            let mode = TombstoneMode::from_wire(&mode_text).ok_or_else(|| StoreError::Corrupt {
                id: capsule_id.clone(),
                reason: format!("tombstones.mode: unknown value {mode_text:?}"),
            })?;
            let at = parse_at(&capsule_id, "tombstones.at", &at_text)?;
            out.push(TombstoneRecord {
                capsule_id,
                mode,
                content_hmac,
                at,
                reason,
                provenance_source,
                provenance_anchor,
                source_hash,
            });
        }
        Ok(out)
    }

    /// Merge the store at `incoming_path` INTO this one: the imperative
    /// shell around the pure core ([`crate::merge::plan_merge`]). Opens
    /// INCOMING read-only ([`Store::open_readonly`]), reads both sides' core
    /// rows (capsules / relations / tombstones), computes the plan, and
    /// APPLIES it in ONE transaction — atomic and deterministic (no clock,
    /// no randomness; every persisted value is carried from the plan or the
    /// LOCAL row it forgets). A failure at any step rolls the whole
    /// transaction back: LOCAL is either fully merged or untouched, NEVER
    /// partially written.
    ///
    /// Apply, in plan order:
    /// - each new capsule is appended under its PLANNED id — the store
    ///   re-mints the id via its own `MAX(seq)+1` discipline and fails
    ///   closed if it diverges from the plan ([`StoreError::Corrupt`], the
    ///   whole merge rolls back). The capsule's `created_at`/`session_id`
    ///   and its full validated bytes (content, `authority_class`,
    ///   `instruction_taint`, provenance) ride through VERBATIM: a foreign
    ///   capsule is NOT more trusted than an import, so its stored taint is
    ///   carried and authority is NEVER elevated. A capsule whose content
    ///   LOCAL previously forgot hits the UNIQUE `source_hash` backstop
    ///   ([`StoreError::DuplicateSourceHash`]) and fails the merge closed —
    ///   forget is sticky, a merge never silently resurrects it;
    /// - each new relation edge is inserted (`INSERT OR IGNORE`; the plan
    ///   already remapped, deduped, and dropped danglers), carrying `origin`;
    /// - each forget-wins tombstone forgets the resolved LOCAL LIVE capsule
    ///   (content nulled, FTS row emptied, embedding dropped — exactly
    ///   [`Store::forget_capsule`]'s content destruction) and records the
    ///   marker RE-KEYED under LOCAL's key: `content_hmac` is re-derived
    ///   against LOCAL's content and `hmac_key` (the incoming marker's key
    ///   is not LOCAL's), `source_hash` set to the local capsule's own
    ///   identity. A target that is not a LOCAL live capsule is skipped (the
    ///   plan does not produce these; the guard is defensive).
    ///
    /// `hmac_key` is LOCAL's tombstone key, resolved at the boundary exactly
    /// like [`Store::forget_capsule`]. This method writes NO audit row — it
    /// returns the touched LOCAL ids ([`MergeApplied`]) so the caller audits
    /// each one (module audit policy; replay coverage needs every added
    /// capsule as an audit subject and every propagated forget as a
    /// recognized forget event), matching every other mutating store method.
    pub fn merge_from(
        &mut self,
        incoming_path: &Path,
        hmac_key: &[u8],
    ) -> Result<MergeApplied, StoreError> {
        // INCOMING core rows, read-only — fails closed on a missing, corrupt,
        // or stale-schema path BEFORE LOCAL is touched.
        let incoming = Store::open_readonly(incoming_path)?;
        let incoming_capsules = incoming.list(ListFilter::default())?;
        let incoming_relations = incoming.all_relations()?;
        let incoming_tombstones = incoming.all_tombstones()?;
        // LOCAL core rows.
        let local_capsules = self.list(ListFilter::default())?;
        let local_relations = self.all_relations()?;
        let local_tombstones = self.all_tombstones()?;

        let plan = crate::merge::plan_merge(
            &local_capsules,
            &local_relations,
            &local_tombstones,
            &incoming_capsules,
            &incoming_relations,
            &incoming_tombstones,
        );

        let capsules_added = plan.new_capsules.len();
        let relations_added = plan.new_relations.len();
        let id_remap_size = plan.id_remap.len();
        // Every incoming live capsule is in the remap; those that did not
        // mint a new row collapsed onto existing LOCAL content.
        let capsules_collapsed = id_remap_size.saturating_sub(capsules_added);
        // The LOCAL ids the caller must audit (in plan order): the appended
        // capsules, and the forgotten ids collected as each forget applies.
        let added_ids: Vec<String> = plan.new_capsules.iter().map(|p| p.id.clone()).collect();
        let mut forgotten_ids: Vec<String> = Vec::new();

        let tx = self.conn.transaction().map_err(backend)?;

        // 1. New capsules — appended under their planned ids.
        for planned in &plan.new_capsules {
            let canonical_json = planned
                .capsule
                .to_canonical_json()
                .map_err(|e| StoreError::Serialize(e.to_string()))?;
            let created_at = rfc3339_text(planned.created_at)?;
            let valid_from = rfc3339_text(planned.capsule.freshness().valid_from)?;
            let authority_class = authority_class_text(planned.capsule.authority_class())?;
            let seq: i64 = tx
                .query_row(
                    "SELECT COALESCE(MAX(seq), 0) + 1 FROM capsules",
                    [],
                    |row| row.get(0),
                )
                .map_err(backend)?;
            let minted = format!("cap-{seq}");
            // The store is the only id mint. The pure plan minted the SAME
            // id after LOCAL's ceiling; a divergence means the ceiling the
            // plan saw and the live MAX(seq) disagree — fail closed.
            if minted != planned.id {
                return Err(StoreError::Corrupt {
                    id: planned.id.clone(),
                    reason: format!(
                        "merge planned id {} but the store minted {minted} \
                         (id-ceiling divergence)",
                        planned.id
                    ),
                });
            }
            tx.execute(
                "INSERT INTO capsules \
                 (seq, id, canonical_json, created_at, source_hash, project_id, \
                  authority_class, valid_from, session_id) \
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)",
                params![
                    seq,
                    minted,
                    canonical_json,
                    created_at,
                    planned.capsule.provenance().source_hash,
                    planned.capsule.scope().project_id,
                    authority_class,
                    valid_from,
                    planned.session_id,
                ],
            )
            .map_err(|e| map_unique_source_hash(e, &planned.capsule.provenance().source_hash))?;
            tx.execute(
                "INSERT INTO capsules_fts (rowid, content) VALUES (?1, ?2)",
                params![seq, planned.capsule.content()],
            )
            .map_err(backend)?;
        }

        // 1b. Review durability without foreign CLOSE authority (b2 / d13):
        //     a newly-MINTED capsule with any SOURCE review history becomes
        //     exactly one LOCAL `proposed` row. Foreign `ratified` /
        //     `rejected` verdicts never cross the standalone connector as
        //     local authority. A capsule that COLLAPSED onto existing LOCAL
        //     content carries NOTHING, so incoming review state cannot demote
        //     local truth. The first reviewed incoming contributor per minted
        //     id wins deterministically (`id_remap` is incoming-id ordered).
        //     A malformed source verdict fails closed before a write.
        let minted_ids: BTreeSet<&str> = plan.new_capsules.iter().map(|p| p.id.as_str()).collect();
        let mut review_written: BTreeSet<&str> = BTreeSet::new();
        for (incoming_id, local_id) in &plan.id_remap {
            if !minted_ids.contains(local_id.as_str()) {
                continue;
            }
            let events = incoming.review_events_of(incoming_id)?;
            let Some(latest) = events.last() else {
                continue;
            };
            if !review_written.insert(local_id.as_str()) {
                continue;
            }
            let review_seq: i64 = tx
                .query_row(
                    "SELECT COALESCE(MAX(seq), 0) + 1 FROM review_events",
                    [],
                    |row| row.get(0),
                )
                .map_err(backend)?;
            let at = rfc3339_text(latest.at)?;
            let reason = format!(
                "foreign review state normalized to proposal (latest={})",
                latest.verdict.as_str()
            );
            tx.execute(
                "INSERT INTO review_events (seq, capsule_id, verdict, reason, actor, at) \
                 VALUES (?1, ?2, 'proposed', ?3, 'memory_merge', ?4)",
                params![review_seq, local_id, reason, at],
            )
            .map_err(backend)?;
        }

        // 2. New relations — the plan already remapped, deduped, and dropped
        //    danglers; INSERT OR IGNORE is an idempotent backstop.
        for edge in &plan.new_relations {
            let at = rfc3339_text(edge.at)?;
            tx.execute(
                "INSERT OR IGNORE INTO relations (kind, from_id, to_id, at, origin) \
                 VALUES (?1, ?2, ?3, ?4, ?5)",
                params![
                    edge.kind.as_str(),
                    edge.from_id,
                    edge.to_id,
                    at,
                    edge.origin.as_str()
                ],
            )
            .map_err(backend)?;
        }

        // 3. Forget-wins tombstones — forget the resolved LOCAL live capsule
        //    and record its marker re-keyed under LOCAL's key.
        let mut tombstones_applied = 0usize;
        for marker in &plan.new_tombstones {
            let row: Option<(i64, Option<String>)> = tx
                .query_row(
                    "SELECT seq, canonical_json FROM capsules WHERE id = ?1",
                    [marker.capsule_id.as_str()],
                    |row| Ok((row.get(0)?, row.get(1)?)),
                )
                .optional()
                .map_err(backend)?;
            // Only a LOCAL LIVE capsule is forgotten; an absent or already-
            // tombstoned target is skipped (the plan does not produce these).
            let (seq, canonical_json) = match row {
                Some((seq, Some(json))) => (seq, json),
                _ => continue,
            };
            let capsule: Capsule =
                serde_json::from_str(&canonical_json).map_err(|e| StoreError::Corrupt {
                    id: marker.capsule_id.clone(),
                    reason: format!("canonical_json: {e}"),
                })?;
            let content_hmac = content_hmac_hex(hmac_key, &marker.capsule_id, capsule.content());
            let at = rfc3339_text(marker.at)?;
            tx.execute(
                "UPDATE capsules SET canonical_json = NULL WHERE seq = ?1",
                [seq],
            )
            .map_err(backend)?;
            tx.execute("DELETE FROM capsules_fts WHERE rowid = ?1", [seq])
                .map_err(backend)?;
            tx.execute(
                "INSERT INTO capsules_fts (rowid, content) VALUES (?1, '')",
                [seq],
            )
            .map_err(backend)?;
            tx.execute(
                "DELETE FROM embeddings WHERE capsule_id = ?1",
                [marker.capsule_id.as_str()],
            )
            .map_err(backend)?;
            tx.execute(
                "INSERT INTO tombstones (capsule_id, mode, content_hmac, at, reason, \
                                         provenance_source, provenance_anchor, source_hash) \
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)",
                params![
                    marker.capsule_id,
                    marker.mode.as_str(),
                    content_hmac,
                    at,
                    marker.reason,
                    marker.provenance_source,
                    marker.provenance_anchor,
                    capsule.provenance().source_hash,
                ],
            )
            .map_err(backend)?;
            forgotten_ids.push(marker.capsule_id.clone());
            tombstones_applied += 1;
        }

        tx.commit().map_err(backend)?;
        Ok(MergeApplied {
            summary: MergeSummary {
                capsules_added,
                capsules_collapsed,
                relations_added,
                tombstones_applied,
                id_remap_size,
            },
            added_ids,
            forgotten_ids,
        })
    }

    /// Open a session bracket. `session_id` is caller-chosen, non-empty
    /// ([`StoreError::EmptyField`]) and unique
    /// ([`StoreError::DuplicateSession`] — a bracket opens once);
    /// `started_at` is the INJECTED `now`.
    pub fn open_session(
        &mut self,
        session_id: &str,
        now: OffsetDateTime,
    ) -> Result<(), StoreError> {
        if session_id.trim().is_empty() {
            return Err(StoreError::EmptyField("session_id"));
        }
        let started_at = rfc3339_text(now)?;
        let tx = self.conn.transaction().map_err(backend)?;
        let exists: bool = tx
            .query_row(
                "SELECT EXISTS(SELECT 1 FROM sessions WHERE session_id = ?1)",
                [session_id],
                |row| row.get(0),
            )
            .map_err(backend)?;
        if exists {
            return Err(StoreError::DuplicateSession(session_id.to_string()));
        }
        tx.execute(
            "INSERT INTO sessions (session_id, started_at, finished_at, summary) \
             VALUES (?1, ?2, NULL, NULL)",
            params![session_id, started_at],
        )
        .map_err(backend)?;
        tx.commit().map_err(backend)?;
        Ok(())
    }

    /// Close a session bracket: stamps `finished_at` from the INJECTED
    /// `now` and records the optional `summary`. Unknown sessions are
    /// [`StoreError::UnknownSession`]; a bracket closes exactly once
    /// ([`StoreError::SessionFinished`] on a re-finish — the first close
    /// record is never silently overwritten).
    pub fn finish_session(
        &mut self,
        session_id: &str,
        summary: Option<&str>,
        now: OffsetDateTime,
    ) -> Result<(), StoreError> {
        let finished_at = rfc3339_text(now)?;
        let tx = self.conn.transaction().map_err(backend)?;
        let state: Option<Option<String>> = tx
            .query_row(
                "SELECT finished_at FROM sessions WHERE session_id = ?1",
                [session_id],
                |row| row.get(0),
            )
            .optional()
            .map_err(backend)?;
        match state {
            None => Err(StoreError::UnknownSession(session_id.to_string())),
            Some(Some(_)) => Err(StoreError::SessionFinished(session_id.to_string())),
            Some(None) => {
                tx.execute(
                    "UPDATE sessions SET finished_at = ?2, summary = ?3 \
                     WHERE session_id = ?1",
                    params![session_id, finished_at, summary],
                )
                .map_err(backend)?;
                tx.commit().map_err(backend)?;
                Ok(())
            }
        }
    }

    /// One session record by id; `Ok(None)` when it was never opened.
    pub fn get_session(&self, session_id: &str) -> Result<Option<SessionRecord>, StoreError> {
        self.conn
            .query_row(
                "SELECT session_id, started_at, finished_at, summary \
                 FROM sessions WHERE session_id = ?1",
                [session_id],
                row_to_session,
            )
            .optional()
            .map_err(backend)?
            .map(RawSession::decode)
            .transpose()
    }

    /// Every session record, deterministic data order
    /// `(started_at, session_id)` — stable under VACUUM/replay.
    pub fn list_sessions(&self) -> Result<Vec<SessionRecord>, StoreError> {
        let mut stmt = self
            .conn
            .prepare(
                "SELECT session_id, started_at, finished_at, summary \
                 FROM sessions ORDER BY started_at, session_id",
            )
            .map_err(backend)?;
        let rows = stmt.query_map([], row_to_session).map_err(backend)?;
        let mut out = Vec::new();
        for row in rows {
            out.push(row.map_err(backend)?.decode()?);
        }
        Ok(out)
    }

    /// Atomic store-local activity projection over every exact session label
    /// present in `sessions`, `capsules`, or `recall_receipts`. One SQLite
    /// statement holds the read snapshot: capsule and receipt sources are
    /// pre-aggregated independently before their LEFT JOIN, so `2` saves and
    /// `3` recalls stay `2/3` rather than multiplying through a raw-row join.
    ///
    /// Equality and label-only ordering are explicitly `BINARY`: case,
    /// leading/trailing space, Unicode normalization, NUL, and hostile bytes
    /// remain distinct accepted labels. Local brackets lead in
    /// `(started_at, session_id)` order; rows without a local bracket follow
    /// in exact label order. Persisted NULL, wrong-storage-class,
    /// invalid-UTF-8, or whitespace-only labels and malformed/incorrectly
    /// typed bracket timestamps are typed corruption, never filtered or
    /// rendered as plausible activity.
    pub fn session_activity(&self) -> Result<Vec<SessionActivityRow>, StoreError> {
        let mut stmt = self
            .conn
            .prepare(
                "WITH labels(session_id) AS ( \
                     SELECT session_id COLLATE BINARY FROM sessions \
                     UNION \
                     SELECT session_id COLLATE BINARY FROM capsules \
                     WHERE session_id IS NOT NULL \
                     UNION \
                     SELECT session_id COLLATE BINARY FROM recall_receipts \
                     WHERE session_id IS NOT NULL \
                 ), \
                 capsule_counts(session_id, saves) AS ( \
                     SELECT session_id COLLATE BINARY, COUNT(*) \
                     FROM capsules \
                     WHERE session_id IS NOT NULL \
                     GROUP BY session_id COLLATE BINARY \
                 ), \
                 receipt_counts(session_id, recalls) AS ( \
                     SELECT session_id COLLATE BINARY, COUNT(*) \
                     FROM recall_receipts \
                     WHERE session_id IS NOT NULL \
                     GROUP BY session_id COLLATE BINARY \
                 ) \
                 SELECT labels.session_id, \
                        COALESCE(capsule_counts.saves, 0), \
                        COALESCE(receipt_counts.recalls, 0), \
                        sessions.session_id, sessions.started_at, sessions.finished_at \
                 FROM labels \
                 LEFT JOIN capsule_counts \
                   ON capsule_counts.session_id COLLATE BINARY \
                    = labels.session_id COLLATE BINARY \
                 LEFT JOIN receipt_counts \
                   ON receipt_counts.session_id COLLATE BINARY \
                    = labels.session_id COLLATE BINARY \
                 LEFT JOIN sessions \
                   ON sessions.session_id COLLATE BINARY \
                    = labels.session_id COLLATE BINARY \
                 ORDER BY CASE WHEN sessions.session_id IS NULL THEN 1 ELSE 0 END, \
                          sessions.started_at, labels.session_id COLLATE BINARY",
            )
            .map_err(backend)?;
        let rows = stmt
            .query_map([], |row| {
                Ok(RawSessionActivity {
                    session_id: RawSqlValue::read(row, 0)?,
                    saves: row.get(1)?,
                    recalls: row.get(2)?,
                    bracket_session_id: RawSqlValue::read(row, 3)?,
                    started_at: RawSqlValue::read(row, 4)?,
                    finished_at: RawSqlValue::read(row, 5)?,
                })
            })
            .map_err(backend)?;
        let mut out = Vec::new();
        for row in rows {
            out.push(row.map_err(backend)?.decode()?);
        }
        Ok(out)
    }

    /// Count one recall for every id in `ids` (one increment per slice
    /// entry) and stamp `last_recalled_at` from the INJECTED `now` — the
    /// store still reads no clock. All ids commit in one transaction;
    /// an empty slice writes nothing.
    ///
    /// The `usage` sidecar is derived, best-effort data: ids are not
    /// validated against `capsules` (an orphan counter row is harmless —
    /// dropping the whole table loses nothing).
    pub fn record_recall(&mut self, ids: &[&str], now: OffsetDateTime) -> Result<(), StoreError> {
        if ids.is_empty() {
            return Ok(());
        }
        let at = rfc3339_text(now)?;
        let tx = self.conn.transaction().map_err(backend)?;
        {
            let mut stmt = tx
                .prepare(
                    "INSERT INTO usage (capsule_id, recall_count, last_recalled_at) \
                     VALUES (?1, 1, ?2) \
                     ON CONFLICT(capsule_id) DO UPDATE SET \
                         recall_count = recall_count + 1, \
                         last_recalled_at = excluded.last_recalled_at",
                )
                .map_err(backend)?;
            for id in ids {
                stmt.execute(params![id, at]).map_err(backend)?;
            }
        }
        tx.commit().map_err(backend)?;
        Ok(())
    }

    /// Usage counters for `id`; `Ok(None)` when it was never recalled
    /// (or the derived table was dropped — same meaning: no usage data).
    pub fn usage_of(&self, id: &str) -> Result<Option<UsageStat>, StoreError> {
        let row = self
            .conn
            .query_row(
                "SELECT recall_count, last_recalled_at FROM usage WHERE capsule_id = ?1",
                [id],
                |row| Ok((row.get::<_, i64>(0)?, row.get::<_, String>(1)?)),
            )
            .optional()
            .map_err(backend)?;
        match row {
            None => Ok(None),
            Some((recall_count, at_text)) => {
                let last_recalled_at =
                    OffsetDateTime::parse(&at_text, &Rfc3339).map_err(|e| StoreError::Corrupt {
                        id: id.to_string(),
                        reason: format!("usage.last_recalled_at: {e}"),
                    })?;
                Ok(Some(UsageStat {
                    recall_count,
                    last_recalled_at,
                }))
            }
        }
    }

    /// Set (or replace) `id`'s lifecycle tier. The capsule must be stored
    /// ([`StoreError::UnknownCapsule`]; tombstoned still counts — the tier
    /// is about the record, not the content). Upsert: re-tiering replaces
    /// the row and stamps the new INJECTED `at`. Setting [`Tier::Active`]
    /// materializes a row rather than deleting one — the ledger of "who
    /// set what when" is the row's `at`; the DEFAULT Active (no row) and
    /// the SET Active are indistinguishable through [`Store::get_tier`],
    /// by design.
    pub fn set_tier(
        &mut self,
        id: &str,
        tier: Tier,
        now: OffsetDateTime,
    ) -> Result<(), StoreError> {
        let at = rfc3339_text(now)?;
        let tx = self.conn.transaction().map_err(backend)?;
        let exists: bool = tx
            .query_row(
                "SELECT EXISTS(SELECT 1 FROM capsules WHERE id = ?1)",
                [id],
                |row| row.get(0),
            )
            .map_err(backend)?;
        if !exists {
            return Err(StoreError::UnknownCapsule(id.to_string()));
        }
        tx.execute(
            "INSERT INTO tiers (capsule_id, tier, at) VALUES (?1, ?2, ?3) \
             ON CONFLICT(capsule_id) DO UPDATE SET \
                 tier = excluded.tier, \
                 at = excluded.at",
            params![id, tier.as_str(), at],
        )
        .map_err(backend)?;
        tx.commit().map_err(backend)?;
        Ok(())
    }

    /// `id`'s effective lifecycle tier: the stored row's tier, or
    /// [`Tier::Active`] when no tier was ever set (the default is a rule,
    /// not a row). The capsule must be stored
    /// ([`StoreError::UnknownCapsule`] — a tier for a capsule that does
    /// not exist would be a fabrication); tombstoned rows still answer
    /// (record-level state, like classifications).
    pub fn get_tier(&self, id: &str) -> Result<Tier, StoreError> {
        let exists: bool = self
            .conn
            .query_row(
                "SELECT EXISTS(SELECT 1 FROM capsules WHERE id = ?1)",
                [id],
                |row| row.get(0),
            )
            .map_err(backend)?;
        if !exists {
            return Err(StoreError::UnknownCapsule(id.to_string()));
        }
        let stored: Option<String> = self
            .conn
            .query_row(
                "SELECT tier FROM tiers WHERE capsule_id = ?1",
                [id],
                |row| row.get(0),
            )
            .optional()
            .map_err(backend)?;
        match stored {
            None => Ok(Tier::Active),
            Some(text) => Tier::from_wire(&text).ok_or_else(|| StoreError::Corrupt {
                id: id.to_string(),
                reason: format!("tiers.tier: unknown value {text:?}"),
            }),
        }
    }

    /// Ids of every LIVE capsule whose EFFECTIVE tier is `tier`, in append
    /// (`seq`) order. "Effective" applies the default rule: capsules with
    /// no tier row count as [`Tier::Active`]. Tombstoned rows are excluded
    /// — a destroyed capsule is not lifecycle work in any tier (their
    /// record-level tier still answers via [`Store::get_tier`], mirroring
    /// the get/list split of tombstoned capsules themselves).
    pub fn list_by_tier(&self, tier: Tier) -> Result<Vec<String>, StoreError> {
        let mut stmt = self
            .conn
            .prepare(
                "SELECT c.id FROM capsules c \
                 LEFT JOIN tiers t ON t.capsule_id = c.id \
                 WHERE c.canonical_json IS NOT NULL \
                   AND COALESCE(t.tier, 'active') = ?1 \
                 ORDER BY c.seq",
            )
            .map_err(backend)?;
        let rows = stmt
            .query_map([tier.as_str()], |row| row.get::<_, String>(0))
            .map_err(backend)?;
        let mut out = Vec::new();
        for row in rows {
            out.push(row.map_err(backend)?);
        }
        Ok(out)
    }

    /// Teach the recall index one caller-fed synonym pair: `alias` is a
    /// term a caller may search by that should also suggest `term`. Both
    /// sides are normalized on write ([`fold_term`]: trim + lowercase +
    /// Latin diacritic fold — `"Configuração"` stores as `configuracao`),
    /// so lookups are case- and accent-insensitive by construction.
    /// Returns `true` when the pair was freshly recorded, `false` for the
    /// idempotent re-add (which keeps the FIRST `at` — no-op honesty,
    /// distinguishable on the wire). Empty-after-normalization sides are
    /// [`StoreError::EmptyField`]; a pair that folds to the same word is
    /// [`StoreError::SelfAlias`].
    ///
    /// The synonyms sidecar is DERIVED, caller-fed data: the caller is its
    /// source of truth and can re-teach it at will; dropping the table
    /// loses no canonical byte ([ARCHITECTURE §2 rung]). The store never
    /// auto-expands queries with it — the CALLER asks
    /// ([`Store::aliases_for`]) and decides (the LLM-first law: the caller
    /// is intelligent).
    pub fn add_alias(
        &mut self,
        term: &str,
        alias: &str,
        now: OffsetDateTime,
    ) -> Result<bool, StoreError> {
        let term = fold_term(term);
        let alias = fold_term(alias);
        if term.is_empty() {
            return Err(StoreError::EmptyField("term"));
        }
        if alias.is_empty() {
            return Err(StoreError::EmptyField("alias"));
        }
        if term == alias {
            return Err(StoreError::SelfAlias { term });
        }
        let at = rfc3339_text(now)?;
        let inserted = self
            .conn
            .execute(
                "INSERT OR IGNORE INTO synonyms (term, alias, at) VALUES (?1, ?2, ?3)",
                params![term, alias, at],
            )
            .map_err(backend)?;
        Ok(inserted > 0)
    }

    /// Every alias recorded for `term` (lookup side folded exactly like
    /// the write side — `"CONFIGURAÇÃO"` finds what `"configuracao"`
    /// taught), sorted. Direction is as-taught: this answers
    /// `term → aliases`; the caller records both directions when it wants
    /// symmetry. Unknown terms have no aliases: empty, never an error.
    pub fn aliases_for(&self, term: &str) -> Result<Vec<String>, StoreError> {
        let folded = fold_term(term);
        let mut stmt = self
            .conn
            .prepare("SELECT alias FROM synonyms WHERE term = ?1 ORDER BY alias")
            .map_err(backend)?;
        let rows = stmt
            .query_map([folded], |row| row.get::<_, String>(0))
            .map_err(backend)?;
        let mut out = Vec::new();
        for row in rows {
            out.push(row.map_err(backend)?);
        }
        Ok(out)
    }

    /// The full synonym table as `(term, alias, at)` rows, deterministic
    /// `(term, alias)` order — the caller-side rebuild/export view. `at`
    /// is the FIRST-record instant (idempotent re-adds keep it), exposed
    /// so the documented "first at kept" no-op is verifiable on a read
    /// surface (w2-fix).
    pub fn list_aliases(&self) -> Result<Vec<(String, String, OffsetDateTime)>, StoreError> {
        let mut stmt = self
            .conn
            .prepare("SELECT term, alias, at FROM synonyms ORDER BY term, alias")
            .map_err(backend)?;
        let rows = stmt
            .query_map([], |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, String>(2)?,
                ))
            })
            .map_err(backend)?;
        let mut out = Vec::new();
        for row in rows {
            let (term, alias, at_text) = row.map_err(backend)?;
            let at = parse_at(&term, "synonyms.at", &at_text)?;
            out.push((term, alias, at));
        }
        Ok(out)
    }

    /// Attach (or REPLACE) the caller-fed embedding for `id` — the w3 u6a
    /// vector-sidecar write. ONE embedding per capsule: a second `put` on
    /// the same id REPLACES the row (documented replace-on-write; the store
    /// keeps no vector history, mirroring the single-row discipline of
    /// `tiers`). The capsule must be stored ([`StoreError::UnknownCapsule`]
    /// — a vector for a capsule that does not exist is a dangling
    /// fabrication; a tombstoned id still counts as stored, exactly like
    /// [`Store::set_tier`], but a tombstoned capsule never grounds recall so
    /// its vector is inert). The embedding is validated
    /// ([`validate_embedding`]) and persisted as its deterministic
    /// little-endian `f32` blob with the recorded `dimension` and the
    /// caller's `model_tag` provenance (trimmed; empty is
    /// [`StoreError::EmptyField`]). `now` is the injected instant — the
    /// store reads no clock. Returns `true` on a fresh insert, `false` when
    /// it replaced an existing embedding (the replace is observable, the
    /// replace-on-write honesty signal).
    pub fn put_embedding(
        &mut self,
        id: &str,
        vector: &[f32],
        model_tag: &str,
        now: OffsetDateTime,
    ) -> Result<bool, StoreError> {
        let model_tag = model_tag.trim();
        if model_tag.is_empty() {
            return Err(StoreError::EmptyField("model_tag"));
        }
        validate_embedding(vector)?;
        let tx = self.conn.transaction().map_err(backend)?;
        let exists: bool = tx
            .query_row(
                "SELECT EXISTS(SELECT 1 FROM capsules WHERE id = ?1)",
                [id],
                |row| row.get(0),
            )
            .map_err(backend)?;
        if !exists {
            return Err(StoreError::UnknownCapsule(id.to_string()));
        }
        // q119: ONE embedder per store is a MECHANICAL fence, not a
        // doc-comment — the dimensional guard cannot tell two 768-dim
        // model spaces apart, so the tag itself is compared. The first
        // attach elects the store's resident embedder; a different tag is
        // refused naming the resident (an embedder swap is an explicit
        // re-attach migration, never silent cross-space fusion).
        let resident: Option<String> = tx
            .query_row(
                "SELECT model_tag FROM embeddings WHERE model_tag != ?1 LIMIT 1",
                [model_tag],
                |row| row.get(0),
            )
            .optional()
            .map_err(backend)?;
        if let Some(resident) = resident {
            return Err(StoreError::InvalidEmbedding(format!(
                "model_tag {model_tag:?} differs from the store's resident embedder \
                 {resident:?} — one embedder per store; swapping embedders is an \
                 explicit migration (re-attach every vector under the new tag)"
            )));
        }
        let had_before: bool = tx
            .query_row(
                "SELECT EXISTS(SELECT 1 FROM embeddings WHERE capsule_id = ?1)",
                [id],
                |row| row.get(0),
            )
            .map_err(backend)?;
        let dimension = i64::try_from(vector.len()).unwrap_or(i64::MAX);
        let blob = encode_embedding(vector);
        let at = rfc3339_text(now)?;
        tx.execute(
            "INSERT INTO embeddings (capsule_id, dimension, model_tag, vector, at) \
             VALUES (?1, ?2, ?3, ?4, ?5) \
             ON CONFLICT(capsule_id) DO UPDATE SET \
                 dimension = excluded.dimension, \
                 model_tag = excluded.model_tag, \
                 vector = excluded.vector, \
                 at = excluded.at",
            params![id, dimension, model_tag, blob, at],
        )
        .map_err(backend)?;
        tx.commit().map_err(backend)?;
        Ok(!had_before)
    }

    /// The embedding attached to `id`, or `None` when the capsule carries
    /// none. Decodes the little-endian blob back to the EXACT `f32` vector
    /// the caller `put` (bit-exact round-trip via [`decode_embedding`]); a
    /// wrong-length blob is a typed [`StoreError::Corrupt`].
    pub fn get_embedding(&self, id: &str) -> Result<Option<StoredEmbedding>, StoreError> {
        let row: Option<(i64, String, Vec<u8>)> = self
            .conn
            .query_row(
                "SELECT dimension, model_tag, vector FROM embeddings WHERE capsule_id = ?1",
                [id],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )
            .optional()
            .map_err(backend)?;
        let Some((dimension, model_tag, blob)) = row else {
            return Ok(None);
        };
        let dimension = usize::try_from(dimension).map_err(|_| StoreError::Corrupt {
            id: id.to_string(),
            reason: format!("embeddings.dimension {dimension} is negative"),
        })?;
        let vector = decode_embedding(id, &blob, dimension)?;
        Ok(Some(StoredEmbedding {
            capsule_id: id.to_string(),
            dimension,
            model_tag,
            vector,
        }))
    }

    /// The embedding index — `(capsule_id, dimension, model_tag)` for every
    /// stored vector, in append (`seq`) order of the underlying capsule (a
    /// tombstoned capsule's embedding still lists: the row is inert for
    /// recall but the caller may want to see and forget it). Vectors
    /// themselves stay one [`Store::get_embedding`] away — the list is the
    /// cheap index, never the bytes.
    pub fn list_embeddings(&self) -> Result<Vec<EmbeddingRow>, StoreError> {
        let mut stmt = self
            .conn
            .prepare(
                "SELECT e.capsule_id, e.dimension, e.model_tag \
                 FROM embeddings e \
                 JOIN capsules c ON c.id = e.capsule_id \
                 ORDER BY c.seq",
            )
            .map_err(backend)?;
        let rows = stmt
            .query_map([], |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, i64>(1)?,
                    row.get::<_, String>(2)?,
                ))
            })
            .map_err(backend)?;
        let mut out = Vec::new();
        for row in rows {
            let (capsule_id, dimension, model_tag) = row.map_err(backend)?;
            let dimension = usize::try_from(dimension).map_err(|_| StoreError::Corrupt {
                id: capsule_id.clone(),
                reason: format!("embeddings.dimension {dimension} is negative"),
            })?;
            out.push(EmbeddingRow {
                capsule_id,
                dimension,
                model_tag,
            });
        }
        Ok(out)
    }

    /// The vector-lane candidate source (w3 u6a recall): every LIVE capsule
    /// carrying an embedding, paired with its decoded vector, under the SAME
    /// scope fences [`Store::search_fts_scoped`] applies (`project_id` exact +
    /// `project_prefix` subtree + character-exact store-local `session_id`,
    /// AND-composed; a `None` disables its clause, `substr` keeps prefix
    /// bytes metacharacter-free). Tombstoned
    /// rows are excluded (`canonical_json IS NOT NULL`) — a destroyed
    /// capsule can never ground, by any lane. Append (`seq`) order, so the
    /// engine's dimension check and cosine tiebreak stay deterministic. The
    /// eligibility fences (tier/superseded/currency) are deliberately NOT
    /// applied here — they are the recall engine's job, applied IDENTICALLY
    /// to the FTS and vector lanes so the fence-dominance law is
    /// lane-agnostic.
    pub fn embeddings_for_recall(
        &self,
        project_id: Option<&str>,
        project_prefix: Option<&str>,
        session_id: Option<&str>,
    ) -> Result<Vec<(StoredCapsule, StoredEmbedding)>, StoreError> {
        self.embeddings_for_recall_effort(project_id, project_prefix, session_id, None)
    }

    /// [`Store::embeddings_for_recall`] with the S3 effort-lifecycle
    /// membership fence AND-composed onto the project/session fences — the
    /// vector lane's twin of [`Store::search_fts_effort`]. `effort_ids`
    /// (the effort's members ∪ {epic}) is a single JSON-array bind matched
    /// with `json_each` (never a per-id variable, never a post-filter), so a
    /// 1000-member effort stays a single parameter and the unique `id` index
    /// probes membership. `None` is byte-identical to
    /// [`Store::embeddings_for_recall`] (dormancy).
    pub fn embeddings_for_recall_effort(
        &self,
        project_id: Option<&str>,
        project_prefix: Option<&str>,
        session_id: Option<&str>,
        effort_ids: Option<&[String]>,
    ) -> Result<Vec<(StoredCapsule, StoredEmbedding)>, StoreError> {
        let effort_json = effort_ids_json(effort_ids)?;
        let mut stmt = self
            .conn
            .prepare(
                "SELECT c.id, c.seq, c.canonical_json, c.created_at, c.session_id, \
                        e.dimension, e.model_tag, e.vector \
                 FROM embeddings e \
                 JOIN capsules c ON c.id = e.capsule_id \
                 WHERE c.canonical_json IS NOT NULL \
                   AND (?1 IS NULL OR c.project_id = ?1) \
                   AND (?2 IS NULL OR c.project_id = ?2 \
                        OR substr(c.project_id, 1, length(?2) + 1) = ?2 || '/') \
                   AND (?3 IS NULL OR c.session_id = ?3) \
                   AND (?4 IS NULL OR c.id IN (SELECT value FROM json_each(?4))) \
                 ORDER BY c.seq",
            )
            .map_err(backend)?;
        let rows = stmt
            .query_map(
                params![project_id, project_prefix, session_id, effort_json],
                |row| {
                    let raw = row_to_raw(row)?;
                    let dimension: i64 = row.get(5)?;
                    let model_tag: String = row.get(6)?;
                    let blob: Vec<u8> = row.get(7)?;
                    Ok((raw, dimension, model_tag, blob))
                },
            )
            .map_err(backend)?;
        let mut out = Vec::new();
        for row in rows {
            let (raw, dimension, model_tag, blob) = row.map_err(backend)?;
            let stored = raw.decode()?;
            let id = stored.id.as_str().to_string();
            let dimension = usize::try_from(dimension).map_err(|_| StoreError::Corrupt {
                id: id.clone(),
                reason: format!("embeddings.dimension {dimension} is negative"),
            })?;
            let vector = decode_embedding(&id, &blob, dimension)?;
            let embedding = StoredEmbedding {
                capsule_id: id,
                dimension,
                model_tag,
                vector,
            };
            out.push((stored, embedding));
        }
        Ok(out)
    }
}

fn validate_feedback_score(score: f64) -> Result<(), StoreError> {
    if !score.is_finite() || !(0.0..=1.0).contains(&score) {
        return Err(StoreError::InvalidOutcomeScoring(
            "score must be finite and within 0.0..=1.0",
        ));
    }
    Ok(())
}

fn validate_outcome_scoring(receipt_id: &str, score: f64) -> Result<(), StoreError> {
    if receipt_id.trim().is_empty() {
        return Err(StoreError::InvalidOutcomeScoring(
            "receipt_id must be non-empty",
        ));
    }
    validate_feedback_score(score)
}

fn decode_feedback_weight(capsule_id: &str, weight: f64) -> Result<f64, StoreError> {
    if !weight.is_finite() || !(0.0..=1.0).contains(&weight) {
        return Err(StoreError::Corrupt {
            id: capsule_id.to_string(),
            reason: format!("feedback_weights.weight {weight:?} is not finite within 0.0..=1.0"),
        });
    }
    Ok(weight)
}

fn feedback_weight_on(conn: &Connection, capsule_id: &str) -> Result<Option<f64>, StoreError> {
    conn.query_row(
        "SELECT weight FROM feedback_weights WHERE capsule_id = ?1",
        [capsule_id],
        |row| row.get::<_, f64>(0),
    )
    .optional()
    .map_err(backend)?
    .map(|weight| decode_feedback_weight(capsule_id, weight))
    .transpose()
}

fn apply_feedback_on(
    tx: &rusqlite::Transaction<'_>,
    ids: &[&str],
    score: f64,
    at: &str,
) -> Result<Vec<(String, f64)>, StoreError> {
    validate_feedback_score(score)?;
    let mut updated = Vec::with_capacity(ids.len());
    for capsule_id in ids {
        let weight = feedback_weight_on(tx, capsule_id)?.unwrap_or(FEEDBACK_NEUTRAL_WEIGHT);
        let next = (weight + FEEDBACK_EMA_ALPHA * (score - weight)).clamp(0.0, 1.0);
        let next = decode_feedback_weight(capsule_id, next)?;
        tx.execute(
            "INSERT INTO feedback_weights (capsule_id, weight, at) \
             VALUES (?1, ?2, ?3) \
             ON CONFLICT(capsule_id) DO UPDATE SET \
                 weight = excluded.weight, \
                 at = excluded.at",
            params![capsule_id, next, at],
        )
        .map_err(backend)?;
        updated.push(((*capsule_id).to_string(), next));
    }
    Ok(updated)
}

fn receipt_returned_ids_on(
    conn: &Connection,
    receipt_id: &str,
) -> Result<Option<Vec<String>>, StoreError> {
    let returned_ids: Option<String> = conn
        .query_row(
            "SELECT returned_ids FROM recall_receipts WHERE id = ?1",
            [receipt_id],
            |row| row.get(0),
        )
        .optional()
        .map_err(backend)?;
    returned_ids
        .map(|json| {
            serde_json::from_str(&json).map_err(|error| StoreError::Corrupt {
                id: receipt_id.to_string(),
                reason: format!("recall_receipts.returned_ids: {error}"),
            })
        })
        .transpose()
}

fn validate_receipt_capsules_on(
    conn: &Connection,
    receipt_id: &str,
    returned_ids: &[String],
) -> Result<(), StoreError> {
    let mut seen = BTreeSet::new();
    for capsule_id in returned_ids {
        let exists: bool = conn
            .query_row(
                "SELECT EXISTS(SELECT 1 FROM capsules WHERE id = ?1)",
                [capsule_id],
                |row| row.get(0),
            )
            .map_err(backend)?;
        if capsule_id.trim().is_empty() || !exists {
            return Err(StoreError::Corrupt {
                id: receipt_id.to_string(),
                reason: format!(
                    "recall_receipts.returned_ids names unknown capsule {capsule_id:?}"
                ),
            });
        }
        if !seen.insert(capsule_id.as_str()) {
            return Err(StoreError::Corrupt {
                id: receipt_id.to_string(),
                reason: format!("recall_receipts.returned_ids repeats capsule {capsule_id:?}"),
            });
        }
    }
    Ok(())
}

/// The explicit schema upgrade: brings any migratable file (version 0
/// fresh through the previous stamp) to the CURRENT shape, in ONE
/// transaction, and stamps `PRAGMA user_version = `[`SCHEMA_VERSION`].
/// Idempotent and crash-safe: every step is either conditional on the
/// observed old shape or `IF NOT EXISTS`, and the stamp commits atomically
/// with the changes — a crash rolls the whole upgrade back to the
/// untouched old file.
///
/// v1 → v2 rebuilds, preserving every row and every `seq`:
/// - `capsules`: `canonical_json` loses NOT NULL (the tombstone state) and
///   gains the nullable `session_id` sidecar column (existing rows: NULL —
///   the pre-session truth). Twelve-step rebuild (create shadow, copy,
///   drop, rename, re-index) because SQLite cannot ALTER a NOT NULL away.
/// - `relations`: the v1 supersede pair table `(superseded_id,
///   superseded_by, at)` becomes the generalized edge table; each old row
///   maps to `('supersedes', from = superseded_by, to = superseded_id,
///   at)` — donor orientation, `from` is the newer capsule. A pre-h4 v1
///   file with no `relations` table simply gets the new empty table.
/// - the four w1 sidecar tables + indexes are created; `capsules_fts` /
///   `usage` (derived) are ensured exactly as before.
///
/// v2 → v3 (w2-store2):
/// - `audit_events` gains `chained_hash`: shadow rebuild through the
///   shared [`audit_events_create_sql`] block (so migrated and fresh
///   shapes can never drift), then a DETERMINISTIC backfill — the chain is
///   recomputed from the stored row bytes in `seq` order with exactly the
///   functions live appends use ([`audit_canonical_line`] /
///   [`chained_hash_of`]), so two identical v2 ledgers migrate to
///   identical chains and [`Store::verify_chain`] is green immediately.
///   Trust-on-first-migration (the same boundary `journal.rs`'s module
///   docs carry): the backfill vouches for whatever rows the pre-chain
///   ledger held at migration time — an edit made BEFORE the migration is
///   baked into the new chain and stays invisible to
///   [`Store::verify_chain`] forever after; the chain attests integrity
///   from the backfill instant forward, never before it.
/// - `tiers` / `synonyms` are additive empty tables (`IF NOT EXISTS`,
///   inside [`SIDECAR_SCHEMA`]).
///
/// v4 → v6 (u6h/u6i substrates, renumbered at K integration — vector kept
/// slot 5):
/// - `relations.kind` CHECK widens to add `falsifies`: shared-DDL rebuild
///   ([`relations_create_sql`]) probed on the stored CHECK text
///   ([`relations_has_old_check`]) — every legacy edge satisfies the wider
///   set, so the copy is total; the `from`/`to` indexes re-create in
///   [`SIDECAR_SCHEMA`].
/// - `outcomes` / `preferences` are additive empty append-only tables
///   (`IF NOT EXISTS`, inside [`SIDECAR_SCHEMA`]).
///
/// v6 → v7 (u-r11 kind-vocabulary):
/// - `classifications.kind` CHECK widens to the ten-kind set (the three
///   governance kinds): the SAME shared-DDL rebuild
///   ([`classifications_create_sql`]) probed on the stored CHECK text
///   ([`classifications_has_old_check`], newest-token `'constraint'`) —
///   one rebuild serves BOTH legacy shapes (v3 3-kind and v4–v6 7-kind),
///   every legacy label satisfies the wider set, so the copy is total.
///
/// v12 → v13 (u04 scored outcomes):
/// - `outcomes` gains nullable `receipt_id` and `score` columns, guarded
///   independently so interrupted or hand-shaped files still converge.
/// - `feedback_weights` is an additive empty advisory sidecar.
///
/// v13 → v14 (u05 lane router):
/// - `lane_overrides` is an additive empty append-only advisory telemetry
///   sidecar. Its CHECK admits only the four unequal forced/auto pairs.
///
/// v14 → v15 (u06 event time):
/// - `event_time` is an additive empty caller-declared fact-time sidecar.
///   Capsule bytes and prior sidecars are untouched.
///
/// v15 → v16 (S1 pin):
/// - `pin_events` is an additive empty append-only pin/unpin ledger + its
///   index. Capsule bytes and prior sidecars are untouched.
///
/// v15/v16 → v17 (S2 git witness lane):
/// - `corroborations` + `source_cursors` are additive empty append-only
///   witness sidecars ([`CORROBORATIONS_DDL`] / [`SOURCE_CURSORS_DDL`],
///   `IF NOT EXISTS`). Version-integer-independent: a v15 file (no pin) and
///   a v16 file (S1's parallel `pin_events` present) both converge here by
///   `IF NOT EXISTS`, and this tree leaves any `pin_events` table untouched.
///   Capsule bytes and prior sidecars are untouched.
fn migrate_to_current(conn: &mut Connection) -> Result<(), StoreError> {
    let tx = conn.transaction().map_err(backend)?;
    // v1 capsules shape = no session_id column yet (a fresh file has no
    // table at all; a v2 file already has the column).
    let capsules_exists: bool = tx
        .query_row(
            "SELECT EXISTS(SELECT 1 FROM sqlite_master \
             WHERE type = 'table' AND name = 'capsules')",
            [],
            |row| row.get(0),
        )
        .map_err(backend)?;
    if capsules_exists && !table_has_column(&tx, "capsules", "session_id")? {
        tx.execute_batch(&capsules_create_sql("CREATE TABLE capsules_v2"))
            .map_err(backend)?;
        tx.execute_batch(
            "INSERT INTO capsules_v2 \
                 (seq, id, canonical_json, created_at, source_hash, project_id, \
                  authority_class, valid_from, session_id) \
                 SELECT seq, id, canonical_json, created_at, source_hash, project_id, \
                        authority_class, valid_from, NULL \
                 FROM capsules;
             DROP TABLE capsules;
             ALTER TABLE capsules_v2 RENAME TO capsules;",
        )
        .map_err(backend)?;
    }
    // v1 relations shape = the supersede pair table.
    if table_has_column(&tx, "relations", "superseded_id")? {
        tx.execute_batch(&relations_create_sql("CREATE TABLE relations_v2"))
            .map_err(backend)?;
        tx.execute_batch(
            "INSERT OR IGNORE INTO relations_v2 (kind, from_id, to_id, at) \
                 SELECT 'supersedes', superseded_by, superseded_id, at FROM relations;
             DROP TABLE relations;
             ALTER TABLE relations_v2 RENAME TO relations;",
        )
        .map_err(backend)?;
    }
    // v2 audit_events shape = the ledger without the chain column. Guarded
    // on the table EXISTING (the `seq` probe): a fresh/v1 file has no
    // ledger and simply gets the v3 table below.
    if table_has_column(&tx, "audit_events", "seq")?
        && !table_has_column(&tx, "audit_events", "chained_hash")?
    {
        tx.execute_batch(&audit_events_create_sql("CREATE TABLE audit_events_v3"))
            .map_err(backend)?;
        tx.execute_batch(
            "INSERT INTO audit_events_v3 \
                 (seq, at, actor, action, subject, reason, chained_hash) \
                 SELECT seq, at, actor, action, subject, reason, '' FROM audit_events;
             DROP TABLE audit_events;
             ALTER TABLE audit_events_v3 RENAME TO audit_events;",
        )
        .map_err(backend)?;
        // Deterministic chain backfill: recompute from the stored bytes in
        // seq order with the SAME functions live appends use — identical
        // ledgers yield identical chains, and verify_chain is green on the
        // migrated file without a single row byte changing.
        let rows: Vec<(i64, String, String, String, String, Option<String>)> = {
            let mut stmt = tx
                .prepare(
                    "SELECT seq, at, actor, action, subject, reason \
                     FROM audit_events ORDER BY seq",
                )
                .map_err(backend)?;
            let mapped = stmt
                .query_map([], |row| {
                    Ok((
                        row.get(0)?,
                        row.get(1)?,
                        row.get(2)?,
                        row.get(3)?,
                        row.get(4)?,
                        row.get(5)?,
                    ))
                })
                .map_err(backend)?;
            let mut out = Vec::new();
            for row in mapped {
                out.push(row.map_err(backend)?);
            }
            out
        };
        let mut prev = String::new();
        for (seq, at, actor, action, subject, reason) in rows {
            let line =
                audit_canonical_line(seq, &at, &actor, &action, &subject, reason.as_deref())?;
            let chained = chained_hash_of(&prev, &line);
            tx.execute(
                "UPDATE audit_events SET chained_hash = ?2 WHERE seq = ?1",
                params![seq, chained],
            )
            .map_err(backend)?;
            prev = chained;
        }
    }
    // Pre-v7 classifications shape = a narrower kind CHECK (the v3
    // 3-kind or the v4–v6 7-kind set). SQLite cannot ALTER a CHECK, so
    // the current 10-kind set is ONE shared-DDL table rebuild —
    // shape-probed on the stored CREATE sql (the CHECK text), same
    // no-drift discipline as the rebuilds above. Rows always satisfy the
    // WIDER constraint (every narrower set is a subset), so the copy is
    // total whichever legacy shape arrived.
    if classifications_has_old_check(&tx)? {
        tx.execute_batch(&classifications_create_sql(
            "CREATE TABLE classifications_v7",
        ))
        .map_err(backend)?;
        tx.execute_batch(
            "INSERT INTO classifications_v7 (capsule_id, kind, scope, at) \
                 SELECT capsule_id, kind, scope, at FROM classifications;
             DROP TABLE classifications;
             ALTER TABLE classifications_v7 RENAME TO classifications;",
        )
        .map_err(backend)?;
    }
    // Pre-substrate relations shape = the four-kind CHECK (no
    // 'falsifies'). SQLite cannot ALTER a CHECK, so the current five-kind
    // set is a shared-DDL table rebuild through [`relations_create_sql`] —
    // shape-probed on the stored CREATE sql, the SAME no-drift discipline
    // as the classifications rebuild above. Every existing edge (all four
    // legacy kinds) satisfies the WIDER constraint, so the copy is total;
    // the `from`/`to` indexes are re-created by [`SIDECAR_SCHEMA`] below
    // (they drop with the old table). Order-independent: probes the DDL,
    // not the version.
    if relations_has_old_check(&tx)? {
        tx.execute_batch(&relations_create_sql("CREATE TABLE relations_v5"))
            .map_err(backend)?;
        tx.execute_batch(
            "INSERT INTO relations_v5 (kind, from_id, to_id, at) \
                 SELECT kind, from_id, to_id, at FROM relations;
             DROP TABLE relations;
             ALTER TABLE relations_v5 RENAME TO relations;",
        )
        .map_err(backend)?;
    }
    tx.execute_batch(&capsules_create_sql("CREATE TABLE IF NOT EXISTS capsules"))
        .map_err(backend)?;
    tx.execute_batch(&relations_create_sql(
        "CREATE TABLE IF NOT EXISTS relations",
    ))
    .map_err(backend)?;
    tx.execute_batch(&audit_events_create_sql(
        "CREATE TABLE IF NOT EXISTS audit_events",
    ))
    .map_err(backend)?;
    tx.execute_batch(&classifications_create_sql(
        "CREATE TABLE IF NOT EXISTS classifications",
    ))
    .map_err(backend)?;
    tx.execute_batch(SIDECAR_SCHEMA).map_err(backend)?;
    // v2 files created before the redacted-provenance columns: additive
    // nullable ALTER, still v2 (older builds name their columns explicitly
    // on every read/write, so the extra columns are invisible to them).
    if !table_has_column(&tx, "tombstones", "provenance_source")? {
        tx.execute_batch(
            "ALTER TABLE tombstones ADD COLUMN provenance_source TEXT;
             ALTER TABLE tombstones ADD COLUMN provenance_anchor TEXT;",
        )
        .map_err(backend)?;
    }
    // v11 (store-merge u2): the forgotten capsule's content identity, for
    // cross-store forget propagation. Additive nullable ALTER, guarded on
    // the column's absence (a fresh file has it from [`SIDECAR_SCHEMA`]); a
    // pre-v11 marker backfills NULL — it simply cannot propagate by content.
    if !table_has_column(&tx, "tombstones", "source_hash")? {
        tx.execute_batch("ALTER TABLE tombstones ADD COLUMN source_hash TEXT;")
            .map_err(backend)?;
    }
    // v13 (u04 scored outcomes): nullable columns preserve every unscored
    // v12 row byte-for-byte at the wire. Separate shape guards converge a
    // partially upgraded file without guessing from its version stamp.
    if !table_has_column(&tx, "outcomes", "receipt_id")? {
        tx.execute_batch("ALTER TABLE outcomes ADD COLUMN receipt_id TEXT;")
            .map_err(backend)?;
    }
    if !table_has_column(&tx, "outcomes", "score")? {
        tx.execute_batch("ALTER TABLE outcomes ADD COLUMN score REAL;")
            .map_err(backend)?;
    }
    tx.execute_batch(FTS_DDL).map_err(backend)?;
    tx.execute_batch(USAGE_DDL).map_err(backend)?;
    // w3 u6a vector sidecar: additive `IF NOT EXISTS`, self-contained and
    // order-independent (a pre-vector file gains an empty `embeddings`
    // table; a file that already carries it re-runs this as a no-op; the
    // final stamp below is always the current SCHEMA_VERSION).
    tx.execute_batch(EMBEDDINGS_DDL).map_err(backend)?;
    // v7 (u-r2): the capture-time anchored-file hash and the epistemic
    // sidecar — additive `IF NOT EXISTS`, same order-independence.
    tx.execute_batch(ANCHOR_HASHES_DDL).map_err(backend)?;
    tx.execute_batch(EPISTEMICS_DDL).map_err(backend)?;
    // v9 (u-r5 miss-ledger): the append-only recall-miss ledger — additive
    // `IF NOT EXISTS`, same order-independence.
    tx.execute_batch(RECALL_MISSES_DDL).map_err(backend)?;
    // v10 (u-r8-REDESIGN stale-import-supersession): the import-block
    // lineage sidecar — additive `IF NOT EXISTS`, same order-independence.
    tx.execute_batch(IMPORT_BLOCKS_DDL).map_err(backend)?;
    // v12 (u03 recall receipts): the grounded-only, append-only feedback
    // address ledger — additive `IF NOT EXISTS`, same order-independence.
    tx.execute_batch(RECALL_RECEIPTS_DDL).map_err(backend)?;
    // v13 (u04 scored outcomes): opt-in ranking EMA sidecar. Additive and
    // disposable; capsule authority and eligibility never depend on it.
    tx.execute_batch(FEEDBACK_WEIGHTS_DDL).map_err(backend)?;
    // v14 (u05 lane router): successful explicit lane disagreements only.
    // Additive and disposable; recall never depends on telemetry writes.
    tx.execute_batch(LANE_OVERRIDES_DDL).map_err(backend)?;
    // v15 (u06 event time): caller-declared fact-time ranges. Additive,
    // local-only, and absent by default; no canonical capsule byte moves.
    tx.execute_batch(EVENT_TIME_DDL).map_err(backend)?;
    // v17 (S2 git witness lane): the append-only corroboration ledger and
    // its per-source scan cursor — additive `IF NOT EXISTS`, same
    // order-independence, so a v15 or v16 file converges here identically
    // (S1's v16 pin_events, when present, is left untouched).
    tx.execute_batch(CORROBORATIONS_DDL).map_err(backend)?;
    tx.execute_batch(SOURCE_CURSORS_DDL).map_err(backend)?;
    // v18: a bounded scan pins its target and checkpoints its newest-first
    // offset until the complete range is consumed. Additive and disposable.
    tx.execute_batch(SOURCE_BACKFILLS_DDL).map_err(backend)?;
    // v18 (b2 staged review): the append-only review-verdict ledger —
    // additive `IF NOT EXISTS`, same order-independence.
    tx.execute_batch(REVIEW_EVENTS_DDL).map_err(backend)?;
    // v10 (u-r8 round 3): relation edges carry their writer — `manual`
    // (caller) vs `import` (stale-import supersession). Guarded additive
    // ALTER (a rebuild above already created the column; a fresh file has
    // it from [`relations_create_sql`]). Every pre-existing edge was
    // caller-written — the mechanism is born with this column — so the
    // DEFAULT backfill `'manual'` is the historical truth, not a guess.
    if !table_has_column(&tx, "relations", "origin")? {
        tx.execute_batch(
            "ALTER TABLE relations ADD COLUMN origin TEXT NOT NULL DEFAULT 'manual' \
             CHECK (origin IN ('manual', 'import'))",
        )
        .map_err(backend)?;
    }
    // v16 (S1 pin): the append-only pin/unpin ledger + its index — additive
    // `IF NOT EXISTS`, order-independent, no canonical capsule byte moves.
    tx.execute_batch(PIN_EVENTS_DDL).map_err(backend)?;
    // v18 (b2 staged review) + v19 (effort-lifecycle s1) + v20
    // (planning-plane s1): widen the `relations.kind` CHECK to admit
    // `proposes`, `part_of`, AND `grounded_in`. SQLite cannot ALTER a CHECK,
    // so this is a shared-DDL table rebuild through [`relations_create_sql`]
    // (now the EIGHT-kind set) — shape-probed on the stored CHECK text, the
    // SAME no-drift discipline as the `falsifies` rebuild. ONE rebuild
    // reconciles ALL THREE lineages: it fires when ANY token is absent
    // ([`relations_missing_proposes_check`] OR
    // [`relations_missing_part_of_check`] OR [`relations_lacks_grounded_in`]),
    // so the #131 `proposes` store (lacking `part_of`/`grounded_in`), an
    // out-of-order `part_of` store (lacking `proposes`/`grounded_in` — e.g. a
    // live store migrated by an S1-lineage binary before this integration),
    // and a `grounded_in`-only store each converge to the eight-kind set in a
    // SINGLE pass; a store already carrying ALL THREE tokens is skipped by
    // every probe (no double rebuild, idempotent under crash-rerun). Runs
    // AFTER the `origin` guarantee above so the copy PRESERVES `origin`
    // byte-for-byte — the v5 falsifies template predates `origin` and copies
    // only (kind, from, to, at); dropping it HERE would erase import
    // provenance. Every legacy edge (all prior kinds) satisfies the wider
    // set, so the copy is total; the DROP takes the `from`/`to` indexes with
    // the old table (SIDECAR_SCHEMA already ran above), so they are
    // re-created inline. Order-independent: probes the DDL, not the version.
    // The shadow table is `relations_v7` — the next free name after the v19
    // fold's `relations_v6` (already taken by the two-token fold this
    // extends).
    if relations_missing_proposes_check(&tx)?
        || relations_missing_part_of_check(&tx)?
        || relations_lacks_grounded_in(&tx)?
    {
        tx.execute_batch(&relations_create_sql("CREATE TABLE relations_v7"))
            .map_err(backend)?;
        tx.execute_batch(
            "INSERT INTO relations_v7 (kind, from_id, to_id, at, origin) \
                 SELECT kind, from_id, to_id, at, origin FROM relations;
             DROP TABLE relations;
             ALTER TABLE relations_v7 RENAME TO relations;
             CREATE INDEX IF NOT EXISTS idx_relations_from ON relations (from_id);
             CREATE INDEX IF NOT EXISTS idx_relations_to ON relations (to_id);",
        )
        .map_err(backend)?;
    }
    tx.execute_batch(&format!("PRAGMA user_version = {SCHEMA_VERSION}"))
        .map_err(backend)?;
    tx.commit().map_err(backend)?;
    Ok(())
}

/// Whether `table` currently has a column named `column` (false when the
/// table does not exist) — the shape probe the migration routes on.
fn table_has_column(
    conn: &rusqlite::Transaction<'_>,
    table: &str,
    column: &str,
) -> Result<bool, StoreError> {
    conn.query_row(
        "SELECT EXISTS(SELECT 1 FROM pragma_table_info(?1) WHERE name = ?2)",
        params![table, column],
        |row| row.get(0),
    )
    .map_err(backend)
}

/// Whether a `classifications` table exists with a pre-v7 kind CHECK —
/// the v3 3-kind or the v4–v6 7-kind set (false when the table does not
/// exist or already carries the 10-kind set). The probe reads the table's
/// stored CREATE sql from `sqlite_master` — the CHECK text is the shape;
/// the QUOTED token `'constraint'` is in the CHECK iff the table is v7
/// (capsule ids/kind VALUES never appear in the DDL, and the unquoted SQL
/// keyword CONSTRAINT can never collide with the quoted probe), exactly
/// the [`relations_has_old_check`] newest-token discipline.
fn classifications_has_old_check(conn: &rusqlite::Transaction<'_>) -> Result<bool, StoreError> {
    let sql: Option<String> = conn
        .query_row(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'classifications'",
            [],
            |row| row.get(0),
        )
        .optional()
        .map_err(backend)?
        .flatten();
    Ok(sql.is_some_and(|ddl| !ddl.contains("'constraint'")))
}

/// Whether a `relations` table exists with the pre-v5 four-kind CHECK
/// (false when the table does not exist or already carries `falsifies`).
/// The probe reads the table's stored CREATE sql from `sqlite_master` — the
/// CHECK text is the shape; `'falsifies'` is in the CHECK iff the table is
/// v5 (edge kind/id VALUES never appear in the DDL, so the token is
/// unambiguous), exactly mirroring [`classifications_has_old_check`].
fn relations_has_old_check(conn: &rusqlite::Transaction<'_>) -> Result<bool, StoreError> {
    let sql: Option<String> = conn
        .query_row(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'relations'",
            [],
            |row| row.get(0),
        )
        .optional()
        .map_err(backend)?
        .flatten();
    Ok(sql.is_some_and(|ddl| !ddl.contains("'falsifies'")))
}

/// Whether a `relations` table exists with a CHECK that does NOT yet admit
/// `proposes` (false when the table is absent or already carries it). The
/// probe reads the stored CREATE sql from `sqlite_master` — the CHECK text is
/// the shape; the QUOTED token `'proposes'` is in the CHECK iff the table
/// carries the b2-staged-review widening (edge kind/id VALUES never appear in
/// the DDL, so the token is unambiguous), exactly the
/// [`relations_has_old_check`] newest-token discipline one rung wider.
fn relations_missing_proposes_check(conn: &rusqlite::Transaction<'_>) -> Result<bool, StoreError> {
    let sql: Option<String> = conn
        .query_row(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'relations'",
            [],
            |row| row.get(0),
        )
        .optional()
        .map_err(backend)?
        .flatten();
    Ok(sql.is_some_and(|ddl| !ddl.contains("'proposes'")))
}

/// Whether a `relations` table exists with a CHECK that predates the v19
/// `'part_of'` widening (false when the table does not exist or already
/// carries `part_of`). Same newest-token discipline as
/// [`relations_has_old_check`]: `'part_of'` is in the CHECK iff the table
/// carries the effort-lifecycle-s1 widening (edge kind VALUES never appear in
/// the DDL, so the token is unambiguous). This probe,
/// [`relations_missing_proposes_check`], and [`relations_lacks_grounded_in`]
/// jointly gate ONE shared rebuild to the eight-kind set, so a store carrying
/// only some of the three tokens (the #131 `proposes` lineage lacking
/// `part_of`/`grounded_in`, an out-of-order `part_of` store lacking
/// `proposes`/`grounded_in`, or a `grounded_in`-only store) converges in a
/// single pass; a table already carrying ALL THREE tokens is skipped by
/// every probe — no double rebuild, idempotent under crash-rerun.
fn relations_missing_part_of_check(conn: &rusqlite::Transaction<'_>) -> Result<bool, StoreError> {
    let sql: Option<String> = conn
        .query_row(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'relations'",
            [],
            |row| row.get(0),
        )
        .optional()
        .map_err(backend)?
        .flatten();
    Ok(sql.is_some_and(|ddl| !ddl.contains("'part_of'")))
}

/// Whether a `relations` table exists with a pre-v20 CHECK that does NOT yet
/// admit `grounded_in` (false when the table is absent or already carries
/// it). The probe reads the stored CREATE sql from `sqlite_master` — the
/// CHECK text is the shape; the QUOTED token `'grounded_in'` is in the CHECK
/// iff the table is v20 (edge kind/id VALUES never appear in the DDL, so the
/// token is unambiguous), exactly the [`relations_missing_proposes_check`]
/// newest-token discipline one rung wider. Jointly gates the three-token
/// fold above with [`relations_missing_proposes_check`] and
/// [`relations_missing_part_of_check`].
fn relations_lacks_grounded_in(conn: &rusqlite::Transaction<'_>) -> Result<bool, StoreError> {
    let sql: Option<String> = conn
        .query_row(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'relations'",
            [],
            |row| row.get(0),
        )
        .optional()
        .map_err(backend)?
        .flatten();
    Ok(sql.is_some_and(|ddl| !ddl.contains("'grounded_in'")))
}

/// Raw column tuple read back from `capsules`, decoded OUTSIDE the rusqlite
/// row closure so decode failures surface as typed [`StoreError`]s, not as
/// stringified backend errors. `canonical_json` is `None` for a tombstoned
/// row — decoding one is the typed [`StoreError::Tombstoned`] marker.
struct RawRow {
    id: String,
    seq: i64,
    canonical_json: Option<String>,
    created_at: String,
    session_id: Option<String>,
}

fn row_to_raw(row: &rusqlite::Row<'_>) -> rusqlite::Result<RawRow> {
    Ok(RawRow {
        id: row.get(0)?,
        seq: row.get(1)?,
        canonical_json: row.get(2)?,
        created_at: row.get(3)?,
        session_id: row.get(4)?,
    })
}

/// [`row_to_raw`] plus the trailing bm25 score column of a search row.
fn row_to_scored(row: &rusqlite::Row<'_>) -> rusqlite::Result<(RawRow, f64)> {
    Ok((row_to_raw(row)?, row.get(5)?))
}

/// Encode the S3 effort membership fence as a bindable JSON array (one bound
/// parameter, expanded in-engine by `json_each` — the ceiling-proof
/// alternative to `params_from_iter`). `None` stays `None`, disabling the
/// `json_each` clause entirely (byte-identical dormancy). The ids are exact
/// store handles (`cap-<n>`) so serialization never fails in practice; a
/// failure surfaces as a backend error rather than a silent unfenced query.
fn effort_ids_json(effort_ids: Option<&[String]>) -> Result<Option<String>, StoreError> {
    effort_ids
        .map(|ids| {
            serde_json::to_string(ids)
                .map_err(|e| StoreError::Backend(format!("effort fence serialization: {e}")))
        })
        .transpose()
}

/// Raw relation row; decoded outside the closure (same pattern as
/// [`RawRow`]).
struct RawRelation {
    kind: String,
    from_id: String,
    to_id: String,
    at: String,
    origin: String,
}

fn row_to_relation(row: &rusqlite::Row<'_>) -> rusqlite::Result<RawRelation> {
    Ok(RawRelation {
        kind: row.get(0)?,
        from_id: row.get(1)?,
        to_id: row.get(2)?,
        at: row.get(3)?,
        origin: row.get(4)?,
    })
}

impl RawRelation {
    fn decode(self) -> Result<RelationRecord, StoreError> {
        let edge = format!("{}->{}", self.from_id, self.to_id);
        let kind = RelationKind::from_wire(&self.kind).ok_or_else(|| StoreError::Corrupt {
            id: edge.clone(),
            reason: format!("relations.kind: unknown value {:?}", self.kind),
        })?;
        let at = parse_at(&edge, "relations.at", &self.at)?;
        let origin = match self.origin.as_str() {
            "manual" => RelationOrigin::Manual,
            "import" => RelationOrigin::Import,
            other => {
                return Err(StoreError::Corrupt {
                    id: edge,
                    reason: format!("relations.origin: unknown value {other:?}"),
                });
            }
        };
        Ok(RelationRecord {
            kind,
            from_id: self.from_id,
            to_id: self.to_id,
            at,
            origin,
        })
    }
}

/// Raw tombstone projection shared by the legacy global id probe and the
/// session-label-private id probe. Their SQL predicates differ; their
/// decoding and corruption semantics do not.
struct RawTombstone {
    capsule_id: String,
    mode: String,
    content_hmac: String,
    at: String,
    reason: String,
    provenance_source: Option<String>,
    provenance_anchor: Option<String>,
    source_hash: Option<String>,
}

fn row_to_tombstone(row: &rusqlite::Row<'_>) -> rusqlite::Result<RawTombstone> {
    Ok(RawTombstone {
        capsule_id: row.get(0)?,
        mode: row.get(1)?,
        content_hmac: row.get(2)?,
        at: row.get(3)?,
        reason: row.get(4)?,
        provenance_source: row.get(5)?,
        provenance_anchor: row.get(6)?,
        source_hash: row.get(7)?,
    })
}

impl RawTombstone {
    fn decode(self) -> Result<TombstoneRecord, StoreError> {
        let mode = TombstoneMode::from_wire(&self.mode).ok_or_else(|| StoreError::Corrupt {
            id: self.capsule_id.clone(),
            reason: format!("tombstones.mode: unknown value {:?}", self.mode),
        })?;
        let at = parse_at(&self.capsule_id, "tombstones.at", &self.at)?;
        Ok(TombstoneRecord {
            capsule_id: self.capsule_id,
            mode,
            content_hmac: self.content_hmac,
            at,
            reason: self.reason,
            provenance_source: self.provenance_source,
            provenance_anchor: self.provenance_anchor,
            source_hash: self.source_hash,
        })
    }
}

/// Raw outcome row (u6h); decoded outside the closure so a re-validation
/// failure surfaces as a typed [`StoreError`], not a stringified backend
/// error — the same pattern as [`RawRelation`].
struct RawOutcome {
    id: String,
    description: String,
    actor: String,
    evidence_ref: Option<String>,
    capsule_id: Option<String>,
    receipt_id: Option<String>,
    score: Option<f64>,
    at: String,
}

fn row_to_outcome(row: &rusqlite::Row<'_>) -> rusqlite::Result<RawOutcome> {
    Ok(RawOutcome {
        id: row.get(0)?,
        description: row.get(1)?,
        actor: row.get(2)?,
        evidence_ref: row.get(3)?,
        capsule_id: row.get(4)?,
        receipt_id: row.get(5)?,
        score: row.get(6)?,
        at: row.get(7)?,
    })
}

impl RawOutcome {
    fn decode(self) -> Result<OutcomeRecord, StoreError> {
        let at = parse_at(&self.id, "outcomes.at", &self.at)?;
        let id = self.id.clone();
        OutcomeRecord::new(
            self.id,
            self.description,
            self.actor,
            self.evidence_ref,
            self.capsule_id,
            self.receipt_id,
            self.score,
            at,
        )
        .map_err(|e| StoreError::Corrupt {
            id,
            reason: e.to_string(),
        })
    }
}

/// Raw preference row (u6i); decoded outside the closure, same discipline
/// as [`RawOutcome`].
struct RawPreference {
    id: String,
    preferred_id: String,
    rejected_id: String,
    context: String,
    actor: String,
    at: String,
}

fn row_to_preference(row: &rusqlite::Row<'_>) -> rusqlite::Result<RawPreference> {
    Ok(RawPreference {
        id: row.get(0)?,
        preferred_id: row.get(1)?,
        rejected_id: row.get(2)?,
        context: row.get(3)?,
        actor: row.get(4)?,
        at: row.get(5)?,
    })
}

impl RawPreference {
    fn decode(self) -> Result<PreferenceRecord, StoreError> {
        let at = parse_at(&self.id, "preferences.at", &self.at)?;
        let id = self.id.clone();
        PreferenceRecord::new(
            self.id,
            self.preferred_id,
            self.rejected_id,
            self.context,
            self.actor,
            at,
        )
        .map_err(|e| StoreError::Corrupt {
            id,
            reason: e.to_string(),
        })
    }
}

/// Raw audit row; decoded outside the closure. `at` stays TEXT here —
/// [`Store::verify_chain`] hashes the STORED bytes, exactly what
/// [`Store::append_audit`] and the migration backfill hashed.
struct RawAudit {
    seq: i64,
    at: String,
    actor: String,
    action: String,
    subject: String,
    reason: Option<String>,
    chained_hash: String,
}

fn row_to_audit(row: &rusqlite::Row<'_>) -> rusqlite::Result<RawAudit> {
    Ok(RawAudit {
        seq: row.get(0)?,
        at: row.get(1)?,
        actor: row.get(2)?,
        action: row.get(3)?,
        subject: row.get(4)?,
        reason: row.get(5)?,
        chained_hash: row.get(6)?,
    })
}

impl RawAudit {
    fn decode(self) -> Result<AuditEvent, StoreError> {
        let at = parse_at(&format!("audit-{}", self.seq), "audit_events.at", &self.at)?;
        Ok(AuditEvent {
            seq: self.seq,
            at,
            actor: self.actor,
            action: self.action,
            subject: self.subject,
            reason: self.reason,
            chained_hash: self.chained_hash,
        })
    }
}

/// Raw session row; decoded outside the closure.
struct RawSession {
    session_id: String,
    started_at: String,
    finished_at: Option<String>,
    summary: Option<String>,
}

fn row_to_session(row: &rusqlite::Row<'_>) -> rusqlite::Result<RawSession> {
    Ok(RawSession {
        session_id: row.get(0)?,
        started_at: row.get(1)?,
        finished_at: row.get(2)?,
        summary: row.get(3)?,
    })
}

impl RawSession {
    fn decode(self) -> Result<SessionRecord, StoreError> {
        let started_at = parse_at(&self.session_id, "sessions.started_at", &self.started_at)?;
        let finished_at = match &self.finished_at {
            None => None,
            Some(text) => Some(parse_at(&self.session_id, "sessions.finished_at", text)?),
        };
        Ok(SessionRecord {
            session_id: self.session_id,
            started_at,
            finished_at,
            summary: self.summary,
        })
    }
}

/// Raw row from the one-statement session-activity projection. SQLite rowid
/// tables can persist a NULL or wrongly typed TEXT PRIMARY KEY, and every
/// projected text field can carry the wrong storage class or invalid UTF-8.
/// Those impossible domain values must reach typed decoding rather than become
/// generic rusqlite conversion errors or disappear.
struct RawSessionActivity {
    session_id: RawSqlValue,
    saves: i64,
    recalls: i64,
    bracket_session_id: RawSqlValue,
    started_at: RawSqlValue,
    finished_at: RawSqlValue,
}

/// Owned SQLite value that preserves storage class and raw TEXT bytes. Reading
/// through `row.get::<String>` or `row.get::<Value>` would convert inside
/// rusqlite: a wrong storage class or invalid-UTF-8 TEXT would escape as a
/// backend error before the store could name persisted corruption. `get_ref`
/// performs no such conversion; this enum owns the bytes past the row callback.
enum RawSqlValue {
    Null,
    Integer(i64),
    Real(f64),
    Text(Vec<u8>),
    Blob(Vec<u8>),
}

impl RawSqlValue {
    fn read(row: &rusqlite::Row<'_>, index: usize) -> rusqlite::Result<Self> {
        Ok(match row.get_ref(index)? {
            ValueRef::Null => Self::Null,
            ValueRef::Integer(value) => Self::Integer(value),
            ValueRef::Real(value) => Self::Real(value),
            ValueRef::Text(bytes) => Self::Text(bytes.to_vec()),
            ValueRef::Blob(bytes) => Self::Blob(bytes.to_vec()),
        })
    }

    fn required_text(self, id: &str, field: &str) -> Result<String, StoreError> {
        match self {
            Self::Text(bytes) => decode_sql_text(id, field, bytes),
            other => Err(other.wrong_storage_class(id, field, "TEXT")),
        }
    }

    fn optional_text(self, id: &str, field: &str) -> Result<Option<String>, StoreError> {
        match self {
            Self::Null => Ok(None),
            Self::Text(bytes) => decode_sql_text(id, field, bytes).map(Some),
            other => Err(other.wrong_storage_class(id, field, "NULL or TEXT")),
        }
    }

    fn wrong_storage_class(self, id: &str, field: &str, expected: &str) -> StoreError {
        let actual = match self {
            Self::Null => "NULL storage class".to_string(),
            Self::Integer(value) => format!("INTEGER storage class ({value})"),
            Self::Real(value) => format!("REAL storage class ({value})"),
            Self::Text(_) => "TEXT storage class".to_string(),
            Self::Blob(bytes) => format!("BLOB storage class ({} bytes)", bytes.len()),
        };
        StoreError::Corrupt {
            id: id.to_string(),
            reason: format!("{field} has {actual}; expected {expected}"),
        }
    }
}

fn decode_sql_text(id: &str, field: &str, bytes: Vec<u8>) -> Result<String, StoreError> {
    String::from_utf8(bytes).map_err(|error| StoreError::Corrupt {
        id: id.to_string(),
        reason: format!("{field} TEXT is not valid UTF-8: {error}"),
    })
}

impl RawSessionActivity {
    fn decode(self) -> Result<SessionActivityRow, StoreError> {
        let session_id = self
            .session_id
            .required_text("session_activity", "session_activity.session_id")?;
        if session_id.trim().is_empty() {
            return Err(StoreError::Corrupt {
                id: session_id,
                reason: "sessions/session activity session_id must be non-empty".to_string(),
            });
        }
        let saves = usize::try_from(self.saves).map_err(|_| StoreError::Corrupt {
            id: session_id.clone(),
            reason: format!(
                "session activity saves must be non-negative, got {}",
                self.saves
            ),
        })?;
        let recalls = usize::try_from(self.recalls).map_err(|_| StoreError::Corrupt {
            id: session_id.clone(),
            reason: format!(
                "session activity recalls must be non-negative, got {}",
                self.recalls
            ),
        })?;
        let bracket_session_id = self
            .bracket_session_id
            .optional_text(&session_id, "sessions.session_id")?;
        let started_at = self
            .started_at
            .optional_text(&session_id, "sessions.started_at")?;
        let finished_at = self
            .finished_at
            .optional_text(&session_id, "sessions.finished_at")?;
        let state = match bracket_session_id {
            None => {
                if started_at.is_some() || finished_at.is_some() {
                    return Err(StoreError::Corrupt {
                        id: session_id.clone(),
                        reason: "session activity label-only row carries bracket timestamps"
                            .to_string(),
                    });
                }
                SessionLabelState::LabelOnly
            }
            Some(bracket_session_id) => {
                if bracket_session_id != session_id {
                    return Err(StoreError::Corrupt {
                        id: session_id.clone(),
                        reason: format!(
                            "session activity bracket label mismatch {bracket_session_id:?}"
                        ),
                    });
                }
                let started_at = started_at.ok_or_else(|| StoreError::Corrupt {
                    id: session_id.clone(),
                    reason: "sessions.started_at is NULL".to_string(),
                })?;
                parse_at(&session_id, "sessions.started_at", &started_at)?;
                match finished_at {
                    None => SessionLabelState::Open,
                    Some(finished_at) => {
                        parse_at(&session_id, "sessions.finished_at", &finished_at)?;
                        SessionLabelState::Closed
                    }
                }
            }
        };
        Ok(SessionActivityRow {
            session_id,
            saves,
            recalls,
            state,
        })
    }
}

fn backend(e: rusqlite::Error) -> StoreError {
    StoreError::Backend(e.to_string())
}

/// Comma-joined `?` placeholders for an `IN (...)` clause of `n` positional
/// values. rusqlite binds no array without the `array` feature, so a
/// candidate id set is expanded to `n` positional params; callers guard
/// `n == 0` (`IN ()` is not valid SQL) before calling.
fn sql_placeholders(n: usize) -> String {
    vec!["?"; n].join(",")
}

/// Drop and re-derive the FTS5 mirror from the canonical `capsules`
/// table, atomically; returns the number of rows indexed. Shared by
/// [`Store::rebuild_fts`] and the open-time heal.
fn rebuild_fts_on(conn: &mut Connection) -> Result<usize, StoreError> {
    let tx = conn.transaction().map_err(backend)?;
    tx.execute_batch("DROP TABLE IF EXISTS capsules_fts;")
        .map_err(backend)?;
    tx.execute_batch(FTS_DDL).map_err(backend)?;
    tx.execute_batch(FTS_POPULATE).map_err(backend)?;
    let indexed: i64 = tx
        .query_row("SELECT count(*) FROM capsules_fts", [], |row| row.get(0))
        .map_err(backend)?;
    tx.commit().map_err(backend)?;
    usize::try_from(indexed).map_err(|e| StoreError::Backend(format!("fts row count: {e}")))
}

/// One caller term as an FTS5 match expression: the AND of its
/// whitespace/punctuation-separated words, each individually quoted via
/// [`fts_phrase`] — so a multi-word term matches order- and
/// adjacency-insensitively ("tokio pin" finds "pin tokio at 1.38.0"),
/// while a single-word term stays the plain quoted token. Splitting on
/// non-alphanumeric boundaries mirrors what `unicode61` does to the
/// indexed content, and quoting every word keeps the injection guarantee
/// of [`fts_phrase`]: no caller bytes ever reach the parser as syntax.
fn fts_term_expr(term: &str) -> String {
    let words: Vec<String> = term
        .split(|c: char| !c.is_alphanumeric())
        .filter(|w| !w.is_empty())
        .map(fts_phrase)
        .collect();
    match words.len() {
        // Unreachable for the alphanumeric-filtered callers; total anyway.
        0 => fts_phrase(term),
        1 => words.into_iter().next().unwrap_or_default(),
        _ => format!("({})", words.join(" AND ")),
    }
}

/// Quote one caller term as a single FTS5 string: wrapped in double
/// quotes, internal double quotes doubled. Inside quotes FTS5 treats the
/// content as a phrase of tokens — never as query syntax, so a term can
/// never smuggle `OR`/`NEAR`/`-`/`*`/column-filter operators.
fn fts_phrase(term: &str) -> String {
    let mut out = String::with_capacity(term.len() + 2);
    out.push('"');
    for ch in term.chars() {
        if ch == '"' {
            out.push('"');
        }
        out.push(ch);
    }
    out.push('"');
    out
}

/// The canonical audit line — the EXACT byte sequence the journal chain
/// hashes for one row. Fixed field order (struct declaration order via
/// serde), `reason` explicit (`null` when absent) so every row has one
/// unambiguous serialization. Inputs are the STORED column values (`at` as
/// its persisted RFC3339 text, never re-parsed), which is what makes the
/// migration backfill and a live [`Store::append_audit`] compute identical
/// chains for identical ledgers.
fn audit_canonical_line(
    seq: i64,
    at: &str,
    actor: &str,
    action: &str,
    subject: &str,
    reason: Option<&str>,
) -> Result<String, StoreError> {
    #[derive(Serialize)]
    struct Line<'a> {
        seq: i64,
        at: &'a str,
        actor: &'a str,
        action: &'a str,
        subject: &'a str,
        reason: Option<&'a str>,
    }
    serde_json::to_string(&Line {
        seq,
        at,
        actor,
        action,
        subject,
        reason,
    })
    .map_err(|e| StoreError::Serialize(e.to_string()))
}

/// One journal chain link: lowercase-hex
/// `sha256(prev_hash + canonical_line)` — `prev_hash` is the PREVIOUS
/// row's `chained_hash` hex text (`""` for the first row). Pure function
/// of its inputs; the whole chain is therefore a pure function of the
/// ledger rows in `seq` order.
fn chained_hash_of(prev_hash: &str, canonical_line: &str) -> String {
    let mut bytes = Vec::with_capacity(prev_hash.len() + canonical_line.len());
    bytes.extend_from_slice(prev_hash.as_bytes());
    bytes.extend_from_slice(canonical_line.as_bytes());
    sha256_hex(&bytes)
}

/// Normalize one synonym side for storage and lookup: trim, lowercase,
/// fold Latin diacritics ([`fold_diacritic`]) — so `" Configuração "`
/// and `configuracao` are the same term. Interior whitespace survives
/// (multi-word terms are legal); non-Latin text passes through unchanged.
fn fold_term(text: &str) -> String {
    text.trim()
        .to_lowercase()
        .chars()
        .map(fold_diacritic)
        .collect()
}

/// Fold one lowercase char to its base letter the way FTS5's `unicode61`
/// tokenizer does for the Latin diacritic range (`remove_diacritics` is ON
/// in the default tokenizer this crate pins). Closed table over the
/// Latin-1 Supplement + Latin Extended-A letters that PT/ES/FR/DE text
/// actually uses; anything else passes through unchanged.
///
/// The ONE crate-wide fold table (v2 convergence): the store owns it
/// because the dependency direction is engine → store, and the engine's
/// explain-side tokenizer (`retrieve::tokens`) imports THIS fn — index
/// and explain can no longer drift apart.
pub(crate) const fn fold_diacritic(c: char) -> char {
    match c {
        'à' | 'á' | 'â' | 'ã' | 'ä' | 'å' | 'ā' | 'ă' | 'ą' => 'a',
        'ç' | 'ć' | 'ĉ' | 'ċ' | 'č' => 'c',
        'è' | 'é' | 'ê' | 'ë' | 'ē' | 'ĕ' | 'ė' | 'ę' | 'ě' => 'e',
        'ì' | 'í' | 'î' | 'ï' | 'ĩ' | 'ī' | 'ĭ' | 'į' | 'ı' => 'i',
        'ñ' | 'ń' | 'ņ' | 'ň' => 'n',
        'ò' | 'ó' | 'ô' | 'õ' | 'ö' | 'ø' | 'ō' | 'ŏ' | 'ő' => 'o',
        'ù' | 'ú' | 'û' | 'ü' | 'ũ' | 'ū' | 'ŭ' | 'ů' | 'ű' | 'ų' => 'u',
        'ý' | 'ÿ' => 'y',
        'ď' => 'd',
        'ĝ' | 'ğ' | 'ġ' | 'ģ' => 'g',
        'ĥ' => 'h',
        'ĵ' => 'j',
        'ķ' => 'k',
        'ĺ' | 'ļ' | 'ľ' | 'ł' => 'l',
        'ŕ' | 'ŗ' | 'ř' => 'r',
        'ś' | 'ŝ' | 'ş' | 'š' => 's',
        'ţ' | 'ť' => 't',
        'ŵ' => 'w',
        'ź' | 'ż' | 'ž' => 'z',
        other => other,
    }
}

/// Keyed HMAC-SHA-256 tombstone fingerprint of removed content:
/// `hmac-sha256:<hex>` over `domain_tag || capsule_id || 0x00 || content`
/// with the boundary-injected key. Pure function of its inputs — no clock,
/// no randomness (the determinism law). The domain tag separates this use
/// from any other HMAC keyed on the same key; the capsule id in the data
/// makes fingerprints of identical content differ across capsules (no
/// cross-tombstone correlation); the NUL separator keeps the (id, content)
/// framing injective (ids never contain NUL).
fn content_hmac_hex(hmac_key: &[u8], capsule_id: &str, content: &str) -> String {
    // HMAC accepts any key length (it pads/hashes internally) — this
    // cannot fail; the unreachable error is mapped, never unwrapped.
    let mac = Hmac::<Sha256>::new_from_slice(hmac_key);
    let mut mac = match mac {
        Ok(mac) => mac,
        // Unreachable by HMAC's definition; a zeroed marker would be a lie,
        // so derive a distinguishable constant instead of panicking.
        Err(_) => return "hmac-sha256:invalid-key-length".to_string(),
    };
    mac.update(TOMBSTONE_HMAC_DOMAIN_TAG);
    mac.update(capsule_id.as_bytes());
    mac.update(&[0]);
    mac.update(content.as_bytes());
    let digest = mac.finalize().into_bytes();
    format!("hmac-sha256:{}", hex::encode(digest))
}

impl RawRow {
    /// Decode + re-validate: the canonical JSON funnels through the
    /// Capsule's validated deserialization (no-provenance rows cannot
    /// round-trip), and `created_at` must parse as RFC3339. A NULL
    /// `canonical_json` is the tombstone state: the typed
    /// [`StoreError::Tombstoned`] marker, never content, never `None`.
    fn decode(self) -> Result<StoredCapsule, StoreError> {
        let Some(canonical_json) = self.canonical_json else {
            return Err(StoreError::Tombstoned { id: self.id });
        };
        let capsule: Capsule =
            serde_json::from_str(&canonical_json).map_err(|e| StoreError::Corrupt {
                id: self.id.clone(),
                reason: format!("canonical_json: {e}"),
            })?;
        let created_at =
            OffsetDateTime::parse(&self.created_at, &Rfc3339).map_err(|e| StoreError::Corrupt {
                id: self.id.clone(),
                reason: format!("created_at: {e}"),
            })?;
        Ok(StoredCapsule {
            id: CapsuleId(self.id),
            seq: self.seq,
            capsule,
            created_at,
            session_id: self.session_id,
        })
    }
}

/// Map the UNIQUE-index violation on `capsules.source_hash` to its typed
/// error; every other failure stays a stringified backend error.
fn map_unique_source_hash(e: rusqlite::Error, source_hash: &str) -> StoreError {
    if let rusqlite::Error::SqliteFailure(ffi, Some(msg)) = &e
        && ffi.code == rusqlite::ErrorCode::ConstraintViolation
        && msg.contains("capsules.source_hash")
    {
        return StoreError::DuplicateSourceHash(source_hash.to_string());
    }
    backend(e)
}

/// RFC3339 text for a timestamp column. Formatting fails only for years
/// outside 0..=9999 — surfaced typed, never a panic.
fn rfc3339_text(ts: OffsetDateTime) -> Result<String, StoreError> {
    ts.format(&Rfc3339)
        .map_err(|e| StoreError::Serialize(format!("timestamp not RFC3339-formattable: {e}")))
}

/// Parse a sidecar timestamp column, surfacing failures as typed
/// [`StoreError::Corrupt`] with the row and column named.
fn parse_at(id: &str, column: &str, text: &str) -> Result<OffsetDateTime, StoreError> {
    OffsetDateTime::parse(text, &Rfc3339).map_err(|e| StoreError::Corrupt {
        id: id.to_string(),
        reason: format!("{column}: {e}"),
    })
}

/// Prove that the indexed capsule identity projection still names the
/// authoritative row bytes. Live rows derive identity from decoded canonical
/// provenance; forgotten skeletons derive it from their retained tombstone.
/// A projection is lookup fuel only — never authority by itself.
fn validate_capsule_identity_projection(
    id: &str,
    projected_source_hash: &str,
    canonical_json: Option<&str>,
    tombstone_id: Option<&str>,
    tombstone_source_hash: Option<&str>,
) -> Result<(), StoreError> {
    if projected_source_hash.trim().is_empty() {
        return Err(StoreError::Corrupt {
            id: id.to_string(),
            reason: "capsules.source_hash is empty, so content identity cannot be proven"
                .to_string(),
        });
    }
    if let Some(canonical_json) = canonical_json {
        if tombstone_id.is_some() {
            return Err(StoreError::Corrupt {
                id: id.to_string(),
                reason: "live canonical capsule also has a tombstone marker".to_string(),
            });
        }
        let capsule: Capsule =
            serde_json::from_str(canonical_json).map_err(|error| StoreError::Corrupt {
                id: id.to_string(),
                reason: format!("canonical_json: {error}"),
            })?;
        let canonical_source_hash = capsule.provenance().source_hash.as_str();
        if projected_source_hash != canonical_source_hash {
            return Err(StoreError::Corrupt {
                id: id.to_string(),
                reason: format!(
                    "capsules.source_hash {projected_source_hash:?} disagrees with \
                     canonical provenance.source_hash {canonical_source_hash:?}"
                ),
            });
        }
        return Ok(());
    }

    if tombstone_id.is_none() {
        return Err(StoreError::Corrupt {
            id: id.to_string(),
            reason: "capsule skeleton has neither canonical bytes nor a tombstone marker"
                .to_string(),
        });
    }
    let tombstone_source_hash = tombstone_source_hash.ok_or_else(|| StoreError::Corrupt {
        id: id.to_string(),
        reason: "tombstone identity is missing source_hash".to_string(),
    })?;
    if tombstone_source_hash.trim().is_empty() {
        return Err(StoreError::Corrupt {
            id: id.to_string(),
            reason: "tombstone source_hash is empty, so content identity cannot be proven"
                .to_string(),
        });
    }
    if projected_source_hash != tombstone_source_hash {
        return Err(StoreError::Corrupt {
            id: id.to_string(),
            reason: format!(
                "capsules.source_hash {projected_source_hash:?} disagrees with \
                 tombstone source_hash {tombstone_source_hash:?}"
            ),
        });
    }
    Ok(())
}

/// Validate every identity projection a sync push might resolve through.
/// Pre-v11 tombstones can legitimately lack their retained source hash; they
/// remain unresolvable and are rejected later only if a destination event row
/// needs them. Every identity that IS present must agree with its authority.
fn validate_all_capsule_identity_projections(conn: &Connection) -> Result<(), StoreError> {
    let mut stmt = conn
        .prepare(
            "SELECT c.id, c.source_hash, c.canonical_json, \
                    t.capsule_id, t.source_hash \
             FROM capsules c \
             LEFT JOIN tombstones t ON t.capsule_id = c.id \
             ORDER BY c.seq",
        )
        .map_err(backend)?;
    let rows = stmt
        .query_map([], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, Option<String>>(2)?,
                row.get::<_, Option<String>>(3)?,
                row.get::<_, Option<String>>(4)?,
            ))
        })
        .map_err(backend)?;
    for row in rows {
        let (id, projected_source_hash, canonical_json, tombstone_id, tombstone_source_hash) =
            row.map_err(backend)?;
        if canonical_json.is_none() && tombstone_id.is_some() && tombstone_source_hash.is_none() {
            // A migrated pre-v11 tombstone has no portable content identity.
            // It cannot participate in event-time rebinding, but must not
            // block an unrelated push merely by existing.
            continue;
        }
        validate_capsule_identity_projection(
            &id,
            &projected_source_hash,
            canonical_json.as_deref(),
            tombstone_id.as_deref(),
            tombstone_source_hash.as_deref(),
        )?;
    }
    Ok(())
}

/// The kebab-case wire name of an authority class, derived from the
/// Capsule's own serde — a single source of truth, no duplicated name
/// table in the store.
fn authority_class_text(class: AuthorityClass) -> Result<String, StoreError> {
    match serde_json::to_value(class) {
        Ok(serde_json::Value::String(s)) => Ok(s),
        Ok(other) => Err(StoreError::Serialize(format!(
            "authority_class serialized to non-string {other}"
        ))),
        Err(e) => Err(StoreError::Serialize(e.to_string())),
    }
}

#[cfg(test)]
mod tests {
    #![allow(
        clippy::unwrap_used,
        clippy::expect_used,
        reason = "tests use unwrap/expect so fixture failures fail at the assertion site"
    )]

    use super::*;
    use crate::capsule::{Confidence, Freshness, Provenance, Scope, sha256_hex};
    use time::macros::datetime;

    /// Fixed injected boundary instant — a value no 2026 wall clock can
    /// produce, with sub-second precision and a non-UTC offset so
    /// injected-now exactness is proven end to end.
    fn injected_now() -> OffsetDateTime {
        datetime!(2001-02-03 04:05:06.123456789 +02:00)
    }

    /// A later fixed instant for second events.
    fn later_now() -> OffsetDateTime {
        datetime!(2001-02-03 04:05:07 +02:00)
    }

    /// Distinct `text` ⇒ distinct `source_hash` (the UNIQUE-indexed
    /// column), so fixtures never collide unless a test wants them to.
    fn capsule(text: &str, project: &str) -> Capsule {
        Capsule::new(
            text.to_string(),
            Provenance {
                source: "session:2026-07-18".to_string(),
                anchor: "PLAN.md:67".to_string(),
                source_hash: sha256_hex(text.as_bytes()),
            },
            Confidence::new(0.9).unwrap(),
            Freshness {
                valid_from: datetime!(2026-07-18 12:30:45 UTC),
                valid_to: None,
            },
            Scope {
                project_id: project.to_string(),
            },
            AuthorityClass::UserStated,
            false,
        )
        .unwrap()
    }

    #[test]
    fn append_then_get_returns_identical_capsule() {
        let mut store = Store::open_in_memory().unwrap();
        let c = capsule("nott monorepo lives at /nott/monorepo", "nmemory");
        let id = store.append(&c, injected_now()).unwrap();
        assert_eq!(id.as_str(), "cap-1");

        let got = store.get(id.as_str()).unwrap().unwrap();
        assert_eq!(got.capsule, c);
        assert_eq!(got.id, id);
        assert_eq!(got.seq, 1);
        assert_eq!(got.session_id, None);
    }

    #[test]
    fn ids_are_sequence_derived() {
        let mut store = Store::open_in_memory().unwrap();
        for (n, text) in ["first", "second", "third"].iter().enumerate() {
            let id = store
                .append(&capsule(text, "nmemory"), injected_now())
                .unwrap();
            assert_eq!(id.as_str(), format!("cap-{}", n + 1));
        }
    }

    /// S1: pin state is the LATEST event per capsule. Pin, then unpin — the
    /// newest row decides `is_pinned` / `list_pinned`, while `pin_state_of`
    /// always reports the newest event (pinned or not). The dormant default
    /// (no event) is not pinned, absent from the list, and no state.
    #[test]
    fn pin_state_is_the_latest_event() {
        let mut store = Store::open_in_memory().unwrap();
        let id = store
            .append(&capsule("load-bearing anchor", "nmemory"), injected_now())
            .unwrap();
        let cap = id.as_str().to_string();

        // Dormant default.
        assert!(!store.is_pinned(&cap).unwrap());
        assert!(store.pin_state_of(&cap).unwrap().is_none());
        assert!(store.list_pinned().unwrap().is_empty());

        // Pin.
        let rec = store
            .append_pin_event(&cap, true, "load-bearing", "tester", injected_now())
            .unwrap();
        assert!(rec.pinned);
        assert_eq!(rec.capsule_id, cap);
        assert!(store.is_pinned(&cap).unwrap());
        assert_eq!(
            store
                .list_pinned()
                .unwrap()
                .iter()
                .map(|c| c.as_str().to_string())
                .collect::<Vec<_>>(),
            vec![cap.clone()]
        );
        let state = store.pin_state_of(&cap).unwrap().unwrap();
        assert!(state.pinned);
        assert_eq!(state.reason, "load-bearing");
        assert_eq!(state.actor, "tester");

        // Unpin: newest event wins.
        store
            .append_pin_event(&cap, false, "no longer load-bearing", "tester", later_now())
            .unwrap();
        assert!(!store.is_pinned(&cap).unwrap());
        assert!(store.list_pinned().unwrap().is_empty());
        let state = store.pin_state_of(&cap).unwrap().unwrap();
        assert!(!state.pinned);
        assert_eq!(state.reason, "no longer load-bearing");
    }

    /// S1: `append_pin_event` rejects an empty reason/actor and an id that
    /// names no stored capsule — a rejected append leaves no pin state.
    #[test]
    fn append_pin_event_rejects_empty_fields_and_unknown_capsule() {
        let mut store = Store::open_in_memory().unwrap();
        let id = store
            .append(&capsule("anchor", "nmemory"), injected_now())
            .unwrap();
        let cap = id.as_str().to_string();
        assert_eq!(
            store.append_pin_event(&cap, true, "   ", "tester", injected_now()),
            Err(StoreError::EmptyField("reason"))
        );
        assert_eq!(
            store.append_pin_event(&cap, true, "why", "", injected_now()),
            Err(StoreError::EmptyField("actor"))
        );
        assert_eq!(
            store.append_pin_event("cap-999", true, "why", "tester", injected_now()),
            Err(StoreError::UnknownCapsule("cap-999".to_string()))
        );
        assert!(!store.is_pinned(&cap).unwrap());
    }

    /// S1 red-test (6) essence: pin is a SIDECAR — `canonical_snapshot` reads
    /// only `capsules`, so pin/unpin events (and the v16 migration that adds
    /// the table) leave the capsule comparand byte-identical. This is the
    /// determinism proof the migration red-test leans on.
    #[test]
    fn pin_events_never_move_the_canonical_snapshot() {
        let mut store = Store::open_in_memory().unwrap();
        for text in ["first anchor", "second anchor", "third anchor"] {
            store
                .append(&capsule(text, "nmemory"), injected_now())
                .unwrap();
        }
        let before = store.canonical_snapshot().unwrap();
        store
            .append_pin_event("cap-1", true, "pin one", "tester", injected_now())
            .unwrap();
        store
            .append_pin_event("cap-2", true, "pin two", "tester", injected_now())
            .unwrap();
        store
            .append_pin_event("cap-1", false, "unpin one", "tester", later_now())
            .unwrap();
        let after = store.canonical_snapshot().unwrap();
        assert_eq!(
            before, after,
            "pin events must never move the capsule snapshot"
        );
    }

    #[test]
    fn get_unknown_id_is_none() {
        let store = Store::open_in_memory().unwrap();
        assert!(store.get("cap-999").unwrap().is_none());
        assert!(store.get("junk").unwrap().is_none());
    }

    #[test]
    fn list_returns_all_and_honors_project_and_limit() {
        let mut store = Store::open_in_memory().unwrap();
        store
            .append(&capsule("alpha fact", "proj-a"), injected_now())
            .unwrap();
        store
            .append(&capsule("beta fact", "proj-a"), injected_now())
            .unwrap();
        store
            .append(&capsule("gamma fact", "proj-b"), injected_now())
            .unwrap();

        let all = store.list(ListFilter::default()).unwrap();
        assert_eq!(all.len(), 3);
        let ids: Vec<&str> = all.iter().map(|s| s.id.as_str()).collect();
        assert_eq!(ids, ["cap-1", "cap-2", "cap-3"]);

        let proj_a = store
            .list(ListFilter {
                project_id: Some("proj-a".to_string()),
                ..ListFilter::default()
            })
            .unwrap();
        assert_eq!(proj_a.len(), 2);
        assert!(
            proj_a
                .iter()
                .all(|s| s.capsule.scope().project_id == "proj-a")
        );

        let limited = store
            .list(ListFilter {
                project_id: Some("proj-a".to_string()),
                limit: Some(1),
                ..ListFilter::default()
            })
            .unwrap();
        assert_eq!(limited.len(), 1);
        // limit keeps the NEWEST rows (w1d): proj-a holds cap-1/cap-2, the
        // one-row window shows cap-2.
        assert_eq!(limited[0].id.as_str(), "cap-2");

        let none = store
            .list(ListFilter {
                project_id: Some("no-such-project".to_string()),
                ..ListFilter::default()
            })
            .unwrap();
        assert!(none.is_empty());
    }

    #[test]
    fn reopen_from_same_file_persists() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("memory.sqlite3");
        {
            let mut store = Store::open(&path).unwrap();
            store
                .append(&capsule("persisted one", "nmemory"), injected_now())
                .unwrap();
            store
                .append(&capsule("persisted two", "nmemory"), injected_now())
                .unwrap();
        }

        let mut store = Store::open(&path).unwrap();
        let all = store.list(ListFilter::default()).unwrap();
        assert_eq!(all.len(), 2);
        assert_eq!(all[0].capsule.content(), "persisted one");
        assert_eq!(all[1].capsule.content(), "persisted two");
        assert_eq!(
            store.get("cap-1").unwrap().unwrap().capsule.content(),
            "persisted one"
        );

        // The sequence continues across reopen — no id reuse, no reset.
        let id = store
            .append(&capsule("persisted three", "nmemory"), injected_now())
            .unwrap();
        assert_eq!(id.as_str(), "cap-3");
    }

    #[test]
    fn created_at_is_exactly_the_injected_now() {
        let mut store = Store::open_in_memory().unwrap();
        let now = injected_now();
        let id = store
            .append(&capsule("boundary time", "nmemory"), now)
            .unwrap();

        // Exact equality with a 2001 instant (nanosecond precision,
        // non-UTC offset): the store took the boundary value — a wall
        // clock could not produce it.
        let got = store.get(id.as_str()).unwrap().unwrap();
        assert_eq!(got.created_at, now);
    }

    #[test]
    fn store_source_reads_no_clock_or_randomness() {
        // Structural negative for the determinism law (behavioral proof:
        // created_at_is_exactly_the_injected_now). Needles are assembled
        // with concat! so this test's own source never contains them.
        let src = include_str!("store.rs");
        let needles = [
            concat!("OffsetDateTime::", "now"),
            concat!("System", "Time"),
            concat!("Instant::", "now"),
            concat!("rand", "::"),
            concat!("fastrand", "::"),
        ];
        for needle in needles {
            assert!(
                !src.contains(needle),
                "store.rs must not contain {needle:?} (no clock, no randomness in the store)"
            );
        }
    }

    #[test]
    fn find_by_source_hash_probes_idempotency() {
        let mut store = Store::open_in_memory().unwrap();
        let c = capsule("dedup target", "nmemory");
        store.append(&c, injected_now()).unwrap();

        let found = store
            .find_by_source_hash(&c.provenance().source_hash)
            .unwrap()
            .unwrap();
        assert_eq!(found.id.as_str(), "cap-1");
        assert_eq!(found.capsule, c);

        assert!(store.find_by_source_hash("no-such-hash").unwrap().is_none());
    }

    #[test]
    fn duplicate_source_hash_rejected_typed() {
        let mut store = Store::open_in_memory().unwrap();
        let c = capsule("same source twice", "nmemory");
        store.append(&c, injected_now()).unwrap();

        let err = store.append(&c, injected_now()).unwrap_err();
        assert_eq!(
            err,
            StoreError::DuplicateSourceHash(c.provenance().source_hash.clone())
        );
        // Nothing was written by the rejected append.
        assert_eq!(store.list(ListFilter::default()).unwrap().len(), 1);
    }

    #[test]
    fn canonical_snapshot_is_byte_stable_jsonl() {
        let c1 = capsule("alpha canonical", "nmemory");
        let c2 = capsule("beta canonical", "other");
        let t1 = injected_now();
        let t2 = later_now();

        // Same (capsule, now) sequence into two fresh stores → identical
        // bytes (replay determinism, the h3 comparand).
        let mut a = Store::open_in_memory().unwrap();
        let mut b = Store::open_in_memory().unwrap();
        for store in [&mut a, &mut b] {
            store.append(&c1, t1).unwrap();
            store.append(&c2, t2).unwrap();
        }
        let snap = a.canonical_snapshot().unwrap();
        assert_eq!(snap, b.canonical_snapshot().unwrap());

        // Golden line shape: StoredCapsule field order with the embedded
        // capsule's own canonical bytes, newline-terminated. A session-less
        // capsule line carries NO session_id key — pre-v2 snapshot bytes
        // are unchanged.
        let expected_first = format!(
            "{{\"id\":\"cap-1\",\"seq\":1,\"capsule\":{},\
             \"created_at\":\"2001-02-03T04:05:06.123456789+02:00\"}}",
            c1.to_canonical_json().unwrap()
        );
        let mut lines = snap.lines();
        assert_eq!(lines.next().unwrap(), expected_first);
        assert_eq!(lines.clone().count(), 1);
        assert!(snap.ends_with('\n'));

        // A snapshot line parses back through the validated funnel —
        // the replay path is real.
        let parsed: StoredCapsule = serde_json::from_str(&expected_first).unwrap();
        assert_eq!(parsed.capsule, c1);
        assert_eq!(parsed.created_at, t1);
        assert_eq!(parsed.session_id, None);

        // Empty store → empty string.
        let empty = Store::open_in_memory().unwrap();
        assert_eq!(empty.canonical_snapshot().unwrap(), "");
    }

    #[test]
    fn future_schema_version_fails_closed() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("memory.sqlite3");
        Store::open(&path).unwrap();

        // Schema v21 is one past this build's current v20 stamp: an unknown
        // version is refused, never guessed at. v16 (pin), v17 (git), v18
        // (staged review), v19 (part_of), and v20 (planning-plane
        // grounded_in) all migrate in place, so the first genuinely unknown
        // version is 21.
        let conn = rusqlite::Connection::open(&path).unwrap();
        conn.execute_batch("PRAGMA user_version = 21").unwrap();
        drop(conn);

        let err = Store::open(&path).unwrap_err();
        assert_eq!(err, StoreError::UnsupportedSchemaVersion(21));
    }

    #[test]
    fn fresh_store_is_stamped_current() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("memory.sqlite3");
        Store::open(&path).unwrap();
        let conn = rusqlite::Connection::open(&path).unwrap();
        let version: i64 = conn
            .query_row("PRAGMA user_version", [], |row| row.get(0))
            .unwrap();
        assert_eq!(version, SCHEMA_VERSION);
    }

    #[test]
    fn open_store_sets_busy_timeout() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("memory.sqlite3");
        let store = Store::open(&path).unwrap();
        // busy_timeout is connection-scoped runtime state (not persisted to
        // the file), so it must be read from the store's OWN connection — a
        // fresh connection would report the default 0.
        let timeout: i64 = store
            .conn
            .query_row("PRAGMA busy_timeout", [], |row| row.get(0))
            .unwrap();
        assert_eq!(timeout, BUSY_TIMEOUT_MS);
    }

    #[test]
    fn concurrent_writer_waits_for_lock_instead_of_erroring() {
        use std::sync::mpsc;
        use std::time::Duration;

        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("memory.sqlite3");
        // Initialize the schema + WAL on disk, then keep this second
        // session's Store open on the same file — the two-machine-over-SSH
        // shape (one store, two live writers).
        let mut writer = Store::open(&path).unwrap();
        writer
            .append(&capsule("first session write", "nmemory"), injected_now())
            .unwrap();

        // A concurrent session grabs the write lock and holds it briefly.
        let (locked_tx, locked_rx) = mpsc::channel();
        let hold_path = path.clone();
        let holder = std::thread::spawn(move || {
            let conn = rusqlite::Connection::open(&hold_path).unwrap();
            // Take the WAL write lock now (not lazily), announce it, hold.
            conn.execute_batch("BEGIN IMMEDIATE").unwrap();
            locked_tx.send(()).unwrap();
            std::thread::sleep(Duration::from_millis(300));
            conn.execute_batch("COMMIT").unwrap();
        });

        // Once the lock is held, this write collides. With busy_timeout it
        // waits for the holder's COMMIT and SUCCEEDS; without it, SQLite
        // returns SQLITE_BUSY immediately ("database is locked").
        locked_rx.recv().unwrap();
        let result = writer.append(&capsule("second session write", "nmemory"), later_now());

        holder.join().unwrap();
        // The append began strictly after the holder took the lock (the
        // channel recv), so it could only return once the holder COMMITted:
        // success here IS the proof it waited, not raced. Before the fix
        // this returned Err(Backend("database is locked")) at once.
        assert!(
            result.is_ok(),
            "concurrent write must wait for the lock, not fail: {result:?}"
        );
    }

    #[test]
    fn concurrent_open_waits_for_lock_instead_of_erroring() {
        use std::sync::mpsc;
        use std::time::Duration;

        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("memory.sqlite3");
        // Initialize on disk so a second open re-runs only the open-time
        // schema-init writes — the exact path that reported
        // "cannot open store: ... database is locked".
        {
            let mut store = Store::open(&path).unwrap();
            store
                .append(&capsule("seed session write", "nmemory"), injected_now())
                .unwrap();
        }

        // A concurrent session holds the write lock briefly.
        let (locked_tx, locked_rx) = mpsc::channel();
        let hold_path = path.clone();
        let holder = std::thread::spawn(move || {
            let conn = rusqlite::Connection::open(&hold_path).unwrap();
            conn.execute_batch("BEGIN IMMEDIATE").unwrap();
            locked_tx.send(()).unwrap();
            std::thread::sleep(Duration::from_millis(300));
            conn.execute_batch("COMMIT").unwrap();
        });

        // Opening the store runs schema-init writes; with busy_timeout +
        // BEGIN IMMEDIATE it waits for the holder's COMMIT and SUCCEEDS.
        // Without the fix the schema-init upgrade returns SQLITE_BUSY at once.
        locked_rx.recv().unwrap();
        let opened = Store::open(&path);

        holder.join().unwrap();
        // The open began strictly after the holder took the lock, so its
        // schema-init writes could only complete once the holder COMMITted:
        // success here IS the proof it waited. Before the fix this returned
        // Err(Backend("database is locked")) at once ("cannot open store").
        assert!(
            opened.is_ok(),
            "concurrent open must wait for the lock, not fail: {opened:?}"
        );
    }

    // ------------------------------------------------------------------
    // v1 → v2 migration
    // ------------------------------------------------------------------

    /// The v1 on-disk schema, verbatim from the pre-w1 store (s2+s4+h4):
    /// NOT NULL canonical_json, no session_id, supersede pair `relations`.
    const V1_SCHEMA: &str = "
CREATE TABLE IF NOT EXISTS capsules (
    seq             INTEGER PRIMARY KEY,
    id              TEXT NOT NULL UNIQUE,
    canonical_json  TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    source_hash     TEXT NOT NULL,
    project_id      TEXT NOT NULL,
    authority_class TEXT NOT NULL,
    valid_from      TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_capsules_source_hash
    ON capsules (source_hash);
CREATE INDEX IF NOT EXISTS idx_capsules_project_id
    ON capsules (project_id);
CREATE INDEX IF NOT EXISTS idx_capsules_authority_class
    ON capsules (authority_class);
CREATE INDEX IF NOT EXISTS idx_capsules_valid_from
    ON capsules (valid_from);
CREATE TABLE IF NOT EXISTS relations (
    superseded_id   TEXT NOT NULL,
    superseded_by   TEXT NOT NULL,
    at              TEXT NOT NULL,
    PRIMARY KEY (superseded_id, superseded_by)
);
CREATE VIRTUAL TABLE IF NOT EXISTS capsules_fts
    USING fts5(content, tokenize = 'unicode61');
CREATE TABLE IF NOT EXISTS usage (
    capsule_id       TEXT PRIMARY KEY,
    recall_count     INTEGER NOT NULL,
    last_recalled_at TEXT NOT NULL
);
PRAGMA user_version = 1;
";

    /// Build a faithful v1 file: two capsules (cap-2 superseding cap-1),
    /// one usage row — exactly the rows the v1 code would have written.
    fn seed_v1_file(path: &std::path::Path) -> (Capsule, Capsule) {
        let c1 = capsule("v1 stale claim about the monorepo", "nmemory");
        let c2 = capsule("v1 replacing claim about the monorepo", "nmemory");
        let conn = rusqlite::Connection::open(path).unwrap();
        conn.execute_batch(V1_SCHEMA).unwrap();
        for (seq, c) in [(1_i64, &c1), (2_i64, &c2)] {
            conn.execute(
                "INSERT INTO capsules \
                 (seq, id, canonical_json, created_at, source_hash, project_id, \
                  authority_class, valid_from) \
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)",
                params![
                    seq,
                    format!("cap-{seq}"),
                    c.to_canonical_json().unwrap(),
                    rfc3339_text(injected_now()).unwrap(),
                    c.provenance().source_hash,
                    c.scope().project_id,
                    authority_class_text(c.authority_class()).unwrap(),
                    rfc3339_text(c.freshness().valid_from).unwrap(),
                ],
            )
            .unwrap();
            conn.execute(
                "INSERT INTO capsules_fts (rowid, content) VALUES (?1, ?2)",
                params![seq, c.content()],
            )
            .unwrap();
        }
        conn.execute(
            "INSERT INTO relations (superseded_id, superseded_by, at) VALUES (?1, ?2, ?3)",
            params!["cap-1", "cap-2", rfc3339_text(later_now()).unwrap()],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO usage (capsule_id, recall_count, last_recalled_at) \
             VALUES ('cap-2', 3, ?1)",
            params![rfc3339_text(later_now()).unwrap()],
        )
        .unwrap();
        drop(conn);
        (c1, c2)
    }

    /// `(name, type, notnull, dflt_value, pk)` per column — the shape
    /// comparand proving migrated and fresh files can never drift.
    fn table_shape(
        path: &std::path::Path,
        table: &str,
    ) -> Vec<(String, String, i64, Option<String>, i64)> {
        let conn = rusqlite::Connection::open(path).unwrap();
        let mut stmt = conn
            .prepare(
                "SELECT name, type, \"notnull\", dflt_value, pk \
                 FROM pragma_table_info(?1) ORDER BY cid",
            )
            .unwrap();
        let rows = stmt
            .query_map([table], |row| {
                Ok((
                    row.get(0)?,
                    row.get(1)?,
                    row.get(2)?,
                    row.get(3)?,
                    row.get(4)?,
                ))
            })
            .unwrap();
        rows.map(Result::unwrap).collect()
    }

    #[test]
    fn v1_file_migrates_in_place_to_current() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("memory.sqlite3");
        let (c1, c2) = seed_v1_file(&path);

        // Opening IS the migration.
        let mut store = Store::open(&path).unwrap();

        // Stamped v2.
        {
            let conn = rusqlite::Connection::open(&path).unwrap();
            let version: i64 = conn
                .query_row("PRAGMA user_version", [], |row| row.get(0))
                .unwrap();
            assert_eq!(version, SCHEMA_VERSION);
        }

        // Every capsule row survived, byte-identical through the funnel.
        assert_eq!(store.get("cap-1").unwrap().unwrap().capsule, c1);
        assert_eq!(store.get("cap-2").unwrap().unwrap().capsule, c2);
        assert_eq!(store.get("cap-1").unwrap().unwrap().session_id, None);

        // The v1 supersede pair became the generalized edge with donor
        // orientation (from = newer) and the ORIGINAL at.
        assert!(store.is_superseded("cap-1").unwrap());
        assert!(!store.is_superseded("cap-2").unwrap());
        let edges = store.list_relations("cap-1").unwrap();
        assert_eq!(
            edges,
            vec![RelationRecord {
                kind: RelationKind::Supersedes,
                from_id: "cap-2".to_string(),
                to_id: "cap-1".to_string(),
                at: later_now(),
                // Migrated pre-origin edges backfill as caller-written.
                origin: RelationOrigin::Manual,
            }]
        );

        // Usage sidecar survived untouched.
        assert_eq!(store.usage_of("cap-2").unwrap().unwrap().recall_count, 3);

        // Recall still works over the migrated file.
        let hits = store.search_fts(&["replacing".to_string()], None).unwrap();
        assert_eq!(hits.len(), 1);
        assert_eq!(hits[0].0.id.as_str(), "cap-2");

        // The snapshot of the migrated file is byte-identical to a fresh
        // v2 store replaying the same appends — migration moved no
        // canonical byte.
        let mut fresh = Store::open_in_memory().unwrap();
        fresh.append(&c1, injected_now()).unwrap();
        fresh.append(&c2, injected_now()).unwrap();
        assert_eq!(
            store.canonical_snapshot().unwrap(),
            fresh.canonical_snapshot().unwrap()
        );

        // The append sequence continues where v1 left off.
        let id = store
            .append(&capsule("post-migration capture", "nmemory"), later_now())
            .unwrap();
        assert_eq!(id.as_str(), "cap-3");

        // Every new sidecar API works on the migrated file.
        store
            .append_audit("tester", "migrate.check", "cap-3", None, later_now())
            .unwrap();
        // The journal chain is live on the v1→v3 path too (empty ledger
        // gained its first chained row).
        assert_eq!(store.verify_chain().unwrap(), 1);
        assert!(store.journal_head().unwrap().is_some());
        store
            .set_classification("cap-3", "fact", "project", later_now())
            .unwrap();
        store.open_session("s-mig", later_now()).unwrap();
        store
            .forget_capsule(
                "cap-3",
                TombstoneMode::Redacted,
                "test",
                b"key",
                later_now(),
            )
            .unwrap();
        assert!(store.get_tombstone("cap-3").unwrap().is_some());

        // Shape law: the migrated tables and fresh-created tables have
        // IDENTICAL column definitions (the shared-DDL no-drift proof).
        let fresh_dir = tempfile::tempdir().unwrap();
        let fresh_path = fresh_dir.path().join("fresh.sqlite3");
        Store::open(&fresh_path).unwrap();
        for table in ["capsules", "relations", "audit_events", "tombstones"] {
            assert_eq!(
                table_shape(&path, table),
                table_shape(&fresh_path, table),
                "migrated {table} shape must equal fresh v2 shape"
            );
        }

        // Reopening the migrated file is a plain v2 open (idempotent).
        drop(store);
        let store = Store::open(&path).unwrap();
        assert!(store.is_superseded("cap-1").unwrap());
        assert_eq!(store.get("cap-2").unwrap().unwrap().capsule, c2);
    }

    #[test]
    fn s2_era_v1_file_without_relations_migrates() {
        // A v1 file written before the additive h4/s4 tables existed:
        // capsules only. Migration must not assume any optional table.
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("memory.sqlite3");
        let c = capsule("ancient s2-era capture", "nmemory");
        {
            let conn = rusqlite::Connection::open(&path).unwrap();
            conn.execute_batch(
                "CREATE TABLE capsules (
                    seq             INTEGER PRIMARY KEY,
                    id              TEXT NOT NULL UNIQUE,
                    canonical_json  TEXT NOT NULL,
                    created_at      TEXT NOT NULL,
                    source_hash     TEXT NOT NULL,
                    project_id      TEXT NOT NULL,
                    authority_class TEXT NOT NULL,
                    valid_from      TEXT NOT NULL
                );
                PRAGMA user_version = 1;",
            )
            .unwrap();
            conn.execute(
                "INSERT INTO capsules VALUES (1, 'cap-1', ?1, ?2, ?3, ?4, ?5, ?6)",
                params![
                    c.to_canonical_json().unwrap(),
                    rfc3339_text(injected_now()).unwrap(),
                    c.provenance().source_hash,
                    c.scope().project_id,
                    authority_class_text(c.authority_class()).unwrap(),
                    rfc3339_text(c.freshness().valid_from).unwrap(),
                ],
            )
            .unwrap();
        }

        let store = Store::open(&path).unwrap();
        assert_eq!(store.get("cap-1").unwrap().unwrap().capsule, c);
        assert!(store.list_relations("cap-1").unwrap().is_empty());
        assert!(store.blockers_of("cap-1").unwrap().is_empty());
        // The fts mirror was created AND healed (count drift 1 vs 0).
        let hits = store.search_fts(&["ancient".to_string()], None).unwrap();
        assert_eq!(hits.len(), 1);
    }

    // ------------------------------------------------------------------
    // relations (generalized)
    // ------------------------------------------------------------------

    #[test]
    fn supersede_marks_old_and_validates() {
        let mut store = Store::open_in_memory().unwrap();
        let old = store
            .append(&capsule("stale claim", "nmemory"), injected_now())
            .unwrap();
        let new = store
            .append(&capsule("replacing claim", "nmemory"), injected_now())
            .unwrap();
        assert!(!store.is_superseded(old.as_str()).unwrap());

        store
            .supersede(old.as_str(), new.as_str(), injected_now())
            .unwrap();
        assert!(store.is_superseded(old.as_str()).unwrap());
        assert!(!store.is_superseded(new.as_str()).unwrap());
        // Sidecar law: the old capsule's bytes are untouched — get still
        // returns it, byte-identical.
        assert_eq!(
            store.get(old.as_str()).unwrap().unwrap().capsule.content(),
            "stale claim"
        );

        // Idempotent re-record of the same pair.
        store
            .supersede(old.as_str(), new.as_str(), injected_now())
            .unwrap();
        assert!(store.is_superseded(old.as_str()).unwrap());

        // Unknown ids are typed rejections and record nothing.
        let err = store
            .supersede("cap-999", new.as_str(), injected_now())
            .unwrap_err();
        assert_eq!(err, StoreError::UnknownCapsule("cap-999".to_string()));
        let err = store
            .supersede(new.as_str(), "cap-999", injected_now())
            .unwrap_err();
        assert_eq!(err, StoreError::UnknownCapsule("cap-999".to_string()));
        assert!(!store.is_superseded(new.as_str()).unwrap());

        // Self-supersede is a typed rejection.
        let err = store
            .supersede(new.as_str(), new.as_str(), injected_now())
            .unwrap_err();
        assert_eq!(
            err,
            StoreError::SelfRelation {
                kind: RelationKind::Supersedes,
                id: new.to_string(),
            }
        );
        assert!(!store.is_superseded(new.as_str()).unwrap());

        // An unknown id is simply not superseded.
        assert!(!store.is_superseded("cap-999").unwrap());
    }

    #[test]
    fn supersede_is_a_thin_wrapper_over_the_edge_store() {
        let mut store = Store::open_in_memory().unwrap();
        store
            .append(&capsule("old wrapper", "nmemory"), injected_now())
            .unwrap();
        store
            .append(&capsule("new wrapper", "nmemory"), injected_now())
            .unwrap();
        store.supersede("cap-1", "cap-2", injected_now()).unwrap();

        // Donor orientation: from = the newer capsule, to = the replaced.
        assert_eq!(
            store.list_relations("cap-1").unwrap(),
            vec![RelationRecord {
                kind: RelationKind::Supersedes,
                from_id: "cap-2".to_string(),
                to_id: "cap-1".to_string(),
                at: injected_now(),
                origin: RelationOrigin::Manual,
            }]
        );
    }

    #[test]
    fn supersede_relation_persists_across_reopen() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("memory.sqlite3");
        {
            let mut store = Store::open(&path).unwrap();
            store
                .append(&capsule("old durable", "nmemory"), injected_now())
                .unwrap();
            store
                .append(&capsule("new durable", "nmemory"), injected_now())
                .unwrap();
            store.supersede("cap-1", "cap-2", injected_now()).unwrap();
        }
        let store = Store::open(&path).unwrap();
        assert!(store.is_superseded("cap-1").unwrap());
        assert!(!store.is_superseded("cap-2").unwrap());
    }

    #[test]
    fn upsert_relation_all_kinds_projections_and_validation() {
        let mut store = Store::open_in_memory().unwrap();
        for text in ["node one", "node two", "node three"] {
            store
                .append(&capsule(text, "nmemory"), injected_now())
                .unwrap();
        }
        let t1 = injected_now();
        let t2 = later_now();

        // One edge of every kind.
        store
            .upsert_relation(RelationKind::DerivedFrom, "cap-2", "cap-1", t1)
            .unwrap();
        store
            .upsert_relation(RelationKind::Witnesses, "cap-3", "cap-2", t1)
            .unwrap();
        store
            .upsert_relation(RelationKind::Blocks, "cap-1", "cap-3", t2)
            .unwrap();
        store
            .upsert_relation(RelationKind::Blocks, "cap-2", "cap-3", t2)
            .unwrap();

        // Full edge list, deterministic (at, kind, from, to) order.
        let all = store.all_relations().unwrap();
        assert_eq!(all.len(), 4);
        assert_eq!(
            all.iter()
                .map(|r| (r.kind, r.from_id.as_str(), r.to_id.as_str()))
                .collect::<Vec<_>>(),
            vec![
                (RelationKind::DerivedFrom, "cap-2", "cap-1"),
                (RelationKind::Witnesses, "cap-3", "cap-2"),
                (RelationKind::Blocks, "cap-1", "cap-3"),
                (RelationKind::Blocks, "cap-2", "cap-3"),
            ]
        );

        // Both-endpoint listing.
        let cap2_edges = store.list_relations("cap-2").unwrap();
        assert_eq!(cap2_edges.len(), 3);

        // blocked_by projection: cap-3 is blocked by cap-1 and cap-2.
        assert_eq!(
            store.blockers_of("cap-3").unwrap(),
            vec!["cap-1".to_string(), "cap-2".to_string()]
        );
        assert!(store.blockers_of("cap-1").unwrap().is_empty());
        assert!(store.blockers_of("cap-999").unwrap().is_empty());

        // Idempotent re-record keeps the FIRST at.
        store
            .upsert_relation(RelationKind::DerivedFrom, "cap-2", "cap-1", t2)
            .unwrap();
        let derived: Vec<_> = store
            .all_relations()
            .unwrap()
            .into_iter()
            .filter(|r| r.kind == RelationKind::DerivedFrom)
            .collect();
        assert_eq!(derived.len(), 1);
        assert_eq!(derived[0].at, t1);

        // Self-relation is a typed rejection for every kind.
        for kind in RelationKind::ALL {
            let err = store
                .upsert_relation(kind, "cap-1", "cap-1", t1)
                .unwrap_err();
            assert_eq!(
                err,
                StoreError::SelfRelation {
                    kind,
                    id: "cap-1".to_string(),
                }
            );
        }

        // Unknown endpoints are typed rejections; nothing recorded.
        let err = store
            .upsert_relation(RelationKind::Blocks, "cap-999", "cap-1", t1)
            .unwrap_err();
        assert_eq!(err, StoreError::UnknownCapsule("cap-999".to_string()));
        let err = store
            .upsert_relation(RelationKind::Blocks, "cap-1", "cap-999", t1)
            .unwrap_err();
        assert_eq!(err, StoreError::UnknownCapsule("cap-999".to_string()));
        assert_eq!(store.all_relations().unwrap().len(), 4);
    }

    // ------------------------------------------------------------------
    // audit ledger
    // ------------------------------------------------------------------

    #[test]
    fn audit_ledger_appends_filters_and_orders() {
        let mut store = Store::open_in_memory().unwrap();
        let t1 = injected_now();
        let t2 = later_now();

        assert_eq!(
            store
                .append_audit("session:w1", "memory.ingest", "cap-1", None, t1)
                .unwrap(),
            1
        );
        assert_eq!(
            store
                .append_audit(
                    "session:w1",
                    "memory.forget",
                    "cap-1",
                    Some("owner asked"),
                    t2
                )
                .unwrap(),
            2
        );
        assert_eq!(
            store
                .append_audit("session:w1", "session.open", "s-1", None, t2)
                .unwrap(),
            3
        );

        // Most recent first; every column round-trips; at is the exact
        // injected instant.
        let all = store.list_audit(None, None).unwrap();
        assert_eq!(all.len(), 3);
        assert_eq!(all[0].seq, 3);
        assert_eq!(all[2].seq, 1);
        assert_eq!(all[2].at, t1);
        assert_eq!(all[2].actor, "session:w1");
        assert_eq!(all[2].action, "memory.ingest");
        assert_eq!(all[2].subject, "cap-1");
        assert_eq!(all[2].reason, None);
        assert_eq!(all[1].reason.as_deref(), Some("owner asked"));

        // Subject fence.
        let cap1 = store.list_audit(None, Some("cap-1")).unwrap();
        assert_eq!(cap1.len(), 2);
        assert!(cap1.iter().all(|e| e.subject == "cap-1"));

        // Limit keeps the most recent.
        let latest = store.list_audit(Some(1), None).unwrap();
        assert_eq!(latest.len(), 1);
        assert_eq!(latest[0].seq, 3);

        // Empty required fields are typed rejections.
        for (actor, action, subject, field) in [
            ("", "a", "s", "actor"),
            ("x", "  ", "s", "action"),
            ("x", "a", "", "subject"),
        ] {
            let err = store
                .append_audit(actor, action, subject, None, t1)
                .unwrap_err();
            assert_eq!(err, StoreError::EmptyField(field));
        }
        assert_eq!(store.list_audit(None, None).unwrap().len(), 3);
    }

    // ------------------------------------------------------------------
    // classifications
    // ------------------------------------------------------------------

    #[test]
    fn classification_set_get_upsert_and_closed_sets() {
        let mut store = Store::open_in_memory().unwrap();
        store
            .append(&capsule("classify me", "nmemory"), injected_now())
            .unwrap();

        assert_eq!(store.get_classification("cap-1").unwrap(), None);

        store
            .set_classification("cap-1", "fact", "project", injected_now())
            .unwrap();
        assert_eq!(
            store.get_classification("cap-1").unwrap().unwrap(),
            ClassificationRecord {
                kind: "fact".to_string(),
                scope: "project".to_string(),
                at: injected_now(),
            }
        );

        // Upsert: the label is replaced, the at re-stamped.
        store
            .set_classification("cap-1", "decision", "global", later_now())
            .unwrap();
        assert_eq!(
            store.get_classification("cap-1").unwrap().unwrap(),
            ClassificationRecord {
                kind: "decision".to_string(),
                scope: "global".to_string(),
                at: later_now(),
            }
        );

        // Every member of both closed sets is accepted.
        for kind in CLASSIFICATION_KINDS {
            for scope in CLASSIFICATION_SCOPES {
                store
                    .set_classification("cap-1", kind, scope, injected_now())
                    .unwrap();
            }
        }

        // Outside the closed sets: typed rejections, nothing written.
        let err = store
            .set_classification("cap-1", "vibe", "project", injected_now())
            .unwrap_err();
        assert_eq!(
            err,
            StoreError::InvalidClassification {
                field: "kind",
                value: "vibe".to_string(),
            }
        );
        let err = store
            .set_classification("cap-1", "fact", "universe", injected_now())
            .unwrap_err();
        assert_eq!(
            err,
            StoreError::InvalidClassification {
                field: "scope",
                value: "universe".to_string(),
            }
        );

        // Unknown capsule: typed rejection.
        let err = store
            .set_classification("cap-999", "fact", "project", injected_now())
            .unwrap_err();
        assert_eq!(err, StoreError::UnknownCapsule("cap-999".to_string()));
    }

    // ------------------------------------------------------------------
    // tombstones / forget
    // ------------------------------------------------------------------

    #[test]
    fn forget_capsule_is_typed_irreversible_and_sticky() {
        let mut store = Store::open_in_memory().unwrap();
        let c = capsule("radioactive secret content", "nmemory");
        let id = store.append(&c, injected_now()).unwrap();
        store
            .append(&capsule("innocent bystander", "nmemory"), injected_now())
            .unwrap();

        // Guards first: empty reason, unknown id.
        assert_eq!(
            store
                .forget_capsule(id.as_str(), TombstoneMode::Purged, "  ", b"k", later_now())
                .unwrap_err(),
            StoreError::EmptyReason
        );
        assert_eq!(
            store
                .forget_capsule("cap-999", TombstoneMode::Purged, "why", b"k", later_now())
                .unwrap_err(),
            StoreError::UnknownCapsule("cap-999".to_string())
        );

        store
            .forget_capsule(
                id.as_str(),
                TombstoneMode::Redacted,
                "owner asked",
                b"boundary-key",
                later_now(),
            )
            .unwrap();

        // get: the typed marker, never the content, never a silent None.
        assert_eq!(
            store.get(id.as_str()).unwrap_err(),
            StoreError::Tombstoned { id: id.to_string() }
        );
        // list and snapshot exclude the row; the bystander lives on.
        let live = store.list(ListFilter::default()).unwrap();
        assert_eq!(live.len(), 1);
        assert_eq!(live[0].id.as_str(), "cap-2");
        assert!(!store.canonical_snapshot().unwrap().contains("radioactive"));
        // recall can never match the removed content.
        assert!(
            store
                .search_fts(&["radioactive".to_string()], None)
                .unwrap()
                .is_empty()
        );
        // the idempotency probe surfaces the marker (sticky, not silent).
        assert_eq!(
            store
                .find_by_source_hash(&c.provenance().source_hash)
                .unwrap_err(),
            StoreError::Tombstoned { id: id.to_string() }
        );
        // re-ingesting the same source is still blocked: no resurrection.
        assert_eq!(
            store.append(&c, later_now()).unwrap_err(),
            StoreError::DuplicateSourceHash(c.provenance().source_hash.clone())
        );

        // The marker record: mode, injected at, reason, keyed hmac.
        let marker = store.get_tombstone(id.as_str()).unwrap().unwrap();
        assert_eq!(marker.capsule_id, id.to_string());
        assert_eq!(marker.mode, TombstoneMode::Redacted);
        assert_eq!(marker.at, later_now());
        assert_eq!(marker.reason, "owner asked");
        assert!(marker.content_hmac.starts_with("hmac-sha256:"));
        assert_eq!(
            marker.content_hmac,
            content_hmac_hex(b"boundary-key", id.as_str(), c.content())
        );
        assert!(!marker.content_hmac.contains("radioactive"));
        // w1d: mode `redacted` RETAINS provenance on the marker — the
        // documented reason to choose it over `purged`.
        assert_eq!(
            marker.provenance_source.as_deref(),
            Some("session:2026-07-18")
        );
        assert_eq!(marker.provenance_anchor.as_deref(), Some("PLAN.md:67"));
        // ...while a purged sibling retains nothing.
        store
            .forget_capsule(
                "cap-2",
                TombstoneMode::Purged,
                "purged sibling probe",
                b"boundary-key",
                later_now(),
            )
            .unwrap();
        let purged = store.get_tombstone("cap-2").unwrap().unwrap();
        assert_eq!(purged.provenance_source, None);
        assert_eq!(purged.provenance_anchor, None);
        // Restore the bystander-free flow for the assertions below: cap-2
        // is now tombstoned too, so re-list.
        assert!(store.list(ListFilter::default()).unwrap().is_empty());

        // The skeleton survives: the raw row keeps its projections with a
        // NULL content column.
        // (No content bytes: canonical_json IS NULL.)
        // Verified via the public surface: audit-style probes above; the
        // seq spine is intact — the next append continues after cap-2.
        let next = store
            .append(&capsule("post-forget capture", "nmemory"), later_now())
            .unwrap();
        assert_eq!(next.as_str(), "cap-3");

        // Forgetting twice: the typed marker, not a second tombstone.
        assert_eq!(
            store
                .forget_capsule(
                    id.as_str(),
                    TombstoneMode::Purged,
                    "again",
                    b"k",
                    later_now()
                )
                .unwrap_err(),
            StoreError::Tombstoned { id: id.to_string() }
        );

        // No tombstone for never-forgotten or unknown ids (cap-2 was
        // purged above as the provenance-retention control).
        assert!(store.get_tombstone("cap-3").unwrap().is_none());
        assert!(store.get_tombstone("cap-999").unwrap().is_none());
    }

    #[test]
    fn tombstone_id_probe_is_private_to_the_supplied_session_label() {
        let mut store = Store::open_in_memory().unwrap();
        store.open_session("sess-a", injected_now()).unwrap();
        store.open_session("sess-b", injected_now()).unwrap();
        store
            .append_with_session(
                &capsule("private tombstone alpha", "nmemory"),
                "sess-a",
                injected_now(),
            )
            .unwrap();
        store
            .append_with_session(
                &capsule("private tombstone beta", "nmemory"),
                "sess-b",
                injected_now(),
            )
            .unwrap();
        store
            .forget_capsule(
                "cap-1",
                TombstoneMode::Purged,
                "session privacy probe",
                b"k",
                later_now(),
            )
            .unwrap();
        store
            .forget_capsule(
                "cap-2",
                TombstoneMode::Purged,
                "session privacy probe",
                b"k",
                later_now(),
            )
            .unwrap();

        assert!(
            store
                .get_tombstone_for_session_label("cap-1", "sess-a")
                .unwrap()
                .is_some()
        );
        assert!(
            store
                .get_tombstone_for_session_label("cap-1", "sess-b")
                .unwrap()
                .is_none(),
            "another session label must not learn that cap-1 was forgotten"
        );
        assert!(
            store
                .get_tombstone_for_session_label("cap-2", "sess-a")
                .unwrap()
                .is_none()
        );
        assert!(
            store.get_tombstone("cap-1").unwrap().is_some(),
            "the omitted-session legacy probe remains global"
        );
    }

    #[test]
    fn forget_survives_reopen_and_fts_rebuild() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("memory.sqlite3");
        {
            let mut store = Store::open(&path).unwrap();
            store
                .append(
                    &capsule("ephemeral credential zzyzx", "nmemory"),
                    injected_now(),
                )
                .unwrap();
            store
                .append(&capsule("durable neighbor", "nmemory"), injected_now())
                .unwrap();
            store
                .forget_capsule("cap-1", TombstoneMode::Purged, "leak", b"key", later_now())
                .unwrap();
        }

        let mut store = Store::open(&path).unwrap();
        assert_eq!(
            store.get("cap-1").unwrap_err(),
            StoreError::Tombstoned {
                id: "cap-1".to_string(),
            }
        );
        assert_eq!(
            store.get_tombstone("cap-1").unwrap().unwrap().mode,
            TombstoneMode::Purged
        );
        assert!(
            store
                .search_fts(&["zzyzx".to_string()], None)
                .unwrap()
                .is_empty()
        );

        // A full mirror re-derivation keeps the forgotten content
        // unfindable (tombstoned rows re-derive as '').
        assert_eq!(store.rebuild_fts().unwrap(), 2);
        assert!(
            store
                .search_fts(&["zzyzx".to_string()], None)
                .unwrap()
                .is_empty()
        );
        let hits = store.search_fts(&["durable".to_string()], None).unwrap();
        assert_eq!(hits.len(), 1);
    }

    #[test]
    fn forget_leaves_no_content_bytes_in_the_file() {
        // The honesty bar: after forget + close, the marker string is not
        // recoverable from the raw database bytes (secure_delete zeroes
        // the overwritten cells; the WAL is checkpointed on close).
        let marker = "XUNFORGETTABLEMARKER7431ZQ";
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("memory.sqlite3");
        {
            let mut store = Store::open(&path).unwrap();
            store
                .append(
                    &capsule(&format!("the secret is {marker} keep it"), "nmemory"),
                    injected_now(),
                )
                .unwrap();
            store
                .forget_capsule(
                    "cap-1",
                    TombstoneMode::Purged,
                    "leak drill",
                    b"k",
                    later_now(),
                )
                .unwrap();
        }
        let mut bytes = std::fs::read(&path).unwrap();
        for suffix in ["-wal", "-shm"] {
            let side = path.with_file_name(format!("memory.sqlite3{suffix}"));
            if side.exists() {
                bytes.extend(std::fs::read(&side).unwrap());
            }
        }
        let needle = marker.as_bytes();
        let found = bytes.windows(needle.len()).any(|w| w == needle);
        assert!(
            !found,
            "forgotten content must not survive in the raw file bytes"
        );

        // Replay determinism WITH forget: same append+forget sequence in
        // two stores → byte-identical snapshots.
        let build = || {
            let mut s = Store::open_in_memory().unwrap();
            s.append(&capsule("kept alpha", "nmemory"), injected_now())
                .unwrap();
            s.append(&capsule("dropped beta", "nmemory"), injected_now())
                .unwrap();
            s.forget_capsule("cap-2", TombstoneMode::Purged, "drill", b"k", later_now())
                .unwrap();
            s.canonical_snapshot().unwrap()
        };
        assert_eq!(build(), build());
    }

    #[test]
    fn content_hmac_is_keyed_and_deterministic() {
        let a = content_hmac_hex(b"key-one", "cap-1", "the content");
        // Deterministic: same inputs, same output.
        assert_eq!(a, content_hmac_hex(b"key-one", "cap-1", "the content"));
        assert!(a.starts_with("hmac-sha256:"));
        assert_eq!(a.len(), "hmac-sha256:".len() + 64);

        // Keyed: a different key changes the output — a dictionary built
        // without the key matches nothing.
        assert_ne!(a, content_hmac_hex(b"key-two", "cap-1", "the content"));
        // Per-capsule: the same content in another capsule fingerprints
        // differently — no cross-tombstone correlation.
        assert_ne!(a, content_hmac_hex(b"key-one", "cap-2", "the content"));
        // And it is NOT the unkeyed content hash.
        assert_ne!(
            a.trim_start_matches("hmac-sha256:"),
            sha256_hex(b"the content")
        );
    }

    // ------------------------------------------------------------------
    // sessions
    // ------------------------------------------------------------------

    #[test]
    fn session_lifecycle_brackets_honestly() {
        let mut store = Store::open_in_memory().unwrap();
        let t1 = injected_now();
        let t2 = later_now();

        // Guards: empty id, unknown finish.
        assert_eq!(
            store.open_session("  ", t1).unwrap_err(),
            StoreError::EmptyField("session_id")
        );
        assert_eq!(
            store.finish_session("s-none", None, t2).unwrap_err(),
            StoreError::UnknownSession("s-none".to_string())
        );

        store.open_session("s-1", t1).unwrap();
        assert_eq!(
            store.get_session("s-1").unwrap().unwrap(),
            SessionRecord {
                session_id: "s-1".to_string(),
                started_at: t1,
                finished_at: None,
                summary: None,
            }
        );

        // A bracket opens once.
        assert_eq!(
            store.open_session("s-1", t2).unwrap_err(),
            StoreError::DuplicateSession("s-1".to_string())
        );

        store
            .finish_session("s-1", Some("landed w1 sidecars"), t2)
            .unwrap();
        assert_eq!(
            store.get_session("s-1").unwrap().unwrap(),
            SessionRecord {
                session_id: "s-1".to_string(),
                started_at: t1,
                finished_at: Some(t2),
                summary: Some("landed w1 sidecars".to_string()),
            }
        );

        // A bracket closes once — the first close is never overwritten.
        assert_eq!(
            store
                .finish_session("s-1", Some("rewrite"), t2)
                .unwrap_err(),
            StoreError::SessionFinished("s-1".to_string())
        );

        // Deterministic list order (started_at, session_id).
        store.open_session("s-0", t2).unwrap();
        store.open_session("a-later", t2).unwrap();
        let sessions = store.list_sessions().unwrap();
        let ids: Vec<&str> = sessions.iter().map(|s| s.session_id.as_str()).collect();
        assert_eq!(ids, ["s-1", "a-later", "s-0"]);

        assert!(store.get_session("missing").unwrap().is_none());
    }

    #[test]
    fn session_activity_preaggregates_rows_without_capsule_receipt_fanout() {
        let mut store = Store::open_in_memory().unwrap();
        store.open_session("shared", injected_now()).unwrap();
        store
            .append_with_session(
                &capsule("session activity first", "nmemory"),
                "shared",
                injected_now(),
            )
            .unwrap();
        store
            .append_with_session(
                &capsule("session activity second", "nmemory"),
                "shared",
                injected_now(),
            )
            .unwrap();
        // A forgotten capsule keeps its canonical skeleton and therefore
        // remains one save in the label projection.
        store
            .forget_capsule(
                "cap-2",
                TombstoneMode::Purged,
                "session activity fixture",
                b"session-activity-key",
                later_now(),
            )
            .unwrap();
        for _ in 0..3 {
            store
                .record_recall_receipt(
                    &["session activity".to_string()],
                    &["cap-1"],
                    None,
                    None,
                    Some("shared"),
                    later_now(),
                )
                .unwrap();
        }

        assert_eq!(
            store.session_activity().unwrap(),
            vec![SessionActivityRow {
                session_id: "shared".to_string(),
                saves: 2,
                recalls: 3,
                state: SessionLabelState::Open,
            }],
            "2 capsule rows plus 3 receipt rows must stay 2/3, never fan out to 6/6"
        );
    }

    #[test]
    fn session_activity_unions_exact_local_imported_and_receipt_only_labels() {
        let dir = tempfile::tempdir().unwrap();
        let incoming_path = dir.path().join("incoming.sqlite3");
        let mut local = Store::open_in_memory().unwrap();
        let started = injected_now();
        local.open_session("local-b", started).unwrap();
        local.open_session("local-a", started).unwrap();
        local
            .finish_session("local-a", Some("closed"), later_now())
            .unwrap();
        let grounded_id = local
            .append(
                &capsule("receipt-only grounding capsule", "nmemory"),
                started,
            )
            .unwrap();
        let receipt_id = local
            .record_recall_receipt(
                &["receipt only".to_string()],
                &[grounded_id.as_str()],
                None,
                None,
                Some("receipt-only"),
                later_now(),
            )
            .unwrap();
        let returned_ids = local.receipt_returned_ids(&receipt_id).unwrap().unwrap();
        validate_receipt_capsules_on(&local.conn, &receipt_id, &returned_ids).unwrap();

        let imported_labels = vec![
            "Case".to_string(),
            "case".to_string(),
            " spaced ".to_string(),
            "\u{00e9}".to_string(),
            "e\u{0301}".to_string(),
            "evil \" ] } |\nnode".to_string(),
            "nul\0label".to_string(),
        ];
        {
            let mut incoming = Store::open(&incoming_path).unwrap();
            for (index, label) in imported_labels.iter().enumerate() {
                incoming.open_session(label, started).unwrap();
                incoming
                    .append_with_session(
                        &capsule(&format!("imported session label {index}"), "nmemory"),
                        label,
                        started,
                    )
                    .unwrap();
            }
        }
        local
            .merge_from(&incoming_path, b"session-activity-key")
            .unwrap();

        let rows = local.session_activity().unwrap();
        assert_eq!(rows[0].session_id, "local-a");
        assert_eq!(rows[0].state, SessionLabelState::Closed);
        assert_eq!(rows[1].session_id, "local-b");
        assert_eq!(rows[1].state, SessionLabelState::Open);

        let actual_label_only: Vec<&str> = rows[2..]
            .iter()
            .map(|row| {
                assert_eq!(row.state, SessionLabelState::LabelOnly);
                row.session_id.as_str()
            })
            .collect();
        let mut expected_label_only = imported_labels.clone();
        expected_label_only.push("receipt-only".to_string());
        expected_label_only.sort_by(|a, b| a.as_bytes().cmp(b.as_bytes()));
        assert_eq!(
            actual_label_only,
            expected_label_only
                .iter()
                .map(String::as_str)
                .collect::<Vec<_>>(),
            "label-only rows follow exact BINARY label order"
        );
        for label in &imported_labels {
            let row = rows.iter().find(|row| &row.session_id == label).unwrap();
            assert_eq!((row.saves, row.recalls), (1, 0), "label {label:?}");
        }
        let receipt_only = rows
            .iter()
            .find(|row| row.session_id == "receipt-only")
            .unwrap();
        assert_eq!((receipt_only.saves, receipt_only.recalls), (0, 1));
    }

    #[test]
    fn session_activity_merge_collision_aggregates_under_local_bracket_state() {
        let dir = tempfile::tempdir().unwrap();
        let incoming_path = dir.path().join("incoming.sqlite3");
        let mut local = Store::open_in_memory().unwrap();
        local
            .open_session("sess-collision", injected_now())
            .unwrap();
        local
            .append_with_session(
                &capsule("local collision capsule", "nmemory"),
                "sess-collision",
                injected_now(),
            )
            .unwrap();
        local
            .finish_session("sess-collision", None, later_now())
            .unwrap();
        local
            .record_recall_receipt(
                &["collision".to_string()],
                &["cap-1"],
                None,
                None,
                Some("sess-collision"),
                later_now(),
            )
            .unwrap();
        {
            let mut incoming = Store::open(&incoming_path).unwrap();
            incoming
                .open_session("sess-collision", injected_now())
                .unwrap();
            incoming
                .append_with_session(
                    &capsule("incoming collision capsule", "nmemory"),
                    "sess-collision",
                    injected_now(),
                )
                .unwrap();
            incoming
                .open_session("import-only", injected_now())
                .unwrap();
            incoming
                .append_with_session(
                    &capsule("incoming orphan label", "nmemory"),
                    "import-only",
                    injected_now(),
                )
                .unwrap();
        }
        local
            .merge_from(&incoming_path, b"session-activity-key")
            .unwrap();

        assert_eq!(
            local.session_activity().unwrap(),
            vec![
                SessionActivityRow {
                    session_id: "sess-collision".to_string(),
                    saves: 2,
                    recalls: 1,
                    state: SessionLabelState::Closed,
                },
                SessionActivityRow {
                    session_id: "import-only".to_string(),
                    saves: 1,
                    recalls: 0,
                    state: SessionLabelState::LabelOnly,
                },
            ]
        );
    }

    #[test]
    fn session_activity_rejects_corrupt_labels_and_bracket_timestamps() {
        fn assert_corrupt(store: &Store, needle: &str) {
            let error = store.session_activity().unwrap_err();
            assert!(
                matches!(error, StoreError::Corrupt { ref reason, .. } if reason.contains(needle)),
                "typed corruption must name {needle:?}, got {error:?}"
            );
        }

        let started = rfc3339_text(injected_now()).unwrap();
        let store = Store::open_in_memory().unwrap();
        store
            .conn
            .execute(
                "INSERT INTO sessions (session_id, started_at, finished_at, summary) \
                 VALUES (?1, ?2, NULL, NULL)",
                params![Option::<String>::None, started],
            )
            .unwrap();
        assert_corrupt(&store, "session_id");

        let mut store = Store::open_in_memory().unwrap();
        store.open_session("bad-start", injected_now()).unwrap();
        store
            .conn
            .execute(
                "UPDATE sessions SET started_at = 'not-rfc3339' WHERE session_id = 'bad-start'",
                [],
            )
            .unwrap();
        assert_corrupt(&store, "started_at");

        let mut store = Store::open_in_memory().unwrap();
        store.open_session("bad-finish", injected_now()).unwrap();
        store
            .conn
            .execute(
                "UPDATE sessions SET finished_at = 'not-rfc3339' \
                 WHERE session_id = 'bad-finish'",
                [],
            )
            .unwrap();
        assert_corrupt(&store, "finished_at");

        let mut store = Store::open_in_memory().unwrap();
        store
            .append(
                &capsule("invalid label grounding capsule", "nmemory"),
                injected_now(),
            )
            .unwrap();
        store
            .record_recall_receipt(
                &["invalid label".to_string()],
                &["cap-1"],
                None,
                None,
                Some(" \t "),
                injected_now(),
            )
            .unwrap();
        assert_corrupt(&store, "session_id");

        let mut store = Store::open_in_memory().unwrap();
        store
            .append(&capsule("invalid capsule label", "nmemory"), injected_now())
            .unwrap();
        store
            .conn
            .execute("UPDATE capsules SET session_id = '' WHERE id = 'cap-1'", [])
            .unwrap();
        assert_corrupt(&store, "session_id");
    }

    #[test]
    fn session_activity_rejects_blob_storage_classes_at_every_text_seam() {
        fn assert_blob_corrupt(store: &Store, field: &str) {
            let error = store.session_activity().unwrap_err();
            assert!(
                matches!(
                    error,
                    StoreError::Corrupt { ref reason, .. }
                        if reason.contains(field) && reason.contains("BLOB")
                ),
                "persisted {field} BLOB must be typed corruption naming its storage class, got {error:?}"
            );
        }

        let started = rfc3339_text(injected_now()).unwrap();
        let store = Store::open_in_memory().unwrap();
        store
            .conn
            .execute(
                "INSERT INTO sessions (session_id, started_at, finished_at, summary) \
                 VALUES (x'FF', ?1, NULL, NULL)",
                [started],
            )
            .unwrap();
        assert_blob_corrupt(&store, "session_id");

        let mut store = Store::open_in_memory().unwrap();
        store
            .append(&capsule("blob capsule label", "nmemory"), injected_now())
            .unwrap();
        store
            .conn
            .execute(
                "UPDATE capsules SET session_id = x'FF' WHERE id = 'cap-1'",
                [],
            )
            .unwrap();
        assert_blob_corrupt(&store, "session_id");

        let mut store = Store::open_in_memory().unwrap();
        store
            .append(&capsule("blob receipt label", "nmemory"), injected_now())
            .unwrap();
        let receipt_id = store
            .record_recall_receipt(
                &["blob receipt label".to_string()],
                &["cap-1"],
                None,
                None,
                Some("valid-label"),
                injected_now(),
            )
            .unwrap();
        store
            .conn
            .execute(
                "UPDATE recall_receipts SET session_id = x'FF' WHERE id = ?1",
                [&receipt_id],
            )
            .unwrap();
        assert_blob_corrupt(&store, "session_id");

        let mut store = Store::open_in_memory().unwrap();
        store.open_session("blob-start", injected_now()).unwrap();
        store
            .conn
            .execute(
                "UPDATE sessions SET started_at = x'FF' WHERE session_id = 'blob-start'",
                [],
            )
            .unwrap();
        assert_blob_corrupt(&store, "started_at");

        let mut store = Store::open_in_memory().unwrap();
        store.open_session("blob-finish", injected_now()).unwrap();
        store
            .finish_session("blob-finish", None, later_now())
            .unwrap();
        store
            .conn
            .execute(
                "UPDATE sessions SET finished_at = x'FF' WHERE session_id = 'blob-finish'",
                [],
            )
            .unwrap();
        assert_blob_corrupt(&store, "finished_at");
    }

    #[test]
    fn session_activity_rejects_invalid_utf8_text_as_corrupt() {
        fn assert_utf8_corrupt(store: &Store, field: &str) {
            let error = store.session_activity().unwrap_err();
            assert!(
                matches!(
                    error,
                    StoreError::Corrupt { ref reason, .. }
                        if reason.contains(field) && reason.contains("UTF-8")
                ),
                "persisted invalid-UTF-8 TEXT {field} must be typed corruption, got {error:?}"
            );
        }

        let mut store = Store::open_in_memory().unwrap();
        store
            .append(
                &capsule("invalid utf8 text label", "nmemory"),
                injected_now(),
            )
            .unwrap();
        store
            .conn
            .execute(
                "UPDATE capsules SET session_id = CAST(x'FF' AS TEXT) WHERE id = 'cap-1'",
                [],
            )
            .unwrap();
        let storage_class: String = store
            .conn
            .query_row(
                "SELECT typeof(session_id) FROM capsules WHERE id = 'cap-1'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(storage_class, "text");
        assert_utf8_corrupt(&store, "session_id");

        let mut store = Store::open_in_memory().unwrap();
        store
            .open_session("invalid-utf8-start", injected_now())
            .unwrap();
        store
            .conn
            .execute(
                "UPDATE sessions SET started_at = CAST(x'FF' AS TEXT) \
                 WHERE session_id = 'invalid-utf8-start'",
                [],
            )
            .unwrap();
        assert_utf8_corrupt(&store, "started_at");
    }

    #[test]
    fn raw_session_activity_text_decoder_rejects_every_wrong_storage_class() {
        for (raw, class) in [
            (RawSqlValue::Null, "NULL"),
            (RawSqlValue::Integer(7), "INTEGER"),
            (RawSqlValue::Real(1.25), "REAL"),
            (RawSqlValue::Blob(vec![0xFF]), "BLOB"),
        ] {
            let error = raw
                .required_text("session_activity", "session_activity.session_id")
                .unwrap_err();
            assert!(
                matches!(error, StoreError::Corrupt { ref reason, .. } if reason.contains(class)),
                "required TEXT must reject {class}: {error:?}"
            );
        }
        for (raw, class) in [
            (RawSqlValue::Integer(7), "INTEGER"),
            (RawSqlValue::Real(1.25), "REAL"),
            (RawSqlValue::Blob(vec![0xFF]), "BLOB"),
        ] {
            let error = raw
                .optional_text("sess-1", "sessions.finished_at")
                .unwrap_err();
            assert!(
                matches!(error, StoreError::Corrupt { ref reason, .. } if reason.contains(class)),
                "optional NULL/TEXT must reject {class}: {error:?}"
            );
        }
        assert_eq!(
            RawSqlValue::Null
                .optional_text("sess-1", "sessions.finished_at")
                .unwrap(),
            None
        );
        assert_eq!(
            RawSqlValue::Text(b"sess-1".to_vec())
                .required_text("session_activity", "session_activity.session_id")
                .unwrap(),
            "sess-1"
        );
    }

    #[test]
    fn append_with_session_links_and_validates() {
        let mut store = Store::open_in_memory().unwrap();
        let t1 = injected_now();

        // Unknown session: rejected, nothing appended, no seq burned.
        let err = store
            .append_with_session(&capsule("orphan", "nmemory"), "s-none", t1)
            .unwrap_err();
        assert_eq!(err, StoreError::UnknownSession("s-none".to_string()));
        assert!(store.list(ListFilter::default()).unwrap().is_empty());

        store.open_session("s-1", t1).unwrap();
        let linked = store
            .append_with_session(&capsule("bracketed capture", "nmemory"), "s-1", t1)
            .unwrap();
        assert_eq!(linked.as_str(), "cap-1");
        assert_eq!(
            store.get("cap-1").unwrap().unwrap().session_id.as_deref(),
            Some("s-1")
        );

        // A plain append stays unlinked.
        store
            .append(&capsule("loose capture", "nmemory"), t1)
            .unwrap();
        assert_eq!(store.get("cap-2").unwrap().unwrap().session_id, None);

        // The linked line carries the session_id key AFTER created_at;
        // the unlinked line has no such key (byte-stability for pre-v2
        // shapes).
        let snap = store.canonical_snapshot().unwrap();
        let lines: Vec<&str> = snap.lines().collect();
        assert!(lines[0].ends_with("\"session_id\":\"s-1\"}"));
        assert!(!lines[1].contains("session_id"));
        // And the linked line round-trips.
        let parsed: StoredCapsule = serde_json::from_str(lines[0]).unwrap();
        assert_eq!(parsed.session_id.as_deref(), Some("s-1"));

        // A finished session accepts no further captures.
        store.finish_session("s-1", None, later_now()).unwrap();
        let err = store
            .append_with_session(&capsule("too late", "nmemory"), "s-1", later_now())
            .unwrap_err();
        assert_eq!(err, StoreError::SessionFinished("s-1".to_string()));
        assert_eq!(store.list(ListFilter::default()).unwrap().len(), 2);
    }

    // ------------------------------------------------------------------
    // snapshot × sidecars
    // ------------------------------------------------------------------

    #[test]
    fn sidecar_writes_never_move_the_canonical_snapshot() {
        let mut store = Store::open_in_memory().unwrap();
        store
            .append(&capsule("snapshot anchor one", "nmemory"), injected_now())
            .unwrap();
        store
            .append(&capsule("snapshot anchor two", "nmemory"), injected_now())
            .unwrap();
        let before = store.canonical_snapshot().unwrap();

        store.supersede("cap-1", "cap-2", later_now()).unwrap();
        store
            .upsert_relation(RelationKind::Blocks, "cap-1", "cap-2", later_now())
            .unwrap();
        store
            .append_audit("t", "memory.supersede", "cap-1", None, later_now())
            .unwrap();
        store
            .set_classification("cap-2", "fact", "project", later_now())
            .unwrap();
        store.open_session("s-x", later_now()).unwrap();
        store.record_recall(&["cap-2"], later_now()).unwrap();
        // w2 sidecars: tiers and synonyms are excluded by the same rule.
        store
            .set_tier("cap-1", Tier::Archived, later_now())
            .unwrap();
        store
            .add_alias("tokio", "async runtime", later_now())
            .unwrap();
        store
            .record_lane_override(LaneOverride::TermOverFused, later_now())
            .unwrap();
        assert_eq!(
            store.lane_override_totals().unwrap(),
            vec![("term".to_string(), "fused".to_string(), 1)]
        );

        assert_eq!(store.canonical_snapshot().unwrap(), before);
    }

    #[test]
    fn record_recall_increments_and_stamps_injected_now() {
        let mut store = Store::open_in_memory().unwrap();
        let id = store
            .append(&capsule("recalled fact", "nmemory"), injected_now())
            .unwrap();
        // Never recalled → no usage row.
        assert_eq!(store.usage_of(id.as_str()).unwrap(), None);

        let t1 = injected_now();
        store.record_recall(&[id.as_str()], t1).unwrap();
        let stat = store.usage_of(id.as_str()).unwrap().unwrap();
        assert_eq!(stat.recall_count, 1);
        // Exact equality with the 2001 boundary instant: the stamp is the
        // injected now, not a wall clock.
        assert_eq!(stat.last_recalled_at, t1);

        let t2 = later_now();
        store.record_recall(&[id.as_str()], t2).unwrap();
        let stat = store.usage_of(id.as_str()).unwrap().unwrap();
        assert_eq!(stat.recall_count, 2);
        assert_eq!(stat.last_recalled_at, t2);

        // Empty slice writes nothing.
        store.record_recall(&[], t2).unwrap();
        assert_eq!(
            store.usage_of(id.as_str()).unwrap().unwrap().recall_count,
            2
        );
    }

    #[test]
    fn usage_table_is_derived_and_droppable() {
        // ARCHITECTURE §2: deleting `usage` loses nothing — counters reset,
        // capsules and relations stay intact, open recreates the table.
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("memory.sqlite3");
        {
            let mut store = Store::open(&path).unwrap();
            store
                .append(&capsule("counted fact", "nmemory"), injected_now())
                .unwrap();
            store.record_recall(&["cap-1"], injected_now()).unwrap();
            assert!(store.usage_of("cap-1").unwrap().is_some());
        }
        {
            let raw = rusqlite::Connection::open(&path).unwrap();
            raw.execute_batch("DROP TABLE usage").unwrap();
        }
        let store = Store::open(&path).unwrap();
        assert_eq!(store.usage_of("cap-1").unwrap(), None);
        assert_eq!(
            store.get("cap-1").unwrap().unwrap().capsule.content(),
            "counted fact"
        );
    }

    #[test]
    fn fts5_compiled_into_bundled_sqlite() {
        // s4 (FTS5+bm25 recall) depends on FTS5 being compiled into the
        // bundled SQLite — prove it, don't assume it.
        let conn = rusqlite::Connection::open_in_memory().unwrap();
        conn.execute_batch("CREATE VIRTUAL TABLE t USING fts5(content);")
            .unwrap();
        conn.execute(
            "INSERT INTO t (content) VALUES (?1)",
            ["grounded recall abstains when nothing matches"],
        )
        .unwrap();
        let n: i64 = conn
            .query_row("SELECT count(*) FROM t WHERE t MATCH 'recall'", [], |row| {
                row.get(0)
            })
            .unwrap();
        assert_eq!(n, 1);
    }

    #[test]
    fn append_syncs_fts_and_search_finds_scored_capsules() {
        let mut store = Store::open_in_memory().unwrap();
        let planted = capsule("the recall engine speaks sqlite", "nmemory");
        store.append(&planted, injected_now()).unwrap();
        store
            .append(
                &capsule("unrelated spool organ note", "nmemory"),
                injected_now(),
            )
            .unwrap();

        let hits = store.search_fts(&["sqlite".to_string()], None).unwrap();
        assert_eq!(hits.len(), 1);
        let (stored, score) = &hits[0];
        assert_eq!(stored.id.as_str(), "cap-1");
        assert_eq!(stored.capsule, planted);
        assert!(
            *score < 0.0,
            "SQLite bm25 match scores are negative, got {score}"
        );
    }

    #[test]
    fn search_fts_or_across_terms_and_project_fence() {
        let mut store = Store::open_in_memory().unwrap();
        store
            .append(&capsule("alpha shared term", "proj-a"), injected_now())
            .unwrap();
        store
            .append(&capsule("beta shared term", "proj-b"), injected_now())
            .unwrap();

        // OR: one term matching suffices.
        let hits = store
            .search_fts(&["alpha".to_string(), "zzz-absent".to_string()], None)
            .unwrap();
        assert_eq!(hits.len(), 1);
        assert_eq!(hits[0].0.id.as_str(), "cap-1");

        // Project fence.
        let fenced = store
            .search_fts(&["shared".to_string()], Some("proj-b"))
            .unwrap();
        assert_eq!(fenced.len(), 1);
        assert_eq!(fenced[0].0.capsule.scope().project_id, "proj-b");
        let open = store.search_fts(&["shared".to_string()], None).unwrap();
        assert_eq!(open.len(), 2);
    }

    #[test]
    fn search_fts_limited_caps_candidates_to_top_k_by_rank() {
        // perf-ingest: the write-time hint scans bound their candidate set
        // with this cap, so a common token no longer returns the whole
        // store. Byte-distinct rows sharing one token: the unbounded search
        // returns all, the limited search returns exactly the top-K prefix.
        let mut store = Store::open_in_memory().unwrap();
        let corpus = 40;
        for i in 0..corpus {
            store
                .append(
                    &capsule(&format!("shared marker row number {i}"), "nmemory"),
                    injected_now(),
                )
                .unwrap();
        }
        let all = store.search_fts(&["shared".to_string()], None).unwrap();
        assert_eq!(
            all.len(),
            corpus,
            "every sharer matches the unbounded search"
        );

        // The cap keeps the strongest-ranked prefix — same ORDER BY, only a
        // LIMIT — so it is exactly the head of the unbounded result.
        let capped = store
            .search_fts_limited(&["shared".to_string()], None, 10)
            .unwrap();
        assert_eq!(capped.len(), 10, "the scan is bounded to the cap");
        let head: Vec<&str> = all.iter().take(10).map(|(s, _)| s.id.as_str()).collect();
        let capped_ids: Vec<&str> = capped.iter().map(|(s, _)| s.id.as_str()).collect();
        assert_eq!(capped_ids, head, "the cap keeps the top-K by bm25 rank");

        // A cap at/above the corpus truncates nothing; a zero cap returns
        // nothing (the LIMIT is honored exactly).
        let loose = store
            .search_fts_limited(&["shared".to_string()], None, corpus + 100)
            .unwrap();
        assert_eq!(loose.len(), corpus);
        let none = store
            .search_fts_limited(&["shared".to_string()], None, 0)
            .unwrap();
        assert!(none.is_empty());
    }

    #[test]
    fn batched_hint_fences_match_per_candidate_probes() {
        // perf-ingest: the ingest hint scans replace N per-candidate
        // is_superseded/is_falsified/get_tier round-trips with ONE batched
        // query. Prove the batched sets equal the per-candidate probes over
        // a candidate id set with every lifecycle state represented.
        let mut store = Store::open_in_memory().unwrap();
        for i in 0..5 {
            store
                .append(
                    &capsule(
                        &format!("fence family shared record variant {i}"),
                        "nmemory",
                    ),
                    injected_now(),
                )
                .unwrap();
        }
        // cap-1 superseded (by cap-5), cap-2 falsified (by cap-5),
        // cap-3 quarantined, cap-4 archived; cap-5 is the one live row.
        store.supersede("cap-1", "cap-5", injected_now()).unwrap();
        store
            .upsert_relation(RelationKind::Falsifies, "cap-5", "cap-2", injected_now())
            .unwrap();
        store
            .set_tier("cap-3", Tier::Quarantined, injected_now())
            .unwrap();
        store
            .set_tier("cap-4", Tier::Archived, injected_now())
            .unwrap();
        let ids = ["cap-1", "cap-2", "cap-3", "cap-4", "cap-5"];

        // Batched superseded set == the per-candidate is_superseded probe.
        let superseded = store.superseded_among(&ids).unwrap();
        assert_eq!(superseded, BTreeSet::from(["cap-1".to_string()]));
        for id in ids {
            assert_eq!(
                superseded.contains(id),
                store.is_superseded(id).unwrap(),
                "superseded_among must agree with is_superseded for {id}"
            );
        }

        // Batched sibling-exclusion == the tier/falsify/supersede trio.
        let excluded = store.sibling_excluded_among(&ids).unwrap();
        assert_eq!(
            excluded,
            BTreeSet::from(["cap-1", "cap-2", "cap-3", "cap-4"].map(String::from)),
            "superseded, falsified, quarantined, and archived are all dropped"
        );
        for id in ids {
            let dead = store.is_superseded(id).unwrap()
                || store.is_falsified(id).unwrap()
                || store.get_tier(id).unwrap() != Tier::Active;
            assert_eq!(
                excluded.contains(id),
                dead,
                "sibling_excluded_among must agree with the point-query trio for {id}"
            );
        }
        assert!(
            !excluded.contains("cap-5"),
            "the one live candidate survives"
        );

        // Empty candidate set short-circuits (no invalid `IN ()`).
        assert!(store.superseded_among(&[]).unwrap().is_empty());
        assert!(store.sibling_excluded_among(&[]).unwrap().is_empty());
    }

    #[test]
    fn search_fts_quotes_terms_no_syntax_injection() {
        let mut store = Store::open_in_memory().unwrap();
        store
            .append(&capsule("beta gamma delta", "nmemory"), injected_now())
            .unwrap();

        // If quoting leaked, this term would parse as "beta" OR "delta"
        // and match; quoted it is the phrase "beta or delta" — absent.
        let injected = store
            .search_fts(&[r#"beta" OR "delta"#.to_string()], None)
            .unwrap();
        assert!(injected.is_empty(), "FTS5 OR injection must not match");

        // Operators/specials as literal text: never a syntax error.
        for weird in [
            "NEAR(beta",
            "beta AND gamma",
            "-beta",
            "beta*",
            "content:beta",
        ] {
            let result = store.search_fts(&[weird.to_string()], None);
            assert!(
                result.is_ok(),
                "term {weird:?} must not raise FTS5 syntax: {result:?}"
            );
        }
        // NUL (JSON-legal `\u{0000}`, reachable from the MCP surface) is a
        // separator, never "unterminated string" (w3 review): the term
        // "beta\0gamma" is the phrase [beta, gamma] — present in content.
        assert_eq!(
            store
                .search_fts(&["beta\0gamma".to_string()], None)
                .unwrap()
                .len(),
            1
        );
        // A pure-NUL term cannot tokenize: skipped, never an error.
        assert!(
            store
                .search_fts(&["\0".to_string()], None)
                .unwrap()
                .is_empty()
        );
        // Column-filter shape is a phrase [content, beta] — absent.
        assert!(
            store
                .search_fts(&["content:beta".to_string()], None)
                .unwrap()
                .is_empty()
        );
        // A plain term still matches.
        assert_eq!(
            store
                .search_fts(&["gamma".to_string()], None)
                .unwrap()
                .len(),
            1
        );
    }

    #[test]
    fn search_fts_skips_unsearchable_terms() {
        let mut store = Store::open_in_memory().unwrap();
        store
            .append(&capsule("epsilon zeta", "nmemory"), injected_now())
            .unwrap();

        assert!(store.search_fts(&[], None).unwrap().is_empty());
        assert!(
            store
                .search_fts(&["***".to_string(), "  ".to_string()], None)
                .unwrap()
                .is_empty()
        );
        // The unsearchable term is dropped, the searchable one still runs.
        assert_eq!(
            store
                .search_fts(&["***".to_string(), "zeta".to_string()], None)
                .unwrap()
                .len(),
            1
        );
    }

    #[test]
    fn rebuild_fts_after_external_drop_restores_identical_results() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("memory.sqlite3");
        let mut store = Store::open(&path).unwrap();
        for text in ["derived alpha", "derived beta", "other gamma"] {
            store
                .append(&capsule(text, "nmemory"), injected_now())
                .unwrap();
        }
        let terms = ["derived".to_string()];
        let before = store.search_fts(&terms, None).unwrap();
        assert_eq!(before.len(), 2);

        let raw = rusqlite::Connection::open(&path).unwrap();
        raw.execute_batch("DROP TABLE capsules_fts").unwrap();
        drop(raw);

        assert_eq!(store.rebuild_fts().unwrap(), 3);
        let after = store.search_fts(&terms, None).unwrap();
        assert_eq!(before, after, "derived table: drop→rebuild loses nothing");
    }

    #[test]
    fn open_heals_missing_fts_mirror() {
        // Simulates a pre-fts file (or a vandalized derived table): the
        // canonical table has rows, the mirror is gone. Open re-derives.
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("memory.sqlite3");
        {
            let mut store = Store::open(&path).unwrap();
            store
                .append(&capsule("healed recall target", "nmemory"), injected_now())
                .unwrap();
        }
        {
            let raw = rusqlite::Connection::open(&path).unwrap();
            raw.execute_batch("DROP TABLE capsules_fts").unwrap();
        }
        let store = Store::open(&path).unwrap();
        let hits = store.search_fts(&["healed".to_string()], None).unwrap();
        assert_eq!(hits.len(), 1);
        assert_eq!(hits[0].0.id.as_str(), "cap-1");
    }

    // ------------------------------------------------------------------
    // w2: lifecycle tiers
    // ------------------------------------------------------------------

    #[test]
    fn tier_wire_names_are_the_closed_snake_case_set() {
        assert_eq!(Tier::Active.as_str(), "active");
        assert_eq!(Tier::Archived.as_str(), "archived");
        assert_eq!(Tier::Quarantined.as_str(), "quarantined");
        for tier in Tier::ALL {
            assert_eq!(Tier::from_wire(tier.as_str()), Some(tier));
        }
        assert_eq!(Tier::from_wire("hot"), None);
        assert_eq!(Tier::from_wire("Active"), None);
    }

    #[test]
    fn tier_default_active_set_get_list_and_unknown_id() {
        let mut store = Store::open_in_memory().unwrap();
        store
            .append(&capsule("tiered alpha", "nmemory"), injected_now())
            .unwrap();
        store
            .append(&capsule("tiered beta", "nmemory"), injected_now())
            .unwrap();

        // Default rule: never-tiered stored capsules are Active, no row.
        assert_eq!(store.get_tier("cap-1").unwrap(), Tier::Active);
        assert_eq!(
            store.list_by_tier(Tier::Active).unwrap(),
            vec!["cap-1".to_string(), "cap-2".to_string()]
        );
        assert!(store.list_by_tier(Tier::Archived).unwrap().is_empty());

        // Set → get roundtrip; the listing re-buckets.
        store
            .set_tier("cap-1", Tier::Archived, injected_now())
            .unwrap();
        assert_eq!(store.get_tier("cap-1").unwrap(), Tier::Archived);
        assert_eq!(
            store.list_by_tier(Tier::Active).unwrap(),
            vec!["cap-2".to_string()]
        );
        assert_eq!(
            store.list_by_tier(Tier::Archived).unwrap(),
            vec!["cap-1".to_string()]
        );

        // Upsert: re-tiering replaces.
        store
            .set_tier("cap-1", Tier::Quarantined, later_now())
            .unwrap();
        assert_eq!(store.get_tier("cap-1").unwrap(), Tier::Quarantined);
        assert!(store.list_by_tier(Tier::Archived).unwrap().is_empty());
        assert_eq!(
            store.list_by_tier(Tier::Quarantined).unwrap(),
            vec!["cap-1".to_string()]
        );
        // Explicit Active is expressible too (indistinguishable from the
        // default through get_tier, by design).
        store.set_tier("cap-1", Tier::Active, later_now()).unwrap();
        assert_eq!(store.get_tier("cap-1").unwrap(), Tier::Active);
        assert_eq!(
            store.list_by_tier(Tier::Active).unwrap(),
            vec!["cap-1".to_string(), "cap-2".to_string()]
        );

        // Unknown ids: typed rejection on BOTH verbs, nothing written.
        assert_eq!(
            store
                .set_tier("cap-999", Tier::Archived, injected_now())
                .unwrap_err(),
            StoreError::UnknownCapsule("cap-999".to_string())
        );
        assert_eq!(
            store.get_tier("cap-999").unwrap_err(),
            StoreError::UnknownCapsule("cap-999".to_string())
        );
    }

    #[test]
    fn tier_tombstone_split_and_reopen_persistence() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("memory.sqlite3");
        {
            let mut store = Store::open(&path).unwrap();
            store
                .append(
                    &capsule("quarantine then forget", "nmemory"),
                    injected_now(),
                )
                .unwrap();
            store
                .append(&capsule("stays active", "nmemory"), injected_now())
                .unwrap();
            store
                .set_tier("cap-1", Tier::Quarantined, injected_now())
                .unwrap();
            store
                .forget_capsule("cap-1", TombstoneMode::Purged, "drill", b"k", later_now())
                .unwrap();
        }
        let store = Store::open(&path).unwrap();
        // Record-level state persists and still answers on the tombstoned
        // row (like classifications)...
        assert_eq!(store.get_tier("cap-1").unwrap(), Tier::Quarantined);
        // ...but no tier LISTING surfaces a destroyed capsule (the q36/M4
        // lesson: dead nodes never advertise as work).
        assert!(store.list_by_tier(Tier::Quarantined).unwrap().is_empty());
        assert_eq!(
            store.list_by_tier(Tier::Active).unwrap(),
            vec!["cap-2".to_string()]
        );
    }

    // ------------------------------------------------------------------
    // w2: caller-fed synonyms
    // ------------------------------------------------------------------

    #[test]
    fn synonyms_fold_on_write_and_lookup_and_stay_deterministic() {
        let mut store = Store::open_in_memory().unwrap();

        // First add records; the idempotent re-add — even spelled with
        // different case/accents — answers false (no-op honesty).
        assert!(
            store
                .add_alias("Configuração", "config", injected_now())
                .unwrap()
        );
        assert!(
            !store
                .add_alias("configuracao", "CONFIG", later_now())
                .unwrap()
        );
        assert!(
            store
                .add_alias("configuracao", "cfg", injected_now())
                .unwrap()
        );
        assert!(store.add_alias("deploy", "ship", injected_now()).unwrap());

        // Lookup folds exactly like the write side.
        assert_eq!(
            store.aliases_for("CONFIGURAÇÃO").unwrap(),
            vec!["cfg".to_string(), "config".to_string()]
        );
        assert_eq!(
            store.aliases_for("configuracao").unwrap(),
            vec!["cfg".to_string(), "config".to_string()]
        );
        // Direction is as-taught: the alias side does not answer.
        assert!(store.aliases_for("config").unwrap().is_empty());
        assert!(store.aliases_for("unknown-term").unwrap().is_empty());

        // Full view, deterministic (term, alias) order, folded storage,
        // each row carrying its first-record instant (w2-fix: the
        // "first at kept" no-op is now verifiable on this surface).
        let rows = store.list_aliases().unwrap();
        assert_eq!(
            rows.iter()
                .map(|(t, a, _)| (t.as_str(), a.as_str()))
                .collect::<Vec<_>>(),
            vec![
                ("configuracao", "cfg"),
                ("configuracao", "config"),
                ("deploy", "ship"),
            ]
        );
        assert!(
            rows.iter().all(|(_, _, at)| *at == injected_now()),
            "every alias row carries its recorded at"
        );

        // Typed negatives; nothing written by any of them.
        assert_eq!(
            store.add_alias("  ", "x", injected_now()).unwrap_err(),
            StoreError::EmptyField("term")
        );
        assert_eq!(
            store.add_alias("x", "\t ", injected_now()).unwrap_err(),
            StoreError::EmptyField("alias")
        );
        assert_eq!(
            store
                .add_alias("Tokio", "tokio", injected_now())
                .unwrap_err(),
            StoreError::SelfAlias {
                term: "tokio".to_string(),
            }
        );
        assert_eq!(store.list_aliases().unwrap().len(), 3);
    }

    #[test]
    fn synonyms_table_is_derived_and_droppable() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("memory.sqlite3");
        {
            let mut store = Store::open(&path).unwrap();
            store
                .append(&capsule("synonym neighbor", "nmemory"), injected_now())
                .unwrap();
            store
                .add_alias("pr", "pull request", injected_now())
                .unwrap();
            assert_eq!(store.aliases_for("pr").unwrap().len(), 1);
        }
        {
            let raw = rusqlite::Connection::open(&path).unwrap();
            raw.execute_batch("DROP TABLE synonyms").unwrap();
        }
        // Open recreates the table empty; no canonical byte was lost.
        let store = Store::open(&path).unwrap();
        assert!(store.aliases_for("pr").unwrap().is_empty());
        assert!(store.list_aliases().unwrap().is_empty());
        assert_eq!(
            store.get("cap-1").unwrap().unwrap().capsule.content(),
            "synonym neighbor"
        );
    }

    // ------------------------------------------------------------------
    // w2: audit journal hash chain
    // ------------------------------------------------------------------

    #[test]
    fn journal_chain_links_head_and_golden_recomputation() {
        let mut store = Store::open_in_memory().unwrap();
        // Empty ledger: no head, zero verified rows.
        assert_eq!(store.journal_head().unwrap(), None);
        assert_eq!(store.verify_chain().unwrap(), 0);

        let at1 = rfc3339_text(injected_now()).unwrap();
        let at2 = rfc3339_text(later_now()).unwrap();
        store
            .append_audit("session:w2", "memory.ingest", "cap-1", None, injected_now())
            .unwrap();
        store
            .append_audit(
                "session:w2",
                "memory.forget",
                "cap-1",
                Some("owner asked"),
                later_now(),
            )
            .unwrap();

        // Golden line shape: fixed field order, explicit null reason.
        let line1 =
            audit_canonical_line(1, &at1, "session:w2", "memory.ingest", "cap-1", None).unwrap();
        assert_eq!(
            line1,
            format!(
                "{{\"seq\":1,\"at\":{at},\"actor\":\"session:w2\",\
                 \"action\":\"memory.ingest\",\"subject\":\"cap-1\",\"reason\":null}}",
                at = serde_json::to_string(&at1).unwrap()
            )
        );
        // Golden chain: h1 = sha256("" + line1), h2 = sha256(h1 + line2).
        let h1 = chained_hash_of("", &line1);
        let line2 = audit_canonical_line(
            2,
            &at2,
            "session:w2",
            "memory.forget",
            "cap-1",
            Some("owner asked"),
        )
        .unwrap();
        let h2 = chained_hash_of(&h1, &line2);
        assert_eq!(h1, sha256_hex(line1.as_bytes()));
        assert_ne!(h1, h2);

        // The stored rows carry exactly these links; the head is the last.
        let events = store.list_audit(None, None).unwrap();
        assert_eq!(events[1].chained_hash, h1);
        assert_eq!(events[0].chained_hash, h2);
        assert_eq!(store.journal_head().unwrap(), Some(h2));
        assert_eq!(store.verify_chain().unwrap(), 2);

        // Replay determinism: the same audit sequence in a fresh store
        // yields the identical head.
        let mut replay = Store::open_in_memory().unwrap();
        replay
            .append_audit("session:w2", "memory.ingest", "cap-1", None, injected_now())
            .unwrap();
        replay
            .append_audit(
                "session:w2",
                "memory.forget",
                "cap-1",
                Some("owner asked"),
                later_now(),
            )
            .unwrap();
        assert_eq!(
            replay.journal_head().unwrap(),
            store.journal_head().unwrap()
        );
    }

    /// Seed a file-backed store with three chained audit rows and return
    /// its path (the tamper-drill fixture).
    fn seed_audited_store(dir: &tempfile::TempDir) -> std::path::PathBuf {
        let path = dir.path().join("memory.sqlite3");
        let mut store = Store::open(&path).unwrap();
        store
            .append_audit("session:w2", "memory.ingest", "cap-1", None, injected_now())
            .unwrap();
        store
            .append_audit("session:w2", "memory.relate", "cap-1", None, injected_now())
            .unwrap();
        store
            .append_audit(
                "session:w2",
                "memory.forget",
                "cap-1",
                Some("drill"),
                later_now(),
            )
            .unwrap();
        assert_eq!(store.verify_chain().unwrap(), 3);
        path
    }

    #[test]
    fn journal_chain_names_first_broken_seq_on_row_tamper() {
        let dir = tempfile::tempdir().unwrap();
        let path = seed_audited_store(&dir);
        // Flip one byte of a mid-ledger row (actor 'session:w2' →
        // 'sessiom:w2') behind the store's back.
        {
            let raw = rusqlite::Connection::open(&path).unwrap();
            raw.execute(
                "UPDATE audit_events SET actor = 'sessiom:w2' WHERE seq = 2",
                [],
            )
            .unwrap();
        }
        let store = Store::open(&path).unwrap();
        assert_eq!(
            store.verify_chain().unwrap_err(),
            StoreError::JournalBroken { seq: 2 }
        );
        // The ledger still reads (tamper detection is verify's job, not a
        // read gate) and the head still answers — the chain is the judge.
        assert_eq!(store.list_audit(None, None).unwrap().len(), 3);
        assert!(store.journal_head().unwrap().is_some());
    }

    #[test]
    fn journal_chain_names_first_broken_seq_on_hash_forgery_and_row_removal() {
        // Forged hash: the LINK itself is rewritten.
        let dir = tempfile::tempdir().unwrap();
        let path = seed_audited_store(&dir);
        {
            let raw = rusqlite::Connection::open(&path).unwrap();
            raw.execute(
                "UPDATE audit_events SET chained_hash = lower(hex(randomblob(32))) \
                 WHERE seq = 3",
                [],
            )
            .unwrap();
        }
        let store = Store::open(&path).unwrap();
        assert_eq!(
            store.verify_chain().unwrap_err(),
            StoreError::JournalBroken { seq: 3 }
        );
        drop(store);

        // Mid-ledger removal: the row AFTER the hole fails (its prev link
        // no longer exists), so the hole is named at the first surviving
        // successor.
        let dir2 = tempfile::tempdir().unwrap();
        let path2 = seed_audited_store(&dir2);
        {
            let raw = rusqlite::Connection::open(&path2).unwrap();
            raw.execute("DELETE FROM audit_events WHERE seq = 2", [])
                .unwrap();
        }
        let store2 = Store::open(&path2).unwrap();
        assert_eq!(
            store2.verify_chain().unwrap_err(),
            StoreError::JournalBroken { seq: 3 }
        );
    }

    // ------------------------------------------------------------------
    // w2: v2 → v3 migration (chain backfill)
    // ------------------------------------------------------------------

    /// The v2 on-disk schema, verbatim from the w1 store: `session_id`-
    /// bearing capsules, generalized relations, chainless `audit_events`,
    /// the w1 sidecars, fts + usage, stamped 2.
    const V2_SCHEMA: &str = "
CREATE TABLE capsules (
    seq             INTEGER PRIMARY KEY,
    id              TEXT NOT NULL UNIQUE,
    canonical_json  TEXT,
    created_at      TEXT NOT NULL,
    source_hash     TEXT NOT NULL,
    project_id      TEXT NOT NULL,
    authority_class TEXT NOT NULL,
    valid_from      TEXT NOT NULL,
    session_id      TEXT
);
CREATE TABLE relations (
    kind    TEXT NOT NULL CHECK (kind IN ('supersedes', 'derived_from', 'witnesses', 'blocks')),
    from_id TEXT NOT NULL,
    to_id   TEXT NOT NULL,
    at      TEXT NOT NULL,
    PRIMARY KEY (kind, from_id, to_id)
);
CREATE UNIQUE INDEX idx_capsules_source_hash ON capsules (source_hash);
CREATE INDEX idx_capsules_project_id ON capsules (project_id);
CREATE INDEX idx_capsules_authority_class ON capsules (authority_class);
CREATE INDEX idx_capsules_valid_from ON capsules (valid_from);
CREATE INDEX idx_capsules_session_id ON capsules (session_id);
CREATE INDEX idx_relations_from ON relations (from_id);
CREATE INDEX idx_relations_to ON relations (to_id);
CREATE TABLE audit_events (
    seq     INTEGER PRIMARY KEY,
    at      TEXT NOT NULL,
    actor   TEXT NOT NULL,
    action  TEXT NOT NULL,
    subject TEXT NOT NULL,
    reason  TEXT
);
CREATE INDEX idx_audit_events_subject ON audit_events (subject);
CREATE TABLE classifications (
    capsule_id TEXT PRIMARY KEY,
    kind       TEXT NOT NULL CHECK (kind IN ('fact', 'procedure', 'decision')),
    scope      TEXT NOT NULL CHECK (scope IN ('project', 'global', 'session')),
    at         TEXT NOT NULL
);
CREATE TABLE tombstones (
    capsule_id        TEXT PRIMARY KEY,
    mode              TEXT NOT NULL CHECK (mode IN ('purged', 'redacted')),
    content_hmac      TEXT NOT NULL,
    at                TEXT NOT NULL,
    reason            TEXT NOT NULL,
    provenance_source TEXT,
    provenance_anchor TEXT
);
CREATE TABLE sessions (
    session_id  TEXT PRIMARY KEY,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    summary     TEXT
);
CREATE VIRTUAL TABLE capsules_fts USING fts5(content, tokenize = 'unicode61');
CREATE TABLE usage (
    capsule_id       TEXT PRIMARY KEY,
    recall_count     INTEGER NOT NULL,
    last_recalled_at TEXT NOT NULL
);
PRAGMA user_version = 2;
";

    /// Build a faithful v2 file: two capsules, THREE chainless audit rows
    /// (one with a reason, one with an embedded quote to exercise JSON
    /// escaping in the backfill), one relation, one usage row.
    fn seed_v2_file(path: &std::path::Path) -> (Capsule, Capsule) {
        let c1 = capsule("v2 audited claim", "nmemory");
        let c2 = capsule("v2 second claim", "nmemory");
        let conn = rusqlite::Connection::open(path).unwrap();
        conn.execute_batch(V2_SCHEMA).unwrap();
        for (seq, c) in [(1_i64, &c1), (2_i64, &c2)] {
            conn.execute(
                "INSERT INTO capsules \
                 (seq, id, canonical_json, created_at, source_hash, project_id, \
                  authority_class, valid_from, session_id) \
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, NULL)",
                params![
                    seq,
                    format!("cap-{seq}"),
                    c.to_canonical_json().unwrap(),
                    rfc3339_text(injected_now()).unwrap(),
                    c.provenance().source_hash,
                    c.scope().project_id,
                    authority_class_text(c.authority_class()).unwrap(),
                    rfc3339_text(c.freshness().valid_from).unwrap(),
                ],
            )
            .unwrap();
            conn.execute(
                "INSERT INTO capsules_fts (rowid, content) VALUES (?1, ?2)",
                params![seq, c.content()],
            )
            .unwrap();
        }
        for (seq, action, reason) in [
            (1_i64, "memory.ingest", None::<&str>),
            (2, "memory.relate", Some("caller \"quoted\" why")),
            (3, "memory.classify", None),
        ] {
            conn.execute(
                "INSERT INTO audit_events (seq, at, actor, action, subject, reason) \
                 VALUES (?1, ?2, 'session:w1', ?3, 'cap-1', ?4)",
                params![seq, rfc3339_text(injected_now()).unwrap(), action, reason],
            )
            .unwrap();
        }
        conn.execute(
            "INSERT INTO relations (kind, from_id, to_id, at) \
             VALUES ('supersedes', 'cap-2', 'cap-1', ?1)",
            params![rfc3339_text(later_now()).unwrap()],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO usage (capsule_id, recall_count, last_recalled_at) \
             VALUES ('cap-2', 5, ?1)",
            params![rfc3339_text(later_now()).unwrap()],
        )
        .unwrap();
        drop(conn);
        (c1, c2)
    }

    #[test]
    fn v2_file_migrates_to_v3_with_deterministic_chain_backfill() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("memory.sqlite3");
        let (c1, c2) = seed_v2_file(&path);

        // Opening IS the migration.
        let mut store = Store::open(&path).unwrap();
        {
            let conn = rusqlite::Connection::open(&path).unwrap();
            let version: i64 = conn
                .query_row("PRAGMA user_version", [], |row| row.get(0))
                .unwrap();
            assert_eq!(version, SCHEMA_VERSION);
        }

        // THE w2 gate: verify_chain is green on the migrated v2 file.
        assert_eq!(store.verify_chain().unwrap(), 3);
        let migrated_head = store.journal_head().unwrap().unwrap();

        // Backfill ≡ live appends: a fresh store fed the SAME audit
        // sequence lands on the identical head.
        let mut fresh = Store::open_in_memory().unwrap();
        fresh
            .append_audit("session:w1", "memory.ingest", "cap-1", None, injected_now())
            .unwrap();
        fresh
            .append_audit(
                "session:w1",
                "memory.relate",
                "cap-1",
                Some("caller \"quoted\" why"),
                injected_now(),
            )
            .unwrap();
        fresh
            .append_audit(
                "session:w1",
                "memory.classify",
                "cap-1",
                None,
                injected_now(),
            )
            .unwrap();
        assert_eq!(fresh.journal_head().unwrap().unwrap(), migrated_head);

        // Determinism across migrations: an identical v2 file migrates to
        // the identical head.
        let dir_b = tempfile::tempdir().unwrap();
        let path_b = dir_b.path().join("memory.sqlite3");
        seed_v2_file(&path_b);
        let store_b = Store::open(&path_b).unwrap();
        assert_eq!(store_b.journal_head().unwrap().unwrap(), migrated_head);

        // Chain continues live after migration.
        store
            .append_audit("session:w2", "memory.ingest", "cap-2", None, later_now())
            .unwrap();
        assert_eq!(store.verify_chain().unwrap(), 4);
        assert_ne!(store.journal_head().unwrap().unwrap(), migrated_head);

        // Nothing else moved: capsules byte-identical through the funnel,
        // relation + usage sidecars intact, recall works, and the
        // canonical snapshot equals a fresh replay (migration moved no
        // canonical byte).
        assert_eq!(store.get("cap-1").unwrap().unwrap().capsule, c1);
        assert_eq!(store.get("cap-2").unwrap().unwrap().capsule, c2);
        assert!(store.is_superseded("cap-1").unwrap());
        assert_eq!(store.usage_of("cap-2").unwrap().unwrap().recall_count, 5);
        assert_eq!(
            store
                .search_fts(&["audited".to_string()], None)
                .unwrap()
                .len(),
            1
        );
        let mut replay = Store::open_in_memory().unwrap();
        replay.append(&c1, injected_now()).unwrap();
        replay.append(&c2, injected_now()).unwrap();
        assert_eq!(
            store.canonical_snapshot().unwrap(),
            replay.canonical_snapshot().unwrap()
        );

        // Shape law: migrated audit_events == fresh v3 audit_events.
        let fresh_dir = tempfile::tempdir().unwrap();
        let fresh_path = fresh_dir.path().join("fresh.sqlite3");
        Store::open(&fresh_path).unwrap();
        assert_eq!(
            table_shape(&path, "audit_events"),
            table_shape(&fresh_path, "audit_events"),
            "migrated audit_events shape must equal fresh v3 shape"
        );

        // Reopen is a plain v3 open; the chain stays green.
        drop(store);
        let reopened = Store::open(&path).unwrap();
        assert_eq!(reopened.verify_chain().unwrap(), 4);
    }

    /// v4 (w2-kinds), re-proven at u-r11: a pre-v4 file with the 3-kind
    /// classifications CHECK is rebuilt to the CURRENT kind set on open —
    /// existing labels survive, work-plane kinds persist, unknown kinds
    /// stay rejected at both the Rust guard and the SQL CHECK.
    #[test]
    fn v3_file_with_old_classifications_check_migrates_to_v4() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("memory.sqlite3");
        {
            let mut store = Store::open(&path).unwrap();
            store
                .append(&capsule("kind migration target", "nott"), injected_now())
                .unwrap();
            store
                .set_classification("cap-1", "fact", "project", injected_now())
                .unwrap();
        }
        // Downgrade the table to the pre-v4 shape (3-kind CHECK) and
        // stamp the file v3 — a faithful w2-store2-era file.
        {
            let conn = rusqlite::Connection::open(&path).unwrap();
            conn.execute_batch(
                "CREATE TABLE classifications_v3 (
                     capsule_id TEXT PRIMARY KEY,
                     kind       TEXT NOT NULL CHECK (kind IN ('fact', 'procedure', 'decision')),
                     scope      TEXT NOT NULL CHECK (scope IN ('project', 'global', 'session')),
                     at         TEXT NOT NULL
                 );
                 INSERT INTO classifications_v3 SELECT * FROM classifications;
                 DROP TABLE classifications;
                 ALTER TABLE classifications_v3 RENAME TO classifications;
                 PRAGMA user_version = 3;",
            )
            .unwrap();
        }

        // Opening IS the migration.
        let mut store = Store::open(&path).unwrap();
        {
            let conn = rusqlite::Connection::open(&path).unwrap();
            let version: i64 = conn
                .query_row("PRAGMA user_version", [], |row| row.get(0))
                .unwrap();
            assert_eq!(version, SCHEMA_VERSION);
        }
        // The pre-migration label survived the rebuild.
        let kept = store.get_classification("cap-1").unwrap().unwrap();
        assert_eq!(
            (kept.kind.as_str(), kept.scope.as_str()),
            ("fact", "project")
        );
        // The work plane persists on the migrated file (the CHECK moved).
        store
            .set_classification("cap-1", "task", "project", injected_now())
            .unwrap();
        assert_eq!(
            store.get_classification("cap-1").unwrap().unwrap().kind,
            "task"
        );
        // Fail-closed unchanged: unknown kind → typed error, and the
        // rebuilt CHECK itself still fences raw writes.
        assert!(matches!(
            store.set_classification("cap-1", "causes", "project", injected_now()),
            Err(StoreError::InvalidClassification { .. })
        ));
        {
            let conn = rusqlite::Connection::open(&path).unwrap();
            let raw = conn.execute(
                "INSERT OR REPLACE INTO classifications (capsule_id, kind, scope, at) \
                 VALUES ('cap-1', 'causes', 'project', '2026-07-18T00:00:00Z')",
                [],
            );
            assert!(raw.is_err(), "the SQL CHECK must reject unknown kinds");
        }
        // Migrated shape == fresh v4 shape (no-drift law).
        let fresh_dir = tempfile::tempdir().unwrap();
        let fresh_path = fresh_dir.path().join("fresh.sqlite3");
        Store::open(&fresh_path).unwrap();
        assert_eq!(
            table_shape(&path, "classifications"),
            table_shape(&fresh_path, "classifications"),
            "migrated classifications shape must equal fresh v4 shape"
        );
    }

    /// u6h/u6i MIGRATION v4→current: a faithful v4 file — relations under
    /// the FOUR-kind CHECK (no `falsifies`), NO outcomes/preferences
    /// sidecar tables, `user_version = 4` — migrates IN PLACE on open. The
    /// legacy edge survives the CHECK rebuild, the relations CHECK gains
    /// `falsifies`, the two sidecar tables are (re)created, and the stamp
    /// advances to the current version. Mirrors the v3→v4 CHECK-rebuild
    /// test (no-drift discipline); this is the in-crate proof the reviewer
    /// ran live.
    #[test]
    fn v4_file_with_four_kind_relations_migrates_to_current_with_falsifies_and_sidecars() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("memory.sqlite3");
        {
            let mut store = Store::open(&path).unwrap();
            store
                .append(&capsule("migration claim alpha", "nott"), injected_now())
                .unwrap(); // cap-1
            store
                .append(&capsule("migration claim beta", "nott"), injected_now())
                .unwrap(); // cap-2
            // A legacy edge from the four-kind era — must survive the rebuild.
            store
                .upsert_relation(RelationKind::Blocks, "cap-1", "cap-2", injected_now())
                .unwrap();
        }
        // Downgrade to a FAITHFUL v4 shape: rebuild `relations` under the
        // four-kind CHECK (no falsifies), DROP the u6h sidecar tables, and
        // stamp the file v4.
        {
            let conn = rusqlite::Connection::open(&path).unwrap();
            conn.execute_batch(
                "CREATE TABLE relations_v4 (
                     kind    TEXT NOT NULL CHECK (kind IN ('supersedes', 'derived_from', 'witnesses', 'blocks')),
                     from_id TEXT NOT NULL,
                     to_id   TEXT NOT NULL,
                     at      TEXT NOT NULL,
                     PRIMARY KEY (kind, from_id, to_id)
                 );
                 INSERT INTO relations_v4 SELECT kind, from_id, to_id, at FROM relations;
                 DROP TABLE relations;
                 ALTER TABLE relations_v4 RENAME TO relations;
                 DROP TABLE outcomes;
                 DROP TABLE preferences;
                 PRAGMA user_version = 4;",
            )
            .unwrap();
            // Faithful: the v4 four-kind CHECK rejects a raw 'falsifies' edge.
            let raw = conn.execute(
                "INSERT INTO relations (kind, from_id, to_id, at) \
                 VALUES ('falsifies', 'cap-1', 'cap-2', '2026-07-18T00:00:00Z')",
                [],
            );
            assert!(
                raw.is_err(),
                "the v4 four-kind CHECK must reject a 'falsifies' edge"
            );
        }

        // Opening IS the migration.
        let mut store = Store::open(&path).unwrap();
        {
            let conn = rusqlite::Connection::open(&path).unwrap();
            let version: i64 = conn
                .query_row("PRAGMA user_version", [], |row| row.get(0))
                .unwrap();
            assert_eq!(version, SCHEMA_VERSION);
        }
        // The legacy 'blocks' edge survived the CHECK rebuild.
        assert_eq!(
            store.blockers_of("cap-2").unwrap(),
            vec!["cap-1".to_string()]
        );
        // The CHECK moved: a falsifies edge (from a fresh outcome) now
        // writes, and the outcomes sidecar was recreated by the migration.
        let outcome = store
            .append_outcome(
                "observed",
                "tester",
                None,
                Some("cap-1"),
                None,
                None,
                injected_now(),
            )
            .unwrap();
        assert_eq!(outcome.record.id, "out-1");
        assert!(
            store
                .upsert_relation(RelationKind::Falsifies, "out-1", "cap-1", injected_now())
                .unwrap()
        );
        assert!(store.is_falsified("cap-1").unwrap());
        // The preferences sidecar was recreated too.
        let pref = store
            .append_preference("cap-1", "cap-2", "which claim", "tester", injected_now())
            .unwrap();
        assert_eq!(pref.id, "pref-1");
        // No-drift on the CHECK: the migrated relations DDL carries
        // 'falsifies' (non-vacuity: a stale four-kind DDL fails this).
        let relations_ddl: String = rusqlite::Connection::open(&path)
            .unwrap()
            .query_row(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'relations'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert!(
            relations_ddl.contains("'falsifies'"),
            "migrated relations CHECK must include 'falsifies': {relations_ddl}"
        );
    }

    /// u-r11: the three governance kinds are members of the closed set —
    /// accepted by the Rust guard AND the SQL CHECK; the deliberate
    /// NON-kinds (`proof`, `outcome` — witnesses/provenance and the
    /// `out-<n>` record class already carry those meanings) stay rejected.
    #[test]
    fn governance_kinds_pass_the_guard_and_the_check_non_kinds_stay_rejected() {
        let mut store = Store::open_in_memory().unwrap();
        store
            .append(&capsule("governance kind target", "nott"), injected_now())
            .unwrap();
        for kind in ["constraint", "capability", "failure_pattern"] {
            store
                .set_classification("cap-1", kind, "project", injected_now())
                .unwrap_or_else(|e| panic!("{kind} must be a closed-set member: {e}"));
            assert_eq!(
                store.get_classification("cap-1").unwrap().unwrap().kind,
                kind
            );
        }
        for non_kind in ["proof", "outcome"] {
            assert!(
                matches!(
                    store.set_classification("cap-1", non_kind, "project", injected_now()),
                    Err(StoreError::InvalidClassification { .. })
                ),
                "{non_kind} is deliberately NOT a kind"
            );
        }
    }

    /// u-r11 MIGRATION v6→current: a faithful v6 file — classifications
    /// under the SEVEN-kind CHECK, `user_version = 6` — migrates IN PLACE
    /// on open: labels survive, the governance kinds persist, and unknown
    /// kinds stay rejected at both guards. Mirrors the v3→v4 CHECK-rebuild
    /// test (no-drift discipline).
    #[test]
    fn v6_file_with_seven_kind_classifications_migrates_to_current() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("memory.sqlite3");
        {
            let mut store = Store::open(&path).unwrap();
            store
                .append(&capsule("kind migration target", "nott"), injected_now())
                .unwrap();
            store
                .set_classification("cap-1", "task", "project", injected_now())
                .unwrap();
        }
        // Downgrade the table to the pre-v7 shape (7-kind CHECK) and
        // stamp the file v6 — a faithful substrate-era file.
        {
            let conn = rusqlite::Connection::open(&path).unwrap();
            conn.execute_batch(
                "CREATE TABLE classifications_v6 (
                     capsule_id TEXT PRIMARY KEY,
                     kind       TEXT NOT NULL CHECK (kind IN ('fact', 'procedure', 'decision', \
                 'task', 'epic', 'brainstorm', 'doc')),
                     scope      TEXT NOT NULL CHECK (scope IN ('project', 'global', 'session')),
                     at         TEXT NOT NULL
                 );
                 INSERT INTO classifications_v6 SELECT * FROM classifications;
                 DROP TABLE classifications;
                 ALTER TABLE classifications_v6 RENAME TO classifications;
                 PRAGMA user_version = 6;",
            )
            .unwrap();
            // Faithful: the seven-kind CHECK rejects a raw governance kind.
            let raw = conn.execute(
                "INSERT OR REPLACE INTO classifications (capsule_id, kind, scope, at) \
                 VALUES ('cap-1', 'constraint', 'project', '2026-07-18T00:00:00Z')",
                [],
            );
            assert!(
                raw.is_err(),
                "the v6 seven-kind CHECK must reject 'constraint'"
            );
        }

        // Opening IS the migration.
        let mut store = Store::open(&path).unwrap();
        {
            let conn = rusqlite::Connection::open(&path).unwrap();
            let version: i64 = conn
                .query_row("PRAGMA user_version", [], |row| row.get(0))
                .unwrap();
            assert_eq!(version, SCHEMA_VERSION);
        }
        // The pre-migration label survived the rebuild...
        assert_eq!(
            store.get_classification("cap-1").unwrap().unwrap().kind,
            "task"
        );
        // ...and the governance kinds persist on the migrated file.
        store
            .set_classification("cap-1", "failure_pattern", "project", injected_now())
            .unwrap();
        assert_eq!(
            store.get_classification("cap-1").unwrap().unwrap().kind,
            "failure_pattern"
        );
        // Fail-closed unchanged at both guards.
        assert!(matches!(
            store.set_classification("cap-1", "proof", "project", injected_now()),
            Err(StoreError::InvalidClassification { .. })
        ));
        {
            let conn = rusqlite::Connection::open(&path).unwrap();
            let raw = conn.execute(
                "INSERT OR REPLACE INTO classifications (capsule_id, kind, scope, at) \
                 VALUES ('cap-1', 'proof', 'project', '2026-07-18T00:00:00Z')",
                [],
            );
            assert!(raw.is_err(), "the SQL CHECK must reject unknown kinds");
        }
        // Migrated shape == fresh shape (no-drift law).
        let fresh_dir = tempfile::tempdir().unwrap();
        let fresh_path = fresh_dir.path().join("fresh.sqlite3");
        Store::open(&fresh_path).unwrap();
        assert_eq!(
            table_shape(&path, "classifications"),
            table_shape(&fresh_path, "classifications"),
            "migrated classifications shape must equal fresh shape"
        );
    }

    // ------------------------------------------------------------------
    // w2: project_prefix scope fence
    // ------------------------------------------------------------------

    /// Four projects that exercise every prefix edge: the exact id, a
    /// child, a sibling whose NAME merely starts with the prefix, and an
    /// unrelated project.
    fn seed_prefix_store() -> Store {
        let mut store = Store::open_in_memory().unwrap();
        for (text, project) in [
            ("prefix root fact", "nott"),
            ("prefix child fact", "nott/x"),
            ("prefix impostor fact", "nottx"),
            ("prefix other fact", "other"),
        ] {
            store
                .append(&capsule(text, project), injected_now())
                .unwrap();
        }
        store
    }

    #[test]
    fn list_project_prefix_matches_subtree_not_impostors() {
        let store = seed_prefix_store();

        let fenced = store
            .list(ListFilter {
                project_prefix: Some("nott".to_string()),
                ..ListFilter::default()
            })
            .unwrap();
        assert_eq!(
            fenced
                .iter()
                .map(|s| s.capsule.scope().project_id.as_str())
                .collect::<Vec<_>>(),
            vec!["nott", "nott/x"],
            "prefix must cover the exact id and the '/' subtree, never 'nottx'"
        );

        // limit still keeps the newest WITHIN the fence.
        let limited = store
            .list(ListFilter {
                project_prefix: Some("nott".to_string()),
                limit: Some(1),
                ..ListFilter::default()
            })
            .unwrap();
        assert_eq!(limited.len(), 1);
        assert_eq!(limited[0].capsule.scope().project_id, "nott/x");

        // AND-composition with the exact fence: both must hold.
        let both = store
            .list(ListFilter {
                project_id: Some("nott/x".to_string()),
                project_prefix: Some("nott".to_string()),
                ..ListFilter::default()
            })
            .unwrap();
        assert_eq!(both.len(), 1);
        assert_eq!(both[0].capsule.scope().project_id, "nott/x");
        let contradictory = store
            .list(ListFilter {
                project_id: Some("other".to_string()),
                project_prefix: Some("nott".to_string()),
                ..ListFilter::default()
            })
            .unwrap();
        assert!(contradictory.is_empty());

        // A prefix matching nothing is empty, never an error.
        assert!(
            store
                .list(ListFilter {
                    project_prefix: Some("absent".to_string()),
                    ..ListFilter::default()
                })
                .unwrap()
                .is_empty()
        );
    }

    #[test]
    fn search_fts_scoped_honors_project_prefix() {
        let store = seed_prefix_store();
        let terms = ["prefix".to_string()];

        // Unfenced: all four match (delegating wrapper unchanged).
        assert_eq!(store.search_fts(&terms, None).unwrap().len(), 4);
        assert_eq!(
            store
                .search_fts_scoped(&terms, None, None, None)
                .unwrap()
                .len(),
            4
        );

        // Prefix fence: subtree only — nott + nott/x, never nottx.
        let fenced = store
            .search_fts_scoped(&terms, None, Some("nott"), None)
            .unwrap();
        assert_eq!(
            fenced
                .iter()
                .map(|(s, _)| s.capsule.scope().project_id.as_str())
                .collect::<Vec<_>>(),
            vec!["nott", "nott/x"]
        );

        // Exact fence keeps working through the scoped form, and the two
        // fences AND-compose.
        assert_eq!(
            store
                .search_fts_scoped(&terms, Some("nottx"), None, None)
                .unwrap()
                .len(),
            1
        );
        assert_eq!(
            store
                .search_fts_scoped(&terms, Some("nott/x"), Some("nott"), None)
                .unwrap()
                .len(),
            1
        );
        assert!(
            store
                .search_fts_scoped(&terms, Some("other"), Some("nott"), None)
                .unwrap()
                .is_empty()
        );
    }

    #[test]
    fn search_fts_session_label_fence_is_exact_and_composes_before_ranking() {
        let mut store = Store::open_in_memory().unwrap();
        let exact = " Sess-\u{00e9}\0 ";
        let other = "sess-other";
        store.open_session(exact, injected_now()).unwrap();
        store.open_session(other, injected_now()).unwrap();
        store
            .append_with_session(
                &capsule("shared recall selected", "nott/sub"),
                exact,
                injected_now(),
            )
            .unwrap();
        store
            .append_with_session(
                &capsule("shared recall wrong session", "nott/sub"),
                other,
                injected_now(),
            )
            .unwrap();
        store
            .append_with_session(
                &capsule("shared recall wrong project", "other"),
                exact,
                injected_now(),
            )
            .unwrap();

        let terms = ["shared recall".to_string()];
        let scoped = store
            .search_fts_scoped(&terms, Some("nott/sub"), Some("nott"), Some(exact))
            .unwrap();
        let ids: Vec<&str> = scoped
            .iter()
            .map(|(stored, _)| stored.id.as_str())
            .collect();
        assert_eq!(ids, ["cap-1"]);

        for non_match in [
            "Sess-\u{00e9}\0",
            " sess-\u{00e9}\0 ",
            " Sess-e\u{0301}\0 ",
            " Sess-\u{00c9}\0 ",
        ] {
            assert!(
                store
                    .search_fts_scoped(&terms, None, None, Some(non_match))
                    .unwrap()
                    .is_empty(),
                "session label equality must preserve every byte: {non_match:?}"
            );
        }
    }

    #[test]
    fn absent_session_scope_is_bit_identical_to_the_legacy_fts_wrapper() {
        let mut store = Store::open_in_memory().unwrap();
        store.open_session("sess-labeled", injected_now()).unwrap();
        store
            .append_with_session(
                &capsule("dormant session fts", "nott"),
                "sess-labeled",
                injected_now(),
            )
            .unwrap();
        store
            .append(
                &capsule("dormant session fts sibling", "nott"),
                injected_now(),
            )
            .unwrap();
        let terms = ["dormant session".to_string()];
        assert_eq!(
            store.search_fts(&terms, Some("nott")).unwrap(),
            store
                .search_fts_scoped(&terms, Some("nott"), None, None)
                .unwrap(),
            "a NULL session fence must preserve rows, bm25 scores, and order"
        );
    }

    #[test]
    fn fold_term_normalizes_like_the_index() {
        assert_eq!(fold_term("  Configuração  "), "configuracao");
        assert_eq!(fold_term("TOKIO"), "tokio");
        assert_eq!(fold_term("pull request"), "pull request");
        assert_eq!(fold_term("日本語"), "日本語");
        assert_eq!(fold_term("   "), "");
    }

    // --- w3 u6a vector sidecar ------------------------------------------

    /// RED (round-trip bit-exactness): the stored little-endian blob decodes
    /// back to the EXACT `f32` bits, including values with no finite decimal
    /// representation, tiny subnormals, and signed zero — a decimal-text
    /// codec would have drifted on these.
    #[test]
    fn embedding_round_trips_bit_exact() {
        let mut store = Store::open_in_memory().unwrap();
        store
            .append(&capsule("vector host", "nott"), injected_now())
            .unwrap();
        let vector: Vec<f32> = vec![
            0.1,
            -0.333_333_34,
            f32::MIN_POSITIVE,
            1e-30,
            -0.0,
            123_456.79,
            std::f32::consts::PI,
        ];
        let fresh = store
            .put_embedding("cap-1", &vector, "unit-test-model", injected_now())
            .unwrap();
        assert!(fresh, "first put is a fresh insert");
        let got = store.get_embedding("cap-1").unwrap().unwrap();
        assert_eq!(got.dimension, vector.len());
        assert_eq!(got.model_tag, "unit-test-model");
        // Bit-exact, not just ==: -0.0 == 0.0 but their bits differ, and a
        // lossy codec would corrupt the subnormal.
        let stored_bits: Vec<u32> = got.vector.iter().map(|v| v.to_bits()).collect();
        let want_bits: Vec<u32> = vector.iter().map(|v| v.to_bits()).collect();
        assert_eq!(stored_bits, want_bits, "every f32 bit round-trips");
    }

    /// One embedding per capsule: a second put REPLACES, reported on the
    /// wire (`false`), and `get` returns the new vector (dimension may
    /// change; the model_tag stays inside q119's resident-embedder fence).
    #[test]
    fn put_embedding_replaces_on_write() {
        let mut store = Store::open_in_memory().unwrap();
        store
            .append(&capsule("replace host", "nott"), injected_now())
            .unwrap();
        assert!(
            store
                .put_embedding("cap-1", &[1.0, 0.0], "m1", injected_now())
                .unwrap()
        );
        // Second put on the same id: not fresh (replaced).
        assert!(
            !store
                .put_embedding("cap-1", &[0.0, 1.0, 0.0], "m1", later_now())
                .unwrap()
        );
        let got = store.get_embedding("cap-1").unwrap().unwrap();
        assert_eq!(got.vector, vec![0.0, 1.0, 0.0]);
        assert_eq!(got.dimension, 3);
        assert_eq!(got.model_tag, "m1");
        // Still exactly one row.
        assert_eq!(store.list_embeddings().unwrap().len(), 1);
    }

    /// RED (unknown capsule): an embedding for an id that was never stored
    /// is refused — a dangling vector would be a fabrication.
    #[test]
    fn put_embedding_unknown_capsule_errors() {
        let mut store = Store::open_in_memory().unwrap();
        let err = store
            .put_embedding("cap-999", &[1.0], "m", injected_now())
            .unwrap_err();
        assert_eq!(err, StoreError::UnknownCapsule("cap-999".to_string()));
    }

    /// RED (invalid embedding): empty, non-finite, and zero-magnitude
    /// vectors are refused (cosine is undefined for them); an empty
    /// model_tag is refused (provenance law).
    #[test]
    fn put_embedding_rejects_invalid_inputs() {
        let mut store = Store::open_in_memory().unwrap();
        store
            .append(&capsule("guard host", "nott"), injected_now())
            .unwrap();
        assert!(matches!(
            store.put_embedding("cap-1", &[], "m", injected_now()),
            Err(StoreError::InvalidEmbedding(_))
        ));
        assert!(matches!(
            store.put_embedding("cap-1", &[1.0, f32::NAN], "m", injected_now()),
            Err(StoreError::InvalidEmbedding(_))
        ));
        assert!(matches!(
            store.put_embedding("cap-1", &[1.0, f32::INFINITY], "m", injected_now()),
            Err(StoreError::InvalidEmbedding(_))
        ));
        assert!(matches!(
            store.put_embedding("cap-1", &[0.0, 0.0], "m", injected_now()),
            Err(StoreError::InvalidEmbedding(_))
        ));
        assert!(matches!(
            store.put_embedding("cap-1", &[1.0], "   ", injected_now()),
            Err(StoreError::EmptyField("model_tag"))
        ));
        // None of the rejects wrote a row.
        assert!(store.get_embedding("cap-1").unwrap().is_none());
    }

    /// `list_embeddings` returns the index in append (seq) order.
    #[test]
    fn list_embeddings_is_append_ordered() {
        let mut store = Store::open_in_memory().unwrap();
        for n in 1..=3 {
            store
                .append(&capsule(&format!("host {n}"), "nott"), injected_now())
                .unwrap();
        }
        // Put out of order; the list must still be seq-ordered (one
        // resident model_tag — the q119 fence).
        store
            .put_embedding("cap-3", &[1.0], "m", injected_now())
            .unwrap();
        store
            .put_embedding("cap-1", &[1.0], "m", injected_now())
            .unwrap();
        let rows = store.list_embeddings().unwrap();
        let ids: Vec<&str> = rows.iter().map(|r| r.capsule_id.as_str()).collect();
        assert_eq!(ids, vec!["cap-1", "cap-3"]);
    }

    /// `embeddings_for_recall` applies the project fences and excludes
    /// tombstoned capsules — the vector-lane candidate source is scoped
    /// exactly like `search_fts_scoped`.
    #[test]
    fn embeddings_for_recall_scopes_and_excludes_tombstoned() {
        let mut store = Store::open_in_memory().unwrap();
        store.append(&capsule("a", "nott"), injected_now()).unwrap(); // cap-1
        store
            .append(&capsule("b", "nott/sub"), injected_now())
            .unwrap(); // cap-2
        store
            .append(&capsule("c", "other"), injected_now())
            .unwrap(); // cap-3
        for id in ["cap-1", "cap-2", "cap-3"] {
            store
                .put_embedding(id, &[1.0, 2.0], "m", injected_now())
                .unwrap();
        }
        // Prefix fence "nott" covers nott and nott/sub, never "other".
        let scoped = store
            .embeddings_for_recall(None, Some("nott"), None)
            .unwrap();
        let ids: Vec<&str> = scoped.iter().map(|(s, _)| s.id.as_str()).collect();
        assert_eq!(ids, vec!["cap-1", "cap-2"]);
        // Forget cap-1: its embedding row remains but the capsule is
        // tombstoned, so recall (live only) must drop it.
        let key = [7u8; 32];
        store
            .forget_capsule("cap-1", TombstoneMode::Purged, "test", &key, later_now())
            .unwrap();
        let live = store
            .embeddings_for_recall(Some("nott"), None, None)
            .unwrap();
        let live_ids: Vec<&str> = live.iter().map(|(s, _)| s.id.as_str()).collect();
        assert_eq!(
            live_ids,
            Vec::<&str>::new(),
            "tombstoned capsule drops from the lane"
        );
    }

    #[test]
    fn embeddings_for_recall_session_fence_precedes_vector_decode() {
        let mut store = Store::open_in_memory().unwrap();
        store.open_session("sess-selected", injected_now()).unwrap();
        store.open_session("sess-other", injected_now()).unwrap();
        store
            .append_with_session(
                &capsule("selected vector", "nott/sub"),
                "sess-selected",
                injected_now(),
            )
            .unwrap();
        store
            .append_with_session(
                &capsule("wrong-session corrupt vector", "nott/sub"),
                "sess-other",
                injected_now(),
            )
            .unwrap();
        store
            .put_embedding("cap-1", &[1.0, 0.0], "m", injected_now())
            .unwrap();
        store
            .put_embedding("cap-2", &[0.0, 1.0], "m", injected_now())
            .unwrap();
        store
            .conn
            .execute(
                "UPDATE embeddings SET vector = X'00' WHERE capsule_id = 'cap-2'",
                [],
            )
            .unwrap();

        let scoped = store
            .embeddings_for_recall(Some("nott/sub"), Some("nott"), Some("sess-selected"))
            .unwrap();
        let ids: Vec<&str> = scoped
            .iter()
            .map(|(stored, _)| stored.id.as_str())
            .collect();
        assert_eq!(ids, ["cap-1"]);
    }

    /// A fresh store is stamped at the current version and carries the
    /// vector, lane-override, event-time, and source-backfill sidecars.
    #[test]
    fn fresh_store_is_current_version_with_additive_sidecar_tables() {
        let store = Store::open_in_memory().unwrap();
        let version: i64 = store
            .conn
            .query_row("PRAGMA user_version", [], |r| r.get(0))
            .unwrap();
        assert_eq!(version, SCHEMA_VERSION);
        assert_eq!(
            SCHEMA_VERSION, 20,
            "u6a vector took slot 5, u6h/u6i substrates took slot 6, \
             u-r11 kind-vocabulary took slot 7, u-r2 anchor-drift + \
             epistemics took slot 8, u-r5 miss-ledger took slot 9, \
             u-r8-REDESIGN stale-import-supersession took slot 10, \
             store-merge tombstone source_hash took slot 11, \
             u03 recall receipts took slot 12, \
             u04 scored-outcome feedback took slot 13, \
             u05 lane overrides took slot 14, \
             u06 caller-declared fact time took slot 15, \
             S1 pin took slot 16 and S2 git-corroboration took slot 17 \
             (sibling slices, integrated separately), \
             b2 staged review and bounded git-history checkpoints share \
             slot 18, \
             effort-lifecycle s1 part_of relation kind took slot 19, \
             planning-plane s1 grounded_in relation kind took slot 20"
        );
        let has_table: bool = store
            .conn
            .query_row(
                "SELECT EXISTS(SELECT 1 FROM sqlite_master \
                 WHERE type='table' AND name='embeddings')",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert!(has_table, "fresh store has the embeddings table");
        let has_lane_overrides: bool = store
            .conn
            .query_row(
                "SELECT EXISTS(SELECT 1 FROM sqlite_master \
                 WHERE type='table' AND name='lane_overrides')",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert!(
            has_lane_overrides,
            "fresh store has the lane_overrides table"
        );
        let has_event_time: bool = store
            .conn
            .query_row(
                "SELECT EXISTS(SELECT 1 FROM sqlite_master \
                 WHERE type='table' AND name='event_time')",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert!(has_event_time, "fresh store has the event_time table");
        let has_review_events: bool = store
            .conn
            .query_row(
                "SELECT EXISTS(SELECT 1 FROM sqlite_master \
                 WHERE type='table' AND name='review_events')",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert!(has_review_events, "fresh store has the review_events table");
        let has_pin_events: bool = store
            .conn
            .query_row(
                "SELECT EXISTS(SELECT 1 FROM sqlite_master \
                 WHERE type='table' AND name='pin_events')",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert!(has_pin_events, "fresh store has the pin_events table");
        for table in ["corroborations", "source_cursors", "source_backfills"] {
            let present: bool = store
                .conn
                .query_row(
                    "SELECT EXISTS(SELECT 1 FROM sqlite_master \
                     WHERE type='table' AND name=?1)",
                    [table],
                    |r| r.get(0),
                )
                .unwrap();
            assert!(present, "fresh store has the {table} table");
        }
    }

    /// u05 negative proof: equal lanes are not overrides and therefore
    /// cannot be represented in the telemetry table, even through raw SQL.
    #[test]
    fn lane_override_schema_rejects_an_equal_pair() {
        let store = Store::open_in_memory().unwrap();
        let inserted = store.conn.execute(
            "INSERT INTO lane_overrides (forced, auto_pick, at) VALUES ('term', 'term', ?1)",
            [rfc3339_text(injected_now()).unwrap()],
        );
        assert!(
            inserted.is_err(),
            "schema accepted illegal equal override term/term: {inserted:?}"
        );
    }

    #[test]
    fn lane_override_schema_accepts_exactly_the_four_disagreement_pairs() {
        let store = Store::open_in_memory().unwrap();
        let values = ["auto", "term", "vector", "fused", "invalid"];
        let at = rfc3339_text(injected_now()).unwrap();
        for forced in values {
            for auto_pick in values {
                let legal = matches!(
                    (forced, auto_pick),
                    ("term", "fused")
                        | ("vector", "fused")
                        | ("vector", "term")
                        | ("fused", "term")
                );
                let inserted = store.conn.execute(
                    "INSERT INTO lane_overrides (forced, auto_pick, at) VALUES (?1, ?2, ?3)",
                    params![forced, auto_pick, at],
                );
                assert_eq!(
                    inserted.is_ok(),
                    legal,
                    "forced={forced:?}, auto_pick={auto_pick:?}: {inserted:?}"
                );
            }
        }
    }

    #[test]
    fn lane_override_writer_is_typed_and_totals_are_grouped_and_ordered() {
        let mut store = Store::open_in_memory().unwrap();
        for override_ in [
            LaneOverride::VectorOverTerm,
            LaneOverride::TermOverFused,
            LaneOverride::VectorOverFused,
            LaneOverride::FusedOverTerm,
            LaneOverride::TermOverFused,
        ] {
            store
                .record_lane_override(override_, injected_now())
                .unwrap();
        }
        assert_eq!(
            store.lane_override_totals().unwrap(),
            vec![
                ("fused".to_string(), "term".to_string(), 1),
                ("term".to_string(), "fused".to_string(), 2),
                ("vector".to_string(), "fused".to_string(), 1),
                ("vector".to_string(), "term".to_string(), 1),
            ]
        );
    }

    #[test]
    fn lane_override_totals_rejects_illegal_rows_from_a_hand_shaped_v14_table() {
        let store = Store::open_in_memory().unwrap();
        store
            .conn
            .execute_batch(
                "DROP TABLE lane_overrides;
                 CREATE TABLE lane_overrides (
                     seq INTEGER PRIMARY KEY,
                     forced TEXT NOT NULL,
                     auto_pick TEXT NOT NULL,
                     at TEXT NOT NULL
                 );",
            )
            .unwrap();
        store
            .conn
            .execute(
                "INSERT INTO lane_overrides (forced, auto_pick, at) \
                 VALUES ('term', 'term', ?1)",
                [rfc3339_text(injected_now()).unwrap()],
            )
            .unwrap();

        assert_eq!(
            store.lane_override_totals().unwrap_err(),
            StoreError::Corrupt {
                id: "lane_overrides:1".to_string(),
                reason: "illegal lane override pair forced=\"term\", auto_pick=\"term\""
                    .to_string(),
            }
        );
    }

    #[test]
    fn lane_override_totals_rejects_bad_seq_and_time_from_a_hand_shaped_v14_table() {
        let store = Store::open_in_memory().unwrap();
        store
            .conn
            .execute_batch(
                "DROP TABLE lane_overrides;
                 CREATE TABLE lane_overrides (
                     seq INTEGER PRIMARY KEY,
                     forced TEXT NOT NULL,
                     auto_pick TEXT NOT NULL,
                     at TEXT NOT NULL
                 );",
            )
            .unwrap();
        store
            .conn
            .execute(
                "INSERT INTO lane_overrides (seq, forced, auto_pick, at) \
                 VALUES (0, 'vector', 'term', ?1)",
                [rfc3339_text(injected_now()).unwrap()],
            )
            .unwrap();
        assert_eq!(
            store.lane_override_totals().unwrap_err(),
            StoreError::Corrupt {
                id: "lane_overrides:0".to_string(),
                reason: "lane override seq must be positive, got 0".to_string(),
            }
        );

        store
            .conn
            .execute("DELETE FROM lane_overrides", [])
            .unwrap();
        store
            .conn
            .execute(
                "INSERT INTO lane_overrides (forced, auto_pick, at) \
                 VALUES ('vector', 'term', 'not-rfc3339')",
                [],
            )
            .unwrap();

        match store.lane_override_totals().unwrap_err() {
            StoreError::Corrupt { id, reason } => {
                assert_eq!(id, "lane_overrides:1");
                assert!(
                    reason.starts_with("lane override timestamp is not RFC3339:"),
                    "unexpected reason: {reason}"
                );
            }
            other => panic!("malformed timestamp must be typed corrupt: {other:?}"),
        }
    }

    #[test]
    fn missing_lane_override_table_is_an_honest_store_error() {
        let mut store = Store::open_in_memory().unwrap();
        store
            .conn
            .execute_batch("DROP TABLE lane_overrides")
            .unwrap();
        assert!(matches!(
            store
                .record_lane_override(LaneOverride::TermOverFused, injected_now())
                .unwrap_err(),
            StoreError::Backend(_)
        ));
        assert!(matches!(
            store.lane_override_totals().unwrap_err(),
            StoreError::Backend(_)
        ));
    }

    /// RED (migration v4 -> current): a genuine v4 file lacks the
    /// `embeddings` table and is stamped 4. Opening it migrates in place —
    /// the table is created and the stamp advances to the current version —
    /// WITHOUT touching any capsule.
    #[test]
    fn v4_file_migrates_to_current_and_gains_embeddings() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("memory.sqlite3");
        {
            let mut store = Store::open(&path).unwrap();
            store
                .append(&capsule("pre-migration", "nott"), injected_now())
                .unwrap();
        }
        // Simulate a v4 file: drop the embeddings table and re-stamp v4.
        {
            let conn = rusqlite::Connection::open(&path).unwrap();
            conn.execute_batch("DROP TABLE embeddings; PRAGMA user_version = 4;")
                .unwrap();
        }
        // Reopen: v4 is an enumerated migratable version, so this succeeds.
        let mut store = Store::open(&path).unwrap();
        let version: i64 = store
            .conn
            .query_row("PRAGMA user_version", [], |r| r.get(0))
            .unwrap();
        assert_eq!(
            version, SCHEMA_VERSION,
            "migrated file re-stamped to the current version"
        );
        // The capsule survived and the recreated table is writable.
        assert!(store.get("cap-1").unwrap().is_some());
        assert!(
            store
                .put_embedding("cap-1", &[1.0], "m", later_now())
                .unwrap()
        );
        assert_eq!(store.list_embeddings().unwrap().len(), 1);
    }

    /// RED (renumbering, integration K): a faithful v5 file — the
    /// vector-only era: `embeddings` present, relations still under the
    /// FOUR-kind CHECK, NO outcomes/preferences tables, `user_version = 5`
    /// — is an enumerated migratable version. Opening it advances the stamp
    /// to the current version, rebuilds the relations CHECK with
    /// `falsifies`, creates both substrate sidecars, and touches neither
    /// capsules nor embeddings. Kills the renumbering trap: an accept-arm
    /// that skipped 5 would answer UnsupportedSchemaVersion to every store
    /// the vector-only binary stamped.
    #[test]
    fn v5_vector_era_file_migrates_to_current_and_gains_substrates() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("memory.sqlite3");
        {
            let mut store = Store::open(&path).unwrap();
            store
                .append(&capsule("vector-era claim", "nott"), injected_now())
                .unwrap(); // cap-1
            assert!(
                store
                    .put_embedding("cap-1", &[0.5, 0.5], "m", injected_now())
                    .unwrap()
            );
        }
        // Downgrade to a FAITHFUL v5 shape: four-kind relations CHECK, no
        // substrate sidecars, embeddings kept, stamp 5.
        {
            let conn = rusqlite::Connection::open(&path).unwrap();
            conn.execute_batch(
                "CREATE TABLE relations_v5 (
                     kind    TEXT NOT NULL CHECK (kind IN ('supersedes', 'derived_from', 'witnesses', 'blocks')),
                     from_id TEXT NOT NULL,
                     to_id   TEXT NOT NULL,
                     at      TEXT NOT NULL,
                     PRIMARY KEY (kind, from_id, to_id)
                 );
                 INSERT INTO relations_v5 SELECT kind, from_id, to_id, at FROM relations;
                 DROP TABLE relations;
                 ALTER TABLE relations_v5 RENAME TO relations;
                 DROP TABLE outcomes;
                 DROP TABLE preferences;
                 PRAGMA user_version = 5;",
            )
            .unwrap();
        }

        // Opening IS the migration — v5 is enumerated, never fail-closed.
        let mut store = Store::open(&path).unwrap();
        let version: i64 = store
            .conn
            .query_row("PRAGMA user_version", [], |r| r.get(0))
            .unwrap();
        assert_eq!(
            version, SCHEMA_VERSION,
            "v5 re-stamped to the current version"
        );
        // Capsule and embedding survived untouched.
        assert!(store.get("cap-1").unwrap().is_some());
        assert_eq!(store.list_embeddings().unwrap().len(), 1);
        // The substrates arrived: an outcome writes, and its falsifies edge
        // passes the rebuilt CHECK and fences the capsule.
        let outcome = store
            .append_outcome(
                "observed",
                "tester",
                None,
                Some("cap-1"),
                None,
                None,
                injected_now(),
            )
            .unwrap();
        assert!(
            store
                .upsert_relation(
                    RelationKind::Falsifies,
                    &outcome.record.id,
                    "cap-1",
                    injected_now()
                )
                .unwrap()
        );
        assert!(store.is_falsified("cap-1").unwrap());
    }

    /// u-r2 RED (migration era): a faithful v6 file — the substrate era:
    /// outcomes/preferences present, NO `anchor_hashes` / `epistemics`
    /// tables, `user_version = 6` — is an enumerated migratable version.
    /// Opening it advances the stamp to the current version, creates both
    /// v7 sidecars, and touches no capsule byte. Kills the renumbering
    /// trap: an accept-arm that skipped 6 would answer
    /// UnsupportedSchemaVersion to every store the substrate-era binary
    /// stamped.
    #[test]
    fn v6_substrate_era_file_migrates_to_current_and_gains_the_epistemic_sidecars() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("memory.sqlite3");
        {
            let mut store = Store::open(&path).unwrap();
            store
                .append(&capsule("substrate-era claim", "nott"), injected_now())
                .unwrap(); // cap-1
        }
        // Downgrade to a FAITHFUL v6 shape: no v7 sidecars, stamp 6.
        {
            let conn = rusqlite::Connection::open(&path).unwrap();
            conn.execute_batch(
                "DROP TABLE anchor_hashes;
                 DROP TABLE epistemics;
                 PRAGMA user_version = 6;",
            )
            .unwrap();
        }

        // Opening IS the migration — v6 is enumerated, never fail-closed.
        let mut store = Store::open(&path).unwrap();
        let version: i64 = store
            .conn
            .query_row("PRAGMA user_version", [], |r| r.get(0))
            .unwrap();
        assert_eq!(
            version, SCHEMA_VERSION,
            "v6 re-stamped to the current version"
        );
        // Capsule survived; both v7 sidecars arrived writable + readable.
        assert!(store.get("cap-1").unwrap().is_some());
        assert!(
            store
                .set_anchor_hash("cap-1", &sha256_hex(b"anchored file bytes"), injected_now())
                .unwrap()
        );
        assert_eq!(
            store.anchor_hash_of("cap-1").unwrap(),
            Some(sha256_hex(b"anchored file bytes"))
        );
        store
            .set_epistemics("cap-1", Some("observed"), None, None, injected_now())
            .unwrap();
        assert_eq!(
            store
                .epistemics_of("cap-1")
                .unwrap()
                .unwrap()
                .evidence_state
                .as_deref(),
            Some("observed")
        );
    }

    /// u-r5 RED (migration era): a faithful v8 file — the epistemic-sidecar
    /// era: anchor_hashes/epistemics present, NO `recall_misses` table,
    /// `user_version = 8` — is an enumerated migratable version. Opening it
    /// advances the stamp to the current version, creates the recall-miss
    /// ledger, and touches no capsule byte. Kills the renumbering trap: an
    /// accept-arm that skipped 8 would answer UnsupportedSchemaVersion to
    /// every store the epistemic-era binary stamped.
    #[test]
    fn v8_epistemic_era_file_migrates_to_current_and_gains_recall_misses() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("memory.sqlite3");
        {
            let mut store = Store::open(&path).unwrap();
            store
                .append(&capsule("epistemic-era claim", "nott"), injected_now())
                .unwrap(); // cap-1
        }
        // Downgrade to a FAITHFUL v8 shape: no recall_misses, stamp 8.
        {
            let conn = rusqlite::Connection::open(&path).unwrap();
            conn.execute_batch("DROP TABLE recall_misses; PRAGMA user_version = 8;")
                .unwrap();
        }

        // Opening IS the migration — v8 is enumerated, never fail-closed.
        let mut store = Store::open(&path).unwrap();
        let version: i64 = store
            .conn
            .query_row("PRAGMA user_version", [], |r| r.get(0))
            .unwrap();
        assert_eq!(
            version, SCHEMA_VERSION,
            "v8 re-stamped to the current version"
        );
        // Capsule survived; the ledger arrived writable + readable.
        assert!(store.get("cap-1").unwrap().is_some());
        assert_eq!(
            store
                .record_recall_miss(
                    &["retreival".to_string()],
                    RecallMissOutcome::Abstain,
                    later_now()
                )
                .unwrap(),
            1
        );
        assert_eq!(
            store.recall_miss_terms().unwrap(),
            vec![("retreival".to_string(), 1)]
        );
        assert_eq!(store.count_recall_misses().unwrap(), 1);
    }

    /// u-r8-REDESIGN RED (migration era): a faithful v9 file — the
    /// miss-ledger era: recall_misses present, NO `import_blocks` table,
    /// `user_version = 9` — is an enumerated migratable version. Opening it
    /// advances the stamp to the current version, creates the import-block
    /// lineage sidecar, and touches no capsule byte. Kills the renumbering
    /// trap: an accept-arm that skipped 9 would answer
    /// UnsupportedSchemaVersion to every store the miss-ledger-era binary
    /// stamped.
    #[test]
    fn v9_miss_ledger_era_file_migrates_to_current_and_gains_import_blocks() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("memory.sqlite3");
        {
            let mut store = Store::open(&path).unwrap();
            store
                .append(&capsule("miss-ledger-era claim", "nott"), injected_now())
                .unwrap(); // cap-1
        }
        // Downgrade to a FAITHFUL v9 shape: no import_blocks, stamp 9.
        {
            let conn = rusqlite::Connection::open(&path).unwrap();
            conn.execute_batch("DROP TABLE import_blocks; PRAGMA user_version = 9;")
                .unwrap();
        }

        // Opening IS the migration — v9 is enumerated, never fail-closed.
        let mut store = Store::open(&path).unwrap();
        let version: i64 = store
            .conn
            .query_row("PRAGMA user_version", [], |r| r.get(0))
            .unwrap();
        assert_eq!(
            version, SCHEMA_VERSION,
            "v9 re-stamped to the current version"
        );
        // Capsule survived; the lineage sidecar arrived writable + readable.
        assert!(store.get("cap-1").unwrap().is_some());
        let src = "user-claude-md\nCLAUDE.md";
        let block = sha256_hex(b"anchored block bytes");
        assert!(
            store
                .record_import_block(src, &block, "cap-1", 0, later_now())
                .unwrap()
        );
        let rows = store.import_blocks_for(src).unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].capsule_id, "cap-1");
        assert_eq!(rows[0].block_hash, block);
    }

    /// A faithful v11 file predates the recall-receipt ledger, scored-outcome
    /// columns, and feedback sidecar. Opening it migrates in place to the
    /// current schema and leaves every canonical capsule byte untouched.
    #[test]
    fn v11_file_migrates_to_current_and_gains_recall_receipts() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("memory.sqlite3");
        let before = {
            let mut store = Store::open(&path).unwrap();
            store
                .append(&capsule("v11 receipt migration", "nott"), injected_now())
                .unwrap();
            store.canonical_snapshot().unwrap()
        };
        {
            let conn = rusqlite::Connection::open(&path).unwrap();
            conn.execute_batch(
                "DROP TABLE recall_receipts; \
                 DROP TABLE feedback_weights; \
                 ALTER TABLE outcomes DROP COLUMN receipt_id; \
                 ALTER TABLE outcomes DROP COLUMN score; \
                 PRAGMA user_version = 11;",
            )
            .unwrap();
        }

        let mut store = Store::open(&path).unwrap();
        let version: i64 = store
            .conn
            .query_row("PRAGMA user_version", [], |row| row.get(0))
            .unwrap();
        assert_eq!(version, SCHEMA_VERSION);
        assert_eq!(store.canonical_snapshot().unwrap(), before);
        assert_eq!(store.receipt_returned_ids("rcpt-1").unwrap(), None);
        assert_eq!(
            store
                .record_recall_receipt(
                    &["migration".to_string()],
                    &["cap-1"],
                    Some("nott"),
                    None,
                    None,
                    later_now(),
                )
                .unwrap(),
            "rcpt-1"
        );
        assert_eq!(
            store.receipt_returned_ids("rcpt-1").unwrap(),
            Some(vec!["cap-1".to_string()])
        );
    }

    /// A faithful v12 file has recall receipts and legacy unscored outcomes,
    /// but no scored columns or feedback sidecar. Migration adds only NULL
    /// columns plus the empty sidecar: canonical capsules and the legacy
    /// outcome decode identically, then the new table is writable.
    #[test]
    fn v12_file_migrates_to_current_and_gains_scored_outcome_feedback() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("memory.sqlite3");
        let (before_snapshot, before_outcome) = {
            let mut store = Store::open(&path).unwrap();
            store
                .append(
                    &capsule("v12 scored-outcome migration", "nott"),
                    injected_now(),
                )
                .unwrap();
            let outcome = store
                .append_outcome(
                    "legacy observation",
                    "tester",
                    Some("ci://legacy"),
                    Some("cap-1"),
                    None,
                    None,
                    injected_now(),
                )
                .unwrap()
                .record;
            (store.canonical_snapshot().unwrap(), outcome)
        };
        {
            let conn = rusqlite::Connection::open(&path).unwrap();
            conn.execute_batch(
                "DROP TABLE feedback_weights; \
                 ALTER TABLE outcomes DROP COLUMN receipt_id; \
                 ALTER TABLE outcomes DROP COLUMN score; \
                 PRAGMA user_version = 12;",
            )
            .unwrap();
        }

        let mut store = Store::open(&path).unwrap();
        let version: i64 = store
            .conn
            .query_row("PRAGMA user_version", [], |row| row.get(0))
            .unwrap();
        assert_eq!(version, SCHEMA_VERSION);
        assert_eq!(store.canonical_snapshot().unwrap(), before_snapshot);
        assert_eq!(store.list_outcomes().unwrap(), vec![before_outcome]);
        assert_eq!(store.feedback_weight_of("cap-1").unwrap(), None);
        assert_eq!(
            store.apply_feedback(&["cap-1"], 1.0, later_now()).unwrap(),
            vec![("cap-1".to_string(), 0.55)]
        );
    }

    /// A faithful v13 file carries every scored-outcome surface but no lane
    /// override telemetry. Migration adds only the empty u05 sidecar and
    /// leaves canonical capsule bytes untouched.
    #[test]
    fn v13_file_migrates_to_v14_and_gains_lane_override_telemetry() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("memory.sqlite3");
        let before = {
            let mut store = Store::open(&path).unwrap();
            store
                .append(&capsule("v13 lane migration", "nott"), injected_now())
                .unwrap();
            store.canonical_snapshot().unwrap()
        };
        {
            let conn = rusqlite::Connection::open(&path).unwrap();
            conn.execute_batch("DROP TABLE lane_overrides; PRAGMA user_version = 13;")
                .unwrap();
        }

        let mut store = Store::open(&path).unwrap();
        let version: i64 = store
            .conn
            .query_row("PRAGMA user_version", [], |row| row.get(0))
            .unwrap();
        assert_eq!(version, SCHEMA_VERSION);
        assert_eq!(store.canonical_snapshot().unwrap(), before);
        assert!(store.lane_override_totals().unwrap().is_empty());
        store
            .record_lane_override(LaneOverride::TermOverFused, later_now())
            .unwrap();
        assert_eq!(
            store.lane_override_totals().unwrap(),
            vec![("term".to_string(), "fused".to_string(), 1)]
        );
    }

    /// u-r8-REDESIGN: the import-block lineage sidecar records keep-first,
    /// lists in `(ordinal, hash)` order, fails closed on an unknown
    /// capsule and on an empty key, and forgets exactly one
    /// `(source_key, block_hash)` row — the rest of the source's live-block
    /// map untouched. This is the membership set the auto-supersede/revive
    /// fence keys on.
    #[test]
    fn import_block_lineage_records_lists_forgets_and_fails_closed() {
        let mut store = Store::open_in_memory().unwrap();
        let src = "project-claude-md\nCLAUDE.md";
        // Fresh: nothing recorded, never an error.
        assert!(store.import_blocks_for(src).unwrap().is_empty());
        // Two stored capsules to key lineage rows against.
        store
            .append(&capsule("alpha block", "nott"), injected_now())
            .unwrap(); // cap-1
        store
            .append(&capsule("beta block", "nott"), injected_now())
            .unwrap(); // cap-2
        let ha = sha256_hex(b"alpha block");
        let hb = sha256_hex(b"beta block");
        // Fresh record returns true; a keep-first re-record returns false.
        assert!(
            store
                .record_import_block(src, &ha, "cap-1", 0, injected_now())
                .unwrap()
        );
        assert!(
            !store
                .record_import_block(src, &ha, "cap-1", 0, later_now())
                .unwrap()
        );
        assert!(
            store
                .record_import_block(src, &hb, "cap-2", 1, injected_now())
                .unwrap()
        );
        // Listed in (ordinal, hash) order.
        let rows = store.import_blocks_for(src).unwrap();
        assert_eq!(
            rows.iter()
                .map(|r| r.capsule_id.as_str())
                .collect::<Vec<_>>(),
            vec!["cap-1", "cap-2"]
        );
        assert_eq!(rows[0].ordinal, 0);
        assert_eq!(rows[1].block_hash, hb);
        // Fails closed on an unknown capsule — nothing recorded.
        assert_eq!(
            store
                .record_import_block(src, "deadbeef", "cap-99", 2, injected_now())
                .unwrap_err(),
            StoreError::UnknownCapsule("cap-99".to_string())
        );
        // Empty source_key / block_hash are EmptyField faults, checked
        // BEFORE the capsule lookup.
        assert_eq!(
            store
                .record_import_block("  ", &ha, "cap-1", 0, injected_now())
                .unwrap_err(),
            StoreError::EmptyField("import block source_key")
        );
        assert_eq!(
            store
                .record_import_block(src, "  ", "cap-1", 0, injected_now())
                .unwrap_err(),
            StoreError::EmptyField("import block hash")
        );
        // A different source_key is an independent namespace.
        assert!(
            store
                .import_blocks_for("other\nAGENTS.md")
                .unwrap()
                .is_empty()
        );
        // forget drops exactly the named row; the rest of the map stays.
        store.forget_import_block(src, &ha).unwrap();
        let rows = store.import_blocks_for(src).unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].capsule_id, "cap-2");
        // Forgetting an absent row is a no-op, never an error.
        store.forget_import_block(src, "deadbeef").unwrap();
        assert_eq!(store.import_blocks_for(src).unwrap().len(), 1);
    }

    /// u-r8-REDESIGN (bug 2 multi-owner fix): [`Store::import_block_owners`]
    /// reports every DISTINCT live source_key naming a capsule, in
    /// deterministic order, and answers empty for a capsule that was never
    /// recorded into any lineage row — the fence a hand-ingested capsule
    /// relies on, whatever else exists in the table.
    #[test]
    fn import_block_owners_reports_every_live_source_and_empty_for_an_unrecorded_capsule() {
        let mut store = Store::open_in_memory().unwrap();
        store
            .append(&capsule("shared block", "nott"), injected_now())
            .unwrap(); // cap-1
        store
            .append(&capsule("never recorded", "nott"), injected_now())
            .unwrap(); // cap-2
        let hash = sha256_hex(b"shared block");
        assert!(store.import_block_owners("cap-1").unwrap().is_empty());
        store
            .record_import_block("memory-dir\na.md", &hash, "cap-1", 0, injected_now())
            .unwrap();
        store
            .record_import_block("memory-dir\nb.md", &hash, "cap-1", 0, later_now())
            .unwrap();
        assert_eq!(
            store.import_block_owners("cap-1").unwrap(),
            vec![
                "memory-dir\na.md".to_string(),
                "memory-dir\nb.md".to_string()
            ],
            "deterministic ascending order, both live owners reported"
        );
        // The fence: a capsule never adopted into lineage has ZERO owners
        // no matter how many rows exist for OTHER capsules.
        assert!(store.import_block_owners("cap-2").unwrap().is_empty());
        // Dropping ONE owner's row leaves the other reachable.
        store
            .forget_import_block("memory-dir\na.md", &hash)
            .unwrap();
        assert_eq!(
            store.import_block_owners("cap-1").unwrap(),
            vec!["memory-dir\nb.md".to_string()]
        );
    }

    /// u-r8-REDESIGN (bug 1 revive fix) + round 3 origin fence:
    /// [`Store::unsupersede`] reverses EXACTLY the named MACHINE edge —
    /// [`Store::is_superseded`] flips back to `false` for the revived id,
    /// an UNRELATED predecessor of the same successor is untouched (a
    /// targeted reversal, never a blanket unsupersede), and re-reversing
    /// an absent edge is a documented no-op. A caller-written (`manual`)
    /// edge NEVER deletes: the call answers `false` and the row stays —
    /// the machine only unwrites what the machine wrote.
    #[test]
    fn unsupersede_reverses_exactly_the_named_machine_edge_and_never_a_manual_one() {
        let mut store = Store::open_in_memory().unwrap();
        store
            .append(&capsule("alpha", "nott"), injected_now())
            .unwrap(); // cap-1
        store
            .append(&capsule("beta", "nott"), injected_now())
            .unwrap(); // cap-2
        store
            .append(&capsule("gamma", "nott"), injected_now())
            .unwrap(); // cap-3
        // cap-3 supersedes BOTH cap-1 and cap-2 — MACHINE-written edges
        // (the import mechanism's own), the only reversible kind; two
        // distinct predecessors of one successor, the corner a targeted
        // reversal must respect.
        store
            .supersede_imported("cap-1", "cap-3", injected_now())
            .unwrap();
        store
            .supersede_imported("cap-2", "cap-3", injected_now())
            .unwrap();
        assert!(store.is_superseded("cap-1").unwrap());
        assert!(store.is_superseded("cap-2").unwrap());

        assert!(store.unsupersede("cap-1", "cap-3").unwrap());
        assert!(
            !store.is_superseded("cap-1").unwrap(),
            "the named machine edge is gone — cap-1 grounds again"
        );
        assert!(
            store.is_superseded("cap-2").unwrap(),
            "an unrelated predecessor of the same successor is untouched"
        );

        // A no-op on an absent edge — never an error, never a false report.
        assert!(!store.unsupersede("cap-1", "cap-3").unwrap());
        assert!(!store.unsupersede("cap-1", "cap-2").unwrap());

        // THE ORIGIN FENCE (round 3): a caller-written edge survives every
        // machine reversal attempt — `false`, row intact, still superseded.
        store.supersede("cap-1", "cap-2", injected_now()).unwrap();
        assert!(
            !store.unsupersede("cap-1", "cap-2").unwrap(),
            "a manual edge is a human decision — the machine may not delete it"
        );
        assert!(
            store.is_superseded("cap-1").unwrap(),
            "the manual edge stays; cap-1 remains superseded"
        );
        // Replay with a different origin is first-write-wins: the machine
        // re-recording the SAME edge does not relabel it import.
        store
            .supersede_imported("cap-1", "cap-2", injected_now())
            .unwrap();
        assert!(
            !store.unsupersede("cap-1", "cap-2").unwrap(),
            "an INSERT OR IGNORE replay never rewrites origin"
        );
        let edges = store.list_relations("cap-2").unwrap();
        assert!(
            edges.iter().any(|r| r.from_id == "cap-2"
                && r.to_id == "cap-1"
                && r.origin == RelationOrigin::Manual),
            "the surviving row still reads back manual"
        );
    }

    /// fleet-8 c7 F2: forget destroys the vector sidecar row WITH the
    /// content — both modes; the forgotten id stops being enumerable via
    /// `list_embeddings` (pre-fix the embedding bytes and the
    /// id/model_tag row survived a "nothing retained" purge — a
    /// content-derived, in-principle-invertible artifact outliving the
    /// destruction primitive).
    #[test]
    fn forget_destroys_the_embedding_sidecar_row_in_both_modes() {
        let mut store = Store::open_in_memory().unwrap();
        store
            .append(&capsule("alpha vector", "nott"), injected_now())
            .unwrap(); // cap-1
        store
            .append(&capsule("beta vector", "nott"), injected_now())
            .unwrap(); // cap-2
        store
            .put_embedding("cap-1", &[1.0, 0.0], "m", injected_now())
            .unwrap();
        store
            .put_embedding("cap-2", &[0.0, 1.0], "m", injected_now())
            .unwrap();
        assert_eq!(store.list_embeddings().unwrap().len(), 2);

        store
            .forget_capsule("cap-1", TombstoneMode::Purged, "test", b"k", injected_now())
            .unwrap();
        store
            .forget_capsule(
                "cap-2",
                TombstoneMode::Redacted,
                "test",
                b"k",
                injected_now(),
            )
            .unwrap();
        let rows = store.list_embeddings().unwrap();
        assert!(
            rows.is_empty(),
            "no embedding row outlives a forget: {rows:?}"
        );
    }

    /// u-r5: the miss ledger folds terms like the alias key, deduplicates
    /// within one query (a repeated or diacritic-equal term counts once),
    /// drops terms with no alphanumeric, and its GROUP BY count is the
    /// number of missing queries carrying the term. `at` is injected.
    #[test]
    fn recall_miss_ledger_folds_dedups_and_counts() {
        let mut store = Store::open_in_memory().unwrap();
        // Fresh: nothing recorded.
        assert!(store.recall_miss_terms().unwrap().is_empty());
        assert_eq!(store.count_recall_misses().unwrap(), 0);

        // One query, four RAW terms folding to TWO uniques ("Café"/"cafe"
        // collapse) plus a punctuation-only term dropped for carrying no
        // alphanumeric.
        let inserted = store
            .record_recall_miss(
                &[
                    "Café".to_string(),
                    "cafe".to_string(),
                    "Tokio".to_string(),
                    "!!!".to_string(),
                ],
                RecallMissOutcome::Abstain,
                injected_now(),
            )
            .unwrap();
        assert_eq!(inserted, 2, "folded-dedup to {{cafe, tokio}}; junk dropped");

        // A second pre-trim term-lane miss carrying "tokio" again —
        // missing_evidence this time; both typed miss outcomes contribute.
        store
            .record_recall_miss(
                &["tokio".to_string()],
                RecallMissOutcome::MissingEvidence,
                later_now(),
            )
            .unwrap();

        // miss_count: tokio twice, cafe once — deterministic term asc.
        assert_eq!(
            store.recall_miss_terms().unwrap(),
            vec![("cafe".to_string(), 1), ("tokio".to_string(), 2)]
        );
        assert_eq!(store.count_recall_misses().unwrap(), 3);

        // The outcome column carries the closed wire values, per append
        // order (two abstains from query one, then the missing_evidence).
        let outcomes: Vec<String> = {
            let mut stmt = store
                .conn
                .prepare("SELECT outcome FROM recall_misses ORDER BY seq")
                .unwrap();
            stmt.query_map([], |r| r.get::<_, String>(0))
                .unwrap()
                .map(Result::unwrap)
                .collect()
        };
        assert_eq!(outcomes, vec!["abstain", "abstain", "missing_evidence"]);

        // An all-junk query records nothing — no phantom row.
        assert_eq!(
            store
                .record_recall_miss(
                    &["   ".to_string(), "()".to_string()],
                    RecallMissOutcome::Abstain,
                    later_now()
                )
                .unwrap(),
            0
        );
        assert_eq!(store.count_recall_misses().unwrap(), 3);
    }

    /// u10: the digest reader consumes folded-term ROWS, not grouped query
    /// counts. Newest means sequence order, so terms appended by one query
    /// appear in reverse insertion order when read newest-first.
    #[test]
    fn recent_recall_misses_returns_typed_rows_newest_by_sequence() {
        let mut store = Store::open_in_memory().unwrap();
        store
            .record_recall_miss(
                &["Alpha".to_string(), "Beta".to_string()],
                RecallMissOutcome::Abstain,
                injected_now(),
            )
            .unwrap();
        store
            .record_recall_miss(
                &["Gamma".to_string()],
                RecallMissOutcome::MissingEvidence,
                later_now(),
            )
            .unwrap();

        let rows = store.recent_recall_misses(3).unwrap();
        assert_eq!(rows.len(), 3);
        assert_eq!(
            rows.iter().map(|row| row.seq).collect::<Vec<_>>(),
            vec![3, 2, 1]
        );
        assert_eq!(
            rows.iter().map(|row| row.term.as_str()).collect::<Vec<_>>(),
            vec!["gamma", "beta", "alpha"]
        );
        assert_eq!(rows[0].outcome, RecallMissOutcome::MissingEvidence);
        assert_eq!(rows[1].outcome, RecallMissOutcome::Abstain);
        assert_eq!(rows[0].at, later_now());
        assert_eq!(rows[2].at, injected_now());
        assert_eq!(
            store
                .recent_recall_misses(2)
                .unwrap()
                .iter()
                .map(|row| row.term.as_str())
                .collect::<Vec<_>>(),
            vec!["gamma", "beta"],
            "N counts folded-term rows, not queries"
        );
        assert!(store.recent_recall_misses(0).unwrap().is_empty());
    }

    /// A hand-shaped current-version database cannot smuggle an open outcome,
    /// non-positive sequence, non-canonical term, or malformed timestamp into
    /// the digest. The reader returns one typed corruption error and no partial
    /// vector even when an older valid row exists.
    #[test]
    fn recent_recall_misses_revalidates_every_persisted_field() {
        fn hand_shaped_store(seq: i64, term: &str, outcome: &str, at: &str) -> Store {
            let store = Store::open_in_memory().unwrap();
            store
                .conn
                .execute_batch(
                    "DROP TABLE recall_misses;
                     CREATE TABLE recall_misses (
                       seq INTEGER PRIMARY KEY,
                       term TEXT NOT NULL,
                       outcome TEXT NOT NULL,
                       at TEXT NOT NULL
                     );",
                )
                .unwrap();
            store
                .conn
                .execute(
                    "INSERT INTO recall_misses (seq, term, outcome, at)
                     VALUES (1, 'older-valid', 'abstain', '2001-02-03T02:05:06Z')",
                    [],
                )
                .unwrap();
            store
                .conn
                .execute(
                    "INSERT OR REPLACE INTO recall_misses (seq, term, outcome, at)
                     VALUES (?1, ?2, ?3, ?4)",
                    params![seq, term, outcome, at],
                )
                .unwrap();
            store
        }

        for (label, store, reason) in [
            (
                "outcome",
                hand_shaped_store(2, "term", "grounded", "2001-02-03T02:05:07Z"),
                "outcome",
            ),
            (
                "sequence",
                hand_shaped_store(0, "term", "abstain", "2001-02-03T02:05:07Z"),
                "positive",
            ),
            (
                "term",
                hand_shaped_store(2, " Term ", "abstain", "2001-02-03T02:05:07Z"),
                "canonical",
            ),
            (
                "timestamp",
                hand_shaped_store(2, "term", "abstain", "not-a-time"),
                "RFC3339",
            ),
        ] {
            let err = store.recent_recall_misses(5).unwrap_err();
            match err {
                StoreError::Corrupt { id, reason: actual } => {
                    assert!(id.starts_with("recall_misses:"), "{label}: {id}");
                    assert!(
                        actual.contains(reason),
                        "{label} must name {reason:?}: {actual}"
                    );
                }
                other => panic!("{label} must be typed Corrupt, got {other:?}"),
            }
        }
    }

    #[cfg(target_pointer_width = "64")]
    #[test]
    fn recent_recall_misses_rejects_unrepresentable_limit() {
        let store = Store::open_in_memory().unwrap();
        let err = store.recent_recall_misses(usize::MAX).unwrap_err();
        assert!(
            matches!(err, StoreError::Backend(ref message) if message.contains("limit")),
            "usize-to-SQL limit conversion is checked: {err:?}"
        );
    }

    #[test]
    fn recall_receipts_round_trip_and_sequence() {
        let mut store = Store::open_in_memory().unwrap();
        let first_terms = vec![
            "  SQLite  ".to_string(),
            "sync".to_string(),
            "sync".to_string(),
        ];
        let first = store
            .record_recall_receipt(
                &first_terms,
                &["cap-2", "cap-1"],
                Some("nott"),
                Some("nott/sub"),
                None,
                injected_now(),
            )
            .unwrap();
        let second_terms = vec!["next".to_string()];
        let second = store
            .record_recall_receipt(
                &second_terms,
                &["cap-3"],
                None,
                None,
                Some("sess-9"),
                later_now(),
            )
            .unwrap();

        assert_eq!(first, "rcpt-1");
        assert_eq!(second, "rcpt-2");
        assert_eq!(
            store.receipt_returned_ids(&first).unwrap(),
            Some(vec!["cap-2".to_string(), "cap-1".to_string()]),
            "returned ids round-trip in response order"
        );
        assert_eq!(
            store.receipt_returned_ids(&second).unwrap(),
            Some(vec!["cap-3".to_string()])
        );
        assert_eq!(store.receipt_returned_ids("rcpt-99").unwrap(), None);

        let rows = {
            let mut stmt = store
                .conn
                .prepare(
                    "SELECT terms, returned_ids, project_id, project_prefix, session_id, at \
                     FROM recall_receipts ORDER BY seq",
                )
                .unwrap();
            stmt.query_map([], |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, Option<String>>(2)?,
                    row.get::<_, Option<String>>(3)?,
                    row.get::<_, Option<String>>(4)?,
                    row.get::<_, String>(5)?,
                ))
            })
            .unwrap()
            .map(Result::unwrap)
            .collect::<Vec<_>>()
        };
        assert_eq!(rows.len(), 2);
        assert_eq!(rows[0].0, serde_json::to_string(&first_terms).unwrap());
        assert_eq!(rows[0].1, r#"["cap-2","cap-1"]"#);
        assert_eq!(rows[0].2.as_deref(), Some("nott"));
        assert_eq!(rows[0].3.as_deref(), Some("nott/sub"));
        assert_eq!(rows[0].4, None);
        assert_eq!(rows[0].5, rfc3339_text(injected_now()).unwrap());
        assert_eq!(rows[1].0, serde_json::to_string(&second_terms).unwrap());
        assert_eq!(rows[1].2, None);
        assert_eq!(rows[1].3, None);
        assert_eq!(rows[1].4.as_deref(), Some("sess-9"));
        assert_eq!(rows[1].5, rfc3339_text(later_now()).unwrap());
    }

    #[test]
    fn record_recall_receipt_fails_typed_when_the_ledger_is_missing() {
        let mut store = Store::open_in_memory().unwrap();
        store
            .conn
            .execute_batch("DROP TABLE recall_receipts")
            .unwrap();
        let err = store
            .record_recall_receipt(
                &["sqlite".to_string()],
                &["cap-1"],
                None,
                None,
                None,
                injected_now(),
            )
            .unwrap_err();
        assert!(
            matches!(err, StoreError::Backend(_)),
            "a missing receipt ledger is a typed backend error, never a phantom id: {err:?}"
        );
    }

    #[test]
    fn receipt_returned_ids_rejects_corrupt_json() {
        let store = Store::open_in_memory().unwrap();
        store
            .conn
            .execute(
                "INSERT INTO recall_receipts \
                 (seq, id, terms, returned_ids, project_id, project_prefix, session_id, at) \
                 VALUES (1, 'rcpt-1', '[]', 'not-json', NULL, NULL, NULL, ?1)",
                [rfc3339_text(injected_now()).unwrap()],
            )
            .unwrap();

        let err = store.receipt_returned_ids("rcpt-1").unwrap_err();
        assert!(
            matches!(
                err,
                StoreError::Corrupt { ref id, ref reason }
                    if id == "rcpt-1" && reason.contains("recall_receipts.returned_ids")
            ),
            "malformed returned_ids must fail as the named corrupt receipt: {err:?}"
        );
    }

    #[test]
    fn feedback_ema_folds_toward_the_score_and_clamps() {
        let mut store = Store::open_in_memory().unwrap();

        let first = store
            .apply_feedback(&["cap-1"], 1.0, injected_now())
            .unwrap();
        assert_eq!(first.len(), 1);
        assert!((first[0].1 - 0.55).abs() < f64::EPSILON);

        let second = store.apply_feedback(&["cap-1"], 0.0, later_now()).unwrap();
        assert!((second[0].1 - 0.495).abs() < f64::EPSILON);

        let mut previous = second[0].1;
        for _ in 0..100 {
            let next = store.apply_feedback(&["cap-1"], 1.0, later_now()).unwrap()[0].1;
            assert!(next >= previous, "EMA toward 1.0 must not decrease");
            assert!(next <= 1.0, "EMA must stay clamped to 1.0");
            previous = next;
        }
        assert_eq!(store.feedback_weight_of("cap-1").unwrap(), Some(previous));
    }

    #[test]
    fn feedback_weight_reads_and_updates_reject_corrupt_persisted_values() {
        let mut store = Store::open_in_memory().unwrap();
        store
            .conn
            .execute(
                "INSERT INTO feedback_weights (capsule_id, weight, at) VALUES ('cap-1', 1.5, ?1)",
                [rfc3339_text(injected_now()).unwrap()],
            )
            .unwrap();
        assert!(matches!(
            store.feedback_weight_of("cap-1").unwrap_err(),
            StoreError::Corrupt { ref id, .. } if id == "cap-1"
        ));
        assert!(matches!(
            store
                .apply_feedback(&["cap-1"], 1.0, later_now())
                .unwrap_err(),
            StoreError::Corrupt { ref id, .. } if id == "cap-1"
        ));
        let unchanged: f64 = store
            .conn
            .query_row(
                "SELECT weight FROM feedback_weights WHERE capsule_id = 'cap-1'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(unchanged, 1.5);
    }

    #[test]
    fn scored_outcome_rolls_back_row_and_prior_weights_when_feedback_fails() {
        let mut store = Store::open_in_memory().unwrap();
        store
            .append(&capsule("atomic feedback first", "nott"), injected_now())
            .unwrap();
        store
            .append(&capsule("atomic feedback second", "nott"), injected_now())
            .unwrap();
        let receipt = store
            .record_recall_receipt(
                &["atomic".to_string()],
                &["cap-1", "cap-2"],
                Some("nott"),
                None,
                None,
                injected_now(),
            )
            .unwrap();
        store
            .conn
            .execute_batch(
                "CREATE TRIGGER fail_second_feedback
                 BEFORE INSERT ON feedback_weights
                 WHEN NEW.capsule_id = 'cap-2'
                 BEGIN
                     SELECT RAISE(ABORT, 'forced feedback failure');
                 END;",
            )
            .unwrap();

        let err = store
            .append_outcome(
                "scored atomically",
                "tester",
                None,
                None,
                Some(&receipt),
                Some(1.0),
                later_now(),
            )
            .unwrap_err();
        assert!(matches!(err, StoreError::Backend(_)));
        assert!(store.list_outcomes().unwrap().is_empty());
        assert_eq!(store.feedback_weight_of("cap-1").unwrap(), None);
        assert_eq!(store.feedback_weight_of("cap-2").unwrap(), None);
    }

    #[test]
    fn scored_outcome_updates_every_receipt_member_in_response_order() {
        let mut store = Store::open_in_memory().unwrap();
        for content in ["ordered first", "ordered second"] {
            store
                .append(&capsule(content, "nott"), injected_now())
                .unwrap();
        }
        store
            .apply_feedback(&["cap-1"], 0.0, injected_now())
            .unwrap(); // cap-1 starts the scored outcome at 0.45
        let receipt = store
            .record_recall_receipt(
                &["ordered".to_string()],
                &["cap-2", "cap-1"],
                None,
                None,
                None,
                injected_now(),
            )
            .unwrap();

        let applied = store
            .append_outcome(
                "ordered recall was useful",
                "tester",
                None,
                None,
                Some(&receipt),
                Some(1.0),
                later_now(),
            )
            .unwrap();
        assert_eq!(
            applied.weights_updated,
            Some(vec![
                ("cap-2".to_string(), 0.55),
                ("cap-1".to_string(), 0.505),
            ])
        );
        assert_eq!(store.list_outcomes().unwrap(), vec![applied.record]);
    }

    #[test]
    fn scored_outcome_rejects_unknown_and_corrupt_receipts_without_effects() {
        let mut store = Store::open_in_memory().unwrap();
        let unknown = store
            .append_outcome(
                "unknown receipt",
                "tester",
                None,
                None,
                Some("rcpt-404"),
                Some(0.5),
                injected_now(),
            )
            .unwrap_err();
        assert_eq!(unknown, StoreError::UnknownReceipt("rcpt-404".to_string()));
        assert!(store.list_outcomes().unwrap().is_empty());

        store
            .conn
            .execute(
                "INSERT INTO recall_receipts
                 (seq, id, terms, returned_ids, project_id, project_prefix, session_id, at)
                 VALUES (1, 'rcpt-1', '[]', 'not-json', NULL, NULL, NULL, ?1)",
                [rfc3339_text(injected_now()).unwrap()],
            )
            .unwrap();
        let corrupt = store
            .append_outcome(
                "corrupt receipt",
                "tester",
                None,
                None,
                Some("rcpt-1"),
                Some(0.5),
                injected_now(),
            )
            .unwrap_err();
        assert!(matches!(corrupt, StoreError::Corrupt { ref id, .. } if id == "rcpt-1"));
        assert!(store.list_outcomes().unwrap().is_empty());

        store
            .conn
            .execute(
                "INSERT INTO recall_receipts
                 (seq, id, terms, returned_ids, project_id, project_prefix, session_id, at)
                 VALUES (2, 'rcpt-2', '[]', '[\"cap-404\"]', NULL, NULL, NULL, ?1)",
                [rfc3339_text(injected_now()).unwrap()],
            )
            .unwrap();
        let orphan = store
            .append_outcome(
                "orphan receipt member",
                "tester",
                None,
                None,
                Some("rcpt-2"),
                Some(0.5),
                injected_now(),
            )
            .unwrap_err();
        assert!(matches!(orphan, StoreError::Corrupt { ref id, .. } if id == "rcpt-2"));
        assert!(store.list_outcomes().unwrap().is_empty());
        assert_eq!(store.feedback_weight_of("cap-404").unwrap(), None);

        store
            .append(&capsule("duplicate receipt member", "nott"), injected_now())
            .unwrap();
        store
            .conn
            .execute(
                "INSERT INTO recall_receipts
                 (seq, id, terms, returned_ids, project_id, project_prefix, session_id, at)
                 VALUES (3, 'rcpt-3', '[]', '[\"cap-1\",\"cap-1\"]', NULL, NULL, NULL, ?1)",
                [rfc3339_text(injected_now()).unwrap()],
            )
            .unwrap();
        let duplicate = store
            .append_outcome(
                "duplicate receipt member",
                "tester",
                None,
                None,
                Some("rcpt-3"),
                Some(1.0),
                injected_now(),
            )
            .unwrap_err();
        assert!(matches!(duplicate, StoreError::Corrupt { ref id, .. } if id == "rcpt-3"));
        assert!(store.list_outcomes().unwrap().is_empty());
        assert_eq!(store.feedback_weight_of("cap-1").unwrap(), None);
    }

    #[test]
    fn empty_receipt_atomically_appends_scored_outcome_with_no_weight_updates() {
        let mut store = Store::open_in_memory().unwrap();
        let receipt = store
            .record_recall_receipt(
                &["count-only".to_string()],
                &[],
                None,
                None,
                None,
                injected_now(),
            )
            .unwrap();
        let applied = store
            .append_outcome(
                "count-only recall was useful",
                "tester",
                None,
                None,
                Some(&receipt),
                Some(1.0),
                later_now(),
            )
            .unwrap();
        assert_eq!(applied.weights_updated, Some(Vec::new()));
        assert_eq!(applied.record.receipt_id.as_deref(), Some("rcpt-1"));
        assert_eq!(applied.record.score, Some(1.0));
        assert_eq!(store.list_outcomes().unwrap(), vec![applied.record]);
    }

    /// u-r5: the store method surfaces a broken ledger HONESTLY (typed
    /// `Err`) — the fail-open swallow lives in [`crate::retrieve`], never
    /// here. Dropping the table makes the next append a backend error.
    #[test]
    fn record_recall_miss_surfaces_a_broken_ledger_honestly() {
        let mut store = Store::open_in_memory().unwrap();
        store
            .conn
            .execute_batch("DROP TABLE recall_misses")
            .unwrap();
        let err = store
            .record_recall_miss(
                &["tokio".to_string()],
                RecallMissOutcome::Abstain,
                injected_now(),
            )
            .unwrap_err();
        assert!(
            matches!(err, StoreError::Backend(_)),
            "a missing ledger table is a backend error, not a silent success"
        );
    }

    /// u-r2: the capture-time anchor hash is keep-first (the capture
    /// instant is the only honest comparison base — a re-record is a
    /// no-op keeping the FIRST row), and it rejects unknown capsules and
    /// empty hashes.
    #[test]
    fn anchor_hash_is_keep_first_and_fails_closed() {
        let mut store = Store::open_in_memory().unwrap();
        store
            .append(&capsule("anchored claim", "nott"), injected_now())
            .unwrap(); // cap-1

        // Nothing recorded yet: the honest None, never a guess.
        assert_eq!(store.anchor_hash_of("cap-1").unwrap(), None);

        let first = sha256_hex(b"capture bytes");
        assert!(
            store
                .set_anchor_hash("cap-1", &first, injected_now())
                .unwrap()
        );
        // Keep-first: the second write is a no-op, the FIRST hash stays.
        assert!(
            !store
                .set_anchor_hash("cap-1", &sha256_hex(b"later bytes"), later_now())
                .unwrap()
        );
        assert_eq!(store.anchor_hash_of("cap-1").unwrap(), Some(first));

        // Unknown capsule / empty hash: typed rejections, nothing stored.
        assert_eq!(
            store
                .set_anchor_hash("cap-99", "deadbeef", injected_now())
                .unwrap_err(),
            StoreError::UnknownCapsule("cap-99".to_string())
        );
        assert_eq!(
            store
                .set_anchor_hash("cap-1", "  ", injected_now())
                .unwrap_err(),
            StoreError::EmptyField("anchor hash")
        );
    }

    /// u-r2: epistemics merge PER FIELD — setting one field never erases a
    /// sibling — an all-`None` call records nothing, an out-of-set
    /// `evidence_state` is the teaching rejection naming the closed set,
    /// and an unknown capsule fails closed.
    #[test]
    fn epistemics_merge_per_field_and_teach_the_closed_set() {
        let mut store = Store::open_in_memory().unwrap();
        store
            .append(&capsule("epistemic claim", "nott"), injected_now())
            .unwrap(); // cap-1

        // All-None records nothing — no phantom row.
        store
            .set_epistemics("cap-1", None, None, None, injected_now())
            .unwrap();
        assert_eq!(store.epistemics_of("cap-1").unwrap(), None);

        // First write: evidence_state only.
        store
            .set_epistemics("cap-1", Some("inferred"), None, None, injected_now())
            .unwrap();
        // Second write: proof_hint only — the state must SURVIVE.
        store
            .set_epistemics(
                "cap-1",
                None,
                Some("cargo test -p nmemory"),
                None,
                later_now(),
            )
            .unwrap();
        // Third write: stale_if only — both siblings survive.
        store
            .set_epistemics(
                "cap-1",
                None,
                None,
                Some("store.rs schema changes"),
                later_now(),
            )
            .unwrap();
        let record = store.epistemics_of("cap-1").unwrap().unwrap();
        assert_eq!(record.evidence_state.as_deref(), Some("inferred"));
        assert_eq!(record.proof_hint.as_deref(), Some("cargo test -p nmemory"));
        assert_eq!(record.stale_if.as_deref(), Some("store.rs schema changes"));

        // A re-set REPLACES the named field (merge, not append).
        store
            .set_epistemics("cap-1", Some("observed"), None, None, later_now())
            .unwrap();
        let record = store.epistemics_of("cap-1").unwrap().unwrap();
        assert_eq!(record.evidence_state.as_deref(), Some("observed"));
        assert_eq!(record.proof_hint.as_deref(), Some("cargo test -p nmemory"));

        // Outside the closed set: the teaching rejection names ALL members.
        let err = store
            .set_epistemics("cap-1", Some("guessed"), None, None, injected_now())
            .unwrap_err();
        let message = err.to_string();
        assert!(
            message.contains("\"observed\"")
                && message.contains("\"inferred\"")
                && message.contains("\"unverified\""),
            "closed set taught in full: {message}"
        );

        // Unknown capsule: fail closed, nothing recorded.
        assert_eq!(
            store
                .set_epistemics("cap-9", Some("observed"), None, None, injected_now())
                .unwrap_err(),
            StoreError::UnknownCapsule("cap-9".to_string())
        );
    }

    /// q119 RED: the one-embedder-per-store law is MECHANICAL — a second
    /// `model_tag` at the SAME dimension is refused naming the resident
    /// tag (the dimensional fence cannot tell two same-width model spaces
    /// apart, and cross-space cosine silently poisons RRF). Same-tag
    /// replace-on-write and sibling attaches stay legal.
    #[test]
    fn second_model_tag_same_dimension_is_refused_naming_the_resident() {
        let mut store = Store::open_in_memory().unwrap();
        store
            .append(&capsule("alpha vector", "nott"), injected_now())
            .unwrap(); // cap-1
        store
            .append(&capsule("beta vector", "nott"), injected_now())
            .unwrap(); // cap-2
        assert!(
            store
                .put_embedding("cap-1", &[0.1, 0.2], "e5-base", injected_now())
                .unwrap()
        );
        // Same tag: replace-on-write and a sibling attach stay legal.
        assert!(
            !store
                .put_embedding("cap-1", &[0.3, 0.4], "e5-base", injected_now())
                .unwrap()
        );
        assert!(
            store
                .put_embedding("cap-2", &[0.5, 0.6], "e5-base", injected_now())
                .unwrap()
        );
        // A DIFFERENT tag at the same dimension: refused, resident named.
        let err = store
            .put_embedding("cap-2", &[0.7, 0.8], "bge-base", injected_now())
            .unwrap_err();
        match err {
            StoreError::InvalidEmbedding(msg) => {
                assert!(
                    msg.contains("bge-base") && msg.contains("e5-base"),
                    "both tags named: {msg}"
                );
            }
            other => panic!("expected InvalidEmbedding, got {other:?}"),
        }
    }

    // ---- store-merge (u2): the imperative apply shell over the pure core ----

    /// A capsule whose IDENTITY is its content, with an EXPLICIT authority
    /// class + taint flag — so a merge test can prove foreign bytes ride
    /// through unelevated.
    fn capsule_authority(
        text: &str,
        project: &str,
        authority: AuthorityClass,
        taint: bool,
    ) -> Capsule {
        Capsule::new(
            text.to_string(),
            Provenance {
                source: "session:2026-07-18".to_string(),
                anchor: "PLAN.md:67".to_string(),
                source_hash: sha256_hex(text.as_bytes()),
            },
            Confidence::new(0.9).unwrap(),
            Freshness {
                valid_from: datetime!(2026-07-18 12:30:45 UTC),
                valid_to: None,
            },
            Scope {
                project_id: project.to_string(),
            },
            authority,
            taint,
        )
        .unwrap()
    }

    /// Distinct content on both sides UNIONS: incoming capsules are minted
    /// fresh ids after LOCAL's ceiling, incoming edges rewrite into LOCAL id
    /// space, and LOCAL's own rows are untouched.
    #[test]
    fn merge_distinct_content_unions_capsules_and_relations() {
        let dir = tempfile::tempdir().unwrap();
        let local_path = dir.path().join("local.sqlite3");
        let incoming_path = dir.path().join("incoming.sqlite3");
        {
            let mut local = Store::open(&local_path).unwrap();
            local
                .append(&capsule("local-A", "nott"), injected_now())
                .unwrap(); // cap-1
            local
                .append(&capsule("local-B", "nott"), injected_now())
                .unwrap(); // cap-2
            local
                .upsert_relation(RelationKind::Blocks, "cap-1", "cap-2", injected_now())
                .unwrap();
        }
        {
            let mut inc = Store::open(&incoming_path).unwrap();
            inc.append(&capsule("incoming-C", "nott"), injected_now())
                .unwrap(); // cap-1
            inc.append(&capsule("incoming-D", "nott"), injected_now())
                .unwrap(); // cap-2
            inc.upsert_relation(RelationKind::Supersedes, "cap-1", "cap-2", injected_now())
                .unwrap();
        } // dropped -> checkpointed, closed

        let mut local = Store::open(&local_path).unwrap();
        let summary = local
            .merge_from(&incoming_path, b"local-key")
            .unwrap()
            .summary;
        assert_eq!(summary.capsules_added, 2);
        assert_eq!(summary.capsules_collapsed, 0);
        assert_eq!(summary.relations_added, 1);
        assert_eq!(summary.tombstones_applied, 0);
        assert_eq!(summary.id_remap_size, 2);

        let live = local.list(ListFilter::default()).unwrap();
        let contents: Vec<&str> = live.iter().map(|s| s.capsule.content()).collect();
        assert_eq!(contents, ["local-A", "local-B", "incoming-C", "incoming-D"]);
        assert_eq!(live[2].id.as_str(), "cap-3");
        assert_eq!(live[3].id.as_str(), "cap-4");
        let edges = local.all_relations().unwrap();
        // The incoming edge is rewritten into LOCAL id space (cap-3 -> cap-4).
        assert!(edges.iter().any(|e| e.kind == RelationKind::Supersedes
            && e.from_id == "cap-3"
            && e.to_id == "cap-4"));
        // LOCAL's own edge survives.
        assert!(
            edges.iter().any(|e| e.kind == RelationKind::Blocks
                && e.from_id == "cap-1"
                && e.to_id == "cap-2")
        );

        // Determinism: a second identical merge is a pure no-op (all content
        // already present -> everything collapses, nothing added).
        let again = local
            .merge_from(&incoming_path, b"local-key")
            .unwrap()
            .summary;
        assert_eq!(again.capsules_added, 0);
        assert_eq!(again.capsules_collapsed, 2);
        assert_eq!(again.relations_added, 0);
        assert_eq!(local.list(ListFilter::default()).unwrap().len(), 4);
    }

    /// Overlapping content COLLAPSES by `source_hash` (no duplicate), and a
    /// foreign capsule's authority + taint ride through UNELEVATED.
    #[test]
    fn merge_overlapping_content_collapses_and_carries_taint_unelevated() {
        let dir = tempfile::tempdir().unwrap();
        let local_path = dir.path().join("local.sqlite3");
        let incoming_path = dir.path().join("incoming.sqlite3");
        {
            let mut local = Store::open(&local_path).unwrap();
            local
                .append(&capsule("shared", "nott"), injected_now())
                .unwrap(); // cap-1
        }
        {
            let mut inc = Store::open(&incoming_path).unwrap();
            inc.append(&capsule("shared", "nott"), injected_now())
                .unwrap(); // cap-1, collapses
            inc.append(
                &capsule_authority(
                    "foreign-tainted",
                    "nott",
                    AuthorityClass::ExternallyImported,
                    true,
                ),
                injected_now(),
            )
            .unwrap(); // cap-2, new + tainted
        }

        let mut local = Store::open(&local_path).unwrap();
        let summary = local
            .merge_from(&incoming_path, b"local-key")
            .unwrap()
            .summary;
        assert_eq!(summary.capsules_added, 1);
        assert_eq!(summary.capsules_collapsed, 1);
        assert_eq!(summary.id_remap_size, 2);

        let live = local.list(ListFilter::default()).unwrap();
        assert_eq!(live.len(), 2, "shared did not duplicate");
        let foreign = local.get("cap-2").unwrap().unwrap();
        assert_eq!(foreign.capsule.content(), "foreign-tainted");
        assert_eq!(
            foreign.capsule.authority_class(),
            AuthorityClass::ExternallyImported,
            "authority is never elevated on merge"
        );
        assert!(
            foreign.capsule.instruction_taint(),
            "foreign taint is carried through, never scrubbed"
        );
    }

    /// A forget on the INCOMING store propagates by `source_hash` to the LIVE
    /// LOCAL capsule of the same content: the local content is destroyed and
    /// its marker is re-keyed under LOCAL's key.
    #[test]
    fn merge_propagates_forget_by_source_hash_to_a_live_local_capsule() {
        let dir = tempfile::tempdir().unwrap();
        let local_path = dir.path().join("local.sqlite3");
        let incoming_path = dir.path().join("incoming.sqlite3");
        {
            let mut local = Store::open(&local_path).unwrap();
            local
                .append(&capsule("secret-to-forget", "nott"), injected_now())
                .unwrap(); // cap-1
            local
                .append(&capsule("local-keeps-this", "nott"), injected_now())
                .unwrap(); // cap-2
        }
        {
            let mut inc = Store::open(&incoming_path).unwrap();
            inc.append(&capsule("secret-to-forget", "nott"), injected_now())
                .unwrap(); // cap-1
            inc.forget_capsule(
                "cap-1",
                TombstoneMode::Purged,
                "forgotten upstream",
                b"incoming-key",
                later_now(),
            )
            .unwrap();
        }

        let mut local = Store::open(&local_path).unwrap();
        assert_eq!(
            local.get("cap-1").unwrap().unwrap().capsule.content(),
            "secret-to-forget"
        );

        let summary = local
            .merge_from(&incoming_path, b"local-key")
            .unwrap()
            .summary;
        assert_eq!(summary.tombstones_applied, 1);
        assert_eq!(
            summary.capsules_added, 0,
            "the incoming forgotten row is not a live capsule to add"
        );

        // The LOCAL capsule is now forgotten (content gone), matched by content.
        match local.get("cap-1") {
            Err(StoreError::Tombstoned { id }) => assert_eq!(id, "cap-1"),
            other => panic!("expected cap-1 tombstoned, got {other:?}"),
        }
        // Its content is unfindable (recall index emptied).
        assert!(
            local
                .search_fts(&["secret".to_string()], None)
                .unwrap()
                .is_empty()
        );
        // The marker is re-keyed under LOCAL's key and records source_hash.
        let marker = local.get_tombstone("cap-1").unwrap().unwrap();
        assert_eq!(
            marker.source_hash.as_deref(),
            Some(sha256_hex(b"secret-to-forget").as_str())
        );
        assert_eq!(
            marker.content_hmac,
            content_hmac_hex(b"local-key", "cap-1", "secret-to-forget"),
            "re-keyed under LOCAL's key, not the incoming store's"
        );
        // The other local capsule is untouched.
        assert!(local.get("cap-2").unwrap().is_some());
    }

    /// A missing or corrupt (non-store) source fails CLOSED with a typed
    /// error and leaves LOCAL byte-for-byte unchanged (no partial write).
    #[test]
    fn merge_from_bad_paths_fail_closed_without_partial_write() {
        let dir = tempfile::tempdir().unwrap();
        let local_path = dir.path().join("local.sqlite3");
        let mut local = Store::open(&local_path).unwrap();
        local
            .append(&capsule("local-A", "nott"), injected_now())
            .unwrap();
        let before = local.canonical_snapshot().unwrap();

        // Missing path -> typed backend error, LOCAL untouched.
        let missing = dir.path().join("nope.sqlite3");
        let err = local.merge_from(&missing, b"local-key").unwrap_err();
        assert!(
            matches!(err, StoreError::Backend(_)),
            "missing path is a typed backend error: {err:?}"
        );
        assert_eq!(
            local.canonical_snapshot().unwrap(),
            before,
            "missing source: no partial write"
        );

        // Corrupt (non-SQLite) file -> typed error, LOCAL untouched.
        let corrupt = dir.path().join("corrupt.sqlite3");
        std::fs::write(&corrupt, b"this is not a sqlite database at all").unwrap();
        let err = local.merge_from(&corrupt, b"local-key").unwrap_err();
        assert!(
            matches!(err, StoreError::Backend(_) | StoreError::Corrupt { .. }),
            "corrupt file is a typed error: {err:?}"
        );
        assert_eq!(
            local.canonical_snapshot().unwrap(),
            before,
            "corrupt source: no partial write"
        );
    }

    /// A v10 file (no `tombstones.source_hash`) opens and upgrades to current:
    /// the column arrives NULL-backfilled for the pre-existing marker, and a
    /// fresh forget records it going forward.
    #[test]
    fn v10_file_migrates_to_current_and_gains_tombstone_source_hash() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("memory.sqlite3");
        {
            let mut store = Store::open(&path).unwrap();
            store
                .append(&capsule("to-forget", "nott"), injected_now())
                .unwrap(); // cap-1
            store
                .append(&capsule("still-here", "nott"), injected_now())
                .unwrap(); // cap-2
            store
                .forget_capsule("cap-1", TombstoneMode::Purged, "gone", b"k", later_now())
                .unwrap();
        }
        // Downgrade to a FAITHFUL v10 shape: drop the v11 column, stamp 10.
        {
            let conn = rusqlite::Connection::open(&path).unwrap();
            conn.execute_batch(
                "ALTER TABLE tombstones DROP COLUMN source_hash; PRAGMA user_version = 10;",
            )
            .unwrap();
        }

        // Opening IS the migration — v10 is an enumerated migratable version.
        let mut store = Store::open(&path).unwrap();
        let version: i64 = store
            .conn
            .query_row("PRAGMA user_version", [], |r| r.get(0))
            .unwrap();
        assert_eq!(
            version, SCHEMA_VERSION,
            "v10 re-stamped to the current version"
        );
        assert_eq!(SCHEMA_VERSION, 20);

        // The pre-v11 marker survives; source_hash backfilled NULL (cannot
        // propagate by content, which is acceptable).
        let old = store.get_tombstone("cap-1").unwrap().unwrap();
        assert_eq!(old.source_hash, None);
        assert_eq!(old.reason, "gone");
        // Capsule bytes untouched by the migration.
        assert!(store.get("cap-2").unwrap().is_some());

        // A fresh forget now records source_hash going forward.
        store
            .forget_capsule(
                "cap-2",
                TombstoneMode::Purged,
                "gone too",
                b"k",
                later_now(),
            )
            .unwrap();
        let fresh = store.get_tombstone("cap-2").unwrap().unwrap();
        assert_eq!(
            fresh.source_hash.as_deref(),
            Some(sha256_hex(b"still-here").as_str())
        );
    }

    #[test]
    fn event_time_append_is_atomic_with_capsule_and_fts() {
        let mut store = Store::open_in_memory().unwrap();
        let range = EventTimeRange::new(
            datetime!(2026-01-02 03:04:05 UTC),
            datetime!(2026-01-03 03:04:05 UTC),
        )
        .unwrap();
        store
            .conn
            .execute_batch(
                "CREATE TRIGGER reject_event_time BEFORE INSERT ON event_time
                 BEGIN SELECT RAISE(ABORT, 'injected event-time failure'); END;",
            )
            .unwrap();

        let err = store
            .append_with_event_time(
                &capsule("atomic event time", "nott"),
                &range,
                injected_now(),
            )
            .unwrap_err();
        assert!(matches!(err, StoreError::Backend(_)));
        for table in ["capsules", "capsules_fts", "event_time"] {
            let count: i64 = store
                .conn
                .query_row(&format!("SELECT COUNT(*) FROM {table}"), [], |row| {
                    row.get(0)
                })
                .unwrap();
            assert_eq!(count, 0, "{table} must roll back with the sidecar insert");
        }

        store
            .conn
            .execute_batch("DROP TRIGGER reject_event_time")
            .unwrap();
        let id = store
            .append_with_event_time(
                &capsule("atomic event time", "nott"),
                &range,
                injected_now(),
            )
            .unwrap();
        assert_eq!(id.as_str(), "cap-1", "the failed append consumed no id");
        assert_eq!(
            store.event_time_of("cap-1").unwrap().unwrap().event_from(),
            datetime!(2026-01-02 03:04:05 UTC)
        );
    }

    #[test]
    fn event_time_append_with_session_links_both_sidecars() {
        let mut store = Store::open_in_memory().unwrap();
        let range = EventTimeRange::new(injected_now(), later_now()).unwrap();
        store.open_session("sess-event", injected_now()).unwrap();
        let id = store
            .append_with_session_and_event_time(
                &capsule("session event capture", "nott"),
                "sess-event",
                &range,
                later_now(),
            )
            .unwrap();
        let stored = store.get(id.as_str()).unwrap().unwrap();
        assert_eq!(stored.session_id.as_deref(), Some("sess-event"));
        let event = store.event_time_of(id.as_str()).unwrap().unwrap();
        assert_eq!(event.event_from(), range.event_from());
        assert_eq!(event.event_to(), range.event_to());
        assert_eq!(event.declared_at(), later_now());
    }

    #[test]
    fn event_time_reader_revalidates_every_timestamp_and_range_direction() {
        let mut store = Store::open_in_memory().unwrap();
        let range = EventTimeRange::new(
            datetime!(2026-01-02 03:04:05 UTC),
            datetime!(2026-01-03 03:04:05 UTC),
        )
        .unwrap();
        store
            .append_with_event_time(&capsule("event decode", "nott"), &range, injected_now())
            .unwrap();
        let good_from = rfc3339_text(range.event_from()).unwrap();
        let good_to = rfc3339_text(range.event_to()).unwrap();
        let good_declared = rfc3339_text(injected_now()).unwrap();

        for (column, reset) in [
            ("event_from", good_from.as_str()),
            ("event_to", good_to.as_str()),
            ("declared_at", good_declared.as_str()),
        ] {
            store
                .conn
                .execute(
                    &format!(
                        "UPDATE event_time SET {column} = 'not-rfc3339' WHERE capsule_id = 'cap-1'"
                    ),
                    [],
                )
                .unwrap();
            let err = store.event_time_of("cap-1").unwrap_err();
            assert!(
                matches!(&err, StoreError::Corrupt { id, reason }
                    if id == "cap-1" && reason.contains(column)),
                "{column} corruption must be named: {err:?}"
            );
            store
                .conn
                .execute(
                    &format!("UPDATE event_time SET {column} = ?1 WHERE capsule_id = 'cap-1'"),
                    [reset],
                )
                .unwrap();
        }

        store
            .conn
            .execute(
                "UPDATE event_time SET event_from = ?1, event_to = ?2 WHERE capsule_id = 'cap-1'",
                params![good_to, good_from],
            )
            .unwrap();
        let err = store.event_time_of("cap-1").unwrap_err();
        assert!(
            matches!(&err, StoreError::Corrupt { id, reason }
                if id == "cap-1" && reason.contains("event_to") && reason.contains("before")),
            "backwards persisted ranges fail typed: {err:?}"
        );
    }

    #[test]
    fn v14_file_migrates_to_current_without_moving_capsules_or_lane_totals() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("memory.sqlite3");
        let (snapshot, lane_totals) = {
            let mut store = Store::open(&path).unwrap();
            store
                .append(&capsule("v14 event migration", "nott"), injected_now())
                .unwrap();
            store
                .record_lane_override(LaneOverride::TermOverFused, later_now())
                .unwrap();
            (
                store.canonical_snapshot().unwrap(),
                store.lane_override_totals().unwrap(),
            )
        };
        {
            let conn = rusqlite::Connection::open(&path).unwrap();
            conn.execute_batch("DROP TABLE event_time; PRAGMA user_version = 14;")
                .unwrap();
        }

        let mut store = Store::open(&path).unwrap();
        let version: i64 = store
            .conn
            .query_row("PRAGMA user_version", [], |row| row.get(0))
            .unwrap();
        assert_eq!(version, SCHEMA_VERSION);
        assert_eq!(SCHEMA_VERSION, 20);
        assert_eq!(store.canonical_snapshot().unwrap(), snapshot);
        assert_eq!(store.lane_override_totals().unwrap(), lane_totals);
        assert_eq!(store.event_time_of("cap-1").unwrap(), None);
        let event = EventTimeRange::new(injected_now(), later_now()).unwrap();
        let id = store
            .append_with_event_time(
                &capsule("post-v14 event capture", "nott"),
                &event,
                later_now(),
            )
            .unwrap();
        assert_eq!(id.as_str(), "cap-2");
    }

    /// planning-plane u1 MIGRATION v15→current: a faithful v15 file —
    /// relations under the FIVE-kind CHECK (no `grounded_in`), `origin`
    /// column present, `user_version = 15` — migrates IN PLACE on open.
    /// The origin-tagged edge survives the CHECK rebuild byte-true, the
    /// relations CHECK gains `grounded_in`, and the stamp advances to the
    /// current version. Mirrors the v4→current relations-CHECK-widen test
    /// (no-drift discipline).
    #[test]
    fn v15_five_kind_relations_widen_and_keep_origin() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("memory.sqlite3");
        {
            let mut store = Store::open(&path).unwrap();
            store
                .append(
                    &capsule("planning plane claim alpha", "nott"),
                    injected_now(),
                )
                .unwrap(); // cap-1
            store
                .append(
                    &capsule("planning plane claim beta", "nott"),
                    injected_now(),
                )
                .unwrap(); // cap-2
            store
                .append(
                    &capsule("planning plane claim gamma", "nott"),
                    injected_now(),
                )
                .unwrap(); // cap-3
            // A legacy edge from the five-kind era — must survive the rebuild.
            store
                .upsert_relation(RelationKind::Blocks, "cap-1", "cap-2", injected_now())
                .unwrap();
            // An origin='import' edge — must survive the rebuild byte-true.
            store
                .upsert_relation_origin(
                    RelationKind::Supersedes,
                    "cap-3",
                    "cap-2",
                    injected_now(),
                    RelationOrigin::Import,
                )
                .unwrap();
        }
        // Downgrade to a FAITHFUL v15 shape: five-kind relations CHECK (no
        // grounded_in), origin column present, stamp 15.
        {
            let conn = rusqlite::Connection::open(&path).unwrap();
            conn.execute_batch(
                "CREATE TABLE relations_v15 (
                     kind    TEXT NOT NULL CHECK (kind IN ('supersedes', 'derived_from', 'witnesses', 'blocks', 'falsifies')),
                     from_id TEXT NOT NULL,
                     to_id   TEXT NOT NULL,
                     at      TEXT NOT NULL,
                     origin  TEXT NOT NULL DEFAULT 'manual' CHECK (origin IN ('manual', 'import')),
                     PRIMARY KEY (kind, from_id, to_id)
                 );
                 INSERT INTO relations_v15 (kind, from_id, to_id, at, origin) \
                     SELECT kind, from_id, to_id, at, origin FROM relations;
                 DROP TABLE relations;
                 ALTER TABLE relations_v15 RENAME TO relations;
                 PRAGMA user_version = 15;",
            )
            .unwrap();
            // Faithful: the v15 five-kind CHECK rejects a raw 'grounded_in' edge.
            let raw = conn.execute(
                "INSERT INTO relations (kind, from_id, to_id, at, origin) \
                 VALUES ('grounded_in', 'cap-1', 'cap-2', '2026-07-24T00:00:00Z', 'manual')",
                [],
            );
            assert!(
                raw.is_err(),
                "the v15 five-kind CHECK must reject a 'grounded_in' edge"
            );
        }

        // Opening IS the migration.
        let mut store = Store::open(&path).unwrap();
        let version: i64 = store
            .conn
            .query_row("PRAGMA user_version", [], |row| row.get(0))
            .unwrap();
        assert_eq!(
            version, SCHEMA_VERSION,
            "v15 re-stamped to the current version"
        );
        assert_eq!(SCHEMA_VERSION, 20);

        // The legacy blocks edge survived the CHECK rebuild.
        assert_eq!(
            store.blockers_of("cap-2").unwrap(),
            vec!["cap-1".to_string()]
        );

        // The import-origin edge survived byte-true: still 'import'.
        let edges = store.list_relations("cap-3").unwrap();
        assert!(
            edges.iter().any(|r| r.kind == RelationKind::Supersedes
                && r.from_id == "cap-3"
                && r.to_id == "cap-2"
                && r.origin == RelationOrigin::Import),
            "the import-origin edge must survive the CHECK rebuild byte-true"
        );

        // The CHECK moved: a grounded_in edge now writes.
        assert!(
            store
                .upsert_relation(RelationKind::GroundedIn, "cap-1", "cap-3", injected_now())
                .unwrap()
        );

        // No-drift on the CHECK: the migrated relations DDL carries
        // 'grounded_in' (non-vacuity: a stale five-kind DDL fails this).
        let relations_ddl: String = rusqlite::Connection::open(&path)
            .unwrap()
            .query_row(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'relations'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert!(
            relations_ddl.contains("'grounded_in'"),
            "migrated relations CHECK must include 'grounded_in': {relations_ddl}"
        );
    }

    /// planning-plane u1 MIGRATION (pre-origin five-kind fixture): a
    /// faithful pre-v10 shape — relations under the FIVE-kind CHECK, NO
    /// `origin` column at all (the column arrived at v10), stamped 9 —
    /// migrates IN PLACE on open through the reconciled post-#131 flow.
    /// The `origin` ALTER guard (`table_has_column(&tx, "relations",
    /// "origin")` FALSE branch — the column is absent) fires FIRST,
    /// backfilling `origin='manual'` on every pre-existing edge (the
    /// historical truth for a store born before the column existed). ONLY
    /// THEN does the shared-DDL rebuild fire, gated on
    /// [`relations_missing_proposes_check`] (a five-kind CHECK is missing
    /// `'proposes'`) — [`relations_missing_part_of_check`] and
    /// [`relations_lacks_grounded_in`] agree, all three tokens absent — and
    /// rebuilding through [`relations_create_sql`]'s one current template —
    /// which already admits `'proposes'`, `'part_of'`, AND `'grounded_in'` —
    /// so the CHECK gains all three as a side effect of that same rebuild.
    /// Because the origin ALTER always runs before it, the rebuild's copy is
    /// UNCONDITIONALLY five-column (`kind,
    /// from_id, to_id, at, origin`) — there is no separate four-column
    /// copy arm. Exercises the `table_has_column` guard's FALSE branch —
    /// the sibling [`v15_five_kind_relations_widen_and_keep_origin`]
    /// exercises TRUE (origin already present, so only the rebuild fires).
    #[test]
    fn pre_origin_five_kind_relations_gain_origin_then_widen() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("memory.sqlite3");
        {
            let mut store = Store::open(&path).unwrap();
            store
                .append(&capsule("pre-origin claim alpha", "nott"), injected_now())
                .unwrap(); // cap-1
            store
                .append(&capsule("pre-origin claim beta", "nott"), injected_now())
                .unwrap(); // cap-2
            store
                .upsert_relation(RelationKind::Blocks, "cap-1", "cap-2", injected_now())
                .unwrap();
        }
        // Downgrade to a FAITHFUL pre-v10 shape: five-kind CHECK, NO origin
        // column (four columns only), stamp 9.
        {
            let conn = rusqlite::Connection::open(&path).unwrap();
            conn.execute_batch(
                "CREATE TABLE relations_pre_origin (
                     kind    TEXT NOT NULL CHECK (kind IN ('supersedes', 'derived_from', 'witnesses', 'blocks', 'falsifies')),
                     from_id TEXT NOT NULL,
                     to_id   TEXT NOT NULL,
                     at      TEXT NOT NULL,
                     PRIMARY KEY (kind, from_id, to_id)
                 );
                 INSERT INTO relations_pre_origin (kind, from_id, to_id, at) \
                     SELECT kind, from_id, to_id, at FROM relations;
                 DROP TABLE relations;
                 ALTER TABLE relations_pre_origin RENAME TO relations;
                 PRAGMA user_version = 9;",
            )
            .unwrap();
            // Faithful: no origin column exists on this shape.
            let has_origin: bool = conn
                .query_row(
                    "SELECT EXISTS(SELECT 1 FROM pragma_table_info('relations') \
                     WHERE name = 'origin')",
                    [],
                    |row| row.get(0),
                )
                .unwrap();
            assert!(!has_origin, "fixture must be faithfully pre-origin");
        }

        // Opening IS the migration — v9 is an enumerated migratable version.
        let mut store = Store::open(&path).unwrap();
        let version: i64 = store
            .conn
            .query_row("PRAGMA user_version", [], |row| row.get(0))
            .unwrap();
        assert_eq!(
            version, SCHEMA_VERSION,
            "pre-origin file re-stamped to current"
        );

        // The legacy blocks edge survived the origin-then-widen rebuild.
        assert_eq!(
            store.blockers_of("cap-2").unwrap(),
            vec!["cap-1".to_string()]
        );
        // Backfilled origin is 'manual' — the historical truth pre-v10.
        let edges = store.list_relations("cap-1").unwrap();
        assert!(
            edges
                .iter()
                .any(|r| r.kind == RelationKind::Blocks && r.origin == RelationOrigin::Manual),
            "a pre-origin edge backfills origin='manual'"
        );

        // The CHECK moved: a grounded_in edge now writes.
        assert!(
            store
                .upsert_relation(RelationKind::GroundedIn, "cap-1", "cap-2", injected_now())
                .unwrap()
        );
    }

    #[test]
    fn v15_file_migrates_to_v17_without_moving_capsules_or_snapshot() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("memory.sqlite3");
        let snapshot = {
            let mut store = Store::open(&path).unwrap();
            store
                .append(
                    &capsule("v15 git witness migration", "nott"),
                    injected_now(),
                )
                .unwrap();
            store.canonical_snapshot().unwrap()
        };
        // Downgrade to a FAITHFUL v15 shape: drop the v17 sidecars, stamp 15.
        {
            let conn = rusqlite::Connection::open(&path).unwrap();
            conn.execute_batch(
                "DROP TABLE corroborations; DROP TABLE source_cursors; \
                 PRAGMA user_version = 15;",
            )
            .unwrap();
        }

        // Opening IS the migration — v15 is now an enumerated migratable version.
        let mut store = Store::open(&path).unwrap();
        let version: i64 = store
            .conn
            .query_row("PRAGMA user_version", [], |r| r.get(0))
            .unwrap();
        assert_eq!(version, SCHEMA_VERSION);
        assert_eq!(SCHEMA_VERSION, 20);
        // The additive sidecars never move the capsule comparand bytes.
        assert_eq!(store.canonical_snapshot().unwrap(), snapshot);
        // The new sidecars are empty and usable after migration.
        assert!(store.latest_corroborations("cap-1").unwrap().is_none());
        assert_eq!(store.get_source_cursor("git:/repo").unwrap(), None);
        assert!(
            store
                .append_corroboration(
                    "cap-1",
                    "git",
                    "anchor_path",
                    "src/x.rs",
                    "corroborated",
                    Some("abc123"),
                    later_now(),
                )
                .unwrap()
        );
        assert_eq!(
            store
                .latest_corroborations("cap-1")
                .unwrap()
                .unwrap()
                .anchor_path
                .as_deref(),
            Some("corroborated")
        );
    }

    #[test]
    fn a_v16_file_migrates_up_rather_than_failing_closed() {
        // A v16 file is the parallel pin slice's stamp; this tree does not
        // create pin_events, but the migration keys on DDL shape / IF NOT
        // EXISTS, never the integer, so a v16 file converges to v17 here.
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("memory.sqlite3");
        {
            let conn = rusqlite::Connection::open(&path).unwrap();
            conn.execute_batch("PRAGMA user_version = 16;").unwrap();
        }
        let store = Store::open(&path).unwrap();
        let version: i64 = store
            .conn
            .query_row("PRAGMA user_version", [], |r| r.get(0))
            .unwrap();
        assert_eq!(version, SCHEMA_VERSION);
    }

    #[test]
    fn current_v18_file_heals_empty_source_backfill_without_moving_capsules() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("memory.sqlite3");
        let snapshot = {
            let mut store = Store::open(&path).unwrap();
            store
                .append(&capsule("v18 backfill heal", "nott"), injected_now())
                .unwrap();
            store.canonical_snapshot().unwrap()
        };
        {
            let conn = rusqlite::Connection::open(&path).unwrap();
            conn.execute_batch("DROP TABLE source_backfills; PRAGMA user_version = 18;")
                .unwrap();
        }

        let store = Store::open(&path).unwrap();
        let version: i64 = store
            .conn
            .query_row("PRAGMA user_version", [], |row| row.get(0))
            .unwrap();
        assert_eq!(version, SCHEMA_VERSION);
        assert_eq!(store.canonical_snapshot().unwrap(), snapshot);
        assert!(store.get_source_backfill("git:/repo").unwrap().is_none());
    }

    #[test]
    fn append_corroboration_is_change_gated_and_dedupes() {
        let mut store = Store::open_in_memory().unwrap();
        store
            .append(&capsule("anchored", "nott"), injected_now())
            .unwrap();
        // First observation writes; re-observing the SAME verdict writes
        // nothing (idempotent re-scan); a changed verdict writes again.
        assert!(
            store
                .append_corroboration(
                    "cap-1",
                    "git",
                    "anchor_path",
                    "src/x.rs",
                    "corroborated",
                    Some("h1"),
                    injected_now()
                )
                .unwrap()
        );
        assert!(
            !store
                .append_corroboration(
                    "cap-1",
                    "git",
                    "anchor_path",
                    "src/x.rs",
                    "corroborated",
                    Some("h1"),
                    later_now()
                )
                .unwrap()
        );
        assert!(
            store
                .append_corroboration(
                    "cap-1",
                    "git",
                    "anchor_path",
                    "src/x.rs",
                    "missing",
                    Some("h2"),
                    later_now()
                )
                .unwrap()
        );
        let count: i64 = store
            .conn
            .query_row("SELECT COUNT(*) FROM corroborations", [], |r| r.get(0))
            .unwrap();
        assert_eq!(count, 2, "unchanged re-scan added no row");
        // An illegal verdict is refused by the CHECK, never silently written.
        assert!(
            store
                .append_corroboration(
                    "cap-1",
                    "git",
                    "anchor_path",
                    "src/x.rs",
                    "unknown",
                    None,
                    later_now()
                )
                .is_err()
        );
    }

    #[test]
    fn latest_corroborations_folds_per_kind_and_counts_mentions() {
        let mut store = Store::open_in_memory().unwrap();
        store
            .append(&capsule("anchored", "nott"), injected_now())
            .unwrap();
        store
            .append_corroboration(
                "cap-1",
                "git",
                "anchor_path",
                "src/x.rs",
                "corroborated",
                Some("head9"),
                injected_now(),
            )
            .unwrap();
        store
            .append_corroboration(
                "cap-1",
                "git",
                "anchor_content",
                "src/x.rs",
                "drifted",
                Some("head9"),
                injected_now(),
            )
            .unwrap();
        store
            .append_corroboration(
                "cap-1",
                "git",
                "mention",
                "commitA",
                "corroborated",
                Some("head9"),
                injected_now(),
            )
            .unwrap();
        store
            .append_corroboration(
                "cap-1",
                "git",
                "mention",
                "commitB",
                "corroborated",
                Some("head9"),
                injected_now(),
            )
            .unwrap();
        let summary = store.latest_corroborations("cap-1").unwrap().unwrap();
        assert_eq!(summary.source, "git");
        assert_eq!(summary.git_ref.as_deref(), Some("head9"));
        assert_eq!(summary.anchor_path.as_deref(), Some("corroborated"));
        assert_eq!(summary.anchor_content.as_deref(), Some("drifted"));
        assert_eq!(summary.anchor_sha, None);
        assert_eq!(summary.mentions, 2);
        assert!(store.latest_corroborations("cap-2").unwrap().is_none());
    }

    #[test]
    fn corroboration_counts_reflect_latest_verdict_only() {
        let mut store = Store::open_in_memory().unwrap();
        store.append(&capsule("a", "nott"), injected_now()).unwrap();
        store.append(&capsule("b", "nott"), injected_now()).unwrap();
        // cap-1's anchor went missing then came back — counts must reflect
        // the LATEST (corroborated), not both.
        store
            .append_corroboration(
                "cap-1",
                "git",
                "anchor_path",
                "a.rs",
                "missing",
                None,
                injected_now(),
            )
            .unwrap();
        store
            .append_corroboration(
                "cap-1",
                "git",
                "anchor_path",
                "a.rs",
                "corroborated",
                None,
                later_now(),
            )
            .unwrap();
        store
            .append_corroboration(
                "cap-2",
                "git",
                "anchor_content",
                "b.rs",
                "drifted",
                None,
                injected_now(),
            )
            .unwrap();
        store
            .append_corroboration(
                "cap-2",
                "git",
                "mention",
                "commitZ",
                "corroborated",
                None,
                injected_now(),
            )
            .unwrap();
        let counts = store.corroboration_counts().unwrap();
        let git = counts.get("git").copied().unwrap();
        assert_eq!(git.corroborated, 1);
        assert_eq!(git.drifted, 1);
        assert_eq!(
            git.missing, 0,
            "the superseded missing verdict does not count"
        );
        assert_eq!(git.mentions, 1);
    }

    #[test]
    fn source_cursor_replaces_in_place() {
        let mut store = Store::open_in_memory().unwrap();
        assert_eq!(store.get_source_cursor("git:/repo").unwrap(), None);
        store
            .set_source_cursor("git:/repo", "sha-one", injected_now())
            .unwrap();
        assert_eq!(
            store.get_source_cursor("git:/repo").unwrap().as_deref(),
            Some("sha-one")
        );
        store
            .set_source_cursor("git:/repo", "sha-two", later_now())
            .unwrap();
        assert_eq!(
            store.get_source_cursor("git:/repo").unwrap().as_deref(),
            Some("sha-two")
        );
        let rows = store.list_source_cursors().unwrap();
        assert_eq!(rows.len(), 1, "cursor is replaced, never accumulated");
        assert!(store.set_source_cursor("", "x", injected_now()).is_err());
        assert!(
            store
                .set_source_cursor("git:/repo", "", injected_now())
                .is_err()
        );
    }

    #[test]
    fn source_backfill_checkpoint_is_cas_guarded_and_completion_is_atomic() {
        let mut store = Store::open_in_memory().unwrap();
        let key = "git:/repo";
        store
            .set_source_cursor(key, "base", injected_now())
            .unwrap();
        store
            .checkpoint_source_backfill(key, Some("base"), "target", 0, 2, injected_now())
            .unwrap();
        let state = store.get_source_backfill(key).unwrap().unwrap();
        assert_eq!(state.base_cursor.as_deref(), Some("base"));
        assert_eq!(state.target_head, "target");
        assert_eq!(state.next_offset, 2);

        let stale = store
            .checkpoint_source_backfill(key, Some("base"), "target", 1, 3, later_now())
            .unwrap_err();
        assert!(matches!(stale, StoreError::StaleSourceBackfill(_)));
        store
            .checkpoint_source_backfill(key, Some("base"), "target", 2, 3, later_now())
            .unwrap();
        store
            .complete_source_backfill(key, Some("base"), "target", 3, later_now())
            .unwrap();
        assert!(store.get_source_backfill(key).unwrap().is_none());
        assert_eq!(
            store.get_source_cursor(key).unwrap().as_deref(),
            Some("target")
        );
    }

    #[test]
    fn stale_source_traversal_cannot_regress_a_winning_cursor() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("memory.sqlite3");
        let key = "git:/repo";
        {
            let mut seed = Store::open(&path).unwrap();
            seed.set_source_cursor(key, "base", injected_now()).unwrap();
        }
        let mut stale = Store::open(&path).unwrap();
        let mut winner = Store::open(&path).unwrap();
        winner
            .complete_source_backfill(key, Some("base"), "newer-target", 0, later_now())
            .unwrap();

        for error in [
            stale
                .checkpoint_source_backfill(key, Some("base"), "older-target", 0, 1, later_now())
                .unwrap_err(),
            stale
                .complete_source_backfill(key, Some("base"), "older-target", 0, later_now())
                .unwrap_err(),
            stale
                .clear_source_traversal(key, Some("base"), None)
                .unwrap_err(),
        ] {
            assert!(matches!(error, StoreError::StaleSourceBackfill(_)));
        }

        let observed = Store::open(&path).unwrap();
        assert_eq!(
            observed.get_source_cursor(key).unwrap().as_deref(),
            Some("newer-target")
        );
        assert!(observed.get_source_backfill(key).unwrap().is_none());
    }

    #[test]
    fn event_time_sidecar_does_not_transfer_on_merge() {
        let dir = tempfile::tempdir().unwrap();
        let local_path = dir.path().join("local.sqlite3");
        let incoming_path = dir.path().join("incoming.sqlite3");
        let local_range = EventTimeRange::new(injected_now(), later_now()).unwrap();
        let incoming_range = EventTimeRange::new(
            datetime!(2025-01-01 00:00:00 UTC),
            datetime!(2025-12-31 23:59:59 UTC),
        )
        .unwrap();
        {
            let mut local = Store::open(&local_path).unwrap();
            local
                .append_with_event_time(
                    &capsule("local dated", "nott"),
                    &local_range,
                    injected_now(),
                )
                .unwrap();
            local
                .append_with_event_time(
                    &capsule("shared dated", "nott"),
                    &local_range,
                    injected_now(),
                )
                .unwrap();
        }
        {
            let mut incoming = Store::open(&incoming_path).unwrap();
            incoming
                .append_with_event_time(
                    &capsule("incoming dated", "nott"),
                    &incoming_range,
                    injected_now(),
                )
                .unwrap();
            incoming
                .append_with_event_time(
                    &capsule("shared dated", "nott"),
                    &incoming_range,
                    injected_now(),
                )
                .unwrap();
        }

        let mut local = Store::open(&local_path).unwrap();
        let before = local.canonical_snapshot().unwrap();
        local.merge_from(&incoming_path, b"local-key").unwrap();
        assert_eq!(
            local.event_time_of("cap-1").unwrap().unwrap().event_from(),
            local_range.event_from(),
            "local declaration survives"
        );
        assert_eq!(
            local.event_time_of("cap-2").unwrap(),
            Some(EventTimeRecord {
                range: local_range,
                declared_at: injected_now(),
            }),
            "a content collapse keeps the receiving store's local declaration"
        );
        assert_eq!(
            local.event_time_of("cap-3").unwrap(),
            None,
            "a newly-added incoming capsule becomes undated on the receiving store"
        );
        assert!(local.canonical_snapshot().unwrap().starts_with(&before));
    }

    // ---- b2 staged review (S4) ----

    /// The v18 relations rebuild PRESERVES the `origin` column byte-for-byte —
    /// the falsifies template it is modeled on copies only four columns, so a
    /// naive copy would silently reset every import edge to `manual` and erase
    /// import provenance. Also proves the migration widens the CHECK to admit
    /// `proposes`, re-creates the review sidecar, and never moves a capsule.
    #[test]
    fn v18_relations_rebuild_preserves_import_origin_and_snapshot() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("memory.sqlite3");
        let snapshot = {
            let mut store = Store::open(&path).unwrap();
            store
                .append(&capsule("origin edge source", "nott"), injected_now())
                .unwrap();
            store
                .append(&capsule("origin edge target", "nott"), injected_now())
                .unwrap();
            // A machine-written import edge — the ONLY kind the rebuild must
            // not silently rewrite to `manual`.
            store
                .upsert_relation_origin(
                    RelationKind::Supersedes,
                    "cap-1",
                    "cap-2",
                    injected_now(),
                    RelationOrigin::Import,
                )
                .unwrap();
            store.canonical_snapshot().unwrap()
        };
        // Downgrade to a FAITHFUL pre-v18 shape: a five-kind relations CHECK
        // (no `proposes`) carrying the import edge, no review sidecar, stamp 17.
        {
            let conn = rusqlite::Connection::open(&path).unwrap();
            conn.execute_batch(
                "CREATE TABLE relations_old (
                     kind TEXT NOT NULL CHECK (kind IN ('supersedes','derived_from','witnesses','blocks','falsifies')),
                     from_id TEXT NOT NULL, to_id TEXT NOT NULL, at TEXT NOT NULL,
                     origin TEXT NOT NULL DEFAULT 'manual' CHECK (origin IN ('manual','import')),
                     PRIMARY KEY (kind, from_id, to_id));
                 INSERT INTO relations_old SELECT kind, from_id, to_id, at, origin FROM relations;
                 DROP TABLE relations;
                 ALTER TABLE relations_old RENAME TO relations;
                 DROP TABLE review_events;
                 PRAGMA user_version = 17;",
            )
            .unwrap();
        }
        // Opening IS the migration (v17 -> v18): the relations_v6 rebuild runs.
        let mut store = Store::open(&path).unwrap();
        let version: i64 = store
            .conn
            .query_row("PRAGMA user_version", [], |r| r.get(0))
            .unwrap();
        assert_eq!(version, SCHEMA_VERSION);
        // The import edge survived byte-for-byte — origin is STILL `import`.
        let edges = store.list_relations("cap-2").unwrap();
        let import_edge = edges
            .iter()
            .find(|e| e.kind == RelationKind::Supersedes && e.to_id == "cap-2")
            .expect("the import supersedes edge survived the rebuild");
        assert_eq!(
            import_edge.origin,
            RelationOrigin::Import,
            "the rebuild MUST copy origin — a dropped origin erases import provenance"
        );
        // Capsules never moved; the review sidecar is back; the CHECK now
        // admits `proposes`.
        assert_eq!(store.canonical_snapshot().unwrap(), snapshot);
        assert!(store.review_state_of("cap-1").unwrap().is_none());
        assert!(
            store
                .upsert_relation(RelationKind::Proposes, "cap-1", "cap-2", injected_now())
                .is_ok(),
            "the widened CHECK admits a proposes edge"
        );
    }

    /// The review fence is DERIVED from the LATEST verdict, never a stored
    /// flag: proposed/rejected are fenced, a later ratified reverses it.
    #[test]
    fn review_fence_derives_from_the_latest_verdict() {
        let mut store = Store::open_in_memory().unwrap();
        let id = store
            .append(&capsule("a reviewable claim", "nott"), injected_now())
            .unwrap();
        let id = id.as_str().to_string();
        // No review history -> not fenced, no state.
        assert!(!store.review_fenced(&id).unwrap());
        assert!(store.review_state_of(&id).unwrap().is_none());
        // proposed -> fenced.
        store
            .append_review_event(
                &id,
                ReviewVerdict::Proposed,
                "staged ingest",
                "author",
                injected_now(),
            )
            .unwrap();
        assert!(store.review_fenced(&id).unwrap());
        assert_eq!(
            store.review_verdict(&id).unwrap().as_deref(),
            Some("proposed")
        );
        // rejected -> still fenced (NEVER a tombstone).
        store
            .append_review_event(
                &id,
                ReviewVerdict::Rejected,
                "not now",
                "owner",
                later_now(),
            )
            .unwrap();
        assert!(store.review_fenced(&id).unwrap());
        assert_eq!(
            store.review_verdict(&id).unwrap().as_deref(),
            Some("rejected")
        );
        // ratified -> reverses the fence; full history is retained.
        store
            .append_review_event(
                &id,
                ReviewVerdict::Ratified,
                "owner approved",
                "owner",
                later_now(),
            )
            .unwrap();
        assert!(!store.review_fenced(&id).unwrap());
        let state = store.review_state_of(&id).unwrap().unwrap();
        assert_eq!(state.latest(), ReviewVerdict::Ratified);
        assert_eq!(
            state.history().len(),
            3,
            "the verdict history is append-only"
        );
        assert_eq!(store.count_review_fenced().unwrap(), 0);
    }

    /// `stale_proposals` counts only fenced proposals older than the window;
    /// a fresh proposal and a ratified one never count.
    #[test]
    fn stale_proposals_counts_only_aged_fenced_proposals() {
        let mut store = Store::open_in_memory().unwrap();
        let fresh = store
            .append(&capsule("fresh proposal", "nott"), injected_now())
            .unwrap();
        let old = store
            .append(&capsule("old proposal", "nott"), injected_now())
            .unwrap();
        let base = datetime!(2026-07-01 00:00:00 UTC);
        store
            .append_review_event(fresh.as_str(), ReviewVerdict::Proposed, "s", "a", base)
            .unwrap();
        store
            .append_review_event(old.as_str(), ReviewVerdict::Proposed, "s", "a", base)
            .unwrap();
        // "now" 20 days after base, window 14 days: only the (equally old, but
        // both are 20d) proposals count — both are stale here.
        let now = base + time::Duration::days(20);
        assert_eq!(store.count_review_fenced().unwrap(), 2);
        assert_eq!(store.stale_proposals(now, 14).unwrap(), 2);
        // A wider window (30 days) makes neither stale.
        assert_eq!(store.stale_proposals(now, 30).unwrap(), 0);
        // Ratifying one drops it from BOTH counts.
        store
            .append_review_event(fresh.as_str(), ReviewVerdict::Ratified, "ok", "o", now)
            .unwrap();
        assert_eq!(store.count_review_fenced().unwrap(), 1);
        assert_eq!(store.stale_proposals(now, 14).unwrap(), 1);
    }

    /// Red-test 11 (fence durability): a proposal merged into a FRESH store
    /// stays fenced there — the review history rides the newly-minted capsule.
    #[test]
    fn merge_carries_a_proposal_fence_into_a_fresh_store() {
        let dir = tempfile::tempdir().unwrap();
        let local_path = dir.path().join("local.sqlite3");
        let incoming_path = dir.path().join("incoming.sqlite3");
        {
            let mut incoming = Store::open(&incoming_path).unwrap();
            let id = incoming
                .append(
                    &capsule("a fenced proposal for merge", "nott"),
                    injected_now(),
                )
                .unwrap();
            incoming
                .append_review_event(
                    id.as_str(),
                    ReviewVerdict::Proposed,
                    "staged",
                    "author",
                    injected_now(),
                )
                .unwrap();
            incoming
                .append_review_event(
                    id.as_str(),
                    ReviewVerdict::Rejected,
                    "foreign rejection",
                    "foreign closer",
                    later_now(),
                )
                .unwrap();
        }
        let mut local = Store::open(&local_path).unwrap();
        let applied = local.merge_from(&incoming_path, b"k").unwrap();
        assert_eq!(
            applied.added_ids.len(),
            1,
            "the proposal minted a fresh capsule"
        );
        let minted = &applied.added_ids[0];
        assert!(
            local.review_fenced(minted).unwrap(),
            "the merged proposal stays fenced (fence durability)"
        );
        assert_eq!(
            local.review_verdict(minted).unwrap().as_deref(),
            Some("proposed")
        );
    }

    /// A standalone destination must never treat a foreign close verdict as
    /// local authority. Any reviewed foreign capsule is one local proposal,
    /// regardless of whether the source's latest state was ratified or
    /// rejected.
    #[test]
    fn merge_normalizes_foreign_close_verdicts_to_one_local_proposal() {
        let dir = tempfile::tempdir().unwrap();
        let local_path = dir.path().join("local.sqlite3");
        let incoming_path = dir.path().join("incoming.sqlite3");
        {
            let mut incoming = Store::open(&incoming_path).unwrap();
            for (content, close) in [
                ("foreign ratified review", ReviewVerdict::Ratified),
                ("foreign rejected review", ReviewVerdict::Rejected),
            ] {
                let id = incoming
                    .append(&capsule(content, "nott"), injected_now())
                    .unwrap();
                incoming
                    .append_review_event(
                        id.as_str(),
                        ReviewVerdict::Proposed,
                        "foreign proposal",
                        "foreign author",
                        injected_now(),
                    )
                    .unwrap();
                incoming
                    .append_review_event(
                        id.as_str(),
                        close,
                        "foreign close",
                        "foreign closer",
                        later_now(),
                    )
                    .unwrap();
            }
        }

        let mut local = Store::open(&local_path).unwrap();
        let applied = local.merge_from(&incoming_path, b"k").unwrap();
        assert_eq!(applied.added_ids.len(), 2);
        for id in applied.added_ids {
            let state = local.review_state_of(&id).unwrap().unwrap();
            assert_eq!(state.latest(), ReviewVerdict::Proposed);
            assert_eq!(
                state.history().len(),
                1,
                "foreign review history is not imported as local authority"
            );
            assert!(local.review_fenced(&id).unwrap());
        }
    }

    /// Red-test 3b (demotion attack, merge path): an incoming proposal whose
    /// content collides with LOCAL plain truth must NOT fence the local
    /// capsule — truth won locally stays truth.
    #[test]
    fn merge_never_fences_local_truth_with_a_colliding_proposal() {
        let dir = tempfile::tempdir().unwrap();
        let local_path = dir.path().join("local.sqlite3");
        let incoming_path = dir.path().join("incoming.sqlite3");
        {
            let mut local = Store::open(&local_path).unwrap();
            local
                .append(
                    &capsule("shared content that is local truth", "nott"),
                    injected_now(),
                )
                .unwrap();
        }
        {
            let mut incoming = Store::open(&incoming_path).unwrap();
            let id = incoming
                .append(
                    &capsule("shared content that is local truth", "nott"),
                    injected_now(),
                )
                .unwrap();
            incoming
                .append_review_event(
                    id.as_str(),
                    ReviewVerdict::Proposed,
                    "staged",
                    "author",
                    injected_now(),
                )
                .unwrap();
            incoming
                .append_review_event(
                    id.as_str(),
                    ReviewVerdict::Rejected,
                    "foreign rejection",
                    "foreign closer",
                    later_now(),
                )
                .unwrap();
        }
        let mut local = Store::open(&local_path).unwrap();
        let applied = local.merge_from(&incoming_path, b"k").unwrap();
        assert_eq!(
            applied.added_ids.len(),
            0,
            "identical content collapses, nothing minted"
        );
        assert!(
            !local.review_fenced("cap-1").unwrap(),
            "an incoming proposal must NEVER demote local truth"
        );
        assert!(local.review_state_of("cap-1").unwrap().is_none());
    }

    #[test]
    fn merge_never_promotes_a_colliding_local_proposal() {
        let dir = tempfile::tempdir().unwrap();
        let local_path = dir.path().join("local.sqlite3");
        let incoming_path = dir.path().join("incoming.sqlite3");
        {
            let mut local = Store::open(&local_path).unwrap();
            let id = local
                .append(
                    &capsule("shared content under local review", "nott"),
                    injected_now(),
                )
                .unwrap();
            local
                .append_review_event(
                    id.as_str(),
                    ReviewVerdict::Proposed,
                    "local proposal",
                    "local author",
                    injected_now(),
                )
                .unwrap();
        }
        {
            let mut incoming = Store::open(&incoming_path).unwrap();
            let id = incoming
                .append(
                    &capsule("shared content under local review", "nott"),
                    injected_now(),
                )
                .unwrap();
            incoming
                .append_review_event(
                    id.as_str(),
                    ReviewVerdict::Proposed,
                    "foreign proposal",
                    "foreign author",
                    injected_now(),
                )
                .unwrap();
            incoming
                .append_review_event(
                    id.as_str(),
                    ReviewVerdict::Ratified,
                    "foreign ratification",
                    "foreign closer",
                    later_now(),
                )
                .unwrap();
        }

        let mut local = Store::open(&local_path).unwrap();
        let applied = local.merge_from(&incoming_path, b"k").unwrap();
        assert!(applied.added_ids.is_empty());
        let state = local.review_state_of("cap-1").unwrap().unwrap();
        assert_eq!(state.latest(), ReviewVerdict::Proposed);
        assert_eq!(state.history().len(), 1);
        assert_eq!(state.history()[0].actor, "local author");
    }

    /// effort-lifecycle s1 MIGRATION (v15→v19 reconciled): a faithful v15
    /// file — the FIVE-kind relations CHECK (falsifies present, proposes and
    /// part_of absent), carrying an `origin='import'` edge — migrates IN PLACE
    /// on open. Every (kind, from_id, to_id, at, origin) row survives
    /// BYTE-EXACT, capsule seqs are untouched, the reconciled CHECK gains BOTH
    /// 'proposes' and 'part_of' in the single folded rebuild, and a fresh
    /// part_of edge then writes. Mirrors the falsifies-widening test.
    #[test]
    fn v15_file_with_five_kind_relations_migrates_to_current_with_part_of() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("memory.sqlite3");
        let (before_edges, before_seqs) = {
            let mut store = Store::open(&path).unwrap();
            store
                .append(&capsule("effort alpha", "nott"), injected_now())
                .unwrap(); // cap-1
            store
                .append(&capsule("effort beta", "nott"), injected_now())
                .unwrap(); // cap-2
            store
                .append(&capsule("effort gamma", "nott"), injected_now())
                .unwrap(); // cap-3
            // A manual blocks edge AND a machine-written (import) supersede
            // edge — `origin` MUST survive the rebuild byte-exact.
            store
                .upsert_relation(RelationKind::Blocks, "cap-1", "cap-2", injected_now())
                .unwrap();
            store
                .upsert_relation_origin(
                    RelationKind::Supersedes,
                    "cap-3",
                    "cap-2",
                    injected_now(),
                    RelationOrigin::Import,
                )
                .unwrap();
            let seqs: Vec<i64> = ["cap-1", "cap-2", "cap-3"]
                .iter()
                .map(|id| store.get(id).unwrap().unwrap().seq)
                .collect();
            (store.all_relations().unwrap(), seqs)
        };

        // Downgrade to a FAITHFUL v15 shape: rebuild `relations` under the
        // FIVE-kind CHECK (no part_of), PRESERVING `origin`, and stamp v15.
        {
            let conn = rusqlite::Connection::open(&path).unwrap();
            conn.execute_batch(
                "CREATE TABLE relations_v15 (
                     kind    TEXT NOT NULL CHECK (kind IN ('supersedes', 'derived_from', 'witnesses', 'blocks', 'falsifies')),
                     from_id TEXT NOT NULL,
                     to_id   TEXT NOT NULL,
                     at      TEXT NOT NULL,
                     origin  TEXT NOT NULL DEFAULT 'manual' CHECK (origin IN ('manual', 'import')),
                     PRIMARY KEY (kind, from_id, to_id)
                 );
                 INSERT INTO relations_v15 SELECT kind, from_id, to_id, at, origin FROM relations;
                 DROP TABLE relations;
                 ALTER TABLE relations_v15 RENAME TO relations;
                 PRAGMA user_version = 15;",
            )
            .unwrap();
            // Faithful: the v15 five-kind CHECK rejects a raw 'part_of' edge.
            let raw = conn.execute(
                "INSERT INTO relations (kind, from_id, to_id, at, origin) \
                 VALUES ('part_of', 'cap-1', 'cap-3', '2026-07-18T00:00:00Z', 'manual')",
                [],
            );
            assert!(
                raw.is_err(),
                "the v15 five-kind CHECK must reject a 'part_of' edge"
            );
        }

        // Opening IS the migration — v15 is an enumerated migratable version.
        let mut store = Store::open(&path).unwrap();
        {
            let conn = rusqlite::Connection::open(&path).unwrap();
            let version: i64 = conn
                .query_row("PRAGMA user_version", [], |row| row.get(0))
                .unwrap();
            assert_eq!(version, SCHEMA_VERSION);
        }
        // Every edge survived BYTE-EXACT, origin included (Import stays Import).
        assert_eq!(
            store.all_relations().unwrap(),
            before_edges,
            "every (kind, from, to, at, origin) row must survive the rebuild"
        );
        // Capsule seqs untouched.
        let after_seqs: Vec<i64> = ["cap-1", "cap-2", "cap-3"]
            .iter()
            .map(|id| store.get(id).unwrap().unwrap().seq)
            .collect();
        assert_eq!(after_seqs, before_seqs, "capsule seqs must be preserved");
        // The CHECK moved: the migrated relations DDL carries 'part_of'.
        let relations_ddl: String = rusqlite::Connection::open(&path)
            .unwrap()
            .query_row(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'relations'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert!(
            relations_ddl.contains("'part_of'"),
            "migrated relations CHECK must include 'part_of': {relations_ddl}"
        );
        // A fresh part_of edge now writes (store layer has no classification
        // guard — that fence lives at the server boundary).
        assert!(
            store
                .upsert_relation(RelationKind::PartOf, "cap-1", "cap-3", injected_now())
                .unwrap()
        );
    }

    /// effort-lifecycle s1 / planning-plane s1 NO-DRIFT: a v15 file migrated
    /// to the current v20 has the EXACT relations schema — CHECK text AND
    /// `pragma_table_info` — of a freshly created v20 store. The shared
    /// [`relations_create_sql`] makes the two shapes unforgeably identical
    /// (the falsifies-widening no-drift law, extended to the reconciled
    /// proposes+part_of+grounded_in set).
    #[test]
    fn migrated_v15_and_fresh_relations_schemas_do_not_drift() {
        let relations_ddl = |path: &std::path::Path| -> String {
            rusqlite::Connection::open(path)
                .unwrap()
                .query_row(
                    "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'relations'",
                    [],
                    |r| r.get(0),
                )
                .unwrap()
        };
        let table_info =
            |path: &std::path::Path| -> Vec<(i64, String, String, i64, Option<String>, i64)> {
                let conn = rusqlite::Connection::open(path).unwrap();
                let mut stmt = conn.prepare("PRAGMA table_info(relations)").unwrap();
                let rows = stmt
                    .query_map([], |r| {
                        Ok((
                            r.get(0)?,
                            r.get(1)?,
                            r.get(2)?,
                            r.get(3)?,
                            r.get(4)?,
                            r.get(5)?,
                        ))
                    })
                    .unwrap();
                rows.map(Result::unwrap).collect()
            };

        // Fresh v16 store.
        let fresh_dir = tempfile::tempdir().unwrap();
        let fresh_path = fresh_dir.path().join("fresh.sqlite3");
        Store::open(&fresh_path).unwrap();

        // Migrated: create v16, downgrade to a faithful v15, reopen (migrate).
        let mig_dir = tempfile::tempdir().unwrap();
        let mig_path = mig_dir.path().join("migrated.sqlite3");
        Store::open(&mig_path).unwrap();
        {
            let conn = rusqlite::Connection::open(&mig_path).unwrap();
            conn.execute_batch(
                "CREATE TABLE relations_v15 (
                     kind    TEXT NOT NULL CHECK (kind IN ('supersedes', 'derived_from', 'witnesses', 'blocks', 'falsifies')),
                     from_id TEXT NOT NULL,
                     to_id   TEXT NOT NULL,
                     at      TEXT NOT NULL,
                     origin  TEXT NOT NULL DEFAULT 'manual' CHECK (origin IN ('manual', 'import')),
                     PRIMARY KEY (kind, from_id, to_id)
                 );
                 INSERT INTO relations_v15 SELECT kind, from_id, to_id, at, origin FROM relations;
                 DROP TABLE relations;
                 ALTER TABLE relations_v15 RENAME TO relations;
                 PRAGMA user_version = 15;",
            )
            .unwrap();
        }
        Store::open(&mig_path).unwrap();

        // The CREATE header differs cosmetically (fresh: `IF NOT EXISTS`;
        // migrated: the rename target) — the SHARED DDL body from the first
        // `(` onward (every column + the kind CHECK + the origin CHECK + the
        // PK) is what must be byte-identical.
        let body = |ddl: &str| ddl[ddl.find('(').unwrap()..].to_string();
        let fresh_ddl = relations_ddl(&fresh_path);
        let mig_ddl = relations_ddl(&mig_path);
        assert_eq!(
            body(&fresh_ddl),
            body(&mig_ddl),
            "migrated and fresh relations body (columns + CHECK text) must not drift"
        );
        assert!(
            body(&fresh_ddl).contains("'part_of'")
                && body(&fresh_ddl).contains("'proposes'")
                && body(&fresh_ddl).contains("'grounded_in'"),
            "the reconciled eight-kind CHECK is the shared shape"
        );
        assert_eq!(
            table_info(&fresh_path),
            table_info(&mig_path),
            "migrated and fresh pragma_table_info must not drift"
        );
    }

    /// Review-mandated single-token migration RED (#143 follow-up): a
    /// faithful v18 shape — relations CHECK admits `proposes` but NEITHER
    /// `part_of` NOR `grounded_in`, `origin` column present, stamp 18 —
    /// migrates IN PLACE on open. Only [`relations_missing_part_of_check`]
    /// and [`relations_lacks_grounded_in`] fire ([`relations_missing_proposes_check`]
    /// is already false); the single folded rebuild still converges the
    /// store to the full eight-kind set, every pre-existing edge (including
    /// the legacy `proposes` edge) survives BYTE-EXACT, and both formerly
    /// missing kinds write afterward.
    #[test]
    fn proposes_only_relations_check_converges_to_the_eight_kind_set() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("memory.sqlite3");
        let before = {
            let mut store = Store::open(&path).unwrap();
            store
                .append(&capsule("proposes-only alpha", "nott"), injected_now())
                .unwrap(); // cap-1
            store
                .append(&capsule("proposes-only beta", "nott"), injected_now())
                .unwrap(); // cap-2
            store
                .append(&capsule("proposes-only gamma", "nott"), injected_now())
                .unwrap(); // cap-3
            store
                .upsert_relation(RelationKind::Blocks, "cap-1", "cap-2", injected_now())
                .unwrap();
            store
                .upsert_relation(RelationKind::Proposes, "cap-3", "cap-2", injected_now())
                .unwrap();
            store.all_relations().unwrap()
        };
        // Downgrade to a FAITHFUL v18 shape: six-kind CHECK (proposes present,
        // part_of and grounded_in absent), origin column kept, stamp 18.
        {
            let conn = rusqlite::Connection::open(&path).unwrap();
            conn.execute_batch(
                "CREATE TABLE relations_v18 (
                     kind    TEXT NOT NULL CHECK (kind IN ('supersedes', 'derived_from', 'witnesses', 'blocks', 'falsifies', 'proposes')),
                     from_id TEXT NOT NULL,
                     to_id   TEXT NOT NULL,
                     at      TEXT NOT NULL,
                     origin  TEXT NOT NULL DEFAULT 'manual' CHECK (origin IN ('manual', 'import')),
                     PRIMARY KEY (kind, from_id, to_id)
                 );
                 INSERT INTO relations_v18 (kind, from_id, to_id, at, origin) \
                     SELECT kind, from_id, to_id, at, origin FROM relations;
                 DROP TABLE relations;
                 ALTER TABLE relations_v18 RENAME TO relations;
                 PRAGMA user_version = 18;",
            )
            .unwrap();
            // Faithful: this CHECK rejects both still-missing kinds.
            for bad_kind in ["part_of", "grounded_in"] {
                let raw = conn.execute(
                    &format!(
                        "INSERT INTO relations (kind, from_id, to_id, at, origin) \
                         VALUES ('{bad_kind}', 'cap-1', 'cap-3', '2026-07-24T00:00:00Z', 'manual')"
                    ),
                    [],
                );
                assert!(
                    raw.is_err(),
                    "the proposes-only v18 CHECK must reject a {bad_kind:?} edge"
                );
            }
        }

        // Opening IS the migration.
        let mut store = Store::open(&path).unwrap();
        let version: i64 = store
            .conn
            .query_row("PRAGMA user_version", [], |row| row.get(0))
            .unwrap();
        assert_eq!(version, SCHEMA_VERSION, "proposes-only file re-stamped");
        assert_eq!(SCHEMA_VERSION, 20);

        // Every edge (including the legacy proposes edge) survives byte-exact.
        assert_eq!(
            store.all_relations().unwrap(),
            before,
            "every (kind, from, to, at, origin) row must survive the rebuild"
        );

        // The CHECK moved: both formerly missing kinds now write.
        assert!(
            store
                .upsert_relation(RelationKind::PartOf, "cap-1", "cap-3", injected_now())
                .unwrap()
        );
        assert!(
            store
                .upsert_relation(RelationKind::GroundedIn, "cap-2", "cap-3", injected_now())
                .unwrap()
        );
        let relations_ddl: String = rusqlite::Connection::open(&path)
            .unwrap()
            .query_row(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'relations'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert!(
            relations_ddl.contains("'proposes'")
                && relations_ddl.contains("'part_of'")
                && relations_ddl.contains("'grounded_in'"),
            "migrated relations CHECK must admit the full eight-kind set: {relations_ddl}"
        );
    }

    /// Review-mandated single-token migration RED (#143 follow-up): the
    /// OUT-OF-ORDER sibling of the test above — a store whose CHECK admits
    /// `part_of` but NEITHER `proposes` NOR `grounded_in` (e.g. an
    /// effort-lifecycle-s1-only binary that never saw #131's `proposes`),
    /// `origin` column present, stamp 19. Only
    /// [`relations_missing_proposes_check`] and [`relations_lacks_grounded_in`]
    /// fire; the same folded rebuild converges it to the full eight-kind set,
    /// every pre-existing edge (including the legacy `part_of` edge) survives
    /// BYTE-EXACT, and both formerly missing kinds write afterward.
    #[test]
    fn part_of_only_relations_check_converges_to_the_eight_kind_set() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("memory.sqlite3");
        let before = {
            let mut store = Store::open(&path).unwrap();
            store
                .append(&capsule("part_of-only alpha", "nott"), injected_now())
                .unwrap(); // cap-1
            store
                .append(&capsule("part_of-only beta", "nott"), injected_now())
                .unwrap(); // cap-2
            store
                .append(&capsule("part_of-only gamma", "nott"), injected_now())
                .unwrap(); // cap-3
            store
                .upsert_relation(RelationKind::Blocks, "cap-1", "cap-2", injected_now())
                .unwrap();
            // part_of has no classification write-guard at the store layer —
            // that fence lives at the server boundary.
            store
                .upsert_relation(RelationKind::PartOf, "cap-3", "cap-2", injected_now())
                .unwrap();
            store.all_relations().unwrap()
        };
        // Downgrade to the out-of-order shape: six-kind CHECK (part_of
        // present, proposes and grounded_in absent), origin column kept,
        // stamp 19.
        {
            let conn = rusqlite::Connection::open(&path).unwrap();
            conn.execute_batch(
                "CREATE TABLE relations_v19_oo (
                     kind    TEXT NOT NULL CHECK (kind IN ('supersedes', 'derived_from', 'witnesses', 'blocks', 'falsifies', 'part_of')),
                     from_id TEXT NOT NULL,
                     to_id   TEXT NOT NULL,
                     at      TEXT NOT NULL,
                     origin  TEXT NOT NULL DEFAULT 'manual' CHECK (origin IN ('manual', 'import')),
                     PRIMARY KEY (kind, from_id, to_id)
                 );
                 INSERT INTO relations_v19_oo (kind, from_id, to_id, at, origin) \
                     SELECT kind, from_id, to_id, at, origin FROM relations;
                 DROP TABLE relations;
                 ALTER TABLE relations_v19_oo RENAME TO relations;
                 PRAGMA user_version = 19;",
            )
            .unwrap();
            // Faithful: this CHECK rejects both still-missing kinds.
            for bad_kind in ["proposes", "grounded_in"] {
                let raw = conn.execute(
                    &format!(
                        "INSERT INTO relations (kind, from_id, to_id, at, origin) \
                         VALUES ('{bad_kind}', 'cap-1', 'cap-3', '2026-07-24T00:00:00Z', 'manual')"
                    ),
                    [],
                );
                assert!(
                    raw.is_err(),
                    "the part_of-only v19 CHECK must reject a {bad_kind:?} edge"
                );
            }
        }

        // Opening IS the migration.
        let mut store = Store::open(&path).unwrap();
        let version: i64 = store
            .conn
            .query_row("PRAGMA user_version", [], |row| row.get(0))
            .unwrap();
        assert_eq!(version, SCHEMA_VERSION, "part_of-only file re-stamped");
        assert_eq!(SCHEMA_VERSION, 20);

        // Every edge (including the legacy part_of edge) survives byte-exact.
        assert_eq!(
            store.all_relations().unwrap(),
            before,
            "every (kind, from, to, at, origin) row must survive the rebuild"
        );

        // The CHECK moved: both formerly missing kinds now write.
        assert!(
            store
                .upsert_relation(RelationKind::Proposes, "cap-1", "cap-3", injected_now())
                .unwrap()
        );
        assert!(
            store
                .upsert_relation(RelationKind::GroundedIn, "cap-2", "cap-3", injected_now())
                .unwrap()
        );
        let relations_ddl: String = rusqlite::Connection::open(&path)
            .unwrap()
            .query_row(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'relations'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert!(
            relations_ddl.contains("'proposes'")
                && relations_ddl.contains("'part_of'")
                && relations_ddl.contains("'grounded_in'"),
            "migrated relations CHECK must admit the full eight-kind set: {relations_ddl}"
        );
    }

    /// Review-mandated single-token migration RED (#143 follow-up): a
    /// faithful v19 shape — relations CHECK admits BOTH `proposes` and
    /// `part_of` but NOT `grounded_in` (the real post-#143 shape this
    /// integration's own `SCHEMA_VERSION` bump was gated on), `origin`
    /// column present, stamp 19. Only [`relations_lacks_grounded_in`] fires
    /// (the other two probes are already false); the SAME folded rebuild
    /// still runs (a store missing exactly one of the three tokens is not a
    /// special case), every pre-existing edge (including the legacy
    /// `proposes` and `part_of` edges) survives BYTE-EXACT, and the
    /// formerly missing kind writes afterward.
    #[test]
    fn proposes_and_part_of_present_converges_on_grounded_in_alone() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("memory.sqlite3");
        let before = {
            let mut store = Store::open(&path).unwrap();
            store
                .append(&capsule("both-present alpha", "nott"), injected_now())
                .unwrap(); // cap-1
            store
                .append(&capsule("both-present beta", "nott"), injected_now())
                .unwrap(); // cap-2
            store
                .append(&capsule("both-present gamma", "nott"), injected_now())
                .unwrap(); // cap-3
            store
                .upsert_relation(RelationKind::Blocks, "cap-1", "cap-2", injected_now())
                .unwrap();
            store
                .upsert_relation(RelationKind::Proposes, "cap-3", "cap-2", injected_now())
                .unwrap();
            store
                .upsert_relation(RelationKind::PartOf, "cap-1", "cap-3", injected_now())
                .unwrap();
            store.all_relations().unwrap()
        };
        // Downgrade to a FAITHFUL v19 shape: seven-kind CHECK (proposes AND
        // part_of present, grounded_in absent), origin column kept, stamp 19.
        {
            let conn = rusqlite::Connection::open(&path).unwrap();
            conn.execute_batch(
                "CREATE TABLE relations_v19 (
                     kind    TEXT NOT NULL CHECK (kind IN ('supersedes', 'derived_from', 'witnesses', 'blocks', 'falsifies', 'proposes', 'part_of')),
                     from_id TEXT NOT NULL,
                     to_id   TEXT NOT NULL,
                     at      TEXT NOT NULL,
                     origin  TEXT NOT NULL DEFAULT 'manual' CHECK (origin IN ('manual', 'import')),
                     PRIMARY KEY (kind, from_id, to_id)
                 );
                 INSERT INTO relations_v19 (kind, from_id, to_id, at, origin) \
                     SELECT kind, from_id, to_id, at, origin FROM relations;
                 DROP TABLE relations;
                 ALTER TABLE relations_v19 RENAME TO relations;
                 PRAGMA user_version = 19;",
            )
            .unwrap();
            // Faithful: this CHECK rejects the one still-missing kind.
            let raw = conn.execute(
                "INSERT INTO relations (kind, from_id, to_id, at, origin) \
                 VALUES ('grounded_in', 'cap-1', 'cap-3', '2026-07-24T00:00:00Z', 'manual')",
                [],
            );
            assert!(
                raw.is_err(),
                "the both-present v19 CHECK must reject a 'grounded_in' edge"
            );
        }

        // Opening IS the migration.
        let mut store = Store::open(&path).unwrap();
        let version: i64 = store
            .conn
            .query_row("PRAGMA user_version", [], |row| row.get(0))
            .unwrap();
        assert_eq!(version, SCHEMA_VERSION, "both-present file re-stamped");
        assert_eq!(SCHEMA_VERSION, 20);

        // Every edge (including the legacy proposes and part_of edges)
        // survives byte-exact.
        assert_eq!(
            store.all_relations().unwrap(),
            before,
            "every (kind, from, to, at, origin) row must survive the rebuild"
        );

        // The CHECK moved: the one formerly missing kind now writes.
        assert!(
            store
                .upsert_relation(RelationKind::GroundedIn, "cap-2", "cap-3", injected_now())
                .unwrap()
        );
        let relations_ddl: String = rusqlite::Connection::open(&path)
            .unwrap()
            .query_row(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'relations'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert!(
            relations_ddl.contains("'proposes'")
                && relations_ddl.contains("'part_of'")
                && relations_ddl.contains("'grounded_in'"),
            "migrated relations CHECK must admit the full eight-kind set: {relations_ddl}"
        );
    }
}
