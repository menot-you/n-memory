//! # nmemory git — the GIT WITNESS lane (S2), reached ONLY from a CLI verb.
//!
//! Git is a WITNESS of stored capsules, never a new truth lane. This module
//! probes a repository for evidence that a capsule's anchor still resolves
//! (path exists, content unchanged, `@sha` reachable) and that commits cite
//! the capsule (`cap-<n>` / `d<N>-slug` mentions), and records each
//! observation into the append-only `corroborations` sidecar. It NEVER
//! creates a capsule, moves a tier, or writes a confidence — a witness
//! observes, it does not decide (`ARCHITECTURE.md` §1–2, the plan's core
//! law).
//!
//! ## Laws this module holds
//!
//! - **The serve path runs no git.** Git is reached ONLY through the
//!   injectable [`GitReader`] seam; the default [`SubprocessGit`] shells out
//!   to an EXTERNAL `git` (`std::process`), so the binary links no network
//!   stack and the stdio serve path spawns no process. `src/server.rs`
//!   NEVER names this module (compile-time layering — [`scan`] is invoked
//!   only from `main.rs`'s `git-scan` verb). Tests inject a fixture
//!   [`GitReader`] and touch no host.
//! - **No guess.** A probe that cannot answer (a non-`path:line` anchor, an
//!   absolute or `..`-traversing or symlinked path, an unreadable file, an
//!   `@sha` on a bare anchor) records NOTHING — never a fabricated verdict
//!   (the `anchor_hashes` "never a guess" rule; the `corroborations` table
//!   has no `unknown` verdict on purpose).
//! - **Root-fenced.** Anchor paths resolve UNDER `--repo` with the same
//!   symlink-refusing, absolute/`..`-rejecting walk the `anchor_live` probe
//!   uses ([`crate::retrieve`]); a path that does not resolve under the repo
//!   is SKIPPED, never a false `missing`.
//! - **Idempotent.** Every write is change-gated ([`Store::append_corroboration`]
//!   appends only on a verdict change); the mention lane pages a fixed
//!   completed-cursor→target range and checkpoints incomplete traversal, so
//!   re-scanning an unchanged tree writes zero corroboration rows.

use std::path::{Component, Path};
use std::process::Command;

use time::OffsetDateTime;

use crate::capsule::sha256_hex;
use crate::store::{ListFilter, Store, StoreError};

/// One commit as the mention lane reads it — sha plus the message split into
/// its subject (first line) and body (the rest). Both are scanned for
/// citations; neither is executed.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GitCommit {
    /// The full commit hash (`%H`).
    pub sha: String,
    /// The commit subject (`%s`, the first message line).
    pub subject: String,
    /// The commit body (`%b`, everything after the subject).
    pub body: String,
}

/// Typed git failures. A SPAWN failure (git absent) or a command that exited
/// unsuccessfully is an `Err`; a probe whose "no" is a legitimate answer
/// (e.g. an unreachable object) is NOT an error — see
/// [`GitReader::sha_reachable`].
#[derive(Debug, thiserror::Error)]
pub enum GitError {
    /// The `git` program could not be launched at all.
    #[error("cannot run {program:?}: {source}")]
    Spawn {
        /// The program that could not be launched.
        program: String,
        /// The OS spawn error.
        #[source]
        source: std::io::Error,
    },
    /// A git subcommand exited non-zero (its own stderr carried through).
    #[error("git {op} exited unsuccessfully ({status}): {stderr}")]
    Command {
        /// The logical operation (`head` / `log`), for the message.
        op: String,
        /// The process exit status, stringified.
        status: String,
        /// The command's captured stderr, trimmed.
        stderr: String,
    },
    /// A git subcommand's stdout could not be decoded/parsed.
    #[error("git {op} produced unusable output: {reason}")]
    Parse {
        /// The logical operation, for the message.
        op: String,
        /// Why the output could not be used.
        reason: String,
    },
}

/// The pluggable git seam — the ONLY thing in this module that touches a
/// repository. Production uses [`SubprocessGit`] (an external command, no
/// linked network stack); tests inject a fixture and reach no host. The
/// serve path never names this trait.
pub trait GitReader {
    /// The repository's current `HEAD` commit sha — the scan reference. A
    /// non-repository or a repo with no commits fails closed
    /// ([`GitError::Command`]) BEFORE the scan touches the store.
    ///
    /// # Errors
    /// [`GitError`] when `git` cannot run or `rev-parse HEAD` fails.
    fn head(&self, repo: &Path) -> Result<String, GitError>;

    /// Whether `sha` names a reachable commit object in `repo`
    /// (`git cat-file -e <sha>^{commit}`). A missing object is `Ok(false)`
    /// (a legitimate answer, not an error); only a spawn failure is `Err`.
    ///
    /// # Errors
    /// [`GitError::Spawn`] when `git` cannot be launched.
    fn sha_reachable(&self, repo: &Path, sha: &str) -> Result<bool, GitError>;

    /// One newest-first page from the fixed `base_cursor..target_head`
    /// range (or the whole target history when `base_cursor` is `None`).
    ///
    /// # Errors
    /// [`GitError`] when `git` cannot run, `git log` fails, or its output
    /// cannot be parsed.
    fn log_page(
        &self,
        repo: &Path,
        base_cursor: Option<&str>,
        target_head: &str,
        skip: usize,
        max: usize,
    ) -> Result<Vec<GitCommit>, GitError>;
}

/// The default production reader: shell out to the system `git`. It links NO
/// network stack into the binary — every call runs in a SEPARATE process
/// (`std::process`), mirroring [`crate::sync::ScpTransport`]. Tests never
/// execute this.
#[derive(Debug, Clone)]
pub struct SubprocessGit {
    /// The git program to invoke — `git` by default.
    program: String,
}

impl Default for SubprocessGit {
    fn default() -> Self {
        SubprocessGit {
            program: "git".to_string(),
        }
    }
}

impl SubprocessGit {
    /// A reader driving a specific `program` (default: `git`).
    #[must_use]
    pub fn new(program: impl Into<String>) -> Self {
        SubprocessGit {
            program: program.into(),
        }
    }

    /// Run `git -C <repo> <args...>`, capturing stdout/stderr so nothing
    /// leaks to this process's streams. Returns stdout on success; a
    /// non-zero exit is [`GitError::Command`] with the command's own stderr.
    fn run(&self, op: &str, repo: &Path, args: &[&str]) -> Result<String, GitError> {
        let output = Command::new(&self.program)
            .arg("-C")
            .arg(repo)
            .args(args)
            .output()
            .map_err(|source| GitError::Spawn {
                program: self.program.clone(),
                source,
            })?;
        if !output.status.success() {
            return Err(GitError::Command {
                op: op.to_string(),
                status: output.status.to_string(),
                stderr: String::from_utf8_lossy(&output.stderr).trim().to_string(),
            });
        }
        // Lossy on purpose: commit messages carry arbitrary author bytes, and
        // one non-UTF-8 commit anywhere in a page would otherwise abort the
        // whole scan as a Parse error. Everything this module extracts from
        // stdout — shas, the record separators, the `cap-N` / slug mention
        // grammar — is ASCII, which lossy decoding maps through byte-exact;
        // only unparsed message bytes can become replacement characters.
        Ok(String::from_utf8_lossy(&output.stdout).into_owned())
    }
}

