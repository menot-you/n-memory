//! # Conformance — ingest idempotency + provenance (unit h2).
//!
//! Ported donor behavior (zero authority, reference only): donor B's
//! `mcps/memory/tests/ingest_episodes.rs` @6d495898 — fixture-pack replay
//! with 100% source_hash recomputation from PERSISTED content, 100% anchor
//! validity, second-pass idempotency with 0 net-new rows, fail-closed
//! rejections that store nothing, and the two tampering adverse cases —
//! plus the restart-idempotency slice of `persistent_default_store.rs`
//! (source_hash-based dedup, not in-process state). Re-authored against
//! the new crate API: `ingest(store, request, defaults, now)` over
//! `IngestRequest` (the episode shape is donor scope; the new capture verb
//! takes content + provenance directly).
//!
//! The donor's Python fixture pipeline (`derive_episodes.py` →
//! `episodes.json`, 50 real transcript-derived episodes) is NOT carried in
//! any form. The replay pack here is `tests/fixtures/ingest_pack.json` —
//! hand-authored synthetic requests in the NEW request shape, varying
//! anchor forms (path:line[:col], doc-<id>[#fragment], &<id>, PR refs,
//! 1-line and 20-line stubs), multi-line and unicode content, and a
//! near-twin pair that must NOT collapse (h4 live: the second twin earns
//! a dedup HINT instead — advisory, never an auto-merge).
//!
//! Donor test consciously not carried and not deferred to a named unit:
//! `fixture_privacy.rs` guarded the donor's real-transcript fixture
//! pipeline; this pack is hand-authored synthetic content and
//! `PackEntry`'s `deny_unknown_fields` enforces the structural shape.

#![allow(
    clippy::unwrap_used,
    clippy::expect_used,
    reason = "tests use unwrap/expect so fixture failures fail at the assertion site"
)]

use std::collections::HashSet;

use nmemory::capsule::{CapsuleError, sha256_hex};
use nmemory::ingest::{
    DEDUP_HINT_MIN_SCORE, IngestDefaults, IngestError, IngestRequest, MAX_ANCHOR_STUB_LINES, ingest,
};
use nmemory::store::{CapsuleId, ListFilter, Store};
use serde::Deserialize;
use time::OffsetDateTime;
use time::macros::datetime;

const PACK_JSON: &str = include_str!("fixtures/ingest_pack.json");

/// Fixed injected boundary instant — the tests inject `now` exactly like
/// the s5 surface will; ingest itself reads no clock.
const NOW: OffsetDateTime = datetime!(2026-07-18 12:00:00 UTC);

/// One pack entry: the two decisions the new capture shape requires
/// (content + provenance pair); every override left to smart defaults.
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
struct PackEntry {
    content: String,
    source: String,
    anchor: String,
}

impl PackEntry {
    fn request(&self) -> IngestRequest {
        IngestRequest {
            content: self.content.clone(),
            source: self.source.clone(),
            anchor: self.anchor.clone(),
            confidence: None,
            valid_from: None,
            valid_to: None,
            project_id: None,
            authority_class: None,
            instruction_taint: None,
            supersedes: None,
            session_id: None,
        }
    }
}

fn load_pack() -> Vec<PackEntry> {
    let pack: Vec<PackEntry> =
        serde_json::from_str(PACK_JSON).expect("fixture pack parses as Vec<PackEntry>");
    assert!(
        pack.len() >= 10,
        "the replay pack must carry >=10 entries, got {}",
        pack.len()
    );
    pack
}

fn defaults() -> IngestDefaults {
    IngestDefaults {
        project_id: "nott".to_string(),
    }
}

#[test]
fn pack_contents_are_distinct_so_no_entry_self_dedupes() {
    let pack = load_pack();
    let contents: HashSet<&str> = pack.iter().map(|e| e.content.as_str()).collect();
    assert_eq!(
        contents.len(),
        pack.len(),
        "every pack entry must carry distinct content (distinct source_hash)"
    );
}

