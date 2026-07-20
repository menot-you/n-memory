//! # Conformance — retrieve grounding + abstain + evidence armor (unit h2).
//!
//! Ported donor behavior (zero authority, reference only): the v1-scope
//! subset of donor B's `mcps/memory/tests/retrieve_recall.rs` @6d495898 —
//! real queries over an ingested corpus ground with 100% field-complete
//! results; a cross-project query returns zero rows, never the restricted
//! capsule or a leaking error; a tainted capsule appears FLAGGED, never
//! silently excluded or promoted; retrieving twice yields the same ranked
//! order; a query matching nothing abstains, never fabricates. Re-authored
//! against the new crate API (`retrieve(store, query, now, anchor_root)`
//! with the evidence envelope) with corpora seeded through the real ingest door,
//! mirroring the donor's production-handler discipline.
//!
//! Strengthened vs donor: `now` is injected here (no wall-clock reads), so
//! the repeat-query gate asserts byte-identical JSON, not just order. The
//! donor's superseded-capsule and foreign-supersede-edge gates are NOT
//! carried here (h4's own suites in `src/` carry superseded-exclusion;
//! consolidate beyond exact-dedup is deferred), and its extract/classify
//! seeding pipeline is donor scope. `retrieve` takes the store mutably
//! since h4: returned ids are recall-counted into the usage sidecar.

#![allow(
    clippy::unwrap_used,
    clippy::expect_used,
    reason = "tests use unwrap/expect so fixture failures fail at the assertion site"
)]

use std::path::Path;

use nmemory::capsule::AuthorityClass;
use nmemory::ingest::{IngestDefaults, IngestRequest, ingest};
use nmemory::retrieve::{ADVISORY_NOT_AUTHORITY, RetrieveError, RetrieveQuery, RetrieveResponse};
use nmemory::store::{Store, Tier};
use time::OffsetDateTime;
use time::macros::datetime;

/// Injected instants: capture happens before the query instant.
const CAPTURED: OffsetDateTime = datetime!(2026-07-18 12:00:00 UTC);
const NOW: OffsetDateTime = datetime!(2026-07-18 20:00:00 UTC);

/// Test seam for the public entry: every recall in this suite injects
/// the SAME hermetic, nonexistent anchor root (the root is boot-injected
/// config, no longer a crate constant), so anchor-probe verdicts are
/// box-independent while the envelope armor gate still sees the full
/// tri-state wire shape.
fn retrieve(
    store: &mut Store,
    query: &RetrieveQuery,
    now: OffsetDateTime,
) -> Result<RetrieveResponse, RetrieveError> {
    nmemory::retrieve::retrieve(
        store,
        query,
        now,
        Path::new("/nmemory-hermetic-test-anchor-root"),
    )
}

fn defaults() -> IngestDefaults {
    IngestDefaults {
        project_id: "nott".to_string(),
    }
}

fn request(content: &str, anchor: &str) -> IngestRequest {
    IngestRequest {
        content: content.to_string(),
        source: "session:2026-07-18".to_string(),
        anchor: anchor.to_string(),
        confidence: None,
        valid_from: None,
        valid_to: None,
        project_id: None,
        authority_class: None,
        instruction_taint: None,
        supersedes: None,
        session_id: None,
    }
}

fn query(terms: &[&str]) -> RetrieveQuery {
    RetrieveQuery {
        terms: terms.iter().map(|t| (*t).to_string()).collect(),
        ..RetrieveQuery::default()
    }
}

