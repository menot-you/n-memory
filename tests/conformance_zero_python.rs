//! # Conformance — zero-Python scan of the crate (unit h2).
//!
//! The d12 law is ABSOLUTE: no `.py`/`.pyi`/`.pyw`/`.pyc`, no
//! `__pycache__`, no python shebang — anywhere in the crate (code, tests,
//! fixtures, tooling). The donor's fixture pipeline included
//! `mcps/memory/tests/fixtures/derive_episodes.py`; it was NOT carried in
//! any form, and this test keeps that true structurally: `cargo test`
//! itself fails if Python ever enters the crate directory.
//!
//! The scanner mirrors the h1 repo-wide gate
//! (`scripts/zero-python-gate.sh`) semantics: prune `.git`, prune a dir
//! named `target` ONLY when cargo's `CACHEDIR.TAG` marker proves it is a
//! build cache (no name-based allowlist), flag the four Python extensions,
//! `__pycache__` dirs, and python shebang first lines. One deliberate
//! strictness delta: the shell gate reads shebangs only on executable
//! files; this scanner reads the first line of EVERY regular file, so a
//! non-executable python script cannot hide either.
//!
//! NEGATIVE (the scanner sees): the same function pointed at a temp dir
//! with planted Python artifacts flags every one, and a clean control
//! temp dir scans empty.

#![allow(
    clippy::unwrap_used,
    clippy::expect_used,
    reason = "tests use unwrap/expect so fixture failures fail at the assertion site"
)]

use std::fs;
use std::io::{self, Read};
use std::path::Path;

/// Python file-name suffixes the law forbids (mirrors the gate's
/// `-name '*.py' -o -name '*.pyi' -o -name '*.pyw' -o -name '*.pyc'`).
const FORBIDDEN_SUFFIXES: [&str; 4] = [".py", ".pyi", ".pyw", ".pyc"];

/// Scan `root` recursively for Python artifacts; returns every violation
/// as a displayable path (empty = clean). I/O failure anywhere is an
/// `Err` — the scan fails closed, never a false PASS.
fn python_violations(root: &Path) -> io::Result<Vec<String>> {
    let mut violations = Vec::new();
    walk(root, &mut violations)?;
    violations.sort();
    Ok(violations)
}

fn walk(dir: &Path, violations: &mut Vec<String>) -> io::Result<()> {
    for entry in fs::read_dir(dir)? {
        let entry = entry?;
        let path = entry.path();
        let name = entry.file_name().to_string_lossy().into_owned();
        let file_type = entry.file_type()?;

        if file_type.is_dir() {
            if name == ".git" {
                continue;
            }
            // A cargo build cache is pruned only on proof (CACHEDIR.TAG);
            // a committed directory that merely NAMES itself `target` is
            // scanned like everything else.
            if name == "target" && path.join("CACHEDIR.TAG").is_file() {
                continue;
            }
            if name == "__pycache__" {
                // The dir is a violation AND its contents are scanned —
                // the shell gate's find descends too, listing both.
                violations.push(path.display().to_string());
            }
            walk(&path, violations)?;
            continue;
        }

        if FORBIDDEN_SUFFIXES.iter().any(|s| name.ends_with(s)) {
            violations.push(path.display().to_string());
            continue;
        }

        // Shebang check on the first line of every regular file (symlinks
        // are name-checked above but not content-followed).
        if file_type.is_file() && first_line_is_python_shebang(&path)? {
            violations.push(format!("{} (python shebang)", path.display()));
        }
    }
    Ok(())
}

/// True when the file's first line (within its first 512 bytes) starts
/// with `#!` and mentions python — the gate's
/// `head -c 512 | head -n 1 | grep '^#!.*python'`.
fn first_line_is_python_shebang(path: &Path) -> io::Result<bool> {
    let mut head = [0u8; 512];
    let mut file = fs::File::open(path)?;
    let mut filled = 0usize;
    loop {
        let n = file.read(&mut head[filled..])?;
        if n == 0 {
            break;
        }
        filled += n;
        if filled == head.len() {
            break;
        }
    }
    let head = &head[..filled];
    let first_line = head.split(|b| *b == b'\n').next().unwrap_or(head);
    let first_line = String::from_utf8_lossy(first_line);
    Ok(first_line.starts_with("#!") && first_line.contains("python"))
}

