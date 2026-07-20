//! # Store-merge — the PURE functional core of offline-first reconcile (u1).
//!
//! Owner-ratified feature "local store + remote mirror, reconcile later". This
//! module is the walking skeleton: the deterministic, hermetic function that
//! computes how one store's core rows (INCOMING) fold into another's (LOCAL).
//! It reads NO store, NO clock, NO randomness — exactly the `export` /
//! `visual` gold-bar idiom (the caller passes already-read rows; identical
//! inputs yield an identical plan). The imperative shell that actually reads
//! the two stores and writes the plan is a LATER unit; this is the functional
//! core it will call, and it cannot name any I/O.
//!
//! ## What it computes
//!
//! [`plan_merge`] takes the core rows of both sides —
//! `Vec<StoredCapsule>` / `Vec<RelationRecord>` / `Vec<TombstoneRecord>` each —
//! and returns a [`MergePlan`]: the capsules/relations/tombstones to ADD to
//! LOCAL, plus the incoming-id -> local-id remap. LOCAL already holds its own
//! rows; the plan is the delta.
//!
//! ## Rules (the spec; the tests below materialize each one)
//!
//! 1. **Identity by content hash.** Two capsules are the SAME iff their
//!    `provenance.source_hash` matches — the exact key the store already
//!    dedups on (`capsules.source_hash` UNIQUE, [`crate::store::Store::append`]'s
//!    idempotency backstop). An incoming capsule whose hash already exists in
//!    LOCAL COLLAPSES onto LOCAL's id and contributes no new capsule.
//! 2. **Id remap.** A genuinely-new incoming capsule is minted a fresh id
//!    after LOCAL's sequence ceiling, in incoming-sequence order — the store's
//!    own `cap-<seq>` discipline ([`crate::store::CapsuleId`]). The remap
//!    covers BOTH collapsed and newly-minted ids.
//! 3. **Relations.** Every incoming edge is rewritten through the remap, then
//!    UNIONed with LOCAL and deduped by `(kind, from, to)`. An edge whose
//!    endpoint does not resolve (dangling) is DROPPED, never an error.
//! 4. **Tombstones — forget wins.** An incoming tombstone whose id resolves
//!    through the remap to a LOCAL capsule tombstones that capsule in the plan
//!    (unless LOCAL already tombstoned it): a forgotten capsule contributes no
//!    content. A tombstone whose id does not resolve is dropped, like a
//!    dangling edge.
//! 5. **Determinism.** Pure and total: no clock, no randomness, every output
//!    re-sorted internally, so input order never changes the bytes and two
//!    runs on the same inputs are identical. The sort discipline mirrors
//!    [`crate::export`] exactly (`cap-<n>` numeric then lexical; relation kind
//!    rank then endpoints then instant).
//!
//! ## Boundaries of this pure unit (resolved by the LATER imperative shell)
//!
//! - A [`MergePlan`] carries [`PlannedCapsule`] (a `String` id + payload), not
//!   [`StoredCapsule`]: [`crate::store::CapsuleId`] mints ONLY inside the store
//!   module, so the plan states the id the shell must assign by appending the
//!   new capsules in plan order.
//! - Cross-store forget propagation is limited by the row shapes: a
//!   [`TombstoneRecord`] carries a keyed, non-portable `content_hmac` (never a
//!   `source_hash`) and a forgotten capsule is absent from the live vec, so an
//!   incoming tombstone is content-addressable ONLY through the id-remap. A
//!   planned tombstone carries the incoming record's fields with `capsule_id`
//!   rewritten to LOCAL; the shell re-derives the keyed hmac against LOCAL's
//!   content and key on apply.
//! - Resurrecting content that LOCAL previously forgot cannot be detected here
//!   (a tombstoned LOCAL capsule's `source_hash` is not among these inputs);
//!   the store's `DuplicateSourceHash` append backstop is the real guard.

use std::cmp::Ordering;
use std::collections::{BTreeMap, HashMap, HashSet};

use time::OffsetDateTime;

use crate::capsule::Capsule;
use crate::store::{RelationKind, RelationRecord, StoredCapsule, TombstoneRecord};

