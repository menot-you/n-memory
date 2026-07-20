//! # h3 — store determinism conformance (PLAN h3, `.2` §3 ported contract).
//!
//! The determinism contract ported from donor B
//! (`mcps/memory/src/store/trait_def.rs` @6d495898, reference only): the
//! store is a pure function of its append sequence — no wall clock, no
//! randomness, no ambient machine state. Ids derive from append order
//! (`cap-1`, `cap-2`, …); `created_at` is the caller-injected boundary
//! instant, never a store-read clock. Replaying the same `(capsule, now)`
//! sequence into two fresh stores yields byte-identical
//! [`Store::canonical_snapshot`] output, and closing/reopening the file
//! changes nothing.

#![allow(
    clippy::unwrap_used,
    clippy::expect_used,
    reason = "tests use unwrap/expect so fixture failures fail at the assertion site"
)]

use nmemory::capsule::{
    AuthorityClass, Capsule, Confidence, Freshness, Provenance, Scope, sha256_hex,
};
use nmemory::store::{ListFilter, Store};
use time::OffsetDateTime;
use time::macros::datetime;

/// Deterministic fixture. Distinct `text` ⇒ distinct `source_hash` (the
/// UNIQUE-indexed column), so capsules in a replay sequence never collide.
fn capsule(text: &str, project: &str) -> Capsule {
    Capsule::new(
        text.to_string(),
        Provenance {
            source: "session:2026-07-18".to_string(),
            anchor: "PLAN.md:140".to_string(),
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

/// The replayed corpus: `(capsule, injected now)` pairs. The injected
/// instants vary offset (UTC / +02:00 / -07:00) and sub-second precision,
/// so byte equality also covers RFC3339 formatting, not just row content.
fn corpus() -> Vec<(Capsule, OffsetDateTime)> {
    vec![
        (
            capsule("alpha determinism fact", "nmemory"),
            datetime!(2001-01-01 00:00:00 UTC),
        ),
        (
            capsule("beta determinism fact", "nmemory"),
            datetime!(2001-02-03 04:05:06.123456789 +02:00),
        ),
        (
            capsule("gamma cross-project fact", "other"),
            datetime!(2003-12-31 23:59:59.5 -07:00),
        ),
    ]
}

/// Same inputs ⇒ same bytes: the SAME `(capsule, now)` sequence appended
/// into TWO fresh file-backed stores yields byte-identical canonical
/// snapshots — the donor determinism gate's comparand, verbatim.
#[test]
fn same_inputs_into_two_fresh_stores_yield_identical_snapshot_bytes() {
    let dir_a = tempfile::tempdir().unwrap();
    let dir_b = tempfile::tempdir().unwrap();
    let mut a = Store::open(&dir_a.path().join("memory.sqlite3")).unwrap();
    let mut b = Store::open(&dir_b.path().join("memory.sqlite3")).unwrap();

    let corpus = corpus();
    for (c, now) in &corpus {
        a.append(c, *now).unwrap();
        b.append(c, *now).unwrap();
    }

    let snap_a = a.canonical_snapshot().unwrap();
    let snap_b = b.canonical_snapshot().unwrap();
    // Not vacuous: one canonical-JSON line per appended capsule.
    assert_eq!(snap_a.lines().count(), corpus.len());
    assert_eq!(
        snap_a.as_bytes(),
        snap_b.as_bytes(),
        "two fresh stores fed the same (capsule, now) sequence must dump identical bytes"
    );
}

/// Ids are the append sequence: `cap-1..cap-n` in order, unchanged by a
/// close/reopen, and the sequence continues (no reuse, no reset) after
/// reopen.
#[test]
fn ids_are_cap_1_through_cap_n_in_append_order_and_stable_across_reopen() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("memory.sqlite3");
    let now = datetime!(2004-05-06 07:08:09 UTC);
    let texts = ["first", "second", "third", "fourth", "fifth"];
    {
        let mut store = Store::open(&path).unwrap();
        for (n, text) in texts.iter().enumerate() {
            let id = store.append(&capsule(text, "nmemory"), now).unwrap();
            assert_eq!(id.as_str(), format!("cap-{}", n + 1));
        }
    }

    let mut store = Store::open(&path).unwrap();
    let ids: Vec<String> = store
        .list(ListFilter::default())
        .unwrap()
        .iter()
        .map(|s| s.id.to_string())
        .collect();
    assert_eq!(ids, ["cap-1", "cap-2", "cap-3", "cap-4", "cap-5"]);

    // The sequence continues across reopen — append order IS identity.
    let next = store.append(&capsule("sixth", "nmemory"), now).unwrap();
    assert_eq!(next.as_str(), "cap-6");
}

/// Injected clock: appending with a FIXED historic `now` (2001-01-01, an
/// instant no 2026 wall clock can produce) persists `created_at` as exactly
/// that value — in memory, across reopen, and in the snapshot bytes. The
/// store consumed the boundary value and read no clock of its own.
#[test]
fn created_at_is_exactly_the_injected_historic_now() {
    let historic = datetime!(2001-01-01 00:00:00 UTC);
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("memory.sqlite3");
    {
        let mut store = Store::open(&path).unwrap();
        let id = store
            .append(&capsule("boundary time fact", "nmemory"), historic)
            .unwrap();
        let got = store.get(id.as_str()).unwrap().unwrap();
        assert_eq!(got.created_at, historic);
    }

    // The persisted form agrees after reopen.
    let store = Store::open(&path).unwrap();
    let got = store.get("cap-1").unwrap().unwrap();
    assert_eq!(got.created_at, historic);

    // And the canonical bytes carry the literal historic instant.
    let snapshot = store.canonical_snapshot().unwrap();
    assert!(
        snapshot.contains("\"created_at\":\"2001-01-01T00:00:00Z\""),
        "snapshot must carry the injected instant verbatim, got: {snapshot}"
    );
}

/// Reopen determinism: the canonical snapshot taken before close is
/// byte-identical to the one taken after reopening the same file.
#[test]
fn snapshot_before_close_equals_snapshot_after_reopen() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("memory.sqlite3");

    let before = {
        let mut store = Store::open(&path).unwrap();
        let corpus = corpus();
        for (c, now) in &corpus {
            store.append(c, *now).unwrap();
        }
        store.canonical_snapshot().unwrap()
    };

    let store = Store::open(&path).unwrap();
    let after = store.canonical_snapshot().unwrap();
    assert!(!before.is_empty(), "comparand must not be vacuously empty");
    assert_eq!(
        before.as_bytes(),
        after.as_bytes(),
        "close/reopen must not change a single snapshot byte"
    );
}
