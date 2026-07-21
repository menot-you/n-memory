//! # nmemory — stdio MCP entrypoint (unit s5).
//!
//! Boot: parse args → resolve the DB path (`--db` > `NMEMORY_DB` >
//! `$XDG_STATE_HOME/nmemory/memory.sqlite3` > `~/.local/state/nmemory/
//! memory.sqlite3`) → resolve the anchor root (`NMEMORY_ANCHOR_ROOT` >
//! the boot cwd) → create parent dirs → open the store → serve the five
//! `memory_*` tools over stdio. stdout carries ONLY protocol frames; the
//! one boot line goes to stderr. No clock read here — `now` is captured
//! per call inside the server boundary (`crate::server`).
//!
//! Donor reference (zero authority): `mcps/memory/src/main.rs` @6d495898,
//! stdio slice only — the HTTP/axum path is not carried.

use std::path::PathBuf;
use std::process::ExitCode;

use nmemory::ingest::IngestDefaults;
use nmemory::server::{BoundaryConfig, MemoryServer};
use nmemory::store::Store;
use nmemory::sync;
use rmcp::ServiceExt;

/// One-line usage, printed with `--help` and on argument errors.
const USAGE: &str = "usage: nmemory [--db <path>] [--project <id>] [--version]\n   or: \
                     nmemory sync --remote <[user@]host:/path> [--db <path>] [--push]";

/// Usage for the `sync` subcommand, printed with `sync --help` and on its
/// argument errors.
const SYNC_USAGE: &str = "usage: nmemory sync --remote <[user@]host:/path> [--db <path>] [--push]";

/// Default `scope.project_id` fence when neither `--project` nor
/// `NMEMORY_PROJECT` names one.
const DEFAULT_PROJECT: &str = "default";

