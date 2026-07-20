//! # Capsule v1 — the frozen atomic memory record (`prd.nMEMORY.2` §4).
//!
//! FROZEN 2026-07-18 (unit s1, the keystone). Any field change after this
//! unit is a migration (`.2` §2.2): new behavior lands as sidecar tables or
//! envelope fields, never as a Capsule field change.
//!
//! Construction is validated: a capsule without provenance **cannot exist** —
//! not by struct literal (fields are private), not via [`Capsule::new`]
//! (returns [`CapsuleError`]), and not via serde (deserialization funnels
//! through the same validation with `#[serde(try_from = "RawCapsule")]`).
//!
//! ## Canonical JSON
//!
//! [`Capsule::to_canonical_json`] is byte-stable: `serde_json` emits struct
//! fields in declaration order, and the declaration order here is the frozen
//! canonical order —
//!
//! ```text
//! content
//! provenance { source, anchor, source_hash }
//! confidence
//! freshness  { valid_from, valid_to }
//! scope      { project_id }
//! authority_class
//! instruction_taint
//! ```
//!
//! No maps, no non-deterministic containers, no float formatting drift
//! (`serde_json` uses shortest-round-trip `f64` output). Timestamps are
//! `time::OffsetDateTime` serialized RFC3339; parse→format is lossless
//! (offset and sub-second precision are preserved), so round-tripping the
//! canonical string yields identical bytes.

use serde::{Deserialize, Serialize};
use time::OffsetDateTime;

/// Where a capsule's content came from, and the proof it has not drifted.
///
/// MANDATORY on every capsule (`.2` §4: no capsule without it). All three
/// fields must be non-empty; [`Capsule::new`] rejects anything less.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Provenance {
    /// Origin system/document of the content (e.g. `"session:2026-07-18"`,
    /// `"PLAN.md"`, `"doc-1361"`).
    pub source: String,
    /// Precise anchor inside the source: `path:line`, `doc-<id>`, `&<id>`,
    /// PR number, commit SHA. Anchor VALIDATION policy lands in ingest (s3).
    pub anchor: String,
    /// SHA-256 hex of the anchored source material at capture time (see
    /// [`sha256_hex`]). Which bytes get hashed is ingest policy (s3).
    pub source_hash: String,
}

/// Calibrated confidence in `0.0..=1.0`, validated at construction.
///
/// Serializes as a plain JSON number; deserialization funnels through
/// [`Confidence::new`] (`#[serde(try_from = "f64")]`), so out-of-range or
/// NaN input is rejected on the wire too.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
#[serde(try_from = "f64", into = "f64")]
pub struct Confidence(f64);

impl Confidence {
    /// Build a confidence value, rejecting NaN and anything outside
    /// `0.0..=1.0`.
    pub fn new(value: f64) -> Result<Self, CapsuleError> {
        if value.is_nan() || !(0.0..=1.0).contains(&value) {
            return Err(CapsuleError::ConfidenceOutOfRange(value));
        }
        Ok(Confidence(value))
    }

    /// The inner `f64`, guaranteed in `0.0..=1.0` and never NaN.
    #[must_use]
    pub fn value(self) -> f64 {
        self.0
    }
}

impl TryFrom<f64> for Confidence {
    type Error = CapsuleError;

    fn try_from(value: f64) -> Result<Self, Self::Error> {
        Confidence::new(value)
    }
}

impl From<Confidence> for f64 {
    fn from(c: Confidence) -> f64 {
        c.0
    }
}

/// Validity window, RFC3339 on the wire.
///
/// `valid_to = None` means "no scheduled expiry". The field itself is
/// REQUIRED on the wire: an omitted `valid_to` is a deserialization error
/// (the `#[serde(with = ...)]` route disables serde's missing-field →
/// `None` special case); only an explicit `null` expresses "no expiry".
/// When present, `valid_to` must be `>= valid_from`; construction rejects
/// inverted windows.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct Freshness {
    /// Instant from which the content is considered valid.
    #[serde(with = "time::serde::rfc3339")]
    pub valid_from: OffsetDateTime,
    /// Expiry instant; explicit `null` = no scheduled expiry.
    #[serde(with = "time::serde::rfc3339::option")]
    pub valid_to: Option<OffsetDateTime>,
}

/// The scope fence: which project a capsule belongs to.
///
/// Mandatory and non-empty at construction — cross-project recall is the
/// defining wrong-context-memory incident.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Scope {
    /// Owning project id (e.g. `"nott"`).
    pub project_id: String,
}