/// Field separator inside one commit's `git log` record (ASCII unit
/// separator — never appears in commit text) and record separator between
/// commits (ASCII record separator). Both are control characters, so a
/// commit subject or body carrying newlines never confuses the split.
const LOG_FIELD_SEP: char = '\u{1f}';
const LOG_RECORD_SEP: char = '\u{1e}';

impl GitReader for SubprocessGit {
    fn head(&self, repo: &Path) -> Result<String, GitError> {
        let out = self.run("head", repo, &["rev-parse", "HEAD^{commit}"])?;
        let sha = out.trim();
        if sha.is_empty() {
            return Err(GitError::Parse {
                op: "head".to_string(),
                reason: "rev-parse HEAD returned nothing".to_string(),
            });
        }
        Ok(sha.to_string())
    }

    fn sha_reachable(&self, repo: &Path, sha: &str) -> Result<bool, GitError> {
        // A missing object is a legitimate "no", not an error: run cat-file
        // and read the exit status directly rather than through `run` (which
        // maps a non-zero exit to Err).
        let spec = format!("{sha}^{{commit}}");
        let output = Command::new(&self.program)
            .arg("-C")
            .arg(repo)
            .args(["cat-file", "-e", &spec])
            .output()
            .map_err(|source| GitError::Spawn {
                program: self.program.clone(),
                source,
            })?;
        Ok(output.status.success())
    }

    fn log_page(
        &self,
        repo: &Path,
        base_cursor: Option<&str>,
        target_head: &str,
        skip: usize,
        max: usize,
    ) -> Result<Vec<GitCommit>, GitError> {
        let format = format!("--format=%H{LOG_FIELD_SEP}%s{LOG_FIELD_SEP}%b{LOG_RECORD_SEP}");
        let limit = format!("-{max}");
        let skip_arg = format!("--skip={skip}");
        // The target is a fixed commit oid, never moving HEAD. `max` includes
        // the caller's sentinel row when it needs to detect truncation.
        let range = base_cursor.map(|cursor| format!("{cursor}..{target_head}"));
        let mut args: Vec<&str> = vec!["log", "--no-notes", &limit, &skip_arg, &format];
        match range.as_deref() {
            Some(r) => args.push(r),
            None => args.push(target_head),
        }
        let out = self.run("log", repo, &args)?;
        Ok(parse_log(&out))
    }
}

/// Parse the `git log` output produced by the record/field-separated format
/// into commits. PURE: control-character split, deterministic, no I/O — the
/// mention lane's testable core.
#[must_use]
pub fn parse_log(out: &str) -> Vec<GitCommit> {
    let mut commits = Vec::new();
    for record in out.split(LOG_RECORD_SEP) {
        // git emits a newline between records after the record separator; a
        // trailing empty record follows the last separator.
        let record = record.trim_start_matches('\n');
        if record.is_empty() {
            continue;
        }
        let mut fields = record.splitn(3, LOG_FIELD_SEP);
        let (Some(sha), Some(subject), body) =
            (fields.next(), fields.next(), fields.next().unwrap_or(""))
        else {
            continue;
        };
        let sha = sha.trim();
        if sha.is_empty() {
            continue;
        }
        commits.push(GitCommit {
            sha: sha.to_string(),
            subject: subject.to_string(),
            body: body.to_string(),
        });
    }
    commits
}

/// The verifiable shape of a capsule anchor: an optional root-relative
/// `path` (present when the anchor is a `path:line`) and an optional `sha`
/// (present when the anchor carries a `@<7..40 hex>` suffix). Anything else
/// — `doc-<id>`, `&<id>`, a bare number or commit sha — yields both `None`
/// (nothing to verify).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ParsedAnchor {
    /// The path before `:<line>`, when the anchor is a `path:line` shape.
    pub path: Option<String>,
    /// The `@sha` suffix, when present and a plausible 7..40-hex object.
    pub sha: Option<String>,
}

/// Parse a capsule anchor into its verifiable parts. PURE. Mirrors the
/// `anchor_live` split ([`crate::retrieve`]): the `path:line` split takes
/// the LAST `:` whose suffix is all digits, so a Windows-style or
/// colon-bearing path keeps its earlier colons; a trailing `@<hex>` of 7..40
/// hex digits is peeled first as the `@sha` grammar.
#[must_use]
pub fn parse_anchor(anchor: &str) -> ParsedAnchor {
    let (head, sha) = match anchor.rsplit_once('@') {
        Some((h, s)) if is_hex_7_40(s) => (h, Some(s.to_string())),
        _ => (anchor, None),
    };
    let path = match head.rsplit_once(':') {
        Some((p, line))
            if !p.is_empty() && !line.is_empty() && line.bytes().all(|b| b.is_ascii_digit()) =>
        {
            Some(p.to_string())
        }
        _ => None,
    };
    ParsedAnchor { path, sha }
}

/// Whether `s` is 7..=40 characters, every one a hex digit — the `@sha`
/// anchor grammar (git's short-to-full object-id range).
#[must_use]
pub fn is_hex_7_40(s: &str) -> bool {
    (7..=40).contains(&s.len()) && s.bytes().all(|b| b.is_ascii_hexdigit())
}

/// The three answers the anchor-path probe can give. `Skip` records
/// NOTHING (the no-guess rule) — an absolute/`..`/symlinked/non-resolving
/// path, never a false `missing`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PathVerdict {
    /// The path resolves cleanly under the repo and exists.
    Corroborated,
    /// The path resolves cleanly under the repo and is genuinely absent.
    Missing,
    /// The probe cannot answer — record nothing.
    Skip,
}

/// Probe a root-relative anchor path UNDER `repo` with the same
/// symlink-refusing, absolute/`..`-rejecting walk `anchor_live` uses
/// ([`crate::retrieve`]). PURE given the filesystem; never follows a symlink
/// out of the repo, never reads content, never panics. The repo is known to
/// exist (the caller ran [`GitReader::head`] first), so a genuinely-absent
/// component is `Missing`; anything the walk cannot resolve safely is `Skip`.
#[must_use]
pub fn probe_anchor_path(repo: &Path, rel_path: &str) -> PathVerdict {
    let rel = Path::new(rel_path);
    if rel_path.is_empty()
        || rel.is_absolute()
        || rel
            .components()
            .any(|c| !matches!(c, Component::Normal(_) | Component::CurDir))
    {
        // Root-mismatch guard: a path that cannot resolve under the repo is
        // SKIPPED, never a false `missing`.
        return PathVerdict::Skip;
    }
    let mut probe = repo.to_path_buf();
    let mut probed = false;
    for component in rel.components() {
        let Component::Normal(part) = component else {
            continue; // CurDir: `./x` probes the same path as `x`.
        };
        probe.push(part);
        probed = true;
        match std::fs::symlink_metadata(&probe) {
            // Symlink-refusing: a link is never followed — no verdict about
            // its target can leak, so the probe cannot safely answer.
            Ok(meta) if meta.file_type().is_symlink() => return PathVerdict::Skip,
            Ok(_) => {}
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return PathVerdict::Missing,
            Err(_) => return PathVerdict::Skip,
        }
    }
    if probed {
        PathVerdict::Corroborated
    } else {
        PathVerdict::Skip
    }
}

