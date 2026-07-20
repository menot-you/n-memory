//! # Conformance — native bridge readers (unit w1-bridge).
//!
//! Behavioral coverage of `src/bridge.rs` against committed fixtures in
//! `tests/fixtures/bridge/`: closed-enum coverage (every variant, only
//! its files), the documented split rule end-to-end, missing-source typed
//! errors, and the no-directory-traversal negatives (a subdir `.md` is
//! NEVER picked up except via `MemoryDir`'s own single-level listing —
//! and not even then). Symlink behavior (leaf rejected, parent followed)
//! runs against tempdirs, unix-only.

#![allow(
    clippy::unwrap_used,
    clippy::expect_used,
    reason = "tests use unwrap/expect so fixture failures fail at the assertion site"
)]

use std::path::{Path, PathBuf};

use nmemory::bridge::{BridgeError, BridgeSource, read_source};

fn fixture_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("tests")
        .join("fixtures")
        .join("bridge")
}

/// The expected anchor for `file:line`, mirroring the bridge's q92 rewrite:
/// root-relative when the file sits under `ANCHOR_ROOT`, else absolute — so
/// these pins hold in BOTH the isolated worktree (outside the root) and the
/// in-root checkout (where the rewrite fires).
fn anchor_for(file: &Path, line: u32) -> String {
    let path = file
        .strip_prefix(nmemory::retrieve::ANCHOR_ROOT)
        .unwrap_or(file);
    format!("{}:{line}", path.display())
}

// ---- split rule end-to-end over committed fixtures ----

#[test]
fn project_claude_md_splits_per_the_documented_rule() {
    let base = fixture_root().join("project");
    let file = base.join("CLAUDE.md");
    let got = read_source(&BridgeSource::ProjectClaudeMd, &base).expect("fixture reads");

    let contents: Vec<&str> = got.iter().map(|c| c.content.as_str()).collect();
    assert_eq!(
        contents,
        vec![
            // Preamble carries the lone h1 title (split level is ##).
            "# Project canon\n\nIntro paragraph under the title.",
            // The fenced `# not a heading` line must ride inside, unsplit.
            "## Build\n\n```bash\n# not a heading: fenced comment\n\nmake build\n```",
            "## Deploy\n\nDeploy notes.",
        ]
    );
    // Frontmatter never leaks into any candidate.
    assert!(got.iter().all(|c| !c.content.contains("frontmatter")));
    // Anchors are `<resolved path>:<original 1-based line>`.
    let anchors: Vec<String> = got.iter().map(|c| c.anchor.clone()).collect();
    assert_eq!(
        anchors,
        vec![
            anchor_for(&file, 5),
            anchor_for(&file, 9),
            anchor_for(&file, 17),
        ]
    );
    assert!(got.iter().all(|c| c.source_label == "project-claude-md"));
}

#[test]
fn agents_md_without_headings_splits_into_paragraph_blocks() {
    let base = fixture_root().join("project");
    let file = base.join("AGENTS.md");
    let got = read_source(&BridgeSource::ProjectAgentsMd, &base).expect("fixture reads");

    let pairs: Vec<(String, &str)> = got
        .iter()
        .map(|c| (c.anchor.clone(), c.content.as_str()))
        .collect();
    assert_eq!(
        pairs,
        vec![
            (anchor_for(&file, 1), "First agent rule spans\ntwo lines."),
            (anchor_for(&file, 4), "Second agent rule."),
        ]
    );
    assert!(got.iter().all(|c| c.source_label == "project-agents-md"));
}

// ---- UserClaudeMd probe order ----

#[test]
fn user_claude_md_prefers_claude2_over_claude() {
    let base = fixture_root().join("home_both");
    let got = read_source(&BridgeSource::UserClaudeMd, &base).expect("fixture reads");
    assert_eq!(got.len(), 1);
    assert_eq!(got[0].content, "claude2 canon wins.");
    assert!(got[0].anchor.contains(".claude2"));
    assert_eq!(got[0].source_label, "user-claude-md");
    // The decoy under .claude was never surfaced.
    assert!(!got[0].content.contains("decoy"));
}

#[test]
fn user_claude_md_prefers_the_live_claude3_generation() {
    // The LIVE harness generation on real hosts is `.claude3` — a stale
    // `.claude2` canon must lose to it (W1 integrate rung record).
    let base = fixture_root().join("home_claude3");
    let got = read_source(&BridgeSource::UserClaudeMd, &base).expect("fixture reads");
    assert_eq!(got.len(), 1);
    assert_eq!(got[0].content, "claude3 live canon wins.");
    assert!(got[0].anchor.contains(".claude3"));
    // The stale .claude2 decoy was never surfaced.
    assert!(!got[0].content.contains("stale"));
}

