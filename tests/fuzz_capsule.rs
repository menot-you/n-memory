//! Boundary harness — `nmemory-capsule-json`, declared in `tools/gates/registry.toml`.
//!
//! THE BOUNDARY. A [`Capsule`] is reconstructed from JSON that this process did
//! not write: the `canonical_json` column of a SQLite file (including a FOREIGN
//! store pulled in by `Store::merge_from`), and a `notion-pull` export read back
//! through the bridge. `#[serde(try_from = "RawCapsule")]` funnels every one of
//! those through the validator, so this one call is where hostile bytes become a
//! typed value.
//!
//! WHAT IS ASSERTED, AND WHY IT IS NOT "IT PARSES". Almost every input here is
//! invalid JSON, and rejecting it is the CORRECT answer — a harness that
//! required success would be asserting the opposite of the contract. What is
//! asserted is that the parser reaches a verdict at all: it MUST NOT panic,
//! MUST NOT abort, and MUST NOT hang. A panic here is reachable from a corrupt
//! database row, so it is a crash triggered by data rather than by code.
//!
//! The second property is stronger and is the one worth the harness: a Capsule
//! that DID parse MUST re-serialize and parse again to the same value. If that
//! ever fails, the store can persist a capsule it cannot read back — silent data
//! loss that no round-trip test over hand-written fixtures would find, because
//! the value that breaks it is by definition one nobody thought to write down.
//!
//! WHY THERE ARE TWO ARMS HERE, and it is the lesson of this file. The
//! raw-bytes arm alone was measured against its own subject with
//! `cargo mutants -f src/capsule.rs -- -E 'test(=capsule_json_reaches_a_verdict)'`
//! and killed NOTHING: 24 mutants, 16 missed, 8 unviable, ZERO caught. Random
//! bytes essentially never form a valid capsule document, so every assertion sat
//! behind an `Ok(...)` that never happened. The harness ran, passed, and proved
//! nothing — the exact shape `d39-proven-falsifiable-tests` forbids, and
//! invisible until the mutants were counted.
//!
//! So the raw arm keeps its job — it holds the committed corpus of real
//! documents and proves the parser reaches a verdict on hostile input — and a
//! SECOND arm generates structurally plausible capsules so the VALIDATOR
//! actually runs, then re-checks every invariant `Capsule::new` documents.
//! Delete a validation branch and the second arm notices.
//!
//! Two ways to run, one file:
//!   * `cargo nextest run` — bounded property mode, deterministic corpus first.
//!   * driven externally by the dispatch-only libFuzzer job, which treats the
//!     corpus under `tests/__fuzz__/<test-fn>/corpus/` as its seed set.

use bolero::check;
use nmemory::capsule::Capsule;

#[test]
fn capsule_json_reaches_a_verdict() {
    check!().with_max_len(4096).for_each(|bytes: &[u8]| {
        let Ok(parsed) = serde_json::from_slice::<Capsule>(bytes) else {
            // The expected outcome for nearly every input. A REJECTION is
            // the parser working, so there is nothing further to check.
            return;
        };

        // It parsed, so the round-trip contract applies. `canonical_json` is
        // what the store writes to disk; if re-reading it does not yield the
        // same capsule, the store has persisted something it cannot recover.
        let Ok(reserialized) = serde_json::to_vec(&parsed) else {
            panic!("a Capsule that deserialized failed to serialize: {parsed:?}");
        };
        match serde_json::from_slice::<Capsule>(&reserialized) {
            Ok(round_tripped) => assert_eq!(
                parsed, round_tripped,
                "a Capsule changed value across a serialize/deserialize round trip, \
                     so the store can write a row it reads back differently"
            ),
            Err(error) => panic!(
                "a Capsule's own serialized bytes failed to parse: {error} — \
                     the store would be unable to read back what it just wrote"
            ),
        }
    });
}

/// A capsule document assembled from generated FIELDS rather than from raw
/// bytes, so the document is nearly always well-formed JSON of the right shape
/// and the validator behind `#[serde(try_from = "RawCapsule")]` actually runs.
/// Whether it ACCEPTS is exactly what varies, which is what makes a mutated
/// validation branch observable.
fn document(
    content: &str,
    source: &str,
    anchor: &str,
    source_hash: &str,
    project_id: &str,
    confidence: f64,
    valid_to_before_from: bool,
) -> String {
    let valid_from = "2026-07-29T12:00:00Z";
    let valid_to = if valid_to_before_from {
        // Deliberately BEFORE valid_from: the inverted-window branch is one of
        // the six the validator claims to enforce, so it has to be reachable.
        "\"2020-01-01T00:00:00Z\""
    } else {
        "null"
    };
    format!(
        r#"{{"content":{},"provenance":{{"source":{},"anchor":{},"source_hash":{}}},"confidence":{},"freshness":{{"valid_from":"{}","valid_to":{}}},"scope":{{"project_id":{}}},"authority_class":"observed-fact","instruction_taint":false}}"#,
        serde_json::Value::String(content.to_string()),
        serde_json::Value::String(source.to_string()),
        serde_json::Value::String(anchor.to_string()),
        serde_json::Value::String(source_hash.to_string()),
        confidence,
        valid_from,
        valid_to,
        serde_json::Value::String(project_id.to_string()),
    )
}

#[test]
fn capsule_validator_enforces_every_invariant_it_documents() {
    check!()
        .with_type::<(String, String, String, String, String, f64, bool)>()
        .for_each(
            |(content, source, anchor, source_hash, project_id, confidence, inverted): &(
                String,
                String,
                String,
                String,
                String,
                f64,
                bool,
            )| {
                let raw = document(
                    content,
                    source,
                    anchor,
                    source_hash,
                    project_id,
                    *confidence,
                    *inverted,
                );

                let Ok(capsule) = serde_json::from_str::<Capsule>(&raw) else {
                    // Rejection is frequently correct — an empty field, a
                    // confidence outside 0..=1, an inverted window. Nothing to
                    // check on a value that was never produced.
                    return;
                };

                // It was ACCEPTED, so every invariant Capsule::new documents
                // MUST hold. Each assertion below mirrors one rejection branch;
                // delete that branch upstream and this fires.
                assert!(
                    !capsule.content().trim().is_empty(),
                    "a capsule with blank content was accepted"
                );
                assert!(
                    !capsule.provenance().source.trim().is_empty(),
                    "a capsule with a blank provenance source was accepted"
                );
                assert!(
                    !capsule.provenance().anchor.trim().is_empty(),
                    "a capsule with a blank provenance anchor was accepted"
                );
                assert!(
                    !capsule.provenance().source_hash.trim().is_empty(),
                    "a capsule with a blank provenance source_hash was accepted"
                );
                assert!(
                    !capsule.scope().project_id.trim().is_empty(),
                    "a capsule with a blank scope project_id was accepted"
                );

                let value = capsule.confidence().value();
                assert!(
                    !value.is_nan() && (0.0..=1.0).contains(&value),
                    "a capsule with confidence {value} outside 0.0..=1.0 was accepted"
                );

                let freshness = capsule.freshness();
                if let Some(valid_to) = freshness.valid_to {
                    assert!(
                        valid_to >= freshness.valid_from,
                        "a capsule with an INVERTED freshness window was accepted"
                    );
                }
            },
        );
}