/// A capsule the plan will ADD to LOCAL. Not a [`StoredCapsule`] because
/// [`crate::store::CapsuleId`] mints only inside the store: `id` is the
/// `cap-<seq>` the imperative shell must assign by appending these in plan
/// order. `created_at` and `session_id` are carried verbatim from the incoming
/// row so the merge injects no clock (the store's determinism law).
#[derive(Debug, Clone, PartialEq)]
pub struct PlannedCapsule {
    /// Planned LOCAL id (`cap-<seq>`, minted after LOCAL's ceiling).
    pub id: String,
    /// The validated capsule, cloned from the incoming row.
    pub capsule: Capsule,
    /// Incoming creation instant, carried through unchanged (no clock read).
    pub created_at: OffsetDateTime,
    /// Incoming session link, carried through unchanged.
    pub session_id: Option<String>,
}

/// The deltas to apply to LOCAL, plus the id remap. Every collection is
/// deterministically sorted; the remap is a `BTreeMap` so its iteration order
/// is stable too.
#[derive(Debug, Clone, PartialEq)]
pub struct MergePlan {
    /// Genuinely-new capsules to append to LOCAL, ascending by planned id.
    pub new_capsules: Vec<PlannedCapsule>,
    /// Incoming edges (remapped, dangling dropped) not already in LOCAL,
    /// deduped by `(kind, from, to)` and sorted in [`crate::export`] order.
    pub new_relations: Vec<RelationRecord>,
    /// Forget-wins tombstones to add to LOCAL, `capsule_id` rewritten to the
    /// resolved LOCAL id, ascending by that id.
    pub new_tombstones: Vec<TombstoneRecord>,
    /// incoming id -> LOCAL id, covering collapsed and newly-minted capsules.
    pub id_remap: BTreeMap<String, String>,
}