#[test]
fn user_claude_md_falls_back_to_legacy_claude() {
    let base = fixture_root().join("home_legacy");
    let got = read_source(&BridgeSource::UserClaudeMd, &base).expect("fixture reads");
    assert_eq!(got.len(), 1);
    assert_eq!(got[0].content, "legacy fallback canon.");
    assert!(got[0].anchor.contains("/.claude/"));
}

// ---- missing source = typed error (never a silent empty) ----

#[test]
fn missing_project_file_is_a_typed_error() {
    // The memory fixture dir exists but holds no CLAUDE.md.
    let base = fixture_root().join("memory");
    let err = read_source(&BridgeSource::ProjectClaudeMd, &base).expect_err("must be typed");
    match err {
        BridgeError::SourceMissing {
            source_label,
            tried,
        } => {
            assert_eq!(source_label, "project-claude-md");
            assert_eq!(tried, vec![base.join("CLAUDE.md")]);
        }
        other => panic!("expected SourceMissing, got {other:?}"),
    }
}

#[test]
fn missing_user_claude_md_reports_every_probed_path() {
    let base = fixture_root().join("project"); // has no .claude3/.claude2/.claude
    let err = read_source(&BridgeSource::UserClaudeMd, &base).expect_err("must be typed");
    match err {
        BridgeError::SourceMissing {
            source_label,
            tried,
        } => {
            assert_eq!(source_label, "user-claude-md");
            assert_eq!(
                tried,
                vec![
                    base.join(".claude3").join("CLAUDE.md"),
                    base.join(".claude2").join("CLAUDE.md"),
                    base.join(".claude").join("CLAUDE.md"),
                ]
            );
        }
        other => panic!("expected SourceMissing, got {other:?}"),
    }
}

// ---- MemoryDir: the one parameterized source, still no traversal ----

#[test]
fn memory_dir_reads_only_top_level_md_files_in_sorted_order() {
    let root = fixture_root();
    let dir = root.join("memory");
    let got = read_source(&BridgeSource::MemoryDir(PathBuf::from("memory")), &root)
        .expect("fixture reads");

    let pairs: Vec<(String, &str)> = got
        .iter()
        .map(|c| (c.anchor.clone(), c.content.as_str()))
        .collect();
    assert_eq!(
        pairs,
        vec![
            (anchor_for(&dir.join("a.md"), 1), "alpha memory."),
            (anchor_for(&dir.join("b.md"), 1), "bravo memory."),
            (anchor_for(&dir.join("b.md"), 3), "bravo second block."),
        ]
    );
    assert!(got.iter().all(|c| c.source_label == "memory-dir"));

    // Negatives, all from the same listing:
    // - sub/nested.md exists but is NEVER picked up (no recursion);
    // - c.MD is skipped (case-sensitive extension rule);
    // - notes.txt is skipped (not markdown);
    // - empty.md contributes zero candidates (skip-empty rule).
    assert!(got.iter().all(|c| !c.content.contains("nested")));
    // q117: the no-recursion anchor negative pins on the nested file's OWN
    // anchor prefix (the q92 anchor_for idiom) — never a substring scan of
    // the whole anchor, whose absolute form carries the checkout path and
    // false-fails in any checkout named "…sub…" (e.g. a lane worktree).
    let nested_anchor_1 = anchor_for(&dir.join("sub").join("nested.md"), 1);
    let nested_prefix = nested_anchor_1
        .strip_suffix('1')
        .expect("anchor_for(_, 1) ends in the line number");
    assert!(got.iter().all(|c| !c.anchor.starts_with(nested_prefix)));
    assert!(got.iter().all(|c| !c.content.contains("uppercase")));
    assert!(got.iter().all(|c| !c.content.contains("not markdown")));
    assert_eq!(got.len(), 3);
}

#[test]
fn memory_dir_accepts_an_absolute_path_ignoring_base_dir() {
    let absolute = fixture_root().join("memory");
    let got = read_source(
        &BridgeSource::MemoryDir(absolute),
        Path::new("/nonexistent-base-never-touched"),
    )
    .expect("absolute dir reads regardless of base_dir");
    assert_eq!(got.len(), 3);
}

