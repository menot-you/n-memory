//! # nmemory sync — opt-in reconcile of a LOCAL store with a REMOTE mirror (u3).
//!
//! Owner-ratified feature "local store always present, remote mirror, reconcile
//! later, user never blocked". This is the imperative shell of that reconcile:
//! the `nmemory sync` subcommand's engine. It sits ON TOP of the u2 core
//! ([`crate::store::Store::merge_from`] → [`crate::merge::plan_merge`]) and adds
//! ONE thing the core does not have: a way to move a remote store file to and
//! from the local disk. It re-implements NONE of the merge.
//!
//! ## Laws this module holds
//!
//! - **Local-first, never blocked.** The LOCAL store is the source of truth the
//!   user keeps working against. [`reconcile`] FETCHES the remote to a private
//!   temp path FIRST; only then does it open LOCAL and merge. A fetch that
//!   fails never reaches the merge, so the user always keeps their local memory.
//! - **Fail-closed, no partial state.** Every bad path is a typed [`SyncError`]
//!   and leaves LOCAL intact. The one writer of LOCAL is
//!   [`Store::merge_from`][crate::store::Store::merge_from], which applies in a
//!   single transaction — LOCAL is either fully merged or byte-untouched, never
//!   partially written.
//! - **The serve path stays hermetic.** The remote is reached ONLY through a
//!   pluggable [`Transport`]. The default [`ScpTransport`] shells out to an
//!   EXTERNAL command (`std::process`), so the binary links NO network stack;
//!   the stdio serve/engine path never constructs or calls a [`Transport`] and
//!   stays socket-free. Tests inject a local-copy transport and touch no host.
//!
//! ## The reconcile
//!
//! 1. FETCH the remote store to a temp path via the [`Transport`].
//! 2. MERGE that store INTO LOCAL with the u2 core (deterministic, atomic).
//! 3. Optionally PUSH the merged LOCAL back to the remote mirror so both sides
//!    converge (the merged LOCAL is the superset of both).

use std::path::Path;
use std::process::Command;

use crate::store::{MergeSummary, Store, StoreError};

/// Typed sync failures. On EVERY variant the LOCAL store is left intact: a
/// [`SyncError::Fetch`] fails before LOCAL is opened, and a
/// [`SyncError::Store`] surfaces from the atomic merge with nothing written.
#[derive(Debug, thiserror::Error)]
pub enum SyncError {
    /// The remote store could not be fetched (transport fetch failed) — LOCAL
    /// was never opened for write, so the user keeps their local memory.
    #[error("cannot fetch remote {remote:?}: {reason}")]
    Fetch {
        /// The remote spec that could not be reached.
        remote: String,
        /// The transport's reason (its captured stderr, when a command).
        reason: String,
    },
    /// The merged LOCAL could not be pushed back to the remote mirror. LOCAL
    /// is fully merged and intact; only the remote mirror is left stale.
    #[error("cannot push merged store to remote {remote:?}: {reason}")]
    Push {
        /// The remote spec that could not be written.
        remote: String,
        /// The transport's reason (its captured stderr, when a command).
        reason: String,
    },
    /// A private temp workspace for the fetched store could not be created.
    #[error("cannot create sync temp workspace: {0}")]
    Temp(#[source] std::io::Error),
    /// Opening LOCAL or applying the merge failed — a typed store error with
    /// nothing written (a corrupt/non-store/stale-schema fetched file fails
    /// here, LOCAL untouched).
    #[error("store: {0}")]
    Store(#[from] StoreError),
    /// No HMAC key source: neither `NMEMORY_HMAC_KEY` nor a usable key file
    /// beside the DB. The key re-keys any forget-wins tombstone the merge
    /// propagates, so it is resolved before the merge and fails closed.
    #[error(
        "no HMAC key ({0}): set NMEMORY_HMAC_KEY, or run against a file-backed store so a \
         key file can live beside the DB"
    )]
    NoKey(String),
}

/// A transport failure — the reason a remote store could not be read or
/// written. Mapped into [`SyncError::Fetch`]/[`SyncError::Push`] at the call
/// site so the sync error names which phase failed.
#[derive(Debug, thiserror::Error)]
#[error("{0}")]
pub struct TransportError(pub String);

