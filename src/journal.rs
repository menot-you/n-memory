//! # Journal replay bridge (u6f) — divergence DETECTION, not reconstruction.
//!
//! The honest u6f rung, stated plainly: this module DETECTS divergence
//! between the store's canonical state and its append-only audit ledger; it
//! does NOT reconstruct a store from the ledger. Reconstruction is
//! impossible on the current ledger BY DESIGN — audit rows carry
//! `actor`/`action`/`subject`/`reason`, never capsule bytes — so a replay
//! engine that claimed to rebuild state from them would be fabricating
//! content. A content-bearing journal is a future, explicitly-migrated rung;
//! until it exists, verification is the whole truthful surface, and this
//! module is that surface.
//!
//! [`verify_replay`] reads the store once through PUBLIC APIs only
//! ([`Store::list_audit`], [`Store::canonical_snapshot`], [`Store::list`],
//! [`Store::list_tombstoned_ids`]) and answers a [`ReplayReport`] with three
//! deterministic legs:
//!
//! 1. **Chain** ([`ChainStatus`]): the w2-store2 hash chain, verified by
//!    [`Store::verify_chain`] — every audit row's `chained_hash` must
//!    re-derive as `sha256(prev_hash + canonical audit line)`. In-place
//!    edits, hash forgery, interior deletion, renumbering, and head cuts
//!    all answer [`ChainStatus::Broken`] naming the seq of the FIRST row
//!    whose link fails verification (for a deleted interior row that is
//!    the row AFTER the gap). Boundary, documented honestly: a PURE tail
//!    truncation (dropping the last k rows) leaves a correctly-linked
//!    prefix and is invisible to the chain itself — `journal_head` must
//!    be pinned OUTSIDE the file (e.g. a session close record) to detect
//!    it; the coverage leg still fires when a cut row named a still-live
//!    capsule. Trust-on-first-migration: a v2→v3 backfill vouches for
//!    whatever rows the pre-chain ledger held — pre-migration edits are
//!    invisible to the chain (store module docs). One more honest
//!    boundary: capsule append and its audit row are separate
//!    transactions at the surface, so a crash between them leaves an
//!    honest store reporting out_of_band, indistinguishable from tamper
//!    in the report (advisory law: the report closes nothing).
//! 2. **Snapshot digest**: SHA-256 hex over the byte-stable
//!    [`Store::canonical_snapshot`] — the replay comparand. Two stores that
//!    replayed the same mutation sequence answer the same digest; a
//!    divergent digest is the tamper/drift alarm for state (as the chain leg
//!    is for history).
//! 3. **Coverage** (`audit_covers_state` + `out_of_band`): every capsule id
//!    in the snapshot must be the `subject` of at least one audit event, and
//!    every tombstone must have its forget event ([`FORGET_ACTIONS`]).
//!    Each miss is listed in [`ReplayReport::out_of_band`], naming the id —
//!    a write that bypassed the audited surface (module audit policy: every
//!    mutation is audited by its call site). The check is deliberately
//!    one-directional: the ledger may legitimately name subjects OUTSIDE the
//!    snapshot (session brackets, tombstoned ids, deduplicated re-captures);
//!    only state without history is out of band, never history without
//!    state.
//!
//! Determinism: all three legs are pure functions of the store file — same
//! bytes, same [`ReplayReport`]. Advisory law: a report never blocks or
//! closes anything by itself; it is evidence for the owner/integrator to act
//! on.

use std::collections::BTreeSet;

use crate::capsule::sha256_hex;
use crate::store::{ListFilter, Store, StoreError};

/// The closed set of audit `action` values recognized as a forget event —
/// exactly the action the surface wires next to [`Store::forget_capsule`]
/// (`memory_forget`, the tool name). Extending this set is a deliberate,
/// reviewed change: a renamed surface action MUST surface here, or clean
/// forgets start reporting as out-of-band (detection doing its job).
pub const FORGET_ACTIONS: &[&str] = &["memory_forget"];

/// Outcome of the audit-ledger chain leg. This is the exact result shape of
/// the w2-store2 [`Store::verify_chain`] contract (`Ok(count)` on an intact
/// ledger; the first broken `seq` on tamper) — the chain leg IS that call.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ChainStatus {
    /// Every one of the `count` ledger rows re-derived its hash link.
    Ok(u64),
    /// The hash chain fails at ledger row `seq` — the first row whose
    /// `chained_hash` does not re-derive from its predecessor (for a
    /// deleted interior row: the row after the gap).
    Broken {
        /// `seq` of the first row failing verification.
        seq: i64,
    },
}

