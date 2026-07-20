//! # Conformance — Capsule construction + fixture round-trips (unit h2).
//!
//! Ported donor behavior (zero authority, reference only): donor B's
//! `mcps/memory-contract/tests/capsule_fixtures.rs` @6d495898, re-authored
//! against the new crate's Capsule v1 (`src/capsule.rs`, RFC3339 freshness
//! instead of the donor's unix seconds). The donor's `schemars` schema test
//! is replaced by a serialized-field-set pin: the new Capsule does not
//! derive `JsonSchema`, so the wire shape is asserted on serialized bytes.
//!
//! The donor's in-code `fixtures::representative_set()` is re-authored as
//! `tests/fixtures/representative_capsules.json` — loading it IS a
//! conformance check (every entry funnels through the validated serde
//! path), and unlike the donor set every fixture obeys the `.2` §4 ingest
//! law (externally-imported ⇒ tainted).

#![allow(
    clippy::unwrap_used,
    clippy::expect_used,
    reason = "tests use unwrap/expect so fixture failures fail at the assertion site"
)]

use std::collections::HashSet;

use nmemory::capsule::{
    AuthorityClass, Capsule, CapsuleError, Confidence, Freshness, Provenance, Scope, sha256_hex,
};
use time::macros::datetime;

/// The re-authored representative fixture pack (donor: >=5 capsules
/// varying the contract axes). Parsing goes through the Capsule's
/// validated deserialization — an invalid fixture cannot load.
const FIXTURE_JSON: &str = include_str!("fixtures/representative_capsules.json");

fn fixture_set() -> Vec<Capsule> {
    let set: Vec<Capsule> =
        serde_json::from_str(FIXTURE_JSON).expect("fixture pack parses as Vec<Capsule>");
    assert!(set.len() >= 5, "hard gate requires >=5 fixture capsules");
    set
}

fn valid_parts() -> (Provenance, Confidence, Freshness, Scope) {
    (
        Provenance {
            source: "doc-1361".to_string(),
            anchor: "doc-1361#invariants".to_string(),
            source_hash: sha256_hex(b"conformance"),
        },
        Confidence::new(0.9).unwrap(),
        Freshness {
            valid_from: datetime!(2026-07-07 00:00:00 UTC),
            valid_to: None,
        },
        Scope {
            project_id: "nott".to_string(),
        },
    )
}

#[test]
fn capsule_without_provenance_errors_at_construction() {
    let (_, confidence, freshness, scope) = valid_parts();
    for (empty_field, provenance) in [
        (
            "source",
            Provenance {
                source: String::new(),
                anchor: "a:1".to_string(),
                source_hash: "aa".to_string(),
            },
        ),
        (
            "anchor",
            Provenance {
                source: "doc-1361".to_string(),
                anchor: "   ".to_string(),
                source_hash: "aa".to_string(),
            },
        ),
        (
            "source_hash",
            Provenance {
                source: "doc-1361".to_string(),
                anchor: "a:1".to_string(),
                source_hash: String::new(),
            },
        ),
    ] {
        let err = Capsule::new(
            "content".to_string(),
            provenance,
            confidence,
            freshness,
            scope.clone(),
            AuthorityClass::ObservedFact,
            false,
        )
        .expect_err("provenance-less capsule must be rejected");
        assert_eq!(err, CapsuleError::MissingProvenance(empty_field));
    }
}

