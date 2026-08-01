//! Boundary harness — `nmemory-extract-text`, declared in `tools/gates/registry.toml`.
//!
//! THE BOUNDARY. [`nmemory::extract::extract`] is handed the raw `content` of an
//! MCP `memory_ingest` call: text an agent produced, which this process did not
//! author and cannot constrain. It is the widest untrusted-text surface in the
//! crate and, unlike the JSON boundaries, no schema stands in front of it —
//! every byte reaches the parser.
//!
//! WHY THIS TARGET. `extract` returns `Vec<ExtractCandidate>` with NO error
//! variant, so it cannot report a bad input; its only available failure is a
//! panic. Underneath it does heavy index-based string slicing — sentence
//! splitting, marker stripping, bracket stamps — and slicing a `&str` at an
//! index that is not a UTF-8 character boundary panics. That defect is invisible
//! to example-based tests, which get written in ASCII, and reachable by any
//! agent that ingests a multi-byte character at the wrong offset.
//!
//! THE TWO PROPERTIES, and neither is "the right candidates come back". Which
//! candidates the heuristics emit is a judgement call the rule tests already
//! pin; that it TERMINATES and that it terminates the SAME WAY every time are
//! contracts. Determinism is the load-bearing one: the store's content hashing
//! and its idempotent-capture guarantee both assume the same text yields the
//! same candidates, so an extractor that varied with allocation order or hash
//! iteration order would produce duplicate capsules for one input and nothing
//! else in the suite would notice.

use bolero::check;
use nmemory::extract::extract;

#[test]
fn extract_is_total_and_deterministic() {
    check!().with_max_len(8192).for_each(|bytes: &[u8]| {
        // Only valid UTF-8 reaches `extract` in production — the MCP layer
        // decodes JSON first — so non-UTF-8 input is SKIPPED rather than
        // lossily converted. A lossy conversion would replace exactly the
        // multi-byte sequences this harness exists to exercise, and the run
        // would prove the parser safe against bytes it never sees.
        let Ok(text) = std::str::from_utf8(bytes) else {
            return;
        };

        // Property one: it returns at all. A panic here is a crash reachable
        // from ingested content, which is to say from anything an agent
        // types.
        let first = extract(text);

        // Property two: the same text yields the same candidates. The store
        // hashes content to make capture idempotent, so a nondeterministic
        // extractor would write a second capsule for an input it already
        // holds, and the duplicate would look like a genuine second memory.
        let second = extract(text);
        assert_eq!(
            first, second,
            "extract returned different candidates for identical input, so \
                 capture is not idempotent for this text"
        );

        // Property three, and the one with teeth. The module documents the cue
        // as `<rule>:'<literal>'`, where the literal is "the exact rule id and
        // the literal token or phrase that fired, so every kind hint is
        // auditable back to bytes actually present in the content". That is a
        // checkable promise, and determinism does NOT check it: a mutated cue
        // builder returns the same wrong answer twice and property two still
        // passes. Measured: without this, the harness killed zero mutants.
        //
        // The comparison folds case because the rules fold before matching, so
        // the cue carries the folded literal.
        for candidate in &first {
            assert!(
                !candidate.cue.is_empty(),
                "a candidate came back with an EMPTY cue, so its kind hint is unauditable"
            );
            // EVERY quoted literal, not the span from the first quote to the
            // last. A compound cue carries two — `frame:'must' entity:'must'` —
            // and spanning them captured `must' entity:'must`, which is in no
            // document. Observed as a real failure of this very assertion, which
            // is the first evidence it is strong enough to be wrong.
            //
            // Splitting on the quote makes every ODD segment a literal.
            let folded_content = candidate.content.to_lowercase();
            for (index, literal) in candidate.cue.split('\'').enumerate() {
                if index % 2 == 0 || literal.is_empty() {
                    continue;
                }
                assert!(
                    folded_content.contains(&literal.to_lowercase()),
                    "the cue claims literal {literal:?} fired, but those bytes are NOT \
                     in the candidate's own content — the kind hint cannot be audited \
                     back to the text"
                );
            }
        }
    });
}
