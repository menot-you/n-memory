// -- COPIED ORGAN — DO NOT MODERNIZE IN THIS UNIT ---------------------------
// source: mcps/ssot-spool/src/lib.rs @ 9f92fa58 (donor A, read-only reference
//         repo /data/repos/menot-you/nott)
// copied: 2026-07-18, unit s1 — copied verbatim (rejection test
//         prd.nMEMORY.2 §8.4: modernizing a copied organ in the unit that
//         copies it fails the plan). Everything below this header block is
//         the donor file byte-for-byte; any change to it is its own later
//         unit. Sole embedding note: the donor crate root becomes module
//         `nmemory::spool`; its inner attributes/docs remain legal unchanged.
// ---------------------------------------------------------------------------
//! Crash-durable filesystem spool — persist + replay, transport-agnostic.
//!
//! Extracted from `mcps/ssot/ssot-server/src/workers/async_writes.rs` so server
//! workers and local-first sync flows share ONE persistence implementation with
//! their own executor. This crate owns only the filesystem side: serialize → fsync →
//! atomic rename, and read → deserialize → ordered iteration. The per-item
//! "execute" step (DB query / HTTP call) stays with the caller.
//!
//! ## Durability contract
//!
//! `persist` writes a tempfile in the spool dir, `sync_all`s it, atomically
//! renames it to `<id>.json` (noclobber — a collision is a corruption signal,
//! not a silent overwrite), then fsyncs the directory entry so the rename
//! survives a crash. Items are plain serde; the `id` is the caller's unique
//! key (e.g. a server-unique UUIDv4) and becomes the filename stem.

#![forbid(unsafe_code)]

use serde::Serialize;
use serde::de::DeserializeOwned;
use std::io::Write;
use std::path::{Path, PathBuf};

/// Persist a serializable item to `spool_dir` with a crash-consistent fsync
/// chain. `id` is the unique key; the file lands at `<spool_dir>/<id>.json`.
///
/// 6 steps: tempfile in same dir (→ atomic rename) → write → flush → sync_all
/// → persist_noclobber → fsync the dir. `persist_noclobber` fails if the
/// target already exists — a colliding `id` signals corruption / replay-of-
/// replay and is surfaced as an error, never an overwrite.
pub fn spool_persist<T: Serialize>(
    spool_dir: &Path,
    id: &str,
    item: &T,
) -> Result<PathBuf, String> {
    let target = spool_dir.join(format!("{id}.json"));
    let payload = serde_json::to_vec(item).map_err(|e| format!("spool item serialize: {e}"))?;

    // Step 1 — tempfile in the spool dir (same filesystem → rename is atomic).
    let mut tmp =
        tempfile::NamedTempFile::new_in(spool_dir).map_err(|e| format!("tempfile create: {e}"))?;

    // Steps 2-4 — write + flush + sync_all (durable content).
    tmp.write_all(&payload)
        .map_err(|e| format!("tempfile write: {e}"))?;
    tmp.flush().map_err(|e| format!("tempfile flush: {e}"))?;
    tmp.as_file()
        .sync_all()
        .map_err(|e| format!("tempfile sync_all: {e}"))?;

    // Step 5 — atomic rename, noclobber. Collision = corruption signal.
    let persisted = tmp
        .persist_noclobber(&target)
        .map_err(|e| format!("tempfile persist_noclobber({}): {e}", target.display()))?;

    // Step 6 — fsync the directory entry so the rename survives a crash.
    if let Ok(dir_handle) = std::fs::File::open(spool_dir) {
        let _ = dir_handle.sync_all();
    }
    let _ = persisted; // File handle drops + closes here.

    Ok(target)
}

