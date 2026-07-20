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

use std::fmt;
use std::path::Path;

use hmac::{Hmac, KeyInit, Mac};
use rusqlite::{Connection, OptionalExtension, TransactionBehavior, params};
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
    kind    TEXT NOT NULL CHECK (kind IN ('supersedes', 'derived_from', 'witnesses', 'blocks', 'falsifies')),
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
    provenance_anchor TEXT
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
/// query term of a `missing_evidence` or `abstain` outcome
/// ([`Store::record_recall_miss`]); a `grounded` outcome records nothing.
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
/// touched). Version 1–9 files migrate in place via [`migrate_to_current`];
/// versions this build does not know fail closed
/// ([`StoreError::UnsupportedSchemaVersion`]). Every migration step keys on
/// the observed DDL shape (`relations_has_old_check` /
/// `classifications_has_old_check`) or `IF NOT EXISTS`, never on the
/// version integer, so the stamp renumbers mechanically when lanes land
/// out of authoring order.
const SCHEMA_VERSION: i64 = 10;

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
        "store: unsupported schema version {0} (this build migrates v1..=9 in place and reads v{SCHEMA_VERSION} natively)"
    )]
    UnsupportedSchemaVersion(i64),
    /// A relation/classification endpoint named a capsule id that is not
    /// stored — nothing was recorded.
    #[error("store: operation references unknown capsule {0}")]
    UnknownCapsule(String),
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
        }
    }
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

/// The five declared relation kinds (donor B closed enum — mcps/memory-
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
///
/// `falsifies` (u6h) is unique: its `from_id` may name an OUTCOME record
/// (`out-<n>`, [`Store::append_outcome`]) as well as a capsule, and its
/// target becomes recall-ineligible (a fence in `crate::retrieve`, not a
/// state change — [`Store::is_falsified`]).
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
}

