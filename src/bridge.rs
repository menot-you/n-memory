//! # bridge — governed readers over the owner's file-based memory (W1).
//!
//! Re-authored from donor B `mcps/memory/src/bridge/native.rs` @6d495898
//! (zero authority, reference only). The donor's CLOSED-source discipline
//! and its born-with taint fence CONTRACT carry over; the donor's store
//! writes, taint scanning and capsule construction deliberately do NOT —
//! this module is the pure read+split half of the bridge, and the fence is
//! wired by the integrator (see *Integration contract* below).
//!
//! ## Closed sources — never a directory walk
//!
//! [`BridgeSource`] is a closed enum of exactly the file-based memory this
//! module may ever read:
//!
//! - [`BridgeSource::UserClaudeMd`] — `<base>/.claude3/CLAUDE.md`, else
//!   `<base>/.claude2/CLAUDE.md`, else `<base>/.claude/CLAUDE.md` (first
//!   that exists; `base_dir` is the caller-injected home directory — this
//!   module never consults `$HOME` or any environment variable).
//! - [`BridgeSource::ProjectClaudeMd`] — `<base>/CLAUDE.md`.
//! - [`BridgeSource::ProjectAgentsMd`] — `<base>/AGENTS.md`.
//! - [`BridgeSource::MemoryDir`] — the ONE parameterized source: every
//!   `.md` file (case-sensitive extension) DIRECTLY inside one
//!   caller-supplied directory, via a single non-recursive listing.
//!   Subdirectories are never entered, symlinked entries are never
//!   followed, files are visited in deterministic byte-order of their
//!   names. Nothing outside that one directory level is ever touched.
//!
//! No other variant lists anything: each resolves to fixed, named paths
//! only. For the fixed variants a LEAF that is itself a symlink is
//! rejected ([`BridgeError::SymlinkRejected`]) — a symlink standing in
//! for a whitelisted name redirects the read outside the closed set
//! (donor r1–r3 lesson). Symlinked PARENT directories (the dotfiles
//! pattern, e.g. `~/.claude2` → a dotfiles repo) are legitimate and
//! transparently followed: only the final path component is checked.
//!
//! ## Split rule (deterministic, tested)
//!
//! [`read_source`] splits each file into capsule-sized
//! [`BridgeCandidate`]s:
//!
//! 1. A leading UTF-8 BOM is stripped; a CLOSED YAML frontmatter block
//!    (first line exactly `---`, closing `---` or `...`) is skipped —
//!    frontmatter is config, not memory. An unclosed opener is plain
//!    content.
//! 2. Fenced code blocks (` ``` ` or `~~~`, at any indentation) are
//!    opaque: no heading detection and no paragraph break applies inside
//!    them. An unclosed fence extends to end of file (deterministic).
//! 3. A heading is an ATX line at column 0: 1–6 `#` then whitespace or
//!    end of line, outside any fence.
//! 4. If the document has headings, the split level is the smallest
//!    heading level that occurs at least TWICE (so a lone `# Title` over
//!    `##` sections splits per `##`), else the smallest level present.
//!    Candidates are: the preamble before the first split-level heading
//!    (if non-blank), then one candidate per split-level section, deeper
//!    headings riding along inside their section.
//! 5. With no headings at all, candidates are paragraph blocks: maximal
//!    runs of lines separated by blank lines outside fences.
//! 6. Every candidate is trimmed of leading/trailing blank lines;
//!    whitespace-only candidates are dropped. An existing but empty file
//!    yields zero candidates — not an error.
//!
//! Each candidate's `anchor` is `<path>:<line>` (the crate's `path:line`
//! provenance convention, `capsule::Provenance`), where `<line>` is the
//! 1-based line of the candidate's first kept line in the ORIGINAL file
//! (frontmatter offsets included). The `<path>` is written RELATIVE to
//! the caller-injected anchor root (q92) — the SAME boot-injected root
//! the `anchor_live` probe resolves against
//! ([`crate::server::BoundaryConfig::anchor_root`]) — when the source
//! sits under it, so an in-root import composes with the probe: that
//! fence resolves only root-relative paths and reads an absolute anchor
//! as `unknown`. A source outside the root keeps its absolute path and
//! stays fail-closed `unknown`.
//!
//! ## Purity
//!
//! Pure file reading + splitting: NO store writes, NO taint dependency,
//! no clock, no randomness, no environment reads, no network. All ambient
//! input (`base_dir`, `anchor_root`) is injected at the call boundary. Absent sources
//! are a TYPED error ([`BridgeError::SourceMissing`]) — a deliberate
//! deviation from the donor's `"absent"` outcome row (campaign brief):
//! the integrator maps it to whatever outcome shape the import surface
//! reports.
//!
//! ## Integration contract (the taint fence, stated not enforced here)
//!
//! Every capsule built from a [`BridgeCandidate`] MUST be born
//! `authority_class = externally-imported` with `instruction_taint =
//! true`, and its content MUST pass the taint scan BEFORE capsule
//! construction — there must exist no path from a bridge candidate to a
//! stored capsule that skips the scan. `src/ingest.rs` already forces
//! `instruction_taint = true` for the `externally-imported` class (`.2`
//! §4: imports are BORN tainted), so the integrator routes candidates
//! through ingest with that class and runs the scanner (u6e) on
//! `content` first. This module has no taint or store dependency by
//! design, so the fence cannot silently erode here — it is wired, and
//! witnessed, at the integration seam.

use serde::Deserialize;
use std::ffi::OsStr;
use std::fs::File;
use std::io::Read as _;
use std::os::fd::OwnedFd;
use std::path::{Component, Path, PathBuf};

use crate::capsule::sha256_hex;
use rustix::fs::{FileType, Mode, OFlags, fstat, open, openat};

/// Closed set of file-based memory sources this bridge may read.
///
/// Closed enum — never a directory walk (see module docs). Adding a
/// variant is a reviewed change: every `match` in this module and its
/// tests is exhaustive on purpose.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum BridgeSource {
    /// `<base>/.claude3/CLAUDE.md`, else `<base>/.claude2/CLAUDE.md`,
    /// else `<base>/.claude/CLAUDE.md` — first that exists. `base_dir` is
    /// the caller-injected home dir. (Generation-numbered probes are a
    /// recorded drift point — CAMPAIGN.md rung record: extend the chain
    /// when the harness generation moves.)
    UserClaudeMd,
    /// `<base>/CLAUDE.md` — `base_dir` is the project root.
    ProjectClaudeMd,
    /// `<base>/AGENTS.md` — `base_dir` is the project root.
    ProjectAgentsMd,
    /// Every `.md` DIRECTLY inside this one caller-supplied directory
    /// (non-recursive, symlinks skipped). A relative path resolves
    /// against `base_dir`; an absolute path stands alone.
    MemoryDir(PathBuf),
    /// A `notion-pull` export directory: a `manifest.json` naming exported
    /// pages, each page's markdown at `<dir>/<entry.file>`. Every entry is
    /// RE-HASHED at read and checked against its manifest `content_sha256`
    /// (the d27 verifier AT CONSUMPTION) — a mismatch rejects THAT entry
    /// while untampered entries still read. One candidate per PAGE (a page is
    /// the unit of proposal/lineage — never a heading split), anchored at the
    /// page url. Read via [`read_notion_export`] for per-entry reporting; the
    /// [`read_source`] arm is the fail-closed all-or-nothing view.
    NotionExportDir(PathBuf),
}

