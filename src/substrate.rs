//! # Substrate — advisory sidecar records: outcome observations (u6h) +
//! preference evidence (u6i).
//!
//! Two APPEND-ONLY sidecar record types, both PURE — no store dependency, no
//! clock, no randomness: `id` and `at` are injected by the store at the
//! boundary, exactly like [`crate::relation::RelationRecord`]. Construction is
//! validated (private-by-convention `new`), and reads re-validate on the way
//! out, so a row that cannot be built by hand cannot be smuggled in off disk.
//!
//! ## The u6h/u6i rung ceiling — ADVISORY SUBSTRATE ONLY
//!
//! - An [`OutcomeRecord`] is an **observation record**: a caller-attested note
//!   that some outcome was *observed*. It is NEVER a witnessed close, and
//!   nothing in nmemory treats it as proven — a witnessed close needs the
//!   kernel (`consequence_service`), which does not exist in this capability.
//!   An outcome NEVER flips a capsule's recall eligibility by itself: only an
//!   explicit `falsifies` edge ([`crate::relation::RelationKind::Falsifies`])
//!   excludes a capsule from recall. The optional `capsule_id` is a soft
//!   "bears on" pointer for the reader, never a consequence. A scored outcome
//!   carries a grounded recall receipt and a `0.0..=1.0` usefulness score;
//!   the store may use that pair to update advisory ranking weights, never
//!   eligibility.
//! - A [`PreferenceRecord`] is ONE **pairwise** preference-evidence datum:
//!   `preferred_id` was chosen over `rejected_id` in some `context`. Pairwise
//!   ONLY — no score, no ranking, no aggregation, no training. It is evidence
//!   substrate for a FUTURE owner-chosen mechanism; nothing consumes it yet.
//!
//! Both types are store-native value objects (like [`crate::store`]'s
//! `RelationRecord`): plain fields the store reads/writes as columns, no serde
//! (the surface maps them to its own wire structs). Timestamps are RFC3339 at
//! the store boundary; here `at` is an already-parsed [`OffsetDateTime`].

use time::OffsetDateTime;

/// Typed rejections at substrate-record construction. A mandatory text field
/// was empty (or whitespace-only) — the same fail-closed shape the store's
/// audit/alias validation uses, kept in this pure module so construction can
/// be unit-tested without a store.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum SubstrateError {
    /// A mandatory field (`id`, `description`/`context`, `actor`, or a
    /// preference endpoint id) was empty or whitespace-only.
    #[error("substrate record rejected: {0} is empty")]
    EmptyField(&'static str),
    /// A scored outcome did not carry `receipt_id` and `score` together, or
    /// its score was not finite and inside the closed `0.0..=1.0` range.
    #[error("substrate outcome scoring rejected: {0}")]
    InvalidOutcomeScoring(&'static str),
}

/// One append-only **outcome observation** (u6h). `id` is the store-minted
/// `out-<n>`; `at` is the injected recording instant. `description` and
/// `actor` are mandatory (the caller names WHO observed — there is no default
/// actor); `evidence_ref` and `capsule_id` are optional. `receipt_id` and
/// `score` are optional only as a PAIR: together they rate the capsules a
/// grounded recall returned. This record is
/// ADVISORY substrate: it asserts an outcome was observed, never that it was
/// witnessed/proven, and it never changes any capsule's recall eligibility
/// (see the module docs — only a `falsifies` edge does).
#[derive(Debug, Clone, PartialEq)]
pub struct OutcomeRecord {
    /// Store-minted id (`out-<n>`, 1-based append sequence).
    pub id: String,
    /// The observation, verbatim from the caller (non-empty).
    pub description: String,
    /// Who observed it (non-empty; the caller names the observer — no
    /// default).
    pub actor: String,
    /// Optional free-text pointer to the evidence (a path / url / id).
    pub evidence_ref: Option<String>,
    /// Optional id of the claim capsule this outcome bears on (`cap-<n>`).
    /// A soft "bears on" pointer for the reader — it has ZERO effect on the
    /// capsule's recall eligibility (only a `falsifies` edge fences recall).
    pub capsule_id: Option<String>,
    /// Grounded recall receipt whose returned capsules this observation rates.
    /// Present exactly when [`Self::score`] is present.
    pub receipt_id: Option<String>,
    /// Usefulness score in the closed `0.0..=1.0` range. Present exactly when
    /// [`Self::receipt_id`] is present.
    pub score: Option<f64>,
    /// Injected recording instant (the store reads no clock).
    pub at: OffsetDateTime,
}

impl OutcomeRecord {
    /// Build a validated outcome record. Rejects empty/whitespace `id`,
    /// `description`, or `actor`; validates that `receipt_id` and `score`
    /// appear together and that the score is finite and inside `0.0..=1.0`.
    /// The store validates a present `capsule_id`'s existence and resolves a
    /// receipt — shapes this pure module cannot see. `id` and `at` are
    /// injected by the store.
    #[allow(
        clippy::too_many_arguments,
        reason = "the validated record constructor mirrors the eight persisted outcome columns"
    )]
    pub fn new(
        id: String,
        description: String,
        actor: String,
        evidence_ref: Option<String>,
        capsule_id: Option<String>,
        receipt_id: Option<String>,
        score: Option<f64>,
        at: OffsetDateTime,
    ) -> Result<Self, SubstrateError> {
        if id.trim().is_empty() {
            return Err(SubstrateError::EmptyField("id"));
        }
        if description.trim().is_empty() {
            return Err(SubstrateError::EmptyField("description"));
        }
        if actor.trim().is_empty() {
            return Err(SubstrateError::EmptyField("actor"));
        }
        match (&receipt_id, score) {
            (None, None) => {}
            (Some(receipt_id), Some(score)) => {
                if receipt_id.trim().is_empty() {
                    return Err(SubstrateError::InvalidOutcomeScoring(
                        "receipt_id must be non-empty",
                    ));
                }
                if !score.is_finite() || !(0.0..=1.0).contains(&score) {
                    return Err(SubstrateError::InvalidOutcomeScoring(
                        "score must be finite and within 0.0..=1.0",
                    ));
                }
            }
            _ => {
                return Err(SubstrateError::InvalidOutcomeScoring(
                    "receipt_id and score must be present together",
                ));
            }
        }
        Ok(OutcomeRecord {
            id,
            description,
            actor,
            evidence_ref,
            capsule_id,
            receipt_id,
            score,
            at,
        })
    }
}

