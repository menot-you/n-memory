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
use nmemory::server::{BoundaryConfig, DigestParams, MemoryServer, RetrieveParams};
use nmemory::store::Store;
use nmemory::sync;
use rmcp::ServiceExt;
use rmcp::handler::server::wrapper::Parameters;
use rmcp::model::CallToolResult;

/// One-line usage, printed with `--help` and on argument errors.
const USAGE: &str = "usage: nmemory [--db <path>] [--project <id>] [--version]\n   or: \
                     nmemory sync --remote <[user@]host:/path> [--db <path>] [--push]\n   or: \
                     nmemory recall --terms <term[,term...]> [--limit <n>] [--budget <n>] \
                     [--project-prefix <p>] [--db <path>]\n   or: nmemory digest \
                     [--project-prefix <p>] [--db <path>]";

/// Usage for the `sync` subcommand, printed with `sync --help` and on its
/// argument errors.
const SYNC_USAGE: &str = "usage: nmemory sync --remote <[user@]host:/path> [--db <path>] [--push]";

/// Usage for the `recall` subcommand, printed with `recall --help` and on
/// its argument errors.
const RECALL_USAGE: &str = "usage: nmemory recall --terms <term[,term...]> [--terms ...] \
                            [--limit <n>] [--budget <n>] [--project-prefix <p>] [--db <path>]";

/// Usage for the `digest` subcommand, printed with `digest --help` and on
/// its argument errors.
const DIGEST_USAGE: &str = "usage: nmemory digest [--project-prefix <p>] [--db <path>]";

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
    /// Malformed `recall` subcommand line (fail closed on anything unknown).
    #[error("{0}\n{RECALL_USAGE}")]
    RecallUsage(String),
    /// Malformed `digest` subcommand line (fail closed on anything unknown).
    #[error("{0}\n{DIGEST_USAGE}")]
    DigestUsage(String),
    /// A one-shot verb's tool handler answered with an error — surfaced
    /// verbatim on stderr, exit 1; the hook callers stay fail-open.
    #[error("{verb}: {message}")]
    Verb {
        /// The MCP tool name whose handler failed.
        verb: &'static str,
        /// The handler's error message.
        message: String,
    },
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

/// Parsed `nmemory recall` command line.
#[derive(Debug, Default, PartialEq, Eq)]
struct RecallArgs {
    terms: Vec<String>,
    limit: Option<usize>,
    budget: Option<usize>,
    project_prefix: Option<String>,
    db: Option<PathBuf>,
    help: bool,
}

/// Parsed `nmemory digest` command line.
#[derive(Debug, Default, PartialEq, Eq)]
struct DigestArgs {
    project_prefix: Option<String>,
    db: Option<PathBuf>,
    help: bool,
}

/// A one-shot-verb flag's value, rejecting a missing or empty one — the
/// [`take_value`] rule with the verb's own usage error attached (the
/// [`sync_value`] idiom, shared by `recall` and `digest`).
fn verb_value(
    flag: &str,
    value: Option<&String>,
    usage: fn(String) -> BootError,
) -> Result<String, BootError> {
    match value {
        Some(v) if !v.is_empty() => Ok(v.clone()),
        _ => Err(usage(format!("{flag} requires a value"))),
    }
}

/// A one-shot-verb numeric flag, failing closed on anything that is not a
/// non-negative integer.
fn verb_usize(flag: &str, value: &str, usage: fn(String) -> BootError) -> Result<usize, BootError> {
    value.parse::<usize>().map_err(|_| {
        usage(format!(
            "{flag} requires a non-negative integer, got {value:?}"
        ))
    })
}

/// Split one `--terms` value on commas into individual recall terms,
/// trimming whitespace and dropping empty fragments. A multi-word term
/// stays intact ("tokio pin,rust" → ["tokio pin", "rust"]) — within a
/// term the engine AND-matches words, exactly as over MCP.
fn split_terms(value: &str) -> Vec<String> {
    value
        .split(',')
        .map(str::trim)
        .filter(|t| !t.is_empty())
        .map(str::to_string)
        .collect()
}