impl BridgeSource {
    /// Stable kebab-case label naming the source KIND — carried onto
    /// every candidate as [`BridgeCandidate::source_label`].
    #[must_use]
    pub fn source_label(&self) -> &'static str {
        match self {
            BridgeSource::UserClaudeMd => "user-claude-md",
            BridgeSource::ProjectClaudeMd => "project-claude-md",
            BridgeSource::ProjectAgentsMd => "project-agents-md",
            BridgeSource::MemoryDir(_) => "memory-dir",
            BridgeSource::NotionExportDir(_) => "notion-export-dir",
        }
    }
}

/// One capsule-sized piece of a source file (see the module split rule).
///
/// Pure data: nothing here has touched a store or a taint scanner — the
/// integration contract in the module docs governs what a candidate must
/// become before it is ever a capsule.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BridgeCandidate {
    /// The candidate text, trimmed of leading/trailing blank lines.
    pub content: String,
    /// `<resolved-path>:<1-based start line>` in the original file.
    pub anchor: String,
    /// The producing source's [`BridgeSource::source_label`].
    pub source_label: String,
}

/// Typed, fail-closed bridge errors — never a panic on hostile input.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum BridgeError {
    /// No file exists at any of the paths this source resolves to.
    #[error("bridge source '{source_label}' missing: tried {tried:?}")]
    SourceMissing {
        /// Label of the source that resolved to nothing.
        source_label: &'static str,
        /// Every path probed, in probe order.
        tried: Vec<PathBuf>,
    },
    /// The whitelisted LEAF is itself a symlink — never followed (module
    /// docs: a symlink standing in for a whitelisted name redirects the
    /// read outside the closed set).
    #[error("bridge rejected '{0}': whitelisted leaf is a symlink, never followed")]
    SymlinkRejected(PathBuf),
    /// [`BridgeSource::MemoryDir`] resolved to something that exists but
    /// is not a directory.
    #[error("bridge memory dir '{0}' is not a directory")]
    NotADirectory(PathBuf),
    /// A path in the closed set exists but could not be read
    /// (permissions, invalid UTF-8, not a regular file, ...).
    #[error("bridge read failed at '{path}': {message}")]
    Io {
        /// The path the failed operation targeted.
        path: PathBuf,
        /// Stringified cause (`std::io::Error` is not `Clone`/`Eq`).
        message: String,
    },
    /// A Notion export `manifest.json` is malformed or carries an
    /// unsupported version — the whole read fails closed (nothing partial is
    /// trusted out of a manifest that cannot be parsed).
    #[error("notion manifest at '{path}' is malformed: {message}")]
    ManifestInvalid {
        /// The manifest path that failed to parse.
        path: PathBuf,
        /// Stringified cause (serde message or a version rejection).
        message: String,
    },
    /// The d27 verifier AT CONSUMPTION: a Notion page's markdown bytes
    /// re-hash to a value the manifest did not declare — that ONE entry is
    /// rejected (untampered entries in the same manifest still read).
    #[error(
        "notion page '{page_id}' failed the content_sha256 verifier: \
         file '{file}' does not match manifest declaration '{expected}'"
    )]
    ContentHashMismatch {
        /// The manifest `page_id` of the rejected entry.
        page_id: String,
        /// The manifest-relative content path that was re-hashed.
        file: String,
        /// The producer-declared digest; the locally computed digest is never
        /// retained because exposing it would create a local-file hash oracle.
        expected: String,
    },
}

/// Read one closed source rooted at `base_dir` and split it into
/// capsule-sized candidates per the module split rule.
///
/// Pure: no store writes, no taint dependency, no ambient input beyond
/// the injected `base_dir` and `anchor_root` (the boot-injected root
/// anchors render RELATIVE to when the source sits under it — q92,
/// module doc). Candidate order is deterministic: file order
/// (probe order / sorted names for [`BridgeSource::MemoryDir`]), then
/// document order within each file.
pub fn read_source(
    source: &BridgeSource,
    base_dir: &Path,
    anchor_root: &Path,
) -> Result<Vec<BridgeCandidate>, BridgeError> {
    let label = source.source_label();
    match source {
        BridgeSource::UserClaudeMd => {
            let tried = vec![
                base_dir.join(".claude3").join("CLAUDE.md"),
                base_dir.join(".claude2").join("CLAUDE.md"),
                base_dir.join(".claude").join("CLAUDE.md"),
            ];
            for path in &tried {
                if let Some(content) = read_regular_file(path)? {
                    return Ok(candidates_from(&content, path, label, anchor_root));
                }
            }
            Err(BridgeError::SourceMissing {
                source_label: label,
                tried,
            })
        }
        BridgeSource::ProjectClaudeMd => {
            read_single(base_dir.join("CLAUDE.md"), label, anchor_root)
        }
        BridgeSource::ProjectAgentsMd => {
            read_single(base_dir.join("AGENTS.md"), label, anchor_root)
        }
        BridgeSource::MemoryDir(dir) => read_memory_dir(&base_dir.join(dir), label, anchor_root),
        BridgeSource::NotionExportDir(dir) => {
            // `read_source`'s Result contract is all-or-nothing, so this is
            // the fail-closed view: the FIRST tampered/unreadable entry sinks
            // the whole read. The import handler drives Notion through
            // [`read_notion_export`] directly for PER-ENTRY reporting (a
            // tampered page rejected while the rest import). `anchor_root` is
            // unused here — a page anchor is a url, never a `path:line`, so it
            // reads `anchor_live: unknown` regardless of the root.
            let mut out = Vec::new();
            for entry in read_notion_export(&base_dir.join(dir))? {
                let candidate = entry?;
                out.push(BridgeCandidate {
                    content: candidate.content,
                    anchor: candidate.url,
                    source_label: label.to_string(),
                });
            }
            Ok(out)
        }
    }
}

/// Read exactly one fixed path; absent is the typed
/// [`BridgeError::SourceMissing`].
fn read_single(
    path: PathBuf,
    label: &'static str,
    anchor_root: &Path,
) -> Result<Vec<BridgeCandidate>, BridgeError> {
    match read_regular_file(&path)? {
        Some(content) => Ok(candidates_from(&content, &path, label, anchor_root)),
        None => Err(BridgeError::SourceMissing {
            source_label: label,
            tried: vec![path],
        }),
    }
}