/// Who asserted the capsule's content — the closed authority ladder.
///
/// Closed enum: exactly these four classes, kebab-case on the wire. Imports
/// are born [`AuthorityClass::ExternallyImported`] with `instruction_taint`
/// true (`.2` §4) — enforced by ingest policy (s3), representable here.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum AuthorityClass {
    /// Directly observed by an agent (strongest).
    ObservedFact,
    /// Stated by the owner/user.
    UserStated,
    /// Inferred by an agent.
    AgentInferred,
    /// Imported from outside the system (weakest trust).
    ExternallyImported,
}

impl AuthorityClass {
    /// All authority classes, weakest-trust last.
    pub const ALL: [AuthorityClass; 4] = [
        AuthorityClass::ObservedFact,
        AuthorityClass::UserStated,
        AuthorityClass::AgentInferred,
        AuthorityClass::ExternallyImported,
    ];
}

/// Validation errors rejected at capsule construction (reject-on-ingest).
#[derive(Debug, Clone, PartialEq, thiserror::Error)]
pub enum CapsuleError {
    /// One of `provenance.source` / `anchor` / `source_hash` was empty.
    #[error("capsule rejected: provenance.{0} is empty (no capsule without provenance)")]
    MissingProvenance(&'static str),
    /// `content` was empty.
    #[error("capsule rejected: content is empty")]
    EmptyContent,
    /// `scope.project_id` was empty.
    #[error("capsule rejected: scope.project_id is empty")]
    MissingScope,
    /// Confidence was NaN or outside `0.0..=1.0`.
    #[error("capsule rejected: confidence {0} outside 0.0..=1.0")]
    ConfidenceOutOfRange(f64),
    /// `valid_to` precedes `valid_from`. The message echoes both
    /// instants in RFC3339 (w2-fix) — the ONE format the fields accept,
    /// so an echoed value pastes straight back into a param.
    #[error(
        "capsule rejected: valid_to {} precedes valid_from {}",
        rfc3339_echo(valid_to),
        rfc3339_echo(valid_from)
    )]
    InvertedFreshnessWindow {
        /// Start of the rejected window.
        valid_from: OffsetDateTime,
        /// End of the rejected window (earlier than the start).
        valid_to: OffsetDateTime,
    },
    /// Canonical-JSON serialization failed (e.g. a timestamp outside the
    /// RFC3339-representable year range 0..=9999).
    #[error("capsule canonical-json serialization failed: {0}")]
    Serialize(String),
}

/// RFC3339 rendering for error messages — the one timestamp format the
/// wire accepts, so an echoed value can be pasted straight back into a
/// param (w2-fix: `OffsetDateTime`'s `Display` echoed a format the API
/// rejects). An instant outside the RFC3339 year range degrades to the
/// raw `Display` instead of failing inside an error path.
fn rfc3339_echo(instant: &OffsetDateTime) -> String {
    instant
        .format(&time::format_description::well_known::Rfc3339)
        .unwrap_or_else(|_| instant.to_string())
}

/// Serde-facing raw shape; every deserialization funnels through
/// `TryFrom<RawCapsule> for Capsule`, so serde input obeys the same
/// validation as [`Capsule::new`]. Field order here IS the canonical
/// JSON byte order — do not reorder.
#[derive(Debug, Clone, Serialize, Deserialize)]
struct RawCapsule {
    content: String,
    provenance: Provenance,
    confidence: Confidence,
    freshness: Freshness,
    scope: Scope,
    authority_class: AuthorityClass,
    instruction_taint: bool,
}

/// The atomic memory object (Capsule v1, frozen). Fields are private: the
/// only ways to obtain a `Capsule` are [`Capsule::new`] and serde
/// deserialization, both validated — a capsule without provenance MUST NOT
/// construct, and cannot.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(try_from = "RawCapsule", into = "RawCapsule")]
pub struct Capsule {
    content: String,
    provenance: Provenance,
    confidence: Confidence,
    freshness: Freshness,
    scope: Scope,
    authority_class: AuthorityClass,
    instruction_taint: bool,
}