/// Read + deserialize every `.json` entry under `spool_dir`, oldest-first by
/// mtime, bounded by `max`. Pure filesystem: it does NOT execute or remove —
/// the caller replays each `(path, item)` against its backend and then calls
/// [`spool_remove`] (or lets its own post-success cleanup unlink the path).
///
/// Tolerant: a missing dir, an unreadable file, or a bad-JSON entry is logged
/// at WARN and skipped (never fatal). Returns the items that parsed cleanly.
#[must_use]
pub fn replay_spool_files<T: DeserializeOwned>(spool_dir: &Path, max: usize) -> Vec<(PathBuf, T)> {
    let mut paths: Vec<PathBuf> = match std::fs::read_dir(spool_dir) {
        Ok(rd) => rd
            .filter_map(std::result::Result::ok)
            .map(|e| e.path())
            .filter(|p| {
                p.extension()
                    .is_some_and(|ext| ext.eq_ignore_ascii_case("json"))
            })
            .collect(),
        Err(e) => {
            tracing::warn!(spool_dir = ?spool_dir, error = %e, "spool replay: read_dir failed; skipping");
            return Vec::new();
        }
    };

    // Oldest-first by mtime — best-effort fairness for stale entries.
    paths.sort_by_key(|p| {
        std::fs::metadata(p)
            .and_then(|m| m.modified())
            .ok()
            .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
            .map_or(0, |d| d.as_secs())
    });

    let mut out = Vec::new();
    for path in paths.into_iter().take(max) {
        let raw = match std::fs::read(&path) {
            Ok(b) => b,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => continue,
            Err(e) => {
                tracing::warn!(?path, error = %e, "spool replay: read failed");
                continue;
            }
        };
        match serde_json::from_slice::<T>(&raw) {
            Ok(item) => out.push((path, item)),
            Err(e) => {
                tracing::warn!(?path, error = %e, "spool replay: deserialize failed");
            }
        }
    }
    out
}

/// Best-effort unlink of a spool entry by `id` after successful processing.
/// ENOENT is tolerable (concurrent replay / already gone); other errors warn.
pub fn spool_remove(spool_dir: &Path, id: &str) {
    let path = spool_dir.join(format!("{id}.json"));
    match std::fs::remove_file(&path) {
        Ok(()) => {}
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
        Err(e) => {
            tracing::warn!(?path, error = %e, "spool remove failed (non-ENOENT)");
        }
    }
}

#[cfg(test)]
mod tests {
    #![allow(
        clippy::expect_used,
        clippy::unwrap_used,
        reason = "tests use unwrap and expect so fixture failures fail at the assertion site"
    )]

    use super::*;
    use serde::{Deserialize, Serialize};

    #[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
    struct Item {
        id: String,
        payload: u32,
    }

    fn item(id: &str, payload: u32) -> Item {
        Item {
            id: id.to_string(),
            payload,
        }
    }

    #[test]
    fn persist_then_replay_roundtrip() {
        let tmp = tempfile::tempdir().unwrap();
        let dir = tmp.path();
        let a = item("aaa", 1);
        let path = spool_persist(dir, &a.id, &a).unwrap();
        assert!(path.exists());

        let back: Vec<(PathBuf, Item)> = replay_spool_files(dir, 100);
        assert_eq!(back.len(), 1);
        assert_eq!(back[0].1, a);
    }

    #[test]
    fn persist_collision_is_error() {
        let tmp = tempfile::tempdir().unwrap();
        let dir = tmp.path();
        let a = item("dup", 1);
        spool_persist(dir, &a.id, &a).unwrap();
        let err = spool_persist(dir, &a.id, &a).unwrap_err();
        assert!(err.contains("persist_noclobber"), "got: {err}");
    }

    #[test]
    fn replay_skips_bad_json_and_non_json() {
        let tmp = tempfile::tempdir().unwrap();
        let dir = tmp.path();
        let good = item("good", 7);
        spool_persist(dir, &good.id, &good).unwrap();
        std::fs::write(dir.join("broken.json"), b"{not valid").unwrap();
        std::fs::write(dir.join("ignore.txt"), b"whatever").unwrap();

        let back: Vec<(PathBuf, Item)> = replay_spool_files(dir, 100);
        assert_eq!(back.len(), 1, "only the good .json parses");
        assert_eq!(back[0].1, good);
    }

    #[test]
    fn remove_is_idempotent() {
        let tmp = tempfile::tempdir().unwrap();
        let dir = tmp.path();
        let a = item("rm", 1);
        spool_persist(dir, &a.id, &a).unwrap();
        spool_remove(dir, &a.id);
        spool_remove(dir, &a.id); // second call: ENOENT, no panic
        let back: Vec<(PathBuf, Item)> = replay_spool_files(dir, 100);
        assert!(back.is_empty());
    }

    #[test]
    fn replay_missing_dir_is_empty() {
        let tmp = tempfile::tempdir().unwrap();
        let missing = tmp.path().join("nope");
        let back: Vec<(PathBuf, Item)> = replay_spool_files(&missing, 100);
        assert!(back.is_empty());
    }
}
