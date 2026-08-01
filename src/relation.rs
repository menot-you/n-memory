//! # Relation — closed capsule-to-capsule edge ontology + pure dag projection.
//!
//! Campaign W1, `.2` §6 u6d (donor B parity: `memory-contract/src/relation.rs`
//! @6d495898, &1532/CAP-02). Two layers, both PURE — no store dependency, no
//! clock, no randomness (`at` is injected by the caller at the boundary):
//!
//! 1. **[`RelationKind`] / [`RelationRecord`]** — the domain vocabulary. A
//!    record is a directed, typed edge `from --kind--> to` between capsule
//!    ids plus the injected `at` instant:
//!
//!    | kind | `from_id` | `to_id` | canonical SSOT analogue |
//!    |---|---|---|---|
//!    | `supersedes` | the newer capsule | the replaced one | replace-over-append chain |
//!    | `derived_from` | the derivative | its origin | story materialized from an epic doc |
//!    | `witnesses` | the evidence capsule | the attested capsule | gate-verdict doc attesting a story |
//!    | `blocks` | the blocker | the blocked | `tasks.blocked_by` / `task_dependencies` edge |
//!    | `falsifies` | the falsifier (an outcome `out-<n>` OR a capsule) | the falsified capsule | an observed outcome contradicting a stored claim |
//!    | `proposes` | the proposing capsule | the target it proposes to replace | a staged, unratified replacement (navigational only) |
//!    | `part_of` | the member capsule | the container epic/task | membership of a capsule in an effort container |
//!    | `grounded_in` | the child task/epic/plan node | its parent epic | the planning-plane mission anchor |
//!    | `about` | any capsule the topic covers | the topic capsule | the topic anchor a recall fence reads |
//!
//!    Wire names are **snake_case** (`supersedes`, `derived_from`,
//!    `witnesses`, `blocks`, `falsifies`, `proposes`, `part_of`,
//!    `grounded_in`, `about`) — chosen (over kebab-case) to byte-match the
//!    donor &1532 contract, the store-side relation-kind CHECK ontology, and
//!    the `memory_relate` tool vocabulary. As built, records cross the layers
//!    BY WIRE NAME (`as_str()` / [`FromStr`]); a server-side parity test pins
//!    all five copies of the closed set (contract module, store enum, tool
//!    param, SQL CHECK, and the wire-name docs) to these exact bytes so none
//!    can drift. The enum is CLOSED: adding a kind is a deliberate, reviewed
//!    ontology change, never an incidental one (u6h added `falsifies`; b2
//!    staged review added `proposes`; effort-lifecycle s1 added `part_of`;
//!    planning-plane s1 added `grounded_in`; topic-anchor s1 added `about`).
//!
//!    `part_of` is PURE MEMBERSHIP — `from` is a member of container `to`,
//!    and like `falsifies` it is NOT a dag input (it joins neither the
//!    blocks universe nor the supersede liveness set); the [`Dag`] projection
//!    below folds `blocks`/`supersedes`/`witnesses` only, so a `part_of`
//!    edge is BYTE-INERT to every ready/blocked/done answer.
//!
//!    `about` is the TOPIC ANCHOR — `from` is about topic node `to`. Like
//!    `part_of` it is NOT a dag input and carries no recall-EXCLUSION effect;
//!    it exists so `memory_retrieve`'s `topic_id` can fence recall to a
//!    topic's members across projects. A topic has NO LIFECYCLE — nothing
//!    opens, closes, or completes it — which is exactly why this is not
//!    `part_of` (whose `to` must be a persisted epic/task container).
//!
//!    `falsifies` is the ONLY kind whose `from_id` may name a non-capsule:
//!    an outcome record id (`out-<n>`, the u6h substrate) may falsify a
//!    capsule, and a capsule may falsify a capsule (the enum is uniform).
//!    The `to_id` is always a capsule. `falsifies` is NOT a dag input (it
//!    joins neither the blocks universe nor the supersede liveness set); it
//!    is a recall-ELIGIBILITY fence handled in [`crate::retrieve`] — a
//!    falsified capsule stops grounding recall while its bytes stay served
//!    by `get`/`list`.
//!
//! 2. **[`Dag`]** — the SSOT `task_dependencies` successor **as a QUERY**
//!    (CAMPAIGN rung: work/docs plane returns as "derived projections
//!    (blocked_by/dag as query), NOT stored state"). [`Dag::project`] folds a
//!    slice of records into `blocked_by` / `ready` / liveness answers:
//!
//!    - **Membership is the blocks subgraph** (w1d stress fix): the node
//!      universe is the endpoints of `blocks` edges only — a capsule that
//!      merely witnesses or derives never shows up as "ready work".
//!    - **Liveness** mirrors the store's `is_superseded` semantics exactly:
//!      an id is dead iff ANY `supersedes` edge names it as `to_id` — or
//!      iff the caller injected it as dead ([`Dag::project_excluding`]:
//!      the tombstone seam — destroyed capsules are dead to the dag).
//!      Supersession is historical fact — a superseder's own later
//!      supersession never resurrects its victim.
//!    - **Dead blockers do not block** and dead ids are never ready. The
//!      ready set is the live ids with zero live blockers — the first layer
//!      of a topological (Kahn) sweep over the live `blocks` subgraph.
//!    - **Witnessed is DONE** (u-r3): a blocks-participant named as the
//!      `to_id` of a `witnesses` edge (the attested side; see the kind
//!      table above) that is not dead is DONE — proof-carrying closure. It
//!      leaves `ready`/`blocked`, joins [`Dag::done`], and STOPS BLOCKING
//!      its dependents exactly like a dead blocker, so a witnessed member
//!      dissolves a live cycle the way a superseded one does (proven in
//!      tests) while a cycle among non-done members still fails closed. It
//!      stays [`Dag::is_live`] — witnessing is NOT supersession: a done
//!      capsule keeps grounding recall (`crate::retrieve` fences none of
//!      it), whereas superseded/tombstoned capsules are dead AND
//!      recall-excluded. DEAD BEATS DONE: a capsule both superseded and
//!      witnessed stays dead, never done. The three exits read side by
//!      side — `witnesses` closes with proof, `supersedes` replaces, forget
//!      destroys.
//!    - **Kind-filtered ready** (w2-kinds): [`Dag::ready_by_kind`] narrows
//!      the ready set to one work-plane [`CandidateKind`] ("which TASKS
//!      can I pick up now?") through a records-provided `id → kind` map —
//!      the caller supplies it (in practice from classification sidecar
//!      records); the projection itself stays pure and store-free, and it
//!      never claims a kind for an id the map does not name.
//!    - **Cycle detection is fail-closed**: a cycle among live `blocks`
//!      edges makes the whole projection return the typed
//!      [`DagCycleError`], carrying one concrete cycle plus the full
//!      entangled set (more cycles may hide behind the reported one).
//!      Relations are append-shaped in the store, so repair is append-only
//!      too: superseding (or forgetting) any cycle member dissolves the
//!      cycle on the next projection (proven in tests).
//!
//! Construction is validated like [`Capsule`](crate::capsule::Capsule):
//! fields are private, [`RelationRecord::new`] rejects empty endpoints and
//! self-relations (no kind is reflexive), and serde deserialization funnels
//! through the same validation via `#[serde(try_from = "RawRelationRecord")]`
//! — an edge that cannot be built by hand cannot be smuggled in over the
//! wire either.

use std::collections::{BTreeMap, BTreeSet};
use std::fmt;
use std::str::FromStr;

use serde::{Deserialize, Serialize};
use time::OffsetDateTime;

use crate::extract::CandidateKind;