/// The pluggable remote seam — the ONLY thing in nmemory that touches a
/// remote. `fetch` copies the remote store file to a local path; `push`
/// copies a local file to the remote. Both are whole-file copies: after a
/// merge the LOCAL file is the superset, so pushing it converges the mirror.
///
/// The serve/engine path never names this trait. Production uses
/// [`ScpTransport`] (an external command, no linked network stack); tests
/// inject a local-copy implementation and reach no host.
pub trait Transport {
    /// Copy the REMOTE store named by `remote` to the local `dest` path.
    /// Fail-closed: an unreachable remote returns [`TransportError`] and the
    /// caller leaves LOCAL untouched.
    ///
    /// # Errors
    /// Returns [`TransportError`] if the remote cannot be read to `dest`.
    fn fetch(&self, remote: &str, dest: &Path) -> Result<(), TransportError>;

    /// Copy the local `src` file to the REMOTE named by `remote`, overwriting
    /// the mirror.
    ///
    /// # Errors
    /// Returns [`TransportError`] if `src` cannot be written to the remote.
    fn push(&self, src: &Path, remote: &str) -> Result<(), TransportError>;
}

/// The default production transport: shell out to `scp` to copy a store file
/// to or from a `[user@]host:/path` remote spec. It links NO network stack
/// into the binary — the copy runs in a SEPARATE process (`std::process`), so
/// `nmemory` on the serve path stays socket-free. Tests never execute this;
/// they inject a local-copy [`Transport`].
#[derive(Debug, Clone)]
pub struct ScpTransport {
    /// The copy program to invoke — `scp` by default. Overridable for a tool
    /// with the same `<program> [flags] <source> <target>` convention.
    program: String,
}

impl Default for ScpTransport {
    fn default() -> Self {
        ScpTransport {
            program: "scp".to_string(),
        }
    }
}

impl ScpTransport {
    /// A transport driving a specific copy `program` (default: [`scp`][Self::default]).
    #[must_use]
    pub fn new(program: impl Into<String>) -> Self {
        ScpTransport {
            program: program.into(),
        }
    }
}

impl Transport for ScpTransport {
    fn fetch(&self, remote: &str, dest: &Path) -> Result<(), TransportError> {
        run_copy(&self.program, remote, &dest.to_string_lossy())
    }

    fn push(&self, src: &Path, remote: &str) -> Result<(), TransportError> {
        run_copy(&self.program, &src.to_string_lossy(), remote)
    }
}

/// Run one external `program from to` copy, capturing its output so nothing
/// leaks to this process's stdout/stderr. A non-zero exit is a
/// [`TransportError`] carrying the command's own stderr.
fn run_copy(program: &str, from: &str, to: &str) -> Result<(), TransportError> {
    let output = Command::new(program)
        .arg("-q")
        .arg(from)
        .arg(to)
        .output()
        .map_err(|e| TransportError(format!("failed to run {program:?}: {e}")))?;
    if output.status.success() {
        Ok(())
    } else {
        let stderr = String::from_utf8_lossy(&output.stderr);
        Err(TransportError(format!(
            "{program} exited unsuccessfully ({}): {}",
            output.status,
            stderr.trim()
        )))
    }
}