/// Compute the merge plan folding INCOMING into LOCAL. Pure and total: same
/// inputs, same plan; no I/O, no clock, no randomness. Dangling edges and
/// unresolvable tombstones are dropped, never errors, so no fallible path
/// exists — the function needs no error type.
#[must_use]
pub fn plan_merge(
    local_capsules: &[StoredCapsule],
    local_relations: &[RelationRecord],
    local_tombstones: &[TombstoneRecord],
    incoming_capsules: &[StoredCapsule],
    incoming_relations: &[RelationRecord],
    incoming_tombstones: &[TombstoneRecord],
) -> MergePlan {
    // 1. Content-hash index of LOCAL live capsules: source_hash -> local id.
    //    Built over a (seq, id) sort so a (degenerate) duplicate hash resolves
    //    first-wins deterministically, independent of input order.
    let mut local_sorted: Vec<&StoredCapsule> = local_capsules.iter().collect();
    local_sorted.sort_by(|a, b| capsule_order(a, b));
    let mut local_by_hash: HashMap<&str, &str> = HashMap::new();
    for stored in &local_sorted {
        local_by_hash
            .entry(stored.capsule.provenance().source_hash.as_str())
            .or_insert(stored.id.as_str());
    }

    // 2. LOCAL id ceiling. A forgotten capsule keeps its row (canonical_json
    //    set NULL) and therefore its seq slot, so it is absent from the live
    //    vec yet still owns its `cap-<n>` id. Fold every `cap-<n>` id LOCAL
    //    references — live seqs, tombstone ids, relation endpoints — so a
    //    minted id can never collide with one LOCAL already holds.
    let ceiling = local_id_ceiling(local_capsules, local_relations, local_tombstones);

    // 3. Remap + new capsules, processing INCOMING in (seq, id) order.
    let mut incoming_sorted: Vec<&StoredCapsule> = incoming_capsules.iter().collect();
    incoming_sorted.sort_by(|a, b| capsule_order(a, b));
    let mut id_remap: BTreeMap<String, String> = BTreeMap::new();
    let mut new_capsules: Vec<PlannedCapsule> = Vec::new();
    let mut minted_by_hash: HashMap<String, String> = HashMap::new();
    let mut next_seq = ceiling;
    for stored in &incoming_sorted {
        let hash = stored.capsule.provenance().source_hash.as_str();
        if let Some(local_id) = local_by_hash.get(hash).copied() {
            // Collapse onto an existing LOCAL capsule.
            id_remap.insert(stored.id.as_str().to_owned(), local_id.to_owned());
            continue;
        }
        if let Some(minted_id) = minted_by_hash.get(hash).cloned() {
            // Collapse onto an already-minted incoming capsule of identical
            // content (idempotency within INCOMING).
            id_remap.insert(stored.id.as_str().to_owned(), minted_id);
            continue;
        }
        next_seq = next_seq.saturating_add(1);
        let new_id = format!("cap-{next_seq}");
        id_remap.insert(stored.id.as_str().to_owned(), new_id.clone());
        minted_by_hash.insert(hash.to_owned(), new_id.clone());
        new_capsules.push(PlannedCapsule {
            id: new_id,
            capsule: stored.capsule.clone(),
            created_at: stored.created_at,
            session_id: stored.session_id.clone(),
        });
    }
    new_capsules.sort_by(|a, b| id_sort_key(&a.id).cmp(&id_sort_key(&b.id)));

    // 4. Relations: rewrite incoming through the remap, drop dangling, union
    //    with LOCAL, dedup by (kind, from, to). One key set seeded with LOCAL's
    //    keys rejects both an edge already in LOCAL and an incoming duplicate.
    let mut relation_keys: HashSet<(RelationKind, String, String)> = local_relations
        .iter()
        .map(|edge| (edge.kind, edge.from_id.clone(), edge.to_id.clone()))
        .collect();
    let mut incoming_edges: Vec<&RelationRecord> = incoming_relations.iter().collect();
    incoming_edges.sort_by(|a, b| relation_order(a, b));
    let mut new_relations: Vec<RelationRecord> = Vec::new();
    for edge in &incoming_edges {
        let (Some(from), Some(to)) = (id_remap.get(&edge.from_id), id_remap.get(&edge.to_id))
        else {
            // Endpoint does not resolve (tombstoned/absent incoming capsule, an
            // outcome `out-<n>`, or truly dangling): drop, never error.
            continue;
        };
        if relation_keys.insert((edge.kind, from.clone(), to.clone())) {
            new_relations.push(RelationRecord {
                kind: edge.kind,
                from_id: from.clone(),
                to_id: to.clone(),
                at: edge.at,
                origin: edge.origin,
            });
        }
    }
    new_relations.sort_by(relation_order);

    // 5. Tombstones — forget wins. Resolve each incoming tombstone id through
    //    the remap; add it for a LOCAL id that is not already tombstoned.
    let local_tombstoned: HashSet<&str> = local_tombstones
        .iter()
        .map(|marker| marker.capsule_id.as_str())
        .collect();
    let mut incoming_markers: Vec<&TombstoneRecord> = incoming_tombstones.iter().collect();
    incoming_markers.sort_by(|a, b| tombstone_order(a, b));
    let mut planned_tombstoned: HashSet<String> = HashSet::new();
    let mut new_tombstones: Vec<TombstoneRecord> = Vec::new();
    for marker in &incoming_markers {
        let Some(local_id) = id_remap.get(&marker.capsule_id) else {
            continue;
        };
        if local_tombstoned.contains(local_id.as_str()) {
            continue;
        }
        if planned_tombstoned.insert(local_id.clone()) {
            new_tombstones.push(TombstoneRecord {
                capsule_id: local_id.clone(),
                mode: marker.mode,
                content_hmac: marker.content_hmac.clone(),
                at: marker.at,
                reason: marker.reason.clone(),
                provenance_source: marker.provenance_source.clone(),
                provenance_anchor: marker.provenance_anchor.clone(),
            });
        }
    }
    new_tombstones.sort_by_key(|marker| owned_id_key(&marker.capsule_id));

    MergePlan {
        new_capsules,
        new_relations,
        new_tombstones,
        id_remap,
    }
}