/// The ONE directory listing in this module (structurally proven single in
/// tests): a non-recursive scan of `dir` for regular `.md` files, sorted
/// by name. Subdirectories are never entered; symlinked entries are never
/// followed; the extension match is case-sensitive (`.MD` is skipped).
fn read_memory_dir(
    dir: &Path,
    label: &'static str,
    anchor_root: &Path,
) -> Result<Vec<BridgeCandidate>, BridgeError> {
    let entries = match std::fs::read_dir(dir) {
        Ok(entries) => entries,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            return Err(BridgeError::SourceMissing {
                source_label: label,
                tried: vec![dir.to_path_buf()],
            });
        }
        Err(e) if e.kind() == std::io::ErrorKind::NotADirectory => {
            return Err(BridgeError::NotADirectory(dir.to_path_buf()));
        }
        Err(e) => {
            return Err(BridgeError::Io {
                path: dir.to_path_buf(),
                message: e.to_string(),
            });
        }
    };

    let mut files: Vec<(std::ffi::OsString, PathBuf)> = Vec::new();
    for entry in entries {
        let entry = entry.map_err(|e| BridgeError::Io {
            path: dir.to_path_buf(),
            message: e.to_string(),
        })?;
        let file_type = entry.file_type().map_err(|e| BridgeError::Io {
            path: entry.path(),
            message: e.to_string(),
        })?;
        // Never descend, never follow: anything that is not a plain
        // regular file (subdirectory, symlink, fifo, ...) is skipped.
        if !file_type.is_file() {
            continue;
        }
        let path = entry.path();
        if path.extension() != Some(OsStr::new("md")) {
            continue;
        }
        files.push((entry.file_name(), path));
    }
    // OS listing order is arbitrary — sort by name for determinism.
    files.sort_by(|a, b| a.0.cmp(&b.0));

    let mut out = Vec::new();
    for (_, path) in &files {
        // A file vanishing between listing and read degrades to "skip",
        // matching the listing's opportunistic nature; symlink/read
        // failures stay typed errors.
        if let Some(content) = read_regular_file(path)? {
            out.extend(candidates_from(&content, path, label, anchor_root));
        }
    }
    Ok(out)
}

/// Probe-and-read one leaf path. `Ok(None)` = absent (the caller decides
/// whether that is an error). The leaf itself must be a regular file:
/// a leaf symlink is rejected without being followed
/// (`symlink_metadata`, which — unlike `metadata` — does not resolve the
/// final component), while symlinked parent directories are transparently
/// followed (dotfiles pattern; donor r3 lesson).
fn read_regular_file(path: &Path) -> Result<Option<String>, BridgeError> {
    match read_regular_file_bytes(path)? {
        // The one UTF-8 decode boundary: invalid bytes are a typed Io error
        // (never lossy), the same "fail-closed, never a panic" answer the
        // old `read_to_string` gave.
        Some(bytes) => match String::from_utf8(bytes) {
            Ok(content) => Ok(Some(content)),
            Err(e) => Err(BridgeError::Io {
                path: path.to_path_buf(),
                message: e.to_string(),
            }),
        },
        None => Ok(None),
    }
}

/// Probe-and-read one leaf path as RAW BYTES — the shared symlink-refusing,
/// regular-file-only read under [`read_regular_file`] (UTF-8 text) and the
/// Notion content-file re-hash (which must hash the EXACT bytes, before any
/// UTF-8 decode). `Ok(None)` = absent; a leaf symlink is rejected without
/// being followed; a non-regular leaf is a typed Io error.
fn read_regular_file_bytes(path: &Path) -> Result<Option<Vec<u8>>, BridgeError> {
    let metadata = match std::fs::symlink_metadata(path) {
        Ok(m) => m,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(e) => {
            return Err(BridgeError::Io {
                path: path.to_path_buf(),
                message: e.to_string(),
            });
        }
    };
    if metadata.file_type().is_symlink() {
        return Err(BridgeError::SymlinkRejected(path.to_path_buf()));
    }
    if !metadata.is_file() {
        return Err(BridgeError::Io {
            path: path.to_path_buf(),
            message: "not a regular file".to_string(),
        });
    }
    match std::fs::read(path) {
        Ok(bytes) => Ok(Some(bytes)),
        // Vanished between probe and read (TOCTOU window, kept narrow):
        // same "absent" answer the probe would have given.
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(e) => Err(BridgeError::Io {
            path: path.to_path_buf(),
            message: e.to_string(),
        }),
    }
}

/// The only Notion export `manifest.json` version this reader understands.
/// A newer manifest fails closed ([`BridgeError::ManifestInvalid`]) rather
/// than being read on optimistic assumptions.
const NOTION_MANIFEST_VERSION: u32 = 1;

/// The `manifest.json` a `notion-pull` export directory carries. Only the
/// fields this consumer reads are typed; unknown fields (e.g. `generated_at`)
/// are ignored so a richer producer manifest stays forward-compatible.
#[derive(Debug, Clone, Deserialize)]
struct NotionManifest {
    /// Manifest format version — only [`NOTION_MANIFEST_VERSION`] is read.
    version: u32,
    /// One row per exported page, in the order the import proposes them.
    entries: Vec<NotionManifestEntry>,
}

/// One page row of a Notion export manifest.
#[derive(Debug, Clone, Deserialize)]
struct NotionManifestEntry {
    /// The Notion page id — the stable per-page identity carried into
    /// provenance (`notion:<page_id>`) and the import-block source_key.
    page_id: String,
    /// The page url — the capsule anchor (a non-path anchor ⇒ the
    /// `anchor_live` probe reads `unknown`, honestly).
    url: String,
    /// Hex sha256 the producer computed over the EXACT markdown bytes — the
    /// value this reader re-derives and checks (the d27 consumption verifier).
    content_sha256: String,
    /// The page markdown, RELATIVE to the export dir (e.g. `pages/<id>.md`).
    file: String,
}

/// One Notion page read from an export directory: the exact markdown as a
/// UTF-8 string plus the page identity (`page_id`) and its `url` (the capsule
/// anchor). Produced in manifest order.
///
/// Pure data — like [`BridgeCandidate`], nothing here has touched a store or
/// a taint scanner; the integration contract governs what it must become
/// before it is ever a capsule (born externally-imported + tainted, and —
/// for this EXTERNAL source — STAGED as a proposal).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct NotionCandidate {
    /// The page's exact markdown bytes, decoded UTF-8.
    pub content: String,
    /// The Notion page id (`<page_id>` of `notion:<page_id>`).
    pub page_id: String,
    /// The page url — carried onto the capsule as its anchor.
    pub url: String,
}