/// Reconcile LOCAL with the REMOTE mirror — LOCAL-first and fail-closed.
///
/// 1. FETCH the remote store to a private temp path via `transport`. On a
///    fetch failure LOCAL is NEVER opened, so the user keeps their store
///    (returns [`SyncError::Fetch`], no partial state).
/// 2. MERGE the fetched store INTO LOCAL with the u2 core
///    ([`Store::merge_from`][crate::store::Store::merge_from]) in ONE atomic
///    transaction — LOCAL is either fully merged or untouched.
/// 3. If `push`, copy the merged LOCAL back to the remote mirror so both
///    sides converge. A push failure leaves LOCAL fully merged (the returned
///    [`SyncError::Push`] names the stale mirror).
///
/// `hmac_key` is LOCAL's tombstone key (see [`resolve_hmac_key`]); the merge
/// re-keys any forget-wins tombstone under it. Returns the deterministic
/// [`MergeSummary`] — identical stores reconcile to an identical summary.
///
/// # Errors
/// Returns a [`SyncError`] for a failed fetch, a temp-workspace failure, a
/// store/merge failure (corrupt or stale-schema fetched file included), or a
/// failed push. On every one of them LOCAL is left intact.
pub fn reconcile(
    local_db: &Path,
    remote: &str,
    hmac_key: &[u8],
    transport: &dyn Transport,
    push: bool,
) -> Result<MergeSummary, SyncError> {
    // A private temp workspace for the fetched remote store, removed (with any
    // fetched bytes) when it drops at return. Honors TMPDIR via `tempfile`.
    let workspace = tempfile::Builder::new()
        .prefix("nmemory-sync-")
        .tempdir()
        .map_err(SyncError::Temp)?;
    let incoming = workspace.path().join("incoming.sqlite3");

    // FETCH first — before LOCAL is ever opened. A failure here can never have
    // touched LOCAL.
    transport
        .fetch(remote, &incoming)
        .map_err(|e| SyncError::Fetch {
            remote: remote.to_string(),
            reason: e.0,
        })?;

    // MERGE. Scope the store so its WAL connection is checkpointed and closed
    // (the on-disk file becomes a complete single file) BEFORE any push reads
    // it. `merge_from` opens the incoming file read-only and applies the plan
    // atomically; a corrupt/stale-schema incoming fails closed here.
    let summary = {
        let mut store = Store::open(local_db)?;
        store.merge_from(&incoming, hmac_key)?.summary
    };

    // PUSH (optional) — the merged LOCAL is the superset of both sides, so
    // copying it to the remote converges the mirror.
    if push {
        transport
            .push(local_db, remote)
            .map_err(|e| SyncError::Push {
                remote: remote.to_string(),
                reason: e.0,
            })?;
    }

    Ok(summary)
}

/// Resolve LOCAL's HMAC key for the merge's forget-wins re-keying, mirroring
/// the serve boundary's rule (`server::MemoryServer::resolve_hmac_key`): an
/// already-resolved non-empty `env_key` (`NMEMORY_HMAC_KEY`) wins; else the
/// `key_file` beside the DB is read, or CREATED on first use with OS
/// randomness (64 hex chars, `0600`). The key is only consumed when a
/// forget-wins tombstone applies, but the merge cannot know that in advance,
/// so it is resolved eagerly and fails closed with [`SyncError::NoKey`] when
/// no source exists.
///
/// # Errors
/// Returns [`SyncError::NoKey`] when neither `env_key` nor a usable `key_file`
/// yields key bytes (no path for an in-memory store, an empty key file, an
/// unreadable file, or OS randomness that cannot be drawn).
pub fn resolve_hmac_key(
    env_key: Option<Vec<u8>>,
    key_file: Option<&Path>,
) -> Result<Vec<u8>, SyncError> {
    if let Some(key) = env_key
        && !key.is_empty()
    {
        return Ok(key);
    }
    let Some(path) = key_file else {
        return Err(SyncError::NoKey("in-memory store, no key file".to_string()));
    };
    match std::fs::read(path) {
        Ok(bytes) => {
            let trimmed = bytes.trim_ascii();
            if trimmed.is_empty() {
                return Err(SyncError::NoKey(format!("{} is empty", path.display())));
            }
            Ok(trimmed.to_vec())
        }
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => create_key_file(path),
        Err(e) => Err(SyncError::NoKey(format!(
            "cannot read {}: {e}",
            path.display()
        ))),
    }
}

/// Create the key file beside the DB with 32 bytes of OS randomness rendered
/// as 64 hex chars, `0600`. Byte-compatible with the serve boundary's
/// `create_hmac_key_file`: whichever process forgets/syncs first mints the
/// file; the other reads the same bytes back.
fn create_key_file(path: &Path) -> Result<Vec<u8>, SyncError> {
    use std::io::Read as _;
    let mut raw = [0u8; 32];
    std::fs::File::open("/dev/urandom")
        .and_then(|mut f| f.read_exact(&mut raw))
        .map_err(|e| SyncError::NoKey(format!("cannot draw OS randomness: {e}")))?;
    let key = hex::encode(raw).into_bytes();
    let mut options = std::fs::OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt as _;
        options.mode(0o600);
    }
    options
        .open(path)
        .and_then(|mut f| {
            use std::io::Write as _;
            f.write_all(&key)
        })
        .map_err(|e| SyncError::NoKey(format!("cannot create key file {}: {e}", path.display())))?;
    Ok(key)
}