/// Highest `cap-<n>` sequence LOCAL owns across live capsules, tombstone
/// markers, and relation endpoints — the base a new id is minted after.
fn local_id_ceiling(
    capsules: &[StoredCapsule],
    relations: &[RelationRecord],
    tombstones: &[TombstoneRecord],
) -> i64 {
    let mut ceiling: i64 = 0;
    for stored in capsules {
        ceiling = ceiling.max(stored.seq);
        if let Some(seq) = cap_seq(stored.id.as_str()) {
            ceiling = ceiling.max(seq);
        }
    }
    for marker in tombstones {
        if let Some(seq) = cap_seq(&marker.capsule_id) {
            ceiling = ceiling.max(seq);
        }
    }
    for edge in relations {
        if let Some(seq) = cap_seq(&edge.from_id) {
            ceiling = ceiling.max(seq);
        }
        if let Some(seq) = cap_seq(&edge.to_id) {
            ceiling = ceiling.max(seq);
        }
    }
    ceiling
}

/// The append-sequence a `cap-<seq>` id names, for id-ceiling arithmetic.
/// `None` for any id outside that shape (e.g. an outcome `out-<n>`).
fn cap_seq(id: &str) -> Option<i64> {
    id.strip_prefix("cap-")
        .and_then(|seq| seq.parse::<i64>().ok())
}

/// [`crate::export`]'s capsule id sort key: `cap-<n>` orders numerically, then
/// any non-`cap` id lexically after all of them (`u64::MAX` bucket).
fn id_sort_key(id: &str) -> (u64, &str) {
    let numeric = id
        .strip_prefix("cap-")
        .and_then(|seq| seq.parse::<u64>().ok())
        .unwrap_or(u64::MAX);
    (numeric, id)
}

/// Owned form of [`id_sort_key`] for sorting by an owned id string.
fn owned_id_key(id: &str) -> (u64, String) {
    let (numeric, _) = id_sort_key(id);
    (numeric, id.to_owned())
}

/// Closed relation-kind rank — [`crate::export`]'s contract order.
const fn relation_kind_rank(kind: RelationKind) -> usize {
    match kind {
        RelationKind::Supersedes => 0,
        RelationKind::DerivedFrom => 1,
        RelationKind::Witnesses => 2,
        RelationKind::Blocks => 3,
        RelationKind::Falsifies => 4,
    }
}

/// Total order over capsules by append sequence then id — the deterministic
/// processing order for both sides.
fn capsule_order(a: &StoredCapsule, b: &StoredCapsule) -> Ordering {
    a.seq
        .cmp(&b.seq)
        .then_with(|| a.id.as_str().cmp(b.id.as_str()))
}

/// Total order over relations mirroring [`crate::export`] (kind rank, then
/// endpoints by id key, then instant), with `origin` as a final tiebreak so
/// the pre-dedup "first wins" pick is deterministic.
fn relation_order(a: &RelationRecord, b: &RelationRecord) -> Ordering {
    relation_kind_rank(a.kind)
        .cmp(&relation_kind_rank(b.kind))
        .then_with(|| id_sort_key(&a.from_id).cmp(&id_sort_key(&b.from_id)))
        .then_with(|| id_sort_key(&a.to_id).cmp(&id_sort_key(&b.to_id)))
        .then_with(|| a.at.cmp(&b.at))
        .then_with(|| a.origin.as_str().cmp(b.origin.as_str()))
}

/// Total order over tombstones by capsule id then instant.
fn tombstone_order(a: &TombstoneRecord, b: &TombstoneRecord) -> Ordering {
    owned_id_key(&a.capsule_id)
        .cmp(&owned_id_key(&b.capsule_id))
        .then_with(|| a.at.cmp(&b.at))
}