impl RelationKind {
    /// All declared kinds, in contract order.
    pub const ALL: [RelationKind; 5] = [
        RelationKind::Supersedes,
        RelationKind::DerivedFrom,
        RelationKind::Witnesses,
        RelationKind::Blocks,
        RelationKind::Falsifies,
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

/// The closed outcome set a recall miss records (u-r5 miss-ledger): the
/// two UNGROUNDED [`crate::retrieve::RetrieveResponse`] outcomes. A
/// `grounded` outcome is NOT a miss and has no variant here — it records
/// nothing. Wire names match the response tags exactly and the SQL CHECK
/// in [`RECALL_MISSES_DDL`], so an illegal outcome is unrepresentable at
/// the type layer before the CHECK ever sees it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RecallMissOutcome {
    /// Terms matched stored capsules but every match was fenced out.
    MissingEvidence,
    /// Zero raw matches — the honest empty answer.
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
            0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | SCHEMA_VERSION => {
                migrate_to_current(&mut conn)?
            }
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
        self.append_inner(capsule, None, now)
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
            Some(None) => self.append_inner(capsule, Some(session_id), now),
        }
    }

    fn append_inner(
        &mut self,
        capsule: &Capsule,
        session_id: Option<&str>,
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

    /// List LIVE capsules in append (`seq`) order, optionally fenced to a
    /// project and/or a project-prefix subtree ([`ListFilter`]; present
    /// fences AND-compose). `limit` keeps the NEWEST rows (the tail of the
    /// append order — "show my recent memories" is the operative ask on a
    /// memory index); the returned slice itself stays in ascending append
    /// order. Tombstoned rows are excluded — they have no capsule bytes to
    /// list; their markers live in [`Store::get_tombstone`].
    pub fn list(&self, filter: ListFilter) -> Result<Vec<StoredCapsule>, StoreError> {
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
    ///   `synonyms`, `usage`, `capsules_fts`) are EXCLUDED by documented
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
        self.search_fts_scoped(terms, project_id, None)
    }

    /// [`Store::search_fts`] with the full w2 scope fences: `project_id`
    /// (exact) and `project_prefix` (the [`ListFilter::project_prefix`]
    /// subtree rule — `project_id == p` OR starting with `p + "/"`).
    /// Present fences AND-compose; both `None` is the unfenced search.
    /// Everything else — match semantics, quoting, ordering, tombstone
    /// exclusion — is exactly [`Store::search_fts`], which delegates here.
    pub fn search_fts_scoped(
        &self,
        terms: &[String],
        project_id: Option<&str>,
        project_prefix: Option<&str>,
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
                 ORDER BY score, c.seq",
            )
            .map_err(backend)?;
        let rows = stmt
            .query_map(
                params![match_expr, project_id, project_prefix],
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
    /// capsule ([`StoreError::UnknownCapsule`]). No verb updates or deletes a
    /// row. `at` is the INJECTED `now` — the store reads no clock.
    pub fn append_outcome(
        &mut self,
        description: &str,
        actor: &str,
        evidence_ref: Option<&str>,
        capsule_id: Option<&str>,
        now: OffsetDateTime,
    ) -> Result<OutcomeRecord, StoreError> {
        let at = rfc3339_text(now)?;
        let tx = self.conn.transaction().map_err(backend)?;
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
             (seq, id, description, actor, evidence_ref, capsule_id, at) \
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
            params![
                seq,
                record.id,
                record.description,
                record.actor,
                record.evidence_ref,
                record.capsule_id,
                at
            ],
        )
        .map_err(backend)?;
        tx.commit().map_err(backend)?;
        Ok(record)
    }

    /// Every outcome record, in append order (`seq` asc) — the deterministic
    /// list surface (u6h). Empty on a fresh store; never an error. Rows
    /// re-validate through [`OutcomeRecord::new`] on the way out, the same
    /// read-revalidation discipline the capsule/relation reads use.
    pub fn list_outcomes(&self) -> Result<Vec<OutcomeRecord>, StoreError> {
        let mut stmt = self
            .conn
            .prepare(
                "SELECT id, description, actor, evidence_ref, capsule_id, at \
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

    /// Append the recall-miss ledger rows for one ungrounded query (u-r5
    /// miss-ledger; see [`RECALL_MISSES_DDL`]): fold each `term` exactly
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
    /// Telemetry semantics: recall calls this FAIL-OPEN ([`crate::retrieve`]
    /// swallows the `Err`) so a ledger write can never fail or delay the
    /// retrieve — the deliberate exception to the crate's fail-closed
    /// default. The method itself still returns the error HONESTLY; the
    /// swallow lives at exactly one call site.
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
                                     provenance_source, provenance_anchor) \
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
            params![
                id,
                mode.as_str(),
                content_hmac,
                at,
                reason,
                provenance_source,
                provenance_anchor
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
                        provenance_source, provenance_anchor \
                 FROM tombstones WHERE capsule_id = ?1",
                [id],
                |row| {
                    Ok((
                        row.get::<_, String>(0)?,
                        row.get::<_, String>(1)?,
                        row.get::<_, String>(2)?,
                        row.get::<_, String>(3)?,
                        row.get::<_, String>(4)?,
                        row.get::<_, Option<String>>(5)?,
                        row.get::<_, Option<String>>(6)?,
                    ))
                },
            )
            .optional()
            .map_err(backend)?;
        match row {
            None => Ok(None),
            Some((
                capsule_id,
                mode_text,
                content_hmac,
                at_text,
                reason,
                provenance_source,
                provenance_anchor,
            )) => {
                let mode =
                    TombstoneMode::from_wire(&mode_text).ok_or_else(|| StoreError::Corrupt {
                        id: capsule_id.clone(),
                        reason: format!("tombstones.mode: unknown value {mode_text:?}"),
                    })?;
                let at = parse_at(&capsule_id, "tombstones.at", &at_text)?;
                Ok(Some(TombstoneRecord {
                    capsule_id,
                    mode,
                    content_hmac,
                    at,
                    reason,
                    provenance_source,
                    provenance_anchor,
                }))
            }
        }
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
    /// `project_prefix` subtree, AND-composed; a `None` disables its clause,
    /// `substr` keeps prefix bytes metacharacter-free). Tombstoned
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
    ) -> Result<Vec<(StoredCapsule, StoredEmbedding)>, StoreError> {
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
                 ORDER BY c.seq",
            )
            .map_err(backend)?;
        let rows = stmt
            .query_map(params![project_id, project_prefix], |row| {
                let raw = row_to_raw(row)?;
                let dimension: i64 = row.get(5)?;
                let model_tag: String = row.get(6)?;
                let blob: Vec<u8> = row.get(7)?;
                Ok((raw, dimension, model_tag, blob))
            })
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

/// Raw outcome row (u6h); decoded outside the closure so a re-validation
/// failure surfaces as a typed [`StoreError`], not a stringified backend
/// error — the same pattern as [`RawRelation`].
struct RawOutcome {
    id: String,
    description: String,
    actor: String,
    evidence_ref: Option<String>,
    capsule_id: Option<String>,
    at: String,
}

fn row_to_outcome(row: &rusqlite::Row<'_>) -> rusqlite::Result<RawOutcome> {
    Ok(RawOutcome {
        id: row.get(0)?,
        description: row.get(1)?,
        actor: row.get(2)?,
        evidence_ref: row.get(3)?,
        capsule_id: row.get(4)?,
        at: row.get(5)?,
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

fn backend(e: rusqlite::Error) -> StoreError {
    StoreError::Backend(e.to_string())
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

        // One past the current stamp (v10): a version this build cannot
        // know is refused, never guessed at.
        let conn = rusqlite::Connection::open(&path).unwrap();
        conn.execute_batch("PRAGMA user_version = 11").unwrap();
        drop(conn);

        let err = Store::open(&path).unwrap_err();
        assert_eq!(err, StoreError::UnsupportedSchemaVersion(11));
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
            .append_outcome("observed", "tester", None, Some("cap-1"), injected_now())
            .unwrap();
        assert_eq!(outcome.id, "out-1");
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
            store.search_fts_scoped(&terms, None, None).unwrap().len(),
            4
        );

        // Prefix fence: subtree only — nott + nott/x, never nottx.
        let fenced = store.search_fts_scoped(&terms, None, Some("nott")).unwrap();
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
                .search_fts_scoped(&terms, Some("nottx"), None)
                .unwrap()
                .len(),
            1
        );
        assert_eq!(
            store
                .search_fts_scoped(&terms, Some("nott/x"), Some("nott"))
                .unwrap()
                .len(),
            1
        );
        assert!(
            store
                .search_fts_scoped(&terms, Some("other"), Some("nott"))
                .unwrap()
                .is_empty()
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
        let scoped = store.embeddings_for_recall(None, Some("nott")).unwrap();
        let ids: Vec<&str> = scoped.iter().map(|(s, _)| s.id.as_str()).collect();
        assert_eq!(ids, vec!["cap-1", "cap-2"]);
        // Forget cap-1: its embedding row remains but the capsule is
        // tombstoned, so recall (live only) must drop it.
        let key = [7u8; 32];
        store
            .forget_capsule("cap-1", TombstoneMode::Purged, "test", &key, later_now())
            .unwrap();
        let live = store.embeddings_for_recall(Some("nott"), None).unwrap();
        let live_ids: Vec<&str> = live.iter().map(|(s, _)| s.id.as_str()).collect();
        assert_eq!(
            live_ids,
            Vec::<&str>::new(),
            "tombstoned capsule drops from the lane"
        );
    }

    /// A fresh store is stamped at the current version and carries the
    /// `embeddings` table.
    #[test]
    fn fresh_store_is_current_version_with_embeddings_table() {
        let store = Store::open_in_memory().unwrap();
        let version: i64 = store
            .conn
            .query_row("PRAGMA user_version", [], |r| r.get(0))
            .unwrap();
        assert_eq!(version, SCHEMA_VERSION);
        assert_eq!(
            SCHEMA_VERSION, 10,
            "u6a vector took slot 5, u6h/u6i substrates took slot 6, \
             u-r11 kind-vocabulary took slot 7, u-r2 anchor-drift + \
             epistemics took slot 8, u-r5 miss-ledger took slot 9, \
             u-r8-REDESIGN stale-import-supersession took slot 10"
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
            .append_outcome("observed", "tester", None, Some("cap-1"), injected_now())
            .unwrap();
        assert!(
            store
                .upsert_relation(
                    RelationKind::Falsifies,
                    &outcome.id,
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

        // A second missing query carrying "tokio" again — missing_evidence
        // this time; the ledger does not care which ungrounded outcome.
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
}