#[test]
fn capsule_without_provenance_is_rejected_by_serde_too() {
    // Missing provenance object entirely: serde must fail (no default).
    let missing_field = r#"{
        "content": "x",
        "confidence": 0.5,
        "freshness": {"valid_from": "2026-07-07T00:00:00Z", "valid_to": null},
        "scope": {"project_id": "nott"},
        "authority_class": "observed-fact",
        "instruction_taint": false
    }"#;
    assert!(serde_json::from_str::<Capsule>(missing_field).is_err());

    // Present but empty provenance strings: must funnel through the same
    // construction validation (try_from), not slip past it.
    let empty_provenance = r#"{
        "content": "x",
        "provenance": {"source": "", "anchor": "a:1", "source_hash": "h"},
        "confidence": 0.5,
        "freshness": {"valid_from": "2026-07-07T00:00:00Z", "valid_to": null},
        "scope": {"project_id": "nott"},
        "authority_class": "observed-fact",
        "instruction_taint": false
    }"#;
    let err = serde_json::from_str::<Capsule>(empty_provenance)
        .expect_err("empty provenance must be rejected on deserialization");
    assert!(
        err.to_string().contains("provenance.source"),
        "error names the empty field: {err}"
    );
}

#[test]
fn invalid_confidence_and_inverted_window_are_rejected() {
    let (provenance, _, _, scope) = valid_parts();
    assert!(matches!(
        Confidence::new(1.5),
        Err(CapsuleError::ConfidenceOutOfRange(_))
    ));
    assert!(matches!(
        Confidence::new(f64::NAN),
        Err(CapsuleError::ConfidenceOutOfRange(_))
    ));

    let valid_from = datetime!(2026-07-07 00:00:00 UTC);
    let valid_to = datetime!(2026-07-06 23:59:59 UTC);
    let err = Capsule::new(
        "content".to_string(),
        provenance,
        Confidence::new(0.5).unwrap(),
        Freshness {
            valid_from,
            valid_to: Some(valid_to),
        },
        scope,
        AuthorityClass::AgentInferred,
        false,
    )
    .expect_err("inverted window must be rejected");
    assert_eq!(
        err,
        CapsuleError::InvertedFreshnessWindow {
            valid_from,
            valid_to
        }
    );
}

#[test]
fn fixture_set_round_trips_byte_stable() {
    for capsule in &fixture_set() {
        let first = capsule.to_canonical_json().expect("serializes");
        let parsed: Capsule = serde_json::from_str(&first).expect("deserializes");
        let second = parsed.to_canonical_json().expect("re-serializes");
        assert_eq!(
            first.as_bytes(),
            second.as_bytes(),
            "round-trip must be byte-stable"
        );
        assert_eq!(&parsed, capsule);
    }
}

#[test]
fn fixture_set_varies_the_contract_axes() {
    let set = fixture_set();
    let authority_classes: HashSet<String> = set
        .iter()
        .map(|c| format!("{:?}", c.authority_class()))
        .collect();
    assert!(
        authority_classes.len() >= 3,
        "fixtures must vary authority_class"
    );
    assert!(
        set.iter().any(|c| c.instruction_taint()),
        "at least one tainted fixture"
    );
    assert!(
        set.iter().any(|c| !c.instruction_taint()),
        "at least one untainted fixture"
    );
    assert!(
        set.iter().any(|c| c.freshness().valid_to.is_some())
            && set.iter().any(|c| c.freshness().valid_to.is_none()),
        "fixtures must vary freshness shape"
    );
    // New-crate additions to the donor's axis check: the pack obeys the
    // ingest laws it will be replayed under —
    // 1. every fixture hash IS the sha256 of its content bytes (the s3
    //    source_hash policy), so store replays exercise real hashes;
    for capsule in &set {
        assert_eq!(
            capsule.provenance().source_hash,
            sha256_hex(capsule.content().as_bytes()),
            "fixture source_hash must be the sha256 of the content bytes"
        );
    }
    // 2. imports are born tainted (`.2` §4) — the donor set carried an
    //    untainted import; the re-authored pack must not.
    for capsule in &set {
        if capsule.authority_class() == AuthorityClass::ExternallyImported {
            assert!(
                capsule.instruction_taint(),
                "externally-imported fixture must be tainted"
            );
        }
    }
}

