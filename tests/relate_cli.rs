//! # CLI verb — `nmemory relate` (unblocks plugin PR #151, #156-e).
//!
//! Shell-level: drives the BUILT binary as a subprocess (not the library
//! directly), matching what a real shell caller runs. Seeds two capsules
//! straight through the library into the SAME db file the subprocess then
//! opens, then asserts the CLI verb's contract: the closed 9-kind wire
//! vocabulary rejects an unknown kind, `part_of` into a non-container
//! answers the SAME teaching message `memory_relate` gives over MCP, and
//! the happy path records the edge and reports `already_recorded` on
//! replay — exit 0 on success/already, nonzero with the teaching error
//! otherwise.

#![allow(
    clippy::unwrap_used,
    clippy::expect_used,
    reason = "tests use unwrap/expect so fixture failures fail at the assertion site"
)]

use std::path::Path;
use std::process::{Command, Output};

use nmemory::capsule::Capsule;
use nmemory::store::Store;
use time::OffsetDateTime;
use time::macros::datetime;

const FIXTURE_JSON: &str = include_str!("fixtures/representative_capsules.json");
/// Fixed injected base instant; the store itself never reads the clock,
/// and neither may its tests (determinism gate).
const BASE_NOW: OffsetDateTime = datetime!(2026-07-18 12:00:00 UTC);

/// Seed a fresh store at `path` with the first two fixture capsules
/// (append order → `cap-1`, `cap-2`), then drop the store — the subprocess
/// below reopens the same file. Neither capsule carries a classification
/// sidecar, so `cap-2` is a stored-but-unclassified `part_of` target.
fn seed_two_capsules(path: &Path) {
    let fixtures: Vec<Capsule> =
        serde_json::from_str(FIXTURE_JSON).expect("fixture pack parses as Vec<Capsule>");
    assert!(fixtures.len() >= 2, "fixture pack needs at least 2 rows");
    let mut store = Store::open(path).expect("open store");
    for (i, capsule) in fixtures.iter().take(2).enumerate() {
        let now = BASE_NOW + time::Duration::seconds(i as i64);
        store.append(capsule, now).expect("append capsule");
    }
}

/// Run `nmemory relate <args> --db <db>` against the built binary and
/// return its captured output (never panics on a nonzero exit — the
/// caller asserts that).
fn run_relate(db: &Path, args: &[&str]) -> Output {
    Command::new(env!("CARGO_BIN_EXE_nmemory"))
        .arg("relate")
        .args(args)
        .arg("--db")
        .arg(db)
        .output()
        .expect("spawn nmemory relate")
}

#[test]
fn unknown_kind_is_rejected() {
    let dir = tempfile::tempdir().expect("tempdir");
    let db = dir.path().join("memory.sqlite3");
    seed_two_capsules(&db);

    let out = run_relate(
        &db,
        &["--kind", "bogus_kind", "--from", "cap-1", "--to", "cap-2"],
    );
    assert!(
        !out.status.success(),
        "an unknown --kind must exit nonzero: {out:?}"
    );
    let stderr = String::from_utf8_lossy(&out.stderr);
    assert!(
        stderr.contains("closed relation kinds"),
        "the teaching error must name the closed set: {stderr}"
    );
}

#[test]
fn part_of_into_a_non_container_is_rejected_with_the_teaching_message() {
    let dir = tempfile::tempdir().expect("tempdir");
    let db = dir.path().join("memory.sqlite3");
    seed_two_capsules(&db);
    // Neither cap-1 nor cap-2 carries a classification sidecar — `to`
    // (cap-2) is stored but not persisted as kind epic/task.

    let out = run_relate(
        &db,
        &["--kind", "part_of", "--from", "cap-1", "--to", "cap-2"],
    );
    assert!(
        !out.status.success(),
        "part_of into a non-container must exit nonzero: {out:?}"
    );
    let stderr = String::from_utf8_lossy(&out.stderr);
    assert!(
        stderr.contains("part_of records membership INTO a container"),
        "the teaching message must explain the container gate — the SAME \
         rejection memory_relate gives over MCP: {stderr}"
    );
}

#[test]
fn happy_path_records_and_replay_reports_already_recorded() {
    let dir = tempfile::tempdir().expect("tempdir");
    let db = dir.path().join("memory.sqlite3");
    seed_two_capsules(&db);

    let first = run_relate(
        &db,
        &["--kind", "witnesses", "--from", "cap-1", "--to", "cap-2"],
    );
    assert!(
        first.status.success(),
        "the happy path must exit 0: {first:?}"
    );
    let stdout = String::from_utf8_lossy(&first.stdout);
    let outcome: serde_json::Value =
        serde_json::from_str(stdout.trim()).expect("one-line JSON outcome");
    assert_eq!(outcome["kind"], "witnesses");
    assert_eq!(outcome["from"], "cap-1");
    assert_eq!(outcome["to"], "cap-2");
    assert_eq!(outcome["recorded"], true);
    assert_eq!(
        outcome["already_recorded"], false,
        "a fresh write: {outcome}"
    );

    let second = run_relate(
        &db,
        &["--kind", "witnesses", "--from", "cap-1", "--to", "cap-2"],
    );
    assert!(
        second.status.success(),
        "a replay is STILL exit 0 (idempotent no-op): {second:?}"
    );
    let stdout = String::from_utf8_lossy(&second.stdout);
    let outcome: serde_json::Value =
        serde_json::from_str(stdout.trim()).expect("one-line JSON outcome");
    assert_eq!(
        outcome["already_recorded"], true,
        "a replay reports already_recorded: {outcome}"
    );
}