/// The nine declared relation kinds. Wire names are the snake_case forms
/// fixed by the donor &1532 contract and mirrored by the store's CHECK
/// ontology; see the module docs for the direction each kind reads in.
/// `falsifies` (u6h) is the recall-eligibility kind — closed like the rest.
/// `proposes` (b2 staged review) is NAVIGATIONAL ONLY: it records that one
/// capsule proposes to replace another, but has NO dag/ready/done effect and
/// NO recall-exclusion effect — the blocks-dag stays `blocks`-only and the
/// grounding fences never read it. On ratification the caller MAY convert a
/// `proposes` edge to `supersedes` explicitly; the machine never does.
/// `part_of` (effort-lifecycle s1) is pure membership — `from` is a member of
/// container `to`; like `falsifies` and `proposes` it is NOT a dag input.
/// `grounded_in` (planning-plane s1) is the mission-anchoring kind — also
/// NOT a dag input; see the [`RelationKind::GroundedIn`] variant doc.
/// `about` (topic-anchor s1) is the topic-anchoring kind — also NOT a dag
/// input; see the [`RelationKind::About`] variant doc.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RelationKind {
    /// `from` replaces `to`. The only kind that KILLS its target: `to` leaves
    /// the dag's live set and stops grounding recall, and no later supersession
    /// of `from` resurrects it — supersession is historical fact.
    Supersedes,
    /// `from` was materialized out of `to`. Provenance only: it records where a
    /// capsule came from and is inert to every ready/blocked/done answer.
    DerivedFrom,
    /// `from` is the evidence attesting `to`. The ATTESTED side (`to`) becomes
    /// done and stops blocking its dependents, while staying live and still
    /// grounding recall — closure with proof, as opposed to replacement.
    Witnesses,
    /// `from` must close before `to` can be ready. The ONLY kind that builds
    /// the dag's node universe, so a capsule reachable by no `blocks` edge is
    /// never reported as work at all.
    Blocks,
    /// `from` contradicts `to`. Uniquely, `from` may name a non-capsule — an
    /// outcome record `out-<n>`. The falsified capsule stops grounding recall
    /// while its bytes stay served by `get`/`list`: a contradiction hides a
    /// claim from answers without destroying the record of it.
    Falsifies,
    /// `from` offers to replace `to`, and nothing acts on it. Navigational
    /// only: no dag effect and no recall exclusion. Ratification is an explicit
    /// caller-side conversion to [`RelationKind::Supersedes`]; the machine
    /// NEVER promotes one on its own.
    Proposes,
    /// `from` is a member of container `to`. Pure membership — byte-inert to
    /// every dag answer, so grouping capsules into an effort can never change
    /// what is ready.
    PartOf,
    /// `from` anchors to its parent epic `to` in the planning plane. Like
    /// [`RelationKind::PartOf`] it is not a dag input; it answers "what mission
    /// is this under", never "can this be picked up".
    GroundedIn,
    /// `from` is ABOUT topic node `to` — the topic anchor. Like
    /// [`RelationKind::PartOf`] it is not a dag input, and it fences nothing
    /// out of recall; it answers "what is this about", so `memory_retrieve`'s
    /// `topic_id` can scope recall to a topic's members ACROSS projects. The
    /// convention (never enforced) is that `to` is a `doc` capsule serving as
    /// the topic node: a topic has no lifecycle to open or close, which is
    /// precisely what separates it from [`RelationKind::PartOf`].
    About,
}

impl RelationKind {
    /// All declared kinds, in contract order.
    pub const ALL: [RelationKind; 9] = [
        RelationKind::Supersedes,
        RelationKind::DerivedFrom,
        RelationKind::Witnesses,
        RelationKind::Blocks,
        RelationKind::Falsifies,
        RelationKind::Proposes,
        RelationKind::PartOf,
        RelationKind::GroundedIn,
        RelationKind::About,
    ];

    /// The closed contract rank of this kind: its position in [`Self::ALL`],
    /// which already declares the contract order. Review found three
    /// hand-written copies of this ordering across the projection surfaces
    /// (export, visual, merge) with nothing holding them together; deriving
    /// rank from the one declared order means no copy can exist to drift.
    /// A test pins this order to the store enum's own `ALL`, wire name by
    /// wire name, so the two vocabularies cannot diverge silently either.
    #[must_use]
    pub fn rank(self) -> usize {
        Self::ALL
            .iter()
            .position(|kind| *kind == self)
            .unwrap_or(usize::MAX)
    }

    /// The wire name, e.g. `"derived_from"` — byte-identical to the serde
    /// form and to the store CHECK strings.
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            RelationKind::Supersedes => "supersedes",
            RelationKind::DerivedFrom => "derived_from",
            RelationKind::Witnesses => "witnesses",
            RelationKind::Blocks => "blocks",
            RelationKind::Falsifies => "falsifies",
            RelationKind::Proposes => "proposes",
            RelationKind::PartOf => "part_of",
            RelationKind::GroundedIn => "grounded_in",
            RelationKind::About => "about",
        }
    }
}

impl fmt::Display for RelationKind {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

impl FromStr for RelationKind {
    type Err = RelationError;

    /// Exact-match parse of a wire name. Strict by design (no trim, no
    /// case-folding): the ontology is closed and the store CHECK strings
    /// are byte-exact.
    fn from_str(s: &str) -> Result<Self, Self::Err> {
        match s {
            "supersedes" => Ok(RelationKind::Supersedes),
            "derived_from" => Ok(RelationKind::DerivedFrom),
            "witnesses" => Ok(RelationKind::Witnesses),
            "blocks" => Ok(RelationKind::Blocks),
            "falsifies" => Ok(RelationKind::Falsifies),
            "proposes" => Ok(RelationKind::Proposes),
            "part_of" => Ok(RelationKind::PartOf),
            "grounded_in" => Ok(RelationKind::GroundedIn),
            "about" => Ok(RelationKind::About),
            other => Err(RelationError::UnknownKind(other.to_owned())),
        }
    }
}

/// Typed rejections at relation construction / kind parse.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum RelationError {
    /// `from_id` or `to_id` was empty (or whitespace-only).
    #[error("relation rejected: {0} is empty")]
    EmptyEndpoint(&'static str),
    /// Both endpoints named the same capsule: no kind is reflexive (a
    /// capsule cannot supersede, derive from, witness, or block itself).
    #[error("relation rejected: self-relation on capsule {0:?}")]
    SelfRelation(String),
    /// A string outside the closed ontology reached [`RelationKind::from_str`].
    #[error(
        "relation rejected: unknown kind {0:?} \
         (closed enum: supersedes, derived_from, witnesses, blocks, falsifies, proposes, part_of, grounded_in, about)"
    )]
    UnknownKind(String),
}

/// Serde-facing raw shape; every deserialization funnels through
/// [`RelationRecord::try_from`] so wire input obeys the same validation as
/// [`RelationRecord::new`].
#[derive(Debug, Clone, Deserialize)]
struct RawRelationRecord {
    kind: RelationKind,
    from_id: String,
    to_id: String,
    #[serde(with = "time::serde::rfc3339")]
    at: OffsetDateTime,
}

/// A directed, typed capsule-to-capsule edge at an injected instant.
///
/// Fields are private: the only ways to obtain a record are
/// [`RelationRecord::new`] and serde deserialization, both validated. The
/// serialized field order is the declaration order — `kind`, `from_id`,
/// `to_id`, `at` — and `at` is RFC3339, matching the crate's canonical-JSON
/// discipline.
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(try_from = "RawRelationRecord")]
pub struct RelationRecord {
    kind: RelationKind,
    from_id: String,
    to_id: String,
    #[serde(with = "time::serde::rfc3339")]
    at: OffsetDateTime,
}

impl RelationRecord {
    /// Build a validated relation record. Rejects empty/whitespace endpoint
    /// ids and self-relations. `at` is injected by the caller — this module
    /// reads no clock.
    pub fn new(
        kind: RelationKind,
        from_id: String,
        to_id: String,
        at: OffsetDateTime,
    ) -> Result<Self, RelationError> {
        if from_id.trim().is_empty() {
            return Err(RelationError::EmptyEndpoint("from_id"));
        }
        if to_id.trim().is_empty() {
            return Err(RelationError::EmptyEndpoint("to_id"));
        }
        if from_id == to_id {
            return Err(RelationError::SelfRelation(from_id));
        }
        Ok(RelationRecord {
            kind,
            from_id,
            to_id,
            at,
        })
    }

    /// The edge's kind. Read it BEFORE either endpoint: `from_id` and `to_id`
    /// mean different things per kind — blocker/blocked, evidence/attested,
    /// newer/replaced — so an accessor pair read without the kind is two ids
    /// with no direction.
    #[must_use]
    pub fn kind(&self) -> RelationKind {
        self.kind
    }

    /// Id of the capsule the edge originates from (the superseder /
    /// derivative / witness / blocker).
    #[must_use]
    pub fn from_id(&self) -> &str {
        &self.from_id
    }

    /// Id of the capsule the edge points to (the superseded / origin /
    /// attested / blocked).
    #[must_use]
    pub fn to_id(&self) -> &str {
        &self.to_id
    }

    /// The injected instant the edge was recorded at.
    #[must_use]
    pub fn at(&self) -> OffsetDateTime {
        self.at
    }
}

impl TryFrom<RawRelationRecord> for RelationRecord {
    type Error = RelationError;