/// Read a `notion-pull` export directory: parse `<dir>/manifest.json`, then
/// for EACH entry (in manifest order) re-hash `<dir>/<entry.file>` and check
/// it against `entry.content_sha256`. An untampered entry yields
/// `Ok(NotionCandidate)`; a tampered or unreadable entry yields a typed
/// `Err` naming the fault — so ONE bad page never sinks the rest (the caller
/// reports it per entry). A manifest-level fault (absent / malformed /
/// unsupported version) fails the whole read closed.
///
/// Purity mirrors the rest of this module: no clock, env, randomness, or
/// network — the export dir path is injected at the boundary. This is the
/// d27 verifier AT CONSUMPTION: corroboration travels with the bytes, not
/// with trust in the channel that produced them.
pub fn read_notion_export(
    dir: &Path,
) -> Result<Vec<Result<NotionCandidate, BridgeError>>, BridgeError> {
    let manifest_path = dir.join("manifest.json");
    // Pin the export root once. Every manifest path is opened relative to
    // this descriptor, component by component, so a pathname swap or an
    // intermediate symlink cannot redirect the subsequent read.
    let root = open_export_root(dir, &manifest_path)?;
    let raw = match read_export_file(&root, Path::new("manifest.json"), &manifest_path)? {
        Some(bytes) => String::from_utf8(bytes).map_err(|e| BridgeError::ManifestInvalid {
            path: manifest_path.clone(),
            message: format!("manifest is not valid UTF-8: {e}"),
        })?,
        None => {
            return Err(BridgeError::SourceMissing {
                source_label: "notion-export-dir",
                tried: vec![manifest_path],
            });
        }
    };
    let manifest: NotionManifest =
        serde_json::from_str(&raw).map_err(|e| BridgeError::ManifestInvalid {
            path: manifest_path.clone(),
            message: e.to_string(),
        })?;
    if manifest.version != NOTION_MANIFEST_VERSION {
        return Err(BridgeError::ManifestInvalid {
            path: manifest_path,
            message: format!(
                "unsupported manifest version {} (this reader understands {NOTION_MANIFEST_VERSION})",
                manifest.version
            ),
        });
    }
    Ok(manifest
        .entries
        .into_iter()
        .map(|entry| read_notion_entry(&root, dir, entry))
        .collect())
}

/// Read and verify ONE Notion manifest entry. The `file` comes from the
/// manifest (data, never trusted): it must be a plain relative path made only
/// of normal components, so a manifest can never redirect the read outside
/// the export dir (the module's closed-read discipline). The content bytes
/// are re-hashed BEFORE any UTF-8 decode — the manifest hash is over exact
/// bytes — and only then decoded for the capsule content.
fn read_notion_entry(
    root: &OwnedFd,
    dir: &Path,
    entry: NotionManifestEntry,
) -> Result<NotionCandidate, BridgeError> {
    let rel = Path::new(&entry.file);
    let inside_dir =
        !entry.file.is_empty() && rel.components().all(|c| matches!(c, Component::Normal(_)));
    if !inside_dir {
        return Err(BridgeError::ManifestInvalid {
            path: dir.join(&entry.file),
            message: format!(
                "entry file '{}' is not a plain relative path inside the export dir",
                entry.file
            ),
        });
    }
    let content_path = dir.join(rel);
    let bytes = match read_export_file(root, rel, &content_path)? {
        Some(bytes) => bytes,
        None => {
            return Err(BridgeError::Io {
                path: content_path,
                message: "notion export content file is missing".to_string(),
            });
        }
    };
    // The d27 verifier: hash the EXACT bytes and compare, before decoding.
    let actual = sha256_hex(&bytes);
    if actual != entry.content_sha256 {
        return Err(BridgeError::ContentHashMismatch {
            page_id: entry.page_id,
            file: entry.file,
            expected: entry.content_sha256,
        });
    }
    let content = String::from_utf8(bytes).map_err(|e| BridgeError::Io {
        path: content_path,
        message: format!("notion export content is not valid UTF-8: {e}"),
    })?;
    Ok(NotionCandidate {
        content,
        page_id: entry.page_id,
        url: entry.url,
    })
}

/// Open and pin the export directory itself. The leaf may not be a symlink;
/// every entry walk remains relative to this descriptor.
fn open_export_root(dir: &Path, manifest_path: &Path) -> Result<OwnedFd, BridgeError> {
    match open(
        dir,
        OFlags::RDONLY | OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::CLOEXEC,
        Mode::empty(),
    ) {
        Ok(fd) => Ok(fd),
        Err(error) if error == rustix::io::Errno::NOENT => Err(BridgeError::SourceMissing {
            source_label: "notion-export-dir",
            tried: vec![manifest_path.to_path_buf()],
        }),
        Err(error) if error == rustix::io::Errno::LOOP => {
            Err(BridgeError::SymlinkRejected(dir.to_path_buf()))
        }
        Err(error) if error == rustix::io::Errno::NOTDIR => {
            Err(BridgeError::NotADirectory(dir.to_path_buf()))
        }
        Err(error) => Err(BridgeError::Io {
            path: dir.to_path_buf(),
            message: error.to_string(),
        }),
    }
}

/// Read one plain relative file below an already-open export root. Every
/// intermediate component is opened as `DIRECTORY|NOFOLLOW`; the final
/// component is opened `NOFOLLOW`, proven regular with `fstat`, then read
/// from that same descriptor. No check-then-reopen window exists.
fn read_export_file(
    root: &OwnedFd,
    relative: &Path,
    display_path: &Path,
) -> Result<Option<Vec<u8>>, BridgeError> {
    let mut components = Vec::new();
    for component in relative.components() {
        match component {
            Component::Normal(name) => components.push(name),
            _ => {
                return Err(BridgeError::Io {
                    path: display_path.to_path_buf(),
                    message: "export file path is not plain relative".to_string(),
                });
            }
        }
    }
    if components.is_empty() {
        return Err(BridgeError::Io {
            path: display_path.to_path_buf(),
            message: "empty export file path".to_string(),
        });
    }

    let mut current: Option<OwnedFd> = None;
    for (index, component) in components.iter().enumerate() {
        let final_component = index + 1 == components.len();
        let flags = if final_component {
            OFlags::RDONLY | OFlags::NONBLOCK | OFlags::NOFOLLOW | OFlags::CLOEXEC
        } else {
            OFlags::RDONLY | OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::CLOEXEC
        };
        let opened = match current.as_ref() {
            Some(directory) => openat(directory, *component, flags, Mode::empty()),
            None => openat(root, *component, flags, Mode::empty()),
        };
        match opened {
            Ok(fd) => current = Some(fd),
            Err(error) if error == rustix::io::Errno::NOENT => return Ok(None),
            Err(error) if error == rustix::io::Errno::LOOP => {
                return Err(BridgeError::SymlinkRejected(display_path.to_path_buf()));
            }
            Err(error) => {
                return Err(BridgeError::Io {
                    path: display_path.to_path_buf(),
                    message: format!("descriptor-relative open rejected the path: {error}"),
                });
            }
        }
    }

    let descriptor = current.ok_or_else(|| BridgeError::Io {
        path: display_path.to_path_buf(),
        message: "export file descriptor was not opened".to_string(),
    })?;
    let stat = fstat(&descriptor).map_err(|error| BridgeError::Io {
        path: display_path.to_path_buf(),
        message: format!("cannot inspect opened export file: {error}"),
    })?;
    if FileType::from_raw_mode(stat.st_mode) != FileType::RegularFile {
        return Err(BridgeError::Io {
            path: display_path.to_path_buf(),
            message: "not a regular file".to_string(),
        });
    }

    let mut bytes = Vec::new();
    File::from(descriptor)
        .read_to_end(&mut bytes)
        .map_err(|error| BridgeError::Io {
            path: display_path.to_path_buf(),
            message: error.to_string(),
        })?;
    Ok(Some(bytes))
}