/// Parse `nmemory recall` args (the `recall` token already consumed).
/// `--terms` is repeatable and comma-splits; unknown arguments fail
/// closed.
fn parse_recall_args(argv: &[String]) -> Result<RecallArgs, BootError> {
    let usage: fn(String) -> BootError = BootError::RecallUsage;
    let mut args = RecallArgs::default();
    let mut it = argv.iter();
    while let Some(arg) = it.next() {
        match arg.as_str() {
            "--help" | "-h" => args.help = true,
            "--terms" => args
                .terms
                .extend(split_terms(&verb_value("--terms", it.next(), usage)?)),
            "--limit" => {
                args.limit = Some(verb_usize(
                    "--limit",
                    &verb_value("--limit", it.next(), usage)?,
                    usage,
                )?);
            }
            "--budget" => {
                args.budget = Some(verb_usize(
                    "--budget",
                    &verb_value("--budget", it.next(), usage)?,
                    usage,
                )?);
            }
            "--project-prefix" => {
                args.project_prefix = Some(verb_value("--project-prefix", it.next(), usage)?);
            }
            "--db" => args.db = Some(PathBuf::from(verb_value("--db", it.next(), usage)?)),
            other => {
                if let Some(v) = other.strip_prefix("--terms=") {
                    args.terms.extend(split_terms(&verb_value(
                        "--terms",
                        Some(&v.to_string()),
                        usage,
                    )?));
                } else if let Some(v) = other.strip_prefix("--limit=") {
                    args.limit = Some(verb_usize(
                        "--limit",
                        &verb_value("--limit", Some(&v.to_string()), usage)?,
                        usage,
                    )?);
                } else if let Some(v) = other.strip_prefix("--budget=") {
                    args.budget = Some(verb_usize(
                        "--budget",
                        &verb_value("--budget", Some(&v.to_string()), usage)?,
                        usage,
                    )?);
                } else if let Some(v) = other.strip_prefix("--project-prefix=") {
                    args.project_prefix =
                        Some(verb_value("--project-prefix", Some(&v.to_string()), usage)?);
                } else if let Some(v) = other.strip_prefix("--db=") {
                    args.db = Some(PathBuf::from(verb_value(
                        "--db",
                        Some(&v.to_string()),
                        usage,
                    )?));
                } else {
                    return Err(usage(format!("unknown argument {other:?}")));
                }
            }
        }
    }
    Ok(args)
}