    fn try_from(raw: RawRelationRecord) -> Result<Self, Self::Error> {
        RelationRecord::new(raw.kind, raw.from_id, raw.to_id, raw.at)
    }
}

/// The live `blocks` subgraph contains a cycle, so no topological ready-set
/// exists — the projection fails closed.
///
/// `cycle` is one concrete cycle in forward (`blocks`) direction: each
/// element blocks the next, and the last blocks the first. It is
/// deterministic (rotated so its smallest id comes first) and every element
/// is live. Repair is append-only: supersede any member and re-project. (In
/// a defensive, structurally unreachable fallback the vec instead lists the
/// sorted cycle-entangled leftover set.)
///
/// `entangled` is the FULL sorted set of live ids stuck in or behind SOME
/// cycle (the Kahn leftover) — always a superset of `cycle`. When it is
/// strictly larger, more cycle entanglement remains beyond the one cycle
/// shown: repairing the reported cycle and re-projecting reveals the next.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error(
    "relation dag rejected: live blocks-cycle {cycle:?} \
     ({entangled_len} id(s) cycle-entangled in total; append-only repair: \
     supersede a member and re-project)",
    entangled_len = entangled.len()
)]
pub struct DagCycleError {
    /// One concrete live cycle, forward direction, smallest id first.
    pub cycle: Vec<String>,
    /// Every live id entangled with some cycle (sorted; ⊇ `cycle`).
    pub entangled: Vec<String>,
}

/// Pure dag projection over a set of relation records — the SSOT
/// `task_dependencies` successor as a QUERY (see module docs).
///
/// The node universe is every id appearing as an endpoint of a `blocks`
/// edge — the projection is the BLOCKS-dag, so membership is scoped to
/// blocks participants (w1d stress fix: witnesses/derived_from-only
/// capsules polluted `ready` with nodes that were never work items).
/// `supersedes` edges kill liveness without joining the universe;
/// `witnesses` / `derived_from` edges contribute nothing here. Ids with no
/// blocks edges at all are the caller's to append (they are trivially
/// ready when live).
#[derive(Debug, Clone)]
pub struct Dag {
    universe: BTreeSet<String>,
    dead: BTreeSet<String>,
    /// Witnessed blocks-participants — DONE (u-r3): universe members named
    /// as the `to_id` of a `witnesses` edge and not dead. A done id has
    /// closed with proof; it is neither `ready` nor `blocked`, no longer
    /// blocks its dependents, yet stays [`Dag::is_live`] (recall untouched).
    done: BTreeSet<String>,
    /// target id → its LIVE, NON-DONE blockers (dead and done blockers are
    /// filtered at projection time; targets may be dead — an honest
    /// `blocked_by` answer is still given for them, they are simply never
    /// ready).
    blockers: BTreeMap<String, BTreeSet<String>>,
}

impl Dag {
    /// Fold `edges` into a projection, failing closed with [`DagCycleError`]
    /// when the live `blocks` subgraph is cyclic.
    ///
    /// Deterministic: same edges (any order, duplicates tolerated) → same
    /// projection, same error. No clock, no randomness, no store.
    pub fn project(edges: &[RelationRecord]) -> Result<Dag, DagCycleError> {
        Self::project_excluding(edges, &BTreeSet::new())
    }

    /// [`Dag::project`] with additional DEAD ids injected by the caller —
    /// the tombstone seam: a forgotten (destroyed) capsule is never ready
    /// work and never gates anything, exactly like a superseded one, and a
    /// cycle through it dissolves (forget is thereby a sanctioned dag
    /// repair, alongside supersede). `extra_dead` ids outside the blocks
    /// universe are harmless.
    pub fn project_excluding(
        edges: &[RelationRecord],
        extra_dead: &BTreeSet<String>,
    ) -> Result<Dag, DagCycleError> {
        let mut universe: BTreeSet<String> = BTreeSet::new();
        let mut dead: BTreeSet<String> = extra_dead.clone();
        for e in edges {
            if e.kind == RelationKind::Blocks {
                universe.insert(e.from_id.clone());
                universe.insert(e.to_id.clone());
            }
            if e.kind == RelationKind::Supersedes {
                dead.insert(e.to_id.clone());
            }
        }

        // done: witnessed blocks-participants — proof-carrying closure
        // (u-r3). A `witnesses` edge names its `to_id` as the attested
        // (witnessed) side; a witnessed universe member that is not dead is
        // DONE. DEAD BEATS DONE — a superseded/tombstoned capsule is never
        // advertised as done (it is recall-excluded, a done capsule is not).
        let mut done: BTreeSet<String> = BTreeSet::new();
        for e in edges {
            if e.kind == RelationKind::Witnesses
                && universe.contains(&e.to_id)
                && !dead.contains(&e.to_id)
            {
                done.insert(e.to_id.clone());
            }
        }

        // target → live, non-done blockers. A dead OR done blocker never
        // blocks: a done (witnessed) blocker has closed, so it releases its
        // dependents exactly like a dead one.
        let mut blockers: BTreeMap<String, BTreeSet<String>> = BTreeMap::new();
        for e in edges {
            if e.kind == RelationKind::Blocks
                && !dead.contains(&e.from_id)
                && !done.contains(&e.from_id)
            {
                blockers
                    .entry(e.to_id.clone())
                    .or_default()
                    .insert(e.from_id.clone());
            }
        }

        // Kahn over the ACTIVE blocks subgraph — nodes neither dead nor done.
        // A done node is inactive here exactly like a dead one, so a
        // witnessed cycle member dissolves the cycle the way a superseded one
        // does; a live cycle among non-done members still fails closed.
        let mut indeg: BTreeMap<&str, usize> = universe
            .iter()
            .filter(|id| !dead.contains(*id) && !done.contains(*id))
            .map(|id| {
                (
                    id.as_str(),
                    blockers.get(id.as_str()).map_or(0, BTreeSet::len),
                )
            })
            .collect();
        let mut out: BTreeMap<&str, Vec<&str>> = BTreeMap::new();
        for (target, bs) in &blockers {
            if dead.contains(target) || done.contains(target) {
                continue;
            }
            for b in bs {
                out.entry(b.as_str()).or_default().push(target.as_str());
            }
        }
        let mut queue: BTreeSet<&str> = indeg
            .iter()
            .filter(|(_, d)| **d == 0)
            .map(|(id, _)| *id)
            .collect();
        while let Some(n) = queue.pop_first() {
            if let Some(targets) = out.get(n) {
                for t in targets {
                    if let Some(d) = indeg.get_mut(t)
                        && let Some(nd) = d.checked_sub(1)
                    {
                        *d = nd;
                        if nd == 0 {
                            queue.insert(t);
                        }
                    }
                }
            }
        }
        let leftover: BTreeSet<&str> = indeg
            .iter()
            .filter(|(_, d)| **d > 0)
            .map(|(id, _)| *id)
            .collect();
        if !leftover.is_empty() {
            return Err(DagCycleError {
                cycle: find_cycle(&leftover, &blockers),
                entangled: leftover.iter().map(|s| (*s).to_string()).collect(),
            });
        }

        Ok(Dag {
            universe,
            dead,
            done,
            blockers,
        })
    }

    /// The LIVE blockers of `id`, sorted. Empty for unknown ids and for ids
    /// whose every blocker is dead. Invariant: `ready()` contains a live id
    /// iff its `blocked_by` is empty.
    #[must_use]
    pub fn blocked_by(&self, id: &str) -> Vec<&str> {
        self.blockers
            .get(id)
            .map(|bs| bs.iter().map(String::as_str).collect())
            .unwrap_or_default()
    }

    /// The topological ready-set: live, NON-DONE ids with zero live blockers,
    /// sorted. Dead (superseded/tombstoned) ids are never ready; a witnessed
    /// (done) id has left ready for [`Dag::done`]. So `ready` is exactly
    /// "unblocked work still awaiting proof" — no separate pending list.
    #[must_use]
    pub fn ready(&self) -> Vec<&str> {
        self.universe
            .iter()
            .filter(|id| {
                !self.dead.contains(*id)
                    && !self.done.contains(*id)
                    && self.blockers.get(*id).is_none_or(BTreeSet::is_empty)
            })
            .map(String::as_str)
            .collect()
    }

    /// The DONE set: witnessed blocks-participants — proof-carrying closure
    /// (u-r3), sorted. A done id has left `ready`/`blocked` and no longer
    /// blocks its dependents, yet stays [`Dag::is_live`] so recall is
    /// untouched (unlike a superseded/tombstoned id, which is recall-fenced).
    /// The caller caps this list exactly like [`Dag::ready`].
    #[must_use]
    pub fn done(&self) -> Vec<&str> {
        self.done.iter().map(String::as_str).collect()
    }