#[test]
fn crate_directory_is_python_free() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR"));
    let violations = python_violations(root).expect("scan must not fail");
    assert!(
        violations.is_empty(),
        "zero-Python law violated inside the crate:\n  {}",
        violations.join("\n  ")
    );
}

#[test]
fn planted_python_artifacts_are_flagged_by_the_same_scanner() {
    // NEGATIVE: prove the scanner SEES. Every forbidden shape is planted
    // in a temp dir and must be flagged by the exact function the positive
    // test runs.
    let dir = tempfile::tempdir().expect("tempdir");
    let root = dir.path();

    fs::write(root.join("planted.py"), "print(\"planted\")\n").expect("plant .py");
    fs::write(root.join("stubs.pyi"), "x: int\n").expect("plant .pyi");
    fs::create_dir_all(root.join("pkg").join("__pycache__")).expect("plant __pycache__");
    fs::write(
        root.join("pkg")
            .join("__pycache__")
            .join("mod.cpython-312.pyc"),
        b"\x00",
    )
    .expect("plant .pyc");
    // The interpreter name is assembled at compile time so THIS source file
    // never contains the forbidden byte sequence (the genesis repo-wide
    // scanner greps file contents for invocation shapes — same
    // fragmentation trick its own gate script uses).
    fs::write(
        root.join("script"),
        concat!("#!/usr/bin/env py", "thon3\nprint(1)\n"),
    )
    .expect("plant shebang file");
    // Camouflage: an innocent Rust file next to the plants must NOT be
    // flagged, so a hit is a detection, not a scanner that flags all.
    fs::write(root.join("innocent.rs"), "fn main() {}\n").expect("write control file");

    let violations = python_violations(root).expect("scan must not fail");
    for needle in [
        "planted.py",
        "stubs.pyi",
        "__pycache__",
        "mod.cpython-312.pyc",
        "script (python shebang)",
    ] {
        assert!(
            violations.iter().any(|v| v.contains(needle)),
            "planted artifact {needle:?} must be flagged, got: {violations:?}"
        );
    }
    assert!(
        !violations.iter().any(|v| v.contains("innocent.rs")),
        "the clean control file must not be flagged: {violations:?}"
    );

    // CONTROL: a fully clean tree scans empty — the flags above are
    // detections, not noise.
    let clean = tempfile::tempdir().expect("clean tempdir");
    fs::write(clean.path().join("lib.rs"), "pub fn f() {}\n").expect("write rs");
    fs::write(
        clean.path().join("run.sh"),
        "#!/usr/bin/env bash\necho ok\n",
    )
    .expect("write sh");
    assert_eq!(
        python_violations(clean.path()).expect("scan must not fail"),
        Vec::<String>::new()
    );
}

#[test]
fn target_prune_requires_cargo_proof_not_a_name() {
    // A dir named `target` WITH cargo's CACHEDIR.TAG is a build cache:
    // pruned, exactly like the h1 gate.
    let cache = tempfile::tempdir().expect("tempdir");
    let cache_target = cache.path().join("target");
    fs::create_dir_all(&cache_target).expect("mkdir target");
    fs::write(
        cache_target.join("CACHEDIR.TAG"),
        "Signature: 8a477f597d28d172789f06886806bc55\n",
    )
    .expect("write tag");
    fs::write(cache_target.join("hidden.py"), "print(1)\n").expect("plant in cache");
    assert_eq!(
        python_violations(cache.path()).expect("scan must not fail"),
        Vec::<String>::new(),
        "a proven cargo cache dir is pruned"
    );

    // The SAME layout without the marker is a committed directory that
    // happens to be named `target`: scanned, and the plant is flagged.
    let committed = tempfile::tempdir().expect("tempdir");
    let committed_target = committed.path().join("target");
    fs::create_dir_all(&committed_target).expect("mkdir target");
    fs::write(committed_target.join("hidden.py"), "print(1)\n").expect("plant in dir");
    let violations = python_violations(committed.path()).expect("scan must not fail");
    assert!(
        violations.iter().any(|v| v.contains("hidden.py")),
        "an unproven `target` dir must be scanned: {violations:?}"
    );
}