/// Parse `nmemory digest` args (the `digest` token already consumed).
/// Unknown arguments fail closed.
fn parse_digest_args(argv: &[String]) -> Result<DigestArgs, BootError> {
    let usage: fn(String) -> BootError = BootError::DigestUsage;
    let mut args = DigestArgs::default();
    let mut it = argv.iter();
    while let Some(arg) = it.next() {
        match arg.as_str() {
            "--help" | "-h" => args.help = true,
            "--project-prefix" => {
                args.project_prefix = Some(verb_value("--project-prefix", it.next(), usage)?);
            }
            "--db" => args.db = Some(PathBuf::from(verb_value("--db", it.next(), usage)?)),
            other => {
                if let Some(v) = other.strip_prefix("--project-prefix=") {
                    args.project_prefix =
                        Some(verb_value("--project-prefix", Some(&v.to_string()), usage)?);
                } else if let Some(v) = other.strip_prefix("--db=") {
                    args.db = Some(PathBuf::from(verb_value(
                        "--db",
                        Some(&v.to_string()),
                        usage,
                    )?));
                } else {
                    return Err(usage(format!("unknown argument {other:?}")));
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

/// The opened store plus its resolved ambient boundary — shared by the
/// stdio serve path and the one-shot verbs so a one-shot answer can NEVER
/// drift from what the same binary would answer over MCP.
struct Boundary {
    server: MemoryServer,
    db_path: PathBuf,
    project: String,
}

/// Resolve the full serve boundary ONCE: DB path (`--db` > `NMEMORY_DB` >
/// XDG > HOME) with parent-dir creation, the default ingest project, the
/// opened store, and the ambient [`BoundaryConfig`] — audit actor,
/// forget-key sources (env wins; else a key file beside the DB, created
/// on first forget), the import base dirs (home + boot cwd), and the
/// anchor root every `path:line` anchor resolves against
/// (`NMEMORY_ANCHOR_ROOT` > boot cwd — never a compiled-in path).
/// Everything ambient stays at this boundary — the server handlers never
/// read env. `actor` names the caller class in audit rows ("mcp-caller"
/// for the stdio serve, "cli" for one-shot verbs).
fn open_boundary(
    db: Option<PathBuf>,
    project: Option<String>,
    actor: &str,
) -> Result<Boundary, BootError> {
    let db_path = resolve_db_path(
        db,
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
    let project = project
        .or_else(|| env_nonempty("NMEMORY_PROJECT"))
        .unwrap_or_else(|| DEFAULT_PROJECT.to_string());
    let store = Store::open(&db_path)?;
    let hmac_key_file = {
        let mut os = db_path.as_os_str().to_os_string();
        os.push(".hmac-key");
        PathBuf::from(os)
    };
    let boot_cwd = std::env::current_dir().ok();
    let anchor_root = resolve_anchor_root(env_nonempty("NMEMORY_ANCHOR_ROOT"), boot_cwd.clone())?;
    let config = BoundaryConfig {
        actor: actor.to_string(),
        hmac_env_key: env_nonempty("NMEMORY_HMAC_KEY").map(|k| k.trim().to_string().into_bytes()),
        hmac_key_file: Some(hmac_key_file),
        home_dir: env_nonempty("HOME").map(PathBuf::from),
        project_dir: boot_cwd,
        anchor_root,
    };
    let server = MemoryServer::new(
        store,
        IngestDefaults {
            project_id: project.clone(),
        },
        config,
    );
    Ok(Boundary {
        server,
        db_path,
        project,
    })
}

/// Map a one-shot handler error onto [`BootError::Verb`] — message to
/// stderr, exit 1; the hook callers stay fail-open by their own law.
fn verb_error(verb: &'static str) -> impl Fn(rmcp::ErrorData) -> BootError {
    move |e| BootError::Verb {
        verb,
        message: e.message.to_string(),
    }
}

/// Print a tool result's JSON text content — the SAME bytes the MCP path
/// ships as `result.content[0].text` — to stdout. This is the whole
/// one-shot contract: callers consume the envelope directly, with no
/// JSON-RPC handshake and no protocol unwrapping.
fn print_result_text(verb: &'static str, result: &CallToolResult) -> Result<(), BootError> {
    let text = result
        .content
        .first()
        .and_then(|c| c.as_text())
        .map(|t| t.text.as_str())
        .ok_or_else(|| BootError::Verb {
            verb,
            message: "tool result carried no text content".to_string(),
        })?;
    println!("{text}");
    Ok(())
}

/// Handle `nmemory recall ...`: ONE one-shot `memory_retrieve` through
/// the exact handler the MCP tool runs — same envelope bytes, same
/// usage-counting and recall-miss side effects — with the evidence
/// envelope on stdout. Built for synchronous shell callers (the
/// recall-inject hook) where an MCP initialize exchange is pure added
/// latency.
async fn run_recall_command(argv: &[String]) -> Result<(), BootError> {
    let args = parse_recall_args(argv)?;
    if args.help {
        println!("{RECALL_USAGE}");
        return Ok(());
    }
    if args.terms.is_empty() {
        return Err(BootError::RecallUsage(
            "--terms requires at least one non-empty term".to_string(),
        ));
    }
    let boundary = open_boundary(args.db, None, "cli")?;
    let params = RetrieveParams {
        terms: args.terms,
        project_id: None,
        project_prefix: args.project_prefix,
        limit: args.limit,
        token_budget: args.budget,
        query_embedding: None,
        vector_k: None,
    };
    let result = boundary
        .server
        .retrieve(Parameters(params))
        .await
        .map_err(verb_error("memory_retrieve"))?;
    print_result_text("memory_retrieve", &result)
}

/// Handle `nmemory digest ...`: ONE one-shot `memory_digest` through the
/// exact handler the MCP tool runs, with the digest projection on stdout.
/// Built for the session-digest hook — same rationale as `recall`.
async fn run_digest_command(argv: &[String]) -> Result<(), BootError> {
    let args = parse_digest_args(argv)?;
    if args.help {
        println!("{DIGEST_USAGE}");
        return Ok(());
    }
    let boundary = open_boundary(args.db, None, "cli")?;
    let params = DigestParams {
        headlines: None,
        project_prefix: args.project_prefix,
    };
    let result = boundary
        .server
        .digest(Parameters(params))
        .await
        .map_err(verb_error("memory_digest"))?;
    print_result_text("memory_digest", &result)
}

async fn run() -> Result<(), BootError> {
    let argv: Vec<String> = std::env::args().skip(1).collect();
    // Subcommand dispatch: `sync` is the opt-in reconcile path; `recall`
    // and `digest` are one-shot verbs answering a single tool call over
    // argv/stdout with NO MCP handshake, so a synchronous shell caller
    // pays engine time only; the default (no subcommand) is the hermetic
    // stdio serve below.
    match argv.first().map(String::as_str) {
        Some("sync") => return run_sync_command(&argv[1..]),
        Some("recall") => return run_recall_command(&argv[1..]).await,
        Some("digest") => return run_digest_command(&argv[1..]).await,
        _ => {}
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

    let boundary = open_boundary(cli.db, cli.project, "mcp-caller")?;
    // Boot line to stderr only — stdout is the MCP protocol channel.
    eprintln!(
        "nmemory {} serving stdio · db {} · default project {}",
        env!("CARGO_PKG_VERSION"),
        boundary.db_path.display(),
        boundary.project
    );
    let service = boundary
        .server
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
    fn parse_recall_args_flag_forms() {
        assert_eq!(
            parse_recall_args(&argv(&[])).unwrap(),
            RecallArgs::default()
        );

        // Space-separated flags; --terms repeats and comma-splits, a
        // multi-word term survives intact.
        let parsed = parse_recall_args(&argv(&[
            "--terms",
            "alpha,beta gamma",
            "--terms",
            "delta",
            "--limit",
            "3",
            "--budget",
            "500",
            "--project-prefix",
            "happyday",
            "--db",
            "/l.sqlite3",
        ]))
        .unwrap();
        assert_eq!(parsed.terms, vec!["alpha", "beta gamma", "delta"]);
        assert_eq!(parsed.limit, Some(3));
        assert_eq!(parsed.budget, Some(500));
        assert_eq!(parsed.project_prefix.as_deref(), Some("happyday"));
        assert_eq!(
            parsed.db.as_deref(),
            Some(std::path::Path::new("/l.sqlite3"))
        );

        // `--flag=value` forms; blank comma fragments drop, zeros parse.
        let parsed = parse_recall_args(&argv(&[
            "--terms=x, ,y",
            "--limit=0",
            "--budget=0",
            "--project-prefix=happy",
        ]))
        .unwrap();
        assert_eq!(parsed.terms, vec!["x", "y"]);
        assert_eq!(parsed.limit, Some(0));
        assert_eq!(parsed.budget, Some(0));
        assert_eq!(parsed.project_prefix.as_deref(), Some("happy"));

        assert!(parse_recall_args(&argv(&["--help"])).unwrap().help);
    }

    #[test]
    fn parse_recall_args_fails_closed() {
        // Missing / empty values.
        assert!(matches!(
            parse_recall_args(&argv(&["--terms"])),
            Err(BootError::RecallUsage(_))
        ));
        assert!(matches!(
            parse_recall_args(&argv(&["--terms="])),
            Err(BootError::RecallUsage(_))
        ));
        // Non-numeric and negative counts.
        assert!(matches!(
            parse_recall_args(&argv(&["--limit", "three"])),
            Err(BootError::RecallUsage(_))
        ));
        assert!(matches!(
            parse_recall_args(&argv(&["--budget", "-1"])),
            Err(BootError::RecallUsage(_))
        ));
        // Missing / empty project prefix.
        assert!(matches!(
            parse_recall_args(&argv(&["--project-prefix"])),
            Err(BootError::RecallUsage(_))
        ));
        assert!(matches!(
            parse_recall_args(&argv(&["--project-prefix="])),
            Err(BootError::RecallUsage(_))
        ));
        // Unknown argument.
        assert!(matches!(
            parse_recall_args(&argv(&["--bogus"])),
            Err(BootError::RecallUsage(_))
        ));
    }

    #[test]
    fn parse_digest_args_flag_forms_and_fails_closed() {
        assert_eq!(
            parse_digest_args(&argv(&[])).unwrap(),
            DigestArgs::default()
        );

        let parsed = parse_digest_args(&argv(&[
            "--project-prefix",
            "happyday",
            "--db",
            "/l.sqlite3",
        ]))
        .unwrap();
        assert_eq!(parsed.project_prefix.as_deref(), Some("happyday"));
        assert_eq!(
            parsed.db.as_deref(),
            Some(std::path::Path::new("/l.sqlite3"))
        );
        assert!(
            parse_digest_args(&argv(&["--db=/m.sqlite3"]))
                .unwrap()
                .db
                .is_some()
        );
        assert_eq!(
            parse_digest_args(&argv(&["--project-prefix=happy"]))
                .unwrap()
                .project_prefix
                .as_deref(),
            Some("happy")
        );
        assert!(matches!(
            parse_digest_args(&argv(&["--project-prefix="])),
            Err(BootError::DigestUsage(_))
        ));
        assert!(parse_digest_args(&argv(&["--help"])).unwrap().help);

        assert!(matches!(
            parse_digest_args(&argv(&["--db"])),
            Err(BootError::DigestUsage(_))
        ));
        assert!(matches!(
            parse_digest_args(&argv(&["--bogus"])),
            Err(BootError::DigestUsage(_))
        ));
    }

    #[tokio::test]
    async fn recall_and_digest_one_shot_answer_on_a_fresh_store() {
        // The one-shot path boots the SAME boundary as the serve path and
        // answers over stdout: recall on a fresh store is an honest
        // abstain, digest a valid projection — both must exit Ok, never
        // hang on a handshake.
        let dir = tempfile::tempdir().unwrap();
        let db = dir.path().join("memory.sqlite3");
        let db_flag = format!("--db={}", db.display());
        run_recall_command(&argv(&["--terms", "alpha", &db_flag]))
            .await
            .unwrap();
        run_digest_command(&argv(&[&db_flag])).await.unwrap();
        // A recall with an empty post-split term list fails closed.
        assert!(matches!(
            run_recall_command(&argv(&["--terms", ",", &db_flag])).await,
            Err(BootError::RecallUsage(_))
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