/// One append-only **pairwise preference-evidence** datum (u6i). `id` is the
/// store-minted `pref-<n>`; `at` is the injected recording instant. All four
/// content fields are mandatory: `preferred_id` was chosen over `rejected_id`
/// in `context`, as observed by `actor`. Pairwise ONLY — no score, no
/// aggregation; the store validates that both ids name stored capsules.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PreferenceRecord {
    /// Store-minted id (`pref-<n>`, 1-based append sequence).
    pub id: String,
    /// The preferred capsule id (`cap-<n>`).
    pub preferred_id: String,
    /// The rejected capsule id (`cap-<n>`).
    pub rejected_id: String,
    /// What the pair was about (non-empty free text).
    pub context: String,
    /// Who expressed the preference (non-empty).
    pub actor: String,
    /// Injected recording instant (the store reads no clock).
    pub at: OffsetDateTime,
}

impl PreferenceRecord {
    /// Build a validated preference record. Rejects empty/whitespace `id`,
    /// `preferred_id`, `rejected_id`, `context`, or `actor`. Endpoint
    /// EXISTENCE (both ids name stored capsules) and pair-distinctness are
    /// the store/surface's checks — this pure module validates shape only.
    /// `id` and `at` are injected by the store.
    pub fn new(
        id: String,
        preferred_id: String,
        rejected_id: String,
        context: String,
        actor: String,
        at: OffsetDateTime,
    ) -> Result<Self, SubstrateError> {
        if id.trim().is_empty() {
            return Err(SubstrateError::EmptyField("id"));
        }
        if preferred_id.trim().is_empty() {
            return Err(SubstrateError::EmptyField("preferred_id"));
        }
        if rejected_id.trim().is_empty() {
            return Err(SubstrateError::EmptyField("rejected_id"));
        }
        if context.trim().is_empty() {
            return Err(SubstrateError::EmptyField("context"));
        }
        if actor.trim().is_empty() {
            return Err(SubstrateError::EmptyField("actor"));
        }
        Ok(PreferenceRecord {
            id,
            preferred_id,
            rejected_id,
            context,
            actor,
            at,
        })
    }
}