#[cfg(test)]
mod tests {
    #![allow(
        clippy::unwrap_used,
        clippy::expect_used,
        reason = "tests use unwrap/expect so fixture failures fail at the assertion site"
    )]

    use super::*;
    use std::path::PathBuf;

    use time::OffsetDateTime;
    use time::macros::datetime;

    use crate::capsule::{
        AuthorityClass, Capsule, Confidence, Freshness, Provenance, Scope, sha256_hex,
    };
    use crate::store::{ListFilter, TombstoneMode};

    const T0: OffsetDateTime = datetime!(2026-07-18 00:00:00 UTC);

    /// A capsule whose IDENTITY is `content` (source_hash = sha256(content)),
    /// so two fixtures collapse under merge iff their content matches — the
    /// same shape the merge core's own tests use.
    fn capsule(content: &str) -> Capsule {
        Capsule::new(
            content.to_owned(),
            Provenance {
                source: "session:2026-07-18".to_owned(),
                anchor: "notes.md:1".to_owned(),
                source_hash: sha256_hex(content.as_bytes()),
            },
            Confidence::new(0.5).unwrap(),
            Freshness {
                valid_from: T0,
                valid_to: None,
            },
            Scope {
                project_id: "nott".to_owned(),
            },
            AuthorityClass::AgentInferred,
            false,
        )
        .unwrap()
    }

    /// Create a real on-disk store at `path` seeded with one capsule per
    /// content string, then close it (WAL checkpointed) so a whole-file copy
    /// of `path` is complete.
    fn seed(path: &Path, contents: &[&str]) {
        let mut store = Store::open(path).unwrap();
        for content in contents {
            store.append(&capsule(content), T0).unwrap();
        }
    }

    /// The sorted LIVE capsule contents of the store at `path` (forgotten rows
    /// excluded — `list` fences on non-NULL canonical_json).
    fn live_contents(path: &Path) -> Vec<String> {
        let store = Store::open(path).unwrap();
        let mut out: Vec<String> = store
            .list(ListFilter::default())
            .unwrap()
            .iter()
            .map(|stored| stored.capsule.content().to_owned())
            .collect();
        out.sort();
        out
    }

    /// The test transport: a whole-file LOCAL copy (`std::fs::copy`). It
    /// reaches NO host — proving the reconcile drives entirely through the
    /// [`Transport`] seam.
    struct LocalCopyTransport;
    impl Transport for LocalCopyTransport {
        fn fetch(&self, remote: &str, dest: &Path) -> Result<(), TransportError> {
            std::fs::copy(remote, dest)
                .map(|_| ())
                .map_err(|e| TransportError(format!("copy {remote} -> {}: {e}", dest.display())))
        }
        fn push(&self, src: &Path, remote: &str) -> Result<(), TransportError> {
            std::fs::copy(src, remote)
                .map(|_| ())
                .map_err(|e| TransportError(format!("copy {} -> {remote}: {e}", src.display())))
        }
    }

    /// A transport whose remote is unreachable: fetch and push both fail. Models
    /// "zero network / host down" without any real socket.
    struct UnreachableTransport;
    impl Transport for UnreachableTransport {
        fn fetch(&self, remote: &str, _dest: &Path) -> Result<(), TransportError> {
            Err(TransportError(format!("host for {remote} is unreachable")))
        }
        fn push(&self, _src: &Path, remote: &str) -> Result<(), TransportError> {
            Err(TransportError(format!("host for {remote} is unreachable")))
        }
    }

    /// A transport that "succeeds" but writes non-store bytes — the merge must
    /// then fail closed, LOCAL untouched.
    struct GarbageTransport;
    impl Transport for GarbageTransport {
        fn fetch(&self, _remote: &str, dest: &Path) -> Result<(), TransportError> {
            std::fs::write(dest, b"this is not a sqlite database")
                .map_err(|e| TransportError(e.to_string()))
        }
        fn push(&self, _src: &Path, _remote: &str) -> Result<(), TransportError> {
            Ok(())
        }
    }

    fn remote_spec(path: &Path) -> String {
        path.to_str().unwrap().to_owned()
    }

    // 1. End-to-end: reconcile merges a second LOCAL store (the "remote") into
    //    the primary, reusing the u2 merge whole. New content is added, shared
    //    content collapses, and LOCAL ends holding the union.
    #[test]
    fn reconcile_merges_a_remote_store_into_local() {
        let tmp = tempfile::tempdir().unwrap();
        let local = tmp.path().join("local.sqlite3");
        let remote = tmp.path().join("remote.sqlite3");
        seed(&local, &["local-A", "local-B"]);
        seed(&remote, &["local-A", "remote-C"]); // local-A collapses, remote-C new

        let summary = reconcile(
            &local,
            &remote_spec(&remote),
            b"unit-key",
            &LocalCopyTransport,
            false,
        )
        .unwrap();

        assert_eq!(summary.capsules_added, 1); // remote-C
        assert_eq!(summary.capsules_collapsed, 1); // local-A
        assert_eq!(summary.id_remap_size, 2);
        // LOCAL now holds the union; the remote file was left untouched (fetch
        // read from a temp copy).
        assert_eq!(
            live_contents(&local),
            vec![
                "local-A".to_owned(),
                "local-B".to_owned(),
                "remote-C".to_owned()
            ]
        );
        assert_eq!(
            live_contents(&remote),
            vec!["local-A".to_owned(), "remote-C".to_owned()]
        );
    }

    // 2. Unreachable remote → LOCAL untouched, typed error. The fetch fails
    //    before LOCAL is opened, so the user keeps their local memory.
    #[test]
    fn unreachable_remote_leaves_local_untouched_with_typed_error() {
        let tmp = tempfile::tempdir().unwrap();
        let local = tmp.path().join("local.sqlite3");
        seed(&local, &["keep-1", "keep-2"]);
        let before = live_contents(&local);

        let err = reconcile(
            &local,
            "backup@host.invalid:/nmemory/store.sqlite3",
            b"unit-key",
            &UnreachableTransport,
            false,
        )
        .unwrap_err();

        assert!(matches!(err, SyncError::Fetch { .. }), "got {err:?}");
        assert_eq!(live_contents(&local), before); // byte-of-record unchanged
    }

    // 2b. A fetched file that is not a store fails the merge closed, LOCAL
    //     untouched (nothing written — merge_from reads incoming before its
    //     transaction).
    #[test]
    fn garbage_remote_fails_closed_local_untouched() {
        let tmp = tempfile::tempdir().unwrap();
        let local = tmp.path().join("local.sqlite3");
        seed(&local, &["keep-1"]);
        let before = live_contents(&local);

        let err =
            reconcile(&local, "irrelevant", b"unit-key", &GarbageTransport, false).unwrap_err();

        assert!(matches!(err, SyncError::Store(_)), "got {err:?}");
        assert_eq!(live_contents(&local), before);
    }

    // 3. Determinism + idempotency: identical setups reconcile to an identical
    //    summary, and reconciling the same remote a second time adds nothing.
    #[test]
    fn reconcile_is_deterministic_and_idempotent() {
        let run = || {
            let tmp = tempfile::tempdir().unwrap();
            let local = tmp.path().join("local.sqlite3");
            let remote = tmp.path().join("remote.sqlite3");
            seed(&local, &["shared", "local-only"]);
            seed(&remote, &["shared", "remote-1", "remote-2"]);
            let summary = reconcile(
                &local,
                &remote_spec(&remote),
                b"unit-key",
                &LocalCopyTransport,
                false,
            )
            .unwrap();
            // Keep tmp alive until after the second reconcile below.
            (tmp, local, remote, summary)
        };

        let (_t1, _l1, _r1, s1) = run();
        let (_t2, l2, r2, s2) = run();
        // Same inputs → same summary (MergeSummary is Eq).
        assert_eq!(s1, s2);
        assert_eq!(s1.capsules_added, 2);
        assert_eq!(s1.capsules_collapsed, 1);

        // Reconciling the already-merged remote again is a pure no-op merge.
        let again = reconcile(
            &l2,
            &remote_spec(&r2),
            b"unit-key",
            &LocalCopyTransport,
            false,
        )
        .unwrap();
        assert_eq!(again.capsules_added, 0);
        assert_eq!(again.relations_added, 0);
        assert_eq!(again.tombstones_applied, 0);
    }

    // 4. Forget wins THROUGH sync: the remote forgot content LOCAL still holds
    //    live; the reconcile propagates that forget and re-keys the tombstone
    //    under LOCAL's key — the one path that actually consumes `hmac_key`.
    #[test]
    fn reconcile_propagates_forget_wins_across_the_transport() {
        let tmp = tempfile::tempdir().unwrap();
        let local = tmp.path().join("local.sqlite3");
        let remote = tmp.path().join("remote.sqlite3");
        let key = b"forget-key";

        seed(&local, &["shared", "local-only"]);
        {
            let mut r = Store::open(&remote).unwrap();
            let id = r.append(&capsule("shared"), T0).unwrap();
            r.forget_capsule(id.as_str(), TombstoneMode::Purged, "gone upstream", key, T0)
                .unwrap();
        }

        let summary = reconcile(
            &local,
            &remote_spec(&remote),
            key,
            &LocalCopyTransport,
            false,
        )
        .unwrap();

        assert_eq!(summary.tombstones_applied, 1);
        assert_eq!(summary.capsules_added, 0); // "shared" is forgotten, not added
        // LOCAL's "shared" is now forgotten; "local-only" survives.
        assert_eq!(live_contents(&local), vec!["local-only".to_owned()]);
    }

    // 5. Push seam: with push=true the merged LOCAL is copied back, converging
    //    the remote mirror to the union of both sides.
    #[test]
    fn push_converges_the_remote_mirror() {
        let tmp = tempfile::tempdir().unwrap();
        let local = tmp.path().join("local.sqlite3");
        let remote = tmp.path().join("remote.sqlite3");
        seed(&local, &["A", "B"]);
        seed(&remote, &["C"]);

        reconcile(
            &local,
            &remote_spec(&remote),
            b"unit-key",
            &LocalCopyTransport,
            true,
        )
        .unwrap();

        // Both sides now hold the union A, B, C.
        let union = vec!["A".to_owned(), "B".to_owned(), "C".to_owned()];
        assert_eq!(live_contents(&local), union);
        assert_eq!(live_contents(&remote), union);
    }

    // 5b. A push failure leaves LOCAL fully merged (only the mirror is stale).
    #[test]
    fn push_failure_still_leaves_local_merged() {
        let tmp = tempfile::tempdir().unwrap();
        let local = tmp.path().join("local.sqlite3");
        let remote = tmp.path().join("remote.sqlite3");
        seed(&local, &["A"]);
        seed(&remote, &["B"]);
        // Copy the remote out so a same-content source exists for the fetch,
        // then drive with a transport whose fetch works but push fails.
        struct FetchOkPushFails {
            source: PathBuf,
        }
        impl Transport for FetchOkPushFails {
            fn fetch(&self, _remote: &str, dest: &Path) -> Result<(), TransportError> {
                std::fs::copy(&self.source, dest)
                    .map(|_| ())
                    .map_err(|e| TransportError(e.to_string()))
            }
            fn push(&self, _src: &Path, _remote: &str) -> Result<(), TransportError> {
                Err(TransportError("mirror is read-only".to_owned()))
            }
        }
        let transport = FetchOkPushFails {
            source: remote.clone(),
        };

        let err = reconcile(&local, "mirror:/store", b"unit-key", &transport, true).unwrap_err();
        assert!(matches!(err, SyncError::Push { .. }), "got {err:?}");
        // LOCAL merged despite the push failure.
        assert_eq!(live_contents(&local), vec!["A".to_owned(), "B".to_owned()]);
    }

    // 6. Key resolution: env wins; else the key file is created then read back
    //    identically; no source fails closed.
    #[test]
    fn resolve_hmac_key_prefers_env_over_file() {
        let key = resolve_hmac_key(Some(b"env-key".to_vec()), None).unwrap();
        assert_eq!(key, b"env-key");
    }

    #[test]
    fn resolve_hmac_key_creates_then_reads_the_key_file() {
        let tmp = tempfile::tempdir().unwrap();
        let key_file = tmp.path().join("memory.sqlite3.hmac-key");

        // Absent → minted (64 hex chars) and persisted.
        let created = resolve_hmac_key(None, Some(&key_file)).unwrap();
        assert_eq!(created.len(), 64);
        assert!(key_file.exists());
        // Present → read back byte-identical (empty env falls through to file).
        let reread = resolve_hmac_key(Some(Vec::new()), Some(&key_file)).unwrap();
        assert_eq!(created, reread);
    }

    #[test]
    fn resolve_hmac_key_fails_closed_without_a_source() {
        let err = resolve_hmac_key(None, None).unwrap_err();
        assert!(matches!(err, SyncError::NoKey(_)), "got {err:?}");
    }
}