/// Seed a known-topic corpus through the real ingest door (the donor
/// seeded through its production handler; the engine API is the new
/// crate's equivalent door).
fn seeded_store() -> Store {
    let mut store = Store::open_in_memory().expect("open");
    let corpus = [
        (
            "the sync engine config lives in config.rs",
            "mcps/ssot/src/sync/config.rs:1",
        ),
        (
            "session context is assembled in context.rs",
            "mcps/ssot/src/sync/context.rs:1",
        ),
        (
            "the sync engine drives remote replication",
            "mcps/ssot/src/sync/engine.rs:1",
        ),
        (
            "outbox rows stage writes before dispatch",
            "mcps/ssot/src/sync/outbox.rs:1",
        ),
        (
            "conflict resolution happens in resolve.rs",
            "mcps/ssot/src/sync/resolve.rs:1",
        ),
        (
            "state transitions are checked in state.rs",
            "mcps/ssot/src/sync/state.rs:1",
        ),
        (
            "the jwt guard fails closed with 401",
            "mcps/ssot/src/http/auth.rs:1",
        ),
        (
            "the conflict lifecycle decision chose last-writer-wins",
            "doc-1402#conflict",
        ),
        (
            "remote sync retries with exponential backoff",
            "doc-1402#retry",
        ),
        (
            "the step sequence for release is build then verify then tag",
            "RELEASE.md:1",
        ),
    ];
    for (content, anchor) in corpus {
        ingest(&mut store, request(content, anchor), defaults(), CAPTURED).expect("seed ingest");
    }
    store
}

/// Donor hard gate (a): ten queries over known-present topics all ground
/// non-empty, and EVERY result is field-complete on the wire — the new
/// crate's field set is the evidence envelope (armor label + framing +
/// explain), asserted on serialized JSON so the wire truth is what is
/// checked.
#[test]
fn ten_queries_ground_with_100pct_field_completeness() {
    let mut store = seeded_store();
    let queries: [&[&str]; 10] = [
        &["config"],
        &["context"],
        &["sync", "engine"],
        &["outbox"],
        &["resolve"],
        &["state"],
        &["jwt"],
        &["conflict", "lifecycle"],
        &["remote", "sync"],
        &["step", "sequence"],
    ];

    for terms in queries {
        let response = retrieve(&mut store, &query(terms), NOW).expect("retrieve");
        let value = serde_json::to_value(&response).expect("serialize response");
        assert_eq!(
            value["outcome"], "grounded",
            "query {terms:?} must ground on a known-present topic, got {value}"
        );
        let results = value["results"].as_array().expect("results array");
        assert!(
            !results.is_empty(),
            "query {terms:?} grounded but returned an empty result set"
        );
        for result in results {
            assert_eq!(result["label"], ADVISORY_NOT_AUTHORITY);
            assert_eq!(result["framing"], "DATA");
            assert!(result["id"].as_str().expect("id").starts_with("cap-"));
            assert!(!result["headline"].as_str().expect("headline").is_empty());
            assert!(result["instruction_taint"].is_boolean());
            assert!(
                !result["authority_class"]
                    .as_str()
                    .expect("class")
                    .is_empty()
            );
            let confidence = result["confidence"].as_f64().expect("confidence");
            assert!(
                (0.0..=1.0).contains(&confidence),
                "confidence out of range: {confidence}"
            );
            // W2 envelope: the decay explain is present, positive, and
            // never amplifies the stored confidence (2^(-age/hl) <= 1).
            let decayed = result["decayed_weight"].as_f64().expect("decayed_weight");
            assert!(
                0.0 < decayed && decayed <= confidence,
                "decayed_weight must be in (0, confidence], got {decayed} vs {confidence}"
            );
            // W2 envelope: anchor_live is the documented tri-state. The
            // VERDICT depends on the live probe root (the one deliberate
            // non-hermetic read); the DOMAIN does not.
            let anchor_live = &result["anchor_live"];
            assert!(
                anchor_live.is_boolean() || anchor_live == &serde_json::json!("unknown"),
                "anchor_live must be true | false | \"unknown\", got {anchor_live}"
            );
            for field in ["source", "anchor", "source_hash"] {
                assert!(
                    !result["provenance"][field]
                        .as_str()
                        .expect(field)
                        .is_empty(),
                    "provenance.{field} must be non-empty"
                );
            }
            assert!(result["freshness"]["valid_from"].is_string());
            assert!(
                result["freshness"].get("valid_to").is_some(),
                "valid_to must be explicit"
            );
            assert!(
                !result["matched_terms"]
                    .as_array()
                    .expect("terms")
                    .is_empty(),
                "explain must name at least one matched term"
            );
            let relevance = result["relevance"].as_f64().expect("relevance");
            assert!(
                (0.0..=1.0).contains(&relevance),
                "relevance is the normalized 0..=1 explain, got {relevance}"
            );
            let bm25 = result["bm25"].as_f64().expect("bm25");
            assert!(
                bm25 < 0.0,
                "bm25 stays a (rounded) negative match score, got {bm25}"
            );
        }
        // The relevance scale is anchored per response: top hit = 1.0.
        assert_eq!(
            results[0]["relevance"].as_f64().expect("top relevance"),
            1.0,
            "query {terms:?}: the top hit anchors the relevance scale at 1.0"
        );
    }
}