/// The donor's central replay gate, on the new shape: pass 1 records every
/// entry with a unique id; persisted state re-verifies 100% (source_hash
/// recomputes from the PERSISTED content, anchors obey the stored-anchor
/// law); pass 2 over the identical pack is idempotent — same ids in the
/// same order, 0 net-new rows.
#[test]
fn replaying_the_pack_is_recorded_verified_and_idempotent() {
    let pack = load_pack();
    let mut store = Store::open_in_memory().expect("open");

    // The pack's near-twin pair shares its significant vocabulary while
    // differing in bytes; with the h4 hint live, the SECOND twin must earn
    // a dedup hint pointing at the first (advisory only — it still records
    // as its own row), and every other entry is novel enough to earn none.
    let twin_indices: Vec<usize> = pack
        .iter()
        .enumerate()
        .filter(|(_, e)| e.content.starts_with("the ssot remote sync epic lives at"))
        .map(|(i, _)| i)
        .collect();
    assert_eq!(
        twin_indices.len(),
        2,
        "the pack must carry exactly one near-twin pair"
    );

    // --- Pass 1: every entry newly recorded, unique capsule ids.
    let mut first_pass_ids: Vec<CapsuleId> = Vec::with_capacity(pack.len());
    let mut seen: HashSet<String> = HashSet::new();
    for (index, entry) in pack.iter().enumerate() {
        let outcome = ingest(&mut store, entry.request(), defaults(), NOW)
            .expect("ingest must succeed for every pack entry");
        assert!(!outcome.deduped, "pass 1 must record, not dedup");
        match &outcome.dedup_hint {
            Some(hint) if index == twin_indices[1] => {
                assert_eq!(
                    hint.similar_id.as_str(),
                    first_pass_ids[twin_indices[0]].as_str(),
                    "the near-twin hint must point at the first twin"
                );
                assert!(
                    (DEDUP_HINT_MIN_SCORE..=1.0).contains(&hint.score),
                    "hint score out of range: {}",
                    hint.score
                );
            }
            Some(hint) => panic!("unexpected dedup hint {hint:?} on pack entry {index}"),
            None => assert_ne!(
                index, twin_indices[1],
                "the near-twin's second entry must earn a dedup hint"
            ),
        }
        assert!(
            seen.insert(outcome.id.to_string()),
            "capsule id {} was reused across distinct entries",
            outcome.id
        );
        first_pass_ids.push(outcome.id);
    }

    let rows_after_pass_1 = store.list(ListFilter::default()).expect("list").len();
    assert_eq!(
        rows_after_pass_1,
        pack.len(),
        "one row per entry after the first pass"
    );

    // --- 100% verification from PERSISTED state, not the caller's copy.
    let mut hash_matches = 0usize;
    let mut anchor_valid = 0usize;
    for stored in store.list(ListFilter::default()).expect("list") {
        let capsule = &stored.capsule;
        if capsule.provenance().source_hash == sha256_hex(capsule.content().as_bytes()) {
            hash_matches += 1;
        }
        // The stored-anchor law (s3): non-empty, at most 20 raw lines.
        let anchor = &capsule.provenance().anchor;
        if !anchor.trim().is_empty() && anchor.lines().count() <= MAX_ANCHOR_STUB_LINES {
            anchor_valid += 1;
        }
    }
    assert_eq!(
        hash_matches,
        pack.len(),
        "100% source_hash recomputation match from persisted content required"
    );
    assert_eq!(
        anchor_valid,
        pack.len(),
        "100% law-valid anchors on persisted capsules required"
    );

    // --- Pass 2: identical pack → every result deduped onto the SAME id,
    //     in order, and no new row.
    let mut second_pass_ids = Vec::with_capacity(pack.len());
    for entry in &pack {
        let outcome =
            ingest(&mut store, entry.request(), defaults(), NOW).expect("re-ingest must succeed");
        assert!(outcome.deduped, "pass 2 must dedup byte-identical content");
        second_pass_ids.push(outcome.id);
    }
    assert_eq!(
        first_pass_ids, second_pass_ids,
        "re-ingest must return the SAME capsule id per entry, in order"
    );
    let rows_after_pass_2 = store.list(ListFilter::default()).expect("list").len();
    assert_eq!(
        rows_after_pass_2, rows_after_pass_1,
        "re-feeding the identical pack must produce 0 net-new rows"
    );
}

// --- Fail-closed adverse cases: a rejected capture stores NOTHING. ---

fn base_entry() -> PackEntry {
    load_pack().into_iter().next().expect("pack non-empty")
}

#[test]
fn missing_source_is_rejected_and_stores_nothing() {
    let mut store = Store::open_in_memory().expect("open");
    let mut entry = base_entry();
    entry.source = "   ".to_string();
    let err = ingest(&mut store, entry.request(), defaults(), NOW)
        .expect_err("empty source must be rejected");
    assert_eq!(err, IngestError::ProvenanceMissing("source"));
    assert!(store.list(ListFilter::default()).expect("list").is_empty());
}

#[test]
fn missing_anchor_is_rejected_and_stores_nothing() {
    let mut store = Store::open_in_memory().expect("open");
    let mut entry = base_entry();
    entry.anchor = String::new();
    let err = ingest(&mut store, entry.request(), defaults(), NOW)
        .expect_err("empty anchor must be rejected");
    assert_eq!(err, IngestError::ProvenanceMissing("anchor"));
    assert!(store.list(ListFilter::default()).expect("list").is_empty());
}

#[test]
fn empty_content_is_rejected_and_stores_nothing() {
    let mut store = Store::open_in_memory().expect("open");
    let mut entry = base_entry();
    entry.content = "  \n ".to_string();
    let err = ingest(&mut store, entry.request(), defaults(), NOW)
        .expect_err("blank content must be rejected");
    assert_eq!(err, IngestError::Capsule(CapsuleError::EmptyContent));
    assert!(store.list(ListFilter::default()).expect("list").is_empty());
}