#[cfg(test)]
mod tests {
    #![allow(
        clippy::unwrap_used,
        clippy::expect_used,
        reason = "tests use unwrap/expect so fixture failures fail at the assertion site"
    )]

    use super::*;
    use crate::capsule::{AuthorityClass, Confidence, Freshness, Provenance, Scope, sha256_hex};
    use crate::store::{CapsuleId, RelationOrigin, TombstoneMode};
    use time::macros::datetime;

    const T0: OffsetDateTime = datetime!(2026-07-18 00:00:00 UTC);

    fn cap_id(id: &str) -> CapsuleId {
        // CapsuleId mints only in the store; tests obtain one through its
        // serde surface (transparent string), same as export's tests.
        serde_json::from_value(serde_json::Value::String(id.to_owned())).unwrap()
    }

    /// A capsule whose IDENTITY is `content` (source_hash = sha256(content)),
    /// so two fixtures collapse iff their content strings match.
    fn capsule(content: &str) -> Capsule {
        Capsule::new(
            content.to_owned(),
            Provenance {
                source: "session:2026-07-18".to_owned(),
                anchor: "notes.md:1".to_owned(),
                source_hash: sha256_hex(content.as_bytes()),
            },
            Confidence::new(0.5).unwrap(),
            Freshness {
                valid_from: T0,
                valid_to: None,
            },
            Scope {
                project_id: "nott".to_owned(),
            },
            AuthorityClass::AgentInferred,
            false,
        )
        .unwrap()
    }

    fn stored(id: &str, seq: i64, content: &str) -> StoredCapsule {
        StoredCapsule {
            id: cap_id(id),
            seq,
            capsule: capsule(content),
            created_at: T0,
            session_id: None,
        }
    }

    fn edge(kind: RelationKind, from: &str, to: &str) -> RelationRecord {
        RelationRecord {
            kind,
            from_id: from.to_owned(),
            to_id: to.to_owned(),
            at: T0,
            origin: RelationOrigin::Manual,
        }
    }

    fn tombstone(id: &str) -> TombstoneRecord {
        TombstoneRecord {
            capsule_id: id.to_owned(),
            mode: TombstoneMode::Purged,
            content_hmac: "hmac-sha256:aa".to_owned(),
            at: T0,
            reason: "forgotten upstream".to_owned(),
            provenance_source: None,
            provenance_anchor: None,
        }
    }

    // 1. Id-collision remap: both sides use cap-1..cap-N with DISTINCT content.
    //    All incoming capsules are preserved with fresh, distinct ids, and an
    //    incoming relation still connects the right (remapped) capsules.
    #[test]
    fn id_collision_remaps_and_preserves_connections() {
        let local = vec![stored("cap-1", 1, "local-A"), stored("cap-2", 2, "local-B")];
        let incoming = vec![
            stored("cap-1", 1, "incoming-C"),
            stored("cap-2", 2, "incoming-D"),
        ];
        // In INCOMING, cap-1 supersedes cap-2.
        let incoming_rels = vec![edge(RelationKind::Supersedes, "cap-1", "cap-2")];

        let plan = plan_merge(&local, &[], &[], &incoming, &incoming_rels, &[]);

        // Both incoming capsules are new (distinct content), minted after
        // LOCAL's ceiling of 2 -> cap-3, cap-4.
        let ids: Vec<&str> = plan.new_capsules.iter().map(|c| c.id.as_str()).collect();
        assert_eq!(ids, ["cap-3", "cap-4"]);
        assert_eq!(plan.new_capsules[0].capsule.content(), "incoming-C");
        assert_eq!(plan.new_capsules[1].capsule.content(), "incoming-D");

        // Remap covers the collision without clobbering LOCAL's ids.
        assert_eq!(
            plan.id_remap.get("cap-1").map(String::as_str),
            Some("cap-3")
        );
        assert_eq!(
            plan.id_remap.get("cap-2").map(String::as_str),
            Some("cap-4")
        );

        // The edge is rewritten to the LOCAL-space ids: cap-3 supersedes cap-4.
        assert_eq!(plan.new_relations.len(), 1);
        // Load-bearing: invert either endpoint to a raw incoming id and this
        // assertion (and the test) fails.
        assert_eq!(plan.new_relations[0].from_id, "cap-3");
        assert_eq!(plan.new_relations[0].to_id, "cap-4");

        // No id appears twice across LOCAL + plan.
        let mut all: Vec<String> = local.iter().map(|c| c.id.as_str().to_owned()).collect();
        all.extend(plan.new_capsules.iter().map(|c| c.id.clone()));
        let distinct: HashSet<&String> = all.iter().collect();
        assert_eq!(all.len(), distinct.len());
    }

    // 2. Content collapse: identical content on both sides yields ONE capsule,
    //    and an incoming relation off the collapsed capsule repoints at LOCAL's
    //    existing id.
    #[test]
    fn content_collapse_dedups_and_repoints_relations() {
        let local = vec![stored("cap-1", 1, "shared"), stored("cap-2", 2, "local-B")];
        let incoming = vec![
            stored("cap-1", 1, "shared"),     // collapses onto local cap-1
            stored("cap-2", 2, "incoming-C"), // new -> cap-3
        ];
        // Incoming: the shared capsule is derived_from the new one.
        let incoming_rels = vec![edge(RelationKind::DerivedFrom, "cap-1", "cap-2")];

        let plan = plan_merge(&local, &[], &[], &incoming, &incoming_rels, &[]);

        // Only the genuinely-new capsule is added (invert to 2 and it fails).
        assert_eq!(plan.new_capsules.len(), 1);
        assert_eq!(plan.new_capsules[0].id, "cap-3");
        assert_eq!(plan.new_capsules[0].capsule.content(), "incoming-C");

        // The shared incoming id collapses onto LOCAL's existing cap-1.
        assert_eq!(
            plan.id_remap.get("cap-1").map(String::as_str),
            Some("cap-1")
        );
        assert_eq!(
            plan.id_remap.get("cap-2").map(String::as_str),
            Some("cap-3")
        );

        // The edge now points off LOCAL's cap-1, not a re-added duplicate.
        assert_eq!(plan.new_relations.len(), 1);
        assert_eq!(plan.new_relations[0].from_id, "cap-1");
        assert_eq!(plan.new_relations[0].to_id, "cap-3");
    }

    // 3. Forget wins: an incoming tombstone resolving (through the remap) to a
    //    capsule LOCAL has LIVE tombstones that LOCAL capsule in the plan.
    #[test]
    fn forget_wins_tombstones_a_live_local_capsule() {
        let local = vec![stored("cap-7", 7, "shared")]; // live
        // INCOMING carries the same content (collapses onto local cap-7) and a
        // tombstone on its own id — the pure fn is defined over arbitrary vecs,
        // and this drives the resolve -> forget-wins path. (Realistic
        // content-addressed forget propagation would need a source_hash on the
        // tombstone; see the module boundary note.)
        let incoming = vec![stored("cap-1", 1, "shared")];
        let incoming_tombs = vec![tombstone("cap-1")];

        let plan = plan_merge(&local, &[], &[], &incoming, &[], &incoming_tombs);

        // Collapsed: no new capsule.
        assert!(plan.new_capsules.is_empty());
        // The tombstone lands on the RESOLVED local id, not the incoming id.
        assert_eq!(plan.new_tombstones.len(), 1);
        assert_eq!(plan.new_tombstones[0].capsule_id, "cap-7");
        assert_eq!(plan.new_tombstones[0].reason, "forgotten upstream");
    }

    // 3b. An incoming tombstone whose id does NOT resolve through the remap is
    //     dropped, and a capsule LOCAL already tombstoned is not re-added.
    #[test]
    fn unresolved_incoming_tombstone_is_dropped() {
        let local = vec![stored("cap-1", 1, "local-A")];
        let incoming = vec![stored("cap-1", 1, "incoming-B")]; // new -> cap-2
        // Tombstone on incoming cap-9 — no such incoming capsule, so it never
        // enters the remap.
        let incoming_tombs = vec![tombstone("cap-9")];

        let plan = plan_merge(&local, &[], &[], &incoming, &[], &incoming_tombs);

        assert!(plan.new_tombstones.is_empty());
        // The INCOMING tombstone's id lives in incoming's id space and does NOT
        // inflate LOCAL's ceiling (only LOCAL rows do; cf. the local-tombstone
        // test). LOCAL's ceiling is 1, so the mint is cap-2 — never cap-9+1.
        assert_eq!(plan.new_capsules[0].id, "cap-2");
    }

    // 4. Relation dedup + dangling drop: an incoming edge already in LOCAL is
    //    not re-added; an incoming edge to an unresolved endpoint is dropped;
    //    a genuinely-new edge is kept.
    #[test]
    fn relations_dedup_against_local_and_drop_dangling() {
        let local = vec![stored("cap-1", 1, "A"), stored("cap-2", 2, "B")];
        let local_rels = vec![edge(RelationKind::Blocks, "cap-1", "cap-2")];
        let incoming = vec![stored("cap-1", 1, "A"), stored("cap-2", 2, "B")]; // both collapse
        let incoming_rels = vec![
            edge(RelationKind::Blocks, "cap-1", "cap-2"), // duplicate of LOCAL -> dropped
            edge(RelationKind::Witnesses, "cap-2", "cap-1"), // new -> kept
            edge(RelationKind::Supersedes, "cap-1", "cap-9"), // cap-9 dangling -> dropped
        ];

        let plan = plan_merge(&local, &local_rels, &[], &incoming, &incoming_rels, &[]);

        // Exactly the one genuinely-new edge survives.
        assert_eq!(plan.new_relations.len(), 1);
        assert_eq!(plan.new_relations[0].kind, RelationKind::Witnesses);
        assert_eq!(plan.new_relations[0].from_id, "cap-2");
        assert_eq!(plan.new_relations[0].to_id, "cap-1");
        // The dangling supersedes edge is absent.
        assert!(
            !plan
                .new_relations
                .iter()
                .any(|edge| edge.kind == RelationKind::Supersedes)
        );
    }

    // 5. New ids never collide with a LOCAL capsule that was forgotten: a
    //    tombstoned LOCAL row keeps its seq slot, so the ceiling must clear it.
    #[test]
    fn minted_ids_clear_local_tombstoned_slots() {
        let local = vec![stored("cap-1", 1, "A")]; // live max seq 1
        let local_tombs = vec![tombstone("cap-5")]; // seq slot 5 still owned
        let incoming = vec![stored("cap-1", 1, "new-content")];

        let plan = plan_merge(&local, &[], &local_tombs, &incoming, &[], &[]);

        // Ceiling folds the tombstoned id (5), so the mint is cap-6, never
        // cap-2 (invert the expectation to "cap-2" and this fails).
        assert_eq!(plan.new_capsules.len(), 1);
        assert_eq!(plan.new_capsules[0].id, "cap-6");
    }

    // 6. Determinism: same inputs -> identical plan, and input ORDER does not
    //    matter (everything is re-sorted internally, mirroring export).
    #[test]
    fn merge_is_deterministic_and_order_independent() {
        let local = vec![stored("cap-1", 1, "shared"), stored("cap-2", 2, "local-B")];
        let incoming = vec![
            stored("cap-1", 1, "shared"),     // collapse
            stored("cap-2", 2, "incoming-C"), // new
            stored("cap-3", 3, "incoming-D"), // new
        ];
        let incoming_rels = vec![
            edge(RelationKind::Witnesses, "cap-2", "cap-1"),
            edge(RelationKind::DerivedFrom, "cap-3", "cap-2"),
        ];
        let incoming_tombs = vec![tombstone("cap-1")];

        let plan_a = plan_merge(&local, &[], &[], &incoming, &incoming_rels, &incoming_tombs);
        let plan_b = plan_merge(&local, &[], &[], &incoming, &incoming_rels, &incoming_tombs);
        assert_eq!(plan_a, plan_b);
        // Byte-identical rendering, not just structural equality.
        assert_eq!(format!("{plan_a:?}"), format!("{plan_b:?}"));

        // Reverse every input slice: the internal sorts must absorb the order.
        let local_rev: Vec<StoredCapsule> = local.iter().rev().cloned().collect();
        let incoming_rev: Vec<StoredCapsule> = incoming.iter().rev().cloned().collect();
        let rels_rev: Vec<RelationRecord> = incoming_rels.iter().rev().cloned().collect();
        let plan_rev = plan_merge(
            &local_rev,
            &[],
            &[],
            &incoming_rev,
            &rels_rev,
            &incoming_tombs,
        );
        assert_eq!(plan_a, plan_rev);
    }
}
