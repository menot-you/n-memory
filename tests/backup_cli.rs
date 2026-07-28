//! # CLI verb — `nmemory backup --to <path>`.
//!
//! Shell-level: drives the BUILT binary as a subprocess, the `digest_cli`
//! pattern. Backup's contract is the RUNBOOK's own instruction — "copy the
//! pre-upgrade state BEFORE the one-way door" — and the review found the
//! implementation defeating it twice: routing through `Store::open` CREATED
//! a missing source (a typo'd `--db` "backed up" a brand-new empty store)
//! and MIGRATED an old schema in place before the copy. Every case here is
//! the falsifiable form of one of those refusals: reintroduce `Store::open`
//! on the backup path and the future-schema case goes red; drop the
//! missing-source guard and that case goes red.

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

/// Fixed injected instant; the store never reads the clock, nor may tests.
const BASE_NOW: OffsetDateTime = datetime!(2026-07-18 12:00:00 UTC);

/// Seed a fresh store at `path` with one capsule, then drop it.
fn seed_one(path: &Path) {
    let mut store = Store::open(path).expect("open store");
    let capsule: Capsule = serde_json::from_str(
        r#"{"content":"backup fixture row about sqlite storage",
            "provenance":{"source":"backup-cli-test",
                          "anchor":"tests/backup_cli.rs:1",
                          "source_hash":"00000000000000000000000000000000000000000000000000000000000000aa"},
            "confidence":0.6,
            "freshness":{"valid_from":"2026-07-07T00:00:00Z","valid_to":null},
            "scope":{"project_id":"nott"},
            "authority_class":"observed-fact",
            "instruction_taint":false}"#,
    )
    .expect("capsule parses");
    store.append(&capsule, BASE_NOW).expect("append capsule");
}

/// Run `nmemory backup --to <to> --db <db>` against the built binary.
fn run_backup(db: &Path, to: &Path) -> Output {
    Command::new(env!("CARGO_BIN_EXE_nmemory"))
        .arg("backup")
        .arg("--to")
        .arg(to)
        .arg("--db")
        .arg(db)
        .output()
        .expect("spawn nmemory backup")
}

#[test]
fn happy_path_copies_the_row_and_leaves_the_source_alone() {
    let dir = tempfile::tempdir().unwrap();
    let db = dir.path().join("memory.sqlite3");
    let to = dir.path().join("copy.sqlite3");
    seed_one(&db);

    let out = run_backup(&db, &to);
    assert!(
        out.status.success(),
        "stderr: {}",
        String::from_utf8_lossy(&out.stderr)
    );

    let copy = Store::open(&to).expect("the snapshot opens as a store");
    assert!(
        copy.get("cap-1").expect("get").is_some(),
        "the seeded row survived the copy"
    );
}

#[test]
fn missing_source_is_refused_and_creates_nothing() {
    let dir = tempfile::tempdir().unwrap();
    let db = dir.path().join("no-such-store.sqlite3");
    let to = dir.path().join("copy.sqlite3");

    let out = run_backup(&db, &to);
    assert!(!out.status.success(), "a missing source MUST refuse");
    let stderr = String::from_utf8_lossy(&out.stderr);
    assert!(
        stderr.contains("backup refused: no database at"),
        "the refusal teaches; got: {stderr}"
    );
    // The old path's exact failure: Store::open created the missing file and
    // the "backup" succeeded over an empty store. Neither file may exist.
    assert!(!db.exists(), "the refusal MUST NOT create the source");
    assert!(!to.exists(), "the refusal MUST NOT create the destination");
}

#[test]
fn future_schema_source_is_copied_verbatim_not_opened() {
    // A source stamped with a schema version this build does not know.
    // `Store::open` refuses such a file, and an older one it would MIGRATE
    // in place — so this case only passes while backup avoids `Store::open`
    // entirely. Reintroduce it and this goes red, in one direction or the
    // other: refusal here, or a mutated source below.
    let dir = tempfile::tempdir().unwrap();
    let db = dir.path().join("memory.sqlite3");
    let to = dir.path().join("copy.sqlite3");
    seed_one(&db);
    let conn = rusqlite::Connection::open(&db).unwrap();
    conn.pragma_update(None, "user_version", 9999).unwrap();
    drop(conn);

    let out = run_backup(&db, &to);
    assert!(
        out.status.success(),
        "a backup tool MUST NOT refuse the file it exists to preserve; stderr: {}",
        String::from_utf8_lossy(&out.stderr)
    );

    let src = rusqlite::Connection::open(&db).unwrap();
    let src_version: i64 = src
        .query_row("PRAGMA user_version", [], |r| r.get(0))
        .unwrap();
    assert_eq!(
        src_version, 9999,
        "the source schema stamp is untouched — nothing migrated"
    );
    let dst = rusqlite::Connection::open(&to).unwrap();
    let dst_version: i64 = dst
        .query_row("PRAGMA user_version", [], |r| r.get(0))
        .unwrap();
    assert_eq!(
        dst_version, 9999,
        "the copy carries the source's stamp verbatim"
    );
}

#[test]
fn missing_to_flag_is_a_usage_error() {
    let out = Command::new(env!("CARGO_BIN_EXE_nmemory"))
        .arg("backup")
        .output()
        .expect("spawn nmemory backup");
    assert!(!out.status.success());
    assert!(
        String::from_utf8_lossy(&out.stderr).contains("--to"),
        "the usage names the missing flag"
    );
}