#[test]
fn freshness_missing_valid_to_is_rejected_but_explicit_null_is_accepted() {
    // Omitted valid_to: hard error — the field must be PRESENT on the
    // wire, never optional-and-unchecked.
    let missing_valid_to = r#"{
        "content": "x",
        "provenance": {"source": "doc-1361", "anchor": "a:1", "source_hash": "h"},
        "confidence": 0.5,
        "freshness": {"valid_from": "2026-07-07T00:00:00Z"},
        "scope": {"project_id": "nott"},
        "authority_class": "observed-fact",
        "instruction_taint": false
    }"#;
    let err = serde_json::from_str::<Capsule>(missing_valid_to)
        .expect_err("omitted valid_to must be rejected");
    assert!(
        err.to_string().contains("valid_to"),
        "error names the field: {err}"
    );

    // Explicit null: accepted, means "no scheduled expiry".
    let explicit_null = r#"{
        "content": "x",
        "provenance": {"source": "doc-1361", "anchor": "a:1", "source_hash": "h"},
        "confidence": 0.5,
        "freshness": {"valid_from": "2026-07-07T00:00:00Z", "valid_to": null},
        "scope": {"project_id": "nott"},
        "authority_class": "observed-fact",
        "instruction_taint": false
    }"#;
    let capsule: Capsule = serde_json::from_str(explicit_null).expect("explicit null accepted");
    assert_eq!(capsule.freshness().valid_to, None);
}

#[test]
fn wire_shape_is_exactly_the_frozen_v1_field_set() {
    // Donor pinned the schema via schemars; the new Capsule derives no
    // JsonSchema, so the pin is on serialized bytes: exactly the seven
    // frozen fields, in the frozen canonical order.
    let capsule = fixture_set().remove(0);
    let canonical = capsule.to_canonical_json().unwrap();

    // Field SET: exactly the seven frozen top-level fields (serde_json's
    // Value sorts keys, so the set check uses sorted expectations).
    let value: serde_json::Value = serde_json::from_str(&canonical).unwrap();
    let mut keys: Vec<&str> = value
        .as_object()
        .expect("capsule serializes as an object")
        .keys()
        .map(String::as_str)
        .collect();
    keys.sort_unstable();
    assert_eq!(
        keys,
        vec![
            "authority_class",
            "confidence",
            "content",
            "freshness",
            "instruction_taint",
            "provenance",
            "scope",
        ]
    );
    let mut provenance_keys: Vec<&str> = value["provenance"]
        .as_object()
        .expect("provenance is an object")
        .keys()
        .map(String::as_str)
        .collect();
    provenance_keys.sort_unstable();
    assert_eq!(provenance_keys, vec!["anchor", "source", "source_hash"]);

    // Field ORDER: pinned on the canonical bytes themselves — declaration
    // order is the frozen canonical order.
    let positions: Vec<usize> = [
        "\"content\":",
        "\"provenance\":",
        "\"confidence\":",
        "\"freshness\":",
        "\"scope\":",
        "\"authority_class\":",
        "\"instruction_taint\":",
    ]
    .iter()
    .map(|field| {
        canonical
            .find(field)
            .unwrap_or_else(|| panic!("canonical JSON missing {field}"))
    })
    .collect();
    assert!(
        positions.windows(2).all(|w| w[0] < w[1]),
        "canonical field order drifted: {positions:?} in {canonical}"
    );
}

#[test]
fn authority_class_wire_names_are_kebab_case() {
    let expected = [
        (AuthorityClass::ObservedFact, "\"observed-fact\""),
        (AuthorityClass::UserStated, "\"user-stated\""),
        (AuthorityClass::AgentInferred, "\"agent-inferred\""),
        (
            AuthorityClass::ExternallyImported,
            "\"externally-imported\"",
        ),
    ];
    for (class, wire) in expected {
        assert_eq!(serde_json::to_string(&class).unwrap(), wire);
        let back: AuthorityClass = serde_json::from_str(wire).unwrap();
        assert_eq!(back, class);
    }
    // Closed enum: an unknown class does not deserialize.
    assert!(serde_json::from_str::<AuthorityClass>("\"made-up\"").is_err());
}