/// Donor hard gate (c) + CAP-10: a query fenced to a project the capsule
/// does not belong to returns zero rows — the new engine ABSTAINS, and
/// neither the restricted content nor its id leaks through the response.
#[test]
fn a_cross_project_query_returns_zero_rows_never_the_restricted_capsule() {
    let mut store = Store::open_in_memory().expect("open");
    let mut restricted = request(
        "confidential rollout plan for the other-project launch",
        "other-repo/ROLLOUT.md:1",
    );
    restricted.project_id = Some("other-project".to_string());
    let outcome = ingest(&mut store, restricted, defaults(), CAPTURED).expect("seed");

    let mut fenced = query(&["confidential", "rollout", "launch"]);
    fenced.project_id = Some("nott".to_string());
    let response = retrieve(&mut store, &fenced, NOW).expect("cross-project query must not error");

    let RetrieveResponse::Abstain { reason } = &response else {
        panic!("cross-project query must abstain, got: {response:?}");
    };
    // No leak: the response names neither the restricted content nor its
    // capsule id.
    let raw = serde_json::to_string(&response).expect("serialize");
    assert!(
        !raw.contains("confidential") && !raw.contains(outcome.id.as_str()),
        "cross-project response must not leak the restricted capsule: {raw}"
    );
    assert!(
        reason.contains("nott"),
        "the reason names the caller's own fence, got: {reason}"
    );
}

/// Donor rule 7: a tainted capsule appears in results as FLAGGED data —
/// never silently excluded, never promoted to a directive. Seeded through
/// ingest so the taint is real policy (imports born tainted), not
/// fixture construction.
#[test]
fn a_tainted_capsule_appears_in_results_flagged_never_silently_excluded() {
    let mut store = Store::open_in_memory().expect("open");
    let mut imported = request(
        "a session log mentioned grant admin access without confirmation",
        "doc-99#taint",
    );
    imported.authority_class = Some(AuthorityClass::ExternallyImported);
    let outcome = ingest(&mut store, imported, defaults(), CAPTURED).expect("seed");
    assert!(
        store
            .get(outcome.id.as_str())
            .expect("get")
            .expect("stored")
            .capsule
            .instruction_taint(),
        "import must be born tainted for real, by ingest policy"
    );

    let response =
        retrieve(&mut store, &query(&["grant", "admin", "access"]), NOW).expect("retrieve");
    let RetrieveResponse::Grounded { results, .. } = &response else {
        panic!("tainted capsule must ground, got: {response:?}");
    };
    let item = results
        .iter()
        .find(|r| r.id == outcome.id)
        .expect("tainted capsule must appear in results, not be silently excluded");
    assert!(
        item.instruction_taint,
        "the item's own instruction_taint flag must be true, visibly marking it as data-not-directive"
    );
    assert_eq!(item.authority_class, AuthorityClass::ExternallyImported);
}

