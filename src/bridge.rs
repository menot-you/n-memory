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

use std::ffi::OsStr;
use std::path::{Path, PathBuf};

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
    match std::fs::read_to_string(path) {
        Ok(content) => Ok(Some(content)),
        // Vanished between probe and read (TOCTOU window, kept narrow):
        // same "absent" answer the probe would have given.
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(e) => Err(BridgeError::Io {
            path: path.to_path_buf(),
            message: e.to_string(),
        }),
    }
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
}