/// What [`verify_replay`] observed. Every field is deterministic evidence;
/// none of it closes an outcome by itself (advisory law).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ReplayReport {
    /// Audit-ledger chain leg: intact (`Ok(count)`) or first broken
    /// position.
    pub chain: ChainStatus,
    /// SHA-256 hex (64 lowercase chars) over the byte-stable
    /// [`Store::canonical_snapshot`] — the replay comparand. The empty
    /// store digests to the SHA-256 of the empty string.
    pub snapshot_digest: String,
    /// `true` iff every snapshot capsule is named by some audit event AND
    /// every tombstone has its forget event — i.e. [`Self::out_of_band`]
    /// is empty.
    pub audit_covers_state: bool,
    /// One line per coverage miss, naming the id: state that exists with
    /// no audit history to account for it. Deterministic order: snapshot
    /// (append) order first, then tombstones sorted by id.
    pub out_of_band: Vec<String>,
}

/// Errors crossing the replay boundary. (v6: the former `SnapshotLine`
/// variant was dead defense — the snapshot's lines are [`Store::list`]
/// rows the store itself just re-serialized, so coverage now reads the
/// typed list directly and no per-line parse exists to fail.)
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum ReplayError {
    /// A public store read failed.
    #[error("replay: {0}")]
    Store(#[from] StoreError),
}

/// Verify the store's audit ledger against its canonical state — the u6f
/// detection rung (see the module docs for exactly what each leg can and
/// cannot see). Reads the ledger ONCE; both the chain and coverage legs see
/// the same rows.
pub fn verify_replay(store: &Store) -> Result<ReplayReport, ReplayError> {
    // The ledger, ascending. `list_audit` answers seq-descending (the
    // natural audit view); reversing restores append order exactly.
    let mut ledger = store.list_audit(None, None)?;
    ledger.reverse();
    // Chain leg = the w2-store2 hash chain ([`Store::verify_chain`]):
    // every row's `chained_hash` must re-derive from its predecessor's.
    // This subsumes the pre-store2 seq-continuity bridge — a seq gap,
    // interior deletion, head cut, or in-place edit all break the first
    // following link. A PURE tail cut remains invisible to the chain
    // itself (`journal_head` must be pinned outside the file to catch
    // it); the coverage leg below still fires when the cut row named a
    // live capsule.
    let chain = match store.verify_chain() {
        Ok(count) => ChainStatus::Ok(count),
        Err(StoreError::JournalBroken { seq }) => ChainStatus::Broken { seq },
        Err(other) => return Err(ReplayError::Store(other)),
    };

    let snapshot = store.canonical_snapshot()?;
    let snapshot_digest = sha256_hex(snapshot.as_bytes());

    let mut out_of_band = Vec::new();
    let audited_subjects: BTreeSet<&str> =
        ledger.iter().map(|event| event.subject.as_str()).collect();
    // The snapshot's line set IS `list(ListFilter::default())`
    // re-serialized (the store builds it from that same call), so
    // coverage reads the ids through the typed list instead of
    // re-parsing snapshot lines — a parse could only ever fail on bytes
    // the store itself just produced.
    for stored in store.list(ListFilter::default())? {
        let id = stored.id.as_str();
        if !audited_subjects.contains(id) {
            out_of_band.push(format!(
                "capsule {id}: present in canonical snapshot, named by no audit event"
            ));
        }
    }
    for id in store.list_tombstoned_ids()? {
        let has_forget_event = ledger
            .iter()
            .any(|event| event.subject == id && FORGET_ACTIONS.contains(&event.action.as_str()));
        if !has_forget_event {
            let recognized = FORGET_ACTIONS.join(", ");
            out_of_band.push(format!(
                "tombstone {id}: no forget audit event (recognized actions: {recognized})"
            ));
        }
    }

    let audit_covers_state = out_of_band.is_empty();
    Ok(ReplayReport {
        chain,
        snapshot_digest,
        audit_covers_state,
        out_of_band,
    })
}

#[cfg(test)]
mod tests {
    #![allow(
        clippy::unwrap_used,
        clippy::expect_used,
        reason = "tests use unwrap/expect so fixture failures fail at the assertion site"
    )]

    use super::*;
    use crate::capsule::{
        AuthorityClass, Capsule, Confidence, Freshness, Provenance, Scope, sha256_hex,
    };
    use crate::store::TombstoneMode;
    use rusqlite::{Connection, params};
    use time::OffsetDateTime;
    use time::macros::datetime;

    /// Fixed injected boundary instant (the store reads no clock).
    fn injected_now() -> OffsetDateTime {
        datetime!(2001-02-03 04:05:06.123456789 +02:00)
    }

    /// Distinct `text` ⇒ distinct `source_hash`, so fixtures never collide
    /// unless a test wants them to.
    fn capsule(text: &str) -> Capsule {
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
                project_id: "nmemory".to_string(),
            },
            AuthorityClass::UserStated,
            false,
        )
        .unwrap()
    }

    /// Append a capsule AND its capture audit row — the module audit
    /// policy a real surface follows (this test plays the surface).
    fn audited_append(store: &mut Store, text: &str) -> String {
        let id = store.append(&capsule(text), injected_now()).unwrap();
        store
            .append_audit("test-actor", "captured", id.as_str(), None, injected_now())
            .unwrap();
        id.as_str().to_string()
    }

    #[test]
    fn clean_store_is_all_green() {
        let mut store = Store::open_in_memory().unwrap();
        let cap1 = audited_append(&mut store, "alpha fact");
        let cap2 = audited_append(&mut store, "beta fact");
        // A non-capsule audit subject (session bracket) must not trip
        // coverage: the check is state → ledger, never ledger → state.
        store
            .append_audit(
                "test-actor",
                "memory_session_start",
                "sess-1",
                None,
                injected_now(),
            )
            .unwrap();
        // A clean forget: content destroyed AND its forget event recorded.
        store
            .forget_capsule(
                &cap2,
                TombstoneMode::Purged,
                "test cleanup",
                b"test-key",
                injected_now(),
            )
            .unwrap();
        store
            .append_audit(
                "test-actor",
                "memory_forget",
                &cap2,
                Some("test cleanup"),
                injected_now(),
            )
            .unwrap();

        let report = verify_replay(&store).unwrap();
        assert_eq!(report.chain, ChainStatus::Ok(4));
        assert_eq!(
            report.snapshot_digest,
            sha256_hex(store.canonical_snapshot().unwrap().as_bytes())
        );
        assert_eq!(report.snapshot_digest.len(), 64);
        assert!(report.audit_covers_state);
        assert_eq!(report.out_of_band, Vec::<String>::new());
        // The live capsule is still cap-1; the snapshot digest covers it.
        assert_eq!(
            store.list(ListFilter::default()).unwrap()[0].id.as_str(),
            cap1
        );
    }

    #[test]
    fn empty_store_is_green_with_the_empty_digest() {
        let store = Store::open_in_memory().unwrap();
        let report = verify_replay(&store).unwrap();
        assert_eq!(report.chain, ChainStatus::Ok(0));
        // SHA-256 of the empty string — the canonical empty-store digest.
        assert_eq!(
            report.snapshot_digest,
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        );
        assert!(report.audit_covers_state);
        assert!(report.out_of_band.is_empty());
    }

    #[test]
    fn replay_determinism_twin_stores_answer_identical_reports() {
        let build = || {
            let mut store = Store::open_in_memory().unwrap();
            audited_append(&mut store, "alpha fact");
            audited_append(&mut store, "beta fact");
            store
        };
        let a = verify_replay(&build()).unwrap();
        let b = verify_replay(&build()).unwrap();
        assert_eq!(a, b);
        assert_eq!(a.chain, ChainStatus::Ok(2));
    }

    #[test]
    fn hand_inserted_capsule_bypassing_audit_is_named_out_of_band() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("journal-test.db");
        {
            let mut store = Store::open(&path).unwrap();
            audited_append(&mut store, "honest fact");
        }
        // Tamper: a capsule row written straight into SQLite, bypassing the
        // audited surface entirely.
        {
            let smuggled = capsule("smuggled fact nobody audited");
            let canonical_json = smuggled.to_canonical_json().unwrap();
            let conn = Connection::open(&path).unwrap();
            conn.execute(
                "INSERT INTO capsules \
                 (seq, id, canonical_json, created_at, source_hash, project_id, \
                  authority_class, valid_from, session_id) \
                 VALUES (2, 'cap-2', ?1, '2001-02-03T04:05:06Z', ?2, 'nmemory', \
                         'user-stated', '2026-07-18T12:30:45Z', NULL)",
                params![canonical_json, smuggled.provenance().source_hash.as_str()],
            )
            .unwrap();
            conn.execute(
                "INSERT INTO capsules_fts (rowid, content) VALUES (2, ?1)",
                params![smuggled.content()],
            )
            .unwrap();
        }

        let store = Store::open(&path).unwrap();
        let report = verify_replay(&store).unwrap();
        assert_eq!(report.chain, ChainStatus::Ok(1));
        assert!(!report.audit_covers_state);
        assert_eq!(
            report.out_of_band,
            vec![
                "capsule cap-2: present in canonical snapshot, named by no audit event".to_string()
            ]
        );
        // The digest still covers BOTH lines — state is reported as it is.
        assert_eq!(
            report.snapshot_digest,
            sha256_hex(store.canonical_snapshot().unwrap().as_bytes())
        );
    }

    /// v6 negative: a chain failing at POSITION 1 — the head row's stored
    /// hash rewritten in place, so the very first link fails re-derivation
    /// (`prev_hash = ""` + canonical line 1 no longer answers the stored
    /// value). Interior-gap tests start at seq 3; this pins the boundary.
    #[test]
    fn in_place_edit_of_the_first_row_breaks_the_chain_at_position_1() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("journal-test.db");
        {
            let mut store = Store::open(&path).unwrap();
            audited_append(&mut store, "alpha fact");
            audited_append(&mut store, "beta fact");
        }
        {
            let conn = Connection::open(&path).unwrap();
            conn.execute(
                "UPDATE audit_events SET chained_hash = 'deadbeef' WHERE seq = 1",
                [],
            )
            .unwrap();
        }

        let store = Store::open(&path).unwrap();
        let report = verify_replay(&store).unwrap();
        assert_eq!(report.chain, ChainStatus::Broken { seq: 1 });
        // Coverage is a separate leg: both capsules keep their history.
        assert!(report.audit_covers_state);
    }

    /// v6: the non-empty snapshot digest pinned to a hand-carried literal
    /// over a fully deterministic fixture. The clean-store test recomputes
    /// its expectation from `canonical_snapshot` (both sides drift
    /// together); THIS assertion breaks on any serializer/snapshot drift.
    #[test]
    fn snapshot_digest_of_the_fixed_two_capsule_fixture_is_pinned() {
        let mut store = Store::open_in_memory().unwrap();
        audited_append(&mut store, "alpha fact");
        audited_append(&mut store, "beta fact");
        let report = verify_replay(&store).unwrap();
        assert_eq!(
            report.snapshot_digest,
            "f64525e6247d2a9a475485bbc3b24863f8bd8b80010938ae3e647880829c9c98"
        );
    }

    #[test]
    fn tampered_audit_interior_deletion_breaks_the_chain_at_the_gap() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("journal-test.db");
        {
            let mut store = Store::open(&path).unwrap();
            audited_append(&mut store, "alpha fact");
            audited_append(&mut store, "beta fact");
            audited_append(&mut store, "gamma fact");
        }
        // Tamper: erase the ledger's second row.
        {
            let conn = Connection::open(&path).unwrap();
            conn.execute("DELETE FROM audit_events WHERE seq = 2", [])
                .unwrap();
        }

        let store = Store::open(&path).unwrap();
        let report = verify_replay(&store).unwrap();
        // Hash-chain semantics: the row AFTER the gap is the first whose
        // link fails to re-derive (seq 3 chained to the erased row 2).
        assert_eq!(report.chain, ChainStatus::Broken { seq: 3 });
        // The erased row was cap-2's only history — coverage flags it too.
        assert!(!report.audit_covers_state);
        assert_eq!(
            report.out_of_band,
            vec![
                "capsule cap-2: present in canonical snapshot, named by no audit event".to_string()
            ]
        );
    }

    #[test]
    fn forget_without_its_forget_event_is_out_of_band() {
        let mut store = Store::open_in_memory().unwrap();
        let cap1 = audited_append(&mut store, "soon forgotten");
        // Content destroyed with NO forget event recorded.
        store
            .forget_capsule(
                &cap1,
                TombstoneMode::Purged,
                "test cleanup",
                b"test-key",
                injected_now(),
            )
            .unwrap();

        let report = verify_replay(&store).unwrap();
        assert_eq!(report.chain, ChainStatus::Ok(1));
        assert!(!report.audit_covers_state);
        assert_eq!(
            report.out_of_band,
            vec![
                "tombstone cap-1: no forget audit event (recognized actions: memory_forget)"
                    .to_string()
            ]
        );
    }

    #[test]
    fn tail_deletion_is_invisible_to_continuity_but_caught_by_coverage() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("journal-test.db");
        {
            let mut store = Store::open(&path).unwrap();
            audited_append(&mut store, "alpha fact");
            audited_append(&mut store, "beta fact");
        }
        // Tamper: cut the ledger's TAIL row. The surviving prefix is a
        // correctly-linked hash chain, so the chain leg alone cannot see
        // a pure tail cut (the documented boundary — external
        // `journal_head` pinning closes it) — but the cut row named
        // cap-2, so the coverage leg still catches the tamper.
        {
            let conn = Connection::open(&path).unwrap();
            conn.execute("DELETE FROM audit_events WHERE seq = 2", [])
                .unwrap();
        }

        let store = Store::open(&path).unwrap();
        let report = verify_replay(&store).unwrap();
        assert_eq!(report.chain, ChainStatus::Ok(1));
        assert!(!report.audit_covers_state);
        assert_eq!(
            report.out_of_band,
            vec![
                "capsule cap-2: present in canonical snapshot, named by no audit event".to_string()
            ]
        );
    }
}