/// Typed boot failures — printed to stderr, exit code 1, never a panic.
#[derive(Debug, thiserror::Error)]
enum BootError {
    /// Malformed command line (fail closed on anything unknown).
    #[error("{0}\n{USAGE}")]
    Usage(String),
    /// Malformed `sync` subcommand line (fail closed on anything unknown).
    #[error("{0}\n{SYNC_USAGE}")]
    SyncUsage(String),
    /// No `--db`, no `NMEMORY_DB`, and neither `XDG_STATE_HOME` nor
    /// `HOME` is set — nowhere to put the database.
    #[error(
        "no database path: pass --db <path>, or set NMEMORY_DB, or set XDG_STATE_HOME/HOME \
         for the default $XDG_STATE_HOME/nmemory/memory.sqlite3"
    )]
    NoDbPath,
    /// No `NMEMORY_ANCHOR_ROOT` and no readable boot cwd — nowhere for
    /// `path:line` anchors to resolve.
    #[error("no anchor root: set NMEMORY_ANCHOR_ROOT, or run from a readable working directory")]
    NoAnchorRoot,
    /// The database's parent directory could not be created.
    #[error("cannot create database directory {dir}: {source}")]
    CreateDir {
        /// Directory that failed to create.
        dir: PathBuf,
        /// Underlying I/O error.
        #[source]
        source: std::io::Error,
    },
    /// The store failed to open at the resolved path.
    #[error("cannot open store: {0}")]
    Store(#[from] nmemory::store::StoreError),
    /// The stdio service failed to initialize or crashed.
    #[error("stdio serve failed: {0}")]
    Serve(String),
    /// The `sync` reconcile failed — a typed sync error; the local store is
    /// left intact (fail-closed, no partial state).
    #[error("{0}")]
    Sync(#[from] sync::SyncError),
}

/// Parsed command line.
#[derive(Debug, Default, PartialEq, Eq)]
struct CliArgs {
    db: Option<PathBuf>,
    project: Option<String>,
    version: bool,
    help: bool,
}

/// A flag's value, rejecting a missing or empty one.
fn take_value(flag: &str, value: Option<&String>) -> Result<String, BootError> {
    match value {
        Some(v) if !v.is_empty() => Ok(v.clone()),
        _ => Err(BootError::Usage(format!("{flag} requires a value"))),
    }
}

/// Parse `argv` (program name already skipped). Unknown arguments fail
/// closed.
fn parse_args(argv: &[String]) -> Result<CliArgs, BootError> {
    let mut args = CliArgs::default();
    let mut it = argv.iter();
    while let Some(arg) = it.next() {
        match arg.as_str() {
            "--version" | "-V" => args.version = true,
            "--help" | "-h" => args.help = true,
            "--db" => args.db = Some(PathBuf::from(take_value("--db", it.next())?)),
            "--project" => args.project = Some(take_value("--project", it.next())?),
            other => {
                if let Some(v) = other.strip_prefix("--db=") {
                    args.db = Some(PathBuf::from(take_value("--db", Some(&v.to_string()))?));
                } else if let Some(v) = other.strip_prefix("--project=") {
                    args.project = Some(take_value("--project", Some(&v.to_string()))?);
                } else {
                    return Err(BootError::Usage(format!("unknown argument {other:?}")));
                }
            }
        }
    }
    Ok(args)
}

/// Parsed `nmemory sync` command line.
#[derive(Debug, Default, PartialEq, Eq)]
struct SyncArgs {
    remote: Option<String>,
    db: Option<PathBuf>,
    push: bool,
    help: bool,
}

/// A sync flag's value, rejecting a missing or empty one — the sync-scoped
/// twin of [`take_value`] so its error trails [`SYNC_USAGE`].
fn sync_value(flag: &str, value: Option<&String>) -> Result<String, BootError> {
    match value {
        Some(v) if !v.is_empty() => Ok(v.clone()),
        _ => Err(BootError::SyncUsage(format!("{flag} requires a value"))),
    }
}

/// Parse `nmemory sync` args (the `sync` token already consumed). Unknown
/// arguments fail closed.
fn parse_sync_args(argv: &[String]) -> Result<SyncArgs, BootError> {
    let mut args = SyncArgs::default();
    let mut it = argv.iter();
    while let Some(arg) = it.next() {
        match arg.as_str() {
            "--help" | "-h" => args.help = true,
            "--push" => args.push = true,
            "--remote" => args.remote = Some(sync_value("--remote", it.next())?),
            "--db" => args.db = Some(PathBuf::from(sync_value("--db", it.next())?)),
            other => {
                if let Some(v) = other.strip_prefix("--remote=") {
                    args.remote = Some(sync_value("--remote", Some(&v.to_string()))?);
                } else if let Some(v) = other.strip_prefix("--db=") {
                    args.db = Some(PathBuf::from(sync_value("--db", Some(&v.to_string()))?));
                } else {
                    return Err(BootError::SyncUsage(format!("unknown argument {other:?}")));
                }
            }
        }
    }
    Ok(args)
}

/// Resolve the database path by fixed precedence: `--db` > `NMEMORY_DB` >
/// `$XDG_STATE_HOME/nmemory/memory.sqlite3` > `$HOME/.local/state/nmemory/
/// memory.sqlite3`. Env values arrive pre-read so the rule is a pure,
/// testable function; empty values count as unset.
fn resolve_db_path(
    cli: Option<PathBuf>,
    env_db: Option<String>,
    xdg_state_home: Option<String>,
    home: Option<String>,
) -> Result<PathBuf, BootError> {
    if let Some(path) = cli {
        return Ok(path);
    }
    if let Some(path) = env_db {
        return Ok(PathBuf::from(path));
    }
    if let Some(xdg) = xdg_state_home {
        return Ok(PathBuf::from(xdg).join("nmemory").join("memory.sqlite3"));
    }
    if let Some(home) = home {
        return Ok(PathBuf::from(home)
            .join(".local")
            .join("state")
            .join("nmemory")
            .join("memory.sqlite3"));
    }
    Err(BootError::NoDbPath)
}

/// Resolve the anchor root — the base every `path:line` anchor resolves
/// against (the `anchor_live`/`anchor_drift` probes and the import
/// bridge's root-relative anchor rendering) — by fixed precedence:
/// `NMEMORY_ANCHOR_ROOT` (non-empty) > the boot cwd (the project the
/// agent runs in, the same value injected as `project_dir`). Inputs
/// arrive pre-read so the rule is a pure, testable function; empty env
/// values count as unset. NEVER a hardcoded default root: with neither
/// source, boot fails typed ([`BootError::NoAnchorRoot`]) instead of
/// guessing a machine layout.
fn resolve_anchor_root(
    env_root: Option<String>,
    boot_cwd: Option<PathBuf>,
) -> Result<PathBuf, BootError> {
    if let Some(root) = env_root {
        return Ok(PathBuf::from(root));
    }
    boot_cwd.ok_or(BootError::NoAnchorRoot)
}

/// Read an environment variable, treating empty/whitespace as unset.
fn env_nonempty(name: &str) -> Option<String> {
    std::env::var(name).ok().filter(|v| !v.trim().is_empty())
}

/// Handle `nmemory sync ...`: reconcile the LOCAL store with a REMOTE mirror
/// through the opt-in transport. Local-first and fail-closed — a bad path
/// leaves the local store intact and exits non-zero. The summary prints to
/// stderr; stdout stays clean (the serve path's stdout discipline).
fn run_sync_command(argv: &[String]) -> Result<(), BootError> {
    let args = parse_sync_args(argv)?;
    if args.help {
        println!("{SYNC_USAGE}");
        return Ok(());
    }
    let remote = args
        .remote
        .filter(|r| !r.trim().is_empty())
        .ok_or_else(|| BootError::SyncUsage("--remote <spec> is required".to_string()))?;

    // Same DB precedence as the serve path (--db > NMEMORY_DB > XDG > HOME).
    let db_path = resolve_db_path(
        args.db,
        env_nonempty("NMEMORY_DB"),
        env_nonempty("XDG_STATE_HOME"),
        env_nonempty("HOME"),
    )?;
    // The local store is always present after a sync (Store::open creates it
    // if absent) — the local-first law; ensure its parent dir exists first.
    if let Some(dir) = db_path.parent().filter(|d| !d.as_os_str().is_empty()) {
        std::fs::create_dir_all(dir).map_err(|source| BootError::CreateDir {
            dir: dir.to_path_buf(),
            source,
        })?;
    }

    // LOCAL's HMAC key, resolved at the boundary EXACTLY like the serve path:
    // NMEMORY_HMAC_KEY (trimmed) wins, else the `<db>.hmac-key` file beside the
    // DB (created on first use). The merge re-keys forget-wins tombstones with
    // it.
    let hmac_key_file = {
        let mut os = db_path.as_os_str().to_os_string();
        os.push(".hmac-key");
        PathBuf::from(os)
    };
    let env_key = env_nonempty("NMEMORY_HMAC_KEY").map(|k| k.trim().to_string().into_bytes());
    let key = sync::resolve_hmac_key(env_key, Some(&hmac_key_file))?;

    // The default production transport shells out to `scp` (a SEPARATE
    // process) — the binary links no network stack, the serve path stays
    // socket-free.
    let transport = sync::ScpTransport::default();
    let summary = sync::reconcile(&db_path, &remote, &key, &transport, args.push)?;

    // Summary to stderr only — stdout stays clean.
    eprintln!(
        "nmemory sync · {} -> {} · +{} capsules, {} collapsed, +{} relations, \
         {} tombstones, remap {}{}",
        remote,
        db_path.display(),
        summary.capsules_added,
        summary.capsules_collapsed,
        summary.relations_added,
        summary.tombstones_applied,
        summary.id_remap_size,
        if args.push { " · pushed" } else { "" },
    );
    Ok(())
}

async fn run() -> Result<(), BootError> {
    let argv: Vec<String> = std::env::args().skip(1).collect();
    // The `sync` subcommand is the opt-in reconcile path; the default (no
    // subcommand) is the hermetic stdio serve below.
    if argv.first().map(String::as_str) == Some("sync") {
        return run_sync_command(&argv[1..]);
    }
    let cli = parse_args(&argv)?;
    if cli.version {
        println!("nmemory {}", env!("CARGO_PKG_VERSION"));
        return Ok(());
    }
    if cli.help {
        println!("{USAGE}");
        return Ok(());
    }

    let db_path = resolve_db_path(
        cli.db,
        env_nonempty("NMEMORY_DB"),
        env_nonempty("XDG_STATE_HOME"),
        env_nonempty("HOME"),
    )?;
    if let Some(dir) = db_path.parent().filter(|d| !d.as_os_str().is_empty()) {
        std::fs::create_dir_all(dir).map_err(|source| BootError::CreateDir {
            dir: dir.to_path_buf(),
            source,
        })?;
    }
    let project = cli
        .project
        .or_else(|| env_nonempty("NMEMORY_PROJECT"))
        .unwrap_or_else(|| DEFAULT_PROJECT.to_string());

    let store = Store::open(&db_path)?;
    // Boot line to stderr only — stdout is the MCP protocol channel.
    eprintln!(
        "nmemory {} serving stdio · db {} · default project {project}",
        env!("CARGO_PKG_VERSION"),
        db_path.display()
    );

    // Boundary knowledge, resolved ONCE here: audit actor, forget-key
    // sources (env wins; else a key file beside the DB, created on first
    // forget), the import base dirs (home + boot cwd), and the anchor
    // root every `path:line` anchor resolves against
    // (NMEMORY_ANCHOR_ROOT > boot cwd — never a compiled-in path).
    // Everything ambient stays at this boundary — the server handlers
    // never read env.
    let hmac_key_file = {
        let mut os = db_path.as_os_str().to_os_string();
        os.push(".hmac-key");
        PathBuf::from(os)
    };
    let boot_cwd = std::env::current_dir().ok();
    let anchor_root = resolve_anchor_root(env_nonempty("NMEMORY_ANCHOR_ROOT"), boot_cwd.clone())?;
    let config = BoundaryConfig {
        actor: "mcp-caller".to_string(),
        hmac_env_key: env_nonempty("NMEMORY_HMAC_KEY").map(|k| k.trim().to_string().into_bytes()),
        hmac_key_file: Some(hmac_key_file),
        home_dir: env_nonempty("HOME").map(PathBuf::from),
        project_dir: boot_cwd,
        anchor_root,
    };
    let server = MemoryServer::new(
        store,
        IngestDefaults {
            project_id: project,
        },
        config,
    );
    let service = server
        .serve(rmcp::transport::io::stdio())
        .await
        .map_err(|e| BootError::Serve(e.to_string()))?;
    service
        .waiting()
        .await
        .map_err(|e| BootError::Serve(e.to_string()))?;
    Ok(())
}

#[tokio::main]
async fn main() -> ExitCode {
    match run().await {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("nmemory: {e}");
            ExitCode::FAILURE
        }
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

    fn argv(args: &[&str]) -> Vec<String> {
        args.iter().map(|s| (*s).to_string()).collect()
    }

    #[test]
    fn parse_args_flag_forms() {
        assert_eq!(parse_args(&argv(&[])).unwrap(), CliArgs::default());

        let parsed = parse_args(&argv(&["--db", "/tmp/x.sqlite3", "--project", "nott"])).unwrap();
        assert_eq!(
            parsed.db.as_deref(),
            Some(std::path::Path::new("/tmp/x.sqlite3"))
        );
        assert_eq!(parsed.project.as_deref(), Some("nott"));

        let parsed = parse_args(&argv(&["--db=/tmp/y.sqlite3", "--project=lab"])).unwrap();
        assert_eq!(
            parsed.db.as_deref(),
            Some(std::path::Path::new("/tmp/y.sqlite3"))
        );
        assert_eq!(parsed.project.as_deref(), Some("lab"));

        assert!(parse_args(&argv(&["--version"])).unwrap().version);
        assert!(parse_args(&argv(&["-V"])).unwrap().version);
        assert!(parse_args(&argv(&["--help"])).unwrap().help);
    }

    #[test]
    fn parse_args_fails_closed() {
        // Unknown argument.
        assert!(matches!(
            parse_args(&argv(&["--verbose"])),
            Err(BootError::Usage(_))
        ));
        // Missing / empty values.
        assert!(matches!(
            parse_args(&argv(&["--db"])),
            Err(BootError::Usage(_))
        ));
        assert!(matches!(
            parse_args(&argv(&["--db="])),
            Err(BootError::Usage(_))
        ));
        assert!(matches!(
            parse_args(&argv(&["--project"])),
            Err(BootError::Usage(_))
        ));
    }

    #[test]
    fn parse_sync_args_flag_forms() {
        assert_eq!(parse_sync_args(&argv(&[])).unwrap(), SyncArgs::default());

        let parsed = parse_sync_args(&argv(&[
            "--remote",
            "u@h:/m.sqlite3",
            "--db",
            "/l.sqlite3",
            "--push",
        ]))
        .unwrap();
        assert_eq!(parsed.remote.as_deref(), Some("u@h:/m.sqlite3"));
        assert_eq!(
            parsed.db.as_deref(),
            Some(std::path::Path::new("/l.sqlite3"))
        );
        assert!(parsed.push);

        // `--flag=value` forms, and push defaults off.
        let parsed =
            parse_sync_args(&argv(&["--remote=u@h:/m.sqlite3", "--db=/l.sqlite3"])).unwrap();
        assert_eq!(parsed.remote.as_deref(), Some("u@h:/m.sqlite3"));
        assert_eq!(
            parsed.db.as_deref(),
            Some(std::path::Path::new("/l.sqlite3"))
        );
        assert!(!parsed.push);

        assert!(parse_sync_args(&argv(&["--help"])).unwrap().help);
    }

    #[test]
    fn parse_sync_args_fails_closed() {
        // Missing / empty --remote value.
        assert!(matches!(
            parse_sync_args(&argv(&["--remote"])),
            Err(BootError::SyncUsage(_))
        ));
        assert!(matches!(
            parse_sync_args(&argv(&["--remote="])),
            Err(BootError::SyncUsage(_))
        ));
        // Unknown argument.
        assert!(matches!(
            parse_sync_args(&argv(&["--bogus"])),
            Err(BootError::SyncUsage(_))
        ));
    }

    #[test]
    fn resolve_db_path_precedence() {
        let cli = Some(PathBuf::from("/cli/path.sqlite3"));
        let env_db = Some("/env/db.sqlite3".to_string());
        let xdg = Some("/xdg-state".to_string());
        let home = Some("/home/user".to_string());

        // --db wins over everything.
        assert_eq!(
            resolve_db_path(cli.clone(), env_db.clone(), xdg.clone(), home.clone()).unwrap(),
            PathBuf::from("/cli/path.sqlite3")
        );
        // NMEMORY_DB next.
        assert_eq!(
            resolve_db_path(None, env_db, xdg.clone(), home.clone()).unwrap(),
            PathBuf::from("/env/db.sqlite3")
        );
        // XDG_STATE_HOME next.
        assert_eq!(
            resolve_db_path(None, None, xdg, home.clone()).unwrap(),
            PathBuf::from("/xdg-state/nmemory/memory.sqlite3")
        );
        // HOME fallback.
        assert_eq!(
            resolve_db_path(None, None, None, home).unwrap(),
            PathBuf::from("/home/user/.local/state/nmemory/memory.sqlite3")
        );
        // Nothing set → typed failure, fail closed.
        assert!(matches!(
            resolve_db_path(None, None, None, None),
            Err(BootError::NoDbPath)
        ));
    }

    #[test]
    fn resolve_anchor_root_precedence() {
        // NMEMORY_ANCHOR_ROOT wins over the boot cwd.
        assert_eq!(
            resolve_anchor_root(
                Some("/env/anchor-root".to_string()),
                Some(PathBuf::from("/boot/cwd"))
            )
            .unwrap(),
            PathBuf::from("/env/anchor-root")
        );
        // Boot cwd next — the generic default: the project the agent
        // runs in, where `path:line` anchors resolve.
        assert_eq!(
            resolve_anchor_root(None, Some(PathBuf::from("/boot/cwd"))).unwrap(),
            PathBuf::from("/boot/cwd")
        );
        // Neither → typed failure, fail closed — NEVER a hardcoded root.
        assert!(matches!(
            resolve_anchor_root(None, None),
            Err(BootError::NoAnchorRoot)
        ));
    }
}