/// The three answers the anchor-content probe can give. `Skip` records
/// NOTHING.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ContentVerdict {
    /// The file's current bytes still hash to the capture-time hash.
    Corroborated,
    /// The file resolves and reads, but its bytes changed.
    Drifted,
    /// The probe cannot answer — record nothing.
    Skip,
}

/// Compare the anchored file's CURRENT content hash against its capture-time
/// hash. Only a path the [`probe_anchor_path`] walk answers `Corroborated`
/// for is read (the probe IS the fence); a directory anchor, a permission
/// error, or a race after the walk degrades to `Skip`. PURE given the
/// filesystem; uses the same [`sha256_hex`] the capture boundary used, so
/// the two hashes differ only when the bytes did.
#[must_use]
pub fn probe_anchor_content(repo: &Path, rel_path: &str, capture_hash: &str) -> ContentVerdict {
    if probe_anchor_path(repo, rel_path) != PathVerdict::Corroborated {
        return ContentVerdict::Skip;
    }
    match std::fs::read(repo.join(rel_path)) {
        Ok(bytes) => {
            if sha256_hex(&bytes) == capture_hash {
                ContentVerdict::Corroborated
            } else {
                ContentVerdict::Drifted
            }
        }
        Err(_) => ContentVerdict::Skip,
    }
}

/// The capsule citations found in one commit message. PURE data.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Citations {
    /// The `<n>` of each `cap-<n>` cited, deduplicated in order.
    pub cap_ids: Vec<u64>,
    /// Each `d<N>-slug` token cited, deduplicated in order.
    pub slugs: Vec<String>,
}

/// Scan free text for capsule citations at WORD BOUNDARIES. PURE. A
/// `cap-<n>` matches only when `cap-` starts at a boundary and is followed
/// by digits then a boundary (so `recap-3` never matches); a `d<N>-slug`
/// matches `d` + digits + `-` + a `[a-z0-9-]` slug at a boundary. Both are
/// deduplicated in first-appearance order.
#[must_use]
pub fn scan_citations(text: &str) -> Citations {
    let bytes = text.as_bytes();
    let mut cap_ids: Vec<u64> = Vec::new();
    let mut slugs: Vec<String> = Vec::new();
    let mut i = 0usize;
    while i < bytes.len() {
        let at_boundary = i == 0 || !is_word_byte(bytes[i - 1]);
        if at_boundary {
            if let Some((id, next)) = match_cap(bytes, i) {
                if !cap_ids.contains(&id) {
                    cap_ids.push(id);
                }
                i = next;
                continue;
            }
            if let Some((slug, next)) = match_slug(bytes, i) {
                if !slugs.contains(&slug) {
                    slugs.push(slug);
                }
                i = next;
                continue;
            }
        }
        i += 1;
    }
    Citations { cap_ids, slugs }
}

/// A byte that continues an identifier word: ASCII alphanumeric or `_`. A
/// citation must begin and end at a NON-word byte (or a string edge).
fn is_word_byte(b: u8) -> bool {
    b.is_ascii_alphanumeric() || b == b'_'
}

/// Match `cap-<digits>` starting at `i`; returns `(id, index-after)` when the
/// digits are followed by a word boundary. Overflow (an absurdly long digit
/// run) yields `None` — no capsule id overflows `u64`.
fn match_cap(bytes: &[u8], i: usize) -> Option<(u64, usize)> {
    let prefix = b"cap-";
    if bytes.len() < i + prefix.len() || &bytes[i..i + prefix.len()] != prefix {
        return None;
    }
    let mut j = i + prefix.len();
    let start = j;
    while j < bytes.len() && bytes[j].is_ascii_digit() {
        j += 1;
    }
    if j == start {
        return None; // `cap-` with no digits.
    }
    if j < bytes.len() && is_word_byte(bytes[j]) {
        return None; // `cap-3x` — not a clean citation.
    }
    let digits = std::str::from_utf8(&bytes[start..j]).ok()?;
    let id: u64 = digits.parse().ok()?;
    Some((id, j))
}

/// Match `d<digits>-<slug>` starting at `i`; returns `(token, index-after)`
/// when a `[a-z0-9-]` slug of at least one char follows, ending at a word
/// boundary. The whole token (e.g. `d25-palantir-ontology`) is returned for
/// the FTS lookup.
fn match_slug(bytes: &[u8], i: usize) -> Option<(String, usize)> {
    if bytes.get(i) != Some(&b'd') {
        return None;
    }
    let mut j = i + 1;
    let num_start = j;
    while j < bytes.len() && bytes[j].is_ascii_digit() {
        j += 1;
    }
    if j == num_start {
        return None; // `d` with no number.
    }
    if bytes.get(j) != Some(&b'-') {
        return None; // no `-slug` part.
    }
    j += 1;
    let slug_start = j;
    while j < bytes.len()
        && (bytes[j].is_ascii_lowercase() || bytes[j].is_ascii_digit() || bytes[j] == b'-')
    {
        j += 1;
    }
    if j == slug_start {
        return None; // `d25-` with no slug.
    }
    // A trailing word byte other than the slug set (e.g. an uppercase letter)
    // means this was not a clean slug token.
    if j < bytes.len() && is_word_byte(bytes[j]) {
        return None;
    }
    let token = std::str::from_utf8(&bytes[i..j]).ok()?.to_string();
    Some((token, j))
}

/// The witness source label for a repository scan: `git:<canonical path>`.
/// The canonical path stabilises the cursor across relative-path invocations
/// (a symlinked or `..`-bearing `--repo` resolves to one key).
#[must_use]
pub fn source_key_for(repo: &Path) -> String {
    let canonical = std::fs::canonicalize(repo).unwrap_or_else(|_| repo.to_path_buf());
    format!("git:{}", canonical.display())
}

/// What one [`scan`] observed — for the verb's stdout and its audit reason.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ScanSummary {
    /// The scan reference (repository `HEAD` at scan time).
    pub head: String,
    /// Fixed commit whose history page was consumed this invocation.
    pub history_target: String,
    /// The witness source key (`git:<canonical repo path>`).
    pub source_key: String,
    /// Fresh `corroborated` anchor verdicts written this scan.
    pub corroborated: usize,
    /// Fresh `drifted` anchor-content verdicts written this scan.
    pub drifted: usize,
    /// Fresh `missing` anchor verdicts written this scan.
    pub missing: usize,
    /// Fresh mention rows written this scan.
    pub mentions: usize,
    /// Anchors probed but recorded NOTHING (the no-guess skip).
    pub skipped: usize,
    /// Commits read from the mention lane this scan.
    pub commits_scanned: usize,
    /// Whether more commits remain behind this page.
    pub truncated: bool,
    /// Whether the completed source cursor was published to `history_target`.
    pub cursor_advanced: bool,
    /// Next newest-first offset while a fixed target remains incomplete.
    pub next_offset: Option<usize>,
}