#[test]
fn overlong_anchor_is_rejected_and_stores_nothing() {
    // Donor's invalid-anchor rejection, under the new anchor law: every
    // single-line anchor is a valid 1-line stub now, so the one rejectable
    // shape is a stub past MAX_ANCHOR_STUB_LINES.
    let mut store = Store::open_in_memory().expect("open");
    let mut entry = base_entry();
    entry.anchor = (1..=MAX_ANCHOR_STUB_LINES + 1)
        .map(|n| format!("stub line {n}"))
        .collect::<Vec<_>>()
        .join("\n");
    let err = ingest(&mut store, entry.request(), defaults(), NOW)
        .expect_err("overlong stub anchor must be rejected");
    assert_eq!(
        err,
        IngestError::InvalidAnchor {
            lines: MAX_ANCHOR_STUB_LINES + 1
        }
    );
    assert!(store.list(ListFilter::default()).expect("list").is_empty());
}

// --- Tampering adverse cases (donor §3.6 r1). ---

/// Flip exactly one character (append a marker if the string cannot absorb
/// an in-place flip), guaranteeing the output differs from the input.
fn flip_one_byte(s: &str) -> String {
    let mut chars: Vec<char> = s.chars().collect();
    match chars.first_mut() {
        Some(c) => *c = if *c == 'z' { 'a' } else { 'z' },
        None => chars.push('z'),
    }
    chars.into_iter().collect()
}

#[test]
fn tampering_one_byte_is_a_legitimate_new_row_not_a_duplicate() {
    let mut store = Store::open_in_memory().expect("open");
    let original = base_entry();
    let original_outcome =
        ingest(&mut store, original.request(), defaults(), NOW).expect("ingest original");
    assert!(!original_outcome.deduped);
    let original_hash = store
        .get(original_outcome.id.as_str())
        .expect("get")
        .expect("stored")
        .capsule
        .provenance()
        .source_hash
        .clone();

    // Mutate exactly one byte of the SAME logical capture.
    let mut tampered = original.clone();
    tampered.content = flip_one_byte(&tampered.content);
    assert_ne!(tampered.content, original.content, "mutation took effect");
    assert_ne!(
        sha256_hex(tampered.content.as_bytes()),
        original_hash,
        "flipping any byte of the content must change source_hash"
    );

    let tampered_outcome =
        ingest(&mut store, tampered.request(), defaults(), NOW).expect("ingest tampered");
    // NOT a duplicate of the original: a different source_hash is a
    // legitimate new row (idempotency is about the SAME bytes, never
    // about collapsing different payloads together).
    assert!(!tampered_outcome.deduped);
    assert_ne!(tampered_outcome.id, original_outcome.id);
    assert_eq!(
        store.list(ListFilter::default()).expect("list").len(),
        2,
        "original + tampered must both persist as distinct rows"
    );
}

#[test]
fn tampered_persisted_content_fails_hash_recomputation_detectably() {
    let mut store = Store::open_in_memory().expect("open");
    let entry = base_entry();
    let outcome = ingest(&mut store, entry.request(), defaults(), NOW).expect("ingest");

    let stored = store
        .get(outcome.id.as_str())
        .expect("get")
        .expect("stored capsule exists");
    let genuine_content = stored.capsule.content().to_string();
    let genuine_hash = stored.capsule.provenance().source_hash.clone();

    // Sanity: untouched persisted content recomputes to its own hash.
    assert_eq!(
        sha256_hex(genuine_content.as_bytes()),
        genuine_hash,
        "genuine persisted content must match its own row's source_hash"
    );

    // Simulate the persisted bytes corrupted out-of-band: flip one byte of
    // the EXACT content that was stored — the recomputation mismatch IS
    // the detectable-tamper signal.
    let corrupted = flip_one_byte(&genuine_content);
    assert_ne!(corrupted, genuine_content, "corruption took effect");
    assert_ne!(
        sha256_hex(corrupted.as_bytes()),
        genuine_hash,
        "recomputing from corrupted persisted content must mismatch the row's source_hash"
    );
}

/// Ported from `persistent_default_store.rs` (the crate-scope slice):
/// idempotency survives a restart because it is source_hash-based in the
/// file, never in-process state. The donor's env-var production-path
/// resolution belongs to the s5 surface and is not carried here.
#[test]
fn idempotency_survives_reopen_of_the_same_file() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("memory.sqlite3");
    let entry = base_entry();

    // "Boot 1": ingest one capture, then drop the store (simulated
    // restart).
    let first_id = {
        let mut store = Store::open(&path).expect("open (boot 1)");
        let outcome =
            ingest(&mut store, entry.request(), defaults(), NOW).expect("ingest (boot 1)");
        assert!(!outcome.deduped);
        outcome.id
    };

    // "Boot 2": same path. The capsule survived, and re-ingesting the
    // identical capture dedupes onto the SAME id.
    let mut store = Store::open(&path).expect("open (boot 2 / restart)");
    let reopened = store
        .get(first_id.as_str())
        .expect("get after restart")
        .expect("capsule must survive the restart");
    assert_eq!(
        reopened.capsule.provenance().source_hash,
        sha256_hex(entry.content.as_bytes())
    );

    let second = ingest(&mut store, entry.request(), defaults(), NOW).expect("re-ingest (boot 2)");
    assert!(second.deduped);
    assert_eq!(second.id, first_id);
    assert_eq!(store.list(ListFilter::default()).expect("list").len(), 1);
}
