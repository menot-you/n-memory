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
//!   "bears on" pointer for the reader, never a consequence.
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
}

/// One append-only **outcome observation** (u6h). `id` is the store-minted
/// `out-<n>`; `at` is the injected recording instant. `description` and
/// `actor` are mandatory (the caller names WHO observed — there is no default
/// actor); `evidence_ref` and `capsule_id` are optional. This record is
/// ADVISORY substrate: it asserts an outcome was observed, never that it was
/// witnessed/proven, and it never changes any capsule's state (see the module
/// docs — only a `falsifies` edge does).
#[derive(Debug, Clone, PartialEq, Eq)]
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
    /// Injected recording instant (the store reads no clock).
    pub at: OffsetDateTime,
}

impl OutcomeRecord {
    /// Build a validated outcome record. Rejects empty/whitespace `id`,
    /// `description`, or `actor`; the optionals are taken as-is (the store
    /// validates a present `capsule_id`'s existence — a shape this pure
    /// module cannot see). `id` and `at` are injected by the store.
    pub fn new(
        id: String,
        description: String,
        actor: String,
        evidence_ref: Option<String>,
        capsule_id: Option<String>,
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
        Ok(OutcomeRecord {
            id,
            description,
            actor,
            evidence_ref,
            capsule_id,
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
            at(),
        )
        .expect("valid full record");
        assert_eq!(full.id, "out-1");
        assert_eq!(full.evidence_ref.as_deref(), Some("ci://run/4821"));
        assert_eq!(full.capsule_id.as_deref(), Some("cap-7"));

        let bare = OutcomeRecord::new(
            "out-2".into(),
            "observed".into(),
            "actor".into(),
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
            OutcomeRecord::new("".into(), "d".into(), "a".into(), None, None, at())
                .expect_err("empty id"),
            SubstrateError::EmptyField("id")
        );
        assert_eq!(
            OutcomeRecord::new("out-1".into(), "  ".into(), "a".into(), None, None, at())
                .expect_err("blank description"),
            SubstrateError::EmptyField("description")
        );
        assert_eq!(
            OutcomeRecord::new("out-1".into(), "d".into(), "".into(), None, None, at())
                .expect_err("empty actor — there is no default observer"),
            SubstrateError::EmptyField("actor")
        );
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
}