impl Capsule {
    /// Build a validated capsule. Rejects empty provenance fields, empty
    /// content, empty scope, and inverted freshness windows (out-of-range
    /// confidence is unrepresentable: [`Confidence`] validates itself).
    pub fn new(
        content: String,
        provenance: Provenance,
        confidence: Confidence,
        freshness: Freshness,
        scope: Scope,
        authority_class: AuthorityClass,
        instruction_taint: bool,
    ) -> Result<Self, CapsuleError> {
        if provenance.source.trim().is_empty() {
            return Err(CapsuleError::MissingProvenance("source"));
        }
        if provenance.anchor.trim().is_empty() {
            return Err(CapsuleError::MissingProvenance("anchor"));
        }
        if provenance.source_hash.trim().is_empty() {
            return Err(CapsuleError::MissingProvenance("source_hash"));
        }
        if content.trim().is_empty() {
            return Err(CapsuleError::EmptyContent);
        }
        if scope.project_id.trim().is_empty() {
            return Err(CapsuleError::MissingScope);
        }
        if let Some(valid_to) = freshness.valid_to
            && valid_to < freshness.valid_from
        {
            return Err(CapsuleError::InvertedFreshnessWindow {
                valid_from: freshness.valid_from,
                valid_to,
            });
        }
        Ok(Capsule {
            content,
            provenance,
            confidence,
            freshness,
            scope,
            authority_class,
            instruction_taint,
        })
    }

    /// The remembered text.
    #[must_use]
    pub fn content(&self) -> &str {
        &self.content
    }

    /// Origin + anchor + source hash (always present by construction).
    #[must_use]
    pub fn provenance(&self) -> &Provenance {
        &self.provenance
    }

    /// Calibrated confidence in `0.0..=1.0`.
    #[must_use]
    pub fn confidence(&self) -> Confidence {
        self.confidence
    }

    /// Validity window.
    #[must_use]
    pub fn freshness(&self) -> Freshness {
        self.freshness
    }

    /// Scope fence.
    #[must_use]
    pub fn scope(&self) -> &Scope {
        &self.scope
    }

    /// Authority ladder class.
    #[must_use]
    pub fn authority_class(&self) -> AuthorityClass {
        self.authority_class
    }

    /// Whether the content is directive-shaped and may only ever ground as
    /// quoted/cited DATA, never occupy a directive role.
    #[must_use]
    pub fn instruction_taint(&self) -> bool {
        self.instruction_taint
    }

    /// Canonical JSON — the byte-stable stored form (see module docs for
    /// the frozen field order). Same capsule value → same bytes, always;
    /// round-tripping through [`Capsule`] and back yields identical bytes.
    ///
    /// Errors only if a timestamp cannot be RFC3339-formatted (year outside
    /// 0..=9999) — surfaced as [`CapsuleError::Serialize`], never a panic.
    pub fn to_canonical_json(&self) -> Result<String, CapsuleError> {
        serde_json::to_string(self).map_err(|e| CapsuleError::Serialize(e.to_string()))
    }
}

impl TryFrom<RawCapsule> for Capsule {
    type Error = CapsuleError;

    fn try_from(raw: RawCapsule) -> Result<Self, Self::Error> {
        Capsule::new(
            raw.content,
            raw.provenance,
            raw.confidence,
            raw.freshness,
            raw.scope,
            raw.authority_class,
            raw.instruction_taint,
        )
    }
}

impl From<Capsule> for RawCapsule {
    fn from(c: Capsule) -> RawCapsule {
        RawCapsule {
            content: c.content,
            provenance: c.provenance,
            confidence: c.confidence,
            freshness: c.freshness,
            scope: c.scope,
            authority_class: c.authority_class,
            instruction_taint: c.instruction_taint,
        }
    }
}

/// SHA-256 of `bytes` as lowercase hex — the `provenance.source_hash`
/// mechanism. The POLICY of which bytes get hashed (canonical content
/// bytes) is the ingest unit's (s3); this helper supplies only the digest.
#[must_use]
pub fn sha256_hex(bytes: &[u8]) -> String {
    use sha2::{Digest, Sha256};
    hex::encode(Sha256::digest(bytes))
}

