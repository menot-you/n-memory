//! # Conformance — store round-trip + replay determinism (unit h2).
//!
//! Ported donor behavior (zero authority, reference only): the v1-scope
//! subset of donor B's `mcps/memory/tests/store_smoke.rs` @6d495898 —
//! replay determinism (same corpus → byte-identical canonical dump, across
//! runs AND across media), capsule round-trip byte-identity, reopen
//! persistence, missing-get None, and sequence-derived ids — re-authored
//! against the new `Store` (`cap-<n>` ids, injected `now`,
//! `canonical_snapshot`). The donor's relation and audit-event tests are
//! NOT carried (relation graph and audit trail are not in v1; supersedes
//! arrives as an h4 sidecar).
//!
//! Delta vs donor, asserted here as new-shape behavior: the donor replayed
//! the SAME capsule twice for id determinism; the new store carries a
//! UNIQUE `source_hash` backstop, so a byte-identical re-append is a typed
//! rejection instead of a second row.

#![allow(
    clippy::unwrap_used,
    clippy::expect_used,
    reason = "tests use unwrap/expect so fixture failures fail at the assertion site"
)]

use nmemory::capsule::Capsule;
use nmemory::store::{ListFilter, Store, StoreError};
use time::OffsetDateTime;
use time::macros::datetime;

const FIXTURE_JSON: &str = include_str!("fixtures/representative_capsules.json");

/// Fixed injected base instant; the store itself never reads the clock,
/// and neither may its tests (determinism gate).
const BASE_NOW: OffsetDateTime = datetime!(2026-07-18 12:00:00 UTC);

fn fixture_set() -> Vec<Capsule> {
    let set: Vec<Capsule> =
        serde_json::from_str(FIXTURE_JSON).expect("fixture pack parses as Vec<Capsule>");
    assert!(set.len() >= 5, "hard gate requires >=5 fixture capsules");
    set
}

/// Replay the shared fixture corpus with a deterministic injected-now
/// sequence and return the canonical snapshot bytes.
fn replay_fixture_corpus(store: &mut Store) -> String {
    for (i, capsule) in fixture_set().iter().enumerate() {
        let now = BASE_NOW + time::Duration::seconds(i as i64);
        store.append(capsule, now).expect("append capsule");
    }
    store.canonical_snapshot().expect("canonical snapshot")
}

#[test]
fn replaying_the_same_corpus_twice_is_byte_identical() {
    let dir_a = tempfile::tempdir().expect("tempdir a");
    let dir_b = tempfile::tempdir().expect("tempdir b");
    let mut store_a = Store::open(&dir_a.path().join("memory.sqlite3")).expect("open a");
    let mut store_b = Store::open(&dir_b.path().join("memory.sqlite3")).expect("open b");

    let dump_a = replay_fixture_corpus(&mut store_a);
    let dump_b = replay_fixture_corpus(&mut store_b);

    assert!(!dump_a.is_empty());
    assert_eq!(
        dump_a, dump_b,
        "two fresh stores replaying the same corpus must dump byte-identical"
    );
}

#[test]
fn dump_is_identical_across_backing_media() {
    let dir = tempfile::tempdir().expect("tempdir");
    let mut file_store = Store::open(&dir.path().join("memory.sqlite3")).expect("open file store");
    let mut mem_store = Store::open_in_memory().expect("open in-memory store");

    assert_eq!(
        replay_fixture_corpus(&mut file_store),
        replay_fixture_corpus(&mut mem_store),
        "the canonical dump must not depend on the backing medium"
    );
}

#[test]
fn capsule_round_trips_byte_identical() {
    let mut store = Store::open_in_memory().expect("open");
    for capsule in fixture_set() {
        let before = capsule.to_canonical_json().expect("serialize before");
        let id = store.append(&capsule, BASE_NOW).expect("append");
        let read = store
            .get(id.as_str())
            .expect("get")
            .expect("appended capsule exists");
        let after = read.capsule.to_canonical_json().expect("serialize after");
        assert_eq!(before, after, "capsule {id} drifted through the store");
    }
}

#[test]
fn capsules_survive_reopen() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("memory.sqlite3");
    let capsule = fixture_set().remove(0);
    let id = {
        let mut store = Store::open(&path).expect("open");
        store.append(&capsule, BASE_NOW).expect("append")
    };
    let store = Store::open(&path).expect("reopen");
    let read = store.get(id.as_str()).expect("get").expect("persisted");
    assert_eq!(
        capsule.to_canonical_json().expect("before"),
        read.capsule.to_canonical_json().expect("after"),
        "capsule must survive close+reopen byte-identical"
    );
    // The injected append instant survives too — nothing re-stamped it.
    assert_eq!(read.created_at, BASE_NOW);
}

#[test]
fn get_missing_capsule_is_none() {
    let store = Store::open_in_memory().expect("open");
    assert!(store.get("cap-does-not-exist").expect("get").is_none());
}

#[test]
fn capsule_ids_are_deterministic_append_sequence() {
    // New shape: `cap-<n>` starting at cap-1 (donor: zero-padded
    // cap-00000001). Distinct fixtures, because the new store's UNIQUE
    // source_hash backstop forbids the donor's same-capsule-twice replay.
    let mut store = Store::open_in_memory().expect("open");
    for (i, capsule) in fixture_set().iter().enumerate() {
        let id = store.append(capsule, BASE_NOW).expect("append");
        assert_eq!(id.as_str(), format!("cap-{}", i + 1));
    }
}

#[test]
fn byte_identical_re_append_is_a_typed_rejection_not_a_second_row() {
    // The new-shape counterpart of the donor's duplicate handling: the
    // store-level idempotency backstop rejects a re-appended source_hash
    // with a typed error and writes nothing.
    let mut store = Store::open_in_memory().expect("open");
    let capsule = fixture_set().remove(0);
    store.append(&capsule, BASE_NOW).expect("first append");

    let err = store
        .append(&capsule, BASE_NOW)
        .expect_err("duplicate source_hash must be rejected");
    assert_eq!(
        err,
        StoreError::DuplicateSourceHash(capsule.provenance().source_hash.clone())
    );
    assert_eq!(store.list(ListFilter::default()).expect("list").len(), 1);
}