/// Split one file's content and wrap each piece as a [`BridgeCandidate`]
/// anchored `<path>:<start-line>` (root-relative when in-root — see
/// [`anchor_path`]).
fn candidates_from(
    content: &str,
    path: &Path,
    label: &'static str,
    anchor_root: &Path,
) -> Vec<BridgeCandidate> {
    let anchor_base = anchor_path(path, anchor_root);
    split_document(content)
        .into_iter()
        .map(|(start_line, text)| BridgeCandidate {
            content: text,
            anchor: format!("{}:{start_line}", anchor_base.display()),
            source_label: label.to_string(),
        })
        .collect()
}

/// q92: render the anchor path RELATIVE to the caller-injected
/// `anchor_root` when the source file sits under it, so an in-root
/// import composes with the `anchor_live` probe — that fence
/// (`crate::retrieve`) reads an ABSOLUTE `path:line` anchor as
/// `unknown` forever, so an absolute anchor could never answer live
/// even with the file on disk. A source OUTSIDE the root keeps its
/// absolute path and stays fail-closed `unknown` — the probe never
/// over-claims liveness for a path it does not resolve.
fn anchor_path<'a>(path: &'a Path, anchor_root: &Path) -> &'a Path {
    path.strip_prefix(anchor_root).unwrap_or(path)
}

/// True for a fence marker line (` ``` ` or `~~~`, any indentation).
fn is_fence_marker(line: &str) -> bool {
    let t = line.trim_start();
    t.starts_with("```") || t.starts_with("~~~")
}

/// ATX heading level at column 0 (1–6 `#` then whitespace or EOL), else
/// `None`. Callers must not pass fenced lines.
fn heading_level(line: &str) -> Option<usize> {
    let hashes = line.bytes().take_while(|&b| b == b'#').count();
    if hashes == 0 || hashes > 6 {
        return None;
    }
    match line.as_bytes().get(hashes).copied() {
        None | Some(b' ') | Some(b'\t') => Some(hashes),
        Some(_) => None,
    }
}

/// Index of the first body line, skipping one CLOSED YAML frontmatter
/// block (first line `---`, closing `---`/`...`). Unclosed = no
/// frontmatter (the opener is plain content).
fn body_start(lines: &[&str]) -> usize {
    let Some(first) = lines.first() else { return 0 };
    if first.trim_end() != "---" {
        return 0;
    }
    for (i, line) in lines.iter().enumerate().skip(1) {
        let t = line.trim_end();
        if t == "---" || t == "..." {
            return i + 1;
        }
    }
    0
}

/// The module split rule (documented at module level, tested below):
/// returns `(start_line, candidate_text)` pairs, `start_line` 1-based in
/// the original content. Pure and deterministic.
fn split_document(content: &str) -> Vec<(usize, String)> {
    let content = content.strip_prefix('\u{feff}').unwrap_or(content);
    let lines: Vec<&str> = content.lines().collect();
    let start = body_start(&lines);

    // Annotate body lines with (1-based number, text, inside-fence).
    let mut body: Vec<(usize, &str, bool)> = Vec::new();
    let mut in_fence = false;
    for (idx, &text) in lines.iter().enumerate().skip(start) {
        if in_fence {
            body.push((idx + 1, text, true));
            if is_fence_marker(text) {
                in_fence = false;
            }
        } else if is_fence_marker(text) {
            body.push((idx + 1, text, true));
            in_fence = true;
        } else {
            body.push((idx + 1, text, false));
        }
    }

    // Headings outside fences, as (body index, level).
    let headings: Vec<(usize, usize)> = body
        .iter()
        .enumerate()
        .filter_map(|(i, &(_, text, fenced))| {
            if fenced {
                None
            } else {
                heading_level(text).map(|level| (i, level))
            }
        })
        .collect();

    // Split level: smallest level occurring at least twice, else the
    // smallest present (see rule 4 in the module docs).
    let mut counts = [0usize; 6];
    for &(_, level) in &headings {
        counts[level - 1] += 1;
    }
    let split_level = counts
        .iter()
        .position(|&c| c >= 2)
        .or_else(|| counts.iter().position(|&c| c >= 1))
        .map(|i| i + 1);

    // Regions as [start, end) index ranges into `body`.
    let mut regions: Vec<(usize, usize)> = Vec::new();
    match split_level {
        Some(level) => {
            let mut starts = vec![0usize];
            for &(i, l) in &headings {
                if l == level && i != 0 {
                    starts.push(i);
                }
            }
            starts.push(body.len());
            regions.extend(starts.windows(2).map(|w| (w[0], w[1])));
        }
        None => {
            // Paragraph blocks: blank lines OUTSIDE fences separate.
            let mut i = 0;
            while i < body.len() {
                let (_, text, fenced) = body[i];
                if !fenced && text.trim().is_empty() {
                    i += 1;
                    continue;
                }
                let block_start = i;
                while i < body.len() {
                    let (_, text, fenced) = body[i];
                    if !fenced && text.trim().is_empty() {
                        break;
                    }
                    i += 1;
                }
                regions.push((block_start, i));
            }
        }
    }

    // Materialize: trim leading/trailing blank lines, drop empties.
    let mut out = Vec::new();
    for (region_start, region_end) in regions {
        let slice = &body[region_start..region_end];
        let Some(first) = slice.iter().position(|&(_, t, _)| !t.trim().is_empty()) else {
            continue;
        };
        let Some(last) = slice.iter().rposition(|&(_, t, _)| !t.trim().is_empty()) else {
            continue;
        };
        let kept = &slice[first..=last];
        let start_line = kept[0].0;
        let text = kept
            .iter()
            .map(|&(_, t, _)| t)
            .collect::<Vec<_>>()
            .join("\n");
        out.push((start_line, text));
    }
    out
}