#[cfg(test)]
mod tests {
    #![allow(
        clippy::unwrap_used,
        clippy::expect_used,
        reason = "tests use unwrap/expect so fixture failures fail at the assertion site"
    )]

    use super::*;
    use time::macros::datetime;

    const CONTENT: &str = "nott monorepo lives at /nott/monorepo";

    fn provenance() -> Provenance {
        Provenance {
            source: "session:2026-07-18".to_string(),
            anchor: "PLAN.md:53".to_string(),
            source_hash: sha256_hex(CONTENT.as_bytes()),
        }
    }

    fn capsule() -> Capsule {
        Capsule::new(
            CONTENT.to_string(),
            provenance(),
            Confidence::new(0.9).unwrap(),
            Freshness {
                valid_from: datetime!(2026-07-18 12:30:45 UTC),
                valid_to: None,
            },
            Scope {
                project_id: "nmemory".to_string(),
            },
            AuthorityClass::UserStated,
            false,
        )
        .unwrap()
    }

    #[test]
    fn capsule_with_sec4_fields_constructs() {
        let c = capsule();
        assert_eq!(c.content(), CONTENT);
        assert_eq!(c.provenance().source, "session:2026-07-18");
        assert_eq!(c.provenance().anchor, "PLAN.md:53");
        assert_eq!(c.provenance().source_hash, sha256_hex(CONTENT.as_bytes()));
        assert!((c.confidence().value() - 0.9).abs() < f64::EPSILON);
        assert_eq!(c.freshness().valid_from, datetime!(2026-07-18 12:30:45 UTC));
        assert_eq!(c.freshness().valid_to, None);
        assert_eq!(c.scope().project_id, "nmemory");
        assert_eq!(c.authority_class(), AuthorityClass::UserStated);
        assert!(!c.instruction_taint());
    }

    #[test]
    fn capsule_without_provenance_does_not_construct() {
        for (field, prov) in [
            (
                "source",
                Provenance {
                    source: "  ".to_string(),
                    ..provenance()
                },
            ),
            (
                "anchor",
                Provenance {
                    anchor: String::new(),
                    ..provenance()
                },
            ),
            (
                "source_hash",
                Provenance {
                    source_hash: String::new(),
                    ..provenance()
                },
            ),
        ] {
            let err = Capsule::new(
                CONTENT.to_string(),
                prov,
                Confidence::new(0.9).unwrap(),
                Freshness {
                    valid_from: datetime!(2026-07-18 12:30:45 UTC),
                    valid_to: None,
                },
                Scope {
                    project_id: "nmemory".to_string(),
                },
                AuthorityClass::UserStated,
                false,
            )
            .unwrap_err();
            assert_eq!(err, CapsuleError::MissingProvenance(field));
        }
    }

    #[test]
    fn json_without_provenance_fails_to_deserialize() {
        let json = r#"{
            "content": "x",
            "confidence": 0.5,
            "freshness": {"valid_from": "2026-07-18T12:30:45Z", "valid_to": null},
            "scope": {"project_id": "nmemory"},
            "authority_class": "user-stated",
            "instruction_taint": false
        }"#;
        let err = serde_json::from_str::<Capsule>(json).unwrap_err();
        assert!(
            err.to_string().contains("provenance"),
            "error must name the missing provenance, got: {err}"
        );
    }

    #[test]
    fn confidence_out_of_range_rejected() {
        assert!(matches!(
            Confidence::new(-0.1),
            Err(CapsuleError::ConfidenceOutOfRange(_))
        ));
        assert!(matches!(
            Confidence::new(1.5),
            Err(CapsuleError::ConfidenceOutOfRange(_))
        ));
        assert!(matches!(
            Confidence::new(f64::NAN),
            Err(CapsuleError::ConfidenceOutOfRange(_))
        ));
        assert!(Confidence::new(0.0).is_ok());
        assert!(Confidence::new(1.0).is_ok());

        // The same rejection holds on the wire.
        let json = capsule().to_canonical_json().unwrap();
        let bad = json.replace("\"confidence\":0.9", "\"confidence\":1.5");
        assert_ne!(json, bad, "fixture must actually flip the confidence");
        let err = serde_json::from_str::<Capsule>(&bad).unwrap_err();
        assert!(
            err.to_string().contains("confidence"),
            "error must name confidence, got: {err}"
        );
    }

    #[test]
    fn inverted_freshness_window_rejected() {
        let err = Capsule::new(
            CONTENT.to_string(),
            provenance(),
            Confidence::new(0.9).unwrap(),
            Freshness {
                valid_from: datetime!(2026-07-18 12:30:45 UTC),
                valid_to: Some(datetime!(2026-07-17 00:00:00 UTC)),
            },
            Scope {
                project_id: "nmemory".to_string(),
            },
            AuthorityClass::UserStated,
            false,
        )
        .unwrap_err();
        assert!(matches!(err, CapsuleError::InvertedFreshnessWindow { .. }));
        // w2-fix: both instants echo in RFC3339 — paste-back-able, never
        // the `time` crate's Display format.
        let message = err.to_string();
        assert!(
            message.contains("valid_to 2026-07-17T00:00:00Z")
                && message.contains("valid_from 2026-07-18T12:30:45Z"),
            "RFC3339 echo, got: {message}"
        );
    }

    #[test]
    fn empty_content_and_empty_scope_rejected() {
        let err = Capsule::new(
            String::new(),
            provenance(),
            Confidence::new(0.9).unwrap(),
            Freshness {
                valid_from: datetime!(2026-07-18 12:30:45 UTC),
                valid_to: None,
            },
            Scope {
                project_id: "nmemory".to_string(),
            },
            AuthorityClass::UserStated,
            false,
        )
        .unwrap_err();
        assert_eq!(err, CapsuleError::EmptyContent);

        let err = Capsule::new(
            CONTENT.to_string(),
            provenance(),
            Confidence::new(0.9).unwrap(),
            Freshness {
                valid_from: datetime!(2026-07-18 12:30:45 UTC),
                valid_to: None,
            },
            Scope {
                project_id: " ".to_string(),
            },
            AuthorityClass::UserStated,
            false,
        )
        .unwrap_err();
        assert_eq!(err, CapsuleError::MissingScope);
    }

    #[test]
    fn valid_to_is_required_on_the_wire() {
        // Omitted valid_to: hard error — only an explicit null means
        // "no scheduled expiry".
        let missing = r#"{"valid_from": "2026-07-18T12:30:45Z"}"#;
        assert!(serde_json::from_str::<Freshness>(missing).is_err());

        let explicit_null = r#"{"valid_from": "2026-07-18T12:30:45Z", "valid_to": null}"#;
        let f = serde_json::from_str::<Freshness>(explicit_null).unwrap();
        assert_eq!(f.valid_to, None);
    }

    #[test]
    fn canonical_json_round_trip_byte_stable() {
        let c = capsule();
        let first = c.to_canonical_json().unwrap();

        // Golden bytes: the frozen field order, RFC3339 timestamps, plain
        // f64 confidence. This pins Capsule v1's wire shape.
        let expected = format!(
            "{{\"content\":\"{CONTENT}\",\
             \"provenance\":{{\"source\":\"session:2026-07-18\",\"anchor\":\"PLAN.md:53\",\
             \"source_hash\":\"{hash}\"}},\
             \"confidence\":0.9,\
             \"freshness\":{{\"valid_from\":\"2026-07-18T12:30:45Z\",\"valid_to\":null}},\
             \"scope\":{{\"project_id\":\"nmemory\"}},\
             \"authority_class\":\"user-stated\",\
             \"instruction_taint\":false}}",
            hash = sha256_hex(CONTENT.as_bytes()),
        );
        assert_eq!(first, expected);

        // Round-trip: parse back (validated) and re-serialize — bytes
        // must be identical.
        let back: Capsule = serde_json::from_str(&first).unwrap();
        assert_eq!(back, c);
        let second = back.to_canonical_json().unwrap();
        assert_eq!(first.as_bytes(), second.as_bytes());

        // A bounded window round-trips byte-stably too.
        let bounded = Capsule::new(
            CONTENT.to_string(),
            provenance(),
            Confidence::new(0.25).unwrap(),
            Freshness {
                valid_from: datetime!(2026-07-18 12:30:45 UTC),
                valid_to: Some(datetime!(2026-12-31 23:59:59 UTC)),
            },
            Scope {
                project_id: "nmemory".to_string(),
            },
            AuthorityClass::ExternallyImported,
            true,
        )
        .unwrap();
        let b1 = bounded.to_canonical_json().unwrap();
        let b2 = serde_json::from_str::<Capsule>(&b1)
            .unwrap()
            .to_canonical_json()
            .unwrap();
        assert_eq!(b1.as_bytes(), b2.as_bytes());
    }

    #[test]
    fn authority_class_wire_names_are_kebab() {
        let expected = [
            "\"observed-fact\"",
            "\"user-stated\"",
            "\"agent-inferred\"",
            "\"externally-imported\"",
        ];
        for (class, wire) in AuthorityClass::ALL.iter().zip(expected) {
            assert_eq!(serde_json::to_string(class).unwrap(), wire);
        }
        // Closed enum: an unknown class does not deserialize.
        assert!(serde_json::from_str::<AuthorityClass>("\"made-up\"").is_err());
    }

    #[test]
    fn sha256_hex_known_vector() {
        assert_eq!(
            sha256_hex(b""),
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        );
    }
}