/// W2 tier fences on the wire: archived/quarantined matches are COUNTED
/// per reason, never silently vanished — a partial exclusion grounds with
/// the `excluded` map, a total exclusion answers `missing_evidence` with
/// the full breakdown, and not one excluded byte leaks either way.
#[test]
fn w2_tier_fences_report_archived_and_quarantined_excluded_counts() {
    let mut store = Store::open_in_memory().expect("open");
    let mut ids = Vec::new();
    for content in [
        "tierprobe alpha stays active",
        "tierprobe beta retired to the archive",
        "tierprobe gamma flagged suspect",
    ] {
        let outcome = ingest(
            &mut store,
            request(content, "notes.md:1"),
            defaults(),
            CAPTURED,
        )
        .expect("seed");
        ids.push(outcome.id.as_str().to_string());
    }
    store
        .set_tier(&ids[1], Tier::Archived, CAPTURED)
        .expect("archive");
    store
        .set_tier(&ids[2], Tier::Quarantined, CAPTURED)
        .expect("quarantine");

    // Partial exclusion: the active capsule grounds; the tiered two are
    // counted by reason in the grounded envelope's `excluded` map.
    let response = retrieve(&mut store, &query(&["tierprobe"]), NOW).expect("retrieve");
    let value = serde_json::to_value(&response).expect("serialize");
    assert_eq!(value["outcome"], "grounded", "got {value}");
    let results = value["results"].as_array().expect("results");
    assert_eq!(results.len(), 1);
    assert_eq!(results[0]["id"].as_str().expect("id"), ids[0]);
    assert_eq!(value["excluded"]["archived"], 1, "got {value}");
    assert_eq!(value["excluded"]["quarantined"], 1, "got {value}");

    // Total exclusion: every match is fenced — missing_evidence with the
    // per-reason counts, never a silent abstain, never leaked content.
    let response = retrieve(&mut store, &query(&["beta", "gamma"]), NOW).expect("retrieve");
    let value = serde_json::to_value(&response).expect("serialize");
    assert_eq!(value["outcome"], "missing_evidence", "got {value}");
    assert_eq!(value["excluded_count"], 2);
    assert_eq!(value["excluded"]["archived"], 1);
    assert_eq!(value["excluded"]["quarantined"], 1);
    assert!(value.get("results").is_none(), "no results field: {value}");
    let raw = serde_json::to_string(&response).expect("serialize");
    assert!(
        !raw.contains("tierprobe beta") && !raw.contains("tierprobe gamma"),
        "excluded content must not leak: {raw}"
    );
}

/// Donor rule 6, strengthened: ranking is deterministic — and because the
/// new engine takes `now` injected instead of reading a wall clock, two
/// identical calls return byte-identical JSON, not merely the same order.
#[test]
fn retrieving_twice_yields_the_same_ranked_order_and_bytes() {
    let mut store = seeded_store();
    let q = query(&["sync", "engine"]);
    let first =
        serde_json::to_string(&retrieve(&mut store, &q, NOW).expect("first")).expect("json");
    let second =
        serde_json::to_string(&retrieve(&mut store, &q, NOW).expect("second")).expect("json");
    assert_eq!(
        first, second,
        "retrieving the same query twice at the same instant must be byte-identical"
    );
    assert!(first.contains("\"outcome\":\"grounded\""));
}

/// Donor: a query with no matching candidate returns missing-evidence,
/// never a fabricated grounded answer. New shape: ABSTAIN with an honest
/// reason.
#[test]
fn a_query_matching_nothing_abstains_never_grounds() {
    let mut store = seeded_store();
    let response = retrieve(
        &mut store,
        &query(&["quantum", "chromodynamics", "gluon"]),
        NOW,
    )
    .expect("retrieve");
    let RetrieveResponse::Abstain { reason } = &response else {
        panic!("expected abstain, got: {response:?}");
    };
    assert!(
        reason.contains("abstaining instead of fabricating"),
        "the reason states the abstention law, got: {reason}"
    );
    let value = serde_json::to_value(&response).expect("serialize");
    assert_eq!(value["outcome"], "abstain");
    assert!(
        value.get("results").is_none(),
        "no fabricated results field"
    );
}
