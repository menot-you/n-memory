//! Boundary harness — `nmemory-git-subprocess-output`, declared in `tools/gates/registry.toml`.
//!
//! THE BOUNDARY. [`nmemory::git::parse_log`] is handed the stdout of a `git log`
//! child process, and [`nmemory::git::parse_anchor`] is handed a capsule's
//! `provenance.anchor` string. Both are untrusted for the same reason: they are
//! text this process did not author. The git output comes from whatever `git`
//! resolves to on `$PATH`, in whatever version, over a repository whose commit
//! messages are written by other people; the anchor comes out of the store,
//! which means out of whatever was ingested.
//!
//! WHY THESE ARE WORTH A HARNESS. Both are hand-written, record-and-field
//! separated parsers over `&str` — the shape where an off-by-one on a separator
//! index panics — and both are `pub`, pure, and total, so a harness costs
//! nothing but the call. `parse_anchor` in particular implements a `path:line`
//! and `@sha` grammar by splitting on punctuation, which is precisely where a
//! multi-byte character lands on a byte index that is not a character boundary.
//!
//! WHAT IS ASSERTED. Termination, and self-consistency of the one claim
//! `is_hex_7_40` makes. Neither parser can report an error — `parse_log` returns
//! a `Vec` and `parse_anchor` returns a struct — so a panic is their only
//! available failure, and it is reachable from a commit message.

use bolero::check;
use nmemory::git::{is_hex_7_40, parse_anchor, parse_log};

#[test]
fn git_text_parsers_are_total() {
    check!().with_max_len(8192).for_each(|bytes: &[u8]| {
        // Both parsers take `&str`; the subprocess layer decodes before
        // calling them, so non-UTF-8 input is skipped rather than lossily
        // converted. A lossy conversion would replace exactly the multi-byte
        // sequences this harness exists to exercise.
        let Ok(text) = std::str::from_utf8(bytes) else {
            return;
        };

        let _ = parse_log(text);
        let parsed = parse_anchor(text);

        // `is_hex_7_40` is a closed predicate: a string it accepts MUST be
        // between 7 and 40 characters and hexadecimal. Checking it against
        // its own definition catches a widened character class, which would
        // let a non-commit string be treated as a resolvable git anchor.
        if is_hex_7_40(text) {
            assert!(
                (7..=40).contains(&text.len()) && text.bytes().all(|b| b.is_ascii_hexdigit()),
                "is_hex_7_40 accepted a string that is not 7-40 hex characters"
            );
        }

        // The parse is deterministic: the same anchor MUST resolve the same
        // way, because a capsule's anchor is re-parsed on every recall and a
        // varying result would make provenance unstable.
        assert_eq!(
            parsed,
            parse_anchor(text),
            "parse_anchor returned different results for identical input"
        );
    });
}