/// Errors crossing the scan boundary: a git failure or a store failure.
#[derive(Debug, thiserror::Error)]
pub enum ScanError {
    /// The git seam failed (spawn, non-zero exit, or unparseable output).
    #[error("{0}")]
    Git(#[from] GitError),
    /// The store failed (a write or read against the corroboration sidecars).
    #[error("store: {0}")]
    Store(#[from] StoreError),
    /// A zero budget cannot observe history or prove a page complete.
    #[error("git-scan max_commits must be greater than zero")]
    ZeroCommitLimit,
    /// The sentinel request (`max_commits + 1`) overflowed.
    #[error("git-scan max_commits is too large to reserve a truncation sentinel")]
    CommitLimitOverflow,
}

/// The default cap on commits the mention lane reads per scan.
pub const DEFAULT_MAX_COMMITS: usize = 500;

/// Run one git-witness scan over `repo`, fenced to `project` (a project-id
/// prefix; `None` scans every capsule), through the injected `git` reader,
/// recording verdicts into the corroboration sidecars at the INJECTED `now`.
///
/// Order (fail-closed BEFORE any store write): (1) `HEAD` — a non-repo or
/// empty repo fails here, store untouched; (2) anchor verification over the
/// project-fenced capsule list — `anchor_path`, `anchor_content` (when a
/// capture hash exists), and `anchor_sha` (for an `@sha` anchor); (3) mention
/// scan over a fixed `cursor..target` page — `cap-<n>` per stored capsule,
/// `d<N>-slug` per UNAMBIGUOUS single FTS match; (4) checkpoint the next
/// offset or publish the cursor only after the final page, then write one
/// audit row. NEVER creates a capsule, moves a tier, or writes a confidence.
///
/// # Errors
/// [`ScanError`] on a git or store failure. A git failure at step (1) leaves
/// the store byte-untouched (the verb's fail-closed-on-non-repo contract).
pub fn scan(
    store: &mut Store,
    git: &dyn GitReader,
    repo: &Path,
    project: Option<&str>,
    max_commits: usize,
    now: OffsetDateTime,
) -> Result<ScanSummary, ScanError> {
    if max_commits == 0 {
        return Err(ScanError::ZeroCommitLimit);
    }
    let probe_limit = max_commits
        .checked_add(1)
        .ok_or(ScanError::CommitLimitOverflow)?;
    // (1) HEAD is the scan reference. This runs FIRST, so a non-repository
    // fails closed before a single store write.
    let head = git.head(repo)?;
    let source_key = source_key_for(repo);
    let mut summary = ScanSummary {
        head: head.clone(),
        history_target: head.clone(),
        source_key: source_key.clone(),
        corroborated: 0,
        drifted: 0,
        missing: 0,
        mentions: 0,
        skipped: 0,
        commits_scanned: 0,
        truncated: false,
        cursor_advanced: false,
        next_offset: None,
    };

    // (2) Anchor verification over the project-fenced capsule list.
    let filter = ListFilter {
        project_id: None,
        limit: None,
        project_prefix: project.map(str::to_string),
    };
    let capsules = store.list(filter)?;
    for stored in &capsules {
        let id = stored.id.as_str();
        let anchor = &stored.capsule.provenance().anchor;
        let parsed = parse_anchor(anchor);
        let mut recorded_any = false;

        if let Some(path) = parsed.path.as_deref() {
            match probe_anchor_path(repo, path) {
                PathVerdict::Corroborated => {
                    if store.append_corroboration(
                        id,
                        "git",
                        "anchor_path",
                        path,
                        "corroborated",
                        Some(&head),
                        now,
                    )? {
                        summary.corroborated += 1;
                    }
                    recorded_any = true;
                    // Content drift only when a capture hash exists.
                    if let Some(capture) = store.anchor_hash_of(id)? {
                        match probe_anchor_content(repo, path, &capture) {
                            ContentVerdict::Corroborated => {
                                if store.append_corroboration(
                                    id,
                                    "git",
                                    "anchor_content",
                                    path,
                                    "corroborated",
                                    Some(&head),
                                    now,
                                )? {
                                    summary.corroborated += 1;
                                }
                            }
                            ContentVerdict::Drifted => {
                                if store.append_corroboration(
                                    id,
                                    "git",
                                    "anchor_content",
                                    path,
                                    "drifted",
                                    Some(&head),
                                    now,
                                )? {
                                    summary.drifted += 1;
                                }
                            }
                            ContentVerdict::Skip => {}
                        }
                    }
                }
                PathVerdict::Missing => {
                    if store.append_corroboration(
                        id,
                        "git",
                        "anchor_path",
                        path,
                        "missing",
                        Some(&head),
                        now,
                    )? {
                        summary.missing += 1;
                    }
                    recorded_any = true;
                }
                PathVerdict::Skip => {}
            }
        }

        if let Some(sha) = parsed.sha.as_deref() {
            let verdict = if git.sha_reachable(repo, sha)? {
                "corroborated"
            } else {
                "missing"
            };
            if store.append_corroboration(
                id,
                "git",
                "anchor_sha",
                sha,
                verdict,
                Some(&head),
                now,
            )? {
                match verdict {
                    "corroborated" => summary.corroborated += 1,
                    _ => summary.missing += 1,
                }
            }
            recorded_any = true;
        }

        if !recorded_any {
            summary.skipped += 1;
        }
    }

    // (3) Mention lane over a FIXED base..target. A truncated page persists
    // its next offset while the completed cursor remains unchanged.
    let cursor = store.get_source_cursor(&source_key)?;
    let progress = store.get_source_backfill(&source_key)?;
    let had_progress = progress.is_some();
    let progress_mismatch = progress
        .as_ref()
        .is_some_and(|progress| progress.base_cursor != cursor);
    let (mut base_cursor, mut history_target, mut offset) = match progress.as_ref() {
        Some(progress) if !progress_mismatch => (
            progress.base_cursor.clone(),
            progress.target_head.clone(),
            progress.next_offset,
        ),
        _ => (cursor.clone(), head.clone(), 0),
    };
    let mut commits = if progress_mismatch {
        let fresh = git.log_page(repo, None, &head, 0, probe_limit)?;
        store.clear_source_traversal(&source_key, cursor.as_deref(), progress.as_ref())?;
        base_cursor = None;
        history_target.clone_from(&head);
        offset = 0;
        fresh
    } else {
        match git.log_page(
            repo,
            base_cursor.as_deref(),
            &history_target,
            offset,
            probe_limit,
        ) {
            Ok(commits) => commits,
            Err(_) if had_progress || cursor.is_some() => {
                // Preserve the old state unless the replacement range can be
                // read first. Then clear both cursor and checkpoint together.
                let fresh = git.log_page(repo, None, &head, 0, probe_limit)?;
                store.clear_source_traversal(&source_key, cursor.as_deref(), progress.as_ref())?;
                base_cursor = None;
                history_target.clone_from(&head);
                offset = 0;
                fresh
            }
            Err(error) => return Err(error.into()),
        }
    };
    summary.history_target.clone_from(&history_target);
    summary.truncated = commits.len() > max_commits;
    commits.truncate(max_commits);
    summary.commits_scanned = commits.len();
    for commit in &commits {
        let mut text = commit.subject.clone();
        text.push('\n');
        text.push_str(&commit.body);
        let citations = scan_citations(&text);
        for cap_id in &citations.cap_ids {
            let id = format!("cap-{cap_id}");
            // A commit may cite a capsule that is not (or no longer) stored;
            // only a LIVE stored capsule earns a mention row. `get` returns
            // `Err(Tombstoned)` for a forgotten id, `Ok(None)` for an absent
            // one — neither is a mention. Any OTHER error is a real store
            // fault and MUST propagate: collapsing it into "no mention"
            // would degrade a backend failure into silently recording
            // nothing, against this module's fail-closed contract.
            let live = match store.get(&id) {
                Ok(stored) => stored.is_some(),
                Err(StoreError::Tombstoned { .. }) => false,
                Err(error) => return Err(error.into()),
            };
            if live
                && store.append_corroboration(
                    &id,
                    "git",
                    "mention",
                    &commit.sha,
                    "corroborated",
                    Some(&history_target),
                    now,
                )?
            {
                summary.mentions += 1;
            }
        }
        for slug in &citations.slugs {
            // A slug earns a mention only on an UNAMBIGUOUS single FTS match
            // within the project fence — zero or several matches record
            // nothing (the no-guess rule).
            let hits = store.search_fts_scoped(std::slice::from_ref(slug), None, project, None)?;
            if let [(only, _)] = hits.as_slice()
                && store.append_corroboration(
                    only.id.as_str(),
                    "git",
                    "mention",
                    &commit.sha,
                    "corroborated",
                    Some(&history_target),
                    now,
                )?
            {
                summary.mentions += 1;
            }
        }
    }

    // (4) Checkpoint or atomically publish the fixed target. A replay after
    // a crash is harmless because corroboration appends are change-gated.
    if summary.truncated {
        let next_offset = offset
            .checked_add(commits.len())
            .ok_or(ScanError::CommitLimitOverflow)?;
        store.checkpoint_source_backfill(
            &source_key,
            base_cursor.as_deref(),
            &history_target,
            offset,
            next_offset,
            now,
        )?;
        summary.next_offset = Some(next_offset);
    } else {
        store.complete_source_backfill(
            &source_key,
            base_cursor.as_deref(),
            &history_target,
            offset,
            now,
        )?;
        summary.cursor_advanced = true;
    }
    let reason = format!(
        "head={head} history_target={history_target} corroborated={} drifted={} missing={} \
         mentions={} skipped={} commits={} truncated={} cursor_advanced={} next_offset={:?}",
        summary.corroborated,
        summary.drifted,
        summary.missing,
        summary.mentions,
        summary.skipped,
        summary.commits_scanned,
        summary.truncated,
        summary.cursor_advanced,
        summary.next_offset,
    );
    store.append_audit("git-scan", "corroborate", &source_key, Some(&reason), now)?;
    Ok(summary)
}

#[cfg(test)]
mod tests {
    #![allow(
        clippy::unwrap_used,
        clippy::expect_used,
        reason = "tests use unwrap/expect so fixture failures fail at the assertion site"
    )]