    /// Whether `id` is DONE — a witnessed blocks-participant. A done id is
    /// still [`Dag::is_live`] (recall unaffected); it is simply neither
    /// `ready` nor `blocked`, and it no longer gates its dependents.
    #[must_use]
    pub fn is_done(&self, id: &str) -> bool {
        self.done.contains(id)
    }

    /// [`Dag::ready`] narrowed to one [`CandidateKind`] — the w2-kinds
    /// work-plane query ("which TASKS can I pick up now?").
    ///
    /// `kinds` is a records-provided map (capsule id → its classified
    /// kind): in practice the caller folds it from classification sidecar
    /// records, but this module stays pure and store-free — the map is a
    /// parameter, never a lookup. Ids the map does not name are OMITTED
    /// from EVERY kind view: the projection never claims a kind it was not
    /// given (grounded-or-abstain, applied to kinds). Everything [`ready`]
    /// guarantees composes in: dead ids never appear, blocked ids never
    /// appear, output is sorted and deterministic.
    ///
    /// [`ready`]: Dag::ready
    #[must_use]
    pub fn ready_by_kind(
        &self,
        kinds: &BTreeMap<String, CandidateKind>,
        kind: CandidateKind,
    ) -> Vec<&str> {
        self.ready()
            .into_iter()
            .filter(|id| kinds.get(*id) == Some(&kind))
            .collect()
    }

    /// Whether `id` is a known endpoint and not superseded. Mirrors the
    /// store's `is_superseded` semantics: dead iff ANY `supersedes` edge
    /// names it as `to_id`; chains never resurrect. Unknown ids are not
    /// live (the projection cannot vouch for ids it never saw).
    #[must_use]
    pub fn is_live(&self, id: &str) -> bool {
        self.universe.contains(id) && !self.dead.contains(id)
    }
}

/// Cycle detection over the live `grounded_in` (child → parent) subgraph —
/// the mission spine's OWN fail-closed check (planning-plane u2),
/// independent of [`Dag`]: `grounded_in` is proven NOT a dag input (see
/// module docs), so it needs its own Kahn sweep rather than piggy-backing
/// on [`Dag::project`]. `dead` (caller-supplied: tombstoned ∪ superseded
/// ids) is excluded from the graph BEFORE the sweep — a dead endpoint
/// neither anchors nor gates a mission cycle, mirroring
/// [`Dag::project_excluding`]'s tombstone seam: append-only repair
/// (supersede or forget any cycle member) dissolves it on the next check.
///
/// Returns `Ok(())` for an acyclic (including empty) live subgraph; no
/// projection is built — this is a pure yes/no fail-closed gate, callers
/// that need roots/children walk `edges` themselves. On a cycle, reuses
/// the private [`find_cycle`] helper (module-internal only, never made
/// `pub`) so the reported shape is byte-identical to the blocks-dag's:
/// one concrete cycle (forward `grounded_in` direction, smallest id
/// first) plus the FULL entangled set.
pub fn grounded_in_cycle(
    edges: &[RelationRecord],
    dead: &BTreeSet<String>,
) -> Result<(), DagCycleError> {
    // Live grounded_in subgraph only: an edge with EITHER endpoint dead is
    // dropped before the sweep. `in_edges_of` maps target -> live in-edge
    // sources — exactly the shape `find_cycle` expects (the same shape
    // `Dag::project`'s `blockers` map has for the blocks subgraph).
    let mut in_edges_of: BTreeMap<String, BTreeSet<String>> = BTreeMap::new();
    let mut universe: BTreeSet<String> = BTreeSet::new();
    for e in edges {
        if e.kind != RelationKind::GroundedIn {
            continue;
        }
        if dead.contains(&e.from_id) || dead.contains(&e.to_id) {
            continue;
        }
        universe.insert(e.from_id.clone());
        universe.insert(e.to_id.clone());
        in_edges_of
            .entry(e.to_id.clone())
            .or_default()
            .insert(e.from_id.clone());
    }

    let mut indeg: BTreeMap<&str, usize> = universe
        .iter()
        .map(|id| {
            (
                id.as_str(),
                in_edges_of.get(id.as_str()).map_or(0, BTreeSet::len),
            )
        })
        .collect();
    let mut out: BTreeMap<&str, Vec<&str>> = BTreeMap::new();
    for (parent, children) in &in_edges_of {
        for child in children {
            out.entry(child.as_str()).or_default().push(parent.as_str());
        }
    }
    let mut queue: BTreeSet<&str> = indeg
        .iter()
        .filter(|(_, d)| **d == 0)
        .map(|(id, _)| *id)
        .collect();
    while let Some(n) = queue.pop_first() {
        if let Some(targets) = out.get(n) {
            for t in targets {
                if let Some(d) = indeg.get_mut(t)
                    && let Some(nd) = d.checked_sub(1)
                {
                    *d = nd;
                    if nd == 0 {
                        queue.insert(t);
                    }
                }
            }
        }
    }
    let leftover: BTreeSet<&str> = indeg
        .iter()
        .filter(|(_, d)| **d > 0)
        .map(|(id, _)| *id)
        .collect();
    if leftover.is_empty() {
        return Ok(());
    }
    Err(DagCycleError {
        cycle: find_cycle(&leftover, &in_edges_of),
        entangled: leftover.iter().map(|s| (*s).to_string()).collect(),
    })
}

/// Extract one concrete cycle from the Kahn leftover set by walking
/// backward along in-edges (every leftover node keeps at least one live
/// blocker inside the leftover — that is what made it leftover). The walk
/// is bounded by pigeonhole: within `leftover.len() + 1` steps a node must
/// repeat, closing the cycle. Deterministic: smallest start, smallest
/// in-neighbor each step, result rotated smallest-first.
fn find_cycle(
    leftover: &BTreeSet<&str>,
    blockers: &BTreeMap<String, BTreeSet<String>>,
) -> Vec<String> {
    let Some(start) = leftover.first().copied() else {
        return Vec::new();
    };
    let mut path: Vec<&str> = vec![start];
    let mut pos: BTreeMap<&str, usize> = BTreeMap::new();
    pos.insert(start, 0);
    let mut cur = start;
    for _ in 0..=leftover.len() {
        let step = blockers
            .get(cur)
            .and_then(|bs| bs.iter().map(String::as_str).find(|b| leftover.contains(b)));
        let Some(prev) = step else {
            break; // structurally unreachable: leftover nodes keep a leftover blocker
        };
        if let Some(&j) = pos.get(prev) {
            // Backward chain path[j] <- ... <- path[last], closed by the
            // discovered in-edge: prev(=path[j]) blocks cur(=path[last]).
            // Forward (blocks) direction: path[j] -> path[last] ->
            // path[last-1] -> ... -> path[j+1] -> path[j].
            let mut cycle: Vec<String> = Vec::with_capacity(path.len() - j);
            if let Some(anchor) = path.get(j) {
                cycle.push((*anchor).to_string());
            }
            for node in path.iter().skip(j + 1).rev() {
                cycle.push((*node).to_string());
            }
            if let Some(min_idx) = cycle
                .iter()
                .enumerate()
                .min_by(|a, b| a.1.cmp(b.1))
                .map(|(i, _)| i)
            {
                cycle.rotate_left(min_idx);
            }
            return cycle;
        }
        pos.insert(prev, path.len());
        path.push(prev);
        cur = prev;
    }
    // Defensive fallback (structurally unreachable): the sorted entangled set.
    leftover.iter().map(|s| (*s).to_string()).collect()
}