#[cfg(test)]
mod tests {
    #![allow(
        clippy::unwrap_used,
        clippy::expect_used,
        reason = "tests use unwrap/expect so fixture failures fail at the assertion site"
    )]

    use time::macros::datetime;

    use super::*;

    fn at() -> OffsetDateTime {
        datetime!(2026-07-19 12:00 UTC)
    }

    #[test]
    fn outcome_new_accepts_a_full_record_and_bare_mandatory_fields() {
        let full = OutcomeRecord::new(
            "out-1".into(),
            "recall regressed after the pin bump".into(),
            "session:2026-07-19".into(),
            Some("ci://run/4821".into()),
            Some("cap-7".into()),
            Some("rcpt-3".into()),
            Some(0.75),
            at(),
        )
        .expect("valid full record");
        assert_eq!(full.id, "out-1");
        assert_eq!(full.evidence_ref.as_deref(), Some("ci://run/4821"));
        assert_eq!(full.capsule_id.as_deref(), Some("cap-7"));
        assert_eq!(full.receipt_id.as_deref(), Some("rcpt-3"));
        assert_eq!(full.score, Some(0.75));

        let bare = OutcomeRecord::new(
            "out-2".into(),
            "observed".into(),
            "actor".into(),
            None,
            None,
            None,
            None,
            at(),
        )
        .expect("optionals may be absent");
        assert_eq!(bare.evidence_ref, None);
        assert_eq!(bare.capsule_id, None);
    }

    #[test]
    fn outcome_new_rejects_each_empty_mandatory_field() {
        assert_eq!(
            OutcomeRecord::new(
                "".into(),
                "d".into(),
                "a".into(),
                None,
                None,
                None,
                None,
                at(),
            )
            .expect_err("empty id"),
            SubstrateError::EmptyField("id")
        );
        assert_eq!(
            OutcomeRecord::new(
                "out-1".into(),
                "  ".into(),
                "a".into(),
                None,
                None,
                None,
                None,
                at(),
            )
            .expect_err("blank description"),
            SubstrateError::EmptyField("description")
        );
        assert_eq!(
            OutcomeRecord::new(
                "out-1".into(),
                "d".into(),
                "".into(),
                None,
                None,
                None,
                None,
                at(),
            )
            .expect_err("empty actor — there is no default observer"),
            SubstrateError::EmptyField("actor")
        );
    }

    #[test]
    fn outcome_new_rejects_unpaired_or_invalid_scoring() {
        for (receipt_id, score, message) in [
            (Some("rcpt-1".into()), None, "receipt without score"),
            (None, Some(0.5), "score without receipt"),
            (Some("  ".into()), Some(0.5), "blank receipt"),
            (Some("rcpt-1".into()), Some(-0.01), "negative score"),
            (Some("rcpt-1".into()), Some(1.01), "score above one"),
            (Some("rcpt-1".into()), Some(f64::NAN), "non-finite score"),
        ] {
            assert!(matches!(
                OutcomeRecord::new(
                    "out-1".into(),
                    "observed".into(),
                    "actor".into(),
                    None,
                    None,
                    receipt_id,
                    score,
                    at(),
                )
                .expect_err(message),
                SubstrateError::InvalidOutcomeScoring(_)
            ));
        }
    }

    #[test]
    fn preference_new_accepts_a_pair_and_rejects_each_empty_field() {
        let ok = PreferenceRecord::new(
            "pref-1".into(),
            "cap-3".into(),
            "cap-4".into(),
            "which recall ranking key".into(),
            "session:2026-07-19".into(),
            at(),
        )
        .expect("valid pair");
        assert_eq!(ok.preferred_id, "cap-3");
        assert_eq!(ok.rejected_id, "cap-4");

        for (bad, field) in [
            (
                PreferenceRecord::new(
                    String::new(),
                    "cap-3".into(),
                    "cap-4".into(),
                    "c".into(),
                    "a".into(),
                    at(),
                ),
                "id",
            ),
            (
                PreferenceRecord::new(
                    "pref-1".into(),
                    " ".into(),
                    "cap-4".into(),
                    "c".into(),
                    "a".into(),
                    at(),
                ),
                "preferred_id",
            ),
            (
                PreferenceRecord::new(
                    "pref-1".into(),
                    "cap-3".into(),
                    String::new(),
                    "c".into(),
                    "a".into(),
                    at(),
                ),
                "rejected_id",
            ),
            (
                PreferenceRecord::new(
                    "pref-1".into(),
                    "cap-3".into(),
                    "cap-4".into(),
                    "".into(),
                    "a".into(),
                    at(),
                ),
                "context",
            ),
            (
                PreferenceRecord::new(
                    "pref-1".into(),
                    "cap-3".into(),
                    "cap-4".into(),
                    "c".into(),
                    "   ".into(),
                    at(),
                ),
                "actor",
            ),
        ] {
            assert_eq!(
                bad.expect_err("empty mandatory field must be rejected"),
                SubstrateError::EmptyField(field)
            );
        }
    }

    // Trim semantics are load-bearing: a mandatory field that is non-space
    // whitespace ONLY (tab, newline, carriage return) is still "empty" and
    // must be rejected. This pins `.trim()` — a regression to a literal
    // `== ""` check would let these through and fail this test. (substrate
    // is already at full line coverage; this hardens the contract.)
    #[test]
    fn non_space_whitespace_only_fields_are_rejected() {
        assert_eq!(
            OutcomeRecord::new(
                "out-1".into(),
                "observed".into(),
                "\t\n".into(),
                None,
                None,
                None,
                None,
                at(),
            )
            .expect_err("tab/newline actor is empty after trim"),
            SubstrateError::EmptyField("actor")
        );
        assert_eq!(
            PreferenceRecord::new(
                "pref-1".into(),
                "cap-3".into(),
                "cap-4".into(),
                "\r\n".into(),
                "actor".into(),
                at(),
            )
            .expect_err("carriage-return/newline context is empty after trim"),
            SubstrateError::EmptyField("context")
        );
    }
}