    use std::cell::{Cell, RefCell};
    use std::collections::BTreeSet;

    use time::macros::datetime;

    use super::*;
    use crate::capsule::{AuthorityClass, Capsule, Confidence, Freshness, Provenance, Scope};
    use crate::store::Store;

    /// Fixed injected instant — the store reads no clock and neither may its
    /// tests (the determinism gate).
    const NOW: OffsetDateTime = datetime!(2026-07-23 12:00:00 UTC);

    /// A capsule anchored at `anchor`, in `project`, with content-derived
    /// `source_hash`. The git-witness capture hash is a SEPARATE sidecar
    /// (`set_anchor_hash`), not this `source_hash`.
    fn cap_at(content: &str, project: &str, anchor: &str) -> Capsule {
        Capsule::new(
            content.to_string(),
            Provenance {
                source: "session".to_string(),
                anchor: anchor.to_string(),
                source_hash: sha256_hex(content.as_bytes()),
            },
            Confidence::new(0.9).unwrap(),
            Freshness {
                valid_from: NOW,
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

    /// A fixture [`GitReader`] — controllable HEAD, reachable object set, and
    /// commit log, with cursor semantics (a re-scan whose cursor equals HEAD
    /// reads no commits). No host is touched.
    struct FixtureGit {
        head: String,
        reachable: BTreeSet<String>,
        commits: Vec<GitCommit>,
        fail_head: bool,
    }

    impl GitReader for FixtureGit {
        fn head(&self, _repo: &Path) -> Result<String, GitError> {
            if self.fail_head {
                return Err(GitError::Command {
                    op: "head".to_string(),
                    status: "exit status: 128".to_string(),
                    stderr: "fatal: not a git repository".to_string(),
                });
            }
            Ok(self.head.clone())
        }
        fn sha_reachable(&self, _repo: &Path, sha: &str) -> Result<bool, GitError> {
            Ok(self.reachable.contains(sha))
        }
        fn log_page(
            &self,
            _repo: &Path,
            base_cursor: Option<&str>,
            target_head: &str,
            skip: usize,
            max: usize,
        ) -> Result<Vec<GitCommit>, GitError> {
            // Cursor semantics: a completed range reads no commits.
            if base_cursor == Some(target_head) {
                Ok(Vec::new())
            } else {
                Ok(self.commits.iter().skip(skip).take(max).cloned().collect())
            }
        }
    }

    /// A fixture whose live HEAD advances while log pages remain keyed to
    /// the exact target requested by the scanner.
    type RequestedPage = (Option<String>, String, usize, usize);

    struct MovingHeadGit {
        heads: Vec<String>,
        head_calls: Cell<usize>,
        old_commits: Vec<GitCommit>,
        new_commits: Vec<GitCommit>,
        requested_pages: RefCell<Vec<RequestedPage>>,
    }

    impl GitReader for MovingHeadGit {
        fn head(&self, _repo: &Path) -> Result<String, GitError> {
            let call = self.head_calls.get();
            self.head_calls.set(call + 1);
            Ok(self
                .heads
                .get(call)
                .or_else(|| self.heads.last())
                .expect("moving-head fixture has at least one head")
                .clone())
        }

        fn sha_reachable(&self, _repo: &Path, _sha: &str) -> Result<bool, GitError> {
            Ok(false)
        }

        fn log_page(
            &self,
            _repo: &Path,
            base_cursor: Option<&str>,
            target_head: &str,
            skip: usize,
            max: usize,
        ) -> Result<Vec<GitCommit>, GitError> {
            self.requested_pages.borrow_mut().push((
                base_cursor.map(str::to_string),
                target_head.to_string(),
                skip,
                max,
            ));
            if base_cursor == Some(target_head) {
                return Ok(Vec::new());
            }
            let commits = if target_head == "old-target" {
                &self.old_commits
            } else {
                &self.new_commits
            };
            Ok(commits.iter().skip(skip).take(max).cloned().collect())
        }
    }

    fn commit(sha: &str, subject: &str, body: &str) -> GitCommit {
        GitCommit {
            sha: sha.to_string(),
            subject: subject.to_string(),
            body: body.to_string(),
        }
    }

    #[test]
    fn scan_records_path_content_sha_and_mention_verdicts() {
        let dir = tempfile::tempdir().unwrap();
        let repo = dir.path();
        std::fs::create_dir_all(repo.join("src")).unwrap();
        std::fs::write(repo.join("src/live.rs"), b"live bytes").unwrap();
        std::fs::write(repo.join("src/drift.rs"), b"original").unwrap();

        let mut store = Store::open_in_memory().unwrap();
        // cap-1: live path + matching capture hash → path + content corroborated.
        store
            .append(&cap_at("live capsule", "nott", "src/live.rs:1"), NOW)
            .unwrap();
        store
            .set_anchor_hash("cap-1", &sha256_hex(b"live bytes"), NOW)
            .unwrap();
        // cap-2: capture hash is of the OLD bytes; the file now differs → content drifted.
        store
            .append(&cap_at("drift capsule", "nott", "src/drift.rs:1"), NOW)
            .unwrap();
        store
            .set_anchor_hash("cap-2", &sha256_hex(b"original"), NOW)
            .unwrap();
        std::fs::write(repo.join("src/drift.rs"), b"changed now").unwrap();
        // cap-3: deleted path → path missing (and cited by a commit below).
        store
            .append(&cap_at("gone capsule", "nott", "src/gone.rs:1"), NOW)
            .unwrap();
        // cap-4: an unreachable @sha → sha missing.
        store
            .append(&cap_at("sha capsule", "nott", "notes@deadbeef1"), NOW)
            .unwrap();
        // cap-5: a reachable @sha → sha corroborated.
        store
            .append(&cap_at("reach capsule", "nott", "notes@cafe1234"), NOW)
            .unwrap();

        let fixture = FixtureGit {
            head: "headsha".to_string(),
            reachable: BTreeSet::from(["cafe1234".to_string()]),
            commits: vec![commit("c1", "fixes cap-3 for good", "")],
            fail_head: false,
        };
        let summary = scan(&mut store, &fixture, repo, Some("nott"), 500, NOW).unwrap();

        assert_eq!(
            store
                .latest_corroborations("cap-1")
                .unwrap()
                .unwrap()
                .anchor_path
                .as_deref(),
            Some("corroborated")
        );
        assert_eq!(
            store
                .latest_corroborations("cap-1")
                .unwrap()
                .unwrap()
                .anchor_content
                .as_deref(),
            Some("corroborated")
        );
        assert_eq!(
            store
                .latest_corroborations("cap-2")
                .unwrap()
                .unwrap()
                .anchor_content
                .as_deref(),
            Some("drifted")
        );
        let c3 = store.latest_corroborations("cap-3").unwrap().unwrap();
        assert_eq!(c3.anchor_path.as_deref(), Some("missing"));
        assert_eq!(c3.mentions, 1, "commit c1 cites cap-3");
        assert_eq!(
            store
                .latest_corroborations("cap-4")
                .unwrap()
                .unwrap()
                .anchor_sha
                .as_deref(),
            Some("missing")
        );
        assert_eq!(
            store
                .latest_corroborations("cap-5")
                .unwrap()
                .unwrap()
                .anchor_sha
                .as_deref(),
            Some("corroborated")
        );
        assert_eq!(summary.head, "headsha");
        assert_eq!(
            store
                .get_source_cursor(&source_key_for(repo))
                .unwrap()
                .as_deref(),
            Some("headsha")
        );
    }

    #[test]
    fn slug_mention_records_only_on_an_unambiguous_single_fts_match() {
        let dir = tempfile::tempdir().unwrap();
        let repo = dir.path();
        let mut store = Store::open_in_memory().unwrap();
        // Exactly one capsule carries the slug's distinctive tokens.
        store
            .append(
                &cap_at("the d25 zzuniqueslug decision text", "nott", "doc-1"),
                NOW,
            )
            .unwrap();
        // A second, unrelated capsule — must not be a false match.
        store
            .append(&cap_at("unrelated content here", "nott", "doc-2"), NOW)
            .unwrap();
        let fixture = FixtureGit {
            head: "h".to_string(),
            reachable: BTreeSet::new(),
            commits: vec![commit("c1", "ratify d25-zzuniqueslug", "")],
            fail_head: false,
        };
        scan(&mut store, &fixture, repo, Some("nott"), 500, NOW).unwrap();
        assert_eq!(
            store
                .latest_corroborations("cap-1")
                .unwrap()
                .unwrap()
                .mentions,
            1
        );
        assert!(store.latest_corroborations("cap-2").unwrap().is_none());
    }

    #[test]
    fn rescan_writes_zero_new_corroboration_rows() {
        let dir = tempfile::tempdir().unwrap();
        let repo = dir.path();
        std::fs::write(repo.join("f.rs"), b"bytes").unwrap();
        let mut store = Store::open_in_memory().unwrap();
        store
            .append(&cap_at("cited capsule", "nott", "f.rs:1"), NOW)
            .unwrap();
        let fixture = FixtureGit {
            head: "h1".to_string(),
            reachable: BTreeSet::new(),
            commits: vec![commit("c1", "touches cap-1", "")],
            fail_head: false,
        };
        let first = scan(&mut store, &fixture, repo, Some("nott"), 500, NOW).unwrap();
        assert!(
            first.corroborated + first.mentions >= 2,
            "first scan records rows"
        );
        // A re-scan at the same HEAD re-probes identically and reads no new
        // commits (cursor == HEAD) → zero fresh rows.
        let second = scan(&mut store, &fixture, repo, Some("nott"), 500, NOW).unwrap();
        assert_eq!(
            second.corroborated + second.drifted + second.missing + second.mentions,
            0,
            "an unchanged re-scan writes nothing"
        );
    }

    #[test]
    fn bounded_scan_does_not_advance_past_unseen_history() {
        let dir = tempfile::tempdir().unwrap();
        let repo = dir.path();
        let db = repo.join("memory.sqlite3");
        let mut store = Store::open(&db).unwrap();
        store
            .append(&cap_at("old citation target", "nott", "doc-1"), NOW)
            .unwrap();
        let fixture = FixtureGit {
            head: "fixed-head".to_string(),
            reachable: BTreeSet::new(),
            commits: vec![
                commit("newest", "unrelated", ""),
                commit("middle", "unrelated", ""),
                commit("oldest", "touch cap-1", ""),
            ],
            fail_head: false,
        };
        let key = source_key_for(repo);

        let first = scan(&mut store, &fixture, repo, Some("nott"), 1, NOW).unwrap();
        assert_eq!(first.commits_scanned, 1);
        assert_eq!(
            store.get_source_cursor(&key).unwrap(),
            None,
            "a full page is not proof that the fixed history target is complete"
        );
        assert_eq!(
            store
                .get_source_backfill(&key)
                .unwrap()
                .unwrap()
                .next_offset,
            1
        );

        drop(store);
        let mut store = Store::open(&db).unwrap();
        let second = scan(&mut store, &fixture, repo, Some("nott"), 1, NOW).unwrap();
        assert_eq!(second.next_offset, Some(2));
        assert_eq!(store.get_source_cursor(&key).unwrap(), None);

        let third = scan(&mut store, &fixture, repo, Some("nott"), 1, NOW).unwrap();
        assert!(third.cursor_advanced);
        assert_eq!(third.mentions, 1, "the oldest citation was reached");
        assert_eq!(
            store.get_source_cursor(&key).unwrap().as_deref(),
            Some("fixed-head")
        );
        assert!(store.get_source_backfill(&key).unwrap().is_none());

        let fourth = scan(&mut store, &fixture, repo, Some("nott"), 1, NOW).unwrap();
        assert_eq!(fourth.commits_scanned, 0);
        assert_eq!(fourth.mentions, 0);
    }

    #[test]
    fn zero_commit_budget_fails_before_git_or_store_access() {
        let dir = tempfile::tempdir().unwrap();
        let repo = dir.path();
        let mut store = Store::open_in_memory().unwrap();
        let before = store.canonical_snapshot().unwrap();
        let fixture = FixtureGit {
            head: "must-not-be-read".to_string(),
            reachable: BTreeSet::new(),
            commits: Vec::new(),
            fail_head: true,
        };

        let error = scan(&mut store, &fixture, repo, Some("nott"), 0, NOW).unwrap_err();
        assert!(matches!(error, ScanError::ZeroCommitLimit));
        assert_eq!(store.canonical_snapshot().unwrap(), before);
        assert!(store.list_audit(None, None).unwrap().is_empty());
        let key = source_key_for(repo);
        assert!(store.get_source_cursor(&key).unwrap().is_none());
        assert!(store.get_source_backfill(&key).unwrap().is_none());
    }

    #[test]
    fn exact_size_page_completes_without_a_false_checkpoint() {
        let dir = tempfile::tempdir().unwrap();
        let repo = dir.path();
        let mut store = Store::open_in_memory().unwrap();
        let fixture = FixtureGit {
            head: "exact-target".to_string(),
            reachable: BTreeSet::new(),
            commits: vec![
                commit("newest", "unrelated", ""),
                commit("oldest", "unrelated", ""),
            ],
            fail_head: false,
        };
        let key = source_key_for(repo);

        let summary = scan(&mut store, &fixture, repo, Some("nott"), 2, NOW).unwrap();
        assert_eq!(summary.commits_scanned, 2);
        assert!(!summary.truncated);
        assert!(summary.cursor_advanced);
        assert_eq!(
            store.get_source_cursor(&key).unwrap().as_deref(),
            Some("exact-target")
        );
        assert!(store.get_source_backfill(&key).unwrap().is_none());
    }

    #[test]
    fn moving_head_does_not_retarget_an_incomplete_history_page() {
        let dir = tempfile::tempdir().unwrap();
        let repo = dir.path();
        let mut store = Store::open_in_memory().unwrap();
        store
            .append(&cap_at("old citation target", "nott", "doc-1"), NOW)
            .unwrap();
        let fixture = MovingHeadGit {
            heads: vec!["old-target".to_string(), "new-target".to_string()],
            head_calls: Cell::new(0),
            old_commits: vec![
                commit("old-newest", "unrelated", ""),
                commit("old-oldest", "touch cap-1", ""),
            ],
            new_commits: vec![commit("new-commit", "touch cap-1", "")],
            requested_pages: RefCell::new(Vec::new()),
        };
        let key = source_key_for(repo);

        let first = scan(&mut store, &fixture, repo, Some("nott"), 1, NOW).unwrap();
        assert_eq!(first.head, "old-target");
        assert_eq!(first.history_target, "old-target");
        assert!(first.truncated);

        let second = scan(&mut store, &fixture, repo, Some("nott"), 1, NOW).unwrap();
        assert_eq!(second.head, "new-target", "live HEAD advanced");
        assert_eq!(
            second.history_target, "old-target",
            "the in-progress traversal stays pinned"
        );
        assert!(second.cursor_advanced);
        assert_eq!(
            store.get_source_cursor(&key).unwrap().as_deref(),
            Some("old-target")
        );

        let third = scan(&mut store, &fixture, repo, Some("nott"), 1, NOW).unwrap();
        assert_eq!(third.history_target, "new-target");
        assert!(third.cursor_advanced);
        assert_eq!(
            store.get_source_cursor(&key).unwrap().as_deref(),
            Some("new-target")
        );

        let targets: Vec<_> = fixture
            .requested_pages
            .borrow()
            .iter()
            .map(|(_, target, skip, _)| (target.clone(), *skip))
            .collect();
        assert_eq!(
            targets,
            vec![
                ("old-target".to_string(), 0),
                ("old-target".to_string(), 1),
                ("new-target".to_string(), 0)
            ]
        );
    }

    #[test]
    fn root_mismatch_anchor_records_nothing() {
        let dir = tempfile::tempdir().unwrap();
        let repo = dir.path();
        let mut store = Store::open_in_memory().unwrap();
        // An absolute anchor and a `..`-traversing anchor cannot resolve under
        // the repo — SKIPPED, never a false `missing`.
        store
            .append(&cap_at("abs", "nott", "/etc/hostname:1"), NOW)
            .unwrap();
        store
            .append(&cap_at("dotdot", "nott", "../escape.rs:1"), NOW)
            .unwrap();
        let fixture = FixtureGit {
            head: "h".to_string(),
            reachable: BTreeSet::new(),
            commits: vec![],
            fail_head: false,
        };
        let summary = scan(&mut store, &fixture, repo, Some("nott"), 500, NOW).unwrap();
        assert!(store.latest_corroborations("cap-1").unwrap().is_none());
        assert!(store.latest_corroborations("cap-2").unwrap().is_none());
        assert_eq!(summary.corroborated + summary.drifted + summary.missing, 0);
        assert_eq!(summary.skipped, 2);
    }

    #[test]
    fn a_scan_never_moves_the_canonical_snapshot() {
        let dir = tempfile::tempdir().unwrap();
        let repo = dir.path();
        std::fs::write(repo.join("f.rs"), b"bytes").unwrap();
        let mut store = Store::open_in_memory().unwrap();
        store
            .append(&cap_at("snap capsule", "nott", "f.rs:1"), NOW)
            .unwrap();
        let before = store.canonical_snapshot().unwrap();
        let fixture = FixtureGit {
            head: "h".to_string(),
            reachable: BTreeSet::new(),
            commits: vec![commit("c1", "cap-1 touched", "")],
            fail_head: false,
        };
        scan(&mut store, &fixture, repo, Some("nott"), 500, NOW).unwrap();
        assert_eq!(
            store.canonical_snapshot().unwrap(),
            before,
            "the witness sidecars never move the capsule comparand"
        );
    }

    #[test]
    fn scan_fails_closed_when_head_fails_and_leaves_the_store_untouched() {
        let dir = tempfile::tempdir().unwrap();
        let repo = dir.path();
        let mut store = Store::open_in_memory().unwrap();
        store.append(&cap_at("x", "nott", "f.rs:1"), NOW).unwrap();
        let snapshot = store.canonical_snapshot().unwrap();
        // HEAD is step 1, before any store write — a non-repo (head fails)
        // leaves the store byte-untouched.
        let fixture = FixtureGit {
            head: String::new(),
            reachable: BTreeSet::new(),
            commits: vec![],
            fail_head: true,
        };
        assert!(scan(&mut store, &fixture, repo, Some("nott"), 500, NOW).is_err());
        assert!(store.list_source_cursors().unwrap().is_empty());
        assert!(store.latest_corroborations("cap-1").unwrap().is_none());
        assert_eq!(store.canonical_snapshot().unwrap(), snapshot);
    }

    #[test]
    fn real_subprocess_git_fails_closed_on_a_non_repo() {
        // The REAL git seam on a REAL non-repository: `git rev-parse HEAD`
        // exits non-zero (or, if git is absent, the spawn fails) — either way
        // the scan fails closed and the store is untouched.
        //
        // Hazard named in review: tempdir() follows TMPDIR, and a TMPDIR that
        // lives inside some repository lets rev-parse climb out of the
        // tempdir, find that parent repo, and run the scan for real — after
        // which a LATER failure would keep is_err() green for the wrong
        // reason. An env ceiling is off the table (this crate forbids
        // `unsafe`, and `set_var` now requires it), so the fence is on the
        // assertion instead: the error must be the HEAD probe itself.
        let dir = tempfile::tempdir().unwrap();
        let repo = dir.path();
        let mut store = Store::open_in_memory().unwrap();
        store.append(&cap_at("x", "nott", "f.rs:1"), NOW).unwrap();
        let git = SubprocessGit::default();
        let error = scan(&mut store, &git, repo, Some("nott"), 500, NOW)
            .expect_err("a non-repository MUST fail the scan");
        match error {
            // The honest outcomes: rev-parse refused the non-repo, or git
            // itself is absent. Anything else means the scan got PAST the
            // head probe — i.e. discovery escaped the tempdir into a parent
            // repository and this test stopped testing what it names.
            ScanError::Git(GitError::Command { ref op, .. }) => {
                assert_eq!(
                    op, "head",
                    "the failure is the head probe, not a later step"
                );
            }
            ScanError::Git(GitError::Spawn { .. }) => {}
            other => panic!("scan failed past the head probe: {other}"),
        }
        assert!(store.list_source_cursors().unwrap().is_empty());
        assert!(store.latest_corroborations("cap-1").unwrap().is_none());
    }

    #[test]
    fn serve_path_references_no_git_module_and_spawns_no_subprocess() {
        // Compile-time layering (S2 core law): the MCP serve path (server.rs)
        // NEVER names the git module, and neither it nor retrieve.rs spawns a
        // subprocess — so a digest/retrieve over a store WITH corroboration
        // rows can reach no git (the read path is store-only SQL). Mold:
        // conformance_zero_python source scan.
        let server = include_str!("server.rs");
        let retrieve = include_str!("retrieve.rs");
        for needle in ["crate::git", concat!("git", "::"), concat!("mod ", "git")] {
            assert!(
                !server.contains(needle),
                "server.rs must not reference the git module ({needle:?})"
            );
        }
        for (name, src) in [("server.rs", server), ("retrieve.rs", retrieve)] {
            for needle in [concat!("Command", "::new"), concat!("std::", "process")] {
                assert!(
                    !src.contains(needle),
                    "{name} must spawn no subprocess ({needle:?})"
                );
            }
        }
    }

    #[test]
    fn parse_anchor_splits_path_line_and_sha() {
        assert_eq!(
            parse_anchor("src/store.rs:520"),
            ParsedAnchor {
                path: Some("src/store.rs".to_string()),
                sha: None,
            }
        );
        assert_eq!(
            parse_anchor("src/store.rs:520@adf8d93"),
            ParsedAnchor {
                path: Some("src/store.rs".to_string()),
                sha: Some("adf8d93".to_string()),
            }
        );
        // A bare commit sha or doc anchor is not a path:line — nothing to
        // verify by path.
        assert_eq!(
            parse_anchor("doc-1361"),
            ParsedAnchor {
                path: None,
                sha: None,
            }
        );
        // A too-short `@` suffix is not the sha grammar; the tail stays part
        // of the head (and here yields no path).
        assert_eq!(parse_anchor("PLAN.md@v2").sha, None);
    }

    #[test]
    fn is_hex_7_40_bounds() {
        assert!(!is_hex_7_40("abc123")); // 6 — too short
        assert!(is_hex_7_40("abc1234")); // 7 — ok
        assert!(is_hex_7_40(&"a".repeat(40)));
        assert!(!is_hex_7_40(&"a".repeat(41))); // too long
        assert!(!is_hex_7_40("abcxyzg")); // non-hex
    }

    #[test]
    fn scan_citations_respects_word_boundaries_and_dedupes() {
        let text =
            "fixes cap-3 and cap-3 again, ratifies d25-palantir-ontology; not recap-9 nor capx-1";
        let c = scan_citations(text);
        assert_eq!(c.cap_ids, vec![3]);
        assert_eq!(c.slugs, vec!["d25-palantir-ontology".to_string()]);
    }

    #[test]
    fn scan_citations_ignores_embedded_and_malformed() {
        assert_eq!(
            scan_citations("recap-1 uncap-2 cap- capxyz").cap_ids,
            Vec::<u64>::new()
        );
        assert_eq!(
            scan_citations("d-slug d25 d25-").slugs,
            Vec::<String>::new()
        );
    }

    #[test]
    fn parse_log_splits_records_and_fields() {
        let out = format!(
            "abc123{f}subject one{f}body\nline two{r}\ndef456{f}subject two{f}{r}\n",
            f = LOG_FIELD_SEP,
            r = LOG_RECORD_SEP
        );
        let commits = parse_log(&out);
        assert_eq!(commits.len(), 2);
        assert_eq!(commits[0].sha, "abc123");
        assert_eq!(commits[0].subject, "subject one");
        assert_eq!(commits[0].body, "body\nline two");
        assert_eq!(commits[1].sha, "def456");
        assert_eq!(commits[1].body, "");
    }

    #[test]
    fn probe_anchor_path_skips_out_of_root_and_symlinks() {
        let dir = tempfile::tempdir().unwrap();
        let repo = dir.path();
        std::fs::create_dir_all(repo.join("src")).unwrap();
        std::fs::write(repo.join("src/store.rs"), b"content").unwrap();

        assert_eq!(
            probe_anchor_path(repo, "src/store.rs"),
            PathVerdict::Corroborated
        );
        assert_eq!(
            probe_anchor_path(repo, "src/absent.rs"),
            PathVerdict::Missing
        );
        // Absolute and `..`-traversing paths cannot resolve under the repo.
        assert_eq!(probe_anchor_path(repo, "/etc/hostname"), PathVerdict::Skip);
        assert_eq!(probe_anchor_path(repo, "../escape.rs"), PathVerdict::Skip);
    }

    #[test]
    fn probe_anchor_content_reports_drift() {
        let dir = tempfile::tempdir().unwrap();
        let repo = dir.path();
        std::fs::write(repo.join("f.txt"), b"original").unwrap();
        let capture = sha256_hex(b"original");
        assert_eq!(
            probe_anchor_content(repo, "f.txt", &capture),
            ContentVerdict::Corroborated
        );
        std::fs::write(repo.join("f.txt"), b"changed").unwrap();
        assert_eq!(
            probe_anchor_content(repo, "f.txt", &capture),
            ContentVerdict::Drifted
        );
        // A non-resolving path never reads content.
        assert_eq!(
            probe_anchor_content(repo, "/etc/hostname", &capture),
            ContentVerdict::Skip
        );
    }
}
