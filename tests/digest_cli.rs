//! # CLI verb — `nmemory digest --headlines <n>`.
//!
//! Shell-level: drives the BUILT binary as a subprocess (not the library
//! directly), matching what a real shell caller runs — the `relate_cli`
//! pattern. The flag is the one knob that decides whether a truncating
//! section can be read whole from a shell, so the proof has to cross the
//! whole argv -> `DigestParams` -> handler path: a unit test over the parser
//! alone would still pass with the value dropped on the floor.
//!
//! The store is seeded past the server's default cap so the default call is
//! observably truncated, and every case asserts the list length MOVES with
//! the flag while `newest_total` stays the exact population.

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

/// Rows seeded — deliberately above the server's 10-row headline default so
/// an unflagged call truncates and the flag has something to reveal.
const SEEDED: usize = 12;
/// Fixed injected base instant; the store never reads the clock, and neither
/// may its tests (determinism gate).
const BASE_NOW: OffsetDateTime = datetime!(2026-07-18 12:00:00 UTC);

/// Seed a fresh store at `path` with [`SEEDED`] distinct capsules in one
/// project, then drop the store — the subprocess below reopens the same file.
fn seed_capsules(path: &Path) {
    let mut store = Store::open(path).expect("open store");
    for i in 0..SEEDED {
        let json = format!(
            r#"{{"content":"census row {i} about sqlite storage",
                 "provenance":{{"source":"digest-cli-test",
                                "anchor":"tests/digest_cli.rs:1",
                                "source_hash":"{i:064}"}},
                 "confidence":0.6,
                 "freshness":{{"valid_from":"2026-07-07T00:00:00Z","valid_to":null}},
                 "scope":{{"project_id":"nott"}},
                 "authority_class":"observed-fact",
                 "instruction_taint":false}}"#
        );
        let capsule: Capsule = serde_json::from_str(&json).expect("capsule parses");
        let now = BASE_NOW + time::Duration::seconds(i as i64);
        store.append(&capsule, now).expect("append capsule");
    }
}

/// Run `nmemory digest <args> --db <db>` against the built binary.
fn run_digest(db: &Path, args: &[&str]) -> Output {
    Command::new(env!("CARGO_BIN_EXE_nmemory"))
        .arg("digest")
        .args(args)
        .arg("--db")
        .arg(db)
        .output()
        .expect("spawn nmemory digest")
}

/// Parse a successful run's single-line JSON envelope.
fn envelope(out: &Output) -> serde_json::Value {
    assert!(out.status.success(), "digest must exit 0: {out:?}");
    let stdout = String::from_utf8_lossy(&out.stdout);
    serde_json::from_str(stdout.trim()).expect("one-line JSON envelope")
}

#[test]
fn headlines_flag_raises_the_cap_in_both_forms_without_moving_the_total() {
    let dir = tempfile::tempdir().expect("tempdir");
    let db = dir.path().join("memory.sqlite3");
    seed_capsules(&db);

    // No flag → the server default (10) truncates, and the total says so.
    let default = envelope(&run_digest(&db, &[]));
    assert_eq!(default["newest"].as_array().unwrap().len(), 10);
    assert_eq!(default["newest_total"], SEEDED);

    // `--flag value` → the whole population is listed.
    let raised = envelope(&run_digest(&db, &["--headlines", "12"]));
    assert_eq!(raised["newest"].as_array().unwrap().len(), SEEDED);
    assert_eq!(raised["newest_total"], SEEDED);

    // `--flag=value` → the same knob, and a value BELOW the default proves
    // the flag is read rather than merely tolerated.
    let lowered = envelope(&run_digest(&db, &["--headlines=3"]));
    assert_eq!(lowered["newest"].as_array().unwrap().len(), 3);
    assert_eq!(
        lowered["newest_total"], SEEDED,
        "the cap is presentation; the total is truth"
    );
}

#[test]
fn non_numeric_headlines_is_rejected_with_a_teaching_error() {
    let dir = tempfile::tempdir().expect("tempdir");
    let db = dir.path().join("memory.sqlite3");
    seed_capsules(&db);

    for form in [
        ["--headlines", "ten"].as_slice(),
        ["--headlines=ten"].as_slice(),
    ] {
        let out = run_digest(&db, form);
        assert!(
            !out.status.success(),
            "a non-numeric --headlines must exit nonzero: {out:?}"
        );
        let stderr = String::from_utf8_lossy(&out.stderr);
        assert!(
            stderr.contains("--headlines requires a non-negative integer")
                && stderr.contains("\"ten\"")
                && stderr.contains("usage: nmemory digest [--headlines <n>]"),
            "the teaching error must name the rule, the rejected value and the usage: {stderr}"
        );
    }
}