#[cfg(test)]
mod tests {
    #![allow(
        clippy::unwrap_used,
        clippy::expect_used,
        reason = "tests use unwrap/expect so fixture failures fail at the assertion site"
    )]

    use std::collections::BTreeSet;

    use time::macros::datetime;

    use super::*;

    fn at() -> OffsetDateTime {
        datetime!(2026-07-18 12:00 UTC)
    }

    /// The two relation vocabularies (this module's and the store's) each
    /// declare their contract order in their own `ALL`, and `rank()` on both
    /// is DERIVED from that order — no rank table exists to drift. What can
    /// still drift is the two `ALL`s against each other; this pins them,
    /// wire name by wire name, position by position. Extend one vocabulary
    /// without the other — or reorder either — and this goes red naming the
    /// first divergent position.
    #[test]
    fn contract_order_matches_the_store_vocabulary_wire_for_wire() {
        let relation_names: Vec<&str> =
            RelationKind::ALL.iter().map(|kind| kind.as_str()).collect();
        let store_names: Vec<&str> = crate::store::RelationKind::ALL
            .iter()
            .map(|kind| kind.as_str())
            .collect();
        assert_eq!(
            relation_names, store_names,
            "relation::RelationKind::ALL and store::RelationKind::ALL MUST \
             agree on members and order — rank() derives from these"
        );
        for (position, kind) in RelationKind::ALL.iter().enumerate() {
            assert_eq!(kind.rank(), position, "rank IS the declared position");
        }
        for (position, kind) in crate::store::RelationKind::ALL.iter().enumerate() {
            assert_eq!(kind.rank(), position, "rank IS the declared position");
        }
    }

    fn rec(kind: RelationKind, from: &str, to: &str) -> RelationRecord {
        RelationRecord::new(kind, from.to_string(), to.to_string(), at()).expect("valid fixture")
    }

    // ---- closed enum: exhaustive round-trip over the whole domain ----

    #[test]
    fn kind_wire_names_match_the_declared_contract_in_order() {
        let names: Vec<&str> = RelationKind::ALL.iter().map(|k| k.as_str()).collect();
        assert_eq!(
            names,
            vec![
                "supersedes",
                "derived_from",
                "witnesses",
                "blocks",
                "falsifies",
                "proposes",
                "part_of",
                "grounded_in",
                "about"
            ]
        );
    }

    #[test]
    fn every_kind_round_trips_display_fromstr_and_serde() {
        for kind in RelationKind::ALL {
            // Display == as_str == FromStr inverse.
            assert_eq!(kind.to_string(), kind.as_str());
            let parsed: RelationKind = kind.as_str().parse().expect("wire name parses");
            assert_eq!(parsed, kind);
            // serde wire form is exactly the quoted as_str.
            let json = serde_json::to_string(&kind).expect("kind serializes");
            assert_eq!(json, format!("{:?}", kind.as_str()));
            let back: RelationKind = serde_json::from_str(&json).expect("kind deserializes");
            assert_eq!(back, kind);
        }
    }

    #[test]
    fn unknown_kind_is_rejected_typed_on_parse_and_on_the_wire() {
        let err = "depends_on"
            .parse::<RelationKind>()
            .expect_err("closed enum");
        assert_eq!(err, RelationError::UnknownKind("depends_on".to_string()));
        // Strict: no case-folding, no trim.
        assert!("Supersedes".parse::<RelationKind>().is_err());
        assert!(" blocks".parse::<RelationKind>().is_err());
        assert!(serde_json::from_str::<RelationKind>("\"replaces\"").is_err());
    }

    // ---- record: validated construction + serde funnel + canonical order ----

    #[test]
    fn record_serializes_in_canonical_field_order() {
        let r = rec(RelationKind::Blocks, "plan", "ship");
        let json = serde_json::to_string(&r).expect("record serializes");
        assert_eq!(
            json,
            r#"{"kind":"blocks","from_id":"plan","to_id":"ship","at":"2026-07-18T12:00:00Z"}"#
        );
        let back: RelationRecord = serde_json::from_str(&json).expect("record deserializes");
        assert_eq!(back, r);
        let again = serde_json::to_string(&back).expect("re-serializes");
        assert_eq!(json.as_bytes(), again.as_bytes(), "byte-stable round-trip");
    }

    #[test]
    fn empty_endpoints_error_at_construction_and_via_serde() {
        let err = RelationRecord::new(RelationKind::Blocks, String::new(), "b".into(), at())
            .expect_err("empty from must be rejected");
        assert_eq!(err, RelationError::EmptyEndpoint("from_id"));
        let err = RelationRecord::new(RelationKind::Blocks, "a".into(), "   ".into(), at())
            .expect_err("blank to must be rejected");
        assert_eq!(err, RelationError::EmptyEndpoint("to_id"));

        let err = serde_json::from_str::<RelationRecord>(
            r#"{"kind":"blocks","from_id":"","to_id":"b","at":"2026-07-18T12:00:00Z"}"#,
        )
        .expect_err("serde funnels through the same validation");
        assert!(err.to_string().contains("from_id"), "{err}");
    }

    #[test]
    fn self_relation_is_rejected_for_every_kind() {
        for kind in RelationKind::ALL {
            let err = RelationRecord::new(kind, "cap-a".into(), "cap-a".into(), at())
                .expect_err("self-relation must be rejected");
            assert_eq!(err, RelationError::SelfRelation("cap-a".to_string()));
        }
        assert!(
            serde_json::from_str::<RelationRecord>(
                r#"{"kind":"witnesses","from_id":"x","to_id":"x","at":"2026-07-18T12:00:00Z"}"#,
            )
            .is_err(),
            "self-relation must not be smuggled over the wire"
        );
    }

    // ---- dag projection: fixture correctness ----

    /// Diamond: plan blocks {build, design}; both block ship. One duplicate
    /// edge to prove set semantics.
    fn diamond() -> Vec<RelationRecord> {
        vec![
            rec(RelationKind::Blocks, "plan", "build"),
            rec(RelationKind::Blocks, "plan", "design"),
            rec(RelationKind::Blocks, "build", "ship"),
            rec(RelationKind::Blocks, "design", "ship"),
            rec(RelationKind::Blocks, "plan", "build"), // duplicate tolerated
        ]
    }

    #[test]
    fn ready_set_and_blocked_by_on_the_diamond_fixture() {
        let dag = Dag::project(&diamond()).expect("diamond is acyclic");
        assert_eq!(dag.ready(), vec!["plan"]);
        assert_eq!(dag.blocked_by("ship"), vec!["build", "design"]);
        assert_eq!(dag.blocked_by("build"), vec!["plan"]);
        assert_eq!(dag.blocked_by("plan"), Vec::<&str>::new());
        assert_eq!(dag.blocked_by("never-seen"), Vec::<&str>::new());
        assert!(dag.is_live("plan"));
        assert!(
            !dag.is_live("never-seen"),
            "unknown ids are not vouched for"
        );
    }

    #[test]
    fn supersession_changes_the_projection_as_a_query() {
        let mut edges = diamond();
        edges.push(rec(RelationKind::Supersedes, "plan2", "plan"));
        let dag = Dag::project(&edges).expect("still acyclic");
        // plan is dead: no longer ready, no longer blocks. plan2 kills it
        // WITHOUT joining the blocks universe (w1d membership fix): a
        // superseder with no blocks edge of its own is lineage, not work.
        assert!(!dag.is_live("plan"));
        assert!(
            !dag.is_live("plan2"),
            "supersedes-only endpoints stay outside the blocks universe"
        );
        assert_eq!(dag.ready(), vec!["build", "design"]);
        assert_eq!(dag.blocked_by("build"), Vec::<&str>::new());
        assert_eq!(dag.blocked_by("ship"), vec!["build", "design"]);
    }

    #[test]
    fn non_gating_kinds_contribute_nothing_to_the_blocks_universe() {
        // w1d membership fix: witnesses/derived_from endpoints polluted
        // `ready` with nodes that were never work items — now they are
        // simply not members.
        let edges = vec![
            rec(RelationKind::Witnesses, "evidence", "claim"),
            rec(RelationKind::DerivedFrom, "story", "epic"),
        ];
        let dag = Dag::project(&edges).expect("no blocks edges, no cycle");
        assert_eq!(dag.ready(), Vec::<&str>::new());
        assert!(!dag.is_live("evidence"));
        assert_eq!(dag.blocked_by("claim"), Vec::<&str>::new());
    }

    #[test]
    fn falsifies_is_not_a_dag_input_it_neither_gates_nor_kills_liveness() {
        // u6h: `falsifies` is a recall-eligibility kind, NOT a dag input.
        // It must NOT join the blocks universe (like witnesses/derived_from)
        // and — unlike `supersedes` — it must NOT mark its target dead: a
        // falsified capsule is fenced from RECALL (crate::retrieve), never
        // erased from the projection's liveness. Endpoints may be an outcome
        // id (`out-<n>`) or a capsule; the projection ignores the edge either
        // way.
        let edges = vec![
            rec(RelationKind::Blocks, "plan", "ship"),
            rec(RelationKind::Falsifies, "out-1", "plan"),
            rec(RelationKind::Falsifies, "cap-9", "ship"),
        ];
        let dag = Dag::project(&edges).expect("falsifies adds no cycle");
        // Only the blocks endpoints are the universe; the falsifiers are not.
        assert!(!dag.is_live("out-1"), "an outcome id is never a dag node");
        assert!(
            !dag.is_live("cap-9"),
            "a falsifier-only capsule is not a node"
        );
        // Falsified targets stay LIVE in the projection (eligibility ≠ history):
        // `plan` is still ready, `ship` still blocked by `plan`.
        assert!(dag.is_live("plan"));
        assert!(dag.is_live("ship"));
        assert_eq!(dag.ready(), vec!["plan"]);
        assert_eq!(dag.blocked_by("ship"), vec!["plan"]);
    }

    #[test]
    fn grounded_in_is_not_a_dag_input_it_neither_gates_nor_kills_liveness() {
        // planning-plane u1: `grounded_in` is the mission-anchoring kind, NOT
        // a dag input. It must NOT join the blocks universe (like
        // witnesses/derived_from/falsifies) and it must NOT mark either
        // endpoint dead — the child (`from_id`) and the parent epic (`to_id`)
        // both stay outside the blocks universe entirely.
        let edges = vec![
            rec(RelationKind::Blocks, "plan", "ship"),
            rec(RelationKind::GroundedIn, "cap-10", "cap-431"),
        ];
        let dag = Dag::project(&edges).expect("grounded_in adds no cycle");
        // Neither grounded_in endpoint is a dag node at all.
        assert!(!dag.is_live("cap-10"), "child endpoint is not a dag node");
        assert!(!dag.is_live("cap-431"), "parent endpoint is not a dag node");
        // The unrelated blocks edge is untouched.
        assert_eq!(dag.ready(), vec!["plan"]);
        assert_eq!(dag.blocked_by("ship"), vec!["plan"]);
    }

    #[test]
    fn about_is_not_a_dag_input_even_between_two_blocks_participants() {
        // topic-anchor s1: `about` is the topic anchor, NOT a dag input. The
        // hard case is an `about` edge whose BOTH endpoints already sit in the
        // blocks universe — if the projection read the kind at all, the edge
        // would gate or kill one of them. It must be BYTE-INERT: the same
        // ready/blocked/done/liveness answers with and without it.
        let base = vec![
            rec(RelationKind::Blocks, "plan", "ship"),
            rec(RelationKind::Blocks, "ship", "announce"),
        ];
        let mut with_about = base.clone();
        // Between two blocks participants, in BOTH directions, plus one edge
        // to a topic node that is not a blocks participant at all.
        with_about.push(rec(RelationKind::About, "ship", "plan"));
        with_about.push(rec(RelationKind::About, "plan", "announce"));
        with_about.push(rec(RelationKind::About, "ship", "topic-doc"));

        let before = Dag::project(&base).expect("acyclic");
        let after = Dag::project(&with_about).expect("about adds no cycle");
        assert_eq!(after.ready(), before.ready(), "ready is untouched");
        assert_eq!(after.done(), before.done(), "done is untouched");
        for id in ["plan", "ship", "announce"] {
            assert_eq!(
                after.blocked_by(id),
                before.blocked_by(id),
                "blocked_by({id}) is untouched by an about edge"
            );
            assert_eq!(
                after.is_live(id),
                before.is_live(id),
                "liveness of {id} is untouched by an about edge"
            );
        }
        // The topic node joins nothing: an about-only endpoint is never a node.
        assert!(
            !after.is_live("topic-doc"),
            "an about-only topic node is not a dag node"
        );
    }

    // ---- dag projection: witnessed → done (u-r3) ----

    #[test]
    fn witnessing_a_blocker_marks_it_done_and_frees_its_dependent() {
        // PRD R3 acceptance at the projection layer: A blocks B, both live →
        // B blocked, A ready, none done.
        let mut edges = vec![rec(RelationKind::Blocks, "task-a", "task-b")];
        let before = Dag::project(&edges).expect("acyclic");
        assert_eq!(before.ready(), vec!["task-a"]);
        assert_eq!(before.blocked_by("task-b"), vec!["task-a"]);
        assert!(before.done().is_empty(), "nothing witnessed yet");

        // Witness A: evidence attests A (a `witnesses` edge names A as the
        // attested to_id). A DERIVES done from that edge — no state enum.
        edges.push(rec(RelationKind::Witnesses, "evidence-e", "task-a"));
        let after = Dag::project(&edges).expect("witnesses adds no cycle");

        // A left ready and joined done; it stopped blocking B, so B is ready.
        assert_eq!(after.done(), vec!["task-a"], "A closed with proof");
        assert!(after.is_done("task-a"));
        assert_eq!(after.ready(), vec!["task-b"], "B freed to ready");
        assert!(!after.is_done("task-b"));
        assert_eq!(
            after.blocked_by("task-b"),
            Vec::<&str>::new(),
            "A no longer blocks B"
        );

        // Witnessing is NOT supersession: A stays live, so recall is
        // untouched (crate::retrieve fences none of it).
        assert!(
            after.is_live("task-a"),
            "witnessed != dead — A stays recallable-live"
        );
        // The evidence capsule merely witnesses — not a blocks participant,
        // so never a dag node and never itself done.
        assert!(!after.is_live("evidence-e"));
        assert!(!after.is_done("evidence-e"));
    }

    #[test]
    fn dead_beats_done_a_superseded_witnessed_capsule_stays_dead() {
        // A capsule BOTH superseded and witnessed is dead, never done:
        // supersede is recall-exclusion, done is not — advertising a
        // recall-fenced capsule as a recallable done task would be a lie.
        let edges = vec![
            rec(RelationKind::Blocks, "task-a", "task-b"),
            rec(RelationKind::Witnesses, "evidence-e", "task-a"),
            rec(RelationKind::Supersedes, "task-a2", "task-a"),
        ];
        let dag = Dag::project(&edges).expect("acyclic");
        assert!(!dag.is_live("task-a"), "superseded → dead");
        assert!(!dag.is_done("task-a"), "dead beats done");
        assert!(dag.done().is_empty(), "no done id");
        // A is dead either way, so B is freed (dead blockers do not block).
        assert_eq!(dag.ready(), vec!["task-b"]);
    }

    // ---- dag projection: kind-filtered ready (w2-kinds) ----

    #[test]
    fn ready_by_kind_filters_ready_with_a_records_provided_map() {
        // Work-plane fixture: an epic with two derived tasks, a spec doc
        // and an unclassified id gating the second task.
        let edges = vec![
            rec(RelationKind::DerivedFrom, "t-write", "e-epic"),
            rec(RelationKind::DerivedFrom, "t-review", "e-epic"),
            rec(RelationKind::Blocks, "t-write", "t-review"),
            rec(RelationKind::Blocks, "d-spec", "t-review"),
            rec(RelationKind::Blocks, "u-mystery", "t-review"),
        ];
        let kinds: BTreeMap<String, CandidateKind> = [
            ("t-write".to_string(), CandidateKind::Task),
            ("t-review".to_string(), CandidateKind::Task),
            ("d-spec".to_string(), CandidateKind::Doc),
            ("e-epic".to_string(), CandidateKind::Epic),
            // "u-mystery" deliberately has NO classification record.
        ]
        .into_iter()
        .collect();
        let dag = Dag::project(&edges).expect("acyclic fixture");
        assert_eq!(dag.ready(), vec!["d-spec", "t-write", "u-mystery"]);

        // The task view answers "which TASKS can I pick up now".
        assert_eq!(
            dag.ready_by_kind(&kinds, CandidateKind::Task),
            vec!["t-write"],
            "only the ready id KNOWN to be a task"
        );
        // The doc view sees the gating spec doc.
        assert_eq!(
            dag.ready_by_kind(&kinds, CandidateKind::Doc),
            vec!["d-spec"]
        );
        // The epic is derived_from-lineage only — outside the blocks
        // universe, so no kind view ever surfaces it.
        assert!(dag.ready_by_kind(&kinds, CandidateKind::Epic).is_empty());
        // A blocked task never appears even though it is mapped...
        assert!(
            !dag.ready_by_kind(&kinds, CandidateKind::Task)
                .contains(&"t-review")
        );
        // ...and an UNMAPPED ready id is claimed for NO kind (the
        // projection never invents a kind it was not given).
        for kind in CandidateKind::ALL {
            assert!(
                !dag.ready_by_kind(&kinds, kind).contains(&"u-mystery"),
                "unmapped id leaked into the {kind:?} view"
            );
        }

        // Liveness composes: superseding the ready task removes it from
        // the task view without promoting the still-blocked one.
        let mut edges2 = edges;
        edges2.push(rec(RelationKind::Supersedes, "t-write-v2", "t-write"));
        let dag2 = Dag::project(&edges2).expect("still acyclic");
        assert!(
            dag2.ready_by_kind(&kinds, CandidateKind::Task).is_empty(),
            "dead task gone; t-review still gated by d-spec + u-mystery"
        );
        assert_eq!(
            dag2.ready_by_kind(&kinds, CandidateKind::Doc),
            vec!["d-spec"],
            "other kind views are untouched by the task supersession"
        );
    }

    // ---- dag projection: cycles ----

    #[test]
    fn two_cycle_and_three_cycle_are_detected_with_a_concrete_cycle() {
        let two = vec![
            rec(RelationKind::Blocks, "x", "y"),
            rec(RelationKind::Blocks, "y", "x"),
        ];
        let err = Dag::project(&two).expect_err("2-cycle must fail closed");
        assert_eq!(err.cycle, vec!["x".to_string(), "y".to_string()]);

        let three = vec![
            rec(RelationKind::Blocks, "a", "b"),
            rec(RelationKind::Blocks, "b", "c"),
            rec(RelationKind::Blocks, "c", "a"),
            rec(RelationKind::Blocks, "a", "d"), // downstream of the cycle
        ];
        let err = Dag::project(&three).expect_err("3-cycle must fail closed");
        assert_eq!(
            err.cycle,
            vec!["a".to_string(), "b".to_string(), "c".to_string()],
            "forward direction, smallest-first rotation, downstream node excluded"
        );
        let msg = err.to_string();
        assert!(msg.contains("blocks-cycle"), "{msg}");
        assert!(msg.contains("supersede"), "repair hint present: {msg}");
    }

    #[test]
    fn superseding_a_cycle_member_repairs_the_dag_append_only() {
        let mut edges = vec![
            rec(RelationKind::Blocks, "a", "b"),
            rec(RelationKind::Blocks, "b", "c"),
            rec(RelationKind::Blocks, "c", "a"),
        ];
        Dag::project(&edges).expect_err("cycle before repair");
        edges.push(rec(RelationKind::Supersedes, "fix", "b"));
        let dag = Dag::project(&edges).expect("dead member dissolves the cycle");
        // "fix" is lineage, not a blocks participant — not in the universe.
        assert_eq!(dag.ready(), vec!["c"]);
        assert_eq!(dag.blocked_by("a"), vec!["c"]);
        assert!(!dag.is_live("b"));
        assert!(!dag.is_live("fix"));
    }

    #[test]
    fn witnessing_a_cycle_member_dissolves_the_cycle_like_supersede() {
        // u-r3 decision, mirroring `superseding_a_cycle_member_...`: a
        // witnessed (done) member is inactive in the Kahn sweep exactly like
        // a superseded one, so it dissolves the cycle append-only — but,
        // unlike supersede, the done member stays LIVE (recall untouched).
        let mut edges = vec![
            rec(RelationKind::Blocks, "a", "b"),
            rec(RelationKind::Blocks, "b", "c"),
            rec(RelationKind::Blocks, "c", "a"),
        ];
        Dag::project(&edges).expect_err("cycle before the witness");
        edges.push(rec(RelationKind::Witnesses, "proof", "b"));
        let dag = Dag::project(&edges).expect("witnessed member dissolves the cycle");
        assert!(dag.is_done("b"));
        assert!(
            dag.is_live("b"),
            "witnessed != dead — b stays recallable-live"
        );
        assert_eq!(dag.done(), vec!["b"]);
        // With b done, c is unblocked and a is gated by c only — exactly the
        // supersede-repair shape.
        assert_eq!(dag.ready(), vec!["c"]);
        assert_eq!(dag.blocked_by("a"), vec!["c"]);
        assert!(!dag.is_live("proof"), "the evidence is not a blocks node");
    }

    #[test]
    fn tombstoned_ids_injected_as_dead_leave_and_repair_the_dag() {
        // w1d: a forgotten capsule is dead to the projection — never
        // ready, never blocking, and a cycle through it dissolves.
        let edges = vec![
            rec(RelationKind::Blocks, "a", "b"),
            rec(RelationKind::Blocks, "b", "c"),
            rec(RelationKind::Blocks, "c", "a"),
        ];
        Dag::project(&edges).expect_err("live cycle without the tombstone");
        let dead: BTreeSet<String> = ["b".to_string()].into_iter().collect();
        let dag =
            Dag::project_excluding(&edges, &dead).expect("tombstoned member dissolves the cycle");
        assert!(!dag.is_live("b"), "tombstoned is dead");
        assert_eq!(dag.ready(), vec!["c"], "b neither ready nor blocking c");
        assert_eq!(dag.blocked_by("a"), vec!["c"]);
        assert_eq!(dag.blocked_by("c"), Vec::<&str>::new());
    }

    #[test]
    fn cycle_error_carries_the_full_entangled_set() {
        // Two disjoint live cycles + one downstream node: the error shows
        // ONE concrete cycle but the entangled set covers everything
        // stuck, so a caller knows more remains after repairing the shown
        // one (w1d whack-a-mole fix).
        let edges = vec![
            rec(RelationKind::Blocks, "a", "b"),
            rec(RelationKind::Blocks, "b", "a"),
            rec(RelationKind::Blocks, "x", "y"),
            rec(RelationKind::Blocks, "y", "x"),
            rec(RelationKind::Blocks, "a", "down"),
        ];
        let err = Dag::project(&edges).expect_err("two live cycles");
        assert_eq!(err.cycle, vec!["a".to_string(), "b".to_string()]);
        assert_eq!(
            err.entangled,
            vec![
                "a".to_string(),
                "b".to_string(),
                "down".to_string(),
                "x".to_string(),
                "y".to_string()
            ],
            "everything cycle-stuck is named, beyond the one shown cycle"
        );
        assert!(err.to_string().contains("5 id(s) cycle-entangled"));
    }

    // ---- grounded_in_cycle: mission-spine cycle check (planning-plane u2) ----
    //
    // Independent of Dag/project: grounded_in is NOT a dag input (proven
    // above), so the mission spine needs its OWN fail-closed cycle check
    // over the child->parent grounded_in subgraph. Reuses the same private
    // find_cycle helper the blocks-dag uses, in-module only.

    #[test]
    fn grounded_in_cycle_is_ok_on_an_acyclic_mission_graph() {
        let edges = vec![
            rec(RelationKind::GroundedIn, "cap-10", "cap-431"),
            rec(RelationKind::GroundedIn, "cap-11", "cap-431"),
            // An unrelated kind must be ignored, not counted into the graph.
            rec(RelationKind::Blocks, "plan", "ship"),
        ];
        assert!(
            grounded_in_cycle(&edges, &BTreeSet::new()).is_ok(),
            "a tree of grounded_in edges is acyclic"
        );
        assert!(
            grounded_in_cycle(&[], &BTreeSet::new()).is_ok(),
            "no grounded_in edges at all is trivially acyclic"
        );
    }

    #[test]
    fn grounded_in_cycle_fails_closed_on_a_live_two_cycle_with_a_concrete_cycle() {
        let edges = vec![
            rec(RelationKind::GroundedIn, "cap-a", "cap-b"),
            rec(RelationKind::GroundedIn, "cap-b", "cap-a"),
        ];
        let err = grounded_in_cycle(&edges, &BTreeSet::new())
            .expect_err("a live grounded_in 2-cycle must fail closed");
        assert_eq!(err.cycle, vec!["cap-a".to_string(), "cap-b".to_string()]);
        assert_eq!(
            err.entangled,
            vec!["cap-a".to_string(), "cap-b".to_string()]
        );
    }

    #[test]
    fn grounded_in_cycle_through_a_dead_node_dissolves() {
        // 3-cycle a->b->c->a; excluding the dead middle node from the
        // graph before the sweep leaves only c->a, which is acyclic — the
        // same append-only repair shape as the blocks-dag's tombstone seam.
        let edges = vec![
            rec(RelationKind::GroundedIn, "cap-a", "cap-b"),
            rec(RelationKind::GroundedIn, "cap-b", "cap-c"),
            rec(RelationKind::GroundedIn, "cap-c", "cap-a"),
        ];
        grounded_in_cycle(&edges, &BTreeSet::new()).expect_err("live cycle before repair");
        let dead: BTreeSet<String> = ["cap-b".to_string()].into_iter().collect();
        assert!(
            grounded_in_cycle(&edges, &dead).is_ok(),
            "excluding a dead cycle member dissolves the mission cycle"
        );
    }

    // ---- property sweep: deterministic seeded generator (no external deps;
    // Cargo.toml is plan-frozen, so no proptest — an LCG gives the same
    // reproducibility with zero dependency surface) ----

    struct Lcg(u64);

    impl Lcg {
        fn next(&mut self) -> u64 {
            self.0 = self
                .0
                .wrapping_mul(6364136223846793005)
                .wrapping_add(1442695040888963407);
            self.0
        }

        fn below(&mut self, n: u64) -> u64 {
            self.next() % n
        }
    }

    /// Oracle recomputed straight from the raw edges, independently of the
    /// projection's internals.
    fn oracle_live(edges: &[RelationRecord], id: &str) -> bool {
        !edges
            .iter()
            .any(|r| r.kind() == RelationKind::Supersedes && r.to_id() == id)
    }

    fn oracle_live_blockers<'a>(edges: &'a [RelationRecord], id: &str) -> Vec<&'a str> {
        let mut v: Vec<&str> = edges
            .iter()
            .filter(|r| {
                r.kind() == RelationKind::Blocks
                    && r.to_id() == id
                    && oracle_live(edges, r.from_id())
            })
            .map(RelationRecord::from_id)
            .collect();
        v.sort_unstable();
        v.dedup();
        v
    }

    #[test]
    fn property_sweep_random_layered_dags_agree_with_the_raw_edge_oracle() {
        let mut lcg = Lcg(0x5EED_CAFE);
        for round in 0..40u32 {
            let n = 2 + lcg.below(9) as usize; // 2..=10 spine nodes
            let names: Vec<String> = (0..n).map(|i| format!("t{i:02}")).collect();
            let mut recs: Vec<RelationRecord> = Vec::new();
            for w in names.windows(2) {
                recs.push(rec(RelationKind::Blocks, &w[0], &w[1]));
            }
            // Random extra FORWARD edges keep the graph acyclic by construction.
            for _ in 0..lcg.below(6) {
                let i = lcg.below(n as u64 - 1) as usize;
                let j = i + 1 + lcg.below((n - 1 - i) as u64) as usize;
                recs.push(rec(RelationKind::Blocks, &names[i], &names[j]));
            }
            // Random supersessions kill some nodes.
            for (i, name) in names.iter().enumerate() {
                if lcg.below(4) == 0 {
                    recs.push(rec(RelationKind::Supersedes, &format!("fix{i:02}"), name));
                }
            }

            let dag = Dag::project(&recs).expect("forward-only graph must project");
            let dag2 = Dag::project(&recs).expect("determinism re-projection");
            assert_eq!(dag.ready(), dag2.ready(), "round {round}: deterministic");

            // Membership contract (w1d): the universe is the BLOCKS
            // endpoints; a supersedes-only endpoint (fixNN) is never live.
            let universe: BTreeSet<&str> = recs
                .iter()
                .filter(|r| r.kind() == RelationKind::Blocks)
                .flat_map(|r| [r.from_id(), r.to_id()])
                .collect();
            for r in &recs {
                if r.kind() == RelationKind::Supersedes && !universe.contains(r.from_id()) {
                    assert!(
                        !dag.is_live(r.from_id()),
                        "round {round}: supersedes-only endpoint {} outside the universe",
                        r.from_id()
                    );
                }
            }
            let ready = dag.ready();
            for id in &universe {
                let live = oracle_live(&recs, id);
                let blockers = oracle_live_blockers(&recs, id);
                assert_eq!(dag.is_live(id), live, "round {round}: liveness of {id}");
                assert_eq!(
                    dag.blocked_by(id),
                    blockers,
                    "round {round}: blocked_by({id})"
                );
                assert_eq!(
                    ready.contains(id),
                    live && blockers.is_empty(),
                    "round {round}: ready ⟺ live ∧ zero live blockers, id {id}"
                );
            }

            // Cyclic variant: same spine (all live — no supersessions kept)
            // plus a back-edge closing the chain. The reported cycle must be
            // real: every consecutive pair (and last→first) is an input edge.
            let mut cyclic: Vec<RelationRecord> = Vec::new();
            for w in names.windows(2) {
                cyclic.push(rec(RelationKind::Blocks, &w[0], &w[1]));
            }
            cyclic.push(rec(RelationKind::Blocks, &names[n - 1], &names[0]));
            let err = Dag::project(&cyclic).expect_err("spine + back-edge must cycle");
            let c = &err.cycle;
            assert!(c.len() >= 2, "round {round}: cycle has at least two nodes");
            let distinct: BTreeSet<&String> = c.iter().collect();
            assert_eq!(distinct.len(), c.len(), "round {round}: members distinct");
            assert_eq!(
                c.first(),
                c.iter().min(),
                "round {round}: smallest-first rotation"
            );
            for k in 0..c.len() {
                let a = &c[k];
                let b = &c[(k + 1) % c.len()];
                assert!(
                    cyclic.iter().any(|r| r.kind() == RelationKind::Blocks
                        && r.from_id() == a
                        && r.to_id() == b),
                    "round {round}: reported edge {a}->{b} must exist in the input"
                );
            }
        }
    }

    // ---- record: the `at` accessor + instant round-trip ----

    #[test]
    fn at_accessor_returns_the_injected_instant_and_survives_serde() {
        // `at` is caller-injected (the module reads no clock); the accessor
        // must return exactly what was injected, and the instant must survive
        // a serde round-trip unchanged (RFC3339, canonical-JSON discipline).
        let r = rec(RelationKind::Witnesses, "evidence", "claim");
        assert_eq!(r.at(), at(), "accessor returns the injected instant");
        let back: RelationRecord =
            serde_json::from_str(&serde_json::to_string(&r).expect("serializes"))
                .expect("deserializes");
        assert_eq!(back.at(), at(), "instant survives the serde round-trip");
        assert_eq!(back.at(), r.at(), "round-trip is instant-stable");
    }

    // ---- dag: honest blocked_by for a dead (superseded) target ----

    #[test]
    fn blocked_by_gives_an_honest_answer_for_a_dead_target_never_ready() {
        // The documented contract: liveness filtering is on the BLOCKER side,
        // so a dead (superseded) TARGET still gets an honest `blocked_by` — its
        // live blockers are named — yet it is never `ready`.
        let edges = vec![
            rec(RelationKind::Blocks, "gate", "victim"),
            rec(RelationKind::Supersedes, "victim2", "victim"),
        ];
        let dag = Dag::project(&edges).expect("acyclic");
        assert!(!dag.is_live("victim"), "victim superseded → dead");
        assert_eq!(
            dag.blocked_by("victim"),
            vec!["gate"],
            "a dead target still gets an honest blocked_by"
        );
        assert!(
            !dag.ready().contains(&"victim"),
            "but a dead id is never ready"
        );
        // The live, unblocked blocker is the only ready node.
        assert_eq!(dag.ready(), vec!["gate"]);
    }

    // ---- find_cycle: the defensive guards, proven by direct unit call ----
    //
    // find_cycle is a private helper the Kahn sweep only ever reaches with a
    // non-empty leftover whose members each keep an in-leftover blocker. It
    // still guards two degenerate inputs; these pin the guard CONTRACT via a
    // direct call (the pure helper must behave on all inputs of its type), even
    // though `project`/`project_excluding` never construct them.

    #[test]
    fn find_cycle_returns_empty_for_an_empty_leftover() {
        let leftover: BTreeSet<&str> = BTreeSet::new();
        let blockers: BTreeMap<String, BTreeSet<String>> = BTreeMap::new();
        assert!(
            find_cycle(&leftover, &blockers).is_empty(),
            "an empty leftover names no cycle"
        );
    }

    #[test]
    fn find_cycle_falls_back_to_the_sorted_leftover_when_no_in_edge_closes() {
        // Both leftover members' only blockers sit OUTSIDE the leftover, so the
        // backward walk finds no in-leftover in-edge and breaks — dropping to
        // the documented fallback: the sorted leftover set itself.
        let leftover: BTreeSet<&str> = ["b", "a"].into_iter().collect();
        let mut blockers: BTreeMap<String, BTreeSet<String>> = BTreeMap::new();
        blockers.insert(
            "a".to_string(),
            ["outsider".to_string()].into_iter().collect(),
        );
        blockers.insert(
            "b".to_string(),
            ["outsider".to_string()].into_iter().collect(),
        );
        assert_eq!(
            find_cycle(&leftover, &blockers),
            vec!["a".to_string(), "b".to_string()],
            "fallback returns the sorted leftover set"
        );
    }
}