#[cfg(test)]
mod tests {
    #![allow(
        clippy::unwrap_used,
        clippy::expect_used,
        reason = "tests use unwrap/expect so fixture failures fail at the assertion site"
    )]

    use super::*;

    // ---- split rule (pure) ----

    #[test]
    fn frontmatter_is_skipped_and_line_numbers_stay_original() {
        let doc = "---\ntitle: cfg\n---\n\nFirst block.\n\nSecond block.\n";
        let got = split_document(doc);
        assert_eq!(
            got,
            vec![
                (5, "First block.".to_string()),
                (7, "Second block.".to_string()),
            ]
        );
    }

    #[test]
    fn unclosed_frontmatter_opener_is_plain_content() {
        let doc = "---\nnot frontmatter, never closed";
        let got = split_document(doc);
        // The opener is a plain line; paragraph mode keeps both lines.
        assert_eq!(got, vec![(1, "---\nnot frontmatter, never closed".into())]);
    }

    #[test]
    fn in_root_anchor_is_root_relative_out_of_root_stays_absolute() {
        // q92: a source UNDER the injected anchor root gets a
        // ROOT-RELATIVE anchor so it composes with retrieve's
        // anchor_live probe (an ABSOLUTE anchor reads "unknown" there
        // forever); a source OUTSIDE the root keeps its absolute path
        // and stays fail-closed "unknown". Host-independent: synthetic
        // paths against a synthetic injected root, no filesystem read.
        let root = Path::new("/injected-anchor-root");
        let in_root = root.join("capabilities/nmemory/PLAN.md");
        let got = candidates_from("only block", &in_root, "project-claude-md", root);
        assert_eq!(got.len(), 1);
        assert_eq!(got[0].anchor, "capabilities/nmemory/PLAN.md:1");

        let out_of_root = Path::new("/etc/some-notes.md");
        let got = candidates_from("only block", out_of_root, "project-claude-md", root);
        assert_eq!(got[0].anchor, "/etc/some-notes.md:1");
    }

    #[test]
    fn heading_documents_split_per_top_level_section_with_preamble() {
        let doc = "intro before any heading\n\n# One\nbody one\n\n# Two\nbody two\n";
        let got = split_document(doc);
        assert_eq!(
            got,
            vec![
                (1, "intro before any heading".to_string()),
                (3, "# One\nbody one".to_string()),
                (6, "# Two\nbody two".to_string()),
            ]
        );
    }

    #[test]
    fn lone_h1_title_splits_at_the_first_repeated_deeper_level() {
        let doc = "# Title\n\nintro\n\n## A\na body\n\n## B\nb body\n";
        let got = split_document(doc);
        assert_eq!(
            got,
            vec![
                (1, "# Title\n\nintro".to_string()),
                (5, "## A\na body".to_string()),
                (8, "## B\nb body".to_string()),
            ]
        );
    }

    #[test]
    fn single_heading_document_is_one_candidate() {
        let doc = "# Memory index\n\n- [a](a.md)\n- [b](b.md)\n";
        let got = split_document(doc);
        assert_eq!(
            got,
            vec![(1, "# Memory index\n\n- [a](a.md)\n- [b](b.md)".to_string())]
        );
    }

    #[test]
    fn fenced_hash_lines_are_not_headings_and_fenced_blanks_do_not_split() {
        // No real headings -> paragraph mode; the fence rides whole.
        let doc = "```bash\n# fenced comment\n\nmake build\n```\ntail line\n";
        let got = split_document(doc);
        assert_eq!(
            got,
            vec![(
                1,
                "```bash\n# fenced comment\n\nmake build\n```\ntail line".to_string()
            )]
        );
    }

    #[test]
    fn fenced_hash_lines_never_open_a_section_in_heading_mode() {
        let doc = "# Real\n\n```\n# fake heading\n```\n\n# Also real\nbody\n";
        let got = split_document(doc);
        assert_eq!(
            got,
            vec![
                (1, "# Real\n\n```\n# fake heading\n```".to_string()),
                (7, "# Also real\nbody".to_string()),
            ]
        );
    }

    #[test]
    fn plain_paragraph_blocks_split_on_blank_lines() {
        let doc = "first spans\ntwo lines\n\n\nsecond block\n";
        let got = split_document(doc);
        assert_eq!(
            got,
            vec![
                (1, "first spans\ntwo lines".to_string()),
                (5, "second block".to_string()),
            ]
        );
    }

    #[test]
    fn empty_and_whitespace_only_content_yield_zero_candidates() {
        assert!(split_document("").is_empty());
        assert!(split_document("   \n\n\t\n").is_empty());
        // Frontmatter-only file: nothing left after the skip.
        assert!(split_document("---\nx: y\n---\n").is_empty());
    }

    #[test]
    fn bom_is_stripped_before_splitting() {
        let doc = "\u{feff}# H\nbody\n";
        let got = split_document(doc);
        assert_eq!(got, vec![(1, "# H\nbody".to_string())]);
    }

    #[test]
    fn heading_detection_requires_column_zero_and_a_space() {
        assert_eq!(heading_level("# ok"), Some(1));
        assert_eq!(heading_level("###### deep"), Some(6));
        assert_eq!(heading_level("#"), Some(1));
        assert_eq!(heading_level("#nope"), None);
        assert_eq!(heading_level("  # indented"), None);
        assert_eq!(heading_level("####### seven"), None);
    }

    // ---- structural proof: exactly ONE listing call site ----

    #[test]
    fn bridge_has_exactly_one_directory_listing_and_no_walker() {
        // Needles built by concatenation so this test's own text never
        // trips the assertion it makes (donor pattern, adapted: the one
        // sanctioned non-recursive listing is counted, walkers are
        // banned outright).
        let source = include_str!("bridge.rs");
        let listing = format!("{}{}", "read_", "dir");
        assert_eq!(
            source.matches(listing.as_str()).count(),
            1,
            "exactly one directory-listing call site is sanctioned (MemoryDir)"
        );
        let walkers = [
            format!("{}{}", "walk", "dir"),
            format!("{}{}", "Walk", "Dir"),
            format!("{}{}", "glob", "("),
        ];
        for needle in &walkers {
            assert!(
                !source.contains(needle.as_str()),
                "bridge source must never contain a directory walker"
            );
        }
    }

    // ---- read_source error arms (typed, fail-closed) ----

    // A MemoryDir whose listing fails for a reason that is NEITHER
    // NotFound NOR NotADirectory — a symlink loop makes the directory
    // listing return FilesystemLoop (ELOOP) — surfaces as the typed
    // BridgeError::Io, never a panic. Root-proof: a symlink loop is not
    // bypassed by privilege, so this arm is provable under any uid.
    // (Comment avoids the literal listing-call token so the single-
    // listing structural test above still counts exactly one call site.)
    #[cfg(unix)]
    #[test]
    fn memory_dir_listing_error_other_than_missing_is_typed_io() {
        let tmp = tempfile::tempdir().unwrap();
        let base = tmp.path();
        std::os::unix::fs::symlink(base.join("loop_b"), base.join("loop_a")).unwrap();
        std::os::unix::fs::symlink(base.join("loop_a"), base.join("loop_b")).unwrap();

        let err = read_source(
            &BridgeSource::MemoryDir(PathBuf::from("loop_a")),
            base,
            base,
        )
        .expect_err("a symlink-loop dir must fail closed");
        assert!(
            matches!(err, BridgeError::Io { .. }),
            "a non-missing listing failure is typed Io, got: {err:?}"
        );
    }

    // A fixed source whose PARENT component is a regular file makes
    // symlink_metadata fail with ENOTDIR (NotADirectory, not NotFound):
    // the typed BridgeError::Io, never a panic. base_dir is itself a file,
    // so <base>/CLAUDE.md traverses through a non-directory.
    #[cfg(unix)]
    #[test]
    fn fixed_source_with_non_directory_parent_is_typed_io() {
        let tmp = tempfile::tempdir().unwrap();
        let not_a_dir = tmp.path().join("i-am-a-file");
        std::fs::write(&not_a_dir, b"x").unwrap();
        let err = read_source(&BridgeSource::ProjectClaudeMd, &not_a_dir, tmp.path())
            .expect_err("a non-directory parent must fail closed");
        assert!(
            matches!(err, BridgeError::Io { .. }),
            "ENOTDIR on the leaf path is typed Io, got: {err:?}"
        );
    }

    // A whitelisted leaf that EXISTS, is not a symlink, but is not a
    // regular file (it is a directory) is rejected as Io "not a regular
    // file" — never read, never a panic.
    #[test]
    fn fixed_source_that_is_a_directory_is_rejected() {
        let tmp = tempfile::tempdir().unwrap();
        std::fs::create_dir(tmp.path().join("CLAUDE.md")).unwrap();
        let err = read_source(&BridgeSource::ProjectClaudeMd, tmp.path(), tmp.path())
            .expect_err("a directory standing in for a file must fail closed");
        assert!(
            matches!(&err, BridgeError::Io { message, .. } if message.contains("not a regular file")),
            "expected Io 'not a regular file', got: {err:?}"
        );
    }

    // A whitelisted leaf that is a regular file but not valid UTF-8 makes
    // read_to_string fail with InvalidData (not NotFound): the typed
    // BridgeError::Io, never a panic and never lossy bytes.
    #[test]
    fn fixed_source_with_invalid_utf8_is_typed_io() {
        let tmp = tempfile::tempdir().unwrap();
        std::fs::write(tmp.path().join("AGENTS.md"), [0xff, 0xfe, 0xfd]).unwrap();
        let err = read_source(&BridgeSource::ProjectAgentsMd, tmp.path(), tmp.path())
            .expect_err("invalid UTF-8 must fail closed");
        assert!(
            matches!(err, BridgeError::Io { .. }),
            "invalid UTF-8 is typed Io, got: {err:?}"
        );
    }

    // Rule 6 materialize step: a split region that is ENTIRELY blank — the
    // blank preamble before the first heading — is dropped, so no empty
    // candidate materializes; the heading section still anchors at its
    // original 1-based line number.
    #[test]
    fn all_blank_preamble_region_is_dropped() {
        let doc = "\n\n# H\nbody\n";
        let got = split_document(doc);
        assert_eq!(got, vec![(3, "# H\nbody".to_string())]);
    }

    // ---- S5b: notion export reader (the d27 verifier at consumption) ----

    /// Build a synthetic notion-pull export under `dir`: a `manifest.json`
    /// plus one `pages/<page_id>.md` per page, each entry's `content_sha256`
    /// the REAL hash of the bytes written (so untampered pages verify). The
    /// manifest carries the producer's extra fields (`title`,
    /// `last_edited_time`, `generated_at`) to prove the reader ignores them.
    /// (d15: synthetic only — never a real store or real Notion.)
    fn write_export(dir: &Path, pages: &[(&str, &str, &str)]) {
        std::fs::create_dir_all(dir.join("pages")).unwrap();
        let entries: Vec<_> = pages
            .iter()
            .map(|(page_id, url, markdown)| {
                let file = format!("pages/{page_id}.md");
                std::fs::write(dir.join(&file), markdown.as_bytes()).unwrap();
                serde_json::json!({
                    "page_id": page_id,
                    "title": format!("Title {page_id}"),
                    "url": url,
                    "last_edited_time": "2026-07-23T00:00:00Z",
                    "content_sha256": sha256_hex(markdown.as_bytes()),
                    "file": file,
                })
            })
            .collect();
        let manifest = serde_json::json!({
            "version": 1,
            "generated_at": "2026-07-23T00:00:00Z",
            "entries": entries,
        });
        std::fs::write(
            dir.join("manifest.json"),
            serde_json::to_vec_pretty(&manifest).unwrap(),
        )
        .unwrap();
    }

    #[test]
    fn notion_export_reads_untampered_pages_in_manifest_order() {
        let tmp = tempfile::tempdir().unwrap();
        let dir = tmp.path().join("export");
        write_export(
            &dir,
            &[
                ("page-a", "https://notion.so/page-a", "# A\nalpha body\n"),
                ("page-b", "https://notion.so/page-b", "# B\nbeta body\n"),
            ],
        );
        let got = read_notion_export(&dir).unwrap();
        assert_eq!(got.len(), 2, "one candidate per page, manifest order");
        let a = got[0].as_ref().unwrap();
        assert_eq!(a.page_id, "page-a");
        assert_eq!(a.url, "https://notion.so/page-a");
        assert_eq!(a.content, "# A\nalpha body\n", "exact bytes, whole page");
        assert_eq!(got[1].as_ref().unwrap().page_id, "page-b");
    }

    #[test]
    fn notion_export_tampered_entry_rejected_naming_mismatch_others_read() {
        let tmp = tempfile::tempdir().unwrap();
        let dir = tmp.path().join("export");
        write_export(
            &dir,
            &[
                ("page-a", "https://notion.so/page-a", "alpha stays honest\n"),
                ("page-b", "https://notion.so/page-b", "beta original\n"),
            ],
        );
        // Flip the bytes of page-b WITHOUT re-stamping the manifest hash — the
        // consumption verifier must reject exactly that entry.
        let expected_hash = sha256_hex(b"beta original\n");
        let tampered = "beta TAMPERED\n";
        let tampered_hash = sha256_hex(tampered.as_bytes());
        std::fs::write(dir.join("pages/page-b.md"), tampered).unwrap();
        let got = read_notion_export(&dir).unwrap();
        assert_eq!(got.len(), 2);
        assert!(got[0].is_ok(), "untampered page-a still reads");
        let err = got[1].as_ref().unwrap_err();
        match err {
            BridgeError::ContentHashMismatch { page_id, .. } => assert_eq!(page_id, "page-b"),
            other => panic!("expected ContentHashMismatch, got {other:?}"),
        }
        let msg = err.to_string();
        assert!(msg.contains("content_sha256 verifier"), "{msg}");
        assert!(msg.contains("page-b"), "names the page: {msg}");
        assert!(
            msg.contains(&expected_hash),
            "the manifest-declared hash remains useful diagnostic context: {msg}"
        );
        assert!(
            !msg.contains(&tampered_hash),
            "the actual file hash is a local-file oracle and must stay private: {msg}"
        );
        let debug = format!("{err:?}");
        assert!(
            debug.contains(&expected_hash),
            "the typed error retains the manifest-declared hash: {debug}"
        );
        assert!(
            !debug.contains(&tampered_hash),
            "the typed error must not retain the actual local-file hash: {debug}"
        );
    }

    #[test]
    fn notion_export_missing_manifest_is_source_missing() {
        let tmp = tempfile::tempdir().unwrap();
        let dir = tmp.path().join("export");
        std::fs::create_dir_all(&dir).unwrap();
        let err = read_notion_export(&dir).unwrap_err();
        assert!(matches!(err, BridgeError::SourceMissing { .. }), "{err:?}");
    }

    #[test]
    fn notion_export_malformed_manifest_is_typed_manifest_invalid() {
        let tmp = tempfile::tempdir().unwrap();
        let dir = tmp.path().join("export");
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(dir.join("manifest.json"), b"{ not json ]").unwrap();
        let err = read_notion_export(&dir).unwrap_err();
        assert!(
            matches!(err, BridgeError::ManifestInvalid { .. }),
            "{err:?}"
        );
    }

    #[test]
    fn notion_export_unsupported_version_fails_closed() {
        let tmp = tempfile::tempdir().unwrap();
        let dir = tmp.path().join("export");
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(
            dir.join("manifest.json"),
            serde_json::to_vec(&serde_json::json!({"version": 2, "entries": []})).unwrap(),
        )
        .unwrap();
        let err = read_notion_export(&dir).unwrap_err();
        assert!(
            matches!(&err, BridgeError::ManifestInvalid { message, .. } if message.contains("version")),
            "{err:?}"
        );
    }

    #[test]
    fn notion_export_rejects_a_content_path_escaping_the_dir() {
        let tmp = tempfile::tempdir().unwrap();
        let dir = tmp.path().join("export");
        std::fs::create_dir_all(&dir).unwrap();
        // A traversal `file` must never redirect the read outside the dir —
        // the closed-read discipline, checked per entry.
        std::fs::write(
            dir.join("manifest.json"),
            serde_json::to_vec(&serde_json::json!({
                "version": 1,
                "entries": [{
                    "page_id": "evil",
                    "url": "https://notion.so/evil",
                    "content_sha256": "00",
                    "file": "../escape.md"
                }]
            }))
            .unwrap(),
        )
        .unwrap();
        let got = read_notion_export(&dir).unwrap();
        assert_eq!(got.len(), 1);
        assert!(
            matches!(
                got[0].as_ref().unwrap_err(),
                BridgeError::ManifestInvalid { .. }
            ),
            "a traversal file path is rejected, never read"
        );
    }

    #[cfg(unix)]
    #[test]
    fn notion_export_rejects_an_intermediate_symlink_without_reading_its_target() {
        use std::os::unix::fs::symlink;

        let tmp = tempfile::tempdir().unwrap();
        let dir = tmp.path().join("export");
        let outside = tmp.path().join("outside");
        std::fs::create_dir_all(dir.join("pages")).unwrap();
        std::fs::create_dir_all(&outside).unwrap();

        let secret = b"outside secret\n";
        std::fs::write(outside.join("credentials.md"), secret).unwrap();
        symlink(&outside, dir.join("pages/sub")).unwrap();
        std::fs::write(
            dir.join("manifest.json"),
            serde_json::to_vec(&serde_json::json!({
                "version": 1,
                "entries": [{
                    "page_id": "escape",
                    "url": "https://notion.so/escape",
                    "content_sha256": sha256_hex(secret),
                    "file": "pages/sub/credentials.md"
                }]
            }))
            .unwrap(),
        )
        .unwrap();

        let got = read_notion_export(&dir).unwrap();
        assert_eq!(got.len(), 1);
        assert!(
            got[0].is_err(),
            "an intermediate symlink escaped the export root and read outside bytes"
        );
    }

    #[cfg(unix)]
    #[test]
    fn notion_export_root_descriptor_is_not_retargeted_by_path_replacement() {
        let tmp = tempfile::tempdir().unwrap();
        let dir = tmp.path().join("export");
        let moved = tmp.path().join("pinned-export");
        let manifest_path = dir.join("manifest.json");
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(&manifest_path, b"original").unwrap();

        let root = open_export_root(&dir, &manifest_path).unwrap();
        std::fs::rename(&dir, &moved).unwrap();
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(dir.join("manifest.json"), b"attacker").unwrap();

        let bytes = read_export_file(&root, Path::new("manifest.json"), &manifest_path)
            .unwrap()
            .unwrap();
        assert_eq!(
            bytes, b"original",
            "the opened root descriptor, not the replaced pathname, owns the read"
        );
    }

    #[test]
    fn read_source_notion_is_all_or_nothing_and_anchors_at_the_url() {
        let tmp = tempfile::tempdir().unwrap();
        let dir = tmp.path().join("export");
        write_export(&dir, &[("page-a", "https://notion.so/page-a", "alpha\n")]);
        // The uniform read maps each page to a BridgeCandidate anchored at the
        // url with the kind label.
        let got = read_source(
            &BridgeSource::NotionExportDir(PathBuf::from("export")),
            tmp.path(),
            tmp.path(),
        )
        .unwrap();
        assert_eq!(got.len(), 1);
        assert_eq!(got[0].anchor, "https://notion.so/page-a");
        assert_eq!(got[0].source_label, "notion-export-dir");
        // A tampered entry sinks the WHOLE read here (Result all-or-nothing).
        std::fs::write(dir.join("pages/page-a.md"), "alpha TAMPERED\n").unwrap();
        let err = read_source(
            &BridgeSource::NotionExportDir(PathBuf::from("export")),
            tmp.path(),
            tmp.path(),
        )
        .unwrap_err();
        assert!(
            matches!(err, BridgeError::ContentHashMismatch { .. }),
            "{err:?}"
        );
    }

    #[test]
    fn bridge_import_path_constructs_no_network_client() {
        // Zero-network layering (binding law 1): the bridge reader is fs +
        // hashing only — no transport type is reachable from it. Needles are
        // concatenated so this test's own text never trips the scan.
        let source = include_str!("bridge.rs");
        let needles = [
            format!("{}{}", "Tcp", "Stream"),
            format!("{}{}", "Udp", "Socket"),
            format!("{}{}", "req", "west"),
            format!("{}{}", "hy", "per::"),
            format!("{}{}", "u", "req::"),
        ];
        for needle in &needles {
            assert!(
                !source.contains(needle.as_str()),
                "bridge must construct no network client (found {needle})"
            );
        }
    }
}