#[test]
fn memory_dir_missing_is_a_typed_error() {
    let root = fixture_root();
    let err = read_source(
        &BridgeSource::MemoryDir(PathBuf::from("no_such_dir")),
        &root,
    )
    .expect_err("must be typed");
    assert!(
        matches!(err, BridgeError::SourceMissing { source_label, .. }
        if source_label == "memory-dir")
    );
}

#[test]
fn memory_dir_pointing_at_a_file_is_a_typed_error() {
    let root = fixture_root();
    let err = read_source(
        &BridgeSource::MemoryDir(PathBuf::from("memory").join("a.md")),
        &root,
    )
    .expect_err("must be typed");
    assert!(matches!(err, BridgeError::NotADirectory(p)
        if p == root.join("memory").join("a.md")));
}

// ---- closed-enum coverage: every variant, nothing else ----

#[test]
fn every_closed_enum_variant_is_covered_and_labeled() {
    let root = fixture_root();
    // Exhaustive on purpose: adding a BridgeSource variant breaks this
    // test (and `source_label`) until its coverage exists.
    let cases: Vec<(BridgeSource, PathBuf, &str)> = vec![
        (
            BridgeSource::UserClaudeMd,
            root.join("home_legacy"),
            "user-claude-md",
        ),
        (
            BridgeSource::ProjectClaudeMd,
            root.join("project"),
            "project-claude-md",
        ),
        (
            BridgeSource::ProjectAgentsMd,
            root.join("project"),
            "project-agents-md",
        ),
        (
            BridgeSource::MemoryDir(PathBuf::from("memory")),
            root.clone(),
            "memory-dir",
        ),
    ];
    for (source, base, label) in &cases {
        match source {
            BridgeSource::UserClaudeMd
            | BridgeSource::ProjectClaudeMd
            | BridgeSource::ProjectAgentsMd
            | BridgeSource::MemoryDir(_) => {}
        }
        assert_eq!(source.source_label(), *label);
        let got = read_source(source, base).expect("covered variant reads its fixture");
        assert!(!got.is_empty());
        assert!(got.iter().all(|c| c.source_label == *label));
    }
}

// ---- symlink policy: leaf rejected, parent followed (unix) ----

#[cfg(unix)]
#[test]
fn a_leaf_symlink_standing_in_for_a_whitelisted_name_is_rejected() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let target = tmp.path().join("target.md");
    std::fs::write(&target, "secret bytes the bridge must never surface").unwrap();
    let leaf = tmp.path().join("CLAUDE.md");
    std::os::unix::fs::symlink(&target, &leaf).unwrap();

    let err = read_source(&BridgeSource::ProjectClaudeMd, tmp.path()).expect_err("rejected");
    assert!(matches!(err, BridgeError::SymlinkRejected(p) if p == leaf));
}

#[cfg(unix)]
#[test]
fn a_symlinked_parent_directory_with_a_plain_leaf_is_accepted() {
    // Dotfiles pattern (donor r3 lesson): ~/.claude2 -> dotfiles repo,
    // plain CLAUDE.md inside. Only the leaf is checked.
    let tmp = tempfile::tempdir().expect("tempdir");
    let real = tmp.path().join("dotfiles");
    std::fs::create_dir(&real).unwrap();
    std::fs::write(real.join("CLAUDE.md"), "canon behind a symlinked parent").unwrap();
    let home = tmp.path().join("home");
    std::fs::create_dir(&home).unwrap();
    std::os::unix::fs::symlink(&real, home.join(".claude2")).unwrap();

    let got = read_source(&BridgeSource::UserClaudeMd, &home).expect("parent symlink is fine");
    assert_eq!(got.len(), 1);
    assert_eq!(got[0].content, "canon behind a symlinked parent");
}

#[cfg(unix)]
#[test]
fn a_symlinked_md_entry_inside_memory_dir_is_skipped_not_followed() {
    let tmp = tempfile::tempdir().expect("tempdir");
    std::fs::write(tmp.path().join("real.md"), "real memory").unwrap();
    let outside = tmp.path().join("outside.txt");
    std::fs::write(&outside, "outside bytes").unwrap();
    std::os::unix::fs::symlink(&outside, tmp.path().join("link.md")).unwrap();

    let got = read_source(
        &BridgeSource::MemoryDir(tmp.path().to_path_buf()),
        Path::new("/unused"),
    )
    .expect("listing reads");
    assert_eq!(got.len(), 1);
    assert_eq!(got[0].content, "real memory");
}
